"""Live ChArUco coverage detector for the calibration HUD.

Runs lightweight ChArUco detection on whatever frames ``coverage_worker`` hands
it — full-resolution grayscale while the grab threads are keeping full frames
(the normal case, because oblique cameras resolve badly at preview size), and
the downsampled preview for a given camera until its first full frame lands.
Tracks:

  - ``glow``          : per-camera decaying pulse, set to 1.0 on each detection
  - ``shared``        : pairwise co-detection counts (board seen by both cams in
                        the same detection tick)
  - ``per_cam_frames``: per-camera co-detection ticks (ticks where this cam
                        detected the board AND at least one other cam did too)
  - ``grid_cells_hit``: how many of the 2x2 FOV cells that camera has seen the
                        board in, binned by the marker centroid
  - ``connectivity``  : the algebraic connectivity (Fiedler value) of the
                        co-visibility graph, each pair weighted by its
                        co-detections once it reaches ``min_edge``. 0 while the
                        graph is split, small while a weak bridge holds it
                        together. ``weak_cut`` is the two camera groups that
                        bridge joins, the smaller first.
  - view quality    : per camera, over its co-detection ticks (the frames the
                        hinted solve decodes): ``fill_cells_hit``, the cells of
                        a FILL_GRID x FILL_GRID image grid a marker corner has
                        reached; ``close_views``, the ticks with the board at
                        ``MIN_BOARD_SIZE`` or more of the view; and
                        ``tilted_views``, the ticks with it tilted
                        ``MIN_TILT_DEG`` or more (``view_shape``).
  - ``ready``         : ALL of: every camera has >= ``min_per_cam_shared``
                        co-detection ticks; every camera has hit
                        >= ``MIN_GRID_CELLS`` of its 4 grid cells; the
                        co-visibility graph is ONE CONNECTED COMPONENT over the
                        pairs with >= ``min_edge`` co-detections, with
                        ``connectivity`` >= ``MIN_CONNECTIVITY``; and every
                        camera meets the view-quality thresholds that are set
                        (each is off at 0). The graph terms are connectivity
                        tests, not per-pair tests: the board is one-sided, so
                        opposed cameras can never co-detect and "every pair
                        connected" could never fill.
                        ``ready`` LATCHES: detection, glow decay, counting and
                        ``codet_frames`` all keep running afterwards, because
                        the hinted solve decodes only the frames listed in
                        ``codet_frames`` and every co-detection recorded while
                        the operator keeps waving is more data for it.
  - ``codet_frames``  : per tick, ``{cam_index: block_id}`` for the cameras
                        that co-detected. The block ID is the one the grab
                        thread published together with the frame the HUD
                        detected on (``latest_full_frames_with_bids``), i.e.
                        that frame's unwrapped trigger ordinal. The solve maps
                        it to a video index through the camera's own
                        ``blockids.npy``, so a drop, a kicked trigger or an
                        alignment that changes one camera's video cannot shift
                        that camera's hints against the others. A camera
                        without an ID for its frame (a preview frame, or no
                        recording) contributes coverage but no hint.
  - ``ticks_per_s``   : measured detection tick rate (EMA), for the HUD and
                        the rig log. Detection is sequential over cameras and
                        costs 6-250 ms per camera depending on scene clutter,
                        so the rate is best effort: a few Hz for nine
                        full-resolution cameras, ~1 Hz when several see
                        clutter.

Counts are at the detection tick rate, not the recorded-frame rate, so the
thresholds are relative coverage signals rather than frame totals: each
threshold buys MORE wall time (and more recorded frames) when the tick rate
falls, which is the safe direction. Tune them on the rig.

Works across both the pre-4.7 and >=4.7 OpenCV ArUco APIs. ``cv2`` and
``yaml`` are imported inside the constructors, not at module level, so this
module imports with numpy alone; the tests drive the counting logic with a stub
engine and the GUI self-disables the HUD when OpenCV is missing.
"""
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from gui_app import charuco


class _CharucoEngine:
    """Returns a visible-marker count for a grayscale frame, across cv2 versions."""

    def __init__(self, board_cfg: dict):
        # Imported here, not at module level: the module must import with
        # numpy alone (tests, hosts without OpenCV).
        import cv2
        self._cv2 = cv2
        # Board and marker detector both come from the shared helper, so the
        # HUD and the solve can never disagree about dictionary, layout, legacy
        # policy or which ArUco API generation is in use; the helper raises on
        # an unknown dictionary or an unexpressible legacy flag. The HUD counts
        # markers only (no charuco corner interpolation), because the count
        # and centroid are all the coverage logic reads.
        self._board, self._dict = charuco.make_board(board_cfg)
        self._detect_markers = charuco.make_marker_detector(self._dict)
        self._marker_fraction = marker_area_fraction(board_cfg)

    def detect(self, gray):
        """Return (marker_count, centroid_xy_normalized, view) for a frame.

        centroid_xy is the mean of all detected marker corners, normalized to
        [0, 1] by the frame dimensions, and view is ``view_shape``'s
        ``(cells, size, tilt_deg)``. Returns (0, None, None) when nothing is
        found.
        """
        cv2 = self._cv2
        if gray is None:
            return 0, None, None
        if gray.ndim == 3:
            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        if not gray.flags["C_CONTIGUOUS"]:
            gray = np.ascontiguousarray(gray)
        h, w = gray.shape[:2]
        try:
            m_corners, m_ids = self._detect_markers(gray)
            if m_ids is None or len(m_ids) == 0:
                return 0, None, None
            n = int(len(m_ids))
            quads = np.concatenate([c.reshape(-1, 4, 2) for c in m_corners])
            all_pts = quads.reshape(-1, 2)
            cx = float(all_pts[:, 0].mean()) / max(w, 1)
            cy = float(all_pts[:, 1].mean()) / max(h, 1)
            return n, (cx, cy), view_shape(quads, w, h, self._marker_fraction)
        except cv2.error:
            return 0, None, None


#: The corners of a unit square in ArUco's corner order (top-left, top-right,
#: bottom-right, bottom-left, image y down), centred and scaled so its
#: columns are orthonormal: a marker's corners P (4 x 2, centred) then map
#: through ``P.T @ _UNIT_SQUARE``, the least-squares affine map of the square.
_UNIT_SQUARE = 0.5 * np.array([[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0],
                               [-1.0, 1.0]])


def marker_area_fraction(board_cfg: dict) -> float:
    """The share of the board's area its ArUco markers cover: one marker in
    every other square, each ``marker_length`` on a ``square_length`` side."""
    bx, by = int(board_cfg["board_x"]), int(board_cfg["board_y"])
    sq = float(board_cfg["square_length"])
    mk = float(board_cfg["marker_length"])
    if bx <= 0 or by <= 0 or sq <= 0:
        return 0.0
    return (bx * by // 2) * mk * mk / (bx * by * sq * sq)


def view_shape(marker_quads, width, height, marker_fraction, grid=None):
    """How one view shows the board, from its detected markers:
    ``(cells, size, tilt_deg)``.

    ``marker_quads`` is (M, 4, 2) pixel corners in ArUco's order.

    - ``cells``: a bit mask of the ``grid`` x ``grid`` image cells holding a
      marker corner (bit ``row * grid + col``). A lens fit needs corners out
      to the edges of the view, where the distortion is.
    - ``size``: the square root of the visible board's share of the image
      area, from the markers' own area over ``marker_fraction``. 1 is a board
      filling the view. A board that stays small leaves the focal length
      poorly determined.
    - ``tilt_deg``: the board's angle to the image plane, from the markers'
      mean affine shape: a square tilted by t images as a parallelogram whose
      axes differ by cos t. A lens fit needs tilted views as well as
      square-on ones.

    Size and tilt follow the sample measures of ROS camera_calibration
    (``Calibrator.get_parameters``: x, y, size, skew), which judges a lens
    calibration by the spread of the views it holds. Numpy only: it runs on
    the HUD's thread for every detection.
    """
    grid = BoardDetector.FILL_GRID if grid is None else grid
    pts = np.asarray(marker_quads, dtype=np.float64).reshape(-1, 4, 2)
    if len(pts) == 0 or width <= 0 or height <= 0:
        return 0, 0.0, 0.0
    flat = pts.reshape(-1, 2)
    ix = np.clip((flat[:, 0] * (grid / width)).astype(int), 0, grid - 1)
    iy = np.clip((flat[:, 1] * (grid / height)).astype(int), 0, grid - 1)
    cells = 0
    for b in np.unique(iy * grid + ix):
        cells |= 1 << int(b)
    x, y = pts[..., 0], pts[..., 1]
    quad_area = 0.5 * np.abs((x * np.roll(y, -1, axis=1)
                              - np.roll(x, -1, axis=1) * y).sum(axis=1))
    size = 0.0
    if marker_fraction > 0:
        size = float(min(1.0, math.sqrt(
            quad_area.sum() / marker_fraction / (width * height))))
    centred = pts - pts.mean(axis=1, keepdims=True)
    a = np.einsum("mki,kj->ij", centred, _UNIT_SQUARE) / len(pts)
    ss = float((a * a).sum())
    det = float(a[0, 0] * a[1, 1] - a[0, 1] * a[1, 0])
    root = math.sqrt(max(0.0, ss * ss - 4.0 * det * det))
    s1_sq, s2_sq = (ss + root) / 2.0, (ss - root) / 2.0
    tilt = 0.0
    if s1_sq > 0:
        ratio = math.sqrt(max(0.0, s2_sq) / s1_sq)
        tilt = math.degrees(math.acos(min(1.0, ratio)))
    return cells, size, tilt


def spectral_cut(shared, min_edge):
    """``(connectivity, weak, rest)`` of a co-detection matrix.

    ``connectivity`` is the second-smallest eigenvalue of the graph
    Laplacian, each pair weighted by its co-detections once it reaches
    ``min_edge``: 0 while the graph is split, and roughly
    ``c * n / (a * b)`` for two well-joined groups of ``a`` and ``b``
    cameras bridged by ``c`` co-detections. ``weak`` and ``rest`` are the two
    sides of the cut that bridge makes, found by sweeping the Fiedler vector
    for the smallest ratio cut, ``weak`` being the smaller side. Monotone:
    weights only grow and edges only appear, so it never falls.
    """
    w = np.asarray(shared, dtype=float)
    n = len(w)
    if n < 2:
        return 0.0, [], list(range(n))
    w = np.where(w >= min_edge, w, 0.0)
    np.fill_diagonal(w, 0.0)
    lap = np.diag(w.sum(axis=1)) - w
    vals, vecs = np.linalg.eigh(lap)
    order = np.argsort(vecs[:, 1], kind="stable")
    best_k, best_ratio = 1, None
    for k in range(1, n):
        cut = float(w[np.ix_(order[:k], order[k:])].sum())
        ratio = cut / (k * (n - k))
        if best_ratio is None or ratio < best_ratio:
            best_k, best_ratio = k, ratio
    a = sorted(int(i) for i in order[:best_k])
    b = sorted(int(i) for i in order[best_k:])
    weak, rest = (a, b) if (len(a), a) <= (len(b), b) else (b, a)
    return max(0.0, float(vals[1])), weak, rest


class BoardDetector:
    GRID_ROWS = 2
    GRID_COLS = 2
    MIN_GRID_CELLS = 3  # out of 4
    #: The fill test's image grid, FILL_GRID x FILL_GRID cells.
    FILL_GRID = 4
    # The graph and view-quality thresholds. Each is off at 0; a rig profile
    # sets them (calibration_min_connectivity and the rest).
    MIN_CONNECTIVITY = 0.0
    MIN_FILL_CELLS = 0
    MIN_BOARD_SIZE = 0.0
    MIN_TILT_DEG = 0.0
    #: Co-detection ticks a camera needs at MIN_BOARD_SIZE, and again at
    #: MIN_TILT_DEG, so one stray detection cannot pass either test.
    QUALITY_VIEWS = 5

    def __init__(self, n_cams, board_config_path,
                 glow_threshold=4, edge_threshold=5,
                 optimal_shared=200, min_edge=40, min_per_cam_shared=120,
                 glow_decay_s=0.4, min_grid_cells=None, min_connectivity=None,
                 min_fill_cells=None, min_board_size=None, min_tilt_deg=None):
        self.n = int(n_cams)
        self.glow_threshold = glow_threshold
        self.edge_threshold = edge_threshold
        self.optimal_shared = optimal_shared
        self.min_edge = min_edge
        self.min_per_cam_shared = min_per_cam_shared
        self.glow_decay_s = glow_decay_s
        # Instance attribute shadows the class default so a rig profile can
        # raise or lower it without editing code.
        if min_grid_cells is not None:
            self.MIN_GRID_CELLS = int(min_grid_cells)
        if min_connectivity is not None:
            self.MIN_CONNECTIVITY = float(min_connectivity)
        if min_fill_cells is not None:
            self.MIN_FILL_CELLS = int(min_fill_cells)
        if min_board_size is not None:
            self.MIN_BOARD_SIZE = float(min_board_size)
        if min_tilt_deg is not None:
            self.MIN_TILT_DEG = float(min_tilt_deg)
        # The same loader as the solve: a config lacking a key raises a
        # ValueError naming it, which the GUI prints as "coverage detector
        # unavailable: ...".
        self._engine = _CharucoEngine(
            charuco.load_board_config(board_config_path))
        self.reset()

    def reset(self):
        n = self.n
        self.glow = np.zeros(n)
        self.shared = np.zeros((n, n), dtype=int)
        self.per_cam_frames = np.zeros(n, dtype=int)  # ticks: what READY uses
        #: Connected components of the co-visibility graph, refreshed each tick.
        #: One component is the READY condition; more than one means the solve
        #: will keep the largest and silently drop the rest.
        self.components: list[list[int]] = [[i] for i in range(n)]
        self.grid_covered = np.zeros((n, self.GRID_ROWS, self.GRID_COLS), dtype=bool)
        self.grid_cells_hit = np.zeros(n, dtype=int)
        #: Algebraic connectivity and the cut it measures (spectral_cut).
        self.connectivity = 0.0
        self.weak_cut: tuple[list[int], list[int]] = ([], list(range(n)))
        #: View quality, counted on co-detection ticks only: the hinted solve
        #: decodes no other frame, so a view no other camera shared never
        #: reaches the lens fit.
        self.fill_mask = [0] * n
        self.fill_cells_hit = np.zeros(n, dtype=int)
        self.close_views = np.zeros(n, dtype=int)
        self.tilted_views = np.zeros(n, dtype=int)
        self.best_size = np.zeros(n)
        self.best_tilt_deg = np.zeros(n)
        self.ready = False
        self._last = time.perf_counter()
        self.codet_frames = []
        #: Measured detection tick rate, exponentially smoothed. 0 until the
        #: second tick. Published so the HUD can show what "a tick" is worth.
        self.ticks_per_s = 0.0

    def _centroid_to_cell(self, cx, cy):
        r = min(int(cy * self.GRID_ROWS), self.GRID_ROWS - 1)
        c = min(int(cx * self.GRID_COLS), self.GRID_COLS - 1)
        return max(0, r), max(0, c)

    def update(self, frames, block_ids=None):
        """Run one detection tick over one frame per camera.

        ``block_ids[i]`` is the trigger ordinal of ``frames[i]`` (the pair
        ``latest_full_frames_with_bids`` published), or None for a frame that
        has none. The co-detecting cameras' IDs are appended to
        ``codet_frames`` for the solve to decode instead of re-scanning every
        frame. A camera whose ID is None still counts towards coverage.

        Runs after READY too: ``ready`` only latches. Stopping would freeze the
        glow and, worse, stop recording hints while the operator is still
        waving the board, and the hinted solve never sees an unlisted frame.
        """
        now = time.perf_counter()
        dt = max(0.0, now - self._last)
        self._last = now
        self.glow *= math.exp(-dt / self.glow_decay_s)
        if dt > 0:
            inst = 1.0 / dt
            self.ticks_per_s = (inst if self.ticks_per_s <= 0
                                else 0.9 * self.ticks_per_s + 0.1 * inst)

        seen, views = [], {}
        for i in range(self.n):
            # A frames list shorter than n (a camera whose first frame has not
            # arrived) reads as "no frame" for the missing cameras, never an
            # IndexError that would kill the worker.
            fr = frames[i] if frames is not None and i < len(frames) else None
            found = self._engine.detect(fr)
            nc, centroid = found[0], found[1]
            if nc >= self.glow_threshold:
                self.glow[i] = 1.0
            if nc >= self.edge_threshold:
                seen.append(i)
                if len(found) > 2 and found[2] is not None:
                    views[i] = found[2]
                if centroid is not None:
                    r, c = self._centroid_to_cell(*centroid)
                    if not self.grid_covered[i, r, c]:
                        self.grid_covered[i, r, c] = True
                        self.grid_cells_hit[i] = int(self.grid_covered[i].sum())

        if len(seen) >= 2:
            for i in seen:
                # RULE: count one per tick, never one per partner. REASON: the
                # solve consumes frames, and READY thresholds this count
                # against min_per_cam_shared. Weighting by partner count would
                # meet the bar N-1 times sooner with N cameras in view and pass
                # a calibration on a fraction of the frames it needs. How the
                # partners connect is the graph test in _update_ready().
                self.per_cam_frames[i] += 1
                if i in views:
                    self._count_view(i, *views[i])
            for a in range(len(seen)):
                for b in range(a + 1, len(seen)):
                    self.shared[seen[a], seen[b]] += 1
                    self.shared[seen[b], seen[a]] += 1
            if block_ids is not None:
                tick = {i: int(block_ids[i]) for i in seen
                        if i < len(block_ids) and block_ids[i] is not None}
                if tick:
                    self.codet_frames.append(tick)

        self._update_ready()
        return self

    def _count_view(self, i, cells, size, tilt_deg):
        """Add one co-detection tick's view of camera ``i`` to its quality."""
        self.fill_mask[i] |= int(cells)
        self.fill_cells_hit[i] = bin(self.fill_mask[i]).count("1")
        self.best_size[i] = max(self.best_size[i], size)
        self.best_tilt_deg[i] = max(self.best_tilt_deg[i], tilt_deg)
        if self.MIN_BOARD_SIZE > 0 and size >= self.MIN_BOARD_SIZE:
            self.close_views[i] += 1
        if self.MIN_TILT_DEG > 0 and tilt_deg >= self.MIN_TILT_DEG:
            self.tilted_views[i] += 1

    def view_needs(self) -> dict:
        """The cameras short of each view-quality threshold that is set, as
        ``{"closer": [...], "tilted": [...], "edges": [...]}`` of camera
        indices; a threshold at 0 lists nobody."""
        idx = range(self.n)
        return {
            "closer": [i for i in idx if self.MIN_BOARD_SIZE > 0
                       and self.close_views[i] < self.QUALITY_VIEWS],
            "tilted": [i for i in idx if self.MIN_TILT_DEG > 0
                       and self.tilted_views[i] < self.QUALITY_VIEWS],
            "edges": [i for i in idx if self.MIN_FILL_CELLS > 0
                      and self.fill_cells_hit[i] < self.MIN_FILL_CELLS],
        }

    def view_quality_on(self) -> bool:
        """Whether any view-quality threshold is set."""
        return (self.MIN_BOARD_SIZE > 0 or self.MIN_TILT_DEG > 0
                or self.MIN_FILL_CELLS > 0)

    def summary(self) -> str:
        """One line per camera plus the graph, for the log at the end of a
        calibration: what READY measured, to set its thresholds from."""
        weak, rest = self.weak_cut
        lines = ["connectivity {:.1f} (min {:g}); weakest cut {} | {}".format(
            self.connectivity, self.MIN_CONNECTIVITY,
            [i + 1 for i in weak], [i + 1 for i in rest])]
        for i in range(self.n):
            lines.append(
                "cam{}: paired {} grid {} fill {}/{} best size {:.2f} "
                "tilt {:.0f} deg; close {} tilted {}".format(
                    i + 1, int(self.per_cam_frames[i]),
                    int(self.grid_cells_hit[i]), int(self.fill_cells_hit[i]),
                    self.FILL_GRID ** 2, float(self.best_size[i]),
                    float(self.best_tilt_deg[i]), int(self.close_views[i]),
                    int(self.tilted_views[i])))
        return "\n".join(lines)

    def _components(self):
        """Connected components of the co-visibility graph, as camera indices.

        Computed every tick rather than only at READY, because it is the
        condition operators cannot see any other way: per-camera counts and grid
        coverage can all be satisfied while the graph sits in several clusters
        that never observed the board together. A nine-camera calibration can reach
        `paired 260/250 grid 4/3` on every camera and still solve only four,
        because the graph is three separate groups — with nothing on screen
        saying so (the solve keeps the largest connected component).
        """
        parent = list(range(self.n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(self.n):
            for j in range(i + 1, self.n):
                if self.shared[i, j] >= self.min_edge:
                    parent[find(i)] = find(j)

        groups: dict[int, list[int]] = {}
        for i in range(self.n):
            groups.setdefault(find(i), []).append(i)
        return sorted(groups.values(), key=lambda g: (-len(g), g[0]))

    def _update_ready(self):
        """Recompute components and READY. READY latches: every input is
        monotone (counts only grow, edges only appear), so a true can never
        honestly become false again, and a flicker would confuse the operator."""
        self.components = self._components() if self.n else []
        if self.n >= 2:
            lam, weak, rest = spectral_cut(self.shared, self.min_edge)
            self.connectivity, self.weak_cut = lam, (weak, rest)
        if self.ready:
            return
        if self.n == 0 or np.any(self.per_cam_frames < self.min_per_cam_shared):
            return
        if np.any(self.grid_cells_hit < self.MIN_GRID_CELLS):
            return
        if any(self.view_needs().values()):
            return
        if len(self.components) != 1:
            return
        self.ready = self.connectivity >= self.MIN_CONNECTIVITY


# ---------------------------------------------------------------------------
# codet_frames.json: the hint file the solve reads
# ---------------------------------------------------------------------------

#: The layout write_codet_frames writes: per camera, the block IDs to decode.
CODET_FORMAT = 3
#: Where each layout keeps its per-camera lists. Format 3 lists block IDs
#: under ``block_ids``; format 2 listed grabbed-frame counts under ``frames``,
#: and the solve still reads it. RULE: the two layouts use different keys.
#: REASON: a reader that knows only format 2 then finds no ``frames`` in a
#: format-3 file and never decodes block IDs as frame indices.
CODET_KEY_BLOCK_IDS = "block_ids"
CODET_KEY_FRAMES = "frames"


def codet_hint_key(doc):
    """The key holding a hint document's per-camera lists.

    ``CODET_KEY_BLOCK_IDS`` for format 3, ``CODET_KEY_FRAMES`` for format 2,
    and None for the flat legacy layout (``{cam: [frames]}``) or anything that
    is not a JSON object.
    """
    if not isinstance(doc, dict):
        return None
    for key in (CODET_KEY_BLOCK_IDS, CODET_KEY_FRAMES):
        if key in doc:
            return key
    return None


def codet_indices(block_ids, ordinals):
    """Video frame indices of hinted block IDs, and how many the video lacks.

    ``ordinals`` is the camera's ``blockids.npy`` unwrapped
    (``frame_sync.unwrap_blockids``), so ``ordinals[i]`` is the trigger of
    video frame i and the array is strictly increasing. A hinted ID the video
    does not hold (a trigger this camera missed, or one that kick mode or the
    alignment removed) is left out and counted, because decoding a nearby
    frame instead would pair two cameras on different triggers.

    Returns ``(indices, absent)``: a sorted list of int and an int.
    """
    ords = np.asarray(ordinals, dtype=np.int64).ravel()
    ids = np.unique(np.asarray(list(block_ids), dtype=np.int64))
    if ords.size == 0 or ids.size == 0:
        return [], int(ids.size)
    pos = np.minimum(np.searchsorted(ords, ids), ords.size - 1)
    hit = ords[pos] == ids
    return pos[hit].tolist(), int((~hit).sum())


def _write_json_atomic(path, doc) -> None:
    """Write ``doc`` as JSON through a sibling temporary file and os.replace.

    RULE: the hint file is replaced whole or not at all. REASON: a crash or a
    full disk during an in-place rewrite leaves truncated JSON, and the hints
    of that calibration are then lost to every later solve.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_codet_frames(path, codet_frames, camera_names, videos=None):
    """Write the co-detection hint file the solve reads, in format 3.

    ``codet_frames`` is ``BoardDetector.codet_frames``, one
    ``{cam_index: block_id}`` per tick; ``camera_names`` maps camera index to
    name. ``videos`` is ``{cam_name: {"name": mp4 file name, "size": bytes}}``
    when the videos already exist; at acquisition stop they usually do not
    (the remux runs afterwards), so the caller stamps them later with
    ``stamp_codet_videos``. The solve refuses hints whose video identity does
    not match the mp4 it is about to open, because a stale hint file from a
    previous calibration decodes the wrong frames with no error.

    RULE: each camera of a tick is listed with every block ID of that tick,
    not only its own. REASON: the cameras' latest frames at one tick can
    straddle a trigger (one camera already holds trigger T, another still
    T-1), and the solve pairs two cameras only on a trigger both decoded. With
    the union, every pair of a tick shares every trigger the tick saw. The
    solve re-detects the board in each listed frame, so an extra frame costs
    one detection and can never pair a frame without the board. A few percent
    of co-detections straddle, so the union adds about that many decodes.

    Returns the total number of hints written.
    """
    per_idx: dict[int, set] = {}
    for tick in codet_frames:
        ids = {int(b) for b in tick.values()}
        for cam_idx in tick:
            per_idx.setdefault(int(cam_idx), set()).update(ids)
    doc = {
        "format": CODET_FORMAT,
        "written": datetime.now().isoformat(timespec="seconds"),
        CODET_KEY_BLOCK_IDS: {camera_names[i]: sorted(per_idx[i])
                              for i in sorted(per_idx)},
        "videos": dict(videos or {}),
    }
    _write_json_atomic(path, doc)
    return sum(len(v) for v in doc[CODET_KEY_BLOCK_IDS].values())


def calibration_video(cam_dir):
    """The calibration mp4 in a camera directory, or None.

    The rule is ``alignment.video_for(cam_dir, "calibration")``: the one mp4
    whose name ends in ``-calibration.mp4``, which is how every writer names
    it. RULE: two candidates raise ``ValueError``. REASON: taking the first of
    two would solve from a video nobody chose, with nothing in the report
    saying which one it was.
    """
    cam_dir = Path(cam_dir)
    if not cam_dir.is_dir():
        return None
    # Imported here, not at module level: alignment reads the session config,
    # which imports yaml, and this module must import with numpy alone.
    from gui_app.alignment import video_for
    return video_for(cam_dir, "calibration")


def stamp_codet_videos(calib_dir):
    """Record each camera's mp4 name and size in ``codet_frames.json``.

    Called once the calibration mp4s are FINAL, not merely present: the
    identity is the file size, and alignment (``alignment.extract_aligned``)
    replaces the session recording with a re-encoded file of a different size.
    A stamp taken before alignment runs therefore mismatches on exactly the
    sessions that were aligned, and the solve discards the hints and scans in
    full. The GUI's right moment is the transition to idle, reached both when
    no alignment runs and after alignment finishes.

    A camera without exactly one calibration mp4 is left unstamped, so the
    solve scans that camera in full and keeps the others' hints. Returns the
    number of cameras stamped, 0 when there is no hint file to stamp.
    """
    calib_dir = Path(calib_dir)
    path = calib_dir / "codet_frames.json"
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    key = codet_hint_key(doc)
    if key is None:
        # Legacy flat {cam: [frames]} layout: lift it into format 2, whose
        # per-camera values are frame numbers like the legacy ones.
        doc = {"format": 2, "written": "", CODET_KEY_FRAMES: doc,
               "videos": {}}
        key = CODET_KEY_FRAMES
    videos = {}
    for cam in doc[key]:
        try:
            mp4 = calibration_video(calib_dir / cam)
        except ValueError as e:
            print(f"[hud] {cam} not stamped: {e}", flush=True)
            continue
        if mp4 is not None:
            videos[cam] = {"name": mp4.name, "size": mp4.stat().st_size}
    doc["videos"] = videos
    _write_json_atomic(path, doc)
    return len(videos)
