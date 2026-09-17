"""blockids.npy must only record frames that were actually persisted.

`SyncEncodeRouter.stop()` and the decoupled `GrabThread` path are the only
guards for that invariant: they reconcile the recorded block IDs against
encoded + spilled, write WARNINGS.txt, fold in retirements and queue-full
drops, and persist the encoded/spilled split point. A regression in any of it
produces recordings that look perfect and map frame i to the wrong trigger,
and until this file nothing exercised those paths without a GPU.

Everything here runs with a fake encoder installed through the
`gui_app.encoders` factory seam: no NVENC, no cameras, no vendor SDK.

    python test_sync_router_offline.py
"""
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.modules["pypylon"] = None            # the capture modules must not need it

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

import numpy as np

from gui_app import encoders, nvenc
from gui_app import grab_thread as gt
from gui_app import sync_encode as se
from gui_app.grab_thread import GrabThread
from gui_app.sync_encode import SyncEncodeRouter

W, H = 64, 48
NV12 = (H * 3 // 2, W)
failures = []


def check(num, name, ok, detail=""):
    print(f"{num}) {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if not ok and detail else ""))
    if not ok:
        failures.append(name)


# Drains at stop are bounded by 30 s + 60 s in production; the wedged-encoder
# cases here would wait that long, so the bounds are shortened for the test.
se.DRAIN_SENTINEL_TIMEOUT_S = gt.DRAIN_SENTINEL_TIMEOUT_S = 0.3
se.DRAIN_JOIN_TIMEOUT_S = gt.DRAIN_JOIN_TIMEOUT_S = 0.5
gt.PUT_TIMEOUT_S = 0.05


class FakeEncoder:
    """Duck-typed EncoderProtocol with knobs: fail after N frames, or block
    every Encode on an event (a wedged GPU)."""

    instances = []

    def __init__(self, fail_after=None, gate=None):
        self.fail_after = fail_after
        self.gate = gate                 # threading.Event; Encode waits on it
        self.encoded = 0
        self.ended = 0
        self.closed = 0
        self.in_encode = 0
        FakeEncoder.instances.append(self)

    def Encode(self, nv12):
        if self.gate is not None:
            self.in_encode += 1
            self.gate.wait()
        if self.fail_after is not None and self.encoded >= self.fail_after:
            raise RuntimeError("simulated encoder death")
        self.encoded += 1
        return b"\x00\x00\x00\x01" + bytes([self.encoded % 256]) * 8

    def EndEncode(self):
        self.ended += 1
        return b""

    def Close(self):
        self.closed += 1


class FakeFactory:
    """Per-camera encoder configuration, plus a note or a raise on demand."""

    def __init__(self):
        self.per_cam = {}
        self.raise_at = None
        self.note = None
        self.created = []

    def __call__(self, width, height, quality, fps, notes):
        i = len(self.created)
        if self.raise_at is not None and i == self.raise_at:
            raise RuntimeError("simulated NVENC session cap (Error code : 21)")
        if self.note:
            notes.append(self.note)
        enc = FakeEncoder(**self.per_cam.get(i, {}))
        self.created.append(enc)
        return enc


def _dirs(tmp, n):
    paths = []
    for i in range(n):
        d = tmp / f"cam{i+1}"
        d.mkdir()
        paths.append(d / "raw.bin")
    return paths


def _submit_all(router, n_cams, triggers, cams=None):
    buf = np.full(NV12, 128, np.uint8)
    for t in triggers:
        for c in (cams if cams is not None else range(n_cams)):
            router.submit(c, t, t / 100.0, buf)


def _warn_text(d):
    p = d / "WARNINGS.txt"
    return p.read_text() if p.exists() else ""


# 1 -- factory seam -----------------------------------------------------------
prev = encoders.set_default_factory(None)
check(1, "the default encoder factory is NVENC until replaced",
      prev is encoders.nvenc_factory and encoders.get_default_factory() is encoders.nvenc_factory)

# 2 -- happy path through set_default_factory ---------------------------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    fac = FakeFactory()
    encoders.set_default_factory(fac)
    try:
        r = SyncEncodeRouter(_dirs(tmp, 3), W, H, 21, fps=100, max_lag=50)
        r.start()
        _submit_all(r, 3, range(1, 101))
        res = r.stop()
    finally:
        encoders.set_default_factory(None)
    ok = (r.available and all(c == 100 for c, _t, _b in res)
          and all(b == list(range(1, 101)) for _c, _t, b in res)
          and r.warnings == [] and not any(_warn_text(tmp / f"cam{i+1}") for i in range(3))
          and all((tmp / f"cam{i+1}" / "stream.h264").stat().st_size > 0 for i in range(3))
          and all(e.ended >= 1 and e.closed == 1 for e in fac.created)
          and r.live_encoders() == 0)
    check(2, "clean recording: counts, IDs, no warnings, encoders released",
          ok, f"{[c for c, _, _ in res]} warnings={r.warnings}")

# 3 -- truncation to encoded+spilled when the spill also dies -----------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    paths = _dirs(tmp, 2)
    (tmp / "cam1" / "raw_tail.bin").mkdir()       # spill open fails -> frames LOST
    fac = FakeFactory()
    fac.per_cam[0] = dict(fail_after=10)
    r = SyncEncodeRouter(paths, W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    r.start()
    _submit_all(r, 2, range(1, 101))
    res = r.stop()
    c0, _t0, b0 = res[0]
    w = _warn_text(tmp / "cam1")
    ok = (c0 == 10 and b0 == list(range(1, 11)) and len(res[0][1]) == 10
          and res[1][0] == 100
          and "truncated to 10" in w and "claimed 100" in w
          and any("truncated" in m for m in r.warnings)
          and not (tmp / "cam1" / "encoded.json").exists()
          and r.live_encoders() == 0)
    check(3, "dead encoder + dead spill: IDs truncated to persisted, WARNINGS.txt written",
          ok, f"count={c0} warnings={r.warnings!r} file={w!r}")

# 4 -- a spilled tail counts as persisted and records the split point ---------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    paths = _dirs(tmp, 2)
    fac = FakeFactory()
    fac.per_cam[1] = dict(fail_after=10)
    r = SyncEncodeRouter(paths, W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    r.start()
    _submit_all(r, 2, range(1, 101))
    res = r.stop()
    tail = tmp / "cam2" / "raw_tail.bin"
    split = tmp / "cam2" / "encoded.json"
    js = json.loads(split.read_text()) if split.exists() else {}
    ok = (res[1][0] == 100 and tail.stat().st_size == 90 * W * H
          and js == {"encoded": 10, "spilled": 90, "persisted": 100}
          and "spilled" in _warn_text(tmp / "cam2")
          and not (tmp / "cam1" / "encoded.json").exists())
    check(4, "spilled tail: no truncation, encoded.json holds the split point",
          ok, f"count={res[1][0]} tail={tail.stat().st_size if tail.exists() else None} json={js}")

# 5 -- UNVERIFIED: an encoder still inside Encode at stop ---------------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    paths = _dirs(tmp, 2)
    gate = threading.Event()
    fac = FakeFactory()
    fac.per_cam[0] = dict(gate=gate)
    r = SyncEncodeRouter(paths, W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    r.start()
    _submit_all(r, 2, range(1, 21))
    res = r.stop()
    w = _warn_text(tmp / "cam1")
    ok = (res[0][0] == 20 and res[0][2] == list(range(1, 21))
          and "UNVERIFIED" in w and any("UNVERIFIED" in m for m in r.warnings)
          and res[1][0] == 20 and not _warn_text(tmp / "cam2")
          and fac.created[0].ended == 0)       # never released under a live thread
    check(5, "encoder alive after join: mapping reported UNVERIFIED, nothing truncated, "
             "session left alone", ok, f"warnings={r.warnings!r}")
    gate.set()                                  # let the daemon thread finish
    r._encoders[0].join(timeout=5)
    os.close(r._fds[0])                         # the fd stop() left to the live thread

# 6 -- retirement folding -----------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    fac = FakeFactory()
    r = SyncEncodeRouter(_dirs(tmp, 3), W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    r.start()
    _submit_all(r, 3, range(1, 51))
    r.retire(2, "stalled in test")
    _submit_all(r, 3, range(51, 101), cams=(0, 1))
    res = r.stop()
    ok = (res[0][0] == 100 and res[1][0] == 100 and res[2][0] == 50
          and any("RETIRED" in m and "cam3" in m and "stalled in test" in m
                  for m in r.warnings))
    check(6, "a retirement reaches the session warnings and survivors keep going",
          ok, f"{[c for c, _, _ in res]} {r.warnings!r}")

# 7 -- queue-full drops reach warnings and WARNINGS.txt -----------------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    paths = _dirs(tmp, 2)
    gate = threading.Event()
    fac = FakeFactory()
    fac.per_cam[0] = dict(gate=gate)
    r = SyncEncodeRouter(paths, W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    r.start()
    depth = gt.ENCODE_QUEUE_DEPTH
    _submit_all(r, 2, [1])
    # Wait until cam1's encoder has taken frame 1 into Encode (where it will
    # block), then pace the rest so cam2's healthy encoder keeps up: put_nowait
    # never yields, and an unpaced burst fills a healthy queue before its
    # thread is scheduled, which is not the wedge this case is about.
    for _ in range(500):
        if fac.created[0].in_encode:
            break
        time.sleep(0.01)
    for t in range(2, depth + 12):              # 211 released; cam1 holds 1 + 200
        _submit_all(r, 2, [t])
        time.sleep(0.0005)
    dropped_before = r.dropped_full
    gate.set()
    res = r.stop()
    w = _warn_text(tmp / "cam1")
    ok = (dropped_before == 10 and res[0][0] == depth + 1 and res[1][0] == depth + 11
          and res[0][2] == list(range(1, depth + 2))
          and "dropped because its encoder queue was full" in w
          and "10 released frames" in w
          and any("queue was full" in m for m in r.warnings)
          and not _warn_text(tmp / "cam2"))
    check(7, "queue-full drops: IDs not recorded, count named in warnings + WARNINGS.txt",
          ok, f"dropped={dropped_before} counts={[c for c, _, _ in res]} file={w!r}")

# 8 -- abandon(): stops encoder threads, releases sessions, frees the files ---
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    paths = _dirs(tmp, 3)
    fac = FakeFactory()
    r = SyncEncodeRouter(paths, W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    r.start()
    _submit_all(r, 3, range(1, 31))
    time.sleep(0.05)
    r.abandon()
    deletable = True
    for i in range(3):
        try:
            (tmp / f"cam{i+1}" / "stream.h264").unlink()
        except OSError:
            deletable = False
    ok = (r.live_encoders() == 0 and all(e.ended == 1 and e.closed == 1 for e in fac.created)
          and deletable)
    check(8, "abandon() leaves no live encoder thread, releases every encoder, "
             "and the stream files can be deleted", ok,
          f"live={r.live_encoders()} ended={[e.ended for e in fac.created]} deletable={deletable}")

# 9 -- abandon() with a full queue (the sentinel cannot be put) ---------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    gate = threading.Event()
    fac = FakeFactory()
    fac.per_cam[0] = dict(gate=gate)
    r = SyncEncodeRouter(_dirs(tmp, 1), W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    r.start()
    _submit_all(r, 1, range(1, gt.ENCODE_QUEUE_DEPTH + 30))
    full = r.dropped_full > 0
    gate.set()
    r.abandon()
    check(9, "abandon() ends an encoder thread behind a FULL queue",
          full and r.live_encoders() == 0 and fac.created[0].ended == 1,
          f"full={full} live={r.live_encoders()}")

# 10 -- init failure: created encoders released, empty stream.h264 removed ----
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    paths = _dirs(tmp, 3)
    fac = FakeFactory()
    fac.raise_at = 2
    r = SyncEncodeRouter(paths, W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    ok = (not r.available and "session cap" in r.unavailable_reason
          and not (tmp / "cam1" / "stream.h264").exists()
          and not (tmp / "cam2" / "stream.h264").exists()
          and len(fac.created) == 2 and all(e.ended == 1 for e in fac.created))
    check(10, "router init failure releases the encoders it made and unlinks the "
              "empty stream files", ok,
          f"available={r.available} reason={r.unavailable_reason!r} "
          f"files={[(tmp / f'cam{i+1}' / 'stream.h264').exists() for i in range(3)]}")

# 11 -- an encoder degradation note lands in WARNINGS.txt ---------------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    fac = FakeFactory()
    fac.note = "created with reduced settings (no tuning_info)"
    r = SyncEncodeRouter(_dirs(tmp, 1), W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    r.start()
    _submit_all(r, 1, range(1, 11))
    r.stop()
    check(11, "encoder config degradation reaches warnings and WARNINGS.txt",
          any("reduced settings" in m for m in r.warnings)
          and "reduced settings" in _warn_text(tmp / "cam1"), repr(r.warnings))

# 12 -- the -1 ordinal is refused by the coordinator through the router -------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    fac = FakeFactory()
    r = SyncEncodeRouter(_dirs(tmp, 1), W, H, 21, fps=100, max_lag=50, encoder_factory=fac)
    r.start()
    raised = False
    try:
        r.submit(0, -1, 0.0, np.full(NV12, 128, np.uint8))
    except ValueError:
        raised = True
    res = r.stop()
    check(12, "router.submit(-1) raises instead of unwrapping it as a wrap",
          raised and res[0][0] == 0)

# 13 -- NVENC ladder: every rung fails -> raise; cap -> raise; rung 1 -> note --
class _FakeNvc:
    def __init__(self, mode):
        self.mode = mode
        self.calls = []

    def CreateEncoder(self, w, h, fmt, cpu, **kw):
        self.calls.append(kw)
        if self.mode == "reject_all":
            raise RuntimeError("kwarg rejected")
        if self.mode == "cap":
            raise RuntimeError("CreateEncoder Error code : 21")
        if "tuning_info" in kw:
            raise RuntimeError("no tuning_info")
        return object()


saved = (nvenc._nvc, nvenc._loaded)
try:
    nvenc._loaded = True
    f = _FakeNvc("reject_all"); nvenc._nvc = f
    try:
        nvenc.create_h264_encoder(W, H, 21, fps=100)
        exhausted_raised = False
    except RuntimeError as e:
        exhausted_raised = "every accepted kwarg set" in str(e)
    gop_everywhere = all("gopLength" in kw and "idrPeriod" in kw for kw in f.calls)
    f2 = _FakeNvc("cap"); nvenc._nvc = f2
    try:
        nvenc.create_h264_encoder(W, H, 21, fps=100)
        cap_raised = False
    except RuntimeError as e:
        cap_raised = "NVENCSTATUS 21" in str(e)
    ladder_short = len(f2.calls) == 2            # rung 0, one GC retry, no descent
    f3 = _FakeNvc("rung1"); nvenc._nvc = f3
    notes = []
    enc = nvenc.create_h264_encoder(W, H, 21, fps=100, notes=notes)
    rung1_ok = enc is not None and len(notes) == 1 and "reduced settings" in notes[0]
finally:
    nvenc._nvc, nvenc._loaded = saved
check(13, "NVENC ladder: GOP on every rung, raises when exhausted, no descent on the "
          "session cap, rung-1 success reported in notes",
      exhausted_raised and gop_everywhere and cap_raised and ladder_short and rung1_ok,
      f"exhausted={exhausted_raised} gop={gop_everywhere} cap={cap_raised} "
      f"short={ladder_short} rung1={rung1_ok}")


# -- decoupled GrabThread path ------------------------------------------------
class StubBackend:
    name = "stub"
    TimeoutException = type("StubTimeout", (Exception,), {})
    GRAB_STRATEGY = object()

    def retrieve(self, cam, timeout_ms):
        return cam.RetrieveResult(timeout_ms)

    def stream_stats(self, cam):
        return {}


class FakeResult:
    ErrorCode = 0
    ErrorDescription = ""
    PaddingX = 0
    PaddingY = 0

    def __init__(self, bid, value):
        self.BlockID = bid
        self.TimeStamp = int(bid * 1e7)
        self._value = value

    def GrabSucceeded(self):
        return True

    @contextmanager
    def GetArrayZeroCopy(self):
        yield np.full((H, W), self._value, np.uint8)

    def Release(self):
        pass


class ScriptedCamera:
    def __init__(self, script):
        self._script = list(script)
        self._grabs = True

    def StartGrabbing(self, *_a):
        pass

    def StopGrabbing(self):
        pass

    def IsGrabbing(self):
        return self._grabs

    def RetrieveResult(self, *_a):
        while True:
            if not self._script:
                self._grabs = False
                raise StubBackend.TimeoutException()
            item = self._script.pop(0)
            if callable(item):
                item()
                continue
            return item


BACKEND = StubBackend()


def _decoupled(tmp, script, fac):
    cam = ScriptedCamera(script)
    t = GrabThread(cam_index=0, camera=cam, backend=BACKEND,
                   raw_path=tmp / "cam1" / "raw.bin", display_every=10 ** 9,
                   realtime=True, width=W, height=H, router=None,
                   encoder_factory=fac)
    t._running = True
    return t, cam


# 14 -- A1-01: no queued ring slot is rewritten while Encode blocks -----------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    (tmp / "cam1").mkdir()
    gate = threading.Event()
    fac = FakeFactory()
    fac.per_cam[0] = dict(gate=gate)
    n = gt.ENCODE_QUEUE_DEPTH + 12
    frames = [FakeResult(k, k % 256) for k in range(1, n + 1)]
    t, cam = _decoupled(tmp, frames, fac)
    holder = {}

    def _inspect():
        # Called after every frame has been retrieved and the queue has been
        # full for the last 11 frames: the encoder holds slot 0 (frame 1) and
        # the queue holds frames 2..201 in slots 1..200. None of them may have
        # been overwritten by the dropped frames 202..212.
        ring = t._nv12_ring
        holder["slot0"] = int(ring[0][0, 0])
        holder["queued_ok"] = all(int(ring[k][0, 0]) == (k + 1) % 256
                                  for k in range(1, gt.ENCODE_QUEUE_DEPTH + 1))
        holder["drops"] = t.drops
        holder["ring_i"] = t._ring_i
        t.signal_triggers_stopped()
        gate.set()

    cam._script.append(_inspect)
    t.run()
    ok = (holder.get("slot0") == 1 and holder.get("queued_ok") is True
          and holder.get("drops") == 11
          and holder.get("ring_i") == gt.ENCODE_QUEUE_DEPTH + 1
          and t.block_ids == list(range(1, gt.ENCODE_QUEUE_DEPTH + 2))
          and t.warnings == [])
    check(14, "backpressure drops reuse the same ring slot: the frame in Encode and "
              "every queued slot keep their pixels", ok, str(holder))

# 15 -- A1-02: decoupled path truncates to encoded+spilled, writes WARNINGS.txt
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    (tmp / "cam1").mkdir()
    (tmp / "cam1" / "raw_tail.bin").mkdir()     # spill cannot open
    fac = FakeFactory()
    fac.per_cam[0] = dict(fail_after=10)
    frames = [FakeResult(k, k % 256) for k in range(1, 101)]
    t, cam = _decoupled(tmp, frames, fac)
    cam._script.append(t.signal_triggers_stopped)
    t.run()
    w = _warn_text(tmp / "cam1")
    ok = (t.block_ids == list(range(1, 11)) and len(t.timestamps) == 10
          and t.frame_count == 10
          and "truncated to 10" in w and any("truncated" in m for m in t.warnings)
          and fac.created[0].ended >= 1
          and not (tmp / "cam1" / "encoded.json").exists())
    check(15, "decoupled path: IDs truncated to encoded+spilled, WARNINGS.txt written, "
              "encoder released", ok, f"ids={len(t.block_ids)} warnings={t.warnings!r}")

# 16 -- decoupled path with a working spill records the split point ----------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    (tmp / "cam1").mkdir()
    fac = FakeFactory()
    fac.per_cam[0] = dict(fail_after=10)
    frames = [FakeResult(k, k % 256) for k in range(1, 101)]
    t, cam = _decoupled(tmp, frames, fac)
    cam._script.append(t.signal_triggers_stopped)
    t.run()
    split = tmp / "cam1" / "encoded.json"
    js = json.loads(split.read_text()) if split.exists() else {}
    ok = (len(t.block_ids) == 100 and js.get("encoded") == 10 and js.get("spilled") == 90
          and (tmp / "cam1" / "raw_tail.bin").stat().st_size == 90 * W * H
          and any("spilled" in m for m in t.warnings))
    check(16, "decoupled path: spilled tail kept, encoded.json written", ok,
          f"ids={len(t.block_ids)} json={js}")

# 17 -- ring failure after the encoder exists: session released, empty
#       stream.h264 removed, raw.bin fallback taken ----------------------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    (tmp / "cam1").mkdir()
    fac = FakeFactory()
    frames = [FakeResult(k, k % 256) for k in range(1, 6)]
    t, cam = _decoupled(tmp, frames, fac)
    cam._script.append(t.signal_triggers_stopped)
    real_full = gt.np.full

    def boom(shape, *a, **k):
        # Only the NV12 ring (H*3//2 rows) fails; the fake results' own
        # np.full calls go through, because the test shares numpy with the
        # module under test.
        if tuple(shape) == NV12:
            raise MemoryError("simulated: cannot allocate the NV12 ring")
        return real_full(shape, *a, **k)

    gt.np.full = boom
    try:
        t.run()
    finally:
        gt.np.full = real_full
    ok = (len(fac.created) == 1 and fac.created[0].ended == 1
          and not (tmp / "cam1" / "stream.h264").exists()
          and (tmp / "cam1" / "raw.bin").stat().st_size == 5 * W * H
          and t.block_ids == [1, 2, 3, 4, 5])
    check(17, "encoder init failure after CreateEncoder: orphan released, empty "
              "stream.h264 unlinked, raw.bin recorded", ok,
          f"ended={[e.ended for e in fac.created]} "
          f"h264={(tmp / 'cam1' / 'stream.h264').exists()}")

# 18 -- GrabThread.abandon() in decoupled mode skips the drain ----------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    (tmp / "cam1").mkdir()
    gate = threading.Event()
    fac = FakeFactory()
    fac.per_cam[0] = dict(gate=gate)
    frames = [FakeResult(k, k % 256) for k in range(1, 31)]
    t, cam = _decoupled(tmp, frames, fac)

    def _abandon():
        t.abandon()
        gate.set()

    cam._script.append(_abandon)
    t0 = time.perf_counter()
    t.run()
    took = time.perf_counter() - t0
    deletable = True
    try:
        (tmp / "cam1" / "stream.h264").unlink()
    except OSError:
        deletable = False
    ok = (took < 5.0 and fac.created[0].ended == 1 and deletable
          and not any("exited before" in m for m in t.warnings))
    check(18, "GrabThread.abandon(): encoder aborted and released, fd closed, "
              "no retirement", ok, f"took={took:.2f}s ended={fac.created[0].ended} "
                                   f"deletable={deletable} warnings={t.warnings!r}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL SYNC ROUTER OFFLINE TESTS PASS")
