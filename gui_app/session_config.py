"""Session configuration and path management."""
import json
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
PROFILES_DIR = REPO_ROOT / "profiles"


@dataclass
class RigProfile:
    name: str = "default"
    frame_width: int = 1920
    frame_height: int = 1200
    frame_rate: int = 100
    calibration_frame_rate: int = 30
    quality: int = 21
    encode_parallel: int = 3
    realtime_encode: bool = True
    # Real-time frame kick-out: gate frames through the cross-camera coordinator
    # during capture so only frames every camera caught get encoded — videos come
    # out already trigger-aligned, no post-hoc re-encode. BOTH shipped profiles
    # set this true; the field default stays False so a profile written before
    # the field existed keeps the post-hoc alignment path (gui_app/alignment.py).
    realtime_kick: bool = False
    # Kick-out coordinator buffer depth (frames). A camera may lag the others by
    # this many frames before its missing triggers are force-dropped to keep the
    # pipeline flowing. Higher = fewer late frames sacrificed, but more RAM held
    # (the NV12 ring is max_lag + 264 buffers per camera). Observed cross-camera
    # lag is 0-2 frames since the grab loop stopped copying with the GIL held, so
    # the headroom the 3dpose profile carries is precautionary — see the note
    # there before changing it either way.
    kick_max_lag: int = 240
    # GigE receive driver: "socket" (user-space, robust packet resends — the
    # proven path), "filter" (in-kernel pylon GigE Vision driver, less CPU but
    # measured 2026-06-12 silently dropping ~23% of frames with default resend
    # settings), or "auto" (leave pylon's default).
    gige_driver: str = "socket"
    pfs_path: str = ""
    output_dir: str = ""
    board_config: str = ""
    serial_port: str = "COM3"
    trigger_pins: list = field(default_factory=lambda: [2, 4, 6, 8, 10, 12])
    # Expected camera count. 0 = don't check. Nonzero makes open_all refuse a
    # partial set: names are positional by serial order, so a camera that fails
    # to ENUMERATE renames every camera after it and silently attaches the
    # calibration extrinsics to the wrong physical cameras.
    n_cameras: int = 0
    # Driver-side buffers queued per camera. THE LARGEST SINGLE RAM CONSUMER:
    # n_cams x max_num_buffer x width x height bytes, so 1000 buffers is 20.7 GiB
    # at nine 1920x1200 cameras, before the NV12 ring is counted at all. It was
    # hardcoded at 1000 in camera_manager until 2026-09-10, which made the
    # capacity preflight's own advice ("Lower MaxNumBuffer or kick_max_lag")
    # impossible to follow.
    #
    # Deep slack absorbs genuine GigE jitter, and it is also what let a 1.5%
    # per-frame deficit hide for ~11 minutes before anything went wrong: nothing
    # errors, the pool quietly fills, and every frame retrieved gets staler. So
    # lower it for RAM, not for speed, and read Buffer_Underrun_Count afterwards
    # — nonzero means the pool ran dry, i.e. the host could not keep up.
    max_num_buffer: int = 1000
    # --- Calibration coverage HUD: when has enough board been captured? -------
    # These decide how long someone stands in the arena waving, so they are
    # worth setting against what the SOLVE consumes rather than by feel.
    # 1_calibrate.py caps intrinsics at 60 pose-diverse frames per camera and
    # stereo at 30 shared frames per pair; everything beyond those caps is
    # discarded, contributing only a slightly richer pool to sample from. The
    # defaults therefore sit at roughly 2x and 1.3x the caps, which is margin,
    # not stinginess. They were 250/80 until 2026-09-10, i.e. ~4x and ~2.7x the
    # caps, which made a 9-camera calibration take far longer than the data
    # could be used for. Raise them if calibrations come out marginal; the
    # per-pair chart in reprojection_error_histogram.png is the evidence.
    calibration_min_per_cam_shared: int = 120
    calibration_min_edge: int = 40
    # Quadrants of its own field of view each camera must see the board in.
    # This is the criterion that actually prevents degenerate intrinsics from
    # waving the board in one spot, and it is cheap to satisfy, so it should be
    # the LAST thing relaxed.
    calibration_min_grid_cells: int = 3
    # Pin each grab thread to a performance core and raise its priority.
    # On a hybrid CPU (P-cores + E-cores) the scheduler must place most of our
    # ~19 busy threads on E-cores at nine cameras, and picks differently each
    # launch — which is the shape of the rotating laggard. See cpu_affinity.py.
    # No effect on a non-hybrid CPU or off Windows.
    pin_capture_threads: bool = False
    # Seconds between temperature polls while acquiring; 0 disables the check.
    # These cameras have no fan and cool by conduction through the mount, so
    # temperature is a property of the INSTALLATION: on the reference rig four
    # of nine cameras sit above the vendor's Critical threshold and one peaked
    # 1 C below thermal shutdown, while three others never pass 73 C. A camera
    # that reaches shutdown stops delivering mid-session, and until now the GUI
    # read temperatures only AFTER the recording -- too late to act on. The
    # thresholds are never hardwired here: every Basler camera reports its own
    # BslTemperatureStatus, BsliCriticalTemperature and BsliOverTemperature, so
    # this works on any model. A GVCP register read is a cold path, hence the
    # slow default rather than the preview timer.
    # Confine ENCODER threads to the P-core set -- not one per core, and not
    # the E-cores. Distinct from pin_encoder_threads above, which pins one
    # encoder per E-core and measured catastrophic (worst lag 321).
    #
    # Leaving encoders unpinned is not neutral: Windows is then free to place
    # one on an E-core, and _EncoderThread.run's own note is that a single
    # E-core cannot sustain encode submission for one 1920x1200 stream at
    # 100 fps. Measured 2026-09-14 in one GUI process running
    # recording -> calibration -> solve -> recording: the first recording held
    # every camera at 0 with qsize 0-1, and the second, with identical
    # grab-thread affinity, filled the encode queue (qsize 183-204 against
    # ENCODE_QUEUE_DEPTH 200) and ran 3-6x slower on every operation as the
    # grab threads blocked on ring slots the coordinator could not release.
    # Placement of unpinned encoders is a fresh lottery each acquisition, which
    # is why one recording passes and the next does not.
    encoder_pcores: bool = False
    thermal_poll_s: float = 20.0
    # Logical CPUs that capture threads are kept OFF, when pinning is enabled.
    # CPU 0 is the Windows boot processor and the default target for timer and
    # DPC work, so a grab thread pinned there is descheduled by exactly the
    # network traffic it is trying to receive. Measured 2026-09-14, nine
    # cameras, 90 s, pinned, lag behind leader as median/p95/max -- the victim
    # followed the CORE, not the camera:
    #   exclude nothing (cam1 on CPU 0)   cam1 0/6/12, others 0/1/1
    #   exclude [0]     (cam1 on CPU 1)   cam1 0/3/4,  others 0/1/1
    #   exclude [0, 1]                    ALL NINE 0/1/1
    # Under the GUI the unexcluded case was far worse than headless: the same
    # camera diverged to kick_max_lag (480) and was force-dropped.
    capture_core_exclude: list = field(default_factory=lambda: [0])
    # Confine ENCODER threads to the E-core set. Separate from the above, and
    # default OFF because it MEASURED WORSE. 2026-09-11, nine cameras, grab
    # threads pinned in every arm:
    #   encoders unpinned          avg_proc 2.19 ms  slack 7.03  worst lag 10
    #   encoders on the E-core set          2.53        6.62               10
    #   encoders one per E-core             3.48        5.52              321
    # A single E-core cannot sustain encode submission for one 1920x1200
    # stream at 100 fps, so that camera backs up and drags its grab thread
    # with it. Kept as a knob for a rig with more cameras than P-cores.
    pin_encoder_threads: bool = False
    # Optostim output pins held LOW from the instant the sketch boots — before
    # the serial handshake, which blocks until the GUI connects. Without this a
    # powered laser driver reads the floating pin as ON at power-up. Pins used by
    # the stimulation workflow are added automatically; list here anything that
    # must be safe even when no paradigm is loaded.
    stim_safe_pins: list = field(default_factory=lambda: [53])
    # Calibration-only exposure/gain. The ChArUco board often needs far more
    # light than the experiment does -- especially when the room is dimmed to
    # keep a wireless optostim receiver from triggering. Calibration can afford
    # it: in trigger mode the minimum interval is
    # `exposure + 1/AcquisitionFrameRate`, so at 100 fps exposure is capped near
    # 3.94 ms, but at the 30 fps calibration rate the ceiling is ~27 ms.
    # camera_manager.apply_exposure_gain() enforces 90% of that ceiling, so the
    # values that actually survive are ~3.55 ms and ~24.5 ms — a larger request
    # is clamped, with a `CLAMPED from ...` log line. These
    # are applied for calibration only and the .pfs values are restored for
    # recording, so a long calibration exposure can never leak into a 100 fps
    # session (where it would silently halve the frame rate).
    # 0 / -1 mean "leave the .pfs value alone".
    calibration_exposure_us: float = 0.0
    calibration_gain_db: float = -1.0
    # AcquisitionFrameRate applied in trigger mode, or 0 to disable the limiter.
    # While externally triggered the camera's internal rate generator serves no
    # purpose, but it still enforces a minimum interval of
    # `exposure + 1/AcquisitionFrameRate` — the thing that capped exposure at
    # ~3.94 ms at 100 fps, and (at the old value of 100) caused the 50 fps bug.
    # 0 leaves only the sensor readout as the constraint.
    trigger_rate_limit: float = 165.0

    @classmethod
    def load(cls, path: Path) -> "RigProfile":
        with open(path) as f:
            data = yaml.safe_load(f)
        def _resolve(raw: str) -> str:
            if not raw:
                return ""
            p = Path(raw)
            if not p.is_absolute():
                p = REPO_ROOT / p
            return str(p)

        return cls(
            name=data.get("name", path.stem),
            frame_width=data.get("frame_width", 1920),
            frame_height=data.get("frame_height", 1200),
            frame_rate=data.get("frame_rate", 100),
            calibration_frame_rate=data.get("calibration_frame_rate", 30),
            quality=data.get("quality", 21),
            encode_parallel=data.get("encode_parallel", 3),
            realtime_encode=data.get("realtime_encode", True),
            realtime_kick=data.get("realtime_kick", False),
            kick_max_lag=data.get("kick_max_lag", 240),
            gige_driver=data.get("gige_driver", "socket"),
            pfs_path=_resolve(data.get("pfs_path", "")),
            output_dir=_resolve(data.get("output_dir", "")),
            board_config=_resolve(data.get("board_config", "")),
            serial_port=data.get("serial_port", "COM3"),
            trigger_pins=data.get("trigger_pins", [2, 4, 6, 8, 10, 12]),
            n_cameras=data.get("n_cameras", 0),
            max_num_buffer=int(data.get("max_num_buffer", 1000)),
            calibration_min_per_cam_shared=int(
                data.get("calibration_min_per_cam_shared", 120)),
            calibration_min_edge=int(data.get("calibration_min_edge", 40)),
            calibration_min_grid_cells=int(
                data.get("calibration_min_grid_cells", 3)),
            pin_capture_threads=bool(data.get("pin_capture_threads", False)),
            thermal_poll_s=float(data.get("thermal_poll_s", 20.0)),
            encoder_pcores=bool(data.get("encoder_pcores", False)),
            capture_core_exclude=[
                int(c) for c in data.get("capture_core_exclude", [0])],
            pin_encoder_threads=bool(data.get("pin_encoder_threads", False)),
            stim_safe_pins=data.get("stim_safe_pins", [53]),
            calibration_exposure_us=float(data.get("calibration_exposure_us", 0.0)),
            calibration_gain_db=float(data.get("calibration_gain_db", -1.0)),
            trigger_rate_limit=float(data.get("trigger_rate_limit", 165.0)),
        )

    @staticmethod
    def list_profiles() -> list[Path]:
        if not PROFILES_DIR.exists():
            return []
        return sorted(PROFILES_DIR.glob("*.yaml"))


def _environment_metadata() -> dict:
    """Host/GPU facts worth freezing into every recording.

    Cheap, entirely best-effort, and never allowed to break saving metadata:
    a session that failed to record its environment is still a session, but a
    session whose environment is unknown is much harder to explain later.
    """
    import platform
    import subprocess
    import sys

    env = {
        "host": platform.node(),
        "os": platform.platform(),
        "python": sys.version.split()[0],
    }
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            name, driver, mem = [x.strip() for x in
                                 r.stdout.strip().splitlines()[0].split(",")]
            env.update(gpu=name, gpu_driver=driver, gpu_memory=mem)
    except Exception:
        pass
    try:
        from gui_app import hardware_check
        # Cached from the acquisition preflight; do not probe again here.
        n = getattr(hardware_check, "_nvenc_sessions", None)
        if n is not None:
            env["nvenc_sessions_available"] = n
    except Exception:
        pass
    return env


@dataclass
class SessionConfig:
    date: str = ""
    mouse_1: str = ""
    mouse_2: str = ""
    assay: str = "open_field"
    experimenter: str = "IT"
    cohort: str = ""
    cage: str = ""
    notes: str = ""

    base_data_dir: Path = Path("")
    pfs_path: Path = Path("")
    serial_port: str = "COM3"
    trigger_pins: list = field(default_factory=lambda: [2, 4, 6, 8, 10, 12])
    # Expected camera count. 0 = don't check. Nonzero makes open_all refuse a
    # partial set: names are positional by serial order, so a camera that fails
    # to ENUMERATE renames every camera after it and silently attaches the
    # calibration extrinsics to the wrong physical cameras.
    n_cameras: int = 0
    frame_rate: int = 100
    calibration_frame_rate: int = 30
    frame_width: int = 1920
    frame_height: int = 1200
    camera_names: list = field(default_factory=lambda: ["cam1", "cam2", "cam3", "cam4", "cam5", "cam6"])
    quality: int = 21
    encode_parallel: int = 3
    realtime_encode: bool = True
    realtime_kick: bool = False
    kick_max_lag: int = 240
    calibration_exposure_us: float = 0.0
    calibration_gain_db: float = -1.0

    def __post_init__(self):
        if not self.date:
            self.date = datetime.now().strftime("%Y%m%d")
        if not self.mouse_1:
            self.mouse_1 = "m1"
        if not self.mouse_2:
            self.mouse_2 = "m2"

    @classmethod
    def from_profile(cls, profile: RigProfile, **overrides) -> "SessionConfig":
        defaults = dict(
            base_data_dir=Path(profile.output_dir) if profile.output_dir else Path(""),
            pfs_path=Path(profile.pfs_path) if profile.pfs_path else Path(""),
            serial_port=profile.serial_port,
            trigger_pins=profile.trigger_pins,
            frame_rate=profile.frame_rate,
            calibration_frame_rate=profile.calibration_frame_rate,
            frame_width=profile.frame_width,
            frame_height=profile.frame_height,
            quality=profile.quality,
            encode_parallel=profile.encode_parallel,
            realtime_encode=profile.realtime_encode,
            realtime_kick=profile.realtime_kick,
            kick_max_lag=profile.kick_max_lag,
            calibration_exposure_us=profile.calibration_exposure_us,
            calibration_gain_db=profile.calibration_gain_db,
        )
        defaults.update(overrides)
        return cls(**defaults)

    @property
    def session_id(self) -> str:
        return f"{self.mouse_1}_{self.mouse_2}"

    @property
    def session_dir(self) -> Path:
        return self.base_data_dir / self.date / self.session_id

    def video_dir(self, acq_type: str) -> Path:
        return self.session_dir / acq_type

    def rate_for(self, acq_type: str) -> int:
        """Trigger/encode frame rate for an acquisition type."""
        return self.calibration_frame_rate if acq_type == "calibration" else self.frame_rate

    def save_metadata(self):
        now = datetime.now()
        # NOTE: _environment_metadata() is folded in below. It records the GPU
        # driver and the NVENC session count because both are *silent* failure
        # sources that move underneath you: NVIDIA has changed the concurrent
        # session cap across driver generations (2 -> 3 -> 5 -> 8 -> 12), and a
        # driver update that lowers it below the camera count pushes cameras
        # onto the raw fallback. Without this in the metadata, a session that
        # breaks after a driver update is undiagnosable after the fact.
        meta = dict(
            date=self.date, session_id=self.session_id,
            mouse_1=self.mouse_1, mouse_2=self.mouse_2,
            assay=self.assay, cohort=self.cohort, cage=self.cage,
            experimenter=self.experimenter, notes=self.notes,
            num_cameras=len(self.camera_names), camera_names=self.camera_names,
            frame_rate=self.frame_rate, calibration_frame_rate=self.calibration_frame_rate,
            resolution=[self.frame_width, self.frame_height],
            time_of_day=now.strftime("%H:%M:%S"),
            timestamp_iso=now.isoformat(),
            # Per-camera thermals, read at stop. Same rationale as the GPU
            # driver version above: it moves underneath a working rig and is
            # undiagnosable afterwards. `temp_max_c` is the one to read —
            # `temp_c` decays as soon as the load comes off.
            camera_thermals=getattr(self, "camera_thermals", None),
            **_environment_metadata(),
        )
        self.session_dir.mkdir(parents=True, exist_ok=True)
        path = self.session_dir / "session_metadata.json"
        with open(path, "w") as f:
            json.dump(meta, f, indent=2)
        return path
