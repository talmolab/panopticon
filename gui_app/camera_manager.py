"""Manages the camera set — opening, closing, and switching between free-run and
trigger modes. The count comes from the profile (`n_cameras`, 6 on 3dpose) and is
enforced by `open_all(expect_cameras=...)`, not hardcoded here."""
import time
import numpy as np
from pathlib import Path
from PyQt5.QtCore import QObject, pyqtSignal
from gui_app.backends import load_backend
from gui_app.grab_thread import GrabThread

#: Bounds on stop_acquisition, each applied to its PHASE as one shared
#: deadline rather than to each thread in turn: the threads exit concurrently,
#: so a per-thread wait would make a nine-camera stop take nine times longer
#: than a one-camera stop in exactly the case the bound exists for (every
#: camera still receiving frames).
STOP_NORMAL_EXIT_S = 5.0       # loop leaves on its first timeout after the board stops
STOP_FORCED_EXIT_S = 10.0      # after gt.stop(): one retrieve, unless wedged natively
STOP_DRAIN_S = 100.0           # decoupled-encoder drain: 30 s sentinel + 60 s join
ABANDON_THREAD_S = 3.0         # abandon(): loop exit + encoder abort, all threads


class AcquisitionStopIncomplete(RuntimeError):
    """stop_acquisition() collected every camera's data but could not finish.

    A grab thread is still running after the stop bounds, so the cameras were
    NOT reconfigured (reconfiguring a handle under a live RetrieveResult is
    concurrent native access) and the thread list is kept for abandon(). The
    per-camera (frame_count, timestamps, block_ids) tuples are on `results`
    and on CameraManager.last_results: the caller must still save them, or
    blockids.npy / frametimes.npy are written for NO camera and the healthy
    cameras' streams cannot be aligned.
    """

    def __init__(self, message: str, results: list, stuck: list):
        super().__init__(message)
        self.results = results
        #: Zero-based indices of the cameras whose thread is still running.
        self.stuck = stuck


class CameraOpenError(RuntimeError):
    """Why open_all refused - RETURNED rather than raised.

    open_all is called from a Qt slot and from the headless probes, both of
    which branch on its result, so the reason travels as a value. Two
    properties make that safe:
      - it is FALSY, so `if not mgr.open_all(...)` still reads as failure;
      - it is an Exception, so a caller can tell a failure that has already
        been reported with a specific message from a plain "nothing was
        opened", and show ONE dialog instead of a specific one followed by a
        generic one that contradicts it.
    """

    def __bool__(self) -> bool:
        return False


class AcquisitionStartRefused(RuntimeError):
    """start_acquisition() could not put every camera into the requested state.

    Raised AFTER the cameras have been returned to free-run preview, so the
    caller only has to report it. A partial start must never record: camera
    names are positional, so a session missing one camera would attach every
    later camera's extrinsics to the wrong physical camera, and in kick mode a
    free-running camera's block IDs would force-drop every other camera's
    frames.
    """

#: Default driver-side buffer pool depth per camera, used only when a caller
#: passes no max_num_buffer of its own. The profile's `max_num_buffer` is
#: authoritative, and the capacity preflight must be given the SAME value it
#: passes to open_all: the pool costs n_cams x depth x 2.304 MB of RAM, so a
#: preflight computed from a different depth permits or refuses the wrong
#: recordings.
#:
#: Depth is not monotonically good and must not be changed without a rig A/B.
#: A deep pool absorbs GigE jitter, but it also hides a per-frame deficit for
#: minutes (nothing errors: the pool fills and every frame retrieved gets
#: staler), and too deep starves capture outright. See docs/PERF_EXPERIMENTS.md.
MAX_NUM_BUFFER = 1000


class CameraManager(QObject):
    error = pyqtSignal(str)

    def __init__(self, backend: str = "basler"):
        # The only vendor-specific object in this class. Everything below is
        # camera-agnostic orchestration; see gui_app/backends/__init__.py for
        # what a new backend has to provide.
        self._backend = load_backend(backend)
        super().__init__()
        self._cameras: list = []
        self._geometry = None      # (w, h) agreed by every camera
        #: (exposure_us, gain_db) per camera as loaded from the .pfs.
        self._baseline_exp_gain: list = []
        #: Why the last open_all refused, or None after one that succeeded.
        #: The same string the `error` signal carried, kept so a caller that
        #: reads the return value never has to also listen to the signal.
        self.last_open_error = None
        #: Problems found while finalising the last recording (retired cameras,
        #: block-ID truncation). Read by the GUI after stop_acquisition().
        self.last_warnings: list = []
        #: Per-camera GigE stream counters and transport settings from the
        #: last stop_acquisition(), index-aligned with the camera list. Held
        #: for the session metadata: they are what separates host starvation
        #: (Buffer_Underrun) from network loss (Failed_Buffer, resends).
        self.last_stream_stats: list = []
        #: Per-camera (frame_count, timestamps, block_ids) from the last
        #: stop_acquisition(), kept even when it raised so the caller can still
        #: write blockids.npy / frametimes.npy for the healthy cameras.
        self.last_results: list = []
        self._grab_threads: list[GrabThread] = []
        #: Grab threads that never exited and the camera handles they hold.
        #: Referenced for the life of the process on purpose: destroying a
        #: running QThread aborts the process, and letting a camera object be
        #: collected closes its handle under the native call the thread is
        #: wedged in. Nothing reads these lists; they exist to keep the
        #: objects alive.
        self._leaked_threads: list = []
        self._leaked_cameras: list = []
        self._router = None  # SyncEncodeRouter in real-time kick-out mode

    @property
    def num_cameras(self) -> int:
        return len(self._cameras)

    @property
    def geometry(self):
        """(width, height) every open camera agreed on, or None when none are
        open.

        Read-only on purpose: the value is what the CAMERAS report, read back
        after the .pfs was applied, and a caller that needs a different size
        fixes the .pfs rather than this number. It is also what the profile's
        frame_width/frame_height are checked against, because those two size
        the NV12 ring and the raw decode.
        """
        return self._geometry

    @property
    def latest_frames(self) -> list:
        return [gt.latest_frame for gt in self._grab_threads]

    @property
    def current_fps(self) -> list[float]:
        return [gt.current_fps for gt in self._grab_threads]

    @property
    def snapshots(self) -> list:
        return [gt.snapshot_frame for gt in self._grab_threads]

    @property
    def latest_full_frames(self) -> list:
        return [gt.latest_full_frame for gt in self._grab_threads]

    @property
    def delivery_lags(self) -> list[float]:
        """Per-camera seconds behind real time. See GrabThread.delivery_lag_s."""
        return [gt.delivery_lag_s for gt in self._grab_threads]

    @property
    def frame_counts(self) -> list[int]:
        return [gt.frame_count for gt in self._grab_threads]

    @property
    def frontier_lags(self) -> list:
        """Per-camera triggers behind the leading camera in kick mode, -1 for a
        retired camera; [] outside kick mode. Counted in block IDs, so it has
        none of the clock drift delivery_lags accumulates."""
        if self._router is None:
            return []
        try:
            return self._router.lag_frames()
        except Exception:
            return []

    def thermals(self) -> list:
        """Per-camera temperature readings, or [] if the backend has none.

        A cold-path GVCP register read: safe to call while grabbing, but do not
        put it on any per-frame path.
        """
        fn = getattr(self._backend, "thermals", None)
        if fn is None:
            return []
        out = []
        for cam in self._cameras:
            try:
                out.append(fn(cam))
            except Exception as e:
                out.append({"error": f"{type(e).__name__}: {e}"})
        return out

    def request_snapshots(self):
        """Ask every camera to stash its next full-resolution frame."""
        for gt in self._grab_threads:
            gt.request_snapshot()

    def set_keep_full(self, flag: bool):
        """Toggle full-resolution frame retention (for the coverage HUD)."""
        for gt in self._grab_threads:
            gt.set_keep_full(flag)

    def _open_failed(self, message: str) -> CameraOpenError:
        """Report an open failure once, and return it.

        RULE: every open_all failure path goes through here. The message is
        emitted on `error` for a caller wired to the signal, kept on
        last_open_error, and returned, so the reason exists in exactly one
        form no matter which of the three a caller reads.
        """
        self.last_open_error = message
        self.error.emit(message)
        return CameraOpenError(message)

    def open_all(self, pfs_path: str, gige_driver: str = "socket",
                 trigger_rate_limit: float = 165.0, expect_cameras: int = 0,
                 max_num_buffer: int = MAX_NUM_BUFFER,
                 only_serials=None, backend: str | None = None,
                 expect_geometry=None,
                 gev_bandwidth_reserve_pct=None,
                 gev_bandwidth_reserve_accum=None):
        """trigger_rate_limit: AcquisitionFrameRate to apply in trigger mode, or
        0 to disable the limiter altogether — see _set_trigger_mode.

        backend: camera backend NAME (see gui_app.backends.load_backend) to use
        from here on, or None to keep the one this manager was constructed
        with. A profile selects its vendor here; the grab threads receive the
        same backend instance, so nothing else in the application changes.

        only_serials: the profile's camera_serials. When given, cam{i+1} is
        entry i of THIS LIST rather than a position in the enumeration, so a
        device the profile does not list cannot shift a name; unlisted devices
        are ignored with a line on stdout, and a listed serial that did not
        enumerate refuses the open.

        expect_cameras: if nonzero, refuse to start unless exactly this many
        cameras are available to open (counted after only_serials filters).

        max_num_buffer: driver-side buffers per camera. Comes from the profile
        so a rig can trade pool depth against RAM; MAX_NUM_BUFFER is the default
        for callers that do not care. The capacity preflight must be given the
        SAME value or it will refuse (or permit) the wrong recordings.

        expect_geometry: the profile's (frame_width, frame_height), or None to
        accept whatever the cameras report. A camera whose ROI differs refuses
        the open, because those two numbers size the NV12 ring and the raw
        decode: in real-time mode the per-frame copy raises and the camera is
        retired, and in raw mode ffmpeg decodes the camera's bytes at the
        profile's size, which shears a full-length recording with no error
        anywhere. Refusing at open costs a dialog; not refusing costs the
        session.

        gev_bandwidth_reserve_pct / gev_bandwidth_reserve_accum: GevSCBWR and
        GevSCBWRA, written through the backend at open when not None. They
        hold bandwidth back for packet resends, which lowers the assigned
        bandwidth every camera gets, so they are opt-in per profile.

        Returns True when every camera opened, or a falsy CameraOpenError
        carrying the reason (which is also emitted on `error` and left on
        last_open_error)."""
        self._trigger_rate_limit = trigger_rate_limit
        self._max_num_buffer = int(max_num_buffer)
        self._baseline_exp_gain = []
        self.last_open_error = None
        # The agreed geometry describes the camera set THIS call opens. A value
        # left over from a previous profile makes every camera of a
        # different-resolution rig fail against a rig that is no longer open,
        # and the message blames camera 1 of the rig being opened.
        self._geometry = None
        if backend is not None and backend != getattr(self._backend, "name", None):
            self._backend = load_backend(backend)
        devices = self._backend.enumerate_devices()
        if len(devices) == 0:
            return self._open_failed("No cameras found")

        # Camera names are positional (`cam{i+1}`) and every extrinsic in
        # calibration.toml attaches to a name, so a device that shifts a
        # position attaches one camera's calibration to a different physical
        # camera: triangulation still runs and the 3D output is simply wrong.
        # There are two orderings, and both are explicit:
        #   - with `only_serials` (the profile's camera_serials), cam{i+1} is
        #     entry i of THAT LIST. An extra pylon device on the host - a USB
        #     camera, another rig on a shared segment - cannot shift a name,
        #     and a listed camera that did not enumerate is named rather than
        #     guessed at;
        #   - without one, the backend's serial-sorted enumeration order,
        #     which is why expect_cameras exists: a camera that never
        #     ENUMERATES (dead switch port, unpowered, still booting) is
        #     invisible to the open check further down, and a missing camera 3
        #     silently renames physical 4..9 to cam3..cam8.
        if only_serials:
            want = [str(x) for x in only_serials]
            by_serial = {d.GetSerialNumber(): d for d in devices}
            missing = [x for x in want if x not in by_serial]
            if missing:
                names = ", ".join(f"cam{want.index(x) + 1} ({x})"
                                  for x in missing)
                return self._open_failed(
                    f"Requested cameras did not enumerate: {names}\n\n"
                    f"They are named by their position in the profile's "
                    f"camera_serials, so opening without them would leave "
                    f"those names unrecorded. Power-cycle them and reselect "
                    f"the profile.")
            extra = [x for x in sorted(by_serial) if x not in set(want)]
            if extra:
                # Ignored rather than refused: an unlisted device cannot take
                # a name when the names come from the list, so it is a fact
                # about the host and not a fault in the rig.
                print(f"[cam] ignoring {len(extra)} enumerated device(s) not "
                      f"in the profile's camera_serials: {', '.join(extra)}",
                      flush=True)
            sorted_devs = [by_serial[x] for x in want]
        else:
            sorted_devs = devices      # backend guarantees a stable order

        # Counted AFTER the serial filter, because the count that matters is
        # the cameras THIS process opens: with a serial list an extra device
        # is not one of them, and a multi-process split opens its own share.
        if expect_cameras and len(sorted_devs) != expect_cameras:
            found = ", ".join(sorted(d.GetSerialNumber() for d in devices))
            return self._open_failed(
                f"Expected {expect_cameras} cameras but {len(sorted_devs)} "
                f"are available to open.\n\nEnumerated: {found}\n\n"
                f"Camera names are positional, so starting with a missing "
                f"camera would rename every camera after it and attach the "
                f"calibration extrinsics to the wrong physical cameras. "
                f"Power-cycle the missing camera and reselect the profile.")

        for i, dev in enumerate(sorted_devs):
            try:
                cam = self._backend.open(dev, pfs_path, self._max_num_buffer)
                # Listed before it is configured, so the close_all below
                # closes THIS camera too: a handle opened and then dropped is
                # still held by this process, and the retry the error message
                # asks for would find the camera busy.
                self._cameras.append(cam)
                # Read back what the .pfs actually applied. FeaturePersistence
                # is loaded with validation disabled, and CLAUDE.md tells users
                # to edit the .pfs in pylon Viewer — where ROI and pixel format
                # are one click away. Both failure modes are severe:
                #   - a Width/Height divergence makes `buf[:H,:] = img` raise
                #     EVERY frame, which now retires the camera but wastes a
                #     session;
                #   - Mono12 makes the frame uint16 and that same assignment
                #     truncates **mod 256 with no error at all** (measured:
                #     300 -> 44), yielding a full-length, perfectly aligned,
                #     visually shredded recording that looks fine until someone
                #     tries to label it.
                info = self._backend.describe(cam)
                pf, w, h = info["pixel_format"], info["width"], info["height"]
                if pf != "Mono8":
                    raise RuntimeError(
                        f"PixelFormat is {pf}, not Mono8. The capture path "
                        f"assumes 8-bit; anything wider is silently truncated "
                        f"mod 256. Fix the .pfs.")
                if expect_geometry and (w, h) != tuple(expect_geometry):
                    raise RuntimeError(
                        f"resolution {w}x{h} differs from the profile's "
                        f"{expect_geometry[0]}x{expect_geometry[1]}. Those two "
                        f"numbers size the NV12 ring and the raw decode, so a "
                        f"session recorded at this ROI is sheared or empty. "
                        f"Fix the .pfs (or the profile) so they agree.")
                if self._geometry and (w, h) != self._geometry:
                    raise RuntimeError(
                        f"resolution {w}x{h} differs from camera 1 "
                        f"({self._geometry[0]}x{self._geometry[1]}); all "
                        f"cameras must match.")
                self._geometry = (w, h)
                print(f"[cam{i+1}] {info['serial']} {w}x{h} {pf}", flush=True)
                # Remember what the .pfs applied, so a calibration-specific
                # exposure can be RESTORED exactly afterwards rather than
                # reconstructed. Leaking a calibration exposure into a 100 fps
                # recording would silently halve the frame rate.
                self._baseline_exp_gain.append(
                    self._backend.get_exposure_gain(cam))
                self._backend.enable_extended_block_ids(i, cam)
                self._backend.select_gige_driver(i, cam, gige_driver)
                if (gev_bandwidth_reserve_pct is not None
                        or gev_bandwidth_reserve_accum is not None):
                    # Applied at open, before any grabbing: the reserve lowers
                    # the assigned bandwidth GevSCBWA, so changing it under a
                    # running stream would repace a camera mid-recording. A
                    # backend or camera without the nodes raises, which is
                    # reported as this camera failing to configure - a profile
                    # asking for a knob the rig does not have is a
                    # misconfiguration, not something to apply to some of the
                    # cameras and not others.
                    applied = self._backend.set_bandwidth_reserve(
                        cam, gev_bandwidth_reserve_pct,
                        gev_bandwidth_reserve_accum)
                    print(f"[cam{i+1}] bandwidth reserve {applied}", flush=True)
            except Exception as e:
                # Never continue with a partial set: names are positional,
                # so a camera missing from the middle shifts every later
                # name and mislabels the recorded data.
                self.close_all()
                return self._open_failed(
                    f"Camera {dev.GetSerialNumber()} failed to open/configure:\n{e}\n\n"
                    "Power-cycle it (or close the app holding it) and reselect the profile.")

        self._set_freerun_mode()
        self._start_grab_threads()
        return True

    def _set_freerun_mode(self):
        for i, cam in enumerate(self._cameras):
            try:
                self._backend.set_freerun(cam, 30.0)
            except Exception as e:
                # A camera that dropped off the bus must not abort teardown for
                # the rest — log and continue so the survivors still recover.
                print(f"[cam{i+1}] free-run config failed (camera offline?): {e}",
                      flush=True)

    def _set_trigger_mode(self) -> list:
        """Put every camera into hardware-trigger mode.

        Returns the (index, error) pairs of cameras that refused. The caller
        decides what to do; a camera left free-running would deliver 30 fps
        block IDs that mean nothing relative to the trigger ordinal, and in
        kick mode the coordinator would force-drop every other camera's frames
        waiting for it.
        """
        limit = getattr(self, "_trigger_rate_limit", 165.0)
        failed = []
        for i, cam in enumerate(self._cameras):
            try:
                self._backend.set_triggered(cam, limit, announce=(i == 0))
            except Exception as e:
                print(f"[cam{i+1}] trigger config failed: {type(e).__name__}: {e}",
                      flush=True)
                failed.append((i, f"{type(e).__name__}: {e}"))
        return failed

    def _start_grab_threads(self, raw_paths=None, display_every=1,
                            realtime=False, width=0, height=0, quality=21,
                            fps=100):
        self._stop_grab_threads()
        for i, cam in enumerate(self._cameras):
            rp = raw_paths[i] if raw_paths else None
            gt = GrabThread(i, cam, self._backend, raw_path=rp,
                            display_every=display_every,
                            realtime=realtime, width=width, height=height,
                            quality=quality, fps=fps, router=self._router,
                            encoder_factory=self.encoder_factory)
            gt._pin_cpu = self.pin_capture_threads
            gt._pin_ecore = self.pin_encoder_threads
            gt._enc_pcores = self.encoder_pcores
            gt.start()
            self._grab_threads.append(gt)

    #: Pin each grab thread to a performance core (see cpu_affinity.py).
    #: Set from the profile before start_acquisition(). Off by default so a
    #: non-hybrid or non-Windows host behaves exactly as before.
    pin_capture_threads = False
    #: Confine encoder threads to E-cores. Measured WORSE; see session_config.
    pin_encoder_threads = False
    #: Confine encoder threads to the P-core SET. Separate from
    #: pin_encoder_threads, which pins one per E-CORE and measured
    #: catastrophic. See _EncoderThread.run.
    encoder_pcores = False
    #: Encoder factory handed to the router and the grab threads; None means
    #: the process default in gui_app.encoders (NVENC).
    encoder_factory = None

    def pinning_report(self) -> str:
        """One line saying how many grab threads actually pinned.

        Nine separate per-thread lines make a PARTIAL pin invisible -- eight
        successes and one silent failure reads as success at a glance, and the
        one that failed is exactly the camera that will lag.
        """
        want = bool(self.pin_capture_threads)
        if not want:
            return "cpu pinning: off"
        from gui_app.cpu_affinity import performance_cores
        n_p = len(performance_cores())
        if n_p == 0:
            # RULE: a host with no performance-core set is a documented no-op,
            # not a partial pin. Reporting 0/N as PARTIAL reads as a failure
            # to do something this host cannot do at all.
            return ("cpu pinning: requested, but this host publishes no "
                    "performance-core set (non-hybrid or non-Windows): no-op")
        got = sum(1 for gt in self._grab_threads
                  if getattr(gt, "pin_result", None)
                  and gt.pin_result.get("pinned"))
        flag = "" if got == len(self._grab_threads) else "  *** PARTIAL ***"
        return (f"cpu pinning: {got}/{len(self._grab_threads)} grab threads on "
                f"{n_p} P-cores{flag}")

    @staticmethod
    def _wait_all(threads, timeout_s: float) -> None:
        """Wait up to `timeout_s` IN TOTAL for the given threads to exit."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        for gt in threads:
            if gt.isRunning():
                gt.wait(max(1, int((deadline - time.monotonic()) * 1000)))

    def _retain_live(self, threads) -> list:
        """Move still-running threads (and their cameras) to the leak lists.

        Returns the indices that were still running. A running QThread must
        stay referenced or Qt aborts the process when its wrapper is
        collected, and its camera handle must stay referenced or the vendor
        object's destructor closes it under the live native call.
        """
        live = [i for i, gt in enumerate(threads) if gt.isRunning()]
        for i in live:
            self._leaked_threads.append(threads[i])
            self._leaked_cameras.append(threads[i]._camera)
        return live

    def _stop_grab_threads(self):
        for gt in self._grab_threads:
            gt.stop()
        self._wait_all(self._grab_threads, STOP_NORMAL_EXIT_S)
        self._retain_live(self._grab_threads)
        self._grab_threads.clear()

    def apply_exposure_gain(self, fps: float, exposure_us=None, gain_db=None):
        """Set exposure/gain for the acquisition about to start.

        This WRITES ExposureTime and Gain on every open camera, every time an
        acquisition starts. The .pfs remains the only SOURCE of a recording's
        values (nothing here writes back into the .pfs file), but the effective
        exposure can be below what the .pfs says — read the `[camN]
        exposure=...` line for that camera rather than assuming.

        Pass exposure_us=None (and gain_db=None) to RESTORE the .pfs baseline
        captured at open — which is what a recording does, so a
        calibration-specific exposure can never leak into it. That leak matters:
        in trigger mode the minimum interval is
        `exposure + 1/AcquisitionFrameRate`, so an exposure sized for 30 fps
        would exceed a 100 fps trigger period and silently halve the frame rate
        with no error anywhere.

        The ceiling is computed and ENFORCED here rather than trusted to the
        profile, because exceeding it fails silently.

        EVERY camera's applied exposure and gain is logged, and anything that
        did not land - a control the camera does not implement, a value the
        node clamped - is appended to last_warnings. A calibration exposure
        left on one camera halves that camera's frame rate in the next
        recording, and until that is recorded per camera the only evidence is
        a rate-check warning after the session.

        Never raises for a bad profile: a frame rate at or above the limiter
        is refused by RigProfile.load, so it cannot reach this call inside a
        Qt slot.
        """
        limit = float(getattr(self, "_trigger_rate_limit", 165.0) or 0.0)
        if limit > 0:
            # In trigger mode the frame-rate timer starts AFTER exposure ends,
            # so the minimum interval is exposure + 1/limit.
            ceiling_us = (1e6 / float(fps) - 1e6 / float(limit)) * 0.9
            limiter = f"AcquisitionFrameRate={limit:g}"
        else:
            # RULE: report the limiter as disabled instead of substituting a
            # default. _set_trigger_mode really did disable
            # AcquisitionFrameRate, so the only bound left is the trigger
            # period itself, and a log line quoting a limiter that is not
            # running is worse than no line at all.
            ceiling_us = (1e6 / float(fps)) * 0.9
            limiter = "limiter disabled"
        if ceiling_us <= 0:
            # fps >= limit: no exposure fits the trigger period at all.
            # RigProfile.load refuses that pairing, so reaching it means the
            # numbers were set some other way; clamping to a non-positive
            # ceiling would record at the sensor minimum and report success,
            # when the real fault is that the camera skips triggers.
            msg = (f"frame rate {fps:g} is at or above the trigger rate limit "
                   f"{limit:g}: the camera skips triggers at this rate and no "
                   f"exposure ceiling exists, so exposure is left as asked")
            print(f"[acq] WARNING: {msg}", flush=True)
            self.last_warnings.append(msg)
            ceiling_us = None
        ceiling_txt = ("none" if ceiling_us is None
                       else f"{ceiling_us:.0f} us")
        for i, cam in enumerate(self._cameras):
            base_exp, base_gain = (self._baseline_exp_gain[i]
                                   if i < len(self._baseline_exp_gain)
                                   else (None, None))
            want_exp = base_exp if exposure_us is None else float(exposure_us)
            want_gain = base_gain if gain_db is None else float(gain_db)
            note = ""
            if (ceiling_us is not None and want_exp is not None
                    and want_exp > ceiling_us):
                note = (f" CLAMPED from {want_exp:.0f} us: at {fps:g} fps with "
                        f"{limiter} the ceiling is {ceiling_us:.0f} us, and "
                        f"exceeding it would halve the frame rate silently")
                want_exp = ceiling_us
            # The unit is stated only for a value that came from the PROFILE,
            # where gain is documented in dB: the backend then refuses a raw
            # camera rather than writing 6 dB as 6 raw steps. The baseline
            # restore writes back what the same node reported, so its unit is
            # the node's by construction and stating one would refuse a
            # legitimate raw-gain camera.
            unit = {} if gain_db is None else {"gain_unit": "dB"}
            try:
                exp, gain = self._backend.set_exposure_gain(
                    cam, want_exp, want_gain, **unit)
            except Exception as e:
                msg = (f"cam{i+1}: exposure/gain was NOT applied "
                       f"({type(e).__name__}: {e}); this camera is recording "
                       f"at whatever the previous acquisition left")
                print(f"[cam{i+1}] exposure/gain set failed: {e}", flush=True)
                self.last_warnings.append(msg)
                continue
            # Every camera, every time (mandate M7): a value that lands on
            # cam1 and not on cam5 is invisible otherwise, and cam5 then
            # records at the wrong exposure with nothing in the log.
            print(f"[cam{i+1}] exposure="
                  f"{exp if exp is None else f'{exp:.0f}'} us gain="
                  f"{gain if gain is None else f'{gain:.1f}'} "
                  f"(ceiling {ceiling_txt} at {fps:g} fps, {limiter}){note}",
                  flush=True)
            self.last_warnings.extend(
                self._exposure_gain_warnings(i, want_exp, want_gain, exp, gain))

    @staticmethod
    def _exposure_gain_warnings(i: int, want_exp, want_gain, exp, gain) -> list:
        """Warnings for a camera whose exposure or gain did not land.

        A read-back of None means the camera has no such control, and a
        read-back further from the request than the node's own quantisation
        can explain means the node clamped the value: the camera is recording
        at something other than what was asked, which is exactly the silent
        frame-rate halving the .pfs restore exists to prevent. The tolerance
        is one unit or 1%, whichever is larger, because a node increment is
        model-specific and a legitimate rounding must not read as a fault.
        """
        out = []
        for label, want, got, unit, floor in (
                ("exposure", want_exp, exp, " us", 1.0),
                ("gain", want_gain, gain, "", 0.1)):
            if want is None:
                continue
            if got is None:
                out.append(f"cam{i+1}: {label} {want:g}{unit} was not applied "
                           f"(the camera reports no such control)")
            elif abs(float(got) - float(want)) > max(floor, 0.01 * abs(want)):
                out.append(f"cam{i+1}: {label} was set to {float(got):g}{unit}, "
                           f"not the {want:g}{unit} asked for (the camera "
                           f"clamped it to its own range)")
        return out

    def start_acquisition(self, raw_paths: list[Path], display_every: int = 10,
                          realtime: bool = False, width: int = 0, height: int = 0,
                          quality: int = 21, fps: int = 100,
                          realtime_kick: bool = False,
                          kick_max_lag: int = 240,
                          exposure_us=None, gain_db=None):
        # Refused BEFORE anything is stopped or reconfigured, so the cameras
        # stay in preview and the operator can fix the .pfs and start again:
        # width/height size the NV12 ring and the raw decode, so a
        # disagreement with the cameras' ROI retires every camera (real-time)
        # or shears a full-length recording (raw). Both waste the session.
        if (self._geometry and width and height
                and (int(width), int(height)) != self._geometry):
            raise AcquisitionStartRefused(
                f"The profile records {int(width)}x{int(height)} but the "
                f"cameras are configured for {self._geometry[0]}x"
                f"{self._geometry[1]}. Nothing was recorded and the cameras "
                f"are still in preview: fix the .pfs (or the profile) so they "
                f"agree.")
        # Grab threads first, router second: the previous session's kick-mode
        # threads submit() to the router until they exit and retire() through
        # it from their finally blocks, so the router must outlive them.
        self._stop_grab_threads()
        if self._router is not None:
            # A router from an acquisition that was never stopped holds one
            # encoder session and one open stream.h264 per camera. Dropping the
            # reference would keep its encoder threads blocked on their queues
            # for the life of the process, and the new router would then hit
            # the NVENC session cap and every camera would fall to raw.bin.
            print("[acq] WARNING: start_acquisition called with a live router; "
                  "abandoning it and releasing its encoders first", flush=True)
            try:
                self._router.abandon()
            except Exception as e:
                print(f"[acq] abandoning the stale router failed: {e}", flush=True)
            self._router = None
        self.last_warnings = []
        if realtime and realtime_kick:
            # Shared router gates frames through the cross-camera coordinator so
            # only frames every camera captured get encoded (already aligned, no
            # post-hoc re-encode). The profile asked for kick-out, so anything
            # less is refused: the decoupled fallback would try one encoder per
            # grab thread against the same exhausted session cap and end in
            # raw.bin at ~129 GiB per 10 minutes, unaligned, with the capacity
            # preflight having budgeted for H.264.
            from gui_app.sync_encode import SyncEncodeRouter
            router = SyncEncodeRouter(raw_paths, width, height, quality,
                                      fps=fps, max_lag=kick_max_lag,
                                      pin_encoders=self.pin_encoder_threads,
                                      enc_pcores=self.encoder_pcores,
                                      encoder_factory=self.encoder_factory)
            if not router.available:
                self._start_grab_threads()      # back to preview
                raise AcquisitionStartRefused(
                    f"Real-time kick-out was requested but its encoders could "
                    f"not be created: {router.unavailable_reason}\n\nNothing "
                    f"was recorded. If this is the NVENC session cap, close "
                    f"other GPU-encoding applications or restart this one.")
            router.start()
            self._router = router
            print("[acq] real-time kick-out router active", flush=True)
        failed = self._set_trigger_mode()
        if failed:
            # Refuse rather than record a partial set: names are positional
            # and a free-running camera poisons the coordinator (see the
            # exception's docstring). Undo everything done so far.
            names = ", ".join(f"cam{i+1} ({err})" for i, err in failed)
            print(f"[acq] REFUSING to start: trigger mode failed on {names}",
                  flush=True)
            if self._router is not None:
                try:
                    self._router.abandon()
                except Exception:
                    pass
                self._router = None
            self._set_freerun_mode()
            self._start_grab_threads()
            raise AcquisitionStartRefused(
                f"Could not put every camera into trigger mode:\n{names}\n\n"
                f"Nothing was recorded. Power-cycle the camera and retry.")
        # AFTER trigger mode, and the order is load-bearing: _set_trigger_mode
        # rewrites AcquisitionFrameRate and StopGrabbing()s the camera, so an
        # exposure applied before it would be set against the free-run
        # configuration that is about to be replaced. Note the ceiling below is
        # computed from self._trigger_rate_limit — the same number
        # _set_trigger_mode writes — and is NOT read back from the camera.
        self.apply_exposure_gain(fps, exposure_us, gain_db)
        self._start_grab_threads(raw_paths=raw_paths, display_every=display_every,
                                 realtime=realtime, width=width, height=height, quality=quality,
                                 fps=fps)

    def wait_until_ready(self, timeout_s: float = 30.0) -> tuple[int, int]:
        """Block until every grab thread has its ring and its stream, or time out.

        Returns (ready, total). The caller starts the trigger board only after
        this, because starting it earlier is what produced the startup backlog:
        each thread writes a 2.57 GiB NV12 ring at nine cameras, and frames
        delivered during that allocation queue in the driver. The grab loop
        retrieves at exactly the arrival rate, so a backlog created here is
        never recovered -- on 2026-09-14 one camera began 124 frames behind and
        rode kick_max_lag for the whole session, force-dropping 2,036 frames.

        A timeout is not fatal: a thread that cannot become ready sets its event
        anyway, and the coordinator retires a camera that never publishes, so
        the remaining cameras still record aligned.
        """
        import time as _time
        deadline = _time.monotonic() + max(0.0, timeout_s)
        total = len(self._grab_threads)
        for gt in self._grab_threads:
            ev = getattr(gt, "ready", None)
            if ev is None:
                continue
            ev.wait(max(0.0, deadline - _time.monotonic()))
        ready = sum(1 for gt in self._grab_threads
                    if getattr(getattr(gt, "ready", None), "is_set", bool)())
        return ready, total

    def signal_triggers_started(self):
        """Tell every grab thread the trigger board acknowledged its start.

        Call this right after start_triggers() returns True. It arms the
        threads' stall detectors: until then a run of retrieve timeouts is the
        board not yet running, and a re-arm inside that gap would restart the
        block-ID counter with no history to re-base it against and retire the
        camera on its first real frame.
        """
        for gt in self._grab_threads:
            gt.signal_triggers_started()

    def stop_acquisition(self) -> list[tuple[int, list[float], list[int]]]:
        """Stop the grab threads and return each camera's
        (frame_count, timestamps, block_ids).

        Does NOT restore preview — the caller should save frametimes/metadata first,
        then call resume_preview(). Separating data collection from the camera
        reconfigure means a camera that dropped off the bus can't crash teardown
        (or take the other cameras' data down with it) before the data is written.

        Worst case this blocks STOP_NORMAL_EXIT_S + STOP_FORCED_EXIT_S +
        STOP_DRAIN_S (~115 s) regardless of camera count; each bound is one
        shared deadline for all threads.

        Raises AcquisitionStopIncomplete when a thread is still running at the
        end: the results are on the exception and on last_results, and the
        cameras are left untouched for abandon().
        """
        # Seeded with what is already recorded, not emptied: the
        # exposure/gain problems apply_exposure_gain found at start belong to
        # THIS recording, and the session's WARNINGS.txt is written from this
        # list. A camera that recorded at the wrong exposure is exactly what
        # that file exists to show.
        warnings: list = list(self.last_warnings)
        threads = self._grab_threads
        for gt in threads:
            gt.signal_triggers_stopped()
        # Normal exit: the board has stopped, the loop drains what is left in
        # the pool and leaves on its first retrieve timeout (200 ms).
        self._wait_all(threads, STOP_NORMAL_EXIT_S)
        # Escalation. The loop only honours signal_triggers_stopped() on a
        # timeout, so a camera still receiving frames (the board ignored the
        # stop, or a camera never left free-run) never times out and the
        # thread never exits on its own. Frames after the stop command are not
        # wanted, so tell it to stop outright; that ends the loop within one
        # retrieve (at most 200 ms) unless it is wedged in a native call.
        # Only a thread whose RETRIEVE LOOP is still running is escalated: a
        # thread already in its decoupled-encoder drain receives nothing, and
        # its frame_count may still move as the queue takes the pool's last
        # frames, so it must not be read as a board that ignored the stop.
        escalated = []
        for i, gt in enumerate(threads):
            if not gt.isRunning() or gt.retrieve_loop_exited:
                continue
            msg = (f"cam{i+1}: its grab thread was still receiving frames "
                   f"{STOP_NORMAL_EXIT_S:.0f} s after the trigger board was told "
                   f"to stop; the board may still be triggering, or the camera "
                   f"was not in trigger mode. The thread was stopped outright.")
            print(f"[acq] WARNING: {msg}", flush=True)
            warnings.append(msg)
            gt.stop()
            escalated.append(gt)
        self._wait_all(escalated, STOP_FORCED_EXIT_S)
        # A thread still running now is inside its decoupled-encoder drain,
        # which is bounded (~95 s worst case: 30 s sentinel put + 60 s join),
        # or wedged in a native call. Wait the bound out loudly.
        draining = [gt for gt in threads if gt.isRunning()]
        for i, gt in enumerate(threads):
            if gt.isRunning():
                print(f"[cam{i+1}] grab thread still draining at stop, waiting...", flush=True)
        self._wait_all(draining, STOP_DRAIN_S)

        if self._router is not None:
            # Kick-out mode: grab threads have stopped submitting; flush the
            # coordinator and drain the shared encoders. Metadata (the released,
            # already-common frames) comes from the router, not the grab threads.
            results = self._router.stop()
            # Read the warnings BEFORE dropping the router, or they are lost
            # with it — which is how a truncated or retired camera used to
            # degrade to a line on stdout that nobody was watching.
            warnings.extend(self._router.warnings)
            self._router = None
        else:
            results = [(gt.frame_count, gt.timestamps, gt.block_ids)
                       for gt in self._grab_threads]
            # Decoupled/raw modes: each thread reconciled its own bookkeeping
            # (truncation, failed resync, spilled tail) and carries the result.
            for gt in self._grab_threads:
                warnings.extend(gt.warnings)
        self.last_warnings = warnings
        self.last_results = results
        self.last_stream_stats = self._collect_stream_stats(
            skip={id(gt._camera) for gt in threads if gt.isRunning()})

        stuck = [i for i, gt in enumerate(threads) if gt.isRunning()]
        if stuck:
            # Never hand the cameras back to preview under a live grab thread:
            # set_freerun() calls StopGrabbing() on the same handle the thread
            # is inside RetrieveResult on, which is concurrent native access.
            # The thread list is kept so abandon() can still find them. The
            # results travel on the exception: the streams are on disk, but
            # blockids.npy / frametimes.npy are the caller's to write, and
            # without them the healthy cameras' streams cannot be aligned.
            names = ", ".join(f"cam{i+1}" for i in stuck)
            raise AcquisitionStopIncomplete(
                f"grab thread(s) for {names} did not exit after the stop "
                f"timeouts (GPU or driver wedged?). The cameras were NOT "
                f"reconfigured; restart the application before recording "
                f"again. The captured streams are on disk, but blockids.npy "
                f"and frametimes.npy have NOT been written yet: save "
                f"CameraManager.last_results before abandoning.",
                results, stuck)
        self._grab_threads.clear()
        return results

    def _collect_stream_stats(self, skip=frozenset()) -> list:
        """backend.stream_stats() per camera, one dict each.

        A camera whose grab thread is still running is skipped and a backend
        without the call or a camera that dropped off the bus contributes an
        error entry: the counters are evidence for the metadata, never a
        reason to fail a stop or to read a node map from two threads at once.
        The authoritative read is the grab thread's own, taken before it
        stops grabbing, because StopGrabbing resets the counters.
        """
        fn = getattr(self._backend, "stream_stats", None)
        out = []
        for cam in self._cameras:
            if id(cam) in skip:
                out.append({"error": "grab thread still running"})
            elif fn is None:
                out.append({"error": "backend reports no stream statistics"})
            else:
                try:
                    out.append(dict(fn(cam)))
                except Exception as e:
                    out.append({"error": f"{type(e).__name__}: {e}"})
        return out

    def resume_preview(self, preview_fps: float = 30.0):
        """Return all cameras to free-run preview after an acquisition. Resilient
        to a camera that went offline mid-session (it is skipped, not fatal).

        Restores the .pfs exposure and gain. Without this the preview keeps
        whatever the last acquisition set, so after a calibration it sat at
        calibration_exposure_us (15 ms on 3dpose, 5x the recording value). Free
        run at 30 fps has the headroom, so nothing breaks — it just looks far
        brighter than what a recording will actually capture, which is exactly
        the misreading the "judge exposure from a recording, not the preview"
        rule exists to prevent. Passing None restores the baseline read at open.

        Refuses while any grab thread is still running: reconfiguring a camera
        under a thread inside RetrieveResult is concurrent native access.
        """
        live = [i for i, gt in enumerate(self._grab_threads) if gt.isRunning()]
        if live:
            raise RuntimeError(
                "resume_preview() called while grab thread(s) "
                + ", ".join(f"cam{i+1}" for i in live)
                + " are still running; the cameras were not reconfigured")
        self._set_freerun_mode()
        self.apply_exposure_gain(preview_fps, None, None)
        self._start_grab_threads()

    def close_all(self):
        self._stop_grab_threads()
        for cam in self._cameras:
            try:
                self._backend.close(cam)
            except Exception:
                pass
        self._cameras.clear()
        # Geometry belongs to the open camera set; see open_all.
        self._geometry = None

    def abandon(self):
        """Tear down capture immediately, WITHOUT draining encoders (app quit
        mid-session, or a finalize that failed). The grab threads skip their
        encoder drain and close their stream fds at once, and the kick-out
        router stops its encoder threads, releases their sessions and closes
        its fds, so the half-baked stream files unlock and can be deleted and
        the sessions are available to the next acquisition in this process.

        A camera whose grab thread is still running after the wait is NOT
        stopped or closed: by hypothesis that thread is wedged inside a native
        call on the same handle, and stop_grabbing()/close() from this thread
        would be the concurrent native access stop_acquisition refused to
        risk. Its thread and handle are kept referenced instead (see
        _leaked_threads), which costs nothing at process exit and, on the
        return-to-IDLE path, is the lesser harm.

        Bounded by ABANDON_THREAD_S for all grab threads together plus the
        router's own abandon bound, independent of camera count.
        """
        threads = self._grab_threads
        for gt in threads:
            gt.abandon()
        self._wait_all(threads, ABANDON_THREAD_S)
        live = self._retain_live(threads)
        live_cams = {id(threads[i]._camera) for i in live}
        for i in live:
            print(f"[cam{i+1}] grab thread still running at abandon; leaking "
                  f"its camera handle rather than closing under a live native "
                  f"call", flush=True)
        self._grab_threads = []
        if self._router is not None:
            try:
                self._router.abandon()
            except Exception:
                pass
            self._router = None
        for cam in self._cameras:
            if id(cam) in live_cams:
                continue
            try:
                self._backend.stop_grabbing(cam)
            except Exception:
                pass
            try:
                self._backend.close(cam)
            except Exception:
                pass
        self._cameras.clear()
        # Geometry belongs to the open camera set; see open_all.
        self._geometry = None
