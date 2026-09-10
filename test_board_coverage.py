"""Calibration coverage: connectivity, weighting, and the READY conditions.

The condition these guard is the one that cost a real session. On 2026-09-10 a
nine-camera calibration reached `paired 260/250` and `grid 4/3` on every camera,
never went READY, and solved only four cameras: the co-visibility graph was in
three disconnected groups and the solve kept the largest. Per-camera numbers
cannot express that — each cluster looks fully covered from the inside — so the
tests below pin the graph-level behaviour that can.

No OpenCV, no cameras, no Qt: the detector is built with a stub engine so the
counting and graph logic can be driven directly.

    uv run python test_board_coverage.py
"""
import sys

import numpy as np

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


def tick(d, seen, centroid=(0.5, 0.5)):
    """One detection tick in which exactly `seen` cameras see the board."""
    d._engine.queue = [(10, centroid) if i in seen else (0, None)
                       for i in range(d.n)]
    d.update([None] * d.n)


def edge(d, i, j, n):
    d.shared[i, j] = d.shared[j, i] = n


failures = []


def check(num, name, ok):
    print(f"{num}) {name}: {'PASS' if ok else 'FAIL'}")
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

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL BOARD COVERAGE TESTS PASS")
