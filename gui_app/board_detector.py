"""Live ChArUco coverage detector for the calibration HUD.

Runs lightweight ChArUco detection on whatever frames ``coverage_worker`` hands
it — full-resolution grayscale while the grab threads are keeping full frames
(the normal case, because oblique cameras resolve badly at preview size), and
the downsampled preview for a given camera until its first full frame lands.
Tracks:

  - ``glow``          : per-camera decaying pulse, set to 1.0 on each detection
  - ``shared``        : pairwise co-detection counts (board seen by both cams in
                        the same detection tick)
  - ``per_cam_covis`` : per-camera co-visibility coverage (ticks where this cam
                        detected the board AND at least one other cam did too)
  - ``grid_cells_hit``: how many of the 2x2 FOV cells that camera has seen the
                        board in, binned by the marker centroid
  - ``ready``         : ALL THREE of — every camera has >= ``min_per_cam_shared``
                        co-visible detections; every camera has hit
                        >= ``MIN_GRID_CELLS`` of its 4 grid cells; and the
                        co-visibility graph is ONE CONNECTED COMPONENT over the
                        pairs with >= ``min_edge`` co-detections. Note the last
                        one is a connectivity test, not a per-pair test: the
                        board is one-sided, so opposed cameras can never
                        co-detect and "every pair connected" could never fill.
                        Once ``ready`` goes true ``update()`` stops counting.

Counts are at the detection tick rate (``coverage_worker``'s ~30 Hz), not the
recorded-frame rate, so the thresholds are relative coverage signals rather than
frame totals — tune them on the rig.

Works across both the pre-4.7 and >=4.7 OpenCV ArUco APIs.
"""
import math
import time

import numpy as np
import cv2
import yaml


class _CharucoEngine:
    """Returns a visible-marker count for a grayscale frame, across cv2 versions."""

    def __init__(self, board_x, board_y, marker_bits, dict_size,
                 square_length=1.0, marker_length=0.8, legacy=False):
        aruco = cv2.aruco
        dict_name = "DICT_{0}X{0}_{1}".format(marker_bits, dict_size)
        self._dict = aruco.getPredefinedDictionary(getattr(aruco, dict_name))
        # Absolute lengths don't affect detection (only board topology, the
        # dictionary, and the legacy pattern do) — we pass the real values to
        # mirror 1_calibrate.py exactly.
        self._new_api = hasattr(aruco, "CharucoDetector")
        if self._new_api:
            self._board = aruco.CharucoBoard(
                (board_x, board_y), square_length, marker_length, self._dict)
            # Boards printed before the OpenCV 4.6 charuco layout change use the
            # legacy marker pattern; without this the >=4.7 detector matches the
            # wrong markers and returns zero charuco corners.
            if legacy and hasattr(self._board, "setLegacyPattern"):
                self._board.setLegacyPattern(True)
            self._detector = aruco.CharucoDetector(self._board)
        else:
            self._board = aruco.CharucoBoard_create(
                board_x, board_y, square_length, marker_length, self._dict)
            if legacy and hasattr(self._board, "setLegacyPattern"):
                self._board.setLegacyPattern(True)
            self._params = aruco.DetectorParameters_create()

    def detect(self, gray):
        """Return (marker_count, centroid_xy_normalized) for a frame.

        centroid_xy is the mean of all detected marker corners, normalized to
        [0, 1] by the frame dimensions. Returns (0, None) when nothing is found.
        """
        if gray is None:
            return 0, None
        if gray.ndim == 3:
            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        if not gray.flags["C_CONTIGUOUS"]:
            gray = np.ascontiguousarray(gray)
        h, w = gray.shape[:2]
        try:
            if self._new_api:
                _ch_corners, _ch_ids, m_corners, m_ids = self._detector.detectBoard(gray)
            else:
                m_corners, m_ids, _ = cv2.aruco.detectMarkers(
                    gray, self._dict, parameters=self._params)
            if m_ids is None or len(m_ids) == 0:
                return 0, None
            n = int(len(m_ids))
            all_pts = np.concatenate([c.reshape(-1, 2) for c in m_corners])
            cx = float(all_pts[:, 0].mean()) / max(w, 1)
            cy = float(all_pts[:, 1].mean()) / max(h, 1)
            return n, (cx, cy)
        except cv2.error:
            return 0, None

    def count(self, gray):
        return self.detect(gray)[0]


class BoardDetector:
    GRID_ROWS = 2
    GRID_COLS = 2
    MIN_GRID_CELLS = 3  # out of 4

    def __init__(self, n_cams, board_config_path,
                 glow_threshold=4, edge_threshold=5,
                 optimal_shared=200, min_edge=40, min_per_cam_shared=120,
                 glow_decay_s=0.4, min_grid_cells=None):
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
        with open(board_config_path) as f:
            b = yaml.safe_load(f)
        self._engine = _CharucoEngine(
            b["board_x"], b["board_y"],
            b.get("marker_bits", 4), b.get("dict_size", 1000),
            square_length=b.get("square_length", 1.0),
            marker_length=b.get("marker_length", 0.8),
            legacy=b.get("board_legacy", False))
        self.reset()

    def reset(self):
        n = self.n
        self.glow = np.zeros(n)
        self.shared = np.zeros((n, n), dtype=int)
        self.per_cam_covis = np.zeros(n, dtype=int)   # partner-weighted: display
        self.per_cam_frames = np.zeros(n, dtype=int)  # ticks: what READY uses
        #: Connected components of the co-visibility graph, refreshed each tick.
        #: One component is the READY condition; more than one means the solve
        #: will keep the largest and silently drop the rest.
        self.components: list[list[int]] = [[i] for i in range(n)]
        self.grid_covered = np.zeros((n, self.GRID_ROWS, self.GRID_COLS), dtype=bool)
        self.grid_cells_hit = np.zeros(n, dtype=int)
        self.ready = False
        self._last = time.perf_counter()
        self.codet_frames = []

    def _centroid_to_cell(self, cx, cy):
        r = min(int(cy * self.GRID_ROWS), self.GRID_ROWS - 1)
        c = min(int(cx * self.GRID_COLS), self.GRID_COLS - 1)
        return max(0, r), max(0, c)

    def update(self, frames, frame_counts=None):
        """Run one detection tick. If frame_counts (per-camera recorded frame
        indices) is provided, co-detection frame numbers are saved for the
        calibration script to use instead of re-scanning every frame."""
        if self.ready:
            return self
        now = time.perf_counter()
        dt = max(0.0, now - self._last)
        self._last = now
        self.glow *= math.exp(-dt / self.glow_decay_s)

        seen = []
        for i in range(self.n):
            fr = frames[i] if frames is not None and i < len(frames) else None
            nc, centroid = self._engine.detect(fr)
            if nc >= self.glow_threshold:
                self.glow[i] = 1.0
            if nc >= self.edge_threshold:
                seen.append(i)
                if centroid is not None:
                    r, c = self._centroid_to_cell(*centroid)
                    if not self.grid_covered[i, r, c]:
                        self.grid_covered[i, r, c] = True
                        self.grid_cells_hit[i] = int(self.grid_covered[i].sum())

        if len(seen) >= 2:
            # Weighted by PARTNER COUNT, not 1 per tick. A tick in which three
            # cameras see the board yields three pairwise constraints, not one,
            # and pairwise constraints are what stereo calibration consumes — so
            # a camera that co-sees with two others is making twice the progress
            # of one that co-sees with a single neighbour. Counting ticks
            # flattened that distinction and let a camera reach its target while
            # only ever pairing with the same partner, which is exactly how a
            # co-visibility graph ends up in disconnected clusters that each
            # look well covered. Connectivity is still enforced separately in
            # _update_ready(); this only makes the per-camera number mean
            # "constraints gathered" rather than "moments seen".
            partners = len(seen) - 1
            for i in seen:
                # TWO counters, deliberately. `per_cam_frames` counts TICKS and
                # is what READY thresholds on, because the solve consumes
                # FRAMES: 1_calibrate.py caps intrinsics at 60 per camera.
                # `per_cam_covis` is partner-weighted and is for the display
                # and the bridge hint -- it says how many pairwise constraints
                # this camera has gathered, which is the right thing to steer
                # by but the WRONG thing to threshold.
                #
                # Thresholding the weighted number was a real bug (found in
                # review 2026-09-11): at nine cameras all seeing the board,
                # partners=8, so a target of 120 was met in FIFTEEN ticks --
                # a 16.7x drop in the actual bar, which would have greenlit a
                # calibration on almost no data and produced a confident,
                # badly-conditioned solve.
                self.per_cam_frames[i] += 1
                self.per_cam_covis[i] += partners
            for a in range(len(seen)):
                for b in range(a + 1, len(seen)):
                    self.shared[seen[a], seen[b]] += 1
                    self.shared[seen[b], seen[a]] += 1
            if frame_counts is not None:
                self.codet_frames.append(
                    {i: frame_counts[i] for i in seen})

        self._update_ready()
        return self

    def _components(self):
        """Connected components of the co-visibility graph, as camera indices.

        Computed every tick rather than only at READY, because it is the
        condition operators cannot see any other way: per-camera counts and grid
        coverage can all be satisfied while the graph sits in several clusters
        that never observed the board together. A 9-camera session on 2026-09-10
        reached `paired 260/250 grid 4/3` on every camera and still solved only
        4 cameras, because the graph was three separate groups — with nothing on
        screen saying so.
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

    def bridge_hint(self):
        """The pair most worth working next, or None once the graph is joined.

        Across every pair of components, the two cameras with the most shared
        detections are the ones already closest to forming an edge, so naming
        them turns "the graph is in pieces" into an instruction.
        """
        comps = self.components
        if len(comps) < 2:
            return None
        best = None
        for a_idx in range(len(comps)):
            for b_idx in range(a_idx + 1, len(comps)):
                for i in comps[a_idx]:
                    for j in comps[b_idx]:
                        n = int(self.shared[i, j])
                        if best is None or n > best[2]:
                            best = (i, j, n)
        return best

    def _update_ready(self):
        self.components = self._components() if self.n else []
        if self.n == 0 or np.any(self.per_cam_frames < self.min_per_cam_shared):
            self.ready = False
            return
        if np.any(self.grid_cells_hit < self.MIN_GRID_CELLS):
            self.ready = False
            return
        self.ready = (len(self.components) == 1)
