"""Camera backends — the vendor-specific layer, and the contract it must meet.

WHY THIS EXISTS
Everything else in `gui_app/` is vendor-neutral. Historically the Basler/pypylon
calls were spread through `camera_manager.py` and `grab_thread.py`, which made
"what would it take to support another camera?" unanswerable without reading
both files. This package is the answer: implement `CameraBackend`, and the rest
of the application does not change.

WHAT IS AND IS NOT ABSTRACTED
The **cold path** (enumerate, open, configure, trigger mode, teardown,
statistics) goes through `CameraBackend` methods. It runs a handful of times per
session, so an extra layer costs nothing and buys clarity.

The **hot path** — retrieving a frame, 100 times a second per camera — is
deliberately NOT wrapped in per-field accessor methods. The backend's
`retrieve()` returns a *native* grab-result object and this module documents the
attributes it must expose (see `GrabResultProtocol`). For the same reason the
grab loop drives the camera handle itself (`StartGrabbing`/`StopGrabbing`/
`IsGrabbing`) rather than the equivalent `CameraBackend` methods — see
`CameraHandleProtocol`. A new backend supplies a thin adapter object rather than
paying an indirection per attribute.

To be clear about the reasoning, because it would be easy to assume otherwise:
Python call overhead is NOT why. A call is ~60 ns, so even seven per frame
across nine cameras at 100 fps is 6,300 calls/s ≈ 0.4 ms of CPU per second —
0.04% of one core, irrelevant. The real reason is that the hot path has
invariants a wrapper tends to quietly break: the frame view must not outlive
`Release()`, and it must not be copied on the way through (a hidden copy here is
exactly the 2.3 MB GIL-held memcpy that cost this project years of frame loss —
see docs/PERF_EXPERIMENTS.md E3). A documented duck-type contract keeps those
invariants visible at the point they matter.

WRITING A NEW BACKEND
1. Implement `CameraBackend` for your SDK — ALL of it, including the members
   below the `set_freerun` line, which are as load-bearing as the rest, plus
   the optional members listed under `CameraBackend` if they apply.
2. Return camera handles satisfying `CameraHandleProtocol` from `open()`, and
   result objects satisfying `GrabResultProtocol` from `retrieve()`.
3. Register it in `load_backend()`.
4. Run `test_grab_failure.py` and `test_sim_backend.py` (both need PyQt5 but
   no hardware and no SDK) and then `probe_lag.py` against real cameras, and
   check the `cycle=` figure in the grab threads' log equals your frame
   period.
5. `gui_app/backends/sim.py` is the worked example: it implements this whole
   contract with no SDK, so a question this document leaves open can be
   answered by reading what the simulated rig does.

TRIGGER SEMANTICS AND THE STOP PROTOCOL
The application never asks a camera to stop producing frames. `set_freerun`
means "deliver at `fps` with no trigger source" and is the preview; recording
uses `set_triggered`, which means "deliver ONE frame per external trigger and
NOTHING while the triggers are stopped". That distinction is the whole stop
path: `CameraManager.stop_acquisition` tells the trigger board to stop, sets
each grab thread's `_triggers_stopped` flag and then waits — it never calls
`GrabThread.stop()` first. The grab loop leaves only when `retrieve()` raises
`TimeoutException` with that flag set, so:

  **`retrieve()` MUST time out once the trigger source stops.** A backend
  that keeps synthesising frames in triggered mode can never be stopped: the
  thread runs until the escalation path stops it outright, which is reported
  to the operator as a board that ignored its stop command.

A backend whose trigger source is not a wire needs an out-of-band hook for
the trigger controller to drive, because nothing in this contract mentions
triggers. The simulated backend's hook is `sim_board.SimBoard`: one virtual
clock, started and stopped by `SimSerial` (what `TeensyController` opens for
the `sim` port), read by every simulated camera.

The hard part is rarely the API. It is the guarantees:
  - a per-frame **monotonic trigger ordinal** that survives a stream restart
    (Basler: GigE Vision BlockID). Without it, cross-camera alignment has
    nothing to align on and every downstream stage silently mis-associates.
  - a **buffer pool** deep enough to absorb jitter, and a way to observe when it
    is exhausted (Basler: MaxNumBuffer, Statistic_Buffer_Underrun_Count).
  - **zero-copy access** to the pixel data. If your SDK only offers a copying
    accessor, measure it before assuming it is affordable.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class GrabResultProtocol(Protocol):
    """What `CameraBackend.retrieve()` must hand back.

    Attribute access here happens on the 10 ms hot path, so keep implementations
    cheap — no allocation, no copying, no locking.
    """

    def GrabSucceeded(self) -> bool:
        """False if this result is an error rather than an image."""

    @property
    def ErrorCode(self) -> int: ...

    @property
    def ErrorDescription(self) -> str: ...

    @property
    def BlockID(self) -> int:
        """Monotonic trigger ordinal, the SAME value on every camera for a given
        hardware trigger. This is the entire basis of cross-camera alignment.
        May wrap (Basler wraps at 65535 unless 64-bit IDs are negotiated);
        `alignment._unwrap_blockids` handles that. Values are >= 1: GVSP
        reserves 0, and the grab loop refuses anything at or below it rather
        than let a placeholder be read as a wrap.

        Reading it MAY RAISE, and the recording path deliberately does not
        shield it: the grab loop counts the exception as a frame error and
        retires the camera after ten in a row, because substituting a
        placeholder ordinal would starve every other camera through the
        coordinator and poison post-hoc alignment silently."""

    @property
    def TimeStamp(self) -> int:
        """Device-clock timestamp in nanoseconds. Must be a free-running camera
        clock, not a host clock — it is used to re-derive the trigger ordinal
        after a stream restart, when BlockID resets. It therefore has to stay
        MONOTONIC across `StopGrabbing`/`StartGrabbing`, and the unit really is
        nanoseconds: `frame_sync.check_block_id_rate` compares block IDs
        against it and says so when the two disagree. May raise on the same
        terms as `BlockID`."""

    @property
    def PaddingX(self) -> int:
        """Row padding in bytes. MUST be 0, or the (H, W) reshape of the raw
        buffer shears the image. The grab loop refuses to record if it is not."""

    @property
    def PaddingY(self) -> int: ...

    def GetArrayZeroCopy(self):
        """Context manager yielding a (H, W) uint8 view over the driver buffer
        WITHOUT copying.

        The view must be C-CONTIGUOUS and exactly (height, width) of uint8:
        it is handed to `os.write(fd, img)` and assigned into `buf[:H, :]` of
        an NV12 buffer, both of which take the memory as it lies. A padded or
        non-contiguous view would write sheared rows with no error, which is
        why `PaddingX`/`PaddingY` are checked before the view is taken.

        The view must remain valid until the context exits, and the caller
        guarantees it does not outlive `Release()`. If your SDK cannot do this,
        say so loudly in the backend rather than silently substituting a copy."""

    def Release(self) -> None:
        """Return the buffer to the driver pool. Called once per result."""


@runtime_checkable
class CameraHandleProtocol(Protocol):
    """What `CameraBackend.open()` must hand back.

    The handle is otherwise opaque — it is only ever passed back into the
    backend's own methods — EXCEPT for these three, which `grab_thread` calls on
    it directly. Same reasoning as `GrabResultProtocol`: the grab loop stays on
    the native object. `CameraBackend` also exposes them, for callers outside the
    grab loop; both spellings must work.
    """

    def StartGrabbing(self, strategy) -> None:
        """Arm the stream. `strategy` is the backend's `GRAB_STRATEGY`.

        Re-arming after a stall is expected to restart the block-ID counter;
        `GrabThread._resync_offset` recovers the true ordinal from `TimeStamp`,
        which is why that clock must keep running across the restart. A
        counter that does NOT restart is fine too — the resync arithmetic
        lands on the same ordinal either way."""

    def StopGrabbing(self) -> None: ...

    def IsGrabbing(self) -> bool:
        """The grab loop's `while` condition. Going False just ends the loop with
        no error, which is why `run()` has a `finally` catch-all that retires."""


class CameraBackend(Protocol):
    """The cold path. One instance per application, stateless w.r.t. cameras."""

    #: Human-readable, e.g. "basler".
    name: str

    #: The exception `retrieve()` raises on timeout. The grab loop catches this
    #: specifically — a timeout is normal (triggers stopped) and must be
    #: distinguishable from a real failure.
    TimeoutException: type

    #: The argument `StartGrabbing` takes. Must deliver frames OLDEST-FIRST: that
    #: is what makes a grab loop too slow for the trigger rate show up as
    #: increasingly stale frames (visible in `delivery_lag_s`) rather than as
    #: silent drops. `grab_thread` re-exports this and passes it to the handle.
    GRAB_STRATEGY: object

    def enumerate_devices(self) -> list:
        """All attached cameras, in a STABLE order (sort by serial number).

        Order defines camera names (`cam1`...`camN`), which are baked into the
        calibration extrinsics — so an unstable order silently mislabels data.
        The device objects are opaque apart from `GetSerialNumber()`, which
        must return a `str`: `camera_manager` names it in its failure messages
        and compares it with the profile's `camera_serials` entries as
        strings, so a number here would match nothing and refuse to start. The
        caller does NOT sort — the stable order above is this backend's
        obligation, and camera names are positional over the order returned
        here (`cam{i+1}`), not derived from the serial."""

    def open(self, device, pfs_path: str, max_num_buffer: int):
        """Open and configure one camera, returning a `CameraHandleProtocol`.

        Raise on any problem; the caller refuses to start a partial set rather
        than shifting camera names. `max_num_buffer` is the driver-side pool
        depth (`camera_manager.MAX_NUM_BUFFER`) and must be honoured — the
        capacity preflight budgets RAM against it. `pfs_path` must be ACCEPTED
        even by a backend with no such concept (ignore it): the caller has one
        code path and passes the profile's value whatever backend is loaded."""

    def describe(self, cam) -> dict:
        """`{"width", "height", "pixel_format", "serial"}` read back FROM THE
        CAMERA, not from config. The caller aborts unless every camera agrees
        with the profile — a mismatched pixel format is silently destructive.

        `pixel_format` comes from a one-word vocabulary: **"Mono8"** is the
        only value the application accepts, compared literally, and anything
        else refuses the start. The capture path is 8-bit throughout, and a
        wider format is not an error anywhere — a Mono12 frame is uint16 and
        the NV12 copy truncates it mod 256, yielding a full-length, perfectly
        aligned, visually shredded recording. `width`/`height` are ints and
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
        stop protocol" above — this is what lets a recording end).

        `rate_limit` sets the camera's internal frame
        rate; note the minimum interval becomes `exposure + 1/rate_limit`, which
        is what caps usable exposure. `rate_limit <= 0` means disable the limiter
        (see `basler.set_triggered` for why that is a trap).

        `announce` is passed True for camera 0 only, so a per-rig log line is
        printed once rather than N times."""

    def get_exposure_gain(self, cam) -> tuple:
        """`(exposure_us, gain_db)` as currently set, or `(None, None)` if the
        camera has no such controls. Read once at open to capture the .pfs
        baseline, so a calibration-only exposure can be restored EXACTLY
        afterwards instead of reconstructed."""

    def set_exposure_gain(self, cam, exposure_us=None, gain_db=None) -> tuple:
        """Apply exposure/gain; return what was actually set, for logging.

        `None` means leave that control alone. The CEILING is the caller's job
        (`camera_manager.apply_exposure_gain` clamps), because exceeding it does
        not error — the camera simply ignores triggers it is still busy for."""

    def enable_extended_block_ids(self, i: int, cam) -> bool:
        """Try to negotiate 64-bit block IDs; return whether it took.

        An optimisation, not a requirement: `alignment._unwrap_blockids` and
        `FrameSyncCoordinator._unwrap` handle a 16-bit wrap in software. `i` is
        the camera index, for log lines only. Return False if the concept does
        not apply to your transport."""

    def select_gige_driver(self, i: int, cam, which: str = "socket") -> None:
        """Apply the profile's `gige_driver` setting ("socket"/"filter"/"auto").

        A no-op for non-GigE transports. `i` is the camera index, for logging."""

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
        """Per-stream counters for the log. Keys are backend-specific; include
        whatever distinguishes *host* starvation from *network* loss, since that
        is the distinction every capture problem eventually reduces to.

        ONE key is reserved: **"error"**. Set it (to a message) when the
        counters cannot be read, and set nothing else — the grab loop branches
        on it and prints "stream stats unavailable" instead of a dict of
        counters that are not there. Never raise: the caller reads these while
        finalising a recording, and a raise there is caught and turned into
        the same `{"error": ...}`, which is a worse message than the backend's
        own.

        OPTIONAL MEMBERS (discovered with `getattr`, so a backend without them
        simply loses the feature):
          - `thermals(cam) -> dict` — the camera's own temperature, polled
            while acquiring. `CameraManager.thermals()` calls it per camera
            and substitutes `{"error": ...}` if it raises, and the main
            window's thermal watch reads `temp_c` (float, degrees C),
            `temp_status` (the camera's own verdict; anything but "Ok" or
            empty is over temperature), `temp_critical_c` and
            `temp_shutdown_c`. `temp_max_c` is recorded in session metadata.
            Return only the keys the camera has rather than raising.
          - `gain_unit(cam)` — "dB", "raw" or None, for a log line that has to
            name the unit a gain value is in.
        """


def load_backend(name: str = "basler") -> CameraBackend:
    """Return a backend by name. Import is lazy so a missing SDK only breaks the
    backend that needs it, not the whole application."""
    if name == "basler":
        from gui_app.backends.basler import BaslerBackend
        return BaslerBackend()
    if name == "sim":
        # The simulated rig: a virtual trigger clock and cameras with
        # injectable faults, so the capture path can be exercised with no
        # hardware and no SDK (profiles/sim.yaml selects it).
        from gui_app.backends.sim import SimBackend
        return SimBackend()
    raise ValueError(
        f"unknown camera backend {name!r}. Available: 'basler', 'sim'. "
        f"To add one, implement CameraBackend in gui_app/backends/ and register "
        f"it here — see this module's docstring for the guarantees required.")
