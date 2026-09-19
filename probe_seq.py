"""Drive the REAL GUI through a SEQUENCE of acquisitions in one process.

Why this exists. Plain repeated recordings are not the reproduction: on
2026-09-14 three consecutive 600 s recordings passed while the bug Isaac hits
was very much alive. His reproduction is a *mixed* sequence in a single GUI
process -- recording, then a calibration, then a recording -- and the second
recording degraded: encode queue full at qsize 183-204 against
ENCODE_QUEUE_DEPTH 200, avg_proc 1.0 -> 5-7 ms, and five cameras climbing, with
grab-thread affinity identical to the clean first recording.

The suspected mechanism is that encoder threads are placed afresh for every
acquisition, so an unpinned encoder can land on an E-core -- which cannot
sustain encode submission for one 1920x1200 stream at 100 fps -- and the full
queue then stalls every camera through the shared NV12 ring. A single
recording cannot test that. A sequence can.

This is the only GUI-driving probe: a plain unattended recording is the
one-step sequence `--steps r:300`, so there is one scaffold to keep correct
rather than two that drift.

    uv run probe_seq.py                         # record 120, calibrate 60, record 120
    uv run probe_seq.py --steps r:180,c:60,r:180
    uv run probe_seq.py --steps r:120,c:60,r:120,c:60,r:120
    uv run probe_seq.py --steps r:300            # one unattended recording
    uv run probe_seq.py --steps r:300 --stim data/test_stim/stim_config.json
    uv run probe_seq.py --steps r:300 --display-hz 10
    uv run probe_seq.py --profile sim --steps c:20,r:40   # the simulated rig

`r:N` is a recording of N seconds, `c:N` a calibration. Calibration here runs
with no board in front of the cameras, which is fine: the point is to exercise
the same code path (full-res retention, the coverage worker, a second set of
encoders) between two recordings, not to produce a solve.

Everything lands in probe_out/gui_scratch, which is cleared on startup.
"""
import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer

from gui_app import settings
from gui_app.probe_guard import (add_force_argument,
                                 refuse_if_panopticon_running)


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


def launch_window(app, MainWindow, drain_s: float = 0.4):
    """Build the real window and report the dialogs the LAUNCH itself raises.

    RULE: the reporting shim covers the construction and the first moments of
    the event loop, and nothing after that. REASON: the startup camera open
    runs on whichever profile this machine REMEMBERS, which is not the one
    `--profile` asks for; on a host that lacks that rig's SDK it fails and
    posts a modal about 100 ms in, and the first processEvents enters a nested
    exec loop an unattended probe has nobody to answer. Later modals must stay
    real -- the Overwrite prompt would clobber a previous acquisition if
    something answered it for the operator -- so the probe keeps avoiding
    those by construction instead of suppressing them.
    """
    from gui_app import main_window as mw_mod

    real_box = mw_mod.QMessageBox

    class _Reported(real_box):
        """Prints a dialog to the console and answers it, for the launch only."""

        @staticmethod
        def _say(kind, parent, title, text, *a, **k):
            print(f"[probe] launch {kind}: {title}: {text}", flush=True)
            return real_box.Ok

        @staticmethod
        def warning(*a, **k):
            return _Reported._say("warning", *a, **k)

        @staticmethod
        def critical(*a, **k):
            return _Reported._say("critical", *a, **k)

        @staticmethod
        def information(*a, **k):
            return _Reported._say("information", *a, **k)

    mw_mod.QMessageBox = _Reported
    try:
        win = MainWindow()
        win.show()
        end = time.monotonic() + drain_s
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.01)
    finally:
        mw_mod.QMessageBox = real_box
    return win


def select_profile(app, win, name: str, timeout_s: float = 180.0) -> None:
    """Switch the window to the named rig profile and wait for the cameras.

    Driven through the combo box rather than by writing the profile into the
    settings store, so this takes exactly the path an operator's own selection
    takes -- close the old cameras, open the new ones, adopt the new serial
    port -- instead of a second, probe-only way of choosing a rig.

    RULE: return only once the switch has finished. REASON: it runs on a
    worker so the window stays responsive, and everything after this point
    (the firmware upload, the first step's toggle) is refused while the window
    is busy -- an unwaited switch turns into a probe that silently does
    nothing.

    RULE: the profile this machine REMEMBERS is put back afterwards. REASON:
    selecting a profile records it as the one the GUI comes up on next
    launch, and a probe run on `sim` must not leave the rig's own window
    pointing at the simulated cameras.
    """
    combo = win._sidebar._profile_combo
    names = [combo.itemText(i) for i in range(combo.count())]
    if name not in names:
        raise SystemExit(f"--profile {name!r}: not one of {names}")
    if win._profile.name == name:
        print(f"[probe] already on profile {name}", flush=True)
        return
    remembered = settings.app_settings().value(settings.KEY_PROFILE, "",
                                               type=str)
    print(f"[probe] switching to profile {name}", flush=True)
    combo.setCurrentIndex(names.index(name))
    deadline = time.monotonic() + timeout_s
    while win._busy and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    settings.app_settings().setValue(settings.KEY_PROFILE, remembered)
    if win._busy or win._profile.name != name:
        raise SystemExit(f"--profile {name!r}: the switch did not finish "
                         f"(now on {win._profile.name!r})")
    print(f"[probe] profile {name}: {win._camera_mgr.num_cameras} camera(s) "
          f"open", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=None,
                    help="rig profile to run on (default: whichever the GUI "
                         "remembers). `--profile sim` drives the simulated "
                         "rig, so this probe needs no hardware.")
    ap.add_argument("--steps", default="r:120,c:60,r:120",
                    help="comma list of r:SECONDS / c:SECONDS, in order")
    ap.add_argument("--warmup", type=float, default=8)
    ap.add_argument("--gap", type=float, default=4,
                    help="seconds to settle between steps (after ENCODING ends)")
    ap.add_argument("--stim", type=Path, default=None,
                    help="load this stim_config.json and Apply it before the "
                         "first step")
    ap.add_argument("--display-hz", type=float, default=None,
                    help="throttle the GUI's display refresh (default 30 Hz). "
                         "Tests whether main-thread display work is what "
                         "pushes the grab threads under 100 fps.")
    add_force_argument(ap)
    args = ap.parse_args()
    # Drives the whole rig through a real MainWindow, so no other Panopticon
    # may be running; parsing first keeps --help answerable either way.
    refuse_if_panopticon_running(force=args.force)
    steps = parse_steps(args.steps)

    from gui_app.main_window import MainWindow, State as S

    app = QApplication(sys.argv)
    win = launch_window(app, MainWindow)

    # Before anything else: the profile carries the cameras, the serial port
    # and the encoder, so every setting below has to be applied to the rig the
    # steps will actually run on.
    if args.profile:
        select_profile(app, win, args.profile)

    # Unique session id + scratch output dir: otherwise this reuses the default
    # m1_m2 path, and _start_acquisition would block forever on the "Overwrite?"
    # dialog with nobody to click it ... and would clobber real data if answered.
    # Clear it first, because the GUI raises that modal whenever the session
    # directory already holds data and an unattended probe cannot answer it.
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

    if args.display_hz:
        win._display_timer.setInterval(int(1000 / args.display_hz))
        print(f"[probe] display refresh throttled to {args.display_hz:g} Hz",
              flush=True)

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

    def begin():
        QTimer.singleShot(int(args.warmup * 1000), start)

    if args.stim:
        import json
        cfg = json.loads(args.stim.read_text())
        win._on_stimulation()
        sw = win._stim_window
        sw._canvas.load_workflow(cfg.get("blocks", []), cfg.get("edges", []))
        print(f"[probe] loaded stim paradigm from {args.stim}; uploading.",
              flush=True)

        def after_upload(ok, msg):
            print(f"[probe] stim upload ok={ok}: {msg[:200]}", flush=True)
            if not ok:
                # Do NOT record behind a failed upload. The GUI shows a MODAL
                # error dialog here, and a modal runs a nested event loop in
                # which QTimer still fires -- so a recording started while the
                # "Upload failed" box was open would be labelled stimulated
                # although nothing was ever flashed.
                print("[probe] ABORTING: refusing to record without the "
                      "paradigm on the board", flush=True)
                QApplication.instance().exit(2)
                return
            begin()
        # arduino-cli needs the serial port to itself; the GUI holds it warm
        # after startup, so an upload behind a held port loses the race and
        # fails with "exit 1".
        try:
            win.release_serial_port()
            print("[probe] released serial port for arduino-cli", flush=True)
        except Exception as e:
            print(f"[probe] could not release serial port: {e}", flush=True)
        sw._apply_btn.click()
        if sw._upload_worker is not None:
            sw._upload_worker.done.connect(after_upload)
        else:
            begin()
    else:
        begin()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
