"""Session configuration and path management.

RigProfile is the one place a rig is described; SessionConfig is one
acquisition session on that rig. Both are plain dataclasses so the field
list, its defaults and its types are the single source of truth: RigProfile.load
builds itself from ``dataclasses.fields`` rather than repeating each default,
so a default can never drift between the class and the loader.

A profile's ``camera:`` block (CameraSpec) holds the camera settings of a
backend that has no settings file of its own, such as FLIR. It is parsed by
``parse_camera_spec`` into frozen dataclasses and handed to the backend's
``open()``; ``RigProfile.validate`` refuses the fields that belong to another
vendor, and ``RigProfile.settings_ready`` says whether the files a profile
points at are there.
"""
import dataclasses
import difflib
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
# One definition of the Serial0 pins, shared with the stimulation compiler, so
# the trigger-pin refusal and the stim-pin refusal can never disagree.
from gui_app.stim_compiler import RESERVED_SERIAL_PINS


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

#: How the NVENC encoder receives each frame (RigProfile.nvenc_upload).
#: "host" hands PyNvVideoCodec the ring slot as it is; "pinned" copies it into
#: page-locked memory outside the GIL first. gui_app/nvenc.py implements both.
NVENC_UPLOAD_MODES = ("host", "pinned")

#: CUDA context of the pinned upload path (RigProfile.nvenc_context): "shared"
#: runs every encoder in the device's primary context, "own" gives each
#: encoder a context of its own.
NVENC_CONTEXT_MODES = ("shared", "own")

#: H.264 quantiser range. The encoders pass RigProfile.quality straight
#: through as the QP, and libx264 clamps a value above the top of the range
#: to it, so an out-of-range value would record at another quality.
QUALITY_RANGE = (0, 51)

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


# ------------------------------------------------------------ camera: block
#: Backends that read their camera settings from a pylon feature file named by
#: pfs_path. settings_ready() refuses to open them until that file exists.
_PFS_BACKENDS = ("basler",)
#: Backends whose trigger mode runs the AcquisitionFrameRate limiter that
#: trigger_rate_limit sets. The simulated camera models the same limiter, so
#: its profile keeps the frame_rate < trigger_rate_limit rule too.
_LIMITER_BACKENDS = ("basler", "sim")
#: Backends that accept a camera: block. The simulated backend accepts one so
#: the block's path through open() can be exercised without an SDK.
_CAMERA_BLOCK_BACKENDS = ("flir", "flir_sim", "sim")
#: Backends with no settings file of their own: the block is their only source
#: of the recording exposure, gain and trigger line, so it is required.
_CAMERA_BLOCK_REQUIRED = ("flir", "flir_sim")

#: Values the block's enumerated keys may take. Each is the camera's own node
#: entry name, so the backend writes it unchanged.
PIXEL_FORMATS = ("Mono8",)
TRIGGER_ACTIVATIONS = ("RisingEdge", "FallingEdge")
TRIGGER_OVERLAPS = ("ReadOut", "Off", "PreviousFrame")
FLIR_USER_SETS = ("Default", "UserSet0", "UserSet1", "none")
FLIR_ADC_BIT_DEPTHS = ("Bit8", "Bit10", "Bit12")
FLIR_STREAM_MODES = ("auto", "TeledyneGigEVision", "LWF", "Socket")
FLIR_BLOCK_ID_SOURCES = ("auto", "frame_id", "trigger_counter")
FLIR_TIMESTAMP_SOURCES = ("auto", "image", "chunk")
#: Words camera.flir.link_throughput_limit accepts besides a byte rate.
FLIR_LINK_LIMIT_WORDS = ("auto", "max")
#: GigE Vision stream packet sizes a profile may ask for, in bytes: the
#: smallest packet every IPv4 host must accept, and the usual jumbo frame.
FLIR_PACKET_SIZE_RANGE = (576, 9000)
#: A hardware input line as GenICam names it. A software trigger is refused:
#: Panopticon is always hardware-triggered, so every camera takes the same
#: edge.
_TRIGGER_LINE = re.compile(r"Line[0-9]+")


@dataclass(frozen=True)
class TriggerSpec:
    """``camera.trigger``: the input line the hardware trigger arrives on, and
    how the camera responds to it.

    ``overlap`` defaults to ReadOut because a camera that does not overlap
    exposure with readout disregards a trigger that arrives during readout,
    and a disregarded trigger consumes no frame ID. ``delay_us`` None leaves
    the camera's TriggerDelay as it is.
    """
    line: str
    activation: str = "RisingEdge"
    overlap: str = "ReadOut"
    delay_us: float | None = None


@dataclass(frozen=True)
class PerCameraSpec:
    """One camera's overrides, ``camera.per_camera[serial]``. None keeps the
    block-wide value."""
    exposure_us: float | None = None
    gain_db: float | None = None
    offset_x: int | str | None = None
    offset_y: int | str | None = None
    trigger_line: str | None = None


@dataclass(frozen=True)
class FlirSpec:
    """``camera.flir``: settings only a Spinnaker camera has.

    Every range is checked against the camera at open; the loader checks only
    what holds for every camera. None leaves the camera's value as the
    baseline user set left it.
    """
    #: User set loaded before anything else is written, so a camera left in
    #: an odd state by another program starts from factory settings. "none"
    #: keeps whatever the camera holds.
    user_set: str = "Default"
    gamma_enable: bool = False
    black_level: float | None = None
    adc_bit_depth: str | None = None
    #: Bytes per second, "auto" (sized from the frame and frame_rate by the
    #: backend), "max", or None to leave the camera's value.
    link_throughput_limit: int | str | None = "auto"
    packet_size: int | None = None
    packet_delay: int | None = None
    stream_mode: str = "auto"
    extended_ids: bool = True
    block_id_source: str = "auto"
    timestamp_source: str = "auto"
    #: Folder holding the Spinnaker C library, resolved against the
    #: repository root like every profile path. None searches the default
    #: install locations.
    sdk_dir: str | None = None


@dataclass(frozen=True)
class CameraSpec:
    """A profile's ``camera:`` block, as ``parse_camera_spec`` returns it.

    The block is the only source of every setting it names; the backend
    takes everything else from ``flir.user_set``, so a camera cannot carry a
    value into a recording that the profile does not state. ``exposure_us``,
    ``gain_db`` and ``trigger.line`` are required for the same reason.

    ``per_camera`` is a tuple of ``(serial, PerCameraSpec)`` pairs in serial
    order, so the spec stays hashable; ``for_camera(serial)`` gives the
    settings one camera runs with.
    """
    exposure_us: float
    gain_db: float
    trigger: TriggerSpec
    pixel_format: str = "Mono8"
    #: Pixels from the sensor's left/top edge, or "center" to centre the
    #: frame_width x frame_height region on the sensor.
    offset_x: int | str = "center"
    offset_y: int | str = "center"
    per_camera: tuple = ()
    flir: FlirSpec = field(default_factory=FlirSpec)

    def override(self, serial: str) -> PerCameraSpec | None:
        """The ``per_camera`` entry for ``serial``, or None."""
        for s, spec in self.per_camera:
            if s == serial:
                return spec
        return None

    def for_camera(self, serial: str) -> "CameraSpec":
        """This block with ``serial``'s overrides applied and ``per_camera``
        emptied: the settings that one camera is opened with."""
        o = self.override(serial)
        if o is None:
            return dataclasses.replace(self, per_camera=())
        trigger = self.trigger
        if o.trigger_line is not None:
            trigger = dataclasses.replace(trigger, line=o.trigger_line)
        return dataclasses.replace(
            self, per_camera=(), trigger=trigger,
            exposure_us=(self.exposure_us if o.exposure_us is None
                         else o.exposure_us),
            gain_db=self.gain_db if o.gain_db is None else o.gain_db,
            offset_x=self.offset_x if o.offset_x is None else o.offset_x,
            offset_y=self.offset_y if o.offset_y is None else o.offset_y)

    def to_dict(self) -> dict:
        """The block as JSON-ready data, laid out as the profile writes it.
        ``per_camera`` keeps only the keys each entry sets."""
        out = dataclasses.asdict(self)
        out["per_camera"] = {
            s: {k: v for k, v in dataclasses.asdict(o).items() if v is not None}
            for s, o in self.per_camera}
        return out


_CAMERA_KEYS = tuple(f.name for f in dataclasses.fields(CameraSpec))
_TRIGGER_KEYS = tuple(f.name for f in dataclasses.fields(TriggerSpec))
_PER_CAMERA_KEYS = tuple(f.name for f in dataclasses.fields(PerCameraSpec))
_FLIR_KEYS = tuple(f.name for f in dataclasses.fields(FlirSpec))


def _block_refused_message(backend: str) -> str:
    if backend == "basler":
        return ("camera block given but camera_backend is 'basler': Basler "
                "cameras take their settings from the .pfs named by pfs_path. "
                "Remove the camera: block, or set camera_backend: flir.")
    return (f"camera block given but camera_backend {backend!r} takes no "
            f"camera: block. Remove it, or set camera_backend to one of "
            f"{', '.join(_CAMERA_BLOCK_BACKENDS)}.")


def _required_reason(backend: str, what: str) -> str:
    if backend in _CAMERA_BLOCK_REQUIRED:
        return (f"FLIR cameras have no .pfs, so the recording {what} must be "
                f"written here")
    return f"the camera: block is where the recording {what} is written"


def _check_keys(raw, known: tuple, where: str) -> dict:
    """``raw`` as a dict with str keys, refusing a non-mapping and any key
    that is not in ``known``, with the closest known key as a suggestion."""
    if not isinstance(raw, dict):
        raise ValueError(f"{where}: expected a mapping of settings, got "
                         f"{type(raw).__name__}")
    for key in raw:
        name = str(key)
        if name in known:
            continue
        close = difflib.get_close_matches(name, known, n=1)
        hint = (f"did you mean {close[0]!r}?" if close
                else f"the fields are {', '.join(known)}")
        raise ValueError(f"{where}: unknown field {name!r}; {hint}")
    return {str(k): v for k, v in raw.items()}


def _spec_scalar(where: str, tp, value, nullable: bool = False):
    """One block value coerced like a top-level field, or None when
    ``nullable`` and the YAML value is empty."""
    if value is None:
        if nullable:
            return None
        raise ValueError(f"{where} is empty; remove the key to keep the "
                         f"default")
    try:
        return _coerce_scalar(tp, value)
    except TypeError as e:
        raise ValueError(f"{where}: {e}") from None


def _spec_choice(where: str, value, choices: tuple, nullable: bool = False):
    value = _spec_scalar(where, str, value, nullable)
    if value is None or value in choices:
        return value
    by_lower = {c.lower(): c for c in choices}
    hint = (f"; did you mean {by_lower[value.lower()]!r}?"
            if value.lower() in by_lower else "")
    raise ValueError(f"{where} {value!r} is not one of "
                     f"{', '.join(choices)}{hint}")


def _spec_offset(where: str, value):
    """An ROI offset: "center" or a pixel count of 0 or more."""
    if isinstance(value, str) and value.strip() == "center":
        return "center"
    try:
        n = _spec_scalar(where, int, value)
    except ValueError:
        raise ValueError(f"{where} must be a pixel count of 0 or more, or "
                         f"center; got {value!r}") from None
    if n < 0:
        raise ValueError(f"{where} {n} must be 0 or more pixels, or center")
    return n


def _spec_trigger_line(where: str, value) -> str:
    line = _spec_scalar(where, str, value).strip()
    if line.lower() == "software":
        raise ValueError(f"{where} {line!r} is refused: Panopticon is always "
                         f"hardware-triggered.")
    if not _TRIGGER_LINE.fullmatch(line):
        fixed = line[:1].upper() + line[1:]
        hint = (f" Did you mean {fixed!r}?" if _TRIGGER_LINE.fullmatch(fixed)
                else "")
        raise ValueError(
            f"{where} {line!r} is not a camera input line. Lines are named "
            f"Line0, Line1 and so on; run 'uv run probe_flir.py --find-line' "
            f"to see which one your trigger wire is on.{hint}")
    return line


def _spec_exposure(where: str, value) -> float:
    us = _spec_scalar(where, float, value)
    if not (math.isfinite(us) and us > 0):
        raise ValueError(f"{where} {us:g} must be a positive number of "
                         f"microseconds")
    return us


def _spec_gain(where: str, value) -> float:
    db = _spec_scalar(where, float, value)
    if not (math.isfinite(db) and db >= 0):
        raise ValueError(f"{where} {db:g} must be 0 dB or more")
    return db


def _parse_trigger(raw, backend: str) -> TriggerSpec:
    missing = (f"camera.trigger.line is required for camera_backend "
               f"{backend}: it names the camera input the trigger wire is on "
               f"(run 'uv run probe_flir.py --find-line' to find it).")
    if raw is None:
        raise ValueError(missing)
    d = _check_keys(raw, _TRIGGER_KEYS, "camera.trigger")
    if d.get("line") is None:
        raise ValueError(missing)
    delay = _spec_scalar("camera.trigger.delay_us", float, d.get("delay_us"),
                         nullable=True)
    if delay is not None and not (math.isfinite(delay) and delay >= 0):
        raise ValueError(f"camera.trigger.delay_us {delay:g} must be 0 or "
                         f"more microseconds, or null to leave it")
    return TriggerSpec(
        line=_spec_trigger_line("camera.trigger.line", d["line"]),
        activation=_spec_choice("camera.trigger.activation",
                                d.get("activation", "RisingEdge"),
                                TRIGGER_ACTIVATIONS),
        overlap=_spec_choice("camera.trigger.overlap",
                             d.get("overlap", "ReadOut"), TRIGGER_OVERLAPS),
        delay_us=delay)


def _parse_per_camera(raw) -> tuple:
    if raw is None:
        return ()
    if not isinstance(raw, dict):
        raise ValueError(f"camera.per_camera: expected a mapping of serial -> "
                         f"settings, got {type(raw).__name__}")
    pairs = []
    for key, value in raw.items():
        # A bare number is refused for the reason _coerce_serial gives: YAML
        # reads a leading-zero serial as octal, so it would name a camera
        # that does not exist.
        if not isinstance(key, str) or not key.strip():
            raise ValueError(
                f"camera.per_camera key {key!r} must be a quoted serial, "
                f"written as in camera_serials: YAML reads a bare number as "
                f"an integer, and a leading-zero one as octal.")
        serial = key.strip()
        where = f"camera.per_camera[{serial!r}]"
        if value is None:
            raise ValueError(f"{where} is empty; give the settings this "
                             f"camera overrides, or remove the entry")
        d = _check_keys(value, _PER_CAMERA_KEYS, where)
        spec = PerCameraSpec(
            exposure_us=(None if d.get("exposure_us") is None else
                         _spec_exposure(f"{where}.exposure_us",
                                        d["exposure_us"])),
            gain_db=(None if d.get("gain_db") is None else
                     _spec_gain(f"{where}.gain_db", d["gain_db"])),
            offset_x=(None if d.get("offset_x") is None else
                      _spec_offset(f"{where}.offset_x", d["offset_x"])),
            offset_y=(None if d.get("offset_y") is None else
                      _spec_offset(f"{where}.offset_y", d["offset_y"])),
            trigger_line=(None if d.get("trigger_line") is None else
                          _spec_trigger_line(f"{where}.trigger_line",
                                             d["trigger_line"])))
        pairs.append((serial, spec))
    serials = [s for s, _ in pairs]
    if len(set(serials)) != len(serials):
        raise ValueError(f"camera.per_camera lists a serial twice: {serials}")
    return tuple(sorted(pairs, key=lambda p: p[0]))


def _parse_flir(raw) -> FlirSpec:
    if raw is None:
        return FlirSpec()
    d = _check_keys(raw, _FLIR_KEYS, "camera.flir")
    w = "camera.flir."
    limit = d.get("link_throughput_limit", "auto")
    if isinstance(limit, str) and limit.strip() in FLIR_LINK_LIMIT_WORDS:
        limit = limit.strip()
    elif limit is not None:
        try:
            limit = _spec_scalar(w + "link_throughput_limit", int, limit)
        except ValueError:
            limit = 0
        if limit <= 0:
            raise ValueError(
                f"{w}link_throughput_limit {d['link_throughput_limit']!r} "
                f"must be a positive number of bytes per second, auto, max, "
                f"or null to leave the camera's value")
    packet = _spec_scalar(w + "packet_size", int, d.get("packet_size"),
                          nullable=True)
    lo, hi = FLIR_PACKET_SIZE_RANGE
    if packet is not None and not lo <= packet <= hi:
        raise ValueError(f"{w}packet_size {packet} is outside {lo}..{hi} "
                         f"bytes")
    delay = _spec_scalar(w + "packet_delay", int, d.get("packet_delay"),
                         nullable=True)
    if delay is not None and delay < 0:
        raise ValueError(f"{w}packet_delay {delay} must be 0 or more")
    black = _spec_scalar(w + "black_level", float, d.get("black_level"),
                         nullable=True)
    if black is not None and not math.isfinite(black):
        raise ValueError(f"{w}black_level must be a finite number")
    sdk_dir = _spec_scalar(w + "sdk_dir", str, d.get("sdk_dir"), nullable=True)
    return FlirSpec(
        user_set=_spec_choice(w + "user_set", d.get("user_set", "Default"),
                              FLIR_USER_SETS),
        gamma_enable=_spec_scalar(w + "gamma_enable", bool,
                                  d.get("gamma_enable", False)),
        black_level=black,
        adc_bit_depth=_spec_choice(w + "adc_bit_depth", d.get("adc_bit_depth"),
                                   FLIR_ADC_BIT_DEPTHS, nullable=True),
        link_throughput_limit=limit,
        packet_size=packet,
        packet_delay=delay,
        stream_mode=_spec_choice(w + "stream_mode",
                                 d.get("stream_mode", "auto"),
                                 FLIR_STREAM_MODES),
        extended_ids=_spec_scalar(w + "extended_ids", bool,
                                  d.get("extended_ids", True)),
        block_id_source=_spec_choice(w + "block_id_source",
                                     d.get("block_id_source", "auto"),
                                     FLIR_BLOCK_ID_SOURCES),
        timestamp_source=_spec_choice(w + "timestamp_source",
                                      d.get("timestamp_source", "auto"),
                                      FLIR_TIMESTAMP_SOURCES),
        sdk_dir=_resolve_path(sdk_dir.strip()) if sdk_dir else None)


def parse_camera_spec(raw, backend: str) -> CameraSpec | None:
    """The profile's ``camera:`` block as a CameraSpec, or None for none.

    ``backend`` is the profile's camera_backend. A known backend that takes
    its settings from elsewhere (Basler: the .pfs) refuses the block. The
    checks here are the ones that hold for every camera: types, enumerations,
    ranges that do not depend on the camera, and unknown keys at every level.
    Checks against another profile field (the exposure against frame_rate,
    per_camera keys against camera_serials) are in ``RigProfile.validate``,
    and checks against the camera's own node ranges run at open. Raises
    ValueError whose text starts with the key path (``camera.trigger.line``).
    """
    if raw is None:
        return None
    if backend in KNOWN_BACKENDS and backend not in _CAMERA_BLOCK_BACKENDS:
        raise ValueError(_block_refused_message(backend))
    d = _check_keys(raw, _CAMERA_KEYS, "camera")
    for key, what in (("exposure_us", "exposure"), ("gain_db", "gain")):
        if d.get(key) is None:
            raise ValueError(f"camera.{key} is required for camera_backend "
                             f"{backend}: {_required_reason(backend, what)}.")
    pixel_format = _spec_scalar("camera.pixel_format", str,
                                d.get("pixel_format", "Mono8")).strip()
    if pixel_format not in PIXEL_FORMATS:
        raise ValueError(
            f"camera.pixel_format {pixel_format!r} is not supported: the "
            f"capture path is 8-bit end to end, and a wider format is cut to "
            f"its low 8 bits, which turns every frame into noise. Use "
            f"pixel_format: Mono8.")
    return CameraSpec(
        exposure_us=_spec_exposure("camera.exposure_us", d["exposure_us"]),
        gain_db=_spec_gain("camera.gain_db", d["gain_db"]),
        trigger=_parse_trigger(d.get("trigger"), backend),
        pixel_format=pixel_format,
        offset_x=_spec_offset("camera.offset_x", d.get("offset_x", "center")),
        offset_y=_spec_offset("camera.offset_y", d.get("offset_y", "center")),
        per_camera=_parse_per_camera(d.get("per_camera")),
        flir=_parse_flir(d.get("flir")))


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
    # during capture so only frames every camera caught get encoded. Videos come
    # out trigger-aligned with no post-hoc re-encode, and the block-ID rate
    # check runs when the recording stops. The default is true so a profile that
    # omits the key gets that path; false selects post-hoc alignment
    # (gui_app/alignment.py).
    realtime_kick: bool = True
    # Kick-out coordinator depth, in frames: how far a camera may lag the
    # others before its missing triggers are force-dropped to keep the pipeline
    # flowing. Higher sacrifices fewer late frames and holds more RAM, because
    # the NV12 ring is kick_max_lag + grab_thread.ENCODE_QUEUE_DEPTH +
    # grab_thread.KICK_RING_SLACK frames per camera. validate() refuses a value
    # above max_num_buffer: the lagging camera's backlog waits in the driver
    # pool, so a shallower pool loses frames the coordinator would have waited
    # for.
    kick_max_lag: int = 240
    # Basler GigE receive driver: "socket" (user-space, robust packet resends),
    # "filter" (in-kernel pylon GigE Vision driver, less CPU, but with default
    # resend settings it discards a frame on a lost packet), or "auto" (leave
    # pylon's default). A flir profile refuses any value but auto; its
    # equivalent is camera.flir.stream_mode.
    gige_driver: str = "socket"
    # Camera backend name, resolved by gui_app.backends.load_backend and
    # checked against gui_app.backends.KNOWN_BACKENDS at load. The profile
    # selects the vendor, so porting a rig is a profile edit.
    camera_backend: str = "basler"
    # Serial numbers of the cameras this rig consists of, as quoted strings in
    # ascending order, or None to open every enumerated camera. When set,
    # open_all opens only these serials and refuses to start if any of them
    # is missing, so a replacement camera cannot slip into a session under a
    # calibrated camera's name. cam{i+1} is entry i of this list, and an
    # enumerated device the list does not name is ignored. validate() requires
    # ascending (string) order because that is the order the backend
    # enumerates in: the list then names every camera exactly as the rig names
    # it without the list, so adding or removing it never moves a calibration's
    # extrinsics onto a different physical camera.
    # Serials must be quoted: YAML reads a bare leading-zero number as octal,
    # so an unquoted serial can load as a different number and validate.
    camera_serials: list | None = None
    # Video encoder: "auto" (NVENC when a session is free, else libx264),
    # "nvenc", "x264" or "raw" (raw.bin + post-hoc encode). Declared here so
    # a rig without an NVIDIA GPU is a profile edit, not a code edit.
    encoder: str = "auto"
    # Basler GigE bandwidth reserve applied at open, or None to keep each
    # camera's .pfs value. The percentage (GevSCBWR) is the share of link
    # bandwidth held back for packet resends; the accumulation (GevSCBWRA) is
    # how many reserve slots may pool. Both are per-rig network facts, so they
    # belong here rather than in the shared camera file. A flir profile
    # refuses both: FLIR cameras have no equivalent node.
    gev_bandwidth_reserve_pct: float | None = None
    gev_bandwidth_reserve_accum: int | None = None
    # Metadata fields the sidebar pre-fills for a new session. The code default
    # is blank so that no operator's initials or assay can be written into
    # the session_metadata.json of a rig whose operator does not notice the
    # field; lab-specific values live in the profile.
    metadata_defaults: dict = field(default_factory=_default_metadata)
    # Basler pylon feature file (.pfs) every camera loads at open: the source
    # of a basler profile's recording exposure, gain and ROI. A flir profile
    # states its camera settings in the camera: block instead and refuses a
    # non-empty pfs_path.
    pfs_path: str = ""
    output_dir: str = ""
    board_config: str = ""
    # Trigger-board serial port. No code default: the device name is per-host
    # (COMn on Windows, /dev/tty* elsewhere), so a profile must state it and an
    # empty value fails at the first serial open instead of guessing.
    serial_port: str = ""
    trigger_pins: list = field(default_factory=lambda: [2, 4, 6, 8, 10, 12])
    # Expected camera count. 0 = don't check. Nonzero makes open_all refuse any
    # other number of cameras available to open, counted after camera_serials
    # has filtered the enumeration. Without camera_serials the names follow
    # enumeration order, so a camera that fails to enumerate would rename
    # every camera after it and attach the calibration extrinsics to the wrong
    # physical cameras.
    n_cameras: int = 0
    # Driver-side buffers queued per camera, and the largest single RAM
    # consumer: the pool alone is n_cameras x max_num_buffer x frame_width x
    # frame_height bytes, before the NV12 ring is counted. It is a profile
    # field (not a camera_manager constant) so the capacity preflight's advice
    # ("Lower max_num_buffer or kick_max_lag") can be followed. In kick-out
    # mode it must be at least kick_max_lag (see there).
    #
    # A deep pool absorbs network jitter, and it also hides a host that is
    # falling behind: nothing errors while the pool fills, and each frame
    # retrieved is staler than the last. Lower it for RAM, not for speed, and
    # read the buffer-underrun count in the session's stream statistics
    # afterwards: nonzero means the pool ran dry because the host could not
    # keep up.
    max_num_buffer: int = 1000
    # --- Calibration coverage HUD: when has enough board been captured? -------
    # These decide how long someone stands in the arena waving, so they are
    # worth setting against what the solve consumes rather than by feel.
    # 1_calibrate.py caps intrinsics at 60 pose-diverse frames per camera and
    # stereo at 30 shared frames per pair; everything beyond those caps is
    # discarded, contributing only a slightly richer pool to sample from. The
    # defaults therefore sit at roughly 2x and 1.3x the caps, which is margin;
    # about 4x and 2.7x the caps makes a many-camera calibration take far
    # longer than the data can be used for. Raise them if calibrations come
    # out marginal; the per-pair chart in reprojection_error_histogram.png is
    # the evidence.
    calibration_min_per_cam_shared: int = 120
    calibration_min_edge: int = 40
    # Quadrants of its own field of view each camera must see the board in.
    # This is the criterion that prevents the degenerate intrinsics of a board
    # waved in one spot, and it is cheap to satisfy, so relax it last.
    calibration_min_grid_cells: int = 3
    # Pin each grab thread to a performance core and raise its priority.
    # On a hybrid CPU (P-cores + E-cores) with many cameras the scheduler has
    # to place most busy threads on E-cores, and picks differently each
    # launch, which is the shape of the rotating laggard. See cpu_affinity.py.
    # No effect on a non-hybrid CPU or off Windows.
    pin_capture_threads: bool = False
    # Confine encoder threads to the P-core set: not one per core, and not the
    # E-cores. Distinct from pin_encoder_threads below, which confines them to
    # the E-core set.
    #
    # Leaving encoders unpinned lets Windows place one on an E-core, and a
    # single E-core cannot always sustain encode submission for one camera's
    # stream. That encoder's queue then fills and the grab threads block on
    # ring slots the coordinator cannot release, which slows every camera.
    # Placement of unpinned encoders changes with each acquisition, so one
    # recording in a GUI session can pass and the next one fail.
    encoder_pcores: bool = False
    # Seconds between temperature polls while acquiring; 0 disables the check.
    # How hot a camera runs depends on its mounting and airflow, and a camera
    # that reaches its shutdown temperature stops delivering mid-session, so
    # the poll is what lets the operator act before that happens. Thresholds
    # are never hardwired here: each backend's thermals() reports the camera's
    # own status and shutdown temperature, so this works on any model. A
    # register read per camera is a cold path, hence the slow default rather
    # than the preview timer.
    thermal_poll_s: float = 20.0
    # Degrees C below the shutdown temperature a camera reports
    # (thermals()["temp_shutdown_c"]) at which the live thermal warning starts.
    # The margin is a profile field because how fast a camera heats is a
    # property of the installation; the shutdown point itself always comes
    # from the camera. A camera that reports no shutdown point is judged by its
    # own temperature status instead.
    thermal_warn_margin_c: float = 3.0
    # Logical CPUs that capture threads are kept off, when pinning is enabled.
    # CPU 0 is the Windows boot processor and the default target for timer and
    # DPC work, so a grab thread pinned there is descheduled by exactly the
    # network traffic it is trying to receive. With grab threads pinned, lag
    # behind the leader as median/p95/max, measured on the nine-camera
    # reference rig (the victim follows the core, not the camera):
    #   exclude nothing (cam1 on CPU 0)   cam1 0/6/12, others 0/1/1
    #   exclude [0]     (cam1 on CPU 1)   cam1 0/3/4,  others 0/1/1
    #   exclude [0, 1]                    all nine 0/1/1
    # Under the GUI the unexcluded case was far worse than headless: the same
    # camera diverged to kick_max_lag and was force-dropped.
    capture_core_exclude: list = field(default_factory=lambda: [0])
    # Confine encoder threads to the E-core set. Default off because it
    # measured worse than unpinned encoders on the nine-camera reference rig,
    # with grab threads pinned in every arm:
    #   encoders unpinned          avg_proc 2.19 ms  slack 7.03  worst lag 10
    #   encoders on the E-core set          2.53        6.62               10
    #   encoders one per E-core             3.48        5.52              321
    # A single E-core cannot always sustain encode submission for one camera's
    # stream, so that camera backs up and drags its grab thread with it. Kept
    # as a knob for a rig with more cameras than P-cores.
    pin_encoder_threads: bool = False
    # Optostim output pins held LOW from the instant the sketch boots — before
    # the serial handshake, which blocks until the GUI connects. Without this a
    # powered laser driver reads the floating pin as ON at power-up. Pins used by
    # the stimulation workflow are added automatically; list here anything that
    # must be safe even when no paradigm is loaded.
    stim_safe_pins: list = field(default_factory=lambda: [53])
    # Calibration-only exposure/gain. The ChArUco board often needs far more
    # light than the experiment does, especially when the room is dimmed to
    # keep a wireless optostim receiver from triggering. Calibration can afford
    # it, because the calibration frame rate leaves a longer trigger period.
    # camera_manager.apply_exposure_gain() clamps the exposure to 90% of the
    # camera's exposure ceiling at the acquisition's frame rate (the backend's
    # exposure_ceiling_us) and logs `CLAMPED from ...` when it lowers a value.
    # These apply to calibration only; the recording values (the .pfs, or the
    # camera: block) are restored for recording, so a long calibration
    # exposure cannot reach a recording, where it would make the camera ignore
    # triggers.
    # calibration_exposure_us 0 keeps the recording exposure, and
    # calibration_gain_db -1 keeps the recording gain (0 dB is a real gain).
    calibration_exposure_us: float = 0.0
    calibration_gain_db: float = -1.0
    # Basler only: AcquisitionFrameRate written in trigger mode, or 0 to
    # disable the limiter. While triggered, the limiter still enforces a
    # minimum frame interval of exposure + 1/trigger_rate_limit, which sets the
    # exposure ceiling, and it paces each camera's readout onto the link.
    # validate() refuses a frame rate at or above it, because the camera would
    # ignore triggers. A flir profile refuses the field; FLIR pacing is
    # camera.flir.link_throughput_limit.
    trigger_rate_limit: float = 165.0
    # Camera settings for a backend with no settings file of its own (FLIR):
    # exposure, gain, trigger line, ROI offsets and the Spinnaker-only nodes.
    # Parsed by parse_camera_spec; required for flir and flir_sim, accepted
    # by sim, refused for basler, whose settings are the .pfs.
    camera: CameraSpec | None = None
    # Worker processes the cameras are captured in; 0 captures in this
    # process. Cameras are dealt to workers contiguously by camera index.
    # Multi-process capture runs only on the real-time kick-out path, so
    # validate() refuses a nonzero value unless realtime_encode and
    # realtime_kick are both true.
    capture_processes: int = 0
    # How the NVENC encoder receives each frame; one of NVENC_UPLOAD_MODES.
    # "host" hands PyNvVideoCodec the ring slot, and PyNvVideoCodec copies it
    # to the GPU with the GIL held. "pinned" copies the frame into page-locked
    # memory without the GIL first. No effect when the frames are encoded on
    # the CPU.
    nvenc_upload: str = "host"
    # CUDA context the pinned upload path runs its encoders in; one of
    # NVENC_CONTEXT_MODES. "own" costs GPU memory per encoder. It applies to
    # nvenc_upload: pinned only, so validate() refuses "own" with "host".
    nvenc_context: str = "shared"

    #: The keys the profile file sets, recorded by ``load``. A dataclass
    #: default cannot tell a key the file left out from one it set to the
    #: default value, and the backend-aware refusals in ``validate`` need that
    #: difference: ``trigger_rate_limit: 165`` in a flir profile is refused,
    #: an absent one is not. A profile built in code has none, and ``validate``
    #: then treats a field as set when it differs from its default.
    _explicit: typing.ClassVar[frozenset] = frozenset()

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
            if key == "camera":
                continue
            try:
                kwargs[key] = _coerce_field(known[key], raw)
            except (TypeError, ValueError) as e:
                raise ProfileError(f"{path.name}: field {key!r}: {e}") from None
        # The camera: block is parsed after camera_backend, because whether
        # the backend takes a block at all decides the first refusal. Its
        # messages already start with the key path (camera.trigger.line), so
        # only the file name is prefixed.
        if "camera" in data:
            backend = kwargs.get(
                "camera_backend", known["camera_backend"].default)
            try:
                kwargs["camera"] = _coerce_field(known["camera"],
                                                 data["camera"], backend)
            except (TypeError, ValueError) as e:
                raise ProfileError(f"{path.name}: {e}") from None
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
        profile._explicit = frozenset(str(k) for k in data)
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
        a pool too shallow for the kick-out depth, or a field that belongs to
        another camera vendor. Paths are not checked here (a profile must load
        on a machine that lacks them); ``settings_ready`` checks them before
        the cameras open.
        """
        self._validate_backend()
        if self.gige_driver not in GIGE_DRIVERS:
            raise ValueError(
                f"gige_driver {self.gige_driver!r} is not one of "
                f"{list(GIGE_DRIVERS)}; an unknown name would fall through to "
                f"pylon's default driver")
        if self.encoder not in ENCODERS:
            raise ValueError(
                f"encoder {self.encoder!r} is not one of {list(ENCODERS)}")

        pins = set(self.trigger_pins)
        on_serial = sorted(pins & set(RESERVED_SERIAL_PINS))
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
            # Only a backend with that limiter has the rule; a FLIR camera's
            # limits are checked against the camera at open.
            if (self.camera_backend in _LIMITER_BACKENDS
                    and limit and fps >= limit):
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
        margin = self.thermal_warn_margin_c
        if not (math.isfinite(margin) and margin > 0):
            raise ValueError(
                f"thermal_warn_margin_c {margin:g} must be a positive number "
                f"of degrees C: the thermal warning starts this far below the "
                f"shutdown temperature each camera reports")
        if self.nvenc_upload not in NVENC_UPLOAD_MODES:
            raise ValueError(
                f"nvenc_upload {self.nvenc_upload!r} is not one of "
                f"{list(NVENC_UPLOAD_MODES)}")
        if self.nvenc_context not in NVENC_CONTEXT_MODES:
            raise ValueError(
                f"nvenc_context {self.nvenc_context!r} is not one of "
                f"{list(NVENC_CONTEXT_MODES)}")
        if self.nvenc_context != "shared" and self.nvenc_upload != "pinned":
            raise ValueError(
                f"nvenc_context {self.nvenc_context!r} chooses the CUDA "
                f"context of the pinned upload path, so it has no effect with "
                f"nvenc_upload {self.nvenc_upload!r}. Set nvenc_upload: "
                f"pinned, or remove nvenc_context.")
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
            # cam{i+1} is entry i of the list. Ascending (string) order is the
            # order the backend enumerates in, so the list names every camera
            # as the rig names it without the list, and a calibration taken
            # with or without it attaches to the same physical cameras.
            if self.camera_serials != sorted(self.camera_serials):
                raise ValueError(
                    f"camera_serials {self.camera_serials} is not in ascending "
                    f"order. cam1..camN are named by position in this list, "
                    f"and ascending order keeps those names the same as the "
                    f"serial order the cameras enumerate in without it, so "
                    f"list them as {sorted(self.camera_serials)}")
            if self.n_cameras and self.n_cameras != len(self.camera_serials):
                raise ValueError(
                    f"n_cameras {self.n_cameras} disagrees with the "
                    f"{len(self.camera_serials)} entries in camera_serials")
        self._validate_capture_processes()
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
        self._validate_camera()

    # -------------------------------------------------- validation helpers
    def _given(self, name: str) -> bool:
        """True when the profile sets field ``name``: the key is in the file
        it was loaded from, or its value differs from the code default."""
        if name in self._explicit:
            return True
        f = type(self).__dataclass_fields__[name]
        default = (f.default_factory() if f.default is dataclasses.MISSING
                   else f.default)
        return getattr(self, name) != default

    def _validate_backend(self) -> None:
        """The backend name, and the fields that belong to another vendor.

        A flir profile refuses each Basler-only field it sets, with the FLIR
        equivalent in the message. Ignoring them instead would leave a
        profile that reads as configuring a limiter, a receive driver or a
        bandwidth reserve that no camera applies.
        """
        backend = self.camera_backend
        if not isinstance(backend, str) or not backend:
            raise ValueError("camera_backend must be a non-empty backend name")
        if backend not in KNOWN_BACKENDS:
            raise ValueError(
                f"camera_backend {backend!r} is not a known backend. Known "
                f"backends: {', '.join(KNOWN_BACKENDS)}. A new one is a "
                f"module in gui_app/backends/, registered in KNOWN_BACKENDS.")
        if backend not in _CAMERA_BLOCK_REQUIRED:
            return
        if self.pfs_path:
            raise ValueError(
                f"pfs_path is a Basler pylon feature file; a {backend} profile "
                f"describes its cameras in the camera: block. Remove pfs_path.")
        if self._given("trigger_rate_limit"):
            raise ValueError(
                "trigger_rate_limit sets a Basler camera's "
                "AcquisitionFrameRate limiter, which FLIR cameras do not "
                "have. FLIR pacing is camera.flir.link_throughput_limit (and "
                "packet_delay on GigE). Remove trigger_rate_limit.")
        if self._given("gige_driver") and self.gige_driver != "auto":
            raise ValueError(
                f"gige_driver chooses the pylon GigE driver; on FLIR the "
                f"equivalent is camera.flir.stream_mode "
                f"({', '.join(FLIR_STREAM_MODES)}). Remove gige_driver.")
        for name, node in (("gev_bandwidth_reserve_pct", "GevSCBWR"),
                           ("gev_bandwidth_reserve_accum", "GevSCBWRA")):
            if getattr(self, name) is not None:
                raise ValueError(
                    f"{name} is a Basler GigE node ({node}); FLIR cameras "
                    f"have no equivalent. Remove it.")

    def _validate_capture_processes(self) -> None:
        n = self.capture_processes
        if n < 0:
            raise ValueError(
                f"capture_processes {n} must be 0 (capture in this process) "
                f"or a positive number of worker processes")
        if not n:
            return
        count = self.n_cameras or len(self.camera_serials or ())
        if count and n > count:
            raise ValueError(
                f"capture_processes {n} is more than the {count} cameras the "
                f"profile expects (n_cameras); each worker needs a camera")
        if not (self.realtime_encode and self.realtime_kick):
            raise ValueError(
                f"capture_processes {n} needs realtime_encode: true and "
                f"realtime_kick: true, because multi-process capture runs only "
                f"on the real-time kick-out path. Turn both on, or set "
                f"capture_processes: 0.")

    def _validate_camera(self) -> None:
        """The camera: block against the backend and the other fields."""
        backend = self.camera_backend
        spec = self.camera
        if spec is None:
            if backend in _CAMERA_BLOCK_REQUIRED:
                raise ValueError(
                    f"camera block is required for camera_backend {backend}: "
                    f"FLIR cameras have no .pfs, so the recording exposure, "
                    f"gain and trigger line must be written in a camera: "
                    f"block. profiles/templates/ has examples.")
            return
        if backend not in _CAMERA_BLOCK_BACKENDS:
            raise ValueError(_block_refused_message(backend))
        period_us = 1e6 / self.frame_rate
        exposures = [("camera.exposure_us", spec.exposure_us)]
        exposures += [(f"camera.per_camera[{s!r}].exposure_us", o.exposure_us)
                      for s, o in spec.per_camera if o.exposure_us is not None]
        for where, us in exposures:
            if us >= period_us:
                raise ValueError(
                    f"{where} {us:g} cannot fit a frame_rate {self.frame_rate} "
                    f"trigger period ({period_us:g} us). Lower the exposure "
                    f"or the frame rate.")
        if spec.per_camera:
            if self.camera_serials is None:
                raise ValueError(
                    "camera.per_camera names cameras by serial, so the profile "
                    "must list them in camera_serials.")
            listed = set(self.camera_serials)
            for serial, _ in spec.per_camera:
                if serial not in listed:
                    raise ValueError(f"camera.per_camera key {serial!r} is not "
                                     f"in camera_serials.")

    def settings_ready(self) -> str | None:
        """Why this profile cannot open its cameras yet, or None when it can.

        ``validate`` checks what the file says; this checks what it points
        at, which can change between loading a profile and opening its
        cameras: a .pfs deleted or never copied, an SDK folder moved. Callers
        show the text in a dialog and do not open. A basler profile needs its
        .pfs; a flir profile needs its camera: block and, when it names one,
        its camera.flir.sdk_dir; the simulated backends need nothing.
        """
        backend = self.camera_backend
        if backend in _PFS_BACKENDS:
            pfs = self.pfs_path
            if not pfs or not Path(pfs).exists():
                return (f"The profile's camera settings file is missing:\n"
                        f"{pfs or '(not set)'}\n\nSet pfs_path in the profile "
                        f"YAML to a file in configs/.")
            return None
        if backend in _CAMERA_BLOCK_REQUIRED:
            if self.camera is None:
                return (f"The profile has no camera: block. A {backend} "
                        f"profile states each camera's exposure, gain and "
                        f"trigger line there; profiles/templates/ has "
                        f"examples.")
            sdk = self.camera.flir.sdk_dir
            if backend == "flir" and sdk and not Path(sdk).is_dir():
                return (f"camera.flir.sdk_dir is not a folder:\n{sdk}\n\n"
                        f"Point it at the Spinnaker SDK folder that holds the "
                        f"Spinnaker C library, or remove it to search the "
                        f"default install locations.")
            return None
        if backend in KNOWN_BACKENDS:
            return None
        return (f"camera_backend {backend!r} is not a known backend. Known "
                f"backends: {', '.join(KNOWN_BACKENDS)}.")

    @staticmethod
    def list_profiles() -> list[Path]:
        """The profiles the dropdown offers: ``profiles/*.yaml`` only.

        ``profiles/templates/`` is not listed. Its files are examples with
        placeholder serials, meant to be copied into ``profiles/`` and edited,
        so none of them can be selected and run as it stands.
        """
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


def _coerce_field(f: dataclasses.Field, value, backend: str | None = None):
    """Coerce a YAML value to the declared type of dataclass field ``f``.

    ``camera`` is parsed by ``parse_camera_spec``, which needs the profile's
    camera_backend: the generic mapping branch below would turn every value
    of the block into text.
    """
    if f.name == "camera":
        return parse_camera_spec(value, backend or "")
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
