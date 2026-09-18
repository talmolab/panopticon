"""The probe guard's refusal rules, with the process table stubbed.

The guard is the only thing standing between an unattended probe and the
operator's live session, and the two ways it can be wrong are opposite: a
refusal that should be a pass merely annoys, while a pass that should be a
refusal destroys a recording and produces a measurement that looks real. These
cases pin the fail-closed half, which cannot be exercised by running the guard
for real because the process table always reads successfully here.

    python test_probe_guard.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_app import probe_guard as g

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS  {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL  {name}  {detail}")


def expect_refusal(name, **kwargs):
    """The guard must exit(3); anything else (including returning) is a fail."""
    try:
        g.refuse_if_panopticon_running(**kwargs)
    except SystemExit as e:
        check(name, e.code == 3, f"exited {e.code}, want 3")
        return
    check(name, False, "returned instead of refusing")


def expect_pass(name, **kwargs):
    try:
        g.refuse_if_panopticon_running(**kwargs)
    except SystemExit as e:
        check(name, False, f"refused with exit {e.code}")
        return
    check(name, True)


def main():
    me = os.getpid()

    # 1. The lock is anchored to the repository, so two probes started from
    #    different working directories contend for the same file.
    check("lock path is absolute", g.LOCK.is_absolute(), str(g.LOCK))
    check("lock lives under the repository",
          g.LOCK.parent.parent == Path(__file__).resolve().parent,
          f"{g.LOCK} vs {Path(__file__).resolve().parent}")

    # 2. Every hardware-opening probe is a marker, and the read-only discovery
    #    probe is not, or a harmless run would block every other probe.
    for name in ("gui.py", "probe_lag.py", "probe_abuse.py",
                 "probe_multiproc.py", "probe_release_gil.py",
                 "probe_zerocopy.py", "probe_seq.py", "probe_gui_record.py"):
        check(f"{name} is a marker", name in g.PANOPTICON_MARKERS)
    check("probe_network.py is not a marker",
          "probe_network.py" not in g.PANOPTICON_MARKERS)

    # 3. A real read returns rows, so the psutil path works on this machine.
    real = g._process_table()
    check("process table reads as a list", isinstance(real, list) and real,
          f"got {type(real).__name__}")
    check("this process appears in the table",
          any(pid == me for pid, _p, _c in (real or [])))

    tmp = Path(tempfile.mkdtemp(prefix="guard_test_"))
    g.LOCK = tmp / "probe_out" / ".gui_probe.lock"

    # 4. An unreadable process table refuses, because "nobody knows what is
    #    running" must not be treated as "nothing is running".
    g._process_table = lambda: None
    expect_refusal("unknown process table refuses")
    check("a refusal takes no lock", not g.LOCK.exists())

    # 5. --force is the deliberate override and still takes the lock.
    expect_pass("unknown process table with --force starts", force=True)
    check("--force takes the lock", g.LOCK.exists())

    # 6. With an unknown table an existing lock is HELD, never stale: nothing
    #    can show the holder is gone, and declaring it stale overwrites it.
    g.LOCK.write_text("999999")
    expect_refusal("unknown table treats an existing lock as held")
    check("the held lock is not overwritten", g.LOCK.read_text() == "999999")
    expect_pass("--force overrides a lock under an unknown table", force=True)
    check("--force takes over the lock", g.LOCK.read_text() == str(me))
    g.LOCK.unlink()

    # 7. A marker on a later line of a multi-line command line is still found;
    #    a parser that keeps only the first line of a row misses it.
    g._process_table = lambda: [
        (me, 1, "python test_probe_guard.py"),
        (4242, 1, 'python -c "import gui_app\ngui.py main()"'),
    ]
    expect_refusal("marker after an embedded newline is detected")

    # 8. A running Panopticon refuses; this process and its ancestors do not
    #    count, or `uv run`'s launcher would refuse against itself.
    g._process_table = lambda: [(me, 0, "python probe_lag.py"),
                                (4444, 1, "python gui.py")]
    expect_refusal("a running GUI refuses")
    g._process_table = lambda: [(me, 7, "python probe_lag.py"),
                                (7, 0, "uv run probe_lag.py")]
    expect_pass("own ancestry does not refuse", take_lock=False)

    # 9. With a known table, a lock from a pid that is gone is stale and a lock
    #    from a live pid is held.
    g.LOCK.parent.mkdir(parents=True, exist_ok=True)
    g.LOCK.write_text("999999")
    g._process_table = lambda: [(me, 0, "python test_probe_guard.py")]
    expect_pass("a lock from a dead pid is cleared")
    check("the cleared lock names this process", g.LOCK.read_text() == str(me))
    g.LOCK.write_text("4243")
    g._process_table = lambda: [(me, 0, "python test_probe_guard.py"),
                                (4243, 0, "python -m http.server")]
    expect_refusal("a lock from a live pid is held")
    check("the held lock is left alone", g.LOCK.read_text() == "4243")

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("\nALL PROBE GUARD TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
