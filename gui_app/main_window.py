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
from PyQt5.QtWidgets import QDialog, QLabel, QPushButton, QVBoxLayout
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QPalette, QColor, QIcon, QCursor

from gui_app.camera_manager import (AcquisitionStartRefused,
                                    AcquisitionStopIncomplete, CameraManager,
                                    CameraOpenError)
from gui_app.grab_thread import SOURCE_SILENT_S, ring_slots
from gui_app.serial_controller import TeensyController
from gui_app.encode_worker import EncodeWorker
from gui_app.align_worker import AlignWorker
from gui_app.ui_workers import CallableWorker
from gui_app import alignment
from gui_app import recording_meta
from gui_app import stim_trace
from gui_app.calibration_worker import CalibrationWorker
from gui_app.hardware_check import (HardwareCheckThread, check_capacity,
                                    format_report, installed_encoder,
                                    invalidate_nvenc_cache, select_encoder)
from gui_app.coverage_worker import CoverageWorker
from gui_app import rig_setup
from gui_app import settings
from gui_app import stim_compiler
from gui_app.session_config import SessionConfig, RigProfile
from gui_app.trigger_source import (FIRST_TRIGGER_TIMEOUT_S, STOP_WAIT_S,
                                    ExternalTriggerSource,
                                    make_trigger_source)
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
STIM_FILES = ("stim_paradigm.json", "stim_paradigm.ino", "stim_trace.csv")
STALE_SESSION_FILES = ("WARNINGS.txt", "codet_frames.json") + STIM_FILES


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


class _TriggerPrompt(QDialog):
    """The non-modal prompt of an external trigger source: one text and one
    button, which rejects the dialog. The window keeps it, rewrites its text
    while it counts down, and closes it itself when the cameras answer."""

    def __init__(self, parent, title: str, text: str, button: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        layout = QVBoxLayout(self)
        self._label = QLabel(text)
        self._label.setWordWrap(True)
        layout.addWidget(self._label)
        row = QHBoxLayout()
        row.addStretch(1)
        self.button = QPushButton(button)
        self.button.clicked.connect(self.reject)
        row.addWidget(self.button)
        layout.addLayout(row)

    def setText(self, text: str) -> None:
        self._label.setText(text)

    def text(self) -> str:
        return self._label.text()


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
    #: How a lag verdict that finds nothing wrong begins.
    _HEALTHY = "Capture healthy"
    #: Seconds the start waits for every grab thread to arm (fill its frame
    #: ring and start its stream) before refusing. Arming normally takes a
    #: few seconds; the bound only has to cover a host paging under memory
    #: pressure without letting a wedged camera hold the start for minutes.
    READY_TIMEOUT_S = 30.0
    #: Set by closeEvent once the operator has agreed to quit, and never
    #: cleared. The start path reads it so that nothing reaches the trigger
    #: board after the quit has stood it down.
    _quitting = False
    #: What this recording's stimulation files describe, taken at arm time
    #: (_snapshot_stim); None when the recording carries no paradigm.
    _stim_snapshot: dict | None = None
    #: Files from an earlier take that this acquisition's start could not
    #: remove, one warning each (_sweep_stale_diagnostics).
    _sweep_warnings: tuple | list = ()
    #: True from the moment a hardware check starts until its report arrives.
    #: Record and Calibrate stay disabled meanwhile (_toggles_permitted).
    _hw_check_pending = False
    #: What the finalize itself found (raw.bin against its block IDs, the
    #: block-ID rate check outside kick mode), for this recording's report.
    _finalize_warnings: tuple | list = ()
    #: Rate-check texts this recording has already reported, so the
    #: alignment summary's copy of the same finding is not shown twice.
    _reported_rate_warnings: frozenset | set = frozenset()
    #: Why the post-hoc alignment left each camera out, shown with its result.
    _align_notes: tuple | list = ()
    #: The encoder and NVENC upload this acquisition was started with, for its
    #: session_metadata.json (_arm_encoder_record).
    _session_encoder = ""
    _session_upload: dict | None = None
    #: The all-cameras-silent alarm has been raised for the current silence.
    _source_alarm_raised = False
    #: A camera that reached its shutdown temperature, one warning each. They
    #: always reach WARNINGS.txt and the post-session dialog.
    _thermal_shutdown_warnings: tuple | list = ()
    #: Cameras whose thermal-watch fallback has been logged this acquisition.
    _thermal_logged: frozenset | set = frozenset()
    #: Where an acquisition on an external trigger source is in the
    #: operator's part of the protocol: None outside one, "awaiting" from the
    #: prompt to the first trigger, "running" while triggers arrive, and
    #: "stopping" from Stop until every camera is silent.
    _ext_phase: str | None = None
    #: time.monotonic() at which the current phase stops waiting.
    _ext_deadline = 0.0
    #: The timer that watches the cameras during those phases, and the
    #: prompt on screen, if any.
    _ext_timer = None
    _ext_prompt = None
    #: Milliseconds between two looks at the cameras in those phases.
    EXTERNAL_POLL_MS = 100
    #: (holder, target) while an external-source start holds the session the
    #: operator agreed to overwrite: moved out of the way into the hidden
    #: directory ``holder``, not yet deleted. None outside that window
    #: (_set_overwrite_aside).
    _overwrite_aside: tuple | None = None
    #: The last error the trigger-source watch printed, so a manager read
    #: that fails on every tick is logged once, not ten times a second.
    _ext_watch_error = ""
    #: The trigger pins of the profile a switch left, for the stop a switch
    #: to an external trigger source sends that profile's board.
    _switch_from_pins: list | None = None

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
        # preview timer: thermals() is a register read per camera, while
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
        # here. Fall back to the first profile that can open its cameras
        # (RigProfile.settings_ready: a Basler profile's .pfs exists, a FLIR
        # profile has its camera: block).
        self._profile = self._sidebar.current_profile
        if self._sidebar.select_profile(self._sidebar.remembered_profile()):
            self._profile = self._sidebar.current_profile
        else:
            for prof in self._sidebar.profiles:
                if prof.settings_ready() is None:
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
        # What a camera needs besides the profile depends on the backend (a
        # Basler .pfs, a FLIR camera: block), so the profile says whether it
        # has it.
        why = (self._profile.settings_ready()
               or self._capture_processes_refusal())
        if why:
            return CameraOpenError(why)
        rig_setup.apply_profile_to_manager(self._camera_mgr, self._profile)
        return self._camera_mgr.open_all(
            **rig_setup.open_kwargs(self._camera_mgr, self._profile))

    def _capture_processes_refusal(self) -> str | None:
        """Why this window opens no camera for the profile's
        capture_processes, or None.

        RULE: a profile with capture_processes above 0 opens no camera here.
        REASON: this window captures every camera in its own process
        (CameraManager), and only probe_mp.py builds the multi-process manager
        (gui_app.mp.manager.ProcessCameraManager). Opening anyway records in
        one process a session whose profile asks for several, and a
        comparison of the two capture paths then counts that session on the
        wrong side.
        session_metadata.json records both the request and what ran
        (capture_processes, capture_processes_used).
        """
        n = int(getattr(self._profile, "capture_processes", 0) or 0)
        if n <= 0:
            return None
        return (f"The profile sets capture_processes: {n}. This window "
                f"captures every camera in its own process. Only probe_mp.py "
                f"captures in several processes so far.\n\nSet "
                f"capture_processes: 0 in the profile YAML to record from "
                f"this window.")

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
                and not self._hw_check_pending
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

    def _run_hardware_check(self, survey: bool = True):
        """Check the host against the profile, off the UI thread.

        At launch (`survey`) the whole host is surveyed; after a profile
        switch only the encoder half runs again (HardwareCheckThread). The
        profile and the camera count are what make the libx264 bench, the
        NVENC session probe, the NVENC upload setting and the encoder
        selection run HERE. Without them the profile's `encoder` field
        selects nothing at all and the session probe lands on the UI thread
        at the first Record, freezing the window with no busy indicator.

        RULE: Record and Calibrate stay disabled until the report arrives.
        REASON: the check installs the encoder factory and the NVENC upload,
        and its session probe allocates every session the driver grants. A
        start during it would record on whatever was installed before, and
        the capacity preflight's own probe would run beside this one, each
        child seeing only part of the session cap.
        """
        output_dir = self._profile.output_dir if self._profile else ""
        n_cams = self._camera_mgr.num_cameras or (
            self._profile.n_cameras if self._profile else 0)
        self._hw_check_pending = True
        self._sidebar.set_toggles_enabled(False)
        self.statusBar().showMessage(
            "Checking hardware: Record and Calibrate are available once it "
            "reports")
        thread = HardwareCheckThread(output_dir, profile=self._profile,
                                     n_cams=n_cams, survey=survey)
        self._hw_check_thread = thread
        thread.report_ready.connect(self._on_hardware_check_done)
        # QThread's own finished, after report_ready: the backstop for a
        # thread that ended without a report.
        thread.finished.connect(self._on_hardware_check_thread_finished)
        thread.start()

    def _on_hardware_check_thread_finished(self):
        if self._hw_check_pending:
            print("[hw] the hardware check ended without a report; Record and "
                  "Calibrate are enabled without its findings", flush=True)
            self._hw_check_pending = False
            self._sidebar.set_toggles_enabled(self._toggles_permitted())

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
        self._hw_check_pending = False
        self._sidebar.set_toggles_enabled(self._toggles_permitted())
        msg = format_report(report)
        print(msg, flush=True)
        self.statusBar().showMessage(
            "Hardware check done" + (f": encoding with {report.encoder}"
                                     if report.encoder else ""))
        if report.warnings:
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
        if self._worker_busy(self._hw_check_thread):
            # The switch runs the encoder check again for the new profile, and
            # a second check cannot start over a running one: assigning over
            # the running thread drops its last reference (a qFatal), and the
            # two would install encoders for different profiles.
            self._refuse_profile_switch()
            QMessageBox.information(
                self, "The hardware check is running",
                "Panopticon is still checking the hardware for the current "
                "profile. Switch profiles once it reports (the status bar "
                "says when).")
            return
        # close_all + open the cameras (+ settings load) is seconds of camera
        # round-trips; run it off the UI thread so the window doesn't go "not
        # responding".
        self._begin_busy("Switching cameras…")
        self._switch_from_port = self._profile.serial_port
        # The old board's own pins, for the stop a switch to another port or
        # to an external trigger source sends it (_stand_down_switched_board).
        self._switch_from_pins = list(self._profile.trigger_pins)
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
        # RULE: the encoder check runs again for the new profile. REASON: the
        # encoder factory, the NVENC upload and the libx264 bench are process
        # state set by the last check, so without it the new profile records
        # on the old profile's encoder and upload path, against a bench
        # measured at the old frame size.
        self._run_hardware_check(survey=False)
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
        activate stim". The old board is stood down first
        (_stand_down_switched_board), and its hint and identity describe the
        other board and are forgotten, so the flash runs.

        A profile on an external trigger source uses no board. The previous
        profile's board is stood down the same way, so a later switch back
        runs the launch check on it, and the Stimulation editor is closed,
        because nothing can run a paradigm now.
        """
        if self._external_trigger():
            if self._teensy is not None:
                self._stand_down_switched_board(
                    "the profile takes its triggers from an external source")
            close = getattr(self._stim_window, "close", None)
            if close is not None:
                close()
            return
        new_port = self._profile.serial_port
        if new_port == old_port:
            return
        print(f"[acq] the profile moved the trigger board from "
              f"{old_port or '(none)'} to {new_port or '(none)'}; running the "
              f"launch firmware check for it", flush=True)
        if self._teensy is not None and self._teensy.port != new_port:
            self._stand_down_switched_board(
                f"the profile moved the trigger board to {new_port or '(none)'}")
        settings.set_board_sketch_hint("")
        self._board_id_stale = True
        self._board_identity_reflashed = False
        self._ensure_clean_firmware()

    def _stand_down_switched_board(self, why: str) -> None:
        """Stand the previous profile's board down and forget it.

        The board is sent a stop on the previous profile's own trigger pins
        and its link is closed (stop_and_close), then the board-sketch hint
        and its identity are forgotten.

        RULE: the stop goes out before the link closes, and a stop the board
        does not confirm is shown. REASON: after a switch nothing talks to
        that board again, not even the quit's stand-down, which reaches only
        the board the current profile names, so this is the last chance to
        drive its camera and stimulation pins low. The switch runs only at
        IDLE, so the board is normally stood down already; the stop matters
        when the last one was not confirmed.
        """
        teensy, self._teensy = self._teensy, None
        pins = list(self._switch_from_pins or self._profile.trigger_pins)
        print(f"[acq] {why}; stopping the trigger board on {teensy.port} and "
              f"closing its link", flush=True)
        try:
            stood_down = teensy.stop_and_close(pins)
        except Exception as e:
            print(f"[acq] standing the board down failed: {e}", flush=True)
            stood_down = False
        settings.set_board_sketch_hint("")
        self._board_id_stale = True
        self._warn_if_not_stood_down(stood_down)

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

        RULE: silence is checked before lag. REASON: the lag figures and the
        frame rates are computed as frames arrive, so with no frame arriving
        they hold their last values and read as healthy; a camera that has
        stopped delivering is the worse news.
        """
        if self._state not in (State.RECORDING, State.CALIBRATING):
            return
        silence = self._silence_health_text()
        if self._state != State.RECORDING and silence is None:
            return
        lag = (self._frontier_health_text() or self._delivery_health_text()
               if self._state == State.RECORDING else None)
        if silence and lag and lag.startswith(self._HEALTHY):
            # A camera delivering nothing is not healthy, whatever the lag
            # figures it left behind say.
            lag = None
        msg = "  |  ".join(m for m in (silence, lag) if m) or None
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
            # RULE: never fall back to the delivery text here. REASON: it is
            # computed from the last frame each camera delivered, so with every
            # camera retired it reads "healthy" for the rest of the session.
            if lags:
                return ("EVERY CAMERA IS RETIRED: nothing is being recorded. "
                        "Stop the recording.")
            return None
        retired = [self._camera_label(i + 1) for i, n in enumerate(lags) if n < 0]
        tail = f"  |  RETIRED: {', '.join(retired)}" if retired else ""
        worst, idx = max(live)
        cap = max(1, int(self._session_rig().kick_max_lag))
        name = self._camera_label(idx + 1)
        if worst < cap * 0.25:
            return (f"{self._HEALTHY} — every camera within {worst} "
                    f"trigger(s) of the leader{tail}")
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
            return (f"{self._HEALTHY} — keeping up with the trigger "
                    f"(max lag {worst * 1000:.0f} ms)")
        if worst < 1.0:
            return (f"CAPTURE FALLING BEHIND: {name} is {worst:.2f} s behind "
                    f"real time and growing. Close other applications.")
        return (f"CAPTURE {worst:.1f} s BEHIND REAL TIME ({name}). Frames will "
                f"be lost when the buffer pool fills. Stop and investigate.")

    def _silence_health_text(self):
        """Which cameras have delivered nothing for a while, or None.

        Every active camera silent at once is the trigger source, not the
        cameras (CameraManager.source_silent), so it raises the alarm as
        well. A camera the kick-out router retired is left out: it is silent
        for good, and the lag text names it.
        """
        try:
            secs = list(self._camera_mgr.seconds_since_frame())
            all_silent = bool(self._camera_mgr.source_silent())
            lags = list(self._camera_mgr.frontier_lags or [])
        except Exception:
            return None
        if all_silent and self._external_trigger():
            # Silence on every camera is how an external source's recording
            # begins and ends, so it raises no alarm; the watch that ends the
            # recording reads it (_poll_external_source).
            return self._external_silence_text(max(secs) if secs else 0.0)
        if all_silent:
            worst = max(secs) if secs else 0.0
            self._raise_source_alarm(worst)
            return (f"NO FRAMES FROM ANY CAMERA for {worst:.0f} s: the trigger "
                    f"board may have stopped. Check the board, its USB cable"
                    + (" and the laser" if self._profile.stim_safe_pins
                       else "") + ".")
        # The silence is over (or never started), so the next one alarms.
        self._source_alarm_raised = False
        silent = [(s, i) for i, s in enumerate(secs)
                  if s > SOURCE_SILENT_S and not (i < len(lags) and lags[i] < 0)]
        if not silent:
            return None
        silent.sort(reverse=True)
        names = ", ".join(self._camera_label(i + 1) for _s, i in silent)
        return f"NO FRAMES from {names} for {silent[0][0]:.0f} s"

    def _external_silence_text(self, silent_s: float) -> str:
        """The status line while every camera is silent on an external
        trigger source: before the first trigger, while the operator is
        asked to stop the source, or once it has stopped."""
        left = max(0.0, self._ext_deadline - time.monotonic())
        if self._ext_phase == "awaiting":
            return (f"Waiting for the first trigger: start your trigger "
                    f"source now ({left:.0f} s left)")
        if self._ext_phase == "stopping":
            return "Waiting for your trigger source to stop"
        return (f"NO FRAMES FROM ANY CAMERA for {silent_s:.0f} s: the trigger "
                f"source has stopped, so the {self._acq_label()} is "
                f"finishing.")

    def _raise_source_alarm(self, silent_s: float):
        """One modal per silence: every camera stopped receiving frames.

        RULE: the alarm names the trigger board and, on a profile with
        stimulation pins, the laser. REASON: cameras stop together when what
        they share stops, and on the board path that is the board (reset,
        unplugged, or without power). The capture path retires no camera
        while all of them are silent, and re-arms only once the silence has
        lasted grab_thread.SOURCE_DOWN_WAIT_WINDOWS stall windows, so nothing
        else tells the operator. A board without power leaves its stimulation
        pins undriven, which a laser driver can read as on.
        """
        if self._source_alarm_raised:
            return
        self._source_alarm_raised = True
        teensy = self._teensy
        try:
            alive = teensy is not None and bool(teensy.port_alive())
        except Exception:
            alive = False
        port = self._profile.serial_port or "(none)"
        if alive:
            link = (f"The trigger board's serial link on {port} still answers, "
                    f"so the board may have reset or stopped triggering, or "
                    f"the network to every camera is down.")
        else:
            link = (f"The trigger board's serial link on {port} is gone: the "
                    f"board was unplugged, reset, or lost power.")
        pins = list(self._profile.stim_safe_pins or [])
        laser = (f"\n\nA board without power leaves its stimulation pins "
                 f"{', '.join(str(p) for p in pins)} undriven, and a laser "
                 f"driver can read an undriven input as on. Check the laser "
                 f"now." if pins else "")
        text = (f"No camera has received a frame for {silent_s:.0f} s.\n\n"
                f"{link} No camera is retired while all of them are silent. "
                f"If the silence lasts, each camera re-arms its stream, which "
                f"clears a stall of the network the cameras share. Every "
                f"trigger in the silence is missing from every "
                f"camera.{laser}\n\nStop the recording, then check the "
                f"trigger board and its USB cable.")
        print(f"[acq] ALARM: every camera silent for {silent_s:.1f} s; board "
              f"link {'answers' if alive else 'is gone'}", flush=True)
        QMessageBox.critical(self, "No frames from any camera", text)

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

        RULE: a camera that reports its shutdown temperature alerts at
        shutdown minus the profile's `thermal_warn_margin_c`, and always in
        its own error (over-temperature) state; a 'Critical' status alone
        does not alert. REASON: the camera's Critical level is a fixed
        firmware value that an installation can sit above for hours without
        losing a frame, so an alert on it fires on every session and is
        ignored by the time it matters, while the shutdown point is where the
        camera stops delivering. The margin is how much warning the
        installation needs, so it lives in the profile. temp_max_c still
        records how hot each camera got (session_metadata.json).

        A camera that reports no shutdown temperature is judged by its own
        status instead, Critical included, and one that reports neither
        cannot be judged; the log says which, once per camera.
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

        margin_c = float(getattr(self._profile, "thermal_warn_margin_c",
                                 0.0) or 0.0)
        hot = []
        for idx, t in enumerate(readings, start=1):
            if not isinstance(t, dict) or t.get("error"):
                continue
            temp = t.get("temp_c")
            status = str(t.get("temp_status", "") or "").strip()
            shutdown = t.get("temp_shutdown_c")
            # Ok and Critical are the two states below the camera's own
            # over-temperature state; anything else is that state.
            error_state = status.lower() not in ("", "ok", "critical")
            if shutdown is not None and temp is not None:
                over = error_state or temp >= shutdown - margin_c
                at_shutdown = error_state or temp >= shutdown
            else:
                self._log_thermal_fallback(idx, status)
                over = status.lower() not in ("", "ok")
                at_shutdown = error_state
            if not over:
                continue
            margin = (shutdown - temp) if (temp is not None
                                           and shutdown is not None) else None
            # Sort key first: closest to shutdown is the one to name.
            hot.append((margin if margin is not None else 999.0,
                        idx, temp, status, margin, at_shutdown))

        if not hot:
            self._thermal_alert = None
            return
        hot.sort()

        _key, idx, temp, status, margin, at_shutdown = hot[0]
        name = self._camera_label(idx)
        if margin is not None and margin <= 0:
            # Past the vendor's own shutdown point: a negative margin read as
            # though there were headroom left, which is the opposite of true.
            gap = ", AT OR PAST ITS SHUTDOWN POINT"
        elif at_shutdown:
            gap = ", IN ITS OVER-TEMPERATURE STATE"
        elif margin is not None:
            gap = f", {margin:.0f} C from shutdown"
        else:
            gap = ""
        extra = "" if len(hot) == 1 else f" (+{len(hot) - 1} more)"
        temp_s = "?" if temp is None else f"{temp:.0f}"
        word = status if status.lower() not in ("", "ok") else "near shutdown"
        self._thermal_alert = (
            f"CAMERA TEMPERATURE: {name} {temp_s} C {word}{gap}{extra}")

        # One durable warning per camera per session, so this reaches
        # WARNINGS.txt and the post-session dialog and not just a status bar
        # message that scrolls past unread. A camera that reaches shutdown
        # gets a second one, which is reported whether or not frames were
        # lost: from that point it stops delivering.
        for _key, idx, temp, status, margin, at_shutdown in hot:
            name = self._camera_label(idx)
            temp_s = "?" if temp is None else f"{temp:.1f}"
            shutdown = (temp + margin if temp is not None
                        and margin is not None else None)
            if idx not in self._thermal_reported:
                self._thermal_reported.add(idx)
                tail = ""
                if shutdown is not None:
                    if margin <= 0:
                        tail = (f", which is AT OR PAST its {shutdown:.0f} C "
                                f"shutdown point")
                    else:
                        tail = (f", {margin:.1f} C below its {shutdown:.0f} C "
                                f"shutdown point")
                self._thermal_warnings.append(
                    f"{name} reached {temp_s} C during this acquisition, which "
                    f"its own firmware reports as "
                    f"'{status or 'no status'}'{tail}. Check the airflow "
                    f"around the camera and its mounting. A camera that "
                    f"reaches its shutdown point stops delivering "
                    f"mid-session.")
                print(f"[acq] THERMAL: {self._thermal_warnings[-1]}",
                      flush=True)
            if at_shutdown and idx not in self._thermal_shutdown_reported():
                point = (f"its {shutdown:.0f} C shutdown point"
                         if shutdown is not None
                         else f"its over-temperature state ('{status}')")
                self._thermal_shutdown_warnings = (
                    list(self._thermal_shutdown_warnings)
                    + [(idx, f"{name} reached {point} during this "
                             f"acquisition ({temp_s} C). A camera at its "
                             f"shutdown point stops delivering frames, so its "
                             f"recording may end there. Let it cool before the "
                             f"next recording.")])
                print(f"[acq] THERMAL: {self._thermal_shutdown_warnings[-1][1]}",
                      flush=True)

    def _thermal_shutdown_reported(self) -> set:
        """Cameras that already have a shutdown warning this acquisition."""
        return {idx for idx, _text in self._thermal_shutdown_warnings}

    def _log_thermal_fallback(self, idx: int, status: str) -> None:
        """Say once per camera how the thermal watch judges a camera that
        reports no shutdown temperature."""
        if idx in self._thermal_logged:
            return
        self._thermal_logged = frozenset(set(self._thermal_logged) | {idx})
        name = self._camera_label(idx)
        if status:
            print(f"[acq] thermal watch: {name} reports no shutdown "
                  f"temperature, so it is judged by its own temperature "
                  f"status ('{status}' now); any status but Ok raises the "
                  f"alert", flush=True)
        else:
            print(f"[acq] thermal watch: {name} reports neither a shutdown "
                  f"temperature nor a temperature status, so the live watch "
                  f"cannot warn about it; its temperature is still recorded",
                  flush=True)

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
        n_cams = self._camera_mgr.num_cameras
        # RULE: the encoder is selected again for every start, against the
        # live profile and the cameras that are open. REASON: the launch
        # selection answers for the machine as it was then; an NVENC session
        # shortfall at launch installs libx264, and a start that trusted it
        # would record every camera on the CPU after the sessions came back,
        # while the check below saw enough sessions and said nothing. The
        # session count is cached, so this re-probes only when the cache is
        # short of what this start needs.
        choice_blocking = []
        try:
            if n_cams > 0:
                choice = select_encoder(p, n_cams, fps, p.frame_width,
                                        p.frame_height)
                print(f"[acq] encoder: {choice.encoder} ({choice.reason})",
                      flush=True)
                if choice.blocking:
                    choice_blocking.append(choice.blocking)
        except Exception as e:
            print(f"[acq] encoder selection failed to run: {e}", flush=True)
        try:
            blocking, warnings = check_capacity(
                n_cams=n_cams,
                width=p.frame_width, height=p.frame_height,
                ring_n=ring_n, max_num_buffer=p.max_num_buffer,
                realtime=realtime, output_dir=self._sidebar.output_dir,
                fps=fps, encoder=p.encoder)
        except Exception as e:
            # A broken preflight must never be what stops a recording.
            print(f"[acq] capacity preflight failed to run: {e}", flush=True)
            return choice_blocking, []
        blocking = list(blocking or [])
        return (choice_blocking + [b for b in blocking
                                   if b not in choice_blocking],
                list(warnings or []))

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

        A profile on an external trigger source has its own check
        (_external_stim_refusal): no paradigm can run there at all.
        """
        if self._external_trigger():
            return self._external_stim_refusal(acq_type)
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

    def _external_stim_refusal(self, acq_type: str):
        """(title, message) when a recording on an external trigger source
        would carry a stimulation paradigm, or None.

        A paradigm runs only on the trigger board, which this mode never
        opens, so a canvas holding one would label a recording as stimulated
        while nothing fired. Every recording with a paradigm on the canvas is
        refused. A calibration never carries one, so it is not.
        """
        stim = self._stim_window
        if stim is None:
            return None
        if stim.is_testing():
            return ("Stop the stimulation test first",
                    "A stimulation test is still running from the previous "
                    "profile. Stop it in the Stimulation editor, then start "
                    "again.")
        if acq_type != "recording":
            return None
        try:
            blocks, _edges = stim.get_workflow()
        except Exception as e:
            blocks, why = True, f"\n\nThe canvas could not be read: {e}"
        else:
            why = ""
        if not blocks:
            return None
        return ("Stimulation needs the trigger board",
                f"The Stimulation canvas holds a paradigm, but this profile "
                f"takes its triggers from your own source (trigger_source: "
                f"external). A paradigm runs only on Panopticon's trigger "
                f"board, so none can run here, and the recording would be "
                f"labelled as stimulated.\n\nSwitch to the profile the "
                f"paradigm was made on, clear or save the canvas, then switch "
                f"back and press Record again.{why}")

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

        # The toggles are disabled while the hardware check runs; this is the
        # guard for a start that reaches here another way.
        if self._hw_check_pending:
            self._refuse_start(
                "The hardware check is running",
                "Panopticon is still checking the hardware for this profile: "
                "it installs the encoder and probes the NVENC session cap, "
                "and a start now would record on whatever was installed "
                "before.\n\nStart again once the status bar says the check "
                "is done.")
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
        self._sweep_warnings = []
        self._finalize_warnings = []
        self._reported_rate_warnings = set()
        self._align_notes = []
        self._thermal_warnings = []
        self._thermal_reported = set()
        self._thermal_shutdown_warnings = []
        self._thermal_logged = frozenset()
        self._thermal_alert = None
        self._source_alarm_raised = False
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

    def _set_overwrite_aside(self):
        """Move the session the operator agreed to overwrite out of the way.
        Worker side, external trigger source only.

        RULE: on an external trigger source the agreed session is renamed
        into a hidden directory beside it, deleted only once the first
        trigger arrives (_drop_overwrite_aside), and put back by every start
        that ends before then (_restore_overwrite_aside). REASON: in this
        mode the refusals after this point are routine: a source left
        running, Cancel at the prompt, no pulse within the timeout. None of
        them records anything, and a delete here would cost the earlier
        session on each one. A rename on the same volume takes no time and
        keeps the files' timestamps. The files in KEPT_ON_OVERWRITE are
        copied into the new directory, so the session set aside stays whole
        until it is dropped.

        The guard is _overwrite_dir_if_agreed's: only the agreed directory,
        and only one strictly inside the output directory. Raises OSError
        when the rename or a copy fails; the start then refuses, and
        _discard_refused_capture puts back a rename that did happen.
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
        holder = Path(tempfile.mkdtemp(prefix=f".{target.name}-overwritten-",
                                       dir=target.parent))
        try:
            os.replace(target, holder / target.name)
        except OSError as e:
            try:
                holder.rmdir()
            except OSError:
                pass
            raise OSError(f"the earlier data in {target} could not be moved "
                          f"aside for the overwrite ({e}). Close anything "
                          f"that has a file in it open.") from e
        self._overwrite_aside = (holder, target)
        print(f"[acq] moved the earlier data in {target} aside into {holder}; "
              f"it is deleted when the first trigger arrives", flush=True)
        if kept:
            target.mkdir(parents=True, exist_ok=True)
            self._created_dirs.append(target)
            for name in kept:
                shutil.copy2(holder / target.name / name, target / name)

    def _restore_overwrite_aside(self) -> tuple:
        """Put back the session _set_overwrite_aside moved out of the way.

        Returns (note, failed): the paragraph a refusal adds to its message,
        "" when nothing was set aside, and whether the session is still out
        of place. Call once this start's own directory is gone: the rename
        back needs the path free.
        """
        aside, self._overwrite_aside = self._overwrite_aside, None
        if aside is None:
            return "", False
        holder, target = aside
        moved = holder / target.name
        try:
            os.replace(moved, target)
        except OSError as e:
            print(f"[acq] could not put the earlier data back into {target}: "
                  f"{e}; it is in {moved}", flush=True)
            return (f"The data that was in {target} before this start could "
                    f"not be put back ({e}). None of it was deleted: it is in "
                    f"{moved}. Move that folder back to {target} by hand.",
                    True)
        try:
            holder.rmdir()
        except OSError as e:
            print(f"[acq] could not remove the empty {holder}: {e}",
                  flush=True)
        print(f"[acq] put the earlier data back into {target}", flush=True)
        return (f"The data already in {target} is back in place: none of it "
                f"was deleted.", False)

    def _drop_overwrite_aside(self) -> None:
        """Delete the session _set_overwrite_aside moved out of the way. UI
        thread, at the first trigger: from there the start can no longer be
        cancelled, so the overwrite the operator agreed to goes ahead.

        Only a directory strictly inside the output directory is removed.
        Whatever cannot be removed is logged and becomes a line of this
        acquisition's WARNINGS.txt and its post-session dialog
        (_sweep_warnings), because it holds the earlier data under a hidden
        name beside this one.
        """
        aside, self._overwrite_aside = self._overwrite_aside, None
        if aside is None:
            return
        holder, target = aside
        try:
            base = Path(self._sidebar.output_dir).resolve()
            resolved = holder.resolve()
            inside = resolved.is_relative_to(base) and resolved != base
        except (OSError, ValueError):
            inside = False
        errors = []
        if inside:
            shutil.rmtree(holder, onerror=lambda _f, path, exc: errors.append(
                f"{path}: {exc[1]}"))
        else:
            errors.append("it is outside the output directory")
        if not holder.exists():
            print(f"[acq] overwrote existing data in {target}", flush=True)
            return
        print(f"[acq] could not remove the earlier data set aside in {holder}: "
              f"{errors[0] if errors else 'unknown error'}", flush=True)
        self._sweep_warnings = list(self._sweep_warnings) + [
            f"The earlier data this {self._acq_label()} replaced could not be "
            f"deleted completely ({errors[0] if errors else 'unknown error'}). "
            f"What is left is in {holder}, a hidden directory beside this "
            f"one. It is not part of this take; delete it by hand."]

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

        On an external trigger source the agreed session is moved aside
        instead of deleted (_set_overwrite_aside), because the refusals that
        follow are routine there.
        """
        if self._external_trigger():
            self._set_overwrite_aside()
        else:
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

        A file that cannot be removed is logged and becomes a line of this
        recording's WARNINGS.txt and its post-session dialog
        (_sweep_warnings), because it now sits beside this take claiming to
        describe it.
        """
        stale_paths = [self._video_dir / name for name in STALE_SESSION_FILES]
        stale_paths += [self._video_dir / cam / name
                        for cam in self._camera_names
                        for name in ("raw_tail.bin", "tail.h264",
                                     "encode_error.log", "WARNINGS.txt")]
        warnings = []
        for path in stale_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as e:
                print(f"[acq] could not remove {path}, left by an earlier "
                      f"take: {e}", flush=True)
                text = (f"{path.relative_to(self._video_dir).as_posix()} is "
                        f"left from an earlier take in this directory and "
                        f"could not be removed ({e}). It describes that "
                        f"take, not this one.")
                if path.name in STIM_FILES:
                    text += (" This recording's stim_paradigm.json and "
                             "stim_trace.csv may describe the earlier take's "
                             "paradigm instead of this one's.")
                warnings.append(text)
        self._sweep_warnings = warnings

    def _arm_encoder_record(self, rig) -> None:
        """Note the encoder and NVENC upload this acquisition starts with.

        Read off the installed seam just before the encoders are built, which
        is what they are built from, and written into session_metadata.json
        at stop: the profile says what was asked for (`encoder: auto`, the
        requested upload), and only this says what recorded.
        """
        self._session_encoder = installed_encoder(bool(rig.realtime_encode))
        self._session_upload = None
        if self._session_encoder != "nvenc":
            return
        try:
            from gui_app import nvenc
            self._session_upload = dict(nvenc.upload_config(),
                                        stats_at_start=nvenc.upload_stats())
        except Exception as e:
            print(f"[acq] NVENC upload setting unavailable: {e}", flush=True)

    def _start_body(self, acq_type, raw_paths, display_every, realtime, kick,
                    fps) -> dict:
        """Claim the board, start the cameras, start the triggers.

        Runs on a worker thread and returns what _on_acquisition_started
        needs: {"ok": True}, or {"ok": False, "title", "message"} plus
        "cameras_closed" when the rollback had to abandon them. A profile on
        an external trigger source takes _start_body_external instead, which
        adds "await_trigger" to its {"ok": True}.
        """
        external = self._external_trigger()
        # The stop warnings name the source the operator has to check.
        self._camera_mgr.trigger_source = "external" if external else "board"
        try:
            if external:
                return self._start_body_external(acq_type, raw_paths,
                                                 display_every, realtime,
                                                 kick, fps)
            return self._start_body_inner(acq_type, raw_paths, display_every,
                                          realtime, kick, fps)
        except Exception as e:
            traceback.print_exc()
            if external:
                # No start went anywhere, and the source may be running, so
                # the cameras are cancelled rather than stopped.
                return self._cancel_external_start(
                    f"Starting the {acq_type} failed:\n\n"
                    f"{type(e).__name__}: {e}")
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
        if teensy is None and self._quitting:
            return self._quit_during_start()
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

        self._arm_encoder_record(rig)
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
            # nothing recorded, so there is nothing to roll back. A refusal
            # because the kick-out encoders could not be created makes the
            # cached NVENC session count suspect (invalidate_nvenc_cache).
            failures = list(getattr(self._camera_mgr, "last_encoder_failures",
                                    []) or [])
            if failures:
                invalidate_nvenc_cache("; ".join(failures))
            return {"ok": False, "title": "Cannot start the acquisition",
                    "message": str(e)}

        # Barrier: never start the board while a grab thread is still
        # allocating. See CameraManager.wait_until_ready for the measurement.
        # RULE: the start is refused unless every camera is armed. REASON: a
        # camera that arms after the board starts counts its block IDs from
        # a later trigger than the others, so every frame of it is paired
        # with the wrong trigger, with no gap in blockids.npy and a clean
        # rate check. The board has not been sent anything yet, so the
        # rollback stops only the cameras.
        t_bar = time.perf_counter()
        try:
            n_ready, n_tot = self._camera_mgr.wait_until_ready(
                self.READY_TIMEOUT_S)
            late = list(self._camera_mgr.not_ready())
        except Exception as e:
            print(f"[acq] readiness barrier failed: {e}", flush=True)
            return self._rollback_acquisition(
                f"The check that every camera is armed before the trigger "
                f"board starts could not run:\n\n{type(e).__name__}: {e}\n\n"
                f"The board was not started and nothing was recorded. Start "
                f"again.", sent_start=False)
        waited = time.perf_counter() - t_bar
        flag = "" if not late else "  *** NOT ALL READY ***"
        print(f"[acq] grab threads ready {n_ready}/{n_tot} after "
              f"{waited:.2f}s{flag}", flush=True)
        if late:
            names = ", ".join(self._camera_label(i + 1) for i in late)
            return self._rollback_acquisition(
                f"{names} had not armed after {self.READY_TIMEOUT_S:.0f} s, "
                f"so the trigger board was not started: a camera that arms "
                f"after the board starts records every frame against the "
                f"wrong trigger, with nothing in the files to show it.\n\n"
                f"Nothing was recorded. Arming fills each camera's frame "
                f"ring in memory first, so the usual cause is memory "
                f"pressure: close other applications, or lower "
                f"kick_max_lag or max_num_buffer in the rig profile, then "
                f"start again.", sent_start=False)
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
        # Immediately before the start: from here a camera whose stream arms
        # late retires itself, and the frames each camera took before this
        # point are fixed.
        self._camera_mgr.mark_board_starting()
        early = {name: n for name, n
                 in self._camera_mgr.frames_before_barrier().items() if n}
        if early:
            # Every camera is armed and this start has not reached the board,
            # so a frame here came from a trigger this start did not send.
            # Such a camera's block IDs do not start at the board's first
            # trigger. The rollback sends the board a stop, which is always
            # safe, in case it is still triggering from an earlier start.
            detail = ", ".join(f"{name} ({n})" for name, n in early.items())
            return self._rollback_acquisition(
                f"Frames arrived before the trigger board was started: "
                f"{detail}. Something is triggering these cameras already: "
                f"the board still running from an earlier start, another "
                f"trigger source on their input line, or a camera not in "
                f"trigger mode. Their block IDs would not count this "
                f"recording's triggers.\n\nNothing was recorded. Check the "
                f"trigger wiring and the cameras' trigger settings, then "
                f"start again.", sent_start=True)
        print(f"[acq] sending start_triggers "
              f"pins={self._profile.trigger_pins} fps={fps}", flush=True)
        counted_before_retry = []

        def may_retry() -> bool:
            """Refuse the reset-and-retry once any camera has frames.

            RULE: the board is not reset and restarted under cameras that
            already counted triggers, whatever firmware it runs. REASON: the
            cameras are armed before the start, so frames here mean the board
            was triggering during the first attempt and only the ack is
            missing. The reset restarts the board's trigger count and its
            stim state machine but not the cameras' block IDs, so every
            camera would carry the first attempt's triggers as an offset,
            followed by a gap as long as the reset, and stim_trace.csv would
            place every stimulus that many frames late. Firmware that has
            never spoken RDY is no exception: its frames prove the same
            thing. It still gets the reset whenever no frames were counted,
            which is how such firmware takes a start.
            """
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

        # BoardTriggerSource forwards to this controller's start_triggers
        # with the same arguments: the RDY ack, the reset-and-retry and the
        # may_retry veto are the controller's.
        if not make_trigger_source(self._profile, teensy).start_triggers(
                self._profile.trigger_pins, fps, may_retry=may_retry):
            if self._quitting:
                # The quit closed the link under this start; the board is
                # already stood down.
                return self._quit_during_start()
            if counted_before_retry:
                # Every sketch Panopticon compiles acks with a RDY line, so a
                # board that has never printed one most likely runs other
                # firmware, and then every start ends here until it is
                # flashed.
                action = ("Start again." if getattr(teensy, "speaks_rdy", False)
                          else "This board has not confirmed any command, "
                               "which every Panopticon sketch does, so it is "
                               "probably running other firmware and every "
                               "start will end this way. Open Stimulation "
                               "and press Apply (an empty canvas is fine) to "
                               "flash it, then start again.")
                return self._rollback_acquisition(
                    f"The trigger board did not confirm the start, but the "
                    f"cameras had already counted "
                    f"{max(counted_before_retry)} frames, so the board did "
                    f"start. Resetting it and starting again would leave "
                    f"every camera's block IDs offset from the board's "
                    f"trigger count, which shifts stim_trace.csv against the "
                    f"paradigm.\n\nThe start has been rolled back. {action}",
                    sent_start=True)
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

    def _start_body_external(self, acq_type, raw_paths, display_every,
                             realtime, kick, fps) -> dict:
        """The start sequence on an external trigger source. Worker thread.

        The order is directories, cameras, readiness barrier, the arm check,
        and no serial port at any point. Panopticon cannot start this source,
        so the barrier holds only if the operator starts it after every
        camera is armed. The arm check (ExternalTriggerSource.close_barrier)
        refuses a start in which any camera received a frame first: the
        source was already running, and each camera's block IDs then count
        from a different pulse. Returns {"ok": True, "await_trigger": True}
        once the cameras are armed and the barrier is closed; the prompt to
        start the source and the wait for its first pulse run on the UI
        thread (_begin_external_wait).

        Every refusal after the cameras start cancels them instead of
        stopping them, and removes what the start wrote
        (_cancel_external_start): a source that is running would otherwise
        keep a stop waiting and fill files that are not a recording.
        """
        rig = self._session_rig()
        source = self._trigger_source()
        action = "Calibrate" if acq_type == "calibration" else "Record"
        if self._quitting:
            return self._quit_during_start()
        try:
            self._create_capture_dirs()
        except OSError as e:
            # Nothing is armed yet, so what the start made is removed at once
            # and an earlier session it had set aside goes back.
            note, _failed = self._discard_refused_capture()
            return {"ok": False,
                    "title": "Could not create the session directories",
                    "message": (
                        f"{self._video_dir}\n\ncould not be created:\n\n{e}"
                        f"\n\nNothing has been started and nothing has been "
                        f"recorded. Check the output directory, then start "
                        f"again." + (f"\n\n{note}" if note else ""))}

        self._arm_encoder_record(rig)
        try:
            self._camera_mgr.start_acquisition(
                raw_paths, display_every=display_every,
                realtime=realtime, width=rig.frame_width,
                height=rig.frame_height, quality=rig.quality,
                fps=fps, realtime_kick=kick, kick_max_lag=rig.kick_max_lag,
                exposure_us=(rig.calibration_exposure_us or None
                             if acq_type == "calibration" else None),
                gain_db=(rig.calibration_gain_db
                         if acq_type == "calibration"
                         and rig.calibration_gain_db >= 0 else None))
        except AcquisitionStartRefused as e:
            # The cameras are back in preview with nothing recorded, so what
            # the start made goes, and an earlier session it set aside comes
            # back.
            failures = list(getattr(self._camera_mgr, "last_encoder_failures",
                                    []) or [])
            if failures:
                invalidate_nvenc_cache("; ".join(failures))
            note, _failed = self._discard_refused_capture()
            return {"ok": False, "title": "Cannot start the acquisition",
                    "message": str(e) + (f"\n\n{note}" if note else "")}

        t_bar = time.perf_counter()
        try:
            n_ready, n_tot = self._camera_mgr.wait_until_ready(
                self.READY_TIMEOUT_S)
            late = list(self._camera_mgr.not_ready())
        except Exception as e:
            print(f"[acq] readiness barrier failed: {e}", flush=True)
            return self._cancel_external_start(
                f"The check that every camera is armed before your trigger "
                f"source starts could not run:\n\n{type(e).__name__}: {e}"
                f"\n\nNothing was recorded. Start again.")
        waited = time.perf_counter() - t_bar
        flag = "" if not late else "  *** NOT ALL READY ***"
        print(f"[acq] grab threads ready {n_ready}/{n_tot} after "
              f"{waited:.2f}s{flag}", flush=True)
        if late:
            names = ", ".join(self._camera_label(i + 1) for i in late)
            return self._cancel_external_start(
                f"{names} had not armed after {self.READY_TIMEOUT_S:.0f} s, "
                f"so you were not asked to start your trigger source. A "
                f"camera that arms after the first pulse counts its block IDs "
                f"from a later trigger than the others, and nothing in the "
                f"files shows it.\n\nNothing was recorded. Arming fills each "
                f"camera's frame ring in memory first, so the usual cause is "
                f"memory pressure: close other applications, or lower "
                f"kick_max_lag or max_num_buffer in the rig profile, then "
                f"start again.")
        try:
            print(self._camera_mgr.pinning_report(), flush=True)
        except Exception as e:
            print(f"[acq] pinning report unavailable: {e}", flush=True)
        if self._quitting:
            return self._quit_during_start()

        refusal = source.close_barrier(self._camera_mgr, fps, action=action)
        if self._quitting:
            return self._quit_during_start()
        if refusal:
            print(f"[acq] refusing the start: frames arrived before every "
                  f"camera was armed {self._camera_mgr.frames_before_barrier()}",
                  flush=True)
            return self._cancel_external_start(
                refusal, title="The trigger was already running")
        print(f"[acq] every camera is armed and none has received a frame; "
              f"the operator starts the trigger source at {fps} Hz",
              flush=True)
        return {"ok": True, "await_trigger": True}

    def _cancel_external_start(self, message: str,
                               title: str = "Could not start the acquisition"
                               ) -> dict:
        """Undo an external-source start that must not record, and describe it
        for the dialog. Worker thread.

        No start was sent anywhere, so there is nothing to stand down: the
        cameras are cancelled, what the start wrote is removed and a session
        it set aside is put back (_cancel_capture, _discard_refused_capture).
        """
        result = {"ok": False, "title": title, "message": message}
        if self._cancel_capture():
            result["cameras_closed"] = True
            result["message"] += (
                "\n\nThe cameras could not be put back in preview and have "
                "been closed. Switch profile and back (or restart "
                "Panopticon) to reopen them.")
        note, _failed = self._discard_refused_capture()
        if note:
            result["message"] += f"\n\n{note}"
        return result

    def _cancel_capture(self) -> bool:
        """Return the cameras to preview without recording anything. Worker
        thread. True when they had to be closed instead.

        The recording grab threads are abandoned, not stopped. A trigger
        source that is still running keeps a stop's grab threads receiving,
        so the stop would wait out its bound and then drain every frame the
        cameras took into files that are about to be removed. A manager with
        no cancel_acquisition, or one whose cancel fails (a grab thread that
        will not exit), is abandoned, which closes the cameras.
        """
        cancel = getattr(self._camera_mgr, "cancel_acquisition", None)
        if cancel is not None:
            try:
                cancel()
                return False
            except Exception as e:
                print(f"[acq] cancelling the acquisition failed: {e}",
                      flush=True)
        try:
            self._camera_mgr.abandon()
        except Exception as e:
            print(f"[acq] abandon while cancelling failed: {e}", flush=True)
        return True

    def _discard_refused_capture(self) -> tuple:
        """Remove what a refused external-source start wrote, and put back
        the session it set aside. Worker thread, or the quit.

        The start had armed its cameras, so it may have written frames a
        running source delivered before the barrier, or empty streams when
        no trigger came. Neither is a recording. A directory this start
        created is removed whole, because nothing in it is older than the
        start. In a camera directory that already existed, only raw.bin and
        stream.h264 are removed: the start deleted both before it opened
        them, so what is there now is its own. Nothing outside the output
        directory is touched. An overwrite the operator agreed to has only
        moved the earlier session aside, and it goes back once this start's
        directory is gone (_restore_overwrite_aside).

        Returns _restore_overwrite_aside's (note, failed).
        """
        self._remove_refused_capture()
        return self._restore_overwrite_aside()

    def _remove_refused_capture(self) -> None:
        """The removal half of _discard_refused_capture."""
        video_dir = self._video_dir
        created = list(self._created_dirs)
        self._created_dirs = []
        if video_dir is None:
            return
        try:
            base = Path(self._sidebar.output_dir).resolve()
        except (OSError, ValueError):
            base = None

        def inside(path: Path) -> bool:
            try:
                resolved = path.resolve()
                return (base is not None and resolved.is_relative_to(base)
                        and resolved != base)
            except (OSError, ValueError):
                return False

        def report(_func, path, exc_info):
            print(f"[acq] could not remove {path}: {exc_info[1]}", flush=True)

        made = set(created)
        for cam in self._camera_names:
            cam_dir = video_dir / cam
            if cam_dir in made or not inside(cam_dir):
                continue
            for name in ("raw.bin", "stream.h264"):
                try:
                    (cam_dir / name).unlink(missing_ok=True)
                except OSError as e:
                    print(f"[acq] could not remove {cam_dir / name}: {e}",
                          flush=True)
        for path in sorted(created, key=lambda p: len(p.parts), reverse=True):
            if not path.exists():
                continue
            if not inside(path):
                print(f"[acq] refusing to remove {path}: outside the output "
                      f"directory", flush=True)
                continue
            shutil.rmtree(path, onerror=report)
        print(f"[acq] removed what the refused start wrote in {video_dir}",
              flush=True)

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
            if self._overwrite_aside is not None:
                # An external-source start that ended without putting back
                # the session it set aside; its own refusals already have.
                note, _failed = self._discard_refused_capture()
                if note:
                    result["message"] = (result.get("message", "")
                                         + f"\n\n{note}")
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
        if result.get("await_trigger"):
            self._begin_external_wait()

    # ── external trigger source ───────────────────────────────────────────────
    def _external_trigger(self) -> bool:
        """True when this profile's triggers come from the operator's own TTL
        source (trigger_source: external) rather than the trigger board."""
        return getattr(self._profile, "trigger_source", "board") == "external"

    def _trigger_source(self):
        """What starts and stops this rig's camera triggers
        (trigger_source.make_trigger_source over the window's board link)."""
        return make_trigger_source(self._profile, self._teensy)

    def _acq_label(self) -> str:
        return "calibration" if self._acq_type == "calibration" else "recording"

    def _acq_action(self) -> str:
        """The toggle that starts this acquisition, as a dialog names it."""
        return "Calibrate" if self._acq_type == "calibration" else "Record"

    def _begin_external_wait(self):
        """Every camera is armed on an external source: prompt the operator
        and watch for the first trigger. UI thread.

        The acquisition is live from here: the first pulse is recorded. If
        none reaches any camera within FIRST_TRIGGER_TIMEOUT_S the start is
        cancelled (_no_trigger_received). That bound ends before the grab
        loops' pre-trigger grace, so no camera is retired for silence the
        operator has not ended yet.
        """
        self._ext_phase = "awaiting"
        self._ext_deadline = time.monotonic() + FIRST_TRIGGER_TIMEOUT_S
        self._sidebar.set_status("WAITING FOR TRIGGER", "#ffaa00")
        self.statusBar().showMessage(
            "Every camera is armed: start your trigger source now")
        self._show_external_prompt(
            "Start your trigger source",
            ExternalTriggerSource.prompt_text(
                self._acq_fps, FIRST_TRIGGER_TIMEOUT_S, self._acq_label()),
            "Cancel")
        self._start_external_watch()
        print(f"[acq] every camera is armed; waiting up to "
              f"{FIRST_TRIGGER_TIMEOUT_S:.0f} s for the external trigger "
              f"source's first pulse", flush=True)

    def _start_external_watch(self):
        if self._ext_timer is None:
            self._ext_timer = QTimer(self)
            self._ext_timer.timeout.connect(self._poll_external_source)
        self._ext_timer.start(self.EXTERNAL_POLL_MS)

    def _stop_external_watch(self):
        """Stop watching the cameras and take the prompt down."""
        if self._ext_timer is not None:
            self._ext_timer.stop()
        self._close_external_prompt()

    def _show_external_prompt(self, title: str, text: str, button: str):
        """Show a non-modal prompt with one button; closing it by any means
        is the button (_on_external_prompt_closed).

        Non-modal, because the acquisition goes on underneath it: the
        watch closes it when the cameras answer, and the operator may still
        use the window.
        """
        self._close_external_prompt()
        box = _TriggerPrompt(self, title, text, button)
        box.finished.connect(
            lambda _code, b=box: self._on_external_prompt_closed(b))
        self._ext_prompt = box
        box.show()

    def _update_external_prompt(self, text: str):
        box = self._ext_prompt
        if box is not None:
            box.setText(text)

    def _close_external_prompt(self):
        """Take the prompt down without it counting as the operator's
        answer."""
        box, self._ext_prompt = self._ext_prompt, None
        if box is None:
            return
        try:
            box.done(0)
            box.deleteLater()
        except Exception as e:
            print(f"[acq] could not close the trigger prompt: {e}", flush=True)

    def _on_external_prompt_closed(self, box):
        """The operator dismissed a prompt. Before the first trigger that
        cancels the acquisition; while the source is asked to stop, it
        finishes the acquisition without waiting any longer."""
        if box is not self._ext_prompt:
            return                    # taken down by the window itself
        self._ext_prompt = None
        if self._ext_phase == "awaiting":
            print("[acq] the operator cancelled before the first trigger",
                  flush=True)
            self._sidebar.clear_toggle_silently(self._acq_type)
            self._stop_acquisition()
        elif self._ext_phase == "stopping":
            print("[acq] the operator asked to finish without waiting for the "
                  "trigger source to stop", flush=True)
            self._ext_phase = None
            self._stop_external_watch()
            self._stop_acquisition()

    def _poll_external_source(self):
        """One look at the cameras during an external-source acquisition.
        UI thread, every EXTERNAL_POLL_MS.

        awaiting: the first result on any camera starts the recording
        proper; the deadline passing is "no trigger received". running:
        every camera silent for end_silence_s means the operator stopped
        the source, and the acquisition finishes through the normal stop.
        stopping: every camera silent for stop_silence_s, or STOP_WAIT_S
        gone, finishes it.

        RULE: each deadline is checked whatever the manager read returns.
        REASON: a read that raises on every tick would otherwise hold the
        window in WAITING FOR TRIGGER, or in STOP YOUR TRIGGER SOURCE, until
        the operator pressed a button. A failed read counts as no trigger
        and no silence, so only the deadline ends the phase.
        """
        phase = self._ext_phase
        if phase is None or self._quitting:
            self._stop_external_watch()
            return
        mgr = self._camera_mgr
        fps = self._acq_fps
        now = time.monotonic()
        try:
            if phase == "awaiting":
                if self._watch_read(
                        lambda: ExternalTriggerSource.triggers_arrived(mgr)):
                    self._external_source_started()
                elif now >= self._ext_deadline:
                    self._no_trigger_received()
                else:
                    self._update_external_prompt(
                        ExternalTriggerSource.prompt_text(
                            fps, self._ext_deadline - now, self._acq_label()))
            elif phase == "running":
                end_s = ExternalTriggerSource.end_silence_s(fps)
                if self._watch_read(
                        lambda: ExternalTriggerSource.source_stopped(mgr,
                                                                     end_s)):
                    print(f"[acq] every camera silent for over {end_s:g} s: "
                          f"the external trigger source has stopped; "
                          f"finishing the {self._acq_label()}", flush=True)
                    self._end_external_acquisition()
            elif phase == "stopping":
                stop_s = ExternalTriggerSource.stop_silence_s(fps)
                stopped = self._watch_read(
                    lambda: ExternalTriggerSource.source_stopped(mgr, stop_s))
                if stopped or now >= self._ext_deadline:
                    if not stopped:
                        print(f"[acq] WARNING: frames still arriving "
                              f"{STOP_WAIT_S:.0f} s after Stop; finishing "
                              f"anyway", flush=True)
                    self._ext_phase = None
                    self._stop_external_watch()
                    self._stop_acquisition()
                else:
                    self._update_external_prompt(
                        ExternalTriggerSource.stop_prompt_text(
                            self._ext_deadline - now, self._acq_label()))
        except Exception as e:
            # A watch that raises in a timer slot would stop watching; one
            # that reports and looks again next tick does not.
            print(f"[acq] trigger-source watch failed: {type(e).__name__}: "
                  f"{e}", flush=True)

    def _watch_read(self, read) -> bool:
        """``read()`` for the trigger-source watch, False when it raises.

        The error is printed when it differs from the last one, so a read
        that fails on every tick is logged once rather than ten times a
        second.
        """
        try:
            answer = bool(read())
        except Exception as e:
            text = f"{type(e).__name__}: {e}"
            if text != self._ext_watch_error:
                print(f"[acq] trigger-source watch could not read the "
                      f"cameras: {text}", flush=True)
                self._ext_watch_error = text
            return False
        self._ext_watch_error = ""
        return answer

    def _external_source_started(self):
        """The first trigger reached a camera: the recording is under way.

        The grab threads' stall detectors are armed now, as the board's ack
        arms them on the board path: from here a camera that receives
        nothing while the others do is a camera fault, not a source that has
        not started. The previous run's reports are swept now too, and an
        earlier session the operator agreed to overwrite is deleted now,
        because until this moment the start could still be cancelled.
        """
        self._ext_phase = "running"
        self._close_external_prompt()
        try:
            self._camera_mgr.signal_triggers_started()
        except Exception as e:
            print(f"[acq] could not arm the stall detectors: {e}", flush=True)
        self._sweep_stale_diagnostics()
        self._drop_overwrite_aside()
        if self._state is State.CALIBRATING:
            self._sidebar.set_status("CALIBRATING", "#4488ff")
        else:
            self._sidebar.set_status("RECORDING", "#ff4444")
        self.statusBar().showMessage(
            f"Triggers arriving: {self._acq_label()} under way. Stop your "
            f"trigger source to finish.")
        print("[acq] the first trigger arrived from the external source",
              flush=True)

    def _end_external_acquisition(self):
        """Finish an acquisition whose source stopped, through the normal
        stop path: the toggle goes off and _stop_acquisition runs, as a
        click would."""
        self._sidebar.clear_toggle_silently(self._acq_type)
        self._stop_acquisition()

    def _external_stop_deferred(self) -> bool:
        """Answer a stop on an external source before the normal stop runs.
        True when the stop is handled here or deferred.

        Before the first trigger nothing has been recorded, so the attempt
        is cancelled. While triggers are still arriving the operator is
        asked to stop the source, and the stop runs once every camera is
        silent (_poll_external_source): a stop that ran now would keep
        receiving frames, stop every grab thread itself once its bound ran out,
        and end each camera on a different pulse. Once every camera is
        silent the normal stop runs, and its grab loops end on their first
        retrieve timeout.
        """
        phase = self._ext_phase
        if phase == "awaiting":
            self._ext_phase = None
            self._stop_external_watch()
            self._end_external_attempt(
                "Acquisition cancelled",
                "No trigger had arrived, so nothing was recorded. The "
                "cameras are back in preview.",
                status=("IDLE", "#888"), dialog=False)
            return True
        if phase == "stopping":
            return True
        if phase == "running":
            try:
                stopped = ExternalTriggerSource.source_stopped(
                    self._camera_mgr,
                    ExternalTriggerSource.stop_silence_s(self._acq_fps))
            except Exception as e:
                print(f"[acq] could not read the cameras' silence: {e}",
                      flush=True)
                stopped = True
            if not stopped:
                self._ext_phase = "stopping"
                self._ext_deadline = time.monotonic() + STOP_WAIT_S
                self._sidebar.set_status("STOP YOUR TRIGGER SOURCE", "#ffaa00")
                self._show_external_prompt(
                    "Stop your trigger source",
                    ExternalTriggerSource.stop_prompt_text(
                        STOP_WAIT_S, self._acq_label()),
                    "Finish now")
                self._start_external_watch()
                print("[acq] stop requested while triggers still arrive; "
                      "waiting for the external source to stop", flush=True)
                return True
        self._ext_phase = None
        self._stop_external_watch()
        return False

    def _no_trigger_received(self):
        """No trigger reached any camera within FIRST_TRIGGER_TIMEOUT_S of the
        prompt: cancel the start and show the "no trigger" state."""
        self._ext_phase = None
        self._stop_external_watch()
        print(f"[acq] no trigger received within "
              f"{FIRST_TRIGGER_TIMEOUT_S:.0f} s of the prompt; cancelling",
              flush=True)
        self._end_external_attempt(
            "No trigger received",
            ExternalTriggerSource.no_trigger_text(
                self._acq_fps, FIRST_TRIGGER_TIMEOUT_S, self._acq_action()),
            status=("NO TRIGGER", "#ff4444"), dialog=True)

    def _end_external_attempt(self, title: str, message: str, status: tuple,
                              dialog: bool):
        """Cancel an external-source acquisition that recorded nothing, off
        the UI thread, then return to IDLE showing ``status``."""
        if self._quitting:
            return          # the quit abandons the cameras and the directory
        self._thermal_timer.stop()
        self._cancel_stim_autostop()
        self._stop_coverage_hud()
        self._detector = None
        self._sidebar.hide_coverage()
        if self._worker_busy(self._cam_op):
            # Only the start worker runs here, and it has delivered its
            # result, so this is its thread finishing. The cancel waits for
            # it rather than leaving the window in an acquisition state.
            print("[acq] a camera operation is still running; cancelling "
                  "once it ends", flush=True)
            QTimer.singleShot(200, lambda: self._end_external_attempt(
                title, message, status, dialog))
            return
        self._begin_busy("Cancelling…")
        self._cam_op = CallableWorker(self._cancel_and_discard)
        self._cam_op.done.connect(
            lambda result: self._on_external_attempt_ended(
                result, title, message, status, dialog))
        self._cam_op.start()

    def _cancel_and_discard(self) -> dict:
        """Worker side of _end_external_attempt."""
        closed = self._cancel_capture()
        note, failed = self._discard_refused_capture()
        return {"cameras_closed": closed, "note": note,
                "restore_failed": failed}

    def _on_external_attempt_ended(self, result, title: str, message: str,
                                   status: tuple, dialog: bool):
        if self._quitting:
            QApplication.restoreOverrideCursor()
            return
        # IDLE before the toggles are reset, so their signal stops nothing.
        self._state = State.IDLE
        self._end_busy()
        self._finalized = True
        self._video_dir = None
        self._sidebar.set_fields_editable(True)
        self._reset_toggles()
        self._sidebar.set_status(*status)
        closed = not isinstance(result, dict) or result.get("cameras_closed")
        if isinstance(result, dict):
            note = result.get("note", "")
            failed = bool(result.get("restore_failed"))
        else:
            message += (f"\n\nCancelling failed: {type(result).__name__}: "
                        f"{result}")
            # The worker may have stopped before it put back an earlier
            # session the start had set aside.
            note, failed = self._restore_overwrite_aside()
        if closed:
            self._camera_grid.setup_grid(0)
            self._camera_names = []
            message += ("\n\nThe cameras could not be put back in preview and "
                        "have been closed. Switch profile and back (or "
                        "restart Panopticon) to reopen them.")
        if note:
            message += f"\n\n{note}"
        self.statusBar().showMessage(
            f"{title}: nothing was recorded"
            + ("; the earlier data is back in place" if note and not failed
               else ""))
        # A session that could not be put back is shown even for a Cancel:
        # the operator has to move it back by hand.
        if dialog or closed or failed:
            QMessageBox.warning(self, title, message)

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

        A profile on an external trigger source has no board to flash, so
        the start continues at once.
        """
        if self._external_trigger():
            return True
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
                reclaimed = self._teensy_connection() is not None
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
                    f"check the board, then retry."
                    + ("" if reclaimed else
                       " The serial port could not be reopened either, so "
                       "the next start reopens it, which resets the board.")
                    + f"\n\n{msg}")
                self._reset_toggles()
                return
            # Retake the port BEFORE recording what was flashed: a reclaim
            # that finds the controller on another port forgets the hint, and
            # a hint recorded first would be wiped, costing a second flash.
            if self._teensy_connection() is None:
                # The start goes ahead and opens the port on its worker, so the
                # reset lands in the start, before any camera is armed. Said
                # here because nothing else would say why the board resets.
                note = (f"Flashed the {label} sketch, but the serial port could "
                        f"not be reopened. The {acq_type} reopens it as it "
                        f"starts, which resets the board: key off the laser.")
                print(f"[acq] {note}", flush=True)
                self.statusBar().showMessage(note)
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

        A profile on an external trigger source uses no board: nothing is
        flashed and no port is opened.
        """
        if self._external_trigger():
            print("[acq] this profile takes its triggers from an external "
                  "source: no trigger board is flashed or opened", flush=True)
            return
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

        None once the window is quitting: the quit has stood the board down,
        and an open now would reset it while the process exits. None on a
        profile with an external trigger source, which opens no serial port.
        """
        if self._quitting:
            return None
        if self._external_trigger():
            return None
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
        # An external source's recording ends when the operator's source
        # does, so its stop may be cancelled or deferred here first.
        if self._external_trigger() and self._external_stop_deferred():
            return
        # Stop the temperature poll FIRST. It is a register read on every
        # camera, made from the UI thread, and the finalize worker is about to
        # be inside the camera SDK on the same devices; two threads making
        # native calls on one device is an access violation, not an
        # exception. _poll_thermals
        # only stops itself on a state change, and the state does not change
        # until the finalize returns.
        self._thermal_timer.stop()
        self._cancel_stim_autostop()
        # Stop the triggers but KEEP the port open: reopening it would reset the
        # board at the start of the next recording and flash a connected laser.
        # This is the everyday stop — the one taken every session — so it is the
        # path where a swallowed failure matters most, not least. The board's
        # stop is TeensyController.stop_triggers, False with no link; an
        # external source has nothing to send and has fallen silent already.
        self._warn_if_not_stood_down(
            self._trigger_source().stop_triggers(self._profile.trigger_pins))

        self._stop_coverage_hud()
        self._detector = None
        self._sidebar.hide_coverage()

        # Draining the encoders and reconfiguring the cameras back to preview
        # is blocking work; run it off the UI thread so the window stays live.
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
                self._save_capture_record(e.results)
                self._save_acquisition_metadata()
                self._write_stim_trace()
                self._finalized = True
                raise
            self._save_capture_record(cam_results)
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
            info = list(getattr(self._camera_mgr, "camera_info", []) or [])
        except Exception as e:
            print(f"[acq] camera identities unavailable: {e}", flush=True)
            info = []
        try:
            path = self._config.save_metadata(
                self._acq_type, camera_info=info or None,
                encoder=self._session_encoder or None,
                capture_processes_used=int(getattr(
                    self._camera_mgr, "capture_processes", 0) or 0))
        except Exception as e:
            print(f"[acq] could not write session metadata: {e}", flush=True)
            return
        extra = {}
        stats = list(getattr(self._camera_mgr, "last_stream_stats", []) or [])
        if stats:
            extra["camera_stream_stats"] = stats
        upload = self._upload_record()
        if upload is not None:
            extra["nvenc_upload_used"] = upload
        if not extra:
            return
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta.update(extra)
            path.write_text(json.dumps(meta, indent=2, default=str),
                            encoding="utf-8")
        except Exception as e:
            print(f"[acq] could not record stream statistics: {e}", flush=True)

    def _upload_record(self) -> dict | None:
        """How NVENC received this acquisition's frames, or None when it did
        not encode on NVENC.

        The profile's nvenc_upload is what was asked for; the launch check
        can have put the host upload back, and a pinned encoder that could
        not be set up falls back to the host upload on its own. The count is
        this acquisition's, not the process's.
        """
        rec = self._session_upload
        if not rec:
            return None
        try:
            from gui_app import nvenc
            now = nvenc.upload_stats()
        except Exception:
            now = {}
        start = rec.get("stats_at_start") or {}
        return dict(
            upload=rec.get("upload"),
            context=rec.get("context") if rec.get("upload") == "pinned"
            else None,
            host_fallbacks=(int(now.get("host_fallbacks", 0))
                            - int(start.get("host_fallbacks", 0))),
            pinned_disabled=now.get("pinned_disabled") or None)

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
        # after reading them. The finalize's own findings follow them.
        self._capture_warnings = (
            list(getattr(self._camera_mgr, "last_warnings", []) or [])
            + list(self._finalize_warnings))
        # RULE: an acquisition in which a real-time encoder could not be
        # created or failed forgets the cached NVENC session count. REASON:
        # the count the preflight passed on was wrong for this start, and a
        # cache that is never re-probed passes the next start on it too.
        failures = list(getattr(self._camera_mgr, "last_encoder_failures",
                                []) or [])
        if failures:
            invalidate_nvenc_cache("; ".join(failures))
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
            # RULE: the capture warnings are written and shown here too.
            # REASON: this is the session most likely to be misread later,
            # and the manager's warnings (a board that ignored the stop, a
            # retired camera, exposure not applied) exist nowhere else.
            problems = ([f"Saving the recording failed: "
                         f"{type(_result).__name__}: {_result}"]
                        + list(self._capture_warnings)
                        + self._thermal_shutdown_texts())
            written = self._write_warnings_file(problems)
            where = (f"\n\nThis has also been written to:\n{written}"
                     if written else "")
            warn_text = ("\n\nCapture warnings:\n- "
                         + "\n- ".join(self._capture_warnings)
                         if self._capture_warnings else "")
            QMessageBox.critical(
                self, "Recording did not finish cleanly",
                f"Saving the recording failed:\n\n{type(_result).__name__}: "
                f"{_result}\n\n"
                + (f"Still running when the stop gave up: {stuck}.\n\n"
                   if stuck else "")
                + f"The capture files are still in:\n"
                f"{self._video_dir}\n\nThey have NOT been encoded or deleted. Do "
                f"not start another recording into that directory. Once the "
                f"cause is fixed, 0_encode.py turns them into mp4s:\n"
                f"uv run python 0_encode.py \"{self._video_dir}\"\n\nThe "
                f"cameras have been closed: switch profile and back, or restart "
                f"Panopticon, to reopen them." + warn_text + where)
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

    def _save_capture_record(self, cam_results: list) -> None:
        """Everything the finalize writes from the capture itself. Worker thread.

        The frame times and block IDs, the RETIRED.json of each retired
        camera, and the block-ID rate check outside kick mode. What they find
        is kept in _finalize_warnings for this recording's report.
        """
        warnings = list(self._save_frametimes(cam_results) or [])
        self._write_retired()
        rate = self._finalize_rate_check()
        self._reported_rate_warnings = set(self._reported_rate_warnings) | set(rate)
        self._finalize_warnings = warnings + rate

    def _save_frametimes(self, cam_results: list[tuple[int, list[float], list[int]]]
                         ) -> list:
        """Write each camera's frametimes.npy and blockids.npy. Worker thread.

        Returns the warnings, one per camera whose raw.bin disagreed with its
        own block IDs.

        RULE: every camera keeps every frame it persisted, in every mode; no
        camera is cut to another camera's length. REASON: cameras drop frames
        independently, so frame i is not the same trigger on two cameras, and
        the block IDs are what align them afterwards (the post-hoc alignment,
        2_align.py). Cutting each raw.bin to the shortest camera's count
        destroys the survivors' frames when one camera stops early, and in
        raw mode gives equal-length videos whose frame i is a different
        trigger on any camera that dropped one.

        RULE: when a raw.bin and its camera's block IDs disagree, the block
        IDs are cut to the whole frames raw.bin holds, never the reverse, and
        the camera is named in a warning. REASON: blockids.npy records only
        frames that were persisted; a frame on disk without a block ID cannot
        be placed on the trigger timeline, so it is the one cut, and a
        partial frame at the end of the file is cut with it.
        """
        rig = self._session_rig()
        frame_size = int(rig.frame_width) * int(rig.frame_height)
        warnings = []
        for i, (_count, timestamps, block_ids) in enumerate(cam_results):
            if not timestamps:
                continue
            cam = self._camera_names[i]
            cam_dir = self._video_dir / cam
            n = len(timestamps)
            if block_ids:
                n = min(n, len(block_ids))

            raw_path = cam_dir / "raw.bin"
            if raw_path.exists() and frame_size > 0:
                size = raw_path.stat().st_size
                on_disk = size // frame_size
                keep = min(on_disk, n)
                if keep * frame_size != size:
                    with open(raw_path, "r+b") as f:
                        f.truncate(keep * frame_size)
                note = None
                if on_disk < n:
                    note = (f"{cam}: raw.bin holds {on_disk} whole frames but "
                            f"{n} were recorded, so its block IDs and frame "
                            f"times are cut to the {on_disk} frames on disk. "
                            f"The frames it lost at the end are missing from "
                            f"its video.")
                    n = on_disk
                elif on_disk > n:
                    note = (f"{cam}: raw.bin holds {on_disk} frames but only "
                            f"{n} have a block ID; the {on_disk - n} without "
                            f"one cannot be placed on the trigger timeline and "
                            f"were cut from raw.bin.")
                elif size != keep * frame_size:
                    print(f"[acq] {cam}: raw.bin ended in a partial frame; it "
                          f"was cut to its {keep} whole frames", flush=True)
                if note:
                    warnings.append(note)
                    print(f"[acq] WARNING: {note}", flush=True)
            if n <= 0:
                continue

            frame_nums = np.arange(1, n + 1, dtype=np.float64)
            ts_arr = np.array(timestamps[:n], dtype=np.float64)
            ts_arr -= ts_arr[0]
            np.save(cam_dir / "frametimes.npy", np.stack([frame_nums, ts_arr]))
            # Block ID = trigger ordinal; dropped frames show as gaps, so
            # cross-camera alignment survives a drop (see gui_app/alignment.py).
            if block_ids:
                bids = np.asarray(block_ids[:n], dtype=np.int64)
                np.save(cam_dir / "blockids.npy", bids)
        return warnings

    def _write_retired(self) -> None:
        """RETIRED.json beside each camera the capture retired. Worker thread.

        2_align.py and this window's own alignment leave such a camera out of
        the alignment, so a camera that stopped early cannot cut the cameras
        that kept recording down to its length.
        """
        retired = dict(getattr(self._camera_mgr, "last_retired", {}) or {})
        for name, reason in retired.items():
            cam_dir = self._video_dir / name
            if not cam_dir.is_dir():
                continue
            extra = {}
            try:
                bids = np.load(cam_dir / "blockids.npy")
                extra = dict(frames=int(bids.size),
                             last_block_id=int(bids[-1]) if bids.size else None)
            except (OSError, ValueError):
                extra = dict(frames=0, last_block_id=None)
            try:
                recording_meta.write_retired(cam_dir, reason, **extra)
                print(f"[acq] {name}: RETIRED.json written ({reason})",
                      flush=True)
            except OSError as e:
                print(f"[acq] could not write {name}/RETIRED.json: {e}",
                      flush=True)

    def _finalize_rate_check(self) -> list:
        """The block-ID rate check of a recording made without kick-out.
        Worker thread; returns its warnings.

        RULE: it runs at every stop outside kick mode, from the block IDs and
        frame times just saved. REASON: a camera that ignores triggers
        (exposure over the ceiling) keeps gapless block IDs and the same frame
        count as the others, so nothing else in these modes looks: the
        post-hoc alignment finds nothing to trim and stops there. Kick mode
        runs the same check in the router's stop and reports it through the
        manager's warnings.
        """
        rig = self._session_rig()
        if rig.realtime_encode and rig.realtime_kick:
            return []
        fps = self._acq_fps or rig.frame_rate
        try:
            an = alignment.analyse(self._video_dir, fps)
        except Exception as e:
            msg = (f"The block-ID rate check could not run on this recording "
                   f"({e}), so a camera that ignored triggers would not be "
                   f"detected. Run 2_align.py on {self._video_dir} to check "
                   f"it.")
            print(f"[acq] WARNING: {msg}", flush=True)
            return [msg]
        for name, why in an.rate_skipped.items():
            print(f"[acq] block-ID rate check skipped {name}: {why}",
                  flush=True)
        for msg in an.rate_warnings:
            print(f"[acq] WARNING: {msg}", flush=True)
        return list(an.rate_warnings)

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
        # which is where it can still be acted on. After encoding a camera
        # that came near its shutdown point is added to the report only when
        # the session actually lost frames, so a clean recording on warm
        # cameras is not flagged for heat that cost nothing; when frames WERE
        # lost, the thermal history is included so overheating is on the
        # table as the cause. A camera that reached its shutdown point is
        # always reported, because from there it stops delivering. Either way
        # the temperatures stay in session_metadata.json.
        lost_frames = bool(self._capture_warnings) or bool(failed) \
            or bool(self._encode_worker.warnings) \
            or (len(frame_counts) > 1 and min_frames != max_frames)
        if self._thermal_warnings and lost_frames:
            problems += list(self._thermal_warnings)
        problems += self._thermal_shutdown_texts()
        # Not a loss of frames, so not in lost_frames: these are files from
        # an earlier take that could not be removed at the start.
        problems += list(self._sweep_warnings)
        if problems:
            # Write it down as well as showing it: a dialog is dismissed and
            # forgotten, and this is exactly what someone needs months later
            # when the data looks odd.
            body = "\n\n".join(problems)
            written = self._write_warnings_file(problems)
            QMessageBox.warning(
                self, "Recording completed with problems",
                f"{body}\n\nThis has also been written to:\n"
                f"{written or self._video_dir / recording_meta.WARNINGS_NAME}")

        # Kick-out keeps every camera on the same triggers, so a kick-mode
        # session is NOT auto-aligned, even after it dropped frames: its
        # forced-drop, kick-out and block-rate warnings are reported
        # (WARNINGS.txt, above, from the router's stop) rather than followed
        # by an unrequested re-encode. Every other mode (post-hoc alignment,
        # and raw capture, whose cameras keep whatever frames they caught)
        # runs the post-hoc alignment. A kick session whose videos are
        # unequal length - a retirement, a truncated tail - is flagged so the
        # operator can run 2_align.py by hand instead of it happening silently.
        rig = self._session_rig()
        if rig.realtime_encode and rig.realtime_kick:
            self._warn_if_unequal_videos()
        elif self._start_alignment():
            return
        self._finish_to_idle()

    def _thermal_shutdown_texts(self) -> list:
        return [text for _idx, text in self._thermal_shutdown_warnings]

    def _write_warnings_file(self, problems: list):
        """Write this acquisition's WARNINGS.txt afresh; its path, or None.

        The start removed the previous take's copy, and this is the first
        writer, so it replaces rather than appends; the alignment appends
        after it.
        """
        path = self._video_dir / recording_meta.WARNINGS_NAME
        try:
            path.write_text("\n\n".join(problems) + "\n", encoding="utf-8")
        except Exception as e:
            print(f"[acq] could not write WARNINGS.txt: {e}", flush=True)
            return None
        return path

    def _append_warnings(self, problems: list):
        """Append paragraphs to this acquisition's WARNINGS.txt; its path, or
        None when it could not be written (the caller shows them anyway)."""
        path = recording_meta.append_warning(self._video_dir,
                                             "\n\n".join(problems))
        if path is None:
            print("[align] could not append to WARNINGS.txt", flush=True)
        return path

    @staticmethod
    def _names_text(names) -> str:
        names = list(names)
        if len(names) <= 1:
            return "".join(names)
        return ", ".join(names[:-1]) + " and " + names[-1]

    def _warn_if_unequal_videos(self):
        """Flag a kick-mode session whose per-camera videos are not all the
        same triggers, without re-encoding it. The operator chose to skip
        auto-align, but an unequal set left unremarked is the silent trap this
        reports.

        RULE: a retired camera is named as retired, and a camera that ended
        early is named with the command that aligns the others. REASON:
        trimming every camera to the frames all of them share cuts the
        cameras that kept recording down to the one that stopped, which is
        the data the retirement exists to keep; 2_align.py leaves a camera
        with a RETIRED.json out by default, and --exclude leaves out any
        other.
        """
        fps = self._acq_fps or self._session_rig().frame_rate
        retired = recording_meta.retired_cameras(self._video_dir)
        try:
            an = alignment.analyse(self._video_dir, fps, exclude=retired)
        except Exception as e:
            print(f"[align] equal-length check skipped ({e})", flush=True)
            return
        notes = []
        if retired:
            names = self._names_text(retired)
            one = len(retired) == 1
            rest = ("hold the same triggers" if not an.needed
                    else "do not all hold the same triggers either (below)")
            notes.append(
                f"{names} {'was' if one else 'were'} retired during the "
                f"recording, so {'its video ends' if one else 'their videos end'} "
                f"early and the videos are not equal length. The other "
                f"cameras' videos {rest}. 2_align.py leaves a retired camera "
                f"out by default (its RETIRED.json), so it never cuts the "
                f"others to {'its' if one else 'their'} length; pair "
                f"{'its' if one else 'their'} frames with theirs by block ID "
                f"(blockids.npy), not by frame number.")
        if an.needed:
            short = {nm: why for nm, why in an.short_cams.items()}
            if short:
                names = ",".join(short)
                notes.append(
                    "The cameras did not all keep the same frames: "
                    + "; ".join(f"{nm} {why}" for nm, why in short.items())
                    + f". Run 2_align.py --replace --exclude {names} on this "
                      f"session to align the other cameras and leave "
                      f"{self._names_text(short)} as recorded, or add "
                      f"--truncate-to-shortest to cut every camera to the "
                      f"{an.common.size} triggers they share.")
            else:
                notes.append(
                    "The cameras did not all keep the same frames, so the "
                    "videos are not equal length. Auto-alignment is off, so "
                    "they are left as recorded. Run 2_align.py --replace on "
                    "this session to trim them to the common frames before "
                    "using them together.")
        if not notes:
            return
        note = "\n\n".join(notes)
        print(f"[align] {note}", flush=True)
        self._append_warnings(notes)
        QMessageBox.warning(self, "Videos are not equal length", note)

    def _start_alignment(self) -> bool:
        """Start the post-hoc alignment if cameras dropped different frames.
        Returns True if alignment is now running (caller should defer the
        idle reset).

        RULE: a camera that was retired, or that recorded no frames, is left
        out of the alignment, and every camera left out, every skip and every
        failure is written to WARNINGS.txt and shown. REASON: aligning keeps
        only the triggers every camera holds, so such a camera would cut the
        others down to its frames or to none, and a problem reported only on
        stdout reads afterwards as a recording that was aligned. The replace
        itself refuses while another camera ended early or stopped
        mid-recording (alignment.refusal_reason); the index is written and
        _on_align_done says why.
        """
        rig = self._session_rig()
        fps = self._acq_fps or rig.frame_rate
        exclude = dict(recording_meta.retired_cameras(self._video_dir))
        try:
            an = alignment.analyse(self._video_dir, fps, exclude=exclude)
            if an.empty_cams:
                exclude.update(an.empty_cams)
                an = alignment.analyse(self._video_dir, fps, exclude=exclude)
        except Exception as e:
            problem = (f"The post-hoc alignment did not run on this recording "
                       f"({e}), so no video was aligned and the videos are "
                       f"left as recorded: frame i is not the same trigger on "
                       f"every camera. Fix the cause, then run 2_align.py "
                       f"--replace on {self._video_dir}.")
            print(f"[align] {problem}", flush=True)
            self._append_warnings([problem])
            QMessageBox.warning(self, "Videos were not aligned", problem)
            return False
        self._align_notes = [
            f"{nm} was left out of the post-hoc alignment ({why}). Its video "
            f"and block IDs are as recorded, so pair its frames with the "
            f"others by block ID (blockids.npy), not by frame number."
            for nm, why in exclude.items()]
        if not an.needed:
            # Loss-free among the cameras aligned: the videos already hold the
            # same triggers.
            if self._align_notes:
                self._append_warnings(self._align_notes)
                QMessageBox.warning(self, "Cameras left out of the alignment",
                                    "\n\n".join(self._align_notes))
            return False

        self._state = State.ALIGNING
        self._sidebar.set_status("ALIGNING", "#ffaa00")
        self._sidebar.set_toggles_enabled(False)
        self.statusBar().showMessage("Aligning videos by trigger (re-encode)...")
        self._align_worker = AlignWorker(
            self._video_dir, fps, rig.quality,
            parallel=rig.encode_parallel, exclude=exclude)
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
        if self._quitting:
            # The window is closing: the quit dialog already said what is left
            # to do, and nothing here may start a state or a worker.
            return
        self._sidebar.hide_progress()
        failures = list(summary.get("failures") or [])
        replaced_cams = list(summary.get("replaced_cams") or [])
        failed_cams = list(summary.get("failed_cams") or [])
        refused = summary.get("refused")
        index_error = summary.get("index_error")
        n_cams = len(summary.get("camera_names") or self._camera_names)
        if summary.get("error"):
            self.statusBar().showMessage(
                f"Alignment failed: {summary['error']} — videos left as-is")
        elif refused:
            self.statusBar().showMessage(
                "Alignment index written; no video was replaced (see the "
                "dialog)")
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
                f"{len(failed_cams) or len(failures)} could not be replaced — "
                f"see WARNINGS.txt")
        elif failures:
            self.statusBar().showMessage(
                f"Alignment replaced no camera ({failures[0]}) — originals kept")
        else:
            self.statusBar().showMessage("Alignment: videos already aligned")
        self._report_alignment(summary, failures, replaced_cams, failed_cams,
                               refused, index_error)
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

    def _report_alignment(self, summary, failures, replaced_cams, failed_cams,
                          refused, index_error) -> None:
        """Write the alignment's problems to WARNINGS.txt and show them.

        RULE: none of this decides whether a video was replaced, or whether
        the stimulus trace is regenerated; `replaced` and `replaced_cams`
        decide that. REASON: a block-rate warning or a refused replace says
        nothing about the videos already on disk, and a trace left
        unregenerated after a partial replace is offset in exactly the
        session that had trouble.
        """
        rec = self._video_dir
        problems = list(self._align_notes)
        if summary.get("error"):
            problems.append(
                f"The post-hoc alignment failed ({summary['error']}). The "
                f"videos are left as recorded and are not trigger-aligned: "
                f"frame i is not the same trigger on every camera. Run "
                f"2_align.py --replace on {rec} once the cause is fixed.")
        if refused:
            problems.append(
                f"{refused} The alignment index (aligned/alignment.npz) was "
                f"written and no video was changed, so the videos are not "
                f"trigger-aligned with each other.")
        other = [f for f in failures if f != refused and f != index_error
                 and f != summary.get("error")]
        if other:
            problems.append("Cameras that could not be aligned, with their "
                            "videos kept as recorded: " + "; ".join(other))
        if replaced_cams and failed_cams:
            problems.append(
                f"Only {self._names_text(replaced_cams)} "
                f"{'was' if len(replaced_cams) == 1 else 'were'} replaced by "
                f"the aligned video; {self._names_text(failed_cams)} kept "
                f"{'its' if len(failed_cams) == 1 else 'their'} original. The "
                f"videos are NOT all the common set, so frame i differs "
                f"between them: aligned/alignment.json says which video is "
                f"which (video_is_common). Run 2_align.py --replace on {rec} "
                f"to finish.")
        new_rate = [w for w in (summary.get("rate_warnings") or [])
                    if w not in self._reported_rate_warnings]
        self._reported_rate_warnings = (set(self._reported_rate_warnings)
                                        | set(new_rate))
        problems += new_rate
        if index_error:
            if replaced_cams:
                problems.append(
                    f"{self._names_text(replaced_cams)} "
                    f"{'was' if len(replaced_cams) == 1 else 'were'} aligned "
                    f"and replaced, but the aligned/ index could not be "
                    f"written: {index_error}. Run 2_align.py on {rec} (without "
                    f"--replace) to write it.")
            else:
                problems.append(
                    f"The aligned/ index could not be written: {index_error}. "
                    f"No video was replaced. Run 2_align.py on {rec} to write "
                    f"it.")
        if not problems:
            return
        body = "\n\n".join(problems)
        print(f"[align] {body}", flush=True)
        written = self._append_warnings(problems)
        QMessageBox.warning(
            self, "Alignment reported problems",
            body + (f"\n\nThis has also been written to:\n{written}"
                    if written else ""))

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
        # finished_solve is one of run()'s last two emits (finished follows
        # it), so the thread is ending; joining it lets _toggles_permitted()
        # see the solve as over.
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
        if self._external_trigger():
            # The editor's Apply and Test drive the trigger board, which this
            # profile never opens; say why instead of offering them.
            QMessageBox.information(
                self, "Stimulation needs the trigger board",
                ExternalTriggerSource.no_stimulation_text(self._profile.name))
            return
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
        # REASON: its slot is a register read per camera, it only
        # self-stops on a state change, and the quit path never moves the
        # state to IDLE - so from RECORDING or CALIBRATING it keeps firing,
        # including inside the nested event loops of the two modal dialogs
        # below, against cameras _abandon_and_cleanup is closing. Two threads
        # in the camera SDK on one device is an access violation, not an
        # exception.
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
            elif self._state is State.ENCODING:
                text = (f"State is {self._state.value}. Quit anyway?\n\n"
                        "The capture is COMPLETE and will be KEPT. Only the "
                        "mp4 wrapping is unfinished. To finish it, run\n"
                        f"uv run python 0_encode.py \"{self._video_dir}\"\n"
                        "and then 2_align.py on the same directory (with "
                        "--replace for a recording made without real-time "
                        "kick-out), which also checks the block-ID rate.")
            elif self._state is State.ALIGNING:
                text = (f"State is {self._state.value}. Quit anyway?\n\n"
                        "The capture and its videos are KEPT, but the "
                        "alignment is unfinished: some cameras may already "
                        "hold their aligned video and others not, and "
                        "stim_trace.csv still describes the unaligned frames. "
                        "To finish, run\n"
                        f"uv run python 2_align.py \"{self._video_dir}\" "
                        f"--replace\nand then 3_stim_trace.py on the same "
                        f"directory.")
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
        # An external source's watch and prompt end with the window; the
        # source itself is the operator's to stop.
        self._ext_phase = None
        self._stop_external_watch()
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
        is set, whether or not the link looks open. REASON: a start worker may
        be waiting for its ack on the same controller. The controller runs
        this stop after that attempt and the attempt does not retry after it,
        and no start can be written between the stop and the close; _quitting
        keeps a worker that has not reached the board yet from sending one at
        all. A stop followed by a separate close leaves a gap in which a
        queued start goes out after the stop. And ``is_open`` read here,
        outside the controller's lock, is False while a start's retry is
        between closing the port and reopening it: a quit that skipped the
        stop on that reading would leave the board the retry then starts.
        The controller looks at the link under its lock and says whether a
        stand-down was owed and failed.

        The warning comes before the window goes, while there is something to
        show it on. ``is_open`` proves nothing in the other direction either:
        pyserial keeps it True after the USB device disappears, so an
        unplugged cable looks healthy right up until the write.
        """
        try:
            if self._teensy is None:
                return
            self._warn_if_not_stood_down(
                self._teensy.stop_and_close(self._profile.trigger_pins))
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
        # StopGrabbing()/Close() on every camera from the Qt main thread,
        # while _cam_op is the thread running _finalize — possibly inside
        # _router.stop() or resume_preview(). Two threads making native camera
        # SDK calls on the same device is an access violation, not an
        # exception, so no excepthook can intercept it. Killing the child
        # processes above is what lets these waits actually return.
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
            # Still inside the camera SDK after 3 s. Leaking the camera
            # handles costs nothing at process exit; closing them under a live
            # native call crashes. Skip the teardown entirely.
            print("[quit] _cam_op still running — leaking camera handles rather "
                  "than closing under a live camera SDK call", flush=True)
        else:
            try:
                self._camera_mgr.abandon()
            except Exception as e:
                print(f"[quit] abandon failed: {e}", flush=True)
        if self._overwrite_aside is not None:
            # An external-source start that never reached its first trigger:
            # it recorded nothing, and the session it was to replace is only
            # set aside. What the start wrote goes, and that session comes
            # back, whatever the state says.
            note, _failed = self._discard_refused_capture()
            print(f"[quit] {note}", flush=True)
            return
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
