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

#: Tier applied to grab threads. Overridable so an arm can be A/B'd
#: without editing the call site.
GRAB_THREAD_PRIORITY = THREAD_PRIORITY_HIGHEST

_pcores_cache: list[int] | None = None
#: Optional explicit P-core ORDER for camera assignment. Slot i takes
#: _core_order[i % len]. Exists because the default sorted order puts camera 0
#: (and, at nine cameras, camera 8) on logical 0 -- which on this part is also
#: one of the two cores carrying ~46% NIC DPC time. Set via set_core_order().
_core_order: list[int] | None = None

#: Logical CPUs capture threads are kept off by default. CPU 0 is the Windows
#: boot processor and the default target for timer and DPC work; on the
#: reference machine CPU 0 and CPU 1 together carry ~46% of NIC DPC time. A
#: profile can override this with capture_core_exclude -- measure before
#: changing it, because the cost of guessing wrong is one camera diverging to
#: kick_max_lag under the GUI while headless runs look clean.
DEFAULT_EXCLUDED_CORES: tuple = (0,)


def set_core_order(order) -> None:
    """Override which P-cores cameras are assigned to, and in what order.

    Pass None to restore the detected order. Values not present in the
    detected P-core set are dropped rather than trusted, so a stale order from
    another machine degrades to the default instead of pinning onto E-cores.
    """
    global _core_order
    if not order:
        _core_order = None
        return
    valid = set(performance_cores())
    _core_order = [int(c) for c in order if int(c) in valid] or None


def _process_mask() -> int:
    """The affinity mask this PROCESS is allowed to use, or 0 if unknown.

    Pinning to a CPU outside the process mask FAILS -- SetThreadAffinityMask
    returns 0 -- so under a job object, a Docker cpuset, or a user-set
    affinity, some cameras would pin and others silently would not. A partial
    pin is worse than none: the unpinned thread is exactly the one that lags.
    """
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = ctypes.c_void_p
        proc = ctypes.c_size_t(0)
        sysm = ctypes.c_size_t(0)
        k.GetProcessAffinityMask.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_size_t)]
        if k.GetProcessAffinityMask(k.GetCurrentProcess(),
                                    ctypes.byref(proc), ctypes.byref(sysm)):
            return proc.value
    except Exception:
        pass
    return 0


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
            fast = sorted(x for c, xs in by_class.items() if c > min(by_class)
                          for x in xs)
            # Intersect with the process mask, or some threads pin and some
            # silently do not -- see _process_mask.
            pm = _process_mask()
            if pm:
                fast = [c for c in fast if pm & (1 << c)]
            # >64 logical CPUs means processor groups, which a single 64-bit
            # mask cannot address. Refuse rather than pin to the wrong group.
            if any(c >= 64 for c in fast):
                fast = []
            _pcores_cache = fast
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


def restrict_to_performance_cores(priority: int | None = None) -> dict:
    """Confine the calling thread to the P-core SET, without fixing one core.

    The alternative to one-core-per-camera. With nine cameras on eight P-cores
    the round-robin doubles two cameras onto one core, and on this part the
    first two P-cores are also the ones carrying ~46% NIC DPC -- so the naive
    mapping puts cam1 and cam9 on the busiest core in the machine. Handing the
    scheduler the whole P-core set keeps threads off E-cores (the thing that
    actually matters) while letting it balance around DPC load.
    """
    cores = performance_cores()
    out = {"pinned": False, "cpu": None, "priority": False,
           "n_pcores": len(cores)}
    if cores:
        out["cpu"] = f"Pset[{len(cores)}]"
        out["pinned"] = restrict_current_thread(cores)
    if priority is not None:
        out["priority"] = set_current_thread_priority(priority)
    return out


def pin_to_performance_core(slot: int, priority: int | None = None) -> dict:
    """Pin the calling thread to the slot-th P-core, round-robin if oversubscribed.

    `slot` is normally the camera index, so cameras spread across P-cores
    deterministically instead of competing for whichever the scheduler picks.
    Returns a dict describing what happened — callers log it rather than trust it.
    """
    cores = _core_order or performance_cores()
    out = {"pinned": False, "cpu": None, "priority": False,
           "n_pcores": len(cores)}
    if not cores:
        # Not hybrid, or pinning unavailable. Do NOTHING -- including no
        # priority bump. session_config advertises this as a no-op in that
        # case, and quietly raising priority anyway would make that false and
        # change scheduling on machines nobody measured.
        return out
    cpu = cores[slot % len(cores)]
    out["cpu"] = cpu
    out["pinned"] = pin_current_thread(cpu)
    if priority is not None:
        out["priority"] = set_current_thread_priority(priority)
    return out


def capture_core_pool(exclude=None) -> list[int]:
    """P-cores a capture thread may use: the P-cores, minus logical CPU 0.

    CPU 0 is not an ordinary core on Windows. It is the boot processor and the
    default target for timer interrupts and much DPC work, including the NIC's
    -- on this part the first two P-cores carry ~46% of NIC DPC time. A grab
    thread pinned there is descheduled by exactly the traffic it is trying to
    receive, and because the loop retrieves at the rate frames arrive it can
    never catch the deficit up.

    Measured 2026-09-14, nine cameras, 90 s, grab threads pinned, lag behind
    the leader as median/p95/max. The victim followed the CORE, not the camera:

      default order, cam1 on CPU 0    cam1 0/6/12    cam7 0/1/1
      rotated,       cam7 on CPU 0    cam1 0/0/1     cam7 0/3/12

    Headless the penalty is a few frames. Under the GUI, whose main thread adds
    ~1.5-2 ms of repaint work per cycle, the same camera diverged to
    kick_max_lag (480) and was force-dropped.

    Dropping CPU 0 leaves 7 cores for 9 cameras here, so two float. That is a
    far better trade than one camera parked on the busiest core in the machine.
    An explicit set_core_order() wins over this, so a rig that has measured
    something different can override it.
    """
    cores = performance_cores()
    if len(cores) <= 1:
        return cores
    drop = set(DEFAULT_EXCLUDED_CORES if exclude is None else exclude)
    pruned = [c for c in cores if c not in drop]
    return pruned or cores


def place_capture_thread(slot: int, priority: int | None = None) -> dict:
    """Placement policy for capture threads: exclusive core, else float.

    `pin_to_performance_core` wraps with `slot % len(cores)`, which is correct
    only while there are at least as many P-cores as cameras. At nine cameras
    on eight P-cores it pins cam1 and cam9 to the SAME core, both at
    GRAB_THREAD_PRIORITY, and that core is CPU 0 -- which on this part also
    carries the largest share of NIC DPC. Measured 2026-09-11 in a GUI
    recording: cam1 accumulated lag monotonically (67 -> 96 -> 153 -> 173
    frames) while the other eight sat at 0-4, with resends at 9-15 and zero
    buffer underruns, so it was pure CPU contention and not the network.

    Two whole-rig alternatives were already measured and rejected: confining
    every thread to the P-core set was worse than baseline, and reordering the
    cores only moves which camera is the victim -- with nine threads on eight
    cores, somebody always doubles up.

    So do neither. Give every camera that fits a core of its own, and let only
    the overflow float across the whole P-core set, where the scheduler can
    slot it into whichever core is momentarily free. Nothing changes at all
    for a rig with no more cameras than P-cores.
    """
    cores = _core_order or capture_core_pool()
    out = {"pinned": False, "cpu": None, "priority": False,
           "n_pcores": len(cores)}
    if not cores:
        # Not hybrid, or pinning unavailable: no-op, exactly as
        # pin_to_performance_core would.
        return out
    if slot < len(cores):
        cpu = cores[slot]
        out["cpu"] = cpu
        out["pinned"] = pin_current_thread(cpu)
        if priority is not None:
            out["priority"] = set_current_thread_priority(priority)
        return out
    # Overflow: float across the SAME pool, not the raw P-core set -- otherwise
    # the excluded core comes back in through the overflow path.
    out["cpu"] = f"pool[{len(cores)}]"
    out["pinned"] = restrict_current_thread(cores)
    if priority is not None:
        out["priority"] = set_current_thread_priority(priority)
    out["overflow"] = True
    return out


def efficiency_cores() -> list[int]:
    """Logical CPUs in the LOWEST efficiency class, or [] if not hybrid."""
    if not _IS_WINDOWS:
        return []
    fast = set(performance_cores())
    if not fast:
        return []
    try:
        import os
        return [c for c in range(os.cpu_count() or 0) if c not in fast]
    except Exception:
        return []


def restrict_current_thread(cpus) -> bool:
    """Confine the calling thread to a SET of logical CPUs."""
    if not _IS_WINDOWS or not cpus:
        return False
    try:
        k32 = _k32()
        mask = 0
        for c in cpus:
            mask |= 1 << c
        return k32.SetThreadAffinityMask(k32.GetCurrentThread(), mask) != 0
    except Exception:
        return False


def pin_to_efficiency_core(slot: int) -> dict:
    """Confine the calling thread to the E-core SET (not one E-core).

    For the ENCODER threads. They are not latency-critical — `Encode()` and
    `os.write()` both release the GIL and the real work is on the GPU — but
    there is one per camera, so left unpinned they compete with the grab
    threads for the eight P-cores, which would undo the grab-thread pinning.

    **Measured 2026-09-11: pinning each encoder to ONE E-core is much worse
    than not pinning them at all** — cam9 blew out to 321 frames behind and
    avg_proc went 2.19 -> 3.48 ms. A single E-core cannot sustain encode
    submission for one 1920x1200 stream at 100 fps, so the encoder backs up and
    drags its camera with it. The set keeps them off the P-cores while letting
    the scheduler move them freely among the sixteen E-cores.

    Deliberately no priority bump: the point is to yield to capture.
    """
    cores = efficiency_cores()
    out = {"pinned": False, "cpu": None, "n_ecores": len(cores)}
    if cores:
        out["cpu"] = f"set[{len(cores)}]"
        out["pinned"] = restrict_current_thread(cores)
    return out


ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
HIGH_PRIORITY_CLASS = 0x00000080


def set_process_priority(cls: int = HIGH_PRIORITY_CLASS) -> bool:
    """Raise the whole process's priority class.

    Thread priority is relative to the process class, so HIGHEST inside a
    NORMAL-class process still loses to a normal thread in a HIGH-class one.
    Deliberately NOT offering REALTIME_PRIORITY_CLASS: it outranks most kernel
    worker threads, and this machine is simultaneously servicing ~230k
    interrupts/s of NIC receive traffic -- starving those DPCs would trade a
    lagging camera for lost packets.
    """
    if not _IS_WINDOWS:
        return False
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        k.SetPriorityClass.restype = ctypes.c_bool
        return bool(k.SetPriorityClass(k.GetCurrentProcess(), cls))
    except Exception:
        return False


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
