"""Pin capture threads to performance cores, and raise their priority.

WHY THIS EXISTS. The rig's CPU is a hybrid: an Intel Ultra 9 285K with **8
P-cores and 16 E-cores**, no hyperthreading. At nine cameras Panopticon runs
about nineteen busy threads (9 grab + 9 encoder + Qt), so eleven of them
*cannot* be on a P-core, and Windows' Thread Director decides which — a
decision that varies per launch and drifts during a run.

That is an exact match for the symptom this project has chased for months: one
camera, *a different one each session* (cam9, then cam5, then cam4 on three
consecutive runs), drifting behind while its `avg_proc` reads LOWER than the
healthy cameras and its `rel` reads higher. A thread on an E-core is not doing
more work; it is executing the same work more slowly and waiting more. It only
needs to run a few percent slow: the grab loop can never catch up, because it
retrieves at exactly the rate frames arrive.

Two further facts make it worse here than the raw ratio suggests:
  - the P-cores are **not** logicals 0-7. On this part they are
    [0, 1, 10, 11, 12, 13, 22, 23], interleaved with E-cores, so any hand-rolled
    "first 8 CPUs" mask would pin threads onto E-cores while looking correct;
  - `docs/PERF_EXPERIMENTS.md` E5 measured cores 0 and 1 at ~46% DPC time from
    NIC receive processing. Those two are P-cores, so the effective P-core
    budget for capture is closer to six than eight.

Nothing in this codebase touched affinity, thread priority or timer resolution
before 2026-09-11. Every prior optimisation attacked the GIL or the network.

Everything here degrades to a no-op off Windows and never raises: a failure to
pin is a performance regression, not a correctness one, and must never take a
recording down.
"""
from __future__ import annotations

import ctypes
import sys

_IS_WINDOWS = sys.platform == "win32"


def _k32():
    """kernel32 with argtypes declared.

    Without these, ctypes defaults the return of GetCurrentThread() to c_int and
    TRUNCATES the 64-bit pseudo-handle, and passes the affinity mask as a 32-bit
    int. Both calls then fail silently and return 0 — which looks exactly like
    "this machine does not support pinning" and cost a debugging round here.
    """
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.GetCurrentThread.restype = ctypes.c_void_p
    k.GetCurrentThread.argtypes = []
    k.SetThreadAffinityMask.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    k.SetThreadAffinityMask.restype = ctypes.c_size_t
    k.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
    k.SetThreadPriority.restype = ctypes.c_bool
    return k

# SetThreadPriority levels
THREAD_PRIORITY_ABOVE_NORMAL = 1
THREAD_PRIORITY_HIGHEST = 2
THREAD_PRIORITY_TIME_CRITICAL = 15

_pcores_cache: list[int] | None = None


def performance_cores() -> list[int]:
    """Logical CPU indices whose efficiency class is above the minimum.

    Uses GetSystemCpuSetInformation, which reports an EfficiencyClass per
    logical processor — higher is faster. On a non-hybrid CPU every core lands
    in one class and this returns them all, which makes pinning a no-op rather
    than a mistake.
    """
    global _pcores_cache
    if _pcores_cache is not None:
        return _pcores_cache
    if not _IS_WINDOWS:
        _pcores_cache = []
        return _pcores_cache
    try:
        k32 = ctypes.WinDLL("kernel32")
        need = ctypes.c_ulong(0)
        k32.GetSystemCpuSetInformation(None, 0, ctypes.byref(need), None, 0)
        buf = ctypes.create_string_buffer(need.value)
        k32.GetSystemCpuSetInformation(buf, need.value, ctypes.byref(need),
                                       None, 0)
        by_class: dict[int, list[int]] = {}
        off = 0
        raw = buf.raw
        while off < need.value:
            size = int.from_bytes(raw[off:off + 4], "little")
            if size <= 0:
                break
            # SYSTEM_CPU_SET_INFORMATION: Size(4) Type(4) Id(4) Group(2)
            # LogicalProcessorIndex(1) CoreIndex(1) LastLevelCacheIndex(1)
            # NumaNodeIndex(1) EfficiencyClass(1)
            logical = raw[off + 14]
            eff = raw[off + 18]
            by_class.setdefault(eff, []).append(logical)
            off += size
        if len(by_class) < 2:
            _pcores_cache = []          # not hybrid: nothing to prefer
        else:
            best = max(by_class)
            fast = sorted(x for c, xs in by_class.items() if c > min(by_class)
                          for x in xs)
            _pcores_cache = fast or sorted(by_class[best])
    except Exception:
        _pcores_cache = []
    return _pcores_cache


def pin_current_thread(cpu: int) -> bool:
    """Pin the calling thread to one logical CPU. False if it did not take."""
    if not _IS_WINDOWS or cpu is None:
        return False
    try:
        k32 = _k32()
        prev = k32.SetThreadAffinityMask(k32.GetCurrentThread(), 1 << cpu)
        return prev != 0
    except Exception:
        return False


def set_current_thread_priority(level: int) -> bool:
    """Raise the calling thread's priority.

    Priority matters independently of affinity: Thread Director uses scheduling
    class as a hint for P-core placement, so a raised priority makes Windows
    *prefer* a P-core even without an explicit mask. Used together, the mask
    decides and the priority keeps the thread from being preempted there.
    """
    if not _IS_WINDOWS:
        return False
    try:
        k32 = _k32()
        return bool(k32.SetThreadPriority(k32.GetCurrentThread(), level))
    except Exception:
        return False


def pin_to_performance_core(slot: int, priority: int | None = None) -> dict:
    """Pin the calling thread to the slot-th P-core, round-robin if oversubscribed.

    `slot` is normally the camera index, so cameras spread across P-cores
    deterministically instead of competing for whichever the scheduler picks.
    Returns a dict describing what happened — callers log it rather than trust it.
    """
    cores = performance_cores()
    out = {"pinned": False, "cpu": None, "priority": False,
           "n_pcores": len(cores)}
    if cores:
        cpu = cores[slot % len(cores)]
        out["cpu"] = cpu
        out["pinned"] = pin_current_thread(cpu)
    if priority is not None:
        out["priority"] = set_current_thread_priority(priority)
    return out


def begin_high_resolution_timers(ms: int = 1) -> bool:
    """Ask Windows for a 1 ms timer tick (default is ~15.6 ms).

    Anything that sleeps or waits on a timed primitive inherits the system tick,
    so a coarse tick turns a sub-millisecond wait into a ~15 ms one. Process
    wide and must be matched by end_high_resolution_timers() — though Windows
    releases it at process exit anyway.
    """
    if not _IS_WINDOWS:
        return False
    try:
        return ctypes.WinDLL("winmm").timeBeginPeriod(ms) == 0
    except Exception:
        return False


def end_high_resolution_timers(ms: int = 1) -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        return ctypes.WinDLL("winmm").timeEndPeriod(ms) == 0
    except Exception:
        return False
