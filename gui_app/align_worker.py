"""Background worker: block-ID align a finished recording (realtime path).

Runs after EncodeWorker. When cameras dropped different frames it re-encodes
each mp4 down to the frames every camera captured and replaces the original
(the user chose replace-in-place), so the per-camera videos end up equal-length
and trigger-aligned. A loss-free recording is a no-op fast path.
"""
import threading
from pathlib import Path
from PyQt5.QtCore import QThread, pyqtSignal

from gui_app import alignment


class AlignWorker(QThread):
    progress = pyqtSignal(int, int, str)   # done, total, message
    finished_align = pyqtSignal(dict)      # summary from align_recording

    def __init__(self, video_dir: Path, fps: int, quality: int,
                 parallel: int = 3, backend: str | None = None):
        super().__init__()
        self._video_dir = video_dir
        self._fps = fps
        self._quality = quality
        self._parallel = parallel
        self._backend = backend
        self._stop = threading.Event()

    def request_stop(self) -> None:
        """Ask the worker to stop launching and to abort the encode in flight.

        Checked before every ffmpeg launch and once per decoded frame, so a
        quit does not race a fresh ffmpeg against the cleanup that follows.
        Every camera not completed is reported as a failure with its original
        video kept.
        """
        self._stop.set()

    def run(self):
        try:
            summary = alignment.align_recording(
                self._video_dir, fps=self._fps, quality=self._quality,
                replace=True, parallel=self._parallel,
                progress=lambda d, t, m: self.progress.emit(d, t, m),
                backend=self._backend, should_stop=self._stop.is_set)
        except Exception as e:
            print(f"[align] failed: {e}", flush=True)
            summary = dict(error=str(e), needed=False, replaced=False,
                           common_frames=0, warnings=[str(e)],
                           rate_warnings=[], failures=[str(e)],
                           replaced_cams=[], failed_cams=[], index_error=None,
                           stopped=self._stop.is_set())
        self.finished_align.emit(summary)
