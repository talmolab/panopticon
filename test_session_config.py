"""Profile loading and session configuration must refuse what the rig cannot run.

A profile typo used to take the field's default silently, so a misspelled
``realtime_kick`` recorded a whole session on the wrong pipeline. Session
fields typed into the sidebar became directory names unchecked. Both are
exercised here without cameras, a board or Qt. Every way a profile file can
fail to load must surface as ProfileError, because the sidebar loads every
file in the profiles directory and one bad file must not abort launch.
SessionConfig construction never raises; ``validate()`` does, so the entry
points can refuse a bad field with a dialog instead of a traceback in a slot.

The two shipped profiles are asserted field by field so a stray edit cannot
change the rig's effective configuration without this test noticing.

    uv run python test_session_config.py
"""
import dataclasses
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gui_app import session_config as sc
from gui_app.session_config import (
    RigProfile, SessionConfig, ProfileError, validate_path_component,
    validate_session_date)
from gui_app import rig_setup

failures = []
_n = 0


def check(name, cond, detail=""):
    global _n
    _n += 1
    print(f"{_n}) {name}: {'PASS' if cond else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


def raises(exc, fn, *args, **kwargs):
    """The exception raised by fn(), or None."""
    try:
        fn(*args, **kwargs)
    except exc as e:
        return e
    return None


_tmp = tempfile.TemporaryDirectory()
TMP = Path(_tmp.name)


def write_profile(text, stem="test_profile"):
    p = TMP / f"{stem}.yaml"
    p.write_text(text, encoding="utf-8")
    return p


def load(text, stem="test_profile"):
    return RigProfile.load(write_profile(text, stem))


# --- the shipped profiles ---------------------------------------------------
pose = RigProfile.load(sc.PROFILES_DIR / "3dpose.yaml")
expect_pose = dict(
    name="3dpose", frame_width=1920, frame_height=1200, frame_rate=100,
    calibration_frame_rate=30, quality=21, encode_parallel=3,
    realtime_encode=True, realtime_kick=True, kick_max_lag=480,
    max_num_buffer=600, n_cameras=9, gige_driver="socket",
    camera_backend="basler", encoder="auto", serial_port="COM3",
    trigger_pins=[2, 4, 6, 8, 10, 12], stim_safe_pins=[53],
    trigger_rate_limit=165.0, calibration_exposure_us=5000.0,
    calibration_gain_db=-1.0, calibration_min_per_cam_shared=120,
    calibration_min_edge=20, calibration_min_grid_cells=3,
    pin_capture_threads=True, encoder_pcores=False, pin_encoder_threads=False,
    capture_core_exclude=[0, 1], thermal_poll_s=20.0, camera_serials=None,
    gev_bandwidth_reserve_pct=None, gev_bandwidth_reserve_accum=None,
    metadata_defaults={"experimenter": "IT", "assay": "open_field"},
)
diff = {k: (getattr(pose, k), v) for k, v in expect_pose.items()
        if getattr(pose, k) != v}
check("3dpose.yaml loads with its rig values (480 / 600 / 9 cameras)",
      not diff, str(diff))
check("3dpose paths resolve against the repository root",
      Path(pose.pfs_path).is_absolute()
      and pose.pfs_path.endswith("mono8_1920x1200.pfs")
      and Path(pose.board_config).is_absolute()
      and Path(pose.output_dir) == sc.REPO_ROOT / "data",
      f"{pose.pfs_path} {pose.output_dir}")
check("every 3dpose field is a real int/float/bool where declared",
      all(isinstance(getattr(pose, f.name), f.type)
          for f in dataclasses.fields(RigProfile)
          if f.type in (int, float, bool, str)))

face = RigProfile.load(sc.PROFILES_DIR / "3dface.yaml")
expect_face = dict(
    name="3dface", frame_width=1280, frame_height=1024, frame_rate=100,
    calibration_frame_rate=30, kick_max_lag=240, max_num_buffer=1000,
    n_cameras=6, camera_backend="basler", encoder="auto",
    stim_safe_pins=[], trigger_pins=[2, 4, 6, 8, 10, 12],
    calibration_min_per_cam_shared=250, calibration_min_edge=80,
    pin_capture_threads=False, thermal_poll_s=20.0, trigger_rate_limit=165.0,
    metadata_defaults={"experimenter": "IT", "assay": ""},
)
diff = {k: (getattr(face, k), v) for k, v in expect_face.items()
        if getattr(face, k) != v}
check("3dface.yaml loads with n_cameras 6 and its pinned HUD thresholds",
      not diff, str(diff))
check("list_profiles finds both shipped profiles",
      {p.stem for p in RigProfile.list_profiles()} >= {"3dpose", "3dface"})

# --- defaults live in one place --------------------------------------------
minimal = load("name: bare\n", "bare")
expected = dataclasses.asdict(RigProfile(name="bare"))
check("a profile that sets only its name equals the dataclass defaults",
      dataclasses.asdict(minimal) == expected)
nameless = load("frame_rate: 50\n", "stemname")
check("a profile without a name takes the file stem",
      nameless.name == "stemname" and nameless.frame_rate == 50)
check("serial_port has no code default (per-host device name)",
      RigProfile().serial_port == "")
check("metadata_defaults code default is blank",
      RigProfile().metadata_defaults == {"experimenter": "", "assay": ""})

# --- unknown keys, empty and malformed files -------------------------------
e = raises(ProfileError, load, "name: t\nrealtime_kik: true\nkick_maxlag: 480\n")
check("a misspelled key is refused and named",
      e is not None and "realtime_kik" in str(e) and "kick_maxlag" in str(e),
      str(e)[:120])
check("ProfileError is a ValueError", issubclass(ProfileError, ValueError))
e = raises(ProfileError, load, "1: x\nbogus: y\nyes: z\n", "mixedkeys")
check("unknown keys of mixed types (int, str, bool) raise ProfileError, not TypeError",
      e is not None and "bogus" in str(e) and "'1'" in str(e), str(e)[:120])
e = raises(ProfileError, load, "name: [unclosed\nframe_rate: 100\n", "syntax")
check("a YAML syntax error raises ProfileError naming the file",
      e is not None and "syntax.yaml" in str(e), str(e).replace(chr(10), " ")[:100])
e = raises(ProfileError, load, "name: t\nkick_max_lag: !!python/object:os.system x\n", "tag")
check("an unsafe YAML tag raises ProfileError", e is not None, str(e)[:100])
_bin = TMP / "binary.yaml"
_bin.write_bytes(b"\xff\xfe\x00name: t\n")
e = raises(ProfileError, RigProfile.load, _bin)
check("a file that is not UTF-8 raises ProfileError", e is not None, str(e)[:100])
e = raises(ProfileError, RigProfile.load, TMP / "does_not_exist.yaml")
check("a missing profile file raises ProfileError", e is not None, str(e)[:100])
e = raises(ProfileError, load, "", "empty")
check("an empty profile file is refused", e is not None, str(e)[:80])
e = raises(ProfileError, load, "- a\n- b\n", "listy")
check("a top-level list is refused", e is not None, str(e)[:80])
e = raises(ProfileError, load, "just a string\n", "scalar")
check("a top-level scalar is refused", e is not None, str(e)[:80])
p = load("name: t\n# comments only otherwise\n", "commented")
check("a file with only comments and a name loads", p.name == "t")

# --- field validation -------------------------------------------------------
e = raises(ProfileError, load, "gige_driver: sockett\n")
check("gige_driver typo is refused",
      e is not None and "gige_driver" in str(e), str(e)[:100])
for d in ("socket", "filter", "auto"):
    check(f"gige_driver {d} is accepted", load(f"gige_driver: {d}\n").gige_driver == d)
e = raises(ProfileError, load, "trigger_pins: [1, 2, 4]\n")
check("a trigger on Serial0 (pin 1) is refused",
      e is not None and "Serial0" in str(e), str(e)[:100])
e = raises(ProfileError, load, "trigger_pins: [2, 53]\nstim_safe_pins: [53]\n")
check("a trigger on a stim_safe pin is refused",
      e is not None and "stim_safe_pins" in str(e), str(e)[:100])
check("pin 53 as a trigger is fine on a rig with no stim pins",
      load("trigger_pins: [2, 53]\nstim_safe_pins: []\n").trigger_pins == [2, 53])
e = raises(ProfileError, load, "trigger_pins: [2, 2]\n")
check("a duplicated trigger pin is refused", e is not None, str(e)[:80])
e = raises(ProfileError, load, "frame_rate: 165\n")
check("frame_rate >= trigger_rate_limit is refused",
      e is not None and "skip triggers" in str(e), str(e)[:100])
e = raises(ProfileError, load, "calibration_frame_rate: 200\n")
check("calibration_frame_rate >= trigger_rate_limit is refused",
      e is not None and "calibration_frame_rate" in str(e), str(e)[:100])
check("with the limiter off (0) a high frame rate loads",
      load("frame_rate: 200\ntrigger_rate_limit: 0\n").frame_rate == 200)
check("a limit just above the rate loads",
      load("frame_rate: 100\ntrigger_rate_limit: 100.5\n").trigger_rate_limit == 100.5)
e = raises(ProfileError, load, "encoder: hevc\n")
check("an unknown encoder is refused", e is not None and "encoder" in str(e))
for enc in ("auto", "nvenc", "x264", "raw"):
    check(f"encoder {enc} is accepted", load(f"encoder: {enc}\n").encoder == enc)
e = raises(ProfileError, load, "camera_backend: ''\n")
check("an empty camera_backend is refused", e is not None)
e = raises(ProfileError, load, "kick_max_lag: 0\n")
check("kick_max_lag 0 is refused", e is not None)
e = raises(ProfileError, load, "frame_rate: 0\n")
check("frame_rate 0 is refused", e is not None)

# --- coercion by field type -------------------------------------------------
p = load('frame_rate: "100"\nkick_max_lag: 480.0\nthermal_poll_s: 5\n'
         'pin_capture_threads: "yes"\ntrigger_pins: ["2", 4]\n')
check("quoted '100' becomes int 100", p.frame_rate == 100 and type(p.frame_rate) is int)
check("integral float 480.0 becomes int 480",
      p.kick_max_lag == 480 and type(p.kick_max_lag) is int)
check("int 5 becomes float for a float field",
      p.thermal_poll_s == 5.0 and type(p.thermal_poll_s) is float)
check("quoted 'yes' becomes True for a bool field", p.pin_capture_threads is True)
check("trigger pin elements are ints", p.trigger_pins == [2, 4])
e = raises(ProfileError, load, "frame_rate: abc\n")
check("a non-numeric frame_rate is refused with the field named",
      e is not None and "frame_rate" in str(e), str(e)[:100])
e = raises(ProfileError, load, "kick_max_lag: true\n")
check("a bool for an int field is refused", e is not None)
e = raises(ProfileError, load, "kick_max_lag: 480.5\n")
check("a fractional value for an int field is refused", e is not None)
e = raises(ProfileError, load, "realtime_kick: 1\n")
check("an int for a bool field is refused", e is not None)
e = raises(ProfileError, load, "trigger_pins: 4\n")
check("a scalar for a list field is refused", e is not None)
e = raises(ProfileError, load, "kick_max_lag:\n")
check("an empty value for a required scalar is refused", e is not None)
p = load("camera_serials: ['41920544', \"41920545\"]\nn_cameras: 2\n")
check("quoted camera_serials load as the strings written",
      p.camera_serials == ["41920544", "41920545"])
e = raises(ProfileError, load, "camera_serials: [41920544, '41920545']\n")
check("a bare (unquoted) serial is refused with the field named",
      e is not None and "camera_serials" in str(e) and "quoted" in str(e), str(e)[:120])
# YAML 1.1 reads an unquoted leading-zero number as octal; stringifying it
# would produce a phantom serial that validates and never enumerates.
e = raises(ProfileError, load, "camera_serials: [01234567, '41920545']\n")
check("a leading-zero unquoted serial (octal in YAML) is refused, not stringified",
      e is not None and "quoted" in str(e), str(e)[:120] if e else "loaded")
e = raises(ProfileError, load, "camera_serials: ['', '41920545']\n")
check("an empty serial string is refused", e is not None)
e = raises(ProfileError, load, "camera_serials: ['41920545', '41920544']\n")
check("camera_serials out of ascending order is refused and the sorted list is shown",
      e is not None and "ascending" in str(e) and "['41920544', '41920545']" in str(e),
      str(e)[:140])
# A string sort is what the backend does; a numeric sort would differ for
# serials of unequal length, so the check must compare as strings.
e = raises(ProfileError, load, "camera_serials: ['9', '10']\n")
check("order is compared as strings ('10' sorts before '9'), matching the backend",
      e is not None and "ascending" in str(e), str(e)[:100])
check("a hand-built profile with int serials fails validate()",
      raises(ValueError, RigProfile(camera_serials=[1, 2]).validate) is not None)
e = raises(ProfileError, load, "camera_serials: ['1', '2', '3']\nn_cameras: 9\n")
check("camera_serials length must agree with a nonzero n_cameras",
      e is not None and "n_cameras" in str(e), str(e)[:100])
e = raises(ProfileError, load, "camera_serials: []\n")
check("an empty camera_serials list is refused", e is not None)
e = raises(ProfileError, load, "camera_serials: ['1', '1']\n")
check("a duplicated serial is refused", e is not None)
p = load("gev_bandwidth_reserve_pct: 15\ngev_bandwidth_reserve_accum: 4\n")
check("bandwidth reserve fields coerce to float / int",
      p.gev_bandwidth_reserve_pct == 15.0 and p.gev_bandwidth_reserve_accum == 4
      and type(p.gev_bandwidth_reserve_accum) is int)
check("bandwidth reserve fields default to None (keep the .pfs values)",
      RigProfile().gev_bandwidth_reserve_pct is None
      and RigProfile().gev_bandwidth_reserve_accum is None)
e = raises(ProfileError, load, "gev_bandwidth_reserve_pct: 150\n")
check("a reserve percentage over 100 is refused", e is not None)
p = load("metadata_defaults:\n  experimenter: AB\n")
check("metadata_defaults merges with the blank defaults",
      p.metadata_defaults == {"experimenter": "AB", "assay": ""})
e = raises(ProfileError, load, "metadata_defaults:\n  operator: AB\n")
check("an unknown metadata_defaults key is refused",
      e is not None and "operator" in str(e), str(e)[:100])

# --- SessionConfig ----------------------------------------------------------
sc._environment_metadata = lambda: {}   # keep the test offline and fast

cfg = SessionConfig.from_profile(pose, date="20260918", mouse_1="m1", mouse_2="m2",
                                 base_data_dir=TMP / "data")
check("from_profile fills experimenter/assay from metadata_defaults",
      cfg.experimenter == "IT" and cfg.assay == "open_field")
check("an override wins over metadata_defaults",
      SessionConfig.from_profile(pose, experimenter="ZZ").experimenter == "ZZ")
check("SessionConfig keeps a reference to its profile", cfg.profile is pose)
check("the two rates rate_for resolves are still filled from the profile",
      cfg.rate_for("calibration") == 30 and cfg.rate_for("recording") == 100)
# Every other rig fact has one source, the profile: a mirror drifts silently
# because nothing compares the copies.
for gone in ("serial_port", "trigger_pins", "n_cameras", "pfs_path",
             "frame_width", "frame_height", "quality", "encode_parallel",
             "realtime_encode", "realtime_kick", "kick_max_lag",
             "calibration_exposure_us", "calibration_gain_db"):
    check(f"SessionConfig no longer mirrors {gone}", not hasattr(cfg, gone))
check("the geometry in the metadata comes from the profile the cameras were "
      "configured from",
      cfg.metadata()["resolution"] == [pose.frame_width, pose.frame_height]
      and SessionConfig().metadata()["resolution"] is None,
      str(cfg.metadata()["resolution"]))
check("camera_names defaults to an empty list, not six cameras",
      SessionConfig().camera_names == [])
check("camera_thermals is a declared field defaulting to None",
      "camera_thermals" in {f.name for f in dataclasses.fields(SessionConfig)}
      and cfg.camera_thermals is None)
check("SessionConfig metadata defaults are blank in code",
      SessionConfig().experimenter == "" and SessionConfig().assay == "")
check("session_dir is base / date / m1_m2",
      cfg.session_dir == TMP / "data" / "20260918" / "m1_m2")

bad_components = {
    "leading slash": "/x", "drive letter": "C:", "parent ref": "..",
    "dot": ".", "nested": "a/b", "backslash": "a\\b", "question mark": "a?b",
    "pipe": "a|b", "trailing dot": "name.",
    "control char": "a\x07b", "reserved device": "CON",
    "reserved device with ext": "com1.txt", "too long": "x" * 81,
}
# Construction must never raise: _build_config runs inside Qt slots, and an
# exception there leaves the toggle ON. validate() is the explicit gate.
for label, value in bad_components.items():
    built = raises(Exception, SessionConfig, date="20260918", mouse_1=value)
    e = raises(ValueError, SessionConfig(date="20260918", mouse_1=value).validate)
    check(f"mouse_1 {label} constructs but validate() refuses it",
          built is None and e is not None and "mouse_1" in str(e),
          str(e)[:80] if e else "no error")
check("mouse_2 is validated too",
      raises(ValueError, SessionConfig(mouse_2="a/b").validate) is not None)
# cohort and cage are metadata values, not path components: session_dir is
# base/date/mouse_1_mouse_2, so a label with a separator or colon is fine.
c = SessionConfig(cohort=" A/3 ", cage="rack 2: left")
check("cohort and cage accept any text and are only stripped",
      c.cohort == "A/3" and c.cage == "rack 2: left"
      and c.validate() is c and c.metadata()["cage"] == "rack 2: left")
check("blank cohort and cage are allowed",
      SessionConfig(cohort="", cage="  ").cohort == "")
for bad_date in ("2026-09-18", "20261301", "abcdefgh", "2026918", "20260918/"):
    built = raises(Exception, SessionConfig, date=bad_date)
    check(f"date {bad_date!r} constructs but validate() refuses it",
          built is None
          and raises(ValueError, SessionConfig(date=bad_date).validate) is not None)
c = SessionConfig(date="", mouse_1="   ", mouse_2="")
check("blank date takes today; whitespace-only mouse ids take placeholders",
      len(c.date) == 8 and c.mouse_1 == "m1" and c.mouse_2 == "m2"
      and c.validate() is c)
c = SessionConfig(date=" 20260918 ", mouse_1=" M-12.a ", mouse_2="B_2 ")
check("valid fields are stripped and kept (a trailing space is stripped, not refused)",
      c.date == "20260918" and c.mouse_1 == "M-12.a" and c.session_id == "M-12.a_B_2")
check("validate() returns the config so it chains from from_profile",
      SessionConfig.from_profile(pose, date="20260918").validate().date == "20260918")
check("non-string identity fields are stringified, not refused, at construction",
      SessionConfig(date=20260918, mouse_1=7).validate().session_id == "7_m2")
check("a valid session_dir stays under base_data_dir",
      SessionConfig(date="20260918", mouse_1="a", mouse_2="b",
                    base_data_dir=TMP).session_dir.resolve()
      .is_relative_to(TMP.resolve()))
check("validate_path_component returns the stripped value",
      validate_path_component("  ok  ", "x") == "ok")
check("validate_session_date accepts a real date",
      validate_session_date("20240229") == "20240229")

# --- save_metadata placement ------------------------------------------------
cfg = SessionConfig.from_profile(pose, date="20260918", mouse_1="a", mouse_2="b",
                                 base_data_dir=TMP / "meta", camera_names=["cam1", "cam2"])
cfg.camera_thermals = [{"temp_max_c": 61.0}]
p1 = cfg.save_metadata("calibration")
check("calibration metadata lands in video_dir('calibration')",
      p1 == cfg.video_dir("calibration") / "session_metadata.json" and p1.exists())
m1 = json.loads(p1.read_text(encoding="utf-8"))
check("calibration metadata names its acquisition and rate",
      m1["acq_type"] == "calibration" and m1["acq_fps"] == 30
      and m1["camera_thermals"] == [{"temp_max_c": 61.0}]
      and m1["rig"] == "3dpose" and m1["num_cameras"] == 2)
session_copy = cfg.session_dir / "session_metadata.json"
check("a session-level copy is written when absent", session_copy.exists())
cfg.camera_thermals = [{"temp_max_c": 70.0}]
p2 = cfg.save_metadata("recording")
m2 = json.loads(p2.read_text(encoding="utf-8"))
check("recording metadata lands in video_dir('recording') with its own values",
      p2 == cfg.video_dir("recording") / "session_metadata.json"
      and m2["acq_type"] == "recording" and m2["acq_fps"] == 100
      and m2["camera_thermals"] == [{"temp_max_c": 70.0}])
check("the calibration file is untouched by the recording",
      json.loads(p1.read_text(encoding="utf-8"))["camera_thermals"]
      == [{"temp_max_c": 61.0}])
check("the session-level copy is not overwritten",
      json.loads(session_copy.read_text(encoding="utf-8"))["acq_type"] == "calibration")
cfg2 = SessionConfig.from_profile(pose, date="20260918", mouse_1="c", mouse_2="d",
                                  base_data_dir=TMP / "meta")
p3 = cfg2.save_metadata()
check("save_metadata() without acq_type keeps the session-level layout",
      p3 == cfg2.session_dir / "session_metadata.json" and p3.exists()
      and "acq_type" not in json.loads(p3.read_text(encoding="utf-8")))

# --- rig_setup ---------------------------------------------------------------
class _OldManager:
    """open_all as shipped before the bandwidth-reserve keywords existed."""
    pin_capture_threads = False
    encoder_pcores = False
    pin_encoder_threads = False

    def open_all(self, pfs_path, gige_driver="socket", trigger_rate_limit=165.0,
                 expect_cameras=0, max_num_buffer=1000, only_serials=None,
                 backend=None):
        return True


class _KwargsManager(_OldManager):
    def open_all(self, pfs_path, **kwargs):
        return True


kw = rig_setup.open_kwargs(_OldManager(), pose)
check("open_kwargs passes the profile's open path settings",
      kw["pfs_path"] == pose.pfs_path and kw["gige_driver"] == "socket"
      and kw["trigger_rate_limit"] == 165.0 and kw["expect_cameras"] == 9
      and kw["max_num_buffer"] == 600 and kw["backend"] == "basler", str(kw))
check("open_kwargs drops keywords the manager does not accept",
      "gev_bandwidth_reserve_pct" not in kw and "gev_bandwidth_reserve_accum" not in kw)
check("open_kwargs omits only_serials when camera_serials is None",
      "only_serials" not in kw)
serial_prof = load("camera_serials: ['1', '2']\nn_cameras: 2\n"
                   "gev_bandwidth_reserve_pct: 10\n", "serials")
kw2 = rig_setup.open_kwargs(_KwargsManager(), serial_prof)
check("a **kwargs manager receives every non-None field",
      kw2["only_serials"] == ["1", "2"] and kw2["gev_bandwidth_reserve_pct"] == 10.0
      and "gev_bandwidth_reserve_accum" not in kw2, str(kw2))
check("open_kwargs output is accepted by the stub's open_all",
      _OldManager().open_all(**kw) is True)

mgr = _OldManager()
logged = []
pool = rig_setup.apply_profile_to_manager(mgr, pose, log=logged.append)
check("apply_profile_to_manager copies the three placement flags",
      mgr.pin_capture_threads is True and mgr.encoder_pcores is False
      and mgr.pin_encoder_threads is False)
check("apply_profile_to_manager logs one line through a one-argument callable",
      len(logged) == 1 and isinstance(logged[0], str)
      and (pool is None or isinstance(pool, list)), str(logged))
import logging
_records = []
_logger = logging.getLogger("test_session_config.rig")
_logger.addHandler(type("H", (logging.Handler,), {"emit": lambda self, r: _records.append(r)})())
_logger.setLevel(logging.INFO)
check("a logging.Logger method is accepted as log (no flush= keyword leaks)",
      raises(Exception, rig_setup.apply_profile_to_manager, _OldManager(), pose,
             log=_logger.info) is None and len(_records) == 1)
check("a bare lambda is accepted as log",
      raises(Exception, rig_setup.apply_profile_to_manager, _OldManager(), pose,
             log=lambda m: None) is None)
check("the default log is unbuffered print",
      inspect.signature(rig_setup.apply_profile_to_manager).parameters["log"]
      .default.keywords == {"flush": True})
if pool:
    check("the pool honours capture_core_exclude",
          not set(pool) & set(pose.capture_core_exclude), str(pool))

# The real CameraManager, if importable here (needs PyQt5; no cameras opened).
try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from gui_app.camera_manager import CameraManager
    params = inspect.signature(CameraManager.open_all).parameters
    kw3 = rig_setup.open_kwargs(CameraManager, pose)
    check("open_kwargs against the installed CameraManager.open_all is accepted",
          set(kw3) <= set(params) and {"pfs_path", "expect_cameras",
                                       "max_num_buffer", "backend"} <= set(kw3),
          str(sorted(kw3)))
except ImportError as e:
    print(f"   (CameraManager not importable here: {e}; signature check skipped)")

# ----------------------------------------------------------------------------
_tmp.cleanup()
if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print(f"\nALL {_n} PASS")
