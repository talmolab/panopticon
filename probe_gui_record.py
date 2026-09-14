"""Drive the REAL Panopticon GUI through a recording, unattended.

The headless probe (probe_lag.py) shows zero cross-camera lag over 5 minutes,
while GUI recordings show one camera pinned at kick_max_lag. This runs the
actual MainWindow ... real window, real display refresh, real sidebar ... and flips
the Record toggle on a timer, so the only variable left is "is it the GUI".

    uv run probe_gui_record.py --seconds 300

Everything lands in the normal logs/ file plus whatever the GUI writes.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QTimer


def _refuse_if_already_running():
    """Abort if another Panopticon probe is already up, via a PID lock file.

    Two instances enumerate the same cameras and fight over them, and the
    resulting lag looks exactly like a laggard bug. On 2026-09-14 three
    concurrent instances -- launched by chain scripts that outlived the pkill
    meant to stop them -- produced two "divergence" findings that drove two code
    changes before the overlap was noticed. Both had to be reverted.

    A lock file rather than scanning process command lines: `uv run` starts two
    python processes per run, so a command-line scan sees its OWN sibling and
    refuses to start. That is exactly what the first version of this guard did.
    """
    import atexit
    import os
    import sys

    lock = Path("probe_out") / ".gui_probe.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    def _alive(pid: int) -> bool:
        try:
            import subprocess
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"(Get-Process -Id {pid} -ErrorAction "
                                f"SilentlyContinue | Measure-Object).Count"],
                               capture_output=True, text=True, timeout=30)
            return r.stdout.strip().startswith("1")
        except Exception:
            return False        # cannot tell: assume stale, do not block

    if lock.exists():
        try:
            held = int(lock.read_text().split()[0])
        except Exception:
            held = None
        if held and held != os.getpid() and _alive(held):
            print(f"[probe] REFUSING TO START: another probe holds {lock} "
                  f"(pid {held}). Two instances fight over the same cameras.",
                  flush=True)
            sys.exit(3)
        print(f"[probe] clearing a stale lock from pid {held}", flush=True)

    lock.write_text(str(os.getpid()), encoding="utf-8")
    atexit.register(lambda: lock.unlink(missing_ok=True))
    print(f"[probe] holding {lock} (pid {os.getpid()})", flush=True)


def main():
    _refuse_if_already_running()
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=300)
    ap.add_argument("--warmup", type=float, default=8, help="settle before Record")
    ap.add_argument("--repeats", type=int, default=1,
                    help="consecutive recordings in ONE GUI process")
    ap.add_argument("--stim", type=Path, default=None,
                    help="load this stim_config.json and Apply it before recording")
    ap.add_argument("--display-hz", type=float, default=None,
                    help="throttle the GUI's display refresh (default 30 Hz). "
                         "Tests whether main-thread display work is what pushes "
                         "the grab threads under 100 fps.")
    args = ap.parse_args()

    from gui_app.main_window import MainWindow

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()

    # Unique session id + scratch output dir: otherwise this reuses the default
    # m1_m2 path, and _start_acquisition would block forever on the "Overwrite?"
    # dialog with nobody to click it ... and would clobber real data if answered.
    scratch = Path("probe_out") / "gui_scratch"
    # Clear it first. The GUI raises a MODAL "Overwrite?" dialog when the
    # session directory already holds data, and an unattended probe has nobody
    # to answer it: on 2026-09-14 two 600 s runs sat on that dialog forever and
    # were misread as an NVENC hang, until py-spy showed the main thread parked
    # in QMessageBox.question at main_window.py:630. Leave no data behind and
    # the prompt cannot fire.
    if scratch.exists():
        import shutil
        shutil.rmtree(scratch, ignore_errors=True)
    scratch.mkdir(parents=True, exist_ok=True)
    win._sidebar._output_dir = str(scratch)
    win._sidebar._fields["mouse_1"].setText("lagprobe")
    win._sidebar._fields["mouse_2"].setText(str(int(args.seconds)))
    print(f"[probe] writing to {scratch}", flush=True)

    if args.display_hz:
        win._display_timer.setInterval(int(1000 / args.display_hz))
        print(f"[probe] display refresh throttled to {args.display_hz:g} Hz",
              flush=True)

    state = {"n": 0}

    def start():
        state["n"] += 1
        # A fresh session id per repeat, else the GUI blocks on "Overwrite?".
        win._sidebar._fields["mouse_2"].setText(f"r{state['n']}")
        print(f"[probe] === recording {state['n']}/{args.repeats} "
              f"({args.seconds:g}s) ===", flush=True)
        win._sidebar._record_toggle.setChecked(True)
        QTimer.singleShot(int(args.seconds * 1000), stop)

    def stop():
        print(f"[probe] stopping recording {state['n']}", flush=True)
        win._sidebar._record_toggle.setChecked(False)
        if state["n"] < args.repeats:
            # Wait out ENCODING before the next one; toggles are disabled until
            # it finishes, so poll rather than guess.
            def again():
                from gui_app.main_window import State as S
                if win._state != S.IDLE or win._busy:
                    QTimer.singleShot(2000, again)
                else:
                    start()
            QTimer.singleShot(3000, again)
        else:
            QTimer.singleShot(60000, app.quit)

    if args.stim:
        import json
        from gui_app.widgets.stimulation_window import StimulationWindow
        win._on_stimulation()
        sw = win._stim_window
        cfg = json.loads(args.stim.read_text())
        sw._canvas.load_workflow(cfg.get("blocks", []), cfg.get("edges", []))
        print(f"[probe] loaded stim paradigm from {args.stim}; uploading.",
              flush=True)

        def after_upload(ok, msg):
            print(f"[probe] stim upload ok={ok}: {msg[:200]}", flush=True)
            if not ok:
                # Do NOT record behind a failed upload. The GUI shows a MODAL
                # error dialog here, and a modal runs a nested event loop in
                # which QTimer still fires -- so on 2026-09-14 the recording
                # started while the "Upload failed" box was open, and the trace
                # labelled it stimulated although nothing was ever flashed.
                print("[probe] ABORTING: refusing to record without the "
                      "paradigm on the board", flush=True)
                QApplication.instance().exit(2)
                return
            QTimer.singleShot(int(args.warmup * 1000), start)
        # arduino-cli needs the serial port to itself; the GUI holds it warm
        # after startup. Without this the upload loses the race and fails with
        # "exit 1", which is exactly what happened on 2026-09-14.
        try:
            win.release_serial_port()
            print("[probe] released serial port for arduino-cli", flush=True)
        except Exception as e:
            print(f"[probe] could not release serial port: {e}", flush=True)
        sw._apply_btn.click()
        if sw._upload_worker is not None:
            sw._upload_worker.done.connect(after_upload)
        else:
            QTimer.singleShot(int(args.warmup * 1000), start)
    else:
        QTimer.singleShot(int(args.warmup * 1000), start)
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
