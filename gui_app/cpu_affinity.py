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
  - cores 0 and 1 carry ~46% DPC time from NIC receive processing (docs/HISTORY.md,
    phase 5). Those two are P-cores, so the effective P-core budget for capture is
    closer to six than eight.

Everything here degrades to a no-op off Windows and never raises: a failure to
pin is a performance regression, not a correctness one, and must never take a
recording down.
"""
from __future__ import annotations

import ctypes
import struct
import sys
from typing import NamedTuple

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
#: Result of the last CPU-set parse (classes, groups, CPU Set ids). Filled by
#: performance_cores() so efficiency_cores() and the CPU Sets helpers read the
#: same enumeration instead of re-deriving it from os.cpu_count().
_classes_cache: "CpuClasses | None" = None
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


class CpuClasses(NamedTuple):
    """What GetSystemCpuSetInformation says about this host's logical CPUs.

    fast      - logical CPUs in the TOP efficiency class, i.e. the P-cores
                (empty when not hybrid, when processor groups are in use, or
                when the enumeration failed);
    slow      - logical CPUs in EVERY lower class, i.e. all E-core flavours;
    by_class  - {efficiency_class: sorted logical CPUs};
    groups    - processor groups seen;
    ids       - {logical CPU: CPU Set id}, the handles SetThreadSelectedCpuSets
                takes (they are NOT the logical indices);
    reason    - why `fast` is empty, or "" when it is populated.
    """
    fast: list
    slow: list
    by_class: dict
    groups: set
    ids: dict
    reason: str


#: Byte layout of one SYSTEM_CPU_SET_INFORMATION record: Size(4) Type(4)
#: Id(4) Group(2) LogicalProcessorIndex(1) CoreIndex(1) LastLevelCacheIndex(1)
#: NumaNodeIndex(1) EfficiencyClass(1) AllFlags(1) Reserved(4) AllocationTag(8).
#: Records are walked by their own Size field, never by this constant.
CPU_SET_RECORD_SIZE = 32
_CPU_SET_TYPE_CPU_SET = 0
_CPU_SET_HEADER = struct.Struct("<IIIHB")


def parse_cpu_set_information(raw: bytes) -> list:
    """Records of a SYSTEM_CPU_SET_INFORMATION buffer as dicts.

    Each dict carries id, group, logical (the index WITHIN its group) and
    efficiency_class. Records of other types are skipped; a zero Size ends the
    walk, since it can only mean a truncated buffer.
    """
    out = []
    off = 0
    n = len(raw)
    while off + 19 < n:
        size, rtype, cid, group, logical = _CPU_SET_HEADER.unpack_from(raw, off)
        if size <= 0:
            break
        if rtype == _CPU_SET_TYPE_CPU_SET:
            out.append({"id": cid, "group": group, "logical": logical,
                        "efficiency_class": raw[off + 18]})
        off += size
    return out


def classify_cpu_sets(raw: bytes, process_mask: int = 0) -> CpuClasses:
    """Decide the P-core set from a raw CPU-set buffer. Pure, so it is testable
    with synthetic buffers for hosts this machine is not.

    Rules, and why each exists:
      - Only the TOP efficiency class is "fast". Three-class parts (a low-power
        E-core class below the E-cores) put the E-cores in a middle class, and
        "anything above the minimum" would pin grab threads onto them while
        looking correct.
      - One class means not hybrid: nothing to prefer, so `fast` is empty and
        every pin degrades to a no-op rather than a mistake.
      - More than one processor group means LogicalProcessorIndex repeats per
        group and a single 64-bit affinity mask cannot address the machine, so
        `fast` is empty rather than pinned to whichever group the caller is in.
      - Both `fast` and `slow` are intersected with the process affinity mask
        when one is given: SetThreadAffinityMask fails outright for a mask
        with any CPU outside the process mask, and a PARTIAL pin is worse
        than none, because the unpinned thread is exactly the one that lags.
        An empty `fast` after the intersection refuses with a reason; an
        empty `slow` just leaves the encoder threads unpinned.
    """
    recs = parse_cpu_set_information(raw)
    by_class: dict = {}
    groups = set()
    ids: dict = {}
    for r in recs:
        by_class.setdefault(r["efficiency_class"], []).append(r["logical"])
        groups.add(r["group"])
        ids[r["logical"]] = r["id"]
    for xs in by_class.values():
        xs.sort()
    if not by_class:
        return CpuClasses([], [], by_class, groups, ids, "no CPU set records")
    if len(groups) > 1:
        return CpuClasses([], [], by_class, groups, {},
                          f"{len(groups)} processor groups: pinning disabled")
    if len(by_class) < 2:
        return CpuClasses([], [], by_class, groups, ids,
                          "not hybrid: one efficiency class")
    top = max(by_class)
    fast = list(by_class[top])
    slow = sorted(x for c, xs in by_class.items() if c != top for x in xs)
    if process_mask:
        fast = [c for c in fast if process_mask & (1 << c)]
        slow = [c for c in slow if process_mask & (1 << c)]
        if not fast:
            return CpuClasses([], slow, by_class, groups, ids,
                              "no performance core inside the process mask")
    return CpuClasses(fast, slow, by_class, groups, ids, "")


def _read_cpu_set_buffer() -> bytes:
    """Raw GetSystemCpuSetInformation buffer for this process, or b""."""
    k32 = ctypes.WinDLL("kernel32")
    need = ctypes.c_ulong(0)
    k32.GetSystemCpuSetInformation(None, 0, ctypes.byref(need), None, 0)
    if need.value <= 0:
        return b""
    buf = ctypes.create_string_buffer(need.value)
    k32.GetSystemCpuSetInformation(buf, need.value, ctypes.byref(need), None, 0)
    return buf.raw[:need.value]


def describe_classes(classes: CpuClasses) -> str:
    """One log line naming every class and its size, so a host with a class
    layout nobody has measured is recognisable from the log alone."""
    parts = [f"class {c}: {len(xs)} logical"
             for c, xs in sorted(classes.by_class.items())]
    line = "[affinity] CPU efficiency classes: " + ("; ".join(parts) or "none")
    if len(classes.groups) > 1:
        line += f"; processor groups {sorted(classes.groups)}"
    if classes.fast:
        line += f"; performance class {max(classes.by_class)} -> {classes.fast}"
    else:
        line += f"; no pinning ({classes.reason})"
    return line


def cpu_classes() -> CpuClasses:
    """The cached classification of this host (enumerated once)."""
    performance_cores()
    return _classes_cache or CpuClasses([], [], {}, set(), {}, "not enumerated")


def performance_cores() -> list[int]:
    """Logical CPU indices in the TOP efficiency class (the P-cores).

    Uses GetSystemCpuSetInformation, which reports an EfficiencyClass per
    logical processor: higher is faster. The rules live in classify_cpu_sets.
    On a non-hybrid CPU this returns [], which makes pinning a no-op rather
    than a mistake. The per-class counts are logged once per process.
    """
    global _pcores_cache, _classes_cache
    if _pcores_cache is not None:
        return _pcores_cache
    if not _IS_WINDOWS:
        _pcores_cache = []
        return _pcores_cache
    try:
        classes = classify_cpu_sets(_read_cpu_set_buffer(), _process_mask())
        _classes_cache = classes
        _pcores_cache = list(classes.fast)
    except Exception:
        _pcores_cache = []
        return _pcores_cache
    # The log line is written AFTER the caches are set and in its own guard:
    # stdout may be a closed or full log file (the GUI runs under pythonw with
    # stdout redirected), and a print failure must never cost the pin.
    try:
        print(describe_classes(classes), flush=True)
    except Exception:
        pass
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


#: Priority for capture threads that overflow the core pool (see
#: place_capture_thread), or None to give them the same priority as the
#: pinned ones. Set through set_overflow_priority.
_overflow_priority: int | None = None


def set_overflow_priority(level: int | None) -> None:
    """Set the priority of capture threads that float over the pool.

    None, the default, gives an overflow thread the same priority as a pinned
    one. A diagnostic knob for a rig A/B: at equal priority Windows only
    round-robins, so a floater that lands on a pinned thread's core makes
    that thread wait, while one notch lower (THREAD_PRIORITY_ABOVE_NORMAL
    under a pinned THREAD_PRIORITY_HIGHEST) lets the pinned thread preempt
    it. Nothing in the application calls this; a probe does, before its grab
    threads start.
    """
    global _overflow_priority
    _overflow_priority = None if level is None else int(level)


def capture_core_pool(exclude=None) -> list[int]:
    """P-cores a capture thread may use: the P-cores, minus logical CPU 0.

    CPU 0 is not an ordinary core on Windows. It is the boot processor and the
    default target for timer interrupts and much DPC work, including the NIC's
    -- on this part the first two P-cores carry ~46% of NIC DPC time. A grab
    thread pinned there is descheduled by exactly the traffic it is trying to
    receive, and because the loop retrieves at the rate frames arrive it can
    never catch the deficit up.

    Measured at nine cameras with grab threads pinned (lag behind the leader,
    median/p95/max). The victim follows the CORE, not the camera:

      default order, cam1 on CPU 0    cam1 0/6/12    cam7 0/1/1
      rotated,       cam7 on CPU 0    cam1 0/0/1     cam7 0/3/12

    Headless the penalty is a few frames. Under the GUI, whose main thread adds
    ~1.5-2 ms of repaint work per cycle, the same camera diverged to
    kick_max_lag and was force-dropped.

    Every excluded core is one fewer exclusive core, so on a rig with more
    cameras than the pool holds the extra capture threads float over the
    pool (see place_capture_thread). That is a far better trade than a camera
    parked on the busiest core in the machine. `exclude` is the profile's
    capture_core_exclude; an explicit set_core_order() wins over both, so a
    rig that has measured something different can override them.
    """
    cores = performance_cores()
    if len(cores) <= 1:
        return cores
    drop = set(DEFAULT_EXCLUDED_CORES if exclude is None else exclude)
    pruned = [c for c in cores if c not in drop]
    return pruned or cores


def place_capture_thread(slot: int, priority: int | None = None) -> dict:
    """Placement policy for capture threads: exclusive core, else float.

    RULE: never pin two capture threads to one core. REASON: a round-robin
    `slot % len(cores)` is correct only while there are at least as many
    cores as cameras; past that it puts two grab threads on one core at
    GRAB_THREAD_PRIORITY. Measured on the reference rig, that gives monotonic
    lag on one camera (67 -> 173 frames) while the others sit at 0-4, with
    resends at 9-15 and zero buffer underruns: CPU contention, not the
    network.

    Two whole-rig alternatives were measured and rejected: confining every
    thread to the P-core set was worse than baseline, and reordering the
    cores only moves which camera is the victim, because with more threads
    than cores somebody always doubles up.

    So every camera that fits gets a core of its own, and only the overflow
    floats across the whole pool, where the scheduler can slot it into
    whichever core is momentarily free. Nothing changes for a rig with no
    more cameras than pool cores. The overflow threads take `priority`
    unless set_overflow_priority() gave them a level of their own.
    """
    cores = _core_order or capture_core_pool()
    out = {"pinned": False, "cpu": None, "priority": False,
           "n_pcores": len(cores)}
    if not cores:
        # Not hybrid, or pinning unavailable. Do NOTHING, including no
        # priority change: session_config advertises pinning as a no-op on
        # such a host, and raising priority anyway would change scheduling on
        # machines nobody measured.
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
    level = priority if _overflow_priority is None else _overflow_priority
    if level is not None:
        out["priority"] = set_current_thread_priority(level)
    out["overflow"] = True
    return out


def efficiency_cores() -> list[int]:
    """Logical CPUs in every class BELOW the top one, or [] if not hybrid.

    The union of the lower classes, not "everything that is not fast": on a
    three-class part the middle class is still an E-core flavour and belongs
    here, and enumerating from os.cpu_count() would include CPUs the process
    mask excludes.
    """
    if not _IS_WINDOWS or not performance_cores():
        return []
    return list(cpu_classes().slow)


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

    For the ENCODER threads, and off by default (`pin_encoder_threads`). It
    keeps the encoders off the P-cores the grab threads are pinned to, which
    unpinned encoders compete for. `Encode()` holds the GIL while it uploads
    each frame to the GPU, so an encoder is not idle CPU work either.

    **Never pin an encoder to ONE E-core: it is much worse than not pinning
    them at all.** Measured on the reference rig, one camera fell 321 frames
    behind and avg_proc rose from 2.19 to 3.48 ms: a single E-core could not
    sustain encode submission for one 1920x1200 stream at 100 fps, so the
    encoder backed up and held its camera back. The set keeps them off the
    P-cores while letting the scheduler move them freely among the E-cores.

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


# ---------------------------------------------------------------------------
# Opt-in alternatives to the hard affinity mask. None of these has a caller in
# the GUI: they exist so an A/B against the hard mask can be run from a probe
# without editing this module. Each degrades to False/None and never raises,
# like everything above.
# ---------------------------------------------------------------------------

def cpu_set_ids(cpus) -> list[int]:
    """CPU Set ids for the given logical CPUs, from the cached enumeration.

    SetThreadSelectedCpuSets takes CPU Set IDs (256, 257, ... on the reference
    host), not logical indices; passing indices silently selects nothing. A
    logical CPU the enumeration does not know is dropped.
    """
    ids = cpu_classes().ids
    return [ids[c] for c in cpus if c in ids]


def restrict_current_thread_cpu_sets(cpus) -> bool:
    """Soft-pin the calling thread to a set of logical CPUs via CPU Sets.

    CPU Sets are the scheduling preference Windows documents for hybrid parts
    and power management, as opposed to SetThreadAffinityMask, which is a hard
    constraint. The soft form still allows the placement that caused the
    laggard symptom (a grab thread on an E-core), so the hard mask stays the
    default and this is the experimental arm. An empty `cpus` CLEARS the
    thread's selection. False when the call did not take.
    """
    if not _IS_WINDOWS:
        return False
    try:
        k32 = _k32()
        k32.SetThreadSelectedCpuSets.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong]
        k32.SetThreadSelectedCpuSets.restype = ctypes.c_bool
        ids = cpu_set_ids(cpus) if cpus else []
        if cpus and not ids:
            return False
        if not ids:
            return bool(k32.SetThreadSelectedCpuSets(
                k32.GetCurrentThread(), None, 0))
        arr = (ctypes.c_ulong * len(ids))(*ids)
        return bool(k32.SetThreadSelectedCpuSets(
            k32.GetCurrentThread(), arr, len(ids)))
    except Exception:
        return False


def clear_current_thread_cpu_sets() -> bool:
    """Undo restrict_current_thread_cpu_sets for the calling thread."""
    return restrict_current_thread_cpu_sets([])


# THREAD_INFORMATION_CLASS value for SetThreadInformation.
_THREAD_POWER_THROTTLING = 3
THREAD_POWER_THROTTLING_CURRENT_VERSION = 1
THREAD_POWER_THROTTLING_EXECUTION_SPEED = 0x1


def set_current_thread_power_throttling(enabled: bool | None) -> bool:
    """Control EcoQoS execution-speed throttling for the calling thread.

    Windows may run a thread it deems background work at reduced clock (EcoQoS),
    and on a hybrid part that is a route onto the E-cores that no affinity
    mask forbids. THREAD_POWER_THROTTLING_STATE with the EXECUTION_SPEED bit
    set in ControlMask and clear in StateMask opts the thread OUT of it; set in
    both opts IN; a zero ControlMask returns the decision to the system.
    False when the call is unavailable or did not take.
    """
    if not _IS_WINDOWS:
        return False
    try:
        k32 = _k32()
        k32.SetThreadInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        k32.SetThreadInformation.restype = ctypes.c_bool
        if enabled is None:
            control, state = 0, 0
        else:
            control = THREAD_POWER_THROTTLING_EXECUTION_SPEED
            state = THREAD_POWER_THROTTLING_EXECUTION_SPEED if enabled else 0
        buf = (ctypes.c_ulong * 3)(THREAD_POWER_THROTTLING_CURRENT_VERSION,
                                  control, state)
        return bool(k32.SetThreadInformation(
            k32.GetCurrentThread(), _THREAD_POWER_THROTTLING,
            ctypes.byref(buf), ctypes.sizeof(buf)))
    except Exception:
        return False


def disable_current_thread_power_throttling() -> bool:
    """Opt the calling thread out of EcoQoS execution-speed throttling."""
    return set_current_thread_power_throttling(False)


# AvSetMmThreadPriority levels.
AVRT_PRIORITY_LOW = -1
AVRT_PRIORITY_NORMAL = 0
AVRT_PRIORITY_HIGH = 1
AVRT_PRIORITY_CRITICAL = 2


def mmcss_register_current_thread(task: str = "Capture",
                                  priority: int | None = None):
    """Register the calling thread with the Multimedia Class Scheduler.

    MMCSS runs a registered thread at a priority band above any non-realtime
    process class (the "Capture" and "Pro Audio" tasks in particular), which
    is the documented alternative to raising thread priority by hand. The
    catch that makes this an experiment rather than the default: a thread that
    exceeds its share (SystemResponsiveness, 20 percent by default, is
    reserved for everything else) is DEMOTED to priority 1-7, below a normal
    thread, so a grab loop that misbehaves would be punished harder than it is
    today. Returns the opaque handle for mmcss_revert, or None.
    """
    if not _IS_WINDOWS:
        return None
    try:
        avrt = ctypes.WinDLL("avrt", use_last_error=True)
        avrt.AvSetMmThreadCharacteristicsW.argtypes = [
            ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_ulong)]
        avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
        index = ctypes.c_ulong(0)
        handle = avrt.AvSetMmThreadCharacteristicsW(task, ctypes.byref(index))
        if not handle:
            return None
        if priority is not None:
            avrt.AvSetMmThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            avrt.AvSetMmThreadPriority.restype = ctypes.c_bool
            avrt.AvSetMmThreadPriority(handle, int(priority))
        return handle
    except Exception:
        return None


def mmcss_revert(handle) -> bool:
    """Undo mmcss_register_current_thread on the same thread."""
    if not _IS_WINDOWS or not handle:
        return False
    try:
        avrt = ctypes.WinDLL("avrt", use_last_error=True)
        avrt.AvRevertMmThreadCharacteristics.argtypes = [ctypes.c_void_p]
        avrt.AvRevertMmThreadCharacteristics.restype = ctypes.c_bool
        return bool(avrt.AvRevertMmThreadCharacteristics(handle))
    except Exception:
        return False
