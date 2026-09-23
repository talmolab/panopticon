"""Background worker: block-ID align a finished recording (post-hoc mode).

Runs after EncodeWorker. When cameras dropped different frames it re-encodes
each mp4 down to the frames every camera captured and replaces the original
(the user chose replace-in-place), so the per-camera videos end up equal-length
and trigger-aligned. A loss-free recording is a no-op fast path.

The replace is refused, and only the index written, while a camera that ended
early, started late or stopped mid-recording takes part
(``alignment.refusal_reason``): replacing would cut every other camera to that
camera's frames. ``exclude`` leaves cameras out and ``truncate_to_shortest``
accepts the cut; the summary's ``refused`` and ``short_cams`` say what
happened.
"""
import threading
from pathlib import Path
from PyQt5.QtCore import QThread, pyqtSignal

from gui_app import alignment


class AlignWorker(QThread):
    progress = pyqtSignal(int, int, str)   # done, total, message
    finished_align = pyqtSignal(dict)      # summary from align_recording

    def __init__(self, video_dir: Path, fps: int, quality: int,
                 parallel: int = 3, backend: str | None = None,
                 exclude=(), truncate_to_shortest: bool = False):
        super().__init__()
        self._video_dir = video_dir
        self._fps = fps
        self._quality = quality
        self._parallel = parallel
        self._backend = backend
        self._exclude = exclude
        self._truncate = truncate_to_shortest
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
                backend=self._backend, should_stop=self._stop.is_set,
                exclude=self._exclude,
                truncate_to_shortest=self._truncate)
        except Exception as e:
            print(f"[align] failed: {e}", flush=True)
            summary = dict(error=str(e), needed=False, replaced=False,
                           common_frames=0, warnings=[str(e)],
                           rate_warnings=[], rate_checked=[], rate_skipped={},
                           failures=[str(e)], replaced_cams=[], failed_cams=[],
                           index_error=None, refused=None, short_cams={},
                           excluded={}, stopped=self._stop.is_set())
        self.finished_align.emit(summary)
