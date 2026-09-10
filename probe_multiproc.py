"""Does splitting acquisition across PROCESSES recover the timing margin?

At nine cameras the grab loops have ~3 ms of slack in a 10 ms window, and the
laggard rotates (cam9, then cam5, then cam4 on three consecutive sessions) --
the signature of a scheduling lottery rather than a bad camera. Threads were
chosen over processes for Qt convenience, not performance; campy, the lineage
this comes from, is multiprocess.

The question this answers is narrow and measurable: **with the same nine
cameras, does putting the grab loops in separate processes change `cycle` and
slack?** Each worker runs the REAL path -- CameraManager, GrabThread,
SyncEncodeRouter -- over its own share of cameras, selected by serial so every
worker agrees on the same cam1..camN ordering.

What this prototype deliberately does NOT do: cross-process frame
synchronisation. Each worker coordinates only its own cameras, so the output is
not globally trigger-aligned and must not be used for real data. Solving that
needs one authoritative coordinator publishing decisions to all workers (the
frontier is nine int64s, so the channel is tiny) -- worth building only if the
timing below justifies it.

    uv run probe_multiproc.py --workers 3 --seconds 60
    uv run probe_multiproc.py --workers 1 --seconds 60    # control
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).parent


def worker(idx, serials, seconds, profile_name, q):
    """One acquisition process owning `serials`. Reports timing back on `q`."""
    import os
    sys.path.insert(0, str(REPO))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PyQt5.QtWidgets import QApplication
    from gui_app.camera_manager import CameraManager
    from gui_app.session_config import RigProfile

    app = QApplication.instance() or QApplication(sys.argv[:1])   # noqa: F841

    paths = {p.stem: p for p in RigProfile.list_profiles()}
    prof = RigProfile.load(paths[profile_name])

    mgr = CameraManager()
    ok = mgr.open_all(prof.pfs_path, gige_driver=prof.gige_driver,
                      trigger_rate_limit=prof.trigger_rate_limit,
                      expect_cameras=0,
                      max_num_buffer=prof.max_num_buffer,
                      only_serials=serials)
    if not ok:
        q.put({"worker": idx, "error": "open_all failed"})
        return

    out = Path(REPO / "probe_out" / "multiproc" / f"w{idx}")
    out.mkdir(parents=True, exist_ok=True)
    paths_out = [out / f"cam{i}" for i in range(len(serials))]
    for p in paths_out:
        p.mkdir(exist_ok=True)

    mgr.start_acquisition(
        [p / "stream.h264" for p in paths_out],
        display_every=10, realtime=prof.realtime_encode,
        width=prof.frame_width, height=prof.frame_height,
        quality=prof.quality, fps=prof.frame_rate,
        realtime_kick=prof.realtime_kick, kick_max_lag=prof.kick_max_lag)

    q.put({"worker": idx, "ready": True})
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        time.sleep(0.5)

    res = mgr.stop_acquisition()
    lags = list(mgr.delivery_lags)
    q.put({"worker": idx, "serials": serials,
           "frames": [getattr(r, "frames", None) for r in res] if res else None,
           "delivery_lags": lags})
    try:
        mgr.close_all()
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--profile", default="3dpose")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO))
    from gui_app.backends import load_backend

    be = load_backend("basler")
    serials = [d.GetSerialNumber() for d in be.enumerate_devices()]
    if not serials:
        print("no cameras")
        return 1
    print(f"{len(serials)} cameras across {args.workers} process(es)")

    # Contiguous blocks, so each worker owns cameras that share a switch.
    per = -(-len(serials) // args.workers)
    groups = [serials[i:i + per] for i in range(0, len(serials), per)]
    for i, g in enumerate(groups):
        print(f"  worker {i}: {', '.join(g)}")

    q = mp.Queue()
    procs = [mp.Process(target=worker, args=(i, g, args.seconds, args.profile, q),
                        daemon=False) for i, g in enumerate(groups)]
    t0 = time.perf_counter()
    for p in procs:
        p.start()

    msgs = []
    deadline = time.perf_counter() + args.seconds + 180
    while len(msgs) < 2 * len(procs) and time.perf_counter() < deadline:
        try:
            msgs.append(q.get(timeout=5))
        except Exception:
            if not any(p.is_alive() for p in procs):
                break
    for p in procs:
        p.join(timeout=30)
        if p.is_alive():
            p.terminate()

    print(f"\nwall {time.perf_counter()-t0:.1f}s")
    for m in msgs:
        if "error" in m:
            print(f"  worker {m['worker']}: ERROR {m['error']}")
        elif "delivery_lags" in m:
            dl = [f"{v:+.3f}" for v in m["delivery_lags"]]
            print(f"  worker {m['worker']}: delivery_lag {dl}")
    print("\nRead `cycle` and `avg_wait` from the per-grab-thread lines above:")
    print("  threaded 9-camera baseline was cycle ~10.0-10.1, slack 2.9-4.3 ms")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
