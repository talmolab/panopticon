"""Prove FrameSyncCoordinator == post-hoc block-ID intersection.

For each random scenario: build per-camera delivered-trigger streams (independent
drops, optional stragglers/freezes/wrap), feed them to the coordinator in a
randomized but per-camera-ordered interleaving, and check:
  - group integrity: every released trigger is released by ALL cameras, once,
    and in increasing order per camera (so encoders get a clean stream);
  - no forcing => released set EXACTLY equals the intersection of delivered sets;
  - with forcing => released is a subset of the intersection (only triggers the
    intersection would keep can ever be released), and loss is bounded.
"""
import random
from collections import defaultdict
from gui_app.frame_sync import FrameSyncCoordinator, check_block_id_rate


def feed(n, delivered, max_lag, raw=None, max_skew=None):
    """delivered[c] = sorted list of triggers camera c captured.
    raw[c] = matching raw (possibly wrapped) block IDs to submit (defaults to
    delivered). Interleave submissions preserving per-camera order, with optional
    bounded skew between cameras."""
    raw = raw or delivered
    co = FrameSyncCoordinator(n, max_lag=max_lag)
    ptr = [0] * n
    out = []  # (cam, trigger)
    order_pos = [0] * n  # how many this cam has submitted
    while any(ptr[c] < len(delivered[c]) for c in range(n)):
        cand = [c for c in range(n) if ptr[c] < len(delivered[c])]
        if max_skew is not None:
            lead = min(order_pos[c] for c in cand)
            cand = [c for c in cand if order_pos[c] - lead < max_skew]
        c = random.choice(cand)
        for cam, t, _f in co.submit(c, raw[c][ptr[c]], None):
            out.append((cam, t))
        ptr[c] += 1
        order_pos[c] += 1
    for cam, t, _f in co.flush():
        out.append((cam, t))
    return co, out


def check_group_integrity(n, out):
    by_t = defaultdict(set)
    per_cam_seq = defaultdict(list)
    for cam, t in out:
        assert cam not in by_t[t], f"trigger {t} released twice for cam {cam}"
        by_t[t].add(cam)
        per_cam_seq[cam].append(t)
    for t, cams in by_t.items():
        assert len(cams) == n, f"trigger {t} released by {len(cams)}/{n} cams (partial!)"
    for cam, seq in per_cam_seq.items():
        assert seq == sorted(seq), f"cam {cam} released out of order"
    return set(by_t.keys())


def intersection(delivered):
    s = set(delivered[0])
    for d in delivered[1:]:
        s &= set(d)
    return s


def rand_delivered(n, total, drop_rate):
    return [sorted(t for t in range(1, total + 1) if random.random() >= drop_rate)
            for _ in range(n)]


def main():
    random.seed(1234)
    N = 6

    # 1) exact equivalence, no forcing (huge max_lag), varied drop rates
    for trial in range(400):
        total = random.randint(50, 1500)
        dr = random.choice([0.0, 0.001, 0.01, 0.05, 0.2])
        delivered = rand_delivered(N, total, dr)
        co, out = feed(N, delivered, max_lag=10 * total + 10)
        rel = check_group_integrity(N, out)
        assert rel == intersection(delivered), (
            f"trial {trial}: released != intersection "
            f"({len(rel)} vs {len(intersection(delivered))})")
        assert co.forced == 0
    print("1) exact equivalence (no forcing), 400 trials: PASS")

    # 2) bounded skew but within max_lag => still exact
    for trial in range(200):
        total = random.randint(100, 800)
        delivered = rand_delivered(N, total, 0.02)
        co, out = feed(N, delivered, max_lag=60, max_skew=40)
        rel = check_group_integrity(N, out)
        assert rel == intersection(delivered), f"skew trial {trial} mismatch"
        assert co.forced == 0
    print("2) bounded skew within max_lag, 200 trials: PASS")

    # 3) stragglers force-drop: released subset of intersection, integrity holds
    for trial in range(200):
        total = random.randint(200, 1000)
        delivered = rand_delivered(N, total, 0.02)
        co, out = feed(N, delivered, max_lag=30, max_skew=300)  # big skew -> forcing
        rel = check_group_integrity(N, out)
        inter = intersection(delivered)
        assert rel <= inter, f"forcing trial {trial}: released NOT subset of intersection"
    print("3) stragglers w/ forcing (subset + integrity), 200 trials: PASS")

    # 4) hard freeze: one camera stops for a long stretch then resumes
    for trial in range(100):
        total = 1000
        delivered = rand_delivered(N, total, 0.01)
        victim = random.randrange(N)
        # remove a 150-trigger block from the victim (freeze)
        start = random.randint(200, 700)
        delivered[victim] = [t for t in delivered[victim]
                             if not (start <= t < start + 150)]
        co, out = feed(N, delivered, max_lag=40, max_skew=500)
        rel = check_group_integrity(N, out)
        assert rel <= intersection(delivered)
        # cameras keep flowing: triggers well before and after the freeze survive
        assert any(t < start for t in rel) and any(t > start + 150 for t in rel)
    print("4) hard freeze recovery (others keep flowing), 100 trials: PASS")

    # 5) 16-bit wrap: raw IDs wrap 65535->1, unwrap must keep alignment
    total = 70000  # crosses 65535
    delivered = rand_delivered(N, total, 0.01)
    raw = [[((t - 1) % 65535) + 1 for t in d] for d in delivered]  # wrapped raw
    co, out = feed(N, delivered, max_lag=10 * total, raw=raw)
    rel = check_group_integrity(N, out)
    assert rel == intersection(delivered), "wrap: released != intersection"
    print("5) 16-bit wrap unwrap (70k triggers), 1 trial: PASS")

    # 6) all-perfect (no drops) => everything released, nothing dropped
    delivered = [list(range(1, 2001)) for _ in range(N)]
    co, out = feed(N, delivered, max_lag=240)
    rel = check_group_integrity(N, out)
    assert rel == set(range(1, 2001)) and co.dropped == 0
    print("6) zero-loss passthrough, 1 trial: PASS")

    test_retire_keeps_survivors_aligned()
    test_retired_camera_submissions_ignored()
    test_forced_drops_are_attributed()
    test_block_rate_catches_skipped_triggers()
    test_non_positive_block_ids_refused()
    test_lag_frames_and_quiet_retire()

    print("\nALL FRAMESYNC EQUIVALENCE TESTS PASS")


# ── camera retirement ────────────────────────────────────────────────────────
# A stalled camera must not pin the watermark: without retire() every later
# trigger is force-dropped and the recording yields nothing from the stall
# onward. Retiring it keeps the survivors aligned.

def test_retire_keeps_survivors_aligned():
    co = FrameSyncCoordinator(3, max_lag=10)
    out = []
    for t in range(1, 6):                      # 5 clean triggers, all 3 cams
        for c in range(3):
            out += co.submit(c, t, f"c{c}t{t}")
    assert co.released_triggers == 5

    # cam2 stalls. Without retirement the other two starve past max_lag.
    for t in range(6, 40):
        for c in (0, 1):
            co.submit(c, t, f"c{c}t{t}")
    before = co.released_triggers
    assert before == 5, "a stalled camera should block releases until retired"

    co.retire(2, "stalled in test")
    got = []
    for t in range(40, 50):
        for c in (0, 1):
            got += co.submit(c, t, f"c{c}t{t}")
    assert co.released_triggers > before, "retirement did not resume releases"
    cams = {c for c, _t, _f in got}
    assert cams == {0, 1}, f"retired camera still being released: {cams}"
    # every released trigger must still be held by BOTH survivors
    per_t = {}
    for c, t, _f in got:
        per_t.setdefault(t, set()).add(c)
    assert all(v == {0, 1} for v in per_t.values()), "survivors not aligned"
    print("7) retiring a stalled camera keeps survivors aligned: PASS")


def test_retired_camera_submissions_ignored():
    co = FrameSyncCoordinator(2, max_lag=10)
    co.retire(1, "test")
    assert co.submit(1, 5, "late") == []
    out = []
    for t in range(1, 4):
        out += co.submit(0, t, f"t{t}")
    assert [c for c, _t, _f in out] == [0, 0, 0]
    assert co.released_triggers == 3
    print("8) a retired camera's late frames never re-enter the stream: PASS")


def test_forced_drops_are_attributed():
    """The log must name the laggard — that is the whole point of the counter."""
    co = FrameSyncCoordinator(3, max_lag=5)
    for t in range(1, 30):
        for c in (0, 1):        # cam2 never delivers
            co.submit(c, t, None)
    assert co.forced > 0, "no forcing happened, test is not exercising the path"
    assert co.forced_by[2] > 0, "laggard not identified"
    assert co.forced_by[0] == 0 and co.forced_by[1] == 0, "blamed the wrong camera"
    assert "c3:" in co.lag_report()
    print("9) forced drops are attributed to the lagging camera: PASS")


# ── block ID == trigger ordinal ──────────────────────────────────────────────
# The release rule matches on block ID alone. A camera that IGNORES triggers
# (exposure over the ceiling) keeps its block IDs gapless and ends up with the
# same frame count as everyone else, so the coordinator, the intersection and
# every packet counter all report a clean session while the videos are skewed
# by seconds. The device clock is the only independent witness.

def test_block_rate_catches_skipped_triggers():
    fps = 100
    n = 6000                                  # 60 s at 100 fps
    ids = list(range(1, n + 1))

    healthy = [i / fps for i in range(n)]
    assert check_block_id_rate(ids, healthy, fps, "cam1") is None, \
        "false positive on a clean camera"

    # Crystal-level disagreement between the two clocks must not trip it.
    drifted = [i / fps * 1.0005 for i in range(n)]
    assert check_block_id_rate(ids, drifted, fps, "cam1") is None, \
        "tolerance is too tight for real oscillator drift"

    # Every second trigger ignored: block ID k happened at 2k periods.
    halved = [i * 2 / fps for i in range(n)]
    msg = check_block_id_rate(ids, halved, fps, "cam3")
    assert msg and "cam3" in msg, "missed a camera running at half rate"
    assert "60.0 s" in msg, f"drift not reported correctly: {msg}"

    # The dangerous regime: one trigger in a hundred, no gaps, equal counts.
    subtle = [i / fps * (100 / 99) for i in range(n)]
    msg = check_block_id_rate(ids, subtle, fps, "cam2")
    assert msg, "missed a 1%% skip — that is the case nothing else can see"

    # Too short to judge => abstain rather than guess.
    assert check_block_id_rate(ids[:100], halved[:100], fps, "cam1") is None
    assert check_block_id_rate([], [], fps, "cam1") is None

    # Block IDs cannot outrun the trigger; say so differently.
    fast = [i / (fps * 1.5) for i in range(n)]
    msg = check_block_id_rate(ids, fast, fps, "cam4")
    assert msg and "cannot outrun" in msg, f"wrong diagnosis: {msg}"

    print("10) block-ID rate check catches ignored triggers: PASS")


# ── the -1 / 0 placeholder must never enter the unwrap ───────────────────────
# GVSP reserves 0 and no camera reports a negative ID, so either value means
# "no ordinal". Unwrapped, it reads as a 16-bit wrap and places the camera far
# ahead, force-dropping every other camera. Refusing it is the live half of the
# guard alignment._unwrap_blockids applies post hoc.

def test_non_positive_block_ids_refused():
    co = FrameSyncCoordinator(2, max_lag=10)
    for t in range(1, 4):
        for c in range(2):
            co.submit(c, t, None)
    for bad in (0, -1):
        try:
            co.submit(0, bad, None)
        except ValueError as e:
            assert "ordinal" in str(e), e
        else:
            raise AssertionError(f"block ID {bad} was accepted")
    # The refused value left no trace: the camera's frontier and the release
    # rule are untouched, so the next good frame releases normally.
    out = co.submit(0, 4, None) + co.submit(1, 4, None)
    assert {t for _c, t, _f in out} == {4}, out
    assert co.released_triggers == 4
    print("11) block IDs <= 0 raise instead of unwrapping as a wrap: PASS")


def test_lag_frames_and_quiet_retire():
    co = FrameSyncCoordinator(3, max_lag=50)
    for t in range(1, 11):
        co.submit(0, t, None)
        co.submit(1, t, None)
    for t in range(1, 4):
        co.submit(2, t, None)
    assert co.lag_frames() == [0, 0, 7], co.lag_frames()
    assert co.decided_upto == 3
    msg = co.retire(2, "quiet test", announce=False)
    assert msg and "RETIRED" in msg and "cam3" in msg, msg
    assert co.retire(2, "again", announce=False) is None
    assert co.lag_frames() == [0, 0, -1], co.lag_frames()
    print("12) lag_frames() counts triggers behind the leader; retire() can hand "
          "its log line to the caller: PASS")


if __name__ == "__main__":
    main()
