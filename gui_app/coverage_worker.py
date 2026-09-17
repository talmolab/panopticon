"""Background worker that runs ChArUco coverage detection off the UI thread.

The detector is fed one frame per camera per tick so the coverage graph fills
while data is recorded, without blocking the Qt UI thread: several charuco
detections per tick would otherwise blow the display-refresh budget and stutter
the live preview.

The tick rate is BEST EFFORT, not a fixed 30 Hz. Detection runs sequentially
over the cameras and costs 6-250 ms per camera depending on scene clutter, so
nine cameras land at typically 10-20 Hz and ~1 Hz when several cameras see a
cluttered scene. ``interval_ms`` is only a floor on the tick period. The
measured rate is published as ``ticks_per_s`` (also on the detector, which is
what the HUD widget reads) and logged every ``TICK_LOG_S`` seconds so a rig run
records it.

Full-resolution frames are preferred (that is what resolves the board for the
oblique cameras, and it matches what the post-hoc solve sees); the downsampled
preview is only a stand-in for a camera whose first full frame has not arrived.

OpenCV thread pool. Each ``detectBoard`` call fans out over OpenCV's global
pool, which defaults to every hardware thread on the host, while the grab
threads are pinned and budgeted per frame. ``run()`` therefore caps the pool
to ``opencv_threads`` (default ``OPENCV_THREADS``) for the life of the worker
and restores the previous value on exit. The cap is process-wide, so it also
applies to any OpenCV call the UI thread makes while the HUD runs. The default
is conservative and matches the offline solve's per-worker cap; the rig A/B
(capture loss and ticks/s with 2 versus the default pool) decides the final
value, and the constructor argument is the hook for that experiment.

Lifecycle rules:
- ``_running`` is set in ``__init__`` and ``stop()`` both clears it and calls
  ``requestInterruption()``, so a ``stop()`` that lands before ``run()`` has
  begun is not overwritten and the loop exits on its first check.
- The loop exits only between ticks, so ``stop()`` plus an unbounded
  ``wait()`` is a real join bounded by ONE detection pass. The owner must
  check ``wait(timeout)``'s return value and keep the reference until the
  thread has finished; dropping the last reference to a running QThread is a
  qFatal, not an exception.
- A persistent detector exception is printed once, then at most every
  ``ERROR_LOG_S`` seconds, so a broken detector does not flood the log at tick
  rate.
"""
import time

from PyQt5.QtCore import QThread, pyqtSignal

#: Default OpenCV pool size while the HUD runs. Conservative on purpose; the
#: rig A/B against the default pool decides whether it stays.
OPENCV_THREADS = 2
#: Seconds between "[hud] detection error" repeats for a persistent failure.
ERROR_LOG_S = 10.0
#: Seconds between tick-rate log lines.
TICK_LOG_S = 30.0


class CoverageWorker(QThread):
    updated = pyqtSignal()  # detector state advanced; UI should repaint the graph

    def __init__(self, detector, camera_mgr, interval_ms: int = 33, parent=None,
                 opencv_threads: int = OPENCV_THREADS):
        super().__init__(parent)
        self._detector = detector
        self._camera_mgr = camera_mgr
        self._interval = interval_ms / 1000.0
        self._opencv_threads = int(opencv_threads)
        # Set here, not in run(): a stop() that races the thread start must win.
        self._running = True
        self.ticks_per_s = 0.0
        self.ticks = 0
        self._err_last = -float("inf")
        self._err_suppressed = 0

    def _log_error(self, e):
        now = time.monotonic()
        if now - self._err_last >= ERROR_LOG_S:
            more = (" ({} repeats suppressed)".format(self._err_suppressed)
                    if self._err_suppressed else "")
            print("[hud] detection error: {}{}".format(e, more), flush=True)
            self._err_last = now
            self._err_suppressed = 0
        else:
            self._err_suppressed += 1

    def _set_opencv_threads(self):
        """Cap the process-wide OpenCV pool; return the previous size or None."""
        if self._opencv_threads <= 0:
            return None
        try:
            import cv2
        except ImportError:
            return None
        try:
            prev = cv2.getNumThreads()
            cv2.setNumThreads(self._opencv_threads)
            print("[hud] OpenCV threads {} -> {} while the coverage HUD runs".format(
                prev, self._opencv_threads), flush=True)
            return prev
        except Exception as e:
            print("[hud] could not set OpenCV threads: {}".format(e), flush=True)
            return None

    def run(self):
        prev_threads = self._set_opencv_threads()
        t_start = time.perf_counter()
        t_log = t_start
        n_log = 0
        try:
            while self._running and not self.isInterruptionRequested():
                t0 = time.perf_counter()
                # Prefer full-res frames (resolves oblique cams); fall back to the
                # downsampled preview per-camera until the first full frame lands.
                full = self._camera_mgr.latest_full_frames
                preview = self._camera_mgr.latest_frames
                frames = [f if f is not None else (preview[i] if i < len(preview) else None)
                          for i, f in enumerate(full)]
                fc = self._camera_mgr.frame_counts
                try:
                    self._detector.update(frames, frame_counts=fc)
                except Exception as e:
                    self._log_error(e)
                self.ticks += 1
                now = time.perf_counter()
                self.ticks_per_s = getattr(self._detector, "ticks_per_s",
                                           self.ticks / max(now - t_start, 1e-9))
                if now - t_log >= TICK_LOG_S:
                    rate = (self.ticks - n_log) / (now - t_log)
                    print("[hud] coverage ticks/s: {:.1f}".format(rate), flush=True)
                    t_log, n_log = now, self.ticks
                self.updated.emit()
                remaining = self._interval - (now - t0)
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            if prev_threads is not None:
                try:
                    import cv2
                    cv2.setNumThreads(prev_threads)
                except Exception:
                    pass

    def stop(self):
        """Ask the loop to exit after the current tick. Safe before start()."""
        self._running = False
        self.requestInterruption()
