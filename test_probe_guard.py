"""The probe guard's refusal rules, and the operator tools around it.

The guard is the only thing standing between an unattended probe and the
operator's live session, and the two ways it can be wrong are opposite: a
refusal that should be a pass merely annoys, while a pass that should be a
refusal destroys a recording and produces a measurement that looks real. These
cases pin the fail-closed half, which cannot be exercised by running the guard
for real because the process table always reads successfully here.

The static cases alongside them pin rules whose only other witness is a run on
the rig: a probe that cannot import the guard, a gate `python -O` would strip,
a settle deadline that outlives its own backstop, and a NIC check that reports
success without reading an adapter. None of them opens a camera, a serial port
or a network adapter.

    python test_probe_guard.py
"""
import ast
import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_app import probe_guard as g

FAILURES = []


class _Row:
    """A psutil row stand-in: the guard reads only ``.info``."""

    def __init__(self, info):
        self.info = info


def _runs_at_import(node):
    """Every node ``node`` executes when the module is imported.

    A function or class body runs only when it is called, and the
    ``__main__`` guard runs only when the file is the entry point, so neither
    is descended into; every other nested statement does run at import.
    """
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)):
            continue
        if isinstance(child, ast.If) and "__name__" in ast.dump(child.test):
            continue
        yield from _runs_at_import(child)


def _import_roots(node):
    """The top-level package names an Import/ImportFrom node brings in."""
    if isinstance(node, ast.ImportFrom):
        names = [node.module or ""]
    else:
        names = [a.name for a in node.names]
    return [n.split(".")[0] for n in names]


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
    read_table = g._process_table          # kept before any test stubs it

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
                 "probe_zerocopy.py", "probe_seq.py"):
        check(f"{name} is a marker", name in g.PANOPTICON_MARKERS)
    check("probe_network.py is not a marker",
          "probe_network.py" not in g.PANOPTICON_MARKERS)

    repo = Path(__file__).resolve().parent

    # 2b. A guard that is never reached guards nothing, so the call SITE is
    #     checked as well as the rule. sys.path[0] for a probe under
    #     tools/experiments/ is that probe's own directory, so every
    #     gui_app/tools import in it must sit below the module-level path
    #     insert. One placed later -- inside main(), beside the guard call --
    #     raises ModuleNotFoundError before argparse or the guard ever run.
    exp = sorted((repo / "tools" / "experiments").glob("probe_*.py"))
    check("tools/experiments holds the moved probes", bool(exp), str(exp))
    for f in exp:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        insert = min((n.lineno for n in _runs_at_import(tree)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)
                      and n.func.attr == "insert"
                      and ast.unparse(n.func.value) == "sys.path"),
                     default=None)
        pkg = min((n.lineno for n in ast.walk(tree)
                   if isinstance(n, (ast.Import, ast.ImportFrom))
                   and any(r in ("gui_app", "tools")
                           for r in _import_roots(n))),
                  default=None)
        if pkg is None:
            continue
        check(f"{f.name} joins sys.path before its first package import",
              insert is not None and insert < pkg,
              f"insert at {insert}, first gui_app/tools import at {pkg}")

    # 2c. The same rule proved end to end, from a foreign working directory and
    #     without hardware: --help must answer. Compiling a file cannot catch
    #     an import that runs too early -- only running it can.
    for f in exp:
        r = subprocess.run([sys.executable, str(f), "--help"],
                           cwd=tempfile.gettempdir(), capture_output=True,
                           text=True)
        check(f"{f.name} --help runs from another directory",
              r.returncode == 0 and "usage" in r.stdout.lower(),
              (r.stderr or r.stdout).strip()[-200:])

    # 2d. No test module parses argv at import: a collector that imports every
    #     test_*.py would otherwise consume the HOST's argv and exit before a
    #     line of the code under test runs.
    for f in sorted(repo.glob("test_*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        at_import = [c.lineno for c in _runs_at_import(tree)
                     if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Attribute)
                     and c.func.attr in ("parse_args", "parse_known_args")]
        check(f"{f.name} does not parse argv at import", not at_import,
              f"parsed at line(s) {at_import}")

    # 2e. Rules in the probes and the operator script whose failure only shows
    #     on the rig, pinned here by reading the source instead.
    zc = repo / "tools" / "experiments" / "probe_zerocopy.py"
    if zc.exists():
        # A padding gate written as `assert` leaves the InstantCamera open when
        # it fires -- the next run cannot claim the device -- and `python -O`
        # strips it, turning the gate on a hot-path change into a no-op.
        asserts = [n.lineno for n in
                   ast.walk(ast.parse(zc.read_text(encoding="utf-8")))
                   if isinstance(n, ast.Assert)]
        check("probe_zerocopy.py gates without assert", not asserts,
              f"assert at line(s) {asserts}")

    ab = repo / "probe_abuse.py"
    if ab.exists():
        # Every settle poll takes the ONE shared deadline. A poll given its own
        # `perf_counter() + N` outlives the hard backstop, so the invariant's
        # verdict is replaced by "TIMED OUT" in the failing case it exists for.
        tree = ast.parse(ab.read_text(encoding="utf-8"))
        # settle_then reschedules itself with its own parameter, so the call
        # inside its body is not a caller and is excluded.
        inner = {id(c) for d in ast.walk(tree)
                 if isinstance(d, ast.FunctionDef) and d.name == "settle_then"
                 for c in ast.walk(d)}
        args_used = [ast.unparse(c.args[0]) for c in ast.walk(tree)
                     if isinstance(c, ast.Call) and id(c) not in inner
                     and isinstance(c.func, ast.Name)
                     and c.func.id == "settle_then" and c.args]
        check("probe_abuse.py settles against one shared deadline",
              args_used and all(a == "settle_deadline" for a in args_used),
              f"deadlines passed: {args_used}")

    nic = repo / "configure_nic.ps1"
    if nic.exists():
        ps = nic.read_text(encoding="utf-8")
        # Comment lines are dropped first: the rules below are also STATED in
        # comments there, and a check that reads its own rule text passes
        # whatever the code does.
        code = "\n".join(l for l in ps.splitlines()
                          if not l.lstrip().startswith("#"))
        # PowerShell unrolls a collection on return, so `return @()` hands the
        # caller $null -- which is the same function's "unreadable" answer.
        check("configure_nic.ps1 returns empty arrays with the unary comma",
              "return @()" not in code)
        # A foreach over the query itself runs zero times when the query
        # returns nothing, which reports success for ports never read.
        check("configure_nic.ps1 never iterates an RSS query inline",
              "in (Get-NetAdapterRss" not in code)
        # Both read-backs prove one object came back per port first.
        for token in ("$now.Count -eq $Ports.Count", "$silent"):
            check(f"configure_nic.ps1 counts the read-back ({token})",
                  token in code)

    # 3. A real read returns rows, so the psutil path works on this machine.
    real = g._process_table()
    check("process table reads as a list", isinstance(real, list) and real,
          f"got {type(real).__name__}")
    check("this process appears in the table",
          any(pid == me for pid, _p, _c in (real or [])))
    check("this process's own command line is readable",
          any(pid == me and "python" in cmd.lower()
              for pid, _p, cmd in (real or [])))

    tmp = Path(tempfile.mkdtemp(prefix="guard_test_"))
    g.LOCK = tmp / "probe_out" / ".gui_probe.lock"

    # 4. An unreadable process table refuses, because "nobody knows what is
    #    running" must not be treated as "nothing is running".
    g._process_table = lambda: None
    expect_refusal("unknown process table refuses")
    check("a refusal takes no lock", not g.LOCK.exists())

    # 4b. A table in which even this process has no command line is UNKNOWN,
    #     not empty. psutil reports a per-process AccessDenied as cmdline None
    #     instead of raising, and a process NAME never holds a marker, so a
    #     broadly denied table would otherwise enumerate happily, match nothing
    #     and report "clear to run" while a GUI records.
    import psutil
    real_iter = psutil.process_iter
    psutil.process_iter = lambda _attrs: [
        _Row({"pid": me, "ppid": 1, "cmdline": None, "name": "python.exe"}),
        _Row({"pid": 4444, "ppid": 1, "cmdline": None, "name": "python.exe"}),
    ]
    try:
        check("a table with no readable command lines reads as unknown",
              read_table() is None)
    finally:
        psutil.process_iter = real_iter

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

    # 10. A forced run must not log a refusal for a run that proceeded: the
    #     operator log is read after the fact, and "REFUSING TO START" above a
    #     run that started says the opposite of what happened.
    for what, table in (("an unknown table", None),
                        ("a live holder",
                         [(me, 0, "python test_probe_guard.py"),
                          (4243, 0, "python -m http.server")])):
        g.LOCK.write_text("4243")
        g._process_table = (lambda t: (lambda: t))(table)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            g.refuse_if_panopticon_running(force=True)
        out = buf.getvalue()
        check(f"--force over {what} logs an override, not a refusal",
              "REFUSING TO START" not in out and "overriding" in out,
              " ".join(out.split())[:160])

    # 11. The lock is released only by the process that still owns it: --force
    #     can take over a lock whose holder is still running, and deleting that
    #     file when this process exits would leave the holder unprotected and
    #     let the next probe's check pass against it.
    g.LOCK.write_text("4243")
    g._release_lock()
    check("another pid's lock survives this process's exit handler",
          g.LOCK.exists() and g.LOCK.read_text() == "4243")
    g.LOCK.write_text(str(me))
    g._release_lock()
    check("this process's own lock is removed on exit", not g.LOCK.exists())

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
        return 1
    print("\nALL PROBE GUARD TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
