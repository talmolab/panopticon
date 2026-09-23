"""Simulated camera backend: a hardware-free rig that can be made to misbehave.

`SimBackend` implements `CameraBackend` (see this package's `__init__`) over
`sim_board.SimBoard`, the virtual trigger clock. Every camera is paced by that
one clock, so the cameras are synchronised for the same reason the real ones
are, and `retrieve()` blocks until a trigger is actually due rather than
returning on demand — a backend that synthesised a frame per call would make
every timing assertion in the capture path vacuous and could never time out
when the triggers stop.

WHAT IT IS FOR
Every failure this application guards against is invisible while it happens
and expensive to stage on the rig: a camera that ignores triggers, a stream
that stalls, a 16-bit block-ID wrap, row padding, a pool running dry. Each is
a knob here (`SimFaults`), so the guard can be proven with no cameras, no
trigger board and no vendor SDK — this module imports none of them, and works
with `sys.modules['pypylon'] = None`.

TIME
Timestamps and trigger ordinals are virtual (see `sim_board`), so a run can be
compressed by raising `SimBoard.speed` while the cameras still report 100 fps.
Device timestamps carry a per-camera ppm offset, because a real camera's
oscillator is not the trigger board's: that offset is exactly what
`GrabThread._block_rate` measures and what a resync across a long gap spends
its tolerance on.
"""
import random
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from gui_app.backends import sim_board
from gui_app.session_config import RigProfile

#: The profile that selects this backend, and the ONE place the simulated
#: rig's shape is written down.
#: RULE: read `n_cameras`/`frame_width`/`frame_height` from this profile
#: instead of restating them as constants here.
#: REASON: `load_backend` takes only a name, so a copy in this module is what
#: the application would actually build, while `camera_manager` checks the
#: cameras against the PROFILE. The two drifting apart surfaces as "Expected N
#: cameras but M enumerated", or as a width mismatch, with nothing naming the
#: cause — and growing the simulated rig is a one-line profile edit precisely
#: so no code has to be touched to do it.
SIM_PROFILE_PATH = Path(__file__).resolve().parents[2] / "profiles" / "sim.yaml"

#: Shape overrides installed by `configure()`, for a test that needs a rig the
#: profile does not describe. Empty means the profile decides.
_SHAPE_OVERRIDES: dict = {}

#: Per-camera faults a `load_backend("sim")` instance gets. Module state
#: rather than a constructor argument because `load_backend` takes only a
#: name, so a caller arms the simulated rig here (or by constructing
#: `SimBackend` directly) before the manager opens it.
FAULTS: dict = {}

#: What the cameras report before anything sets them, matching the .pfs values
#: the real rig records with. Exposure is load-bearing: a value over the
#: ceiling makes a camera ignore triggers, which is modelled below.
BASELINE_EXPOSURE_US = 3000.0
BASELINE_GAIN_DB = 6.0

#: Grab results a camera can hold at once. The grab loop holds exactly one and
#: releases it in a `finally`, so a pool this small turns a leaked result into
#: an immediate, named failure instead of unbounded memory growth.
BUFFER_POOL = 4

#: 16-bit GVSP block IDs run 1..65535: 0 is reserved, so the counter wraps
#: onto 1. `frame_sync.BLOCKID_WRAP` and `alignment._unwrap_blockids` assume
#: exactly this, and a wrap that produced a 0 would be read as "no ordinal".
BLOCKID_WRAP = 65535


class SimTimeout(Exception):
    """No frame arrived in time. The grab loop treats this as normal silence
    (the triggers stopped) and anything else as a fault, so the simulated
    backend must raise THIS and nothing else on a timeout."""


@dataclass
class SimFaults:
    """What one simulated camera does wrong. Every default is a healthy camera.

    The faults are deliberately expressed the way the rig produces them, so a
    test asserts on the guard rather than on the simulation:
      - `drop_triggers` / `drop_every` are frames lost in transmission: the
        camera acquired them and consumed a block ID, so the host sees a GAP.
      - `ignore_every` is the camera never acquiring, so NO id is consumed and
        the ids stay contiguous while the clock says otherwise — the one loss
        mode that leaves a recording looking perfect.
      - `stall_at` / `stall_s` wedge the stream: silence long enough for the
        grab loop's re-arm ladder, after which `StartGrabbing` restarts the
        block-ID counter and the loop must re-base it from the timestamps.
    """
    #: Device-clock offset from the trigger board, as a fraction (250e-6 is
    #: the +250 ppm measured across real sessions).
    ppm: float = 0.0
    #: Upper bound on per-frame delivery jitter, in virtual seconds. A frame
    #: is never early, only late, which is what a buffer pool produces.
    jitter_s: float = 0.0
    #: Constant delivery backlog, in triggers: frame N arrives when trigger
    #: N+k is due, with N's own block ID and timestamp.
    lag_frames: int = 0
    #: Trigger ordinals lost in transmission (block ID consumed, no frame).
    drop_triggers: frozenset = frozenset()
    #: Every n-th trigger lost in transmission; 0 disables.
    drop_every: int = 0
    #: Every n-th trigger lost to an exhausted buffer pool; 0 disables.
    #: Counted apart from `drop_every` because separating host starvation from
    #: network loss is what `stream_stats` exists for.
    underrun_every: int = 0
    #: Every n-th result reports GrabSucceeded() False; 0 disables.
    failed_grab_every: int = 0
    #: Every n-th trigger the camera does not acquire at all; 0 disables.
    ignore_every: int = 0
    #: First trigger of a stream stall, and how long it lasts in virtual
    #: seconds. 0 disables.
    stall_at: int = 0
    stall_s: float = 0.0
    #: Block ID the counter starts from after each `StartGrabbing`. 65500
    #: puts a 16-bit wrap a few frames into the recording.
    blockid_start: int = 1
    #: The numbering of the camera's raw counter. 1 is the GigE Vision
    #: convention the contract asks for (`GrabResultProtocol.BlockID`): the
    #: first frame after `StartGrabbing` reports 1, on the 1..65535 cycle.
    #: 0 is a 0-based 64-bit counter such as Spinnaker's FrameID, passed
    #: through without the normalisation a conforming backend applies: the
    #: first frame reports `blockid_start - 1` (0 by default) and the counter
    #: never wraps. It stages a backend that skipped the normalisation.
    first_block_id: int = 1
    #: Row padding reported from `padding_from` frames on. Non-zero must
    #: retire the camera: the (H, W) reshape would shear every row.
    padding_x: int = 0
    padding_y: int = 0
    padding_from: int = 1
    #: Trigger from which the camera delivers nothing, ever (a dead link).
    #: Silence, so the grab loop finds it through its stall ladder.
    dead_after: int = 0
    #: Delivered frames before every `retrieve()` raises a transport error.
    #: The loop retires the camera after MAX_CONSEC_ERRORS of these.
    fail_after: int = 0
    #: `BlockID` raises instead of answering, which the contract allows and
    #: the grab loop must survive.
    blockid_raises: bool = False
    #: `stream_stats()` reports this under its "error" key instead of counters.
    stats_error: str = ""
    #: Temperature reading, or None for a camera that reports nothing.
    thermals: dict | None = None

    def __post_init__(self):
        if self.first_block_id not in (0, 1):
            raise ValueError(
                f"first_block_id must be 1 (GigE Vision numbering) or 0 (a "
                f"0-based counter), not {self.first_block_id!r}")


def set_faults(faults: dict | None) -> dict:
    """Install the per-camera fault map (index -> SimFaults) and return the old
    one, so a caller can restore it."""
    global FAULTS
    prev, FAULTS = FAULTS, dict(faults or {})
    return prev


def profile_shape(path=None) -> tuple:
    """`(n_cameras, width, height)` as the sim profile declares them.

    Loaded through `RigProfile` rather than parsed here, because the profile
    object is what `camera_manager` compares the opened cameras against: read
    by any other route, the backend could agree with the file and still
    disagree with the caller.
    """
    prof = RigProfile.load(Path(path) if path is not None else SIM_PROFILE_PATH)
    return prof.n_cameras, prof.frame_width, prof.frame_height


def rig_shape() -> tuple:
    """The shape a `load_backend("sim")` instance is built with: the profile's,
    with any `configure()` override applied. Cold path only — it reads the
    profile file, so it is called when a backend is constructed, never per
    frame."""
    n, w, h = profile_shape()
    return (_SHAPE_OVERRIDES.get("n_cameras", n),
            _SHAPE_OVERRIDES.get("width", w),
            _SHAPE_OVERRIDES.get("height", h))


def configure(n_cameras: int | None = None, width: int | None = None,
              height: int | None = None) -> None:
    """Override the profile's shape for backends built by `load_backend`.

    A `None` leaves that dimension to the profile, so calling this with no
    arguments restores the profile's shape in full. The profile stays the
    default so the simulated rig cannot be reshaped by accident.
    """
    global _SHAPE_OVERRIDES
    _SHAPE_OVERRIDES = {
        key: int(value) for key, value in (("n_cameras", n_cameras),
                                           ("width", width),
                                           ("height", height))
        if value is not None}


def _sleep_until(deadline: float) -> None:
    """Block until `deadline`, a `perf_counter` value.

    RULE: wait against an absolute deadline, never by sleeping one period at a
    time. REASON: sleep overshoot accumulates when each wait is relative, so a
    relative pacer drifts slow and a simulated 100 fps clock stops being one.
    """
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(remaining)


class SimDevice:
    """An entry from `enumerate_devices()`. Opaque but for its serial number,
    which is all `camera_manager` reads from a device."""

    def __init__(self, index: int, serial: str):
        self.index = index
        self._serial = serial

    def GetSerialNumber(self) -> str:
        return self._serial

    def __repr__(self) -> str:
        return f"<SimDevice {self._serial}>"


class SimGrabResult:
    """One frame, satisfying `GrabResultProtocol`.

    The array is a view over a buffer the camera preallocated, never a fresh
    allocation: a simulated backend that copied per frame would hide the cost
    the real zero-copy path exists to avoid.
    """

    def __init__(self, cam, buf_index: int, block_id: int, timestamp_ns: int,
                 padding_x: int = 0, padding_y: int = 0, ok: bool = True,
                 error_code: int = 0, error_description: str = "",
                 blockid_raises: bool = False):
        self._cam = cam
        self._buf_index = buf_index
        self._block_id = block_id
        self._timestamp_ns = timestamp_ns
        self._ok = ok
        self._error_code = error_code
        self._error_description = error_description
        self._blockid_raises = blockid_raises
        self._released = False
        self._view_open = False
        self.PaddingX = padding_x
        self.PaddingY = padding_y

    def GrabSucceeded(self) -> bool:
        return self._ok

    @property
    def ErrorCode(self) -> int:
        return self._error_code

    @property
    def ErrorDescription(self) -> str:
        return self._error_description

    @property
    def BlockID(self) -> int:
        if self._blockid_raises:
            raise RuntimeError("simulated: block ID unavailable")
        return self._block_id

    @property
    def TimeStamp(self) -> int:
        return self._timestamp_ns

    @contextmanager
    def GetArrayZeroCopy(self):
        """A (H, W) uint8 C-contiguous view over the driver buffer.

        Reusing the buffer is the point: the view is only valid until
        `Release()`, and a consumer that keeps it reads a frame that has since
        been overwritten — which is the bug this contract exists to prevent.

        RULE: on leaving the block, refuse a caller that still holds a
        reference to the array, mirroring pypylon's own exit guard.
        REASON: the buffer is reused and carries a plausible payload, so an
        escaped view reads a live frame forever and the regression the
        grab-loop invariant is written against — a consumer that STORES `img`
        instead of copying out of it — would pass here and fail only on
        hardware. One extra reference is the `with ... as img` target itself,
        which Python leaves bound after the block; anything beyond that is a
        reference the consumer chose to keep.
        """
        if self._released:
            raise RuntimeError("simulated: zero-copy view after Release()")
        arr = self._cam._buffers[self._buf_index]
        baseline = sys.getrefcount(arr)
        self._view_open = True
        try:
            yield arr
        finally:
            self._view_open = False
            escaped = sys.getrefcount(arr) - baseline - 1
            if escaped > 0:
                raise RuntimeError(
                    f"simulated: {escaped} reference(s) to the zero-copy view "
                    f"outlive the with block; the view is a window onto a "
                    f"buffer that is about to be reused, so a consumer must "
                    f"copy out of it, never store it")

    def Release(self) -> None:
        """Return the buffer to the pool.

        Releasing twice, or while a view is open, raises: both are the shape
        of a grab loop that lost track of a buffer, and on real hardware both
        are silent until the pool runs dry or the view reads freed memory.
        """
        if self._released:
            raise RuntimeError("simulated: Release() called twice on one result")
        if self._view_open:
            raise RuntimeError(
                "simulated: Release() inside the zero-copy view; the view must "
                "not outlive the buffer")
        self._released = True
        self._cam._return_buffer(self._buf_index)


class SimCamera:
    """One simulated camera: a handle (`CameraHandleProtocol`) plus its state.

    Delivery is decided per trigger, in this order: a dead link, then a stall,
    then a pulse the board never fired, then a trigger the camera did not
    acquire, then one lost in transmission or to the pool. Order matters and
    is fixed so a fault schedule reads the same way every run.
    """

    def __init__(self, index: int, serial: str, width: int, height: int,
                 faults: SimFaults, board: sim_board.SimBoard,
                 max_num_buffer: int = 0):
        self.index = index
        self.serial = serial
        self.width = int(width)
        self.height = int(height)
        self.faults = faults
        self.board = board
        self.max_num_buffer = int(max_num_buffer)
        self.exposure_us = BASELINE_EXPOSURE_US
        self.gain_db = BASELINE_GAIN_DB
        self.rate_limit = 165.0
        self.gige_driver = ""
        self.extended_ids = False
        #: The `camera_spec` open() was given, kept so a test can prove the
        #: manager passed the profile's camera: block through. The simulated
        #: camera takes its settings from `SimFaults` and the module baseline,
        #: so nothing in the block is applied.
        self.camera_spec = None
        self.is_open = True
        # np.full rather than np.empty: it pre-faults every page, so the first
        # frame is not the one that pays for the allocation.
        self._buffers = [np.full((self.height, self.width), 32, np.uint8)
                         for _ in range(BUFFER_POOL)]
        self._free = list(range(BUFFER_POOL))
        self._pool_lock = threading.Lock()
        self._rng = random.Random(1000 + index)
        self._grabbing = False
        self._freerun_fps = 0.0
        self._freerun_epoch_v = 0.0
        self._freerun_i = 0
        self._next = 1            # next trigger ordinal to consider
        #: Epoch of the pulse train `_next` is counted in, so a camera that is
        #: already armed when the board starts a NEW train re-anchors to it.
        self._train_epoch_v: float | None = None
        self._consumed = 0        # block IDs consumed since StartGrabbing
        self._delivered = 0       # frames handed over since StartGrabbing
        #: Results handed over since the camera was opened, failed grabs
        #: included. Not reset by a re-arm, because a link that has died stays
        #: dead across one.
        self.handed = 0
        #: Virtual time of the last trigger this camera ACQUIRED. Kept across
        #: a re-arm because the sensor's readout is not restarted by one.
        self._last_acq_v = -1e18
        self.starts = 0
        #: RULE: `succeeded` counts only buffers handed over with
        #: GrabSucceeded() True, and a failed grab is counted once, under
        #: `failed`. REASON: `stream_stats` reports Total = succeeded + failed
        #: + underrun the way a real camera does — one count per buffer — and
        #: counting a failed grab in both halves would inflate
        #: Total_Buffer_Count past the number of buffers the camera produced.
        self.stats = {"succeeded": 0, "failed": 0, "underrun": 0,
                      "ignored": 0, "stalled": 0}

    # ---------------------------------------------------- CameraHandleProtocol
    def StartGrabbing(self, strategy=None) -> None:
        """Arm the stream, restarting the block-ID counter.

        The counter restart is deliberate: GigE Vision does it, and it is the
        reason `GrabThread` re-bases a re-armed camera from its timestamps
        rather than trusting the new ids.
        """
        if not self.is_open:
            raise RuntimeError(f"cam{self.index + 1}: StartGrabbing on a "
                               f"closed camera")
        self._grabbing = True
        self.starts += 1
        self._consumed = 0
        self._delivered = 0
        st = self.board.state()
        # Anchored to the train that is running NOW: a camera cannot deliver a
        # trigger that has already passed. The train it is anchored IN is
        # recorded with it, because `retrieve` must re-anchor if the board
        # starts another one - see `_retrigger_anchor`.
        self._next = self.board.ordinal_now(st) + 1
        self._train_epoch_v = st.epoch_v
        self._freerun_epoch_v = self.board.virtual_now()
        self._freerun_i = 0

    def StopGrabbing(self) -> None:
        self._grabbing = False

    def IsGrabbing(self) -> bool:
        return self._grabbing

    # ------------------------------------------------------------- internals
    def _take_buffer(self) -> int:
        with self._pool_lock:
            if not self._free:
                raise RuntimeError(
                    f"cam{self.index + 1}: the simulated buffer pool is empty; "
                    f"a grab result was not Release()d")
            return self._free.pop(0)

    def _return_buffer(self, i: int) -> None:
        with self._pool_lock:
            self._free.append(i)

    def _min_interval_v(self) -> float:
        """Shortest interval between acquisitions this camera can sustain.

        `exposure + 1/rate_limit`, the rule that caps usable exposure: a
        trigger arriving inside it finds the camera busy and is IGNORED, with
        no frame and no block ID consumed. Modelling it here is what lets an
        over-exposed profile reproduce the one loss mode that leaves the
        recording looking perfect.
        """
        interval = (self.exposure_us or 0.0) * 1e-6
        if self.rate_limit and self.rate_limit > 0:
            interval += 1.0 / float(self.rate_limit)
        return interval

    def _retrigger_anchor(self, st) -> None:
        """Re-anchor to trigger 1 when the board has started a NEW pulse train.

        RULE: an armed camera counts triggers in the train the board is
        running now, not the one it was armed in. REASON: the application arms
        every camera BEFORE it tells the board to start, and `SimBoard.start`
        restarts ordinals at 1 - so a camera holding the ordinal it computed
        against the train that has just ended ignores every trigger of the new
        one until that stale number comes round. It is silent: the frames that
        do arrive still carry block IDs from 1 and stay contiguous, so a
        recording short at the front looks perfect.

        Trigger 1, not `ordinal_now() + 1`: the camera was already armed when
        this train began, so none of its triggers passed unwatched. A camera
        armed mid-train keeps the anchor `StartGrabbing` gave it.
        """
        if self._train_epoch_v is None or st.epoch_v == self._train_epoch_v:
            return
        self._train_epoch_v = st.epoch_v
        self._next = 1

    def _ignores(self, i: int, tv: float) -> bool:
        f = self.faults
        if f.ignore_every and i % f.ignore_every == 0:
            return True
        return (tv - self._last_acq_v) < self._min_interval_v()

    def _stall_span(self, st) -> tuple:
        f = self.faults
        if not f.stall_at or f.stall_s <= 0 or st.fps <= 0:
            return (0, -1)
        return (f.stall_at, f.stall_at + int(round(f.stall_s * st.fps)) - 1)

    def _due_real(self, i: int, st) -> float:
        """When trigger `i`'s frame reaches the host, as a `perf_counter` value."""
        v = sim_board.SimBoard.trigger_v(i + max(0, self.faults.lag_frames), st)
        if st.stop_v is not None:
            # A backlogged camera drains its pool once the triggers stop, so
            # its late frames are not lost with the end of the recording.
            v = min(v, st.stop_v)
        if self.faults.jitter_s:
            v += self._rng.uniform(0.0, self.faults.jitter_s)
        return sim_board.SimBoard.real_time_of(v, st)

    def _block_id(self) -> int:
        """Next block ID: a 16-bit GVSP counter, or with `first_block_id` 0 a
        0-based counter that does not wrap (see `SimFaults.first_block_id`)."""
        raw = self.faults.blockid_start + self._consumed - 1
        if self.faults.first_block_id == 0:
            return raw - 1
        return (raw - 1) % BLOCKID_WRAP + 1

    def _make_result(self, i: int, st, ok: bool = True) -> SimGrabResult:
        self._consumed += 1
        self._delivered += 1
        self.handed += 1
        # A failed grab is already counted under "failed" by the caller; this
        # buffer must not be counted twice.
        if ok:
            self.stats["succeeded"] += 1
        bid = self._block_id()
        tv = sim_board.SimBoard.trigger_v(i, st)
        self._last_acq_v = tv
        ts_ns = int(round(tv * (1.0 + self.faults.ppm) * 1e9))
        buf_i = self._take_buffer()
        # One byte per frame, so the payload identifies itself without paying
        # for a full-frame fill on the simulated hot path.
        self._buffers[buf_i][0, 0] = bid & 0xFF
        pad_x = pad_y = 0
        if self._delivered >= self.faults.padding_from:
            pad_x, pad_y = self.faults.padding_x, self.faults.padding_y
        return SimGrabResult(
            self, buf_i, bid, ts_ns, padding_x=pad_x, padding_y=pad_y, ok=ok,
            error_code=0 if ok else 0xE1000014,
            error_description="" if ok else "simulated: buffer incompletely grabbed",
            blockid_raises=self.faults.blockid_raises)

    # --------------------------------------------------------------- freerun
    def _retrieve_freerun(self, deadline: float) -> SimGrabResult:
        st = self.board.state()
        self._freerun_i += 1
        v = self._freerun_epoch_v + self._freerun_i / self._freerun_fps
        due = st.t0 + v / st.speed
        if due > deadline:
            self._freerun_i -= 1
            _sleep_until(deadline)
            raise SimTimeout(f"cam{self.index + 1}: no free-run frame in time")
        _sleep_until(due)
        self._consumed += 1
        self._delivered += 1
        self.handed += 1
        self.stats["succeeded"] += 1
        bid = self._block_id()
        buf_i = self._take_buffer()
        self._buffers[buf_i][0, 0] = bid & 0xFF
        return SimGrabResult(self, buf_i, bid,
                             int(round(v * (1.0 + self.faults.ppm) * 1e9)))

    # -------------------------------------------------------------- retrieve
    def retrieve(self, timeout_ms: int) -> SimGrabResult:
        """Block until the next frame is due, or raise `SimTimeout`.

        Never returns on demand: a frame exists only once its trigger has
        fired, which is what makes the trigger/stop protocol testable at all.
        """
        deadline = time.perf_counter() + max(0, int(timeout_ms)) / 1000.0
        if not self._grabbing:
            _sleep_until(deadline)
            raise SimTimeout(f"cam{self.index + 1}: not grabbing")
        f = self.faults
        if f.fail_after and self.handed >= f.fail_after:
            raise RuntimeError(
                f"cam{self.index + 1}: simulated transport failure")
        if self._freerun_fps > 0:
            return self._retrieve_freerun(deadline)
        while True:
            st = self.board.state()
            self._retrigger_anchor(st)
            i = self._next
            if st.fps <= 0 or self.board.exhausted(i, st):
                # The triggers have stopped, so nothing is coming: wait out the
                # caller's budget and time out, which is how the grab loop is
                # told a recording has ended.
                _sleep_until(deadline)
                raise SimTimeout(
                    f"cam{self.index + 1}: no trigger due (board stopped)")
            due = self._due_real(i, st)
            if due > deadline:
                _sleep_until(deadline)
                raise SimTimeout(f"cam{self.index + 1}: no frame in "
                                 f"{timeout_ms} ms")
            _sleep_until(due)
            tv = sim_board.SimBoard.trigger_v(i, st)
            stall_lo, stall_hi = self._stall_span(st)
            self._next = i + 1
            if f.dead_after and i >= f.dead_after:
                continue                       # dead link: silence, no counters
            if stall_lo <= i <= stall_hi:
                # A wedged stream: the frames are gone and their ids with them,
                # so the host sees a gap until the loop re-arms.
                self._consumed += 1
                self.stats["stalled"] += 1
                self.stats["failed"] += 1
                continue
            if not self.board.fired(i):
                continue                       # the board skipped this pulse
            if self._ignores(i, tv):
                # No acquisition, so no block ID is consumed: the ids stay
                # contiguous while this camera falls behind the trigger.
                self.stats["ignored"] += 1
                continue
            if i in f.drop_triggers or (f.drop_every and i % f.drop_every == 0):
                self._consumed += 1
                self._last_acq_v = tv
                self.stats["failed"] += 1
                continue
            if f.underrun_every and i % f.underrun_every == 0:
                self._consumed += 1
                self._last_acq_v = tv
                self.stats["underrun"] += 1
                continue
            ok = not (f.failed_grab_every
                      and (self._delivered + 1) % f.failed_grab_every == 0)
            if not ok:
                self.stats["failed"] += 1
            return self._make_result(i, st, ok=ok)

    def close(self) -> None:
        """Close the camera and drop its buffer pool.

        RULE: release the frame buffers here, not at garbage-collection time.
        REASON: one application holds ONE backend for the life of the process
        while `open_all` runs again on every profile switch and preview
        restart, so a pool kept alive by a closed camera is stranded for the
        whole session — n_cams x BUFFER_POOL x H x W bytes per cycle.
        """
        self._grabbing = False
        self.is_open = False
        self._buffers = []
        self._free = []


class SimBackend:
    """`CameraBackend` over the simulated rig. No SDK, no hardware, no I/O."""

    name = "sim"

    #: A timeout is normal here for the same reason it is on the rig: the
    #: triggers stopped. The grab loop distinguishes it from a real failure,
    #: so the simulated backend raises this and nothing else.
    TimeoutException = SimTimeout

    #: Oldest-first, like the real strategy: a camera that falls behind is
    #: expected to deliver stale frames rather than to skip ahead.
    GRAB_STRATEGY = "sim-oldest-first"

    def __init__(self, n_cameras: int | None = None, width: int | None = None,
                 height: int | None = None, faults: dict | None = None,
                 board: sim_board.SimBoard | None = None):
        # The profile decides the rig's shape; an explicit argument is a test
        # asking for a rig of its own. The profile is read only when something
        # is left to it, so a test that states its whole geometry does not
        # depend on the file at all.
        if None in (n_cameras, width, height):
            prof_n, prof_w, prof_h = rig_shape()
        else:
            prof_n, prof_w, prof_h = n_cameras, width, height
        self.n_cameras = prof_n if n_cameras is None else int(n_cameras)
        self.width = prof_w if width is None else int(width)
        self.height = prof_h if height is None else int(height)
        self.faults = dict(FAULTS if faults is None else faults)
        self._board = board
        self.cameras: list = []

    @property
    def board(self) -> sim_board.SimBoard:
        """The trigger clock. Shared with `SimSerial` unless one was passed in,
        because the cameras and the board are wired together on the rig."""
        return self._board or sim_board.shared_board()

    def faults_for(self, index: int) -> SimFaults:
        return self.faults.get(index) or SimFaults()

    # ------------------------------------------------------------- cold path
    def enumerate_devices(self) -> list:
        """The simulated cameras, sorted by serial number as the contract
        requires: camera names are positional over this order."""
        return [SimDevice(i, f"SIM{i + 1:05d}") for i in range(self.n_cameras)]

    def open(self, device, pfs_path: str = "", max_num_buffer: int = 0,
             camera_spec=None):
        """Open one camera. `pfs_path` is accepted and ignored: the simulated
        camera has no feature file, and the caller must not have to know.

        `max_num_buffer` is recorded rather than allocated. The real pool
        absorbs jitter; here the grab loop holds one result at a time, so a
        600-deep pool would allocate gigabytes to hide the leak a four-deep
        one names immediately.

        `camera_spec` (the profile's `camera:` block) is accepted, because
        the sim profile may carry one to exercise the parser, and recorded on
        the camera as `camera_spec` without being applied.
        """
        cam = SimCamera(device.index, device.GetSerialNumber(), self.width,
                        self.height, self.faults_for(device.index),
                        self.board, max_num_buffer)
        cam.camera_spec = camera_spec
        self.cameras.append(cam)
        return cam

    def describe(self, cam) -> dict:
        return {"width": cam.width, "height": cam.height,
                "pixel_format": "Mono8", "serial": cam.serial}

    def set_freerun(self, cam, fps: float = 30.0) -> None:
        """Untriggered preview: frames arrive at `fps` with no board running."""
        cam._freerun_fps = float(fps)
        cam._freerun_epoch_v = cam.board.virtual_now()
        cam._freerun_i = 0

    def set_triggered(self, cam, rate_limit: float = 165.0,
                      announce: bool = False) -> None:
        """Hardware-trigger mode: frames come only while the board runs."""
        cam._freerun_fps = 0.0
        cam.rate_limit = float(rate_limit)
        if announce:
            print(f"[sim] trigger mode, rate limit {rate_limit:g}", flush=True)

    def get_exposure_gain(self, cam) -> tuple:
        return cam.exposure_us, cam.gain_db

    #: Unit of the simulated camera's gain control, which models a `Gain`
    #: node in dB. Not published as the optional `gain_unit` member, so the
    #: exposure log lines keep the bare gain value the sim has always
    #: printed.
    GAIN_UNIT = "dB"

    def set_exposure_gain(self, cam, exposure_us=None, gain_db=None,
                          gain_unit=None) -> tuple:
        """Apply exposure/gain and report what took.

        Nothing is clamped here: the ceiling is the caller's job, because a
        camera does not error on an exposure it cannot sustain. It ignores
        triggers instead, which is what this camera then does.

        `gain_unit` follows the contract: None (the node's own unit) and "dB"
        are written; "raw" is refused with ValueError, because this camera's
        gain is in dB; anything else is refused before any write. The
        exposure is applied before the gain's unit is checked, as on a real
        camera.
        """
        if gain_unit not in (None, "dB", "raw"):
            raise ValueError(f"gain_unit must be None, 'dB' or 'raw', "
                             f"not {gain_unit!r}")
        if exposure_us is not None:
            cam.exposure_us = float(exposure_us)
        if gain_db is not None:
            if gain_unit is not None and gain_unit != self.GAIN_UNIT:
                raise ValueError(
                    f"gain value {gain_db!r} is in {gain_unit} but the "
                    f"simulated camera's gain takes {self.GAIN_UNIT}")
            cam.gain_db = float(gain_db)
        return cam.exposure_us, cam.gain_db

    @staticmethod
    def exposure_ceiling_us(cam, fps: float, rate_limit: float) -> float:
        """Longest exposure, in us, at which the simulated camera acquires
        every trigger: `1e6/fps - 1e6/rate_limit`, or `1e6/fps` with the
        limiter disabled (`rate_limit <= 0`).

        The simulated camera ignores a trigger that arrives inside
        `exposure + 1/rate_limit` of the last one it acquired (see
        `SimCamera._min_interval_v`), the Basler limiter rule, so its ceiling
        is the Basler formula. The value is raw; the caller applies its 0.9
        margin, and a value at or below 0 is returned as is.
        """
        fps = float(fps)
        limit = float(rate_limit or 0.0)
        if limit > 0:
            return 1e6 / fps - 1e6 / limit
        return 1e6 / fps

    def enable_extended_block_ids(self, i: int, cam) -> bool:
        """Report whether 64-bit ids were negotiated. False by default, so the
        16-bit wrap stays in play and the software unwrap keeps being tested."""
        return bool(cam.extended_ids)

    def select_gige_driver(self, i: int, cam, which: str = "socket") -> None:
        cam.gige_driver = which

    # -------------------------------------------------------------- grabbing
    def start_grabbing(self, cam) -> None:
        cam.StartGrabbing(self.GRAB_STRATEGY)

    def stop_grabbing(self, cam) -> None:
        cam.StopGrabbing()

    def is_grabbing(self, cam) -> bool:
        return cam.IsGrabbing()

    def retrieve(self, cam, timeout_ms: int):
        return cam.retrieve(timeout_ms)

    def close(self, cam) -> None:
        """Close one camera and forget it.

        RULE: drop the camera from `self.cameras`. REASON: the backend
        outlives every camera it opens (the manager reloads one only when the
        backend NAME changes), so a list that only ever grows keeps every
        camera of every past session — and its buffers — alive.
        """
        cam.close()
        if cam in self.cameras:
            self.cameras.remove(cam)

    # ----------------------------------------------------------- diagnostics
    def thermals(self, cam) -> dict:
        """Temperature reading in the keys the thermal watch reads."""
        if cam.faults.thermals is not None:
            return dict(cam.faults.thermals)
        return {"temp_c": 55.0 + cam.index, "temp_max_c": 60.0 + cam.index,
                "temp_status": "Ok", "temp_critical_c": 76.0,
                "temp_shutdown_c": 81.0}

    def stream_stats(self, cam) -> dict:
        """Counters in the shape the log expects, separating host starvation
        (Buffer_Underrun) from transmission loss (Failed_Buffer).

        `Total_Buffer_Count` counts each buffer ONCE, as a real camera does:
        it is succeeded + failed + underrun, and a buffer that arrived with
        GrabSucceeded() False is in the failed half only. Counting it in both
        would report more buffers than the camera produced, and a test
        asserting on the loss rate would read it as loss that did not happen.
        """
        if cam.faults.stats_error:
            return {"error": cam.faults.stats_error}
        s = cam.stats
        total = s["succeeded"] + s["failed"] + s["underrun"]
        return {"Total_Buffer_Count": total,
                "Failed_Buffer_Count": s["failed"],
                "Buffer_Underrun_Count": s["underrun"],
                "Resend_Request_Count": s["failed"] * 3,
                "Ignored_Trigger_Count": s["ignored"]}
