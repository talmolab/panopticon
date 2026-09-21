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

BOTH ENCODER HOSTS
RULE: every expectation about which dialogs a pass raises is DERIVED from the
encoder the launch preflight actually installed, never written down. REASON:
the profile says `encoder: auto`, so a host without PyNvVideoCodec falls to
libx264, and libx264 then adds a capacity warning before every acquisition and
its own note to every recording's problems. A list of dialog titles written on
an NVENC-equipped machine therefore fails on exactly the hardware-free host
this file exists to prove, and the failure looks like a broken window rather
than like a hard-coded expectation. The acceptance run is the one with
PyNvVideoCodec un-importable; the NVENC run only shows the other branch.

WHAT IS ASSERTED, and why each one is worth a case:
  - both acquisitions reach IDLE with exactly the dialogs the selected encoder
    path implies, because a half-finished state machine is what leaves the
    cameras streaming while the window reads IDLE;
  - block IDs are contiguous, equal-length and IDENTICAL across the three
    cameras, which is the property every downstream consumer assumes and the
    one a silent misalignment breaks while looking perfect;
  - `frametimes.npy` beside them, one column per block ID, because the
    per-frame stimulus trace and the rate check are both built from it;
  - one mp4 per camera, with as many frames as `blockids.npy` has entries;
  - the encoder that produced them is the one the pass demands, witnessed by
    the installed factory AND by the libx264 note in each camera's
    WARNINGS.txt -- present on the CPU path, absent on the GPU one;
  - the alignment pass reads the acquisition and finds nothing to trim,
    because every camera holds the same triggers;
  - `session_metadata.json` beside each acquisition's videos, carrying that
    acquisition's own `acq_type`;
  - `stim_trace.csv` and `stim_paradigm.json` when a paradigm is applied;
  - the moved-aside folder, when the same session is recorded twice.

TIME
The simulated board runs on a virtual clock (`sim_board.SimBoard.speed`), so
the durations below are the ones the acceptance names -- 20 virtual seconds of
calibration and 40 of recording -- while the wall clock stays inside a suite's
budget. RULE: state the acquisitions in virtual seconds and buy the wall clock
back with `SPEED`, never by shortening them. REASON: the sequence is what is
under test, but a four-second recording gives the kick-out router almost
nothing to route and leaves a camera that kept the previous acquisition's
trigger ordinal too little room to show the gap at the front.

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
import traceback
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

from gui_app import settings as settings_mod

# RULE: redirect the settings store through the environment variable BEFORE
# importing anything that reads it, and never with setDefaultFormat/setPath.
# REASON: this run pins the remembered profile to the simulated rig, and the
# operator's real store decides which rig the next launch comes up on and
# carries the board-sketch hint, a safety record about the firmware believed
# to be on the trigger board. setDefaultFormat plus setPath looks like it
# redirects and does not: QSettings(ORG, APP) keeps the native format and the
# registry path, so every write still escaped while the isolation read as
# working. That is how a rig ended up remembering the simulated profile.
os.environ[settings_mod.ENV_SETTINGS_FILE] = str(SCRATCH / "settings.ini")
_STORE = settings_mod.app_settings()
_STORE.setValue(settings_mod.KEY_PROFILE, "sim")
_STORE.sync()
assert "HKEY" not in _STORE.fileName(), (
    f"settings isolation failed: writes are going to {_STORE.fileName()}")

from PyQt5.QtWidgets import QApplication, QMessageBox as _RealMessageBox

_APP = QApplication.instance() or QApplication([])

import numpy as np

from gui_app import alignment, cpu_encode, encoders, ffmpeg_cmd, hardware_check
from gui_app import main_window as mw
from gui_app.backends import sim_board
from gui_app.camera_manager import CameraManager
from gui_app.main_window import MainWindow, State
from gui_app.widgets import stimulation_window as sw_mod

#: Virtual seconds per real second. The cameras still report their profile
#: rate; only the wall clock is compressed.
SPEED = 4.0
#: Virtual seconds per acquisition, as the acceptance states them.
CAL_S = 20.0
REC_S = 40.0

DATA = SCRATCH / "data"
DATA.mkdir(parents=True, exist_ok=True)

failures = []


def check(num, name, ok, detail=""):
    print(f"{num}) {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


class Box:
    """Records the dialogs the window raises and answers them.

    RULE: answer with the affirmative the dialog actually OFFERS -- `Yes` when
    it is among the buttons, else `Ok`. REASON: the code under test compares
    the reply against the button it put on the dialog, so a fixed `Ok` is read
    as "No" by every Yes/No question (the capacity preflight's "Proceed?", and
    four guards in the stimulation window) and the step then aborts quietly
    instead of failing loudly. A dialog that is NOT expected is caught by the
    per-step assertion, not by refusing it here.
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
    def seen(cls):
        """The distinct dialog titles of the step just driven, sorted."""
        return sorted(set(cls.titles()))

    @classmethod
    def _answer(cls, buttons):
        """`Yes` when the dialog offers it, else `Ok`."""
        try:
            if buttons is not None and int(buttons) & int(cls.Yes):
                return cls.Yes
        except TypeError:
            pass
        return cls.Ok

    @classmethod
    def _record(cls, kind, _parent, title, text, buttons=None, *a, **k):
        cls.shown.append((kind, title, text))
        print(f"   [dialog:{kind}] {title}", flush=True)
        return cls._answer(buttons)

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


def run_step(win, kind, virtual_s):
    """Drive one acquisition from the sidebar toggle and wait out its tail.

    Returns (reached the acquiring state, came back to IDLE, the encoder
    factory that was installed while it ran). The wait after the toggle is
    released covers the finalize, the encode and -- whenever capture reported
    anything at all -- the alignment pass, which is why it is generous.

    RULE: read the encoder factory while the acquisition is RUNNING, not
    afterwards. REASON: it is the seam the grab threads and the router resolve
    when they build their encoders, so reading it later would report whatever
    the next step installs rather than what wrote this acquisition's mp4s.
    """
    toggle = (win._sidebar._record_toggle if kind == "r"
              else win._sidebar._calibrate_toggle)
    want = State.RECORDING if kind == "r" else State.CALIBRATING
    Box.reset()
    toggle.setChecked(True)
    started = pump(lambda: win._state is want, 60, want.value)
    factory = encoders.get_default_factory()
    if started:
        spin(virtual_s / SPEED)
    toggle.setChecked(False)
    idle = pump(lambda: win._state is State.IDLE and not win._busy, 600,
                "IDLE after the tail")
    return started, idle, factory


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


def blockid_count(acq_dir, cam):
    """Entries in one camera's `blockids.npy`, or -1 when it cannot be read.

    RULE: never let a missing recording raise out of an assertion's inputs.
    REASON: a start that timed out is already reported as a failed case, and a
    bare `np.load` a line later would turn that reported failure into a crash
    that skips every remaining case.
    """
    try:
        return len(np.load(acq_dir / cam / "blockids.npy"))
    except Exception:
        return -1


def encoder_label(factory):
    """The name of an installed encoder factory, for a case's detail line."""
    if factory is cpu_encode.x264_factory:
        return "libx264"
    if factory is encoders.nvenc_factory:
        return "nvenc"
    return getattr(factory, "__name__", repr(factory))


def cams_noting_libx264(acq_dir, cams):
    """Cameras whose WARNINGS.txt records that this video was encoded on the CPU.

    The libx264 factory appends that note for every encoder it creates, and
    the kick-out router writes it into that camera's own WARNINGS.txt at stop.
    It is the recording's own witness of which encoder produced it, which the
    frame count and the file name are not.
    """
    out = []
    for cam in cams:
        try:
            body = (acq_dir / cam / "WARNINGS.txt").read_text(
                encoding="utf-8", errors="replace")
        except Exception:
            continue
        if "libx264" in body:
            out.append(cam)
    return out


def acquisition_ok(num, label, acq_dir, cams, acq_type, factory, expect):
    """The eight per-acquisition assertions, as eight numbered cases."""
    wanted = encoder_label(expect)
    try:
        ids = [np.load(acq_dir / cam / "blockids.npy") for cam in cams]
    except Exception as e:
        for offset, what in enumerate(("blockids.npy for every camera",
                                       "block IDs contiguous",
                                       "block IDs identical across cameras",
                                       "frametimes.npy",
                                       "one mp4 per camera",
                                       "the expected encoder",
                                       "session_metadata.json",
                                       "the alignment pass")):
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

    # frametimes.npy holds the device timestamp and the host arrival of every
    # frame the recording kept. The per-frame stimulus trace and the block-ID
    # rate check are both built from it, so a recording without it is
    # incomplete even though its videos play.
    shapes = []
    for cam in cams:
        try:
            shapes.append(tuple(np.load(acq_dir / cam / "frametimes.npy").shape))
        except Exception as e:
            shapes.append(str(e))
    check(num + 3, f"{label}: frametimes.npy beside the videos, one entry per "
                   f"block ID on every camera",
          all(isinstance(s, tuple) and s and s[-1] == n
              for s, n in zip(shapes, lens)),
          f"shapes={shapes} blockids={lens}")

    found, counts = [], []
    for cam in cams:
        vids = sorted((acq_dir / cam).glob("*.mp4"))
        found.append(len(vids))
        counts.append(mp4_frames(vids[0]) if vids else -1)
    check(num + 4, f"{label}: one mp4 per camera, as long as the trigger record",
          all(n == 1 for n in found) and counts == lens,
          f"mp4s={found} frames={counts} blockids={lens}")

    # The encoder is asserted, not labelled: the factory that was installed
    # while the acquisition ran, and the recording's own note about it. A
    # frame count reads identically whichever encoder produced it.
    noted = cams_noting_libx264(acq_dir, cams)
    want_note = list(cams) if expect is cpu_encode.x264_factory else []
    check(num + 5, f"{label}: encoded by the {wanted} factory, and every "
                   f"camera's WARNINGS.txt agrees",
          factory is expect and noted == want_note,
          f"factory={encoder_label(factory)} expected={wanted} "
          f"libx264_note={noted} expected_note={want_note}")

    meta = acq_dir / "session_metadata.json"
    try:
        body = json.loads(meta.read_text())
    except Exception as e:
        body = {"error": str(e)}
    check(num + 6, f"{label}: session_metadata.json beside the videos, naming "
                   f"this acquisition",
          meta.exists() and body.get("acq_type") == acq_type,
          f"acq_type={body.get('acq_type')!r} error={body.get('error', '')}")

    # The alignment pass runs over the real output rather than being assumed:
    # `load_blockids` has to find every camera and its video by name, and the
    # intersection has to come out total. A camera that quietly kept frames
    # the others missed reads as "alignment needed" and fails this case.
    try:
        names, blocks, videos = alignment.load_blockids(acq_dir)
        needed = alignment.needs_alignment(blocks)
        detail = f"cameras={names} videos={len(videos)} needs_alignment={needed}"
        ok = (sorted(names) == sorted(cams) and len(videos) == len(cams)
              and not needed)
    except Exception as e:
        ok, detail = False, str(e)
    check(num + 7, f"{label}: the alignment pass reads it and finds nothing to "
                   f"trim, because every camera holds the same triggers",
          ok, detail)


# -- 1-4 -- the window, on the simulated rig ---------------------------------
board = sim_board.reset_board(speed=SPEED)
win = None

# RULE: everything that drives the window lives inside this try, and the
# teardown lives in its finally. REASON: an unhandled exception anywhere in
# the driving below would otherwise skip `win.close()` -- the path that stands
# the board down and joins the grab threads -- and skip the scratch removal,
# so one bad assertion reads as an interpreter-shutdown crash and leaves a
# temp tree behind on the very run where cleanup matters most.
try:
    # This is mandate M1 itself: the window is built with the vendor SDK
    # un-importable, on a profile that says `camera_backend: sim`. It works
    # only because CameraManager records the backend NAME at construction and
    # loads the backend on its first vendor call, by which time
    # open_all(backend=...) has been given the profile's choice -- so a
    # regression that loads one eagerly, or that reads the manager's backend
    # object before the profile is resolved, fails here and not on the
    # acceptance host.
    try:
        win = MainWindow()
        sdk_free_build, build_error = True, ""
    except ImportError as e:
        win, sdk_free_build, build_error = None, False, str(e).splitlines()[0]
    check(1, "MainWindow builds with pypylon un-importable and a profile that "
             "says camera_backend: sim",
          sdk_free_build, build_error)
    if win is None:
        # The run cannot continue: every case below drives this window. Build
        # one with the backend named up front so the failure above is the only
        # one reported, rather than 44 consequences of it.
        mw.CameraManager = functools.partial(CameraManager, "sim")
        win = MainWindow()

    win._sidebar._output_dir = str(DATA)
    win._sidebar._fields["mouse_1"].setText("sim1")
    win._sidebar._fields["mouse_2"].setText("sim2")

    CAMS = list(win._camera_names)
    check(2, "the window comes up on profiles/sim.yaml with the simulated "
             "backend",
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

    #: True on the host the acceptance names: no PyNvVideoCodec, so the
    #: profile's `encoder: auto` falls through to libx264.
    CPU_PATH = selected == "x264"
    #: The CPU path shows two extra dialogs around EVERY acquisition: the
    #: capacity preflight warns that the recording is moving to libx264
    #: ("Proceed?"), and the libx264 factory's own note makes the completion
    #: report non-empty ("Recording completed with problems"). Pass two forces
    #: libx264 whatever the host, so its completion warning is unconditional
    #: while its "Proceed?" still depends on what NVENC could grant.
    CPU_DIALOGS = ["Proceed?", "Recording completed with problems"]
    PASS_ONE_DIALOGS = sorted(set(CPU_DIALOGS)) if CPU_PATH else []
    PASS_TWO_DIALOGS = sorted(set(
        (CPU_DIALOGS if CPU_PATH else ["Recording completed with problems"])
        + ["Overwrite the existing data?"]))
    PASS_ONE_FACTORY = (cpu_encode.x264_factory if CPU_PATH
                        else encoders.nvenc_factory)
    print(f"   encoder path {selected!r}: pass one expects "
          f"{PASS_ONE_DIALOGS}, pass two {PASS_TWO_DIALOGS}", flush=True)

    # -- 5-22 -- pass one: calibration then recording, no paradigm -----------
    session = DATA / win._sidebar._fields["date"].text() / "sim1_sim2"

    # `activeModalWidget()` covers only the modals this file does NOT stand in
    # for -- QFileDialog, reachable from a button no step here clicks. Every
    # QMessageBox is answered by `Box`, which builds no widget, so what catches
    # an unexpected or left-open message box is the title set beside it.
    started, idle, factory = run_step(win, "c", CAL_S)
    check(5, "calibration runs and the window returns to IDLE having shown "
             "exactly the dialogs the selected encoder implies",
          (started and idle and QApplication.activeModalWidget() is None
           and Box.seen() == PASS_ONE_DIALOGS),
          f"started={started} idle={idle} dialogs={Box.seen()} "
          f"expected={PASS_ONE_DIALOGS}")
    acquisition_ok(6, "calibration", session / "calibration", CAMS,
                   "calibration", factory, PASS_ONE_FACTORY)

    # The second acquisition in one process, which is where a camera that kept
    # the previous train's trigger ordinal records short at the front; case 23
    # below is what measures that.
    started, idle, factory = run_step(win, "r", REC_S)
    check(14, "recording runs and the window returns to IDLE having shown "
              "exactly the dialogs the selected encoder implies",
          (started and idle and QApplication.activeModalWidget() is None
           and Box.seen() == PASS_ONE_DIALOGS),
          f"started={started} idle={idle} dialogs={Box.seen()} "
          f"expected={PASS_ONE_DIALOGS}")
    acquisition_ok(15, "recording", session / "recording", CAMS, "recording",
                   factory, PASS_ONE_FACTORY)

    fired = fired_triggers(board)
    recorded = blockid_count(session / "recording", CAMS[0])
    # The one loss mode a recording cannot show you: the frames that do arrive
    # carry block IDs from 1 and stay contiguous, so a run short at the front
    # looks perfect. Counted against the pulses the board actually fired,
    # because that is the only number the camera cannot influence.
    check(23, "a second acquisition in the same process records every trigger "
              "the board fired",
          recorded >= 0 and abs(fired - recorded) <= 2,
          f"fired={fired} recorded={recorded}")

    # -- 24-25 -- a paradigm, and the CPU encoder ----------------------------
    win._on_stimulation()
    stim = win._stim_window
    stim._canvas.load_workflow(
        [{"id": "b1", "x": 0.0, "y": 0.0, "pin": win._profile.stim_safe_pins[0],
          "freq": 10.0, "pw": 20.0, "dur": 2.0, "start": True, "end": False}],
        [])
    Box.reset()
    stim._apply_btn.click()
    applied = pump(lambda: (stim._upload_worker is None
                            and win._session_stim_ino is not None), 120,
                   "the simulated firmware upload")
    check(24, "a paradigm compiles and 'uploads' to the simulated board, which "
              "then reports that sketch's identity",
          (applied and board.sketch_id is not None
           and not [k for k, _t, _x in Box.shown if k == "critical"]),
          f"applied={applied} sketch_id={board.sketch_id!r} "
          f"dialogs={Box.titles()}")

    cpu_profile = dataclasses.replace(win._profile, encoder="x264")
    cpu_choice = hardware_check.select_encoder(
        cpu_profile, len(CAMS), win._profile.frame_rate,
        win._profile.frame_width, win._profile.frame_height)
    check(25, "asking the profile for `encoder: x264` installs the libx264 "
              "factory, so the rest of the run needs no NVIDIA GPU",
          (cpu_choice.encoder == "x264"
           and encoders.get_default_factory() is cpu_encode.x264_factory
           and ffmpeg_cmd.get_default_backend() == "x264"),
          f"choice={cpu_choice.encoder} blocking={bool(cpu_choice.blocking)}")

    # -- 26-44 -- pass two: the same session again, on the CPU encoder -------
    started, idle, factory = run_step(win, "c", CAL_S)
    aside_cal = sorted(session.glob("calibration.previous-*"))
    check(26, "a second calibration into the same session returns to IDLE and "
              "overwrites the first, keeping the canonical folder name",
          (started and idle and QApplication.activeModalWidget() is None
           and not aside_cal
           and (session / "calibration" / CAMS[0] / "blockids.npy").exists()
           and Box.seen() == PASS_TWO_DIALOGS),
          f"started={started} idle={idle} aside={[p.name for p in aside_cal]} "
          f"dialogs={Box.seen()} expected={PASS_TWO_DIALOGS}")
    acquisition_ok(27, "calibration (libx264)", session / "calibration", CAMS,
                   "calibration", factory, cpu_encode.x264_factory)

    started, idle, factory = run_step(win, "r", REC_S)
    aside_rec = sorted(session.glob("recording.previous-*"))
    check(35, "a second recording into the same session returns to IDLE and "
              "overwrites the first",
          (started and idle and QApplication.activeModalWidget() is None
           and not aside_rec
           and (session / "recording" / CAMS[0] / "blockids.npy").exists()
           and Box.seen() == PASS_TWO_DIALOGS),
          f"started={started} idle={idle} aside={[p.name for p in aside_rec]} "
          f"dialogs={Box.seen()} expected={PASS_TWO_DIALOGS}")
    acquisition_ok(36, "recording (libx264)", session / "recording", CAMS,
                   "recording", factory, cpu_encode.x264_factory)

    rec = session / "recording"
    trace = rec / "stim_trace.csv"
    rows = trace.read_text().splitlines() if trace.exists() else []
    recorded = blockid_count(rec, CAMS[0])
    check(44, "a recording made under an applied paradigm carries "
              "stim_trace.csv with one row per recorded frame, plus the "
              "firmware that produced it",
          (trace.exists() and (rec / "stim_paradigm.json").exists()
           and (rec / "stim_paradigm.ino").exists()
           and recorded > 0 and len(rows) - 1 == recorded),
          f"rows={max(0, len(rows) - 1)} blockids={recorded} "
          f"paradigm={(rec / 'stim_paradigm.json').exists()}")

    # -- 45 -- nothing pulled the vendor SDK in ------------------------------
    # RULE: assert on something that CAN fail. REASON: the two sentinels at the
    # top make `import pypylon` raise, so they can never turn into modules on
    # their own and reading them back proves nothing; what a regression leaves
    # behind is a THIRD pypylon key, a sentinel popped and replaced by the real
    # package, or the Basler backend module imported on a run where the window
    # built without it.
    pypylon_keys = sorted(k for k in sys.modules
                          if k == "pypylon" or k.startswith("pypylon."))
    loaded = [k for k in pypylon_keys if sys.modules[k] is not None]
    basler = "gui_app.backends.basler" in sys.modules
    check(45, "the whole run imported no vendor camera SDK and, where the "
              "window builds without it, never reached the Basler backend",
          (pypylon_keys == ["pypylon", "pypylon.pylon"] and not loaded
           and not (sdk_free_build and basler)),
          f"pypylon_keys={pypylon_keys} loaded={loaded} "
          f"basler_imported={basler} window_built_sdk_free={sdk_free_build}")

except Exception:
    traceback.print_exc()
    failures.append("unhandled exception while driving the window")
finally:
    # closeEvent is the path that stands the board down and joins the workers;
    # a test that skipped it would leave grab threads running into interpreter
    # shutdown, which reads as a crash rather than as a failure.
    if win is not None:
        try:
            win.close()
        except Exception:
            traceback.print_exc()
    spin(1.0)
    # RULE: retry the removal rather than accept the first failure. REASON: on
    # Windows a directory whose files have just been closed stays un-removable
    # for a moment, so one rmtree leaves an empty shell behind every run and
    # the temp tree fills up with them.
    for _ in range(20):
        shutil.rmtree(SCRATCH, ignore_errors=True)
        if not SCRATCH.exists():
            break
        spin(0.25)
    if SCRATCH.exists():
        print(f"could not remove the scratch directory {SCRATCH}", flush=True)

print()
if failures:
    print(f"{len(failures)} SIM-GUI TEST(S) FAILED: {failures}")
    sys.exit(1)
print("ALL SIM-GUI TESTS PASS")
