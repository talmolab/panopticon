"""Manages the camera set — opening, closing, and switching between free-run and
trigger modes. The count comes from the profile (`n_cameras`) and is enforced
by `open_all(expect_cameras=...)`, not hardcoded here."""
import gc
import inspect
import time
import numpy as np
from pathlib import Path
from PyQt5.QtCore import QObject, pyqtSignal
from gui_app.backends import load_backend
from gui_app.frame_sync import source_name
from gui_app.grab_thread import GrabThread, SOURCE_SILENT_S

#: Bounds on stop_acquisition, each applied to its PHASE as one shared
#: deadline rather than to each thread in turn: the threads exit concurrently,
#: so a per-thread wait would make a nine-camera stop take nine times longer
#: than a one-camera stop in exactly the case the bound exists for (every
#: camera still receiving frames).
STOP_NORMAL_EXIT_S = 5.0       # loop leaves on its first timeout after the board stops
STOP_FORCED_EXIT_S = 10.0      # after gt.stop(): one retrieve, unless wedged natively
STOP_DRAIN_S = 100.0           # decoupled-encoder drain: 30 s sentinel + 60 s join
ABANDON_THREAD_S = 3.0         # abandon(): loop exit + encoder abort, all threads
#: A camera silent for longer than this reads 0 fps. A grab thread updates
#: its frame rate only when a frame arrives, so without the decay a camera
#: that stopped receiving would keep showing its last rate.
FPS_DECAY_S = 1.0


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
#: staler), and too deep starves capture outright. See docs/HISTORY.md.
MAX_NUM_BUFFER = 1000


def resolve_device_order(devices, only_serials=None, expect_cameras: int = 0,
                         global_indices=None, announce_extra: bool = True):
    """The enumerated devices in cam1..camN order, and why they cannot be
    opened: (devices, None), or (None, refusal message).

    Camera names are positional (`cam{i+1}`) and every extrinsic in
    calibration.toml attaches to a name, so a device that shifts a
    position attaches one camera's calibration to a different physical
    camera: triangulation still runs and the 3D output is simply wrong.
    There are two orderings, and both are explicit:
      - with `only_serials` (the profile's camera_serials), cam{i+1} is
        entry i of THAT LIST. An extra device on the host - a USB camera,
        another rig on a shared segment - cannot shift a name, and a listed
        camera that did not enumerate is named rather than guessed at;
      - without one, the backend's serial-sorted enumeration order, which is
        why expect_cameras exists: a camera that never ENUMERATES (dead
        switch port, unpowered, still booting) is invisible to the open
        check, and a missing camera 3 silently renames physical 4..N to
        cam3..cam(N-1).

    `expect_cameras`, when nonzero, is counted AFTER the serial filter,
    because the count that matters is the cameras the caller opens: with a
    serial list an extra device is not one of them. A capture worker is
    handed its own share as `only_serials` and checks its own count;
    `global_indices` (one per entry of `only_serials`) then names a missing
    camera by its index in the whole rig, and `announce_extra` False leaves
    the line about devices the list does not name to the caller.
    """
    if only_serials:
        want = [str(x) for x in only_serials]
        by_serial = {d.GetSerialNumber(): d for d in devices}
        missing = [x for x in want if x not in by_serial]
        if missing:
            gidx = (list(global_indices) if global_indices is not None
                    else list(range(len(want))))
            names = ", ".join(f"cam{gidx[want.index(x)] + 1} ({x})"
                              for x in missing)
            return None, (
                f"Requested cameras did not enumerate: {names}\n\n"
                f"They are named by their position in the profile's "
                f"camera_serials, so opening without them would leave "
                f"those names unrecorded. Power-cycle them and reselect "
                f"the profile.")
        extra = [x for x in sorted(by_serial) if x not in set(want)]
        if extra and announce_extra:
            # Ignored rather than refused: an unlisted device cannot take
            # a name when the names come from the list, so it is a fact
            # about the host and not a fault in the rig.
            print(f"[cam] ignoring {len(extra)} enumerated device(s) not "
                  f"in the profile's camera_serials: {', '.join(extra)}",
                  flush=True)
        sorted_devs = [by_serial[x] for x in want]
    else:
        sorted_devs = devices      # backend guarantees a stable order
    if expect_cameras and len(sorted_devs) != expect_cameras:
        found = ", ".join(sorted(d.GetSerialNumber() for d in devices))
        return None, (
            f"Expected {expect_cameras} cameras but {len(sorted_devs)} "
            f"are available to open.\n\nEnumerated: {found}\n\n"
            f"Camera names are positional, so starting with a missing "
            f"camera would rename every camera after it and attach the "
            f"calibration extrinsics to the wrong physical cameras. "
            f"Power-cycle the missing camera and reselect the profile.")
    return sorted_devs, None


def _device_model(dev):
    """The model name an enumerated device reports, or None.

    Device objects are opaque to this module apart from GetSerialNumber();
    pylon's device info also answers GetModelName(), and a backend whose
    devices do not is asked through describe() instead (its "model" key).
    """
    fn = getattr(dev, "GetModelName", None)
    if fn is None:
        return None
    try:
        return str(fn()) or None
    except Exception:
        return None


class CameraManager(QObject):
    error = pyqtSignal(str)

    #: Backend name and instance, restated as class defaults because a manager
    #: built with __new__ - which is how the offline tests skip the Qt parent -
    #: assigns `_backend` without ever running __init__.
    _backend_name = "basler"
    _backend_obj = None
    #: The same reason for the state below; every one of them is REASSIGNED,
    #: never mutated in place, so the class-level objects stay empty.
    _camera_info: tuple = ()
    _board_started_t = None
    _uses_camera_block = False
    #: Why each camera of the last recording was retired, {"camN": reason}.
    #: The main window writes it beside the camera's video as RETIRED.json
    #: (main_window._write_retired).
    last_retired: dict = {}
    #: "camN: reason" for every real-time encoder that could not be created
    #: or failed during the last recording. The main window invalidates the
    #: NVENC session count the preflight cached when this is not empty.
    last_encoder_failures: list = []
    #: What starts the triggers, the profile's trigger_source ("board" or
    #: "external"). It picks the wording of the stop warning for a camera
    #: still receiving frames, because an external source is stopped by the
    #: operator, not by a command from this host. The window sets it at every
    #: start.
    trigger_source = "board"
    #: How many worker processes capture the cameras: 0, because this
    #: manager captures them in the calling process. The multi-process
    #: manager (gui_app.mp.manager) sets its own count. The main window
    #: records it in session_metadata.json as capture_processes_used.
    capture_processes = 0

    def __init__(self, backend: str = "basler"):
        # The only vendor-specific object in this class. Everything below is
        # camera-agnostic orchestration; see gui_app/backends/__init__.py for
        # what a new backend has to provide.
        #
        # RULE: construction records the backend NAME and loads nothing; the
        # instance is built on first use, by the `_backend` property below.
        # REASON: load_backend imports the vendor SDK, and the manager is
        # constructed before the profile that names the backend is known -
        # so an eager load imports pypylon whatever `camera_backend` the
        # profile says, and the application cannot be built at all on a host
        # that has no pypylon, which is exactly the hardware-free run the
        # simulated backend exists for.
        self._backend_name = backend
        self._backend_obj = None
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
        #: Per-camera GigE TRANSPORT SETTINGS from the last
        #: stop_acquisition(), index-aligned with the camera list and held
        #: for the session metadata, so a session's network state is on
        #: record. It belongs to ONE recording: start_acquisition and
        #: abandon() empty it, because a stale entry read as this session's
        #: describes the wrong network. The stream-grabber counters that
        #: separate host starvation from network loss are NOT in it; see
        #: STREAM_COUNTERS_NOTE for why they cannot be read here.
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
        self.last_retired = {}
        self.last_encoder_failures = []

    @property
    def _backend(self):
        """The backend instance, loaded from `backend_name` on first use.

        Every vendor call in this class goes through here, so the import of
        the SDK happens on the first one - open_all's enumerate_devices in
        practice - and never at construction. A caller that already holds a
        backend object (a test with a stub, a profile switch) assigns it to
        this name and no load happens at all.
        """
        if self._backend_obj is None:
            self._backend_obj = load_backend(self._backend_name)
        return self._backend_obj

    @_backend.setter
    def _backend(self, backend) -> None:
        self._backend_obj = backend
        self._backend_name = getattr(backend, "name", self._backend_name)

    @property
    def backend_name(self) -> str:
        """Name of the backend this manager loads (or has loaded).

        Readable without loading anything, so a caller can report which
        vendor layer a profile selected on a host that could not import it.
        """
        return self._backend_name

    @property
    def backend_loaded(self) -> bool:
        """Whether the backend instance has been built yet."""
        return self._backend_obj is not None

    @property
    def num_cameras(self) -> int:
        return len(self._cameras)

    @property
    def geometry(self):
        """(width, height) every open camera agreed on, or None when none are
        open.

        RULE: None means no camera is open. REASON: every guard on this value
        is written `if self.geometry and ...`, so a None left behind by an
        open that refused while cameras were still open would turn each of
        those guards into a silent no-op; open_all closes the previous set
        before it can refuse, which is what keeps the two in step.

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
        """Per-camera frame rate, 0.0 for a camera silent over FPS_DECAY_S.

        The decay is applied here, when the rate is read, so the grab loop
        does no extra work for it.
        """
        now = time.perf_counter()
        return [0.0 if self._silence(gt, now) > FPS_DECAY_S else gt.current_fps
                for gt in self._grab_threads]

    @property
    def snapshots(self) -> list:
        return [gt.snapshot_frame for gt in self._grab_threads]

    @property
    def latest_full_frames(self) -> list:
        return [gt.latest_full_frame for gt in self._grab_threads]

    def latest_full_frames_with_bids(self) -> list:
        """Per camera, (full-resolution frame, block ID) of one frame, or None.

        Each pair is read as one tuple the grab thread published in a single
        assignment, so the ID is always that frame's own. The ID is the
        camera's unwrapped trigger ordinal during a recording (the numbering
        of frame_sync.unwrap_blockids over its blockids.npy) and None in
        preview. Frames are kept only while set_keep_full(True) is on.
        """
        return [getattr(gt, "latest_full_with_bid", None)
                for gt in self._grab_threads]

    @property
    def camera_info(self) -> list:
        """Per camera, in cam1..camN order, the dict {"serial", "model",
        "backend"} captured when the camera was opened.

        `model` is None when neither the backend's describe() nor the
        device reports one. Session metadata records this, so which
        physical camera became camN is on disk, not only in the log.
        """
        return [dict(d) for d in self._camera_info]

    @property
    def delivery_lags(self) -> list[float]:
        """Per-camera seconds behind real time. See GrabThread.delivery_lag_s."""
        return [gt.delivery_lag_s for gt in self._grab_threads]

    @property
    def frame_counts(self) -> list[int]:
        return [gt.frame_count for gt in self._grab_threads]

    def results_received(self) -> list[int]:
        """Per camera, grab results this acquisition has retrieved: the
        frames it counted plus the grabs that failed.

        A failed grab is a trigger the camera acquired and lost in
        transmission, so it proves the trigger source is running even on a
        camera none of whose frames arrive whole. An external trigger source
        is judged started by this (trigger_source.ExternalTriggerSource).
        Reads two counters the grab loop already keeps.
        """
        return [int(gt.frame_count) + int(getattr(gt, "failed_grabs", 0))
                for gt in self._grab_threads]

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

    def _settings_source(self) -> str:
        """What a message tells the operator to fix: the file the camera
        settings come from, or the profile block that replaces it."""
        if self._uses_camera_block:
            return "the profile's camera: block"
        return "the .pfs"

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
                 gev_bandwidth_reserve_accum=None,
                 camera_spec=None, frame_rate=None):
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
        bandwidth every camera gets, so they are opt-in per profile. A
        backend without set_bandwidth_reserve refuses the open, naming the
        two fields to remove.

        camera_spec: the profile's parsed `camera:` block, or None. Passed to
        backend.open as camera_spec only when not None, so a backend written
        before the block existed keeps working; a backend whose settings come
        from elsewhere refuses a non-None value (Basler: the .pfs).

        frame_rate: the profile's frame_rate, or None. It reaches
        backend.open as frame_rate, and expect_geometry reaches it as
        frame_size, only when the backend's open takes that keyword by name
        (_backend_open_kwargs). A backend that takes them programs the ROI
        from the profile and refuses at open a rate its camera cannot
        record. One that does not (Basler: the .pfs holds the ROI) is called
        as before.

        Returns True when every camera opened, or a falsy CameraOpenError
        carrying the reason (which is also emitted on `error` and left on
        last_open_error)."""
        # RULE: a camera set that is still open is closed HERE, before any
        # refusal below can return. REASON: the refusals for "No cameras
        # found", a missing serial and expect_cameras return without touching
        # self._cameras, while the geometry reset just below has already run,
        # so the previous cameras would be left open and grabbing with
        # geometry None. Every geometry guard is written `if self._geometry
        # and ...`, so the start-time refusal and any preflight built on
        # `geometry` would pass silently on cameras whose ROI was never
        # checked against this profile - and the property promises that None
        # means no cameras are open.
        if self._cameras:
            self.close_all()
        self._trigger_rate_limit = trigger_rate_limit
        self._max_num_buffer = int(max_num_buffer)
        self._baseline_exp_gain = []
        self._camera_info = ()
        self._uses_camera_block = camera_spec is not None
        self.last_open_error = None
        # The agreed geometry describes the camera set THIS call opens. A value
        # left over from a previous profile makes every camera of a
        # different-resolution rig fail against a rig that is no longer open,
        # and the message blames camera 1 of the rig being opened.
        self._geometry = None
        # RULE: a new name is recorded and the old instance dropped; the new
        # one is built by the property, on the enumerate below. REASON: the
        # comparison must not touch self._backend, because reading it to ask
        # what the current backend is called would load the one being
        # replaced - which is the vendor SDK this profile says it does not
        # need.
        if backend is not None and backend != self._backend_name:
            self._backend_name = backend
            self._backend_obj = None
        devices = self._backend.enumerate_devices()
        if len(devices) == 0:
            return self._open_failed("No cameras found")
        sorted_devs, refusal = resolve_device_order(
            devices, only_serials, expect_cameras,
            global_indices=self.global_indices,
            announce_extra=self.announce_unlisted)
        if refusal:
            return self._open_failed(refusal)

        # The keyword is passed only for a profile that has a camera: block,
        # so a backend written before the block existed is called as before.
        spec_kw = {} if camera_spec is None else {"camera_spec": camera_spec}
        spec_kw.update(self._backend_open_kwargs(
            frame_size=tuple(expect_geometry) if expect_geometry else None,
            frame_rate=frame_rate))
        infos = []
        fix = self._settings_source()
        for i, dev in enumerate(sorted_devs):
            try:
                cam = self._backend.open(dev, pfs_path, self._max_num_buffer,
                                         **spec_kw)
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
                        f"mod 256. Fix {fix}.")
                if expect_geometry and (w, h) != tuple(expect_geometry):
                    raise RuntimeError(
                        f"resolution {w}x{h} differs from the profile's "
                        f"{expect_geometry[0]}x{expect_geometry[1]}. Those two "
                        f"numbers size the NV12 ring and the raw decode, so a "
                        f"session recorded at this ROI is sheared or empty. "
                        f"Fix {fix} (or the profile) so they agree.")
                if self._geometry and (w, h) != self._geometry:
                    raise RuntimeError(
                        f"resolution {w}x{h} differs from camera 1 "
                        f"({self._geometry[0]}x{self._geometry[1]}); all "
                        f"cameras must match.")
                self._geometry = (w, h)
                print(f"[{self._cn(i)}] {info['serial']} {w}x{h} {pf}", flush=True)
                # Which physical camera became camN, for the session
                # metadata: the log line above is otherwise the only record.
                infos.append({"serial": str(info["serial"]),
                              "model": info.get("model") or _device_model(dev),
                              "backend": self._backend_name})
                # Remember what the .pfs applied, so a calibration-specific
                # exposure can be RESTORED exactly afterwards rather than
                # reconstructed. Leaking a calibration exposure into a 100 fps
                # recording would silently halve the frame rate.
                self._baseline_exp_gain.append(
                    self._backend.get_exposure_gain(cam))
                self._backend.enable_extended_block_ids(self._gi(i), cam)
                self._backend.select_gige_driver(self._gi(i), cam, gige_driver)
                if (gev_bandwidth_reserve_pct is not None
                        or gev_bandwidth_reserve_accum is not None):
                    # Applied at open, before any grabbing: the reserve lowers
                    # the assigned bandwidth GevSCBWA, so changing it under a
                    # running stream would repace a camera mid-recording. A
                    # backend or camera without the control raises, which is
                    # reported as this camera failing to configure - a profile
                    # asking for a knob the rig does not have is a
                    # misconfiguration, not something to apply to some of the
                    # cameras and not others.
                    reserve = getattr(self._backend, "set_bandwidth_reserve",
                                      None)
                    if reserve is None:
                        raise RuntimeError(
                            f"the {self._backend_name} backend has no "
                            f"bandwidth-reserve control; remove "
                            f"gev_bandwidth_reserve_pct and "
                            f"gev_bandwidth_reserve_accum from the profile")
                    applied = reserve(cam, gev_bandwidth_reserve_pct,
                                      gev_bandwidth_reserve_accum)
                    print(f"[{self._cn(i)}] bandwidth reserve {applied}", flush=True)
            except Exception as e:
                # Never continue with a partial set: names are positional,
                # so a camera missing from the middle shifts every later
                # name and mislabels the recorded data.
                self.close_all()
                return self._open_failed(
                    f"Camera {dev.GetSerialNumber()} failed to open/configure:\n{e}\n\n"
                    "Power-cycle it (or close the app holding it) and reselect the profile.")

        self._camera_info = tuple(infos)
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
                print(f"[{self._cn(i)}] free-run config failed (camera offline?): {e}",
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
                self._backend.set_triggered(cam, limit,
                                            announce=(self._gi(i) == 0))
            except Exception as e:
                print(f"[{self._cn(i)}] trigger config failed: {type(e).__name__}: {e}",
                      flush=True)
                failed.append((i, f"{type(e).__name__}: {e}"))
        return failed

    def _start_grab_threads(self, raw_paths=None, display_every=1,
                            realtime=False, width=0, height=0, quality=21,
                            fps=100):
        self._stop_grab_threads()
        for i, cam in enumerate(self._cameras):
            rp = raw_paths[i] if raw_paths else None
            gt = GrabThread(self._gi(i), cam, self._backend, raw_path=rp,
                            display_every=display_every,
                            realtime=realtime, width=width, height=height,
                            quality=quality, fps=fps, router=self._router,
                            encoder_factory=self.encoder_factory)
            gt._pin_cpu = self.pin_capture_threads
            gt._pin_ecore = self.pin_encoder_threads
            gt._enc_pcores = self.encoder_pcores
            gt.set_source_down_check(self.source_down_check or self._source_down)
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
    #: Each open camera's index in the whole rig, in open order, or None
    #: when this manager holds the whole rig (the index is the position).
    #: A capture worker process opens a share of the cameras and sets it, so
    #: its grab threads, their capture-core slots and every camN in its log
    #: and warnings carry the rig-wide index.
    global_indices = None
    #: Builds the kick-out router, called with SyncEncodeRouter's arguments;
    #: None means SyncEncodeRouter. A capture worker installs the router
    #: that talks to the cross-process ledger.
    router_factory = None
    #: The stall ladder's check for a silent trigger source (GrabThread.
    #: set_source_down_check), or None for this manager's own view across
    #: its cameras (_source_down). A capture worker holds a share of the
    #: cameras, so it installs a check that reads the whole rig's silence.
    source_down_check = None
    #: Whether stop_acquisition adds the warning about every camera falling
    #: silent at once. A capture worker sees only its share of the cameras,
    #: so its parent reports it for the whole rig instead.
    report_source_silence = True
    #: Whether open_all prints the enumerated devices its serial list leaves
    #: out. A capture worker is handed its share of the rig as that list, so
    #: every other camera would be printed as unlisted.
    announce_unlisted = True

    def _gi(self, i: int) -> int:
        """Rig-wide index of open camera `i` (see global_indices)."""
        g = self.global_indices
        return i if g is None else int(g[i])

    def _cn(self, i: int) -> str:
        """The camN name of open camera `i`."""
        return f"cam{self._gi(i) + 1}"

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

    def _stop_grab_threads(self) -> set:
        """Stop every grab thread; return id() of each camera whose thread is
        still running (moved to the leak lists), which must not be closed."""
        threads = list(self._grab_threads)
        for gt in threads:
            gt.stop()
        self._wait_all(threads, STOP_NORMAL_EXIT_S)
        live = self._retain_live(threads)
        self._grab_threads.clear()
        return {id(threads[i]._camera) for i in live}

    def apply_exposure_gain(self, fps: float, exposure_us=None, gain_db=None,
                            collect: bool = True):
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

        EVERY camera's applied exposure and gain is logged, with the gain's
        UNIT beside the value: `Gain` is dB and `GainRaw` is model-specific
        sensor steps, so a bare number is not comparable between two cameras
        or against the profile. Anything that did not land - a control the
        camera does not implement, a value the node clamped - is appended to
        last_warnings. A calibration exposure left on one camera halves that
        camera's frame rate in the next recording, and until that is recorded
        per camera the only evidence is a rate-check warning after the
        session.

        collect=False logs everything and appends NOTHING to last_warnings.
        RULE: only the call that configures a RECORDING collects. REASON:
        stop_acquisition finalises last_warnings for the recording that just
        ended and the GUI builds WARNINGS.txt from it afterwards, so the
        preview restore that runs in between must not file its own problems
        against a session that is already over.

        Never raises for a bad profile: a frame rate at or above the limiter
        is refused by RigProfile.load, so it cannot reach this call inside a
        Qt slot. The one refusal is the backend's: a camera whose
        exposure_ceiling_us raises the backend's RefusalException cannot
        record at `fps`, and a collecting call raises
        AcquisitionStartRefused naming every such camera before any exposure
        is written. A call that does not collect (the preview restore) only
        prints the reason.
        """
        # Problems found here, published to last_warnings only when the
        # caller collects (see the docstring).
        found: list = []
        limit = float(getattr(self, "_trigger_rate_limit", 165.0) or 0.0)
        # RULE: with the limiter off the log says so instead of quoting a
        # default. _set_trigger_mode really did disable AcquisitionFrameRate,
        # so the only bound left is the trigger period itself, and a log line
        # quoting a limiter that is not running is worse than no line at all.
        limiter = (f"AcquisitionFrameRate={limit:g}" if limit > 0
                   else "limiter disabled")
        # The raw ceiling is the backend's (a camera's own physics; see
        # OptionalBackendMembers.exposure_ceiling_us), with the 10% margin
        # applied here for every backend alike.
        refused: list = []
        ceilings = [self._raw_ceiling_us(i, cam, fps, limit, found, refused)
                    for i, cam in enumerate(self._cameras)]
        if refused:
            # RULE: a camera that cannot record at this rate refuses the
            # start; the preview restore only prints it. REASON: a
            # recording made anyway skips triggers or drops frames, and a
            # WARNINGS.txt line reports that only after the session is lost.
            text = "\n".join(refused)
            print(f"[acq] cannot record at {fps:g} fps: {text}", flush=True)
            if collect:
                raise AcquisitionStartRefused(
                    f"These cameras cannot record at {fps:g} fps:\n{text}"
                    f"\n\nNothing was recorded.")
        ceilings = [None if c is None else c * 0.9 for c in ceilings]
        if any(c is not None and c <= 0 for c in ceilings):
            # fps >= limit: no exposure fits the trigger period at all.
            # RigProfile.load refuses that pairing, so reaching it means the
            # numbers were set some other way; clamping to a non-positive
            # ceiling would record at the sensor minimum and report success,
            # when the real fault is that the camera skips triggers.
            msg = (f"frame rate {fps:g} is at or above the trigger rate limit "
                   f"{limit:g}: the camera skips triggers at this rate and no "
                   f"exposure ceiling exists, so exposure is left as asked")
            print(f"[acq] WARNING: {msg}", flush=True)
            found.append(msg)
            ceilings = [None if c is not None and c <= 0 else c
                        for c in ceilings]
        for i, cam in enumerate(self._cameras):
            ceiling_us = ceilings[i]
            ceiling_txt = ("none" if ceiling_us is None
                           else f"{ceiling_us:.0f} us")
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
                if (isinstance(e, TypeError)
                        and not self._call_binds(self._backend.set_exposure_gain,
                                                 cam, want_exp, want_gain,
                                                 **unit)):
                    # The backend's method does not take these arguments, so
                    # the call failed before its body ran: nothing at all
                    # was written.
                    msg = (f"{self._cn(i)}: the exposure/gain write was refused "
                           f"before anything was applied ({type(e).__name__}: "
                           f"{e}): the {self._backend_name} backend's "
                           f"set_exposure_gain does not take the arguments "
                           f"the camera manager passes, so this camera "
                           f"records at whatever the previous acquisition "
                           f"left")
                else:
                    # RULE: the message states what is UNKNOWN, not that
                    # nothing was written. REASON: the backend applies the
                    # exposure before it validates the gain's unit, so the
                    # commonest way this raises - a profile gain in dB on a
                    # GainRaw camera - leaves the exposure APPLIED; telling
                    # the operator it was not sends them looking for a fault
                    # in the wrong place.
                    msg = (f"{self._cn(i)}: the exposure/gain write failed part-way "
                           f"({type(e).__name__}: {e}); the exposure may have "
                           f"been applied and the gain not, so this camera may "
                           f"be recording at whatever the previous acquisition "
                           f"left")
                print(f"[{self._cn(i)}] exposure/gain set failed: {e}", flush=True)
                found.append(msg)
                continue
            # Every camera, every time (mandate M7): a value that lands on
            # cam1 and not on cam5 is invisible otherwise, and cam5 then
            # records at the wrong exposure with nothing in the log.
            exp_txt = "None" if exp is None else f"{exp:.0f}"
            # RULE: the gain is logged with the unit the camera's node uses.
            # REASON: dB and raw sensor steps print as the same bare number,
            # so a baseline restore on a GainRaw camera is otherwise
            # indistinguishable in the log from 6 dB on a Gain camera, and
            # the log is what a session's settings are read back from.
            gain_txt = ("None" if gain is None
                        else f"{gain:.1f} {self._gain_unit(cam)}".rstrip())
            print(f"[{self._cn(i)}] exposure={exp_txt} us gain={gain_txt} "
                  f"(ceiling {ceiling_txt} at {fps:g} fps, {limiter}){note}",
                  flush=True)
            found.extend(
                self._exposure_gain_warnings(self._gi(i), want_exp, want_gain,
                                             exp, gain))
        if collect:
            self.last_warnings.extend(found)

    def _backend_open_kwargs(self, **values) -> dict:
        """The keywords of `values` that are not None and that the backend's
        open() takes by name, read with inspect.signature. A backend that
        takes **kwargs gets all of them. A backend written before a keyword
        existed does not take it and is called without it, and one whose
        signature cannot be read gets none of them."""
        try:
            params = inspect.signature(self._backend.open).parameters
        except (TypeError, ValueError):
            return {}
        any_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD
                     for p in params.values())
        return {k: v for k, v in values.items()
                if v is not None and (any_kw or (
                    k in params and params[k].kind in (
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        inspect.Parameter.KEYWORD_ONLY)))}

    @staticmethod
    def _call_binds(fn, *args, **kwargs) -> bool:
        """Whether fn's signature accepts these arguments. True when the
        signature cannot be read, so an unknown case is never reported as
        'nothing was applied'."""
        try:
            inspect.signature(fn).bind(*args, **kwargs)
        except TypeError:
            return False
        except ValueError:
            return True
        return True

    def _raw_ceiling_us(self, i: int, cam, fps, limit: float, found: list,
                        refused: list):
        """Camera i's exposure ceiling in us before the 0.9 margin.

        From the backend's exposure_ceiling_us when it has one; otherwise the
        limiter formula (in trigger mode the frame-rate timer starts after
        exposure ends, so the interval is exposure + 1/limit), which is the
        same number the Basler backend returns. A backend whose read fails
        leaves the trigger period as the only bound, and says so in `found`.
        The exception is the backend's RefusalException: the camera cannot
        record at `fps`, and "camN: reason" goes to `refused` instead, for
        apply_exposure_gain to refuse the start on.
        """
        fps = float(fps)
        fn = getattr(self._backend, "exposure_ceiling_us", None)
        if fn is None:
            if limit > 0:
                return 1e6 / fps - 1e6 / float(limit)
            return 1e6 / fps
        try:
            return float(fn(cam, fps, limit))
        except Exception as e:
            refusal = getattr(self._backend, "RefusalException", None)
            if (isinstance(refusal, type) and issubclass(refusal, Exception)
                    and isinstance(e, refusal)):
                refused.append(f"{self._cn(i)}: {e}")
                return 1e6 / fps
            msg = (f"{self._cn(i)}: the backend could not report its exposure "
                   f"ceiling ({type(e).__name__}: {e}), so exposure is "
                   f"bounded by the trigger period alone")
            print(f"[acq] WARNING: {msg}", flush=True)
            found.append(msg)
            return 1e6 / fps

    def _gain_unit(self, cam) -> str:
        """The backend's name for this camera's gain unit ('dB', 'raw'), or
        '' when there is none to report.

        RULE: reading the unit never raises and never fails an acquisition.
        REASON: it is log text; a backend that does not implement the call,
        or a camera that dropped off the bus between the write and the log
        line, must not turn a successful exposure write into a refused start.
        """
        fn = getattr(self._backend, "gain_unit", None)
        if fn is None:
            return ""
        try:
            return fn(cam) or ""
        except Exception:
            return ""

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

    def geometry_mismatch(self, width, height) -> str | None:
        """Why recording at (width, height) would not match the open cameras,
        or None when it would - including when no camera is open, or the
        caller states no size.

        RULE: the geometry check exists as a PREDICATE, so a preflight can
        refuse before the session directory is touched. REASON: those two
        numbers size the NV12 ring and the raw decode, so a disagreement
        retires every camera in real-time mode and shears a full-length
        recording in raw mode; refusing is right, but start_acquisition can
        only refuse by raising, and by then the previous run's artifacts in
        the target directory have already been deleted and the exception
        reaches the operator as a traceback rather than a dialog. A caller
        that preflights calls this first.
        """
        if not (self._geometry and width and height):
            return None
        if (int(width), int(height)) == self._geometry:
            return None
        return (f"The profile records {int(width)}x{int(height)} but the "
                f"cameras are configured for {self._geometry[0]}x"
                f"{self._geometry[1]}: fix {self._settings_source()} (or the "
                f"profile) so they agree.")

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
        # This raise is the BACKSTOP, not the intended report: a caller that
        # runs geometry_mismatch() in its preflight refuses cleanly, while
        # reaching here means the refusal arrives after the target directory
        # has been swept and as a traceback. Loud and late still beats
        # recording a sheared session, so the check stays until a preflight
        # calls the predicate.
        mismatch = self.geometry_mismatch(width, height)
        if mismatch:
            raise AcquisitionStartRefused(
                f"{mismatch}\n\nNothing was recorded and the cameras are "
                f"still in preview.")
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
        # RULE: the previous session's stream statistics are dropped here,
        # with its warnings. REASON: stop_acquisition can raise before it
        # refills them (a router that throws) and a session can end through
        # abandon() instead, while the metadata writer persists this list
        # as-is - so a stale entry records one session's network state as
        # another's with nothing marking it stale.
        self.last_stream_stats = []
        self.last_retired = {}
        self.last_encoder_failures = []
        self._board_started_t = None
        if realtime and realtime_kick:
            # Shared router gates frames through the cross-camera coordinator so
            # only frames every camera captured get encoded (already aligned, no
            # post-hoc re-encode). The profile asked for kick-out, so anything
            # less is refused: the decoupled fallback would try one encoder per
            # grab thread against the same exhausted session cap and end in
            # raw.bin, width x height bytes per frame per camera, unaligned,
            # with the capacity preflight having budgeted for H.264.
            make_router = self.router_factory
            if make_router is None:
                from gui_app.sync_encode import SyncEncodeRouter
                make_router = SyncEncodeRouter
            router = make_router(raw_paths, width, height, quality,
                                 fps=fps, max_lag=kick_max_lag,
                                 pin_encoders=self.pin_encoder_threads,
                                 enc_pcores=self.encoder_pcores,
                                 encoder_factory=self.encoder_factory,
                                 rate_hints={"source_hint": source_name(
                                     self.trigger_source)})
            if not router.available:
                self._start_grab_threads()      # back to preview
                # Reported like a mid-session encoder failure: the cached
                # NVENC session count the preflight passed on is suspect.
                self.last_encoder_failures = [
                    f"kick-out encoders: {router.unavailable_reason}"]
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
            names = ", ".join(f"{self._cn(i)} ({err})" for i, err in failed)
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
        try:
            self.apply_exposure_gain(fps, exposure_us, gain_db)
        except AcquisitionStartRefused:
            # A camera cannot record at this rate (the backend's
            # RefusalException). Undone like a trigger-mode failure, so the
            # cameras are back in preview when the refusal reaches the caller.
            if self._router is not None:
                try:
                    self._router.abandon()
                except Exception:
                    pass
                self._router = None
            self._set_freerun_mode()
            self._start_grab_threads()
            raise
        self._start_grab_threads(raw_paths=raw_paths, display_every=display_every,
                                 realtime=realtime, width=width, height=height, quality=quality,
                                 fps=fps)

    def wait_until_ready(self, timeout_s: float = 30.0) -> tuple[int, int]:
        """Block until every grab thread has its ring and its stream, or time out.

        Returns (ready, total). The caller starts the trigger board only after
        this, because starting it earlier is what produced the startup backlog:
        each thread fills its NV12 ring (ring_slots() frames of width x height
        x 1.5 bytes) before it arms, and frames delivered during that
        allocation queue in the driver. The grab loop
        retrieves at exactly the arrival rate, so a backlog created here is
        never recovered -- a camera that starts 124 frames behind rides kick_max_lag
        for the whole session and force-drops thousands of frames for everyone.

        Returning within the bound is not the same as every camera being
        armed. A thread that fails to arm sets its event anyway (and retires
        itself), so it never holds the barrier; a thread still allocating or
        inside StartGrabbing when the bound expires is simply not ready, and
        not_ready() names it. RULE: the caller refuses the start when
        not_ready() is not empty. REASON: a camera that arms after the board
        starts counts its block IDs from a later trigger than the others,
        which misaligns it by a constant offset with no gap and a clean rate
        check. mark_board_starting() makes such a camera retire itself, as a
        second line of defence.
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

    def not_ready(self) -> list[int]:
        """Zero-based indices of the grab threads that have not armed their
        stream yet (see wait_until_ready)."""
        return [i for i, gt in enumerate(self._grab_threads)
                if not getattr(getattr(gt, "ready", None), "is_set", bool)()]

    def mark_board_starting(self) -> None:
        """Call immediately before telling the trigger board to start.

        From here, a grab thread whose StartGrabbing returns retires itself
        ("armed after the trigger board started") instead of recording
        under block IDs offset from every other camera's. It also closes the
        pre-barrier window of frames_before_barrier(), and starts the silence
        clock of seconds_since_frame(). With an external TTL source, call it
        once every camera is ready and before the source is started.
        """
        self._board_started_t = time.perf_counter()
        for gt in self._grab_threads:
            mark = getattr(gt, "mark_board_starting", None)
            if mark is not None:
                mark()

    def frames_before_barrier(self) -> dict:
        """{"camN": frames retrieved in trigger mode before the barrier}.

        The barrier is mark_board_starting() (or signal_triggers_started(),
        whichever came first). Every camera is armed before the board
        starts, so a nonzero count on the board path is a trigger nobody
        sent through the board, and with an external TTL source it is a
        pulse that reached this camera before every camera was armed. Either
        way the cameras' block-ID origins differ, and the caller refuses the
        recording.
        """
        return {self._cn(i): int(getattr(gt, "frames_before_barrier", 0))
                for i, gt in enumerate(self._grab_threads)}

    @staticmethod
    def _silence(gt, now: float) -> float:
        fn = getattr(gt, "seconds_since_frame", None)
        return 0.0 if fn is None else fn(now)

    def seconds_since_frame(self) -> list[float]:
        """Per camera, seconds since its last result (good or failed).

        0.0 before the trigger source was marked started, because silence
        before the board starts is expected; after that the clock runs from
        the mark until the first frame.
        """
        now = time.perf_counter()
        return [self._silence(gt, now) for gt in self._grab_threads]

    @staticmethod
    def _is_active(gt) -> bool:
        """A grab thread that is still receiving: running, its retrieve loop
        not finished, and not retired."""
        running = getattr(gt, "isRunning", None)
        return (not getattr(gt, "desynced", False)
                and not getattr(gt, "retrieve_loop_exited", False)
                and (running is None or running()))

    def source_silent(self, threshold_s: float = SOURCE_SILENT_S) -> bool:
        """True when every active camera has been silent for more than
        `threshold_s`.

        Cameras stop together only when what they share stops: the trigger
        board (reset, unplugged, or powered down, which on the reference rig
        also leaves the laser input floating) or the network to all of
        them. The main window polls this during a recording and raises the
        alarm; False when no camera is active, because a session whose
        cameras were all retired is a different report.
        """
        now = time.perf_counter()
        active = [gt for gt in list(self._grab_threads) if self._is_active(gt)]
        return bool(active) and all(self._silence(gt, now) > threshold_s
                                    for gt in active)

    def _source_down(self) -> bool:
        """The stall ladder's cross-camera check (GrabThread.
        set_source_down_check): every active camera silent for more than
        SOURCE_SILENT_S, with at least two of them.

        One camera alone cannot tell its own stall from the source's, so a
        single active camera keeps its per-camera ladder.
        """
        now = time.perf_counter()
        active = [gt for gt in list(self._grab_threads) if self._is_active(gt)]
        return len(active) >= 2 and all(
            self._silence(gt, now) > SOURCE_SILENT_S for gt in active)

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
            if self.trigger_source == "external":
                msg = (f"{self._cn(i)}: its grab thread was still receiving "
                       f"frames {STOP_NORMAL_EXIT_S:.0f} s after the recording "
                       f"was stopped, so the external trigger source was "
                       f"still running, or the camera was not in trigger "
                       f"mode. Panopticon stopped the thread, so this "
                       f"camera's recording ends on a different pulse from "
                       f"the others'.")
            else:
                msg = (f"{self._cn(i)}: its grab thread was still receiving frames "
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
                print(f"[{self._cn(i)}] grab thread still draining at stop, waiting...", flush=True)
        self._wait_all(draining, STOP_DRAIN_S)

        retired = {}
        encoder_failures = []
        if self._router is not None:
            # Kick-out mode: grab threads have stopped submitting; flush the
            # coordinator and drain the shared encoders. Metadata (the released,
            # already-common frames) comes from the router, not the grab threads.
            results = self._router.stop()
            # Read the warnings BEFORE dropping the router, or they are lost
            # with it, and a truncated or retired camera then reaches the
            # operator only as a line on stdout.
            warnings.extend(self._router.warnings)
            for cam, reason in self._router.retired_reasons:
                retired.setdefault(f"cam{cam + 1}", reason)
            encoder_failures.extend(self._router.encoder_failures)
            self._router = None
            # A grab thread's own findings in this mode are the ones only it
            # can see (failed-grab rates); its recording is the router's.
            for gt in threads:
                warnings.extend(getattr(gt, "warnings", []))
        else:
            results = [(gt.frame_count, gt.timestamps, gt.block_ids)
                       for gt in self._grab_threads]
            # Decoupled/raw modes: each thread reconciled its own bookkeeping
            # (truncation, failed resync, spilled tail) and carries the result.
            for gt in self._grab_threads:
                warnings.extend(gt.warnings)
        for i, gt in enumerate(threads):
            reason = getattr(gt, "retired_reason", None)
            if reason:
                retired.setdefault(self._cn(i), reason)
            failure = getattr(gt, "encoder_failure", None)
            if failure:
                encoder_failures.append(f"{self._cn(i)}: {failure}")
        if self.report_source_silence:
            warnings.extend(self._source_silence_warnings(threads))
        running = {id(gt._camera) for gt in threads if gt.isRunning()}
        warnings.extend(self._camera_acquisition_warnings(threads, running))
        self.last_warnings = warnings
        self.last_results = results
        self.last_retired = retired
        self.last_encoder_failures = encoder_failures
        self.last_stream_stats = self._collect_stream_stats(
            skip=running, threads=threads)

        stuck = [i for i, gt in enumerate(threads) if gt.isRunning()]
        if stuck:
            # Never hand the cameras back to preview under a live grab thread:
            # set_freerun() calls StopGrabbing() on the same handle the thread
            # is inside RetrieveResult on, which is concurrent native access.
            # The thread list is kept so abandon() can still find them. The
            # results travel on the exception: the streams are on disk, but
            # blockids.npy / frametimes.npy are the caller's to write, and
            # without them the healthy cameras' streams cannot be aligned.
            names = ", ".join(self._cn(i) for i in stuck)
            raise AcquisitionStopIncomplete(
                f"grab thread(s) for {names} did not exit after the stop "
                f"timeouts (GPU or driver wedged?). The cameras were NOT "
                f"reconfigured; restart the application before recording "
                f"again. The captured streams are on disk, but blockids.npy "
                f"and frametimes.npy have NOT been written yet: save "
                f"CameraManager.last_results before abandoning.",
                results, stuck)
        self._grab_threads.clear()
        # RULE: collect every generation once the acquisition is over.
        # REASON: whatever a recording leaves in a reference cycle stays
        # allocated until the collector's oldest generation runs, which in a
        # quiet GUI can be never; the grab threads and the sinks drop their
        # rings explicitly, and this frees the rest. Every grab thread has
        # exited by here, so the pause costs no capture time.
        gc.collect()
        return results

    #: What a post-stop read of backend.stream_stats() may be believed for.
    #: RULE: only the CAMERA-side transport settings are kept here; every
    #: stream-grabber counter is dropped. REASON: this read happens after
    #: every grab thread has exited, and a grab thread's last act is
    #: StopGrabbing(), which resets those counters - so Failed_Buffer_Count,
    #: Buffer_Underrun_Count and the resend counts all read 0 whatever the
    #: session did, and a persisted 0 reads as "no network loss", the
    #: opposite of the truth in exactly the sessions they are collected to
    #: diagnose. Each grab thread logs its own counters before it stops
    #: grabbing; persisting them belongs to whoever can stash them there.
    STREAM_COUNTERS_NOTE = ("not read here: the grab threads have already "
                            "stopped grabbing, which resets the stream "
                            "counters; each thread logs its own at stop")

    #: What `counters` says for an entry whose grab thread read the stream
    #: counters before StopGrabbing; the values are under `before_stop`.
    STREAM_COUNTERS_BEFORE_STOP = ("read by the grab thread before it "
                                   "stopped grabbing; see before_stop")

    def _collect_stream_stats(self, skip=frozenset(), threads=()) -> list:
        """The camera-side transport settings per camera, one dict each.

        A camera whose grab thread is still running is skipped, and a backend
        without the call or a camera that dropped off the bus contributes an
        error entry: this is evidence for the metadata, never a reason to
        fail a stop or to read a node map from two threads at once.

        Only the keys the backend declares as camera-side (TRANSPORT_NODES)
        survive the read taken here - see STREAM_COUNTERS_NOTE for why the
        counters do not. The counters the camera's own grab thread read just
        before StopGrabbing are folded in under `before_stop` instead, when
        it read them, and `counters` says which of the two applies. Every
        entry carries the camera's serial, so the metadata names the
        physical camera and not only its position.
        """
        fn = getattr(self._backend, "stream_stats", None)
        keep = tuple(getattr(self._backend, "TRANSPORT_NODES", ()))
        before = {id(gt._camera): getattr(gt, "stream_stats_at_stop", None)
                  for gt in threads}
        out = []
        for i, cam in enumerate(self._cameras):
            if id(cam) in skip:
                entry = {"error": "grab thread still running"}
            elif fn is None:
                entry = {"error": "backend reports no stream statistics"}
            else:
                try:
                    entry = {k: v for k, v in dict(fn(cam)).items()
                             if k in keep or k == "error"}
                except Exception as e:
                    entry = {"error": f"{type(e).__name__}: {e}"}
            counters = before.get(id(cam))
            if counters is not None and id(cam) not in skip:
                entry["before_stop"] = dict(counters)
                entry["counters"] = self.STREAM_COUNTERS_BEFORE_STOP
            else:
                entry["counters"] = self.STREAM_COUNTERS_NOTE
            if i < len(self._camera_info):
                entry["serial"] = self._camera_info[i]["serial"]
            out.append(entry)
        return out

    def _camera_acquisition_warnings(self, threads, running) -> list:
        """What the backend alone can witness about the recording that just
        ended (OptionalBackendMembers.acquisition_warnings), one sentence per
        problem, prefixed with the camera's name and serial.

        Asked only of cameras whose grab thread has exited, because the call
        reads the camera's node map. Never raises.
        """
        fn = getattr(self._backend, "acquisition_warnings", None)
        if fn is None:
            return []
        frames = {id(gt._camera): int(getattr(gt, "frames_retrieved", 0))
                  for gt in threads}
        out = []
        for i, cam in enumerate(self._cameras):
            if id(cam) in running or id(cam) not in frames:
                continue
            try:
                found = list(fn(cam, frames[id(cam)]) or [])
            except Exception as e:
                print(f"[{self._cn(i)}] acquisition_warnings failed: "
                      f"{type(e).__name__}: {e}", flush=True)
                continue
            serial = (self._camera_info[i]["serial"]
                      if i < len(self._camera_info) else "?")
            for sentence in found:
                msg = f"{self._cn(i)} ({serial}): {sentence}"
                print(f"[acq] WARNING: {msg}", flush=True)
                out.append(msg)
        return out

    def _source_silence_warnings(self, threads) -> list:
        """One session warning when the stall ladders waited because every
        active camera went silent at once, and whether they re-armed after
        the wait (grab_thread.SOURCE_DOWN_WAIT_WINDOWS)."""
        stalled = [(i, gt) for i, gt in enumerate(threads)
                   if getattr(gt, "source_down_stalls", 0)
                   or getattr(gt, "source_down_rearms", 0)]
        if not stalled:
            return []
        names = ", ".join(self._cn(i) for i, _gt in stalled)
        # The source the operator has to check: an external source is not
        # the board, and it has no USB cable or stimulation pins of ours.
        # Read with getattr: ProcessCameraManager borrows this method.
        kind = getattr(self, "trigger_source", "board")
        external = kind == "external"
        source = source_name(kind)
        if not any(getattr(gt, "frames_retrieved", 0)
                   or getattr(gt, "frame_count", 0)
                   or getattr(gt, "failed_grabs", 0) for gt in threads):
            # No camera delivered a single result, good or failed: the
            # triggers never reached any camera, which a source that stops
            # mid-run cannot explain.
            started = ("the recording started" if external
                       else f"{source} started")
            msg = (f"No camera received a frame after {started} ({names}), "
                   f"so nothing was recorded, and no camera was retired for "
                   f"it. Check that {source} is running and wired to every "
                   f"camera, and that each camera's trigger input (its "
                   f"trigger line and source settings) matches the line it "
                   f"drives.")
            print(f"[acq] WARNING: {msg}", flush=True)
            return [msg]
        start = self._board_started_t
        firsts = [gt.source_down_since for _i, gt in stalled
                  if getattr(gt, "source_down_since", None) is not None]
        when = ""
        if start is not None and firsts:
            when = (f" about {max(0.0, min(firsts) - start):.0f} s into the "
                    f"recording")
        rearmed = [(i, gt) for i, gt in stalled
                   if getattr(gt, "source_down_rearms", 0)]
        if rearmed:
            waited = [gt.source_down_rearm_t - gt.source_down_since
                      for _i, gt in rearmed
                      if getattr(gt, "source_down_rearm_t", None) is not None
                      and getattr(gt, "source_down_since", None) is not None]
            wait = (f" about {max(0.0, min(waited)):.0f} s" if waited
                    else "")
            handled = (f". Each camera waited{wait} for frames to resume, "
                       f"then re-armed its stream, which clears a stall of "
                       f"the network the cameras share ("
                       + ", ".join(self._cn(i) for i, _gt in rearmed) + ").")
        else:
            handled = ", so no camera was re-armed or retired for it."
        if external:
            cause = f"{source} stopped"
            check = f"Check {source} and its cables."
        else:
            cause = f"{source} reset, or lost USB or power"
            check = (f"Check {source} and its USB cable. A board that lost "
                     f"power leaves its output pins undriven, stimulation "
                     f"pins included.")
        msg = (f"Every active camera stopped receiving frames at the same "
               f"time{when} ({names}). A silence shared by every camera "
               f"comes from the trigger source ({cause}) or from the network "
               f"to all of them{handled} Triggers during the silence are "
               f"missing from every camera. {check}")
        print(f"[acq] WARNING: {msg}", flush=True)
        return [msg]

    def resume_preview(self, preview_fps: float = 30.0):
        """Return all cameras to free-run preview after an acquisition. Resilient
        to a camera that went offline mid-session (it is skipped, not fatal).

        Restores the exposure and gain read at open (the .pfs on Basler).
        Without this the preview keeps whatever the last acquisition set, so
        after a calibration it sits at calibration_exposure_us, typically
        longer than the recording exposure. Free run at 30 fps has the
        headroom, so nothing breaks; it just looks brighter than what a
        recording will capture, which is exactly the misreading the "judge
        exposure from a recording, not the preview" rule exists to prevent.
        Passing None restores the baseline read at open.

        Refuses while any grab thread is still running: reconfiguring a camera
        under a thread inside RetrieveResult is concurrent native access.
        """
        live = [i for i, gt in enumerate(self._grab_threads) if gt.isRunning()]
        if live:
            raise RuntimeError(
                "resume_preview() called while grab thread(s) "
                + ", ".join(self._cn(i) for i in live)
                + " are still running; the cameras were not reconfigured")
        self._set_freerun_mode()
        # RULE: the preview restore collects no warnings. REASON: it runs
        # AFTER stop_acquisition finalised last_warnings for the recording
        # just finished and before the GUI reads that list, so a camera that
        # dropped off the bus mid-session - the case this method is written
        # for - would otherwise file its preview-restore failure in that
        # recording's WARNINGS.txt as a capture problem.
        self.apply_exposure_gain(preview_fps, None, None, collect=False)
        self._start_grab_threads()

    def cancel_acquisition(self, preview_fps: float = 30.0) -> None:
        """Undo a start_acquisition that returned but must not record, and
        put the cameras back in free-run preview.

        For a start that succeeded here while another part of the rig
        refused (a capture worker whose peer could not arm): the recording
        grab threads are abandoned, so they skip their drain and record no
        retirement, the kick-out router drops its encoders and removes its
        empty streams, and the cameras return to preview with their open
        exposure and gain. Nothing was triggered, so nothing is lost.

        Raises RuntimeError, as resume_preview does, when a grab thread is
        still running after the wait: reconfiguring a camera under it would
        be concurrent native access.
        """
        threads = self._grab_threads
        for gt in threads:
            gt.abandon()
        self._wait_all(threads, ABANDON_THREAD_S)
        live = self._retain_live(threads)
        self._grab_threads = [threads[i] for i in live]
        if self._router is not None:
            try:
                self._router.abandon()
            except Exception as e:
                print(f"[acq] abandoning the router failed: {e}", flush=True)
            self._router = None
        self.resume_preview(preview_fps)

    def close_all(self):
        """Stop the grab threads and close every camera.

        RULE: a camera whose grab thread is still running is not closed.
        REASON: that thread is wedged inside a native call on the handle,
        and closing it from here is concurrent native access, which can
        crash the process; the handle is kept referenced instead
        (_leaked_cameras), exactly as abandon() does.
        """
        live = self._stop_grab_threads() or set()
        for cam in self._cameras:
            if id(cam) in live:
                print("[cam] grab thread still running at close; leaking its "
                      "camera handle rather than closing under a live native "
                      "call", flush=True)
                continue
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
            print(f"[{self._cn(i)}] grab thread still running at abandon; leaking "
                  f"its camera handle rather than closing under a live native "
                  f"call", flush=True)
        self._grab_threads = []
        if self._router is not None:
            try:
                self._router.abandon()
            except Exception:
                pass
            self._router = None
        # Dropped with the camera set: an abandoned session's statistics
        # describe a recording that was never finalised, and the next
        # session's metadata must not inherit them.
        self.last_stream_stats = []
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
