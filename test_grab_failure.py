"""A camera that fails must be RETIRED, not left silent; a stall must re-arm and
realign, or retire with a reason.

The highest-damage failure mode in the capture path is entirely invisible
while it happens. In kick-out mode `FrameSyncCoordinator` releases trigger N
only once EVERY camera has delivered N. A camera that never publishes a frame
therefore holds the frontier at 0 forever and the coordinator force-drops
every trigger for every camera — so ONE dead camera produces an empty
recording from ALL of them, with no error beyond a single line on stdout.

`retire()` is the escape hatch: it drops the camera from the alignment set so
the survivors keep recording aligned. These tests pin that every early-return
path in `GrabThread.run()` takes it, that a re-armed stream is re-based onto
the true trigger ordinal in every recording mode, that silence before the
trigger board has started is not read as a stall, and that the driver buffer
is returned once per result on every path. The last cases pin
CameraManager's stop/abandon contract under a grab thread that will not exit:
the data is kept, the bounds are shared, and the stuck camera's handle is
leaked rather than closed under a live native call.

No cameras, no vendor SDK and no encoder: the camera, the backend and the
router are stubs, and pypylon is made un-importable up front so a regression
that pulls it back into the capture modules fails here.

    python test_grab_failure.py
"""
import json
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# The capture modules must import without the vendor SDK: the backend is an
# instance handed to GrabThread, never a module-level import.
sys.modules["pypylon"] = None
sys.modules["pypylon.pylon"] = None

# GrabThread subclasses QThread, so Qt must exist. Offscreen: no display needed.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QThread
from PyQt5.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

import numpy as np

from gui_app import camera_manager as cm
from gui_app import grab_thread as gt
from gui_app.camera_manager import CameraManager, AcquisitionStopIncomplete
from gui_app.grab_thread import GrabThread

W, H = 64, 48          # tiny: these tests never encode, only allocate
FPS = 100


class StubTimeout(Exception):
    """The backend's timeout type; the grab loop must catch THIS, not pylon's."""


class StubBackend:
    """Just the three things the hot loop reads from a backend."""
    name = "stub"
    TimeoutException = StubTimeout
    GRAB_STRATEGY = object()

    def retrieve(self, cam, timeout_ms):
        return cam.RetrieveResult(timeout_ms)

    def stream_stats(self, cam):
        return {}


BACKEND = StubBackend()


class FakeRouter:
    """Records retire() and submit() calls; mimics just enough of
    SyncEncodeRouter."""

    def __init__(self, max_lag=8, accept_submits=False):
        self.max_lag = max_lag
        self.retired = []
        self.submitted = []
        self._accept = accept_submits

    def retire(self, cam, reason=""):
        self.retired.append((cam, reason))

    def pending(self):
        return 0

    def submit(self, cam, bid, ts, buf):
        if not self._accept:
            raise AssertionError("submit() must not be reached in these tests")
        self.submitted.append((cam, bid, ts))


class FakeResult:
    """A grab result satisfying GrabResultProtocol, with a release counter."""

    ErrorCode = 0
    ErrorDescription = ""

    def __init__(self, bid, ts_s, value=0, padding_x=0, bid_raises=False):
        self._bid = bid
        self._raises = bid_raises
        self._ts = int(round(ts_s * 1e9))
        self._value = value
        self.PaddingX = padding_x
        self.PaddingY = 0
        self.released = 0

    def GrabSucceeded(self):
        return True

    @property
    def BlockID(self):
        if self._raises:
            raise RuntimeError("BlockID unavailable")
        return self._bid

    @property
    def TimeStamp(self):
        return self._ts

    @contextmanager
    def GetArrayZeroCopy(self):
        yield np.full((H, W), self._value, np.uint8)

    def Release(self):
        self.released += 1


class DeadCamera:
    """A camera whose stream refuses to start — the real-world case is a dead
    switch port, an unpowered camera, or a transport left in a bad state."""

    def __init__(self, exc=RuntimeError("the camera is offline")):
        self._exc = exc

    def StartGrabbing(self, *_a, **_k):
        raise self._exc

    def IsGrabbing(self):
        return False

    def StopGrabbing(self):
        pass


def _make(router, camera, tmp, **kw):
    args = dict(raw_path=tmp / "raw.bin", display_every=10 ** 9, realtime=True,
                width=W, height=H, router=router)
    args.update(kw)
    t = GrabThread(cam_index=3, camera=camera, backend=BACKEND, **args)
    t._running = True
    return t


def test_start_grabbing_failure_retires(tmp):
    router = FakeRouter()
    t = _make(router, DeadCamera(), tmp)
    t.run()                      # synchronous: no thread, no Qt event loop
    assert router.retired, ("StartGrabbing failed and the camera was NOT retired "
                            "— the coordinator would force-drop every trigger for "
                            "every camera and the whole session would be empty")
    cam, reason = router.retired[0]
    assert cam == 3, cam
    assert "grab" in reason.lower(), reason
    print("1) StartGrabbing failure retires the camera: PASS")


def test_ring_allocation_failure_retires(tmp, monkey_full):
    """A MemoryError allocating the NV12 ring must retire, not escape run().

    Reachable at scale rather than theoretical: the ring is `max_lag +
    ENCODE_QUEUE_DEPTH + 64` buffers per camera — 2.39 GiB each at max_lag=480,
    so ~21.5 GiB across 9 cameras on top of ~20.7 GiB of pylon pool. Escaping
    run() would take the GUI down with it.
    """
    router = FakeRouter()
    t = _make(router, DeadCamera(), tmp)
    with monkey_full:
        t.run()                  # must NOT raise
    assert router.retired, "ring MemoryError did not retire the camera"
    cam, reason = router.retired[0]
    assert cam == 3, cam
    assert "ring" in reason.lower(), reason
    print("2) NV12 ring MemoryError retires the camera, does not escape run(): PASS")


class QuietCamera:
    """Arms fine, then reports it is no longer grabbing.

    The nastiest shape of this failure: no exception, no timeout, nothing in the
    log. The while-loop condition simply goes false and run() falls out through
    `finally`. Without the catch-all retire, that leaves the coordinator waiting
    on a camera that has already gone home.
    """

    def __init__(self, grabs=False):
        self._grabs = grabs

    def StartGrabbing(self, *_a, **_k):
        pass

    def IsGrabbing(self):
        return self._grabs

    def StopGrabbing(self):
        pass


class ErroringCamera(QuietCamera):
    """Raises a non-timeout exception on every retrieve.

    Hits the broad `except Exception` handler, which must count toward the
    error bound: a print-sleep-loop there would let a camera consume frames at
    100 fps, discard all of them, and starve every OTHER camera through the
    coordinator.
    """

    def __init__(self):
        super().__init__(grabs=True)
        self.calls = 0

    def RetrieveResult(self, *_a, **_k):
        self.calls += 1
        raise RuntimeError("simulated per-frame failure")


class TimingOutCamera(QuietCamera):
    """Times out forever: a GigE stream that went quiet and never came back.

    Exercises the re-arm ladder to exhaustion. Once `rearms` reaches
    MAX_REARMS the stall branch must retire and break, not go false forever
    with the thread timing out in silence for the rest of the session.
    """

    def __init__(self):
        super().__init__(grabs=True)
        self.rearms = 0

    def RetrieveResult(self, *_a, **_k):
        raise StubTimeout("simulated stream stall")

    def StartGrabbing(self, *_a, **_k):
        self.rearms += 1


class ScriptedCamera(QuietCamera):
    """Plays a script: FakeResult objects are delivered, "timeout" raises the
    backend timeout, and a callable is invoked (to poke the thread mid-run).
    When the script runs out the camera reports it has stopped grabbing."""

    def __init__(self, script):
        super().__init__(grabs=True)
        self._script = list(script)
        self.rearms = 0
        self.results = []
        self.retrieves = 0

    def StartGrabbing(self, *_a, **_k):
        self.rearms += 1

    def RetrieveResult(self, *_a, **_k):
        while True:
            self.retrieves += 1
            if not self._script:
                self._grabs = False
                raise StubTimeout("script exhausted")
            item = self._script.pop(0)
            if callable(item):
                item()
                continue
            if item == "timeout":
                raise StubTimeout("scripted timeout")
            self.results.append(item)
            return item


def test_quiet_exit_retires(tmp):
    router = FakeRouter()
    t = _make(router, QuietCamera(grabs=False), tmp)
    t.run()
    assert router.retired, ("the grab loop exited without retiring — the "
                            "coordinator would wait forever on a camera that "
                            "is gone and force-drop every trigger for all of them")
    assert "exited" in router.retired[0][1].lower(), router.retired
    print("3) a silent exit (IsGrabbing goes False) retires the camera: PASS")


def test_repeated_errors_retire(tmp):
    router = FakeRouter()
    cam = ErroringCamera()
    t = _make(router, cam, tmp)
    t.run()
    assert router.retired, "a camera raising every frame was never retired"
    assert "error" in router.retired[0][1].lower(), router.retired
    # Must give up quickly rather than spin: bounded by MAX_CONSEC_ERRORS.
    assert cam.calls <= 25, f"spun {cam.calls} times before giving up"
    print(f"4) repeated frame errors retire after {cam.calls} attempts, "
          f"no infinite spin: PASS")


def test_rearm_exhaustion_retires(tmp):
    """A stream that never delivers, with the board never signalled as
    started: the pre-trigger grace must EXPIRE and the ladder must then
    retire, or a dead camera holds the coordinator's frontier at 0 for the
    whole session. The grace is shortened to keep the test fast; the same
    script under the default grace is what test 17 relies on."""
    router = FakeRouter()
    cam = TimingOutCamera()
    t = _make(router, cam, tmp)
    saved = gt.PRE_TRIGGER_GRACE_S
    gt.PRE_TRIGGER_GRACE_S = 0.0
    try:
        t.run()
    finally:
        gt.PRE_TRIGGER_GRACE_S = saved
    assert router.retired, ("re-arms were exhausted and the camera was never "
                            "retired — the thread would time out in silence for "
                            "the rest of the session")
    assert "re-arm" in router.retired[0][1].lower(), router.retired
    print(f"5) re-arm exhaustion retires the camera once the pre-trigger grace "
          f"has expired (after {cam.rearms} StartGrabbing calls): PASS")


def _good_frames(first_bid, n, ts_of=lambda b: b / FPS):
    return [FakeResult(b, ts_of(b), value=b % 256)
            for b in range(first_bid, first_bid + n)]


def _stall_script(t, gap_periods, resid_periods=0.0, n_before=400, n_after=5,
                  tail=True):
    """n_before good frames, a stall long enough to re-arm, then frames whose
    block IDs restart at 1 while the device clock has advanced by
    gap_periods (+ resid_periods) trigger periods."""
    before = _good_frames(1, n_before)
    restart_ts0 = (n_before + gap_periods + resid_periods) / FPS
    after = [FakeResult(k + 1, restart_ts0 + k / FPS, value=k)
             for k in range(n_after)]
    script = before + ["timeout"] * 25 + after
    if tail:
        script += [t.signal_triggers_stopped, "timeout"]
    return script


def test_rearm_resync_applies_offset(tmp):
    """A clean gap re-bases the restarted counter onto the true ordinal."""
    router = FakeRouter(accept_submits=True)
    cam = ScriptedCamera([])
    t = _make(router, cam, tmp)
    cam._script = _stall_script(t, gap_periods=300)
    t.run()
    assert cam.rearms == 2, f"expected the arm + one re-arm, got {cam.rearms}"
    assert not router.retired, router.retired
    bids = [b for (_c, b, _ts) in router.submitted]
    assert bids[:400] == list(range(1, 401)), bids[:5]
    # Restarted raw IDs 1..5 must come out as 700..704: 400 frames, a 300
    # period gap, so the first frame after the stall is trigger 700.
    assert bids[400:] == [700, 701, 702, 703, 704], bids[400:]
    assert t.frame_count == 405, t.frame_count
    assert not t.desynced
    print("6) re-arm + clean gap: restarted block IDs re-based onto the true "
          "ordinal, camera stays in the alignment set: PASS")


def test_resync_refusal_retires_and_stops(tmp):
    """A gap 0.4 periods off a boundary is refused; the camera is retired AND
    the loop leaves, so a retired camera does not keep copying 2.3 MB frames
    for the rest of the session."""
    router = FakeRouter(accept_submits=True)
    cam = ScriptedCamera([])
    t = _make(router, cam, tmp)
    cam._script = _stall_script(t, gap_periods=300, resid_periods=0.4,
                                n_after=50, tail=False)
    t.run()
    assert router.retired and "realigned" in router.retired[0][1], router.retired
    assert t.desynced
    # 400 good + 25 timeouts + the 1 frame that failed to resync; the other 49
    # scripted frames must never have been retrieved.
    assert len(cam.results) == 401, len(cam.results)
    assert len(router.submitted) == 400, len(router.submitted)
    assert all(r.released == 1 for r in cam.results), "a result was not released once"
    print("7) resync refusal retires the camera and ends its loop: PASS")


def test_padding_retires_and_releases(tmp):
    router = FakeRouter(accept_submits=True)
    padded = FakeResult(1, 0.01, padding_x=1)
    cam = ScriptedCamera([padded] + _good_frames(2, 10))
    t = _make(router, cam, tmp)
    t.run()
    assert router.retired and "padding" in router.retired[0][1], router.retired
    assert padded.released == 1, "padded result not released exactly once"
    assert len(cam.results) == 1, "loop kept grabbing after the padding retire"
    assert router.submitted == [], "a padded frame was submitted"
    print("8) PaddingX=1 retires the camera before the zero-copy view and "
          "releases the result: PASS")


def test_unreadable_block_id_retires_and_releases(tmp):
    """A BlockID the result cannot supply is a per-frame error, never a -1
    placeholder: it must reach the error bound and retire, and every result
    must still be released exactly once."""
    router = FakeRouter(accept_submits=True)
    bad = [FakeResult(None, 0.01 * k, bid_raises=True) for k in range(1, 30)]
    cam = ScriptedCamera(bad)
    t = _make(router, cam, tmp)
    t.run()
    assert router.retired and "error" in router.retired[0][1].lower(), router.retired
    assert router.submitted == [], "a frame without an ordinal was submitted"
    assert t.block_ids == [] and -1 not in t.block_ids
    n = len(cam.results)
    assert n == gt_max_consec_errors(), f"gave up after {n}, expected the error bound"
    assert all(r.released == 1 for r in cam.results), \
        [r.released for r in cam.results]
    print("9) an unreadable BlockID retires via the error bound, with every "
          "result released once: PASS")


def gt_max_consec_errors():
    # The loop-local bound; kept in one place here so the assertion reads as
    # "the bound", not a magic number.
    return 10


def test_negative_block_id_is_refused(tmp):
    """A block ID <= 0 reaching the loop is a frame error in every mode."""
    router = FakeRouter(accept_submits=True)
    cam = ScriptedCamera([FakeResult(-1, 0.01 * k) for k in range(1, 30)])
    t = _make(router, cam, tmp)
    t.run()
    assert router.retired, "a -1 BlockID was accepted"
    assert router.submitted == []
    print("10) a -1 BlockID never reaches the coordinator or disk; the camera "
          "is retired: PASS")


def test_resync_offset_uses_measured_rate(tmp):
    """The camera's own block-ID rate (+250 ppm here) must drive the resync,
    or a 40 s gap rounds to the wrong ordinal against the nominal fps."""
    rate = FPS * (1 + 250e-6)
    t = _make(FakeRouter(), QuietCamera(), tmp)
    # Ten minutes of history at the real rate: ordinal k at device time k/rate.
    n_hist = 60_000
    for k in range(1, n_hist + 1):
        t._note_block_id(k, k / rate)
    for gap_s in (5.0, 20.0, 40.0):
        k_true = round(gap_s * rate)
        ts = (n_hist + k_true) / rate
        off = t._resync_offset(1, ts)
        assert off is not None, f"gap {gap_s}s refused"
        assert 1 + off == n_hist + k_true, (gap_s, 1 + off, n_hist + k_true)
    # Against the nominal rate the same 40 s gap lands a whole period off,
    # which is exactly the systematic error the measured rate removes.
    t2 = _make(FakeRouter(), QuietCamera(), tmp)
    t2._note_block_id(1, 1 / rate)          # too little history: nominal fps
    assert t2._block_rate() == float(FPS)
    t2._last_bid_eff, t2._last_ts = n_hist, n_hist / rate
    k_true = round(40.0 * rate)
    off2 = t2._resync_offset(1, (n_hist + k_true) / rate)
    assert off2 is None or 1 + off2 != n_hist + k_true, \
        "nominal-rate arithmetic unexpectedly correct; test premise broken"
    # A genuinely off-boundary gap is still refused with the measured rate.
    assert t._resync_offset(1, (n_hist + 100 + 0.4) / rate) is None
    print("11) _resync_offset uses the camera's measured block-ID rate "
          "(5/20/40 s gaps at +250 ppm all land on the true ordinal): PASS")


def test_raw_mode_rearm_applies_offset_and_lag(tmp):
    """Raw-to-disk recording: the re-arm offset is applied to the recorded
    IDs, and delivery lag is computed (not stuck at 0.0)."""
    cam = ScriptedCamera([])
    t = _make(None, cam, tmp, realtime=False)
    cam._script = _stall_script(t, gap_periods=300)
    t.run()
    assert t.block_ids[:400] == list(range(1, 401))
    assert t.block_ids[400:] == [700, 701, 702, 703, 704], t.block_ids[400:]
    assert len(t.timestamps) == 405 and t.frame_count == 405
    assert t.warnings == [], t.warnings
    assert t.delivery_lag_s != 0.0, "delivery_lag_s never computed in raw mode"
    assert (tmp / "raw.bin").stat().st_size == 405 * W * H
    print("12) raw mode: re-arm offset applied to recorded IDs, delivery lag "
          "computed: PASS")


def test_raw_mode_resync_refusal_stops_recording(tmp):
    cam = ScriptedCamera([])
    t = _make(None, cam, tmp, realtime=False)
    cam._script = _stall_script(t, gap_periods=300, resid_periods=0.4,
                                n_after=50, tail=False)
    t.run()
    assert t.desynced
    assert t.block_ids == list(range(1, 401)), "IDs recorded after a failed resync"
    assert t.warnings and "realigned" in t.warnings[0], t.warnings
    assert (tmp / "WARNINGS.txt").exists(), "warning not written beside the video"
    assert len(cam.results) == 401, len(cam.results)
    print("13) raw mode: a failed resync ends the recording with a warning "
          "instead of recording unplaceable IDs: PASS")


def test_abandon_is_not_a_retirement(tmp):
    """abandon() on a kick recording is an intentional stop: no RETIRED line."""
    router = FakeRouter(accept_submits=True)
    cam = ScriptedCamera([])
    t = _make(router, cam, tmp)
    cam._script = _good_frames(1, 20) + [t.abandon, "timeout"]
    t.run()
    assert router.retired == [], router.retired
    assert len(router.submitted) == 20
    print("14) abandon() mid-recording retires nothing: PASS")


def test_stop_after_triggers_stopped_is_clean(tmp):
    """stop_acquisition's escalation (signal_triggers_stopped then stop) on a
    camera that keeps delivering frames ends the loop without a retirement."""
    router = FakeRouter(accept_submits=True)
    cam = ScriptedCamera([])
    t = _make(router, cam, tmp)
    cam._script = (_good_frames(1, 20) + [t.signal_triggers_stopped]
                   + _good_frames(21, 5) + [t.stop] + _good_frames(26, 100))
    t.run()
    assert router.retired == [], router.retired
    # The frame in flight when stop() lands is still processed; nothing after.
    assert len(router.submitted) <= 26, len(router.submitted)
    print("15) stop() on a still-delivering camera ends the loop within one "
          "frame, no retirement: PASS")


def test_ring_slots_formula():
    assert gt.ring_slots(480, kick=True) == 480 + gt.ENCODE_QUEUE_DEPTH + gt.KICK_RING_SLACK
    assert gt.ring_slots(None, kick=False) == gt.ENCODE_QUEUE_DEPTH + gt.DECOUPLED_RING_SLACK
    print("16) ring_slots() is the single source of the ring depth: PASS")


def test_pre_trigger_silence_is_not_a_stall(tmp):
    """Timeouts before the board has started must not re-arm or retire.

    The board is started only after every grab thread is ready, and that gap
    (sketch swap, readiness barrier, serial handshake) can exceed the stall
    bound. A re-arm inside it restarts the block-ID counter with no frame
    history to re-base it against, and the first real frame then retires the
    camera. Both kick and raw modes, since the retire applies to every mode.
    """
    for realtime, label in ((True, "kick"), (False, "raw")):
        router = FakeRouter(accept_submits=True) if realtime else None
        cam = ScriptedCamera([])
        t = _make(router, cam, tmp, realtime=realtime)
        assert not t.retrieve_loop_exited
        cam._script = (["timeout"] * 30 + _good_frames(1, 10)
                       + [t.signal_triggers_stopped, "timeout"])
        t.run()
        assert cam.rearms == 1, f"{label}: re-armed during pre-trigger silence ({cam.rearms})"
        assert not t.desynced, label
        if realtime:
            assert router.retired == [], (label, router.retired)
            bids = [b for (_c, b, _ts) in router.submitted]
        else:
            assert t.warnings == [], (label, t.warnings)
            bids = t.block_ids
        assert bids == list(range(1, 11)), (label, bids)
        assert t.frame_count == 10, (label, t.frame_count)
        assert t.retrieve_loop_exited, label
    print("17) 30 timeouts before the first frame (board not started): no re-arm, "
          "no retire, IDs 1..10 recorded in kick and raw modes: PASS")


def test_signalled_stall_without_history_retires(tmp):
    """Once the board is known to be running, silence IS a stall even before
    the first frame, and the re-armed counter cannot be re-based without
    history: the camera is retired rather than recorded under offset 0, which
    would shift every ID by the unknown number of missed triggers."""
    router = FakeRouter(accept_submits=True)
    cam = ScriptedCamera([])
    t = _make(router, cam, tmp)
    cam._script = ([t.signal_triggers_started] + ["timeout"] * 25
                   + _good_frames(1, 10))
    t.run()
    assert cam.rearms == 2, f"expected the arm + one re-arm, got {cam.rearms}"
    assert router.retired and "realigned" in router.retired[0][1], router.retired
    assert router.submitted == [], "frames were recorded under a guessed ordinal"
    print("18) signal_triggers_started + 25 timeouts with no frame history: "
          "re-armed, then retired instead of guessing offset 0: PASS")


class FailedResult(FakeResult):
    """GrabSucceeded() is False; the loop must release it and carry on."""

    ErrorCode = 0xE1000014
    ErrorDescription = "The buffer was incompletely grabbed."

    def GrabSucceeded(self):
        return False


class PoisonedFailedResult(FailedResult):
    """A failed result whose error fields raise when read."""

    @property
    def ErrorDescription(self):
        raise RuntimeError("error description unavailable")


def test_failed_grab_is_released_on_every_path(tmp):
    router = FakeRouter(accept_submits=True)
    bad = FailedResult(1, 0.01)
    poisoned = PoisonedFailedResult(2, 0.02)
    cam = ScriptedCamera([bad, poisoned] + _good_frames(3, 5)
                         + [lambda: None])
    t = _make(router, cam, tmp)
    cam._script.append(t.signal_triggers_stopped)
    cam._script.append("timeout")
    t.run()
    assert bad.released == 1, bad.released
    assert poisoned.released == 1, "a failed result whose error text raised was not released"
    assert router.retired == [], router.retired
    assert [b for (_c, b, _ts) in router.submitted] == [3, 4, 5, 6, 7]
    print("19) a failed grab is released exactly once, even when reading its "
          "error fields raises, and the loop continues: PASS")


# -- CameraManager stop/abandon under a thread that will not exit -------------


class FakeGrabThread(QThread):
    """Just the surface CameraManager touches; run() blocks on an event."""

    def __init__(self, cam, block: threading.Event = None, frame_count=100):
        super().__init__()
        self._camera = cam
        self._block = block
        self.frame_count = frame_count
        self.timestamps = [k / 100.0 for k in range(1, frame_count + 1)]
        self.block_ids = list(range(1, frame_count + 1))
        self.warnings = []
        self.retrieve_loop_exited = False
        self.stopped = 0
        self.abandoned = 0
        self.triggers_started = 0

    def run(self):
        if self._block is not None:
            self._block.wait()

    def signal_triggers_stopped(self):
        pass

    def signal_triggers_started(self):
        self.triggers_started += 1

    def stop(self):
        self.stopped += 1

    def abandon(self):
        self.abandoned += 1


class FakeManagerBackend:
    def __init__(self):
        self.stopped = []
        self.closed = []

    def stop_grabbing(self, cam):
        self.stopped.append(cam)

    def close(self, cam):
        self.closed.append(cam)


def _manager(threads, cams, backend):
    m = CameraManager.__new__(CameraManager)   # no backend load, no Qt parent
    QObject_init = cm.QObject.__init__
    QObject_init(m)
    m._backend = backend
    m._cameras = list(cams)
    m._geometry = None
    m._baseline_exp_gain = []
    m.last_warnings = []
    m.last_results = []
    m._grab_threads = list(threads)
    m._leaked_threads = []
    m._leaked_cameras = []
    m._router = None
    return m


def test_stop_incomplete_keeps_results_and_abandon_leaks_the_stuck_camera():
    cams = [object(), object(), object()]
    release = threading.Event()
    threads = [FakeGrabThread(cams[0]), FakeGrabThread(cams[1], release),
               FakeGrabThread(cams[2])]
    backend = FakeManagerBackend()
    m = _manager(threads, cams, backend)
    for t in threads:
        t.start()
    threads[0].wait(2000); threads[2].wait(2000)
    saved = (cm.STOP_NORMAL_EXIT_S, cm.STOP_FORCED_EXIT_S, cm.STOP_DRAIN_S,
             cm.ABANDON_THREAD_S)
    cm.STOP_NORMAL_EXIT_S = cm.STOP_FORCED_EXIT_S = cm.STOP_DRAIN_S = 0.2
    cm.ABANDON_THREAD_S = 0.2
    try:
        m.signal_triggers_started()
        assert all(t.triggers_started == 1 for t in threads)
        try:
            m.stop_acquisition()
            raise AssertionError("stop_acquisition returned with a live thread")
        except AcquisitionStopIncomplete as e:
            assert e.stuck == [1], e.stuck
            assert len(e.results) == 3 and all(c == 100 for c, _t, _b in e.results)
            assert e.results is m.last_results
            assert "NOT been written" in str(e) and "cam2" in str(e), str(e)
        assert threads[1].stopped == 1, "the still-receiving thread was not escalated"
        assert threads[0].stopped == 0 and threads[2].stopped == 0
        assert len(m._grab_threads) == 3, "the thread list was cleared under a live thread"

        m.abandon()
        assert threads[1].abandoned == 1
        assert cams[0] in backend.closed and cams[2] in backend.closed
        assert cams[1] not in backend.closed and cams[1] not in backend.stopped, \
            "the stuck camera was closed under its live grab thread"
        assert threads[1] in m._leaked_threads and cams[1] in m._leaked_cameras
        assert m._grab_threads == [] and m._cameras == []
    finally:
        (cm.STOP_NORMAL_EXIT_S, cm.STOP_FORCED_EXIT_S, cm.STOP_DRAIN_S,
         cm.ABANDON_THREAD_S) = saved
        release.set()
        threads[1].wait(2000)
    print("20) stop with a stuck thread: results kept on the exception and "
          "last_results, only the stuck camera escalated, abandon() leaks its "
          "handle instead of closing it: PASS")


def test_stop_bounds_are_shared_not_per_thread():
    """Nine cameras all still receiving must take one set of bounds, not nine."""
    release = threading.Event()
    cams = [object() for _ in range(9)]
    threads = [FakeGrabThread(c, release) for c in cams]
    m = _manager(threads, cams, FakeManagerBackend())
    for t in threads:
        t.start()
    saved = (cm.STOP_NORMAL_EXIT_S, cm.STOP_FORCED_EXIT_S, cm.STOP_DRAIN_S)
    cm.STOP_NORMAL_EXIT_S = cm.STOP_FORCED_EXIT_S = cm.STOP_DRAIN_S = 0.2
    try:
        t0 = time.perf_counter()
        try:
            m.stop_acquisition()
        except AcquisitionStopIncomplete as e:
            assert len(e.stuck) == 9
        took = time.perf_counter() - t0
    finally:
        cm.STOP_NORMAL_EXIT_S, cm.STOP_FORCED_EXIT_S, cm.STOP_DRAIN_S = saved
        release.set()
        for t in threads:
            t.wait(2000)
    # Three phases of 0.2 s shared across nine threads: ~0.6 s, not ~5.4 s.
    assert took < 1.5, f"stop took {took:.2f}s for 9 stuck threads (sequential waits?)"
    print(f"21) stop bounds are one deadline per phase for all threads "
          f"({took:.2f}s for 9 stuck threads): PASS")


def test_draining_thread_is_not_escalated():
    """A thread whose retrieve loop has exited but whose drain is slow, with
    frame_count still moving, is not 'still receiving' and gets no stop()."""
    release = threading.Event()
    cam = object()
    t = FakeGrabThread(cam, release)
    t.retrieve_loop_exited = True
    m = _manager([t], [cam], FakeManagerBackend())
    t.start()
    saved = (cm.STOP_NORMAL_EXIT_S, cm.STOP_FORCED_EXIT_S, cm.STOP_DRAIN_S)
    cm.STOP_NORMAL_EXIT_S = cm.STOP_FORCED_EXIT_S = 0.1
    cm.STOP_DRAIN_S = 0.5

    def _finish():
        time.sleep(0.2)
        t.frame_count += 3            # the queue took the pool's last frames
        release.set()

    threading.Thread(target=_finish, daemon=True).start()
    try:
        res = m.stop_acquisition()
    finally:
        cm.STOP_NORMAL_EXIT_S, cm.STOP_FORCED_EXIT_S, cm.STOP_DRAIN_S = saved
        release.set()
        t.wait(2000)
    assert t.stopped == 0, "a draining thread was stopped outright"
    assert m.last_warnings == [], m.last_warnings
    assert len(res) == 1 and m._grab_threads == []
    print("22) a thread in its encoder drain (loop exited, frame_count moving) "
          "is waited for, not reported as a board that ignored the stop: PASS")


class _RaisingFull:
    """Context manager swapping np.full for one that raises MemoryError."""

    def __enter__(self):
        self._orig = gt.np.full

        def boom(*_a, **_k):
            raise MemoryError("simulated: cannot allocate the NV12 ring")

        gt.np.full = boom
        return self

    def __exit__(self, *_exc):
        gt.np.full = self._orig
        return False


# ── the split point is the CODED count, not the fed one ──────────────────────

class _CodingEncoder:
    """Encoder stand-in that counts coded pictures the way libx264 does.

    `Encode()` returns as soon as the plane is handed to the child, so a frame
    is coded one call later; `frames_out` therefore trails `encoded` by the
    frame still in flight, and where the encoder dies the flush that would
    have coded it never arrives. `dies_after` frames are accepted, the next
    call raises.
    """

    def __init__(self, dies_after=None):
        self._dies_after = dies_after
        self.calls = 0
        self.frames_out = 0
        self.ended = False

    def Encode(self, nv12):
        self.calls += 1
        if self._dies_after is not None and self.calls > self._dies_after:
            raise RuntimeError("libx264 encoder died: ffmpeg exit=1")
        if self.calls > 1:
            self.frames_out += 1        # the PREVIOUS frame came back coded
        return b"nal"

    def EndEncode(self, timeout_s=None):
        self.ended = True
        if self._dies_after is not None:
            return b""                  # a dead child flushes nothing
        self.frames_out = self.calls    # a healthy flush codes the last frame
        return b"nal"

    def Close(self):
        pass


class _NvencLikeEncoder:
    """Returns each frame's bytes from Encode() and exposes no frames_out."""

    def __init__(self):
        self.calls = 0

    def Encode(self, nv12):
        self.calls += 1
        return b"nal"

    def EndEncode(self):
        return b""


def _drain(t, enc, tmp, name, n_frames):
    """Feed n_frames through a real _EncoderThread and finish it as run() does.

    Returns the camera directory the reconciliation wrote into.
    """
    d = tmp / name
    d.mkdir()
    fd = os.open(str(d / "stream.h264"),
                 os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0))
    et = gt._EncoderThread(0, enc, fd, d / "raw_tail.bin", H)
    et.start()
    for _ in range(n_frames):
        et.queue.put(np.zeros((H * 3 // 2, W), np.uint8))
    t.block_ids = list(range(1, n_frames + 1))
    t.timestamps = [i / FPS for i in range(n_frames)]
    t.frame_count = n_frames
    exited = t._finish_encoder(et)
    try:
        os.close(fd)
    except OSError:
        pass
    assert exited, "the encoder thread did not finish draining"
    return d


def test_split_point_is_the_coded_count(tmp):
    """An encoder that dies mid-stream must not have its last fed frame
    recorded as persisted.

    blockids.npy records only frames that reached a file. A frame accepted by
    Encode() and never coded leaves NO gap when it is counted, so every later
    frame of this camera maps to the wrong trigger and the recording still
    looks perfect — the same silent class as the block-ID axiom.
    """
    enc = _CodingEncoder(dies_after=5)
    t = _make(FakeRouter(), DeadCamera(), tmp, raw_path=tmp / "dying" / "raw.bin")
    d = _drain(t, enc, tmp, "dying", 8)
    # Fed frames 1-5 were accepted, 1-4 coded; 6-8 spilled. Trigger 5 reached
    # NEITHER file, and it sits between the stream and the tail — so it comes
    # out of the middle of the list, leaving a GAP that says so. Truncating the
    # end instead would leave 1-7 against a video holding 1,2,3,4,6,7,8.
    assert enc.frames_out == 4 and t.block_ids == [1, 2, 3, 4, 6, 7, 8],         (enc.frames_out, t.block_ids)
    assert t.timestamps == [i / FPS for i in (0, 1, 2, 3, 5, 6, 7)], t.timestamps
    assert t.frame_count == 7, t.frame_count
    info = json.loads((d / "encoded.json").read_text())
    assert info == {"encoded": 4, "spilled": 3, "persisted": 7}, info
    assert any("coded=4 of 5 fed" in w for w in t.warnings), t.warnings
    assert any("spliced out at index 4" in w for w in t.warnings), t.warnings
    print("23) an encoder that dies mid-stream splits at the coded count and "
          "splices the uncoded junction frame out of the middle: PASS")


def test_healthy_encoder_keeps_every_frame(tmp):
    """The reconciliation must be a no-op on a run that encoded everything."""
    enc = _CodingEncoder()
    t = _make(FakeRouter(), DeadCamera(), tmp, raw_path=tmp / "healthy" / "raw.bin")
    d = _drain(t, enc, tmp, "healthy", 8)
    assert enc.frames_out == 8 and t.block_ids == list(range(1, 9)),         (enc.frames_out, t.block_ids)
    assert t.warnings == [] and not (d / "encoded.json").exists()
    print("24) a healthy encoder keeps every block ID and writes no split "
          "point: PASS")


def test_encoder_without_frames_out_uses_the_fed_count(tmp):
    """NVENC returns each frame's bytes from Encode(), so fed IS coded."""
    enc = _NvencLikeEncoder()
    t = _make(FakeRouter(), DeadCamera(), tmp, raw_path=tmp / "nvenc" / "raw.bin")
    d = _drain(t, enc, tmp, "nvenc", 6)
    assert enc.calls == 6 and t.block_ids == list(range(1, 7)),         (enc.calls, t.block_ids)
    assert t.warnings == [] and not (d / "encoded.json").exists()
    print("25) an encoder with no frames_out reconciles against the fed count: "
          "PASS")


def test_abandon_bounds_the_flush_as_well_as_the_join(tmp):
    """release_encoder() passes a deadline on to an EndEncode that takes one."""
    seen = []

    class _TimedEncoder:
        def Encode(self, nv12):
            return b""

        def EndEncode(self, timeout_s=None):
            seen.append(timeout_s)
            return b""

    et = gt._EncoderThread(0, _TimedEncoder(), -1, tmp / "unused.bin", H)
    et.release_encoder(timeout_s=0.25)
    assert seen == [0.25], seen
    et2 = gt._EncoderThread(0, _TimedEncoder(), -1, tmp / "unused.bin", H)
    et2.release_encoder()
    assert seen == [0.25, None], seen

    class _NoTimeoutEncoder:
        def EndEncode(self):
            seen.append("no-arg")
            return b""

    et3 = gt._EncoderThread(0, _NoTimeoutEncoder(), -1, tmp / "unused.bin", H)
    et3.release_encoder(timeout_s=0.25)
    assert seen[-1] == "no-arg", seen
    print("26) the teardown deadline reaches EndEncode where it takes one and "
          "is dropped where it does not: PASS")


def main():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        test_start_grabbing_failure_retires(tmp)
        test_ring_allocation_failure_retires(tmp, _RaisingFull())
        test_quiet_exit_retires(tmp)
        test_repeated_errors_retire(tmp)
        test_rearm_exhaustion_retires(tmp)
        test_rearm_resync_applies_offset(tmp)
        test_resync_refusal_retires_and_stops(tmp)
        test_padding_retires_and_releases(tmp)
        test_unreadable_block_id_retires_and_releases(tmp)
        test_negative_block_id_is_refused(tmp)
        test_resync_offset_uses_measured_rate(tmp)
        test_raw_mode_rearm_applies_offset_and_lag(tmp)
        test_raw_mode_resync_refusal_stops_recording(tmp)
        test_abandon_is_not_a_retirement(tmp)
        test_stop_after_triggers_stopped_is_clean(tmp)
        test_ring_slots_formula()
        test_pre_trigger_silence_is_not_a_stall(tmp)
        test_signalled_stall_without_history_retires(tmp)
        test_failed_grab_is_released_on_every_path(tmp)
        test_split_point_is_the_coded_count(tmp)
        test_healthy_encoder_keeps_every_frame(tmp)
        test_encoder_without_frames_out_uses_the_fed_count(tmp)
        test_abandon_bounds_the_flush_as_well_as_the_join(tmp)
    test_stop_incomplete_keeps_results_and_abandon_leaks_the_stuck_camera()
    test_stop_bounds_are_shared_not_per_thread()
    test_draining_thread_is_not_escalated()
    print("\nALL GRAB-FAILURE TESTS PASS")


if __name__ == "__main__":
    main()
