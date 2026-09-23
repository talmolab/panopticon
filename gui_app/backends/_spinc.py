"""Spinnaker C API binding: the one module that calls the Spinnaker SDK.

`SpinC` wraps the flat C API that ships with Teledyne's Spinnaker SDK
(`SpinnakerC_v140.dll` on Windows) through `ctypes`. No other Panopticon
module uses ctypes to talk to a FLIR camera. `flir.py` is written against the
methods of `SpinC`, and `fake_spinc.FakeSpinC` implements the same methods over
simulated cameras, so the backend runs unchanged against either one.

Why the C API through ctypes:
- `ctypes.CDLL` releases the GIL around every foreign call. A grab thread
  blocked in `spinCameraGetNextImageEx` therefore does not stall the other
  grab threads. PySpin does not document what it does here.
- `spinImageGetData` returns the address of the driver buffer. A numpy view
  over it stays valid until `spinImageRelease`, which is the zero-copy
  contract the grab loop needs. PySpin does not document whether `GetNDArray`
  copies.
- The DLL ships with the SDK. There is no wheel to install and no NumPy ABI to
  match.

What this module guarantees:
- Importing it loads no DLL and creates no SDK state. A Basler or simulated
  host is unaffected, and a spawned child can import it at no cost. The DLL
  loads when a `SpinC` is constructed.
- Every function it calls has its argument and return types declared before
  the first call. Without them ctypes passes a Python int as a 32-bit C int,
  which truncates the 64-bit handles and pointers this API passes everywhere.
- A required function missing from the DLL is named when the DLL loads, so an
  unsupported SDK fails at startup and not at the first frame.
- Handles are plain Python ints. A NULL handle is `None` where the method says
  so (`node()` for an absent node), and a `FlirError` everywhere else.
- `next_image` raises `FlirTimeout` when no image arrives in time and
  `FlirError(code)` for any other failure. Every other method raises
  `FlirError(code)` for a non-zero return code.

Threads:
One `SpinC` per process is shared by every grab thread. The per-frame methods
(`next_image`, `image_*`, `chunk_int`, `image_release`) write their out
parameters into ctypes objects that each thread allocates once, on its first
call. The only objects a frame creates are the ones `image_view` returns: a
ctypes array over the driver buffer and the numpy view on it. The cold-path
methods allocate as they go.

DLL discovery, where the first match wins:
  1. `sdk_dir` (the profile's `camera.flir.sdk_dir`), then
  2. the `PANOPTICON_SPINNAKER_DIR` environment variable, then
  3. the Spinnaker install roots, newest SDK line first:
     `%ProgramFiles%\\Teledyne\\Spinnaker` (4.x),
     `%ProgramFiles%\\FLIR Systems\\Spinnaker` (2.x and 3.x),
     `%ProgramFiles%\\Point Grey Research\\Spinnaker` (1.x), then
  4. every directory on `PATH`.
An explicit location (1 or 2) may name the install root or the folder that
holds the DLL. If it holds no DLL, loading stops with an error naming it.
Skipping it would load whichever other SDK the search finds next, and the log
would name an SDK the operator did not choose.

The DLL loads inside a scoped `os.add_dll_directory` for its own folder, so
its dependencies in that folder resolve. `PATH` is never edited: the SDK's
`bin64\\vs2015` folder also holds Qt5 DLLs, and PyQt5's copies must stay the
ones the process finds.
"""
from __future__ import annotations

import atexit
import ctypes
import os
import struct
import threading
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------- error codes
# spinError values from SpinnakerDefsC.h (the JavaCPP presets for Spinnaker
# 4.0.0.116 list the same values).
SPINNAKER_ERR_SUCCESS = 0
SPINNAKER_ERR_ERROR = -1001
SPINNAKER_ERR_NOT_INITIALIZED = -1002
SPINNAKER_ERR_NOT_IMPLEMENTED = -1003
SPINNAKER_ERR_RESOURCE_IN_USE = -1004
SPINNAKER_ERR_ACCESS_DENIED = -1005
SPINNAKER_ERR_INVALID_HANDLE = -1006
SPINNAKER_ERR_INVALID_ID = -1007
SPINNAKER_ERR_NO_DATA = -1008
SPINNAKER_ERR_INVALID_PARAMETER = -1009
SPINNAKER_ERR_IO = -1010
SPINNAKER_ERR_TIMEOUT = -1011
SPINNAKER_ERR_ABORT = -1012
SPINNAKER_ERR_INVALID_BUFFER = -1013
SPINNAKER_ERR_NOT_AVAILABLE = -1014
SPINNAKER_ERR_INVALID_ADDRESS = -1015
SPINNAKER_ERR_BUFFER_TOO_SMALL = -1016
SPINNAKER_ERR_INVALID_INDEX = -1017
SPINNAKER_ERR_PARSING_CHUNK_DATA = -1018
SPINNAKER_ERR_INVALID_VALUE = -1019
SPINNAKER_ERR_RESOURCE_EXHAUSTED = -1020
SPINNAKER_ERR_OUT_OF_MEMORY = -1021
SPINNAKER_ERR_BUSY = -1022
SPINNAKER_ERR_GENICAM_INVALID_ARGUMENT = -2001
SPINNAKER_ERR_GENICAM_OUT_OF_RANGE = -2002
SPINNAKER_ERR_GENICAM_PROPERTY = -2003
SPINNAKER_ERR_GENICAM_RUN_TIME = -2004
SPINNAKER_ERR_GENICAM_LOGICAL = -2005
SPINNAKER_ERR_GENICAM_ACCESS = -2006
SPINNAKER_ERR_GENICAM_TIMEOUT = -2007
SPINNAKER_ERR_GENICAM_DYNAMIC_CAST = -2008
SPINNAKER_ERR_GENICAM_GENERIC = -2009
SPINNAKER_ERR_GENICAM_BAD_ALLOCATION = -2010

#: Code -> name, for messages. Built from the constants above so the two
#: cannot disagree.
ERROR_NAMES = {value: name[len("SPINNAKER_"):]
               for name, value in dict(globals()).items()
               if name.startswith("SPINNAKER_ERR_")}

#: spinImageStatus values (SpinnakerDefsC.h). 3 is transport loss.
IMAGE_STATUS_NAMES = {
    -1: "UNKNOWN_ERROR",
    0: "NO_ERROR",
    1: "CRC_CHECK_FAILED",
    2: "DATA_OVERFLOW",
    3: "MISSING_PACKETS",
    4: "LEADER_BUFFER_SIZE_INCONSISTENT",
    5: "TRAILER_BUFFER_SIZE_INCONSISTENT",
    6: "PACKETID_INCONSISTENT",
    7: "MISSING_LEADER",
    8: "MISSING_TRAILER",
    9: "DATA_INCOMPLETE",
    10: "INFO_INCONSISTENT",
    11: "CHUNK_DATA_INVALID",
    12: "NO_SYSTEM_RESOURCES",
}
IMAGE_STATUS_MISSING_PACKETS = 3


def error_name(code: int) -> str:
    """`ERR_TIMEOUT` for -1011, and `spinError <code>` for a code not listed."""
    return ERROR_NAMES.get(int(code), f"spinError {int(code)}")


# ----------------------------------------------------------------- exceptions
class FlirSdkUnavailable(ImportError):
    """The Spinnaker C DLL cannot be used on this host.

    A subclass of `ImportError` so the launcher's existing "install the SDK"
    path handles it like any missing camera SDK. `searched` lists every
    location the discovery looked at, in order, so the message can say where
    the DLL was expected.
    """

    def __init__(self, message: str, searched=()):
        super().__init__(message)
        self.searched = tuple(searched)


class FlirError(RuntimeError):
    """A Spinnaker call returned a non-zero `spinError`.

    `code` is the raw value (for example -1004 when another program holds the
    camera), `name` its symbolic name and `what` the call or node that failed.
    """

    def __init__(self, code: int, what: str = ""):
        self.code = int(code)
        self.name = error_name(self.code)
        self.what = what
        detail = f"Spinnaker error {self.code} ({self.name})"
        super().__init__(f"{what}: {detail}" if what else detail)


class FlirTimeout(Exception):
    """No image arrived within the timeout (`SPINNAKER_ERR_TIMEOUT`).

    Kept apart from `FlirError` because the grab loop treats a timeout as the
    normal end of a recording (the triggers stopped) and every other failure
    as a fault. An `except FlirError` must never swallow it.
    """

    code = SPINNAKER_ERR_TIMEOUT


# -------------------------------------------------------------- DLL discovery
#: Environment variable that names a Spinnaker install when the profile does
#: not.
ENV_SDK_DIR = "PANOPTICON_SPINNAKER_DIR"

if os.name == "nt":
    #: The release build of the C API. The debug build (`SpinnakerCd_v140`)
    #: links the debug C runtime and is never loaded.
    DLL_NAME = "SpinnakerC_v140.dll"
    #: Where the DLL sits inside an install root.
    DLL_SUBDIR = ("bin64", "vs2015")
    #: Vendor folders under Program Files, newest SDK line first.
    INSTALL_VENDORS = ("Teledyne", "FLIR Systems", "Point Grey Research")
else:
    DLL_NAME = "libSpinnaker_C.so"
    DLL_SUBDIR = ("lib",)
    INSTALL_VENDORS = ()

#: Pointer width of this interpreter. The SDK ships a 64-bit DLL only.
_POINTER_BITS = struct.calcsize("P") * 8

_INSTALL_HINT = (
    "Install the Spinnaker SDK 4.x from Teledyne (the full Windows x64 "
    "installer), or set camera.flir.sdk_dir in the profile, or the "
    f"{ENV_SDK_DIR} environment variable, to its install folder.")


def _program_files(env) -> list:
    """Program Files folders to search, without duplicates, in order.

    `ProgramW6432` comes first because it names the 64-bit folder even when
    `ProgramFiles` has been redirected. The literal default is used only when
    the environment names neither.
    """
    out, seen = [], set()
    for key in ("ProgramW6432", "ProgramFiles"):
        value = env.get(key)
        if value and value.lower() not in seen:
            seen.add(value.lower())
            out.append(Path(value))
    return out or [Path(r"C:\Program Files")]


def _candidates_in(folder: Path) -> list:
    """The DLL's possible paths under a folder the user named: the folder
    itself, and the install layout below it."""
    direct = folder / DLL_NAME
    nested = folder.joinpath(*DLL_SUBDIR) / DLL_NAME
    return [direct] if direct == nested else [direct, nested]


def find_library(sdk_dir=None, environ=None) -> tuple:
    """Locate the Spinnaker C DLL. Returns `(path, searched)`.

    `searched` is every path looked at, in order, each labelled with where it
    came from. Raises `FlirSdkUnavailable` when an explicit location holds no
    DLL or when nothing is found. `environ` defaults to `os.environ`; tests
    pass their own.
    """
    env = os.environ if environ is None else environ
    searched = []

    for folder, label in ((sdk_dir, "camera.flir.sdk_dir"),
                          (env.get(ENV_SDK_DIR), ENV_SDK_DIR)):
        if not folder:
            continue
        paths = _candidates_in(Path(folder))
        searched.extend(f"{p} ({label})" for p in paths)
        for p in paths:
            if p.is_file():
                return p, tuple(searched)
        raise FlirSdkUnavailable(
            f"{label} is {folder}, but {DLL_NAME} is not there (looked for "
            + " and ".join(str(p) for p in paths)
            + "). Point it at the Spinnaker install folder, or remove it so "
            "the default install locations are searched.", searched)

    for vendor in INSTALL_VENDORS:
        for base in _program_files(env):
            p = base.joinpath(vendor, "Spinnaker", *DLL_SUBDIR, DLL_NAME)
            searched.append(f"{p} (install root)")
            if p.is_file():
                return p, tuple(searched)

    for entry in env.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry:
            continue
        p = Path(entry) / DLL_NAME
        searched.append(f"{p} (PATH)")
        if p.is_file():
            return p, tuple(searched)

    raise FlirSdkUnavailable(
        f"The Spinnaker SDK is not installed: {DLL_NAME} was not found. "
        + _INSTALL_HINT + " Searched:\n  " + "\n  ".join(searched), searched)


def _open_dll(path: Path):
    """Load the DLL with its own folder added to the search for this call only.

    `CDLL`, never `PyDLL`: a `PyDLL` keeps the GIL through every call, which
    would turn each thread's wait for a frame into a stall of every thread.
    """
    if hasattr(os, "add_dll_directory"):
        with os.add_dll_directory(str(path.parent)):
            return ctypes.CDLL(str(path))
    return ctypes.CDLL(str(path))


def _load_library(sdk_dir, environ) -> tuple:
    """`(lib, dll_path, searched)`, or `FlirSdkUnavailable` naming the cause."""
    if _POINTER_BITS != 64:
        raise FlirSdkUnavailable(
            f"The Spinnaker C API ships as a 64-bit DLL, and this Python is "
            f"{_POINTER_BITS}-bit. Run Panopticon with a 64-bit Python.")
    path, searched = find_library(sdk_dir, environ)
    try:
        lib = _open_dll(path)
    except OSError as e:
        raise FlirSdkUnavailable(
            f"{path} was found but could not be loaded ({e}). A DLL it "
            "depends on may be missing, or the SDK install may be damaged. "
            "Reinstall the Spinnaker SDK 4.x.", searched) from e
    return lib, str(path), searched


# ------------------------------------------------------------ prototype table
# The argument types below extend the prototype table in octacam's
# `src/octacam/cameras/spinnaker_c.py` (https://github.com/NeLy-EPFL/octacam),
# used under the MIT License, whose notice is reproduced here as it requires:
#
#   MIT License
#
#   Copyright (c) 2026 Neuroengineering Laboratory @EPFL - Ramdya Lab
#
#   Permission is hereby granted, free of charge, to any person obtaining a copy
#   of this software and associated documentation files (the "Software"), to deal
#   in the Software without restriction, including without limitation the rights
#   to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#   copies of the Software, and to permit persons to whom the Software is
#   furnished to do so, subject to the following conditions:
#
#   The above copyright notice and this permission notice shall be included in all
#   copies or substantial portions of the Software.
#
#   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#   IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#   FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#   AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#   LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#   OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#   SOFTWARE.
#
# The entries octacam does not have (frame ID, padding, image status, chunk
# data, library version, enumeration by index) were checked against the
# Spinnaker 4.0.0.116 C headers as the JavaCPP presets reproduce them.
# Every function returns `spinError`, a C int.

class _LibraryVersion(ctypes.Structure):
    """`spinLibraryVersion`: four unsigned ints."""
    _fields_ = [("major", ctypes.c_uint), ("minor", ctypes.c_uint),
                ("type", ctypes.c_uint), ("build", ctypes.c_uint)]


_P = ctypes.POINTER
_V = ctypes.c_void_p          # every opaque handle, and void*
_SZ = ctypes.c_size_t
_I64 = ctypes.c_int64
_U64 = ctypes.c_uint64
_DBL = ctypes.c_double
_U8 = ctypes.c_uint8          # bool8_t
_CH = ctypes.c_char_p         # const char* in, and char* buffers out
_I32 = ctypes.c_int           # enums: spinImageStatus

#: Function name -> argtypes.
PROTOTYPES = {
    # System and camera list
    "spinSystemGetInstance": (_P(_V),),
    "spinSystemReleaseInstance": (_V,),
    "spinSystemGetCameras": (_V, _V),
    "spinSystemGetLibraryVersion": (_V, _P(_LibraryVersion)),
    "spinCameraListCreateEmpty": (_P(_V),),
    "spinCameraListClear": (_V,),
    "spinCameraListDestroy": (_V,),
    "spinCameraListGetSize": (_V, _P(_SZ)),
    "spinCameraListGet": (_V, _SZ, _P(_V)),
    # Camera lifecycle
    "spinCameraInit": (_V,),
    "spinCameraDeInit": (_V,),
    "spinCameraRelease": (_V,),
    "spinCameraIsInitialized": (_V, _P(_U8)),
    "spinCameraIsStreaming": (_V, _P(_U8)),
    "spinCameraGetNodeMap": (_V, _P(_V)),
    "spinCameraGetTLDeviceNodeMap": (_V, _P(_V)),
    "spinCameraGetTLStreamNodeMap": (_V, _P(_V)),
    "spinCameraBeginAcquisition": (_V,),
    "spinCameraEndAcquisition": (_V,),
    "spinCameraGetNextImageEx": (_V, _U64, _P(_V)),
    # Image
    "spinImageIsIncomplete": (_V, _P(_U8)),
    "spinImageGetStatus": (_V, _P(_I32)),
    "spinImageGetStatusDescription": (_I32, _CH, _P(_SZ)),
    "spinImageGetFrameID": (_V, _P(_U64)),
    "spinImageGetTimeStamp": (_V, _P(_U64)),
    "spinImageGetPaddingX": (_V, _P(_SZ)),
    "spinImageGetPaddingY": (_V, _P(_SZ)),
    "spinImageGetWidth": (_V, _P(_SZ)),
    "spinImageGetHeight": (_V, _P(_SZ)),
    "spinImageGetStride": (_V, _P(_SZ)),
    "spinImageGetBitsPerPixel": (_V, _P(_SZ)),
    "spinImageGetData": (_V, _P(_V)),
    "spinImageChunkDataGetIntValue": (_V, _CH, _P(_I64)),
    "spinImageRelease": (_V,),
    # GenApi nodes
    "spinNodeMapGetNode": (_V, _CH, _P(_V)),
    "spinNodeIsAvailable": (_V, _P(_U8)),
    "spinNodeIsReadable": (_V, _P(_U8)),
    "spinNodeIsWritable": (_V, _P(_U8)),
    "spinIntegerGetValue": (_V, _P(_I64)),
    "spinIntegerSetValue": (_V, _I64),
    "spinIntegerGetMin": (_V, _P(_I64)),
    "spinIntegerGetMax": (_V, _P(_I64)),
    "spinIntegerGetInc": (_V, _P(_I64)),
    "spinFloatGetValue": (_V, _P(_DBL)),
    "spinFloatSetValue": (_V, _DBL),
    "spinFloatGetMin": (_V, _P(_DBL)),
    "spinFloatGetMax": (_V, _P(_DBL)),
    "spinEnumerationGetCurrentEntry": (_V, _P(_V)),
    "spinEnumerationGetEntryByName": (_V, _CH, _P(_V)),
    "spinEnumerationGetNumEntries": (_V, _P(_SZ)),
    "spinEnumerationGetEntryByIndex": (_V, _SZ, _P(_V)),
    "spinEnumerationEntryGetIntValue": (_V, _P(_I64)),
    "spinEnumerationEntryGetSymbolic": (_V, _CH, _P(_SZ)),
    "spinEnumerationSetIntValue": (_V, _I64),
    "spinBooleanGetValue": (_V, _P(_U8)),
    "spinBooleanSetValue": (_V, _U8),
    "spinCommandExecute": (_V,),
    "spinStringGetValue": (_V, _CH, _P(_SZ)),
}

#: Functions whose absence costs one feature, not the backend. Chunk data is
#: needed only for trigger-counter block IDs and chunk timestamps, the status
#: text only for a log line, and the version only for the log.
OPTIONAL_FUNCTIONS = frozenset({
    "spinImageChunkDataGetIntValue",
    "spinImageGetStatusDescription",
    "spinSystemGetLibraryVersion",
})

#: Buffer length for C strings. 256 is the length Teledyne's C examples use.
_MAX_BUFF_LEN = 256


def _configure(lib) -> tuple:
    """Declare every function's types. Returns `(missing, missing_optional)`.

    Missing names are collected rather than raised one at a time, so a
    message can list all of them at once.
    """
    missing, missing_optional = [], []
    for name, argtypes in PROTOTYPES.items():
        try:
            fn = getattr(lib, name)
        except AttributeError:
            (missing_optional if name in OPTIONAL_FUNCTIONS
             else missing).append(name)
            continue
        fn.restype = ctypes.c_int
        fn.argtypes = list(argtypes)
    return missing, missing_optional


class _Scratch:
    """One thread's out parameters for the per-frame calls.

    Each value has a pointer object made once, so a call passes an existing
    object instead of building a `byref` per frame.
    """

    __slots__ = ("img", "p_img", "data", "p_data", "u64", "p_u64", "sza",
                 "p_sza", "szb", "p_szb", "u8", "p_u8", "i64", "p_i64")

    def __init__(self):
        self.img = ctypes.c_void_p()
        self.p_img = ctypes.pointer(self.img)
        self.data = ctypes.c_void_p()
        self.p_data = ctypes.pointer(self.data)
        self.u64 = ctypes.c_uint64()
        self.p_u64 = ctypes.pointer(self.u64)
        self.sza = ctypes.c_size_t()
        self.p_sza = ctypes.pointer(self.sza)
        self.szb = ctypes.c_size_t()
        self.p_szb = ctypes.pointer(self.szb)
        self.u8 = ctypes.c_uint8()
        self.p_u8 = ctypes.pointer(self.u8)
        self.i64 = ctypes.c_int64()
        self.p_i64 = ctypes.pointer(self.i64)


def _as_int(value, what: str) -> int:
    """An integer node value. A float with a fraction is refused instead of
    truncated, because a truncated ROI or buffer count is a different setting
    from the one asked for."""
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"{what}: {value!r} is not an integer")
        return int(value)
    return int(value)


class SpinC:
    """The Spinnaker C API, one method per operation the backend needs.

    Construct one per process: `SpinC(sdk_dir)` finds and loads the DLL (see
    the module docstring) and raises `FlirSdkUnavailable` if it cannot. `lib`
    replaces the DLL with any object whose attributes are ctypes function
    pointers; the tests use it to run this class's real type declarations
    against callbacks. After construction:

    - `dll_path` is the DLL that was loaded ("" when `lib` was given).
    - `searched` lists the locations the discovery looked at.
    - `missing_optional` names the optional functions this SDK lacks.

    Node methods take the handle `node()` returned. `node()` returns None for
    a node the camera does not have, the flag methods answer False for None,
    and every other node method raises `FlirError` for it.
    """

    def __init__(self, sdk_dir=None, *, lib=None, environ=None):
        self.dll_path = ""
        self.searched: tuple = ()
        if lib is None:
            lib, self.dll_path, self.searched = _load_library(sdk_dir, environ)
        missing, missing_optional = _configure(lib)
        if missing:
            where = self.dll_path or "the supplied library"
            raise FlirSdkUnavailable(
                f"{where} lacks {len(missing)} Spinnaker C function(s) "
                f"Panopticon needs: {', '.join(missing)}. This SDK version is "
                "not supported. " + _INSTALL_HINT, self.searched)
        self.missing_optional = tuple(missing_optional)
        self._lib = lib
        self._tls = threading.local()
        self._lock = threading.Lock()
        self._system = None
        self._camlist = None
        self._atexit = False
        #: Camera handles `cameras()` handed out and `release()` has not taken
        #: back. `system_release()` releases any that are left, because the
        #: SDK refuses to release a system whose cameras are still referenced.
        self._cams: set = set()
        #: Node handle -> node name, so an error names the node.
        self._names: dict = {}
        #: Byte count -> ctypes array type, for `image_view`. Creating the
        #: type costs microseconds, so it is made once per frame size.
        self._arrays: dict = {}
        self._chunk_names: dict = {}
        # The per-frame functions, bound once.
        self._f_next = lib.spinCameraGetNextImageEx
        self._f_incomplete = lib.spinImageIsIncomplete
        self._f_frame_id = lib.spinImageGetFrameID
        self._f_timestamp = lib.spinImageGetTimeStamp
        self._f_pad_x = lib.spinImageGetPaddingX
        self._f_pad_y = lib.spinImageGetPaddingY
        self._f_data = lib.spinImageGetData
        self._f_release = lib.spinImageRelease
        self._f_chunk_int = (None if "spinImageChunkDataGetIntValue"
                             in self.missing_optional
                             else lib.spinImageChunkDataGetIntValue)

    def __repr__(self) -> str:
        return f"<SpinC {self.dll_path or 'custom library'}>"

    # ------------------------------------------------------------- internals
    def _scratch(self) -> _Scratch:
        try:
            return self._tls.s
        except AttributeError:
            s = self._tls.s = _Scratch()
            return s

    def _label(self, node) -> str:
        return self._names.get(node) or f"node {node!r}"

    def _need(self, node, what: str):
        if node is None:
            raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                            f"{what}: the node is absent on this camera")
        return node

    def _string(self, fn, first, what: str) -> str:
        """Read a C string through `fn(first, buf, &len)`, retrying once with
        the length the SDK asks for if 256 bytes were not enough."""
        size = _MAX_BUFF_LEN
        for _ in range(2):
            buf = ctypes.create_string_buffer(size)
            n = ctypes.c_size_t(size)
            err = fn(first, buf, ctypes.byref(n))
            if err == SPINNAKER_ERR_BUFFER_TOO_SMALL and n.value > size:
                size = int(n.value) + 1
                continue
            if err:
                raise FlirError(err, what)
            return buf.value.decode("utf-8", "replace")
        raise FlirError(SPINNAKER_ERR_BUFFER_TOO_SMALL, what)

    def _flag(self, fn, handle, what: str) -> bool:
        if handle is None:
            return False
        b = ctypes.c_uint8()
        err = fn(handle, ctypes.byref(b))
        if err:
            raise FlirError(err, what)
        return bool(b.value)

    def _out_handle(self, fn, first, what: str) -> int:
        h = ctypes.c_void_p()
        err = fn(first, ctypes.byref(h))
        if err:
            raise FlirError(err, what)
        if not h.value:
            raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                            f"{what} returned a NULL handle")
        return h.value

    # ----------------------------------------------------- system and cameras
    def system_get(self) -> int:
        """The process's Spinnaker system, acquired on first use.

        Released by `system_release()`, which also runs at interpreter exit.
        """
        with self._lock:
            if self._system is None:
                self._system = self._system_instance()
                if not self._atexit:
                    atexit.register(self.system_release)
                    self._atexit = True
            return self._system

    def _system_instance(self) -> int:
        h = ctypes.c_void_p()
        err = self._lib.spinSystemGetInstance(ctypes.byref(h))
        if err:
            raise FlirError(err, "spinSystemGetInstance")
        if not h.value:
            raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                            "spinSystemGetInstance returned a NULL system")
        return h.value

    def system_release(self) -> None:
        """Release every camera handle still held, the camera list, and then
        the system. Safe to call more than once.

        Leftover handles are released first because the SDK refuses to release
        a system whose cameras are still referenced. That cleanup is best
        effort; a failure to release the system itself raises.
        """
        with self._lock:
            leftovers, self._cams = list(self._cams), set()
            for cam in leftovers:
                for step in (self._end_if_streaming, self._deinit_if_initialized,
                             self._release_handle):
                    try:
                        step(cam)
                    except FlirError:
                        pass
            if self._camlist is not None:
                for fn in (self._lib.spinCameraListClear,
                           self._lib.spinCameraListDestroy):
                    try:
                        fn(self._camlist)
                    except Exception:
                        pass
                self._camlist = None
            system, self._system = self._system, None
        if system is not None:
            err = self._lib.spinSystemReleaseInstance(system)
            if err:
                raise FlirError(err, "spinSystemReleaseInstance")

    def _end_if_streaming(self, cam) -> None:
        if self.is_streaming(cam):
            self.end(cam)

    def _deinit_if_initialized(self, cam) -> None:
        if self.is_initialized(cam):
            self.deinit(cam)

    def _release_handle(self, cam) -> None:
        err = self._lib.spinCameraRelease(cam)
        if err:
            raise FlirError(err, "spinCameraRelease")

    def library_version(self) -> str:
        """`"major.minor.type.build"` of the loaded SDK, or "unknown" when the
        SDK lacks `spinSystemGetLibraryVersion`."""
        if "spinSystemGetLibraryVersion" in self.missing_optional:
            return "unknown"
        v = _LibraryVersion()
        err = self._lib.spinSystemGetLibraryVersion(self.system_get(),
                                                    ctypes.byref(v))
        if err:
            raise FlirError(err, "spinSystemGetLibraryVersion")
        return f"{v.major}.{v.minor}.{v.type}.{v.build}"

    def cameras(self) -> list:
        """Handles of every camera the SDK detects, in the SDK's order.

        The order is whatever the transport layers report, so a caller that
        needs a stable order sorts by serial. Each handle must go back through
        `release()`. The camera list is refilled on every call and held until
        `system_release()`.
        """
        system = self.system_get()
        lib = self._lib
        with self._lock:
            if self._camlist is None:
                self._camlist = self._create_list()
            else:
                err = lib.spinCameraListClear(self._camlist)
                if err:
                    raise FlirError(err, "spinCameraListClear")
            err = lib.spinSystemGetCameras(system, self._camlist)
            if err:
                raise FlirError(err, "spinSystemGetCameras")
            n = ctypes.c_size_t()
            err = lib.spinCameraListGetSize(self._camlist, ctypes.byref(n))
            if err:
                raise FlirError(err, "spinCameraListGetSize")
            out = []
            for i in range(int(n.value)):
                h = ctypes.c_void_p()
                err = lib.spinCameraListGet(self._camlist, i, ctypes.byref(h))
                if err:
                    raise FlirError(err, f"spinCameraListGet({i})")
                if not h.value:
                    raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                                    f"spinCameraListGet({i}) returned NULL")
                out.append(h.value)
                self._cams.add(h.value)
            return out

    def _create_list(self) -> int:
        h = ctypes.c_void_p()
        err = self._lib.spinCameraListCreateEmpty(ctypes.byref(h))
        if err:
            raise FlirError(err, "spinCameraListCreateEmpty")
        if not h.value:
            raise FlirError(SPINNAKER_ERR_INVALID_HANDLE,
                            "spinCameraListCreateEmpty returned NULL")
        return h.value

    def camera_serial(self, cam) -> str:
        """`DeviceSerialNumber` from the transport-layer device nodemap, which
        is readable before `init()`."""
        node = self.node(self.nodemap(cam, "tldevice"), "DeviceSerialNumber")
        if node is None:
            raise FlirError(SPINNAKER_ERR_NOT_AVAILABLE,
                            "DeviceSerialNumber is absent from the TL device "
                            "nodemap")
        return self.string_get(node)

    def init(self, cam) -> None:
        err = self._lib.spinCameraInit(cam)
        if err:
            raise FlirError(err, "spinCameraInit")

    def deinit(self, cam) -> None:
        err = self._lib.spinCameraDeInit(cam)
        if err:
            raise FlirError(err, "spinCameraDeInit")

    def release(self, cam) -> None:
        """Give back a handle from `cameras()`. The handle is invalid after."""
        self._cams.discard(cam)
        self._release_handle(cam)

    def is_initialized(self, cam) -> bool:
        return self._flag(self._lib.spinCameraIsInitialized, cam,
                          "spinCameraIsInitialized")

    # ------------------------------------------------------------------ nodes
    _NODEMAP_FUNCS = {"device": "spinCameraGetNodeMap",
                      "tldevice": "spinCameraGetTLDeviceNodeMap",
                      "tlstream": "spinCameraGetTLStreamNodeMap"}

    def nodemap(self, cam, which: str = "device") -> int:
        """One of the camera's three nodemaps.

        "device" is the camera's own GenICam nodemap and needs `init()` first.
        "tldevice" (identity, interface type) and "tlstream" (buffers, stream
        counters) are the transport layer's, readable before `init()`.
        """
        name = self._NODEMAP_FUNCS.get(which)
        if name is None:
            raise ValueError(f"nodemap {which!r}: use one of "
                             f"{sorted(self._NODEMAP_FUNCS)}")
        return self._out_handle(getattr(self._lib, name), cam, name)

    def node(self, nodemap, name: str):
        """The named node, or None when this camera has no such node.

        An invalid or uninitialised nodemap still raises, so a caller bug is
        not reported as a missing feature.
        """
        h = ctypes.c_void_p()
        err = self._lib.spinNodeMapGetNode(nodemap, name.encode("ascii"),
                                           ctypes.byref(h))
        if err in (SPINNAKER_ERR_INVALID_HANDLE, SPINNAKER_ERR_NOT_INITIALIZED):
            raise FlirError(err, f"spinNodeMapGetNode({name})")
        if err or not h.value:
            return None
        self._names[h.value] = name
        return h.value

    def node_available(self, node) -> bool:
        return self._flag(self._lib.spinNodeIsAvailable, node,
                          f"{self._label(node)}: spinNodeIsAvailable")

    def node_readable(self, node) -> bool:
        return self._flag(self._lib.spinNodeIsReadable, node,
                          f"{self._label(node)}: spinNodeIsReadable")

    def node_writable(self, node) -> bool:
        return self._flag(self._lib.spinNodeIsWritable, node,
                          f"{self._label(node)}: spinNodeIsWritable")

    def _get_num(self, fn, ctype, node, what):
        self._need(node, what)
        v = ctype()
        err = fn(node, ctypes.byref(v))
        if err:
            raise FlirError(err, f"{self._label(node)}: {what}")
        return v.value

    def int_get(self, node) -> int:
        return self._get_num(self._lib.spinIntegerGetValue, ctypes.c_int64,
                             node, "spinIntegerGetValue")

    def int_set(self, node, value) -> None:
        self._need(node, "spinIntegerSetValue")
        v = _as_int(value, self._label(node))
        err = self._lib.spinIntegerSetValue(node, v)
        if err:
            raise FlirError(err, f"{self._label(node)}: spinIntegerSetValue({v})")

    def int_min(self, node) -> int:
        return self._get_num(self._lib.spinIntegerGetMin, ctypes.c_int64,
                             node, "spinIntegerGetMin")

    def int_max(self, node) -> int:
        return self._get_num(self._lib.spinIntegerGetMax, ctypes.c_int64,
                             node, "spinIntegerGetMax")

    def int_inc(self, node) -> int:
        return self._get_num(self._lib.spinIntegerGetInc, ctypes.c_int64,
                             node, "spinIntegerGetInc")

    def float_get(self, node) -> float:
        return self._get_num(self._lib.spinFloatGetValue, ctypes.c_double,
                             node, "spinFloatGetValue")

    def float_set(self, node, value) -> None:
        self._need(node, "spinFloatSetValue")
        v = float(value)
        err = self._lib.spinFloatSetValue(node, v)
        if err:
            raise FlirError(err, f"{self._label(node)}: spinFloatSetValue({v})")

    def float_min(self, node) -> float:
        return self._get_num(self._lib.spinFloatGetMin, ctypes.c_double,
                             node, "spinFloatGetMin")

    def float_max(self, node) -> float:
        return self._get_num(self._lib.spinFloatGetMax, ctypes.c_double,
                             node, "spinFloatGetMax")

    def enum_get_symbolic(self, node) -> str:
        """The current entry's symbolic name, for example "Mono8"."""
        self._need(node, "spinEnumerationGetCurrentEntry")
        entry = self._out_handle(self._lib.spinEnumerationGetCurrentEntry,
                                 node, f"{self._label(node)}: "
                                       "spinEnumerationGetCurrentEntry")
        return self._string(self._lib.spinEnumerationEntryGetSymbolic, entry,
                            f"{self._label(node)}: "
                            "spinEnumerationEntryGetSymbolic")

    def enum_set_symbolic(self, node, symbolic: str) -> None:
        """Select an entry by name (entry -> its integer -> SetIntValue, the
        path Teledyne's C examples use)."""
        label = self._label(self._need(node, "spinEnumerationGetEntryByName"))
        entry = ctypes.c_void_p()
        err = self._lib.spinEnumerationGetEntryByName(
            node, str(symbolic).encode("ascii"), ctypes.byref(entry))
        if err or not entry.value:
            raise FlirError(err or SPINNAKER_ERR_NOT_AVAILABLE,
                            f"{label}: no entry {symbolic!r}")
        value = ctypes.c_int64()
        err = self._lib.spinEnumerationEntryGetIntValue(entry.value,
                                                        ctypes.byref(value))
        if err:
            raise FlirError(err, f"{label}: entry {symbolic!r} "
                                 "spinEnumerationEntryGetIntValue")
        err = self._lib.spinEnumerationSetIntValue(node, value.value)
        if err:
            raise FlirError(err, f"{label}: spinEnumerationSetIntValue"
                                 f"({symbolic!r})")

    def enum_entries(self, node) -> list:
        """Symbolic names of the entries that are available now, in the
        camera's order. An entry the camera lists but reports unavailable is
        left out, because selecting it would fail."""
        label = self._label(self._need(node, "spinEnumerationGetNumEntries"))
        n = ctypes.c_size_t()
        err = self._lib.spinEnumerationGetNumEntries(node, ctypes.byref(n))
        if err:
            raise FlirError(err, f"{label}: spinEnumerationGetNumEntries")
        out = []
        for i in range(int(n.value)):
            entry = ctypes.c_void_p()
            err = self._lib.spinEnumerationGetEntryByIndex(node, i,
                                                           ctypes.byref(entry))
            if err:
                raise FlirError(err, f"{label}: "
                                     f"spinEnumerationGetEntryByIndex({i})")
            if not entry.value or not self._flag(
                    self._lib.spinNodeIsAvailable, entry.value,
                    f"{label}: entry {i} spinNodeIsAvailable"):
                continue
            out.append(self._string(
                self._lib.spinEnumerationEntryGetSymbolic, entry.value,
                f"{label}: entry {i} spinEnumerationEntryGetSymbolic"))
        return out

    def bool_get(self, node) -> bool:
        return bool(self._get_num(self._lib.spinBooleanGetValue,
                                  ctypes.c_uint8, node, "spinBooleanGetValue"))

    def bool_set(self, node, value) -> None:
        self._need(node, "spinBooleanSetValue")
        err = self._lib.spinBooleanSetValue(node, 1 if value else 0)
        if err:
            raise FlirError(err, f"{self._label(node)}: spinBooleanSetValue")

    def command(self, node) -> None:
        """Execute a command node (for example `UserSetLoad`)."""
        self._need(node, "spinCommandExecute")
        err = self._lib.spinCommandExecute(node)
        if err:
            raise FlirError(err, f"{self._label(node)}: spinCommandExecute")

    def string_get(self, node) -> str:
        self._need(node, "spinStringGetValue")
        return self._string(self._lib.spinStringGetValue, node,
                            f"{self._label(node)}: spinStringGetValue")

    # ------------------------------------------------------------ acquisition
    def begin(self, cam) -> None:
        err = self._lib.spinCameraBeginAcquisition(cam)
        if err:
            raise FlirError(err, "spinCameraBeginAcquisition")

    def end(self, cam) -> None:
        """Stop acquiring. Every image must be released first: the SDK
        discards the pool on this call."""
        err = self._lib.spinCameraEndAcquisition(cam)
        if err:
            raise FlirError(err, "spinCameraEndAcquisition")

    def is_streaming(self, cam) -> bool:
        return self._flag(self._lib.spinCameraIsStreaming, cam,
                          "spinCameraIsStreaming")

    def next_image(self, cam, timeout_ms: int) -> int:
        """Block up to `timeout_ms` for the next image and return its handle.

        The GIL is released for the whole wait. Raises `FlirTimeout` when
        nothing arrives, and `FlirError` for any other failure. On a timeout
        no image exists, so there is nothing to release.
        """
        timeout_ms = int(timeout_ms)
        if timeout_ms < 0:
            raise ValueError(f"timeout_ms {timeout_ms} is negative")
        s = self._scratch()
        # The scratch keeps the last call's handle, so it is cleared first:
        # a call that succeeds without writing then reads as no image, not
        # as the previous, already released one.
        s.img.value = None
        err = self._f_next(cam, timeout_ms, s.p_img)
        if not err:
            h = s.img.value
            if h:
                return h
            raise FlirError(SPINNAKER_ERR_NO_DATA,
                            "spinCameraGetNextImageEx returned no image")
        if err == SPINNAKER_ERR_TIMEOUT:
            raise FlirTimeout(f"no image within {timeout_ms} ms")
        raise FlirError(err, "spinCameraGetNextImageEx")

    # ------------------------------------------------------------------ image
    def image_incomplete(self, image) -> bool:
        s = self._scratch()
        err = self._f_incomplete(image, s.p_u8)
        if err:
            raise FlirError(err, "spinImageIsIncomplete")
        return s.u8.value != 0

    def image_status(self, image) -> int:
        """`spinImageStatus` of an image; 3 means packets went missing."""
        v = ctypes.c_int()
        err = self._lib.spinImageGetStatus(image, ctypes.byref(v))
        if err:
            raise FlirError(err, "spinImageGetStatus")
        return int(v.value)

    def image_status_text(self, image) -> str:
        """The SDK's description of the image's status, for a log line."""
        status = self.image_status(image)
        if "spinImageGetStatusDescription" in self.missing_optional:
            return IMAGE_STATUS_NAMES.get(status, f"image status {status}")
        return self._string(self._lib.spinImageGetStatusDescription, status,
                            "spinImageGetStatusDescription")

    def image_frame_id(self, image) -> int:
        s = self._scratch()
        err = self._f_frame_id(image, s.p_u64)
        if err:
            raise FlirError(err, "spinImageGetFrameID")
        return s.u64.value

    def image_timestamp(self, image) -> int:
        """The image's device timestamp as the SDK reports it (documented in
        nanoseconds; the backend's self-test checks the unit)."""
        s = self._scratch()
        err = self._f_timestamp(image, s.p_u64)
        if err:
            raise FlirError(err, "spinImageGetTimeStamp")
        return s.u64.value

    def image_padding(self, image) -> tuple:
        """`(padding_x, padding_y)`: bytes at the end of each line, and of the
        image."""
        s = self._scratch()
        err = self._f_pad_x(image, s.p_sza)
        if err:
            raise FlirError(err, "spinImageGetPaddingX")
        err = self._f_pad_y(image, s.p_szb)
        if err:
            raise FlirError(err, "spinImageGetPaddingY")
        return s.sza.value, s.szb.value

    def image_dims(self, image) -> tuple:
        """`(width, height)` of an image in pixels."""
        s = self._scratch()
        err = self._lib.spinImageGetWidth(image, s.p_sza)
        if err:
            raise FlirError(err, "spinImageGetWidth")
        err = self._lib.spinImageGetHeight(image, s.p_szb)
        if err:
            raise FlirError(err, "spinImageGetHeight")
        return s.sza.value, s.szb.value

    def image_stride(self, image) -> int:
        """Bytes per row. The (H, W) view in `image_view` assumes it equals
        the width."""
        s = self._scratch()
        err = self._lib.spinImageGetStride(image, s.p_sza)
        if err:
            raise FlirError(err, "spinImageGetStride")
        return s.sza.value

    def image_bpp(self, image) -> int:
        """Bits per pixel. The capture path handles 8 only."""
        s = self._scratch()
        err = self._lib.spinImageGetBitsPerPixel(image, s.p_sza)
        if err:
            raise FlirError(err, "spinImageGetBitsPerPixel")
        return s.sza.value

    def image_data_ptr(self, image) -> int:
        """Address of the image's pixels in the driver buffer. Valid until
        `image_release()`."""
        s = self._scratch()
        s.data.value = None           # as in next_image: no stale pointer
        err = self._f_data(image, s.p_data)
        if err:
            raise FlirError(err, "spinImageGetData")
        ptr = s.data.value
        if not ptr:
            raise FlirError(SPINNAKER_ERR_NO_DATA,
                            "spinImageGetData returned NULL")
        return ptr

    def image_view(self, image, width: int, height: int):
        """A (height, width) uint8 numpy view over the driver buffer, no copy.

        The view is valid until `image_release()`, and the caller must not let
        it outlive that call. It reads `width * height` bytes from the start
        of the buffer, so check `image_padding`, `image_stride` and
        `image_bpp` before taking it: a wider pixel or a padded row gives a
        sheared image and no error.
        """
        n = int(width) * int(height)
        array_type = self._arrays.get(n)
        if array_type is None:
            array_type = self._arrays[n] = ctypes.c_ubyte * n
        raw = array_type.from_address(self.image_data_ptr(image))
        return np.frombuffer(raw, dtype=np.uint8).reshape(int(height),
                                                          int(width))

    def chunk_int(self, image, name: str) -> int:
        """An integer chunk value carried by the image (chunk mode must have
        been active when acquisition began). `name` is passed to the SDK as
        given."""
        if self._f_chunk_int is None:
            raise FlirError(SPINNAKER_ERR_NOT_IMPLEMENTED,
                            "spinImageChunkDataGetIntValue is not in this "
                            "Spinnaker SDK")
        key = self._chunk_names.get(name)
        if key is None:
            key = self._chunk_names[name] = name.encode("ascii")
        s = self._scratch()
        err = self._f_chunk_int(image, key, s.p_i64)
        if err:
            raise FlirError(err, f"spinImageChunkDataGetIntValue({name})")
        return s.i64.value

    def image_release(self, image) -> None:
        """Return the image's buffer to the pool. Call once per image."""
        err = self._f_release(image)
        if err:
            raise FlirError(err, "spinImageRelease")
