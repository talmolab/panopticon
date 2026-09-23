"""CUDA driver API through ctypes, for the pinned NVENC upload path.

Only `gui_app.nvenc` uses this module. It binds the driver entry points the
pinned-staging encoder needs: device and context handles, streams, events and
page-locked host memory, plus a stream stall for the launch check that proves
the upload's ordering. Every call is checked, and a failure raises
`CudaError` carrying the driver's error name.

RULE: nothing is loaded at import. The driver library is opened by the first
`get()`, and a machine without an NVIDIA driver gets `CudaUnavailable` with the
reason. REASON: `gui_app` is imported on machines with no NVIDIA GPU (the sim
profile, the offline tools, the test suites). Only the pinned upload path needs
the driver, and that path falls back to the host upload when the driver is
missing.

RULE: every call goes through a ctypes CDLL or WinDLL function, never PyDLL.
REASON: ctypes releases the GIL for the duration of a foreign call made through
CDLL or WinDLL (Python docs, ctypes, "Loading shared libraries"); PyDLL keeps it.
Keeping the upload's driver calls off the GIL is the reason this module exists:
a grab thread that waits for the GIL falls behind its camera.

`set_driver()` replaces the process's driver object, so a test can install a
fake with the same methods and exercise the pinned path on a machine without a
GPU.
"""
from __future__ import annotations

import ctypes
import os
import threading
from ctypes import POINTER, byref, c_int, c_size_t, c_uint, c_void_p

CUresult = c_int

#: cuStreamCreate: the stream does not synchronize with the legacy NULL stream.
CU_STREAM_NON_BLOCKING = 0x1
#: cuMemHostAlloc: the buffer is page-locked for every context, not only the
#: one current at allocation. A pinned buffer outlives any one context's use.
CU_MEMHOSTALLOC_PORTABLE = 0x1
#: cuEventCreate: cuEventSynchronize blocks the thread instead of spinning.
CU_EVENT_BLOCKING_SYNC = 0x1
#: cuEventCreate: no timestamp; recording and querying are cheaper.
CU_EVENT_DISABLE_TIMING = 0x2
CUDA_SUCCESS = 0
CUDA_ERROR_NOT_READY = 600

#: The driver library. It ships with the NVIDIA display driver, not with the
#: CUDA toolkit, so it is present wherever NVENC is.
LIBRARY_NAME = "nvcuda.dll" if os.name == "nt" else "libcuda.so.1"

#: Entry point -> argument types. The return type is always CUresult. The
#: `_v2` names are the ones the driver's own header maps the plain names to.
_SIGNATURES = {
    "cuInit": [c_uint],
    "cuGetErrorName": [CUresult, POINTER(ctypes.c_char_p)],
    "cuDriverGetVersion": [POINTER(c_int)],
    "cuDeviceGetCount": [POINTER(c_int)],
    "cuDeviceGet": [POINTER(c_int), c_int],
    "cuDevicePrimaryCtxRetain": [POINTER(c_void_p), c_int],
    "cuDevicePrimaryCtxRelease_v2": [c_int],
    "cuCtxCreate_v2": [POINTER(c_void_p), c_uint, c_int],
    "cuCtxDestroy_v2": [c_void_p],
    "cuCtxPushCurrent_v2": [c_void_p],
    "cuCtxPopCurrent_v2": [POINTER(c_void_p)],
    "cuCtxGetCurrent": [POINTER(c_void_p)],
    "cuStreamCreate": [POINTER(c_void_p), c_uint],
    "cuStreamDestroy_v2": [c_void_p],
    "cuEventCreate": [POINTER(c_void_p), c_uint],
    "cuEventDestroy_v2": [c_void_p],
    "cuEventRecord": [c_void_p, c_void_p],
    "cuEventQuery": [c_void_p],
    "cuEventSynchronize": [c_void_p],
    "cuMemHostAlloc": [POINTER(c_void_p), c_size_t, c_uint],
    "cuMemFreeHost": [c_void_p],
    "cuMemGetInfo_v2": [POINTER(c_size_t), POINTER(c_size_t)],
    "cuLaunchHostFunc": [c_void_p, c_void_p, c_void_p],
}


class CudaUnavailable(RuntimeError):
    """The CUDA driver cannot be used on this machine (no library, no device,
    or a driver too old for these entry points)."""


class CudaError(RuntimeError):
    """A driver call returned an error code."""

    def __init__(self, fn: str, code: int, name: str = "?"):
        super().__init__(f"{fn} failed: CUDA error {code} ({name})")
        self.fn = fn
        self.code = code
        self.name = name


def _open_library(name: str):
    """ctypes handle to the driver library: WinDLL on Windows, CDLL elsewhere.
    On x64 both use the one calling convention, and both release the GIL for
    each call."""
    loader = getattr(ctypes, "WinDLL", None) if os.name == "nt" else None
    return (loader or ctypes.CDLL)(name)


class Driver:
    """The CUDA driver entry points the pinned upload path and its launch
    check use.

    Handles (contexts, streams, events, host pointers) are plain ints. A
    context made by `ctx_create` is returned NOT current: callers bracket their
    work with `ctx_push` / `ctx_pop`, so no thread is left with a context
    current that another thread may later destroy.
    """

    def __init__(self, lib):
        self._lib = lib

    @classmethod
    def load(cls, name: str = LIBRARY_NAME) -> "Driver":
        """Open the driver library, bind the entry points and cuInit(0).

        Raises CudaUnavailable with the reason when any step fails.
        """
        try:
            lib = _open_library(name)
        except OSError as e:
            raise CudaUnavailable(
                f"the NVIDIA CUDA driver library ({name}) could not be loaded: "
                f"{e}. It is installed with the NVIDIA display driver; this "
                f"machine has no NVIDIA driver, or it is not on the DLL search "
                f"path.") from e
        missing = []
        for fn, argtypes in _SIGNATURES.items():
            try:
                f = getattr(lib, fn)
            except AttributeError:
                missing.append(fn)
                continue
            f.argtypes = argtypes
            f.restype = CUresult
        if missing:
            raise CudaUnavailable(
                f"the CUDA driver in {name} lacks {', '.join(missing)}; update "
                f"the NVIDIA display driver.")
        drv = cls(lib)
        rc = lib.cuInit(0)
        if rc != CUDA_SUCCESS:
            raise CudaUnavailable(
                f"cuInit failed: CUDA error {rc} ({drv.error_name(rc)}). No "
                f"usable NVIDIA GPU, or the display driver is too old.")
        return drv

    # -- errors
    def error_name(self, code: int) -> str:
        p = ctypes.c_char_p()
        try:
            if self._lib.cuGetErrorName(code, byref(p)) == CUDA_SUCCESS and p.value:
                return p.value.decode(errors="replace")
        except Exception:
            pass
        return "?"

    def _call(self, fn: str, *args) -> None:
        rc = getattr(self._lib, fn)(*args)
        if rc != CUDA_SUCCESS:
            raise CudaError(fn, rc, self.error_name(rc))

    # -- device
    def driver_version(self) -> int:
        v = c_int()
        self._call("cuDriverGetVersion", byref(v))
        return v.value

    def device_count(self) -> int:
        n = c_int()
        self._call("cuDeviceGetCount", byref(n))
        return n.value

    def device(self, ordinal: int = 0) -> int:
        d = c_int()
        self._call("cuDeviceGet", byref(d), ordinal)
        return d.value

    # -- contexts
    def primary_ctx_retain(self, ordinal: int = 0) -> int:
        ctx = c_void_p()
        self._call("cuDevicePrimaryCtxRetain", byref(ctx), self.device(ordinal))
        return ctx.value or 0

    def primary_ctx_release(self, ordinal: int = 0) -> None:
        self._call("cuDevicePrimaryCtxRelease_v2", self.device(ordinal))

    def ctx_create(self, ordinal: int = 0, flags: int = 0) -> int:
        """A new context on the device, left NOT current on the calling thread.

        cuCtxCreate pushes the new context onto the caller's stack; it is
        popped at once so the creating thread's state is what it was.
        """
        ctx = c_void_p()
        self._call("cuCtxCreate_v2", byref(ctx), flags, self.device(ordinal))
        popped = c_void_p()
        try:
            self._call("cuCtxPopCurrent_v2", byref(popped))
        except CudaError:
            self._call("cuCtxDestroy_v2", ctx)
            raise
        return ctx.value or 0

    def ctx_destroy(self, ctx: int) -> None:
        self._call("cuCtxDestroy_v2", c_void_p(ctx))

    def ctx_push(self, ctx: int) -> None:
        self._call("cuCtxPushCurrent_v2", c_void_p(ctx))

    def ctx_pop(self) -> int:
        popped = c_void_p()
        self._call("cuCtxPopCurrent_v2", byref(popped))
        return popped.value or 0

    def ctx_get_current(self) -> int:
        c = c_void_p()
        self._call("cuCtxGetCurrent", byref(c))
        return c.value or 0

    # -- streams and events (the current context's)
    def stream_create(self, flags: int = CU_STREAM_NON_BLOCKING) -> int:
        s = c_void_p()
        self._call("cuStreamCreate", byref(s), flags)
        return s.value or 0

    def stream_destroy(self, stream: int) -> None:
        self._call("cuStreamDestroy_v2", c_void_p(stream))

    def event_create(self, flags: int = CU_EVENT_BLOCKING_SYNC
                     | CU_EVENT_DISABLE_TIMING) -> int:
        e = c_void_p()
        self._call("cuEventCreate", byref(e), flags)
        return e.value or 0

    def event_destroy(self, event: int) -> None:
        self._call("cuEventDestroy_v2", c_void_p(event))

    def event_record(self, event: int, stream: int) -> None:
        self._call("cuEventRecord", c_void_p(event), c_void_p(stream))

    def event_query(self, event: int) -> bool:
        """True when the work before the event's last record has completed."""
        rc = self._lib.cuEventQuery(c_void_p(event))
        if rc == CUDA_SUCCESS:
            return True
        if rc == CUDA_ERROR_NOT_READY:
            return False
        raise CudaError("cuEventQuery", rc, self.error_name(rc))

    def event_sync(self, event: int) -> None:
        self._call("cuEventSynchronize", c_void_p(event))

    def stream_stall(self, stream: int, ms: int) -> None:
        """Hold `stream` for `ms` milliseconds: work queued on it after this
        call starts no earlier than that.

        The driver runs a C sleep function (kernel32 Sleep on Windows, usleep
        elsewhere) on its own callback thread, so the stall needs neither the
        GIL nor any Python code. The calling thread does not wait.
        """
        fn, units_per_ms = _sleep_host_fn()
        self._call("cuLaunchHostFunc", c_void_p(stream), c_void_p(fn),
                   c_void_p(max(1, int(ms)) * units_per_ms))

    # -- memory
    def host_alloc(self, nbytes: int, flags: int = CU_MEMHOSTALLOC_PORTABLE) -> int:
        p = c_void_p()
        self._call("cuMemHostAlloc", byref(p), c_size_t(nbytes), flags)
        return p.value or 0

    def host_free(self, ptr: int) -> None:
        self._call("cuMemFreeHost", c_void_p(ptr))

    def mem_info(self) -> tuple[int, int]:
        """(free, total) device memory in bytes. Needs a current context."""
        free, total = c_size_t(), c_size_t()
        self._call("cuMemGetInfo_v2", byref(free), byref(total))
        return free.value, total.value


_sleep_fn = None


def _sleep_host_fn() -> tuple[int, int]:
    """(address, argument units per millisecond) of a C function that takes
    one integer argument and sleeps for it: a valid CUDA host function.

    RULE: the host function is a C library function, never a ctypes
    callback. REASON: a ctypes callback takes the GIL on the driver's
    thread, and a thread inside PyNvVideoCodec's Encode holds the GIL while
    it may wait for that same stream, which deadlocks.
    """
    global _sleep_fn
    if _sleep_fn is None:
        if os.name == "nt":
            f = ctypes.WinDLL("kernel32").Sleep        # VOID Sleep(DWORD ms)
            _sleep_fn = (f, ctypes.cast(f, c_void_p).value, 1)
        else:
            f = ctypes.CDLL(None).usleep               # int usleep(useconds_t us)
            _sleep_fn = (f, ctypes.cast(f, c_void_p).value, 1000)
    return _sleep_fn[1], _sleep_fn[2]


def host_view(ptr: int, nbytes: int):
    """A writable uint8 numpy array over `nbytes` of host memory at `ptr`.

    The array does not own the memory. The caller keeps the allocation alive
    for as long as the array is used, and drops every view before freeing it.
    """
    import numpy as np
    buf = (ctypes.c_ubyte * nbytes).from_address(ptr)
    return np.frombuffer(buf, dtype=np.uint8)


_driver = None
_load_failure: CudaUnavailable | None = None
_lock = threading.Lock()


def get() -> Driver:
    """The process's driver, loaded on first use.

    Raises CudaUnavailable when the driver cannot be used. The failure is
    remembered, so every later call reports the same reason without retrying
    the load.
    """
    global _driver, _load_failure
    drv = _driver
    if drv is not None:
        return drv
    with _lock:
        if _driver is not None:
            return _driver
        if _load_failure is not None:
            raise CudaUnavailable(str(_load_failure))
        try:
            _driver = Driver.load()
        except CudaUnavailable as e:
            _load_failure = e
            raise
        return _driver


def available() -> bool:
    """Whether `get()` succeeds on this machine. Loads the driver."""
    try:
        get()
        return True
    except CudaUnavailable:
        return False


def set_driver(driver) -> object:
    """Install `driver` as the process's driver and return the previous one.

    None clears both the driver and a remembered load failure, so the next
    `get()` loads the real library. Tests use this to install a fake with the
    same methods as `Driver`.
    """
    global _driver, _load_failure
    with _lock:
        previous = _driver
        _driver = driver
        _load_failure = None
        return previous
