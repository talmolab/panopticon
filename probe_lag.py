"""Headless acquisition harness for diagnosing cross-camera submission lag.

Drives the REAL code path --- CameraManager, GrabThread, SyncEncodeRouter,
FrameSyncCoordinator, TeensyController --- so anything found here applies to the
GUI. The only thing missing is Qt.

RULE: the profile reaches the manager through gui_app.rig_setup, the same two
calls the GUI makes, and the effective configuration is printed before the
cameras open. A probe that configures the rig by hand measures a machine nobody
records with: pool depth, capture-core exclusion and thread pinning each change
what the rig does, and a number quoted from the wrong one is worse than no
number. Read the CONFIG lines below before comparing a run against the GUI.

    uv run probe_lag.py --seconds 120
    uv run probe_lag.py --seconds 120 --max-lag 480 --label baseline
    uv run probe_lag.py --seconds 120 --no-display     # skip preview downsample
    uv run probe_lag.py --seconds 120 --max-buffer 200 # shrink the driver queue

Writes probe_out/<label>/ with the per-camera lag trace and a summary, and
prints a verdict. Recordings go to a scratch dir and are deleted afterwards
unless --keep.
"""
import argparse
import faulthandler
import json
import shutil
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

import numpy as np

# GrabThread is a QThread and CameraManager emits pyqtSignals. Without a
# QApplication the thread machinery wedges partway through a recording, so create
# an offscreen one --- this also keeps the probe faithful to how the GUI runs.
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QImage, QPixmap
_QAPP = QApplication.instance() or QApplication([])

#: Seconds allowed for teardown once the acquisition loop ends. Long enough
#: for nine cameras plus an encoder drain, short enough that a wedge still
#: yields a traceback rather than a probe that never exits.
TEARDOWN_WATCHDOG_S = 300

from gui_app import rig_setup
from gui_app.camera_manager import CameraManager
from gui_app.probe_guard import (add_force_argument,
                                 refuse_if_panopticon_running)
from gui_app.serial_controller import TeensyController
from gui_app.session_config import RigProfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--profile", default="3dpose")
    ap.add_argument("--max-lag", type=int, default=None, help="override kick_max_lag")
    ap.add_argument("--max-buffer", type=int, default=None,
                    help="override the profile's max_num_buffer")
    ap.add_argument("--no-display", action="store_true",
                    help="display_every=10**9, i.e. never build the preview frame")
    ap.add_argument("--no-kick", action="store_true", help="disable the coordinator")
    ap.add_argument("--gui-load", action="store_true",
                    help="reproduce the GUI's 30 Hz display refresh (QImage -> "
                         "QPixmap -> setPixmap for all cameras) so its main-thread "
                         "cost is present; the plain probe has none")
    ap.add_argument("--label", default="run")
    # Match production's 0.001 switch interval (gui.py:97): the default 5 ms
    # punishes an 18-thread interpreter far harder than a 6-thread one, so any
    # comparison without it measures the mitigation, not the design.
    # Always state the interval when quoting a number from this probe.
    ap.add_argument("--switch-interval", type=float, default=0.001,
                    help="sys.setswitchinterval; 0.001 matches gui.py")
    # Default None, not False: the profile decides, so a run with no flags
    # reproduces the GUI. An explicit flag overrides it in either direction.
    ap.add_argument("--pin", nargs="?", const=True, default=None,
                    help="pin grab threads to P-cores: bare flag = one core "
                         "each, 'set' = confined to the P-core set "
                         "(default: the profile's pin_capture_threads)")
    ap.add_argument("--no-pin", action="store_true",
                    help="leave grab threads unpinned even if the profile "
                         "pins them")
    ap.add_argument("--core-order", default=None,
                    help="comma-separated P-core order for camera pinning, "
                         "e.g. 10,11,12,13,22,23 to avoid the DPC-heavy cores")
    ap.add_argument("--hires-timer", action="store_true",
                    help="timeBeginPeriod(1): 1 ms system tick instead of ~15.6")
    ap.add_argument("--proc-prio", action="store_true",
                    help="HIGH_PRIORITY_CLASS for the whole process")
    ap.add_argument("--thread-prio", default="highest",
                    choices=["highest", "timecritical", "abovenormal"],
                    help="grab-thread priority tier")
    ap.add_argument("--gev-scpd", type=int, default=None,
                    help="override GevSCPD (inter-packet delay, 1 ns ticks). "
                         "Spreads each frame over more of the trigger period, "
                         "cutting the synchronised burst three cameras make "
                         "into one 10 GbE uplink.")
    ap.add_argument("--keep", action="store_true")
    add_force_argument(ap)
    args = ap.parse_args()
    # This probe opens every camera and the trigger board, so it must not
    # run beside another instance: two of them fight over the same devices
    # and the contention reads as the lag this probe exists to measure.
    refuse_if_panopticon_running(force=args.force)
    sys.setswitchinterval(args.switch_interval)
    print(f"sys.setswitchinterval({args.switch_interval})  "
          f"[gui.py uses 0.001]", flush=True)

    prof = next(RigProfile.load(p) for p in RigProfile.list_profiles()
                if p.stem == args.profile)
    max_lag = args.max_lag if args.max_lag is not None else prof.kick_max_lag
    pin = prof.pin_capture_threads
    if args.pin is not None:
        pin = args.pin
    if args.no_pin:
        pin = False
    # Anchored to the repository, never to the working directory: a probe
    # started from another shell otherwise writes its trace and its scratch
    # recordings wherever it was launched.
    out = REPO / "probe_out" / args.label
    out.mkdir(parents=True, exist_ok=True)
    scratch = REPO / "probe_out" / "_scratch"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"=== probe '{args.label}': {args.seconds:g}s, max_lag={max_lag}, "
          f"display={'off' if args.no_display else 'on'}, "
          f"kick={'off' if args.no_kick else 'on'} ===", flush=True)

    mgr = CameraManager()
    mgr.error.connect(lambda m: print(f"[cam-error] {m}", flush=True))
    # The GUI's two calls, in the GUI's order: the thread-placement flags
    # (rig_setup.MANAGER_FLAGS) and the capture-core pool are set before the
    # cameras open. open_all ends by starting the PREVIEW grab threads, and
    # each grab thread copies pin_capture_threads and pins itself against the
    # pool when it starts, so a flag set after open_all reaches the recording
    # threads (start_acquisition rebuilds them) but never the preview ones --
    # and the preview threads are the ones a profile switch leaves running.
    pool = rig_setup.apply_profile_to_manager(mgr, prof)
    mgr.pin_capture_threads = pin
    kwargs = rig_setup.open_kwargs(mgr, prof)
    if args.max_buffer:
        kwargs["max_num_buffer"] = args.max_buffer

    # Print what is actually in force. A lag number is only comparable to a GUI
    # recording when these lines match the ones the GUI logs.
    print(f"CONFIG profile      : {prof.name} ({args.profile})", flush=True)
    for key in sorted(kwargs):
        print(f"CONFIG {key:13s}: {kwargs[key]}", flush=True)
    print(f"CONFIG core pool    : {pool} "
          f"(excluding {prof.capture_core_exclude})", flush=True)
    print(f"CONFIG pinning      : pin_capture_threads={mgr.pin_capture_threads} "
          f"pin_encoder_threads={getattr(mgr, 'pin_encoder_threads', None)} "
          f"encoder_pcores={getattr(mgr, 'encoder_pcores', None)}", flush=True)
    print(f"CONFIG encode       : realtime={prof.realtime_encode} "
          f"kick={prof.realtime_kick and not args.no_kick} "
          f"max_lag={max_lag} quality={prof.quality} "
          f"{prof.frame_width}x{prof.frame_height}@{prof.frame_rate:g}",
          flush=True)

    if not mgr.open_all(**kwargs):
        print("FAILED to open cameras")
        return 1
    n = mgr.num_cameras
    print(f"opened {n} cameras", flush=True)
    # Dump every thread's stack if we wedge, so a stall is diagnosable
    # rather than a guess.
    faulthandler.dump_traceback_later(args.seconds + 40, exit=True)

    # PyNvVideoCodec's Encode() does a lazy import on first call. Six encoder
    # threads hitting that simultaneously wedges on the import machinery
    # (the whole process stalls, one thread parked in find_spec under
    # Encode()). Force it once, single-threaded, first.
    if prof.realtime_encode:
        try:
            from gui_app import nvenc
            _e = nvenc.create_h264_encoder(prof.frame_width, prof.frame_height,
                                           prof.quality, fps=prof.frame_rate)
            _e.Encode(np.full((prof.frame_height * 3 // 2, prof.frame_width),
                              128, np.uint8))
            try:
                _e.EndEncode()
            except Exception:
                pass
            del _e
            print("nvenc lazy import pre-warmed", flush=True)
        except Exception as e:
            print(f"nvenc pre-warm failed (continuing): {e}", flush=True)

    if args.gev_scpd is not None:
        for i, cam in enumerate(mgr._cameras):
            try:
                cam.GevSCPD.SetValue(args.gev_scpd)
            except Exception as e:
                print(f"[cam{i+1}] GevSCPD override failed: {e}", flush=True)
        try:
            pkt = mgr._cameras[0].GevSCPSPacketSize.GetValue()
            npkt = (prof.frame_width * prof.frame_height + pkt - 1) // pkt
            per_pkt_ns = args.gev_scpd + pkt * 8 / 5.0   # 5 Gbit/s = 1.6 ns/byte
            print(f"GevSCPD={args.gev_scpd} -> ~{npkt} packets x "
                  f"{per_pkt_ns/1000:.1f} us = {npkt*per_pkt_ns/1e6:.2f} ms per "
                  f"frame (period {1000/prof.frame_rate:.1f} ms)", flush=True)
        except Exception:
            pass

    names = mgr.camera_names if hasattr(mgr, "camera_names") else \
        [f"cam{i+1}" for i in range(n)]
    raw_paths = []
    for cn in names:
        d = scratch / cn
        d.mkdir(parents=True, exist_ok=True)
        raw_paths.append(d / "raw.bin")

    from gui_app import cpu_affinity as _ca
    if args.hires_timer:
        print(f"timeBeginPeriod(1) -> {_ca.begin_high_resolution_timers(1)}", flush=True)
    if args.proc_prio:
        print(f"HIGH_PRIORITY_CLASS -> {_ca.set_process_priority()}", flush=True)
    _ca.GRAB_THREAD_PRIORITY = {
        "highest": _ca.THREAD_PRIORITY_HIGHEST,
        "timecritical": _ca.THREAD_PRIORITY_TIME_CRITICAL,
        "abovenormal": _ca.THREAD_PRIORITY_ABOVE_NORMAL,
    }[args.thread_prio]
    print(f"grab thread priority tier: {args.thread_prio}", flush=True)
    if args.core_order:
        # Replaces the pool rig_setup derived from the profile, so say so: the
        # run is no longer comparable to a GUI recording.
        from gui_app.cpu_affinity import set_core_order
        order = [int(x) for x in args.core_order.split(",")]
        set_core_order(order)
        print(f"CONFIG core pool    : {order} (OVERRIDDEN by --core-order; "
              f"this run no longer matches the GUI)", flush=True)
    mgr.start_acquisition(
        raw_paths, display_every=10**9 if args.no_display else 10,
        realtime=prof.realtime_encode, width=prof.frame_width,
        height=prof.frame_height, quality=prof.quality, fps=prof.frame_rate,
        realtime_kick=(prof.realtime_kick and not args.no_kick),
        kick_max_lag=max_lag)

    teensy = TeensyController(port=prof.serial_port)
    if not teensy.open():
        print("FAILED to open serial")
        mgr.stop_acquisition(); mgr.close_all()
        return 1
    if not teensy.start_triggers(prof.trigger_pins, prof.frame_rate):
        print("board did not acknowledge start")
        mgr.stop_acquisition(); mgr.close_all()
        return 1

    # Sample the coordinator's view + each thread's delivery lag once a second.
    router = getattr(mgr, "_router", None)
    trace = []
    t0 = time.perf_counter()
    # Optional stand-in for main_window._refresh_displays: same 30 Hz cadence,
    # same per-camera QImage -> QPixmap -> setPixmap, on the main thread.
    labels = None
    if args.gui_load:
        from PyQt5.QtWidgets import QLabel, QWidget, QGridLayout
        holder = QWidget()
        grid = QGridLayout(holder)
        labels = [QLabel() for _ in range(n)]
        for i, lb in enumerate(labels):
            grid.addWidget(lb, i // 3, i % 3)
        holder.resize(1280, 800)
        # With QT_QPA_PLATFORM=offscreen Qt never rasterises, so setPixmap is
        # far cheaper than in the real GUI. Set QT_QPA_PLATFORM=windows to get
        # actual compositing, which is what the GUI pays for.
        if os.environ.get("QT_QPA_PLATFORM") != "offscreen":
            holder.show()
        print(f"gui-load: 30 Hz display refresh active "
              f"(platform={os.environ.get('QT_QPA_PLATFORM')})", flush=True)

    def _pump(seconds):
        """Wait, pumping Qt and (optionally) doing the GUI's display work."""
        end = time.perf_counter() + seconds
        while time.perf_counter() < end:
            if labels is not None:
                for i, fr in enumerate(mgr.latest_frames):
                    if fr is None:
                        continue
                    h, w = fr.shape[:2]
                    qimg = QImage(fr.data, w, h, w, QImage.Format_Grayscale8)
                    labels[i].setPixmap(QPixmap.fromImage(qimg))
            _QAPP.processEvents()
            time.sleep(0.033)

    last_temp_s = -1
    while time.perf_counter() - t0 < args.seconds:
        _pump(1.0)
        el = time.perf_counter() - t0
        row = {"t": round(el, 2),
               "frames": list(mgr.frame_counts),
               "fps": [round(f, 1) for f in mgr.current_fps]}
        if router is not None:
            co = router._coord
            row["frontier"] = list(co._frontier)
            row["released"] = co.released_triggers
            row["forced"] = co.forced
            row["forced_by"] = list(co.forced_by)
        # Temperature every ~5 s. These cameras have no fan and cool by
        # conduction through the mount, so how hot each gets is a property of
        # its installation -- and the question this samples is whether the
        # LAGGARD heats up before it starts falling behind, or is simply the
        # thread that lost the scheduling lottery. A cold-path register read,
        # rate-limited so it never competes with streaming.
        # The dedup keeps its own variable: `row` is rebuilt every
        # iteration, so a key stored in it can never be read back and the
        # rate limit reduces to the second boundary, which a drifting pump
        # hits twice or skips.
        if int(el) % 5 == 0 and int(el) != last_temp_s:
            last_temp_s = int(el)
            try:
                row["temps"] = [t.get("temp_c") for t in mgr.thermals()]
            except Exception:
                pass
        trace.append(row)
        if int(el) % 10 == 0 and router is not None:
            print(f"  t={el:6.1f}s  {router.lag_report()}", flush=True)
            temps = row.get("temps")
            if temps:
                print("           temps  "
                      + " ".join(f"c{i+1}:{t:.0f}" for i, t in enumerate(temps)
                                 if t is not None), flush=True)

    # The acquisition watchdog has done its job; re-arm it for teardown with
    # a budget of its own. Closing nine cameras and draining the encoders can
    # legitimately outlast the acquisition budget, and a hard kill inside
    # stop_acquisition leaves the cameras open and the serial port held, so the
    # next run cannot even open the board.
    faulthandler.cancel_dump_traceback_later()
    faulthandler.dump_traceback_later(TEARDOWN_WATCHDOG_S, exit=True)

    teensy.stop_triggers(prof.trigger_pins)
    time.sleep(0.5)
    results = mgr.stop_acquisition()
    if router is not None:
        co = router._coord
        summary = dict(released=co.released_triggers, dropped=co.dropped,
                       forced=co.forced, forced_by=list(co.forced_by),
                       max_lag=max_lag)
    else:
        summary = dict(max_lag=None)
    summary["grabbed"] = [r[0] for r in results]
    summary["args"] = vars(args)
    (out / "trace.json").write_text(json.dumps({"summary": summary,
                                                "trace": trace}, indent=1))

    print("\n--- result ---")
    print(f"grabbed per camera : {summary['grabbed']}")
    if router is not None:
        print(f"released triggers  : {summary['released']}  "
              f"forced={summary['forced']}  forced_by={summary['forced_by']}")
        f = np.array([r["frontier"] for r in trace if "frontier" in r])
        if len(f):
            lead = f.max(axis=1, keepdims=True)
            lag = lead - f
            print("per-camera lag behind leader (median / p95 / max):")
            for c in range(lag.shape[1]):
                v = lag[:, c]
                print(f"  cam{c+1}: {np.median(v):7.0f} {np.percentile(v,95):7.0f} "
                      f"{v.max():7.0f}")
    mgr.close_all()
    teensy.close()
    faulthandler.cancel_dump_traceback_later()
    if not args.keep:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"\ntrace written to {out/'trace.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
