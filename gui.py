"""Panopticon Acquisition GUI — launch with: conda run -n 3dpose python gui.py"""
import ntpath
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

from PyQt5.QtWidgets import QApplication, QSplashScreen, QMessageBox
from PyQt5.QtCore import Qt, QThread
from PyQt5.QtGui import QColor, QFont, QPainter, QPixmap

LOG_DIR = Path(__file__).parent / "logs"

#: Seconds before the same failure may open a second dialog.
REPEAT_DIALOG_S = 30.0

#: Where this launch's log went, for the failure dialogs to name.
_LOG_PATH = None

#: Non-modal error dialogs still on screen. A Qt widget with no parent and no
#: Python reference is collected while it is visible.
_OPEN_DIALOGS = []


class _Tee:
    """Write to several streams at once (e.g. the console and a log file).

    Under the pythonw launcher sys.stdout/stderr are None, so the diagnostic
    prints from the grab threads would otherwise be discarded — this captures
    them to a file so a crash can be diagnosed after the fact."""
    def __init__(self, *streams):
        self._streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self._streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self._streams:
            try:
                s.flush()
            except Exception:
                pass


def _setup_logging():
    """Tee stdout/stderr to a timestamped log file under logs/ (pythonw discards
    them otherwise). Returns the log path, or None if logging couldn't be set up."""
    try:
        LOG_DIR.mkdir(exist_ok=True)
        log_path = LOG_DIR / f"panopticon_{datetime.now():%Y%m%d_%H%M%S}.log"
        f = open(log_path, "a", buffering=1, encoding="utf-8")
        sys.stdout = _Tee(sys.__stdout__, f)
        sys.stderr = _Tee(sys.__stderr__, f)
        # Dump every thread's Python stack into the log on a NATIVE crash
        # (access violation / abort — e.g. Qt's 0xc0000409 fail-fast), which
        # sys.excepthook can't see. Needs the real file, not the _Tee.
        import faulthandler
        faulthandler.enable(file=f, all_threads=True)
        print(f"[startup] logging to {log_path}", flush=True)
        return log_path
    except Exception:
        return None


def _log_location() -> str:
    return str(_LOG_PATH) if _LOG_PATH else "(logging could not be set up)"


def _install_excepthook():
    """Surface unhandled exceptions as a dialog + log entry instead of letting
    them escape a Qt slot — PyQt responds to that by calling abort(), which is
    a silent crash (a camera that raised during teardown was one).

    RULE: one dialog per distinct failure per REPEAT_DIALOG_S, and it is not
    modal. REASON: a modal box runs a nested event loop in which the preview
    and thermal timers keep firing, so a failure inside a timer slot opens a
    new dialog on every tick — hundreds stacked within seconds, while the
    cameras are still recording and the Stop toggle is behind all of them.
    Every occurrence still reaches the log.
    """
    shown: dict = {}

    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        print(f"[UNHANDLED]\n{msg}", flush=True)
        try:
            app = QApplication.instance()
            # QMessageBox is only safe on the GUI thread.
            if app is None or QThread.currentThread() != app.thread():
                return
            frame = traceback.extract_tb(tb)[-1] if tb is not None else None
            key = (exc_type.__name__, getattr(frame, "filename", ""),
                   getattr(frame, "lineno", 0))
            now = time.monotonic()
            if now - shown.get(key, -REPEAT_DIALOG_S) < REPEAT_DIALOG_S:
                return
            shown[key] = now
            box = QMessageBox(QMessageBox.Critical,
                              "Panopticon — Unexpected Error",
                              f"{exc_type.__name__}: {exc}\n\nThe app is still "
                              f"running. Every occurrence, including repeats "
                              f"of this one, is logged to:\n{_log_location()}")
            box.setAttribute(Qt.WA_DeleteOnClose)
            box.setModal(False)
            box.finished.connect(lambda _r, b=box: _forget_dialog(b))
            _OPEN_DIALOGS.append(box)
            box.show()
        except Exception:
            pass
    sys.excepthook = hook


def _forget_dialog(box):
    try:
        _OPEN_DIALOGS.remove(box)
    except ValueError:
        pass


def _report_startup_failure(exc):
    """Say why the window never appeared.

    RULE: a startup failure gets a dialog, not only a log line. REASON: under
    the pythonw launcher there is no console, so the splash simply vanishes
    and the only diagnostic is a file in logs/ the operator does not know
    exists — and the usual causes (the camera SDK absent, a malformed profile,
    a missing dependency) are all first-run problems, hit by exactly the
    people who cannot guess where to look.
    """
    hint = ""
    if isinstance(exc, ImportError):
        hint = ("\n\nA package this build needs is not installed. Install the "
                "project's dependencies, or choose a rig profile whose "
                "camera_backend does not need it.")
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        QMessageBox.critical(
            None, "Panopticon failed to start",
            f"{type(exc).__name__}: {exc}{hint}\n\nFull traceback:\n"
            f"{_log_location()}")
    except Exception:
        pass


#: Set to an absolute path to put the single-instance lock somewhere else.
#: The offline tests set it so they never touch the operator's own lock.
ENV_LOCK_FILE = "PANOPTICON_LOCK_FILE"

#: Exit status of a launch refused because Panopticon is already running.
EXIT_ALREADY_RUNNING = 3

#: Held for the life of the process once taken; releasing it is what lets
#: the next launch start.
_INSTANCE_LOCK = None


def _lock_path() -> Path:
    """The per-user single-instance lock file.

    Per user rather than per checkout, so a second copy launched from another
    clone or worktree is refused as well: both would open the same cameras
    and the same serial port.
    """
    override = os.environ.get(ENV_LOCK_FILE)
    if override:
        return Path(override)
    from PyQt5.QtCore import QStandardPaths
    from gui_app import settings
    base = QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation)
    return Path(base) / settings.ORG / settings.APP / "gui.lock"


def _entry_scripts() -> frozenset:
    """File names, lower-cased, of the scripts that make a process a
    Panopticon: every entry point the probe guard lists."""
    from gui_app import probe_guard
    return frozenset(m.lower() for m in probe_guard.PANOPTICON_MARKERS
                     if m.lower().endswith(".py"))


def _is_launcher(arg0: str) -> bool:
    """True for a Python interpreter (python, python3.12, pythonw, the py
    launcher) or uv, with or without a directory and ".exe"."""
    name = ntpath.basename(arg0).lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name in ("py", "uv") or name.startswith("python")


def _runs_entry_script(argv, scripts) -> bool:
    """True when ``argv`` is an interpreter, or uv, running one of ``scripts``.

    RULE: an argument counts only when its whole file name is an entry
    script, and only in an interpreter's or uv's command line. REASON: the
    command line as text matches far too much. The repository is usually
    cloned into a folder named panopticon, so every program started from its
    virtual environment has that word in its interpreter path: a notebook
    kernel, an offline script, a language server. labelgui.py ends in gui.py,
    and an editor with gui.py open names the file in its own command line.
    Refusing a launch over any of these stops the operator for nothing.
    """
    if not argv or not _is_launcher(argv[0]):
        return False
    return any(ntpath.basename(a).lower() in scripts for a in argv[1:])


def _other_panopticons():
    """(pid, command line) of other Panopticon processes, or None if unknown.

    A Panopticon process is a GUI started some other way or a probe that
    holds the cameras: an interpreter or uv running an entry script the
    probe guard lists (_runs_entry_script). This process and its ancestors
    (uv, the virtual environment's launcher) are not counted.

    RULE: unknown when this process's own command line cannot be read.
    REASON: psutil reports a command line it is not allowed to read as empty
    instead of raising, so on a restricted account every row is empty,
    nothing matches, and the scan would read as clear.
    """
    try:
        import psutil
        from gui_app import probe_guard
        scripts = _entry_scripts()
        rows, own = [], False
        for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
            info = proc.info
            argv = list(info.get("cmdline") or [])
            pid = int(info["pid"])
            if argv and pid == os.getpid():
                own = True
            rows.append((pid, int(info.get("ppid") or 0), argv))
        if not own:
            print("[startup] this process's own command line could not be "
                  "read, so the process scan cannot see any", flush=True)
            return None
        mine = probe_guard._own_lineage(rows)
        return [(pid, " ".join(" ".join(argv).split())[:120])
                for pid, _ppid, argv in rows
                if pid not in mine and _runs_entry_script(argv, scripts)]
    except Exception as e:
        print(f"[startup] the process scan failed: {e}", flush=True)
        return None


def _single_instance_refusal() -> str | None:
    """Take the single-instance lock, or say why this launch must not start.

    RULE: a second launch exits before any camera, serial, benchmark or
    firmware work. REASON: a second instance runs the launch hardware check
    (a disk speed test, two libx264 encodes and an NVENC probe that takes
    every session the driver grants) alongside the first one's recording,
    which is the contention that produces false lag, and 1.5 s later it can
    start a firmware flash on the board the first one drives.

    Two checks, because each misses a case: the lock sees another GUI however
    it was launched, and the scan sees a probe, which takes no GUI lock. A
    process table that cannot be read does not stop the launch: the lock
    still covers GUI against GUI, and a GUI that will not open on a locked
    down account is worse than a probe run beside it.
    """
    global _INSTANCE_LOCK
    from PyQt5.QtCore import QLockFile
    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[startup] could not create {path.parent}: {e}", flush=True)
    lock = QLockFile(str(path))
    # Zero: a lock is stale only when the process holding it has gone, never
    # because of its age; a GUI left running for days still holds it.
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        if lock.error() == QLockFile.LockFailedError:
            _ok, pid, _host, _app = lock.getLockInfo()
            return (f"Panopticon is already running (pid {pid}).\n\nSwitch to "
                    f"that window, or close it first. A second copy would "
                    f"compete with it for the cameras, the trigger board and "
                    f"the GPU encoder, and could flash the trigger board "
                    f"while it records.")
        print(f"[startup] the single-instance lock {path} could not be "
              f"taken (error {lock.error()}); relying on the process scan",
              flush=True)
    else:
        _INSTANCE_LOCK = lock
    others = _other_panopticons()
    if others is None:
        print("[startup] the process table could not be read; the "
              "single-instance lock is the only check", flush=True)
        return None
    if others:
        listed = "\n".join(f"  pid {pid}  {cmd}" for pid, cmd in others[:5])
        if len(others) > 5:
            listed += f"\n  and {len(others) - 5} more"
        return (f"Panopticon, or one of its probes, is already running:\n\n"
                f"{listed}\n\nClose it first. Two copies compete for the "
                f"cameras, the trigger board and the GPU encoder.")
    return None


def make_splash():
    px = QPixmap(360, 120)
    px.fill(QColor(25, 25, 42))
    p = QPainter(px)
    p.setPen(QColor(220, 220, 220))
    p.setFont(QFont("Segoe UI", 18, QFont.Bold))
    p.drawText(px.rect(), Qt.AlignCenter, "Panopticon")
    p.setPen(QColor(120, 120, 160))
    p.setFont(QFont("Segoe UI", 10))
    p.drawText(px.rect().adjusted(0, 40, 0, 0), Qt.AlignCenter, "Loading cameras...")
    p.end()
    return px


def main():
    # --force starts a second copy anyway, for an operator who has checked
    # the machine by hand. Qt never sees it.
    force = "--force" in sys.argv[1:]
    qt_argv = [a for a in sys.argv if a != "--force"]
    # One grab thread and one encoder thread per camera, plus the UI, all
    # sharing the GIL during a recording. The default 5 ms switch interval
    # lets a GIL-holding thread stall the others for whole milliseconds; 1 ms
    # keeps grab-loop latency bounded.
    sys.setswitchinterval(0.001)
    global _LOG_PATH
    _LOG_PATH = _setup_logging()

    # Without this, Windows groups the taskbar entry under python.exe and shows
    # its icon instead of Panopticon's.
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "salk.talmo.panopticon")
    except Exception:
        pass

    app = QApplication(qt_argv)
    app.setApplicationName("Panopticon Acquisition")
    icon_path = Path(__file__).parent / "panopticon.ico"
    if icon_path.exists():
        from PyQt5.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))

    # Before the splash and before main_window is imported: nothing that
    # opens a camera, the serial port or a benchmark may run in a refused
    # launch.
    refusal = None if force else _single_instance_refusal()
    if refusal:
        print(f"[startup] REFUSING TO START: {refusal}", flush=True)
        QMessageBox.critical(
            None, "Panopticon is already running",
            refusal + "\n\nTo start a second copy anyway, run gui.py --force.")
        sys.exit(EXIT_ALREADY_RUNNING)
    if force:
        print("[startup] --force: the single-instance check was skipped",
              flush=True)

    _install_excepthook()

    splash = QSplashScreen(make_splash())
    splash.show()
    app.processEvents()

    from gui_app.main_window import MainWindow
    window = MainWindow()
    window.show()
    splash.finish(window)

    sys.exit(app.exec_())


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        _report_startup_failure(e)
        sys.exit(1)
