"""Main application window — wires cameras, sidebar, state machine, and encoding."""
import json
import shutil
from datetime import datetime
import numpy as np
from enum import Enum
from pathlib import Path

from PyQt5.QtWidgets import QMainWindow, QWidget, QHBoxLayout, QApplication, QMessageBox
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QPalette, QColor, QIcon, QCursor

from gui_app.camera_manager import CameraManager, MAX_NUM_BUFFER
from gui_app.grab_thread import ENCODE_QUEUE_DEPTH
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
from gui_app.session_config import SessionConfig, RigProfile
from gui_app.widgets.camera_grid import CameraGridWidget
from gui_app.widgets.sidebar import SidebarWidget
from gui_app.widgets.stimulation_window import StimulationWindow

try:
    from gui_app.board_detector import BoardDetector
except Exception:  # OpenCV missing → coverage HUD disabled, rest of GUI still runs
    BoardDetector = None

CALIBRATION_SCRIPT = Path(__file__).parent.parent / "1_calibrate.py"


class State(Enum):
    IDLE = "IDLE"
    CALIBRATING = "CALIBRATING"
    RECORDING = "RECORDING"
    ENCODING = "ENCODING"
    ALIGNING = "ALIGNING"


class MainWindow(QMainWindow):
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
        self._acq_type = ""
        self._acq_fps = 0
        self._detector = None
        self._coverage_worker: CoverageWorker | None = None
        self._encode_worker: EncodeWorker | None = None
        self._align_worker: AlignWorker | None = None
        self._calib_worker: CalibrationWorker | None = None
        self._config: SessionConfig | None = None
        self._video_dir: Path | None = None
        self._busy = False                 # a blocking camera op is running
        self._cam_op: CallableWorker | None = None
        self._fw_op: CallableWorker | None = None
        # The paradigm applied during THIS session, if any. Never persisted:
        # a launch always starts stimulation-free. Within a session it lets
        # calibration and recording swap firmware automatically.
        self._session_stim_ino: str | None = None

        self._camera_mgr = CameraManager()
        self._teensy = TeensyController()
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
        self._camera_mgr.error.connect(self._on_camera_error)

        self._stim_window: StimulationWindow | None = None
        self._stim_end_timer: QTimer | None = None

        self._display_timer = QTimer()
        self._display_timer.timeout.connect(self._refresh_displays)
        self._display_timer.start(self._display_interval_ms())

        # Prefer whatever profile this machine used last — the profile list is
        # shared with the 3dface rig, so alphabetical order picks the wrong one
        # here. Fall back to the first profile whose .pfs actually exists.
        self._profile = self._sidebar.current_profile
        if self._sidebar.select_profile(self._sidebar.remembered_profile()):
            self._profile = self._sidebar.current_profile
        else:
            for prof in self._sidebar._profiles:
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

    def _open_cameras(self):
        """Open cameras for the current profile (synchronous — startup only)."""
        ok = self._open_cameras_bg()
        self._apply_camera_open_result(ok)

    def _open_cameras_bg(self) -> bool:
        """Blocking open (run on a worker thread for live profile switches)."""
        pfs = self._profile.pfs_path
        if pfs and Path(pfs).exists():
            return self._camera_mgr.open_all(
                pfs, gige_driver=self._profile.gige_driver,
                trigger_rate_limit=self._profile.trigger_rate_limit,
                expect_cameras=self._profile.n_cameras,
                max_num_buffer=self._profile.max_num_buffer)
        return False

    def _apply_camera_open_result(self, ok):
        if ok is True:
            n = self._camera_mgr.num_cameras
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
        # open_all already emits a specific error for a camera fault; only warn
        # here for the plain "nothing opened" case (e.g. missing .pfs).
        if not isinstance(ok, Exception):
            QTimer.singleShot(100, lambda: QMessageBox.warning(
                self, "Camera Error",
                "No cameras found or .pfs missing. Check connections and profile."))

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
        self._busy = False
        self._display_timer.start(self._display_interval_ms())

    def _display_interval_ms(self) -> int:
        """Preview repaint period, widened as the camera count grows.

        Repainting is per-pane work on the Qt MAIN thread — QImage conversion
        plus a widget repaint each — so its cost is linear in camera count while
        the grab threads' deadline stays fixed at one trigger period. Measured
        2026-09-10 at nine cameras: identical runs gave 5.07–5.57 ms of grab-loop
        slack headless against 2.87–4.34 ms with the GUI up, and that ~1.5–2 ms
        gap is this timer.

        Six cameras keep the historical 33 ms. Beyond that the period grows with
        the count so total repaint work per second stays roughly flat, capped at
        100 ms (10 Hz) because the preview's job is aiming and focus, not motion:
        capture never has priority taken from it for a picture nobody is scoring.
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
        output_dir = self._profile.output_dir if self._profile else ""
        self._hw_check_thread = HardwareCheckThread(output_dir)
        self._hw_check_thread.finished.connect(self._on_hardware_check_done)
        self._hw_check_thread.start()

    def _on_hardware_check_done(self, report):
        if report.warnings:
            msg = format_report(report)
            print(msg, flush=True)
            QMessageBox.warning(self, "Hardware Check", msg)

    def _on_profile_changed(self, profile: RigProfile):
        if self._state != State.IDLE or self._busy:
            return
        # close_all + open 6 cameras (+ .pfs load) is ~1-2 s of GigE round-trips;
        # run it off the UI thread so the window doesn't go "not responding".
        self._begin_busy("Switching cameras…")
        self._profile = profile

        def _switch():
            self._camera_mgr.close_all()
            return self._open_cameras_bg()

        self._cam_op = CallableWorker(_switch)
        self._cam_op.done.connect(self._on_profile_switch_done)
        self._cam_op.start()

    def _on_profile_switch_done(self, ok):
        self._apply_camera_open_result(ok)
        self._size_to_screen()
        self._end_busy()
        self._sidebar.set_status("IDLE", "#888")

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

    def _refresh_displays(self):
        self._display_tick = getattr(self, '_display_tick', 0) + 1
        brightness = self._sidebar.brightness
        contrast = self._sidebar.contrast

        for i, frame in enumerate(self._camera_mgr.latest_frames):
            if frame is not None:
                if brightness != 0 or contrast != 0:
                    f = frame.astype(np.float32)
                    if contrast != 0:
                        factor = (100 + contrast) / 100
                        np.subtract(f, 128, out=f)
                        np.multiply(f, factor, out=f)
                        np.add(f, 128, out=f)
                    if brightness != 0:
                        np.add(f, brightness, out=f)
                    np.clip(f, 0, 255, out=f)
                    frame = f.astype(np.uint8)
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
        try:
            lags = self._camera_mgr.delivery_lags
        except Exception:
            return
        if not lags:
            return
        worst = max(lags)
        if worst < 0.25:
            self.statusBar().showMessage(
                f"Capture healthy — keeping up with the trigger "
                f"(max lag {worst * 1000:.0f} ms)")
        elif worst < 1.0:
            self.statusBar().showMessage(
                f"CAPTURE FALLING BEHIND: cam{lags.index(worst) + 1} is "
                f"{worst:.2f} s behind real time and growing. Close other "
                f"applications.")
        else:
            self.statusBar().showMessage(
                f"CAPTURE {worst:.1f} s BEHIND REAL TIME (cam"
                f"{lags.index(worst) + 1}). Frames will be lost when the buffer "
                f"pool fills. Stop and investigate.")

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

    def _preflight_capacity(self) -> bool:
        """Check RAM, NVENC sessions and disk against the ACTUAL camera count.

        False means refuse to start. Cheap arithmetic plus a cached NVENC session
        probe, so it costs nothing per recording after the first.
        """
        p = self._profile
        realtime = bool(getattr(p, "realtime_encode", True))
        kick = realtime and bool(getattr(p, "realtime_kick", False))
        # Mirrors grab_thread: the kick-mode ring must outlast a frame's whole
        # journey (coordinator up to max_lag, then the encoder queue).
        ring_n = ((p.kick_max_lag + ENCODE_QUEUE_DEPTH + 64) if kick
                  else (ENCODE_QUEUE_DEPTH + 4))
        try:
            blocking, warnings = check_capacity(
                n_cams=self._camera_mgr.num_cameras,
                width=p.frame_width, height=p.frame_height,
                ring_n=ring_n, max_num_buffer=p.max_num_buffer,
                realtime=realtime, output_dir=self._sidebar.output_dir,
                fps=p.frame_rate)
        except Exception as e:
            # A broken preflight must never be what stops a recording.
            print(f"[acq] capacity preflight failed to run: {e}", flush=True)
            return True
        for w in warnings:
            print(f"[acq] capacity warning: {w}", flush=True)
        if blocking:
            print("[acq] REFUSING to start:\n  " + "\n  ".join(blocking), flush=True)
            QMessageBox.critical(
                self, "Cannot start",
                "\n\n".join(blocking)
                + ("\n\nWarnings:\n- " + "\n- ".join(warnings) if warnings else ""))
            return False
        if warnings:
            reply = QMessageBox.warning(
                self, "Proceed?", "\n\n".join(warnings) + "\n\nStart anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                return False
        return True

    def _start_acquisition(self, acq_type: str):
        # Refuse to start on top of a live or still-finalising acquisition. The
        # sidebar already disables the toggles while busy, so a user cannot
        # reach this — but any programmatic path that bypasses the widget starts
        # a SECOND acquisition whose state the machine then loses track of
        # (proven 2026-08-11: cameras kept streaming while _state read IDLE).
        if self._state != State.IDLE or self._busy:
            print(f"[acq] refusing start: state={self._state.value} "
                  f"busy={self._busy}", flush=True)
            self._sidebar.clear_toggles_silently()
            return

        # Capacity preflight. Every limit here scales linearly with camera
        # count, and each one currently fails SILENTLY — a camera dropping to
        # raw.bin because the driver's NVENC session cap was hit, a MemoryError
        # inside a grab thread, or a disk filling mid-session. Refuse up front
        # instead of half-recording.
        # A stim graph on a camera trigger pin injects extra rising edges into
        # ONE camera, so its block IDs advance faster and block-ID N stops
        # meaning the same instant everywhere — which frame_sync, alignment.py
        # and stim_trace all take as given. Apply and Test were gated; Record was
        # not, and Record is the one that produces data.
        if self._stim_window is not None:
            blocker = self._stim_window.record_blocker()
            if blocker:
                print(f"[acq] refusing start, stim workflow: {blocker}", flush=True)
                QMessageBox.critical(self, "Cannot record with this stim workflow",
                                     blocker)
                self._sidebar.reset_toggles()
                return

        if not self._preflight_capacity():
            # reset_toggles(), not the silent variant: we are at IDLE here, so
            # letting the signal fire is a genuine no-op for the state machine
            # (_on_record_toggle only acts when state == RECORDING) while still
            # reversing the thumb animation and re-enabling the sibling toggle.
            self._sidebar.reset_toggles()
            return

        self._config = self._build_config()
        self._acq_type = acq_type
        video_dir = self._config.video_dir(acq_type)

        # blockids/frametimes/aligned count as data too: a directory whose mp4s
        # were moved away for labelling still holds the metadata that makes them
        # interpretable, and without these patterns it reads as empty and gets
        # silently overwritten.
        has_data = video_dir.exists() and any(
            next(video_dir.rglob(pat), None) is not None
            for pat in ("*.mp4", "raw.bin", "stream.h264",
                        "blockids.npy", "frametimes.npy", "alignment.npz"))
        if has_data:
            reply = QMessageBox.question(
                self, "Overwrite?",
                f"Existing files found in:\n{video_dir}\n\nOverwrite?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply == QMessageBox.No:
                self._sidebar.reset_toggles()
                return

        self._video_dir = video_dir
        # Session-level warnings from a PREVIOUS run into this directory would
        # otherwise sit beside a clean recording and be believed months later.
        try:
            (video_dir / "WARNINGS.txt").unlink(missing_ok=True)
        except OSError:
            pass
        self._capture_warnings = []
        for cam in self._camera_names:
            cam_dir = self._video_dir / cam
            cam_dir.mkdir(parents=True, exist_ok=True)
            # Remove stale capture artifacts from a previous run in this dir —
            # a leftover raw_tail.bin would otherwise be appended to the NEW
            # recording's stream at stop.
            # WARNINGS.txt is swept too: it is the only durable trace of a
            # block-ID reconciliation, so a stale one left beside a clean
            # recording is exactly what someone would trust months later.
            for stale in ("raw.bin", "raw_tail.bin", "stream.h264",
                          "tail.h264", "encode_error.log", "WARNINGS.txt"):
                try:
                    (cam_dir / stale).unlink(missing_ok=True)
                except OSError:
                    pass

        raw_paths = [
            self._video_dir / cam / "raw.bin"
            for cam in self._camera_names
        ]

        # Calibration runs at a lower trigger rate (still sharp, plenty of distinct
        # board poses) with a smooth 1:1 preview; recording stays at the full rate
        # with a decimated preview to protect the disk-write loop.
        fps = self._config.rate_for(acq_type)
        self._acq_fps = fps
        display_every = 1 if acq_type == "calibration" else 10

        rt = self._config.realtime_encode
        kick = self._config.realtime_kick
        print(f"[acq] start_acquisition({acq_type}) fps={fps} realtime={rt} kick={kick}: switching cameras to trigger mode", flush=True)
        self._camera_mgr.start_acquisition(
            raw_paths, display_every=display_every,
            realtime=rt, width=self._config.frame_width,
            height=self._config.frame_height, quality=self._config.quality,
            fps=fps, realtime_kick=kick, kick_max_lag=self._config.kick_max_lag,
            # Calibration gets its own exposure/gain when the profile sets them;
            # a recording passes None, which RESTORES the .pfs values. Restoring
            # rather than re-deriving is what guarantees a long calibration
            # exposure can never leak into a 100 fps session, where it would
            # silently halve the frame rate.
            exposure_us=(self._config.calibration_exposure_us or None
                         if acq_type == "calibration" else None),
            gain_db=(self._config.calibration_gain_db
                     if acq_type == "calibration"
                     and self._config.calibration_gain_db >= 0 else None))

        # Put the right firmware on the board for this acquisition, flashing it
        # only if the board is not already carrying it. A CALIBRATION ALWAYS
        # gets the stimulation-free sketch: the board's config path calls
        # initStim() regardless of which acquisition asked for triggers, and a
        # calibration is the one acquisition performed with a PERSON inside the
        # arena holding the target. Returning here means a flash is in flight
        # and this method will be re-entered when it finishes.
        if not self._ensure_sketch_for(acq_type):
            return

        teensy = self._teensy_connection()
        if teensy is None:
            self._rollback_acquisition(
                f"Could not open serial port {self._profile.serial_port}.\n"
                "Close Arduino Serial Monitor / other apps holding the port and retry.")
            return
        print(f"[acq] sending start_triggers pins={self._profile.trigger_pins} fps={fps}", flush=True)
        if not teensy.start_triggers(self._profile.trigger_pins, fps):
            # The board never confirmed the config, even after a forced reset.
            # Recording now would produce a full-length session with no frames.
            self._rollback_acquisition(
                "The trigger board did not acknowledge the start command, so no "
                "triggers would be sent.\n\nCheck the Arduino is connected and "
                "running the Panopticon sketch, then retry.")
            return
        print(f"[acq] start_acquisition done", flush=True)

        self._sidebar.set_fields_editable(False)
        if acq_type == "calibration":
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
        after the fact.
        """
        if self._stim_window is None:
            return
        try:
            blocks, _edges = self._stim_window.get_workflow()
            if not blocks:
                return
            (self._video_dir / "stim_paradigm.json").write_text(
                json.dumps(self._stim_window.provenance(), indent=2))
            (self._video_dir / "stim_paradigm.ino").write_text(
                self._stim_window.firmware_source(), encoding="utf-8")
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
        the Arduino's own clock.
        """
        if self._stim_window is None:
            return
        secs = self._stim_window.end_time_s()
        if not secs or secs <= 0:
            return
        self._stim_end_timer = QTimer(self)
        self._stim_end_timer.setSingleShot(True)
        self._stim_end_timer.timeout.connect(self._on_stim_end)
        self._stim_end_timer.start(int(secs * 1000))
        print(f"[stim] auto-stop armed: {secs:g}s", flush=True)

    def _on_stim_end(self):
        self._stim_end_timer = None
        if self._state == State.RECORDING:
            print("[stim] end block reached — stopping recording", flush=True)
            self._sidebar.stop_record()

    def _cancel_stim_autostop(self):
        if self._stim_end_timer is not None:
            self._stim_end_timer.stop()
            self._stim_end_timer = None

    # ── shared trigger-board link ─────────────────────────────────────────────
    def _on_stim_applied(self, ino: str):
        """Remember the paradigm the editor just put on the board.

        Held for THIS SESSION only, deliberately. A launch always starts
        stimulation-free, so a paradigm never carries over from a previous
        session; but within a session this is what lets calibration and
        recording swap firmware automatically, so Apply is only needed when the
        paradigm itself changes.
        """
        from gui_app import stim_compiler
        self._session_stim_ino = ino
        try:
            from PyQt5.QtCore import QSettings
            QSettings("Salk", "Panopticon").setValue(
                "board_sketch_sha", stim_compiler.sketch_sha(ino))
        except Exception as e:
            print(f"[stim] could not record the applied sketch hash: {e}",
                  flush=True)
        print("[stim] paradigm applied and held for this session", flush=True)

    def _sketch_for(self, acq_type: str):
        """(source, label) of the firmware this acquisition must run under."""
        from gui_app import stim_compiler
        blank = stim_compiler.recording_only_sketch(
            self._profile.stim_safe_pins, self._profile.trigger_pins)
        if acq_type == "calibration" or not self._session_stim_ino:
            return blank, "recording-only"
        return self._session_stim_ino, "recording + stimulation"

    def _ensure_sketch_for(self, acq_type: str) -> bool:
        """Make the board carry the firmware this acquisition needs.

        Returns True to continue immediately, False to stop — either because a
        flash is now running (this method re-enters _start_acquisition when it
        finishes) or because the board could not be put into a known state.

        Flashing takes ~30 s, so it happens only when the board is not already
        carrying the right sketch. In the common order — calibrate, then set up
        stimulation, then record — the calibration finds the launch-time
        stimulation-free sketch already in place and costs nothing.
        """
        from gui_app import stim_compiler
        from PyQt5.QtCore import QSettings
        settings = QSettings("Salk", "Panopticon")
        try:
            want, label = self._sketch_for(acq_type)
            want_sha = stim_compiler.sketch_sha(want)
        except Exception as e:
            QMessageBox.critical(
                self, "Cannot prepare the trigger board",
                f"Could not build the firmware for this acquisition, so the "
                f"board cannot be put into a known state:\n\n{e}")
            self._sidebar.reset_toggles()
            return False

        if settings.value("board_sketch_sha", "", type=str) == want_sha:
            return True                      # already correct; no flash

        print(f"[acq] board needs the {label} sketch for a {acq_type}; flashing",
              flush=True)
        self.release_serial_port()           # arduino-cli needs the port alone
        self._begin_busy(f"Flashing {label} firmware…")
        port = self._profile.serial_port
        self._fw_op = CallableWorker(lambda: stim_compiler.upload(want, port))

        def done(result):
            self._end_busy()
            self._sidebar.set_status("IDLE", "#888")
            ok, msg = result if isinstance(result, tuple) else (False, str(result))
            if not ok:
                # Refusing is correct. The flash is what makes the board's
                # contents known, so a failed flash means they are not.
                print(f"[acq] firmware flash failed: {msg}", flush=True)
                settings.setValue("board_sketch_sha", "")
                if self._stim_window is not None:
                    self._stim_window.invalidate_upload(
                        "the flash failed, so the board's contents are unknown")
                QMessageBox.critical(
                    self, f"Cannot start the {acq_type}",
                    f"The trigger board could not be flashed with the {label} "
                    f"firmware, so what it is running is unknown. The "
                    f"{acq_type} has not been started.\n\nKey off the laser and "
                    f"check the board, then retry.\n\n{msg}")
                self._sidebar.reset_toggles()
                return
            settings.setValue("board_sketch_sha", want_sha)
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
            self._teensy_connection()        # retake the port before acquiring
            self._start_acquisition(acq_type)

        self._fw_op.done.connect(done)
        self._fw_op.start()
        return False

    def _ensure_clean_firmware(self):
        """Put the board back to the recording-only sketch at every launch.

        A paradigm lives in the Arduino's FLASH, so it survives closing the GUI,
        power cycles and USB unplugs — while the canvas comes up empty and
        nothing can read the firmware back over serial. That combination means a
        blank-looking editor over a fully armed board, and Record would then
        fire a paradigm nobody chose. Stim is therefore opt-in per session:
        unless it was Applied since this launch, the board carries no stim.

        Flashing takes ~30 s, so the SHA of whatever we last uploaded is kept in
        QSettings: if the board already has the recording-only sketch we skip.
        The slow path is only hit on the first launch after a session that used
        stim, which is exactly when it is worth paying for.

        Runs BEFORE _warm_serial: arduino-cli needs the port to itself.
        """
        from PyQt5.QtCore import QSettings
        self._settings = QSettings("Salk", "Panopticon")
        try:
            from gui_app import stim_compiler
            blank = stim_compiler.recording_only_sketch(
                self._profile.stim_safe_pins, self._profile.trigger_pins)
            self._pending_sha = stim_compiler.sketch_sha(blank)
        except Exception as e:
            print(f"[acq] could not build the recording-only sketch: {e}", flush=True)
            self._warm_serial()
            return

        if self._settings.value("board_sketch_sha", "", type=str) == self._pending_sha:
            print("[acq] board already carries the recording-only sketch "
                  "(no stim); skipping flash", flush=True)
            self._warm_serial()
            return

        print("[acq] board may carry a stim paradigm from a previous session — "
              "flashing the recording-only sketch", flush=True)
        self._begin_busy("Clearing stim firmware…")
        port = self._profile.serial_port
        self._fw_op = CallableWorker(
            lambda: __import__("gui_app.stim_compiler", fromlist=["upload"])
            .upload(blank, port))
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
            self._settings.setValue("board_sketch_sha", self._pending_sha)
            print("[acq] board flashed with the recording-only sketch; stim is "
                  "off until you Apply one", flush=True)
        else:
            # Do NOT record the SHA — we do not know what the board holds, and
            # claiming it is clean would be worse than saying nothing.
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
        recording #1 (observed 2026-07-27).

        Non-fatal, though: one attempt here, and if the board is not reachable
        yet the next _teensy_connection() call retries with the full count.
        """
        if self._teensy_connection(retries=1) is None:
            print(f"[acq] trigger board not reachable on {self._profile.serial_port} "
                  f"at startup; will retry on first use", flush=True)

    def _teensy_connection(self, retries: int = 10) -> TeensyController | None:
        """The one serial link to the trigger board, kept open for the session.

        Opening the port resets the Arduino, and during the reset + bootloader
        every pin floats — long enough for a connected laser to fire. Holding
        the connection open means that only happens at GUI launch (_warm_serial
        claims the port eagerly) and on upload, never at the start of a
        recording.
        """
        if self._teensy is not None and self._teensy.port != self._profile.serial_port:
            self._teensy.close()          # profile switched to a different port
            self._teensy = None
        if self._teensy is None:
            self._teensy = TeensyController(port=self._profile.serial_port)
        if not self._teensy.is_open:
            print(f"[acq] opening teensy on {self._profile.serial_port}", flush=True)
            if not self._teensy.open(retries=retries):
                return None
        return self._teensy

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

    def _rollback_acquisition(self, message: str):
        """Undo a half-started acquisition. The cameras are already grabbing in
        trigger mode, so without this they sit waiting for triggers forever.

        Stand the BOARD down first, before the cameras. This runs from the
        `start_triggers() == False` branch, which is precisely the case where the
        board may have consumed the config, begun triggering and run initStim()
        but failed to ack — so a stim paradigm (and the laser pin) can be live
        right now. Rolling back only the cameras leaves it running while the GUI
        returns to IDLE showing "did not acknowledge", which reads to the user as
        "nothing happened".
        """
        if self._teensy is not None:
            if not self._teensy.stop_triggers(self._profile.trigger_pins):
                message += ("\n\nWARNING: the trigger board did not accept the stop "
                            "command. It may still be triggering and any stim "
                            "paradigm may still be running. Power-cycle the board "
                            "and key off the laser before continuing.")
        self._camera_mgr.stop_acquisition()
        self._camera_mgr.resume_preview()
        self._sidebar.reset_toggles()
        self._on_camera_error(message)

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

    def _stop_coverage_hud(self):
        if self._coverage_worker is not None:
            self._coverage_worker.stop()
            self._coverage_worker.wait(2000)
            self._coverage_worker = None
        try:
            self._camera_mgr.set_keep_full(False)
        except Exception:
            pass

    def _stop_acquisition(self):
        self._cancel_stim_autostop()
        # Stop the triggers but KEEP the port open: reopening it would reset the
        # board at the start of the next recording and flash a connected laser.
        # This is the everyday stop — the one taken every session — so it is the
        # path where a swallowed failure matters most, not least.
        self._warn_if_not_stood_down(
            self._teensy.stop_triggers(self._profile.trigger_pins))

        self._stop_coverage_hud()
        if self._detector is not None and self._detector.codet_frames:
            self._save_codet_frames(self._detector.codet_frames)
        self._detector = None
        self._sidebar.hide_coverage()

        # Draining the encoders + reconfiguring 6 cameras back to preview is ~1 s
        # of blocking work; run it off the UI thread so the window stays live.
        self._begin_busy("Finishing…")

        def _finalize():
            # SAVE the captured data before restoring preview — restoring can
            # fail if a camera dropped off the bus mid-session, and saving first
            # guarantees the surviving cameras' recordings aren't lost.
            cam_results = self._camera_mgr.stop_acquisition()
            self._save_frametimes(cam_results)
            # Read thermals BEFORE resume_preview: DeviceTemperature starts
            # decaying the moment the load comes off, and these cameras have no
            # fan, so how hot they got is a property of the mounting that is
            # otherwise unrecoverable after the fact.
            try:
                self._config.camera_thermals = self._camera_mgr.thermals()
            except Exception as e:
                print(f"[acq] thermals unavailable: {e}", flush=True)
            self._config.save_metadata()
            self._write_stim_trace()   # needs blockids, so after _save_frametimes
            self._camera_mgr.resume_preview()

        self._cam_op = CallableWorker(_finalize)
        self._cam_op.done.connect(self._on_acquisition_finalized)
        self._cam_op.start()

    def _on_acquisition_finalized(self, _result):
        self._end_busy()
        # CallableWorker delivers a raised exception AS the result
        # (CallableWorker.run), and this slot used to ignore its argument. So if
        # _finalize raised — a full disk at np.save, anything inside
        # _router.stop() — camera_manager.stop_acquisition() never reached
        # `self._router = None`, the encoder threads never got their sentinel and
        # blocked forever holding NVENC sessions, and the GUI marched on to
        # ENCODING and remuxed an unflushed stream.h264, then deleted the source.
        # Silently. _apply_camera_open_result already does this check; the
        # pattern was understood and simply not applied here.
        # Capture-side problems (a retired camera, block-ID truncation) reach
        # the operator here or not at all: camera_manager drops the router right
        # after reading them.
        self._capture_warnings = list(getattr(self._camera_mgr, "last_warnings", []))
        if isinstance(_result, Exception):
            print(f"[acq] FINALIZE FAILED: {type(_result).__name__}: {_result}",
                  flush=True)
            try:
                self._camera_mgr.abandon()
            except Exception as e:
                print(f"[acq] abandon after failed finalize also failed: {e}",
                      flush=True)
            self._state = State.IDLE
            self._sidebar.set_status("IDLE", "#888888")
            self._sidebar.set_toggles_enabled(True)
            self._sidebar.reset_toggles()
            QMessageBox.critical(
                self, "Recording did not finish cleanly",
                f"Saving the recording failed:\n\n{type(_result).__name__}: "
                f"{_result}\n\nThe raw capture files are still in:\n"
                f"{self._video_dir}\n\nThey have NOT been encoded or deleted. Do "
                f"not start another recording into that directory.")
            return
        self._state = State.ENCODING
        self._sidebar.set_status("ENCODING", "#ffaa00")
        self._sidebar.set_toggles_enabled(False)

        self._encode_worker = EncodeWorker(
            self._video_dir,
            self._camera_names,
            self._acq_type,
            self._config.frame_width,
            self._config.frame_height,
            self._acq_fps,
            self._config.quality,
            self._config.date,
            self._config.session_id,
            max_parallel=self._config.encode_parallel,
            realtime=self._config.realtime_encode,
        )
        self._encode_worker.progress.connect(self._sidebar.show_progress)
        self._encode_worker.finished_all.connect(self._on_encoding_done)
        self._encode_worker.start()

    def _save_codet_frames(self, codet_frames: list[dict[int, int]]):
        """Save co-detection frame indices so 1_calibrate.py can skip full-video
        scanning and only process frames where the board was co-visible."""
        if not self._video_dir:
            return
        import json
        per_cam = {}
        for tick in codet_frames:
            for cam_idx, frame_n in tick.items():
                name = self._camera_names[cam_idx]
                per_cam.setdefault(name, set()).add(frame_n)
        out = {cam: sorted(fns) for cam, fns in per_cam.items()}
        path = self._video_dir / "codet_frames.json"
        with open(path, "w") as f:
            json.dump(out, f)
        print(f"[hud] saved {sum(len(v) for v in out.values())} co-detection "
              f"frame indices to {path.name}", flush=True)

    def _save_frametimes(self, cam_results: list[tuple[int, list[float], list[int]]]):
        counts = [len(ts) for _, ts, _ in cam_results if ts]
        if not counts:
            return
        min_frames = min(counts)
        realtime = self._config.realtime_encode

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
                frame_size = self._config.frame_width * self._config.frame_height
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

        problems = list(getattr(self, "_capture_warnings", []))
        if failed:
            problems.append(
                f"{len(failed)} camera(s) produced no usable video: "
                f"{', '.join(failed)}. Their source files were KEPT rather than "
                f"deleted — look for raw.bin / stream.h264 and encode_error.log "
                f"in those camera directories.")
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

        # Kick-out normally guarantees every camera encoded the same triggers,
        # so alignment is skipped. But a retirement or a truncation breaks that
        # guarantee mid-recording — those videos are NOT equal-length, and
        # skipping the one pass that would trim them to the common set leaves
        # them permanently disjoint. _start_alignment() no-ops when the videos
        # already agree, so running it here costs nothing in the normal case.
        needs_align = (not self._config.realtime_kick) or bool(problems)
        if self._config.realtime_encode and needs_align and self._start_alignment():
            return
        self._finish_to_idle()

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
        self._align_worker = AlignWorker(
            self._video_dir, self._acq_fps, self._config.quality,
            parallel=self._config.encode_parallel)
        self._align_worker.progress.connect(self._on_align_progress)
        self._align_worker.finished_align.connect(self._on_align_done)
        self._align_worker.start()
        return True

    def _on_align_progress(self, done: int, total: int, msg: str):
        self._sidebar.show_progress(done, total, label="Aligning")
        self.statusBar().showMessage(f"Aligning {done}/{total}: {msg}")

    def _on_align_done(self, summary: dict):
        self._sidebar.hide_progress()
        if summary.get("error"):
            self.statusBar().showMessage(
                f"Alignment failed: {summary['error']} — videos left as-is")
        elif summary.get("replaced"):
            # The align pass rewrote each camera's blockids.npy / frametimes.npy
            # to the common set and replaced the mp4s. stim_trace.csv was
            # written during _finalize, i.e. BEFORE that, so its frame column no
            # longer matches the videos. Regenerate it against the aligned data;
            # otherwise the file is silently offset in exactly the sessions that
            # had trouble, and every file involved still looks self-consistent.
            self._write_stim_trace()
            self.statusBar().showMessage(
                f"Aligned: {summary['common_frames']} synchronized frames per camera")
        elif summary.get("warnings"):
            self.statusBar().showMessage(
                "Alignment finished with warnings — see log; originals kept")
        self._finish_to_idle()

    def _finish_to_idle(self):
        self._sidebar.set_fields_editable(True)
        self._sidebar.reset_toggles()
        self._state = State.IDLE
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
        config = self._build_config()
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

        self._calib_worker = CalibrationWorker(
            config.session_dir, CALIBRATION_SCRIPT, board_cfg)
        self._calib_worker.status.connect(lambda s: self.statusBar().showMessage(s))
        self._calib_worker.finished.connect(self._on_calibration_done)
        self._calib_worker.start()

    def _on_calibration_done(self, success: bool, msg: str):
        self._sidebar.set_toggles_enabled(True)
        self._sidebar.set_solve_enabled(True)
        self._sidebar.set_status("IDLE", "#888")
        if success:
            config = self._build_config()
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
        from PIL import Image
        cfg = self._build_config()
        out_dir = cfg.session_dir / "snapshots" / f"{cfg.date}_{datetime.now().strftime('%H%M%S')}"
        out_dir.mkdir(parents=True, exist_ok=True)
        frames = self._camera_mgr.snapshots
        saved = 0
        for i, frame in enumerate(frames):
            if frame is None:
                continue
            cam = self._camera_names[i] if i < len(self._camera_names) else f"cam{i+1}"
            try:
                Image.fromarray(frame).save(out_dir / f"{cam}.png")
                saved += 1
            except Exception as e:
                print(f"[snapshot] {cam} failed: {e}", flush=True)
        self.statusBar().showMessage(
            f"Snapshot: saved {saved}/{len(frames)} cameras → {out_dir}")

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
                get_safe_pins=lambda: self._profile.stim_safe_pins,
                get_trigger_pins=lambda: self._profile.trigger_pins,
                get_serial=self._teensy_connection,
                release_serial=self.release_serial_port,
                on_applied=self._on_stim_applied,
                parent=self,
            )
        self._stim_window.show()
        self._stim_window.raise_()

    def _on_camera_error(self, msg: str):
        QMessageBox.critical(self, "Error", msg)

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
        if self._stim_window is not None and self._stim_window.is_uploading():
            return True
        return any(w is not None and w.isRunning() for w in
                   (self._encode_worker, self._align_worker, self._calib_worker,
                    self._cam_op, self._coverage_worker, self._fw_op))

    def closeEvent(self, event):
        # Quitting mid-session can't be finalized — confirm, then ABANDON the
        # half-baked data rather than blocking the close on encode/align/solve
        # workers (which is what made it freeze on "quit anyway").
        busy = self._state != State.IDLE or self._busy or self._workers_running()
        if busy:
            reply = QMessageBox.question(
                self, "Work in progress",
                f"State is {self._state.value}. Quit anyway?\n\n"
                "The current session is not finished — its incomplete data "
                "will be DELETED.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                event.ignore()
                return

        self._display_timer.stop()
        self._stop_coverage_hud()
        # Always stand the board down, not just mid-acquisition: stop_triggers
        # drives the stim pins LOW as well as the camera pins, so quitting can
        # never leave a paradigm — or a laser — running.
        try:
            if self._teensy is not None:
                if self._teensy.is_open:
                    # Warn BEFORE the window goes, while there is still something
                    # to show the dialog on. `is_open` is not proof of anything:
                    # pyserial keeps it True after the USB device disappears, so
                    # an unplugged cable looks healthy right up until the write.
                    self._warn_if_not_stood_down(
                        self._teensy.stop_triggers(self._profile.trigger_pins))
                self._teensy.close()
        except Exception as e:
            print(f"[quit] standing the board down failed: {e}", flush=True)

        if busy:
            self._abandon_and_cleanup()
        else:
            self._camera_mgr.close_all()
        event.accept()

    def _abandon_and_cleanup(self):
        """Kill in-flight ffmpeg/solve subprocesses, tear down capture without
        draining, and delete the incomplete session's data — so 'quit anyway'
        returns immediately instead of waiting on workers."""
        # Only the actively-written session's data is incomplete; a Solve
        # (state IDLE) operates on already-complete videos, so don't delete those.
        delete_data = self._state in (
            State.RECORDING, State.CALIBRATING, State.ENCODING, State.ALIGNING)
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
        # excepthook cannot save us. Killing the child processes above is what
        # lets these waits actually return.
        for w in (self._cam_op, self._encode_worker, self._align_worker,
                  self._calib_worker, self._coverage_worker):
            if w is not None and w.isRunning():
                w.wait(3000)
        # A flash gets far longer, because it is not being killed above and
        # abandoning the QThread under a live avrdude is the thing this is
        # avoiding. arduino-cli's compile+upload is ~30 s.
        if self._fw_op is not None and self._fw_op.isRunning():
            print("[quit] waiting for the firmware flash to finish before "
                  "tearing down", flush=True)
            self._fw_op.wait(60000)
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
        if delete_data and self._video_dir and Path(self._video_dir).exists():
            shutil.rmtree(self._video_dir, ignore_errors=True)
            print(f"[quit] deleted incomplete session data: {self._video_dir}", flush=True)
