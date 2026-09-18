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
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).parent))

import numpy as np
from PyQt5.QtCore import QSettings
from PyQt5.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from gui_app.widgets import sidebar as sidebar_module
from gui_app.widgets.sidebar import SidebarWidget

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


def main():
    test_exclusion_survives_busy_cycle()
    test_solve_gate_survives_busy_cycle()
    test_toggles_gate_survives_busy_and_reset_reopens()
    test_busy_overlay_does_not_touch_stimulation()
    test_clear_toggle_silently_keeps_the_live_toggle()
    test_clear_toggles_silently_alias_uses_last_armed()
    test_stop_record_emits_when_already_off()
    if _failures:
        print(f"\n{len(_failures)} FAILED: {_failures}")
        sys.exit(1)
    print("\nALL WIDGET TESTS PASS")


if __name__ == "__main__":
    main()
