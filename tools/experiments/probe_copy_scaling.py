"""Microbenchmark: is the gray->NV12 copy GIL-bound, and what explains 850 MB/s?

No cameras needed. Answers three questions that gate the 9-camera architecture:

  Q1  Does `nv12_buf[:h, :] = gray` scale across threads?
      If per-op time stays flat as threads grow, numpy releases the GIL during the
      copy and the copy is NOT the serialized term -> threads can reach 9 cameras.
      If per-op time grows ~linearly with thread count, it is GIL-bound and the
      grab threads MUST be split across processes.

  Q2  Does ring size explain the measured 2.7 ms / ~850 MB/s?
      The production NV12 ring is `max_lag + ENCODE_QUEUE_DEPTH + 64` buffers
      (GrabThread's ring allocation) = 744 x 3.456 MB = 2.57 GB per camera. A destination
      that large is never cache-resident and page-faults on first touch. Compared
      here: small (cache-friendly) vs large-cold vs large-prefaulted.

  Q3  What is the floor? i.e. how fast can this copy possibly be, so we know how
      much of the 10 ms/frame budget is recoverable.

Controls are included so a scaling number can be trusted:
  - `sleep`  : releases the GIL entirely -> must show perfect scaling. Validates
               that the harness can detect parallelism at all.
  - `pyloop` : pure Python bytecode -> must show ~linear slowdown. Validates that
               the harness can detect serialization at all.
If either control misbehaves, ignore the copy numbers; the harness is lying.

    uv run tools/experiments/probe_copy_scaling.py
    uv run tools/experiments/probe_copy_scaling.py --threads 1,3,6,9,12 --iters 200
"""
import argparse
import json
import statistics
import threading
import time
from pathlib import Path

import numpy as np

#: The repository root, three levels up from tools/experiments/. Output is
#: anchored to it, never to the working directory, so a run started from
#: anywhere writes to one place.
REPO = Path(__file__).resolve().parents[2]

W, H = 1920, 1200
FRAME_BYTES = W * H                      # mono8 source frame = 2.304 MB
NV12_H = H * 3 // 2                      # NV12 = Y plane (=gray) + UV plane
NV12_BYTES = W * NV12_H                  # 3.456 MB per buffer
PAGE = 4096


def make_ring(n_buf, prefault):
    """Allocate an NV12 ring the way production does, optionally pre-faulted.

    np.empty does not touch pages, so the first write to each buffer takes a
    page fault (and a cold cache line). Pre-faulting means paying that once at
    allocation instead of on the capture hot path.
    """
    ring = [np.empty((NV12_H, W), np.uint8) for _ in range(n_buf)]
    for b in ring:
        # UV plane is a constant 128 in production; written once, never again.
        b[H:, :] = 128
        if prefault:
            # Touch every page of the Y plane so the fault is not paid later.
            b.reshape(-1)[::PAGE] = 0
    return ring


def op_copy(ring, src, iters, ring_i=0):
    """The operation under test: exactly what grab_thread does per frame."""
    n = len(ring)
    ts = []
    for _ in range(iters):
        buf = ring[ring_i]
        ring_i = (ring_i + 1) % n
        t0 = time.perf_counter()
        buf[:H, :] = src
        ts.append(time.perf_counter() - t0)
    return ts


def op_sleep(ring, src, iters, ring_i=0):
    """Control: fully GIL-releasing. Must scale perfectly."""
    ts = []
    for _ in range(iters):
        t0 = time.perf_counter()
        time.sleep(0.002)
        ts.append(time.perf_counter() - t0)
    return ts


def op_pyloop(ring, src, iters, ring_i=0):
    """Control: pure Python bytecode, holds the GIL. Must serialize."""
    ts = []
    for _ in range(iters):
        t0 = time.perf_counter()
        x = 0
        for i in range(20000):
            x += i
        ts.append(time.perf_counter() - t0)
    return ts


OPS = {"copy": op_copy, "sleep": op_sleep, "pyloop": op_pyloop}


def run_scaled(op_name, n_threads, n_buf, prefault, iters):
    """Run `op` in n_threads concurrently; return per-op ms stats."""
    op = OPS[op_name]
    # Each thread gets its own ring and source, exactly like per-camera state.
    rings = [make_ring(n_buf, prefault) for _ in range(n_threads)]
    srcs = [np.full((H, W), 64 + i, np.uint8) for i in range(n_threads)]
    barrier = threading.Barrier(n_threads)
    results = [None] * n_threads

    def worker(k):
        barrier.wait()            # start together, so contention is real
        results[k] = op(rings[k], srcs[k], iters)

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n_threads)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0

    allts = [t * 1e3 for r in results for t in r]
    allts.sort()
    total_ops = n_threads * iters
    return {
        "op": op_name, "threads": n_threads, "buffers": n_buf,
        "prefault": prefault,
        "ring_gb": round(n_buf * NV12_BYTES / 2**30, 3),
        "median_ms": round(statistics.median(allts), 3),
        "p95_ms": round(allts[int(len(allts) * 0.95)], 3),
        "wall_s": round(wall, 3),
        "agg_ops_per_s": round(total_ops / wall, 1),
        # For the copy op, throughput of useful bytes moved.
        "agg_GBps": round(total_ops * FRAME_BYTES / wall / 2**30, 2),
    }


def show(rows, title):
    print(f"\n=== {title} ===")
    print(f"{'op':7} {'thr':>4} {'ring':>8} {'pf':>3} "
          f"{'median':>9} {'p95':>9} {'ops/s':>9} {'GB/s':>7}")
    for r in rows:
        print(f"{r['op']:7} {r['threads']:4d} {r['ring_gb']:7.2f}G "
              f"{'Y' if r['prefault'] else 'N':>3} "
              f"{r['median_ms']:8.3f}m {r['p95_ms']:8.3f}m "
              f"{r['agg_ops_per_s']:9.1f} {r['agg_GBps']:7.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", default="1,3,6,9",
                    help="comma list of thread counts to test")
    ap.add_argument("--iters", type=int, default=150,
                    help="ops per thread")
    ap.add_argument("--ring-small", type=int, default=4,
                    help="cache-friendly ring depth")
    ap.add_argument("--ring-big", type=int, default=200,
                    help="per-thread ring depth for the large case. 200 x 3.456 MB"
                         " = 691 MB/thread; kept below production 744 so that"
                         " 9 threads stay ~6.2 GB instead of 23 GB.")
    ap.add_argument("--out",
                    default=str(REPO / "probe_out" / "copy_scaling.json"))
    args = ap.parse_args()

    tcounts = [int(x) for x in args.threads.split(",")]
    print(f"frame={W}x{H} mono8 = {FRAME_BYTES/1e6:.3f} MB   "
          f"NV12 buffer = {NV12_BYTES/1e6:.3f} MB")
    print(f"production ring = 744 buf = {744*NV12_BYTES/2**30:.2f} GB/camera "
          f"(max_lag 480 + queue 200 + 64)")

    rows = []

    # --- controls: prove the harness can see parallelism AND serialization ---
    ctrl = []
    for op in ("sleep", "pyloop"):
        for n in (1, max(tcounts)):
            ctrl.append(run_scaled(op, n, 2, True, 40 if op == "sleep" else 25))
    show(ctrl, "CONTROLS (sleep must stay flat; pyloop must inflate)")
    rows += ctrl

    # --- Q1/Q2: the real copy, across thread counts and ring configs ---
    for label, n_buf, pf in (("small ring", args.ring_small, True),
                             ("large ring, cold", args.ring_big, False),
                             ("large ring, prefaulted", args.ring_big, True)):
        sub = [run_scaled("copy", n, n_buf, pf, args.iters) for n in tcounts]
        show(sub, f"COPY - {label}")
        rows += sub

    # --- Q2b: single-thread ring-size sweep, up to full production size ---
    sweep = [run_scaled("copy", 1, nb, False, 60) for nb in (4, 50, 200, 744)]
    show(sweep, "COPY - single thread, ring-size sweep (cold)")
    rows += sweep

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    print(f"\nwrote {out}")

    # --- verdict ---
    c = {r["threads"]: r for r in rows
         if r["op"] == "copy" and r["buffers"] == args.ring_big and not r["prefault"]}
    if 1 in c and max(tcounts) in c:
        lo, hi = c[1]["median_ms"], c[max(tcounts)]["median_ms"]
        ratio = hi / lo if lo else 0
        print(f"\nVERDICT: copy median {lo:.3f} ms @1 thread -> {hi:.3f} ms "
              f"@{max(tcounts)} threads  (x{ratio:.2f})")
        if ratio > max(tcounts) * 0.6:
            print("  => GIL-BOUND / serialized. Threads cannot reach 9 cameras;")
            print("     the per-camera process split is MANDATORY.")
        elif ratio < 2.0:
            print("  => SCALES. The copy releases the GIL; it is not the")
            print("     serialized term. Attack memory locality, not the GIL.")
        else:
            print("  => PARTIAL scaling; memory-bandwidth bound rather than GIL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
