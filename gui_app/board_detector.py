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
                        ``ready`` LATCHES: detection, glow decay, counting and
                        ``codet_frames`` all keep running afterwards, because
                        the hinted solve decodes only the frames listed in
                        ``codet_frames`` and every co-detection recorded while
                        the operator keeps waving is more data for it.
  - ``codet_frames``  : per tick, ``{cam_index: grabbed_frame_ordinal}`` for
                        the cameras that co-detected. The value is the grab
                        thread's GRABBED count read after the frame copy, so it
                        is +1 relative to the 0-based mp4 index and, in kick
                        mode, further offset by any frames the coordinator
                        force-dropped. Pairing survives because every camera
                        carries the same offset for the same tick; the solve
                        clamps the hints into range and treats them as
                        neighbourhood hints, not exact indices.
  - ``ticks_per_s``   : measured detection tick rate (EMA), for the HUD and
                        the rig log. Detection is sequential over cameras and
                        costs 6-250 ms per camera depending on scene clutter,
                        so the rate is best effort: typically 10-20 Hz for nine
                        cameras, ~1 Hz when several cameras see clutter.

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

    def detect(self, gray):
        """Return (marker_count, centroid_xy_normalized) for a frame.

        centroid_xy is the mean of all detected marker corners, normalized to
        [0, 1] by the frame dimensions. Returns (0, None) when nothing is found.
        """
        cv2 = self._cv2
        if gray is None:
            return 0, None
        if gray.ndim == 3:
            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        if not gray.flags["C_CONTIGUOUS"]:
            gray = np.ascontiguousarray(gray)
        h, w = gray.shape[:2]
        try:
            m_corners, m_ids = self._detect_markers(gray)
            if m_ids is None or len(m_ids) == 0:
                return 0, None
            n = int(len(m_ids))
            all_pts = np.concatenate([c.reshape(-1, 2) for c in m_corners])
            cx = float(all_pts[:, 0].mean()) / max(w, 1)
            cy = float(all_pts[:, 1].mean()) / max(h, 1)
            return n, (cx, cy)
        except cv2.error:
            return 0, None


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
        import yaml
        with open(board_config_path) as f:
            b = yaml.safe_load(f)
        self._engine = _CharucoEngine(b)
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
        #: Measured detection tick rate, exponentially smoothed. 0 until the
        #: second tick. Published so the HUD can show what "a tick" is worth.
        self.ticks_per_s = 0.0

    def _centroid_to_cell(self, cx, cy):
        r = min(int(cy * self.GRID_ROWS), self.GRID_ROWS - 1)
        c = min(int(cx * self.GRID_COLS), self.GRID_COLS - 1)
        return max(0, r), max(0, c)

    def update(self, frames, frame_counts=None):
        """Run one detection tick. If frame_counts (per-camera GRABBED frame
        ordinals) is provided, the co-detecting cameras' ordinals are appended
        to ``codet_frames`` for the solve to decode instead of re-scanning
        every frame.

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

        seen = []
        for i in range(self.n):
            # A frames list shorter than n (a camera whose first frame has not
            # arrived) reads as "no frame" for the missing cameras, never an
            # IndexError that would kill the worker.
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
                # Threshold FRAMES, never the partner-weighted number: at nine
                # cameras all seeing the board partners=8, so a target of 120 is
                # met in FIFTEEN ticks -- a 16.7x drop in the actual bar, which
                # would greenlight a calibration on almost no data and produce a
                # confident, badly-conditioned solve.
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
        """Recompute components and READY. READY latches: every input is
        monotone (counts only grow, edges only appear), so a true can never
        honestly become false again, and a flicker would confuse the operator."""
        self.components = self._components() if self.n else []
        if self.ready:
            return
        if self.n == 0 or np.any(self.per_cam_frames < self.min_per_cam_shared):
            return
        if np.any(self.grid_cells_hit < self.MIN_GRID_CELLS):
            return
        self.ready = (len(self.components) == 1)


# ---------------------------------------------------------------------------
# codet_frames.json: the hint file the solve reads
# ---------------------------------------------------------------------------

CODET_FORMAT = 2


def write_codet_frames(path, codet_frames, camera_names, videos=None):
    """Write the co-detection hint file the solve reads.

    ``codet_frames`` is ``BoardDetector.codet_frames``; ``camera_names`` maps
    camera index to name. ``videos`` is ``{cam_name: {"name": mp4 file name,
    "size": bytes}}`` when the videos already exist; at acquisition stop they
    usually do not (the remux runs afterwards), so the caller stamps them later
    with ``stamp_codet_videos``. The solve refuses hints whose video identity
    does not match the mp4 it is about to open, because a stale hint file from
    a previous calibration decodes the wrong frames silently.

    Returns the total number of hint indices written.
    """
    per_cam: dict[str, set] = {}
    for tick in codet_frames:
        for cam_idx, frame_n in tick.items():
            per_cam.setdefault(camera_names[cam_idx], set()).add(int(frame_n))
    doc = {
        "format": CODET_FORMAT,
        "written": datetime.now().isoformat(timespec="seconds"),
        "frames": {cam: sorted(fns) for cam, fns in per_cam.items()},
        "videos": dict(videos or {}),
    }
    with open(path, "w") as f:
        json.dump(doc, f)
    return sum(len(v) for v in doc["frames"].values())


def calibration_video(cam_dir):
    """The calibration mp4 in a camera directory, or None."""
    cam_dir = Path(cam_dir)
    if not cam_dir.is_dir():
        return None
    mp4s = sorted(f for f in cam_dir.iterdir()
                  if f.is_file() and f.suffix == ".mp4"
                  and "calibration" in f.name)
    return mp4s[0] if mp4s else None


def stamp_codet_videos(calib_dir):
    """Record each camera's mp4 name and size in ``codet_frames.json``.

    Called once the calibration mp4s are FINAL, not merely present: the
    identity is the file size, and alignment (``alignment.extract_aligned``)
    replaces the session recording with a re-encoded file of a different size.
    A stamp taken before alignment runs therefore mismatches on exactly the
    sessions that were aligned, and the solve discards the hints and scans in
    full. The GUI's right moment is the transition to idle, reached both when
    no alignment runs and after alignment finishes. Returns the number of
    cameras stamped, 0 when there is no hint file to stamp.
    """
    calib_dir = Path(calib_dir)
    path = calib_dir / "codet_frames.json"
    if not path.exists():
        return 0
    with open(path) as f:
        doc = json.load(f)
    if "frames" not in doc:
        # Legacy flat {cam: [frames]} layout: lift it into the current one.
        doc = {"format": CODET_FORMAT, "written": "", "frames": doc,
               "videos": {}}
    videos = {}
    for cam in doc["frames"]:
        mp4 = calibration_video(calib_dir / cam)
        if mp4 is not None:
            videos[cam] = {"name": mp4.name, "size": mp4.stat().st_size}
    doc["videos"] = videos
    with open(path, "w") as f:
        json.dump(doc, f)
    return len(videos)
