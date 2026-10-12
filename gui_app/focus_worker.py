"""Live focus figures for the Focus window, computed off the UI thread.

A lens out of focus at the board's distance still shows the board in the
decimated preview, and the coverage HUD still counts it, because one marker is
a detection there. Its marker codes blur together, though, and the solve gets
few corners from it. The Focus window shows, per camera, how much of a still
board the camera reads:

- markers decoded, of the board's markers: the main counter. Blur stops the
  code cells decoding long before it widens an edge, so the count is what
  separates a soft lens from a sharp one;
- ChArUco corners, of the board's inner corners: what the solve needs;
- the median marker confidence and a sharpness score, for the last turn of the
  ring once every marker decodes and the counters stop moving. Both depend on
  the board's distance and angle, so each is compared only with itself while
  the board lies still.

Scheduling. A full-resolution detection costs 6-250 ms per camera, depending on
the clutter in view, so a pass over every camera runs at a few Hz at best. A
tick detects the selected camera, the one being focused, and one other camera
in turn (``cameras_for_tick``). The selected camera then updates several times
a second and the others every few seconds.

Frames come from ``CameraManager.latest_full_frames``, which holds a frame only
while ``set_keep_full(True)`` is on; the window that starts this worker turns
it on. A camera without a full frame reports None, never its decimated
preview: a decimated frame hides exactly the blur this window measures.

Lifecycle rules, as for ``CoverageWorker``: ``_running`` is set in ``__init__``
and ``stop()`` clears it and requests interruption, so a stop that races the
start wins. The loop exits only between ticks, so ``stop()`` plus ``wait()`` is
a join bounded by one tick, and the owner keeps the reference until ``wait()``
returns True.
"""
import math
import time
from dataclasses import dataclass

import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from gui_app import charuco

#: OpenCV pool size while the worker runs, as for the coverage HUD.
OPENCV_THREADS = 2
#: Seconds between "[focus] detection error" repeats for a persistent failure.
ERROR_LOG_S = 10.0
#: Seconds between tick-rate log lines.
TICK_LOG_S = 30.0
#: Decoded markers needed before the sharpness score is computed: fewer leave
#: a hull too small to hold a fair sample of the board's edges.
MIN_SHARP_MARKERS = 4
#: Contrast (95th minus 5th percentile, grey levels) under which the hull is
#: too flat to score: a washed-out view would divide by almost nothing.
MIN_CONTRAST = 20.0
#: Widest view of the selected camera the worker sends the window. A smaller
#: frame is sent as it is. The shrink runs here, off the UI thread, so the
#: window only has to draw it.
VIEW_MAX_WIDTH = 960


@dataclass(frozen=True)
class FocusStats:
    """What one camera read of the board in one frame.

    ``confidence`` is the median marker confidence, None when the build has
    no scores or nothing decoded. ``sharpness`` is ``board_sharpness`` of the
    decoded markers' hull, None below ``MIN_SHARP_MARKERS`` markers or
    ``MIN_CONTRAST``. ``roi`` is ``(x0, y0, x1, y1)`` around the decoded
    markers in frame pixels, None when nothing decoded.
    """
    markers: int
    corners: int
    confidence: float | None
    sharpness: float | None
    roi: tuple | None


@dataclass(frozen=True)
class FocusTick:
    """One tick's results: ``results`` maps camera index to its FocusStats, or
    None for a camera with no full frame yet. ``view`` is the frame the
    selected camera's stats were measured on, shrunk to ``VIEW_MAX_WIDTH``
    (a grey uint8 array, the window's to keep), or None; ``view_cam`` is its
    camera and ``view_scale`` its size over the full frame's, which maps a
    FocusStats ``roi`` onto it."""
    results: dict
    view: object
    view_cam: int | None
    view_scale: float
    ticks_per_s: float


def view_of(frame, cv2):
    """``(view, scale)``: ``frame`` shrunk to ``VIEW_MAX_WIDTH`` by area
    averaging, or a copy of it when it is that narrow already."""
    h, w = frame.shape[:2]
    if w <= VIEW_MAX_WIDTH:
        return frame.copy(), 1.0
    scale = VIEW_MAX_WIDTH / w
    size = (VIEW_MAX_WIDTH, max(1, int(round(h * scale))))
    return cv2.resize(frame, size, interpolation=cv2.INTER_AREA), scale


def board_sharpness(gray, points, cv2) -> float | None:
    """RMS of the Laplacian inside the convex hull of ``points``, per unit of
    the hull's contrast, in percent.

    The Laplacian of an edge scales with its contrast, so dividing by the
    contrast (95th minus 5th percentile inside the hull) leaves a figure that
    does not move with exposure or gain, only with how steep the board's
    edges are. Returns None when the hull is under 64 pixels or flatter than
    ``MIN_CONTRAST``.
    """
    pts = np.asarray(points, np.float32).reshape(-1, 2)
    hull = cv2.convexHull(pts).reshape(-1, 2)
    h, w = gray.shape[:2]
    x0 = max(int(math.floor(hull[:, 0].min())), 0)
    y0 = max(int(math.floor(hull[:, 1].min())), 0)
    x1 = min(int(math.ceil(hull[:, 0].max())), w - 1)
    y1 = min(int(math.ceil(hull[:, 1].max())), h - 1)
    if x1 <= x0 or y1 <= y0:
        return None
    roi = gray[y0:y1 + 1, x0:x1 + 1]
    mask = np.zeros(roi.shape, np.uint8)
    cv2.fillConvexPoly(mask, np.round(hull - (x0, y0)).astype(np.int32), 1)
    inside = mask > 0
    if int(inside.sum()) < 64:
        return None
    pix = roi[inside]
    lo, hi = np.percentile(pix, (5, 95))
    contrast = float(hi - lo)
    if contrast < MIN_CONTRAST:
        return None
    lap = cv2.Laplacian(roi, cv2.CV_32F, ksize=3)[inside]
    return float(100.0 * np.sqrt(np.mean(lap * lap)) / contrast)


def board_markers(m_corners, m_ids, conf, board_ids):
    """Keep only the detections whose ID is on the board, each ID once.

    The dictionary holds far more codes than the board prints, and clutter in
    view decodes as one of them now and then, sometimes as an ID the board
    also shows. Counted as they come, a camera reads more markers than the
    board has, and one that sees no board reads 1. Returns ``(corners, ids,
    confidence)`` in the detector's shapes, or ``([], None, None)``.
    """
    if m_ids is None or len(m_ids) == 0:
        return [], None, None
    flat = np.asarray(m_ids).ravel()
    keep, seen = [], set()
    for k, marker_id in enumerate(flat.tolist()):
        if marker_id in board_ids and marker_id not in seen:
            seen.add(marker_id)
            keep.append(k)
    if not keep:
        return [], None, None
    corners = [m_corners[k] for k in keep]
    ids = np.asarray(m_ids).reshape(-1, 1)[keep]
    scores = None
    if conf is not None and len(conf) == len(flat):
        scores = np.asarray(conf, np.float64).ravel()[keep]
    return corners, ids, scores


def focus_stats(gray, detect, board, cv2, board_ids=None) -> FocusStats:
    """FocusStats for one frame. ``detect`` is a
    ``charuco.make_scored_marker_detector`` callable and ``board`` the board
    from ``charuco.make_board``: the same dictionary, layout and legacy flag
    as the solve, and the same corner interpolation it runs. ``board_ids``
    is the set of marker IDs the board prints; detections of any other ID
    are dropped (``board_markers``)."""
    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    if not gray.flags["C_CONTIGUOUS"]:
        gray = np.ascontiguousarray(gray)
    m_corners, m_ids, conf = detect(gray)
    if board_ids is not None:
        m_corners, m_ids, conf = board_markers(m_corners, m_ids, conf,
                                               board_ids)
    n = 0 if m_ids is None else int(len(m_ids))
    if n == 0:
        return FocusStats(0, 0, None, None, None)
    n_corners = 0
    if n >= 2:
        _ret, _cc, c_ids = cv2.aruco.interpolateCornersCharuco(
            m_corners, m_ids, gray, board)
        n_corners = 0 if c_ids is None else int(len(c_ids))
    pts = np.concatenate([c.reshape(-1, 2) for c in m_corners])
    confidence = None
    if conf is not None and len(conf):
        confidence = float(np.median(np.asarray(conf, np.float64)))
    sharpness = (board_sharpness(gray, pts, cv2)
                 if n >= MIN_SHARP_MARKERS else None)
    roi = (int(math.floor(pts[:, 0].min())), int(math.floor(pts[:, 1].min())),
           int(math.ceil(pts[:, 0].max())), int(math.ceil(pts[:, 1].max())))
    return FocusStats(n, n_corners, confidence, sharpness, roi)


def cameras_for_tick(n_cams: int, selected, cursor: int):
    """``(cameras, next_cursor)`` for one tick.

    With a camera selected: that camera and the next other camera after
    ``cursor``. With none: the next two cameras after ``cursor``. A rig with
    one camera detects it every tick.
    """
    if n_cams <= 0:
        return [], 0
    if n_cams == 1:
        return [0], 0
    if selected is not None and not 0 <= selected < n_cams:
        selected = None
    picks = [] if selected is None else [selected]
    want = 2
    c = cursor
    while len(picks) < want:
        c = (c + 1) % n_cams
        if c not in picks:
            picks.append(c)
    return picks, c


class FocusEngine:
    """The board, its scored marker detector and its totals, built once."""

    def __init__(self, board_cfg: dict):
        # Imported here, not at module level, so this module imports on a
        # host without OpenCV and the window can say why Focus is missing.
        import cv2
        self._cv2 = cv2
        self._board, aruco_dict = charuco.make_board(board_cfg)
        self._detect = charuco.make_scored_marker_detector(aruco_dict)
        ids = (self._board.getIds() if hasattr(self._board, "getIds")
               else self._board.ids)
        #: The marker IDs the board prints.
        self.board_ids = frozenset(int(i) for i in np.asarray(ids).ravel())
        #: Markers and inner corners (the ChArUco corners) of the board the
        #: profile's board config describes, counted from the board object
        #: the solve also builds, so any board size and layout gives its own
        #: totals.
        self.total_markers = len(self.board_ids)
        self.total_corners = int(len(self._chessboard_corners()))

    def _chessboard_corners(self):
        board = self._board
        if hasattr(board, "getChessboardCorners"):
            return np.asarray(board.getChessboardCorners()).reshape(-1, 3)
        return np.asarray(board.chessboardCorners).reshape(-1, 3)

    @property
    def cv2(self):
        return self._cv2

    def stats(self, gray) -> FocusStats:
        return focus_stats(gray, self._detect, self._board, self._cv2,
                           self.board_ids)


class FocusWorker(QThread):
    updated = pyqtSignal(object)  # a FocusTick

    def __init__(self, engine, camera_mgr, n_cams: int,
                 interval_ms: int = 50, parent=None,
                 opencv_threads: int = OPENCV_THREADS):
        super().__init__(parent)
        self._engine = engine
        self._camera_mgr = camera_mgr
        self._n = int(n_cams)
        self._interval = interval_ms / 1000.0
        self._opencv_threads = int(opencv_threads)
        # Set here, not in run(): a stop() that races the thread start must win.
        self._running = True
        self._selected = None
        self._cursor = -1
        self.ticks = 0
        self._err_last = -float("inf")
        self._err_suppressed = 0

    def set_selected(self, cam):
        """Give ``cam`` (an index, or None) every tick. One assignment, read
        once per tick by the worker."""
        self._selected = cam

    def _log_error(self, e):
        now = time.monotonic()
        if now - self._err_last >= ERROR_LOG_S:
            more = (" ({} repeats suppressed)".format(self._err_suppressed)
                    if self._err_suppressed else "")
            print("[focus] detection error: {}{}".format(e, more), flush=True)
            self._err_last = now
            self._err_suppressed = 0
        else:
            self._err_suppressed += 1

    def _tick(self, rate: float) -> FocusTick:
        frames = self._camera_mgr.latest_full_frames
        selected = self._selected
        cams, self._cursor = cameras_for_tick(self._n, selected, self._cursor)
        results, view, scale = {}, None, 1.0
        for cam in cams:
            frame = frames[cam] if cam < len(frames) else None
            if frame is None:
                results[cam] = None
                continue
            results[cam] = self._engine.stats(frame)
            if cam == selected:
                view, scale = view_of(frame, self._engine.cv2)
        return FocusTick(results, view, selected if view is not None else None,
                         scale, rate)

    def run(self):
        prev_threads = None
        try:
            import cv2
            prev_threads = cv2.getNumThreads()
            cv2.setNumThreads(self._opencv_threads)
        except Exception:
            prev_threads = None
        t_log = time.perf_counter()
        n_log = 0
        rate = 0.0
        try:
            while self._running and not self.isInterruptionRequested():
                t0 = time.perf_counter()
                try:
                    tick = self._tick(rate)
                except Exception as e:
                    self._log_error(e)
                    tick = None
                self.ticks += 1
                now = time.perf_counter()
                if now - t_log >= 2.0:
                    rate = (self.ticks - n_log) / (now - t_log)
                if now - t_log >= TICK_LOG_S:
                    print("[focus] detection ticks/s: {:.1f}".format(rate),
                          flush=True)
                    t_log, n_log = now, self.ticks
                if tick is not None:
                    self.updated.emit(tick)
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
