"""Refuse to start a probe while any Panopticon is already running.

Two instances enumerate the same cameras and fight over them, and the lag that
produces looks exactly like a laggard bug: three concurrent instances have
produced false "divergence" findings that drove two since-reverted code changes.

Both checks below are required, because each alone has a gap:

  * A command-line scan alone refuses to start at all, because `uv run` spawns
    a second python in the same tree and the guard detects its own parent.
  * A lock file alone misses a GUI launched from the desktop shortcut, which
    never takes a probe lock. A probe would start alongside it and ruin both
    the session and the measurement.

So do both. Scan for any Panopticon process, excluding this process's own
ancestry, AND hold a lock so two probes cannot race each other. Never delete
the lock file to "clean up" before launching -- that throws the protection away.

RULE: the guard fails closed. A process table that cannot be read is a refusal,
not a pass, because a guard that gives up when its own instrument fails protects
nothing, and an instrument failure is indistinguishable from the state the guard
exists to catch. ``--force`` is the operator's deliberate override after
checking the machine by hand.
"""
from __future__ import annotations

import atexit
import ntpath
import os
import re
import sys
from pathlib import Path

#: File names that identify a Panopticon process. Every entry point that opens
#: cameras, the trigger board or the GUI is listed, because the guard protects
#: both directions only when each side can see the other: a probe that is
#: guarded but unlisted still refuses to start over a GUI while every other
#: probe starts over it. ``probe_network.py`` is absent: its default discovery
#: pass sends a UDP query and opens nothing, so listing it would let a harmless
#: run block every other probe.
#:
#: RULE: a marker matches an argument whose whole file name is the marker (a
#: bare name such as ``panopticon`` also matches with an extension), never a
#: fragment of the command line. REASON: the repository is usually cloned
#: into a folder named panopticon, so every program run from its virtual
#: environment carries that word in its interpreter path, and test_sim_gui.py
#: or labelgui.py end in gui.py; matching fragments refuses a probe beside
#: any of them.
PANOPTICON_MARKERS = ("gui.py", "probe_seq.py", "probe_lag.py", "probe_mp.py",
                      "probe_flir.py", "probe_abuse.py", "probe_multiproc.py",
                      "probe_release_gil.py", "probe_zerocopy.py",
                      "panopticon")

#: A multiprocessing worker started by spawn names its parent on its command
#: line: ``... spawn_main(parent_pid=1234, pipe_handle=...)``.
_SPAWN_PARENT = re.compile(r"spawn_main\(\s*parent_pid\s*=\s*(\d+)")

#: The repository root, taken from this module's own location.
REPO = Path(__file__).resolve().parents[1]

#: The lock is anchored to the repository, never to the working directory: two
#: probes launched from different directories must contend for the SAME file,
#: or each creates its own ``probe_out/`` and the mutual exclusion the lock
#: exists for never happens.
LOCK = REPO / "probe_out" / ".gui_probe.lock"


def add_force_argument(parser) -> None:
    """Give a probe's parser the guard's override flag.

    Every guarded probe offers the same flag under the same name, so an
    operator who has checked the machine by hand does not have to learn a
    per-probe spelling.
    """
    parser.add_argument("--force", action="store_true",
                        help="start even if the process table cannot be read "
                             "or another probe's lock is present")


def _process_table() -> list[tuple[int, int, str]] | None:
    """(pid, ppid, command line) for every visible process, or None when the
    table cannot be read.

    None and [] mean opposite things -- "nobody knows what is running" versus
    "nothing is running" -- and every caller keeps them apart.

    psutil reports the command line as a LIST of arguments, so an argument
    containing a newline stays inside its own row. A text table of one line per
    process loses everything after such a newline and misses a marker that sits
    on a later line.

    RULE: this process's own row is the canary. A table in which even this
    process has no command line is UNKNOWN, not empty.
    REASON: psutil turns a per-process AccessDenied into ``cmdline == None``
    instead of raising, and the process NAME alone never contains a marker. On
    a restricted account every row can come back that way, so the scan would
    enumerate happily, match nothing and report "clear to run" while a GUI is
    recording -- the fail-open this guard exists to close. A process can always
    read its own command line, so losing that one means command lines are not
    readable here at all.
    """
    try:
        import psutil
    except Exception as exc:
        print(f"[guard] psutil is unavailable: {exc}", flush=True)
        return None
    rows: list[tuple[int, int, str]] = []
    own_cmdline = False
    try:
        for proc in psutil.process_iter(["pid", "ppid", "cmdline", "name"]):
            info = proc.info
            argv = info.get("cmdline") or []
            pid = int(info["pid"])
            if argv and pid == os.getpid():
                own_cmdline = True
            cmd = " ".join(argv) if argv else (info.get("name") or "")
            rows.append((pid, int(info.get("ppid") or 0), cmd))
    except Exception as exc:
        print(f"[guard] could not read the process table: {exc}", flush=True)
        return None
    if not own_cmdline:
        print("[guard] the process table came back without this process's own "
              "command line, so command lines are not readable here and no "
              "marker could ever match.", flush=True)
        return None
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


def _file_names(cmd: str):
    """Each argument of a command line as a lower-case file name."""
    for tok in re.split(r"[\s\"']+", cmd or ""):
        if tok:
            yield ntpath.basename(tok).lower()


def is_panopticon_command(cmd: str) -> bool:
    """Whether a command line runs a Panopticon entry point (see
    PANOPTICON_MARKERS for the matching rule)."""
    marks = {m.lower() for m in PANOPTICON_MARKERS}
    for name in _file_names(cmd):
        if name in marks:
            return True
        stem = name.rsplit(".", 1)[0] if "." in name else name
        if stem in marks and stem != name:
            return True
    return False


def spawn_parent(cmd: str) -> int | None:
    """The parent pid a multiprocessing spawn child names, or None."""
    m = _SPAWN_PARENT.search(cmd or "")
    return int(m.group(1)) if m else None


def find_others(rows: list[tuple[int, int, str]]) -> list[tuple[int, str]]:
    """Panopticon processes in ``rows`` other than this one and its ancestors.

    A capture worker (a spawn child) counts when its parent is a Panopticon
    process or is gone. RULE: an orphaned worker refuses a probe as its
    parent would have. REASON: it still holds its cameras and its NVENC
    sessions, and a probe started beside it measures the contention, which
    reads as the lag the probe exists to find.
    """
    mine = _own_lineage(rows)
    live = {pid for pid, _ppid, _cmd in rows}
    ours = {pid for pid, _ppid, cmd in rows if is_panopticon_command(cmd)}
    out = []
    for pid, _ppid, cmd in rows:
        if pid in mine:
            continue
        text = " ".join(cmd.split())[:120]
        if pid in ours:
            out.append((pid, text))
            continue
        parent = spawn_parent(cmd)
        if parent is None or parent in mine:
            continue
        if parent not in live:
            out.append((pid, f"{text}  [a worker whose parent pid {parent} "
                             f"has exited]"))
        elif parent in ours:
            out.append((pid, f"{text}  [a worker of Panopticon pid {parent}]"))
    return out


def _release_lock() -> None:
    """Drop the lock on exit, but only while it still names this process.

    RULE: a lock file naming another pid belongs to another probe and is left
    alone.
    REASON: ``--force`` may take over a lock whose holder is still running.
    Deleting that file when this process exits would leave the running probe
    with no lock at all and let the next probe's check pass against it, so the
    half of the guard that protects probe against probe would disappear exactly
    after the one command that says a human is watching.
    """
    try:
        held = int(LOCK.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return
    if held == os.getpid():
        try:
            LOCK.unlink()
        except OSError:
            pass


def refuse_if_panopticon_running(*, take_lock: bool = True,
                                 force: bool = False) -> None:
    """Exit(3) unless nothing Panopticon-shaped runs and the lock is free.

    ``force`` skips both refusals for an operator who has checked by hand; it
    still takes the lock afterwards, so the next probe sees this one.
    """
    rows = _process_table()

    if rows is None:
        if not force:
            print("[guard] REFUSING TO START: the process table could not be "
                  "read, so whether a Panopticon is already running is "
                  "UNKNOWN. Check by hand and re-run with --force to "
                  "override.", flush=True)
            sys.exit(3)
        print("[guard] process table unknown and --force given: starting "
              "without the process check.", flush=True)
    else:
        others = find_others(rows)
        if others:
            print("[guard] REFUSING TO START: Panopticon is already running, "
                  "and two instances fight over the same cameras:", flush=True)
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
        if held and held != os.getpid():
            # RULE: the refusal text belongs only to the branch that refuses; a
            # forced run says it is overriding.
            # REASON: an operator log is read after the fact, and a run that
            # went ahead under --force while its log says "REFUSING TO START"
            # tells the reader the opposite of what happened.
            if rows is None:
                # An unknown table cannot show the holder is gone, and a lock
                # declared stale is a lock overwritten, so unknown means HELD.
                if not force:
                    print(f"[guard] REFUSING TO START: {LOCK} is held by pid "
                          f"{held} and the process table is unknown, so the "
                          f"lock cannot be shown to be stale. Re-run with "
                          f"--force to override.", flush=True)
                    sys.exit(3)
                print(f"[guard] overriding a lock held by pid {held} under an "
                      f"unknown process table (--force).", flush=True)
            elif held in {pid for pid, _p, _c in rows}:
                if not force:
                    print(f"[guard] REFUSING TO START: another probe holds "
                          f"{LOCK} (pid {held}).", flush=True)
                    sys.exit(3)
                print(f"[guard] overriding a lock held by running pid {held} "
                      f"(--force).", flush=True)
            else:
                print(f"[guard] clearing a stale lock from pid {held}",
                      flush=True)
    LOCK.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(_release_lock)
    print(f"[guard] clear to run; holding {LOCK} (pid {os.getpid()})",
          flush=True)
