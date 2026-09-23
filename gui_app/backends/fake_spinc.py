"""FakeSpinC: the `SpinC` method surface over simulated FLIR cameras.

`FakeSpinC` has every public method of `_spinc.SpinC` with the same
signature, so `flir.py` runs against it unchanged. Behind the methods are
simulated cameras with GenICam-style nodemaps, paced by the same virtual
trigger clock as the simulated Basler rig (`sim_board.SimBoard`), so the
trigger, arm and stop protocol runs for real: a triggered camera delivers a
frame only when the board fires a pulse on the line the camera is wired to,
and `next_image` times out once the board stops.

Nothing here loads a DLL, opens a device or imports a vendor SDK.
`load_backend("flir_sim")` pairs it with `FlirBackend`, and the tests use it
to reach every branch of the backend without FLIR hardware.

What the fake models, and what is a guess:
The node names, entries and rules follow SFNC and the Blackfly S reference as
FLIR_DESIGN.md sections 3 and 5 describe them. Real cameras differ per model
and firmware, and several behaviours are unknown until a volunteer's probe
reports them. The fake picks one answer for each and makes the others
reachable through `FlirFaults`:
- whether the frame ID restarts on `begin`, and from 0 or 1
  (`id_restart_on_begin`, `id_base`);
- the timestamp unit and whether it resets (`ts_unit`, `ts_reset_on_begin`);
- whether `end` resets the stream counters (`counters_reset_on_end`);
- whether `ExposureTime`'s maximum follows `AcquisitionFrameRate`
  (`ceiling_follows_frame_rate`);
- which counter and chunk nodes exist (`absent_nodes`, `enum_entries`);
- the chunk-data name spelling: `chunk_int` accepts "FrameID" and
  "ChunkFrameID" alike and records the spelling used in
  `chunk_names_used`, so the probe can settle which one the SDK takes.
Error codes the SDK documents are used as documented. Where the SDK's code
for a refusal is unknown, the fake uses the nearest documented code, and the
behaviour (a refusal) is what a test should assert on, not the number.

Time:
Device timestamps and trigger ordinals are virtual, as in `sim.py`: raising
`SimBoard.speed` compresses a run while the cameras still report the profile
rate. A check that compares device time with host time therefore holds only
at `speed` 1. Compare device time with the programmed rate instead.

Misuse:
Some mistakes have undefined results on hardware: a crash, or a view that
reads a buffer the driver has refilled. The fake raises `FakeMisuse` for each
and appends the message to `FakeSpinC.violations`:
- releasing an image twice, or using an image after releasing it;
- `end` (or `deinit`) while an image is still held;
- a zero-copy view that is still referenced when its buffer comes round
  again, which is a consumer that stored the view instead of copying it;
- a view taken with the wrong shape, over a pixel format wider than 8 bits,
  over padded rows, or with a stride that is not the width;
- holding every buffer of the pool at once.
The fake also raises `FakeMisuse` for a board sequence it cannot replay.
`SimBoard` keeps only the pulse train it runs now, so a streaming camera and
a trigger-line counter must each look at the board between one train and
the next (a `next_image` or `image_release` call, or a `CounterValue`
read). Without that look, the pulses of the train in between are unknown.

Host pool:
`StreamBufferCountManual` buffers hold the frames waiting for `next_image`
and the images the application holds. When none is free, OldestFirst loses
the new frame (`StreamLostFrameCount`), and the other handling modes
discard queued frames (`StreamDroppedFrameCount`), as SFNC defines them.
"""
from __future__ import annotations

import math
import os
import random
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from gui_app.backends import sim_board
from gui_app.backends._spinc import (
    IMAGE_STATUS_MISSING_PACKETS, IMAGE_STATUS_NAMES,
    SPINNAKER_ERR_ACCESS_DENIED, SPINNAKER_ERR_BUSY,
    SPINNAKER_ERR_GENICAM_DYNAMIC_CAST, SPINNAKER_ERR_GENICAM_OUT_OF_RANGE,
    SPINNAKER_ERR_INVALID_HANDLE, SPINNAKER_ERR_NOT_AVAILABLE,
    SPINNAKER_ERR_NOT_INITIALIZED, FlirError, FlirSdkUnavailable,
    FlirTimeout, _as_int)

#: The profile a `FakeSpinC()` built with no shape arguments reads the
#: simulated rig's camera count, frame size and serials from, the way
#: `sim.py` reads `profiles/sim.yaml`: the profile is what `camera_manager`
#: compares the cameras against, so the fake takes its shape from the same
#: file instead of from a constant that could disagree with it.
FLIR_SIM_PROFILE_PATH = (Path(__file__).resolve().parents[2] / "profiles"
                         / "templates" / "flir_sim.yaml")

#: Per-camera faults for a `FakeSpinC()` built without `faults=`, keyed by
#: serial (or camera index). Module state because `load_backend` takes only a
#: name; set it before the backend is built.
FAULTS: dict = {}

#: Shape overrides installed by `configure()`.
_SHAPE_OVERRIDES: dict = {}

#: Buffers that really exist per camera. The grab loop holds one image at a
#: time, and a small pool turns a leaked image into a named failure at once.
#: `StreamBufferCountManual` is modelled separately, as the host pool: the
#: frames queued for `next_image` plus the images the application holds.
PHYSICAL_BUFFERS = 4

#: Host queue depth when `StreamBufferCountMode` is left at `Auto`.
AUTO_BUFFER_COUNT = 10

#: Longest a waiting triggered camera goes without re-reading the board, in
#: real seconds. A start or stop is noticed within this.
POLL_S = 0.002

#: Upper bound of `ExposureTime` when no frame rate bounds it, in us.
EXPOSURE_MAX_US = 30_000_000.0

#: Link figures per interface, in bytes per second.
LINK = {"USB3": {"speed": 500_000_000, "throughput_max": 380_000_000},
        "GigE": {"speed": 125_000_000, "throughput_max": 125_000_000}}
INTERFACES = tuple(LINK)

#: Bits per pixel of each offered pixel format.
PIXEL_BITS = {"Mono8": 8, "Mono10Packed": 10, "Mono12Packed": 12,
              "Mono16": 16}

#: First generated serial. Only used when neither the caller nor the profile
#: names serials.
SERIAL_BASE = 90000001

#: The version `library_version()` reports.
FAKE_LIBRARY_VERSION = "4.4.0.0"

CHUNK_ENTRIES = ("Image", "CRC", "FrameID", "OffsetX", "OffsetY", "Width",
                 "Height", "ExposureTime", "Gain", "BlackLevel", "PixelFormat",
                 "Timestamp", "CounterValue")
#: Chunk entries whose value `chunk_int` can return.
CHUNK_INT_VALUES = ("FrameID", "Timestamp", "CounterValue", "OffsetX",
                    "OffsetY", "Width", "Height")
COUNTERS = ("Counter0", "Counter1")

#: Default GPIO lines: (name, default mode, format, mode writable). Line0 is
#: the opto-isolated input and Line1 the opto-isolated output; Line2 and Line3
#: are bidirectional, as on a Blackfly S.
DEFAULT_LINES = (("Line0", "Input", "OptoCoupled", False),
                 ("Line1", "Output", "OptoCoupled", False),
                 ("Line2", "Input", "TTL", True),
                 ("Line3", "Input", "TTL", True))

# Handles are 64-bit values above 2**32, as on a 64-bit SDK, so a caller that
# truncates a handle breaks here too. Images get a range of their own so a
# stale image handle can be told from a foreign one.
_HANDLE_BASE = 0x7F00_0000_0001_0000
_IMAGE_BASE = 0x7E00_0000_0000_0000

_STAT_NODES = {
    "StreamStartedFrameCount": "started",
    "StreamDeliveredFrameCount": "delivered",
    "StreamReceivedFrameCount": "received",
    "StreamIncompleteFrameCount": "incomplete",
    "StreamLostFrameCount": "lost",
    "StreamDroppedFrameCount": "dropped",
}
_GIGE_STAT_NODES = {
    "StreamMissedPacketCount": "missed_packets",
    "StreamPacketResendRequestCount": "resend_requests",
    "StreamPacketResendRequestedPacketCount": "resend_requested_packets",
    "StreamPacketResendReceivedPacketCount": "resend_received_packets",
}
_STAT_KEYS = tuple(_STAT_NODES.values()) + tuple(_GIGE_STAT_NODES.values())


class FakeMisuse(RuntimeError):
    """A use of the API whose result on hardware is undefined (see the module
    docstring). Raised so a test fails at the mistake, not at a later
    symptom."""


@dataclass
class FlirFaults:
    """What one simulated camera does differently. Every default is a healthy
    camera, and every fault is expressed the way the hardware produces it.

    Knobs that shape the node model (`interface`, `lines`, `temperatures`,
    `absent_nodes`, `read_only_nodes`, `node_ranges`, `enum_entries`,
    `initial_values`, `user_sets`, `stream_buffer_count_max`,
    `link_throughput_max`, `link_speed`) are read when the camera's nodemaps
    are first used, which is at enumeration. The rest are read on every
    frame.
    """
    # ---------------------------------------------------------- transport
    #: "USB3" or "GigE"; None takes the `FakeSpinC` default.
    interface: str | None = None
    # --------------------------------------------------------------- clock
    #: Device-clock rate error against the trigger board, as a fraction.
    ppm: float = 0.0
    #: Upper bound on per-frame delivery delay, in virtual seconds.
    jitter_s: float = 0.0
    #: Constant delivery backlog, in triggers.
    lag_frames: int = 0
    # ----------------------------------------------------- trigger acceptance
    #: Every n-th trigger is not acquired: no frame and no frame ID, while
    #: the trigger-line counter still counts the edge. 0 disables.
    ignore_every: int = 0
    #: Readout time, in us, that a trigger must not land in while
    #: `TriggerOverlap` is `Off`; such a trigger is ignored as above. 0 means
    #: overlap `Off` costs nothing.
    overlap_off_readout_us: float = 0.0
    #: Frame time on top of the exposure, in us. It sets both the exposure
    #: ceiling at a frame rate (`1e6 / fps - overhead`) and the shortest
    #: trigger interval the camera accepts with overlap `ReadOut`.
    exposure_overhead_us: float = 80.0
    #: Whether `ExposureTime`'s maximum drops to the frame time while
    #: `AcquisitionFrameRateEnable` is on. False models a camera whose node
    #: maximum does not move, so a ceiling read from it is not the real one.
    ceiling_follows_frame_rate: bool = True
    #: The line the trigger wire is on. A camera whose `TriggerSource` names
    #: another line never sees a pulse.
    wired_line: str = "Line3"
    # ------------------------------------------------------- transport loss
    #: Trigger ordinals whose frame is lost in transport (frame ID consumed,
    #: counted in `StreamIncompleteFrameCount`, nothing delivered).
    drop_triggers: frozenset = frozenset()
    #: Every n-th trigger lost the same way; 0 disables.
    drop_every: int = 0
    #: Every n-th acquired frame is delivered with `image_incomplete` True
    #: and status 3 (`MISSING_PACKETS`); 0 disables.
    incomplete_every: int = 0
    #: Every n-th trigger lost to an empty host pool (frame ID consumed,
    #: counted in `StreamLostFrameCount`); 0 disables.
    underrun_every: int = 0
    # --------------------------------------------------------- stream health
    #: First trigger of a stream stall and its length in virtual seconds.
    #: The stalled frames are lost with their IDs. 0 disables.
    stall_at: int = 0
    stall_s: float = 0.0
    #: Trigger from which nothing is ever delivered; 0 disables.
    dead_after: int = 0
    # ------------------------------------------------------------- frame ID
    #: Whether the frame-ID counter restarts on every `begin`.
    id_restart_on_begin: bool = True
    #: The first frame ID after a restart: 1 or 0.
    id_base: int = 1
    #: Added to every frame ID, to put a wrap a few frames into a test.
    id_offset: int = 0
    #: "auto" (16-bit and 65535 -> 1 on GigE without extended IDs, 64-bit
    #: otherwise), "65535->1", "65535->0" or "none".
    id_wrap: str = "auto"
    # ------------------------------------------------------------ timestamp
    #: "ns", or "ticks_125MHz" for a clock reported in raw 8 ns ticks.
    ts_unit: str = "ns"
    #: `image_timestamp` returns 0.
    ts_zero: bool = False
    #: The `Timestamp` chunk carries 0 as well.
    chunk_ts_zero: bool = False
    #: The device clock restarts from 0 on every `begin`.
    ts_reset_on_begin: bool = False
    # --------------------------------------------------------- image layout
    padding_x: int = 0
    padding_y: int = 0
    #: Bytes added to every row's stride.
    stride_extra: int = 0
    #: Bits per pixel reported whatever the pixel format; None follows
    #: `PixelFormat`.
    bpp: int | None = None
    # ----------------------------------------------------------- node model
    #: GPIO lines as (name, default mode, format, mode writable).
    lines: tuple = DEFAULT_LINES
    #: `DeviceTemperatureSelector` entry -> temperature in degrees C.
    temperatures: dict = field(default_factory=lambda: {"Sensor": 45.0})
    #: Nodes this camera does not have.
    absent_nodes: frozenset = frozenset()
    #: Nodes this camera reports read-only.
    read_only_nodes: frozenset = frozenset()
    #: Node -> (min, max, inc); a None element keeps the modelled bound.
    node_ranges: dict = field(default_factory=dict)
    #: Enumeration node -> the entries it offers.
    enum_entries: dict = field(default_factory=dict)
    #: Node values the camera holds at power-up in place of its factory
    #: values, as another program may have left them. Keys are node names,
    #: or (name, selector value) for a selector-dependent node.
    initial_values: dict = field(default_factory=dict)
    #: User set name -> node values that `UserSetLoad` applies on top of the
    #: factory values. `Default` always loads the factory values.
    user_sets: dict = field(default_factory=dict)
    #: `StreamBufferCountManual` maximum, reported as `StreamBufferCountMax`.
    stream_buffer_count_max: int = 1000
    #: `DeviceLinkThroughputLimit` maximum in bytes/s; None per interface.
    link_throughput_max: int | None = None
    #: `DeviceLinkSpeed` in bytes/s; None per interface.
    link_speed: int | None = None
    #: `CounterValue` maximum; the counter wraps to 0 after it.
    counter_max: int = 2 ** 32 - 1
    #: Whether `end` resets the stream counters.
    counters_reset_on_end: bool = False
    # --------------------------------------------------------------- errors
    #: Spinnaker error code `init` raises, for example -1004 when another
    #: program holds the camera; 0 disables.
    init_error: int = 0
    #: Error code `begin` raises; 0 disables.
    begin_error: int = 0
    #: Error code `next_image` raises once `next_image_error_after` images
    #: have been delivered; 0 disables.
    next_image_error: int = 0
    next_image_error_after: int = 0
    #: `next_image` waits while holding the GIL, so a GIL meter can be shown
    #: to catch it.
    gil_hold_wait: bool = False


_HEALTHY = FlirFaults()


def set_faults(faults: dict | None) -> dict:
    """Install the module fault map (serial or index -> FlirFaults) and
    return the previous one, so a caller can restore it."""
    global FAULTS
    prev, FAULTS = FAULTS, dict(faults or {})
    return prev


def configure(n_cameras: int | None = None, width: int | None = None,
              height: int | None = None, serials=None,
              interface: str | None = None) -> None:
    """Override the profile's rig shape for a `FakeSpinC()` built with no
    arguments. A None leaves that value to the profile, so calling this with
    no arguments restores the profile in full."""
    global _SHAPE_OVERRIDES
    _SHAPE_OVERRIDES = {
        key: value for key, value in (
            ("n_cameras", n_cameras), ("width", width), ("height", height),
            ("serials", None if serials is None else [str(s) for s in serials]),
            ("interface", interface))
        if value is not None}


def profile_shape(path=None) -> tuple:
    """`(n_cameras, width, height, serials)` as a profile declares them.

    Loaded through `RigProfile`, so the fake agrees with the object
    `camera_manager` checks the cameras against. `serials` is a list, empty
    when the profile names none.
    """
    from gui_app.session_config import RigProfile
    p = Path(path) if path is not None else FLIR_SIM_PROFILE_PATH
    if not p.is_file():
        raise FileNotFoundError(
            f"{p} does not exist. FakeSpinC() takes the simulated rig's "
            f"camera count and frame size from that profile; pass n_cameras, "
            f"width and height instead, or create the profile.")
    prof = RigProfile.load(p)
    serials = [str(s) for s in (prof.camera_serials or [])]
    n = prof.n_cameras or len(serials)
    return n, prof.frame_width, prof.frame_height, serials


def _sleep_until(deadline: float) -> None:
    """Block until `deadline`, a `perf_counter` value, against the absolute
    deadline so sleep overshoot does not accumulate."""
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        time.sleep(remaining)


_GIL_SLEEP = None


def _gil_holding_sleep_until(deadline: float) -> None:
    """Block until `deadline` without releasing the GIL.

    A foreign call made through `ctypes.PyDLL` keeps the GIL for its whole
    duration, which is the behaviour the `gil_hold_wait` knob exists to show.
    The library (kernel32, or the C library) is already loaded in every
    process, so this loads nothing new.
    """
    global _GIL_SLEEP
    if _GIL_SLEEP is None:
        import ctypes
        if os.name == "nt":
            fn = ctypes.PyDLL("kernel32").Sleep
            fn.argtypes = [ctypes.c_uint32]
            fn.restype = None
            _GIL_SLEEP = lambda s: fn(max(1, int(s * 1000)))
        else:
            fn = ctypes.PyDLL(None).usleep
            fn.argtypes = [ctypes.c_uint32]
            fn.restype = ctypes.c_int
            _GIL_SLEEP = lambda s: fn(max(1, int(s * 1e6)))
    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            return
        _GIL_SLEEP(remaining)


def _pulses(board, st, from_v: float, to_v: float) -> int:
    """Pulses of the train `st` fired after `from_v` and at or before `to_v`
    (virtual seconds), leaving out those the board skips."""
    if st.fps <= 0:
        return 0
    end = to_v if st.stop_v is None else min(to_v, st.stop_v)
    hi = math.floor((end - st.epoch_v) * st.fps + 1e-6)
    lo = 0
    if from_v > st.epoch_v:
        lo = math.floor((from_v - st.epoch_v) * st.fps + 1e-6)
    if hi <= lo:
        return 0
    missed = sum(1 for m in getattr(board, "miss_pulses", ()) if lo < m <= hi)
    return hi - lo - missed


def _board_snapshot(board) -> tuple:
    """`(state, starts, stops)` of the board, read as one consistent view.

    `SimBoard.state()` does not carry the start and stop counts. They are
    read on both sides of it, and the read repeats if a start or stop lands
    in between.
    """
    while True:
        before = (board.starts, board.stops)
        st = board.state()
        if (board.starts, board.stops) == before:
            return st, before[0], before[1]


class _Def:
    """One node of a simulated nodemap.

    `lo`, `hi` and `default` may be callables of the camera, for ranges that
    depend on other nodes. `get` makes the value computed rather than
    stored. `locked` nodes are not writable while the camera streams, and
    `writable_if(camera, selector_value)` adds a condition of its own.
    """

    __slots__ = ("name", "kind", "default", "entries", "lo", "hi", "inc",
                 "selector", "ro", "locked", "writable_if", "get", "on_set",
                 "on_exec", "nodemap")

    def __init__(self, name, kind, default=None, *, entries=(), lo=None,
                 hi=None, inc=None, selector=None, ro=False, locked=False,
                 writable_if=None, get=None, on_set=None, on_exec=None):
        self.name = name
        self.kind = kind
        self.default = default
        self.entries = tuple(entries)
        self.lo = lo
        self.hi = hi
        self.inc = inc
        self.selector = selector
        self.ro = ro
        self.locked = locked
        self.writable_if = writable_if
        self.get = get
        self.on_set = on_set
        self.on_exec = on_exec
        self.nodemap = ""


class _Counter:
    """One `CounterSelector` entry's state.

    `acc` holds the edges of earlier pulse trains since the reset, and
    `last_st` with `seen` (the board's start and stop counts) is the board
    as the counter last saw it. `prev_st` and `prev_acc` keep the train
    before the current one, so a frame of that train that is walked late
    still reads the count its trigger saw.
    """

    __slots__ = ("reset_v", "acc", "base", "epoch", "last_st", "seen",
                 "prev_st", "prev_acc", "acq_at_reset", "value_at_reset")

    def __init__(self):
        self.reset_v = -math.inf
        self.acc = 0
        self.base = 0
        self.epoch = None
        self.last_st = None
        self.seen = None
        self.prev_st = None
        self.prev_acc = 0
        self.acq_at_reset = 0
        self.value_at_reset = 0


class _FakeImage:
    __slots__ = ("cam", "buf", "frame_id", "timestamp", "chunk", "incomplete",
                 "status", "width", "height", "bpp", "stride", "padding")

    def __init__(self, cam, buf, frame_id, timestamp, chunk, incomplete,
                 width, height, bpp, stride, padding):
        self.cam = cam
        self.buf = buf
        self.frame_id = frame_id
        self.timestamp = timestamp
        self.chunk = chunk
        self.incomplete = incomplete
        self.status = IMAGE_STATUS_MISSING_PACKETS if incomplete else 0
        self.width = width
        self.height = height
        self.bpp = bpp
        self.stride = stride
        self.padding = padding


class _FakeCamera:
    """One simulated camera: nodemaps, clock, counters and acquisition.

    Test code reaches it through `FakeSpinC.fake_camera()`. The attributes
    below the "introspection" line are what a test asserts on.
    """

    def __init__(self, api, index: int, serial: str, width: int, height: int,
                 sensor: tuple):
        self.api = api
        self.index = index
        self.serial = serial
        self._shape = (int(width), int(height))
        self._sensor = sensor
        self.lock = threading.RLock()
        self.initialized = False
        self.streaming = False
        self.defs: dict = {}
        self._flat: dict = {}
        self.values: dict = {}
        self.factory: dict = {}
        self._counters: dict = {}
        self._rng = random.Random(2000 + index)
        #: Device uptime at virtual time 0, in seconds. Differs per camera, as
        #: real cameras booted at different moments.
        self._ts_origin_s = 1000.0 + 97.0 * index
        self._fid_count = 0
        self._last_acq_v = -math.inf
        self._buffers: list = []
        self._addr: list = []
        #: Each buffer's reference count with no view of it alive.
        self._buf_refs: list = []
        self._free: deque = deque()
        self._held: set = set()
        #: Frames on the host waiting for `next_image`, oldest first.
        self._queue: deque = deque()
        self._acq = None
        self._latch = 0
        # ------------------------------------------------- introspection
        #: (name, selector value, value) for every write and command, in
        #: order, so a test can check what the backend set and when.
        self.write_log: list = []
        self.inits = 0
        self.begins = 0
        self.ends = 0
        self.user_set_loads: list = []
        self.timestamp_resets = 0
        self.software_triggers = 0
        #: Triggers fired on the wired line that the camera did not acquire.
        self.ignored = 0
        #: Frames acquired (exposed), whether or not they were delivered.
        self.acquired = 0
        #: Images handed to the caller.
        self.handed = 0
        #: Spellings `chunk_int` was called with.
        self.chunk_names_used: set = set()
        self.stats = dict.fromkeys(_STAT_KEYS, 0)

    def __repr__(self) -> str:
        return f"<FakeCamera {self.serial} cam{self.index + 1}>"

    # ------------------------------------------------------------- plumbing
    @property
    def faults(self) -> FlirFaults:
        return self.api._faults_for(self)

    @property
    def board(self):
        return self.api.board

    @property
    def interface(self) -> str:
        return self.faults.interface or self.api.interface

    def ensure_built(self) -> None:
        with self.lock:
            if not self.defs:
                self._build()

    def val(self, name: str, sel=None):
        """A node's current value by name; `sel` picks a selector entry."""
        d = self._flat[name]
        if d.selector is None:
            sel = None
        elif sel is None:
            sel = self.values[(d.selector, None)]
        if d.get is not None:
            return d.get(self, sel)
        return self.values[(name, sel)]

    def val_or(self, name: str, sel=None, default=None):
        return self.val(name, sel) if name in self._flat else default

    def _sel(self, d):
        return None if d.selector is None else self.values[(d.selector, None)]

    def read(self, d):
        sel = self._sel(d)
        if d.get is not None:
            return d.get(self, sel)
        return self.values[(d.name, sel)]

    def bounds(self, d) -> tuple:
        lo = d.lo(self) if callable(d.lo) else d.lo
        hi = d.hi(self) if callable(d.hi) else d.hi
        inc = d.inc
        if d.kind == "int":
            if lo is None or hi is None:
                v = self.read(d)
                lo = v if lo is None else lo
                hi = v if hi is None else hi
            inc = inc or 1
            lo, hi = int(lo), int(hi)
            hi = lo + max(0, (hi - lo) // inc) * inc
        return lo, hi, inc

    def writable(self, d) -> bool:
        if d.ro or d.kind == "string":
            return False
        if d.locked and self.streaming:
            return False
        if d.writable_if is not None:
            return bool(d.writable_if(self, self._sel(d)))
        return True

    def write(self, d, value) -> None:
        if not self.writable(d):
            why = ("the camera is streaming" if d.locked and self.streaming
                   else "it is read-only" if d.ro or d.kind == "string"
                   else "another setting disables it")
            raise FlirError(SPINNAKER_ERR_ACCESS_DENIED,
                            f"{d.name} is not writable now ({why})")
        if d.kind in ("int", "float"):
            lo, hi, inc = self.bounds(d)
            if not (lo <= value <= hi):
                raise FlirError(SPINNAKER_ERR_GENICAM_OUT_OF_RANGE,
                                f"{d.name} = {value} is outside "
                                f"[{lo}, {hi}]")
            if d.kind == "int" and (value - lo) % inc:
                raise FlirError(SPINNAKER_ERR_GENICAM_OUT_OF_RANGE,
                                f"{d.name} = {value} is not {lo} plus a "
                                f"multiple of the increment {inc}")
        elif d.kind == "enum" and value not in d.entries:
            raise FlirError(SPINNAKER_ERR_NOT_AVAILABLE,
                            f"{d.name}: no entry {value!r}; entries "
                            f"{list(d.entries)}")
        sel = self._sel(d)
        self.write_log.append((d.name, sel, value))
        if d.on_set is not None:
            d.on_set(self, sel, value)
        else:
            self.values[(d.name, sel)] = value

    def execute(self, d) -> None:
        if not self.writable(d):
            raise FlirError(SPINNAKER_ERR_ACCESS_DENIED,
                            f"{d.name} cannot be executed now")
        sel = self._sel(d)
        self.write_log.append((d.name, sel, "execute"))
        if d.on_exec is not None:
            d.on_exec(self, sel)

    # ----------------------------------------------------------- node model
    def _build(self) -> None:
        f = self.faults
        iface = self.interface
        if iface not in LINK:
            raise ValueError(f"{self}: interface {iface!r}; use one of "
                             f"{list(INTERFACES)}")
        gige = iface == "GigE"
        sw, sh = self._sensor
        link = LINK[iface]
        link_max = int(f.link_throughput_max or link["throughput_max"])
        link_speed = int(f.link_speed or link["speed"])
        lines = tuple(f.lines)
        line_names = tuple(ln[0] for ln in lines)
        line_mode = {ln[0]: ln[1] for ln in lines}
        line_format = {ln[0]: ln[2] for ln in lines}
        line_bidir = {ln[0]: bool(ln[3]) for ln in lines}
        inputs = tuple(ln[0] for ln in lines if ln[1] == "Input" or ln[3])
        temps = tuple(f.temperatures) or ("Sensor",)
        model = "FakeSpinC-GEV" if gige else "FakeSpinC-U3"
        ident = {"DeviceSerialNumber": self.serial,
                 "DeviceModelName": model,
                 "DeviceVendorName": "FakeSpinC (simulated)",
                 "DeviceVersion": "fake-1.0",
                 "DeviceFirmwareVersion": "fake-1.0"}
        ip = 0xC0A80A00 + 10 + self.index
        D = _Def
        def trigger_off(c, s):
            return c.values.get(("TriggerMode", s), "Off") == "Off"

        device = [D(k, "string", v, ro=True) for k, v in ident.items()] + [
            D("DeviceTemperatureSelector", "enum", temps[0], entries=temps),
            D("DeviceTemperature", "float", ro=True,
              selector="DeviceTemperatureSelector",
              get=lambda c, s: float(c.faults.temperatures.get(s, 45.0))),
            D("UserSetSelector", "enum", "Default",
              entries=("Default", "UserSet0", "UserSet1")),
            D("UserSetLoad", "command", locked=True,
              on_exec=_FakeCamera._user_set_load),
            D("AcquisitionMode", "enum", "Continuous",
              entries=("Continuous", "SingleFrame", "MultiFrame"),
              locked=True),
            D("AcquisitionFrameCount", "int", 2, lo=1, hi=65535, inc=1),
            D("AcquisitionFrameRateEnable", "bool", False),
            D("AcquisitionFrameRate", "float", 30.0, lo=1.0,
              hi=_FakeCamera._frame_rate_max,
              writable_if=lambda c, s: c.values.get(
                  ("AcquisitionFrameRateEnable", None), True)),
            D("AcquisitionResultingFrameRate", "float", ro=True,
              get=lambda c, s: c._resulting_rate()),
            D("ExposureAuto", "enum", "Continuous",
              entries=("Off", "Once", "Continuous")),
            D("ExposureMode", "enum", "Timed",
              entries=("Timed", "TriggerWidth")),
            D("ExposureTime", "float", 5000.0, lo=10.0,
              hi=_FakeCamera._exposure_max,
              writable_if=lambda c, s: (
                  c.values.get(("ExposureAuto", None), "Off") == "Off"
                  and c.values.get(("ExposureMode", None), "Timed") == "Timed")),
            D("GainAuto", "enum", "Continuous",
              entries=("Off", "Once", "Continuous")),
            D("Gain", "float", 0.0, lo=0.0, hi=47.99,
              writable_if=lambda c, s: c.values.get(("GainAuto", None),
                                                    "Off") == "Off"),
            D("PixelFormat", "enum", "Mono8", entries=tuple(PIXEL_BITS),
              locked=True),
            D("SensorWidth", "int", sw, ro=True),
            D("SensorHeight", "int", sh, ro=True),
            D("WidthMax", "int", sw, ro=True),
            D("HeightMax", "int", sh, ro=True),
            D("Width", "int", sw, lo=16, inc=16, locked=True,
              hi=lambda c: c.val("WidthMax") - c.val("OffsetX")),
            D("Height", "int", sh, lo=2, inc=2, locked=True,
              hi=lambda c: c.val("HeightMax") - c.val("OffsetY")),
            D("OffsetX", "int", 0, lo=0, inc=4,
              hi=lambda c: c.val("WidthMax") - c.val("Width")),
            D("OffsetY", "int", 0, lo=0, inc=2,
              hi=lambda c: c.val("HeightMax") - c.val("Height")),
            D("GammaEnable", "bool", True),
            D("Gamma", "float", 0.8, lo=0.25, hi=4.0,
              writable_if=lambda c, s: c.values.get(("GammaEnable", None),
                                                    True)),
            D("BlackLevel", "float", 0.0, lo=0.0, hi=10.0),
            D("AdcBitDepth", "enum", "Bit10",
              entries=("Bit8", "Bit10", "Bit12"), locked=True),
            D("TriggerSelector", "enum", "FrameStart",
              entries=("FrameStart", "AcquisitionStart", "FrameBurstStart")),
            D("TriggerMode", "enum", "Off", entries=("Off", "On"),
              selector="TriggerSelector"),
            D("TriggerSource", "enum", "Software",
              entries=("Software",) + inputs, selector="TriggerSelector",
              writable_if=trigger_off),
            D("TriggerActivation", "enum", "RisingEdge",
              entries=("RisingEdge", "FallingEdge", "AnyEdge", "LevelHigh",
                       "LevelLow"),
              selector="TriggerSelector", writable_if=trigger_off),
            D("TriggerOverlap", "enum", "Off",
              entries=("Off", "ReadOut", "PreviousFrame"),
              selector="TriggerSelector", writable_if=trigger_off),
            D("TriggerDelay", "float", 0.0, lo=0.0, hi=65520.0,
              selector="TriggerSelector"),
            D("TriggerSoftware", "command",
              on_exec=_FakeCamera._software_trigger),
            D("LineSelector", "enum", line_names[0], entries=line_names),
            D("LineMode", "enum", line_mode, entries=("Input", "Output"),
              selector="LineSelector",
              writable_if=lambda c, s: line_bidir.get(s, False)),
            D("LineFormat", "enum", line_format,
              entries=tuple(dict.fromkeys(line_format.values())),
              selector="LineSelector", ro=True),
            D("LineInverter", "bool", False, selector="LineSelector"),
            D("LineStatus", "bool", selector="LineSelector", ro=True,
              get=lambda c, s: c._line_high(s)),
            D("LineStatusAll", "int", ro=True, lo=0,
              hi=(1 << (max(_line_bit(n) for n in line_names) + 1)) - 1,
              get=lambda c, s: c._line_status_all()),
            D("CounterSelector", "enum", COUNTERS[0], entries=COUNTERS),
            D("CounterEventSource", "enum", "Off",
              entries=("Off",) + line_names + ("ExposureStart", "ExposureEnd",
                                                "FrameTriggerWait"),
              selector="CounterSelector"),
            D("CounterEventActivation", "enum", "RisingEdge",
              entries=("RisingEdge", "FallingEdge", "AnyEdge"),
              selector="CounterSelector"),
            D("CounterReset", "command", selector="CounterSelector",
              on_exec=_FakeCamera._counter_reset),
            D("CounterValue", "int", selector="CounterSelector", lo=0,
              hi=lambda c: c.faults.counter_max, inc=1,
              get=lambda c, s: c.counter_value(s),
              on_set=_FakeCamera._counter_write),
            D("CounterValueAtReset", "int", selector="CounterSelector",
              ro=True, get=lambda c, s: c._counter(s).value_at_reset),
            D("ChunkModeActive", "bool", False, locked=True),
            D("ChunkSelector", "enum", "Image", entries=CHUNK_ENTRIES),
            D("ChunkEnable", "bool", {"Image": True},
              selector="ChunkSelector", locked=True),
            D("ChunkCounterSelector", "enum", COUNTERS[0], entries=COUNTERS,
              locked=True),
            D("TimestampIncrement", "int", ro=True,
              get=lambda c, s: 8 if c.faults.ts_unit == "ticks_125MHz" else 1),
            D("TimestampLatch", "command",
              on_exec=_FakeCamera._timestamp_latch),
            D("TimestampLatchValue", "int", ro=True,
              get=lambda c, s: c._latch),
            D("TimestampReset", "command",
              on_exec=_FakeCamera._timestamp_reset),
            D("DeviceLinkThroughputLimit", "int", link_max, lo=8_000_000,
              hi=link_max, inc=8000),
            D("DeviceLinkSpeed", "int", link_speed, ro=True),
            D("DeviceMaxThroughput", "int", ro=True,
              get=lambda c, s: c.values[("DeviceLinkThroughputLimit", None)]),
            D("PayloadSize", "int", ro=True, get=lambda c, s: c._payload()),
        ]
        if gige:
            device += [
                D("GevSCPSPacketSize", "int", 1400, lo=576, hi=9000, inc=4,
                  locked=True),
                D("GevSCPD", "int", 0, lo=0, hi=65535, inc=1),
                D("GevGVSPExtendedIDMode", "enum", "Off",
                  entries=("Off", "On"), locked=True),
                D("GevTimestampTickFrequency", "int", ro=True,
                  get=lambda c, s: (125_000_000
                                    if c.faults.ts_unit == "ticks_125MHz"
                                    else 1_000_000_000)),
                D("GevCurrentIPAddress", "int", ip, ro=True),
                D("GevCurrentSubnetMask", "int", 0xFFFFFF00, ro=True),
            ]

        tldevice = [D(k, "string", v, ro=True) for k, v in ident.items()
                    if k != "DeviceFirmwareVersion"] + [
            D("DeviceType", "enum", "GigEVision" if gige else "USB3Vision",
              entries=("GigEVision", "USB3Vision"), ro=True),
            D("DeviceDisplayName", "string", f"{model} ({self.serial})",
              ro=True),
            D("DeviceID", "string", self.serial, ro=True),
        ]
        if gige:
            tldevice += [D("GevDeviceIPAddress", "int", ip, ro=True),
                         D("GevDeviceSubnetMask", "int", 0xFFFFFF00, ro=True)]

        stat_nodes = dict(_STAT_NODES)
        if gige:
            stat_nodes.update(_GIGE_STAT_NODES)
        tlstream = [
            D("StreamBufferCountMode", "enum", "Auto",
              entries=("Auto", "Manual"), locked=True),
            D("StreamBufferCountManual", "int", AUTO_BUFFER_COUNT, lo=3,
              hi=int(f.stream_buffer_count_max), inc=1, locked=True),
            D("StreamBufferCountMax", "int", ro=True,
              get=lambda c, s: c.bounds(
                  c._flat["StreamBufferCountManual"])[1]),
            D("StreamBufferCountResult", "int", ro=True,
              get=lambda c, s: c._host_buffers()),
            D("StreamBufferHandlingMode", "enum", "OldestFirst",
              entries=("OldestFirst", "OldestFirstOverwrite", "NewestFirst",
                       "NewestOnly"), locked=True),
        ] + [D(node, "int", ro=True, get=_stat_getter(key))
             for node, key in stat_nodes.items()]
        if gige:
            tlstream.append(D("StreamMode", "enum", "TeledyneGigEVision",
                              entries=("TeledyneGigEVision", "LWF", "Socket"),
                              locked=True))

        maps = {"device": device, "tldevice": tldevice, "tlstream": tlstream}
        self._apply_knobs(maps)
        self.defs = {k: {d.name: d for d in v} for k, v in maps.items()}
        for kind, table in self.defs.items():
            for d in table.values():
                d.nodemap = kind
        # One flat table for `val()`. The identity strings exist in two maps
        # with the same values, so either definition serves.
        self._flat = {}
        for kind in ("tlstream", "tldevice", "device"):
            self._flat.update(self.defs[kind])
        self.factory = self._factory_values()
        self.values = dict(self.factory)
        for key, value in f.initial_values.items():
            self._set_initial(key, value)

    def _apply_knobs(self, maps: dict) -> None:
        f = self.faults
        for kind, defs in maps.items():
            names = {d.name for d in defs}
            kept = [d for d in defs if d.name not in f.absent_nodes]
            for d in kept:
                if d.selector is not None and (d.selector in f.absent_nodes
                                               or d.selector not in names):
                    # Without its selector the node has one value.
                    if isinstance(d.default, dict):
                        d.default = next(iter(d.default.values()), None)
                    d.selector = None
                if d.name in f.read_only_nodes:
                    d.ro = True
                if d.name in f.node_ranges:
                    lo, hi, inc = f.node_ranges[d.name]
                    d.lo = d.lo if lo is None else lo
                    d.hi = d.hi if hi is None else hi
                    d.inc = d.inc if inc is None else inc
                if d.name in f.enum_entries:
                    d.entries = tuple(f.enum_entries[d.name])
                    if (not callable(d.default)
                            and not isinstance(d.default, dict)
                            and d.default not in d.entries and d.entries):
                        d.default = d.entries[0]
            if kind == "device":
                chunk = next((d for d in kept if d.name == "ChunkSelector"),
                             None)
                if chunk is None or "CounterValue" not in chunk.entries:
                    kept = [d for d in kept
                            if d.name != "ChunkCounterSelector"]
            maps[kind] = kept

    def _factory_values(self) -> dict:
        out = {}
        for table in self.defs.values():
            for d in table.values():
                if d.kind == "command" or d.get is not None:
                    continue
                if d.selector is None:
                    out[(d.name, None)] = d.default
                    continue
                sel_def = self._flat[d.selector]
                for entry in sel_def.entries:
                    value = d.default
                    if isinstance(value, dict):
                        value = value.get(entry, False if d.kind == "bool"
                                          else next(iter(value.values())))
                    out[(d.name, entry)] = value
        return out

    def _set_initial(self, key, value) -> None:
        name, sel = key if isinstance(key, tuple) else (key, None)
        d = self._flat.get(name)
        if d is None:
            raise ValueError(f"{self}: initial_values names {name!r}, which "
                             f"this camera does not have")
        if d.selector is not None and sel is None:
            for entry in self._flat[d.selector].entries:
                self.values[(name, entry)] = value
        else:
            self.values[(name, sel if d.selector else None)] = value

    # --------------------------------------------------- node side effects
    def _user_set_load(self, sel) -> None:
        which = self.values[("UserSetSelector", None)]
        self.values = dict(self.factory)
        for key, value in self.faults.user_sets.get(which, {}).items():
            self._set_initial(key, value)
        # The selector itself keeps the entry that was loaded.
        self.values[("UserSetSelector", None)] = which
        self.user_set_loads.append(which)

    def _software_trigger(self, sel) -> None:
        # Panopticon never software-triggers; the command is counted so a
        # test can prove it, and it produces no frame.
        self.software_triggers += 1

    def _timestamp_latch(self, sel) -> None:
        self._latch = self._ts_units(self._device_ns(self.board.virtual_now()))

    def _timestamp_reset(self, sel) -> None:
        self._ts_origin_s = -self.board.virtual_now() * (1.0 + self.faults.ppm)
        self.timestamp_resets += 1

    def _counter(self, name) -> _Counter:
        c = self._counters.get(name)
        if c is None:
            c = self._counters[name] = _Counter()
        return c

    def _counter_reset(self, sel, base: int = 0) -> None:
        with self.lock:
            c = self._counter(sel)
            prev = self.counter_value(sel)
            st, starts, stops = _board_snapshot(self.board)
            c.reset_v = self.board.virtual_now()
            c.acc = 0
            c.base = int(base)
            c.epoch = st.epoch_v
            c.last_st = st
            c.seen = (starts, stops)
            c.prev_st = None
            c.prev_acc = 0
            c.acq_at_reset = self.acquired
            c.value_at_reset = prev

    def _counter_write(self, sel, value) -> None:
        self._counter_reset(sel, base=value)

    def counter_value(self, name, at_v=None) -> int:
        """`CounterValue` of counter `name`, at virtual time `at_v` (now when
        None). A line source counts the board's pulses on the wired line,
        acquired or not; an exposure source counts acquisitions."""
        c = self._counter(name)
        src = self.val_or("CounterEventSource", name, "Off")
        act = self.val_or("CounterEventActivation", name, "RisingEdge")
        if src in ("ExposureStart", "ExposureEnd"):
            events = self.acquired - c.acq_at_reset
        elif src == self.faults.wired_line and self._line_is_input(src):
            events = self._line_edges(c, at_v)
            if act == "AnyEdge":
                events *= 2
        else:
            events = 0
        return (c.base + events) % (int(self.faults.counter_max) + 1)

    def _line_edges(self, c: _Counter, at_v) -> int:
        board = self.board
        with self.lock:
            st, starts, stops = _board_snapshot(board)
            if c.epoch is None:
                c.epoch = st.epoch_v
            elif st.epoch_v != c.epoch:
                # A new pulse train began: bank what the previous one fired
                # after the reset.
                end = self._train_end(c.last_st, c.seen, st, (starts, stops),
                                      "the trigger-line counter",
                                      "read CounterValue")
                c.prev_acc = c.acc
                c.prev_st = None
                if end is not None:
                    c.prev_st = c.last_st._replace(stop_v=end)
                    c.acc += _pulses(board, c.prev_st, c.reset_v, end)
                c.epoch = st.epoch_v
            c.last_st = st
            c.seen = (starts, stops)
            v = board.virtual_now() if at_v is None else at_v
            if c.prev_st is not None and v < st.epoch_v:
                # A frame of the replaced train reads the count its trigger
                # saw.
                return c.prev_acc + _pulses(board, c.prev_st, c.reset_v, v)
            return c.acc + _pulses(board, st, c.reset_v, v)

    def _train_end(self, prev, prev_seen, cur, cur_seen, what: str,
                   how: str):
        """Virtual time at which the pulse train `prev` ended, now that
        `cur` has replaced it, or None when `prev` is no train.

        `SimBoard` keeps only the train it runs now. The end of the one
        before is known in two cases: it was seen stopped, or no stop came
        between the last look at it and the start of `cur`, so that start cut
        it off. In any other case pulses fired at times nobody saw, and
        `FakeMisuse` names the limitation instead of guessing a count. `what`
        names the observer and `how` the call that lets it look.
        """
        started = cur_seen[0] - prev_seen[0]
        if started == 1:
            if prev.fps <= 0:
                return None
            if prev.stop_v is not None:
                return prev.stop_v
            # A stop of `cur` shows in `cur`; any other stop ended `prev`.
            if cur_seen[1] - prev_seen[1] - (cur.stop_v is not None) == 0:
                return cur.epoch_v
            detail = ("the train it last saw running stopped at a moment it "
                      "did not see, and another train started")
        elif started > 1:
            detail = (f"{started} pulse trains started since it last looked, "
                      f"so at least one ran unseen")
        else:
            detail = "the board's start count went back, so the board changed"
        self.api._misuse(
            f"{self}: {what} cannot follow the trigger board: {detail}. "
            f"SimBoard keeps only the train it runs now, so {how} between "
            f"one pulse train and the next.")

    def _line_is_input(self, line) -> bool:
        return self.val_or("LineMode", line, "Input") == "Input"

    def _line_high(self, line) -> bool:
        high = False
        if line == self.faults.wired_line and self._line_is_input(line):
            st = self.board.state()
            if st.running and st.fps > 0:
                x = (self.board.virtual_now() - st.epoch_v) * st.fps
                # A 50% duty square wave with its rising edge on each pulse.
                high = (x >= 1.0 and (x % 1.0) < 0.5
                        and self.board.fired(int(x)))
        return high != bool(self.val_or("LineInverter", line, False))

    def _line_status_all(self) -> int:
        bits = 0
        selector = self._flat.get("LineSelector")
        names = selector.entries if selector else (self.faults.wired_line,)
        for name in names:
            if self._line_high(name):
                bits |= 1 << _line_bit(name)
        return bits

    # ------------------------------------------------- derived quantities
    def _bits(self) -> int:
        return int(self.faults.bpp or PIXEL_BITS.get(
            self.val_or("PixelFormat", None, "Mono8"), 8))

    def _payload(self) -> int:
        return self.val("Width") * self.val("Height") * self._bits() // 8

    def _link_interval_s(self) -> float:
        limit = self.val_or("DeviceLinkThroughputLimit", None, 0)
        return self._payload() / limit if limit else 0.0

    def _exposure_max(self) -> float:
        f = self.faults
        if (f.ceiling_follows_frame_rate
                and self.values.get(("AcquisitionFrameRateEnable", None))):
            fps = self.values.get(("AcquisitionFrameRate", None), 0.0)
            if fps > 0:
                return 1e6 / fps - f.exposure_overhead_us
        return EXPOSURE_MAX_US

    def _frame_rate_max(self) -> float:
        exposure = self.values.get(("ExposureTime", None), 0.0)
        frame_s = (exposure + self.faults.exposure_overhead_us) * 1e-6
        return 1.0 / max(frame_s, self._link_interval_s(), 1e-6)

    def _resulting_rate(self) -> float:
        fps = self.values.get(("AcquisitionFrameRate", None), 0.0)
        if self.values.get(("AcquisitionFrameRateEnable", None)) and fps > 0:
            return min(fps, self._frame_rate_max())
        return self._frame_rate_max()

    def _min_interval_s(self) -> float:
        """Shortest trigger interval the camera acquires. A trigger sooner
        than this after the last acquisition finds the camera busy and is
        ignored: no frame, no frame ID."""
        f = self.faults
        exposure = self.val_or("ExposureTime", None, 0.0)
        overlap = self.val_or("TriggerOverlap", "FrameStart", "ReadOut")
        frame_us = exposure + f.exposure_overhead_us
        if overlap == "Off":
            frame_us += f.overlap_off_readout_us
        interval = max(frame_us * 1e-6, self._link_interval_s())
        fps = self.values.get(("AcquisitionFrameRate", None), 0.0)
        if self.values.get(("AcquisitionFrameRateEnable", None)) and fps > 0:
            interval = max(interval, 1.0 / fps)
        return interval

    def _host_buffers(self) -> int:
        if self.val_or("StreamBufferCountMode", None, "Auto") == "Manual":
            return int(self.val("StreamBufferCountManual"))
        return AUTO_BUFFER_COUNT

    # ---------------------------------------------------------------- clock
    def _device_ns(self, v: float) -> int:
        secs = v * (1.0 + self.faults.ppm) + self._ts_origin_s
        return max(0, int(round(secs * 1e9)))

    def _ts_units(self, ns: int) -> int:
        return ns // 8 if self.faults.ts_unit == "ticks_125MHz" else ns

    def _wait(self, deadline: float) -> None:
        if self.faults.gil_hold_wait:
            _gil_holding_sleep_until(deadline)
        else:
            _sleep_until(deadline)

    # ------------------------------------------------------------ lifecycle
    def init(self) -> None:
        f = self.faults
        if f.init_error:
            raise FlirError(f.init_error, f"spinCameraInit ({self.serial})")
        self.ensure_built()
        self.initialized = True
        self.inits += 1

    def deinit(self) -> None:
        if self.streaming:
            self.end()
        self.initialized = False

    def begin(self) -> None:
        f = self.faults
        if not self.initialized:
            raise FlirError(SPINNAKER_ERR_NOT_INITIALIZED,
                            f"spinCameraBeginAcquisition ({self.serial}): "
                            f"the camera is not initialised")
        if self.streaming:
            raise FlirError(SPINNAKER_ERR_BUSY,
                            f"spinCameraBeginAcquisition ({self.serial}): "
                            f"already streaming")
        if f.begin_error:
            raise FlirError(f.begin_error,
                            f"spinCameraBeginAcquisition ({self.serial})")
        width, height, bits = self.val("Width"), self.val("Height"), self._bits()
        stride = width * bits // 8 + f.stride_extra + f.padding_x
        size = stride * height + f.padding_y
        # np.full, not np.empty: it touches every page now, so the first
        # frame does not pay for the allocation.
        self._buffers = [np.full(size, 32, np.uint8)
                         for _ in range(PHYSICAL_BUFFERS)]
        self._addr = [b.__array_interface__["data"][0] for b in self._buffers]
        # A view, and any slice or reshape of it, holds a reference to its
        # buffer (numpy points every derived view's base at the array that
        # owns the memory), so a count above this baseline is a view still
        # alive.
        self._buf_refs = [sys.getrefcount(self._buffers[i])
                          for i in range(PHYSICAL_BUFFERS)]
        self._free = deque(range(PHYSICAL_BUFFERS))
        chunks = None
        if self.val_or("ChunkModeActive", None, False):
            chunks = frozenset(
                e for e in self._flat["ChunkSelector"].entries
                if self.values.get(("ChunkEnable", e), False))
        mode = self.val_or("AcquisitionMode", None, "Continuous")
        frames_left = {"Continuous": None, "SingleFrame": 1}.get(
            mode, self.val_or("AcquisitionFrameCount", None, 1))
        self._acq = {
            "width": width, "height": height, "bits": bits, "stride": stride,
            "nbuf": self._host_buffers(),
            "handling": self.val_or("StreamBufferHandlingMode", None,
                                    "OldestFirst"),
            "chunks": chunks,
            "chunk_counter": self.val_or("ChunkCounterSelector", None,
                                         COUNTERS[0]),
            "id16": (self.interface == "GigE"
                     and self.val_or("GevGVSPExtendedIDMode", None,
                                     "Off") != "On"),
            "frames_left": frames_left,
        }
        if f.id_restart_on_begin:
            self._fid_count = 0
        now_v = self.board.virtual_now()
        if f.ts_reset_on_begin:
            self._ts_origin_s = -now_v * (1.0 + f.ppm)
        self._fr_v = now_v
        st, starts, stops = _board_snapshot(self.board)
        # Anchored to the pulse train running now: a camera cannot deliver a
        # trigger that has already passed.
        self._next = self.board.ordinal_now(st) + 1
        self._train_st = st
        self._seen = (starts, stops)
        self._due_key = None
        self._queue = deque()
        self.streaming = True
        self.begins += 1

    def end(self) -> None:
        if not self.streaming:
            raise FlirError(SPINNAKER_ERR_NOT_INITIALIZED,
                            f"spinCameraEndAcquisition ({self.serial}): the "
                            f"camera is not started")
        if self._held:
            self.api._misuse(
                f"{self}: EndAcquisition with {len(self._held)} image(s) not "
                f"released; the SDK discards the pool on this call, so a "
                f"view of a held image would read freed memory")
        self.streaming = False
        self.ends += 1
        if self.faults.counters_reset_on_end:
            self.stats = dict.fromkeys(_STAT_KEYS, 0)
        self._buffers, self._addr, self._buf_refs = [], [], []
        self._free = deque()
        self._queue = deque()

    # ---------------------------------------------------------- acquisition
    def next_image(self, timeout_ms: int):
        deadline = time.perf_counter() + timeout_ms / 1000.0
        if not self.streaming:
            raise FlirError(SPINNAKER_ERR_NOT_INITIALIZED,
                            f"spinCameraGetNextImageEx ({self.serial}): the "
                            f"camera is not started")
        f = self.faults
        if f.next_image_error and self.handed >= f.next_image_error_after:
            raise FlirError(f.next_image_error,
                            f"spinCameraGetNextImageEx ({self.serial})")
        if len(self._held) >= PHYSICAL_BUFFERS:
            self.api._misuse(
                f"{self}: all {PHYSICAL_BUFFERS} buffers are held by images "
                f"that were never released")
        if self.val_or("TriggerMode", "FrameStart", "Off") == "Off":
            if self._acq["frames_left"] == 0 or self._other_trigger_armed():
                self._wait(deadline)
                raise FlirTimeout(f"{self.serial}: no image within "
                                  f"{timeout_ms} ms")
            return self._next_freerun(deadline, timeout_ms)
        return self._next_triggered(deadline, timeout_ms)

    def _other_trigger_armed(self) -> bool:
        """An acquisition or burst trigger that is on and never fires holds
        back every frame, as it does on the camera."""
        selector = self._flat.get("TriggerSelector")
        if selector is None:
            return False
        return any(self.val_or("TriggerMode", sel, "Off") == "On"
                   for sel in ("AcquisitionStart", "FrameBurstStart")
                   if sel in selector.entries)

    def _deaf_reason(self):
        """Why the camera takes no pulse from its trigger wire now, or None
        when it takes each one."""
        if self.val_or("TriggerMode", "FrameStart", "Off") == "Off":
            return "TriggerMode is Off"
        if self._other_trigger_armed():
            return ("an AcquisitionStart or FrameBurstStart trigger is armed "
                    "and never fires")
        source = self.val_or("TriggerSource", "FrameStart", "Software")
        if source != self.faults.wired_line or not self._line_is_input(source):
            # No signal reaches that input.
            return f"TriggerSource {source} carries no pulses"
        if self._acq["frames_left"] == 0:
            return "the acquisition's frame count is used up"
        return None

    def _next_freerun(self, deadline: float, timeout_ms: int):
        rate = self._resulting_rate()
        v = self._fr_v + 1.0 / rate
        st = self.board.state()
        due = sim_board.SimBoard.real_time_of(v, st)
        if due > deadline:
            self._wait(deadline)
            raise FlirTimeout(f"{self.serial}: no free-run image within "
                              f"{timeout_ms} ms")
        self._wait(due)
        buf = self._take_buffer()
        self._fr_v = v
        self.acquired += 1
        self.stats["started"] += 1
        self.stats["received"] += 1
        if self._acq["frames_left"]:
            self._acq["frames_left"] -= 1
        return self._hand_out(self._frame(v, v, incomplete=False), buf)

    def _stall_span(self, st) -> tuple:
        f = self.faults
        if not f.stall_at or f.stall_s <= 0 or st.fps <= 0:
            return (0, -1)
        return (f.stall_at, f.stall_at + int(round(f.stall_s * st.fps)) - 1)

    def _due_real(self, i: int, st) -> float:
        v = sim_board.SimBoard.trigger_v(i + max(0, self.faults.lag_frames), st)
        if st.stop_v is not None:
            v = min(v, st.stop_v)
        if self.faults.jitter_s:
            v += self._rng.uniform(0.0, self.faults.jitter_s)
        return sim_board.SimBoard.real_time_of(v, st)

    def _next_triggered(self, deadline: float, timeout_ms: int):
        while True:
            due = self._walk()
            if self._queue:
                buf = self._take_buffer()
                frame = (self._queue.pop()
                         if self._acq["handling"] == "NewestFirst"
                         else self._queue.popleft())
                return self._hand_out(frame, buf)
            now = time.perf_counter()
            reason = self._deaf_reason()
            if now >= deadline:
                why = reason or ("no trigger due" if due is None else "")
                raise FlirTimeout(f"{self.serial}: no image within "
                                  f"{timeout_ms} ms"
                                  + (f" ({why})" if why else ""))
            if reason is not None:
                # Nothing can reach the queue before the deadline.
                self._wait(deadline)
            elif due is None:
                # Keep watching the board, because a waiting camera delivers
                # the first pulse of a train that starts during its wait.
                self._wait(min(deadline, now + POLL_S))
            else:
                self._wait(min(due, deadline, now + POLL_S))

    def _walk(self):
        """Bring the host queue up to now.

        Every trigger that has reached the camera since the last walk goes,
        in order, through acquisition and into the host pool, as the faults
        and `StreamBufferHandlingMode` decide. `next_image` and
        `image_release` both walk, because those are the moments the pool's
        free room changes. Returns the `perf_counter` time at which the next
        trigger arrives, or None when none is due.

        A new pulse train restarts the board's ordinals at 1. The pulses the
        replaced train fired before it ended reach the camera first, and
        `_train_end` refuses when that end was not seen.
        """
        if not self.streaming:
            return None
        board = self.board
        with self.lock:
            st, starts, stops = _board_snapshot(board)
            prev, prev_seen = self._train_st, self._seen
            new_train = st.epoch_v != prev.epoch_v
            deaf = self._deaf_reason() is not None
            if deaf:
                # The pulses fired so far pass the camera without effect.
                if new_train:
                    self._next = 1
                self._next = max(self._next, board.ordinal_now(st) + 1)
            elif new_train:
                end = self._train_end(prev, prev_seen, st, (starts, stops),
                                      "the frame stream",
                                      "call next_image or image_release")
                if end is not None:
                    self._walk_train(prev._replace(stop_v=end), math.inf)
                self._next = 1
            self._train_st, self._seen = st, (starts, stops)
            if deaf:
                return None
            return self._walk_train(st, time.perf_counter())

    def _walk_train(self, st, now: float):
        """Take the triggers of train `st` that arrive by `now`, in order.
        Returns the arrival time of the next one, or None when none is
        due."""
        board = self.board
        while st.fps > 0 and not board.exhausted(self._next, st):
            i = self._next
            key = (i, st.epoch_v, st.stop_v)
            if key != self._due_key:
                # Once per trigger, so a jittered arrival is drawn once.
                self._due_key, self._due = key, self._due_real(i, st)
            if self._due > now:
                return self._due
            self._next = i + 1
            self._take_trigger(i, st)
        return None

    def _take_trigger(self, i: int, st) -> None:
        """Trigger `i` of the train `st` reaching the camera: acquired or
        not, then queued on the host or lost."""
        f = self.faults
        acq = self._acq
        if not self.board.fired(i) or acq["frames_left"] == 0:
            return
        if f.dead_after and i >= f.dead_after:
            return
        lo, hi = self._stall_span(st)
        if lo <= i <= hi:
            self._consume_id()
            self.stats["incomplete"] += 1
            return
        tv = sim_board.SimBoard.trigger_v(i, st)
        if ((f.ignore_every and i % f.ignore_every == 0)
                or tv - self._last_acq_v < self._min_interval_s() - 1e-9):
            self.ignored += 1
            return
        self._last_acq_v = tv
        self.acquired += 1
        self.stats["started"] += 1
        if acq["frames_left"]:
            acq["frames_left"] -= 1
        if i in f.drop_triggers or (f.drop_every and i % f.drop_every == 0):
            self._consume_id()
            self.stats["incomplete"] += 1
            if self.interface == "GigE":
                self.stats["missed_packets"] += 3
                self.stats["resend_requests"] += 1
                self.stats["resend_requested_packets"] += 3
            return
        if f.underrun_every and i % f.underrun_every == 0:
            self._consume_id()
            self.stats["lost"] += 1
            return
        incomplete = bool(f.incomplete_every
                          and self.acquired % f.incomplete_every == 0)
        if incomplete:
            self.stats["incomplete"] += 1
        v = tv + self.val_or("TriggerDelay", "FrameStart", 0.0) * 1e-6
        if self.val_or("TriggerActivation", "FrameStart",
                       "RisingEdge") == "FallingEdge":
            v += 0.5 / st.fps
        self._enqueue(self._frame(v, tv, incomplete))

    def _enqueue(self, frame: tuple) -> None:
        """Put an acquired frame into the host pool.

        The pool is `StreamBufferCountManual` buffers, shared by the frames
        waiting in the queue and the images the application holds. With no
        free buffer, OldestFirst keeps the frames it has and loses the new
        one (`StreamLostFrameCount`). The other modes discard the oldest
        queued frame to make room (`StreamDroppedFrameCount`). NewestOnly
        keeps one frame at most, and NewestFirst hands out the newest first.
        """
        acq = self._acq
        q = self._queue
        mode = acq["handling"]
        if mode == "NewestOnly":
            while q:
                q.popleft()
                self.stats["dropped"] += 1
        if acq["nbuf"] - len(q) - len(self._held) <= 0:
            if mode == "OldestFirst" or not q:
                # With nothing queued, the application holds every buffer and
                # there is no frame to overwrite.
                self.stats["lost"] += 1
                return
            q.popleft()
            self.stats["dropped"] += 1
        q.append(frame)
        self.stats["received"] += 1

    def _consume_id(self) -> int:
        f = self.faults
        raw = f.id_base + f.id_offset + self._fid_count
        self._fid_count += 1
        mode = f.id_wrap
        if mode == "auto":
            mode = "65535->1" if self._acq["id16"] else "none"
        if mode == "65535->1":
            return raw if raw <= 65535 else (raw - 1) % 65535 + 1
        if mode == "65535->0":
            return raw % 65536
        return raw

    def _take_buffer(self) -> int:
        if not self._free:
            self.api._misuse(f"{self}: no free buffer; images were not "
                             f"released")
        i = self._free.popleft()
        alive = sys.getrefcount(self._buffers[i]) - self._buf_refs[i]
        if alive > 0:
            self._free.appendleft(i)
            self.api._misuse(
                f"{self}: {alive} zero-copy view(s) of an earlier, released "
                f"image are still referenced while the driver refills their "
                f"buffer; a consumer stored the view instead of copying out "
                f"of it")
        return i

    def _frame(self, v: float, trigger_v: float, incomplete: bool) -> tuple:
        """`(frame_id, timestamp, chunk, incomplete)` of a frame acquired at
        trigger time `trigger_v` and stamped at `v`, in virtual seconds.
        Consumes the frame ID."""
        f = self.faults
        acq = self._acq
        fid = self._consume_id()
        ns = self._device_ns(v)
        chunk = None
        if acq["chunks"] is not None:
            enabled = acq["chunks"]
            chunk = {}
            if "FrameID" in enabled:
                chunk["FrameID"] = fid
            if "Timestamp" in enabled:
                chunk["Timestamp"] = 0 if f.chunk_ts_zero else self._ts_units(ns)
            if "CounterValue" in enabled:
                chunk["CounterValue"] = self.counter_value(
                    acq["chunk_counter"], at_v=trigger_v)
            for key in ("OffsetX", "OffsetY"):
                if key in enabled:
                    chunk[key] = self.val(key)
            if "Width" in enabled:
                chunk["Width"] = acq["width"]
            if "Height" in enabled:
                chunk["Height"] = acq["height"]
        return fid, 0 if f.ts_zero else self._ts_units(ns), chunk, incomplete

    def _hand_out(self, frame: tuple, buf: int) -> int:
        """Give a frame to the application as an image in buffer `buf`."""
        fid, timestamp, chunk, incomplete = frame
        f = self.faults
        acq = self._acq
        self._buffers[buf][0] = fid & 0xFF
        img = _FakeImage(self, buf, fid, timestamp, chunk, incomplete,
                         acq["width"], acq["height"], acq["bits"],
                         acq["stride"], (f.padding_x, f.padding_y))
        self.stats["delivered"] += 1
        self.handed += 1
        return self.api._register_image(img)


def _line_bit(name: str) -> int:
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 0


def _stat_getter(key: str):
    return lambda c, s: c.stats[key]


class FakeSpinC:
    """Simulated cameras behind `SpinC`'s methods.

    `n_cameras`, `width`, `height` and `serials` shape the rig. Whatever is
    not given comes from `configure()`, and then from the flir_sim profile
    (`profile_path`, default `FLIR_SIM_PROFILE_PATH`). Serials default to the
    profile's `camera_serials`, else to generated ones. Camera index `i` is
    position `i` in the serials sorted as strings, which is the order
    Panopticon names cameras in.

    `sensor` is the sensor size `(width, height)`; it defaults to the frame
    size. `interface` is "USB3" or "GigE" for every camera without its own
    `FlirFaults.interface`. `faults` maps a serial or a camera index to a
    `FlirFaults` and defaults to a copy of the module `FAULTS`. `board` is
    the trigger clock and defaults to `sim_board.shared_board()`, the one
    `SimSerial` drives. `enumeration_order` is "reverse" (the default, so a
    backend that forgets to sort is caught) or "sorted". `sdk_missing` makes
    construction raise `FlirSdkUnavailable`, as `SpinC` does on a host
    without the SDK.

    Test code reads `violations`, `leaks` and `fake_camera(...)`.
    """

    def __init__(self, n_cameras: int | None = None, *,
                 width: int | None = None, height: int | None = None,
                 serials=None, sensor=None, interface: str | None = None,
                 faults: dict | None = None, board=None,
                 sdk_missing: bool = False, enumeration_order: str = "reverse",
                 profile_path=None):
        if sdk_missing:
            raise FlirSdkUnavailable(
                "The Spinnaker SDK is not installed (FakeSpinC was built with "
                "sdk_missing=True to simulate a host without it).",
                ("<FakeSpinC: sdk_missing>",))
        if enumeration_order not in ("reverse", "sorted"):
            raise ValueError(f"enumeration_order {enumeration_order!r}: use "
                             f"'reverse' or 'sorted'")
        ov = _SHAPE_OVERRIDES
        if serials is None:
            serials = ov.get("serials")
        if n_cameras is None and serials is not None:
            n_cameras = len(serials)
        n_cameras = n_cameras if n_cameras is not None else ov.get("n_cameras")
        width = width if width is not None else ov.get("width")
        height = height if height is not None else ov.get("height")
        if None in (n_cameras, width, height):
            pn, pw, ph, ps = profile_shape(profile_path)
            n_cameras = pn if n_cameras is None else n_cameras
            width = pw if width is None else width
            height = ph if height is None else height
            if serials is None and ps:
                serials = ps
        n_cameras = int(n_cameras)
        if n_cameras <= 0:
            raise ValueError(f"n_cameras {n_cameras}: need at least one camera")
        if serials is None:
            serials = [str(SERIAL_BASE + i) for i in range(n_cameras)]
        serials = sorted(str(s) for s in serials)
        if len(serials) != n_cameras or len(set(serials)) != n_cameras:
            raise ValueError(f"serials {serials} must be {n_cameras} distinct "
                             f"values")
        self.interface = interface or ov.get("interface") or "USB3"
        if self.interface not in LINK:
            raise ValueError(f"interface {self.interface!r}; use one of "
                             f"{list(INTERFACES)}")
        if sensor is None:
            sensor = (-(-int(width) // 16) * 16, -(-int(height) // 2) * 2)
        self.faults = dict(FAULTS if faults is None else faults)
        self.enumeration_order = enumeration_order
        # Parity with SpinC's attributes.
        self.dll_path = "<FakeSpinC: simulated cameras, no SDK>"
        self.searched: tuple = ()
        self.missing_optional: tuple = ()
        #: Every `FakeMisuse` message, in order.
        self.violations: list = []
        #: Handles `system_release()` found still held, in order.
        self.leaks: list = []
        self._board = board
        self._reg = threading.Lock()
        self._next_handle = _HANDLE_BASE
        self._img_seq = 0
        self._system = None
        self._cameras = [_FakeCamera(self, i, s, width, height, tuple(sensor))
                         for i, s in enumerate(serials)]
        self._cam_handles: dict = {}
        self._nodemaps: dict = {}
        self._nodemap_by_key: dict = {}
        self._nodes: dict = {}
        self._node_by_key: dict = {}
        self._images: dict = {}

    def __repr__(self) -> str:
        return f"<FakeSpinC {len(self._cameras)} x {self.interface}>"

    # ----------------------------------------------------------- test hooks
    @property
    def board(self):
        """The trigger clock the cameras read."""
        return self._board or sim_board.shared_board()

    def fake_camera(self, key) -> _FakeCamera:
        """The simulated camera with serial `key`, or index `key`."""
        for cam in self._cameras:
            if cam.serial == str(key) or (isinstance(key, int)
                                          and cam.index == key):
                return cam
        raise KeyError(f"no simulated camera {key!r}")

    def _faults_for(self, cam) -> FlirFaults:
        return (self.faults.get(cam.serial) or self.faults.get(cam.index)
                or _HEALTHY)

    def _misuse(self, message: str):
        self.violations.append(message)
        raise FakeMisuse(message)

    def _new_handle(self) -> int:
        with self._reg:
            h = self._next_handle
            self._next_handle += 16
            return h

    def _register_image(self, img: _FakeImage) -> int:
        with self._reg:
            h = _IMAGE_BASE + self._img_seq
            self._img_seq += 1
        self._images[h] = img
        img.cam._held.add(h)
        return h

    def _cam(self, h, what: str) -> _FakeCamera:
        cam = self._cam_handles.get(h)
        if cam is None:
            raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                            f"{what}: {h!r} is not a camera handle")
        return cam

    def _node(self, h, what: str) -> tuple:
        if h is None:
            raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                            f"{what}: the node is absent on this camera")
        entry = self._nodes.get(h)
        if entry is None:
            raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                            f"{what}: {h!r} is not a node handle")
        cam, d = entry
        if d.nodemap == "device" and not cam.initialized:
            raise FlirError(SPINNAKER_ERR_NOT_INITIALIZED,
                            f"{d.name}: {what}: the camera is not "
                            f"initialised")
        return cam, d

    def _typed(self, h, kind: str, what: str) -> tuple:
        cam, d = self._node(h, what)
        if d.kind != kind:
            raise FlirError(SPINNAKER_ERR_GENICAM_DYNAMIC_CAST,
                            f"{d.name}: {what}: it is a {d.kind} node")
        return cam, d

    def _image(self, h, what: str) -> _FakeImage:
        img = self._images.get(h)
        if img is not None:
            return img
        if isinstance(h, int) and _IMAGE_BASE <= h < _IMAGE_BASE + self._img_seq:
            self._misuse(f"{what}: image {h:#x} was already released; after "
                         f"spinImageRelease its buffer belongs to the driver")
        raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                        f"{what}: {h!r} is not an image handle")

    # ----------------------------------------------------- system and cameras
    def system_get(self) -> int:
        if self._system is None:
            self._system = self._new_handle()
        return self._system

    def system_release(self) -> None:
        for h, cam in list(self._cam_handles.items()):
            self.leaks.append(f"{cam}: handle {h:#x} was not released before "
                              f"system_release")
            try:
                if cam.streaming:
                    cam.end()
                if cam.initialized:
                    cam.deinit()
            except (FlirError, FakeMisuse):
                pass
            del self._cam_handles[h]
        self._system = None

    def library_version(self) -> str:
        self.system_get()
        return FAKE_LIBRARY_VERSION

    def cameras(self) -> list:
        self.system_get()
        order = (reversed(self._cameras) if self.enumeration_order == "reverse"
                 else self._cameras)
        out = []
        for cam in order:
            cam.ensure_built()
            h = self._new_handle()
            self._cam_handles[h] = cam
            out.append(h)
        return out

    def camera_serial(self, cam) -> str:
        node = self.node(self.nodemap(cam, "tldevice"), "DeviceSerialNumber")
        if node is None:
            raise FlirError(SPINNAKER_ERR_NOT_AVAILABLE,
                            "DeviceSerialNumber is absent from the TL device "
                            "nodemap")
        return self.string_get(node)

    def init(self, cam) -> None:
        self._cam(cam, "spinCameraInit").init()

    def deinit(self, cam) -> None:
        self._cam(cam, "spinCameraDeInit").deinit()

    def release(self, cam) -> None:
        self._cam(cam, "spinCameraRelease")
        del self._cam_handles[cam]

    def is_initialized(self, cam) -> bool:
        return self._cam(cam, "spinCameraIsInitialized").initialized

    # ------------------------------------------------------------------ nodes
    def nodemap(self, cam, which: str = "device") -> int:
        if which not in ("device", "tldevice", "tlstream"):
            raise ValueError(f"nodemap {which!r}: use one of "
                             f"['device', 'tldevice', 'tlstream']")
        fake = self._cam(cam, "spinCameraGetNodeMap")
        if which == "device" and not fake.initialized:
            raise FlirError(SPINNAKER_ERR_NOT_INITIALIZED,
                            f"spinCameraGetNodeMap ({fake.serial}): the "
                            f"camera is not initialised")
        fake.ensure_built()
        key = (fake.index, which)
        h = self._nodemap_by_key.get(key)
        if h is None:
            h = self._nodemap_by_key[key] = self._new_handle()
            self._nodemaps[h] = (fake, which)
        return h

    def node(self, nodemap, name: str):
        entry = self._nodemaps.get(nodemap)
        if entry is None:
            raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                            f"spinNodeMapGetNode({name}): {nodemap!r} is not "
                            f"a nodemap handle")
        cam, which = entry
        if which == "device" and not cam.initialized:
            raise FlirError(SPINNAKER_ERR_NOT_INITIALIZED,
                            f"spinNodeMapGetNode({name}): the camera is not "
                            f"initialised")
        d = cam.defs[which].get(name)
        if d is None:
            return None
        key = (cam.index, which, name)
        h = self._node_by_key.get(key)
        if h is None:
            h = self._node_by_key[key] = self._new_handle()
            self._nodes[h] = (cam, d)
        return h

    def node_available(self, node) -> bool:
        if node is None:
            return False
        self._node(node, "spinNodeIsAvailable")
        return True

    def node_readable(self, node) -> bool:
        if node is None:
            return False
        return self._node(node, "spinNodeIsReadable")[1].kind != "command"

    def node_writable(self, node) -> bool:
        if node is None:
            return False
        cam, d = self._node(node, "spinNodeIsWritable")
        return cam.writable(d)

    def int_get(self, node) -> int:
        cam, d = self._typed(node, "int", "spinIntegerGetValue")
        return int(cam.read(d))

    def int_set(self, node, value) -> None:
        cam, d = self._typed(node, "int", "spinIntegerSetValue")
        cam.write(d, _as_int(value, d.name))

    def int_min(self, node) -> int:
        cam, d = self._typed(node, "int", "spinIntegerGetMin")
        return cam.bounds(d)[0]

    def int_max(self, node) -> int:
        cam, d = self._typed(node, "int", "spinIntegerGetMax")
        return cam.bounds(d)[1]

    def int_inc(self, node) -> int:
        cam, d = self._typed(node, "int", "spinIntegerGetInc")
        return cam.bounds(d)[2]

    def float_get(self, node) -> float:
        cam, d = self._typed(node, "float", "spinFloatGetValue")
        return float(cam.read(d))

    def float_set(self, node, value) -> None:
        cam, d = self._typed(node, "float", "spinFloatSetValue")
        cam.write(d, float(value))

    def float_min(self, node) -> float:
        cam, d = self._typed(node, "float", "spinFloatGetMin")
        return float(cam.bounds(d)[0])

    def float_max(self, node) -> float:
        cam, d = self._typed(node, "float", "spinFloatGetMax")
        return float(cam.bounds(d)[1])

    def enum_get_symbolic(self, node) -> str:
        cam, d = self._typed(node, "enum", "spinEnumerationGetCurrentEntry")
        return str(cam.read(d))

    def enum_set_symbolic(self, node, symbolic: str) -> None:
        cam, d = self._typed(node, "enum", "spinEnumerationGetEntryByName")
        if symbolic not in d.entries:
            raise FlirError(SPINNAKER_ERR_NOT_AVAILABLE,
                            f"{d.name}: no entry {symbolic!r}")
        cam.write(d, symbolic)

    def enum_entries(self, node) -> list:
        return list(self._typed(node, "enum",
                                "spinEnumerationGetNumEntries")[1].entries)

    def bool_get(self, node) -> bool:
        cam, d = self._typed(node, "bool", "spinBooleanGetValue")
        return bool(cam.read(d))

    def bool_set(self, node, value) -> None:
        cam, d = self._typed(node, "bool", "spinBooleanSetValue")
        cam.write(d, bool(value))

    def command(self, node) -> None:
        cam, d = self._typed(node, "command", "spinCommandExecute")
        cam.execute(d)

    def string_get(self, node) -> str:
        cam, d = self._typed(node, "string", "spinStringGetValue")
        return str(cam.read(d))

    # ------------------------------------------------------------ acquisition
    def begin(self, cam) -> None:
        self._cam(cam, "spinCameraBeginAcquisition").begin()

    def end(self, cam) -> None:
        self._cam(cam, "spinCameraEndAcquisition").end()

    def is_streaming(self, cam) -> bool:
        return self._cam(cam, "spinCameraIsStreaming").streaming

    def next_image(self, cam, timeout_ms: int) -> int:
        timeout_ms = int(timeout_ms)
        if timeout_ms < 0:
            raise ValueError(f"timeout_ms {timeout_ms} is negative")
        return self._cam(cam, "spinCameraGetNextImageEx").next_image(
            timeout_ms)

    # ------------------------------------------------------------------ image
    def image_incomplete(self, image) -> bool:
        return self._image(image, "spinImageIsIncomplete").incomplete

    def image_status(self, image) -> int:
        return self._image(image, "spinImageGetStatus").status

    def image_status_text(self, image) -> str:
        status = self._image(image, "spinImageGetStatus").status
        return IMAGE_STATUS_NAMES.get(status, f"image status {status}")

    def image_frame_id(self, image) -> int:
        return self._image(image, "spinImageGetFrameID").frame_id

    def image_timestamp(self, image) -> int:
        return self._image(image, "spinImageGetTimeStamp").timestamp

    def image_padding(self, image) -> tuple:
        return self._image(image, "spinImageGetPaddingX").padding

    def image_dims(self, image) -> tuple:
        img = self._image(image, "spinImageGetWidth")
        return img.width, img.height

    def image_stride(self, image) -> int:
        return self._image(image, "spinImageGetStride").stride

    def image_bpp(self, image) -> int:
        return self._image(image, "spinImageGetBitsPerPixel").bpp

    def image_data_ptr(self, image) -> int:
        img = self._image(image, "spinImageGetData")
        return img.cam._addr[img.buf]

    def image_view(self, image, width: int, height: int):
        img = self._image(image, "spinImageGetData")
        cam = img.cam
        problems = []
        if (int(width), int(height)) != (img.width, img.height):
            problems.append(f"asked for {width}x{height}, the image is "
                            f"{img.width}x{img.height}")
        if img.bpp != 8:
            problems.append(f"the image has {img.bpp} bits per pixel")
        if img.padding != (0, 0):
            problems.append(f"the image has padding {img.padding}")
        if img.stride != img.width:
            problems.append(f"the stride is {img.stride} bytes for "
                            f"{img.width} pixels")
        if problems:
            self._misuse(f"{cam}: an 8-bit (H, W) view of this image would "
                         f"be sheared or truncated: " + "; ".join(problems))
        view = cam._buffers[img.buf][:img.width * img.height].reshape(
            img.height, img.width)
        return view

    def chunk_int(self, image, name: str) -> int:
        img = self._image(image, "spinImageChunkDataGetIntValue")
        img.cam.chunk_names_used.add(name)
        if img.chunk is None:
            raise FlirError(SPINNAKER_ERR_NOT_AVAILABLE,
                            f"spinImageChunkDataGetIntValue({name}): chunk "
                            f"mode was not active when acquisition began")
        key = name[len("Chunk"):] if name.startswith("Chunk") else name
        if key not in img.chunk:
            raise FlirError(SPINNAKER_ERR_NOT_AVAILABLE,
                            f"spinImageChunkDataGetIntValue({name}): that "
                            f"chunk is not enabled (enabled: "
                            f"{sorted(img.chunk)})")
        return int(img.chunk[key])

    def image_release(self, image) -> None:
        img = self._image(image, "spinImageRelease")
        cam = img.cam
        # Frames that reached the host while this image was held found one
        # free buffer fewer, so the queue catches up before the buffer
        # returns to the pool.
        cam._walk()
        del self._images[image]
        cam._held.discard(image)
        if cam.streaming:
            cam._free.append(img.buf)
