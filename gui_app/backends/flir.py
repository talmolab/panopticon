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
unwraps it with the device clock, which counts the trigger periods across
each wrap, so a trigger the camera ignored at a wrap is a gap
(`_id16_step`).

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
Two counters cannot be read at one instant, so an edge that lands between
the reads at a stop or a re-arm may be on either side of the boundary. Each
boundary reads until it settles or records how many edges it left
unresolved, and a count with unresolved edges is reported as a range. A
trigger ignored at a re-arm after BeginAcquisition arms the camera and before
the first read after it returns is counted as down time
(`_note_restart_counters`). A counter narrower than 2**31 counts modulo its
period. Its reads prove a count only while the frames and the edges of stall
re-arm down time stay under half the period, so past that the witness says it
has no count for the recording (`_wrap_reach`). The witness never stays
silent when its counts cannot be proven to fit: counters that do not count,
reads that disagree and edges it could not place each give a sentence that
says the recording has no witness or is not proven aligned.

Silence is not proof in these cases, each outside what two counters can
see: a trigger ignored between the arming inside BeginAcquisition and the
first read after it (counted as down time); an exposure that starts later
than the TriggerDelay plus one register read at a re-arm; a counter
narrower than 2**31 that ignored a whole multiple of its period, which
`frame_sync.check_block_id_rate` reports because the camera then ignored
more than twice the frames it delivered; and two counters that count the
wrong events but agree. A frame_id camera without an edge counter has no
witness: it gets no sentence, and only the log says the witness is off.

UNKNOWNS
Several behaviours are unknown until a volunteer's probe measures them on
real cameras; each is marked where the code depends on it: whether the
ExposureTime maximum follows AcquisitionFrameRate, the spelling of chunk
names, the CounterValue chunk's timing, which counter the chunk carries on a
model without ChunkCounterSelector (`_select_edge_counter`), how wide a
model's counters are (the CounterValue maximum, `_wrap_reach`), whether a
model's counters can be read while it streams, how long after its edge and
its TriggerDelay a camera counts the exposure (the witness assumes less than
one register read), whether a trigger whose delayed exposure has not started
when EndAcquisition runs is still exposed (if not, the witness counts it as
ignored), whether a camera keeps exposing while its host has stopped taking
frames (a stall), which temperature status and threshold nodes a model has,
and which DeviceTemperatureSelector entry its DeviceTemperatureStatus and
DeviceTemperatureStatusTransition thresholds refer to (`_temp_basis`).
"""
from __future__ import annotations

import math
import statistics
import threading
import time
from pathlib import Path

from gui_app import logging_setup
from gui_app.backends._spinc import (
    SPINNAKER_ERR_ACCESS_DENIED, SPINNAKER_ERR_RESOURCE_IN_USE, FlirError,
    FlirSdkUnavailable, FlirTimeout, SpinC)

__all__ = ["FlirBackend", "FlirCamera", "FlirConfigError", "FlirDevice",
           "FlirFrameError", "FlirRateError", "FlirResult",
           "FlirSdkUnavailable", "FlirTimeout", "GRAB_STRATEGY", "shared_api"]

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
#: Attempts at one settled read of both witness counters while the camera
#: acquires (`FlirCamera._read_settled`). An attempt fails only when an edge
#: lands between its two edge reads, so at a trigger period several times
#: the read time the first attempts settle; a trigger rate that leaves no
#: quiet interval ends with the edges it could not place recorded.
COUNTER_READ_TRIES = 8

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


def _temp_status_rank(word: str) -> int:
    """How serious a DeviceTemperatureStatus word is: Ok 0, Critical 1, and
    2 for Error or a word `_TEMP_STATUS_WORDS` does not list, which the
    watch treats as over temperature."""
    return {"Ok": 0, "Critical": 1}.get(
        _TEMP_STATUS_WORDS.get(word.lower()), 2)


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


class FlirRateError(FlirConfigError):
    """The camera cannot record at the frame rate asked: its link cannot
    carry the frames, AcquisitionFrameRate cannot reach the rate, or the
    exposure does not fit the trigger period. Recording anyway skips
    triggers or drops frames, so this is a reason to refuse an acquisition
    (`FlirBackend.RefusalException`), not a warning after it."""


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


def _same_value(want, got: str) -> bool:
    """Whether a read-back matches the value written: as numbers when both
    are numbers (within a camera's float rounding), else as text."""
    words = {"true": 1.0, "false": 0.0}

    def num(v):
        if isinstance(v, bool):
            return float(v)
        text = str(v).strip().lower()
        return words[text] if text in words else float(text)
    try:
        a, b = num(want), num(got)
        return abs(a - b) <= max(1e-6, 1e-4 * abs(a))
    except (KeyError, TypeError, ValueError):
        return str(want).strip() == str(got).strip()


def _json_text(value) -> str:
    """A dict for a log line, compact; never raises."""
    import json
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return repr(value)


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
        #: While a list, each write appends (node, requested, read back) to
        #: it; None outside a configuration phase (FlirBackend._phase_begin
        #: and _phase_end). A write from a grab thread (the counter reset at
        #: StartGrabbing) is never inside one, so it reads nothing back.
        self.writes = None

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

    def refuse(self, text: str, cls=FlirConfigError):
        raise cls(f"{self.who}: {text}")

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
        if self.writes is not None:
            label = name if which == "device" else f"{which}.{name}"
            self.writes.append((label, value, self._read_back(h, fn)))

    def _read_back(self, h, setter) -> str:
        """The value a node reads after a write, as text; never raises."""
        api = self.api
        getter = {api.int_set: api.int_get, api.float_set: api.float_get,
                  api.bool_set: api.bool_get,
                  api.enum_set_symbolic: api.enum_get_symbolic}.get(setter)
        try:
            if getter is None or not api.node_readable(h):
                return "not readable"
            value = getter(h)
        except Exception as e:
            return f"unreadable ({type(e).__name__})"
        if isinstance(value, float):
            return f"{value:g}"
        return str(value)

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

    #: The TriggerDelay `set_triggered` read back, in seconds. An exposure
    #: starts this long after its edge, so a witness read waits it before it
    #: counts exposures (`_read_settled`).
    _trigger_delay_s = 0.0

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
        # DeviceTemperatureSelector entry -> the status transition
        # thresholds read under it, learned at the first thermals() poll
        # that finds the threshold nodes (`FlirBackend._thermals`).
        self._temp_limits = None
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
        # The 16-bit cycle once learned, and whether the camera has shown the
        # raw IDs 0 and 65535 at a wrap; kept across arms (`_id16_step`).
        self._id16_cycle = None
        self._id16_saw_zero = False
        self._id16_saw_top = False
        # Trigger-counter state, reset with the counter.
        self._ctr_prev = None
        self._ctr_acc = 0
        self._ctr_period = 0
        self._ctr_latch = 0
        self._ctr_first = True
        self._ctr_latch_known = False
        self._ctr_first_excess = None
        self._ctr_name = None
        self._last_counter = None
        # Whether the frame ID restarts at every arm and counts frames, and
        # what makes it 1-based; the latch decision uses it.
        self._fid_evidence = False
        self._fid_base_offset = 0
        #: The witness for the current triggered arm, filled at stops.
        self.witness = None
        self._late_read_logged = False
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
        stream was down are not counted as ignored triggers
        (`_note_restart_counters`). A camera without an exposure counter also
        has its edges read just before BeginAcquisition
        (`_note_begin_counters`). In trigger-counter mode the edge counter is
        selected just before BeginAcquisition (`_select_edge_counter`)."""
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
        if rearm:
            self._note_begin_counters()
        if self._triggered:
            self._select_edge_counter()
        self._api.begin(self.handle)
        self._grabbing = True
        if rearm:
            self._note_restart_counters()

    def StopGrabbing(self) -> None:
        """End the stream (EndAcquisition). Every image must be released
        first, which the grab loop guarantees.

        The trigger counters are read on both sides of EndAcquisition. The
        exposures and then the edges are read while the camera still
        acquires, so an edge that arrives while acquisition stops is not
        taken for an ignored trigger. The exposures are read again once it
        has stopped. No exposure starts after that, so every edge counted
        before the stop has its exposure in that count, however late the
        exposure started (a TriggerDelay, or readout holding it back). When
        an exposure started between the two exposure reads, the edges are
        read once more to bound the edges this stop leaves on an unknown side
        (`_stop_unresolved`). A camera whose counters cannot be read while it
        streams is read after EndAcquisition only. When the stream cannot be
        stopped, the camera may still expose, so the witness records the
        error instead of counts."""
        was = self._grabbing
        self._grabbing = False
        witness = was and self._triggered
        try:
            streaming = self._api.is_streaming(self.handle)
            before = self._read_before_end() if witness and streaming else None
            if streaming:
                self._api.end(self.handle)
        except Exception as e:
            if witness and self.witness is not None \
                    and not self.witness["error"]:
                self._witness_failed("could not be read at the stop, because "
                                     "the stream did not stop", e)
            raise
        if witness:
            self._read_counters_at_stop(before)

    def IsGrabbing(self) -> bool:
        return self._grabbing

    # ------------------------------------------------------------ block IDs
    def _bid_frame_id(self, img) -> int:
        return self._api.image_frame_id(img)

    def _bid_frame_id_plus1(self, img) -> int:
        return self._api.image_frame_id(img) + 1

    def _bid_frame_id16(self, img) -> int:
        """A 16-bit frame ID unwrapped into a value that does not wrap.

        Each wrap is resolved by `_id16_step`. The timestamp read here is
        one more SDK call per frame, made only on a GigE camera without
        extended IDs. A wrap it cannot resolve leaves the arm without an
        ordinal: this image and every later one of the arm raise
        FlirFrameError, and the grab loop retires the camera after its
        consecutive-error limit."""
        lost = self._id_lost
        if lost is not None:
            raise FlirFrameError(lost)
        raw = self._api.image_frame_id(img)
        ts = self._ts(img)
        prev = self._prev_raw
        if prev is None:
            self._id_ref = (raw + self.id_offset + self._id_acc, ts)
        elif raw < prev - _ID16_FULL // 2:
            self._id_acc += self._id16_step(prev, raw, ts) + prev - raw
        self._prev_raw = raw
        self._prev_ts = ts
        return raw + self.id_offset + self._id_acc

    def _id16_step(self, prev: int, raw: int, ts: int) -> int:
        """The block-ID step across the 16-bit wrap from raw ID `prev` to
        `raw` (at device time `ts`, in ns), or FlirFrameError.

        The counter's cycle is 65535 IDs (1..65535 as a GigE Vision block
        ID, or 0..65534) or 65536 (0..65535), and with frames lost at the
        wrap the IDs alone do not tell which. The device clock counts the
        frame periods since the previous frame (`_id16_periods`). The cycle
        is learned once and kept for this camera across arms: 65536 once
        the camera has shown both 0 and 65535, or 65535 when the clock's
        count equals that cycle's step, since 65536 would then need more
        frames than periods.

        The step is the clock's count whenever that is at least the step of
        the camera's cycle, or of the 65536 cycle while the cycle is not
        known. A trigger the camera ignored at the wrap consumed no ID, so
        on either cycle it becomes a gap, and `wrap_gaps` counts it for the
        witness. With the cycle not yet known that count leaves out the one
        more a 65535 cycle would mean, which errs toward the witness's
        warning. When the clock does not count a whole number of periods,
        or counts fewer than a known cycle's step, the step is the cycle's;
        with no known cycle the arm has no ordinal from here."""
        if prev == _ID16_SKIP0:
            self._id16_saw_top = True
        if raw == 0:
            self._id16_saw_zero = True
        step = {c: raw - prev + c for c in (_ID16_SKIP0, _ID16_FULL)}
        k, problem = self._id16_periods(prev, ts)
        cycle, why = self._id16_cycle, ""
        if (cycle != _ID16_FULL and self._id16_saw_zero
                and self._id16_saw_top):
            cycle, why = _ID16_FULL, "the camera has shown both 0 and 65535"
        elif cycle is None and k is not None and k == step[_ID16_SKIP0]:
            cycle, why = _ID16_SKIP0, ("a 65536 cycle would need more frames "
                                       "than periods")
        self._id16_cycle = cycle
        head = (f"[flir] {self.serial}: 16-bit frame ID wrapped from {prev} "
                f"to {raw}")
        if cycle is None:
            if k is None or k < step[_ID16_FULL]:
                self._id16_lose(prev, raw, problem or (
                    f"the device clock counts {k} frame period(s) between "
                    f"them, fewer than the {step[_ID16_SKIP0]} or "
                    f"{step[_ID16_FULL]} IDs the two cycles give"))
            gaps = k - step[_ID16_FULL]
            fits = (f"which a 65536 cycle gives, and a 65535 cycle with 1 "
                    f"trigger ignored at the wrap" if not gaps else
                    f"which a 65536 cycle gives with {gaps} trigger(s) "
                    f"ignored at the wrap, and a 65535 cycle with {gaps + 1}")
            print(f"{head}; the device clock counts {k} frame period(s), "
                  f"{fits}. The block ID follows the clock, so an ignored "
                  f"trigger there is a gap", flush=True)
            self._add_wrap_gaps(gaps)
            return k
        known = f"the cycle is {cycle}" + (f" ({why})" if why else "")
        if k is not None and k >= step[cycle]:
            gaps = k - step[cycle]
            print(f"{head}; the device clock counts {k} frame period(s) and "
                  f"{known}"
                  + (f", so {gaps} trigger(s) ignored at the wrap are a gap "
                     f"in the block IDs" if gaps else ""), flush=True)
            self._add_wrap_gaps(gaps)
            return k
        clock = problem or (f"the device clock counts {k} frame period(s), "
                            f"fewer than the {step[cycle]} IDs of that cycle")
        print(f"{head}; {known}, and {clock}, so the block ID steps by the "
              f"cycle", flush=True)
        return step[cycle]

    def _id16_periods(self, prev: int, ts: int) -> tuple:
        """`(k, None)`, the whole number of frame periods the device clock
        puts between the frame before the wrap (raw ID `prev`) and time
        `ts`, or `(None, why not)`. The frame rate is this arm's own:
        unwrapped IDs over device time, from its first frame to the frame
        before the wrap, and the count must land within ID16_RESID of a
        whole number."""
        ref_bid, ref_ts = self._id_ref
        prev_bid = prev + self.id_offset + self._id_acc
        prev_ts = self._prev_ts
        if prev_bid <= ref_bid or prev_ts <= ref_ts:
            return None, ("this acquisition has no earlier frame to measure "
                          "its frame rate from")
        periods = (ts - prev_ts) * (prev_bid - ref_bid) / (prev_ts - ref_ts)
        k = round(periods)
        if k < 1 or abs(periods - k) > ID16_RESID:
            return None, (f"the device clock puts {periods:.2f} frame periods "
                          f"between them, not a whole number")
        return k, None

    def _id16_lose(self, prev: int, raw: int, problem: str) -> None:
        """Leave the arm without an ordinal after the wrap from `prev` to
        `raw`, and raise FlirFrameError."""
        self._id_lost = (
            f"{self.who}: the 16-bit frame ID wrapped from {prev} to {raw}, "
            f"{problem}, and this camera's cycle (65535 or 65536 IDs) is not "
            f"known yet, so this frame and every later one of this "
            f"acquisition have no trigger ordinal. Set camera.flir."
            f"extended_ids: true if the camera offers GevGVSPExtendedIDMode.")
        print(f"[flir] {self._id_lost}", flush=True)
        raise FlirFrameError(self._id_lost)

    def _add_wrap_gaps(self, n: int) -> None:
        w = self.witness
        if n and w is not None:
            w["wrap_gaps"] += n

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

        The chunk is taken to carry the count including the edge that
        triggered the image, so the first trigger reads 1. The recording's
        first image can prove that the camera latches the count before the
        edge instead (`_first_counter_image`); from then on 1 is added, and
        the log says so. When it proves neither, the stop checks the count
        (`_latch_sentences`). A counter narrower than 2**31 is unwrapped
        here."""
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
        """Check the recording's first image, whose chunk count is `v`, for
        proof that the camera latches CounterValue before the edge.

        The image's trigger is edge T: its ordinal among the frames the
        camera acquired, plus the triggers it ignored before it. A count
        that includes the edge reads T, and one latched before the edge
        reads T - 1. So 0 proves the latch before the edge, and so does a
        count below the frame ID on a camera whose frame ID restarts at
        every arm and counts frames. Any other count fits both, because a
        count latched before the edge also reads T when the camera ignored
        one more trigger before this image. That count is taken to include
        the edge, `_ctr_first_excess` keeps how far it is above the frame
        ID, and `_latch_sentences` checks it at stop. The frame ID is one
        more SDK call, on this image only."""
        if v == 0:
            fid = None
        elif self._fid_evidence:
            fid = self._api.image_frame_id(img) + self._fid_base_offset
            if v >= fid:
                self._ctr_first_excess = v - fid
                return
        else:
            return
        self._ctr_latch_known = True
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

    def _read(self, order) -> dict:
        """The witness counters named in `order` ("edges", "exposures"),
        read one after the other in that order. A counter this camera does
        not have is left out."""
        sel = {what: s for s, what in self.counters.items()}
        return {what: self._counter_value(sel[what]) for what in order
                if what in sel}

    def _reset_counters(self) -> None:
        """Reset the witness counters for a new triggered arm.

        In trigger-counter mode the counter is the block ID, so a reset that
        fails refuses the arm. Otherwise the witness is diagnostic: a failed
        reset leaves a witness that holds only the error, which
        `acquisition_warnings` reports."""
        self.witness = None
        self._ctr_prev = None
        self._ctr_acc = 0
        self._ctr_latch = 0
        self._ctr_first = True
        self._ctr_latch_known = False
        self._ctr_first_excess = None
        self._last_counter = None
        if not self.counters:
            return
        self.witness = self._new_witness()
        try:
            for sel in self.counters:
                self.nodes.sete("CounterSelector", sel)
                self.nodes.execute("CounterReset")
        except Exception as e:
            if self.block_id_source == "trigger_counter":
                self.witness = None
                raise
            self._witness_failed("could not be reset when the camera was "
                                 "armed", e)
            print(f"[flir] {self.serial}: the trigger counters "
                  f"{self.witness['error']}; this acquisition has no trigger "
                  f"witness", flush=True)

    def _select_edge_counter(self) -> None:
        """In trigger-counter mode, leave CounterSelector on the edge
        counter for BeginAcquisition.

        The block IDs come from the CounterValue chunk, which
        ChunkCounterSelector points at the edge counter. A model without
        ChunkCounterSelector may fill the chunk from the counter
        CounterSelector names when acquisition starts, and the witness reads
        leave it on the exposure counter (the reset ends on it, and so does
        a stop's last read). Such a chunk would count exposures, so a
        trigger the camera ignored would leave no gap. A failed write
        refuses the arm, as a failed counter reset does. The reads made
        while the camera streams end on the edge counter too."""
        if self.block_id_source != "trigger_counter" or not self.counters:
            return
        self.nodes.sete("CounterSelector", EDGE_COUNTER)

    @staticmethod
    def _new_witness() -> dict:
        """The witness of a triggered arm whose counters were just reset.

        `unresolved` counts the edges that the reads bounding a re-arm's
        down time left on an unknown side, and `stop_unresolved` those of
        the latest stop (`_stop_unresolved`), until a re-arm adds them to
        `unresolved`. `begin_edges` is the edge count read just before a
        re-arm's BeginAcquisition on a camera without an exposure counter."""
        return {"edges": None, "exposures": None, "exposures_check": None,
                "gap_edges": 0, "stopped_at": None, "error": None,
                "rearms": 0, "id_frames": 0, "late_reads": 0,
                "ignored_by_rearm": 0, "wrap_gaps": 0, "gaps_by_rearm": 0,
                "unresolved": 0, "stop_unresolved": 0, "begin_edges": None}

    def _witness_failed(self, what: str, e: BaseException) -> None:
        """Record why the witness stopped counting. `what` completes "the
        trigger counters ...", and the sentence for WARNINGS.txt quotes
        it."""
        self.witness["error"] = f"{what} ({type(e).__name__}: {e})"

    def _ctr_delta(self, later: int, earlier: int) -> int:
        """`later - earlier` for two reads of one counter, across a wrap of
        a counter narrower than 2**31."""
        period = self._ctr_period
        return (later - earlier) % period if period else later - earlier

    def _ctr_signed(self, a: int, b: int) -> int:
        """`a - b` for reads of two counters, where `a` may be the smaller:
        a counter narrower than 2**31 gives the difference nearest 0."""
        period = self._ctr_period
        if not period:
            return a - b
        d = (a - b) % period
        return d - period if d >= period // 2 else d

    def _read_settled(self) -> tuple:
        """`(counts, unresolved)`: the edges and the exposures at one moment
        while the camera acquires, and how many of the edges counted since
        the first read may hide an ignored trigger.

        The edges are read, then the exposures once the TriggerDelay has
        passed, then the edges again, until two edge reads agree or
        COUNTER_READ_TRIES attempts have run. The wait gives every edge the
        first read counted the time to start its exposure, which begins the
        TriggerDelay after the edge. Two edge reads that agree put no edge
        between them, so every exposure read has its edge in the count, and
        every edge counted has its exposure in the count unless the exposure
        started more than one register read after the delay (the module's
        UNKNOWNS). When no attempt settles, the last attempt's counts are
        kept.

        Every edge after the first read arrived while the camera was armed.
        One that the exposures read since the first exposure read do not
        account for was ignored, or was exposed after the last exposure
        read. The caller counts it as down time, which takes one ignored
        trigger out of the count in either case. So `unresolved` is the
        edges since the first read less the exposures since the first
        exposure read: 0 when the first attempt settles, and too high only
        by an edge exposed before the first exposure read. A camera without
        an exposure counter reads its edges once."""
        sel = {what: s for s, what in self.counters.items()}
        if "exposures" not in sel:
            return self._read(("edges",)), 0
        start = first = self._counter_value(sel["edges"])
        start_exposures = None
        for _ in range(COUNTER_READ_TRIES):
            if self._trigger_delay_s:
                time.sleep(self._trigger_delay_s)
            exposures = self._counter_value(sel["exposures"])
            if start_exposures is None:
                start_exposures = exposures
            last = self._counter_value(sel["edges"])
            if not self._ctr_delta(last, first):
                break
            first = last
        unresolved = (self._ctr_delta(last, start)
                      - self._ctr_delta(exposures, start_exposures))
        return {"edges": last, "exposures": exposures}, max(0, unresolved)

    def _note_begin_counters(self) -> None:
        """Read the edges just before a re-arm's BeginAcquisition, on a
        camera without an exposure counter. Every edge up to this read
        arrived while the stream was down (`_note_restart_counters`)."""
        w = self.witness
        if (w is None or w["error"] or w["stopped_at"] is None
                or "exposures" in self.counters.values()):
            return
        try:
            w["begin_edges"] = self._read(("edges",))["edges"]
        except Exception as e:
            self._witness_failed("could not be read before a stall re-arm", e)

    def _note_restart_counters(self) -> None:
        """Leave the edges of a re-arm's down time out of the witness.

        Called once BeginAcquisition has returned. The window runs from the
        stop's reads to a settled read now (`_read_settled`), so it covers
        all the time the camera could not expose, BeginAcquisition included.
        At its start the edges were read before EndAcquisition and the
        exposures after it, so the exposure of every edge before the window
        is outside it. Edges minus exposures over the window is taken for
        the down-time edges.

        The window also holds time the camera is armed: the end of
        BeginAcquisition after it arms the camera, and the reads after it
        returns. A trigger ignored there is counted as down time. The edges
        from the first read after BeginAcquisition on that the exposures do
        not account for are added to `unresolved`, with the edges the stop
        left on an unknown side (`_stop_unresolved`). A trigger ignored
        between the arming and that first read stays hidden. It shifts block
        IDs only when the camera delivered a frame after the arming and
        before it, because grab_thread realigns a re-armed camera at its
        first delivered frame.

        With no exposure counter every edge in the window is left out, and
        the edges from the read before BeginAcquisition to this one are
        unresolved, because the camera may have exposed any of them once
        BeginAcquisition armed it."""
        w = self.witness
        if w is None or w["error"] or w["stopped_at"] is None:
            return
        try:
            now, unresolved = self._read_settled()
        except Exception as e:
            self._witness_failed("could not be read at a stall re-arm", e)
            return
        before = w["stopped_at"]
        down = self._ctr_delta(now["edges"], before["edges"])
        if "exposures" in now and "exposures" in before:
            down -= self._ctr_delta(now["exposures"], before["exposures"])
            # The triggers ignored so far all came in an acquisition that
            # ended in a stall re-arm (`_ignored_sentences`).
            upto = self._ctr_signed(before["edges"] - w["gap_edges"],
                                    before["exposures"])
            w["ignored_by_rearm"] = max(w["ignored_by_rearm"], upto)
            w["gaps_by_rearm"] = w["wrap_gaps"]
        elif w["begin_edges"] is not None:
            unresolved = self._ctr_delta(now["edges"], w["begin_edges"])
        w["gap_edges"] += max(0, down)
        w["unresolved"] += w["stop_unresolved"] + unresolved
        w["stop_unresolved"] = 0
        w["begin_edges"] = None
        w["rearms"] += 1

    def _read_before_end(self):
        """The witness counters just before EndAcquisition, the exposures
        and then the edges, or None when there is no witness or the read
        fails.

        A failed read is counted in the witness (`late_reads`) and logged
        once per camera. The stop then reads every counter after
        EndAcquisition, so an edge that arrives while acquisition stops
        counts as an ignored trigger and none is hidden. At a stall re-arm
        such an edge falls in the acquisition that ended there, whose ignored
        triggers the witness words as not proven to shift the block IDs
        (`_ignored_sentences`)."""
        w = self.witness
        if w is None or w["error"]:
            return None
        try:
            return self._read(("exposures", "edges"))
        except Exception as e:
            w["late_reads"] += 1
            if not self._late_read_logged:
                self._late_read_logged = True
                print(f"[flir] {self.serial}: the trigger counters could not "
                      f"be read while the camera streams ({type(e).__name__}: "
                      f"{e}), so they are read after EndAcquisition; an edge "
                      f"that arrives while acquisition stops then counts as "
                      f"an ignored trigger", flush=True)
            return None

    def _read_counters_at_stop(self, before) -> None:
        """Record the counters at a stop, after EndAcquisition. `before` is
        the read StopGrabbing made before EndAcquisition, or None. The arm's
        last block ID is banked here too.

        The edges are `before`'s and the exposures are read now; without
        `before`, both are read now, the exposures first. `exposures_check`
        keeps the exposures read before the edges, the bound a working
        exposure counter meets (`_implausible_counts`)."""
        w = self.witness
        if w is None:
            return
        w["id_frames"] += self._arm_last_bid
        self._arm_last_bid = 0
        if w["error"]:
            return
        try:
            if before is None:
                now = self._read(("exposures", "edges"))
                check = now.get("exposures")
                unresolved = 0
            else:
                now = dict(before, **self._read(("exposures",)))
                check = before.get("exposures")
                unresolved = self._stop_unresolved(before, now)
        except Exception as e:
            self._witness_failed("could not be read at the stop", e)
            return
        w["stopped_at"] = now
        w["edges"] = now.get("edges")
        w["exposures"] = now.get("exposures")
        w["exposures_check"] = check
        w["stop_unresolved"] = unresolved

    def _stop_unresolved(self, before, now) -> int:
        """The edges a stop leaves on an unknown side. `before` holds the
        exposures and then the edges read before EndAcquisition, `now` the
        exposures read after it.

        An edge that lands between the edge read and EndAcquisition and is
        exposed before EndAcquisition adds an exposure without its edge,
        which hides one ignored trigger. Such edges number no more than the
        exposures started after the first exposure read, nor more than the
        edges counted after the edge read. So when an exposure started in
        between, the edges are read once more, now that the camera no longer
        exposes, and the smaller of the two counts is the bound. None
        started means none was hidden."""
        if "exposures" not in now:
            return 0
        late = self._ctr_delta(now["exposures"], before["exposures"])
        if not late:
            return 0
        after = self._read(("edges",))["edges"]
        return min(late, self._ctr_delta(after, before["edges"]))


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

    #: The exception class that means a camera cannot record at the frame
    #: rate asked. Raised by `open` for the profile's frame_rate and by
    #: `exposure_ceiling_us` for the acquisition's; from the second, it is a
    #: reason to refuse the start, because a recording made anyway skips
    #: triggers or drops frames.
    RefusalException = FlirRateError

    #: What bounds the exposure on this backend, in the words of the
    #: '[camN] exposure=... (ceiling ... at N fps, <this>)' log line, where
    #: the Basler backend names its AcquisitionFrameRate limiter.
    CEILING_BASIS = "the ExposureTime limit this camera reports"

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

    # ----------------------------------------------------- read-back logging
    #: Writes per read-back line, so a phase with dozens of writes stays
    #: readable.
    READBACK_PER_LINE = 8

    def _phase_begin(self, cam):
        """Start collecting cam's writes for the read-back log, at verbose.
        Returns whether this call started it: a phase inside another (the
        exposure check at open) adds its writes to the outer one."""
        n = cam.nodes
        if n.writes is not None or not logging_setup.verbose():
            return False
        n.writes = []
        return True

    def _phase_end(self, cam, started: bool, phase: str) -> None:
        """Print what the phase wrote, each as requested -> read back, and
        stop collecting. The callers run it in a finally, so a refusal
        part-way still logs the writes that happened."""
        if not started:
            return
        rows, cam.nodes.writes = cam.nodes.writes or [], None
        if not rows:
            return
        per = self.READBACK_PER_LINE
        parts = []
        for name, want, got in rows:
            want_txt = f"{want:g}" if isinstance(want, float) else str(want)
            if got == "not readable" or str(got).startswith("unreadable"):
                # A write-only node, or a read that failed: not a mismatch.
                mark = " (not read back)"
            else:
                mark = "" if _same_value(want, got) else " (differs)"
            parts.append(f"{name} {want_txt} -> {got}{mark}")
        chunks = [parts[k:k + per] for k in range(0, len(parts), per)]
        for k, chunk in enumerate(chunks):
            part = f" ({k + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            print(f"[flir] {cam.serial} {phase} read-back{part}: "
                  + ", ".join(chunk), flush=True)

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
        started = self._phase_begin(cam)
        try:
            self._configure(cam, frame_size, frame_rate)
        except BaseException:
            self._phase_end(cam, started, "open (refused part-way)")
            self._deinit_after_failed_open(cam)
            raise
        self._phase_end(cam, started, "open")
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
                f"loaded it (for another profile, a capture worker or the "
                f"launch check). Set the PANOPTICON_SPINNAKER_DIR environment "
                f"variable to {want} and restart Panopticon, or remove "
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
        if logging_setup.verbose():
            print(f"[flir] {cam.serial}: self-test result "
                  f"{_json_text(cam.selftest)}", flush=True)
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
                     f"ROI, or check the link{speed}.", FlirRateError)
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
                     f"or set it to auto.", FlirRateError)

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
                         f"this camera (LineMode {n.gete('LineMode')}). Wire "
                         f"the trigger to another input line, or save the "
                         f"line as Input in a SpinView user set and name that "
                         f"user set in camera.flir.user_set. Every open loads "
                         f"that user set first.")
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
                f"exposure_us or add light.", FlirRateError)

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
        """Width, height and pixel format read back from the camera, with
        the model, the interface, and the firmware version and link speed
        when the camera reports them (for the session header)."""
        n = cam.nodes
        out = {"width": n.geti("Width"), "height": n.geti("Height"),
               "pixel_format": n.gete("PixelFormat"), "serial": cam.serial,
               "model": cam.model}
        if cam.interface:
            out["interface"] = cam.interface
        try:
            if n.readable("DeviceFirmwareVersion"):
                out["firmware"] = n.gets("DeviceFirmwareVersion")
        except Exception:
            pass
        try:
            if n.readable("DeviceLinkSpeed"):
                out["link_speed"] = (f"{n.geti('DeviceLinkSpeed') * 8 / 1e6:g}"
                                     f" Mb/s (DeviceLinkSpeed)")
            elif n.readable("DeviceCurrentSpeed", "tldevice"):
                out["link_speed"] = (f"{n.gete('DeviceCurrentSpeed', 'tldevice')}"
                                     f" (DeviceCurrentSpeed)")
        except Exception:
            pass
        return out

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
        """Untriggered preview at `fps`, or the fastest the camera allows.

        The last recording's trigger witness is dropped, because
        `acquisition_warnings` read it at that recording's stop and a later
        recording must not report it again."""
        started = self._phase_begin(cam)
        try:
            self._set_freerun(cam, fps)
        finally:
            self._phase_end(cam, started, "free-run")

    def _set_freerun(self, cam, fps: float) -> None:
        self._stop_if_streaming(cam)
        cam._triggered = False
        cam._arm_pending = False
        cam.witness = None
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
        saying so and naming the pacing the camera actually uses. Any earlier
        trigger witness is dropped, so a camera armed here that never starts
        grabbing reports none; its first StartGrabbing starts a new one."""
        started = self._phase_begin(cam)
        try:
            self._set_triggered(cam, rate_limit, announce)
        finally:
            self._phase_end(cam, started, "trigger mode")

    def _set_triggered(self, cam, rate_limit: float, announce: bool) -> None:
        self._stop_if_streaming(cam)
        cam.witness = None
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
        # The user set can hold a delay the profile does not name, so the
        # witness waits the value the camera reports in trigger mode.
        cam._trigger_delay_s = (max(0.0, n.getf("TriggerDelay")) * 1e-6
                                if n.readable("TriggerDelay") else 0.0)
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
        started = self._phase_begin(cam)
        try:
            return self._set_exposure_gain(cam, exposure_us, gain_db,
                                           gain_unit)
        finally:
            self._phase_end(cam, started, "exposure/gain")

    def _set_exposure_gain(self, cam, exposure_us, gain_db, gain_unit):
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
        started = self._phase_begin(cam)
        try:
            ceiling = self._measure_ceiling(cam, fps)
        finally:
            self._phase_end(cam, started,
                            f"exposure ceiling measurement at {fps:g} fps")
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
                             f"the ROI.", FlirRateError)
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

        Every `DeviceTemperatureSelector` entry is reported as
        `temp_<entry>_c`, and `temp_c` is the one the camera's thresholds are
        compared with (`_temp_basis`), named in `temp_entry`.
        `temp_critical_c` and `temp_shutdown_c` come from SFNC
        `DeviceTemperatureStatusTransition` (NormalToHigh and
        HighToExceeded) when the camera has it, and `temp_limits_entry`
        names the entry they belong to, or reads "unknown". `temp_max_c` is
        the highest `temp_c` this backend has polled since the camera was
        opened, not a device high-water mark. `temp_status` is SFNC
        `DeviceTemperatureStatus`, read under every entry the selector can
        take, the worst word kept: it is translated to Ok, Critical or Error,
        and the camera's own word is kept under `temp_status_camera`. A
        camera that reports no shutdown temperature has no `temp_shutdown_c`,
        and the watch falls back to its status."""
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
        status = (n.node("DeviceTemperatureStatus")
                  if n.readable("DeviceTemperatureStatus") else None)
        # The thresholds are firmware values, read under each entry once.
        learn = cam._temp_limits is None and self._has_temp_limits(cam)
        readings, words, limits = {}, {}, {}

        def read(entry):
            readings[entry] = float(api.float_get(temp))
            if status is not None:
                words[entry] = str(api.enum_get_symbolic(status))
            if learn:
                limits[entry] = self._temp_transitions(cam)

        sel = n.node("DeviceTemperatureSelector")
        if sel is not None and n.writable("DeviceTemperatureSelector"):
            current = api.enum_get_symbolic(sel)
            entries = list(api.enum_entries(sel))
            try:
                for entry in entries:
                    api.enum_set_symbolic(sel, entry)
                    read(entry)
            finally:
                api.enum_set_symbolic(sel, current)
        else:
            entries = None
            read(api.enum_get_symbolic(sel) if sel is not None else "")
        if learn:
            cam._temp_limits = limits
        entry, lim, owner = self._temp_basis(
            readings, cam._temp_limits or {},
            single=sel is None or entries is not None and len(entries) == 1)
        out["temp_c"] = readings[entry]
        if entry:
            out["temp_entry"] = entry
        for name, value in readings.items():
            if name:
                out[f"temp_{name.lower()}_c"] = value
        if cam.temp_max_c is None or out["temp_c"] > cam.temp_max_c:
            cam.temp_max_c = out["temp_c"]
        out["temp_max_c"] = cam.temp_max_c
        if words:
            word = max(words.values(), key=_temp_status_rank)
            out["temp_status_camera"] = word
            out["temp_status"] = _TEMP_STATUS_WORDS.get(word.lower(), word)
        if lim:
            out.update(lim)
            out["temp_limits_entry"] = owner
        return out

    @staticmethod
    def _has_temp_limits(cam) -> bool:
        n = cam.nodes
        return (n.node("DeviceTemperatureStatusTransitionSelector") is not None
                and n.readable("DeviceTemperatureStatusTransition"))

    def _temp_transitions(self, cam) -> dict:
        """The thresholds `_TEMP_TRANSITIONS` names, read under the
        DeviceTemperatureSelector entry now selected, with the transition
        selector put back."""
        n = cam.nodes
        api = self._api
        tsel = n.node("DeviceTemperatureStatusTransitionSelector")
        offered = api.enum_entries(tsel)
        current = api.enum_get_symbolic(tsel)
        out = {}
        try:
            for entry, key in _TEMP_TRANSITIONS:
                if entry in offered:
                    api.enum_set_symbolic(tsel, entry)
                    out[key] = n.getf("DeviceTemperatureStatusTransition")
        finally:
            api.enum_set_symbolic(tsel, current)
        return out

    @staticmethod
    def _temp_basis(readings: dict, limits: dict, single: bool) -> tuple:
        """`(entry, thresholds, owner)`: the DeviceTemperatureSelector entry
        whose reading is `temp_c`, the thresholds it is compared with, and
        the entry those thresholds belong to ("unknown" when the camera does
        not show it). `single` is True for a camera with one entry or none.

        Thresholds that change with the selector belong to their entry, so
        each reading is compared with its own, and the entry with the least
        room below its shutdown point (or its NormalToHigh point) is
        reported. Thresholds that are the same under every entry belong to
        an entry the camera does not name, so they are compared with the
        hottest reading: a cooler location never stands in for the one they
        belong to. A camera without thresholds reports its Sensor reading,
        or its first entry's."""
        if not any(limits.values()):
            entry = "Sensor" if "Sensor" in readings else next(iter(readings))
            return entry, {}, None
        if len({tuple(sorted(v.items())) for v in limits.values()}) > 1:
            def room(e):
                lim = limits.get(e) or {}
                top = lim.get("temp_shutdown_c", lim.get("temp_critical_c"))
                return math.inf if top is None else top - readings[e]
            entry = min(readings, key=room)
            return entry, dict(limits.get(entry) or {}), entry
        entry = max(readings, key=readings.get)
        lim = next(v for v in limits.values() if v)
        return entry, dict(lim), entry if single else "unknown"

    def acquisition_warnings(self, cam, frames_acquired: int) -> list:
        """What the trigger witness found about the acquisition that just
        ended, as sentences for WARNINGS.txt. Never raises.

        With both counters, edges on the trigger line minus exposures
        started is the number of triggers the camera ignored, leaving out
        edges that arrived while a stall re-arm had the stream down. Edges
        that the reads at a stop or a re-arm could not place make that count
        a range, from what the counters prove to that plus the unresolved
        edges. The sentence says what those triggers did to the block IDs
        (`_ignored_sentences`), and in trigger_counter mode a CounterValue
        latch the recording did not settle gets its own
        (`_latch_sentences`). With the edge counter alone the count mixes
        ignored triggers with frames lost in transport, and the sentence
        says so and makes no claim about alignment
        (`_edge_only_sentences`).

        A camera gets no sentence when its counts show no ignored trigger,
        which proves none was ignored except in the cases the module
        docstring lists (the arm window, a late exposure, a narrow counter
        that ignored a whole multiple of its period, counters that count
        the wrong events but agree). A camera without an edge counter has
        no witness and gets no sentence either. Every state the counts
        cannot prove gets a sentence: counters whose reads cannot come from
        counters that count (`_implausible_counts`), counters narrower than
        2**31 in a recording past what their reads can prove
        (`_wrap_reach`), edges the reads could not place (a range), and a
        witness whose counters failed, were never read at a stop, or whose
        sentences could not be built."""
        if logging_setup.verbose():
            print(f"[flir] {cam.serial}: trigger witness at stop "
                  f"(frames acquired {frames_acquired}): "
                  f"{_json_text(getattr(cam, 'witness', None))}", flush=True)
        try:
            return self._witness_sentences(cam, int(frames_acquired))
        except Exception as e:
            why = f"{type(e).__name__}: {e}"
            print(f"[flir] {cam.serial}: trigger witness failed: {why}",
                  flush=True)
            if getattr(cam, "witness", None) is None:
                return []
            return [f"its trigger witness could not be evaluated ({why}), so "
                    f"this recording has no trigger witness for this camera."]

    def _witness_sentences(self, cam, frames: int) -> list:
        w = cam.witness
        if w is None:
            return []
        if w["error"]:
            print(f"[flir] {cam.serial}: the trigger counters {w['error']}; "
                  f"no witness for this acquisition", flush=True)
            return [f"its trigger-witness counters {w['error']}, so this "
                    f"recording has no trigger witness for this camera. Send "
                    f"the output of 'uv run probe_flir.py'."]
        if w["edges"] is None:
            if frames <= 0:
                return []
            print(f"[flir] {cam.serial}: the trigger counters were not read "
                  f"at a stop; no trigger witness for this acquisition",
                  flush=True)
            return [f"its trigger-witness counters were not read when its "
                    f"stream stopped, so this recording has no trigger "
                    f"witness for this camera."]
        edges = w["edges"] - w["gap_edges"]
        exposures = w["exposures"]
        line = cam.trigger_line
        unresolved = w["unresolved"] + w["stop_unresolved"]
        print(f"[flir] {cam.serial}: trigger witness: {w['edges']} edges on "
              f"{line}"
              + (f" ({w['gap_edges']} of them while re-arming)"
                 if w["gap_edges"] else "")
              + (f", {exposures} exposures" if exposures is not None else "")
              + f", {frames} frames delivered"
              + (f", {unresolved} edge(s) not placed at a stop or re-arm"
                 if unresolved else "")
              + (f", last CounterValue {cam._last_counter}"
                 if cam.block_id_source == "trigger_counter" else "")
              + (f" (the counters wrap at {cam._ctr_period})"
                 if cam._ctr_period else ""),
              flush=True)
        dead = self._implausible_counts(cam, w, frames)
        if dead is not None:
            why, edge_counter, alt = dead
            print(f"[flir] {cam.serial}: {why}; no trigger witness for this "
                  f"acquisition", flush=True)
            ids = (f" Its block IDs come from that counter's CounterValue "
                   f"chunk (the last image carried {cam._last_counter}), so "
                   f"check that they advance before using this camera's "
                   f"video." if edge_counter
                   and cam.block_id_source == "trigger_counter" else "")
            if alt is None:
                verdict = (f"{why}, so its trigger-witness counters do not "
                           f"count what Panopticon set them to count on this "
                           f"model, and this recording has no trigger "
                           f"witness for this camera.")
            else:
                verdict = (f"{why}. So either its trigger-witness counters do "
                           f"not count what Panopticon set them to count on "
                           f"this model, or {alt}. Either way this recording "
                           f"has no trigger witness for this camera.")
            return [f"{verdict}{ids} Send the output of 'uv run "
                    f"probe_flir.py'."]
        reach = self._wrap_reach(cam, w, frames)
        latch = self._latch_sentences(cam, w, exact=reach is None)
        if reach is not None:
            print(f"[flir] {cam.serial}: the trigger counters wrap at "
                  f"{cam._ctr_period}, and the frames and down-time edges "
                  f"came to {reach}, past the {self._wrap_half(cam)} under "
                  f"which their reads prove a count; no ignored-trigger "
                  f"count for this acquisition", flush=True)
            return (self._limited_sentences(cam, w, frames, reach,
                                            bool(latch)) + latch)
        return (self._ignored_sentences(cam, w, edges, frames, bool(latch))
                + latch)

    #: What a trigger-counter gap means: when nothing questions the block
    #: IDs, and when the camera's latch warning follows.
    _GAP_DROPPED = "which alignment drops from every camera."
    _GAP_IN_DOUBT = ("Alignment drops a gap from every camera only if the "
                     "block IDs name the right triggers, and the next warning "
                     "about this camera questions that.")

    def _ignored_sentences(self, cam, w, edges: int, frames: int,
                           latch_doubt: bool = False) -> list:
        """The ignored-trigger count from counters that passed the
        plausibility check. `edges` leaves out the re-arm down time.
        `latch_doubt` is True when `_latch_sentences` questions whether the
        trigger-counter block IDs name the right triggers.

        In frame_id mode an ignored trigger shifts every later block ID, with
        these exceptions. One ignored at a 16-bit wrap is a gap
        (`wrap_gaps`, `_id16_step`). One that no delivered frame follows in
        its acquisition shifts nothing: after the last frame the camera
        delivered before a stall re-arm, or at the end of the recording. So
        does one ignored after a re-arm and before the re-armed camera's
        first delivered frame, because grab_thread realigns the camera at
        that frame from its device clock. The witness has no count per
        frame, only one per acquisition, so when every ignored trigger that
        is not a wrap gap came in an acquisition that ended in a re-arm
        (`ignored_by_rearm`), the sentence says the alignment is unproven.
        One ignored in the last acquisition shifts IDs unless it fell at an
        end of that acquisition. The sentence names those ends and still
        advises against using the recording, because every ignored trigger
        would have to fall there. Whether a stalled camera ignores triggers
        at all, or keeps exposing them, is one of the module's UNKNOWNS.

        Unresolved edges (`_unresolved_clause`) make the count a range whose
        low end is what the counters prove. An edge hidden at a re-arm's
        stop lowers the count and the triggers ignored before that re-arm
        alike, and one hidden later lowers only the count, so the low end
        never overstates the triggers ignored in the last acquisition. In
        frame_id mode an unresolved edge may be one more trigger that shifts
        block IDs, so the sentence says the frames are not proven aligned.
        In trigger_counter mode an ignored trigger is a gap, so the sentence
        gives the range of gaps.

        No sentence means the counts prove the camera ignored no trigger:
        the difference plus the unresolved edges is 0. A sum below 0 is a
        pair of reads `_implausible_counts` refuses before this runs.

        A counter narrower than 2**31 reaches this method only while its
        reads prove a count (`_wrap_reach`). The down-time edges are
        subtracted before the difference nearest 0 is taken, and the
        exposures stated are the fewest that fit the frames that reached the
        host (every one of them was exposed). Both equal the reads unless a
        count passed the period, and the sentence says when they differ
        (`_wrap_clause`)."""
        exposures = w["exposures"]
        line = cam.trigger_line
        if exposures is None:
            return self._edge_only_sentences(cam, w, edges, frames,
                                             latch_doubt)
        unresolved = w["unresolved"] + w["stop_unresolved"]
        # Below 0 only when an unresolved edge added an exposure without
        # its edge.
        raw = cam._ctr_signed(w["edges"] - w["gap_edges"], exposures)
        exposed = frames + cam._ctr_delta(exposures, frames)
        edges = exposed + raw
        ignored = max(0, raw)
        top = max(ignored, raw + unresolved)
        outside = " outside stall re-arms" if w["rearms"] else ""
        wrap = (self._wrap_clause(
            cam, f"the exposures stated are the fewest that fit the {frames} "
                 f"frames that reached the host, the edges stated add the "
                 f"ignored triggers to them, and the ignored count is right "
                 f"while it is under {cam._ctr_period // 2}")
                if cam._ctr_period and (exposed != exposures or edges != w[
                    "edges"] - w["gap_edges"]) else "")
        if top == ignored:
            what = (f"its trigger input ({line}) counted {edges} edges"
                    f"{outside} but it started only {exposed} exposures, so "
                    f"it ignored {ignored} trigger(s).{wrap}")
        else:
            what = (f"its trigger input ({line}) counted {edges} edges"
                    f"{outside} and it started {exposed} exposures, so it "
                    f"ignored {ignored} to {top} trigger(s).{wrap}"
                    + self._unresolved_clause(
                        unresolved, "while its trigger counters were read at "
                                    "a stall re-arm or at the stop"))
        if top <= 0:
            return []
        advice = (" Lower camera.exposure_us, or set camera.flir."
                  "block_id_source: trigger_counter so an ignored trigger "
                  "becomes a gap.")
        if cam.block_id_source == "trigger_counter":
            return [f"{what} Its block IDs count the edges, so each ignored "
                    f"trigger is a gap"
                    + (f". {self._GAP_IN_DOUBT}" if latch_doubt
                       else f", {self._GAP_DROPPED}")
                    + " Lower camera.exposure_us to keep those triggers."]
        # Ignored triggers that are not wrap gaps: in the last acquisition
        # (they shift IDs), and in acquisitions that ended in a re-arm.
        by_rearm = min(w["ignored_by_rearm"], ignored)
        gaps = min(w["wrap_gaps"], ignored)
        gaps_by_rearm = min(w["gaps_by_rearm"], gaps, by_rearm)
        shifting = (ignored - by_rearm) - (gaps - gaps_by_rearm)
        unproven = by_rearm - gaps_by_rearm
        if shifting > 0:
            after_rearm = (", or before the first frame it delivered after "
                           "the last stall re-arm," if w["rearms"] else "")
            return [f"{what} Its block IDs count the frames it acquired, so "
                    f"from the first ignored trigger on they name later "
                    f"triggers than the other cameras' do, and its frames are "
                    f"paired with the wrong instants. Only a trigger ignored "
                    f"after the last frame it delivered{after_rearm} shifts "
                    f"nothing, and the witness cannot show where each one "
                    f"fell. Do not use this recording for 3D reconstruction."
                    + advice]
        if top > ignored:
            unless = " or ".join(
                (["at a 16-bit frame-ID wrap"] if cam.id16 else [])
                + (["after the last frame before a stall re-arm"]
                   if w["rearms"] else []))
            return [f"{what} An ignored trigger makes its block IDs from that "
                    f"trigger on name later triggers than the other cameras' "
                    f"do" + (f", unless it fell {unless}" if unless else "")
                    + ". So this camera's frames are not proven aligned."
                    + advice]
        if unproven > 0:
            lead = ("All of them" if not gaps else
                    f"All of them but the {gaps} at a 16-bit frame-ID wrap, "
                    f"which are gaps,")
            return [f"{what} {lead} came before a stall re-arm. One "
                    f"ignored after the last frame the camera delivered "
                    f"before the re-arm shifts no block ID, because the "
                    f"re-arm realigns the camera from its device clock. One "
                    f"ignored earlier makes its block IDs from that trigger "
                    f"on, the re-armed ones included, name later triggers "
                    f"than the other cameras' do. The witness cannot tell "
                    f"the two apart, so this camera's frames are not proven "
                    f"aligned." + advice]
        return [f"{what} Each fell at a 16-bit frame-ID wrap, where its block "
                f"IDs follow the device clock, so each is a gap, "
                f"{self._GAP_DROPPED} Lower camera.exposure_us to keep those "
                f"triggers."]

    @staticmethod
    def _wrap_clause(cam, how: str) -> str:
        """Why the counts a sentence states are not what a counter narrower
        than 2**31 read: `how` says how they were rebuilt."""
        return f" Its trigger counters wrap at {cam._ctr_period}, so {how}."

    @staticmethod
    def _unresolved_clause(unresolved: int, where: str) -> str:
        """Why an ignored-trigger count is a range: `unresolved` edges that
        reached the camera `where`."""
        them = "it" if unresolved == 1 else "them"
        return (f" The count is a range because {unresolved} edge(s) reached "
                f"the camera {where}, and the witness cannot show whether the "
                f"camera exposed {them}.")

    @staticmethod
    def _latch_sentences(cam, w, exact: bool = True) -> list:
        """In trigger_counter mode, when the first image did not prove how
        CounterValue is latched: the check of the count taken to include
        the edge. `exact` is False when the counters' reads prove no count
        (`_wrap_reach`), and then the ignored count settles nothing.

        With such a count, a last image on the last trigger reads the edge
        count at stop. A latch before the edge reads at least one below it,
        and so do triggers at the end that delivered no frame. The first
        image settles it when its count was `_ctr_first_excess` above the
        frame ID and the camera ignored that many triggers in all, because
        a latch before the edge needs one more ignored trigger before that
        image. The ignored count settles it only when it is exact: both
        counters, no stall re-arm, the stop's edges read before
        EndAcquisition, and no edge the stop left on an unknown side
        (`_stop_unresolved`). Otherwise the sentence names both causes.

        On a counter narrower than 2**31 the counts it states are unwrapped:
        the last image's as the block IDs unwrap it (`_bid_counter`), and
        the edges as that count plus the lag, which is right while the lag
        is under the period."""
        if (cam.block_id_source != "trigger_counter" or cam._ctr_latch_known
                or cam._last_counter is None):
            return []
        lag = cam._ctr_delta(w["edges"], cam._last_counter)
        if lag <= 0:
            return []
        last = cam._last_counter + cam._ctr_acc
        excess = cam._ctr_first_excess
        exposures = w["exposures"]
        if (exact and excess is not None and exposures is not None
                and not w["rearms"] and not w["late_reads"]
                and not w["stop_unresolved"]
                and cam._ctr_signed(w["edges"], exposures) == excess):
            return []
        return [f"its first image did not show whether it latches the "
                f"CounterValue chunk before the trigger edge, and its last "
                f"image's count ({last}) is {lag} below the {last + lag} "
                f"edges it counted by the stop. Its last {lag} "
                f"trigger(s) delivering no frame would give this. So would a "
                f"latch before the edge, which makes every block ID of this "
                f"camera one trigger early. Send the output of 'uv run "
                f"probe_flir.py'."]

    @classmethod
    def _implausible_counts(cls, cam, w, frames: int):
        """`(why, edge counter at fault, other cause)` when the counters'
        reads cannot come from counters that counted what they were set to,
        or None. `other cause` is None when nothing else explains the reads.
        Otherwise it names the other explanation, which a counter narrower
        than 2**31 always leaves.

        Every frame that reached the host was exposed on a trigger edge, so
        a working edge counter and a working exposure counter each reach at
        least the frames delivered. Exposures read before the edges never
        pass them (`exposures_check`); the exposures read after
        EndAcquisition may, by the edges that arrived between the reads,
        which `_stop_unresolved` counts, and never fall below the exposures
        read before it. So edges minus exposures, less the
        down-time edges, plus the unresolved edges, is never below 0. With
        the edge counter alone, the edges outside down time plus the
        unresolved ones are never fewer than the frames the camera acquired.
        In trigger_counter mode the last image's CounterValue chunk is the
        edge counter's value at that image, so it never passes the edges
        read at the stop.

        A counter narrower than 2**31 reads its counts modulo its period, so
        a count below the frames is a counter that does not count or one
        that wrapped. Those checks run only while its reads prove a count
        (`_wrap_reach`). The differences are taken nearest 0 modulo the
        period and are checked at any length; below 0 they may also be a
        camera that ignored half the period or more."""
        edges, exposures = w["edges"], w["exposures"]
        check = w["exposures_check"]
        line = cam.trigger_line
        period = cam._ctr_period
        half = period // 2
        slack = w["unresolved"] + w["stop_unresolved"]
        gap = w["gap_edges"]
        if cls._wrap_reach(cam, w, frames) is None:
            wrapped = (f"its counters wrapped at {period} because the camera "
                       f"ignored or lost at least {half} triggers"
                       if period else None)
            if edges < frames:
                return (f"its trigger-line counter counted {edges} edges on "
                        f"{line} for {frames} frames that reached the host",
                        True, wrapped)
            if exposures is not None and exposures < frames:
                return (f"its exposure counter counted {exposures} exposures "
                        f"for {frames} frames that reached the host", False,
                        wrapped)
            if check is not None and check > edges:
                return (f"its exposure counter counted {check} exposures, "
                        f"more than the {edges} edges on {line}", True,
                        wrapped)
            if (check is not None and exposures is not None
                    and exposures < check):
                return (f"its exposure counter read {check} before "
                        f"EndAcquisition and {exposures} after it", False,
                        None)
            last = cam._last_counter
            if (cam.block_id_source == "trigger_counter"
                    and last is not None and cam._ctr_signed(edges, last) < 0):
                return (f"its last image's CounterValue chunk read {last}, "
                        f"more than the {edges} edges on {line} its counter "
                        f"read at the stop", True, None)
        outside = " outside stall re-arms" if w["rearms"] else ""
        within = f", {gap} of them while re-arming," if gap else ""
        even = (f" even with the {slack} edge(s) its reads could not place"
                if slack else "")
        placed = (f" and the {slack} edge(s) its reads could not place"
                  if slack else "")
        if exposures is None:
            acquired = cam.block_id_source == "frame_id"
            base = w["id_frames"] if acquired else frames
            if cam._ctr_signed(edges - gap, base) + slack >= 0:
                return None
            what = ("frames its frame IDs account for" if acquired
                    else "frames that reached the host")
            if not period:
                return (f"its trigger-line counter counted {edges - gap} "
                        f"edges on {line}{outside}, fewer than the {base} "
                        f"{what}{even}", True, None)
            return (f"its trigger-line counter read {edges} edges on {line}"
                    f"{within or ','} which taken modulo {period} is fewer "
                    f"than the {base} {what}{even}", True,
                    f"the camera ignored or lost at least {half} triggers")
        if period and frames and not edges and not exposures:
            return (f"its trigger counters both read 0 after {frames} frames "
                    f"reached the host", True,
                    f"both counts are whole multiples of {period}, the "
                    f"period its counters wrap at")
        if cam._ctr_signed(edges - gap, exposures) + slack >= 0:
            return None
        if not period:
            return (f"its exposure counter counted {exposures} exposures, "
                    f"more than the {edges - gap} edges on {line}{outside}"
                    f"{placed} account for", True,
                    None)
        return (f"its trigger counters read {edges} edges on {line}{within} "
                f"and {exposures} exposures, which taken modulo {period} "
                f"leave more exposures than edges{even}", True,
                f"the camera ignored at least {half} triggers")

    @staticmethod
    def _wrap_half(cam) -> int:
        """The frames and down-time edges at which a counter narrower than
        2**31 stops proving a count: half its period, rounded up."""
        period = cam._ctr_period
        return period - period // 2

    @classmethod
    def _wrap_reach(cls, cam, w, frames: int):
        """The frames and down-time edges the witness had to count, when a
        counter narrower than 2**31 cannot prove a count over them, or None.

        Such a counter reads each count modulo its period. The frames a
        camera acquired and the edges of stall re-arm down time are the
        part of the edge count the witness knows. While they stay under
        half the period, the edges stay under the period for any camera that
        ignored and lost fewer than half the period, so a read below the
        frames proves a counter that does not count, and the differences
        are the true ones. A camera that ignored half the period or more
        reads as a pair of counters that disagree (`_implausible_counts`),
        except one that ignored a whole period or more, whose count can read
        low. That camera ignored more than twice the frames it delivered,
        which `frame_sync.check_block_id_rate` reports. Past half the period
        a counter that does not count can read like one that does, so the
        witness gives no count (`_limited_sentences`)."""
        period = cam._ctr_period
        if not period:
            return None
        reach = max(frames, w["id_frames"]) + w["gap_edges"]
        return reach if reach >= cls._wrap_half(cam) else None

    @classmethod
    def _limited_sentences(cls, cam, w, frames: int, reach: int,
                           latch_doubt: bool = False) -> list:
        """The sentence for a counter narrower than 2**31 in a recording
        past what its reads prove (`_wrap_reach`): the limit, that this
        recording has no count, and what an ignored trigger does to the
        block IDs. `latch_doubt` is as in `_ignored_sentences`."""
        half = cls._wrap_half(cam)
        gap = w["gap_edges"]
        acquired = max(frames, w["id_frames"])
        if gap:
            span = (f"while the frames and the edges of stall re-arm down "
                    f"time number fewer than {half}, and here they number "
                    f"{reach} ({acquired} frames, {gap} edges)")
        else:
            span = (f"in a recording of fewer than {half} frames, and this "
                    f"one has {acquired}")
        lead = (f"its trigger witness is limited: its trigger counters wrap "
                f"at {cam._ctr_period}, so it counts ignored triggers only "
                f"{span}. So this recording has no trigger witness for this "
                f"camera.")
        if cam.block_id_source == "trigger_counter":
            return [f"{lead} Its block IDs count the edges, so each ignored "
                    f"trigger is a gap"
                    + (f". {cls._GAP_IN_DOUBT}" if latch_doubt
                       else f", {cls._GAP_DROPPED}")]
        advice = (" Set camera.flir.block_id_source: trigger_counter, which "
                  "this camera offers, so an ignored trigger becomes a gap."
                  if cam.counter_chunk_ok else
                  f" Keep recordings under {half} frames to keep its count.")
        return [f"{lead} Each ignored trigger moves its later block IDs one "
                f"trigger away from the other cameras'. The block-ID rate "
                f"check still runs, but it reports only a drift above its "
                f"tolerance, which a few ignored triggers in a long recording "
                f"do not reach." + advice]

    @classmethod
    def _edge_only_sentences(cls, cam, w, edges: int, frames: int,
                             latch_doubt: bool = False) -> list:
        """The witness of a camera with an edge counter and no exposure
        counter. Neither branch can tell an ignored trigger from a frame
        lost in transport, so neither says the recording is misaligned.

        In frame_id mode the frames the camera acquired come from its own
        IDs: the last block ID of each arm, summed. A frame lost before the
        last one that reached the host is inside that count, so what is
        left is triggers ignored, or frames lost after the last delivered
        one of an arm (a stall's, or the recording's last).

        The edges from the read before a re-arm's BeginAcquisition to the
        read after it are left out as down time, and any of them the camera
        exposed once armed is a frame counted without its edge. So those
        edges make the count a range. In trigger_counter mode each ignored
        trigger is a gap, so the sentence gives the range of gaps. No
        sentence means the edges left over plus the unresolved ones are 0;
        below 0 is a read `_implausible_counts` refuses before this runs.

        A counter narrower than 2**31 reaches this method only while its
        reads prove a count (`_wrap_reach`). The edges left over are the
        difference nearest 0 of the edges and the frames modulo the period,
        which equals the reads' difference unless the edges passed the
        period, and the edges the sentence states are the frames plus it."""
        line = cam.trigger_line
        outside = " outside stall re-arms" if w["rearms"] else ""
        unresolved = w["unresolved"]
        period = cam._ctr_period

        def left_over(base, what):
            """(edges left over past `base` frames, the edges to state, and
            the clause for a counter that wraps)."""
            d = cam._ctr_signed(w["edges"] - w["gap_edges"], base)
            wrap = (cls._wrap_clause(
                cam, f"the edge count stated is the {base} {what} plus "
                     f"the edges left over modulo {period}, which is right "
                     f"while fewer than {period // 2} are left over")
                    if period and base + d != edges else "")
            return d, base + d, wrap

        def span(unexplained):
            low = max(0, unexplained)
            top = max(low, unexplained + unresolved)
            if top == low:
                return low, top, f"{low}", ""
            return low, top, f"{low} to {top}", cls._unresolved_clause(
                unresolved, "between the edge reads either side of "
                            "BeginAcquisition at a stall re-arm")

        if cam.block_id_source == "trigger_counter":
            d, shown, wrap = left_over(frames, "frames that reached the host")
            low, top, count, why = span(d)
            if top <= 0:
                return []
            but = "but only" if low else "and"
            return [f"its trigger input ({line}) counted {shown} edges"
                    f"{outside} {but} {frames} frames reached the host, so "
                    f"{count} trigger(s) were ignored or their frames "
                    f"were lost in transport.{wrap}{why} This camera has no "
                    f"exposure counter to tell the two apart. Its block IDs "
                    f"count the edges, so each is a gap"
                    + (f". {cls._GAP_IN_DOUBT}" if latch_doubt
                       else f", {cls._GAP_DROPPED}")]
        acquired = w["id_frames"]
        d, shown, wrap = left_over(acquired, "frames its frame IDs account for")
        _low, top, count, why = span(d)
        if top <= 0:
            return []
        advice = (" Set camera.flir.block_id_source: trigger_counter, which "
                  "this camera offers, so an ignored trigger becomes a gap."
                  if cam.counter_chunk_ok else "")
        return [f"its trigger input ({line}) counted {shown} edges{outside} "
                f"and its frame IDs account for {acquired} frames acquired, "
                f"so {count} trigger(s) were ignored, or their frames were "
                f"lost after the last frame that reached the host in an "
                f"acquisition" + (" (it was re-armed after a stall)"
                                  if w["rearms"] else "")
                + f".{wrap}{why} This camera has no exposure counter to tell "
                f"the two apart. Each ignored trigger moves its later block "
                f"IDs one trigger away from the other cameras'." + advice]

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
