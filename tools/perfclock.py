"""Per-thread CPU cycle clock, shared by the timing experiments.

RULE: one implementation, imported, never re-typed. The helper existed five
times over with different handles and inconsistent error checking, and a
QueryThreadCycleTime call that fails silently returns a stale count, which
reads as "this thread did no work" rather than as a broken instrument.

Windows QueryThreadCycleTime returns the cycles a thread actually EXECUTED, so
``wall - exec`` is the time it spent waiting: for the GIL, or for the
scheduler. That decomposition is the reason these experiments exist, because
wall time alone reported waiting as working twice.

Cycles are not elapsed time. This CPU is hybrid, so a P-core cycle and an
E-core cycle are neither the same work nor the same duration; convert only
through a calibration taken on the same thread, and prefer ratios between
threads sampled over one interval to absolute milliseconds.
https://learn.microsoft.com/en-us/windows/win32/api/realtimeapiset/nf-realtimeapiset-querythreadcycletime

Attributing cycles to OTHER threads needs real handles from OpenThread plus a
thread enumeration, which is a different instrument; probe_native_cpu.py keeps
its own ctypes block for that and does not import this one.

    from tools.perfclock import thread_cycles, calibrate_cycles_per_s
"""
import ctypes
import time
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.QueryThreadCycleTime.argtypes = [wintypes.HANDLE,
                                      ctypes.POINTER(ctypes.c_ulonglong)]
_k32.QueryThreadCycleTime.restype = wintypes.BOOL
_k32.GetCurrentThread.argtypes = []
_k32.GetCurrentThread.restype = wintypes.HANDLE

#: GetCurrentThread() is the pseudo-handle -2, which every thread may use to
#: name itself, so it is fetched once and reused rather than per call.
CURRENT_THREAD = _k32.GetCurrentThread()


def thread_cycles(handle=None) -> int:
    """Cycles executed by the calling thread, or by ``handle`` if given.

    Raises on failure rather than returning a stale or zero count: an
    instrument that fails quietly turns a broken measurement into a plausible
    one, which is worse than no measurement.
    """
    buf = ctypes.c_ulonglong()
    if not _k32.QueryThreadCycleTime(CURRENT_THREAD if handle is None
                                     else handle, ctypes.byref(buf)):
        raise ctypes.WinError(ctypes.get_last_error())
    return buf.value


def calibrate_cycles_per_s(dur: float = 0.30) -> float:
    """Cycles per second for this thread while it genuinely runs.

    Measured uncontended and on the calling thread, because that is the only
    context in which the ratio means anything. Turbo and P/E-core placement
    make it approximate, which is enough: these experiments separate
    "executed for 0.08 ms" from "waited for 2.6 ms", a thirty-fold difference
    against a calibration error of a few per cent.
    """
    c0, t0 = thread_cycles(), time.perf_counter()
    x = 0
    while time.perf_counter() - t0 < dur:
        for i in range(10000):
            x += i
    c1, t1 = thread_cycles(), time.perf_counter()
    return (c1 - c0) / (t1 - t0)
