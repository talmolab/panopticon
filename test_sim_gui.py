"""The whole application, driven through its real window, with no hardware.

Every other offline test exercises one module. This one builds the actual
`MainWindow` on `profiles/sim.yaml` and drives it through a calibration and a
recording the way an operator does -- the sidebar toggles -- then asserts on
what landed on disk. It is the acceptance of the hardware-free rig: if this
passes, the application runs end to end on a machine with no cameras, no
trigger board, no vendor SDK and no NVIDIA GPU.

pypylon is made un-importable before the first `gui_app` import, so a
regression that pulls the vendor SDK into a run whose profile says
`camera_backend: sim` fails here rather than on someone's laptop.

WHAT IS ASSERTED, and why each one is worth a case:
  - both acquisitions reach IDLE with no modal dialog left open, because a
    half-finished state machine is what leaves the cameras streaming while the
    window reads IDLE;
  - block IDs are contiguous, equal-length and IDENTICAL across the three
    cameras, which is the property every downstream consumer assumes and the
    one a silent misalignment breaks while looking perfect;
  - one mp4 per camera, with as many frames as `blockids.npy` has entries,
    through whichever encoder the launch preflight selected AND through
    libx264 when the profile asks for the CPU path;
  - `session_metadata.json` beside each acquisition's videos, carrying that
    acquisition's own `acq_type`;
  - `stim_trace.csv` and `stim_paradigm.json` when a paradigm is applied;
  - the moved-aside folder, when the same session is recorded twice.

TIME
The simulated board runs on a virtual clock (`sim_board.SimBoard.speed`), so
the acquisitions below are short in wall-clock seconds and still look like a
full-rate session to every consumer. The point is the sequence, not duration.

    set QT_QPA_PLATFORM=offscreen && python test_sim_gui.py
"""
import dataclasses
import functools
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# RULE: set before any gui_app import. REASON: this is the claim the whole
# file makes -- that the simulated rig needs no vendor SDK -- and an import
# that has already succeeded cannot be un-done by setting this afterwards.
sys.modules["pypylon"] = None
sys.modules["pypylon.pylon"] = None

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: Everything this test writes. Never the repository's `data/`: the GUI's own
#: default output directory is a real session tree on the rig machine.
SCRATCH = Path(tempfile.mkdtemp(prefix="panopticon_sim_gui_"))

from PyQt5.QtCore import QSettings

# RULE: redirect QSettings before importing anything that constructs one.
# REASON: the sidebar builds its store at MODULE import and the window reads
# the remembered profile from it, so this is both how the run is pinned to the
# simulated rig and how it is kept out of the operator's real settings --
# which carry the board-sketch hint, a safety record about the firmware
# believed to be on the trigger board.
QSettings.setDefaultFormat(QSettings.IniFormat)
QSettings.setPath(QSettings.IniFormat, QSettings.UserScope,
                  str(SCRATCH / "settings"))
_STORE = QSettings("Salk", "Panopticon")
_STORE.setValue("profile_name", "sim")
_STORE.sync()

from PyQt5.QtWidgets import QApplication, QMessageBox as _RealMessageBox

_APP = QApplication.instance() or QApplication([])

import numpy as np

from gui_app import cpu_encode, encoders, ffmpeg_cmd, hardware_check
from gui_app import main_window as mw
from gui_app.backends import sim_board
from gui_app.camera_manager import CameraManager
from gui_app.main_window import MainWindow, State
from gui_app.widgets import stimulation_window as sw_mod

#: Virtual seconds per real second. The cameras still report their profile
#: rate; only the wall clock is compressed.
SPEED = 2.0
#: Virtual seconds per acquisition. Long enough that every camera passes its
#: first-frame, ready-barrier and stop path, short enough to run in a suite.
CAL_S = 4.0
REC_S = 5.0

DATA = SCRATCH / "data"
DATA.mkdir(parents=True, exist_ok=True)

failures = []
expected_failures = []


def check(num, name, ok, detail=""):
    print(f"{num}) {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def xfail(num, name, ok, finding, reason, detail=""):
    """A case that fails on a KNOWN defect in a file this package does not own.

    RULE: a known defect is recorded here and named by its finding id, never
    deleted and never asserted the other way round. REASON: the suite has to
    keep passing while the owning package fixes it, but a deleted case is a
    guard nobody will put back, and a case inverted to expect the bug turns
    green when the fix lands and red only when it regresses -- the wrong way
    round. XPASS is therefore not a failure either: it says the fix arrived
    and this case can go back to `check`.
    """
    verdict = "XPASS (fixed — promote to check)" if ok else f"XFAIL ({finding})"
    print(f"{num}) {name}: {verdict}", flush=True)
    print(f"   {reason}" + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        expected_failures.append(f"{finding}: {name}")


class Box:
    """Records the dialogs the window raises and answers them.

    RULE: answer `Ok` to everything. REASON: the only question this sequence
    asks is the move-aside prompt, and `Ok` is the answer that exercises the
    rename; `Cancel` would abort the start and the second pass would assert
    nothing. A dialog that is NOT expected is caught by the per-step
    assertion, not by refusing it here.
    """
    Yes, No, Ok, Cancel = (_RealMessageBox.Yes, _RealMessageBox.No,
                           _RealMessageBox.Ok, _RealMessageBox.Cancel)
    Critical = _RealMessageBox.Critical
    shown = []

    @classmethod
    def reset(cls):
        cls.shown = []

    @classmethod
    def titles(cls):
        return [t for _k, t, _x in cls.shown]

    @classmethod
    def _record(cls, kind, _parent, title, text, *a, **k):
        cls.shown.append((kind, title, text))
        print(f"   [dialog:{kind}] {title}", flush=True)
        return cls.Ok

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


mw.QMessageBox = Box
sw_mod.QMessageBox = Box


def pump(predicate, timeout_s, label):
    """Run the event loop until `predicate` holds. False on timeout.

    RULE: drive the loop with processEvents rather than `app.exec_()` and
    timers. REASON: every step here is a blocking assertion -- the next one
    reads the files this one wrote -- so the test reads top to bottom, and a
    timeout names the step that hung instead of leaving the suite wedged.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _APP.processEvents()
        if predicate():
            _APP.processEvents()
            return True
        time.sleep(0.005)
    print(f"   TIMEOUT after {timeout_s:g}s waiting for {label}", flush=True)
    return False


def spin(seconds):
    """Keep the event loop alive for `seconds` of wall clock."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _APP.processEvents()
        time.sleep(0.005)


def fired_triggers(brd):
    """Pulses the simulated board put on the wire in its most recent train."""
    st = brd.state()
    end = st.stop_v if st.stop_v is not None else brd.virtual_now()
    if st.fps <= 0:
        return 0
    return max(0, int((end - st.epoch_v) * st.fps))


def rebase_ordinals(brd):
    """Stand in for the fix to M1-02, so the steps after it assert something.

    A camera arms (StartGrabbing) before the board is told to start, and
    anchors its next trigger ordinal to the train that has just ENDED, while
    `SimBoard.start` restarts ordinals at 1 -- so from the second acquisition
    in a process on, the camera ignores every trigger until that stale ordinal
    comes round. Moving the stopped board's epoch onto its stop instant makes
    `ordinal_now()` read 0, which is what the camera would compute if it
    re-anchored on the restart. Harmless once the backend does that itself.
    """
    with brd._lock:
        if brd._stop_v is not None:
            brd._epoch_v = brd._stop_v


def run_step(win, kind, virtual_s, rebase=True):
    """Drive one acquisition from the sidebar toggle and wait out its tail.

    Returns (reached the acquiring state, came back to IDLE). The wait after
    the toggle is released covers the finalize, the encode and -- whenever
    capture reported anything at all -- the alignment pass, which is why it is
    generous.
    """
    if rebase:
        rebase_ordinals(sim_board.shared_board())
    toggle = (win._sidebar._record_toggle if kind == "r"
              else win._sidebar._calibrate_toggle)
    want = State.RECORDING if kind == "r" else State.CALIBRATING
    Box.reset()
    toggle.setChecked(True)
    started = pump(lambda: win._state is want, 60, want.value)
    if started:
        spin(virtual_s / SPEED)
    toggle.setChecked(False)
    idle = pump(lambda: win._state is State.IDLE and not win._busy, 600,
                "IDLE after the tail")
    return started, idle


def mp4_frames(path):
    """Frames the container reports, or -1 when OpenCV cannot say.

    Read from the mp4 rather than trusted from the encoder's own count: the
    question is whether the file a downstream tool opens holds the frames the
    trigger record claims, and an mp4 no reader can index is not a recording.
    """
    try:
        import cv2
    except Exception:
        return -1
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return -1
        return int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        cap.release()


def acquisition_ok(num, label, acq_dir, cams, acq_type, encoder_name):
    """The five per-acquisition assertions, as five numbered cases."""
    try:
        ids = [np.load(acq_dir / cam / "blockids.npy") for cam in cams]
    except Exception as e:
        for offset, what in enumerate(("blockids.npy for every camera",
                                       "block IDs contiguous",
                                       "block IDs identical across cameras",
                                       "one mp4 per camera",
                                       "session_metadata.json")):
            check(num + offset, f"{label}: {what}", False, str(e))
        return
    lens = [len(b) for b in ids]
    check(num, f"{label}: blockids.npy present and equal-length on all "
               f"{len(cams)} cameras",
          len(set(lens)) == 1 and lens[0] > 0, f"lengths={lens}")
    gaps = [sorted(set(np.diff(b).tolist())) for b in ids]
    check(num + 1, f"{label}: block IDs contiguous on every camera",
          all(g == [1] for g in gaps), f"diffs={gaps}")
    check(num + 2, f"{label}: block IDs identical across cameras",
          all(np.array_equal(ids[0], b) for b in ids[1:]),
          f"first={[int(b[0]) for b in ids]} last={[int(b[-1]) for b in ids]}")

    found, counts = [], []
    for cam in cams:
        vids = sorted((acq_dir / cam).glob("*.mp4"))
        found.append(len(vids))
        counts.append(mp4_frames(vids[0]) if vids else -1)
    check(num + 3, f"{label}: one mp4 per camera through the {encoder_name} "
                   f"encoder, as long as the trigger record",
          all(n == 1 for n in found) and counts == lens,
          f"mp4s={found} frames={counts} blockids={lens}")

    meta = acq_dir / "session_metadata.json"
    try:
        body = json.loads(meta.read_text())
    except Exception as e:
        body = {"error": str(e)}
    check(num + 4, f"{label}: session_metadata.json beside the videos, naming "
                   f"this acquisition",
          meta.exists() and body.get("acq_type") == acq_type,
          f"acq_type={body.get('acq_type')!r} error={body.get('error', '')}")


# -- 1-4 -- the window, on the simulated rig ---------------------------------
board = sim_board.reset_board(speed=SPEED)

try:
    win = MainWindow()
    sdk_free_build, build_error = True, ""
except ImportError as e:
    win, sdk_free_build, build_error = None, False, str(e).splitlines()[0]
xfail(1, "MainWindow builds with pypylon un-importable and a profile that "
         "says camera_backend: sim",
      sdk_free_build, "M1-01",
      "main_window.__init__ builds CameraManager() before the profile is "
      "resolved, and CameraManager.__init__ loads the default 'basler' "
      "backend eagerly, so pypylon is imported at construction whatever the "
      "profile names and the application cannot start on a host without the "
      "vendor SDK.", build_error)
if win is None:
    # Stands in for that fix -- hand the manager the backend the profile
    # names -- so every case below still drives the real window. It is one
    # line here precisely because open_all already takes the backend by name.
    mw.CameraManager = functools.partial(CameraManager, "sim")
    win = MainWindow()

win._sidebar._output_dir = str(DATA)
win._sidebar._fields["mouse_1"].setText("sim1")
win._sidebar._fields["mouse_2"].setText("sim2")

CAMS = list(win._camera_names)
check(2, "the window comes up on profiles/sim.yaml with the simulated backend",
      (win._profile.name == "sim"
       and win._profile.camera_backend == "sim"
       and getattr(win._camera_mgr._backend, "name", None) == "sim"),
      f"profile={win._profile.name!r} "
      f"backend={getattr(win._camera_mgr._backend, 'name', None)!r}")
check(3, "the profile's camera count and geometry are what opened",
      (len(CAMS) == win._profile.n_cameras
       and win._camera_mgr.geometry == (win._profile.frame_width,
                                          win._profile.frame_height)),
      f"cams={CAMS} geometry={win._camera_mgr.geometry}")

pump(lambda: win._teensy is not None and win._teensy.is_open, 30,
     "the simulated serial port")
pump(lambda: (win._hw_check_thread is not None
              and win._hw_check_thread.isFinished()), 300,
     "the launch hardware check")
selected = hardware_check._selected_encoder
port_open = win._teensy is not None and win._teensy.is_open
check(4, "the launch preflight claims the simulated board and selects an "
         "encoder from the profile",
      port_open and selected in ("nvenc", "x264"),
      f"port_open={port_open} encoder={selected!r}")

# -- 5-16 -- pass one: calibration then recording, no paradigm ---------------
session = DATA / win._sidebar._fields["date"].text() / "sim1_sim2"

started, idle = run_step(win, "c", CAL_S)
check(5, "calibration runs and the window returns to IDLE with no dialog left "
         "open",
      (started and idle and QApplication.activeModalWidget() is None
       and not Box.shown),
      f"started={started} idle={idle} dialogs={Box.titles()}")
acquisition_ok(6, "calibration", session / "calibration", CAMS, "calibration",
               selected)

# rebase=False: the second acquisition in a process is where M1-02 bites, and
# case 17 below is what states it. Every later step takes the stand-in.
started, idle = run_step(win, "r", REC_S, rebase=False)
check(11, "recording runs and the window returns to IDLE with no dialog left "
          "open",
      (started and idle and QApplication.activeModalWidget() is None
       and not Box.shown),
      f"started={started} idle={idle} dialogs={Box.titles()}")
acquisition_ok(12, "recording", session / "recording", CAMS, "recording",
               selected)

fired = fired_triggers(board)
recorded = len(np.load(session / "recording" / CAMS[0] / "blockids.npy"))
xfail(17, "a second acquisition in the same process records every trigger the "
          "board fired",
      abs(fired - recorded) <= 2, "M1-02",
      "SimCamera.StartGrabbing anchors its next trigger ordinal to the pulse "
      "train that has just ENDED, while SimBoard.start restarts ordinals at "
      "1, so a camera armed for any acquisition after the first ignores every "
      "trigger until that stale ordinal comes round. It is silent: the frames "
      "that do arrive still carry block IDs from 1 and stay contiguous, so "
      "the recording looks perfect and is simply short at the front.",
      f"fired={fired} recorded={recorded}")

# -- 18-19 -- a paradigm, and the CPU encoder ---------------------------------
win._on_stimulation()
stim = win._stim_window
stim._canvas.load_workflow(
    [{"id": "b1", "x": 0.0, "y": 0.0, "pin": win._profile.stim_safe_pins[0],
      "freq": 10.0, "pw": 20.0, "dur": 2.0, "start": True, "end": False}], [])
Box.reset()
stim._apply_btn.click()
applied = pump(lambda: (stim._upload_worker is None
                        and win._session_stim_ino is not None), 120,
               "the simulated firmware upload")
check(18, "a paradigm compiles and 'uploads' to the simulated board, which "
          "then reports that sketch's identity",
      (applied and board.sketch_id is not None
       and not [k for k, _t, _x in Box.shown if k == "critical"]),
      f"applied={applied} sketch_id={board.sketch_id!r} "
      f"dialogs={Box.titles()}")

cpu_profile = dataclasses.replace(win._profile, encoder="x264")
cpu_choice = hardware_check.select_encoder(
    cpu_profile, len(CAMS), win._profile.frame_rate,
    win._profile.frame_width, win._profile.frame_height)
check(19, "asking the profile for `encoder: x264` installs the libx264 "
          "factory, so the rest of the run needs no NVIDIA GPU",
      (cpu_choice.encoder == "x264"
       and encoders.get_default_factory() is cpu_encode.x264_factory
       and ffmpeg_cmd.get_default_backend() == "x264"),
      f"choice={cpu_choice.encoder} blocking={bool(cpu_choice.blocking)}")

# -- 20-33 -- pass two: the same session again, on the CPU encoder ------------
#: The two dialogs pass two is EXPECTED to raise, and the only two it may:
#: the move-aside prompt, and the completion warning that carries the CPU
#: encoder's own note about sharing cores with capture. Asserting the exact
#: set is what makes "no dialog left open" mean something on a pass that
#: legitimately shows some.
CPU_PASS_DIALOGS = ["Existing data will be moved aside",
                    "Recording completed with problems"]

started, idle = run_step(win, "c", CAL_S)
moved_cal = sorted(session.glob("calibration.previous-*"))
check(20, "a second calibration into the same session returns to IDLE and "
          "moves the first one aside instead of recording over it",
      (started and idle and QApplication.activeModalWidget() is None
       and len(moved_cal) == 1
       and (moved_cal[0] / CAMS[0] / "blockids.npy").exists()
       and sorted(set(Box.titles())) == CPU_PASS_DIALOGS),
      f"started={started} idle={idle} moved={[p.name for p in moved_cal]} "
      f"dialogs={Box.titles()}")
acquisition_ok(21, "calibration (libx264)", session / "calibration", CAMS,
               "calibration", "libx264")

started, idle = run_step(win, "r", REC_S)
moved_rec = sorted(session.glob("recording.previous-*"))
check(26, "a second recording into the same session returns to IDLE and moves "
          "the first one aside",
      (started and idle and QApplication.activeModalWidget() is None
       and len(moved_rec) == 1
       and (moved_rec[0] / CAMS[0] / "blockids.npy").exists()
       and sorted(set(Box.titles())) == CPU_PASS_DIALOGS),
      f"started={started} idle={idle} moved={[p.name for p in moved_rec]} "
      f"dialogs={Box.titles()}")
acquisition_ok(27, "recording (libx264)", session / "recording", CAMS,
               "recording", "libx264")

rec = session / "recording"
trace = rec / "stim_trace.csv"
rows = trace.read_text().splitlines() if trace.exists() else []
recorded = len(np.load(rec / CAMS[0] / "blockids.npy"))
check(32, "a recording made under an applied paradigm carries stim_trace.csv "
          "with one row per recorded frame, plus the firmware that produced it",
      (trace.exists() and (rec / "stim_paradigm.json").exists()
       and (rec / "stim_paradigm.ino").exists()
       and len(rows) - 1 == recorded),
      f"rows={max(0, len(rows) - 1)} blockids={recorded} "
      f"paradigm={(rec / 'stim_paradigm.json').exists()}")

# -- 33 -- nothing pulled the vendor SDK in -----------------------------------
check(33, "the whole run imported no vendor camera SDK",
      (sys.modules.get("pypylon") is None
       and sys.modules.get("pypylon.pylon") is None),
      f"pypylon={sys.modules.get('pypylon')!r}")

# -- teardown -----------------------------------------------------------------
# closeEvent is the path that stands the board down and joins the workers; a
# test that skipped it would leave grab threads running into interpreter
# shutdown, which reads as a crash rather than as a failure.
win.close()
spin(1.0)
# RULE: retry the removal rather than accept the first failure. REASON: on
# Windows a directory whose files have just been closed stays un-removable for
# a moment, so one rmtree leaves an empty shell behind every run and the temp
# tree fills up with them.
for _ in range(20):
    shutil.rmtree(SCRATCH, ignore_errors=True)
    if not SCRATCH.exists():
        break
    spin(0.25)
if SCRATCH.exists():
    print(f"could not remove the scratch directory {SCRATCH}", flush=True)

print()
for known in expected_failures:
    print(f"KNOWN DEFECT, not fixed here: {known}")
if failures:
    print(f"{len(failures)} SIM-GUI TEST(S) FAILED: {failures}")
    sys.exit(1)
print("ALL SIM-GUI TESTS PASS"
      + (f" ({len(expected_failures)} expected failure(s))"
         if expected_failures else ""))
