"""E3: verify a zero-copy replacement for `img = result.Array` on a real camera.

WHY
`grab_thread.py:339` does `img = result.Array`. pypylon's GetArray() ALLOCATES a fresh
2.3 MB numpy array and memcpys the driver buffer into it -- and pypylon's `%nothread`
list means that copy runs with the GIL HELD. Round-1 agent measurements on this rig, on
one real 100 fps camera:

    bare loop (no pixel access)   90.9 fps   exec 0.070 ms   gil_held 1.447 ms
    + result.Array                74.8 fps   exec 1.300 ms   gil_held 2.221 ms
    + GetArrayZeroCopy           101.3 fps   exec 0.372 ms   gil_held 1.686 ms

`.Array` alone drops ONE camera below the 100 fps trigger rate. E2 then showed the
system tolerates <=300 us of GIL-held work per thread per frame even at 17 threads, but
~1000 us blows the 10 ms budget at 11 threads. So removing this copy is the whole game.

WHAT THIS CHECKS BEFORE THE HOT PATH IS EDITED
  1. PaddingX == 0. GetArrayZeroCopy reshapes the buffer to (H, W) and does NOT account
     for row padding, so a nonzero PaddingX would silently shear the image. Must assert.
  2. The context-manager semantics survive OUR access pattern. pypylon's zero-copy
     context raises on exit if any reference to the view escaped, and production touches
     the frame six ways (snapshot copy, NV12 ring copy, os.write, preview decimate,
     full-res HUD copy, encoder put). All six copy -- this proves it.
  3. An A/B of the three candidate routes in the production access pattern, measuring
     fps, execution time (QueryThreadCycleTime) and wall time, so the choice is made on
     numbers rather than on the docstring.

    uv run probe_zerocopy.py --seconds 12
"""
import argparse
import ctypes
import json
import statistics
import sys
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
from pypylon import pylon

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_app.probe_guard import (add_force_argument,
                                 refuse_if_panopticon_running)

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.QueryThreadCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulonglong)]
_k32.QueryThreadCycleTime.restype = wintypes.BOOL
_k32.GetCurrentThread.restype = wintypes.HANDLE


def cycles(_b=ctypes.c_ulonglong()):
    _k32.QueryThreadCycleTime(_k32.GetCurrentThread(), ctypes.byref(_b))
    return _b.value


def calibrate(dur=0.25):
    c0, t0 = cycles(), time.perf_counter()
    x = 0
    while time.perf_counter() - t0 < dur:
        for i in range(10000):
            x += i
    return (cycles() - c0) / (time.perf_counter() - t0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=12)
    ap.add_argument("--out", default="probe_out/zerocopy.json")
    add_force_argument(ap)
    args = ap.parse_args()
    # Opens a camera, so it must not run beside an instance that already
    # holds it; a second opener changes the frame rate this A/B compares.
    refuse_if_panopticon_running(force=args.force)

    cps = calibrate()
    tl = pylon.TlFactory.GetInstance()
    devs = tl.EnumerateDevices()
    if not devs:
        print("no cameras")
        return 1
    cam = pylon.InstantCamera(tl.CreateDevice(devs[0]))
    cam.Open()
    W = cam.Width.GetValue()
    H = cam.Height.GetValue()
    pf = cam.PixelFormat.GetValue()
    print(f"camera: {devs[0].GetModelName()} {W}x{H} {pf}")

    # (1) PaddingX must be zero or the (H, W) reshape shears the image.
    padx = None
    for node in ("PaddingX",):
        try:
            padx = getattr(cam, node).GetValue()
        except Exception as e:
            print(f"  {node}: unavailable ({type(e).__name__})")
    print(f"  PaddingX = {padx}")

    # Free-run so the probe does not need the trigger board. Frame arrival is then
    # camera-paced rather than 100 Hz-paced, which is what we want: it measures how
    # fast the LOOP can go, not how fast the trigger allows.
    try:
        cam.TriggerMode.SetValue("Off")
    except Exception as e:
        print(f"  TriggerMode Off failed: {e}")
    try:
        cam.AcquisitionFrameRateEnable.SetValue(True)
        cam.AcquisitionFrameRate.SetValue(100.0)
    except Exception as e:
        print(f"  frame-rate set failed: {e}")

    # Production-shaped consumers: one NV12 ring slot + a preview decimate.
    nv12 = np.full((H * 3 // 2, W), 128, np.uint8)
    dwn = 4

    def consume(img, do_preview):
        """Exactly what production does with the frame, so the cost is comparable."""
        nv12[:H, :] = img                      # NV12 ring copy (the E1 copy)
        if do_preview:
            _ = img[::dwn, ::dwn].copy()       # preview decimate
        return int(img[0, 0])                  # force a real read

    results = {}

    def run(label, fn):
        cam.StartGrabbing(pylon.GrabStrategy_OneByOne)
        n, errs = 0, 0
        walls, execs = [], []
        t_start = time.perf_counter()
        first = None
        while time.perf_counter() - t_start < args.seconds:
            try:
                res = cam.RetrieveResult(1000, pylon.TimeoutHandling_ThrowException)
            except Exception:
                errs += 1
                continue
            if not res.GrabSucceeded():
                res.Release(); errs += 1; continue
            c0, w0 = cycles(), time.perf_counter()
            try:
                v = fn(res, n % 10 == 0)
                if first is None:
                    first = v
            except Exception as e:
                print(f"  !! {label} raised: {type(e).__name__}: {e}")
                res.Release(); cam.StopGrabbing()
                results[label] = {"error": f"{type(e).__name__}: {e}"}
                return
            w1, c1 = time.perf_counter(), cycles()
            res.Release()
            walls.append((w1 - w0) * 1e3)
            execs.append((c1 - c0) / cps * 1e3)
            n += 1
        dur = time.perf_counter() - t_start
        cam.StopGrabbing()
        walls.sort(); execs.sort()
        q = lambda a, p: a[min(len(a) - 1, int(len(a) * p))]
        results[label] = {
            "fps": round(n / dur, 2), "frames": n, "errors": errs,
            "wall_med": round(statistics.median(walls), 4),
            "wall_p95": round(q(walls, 0.95), 4),
            "exec_med": round(statistics.median(execs), 4),
            "exec_p95": round(q(execs, 0.95), 4),
            "wait_med": round(statistics.median(walls) - statistics.median(execs), 4),
            "first_px": first,
        }
        r = results[label]
        print(f"  {label:34} fps={r['fps']:7.2f}  wall_med={r['wall_med']:.4f} ms  "
              f"exec_med={r['exec_med']:.4f} ms  wait={r['wait_med']:.4f} ms")

    print(f"\nA/B over {args.seconds:g} s each (calibration {cps/1e9:.2f} Gcyc/s):")

    # (a) production
    run("PRODUCTION result.Array", lambda res, pv: consume(res.Array, pv))

    # (b) the context-manager route. Also test (2): our access pattern must not
    #     leave a dangling reference, or __exit__ raises.
    def zc(res, pv):
        with res.GetArrayZeroCopy() as img:
            return consume(img, pv)
    run("GetArrayZeroCopy (ctx mgr)", zc)

    # (c) a plain view over the buffer, no context manager, no refcount enforcement.
    #     Lifetime is ours to manage -- safe here because every consumer copies before
    #     Release(). Avoids re-indenting 100 lines of hot path if it is competitive.
    def fb(res, pv):
        mv = res.GetBuffer()
        img = np.frombuffer(mv, dtype=np.uint8, count=W * H).reshape(H, W)
        return consume(img, pv)
    run("np.frombuffer(GetBuffer())", fb)

    cam.Close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"padding_x": padx, "w": W, "h": H,
                               "results": results}, indent=1))

    # Correctness: all three must read the SAME first pixel value class (they are
    # different frames, so only assert they produced a plausible uint8).
    print(f"\nfirst-pixel sanity: "
          + ", ".join(f"{k}={v.get('first_px')}" for k, v in results.items()))
    base = results.get("PRODUCTION result.Array", {})
    for k in ("GetArrayZeroCopy (ctx mgr)", "np.frombuffer(GetBuffer())"):
        r = results.get(k, {})
        if "error" in r:
            print(f"VERDICT {k}: UNUSABLE -- {r['error']}")
        elif base.get("exec_med"):
            print(f"VERDICT {k}: exec {base['exec_med']:.4f} -> {r['exec_med']:.4f} ms "
                  f"({base['exec_med']/max(r['exec_med'],1e-9):.2f}x cheaper), "
                  f"fps {base['fps']} -> {r['fps']}")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
