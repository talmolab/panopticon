"""Camera backends: the vendor-specific layer, and the contract it must meet.

WHY THIS EXISTS
Everything else in `gui_app/` is vendor-neutral. The vendor SDK calls live in
one module per SDK under this package, and this module states what such a
module must provide: implement `CameraBackend`, register the name, and the
rest of the application does not change.

WHAT IS AND IS NOT ABSTRACTED
The **cold path** (enumerate, open, configure, trigger mode, teardown,
statistics) goes through `CameraBackend` methods. It runs a handful of times per
session, so an extra layer costs nothing and buys clarity.

The **hot path** (retrieving a frame, 100 times a second per camera) is not
wrapped in per-field accessor methods. The backend's `retrieve()` returns a
*native* grab-result object and this module documents the attributes it must
expose (see `GrabResultProtocol`). For the same reason the grab loop drives the
camera handle itself (`StartGrabbing`/`StopGrabbing`/`IsGrabbing`) rather than
the equivalent `CameraBackend` methods; see `CameraHandleProtocol`. A new
backend supplies a thin adapter object rather than paying an indirection per
attribute.

Python call overhead is not the reason. A call is ~60 ns, so seven per frame
cost about 0.4 us per frame per camera, a small fraction of any trigger
period. The reason is that the hot path has invariants a wrapper tends to
break: the frame view must not outlive `Release()`, and it must not be copied
on the way through. A hidden copy here is a full-frame memcpy (width x height
bytes) with the GIL held, which dropped frames on every camera
(docs/HISTORY.md, phase 5). A documented duck-type contract keeps those
invariants visible at the point they matter.

WRITING A NEW BACKEND
1. Implement `CameraBackend` for your SDK, all of it, including the members
   below the `set_freerun` line. Add the members of `OptionalBackendMembers`
   that apply to your cameras; callers look each one up with `getattr`.
2. Return camera handles satisfying `CameraHandleProtocol` from `open()`, and
   result objects satisfying `GrabResultProtocol` from `retrieve()`.
3. Add the name to `KNOWN_BACKENDS` and a branch to `load_backend()`. The
   module must import no SDK at import time if other backends' hosts are to
   keep working: load the SDK in the backend's constructor, and raise an
   `ImportError` subclass that says what to install when it is absent.
4. Exercise the retirement and simulated-backend paths headlessly first (the
   offline suite covers both with PyQt5 but no hardware and no SDK), then run
   real cameras through the GUI. `uv run probe_network.py` first confirms every
   camera's path. Check that the `cycle=` figure in the grab threads' log
   equals your frame period.
5. `gui_app/backends/sim.py` is the worked example: it implements this whole
   contract with no SDK, so a question this document leaves open can be
   answered by reading what the simulated rig does.

TRIGGER SEMANTICS AND THE STOP PROTOCOL
The application never asks a camera to stop producing frames. `set_freerun`
means "deliver at `fps` with no trigger source" and is the preview; recording
uses `set_triggered`, which means "deliver ONE frame per external trigger and
NOTHING while the triggers are stopped". That distinction is the whole stop
path: `CameraManager.stop_acquisition` tells the trigger board to stop, sets
each grab thread's `_triggers_stopped` flag and then waits. It never calls
`GrabThread.stop()` first. The grab loop leaves only when `retrieve()` raises
`TimeoutException` with that flag set, so:

  `retrieve()` MUST time out once the trigger source stops. A backend that
  keeps synthesising frames in triggered mode can never be stopped: the
  thread runs until the escalation path stops it, which the operator is
  told is a board that ignored its stop command.

A backend whose trigger source is not a wire needs an out-of-band hook for
the trigger controller to drive, because nothing in this contract mentions
triggers. The simulated backend's hook is `sim_board.SimBoard`: one virtual
clock, started and stopped by `SimSerial` (what `TeensyController` opens for
the `sim` port), read by every simulated camera.

The hard part is rarely the API. It is the guarantees:
  - a per-frame **trigger ordinal**, numbered from 1 at the first trigger
    after the stream is armed (`GrabResultProtocol.BlockID`). Without it,
    cross-camera alignment has nothing to align on.
  - a **device clock in nanoseconds** that keeps running across a stream
    restart (`GrabResultProtocol.TimeStamp`).
  - a **buffer pool** deep enough to absorb jitter, and a way to observe when it
    is exhausted (Basler: MaxNumBuffer, Statistic_Buffer_Underrun_Count).
  - **zero-copy access** to the pixel data. If your SDK only offers a copying
    accessor, measure it before assuming it is affordable.
"""
from __future__ import annotations

import importlib
from typing import Protocol, runtime_checkable

#: Every backend name `load_backend` accepts, in the order the error message
#: lists them. Profile validation checks `camera_backend` against this tuple,
#: so it must stay importable without any vendor SDK: nothing in this module
#: imports one at load time.
KNOWN_BACKENDS = ("basler", "sim", "flir", "flir_sim")

#: Keys every backend's `stream_stats()` reports under the same name when the
#: camera has the counter behind it, so session metadata compares across
#: vendors. See `CameraBackend.stream_stats`.
CANONICAL_STREAM_STATS = ("buffers_total", "buffers_failed", "buffers_underrun",
                          "resend_requests")


@runtime_checkable
class GrabResultProtocol(Protocol):
    """What `CameraBackend.retrieve()` must hand back.

    Attribute access here happens on the 10 ms hot path, so keep implementations
    cheap: no allocation, no copying, no locking.
    """

    def GrabSucceeded(self) -> bool:
        """False if this result is an error rather than an image."""

    @property
    def ErrorCode(self) -> int: ...

    @property
    def ErrorDescription(self) -> str: ...

    @property
    def BlockID(self) -> int:
        """The trigger ordinal: the same value on every camera for a given
        hardware trigger. Cross-camera alignment is built on it.

        Numbering. The first frame the camera acquires after each
        `StartGrabbing` reports 1, and every frame it acquires after that
        adds one. A frame lost in transmission still consumed its ID, so a
        transmission loss is a gap. A trigger the camera ignores consumes no
        ID and leaves no gap; `frame_sync.check_block_id_rate` finds that case
        from `TimeStamp`.

        The backend normalises its SDK's counter to this numbering before the
        value reaches this attribute, and logs which convention it found.
        Spinnaker's FrameID, for one, starts at 0. The rest of the pipeline
        takes the numbering as given: the grab loop refuses an ID at or below
        0, `stim_trace` computes t = (id - 1) / fps, and alignment reads ID 1
        as the first trigger. A 0-based counter passed through unchanged costs
        every camera its first frame, and every later frame then carries the
        ID of the trigger before it, so stimulation labels are one period
        early and nothing in the log says so.

        A 16-bit counter cycles 1..65535 and skips 0, the GigE Vision
        convention that `frame_sync.BLOCKID_WRAP` names; the software unwrap
        handles that cycle. A counter with any other period is unwrapped by
        the backend into a value that does not wrap.

        Reading it MAY RAISE, and the recording path does not shield it: the
        grab loop counts the exception as a frame error and retires the camera
        after ten in a row. A placeholder ordinal would starve every other
        camera through the coordinator and corrupt post-hoc alignment with
        nothing in the log."""

    @property
    def TimeStamp(self) -> int:
        """Device-clock timestamp in nanoseconds.

        It must be a free-running camera clock, not a host clock: it is used
        to re-derive the trigger ordinal after a stream restart, when BlockID
        restarts. It therefore has to stay MONOTONIC across
        `StopGrabbing`/`StartGrabbing`.

        The unit is nanoseconds whatever the camera counts in. A backend whose
        camera ticks at another rate converts the value, or refuses at open.
        The delivery-lag signal, the stall resync and
        `frame_sync.check_block_id_rate` all read it as nanoseconds, and a
        wrong unit shows up as every camera off by the same factor. May raise
        on the same terms as `BlockID`."""

    @property
    def PaddingX(self) -> int:
        """Row padding in bytes. MUST be 0, or the (H, W) reshape of the raw
        buffer shears the image. The grab loop refuses to record if it is not."""

    @property
    def PaddingY(self) -> int: ...

    def GetArrayZeroCopy(self):
        """Context manager yielding a (H, W) uint8 view over the driver buffer
        WITHOUT copying.

        The view must be C-CONTIGUOUS and (height, width) of uint8: it is
        handed to `os.write(fd, img)` and assigned into `buf[:H, :]` of an
        NV12 buffer, both of which take the memory as it lies. A padded or
        non-contiguous view would write sheared rows with no error, which is
        why `PaddingX`/`PaddingY` are checked before the view is taken.

        The view must remain valid until the context exits, and the caller
        guarantees it does not outlive `Release()`. If your SDK cannot do this,
        say so in the backend's own error message rather than substituting a
        copy."""

    def Release(self) -> None:
        """Return the buffer to the driver pool. Called once per result."""


@runtime_checkable
class CameraHandleProtocol(Protocol):
    """What `CameraBackend.open()` must hand back.

    The handle is otherwise opaque (it is only ever passed back into the
    backend's own methods), EXCEPT for these three, which `grab_thread` calls
    on it directly. Same reasoning as `GrabResultProtocol`: the grab loop stays
    on the native object. `CameraBackend` also exposes them, for callers
    outside the grab loop; both spellings must work.
    """

    def StartGrabbing(self, strategy) -> None:
        """Arm the stream. `strategy` is the backend's `GRAB_STRATEGY`.

        Every call restarts the block-ID counter at 1 (see
        `GrabResultProtocol.BlockID`). After a stall the grab loop re-arms,
        and `GrabThread._resync_offset` recovers the true ordinal from
        `TimeStamp`, which is why that clock must keep running across the
        restart. The resync would also land on the right ordinal with a
        counter that kept counting, but the arm that starts a recording would
        not: each camera would begin from whatever its preview left, and
        kick-out would pair different triggers."""

    def StopGrabbing(self) -> None: ...

    def IsGrabbing(self) -> bool:
        """The grab loop's `while` condition. Going False just ends the loop with
        no error, which is why `run()` has a `finally` catch-all that retires."""


class CameraBackend(Protocol):
    """The cold path. One instance per application, stateless w.r.t. cameras.

    Members a backend may leave out are listed in `OptionalBackendMembers`.
    """

    #: The registry name this backend was loaded by, one of `KNOWN_BACKENDS`.
    #: `CameraManager` compares it with the profile's `camera_backend` to
    #: decide whether a profile switch needs a different backend, so two
    #: backends that behave differently must not share a name.
    name: str

    #: The exception `retrieve()` raises on timeout, and on nothing else. The
    #: grab loop catches this class specifically: a timeout is normal
    #: (triggers stopped) and must be distinguishable from a real failure. An
    #: SDK that reports a timeout as a generic error code needs a class of the
    #: backend's own, raised only for that code.
    TimeoutException: type

    #: The argument `StartGrabbing` takes. Must deliver frames OLDEST-FIRST: that
    #: is what makes a grab loop too slow for the trigger rate show up as
    #: increasingly stale frames (visible in `delivery_lag_s`) rather than as
    #: drops nobody sees. `grab_thread` re-exports this and passes it to the
    #: handle.
    GRAB_STRATEGY: object

    def enumerate_devices(self) -> list:
        """All attached cameras, in a STABLE order (sort by serial number).

        Order defines camera names (`cam1`...`camN`), which are baked into the
        calibration extrinsics, so an unstable order mislabels data with no
        error. The device objects are opaque apart from `GetSerialNumber()`,
        which must return a `str`: `camera_manager` names it in its failure
        messages and compares it with the profile's `camera_serials` entries
        as strings, so a number here would match nothing and refuse to start.
        The caller does NOT sort: the stable order above is this backend's
        obligation, and camera names are positional over the order returned
        here (`cam{i+1}`), not derived from the serial."""

    def open(self, device, pfs_path: str, max_num_buffer: int,
             camera_spec=None):
        """Open and configure one camera, returning a `CameraHandleProtocol`.

        Raise on any problem; the caller refuses to start a partial set rather
        than shifting camera names.

        `pfs_path` is the profile's `pfs_path`. The parameter must exist on
        every backend, because the caller has one code path and passes the
        profile's value whatever backend is loaded. A backend with no such
        concept ignores an empty value. One whose settings come from
        somewhere else refuses a non-empty value, naming that source (FLIR:
        the profile's camera: block), because a camera configured from two
        places drifts between them. `RigProfile.validate` refuses that
        pairing first.

        `max_num_buffer` is the driver-side pool depth, the profile's
        `max_num_buffer`, and must be honoured: the capacity preflight budgets
        RAM against it, and real-time kick-out needs at least `kick_max_lag`
        buffers to hold a camera's backlog.

        `camera_spec` is the profile's `camera:` block as parsed by
        `session_config`, or None when the profile has none. A backend whose
        settings come from somewhere else refuses a non-None value, naming
        that source (Basler: the .pfs). The caller passes the keyword only
        when the profile has a block, so a backend written before the block
        existed keeps working unchanged.

        A backend MAY also take the keyword-only arguments `frame_size` (the
        profile's `(frame_width, frame_height)`) and `frame_rate` (the
        profile's `frame_rate`). The caller passes each only when it is set
        and the backend's `open` names it (or takes `**kwargs`), read with
        `inspect.signature`. A backend that takes them programs the ROI from
        the profile and refuses at open a rate its camera cannot record,
        raising its `RefusalException`. One that does not (Basler: the .pfs
        holds the ROI) is called as before, and the caller then refuses a
        camera whose size differs from the profile."""

    def describe(self, cam) -> dict:
        """`{"width", "height", "pixel_format", "serial"}` read back FROM THE
        CAMERA, not from config. The caller aborts unless every camera agrees
        with the profile, because a mismatched pixel format destroys the
        recording.

        `pixel_format` comes from a one-word vocabulary: **"Mono8"** is the
        only value the application accepts, compared literally, and anything
        else refuses the start. The capture path is 8-bit throughout, and a
        wider format raises nothing anywhere: a Mono12 frame is uint16 and the
        NV12 copy truncates it mod 256, yielding a full-length, aligned
        recording whose images are noise. `width`/`height` are ints and
        `serial` a str."""

    def set_freerun(self, cam, fps: float) -> None:
        """Untriggered preview mode at `fps` (the app uses 30).

        Frames must arrive at that rate with NO trigger source running: this
        is the preview, and it is the half of the pair that makes the
        recording half meaningful."""

    def set_triggered(self, cam, rate_limit: float,
                      announce: bool = False) -> None:
        """Hardware-trigger mode: deliver one frame per external trigger and
        NOTHING while the triggers are stopped (see "Trigger semantics and the
        stop protocol" above; this is what lets a recording end).

        `rate_limit` is the profile's `trigger_rate_limit`, Basler's
        AcquisitionFrameRate limiter: the minimum interval becomes
        `exposure + 1/rate_limit`, which is what caps usable exposure, and
        `rate_limit <= 0` disables it (see `basler.set_triggered` for why that
        is a trap). A backend whose cameras have no such limiter ignores the
        value and says so once, on the `announce` call.

        Whatever the SDK's trigger settings, the camera must accept a trigger
        that arrives while the previous frame is still being read out (FLIR:
        TriggerOverlap=ReadOut). A camera that disregards it acquires no frame
        and consumes no block ID, so its recording has no gap and drifts one
        trigger per miss; only the block-ID rate check catches it.

        `announce` is passed True for camera 0 only, so a per-rig log line is
        printed once rather than N times."""

    def get_exposure_gain(self, cam) -> tuple:
        """`(exposure_us, gain)` as currently set, or None for a control the
        camera does not have. Read once at open to capture the baseline (the
        .pfs on Basler), so a calibration-only exposure can be restored
        exactly afterwards instead of reconstructed. The gain is in the unit
        of the camera's own gain node (see `gain_unit`)."""

    def set_exposure_gain(self, cam, exposure_us=None, gain_db=None,
                          gain_unit=None) -> tuple:
        """Apply exposure/gain; return what was actually set, for logging.

        `None` means leave that control alone. The CEILING is the caller's job
        (`camera_manager.apply_exposure_gain` clamps), because exceeding it
        raises nothing: the camera ignores the triggers it is still busy for.

        `gain_unit` names the unit the gain value is in:
          - None: the node's own unit. This is the baseline restore, which
            writes back what `get_exposure_gain` read, so it is exact on any
            camera.
          - "dB": a profile value such as `calibration_gain_db`.
          - "raw": a sensor step count.
        A backend refuses (ValueError) a unit its camera's gain node does not
        take, before writing the gain, because a dB value written as raw
        steps records at a wrong gain that nothing in the log reveals. The
        exposure is applied before the unit is checked, so a refused gain
        never leaves the exposure unset. Any other value of `gain_unit` is a
        ValueError before anything is written."""

    def enable_extended_block_ids(self, i: int, cam) -> bool:
        """Try to negotiate 64-bit block IDs; return whether it took.

        An optimisation, not a requirement: `alignment._unwrap_blockids` and
        `FrameSyncCoordinator._unwrap` handle a 16-bit wrap in software. `i` is
        the camera index, for log lines only. Return True for a transport
        whose IDs are 64-bit already (USB3 Vision), and False when the
        concept does not apply."""

    def select_gige_driver(self, i: int, cam, which: str = "socket") -> None:
        """Apply the profile's `gige_driver` setting.

        `which` is in pylon's vocabulary ("socket"/"filter"/"auto"). A
        backend whose SDK names its GigE receive drivers differently takes
        the choice from its own configuration and ignores `which`. A no-op
        for non-GigE transports. `i` is the camera index, for logging."""

    def start_grabbing(self, cam) -> None:
        """Cold-path spelling of `handle.StartGrabbing(GRAB_STRATEGY)`. The grab
        loop uses the handle directly; this exists for other callers."""

    def stop_grabbing(self, cam) -> None: ...
    def is_grabbing(self, cam) -> bool: ...

    def retrieve(self, cam, timeout_ms: int):
        """Block for the next frame. Returns a `GrabResultProtocol`.
        Raises `self.TimeoutException` if none arrives in time."""

    def close(self, cam) -> None: ...

    def stream_stats(self, cam) -> dict:
        """Per-stream counters for the log. Include whatever distinguishes
        *host* starvation from *network* loss, since that is the distinction
        every capture problem eventually reduces to. The grab loop reads them
        before `StopGrabbing`, because some SDKs reset them there.

        Native names are backend-specific. Alongside them, report the
        `CANONICAL_STREAM_STATS` keys, each only when the camera has the
        counter behind it, so session metadata compares across vendors:
          buffers_total     buffers the stream delivered or gave up on
          buffers_failed    buffers given up on in transmission
          buffers_underrun  frames lost because the pool had no free buffer
                            (the HOST could not keep up)
          resend_requests   packet resend requests (GigE)

        ONE key is reserved: **"error"**. Set it (to a message) when the
        counters cannot be read, and set nothing else. The grab loop branches
        on it and prints "stream stats unavailable" instead of a dict of
        counters that are not there. Never raise: the caller reads these while
        finalising a recording, and a raise there is caught and turned into
        the same `{"error": ...}`, which is a worse message than the backend's
        own."""


class OptionalBackendMembers(Protocol):
    """Members a backend MAY provide. Every caller looks each one up with
    `getattr`, so a backend without one loses only that feature. A backend
    that provides one keeps the signature and meaning stated here, because
    the callers are written against this class. `OPTIONAL_MEMBERS` lists the
    same names for tests that walk them.
    """

    #: Camera-side transport settings `stream_stats` reports that stay
    #: readable after `StopGrabbing`. `CameraManager` keeps only these keys
    #: from a read taken after the stream stopped, because the stream counters
    #: reset there and would record zeros.
    TRANSPORT_NODES: tuple

    #: The exception class a backend raises when a camera cannot record at
    #: the frame rate asked: from `open` for the profile's frame_rate, or
    #: from `exposure_ceiling_us` for an acquisition's. `CameraManager`
    #: refuses the start on it, because a recording made anyway skips
    #: triggers or drops frames. Any other exception from
    #: `exposure_ceiling_us` leaves the trigger period as the bound, with a
    #: warning.
    RefusalException: type

    #: What bounds the exposure on this backend, in the words of the
    #: `[camN] exposure=... (ceiling ... at N fps, <this>)` log line.
    #: Without it the line names the AcquisitionFrameRate limiter, which is
    #: what `rate_limit` sets on a Basler camera.
    CEILING_BASIS: str

    #: The `ceiling_hint` and `timestamp_hint` clauses of the block-ID rate
    #: warning (`frame_sync.check_block_id_rate`) for this backend. The
    #: camera manager merges them with the trigger source's name and passes
    #: the dict to the kick-out router as `rate_hints`. Without it the
    #: warning gives the Basler advice.
    BLOCK_RATE_HINTS: dict

    def set_bandwidth_reserve(self, cam, percent=None,
                              accumulation=None) -> dict:
        """Write the GigE bandwidth reserve (Basler GevSCBWR/GevSCBWRA) and
        return what the camera read back. None leaves a setting alone. Called
        at open when the profile sets `gev_bandwidth_reserve_*`. A camera
        without the nodes raises, because a profile asking for a knob the rig
        does not have is a configuration error."""

    def set_transmission_delay(self, cam, ticks: int) -> int:
        """Write the GigE frame transmission delay (Basler GevSCFTD) in ticks
        of the camera's timestamp clock, and return the value read back. A
        camera without the node raises."""

    def thermals(self, cam) -> dict:
        """The camera's own temperature readings, polled while acquiring.

        `CameraManager.thermals()` calls it per camera and substitutes
        `{"error": ...}` if it raises. The main window's thermal watch reads
        `temp_c` (float, degrees C), `temp_status` (the camera's own verdict;
        anything but "Ok" or empty is over temperature), `temp_critical_c`
        and `temp_shutdown_c`. `temp_max_c` is recorded in session metadata;
        a backend that tracks it itself (not a device high-water mark) says so
        in its own docstring. Return only the keys the camera has rather than
        raising."""

    def gain_unit(self, cam):
        """"dB", "raw" or None (no gain control): the unit of the camera's
        gain node, for log lines that print a gain value."""

    def exposure_ceiling_us(self, cam, fps: float, rate_limit: float) -> float:
        """The longest exposure, in microseconds, at which this camera still
        acquires every trigger at `fps`. `rate_limit` is the profile's
        `trigger_rate_limit`; a backend with no limiter ignores it.

        This is the raw ceiling. `camera_manager.apply_exposure_gain` keeps
        its own 10% margin (it multiplies by 0.9) and logs `CLAMPED` when it
        lowers an exposure. The value may be 0 or negative when no exposure
        fits, and is returned as is: the caller reports that case rather than
        clamping to a non-positive exposure. Without this member the caller
        uses the Basler limiter formula."""

    def set_packet_size(self, cam, n: int) -> int:
        """Write the GigE stream packet size in bytes and return the value the
        camera read back. For `probe_network.py --sweep`. A camera without
        the node raises."""

    def device_address(self, device):
        """The IPv4 address of an `enumerate_devices()` entry as dotted text,
        or None for a device that has none (USB3). Never raises."""

    def acquisition_warnings(self, cam, frames_acquired: int) -> list:
        """Problems with the acquisition that just ended that only the camera
        can witness, as sentences for WARNINGS.txt; [] when there are none.

        Called once per recording after the stream stops, with the number of
        frames this camera delivered in trigger mode. Example: a trigger
        counter on the camera's input line that saw more edges than frames
        were delivered counts the triggers the camera ignored. Never raises."""

    def sdk_report(self) -> str:
        """One line for the launch preflight: the SDK this backend drives, its
        version and where it was loaded from. Callable on the class (a
        staticmethod or classmethod), because the preflight asks before any
        camera is opened. Never raises. The module-level `sdk_report(name)`
        is the caller."""


#: The names `OptionalBackendMembers` declares, in declaration order.
OPTIONAL_MEMBERS = ("TRANSPORT_NODES", "RefusalException", "CEILING_BASIS",
                    "BLOCK_RATE_HINTS", "set_bandwidth_reserve",
                    "set_transmission_delay", "thermals", "gain_unit",
                    "exposure_ceiling_us", "set_packet_size", "device_address",
                    "acquisition_warnings", "sdk_report")


def _unknown_backend(name) -> ValueError:
    known = ", ".join(repr(n) for n in KNOWN_BACKENDS)
    return ValueError(
        f"unknown camera backend {name!r}. Known backends: {known}. Set the "
        f"profile's camera_backend field to one of them. To add one, implement "
        f"CameraBackend in gui_app/backends/ and register it in KNOWN_BACKENDS "
        f"and load_backend(); this module's docstring lists the guarantees "
        f"required.")


#: What each backend module is for, when a copy of the code base lacks it.
_MODULE_HINTS = {
    "gui_app.backends.flir": (
        "It drives FLIR/Teledyne cameras through Teledyne's Spinnaker SDK 4.x "
        "(the full Windows installer, which includes the C libraries). Update "
        "this copy of Panopticon to one that includes gui_app/backends/flir.py "
        "and install the Spinnaker SDK."),
    "gui_app.backends.fake_spinc": (
        "It simulates the Spinnaker SDK, so the flir_sim backend needs no "
        "camera and no SDK. Update this copy of Panopticon to one that "
        "includes gui_app/backends/fake_spinc.py."),
}


def _import_backend_attr(backend: str, module: str, attr: str):
    """`module.attr`, imported now. A missing MODULE becomes an ImportError
    that names the backend, the file and what to install.

    Only the named module being absent is rewritten. A ModuleNotFoundError for
    anything the module itself imports propagates unchanged, because its own
    message names the real missing piece; so does every other ImportError,
    including an SDK-missing error the backend raises with its own hint.
    """
    try:
        mod = importlib.import_module(module)
    except ModuleNotFoundError as e:
        if e.name != module:
            raise
        known = ", ".join(repr(n) for n in KNOWN_BACKENDS)
        raise ImportError(
            f"The {backend!r} camera backend is not available: "
            f"{module.replace('.', '/')}.py is missing from this copy of "
            f"Panopticon. {_MODULE_HINTS.get(module, '')} Or set the "
            f"profile's camera_backend field to another backend "
            f"(known: {known}).") from e
    return getattr(mod, attr)


def backend_options(name: str, camera_spec=None) -> dict:
    """The constructor keywords backend `name` takes from the profile's
    parsed `camera:` block, or {} when it takes none.

    The flir backend takes `sdk_dir` from `camera.flir.sdk_dir`: the folder
    the Spinnaker library is loaded from. The library loads once per
    process, when the first FLIR backend is built, so the folder has to
    reach that first construction; a later one cannot move the library.
    `camera_spec` is duck-typed, because this module never imports the
    profile code.
    """
    if name == "flir" and camera_spec is not None:
        sdk_dir = getattr(getattr(camera_spec, "flir", None), "sdk_dir", None)
        if sdk_dir:
            return {"sdk_dir": str(sdk_dir)}
    return {}


def load_backend(name: str = "basler", camera_spec=None) -> CameraBackend:
    """Return a backend by name.

    The import is lazy, so a missing SDK breaks only the backend that needs
    it, never the application or the other backends. An unknown name raises
    ValueError listing `KNOWN_BACKENDS`. A missing SDK or backend module
    raises an ImportError whose message says what to install.

    `camera_spec` is the profile's parsed `camera:` block, or None. A backend
    that reads a setting from it before any camera exists gets it here (see
    `backend_options`); every other backend ignores it.
    """
    if name == "basler":
        from gui_app.backends.basler import BaslerBackend
        return BaslerBackend()
    if name == "sim":
        # The simulated rig: a virtual trigger clock and cameras with
        # injectable faults, so the capture path can be exercised with no
        # hardware and no SDK (profiles/sim.yaml selects it).
        from gui_app.backends.sim import SimBackend
        return SimBackend()
    if name == "flir":
        # Importing the module loads no DLL; the constructor loads the
        # Spinnaker C library and raises an ImportError subclass that names
        # the searched paths when it is absent.
        flir_cls = _import_backend_attr(name, "gui_app.backends.flir",
                                        "FlirBackend")
        return flir_cls(**backend_options(name, camera_spec))
    if name == "flir_sim":
        # The FLIR backend over a simulated Spinnaker library, paced by the
        # same virtual trigger clock as the sim backend (pairs with
        # serial_port: sim).
        flir_cls = _import_backend_attr(name, "gui_app.backends.flir",
                                        "FlirBackend")
        fake_cls = _import_backend_attr(name, "gui_app.backends.fake_spinc",
                                        "FakeSpinC")
        backend = flir_cls(api=fake_cls())
        # The instance answers to the name it was loaded by: CameraManager
        # reloads only when the name changes, so a flir_sim instance calling
        # itself "flir" would keep driving the fake after a switch to a real
        # FLIR profile.
        backend.name = name
        return backend
    raise _unknown_backend(name)


def sdk_report(name: str, camera_spec=None) -> str:
    """One line for the launch preflight: the camera SDK that backend `name`
    uses, its version and location, or why it cannot be loaded.

    Importing this package imports no SDK; this call imports the backend's
    module, and with it the SDK, only when asked. Never raises: the preflight
    prints the line whatever it says.

    `camera_spec` is the profile's parsed `camera:` block, or None. When it
    names where the SDK is loaded from (`backend_options`), the backend is
    built with that first, so the report describes the library the profile
    asks for. Without it, a report that runs before the cameras are opened
    loads the SDK from its default location, and the open then refuses a
    profile that names another folder.
    """
    if name not in KNOWN_BACKENDS:
        return str(_unknown_backend(name))
    try:
        if name == "basler":
            from gui_app.backends.basler import BaslerBackend as cls
        elif name == "sim":
            from gui_app.backends.sim import SimBackend as cls
        else:
            cls = _import_backend_attr(name, "gui_app.backends.flir",
                                       "FlirBackend")
            if name == "flir_sim":
                _import_backend_attr(name, "gui_app.backends.fake_spinc",
                                     "FakeSpinC")
                return ("flir_sim: no camera SDK (simulated Spinnaker "
                        "library, gui_app/backends/fake_spinc.py)")
        options = backend_options(name, camera_spec)
        if options:
            # Built once for its side effect: the SDK loads from the
            # profile's folder. A failure is the report, because the class's
            # own report would then load the SDK from its default location.
            cls(**options)
    except ImportError as e:
        return f"{name}: cannot load ({e})"
    except Exception as e:
        return f"{name}: cannot load ({type(e).__name__}: {e})"
    fn = getattr(cls, "sdk_report", None)
    if fn is None:
        return f"{name}: loaded; the backend reports no SDK version"
    try:
        return f"{name}: {fn()}"
    except Exception as e:
        return f"{name}: SDK report failed ({type(e).__name__}: {e})"
