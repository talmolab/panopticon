"""Panopticon Acquisition GUI — launch with: conda run -n 3dpose python gui.py"""
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

    app = QApplication(sys.argv)
    app.setApplicationName("Panopticon Acquisition")
    icon_path = Path(__file__).parent / "panopticon.ico"
    if icon_path.exists():
        from PyQt5.QtGui import QIcon
        app.setWindowIcon(QIcon(str(icon_path)))

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
