"""The live thermal watch: does an overheating camera get reported in time?

Why this is worth a test. Reading temperatures only at stop records an
overheating camera but cannot prevent it -- by the time the number is visible
the camera has already stopped delivering and the block-ID bookkeeping has
truncated the session. The live watch is the only thing standing between a hot
rig and a silently short recording, and on the reference rig four of nine
cameras sit above the vendor's Critical
threshold with one peaking 1 C below shutdown, so it fires for real.

The thresholds must come from the CAMERA (`BslTemperatureStatus`,
`BsliOverTemperature`), never from a constant here, or the repo stops working
for anyone else's cameras. Several cases below pin exactly that.

No cameras, no switches, no real Qt window: the window is built with
`__new__` and the pieces the method touches are stubbed, so the logic can be
driven directly.

    uv run python test_thermal_watch.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui_app import main_window as mw
from gui_app.main_window import MainWindow, State
from gui_app.session_config import RigProfile


class _StubTimer:
    def __init__(self):
        self.stopped = 0
        self.started_ms = None

    def stop(self):
        self.stopped += 1

    def start(self, ms):
        self.started_ms = ms


class _StubMgr:
    """Returns queued thermal readings, or raises if handed an exception."""

    def __init__(self, readings):
        self.readings = readings
        self.calls = 0

    def thermals(self):
        self.calls += 1
        if isinstance(self.readings, Exception):
            raise self.readings
        return self.readings


def make(readings, n=3, state=State.RECORDING, names=None):
    w = MainWindow.__new__(MainWindow)
    w._state = state
    w._camera_mgr = _StubMgr(readings)
    w._camera_names = names if names is not None else [f"cam{i+1}" for i in range(n)]
    w._thermal_alert = None
    w._thermal_warnings = []
    w._thermal_reported = set()
    w._thermal_timer = _StubTimer()
    return w


def ok(temp=70.0):
    return {"temp_c": temp, "temp_status": "Ok",
            "temp_critical_c": 76.0, "temp_shutdown_c": 81.0}


def crit(temp=78.0, status="Critical", shutdown=81.0):
    d = {"temp_c": temp, "temp_status": status, "temp_critical_c": 76.0}
    if shutdown is not None:
        d["temp_shutdown_c"] = shutdown
    return d


failures = []


def check(num, name, cond, detail=""):
    print(f"{num}) {name}: {'PASS' if cond else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


# 1 -------------------------------------------------------------------------
w = make([ok(), ok(71.0), ok(69.0)])
w._poll_thermals()
check(1, "all cameras Ok leaves no alert and no warnings",
      w._thermal_alert is None and w._thermal_warnings == [])

# 2 -------------------------------------------------------------------------
w = make([ok(), crit(78.0), ok(69.0)])
w._poll_thermals()
check(2, "one Critical camera raises an alert naming it",
      w._thermal_alert is not None and "cam2" in w._thermal_alert,
      w._thermal_alert or "none")
check(3, "and records exactly one durable warning",
      len(w._thermal_warnings) == 1 and "cam2" in w._thermal_warnings[0])

# 4 -------------------------------------------------------------------------
# The camera CLOSEST to shutdown must be the one named, not the first found.
w = make([crit(77.0), ok(), crit(80.0)])
w._poll_thermals()
check(4, "the camera closest to shutdown is the one named",
      "cam3" in w._thermal_alert and "cam1" not in w._thermal_alert,
      w._thermal_alert)
check(5, "and the others are counted, not dropped",
      "+1 more" in w._thermal_alert, w._thermal_alert)
check(6, "both get a durable warning", len(w._thermal_warnings) == 2)

# 7 -------------------------------------------------------------------------
# The margin must be computed from the CAMERA's shutdown node, not a constant.
w = make([crit(78.0, shutdown=81.0)])
w._poll_thermals()
a = w._thermal_alert
w2 = make([crit(78.0, shutdown=95.0)])       # a model with more headroom
w2._poll_thermals()
check(7, "margin to shutdown comes from the camera, not a hardwired number",
      "3 C from shutdown" in a and "17 C from shutdown" in w2._thermal_alert,
      f"{a!r} vs {w2._thermal_alert!r}")

# 8 -------------------------------------------------------------------------
# Fallback: a camera that does not expose the status node but is past its
# shutdown temperature must still be caught.
w = make([{"temp_c": 82.0, "temp_shutdown_c": 81.0}])
w._poll_thermals()
check(8, "no status node, but temp past shutdown, is still caught",
      w._thermal_alert is not None, w._thermal_alert or "none")
# A negative margin must never read as though there were headroom left.
check(81, "a camera past shutdown says so, not a negative margin",
      "AT OR PAST" in (w._thermal_alert or "")
      and "-" not in (w._thermal_alert or ""),
      w._thermal_alert or "none")

# 9 -------------------------------------------------------------------------
w = make([{"temp_c": 70.0}])                 # no status, no shutdown
w._poll_thermals()
check(9, "a camera exposing neither node is not falsely accused",
      w._thermal_alert is None)

# 10 ------------------------------------------------------------------------
# Repeated polls must not pile up duplicate warnings for the same camera.
w = make([ok(), crit(78.0), ok()])
for _ in range(5):
    w._poll_thermals()
check(10, "five polls of the same hot camera record ONE warning",
      len(w._thermal_warnings) == 1,
      f"{len(w._thermal_warnings)} warnings after 5 polls")

# 11 ------------------------------------------------------------------------
# A camera that cools back to Ok clears the transient alert but keeps the
# durable warning -- the operator still needs to know it happened.
w = make([crit(78.0)])
w._poll_thermals()
had = len(w._thermal_warnings)
w._camera_mgr.readings = [ok(70.0)]
w._poll_thermals()
check(11, "cooling clears the live alert but keeps the durable warning",
      w._thermal_alert is None and len(w._thermal_warnings) == had == 1)

# 12 ------------------------------------------------------------------------
w = make([crit(78.0)], state=State.IDLE)
w._poll_thermals()
check(12, "not acquiring: stops its own timer and reads nothing",
      w._thermal_timer.stopped == 1 and w._camera_mgr.calls == 0
      and w._thermal_alert is None)

# 13 ------------------------------------------------------------------------
w = make([crit(78.0)], state=State.CALIBRATING)
w._poll_thermals()
check(13, "a calibration is watched too, not just a recording",
      w._thermal_alert is not None)

# 14 ------------------------------------------------------------------------
w = make(RuntimeError("GVCP read failed"))
try:
    w._poll_thermals()
    survived = True
except Exception as e:
    survived = False
    print(f"     raised {type(e).__name__}: {e}")
check(14, "a failing thermal read never propagates into the GUI loop",
      survived and w._thermal_alert is None)

# 15 ------------------------------------------------------------------------
w = make([{"error": "TimeoutException: no reply"}, crit(78.0)])
w._poll_thermals()
check(15, "a per-camera error reading is skipped, others still checked",
      w._thermal_alert is not None and "cam2" in w._thermal_alert,
      w._thermal_alert)

# 16 ------------------------------------------------------------------------
# Operator-facing names, not indices, when the profile supplies them.
w = make([ok(), crit(78.0)], names=["front-left", "front-right"])
w._poll_thermals()
check(16, "the profile's camera name is used in the alert",
      "front-right" in w._thermal_alert, w._thermal_alert)

# 17 ------------------------------------------------------------------------
# More readings than names must not IndexError.
w = make([ok(), ok(), crit(78.0)], names=["a", "b"])
try:
    w._poll_thermals()
    survived = True
except Exception as e:
    survived = False
    print(f"     raised {type(e).__name__}: {e}")
check(17, "more cameras than names falls back to an index label",
      survived and w._thermal_alert is not None
      and "cam3" in w._thermal_alert,
      (w._thermal_alert or "none") if survived else "raised")

# 18 ------------------------------------------------------------------------
# 'Error' (Basler's state above Critical) must be treated as hot, not ignored
# just because it is not the literal string 'Critical'.
w = make([crit(80.0, status="Error")])
w._poll_thermals()
check(18, "any non-Ok status counts, not only 'Critical'",
      w._thermal_alert is not None and "Error" in w._thermal_alert,
      w._thermal_alert)


# 19-21 ----------------------------------------------------------------------
# Quitting out of a recording. The poll is a GVCP register read per camera and
# it only self-stops on a state change, which the quit path never makes, so a
# timer left running fires inside the nested event loops of the quit dialogs -
# against cameras the quit is closing. Two threads inside pylon on one device
# is an access violation, not an exception.
class _Event:
    def __init__(self):
        self.accepted = None

    def accept(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


class _QuitBox:
    """QMessageBox stand-in that records the window state when it is asked."""
    Yes, No = 1, 0
    asked_at = []
    answer = Yes

    @classmethod
    def question(cls, parent, title, text, *a, **k):
        cls.asked_at.append(parent._thermal_timer.stopped)
        return cls.answer

    @classmethod
    def critical(cls, *a, **k):
        return cls.No

    @classmethod
    def information(cls, *a, **k):
        return cls.No

    @classmethod
    def warning(cls, *a, **k):
        return cls.No


def quitting(state=State.RECORDING, answer=_QuitBox.Yes):
    w = make([ok()], state=state)
    w._busy = False
    w._finalized = False
    w._fw_op = None
    w._stim_window = None
    w._teensy = None
    w._video_dir = None
    w._display_timer = _StubTimer()
    w._profile = RigProfile(thermal_poll_s=5.0)
    w._workers_running = lambda: False
    w._stop_coverage_hud = lambda timeout_ms=5000: None
    w._abandon_and_cleanup = lambda: None
    w._join_retired_workers = lambda: None
    w._camera_mgr.close_all = lambda: None
    _QuitBox.asked_at = []
    _QuitBox.answer = answer
    return w


real_box = mw.QMessageBox
mw.QMessageBox = _QuitBox
try:
    w = quitting()
    event = _Event()
    MainWindow.closeEvent(w, event)
    check(19, "the thermal poll is stopped BEFORE the quit dialog runs",
          _QuitBox.asked_at == [1] and event.accepted is True,
          f"stopped-at-dialog={_QuitBox.asked_at}")

    w = quitting(answer=_QuitBox.No)
    event = _Event()
    MainWindow.closeEvent(w, event)
    check(20, "a quit the operator cancels puts the watch back",
          event.accepted is False and w._thermal_timer.started_ms == 5000,
          f"started={w._thermal_timer.started_ms}")

    w = quitting(state=State.IDLE)
    w._finalized = True
    event = _Event()
    MainWindow.closeEvent(w, event)
    check(21, "quitting from IDLE stops it too, with no dialog at all",
          w._thermal_timer.stopped >= 1 and _QuitBox.asked_at == []
          and event.accepted is True)
finally:
    mw.QMessageBox = real_box

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL THERMAL WATCH TESTS PASS")
