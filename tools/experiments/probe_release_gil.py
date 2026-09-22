"""Does `result.Release()` hold the GIL?

Hypothesis: if Release() holds the GIL for ~1 ms, then nine cameras need 9 ms of
a 10 ms window on that call alone and the pipeline saturates on it, independently
of everything else. The supporting hint is that in laggard episodes the laggard's
`rel` has run 3-4 ms against ~1 ms on healthy cameras -- the largest per-camera
divergence in the instrumentation block. (See docs/HISTORY.md, phase 6.)

Two measurements, because neither alone is conclusive:

1. **exec vs wall**, via QueryThreadCycleTime. Wall time around a GIL-RELEASING
   call includes the re-acquisition wait; executed cycles do not. A call that
   HOLDS the GIL has exec ~= wall even under contention, because the thread is
   running the whole time. This is the same split that showed `copy` was mostly
   wait (E1a/E2) and that `disp` was too.

2. **Thread scaling.** The same work in N concurrent threads: aggregate
   throughput rises if the call releases the GIL, stays flat if it does not.
   This is the decisive one, and it needs no assumptions about timers.

Run with the cameras free and the machine otherwise quiet -- analysis running
alongside a measurement has invalidated a conclusion on this rig before.

    uv run tools/experiments/probe_release_gil.py                 # all cameras
    uv run tools/experiments/probe_release_gil.py --cams 1        # single-camera control
"""
from __future__ import annotations

import argparse
import statistics
import sys
import threading
import time
from pathlib import Path

#: The repository root, three levels up from tools/experiments/. Output and
#: imports are anchored to it, never to the working directory, so a run
#: started from anywhere reads the same package and writes to one place.
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from gui_app.backends import load_backend
from gui_app.probe_guard import (add_force_argument,
                                 refuse_if_panopticon_running)
from gui_app.session_config import RigProfile
from tools.perfclock import calibrate_cycles_per_s, thread_cycles as cycles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cams", type=int, default=0, help="0 = all enumerated")
    ap.add_argument("--frames", type=int, default=600, help="per camera")
    ap.add_argument("--profile", default="3dpose")
    add_force_argument(ap)
    args = ap.parse_args()
    # Opens every camera, so another instance holding them makes both the
    # thread-scaling arm and the exec/wall split meaningless.
    refuse_if_panopticon_running(force=args.force)

    paths = {p.stem: p for p in RigProfile.list_profiles()}
    prof = RigProfile.load(paths.get(args.profile, next(iter(paths.values()))))

    cps = calibrate_cycles_per_s()
    print(f"cycles/s ~= {cps/1e9:.2f}e9\n")

    be = load_backend("basler")
    devices = be.enumerate_devices()
    if args.cams:
        devices = devices[:args.cams]
    n = len(devices)
    print(f"{n} camera(s), {args.frames} frames each, free-run at {prof.frame_rate} fps\n")

    cams = []
    for i, dev in enumerate(devices):
        c = be.open(dev, prof.pfs_path, 250)
        be.select_gige_driver(i, c, prof.gige_driver)
        be.set_freerun(c, prof.frame_rate)
        cams.append(c)

    # per-camera accumulators
    rel_wall, rel_exec = [[] for _ in cams], [[] for _ in cams]
    cpy_wall, cpy_exec = [[] for _ in cams], [[] for _ in cams]
    start = threading.Barrier(n)

    def run(idx, cam):
        import numpy as np
        buf = np.full((prof.frame_height * 3 // 2, prof.frame_width), 128, np.uint8)
        be.start_grabbing(cam)
        start.wait()
        got = 0
        while got < args.frames:
            try:
                res = be.retrieve(cam, 2000)
            except be.TimeoutException:
                continue
            if not res.GrabSucceeded():
                res.Release()
                continue
            with res.GetArrayZeroCopy() as img:
                c0, w0 = cycles(), time.perf_counter()
                buf[:prof.frame_height, :] = img
                cpy_wall[idx].append((time.perf_counter() - w0) * 1000)
                cpy_exec[idx].append((cycles() - c0) / cps * 1000)
                del img
            c0, w0 = cycles(), time.perf_counter()
            res.Release()
            rel_wall[idx].append((time.perf_counter() - w0) * 1000)
            rel_exec[idx].append((cycles() - c0) / cps * 1000)
            got += 1
        be.stop_grabbing(cam)

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(cams)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0

    for c in cams:
        try:
            be.close(c)
        except Exception:
            pass

    def med(lists):
        flat = [v for L in lists for v in L]
        return statistics.median(flat) if flat else float("nan")

    rw, re_ = med(rel_wall), med(rel_exec)
    cw, ce = med(cpy_wall), med(cpy_exec)

    print(f"{'call':<26}{'wall ms':>10}{'exec ms':>10}{'wait ms':>10}{'exec/wall':>11}")
    print(f"{'result.Release()':<26}{rw:10.3f}{re_:10.3f}{rw-re_:10.3f}{re_/rw:11.2f}")
    print(f"{'NV12 copy (reference)':<26}{cw:10.3f}{ce:10.3f}{cw-ce:10.3f}{ce/cw:11.2f}")

    print(f"\nwall clock {elapsed:.1f}s for {args.frames} frames/camera")
    print(f"GIL-held budget if Release HOLDS it: {n} x {re_:.3f} = "
          f"{n*re_:.2f} ms per 10 ms window")

    print("\nreading:")
    print("  exec/wall near 1.0  -> the call HOLDS the GIL (it executes throughout)")
    print("  exec/wall well <1.0 -> it RELEASES and the bracket is mostly waiting")
    print("  Compare against --cams 1, where there is no contention to wait for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
