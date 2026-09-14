"""A failed Apply must block Record.

`stim_trace.csv` marks which frames were stimulated by reading the CANVAS, not
the board. So when an upload fails the trace still reports stimulation windows
for a session in which the board never received the paradigm and nothing fired.
Observed 2026-09-14: arduino-cli lost a race for the serial port, Apply failed,
the recording went ahead anyway, and the trace reported "1503 frames, 900 with
stimulation active (59.9%)". Data mislabelled as stimulated is worse than no
data.

The distinction that matters, and the reason this is a separate flag rather
than a check on `_uploaded_ino`: a paradigm Applied in an EARLIER session is
still in the board's flash memory and must stay recordable, because the
paradigm survives closing the GUI. Only a failure we actually observed blocks.

No Qt window, no board: the window is built with `__new__` and only the
attributes `record_blocker` touches are stubbed.

    uv run python test_stim_guard.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui_app.widgets.stimulation_window import StimulationWindow


class _StubCanvas:
    """An empty, perfectly legal workflow."""

    def get_workflow(self):
        return [], []


def make(apply_failed=False, uploaded_ino=None):
    w = StimulationWindow.__new__(StimulationWindow)
    w._apply_failed = apply_failed
    w._uploaded_ino = uploaded_ino
    w._canvas = _StubCanvas()
    w._get_trigger_pins = lambda: [2, 4, 6, 8, 10, 12]
    w._get_safe_pins = lambda: [53]
    return w


failures = []


def check(num, name, cond, detail=""):
    print(f"{num}) {name}: {'PASS' if cond else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


# 1 -------------------------------------------------------------------------
w = make(apply_failed=True)
b = w.record_blocker()
check(1, "a FAILED Apply blocks Record", b is not None,
      (b or "")[:60])
check(2, "and the reason names the real risk, not a generic error",
      b is not None and "labelled as stimulated" in b,
      (b or "")[:80])

# 3 -------------------------------------------------------------------------
# The legitimate workflow that must NOT be blocked: nothing uploaded this
# session, because the paradigm was Applied before the GUI was restarted and
# still lives in the board's flash.
w = make(apply_failed=False, uploaded_ino=None)
check(3, "nothing uploaded THIS session does not block "
         "(paradigm survives in flash)", w.record_blocker() is None,
      str(w.record_blocker())[:60])

# 4 -------------------------------------------------------------------------
w = make(apply_failed=False, uploaded_ino="// some ino")
check(4, "a successful Apply does not block", w.record_blocker() is None)

# 5 -------------------------------------------------------------------------
# A failure followed by a success must clear: this is what "press Apply again
# and let it succeed" depends on.
w = make(apply_failed=True)
assert w.record_blocker() is not None
w._apply_failed = False                      # what _on_upload_done sets on ok
w._uploaded_ino = "// some ino"
check(5, "a later successful Apply clears the block",
      w.record_blocker() is None)

# 6 -------------------------------------------------------------------------
# The failure flag must outrank a clean canvas: a legal graph is not evidence
# that the board has it.
w = make(apply_failed=True)
check(6, "a legal canvas does not override a known failed upload",
      w.record_blocker() is not None)

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL STIM GUARD TESTS PASS")
