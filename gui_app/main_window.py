"""Main application window — wires cameras, sidebar, state machine, and encoding."""
import json
import os
import shutil
import tempfile
import time
import traceback
from datetime import datetime
import numpy as np
from enum import Enum
from pathlib import Path

from PyQt5.QtWidgets import QMainWindow, QWidget, QHBoxLayout, QApplication, QMessageBox
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QPalette, QColor, QIcon, QCursor

from gui_app.camera_manager import (AcquisitionStartRefused,
                                    AcquisitionStopIncomplete, CameraManager,
                                    CameraOpenError)
from gui_app.grab_thread import ring_slots
from gui_app.serial_controller import TeensyController
from gui_app.encode_worker import EncodeWorker
from gui_app.align_worker import AlignWorker
from gui_app.ui_workers import CallableWorker
from gui_app import alignment
from gui_app import stim_trace
from gui_app.calibration_worker import CalibrationWorker
from gui_app.hardware_check import (HardwareCheckThread, format_report,
                                    check_capacity)
from gui_app.coverage_worker import CoverageWorker
from gui_app import rig_setup
from gui_app import settings
from gui_app import stim_compiler
from gui_app.session_config import SessionConfig, RigProfile
from gui_app.widgets.camera_grid import CameraGridWidget
from gui_app.widgets.sidebar import SidebarWidget
from gui_app.widgets.stimulation_window import StimulationWindow

from gui_app import board_detector

try:
    from gui_app.board_detector import BoardDetector
except Exception:  # OpenCV missing → coverage HUD disabled, rest of GUI still runs
    BoardDetector = None

CALIBRATION_SCRIPT = Path(__file__).parent.parent / "1_calibrate.py"

#: What makes a directory "already holds an acquisition". blockids, frametimes,
#: the alignment archive and the stimulation record count as data too: a
#: directory whose mp4s were moved away for labelling still holds what makes
#: them interpretable, and without these patterns it reads as empty. Without
#: stim_paradigm.json here, a new take into such a directory would get no
#: overwrite prompt and would inherit the old take's paradigm.
DATA_PATTERNS = ("*.mp4", "raw.bin", "stream.h264", "blockids.npy",
                 "frametimes.npy", "alignment.npz", "stim_paradigm.json")

#: Files an agreed overwrite keeps. calibration.toml is the calibration Solve
#: copied beside the recording; it belongs to the session, not to the take
#: being replaced, and the overwrite prompt names it as kept.
KEPT_ON_OVERWRITE = ("calibration.toml",)

#: The previous run's session-level files a committed start removes. The stim
#: files are among them because stim_trace.write_trace builds the trace from
#: whatever stim_paradigm.json it finds: one left from a stimulated take would
#: label every frame of a new, unstimulated take as stimulated.
STALE_SESSION_FILES = ("WARNINGS.txt", "codet_frames.json",
                       "stim_paradigm.json", "stim_paradigm.ino",
                       "stim_trace.csv")


def _has_capture_data(video_dir: Path) -> bool:
    """True when this directory holds an acquisition worth keeping.

    Zero-length files do not count: a start refused after the router opened
    its streams leaves an empty stream.h264 per camera, and treating those as
    data would have the next attempt move an empty directory aside and report
    data that does not exist.
    """
    if not video_dir.exists():
        return False
    for pat in DATA_PATTERNS:
        for path in video_dir.rglob(pat):
            try:
                if path.stat().st_size > 0:
                    return True
            except OSError:
                continue
    return False


class State(Enum):
    IDLE = "IDLE"
    CALIBRATING = "CALIBRATING"
    RECORDING = "RECORDING"
    ENCODING = "ENCODING"
    ALIGNING = "ALIGNING"


class MainWindow(QMainWindow):
    #: Serial open attempts on the interactive start path. The port is
    #: normally already held from launch, so this only runs when the board is
    #: unplugged or held by another program; each failed attempt sleeps a
    #: second, and the operator is waiting on a dialog either way.
    START_SERIAL_RETRIES = 2
    #: Serial open attempts on the UI thread: at launch, after a flash, after
    #: an editor Apply and at a Test. RULE: one. REASON: each failed attempt
    #: sleeps a second on the UI thread, and ten of them read as a hung
    #: window, which invites a Task Manager kill in the middle of the
    #: firmware path. A failure is reported, and the next start retries on
    #: its worker with START_SERIAL_RETRIES.
    UI_SERIAL_RETRIES = 1
    #: Set by closeEvent once the operator has agreed to quit, and never
    #: cleared. The start path reads it so that nothing reaches the trigger
    #: board after the quit has stood it down.
    _quitting = False
    #: What this recording's stimulation files describe, taken at arm time
    #: (_snapshot_stim); None when the recording carries no paradigm.
    _stim_snapshot: dict | None = None

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Panopticon")
        self.setMinimumSize(1000, 400)
        self._apply_theme()
        icon_path = Path(__file__).parent.parent / "panopticon.ico"
        if icon_path.exists():
            icon = QIcon(str(icon_path))
            self.setWindowIcon(icon)
            QApplication.instance().setWindowIcon(icon)

        self._state = State.IDLE
        self._quitting = False
        self._acq_type = ""
        self._acq_fps = 0
        self._camera_names: list[str] = []
        self._capture_warnings: list[str] = []
        #: Preview tick counter: the fps readout and the health line are
        #: refreshed every tenth repaint, not every one.
        self._display_tick = 0
        self._lut = None
        self._lut_key = None
        #: SHA of the sketch the launch-time flash is putting on the board.
        self._pending_sha = ""
        self._detector = None
        self._coverage_worker: CoverageWorker | None = None
        self._encode_worker: EncodeWorker | None = None
        self._align_worker: AlignWorker | None = None
        self._calib_worker: CalibrationWorker | None = None
        #: The config the running solve was started with, so its completion
        #: minutes later does not re-read fields the operator has edited since.
        self._calib_config: SessionConfig | None = None
        #: Workers that outlived their join. Kept referenced until Qt reports
        #: them finished: dropping the last reference to a running QThread is
        #: a qFatal, which no excepthook can intercept.
        self._retired_workers: list = []
        self._config: SessionConfig | None = None
        self._video_dir: Path | None = None
        self._busy = False                 # a blocking camera op is running
        # True once this acquisition's data is fully written. Quitting reads
        # it, because the state still says RECORDING while the finalize runs.
        self._finalized = True
        self._created_dirs: list = []
        #: The video_dir the operator has agreed to overwrite, so the worker
        #: deletes it once the serial claim has succeeded. Reset at each
        #: user-initiated start; survives the firmware-flash re-entry so the
        #: operator is asked once, not again after the flash.
        self._overwrite_dir: Path | None = None
        self._cam_op: CallableWorker | None = None
        #: The capacity preflight, which runs off the UI thread because the
        #: NVENC session probe it may trigger spawns an isolated child.
        self._cap_op: CallableWorker | None = None
        self._fw_op: CallableWorker | None = None
        self._snap_op: CallableWorker | None = None
        # The paradigm applied during THIS session, if any. Never persisted:
        # a launch always starts stimulation-free. Within a session it lets
        # calibration and recording swap firmware automatically.
        self._session_stim_ino: str | None = None
        #: True while the board has not spoken since the last flash, so its
        #: reported identity is the PREVIOUS sketch's and must not be believed.
        self._board_id_stale = True
        #: One reflash per launch from the identity check, so a board whose
        #: firmware can never identify itself is not flashed in a loop.
        self._board_identity_reflashed = False

        # Constructed before the profile is resolved, which is safe only
        # because the manager LOADS no backend until it opens cameras: the
        # profile's camera_backend reaches it through rig_setup.open_kwargs
        # into open_all(backend=...), and that is the call that decides which
        # vendor SDK is imported. RULE: nothing here may ask the manager for
        # its backend object. REASON: the ask alone would load the class
        # default and import pypylon, so the window would refuse to build on
        # a host with no vendor SDK whatever the profile names.
        self._camera_mgr = CameraManager()
        #: Built by _teensy_connection, not here. RULE: the controller is
        #: never constructed before the profile is resolved. REASON: the
        #: controller carries the port, and _teensy_connection treats a
        #: controller whose port differs from the profile's as a DIFFERENT
        #: board and forgets the board-sketch hint — so a placeholder built on
        #: the class default wipes that hint at every launch on any rig whose
        #: profile names another port, costing a 30 s reflash each time.
        self._teensy: TeensyController | None = None
        self._hw_check_thread: HardwareCheckThread | None = None

        self._camera_grid = CameraGridWidget()
        self._sidebar = SidebarWidget()

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._camera_grid, stretch=3)

        sidebar_container = QWidget()
        sidebar_container.setStyleSheet("background-color: #141428; border-left: 1px solid #333;")
        sidebar_layout = QHBoxLayout(sidebar_container)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.addWidget(self._sidebar)
        layout.addWidget(sidebar_container, stretch=0)

        self.setCentralWidget(central)

        self._sidebar.calibrate_toggled.connect(self._on_calibrate_toggle)
        self._sidebar.record_toggled.connect(self._on_record_toggle)
        self._sidebar.run_calibration_clicked.connect(self._on_run_calibration)
        self._sidebar.snapshot_clicked.connect(self._on_snapshot)
        self._sidebar.stimulation_clicked.connect(self._on_stimulation)
        self._sidebar.profile_changed.connect(self._on_profile_changed)

        self._stim_window: StimulationWindow | None = None
        # One timer for the life of the window: a QTimer per recording is a
        # child QObject per recording, never deleted.
        self._stim_end_timer = QTimer(self)
        self._stim_end_timer.setSingleShot(True)
        self._stim_end_timer.timeout.connect(self._on_stim_end)

        self._display_timer = QTimer()
        self._display_timer.timeout.connect(self._refresh_displays)
        self._display_timer.start(self._display_interval_ms())

        # Temperature gets its own slow timer and is deliberately kept off the
        # preview timer: thermals() is a GVCP register read per camera, while
        # the preview repaints up to ten times a second. The interval is read
        # from the profile when acquisition starts, because _profile is not
        # assigned yet at this point in __init__.
        self._thermal_alert: str | None = None
        self._thermal_warnings: list = []
        self._thermal_reported: set = set()
        self._thermal_timer = QTimer()
        self._thermal_timer.timeout.connect(self._poll_thermals)

        # Prefer whatever profile this machine used last — the profile list is
        # shared with the 3dface rig, so alphabetical order picks the wrong one
        # here. Fall back to the first profile whose .pfs actually exists.
        self._profile = self._sidebar.current_profile
        if self._sidebar.select_profile(self._sidebar.remembered_profile()):
            self._profile = self._sidebar.current_profile
        else:
            for prof in self._sidebar.profiles:
                if prof.pfs_path and Path(prof.pfs_path).exists():
                    self._sidebar.select_profile(prof.name)
                    self._profile = prof
                    break
        print(f"[acq] profile: {self._profile.name}", flush=True)

        self._open_cameras()
        self._size_to_screen()
        self._sidebar.set_status("IDLE", "#888")
        self._run_hardware_check()
        # Two things, in this order, deferred so the window paints and
        # Windows finishes registering the taskbar entry first. The serial
        # open in _warm_serial blocks the main thread for ~1 s (Arduino
        # reset settle); if that lands before the taskbar setup completes,
        # Windows falls back to the python.exe default icon. 1.5 s, not
        # singleShot(0): one event-loop turn is not enough for the shell to
        # take the icon, which is the bug this delay exists to fix.
        # (1) Put the board back to the recording-only sketch, so a
        # paradigm can never survive from a previous session -- stim is opt-in
        # per launch. (2) Then claim the serial port: opening it resets the
        # Arduino, and during the reset + bootloader every pin floats, which
        # fires a connected laser. Doing it at launch keeps that flash out of
        # the experiment. arduino-cli needs the port to itself, hence the order.
        QTimer.singleShot(1500, self._ensure_clean_firmware)
        # After the window is up, so the warning is a dialog over a live
        # window rather than a message behind the splash screen.
        QTimer.singleShot(0, self._show_profile_warnings)

    def _open_cameras(self):
        """Open cameras for the current profile (synchronous — startup only).

        RULE: a raised exception is turned into the same value
        `_apply_camera_open_result` already accepts, never allowed out of
        here. REASON: this runs inside `__init__`, so anything that escapes
        destroys the window before it exists — and the vendor SDK is loaded
        for the first time under this call, on the profile the machine
        happens to remember. A host with no `pypylon` would then have no
        window at all, hence no profile dropdown, hence no way to select a
        backend that needs no SDK. The live profile-switch path gets this for
        free: CallableWorker delivers the exception as its result.
        """
        try:
            ok = self._open_cameras_bg()
        except Exception as exc:
            traceback.print_exc()
            ok = exc
        self._apply_camera_open_result(ok)

    def _open_cameras_bg(self):
        """Blocking open (run on a worker thread for live profile switches).

        RULE: the thread-placement flags and the capture core pool are applied
        BEFORE open_all, through rig_setup, which is also what the probes
        call. REASON: open_all ends by starting the PREVIEW grab threads, and
        each pins itself against the core pool as it starts, so a flag set
        afterwards reaches only the recording threads a later start rebuilds —
        and the preview threads are what a profile switch leaves running.
        Applying the profile anywhere but rig_setup is how the GUI came to run
        unpinned while every probe number looked right.
        """
        pfs = self._profile.pfs_path
        if not pfs or not Path(pfs).exists():
            return CameraOpenError(
                f"The profile's camera settings file is missing:\n"
                f"{pfs or '(not set)'}\n\nSet pfs_path in the profile YAML to "
                f"a file in configs/.")
        rig_setup.apply_profile_to_manager(self._camera_mgr, self._profile)
        return self._camera_mgr.open_all(
            **rig_setup.open_kwargs(self._camera_mgr, self._profile))

    def _apply_camera_open_result(self, ok):
        if ok is True:
            n = self._camera_mgr.num_cameras
            # The panes letterbox to the cameras' real frame shape; without it
            # a 1920x1200 rig is drawn at the widget's default aspect.
            self._camera_grid.set_camera_aspect(self._profile.frame_width,
                                                self._profile.frame_height)
            # The layout follows the camera count, so a four-camera rig gets
            # a 2x2 grid rather than three columns with one pane on a second
            # row.
            self._camera_grid.set_columns(CameraGridWidget.columns_for(n))
            self._camera_grid.setup_grid(n)
            self._camera_names = [f"cam{i+1}" for i in range(n)]
            # The camera count is only known now, and the repaint period scales
            # with it — the timer built in __init__ used the fallback.
            if not self._busy:
                ms = self._display_interval_ms()
                self._display_timer.start(ms)
                print(f"[ui] preview repaint every {ms} ms for {n} cameras",
                      flush=True)
            return
        self._camera_grid.setup_grid(0)
        self._camera_names = []
        # ONE dialog, carrying the reason the manager gave. open_all returns
        # its refusal as a value for exactly this: the error signal used to
        # deliver a specific message that a generic "No cameras found or .pfs
        # missing" then contradicted.
        reason = (str(ok) if isinstance(ok, Exception)
                  else (self._camera_mgr.last_open_error
                        or "No cameras found. Check connections and profile."))
        QTimer.singleShot(100, lambda: QMessageBox.warning(
            self, "Camera Error", reason))

    @staticmethod
    def _worker_busy(worker) -> bool:
        """True when a worker slot still holds a running thread.

        RULE: no worker attribute is assigned over one. REASON: the
        assignment drops the last Python reference to a live QThread, and sip
        deleting the C++ object under it is a qFatal, which sys.excepthook
        cannot intercept — instant process death, mid-operation.
        """
        return worker is not None and worker.isRunning()

    def _session_rig(self, config=None):
        """The rig this session runs on: the profile its config was built from.

        RULE: rig facts - geometry, quality, encoder counts, the kick cap, the
        calibration exposure - are read from the PROFILE, never from the
        scalars SessionConfig mirrors from it. REASON: two sources for one
        fact is how a session comes to be encoded at a geometry the cameras
        were never configured for; the mirror exists only until its last
        reader is gone, and this window was its last reader.

        Falls back to the live profile for a window that has not built a
        config yet, and for a config assembled by hand in a test.
        """
        if config is None:
            config = self._config
        rig = getattr(config, "profile", None)
        return rig if rig is not None else self._profile

    def _solve_running(self) -> bool:
        """True while a calibration solve is running.

        RULE: a solve counts as an owner of the sidebar's gates, beside the
        state machine, the busy cycle and the stimulation editor's flash.
        REASON: a solve runs for minutes and never leaves State.IDLE, so a
        gate recomputed from the state alone reopens under it - which hands
        back the fields the solve locked and lets a Record start on top of the
        running solve child.
        """
        return self._worker_busy(self._calib_worker)

    def _toggles_permitted(self) -> bool:
        """Whether the acquisition toggles may be live right now.

        RULE: every owner of the sidebar's single toggles gate is consulted
        here, and an owner that wants the toggles shut asks through this.
        REASON: set_toggles_enabled IS one gate, shared by the
        ENCODING/ALIGNING phases, the solve, the no-profile lockout and the
        editor's firmware upload, so an owner that writes it from its own
        state alone reopens a gate another owner closed - and a Record that
        reaches the start path during a solve runs an acquisition on top of
        it, because the state machine reads IDLE throughout.
        """
        return (self._state is State.IDLE
                and not self._solve_running()
                and bool(self._profile.name)
                and not (self._stim_window is not None
                         and self._stim_window.is_uploading()))

    def _reset_toggles(self):
        """Both toggles off, with the shared gate set from every owner.

        RULE: the one way this window resets the toggles. REASON: a reset
        that forces the gate open reopens Record and Calibrate in the middle
        of an editor flash whenever an encode, an alignment or a refused
        start finishes during it; the start path still refuses, but only
        through a dialog on a control that should not be live.
        """
        self._sidebar.reset_toggles(self._toggles_permitted())

    def _begin_busy(self, text: str):
        self._busy = True
        self._display_timer.stop()
        self._sidebar.set_busy(True)
        self._sidebar.set_status(text, "#ffaa00")
        QApplication.setOverrideCursor(QCursor(Qt.WaitCursor))

    def _end_busy(self):
        """Undo _begin_busy. NOTE: does NOT restore the status text — the caller
        owns that, because most callers move to a new state (ENCODING, etc.).
        A caller with no new state to show must set it back to IDLE itself."""
        QApplication.restoreOverrideCursor()
        self._sidebar.set_busy(False)
        # set_busy(False) re-enables the profile combo and the output-dir
        # button, which the acquisition locked. Re-apply the session lock:
        # ENCODING and ALIGNING still belong to this session, and a profile
        # changed there leaves the dropdown showing one rig while the window
        # holds another and persists the wrong one for the next launch.
        # A running solve holds the same lock and never leaves IDLE, so the
        # state alone does not say whether the fields may come back: a busy
        # cycle that overlaps one - a deferred firmware check, a trace rebuild
        # - would otherwise hand back the mouse ids the solve is filing under.
        self._sidebar.set_fields_editable(self._state == State.IDLE
                                          and not self._solve_running())
        self._busy = False
        self._display_timer.start(self._display_interval_ms())

    def _display_interval_ms(self) -> int:
        """Preview repaint period, widened as the camera count grows.

        Repainting is per-pane work on the Qt MAIN thread — QImage conversion
        plus a widget repaint each — so its cost is linear in camera count while
        the grab threads' deadline stays fixed at one trigger period. At nine
        cameras this timer costs ~1.5-2 ms of grab-loop slack (5.07-5.57 ms
        headless against 2.87-4.34 ms with the GUI up).

        Six cameras keep a 33 ms period. Beyond that the period grows with the
        count so total repaint work per second stays roughly flat, capped at
        100 ms (10 Hz) because the preview's job is aiming and focus, not
        motion: capture never has priority taken from it for a picture nobody
        is scoring.
        """
        n = max(1, getattr(self._camera_mgr, "num_cameras", 0) or 6)
        return int(min(100, max(33, round(33 * n / 6))))

    def _size_to_screen(self):
        screen = QApplication.primaryScreen().availableGeometry()
        sidebar_w = 260
        grid_aspect = self._camera_grid.grid_aspect()
        target_h = int(screen.height() * 0.8)
        target_w = int(target_h * grid_aspect) + sidebar_w
        if target_w > screen.width() * 0.9:
            target_w = int(screen.width() * 0.9)
            target_h = int((target_w - sidebar_w) / grid_aspect)
        self.resize(target_w, target_h)
        self.move(
            (screen.width() - target_w) // 2 + screen.x(),
            (screen.height() - target_h) // 2 + screen.y(),
        )

    def _run_hardware_check(self):
        """Survey the host once, at launch, off the UI thread.

        The profile and the camera count are what make the libx264 bench, the
        NVENC session probe and the encoder selection run HERE. Without them
        the profile's `encoder` field selects nothing at all and the session
        probe lands on the UI thread at the first Record, freezing the window
        with no busy indicator.
        """
        output_dir = self._profile.output_dir if self._profile else ""
        n_cams = self._camera_mgr.num_cameras or (
            self._profile.n_cameras if self._profile else 0)
        self._hw_check_thread = HardwareCheckThread(
            output_dir, profile=self._profile, n_cams=n_cams)
        self._hw_check_thread.report_ready.connect(self._on_hardware_check_done)
        self._hw_check_thread.start()

    def _show_profile_warnings(self):
        """Report the profiles that would not load, once the window is up.

        A skipped profile is otherwise silent: the dropdown simply does not
        offer it, so a rig whose own profile failed to parse comes up running
        another rig's settings, or none at all.
        """
        warnings = self._sidebar.profile_warnings
        if not self._profile.name:
            self._sidebar.set_toggles_enabled(False)
            self._sidebar.set_solve_enabled(False)
            QMessageBox.critical(
                self, "No rig profile",
                ("\n".join(warnings) or "No rig profile could be loaded.")
                + "\n\nAcquisition is disabled until one loads: a profile is "
                  "what says how many cameras there are, what rate they run "
                  "at and which pins the trigger board drives.\n\nThe trigger "
                  "board is left alone for the same reason: with no profile "
                  "there is no serial port to reach it on and no pin list to "
                  "drive low, so the launch-time stim-clearing flash does not "
                  "run and the board may still carry a paradigm from a "
                  "previous session. Key off the laser, load a profile, then "
                  "start Panopticon again.")
            return
        if warnings:
            QMessageBox.warning(self, "Rig profiles", "\n".join(warnings))

    def _on_hardware_check_done(self, report):
        if report.warnings:
            msg = format_report(report)
            print(msg, flush=True)
            QMessageBox.warning(self, "Hardware Check", msg)

    def _refuse_profile_switch(self):
        """Point the sidebar's dropdown back at the rig this window runs.

        RULE: every refused switch ends here. REASON: the sidebar applies and
        remembers a profile only once the window accepts it, so a refusal that
        left the dropdown alone would show the refused rig while this window
        records with the old one.
        """
        self._sidebar.restore_profile_choice(self._profile.name)

    def _on_profile_changed(self, profile: RigProfile):
        if self._state != State.IDLE or self._busy:
            self._refuse_profile_switch()
            return
        if self._solve_running():
            # A solve never leaves IDLE, so the state guard above misses it.
            self._refuse_profile_switch()
            QMessageBox.information(
                self, "A solve is running",
                "A calibration solve is running. Switch profiles once it has "
                "finished: a profile carries the rig's cameras, and closing "
                "and reopening them under the solve leaves the window "
                "describing one rig while another is streaming.")
            return
        if self._stim_window is not None and self._stim_window.is_uploading():
            # A profile carries the serial port, and arduino-cli is holding it.
            self._refuse_profile_switch()
            QMessageBox.information(
                self, "Firmware upload in progress",
                "The trigger board is being flashed (~30 s). Switch profiles "
                "once it has finished: the profile names the serial port, and "
                "changing it under a running upload leaves the board in an "
                "unknown state.")
            return
        if self._stim_window is not None and self._stim_window.is_testing():
            # The test drives the board through this window's link, and only
            # the editor's Stop Test ends a looping chain.
            self._refuse_profile_switch()
            QMessageBox.information(
                self, "Stop the stimulation test first",
                "A stimulation test is driving the trigger board. Stop it in "
                "the Stimulation editor, then switch profiles: a profile "
                "names the serial port, and switching can close the link the "
                "test's stop has to go out on.")
            return
        # RULE: every refusal is answered BEFORE _begin_busy and before the new
        # profile is adopted. REASON: a return placed between them leaves the
        # wait cursor pushed, the sidebar disabled, the preview timer stopped
        # and _busy True for the rest of the session - every later start,
        # profile switch and firmware check then refuses on _busy - while
        # self._profile already describes a rig whose cameras were never
        # opened, so the geometry, the trigger pins and the serial port name a
        # rig that is not the one streaming.
        if self._worker_busy(self._cam_op):
            print("[acq] a camera operation is still running; not switching "
                  "profile", flush=True)
            self._refuse_profile_switch()
            return
        # close_all + open 6 cameras (+ .pfs load) is ~1-2 s of GigE round-trips;
        # run it off the UI thread so the window doesn't go "not responding".
        self._begin_busy("Switching cameras…")
        self._switch_from_port = self._profile.serial_port
        self._profile = profile

        def _switch():
            self._camera_mgr.close_all()
            return self._open_cameras_bg()

        self._cam_op = CallableWorker(_switch)
        self._cam_op.done.connect(self._on_profile_switch_done)
        self._cam_op.start()

    def _on_profile_switch_done(self, ok):
        # The window runs this profile from here on, whether or not its
        # cameras opened, so the sidebar takes its fields and the next launch
        # comes up on it.
        self._sidebar.accept_profile(self._profile)
        self._apply_camera_open_result(ok)
        self._size_to_screen()
        self._end_busy()
        self._sidebar.set_status("IDLE", "#888")
        self._prepare_board_after_switch(
            getattr(self, "_switch_from_port", self._profile.serial_port))

    def _prepare_board_after_switch(self, old_port: str):
        """Run the launch sequence for the board on a newly selected port.

        RULE: a profile on another serial port gets the launch-time clean
        flash and identity check at the switch, never at the first
        acquisition. REASON: the launch sequence ran only for the old port's
        board. Left alone, the first Record or Calibrate on the new profile
        opens the new port, which resets that board and floats its pins inside
        the experiment, and runs whatever sketch it carries; a calibration on
        a board holding a stimulation paradigm breaks "calibration can never
        activate stim". The old port's hint and board identity describe the
        other board and are forgotten first, so the flash runs.
        """
        new_port = self._profile.serial_port
        if new_port == old_port:
            return
        print(f"[acq] the profile moved the trigger board from "
              f"{old_port or '(none)'} to {new_port or '(none)'}; running the "
              f"launch firmware check for it", flush=True)
        self._forget_board_on_other_port()
        settings.set_board_sketch_hint("")
        self._board_id_stale = True
        self._board_identity_reflashed = False
        self._ensure_clean_firmware()

    def _apply_theme(self):
        app = QApplication.instance()
        app.setStyle("Fusion")
        p = QPalette()
        p.setColor(QPalette.Window, QColor(25, 25, 42))
        p.setColor(QPalette.WindowText, QColor(220, 220, 220))
        p.setColor(QPalette.Base, QColor(15, 15, 30))
        p.setColor(QPalette.AlternateBase, QColor(35, 35, 55))
        p.setColor(QPalette.Text, QColor(220, 220, 220))
        p.setColor(QPalette.Button, QColor(40, 40, 65))
        p.setColor(QPalette.ButtonText, QColor(220, 220, 220))
        p.setColor(QPalette.Highlight, QColor(80, 120, 200))
        p.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
        p.setColor(QPalette.ToolTipBase, QColor(40, 40, 65))
        p.setColor(QPalette.ToolTipText, QColor(220, 220, 220))
        app.setPalette(p)

    def _preview_lut(self):
        """The brightness/contrast table, or None when both sliders are zero.

        RULE: the adjustment is a 256-entry lookup, rebuilt only when a slider
        moves. REASON: it runs on the Qt main thread for every camera on every
        repaint, and two float32 copies of each frame there is work taken
        straight out of the grab threads' budget — the same budget the repaint
        period is widened to protect.
        """
        key = (self._sidebar.brightness, self._sidebar.contrast)
        if key == (0, 0):
            return None
        if key != self._lut_key:
            brightness, contrast = key
            table = np.arange(256, dtype=np.float32) - 128.0
            table *= (100 + contrast) / 100.0
            table += 128.0 + brightness
            self._lut = np.clip(table, 0, 255).astype(np.uint8)
            self._lut_key = key
        return self._lut

    def _refresh_displays(self):
        self._display_tick += 1
        lut = self._preview_lut()

        for i, frame in enumerate(self._camera_mgr.latest_frames):
            if frame is not None:
                if lut is not None:
                    frame = lut[frame]
                self._camera_grid.update_frame(i, frame)

        if self._display_tick % 10 == 0:
            for i, fps in enumerate(self._camera_mgr.current_fps):
                self._camera_grid.update_fps(i, fps)
            self._refresh_capture_health()

    def _refresh_capture_health(self):
        """Show how far behind real time capture is running, WHILE it happens.

        This is the one failure this pipeline cannot show you any other way. A
        grab loop that is a fraction of a millisecond over budget loses nothing
        at first — the driver's buffer pool absorbs the deficit — so there is no
        error, no dropped frame, and no clue, for as long as ten minutes. What
        actually happens is that every frame retrieved gets progressively
        staler, and by the time the pool is exhausted the session is spoiled.
        A live number is the only warning available before that point.

        Also worth knowing while aiming the rig: a large lag means the preview
        is showing you the past, not the present.
        """
        if self._state != State.RECORDING:
            return
        msg = self._frontier_health_text() or self._delivery_health_text()
        if msg is None:
            return
        # An overheating camera outranks a lag report: lag costs alignment,
        # thermal shutdown costs that camera for the rest of the session.
        if self._thermal_alert:
            msg = f"{self._thermal_alert}  |  {msg}"
        self.statusBar().showMessage(msg)

    def _frontier_health_text(self):
        """Kick-mode verdict, or None when kick-out is not running.

        RULE: in kick mode the verdict comes from the per-camera TRIGGER lag,
        not from delivery_lag_s. REASON: the coordinator holds every camera to
        the slowest one, so what decides whether frames are force-dropped is
        how many triggers a camera is behind the leader; and that count is
        made of block IDs, so it carries none of the clock drift a
        seconds-behind-real-time figure accumulates.
        """
        try:
            lags = self._camera_mgr.frontier_lags
        except Exception:
            return None
        live = [(n, i) for i, n in enumerate(lags) if n >= 0]
        if not live:
            return None
        retired = [self._camera_label(i + 1) for i, n in enumerate(lags) if n < 0]
        tail = f"  |  RETIRED: {', '.join(retired)}" if retired else ""
        worst, idx = max(live)
        cap = max(1, int(self._session_rig().kick_max_lag))
        name = self._camera_label(idx + 1)
        if worst < cap * 0.25:
            return (f"Capture healthy — every camera within {worst} trigger(s) "
                    f"of the leader{tail}")
        if worst < cap * 0.75:
            return (f"CAPTURE FALLING BEHIND: {name} is {worst} triggers "
                    f"behind the leader (cap {cap}). Close other "
                    f"applications.{tail}")
        return (f"{name} IS {worst} TRIGGERS BEHIND THE LEADER (cap {cap}): "
                f"frames every camera captured are being dropped. Stop and "
                f"investigate.{tail}")

    def _delivery_health_text(self):
        """Wall-clock verdict: how far behind real time delivery is running."""
        try:
            lags = self._camera_mgr.delivery_lags
        except Exception:
            return None
        if not lags:
            return None
        worst = max(lags)
        name = self._camera_label(lags.index(worst) + 1)
        if worst < 0.25:
            return (f"Capture healthy — keeping up with the trigger "
                    f"(max lag {worst * 1000:.0f} ms)")
        if worst < 1.0:
            return (f"CAPTURE FALLING BEHIND: {name} is {worst:.2f} s behind "
                    f"real time and growing. Close other applications.")
        return (f"CAPTURE {worst:.1f} s BEHIND REAL TIME ({name}). Frames will "
                f"be lost when the buffer pool fills. Stop and investigate.")

    def _start_thermal_watch(self):
        """Begin polling temperatures for this acquisition, if enabled."""
        secs = float(self._profile.thermal_poll_s or 0.0)
        if secs <= 0:
            return
        self._thermal_timer.start(int(secs * 1000))

    def _poll_thermals(self):
        """Warn about an overheating camera while there is still time to act.

        Temperatures must be polled DURING an acquisition: read only at stop
        they record the problem but cannot prevent it, because a camera that
        reaches its shutdown threshold stops delivering, and by the time the
        number is visible the session is already short a camera and the
        block-ID bookkeeping has had to truncate.

        Every threshold comes from the camera itself (its temperature status
        and its shutdown point, as the backend reports them), so this is not
        tied to one model or one rig. How hot a camera runs depends on its
        installation as much as on the camera: airflow, mounting and whether
        the model has a fan.
        """
        if self._state not in (State.RECORDING, State.CALIBRATING):
            self._thermal_timer.stop()
            return
        try:
            readings = self._camera_mgr.thermals()
        except Exception as e:
            print(f"[acq] thermal poll failed: {type(e).__name__}: {e}",
                  flush=True)
            return

        hot = []
        for idx, t in enumerate(readings, start=1):
            if not isinstance(t, dict) or t.get("error"):
                continue
            temp = t.get("temp_c")
            status = str(t.get("temp_status", "") or "").strip()
            shutdown = t.get("temp_shutdown_c")
            # The camera's own verdict is authoritative; the numeric comparison
            # is a fallback for a model that does not expose the status node.
            over = status.lower() not in ("", "ok")
            if temp is not None and shutdown is not None and temp >= shutdown:
                over = True
            if not over:
                continue
            margin = (shutdown - temp) if (temp is not None
                                           and shutdown is not None) else None
            # Sort key first: closest to shutdown is the one to name.
            hot.append((margin if margin is not None else 999.0,
                        idx, temp, status, margin))

        if not hot:
            self._thermal_alert = None
            return
        hot.sort()

        _key, idx, temp, status, margin = hot[0]
        name = self._camera_label(idx)
        if margin is None:
            gap = ""
        elif margin <= 0:
            # Past the vendor's own shutdown point: a negative margin read as
            # though there were headroom left, which is the opposite of true.
            gap = ", AT OR PAST ITS SHUTDOWN POINT"
        else:
            gap = f", {margin:.0f} C from shutdown"
        extra = "" if len(hot) == 1 else f" (+{len(hot) - 1} more)"
        temp_s = "?" if temp is None else f"{temp:.0f}"
        self._thermal_alert = (
            f"CAMERA TEMPERATURE: {name} {temp_s} C "
            f"{status or 'over limit'}{gap}{extra}")

        # One durable warning per camera per session, so this reaches
        # WARNINGS.txt and the post-session dialog and not just a status bar
        # message that scrolls past unread.
        for _key, idx, temp, status, margin in hot:
            if idx in self._thermal_reported:
                continue
            self._thermal_reported.add(idx)
            name = self._camera_label(idx)
            temp_s = "?" if temp is None else f"{temp:.1f}"
            tail = ""
            if temp is not None and margin is not None:
                if margin <= 0:
                    tail = (f", which is AT OR PAST its "
                            f"{temp + margin:.0f} C shutdown point")
                else:
                    tail = (f", {margin:.1f} C below its "
                            f"{temp + margin:.0f} C shutdown point")
            self._thermal_warnings.append(
                f"{name} reached {temp_s} C during this acquisition, which its "
                f"own firmware reports as '{status or 'over limit'}'{tail}. "
                f"Check the airflow around the camera and its mounting. A "
                f"camera that reaches its shutdown point stops delivering "
                f"mid-session.")
            print(f"[acq] THERMAL: {self._thermal_warnings[-1]}", flush=True)

    def _camera_label(self, idx: int) -> str:
        """Operator-facing name for a 1-based camera index."""
        if 0 < idx <= len(self._camera_names):
            return self._camera_names[idx - 1]
        return f"cam{idx}"

    def _build_config(self) -> SessionConfig:
        vals = self._sidebar.get_field_values()
        return SessionConfig.from_profile(
            self._profile,
            date=vals["date"],
            mouse_1=vals["mouse_1"],
            mouse_2=vals["mouse_2"],
            assay=vals["assay"],
            experimenter=vals["experimenter"],
            cohort=vals["cohort"],
            cage=vals["cage"],
            notes=vals["notes"],
            base_data_dir=Path(self._sidebar.output_dir),
            camera_names=self._camera_names,
        )

    def _on_calibrate_toggle(self, checked):
        if checked:
            self._start_acquisition("calibration")
        elif self._state == State.CALIBRATING:
            self._stop_acquisition()

    def _on_record_toggle(self, checked):
        if checked:
            self._start_acquisition("recording")
        elif self._state == State.RECORDING:
            self._stop_acquisition()

    def _warn_if_not_stood_down(self, stopped: bool) -> bool:
        """Surface a stop_triggers() failure. Returns what it was given.

        CLAUDE.md's invariant is that closing the GUI can never leave a paradigm
        or a laser running. The board is the only thing that can honour that, so
        when it does not accept the stop the operator has to be told — a
        swallowed failure turns a laser left running into a silent one.
        """
        if stopped:
            return True
        print("[acq] STOP NOT CONFIRMED — board may still be triggering",
              flush=True)
        try:
            QMessageBox.critical(
                self, "Trigger board did not confirm the stop",
                "The trigger board did not accept the stop command.\n\n"
                "It may still be triggering, and any stim paradigm — including "
                "a looping one, which never ends on its own — may still be "
                "driving its pin.\n\n"
                "Power-cycle the trigger board and key off the laser.")
        except Exception:
            pass       # a dialog failure must not mask the printed warning
        return False

    def _geometry_refusal(self) -> str:
        """The cameras disagreeing with the profile about geometry, or "".

        The profile's width/height size the NV12 ring and the raw decode, so
        a disagreement with the cameras' ROI retires every camera in real-time
        mode and shears a full-length recording in raw mode. Answered on the
        UI thread and before anything is started: start_acquisition can only
        refuse by raising, and by then the session directory has been touched.
        """
        p = self._profile
        return self._camera_mgr.geometry_mismatch(p.frame_width,
                                                  p.frame_height) or ""

    def _preflight_capacity(self, acq_type: str) -> tuple:
        """(blocking, warnings) for this acquisition. Runs on a worker thread.

        Weighs RAM, NVENC sessions and disk against the ACTUAL camera count
        and the frame rate THIS acquisition will run at.

        RULE: never called on the UI thread; the caller runs it under
        _begin_busy. REASON: the NVENC session count is cached, but a cached
        value BELOW what this start needs is always re-probed - a shortfall is
        usually another process holding sessions for a moment - and the probe
        spawns an isolated child and waits for it. On the UI thread that is a
        frozen window with no busy indicator, in exactly the transient case
        the probe exists to re-measure.
        """
        p = self._profile
        realtime = bool(p.realtime_encode)
        kick = realtime and bool(p.realtime_kick)
        # One formula, and it lives in grab_thread: the ring the preflight
        # budgets RAM for must be the ring a grab thread allocates, or the
        # preflight permits and refuses the wrong recordings.
        ring_n = ring_slots(p.kick_max_lag, kick)
        # A calibration runs at its own, lower rate; budgeting the recording
        # rate for it overstates the disk cost by the ratio between them.
        fps = (p.calibration_frame_rate if acq_type == "calibration"
               else p.frame_rate)
        try:
            blocking, warnings = check_capacity(
                n_cams=self._camera_mgr.num_cameras,
                width=p.frame_width, height=p.frame_height,
                ring_n=ring_n, max_num_buffer=p.max_num_buffer,
                realtime=realtime, output_dir=self._sidebar.output_dir,
                fps=fps, encoder=p.encoder)
        except Exception as e:
            # A broken preflight must never be what stops a recording.
            print(f"[acq] capacity preflight failed to run: {e}", flush=True)
            return [], []
        return list(blocking or []), list(warnings or [])

    def _on_capacity_checked(self, result):
        """Answer the capacity preflight, then carry the start on. UI thread."""
        if self._quitting:
            QApplication.restoreOverrideCursor()
            return
        self._end_busy()
        self._sidebar.set_status("IDLE", "#888")
        acq_type = self._acq_type
        if isinstance(result, tuple) and len(result) == 2:
            blocking, warnings = list(result[0] or []), list(result[1] or [])
        else:
            # CallableWorker delivers a raised exception AS the result, and a
            # preflight that could not answer must never be what stops a
            # recording.
            print(f"[acq] capacity preflight did not answer: {result}",
                  flush=True)
            blocking, warnings = [], []
        for w in warnings:
            print(f"[acq] capacity warning: {w}", flush=True)
        if blocking:
            print("[acq] REFUSING to start:\n  " + "\n  ".join(blocking), flush=True)
            QMessageBox.critical(
                self, "Cannot start",
                "\n\n".join(blocking)
                + ("\n\nWarnings:\n- " + "\n- ".join(warnings) if warnings else ""))
            self._reset_toggles()
            return
        if warnings:
            reply = QMessageBox.warning(
                self, "Proceed?", "\n\n".join(warnings) + "\n\nStart anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                self._reset_toggles()
                return
        # Firmware, then the port, then everything with a side effect:
        # arduino-cli needs the port to itself, and both have to be settled
        # before anything is written to disk or a camera is reconfigured.
        if not self._ensure_sketch_for(acq_type):
            return          # a flash is running; it re-enters _arm_acquisition
        self._arm_acquisition(acq_type)

    def _refuse_start(self, title: str, message: str) -> bool:
        """Report a refused start and put the sidebar back. Always False.

        reset_toggles(), not the silent variant: the state machine is at IDLE
        here, so letting the signal fire is a no-op for it (_on_record_toggle
        only acts when state == RECORDING) while still reversing the thumb
        animation and re-enabling the sibling toggle.
        """
        print(f"[acq] refusing start: {title}", flush=True)
        QMessageBox.critical(self, title, message)
        self._reset_toggles()
        return False

    def _stim_refusal(self, acq_type: str):
        """(title, message) when the stimulation editor is not ready for this
        acquisition, or None.

        Each refusal prevents a session whose recorded paradigm is not the one
        the animal received:

        - A bench Test is armed. It borrows this window's serial link, and its
          timed stop would cut this acquisition's camera triggers part-way
          through while the window still reads RECORDING.
        - record_blocker(): an Apply mid-flash (arduino-cli holds the port, and
          a second flash on it leaves the board in an unknown state), a failed
          Apply, or a canvas whose pins or parameters would break the block-ID
          identity every downstream consumer assumes.
        - The canvas differs from the paradigm this recording would flash. The
          board would run the held paradigm while stim_paradigm.json,
          stim_paradigm.ino and stim_trace.csv all describe the canvas. The
          comparison is against _session_stim_ino, NOT the editor's own last
          upload: a calibration invalidates that while the held paradigm is
          still what Record flashes back.
        """
        if self._stim_window is None:
            return None
        if self._stim_window.is_testing():
            return ("Stop the stimulation test first",
                    "A stimulation test is driving the trigger board.\n\n"
                    "Its timed stop would cut this acquisition's camera "
                    "triggers part-way through, and the window would go on "
                    "reading RECORDING with nothing being captured.\n\n"
                    "Stop the test in the Stimulation editor, then start "
                    "again.")
        blocker = self._stim_window.record_blocker()
        if blocker:
            return ("Cannot record with this stim workflow", blocker)
        if acq_type != "recording":
            return None          # a calibration always runs stimulation-free
        try:
            blocks, _edges = self._stim_window.get_workflow()
            if not blocks:
                return self._empty_canvas_refusal()
            canvas = self._stim_window.firmware_source()
        except Exception as e:
            return ("Cannot record with this stim workflow",
                    f"The stimulation canvas could not be compiled, so what "
                    f"the board would run cannot be established:\n\n{e}")
        if self._session_stim_ino is None:
            return ("Apply the stimulation paradigm first",
                    "The canvas holds a paradigm that has never been Applied, "
                    "so the board is carrying the recording-only sketch and "
                    "nothing would fire.\n\nThe recording would still be "
                    "labelled as stimulated: stim_paradigm.json, "
                    "stim_paradigm.ino and stim_trace.csv are all written from "
                    "the canvas.\n\nPress Apply in the Stimulation editor, or "
                    "clear the canvas.")
        if canvas != self._session_stim_ino:
            return ("Apply the edited paradigm first",
                    "The canvas has been edited since the last Apply, so this "
                    "recording would run the PREVIOUS paradigm while "
                    "stim_paradigm.json and stim_trace.csv describe the edited "
                    "one.\n\nPress Apply in the Stimulation editor, or undo "
                    "the edit.")
        return None

    def _empty_canvas_refusal(self):
        """(title, message) when the canvas is empty but the board would run a
        paradigm, or None.

        RULE: an empty canvas records only on a board that carries no
        paradigm. REASON: the recording flashes back the paradigm Applied
        earlier this session whatever the canvas shows, and an empty canvas
        writes no stim_paradigm.json, no stim_paradigm.ino and no
        stim_trace.csv, so the laser would fire through a recording that
        says it was unstimulated.
        """
        held = self._session_stim_ino
        if held is None:
            return None
        blank = stim_compiler.recording_only_sketch(
            self._profile.stim_safe_pins, self._profile.trigger_pins)
        if held == blank:
            return None
        return ("Apply the empty canvas first",
                "The canvas is empty, but the paradigm Applied earlier this "
                "session is still what this recording would put on the "
                "board, so the laser would fire through a recording with no "
                "stim_paradigm.json or stim_trace.csv.\n\nPress Apply in the "
                "Stimulation editor to clear the board, or Load the paradigm "
                "back onto the canvas.")

    def _snapshot_stim(self, acq_type: str) -> dict | None:
        """What this recording's stimulation files will say, fixed at arm time.

        RULE: stim_paradigm.json, stim_paradigm.ino and the auto-stop come
        from the canvas as it was when the last check passed, and the .ino is
        the sketch this recording flashes. REASON: the editor stays live
        while the start worker runs for seconds, and a block nudged then
        would become the recording's provenance, its firmware and its stop
        time although the board runs the sketch checked a moment before.

        None when there is nothing to record: a calibration (always
        stimulation-free), no editor, or an empty canvas.
        """
        if acq_type != "recording" or self._stim_window is None:
            return None
        try:
            blocks, _edges = self._stim_window.get_workflow()
            if not blocks:
                return None
            flashed, _label = self._sketch_for(acq_type)
            return {
                # The sketch THIS acquisition put on the board, not the
                # editor's own last upload: matches_uploaded_firmware answers
                # "did the animal receive what this file describes", and a
                # calibration clears the editor's record while the held
                # paradigm is still what Record flashes back.
                "provenance": self._stim_window.provenance(
                    flashed_source=flashed),
                "ino": flashed,
                "end_time_s": self._stim_window.end_time_s(),
            }
        except Exception as e:
            # Provenance must never take the recording down with it.
            print(f"[stim] could not record the paradigm at arm time: {e}",
                  flush=True)
            return None

    def _start_acquisition(self, acq_type: str):
        """Every check that can still refuse this acquisition, and nothing else.

        RULE: no directory, file, camera or encoder side effect happens here;
        those belong to _arm_acquisition. REASON: a firmware flash returns to
        the event loop and re-enters _arm_acquisition when it finishes, so a
        side effect placed here runs twice - which orphaned a router still
        holding one NVENC session and one open stream.h264 per camera,
        prompted about files the same run had just created, and left the
        cameras in trigger mode with the window reading IDLE when the flash
        failed.
        """
        # Refuse to start on top of a live or still-finalising acquisition.
        # The sidebar already disables the toggles while busy, so a user cannot
        # reach this - but any programmatic path that bypasses the widget
        # starts a SECOND acquisition whose state the machine then loses track
        # of (a second acquisition leaves the cameras streaming while _state
        # reads IDLE).
        if self._state != State.IDLE or self._busy:
            print(f"[acq] refusing start: state={self._state.value} "
                  f"busy={self._busy}", flush=True)
            self._sidebar.clear_toggle_silently(acq_type)
            return

        # A solve runs for minutes and never leaves IDLE, so the guard above
        # does not see it. Starting on top of one puts an acquisition and a
        # running solve child on the same machine, the same calibration.toml
        # and the same encoders.
        if self._solve_running():
            self._refuse_start(
                "A solve is running",
                "A calibration solve is still running.\n\nWait for it to "
                "finish: it is reading the calibration videos and writing "
                "calibration.toml, and an acquisition started on top of it "
                "competes for the same CPU and the same files.")
            return

        refusal = self._stim_refusal(acq_type)
        if refusal:
            self._refuse_start(*refusal)
            return

        # The sidebar's free text becomes directory names and the prefix of
        # every mp4 name, so it is validated where a dialog can be shown. Left
        # to the mkdir, a separator nests the session somewhere else, '..'
        # climbs out of the data directory, and the OSError lands in a Qt slot
        # with the toggle left on.
        try:
            config = self._build_config().validate()
        except ValueError as e:
            self._refuse_start("Check the session details",
                               f"{e}\n\nFix the field and start again.")
            return

        mismatch = self._geometry_refusal()
        if mismatch:
            self._refuse_start("Cannot start", mismatch)
            return

        self._config = config
        self._acq_type = acq_type
        # A fresh user start: forget any overwrite the operator agreed to on a
        # previous start. _arm_acquisition (which the firmware flash re-enters)
        # reads this and must not clear it, or the flash would re-prompt.
        self._overwrite_dir = None
        if self._worker_busy(self._cap_op):
            print("[acq] a capacity check is still running; not starting",
                  flush=True)
            self._reset_toggles()
            return
        # The rest of the start continues in _on_capacity_checked: the
        # capacity answer can cost an NVENC session probe, which is a child
        # process this thread would otherwise wait for.
        self._begin_busy("Checking capacity\u2026")
        self._cap_op = CallableWorker(lambda: self._preflight_capacity(acq_type))
        self._cap_op.done.connect(self._on_capacity_checked)
        self._cap_op.start()

    def _arm_acquisition(self, acq_type: str):
        """Everything with a side effect, once nothing can refuse the start.

        Re-entered by the firmware flash's completion callback. The checks
        that must NOT be repeated are the capacity preflight and the overwrite
        prompt: one is slow, the other asks the operator a question they have
        already answered (the consent is remembered in self._overwrite_dir).
        The stimulation predicate IS repeated, because it is pure and the
        canvas can be edited during the ~30 s flash - and a canvas edited
        there records a paradigm the board was never given.

        RULE: no file side effect happens here. The overwrite is only agreed
        to here; the delete, the directory creation and the clearing of the
        previous run's files all belong to the start worker, after the serial
        claim. REASON: the claim, the camera start and the board's ack can all
        still refuse, and a start refused because the port was busy - the
        common one - must not have destroyed the data it was going to replace.
        """
        if self._quitting:
            return
        refusal = self._stim_refusal(acq_type)
        if refusal:
            self._refuse_start(*refusal)
            return
        self._stim_snapshot = self._snapshot_stim(acq_type)

        config = self._config
        video_dir = config.video_dir(acq_type)
        if not self._confirm_overwrite(video_dir):
            self._reset_toggles()
            return

        self._video_dir = video_dir
        # What this start created, so a refused start can take it back out
        # instead of leaving an empty session folder behind. The worker fills
        # it, because the worker is what makes the directories.
        self._created_dirs = []
        self._capture_warnings = []
        self._thermal_warnings = []
        self._thermal_reported = set()
        self._thermal_alert = None
        self._finalized = False

        raw_paths = [self._video_dir / cam / "raw.bin"
                     for cam in self._camera_names]

        # Calibration runs at a lower trigger rate (still sharp, plenty of
        # distinct board poses) with a smooth 1:1 preview; recording stays at
        # the full rate with a decimated preview to protect the disk-write loop.
        fps = config.rate_for(acq_type)
        self._acq_fps = fps
        display_every = 1 if acq_type == "calibration" else 10
        rig = self._session_rig(config)
        rt = rig.realtime_encode
        kick = rig.realtime_kick
        print(f"[acq] start_acquisition({acq_type}) fps={fps} realtime={rt} "
              f"kick={kick}: switching cameras to trigger mode", flush=True)

        # Off the UI thread: the serial open, one trigger-mode reconfigure per
        # camera, the readiness barrier (each thread pre-faults a multi-GiB
        # NV12 ring) and the board's ack add up to seconds, and the rollback
        # for a failed start can cost the whole stop budget. On the UI thread
        # that is a 'not responding' window with the Stop toggle out of reach.
        if self._worker_busy(self._cam_op):
            print("[acq] a camera operation is still running; not starting",
                  flush=True)
            self._reset_toggles()
            return
        self._begin_busy("Starting...")
        self._cam_op = CallableWorker(
            lambda: self._start_body(acq_type, raw_paths, display_every,
                                     rt, kick, fps))
        self._cam_op.done.connect(self._on_acquisition_started)
        self._cam_op.start()

    def _confirm_overwrite(self, video_dir: Path) -> bool:
        """Ask the operator before overwriting a session that already holds
        data. False means abort the acquisition.

        Consent only: the directory is deleted later, by the worker, once the
        serial port is open and the start can no longer be refused for the
        common reason. Records the agreed directory in self._overwrite_dir so
        the firmware-flash re-entry does not ask twice, and so the worker
        knows exactly which directory the operator agreed to lose.

        RULE: the whole session directory is deleted, not overwritten file by
        file, and only the acquisition being started. REASON: recording over
        old files only replaces the ones THIS run writes - a camera that
        captures nothing would keep the previous session's mp4, blockids.npy
        and frametimes.npy under identical names, so eight cameras are this
        session and one is the last, with plausible block IDs, and alignment
        then intersects two different sessions. Deleting the directory whole
        removes that trap; the new run recreates it under the same name, which
        is what 1_calibrate and alignment.video_for resolve a session by. The
        files in KEPT_ON_OVERWRITE are the exception, and the prompt names
        them.
        """
        if self._overwrite_dir == video_dir:
            return True                       # already agreed this start
        if not _has_capture_data(video_dir):
            return True                       # nothing to overwrite, no prompt
        kept = [name for name in KEPT_ON_OVERWRITE
                if (video_dir / name).is_file()]
        keep_note = (f" {', '.join(kept)} is kept." if kept else "")
        reply = QMessageBox.question(
            self, "Overwrite the existing data?",
            f"{video_dir}\n\nalready holds data from an earlier acquisition."
            f"\n\nStarting will PERMANENTLY DELETE it and record over it. This "
            f"cannot be undone.{keep_note}\n\nOverwrite?",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if reply != QMessageBox.Yes:
            print("[acq] start cancelled; existing data left in place",
                  flush=True)
            return False
        self._overwrite_dir = video_dir
        return True

    def _overwrite_dir_if_agreed(self):
        """Delete the session the operator agreed to overwrite. Worker side,
        after the serial claim, so a port-busy refusal costs no data.

        RULE: rmtree only a path resolving strictly inside the output
        directory, and only the one the operator agreed to. REASON: the
        session path is built from free-text fields, and a component that
        escaped validation must never turn this into an rmtree of somewhere
        else. Same guard as the quit-time cleanup.
        """
        target = self._overwrite_dir
        if target is None or target != self._video_dir or not target.exists():
            return
        try:
            base = Path(self._sidebar.output_dir).resolve()
            resolved = target.resolve()
            inside = resolved.is_relative_to(base) and resolved != base
        except (OSError, ValueError):
            inside = False
        if not inside:
            raise OSError(f"refusing to overwrite {target}: outside the "
                          f"output directory {self._sidebar.output_dir}")
        kept = [name for name in KEPT_ON_OVERWRITE
                if (target / name).is_file()]
        aside = None
        if kept:
            # A sibling on the same volume, so each move is a rename and the
            # file keeps its timestamps.
            aside = Path(tempfile.mkdtemp(prefix=f".{target.name}-kept-",
                                          dir=target.parent))
            for name in kept:
                os.replace(target / name, aside / name)
        try:
            shutil.rmtree(target)
        finally:
            if aside is not None:
                self._restore_kept_files(target, aside, kept)
        print(f"[acq] overwrote existing data in {target}"
              + (f", keeping {', '.join(kept)}" if kept else ""), flush=True)

    @staticmethod
    def _restore_kept_files(target: Path, aside: Path, kept: list):
        """Put the files an overwrite kept back into the recreated directory."""
        try:
            target.mkdir(parents=True, exist_ok=True)
            for name in kept:
                os.replace(aside / name, target / name)
            aside.rmdir()
        except OSError as e:
            print(f"[acq] could not put {', '.join(kept)} back into {target}: "
                  f"{e}; it is in {aside}", flush=True)

    def _remove_created_dirs(self):
        """Take back the empty directories a refused start created.

        Only directories this start made, and only while they hold nothing but
        the zero-length stream files a router opens, so a refused start leaves
        no 'existing data' for the next attempt to move aside and no path
        holding real data can be removed here.
        """
        for path in reversed(self._created_dirs):
            try:
                for leftover in path.iterdir():
                    if leftover.is_file() and leftover.stat().st_size == 0:
                        leftover.unlink()
                path.rmdir()
            except OSError:
                pass
        self._created_dirs = []

    def _create_capture_dirs(self):
        """Make this acquisition's directories and clear what it writes into.

        Runs on the start worker, after the serial claim. If the operator
        agreed to overwrite, the whole previous session is deleted first, here
        rather than at the prompt, so a start refused because the port was
        busy leaves the old data untouched. After that only the files the
        capture itself opens are removed - a leftover raw.bin or stream.h264
        from a start that was refused would otherwise be written into or
        appended to.
        """
        self._overwrite_dir_if_agreed()
        if not self._video_dir.exists():
            self._created_dirs.append(self._video_dir)
        for cam in self._camera_names:
            cam_dir = self._video_dir / cam
            if not cam_dir.exists():
                self._created_dirs.append(cam_dir)
            cam_dir.mkdir(parents=True, exist_ok=True)
            for stale in ("raw.bin", "stream.h264"):
                try:
                    (cam_dir / stale).unlink(missing_ok=True)
                except OSError:
                    pass

    def _sweep_stale_diagnostics(self):
        """Remove a previous run's reports from this directory.

        RULE: runs only once the board has acked the start, i.e. once nothing
        can still refuse. REASON: these files are not capture sources - they
        are written at stop, at encode and after the solve - so nothing reads
        them between the start and here, while they ARE what the failed-camera
        dialog sends the operator to read. Swept before the refusable steps, a
        start the port or the board turned down destroys the evidence of the
        run before it, and _remove_created_dirs cannot put them back.

        They must go, though: WARNINGS.txt is the only durable trace of a
        block-ID reconciliation, and a stale one beside a clean recording is
        exactly what someone trusts months later; a leftover raw_tail.bin is
        appended to THIS recording's stream at stop; a codet_frames.json from
        a previous calibration points at frame numbers in videos this run
        replaces; and a stim_paradigm.json from a stimulated take becomes the
        paradigm stim_trace.csv describes for this one, which the recording
        writes again only when it has a paradigm of its own.
        """
        for stale in STALE_SESSION_FILES:
            try:
                (self._video_dir / stale).unlink(missing_ok=True)
            except OSError:
                pass
        for cam in self._camera_names:
            for stale in ("raw_tail.bin", "tail.h264", "encode_error.log",
                          "WARNINGS.txt"):
                try:
                    (self._video_dir / cam / stale).unlink(missing_ok=True)
                except OSError:
                    pass

    def _start_body(self, acq_type, raw_paths, display_every, realtime, kick,
                    fps) -> dict:
        """Claim the board, start the cameras, start the triggers.

        Runs on a worker thread and returns what _on_acquisition_started
        needs: {"ok": True}, or {"ok": False, "title", "message"} plus
        "cameras_closed" when the rollback had to abandon them.
        """
        try:
            return self._start_body_inner(acq_type, raw_paths, display_every,
                                          realtime, kick, fps)
        except Exception as e:
            traceback.print_exc()
            # Unknown ground: the cameras may be in trigger mode and the board
            # may already be triggering, so stand both down rather than leave
            # them running behind an IDLE window.
            return self._rollback_acquisition(
                f"Starting the {acq_type} failed:\n\n{type(e).__name__}: {e}",
                sent_start=True)

    def _start_body_inner(self, acq_type, raw_paths, display_every, realtime,
                          kick, fps) -> dict:
        """The start sequence proper.

        RULE: serial claim, directories, cameras, readiness barrier,
        triggers, and only then the sweep of the previous run's reports - in
        that order. REASON: the barrier exists so the board is never started
        while a grab thread is still allocating its ring; the serial claim
        comes first because a port that cannot be opened must not leave every
        camera sitting in trigger mode waiting for triggers that will never
        come, and because it is the refusal that must not have cost the
        target directory anything; and the sweep comes last because the files
        it removes are the ones a refused start needs to leave behind.
        """
        rig = self._session_rig()
        if self._quitting:
            return self._quit_during_start()
        teensy = self._teensy_connection(retries=self.START_SERIAL_RETRIES)
        if teensy is None:
            # Nothing has been started, so there is nothing to stand down: no
            # camera is in trigger mode and the board never saw a start.
            reason = getattr(self._teensy, "last_error", "") or ""
            return {"ok": False,
                    "title": "Could not open the trigger board",
                    "message": (
                        f"Serial port {self._profile.serial_port} could not be "
                        f"opened, so no triggers would be sent. Nothing has "
                        f"been started and nothing has been recorded."
                        f"\n\nClose Arduino Serial Monitor or anything else "
                        f"holding the port, check the cable, then start again."
                        + (f"\n\n{reason}" if reason else ""))}

        # The port is held, so the first file side effect of the whole start
        # happens here and not before.
        try:
            self._create_capture_dirs()
        except OSError as e:
            return {"ok": False,
                    "title": "Could not create the session directories",
                    "message": (
                        f"{self._video_dir}\n\ncould not be created:\n\n{e}"
                        f"\n\nNothing has been started and nothing has been "
                        f"recorded. Check the output directory, then start "
                        f"again.")}

        try:
            self._camera_mgr.start_acquisition(
                raw_paths, display_every=display_every,
                realtime=realtime, width=rig.frame_width,
                height=rig.frame_height, quality=rig.quality,
                fps=fps, realtime_kick=kick, kick_max_lag=rig.kick_max_lag,
                # Calibration gets its own exposure/gain when the profile sets
                # them; a recording passes None, which RESTORES the .pfs
                # values. Restoring rather than re-deriving is what guarantees
                # a long calibration exposure can never leak into a 100 fps
                # session, where it would silently halve the frame rate.
                exposure_us=(rig.calibration_exposure_us or None
                             if acq_type == "calibration" else None),
                gain_db=(rig.calibration_gain_db
                         if acq_type == "calibration"
                         and rig.calibration_gain_db >= 0 else None))
        except AcquisitionStartRefused as e:
            # Raised only once the cameras are back in free-run preview with
            # nothing recorded, so there is nothing to roll back.
            return {"ok": False, "title": "Cannot start the acquisition",
                    "message": str(e)}

        # Barrier: never start the board while a grab thread is still
        # allocating. See CameraManager.wait_until_ready for the measurement.
        t_bar = time.perf_counter()
        try:
            n_ready, n_tot = self._camera_mgr.wait_until_ready(30.0)
            waited = time.perf_counter() - t_bar
            flag = "" if n_ready == n_tot else "  *** NOT ALL READY ***"
            print(f"[acq] grab threads ready {n_ready}/{n_tot} after "
                  f"{waited:.2f}s{flag}", flush=True)
        except Exception as e:
            print(f"[acq] readiness barrier failed, starting anyway: {e}",
                  flush=True)
        try:
            # Printed beside the readiness line so a session's log records
            # where the capture threads actually ran, which is what a probe's
            # numbers have to be compared against.
            print(self._camera_mgr.pinning_report(), flush=True)
        except Exception as e:
            print(f"[acq] pinning report unavailable: {e}", flush=True)

        # The last moment a quit can still keep the start off the board. A
        # quit that lands after this is serialised by the controller: its stop
        # runs after this start and the start does not retry after it.
        if self._quitting:
            return self._quit_during_start()
        print(f"[acq] sending start_triggers "
              f"pins={self._profile.trigger_pins} fps={fps}", flush=True)
        counted_before_retry = []

        def may_retry() -> bool:
            """Refuse the reset-and-retry once any camera has frames.

            RULE: a board that speaks RDY is not reset and restarted under
            cameras that already counted triggers. REASON: the cameras are
            armed before the start, so frames here mean the first command did
            start the board and only its ack was lost. The reset restarts the
            board's trigger count and its stim state machine but not the
            cameras' block IDs, so every camera would carry the first
            attempt's triggers as an offset, and stim_trace.csv would place
            every stimulus that many frames late. Firmware that has never
            spoken RDY is exempt: for it the reset is how a start takes
            effect.
            """
            if not getattr(teensy, "speaks_rdy", False):
                return True
            try:
                counts = [int(n) for n in self._camera_mgr.frame_counts]
            except Exception as e:
                print(f"[acq] frame counts unavailable before the retry: {e}",
                      flush=True)
                return True
            if any(counts):
                counted_before_retry.extend(counts)
                return False
            return True

        if not teensy.start_triggers(self._profile.trigger_pins, fps,
                                     may_retry=may_retry):
            if self._quitting:
                # The quit closed the link under this start; the board is
                # already stood down.
                return self._quit_during_start()
            if counted_before_retry:
                return self._rollback_acquisition(
                    f"The trigger board did not confirm the start, but the "
                    f"cameras had already counted "
                    f"{max(counted_before_retry)} frames, so the board did "
                    f"start. Resetting it and starting again would leave "
                    f"every camera's block IDs offset from the board's "
                    f"trigger count, which shifts stim_trace.csv against the "
                    f"paradigm.\n\nThe start has been rolled back. Start "
                    f"again.", sent_start=True)
            # The board never confirmed the config, even after a forced reset.
            # Recording now would produce a full-length session with no frames.
            return self._rollback_acquisition(
                "The trigger board did not acknowledge the start command, so "
                "no triggers would be sent.\n\nCheck the Arduino is connected "
                "and running the Panopticon sketch, then retry.",
                sent_start=True)
        # Arm every grab thread's stall detector. Until this arrives a run of
        # retrieve timeouts is read as the board not yet running, so a camera
        # dead from the start is only retired when the pre-trigger grace
        # expires, minutes into the session.
        # The board has just printed its RDY line, so what it reports as its
        # sketch identity describes the firmware running now.
        self._board_id_stale = False
        refusal = self._wrong_sketch_refusal(teensy, acq_type)
        if refusal:
            return self._rollback_acquisition(refusal, sent_start=True)
        # Nothing can refuse the start from here, so the previous run's
        # reports can go.
        self._sweep_stale_diagnostics()
        self._camera_mgr.signal_triggers_started()
        print("[acq] start_acquisition done", flush=True)
        return {"ok": True}

    def _wrong_sketch_refusal(self, teensy, acq_type: str) -> str:
        """Why the board that just acked must not run this acquisition, or "".

        RULE: when the ack carries a sketch identity, it must be the sketch
        this acquisition needs. REASON: the flash decision before the start
        is made from what was known then (an identity heard earlier, or this
        machine's record of its last flash), and a board swapped, reflashed
        elsewhere or reached on a new port can differ from both; a
        calibration running a stimulation sketch breaks "calibration can
        never activate stim". The identity just heard is fresh, so the next
        start flashes the right sketch first.
        """
        heard = getattr(teensy, "board_id", None)
        want, label = self._sketch_for(acq_type)
        want_id = stim_compiler.sketch_id(want)
        if not heard or not want_id or heard == want_id:
            return ""
        print(f"[acq] the board acked with sketch {heard}, not the {label} "
              f"sketch {want_id}: refusing", flush=True)
        # Whatever this machine recorded about the board is now known to be
        # wrong, so the next start cannot skip the flash on it either.
        settings.set_board_sketch_hint("")
        return (f"The trigger board is running sketch {heard}, not the "
                f"{label} sketch this {acq_type} needs ({want_id}).\n\nThe "
                f"start has been rolled back. Start again: Panopticon flashes "
                f"the {label} sketch first.")

    def _quit_during_start(self) -> dict:
        """The start's answer when the window is quitting: send nothing.

        The cameras are left as they are. closeEvent has stood the board down
        and abandon() tears the cameras down once this worker returns, which
        is sooner than a rollback's full stop would let it.
        """
        print("[acq] quitting: the start stops here and sends nothing to the "
              "board", flush=True)
        return {"ok": False, "quitting": True, "title": "Quitting",
                "message": "Panopticon is closing; the acquisition was not "
                           "started."}

    def _on_acquisition_started(self, result):
        """Finish the start on the UI thread: dialogs, state, HUD."""
        if self._quitting:
            # Delivered after the quit: the board is stood down and the
            # cameras belong to abandon(), so nothing here may start a state.
            QApplication.restoreOverrideCursor()
            return
        self._end_busy()
        if not isinstance(result, dict):
            # CallableWorker delivers a raised exception AS the result, and
            # _start_body turns one into a rollback dict, so this is the
            # last-resort branch: report it rather than march on to RECORDING.
            result = {"ok": False, "title": "Could not start the acquisition",
                      "message": f"{type(result).__name__}: {result}"}
        if not result.get("ok"):
            self._remove_created_dirs()
            self._video_dir = None
            self._state = State.IDLE
            self._sidebar.set_status("IDLE", "#888")
            self._reset_toggles()
            if result.get("cameras_closed"):
                self._camera_grid.setup_grid(0)
                self._camera_names = []
            QMessageBox.critical(self, result.get("title", "Cannot start"),
                                 result.get("message", ""))
            return

        # The start is committed; the overwrite, if any, has already happened.
        self._overwrite_dir = None
        self._sidebar.set_fields_editable(False)
        self._start_thermal_watch()
        if self._acq_type == "calibration":
            self._state = State.CALIBRATING
            self._sidebar.set_status("CALIBRATING", "#4488ff")
            # No stim provenance is written for a calibration, and none is
            # needed: the board is reflashed stim-free before a calibration can
            # start, so there is never a paradigm to record.
            self._start_coverage_hud()
        else:
            self._state = State.RECORDING
            self._sidebar.set_status("RECORDING", "#ff4444")
            self._save_stim_paradigm()
            self._arm_stim_autostop()

    def _save_stim_paradigm(self):
        """Write the stimulus paradigm beside the video.

        Without this the only record of what the animal received is whatever the
        user happened to Save by hand, so a recording could not be interpreted
        after the fact. Written from the arm-time snapshot (_snapshot_stim),
        never from the canvas as it is now.
        """
        snap = self._stim_snapshot
        if snap is None:
            return
        try:
            (self._video_dir / "stim_paradigm.json").write_text(
                json.dumps(snap["provenance"], indent=2))
            (self._video_dir / "stim_paradigm.ino").write_text(
                snap["ino"], encoding="utf-8")
            print(f"[stim] paradigm saved to {self._video_dir}", flush=True)
        except Exception as e:
            # Provenance must never take the recording down with it.
            print(f"[stim] could not save paradigm: {e}", flush=True)

    def _write_stim_trace(self):
        """Emit the per-frame stimulus trace beside the videos.

        Makes the frame -> stimulation mapping explicit in the data instead of
        something every downstream analysis has to re-derive from the paradigm.
        """
        if self._video_dir is None:
            return
        try:
            out, msg = stim_trace.write_trace(self._video_dir, self._acq_fps or 100)
            print(f"[stim] trace: {msg}" if out else f"[stim] no trace: {msg}",
                  flush=True)
        except Exception as e:
            # Never let bookkeeping take down the save path.
            print(f"[stim] could not write trace: {e}", flush=True)

    def _arm_stim_autostop(self):
        """Stop the recording when the paradigm's 'Ending' block finishes.

        The stim sequence is baked into the sketch and starts on the same serial
        command as the triggers, so counting down from here is within a few ms of
        the Arduino's own clock. The end time is the arm-time snapshot's.
        """
        snap = self._stim_snapshot
        if snap is None:
            return
        secs = snap.get("end_time_s")
        if not secs or secs <= 0:
            return
        self._stim_end_timer.start(int(secs * 1000))
        print(f"[stim] auto-stop armed: {secs:g}s", flush=True)

    def _on_stim_end(self):
        if self._state == State.RECORDING:
            print("[stim] end block reached — stopping recording", flush=True)
            self._sidebar.stop_record()

    def _cancel_stim_autostop(self):
        self._stim_end_timer.stop()

    # ── shared trigger-board link ─────────────────────────────────────────────
    def _on_stim_applied(self, ino: str):
        """Remember the paradigm the editor just put on the board.

        Held for THIS SESSION only, deliberately. A launch always starts
        stimulation-free, so a paradigm never carries over from a previous
        session; but within a session this is what lets calibration and
        recording swap firmware automatically, so Apply is only needed when the
        paradigm itself changes.
        """
        self._session_stim_ino = ino
        try:
            settings.set_board_sketch_hint(stim_compiler.sketch_sha(ino))
            self._board_id_stale = True
        except Exception as e:
            print(f"[stim] could not record the applied sketch hash: {e}",
                  flush=True)
        print("[stim] paradigm applied and held for this session", flush=True)

    def _on_stim_upload_failed(self, touched_board: bool):
        """Forget what the board carries after an editor Apply failed on it.

        RULE: a failed flash that may have written the board clears the
        board-sketch hint, like this window's own failed flashes. REASON: the
        hint is what lets the next launch skip its clean flash, so a hint
        left claiming the pre-Apply sketch hands the next session a board
        whose flash may be half-written and missing its allStimLow() guard. A
        failure that never reached the board (a compile error) leaves the
        record true and keeps it.
        """
        if not touched_board:
            print("[stim] Apply failed before reaching the board; its "
                  "contents are unchanged", flush=True)
            return
        settings.set_board_sketch_hint("")
        self._board_id_stale = True
        print("[stim] Apply failed on the board; its contents are unknown "
              "until the next flash", flush=True)

    def _sketch_for(self, acq_type: str):
        """(source, label) of the firmware this acquisition must run under."""
        blank = stim_compiler.recording_only_sketch(
            self._profile.stim_safe_pins, self._profile.trigger_pins)
        if acq_type == "calibration" or not self._session_stim_ino:
            return blank, "recording-only"
        return self._session_stim_ino, "recording + stimulation"

    def _board_needs_flash(self, want: str) -> bool:
        """Whether the board has to be flashed to be carrying ``want``.

        RULE: the board's own RDY identity decides, and the stored sketch hash
        is consulted only when the board cannot identify itself. REASON: that
        hash records what THIS MACHINE last flashed onto whatever was on the
        port, so a board flashed from the Arduino IDE, swapped for another, or
        shared with a second rig leaves it claiming to be stimulation-free
        while carrying a paradigm — which is the case the flash exists to
        cover, and exactly the case the shortcut skips.

        A board that has not spoken since the last flash is not asked: the id
        it last printed is the previous sketch's, and reflashing what was just
        written would cost 30 s at every acquisition.
        """
        want_sha = stim_compiler.sketch_sha(want)
        want_id = stim_compiler.sketch_id(want)
        heard = getattr(self._teensy, "board_id", None)
        # An id heard on another port is another board's.
        same_port = (getattr(self._teensy, "port", None)
                     == self._profile.serial_port)
        if (heard is not None and want_id is not None and same_port
                and not self._board_id_stale):
            return heard != want_id
        return settings.board_sketch_hint() != want_sha

    def _confirm_board_identity(self):
        """Ask the board what it is carrying, and reflash if it disagrees.

        Standing the board down is how the identity is obtained: the sketch
        prints its RDY line only in answer to a config, and a stop is the one
        config that is always safe to send — it drives the camera pins and
        every stim pin LOW, which is also the right state for a board that has
        just been found running a paradigm from a previous session.

        At most one reflash per launch, so firmware that can never print an
        identity is not flashed over and over.
        """
        teensy = self._teensy
        if teensy is None or not teensy.is_open:
            return
        try:
            # The controller's identify() awaits the stop's ack even before
            # the first RDY line, which is the state at launch: stop_triggers
            # alone skips the ack then, so RDY firmware would read as pre-RDY
            # and the decision would fall back to the per-machine hint.
            heard = teensy.identify(self._profile.trigger_pins)
        except Exception as e:
            print(f"[acq] could not stand the board down at launch: {e}",
                  flush=True)
            return
        if heard is None:
            # Firmware that prints no identity: what this machine last flashed
            # is all there is, and it was already acted on.
            print("[acq] trigger board reported no sketch identity; its "
                  "contents are known only from what this machine last "
                  "flashed", flush=True)
            return
        want, _label = self._sketch_for("calibration")
        want_id = stim_compiler.sketch_id(want)
        self._board_id_stale = False
        if heard and want_id and heard == want_id:
            print(f"[acq] trigger board reports sketch {heard}: the "
                  f"recording-only sketch", flush=True)
            settings.set_board_sketch_hint(stim_compiler.sketch_sha(want))
            return
        if self._board_identity_reflashed:
            print(f"[acq] trigger board still reports sketch "
                  f"{heard or 'none'}; not flashing again this launch",
                  flush=True)
            return
        print(f"[acq] trigger board reports sketch {heard or 'none'}, not the "
              f"recording-only {want_id}; flashing", flush=True)
        self._board_identity_reflashed = True
        # Forget the hint first: it is what claimed the board was clean.
        settings.set_board_sketch_hint("")
        # arduino-cli drives avrdude, which cannot open a COM port this
        # process holds, and the port IS held here - this method is only
        # reached from _warm_serial, which has just opened it.
        # _on_clean_firmware_done reclaims it through _warm_serial in both
        # branches; _board_identity_reflashed is what stops that second
        # _warm_serial from starting a third flash.
        self.release_serial_port()
        self._ensure_clean_firmware()

    def _ensure_sketch_for(self, acq_type: str) -> bool:
        """Make the board carry the firmware this acquisition needs.

        Returns True to continue immediately, False to stop — either because a
        flash is now running (its completion re-enters _arm_acquisition, past
        the checks and the prompts that already ran) or because the board could
        not be put into a known state.

        Flashing takes ~30 s, so it happens only when the board is not already
        carrying the right sketch. In the common order — calibrate, then set up
        stimulation, then record — the calibration finds the launch-time
        stimulation-free sketch already in place and costs nothing.
        """
        try:
            want, label = self._sketch_for(acq_type)
            want_sha = stim_compiler.sketch_sha(want)
            needs_flash = self._board_needs_flash(want)
        except Exception as e:
            QMessageBox.critical(
                self, "Cannot prepare the trigger board",
                f"Could not build the firmware for this acquisition, so the "
                f"board cannot be put into a known state:\n\n{e}")
            self._reset_toggles()
            return False

        if not needs_flash:
            return True                      # already correct; no flash

        print(f"[acq] board needs the {label} sketch for a {acq_type}; flashing",
              flush=True)
        self.release_serial_port()           # arduino-cli needs the port alone
        if self._worker_busy(self._fw_op):
            QMessageBox.information(
                self, "Firmware upload in progress",
                "The trigger board is already being flashed. Wait for that to "
                "finish and start again.")
            self._reset_toggles()
            return False
        self._begin_busy(f"Flashing {label} firmware…")
        port = self._profile.serial_port
        self._fw_op = CallableWorker(lambda: stim_compiler.upload(want, port))

        def done(result):
            self._end_busy()
            self._sidebar.set_status("IDLE", "#888")
            ok, msg = result if isinstance(result, tuple) else (False, str(result))
            if not ok:
                # Retake the port before anything else, exactly as the success
                # branch does. RULE: whoever calls release_serial_port()
                # reclaims it on EVERY path out. REASON: left released, the
                # next Record is what reopens it - and the open pulses DTR,
                # resets the board and floats every pin, so the laser flash
                # the eager open exists to keep out of the experiment happens
                # inside one.
                self._teensy_connection()
                # Refusing is correct. The flash is what makes the board's
                # contents known, so a failed flash means they are not.
                print(f"[acq] firmware flash failed: {msg}", flush=True)
                settings.set_board_sketch_hint("")
                self._board_id_stale = True
                if self._stim_window is not None:
                    self._stim_window.invalidate_upload(
                        "the flash failed, so the board's contents are unknown")
                QMessageBox.critical(
                    self, f"Cannot start the {acq_type}",
                    f"The trigger board could not be flashed with the {label} "
                    f"firmware, so what it is running is unknown. The "
                    f"{acq_type} has not been started.\n\nKey off the laser and "
                    f"check the board, then retry.\n\n{msg}")
                self._reset_toggles()
                return
            # Retake the port BEFORE recording what was flashed: a reclaim
            # that finds the controller on another port forgets the hint, and
            # a hint recorded first would be wiped, costing a second flash.
            self._teensy_connection()
            settings.set_board_sketch_hint(want_sha)
            # Whatever the board printed last is the OLD sketch's identity.
            self._board_id_stale = True
            # The board now carries `want`. If that is not the paradigm the stim
            # editor last uploaded, the editor's record of what is on the board
            # is stale and MUST be cleared. Otherwise Test compares the canvas
            # against _uploaded_ino, finds them equal, skips the re-upload
            # prompt, and drives a board whose sketch has NUM_CHAINS == 0: the
            # laser never fires and nothing says so. A calibration reaches this
            # every time, because calibration always flashes recording-only.
            if (self._stim_window is not None
                    and self._session_stim_ino is not None
                    and want != self._session_stim_ino):
                self._stim_window.invalidate_upload(
                    f"a {acq_type} needed the {label} sketch")
            # Re-enter at the SIDE EFFECTS, not at the top: every check has
            # already passed, and repeating them would prompt a second time
            # about data and re-run the preflight for an acquisition the
            # operator has already confirmed.
            self._arm_acquisition(acq_type)

        self._fw_op.done.connect(done)
        self._fw_op.start()
        return False

    def _ensure_clean_firmware(self):
        """Put the board back to the recording-only sketch at every launch.

        A paradigm lives in the Arduino's FLASH, so it survives closing the GUI,
        power cycles and USB unplugs, while the canvas comes up empty. That
        combination means a blank-looking editor over a fully armed board, and
        Record would then fire a paradigm nobody chose. Stim is therefore
        opt-in per session: unless it was Applied since this launch, the board
        carries no stim.

        Flashing takes ~30 s, so the SHA of what this machine last uploaded is
        kept and the flash is skipped when it already matches. That record is a
        hint about this machine, not about the board, which is why
        _confirm_board_identity asks the board itself once the port is open and
        calls this again when the two disagree.

        Runs BEFORE _warm_serial: arduino-cli needs the port to itself.
        """
        # Defer rather than run alongside anything: this fires 1.5 s after
        # the window becomes interactive, so an acquisition or another flash
        # can already be under way, and assigning over a running _fw_op drops
        # the last reference to a live QThread (a qFatal) and puts a second
        # arduino-cli on a port the first avrdude holds.
        # The editor's Apply and Test run with this window idle and not busy,
        # and both need the port: a flash on top of an Apply puts two
        # avrdudes on one board, and one under a Test closes its link.
        editor_busy = self._stim_window is not None and (
            self._stim_window.is_uploading() or self._stim_window.is_testing())
        if (self._busy or self._state != State.IDLE
                or self._worker_busy(self._fw_op) or editor_busy):
            print("[acq] busy; deferring the launch firmware check", flush=True)
            QTimer.singleShot(1000, self._ensure_clean_firmware)
            return
        # No profile is no rig: there is no serial port to flash and no pin
        # list to drive low, so the upload would fail against an empty port
        # name and raise a third dialog telling the operator to key off a
        # laser on a rig that has no board configured. The no-profile dialog
        # says this instead.
        if not self._profile.name or not self._profile.serial_port:
            print("[acq] no profile with a serial port; the launch firmware "
                  "check cannot run and the board is left as it is",
                  flush=True)
            return
        try:
            blank = stim_compiler.recording_only_sketch(
                self._profile.stim_safe_pins, self._profile.trigger_pins)
            self._pending_sha = stim_compiler.sketch_sha(blank)
        except Exception as e:
            print(f"[acq] could not build the recording-only sketch: {e}", flush=True)
            self._warm_serial()
            return

        if settings.board_sketch_hint() == self._pending_sha:
            print("[acq] board already carries the recording-only sketch "
                  "(no stim); skipping flash", flush=True)
            self._warm_serial()
            return

        print("[acq] board may carry a stim paradigm from a previous session — "
              "flashing the recording-only sketch", flush=True)
        self._begin_busy("Clearing stim firmware…")
        port = self._profile.serial_port
        self._fw_op = CallableWorker(lambda: stim_compiler.upload(blank, port))
        self._fw_op.done.connect(self._on_clean_firmware_done)
        self._fw_op.start()

    def _on_clean_firmware_done(self, result):
        self._end_busy()
        # _end_busy() restores the cursor and re-enables the UI but deliberately
        # leaves the status text alone, because most callers replace it with a
        # new state. This one has no new state to show, so it must put the
        # sidebar back to IDLE itself — otherwise "Clearing stim firmware…"
        # stays on screen for the rest of the session.
        self._sidebar.set_status("IDLE", "#888")
        ok, msg = result if isinstance(result, tuple) else (False, str(result))
        if ok:
            settings.set_board_sketch_hint(self._pending_sha)
            self._board_id_stale = True
            print("[acq] board flashed with the recording-only sketch; stim is "
                  "off until you Apply one", flush=True)
        else:
            # Do NOT record the SHA: the board's contents are unknown here,
            # and recording it would claim the board is clean when nothing
            # said so.
            print(f"[acq] could not clear stim firmware: {msg}", flush=True)
            QMessageBox.warning(
                self, "Could not clear stim firmware",
                "Panopticon could not reflash the trigger board, so it may "
                "still be carrying a stimulation paradigm from a previous "
                "session — including one that loops and never ends.\n\n"
                "Open Stimulation and press Apply (an empty canvas is fine) "
                "before recording, or key off the laser.\n\n" + msg)
        self._warm_serial()

    def _warm_serial(self):
        """Claim the port at startup so the board's reset lands here.

        This eager open is the whole point and must not become lazy: first use
        would then BE the first Record, which just moves the reset flash into
        recording #1.

        Non-fatal, though: one attempt here, and if the board is not reachable
        yet the next start retries on its worker.
        """
        if self._teensy_connection(retries=self.UI_SERIAL_RETRIES) is None:
            print(f"[acq] trigger board not reachable on {self._profile.serial_port} "
                  f"at startup; will retry on first use", flush=True)
            return
        self._confirm_board_identity()

    def _teensy_connection(self, retries: int | None = None
                           ) -> TeensyController | None:
        """The one serial link to the trigger board, kept open for the session.

        ``retries`` defaults to UI_SERIAL_RETRIES, because every caller but
        the start worker is on the UI thread.

        Opening the port resets the Arduino, and during the reset + bootloader
        every pin floats — long enough for a connected laser to fire. Holding
        the connection open means that only happens at GUI launch (_warm_serial
        claims the port eagerly) and on upload, never at the start of a
        recording.
        """
        self._forget_board_on_other_port()
        if self._teensy is None:
            self._teensy = TeensyController(port=self._profile.serial_port)
        if not self._teensy.is_open:
            print(f"[acq] opening teensy on {self._profile.serial_port}", flush=True)
            if retries is None:
                retries = self.UI_SERIAL_RETRIES
            if not self._teensy.open(retries=retries):
                return None
        return self._teensy

    def _forget_board_on_other_port(self) -> bool:
        """Close a link to a port the profile no longer names. True if it did.

        A different port is a different board: what this machine last flashed
        says nothing about what is on this one, so the board-sketch hint is
        forgotten and the old board's identity is no longer believed.
        """
        if self._teensy is None or self._teensy.port == self._profile.serial_port:
            return False
        self._teensy.close()
        self._teensy = None
        settings.set_board_sketch_hint("")
        self._board_id_stale = True
        return True

    def release_serial_port(self):
        """Hand the port back so arduino-cli can upload.

        The caller MUST reclaim it via _teensy_connection() as soon as the
        upload finishes (_ensure_sketch_for's done() and the stim editor's
        _on_upload_done both do). Leaving it to reopen lazily would put the
        open — and the board reset, and the laser flash — back at the start of
        the next recording.
        """
        if self._teensy is not None and self._teensy.is_open:
            print("[acq] releasing serial port for upload", flush=True)
            self._teensy.close()

    def _rollback_acquisition(self, message: str, sent_start: bool) -> dict:
        """Undo a half-started acquisition and describe it for the dialog.

        Runs on the start worker's thread, never the UI thread: stop_acquisition
        can cost the whole stop budget (5 s + 10 s + 100 s), which on the UI
        thread is a frozen window.

        RULE: the board stands down before the cameras, and only when a start
        was actually sent. REASON: the failing branch is the one where the board
        may have consumed the config, begun triggering and run initStim() but
        failed to ack, so a paradigm and its laser can be live right now, and
        rolling back only the cameras leaves that running behind an IDLE window.
        A port that never opened, on the other hand, received no start: telling
        the operator there that the board "did not accept the stop" sends them
        to power-cycle a board that was never running.
        """
        if sent_start and self._teensy is not None:
            if not self._teensy.stop_triggers(self._profile.trigger_pins):
                message += ("\n\nWARNING: the trigger board did not accept the stop "
                            "command. It may still be triggering and any stim "
                            "paradigm may still be running. Power-cycle the board "
                            "and key off the laser before continuing.")
        result = {"ok": False, "title": "Could not start the acquisition",
                  "message": message}
        try:
            self._camera_mgr.stop_acquisition()
        except AcquisitionStopIncomplete as e:
            # A grab thread outlived the stop bounds, so the cameras were left
            # untouched for abandon(); reconfiguring a handle under a live
            # RetrieveResult is concurrent native access, not an exception.
            print(f"[acq] rollback could not stop cleanly: {e}", flush=True)
            try:
                self._camera_mgr.abandon()
            except Exception as ae:
                print(f"[acq] abandon during rollback failed: {ae}", flush=True)
            result["cameras_closed"] = True
            result["message"] += (
                "\n\nThe cameras could not be stopped cleanly and have been "
                "closed. Switch profile and back (or restart Panopticon) to "
                "reopen them.")
            return result
        except Exception as e:
            print(f"[acq] rollback stop failed: {e}", flush=True)
        try:
            self._camera_mgr.resume_preview()
        except Exception as e:
            print(f"[acq] rollback could not resume preview: {e}", flush=True)
        return result

    def _start_coverage_hud(self):
        """Spin up the live ChArUco coverage graph for this calibration run."""
        self._detector = None
        board_cfg = self._profile.board_config
        n = self._camera_mgr.num_cameras
        if BoardDetector is None or n == 0 or not board_cfg or not Path(board_cfg).exists():
            return
        try:
            p = self._profile
            self._detector = BoardDetector(
                n, board_cfg,
                min_per_cam_shared=p.calibration_min_per_cam_shared,
                min_edge=p.calibration_min_edge,
                min_grid_cells=p.calibration_min_grid_cells)
        except Exception as e:
            print(f"[hud] coverage detector unavailable: {e}", flush=True)
            self._detector = None
            return
        self._sidebar.setup_coverage(n)
        self._sidebar.show_coverage()

        # Run detection off the UI thread on full-res frames (resolves oblique
        # cams, same as the post-hoc calibration).
        self._camera_mgr.set_keep_full(True)
        self._coverage_worker = CoverageWorker(self._detector, self._camera_mgr)
        self._coverage_worker.updated.connect(self._on_coverage_updated)
        self._coverage_worker.start()

    def _on_coverage_updated(self):
        if self._detector is not None:
            self._sidebar.update_coverage(self._detector)

    def _stop_coverage_hud(self, timeout_ms: int = 5000):
        """Join the coverage worker, then take its co-detection record.

        RULE: the reference is kept until the thread has actually finished,
        and codet_frames is read only once the join succeeded. REASON: the
        loop exits between ticks and one tick is a ChArUco pass over every
        camera's full-resolution frame, which on a loaded host runs past a
        short wait; dropping the last reference to a running QThread is a
        qFatal that no excepthook can intercept, and the detector's
        dictionaries are still being written while the thread lives.

        ``timeout_ms`` <= 0 waits without bound, which is what the close path
        wants: the join is bounded by one detection pass either way.
        """
        joined = True
        worker = self._coverage_worker
        if worker is not None:
            worker.stop()
            joined = worker.wait() if timeout_ms <= 0 else worker.wait(timeout_ms)
            self._coverage_worker = None
            if not joined:
                print("[hud] coverage worker still inside a detection pass; "
                      "keeping the reference until it finishes", flush=True)
                self._retired_workers.append(worker)
                worker.finished.connect(lambda w=worker: self._retire_worker(w))
        if joined and self._detector is not None and self._detector.codet_frames:
            self._save_codet_frames(self._detector.codet_frames)
        try:
            self._camera_mgr.set_keep_full(False)
        except Exception:
            pass

    def _join_retired_workers(self):
        """Wait for every worker that outlived its join.

        RULE: no QThread is still running when this process exits. REASON: a
        worker parked here missed its bounded join mid-session and nothing
        else waits for it, so at interpreter exit its last reference is
        dropped while it runs - the qFatal "QThread: Destroyed while thread is
        still running", which no excepthook can intercept. The wait is bounded
        by one detection pass, and the reference is kept either way.
        """
        for worker in list(self._retired_workers):
            try:
                worker.wait(5000)
            except Exception as e:
                print(f"[quit] could not join a retired worker: {e}",
                      flush=True)

    def _retire_worker(self, worker):
        """Let go of a worker that outlived its join, once Qt reports it done."""
        try:
            self._retired_workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

    def _stop_acquisition(self):
        # Stop the temperature poll FIRST. It is a GVCP register read on every
        # camera, made from the UI thread, and the finalize worker is about to
        # be inside pylon on the same devices; two threads making native calls
        # on one device is an access violation, not an exception. _poll_thermals
        # only stops itself on a state change, and the state does not change
        # until the finalize returns.
        self._thermal_timer.stop()
        self._cancel_stim_autostop()
        # Stop the triggers but KEEP the port open: reopening it would reset the
        # board at the start of the next recording and flash a connected laser.
        # This is the everyday stop — the one taken every session — so it is the
        # path where a swallowed failure matters most, not least.
        self._warn_if_not_stood_down(
            self._teensy is not None
            and self._teensy.stop_triggers(self._profile.trigger_pins))

        self._stop_coverage_hud()
        self._detector = None
        self._sidebar.hide_coverage()

        # Draining the encoders + reconfiguring 6 cameras back to preview is ~1 s
        # of blocking work; run it off the UI thread so the window stays live.
        if self._worker_busy(self._cam_op):
            print("[acq] a camera operation is still running; the stop will "
                  "be completed by it", flush=True)
            return
        self._begin_busy("Finishing…")

        def _finalize():
            # SAVE the captured data before restoring preview — restoring can
            # fail if a camera dropped off the bus mid-session, and saving first
            # guarantees the surviving cameras' recordings aren't lost.
            try:
                cam_results = self._camera_mgr.stop_acquisition()
            except AcquisitionStopIncomplete as e:
                # A grab thread outlived the stop bounds. The healthy cameras'
                # data is on the exception and must be written BEFORE the slot
                # abandons, or blockids.npy and frametimes.npy are written for
                # NO camera and even the cameras that finished cannot be
                # aligned. The cameras are left untouched for abandon().
                print(f"[acq] stop incomplete, saving what was collected: {e}",
                      flush=True)
                self._save_frametimes(e.results)
                self._save_acquisition_metadata()
                self._write_stim_trace()
                self._finalized = True
                raise
            self._save_frametimes(cam_results)
            # Read thermals BEFORE resume_preview: DeviceTemperature starts
            # decaying the moment the load comes off, and how hot a camera got
            # under load cannot be recovered after the fact.
            try:
                self._config.camera_thermals = self._camera_mgr.thermals()
            except Exception as e:
                print(f"[acq] thermals unavailable: {e}", flush=True)
            self._save_acquisition_metadata()
            self._write_stim_trace()   # needs blockids, so after _save_frametimes
            # Set before resume_preview, which is the one step after this that
            # can still fail: everything the session consists of is on disk by
            # now, so a quit racing the restore must not delete it.
            self._finalized = True
            self._camera_mgr.resume_preview()

        self._cam_op = CallableWorker(_finalize)
        self._cam_op.done.connect(self._on_acquisition_finalized)
        self._cam_op.start()

    def _save_acquisition_metadata(self):
        """Write session_metadata.json for THIS acquisition.

        RULE: the file goes beside the videos it describes and carries the
        cameras' transport statistics from the same stop. REASON: a single
        session-level file was overwritten by whichever acquisition ran last,
        so a recording's thermals, timestamp and environment were replaced by
        the calibration's; and the resend, failed-buffer and bandwidth figures
        are the network state of that one session, which nothing can recover
        once the rig has moved on.
        """
        try:
            path = self._config.save_metadata(self._acq_type)
        except Exception as e:
            print(f"[acq] could not write session metadata: {e}", flush=True)
            return
        stats = list(getattr(self._camera_mgr, "last_stream_stats", []) or [])
        if not stats:
            return
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta["camera_stream_stats"] = stats
            path.write_text(json.dumps(meta, indent=2, default=str),
                            encoding="utf-8")
        except Exception as e:
            print(f"[acq] could not record stream statistics: {e}", flush=True)

    def _on_acquisition_finalized(self, _result):
        self._end_busy()
        # This slot MUST inspect its argument. CallableWorker delivers a
        # raised exception AS the result, and a _finalize that raises (a full
        # disk at np.save, anything inside _router.stop()) leaves
        # camera_manager short of `self._router = None`: the encoder threads
        # never get their sentinel and block forever holding NVENC sessions.
        # An exception ignored here then marches the window on to ENCODING,
        # which remuxes an unflushed stream.h264 and deletes the source,
        # silently.
        # Capture-side problems (a retired camera, block-ID truncation) reach
        # the operator here or not at all: camera_manager drops the router right
        # after reading them.
        self._capture_warnings = list(getattr(self._camera_mgr, "last_warnings", []))
        if isinstance(_result, Exception):
            print(f"[acq] FINALIZE FAILED: {type(_result).__name__}: {_result}",
                  flush=True)
            stuck = ""
            if isinstance(_result, AcquisitionStopIncomplete) and _result.stuck:
                stuck = ", ".join(self._camera_label(i + 1)
                                  for i in _result.stuck)
            try:
                self._camera_mgr.abandon()
            except Exception as e:
                print(f"[acq] abandon after failed finalize also failed: {e}",
                      flush=True)
            self._state = State.IDLE
            self._sidebar.set_status("IDLE", "#888888")
            self._reset_toggles()
            # abandon() closed every camera, so the preview is dead and the
            # next start would be refused with "No cameras are open". Say so,
            # and give the fields back: the dialog tells the operator to record
            # somewhere else, which is exactly what editing them is for.
            self._sidebar.set_fields_editable(True)
            self._camera_grid.setup_grid(0)
            self._camera_names = []
            QMessageBox.critical(
                self, "Recording did not finish cleanly",
                f"Saving the recording failed:\n\n{type(_result).__name__}: "
                f"{_result}\n\n"
                + (f"Still running when the stop gave up: {stuck}.\n\n"
                   if stuck else "")
                + f"The raw capture files are still in:\n"
                f"{self._video_dir}\n\nThey have NOT been encoded or deleted. Do "
                f"not start another recording into that directory.\n\nThe "
                f"cameras have been closed: switch profile and back, or restart "
                f"Panopticon, to reopen them.")
            return
        self._state = State.ENCODING
        self._sidebar.set_status("ENCODING", "#ffaa00")
        self._sidebar.set_toggles_enabled(False)

        rig = self._session_rig()
        self._encode_worker = EncodeWorker(
            self._video_dir,
            self._camera_names,
            self._acq_type,
            rig.frame_width,
            rig.frame_height,
            self._acq_fps,
            rig.quality,
            self._config.date,
            self._config.session_id,
            max_parallel=rig.encode_parallel,
            realtime=rig.realtime_encode,
        )
        self._encode_worker.progress.connect(self._sidebar.show_progress)
        self._encode_worker.finished_all.connect(self._on_encoding_done)
        self._encode_worker.start()

    def _save_codet_frames(self, codet_frames: list[dict[int, int]]):
        """Save co-detection frame indices so 1_calibrate.py can skip full-video
        scanning and only process frames where the board was co-visible.

        The layout is board_detector's, not a second copy of it here: the
        solve validates the hint file against the videos it opens, and a
        writer that drifts from the reader is refused and falls back to a
        full scan with no visible symptom but a slow solve.
        """
        if not self._video_dir:
            return
        path = self._video_dir / "codet_frames.json"
        try:
            n = board_detector.write_codet_frames(
                path, codet_frames, self._camera_names)
        except Exception as e:
            print(f"[hud] could not save co-detection hints: {e}", flush=True)
            return
        print(f"[hud] saved {n} co-detection frame indices to {path.name}",
              flush=True)

    def _save_frametimes(self, cam_results: list[tuple[int, list[float], list[int]]]):
        counts = [len(ts) for _, ts, _ in cam_results if ts]
        if not counts:
            return
        min_frames = min(counts)
        rig = self._session_rig()
        realtime = rig.realtime_encode

        for i, (count, timestamps, block_ids) in enumerate(cam_results):
            if not timestamps:
                continue
            cam = self._camera_names[i]
            cam_dir = self._video_dir / cam

            # Realtime: the mp4 carries every encoded frame, so save FULL
            # per-camera frametimes + blockids — auto-alignment then trims them
            # to the frames every camera captured. Raw mode truncates raw.bin
            # (below) to the min count for positional cross-cam consistency, so
            # its frametimes/blockids are truncated to match.
            n = len(timestamps) if realtime else min_frames
            frame_nums = np.arange(1, n + 1, dtype=np.float64)
            ts_arr = np.array(timestamps[:n])
            ts_arr -= ts_arr[0]
            np.save(cam_dir / "frametimes.npy", np.stack([frame_nums, ts_arr]))
            # Block ID = trigger ordinal; dropped frames show as gaps, so
            # cross-camera alignment survives a drop (see gui_app/alignment.py).
            if block_ids:
                bids = np.asarray(block_ids, dtype=np.int64)
                np.save(cam_dir / "blockids.npy", bids if realtime else bids[:n])

            raw_path = cam_dir / "raw.bin"
            if raw_path.exists():
                frame_size = rig.frame_width * rig.frame_height
                expected_size = min_frames * frame_size
                actual_size = raw_path.stat().st_size
                if actual_size > expected_size:
                    with open(raw_path, "r+b") as f:
                        f.truncate(expected_size)

    def _on_encoding_done(self, results):
        self._sidebar.hide_progress()

        frame_counts = [n for _, n, ok in results if ok]
        fps_vals = []
        for cam, n_frames, ok in results:
            if not ok:
                continue
            cam_dir = self._video_dir / cam / "frametimes.npy"
            try:
                ft = np.load(cam_dir)
                duration = ft[1][-1] - ft[1][0]
                fps_vals.append(ft.shape[1] / duration if duration > 0 else 0)
            except Exception:
                fps_vals.append(0)

        avg_fps = sum(fps_vals) / len(fps_vals) if fps_vals else 0
        min_frames = min(frame_counts) if frame_counts else 0
        max_frames = max(frame_counts) if frame_counts else 0
        count_str = str(min_frames) if min_frames == max_frames else f"{min_frames}-{max_frames}"

        # Build the summary from ALL cameras, not just the ones that worked.
        # Skipping failures meant "encoded: 6022 frames" could describe a
        # session where two of nine cameras produced no video at all.
        failed = [cam for cam, _n, ok in results if not ok]
        status = (f"{self._acq_type.title()} encoded: {count_str} frames, "
                  f"{avg_fps:.1f} fps"
                  + (f"  —  {len(failed)} CAMERA(S) FAILED: {', '.join(failed)}"
                     if failed else ""))
        self.statusBar().showMessage(status)

        problems = list(self._capture_warnings)
        # The encoder's own warnings are the truncated-tail cases: the mp4 has
        # fewer frames than the capture intended and the metadata has been
        # truncated to match, with the full arrays kept beside it. Nothing
        # else reports them, and they are exactly what makes a session's
        # frame count disagree with its trigger record.
        problems += list(self._encode_worker.warnings)
        if failed:
            problems.append(
                f"{len(failed)} camera(s) produced no usable video: "
                f"{', '.join(failed)}. Their source files were KEPT rather than "
                f"deleted — look for raw.bin / stream.h264, raw_tail.bin, "
                f"encode_error.log, tail_error.log and WARNINGS.txt in those "
                f"camera directories.")
        # Temperature is reported LIVE during the run (the status-bar alert),
        # which is where it can still be acted on. After encoding it is added
        # to the report only when the session actually lost frames, so a clean
        # recording on chronically-warm cameras is not flagged for heat that
        # cost nothing - the common case on a rig where several cameras sit
        # above Critical by installation. When frames WERE lost, the thermal
        # history is included so overheating is on the table as the cause.
        # Either way the temperatures stay in session_metadata.json.
        lost_frames = bool(self._capture_warnings) or bool(failed) \
            or bool(self._encode_worker.warnings) \
            or (len(frame_counts) > 1 and min_frames != max_frames)
        if self._thermal_warnings and lost_frames:
            problems += list(self._thermal_warnings)
        if problems:
            # Write it down as well as showing it: a dialog is dismissed and
            # forgotten, and this is exactly what someone needs months later
            # when the data looks odd.
            warnings_path = self._video_dir / "WARNINGS.txt"
            body = "\n\n".join(problems)
            try:
                warnings_path.write_text(body + "\n", encoding="utf-8")
            except Exception as e:
                print(f"[acq] could not write WARNINGS.txt: {e}", flush=True)
            QMessageBox.warning(
                self, "Recording completed with problems",
                f"{body}\n\nThis has also been written to:\n{warnings_path}")

        # Kick-out keeps every camera on the same triggers, so a kick-mode
        # session is NOT auto-aligned, even after it dropped frames: the
        # operator asked for the forced-drop and block-rate warnings to be
        # reported (WARNINGS.txt, above) rather than followed by an
        # unrequested re-encode. Only the explicit post-hoc mode (realtime_kick
        # False) trims at stop. A kick session whose videos are genuinely
        # unequal length - a retirement, a truncated tail - is flagged so the
        # operator can run 2_align.py by hand instead of it happening silently.
        rig = self._session_rig()
        if rig.realtime_encode and not rig.realtime_kick:
            if self._start_alignment():
                return
        elif rig.realtime_encode:
            self._warn_if_unequal_videos()
        self._finish_to_idle()

    def _warn_if_unequal_videos(self):
        """Flag a kick-mode session whose per-camera videos are not equal
        length, without re-encoding it. The operator chose to skip auto-align,
        but an unequal set left unremarked is the silent trap this reports.
        """
        try:
            _names, blocks, _videos = alignment.load_blockids(self._video_dir)
            if not alignment.needs_alignment(blocks):
                return
        except Exception as e:
            print(f"[align] equal-length check skipped ({e})", flush=True)
            return
        note = ("The cameras did not all keep the same frames, so the videos "
                "are not equal length. Auto-alignment is off, so they are left "
                "as recorded. Run 2_align.py on this session to trim them to "
                "the common frames before using them together.")
        print(f"[align] {note}", flush=True)
        warnings_path = self._video_dir / "WARNINGS.txt"
        try:
            with warnings_path.open("a", encoding="utf-8") as f:
                f.write("\n" + note + "\n")
        except OSError as e:
            print(f"[align] could not append to WARNINGS.txt: {e}", flush=True)
        QMessageBox.warning(self, "Videos are not equal length", note)

    def _start_alignment(self) -> bool:
        """Start the align worker if cameras dropped different frames. Returns
        True if alignment is now running (caller should defer the idle reset)."""
        try:
            _names, blocks, _videos = alignment.load_blockids(self._video_dir)
        except Exception as e:
            print(f"[align] skipped ({e})", flush=True)
            return False
        try:
            if not alignment.needs_alignment(blocks):
                return False  # loss-free: videos already equal-length + aligned
        except Exception as e:
            print(f"[align] check failed ({e})", flush=True)
            return False

        self._state = State.ALIGNING
        self._sidebar.set_status("ALIGNING", "#ffaa00")
        self._sidebar.set_toggles_enabled(False)
        self.statusBar().showMessage("Aligning videos by trigger (re-encode)...")
        rig = self._session_rig()
        self._align_worker = AlignWorker(
            self._video_dir, self._acq_fps, rig.quality,
            parallel=rig.encode_parallel)
        self._align_worker.progress.connect(self._on_align_progress)
        self._align_worker.finished_align.connect(self._on_align_done)
        self._align_worker.start()
        return True

    def _on_align_progress(self, done: int, total: int, msg: str):
        self._sidebar.show_progress(done, total, label="Aligning")
        self.statusBar().showMessage(f"Aligning {done}/{total}: {msg}")

    def _regenerate_stim_trace(self, then):
        """Rewrite stim_trace.csv off the UI thread, then call ``then``.

        RULE: not on the UI thread, and the window stays busy until it is
        written. REASON: the trace is one Python row per recorded frame, so a
        17-minute session at 100 fps is ~100k rows and seconds of frozen
        window — in the post-hoc mode that exists for troubled sessions —
        and returning to IDLE first would let a new acquisition move
        _video_dir under the worker.
        """
        if self._worker_busy(self._cam_op):
            print("[stim] a camera operation is still running; the trace is "
                  "left as it is", flush=True)
            then()
            return
        self._begin_busy("Updating the stimulus trace...")
        self._cam_op = CallableWorker(self._write_stim_trace)

        def done(_result):
            self._end_busy()
            then()

        self._cam_op.done.connect(done)
        self._cam_op.start()

    def _on_align_done(self, summary: dict):
        self._sidebar.hide_progress()
        failures = list(summary.get("failures") or [])
        replaced_cams = list(summary.get("replaced_cams") or [])
        n_cams = len(summary.get("camera_names") or self._camera_names)
        if summary.get("error"):
            self.statusBar().showMessage(
                f"Alignment failed: {summary['error']} — videos left as-is")
        elif summary.get("replaced"):
            self.statusBar().showMessage(
                f"Aligned: {summary.get('common_frames', 0)} synchronized "
                f"frames per camera")
        elif replaced_cams:
            # Wording comes from failures, never from warnings: a block-rate
            # warning is informational and says nothing about whether a
            # camera's video was replaced.
            self.statusBar().showMessage(
                f"Aligned {len(replaced_cams)} of {n_cams} cameras "
                f"({', '.join(replaced_cams)}); "
                f"{len(failures)} could not be replaced — see the log")
        elif failures:
            self.statusBar().showMessage(
                f"Alignment replaced no camera ({failures[0]}) — originals kept")
        else:
            self.statusBar().showMessage("Alignment: videos already aligned")
        if summary.get("index_error"):
            QMessageBox.warning(
                self, "Alignment could not index the videos",
                f"{summary['index_error']}\n\nThe videos have been left as "
                f"they are.")
        # stim_trace.csv is written during the finalize, i.e. BEFORE the align
        # pass rewrites each camera's blockids.npy / frametimes.npy and
        # replaces its mp4, so its frame column no longer matches any camera
        # that WAS replaced. Regenerate whenever one was — a block-rate
        # warning must not decide this, or the trace is silently offset in
        # exactly the sessions that had trouble while every file involved
        # still looks self-consistent.
        if summary.get("replaced") or replaced_cams:
            self._regenerate_stim_trace(self._finish_to_idle)
            return
        self._finish_to_idle()

    def _finish_to_idle(self):
        if self._acq_type == "calibration" and self._video_dir is not None:
            # Stamp the hint file with the videos it was measured against.
            # RULE: here, at the idle transition, not when the encode finishes.
            # REASON: alignment REPLACES every mp4 and changes its size, so a
            # stamp taken before it would never match the file the solve
            # opens and every calibration would fall back to a full scan.
            try:
                n = board_detector.stamp_codet_videos(self._video_dir)
                if n:
                    print(f"[hud] co-detection hints stamped against {n} "
                          f"videos", flush=True)
            except Exception as e:
                print(f"[hud] could not stamp the co-detection hints: {e}",
                      flush=True)
        self._sidebar.set_fields_editable(True)
        # IDLE first: the gate is computed from the state, and unchecking the
        # toggles emits into handlers that act only on RECORDING/CALIBRATING.
        self._state = State.IDLE
        self._reset_toggles()
        self._sidebar.set_status("IDLE", "#888")

    def _on_run_calibration(self):
        if self._state != State.IDLE:
            self.statusBar().showMessage("Solve unavailable while acquiring/encoding")
            return
        # A solve takes 4-5 minutes and never changes _state, so the guard above
        # does not cover a second click — and the only feedback is a status-bar
        # message, which makes a second click likely. That click would rebind
        # self._calib_worker below, dropping the ONLY Python reference to a
        # running QThread: sip deletes the C++ object underneath it and Qt calls
        # qFatal("QThread: Destroyed while thread is still running"), which
        # sys.excepthook cannot intercept. Instant process death, mid-solve.
        # (Two solves would also race on the same calibration.toml.)
        if self._calib_worker is not None and self._calib_worker.isRunning():
            self.statusBar().showMessage("A solve is already running")
            return
        try:
            config = self._build_config().validate()
        except ValueError as e:
            QMessageBox.warning(self, "Check the session details",
                                f"{e}\n\nFix the field and solve again.")
            return
        calib_dir = config.video_dir("calibration")

        if not calib_dir.exists():
            QMessageBox.warning(self, "No Data", f"Calibration directory not found:\n{calib_dir}")
            return

        mp4s = list(calib_dir.rglob("*.mp4"))
        if not mp4s:
            QMessageBox.warning(self, "No Data", f"No calibration videos found in:\n{calib_dir}")
            return

        board_cfg = self._profile.board_config
        if not board_cfg or not Path(board_cfg).exists():
            QMessageBox.warning(
                self, "Missing Board Config",
                f"Board config not found: {board_cfg or '(not set)'}\n\n"
                "Set board_config in your profile YAML to a valid file in configs/boards/.")
            return

        self._sidebar.set_status("CALIBRATING...", "#aa88ff")
        self._sidebar.set_toggles_enabled(False)
        # set_toggles_enabled touches only the two toggles, not the Solve button
        # (sidebar.set_toggles_enabled), so disable it explicitly here.
        self._sidebar.set_solve_enabled(False)
        self.statusBar().showMessage("Solving calibration...")

        # The solve runs for minutes and finishes against the fields as they
        # were when it started, not as they are then: a mouse id edited in the
        # meantime would copy the calibration into a different session.
        self._calib_config = config
        self._sidebar.set_fields_editable(False)

        self._calib_worker = CalibrationWorker(
            config.session_dir, CALIBRATION_SCRIPT, board_cfg)
        self._calib_worker.status.connect(lambda s: self.statusBar().showMessage(s))
        self._calib_worker.finished_solve.connect(self._on_calibration_done)
        self._calib_worker.start()

    def _on_calibration_done(self, success: bool, msg: str):
        # finished_solve is the last thing run() does, so the thread is
        # ending; joining it lets _toggles_permitted() see the solve as over.
        worker = self._calib_worker
        if worker is not None and worker.isRunning():
            worker.wait(5000)
        self._sidebar.set_toggles_enabled(self._toggles_permitted())
        self._sidebar.set_solve_enabled(True)
        self._sidebar.set_fields_editable(True)
        self._sidebar.set_status("IDLE", "#888")
        if success:
            config = self._calib_config or self._build_config()
            src = config.video_dir("calibration") / "calibration.toml"
            dst = config.video_dir("recording") / "calibration.toml"
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                # Copying unconditionally rewrites the calibration attached to a
                # recording that may already have been shot with a different
                # one, which changes that session's provenance after the fact.
                replace = True
                if dst.exists() and dst.read_bytes() != src.read_bytes():
                    replace = QMessageBox.question(
                        self, "Replace the recording's calibration?",
                        f"{dst}\n\nalready holds a DIFFERENT calibration. If a "
                        f"recording in that folder was made with it, replacing "
                        f"it changes which calibration that data claims to have "
                        f"been shot with.\n\nReplace it?",
                        QMessageBox.Yes | QMessageBox.No,
                        QMessageBox.No) == QMessageBox.Yes
                if replace:
                    shutil.copy2(src, dst)
                    status = f"Calibration solved — copied to {dst}"
                else:
                    status = (f"Calibration solved — kept in "
                              f"{src.parent.name}/, recording's copy left "
                              f"unchanged")
            else:
                status = "Calibration solved (no toml found to copy)"
            # A partial solve exits 0 by design, so the count is the only
            # thing that says nine cameras went in and seven came out.
            report = getattr(self._calib_worker, "report", None) or {}
            cams = report.get("cameras") or []
            dropped = sum(len(v) for v in (report.get("dropped") or {}).values())
            if cams:
                status = status.replace(
                    "Calibration solved",
                    f"Solved {len(cams)} of {len(cams) + dropped} cameras", 1)
            if msg:
                QMessageBox.warning(self, "Calibration Warnings", msg[:800])
                status += " (with warnings)"
            self.statusBar().showMessage(status)
        else:
            QMessageBox.warning(self, "Calibration Failed", msg[:800])
            self.statusBar().showMessage("Calibration failed")

    def _on_snapshot(self):
        if self._camera_mgr.num_cameras == 0:
            self.statusBar().showMessage("Snapshot: no cameras open")
            return
        self._camera_mgr.request_snapshots()
        # Give the grab threads a moment to stash the next full-res frame.
        QTimer.singleShot(250, self._save_snapshots)

    def _save_snapshots(self):
        """Write the stashed full-resolution frames, off the UI thread.

        PNG-compressing one full-resolution frame per camera is seconds of
        work, and it used to run in a timer slot. A camera whose frame did not
        arrive is named rather than silently counted out: with the triggers
        stopped every frame is missing, and "saved 0/9" does not say why.
        """
        try:
            cfg = self._build_config().validate()
        except ValueError as e:
            self.statusBar().showMessage(f"Snapshot: {e}")
            return
        if self._worker_busy(self._snap_op):
            self.statusBar().showMessage(
                "Snapshot: the previous save is still running")
            return
        out_dir = (cfg.session_dir / "snapshots"
                   / f"{cfg.date}_{datetime.now().strftime('%H%M%S')}")
        frames = list(self._camera_mgr.snapshots)
        names = [self._camera_label(i + 1) for i in range(len(frames))]
        self._snap_op = CallableWorker(
            lambda: self._write_snapshots(out_dir, frames, names))
        self._snap_op.done.connect(self._on_snapshots_saved)
        self._snap_op.start()
        self.statusBar().showMessage(f"Snapshot: saving {len(frames)} cameras…")

    @staticmethod
    def _write_snapshots(out_dir: Path, frames: list, names: list) -> dict:
        """Save one PNG per camera that produced a frame. Worker thread."""
        from PIL import Image
        out_dir.mkdir(parents=True, exist_ok=True)
        saved, missing = 0, []
        for frame, cam in zip(frames, names):
            if frame is None:
                missing.append(cam)
                continue
            try:
                Image.fromarray(frame).save(out_dir / f"{cam}.png")
                saved += 1
            except Exception as e:
                print(f"[snapshot] {cam} failed: {e}", flush=True)
                missing.append(cam)
        return {"saved": saved, "total": len(frames), "missing": missing,
                "out_dir": out_dir}

    def _on_snapshots_saved(self, result):
        if not isinstance(result, dict):
            self.statusBar().showMessage(f"Snapshot failed: {result}")
            return
        tail = (f"  —  no frame from: {', '.join(result['missing'])}"
                if result["missing"] else "")
        self.statusBar().showMessage(
            f"Snapshot: saved {result['saved']}/{result['total']} cameras → "
            f"{result['out_dir']}{tail}")

    def _on_stimulation(self):
        if self._stim_window is None:
            self._stim_window = StimulationWindow(
                get_port=lambda: self._profile.serial_port,
                get_output_dir=lambda: self._sidebar.output_dir,
                get_fps=lambda: self._profile.frame_rate,
                # Must cover a firmware flash too, not just an acquisition. The
                # launch-time clean flash and the per-acquisition sketch swap
                # both run with _state IDLE and _busy True, and both hand the
                # serial port to arduino-cli. Without _busy here the editor's
                # Apply and Test stay enabled and can launch a second
                # arduino-cli onto a port the first one already holds.
                is_busy=lambda: (
                    self._state in (State.RECORDING, State.CALIBRATING)
                    or self._busy
                    or (self._fw_op is not None and self._fw_op.isRunning())),
                # Narrower than is_busy: only an acquisition or a flash takes
                # the board from a Test. A Test ending during camera work (a
                # profile switch, a trace rebuild) still sends its stop,
                # because that write is the only thing that ends a looping
                # chain.
                board_taken=self._board_taken_from_test,
                get_safe_pins=lambda: self._profile.stim_safe_pins,
                get_trigger_pins=lambda: self._profile.trigger_pins,
                # The editor reclaims on the UI thread after every Apply and at
                # Test, so one attempt: each failed open waits a second, and the
                # start worker keeps its own retry count.
                get_serial=lambda: self._teensy_connection(
                    retries=self.UI_SERIAL_RETRIES),
                release_serial=self.release_serial_port,
                on_applied=self._on_stim_applied,
                on_upload_failed=self._on_stim_upload_failed,
                parent=self,
            )
            # The editor's Apply runs with this window at IDLE and not busy,
            # so without this the acquisition toggles stay live over a ~30 s
            # flash. The start path refuses anyway; greying them says so
            # before the operator presses anything.
            self._stim_window.uploading_changed.connect(
                self._on_stim_upload_state)
        self._stim_window.show()
        self._stim_window.raise_()

    def _board_taken_from_test(self) -> bool:
        """True while an acquisition or a firmware flash holds the board.

        The editor's Test skips its stop only on this: an acquisition's own
        start replaced the test's configuration and a stop would cut its
        camera triggers, and a flash has released the port. Anything else the
        window is busy with leaves the board to the test.
        """
        return (self._state in (State.RECORDING, State.CALIBRATING)
                or self._worker_busy(self._fw_op))

    def _on_stim_upload_state(self, uploading: bool):
        """Grey the acquisition toggles for the duration of an editor flash.

        RULE: the gate is recomputed from every owner, never written from this
        one's state. REASON: the gate is shared, so answering "the upload
        finished" with set_toggles_enabled(True) also reopens the toggles a
        solve, an encode, an alignment or a missing profile had closed - and
        during a solve the start path's own guards pass, because the state
        machine reads IDLE for the whole four to five minutes.
        """
        self._sidebar.set_toggles_enabled(
            self._toggles_permitted() and not uploading)

    def _workers_running(self) -> bool:
        # _cam_op and _coverage_worker are usually masked by self._busy, but the
        # stim editor's upload is NOT: an Apply runs with _state IDLE and _busy
        # False, so quitting during a ~30 s arduino-cli flash would destroy a
        # running QThread and can kill avrdude mid-write — leaving a Mega with
        # no allStimLow() boot guard, i.e. a laser pin floating on next power-up.
        # _fw_op is the per-acquisition sketch swap and the launch-time clean
        # flash. Same hazard as the editor's Apply above and for the same
        # reason: it is arduino-cli driving avrdude, so it must never be torn
        # down silently.
        # The launch hardware check writes a speed-test file and runs ffmpeg,
        # typically 1-3 s. Closing inside that window used to return from
        # closeEvent with the QThread still running, which Qt answers with
        # "QThread: Destroyed while thread is still running" and an abort.
        if self._stim_window is not None and self._stim_window.is_uploading():
            return True
        return any(w is not None and w.isRunning() for w in
                   (self._encode_worker, self._align_worker, self._calib_worker,
                    self._cam_op, self._cap_op, self._coverage_worker,
                    self._fw_op, self._hw_check_thread, self._snap_op,
                    *self._retired_workers))

    def _delete_on_quit(self) -> bool:
        """Whether quitting right now should remove the session directory.

        RULE: only a capture still in flight whose data has not been written.
        REASON: in ENCODING and ALIGNING the capture is OVER — blockids.npy,
        frametimes.npy and a flushed stream.h264 are on disk and the remaining
        work is a remux that can be re-run in seconds — so deleting there
        destroys a good session the dialog calls incomplete; and a finalize
        that completed inside the quit's wait has saved everything while the
        state still reads RECORDING.
        """
        if self._state not in (State.RECORDING, State.CALIBRATING):
            return False
        return not self._finalized

    def closeEvent(self, event):
        # A firmware flash is never interrupted and never waited for on the UI
        # thread. avrdude stopped mid-write leaves the Mega with a
        # half-programmed flash and no allStimLow() boot guard, so the laser
        # pin floats at the next power-up; and blocking the window on the
        # flash instead shows "not responding" for up to a minute, which is
        # what tempts an operator to end the task from Task Manager and does
        # the same damage.
        if ((self._fw_op is not None and self._fw_op.isRunning())
                or (self._stim_window is not None
                    and self._stim_window.is_uploading())):
            QMessageBox.information(
                self, "Firmware upload in progress",
                "The trigger board is being flashed (~30 s).\n\nPanopticon "
                "will not close until it finishes: interrupting the upload "
                "leaves the board with no laser-safety boot guard.\n\nClose "
                "again once the upload reports that it is done.")
            event.ignore()
            return

        # Stop the temperature poll before any dialog on this path. RULE: the
        # thermal timer stops wherever the cameras are about to be abandoned.
        # REASON: its slot is a GVCP register read per camera, it only
        # self-stops on a state change, and the quit path never moves the
        # state to IDLE - so from RECORDING or CALIBRATING it keeps firing,
        # including inside the nested event loops of the two modal dialogs
        # below, against cameras _abandon_and_cleanup is closing. Two threads
        # in pylon on one device is an access violation, not an exception.
        self._thermal_timer.stop()

        # Quitting mid-session can't be finalized — confirm, then ABANDON the
        # half-baked data rather than blocking the close on encode/align/solve
        # workers (which is what made it freeze on "quit anyway").
        busy = self._state != State.IDLE or self._busy or self._workers_running()
        if busy:
            if self._delete_on_quit():
                text = (f"State is {self._state.value}. Quit anyway?\n\n"
                        "This capture is still running, so it cannot be "
                        "finished — its incomplete data will be DELETED.")
            elif self._state in (State.ENCODING, State.ALIGNING):
                text = (f"State is {self._state.value}. Quit anyway?\n\n"
                        "The capture is COMPLETE and will be KEPT. Only the "
                        "mp4 wrapping is unfinished, and it can be re-run "
                        "later from the same directory.")
            else:
                text = ("Work is still in progress — a solve, a profile switch "
                        "or a camera operation.\n\nQuit anyway? It will be "
                        "cancelled. No data is deleted.")
            reply = QMessageBox.question(
                self, "Work in progress", text,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                # The session goes on, so the temperature watch has to as
                # well: stopped above and not restarted, an overheating
                # camera would go unreported for the rest of a recording
                # because the operator thought about quitting and did not.
                if self._state in (State.RECORDING, State.CALIBRATING):
                    self._start_thermal_watch()
                event.ignore()
                return

        # From here on no start may reach the board: a start worker checks
        # this immediately before it claims the port and before it sends the
        # start, and a start result delivered after this point is dropped.
        self._quitting = True
        self._display_timer.stop()
        self._thermal_timer.stop()
        # No bound at close: the loop exits after at most one detection pass,
        # and there is no later moment at which a straggler could be joined.
        self._stop_coverage_hud(timeout_ms=0)
        self._stand_down_board_for_quit()

        if busy:
            self._abandon_and_cleanup()
        else:
            self._join_retired_workers()
            self._camera_mgr.close_all()
        event.accept()

    def _stand_down_board_for_quit(self):
        """Stop the board and close its link, as the last word to it.

        Always, not just mid-acquisition: the stop drives the stim pins LOW as
        well as the camera pins, so quitting can never leave a paradigm (or a
        laser) running.

        RULE: stop_and_close(), one step on the controller, after _quitting
        is set. REASON: a start worker may be waiting for its ack on the same
        controller. The controller runs this stop after that attempt and the
        attempt does not retry after it, and no start can be written between
        the stop and the close; _quitting keeps a worker that has not reached
        the board yet from sending one at all. A stop followed by a separate
        close leaves a gap in which a queued start goes out after the stop.

        The warning comes before the window goes, while there is something to
        show it on. ``is_open`` proves nothing: pyserial keeps it True after
        the USB device disappears, so an unplugged cable looks healthy right
        up until the write.
        """
        try:
            if self._teensy is None:
                return
            if self._teensy.is_open:
                self._warn_if_not_stood_down(
                    self._teensy.stop_and_close(self._profile.trigger_pins))
            else:
                self._teensy.close()
        except Exception as e:
            print(f"[quit] standing the board down failed: {e}", flush=True)

    def _abandon_and_cleanup(self):
        """Kill in-flight ffmpeg/solve subprocesses, tear down capture without
        draining, and delete the session's data only when it is genuinely
        incomplete — so 'quit anyway' returns immediately instead of waiting
        on workers."""
        # Ask the encode and align workers to stop BEFORE killing anything.
        # Without a stop flag a worker whose ffmpeg was killed records the
        # failure and launches the next job, so fresh children appear after
        # the kill, hold the output files open and outlast the wait.
        for w in (self._encode_worker, self._align_worker):
            if w is not None and w.isRunning():
                try:
                    w.request_stop()
                except Exception as e:
                    print(f"[quit] could not ask a worker to stop: {e}",
                          flush=True)
        # Kill child processes (ffmpeg remux/encode, the uv-run solve): unblocks
        # the workers and unlocks output files so they can be removed.
        #
        # NEVER kill the firmware toolchain. avrdude interrupted mid-write
        # leaves the Mega with a half-programmed flash, which means no
        # allStimLow() boot guard — so the laser pin floats on the next
        # power-up, which a powered driver reads as ON. A stranded arduino-cli
        # is a far smaller problem than that: it finishes on its own in ~30 s,
        # and _workers_running() already refuses to reach this path silently
        # while an upload is in flight.
        _FIRMWARE_PROCS = ("avrdude", "arduino-cli")
        try:
            import psutil
            for child in psutil.Process().children(recursive=True):
                try:
                    name = (child.name() or "").lower()
                except Exception:
                    name = ""
                if any(p in name for p in _FIRMWARE_PROCS):
                    print(f"[quit] leaving {name} alone: killing it mid-write "
                          f"would strip the board's laser-safety boot guard",
                          flush=True)
                    continue
                try:
                    child.kill()
                except Exception:
                    pass
        except Exception:
            pass
        # Wait for the workers BEFORE tearing the cameras down. abandon() calls
        # StopGrabbing()/Close() on every InstantCamera from the Qt main thread,
        # while _cam_op is the thread running _finalize — possibly inside
        # _router.stop() or resume_preview(). Two threads making native pylon
        # calls on the same device is an access violation, not an exception, so
        # no excepthook can intercept it. Killing the child processes above is what
        # lets these waits actually return.
        for w in (self._cam_op, self._cap_op, self._encode_worker,
                  self._align_worker, self._calib_worker,
                  self._coverage_worker, self._hw_check_thread,
                  self._snap_op):
            if w is not None and w.isRunning():
                w.wait(3000)
        # _stop_coverage_hud parks a coverage worker that missed its join
        # here and clears _coverage_worker, so the tuple above cannot see it.
        self._join_retired_workers()
        # A flash is not killed above and must not be abandoned under a live
        # avrdude, so closeEvent refuses to close while one is running. This
        # is the backstop for a flash that started between that check and
        # here: a short wait, not the minute-long UI freeze it replaces.
        if self._fw_op is not None and self._fw_op.isRunning():
            print("[quit] a firmware flash is still running; waiting briefly "
                  "rather than tearing it down", flush=True)
            self._fw_op.wait(5000)
        if self._cam_op is not None and self._cam_op.isRunning():
            # Still inside pylon after 3 s. Leaking the camera handles costs
            # nothing at process exit; closing them under a live native call
            # crashes. Skip the teardown entirely.
            print("[quit] _cam_op still running — leaking camera handles rather "
                  "than closing under a live pylon call", flush=True)
        else:
            try:
                self._camera_mgr.abandon()
            except Exception as e:
                print(f"[quit] abandon failed: {e}", flush=True)
        # Read the flag AFTER the waits: a finalize that completed inside them
        # has written the whole session, and deleting it then would destroy
        # exactly the data the wait was there to save.
        if not (self._delete_on_quit() and self._video_dir):
            return
        target = Path(self._video_dir)
        if not target.exists():
            return
        try:
            base = Path(self._sidebar.output_dir).resolve()
            inside = target.resolve().is_relative_to(base)
        except (OSError, ValueError):
            inside = False
        if not inside:
            # The session path is built from free-text fields. A component
            # that escaped validation must never turn this into an rmtree of
            # somewhere else.
            print(f"[quit] refusing to delete {target}: outside the output "
                  f"directory", flush=True)
            return

        def _report(func, path, exc_info):
            print(f"[quit] could not remove {path}: {exc_info[1]}", flush=True)

        shutil.rmtree(target, onerror=_report)
        if target.exists():
            # ignore_errors used to leave a half-deleted directory behind a
            # log line claiming success; a file a dying ffmpeg still holds is
            # the normal way that happens.
            print(f"[quit] session data only PARTLY removed; files remain in "
                  f"{target}", flush=True)
        else:
            print(f"[quit] deleted incomplete session data: {target}",
                  flush=True)
