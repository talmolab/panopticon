"""Tiny helper to run a blocking callable off the Qt main thread.

Used for the operations that otherwise freeze the UI (the window goes "not
responding"): camera open/close/reconfigure, which make many synchronous GigE
round-trips; the whole acquisition start and its rollback, which add the
serial claim, the readiness barrier and, on a failure, the full stop budget;
the finalize and the stimulus-trace rebuild; the snapshot save; and the ~30 s
arduino-cli firmware flash in main_window's ``_ensure_clean_firmware`` /
``_ensure_sketch_for``.

One worker per attribute at a time: assigning a new worker over a running one
drops the last reference to a live QThread, which Qt answers with qFatal
rather than an exception, so every assignment checks isRunning() first.

The callable runs in this QThread; its return value (or the raised exception) is
delivered back on the main thread via the ``done`` signal. Callers MUST check
whether the result is an Exception — see main_window._on_acquisition_finalized
for what silently ignoring it cost.
"""
import traceback
from PyQt5.QtCore import QThread, pyqtSignal


class CallableWorker(QThread):
    done = pyqtSignal(object)  # the callable's return value, or the Exception

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            result = self._fn()
        except Exception as e:  # surface, don't crash the worker
            traceback.print_exc()
            result = e
        self.done.emit(result)
