"""Session configuration and path management.

RigProfile is the one place a rig is described; SessionConfig is one
acquisition session on that rig. Both are plain dataclasses so the field
list, its defaults and its types are the single source of truth: RigProfile.load
builds itself from ``dataclasses.fields`` rather than repeating each default,
so a default can never drift between the class and the loader.
"""
import dataclasses
import json
import math
import re
import types
import typing
import yaml
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gui_app.backends import KNOWN_BACKENDS


REPO_ROOT = Path(__file__).parent.parent
PROFILES_DIR = REPO_ROOT / "profiles"

#: GigE receive drivers RigProfile.gige_driver may name. Anything else raises at
#: load: the backend treats an unknown string as "auto", and pylon's default
#: has been observed to drop frames silently, so a typo must not reach it.
GIGE_DRIVERS = ("socket", "filter", "auto")

#: Encoder selections RigProfile.encoder may name. "auto" picks NVENC when a
#: session is available and falls back to libx264; the others force a path.
#: Consumed by the encoder selection code, declared and validated here.
ENCODERS = ("auto", "nvenc", "x264", "raw")

#: H.264 quantiser range. The encoders pass RigProfile.quality straight
#: through as the QP, and libx264 clamps a value above the top of the range
#: to it, so an out-of-range value would record at another quality.
QUALITY_RANGE = (0, 51)

#: Arduino pins a camera trigger may never occupy: 0 and 1 are Serial0, the
#: link the sketch handshakes over, so driving them would sever the board.
RESERVED_SERIAL_PINS = frozenset({0, 1})

#: Metadata fields a profile may pre-fill through ``metadata_defaults``. The
#: names match SessionConfig's fields so a profile cannot invent a key that
#: nothing writes into session_metadata.json.
METADATA_DEFAULT_KEYS = ("experimenter", "assay", "cohort", "cage", "notes")

#: Profile fields that hold a filesystem path. A relative value resolves
#: against the repository root so a profile works from any working directory.
_PATH_FIELDS = ("pfs_path", "output_dir", "board_config")

#: Element type for list-valued profile fields. Pin and core lists must be
#: ints because they reach ``pinMode``/affinity masks. ``camera_serials`` is
#: not here: its elements go through ``_coerce_serial``, which refuses
#: anything but a quoted string.
_LIST_ELEMENT_TYPES = {
    "trigger_pins": int,
    "stim_safe_pins": int,
    "capture_core_exclude": int,
}


class ProfileError(ValueError):
    """A profile file cannot be read or loaded as written. The message names
    the file and the offending key so the operator can fix the YAML without a
    traceback. Every failure inside ``RigProfile.load`` (unreadable file, YAML
    syntax, unknown key, bad value, failed validation) is raised as this one
    type so callers that load every profile in a directory can catch it and
    skip the bad one instead of aborting launch."""


def _default_metadata() -> dict:
    return {"experimenter": "", "assay": ""}


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
    # measured silently dropping ~23% of frames with default resend settings),
    # or "auto" (leave pylon's default).
    gige_driver: str = "socket"
    # Camera backend NAME, resolved by gui_app.backends.load_backend. The
    # profile selects the vendor so no code changes when a rig ports; the
    # default is the only backend shipped.
    camera_backend: str = "basler"
    # Serial numbers of the cameras this rig consists of, as quoted strings in
    # ASCENDING order, or None to open every enumerated camera. When set,
    # open_all opens only these serials and refuses to start if any of them
    # is missing, so a replacement camera cannot slip into a session under a
    # calibrated camera's name. Names remain positional over the backend's
    # serial-sorted subset (cam1 is the smallest listed serial that
    # enumerated), which is why validate() refuses any other order: an
    # out-of-order list would name the cameras differently from the list and
    # attach the calibration extrinsics to the wrong physical camera. An
    # extra, unlisted device on the host still fails n_cameras, because that
    # count is taken over the full enumeration before this list narrows it.
    # Serials must be quoted: YAML reads a bare leading-zero number as octal,
    # so an unquoted serial can load as a different number and validate.
    camera_serials: list | None = None
    # Video encoder: "auto" (NVENC when a session is free, else libx264),
    # "nvenc", "x264" or "raw" (raw.bin + post-hoc encode). Declared here so
    # a rig without an NVIDIA GPU is a profile edit, not a code edit.
    encoder: str = "auto"
    # GigE bandwidth reserve applied at open, or None to keep each camera's
    # .pfs value. The percentage (GevSCBWR) is the share of link bandwidth
    # held back for packet resends; the accumulation (GevSCBWRA) is how many
    # reserve slots may pool. Both are per-rig network facts, so they belong
    # here rather than in the shared camera file.
    gev_bandwidth_reserve_pct: float | None = None
    gev_bandwidth_reserve_accum: int | None = None
    # Metadata fields the sidebar pre-fills for a new session. The code default
    # is blank on purpose: an initials or assay string baked into code would
    # be written into every session_metadata.json on any rig whose operator
    # does not notice the field, so lab-specific values live in the profile.
    metadata_defaults: dict = field(default_factory=_default_metadata)
    pfs_path: str = ""
    output_dir: str = ""
    board_config: str = ""
    # Trigger-board serial port. No code default: the device name is per-host
    # (COMn on Windows, /dev/tty* elsewhere), so a profile must state it and an
    # empty value fails at the first serial open instead of guessing.
    serial_port: str = ""
    trigger_pins: list = field(default_factory=lambda: [2, 4, 6, 8, 10, 12])
    # Expected camera count. 0 = don't check. Nonzero makes open_all refuse a
    # partial set: names are positional by serial order, so a camera that fails
    # to ENUMERATE renames every camera after it and silently attaches the
    # calibration extrinsics to the wrong physical cameras.
    n_cameras: int = 0
    # Driver-side buffers queued per camera. THE LARGEST SINGLE RAM CONSUMER:
    # n_cams x max_num_buffer x width x height bytes, so 1000 buffers is 20.7 GiB
    # at nine 1920x1200 cameras, before the NV12 ring is counted at all. It is a
    # profile field (not a camera_manager constant) so the capacity preflight's
    # own advice ("Lower MaxNumBuffer or kick_max_lag") can be followed.
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
    # not stinginess; ~4x and ~2.7x the caps just makes a 9-camera calibration
    # take far longer than the data can be used for. Raise them if calibrations
    # come out marginal; the
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
    # 100 fps. In one GUI process running a MIXED sequence
    # (recording -> calibration -> solve -> recording), the first recording held
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
    # network traffic it is trying to receive. With grab threads pinned, lag
    # behind the leader as median/p95/max -- the victim follows the CORE, not
    # the camera:
    #   exclude nothing (cam1 on CPU 0)   cam1 0/6/12, others 0/1/1
    #   exclude [0]     (cam1 on CPU 1)   cam1 0/3/4,  others 0/1/1
    #   exclude [0, 1]                    ALL NINE 0/1/1
    # Under the GUI the unexcluded case was far worse than headless: the same
    # camera diverged to kick_max_lag (480) and was force-dropped.
    capture_core_exclude: list = field(default_factory=lambda: [0])
    # Confine ENCODER threads to the E-core set. Separate from the above, and
    # default OFF because it measures WORSE at nine cameras with grab threads
    # pinned in every arm:
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

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: Path) -> "RigProfile":
        """Read a profile YAML into a RigProfile, or raise ProfileError.

        Every key must be a field of this class, every value is coerced to the
        field's declared type, and the result is validated (``validate``)
        before it is returned. Refusing here, in the loader, is what keeps a
        misconfiguration out of the Qt slots that start an acquisition: a bad
        profile is a dialog at load time, not a traceback or a silently
        degraded recording later.
        """
        path = Path(path)
        try:
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ProfileError(f"{path.name}: not valid YAML: {e}") from None
        except (OSError, UnicodeDecodeError) as e:
            raise ProfileError(f"{path.name}: cannot be read: {e}") from None
        if data is None:
            # An empty file is a mistake, not a request for every default:
            # a profile that ran the rig entirely on code defaults would
            # look valid while describing no rig at all.
            raise ProfileError(f"{path.name}: the profile file is empty")
        if not isinstance(data, dict):
            raise ProfileError(
                f"{path.name}: expected a mapping of profile fields at the top "
                f"level, got {type(data).__name__}")

        known = {f.name: f for f in dataclasses.fields(cls)}
        # Keys are stringified before sorting: YAML types an unquoted key
        # (``1:``, ``yes:``), and sorting a str against an int raises a
        # TypeError that would escape the ProfileError contract.
        unknown = sorted(str(k) for k in set(data) - set(known))
        if unknown:
            raise ProfileError(
                f"{path.name}: unknown profile field(s) {unknown}. Fields a "
                f"profile may set: {sorted(known)}")

        kwargs = {}
        for key, raw in data.items():
            try:
                kwargs[key] = _coerce_field(known[key], raw)
            except (TypeError, ValueError) as e:
                raise ProfileError(f"{path.name}: field {key!r}: {e}") from None
        if "name" not in kwargs:
            kwargs["name"] = path.stem
        for key in _PATH_FIELDS:
            if key in kwargs:
                kwargs[key] = _resolve_path(kwargs[key])
        if "metadata_defaults" in kwargs:
            merged = _default_metadata()
            merged.update(kwargs["metadata_defaults"])
            kwargs["metadata_defaults"] = merged

        profile = cls(**kwargs)
        try:
            profile.validate()
        except ValueError as e:
            raise ProfileError(f"{path.name}: {e}") from None
        return profile

    def validate(self) -> None:
        """Raise ValueError on a combination of values the rig cannot run.

        These are the checks whose failure would otherwise be silent on the
        rig: a driver typo that lands on pylon's default, a trigger on the
        serial link or on the laser pin, a frame rate the camera's own rate
        limiter would make it skip triggers at, a quality the encoder clamps,
        or a pool too shallow for the kick-out depth.
        """
        if self.gige_driver not in GIGE_DRIVERS:
            raise ValueError(
                f"gige_driver {self.gige_driver!r} is not one of "
                f"{list(GIGE_DRIVERS)}; an unknown name would fall through to "
                f"pylon's default driver")
        if self.encoder not in ENCODERS:
            raise ValueError(
                f"encoder {self.encoder!r} is not one of {list(ENCODERS)}")
        if not isinstance(self.camera_backend, str) or not self.camera_backend:
            raise ValueError("camera_backend must be a non-empty backend name")
        if self.camera_backend not in KNOWN_BACKENDS:
            raise ValueError(
                f"camera_backend {self.camera_backend!r} is not a known "
                f"backend. Known backends: {', '.join(KNOWN_BACKENDS)}. A new "
                f"one is a module in gui_app/backends/, registered in "
                f"KNOWN_BACKENDS.")

        pins = set(self.trigger_pins)
        on_serial = sorted(pins & RESERVED_SERIAL_PINS)
        if on_serial:
            raise ValueError(
                f"trigger_pins {on_serial} are the Serial0 link the trigger "
                f"board handshakes over; a camera trigger there severs it")
        on_stim = sorted(pins & set(self.stim_safe_pins))
        if on_stim:
            raise ValueError(
                f"trigger_pins {on_stim} are also in stim_safe_pins; a camera "
                f"trigger there would drive the stimulation output at the "
                f"frame rate")
        if len(pins) != len(self.trigger_pins):
            raise ValueError(f"trigger_pins {self.trigger_pins} lists a pin twice")

        limit = float(self.trigger_rate_limit)
        if limit < 0:
            raise ValueError("trigger_rate_limit must be 0 (off) or positive")
        for label, fps in (("frame_rate", self.frame_rate),
                           ("calibration_frame_rate", self.calibration_frame_rate)):
            if fps <= 0:
                raise ValueError(f"{label} must be positive, got {fps}")
            # The camera's rate limiter enforces a minimum frame interval of
            # 1/limit, so a trigger period at or under it is ignored by the
            # camera: no frame, no block ID consumed, and blockids.npy stays
            # contiguous while the videos drift. Refuse rather than record it.
            if limit and fps >= limit:
                raise ValueError(
                    f"{label} {fps} >= trigger_rate_limit {limit:g}: the "
                    f"camera would skip triggers")

        for label, value in (("frame_width", self.frame_width),
                             ("frame_height", self.frame_height),
                             ("kick_max_lag", self.kick_max_lag),
                             ("max_num_buffer", self.max_num_buffer),
                             ("encode_parallel", self.encode_parallel)):
            if value <= 0:
                raise ValueError(f"{label} must be positive, got {value}")
        # NV12 stores its chroma at half resolution in both directions, so an
        # odd size cannot be encoded. On a camera that takes its ROI from the
        # profile the odd size would reach the camera and fail only at Record.
        for label, value in (("frame_width", self.frame_width),
                             ("frame_height", self.frame_height)):
            if value % 2:
                raise ValueError(
                    f"{label} {value} must be even: the NV12 frames the "
                    f"encoders take have half-resolution chroma, so an odd "
                    f"size cannot be encoded")
        lo, hi = QUALITY_RANGE
        if not lo <= self.quality <= hi:
            raise ValueError(
                f"quality {self.quality} is outside {lo}..{hi}, the H.264 QP "
                f"range the encoders take; lower values give higher quality "
                f"and larger files")
        # While the coordinator waits for a lagging camera, that camera's
        # backlog waits in its driver pool, so a pool shallower than the
        # kick-out depth runs dry first and loses frames the coordinator would
        # have released.
        if (self.realtime_encode and self.realtime_kick
                and self.max_num_buffer < self.kick_max_lag):
            raise ValueError(
                f"max_num_buffer {self.max_num_buffer} is below kick_max_lag "
                f"{self.kick_max_lag}. In kick-out mode a lagging camera's "
                f"backlog waits in the driver pool, so a pool shallower than "
                f"kick_max_lag loses frames the coordinator would have waited "
                f"for. Raise max_num_buffer or lower kick_max_lag.")
        cal = self.calibration_exposure_us
        if not (math.isfinite(cal) and cal >= 0):
            raise ValueError(
                f"calibration_exposure_us {cal:g} must be 0 (keep the "
                f"recording exposure) or a positive number of microseconds")
        if self.n_cameras < 0:
            raise ValueError("n_cameras must be 0 (unchecked) or positive")
        if self.camera_serials is not None:
            if not self.camera_serials:
                raise ValueError(
                    "camera_serials is empty; omit it to name cameras by "
                    "enumeration order")
            if not all(isinstance(s, str) and s for s in self.camera_serials):
                raise ValueError(
                    f"camera_serials {self.camera_serials} must be non-empty "
                    f"strings, as the backend reports them")
            if len(set(self.camera_serials)) != len(self.camera_serials):
                raise ValueError(f"camera_serials {self.camera_serials} lists "
                                 f"a serial twice")
            # The manager names cameras cam1..camN by position over the
            # backend's serial-sorted subset, so the list is required to be
            # in that same (string) order; otherwise cam{i} would not be
            # entry i of the list and the extrinsics would attach to the
            # wrong physical camera with no error anywhere.
            if self.camera_serials != sorted(self.camera_serials):
                raise ValueError(
                    f"camera_serials {self.camera_serials} is not in ascending "
                    f"order; cameras are named cam1..camN over the serial-sorted "
                    f"set, so list them as {sorted(self.camera_serials)}")
            if self.n_cameras and self.n_cameras != len(self.camera_serials):
                raise ValueError(
                    f"n_cameras {self.n_cameras} disagrees with the "
                    f"{len(self.camera_serials)} entries in camera_serials")
        pct = self.gev_bandwidth_reserve_pct
        if pct is not None and not 0 <= pct <= 100:
            raise ValueError(
                f"gev_bandwidth_reserve_pct {pct} must be within 0..100")
        accum = self.gev_bandwidth_reserve_accum
        if accum is not None and accum < 0:
            raise ValueError(
                f"gev_bandwidth_reserve_accum {accum} must be non-negative")
        bad_meta = sorted(set(self.metadata_defaults) - set(METADATA_DEFAULT_KEYS))
        if bad_meta:
            raise ValueError(
                f"metadata_defaults keys {bad_meta} are not session metadata "
                f"fields; allowed: {list(METADATA_DEFAULT_KEYS)}")

    @staticmethod
    def list_profiles() -> list[Path]:
        if not PROFILES_DIR.exists():
            return []
        return sorted(PROFILES_DIR.glob("*.yaml"))


# ----------------------------------------------------------------- coercion
def _resolve_path(raw: str) -> str:
    if not raw:
        return ""
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / p
    return str(p)


def _optional_inner(tp):
    """For ``X | None`` return X; for anything else return None."""
    if isinstance(tp, types.UnionType) or typing.get_origin(tp) is typing.Union:
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return None


def _coerce_scalar(tp, value):
    """Coerce one YAML scalar to ``tp`` (int, float, bool or str).

    YAML already types unquoted values, so a mismatch is almost always a typo
    (a quoted "100", ``yes`` for a number). Ints accept an integral float or a
    digit string but never a bool; bools accept the YAML spellings only; a
    string never silently swallows a list or mapping.
    """
    if tp is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "yes", "on"):
                return True
            if low in ("false", "no", "off"):
                return False
        raise TypeError(f"expected true/false, got {value!r}")
    if tp is int:
        if isinstance(value, bool):
            raise TypeError(f"expected an integer, got {value!r}")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value.strip())
        raise TypeError(f"expected an integer, got {value!r}")
    if tp is float:
        if isinstance(value, bool):
            raise TypeError(f"expected a number, got {value!r}")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise TypeError(f"expected a number, got {value!r}")
    if tp is str:
        if isinstance(value, (list, dict)):
            raise TypeError(f"expected text, got {type(value).__name__}")
        return "" if value is None else str(value)
    return value


def _coerce_serial(value):
    """One camera serial: a non-empty quoted string, exactly as YAML read it.

    A bare number is refused rather than stringified. YAML 1.1 reads an
    unquoted leading-zero number as octal, so ``01234567`` loads as 342391;
    stringifying it would let the profile validate and leave open_all to
    report a serial that never existed as "did not enumerate", with no hint
    that the YAML was at fault.
    """
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"camera serials must be quoted strings, got {value!r}")
    return value.strip()


def _coerce_field(f: dataclasses.Field, value):
    """Coerce a YAML value to the declared type of dataclass field ``f``."""
    tp = f.type
    inner = _optional_inner(tp)
    if inner is not None:
        if value is None:
            return None
        tp = inner
    if tp is list:
        if not isinstance(value, (list, tuple)):
            raise TypeError(f"expected a list, got {value!r}")
        if f.name == "camera_serials":
            return [_coerce_serial(v) for v in value]
        elem = _LIST_ELEMENT_TYPES.get(f.name)
        if elem is None:
            return list(value)
        return [_coerce_scalar(elem, v) for v in value]
    if tp is dict:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise TypeError(f"expected a mapping, got {value!r}")
        return {str(k): _coerce_scalar(str, v) for k, v in value.items()}
    if value is None:
        raise TypeError("value is empty; remove the key to keep the default")
    return _coerce_scalar(tp, value)


# --------------------------------------------------------- path components
#: Characters that cannot appear in a directory name on Windows, plus the two
#: path separators. Any of them in a session field would either fail mkdir
#: inside a Qt slot or, for the separators, silently nest or escape the data
#: directory.
_BAD_COMPONENT_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
#: Device names Windows reserves regardless of extension; a directory so named
#: cannot be created or, worse, aliases the device.
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})
#: Upper bound on one path component. Session paths nest five levels under the
#: data directory and Windows limits the whole path, so a runaway field must
#: fail here rather than at the deepest mkdir.
MAX_COMPONENT_LEN = 80


def validate_path_component(value: str, label: str) -> str:
    """Return ``value`` stripped, or raise ValueError naming ``label``.

    Session fields typed into the sidebar become directory names and mp4
    filenames, so a value that pathlib would treat as anything but a single
    plain component is refused: separators, '.'/'..', Windows-reserved
    characters and device names, control characters, and a trailing dot or
    space (which Windows strips, making the name differ from what was typed).
    """
    if not isinstance(value, str):
        raise ValueError(f"{label} must be text, got {type(value).__name__}")
    s = value.strip()
    if not s:
        raise ValueError(f"{label} is empty")
    if s in (".", ".."):
        raise ValueError(f"{label} {s!r} is a directory reference, not a name")
    bad = sorted(set(_BAD_COMPONENT_CHARS.findall(s)))
    if bad:
        shown = ", ".join(repr(c) for c in bad)
        raise ValueError(f"{label} {s!r} contains {shown}, which cannot be "
                         f"part of a folder name")
    if s[-1] in ". ":
        raise ValueError(f"{label} {s!r} ends with a dot or space")
    if s.split(".")[0].upper() in _RESERVED_NAMES:
        raise ValueError(f"{label} {s!r} is a reserved device name on Windows")
    if len(s) > MAX_COMPONENT_LEN:
        raise ValueError(f"{label} is {len(s)} characters; the limit is "
                         f"{MAX_COMPONENT_LEN}")
    return s


def validate_session_date(value: str) -> str:
    """Return ``value`` stripped if it is a YYYYMMDD date, else raise ValueError.

    The date is the first directory level under the data directory and the
    prefix of every mp4 name, so it must be a real calendar date in the one
    layout the post-hoc tools glob for.
    """
    s = validate_path_component(value, "date")
    # strptime accepts a 7-digit "2026918", so the width is checked explicitly.
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"date {s!r} is not eight digits (YYYYMMDD)")
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError:
        raise ValueError(f"date {s!r} is not a YYYYMMDD calendar date") from None
    return s


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
        # The child must not open a console window over the GUI under pythonw,
        # so it takes the same quiet launch kwargs every ffmpeg launch uses.
        try:
            from gui_app.ffmpeg_cmd import quiet_popen_kwargs
            quiet = quiet_popen_kwargs()
        except Exception:
            quiet = {}
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            stdin=subprocess.DEVNULL, **quiet)
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


METADATA_FILENAME = "session_metadata.json"


@dataclass
class SessionConfig:
    """One acquisition session: who, what, where, on which rig.

    RULE: rig facts live on ``profile`` and are read from there; this class
    mirrors none of them except the two frame rates ``rate_for`` resolves.
    REASON: two sources for one fact is how a session comes to be encoded at a
    geometry the cameras were never configured for, and a mirror drifts
    silently because nothing compares the copies. Nothing rig-specific is
    defaulted here.
    """
    date: str = ""
    mouse_1: str = ""
    mouse_2: str = ""
    assay: str = ""
    experimenter: str = ""
    cohort: str = ""
    cage: str = ""
    notes: str = ""

    base_data_dir: Path = Path("")
    #: The rig this session runs on. None only for a config built by hand.
    profile: RigProfile | None = None
    #: The two rates stay here because ``rate_for`` is a config method: an
    #: acquisition's fps is a property of what is being recorded, not only of
    #: the rig. Every other rig fact is read from ``profile``.
    frame_rate: int = 100
    calibration_frame_rate: int = 30
    #: Operator-facing camera names, cam1..camN, set from the OPENED camera
    #: set. Empty until then: a fixed count here would be a rig assumption
    #: outside the backend layer.
    camera_names: list = field(default_factory=list)
    #: Per-camera temperature readings taken at stop, or None if never read.
    #: Declared so save_metadata has a field to write, not a guessed attribute.
    camera_thermals: list | None = None

    def __post_init__(self):
        # Blank identity fields take placeholders so a quick test session can
        # start without typing. Nothing is refused here: a config is built
        # inside Qt slots from whatever the sidebar holds, and an exception
        # there aborts the slot with the toggle left ON. The path-component
        # checks live in validate(), which the entry point calls where it can
        # show a dialog and reset the toggles.
        self.date = str(self.date or "").strip() or datetime.now().strftime("%Y%m%d")
        self.mouse_1 = str(self.mouse_1 or "").strip() or "m1"
        self.mouse_2 = str(self.mouse_2 or "").strip() or "m2"
        # cohort and cage are metadata values only (session_dir is
        # base/date/mouse_1_mouse_2), so any text is kept as typed.
        for label in ("cohort", "cage", "assay", "experimenter"):
            setattr(self, label, str(getattr(self, label) or "").strip())
        if not isinstance(self.base_data_dir, Path):
            self.base_data_dir = Path(self.base_data_dir or "")

    def validate(self) -> "SessionConfig":
        """Raise ValueError if a field that becomes a path is not a plain
        component; return self otherwise.

        date, mouse_1 and mouse_2 form session_dir and the prefix of every mp4
        name, so each must be one plain directory component (and the date a
        YYYYMMDD calendar date) or the mkdir fails deep inside an acquisition,
        or worse, a separator nests or escapes the data directory. The
        contract for an entry point (acquisition start, snapshot, solve) is
        to call this before any directory is created and turn the ValueError
        into a dialog; a caller that skips it gets the mkdir failure, or the
        misplaced directory, that this exists to prevent.
        """
        self.date = validate_session_date(self.date)
        self.mouse_1 = validate_path_component(self.mouse_1, "mouse_1")
        self.mouse_2 = validate_path_component(self.mouse_2, "mouse_2")
        return self

    @classmethod
    def from_profile(cls, profile: RigProfile, **overrides) -> "SessionConfig":
        """A session on ``profile``. Metadata fields not given in
        ``overrides`` take the profile's ``metadata_defaults``."""
        defaults = dict(
            profile=profile,
            base_data_dir=Path(profile.output_dir) if profile.output_dir else Path(""),
            frame_rate=profile.frame_rate,
            calibration_frame_rate=profile.calibration_frame_rate,
        )
        for key in METADATA_DEFAULT_KEYS:
            if key in profile.metadata_defaults:
                defaults[key] = profile.metadata_defaults[key]
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

    def metadata(self, acq_type: str | None = None) -> dict:
        """The session_metadata.json contents for ``acq_type``."""
        now = datetime.now()
        # The GPU driver and the NVENC session count are recorded because both
        # are silent failure sources that move underneath a working rig: the
        # driver's concurrent-session cap changes across generations, and one
        # that drops below the camera count pushes cameras onto the raw
        # fallback. Without them in the metadata a session that breaks after a
        # driver update cannot be explained afterwards.
        meta = dict(
            date=self.date, session_id=self.session_id,
            mouse_1=self.mouse_1, mouse_2=self.mouse_2,
            assay=self.assay, cohort=self.cohort, cage=self.cage,
            experimenter=self.experimenter, notes=self.notes,
            rig=self.profile.name if self.profile else None,
            num_cameras=len(self.camera_names), camera_names=self.camera_names,
            frame_rate=self.frame_rate, calibration_frame_rate=self.calibration_frame_rate,
            # Read from the profile, which is what the cameras were actually
            # configured from. None for a config built without one.
            resolution=([self.profile.frame_width, self.profile.frame_height]
                        if self.profile else None),
            time_of_day=now.strftime("%H:%M:%S"),
            timestamp_iso=now.isoformat(),
            # Per-camera thermals, read at stop. Same rationale as the GPU
            # driver version above: it moves underneath a working rig and is
            # undiagnosable afterwards. `temp_max_c` is the one to read —
            # `temp_c` decays as soon as the load comes off.
            camera_thermals=self.camera_thermals,
            **_environment_metadata(),
        )
        if acq_type is not None:
            # Which acquisition this file describes, so a calibration's and a
            # recording's metadata are never mistaken for one another.
            meta["acq_type"] = acq_type
            meta["acq_fps"] = self.rate_for(acq_type)
        return meta

    def save_metadata(self, acq_type: str | None = None) -> Path:
        """Write session_metadata.json and return its path.

        With ``acq_type`` the file goes into ``video_dir(acq_type)``, beside
        the videos it describes, so a calibration and a recording in the same
        session each keep their own timestamp, thermals and environment. A
        session-level copy is written only when none exists yet, for readers
        that look there; it is never overwritten, so it describes the first
        acquisition of the session rather than whichever ran last. Without
        ``acq_type`` only the session-level file is written and overwritten,
        which is the older layout.
        """
        meta = self.metadata(acq_type)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        session_copy = self.session_dir / METADATA_FILENAME
        if acq_type is None:
            with open(session_copy, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            return session_copy
        target_dir = self.video_dir(acq_type)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / METADATA_FILENAME
        with open(path, "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)
        if not session_copy.exists():
            with open(session_copy, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        return path
