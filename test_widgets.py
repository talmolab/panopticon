"""Offscreen tests for the sidebar and camera-grid widgets.

The sidebar owns which acquisition control the operator can reach: Calibrate
and Record exclude each other, a busy overlay hides all of them for the
duration of a blocking operation, and the state machine gates them during
encoding, alignment and a solve. A wrong enabled state is not cosmetic: an
enabled sibling during a live recording starts a second acquisition the state
machine cannot track, and a disabled Record with nothing to re-enable it
locks the operator out of stopping a recording on a rig with a laser.

The camera grid is the operator's only view of aim, focus and framing, so
its geometry has to be honest: no stale layout stretch after a profile
switch, an aspect-true letterboxed preview, and no conversion work spent on
panes that are hidden.

Everything runs offscreen against stub data: no cameras, no serial port, and
the sidebar's per-machine QSettings are redirected to a temp file so a test
never writes the operator's remembered profile.

    set QT_QPA_PLATFORM=offscreen
    python test_widgets.py
"""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from PyQt5.QtCore import QPoint, QSettings, Qt
from PyQt5.QtGui import QImage
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

import gui_app.session_config as session_config
from gui_app.widgets import sidebar as sidebar_module
from gui_app.widgets.sidebar import SidebarWidget
from gui_app.widgets.camera_grid import CameraGridWidget
from gui_app.widgets.coverage_graph import CoverageGraphWidget
from gui_app.widgets.toggle_switch import ToggleSwitch

_TMP = tempfile.mkdtemp(prefix="panopticon_widgets_")
# Redirect the per-machine settings so no test touches the real registry.
sidebar_module._SETTINGS = QSettings(str(Path(_TMP) / "ui.ini"), QSettings.IniFormat)

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        _failures.append(name)


def make_sidebar() -> SidebarWidget:
    sb = SidebarWidget(default_output_dir=_TMP)
    sb.show()
    app.processEvents()
    return sb


# --------------------------------------------------------------------------
# A5-01: set_busy(False) restores the gates instead of enabling everything.
# --------------------------------------------------------------------------
def test_exclusion_survives_busy_cycle():
    sb = make_sidebar()
    sb._calibrate_toggle.setChecked(True)
    check("calibrate on disables record", not sb._record_toggle.isEnabled())
    sb.set_busy(True)
    check("busy disables both toggles",
          not sb._record_toggle.isEnabled() and not sb._calibrate_toggle.isEnabled())
    check("busy disables solve", not sb._run_calib_btn.isEnabled())
    sb.set_busy(False)
    check("after busy, record stays disabled while calibrate is on",
          not sb._record_toggle.isEnabled())
    check("after busy, calibrate is enabled again", sb._calibrate_toggle.isEnabled())
    check("after busy, solve is enabled again (no solve running)",
          sb._run_calib_btn.isEnabled())

    sb._calibrate_toggle.setChecked(False)
    check("calibrate off re-enables record", sb._record_toggle.isEnabled())

    sb._record_toggle.setChecked(True)
    sb.set_busy(True)
    sb.set_busy(False)
    check("after busy, calibrate stays disabled while record is on",
          not sb._calibrate_toggle.isEnabled())
    check("after busy, record is enabled again", sb._record_toggle.isEnabled())
    sb.close()


def test_solve_gate_survives_busy_cycle():
    sb = make_sidebar()
    sb.set_toggles_enabled(False)
    sb.set_solve_enabled(False)
    sb.set_busy(True)
    sb.set_busy(False)
    check("solve stays disabled during a solve after a busy cycle",
          not sb._run_calib_btn.isEnabled())
    check("toggles stay gated during a solve after a busy cycle",
          not sb._calibrate_toggle.isEnabled() and not sb._record_toggle.isEnabled())
    sb.set_solve_enabled(True)
    check("set_solve_enabled(True) reopens solve", sb._run_calib_btn.isEnabled())
    sb.set_toggles_enabled(True)
    check("set_toggles_enabled(True) reopens both toggles",
          sb._calibrate_toggle.isEnabled() and sb._record_toggle.isEnabled())
    sb.close()


def test_toggles_gate_survives_busy_and_reset_reopens():
    sb = make_sidebar()
    sb.set_toggles_enabled(False)       # ENCODING / ALIGNING
    sb.set_busy(True)
    sb.set_busy(False)
    check("encoding gate survives a busy cycle",
          not sb._calibrate_toggle.isEnabled() and not sb._record_toggle.isEnabled())
    sb.reset_toggles()                  # _finish_to_idle relies on this
    check("reset_toggles reopens the gate",
          sb._calibrate_toggle.isEnabled() and sb._record_toggle.isEnabled())
    check("reset_toggles leaves both off",
          not sb._calibrate_toggle.isChecked() and not sb._record_toggle.isChecked())
    sb.close()


def test_busy_overlay_does_not_touch_stimulation():
    sb = make_sidebar()
    sb.set_busy(True)
    check("stimulation button stays enabled while busy", sb._stim_btn.isEnabled())
    check("snapshot disabled while busy", not sb._snapshot_btn.isEnabled())
    sb.set_busy(False)
    check("snapshot enabled after busy", sb._snapshot_btn.isEnabled())
    sb.close()


# --------------------------------------------------------------------------
# A5-02: a refused start clears only the refused toggle; stop_record always
# reaches the stop path.
# --------------------------------------------------------------------------
def test_clear_toggle_silently_keeps_the_live_toggle():
    sb = make_sidebar()
    emitted = []
    sb.calibrate_toggled.connect(lambda c: emitted.append(("calibrate", c)))
    sb.record_toggled.connect(lambda c: emitted.append(("record", c)))

    sb._record_toggle.setChecked(True)            # a live recording
    # The exclusion normally forbids this click; force the state a bypassing
    # caller could produce and refuse it the way main_window does.
    sb._calibrate_toggle.setEnabled(True)
    sb._calibrate_toggle.setChecked(True)
    emitted.clear()
    sb.clear_toggle_silently("calibrate")
    check("refused calibrate is off", not sb._calibrate_toggle.isChecked())
    check("live record stays on", sb._record_toggle.isChecked())
    check("live record is enabled again (can be stopped)", sb._record_toggle.isEnabled())
    check("refused calibrate is disabled while record is on",
          not sb._calibrate_toggle.isEnabled())
    check("silent clear emitted nothing", emitted == [], repr(emitted))
    check("refused thumb animates towards off",
          sb._calibrate_toggle._anim.endValue() == 0.0)
    sb.close()


def test_clear_toggles_silently_alias_uses_last_armed():
    sb = make_sidebar()
    sb._calibrate_toggle.setChecked(True)         # live calibration
    sb._record_toggle.setEnabled(True)
    sb._record_toggle.setChecked(True)            # the refused click
    sb.clear_toggles_silently()                   # zero-arg legacy call
    check("alias cleared the refused (last armed) toggle", not sb._record_toggle.isChecked())
    check("alias left the live toggle on", sb._calibrate_toggle.isChecked())
    check("alias re-enabled the live toggle", sb._calibrate_toggle.isEnabled())
    sb.close()

    sb = make_sidebar()
    sb._calibrate_toggle.setChecked(True)
    sb.clear_toggles_silently("calibrate")
    check("alias with an explicit kind delegates", not sb._calibrate_toggle.isChecked())
    sb._record_toggle.setChecked(True)
    sb.clear_toggle_silently("recording")          # main_window's acq_type spelling
    check("acq_type spelling 'recording' is accepted", not sb._record_toggle.isChecked())
    sb._calibrate_toggle.setChecked(True)
    sb.clear_toggle_silently("calibration")
    check("acq_type spelling 'calibration' is accepted", not sb._calibrate_toggle.isChecked())
    try:
        sb.clear_toggle_silently("snapshot")
        check("unknown kind is refused", False)
    except ValueError:
        check("unknown kind is refused", True)
    sb.close()


def test_stop_record_emits_when_already_off():
    sb = make_sidebar()
    emitted = []
    sb.record_toggled.connect(lambda c: emitted.append(c))
    check("record starts off", not sb._record_toggle.isChecked())
    sb.stop_record()
    check("stop_record on an off toggle still emits False", emitted == [False], repr(emitted))
    emitted.clear()
    sb._record_toggle.setChecked(True)
    emitted.clear()
    sb.stop_record()
    check("stop_record on an on toggle emits False once", emitted == [False], repr(emitted))
    check("stop_record turned the toggle off", not sb._record_toggle.isChecked())
    sb.close()


# --------------------------------------------------------------------------
# A5-06: shrinking the camera count leaves no stale row/column stretch.
# --------------------------------------------------------------------------
def _stretches(grid):
    lay = grid._layout
    rows = [lay.rowStretch(r) for r in range(lay.rowCount())]
    cols = [lay.columnStretch(c) for c in range(lay.columnCount())]
    return rows, cols


def test_shrinking_grid_zeroes_stale_stretches():
    grid = CameraGridWidget()
    grid.resize(900, 600)
    grid.show()
    grid.setup_grid(9)
    app.processEvents()
    rows, _ = _stretches(grid)
    check("9 cameras: three rows stretched", rows[:3] == [1, 1, 1], repr(rows))

    grid.setup_grid(3)
    app.processEvents()
    rows, cols = _stretches(grid)
    check("9->3 cameras: only row 0 keeps a stretch",
          rows[0] == 1 and all(r == 0 for r in rows[1:]), repr(rows))
    check("9->3 cameras: three columns stretched", cols[:3] == [1, 1, 1], repr(cols))
    cell_h = grid._cells[0].height()
    check("9->3 cameras: the single row fills the widget height",
          cell_h > 0.8 * grid.height(), f"cell {cell_h} of {grid.height()}")
    grid.close()


def test_unzoom_restores_from_clean_stretches():
    grid = CameraGridWidget()
    grid.resize(900, 600)
    grid.show()
    grid.setup_grid(6)
    app.processEvents()
    grid.toggle_zoom(4)
    app.processEvents()
    rows, cols = _stretches(grid)
    check("zoomed: only (0,0) stretched",
          rows[0] == 1 and all(r == 0 for r in rows[1:])
          and cols[0] == 1 and all(c == 0 for c in cols[1:]), repr((rows, cols)))
    check("zoomed: other panes hidden",
          all(c.isHidden() != (i == 4) for i, c in enumerate(grid._cells)))
    grid.toggle_zoom(4)
    app.processEvents()
    rows, cols = _stretches(grid)
    check("unzoomed: two rows and three columns stretched",
          rows[:2] == [1, 1] and all(r == 0 for r in rows[2:]) and cols[:3] == [1, 1, 1],
          repr((rows, cols)))
    check("unzoomed: every pane visible", all(not c.isHidden() for c in grid._cells))
    grid.close()


# --------------------------------------------------------------------------
# A5-07 / A5-08: frames paint from a Grayscale8 QImage, hidden panes are not
# converted, and the target rect keeps the sensor aspect.
# --------------------------------------------------------------------------
def _frame(w=640, h=400, value=180):
    f = np.full((h, w), value, dtype=np.uint8)
    f[:, : w // 2] = 60      # a left/right split so orientation is testable
    return f


def test_pane_paints_grayscale8_without_pixmap_expansion():
    grid = CameraGridWidget()
    grid.resize(900, 600)
    grid.show()
    grid.setup_grid(6)
    app.processEvents()
    grid.update_frame(0, _frame())
    img = grid._cells[0].view.image()
    check("pane holds a QImage after update_frame", img is not None)
    check("pane image stays 8-bit Grayscale8",
          img is not None and img.format() == QImage.Format_Grayscale8 and img.depth() == 8,
          f"format={img.format() if img else None} depth={img.depth() if img else None}")
    # Painting must run without error and show the frame, not a blank pane.
    shot = grid._cells[0].view.grab().toImage()
    rect = grid._cells[0].view.target_rect()
    cx, cy = rect.center().x(), rect.center().y()
    left = shot.pixelColor(rect.left() + 5, cy).red()
    right = shot.pixelColor(rect.right() - 5, cy).red()
    check("painted pane shows the frame's left/right split",
          left < 100 < right, f"left={left} right={right}")

    non_contig = np.asfortranarray(_frame(320, 200))
    grid.update_frame(1, non_contig)
    check("non-contiguous frame is accepted",
          grid._cells[1].view.image() is not None
          and grid._cells[1].view.image().width() == 320)
    grid.close()


def test_hidden_pane_skips_conversion():
    grid = CameraGridWidget()
    grid.resize(900, 600)
    grid.show()
    grid.setup_grid(6)
    app.processEvents()
    grid.toggle_zoom(0)
    app.processEvents()
    grid.update_frame(1, _frame())
    grid.update_frame(0, _frame())
    check("hidden pane received no image", grid._cells[1].view.image() is None)
    check("zoomed pane received its image", grid._cells[0].view.image() is not None)
    grid.toggle_zoom(0)
    app.processEvents()
    grid.update_frame(1, _frame())
    check("pane converts again once visible", grid._cells[1].view.image() is not None)
    grid.close()


def test_zoomed_pane_keeps_sensor_aspect():
    grid = CameraGridWidget()
    grid.resize(960, 400)       # 2.4:1 grid area, the 6-camera window shape
    grid.show()
    grid.setup_grid(6)
    app.processEvents()
    grid.toggle_zoom(2)
    app.processEvents()
    grid.update_frame(2, _frame(640, 400))
    pane = grid._cells[2].view
    check("zoomed pane is wider than the sensor aspect",
          pane.width() / pane.height() > 2.0, f"{pane.width()}x{pane.height()}")
    rect = pane.target_rect()
    aspect = rect.width() / rect.height()
    check("target rect keeps the 1.6 sensor aspect", abs(aspect - 1.6) < 0.02, f"{aspect:.3f}")
    check("target rect is letterboxed inside the pane",
          rect.width() <= pane.width() and rect.height() <= pane.height()
          and rect.left() > 0 and rect.right() < pane.width() - 1,
          f"rect={rect} pane={pane.width()}x{pane.height()}")
    check("target rect is centred",
          abs(rect.center().x() - pane.width() / 2) <= 1
          and abs(rect.center().y() - pane.height() / 2) <= 1)

    # A tall pane letterboxes the other way.
    pane.resize(200, 400)
    rect = pane.target_rect()
    check("tall pane letterboxes top/bottom",
          rect.width() == 200 and rect.height() == 125 and rect.top() > 0, repr(rect))
    grid.close()


# --------------------------------------------------------------------------
# A5-12: a disabled toggle looks disabled and says why.
# --------------------------------------------------------------------------
def test_disabled_toggle_is_visibly_dimmed():
    tog = ToggleSwitch("Record")
    tog.resize(180, 36)
    tog.show()
    app.processEvents()
    live = tog.grab().toImage()
    tog.setEnabled(False)
    app.processEvents()
    dead = tog.grab().toImage()
    check("disabled toggle pixels differ from enabled", live != dead)
    # The whole switch paints at reduced opacity when disabled, so the track
    # (x in [0,44)) must move towards the widget background, whatever the
    # palette: the offscreen default is light, the app's palette is dark.
    bg = live.pixelColor(170, 2).lightness()          # above the label text
    ly, dy = live.pixelColor(22, 18).lightness(), dead.pixelColor(22, 18).lightness()
    check("disabled track fades towards the background",
          abs(dy - bg) < abs(ly - bg), f"bg={bg} live={ly} dead={dy}")
    tog.setEnabled(True)
    app.processEvents()
    check("re-enabled toggle paints like the original", tog.grab().toImage() == live)
    tog.close()


def test_disabled_toggle_tooltip_names_the_gate():
    sb = make_sidebar()
    check("live toggles carry a purpose tooltip",
          sb._record_toggle.toolTip() and "Disabled" not in sb._record_toggle.toolTip())
    sb._calibrate_toggle.setChecked(True)
    check("record tooltip names the sibling",
          sb._record_toggle.toolTip() == "Disabled while Calibrate is on",
          sb._record_toggle.toolTip())
    sb.set_busy(True)
    check("busy tooltip on both toggles",
          sb._record_toggle.toolTip() == sb._calibrate_toggle.toolTip() == SidebarWidget._BUSY_TIP)
    check("busy tooltip on solve", sb._run_calib_btn.toolTip() == SidebarWidget._BUSY_TIP)
    sb.set_busy(False)
    check("after busy the sibling reason returns",
          sb._record_toggle.toolTip() == "Disabled while Calibrate is on")
    check("after busy the solve tooltip is restored",
          sb._run_calib_btn.toolTip() == SidebarWidget._SOLVE_TIP)
    sb._calibrate_toggle.setChecked(False)
    sb.set_toggles_enabled(False)
    check("gate tooltip while encoding/aligning",
          sb._record_toggle.toolTip() == SidebarWidget._GATE_TIP)
    sb.set_solve_enabled(False)
    check("solve tooltip while a solve runs",
          "solve" in sb._run_calib_btn.toolTip().lower() and "Disabled" in sb._run_calib_btn.toolTip())
    sb.close()


# --------------------------------------------------------------------------
# A10-18 / A5-03: a malformed profile is skipped with a warning, not fatal.
# --------------------------------------------------------------------------
class _profiles_dir:
    """Point session_config at a temporary profiles directory."""

    def __init__(self, files: dict):
        self.files = files

    def __enter__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="profiles_", dir=_TMP))
        for name, text in self.files.items():
            (self.dir / name).write_text(text, encoding="utf-8")
        self.saved = session_config.PROFILES_DIR
        session_config.PROFILES_DIR = self.dir
        return self.dir

    def __exit__(self, *exc):
        session_config.PROFILES_DIR = self.saved


GOOD_PROFILE = (
    "name: goodrig\n"
    "metadata_defaults:\n"
    "  experimenter: AB\n"
    "  assay: maze\n"
)


def test_malformed_profile_is_skipped_with_warning():
    files = {
        "a_bad.yaml": "- this\n- is a list\n",
        "b_empty.yaml": "",
        "c_good.yaml": GOOD_PROFILE,
        "d_typo.yaml": "name: typo\nframe_rate: fast\n",
    }
    with _profiles_dir(files):
        sb = make_sidebar()
    names = [sb._profile_combo.itemText(i) for i in range(sb._profile_combo.count())]
    check("only the good profile loaded", names == ["goodrig"], repr(names))
    w = sb.profile_warnings
    check("one warning per skipped file", len(w) == 3, repr(w))
    check("warnings name the skipped files",
          all(any(n in m for m in w) for n in ("a_bad.yaml", "b_empty.yaml", "d_typo.yaml")), repr(w))
    check("no warning about the good profile", not any("c_good" in m for m in w))
    check("current_profile is the good one", sb.current_profile.name == "goodrig")
    sb.close()


def test_no_loadable_profile_names_the_directory():
    with _profiles_dir({"only_bad.yaml": "42\n"}) as d:
        sb = make_sidebar()
    w = sb.profile_warnings
    check("no profile loaded leaves the combo empty", sb._profile_combo.count() == 0)
    check("warning names the profiles directory", any(str(d) in m for m in w), repr(w))
    check("current_profile falls back to defaults", sb.current_profile.name == session_config.RigProfile().name)
    sb.close()


def test_all_good_profiles_give_no_warnings():
    with _profiles_dir({"one.yaml": GOOD_PROFILE, "two.yaml": "name: two\n"}):
        sb = make_sidebar()
    check("no warnings when every profile loads", sb.profile_warnings == [], repr(sb.profile_warnings))
    check("both profiles listed", sb._profile_combo.count() == 2)
    sb.close()


# --------------------------------------------------------------------------
# A5-04: an untouched date refreshes when a toggle is armed, and only then:
# a read of the fields must not rewrite it, because the main window rebuilds
# the config from the form for Solve and calibration-done, which must find
# the directory the last acquisition was filed under. Experimenter/assay come
# from the profile's metadata_defaults and never overwrite what was typed.
# --------------------------------------------------------------------------
def _user_types(field, text):
    field.setText(text)
    field.textEdited.emit(text)


def test_untouched_date_refreshes_when_a_toggle_is_armed():
    with _profiles_dir({"one.yaml": GOOD_PROFILE}):
        sb = make_sidebar()
    today = datetime.now().strftime("%Y%m%d")
    check("date starts as today", sb._fields["date"].text() == today, sb._fields["date"].text())
    sb._fields["date"].setText("19990101")          # stale, as if left open past midnight
    check("a plain read leaves a stale date alone",
          sb.get_field_values()["date"] == "19990101", sb._fields["date"].text())

    seen = []
    sb.calibrate_toggled.connect(lambda on: seen.append(sb.get_field_values()["date"]))
    sb._calibrate_toggle.setChecked(True)
    check("arming Calibrate refreshes the date before the signal fires",
          seen == [today], repr(seen))
    sb._calibrate_toggle.setChecked(False)
    sb._fields["date"].setText("19990101")
    check("disarming does not touch the date", sb._fields["date"].text() == "19990101")

    seen.clear()
    sb.record_toggled.connect(lambda on: seen.append(sb.get_field_values()["date"]))
    sb._record_toggle.setChecked(True)
    check("arming Record refreshes the date before the signal fires",
          seen == [today], repr(seen))
    sb._record_toggle.setChecked(False)

    _user_types(sb._fields["date"], "20200202")
    sb._calibrate_toggle.setChecked(True)
    check("a date the operator typed survives arming",
          sb.get_field_values()["date"] == "20200202", sb._fields["date"].text())
    sb._calibrate_toggle.setChecked(False)
    sb.close()


def test_metadata_defaults_prefill_from_profile():
    files = {"a_one.yaml": GOOD_PROFILE, "b_two.yaml": "name: two\n"}
    with _profiles_dir(files):
        sb = make_sidebar()
        vals = sb.get_field_values()
        check("experimenter prefilled from the first profile", vals["experimenter"] == "AB", vals["experimenter"])
        check("assay prefilled from the first profile", vals["assay"] == "maze", vals["assay"])
        check("no hardcoded initials anywhere", "IT" not in vals.values())

        sb._profile_combo.setCurrentIndex(1)          # profile without metadata_defaults
        vals = sb.get_field_values()
        check("switching to a profile with blank defaults clears the prefill",
              vals["experimenter"] == "" and vals["assay"] == "", repr(vals))

        _user_types(sb._fields["experimenter"], "ZZ")
        sb._profile_combo.setCurrentIndex(0)
        vals = sb.get_field_values()
        check("operator-typed experimenter survives a profile switch", vals["experimenter"] == "ZZ")
        check("untouched assay follows the profile", vals["assay"] == "maze")

        check("select_profile also applies metadata", sb.select_profile("two") and
              sb.get_field_values()["assay"] == "")
    sb.close()


# --------------------------------------------------------------------------
# A5-13: the thumb follows the checked state even under blockSignals.
# --------------------------------------------------------------------------
def test_thumb_follows_silent_state_changes():
    tog = ToggleSwitch("Calibrate")
    tog.show()
    app.processEvents()
    tog.blockSignals(True)
    tog.setChecked(True)
    tog.blockSignals(False)
    check("silent setChecked(True) animates the thumb on", tog._anim.endValue() == 1.0)
    tog._anim.setCurrentTime(tog._anim.duration())
    check("thumb reaches the on position", abs(tog.thumb_pos - 1.0) < 1e-6, str(tog.thumb_pos))
    tog.blockSignals(True)
    tog.setChecked(False)
    tog.blockSignals(False)
    check("silent setChecked(False) animates the thumb off", tog._anim.endValue() == 0.0)
    check("no private toggled slot remains for callers to reach into",
          not hasattr(ToggleSwitch, "_on_toggled"))
    tog._anim.setCurrentTime(tog._anim.duration())

    # A real click and the Space key flip the state inside nextCheckState with
    # the refresh blocked, so checkStateSet never runs for them; click() below
    # is the programmatic path that reaches both virtuals. All three must move
    # the thumb, or the arm indicator lies on the path the operator uses.
    QTest.mouseClick(tog, Qt.LeftButton, Qt.NoModifier, QPoint(20, 18))
    check("a mouse click checks the switch", tog.isChecked())
    check("a mouse click animates the thumb on", tog._anim.endValue() == 1.0,
          repr(tog._anim.endValue()))
    tog._anim.setCurrentTime(tog._anim.duration())
    check("thumb reaches the on position after a mouse click", abs(tog.thumb_pos - 1.0) < 1e-6)

    QTest.keyClick(tog, Qt.Key_Space)
    check("Space unchecks the switch", not tog.isChecked())
    check("Space animates the thumb off", tog._anim.endValue() == 0.0, repr(tog._anim.endValue()))
    tog._anim.setCurrentTime(tog._anim.duration())
    check("thumb reaches the off position after Space", abs(tog.thumb_pos) < 1e-6)

    tog.click()
    check("a programmatic click still animates the thumb",
          tog._anim.endValue() == 1.0 and tog.isChecked())
    tog.close()


# --------------------------------------------------------------------------
# A5-05: the output directory is elided to the button width, never clipped.
# --------------------------------------------------------------------------
def test_output_dir_is_elided_to_fit_the_button():
    long_path = r"C:\a\b\c\some_very_long_project_folder_name\with_another_long_leaf_folder_name"
    with _profiles_dir({"one.yaml": GOOD_PROFILE}):
        sb = SidebarWidget(default_output_dir=long_path)
        sb.show()
        app.processEvents()
    btn = sb._dir_button
    text = btn.text()
    check("long path is shortened", len(text) < len(long_path), text)
    check("elided text carries an ellipsis", "\u2026" in text, text)
    head, tail = text.split("…")
    check("elided text keeps the start and the end of the path",
          long_path.startswith(head) and long_path.endswith(tail) and head.startswith("C:\\"), text)
    check("elided text fits inside the button",
          btn.fontMetrics().horizontalAdvance(text) <= btn.width(),
          f"{btn.fontMetrics().horizontalAdvance(text)} > {btn.width()}")
    check("tooltip carries the full path", btn.toolTip() == long_path)
    check("output_dir property is the full path", sb.output_dir == long_path)

    sb._set_output_dir(r"C:\short")
    check("a short path is shown whole", btn.text() == r"C:\short", btn.text())
    sb.close()


# --------------------------------------------------------------------------
# A5-09: sensor aspect and column count are settings, not constants.
# --------------------------------------------------------------------------
def test_camera_aspect_and_columns_are_configurable():
    grid = CameraGridWidget()
    grid.setup_grid(6)
    check("default grid aspect is the Basler 3x2 shape", abs(grid.grid_aspect() - 2.4) < 1e-9)
    grid.set_camera_aspect(1600, 1200)
    check("4:3 sensors give a 2.0 grid aspect at 3x2", abs(grid.grid_aspect() - 2.0) < 1e-9,
          str(grid.grid_aspect()))
    grid.set_columns(2)
    grid.setup_grid(4)
    positions = [grid._layout.getItemPosition(grid._layout.indexOf(c))[:2] for c in grid._cells]
    check("two columns lay four cameras out 2x2",
          positions == [(0, 0), (0, 1), (1, 0), (1, 1)], repr(positions))
    check("2x2 of 4:3 sensors is a 4:3 grid", abs(grid.grid_aspect() - 4 / 3) < 1e-9)
    cols = [grid._layout.columnStretch(c) for c in range(grid._layout.columnCount())]
    check("third column from the earlier 3-wide grid holds no stretch",
          cols[:2] == [1, 1] and all(c == 0 for c in cols[2:]), repr(cols))
    for bad in ((0, 1200), (1920, 0)):
        try:
            grid.set_camera_aspect(*bad)
            check(f"set_camera_aspect{bad} refused", False)
        except ValueError:
            check(f"set_camera_aspect{bad} refused", True)
    try:
        grid.set_columns(0)
        check("set_columns(0) refused", False)
    except ValueError:
        check("set_columns(0) refused", True)
    grid.close()


# --------------------------------------------------------------------------
# A5-10: the coverage graph reads the detector directly and keeps no dead
# fields; a renamed detector attribute fails loudly.
# --------------------------------------------------------------------------
class _DetectorStub:
    """The attributes BoardDetector.reset()/_update_ready() define."""

    def __init__(self, n=3):
        self.n = n
        self.glow = np.zeros(n)
        self.shared = np.zeros((n, n), dtype=int)
        self.shared[0, 1] = self.shared[1, 0] = 25
        self.per_cam_frames = np.array([50, 40, 0])
        self.per_cam_covis = np.array([999, 999, 999])   # display-only, must not be shown
        self.optimal_shared = 200
        self.min_per_cam_shared = 120
        self.grid_covered = np.zeros((n, 2, 2), dtype=bool)
        self.grid_cells_hit = np.array([4, 3, 0])
        self.MIN_GRID_CELLS = 3
        self.components = [[0, 1], [2]]
        self.ready = False


def test_coverage_graph_reads_detector_directly():
    g = CoverageGraphWidget()
    g.resize(228, 210)
    g.show()
    g.setup(3)
    check("setup starts with no groups", g._components == [])
    g.grab()                                  # paints before the first tick
    det = _DetectorStub()
    g.update_from(det)
    check("per-camera counter is the READY one, not the display one",
          list(g._per_cam) == [50, 40, 0], repr(g._per_cam))
    check("target comes from the detector", g._target == 120)
    check("grid threshold comes from the detector", g._min_grid_cells == 3)
    check("groups snapshot the detector's components", g._components == [[0, 1], [2]])
    check("no dead bridge/min_edge fields",
          not hasattr(g, "_bridge") and not hasattr(g, "_min_edge"))
    shot = g.grab()
    check("graph paints with two groups", not shot.isNull())
    g.setup(4)
    check("setup clears the previous session's groups", g._components == [])

    class Renamed(_DetectorStub):
        pass
    bad = Renamed()
    del bad.per_cam_frames
    try:
        g.update_from(bad)
        check("a renamed detector attribute fails loudly", False)
    except AttributeError:
        check("a renamed detector attribute fails loudly", True)
    g.close()


def main():
    test_exclusion_survives_busy_cycle()
    test_solve_gate_survives_busy_cycle()
    test_toggles_gate_survives_busy_and_reset_reopens()
    test_busy_overlay_does_not_touch_stimulation()
    test_clear_toggle_silently_keeps_the_live_toggle()
    test_clear_toggles_silently_alias_uses_last_armed()
    test_stop_record_emits_when_already_off()
    test_shrinking_grid_zeroes_stale_stretches()
    test_unzoom_restores_from_clean_stretches()
    test_pane_paints_grayscale8_without_pixmap_expansion()
    test_hidden_pane_skips_conversion()
    test_zoomed_pane_keeps_sensor_aspect()
    test_disabled_toggle_is_visibly_dimmed()
    test_disabled_toggle_tooltip_names_the_gate()
    test_malformed_profile_is_skipped_with_warning()
    test_no_loadable_profile_names_the_directory()
    test_all_good_profiles_give_no_warnings()
    test_untouched_date_refreshes_when_a_toggle_is_armed()
    test_metadata_defaults_prefill_from_profile()
    test_thumb_follows_silent_state_changes()
    test_output_dir_is_elided_to_fit_the_button()
    test_camera_aspect_and_columns_are_configurable()
    test_coverage_graph_reads_detector_directly()
    if _failures:
        print(f"\n{len(_failures)} FAILED: {_failures}")
        sys.exit(1)
    print("\nALL WIDGET TESTS PASS")


if __name__ == "__main__":
    main()
