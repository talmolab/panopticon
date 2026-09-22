"""Attribute host CPU to pylon's NATIVE GigE receive threads, which hold no GIL
and therefore appear in NO Python-side measurement we have ever taken.
(Findings: docs/HISTORY.md, phase 5.)

WHY THIS EXISTS
At 9 cameras the host must reassemble 9 x 100 x 2.304 MB = 2.07 GB/s of GVSP payload,
234,000 packets/s (measured 260.1 packets/frame from Total_Packet_Count/Total_Buffer_Count,
so jumbo frames are in effect). That work runs on threads created inside
PylonGigE_v11_TL.dll and in NIC ISR/DPC context, which the Python-side probes never
measured, so this term is entirely unmeasured. It is also the term that
"trigger_rate_limit: 0" hit: 8-15% of frames lost purely in transmission at SIX cameras,
with block IDs contiguous and acquisition at 100.03 fps.

WHAT THIS MEASURES
  1. Per-thread CPU seconds (user + kernel, real seconds) for EVERY thread in the
     process, split into Python / non-Python / pylon-module buckets.
  2. Per-core % DPC Time and % Interrupt Time -- the kernel-side receive cost, which is
     charged to NO thread at all and is invisible to (1).
  3. NIC and UDP drop counters, which are the OUTCOME of (1)+(2) running out of headroom.
Together these give a two-coefficient model  cpu = a*packets_per_s + b*bytes_per_s
that can be fitted from 6-camera points and evaluated at the 9-camera operating point.

INSTRUMENTS AND WHY
  GetThreadTimes  -- PRIMARY. Returns real seconds of user/kernel execution per thread.
     Granularity is one clock tick: measured on this machine as exactly 156250 x 100 ns
     = 15.625 ms. Useless per frame, irrelevant at session scale (a 600 s run charging a
     thread 60 s carries <= 15.6 ms of quantization = 0.03% error). The kernel/user split
     matters here: the socket-driver receive thread's recvfrom() work lands in KERNEL
     time, so a user-time-only view would under-read it by most of its cost.
     https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getthreadtimes
  QueryThreadCycleTime -- CORROBORATING ONLY, never converted to seconds. Microsoft:
     "Do not attempt to convert the CPU clock cycles returned by QueryThreadCycleTime to
     elapsed time." This machine is an 8P+16E hybrid, so a cycle on an E-core and a cycle
     on a P-core are not the same amount of either work or time. Use it for ratios
     between threads sampled over the same interval, nothing else.
     https://learn.microsoft.com/en-us/windows/win32/api/realtimeapiset/nf-realtimeapiset-querythreadcycletime
  Thread32First/Next -- documented enumeration of a process's threads, and the method
     QueryThreadCycleTime's own Remarks section tells you to use.
     https://learn.microsoft.com/en-us/windows/win32/api/tlhelp32/nf-tlhelp32-thread32first
  NtQueryInformationThread(ThreadQuerySetWin32StartAddress=9) + EnumProcessModulesEx --
     maps a thread's start address to the DLL that owns it. Semi-documented (winternl.h
     documents the function, not this class); it is what Process Explorer uses. Needs
     THREAD_QUERY_INFORMATION, NOT just THREAD_QUERY_LIMITED_INFORMATION -- verified on
     this machine. Treat a failure as "unknown module", never as fatal.
     Caveat measured here: CPython's own threads report ucrtbase.dll (they are created
     via _beginthreadex, whose thunk lives there), so module attribution ALONE cannot
     separate Python from pylon. Exclusion does that; the module only subdivides.

HOW A THREAD IS CLASSIFIED (three independent discriminators, all cheap)
  a) EXCLUSION. A thread is Python's iff its OS thread id is in
     {t.native_id for t in threading.enumerate()} | set(sys._current_frames()).
     Verified on this machine that threading ident == native_id == GetCurrentThreadId
     for CPython 3.11 on Windows, and that sys._current_frames() covers PyQt QThreads
     (their run() is Python, so they hold a thread state) as well as plain threads.
     Everything else is native.
  b) LIFECYCLE DIFF. Census at four points: before open_all, after open_all, after
     StartGrabbing, after StopGrabbing. The threads that appear at StartGrabbing and
     vanish at StopGrabbing, one or two per camera, ARE the stream receive threads.
     This needs no undocumented API and is the decisive identification.
  c) DOSE-RESPONSE. Receive-thread CPU must scale with packets/s. Nothing else in the
     process does (NVENC scales with frames encoded, Qt with repaints). If a candidate
     thread's CPU does not move when packet rate moves, it is not a receive thread.

USAGE (all read-only; --attach touches no camera)
    # 1. offline: confirm the plumbing and dump the static network config
    uv run tools/experiments/probe_native_cpu.py --selftest
    uv run tools/experiments/probe_native_cpu.py --netconfig

    # 2. during a recording started from the GUI, in a second console:
    uv run tools/experiments/probe_native_cpu.py --attach-name python.exe --duration 600 \
        --interval 10 --out probe_out/native_cpu_6cam_100fps_9000.json

    # 3. counters only (no process attach), if you want DPC/interrupt alone:
    uv run tools/experiments/probe_native_cpu.py --counters-only --duration 600
"""
import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

#: The repository root, three levels up from tools/experiments/. Output is
#: anchored to it, never to the working directory, so a run started from
#: anywhere writes to one place.
REPO = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Win32
# ---------------------------------------------------------------------------
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
psapi = ctypes.WinDLL("psapi", use_last_error=True)

THREAD_QUERY_INFORMATION = 0x0040
THREAD_QUERY_LIMITED_INFORMATION = 0x0800
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
TH32CS_SNAPTHREAD = 0x00000004
LIST_MODULES_ALL = 0x03
ERROR_INVALID_PARAMETER = 87
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD)]


def _ft(f):
    return ((f.dwHighDateTime << 32) | f.dwLowDateTime) / 1e7    # -> seconds


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD)]


class MODULEINFO(ctypes.Structure):
    _fields_ = [("lpBaseOfDll", ctypes.c_void_p),
                ("SizeOfImage", wintypes.DWORD),
                ("EntryPoint", ctypes.c_void_p)]


k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenThread.restype = wintypes.HANDLE
k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
k32.OpenProcess.restype = wintypes.HANDLE
k32.CloseHandle.argtypes = [wintypes.HANDLE]
k32.CloseHandle.restype = wintypes.BOOL
k32.GetThreadTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(FILETIME)] * 4
k32.GetThreadTimes.restype = wintypes.BOOL
k32.QueryThreadCycleTime.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(ctypes.c_ulonglong)]
k32.QueryThreadCycleTime.restype = wintypes.BOOL
k32.GetThreadDescription.argtypes = [wintypes.HANDLE,
                                     ctypes.POINTER(wintypes.LPWSTR)]
k32.GetThreadDescription.restype = ctypes.c_long
k32.LocalFree.argtypes = [ctypes.c_void_p]
k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Thread32First.restype = wintypes.BOOL
k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
k32.Thread32Next.restype = wintypes.BOOL
psapi.EnumProcessModulesEx.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(wintypes.HMODULE),
                                       wintypes.DWORD,
                                       ctypes.POINTER(wintypes.DWORD),
                                       wintypes.DWORD]
psapi.GetModuleFileNameExW.argtypes = [wintypes.HANDLE, wintypes.HMODULE,
                                       wintypes.LPWSTR, wintypes.DWORD]
psapi.GetModuleInformation.argtypes = [wintypes.HANDLE, wintypes.HMODULE,
                                       ctypes.POINTER(MODULEINFO), wintypes.DWORD]

_THREAD_QUERY_START_ADDRESS = 9      # ThreadQuerySetWin32StartAddress
ntdll.NtQueryInformationThread.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                           ctypes.c_void_p, wintypes.ULONG,
                                           ctypes.POINTER(wintypes.ULONG)]
ntdll.NtQueryInformationThread.restype = ctypes.c_long


def enum_thread_ids(pid: int) -> list[int]:
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if snap == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    out = []
    te = THREADENTRY32()
    te.dwSize = ctypes.sizeof(THREADENTRY32)
    ok = k32.Thread32First(snap, ctypes.byref(te))
    while ok:
        if te.th32OwnerProcessID == pid:
            out.append(te.th32ThreadID)
        ok = k32.Thread32Next(snap, ctypes.byref(te))
    k32.CloseHandle(snap)
    return out


def module_ranges(hproc) -> list[tuple[int, int, str]]:
    need = wintypes.DWORD()
    arr = (wintypes.HMODULE * 4096)()
    if not psapi.EnumProcessModulesEx(hproc, arr, ctypes.sizeof(arr),
                                      ctypes.byref(need), LIST_MODULES_ALL):
        return []
    n = min(need.value // ctypes.sizeof(wintypes.HMODULE), 4096)
    buf = ctypes.create_unicode_buffer(1024)
    mods = []
    for i in range(n):
        mi = MODULEINFO()
        if not psapi.GetModuleInformation(hproc, arr[i], ctypes.byref(mi),
                                          ctypes.sizeof(mi)):
            continue
        psapi.GetModuleFileNameExW(hproc, arr[i], buf, 1024)
        base = mi.lpBaseOfDll or 0
        mods.append((base, base + mi.SizeOfImage, buf.value))
    mods.sort()
    return mods


def owning_module(mods, addr):
    if not addr:
        return None
    for lo, hi, path in mods:
        if lo <= addr < hi:
            return path
    return None


def thread_start_address(h):
    val = ctypes.c_void_p()
    st = ntdll.NtQueryInformationThread(h, _THREAD_QUERY_START_ADDRESS,
                                        ctypes.byref(val), ctypes.sizeof(val), None)
    return None if st < 0 else (val.value or 0)


def thread_description(h):
    p = wintypes.LPWSTR()
    if k32.GetThreadDescription(h, ctypes.byref(p)) < 0:
        return None
    s = p.value
    k32.LocalFree(ctypes.cast(p, ctypes.c_void_p))
    return s or None


def sample_threads(pid: int, mods, want_static: bool) -> dict:
    """{tid: {user, kernel, cycles, desc, module}}. Threads that exit mid-scan are
    skipped (OpenThread fails with ERROR_INVALID_PARAMETER, observed in practice)."""
    out = {}
    for tid in enum_thread_ids(pid):
        h = k32.OpenThread(THREAD_QUERY_INFORMATION, False, tid)
        limited = False
        if not h:
            h = k32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
            limited = True
        if not h:
            continue                       # exited between enumerate and open
        rec = {}
        c, e, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        if k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e),
                              ctypes.byref(kt), ctypes.byref(ut)):
            rec["user"] = _ft(ut)
            rec["kernel"] = _ft(kt)
            rec["created"] = _ft(c)
        cyc = ctypes.c_ulonglong()
        if k32.QueryThreadCycleTime(h, ctypes.byref(cyc)):
            rec["cycles"] = cyc.value
        if want_static:
            rec["desc"] = thread_description(h)
            sa = None if limited else thread_start_address(h)
            rec["start"] = sa
            rec["module"] = owning_module(mods, sa)
        k32.CloseHandle(h)
        if rec:
            out[tid] = rec
    return out


# ---------------------------------------------------------------------------
# Perf counters -- the kernel-side cost that is charged to NO thread
# ---------------------------------------------------------------------------
#: DPC/interrupt time is what the NIC receive path costs OUTSIDE any thread, so no
#: amount of thread enumeration can see it. `Processor Information` (not the legacy
#: `Processor` object) is required on modern Windows -- it is processor-group aware.
#: https://learn.microsoft.com/en-us/windows-server/administration/performance-tuning/
COUNTERS = [
    r"\Processor Information(*)\% Processor Time",
    r"\Processor Information(*)\% DPC Time",
    r"\Processor Information(*)\% Interrupt Time",
    r"\Processor Information(*)\Interrupts/sec",
    r"\Processor Information(*)\DPCs Queued/sec",
    r"\Network Interface(*)\Bytes Received/sec",
    r"\Network Interface(*)\Packets Received/sec",
    r"\Network Interface(*)\Packets Received Discarded",
    r"\Network Interface(*)\Packets Received Errors",
    r"\Network Interface(*)\Output Queue Length",
    r"\UDPv4\Datagrams Received/sec",
    r"\UDPv4\Datagrams Received Errors",
    r"\Memory\Pages/sec",
]


def start_typeperf(csv_path: Path, interval_s: int, samples: int):
    """typeperf is in-box (C:\\Windows\\System32\\typeperf.exe). -sc bounds the run so
    a crashed probe cannot leave it recording forever."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    cfg = csv_path.with_suffix(".txt")
    cfg.write_text("\n".join(COUNTERS), encoding="utf-8")
    cmd = ["typeperf", "-cf", str(cfg), "-si", str(interval_s),
           "-sc", str(samples), "-f", "CSV", "-o", str(csv_path), "-y"]
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT)


def net_snapshot() -> dict:
    """Cumulative NIC drop counters straight from the driver, cheap and exact."""
    ps = ("Get-NetAdapterStatistics | Select-Object Name,ReceivedBytes,"
          "ReceivedUnicastPackets,ReceivedDiscardedPackets,ReceivedPacketErrors "
          "| ConvertTo-Json -Compress")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps], capture_output=True, text=True,
                           timeout=30)
        return json.loads(r.stdout) if r.stdout.strip() else {}
    except Exception as e:
        return {"error": str(e)}


def udp_snapshot() -> dict:
    try:
        r = subprocess.run(["netstat", "-s", "-p", "udp"], capture_output=True,
                           text=True, timeout=30)
        d = {}
        for line in r.stdout.splitlines():
            m = re.match(r"\s*([A-Za-z ]+?)\s*=\s*(\d+)\s*$", line)
            if m:
                d[m.group(1).strip()] = int(m.group(2))
        return d
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------
PYLON_HINTS = ("pylon", "producergev", "genapi", "gcbase", "nodemapdata",
               "xmlparser", "log4cpp", "gxapi", "uxapi", "_pylon", "_genicam")
GPU_HINTS = ("nvcuda", "nvenc", "nvapi", "cudart", "nvrtc", "nvml",
             "pynvvideocodec")


def classify(rec, python_tids, tid):
    if tid in python_tids:
        return "python"
    mod = (rec.get("module") or "").lower()
    base = os.path.basename(mod)
    if any(h in base for h in PYLON_HINTS):
        return "pylon"
    if any(h in base for h in GPU_HINTS):
        return "gpu"
    if not mod:
        return "native_unknown"
    return "native_other"


def python_tids_of_this_process():
    ids = {t.native_id for t in threading.enumerate() if t.native_id}
    ids |= set(sys._current_frames().keys())
    return ids


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------
def selftest():
    """Prove every API call works and print the numbers the spec depends on.
    Touches no camera and no network."""
    print("=== probe_native_cpu selftest ===")
    stop = threading.Event()

    def burn():
        x = 0
        while not stop.is_set():
            for i in range(50000):
                x += i
        return x

    ts = [threading.Thread(target=burn, daemon=True, name=f"burner{i}")
          for i in range(2)]
    for t in ts:
        t.start()
    time.sleep(1.0)

    pid = os.getpid()
    hproc = k32.GetCurrentProcess()
    mods = module_ranges(hproc)
    pytids = python_tids_of_this_process()
    idents = set(sys._current_frames().keys())
    natives = {t.native_id for t in threading.enumerate() if t.native_id}
    print(f"python {sys.version.split()[0]}  pid {pid}  modules {len(mods)}")
    print(f"threading ident set == native_id set : {idents == natives}"
          "   (must be True; the exclusion rule depends on it)")
    snap = sample_threads(pid, mods, want_static=True)
    print(f"{'tid':>8} {'class':<15} {'user_s':>8} {'kern_s':>8} "
          f"{'cycles':>14}  module")
    for tid, rec in sorted(snap.items()):
        print(f"{tid:8d} {classify(rec, pytids, tid):<15} "
              f"{rec.get('user', -1):8.4f} {rec.get('kernel', -1):8.4f} "
              f"{rec.get('cycles', -1):14d}  "
              f"{os.path.basename(rec.get('module') or '') or hex(rec.get('start') or 0)}")
    stop.set()

    # tick granularity, measured not assumed
    h = k32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False,
                       k32.GetCurrentThreadId())
    seen = set()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.5:
        c, e, kt, ut = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        k32.GetThreadTimes(h, ctypes.byref(c), ctypes.byref(e),
                           ctypes.byref(kt), ctypes.byref(ut))
        seen.add(round(_ft(ut) * 1e7))
    sv = sorted(seen)
    steps = sorted({sv[i + 1] - sv[i] for i in range(len(sv) - 1)})
    k32.CloseHandle(h)
    print(f"\nGetThreadTimes quantum: {steps} x100ns = "
          f"{[round(s / 1e4, 4) for s in steps]} ms (expect 15.625)")
    print("NOTE: 15.625 ms is FINE at session scale (0.03% of a 60 s charge over a "
          "600 s run) and USELESS per frame. Never bracket one frame with it.")
    return 0


def netconfig():
    """Static network/transport configuration. Read-only. Run before any recording so
    the run is reproducible and the confounds are recorded with the data."""
    cmds = [
        ("adapters",
         "Get-NetAdapter | Select-Object Name,InterfaceDescription,Status,"
         "LinkSpeed,MtuSize | ConvertTo-Json -Compress"),
        ("rss",
         "Get-NetAdapter | Where-Object Status -eq 'Up' | Get-NetAdapterRss "
         "-ErrorAction SilentlyContinue | Select-Object Name,Enabled,"
         "NumberOfReceiveQueues,BaseProcessorNumber,MaxProcessorNumber,"
         "MaxProcessors,Profile | ConvertTo-Json -Compress"),
        ("advanced",
         "Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { "
         "Get-NetAdapterAdvancedProperty -Name $_.Name -ErrorAction SilentlyContinue } "
         "| Where-Object { $_.RegistryKeyword -in "
         "'*NumRssQueues','*RSS','*ReceiveBuffers','*TransmitBuffers',"
         "'*InterruptModeration','ITR','*JumboPacket','*FlowControl' } "
         "| Select-Object Name,DisplayName,DisplayValue | ConvertTo-Json -Compress"),
        ("bindings",
         "Get-NetAdapter | Where-Object Status -eq 'Up' | ForEach-Object { "
         "Get-NetAdapterBinding -Name $_.Name } | Where-Object ComponentID "
         "-like '*pylon*' | Select-Object Name,DisplayName,Enabled "
         "| ConvertTo-Json -Compress"),
        ("stats", "Get-NetAdapterStatistics | Select-Object Name,ReceivedBytes,"
                  "ReceivedUnicastPackets,ReceivedDiscardedPackets,"
                  "ReceivedPacketErrors | ConvertTo-Json -Compress"),
    ]
    out = {}
    for name, ps in cmds:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps], capture_output=True, text=True)
        try:
            out[name] = json.loads(r.stdout) if r.stdout.strip() else None
        except Exception:
            out[name] = r.stdout
    out["udp"] = udp_snapshot()
    print(json.dumps(out, indent=1))
    print("\nCHECKLIST (fail any of these and the CPU numbers are not the story):",
          file=sys.stderr)
    print("  * MtuSize 9000+ on every camera port", file=sys.stderr)
    print("  * Jumbo Packet 9014 Bytes", file=sys.stderr)
    print("  * Receive Buffers at the driver maximum", file=sys.stderr)
    print("  * NumberOfReceiveQueues > 1  <-- 1 means every camera on that port "
          "funnels through ONE core's DPC", file=sys.stderr)
    print("  * Interrupt Moderation ON / Rate Extreme  <-- what "
          "PylonGigEConfigurator auto-opt sets", file=sys.stderr)
    print("  * ReceivedDiscardedPackets == 0 and UDP 'Receive Errors' flat",
          file=sys.stderr)
    return 0


def find_pid_by_name(name: str) -> int:
    import psutil
    cands = [p for p in psutil.process_iter(["pid", "name"])
             if (p.info["name"] or "").lower() == name.lower()]
    if not cands:
        raise SystemExit(f"no process named {name}")
    if len(cands) > 1:
        # the GUI is the one with the most threads by a wide margin
        cands.sort(key=lambda p: p.num_threads(), reverse=True)
        print(f"[warn] {len(cands)} processes named {name}; picking pid "
              f"{cands[0].pid} with {cands[0].num_threads()} threads")
    return cands[0].pid


def attach(pid: int, duration: float, interval: float, out_path: Path,
           with_counters: bool):
    hproc = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ,
                            False, pid)
    if not hproc:
        raise SystemExit(f"OpenProcess({pid}) failed: "
                         f"{ctypes.WinError(ctypes.get_last_error())}")
    mods = module_ranges(hproc)
    print(f"attached to pid {pid}, {len(mods)} modules loaded")
    pylon_mods = [os.path.basename(m[2]) for m in mods
                  if any(h in os.path.basename(m[2]).lower() for h in PYLON_HINTS)]
    print(f"pylon modules present: {sorted(set(pylon_mods))}")

    tp = None
    if with_counters:
        tp = start_typeperf(out_path.with_suffix(".counters.csv"),
                            max(1, int(interval)),
                            max(1, int(duration / max(1, int(interval)))) + 2)

    net0, udp0 = net_snapshot(), udp_snapshot()
    samples = []
    t_start = time.perf_counter()
    prev = sample_threads(pid, mods, want_static=True)
    # `created` is the thread's creation FILETIME. Keeping it makes the census diff
    # derivable from ONE run: the stream receive threads are the ones created at
    # StartGrabbing, i.e. after the probe attached.
    static = {tid: {"desc": r.get("desc"), "module": r.get("module"),
                    "start": r.get("start"), "created": r.get("created")}
              for tid, r in prev.items()}
    t_prev = t_start
    while time.perf_counter() - t_start < duration:
        time.sleep(max(0.0, interval - (time.perf_counter() - t_prev)))
        now = time.perf_counter()
        cur = sample_threads(pid, mods, want_static=False)
        dt = now - t_prev
        rows = []
        for tid, rec in cur.items():
            p = prev.get(tid)
            if not p or "user" not in p or "user" not in rec:
                continue
            du = rec["user"] - p["user"]
            dk = rec["kernel"] - p["kernel"]
            rows.append({"tid": tid, "d_user_s": round(du, 4),
                         "d_kernel_s": round(dk, 4),
                         "cpu_frac": round((du + dk) / dt, 4),
                         "d_cycles": rec.get("cycles", 0) - p.get("cycles", 0)})
        # new threads (StartGrabbing) need their static info resolved once
        newtids = set(cur) - set(static)
        if newtids:
            mods = module_ranges(hproc)
            fresh = sample_threads(pid, mods, want_static=True)
            for tid in newtids:
                if tid in fresh:
                    static[tid] = {"desc": fresh[tid].get("desc"),
                                   "module": fresh[tid].get("module"),
                                   "start": fresh[tid].get("start"),
                                   "created": fresh[tid].get("created"),
                                   "appeared_at_s": round(now - t_start, 3)}
        rows.sort(key=lambda r: -r["cpu_frac"])
        samples.append({"t": round(now - t_start, 3), "dt": round(dt, 3),
                        "threads": rows})
        tot = sum(r["cpu_frac"] for r in rows)
        top = ", ".join(f"{r['tid']}={r['cpu_frac']:.2f}" for r in rows[:6])
        print(f"[{now - t_start:7.1f}s] threads={len(rows):3d} "
              f"process_cpu={tot:5.2f} cores | top: {top}", flush=True)
        prev, t_prev = cur, now

    if tp:
        try:
            tp.wait(timeout=10)
        except Exception:
            tp.terminate()
    net1, udp1 = net_snapshot(), udp_snapshot()
    k32.CloseHandle(hproc)

    doc = {"pid": pid, "duration_s": duration, "interval_s": interval,
           "static": {str(k): v for k, v in static.items()},
           "samples": samples,
           "net_before": net0, "net_after": net1,
           "udp_before": udp0, "udp_after": udp1,
           "note": "classification of `static` into python/pylon/gpu must be done "
                   "against the census diff, NOT the module name alone: CPython's "
                   "own threads report ucrtbase.dll."}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1))
    print(f"\nwrote {out_path}")
    print("Now: (1) diff the thread set against a pre-StartGrabbing census to name "
          "the receive threads, (2) sum their cpu_frac -> core-equivalents, "
          "(3) divide by packets/s to get the per-packet coefficient.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--netconfig", action="store_true")
    ap.add_argument("--attach-pid", type=int, default=0)
    ap.add_argument("--attach-name", default="")
    ap.add_argument("--counters-only", action="store_true")
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--interval", type=float, default=10.0)
    ap.add_argument("--no-counters", action="store_true")
    ap.add_argument("--out",
                    default=str(REPO / "probe_out" / "native_cpu.json"))
    a = ap.parse_args()

    if os.name != "nt":
        raise SystemExit("Windows only")
    if a.selftest:
        return selftest()
    if a.netconfig:
        return netconfig()
    if a.counters_only:
        out = Path(a.out)
        tp = start_typeperf(out.with_suffix(".counters.csv"),
                            max(1, int(a.interval)),
                            max(1, int(a.duration / max(1, int(a.interval)))))
        n0, u0 = net_snapshot(), udp_snapshot()
        tp.wait()
        n1, u1 = net_snapshot(), udp_snapshot()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"net_before": n0, "net_after": n1,
                                   "udp_before": u0, "udp_after": u1}, indent=1))
        print(f"wrote {out} and {out.with_suffix('.counters.csv')}")
        return 0
    pid = a.attach_pid or (find_pid_by_name(a.attach_name)
                           if a.attach_name else 0)
    if not pid:
        ap.error("give --selftest, --netconfig, --counters-only, --attach-pid "
                 "or --attach-name")
    return attach(pid, a.duration, a.interval, Path(a.out), not a.no_counters)


if __name__ == "__main__":
    raise SystemExit(main())
