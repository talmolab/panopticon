"""Stimulation editor guards: what must block, what must stop, what must ask.

The editor's Test drives the laser pin through the main window's shared
TeensyController and its Apply flashes the board, so every path here is
exercised offscreen against a stub board and a stub upload worker. No serial
port is opened, no arduino-cli is run and no dialog blocks: QMessageBox and
QFileDialog are replaced by recorders that answer what each case scripts.

Rules pinned here and why:
- record_blocker() refuses while an upload is in flight or after a failed
  Apply, because the trace labels frames from the CANVAS and a board that did
  not take the paradigm fires nothing. Nothing-uploaded-this-session does not
  block: the main window clears the editor's upload record after every
  calibration while still holding the paradigm it will flash back.
- Escape and reject() never hide a running test's Stop control; close acts as
  Stop Test and the dialog stays up when the board did not confirm the stop.
- A test whose board was taken by an acquisition sends no stop, because the
  stop would cut the recording's camera triggers.
- provenance(flashed_source) compares the canvas with the sketch the main
  window actually flashed; the fallback is the editor's own last upload.
- Enter re-runs the diagnostics and refuses a non-positive duration; Load
  drops a second outgoing arrow and asks before discarding unsaved work;
  Save writes UTF-8 once and reports a failed write instead of raising.

    set QT_QPA_PLATFORM=offscreen
    python test_stim_guard.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt, QThread, pyqtSignal, QTimer, QEventLoop
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

from gui_app import stim_compiler
from gui_app.widgets import stimulation_window as sw
from gui_app.widgets.stimulation_window import (
    StimulationWindow, ArrowItem, ConnectorPort, block_mode, BW, BH,
    MIN_ZOOM, MAX_ZOOM,
)

app = QApplication.instance() or QApplication(sys.argv)

TRIGGER_PINS = [2, 4, 6, 8, 10, 12]
SAFE_PINS = [53]


# ── stubs ────────────────────────────────────────────────────────────────────
class _StubBoard:
    """Stands in for the main window's shared TeensyController."""

    def __init__(self, stop_ok=True):
        self.starts: list = []
        self.stops = 0
        self.stop_ok = stop_ok
        self.closed = False

    def start_triggers(self, pins, fps):
        self.starts.append((list(pins), fps))
        return True

    def stop_triggers(self, pins):
        self.stops += 1
        return self.stop_ok

    def close(self):
        self.closed = True


class _FakeUploadWorker(QThread):
    """An _UploadWorker whose flash succeeds instantly and runs no tool."""
    done = pyqtSignal(bool, str)
    result = (True, "ok")

    def __init__(self, ino, port, parent=None):
        super().__init__(parent)
        self.ino = ino
        self._port = port

    def run(self):
        self.done.emit(*self.result)


class _StubRunningWorker:
    """Only what is_uploading() reads: a worker that is still flashing."""

    def isRunning(self):
        return True


class _MsgBox:
    """Records every dialog and answers with the scripted button."""
    Yes, No = 1, 2
    answer = 2
    calls: list = []

    @classmethod
    def reset(cls, answer=None):
        cls.calls = []
        cls.answer = cls.No if answer is None else answer

    @classmethod
    def question(cls, parent, title, text, *a, **k):
        cls.calls.append(("question", title, text))
        return cls.answer

    @classmethod
    def critical(cls, parent, title, text, *a, **k):
        cls.calls.append(("critical", title, text))

    @classmethod
    def warning(cls, parent, title, text, *a, **k):
        cls.calls.append(("warning", title, text))

    @classmethod
    def information(cls, parent, title, text, *a, **k):
        cls.calls.append(("information", title, text))

    @classmethod
    def kinds(cls):
        return [c[0] for c in cls.calls]


class _FileDialog:
    """Returns the scripted path without showing anything."""
    path = ""
    opened = 0

    @classmethod
    def getOpenFileName(cls, *a, **k):
        cls.opened += 1
        return cls.path, "JSON (*.json)"

    @classmethod
    def getSaveFileName(cls, *a, **k):
        return cls.path, "JSON (*.json)"


sw.QMessageBox = _MsgBox
sw.QFileDialog = _FileDialog

TMP = Path(tempfile.mkdtemp(prefix="stim_guard_"))


def make(board=None, busy=False):
    """A real editor wired to a stub board; `flags['busy']` is is_busy()."""
    board = board if board is not None else _StubBoard()
    flags = {"busy": busy, "released": 0, "applied": []}
    w = StimulationWindow(
        get_port=lambda: "COM_STUB",
        get_output_dir=lambda: str(TMP),
        get_fps=lambda: 100,
        is_busy=lambda: flags["busy"],
        get_safe_pins=lambda: list(SAFE_PINS),
        get_trigger_pins=lambda: list(TRIGGER_PINS),
        get_serial=lambda: board,
        release_serial=lambda: flags.__setitem__("released", flags["released"] + 1),
        on_applied=lambda ino: flags["applied"].append(ino),
    )
    w.resize(900, 600)
    w._flags = flags
    w._board = board
    return w


def spin(ms=50):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def wait_until(pred, timeout_ms=3000):
    for _ in range(timeout_ms // 10):
        if pred():
            return True
        spin(10)
    return pred()


failures = []


def check(num, name, cond, detail=""):
    print(f"{num}) {name}: {'PASS' if cond else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


# ── record_blocker: failed Apply, nothing uploaded, in-flight upload ─────────
_MsgBox.reset()
w = make()
w._apply_failed = True
b = w.record_blocker()
check(1, "a FAILED Apply blocks Record", b is not None, (b or "")[:60])
check(2, "and the reason names the real risk, not a generic error",
      b is not None and "labelled as stimulated" in b, (b or "")[:80])

# The main window clears _uploaded_ino after every calibration while still
# holding the paradigm it flashes back for the recording, so None must not
# block; the drift check against that held paradigm is the main window's.
w = make()
check(3, "nothing uploaded THIS session does not block (main window holds "
         "the applied paradigm and reflashes it)", w.record_blocker() is None,
      str(w.record_blocker())[:60])

w._uploaded_ino = "// some ino"
check(4, "a successful Apply does not block", w.record_blocker() is None)

w = make()
w._apply_failed = True
assert w.record_blocker() is not None
w._apply_failed = False                      # what _on_upload_done sets on ok
w._uploaded_ino = "// some ino"
check(5, "a later successful Apply clears the block", w.record_blocker() is None)

w = make()
w._apply_failed = True
check(6, "a legal canvas does not override a known failed upload",
      w.record_blocker() is not None)

w = make()
w._upload_worker = _StubRunningWorker()
b = w.record_blocker()
check(7, "an upload in flight blocks Record", w.is_uploading() and b is not None
      and "upload is in progress" in b, (b or "")[:60])
w._apply_failed = True
b = w.record_blocker()
check(8, "the in-flight upload outranks a stale failed-Apply message",
      b is not None and "upload is in progress" in b, (b or "")[:60])
w._upload_worker = None
check(9, "is_uploading() is False once the worker reference is dropped",
      not w.is_uploading())

# ── uploading_changed signal and worker ownership ────────────────────────────
sw._UploadWorker = _FakeUploadWorker
_MsgBox.reset()
w = make()
w._canvas.add_block(53, 10, 5, 2)
seen: list = []
w.uploading_changed.connect(seen.append)
ino = w.firmware_source()
w._start_upload(ino)
check(10, "uploading_changed(True) fires when the flash starts and the "
          "worker is parented to the window",
      seen == [True] and w._upload_worker is not None
      and w._upload_worker.parent() is w and w._flags["released"] == 1,
      f"seen={seen}")
ok = wait_until(lambda: seen == [True, False])
check(11, "uploading_changed(False) fires when the flash ends, the worker is "
          "released and the paradigm handed to the main window",
      ok and w._upload_worker is None and not w.is_uploading()
      and w._uploaded_ino == ino and w._flags["applied"] == [ino]
      and w._apply_failed is False, f"seen={seen}")

_FakeUploadWorker.result = (False, "avrdude: port busy")
w = make()
w._canvas.add_block(53, 10, 5, 2)
seen = []
w.uploading_changed.connect(seen.append)
w._start_upload(w.firmware_source())
wait_until(lambda: seen == [True, False])
check(12, "a failed flash sets the failed-Apply block and shows the failure",
      w._apply_failed and w.record_blocker() is not None
      and "critical" in _MsgBox.kinds(), f"kinds={_MsgBox.kinds()}")
_FakeUploadWorker.result = (True, "ok")

# ── Escape / reject / close while a test runs ────────────────────────────────
_MsgBox.reset()
w = make()
w.show()
w._begin_test()                       # empty canvas: open-ended (looping) test
check(13, "is_testing() is public and True while the test runs",
      w.is_testing() and w._board.starts == [([], 100)],
      f"starts={w._board.starts}")
QTest.keyClick(w, Qt.Key_Escape)
spin()
check(14, "Escape on the window neither hides it nor stops the test",
      w.isVisible() and w.is_testing() and w._board.stops == 0)
QTest.keyClick(w._canvas, Qt.Key_Escape)
spin()
check(15, "Escape on the canvas is swallowed too",
      w.isVisible() and w.is_testing() and w._board.stops == 0)
w.reject()
spin()
check(16, "reject() routes through close(): the test is stopped and the "
          "board confirmed it",
      not w.isVisible() and not w.is_testing() and w._board.stops == 1
      and not w._board.closed and "Test stopped" in w._status_lbl.text(),
      w._status_lbl.text())

_MsgBox.reset()
w = make(_StubBoard(stop_ok=False))
w.show()
w._begin_test()
w.close()
spin()
check(17, "the dialog refuses to hide when the board did not confirm the stop",
      w.isVisible() and "STOP NOT CONFIRMED" in w._status_lbl.text()
      and _MsgBox.kinds() == ["critical"],
      f"{w._status_lbl.text()[:40]} kinds={_MsgBox.kinds()}")
w._board.stop_ok = True
w.close()
spin()
check(18, "and hides once nothing is running", not w.isVisible())

# ── a test superseded by an acquisition sends no stop ────────────────────────
_MsgBox.reset()
w = make()
w._begin_test()
w._flags["busy"] = True               # the main window took the board
stopped = w._end_test("Test complete.")
check(19, "_end_test skips the stop write while the main window owns the "
          "board and says so",
      stopped and w._board.stops == 0 and not w.is_testing()
      and "superseded" in w._status_lbl.text() and _MsgBox.kinds() == [],
      w._status_lbl.text())
w._flags["busy"] = False
check(20, "the Busy check refuses a new test while the main window owns "
          "the board", (w._flags.__setitem__("busy", True), w._on_test(),
                        _MsgBox.kinds() == ["information"])[2],
      f"kinds={_MsgBox.kinds()}")
w._flags["busy"] = False

# ── provenance against the flashed sketch ────────────────────────────────────
w = make()
w._canvas.add_block(53, 10, 5, 2)
ino = w.firmware_source()
blank = stim_compiler.recording_only_sketch(SAFE_PINS, TRIGGER_PINS)
p_none = w.provenance()
p_same = w.provenance(ino)
p_blank = w.provenance(blank)
check(21, "provenance() with no reference and nothing uploaded reports None",
      p_none["matches_uploaded_firmware"] is None)
check(22, "provenance(flashed_source) is True for the sketch that was flashed "
          "and False for the recording-only sketch",
      p_same["matches_uploaded_firmware"] is True
      and p_blank["matches_uploaded_firmware"] is False)
w._uploaded_ino = ino
check(23, "with no reference it falls back to the editor's own last upload",
      w.provenance()["matches_uploaded_firmware"] is True)
check(24, "firmware_sha256 is stim_compiler.sketch_sha of the canvas sketch",
      p_same["firmware_sha256"] == stim_compiler.sketch_sha(ino)
      and p_same["chains"] and p_same["blocks"] and p_same["edges"] == [])

# ── Enter re-runs the diagnostics and validates ──────────────────────────────
w = make()
a = w._canvas.add_block(53, 10, 5, 10)
w._canvas.set_explicit_end(a, True)
check(25, "the end time shows once an Ending block exists",
      "10 s after start" in w._status_lbl.text(), w._status_lbl.text())
a.setSelected(True)
spin()
assert w._selected_block is a, "selection did not reach the window"
w._f_dur.setText("60")
w._on_field_enter()
check(26, "Enter re-runs refresh_starts: the end time follows the edit",
      a.dur == 60 and "60 s after start" in w._status_lbl.text(),
      w._status_lbl.text())
w._f_dur.setText("0")
w._on_field_enter()
check(27, "Enter rejects dur <= 0 and leaves the block unchanged",
      a.dur == 60 and "Invalid" in w._status_lbl.text(), w._status_lbl.text())
w._f_dur.setText("-3")
w._on_field_enter()
check(28, "a negative duration is refused the same way",
      a.dur == 60 and "Invalid" in w._status_lbl.text())
w._f_dur.setText("60")
w._f_pw.setText("-1")
w._on_field_enter()
check(29, "a negative pulse width is refused",
      a.pw == 5 and "negative" in w._status_lbl.text(), w._status_lbl.text())
w._f_pw.setText("5")
w._f_pin.setText("4")
w._on_field_enter()
check(30, "moving a block onto a trigger pin by Enter shows the forbidden-pin "
          "diagnostic at once",
      a.pin == 4 and "cannot carry" in w._status_lbl.text(),
      w._status_lbl.text())
w._f_pin.setText("53")
w._on_field_enter()
check(31, "and moving it back clears the diagnostic",
      "60 s after start" in w._status_lbl.text(), w._status_lbl.text())
w._canvas.scene().clearSelection()
spin()
w._f_pin.setText("")
w._f_dur.setText("0")
n_before = len(w._canvas.blocks())
w._on_field_enter()                   # nothing selected -> Create path
check(32, "Create refuses a blank pin", len(w._canvas.blocks()) == n_before
      and "pin" in w._status_lbl.text().lower())
w._f_pin.setText("51")
w._on_field_enter()
check(33, "Create refuses dur <= 0 too", len(w._canvas.blocks()) == n_before
      and "Invalid" in w._status_lbl.text())
w._f_dur.setText("")
w._on_field_enter()
check(34, "Create fills a blank duration with 1 s",
      len(w._canvas.blocks()) == n_before + 1
      and any(b.dur == 1 and b.pin == 51 for b in w._canvas.blocks()))

# ── duplicate out-edges on load ──────────────────────────────────────────────
w = make()
blocks = [{"id": i, "x": k * 200, "y": 0, "pin": 53, "freq": 10, "pw": 5,
           "dur": 1} for k, i in enumerate("ABC")]
edges = [{"src": "A", "dst": "B"}, {"src": "A", "dst": "C"},
         {"src": "B", "dst": "B"}, {"src": "Z", "dst": "C"}]
dropped = w._canvas.load_workflow(blocks, edges)
arrows = [i for i in w._canvas.scene().items() if isinstance(i, ArrowItem)]
by_id = {b.block_id: b for b in w._canvas.blocks()}
check(35, "load_workflow keeps the first out-edge and drops the duplicate, "
          "the self-loop and the dangling edge",
      dropped == 3 and len(arrows) == 1 and by_id["A"].out_arrow is arrows[0]
      and arrows[0].dst is by_id["B"] and by_id["C"].in_arrows == [],
      f"dropped={dropped} arrows={len(arrows)}")
_, edges_out = w._canvas.get_workflow()
check(36, "and what the canvas serialises is what it draws",
      len(edges_out) == 1 and edges_out[0]["dst"] == "B")
try:
    ArrowItem(by_id["A"], by_id["A"].port(ConnectorPort.RIGHT),
              by_id["C"], by_id["C"].port(ConnectorPort.LEFT))
    raised = False
except ValueError:
    raised = True
check(37, "ArrowItem refuses a second outgoing arrow loudly", raised
      and by_id["A"].out_arrow is arrows[0])

_MsgBox.reset()
cfg = TMP / "dup.json"
cfg.write_text(json.dumps({"blocks": blocks, "edges": edges}), encoding="utf-8")
_FileDialog.path = str(cfg)
w = make()
w._on_load()
check(38, "Load reports the dropped edges in the status",
      "dropped 3" in w._status_lbl.text() and not w.has_unsaved_changes(),
      w._status_lbl.text())

# ── dirty flag and the Load prompt ───────────────────────────────────────────
_MsgBox.reset()
_FileDialog.opened = 0
w = make()
check(39, "a fresh editor has no unsaved changes", not w.has_unsaved_changes())
w._canvas.add_block(53, 10, 5, 2)
check(40, "adding a block marks the canvas dirty", w.has_unsaved_changes())
w._on_load()                          # answer is No
check(41, "Load asks before discarding a dirty canvas and, on No, never "
          "opens the file dialog",
      _MsgBox.kinds() == ["question"] and _FileDialog.opened == 0
      and len(w._canvas.blocks()) == 1
      and "Discard" in _MsgBox.calls[0][1])
_MsgBox.reset(answer=_MsgBox.Yes)
w._on_load()
check(42, "on Yes the file loads and the canvas is clean again",
      _FileDialog.opened == 1 and len(w._canvas.blocks()) == 3
      and not w.has_unsaved_changes())
_MsgBox.reset()
w._on_load()
check(43, "a clean canvas loads without asking",
      _MsgBox.kinds() == [] and _FileDialog.opened == 2)
_MsgBox.reset(answer=_MsgBox.Yes)
w._on_clear()
check(44, "Clear leaves nothing to protect", not w.has_unsaved_changes()
      and len(w._canvas.blocks()) == 0)
w._canvas.add_block(53, 10, 5, 2)
w.setAttribute(Qt.WA_DeleteOnClose, True)
_MsgBox.reset()
w.show()
w.close()
spin()
check(45, "a delete-on-close editor asks before discarding unsaved work "
          "and stays up on No", w.isVisible() and _MsgBox.kinds() == ["question"])
w.setAttribute(Qt.WA_DeleteOnClose, False)
_MsgBox.reset()
w.close()
spin()
check(46, "a hide-on-close editor (the main window's) closes without asking, "
          "because the canvas survives", not w.isVisible() and _MsgBox.kinds() == [])

# ── Save: one prompt, UTF-8, failure reported ────────────────────────────────
_MsgBox.reset()
w = make()
w._canvas.add_block(53, 10, 5, 2)
out = TMP / "saved.json"
out.write_text("stale", encoding="utf-8")
_FileDialog.path = str(out)
w._on_save()
data = json.loads(out.read_bytes().decode("utf-8"))
check(47, "Save writes UTF-8 JSON over an existing file with no second prompt "
          "and clears the dirty flag",
      _MsgBox.kinds() == [] and len(data["blocks"]) == 1
      and not w.has_unsaved_changes() and "Saved" in w._status_lbl.text())
_MsgBox.reset()
_FileDialog.path = str(TMP / "no_such_dir" / "x.json")
w._canvas.add_block(53, 10, 5, 2)
try:
    w._on_save()
    raised = False
except OSError:
    raised = True
check(48, "a failed write is reported by path, not raised, and the canvas "
          "stays dirty", not raised and _MsgBox.kinds() == ["critical"]
      and "no_such_dir" in _MsgBox.calls[0][2] and w.has_unsaved_changes())

# ── new blocks land in view, not on each other ───────────────────────────────
w = make()
w.show()
spin()
view = w._canvas.mapToScene(w._canvas.viewport().rect()).boundingRect()
b1 = w._canvas.add_block(53, 10, 5, 1)
b2 = w._canvas.add_block(53, 10, 5, 1)
b3 = w._canvas.add_block(53, 10, 5, 1)
rects = [b.sceneBoundingRect() for b in (b1, b2, b3)]
check(49, "three new blocks do not overlap",
      not rects[0].intersects(rects[1]) and not rects[1].intersects(rects[2])
      and not rects[0].intersects(rects[2]))
check(50, "and the first lands inside the visible area",
      view.contains(rects[0]), f"view={view} blk={rects[0]}")
w.close()

# ── zoom clamp and fit ───────────────────────────────────────────────────────
w = make()
for _ in range(60):
    w._canvas.zoom_by(1.15)
hi = w._canvas.zoom()
for _ in range(120):
    w._canvas.zoom_by(1 / 1.15)
lo = w._canvas.zoom()
check(51, "zoom is clamped at both ends",
      abs(hi - MAX_ZOOM) < 1e-6 and abs(lo - MIN_ZOOM) < 1e-6, f"{hi} {lo}")
w._canvas.add_block(53, 10, 5, 1).setPos(2500, 2500)
w.show()
spin()
QTest.keyClick(w._canvas, Qt.Key_Home)
spin()
visible = w._canvas.mapToScene(w._canvas.viewport().rect()).boundingRect()
check(52, "Home brings the content into view",
      all(visible.contains(b.sceneBoundingRect()) for b in w._canvas.blocks())
      and w._canvas.zoom() <= MAX_ZOOM + 1e-9)
w.close()

# ── invalidate_upload wording and status priority ────────────────────────────
w = make()
w._uploaded_ino = "// old"
w.invalidate_upload("a calibration needed the recording-only sketch")
t = w._status_lbl.text()
check(53, "invalidate_upload says Record reflashes on its own and Test asks "
          "first, instead of demanding a re-Apply",
      w._uploaded_ino is None and "Record flashes" in t and "Test will ask" in t
      and "Press Apply again" not in t, t)
a = w._canvas.add_block(53, 10, 5, 10)
w._canvas.set_explicit_end(a, True)
check(54, "a board notice survives canvas edits that would show the end time",
      "Record flashes" in w._status_lbl.text(), w._status_lbl.text())
b = w._canvas.add_block(53, 10, 5, 10)          # second chain on pin 53
check(55, "a canvas error outranks the notice",
      "more than one chain" in w._status_lbl.text(), w._status_lbl.text())
w._canvas.scene().clearSelection()
b.setSelected(True)
spin()
w._canvas._delete_selected()
check(56, "once the error is fixed the end-time line takes over",
      "10 s after start" in w._status_lbl.text(), w._status_lbl.text())
w._canvas.set_explicit_end(a, False)
check(57, "and a diagnostic with nothing left to say clears",
      w._status_lbl.text() == "", w._status_lbl.text())

# ── Test prompt wording ──────────────────────────────────────────────────────
_MsgBox.reset()
w = make()
w._canvas.add_block(53, 10, 5, 2)
w._on_test()
check(58, "Test with nothing uploaded this session says so rather than "
          "'changed since the last upload'",
      _MsgBox.kinds() == ["question"]
      and "Nothing has been uploaded" in _MsgBox.calls[0][2]
      and not w.is_testing() and w._board.starts == [])
_MsgBox.reset()
w._uploaded_ino = "// other"
w._on_test()
check(59, "Test after a canvas edit says the workflow changed",
      "changed since the last upload" in _MsgBox.calls[0][2])

# ── merge hint and the shared block-mode helper ──────────────────────────────
w = make()
blocks = [{"id": "A", "x": 0, "y": 0, "pin": 51, "freq": 10, "pw": 5, "dur": 1},
          {"id": "B", "x": 0, "y": 200, "pin": 52, "freq": 10, "pw": 5, "dur": 1},
          {"id": "C", "x": 300, "y": 100, "pin": 53, "freq": 10, "pw": 5, "dur": 1}]
edges = [{"src": "A", "dst": "C"}, {"src": "B", "dst": "C"}]
w._canvas.load_workflow(blocks, edges)
problem = w._blocking_problem() or ""
check(60, "a join A->C, B->C is reported as a merge on the merged block's pin",
      "merge" in problem and "53" in problem and "2 chains" in problem,
      problem.split("\n\n")[0])
check(61, "and the status line shows the same first sentence",
      w._status_lbl.text() == problem.split("\n\n")[0])
blocks[1]["pin"] = 51
edges = [{"src": "A", "dst": "C"}]
w._canvas.load_workflow(blocks, edges)
problem = w._blocking_problem() or ""
check(62, "two parallel chains on one pin are still a plain conflict",
      "driven by more than one chain" in problem and "merge" not in problem,
      problem.split("\n\n")[0])

modes = {(10, 5): "train", (10, 100): "constant", (10, 150): "impossible",
         (0, 5): "low", (10, 0): "low"}
agree = all(block_mode(f, p)[0] == k for (f, p), k in modes.items())
blk = sw.BlockItem(53, 10, 150, 1)
w._wave.set_params(10, 150)
check(63, "block_mode classifies every case and the block label and preview "
          "read the same helper",
      agree and blk.mode_text() == "constant ON"
      and w._wave.state()[0] == "impossible"
      and sw.BlockItem(53, 10, 5, 1).mode_text() == "5% duty", str(agree))
# The compiler's describe() must agree on the constant-ON threshold.
d = stim_compiler.describe([{"id": "A", "pin": 53, "freq": 10, "pw": 100,
                             "dur": 1}], [])
check(64, "the helper agrees with stim_compiler.describe() at the threshold",
      d[0]["steps"][0]["mode"] == "constant ON"
      and block_mode(10, 100)[0] == "constant")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL STIM GUARD TESTS PASS")
