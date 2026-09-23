"""Tie capture worker processes to the life of the process that spawned them.

A worker holds cameras and NVENC sessions. If the parent dies without
closing it (a crash, a kill from the task manager), the worker must not keep
them: the next launch could not open the cameras, and a probe would measure
the contention. Each worker watches its parent and exits when the parent is
gone, and this module is the backstop for a worker too wedged to do that.

On Windows every worker is assigned to a job object created with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE. The parent holds the only handle to the
job. When the parent exits, however it exits, the system closes that handle
and terminates every process still in the job.

RULE: the parent never puts itself in the job, and the job handle is never
inherited. REASON: a parent in its own job would take the processes it
starts for other purposes (arduino-cli, ffmpeg) down with it on a crash, and
a worker that inherited the handle would keep the job open, so it could
never be killed by the parent's exit. multiprocessing's spawn creates
children without handle inheritance, and the handle is created
non-inheritable.

Elsewhere the class is a no-op that says so (`available` is False): the
worker's own parent watch is then the only protection.
"""
from __future__ import annotations

import sys

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _kernel32():
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC_LIMIT),
                    ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # argtypes and restype on every call: a 64-bit handle passed through
    # ctypes' default int conversion is truncated, and the call then fails
    # as though the handle were invalid.
    k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                            ctypes.c_void_p, wintypes.DWORD]
    k32.SetInformationJobObject.restype = wintypes.BOOL
    k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k32.AssignProcessToJobObject.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    return k32, EXTENDED_LIMIT


#: SetProcessInformation's ProcessPowerThrottling class and its state bit.
_PROCESS_POWER_THROTTLING = 4
_PROCESS_POWER_THROTTLING_CURRENT_VERSION = 1
_PROCESS_POWER_THROTTLING_EXECUTION_SPEED = 0x1


def opt_out_of_power_throttling() -> str:
    """Keep this process out of Windows' EcoQoS execution-speed throttling.

    RULE: every capture worker calls this once, at start. REASON: Windows may
    run a process it judges to be background work (one with no window of its
    own, as a worker has) at reduced clock and on efficiency cores, where the
    grab loop runs slower than the trigger; the GUI or the probe that
    spawned it is a foreground process and is not throttled. Setting the
    EXECUTION_SPEED bit in ControlMask and clearing it in StateMask opts the
    process out.

    Returns "" on success, or why it could not be applied (a platform
    without the call, an older Windows); a worker logs that and carries on.
    """
    if sys.platform != "win32":
        return "power throttling exists only on Windows"
    import ctypes
    from ctypes import wintypes

    class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [("Version", wintypes.ULONG),
                    ("ControlMask", wintypes.ULONG),
                    ("StateMask", wintypes.ULONG)]

    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.SetProcessInformation
    except (OSError, AttributeError) as e:
        return f"SetProcessInformation is unavailable: {e}"
    fn.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                   wintypes.DWORD]
    fn.restype = wintypes.BOOL
    k32.GetCurrentProcess.argtypes = []
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    state = PROCESS_POWER_THROTTLING_STATE(
        _PROCESS_POWER_THROTTLING_CURRENT_VERSION,
        _PROCESS_POWER_THROTTLING_EXECUTION_SPEED, 0)
    if not fn(k32.GetCurrentProcess(), _PROCESS_POWER_THROTTLING,
              ctypes.byref(state), ctypes.sizeof(state)):
        return (f"SetProcessInformation(ProcessPowerThrottling) failed: "
                f"{ctypes.WinError(ctypes.get_last_error())}")
    return ""


class WorkerJob:
    """A kill-on-close job object for this process's capture workers."""

    def __init__(self):
        self._handle = None
        self._k32 = None
        #: Why the job is unavailable, or "" when it works.
        self.reason = ""
        if sys.platform != "win32":
            self.reason = "job objects exist only on Windows"
            return
        import ctypes
        try:
            k32, EXTENDED_LIMIT = _kernel32()
        except Exception as e:
            self.reason = f"kernel32 could not be loaded: {e}"
            return
        handle = k32.CreateJobObjectW(None, None)
        if not handle:
            self.reason = (f"CreateJobObject failed: "
                           f"{ctypes.WinError(ctypes.get_last_error())}")
            return
        info = EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                handle, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(info), ctypes.sizeof(info)):
            err = ctypes.WinError(ctypes.get_last_error())
            k32.CloseHandle(handle)
            self.reason = f"SetInformationJobObject failed: {err}"
            return
        self._handle = handle
        self._k32 = k32

    @property
    def available(self) -> bool:
        return self._handle is not None

    def assign(self, process_handle) -> bool:
        """Put the process behind `process_handle` in the job. Returns
        whether it is now in it; a failure is left to the caller to report."""
        if self._handle is None or not process_handle:
            return False
        import ctypes
        ok = bool(self._k32.AssignProcessToJobObject(self._handle,
                                                     int(process_handle)))
        if not ok:
            self.reason = (f"AssignProcessToJobObject failed: "
                           f"{ctypes.WinError(ctypes.get_last_error())}")
        return ok

    def close(self) -> None:
        """Close the job. Every process still in it is terminated."""
        handle, self._handle = self._handle, None
        if handle is not None:
            self._k32.CloseHandle(handle)
