"""Refuse to start a probe while any Panopticon is already running.

Two instances enumerate the same cameras and fight over them, and the lag that
produces looks exactly like a laggard bug. On 2026-09-14 three concurrent
instances -- launched by loop scripts that outlived the pkill meant to stop them
-- produced two "divergence" findings that drove two code changes, both since
reverted.

Two earlier attempts at this guard each failed in an instructive way:

  * A command-line scan alone refused to start at all, because `uv run` spawns
    a second python in the same tree and the guard detected its own parent.
  * A lock file alone missed the case that actually matters most: **Isaac's own
    GUI**, launched from the desktop shortcut, never takes a probe lock. A probe
    would happily start alongside it and ruin both his session and the
    measurement.

So do both. Scan for any Panopticon process, excluding this process's own
ancestry, AND hold a lock so two probes cannot race each other. Never delete
the lock file to "clean up" before launching -- that throws the protection away.
"""
from __future__ import annotations

import atexit
import os
import subprocess
import sys
from pathlib import Path

#: Command-line fragments that identify a Panopticon process.
PANOPTICON_MARKERS = ("gui.py", "probe_gui_record.py", "probe_seq.py",
                      "probe_lag.py", "panopticon")

LOCK = Path("probe_out") / ".gui_probe.lock"


def _ps(cmd: str, timeout: float = 60.0) -> str:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception as exc:
        print(f"[guard] could not query processes: {exc}", flush=True)
        return ""


def _process_table() -> list[tuple[int, int, str]]:
    """(pid, ppid, commandline) for every python-ish process."""
    out = _ps(
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' } | "
        "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t"
        "$($_.CommandLine)\" }")
    rows = []
    for line in out.splitlines():
        bits = line.split("\t", 2)
        if len(bits) < 3 or not bits[0].strip().isdigit():
            continue
        rows.append((int(bits[0]), int(bits[1] or 0), bits[2]))
    return rows


def _own_lineage(rows: list[tuple[int, int, str]]) -> set[int]:
    """This process plus every ancestor, so `uv run`'s launcher is not flagged."""
    parent = {pid: ppid for pid, ppid, _ in rows}
    mine = {os.getpid()}
    cur = parent.get(os.getpid())
    while cur and cur not in mine:
        mine.add(cur)
        cur = parent.get(cur)
    return mine


def refuse_if_panopticon_running(*, take_lock: bool = True) -> None:
    """Exit(3) if another Panopticon -- GUI or probe -- is already running."""
    rows = _process_table()
    mine = _own_lineage(rows)
    others = [(pid, cmd.strip()[:80]) for pid, _ppid, cmd in rows
              if pid not in mine
              and any(m in cmd.lower() for m in PANOPTICON_MARKERS)]
    if others:
        print("[guard] REFUSING TO START: Panopticon is already running, and "
              "two instances fight over the same cameras:", flush=True)
        for pid, cmd in others:
            print(f"[guard]   pid {pid}  {cmd}", flush=True)
        sys.exit(3)

    if not take_lock:
        return
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    if LOCK.exists():
        try:
            held = int(LOCK.read_text().split()[0])
        except Exception:
            held = None
        live = {pid for pid, _p, _c in rows}
        if held and held != os.getpid() and held in live:
            print(f"[guard] REFUSING TO START: another probe holds {LOCK} "
                  f"(pid {held}).", flush=True)
            sys.exit(3)
        if held:
            print(f"[guard] clearing a stale lock from pid {held}", flush=True)
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: LOCK.unlink(missing_ok=True))
    print(f"[guard] clear to run; holding {LOCK} (pid {os.getpid()})", flush=True)
