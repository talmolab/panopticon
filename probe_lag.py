"""Headless acquisition harness for diagnosing cross-camera submission lag.

Drives the REAL code path --- CameraManager, GrabThread, SyncEncodeRouter,
FrameSyncCoordinator, TeensyController --- so anything found here applies to the
GUI. The only thing missing is Qt.

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

sys.path.insert(0, str(Path(__file__).parent))

import numpy as np

# GrabThread is a QThread and CameraManager emits pyqtSignals. Without a
# QApplication the thread machinery wedges partway through a recording (observed
# 2026-08-11), so create an offscreen one --- this also keeps the probe faithful to
# how the GUI actually runs.
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QImage, QPixmap
_QAPP = QApplication.instance() or QApplication([])

from gui_app.camera_manager import CameraManager
from gui_app.serial_controller import TeensyController
from gui_app.session_config import RigProfile


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=90)
    ap.add_argument("--profile", default="3dpose")
    ap.add_argument("--max-lag", type=int, default=None, help="override kick_max_lag")
    ap.add_argument("--max-buffer", type=int, default=None, help="override MaxNumBuffer")
    ap.add_argument("--no-display", action="store_true",
                    help="display_every=10**9, i.e. never build the preview frame")
    ap.add_argument("--no-kick", action="store_true", help="disable the coordinator")
    ap.add_argument("--gui-load", action="store_true",
                    help="reproduce the GUI's 30 Hz display refresh (QImage -> "
                         "QPixmap -> setPixmap for all cameras) so its main-thread "
                         "cost is present; the plain probe has none")
    ap.add_argument("--label", default="run")
    # PRODUCTION SETS 0.001 (gui.py:97) and this probe did not, which silently
    # invalidated a 2026-09-10 threads-vs-processes comparison: the default 5 ms
    # punishes an 18-thread interpreter far harder than a 6-thread one, so the
    # single-process arm was measured with its own mitigation switched off.
    # Always state the interval when quoting a number from this probe.
    ap.add_argument("--switch-interval", type=float, default=0.001,
                    help="sys.setswitchinterval; 0.001 matches gui.py")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    sys.setswitchinterval(args.switch_interval)
    print(f"sys.setswitchinterval({args.switch_interval})  "
          f"[gui.py uses 0.001]", flush=True)

    prof = next(RigProfile.load(p) for p in RigProfile.list_profiles()
                if p.stem == args.profile)
    max_lag = args.max_lag if args.max_lag is not None else prof.kick_max_lag
    out = Path("probe_out") / args.label
    out.mkdir(parents=True, exist_ok=True)
    scratch = Path("probe_out") / "_scratch"
    if scratch.exists():
        shutil.rmtree(scratch, ignore_errors=True)

    print(f"=== probe '{args.label}': {args.seconds:g}s, max_lag={max_lag}, "
          f"max_buffer={args.max_buffer or 'default'}, "
          f"display={'off' if args.no_display else 'on'}, "
          f"kick={'off' if args.no_kick else 'on'} ===", flush=True)

    mgr = CameraManager()
    mgr.error.connect(lambda m: print(f"[cam-error] {m}", flush=True))
    if not mgr.open_all(prof.pfs_path, gige_driver=prof.gige_driver,
                        trigger_rate_limit=prof.trigger_rate_limit):
        print("FAILED to open cameras")
        return 1
    n = mgr.num_cameras
    print(f"opened {n} cameras", flush=True)
    # Dump every thread's stack if we wedge, so a stall is diagnosable
    # rather than a guess.
    faulthandler.dump_traceback_later(args.seconds + 40, exit=True)

    # PyNvVideoCodec's Encode() does a lazy import on first call. Six encoder
    # threads hitting that simultaneously wedges on the import machinery
    # (observed 2026-08-11: whole process stalled, one thread parked in
    # find_spec under Encode()). Force it once, single-threaded, first.
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

    if args.max_buffer:
        for i, cam in enumerate(mgr._cameras):
            try:
                cam.MaxNumBuffer.SetValue(args.max_buffer)
            except Exception as e:
                print(f"[cam{i+1}] MaxNumBuffer override failed: {e}", flush=True)

    names = mgr.camera_names if hasattr(mgr, "camera_names") else \
        [f"cam{i+1}" for i in range(n)]
    raw_paths = []
    for cn in names:
        d = scratch / cn
        d.mkdir(parents=True, exist_ok=True)
        raw_paths.append(d / "raw.bin")

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
        if int(el) % 5 == 0 and int(el) != row.get("_last_temp_s", -1):
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
    if not args.keep:
        shutil.rmtree(scratch, ignore_errors=True)
    print(f"\ntrace written to {out/'trace.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
