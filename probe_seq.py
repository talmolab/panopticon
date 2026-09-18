"""Drive the REAL GUI through a SEQUENCE of acquisitions in one process.

Why this exists. `probe_gui_record.py` runs recordings, and on 2026-09-14 three
consecutive 600 s recordings passed while the bug Isaac hits was very much
alive. His reproduction is a *mixed* sequence in a single GUI process --
recording, then a calibration, then a recording -- and the second recording
degraded: encode queue full at qsize 183-204 against ENCODE_QUEUE_DEPTH 200,
avg_proc 1.0 -> 5-7 ms, and five cameras climbing, with grab-thread affinity
identical to the clean first recording.

The suspected mechanism is that encoder threads are placed afresh for every
acquisition, so an unpinned encoder can land on an E-core -- which cannot
sustain encode submission for one 1920x1200 stream at 100 fps -- and the full
queue then stalls every camera through the shared NV12 ring. A single
recording cannot test that. A sequence can.

    uv run probe_seq.py                         # record 120, calibrate 60, record 120
    uv run probe_seq.py --steps r:180,c:60,r:180
    uv run probe_seq.py --steps r:120,c:60,r:120,c:60,r:120

`r:N` is a recording of N seconds, `c:N` a calibration. Calibration here runs
with no board in front of the cameras, which is fine: the point is to exercise
the same code path (full-res retention, the coverage worker, a second set of
encoders) between two recordings, not to produce a solve.

Everything lands in probe_out/gui_scratch, which is cleared on startup.
"""
import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer


def parse_steps(spec: str):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        kind, _, secs = part.partition(":")
        kind = kind.strip().lower()
        if kind not in ("r", "c"):
            raise SystemExit(f"step {part!r}: kind must be r (record) or c (calibrate)")
        out.append((kind, float(secs)))
    if not out:
        raise SystemExit("no steps")
    return out


def main():
    from gui_app.probe_guard import refuse_if_panopticon_running
    refuse_if_panopticon_running()
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", default="r:120,c:60,r:120",
                    help="comma list of r:SECONDS / c:SECONDS, in order")
    ap.add_argument("--warmup", type=float, default=8)
    ap.add_argument("--gap", type=float, default=4,
                    help="seconds to settle between steps (after ENCODING ends)")
    args = ap.parse_args()
    steps = parse_steps(args.steps)

    from gui_app.main_window import MainWindow, State as S

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()

    scratch = REPO / "probe_out" / "gui_scratch"
    if scratch.exists():
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    win._sidebar._output_dir = str(scratch)
    win._sidebar._fields["mouse_1"].setText("seqprobe")
    print(f"[probe] writing to {scratch}", flush=True)
    print(f"[probe] sequence: "
          + "  ".join(f"{'record' if k == 'r' else 'calibrate'} {s:g}s"
                      for k, s in steps), flush=True)

    state = {"i": 0}

    def start():
        kind, secs = steps[state["i"]]
        state["i"] += 1
        n = state["i"]
        # Fresh session id per step, else the GUI blocks on "Overwrite?".
        win._sidebar._fields["mouse_2"].setText(f"s{n}{kind}")
        label = "RECORDING" if kind == "r" else "CALIBRATION"
        print(f"[probe] === step {n}/{len(steps)}: {label} {secs:g}s ===",
              flush=True)
        toggle = (win._sidebar._record_toggle if kind == "r"
                  else win._sidebar._calibrate_toggle)
        state["toggle"] = toggle
        toggle.setChecked(True)
        QTimer.singleShot(int(secs * 1000), stop)

    def stop():
        n = state["i"]
        print(f"[probe] stopping step {n}", flush=True)
        state["toggle"].setChecked(False)
        if state["i"] < len(steps):
            def again():
                # Toggles stay disabled through ENCODING, so poll rather than
                # guess how long the previous step needs to finish writing.
                if win._state != S.IDLE or win._busy:
                    QTimer.singleShot(2000, again)
                else:
                    start()
            QTimer.singleShot(int(max(0.5, args.gap) * 1000), again)
        else:
            QTimer.singleShot(60000, app.quit)

    QTimer.singleShot(int(args.warmup * 1000), start)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
