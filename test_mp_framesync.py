"""Cross-process frame sync == in-process frame sync.

The multi-process split is only safe if the shared-memory coordinator decides
EXACTLY what the single-process FrameSyncCoordinator decides. If it ever
diverges, cameras in different workers keep different triggers and the result
is the failure this pipeline exists to prevent: equal frame counts, gapless
block IDs, and videos that drift apart in time with nothing reporting a problem.

So this checks the property directly, over randomised drop patterns, with no
cameras and no processes — the segment is a plain bytearray, which is exactly
what the real one is a view over.

    uv run python test_mp_framesync.py
"""
import random
import sys

from gui_app.frame_sync import FrameSyncCoordinator
from gui_app.mp_framesync import (Coordinator, WorkerLedger, make_layout,
                                  ring_bits_for)

failures = []


def check(num, name, ok, detail=""):
    print(f"{num}) {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if not ok and detail else ""))
    if not ok:
        failures.append(name)


def reference(n_cams, max_lag, per_cam_triggers):
    """What the in-process coordinator releases, given the same arrivals.

    Arrivals are interleaved trigger-major, which is what hardware triggering
    produces: every camera sees trigger T before any sees T+1.
    """
    core = FrameSyncCoordinator(n_cams, max_lag=max_lag)
    released = []
    highest = max((max(t) for t in per_cam_triggers if t), default=0)
    for t in range(1, highest + 1):
        for cam in range(n_cams):
            if t in per_cam_triggers[cam]:
                for (_c, bid, _f) in core.submit(cam, t, None):
                    released.append(bid)
    for (_c, bid, _f) in core.flush():
        released.append(bid)
    return sorted(set(released)), core


def through_shm(n_cams, max_lag, per_cam_triggers):
    """What the shared-memory coordinator releases, same arrivals."""
    layout = make_layout(n_cams, max_lag)
    buf = bytearray(layout.size)
    coord = Coordinator(buf, n_cams, max_lag)
    ledgers = [WorkerLedger(buf, c) for c in range(n_cams)]
    harvested = {c: [] for c in range(n_cams)}
    highest = max((max(t) for t in per_cam_triggers if t), default=0)
    for t in range(1, highest + 1):
        for cam in range(n_cams):
            if t in per_cam_triggers[cam]:
                ledgers[cam].announce(t)
        coord.poll()
        for cam in range(n_cams):
            harvested[cam].extend(ledgers[cam].harvest())
    # End of recording: decide the tail, exactly as a real stop does.
    coord.poll()
    coord.flush()
    for cam in range(n_cams):
        harvested[cam].extend(ledgers[cam].harvest())
    released = sorted({t for c in harvested
                       for (t, ok) in harvested[c] if ok})
    return released, harvested, coord


# 1 -------------------------------------------------------------------------
lay = make_layout(9, 480)
check(1, "segment is small enough to be free (<16 KB at 9 cams/480)",
      lay.size < 16384, f"{lay.size} bytes")
check(2, "ring comfortably exceeds max_lag",
      lay.ring_bits >= 4 * 480, f"{lay.ring_bits} bits")

# 3 — no loss at all ---------------------------------------------------------
n, ml = 6, 240
tr = [set(range(1, 201)) for _ in range(n)]
ref, _ = reference(n, ml, tr)
got, _h, _c = through_shm(n, ml, tr)
check(3, "lossless: shm releases exactly what in-process releases", ref == got,
      f"{len(ref)} vs {len(got)}")

# 4 — independent random drops, well inside max_lag --------------------------
rng = random.Random(20260911)
ok_all = True
detail = ""
for trial in range(40):
    n = rng.choice([3, 6, 9])
    ml = rng.choice([120, 240, 480])
    total = rng.randint(80, 400)
    rate = rng.uniform(0.0, 0.2)
    tr = [set(t for t in range(1, total + 1) if rng.random() >= rate)
          for _ in range(n)]
    ref, _ = reference(n, ml, tr)
    got, _h, _c = through_shm(n, ml, tr)
    if ref != got:
        ok_all = False
        detail = (f"trial {trial}: n={n} max_lag={ml} rate={rate:.2f} "
                  f"ref={len(ref)} shm={len(got)} "
                  f"first diff at {next(iter(set(ref) ^ set(got)), None)}")
        break
check(4, "40 randomised drop scenarios agree exactly", ok_all, detail)

# 5 — forcing: one camera stalls past max_lag --------------------------------
n, ml = 4, 50
tr = [set(range(1, 301)) for _ in range(n)]
tr[2] = set(range(1, 101))          # cam3 stops dead at 100
ref, refcore = reference(n, ml, tr)
got, _h, coord = through_shm(n, ml, tr)
check(5, "forcing agrees when one camera stalls past max_lag", ref == got,
      f"ref={len(ref)} shm={len(got)}")

# 6 — every released trigger reaches EVERY active camera, exactly once -------
n, ml = 5, 200
tr = [set(t for t in range(1, 301) if rng.random() >= 0.1) for _ in range(n)]
_ref, _ = reference(n, ml, tr)
got, harvested, _c = through_shm(n, ml, tr)
per_cam_rel = {c: [t for (t, ok) in harvested[c] if ok] for c in range(n)}
same = all(sorted(per_cam_rel[c]) == got for c in range(n))
once = all(len(per_cam_rel[c]) == len(set(per_cam_rel[c])) for c in range(n))
ordered = all(per_cam_rel[c] == sorted(per_cam_rel[c]) for c in range(n))
check(6, "group integrity: all cameras, exactly once, in increasing order",
      same and once and ordered,
      f"same={same} once={once} ordered={ordered}")

# 7 — a worker learns the fate of every trigger it announced ------------------
decided_all = all(
    sorted(t for (t, _ok) in harvested[c]) == sorted(tr[c]) for c in range(n))
check(7, "every announced trigger gets a decision", decided_all)

# 8 — retirement -------------------------------------------------------------
n, ml = 4, 100
layout = make_layout(n, ml)
buf = bytearray(layout.size)
coord = Coordinator(buf, n, ml)
leds = [WorkerLedger(buf, c) for c in range(n)]
for t in range(1, 51):
    for c in range(n):
        leds[c].announce(t)
    coord.poll()
coord.retire(2, "test")
for t in range(51, 151):
    for c in (0, 1, 3):
        leds[c].announce(t)
    coord.poll()
coord.poll()
rel = [t for c in (0, 1, 3) for (t, ok) in leds[c].harvest() if ok]
check(8, "after retirement the survivors keep being released",
      max(rel, default=0) > 100, f"max released {max(rel, default=0)}")

# 9 — the wrap guard fires instead of silently mis-deciding -------------------
n, ml = 2, 100
layout = make_layout(n, ml)
buf = bytearray(layout.size)
coord = Coordinator(buf, n, ml)
led = WorkerLedger(buf, 0)
led.announce(1)
# Shove decided_upto far past that trigger, as a badly-lagged worker would see.
coord.seg.decided_upto = layout.ring_bits + 10
check(9, "a worker lagged past the ring reports an error, not a guess",
      led.check_lag(1) is False and led.lag_error is not None,
      str(led.lag_error))

# 10 — a stale/foreign segment is rejected ------------------------------------
from gui_app.mp_framesync import attach            # noqa: E402
try:
    attach(bytearray(4096))
    bad = False
except ValueError:
    bad = True
check(10, "a segment without the magic header is refused", bad)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL MP FRAMESYNC TESTS PASS")
