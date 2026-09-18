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


def worker(idx, serials, seconds, profile_name, q, switch_interval):
    """One acquisition process owning `serials`. Reports timing back on `q`."""
    import os
    sys.path.insert(0, str(REPO))
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Per-process: each interpreter has its own switch interval, and comparing
    # against the threaded arm is only meaningful when both use production's.
    sys.setswitchinterval(switch_interval)

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

    # G3: report the actual block IDs this group kept. Without this the probe
    # measures TIMING only and says nothing about alignment -- which is the one
    # thing a process split endangers, because each worker's coordinator sees
    # only its own three cameras. The parent intersects across groups below.
    import numpy as np
    blocks = {}
    for c, p in enumerate(paths_out):
        f = p / "blockids.npy"
        if f.exists():
            b = np.load(f)
            blocks[serials[c]] = (int(b[0]), int(b[-1]), int(b.size),
                                  int(np.asarray(b).sum()))
    q.put({"worker": idx, "serials": serials,
           # stop_acquisition returns (frames, timestamps, block_ids)
           # tuples, so the count is element 0; an attribute lookup on a
           # tuple silently yields None and blanks the whole report.
           "frames": [r[0] for r in res] if res else None,
           "delivery_lags": lags, "blocks": blocks})
    try:
        mgr.close_all()
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--profile", default="3dpose")
    ap.add_argument("--switch-interval", type=float, default=0.001,
                    help="sys.setswitchinterval in each worker; gui.py uses 0.001")
    from gui_app.probe_guard import (add_force_argument,
                                     refuse_if_panopticon_running)
    add_force_argument(ap)
    args = ap.parse_args()
    # The workers open every camera and the parent owns the trigger board,
    # so no other instance may be running: shared devices make the timing
    # this probe reports describe the contention rather than the split.
    refuse_if_panopticon_running(force=args.force)
    print(f"switch interval {args.switch_interval} per worker  [gui.py uses 0.001]")

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

    # The trigger board is ONE serial port, so exactly one process may own it:
    # the parent. Workers arm their cameras and report ready; only then do the
    # triggers start. That ordering is not a nicety — a camera armed AFTER the
    # first pulse misses those triggers, so its block ID 1 is trigger N+1 and it
    # reads as permanently N frames behind, which is precisely the corruption
    # this split has to avoid. (The first version of this probe drove no board
    # at all and reported Total_Packet_Count=0 on every camera; any number it
    # produced described a trigger state nobody had set.)
    from gui_app.serial_controller import TeensyController
    from gui_app.session_config import RigProfile
    paths = {p.stem: p for p in RigProfile.list_profiles()}
    prof = RigProfile.load(paths[args.profile])
    teensy = TeensyController(port=prof.serial_port)

    q = mp.Queue()
    procs = [mp.Process(target=worker,
                        args=(i, g, args.seconds, args.profile, q,
                              args.switch_interval),
                        daemon=False) for i, g in enumerate(groups)]
    t0 = time.perf_counter()
    for p in procs:
        p.start()

    # Startup barrier: wait for every worker's "ready" before any trigger fires.
    msgs = []
    ready = 0
    barrier_deadline = time.perf_counter() + 240
    while ready < len(procs) and time.perf_counter() < barrier_deadline:
        try:
            m = q.get(timeout=5)
        except Exception:
            if not any(p.is_alive() for p in procs):
                break
            continue
        if m.get("ready"):
            ready += 1
            print(f"  worker {m['worker']} armed ({ready}/{len(procs)})")
        else:
            msgs.append(m)
    if ready < len(procs):
        print(f"only {ready}/{len(procs)} workers armed — aborting")
        for p in procs:
            p.terminate()
        return 1

    if not teensy.open():
        print("failed to open the trigger board serial port — aborting")
        for p in procs:
            p.terminate()
        return 1
    if not teensy.start_triggers(prof.trigger_pins, prof.frame_rate):
        print("trigger board did not acknowledge — aborting")
        for p in procs:
            p.terminate()
        return 1
    print(f"triggers running at {prof.frame_rate} fps")

    deadline = time.perf_counter() + args.seconds + 180
    while len(msgs) < len(procs) and time.perf_counter() < deadline:
        try:
            msgs.append(q.get(timeout=5))
        except Exception:
            if not any(p.is_alive() for p in procs):
                break
    teensy.stop_triggers(prof.trigger_pins)
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
    # --- G3: did the groups actually stay aligned with each other? -----------
    allb = {}
    for m in msgs:
        allb.update(m.get("blocks") or {})
    print("\nper-camera block IDs (first, last, count):")
    for s in sorted(allb):
        f, l, n_, _ = allb[s]
        print(f"  {s}: first={f} last={l} count={n_}")
    if len(allb) >= 2:
        counts = {v[2] for v in allb.values()}
        spans = {(v[0], v[1]) for v in allb.values()}
        sums = {v[3] for v in allb.values()}
        same = len(counts) == 1 and len(spans) == 1 and len(sums) == 1
        print(f"\n  ALIGNMENT ACROSS GROUPS: "
              f"{'IDENTICAL' if same else '*** DIVERGED ***'}")
        if not same:
            print(f"    counts {sorted(counts)}  spans {sorted(spans)}")
            print("    Expected while each worker coordinates only its own"
                  " cameras — this is the gap a cross-process coordinator"
                  " would close, and the reason the prototype is not usable"
                  " for real data.")
    print("\nRead `cycle` and `avg_wait` from the per-grab-thread lines above.")
    return 0


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
