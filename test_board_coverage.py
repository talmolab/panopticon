"""Calibration coverage: connectivity, weighting, and the READY conditions.

The condition these guard is the one that cost a real session. On 2026-09-10 a
nine-camera calibration reached `paired 260/250` and `grid 4/3` on every camera,
never went READY, and solved only four cameras: the co-visibility graph was in
three disconnected groups and the solve kept the group holding cam1 (which
happened to be the largest; it now keeps the largest by construction). Per-camera
numbers cannot express that — each cluster looks fully covered from the inside —
so the tests below pin the graph-level behaviour that can.

No OpenCV, no cameras, no Qt: the detector is built with a stub engine so the
counting and graph logic can be driven directly. Importing the detector module
must not import cv2 (test 18 asserts it), or this test would need OpenCV.

    uv run python test_board_coverage.py
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np

import gui_app.board_detector as board_detector
from gui_app.board_detector import BoardDetector


class _StubEngine:
    """Returns whatever the test queued for this tick, camera by camera."""

    def __init__(self):
        self.queue = []

    def detect(self, frame):
        return self.queue.pop(0) if self.queue else (0, None)


def make(n, min_edge=80, min_per_cam_shared=250):
    d = BoardDetector.__new__(BoardDetector)
    d.n = n
    d.glow_threshold = 4
    d.edge_threshold = 5
    d.optimal_shared = 200
    d.min_edge = min_edge
    d.min_per_cam_shared = min_per_cam_shared
    d.glow_decay_s = 0.4
    d._engine = _StubEngine()
    d.reset()
    return d


def tick(d, seen, centroid=(0.5, 0.5), frame_counts=None, frames=None):
    """One detection tick in which exactly `seen` cameras see the board."""
    d._engine.queue = [(10, centroid) if i in seen else (0, None)
                       for i in range(d.n)]
    d.update([None] * d.n if frames is None else frames,
             frame_counts=frame_counts)


def edge(d, i, j, n):
    d.shared[i, j] = d.shared[j, i] = n


failures = []


def check(num, name, ok, detail=""):
    print(f"{num}) {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(name)


# 1 -------------------------------------------------------------------------
d = make(3)
tick(d, [0, 1])
ok = d.per_cam_covis[0] == 1 and d.per_cam_covis[1] == 1 and d.per_cam_covis[2] == 0
check(1, "a two-camera tick gives each participant one partner", ok)

# 2 -------------------------------------------------------------------------
d = make(3)
tick(d, [0, 1, 2])
ok = list(d.per_cam_covis) == [2, 2, 2]
check(2, "a three-camera tick is worth two, not one (partner weighting)", ok)

# 3 -------------------------------------------------------------------------
# The user's stated equivalence: 200 ticks with one partner should equal
# 100 ticks with two partners.
a = make(3)
for _ in range(200):
    tick(a, [0, 1])
b = make(3)
for _ in range(100):
    tick(b, [0, 1, 2])
check(3, "200 single-partner ticks == 100 two-partner ticks",
      a.per_cam_covis[0] == b.per_cam_covis[0] == 200)

# 4 -------------------------------------------------------------------------
d = make(4)
tick(d, [0, 1])
ok = d.per_cam_covis[2] == 0 and d.shared[0, 1] == 1 and d.shared[0, 2] == 0
check(4, "a camera that saw nothing gains nothing", ok)

# 5 -------------------------------------------------------------------------
d = make(4)
tick(d, [1])
ok = d.per_cam_covis.sum() == 0 and d.shared.sum() == 0
check(5, "a lone detection is not co-visibility", ok)

# 6 -------------------------------------------------------------------------
d = make(6)
for i, j in [(0, 1), (1, 2), (3, 4), (4, 5)]:
    edge(d, i, j, 100)
d._update_ready()
comps = [sorted(c) for c in d.components]
check(6, "disconnected clusters are reported as separate components",
      sorted(comps) == [[0, 1, 2], [3, 4, 5]])

# 7 -------------------------------------------------------------------------
d = make(6)
for i, j in [(0, 1), (1, 2), (3, 4), (4, 5)]:
    edge(d, i, j, 100)
edge(d, 2, 3, 79)                      # one short of min_edge
d._update_ready()
check(7, "an edge below min_edge does not join two groups",
      len(d.components) == 2)
edge(d, 2, 3, 80)                      # exactly min_edge
d._update_ready()
check(8, "an edge at exactly min_edge does join them",
      len(d.components) == 1)

# 9 -------------------------------------------------------------------------
# Everything per-camera satisfied, graph split: READY must stay false. This is
# the exact shape of the 2026-09-10 session.
d = make(6)
d.per_cam_frames = np.full(6, 999)
d.per_cam_covis = np.full(6, 999)
d.grid_cells_hit = np.full(6, 4)
for i, j in [(0, 1), (1, 2), (3, 4), (4, 5)]:
    edge(d, i, j, 500)
d._update_ready()
check(9, "per-camera targets met but graph split => NOT ready", not d.ready)

edge(d, 2, 3, 500)
d._update_ready()
check(10, "joining the last two groups flips READY", d.ready)

# 11 ------------------------------------------------------------------------
d = make(6)
d.per_cam_frames = np.full(6, 999)
d.per_cam_covis = np.full(6, 999)
d.grid_cells_hit = np.full(6, 4)
d.grid_cells_hit[4] = 2                # one camera short on spatial spread
for i in range(5):
    edge(d, i, i + 1, 500)
d._update_ready()
check(11, "connected graph but a thin grid => NOT ready", not d.ready)

# 12 ------------------------------------------------------------------------
d = make(6)
for i, j in [(0, 1), (1, 2), (3, 4), (4, 5)]:
    edge(d, i, j, 100)
edge(d, 2, 3, 46)                      # closest cross-group pair
edge(d, 0, 5, 12)
d._update_ready()
hint = d.bridge_hint()
check(12, "bridge_hint names the cross-group pair closest to an edge",
      hint is not None and set(hint[:2]) == {2, 3} and hint[2] == 46)

# 13 ------------------------------------------------------------------------
d = make(6)
for i in range(5):
    edge(d, i, i + 1, 500)
d._update_ready()
check(13, "bridge_hint is None once the graph is connected",
      d.bridge_hint() is None)

# 14 ------------------------------------------------------------------------
# Regression: the real 2026-09-10 nine-camera graph must reproduce the three
# groups, and the largest must be the four cameras the solve actually kept.
d = make(9)
d.per_cam_frames = np.full(9, 999)
d.per_cam_covis = np.full(9, 999)
d.grid_cells_hit = np.full(9, 4)
real = {(0, 3): 156, (0, 6): 102, (0, 8): 170, (1, 2): 256, (1, 5): 207,
        (2, 5): 410, (3, 8): 119, (4, 7): 141, (6, 8): 137, (1, 4): 46,
        (2, 4): 35, (4, 5): 30, (3, 6): 31}
for (i, j), v in real.items():
    edge(d, i, j, v)
d._update_ready()
comps = sorted([sorted(c) for c in d.components])
check(14, "2026-09-10 session reproduces its three groups",
      comps == [[0, 3, 6, 8], [1, 2, 5], [4, 7]] and not d.ready)
check(15, "and its largest group is the four cameras the solve kept",
      sorted(max(d.components, key=len)) == [0, 3, 6, 8])

# 16 — REGRESSION: partner weighting must not shortcut the READY threshold.
# Review 2026-09-11 found per_cam_covis (weighted by partner count) was being
# thresholded against min_per_cam_shared, so at nine cameras all seeing the
# board partners=8 and a target of 120 was met in FIFTEEN ticks. READY must
# count FRAMES; the weighted number is for display only.
d = make(9, min_per_cam_shared=120)
for _ in range(20):
    tick(d, list(range(9)))
for i in range(9):
    edge(d, i, (i + 1) % 9, 999)
d.grid_cells_hit = np.full(9, 4)
d._update_ready()
check(16, "20 all-camera ticks must NOT satisfy a 120-frame bar",
      not d.ready,
      f"frames={int(d.per_cam_frames.min())} weighted={int(d.per_cam_covis.min())}")
check(17, "weighted counter still records partner count for display",
      int(d.per_cam_covis.min()) == 20 * 8 and int(d.per_cam_frames.min()) == 20,
      f"weighted={int(d.per_cam_covis.min())} frames={int(d.per_cam_frames.min())}")

# 18 — the module must import with numpy alone: cv2 and yaml are imported
# inside the engine/detector constructors, never at module level.
check(18, "importing board_detector does not import cv2 or yaml",
      "cv2" not in sys.modules and "yaml" not in sys.modules,
      f"cv2={'cv2' in sys.modules} yaml={'yaml' in sys.modules}")

# 19 — READY latches and detection keeps running: glow decays, counters grow
# and codet_frames keep accumulating, because the hinted solve only decodes
# listed frames and post-READY waving is more data for it.
d = make(3, min_edge=2, min_per_cam_shared=2)
d.grid_cells_hit = np.full(3, 4)
for k in range(2):
    tick(d, [0, 1, 2], frame_counts=[k, k, k])
    d.grid_cells_hit = np.full(3, 4)
check(19, "two all-camera ticks at bar 2 => READY", d.ready,
      f"frames={d.per_cam_frames.tolist()} comps={len(d.components)}")
n_before = len(d.codet_frames)
frames_before = d.per_cam_frames.copy()
glow_before = d.glow.copy()
tick(d, [0, 1], frame_counts=[7, 7, 7])
check(20, "after READY codet_frames keep accumulating",
      len(d.codet_frames) == n_before + 1 and d.codet_frames[-1] == {0: 7, 1: 7})
check(21, "after READY the per-camera counters keep counting",
      d.per_cam_frames[0] == frames_before[0] + 1
      and d.per_cam_frames[2] == frames_before[2])
check(22, "after READY glow still decays for a camera that saw nothing",
      d.glow[2] < glow_before[2] and d.ready)
tick(d, [], frame_counts=[8, 8, 8])
check(23, "READY stays latched on a tick with no detections", d.ready)

# 24 — the hint recorded per tick is frame_counts[i] for exactly the cameras
# that co-detected; a lone detection records nothing.
d = make(4)
tick(d, [1, 3], frame_counts=[10, 20, 30, 40])
tick(d, [2], frame_counts=[11, 21, 31, 41])
check(24, "codet_frames records frame_counts for the co-detecting cameras only",
      d.codet_frames == [{1: 20, 3: 40}])
tick(d, [0, 1], frame_counts=None)
check(25, "no frame_counts => no hint appended", len(d.codet_frames) == 1)

# 26 — a centroid on the far edge (cx or cy == 1.0) maps to the last cell,
# never to an out-of-range index.
d = make(1)
check(26, "_centroid_to_cell clamps cx == 1.0 and cy == 1.0",
      d._centroid_to_cell(1.0, 1.0) == (1, 1)
      and d._centroid_to_cell(1.0, 0.0) == (0, 1)
      and d._centroid_to_cell(0.0, 1.0) == (1, 0)
      and d._centroid_to_cell(-0.1, 1.5) == (1, 0))

# 27 — a frames list shorter than n (a camera whose first frame has not
# arrived) is "no frame" for the missing cameras, not an IndexError.
d = make(4)
try:
    tick(d, [0, 1], frames=[None, None])
    ok = d.per_cam_frames.tolist() == [1, 1, 0, 0]
except IndexError:
    ok = False
check(27, "a frames list shorter than n does not raise", ok)

# 28 — codet_frames.json round trip: the writer records format, per-camera
# frames and (after stamping) each video's name and size; the legacy flat
# layout is lifted rather than rejected.
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    for cam in ("cam1", "cam2"):
        (td / cam).mkdir()
        (td / cam / f"{cam}_calibration.mp4").write_bytes(b"x" * (10 if cam == "cam1" else 20))
    n = board_detector.write_codet_frames(
        td / "codet_frames.json", [{0: 5, 1: 5}, {0: 9, 1: 9}, {0: 9, 1: 9}],
        ["cam1", "cam2"])
    doc = json.load(open(td / "codet_frames.json"))
    check(28, "write_codet_frames writes format 2 with per-camera sorted frames",
          n == 4 and doc["format"] == 2 and doc["frames"] == {"cam1": [5, 9], "cam2": [5, 9]}
          and doc["videos"] == {} and doc["written"])
    stamped = board_detector.stamp_codet_videos(td)
    doc = json.load(open(td / "codet_frames.json"))
    check(29, "stamp_codet_videos records each calibration mp4's name and size",
          stamped == 2 and doc["videos"] == {
              "cam1": {"name": "cam1_calibration.mp4", "size": 10},
              "cam2": {"name": "cam2_calibration.mp4", "size": 20}})
    json.dump({"cam1": [1, 2], "cam2": [3]}, open(td / "codet_frames.json", "w"))
    board_detector.stamp_codet_videos(td)
    doc = json.load(open(td / "codet_frames.json"))
    check(30, "a legacy flat hint file is lifted into the current layout when stamped",
          doc["frames"] == {"cam1": [1, 2], "cam2": [3]} and "cam2" in doc["videos"])
    (td / "codet_frames.json").unlink()
    check(31, "stamping with no hint file is a no-op returning 0",
          board_detector.stamp_codet_videos(td) == 0)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL BOARD COVERAGE TESTS PASS")
