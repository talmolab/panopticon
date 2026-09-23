"""FLIR / Teledyne camera backend: Spinnaker cameras through the C API.

`FlirBackend` implements `CameraBackend` (see this package's `__init__`) for
any Spinnaker camera, GigE or USB3. Every vendor rule lives here: node names,
the order settings are applied in, the self-test at open, block-ID and
timestamp normalisation, the trigger-counter witness, stream statistics and
thermals. It is written only against the methods of `_spinc.SpinC`, so
`fake_spinc.FakeSpinC` runs it unchanged (`load_backend("flir_sim")`).

Importing this module loads no DLL and creates no SDK state. The Spinnaker C
library loads when a `FlirBackend` is built without an `api`, and a host
without it gets `FlirSdkUnavailable`, an `ImportError`, from the constructor.
One `SpinC` serves the whole process, and cameras are opened in the process
that grabs from them, so a spawned child builds its own backend.

SETTINGS
A FLIR camera has no settings file. The profile's `camera:` block names every
recording setting, and everything it does not name comes from the user set it
names (`camera.flir.user_set`, the factory `Default` unless told otherwise).
`open()` checks each value against what this camera reports (node ranges,
increments and entries) and refuses a value outside them with a message that
names the camera, its serial and model, and the node. Nothing is looked up in
a table of models.

BLOCK IDS
The contract's numbering is 1 for the first trigger after `StartGrabbing`.
The self-test at open runs free-run acquisitions three times and records the
first frame ID of each:
  - all 1: the ID restarts 1-based, and is passed through;
  - all 0: it restarts 0-based, and every ID gets 1 added;
  - anything else: it does not restart. Each camera would then start a
    recording from whatever its preview left, so frame IDs cannot align the
    cameras, and the trigger-counter source is used when the camera has one.
A frame ID that restarts but does not step by 1 between free-run frames does
not count frames, and is treated as one that does not restart.
With `camera.flir.block_id_source: trigger_counter` the ID is the camera's own
count of rising edges on its trigger line, carried in each image's
`CounterValue` chunk. A trigger the camera ignores is then a gap, which
kick-out and post-hoc alignment already drop. `auto` uses the frame ID when
the self-test proves it and the trigger counter otherwise, because the chunk
counter's timing relative to the edge it counts is not yet measured on a real
camera.
A GigE camera without extended IDs has a 16-bit frame ID that wraps after
65535 or 65536 frames, depending on whether the counter uses 0. This backend
unwraps it, deciding each wrap from the device clock (`_bid_frame_id16`).

TIMESTAMPS
The same self-test checks that the device clock is not zero, advances at the
frame rate the camera reports (so it is in nanoseconds, or in ticks that a
tick node converts), and keeps running across an acquisition restart, which
the stall resync depends on. A camera that reports no frame rate has its
clock unit judged against the host clock instead. A camera that fails any of
these checks is refused.

TRIGGER WITNESS
When the camera has counters, Counter0 counts the edges on the trigger line
and Counter1 the exposures it started, both reset when a triggered stream is
first armed. After the acquisition, edges minus exposures is the number of
triggers the camera ignored, a direct count that `acquisition_warnings`
reports for WARNINGS.txt. Edges that reach the camera while a stall re-arm
has the stream down are left out of that count (`_note_restart_counters`).

UNKNOWNS
Several behaviours are unknown until a volunteer's probe measures them on
real cameras; each is marked where the code depends on it: whether the
ExposureTime maximum follows AcquisitionFrameRate, the spelling of chunk
names, the CounterValue chunk's timing, whether a model's counters can be
read while it streams, and which temperature status and threshold nodes a
model has.
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from pathlib import Path

from gui_app.backends._spinc import (
    SPINNAKER_ERR_ACCESS_DENIED, SPINNAKER_ERR_RESOURCE_IN_USE, FlirError,
    FlirSdkUnavailable, FlirTimeout, SpinC)

__all__ = ["FlirBackend", "FlirCamera", "FlirConfigError", "FlirDevice",
           "FlirFrameError", "FlirResult", "FlirSdkUnavailable", "FlirTimeout",
           "GRAB_STRATEGY", "shared_api"]

#: The only buffer handling mode this backend runs. OldestFirst makes a grab
#: loop that falls behind the trigger rate show as increasingly stale frames,
#: which the delivery-lag signal reports, instead of as frames that vanish.
GRAB_STRATEGY = "OldestFirst"

#: Free-run acquisitions the self-test runs, frames it takes from each, and
#: the rate it asks for. Three restarts tell a counter that restarts from one
#: that keeps counting; three frames give two intervals per restart for the
#: timestamp unit.
SELFTEST_CYCLES = 3
SELFTEST_FRAMES = 3
SELFTEST_FPS = 30.0
#: Host buffers during the self-test. The recording pool is allocated after
#: it, so the self-test's restarts do not allocate max_num_buffer frames each.
SELFTEST_BUFFERS = 10
#: How far the device clock's frame interval may differ from the frame period
#: the camera reports before the clock counts as another unit. A 125 MHz
#: tick clock reported raw is 8 times off, so 5% separates the cases.
TS_UNIT_TOL = 0.05
#: A self-test restart must advance the device clock by at least this share
#: of the host's time between the two frames. A clock that restarts on
#: BeginAcquisition goes backwards instead.
TS_CONTINUITY_SHARE = 0.5
#: A camera that reports no frame rate has its clock unit judged against the
#: host clock, over free-run frames spanning at least SELFTEST_HOST_S of host
#: time (at most SELFTEST_HOST_MAX_FRAMES frames). A few ms of delivery
#: jitter is then about 1% of the span, and TS_HOST_TOL still separates
#: nanoseconds from any tick unit (125 MHz ticks are 8 times off).
SELFTEST_HOST_S = 0.5
SELFTEST_HOST_MAX_FRAMES = 5000
TS_HOST_TOL = 0.2

#: Share of the trigger period an "auto" link throughput limit gives one
#: frame's transfer. Spreading the transfer over most of the period keeps the
#: cameras that share a trigger from bursting onto the link together.
LINK_AUTO_SHARE = 0.6

#: Trigger selectors other than FrameStart. Each is switched off when the
#: camera offers it, because any armed trigger holds back free-run frames.
OTHER_TRIGGER_SELECTORS = ("AcquisitionStart", "FrameBurstStart")

#: Counter selectors used for the trigger witness: edges on the trigger line,
#: and exposures started.
EDGE_COUNTER = "Counter0"
EXPOSURE_COUNTER = "Counter1"

#: Chunk names are tried in this order until the SDK accepts one; the
#: spelling Spinnaker's C chunk accessor takes is not documented.
CHUNK_SPELLINGS = {"Timestamp": ("Timestamp", "ChunkTimestamp"),
                   "CounterValue": ("CounterValue", "ChunkCounterValue")}

#: 16-bit frame-ID cycles. A GigE Vision block ID runs 1..65535 and skips 0;
#: a plain 16-bit counter runs 0..65535.
_ID16_SKIP0 = 65535
_ID16_FULL = 65536
#: A 16-bit wrap is resolved only when the device clock puts the two frames
#: across it within this share of a period of a whole number of periods, the
#: bound `grab_thread._resync_offset` uses for the same kind of decision.
ID16_RESID = 0.25

#: Transport-layer stream counters `stream_stats` reports.
STREAM_STAT_NODES = (
    "StreamStartedFrameCount", "StreamReceivedFrameCount",
    "StreamDeliveredFrameCount", "StreamIncompleteFrameCount",
    "StreamLostFrameCount", "StreamDroppedFrameCount",
    "StreamMissedPacketCount", "StreamPacketResendRequestCount",
    "StreamPacketResendRequestedPacketCount",
    "StreamPacketResendReceivedPacketCount")

#: `CANONICAL_STREAM_STATS` key -> the Spinnaker counter it repeats.
CANONICAL_STATS = (("buffers_total", "StreamStartedFrameCount"),
                   ("buffers_failed", "StreamIncompleteFrameCount"),
                   ("buffers_underrun", "StreamLostFrameCount"),
                   ("resend_requests", "StreamPacketResendRequestCount"))

#: Temperature status words, as SFNC and Basler name them, in the vocabulary
#: the thermal watch reads: "Ok", "Critical" (hot, still acquiring) and
#: "Error" (over temperature). A word not listed is passed through, and the
#: watch treats it as over temperature.
_TEMP_STATUS_WORDS = {"normal": "Ok", "ok": "Ok", "high": "Critical",
                      "critical": "Critical", "exceeded": "Error",
                      "error": "Error"}
#: DeviceTemperatureStatusTransition entries -> the thermals() key they fill.
_TEMP_TRANSITIONS = (("NormalToHigh", "temp_critical_c"),
                     ("HighToExceeded", "temp_shutdown_c"))


# ------------------------------------------------------------ the shared SDK
_SHARED_LOCK = threading.Lock()
_SHARED_API = None


def shared_api(sdk_dir=None) -> SpinC:
    """The process's `SpinC`, loaded on first use.

    One DLL and one Spinnaker system per process: the SDK hands out camera
    handles per system, and a second `SpinC` would hold a second reference
    to the same cameras. `sdk_dir` is used only by the call that loads the
    library; later calls return the library already loaded, wherever it came
    from, and `FlirBackend.open` refuses a profile whose `camera.flir.sdk_dir`
    names another folder. A failed load is not remembered, so a later call
    tries again once the SDK is installed.
    """
    global _SHARED_API
    with _SHARED_LOCK:
        if _SHARED_API is None:
            _SHARED_API = SpinC(sdk_dir)
        return _SHARED_API


# ------------------------------------------------------------------- errors
class FlirConfigError(RuntimeError):
    """A camera setting that cannot be applied. The message names the camera
    by serial and model, the node, and the value the camera would accept."""


class FlirFrameError(RuntimeError):
    """A delivered image the capture path cannot take. Raised from
    `retrieve()`, after the image is released, for a layout the (H, W) view
    cannot describe (a wider pixel, a stride that is not the width, another
    size), and from `BlockID` for a 16-bit frame ID whose wrap the device
    clock cannot decide. The grab loop counts it as a frame error and retires
    the camera if every frame has it."""


def _fmt_rate(bytes_per_s: float) -> str:
    return f"{bytes_per_s / 1e6:.1f} MB/s"


def _ipv4(value) -> str | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0 or v >= 1 << 32:
        return None
    return ".".join(str((v >> s) & 0xFF) for s in (24, 16, 8, 0))


def _nearest_steps(value: int, lo: int, inc: int) -> tuple:
    below = lo + ((value - lo) // inc) * inc
    return below, below + inc


# ---------------------------------------------------------------- nodemaps
class _Nodes:
    """Typed access to one camera's nodemaps, with messages that name the
    camera and the node. Node handles are looked up once and kept while the
    camera is initialised."""

    def __init__(self, api, handle, who: str):
        self.api = api
        self.who = who
        self._handle = handle
        self._maps: dict = {}
        self._cache: dict = {}

    def _map(self, which: str):
        m = self._maps.get(which)
        if m is None:
            m = self._maps[which] = self.api.nodemap(self._handle, which)
        return m

    def node(self, name: str, which: str = "device"):
        """The node's handle, or None when this camera has no such node."""
        key = (which, name)
        try:
            return self._cache[key]
        except KeyError:
            h = self.api.node(self._map(which), name)
            self._cache[key] = h
            return h

    def has(self, name: str, which: str = "device") -> bool:
        h = self.node(name, which)
        return h is not None and self.api.node_available(h)

    def readable(self, name: str, which: str = "device") -> bool:
        h = self.node(name, which)
        return h is not None and self.api.node_readable(h)

    def writable(self, name: str, which: str = "device") -> bool:
        h = self.node(name, which)
        return h is not None and self.api.node_writable(h)

    def refuse(self, text: str):
        raise FlirConfigError(f"{self.who}: {text}")

    def need(self, name: str, which: str = "device", why: str = ""):
        h = self.node(name, which)
        if h is None:
            self.refuse(f"this camera has no {name} node"
                        + (f", which {why}" if why else ""))
        return h

    # -- reads
    def geti(self, name, which="device") -> int:
        return int(self.api.int_get(self.need(name, which)))

    def getf(self, name, which="device") -> float:
        return float(self.api.float_get(self.need(name, which)))

    def getb(self, name, which="device") -> bool:
        return bool(self.api.bool_get(self.need(name, which)))

    def gete(self, name, which="device") -> str:
        return str(self.api.enum_get_symbolic(self.need(name, which)))

    def gets(self, name, which="device") -> str:
        return str(self.api.string_get(self.need(name, which)))

    def entries(self, name, which="device") -> list:
        h = self.node(name, which)
        return [] if h is None else list(self.api.enum_entries(h))

    def rangei(self, name, which="device") -> tuple:
        h = self.need(name, which)
        api = self.api
        return int(api.int_min(h)), int(api.int_max(h)), int(api.int_inc(h) or 1)

    def rangef(self, name, which="device") -> tuple:
        h = self.need(name, which)
        return float(self.api.float_min(h)), float(self.api.float_max(h))

    # -- writes. Each names the camera and the node when the camera refuses.
    def _write(self, name, which, value, fn):
        h = self.need(name, which)
        if not self.api.node_writable(h):
            self.refuse(f"{name} is not writable now, so it cannot be set to "
                        f"{value!r}")
        try:
            fn(h, value)
        except FlirError as e:
            raise FlirConfigError(
                f"{self.who}: setting {name} to {value!r} failed: {e}") from e

    def seti(self, name, value, which="device") -> None:
        self._write(name, which, int(value), self.api.int_set)

    def setf(self, name, value, which="device") -> None:
        self._write(name, which, float(value), self.api.float_set)

    def setb(self, name, value, which="device") -> None:
        self._write(name, which, bool(value), self.api.bool_set)

    def sete(self, name, value, which="device", field="") -> None:
        """Select an enumeration entry, refusing one the camera does not
        offer with the entries it does. `field` names the profile key the
        value came from."""
        offered = self.entries(name, which)
        if value not in offered:
            src = f"{field} {value!r}" if field else repr(value)
            self.refuse(f"{src} is not an entry of this camera's {name}; it "
                        f"offers {', '.join(offered) or 'nothing'}")
        self._write(name, which, value, self.api.enum_set_symbolic)

    def execute(self, name, which="device") -> None:
        h = self.need(name, which)
        try:
            self.api.command(h)
        except FlirError as e:
            raise FlirConfigError(
                f"{self.who}: executing {name} failed: {e}") from e


# ------------------------------------------------------------------ devices
class FlirDevice:
    """An entry from `enumerate_devices()`: a camera handle and what the
    transport layer says about it, read before the camera is initialised."""

    __slots__ = ("handle", "serial", "model", "vendor", "interface", "ip",
                 "released")

    def __init__(self, handle, serial: str, model: str, vendor: str,
                 interface: str, ip):
        self.handle = handle
        self.serial = serial
        self.model = model
        self.vendor = vendor
        #: "GigE", "USB3" or the SDK's own DeviceType word for anything else.
        self.interface = interface
        self.ip = ip
        self.released = False

    def GetSerialNumber(self) -> str:
        return self.serial

    def GetModelName(self) -> str:
        return self.model

    @property
    def label(self) -> str:
        return f"camera {self.serial} ({self.model or 'unknown model'})"

    def __repr__(self) -> str:
        return f"<FlirDevice {self.serial} {self.model} {self.interface}>"


# ------------------------------------------------------------ grab results
class _ZeroCopyView:
    """`with result.GetArrayZeroCopy() as img:` over the driver buffer.

    A class, not a generator: entering and leaving it is two method calls on
    the hot path. The view is valid until the result is released, and
    `Release()` refuses while it is open."""

    __slots__ = ("_res",)

    def __init__(self, res):
        self._res = res

    def __enter__(self):
        res = self._res
        if res._released:
            raise RuntimeError(f"{res._cam.who}: zero-copy view taken after "
                               f"Release(); the buffer belongs to the driver")
        cam = res._cam
        view = res._api.image_view(res._img, cam.width, cam.height)
        res._view_open = True
        return view

    def __exit__(self, *exc):
        self._res._view_open = False
        return False


class FlirResult:
    """One image, satisfying `GrabResultProtocol`.

    Each attribute is one Spinnaker call on the image, made when it is read.
    `BlockID` and `TimeStamp` go through the functions the camera chose at
    open (frame ID or trigger counter; image or chunk timestamp, in ns). On
    a camera with 16-bit frame IDs, `BlockID` also reads the timestamp, which
    decides the wraps."""

    __slots__ = ("_api", "_cam", "_img", "_released", "_view_open", "_pad")

    def __init__(self, cam, img):
        self._api = cam._api
        self._cam = cam
        self._img = img
        self._released = False
        self._view_open = False
        self._pad = None

    def GrabSucceeded(self) -> bool:
        return not self._api.image_incomplete(self._img)

    @property
    def ErrorCode(self) -> int:
        return self._api.image_status(self._img)

    @property
    def ErrorDescription(self) -> str:
        return self._api.image_status_text(self._img)

    @property
    def BlockID(self) -> int:
        return self._cam._bid(self._img)

    @property
    def TimeStamp(self) -> int:
        return self._cam._ts(self._img)

    @property
    def PaddingX(self) -> int:
        if self._pad is None:
            self._pad = self._api.image_padding(self._img)
        return self._pad[0]

    @property
    def PaddingY(self) -> int:
        if self._pad is None:
            self._pad = self._api.image_padding(self._img)
        return self._pad[1]

    def GetArrayZeroCopy(self):
        return _ZeroCopyView(self)

    def Release(self) -> None:
        """Return the buffer to the pool. A second call raises, because
        releasing an image twice is undefined on the SDK."""
        if self._released:
            raise RuntimeError(f"{self._cam.who}: Release() called twice on "
                               f"one image")
        if self._view_open:
            raise RuntimeError(f"{self._cam.who}: Release() inside the "
                               f"zero-copy view; the view must not outlive "
                               f"the buffer")
        self._released = True
        self._api.image_release(self._img)


# ------------------------------------------------------------ open cameras
class FlirCamera:
    """One open camera: the handle `open()` returns (`CameraHandleProtocol`),
    and every choice the backend made for it.

    Attributes a caller may read: `serial`, `model`, `interface`, `width`,
    `height`, `block_id_source` ("frame_id" or "trigger_counter"),
    `ts_source` ("image" or "chunk"), `ts_scale` (ns per camera unit),
    `selftest` (what the self-test measured) and `applied` (the values open()
    set, read back)."""

    def __init__(self, backend, device: FlirDevice, spec, max_num_buffer: int):
        self._backend = backend
        self._api = backend._api
        self.device = device
        self.handle = device.handle
        self.serial = device.serial
        self.model = device.model
        self.interface = device.interface
        self.who = device.label
        self.spec = spec
        self.max_num_buffer = int(max_num_buffer)
        self.nodes = _Nodes(self._api, self.handle, self.who)
        self.width = 0
        self.height = 0
        self.is_open = True
        self.gige = self.interface == "GigE"
        self.extended_ids = not self.gige
        self.id16 = False
        self.trigger_line = spec.trigger.line
        self.block_id_source = "frame_id"
        self.id_offset = 0
        self.ts_source = "image"
        self.ts_scale = 1
        self.ts_chunk_name = None
        self.selftest: dict = {}
        self.applied: dict = {}
        #: Counter selector -> what it counts ("edges" or "exposures").
        self.counters: dict = {}
        self.counter_chunk_ok = False
        self.temp_max_c = None
        self._ceilings: dict = {}
        self._link_fps = None
        self._triggered = False
        self._arm_pending = False
        self._grabbing = False
        self._verify = True
        # Frame-ID unwrap state, reset by every StartGrabbing: the previous
        # frame's raw ID and timestamp, the first frame of the arm as
        # (block ID, timestamp), and why the ordinal was lost, if it was.
        self._prev_raw = None
        self._prev_ts = None
        self._id_acc = 0
        self._id_ref = None
        self._id_lost = None
        # Trigger-counter state, reset with the counter.
        self._ctr_prev = None
        self._ctr_acc = 0
        self._ctr_period = 0
        self._ctr_latch = 0
        self._ctr_first = True
        self._ctr_latch_known = False
        self._ctr_name = None
        self._last_counter = None
        # Whether the frame ID restarts at every arm and counts frames, and
        # what makes it 1-based; the latch decision uses it.
        self._fid_evidence = False
        self._fid_base_offset = 0
        #: The witness for the current triggered arm, filled at stops.
        self.witness = None
        # With an edge counter and no exposure counter, a frame_id camera
        # keeps its last block ID per arm: frames acquired through the last
        # frame that reached the host (`_bid_tracked`).
        self._track_ids = False
        self._arm_last_bid = 0
        self._bid_inner = None
        self._bid = self._bid_frame_id
        self._ts = self._ts_image

    def __repr__(self) -> str:
        return f"<FlirCamera {self.serial} {self.model}>"

    # ---------------------------------------------------- CameraHandleProtocol
    def StartGrabbing(self, strategy=GRAB_STRATEGY) -> None:
        """Arm the stream (BeginAcquisition).

        The frame ID restarts here, so its unwrap state does too. The first
        arm after `set_triggered` also resets the trigger counters while the
        board is still stopped, so they count this recording's edges from 0.
        A re-arm after a stall does not reset them. It reads them again once
        BeginAcquisition has returned, so the edges that arrived while the
        stream was down are not counted as ignored triggers."""
        if strategy != GRAB_STRATEGY:
            raise ValueError(f"{self.who}: grab strategy {strategy!r}; the "
                             f"FLIR backend runs {GRAB_STRATEGY} only")
        if not self.is_open:
            raise RuntimeError(f"{self.who}: StartGrabbing on a closed camera")
        rearm = False
        if self._triggered:
            if self._arm_pending:
                self._reset_counters()
                self._arm_pending = False
            else:
                rearm = True
        self._prev_raw = None
        self._prev_ts = None
        self._id_acc = 0
        self._id_ref = None
        self._id_lost = None
        self._arm_last_bid = 0
        self._verify = True
        self._api.begin(self.handle)
        self._grabbing = True
        if rearm:
            self._note_restart_counters()

    def StopGrabbing(self) -> None:
        """End the stream (EndAcquisition). Every image must be released
        first, which the grab loop guarantees. The trigger counters are read
        here, just before EndAcquisition while the camera still acquires, so
        a re-arm's down-time window starts no later than the stream stops. A
        camera whose counters cannot be read while it streams is read after
        EndAcquisition instead."""
        was = self._grabbing
        self._grabbing = False
        streaming = self._api.is_streaming(self.handle)
        before_end = None
        if was and self._triggered and streaming:
            before_end = self._try_read_counters()
        if streaming:
            self._api.end(self.handle)
        if was and self._triggered:
            self._read_counters_at_stop(before_end)

    def IsGrabbing(self) -> bool:
        return self._grabbing

    # ------------------------------------------------------------ block IDs
    def _bid_frame_id(self, img) -> int:
        return self._api.image_frame_id(img)

    def _bid_frame_id_plus1(self, img) -> int:
        return self._api.image_frame_id(img) + 1

    def _bid_frame_id16(self, img) -> int:
        """A 16-bit frame ID unwrapped into a value that does not wrap.

        The counter's cycle is 65535 (1..65535 as a GigE Vision block ID, or
        0..65534) or 65536 (0..65535), and the first ID after a wrap does not
        tell which when a frame is lost at the wrap. So each wrap is decided
        by the device clock: `_id16_cycle` counts the trigger periods since
        the previous frame and takes the cycle that gives that many IDs. The
        timestamp read here is one more SDK call per frame, made only on a
        GigE camera without extended IDs.

        A wrap the clock cannot decide leaves the arm without an ordinal:
        this image and every later one of the arm raise FlirFrameError, and
        the grab loop retires the camera after its consecutive-error limit.
        """
        lost = self._id_lost
        if lost is not None:
            raise FlirFrameError(lost)
        raw = self._api.image_frame_id(img)
        ts = self._ts(img)
        prev = self._prev_raw
        if prev is None:
            self._id_ref = (raw + self.id_offset + self._id_acc, ts)
        elif raw < prev - _ID16_FULL // 2:
            self._id_acc += self._id16_cycle(prev, raw, ts)
        self._prev_raw = raw
        self._prev_ts = ts
        return raw + self.id_offset + self._id_acc

    def _id16_cycle(self, prev: int, raw: int, ts: int) -> int:
        """The cycle of the wrap from raw ID `prev` to `raw` (at device time
        `ts`, in ns), or FlirFrameError.

        The frame rate is this arm's own: unwrapped IDs over device time,
        from the arm's first frame to the frame before the wrap. It counts
        the periods between the two frames, which must land within
        ID16_RESID of a whole number, and exactly one cycle must give that
        many IDs. A trigger the camera ignored at the wrap makes both
        cycles miss, and then no ordinal is guessed."""
        ref_bid, ref_ts = self._id_ref
        prev_bid = prev + self.id_offset + self._id_acc
        prev_ts = self._prev_ts
        if prev_bid <= ref_bid or prev_ts <= ref_ts:
            problem = ("this acquisition has no earlier frame to measure its "
                       "frame rate from")
        else:
            periods = (ts - prev_ts) * (prev_bid - ref_bid) / (prev_ts - ref_ts)
            k = round(periods)
            if k < 1 or abs(periods - k) > ID16_RESID:
                problem = (f"the device clock puts {periods:.2f} frame periods "
                           f"between them, not a whole number")
            else:
                for cycle in (_ID16_SKIP0, _ID16_FULL):
                    if raw - prev + cycle == k:
                        print(f"[flir] {self.serial}: 16-bit frame ID wrapped "
                              f"from {prev} to {raw}; the device clock counts "
                              f"{k} frame period(s), so the cycle is {cycle}",
                              flush=True)
                        return cycle
                problem = (f"the device clock counts {k} frame period(s) "
                           f"between them, which neither a 65535 nor a 65536 "
                           f"cycle gives (an ignored trigger at the wrap does "
                           f"this)")
        self._id_lost = (
            f"{self.who}: the 16-bit frame ID wrapped from {prev} to {raw}, "
            f"and {problem}, so this frame and every later one of this "
            f"acquisition have no trigger ordinal. Set camera.flir."
            f"extended_ids: true if the camera offers GevGVSPExtendedIDMode.")
        print(f"[flir] {self._id_lost}", flush=True)
        raise FlirFrameError(self._id_lost)

    def _bid_tracked(self, img) -> int:
        """The block ID, kept as this arm's last. A frame ID restarts at
        every arm, so the last one is the number of frames the camera
        acquired through that frame, whatever was lost before it."""
        v = self._bid_inner(img)
        self._arm_last_bid = v
        return v

    def _bid_counter(self, img) -> int:
        """The trigger ordinal from the image's CounterValue chunk: rising
        edges on the trigger line since the counter was reset at arm.

        The chunk is expected to carry the count including the edge that
        triggered the image, so the first trigger reads 1. The recording's
        first image decides whether the camera latches the count before the
        edge instead (`_first_counter_image`); from then on 1 is added, and
        the log says so. A counter narrower than 2**31 is unwrapped here."""
        name = self._ctr_name
        if name is None:
            v, name = _read_chunk(self._api, img, "CounterValue")
            self._ctr_name = name
        else:
            v = self._api.chunk_int(img, name)
        if self._ctr_first:
            self._ctr_first = False
            self._first_counter_image(img, v)
        period = self._ctr_period
        if period:
            prev = self._ctr_prev
            if prev is not None and v < prev - period // 2:
                self._ctr_acc += period
            self._ctr_prev = v
        self._last_counter = v
        return v + self._ctr_latch + self._ctr_acc

    def _first_counter_image(self, img, v: int) -> None:
        """Decide from the recording's first image, whose chunk count is
        `v`, whether the camera latches CounterValue before the edge.

        A count that includes the edge is at least the image's ordinal among
        the frames the camera acquired, and a count latched before the edge
        is at least one less. So 0 proves the latch, and so does a count
        below the frame ID on a camera whose frame ID restarts at every arm
        and counts frames; a count at or above that frame ID shows the
        count includes the edge. Without such a frame ID a count above 0
        decides nothing (the first trigger's frame may have been lost), and
        `_latch_sentences` checks the last image at stop instead. The frame
        ID is one more SDK call, on this image only."""
        if v == 0:
            fid = None
        elif self._fid_evidence:
            fid = self._api.image_frame_id(img) + self._fid_base_offset
        else:
            return
        self._ctr_latch_known = True
        if fid is not None and v >= fid:
            return
        self._ctr_latch = 1
        print(f"[flir] {self.serial}: the first image's CounterValue chunk "
              f"reads {v}" + (f" on the camera's frame {fid}"
                              if fid is not None else "")
              + ", so this camera latches the counter before the trigger "
                "edge; its block IDs are the chunk value plus 1", flush=True)

    # ----------------------------------------------------------- timestamps
    def _ts_image(self, img) -> int:
        ts = self._api.image_timestamp(img)
        if not ts:
            raise ValueError(f"{self.who}: the image timestamp is 0")
        return ts

    def _ts_image_scaled(self, img) -> int:
        ts = self._api.image_timestamp(img)
        if not ts:
            raise ValueError(f"{self.who}: the image timestamp is 0")
        return ts * self.ts_scale

    def _ts_chunk(self, img) -> int:
        ts = self._api.chunk_int(img, self.ts_chunk_name)
        if not ts:
            raise ValueError(f"{self.who}: the Timestamp chunk is 0")
        return ts * self.ts_scale

    def _choose_hot_path(self) -> None:
        """Bind the BlockID and TimeStamp functions for the chosen sources."""
        if self.block_id_source == "trigger_counter":
            self._bid = self._bid_counter
        elif self.id16:
            self._bid = self._bid_frame_id16
        elif self.id_offset:
            self._bid = self._bid_frame_id_plus1
        else:
            self._bid = self._bid_frame_id
        self._track_ids = (self.block_id_source == "frame_id"
                           and set(self.counters.values()) == {"edges"})
        if self._track_ids:
            self._bid_inner, self._bid = self._bid, self._bid_tracked
        if self.ts_source == "chunk":
            self._ts = self._ts_chunk
        elif self.ts_scale != 1:
            self._ts = self._ts_image_scaled
        else:
            self._ts = self._ts_image

    # ------------------------------------------------------ trigger witness
    def _counter_value(self, selector: str) -> int:
        n = self.nodes
        n.sete("CounterSelector", selector)
        return n.geti("CounterValue")

    def _read_counters(self, exposures_first: bool = False) -> dict:
        """Every witness counter, read one after the other. A re-arm window
        starts with a read of the edges first and ends with one of the
        exposures first, so an edge that lands between the two reads of
        either is counted as down time. That can hide an ignored trigger,
        never invent one."""
        items = list(self.counters.items())
        if exposures_first:
            items.reverse()
        return {what: self._counter_value(sel) for sel, what in items}

    def _reset_counters(self) -> None:
        """Reset the witness counters for a new triggered arm.

        In trigger-counter mode the counter is the block ID, so a reset that
        fails refuses the arm. Otherwise the witness is diagnostic: a failed
        reset is logged and this arm has no witness."""
        self.witness = None
        self._ctr_prev = None
        self._ctr_acc = 0
        self._ctr_latch = 0
        self._ctr_first = True
        self._ctr_latch_known = False
        self._last_counter = None
        if not self.counters:
            return
        try:
            for sel in self.counters:
                self.nodes.sete("CounterSelector", sel)
                self.nodes.execute("CounterReset")
        except Exception as e:
            if self.block_id_source == "trigger_counter":
                raise
            print(f"[flir] {self.serial}: trigger counters could not be reset "
                  f"({type(e).__name__}: {e}); this acquisition has no "
                  f"trigger witness", flush=True)
            return
        self.witness = {"edges": None, "exposures": None, "gap_edges": 0,
                        "stopped_at": None, "error": None, "rearms": 0,
                        "id_frames": 0}

    def _ctr_delta(self, later: int, earlier: int) -> int:
        """`later - earlier` for two reads of one counter, across a wrap of
        a counter narrower than 2**31."""
        period = self._ctr_period
        return (later - earlier) % period if period else later - earlier

    def _note_restart_counters(self) -> None:
        """Leave the edges of a re-arm's down time out of the witness.

        Called once BeginAcquisition has returned. The window runs from the
        read at StopGrabbing to this read, so it covers all the time the
        camera could not expose, BeginAcquisition included. An edge the
        camera exposed inside the window is in both counters' increase, so
        edges minus exposures over the window is the down-time edges. With
        no exposure counter every edge in the window is left out, which can
        only hide an ignored trigger, never report a false one."""
        w = self.witness
        if w is None or w["error"] or w["stopped_at"] is None:
            return
        try:
            now = self._read_counters(exposures_first=True)
        except Exception as e:
            w["error"] = f"{type(e).__name__}: {e}"
            return
        before = w["stopped_at"]
        down = self._ctr_delta(now["edges"], before["edges"])
        if "exposures" in now and "exposures" in before:
            down -= self._ctr_delta(now["exposures"], before["exposures"])
        w["gap_edges"] += max(0, down)
        w["rearms"] += 1

    def _try_read_counters(self):
        """The witness counters now, or None when there is no witness or
        the read fails."""
        w = self.witness
        if w is None or w["error"]:
            return None
        try:
            return self._read_counters()
        except Exception:
            return None

    def _read_counters_at_stop(self, now=None) -> None:
        """Record the counters at a stop. `now` is the read StopGrabbing made
        before EndAcquisition, or None to read them now. The arm's last
        block ID is banked here too."""
        w = self.witness
        if w is None:
            return
        w["id_frames"] += self._arm_last_bid
        self._arm_last_bid = 0
        if w["error"]:
            return
        if now is None:
            try:
                now = self._read_counters()
            except Exception as e:
                w["error"] = f"{type(e).__name__}: {e}"
                return
        w["stopped_at"] = now
        w["edges"] = now.get("edges")
        w["exposures"] = now.get("exposures")


def _read_chunk(api, img, key: str) -> tuple:
    """`(value, spelling)` of an integer chunk, trying each spelling in
    `CHUNK_SPELLINGS` until the SDK accepts one."""
    last = None
    for name in CHUNK_SPELLINGS[key]:
        try:
            return api.chunk_int(img, name), name
        except FlirError as e:
            last = e
    raise last


# ------------------------------------------------------------------ backend
class FlirBackend:
    """`CameraBackend` for FLIR / Teledyne cameras through the Spinnaker C
    API.

    `api` is the object that answers the `SpinC` methods: None loads the
    Spinnaker C library for this process (see `shared_api`), and a
    `FakeSpinC` gives the simulated rig. `sdk_dir` is where to look for the
    library first, when this call is the one that loads it."""

    name = "flir"
    TimeoutException = FlirTimeout
    GRAB_STRATEGY = GRAB_STRATEGY

    #: Camera-side transport settings `stream_stats` reports, which stay
    #: readable after the stream stops.
    TRANSPORT_NODES = ("DeviceLinkThroughputLimit", "DeviceLinkSpeed",
                       "GevSCPSPacketSize", "GevSCPD", "StreamMode")

    #: Advice clauses for `frame_sync.check_block_id_rate` on a FLIR rig, in
    #: the shape `SyncEncodeRouter(rate_hints=...)` takes.
    BLOCK_RATE_HINTS = {
        "ceiling_hint": ("camera.exposure_us must stay under the exposure "
                         "ceiling the camera reports at the frame rate (the "
                         "'[camN] exposure=... ceiling' log line), and "
                         "camera.trigger.overlap should be ReadOut"),
        "timestamp_hint": ("the FLIR backend measures the timestamp unit at "
                           "open and logs it as ts=... on each camera's "
                           "'[flir]' line"),
    }

    def __init__(self, api=None, sdk_dir=None):
        self._api = shared_api(sdk_dir) if api is None else api
        self._lock = threading.Lock()
        self._enumerated: list = []
        self._open: list = []
        #: Self-test results by (serial, camera.flir.timestamp_source).
        #: Frame-ID and clock behaviour belong to the camera's model and
        #: firmware, so a camera re-opened in the same process with the same
        #: timestamp choice is not tested again.
        self._selftests: dict = {}

    @property
    def api(self):
        """The `SpinC` (or `FakeSpinC`) this backend drives."""
        return self._api

    # ------------------------------------------------------------ discovery
    def enumerate_devices(self) -> list:
        """Every camera the SDK detects, sorted by serial number as text.

        Handles from an earlier enumeration that no open camera uses are
        released first. FLIR serials vary in length, so the text order puts
        "10000000" before "9999999"; the profile's camera_serials are
        compared in the same order."""
        api = self._api
        with self._lock:
            in_use = {id(c.device) for c in self._open}
            for dev in self._enumerated:
                if id(dev) in in_use or dev.released:
                    continue
                dev.released = True
                try:
                    api.release(dev.handle)
                except FlirError as e:
                    print(f"[flir] releasing {dev.serial}'s old handle "
                          f"failed: {e}", flush=True)
            devices = [self._describe_device(h) for h in api.cameras()]
            devices.sort(key=lambda d: d.serial)
            self._enumerated = list(devices)
            return devices

    def _describe_device(self, handle) -> FlirDevice:
        api = self._api
        tl = api.nodemap(handle, "tldevice")

        def text(name):
            node = api.node(tl, name)
            return "" if node is None else str(api.string_get(node))

        serial = str(api.camera_serial(handle))
        dtype_node = api.node(tl, "DeviceType")
        dtype = "" if dtype_node is None else str(api.enum_get_symbolic(dtype_node))
        interface = {"GigEVision": "GigE", "USB3Vision": "USB3"}.get(dtype,
                                                                     dtype)
        ip = None
        ip_node = api.node(tl, "GevDeviceIPAddress")
        if ip_node is not None:
            ip = _ipv4(api.int_get(ip_node))
        return FlirDevice(handle, serial, text("DeviceModelName"),
                          text("DeviceVendorName"), interface, ip)

    @staticmethod
    def device_address(device):
        """The camera's IPv4 address as dotted text, or None (USB3). Never
        raises."""
        try:
            return device.ip
        except Exception:
            return None

    # ------------------------------------------------------------- opening
    def open(self, device, pfs_path: str, max_num_buffer: int,
             camera_spec=None, *, frame_size=None, frame_rate=None):
        """Open, configure and self-test one camera. Returns a `FlirCamera`.

        `camera_spec` is the profile's `camera:` block and is required: it is
        the only source of the recording exposure, gain and trigger line.
        `pfs_path` must be empty. `frame_size` (the profile's frame_width and
        frame_height) programs the ROI, and `frame_rate` lets open() size an
        "auto" link limit and check the exposure against the camera's ceiling
        at that rate. Without them the ROI stays as the user set left it
        (camera_manager then refuses a camera whose size differs from the
        profile), and both rate checks run at the first acquisition, in
        `exposure_ceiling_us`.

        Every refusal is a FlirConfigError naming the camera, its serial and
        model, and the node. A camera that fails part-way is deinitialised
        before the error propagates.
        """
        who = device.label
        if pfs_path:
            raise FlirConfigError(
                f"{who}: pfs_path is a Basler pylon feature file ({pfs_path}); "
                f"a flir profile describes its cameras in the camera: block. "
                f"Remove pfs_path.")
        if camera_spec is None:
            raise FlirConfigError(
                f"{who}: the flir backend takes the recording exposure, gain "
                f"and trigger line from the profile's camera: block, and this "
                f"profile has none. profiles/templates/ has examples.")
        spec = camera_spec.for_camera(device.serial)
        self._check_sdk_dir(spec, who)
        api = self._api
        try:
            api.init(device.handle)
        except FlirError as e:
            if e.code in (SPINNAKER_ERR_RESOURCE_IN_USE,
                          SPINNAKER_ERR_ACCESS_DENIED):
                raise FlirConfigError(
                    f"{who} is open in another program (Spinnaker error "
                    f"{e.code} {e.name}). Close SpinView and reselect the "
                    f"profile.") from e
            raise FlirConfigError(
                f"{who} could not be initialised ({e}). Power-cycle it and "
                f"reselect the profile.") from e
        cam = FlirCamera(self, device, spec, max_num_buffer)
        try:
            self._configure(cam, frame_size, frame_rate)
        except BaseException:
            self._deinit_after_failed_open(cam)
            raise
        with self._lock:
            self._open.append(cam)
        return cam

    def _check_sdk_dir(self, spec, who: str) -> None:
        want = getattr(spec.flir, "sdk_dir", None)
        loaded = getattr(self._api, "dll_path", "")
        if not want or not isinstance(self._api, SpinC) or not loaded:
            return
        try:
            inside = Path(want).resolve() in Path(loaded).resolve().parents
        except (OSError, ValueError):
            inside = False
        if not inside:
            raise FlirConfigError(
                f"{who}: camera.flir.sdk_dir is {want}, but this process "
                f"loaded the Spinnaker library from {loaded} when it first "
                f"enumerated cameras, before the profile's camera: block was "
                f"read. Set the PANOPTICON_SPINNAKER_DIR environment variable "
                f"to {want} and restart Panopticon, or remove "
                f"camera.flir.sdk_dir.")

    def _deinit_after_failed_open(self, cam) -> None:
        api = self._api
        cam.is_open = False
        try:
            if api.is_streaming(cam.handle):
                api.end(cam.handle)
        except Exception:
            pass
        try:
            if api.is_initialized(cam.handle):
                api.deinit(cam.handle)
        except Exception as e:
            print(f"[flir] {cam.serial}: deinit after a failed open failed: "
                  f"{e}", flush=True)

    def _configure(self, cam, frame_size, frame_rate) -> None:
        """The ordered apply. Values the camera must reach before another
        node's range is valid come first: the user set, the auto modes, the
        pixel format, then the ROI (offsets zeroed before the size), then
        the transport, the buffers, the trigger line, exposure and gain, the
        counters, and last the self-test that decides the block-ID and
        timestamp sources."""
        n = cam.nodes
        spec = cam.spec
        flir = spec.flir
        if self._api.is_streaming(cam.handle):
            self._api.end(cam.handle)
        self._load_user_set(cam, flir.user_set)
        if n.has("AcquisitionMode"):
            n.sete("AcquisitionMode", "Continuous")
        for auto in ("ExposureAuto", "GainAuto"):
            if n.has(auto):
                n.sete(auto, "Off")
        if n.has("ExposureMode"):
            n.sete("ExposureMode", "Timed")
        n.sete("PixelFormat", spec.pixel_format, field="camera.pixel_format")
        self._apply_roi(cam, frame_size)
        self._apply_image_nodes(cam)
        self._apply_transport(cam, frame_rate)
        self._apply_buffers(cam)
        self._validate_trigger(cam)
        # A frame rate the user set left enabled bounds ExposureTime; off,
        # the exposure is checked against the sensor's own range.
        if n.writable("AcquisitionFrameRateEnable"):
            n.setb("AcquisitionFrameRateEnable", False)
        self._apply_exposure_gain_at_open(cam)
        self._configure_counters(cam)
        self._self_test(cam)
        self._choose_block_id_source(cam)
        cam._choose_hot_path()
        n.seti("StreamBufferCountManual", cam.max_num_buffer, "tlstream")
        if frame_rate:
            self._check_exposure_at_rate(cam, float(frame_rate))
        cam.applied["buffers"] = n.geti("StreamBufferCountManual", "tlstream")
        self._log_summary(cam)

    # -- the steps
    def _load_user_set(self, cam, user_set: str) -> None:
        n = cam.nodes
        if user_set == "none":
            cam.applied["user_set"] = "none"
            print(f"[flir] {cam.serial}: camera.flir.user_set none: the "
                  f"baseline is whatever the camera holds", flush=True)
            return
        if n.node("UserSetSelector") is None or n.node("UserSetLoad") is None:
            n.refuse(f"camera.flir.user_set {user_set!r} needs the "
                     f"UserSetSelector and UserSetLoad nodes, which this "
                     f"camera does not have. Set camera.flir.user_set: none "
                     f"to start from the settings the camera holds.")
        n.sete("UserSetSelector", user_set, field="camera.flir.user_set")
        n.execute("UserSetLoad")
        cam.applied["user_set"] = user_set

    def _apply_roi(self, cam, frame_size) -> None:
        n = cam.nodes
        spec = cam.spec
        for off in ("OffsetX", "OffsetY"):
            if n.writable(off):
                n.seti(off, 0)
        if frame_size is not None:
            w, h = (int(v) for v in frame_size)
            self._set_size(cam, "Width", w, "frame_width")
            self._set_size(cam, "Height", h, "frame_height")
        w, h = n.geti("Width"), n.geti("Height")
        wmax = n.geti("WidthMax") if n.readable("WidthMax") else w
        hmax = n.geti("HeightMax") if n.readable("HeightMax") else h
        ox = self._offset(cam, "OffsetX", spec.offset_x, wmax, w,
                          "camera.offset_x")
        oy = self._offset(cam, "OffsetY", spec.offset_y, hmax, h,
                          "camera.offset_y")
        cam.width, cam.height = n.geti("Width"), n.geti("Height")
        cam.applied["roi"] = (cam.width, cam.height, ox, oy)
        if frame_size is None:
            cam.applied["roi_source"] = "camera"

    def _set_size(self, cam, node: str, value: int, field: str) -> None:
        n = cam.nodes
        lo, hi, inc = n.rangei(node)
        if value < lo or value > hi:
            n.refuse(f"{field} {value} is outside this camera's {node} range "
                     f"{lo} to {hi}.")
        if (value - lo) % inc:
            a, b = _nearest_steps(value, lo, inc)
            n.refuse(f"{field} {value} is not a multiple of this camera's "
                     f"{node} increment {inc} (nearest: {a} or {b}).")
        n.seti(node, value)

    def _offset(self, cam, node: str, want, size_max: int, size: int,
                field: str) -> int:
        n = cam.nodes
        if not n.has(node):
            if want not in ("center", 0):
                n.refuse(f"{field} {want} needs the {node} node, which this "
                         f"camera does not have.")
            return 0
        lo, _hi, inc = n.rangei(node)
        if want == "center":
            value = max(lo, ((size_max - size) // 2 - lo) // inc * inc + lo)
        else:
            value = int(want)
            if (value - lo) % inc:
                a, b = _nearest_steps(value, lo, inc)
                n.refuse(f"{field} {value} is not a multiple of this camera's "
                         f"{node} increment {inc} (nearest: {a} or {b}).")
            if value + size > size_max:
                n.refuse(f"{field} {value} plus the frame size {size} passes "
                         f"the sensor edge ({size_max}).")
        if value:
            n.seti(node, value)
        return value

    def _apply_image_nodes(self, cam) -> None:
        n = cam.nodes
        flir = cam.spec.flir
        if n.has("GammaEnable"):
            n.setb("GammaEnable", flir.gamma_enable)
            cam.applied["gamma_enable"] = n.getb("GammaEnable")
        elif flir.gamma_enable:
            print(f"[flir] {cam.serial}: camera.flir.gamma_enable is true but "
                  f"this camera has no GammaEnable node; left as the camera "
                  f"has it", flush=True)
        if flir.black_level is not None:
            lo, hi = n.rangef("BlackLevel")
            if not lo <= flir.black_level <= hi:
                n.refuse(f"camera.flir.black_level {flir.black_level:g} is "
                         f"outside this camera's BlackLevel range {lo:g} to "
                         f"{hi:g}.")
            n.setf("BlackLevel", flir.black_level)
            cam.applied["black_level"] = n.getf("BlackLevel")
        if flir.adc_bit_depth is not None:
            n.sete("AdcBitDepth", flir.adc_bit_depth,
                   field="camera.flir.adc_bit_depth")
            cam.applied["adc_bit_depth"] = flir.adc_bit_depth

    def _apply_transport(self, cam, frame_rate) -> None:
        n = cam.nodes
        flir = cam.spec.flir
        if not cam.gige:
            for key in ("packet_size", "packet_delay"):
                if getattr(flir, key) is not None:
                    n.refuse(f"camera.flir.{key} applies to GigE cameras, and "
                             f"this one is {cam.interface or 'not GigE'}. "
                             f"Remove it.")
            if flir.stream_mode != "auto":
                n.refuse(f"camera.flir.stream_mode {flir.stream_mode} applies "
                         f"to GigE cameras, and this one is "
                         f"{cam.interface or 'not GigE'}. Set it to auto.")
        else:
            if flir.packet_size is not None:
                self._set_int_checked(cam, "GevSCPSPacketSize",
                                      flir.packet_size,
                                      "camera.flir.packet_size")
            if flir.packet_delay is not None:
                self._set_int_checked(cam, "GevSCPD", flir.packet_delay,
                                      "camera.flir.packet_delay")
            if flir.stream_mode != "auto":
                n.sete("StreamMode", flir.stream_mode, "tlstream",
                       field="camera.flir.stream_mode")
            self._apply_extended_ids(cam, flir.extended_ids)
        self._apply_link_limit(cam, frame_rate)

    def _set_int_checked(self, cam, node, value, field) -> None:
        n = cam.nodes
        lo, hi, inc = n.rangei(node)
        if not lo <= value <= hi:
            n.refuse(f"{field} {value} is outside this camera's {node} range "
                     f"{lo} to {hi}.")
        if (value - lo) % inc:
            a, b = _nearest_steps(value, lo, inc)
            n.refuse(f"{field} {value} is not a multiple of this camera's "
                     f"{node} increment {inc} (nearest: {a} or {b}).")
        n.seti(node, value)
        cam.applied[field] = n.geti(node)

    def _apply_extended_ids(self, cam, want: bool) -> None:
        n = cam.nodes
        if not n.has("GevGVSPExtendedIDMode"):
            cam.extended_ids = False
        else:
            n.sete("GevGVSPExtendedIDMode", "On" if want else "Off")
            cam.extended_ids = n.gete("GevGVSPExtendedIDMode") == "On"
        cam.id16 = cam.gige and not cam.extended_ids

    def _apply_link_limit(self, cam, frame_rate) -> None:
        """Set DeviceLinkThroughputLimit from camera.flir.link_throughput_limit.

        "auto" gives one frame LINK_AUTO_SHARE of the trigger period, which
        needs the frame rate: without one the limit starts at the node's
        maximum and `exposure_ceiling_us` sizes it at the first acquisition.
        A number is checked against the node's range and increment and
        refused outside them; None leaves the camera's value."""
        n = cam.nodes
        want = cam.spec.flir.link_throughput_limit
        if not n.has("DeviceLinkThroughputLimit"):
            if isinstance(want, int) and not isinstance(want, bool):
                n.refuse(f"camera.flir.link_throughput_limit {want} needs the "
                         f"DeviceLinkThroughputLimit node, which this camera "
                         f"does not have.")
            return
        if n.has("DeviceLinkThroughputLimitMode") and n.writable(
                "DeviceLinkThroughputLimitMode") and want is not None:
            if "On" in n.entries("DeviceLinkThroughputLimitMode"):
                n.sete("DeviceLinkThroughputLimitMode", "On")
        lo, hi, inc = n.rangei("DeviceLinkThroughputLimit")
        if want is None:
            value = None
        elif want == "max":
            value = hi
        elif want == "auto":
            if frame_rate:
                self._size_link(cam, float(frame_rate))
                return
            value = hi
        else:
            value = int(want)
            if not lo <= value <= hi:
                n.refuse(f"camera.flir.link_throughput_limit {value} is "
                         f"outside this camera's DeviceLinkThroughputLimit "
                         f"range {lo} to {hi} bytes/s.")
            if (value - lo) % inc:
                a, b = _nearest_steps(value, lo, inc)
                n.refuse(f"camera.flir.link_throughput_limit {value} is not "
                         f"a multiple of this camera's DeviceLinkThroughput"
                         f"Limit increment {inc} (nearest: {a} or {b}).")
        if value is not None:
            n.seti("DeviceLinkThroughputLimit", value)
        cam.applied["link_limit"] = n.geti("DeviceLinkThroughputLimit")

    def _payload(self, cam) -> int:
        n = cam.nodes
        if n.readable("PayloadSize"):
            return n.geti("PayloadSize")
        return cam.width * cam.height

    def _size_link(self, cam, fps: float) -> None:
        """Size an "auto" link limit for `fps`, and refuse a rate the link
        cannot carry whatever the setting."""
        n = cam.nodes
        if not n.has("DeviceLinkThroughputLimit"):
            return
        payload = self._payload(cam)
        need = payload * fps
        lo, hi, inc = n.rangei("DeviceLinkThroughputLimit")
        if need > hi:
            speed = ""
            if n.readable("DeviceLinkSpeed"):
                speed = (f" (DeviceLinkSpeed reads "
                         f"{n.geti('DeviceLinkSpeed') * 8 / 1e6:.0f} Mbit/s)")
            n.refuse(f"at {fps:g} fps a {cam.width}x{cam.height} frame "
                     f"({payload} bytes) needs {_fmt_rate(need)}, but "
                     f"DeviceLinkThroughputLimit can go no higher than "
                     f"{_fmt_rate(hi)} on this link. Lower frame_rate or the "
                     f"ROI, or check the link{speed}.")
        want = cam.spec.flir.link_throughput_limit
        if want == "auto":
            if cam._link_fps == fps:
                return
            value = math.ceil(need / LINK_AUTO_SHARE)
            value = lo + math.ceil(max(0, value - lo) / inc) * inc
            value = min(max(value, lo), hi)
            if n.writable("DeviceLinkThroughputLimit"):
                n.seti("DeviceLinkThroughputLimit", value)
                cam._link_fps = fps
            cam.applied["link_limit"] = n.geti("DeviceLinkThroughputLimit")
            return
        current = n.geti("DeviceLinkThroughputLimit")
        if need > current:
            n.refuse(f"at {fps:g} fps a {cam.width}x{cam.height} frame needs "
                     f"{_fmt_rate(need)}, but camera.flir.link_throughput_"
                     f"limit holds the link to {_fmt_rate(current)}. Raise it, "
                     f"or set it to auto.")

    def _apply_buffers(self, cam) -> None:
        n = cam.nodes
        if n.has("StreamBufferHandlingMode", "tlstream"):
            n.sete("StreamBufferHandlingMode", GRAB_STRATEGY, "tlstream")
        if n.has("StreamBufferCountMode", "tlstream"):
            n.sete("StreamBufferCountMode", "Manual", "tlstream")
        lo, hi, _inc = n.rangei("StreamBufferCountManual", "tlstream")
        if n.readable("StreamBufferCountMax", "tlstream"):
            hi = min(hi, n.geti("StreamBufferCountMax", "tlstream"))
        want = cam.max_num_buffer
        if want > hi:
            n.refuse(f"max_num_buffer {want} exceeds StreamBufferCountMax "
                     f"{hi} on this host. Lower max_num_buffer (RAM: "
                     f"n_cameras x buffers x frame size).")
        if want < lo:
            n.refuse(f"max_num_buffer {want} is below this camera's minimum "
                     f"of {lo} stream buffers.")
        n.seti("StreamBufferCountManual", max(lo, min(SELFTEST_BUFFERS, hi)),
               "tlstream")

    def _validate_trigger(self, cam) -> None:
        """Check the trigger settings against this camera now, so a bad line
        refuses the open instead of the first Record."""
        n = cam.nodes
        t = cam.spec.trigger
        line = cam.trigger_line
        sel_entries = n.entries("TriggerSelector")
        if sel_entries:
            if "FrameStart" not in sel_entries:
                n.refuse(f"TriggerSelector offers {', '.join(sel_entries)} "
                         f"but not FrameStart, the trigger Panopticon uses.")
            n.sete("TriggerSelector", "FrameStart")
        n.need("TriggerMode", why="hardware triggering needs")
        sources = n.entries("TriggerSource")
        if line not in sources:
            n.refuse(f"camera.trigger.line {line!r} is not an input on this "
                     f"camera. TriggerSource offers {', '.join(sources)}. Run "
                     f"'uv run probe_flir.py --find-line' to see which line "
                     f"your trigger wire is on.")
        if n.has("LineSelector") and line in n.entries("LineSelector"):
            n.sete("LineSelector", line)
            if n.readable("LineMode") and n.gete("LineMode") != "Input":
                n.refuse(f"camera.trigger.line {line} is set as an output on "
                         f"this camera (LineMode {n.gete('LineMode')}). Set it "
                         f"to Input in SpinView, or wire the trigger to "
                         f"another input line.")
        if n.has("TriggerActivation"):
            if t.activation not in n.entries("TriggerActivation"):
                n.refuse(f"camera.trigger.activation {t.activation} is not "
                         f"offered; TriggerActivation offers "
                         f"{', '.join(n.entries('TriggerActivation'))}.")
        if n.has("TriggerOverlap"):
            if t.overlap not in n.entries("TriggerOverlap"):
                n.refuse(f"camera.trigger.overlap {t.overlap} is not offered; "
                         f"TriggerOverlap offers "
                         f"{', '.join(n.entries('TriggerOverlap'))}.")
        else:
            print(f"[flir] {cam.serial}: this camera has no TriggerOverlap "
                  f"node, so whether it accepts a trigger during readout is "
                  f"its own; the trigger witness counts any it ignores",
                  flush=True)
        if t.delay_us is not None:
            lo, hi = n.rangef("TriggerDelay")
            if not lo <= t.delay_us <= hi:
                n.refuse(f"camera.trigger.delay_us {t.delay_us:g} is outside "
                         f"this camera's TriggerDelay range {lo:g} to {hi:g} "
                         f"us.")

    def _apply_exposure_gain_at_open(self, cam) -> None:
        n = cam.nodes
        spec = cam.spec
        lo, hi = n.rangef("ExposureTime")
        if not lo <= spec.exposure_us <= hi:
            n.refuse(f"camera.exposure_us {spec.exposure_us:g} is outside this "
                     f"camera's ExposureTime range {lo:g} to {hi:g} us.")
        n.setf("ExposureTime", spec.exposure_us)
        cam.applied["exposure_us"] = n.getf("ExposureTime")
        if n.has("Gain"):
            glo, ghi = n.rangef("Gain")
            if not glo <= spec.gain_db <= ghi:
                n.refuse(f"camera.gain_db {spec.gain_db:g} is outside this "
                         f"camera's Gain range {glo:g} to {ghi:g} dB.")
            n.setf("Gain", spec.gain_db)
            cam.applied["gain_db"] = n.getf("Gain")
        elif spec.gain_db:
            n.refuse(f"camera.gain_db {spec.gain_db:g} needs the Gain node, "
                     f"which this camera does not have. Set camera.gain_db: 0.")

    def _configure_counters(self, cam) -> None:
        """Counter0 counts rising (or falling) edges on the trigger line and
        Counter1 the exposures the camera starts, where the camera offers
        them. A camera without counters records without the witness."""
        n = cam.nodes
        cam.counters = {}
        selectors = n.entries("CounterSelector")
        if not selectors or not n.has("CounterEventSource") or not n.has(
                "CounterValue"):
            print(f"[flir] {cam.serial}: no counters; the trigger witness is "
                  f"off for this camera", flush=True)
            return
        activation = cam.spec.trigger.activation
        plan = ((EDGE_COUNTER, cam.trigger_line, "edges"),
                (EXPOSURE_COUNTER, "ExposureStart", "exposures"))
        for sel, source, what in plan:
            if sel not in selectors:
                continue
            n.sete("CounterSelector", sel)
            if source not in n.entries("CounterEventSource"):
                continue
            n.sete("CounterEventSource", source)
            if (what == "edges" and n.has("CounterEventActivation")
                    and activation in n.entries("CounterEventActivation")):
                n.sete("CounterEventActivation", activation)
            cam.counters[sel] = what
        if "edges" not in cam.counters.values():
            cam.counters = {}
            print(f"[flir] {cam.serial}: no counter counts "
                  f"{cam.trigger_line}; the trigger witness is off for this "
                  f"camera", flush=True)
            return
        n.sete("CounterSelector", EDGE_COUNTER)
        cam.counter_chunk_ok = "CounterValue" in n.entries("ChunkSelector")
        if n.readable("CounterValue"):
            _lo, top, _inc = n.rangei("CounterValue")
            cam._ctr_period = top + 1 if 0 < top < 2 ** 31 else 0

    # ------------------------------------------------------------ self-test
    def _self_test(self, cam) -> None:
        """Measure the frame-ID and clock behaviour in free run, or reuse the
        result for this serial from earlier in the process."""
        key = (cam.serial, cam.spec.flir.timestamp_source)
        cached = self._selftests.get(key)
        if cached is not None:
            cam.selftest = dict(cached, cached=True)
            self._adopt_timestamp(cam, cached["timestamp"])
            cam.ts_chunk_name = cached["timestamp"].get("chunk_name")
            if cam.ts_source == "chunk":
                self._enable_chunk(cam, "Timestamp")
            return
        n = cam.nodes
        flir = cam.spec.flir
        self._trigger_off(cam)
        rate = self._selftest_rate(cam)
        period_s = 1.0 / rate if rate else None
        # A camera that reports no rate is assumed to free-run at 1 fps or
        # faster.
        timeout_ms = int(max(1000.0, 4000.0 * (period_s or 1.0)))

        chunk_ts = flir.timestamp_source == "chunk"
        if chunk_ts:
            self._enable_chunk(cam, "Timestamp")
        cycles = self._selftest_cycles(cam, timeout_ms, chunk_ts)
        host = (None if rate
                else self._selftest_host_run(cam, timeout_ms, chunk_ts))
        first_ids = [c[0]["id"] for c in cycles]
        result = {"frame_id": self._judge_ids(cycles), "rate_fps": rate}
        ts = self._judge_timestamps(cam, cycles, period_s, "image", host)
        if (ts["zero_seen"] and flir.timestamp_source == "auto") or chunk_ts:
            if not chunk_ts:
                self._enable_chunk(cam, "Timestamp")
                cycles = self._selftest_cycles(cam, timeout_ms, True)
                if not rate:
                    host = self._selftest_host_run(cam, timeout_ms, True)
            ts_chunk = self._judge_timestamps(cam, cycles, period_s, "chunk",
                                              host)
            ts_chunk["image_zero"] = ts["zero_seen"]
            ts = ts_chunk
        if ts["zero_seen"]:
            n.refuse("device timestamps are 0 (GetTimeStamp"
                     + (" and chunk Timestamp" if ts["source"] == "chunk"
                        else "") + "); the stall recovery and the block-ID "
                     "rate check need a device clock.")
        if ts["problem"]:
            n.refuse(ts["problem"])
        result["timestamp"] = ts
        result["first_ids"] = first_ids
        self._adopt_timestamp(cam, ts)
        cam.selftest = result
        self._selftests[key] = result
        fid = result["frame_id"]
        at = (f"{rate:g} fps" if rate else
              f"the camera's own rate (it reports none; the clock unit is "
              f"judged against the host clock over {len(host) - 1} frames)")
        print(f"[flir] {cam.serial}: self-test at {at}: first frame "
              f"IDs {fid['first_ids']} ("
              + (f"restarts, {fid['base']}-based" if fid["restarts"]
                 else "does not restart")
              + ("" if fid["counts_frames"]
                 else f", steps {fid['steps']}, does not count frames")
              + f"), timestamps {ts['source']} x{ts['scale']:g} "
                f"(interval ratio {ts['unit_ratio']:.4f}, continuous across "
                f"restarts)", flush=True)

    def _selftest_rate(self, cam):
        """Ask for the self-test's free-run rate where the camera allows it,
        and return the rate the camera says it runs at, or None.

        AcquisitionResultingFrameRate is the camera's own statement. Without
        it, the rate written is used while the frame-rate control is on. A
        node the camera has but does not let this backend write is left as
        it is, and the log says so."""
        n = cam.nodes
        enabled = True
        if n.has("AcquisitionFrameRateEnable"):
            if n.writable("AcquisitionFrameRateEnable"):
                n.setb("AcquisitionFrameRateEnable", True)
            else:
                print(f"[flir] {cam.serial}: AcquisitionFrameRateEnable is "
                      f"not writable; the self-test leaves it as the camera "
                      f"has it", flush=True)
            enabled = (n.readable("AcquisitionFrameRateEnable")
                       and n.getb("AcquisitionFrameRateEnable"))
        wrote = None
        if n.writable("AcquisitionFrameRate"):
            wrote = min(SELFTEST_FPS, n.rangef("AcquisitionFrameRate")[1])
            n.setf("AcquisitionFrameRate", wrote)
        elif n.has("AcquisitionFrameRate"):
            print(f"[flir] {cam.serial}: AcquisitionFrameRate is not "
                  f"writable; the self-test runs at the camera's own rate",
                  flush=True)
        if n.readable("AcquisitionResultingFrameRate"):
            return n.getf("AcquisitionResultingFrameRate")
        return wrote if enabled else None

    def _selftest_frame(self, cam, timeout_ms: int, chunk_ts: bool,
                        where: str) -> dict:
        """One free-run image's frame ID, timestamps and host arrival time.
        The image is released before this returns."""
        api = self._api
        try:
            img = api.next_image(cam.handle, timeout_ms)
        except FlirTimeout:
            cam.nodes.refuse(
                f"the free-run self-test received no frame within "
                f"{timeout_ms} ms ({where}). Check that no other program "
                f"holds the camera, and send the output of 'uv run "
                f"probe_flir.py'.")
        host = time.perf_counter()
        try:
            entry = {"id": int(api.image_frame_id(img)),
                     "ts_image": int(api.image_timestamp(img)),
                     "host": host}
            if chunk_ts:
                if cam.ts_chunk_name is None:
                    v, cam.ts_chunk_name = _read_chunk(api, img, "Timestamp")
                else:
                    v = api.chunk_int(img, cam.ts_chunk_name)
                entry["ts_chunk"] = int(v)
        finally:
            api.image_release(img)
        return entry

    def _selftest_cycles(self, cam, timeout_ms: int, chunk_ts: bool) -> list:
        api = self._api
        cycles = []
        for k in range(SELFTEST_CYCLES):
            frames = []
            api.begin(cam.handle)
            try:
                for _ in range(SELFTEST_FRAMES):
                    frames.append(self._selftest_frame(
                        cam, timeout_ms, chunk_ts,
                        f"acquisition {k + 1} of {SELFTEST_CYCLES}"))
            finally:
                api.end(cam.handle)
            cycles.append(frames)
        return cycles

    def _selftest_host_run(self, cam, timeout_ms: int, chunk_ts: bool) -> list:
        """Free-run frames spanning SELFTEST_HOST_S of host time, from the
        second frame on, for a camera that reports no frame rate. Its clock
        unit is then judged against the host clock."""
        api = self._api
        frames = []
        api.begin(cam.handle)
        try:
            while len(frames) < SELFTEST_HOST_MAX_FRAMES:
                frames.append(self._selftest_frame(
                    cam, timeout_ms, chunk_ts, "the host-clock run"))
                if (len(frames) > 2 and frames[-1]["host"] - frames[1]["host"]
                        >= SELFTEST_HOST_S):
                    break
        finally:
            api.end(cam.handle)
        return frames

    @staticmethod
    def _judge_ids(cycles) -> dict:
        """Whether the frame ID restarts at every BeginAcquisition, from
        what base, and whether it counts frames. Free-run frames are
        consecutive and incomplete ones carry IDs, so an ID that counts
        frames steps by 1. A larger step appears only where a frame was
        lost on the way, so the smallest step must be 1."""
        firsts = [c[0]["id"] for c in cycles]
        steps = sorted({b["id"] - a["id"] for c in cycles
                        for a, b in zip(c, c[1:])})
        counts = bool(steps) and steps[0] == 1
        if all(f == 1 for f in firsts):
            return {"first_ids": firsts, "restarts": True, "base": 1,
                    "steps": steps, "counts_frames": counts}
        if all(f == 0 for f in firsts):
            return {"first_ids": firsts, "restarts": True, "base": 0,
                    "steps": steps, "counts_frames": counts}
        return {"first_ids": firsts, "restarts": False, "base": None,
                "steps": steps, "counts_frames": counts}

    def _judge_timestamps(self, cam, cycles, period_s, source: str,
                          host=None) -> dict:
        """The clock checks on one set of self-test frames.

        The unit is judged against the frame period the camera reports
        (`period_s`), because host delivery jitter is a large share of one
        frame interval and a simulated camera runs on virtual time. A camera
        that reports no rate (`period_s` None) is judged against the host
        clock instead, over the `host` run's span, which is long enough that
        the jitter is a small share of it."""
        key = "ts_image" if source == "image" else "ts_chunk"
        out = {"source": source, "zero_seen": False, "scale": 1,
               "unit_ratio": float("nan"), "continuity_ok": False,
               "problem": None,
               "unit_basis": "camera rate" if period_s else "host clock"}
        runs = list(cycles) + ([host] if host else [])
        stamps = [[f[key] for f in c] for c in runs]
        if any(v == 0 for c in stamps for v in c):
            out["zero_seen"] = True
            return out
        intervals = [b - a for c in stamps for a, b in zip(c, c[1:])]
        if any(d <= 0 for d in intervals):
            out["problem"] = (f"device timestamps do not increase within one "
                              f"acquisition (intervals {intervals[:12]}), so "
                              f"they are not a free-running clock.")
            return out
        if period_s:
            ratio = statistics.median(intervals) / (period_s * 1e9)
            tol, against = TS_UNIT_TOL, "at the frame rate the camera reports"
        else:
            wall = host[-1]["host"] - host[1]["host"]
            dev = host[-1][key] - host[1][key]
            ratio = dev / (wall * 1e9) if wall > 0 else float("nan")
            tol = TS_HOST_TOL
            against = (f"against the host clock over {len(host) - 1} frames "
                       f"(this camera reports no frame rate)")
        out["unit_ratio"] = ratio
        scale = None
        if abs(ratio - 1.0) <= tol:
            scale = 1
        else:
            for factor, node in self._tick_factors(cam):
                if abs(ratio * factor - 1.0) <= tol:
                    scale = factor
                    out["tick_node"] = node
                    break
        if scale is None:
            out["problem"] = (
                f"device timestamps advance {ratio:.4g} times as fast as "
                f"nanoseconds {against}, and no tick-rate node explains it, "
                f"so the stall resync and the block-ID rate check would be "
                f"wrong by that factor. Send the output of 'uv run "
                f"probe_flir.py'.")
            return out
        out["scale"] = scale
        for prev, nxt in zip(cycles, cycles[1:]):
            dev_gap = (nxt[0][key] - prev[-1][key]) * scale / 1e9
            host_gap = nxt[0]["host"] - prev[-1]["host"]
            if dev_gap <= 0 or dev_gap < TS_CONTINUITY_SHARE * host_gap:
                out["problem"] = (
                    f"the device clock restarts when acquisition restarts "
                    f"(it moved {dev_gap:+.3f} s while {host_gap:.3f} s "
                    f"passed), so a stall re-arm could not be realigned from "
                    f"it. Send the output of 'uv run probe_flir.py'.")
                return out
        out["continuity_ok"] = True
        if source == "chunk":
            out["chunk_name"] = cam.ts_chunk_name
        return out

    @staticmethod
    def _tick_factors(cam) -> list:
        """Nanoseconds per timestamp unit that the camera's own nodes state:
        TimestampIncrement (ns per tick) and GevTimestampTickFrequency (Hz)."""
        n = cam.nodes
        out = []
        if n.readable("TimestampIncrement"):
            inc = n.geti("TimestampIncrement")
            if inc > 1:
                out.append((inc, "TimestampIncrement"))
        if n.readable("GevTimestampTickFrequency"):
            hz = n.geti("GevTimestampTickFrequency")
            if 0 < hz < 1_000_000_000:
                factor = 1e9 / hz
                out.append((int(factor) if float(factor).is_integer()
                            else factor, "GevTimestampTickFrequency"))
        return out

    @staticmethod
    def _adopt_timestamp(cam, ts: dict) -> None:
        cam.ts_source = ts["source"]
        cam.ts_scale = ts["scale"]

    def _enable_chunk(self, cam, entry: str) -> None:
        n = cam.nodes
        if entry not in n.entries("ChunkSelector"):
            n.refuse(f"the {entry} chunk is needed but ChunkSelector offers "
                     f"{', '.join(n.entries('ChunkSelector')) or 'nothing'}.")
        n.setb("ChunkModeActive", True)
        n.sete("ChunkSelector", entry)
        n.setb("ChunkEnable", True)

    def _choose_block_id_source(self, cam) -> None:
        n = cam.nodes
        want = cam.spec.flir.block_id_source
        fid = cam.selftest["frame_id"]
        counter_ok = bool(cam.counters) and cam.counter_chunk_ok
        firsts = ", ".join(str(i) for i in fid["first_ids"])
        usable = fid["restarts"] and fid["counts_frames"]
        if not fid["restarts"]:
            why = (f"the frame ID does not restart when acquisition restarts "
                   f"(first IDs {firsts})")
        else:
            why = (f"the frame ID steps by "
                   f"{', '.join(str(s) for s in fid['steps'])} between "
                   f"consecutive free-run frames, not by 1, so it does not "
                   f"count frames")
        if want == "trigger_counter" and not counter_ok:
            n.refuse("camera.flir.block_id_source trigger_counter needs a "
                     "counter on the trigger line and the CounterValue chunk; "
                     "ChunkSelector offers "
                     + (", ".join(n.entries("ChunkSelector")) or "nothing")
                     + ". Set block_id_source: auto.")
        if want == "frame_id" and not usable:
            n.refuse(f"{why}, so this camera cannot be aligned by frame ID. "
                     f"Set camera.flir.block_id_source: auto"
                     + (" or trigger_counter." if counter_ok
                        else "; this model offers no CounterValue chunk."))
        if want == "auto" and not usable and not counter_ok:
            n.refuse(f"{why}, so this camera cannot be aligned by frame ID, "
                     f"and it offers no CounterValue chunk. This model is not "
                     f"supported for recording; please send the output of "
                     f"'uv run probe_flir.py'.")
        cam._fid_evidence = usable
        cam._fid_base_offset = 1 if fid["base"] == 0 else 0
        if want == "trigger_counter" or not usable:
            cam.block_id_source = "trigger_counter"
            self._enable_chunk(cam, "CounterValue")
            if n.has("ChunkCounterSelector"):
                n.sete("ChunkCounterSelector", EDGE_COUNTER)
        else:
            cam.block_id_source = "frame_id"
            cam.id_offset = 1 if fid["base"] == 0 else 0

    def _check_exposure_at_rate(self, cam, fps: float) -> None:
        exposure = cam.applied.get("exposure_us", cam.spec.exposure_us)
        ceiling = self.exposure_ceiling_us(cam, fps, 0.0)
        if exposure > 0.9 * ceiling:
            cam.nodes.refuse(
                f"camera.exposure_us {exposure:g} is above what this camera "
                f"can expose at frame_rate {fps:g} (ExposureTime max "
                f"{ceiling:.0f} us at AcquisitionFrameRate {fps:g}; Panopticon "
                f"keeps 10% margin, so {0.9 * ceiling:.0f} us). Lower "
                f"exposure_us or add light.")

    def _log_summary(self, cam) -> None:
        a = cam.applied
        t = cam.spec.trigger
        if cam.block_id_source == "trigger_counter":
            ids = "trigger_counter(CounterValue chunk)"
        else:
            base = cam.selftest["frame_id"]["base"]
            ids = f"frame_id({base}-based, restarts" + (
                ", 16-bit unwrapped)" if cam.id16 else ")")
        unit = "ns" if cam.ts_scale == 1 else f"x{cam.ts_scale:g} to ns"
        limit = a.get("link_limit", "camera's")
        print(f"[flir] {cam.serial}: exposure_us={a.get('exposure_us', 0):g} "
              f"(asked {cam.spec.exposure_us:g}) "
              f"gain_db={a.get('gain_db', 0):g} line={cam.trigger_line} "
              f"overlap={t.overlap} limit={limit} ids={ids} "
              f"ts={cam.ts_source}({unit}) roi={cam.width}x{cam.height} "
              f"buffers={a.get('buffers')}", flush=True)

    # ------------------------------------------------------------ describe
    def describe(self, cam) -> dict:
        """Width, height and pixel format read back from the camera."""
        n = cam.nodes
        return {"width": n.geti("Width"), "height": n.geti("Height"),
                "pixel_format": n.gete("PixelFormat"), "serial": cam.serial,
                "model": cam.model}

    # --------------------------------------------------------------- modes
    def _stop_if_streaming(self, cam) -> None:
        if self._api.is_streaming(cam.handle):
            self._api.end(cam.handle)
        cam._grabbing = False

    def _trigger_off(self, cam) -> None:
        """TriggerMode Off for FrameStart and every other selector offered,
        leaving the selector on FrameStart."""
        n = cam.nodes
        if not n.has("TriggerMode"):
            return
        selectors = n.entries("TriggerSelector")
        order = [s for s in ("FrameStart",) + OTHER_TRIGGER_SELECTORS
                 if s in selectors] or [None]
        for sel in order:
            if sel is not None:
                n.sete("TriggerSelector", sel)
            n.sete("TriggerMode", "Off")
        if "FrameStart" in selectors:
            n.sete("TriggerSelector", "FrameStart")

    def set_freerun(self, cam, fps: float = 30.0) -> None:
        """Untriggered preview at `fps`, or the fastest the camera allows."""
        self._stop_if_streaming(cam)
        cam._triggered = False
        cam._arm_pending = False
        n = cam.nodes
        self._trigger_off(cam)
        if n.writable("AcquisitionFrameRateEnable"):
            n.setb("AcquisitionFrameRateEnable", True)
        if n.writable("AcquisitionFrameRate"):
            top = n.rangef("AcquisitionFrameRate")[1]
            n.setf("AcquisitionFrameRate", min(float(fps), top))
        elif n.has("AcquisitionFrameRate"):
            print(f"[flir] {cam.serial}: AcquisitionFrameRate is not "
                  f"writable, so the preview runs at the camera's own rate",
                  flush=True)

    def set_triggered(self, cam, rate_limit: float = 165.0,
                      announce: bool = False) -> None:
        """Hardware-trigger mode on the profile's line.

        The source, activation and overlap change only while TriggerMode is
        Off, which Spinnaker requires. `rate_limit` (trigger_rate_limit) is
        Basler's limiter and does nothing here; `announce` prints one line
        saying so and naming the pacing the camera actually uses."""
        self._stop_if_streaming(cam)
        n = cam.nodes
        t = cam.spec.trigger
        self._trigger_off(cam)
        n.sete("TriggerSource", cam.trigger_line, field="camera.trigger.line")
        if n.has("TriggerActivation"):
            n.sete("TriggerActivation", t.activation,
                   field="camera.trigger.activation")
        if n.has("TriggerOverlap"):
            n.sete("TriggerOverlap", t.overlap, field="camera.trigger.overlap")
        if t.delay_us is not None:
            n.setf("TriggerDelay", t.delay_us)
        n.sete("TriggerMode", "On")
        if n.writable("AcquisitionFrameRateEnable"):
            n.setb("AcquisitionFrameRateEnable", False)
        cam._triggered = True
        cam._arm_pending = True
        overlap = n.gete("TriggerOverlap") if n.readable("TriggerOverlap") \
            else "not offered"
        print(f"[flir] {cam.serial}: trigger mode on {cam.trigger_line} "
              f"({t.activation}), TriggerOverlap={overlap}", flush=True)
        if announce:
            pacing = []
            for node in ("DeviceLinkThroughputLimit", "GevSCPD"):
                if n.readable(node):
                    pacing.append(f"{node}={n.geti(node)}")
            print(f"[flir] trigger_rate_limit ({rate_limit:g}) sets a Basler "
                  f"limiter and does not apply to FLIR cameras; pacing: "
                  f"{', '.join(pacing) or 'the camera default'}", flush=True)

    # ------------------------------------------------------- exposure, gain
    def get_exposure_gain(self, cam) -> tuple:
        n = cam.nodes
        exp = n.getf("ExposureTime") if n.readable("ExposureTime") else None
        gain = n.getf("Gain") if n.readable("Gain") else None
        return exp, gain

    def gain_unit(self, cam):
        """"dB": a Spinnaker camera's Gain node is in dB. None without one."""
        return "dB" if cam.nodes.has("Gain") else None

    def set_exposure_gain(self, cam, exposure_us=None, gain_db=None,
                          gain_unit=None) -> tuple:
        """Apply exposure and gain, clamped into each node's range, and
        return what the camera read back.

        The exposure ceiling is the caller's (camera_manager clamps to 90% of
        `exposure_ceiling_us`). `gain_unit` None and "dB" are written; "raw"
        is refused after the exposure is applied, because a Spinnaker Gain
        node takes dB and a step count written as dB is another gain."""
        if gain_unit not in (None, "dB", "raw"):
            raise ValueError(f"gain_unit must be None, 'dB' or 'raw', not "
                             f"{gain_unit!r}")
        n = cam.nodes
        exp = gain = None
        if exposure_us is not None and n.has("ExposureTime"):
            lo, hi = n.rangef("ExposureTime")
            n.setf("ExposureTime", max(lo, min(float(exposure_us), hi)))
            exp = n.getf("ExposureTime")
        if gain_db is not None and n.has("Gain"):
            if gain_unit == "raw":
                raise ValueError(f"{cam.who}: gain value {gain_db!r} is in raw "
                                 f"steps but this camera's Gain node takes dB")
            lo, hi = n.rangef("Gain")
            n.setf("Gain", max(lo, min(float(gain_db), hi)))
            gain = n.getf("Gain")
        return exp, gain

    def exposure_ceiling_us(self, cam, fps: float, rate_limit: float) -> float:
        """The longest exposure, in us, at which this camera acquires every
        trigger at `fps`, as the camera reports it. `rate_limit` does not
        apply to FLIR cameras and is ignored.

        This is the first call that carries the acquisition's frame rate, so
        it also sizes an "auto" link limit for `fps`, and it raises when the
        link cannot carry `fps` at all. The ceiling is measured with the
        stream stopped: the frame rate is set to `fps` with the exposure at
        its minimum, `ExposureTime`'s maximum is read, and the camera's
        exposure and frame-rate settings are put back. When that maximum does
        not follow the frame rate, the ceiling is the period minus the
        readout time implied by `AcquisitionFrameRate`'s own maximum, and the
        log says so. The value is cached per frame rate and link limit, and
        camera_manager keeps its 10% margin below it."""
        fps = float(fps)
        self._size_link(cam, fps)
        n = cam.nodes
        link = (n.geti("DeviceLinkThroughputLimit")
                if n.readable("DeviceLinkThroughputLimit") else None)
        key = (round(fps, 6), link)
        cached = cam._ceilings.get(key)
        if cached is not None:
            return cached
        ceiling = self._measure_ceiling(cam, fps)
        cam._ceilings[key] = ceiling
        return ceiling

    def _measure_ceiling(self, cam, fps: float) -> float:
        n = cam.nodes
        period_us = 1e6 / fps
        if not n.has("ExposureTime"):
            return period_us
        if not (n.has("AcquisitionFrameRate")
                and n.has("AcquisitionFrameRateEnable")):
            print(f"[flir] {cam.serial}: no AcquisitionFrameRate node, so the "
                  f"exposure ceiling at {fps:g} fps is the trigger period "
                  f"({period_us:.0f} us)", flush=True)
            return period_us
        unmeasurable = (f"[flir] {cam.serial}: AcquisitionFrameRate cannot be "
                        f"written on this camera, so the exposure ceiling at "
                        f"{fps:g} fps is the trigger period ({period_us:.0f} "
                        f"us)")
        if not n.writable("AcquisitionFrameRateEnable"):
            print(unmeasurable, flush=True)
            return period_us
        was_on = n.getb("AcquisitionFrameRateEnable")
        # Kept even while the frame rate is disabled (trigger mode), where the
        # camera may still report it, so the measurement leaves no trace.
        was_rate = (n.getf("AcquisitionFrameRate")
                    if n.readable("AcquisitionFrameRate") else None)
        was_exp = n.getf("ExposureTime")
        readout = None
        exp_max = None
        try:
            exp_min = n.rangef("ExposureTime")[0]
            n.setf("ExposureTime", exp_min)
            n.setb("AcquisitionFrameRateEnable", True)
            if n.writable("AcquisitionFrameRate"):
                rate_max = n.rangef("AcquisitionFrameRate")[1]
                if rate_max + 1e-6 < fps:
                    n.refuse(f"AcquisitionFrameRate can go no higher than "
                             f"{rate_max:.2f} fps on this camera even at its "
                             f"shortest exposure ({exp_min:g} us), so it "
                             f"cannot acquire {fps:g} fps at "
                             f"{cam.width}x{cam.height}. Lower frame_rate or "
                             f"the ROI.")
                n.setf("AcquisitionFrameRate", fps)
                exp_max = n.rangef("ExposureTime")[1]
                if exp_max >= period_us:
                    readout = self._sensor_readout_us(cam, exp_min)
        finally:
            # The rate goes back while the frame rate is still enabled (it is
            # writable only then) and the exposure is still at its minimum
            # (so any earlier rate fits); the exposure goes back last, under
            # the settings it was valid with.
            if was_rate is not None and n.writable("AcquisitionFrameRate"):
                n.setf("AcquisitionFrameRate", was_rate)
            n.setb("AcquisitionFrameRateEnable", was_on)
            n.setf("ExposureTime", was_exp)
        if exp_max is None:
            print(unmeasurable, flush=True)
            return period_us
        if readout is None:
            return exp_max
        ceiling = period_us - readout
        print(f"[flir] {cam.serial}: ExposureTime's maximum does not follow "
              f"AcquisitionFrameRate on this camera, so the exposure ceiling "
              f"at {fps:g} fps is the period minus the {readout:.0f} us "
              f"readout that AcquisitionFrameRate's maximum implies: "
              f"{ceiling:.0f} us", flush=True)
        return ceiling

    @staticmethod
    def _sensor_readout_us(cam, exp_min: float) -> float:
        """Frame time beyond the exposure, from AcquisitionFrameRate's
        maximum at the shortest exposure. The link limit is lifted to its
        maximum for the reading, because a paced link lowers the frame-rate
        maximum without lengthening the time the sensor needs."""
        n = cam.nodes
        link = None
        if n.writable("DeviceLinkThroughputLimit"):
            link = n.geti("DeviceLinkThroughputLimit")
            n.seti("DeviceLinkThroughputLimit",
                   n.rangei("DeviceLinkThroughputLimit")[1])
        try:
            rate_max = n.rangef("AcquisitionFrameRate")[1]
        finally:
            if link is not None:
                n.seti("DeviceLinkThroughputLimit", link)
        return max(0.0, 1e6 / rate_max - exp_min)

    # ----------------------------------------------------------- GigE, IDs
    def enable_extended_block_ids(self, i: int, cam) -> bool:
        """Whether this camera's frame IDs are 64-bit. open() applied
        camera.flir.extended_ids already, because the self-test and the
        unwrap depend on it; this reports the result."""
        if not cam.gige:
            status = "native 64-bit (USB3 Vision)"
        elif cam.extended_ids:
            status = "enabled (GevGVSPExtendedIDMode On)"
        else:
            status = ("UNAVAILABLE (16-bit IDs; the FLIR backend unwraps them "
                      "from the device clock)")
        print(f"[cam{i + 1}] extended (64-bit) block IDs: {status}",
              flush=True)
        return not cam.id16

    def select_gige_driver(self, i: int, cam, which: str = "socket") -> None:
        """Log the GigE stream mode. open() set it from
        camera.flir.stream_mode; the profile's gige_driver (`which`) names a
        pylon driver and does not apply."""
        if not cam.gige:
            return
        n = cam.nodes
        mode = (n.gete("StreamMode", "tlstream")
                if n.readable("StreamMode", "tlstream") else "not reported")
        print(f"[cam{i + 1}] FLIR GigE stream mode: {mode} "
              f"(camera.flir.stream_mode)", flush=True)

    def set_packet_size(self, cam, n: int) -> int:
        """Write GevSCPSPacketSize and return what the camera read back. A
        USB3 camera has no such node and raises."""
        nodes = cam.nodes
        nodes.need("GevSCPSPacketSize",
                   why="the packet-size sweep applies to GigE cameras only")
        if self._api.is_streaming(cam.handle):
            nodes.refuse("GevSCPSPacketSize cannot change while the camera "
                         "streams")
        nodes.seti("GevSCPSPacketSize", int(n))
        return nodes.geti("GevSCPSPacketSize")

    # ------------------------------------------------------------ grabbing
    def start_grabbing(self, cam) -> None:
        cam.StartGrabbing(GRAB_STRATEGY)

    def stop_grabbing(self, cam) -> None:
        cam.StopGrabbing()

    def is_grabbing(self, cam) -> bool:
        return cam.IsGrabbing()

    def retrieve(self, cam, timeout_ms: int):
        """The next image as a `FlirResult`, or `FlirTimeout`.

        The first image after each StartGrabbing is checked for the layout
        the (H, W) view assumes: 8 bits per pixel, a stride equal to the
        width and the size describe() reported. An image with row padding is
        passed on, because the grab loop's padding check retires the camera
        for it by name."""
        img = self._api.next_image(cam.handle, timeout_ms)
        if cam._verify:
            try:
                problem = self._layout_problem(cam, img)
            except BaseException:
                self._api.image_release(img)
                raise
            if problem is not None:
                try:
                    self._api.image_release(img)
                finally:
                    raise FlirFrameError(f"{cam.who}: {problem}")
            cam._verify = False
        return FlirResult(cam, img)

    def _layout_problem(self, cam, img):
        api = self._api
        if api.image_padding(img) != (0, 0):
            return None
        bpp = api.image_bpp(img)
        if bpp != 8:
            return (f"the image has {bpp} bits per pixel; the capture path "
                    f"records 8-bit Mono8 only")
        dims = tuple(api.image_dims(img))
        if dims != (cam.width, cam.height):
            return (f"the image is {dims[0]}x{dims[1]} but the camera was "
                    f"opened at {cam.width}x{cam.height}")
        stride = api.image_stride(img)
        if stride != cam.width:
            return (f"the image stride is {stride} bytes for {cam.width} "
                    f"pixels, so an (H, W) view would shear every row")
        return None

    def close(self, cam) -> None:
        """End acquisition, deinitialise and release the camera. Failures
        are logged; closing twice does nothing."""
        if not cam.is_open:
            return
        cam.is_open = False
        cam._grabbing = False
        api = self._api
        failures = []
        for what, fn in (("end", lambda: api.is_streaming(cam.handle)
                          and api.end(cam.handle)),
                         ("deinit", lambda: api.is_initialized(cam.handle)
                          and api.deinit(cam.handle))):
            try:
                fn()
            except Exception as e:
                failures.append(f"{what}: {e}")
        with self._lock:
            if cam in self._open:
                self._open.remove(cam)
            if not cam.device.released:
                cam.device.released = True
                try:
                    api.release(cam.handle)
                except Exception as e:
                    failures.append(f"release: {e}")
        if failures:
            print(f"[flir] {cam.serial}: close: " + "; ".join(failures),
                  flush=True)

    # --------------------------------------------------------- diagnostics
    def stream_stats(self, cam) -> dict:
        """Spinnaker's stream counters, the `CANONICAL_STREAM_STATS` keys,
        and the camera-side transport settings. Never raises.

        StreamLostFrameCount counts frames lost because no host buffer was
        free (the HOST could not keep up); StreamIncompleteFrameCount and
        the packet counters count transport loss; StreamDroppedFrameCount
        stays 0 under OldestFirst."""
        n = cam.nodes
        out: dict = {}
        errors = []
        for name in STREAM_STAT_NODES:
            try:
                if n.readable(name, "tlstream"):
                    out[name] = n.geti(name, "tlstream")
            except Exception as e:
                errors.append(f"{name}: {e}")
        if not out and errors:
            return {"error": "; ".join(errors)}
        for canonical, native in CANONICAL_STATS:
            if native in out:
                out[canonical] = out[native]
        for name in self.TRANSPORT_NODES:
            which = "tlstream" if name == "StreamMode" else "device"
            try:
                if not n.readable(name, which):
                    continue
                out[name] = (n.gete(name, which) if name == "StreamMode"
                             else n.geti(name, which))
            except Exception as e:
                out[name] = f"error: {e}"
        return out

    def thermals(self, cam) -> dict:
        """The camera's temperature readings, in the keys the thermal watch
        reads. Never raises.

        `temp_c` is the `Sensor` reading, or the first `DeviceTemperature
        Selector` entry's, and every entry is also reported as
        `temp_<entry>_c`. `temp_max_c` is the highest `temp_c` this backend
        has polled since the camera was opened, not a device high-water mark.
        `temp_status`, `temp_critical_c` and `temp_shutdown_c` are reported
        only when the camera has the nodes (SFNC `DeviceTemperatureStatus`,
        and `DeviceTemperatureStatusTransition` for NormalToHigh and
        HighToExceeded); the status word is translated to Ok, Critical or
        Error, and the camera's own word is kept under
        `temp_status_camera`. A camera that reports no shutdown temperature
        has no `temp_shutdown_c`, and the watch falls back to its status."""
        try:
            return self._thermals(cam)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _thermals(self, cam) -> dict:
        n = cam.nodes
        api = self._api
        out: dict = {}
        temp = n.node("DeviceTemperature")
        if temp is None or not api.node_readable(temp):
            return out
        readings = {}
        sel = n.node("DeviceTemperatureSelector")
        if sel is not None and n.writable("DeviceTemperatureSelector"):
            current = api.enum_get_symbolic(sel)
            try:
                for entry in api.enum_entries(sel):
                    api.enum_set_symbolic(sel, entry)
                    readings[entry] = float(api.float_get(temp))
            finally:
                api.enum_set_symbolic(sel, current)
        else:
            label = api.enum_get_symbolic(sel) if sel is not None else ""
            readings[label] = float(api.float_get(temp))
        primary = "Sensor" if "Sensor" in readings else next(iter(readings))
        out["temp_c"] = readings[primary]
        for entry, value in readings.items():
            if entry:
                out[f"temp_{entry.lower()}_c"] = value
        if cam.temp_max_c is None or out["temp_c"] > cam.temp_max_c:
            cam.temp_max_c = out["temp_c"]
        out["temp_max_c"] = cam.temp_max_c
        if n.readable("DeviceTemperatureStatus"):
            word = n.gete("DeviceTemperatureStatus")
            out["temp_status_camera"] = word
            out["temp_status"] = _TEMP_STATUS_WORDS.get(word.lower(), word)
        tsel = n.node("DeviceTemperatureStatusTransitionSelector")
        if tsel is not None and n.readable("DeviceTemperatureStatusTransition"):
            offered = api.enum_entries(tsel)
            current = api.enum_get_symbolic(tsel)
            try:
                for entry, key in _TEMP_TRANSITIONS:
                    if entry in offered:
                        api.enum_set_symbolic(tsel, entry)
                        out[key] = n.getf("DeviceTemperatureStatusTransition")
            finally:
                api.enum_set_symbolic(tsel, current)
        return out

    def acquisition_warnings(self, cam, frames_acquired: int) -> list:
        """What the trigger witness found about the acquisition that just
        ended, as sentences for WARNINGS.txt. Never raises.

        With both counters the count is exact: edges on the trigger line
        minus exposures started is the number of triggers the camera
        ignored, leaving out edges that arrived while a stall re-arm had the
        stream down. With the edge counter alone the count mixes ignored
        triggers with frames lost in transport, and the sentence says so
        and makes no claim about alignment (`_edge_only_sentences`)."""
        try:
            return self._witness_sentences(cam, int(frames_acquired))
        except Exception as e:
            print(f"[flir] {cam.serial}: trigger witness failed: "
                  f"{type(e).__name__}: {e}", flush=True)
            return []

    def _witness_sentences(self, cam, frames: int) -> list:
        w = cam.witness
        if w is None:
            return []
        if w["error"]:
            print(f"[flir] {cam.serial}: the trigger counters could not be "
                  f"read ({w['error']}); no witness for this acquisition",
                  flush=True)
            return []
        if w["edges"] is None:
            return []
        edges = w["edges"] - w["gap_edges"]
        exposures = w["exposures"]
        line = cam.trigger_line
        print(f"[flir] {cam.serial}: trigger witness: {edges} edges on {line}"
              + (f" (+{w['gap_edges']} while re-arming)" if w["gap_edges"]
                 else "")
              + (f", {exposures} exposures" if exposures is not None else "")
              + f", {frames} frames delivered"
              + (f", last CounterValue {cam._last_counter}"
                 if cam.block_id_source == "trigger_counter" else ""),
              flush=True)
        dead = self._implausible_counts(cam, w, frames)
        if dead is not None:
            why, edge_counter = dead
            print(f"[flir] {cam.serial}: {why}; no trigger witness for this "
                  f"acquisition", flush=True)
            ids = (f" Its block IDs come from that counter's CounterValue "
                   f"chunk (the last image carried {cam._last_counter}), so "
                   f"check that they advance before using this camera's "
                   f"video." if edge_counter
                   and cam.block_id_source == "trigger_counter" else "")
            return [f"{why}, so its trigger-witness counters do not count "
                    f"what Panopticon set them to count on this model, and "
                    f"this recording has no trigger witness for this camera."
                    f"{ids} Send the output of 'uv run probe_flir.py'."]
        return (self._ignored_sentences(cam, w, edges, frames)
                + self._latch_sentences(cam, w))

    def _ignored_sentences(self, cam, w, edges: int, frames: int) -> list:
        """The ignored-trigger count from counters that passed the
        plausibility check. `edges` leaves out the re-arm down time."""
        exposures = w["exposures"]
        line = cam.trigger_line
        if exposures is None:
            return self._edge_only_sentences(cam, w, edges, frames)
        # Below 0 only when an edge landed between the two reads that bound
        # a re-arm window, which counts it as down time.
        ignored = max(0, cam._ctr_delta(w["edges"], exposures)
                      - w["gap_edges"])
        what = (f"its trigger input ({line}) counted {edges} edges but it "
                f"started only {exposures} exposures, so it ignored "
                f"{ignored} trigger(s).")
        if ignored <= 0:
            return []
        if cam.block_id_source == "trigger_counter":
            return [f"{what} Its block IDs count the edges, so each ignored "
                    f"trigger is a gap, which alignment drops from every "
                    f"camera. Lower camera.exposure_us to keep those "
                    f"triggers."]
        return [f"{what} Its block IDs count the frames it acquired, so from "
                f"the first ignored trigger on they name later triggers than "
                f"the other cameras' do, and its frames are paired with the "
                f"wrong instants. Do not use this recording for 3D "
                f"reconstruction. Lower camera.exposure_us, or set "
                f"camera.flir.block_id_source: trigger_counter so an ignored "
                f"trigger becomes a gap."]

    @staticmethod
    def _latch_sentences(cam, w) -> list:
        """In trigger_counter mode, when the first image did not show
        whether CounterValue is latched before the edge: the last image's
        count against the edges counted at stop.

        With a count that includes the edge, a last image on the last
        trigger reads the edge count. A last image one or more below it is
        what a latch before the edge gives (and then every block ID of the
        recording is one trigger early), and also what triggers at the end
        that delivered no frame give. The sentence names both."""
        if (cam.block_id_source != "trigger_counter" or cam._ctr_latch_known
                or cam._last_counter is None):
            return []
        lag = cam._ctr_delta(w["edges"], cam._last_counter)
        if lag <= 0:
            return []
        return [f"its first image did not show whether it latches the "
                f"CounterValue chunk before the trigger edge, and its last "
                f"image's count ({cam._last_counter}) is {lag} below the "
                f"{w['edges']} edges it counted by the stop. Its last "
                f"trigger(s) delivering no frame gives this, and so does a "
                f"latch before the edge, which makes every block ID of this "
                f"camera one trigger early. Send the output of 'uv run "
                f"probe_flir.py'."]

    @staticmethod
    def _implausible_counts(cam, w, frames: int):
        """`(why, edge counter at fault)` when the counters cannot have
        counted what they were set to, or None.

        Every frame that reached the host was exposed on a trigger edge, so
        a working edge counter and a working exposure counter each reach at
        least the frames delivered, and exposures never pass edges. A
        counter narrower than 2**31 is checked only while the frames
        delivered are fewer than its period, so the counts cannot have
        wrapped."""
        period = cam._ctr_period
        if period and frames >= period:
            return None
        edges, exposures = w["edges"], w["exposures"]
        line = cam.trigger_line
        if edges < frames:
            return (f"its trigger-line counter counted {edges} edges on "
                    f"{line} for {frames} frames that reached the host", True)
        if exposures is not None and exposures < frames:
            return (f"its exposure counter counted {exposures} exposures for "
                    f"{frames} frames that reached the host", False)
        if exposures is not None and exposures > edges:
            return (f"its exposure counter counted {exposures} exposures, "
                    f"more than the {edges} edges on {line}", True)
        return None

    @staticmethod
    def _edge_only_sentences(cam, w, edges: int, frames: int) -> list:
        """The witness of a camera with an edge counter and no exposure
        counter. Neither branch can tell an ignored trigger from a frame
        lost in transport, so neither says the recording is misaligned.

        In frame_id mode the frames the camera acquired come from its own
        IDs: the last block ID of each arm, summed. A frame lost before the
        last one that reached the host is inside that count, so what is
        left is triggers ignored, or frames lost after the last delivered
        one of an arm (a stall's, or the recording's last)."""
        line = cam.trigger_line
        if cam.block_id_source == "trigger_counter":
            unexplained = edges - frames
            if unexplained <= 0:
                return []
            return [f"its trigger input ({line}) counted {edges} edges but "
                    f"only {frames} frames reached the host, so "
                    f"{unexplained} trigger(s) were ignored or their frames "
                    f"were lost in transport; this camera has no exposure "
                    f"counter to tell the two apart. Its block IDs count the "
                    f"edges, so each is a gap, which alignment drops from "
                    f"every camera."]
        acquired = w["id_frames"]
        unexplained = edges - acquired
        if unexplained <= 0:
            return []
        advice = (" Set camera.flir.block_id_source: trigger_counter, which "
                  "this camera offers, so an ignored trigger becomes a gap."
                  if cam.counter_chunk_ok else "")
        return [f"its trigger input ({line}) counted {edges} edges and its "
                f"frame IDs account for {acquired} frames acquired, so "
                f"{unexplained} trigger(s) were ignored, or their frames were "
                f"lost after the last frame that reached the host in an "
                f"acquisition" + (" (it was re-armed after a stall)"
                                  if w["rearms"] else "")
                + ". This camera has no exposure counter to tell the two "
                f"apart. Each ignored trigger moves its later block IDs one "
                f"trigger away from the other cameras'." + advice]

    @classmethod
    def sdk_report(cls) -> str:
        """The Spinnaker C library this process uses: its version and path,
        or why it cannot be loaded. Never raises."""
        try:
            api = shared_api()
        except FlirSdkUnavailable as e:
            # The error lists every folder searched, one per line; the
            # preflight line keeps the first sentence and the count.
            head = str(e).split(" Searched:")[0].splitlines()[0]
            searched = len(getattr(e, "searched", ()))
            return (f"Spinnaker C library not available: {head}"
                    + (f" ({searched} locations searched)" if searched else ""))
        except Exception as e:
            return f"Spinnaker C library failed to load ({type(e).__name__}: {e})"
        try:
            version = api.library_version()
        except Exception as e:
            version = f"version unknown ({type(e).__name__}: {e})"
        return f"Spinnaker C {version} ({api.dll_path})"
