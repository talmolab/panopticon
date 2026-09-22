"""Separate "time executing" from "time waiting for the GIL", and test whether
GIL contention alone can inflate a 0.08 ms copy into the 2.7 ms production reading.
(Findings and the acceptance budget: docs/HISTORY.md, phase 5.)

WHY THIS EXISTS
The production grab loop timed `nv12_buf[:H,:] = gray` at 2.7 ms/frame. The copy is
~0.080 ms on a warm ring and numpy RELEASES the GIL for it; the production ring is
already warm (np.full writes every byte), so page faults are ruled out, and 9 cameras
need only 2.76 GB/s against a ~24 GB/s bandwidth ceiling, so bandwidth is ruled out.
The surviving explanation is that numpy releases the GIL for the
memcpy and must RE-ACQUIRE it before returning -- and that wait sits inside the
perf_counter bracket, so waiting was reported as working. This experiment tests that
directly instead of by elimination.

THE INSTRUMENT
Windows QueryThreadCycleTime() returns CPU cycles the calling thread actually executed.
Wall time comes from perf_counter. For any bracket:
    wall  = executing + waiting (GIL wait, preemption, descheduling)
    exec  ~= cycles / cycles_per_second   (calibrated uncontended)
    wait  ~= wall - exec
That decomposition is the thing the existing instrumentation cannot do, and it is why
`t_copy`/`t_proc` have misled us twice. Docs:
https://learn.microsoft.com/en-us/windows/win32/api/realtimeapiset/nf-realtimeapiset-querythreadcycletime

DESIGN
One MEASURED thread runs the production copy at a realistic 100 Hz duty cycle (one copy
per 10 ms -- NOT continuously, which would measure bandwidth saturation instead of GIL
behavior). Alongside it, N COMPETITOR threads each burn a configurable amount of
GIL-HELD Python work every 10 ms, standing in for the rest of the pipeline: the other
cameras' grab-loop bytecode, the encoder threads, and the Qt main thread.

Competitor counts map to real configurations:
     0  -> the measured thread alone (control)
     5  -> 6 grab threads
    11  -> 6 grab + 6 encoder threads (today)
    17  -> 9 grab + 9 encoder threads (the 9-camera target)

PASS/FAIL
  - If wall inflates toward ~2.7 ms while exec stays ~0.08 ms, the reinterpretation is
    CONFIRMED: the production number was almost entirely GIL wait, and GIL escape
    (processes / subinterpreters / free-threaded CPython / a C extension) is the main
    line of attack.
  - If exec ALSO inflates, the cost is real work or memory contention and the GIL story
    is wrong.
  - If wall stays ~0.08 ms even at 17 competitors, GIL contention does not explain
    production and something else is going on -- go looking again.

    uv run tools/experiments/probe_gil_wait.py
    uv run tools/experiments/probe_gil_wait.py --gil-us 100,300,1000 --competitors 0,5,11,17
"""
import argparse
import json
import statistics
import sys
import threading
import time
from pathlib import Path

import numpy as np

#: The repository root, three levels up from tools/experiments/. Output is
#: anchored to it, never to the working directory, so a run started from
#: anywhere writes to one place.
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.perfclock import calibrate_cycles_per_s, thread_cycles

W, H = 1920, 1200
NV12_H = H * 3 // 2
PERIOD = 0.010            # 100 fps trigger period

# --- workload -----------------------------------------------------------------
def gil_burn(target_us):
    """Burn ~target_us of GIL-HELD time in pure Python bytecode.

    Calibrated per call site by the caller. Pure Python is the point: this models the
    serialized portion of the real pipeline, the part that cannot spread across cores.
    """
    t_end = time.perf_counter() + target_us * 1e-6
    x = 0
    while time.perf_counter() < t_end:
        for i in range(200):
            x += i
    return x


def competitor(stop_evt, gil_us, started):
    started.wait()
    nxt = time.perf_counter()
    while not stop_evt.is_set():
        gil_burn(gil_us)
        nxt += PERIOD
        d = nxt - time.perf_counter()
        if d > 0:
            time.sleep(d)          # releases the GIL, like a real blocked grab thread
        else:
            nxt = time.perf_counter()


def measured_thread(ring, src, iters, out, started, cps):
    """The thread under observation: production copy, one per 10 ms."""
    started.wait()
    rows = []
    ring_i = 0
    nxt = time.perf_counter()
    for _ in range(iters):
        buf = ring[ring_i]
        ring_i = (ring_i + 1) % len(ring)
        c0, t0 = thread_cycles(), time.perf_counter()
        buf[:H, :] = src                      # <-- the bracket that misled us
        t1, c1 = time.perf_counter(), thread_cycles()
        wall_ms = (t1 - t0) * 1e3
        exec_ms = (c1 - c0) / cps * 1e3
        rows.append((wall_ms, exec_ms, max(0.0, wall_ms - exec_ms)))
        nxt += PERIOD
        d = nxt - time.perf_counter()
        if d > 0:
            time.sleep(d)
        else:
            nxt = time.perf_counter()
    out.extend(rows)


def run_case(n_comp, gil_us, iters, ring_n, cps):
    # Production allocation: np.full pre-faults (see E1a), so the ring is warm.
    ring = [np.full((NV12_H, W), 128, np.uint8) for _ in range(ring_n)]
    src = np.full((H, W), 77, np.uint8)
    stop = threading.Event()
    started = threading.Event()
    rows = []

    comps = [threading.Thread(target=competitor, args=(stop, gil_us, started),
                              daemon=True) for _ in range(n_comp)]
    meas = threading.Thread(target=measured_thread,
                            args=(ring, src, iters, rows, started, cps))
    for t in comps:
        t.start()
    meas.start()
    started.set()
    meas.join()
    stop.set()
    for t in comps:
        t.join(timeout=1.0)

    wall = sorted(r[0] for r in rows)
    ex = sorted(r[1] for r in rows)
    wt = sorted(r[2] for r in rows)
    p = lambda a, q: a[min(len(a) - 1, int(len(a) * q))]
    return {
        "competitors": n_comp, "gil_us": gil_us,
        "wall_median": round(statistics.median(wall), 3),
        "wall_p95": round(p(wall, 0.95), 3),
        "wall_max": round(wall[-1], 3),
        "exec_median": round(statistics.median(ex), 3),
        "exec_p95": round(p(ex, 0.95), 3),
        "wait_median": round(statistics.median(wt), 3),
        "wait_p95": round(p(wt, 0.95), 3),
        "n": len(rows),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--competitors", default="0,5,11,17")
    ap.add_argument("--gil-us", default="100,300,1000",
                    help="GIL-held us per competitor per 10 ms frame")
    ap.add_argument("--iters", type=int, default=400, help="copies in the measured thread")
    ap.add_argument("--ring", type=int, default=200)
    ap.add_argument("--out",
                    default=str(REPO / "probe_out" / "gil_wait.json"))
    args = ap.parse_args()

    cps = calibrate_cycles_per_s()
    print(f"calibration: {cps/1e9:.3f} G cycles/s while executing")
    print(f"measured thread: production copy at 100 Hz, ring={args.ring} "
          f"({args.ring*W*NV12_H/2**30:.2f} GiB), {args.iters} copies/case\n")

    rows = []
    for gil_us in [int(x) for x in args.gil_us.split(",")]:
        print(f"--- competitors each burning {gil_us} us of GIL-held work per 10 ms ---")
        print(f"{'comp':>5} {'wall_med':>9} {'wall_p95':>9} {'wall_max':>9} "
              f"{'exec_med':>9} {'wait_med':>9} {'wait_p95':>9}")
        for n in [int(x) for x in args.competitors.split(",")]:
            r = run_case(n, gil_us, args.iters, args.ring, cps)
            rows.append(r)
            print(f"{n:5d} {r['wall_median']:9.3f} {r['wall_p95']:9.3f} "
                  f"{r['wall_max']:9.3f} {r['exec_median']:9.3f} "
                  f"{r['wait_median']:9.3f} {r['wait_p95']:9.3f}")
        print()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    print(f"wrote {out}")

    # --- verdict ---------------------------------------------------------------
    base = next((r for r in rows if r["competitors"] == 0), None)
    worst = max(rows, key=lambda r: r["wall_median"])
    if base:
        print(f"\nVERDICT")
        print(f"  alone            : wall {base['wall_median']:.3f} ms  "
              f"exec {base['exec_median']:.3f} ms")
        print(f"  worst contention : wall {worst['wall_median']:.3f} ms  "
              f"exec {worst['exec_median']:.3f} ms  "
              f"({worst['competitors']} competitors @ {worst['gil_us']} us)")
        exec_growth = worst["exec_median"] / max(base["exec_median"], 1e-6)
        wall_growth = worst["wall_median"] / max(base["wall_median"], 1e-6)
        print(f"  wall x{wall_growth:.1f}   exec x{exec_growth:.1f}")
        if wall_growth > 4 and exec_growth < 2:
            print("  => CONFIRMED: contention inflates the BRACKET, not the work.")
            print("     The production 2.7 ms was GIL wait misattributed to copying.")
            print("     GIL escape is the main line of attack.")
        elif exec_growth >= 2:
            print("  => exec grew too: real work/memory contention, not pure GIL wait.")
        else:
            print("  => contention did NOT reproduce the inflation; look elsewhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
