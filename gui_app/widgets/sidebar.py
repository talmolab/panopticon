"""Sidebar widget with session parameters, toggle switches, progress bar, and status."""
from datetime import datetime
from pathlib import Path
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLineEdit, QLabel,
    QProgressBar, QFrame, QPushButton, QFileDialog, QSlider, QComboBox,
)
from PyQt5.QtCore import Qt, pyqtSignal, QSettings
from PyQt5.QtGui import QFont, QColor

from gui_app.widgets.toggle_switch import ToggleSwitch
from gui_app.widgets.coverage_graph import CoverageGraphWidget
from gui_app.session_config import RigProfile, REPO_ROOT

# Per-machine UI state. The profiles themselves are shared with the 3dface rig
# via git, so which one is "default" can't live in the repo — it's a property of
# the machine, not the codebase.
_SETTINGS = QSettings("Salk", "Panopticon")


class SidebarWidget(QWidget):
    calibrate_toggled = pyqtSignal(bool)
    record_toggled = pyqtSignal(bool)
    run_calibration_clicked = pyqtSignal()
    snapshot_clicked = pyqtSignal()
    stimulation_clicked = pyqtSignal()
    profile_changed = pyqtSignal(object)

    def __init__(self, default_output_dir: str = str(REPO_ROOT / "data"), parent=None):
        super().__init__(parent)
        self.setFixedWidth(260)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)

        title = QLabel("Metadata")
        title.setFont(QFont("Segoe UI", 13, QFont.Bold))
        title.setStyleSheet("color: #dcdcdc; border: none;")
        layout.addWidget(title)

        # Profile selector
        self._profile_combo = QComboBox()
        self._profile_combo.setStyleSheet(
            "QComboBox { background: #1a1a2e; color: #dcdcdc; border: 1px solid #444; "
            "border-radius: 3px; padding: 4px 6px; font-size: 11px; }"
            "QComboBox::drop-down { border: none; }"
            "QComboBox QAbstractItemView { background: #1a1a2e; color: #dcdcdc; selection-background-color: #5078c8; }"
        )
        self._profiles: list[RigProfile] = []
        for path in RigProfile.list_profiles():
            profile = RigProfile.load(path)
            self._profiles.append(profile)
            self._profile_combo.addItem(profile.name)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        layout.addWidget(self._profile_combo)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color: #444;")
        layout.addWidget(sep)

        self._output_dir = default_output_dir
        self._dir_button = QPushButton(self._truncate_path(self._output_dir))
        self._dir_button.setToolTip(self._output_dir)
        self._dir_button.setStyleSheet(
            "QPushButton { background: #1a1a2e; color: #88aadd; border: 1px solid #444; "
            "border-radius: 3px; padding: 5px 8px; font-size: 10px; text-align: left; }"
            "QPushButton:hover { border-color: #5078c8; background: #222244; }"
        )
        self._dir_button.clicked.connect(self._pick_output_dir)
        layout.addWidget(self._dir_button)

        layout.addSpacing(4)

        form = QFormLayout()
        form.setSpacing(6)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self._fields: dict[str, QLineEdit] = {}
        defaults = [
            ("date", datetime.now().strftime("%Y%m%d")),
            ("mouse_1", ""),
            ("mouse_2", ""),
            ("assay", "open_field"),
            ("experimenter", "IT"),
            ("cohort", ""),
            ("cage", ""),
            ("notes", ""),
        ]
        for name, default in defaults:
            field = QLineEdit(default)
            field.setStyleSheet(
                "QLineEdit { background: #1a1a2e; color: #dcdcdc; border: 1px solid #444; "
                "border-radius: 3px; padding: 4px 6px; font-size: 11px; }"
                "QLineEdit:focus { border-color: #5078c8; }"
                "QLineEdit:read-only { background: #111122; color: #888; }"
            )
            label_text = name.replace("_", " ").title()
            label = QLabel(label_text)
            label.setStyleSheet("color: #aaa; font-size: 11px; border: none;")
            form.addRow(label, field)
            self._fields[name] = field

        layout.addLayout(form)
        layout.addSpacing(12)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.HLine)
        sep2.setStyleSheet("color: #444;")
        layout.addWidget(sep2)
        layout.addSpacing(4)

        acq_label = QLabel("Acquisition")
        acq_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        acq_label.setStyleSheet("color: #dcdcdc; border: none;")
        layout.addWidget(acq_label)

        # Enablement of the acquisition controls is derived, never stored on
        # the widgets: three gates combine and each caller owns exactly one.
        # `_busy` is the temporary overlay of a blocking background op;
        # `_toggles_gate` is the state machine's ENCODING/ALIGNING/solve gate
        # (set_toggles_enabled); `_solve_running` is the Solve button's own
        # gate (set_solve_enabled). Sibling exclusion comes from the toggles'
        # checked state. Every setter recomputes from all of them, so releasing
        # one gate cannot enable a control another gate still holds closed.
        self._busy = False
        self._toggles_gate = True
        self._solve_running = False

        self._calibrate_toggle = ToggleSwitch("Calibrate", QColor(66, 133, 244))
        self._record_toggle = ToggleSwitch("Record", QColor(234, 67, 53))
        self._calibrate_toggle.toggled.connect(self._on_calibrate)
        self._record_toggle.toggled.connect(self._on_record)

        calib_row = QHBoxLayout()
        calib_row.setSpacing(6)
        calib_row.addWidget(self._calibrate_toggle, stretch=1)
        self._run_calib_btn = QPushButton("Solve")
        self._run_calib_btn.setFixedSize(50, 28)
        self._run_calib_btn.setToolTip(
            "Solve the camera calibration from the recorded calibration videos")
        self._run_calib_btn.setStyleSheet(
            "QPushButton { background: #2a2a4a; color: #88aadd; border: 1px solid #444; "
            "border-radius: 3px; font-size: 10px; }"
            "QPushButton:hover { background: #333366; border-color: #5078c8; }"
            "QPushButton:disabled { color: #555; border-color: #333; }"
        )
        self._run_calib_btn.clicked.connect(self.run_calibration_clicked.emit)
        calib_row.addWidget(self._run_calib_btn)
        layout.addLayout(calib_row)

        layout.addWidget(self._record_toggle)

        self._snapshot_btn = QPushButton("Snapshot")
        self._snapshot_btn.setToolTip("Save a full-resolution still from every camera to the session's snapshots/ folder")
        self._snapshot_btn.setStyleSheet(
            "QPushButton { background: #2a2a4a; color: #88aadd; border: 1px solid #444; "
            "border-radius: 3px; padding: 5px 8px; font-size: 11px; }"
            "QPushButton:hover { background: #333366; border-color: #5078c8; }"
            "QPushButton:disabled { color: #555; border-color: #333; }"
        )
        self._snapshot_btn.clicked.connect(self.snapshot_clicked.emit)
        layout.addWidget(self._snapshot_btn)

        self._stim_btn = QPushButton("Stimulation")
        self._stim_btn.setToolTip("Open the stimulus paradigm editor")
        self._stim_btn.setStyleSheet(
            "QPushButton { background: #2a1a3a; color: #cc88ee; border: 1px solid #553366; "
            "border-radius: 3px; padding: 6px 8px; font-size: 11px; font-weight: bold; }"
            "QPushButton:hover { background: #3a2050; border-color: #8855aa; }"
        )
        self._stim_btn.clicked.connect(self.stimulation_clicked.emit)
        layout.addWidget(self._stim_btn)

        # Live ChArUco coverage graph — shown only during calibration.
        self._coverage_graph = CoverageGraphWidget()
        self._coverage_graph.setVisible(False)
        layout.addWidget(self._coverage_graph)

        layout.addSpacing(12)

        sep3 = QFrame()
        sep3.setFrameShape(QFrame.HLine)
        sep3.setStyleSheet("color: #444;")
        layout.addWidget(sep3)
        layout.addSpacing(4)

        display_label = QLabel("Display")
        display_label.setFont(QFont("Segoe UI", 11, QFont.Bold))
        display_label.setStyleSheet("color: #dcdcdc; border: none;")
        layout.addWidget(display_label)

        slider_style = (
            "QSlider::groove:horizontal { background: #1a1a2e; height: 4px; border-radius: 2px; }"
            "QSlider::handle:horizontal { background: #5078c8; width: 12px; margin: -4px 0; border-radius: 6px; }"
            "QSlider::sub-page:horizontal { background: #5078c8; border-radius: 2px; }"
        )

        bright_row = QHBoxLayout()
        bright_lbl = QLabel("Brightness")
        bright_lbl.setStyleSheet("color: #aaa; font-size: 10px; border: none;")
        bright_lbl.setFixedWidth(65)
        self._brightness_slider = QSlider(Qt.Horizontal)
        self._brightness_slider.setRange(-100, 100)
        self._brightness_slider.setValue(0)
        self._brightness_slider.setStyleSheet(slider_style)
        bright_row.addWidget(bright_lbl)
        bright_row.addWidget(self._brightness_slider)
        layout.addLayout(bright_row)

        contrast_row = QHBoxLayout()
        contrast_lbl = QLabel("Contrast")
        contrast_lbl.setStyleSheet("color: #aaa; font-size: 10px; border: none;")
        contrast_lbl.setFixedWidth(65)
        self._contrast_slider = QSlider(Qt.Horizontal)
        self._contrast_slider.setRange(-100, 100)
        self._contrast_slider.setValue(0)
        self._contrast_slider.setStyleSheet(slider_style)
        contrast_row.addWidget(contrast_lbl)
        contrast_row.addWidget(self._contrast_slider)
        layout.addLayout(contrast_row)

        layout.addSpacing(8)

        self._progress = QProgressBar()
        self._progress.setStyleSheet(
            "QProgressBar { background: #1a1a2e; border: 1px solid #444; border-radius: 3px; "
            "text-align: center; color: #dcdcdc; font-size: 10px; height: 18px; }"
            "QProgressBar::chunk { background: #ffaa00; border-radius: 2px; }"
        )
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        layout.addStretch()

        self._status = QLabel("IDLE")
        self._status.setFont(QFont("Segoe UI", 10, QFont.Bold))
        self._status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._status.setStyleSheet("color: #888; border: none; padding: 4px;")
        layout.addWidget(self._status)

    def _truncate_path(self, path: str, max_len: int = 32) -> str:
        if len(path) <= max_len:
            return path
        parts = Path(path).parts
        return str(Path(parts[0], "...", *parts[-2:]))

    def _pick_output_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select Output Directory", self._output_dir)
        if d:
            self._output_dir = d
            self._dir_button.setText(self._truncate_path(d))
            self._dir_button.setToolTip(d)

    @property
    def output_dir(self) -> str:
        return self._output_dir

    def _on_calibrate(self, checked):
        self._apply_enablement()
        self.calibrate_toggled.emit(checked)

    def _on_record(self, checked):
        self._apply_enablement()
        self.record_toggled.emit(checked)

    def _apply_enablement(self):
        """Recompute the enabled state of Calibrate, Record and Solve from the
        gates. Calibrate and Record are mutually exclusive: one being checked
        disables the other, so a second acquisition cannot be started on top
        of a live one. A busy overlay disables all three; the toggles gate
        disables both toggles; the solve gate disables Solve alone."""
        calibrate_on = self._calibrate_toggle.isChecked()
        record_on = self._record_toggle.isChecked()
        toggles_open = self._toggles_gate and not self._busy
        self._calibrate_toggle.setEnabled(toggles_open and not record_on)
        self._record_toggle.setEnabled(toggles_open and not calibrate_on)
        self._run_calib_btn.setEnabled(not self._busy and not self._solve_running)

    def _on_profile_changed(self, index: int):
        if 0 <= index < len(self._profiles):
            profile = self._profiles[index]
            self._apply_profile_dir(profile)
            _SETTINGS.setValue("profile_name", profile.name)
            self.profile_changed.emit(profile)

    def _apply_profile_dir(self, profile: RigProfile):
        if profile.output_dir:
            self._output_dir = profile.output_dir
            self._dir_button.setText(self._truncate_path(self._output_dir))
            self._dir_button.setToolTip(self._output_dir)

    def select_profile(self, name: str) -> bool:
        """Select a profile by name without re-emitting profile_changed.

        Used at startup to restore the last one used on this machine.
        """
        for i, profile in enumerate(self._profiles):
            if profile.name == name:
                self._profile_combo.blockSignals(True)
                self._profile_combo.setCurrentIndex(i)
                self._profile_combo.blockSignals(False)
                self._apply_profile_dir(profile)
                return True
        return False

    @staticmethod
    def remembered_profile() -> str:
        """Name of the profile last selected on this machine ("" if none)."""
        return _SETTINGS.value("profile_name", "", type=str)

    @property
    def current_profile(self) -> RigProfile:
        idx = self._profile_combo.currentIndex()
        if 0 <= idx < len(self._profiles):
            return self._profiles[idx]
        return RigProfile()

    @property
    def brightness(self) -> int:
        return self._brightness_slider.value()

    @property
    def contrast(self) -> int:
        return self._contrast_slider.value()

    def get_field_values(self) -> dict:
        return {k: v.text() for k, v in self._fields.items()}

    def set_fields_editable(self, editable: bool):
        for field in self._fields.values():
            field.setReadOnly(not editable)
        self._dir_button.setEnabled(editable)
        # Switching profiles mid-acquisition is ignored by the main window but
        # would still move the dropdown, desyncing it from the active profile.
        self._profile_combo.setEnabled(editable)

    def set_busy(self, busy: bool):
        """Overlay for a blocking background op (camera switch, finishing,
        firmware flash): disables the acquisition controls while True and
        RESTORES them when False.

        Restoring means recomputing from the other gates, not enabling
        everything: a firmware flash runs between the click and the state
        change, so set_busy(False) fires while a toggle is already checked and
        must leave the sibling disabled, and a profile switch during a solve
        must leave Solve disabled. The Stimulation button is deliberately
        left alone so the editor can still be opened, and the status label
        stays legible so the user sees progress."""
        self._busy = busy
        for w in (self._profile_combo, self._dir_button, self._snapshot_btn):
            w.setEnabled(not busy)
        for f in self._fields.values():
            f.setEnabled(not busy)
        self._apply_enablement()

    def set_status(self, text: str, color: str):
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {color}; border: none; padding: 4px;")

    def show_progress(self, current: int, total: int, label: str = "Encoding"):
        self._progress.setVisible(True)
        self._progress.setMaximum(total)
        self._progress.setValue(current)
        self._progress.setFormat(f"{label} {current}/{total}")

    def hide_progress(self):
        self._progress.setVisible(False)
        self._progress.setValue(0)

    def set_toggles_enabled(self, enabled: bool):
        """Gate both toggles for the ENCODING/ALIGNING/solve phases. The gate
        survives a set_busy cycle; reset_toggles() reopens it."""
        self._toggles_gate = enabled
        self._apply_enablement()

    def set_solve_enabled(self, enabled: bool):
        """Enable/disable the Solve button independently of the toggles.

        A solve runs 4-5 minutes without changing the app state, so it needs its
        own gate: a second click would rebind the worker and drop the only
        reference to a running QThread, which is an immediate qFatal. The gate
        survives a set_busy cycle so a profile switch mid-solve cannot reopen it.
        """
        self._solve_running = not enabled
        self._apply_enablement()

    def clear_toggles_silently(self):
        """Force both toggles off WITHOUT emitting — for refusing a start.

        Emitting here would re-enter the stop path we are already guarding.

        But blockSignals also suppresses the widget's OWN animation, because
        ToggleSwitch wires `toggled -> _on_toggled` in its constructor
        (toggle_switch.py:18). Unchecking alone therefore leaves the thumb
        painted fully ON while isChecked() is False — an arm indicator that lies,
        on a rig with a laser. Drive the animation by hand instead.

        Deliberately does NOT touch enabled state: the only caller is the
        non-IDLE guard, i.e. a real acquisition is in progress, and the state
        machine owns which toggles are available then. The refuse-at-IDLE path
        uses reset_toggles() instead, which restores enablement properly.
        """
        for t in (self._calibrate_toggle, self._record_toggle):
            t.blockSignals(True)
            t.setChecked(False)
            t.blockSignals(False)
            t._on_toggled(False)

    def stop_record(self):
        """Flip Record off programmatically — emits record_toggled like a click."""
        self._record_toggle.setChecked(False)

    def reset_toggles(self):
        """Return both toggles to off and reopen the toggles gate: the IDLE
        entry point after an acquisition, an alignment or a refused start.
        Unchecking emits like a click, so a live stop path still runs."""
        self._calibrate_toggle.setChecked(False)
        self._record_toggle.setChecked(False)
        self._toggles_gate = True
        self._apply_enablement()

    # --- calibration coverage graph ---
    def setup_coverage(self, n_cams: int):
        self._coverage_graph.setup(n_cams)

    def show_coverage(self):
        self._coverage_graph.setVisible(True)

    def hide_coverage(self):
        self._coverage_graph.setVisible(False)

    def update_coverage(self, detector):
        self._coverage_graph.update_from(detector)
