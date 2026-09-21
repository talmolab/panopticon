"""The main window's start, refuse, roll back and quit decisions.

Why this is worth a test. The start sequence is where a mistake costs a
session rather than a frame: cameras and the NVENC router used to be started
before the firmware check, so a sketch swap re-entered the sequence, orphaned
a router holding one encoder session and one open stream.h264 per camera,
prompted about files the same run had just created, and on a failed flash left
nine cameras in trigger mode while the window read IDLE. Everything asserted
here is an ORDER or a REFUSAL, not an appearance: what runs before what, what
is never reached, what is moved rather than deleted, and what the quit path is
allowed to remove.

No cameras and no trigger board: the window is built with `__new__` and the
pieces each method touches are stubbed, as test_thermal_watch does, with the
worker made synchronous so a start can be driven to completion. One case does
open the SIMULATED backend through the same profile plumbing the GUI uses, to
prove the profile's camera_backend really reaches open_all.

The last case is the exception: it builds the REAL window, because the launch
itself is what it asserts. `pypylon` is made un-importable for the whole file
so that case can prove a host with no vendor SDK still gets a window and a
dialog rather than an exception out of the constructor.

    set QT_QPA_PLATFORM=offscreen && uv run python test_main_window_start.py
"""
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# RULE: set before any gui_app import. REASON: the launch case below asserts
# that a window still comes up on a host with no vendor SDK, and an import
# that has already succeeded cannot be un-done afterwards.
sys.modules["pypylon"] = None
sys.modules["pypylon.pylon"] = None

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication, QMessageBox as _RealMsgBox

_APP = QApplication.instance() or QApplication([])

from gui_app import main_window as mw
from gui_app import rig_setup, settings
from gui_app.camera_manager import AcquisitionStartRefused, AcquisitionStopIncomplete
from gui_app.grab_thread import ring_slots
from gui_app.main_window import MainWindow, State
from gui_app.session_config import RigProfile, SessionConfig

failures = []


def check(num, name, ok, detail=""):
    print(f"{num}) {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


# ── stubs ────────────────────────────────────────────────────────────────────
class _MsgBox:
    """Records dialogs instead of showing them, and answers as scripted."""
    Yes, No, Ok, Cancel = (_RealMsgBox.Yes, _RealMsgBox.No,
                           _RealMsgBox.Ok, _RealMsgBox.Cancel)
    Critical = _RealMsgBox.Critical
    shown = []
    answer = None

    @classmethod
    def reset(cls, answer=None):
        cls.shown = []
        cls.answer = answer

    @classmethod
    def _record(cls, kind, parent, title, text, *a, **k):
        cls.shown.append((kind, title, text))
        if cls.answer is not None:
            return cls.answer
        return cls.Ok if a else cls.No

    @classmethod
    def question(cls, *a, **k):
        return cls._record("question", *a, **k)

    @classmethod
    def warning(cls, *a, **k):
        return cls._record("warning", *a, **k)

    @classmethod
    def critical(cls, *a, **k):
        return cls._record("critical", *a, **k)

    @classmethod
    def information(cls, *a, **k):
        return cls._record("information", *a, **k)

    @classmethod
    def titles(cls):
        return [t for _k, t, _x in cls.shown]

    @classmethod
    def texts(cls):
        return "\n".join(x for _k, _t, x in cls.shown)


class _Worker:
    """CallableWorker stand-in: runs on start() and delivers on this thread."""
    made = []

    class _Signal:
        def __init__(self, owner):
            self._owner = owner

        def connect(self, slot):
            self._owner.slots.append(slot)

    def __init__(self, fn, parent=None):
        self.fn = fn
        self.slots = []
        self.result = None
        _Worker.made.append(self)

    @property
    def done(self):
        return _Worker._Signal(self)

    def start(self):
        try:
            self.result = self.fn()
        except Exception as e:      # CallableWorker delivers the exception
            self.result = e
        for slot in list(self.slots):
            slot(self.result)

    def isRunning(self):
        return False

    def wait(self, ms=None):
        return True


class _Timer:
    def __init__(self):
        self.started = None
        self.stops = 0

    def start(self, ms=None):
        self.started = ms

    def stop(self):
        self.stops += 1


class _Sidebar:
    def __init__(self, out_dir=""):
        self.output_dir = out_dir
        self.reset_calls = 0
        self.cleared = []
        self.fields_editable = None
        self.toggles_enabled = None
        self.solve_enabled = None
        self.busy = None
        self.status = None
        self.brightness = 0
        self.contrast = 0
        self.profile_warnings = []

    def reset_toggles(self):
        self.reset_calls += 1

    def clear_toggle_silently(self, kind):
        self.cleared.append(kind)

    def set_fields_editable(self, on):
        self.fields_editable = on

    def set_toggles_enabled(self, on):
        self.toggles_enabled = on

    def set_solve_enabled(self, on):
        self.solve_enabled = on

    def set_busy(self, on):
        self.busy = on

    def set_status(self, text, colour):
        self.status = text

    def hide_progress(self):
        pass

    def setup_coverage(self, n):
        pass

    def show_coverage(self):
        pass

    def hide_coverage(self):
        pass


class _Status:
    """Qt status bar stand-in: the window is built with __new__, so the real
    one cannot be reached."""

    def __init__(self):
        self.messages = []

    def showMessage(self, msg):
        self.messages.append(msg)


class _Grid:
    def __init__(self):
        self.cameras = None
        self.aspect = None

    def setup_grid(self, n):
        self.cameras = n

    def set_camera_aspect(self, w, h):
        self.aspect = (w, h)


class _Mgr:
    """Enough CameraManager for the start, stop and rollback paths."""

    def __init__(self, n=2, mismatch=None, start_error=None, stop_error=None):
        self.num_cameras = n
        self._mismatch = mismatch
        self._start_error = start_error
        self._stop_error = stop_error
        self.calls = []
        self.start_kwargs = {}
        self.last_warnings = []
        self.last_stream_stats = []
        self.frontier_lags = []
        self.delivery_lags = []

    def geometry_mismatch(self, w, h):
        return self._mismatch

    def start_acquisition(self, *a, **k):
        self.calls.append("start_acquisition")
        self.start_kwargs = dict(k)
        if self._start_error is not None:
            raise self._start_error

    def wait_until_ready(self, timeout):
        self.calls.append("wait_until_ready")
        return (self.num_cameras, self.num_cameras)

    def pinning_report(self):
        return "[affinity] stub"

    def signal_triggers_started(self):
        self.calls.append("signal_triggers_started")

    def stop_acquisition(self):
        self.calls.append("stop_acquisition")
        if self._stop_error is not None:
            raise self._stop_error
        return []

    def resume_preview(self):
        self.calls.append("resume_preview")

    def abandon(self):
        self.calls.append("abandon")

    def set_keep_full(self, flag):
        pass

    def thermals(self):
        return []


class _Teensy:
    def __init__(self, ack=True, stop_ack=True, board_id=None, speaks=True):
        self.is_open = True
        self.port = "sim"
        self.last_error = None
        self.board_id = board_id
        self._speaks_rdy = speaks
        self._ack = ack
        self._stop_ack = stop_ack
        self.calls = []

    def start_triggers(self, pins, fps):
        self.calls.append("start_triggers")
        return self._ack

    def stop_triggers(self, pins):
        self.calls.append("stop_triggers")
        return self._stop_ack

    def close(self):
        self.calls.append("close")
        self.is_open = False


class _Stim:
    def __init__(self, testing=False, blocker=None, blocks=(), source="",
                 uploading=False):
        self._uploading = uploading
        self._testing = testing
        self._blocker = blocker
        self._blocks = list(blocks)
        self._source = source

    def is_testing(self):
        return self._testing

    def is_uploading(self):
        return self._uploading

    def record_blocker(self):
        return self._blocker

    def get_workflow(self):
        return self._blocks, []

    def firmware_source(self):
        return self._source


SIM_PROFILE = RigProfile.load(Path(__file__).parent / "profiles" / "sim.yaml")


@contextlib.contextmanager
def temp_settings(tmp):
    """Point gui_app.settings at a throwaway INI for the duration.

    The board-sketch key is the operator's real one: it decides whether the
    launch reflashes the trigger board to the recording-only sketch. A test
    that writes it and is then killed leaves the machine claiming the board
    carries a sketch nothing said it carries, and the next launch skips the
    stim-clearing flash. Nothing here may touch the real store.
    """
    path = str(Path(tmp) / "settings.ini")
    real = settings.app_settings
    settings.app_settings = lambda: QSettings(path, QSettings.IniFormat)
    try:
        yield
    finally:
        settings.app_settings = real


class _Running:
    """A worker slot that reports a live thread."""

    def __init__(self):
        self.waits = []

    def isRunning(self):
        return True

    def wait(self, ms=None):
        self.waits.append(ms)
        return True


def make(tmp, *, mgr=None, teensy=None, stim=None, flash_needed=False,
         profile=None):
    """A window with everything the start path touches stubbed out."""
    w = MainWindow.__new__(MainWindow)
    w._state = State.IDLE
    w._busy = False
    w._acq_type = ""
    w._acq_fps = 0
    w._finalized = True
    w._created_dirs = []
    w._overwrite_dir = None
    w._board_id_stale = True
    w._board_identity_reflashed = False
    w._session_stim_ino = None
    w._video_dir = None
    w._config = None
    w._detector = None
    w._coverage_worker = None
    w._retired_workers = []
    w._encode_worker = None
    w._align_worker = None
    w._calib_worker = None
    w._calib_config = None
    w._cam_op = None
    w._cap_op = None
    w._fw_op = None
    w._snap_op = None
    w._hw_check_thread = None
    w._capture_warnings = []
    w._thermal_warnings = []
    w._thermal_reported = set()
    w._thermal_alert = None
    w._thermal_timer = _Timer()
    w._display_timer = _Timer()
    w._display_tick = 0
    w._lut = None
    w._lut_key = None
    w._profile = profile or SIM_PROFILE
    w._camera_mgr = mgr or _Mgr()
    w._teensy = teensy if teensy is not None else _Teensy()
    w._stim_window = stim
    w._sidebar = _Sidebar(str(tmp))
    w._camera_grid = _Grid()
    status = _Status()
    w.statusBar = lambda: status
    w._camera_names = [f"cam{i + 1}" for i in range(w._camera_mgr.num_cameras)]
    w._config = SessionConfig.from_profile(
        SIM_PROFILE, date="20260101", mouse_1="m1", mouse_2="m2",
        base_data_dir=Path(tmp), camera_names=list(w._camera_names))
    cfg = w._config
    w._build_config = lambda: cfg

    def _claim(retries=10):
        w._teensy.calls.append("reclaim")
        w._teensy.is_open = True
        return w._teensy

    w._teensy_connection = _claim
    w._ensure_sketch_for = lambda acq: not flash_needed
    w._start_thermal_watch = lambda: None
    w._start_coverage_hud = lambda: None
    w._save_stim_paradigm = lambda: None
    w._arm_stim_autostop = lambda: None
    return w


def _body(path):
    """The file's bytes, or b"" when a failing case removed it."""
    try:
        return path.read_bytes()
    except OSError:
        return b""


def touch(path: Path, body=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


mw.QMessageBox = _MsgBox
mw.CallableWorker = _Worker
SOURCE = (Path(__file__).parent / "gui_app" / "main_window.py").read_text(
    encoding="utf-8")
SOURCE_TEST = Path(__file__).read_text(encoding="utf-8")
#: What the machine's REAL board-sketch hint says before any case runs. Read
#: once, never written: case 50 proves this suite gave it back untouched.
REAL_HINT_AT_START = settings.board_sketch_hint()


# 1-3 ── the firmware check runs before any camera or file side effect ───────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp, flash_needed=True)
    w._start_acquisition("recording")
    check(1, "a needed flash starts no cameras",
          "start_acquisition" not in w._camera_mgr.calls,
          str(w._camera_mgr.calls))
    check(2, "a needed flash creates no session directory",
          not (tmp / "20260101").exists())
    check(3, "and the window has not left IDLE",
          w._state is State.IDLE)

# 4-6 ── the serial claim runs before the cameras ───────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp)
    w._teensy.last_error = "PermissionError: COM3 is held"
    w._teensy_connection = lambda retries=10: None
    w._start_acquisition("recording")
    check(4, "a port that will not open starts no cameras",
          "start_acquisition" not in w._camera_mgr.calls,
          str(w._camera_mgr.calls))
    check(5, "the dialog carries the port's own error and no laser warning",
          "COM3 is held" in _MsgBox.texts()
          and "key off the laser" not in _MsgBox.texts().lower(),
          _MsgBox.texts()[:120])
    check(6, "the refused start leaves no camera directories behind",
          not (tmp / "20260101" / "m1_m2" / "recording").exists()
          and w._sidebar.reset_calls == 1)

# 7-11 ── existing data is overwritten on consent, deleted whole ────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = tmp / "20260101" / "m1_m2" / "recording"
    old_mp4 = touch(rec / "cam1" / "20260101-m1_m2-cam1-recording.mp4", b"old")
    touch(rec / "cam1" / "blockids.npy", b"ids")
    _MsgBox.reset(answer=_MsgBox.Yes)
    w = make(tmp)
    w._start_acquisition("recording")
    aside = [p for p in (tmp / "20260101" / "m1_m2").iterdir()
             if p.name.startswith("recording.previous-")]
    check(7, "the previous session is deleted, not kept beside the new one",
          not aside and not (rec / "cam1" / old_mp4.name).exists()
          and not (rec / "cam1" / "blockids.npy").exists(),
          str([p.name for p in (tmp / '20260101' / 'm1_m2').iterdir()]))
    check(8, "the new acquisition recreates the canonical folder fresh",
          rec.exists() and (rec / "cam1").is_dir())
    check(9, "the dialog warns the data will be deleted",
          any("DELETE" in t or "Overwrite" in t for _k, _t, t in _MsgBox.shown),
          _MsgBox.texts()[:200])
    check(10, "and the recording started",
          w._state is State.RECORDING
          and "signal_triggers_started" in w._camera_mgr.calls,
          str(w._camera_mgr.calls))

check(11, "the move-aside is gone from the source",
      SOURCE.count("_move_existing_aside") == 0
      and SOURCE.count(".previous-") == 0)

# 12-13 ── cancelling keeps the data and starts nothing ─────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = tmp / "20260101" / "m1_m2" / "recording"
    touch(rec / "cam1" / "stream.h264", b"stream")
    _MsgBox.reset(answer=_MsgBox.Cancel)
    w = make(tmp)
    w._start_acquisition("recording")
    check(12, "Cancel leaves the existing data exactly where it was",
          _body(rec / "cam1" / "stream.h264") == b"stream")
    check(13, "Cancel starts nothing and resets the toggles",
          "start_acquisition" not in w._camera_mgr.calls
          and w._sidebar.reset_calls == 1 and w._state is State.IDLE)

# 14 ── a zero-length stream file is not data ───────────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = tmp / "20260101" / "m1_m2" / "recording"
    touch(rec / "cam1" / "stream.h264", b"")
    _MsgBox.reset(answer=_MsgBox.Cancel)
    w = make(tmp)
    w._start_acquisition("recording")
    check(14, "an aborted start's empty stream file is not treated as data",
          w._state is State.RECORDING and not _MsgBox.shown,
          _MsgBox.texts()[:120])

# 15-18 ── the rollback tells the truth about the board ─────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    w = make(tmp, teensy=_Teensy(stop_ack=False))
    out = w._rollback_acquisition("the start failed", sent_start=False)
    check(15, "no start sent: the board is not asked to stand down",
          "stop_triggers" not in w._teensy.calls)
    check(16, "and no 'did not accept the stop' warning is invented",
          "did not accept the stop" not in out["message"],
          out["message"][:120])

    w = make(tmp, teensy=_Teensy(stop_ack=False))
    out = w._rollback_acquisition("the board did not ack", sent_start=True)
    check(17, "a start that WAS sent stands the board down and warns on refusal",
          "stop_triggers" in w._teensy.calls
          and "did not accept the stop" in out["message"])

    stuck = AcquisitionStopIncomplete("cam2 still running", results=[], stuck=[1])
    w = make(tmp, mgr=_Mgr(stop_error=stuck))
    out = w._rollback_acquisition("the board did not ack", sent_start=True)
    check(18, "a stop that cannot finish abandons and says the cameras closed",
          "abandon" in w._camera_mgr.calls and out.get("cameras_closed") is True
          and "closed" in out["message"])

# 19 ── a refused camera start needs no rollback and records nothing ────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    refused = AcquisitionStartRefused("Could not put every camera into "
                                      "trigger mode")
    w = make(tmp, mgr=_Mgr(start_error=refused))
    w._start_acquisition("recording")
    check(19, "AcquisitionStartRefused is caught, reported and not rolled back",
          "trigger mode" in _MsgBox.texts()
          and "stop_acquisition" not in w._camera_mgr.calls
          and w._state is State.IDLE and w._sidebar.reset_calls == 1,
          _MsgBox.texts()[:120])

# 20 ── the board never acks: the cameras go back to preview ───────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp, teensy=_Teensy(ack=False))
    w._start_acquisition("recording")
    check(20, "a board that never acks rolls the cameras back to preview",
          w._camera_mgr.calls[-2:] == ["stop_acquisition", "resume_preview"]
          and w._state is State.IDLE,
          str(w._camera_mgr.calls))

# 21-24 ── the stimulation editor's refusals ────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    w = make(tmp, stim=_Stim(testing=True))
    check(21, "a running bench Test refuses a recording",
          (w._stim_refusal("recording") or ("", ""))[0]
          == "Stop the stimulation test first")
    check(22, "and refuses a calibration too",
          w._stim_refusal("calibration") is not None)

    w = make(tmp, stim=_Stim(blocker="A firmware upload is in progress"))
    check(23, "record_blocker (upload in flight, failed Apply) refuses",
          w._stim_refusal("recording") is not None)

    w = make(tmp, stim=_Stim(blocks=[{"id": 1}], source="SKETCH B"))
    w._session_stim_ino = "SKETCH A"
    drift = w._stim_refusal("recording")
    check(24, "a canvas edited since the last Apply refuses the recording",
          drift is not None and "Apply" in drift[0], str(drift))
    w._session_stim_ino = "SKETCH B"
    check(25, "and the same canvas Applied does not",
          w._stim_refusal("recording") is None)
    w._session_stim_ino = None
    check(26, "a never-Applied canvas refuses the recording",
          w._stim_refusal("recording") is not None)
    check(27, "but a calibration, which is always stim-free, is allowed",
          w._stim_refusal("calibration") is None)

# 28-29 ── the session fields become paths, so they are validated ───────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp)
    bad = SessionConfig.from_profile(
        SIM_PROFILE, date="20260101", mouse_1="../escape", mouse_2="m2",
        base_data_dir=Path(tmp), camera_names=["cam1"])
    w._build_config = lambda: bad
    w._start_acquisition("recording")
    check(28, "a path separator in a mouse id refuses the start",
          "start_acquisition" not in w._camera_mgr.calls
          and w._sidebar.reset_calls == 1)
    check(29, "and the dialog names the field",
          "mouse_1" in _MsgBox.texts(), _MsgBox.texts()[:120])

# 30-32 ── the capacity preflight ───────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    seen = {}

    def fake_capacity(**kw):
        seen.update(kw)
        return [], []

    real_capacity = mw.check_capacity
    mw.check_capacity = fake_capacity
    try:
        w = make(tmp)
        w._preflight_capacity("calibration")
        check(30, "a calibration is budgeted at the CALIBRATION frame rate",
              seen.get("fps") == SIM_PROFILE.calibration_frame_rate,
              f"fps={seen.get('fps')}")
        check(31, "the ring depth comes from grab_thread, not a second copy",
              seen.get("ring_n") == ring_slots(
                  SIM_PROFILE.kick_max_lag,
                  SIM_PROFILE.realtime_encode and SIM_PROFILE.realtime_kick),
              f"ring_n={seen.get('ring_n')}")
        check(32, "and the profile's encoder choice reaches the preflight",
              seen.get("encoder") == SIM_PROFILE.encoder)
    finally:
        mw.check_capacity = real_capacity

    _MsgBox.reset()
    w = make(tmp, mgr=_Mgr(mismatch="The profile records 640x400 but the "
                                    "cameras are configured for 1920x1200"))
    w._start_acquisition("recording")
    check(33, "a profile/camera geometry mismatch refuses before anything runs",
          "start_acquisition" not in w._camera_mgr.calls
          and "1920x1200" in _MsgBox.texts(), _MsgBox.texts()[:120])

# 34-36 ── what quitting may delete ─────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    w = make(tmp)
    w._state, w._finalized = State.RECORDING, False
    check(34, "a capture still in flight is deleted on quit",
          w._delete_on_quit() is True)
    w._state, w._finalized = State.ENCODING, True
    check(35, "a complete capture waiting to be wrapped is NOT",
          w._delete_on_quit() is False)
    w._state, w._finalized = State.ALIGNING, True
    keep_align = w._delete_on_quit() is False
    w._state, w._finalized = State.RECORDING, True
    check(36, "nor is one whose finalize completed during the quit wait",
          keep_align and w._delete_on_quit() is False)

# 37 ── the launch hardware check is waited for ─────────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    w = make(tmp)
    w._hw_check_thread = _Running()
    check(37, "a running hardware check keeps the window from closing silently",
          w._workers_running() is True)

# 38-39 ── a failed finalize hands the window back ──────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp)
    w._state = State.RECORDING
    w._video_dir = tmp / "rec"
    w._begin_busy("Finishing...")
    w._on_acquisition_finalized(RuntimeError("disk full"))
    check(38, "a failed finalize re-enables the metadata fields",
          w._sidebar.fields_editable is True)
    check(39, "and says the cameras were closed, with an empty grid",
          w._camera_grid.cameras == 0 and "closed" in _MsgBox.texts(),
          _MsgBox.texts()[-120:])

# 40 ── the stuck cameras are named ─────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp)
    w._state = State.RECORDING
    w._video_dir = tmp / "rec"
    w._begin_busy("Finishing...")
    w._on_acquisition_finalized(
        AcquisitionStopIncomplete("stuck", results=[], stuck=[1]))
    check(40, "an incomplete stop names the camera that would not finish",
          "cam2" in _MsgBox.texts(), _MsgBox.texts()[-160:])

# 41-42 ── per-acquisition metadata, with the transport counters ────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    import json
    w = make(tmp)
    w._acq_type = "calibration"
    w._camera_mgr.last_stream_stats = [{"Statistic_Failed_Buffer_Count": 3}]
    w._save_acquisition_metadata()
    path = (tmp / "20260101" / "m1_m2" / "calibration"
            / "session_metadata.json")
    meta = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    check(41, "metadata is written beside the videos it describes",
          meta.get("acq_type") == "calibration"
          and meta.get("acq_fps") == SIM_PROFILE.calibration_frame_rate,
          str(path))
    check(42, "and carries the cameras' transport counters",
          meta.get("camera_stream_stats")
          == [{"Statistic_Failed_Buffer_Count": 3}])

# 43-44 ── alignment: replaced cameras mean the trace is stale ──────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    w = make(tmp)
    w._state = State.ALIGNING
    w._video_dir = tmp / "rec"
    traced = []
    w._write_stim_trace = lambda: traced.append(1)
    w._finish_to_idle = lambda: None
    w._on_align_done({"replaced": False, "replaced_cams": ["cam1"],
                      "failures": ["cam2: too few frames"],
                      "warnings": ["block rate"], "common_frames": 10,
                      "camera_names": ["cam1", "cam2"]})
    check(43, "a partly replaced alignment still regenerates the stim trace",
          traced == [1])
    w2 = make(tmp)
    w2._state = State.ALIGNING
    w2._video_dir = tmp / "rec"
    traced2 = []
    w2._write_stim_trace = lambda: traced2.append(1)
    w2._finish_to_idle = lambda: None
    w2._on_align_done({"replaced": False, "replaced_cams": [], "failures": [],
                       "warnings": [], "camera_names": ["cam1"]})
    check(44, "an alignment that replaced nothing does not rewrite it",
          traced2 == [])

# 45 ── leaving busy re-applies the session lock ────────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    w = make(tmp)
    w._state = State.ENCODING
    w._begin_busy("Working...")
    w._end_busy()
    check(45, "ENCODING keeps the profile and output-dir fields locked",
          w._sidebar.fields_editable is False)
    w._state = State.IDLE
    w._begin_busy("Working...")
    w._end_busy()
    check(46, "and IDLE gets them back",
          w._sidebar.fields_editable is True)

# 47-49 ── the board's own identity decides whether to flash ────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    from gui_app import stim_compiler
    sketch = stim_compiler.recording_only_sketch([53], [2, 4])
    want_id = stim_compiler.sketch_id(sketch)
    # The real store is never touched: see temp_settings.
    with temp_settings(tmp):
        settings.set_board_sketch_hint(stim_compiler.sketch_sha(sketch))
        w = make(tmp, teensy=_Teensy(board_id="deadbeef"))
        w._board_id_stale = False
        check(47, "a board reporting another sketch is flashed despite the hint",
              w._board_needs_flash(sketch) is True)
        w._teensy.board_id = want_id
        check(48, "a board reporting this sketch is not flashed",
              w._board_needs_flash(sketch) is False, f"id={want_id}")
        w._teensy.board_id = "deadbeef"
        w._board_id_stale = True
        check(49, "a board that has not spoken since the flash is not re-flashed",
              w._board_needs_flash(sketch) is False)
check(50, "the machine's real board-sketch hint is left exactly as found",
      settings.app_settings().value(settings.KEY_BOARD_SKETCH, "", type=str)
      == REAL_HINT_AT_START,
      f"{REAL_HINT_AT_START!r} -> {settings.board_sketch_hint()!r}")

# 50-52 ── the profile really selects the backend ───────────────────────────
kwargs = rig_setup.open_kwargs(mw.CameraManager, SIM_PROFILE)
check(51, "open_kwargs carries the profile's camera_backend",
      kwargs.get("backend") == "sim", str(sorted(kwargs)))
check(52, "and the expected geometry and pool depth",
      kwargs.get("expect_geometry") == (SIM_PROFILE.frame_width,
                                        SIM_PROFILE.frame_height)
      and kwargs.get("max_num_buffer") == SIM_PROFILE.max_num_buffer)

_mgr = mw.CameraManager()
rig_setup.apply_profile_to_manager(_mgr, SIM_PROFILE, log=lambda *_a: None)
opened = _mgr.open_all(**kwargs)
try:
    check(53, "the simulated cameras open through that same path",
          opened is True and _mgr.num_cameras == SIM_PROFILE.n_cameras
          and _mgr.geometry == (SIM_PROFILE.frame_width,
                                SIM_PROFILE.frame_height),
          f"{opened} n={_mgr.num_cameras} geom={_mgr.geometry}")
finally:
    _mgr.close_all()

# 53 ── snapshots name the camera that produced nothing ────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    w = make(tmp)
    out = MainWindow._write_snapshots(tmp / "snaps", [None, None],
                                      ["cam1", "cam2"])
    check(54, "a camera with no frame is named, not silently counted out",
          out["missing"] == ["cam1", "cam2"] and out["saved"] == 0)


# 55-59 ── a start refused after the move-aside puts the data back ──────────
# The dialog promises "this acquisition records into the original folder name
# so the solve and the alignment scripts still find it". 1_calibrate,
# the overwrite is consented on the UI thread but DELETED by the worker, after
# the serial claim, so the common refusal (port busy) costs no data while a
# committed start overwrites as agreed.
def _with_previous_data(tmp):
    rec = tmp / "20260101" / "m1_m2" / "recording"
    touch(rec / "cam1" / "20260101-m1_m2-cam1-recording.mp4", b"old")
    return rec


def _moved_dirs(tmp):
    return [p for p in (tmp / "20260101" / "m1_m2").iterdir()
            if p.name.startswith("recording.previous-")]


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = _with_previous_data(tmp)
    _MsgBox.reset(answer=_MsgBox.Yes)
    w = make(tmp)
    w._teensy.last_error = "PermissionError: COM3 is held"
    w._teensy_connection = lambda retries=10: None
    w._start_acquisition("recording")
    check(55, "a port-busy refusal happens before the delete, so the data "
          "the operator agreed to overwrite is still there",
          rec.is_dir()
          and _body(rec / "cam1" / "20260101-m1_m2-cam1-recording.mp4") == b"old"
          and not _moved_dirs(tmp),
          str([p.name for p in (tmp / "20260101" / "m1_m2").iterdir()]))
    check(56, "and the start was refused, not recorded",
          w._state is State.IDLE
          and "start_acquisition" not in w._camera_mgr.calls)

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = _with_previous_data(tmp)
    _MsgBox.reset(answer=_MsgBox.Yes)
    refused = AcquisitionStartRefused("Could not put every camera into "
                                      "trigger mode")
    w = make(tmp, mgr=_Mgr(start_error=refused))
    w._start_acquisition("recording")
    check(57, "a camera refusal comes AFTER the port opened, so the overwrite "
          "the operator agreed to has already happened",
          w._state is State.IDLE
          and not (rec / "cam1" / "20260101-m1_m2-cam1-recording.mp4").exists())

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = _with_previous_data(tmp)
    _MsgBox.reset(answer=_MsgBox.Yes)
    w = make(tmp, teensy=_Teensy(ack=False))
    w._start_acquisition("recording")
    check(58, "a board that never acks also comes after the delete; the start "
          "is refused and the overwrite stands",
          w._state is State.IDLE
          and not (rec / "cam1" / "20260101-m1_m2-cam1-recording.mp4").exists())

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = _with_previous_data(tmp)
    _MsgBox.reset(answer=_MsgBox.Yes)
    w = make(tmp)
    w._start_acquisition("recording")
    check(59, "a start that ran deleted the old data and kept the name",
          w._state is State.RECORDING and not _moved_dirs(tmp)
          and rec.is_dir()
          and not (rec / "cam1"
                   / "20260101-m1_m2-cam1-recording.mp4").exists())

# 60-62 ── the sweep waits for the board, and the port comes first ──────────
# WARNINGS.txt, encode_error.log, codet_frames.json, tail.h264 and
# raw_tail.bin are what the failed-camera dialog sends the operator to read.
# None of them is in DATA_PATTERNS, so no move-aside protects them.
def _with_reports(tmp):
    rec = tmp / "20260101" / "m1_m2" / "recording"
    touch(rec / "WARNINGS.txt", b"session warning")
    touch(rec / "codet_frames.json", b"{}")
    touch(rec / "cam1" / "WARNINGS.txt", b"cam warning")
    touch(rec / "cam1" / "encode_error.log", b"why cam1 failed")
    touch(rec / "cam1" / "raw_tail.bin", b"tail")
    return rec


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = _with_reports(tmp)
    _MsgBox.reset()
    w = make(tmp)
    w._teensy.last_error = "PermissionError: COM3 is held"
    w._teensy_connection = lambda retries=10: None
    w._start_acquisition("recording")
    check(60, "a start the port refuses destroys no previous run's reports",
          _body(rec / "WARNINGS.txt") == b"session warning"
          and (rec / "codet_frames.json").exists()
          and _body(rec / "cam1" / "WARNINGS.txt") == b"cam warning"
          and (rec / "cam1" / "encode_error.log").exists()
          and (rec / "cam1" / "raw_tail.bin").exists())

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = _with_reports(tmp)
    _MsgBox.reset()
    w = make(tmp, mgr=_Mgr(start_error=AcquisitionStartRefused("no cameras")))
    w._start_acquisition("recording")
    check(61, "nor does a camera start that refuses",
          (rec / "WARNINGS.txt").exists()
          and (rec / "cam1" / "encode_error.log").exists())

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    rec = _with_reports(tmp)
    _MsgBox.reset()
    w = make(tmp)
    w._start_acquisition("recording")
    check(62, "a start that the board acked does sweep them",
          w._state is State.RECORDING
          and not (rec / "WARNINGS.txt").exists()
          and not (rec / "codet_frames.json").exists()
          and not (rec / "cam1" / "WARNINGS.txt").exists()
          and not (rec / "cam1" / "encode_error.log").exists()
          and not (rec / "cam1" / "raw_tail.bin").exists())

# 63 ── the NVENC session probe never runs on the UI thread ────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    seen = {}

    def fake_capacity(**kw):
        seen["busy"] = w._busy
        seen["workers"] = len(_Worker.made)
        return [], []

    real_capacity = mw.check_capacity
    mw.check_capacity = fake_capacity
    try:
        _MsgBox.reset()
        _Worker.made = []
        w = make(tmp)
        w._start_acquisition("recording")
        check(63, "the capacity preflight runs in a worker, under the busy "
                  "overlay",
              seen.get("busy") is True and seen.get("workers") == 1
              and w._state is State.RECORDING,
              f"{seen} state={w._state}")
    finally:
        mw.check_capacity = real_capacity

# 64-66 ── a solve owns the toggles, the fields and the start path ─────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp)
    w._calib_worker = _Running()
    w._start_acquisition("recording")
    check(64, "a Record during a solve is refused, not started on top of it",
          "start_acquisition" not in w._camera_mgr.calls
          and w._state is State.IDLE and w._sidebar.reset_calls == 1
          and "solve" in _MsgBox.texts().lower(), _MsgBox.texts()[:120])

    w._sidebar.toggles_enabled = False
    w._stim_window = _Stim(uploading=False)
    w._on_stim_upload_state(False)
    check(65, "an editor upload finishing does not reopen the toggles a solve "
              "closed",
          w._sidebar.toggles_enabled is False)

    w._state = State.IDLE
    w._begin_busy("Updating the stimulus trace...")
    w._end_busy()
    check(66, "nor does a busy cycle hand back the fields the solve locked",
          w._sidebar.fields_editable is False)

    w._calib_worker = None
    w._on_stim_upload_state(False)
    check(67, "and with the solve gone the toggles come back",
          w._sidebar.toggles_enabled is True)

# 68 ── a canvas edited during the ~30 s flash is caught at the re-entry ────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp, stim=_Stim(blocks=[{"id": 1}], source="EDITED"))
    w._session_stim_ino = "FLASHED"
    w._arm_acquisition("recording")
    check(68, "a paradigm edited during the flash refuses at the re-entry",
          "start_acquisition" not in w._camera_mgr.calls
          and not (tmp / "20260101").exists()
          and "Apply" in _MsgBox.texts(), _MsgBox.texts()[:120])

# 69 ── a failed per-acquisition flash hands the port back ──────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    from gui_app import stim_compiler as _sc
    real_upload = _sc.upload
    _sc.upload = lambda ino, port: (False, "avrdude: verification error")
    try:
        with temp_settings(tmp):
            w = make(tmp)
            del w._ensure_sketch_for       # exercise the real one
            ok = MainWindow._ensure_sketch_for(w, "recording")
    finally:
        _sc.upload = real_upload
    check(69, "a flash that fails reclaims the serial port before it reports",
          ok is False and w._teensy.calls[:2] == ["close", "reclaim"]
          and w._teensy.is_open is True
          and "could not be flashed" in _MsgBox.texts(),
          f"{w._teensy.calls} {_MsgBox.texts()[:80]}")

# 70-72 ── the launch identity probe, against the simulated board ──────────
# stop_triggers skips the ack while the controller has never heard an RDY
# line, and at launch it never has: the identity has to be read anyway, or
# every launch decides from the per-machine hint alone.
from gui_app.backends import sim_board
from gui_app.serial_controller import TeensyController
from gui_app import stim_compiler as _sc

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    blank = _sc.recording_only_sketch(SIM_PROFILE.stim_safe_pins,
                                      SIM_PROFILE.trigger_pins)
    sim_board.reset_board()
    sim_board.accept_upload(blank)
    board = TeensyController(port=SIM_PROFILE.serial_port)
    opened = board.open(retries=1)
    _MsgBox.reset()
    w = make(tmp, teensy=board)
    flashed = []
    w._ensure_clean_firmware = lambda: flashed.append(w._teensy.is_open)
    with temp_settings(tmp):
        w._confirm_board_identity()
        hint_after = settings.board_sketch_hint()
    check(70, "the board's own identity is read although it has never acked",
          opened and board.board_id == _sc.sketch_id(blank)
          and board._speaks_rdy is True,
          f"id={board.board_id} speaks={board._speaks_rdy}")
    check(71, "a board carrying the recording-only sketch is not reflashed",
          flashed == [] and hint_after == _sc.sketch_sha(blank))
    board.close()

with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    sim_board.reset_board()
    sim_board.shared_board().sketch_id = "deadbeef"
    board = TeensyController(port=SIM_PROFILE.serial_port)
    board.open(retries=1)
    w = make(tmp, teensy=board)
    flashed = []
    w._ensure_clean_firmware = lambda: flashed.append(w._teensy.is_open)
    with temp_settings(tmp):
        settings.set_board_sketch_hint("whatever this machine last flashed")
        w._confirm_board_identity()
        hint_after = settings.board_sketch_hint()
    check(72, "a board carrying a foreign sketch is reflashed with the port "
              "RELEASED, and the hint forgotten",
          board.board_id == "deadbeef" and flashed == [False]
          and hint_after == "" and w._board_identity_reflashed is True,
          f"id={board.board_id} flashed={flashed} hint={hint_after!r}")
    board.close()

# 73 ── no profile, no flash ────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    _Worker.made = []
    w = make(tmp, profile=RigProfile())
    MainWindow._ensure_clean_firmware(w)
    check(73, "with no profile the launch flash does not run against an empty "
              "port",
          _Worker.made == [] and w._fw_op is None and not _MsgBox.shown,
          _MsgBox.texts()[:120])

# 74 ── the controller is built with the profile's port, not a placeholder ──
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)

    class _FakeTeensy:
        def __init__(self, port="COM3", baudrate=115200):
            self.port = port
            self.is_open = False
            self.board_id = None

        def open(self, retries=10):
            self.is_open = True
            return True

        def close(self):
            self.is_open = False

    real_ctor = mw.TeensyController
    mw.TeensyController = _FakeTeensy
    try:
        with temp_settings(tmp):
            settings.set_board_sketch_hint("a hint worth keeping")
            w = make(tmp)
            w._teensy = None
            del w._teensy_connection
            got = MainWindow._teensy_connection(w, retries=1)
            kept = settings.board_sketch_hint()
    finally:
        mw.TeensyController = real_ctor
    check(74, "the first connection builds the controller on the profile's "
              "port and keeps the hint",
          got is not None and got.port == SIM_PROFILE.serial_port
          and kept == "a hint worth keeping"
          and "self._teensy = TeensyController()" not in SOURCE,
          f"port={getattr(got, 'port', None)} hint={kept!r}")

# 75 ── a worker parked by the coverage HUD is still joined at quit ─────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    w = make(tmp)
    parked = _Running()
    w._retired_workers = [parked]
    check(75, "a parked worker keeps the window from closing silently",
          w._workers_running() is True)
    w._join_retired_workers()
    check(76, "and the quit path joins it rather than destroying it running",
          parked.waits == [5000])

# 77 ── rig facts come from the profile, not SessionConfig's mirror ─────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp)
    # A config whose mirrored scalars disagree with the rig it was built from:
    # the mirror is a copy, and the cameras were configured from the profile.
    w._config.frame_width = 1
    w._config.frame_height = 2
    w._config.quality = 99
    w._config.kick_max_lag = 7
    w._start_acquisition("recording")
    k = w._camera_mgr.start_kwargs
    check(77, "the start is configured from the profile, not the mirror",
          k.get("width") == SIM_PROFILE.frame_width
          and k.get("height") == SIM_PROFILE.frame_height
          and k.get("quality") == SIM_PROFILE.quality
          and k.get("kick_max_lag") == SIM_PROFILE.kick_max_lag, str(k))

# 78 ── a refused profile switch never leaves the window busy ───────────────
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    w = make(tmp)
    other = RigProfile(name="other-rig")
    w._cam_op = _Running()
    w._on_profile_changed(other)
    check(78, "a switch refused by a running camera op leaves no busy state "
              "and no half-adopted profile",
          w._busy is False and w._sidebar.busy is not True
          and w._profile is SIM_PROFILE, f"busy={w._busy}")

    w._cam_op = None
    w._calib_worker = _Running()
    w._on_profile_changed(other)
    check(79, "and a switch during a solve is refused the same way",
          w._busy is False and w._profile is SIM_PROFILE
          and "solve" in _MsgBox.texts().lower(), _MsgBox.texts()[:120])


# 80 ── the whole launch serial sequence, on the simulated rig ─────────────
# No controller exists before the profile is resolved, so this is also what
# proves the launch can still reach the board at all.
with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    sim_board.reset_board()
    sim_board.shared_board().sketch_id = "deadbeef"
    w = make(tmp)
    w._teensy = None
    del w._teensy_connection            # exercise the real one
    flashed = []
    w._ensure_clean_firmware = lambda: flashed.append(
        w._teensy is not None and w._teensy.is_open)
    with temp_settings(tmp):
        settings.set_board_sketch_hint("what this machine last flashed")
        MainWindow._warm_serial(w)
        hint_after = settings.board_sketch_hint()
    check(80, "a launch with no controller yet opens the profile's port, "
              "reads the board and releases it for the flash",
          w._teensy is not None and w._teensy.port == SIM_PROFILE.serial_port
          and w._teensy.board_id == "deadbeef" and flashed == [False]
          and hint_after == "",
          f"id={getattr(w._teensy, 'board_id', None)} flashed={flashed} "
          f"hint={hint_after!r}")
    if w._teensy is not None:
        w._teensy.close()


# 81 ── the launch survives a host with no vendor SDK ──────────────────────
# The only case that builds the REAL window instead of stubbing one, because
# the defect lives in __init__ itself: the startup camera open is synchronous
# and it is where the profile's backend is imported for the first time. On a
# host with no pypylon and a remembered profile that names basler, an
# unguarded open takes the whole window down -- and with no window there is no
# profile dropdown, so the operator cannot select the backend that needs no
# SDK. pypylon is un-importable for this whole file (see the top).
class _NoHwCheck:
    """HardwareCheckThread stand-in: the host survey is not what this tests."""

    class _Signal:
        def connect(self, slot):
            pass

    def __init__(self, *a, **k):
        self.report_ready = _NoHwCheck._Signal()

    def start(self):
        pass

    def isRunning(self):
        return False

    def wait(self, ms=None):
        return True


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    _MsgBox.reset()
    _real_hw_check = mw.HardwareCheckThread
    mw.HardwareCheckThread = _NoHwCheck
    win = None
    raised = None
    try:
        with temp_settings(tmp):
            store = settings.app_settings()
            store.setValue(settings.KEY_PROFILE, "3dpose")
            store.sync()
            try:
                win = MainWindow()
            except Exception as exc:                      # the defect
                raised = exc
            # The dialog is posted through a 100 ms singleShot, so it needs
            # the event loop. Bounded well under the 1.5 s the launch waits
            # before it claims the serial port: no real port may be opened.
            end = time.monotonic() + 1.0
            while (win is not None and "Camera Error" not in _MsgBox.titles()
                   and time.monotonic() < end):
                _APP.processEvents()
                time.sleep(0.01)
    finally:
        mw.HardwareCheckThread = _real_hw_check
        if win is not None:
            win._display_timer.stop()
            win._thermal_timer.stop()
            win.close()
            win.deleteLater()
        _APP.processEvents()
    check(81, "a launch with no vendor SDK and a basler profile remembered "
              "still builds the window, with no cameras and one dialog that "
              "names the profile field to change",
          raised is None and win is not None
          and "Camera Error" in _MsgBox.titles()
          and "camera_backend" in _MsgBox.texts()
          and win._camera_names == [],
          f"raised={raised!r} titles={_MsgBox.titles()}")


print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL MAIN WINDOW START TESTS PASS")
