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
from gui_app import session_config
from gui_app.session_config import RigProfile, ProfileError, REPO_ROOT

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
        # One malformed YAML must not abort launch: the bad file is skipped
        # with a warning and the good profiles stay usable. Every load failure
        # is a ProfileError naming the file and the key, so nothing broader is
        # caught and a programming error still surfaces. The warnings are
        # collected rather than shown here because no window exists yet; the
        # main window reads profile_warnings once it is up.
        self._profiles: list[RigProfile] = []
        self._profile_warnings: list[str] = []
        for path in RigProfile.list_profiles():
            try:
                profile = RigProfile.load(path)
            except ProfileError as e:
                msg = f"skipping profile {path.name}: {e}"
                print(f"[profile] {msg}", flush=True)
                self._profile_warnings.append(msg)
                continue
            self._profiles.append(profile)
            self._profile_combo.addItem(profile.name)
        if not self._profiles:
            # Read at call time so it names the directory list_profiles used.
            msg = (f"No rig profile could be loaded from {session_config.PROFILES_DIR}. "
                   f"Add a <name>.yaml there (profiles/3dpose.yaml is a "
                   f"complete example) and restart.")
            print(f"[profile] {msg}", flush=True)
            self._profile_warnings.append(msg)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        layout.addWidget(self._profile_combo)
        # The fields are built below; the first profile's values are applied
        # once they exist so the form is prefilled even before the main window
        # selects the remembered profile.

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

        # Experimenter and assay have no built-in default: they are rig
        # properties and come from the profile's metadata_defaults, so a
        # shared codebase does not ship one operator's initials. The date is
        # refreshed on read while the operator has not typed into it, because
        # a GUI left open past midnight would otherwise file the session
        # under the previous day.
        self._fields: dict[str, QLineEdit] = {}
        self._user_edited: set[str] = set()
        defaults = [
            ("date", self._today()),
            ("mouse_1", ""),
            ("mouse_2", ""),
            ("assay", ""),
            ("experimenter", ""),
            ("cohort", ""),
            ("cage", ""),
            ("notes", ""),
        ]
        for name, default in defaults:
            field = QLineEdit(default)
            # textEdited fires for keyboard input only, never for setText, so
            # it separates operator intent from programmatic prefill.
            field.textEdited.connect(lambda _text, n=name: self._user_edited.add(n))
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
        if self._profiles:
            self._apply_profile(self._profiles[0])

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
        # Which toggle most recently went on. A refused start is delivered
        # synchronously inside that toggle's own emission, so this names the
        # refused toggle for callers that do not pass the kind themselves.
        self._last_armed: str | None = None

        self._calibrate_toggle = ToggleSwitch("Calibrate", QColor(66, 133, 244))
        self._record_toggle = ToggleSwitch("Record", QColor(234, 67, 53))
        self._calibrate_toggle.toggled.connect(self._on_calibrate)
        self._record_toggle.toggled.connect(self._on_record)

        calib_row = QHBoxLayout()
        calib_row.setSpacing(6)
        calib_row.addWidget(self._calibrate_toggle, stretch=1)
        self._run_calib_btn = QPushButton("Solve")
        self._run_calib_btn.setFixedSize(50, 28)
        self._run_calib_btn.setToolTip(self._SOLVE_TIP)
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
        # Tooltips and enabled state come from the gates from the first paint.
        self._apply_enablement()

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
        if checked:
            self._last_armed = "calibrate"
        self._apply_enablement()
        self.calibrate_toggled.emit(checked)

    def _on_record(self, checked):
        if checked:
            self._last_armed = "record"
        self._apply_enablement()
        self.record_toggled.emit(checked)

    def _toggle_for(self, kind: str) -> ToggleSwitch:
        if kind == "calibrate":
            return self._calibrate_toggle
        if kind == "record":
            return self._record_toggle
        raise ValueError(f"unknown toggle kind {kind!r}; expected 'calibrate' or 'record'")

    _SOLVE_TIP = "Solve the camera calibration from the recorded calibration videos"
    _CALIBRATE_TIP = "Start or stop a calibration recording"
    _RECORD_TIP = "Start or stop a session recording"
    _BUSY_TIP = "Disabled during a background operation"
    _GATE_TIP = "Disabled while encoding, aligning or solving"

    def _apply_enablement(self):
        """Recompute the enabled state of Calibrate, Record and Solve from the
        gates. Calibrate and Record are mutually exclusive: one being checked
        disables the other, so a second acquisition cannot be started on top
        of a live one. A busy overlay disables all three; the toggles gate
        disables both toggles; the solve gate disables Solve alone.

        Each disabled control carries a tooltip naming the gate that holds
        it, so a dead click can be explained by hovering instead of being
        read as a hang."""
        calibrate_on = self._calibrate_toggle.isChecked()
        record_on = self._record_toggle.isChecked()

        def toggle_reason(sibling_on: bool, sibling_name: str) -> str:
            if self._busy:
                return self._BUSY_TIP
            if not self._toggles_gate:
                return self._GATE_TIP
            if sibling_on:
                return f"Disabled while {sibling_name} is on"
            return ""

        for toggle, sibling_on, sibling_name, tip in (
                (self._calibrate_toggle, record_on, "Record", self._CALIBRATE_TIP),
                (self._record_toggle, calibrate_on, "Calibrate", self._RECORD_TIP)):
            reason = toggle_reason(sibling_on, sibling_name)
            toggle.setEnabled(not reason)
            toggle.setToolTip(reason or tip)

        if self._busy:
            solve_reason = self._BUSY_TIP
        elif self._solve_running:
            solve_reason = "Disabled while a solve is running"
        else:
            solve_reason = ""
        self._run_calib_btn.setEnabled(not solve_reason)
        self._run_calib_btn.setToolTip(solve_reason or self._SOLVE_TIP)

    def _on_profile_changed(self, index: int):
        if 0 <= index < len(self._profiles):
            profile = self._profiles[index]
            self._apply_profile(profile)
            _SETTINGS.setValue("profile_name", profile.name)
            self.profile_changed.emit(profile)

    def _apply_profile(self, profile: RigProfile):
        """Take the output directory and the metadata defaults from a profile.

        A metadata field the operator has typed into is left alone: the
        profile supplies defaults, not overrides, and switching profiles must
        not discard what was entered for this session.
        """
        if profile.output_dir:
            self._output_dir = profile.output_dir
            self._dir_button.setText(self._truncate_path(self._output_dir))
            self._dir_button.setToolTip(self._output_dir)
        for key, value in profile.metadata_defaults.items():
            field = self._fields.get(key)
            if field is not None and key not in self._user_edited:
                field.setText("" if value is None else str(value))

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y%m%d")

    def refresh_date(self):
        """Set the date field to today unless the operator has typed into it.
        Called on every read of the fields so a session started after
        midnight is filed under the day it starts."""
        if "date" not in self._user_edited:
            self._fields["date"].setText(self._today())

    def select_profile(self, name: str) -> bool:
        """Select a profile by name without re-emitting profile_changed.

        Used at startup to restore the last one used on this machine.
        """
        for i, profile in enumerate(self._profiles):
            if profile.name == name:
                self._profile_combo.blockSignals(True)
                self._profile_combo.setCurrentIndex(i)
                self._profile_combo.blockSignals(False)
                self._apply_profile(profile)
                return True
        return False

    @property
    def profile_warnings(self) -> list[str]:
        """Messages about profiles that failed to load at construction, one
        per skipped file, plus one naming the profiles directory when none
        loaded. Empty when every profile loaded. The main window shows them
        once after the window is up."""
        return list(self._profile_warnings)

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
        self.refresh_date()
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

    def clear_toggle_silently(self, kind: str):
        """Force ONE toggle off without emitting: the refuse-at-non-IDLE path.

        ``kind`` is the acquisition that was refused ('calibrate' or
        'record'). Only that toggle is cleared, because the other one belongs
        to the acquisition that is still running: painting it off would show
        an idle rig while the cameras stream, and leave the operator nothing to
        click to stop it. Emitting is suppressed because the toggled signal is
        the start/stop path this call is refusing from inside.

        The thumb still animates off: ToggleSwitch drives it from
        checkStateSet, which setChecked calls regardless of blockSignals, so
        the painted state cannot disagree with isChecked() on a rig with a
        laser.

        Enablement is recomputed afterwards: the refused click disabled the
        acquiring toggle through sibling exclusion, and clearing the refused
        toggle is what re-enables it.
        """
        t = self._toggle_for(kind)
        t.blockSignals(True)
        t.setChecked(False)
        t.blockSignals(False)
        self._apply_enablement()

    def clear_toggles_silently(self, kind: str | None = None):
        """Deprecated alias of clear_toggle_silently, kept for one release.

        Without ``kind`` the most recently armed toggle is cleared, which is
        the refused one when called from inside its own emission; if nothing
        has been armed, both toggles are cleared.
        """
        kind = kind or self._last_armed
        if kind is None:
            for k in ("calibrate", "record"):
                self.clear_toggle_silently(k)
            return
        self.clear_toggle_silently(kind)

    def stop_record(self):
        """Flip Record off programmatically, emitting record_toggled like a
        click. When the toggle is already off (a silent clear painted it off
        while the recording continued) the signal is emitted directly, so an
        automatic stop still reaches the recording."""
        if self._record_toggle.isChecked():
            self._record_toggle.setChecked(False)
        else:
            self.record_toggled.emit(False)

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
