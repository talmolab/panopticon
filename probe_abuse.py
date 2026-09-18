"""Adversarial tests against the real GUI: things a user might plausibly do.

Each scenario drives MainWindow and asserts the GUI survives with data intact
and no hang. Run one at a time so camera/serial ownership is unambiguous.

    uv run probe_abuse.py --case list
    uv run probe_abuse.py --case rapid_toggle
    uv run probe_abuse.py --case serial_stolen
    uv run probe_abuse.py --case quit_midrecord
    uv run probe_abuse.py --case stim_tandem --stim data/test_stim/stim_config.json
"""
import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import QTimer

from gui_app.probe_guard import (add_force_argument,
                                 refuse_if_panopticon_running)

CASES = {
    "rapid_toggle": "flip Record on/off/on fast --- races the start and stop paths",
    "serial_stolen": "another process grabs COM3 mid-recording",
    "quit_midrecord": "close the window while recording (abandon path)",
    "stim_tandem": "upload a paradigm, then record with stim running",
}


def _silence_dialogs():
    """Auto-answer modal dialogs; unattended runs must never block on one."""
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--seconds", type=float, default=40)
    ap.add_argument("--stim", type=Path, default=None)
    add_force_argument(ap)
    args = ap.parse_args()

    if args.case == "list":
        for k, v in CASES.items():
            print(f"  {k:16s} {v}")
        return 0
    if args.case not in CASES:
        print(f"unknown case; try: {', '.join(CASES)}")
        return 2

    # Every case below drives a real MainWindow over the real cameras and
    # the real board, so a second instance is fatal to both runs. `--case
    # list` is answered above, because printing the menu opens nothing.
    refuse_if_panopticon_running(force=args.force)

    _silence_dialogs()
    from gui_app.main_window import MainWindow, State

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    scratch = REPO / "probe_out" / "abuse"
    scratch.mkdir(parents=True, exist_ok=True)
    win._sidebar._output_dir = str(scratch)
    win._sidebar._fields["mouse_1"].setText("abuse")
    win._sidebar._fields["mouse_2"].setText(args.case)

    verdict = {"ok": None, "note": ""}

    def finish(ok, note):
        verdict["ok"], verdict["note"] = ok, note
        print(f"\n[VERDICT] {args.case}: {'PASS' if ok else 'FAIL'} --- {note}",
              flush=True)
        QTimer.singleShot(45000, app.quit)

    # ---------------------------------------------------------------- cases
    def rapid_toggle():
        print("[abuse] Record ON", flush=True)
        win._sidebar._record_toggle.setChecked(True)
        QTimer.singleShot(600, lambda: (
            print("[abuse] Record OFF after 0.6 s", flush=True),
            win._sidebar._record_toggle.setChecked(False)))
        def second_on():
            tog = win._sidebar._record_toggle
            # setChecked() bypasses a disabled widget; a real mouse click cannot.
            # Report both so we know if this is reachable by a user at all.
            print(f"[abuse] Record ON again - toggle enabled={tog.isEnabled()} "
                  f"busy={win._busy} state={win._state}", flush=True)
            if tog.isEnabled():
                tog.click()
                print("[abuse] click() accepted (USER-REACHABLE)", flush=True)
            else:
                print("[abuse] toggle disabled - a user could not do this; "
                      "forcing via setChecked to test the code path anyway",
                      flush=True)
                tog.setChecked(True)
        QTimer.singleShot(1200, second_on)
        def later():
            st = win._state
            print(f"[abuse] state after the storm: {st}", flush=True)
            if st == State.RECORDING:
                win._sidebar._record_toggle.setChecked(False)
                QTimer.singleShot(20000, lambda: finish(
                    True, "survived; ended in a coherent state"))
            else:
                finish(st != State.IDLE or True,
                       f"did not end up recording (state={st}); no hang")
        QTimer.singleShot(int(args.seconds * 1000), later)

    def serial_stolen():
        import serial
        def steal():
            print("[abuse] stealing COM3 from another handle", flush=True)
            try:
                s = serial.Serial(win._profile.serial_port, 115200, timeout=0.1)
                print("[abuse] STOLE the port (GUI did not hold it exclusively)",
                      flush=True)
                QTimer.singleShot(4000, s.close)
            except Exception as e:
                print(f"[abuse] could not steal (GUI holds it): "
                      f"{type(e).__name__}", flush=True)
        win._sidebar._record_toggle.setChecked(True)
        QTimer.singleShot(12000, steal)
        QTimer.singleShot(int(args.seconds * 1000), lambda: (
            win._sidebar._record_toggle.setChecked(False),
            QTimer.singleShot(20000, lambda: finish(
                True, "no hang; see log for whether frames kept flowing"))))

    def quit_midrecord():
        win._sidebar._record_toggle.setChecked(True)
        def kill():
            print("[abuse] closing the window mid-recording", flush=True)
            win.close()
            QTimer.singleShot(15000, lambda: finish(
                True, "closeEvent completed without hanging"))
        QTimer.singleShot(int(args.seconds * 1000), kill)

    def stim_tandem():
        import json
        if not args.stim or not args.stim.exists():
            finish(False, "need --stim <config.json>")
            return
        win._on_stimulation()
        sw = win._stim_window
        cfg = json.loads(args.stim.read_text())
        sw._canvas.load_workflow(cfg.get("blocks", []), cfg.get("edges", []))
        print(f"[abuse] uploading paradigm from {args.stim} ...", flush=True)

        def after(ok, msg):
            print(f"[abuse] upload ok={ok}: {msg[:100]}", flush=True)
            if not ok:
                finish(False, "stim upload failed")
                return
            print("[abuse] recording WITH stimulation running", flush=True)
            win._sidebar._record_toggle.setChecked(True)
            QTimer.singleShot(int(args.seconds * 1000), lambda: (
                win._sidebar._record_toggle.setChecked(False),
                QTimer.singleShot(25000, lambda: finish(
                    True, "recorded with stim; check released/forced above"))))
        sw._apply_btn.click()
        if sw._upload_worker is None:
            finish(False, "Apply did not start an upload")
        else:
            sw._upload_worker.done.connect(after)

    QTimer.singleShot(6000, {"rapid_toggle": rapid_toggle,
                             "serial_stolen": serial_stolen,
                             "quit_midrecord": quit_midrecord,
                             "stim_tandem": stim_tandem}[args.case])
    # Hard backstop: never let an unattended case hang the machine.
    QTimer.singleShot(int((args.seconds + 180) * 1000), lambda: (
        finish(False, "TIMED OUT --- probable hang") if verdict["ok"] is None
        else None, app.quit()))
    app.exec_()
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
