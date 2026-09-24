"""Panopticon Acquisition GUI — launch with: conda run -n 3dpose python gui.py"""
import os
import sys
import threading
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


def _setup_logging():
    """Send stdout and stderr to a timestamped log file under logs/, and to
    the console when there is one (pythonw has none).

    gui_app.logging_setup stamps every line with its time and thread and
    writes it from one background thread, so a print never waits for the
    disk or the console. Returns the log path, or None if logging couldn't be
    set up."""
    try:
        from gui_app import logging_setup
        log_path = logging_setup.install(
            LOG_DIR / f"panopticon_{datetime.now():%Y%m%d_%H%M%S}.log")
        if log_path is None:
            return None
        print(f"[startup] logging to {log_path}", flush=True)
        return log_path
    except Exception:
        return None


def _flush_log(timeout_s: float = 1.0) -> None:
    """Let the log writer catch up, on the Qt main thread only.

    RULE: only the main thread waits. REASON: sys.excepthook also runs on
    the thread whose Python code raised, a grab thread included, and no
    thread that holds a camera may wait for the disk; the main thread's
    wait is what puts a traceback in the file before a crash that may
    follow it.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        from gui_app import logging_setup
        logging_setup.flush(timeout_s)
    except Exception:
        pass


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


def _other_panopticons():
    """(pid, command line) of other Panopticon processes, or None if unknown.

    RULE: probe_guard.other_panopticons decides, and this module keeps no
    matching rule of its own. REASON: the launch and the probes must count
    the same processes, and a second copy of the rule falls behind the
    first. That function counts a GUI or probe however it was started, and a
    capture worker whose parent is one of them or has exited (it still holds
    its cameras and its NVENC sessions). It never counts this process or its
    ancestors (uv, the virtual environment's launcher), and it answers None
    when the process table, this process's own command line included,
    cannot be read.
    """
    try:
        from gui_app import probe_guard
        return probe_guard.other_panopticons()
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

    code = app.exec_()
    _shutdown_log()
    sys.exit(code)


def _shutdown_log() -> None:
    """Write out what the log writer still holds and stop it."""
    try:
        from gui_app import logging_setup
        logging_setup.shutdown()
    except Exception:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        traceback.print_exc()
        _flush_log()
        _report_startup_failure(e)
        _shutdown_log()
        sys.exit(1)
