"""Diagnostic probe for FLIR / Teledyne cameras: what each camera reports,
which input the trigger wire is on, and the behaviours Panopticon's FLIR
backend cannot know until a real camera shows them.

Run it on a FLIR rig before the first recording, and send the JSON it writes
(or the zip that --collect makes) to the maintainers. Every stage of one run
writes into one JSON, probe_out/flir_probe_<host>_<YYYYmmdd-HHMMSS>.json, next
to a log of the run in Panopticon's timestamped log format. The probe prints
a PASS/FAIL table at the end and exits 1 when any check fails.

    uv run probe_flir.py --list                   # serials, models, capabilities; nothing streams
    uv run probe_flir.py --find-line              # the input line each camera's trigger wire is on
    uv run probe_flir.py --selftest               # free-run tests: frame ID, clock, chunks, GIL
    uv run probe_flir.py --triggered 60           # 60 s of triggered capture through the FLIR backend
    uv run probe_flir.py --all                    # the four stages above
    uv run probe_flir.py --collect <session dir>  # zip a session's logs and metadata, no video
    uv run probe_flir.py --fake                   # rehearse every stage on simulated cameras

Optional stages: --exposure-sweep (the real exposure ceiling at frame_rate),
--wrap-test (the 16-bit frame-ID wrap, about 65,600 frames) and --pyspin
(PySpin's GetNDArray and GIL, when PySpin is installed).

The profile: --profile takes a profile name or a path to a profile file. The
default is the profile the GUI last used, when it is a FLIR profile, else the
only FLIR profile in profiles/. --list and --selftest run without one; the
other stages need it for the trigger line, the trigger board and the camera
settings.

What each stage changes on the cameras:
- --list reads nodes. It moves selectors to read under each entry and puts
  them back.
- --find-line reads each camera's line status while the trigger board runs at
  10 Hz for about a second per camera.
- --selftest loads the user set the profile names (Default without a profile),
  as the FLIR backend does at open, then free-runs each camera with the
  settings it measures with.
- --triggered opens the cameras through the FLIR backend from the profile, as
  the GUI does, and runs the trigger board at the profile's frame_rate. Its
  counter checks then run the board at 10 Hz on the same cameras.
The probe writes nothing to disk except its JSON, its log and the --collect
zip. It logs at the verbose level (debug when the profile asks for debug), so
every camera setting it writes appears with the value the camera reads back.

The trigger board: the probe starts the profile's board only after the board
reports the recording-only sketch, the one the GUI puts on the board at
launch, so a board that may carry a stimulation paradigm is never started. It
stands the board down when it finishes, also after an error. Opening the
board's serial port resets the board, and its pins float for about a second
during the reset, as they do when the GUI launches. With
trigger_source: external the probe asks you to start and stop your own trigger
source instead, and the counter checks and the exposure sweep, which need
Panopticon's board, are skipped with a note saying so.

The probe refuses to start beside a running Panopticon (gui_app/probe_guard.py),
except for a --collect-only run, which opens nothing.

--fake runs every stage against simulated cameras (gui_app/backends/
fake_spinc.py) and the simulated trigger board, with the default profile
profiles/templates/flir_sim.yaml. It opens no serial port and loads no SDK.
"""
from __future__ import annotations

import argparse
import contextlib
import ctypes
import dataclasses
import hashlib
import importlib
import json
import math
import os
import platform
import re
import socket
import statistics
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent
# The checkout this file sits in, so `python probe_flir.py` run from another
# folder imports this copy of gui_app.
sys.path.insert(0, str(REPO))

from gui_app import logging_setup, probe_guard  # noqa: E402

#: The JSON's schema name. A reader that finds another name reads another
#: layout.
SCHEMA = "panopticon.flir_probe/1"

#: Where the JSON, the log and the --collect zip go by default.
DEFAULT_OUT_DIR = REPO / "probe_out"
#: Where the GUI writes its logs (gui.py LOG_DIR).
DEFAULT_LOG_DIR = REPO / "logs"
#: The profile --fake uses when none is given.
FAKE_PROFILE = REPO / "profiles" / "templates" / "flir_sim.yaml"

#: --triggered's duration when the flag is given without one, in seconds,
#: and the duration --fake uses when it runs every stage by itself.
TRIGGERED_DEFAULT_S = 60.0
FAKE_TRIGGERED_S = 20.0

#: --find-line: the board's rate, how long each camera's lines are watched,
#: the pause between two reads, and the transitions a line needs to count as
#: wired. A 10 Hz square wave gives about 20 transitions in a second.
FIND_LINE_HZ = 10
FIND_LINE_S = 1.0
FIND_LINE_SAMPLE_S = 0.002
FIND_LINE_MIN_TOGGLES = 4

#: --selftest: the free-run rate, acquisitions and frames per acquisition for
#: the frame-ID and clock tests, and the host pool they run with. Three
#: restarts tell a counter that restarts from one that keeps counting.
SELFTEST_FPS = 30.0
SELFTEST_CYCLES = 3
SELFTEST_FRAMES = 5
SELFTEST_BUFFERS = 10
#: A clock whose frame interval differs from the camera's reported frame
#: period by more than this share is in another unit (125 MHz ticks read
#: raw are 8 times off).
TS_UNIT_TOL = 0.05
#: A restart must advance the device clock by at least this share of the
#: host time between the two frames; a clock that restarts goes backwards.
TS_CONTINUITY_SHARE = 0.5

#: GIL meter: the free-run rate the waiting thread sees (low, so the thread
#: spends its time inside the wait), the windows measured, and the held
#: fraction above which the wait counts as holding the GIL.
GIL_FPS = 2.0
GIL_WINDOW_S = 1.0
GIL_CONTROL_S = 0.4
GIL_HELD_FAIL = 0.5

#: --triggered: the retrieve timeout of the probe's grab threads, how long
#: after the board's stop every camera must have timed out, the consecutive
#: frame errors that end a camera's grab, and the per-frame thread CPU above
#: which the hot path is reported. The budget is the one CLAUDE.md states for
#: GIL-held work per thread per frame.
GRAB_TIMEOUT_MS = 200
STOP_GRACE_S = 3.0
MAX_FRAME_ERRORS = 10
HOT_PATH_BUDGET_US = 300.0

#: Counter checks: the board rate, and the phases of each run. Run A reads
#: the CounterValue chunk; run B polls the counters while frames are taken,
#: then while none are, then again; the delay test ends an acquisition while
#: a delayed exposure is pending.
COUNTER_HZ = 10
RUN_A_S = 2.5
RUN_B_DRAIN_S = 2.0
RUN_B_PAUSE_S = 2.5
RUN_B_RESUME_S = 0.5
RUN_B_BUFFERS = 10
COUNTER_POLL_S = 0.001
DELAY_TEST_MAX_US = 50_000.0
#: Run C: an exposure this many trigger periods long makes the camera ignore
#: about every other trigger, so the witness has a known number to count.
IGNORE_EXPOSURE_PERIODS = 1.5
RUN_C_S = 2.0

#: --exposure-sweep: grid steps from the profile's exposure to
#: SWEEP_TOP_PERIODS trigger periods (past the period, so the grid crosses
#: the ceiling), halvings between the last good and the first bad step, and
#: seconds of triggers at each step.
SWEEP_STEPS = 8
SWEEP_TOP_PERIODS = 1.1
SWEEP_BISECT = 3
SWEEP_STEP_S = 2.0

#: --wrap-test: frames past 65535 that prove IDs do not wrap, and the longest
#: the test runs per camera.
WRAP_PAST = 64
WRAP_TIMEOUT_S = 900.0

#: --collect: the file names taken from the session folder (anything else,
#: every video included, stays out), and how many probe JSONs are added.
COLLECT_NAMES = ("session.log", "session_metadata.json", "blockids.npy",
                 "frametimes.npy", "WARNINGS.txt")
COLLECT_PROBE_JSONS = 20

STATUSES = ("PASS", "FAIL", "WARN", "SKIP", "INFO")
STAGES = ("list", "find_line", "selftest", "triggered", "exposure_sweep",
          "wrap_test", "pyspin", "collect")

#: Every behaviour the FLIR backend depends on that is unknown until a real
#: camera shows it: id -> (question, where the backend depends on it, the
#: stage that measures it). The first eleven are the UNKNOWNS of
#: gui_app/backends/flir.py's module docstring, in its order; the rest are the
#: other open questions of the FLIR design. Every id appears in the JSON,
#: with an answer per camera or the reason it was not measured.
UNKNOWNS = {
    "exposure_max_follows_frame_rate": (
        "Does ExposureTime's maximum follow AcquisitionFrameRate?",
        "FlirBackend.exposure_ceiling_us", "selftest"),
    "chunk_name_spelling": (
        "Which chunk-name spelling does spinImageChunkDataGetIntValue take?",
        "flir.CHUNK_SPELLINGS", "selftest"),
    "counter_chunk_latch": (
        "Does an image's CounterValue chunk count the trigger edge that "
        "started the image?", "FlirCamera._bid_counter", "triggered"),
    "counter_chunk_carrier": (
        "Which counter does the CounterValue chunk carry: the one "
        "ChunkCounterSelector names, or the one CounterSelector names when "
        "acquisition starts?", "FlirCamera._select_edge_counter",
        "selftest, triggered"),
    "counter_width": (
        "How wide is each counter (the CounterValue maximum)?",
        "FlirBackend._wrap_reach", "list"),
    "counters_read_while_streaming": (
        "Can CounterValue be read while the camera streams?",
        "FlirCamera._read_before_end", "selftest, triggered"),
    "exposure_latency_after_edge": (
        "How long after its edge and its TriggerDelay does the exposure "
        "counter count a triggered exposure?", "FlirCamera._read_settled",
        "triggered"),
    "delayed_exposure_at_end": (
        "Is a trigger whose delayed exposure has not started when "
        "EndAcquisition runs still exposed?", "FlirCamera.StopGrabbing",
        "triggered"),
    "exposes_while_host_stopped": (
        "Does the camera keep exposing while the host takes no frames?",
        "FlirBackend._ignored_sentences (stall re-arms)", "triggered"),
    "temperature_nodes": (
        "Which temperature status and threshold nodes does the model have?",
        "FlirBackend.thermals", "list"),
    "temperature_threshold_entry": (
        "Which DeviceTemperatureSelector entry do DeviceTemperatureStatus "
        "and the DeviceTemperatureStatusTransition thresholds refer to?",
        "FlirBackend._temp_basis", "list"),
    "counters_count_as_set": (
        "Do Counter0 and Counter1 count trigger-line edges and ExposureStart "
        "with only the nodes the backend sets?", "the trigger witness",
        "triggered"),
    "counter_chunk_supported": (
        "Does ChunkSelector offer CounterValue, and is there a "
        "ChunkCounterSelector?", "block_id_source trigger_counter", "list"),
    "trigger_delay_behaviour": (
        "Is TriggerDelay readable and writable in and out of trigger mode, "
        "and what does it read back?", "FlirBackend._set_triggered",
        "selftest"),
    "frame_id_restart_and_base": (
        "Does the frame ID restart at every BeginAcquisition, from 0 or 1, "
        "stepping by 1?", "FlirBackend._judge_ids", "selftest"),
    "timestamp_unit_and_continuity": (
        "Is the device timestamp non-zero, in nanoseconds, and continuous "
        "across EndAcquisition and BeginAcquisition?",
        "FlirBackend._judge_timestamps", "selftest"),
    "user_set_load": (
        "Does UserSetLoad work, and which of the settings the backend writes "
        "does it change?", "FlirBackend._load_user_set", "selftest"),
    "stream_buffer_count_max": (
        "What is StreamBufferCountMax on this host?",
        "FlirBackend._apply_buffers", "list"),
    "stream_counters_across_end": (
        "Do the stream counters reset at EndAcquisition?",
        "FlirBackend.stream_stats", "triggered"),
    "gige_transport": (
        "GigE only: which StreamMode entries, packet sizes and extended-ID "
        "modes does the camera offer?", "FlirBackend._apply_transport",
        "list"),
    "zero_copy_capi": (
        "Does spinImageGetData hand out the pool's own buffers (a view, not a "
        "copy)?", "FlirResult.GetArrayZeroCopy", "selftest"),
    "gil_capi_wait": (
        "Does a thread waiting in spinCameraGetNextImageEx release the GIL?",
        "the choice of the C API", "selftest"),
    "hot_path_cost": (
        "How much thread CPU does the backend's per-frame path take?",
        "the grab loop's budget", "triggered"),
    "exposure_ceiling_real": (
        "What is the longest exposure that still acquires every trigger at "
        "frame_rate?", "FlirBackend.exposure_ceiling_us", "exposure_sweep"),
    "wrap_16bit": (
        "How does a 16-bit GigE frame ID wrap, and do extended IDs pass "
        "65535?", "FlirCamera._id16_step", "wrap_test"),
    "pyspin_getndarray": (
        "Is PySpin's GetNDArray a view or a copy, and does a PySpin wait "
        "release the GIL?", "the choice of the C API", "pyspin"),
}

#: The GenICam type each node the probe reads is expected to have. A read
#: tries this type first and the others after it, because a model may type a
#: node differently.
NODE_KINDS = {
    **{n: "string" for n in (
        "DeviceModelName", "DeviceVendorName", "DeviceSerialNumber",
        "DeviceFirmwareVersion", "DeviceVersion", "DeviceUserID")},
    **{n: "int" for n in (
        "Width", "Height", "WidthMax", "HeightMax", "SensorWidth",
        "SensorHeight", "OffsetX", "OffsetY", "PayloadSize",
        "DeviceLinkSpeed", "DeviceLinkThroughputLimit", "DeviceMaxThroughput",
        "GevSCPSPacketSize", "GevSCPD", "GevCurrentIPAddress",
        "GevCurrentSubnetMask", "GevTimestampTickFrequency",
        "TimestampIncrement", "CounterValue", "CounterValueAtReset",
        "LineStatusAll", "StreamBufferCountManual", "StreamBufferCountMax",
        "StreamBufferCountResult", "GevDeviceIPAddress",
        "GevDeviceSubnetMask", "StreamStartedFrameCount",
        "StreamDeliveredFrameCount", "StreamReceivedFrameCount",
        "StreamIncompleteFrameCount", "StreamLostFrameCount",
        "StreamDroppedFrameCount", "StreamMissedPacketCount")},
    **{n: "float" for n in (
        "ExposureTime", "Gain", "AcquisitionFrameRate",
        "AcquisitionResultingFrameRate", "TriggerDelay", "DeviceTemperature",
        "DeviceTemperatureStatusTransition", "BlackLevel", "Gamma",
        "DeviceTemperatureMax", "DeviceTemperatureMin")},
    **{n: "enum" for n in (
        "PixelFormat", "TriggerSelector", "TriggerMode", "TriggerSource",
        "TriggerActivation", "TriggerOverlap", "LineSelector", "LineMode",
        "LineFormat", "CounterSelector", "CounterEventSource",
        "CounterEventActivation", "ChunkSelector", "ChunkCounterSelector",
        "DeviceTemperatureSelector", "DeviceTemperatureStatus",
        "DeviceTemperatureStatusTransitionSelector", "UserSetSelector",
        "UserSetDefault", "GevGVSPExtendedIDMode",
        "DeviceLinkThroughputLimitMode", "ExposureAuto", "GainAuto",
        "ExposureMode", "AcquisitionMode", "AdcBitDepth", "DeviceType",
        "DeviceCurrentSpeed", "StreamBufferCountMode",
        "StreamBufferHandlingMode", "StreamMode")},
    **{n: "bool" for n in (
        "AcquisitionFrameRateEnable", "ChunkModeActive", "ChunkEnable",
        "GammaEnable", "LineInverter", "LineStatus")},
}
_KINDS = ("int", "float", "enum", "bool", "string")

#: Temperature node names looked for on every camera. SpinC cannot list a
#: node map, so the probe asks for each name it knows of.
TEMPERATURE_NODES = (
    "DeviceTemperature", "DeviceTemperatureSelector", "DeviceTemperatureStatus",
    "DeviceTemperatureStatusTransitionSelector",
    "DeviceTemperatureStatusTransition", "DeviceTemperatureMax",
    "DeviceTemperatureMin")

#: Settings the backend writes at open, read before and after UserSetLoad.
USER_SET_NODES = ("ExposureTime", "ExposureAuto", "Gain", "GainAuto",
                  "PixelFormat", "Width", "Height", "AcquisitionFrameRate",
                  "AcquisitionFrameRateEnable", "TriggerMode", "TriggerSource",
                  "TriggerOverlap", "GammaEnable", "ChunkModeActive")


# ================================================================== helpers
def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _host_tag() -> str:
    name = socket.gethostname() or "host"
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:40] or "host"


def _git_commit():
    try:
        r = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    return r.stdout.strip() or None if r.returncode == 0 else None


def _sha256(path: Path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def _jsonable(obj):
    """The value as JSON-ready data: tuples and sets become lists, keys
    become text, NaN and infinity become None, and anything else unknown
    becomes its text."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((_jsonable(v) for v in obj), key=str)
    if isinstance(obj, bool) or obj is None or isinstance(obj, (int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(dataclasses.asdict(obj))
    if isinstance(obj, Path):
        return str(obj)
    try:
        import numpy as np
        if isinstance(obj, np.generic):
            return _jsonable(obj.item())
    except Exception:
        pass
    return str(obj)


def _write_json(path: Path, data) -> None:
    """Write `data` to `path` through a temporary file, so a reader never
    sees half a JSON."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_jsonable(data), indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _unique_base(out_dir: Path, base: str) -> str:
    name, k = base, 2
    while (out_dir / f"{name}.json").exists() or (out_dir / f"{name}.log").exists():
        name = f"{base}-{k}"
        k += 1
    return name


def _err(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}"


class _CycleClock:
    """Thread CPU time in microseconds for the calling thread.

    Windows QueryThreadCycleTime counts the cycles a thread executed, so a
    wait for the GIL or the scheduler costs nothing on it; its rate is
    calibrated once against perf_counter on a busy loop. Other platforms use
    time.thread_time_ns. Cycles on a hybrid CPU are approximate, which is
    enough to tell 30 us from 300 us per frame."""

    def __init__(self):
        self.basis = "time.thread_time_ns"
        self._query = None
        self.per_us = 1000.0
        if os.name == "nt":
            try:
                from ctypes import wintypes
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                q = k32.QueryThreadCycleTime
                q.argtypes = [wintypes.HANDLE,
                              ctypes.POINTER(ctypes.c_ulonglong)]
                q.restype = wintypes.BOOL
                k32.GetCurrentThread.restype = wintypes.HANDLE
                self._me = k32.GetCurrentThread()
                self._q = q
                self._query = True
                self.basis = "QueryThreadCycleTime"
                self.per_us = self._calibrate()
            except Exception:
                self._query = None
                self.basis = "time.thread_time_ns"
                self.per_us = 1000.0

    def _cycles(self) -> int:
        buf = ctypes.c_ulonglong()
        if not self._q(self._me, ctypes.byref(buf)):
            raise OSError("QueryThreadCycleTime failed")
        return buf.value

    def _calibrate(self) -> float:
        c0, t0 = self._cycles(), time.perf_counter()
        x = 0
        while time.perf_counter() - t0 < 0.1:
            for i in range(5000):
                x += i
        c1, t1 = self._cycles(), time.perf_counter()
        return max(1.0, (c1 - c0) / ((t1 - t0) * 1e6))

    def now(self) -> int:
        if self._query:
            return self._cycles()
        return time.thread_time_ns()

    def us(self, delta: int) -> float:
        return delta / self.per_us


# ---------------------------------------------------------------- GIL meter
class _Competitor(threading.Thread):
    """Pure-Python work that needs the GIL: its count rate falls by the share
    of the time another thread holds the GIL."""

    def __init__(self):
        super().__init__(daemon=True, name="probe-gil-meter")
        self.n = 0
        self.running = True

    def run(self):
        n = 0
        while self.running:
            n += 1
            if not n & 1023:
                self.n = n
        self.n = n


def _gil_holding_sleep():
    """A sleep that keeps the GIL: a foreign call through ctypes.PyDLL does
    not release it. The meter's positive control."""
    if os.name == "nt":
        fn = ctypes.PyDLL("kernel32").Sleep
        fn.argtypes = [ctypes.c_uint32]
        fn.restype = None
        return lambda s: fn(max(1, int(s * 1000)))
    fn = ctypes.PyDLL(None).usleep
    fn.argtypes = [ctypes.c_uint32]
    fn.restype = ctypes.c_int
    return lambda s: fn(max(1, int(s * 1e6)))


class _GilMeter:
    """The share of wall time a load thread holds the GIL, from a competitor
    thread's count rate with the load running against its rate alone:
    held = 1 - rate_with_load / rate_alone."""

    def __init__(self):
        self.comp = _Competitor()
        self.comp.start()
        time.sleep(0.05)
        self.alone = self._rate(GIL_CONTROL_S)

    def _rate(self, seconds: float) -> float:
        n0, t0 = self.comp.n, time.perf_counter()
        time.sleep(seconds)
        n1, t1 = self.comp.n, time.perf_counter()
        return (n1 - n0) / max(t1 - t0, 1e-9)

    def held(self, load, seconds: float) -> dict:
        """Run `load(stop_event)` on its own thread for `seconds` and return
        the held fraction and what the load reported."""
        stop = threading.Event()
        box = {}

        def body():
            try:
                box["result"] = load(stop)
            except Exception as e:
                box["error"] = _err(e)

        t = threading.Thread(target=body, daemon=True, name="probe-gil-load")
        t.start()
        time.sleep(0.05)
        rate = self._rate(seconds)
        stop.set()
        t.join(10.0)
        frac = 1.0 - rate / self.alone if self.alone > 0 else float("nan")
        out = {"held_fraction": round(min(1.0, max(0.0, frac)), 3)
               if math.isfinite(frac) else None,
               "competitor_rate": round(rate), "alone_rate": round(self.alone)}
        out.update(box)
        return out

    def close(self):
        self.comp.running = False
        self.comp.join(2.0)


# ------------------------------------------------------- raw node access
class _Raw:
    """One camera's nodes through the SpinC methods, for the probe's own
    measurements. Nothing here raises: a node the camera lacks reads None,
    and every failure is kept in `errors` for the JSON."""

    def __init__(self, api, handle, who: str):
        self.api = api
        self.h = handle
        self.who = who
        self.errors: list = []
        self._maps: dict = {}
        self._kind: dict = {}

    def _note(self, text: str) -> None:
        if len(self.errors) < 200:
            self.errors.append(text)

    def node(self, name: str, which: str = "device"):
        try:
            m = self._maps.get(which)
            if m is None:
                m = self._maps[which] = self.api.nodemap(self.h, which)
            return self.api.node(m, name)
        except Exception as e:
            self._note(f"{which}.{name}: {_err(e)}")
            return None

    def has(self, name: str, which: str = "device") -> bool:
        n = self.node(name, which)
        try:
            return n is not None and bool(self.api.node_available(n))
        except Exception:
            return False

    def writable(self, name: str, which: str = "device") -> bool:
        n = self.node(name, which)
        try:
            return n is not None and bool(self.api.node_writable(n))
        except Exception:
            return False

    def readable(self, name: str, which: str = "device") -> bool:
        n = self.node(name, which)
        try:
            return n is not None and bool(self.api.node_readable(n))
        except Exception:
            return False

    def _getter(self, kind):
        a = self.api
        return {"int": a.int_get, "float": a.float_get,
                "enum": a.enum_get_symbolic, "bool": a.bool_get,
                "string": a.string_get}[kind]

    def read(self, name: str, which: str = "device"):
        n = self.node(name, which)
        if n is None:
            return None
        try:
            if not self.api.node_readable(n):
                return None
        except Exception as e:
            self._note(f"{which}.{name}: {_err(e)}")
            return None
        first = self._kind.get((which, name)) or NODE_KINDS.get(name)
        order = ([first] if first else []) + [k for k in _KINDS if k != first]
        last = None
        for kind in order:
            try:
                value = self._getter(kind)(n)
            except Exception as e:
                last = e
                continue
            self._kind[(which, name)] = kind
            return value
        self._note(f"{which}.{name}: unreadable ({_err(last)})")
        return None

    def entries(self, name: str, which: str = "device") -> list:
        n = self.node(name, which)
        if n is None:
            return []
        try:
            return list(self.api.enum_entries(n))
        except Exception as e:
            self._note(f"{which}.{name} entries: {_err(e)}")
            return []

    def rng(self, name: str, which: str = "device"):
        """{"min", "max", "inc"?, "value"} of a number node, or None."""
        n = self.node(name, which)
        if n is None:
            return None
        a = self.api
        kind = self._kind.get((which, name)) or NODE_KINDS.get(name, "int")
        out = {}
        try:
            if kind == "float":
                out = {"min": a.float_min(n), "max": a.float_max(n)}
            else:
                out = {"min": a.int_min(n), "max": a.int_max(n),
                       "inc": a.int_inc(n)}
        except Exception:
            try:
                out = {"min": a.float_min(n), "max": a.float_max(n)}
            except Exception as e:
                self._note(f"{which}.{name} range: {_err(e)}")
        out["value"] = self.read(name, which)
        return out

    def write(self, name: str, value, which: str = "device"):
        """Write `value` (its Python type picks the node type) and return
        None, or the error text."""
        n = self.node(name, which)
        if n is None:
            return f"{name}: the camera has no such node"
        a = self.api
        try:
            if isinstance(value, bool):
                a.bool_set(n, value)
            elif isinstance(value, int):
                a.int_set(n, value)
            elif isinstance(value, float):
                a.float_set(n, value)
            else:
                a.enum_set_symbolic(n, str(value))
        except Exception as e:
            text = f"{name} = {value!r}: {_err(e)}"
            self._note(text)
            return text
        return None

    def execute(self, name: str):
        n = self.node(name)
        if n is None:
            return f"{name}: the camera has no such node"
        try:
            self.api.command(n)
        except Exception as e:
            text = f"{name}: {_err(e)}"
            self._note(text)
            return text
        return None

    @contextlib.contextmanager
    def under(self, selector: str, entry: str, which: str = "device"):
        """Select `entry` of `selector` for the block, then put the selector
        back. Yields whether the entry could be selected."""
        before = self.read(selector, which)
        ok = self.write(selector, entry, which) is None
        try:
            yield ok
        finally:
            if before is not None and before != entry:
                self.write(selector, before, which)


def _counter_read(raw: _Raw, selector: str):
    """CounterValue of counter `selector`, or None."""
    if raw.write("CounterSelector", selector) is not None:
        return None
    return raw.read("CounterValue")


def _line_bit(name: str):
    digits = "".join(ch for ch in str(name) if ch.isdigit())
    return int(digits) if digits else None


# ============================================================ trigger sources
def _fake_flash(ino: str) -> None:
    """--fake: put `ino` on the simulated board, as the GUI's launch does on
    a real one."""
    from gui_app.backends import sim_board
    sim_board.accept_upload(ino)


class _BoardSource:
    """Panopticon's trigger board, through TeensyController.

    open() refuses a board that does not report the recording-only sketch for
    this profile's pins, because any other sketch may run a stimulation
    paradigm when the board starts."""

    kind = "board"
    host_started = True

    def __init__(self, profile, fake: bool):
        from gui_app import stim_compiler
        self.fake = fake
        self.port = "sim" if fake else (profile.serial_port or "")
        self.pins = list(profile.trigger_pins or [])
        self.safe = list(profile.stim_safe_pins or [])
        self.sketch = stim_compiler.recording_only_sketch(self.safe, self.pins)
        self.want_id = stim_compiler.sketch_id(self.sketch)
        self.teensy = None
        self.heard_id = None
        self.stopped = None

    def describe(self) -> str:
        return f"the trigger board on {self.port}"

    def open(self):
        """None when the board is ready, else why it cannot be used."""
        if not self.port:
            return ("the profile names no serial_port, so there is no trigger "
                    "board to start. Set serial_port to the board's COM port, "
                    "or use trigger_source: external with your own source.")
        if not self.pins:
            return "the profile lists no trigger_pins for the board to drive."
        from gui_app.serial_controller import TeensyController
        if self.fake:
            _fake_flash(self.sketch)
        elif self.safe:
            print(f"[probe] opening {self.port} resets the trigger board, and "
                  f"its pins float for about a second during the reset. This "
                  f"profile lists stim_safe_pins {self.safe}: switch off what "
                  f"they drive, or block the beam, before the probe runs.",
                  flush=True)
        t = TeensyController(port=self.port)
        if not t.open(retries=3):
            return (f"could not open the trigger board on {self.port}: "
                    f"{t.last_error}")
        self.teensy = t
        # A stop is always safe to send, and its ack carries the sketch id.
        heard = t.identify(self.pins)
        self.heard_id = heard
        if heard is None or heard != self.want_id:
            said = (f"reports sketch {heard}" if heard else
                    "did not report a sketch identity")
            return (f"the trigger board on {self.port} {said}, not the "
                    f"recording-only sketch {self.want_id} for this profile's "
                    f"trigger_pins and stim_safe_pins, so it may carry a "
                    f"stimulation paradigm. The probe does not start a board "
                    f"it cannot show is stimulation-free. Launch Panopticon "
                    f"with this profile once (it puts the recording-only "
                    f"sketch on the board at launch), quit it, and run the "
                    f"probe again.")
        return None

    def before_arm(self, fps) -> None:
        pass

    def start(self, fps, what: str = "") -> bool:
        return bool(self.teensy.start_triggers(self.pins, int(round(fps))))

    def stop(self, what: str = "") -> bool:
        ok = bool(self.teensy.stop_triggers(self.pins))
        self.stopped = ok
        return ok

    def close(self):
        """Stand the board down and close the link; True when the board is
        known to be stopped, None when it was never opened."""
        if self.teensy is None:
            return None
        ok = self.teensy.stop_and_close(self.pins)
        self.teensy = None
        return bool(ok)


class _FakeOperator:
    """--fake with trigger_source external: stands in for the person who runs
    the external source. It starts the simulated source a moment after the
    probe's prompt and stops it when asked. `start_before_arm` models a
    source already running when the cameras arm, which the probe must
    refuse."""

    delay_s = 0.3
    start_before_arm = False

    def __init__(self):
        from gui_app.backends import sim_board
        self.clock = sim_board.SimExternalClock()
        self._timer = None

    def before_arm(self, fps) -> None:
        if self.start_before_arm:
            self.clock.start(fps)

    def start(self, fps) -> None:
        if self.clock.running:
            return
        self._timer = threading.Timer(self.delay_s, self.clock.start, (fps,))
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer.join(2.0)
        self.clock.stop()


class _ExternalSource:
    """The operator's own TTL source (trigger_source: external). The probe
    prints what to do and watches the cameras; it cannot start or stop the
    source itself."""

    kind = "external"
    host_started = False

    def __init__(self, profile, fake: bool):
        self.fps = profile.frame_rate
        self.op = _FakeOperator() if fake else None
        self.stopped = None

    def describe(self) -> str:
        return "your trigger source"

    def open(self):
        return None

    def before_arm(self, fps) -> None:
        if self.op is not None:
            self.op.before_arm(fps)

    def start(self, fps, what: str = "") -> bool:
        from gui_app.trigger_source import (FIRST_TRIGGER_TIMEOUT_S,
                                            ExternalTriggerSource)
        if what == "find-line":
            print(f"[probe] Start your trigger source now, at {fps:g} Hz. The "
                  f"probe watches each camera's input lines for up to "
                  f"{FIRST_TRIGGER_TIMEOUT_S:.0f} s.", flush=True)
        else:
            print("[probe] " + ExternalTriggerSource.prompt_text(
                fps, FIRST_TRIGGER_TIMEOUT_S, what or "probe run"), flush=True)
        if self.op is not None:
            self.op.start(fps)
        return True

    def stop(self, what: str = "") -> bool:
        from gui_app.trigger_source import STOP_WAIT_S, ExternalTriggerSource
        if what == "find-line":
            print("[probe] You can stop your trigger source now.", flush=True)
        else:
            print("[probe] " + ExternalTriggerSource.stop_prompt_text(
                STOP_WAIT_S, what or "probe run"), flush=True)
        if self.op is not None:
            self.op.stop()
        self.stopped = True
        return True

    def close(self):
        if self.op is not None:
            self.op.stop()
        return None


# ================================================================ the probe
class Probe:
    """One run: the arguments, the report, the checks table, and the camera
    backend the stages share."""

    def __init__(self, args, out_dir: Path, base: str):
        self.args = args
        self.fake = bool(args.fake)
        self.out_dir = out_dir
        self.json_path = out_dir / f"{base}.json"
        self.log_path = out_dir / f"{base}.log"
        self.profile = None
        self.profile_path = None
        self.backend = None
        self.api = None
        self._devices = None
        self.clock = None
        self.report = {
            "schema": SCHEMA, "created": _now_iso(), "fake": self.fake,
            "git_commit": _git_commit(), "argv": list(args.argv),
            "host": {}, "sdk": {}, "profile": {}, "stages": {},
            "cameras": [], "find_line": None, "selftest": {},
            "triggered": None, "exposure_sweep": None, "wrap_test": None,
            "pyspin": None, "collect": None, "unknowns": {}, "checks": [],
            "errors": [], "log_file": str(self.log_path)}
        for uid, (question, where, stage) in UNKNOWNS.items():
            self.report["unknowns"][uid] = {
                "question": question, "where": where, "stage": stage,
                "answers": {}, "settled": False, "not_measured": None}

    # ----------------------------------------------------------- bookkeeping
    def check(self, stage: str, name: str, status: str, detail: str = ""):
        if status not in STATUSES:
            raise ValueError(f"status {status!r}")
        self.report["checks"].append({"stage": stage, "check": name,
                                      "status": status, "detail": detail})
        line = f"[probe] {status:<4} {stage}: {name}"
        print(line + (f" ({detail})" if detail else ""), flush=True)

    def answer(self, uid: str, who: str, answer, **evidence):
        """Record camera (or host) `who`'s answer to unknown `uid`."""
        entry = self.report["unknowns"][uid]
        entry["answers"][str(who)] = {"answer": answer, **evidence}

    def not_measured(self, uid: str, why: str):
        entry = self.report["unknowns"][uid]
        if not entry["answers"]:
            entry["not_measured"] = why

    def error(self, stage: str, e: BaseException):
        self.report["errors"].append({"stage": stage, "error": _err(e),
                                      "traceback": traceback.format_exc()})

    def save(self):
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            _write_json(self.json_path, self.report)
        except Exception as e:
            print(f"[probe] could not write {self.json_path}: {_err(e)}",
                  flush=True)

    # ------------------------------------------------------------- set-up
    def load_profile(self):
        from gui_app.session_config import ProfileError, RigProfile
        from gui_app.settings import KEY_PROFILE, app_settings
        sec = self.report["profile"]
        want = self.args.profile
        path = None
        if want:
            cand = Path(want)
            if cand.suffix.lower() in (".yaml", ".yml") and cand.is_file():
                path = cand
            else:
                for p in RigProfile.list_profiles():
                    if p.stem.lower() == want.lower():
                        path = p
                if path is None:
                    for p in RigProfile.list_profiles():
                        try:
                            if RigProfile.load(p).name.lower() == want.lower():
                                path = p
                        except ProfileError:
                            continue
            if path is None:
                sec["load_error"] = f"no profile named {want!r}"
        elif self.fake:
            path = FAKE_PROFILE
        else:
            path = self._default_profile_path()
        if path is None:
            sec.setdefault("load_error", None)
            sec["name"] = None
            return
        sec["path"] = str(path)
        sec["sha256"] = _sha256(path)
        try:
            prof = RigProfile.load(path)
        except ProfileError as e:
            sec["load_error"] = str(e)
            return
        backend = prof.camera_backend
        if backend not in ("flir", "flir_sim"):
            sec["load_error"] = (f"{path.name} uses camera_backend {backend}; "
                                 f"probe_flir.py runs FLIR profiles "
                                 f"(camera_backend flir or flir_sim)")
            return
        if backend == "flir_sim" and not self.fake:
            sec["load_error"] = (f"{path.name} is a simulated profile "
                                 f"(camera_backend flir_sim); run it with "
                                 f"--fake")
            return
        self.profile = prof
        self.profile_path = path
        sec.update({"name": prof.name, "load_error": None,
                    "camera_backend": backend,
                    "trigger_source": prof.trigger_source,
                    "parsed": dataclasses.asdict(prof)})
        try:
            sec["gui_last_profile"] = app_settings().value(KEY_PROFILE)
        except Exception:
            sec["gui_last_profile"] = None

    def _default_profile_path(self):
        from gui_app.session_config import ProfileError, RigProfile
        from gui_app.settings import KEY_PROFILE, app_settings
        flir = []
        for p in RigProfile.list_profiles():
            try:
                prof = RigProfile.load(p)
            except ProfileError:
                continue
            if prof.camera_backend == "flir":
                flir.append((p, prof))
        try:
            last = app_settings().value(KEY_PROFILE)
        except Exception:
            last = None
        for p, prof in flir:
            if last and prof.name == last:
                return p
        return flir[0][0] if len(flir) == 1 else None

    def load_backend(self) -> bool:
        """Build the FLIR backend (or its simulation). False, with the
        reason in the report, when the SDK cannot load."""
        sdk = self.report["sdk"]
        sdk["pyqt5"] = _pyqt5_version()
        try:
            if self.fake:
                from gui_app.backends import fake_spinc, sim_board
                from gui_app.backends.flir import FlirBackend
                sim_board.reset_board()
                # The simulated rig takes the profile's shape; without a
                # profile that loads, the flir_sim template's.
                p = self.profile
                if p is None:
                    from gui_app.session_config import RigProfile
                    p = RigProfile.load(FAKE_PROFILE)
                serials = [str(s) for s in (p.camera_serials or [])]
                n = len(serials) or p.n_cameras or 3
                api = fake_spinc.FakeSpinC(
                    n_cameras=n, width=p.frame_width, height=p.frame_height,
                    serials=serials or None, interface=self.args.fake_interface)
                self.backend = FlirBackend(api=api)
                self.backend.name = "flir_sim"
            else:
                from gui_app.backends import load_backend
                spec = getattr(self.profile, "camera", None)
                self.backend = load_backend("flir", camera_spec=spec)
            self.api = self.backend.api
        except ImportError as e:
            sdk["load_error"] = str(e)
            sdk["searched"] = [str(s) for s in getattr(e, "searched", ())]
            return False
        except Exception as e:
            sdk["load_error"] = _err(e)
            return False
        sdk["backend"] = self.backend.name
        sdk["dll_path"] = str(getattr(self.api, "dll_path", ""))
        sdk["missing_optional"] = list(getattr(self.api, "missing_optional",
                                               ()) or ())
        try:
            sdk["spinnaker_c_version"] = self.api.library_version()
        except Exception as e:
            sdk["spinnaker_c_version"] = None
            sdk["version_error"] = _err(e)
        sdk["load_error"] = None
        return True

    def host_facts(self):
        host = self.report["host"]
        sdk = self.report["sdk"]
        if sdk.get("backend") and sdk.get("load_error") is None:
            line = (f"{sdk['backend']}: Spinnaker C "
                    f"{sdk.get('spinnaker_c_version')} ({sdk.get('dll_path')})")
        elif sdk.get("load_error"):
            line = f"flir: cannot load ({sdk['load_error']})"
        else:
            line = "flir: not loaded (this run opens no camera)"
        facts = []
        try:
            from gui_app import hardware_check
            facts = hardware_check.environment_facts(line)
        except Exception as e:
            host["facts_error"] = _err(e)
        host["facts"] = {k: v for k, v in facts}
        host["os"] = platform.platform()
        host["python"] = sys.version.split()[0]
        host["executable"] = sys.executable
        try:
            import numpy
            host["numpy"] = numpy.__version__
        except Exception:
            host["numpy"] = None
        host["cpu"] = host["facts"].get("cpu") or platform.processor()
        host["cores"] = os.cpu_count()
        try:
            import psutil
            host["cores_physical"] = psutil.cpu_count(logical=False)
            host["ram_gb"] = round(psutil.virtual_memory().total / 2 ** 30, 1)
        except Exception:
            host["ram_gb"] = None
        host["gpu"] = host["facts"].get("gpu")
        # The GUI's launch check probes the NVENC session cap and the libx264
        # rate; the probe opens no encoder.
        host["nvenc_sessions"] = None
        host["x264_fps_per_core"] = None
        host["encoder_note"] = ("the GUI's launch check measures the NVENC "
                                "session cap and the libx264 rate; its session "
                                "header in the log records both")
        try:
            logging_setup.print_header("probe_flir", facts,
                                       profile=self.profile)
        except Exception as e:
            print(f"[probe] session header failed: {_err(e)}", flush=True)

    # ------------------------------------------------------------- cameras
    def devices(self, fresh: bool = False) -> list:
        """The enumerated cameras, re-enumerated when asked or when a closed
        camera released an earlier handle."""
        if (fresh or self._devices is None
                or any(getattr(d, "released", False) for d in self._devices)):
            self._devices = list(self.backend.enumerate_devices())
        return self._devices

    def selected(self, fresh: bool = False) -> list:
        """The cameras the profile names (all of them without a list), in
        the profile's order."""
        devs = self.devices(fresh)
        serials = [str(s) for s in (getattr(self.profile, "camera_serials",
                                            None) or [])]
        if not serials:
            return devs
        by = {d.serial: d for d in devs}
        return [by[s] for s in serials if s in by]

    def device(self, serial: str):
        """The current enumeration's entry for `serial`. A camera the backend
        closed released its handle, and re-enumerating releases every other
        unused one, so a handle from an earlier enumeration is never reused."""
        for d in self.devices():
            if d.serial == serial:
                return d
        for d in self.devices(fresh=True):
            if d.serial == serial:
                return d
        raise RuntimeError(f"camera {serial} is no longer enumerated")

    def release_devices(self):
        """Release every camera handle of the last enumeration that no open
        camera holds, so another API (PySpin) can take the cameras."""
        for d in self._devices or []:
            if getattr(d, "released", False):
                continue
            with contextlib.suppress(Exception):
                if self.api.is_initialized(d.handle):
                    self.api.deinit(d.handle)
            try:
                self.api.release(d.handle)
                d.released = True
            except Exception as e:
                print(f"[probe] releasing {d.serial}'s handle failed: "
                      f"{_err(e)}", flush=True)
        self._devices = None

    def source(self):
        if self.profile.trigger_source == "external":
            return _ExternalSource(self.profile, self.fake)
        return _BoardSource(self.profile, self.fake)

    def spec_for(self, serial: str):
        spec = getattr(self.profile, "camera", None)
        return None if spec is None else spec.for_camera(serial)

    # ------------------------------------------------------------- driver
    def stage_ran(self, stage: str, t0: float, status_note: str = ""):
        checks = [c for c in self.report["checks"] if c["stage"] == stage]
        statuses = {c["status"] for c in checks}
        status = ("FAIL" if "FAIL" in statuses else
                  "WARN" if "WARN" in statuses else
                  "PASS" if "PASS" in statuses else
                  "SKIP" if "SKIP" in statuses else "INFO")
        self.report["stages"][stage] = {
            "status": status, "seconds": round(time.perf_counter() - t0, 2),
            "finished": _now_iso(), "note": status_note}
        self.save()

    def run(self) -> int:
        a = self.args
        self.clock = _CycleClock()
        self.load_profile()
        prof_err = self.report["profile"].get("load_error")
        if prof_err:
            self.check("setup", "profile loads", "FAIL", prof_err)
        elif self.profile is None:
            self.check("setup", "profile", "INFO",
                       "no FLIR profile given or found; --list and --selftest "
                       "run without one")
        else:
            self.check("setup", "profile loads", "PASS",
                       f"{self.profile.name} ({self.profile_path})")
            logging_setup.set_level(
                "debug" if self.profile.log_level == "debug" else "verbose")
        need_cameras = any((a.list, a.find_line, a.selftest,
                            a.triggered is not None, a.exposure_sweep,
                            a.wrap_test))
        sdk_ok = False
        if need_cameras:
            sdk_ok = self.load_backend()
            if sdk_ok:
                self.check("setup", "Spinnaker C library loads", "PASS",
                           f"{self.report['sdk'].get('spinnaker_c_version')} "
                           f"({self.report['sdk'].get('dll_path')})")
            else:
                self.check("setup", "Spinnaker C library loads", "FAIL",
                           self.report["sdk"].get("load_error") or "")
        self.host_facts()
        self.save()
        stages = [("list", a.list, stage_list),
                  ("find_line", a.find_line, stage_find_line),
                  ("selftest", a.selftest, stage_selftest),
                  ("triggered", a.triggered is not None, stage_triggered),
                  ("exposure_sweep", a.exposure_sweep, stage_exposure_sweep),
                  ("wrap_test", a.wrap_test, stage_wrap_test)]
        interrupted = False
        for name, wanted, fn in stages:
            if not wanted:
                continue
            t0 = time.perf_counter()
            print(f"[probe] ===== stage {name} =====", flush=True)
            if not sdk_ok:
                self.check(name, "stage runs", "SKIP",
                           "the camera library did not load")
                self.stage_ran(name, t0)
                continue
            try:
                fn(self)
            except KeyboardInterrupt:
                self.check(name, "stage runs", "FAIL", "interrupted")
                self.stage_ran(name, t0)
                interrupted = True
                break
            except Exception as e:
                self.error(name, e)
                self.check(name, "stage runs", "FAIL", _err(e))
            self.stage_ran(name, t0)
        if a.pyspin and not interrupted:
            t0 = time.perf_counter()
            print("[probe] ===== stage pyspin =====", flush=True)
            try:
                stage_pyspin(self)
            except Exception as e:
                self.error("pyspin", e)
                self.check("pyspin", "stage runs", "FAIL", _err(e))
            self.stage_ran("pyspin", t0)
        self.finish_unknowns()
        if a.collect and not interrupted:
            t0 = time.perf_counter()
            print("[probe] ===== stage collect =====", flush=True)
            try:
                stage_collect(self)
            except Exception as e:
                self.error("collect", e)
                self.check("collect", "stage runs", "FAIL", _err(e))
            self.stage_ran("collect", t0)
        problems = validate_report(_jsonable(self.report))
        self.check("report", f"JSON matches {SCHEMA}",
                   "FAIL" if problems else "PASS",
                   "; ".join(problems[:5]))
        self.save()
        print_table(self.report, self.json_path)
        if a.collect and self.report.get("collect") \
                and self.report["collect"].get("zip"):
            _refresh_zip_json(self)
        if interrupted:
            return 130
        return 1 if any(c["status"] == "FAIL"
                        for c in self.report["checks"]) else 0

    def finish_unknowns(self):
        ran = set(self.report["stages"])
        for uid, entry in self.report["unknowns"].items():
            entry["settled"] = bool(entry["answers"]) and all(
                a.get("answer") is not None for a in entry["answers"].values())
            if not entry["answers"] and entry["not_measured"] is None:
                stage = entry["stage"]
                names = [s.strip().replace("-", "_") for s in stage.split(",")]
                if not any(n in ran for n in names):
                    which = " and ".join(stage.split(", "))
                    entry["not_measured"] = (
                        f"the {which} stage{'s' if ',' in stage else ''} did "
                        f"not run in this probe run")
                else:
                    entry["not_measured"] = "not measured on these cameras"


def _pyqt5_version():
    try:
        from PyQt5.QtCore import PYQT_VERSION_STR
        return PYQT_VERSION_STR
    except Exception as e:
        return f"unavailable ({_err(e)})"


# ================================================================ --list
def _describe_camera(p: Probe, dev) -> dict:
    """What one initialised camera reports about itself, read only."""
    raw = _Raw(p.api, dev.handle, dev.label)
    d = {"serial": dev.serial, "model": dev.model, "vendor": dev.vendor,
         "interface": dev.interface, "ip": getattr(dev, "ip", None)}
    d["firmware"] = raw.read("DeviceFirmwareVersion")
    d["device_version"] = raw.read("DeviceVersion")
    d["link_speed"] = raw.read("DeviceLinkSpeed")
    d["current_speed"] = raw.read("DeviceCurrentSpeed", "tldevice")
    d["device_max_throughput"] = raw.read("DeviceMaxThroughput")
    lim = raw.rng("DeviceLinkThroughputLimit") or {}
    lim["mode"] = raw.read("DeviceLinkThroughputLimitMode")
    lim["mode_entries"] = raw.entries("DeviceLinkThroughputLimitMode")
    d["link_throughput_limit"] = lim
    d["payload_size"] = raw.read("PayloadSize")
    d["sensor"] = {"width_max": raw.read("WidthMax"),
                   "height_max": raw.read("HeightMax"),
                   "sensor_width": raw.read("SensorWidth"),
                   "sensor_height": raw.read("SensorHeight")}
    d["roi"] = {"width": raw.rng("Width"), "height": raw.rng("Height"),
                "offset_x": raw.rng("OffsetX"), "offset_y": raw.rng("OffsetY")}
    d["pixel_formats"] = raw.entries("PixelFormat")
    d["pixel_format"] = raw.read("PixelFormat")
    d["adc_bit_depths"] = raw.entries("AdcBitDepth")
    d["exposure_us"] = raw.rng("ExposureTime")
    d["exposure_auto"] = raw.read("ExposureAuto")
    d["gain_db"] = raw.rng("Gain")
    d["frame_rate"] = {"range": raw.rng("AcquisitionFrameRate"),
                       "enable": raw.read("AcquisitionFrameRateEnable"),
                       "resulting": raw.read("AcquisitionResultingFrameRate")}
    trig = {"selectors": raw.entries("TriggerSelector")}
    ctx = (raw.under("TriggerSelector", "FrameStart")
           if "FrameStart" in trig["selectors"] else contextlib.nullcontext())
    with ctx:
        trig["mode"] = raw.read("TriggerMode")
        trig["sources"] = raw.entries("TriggerSource")
        trig["source"] = raw.read("TriggerSource")
        trig["activations"] = raw.entries("TriggerActivation")
        trig["overlaps"] = raw.entries("TriggerOverlap")
        trig["overlap"] = raw.read("TriggerOverlap")
        trig["delay_us"] = raw.rng("TriggerDelay")
    d["trigger"] = trig
    lines = []
    for name in raw.entries("LineSelector"):
        with raw.under("LineSelector", name):
            lines.append({"name": name, "mode": raw.read("LineMode"),
                          "format": raw.read("LineFormat"),
                          "inverter": raw.read("LineInverter"),
                          "status": raw.read("LineStatus")})
    d["lines"] = lines
    d["line_status_all"] = raw.read("LineStatusAll")
    d["chunk_selectors"] = raw.entries("ChunkSelector")
    d["chunk_counter_selectors"] = (raw.entries("ChunkCounterSelector")
                                    if raw.has("ChunkCounterSelector") else None)
    d["chunk_mode_active"] = raw.read("ChunkModeActive")
    counters = {"selectors": raw.entries("CounterSelector"),
                "event_sources": [], "activations": [], "per_counter": {}}
    for sel in counters["selectors"]:
        with raw.under("CounterSelector", sel):
            if not counters["event_sources"]:
                counters["event_sources"] = raw.entries("CounterEventSource")
                counters["activations"] = raw.entries("CounterEventActivation")
            counters["per_counter"][sel] = {
                "event_source": raw.read("CounterEventSource"),
                "value": raw.rng("CounterValue")}
    maxes = [v["value"]["max"] for v in counters["per_counter"].values()
             if v.get("value") and v["value"].get("max") is not None]
    counters["value_max"] = max(maxes) if maxes else None
    d["counters"] = counters
    d["temperature"] = _describe_temperature(raw)
    d["timestamp"] = {"increment_ns": raw.read("TimestampIncrement"),
                      "tick_frequency": raw.read("GevTimestampTickFrequency")}
    if dev.interface == "GigE":
        d["gige"] = {"ip": getattr(dev, "ip", None),
                     "current_ip": raw.read("GevCurrentIPAddress"),
                     "mask": raw.read("GevCurrentSubnetMask"),
                     "packet_size": raw.rng("GevSCPSPacketSize"),
                     "packet_delay": raw.rng("GevSCPD"),
                     "stream_modes": raw.entries("StreamMode", "tlstream"),
                     "stream_mode": raw.read("StreamMode", "tlstream"),
                     "extended_id_modes": raw.entries("GevGVSPExtendedIDMode"),
                     "extended_id_mode": raw.read("GevGVSPExtendedIDMode")}
    else:
        d["gige"] = None
    d["stream"] = {"buffer_count_max": raw.read("StreamBufferCountMax",
                                                 "tlstream"),
                   "buffer_count_manual": raw.rng("StreamBufferCountManual",
                                                  "tlstream"),
                   "buffer_count_mode": raw.read("StreamBufferCountMode",
                                                 "tlstream"),
                   "handling_modes": raw.entries("StreamBufferHandlingMode",
                                                 "tlstream"),
                   "handling_mode": raw.read("StreamBufferHandlingMode",
                                             "tlstream")}
    d["user_sets"] = {"entries": raw.entries("UserSetSelector"),
                      "default": raw.read("UserSetDefault")}
    d["errors"] = raw.errors
    return d


def _describe_temperature(raw: _Raw) -> dict:
    out = {"nodes_present": [n for n in TEMPERATURE_NODES if raw.has(n)],
           "selectors": raw.entries("DeviceTemperatureSelector"),
           "values_c": {}, "status": {}, "transition_selectors":
           raw.entries("DeviceTemperatureStatusTransitionSelector"),
           "thresholds": {}}

    def read_here(entry):
        out["values_c"][entry] = raw.read("DeviceTemperature")
        out["status"][entry] = raw.read("DeviceTemperatureStatus")
        th = {}
        for tr in out["transition_selectors"]:
            with raw.under("DeviceTemperatureStatusTransitionSelector", tr):
                th[tr] = raw.read("DeviceTemperatureStatusTransition")
        out["thresholds"][entry] = th

    if out["selectors"]:
        for entry in out["selectors"]:
            with raw.under("DeviceTemperatureSelector", entry):
                read_here(entry)
    elif raw.has("DeviceTemperature"):
        read_here("")
    return out


def _temperature_answers(p: Probe, serial: str, t: dict):
    present = t["nodes_present"]
    p.answer("temperature_nodes", serial,
             ", ".join(present) if present else "no temperature node",
             nodes_present=present, status=t["status"])
    th = {e: v for e, v in t["thresholds"].items() if any(
        x is not None for x in v.values())}
    if not th:
        ans = ("no threshold nodes (DeviceTemperatureStatusTransition), so "
               "the entry question does not arise")
    elif len(t["thresholds"]) <= 1:
        ans = f"the only entry ({next(iter(t['thresholds'])) or 'unnamed'})"
    elif len({json.dumps(v, sort_keys=True) for v in th.values()}) > 1:
        ans = ("each entry has its own thresholds: "
               + "; ".join(f"{e} {v}" for e, v in th.items()))
    else:
        ans = ("the thresholds read the same under every entry ("
               + ", ".join(th) + "), so the camera does not show which entry "
               "they belong to")
    p.answer("temperature_threshold_entry", serial, ans,
             thresholds=t["thresholds"], values_c=t["values_c"])


def _validate_spec(p: Probe, d: dict) -> list:
    """The profile's camera settings against what this camera reports,
    without writing anything. Each row: field, asked, what the camera
    offers, ok, message."""
    prof = p.profile
    spec = p.spec_for(d["serial"])
    if prof is None or spec is None:
        return []
    rows = []

    def row(field, asked, offers, ok, message=""):
        rows.append({"field": field, "asked": asked, "camera": offers,
                     "ok": bool(ok), "message": message})

    def in_range(field, asked, rg, unit=""):
        if not rg or rg.get("min") is None:
            row(field, asked, None, False, "the camera has no such node")
            return
        ok = rg["min"] <= asked <= rg["max"]
        row(field, asked, [rg["min"], rg["max"]], ok,
            "" if ok else f"outside {rg['min']:g} to {rg['max']:g}{unit}")

    in_range("camera.exposure_us", spec.exposure_us, d["exposure_us"], " us")
    if d["gain_db"]:
        in_range("camera.gain_db", spec.gain_db, d["gain_db"], " dB")
    elif spec.gain_db:
        row("camera.gain_db", spec.gain_db, None, False,
            "the camera has no Gain node; set camera.gain_db: 0")
    trig = d["trigger"]
    line = spec.trigger.line
    row("camera.trigger.line", line, trig["sources"], line in trig["sources"],
        "" if line in trig["sources"] else
        f"not a TriggerSource entry; run --find-line")
    for ln in d["lines"]:
        if ln["name"] == line and ln["mode"] not in (None, "Input"):
            row("camera.trigger.line", line, ln["mode"], False,
                f"{line} is set as an {ln['mode']}")
    if trig["activations"]:
        a = spec.trigger.activation
        row("camera.trigger.activation", a, trig["activations"],
            a in trig["activations"])
    if trig["overlaps"]:
        o = spec.trigger.overlap
        row("camera.trigger.overlap", o, trig["overlaps"],
            o in trig["overlaps"])
    if spec.trigger.delay_us is not None:
        in_range("camera.trigger.delay_us", spec.trigger.delay_us,
                 trig["delay_us"], " us")
    row("camera.pixel_format", spec.pixel_format, d["pixel_formats"],
        spec.pixel_format in d["pixel_formats"])
    for field, key, want in (("frame_width", "width", prof.frame_width),
                             ("frame_height", "height", prof.frame_height)):
        rg = d["roi"][key] or {}
        top = d["sensor"][f"{key}_max"] or rg.get("max")
        inc = rg.get("inc") or 1
        lo = rg.get("min") or 0
        ok = top is not None and lo <= want <= top and (want - lo) % inc == 0
        row(field, want, {"min": lo, "max": top, "inc": inc}, ok,
            "" if ok else f"needs {lo} to {top} in steps of {inc}")
    top = d["stream"]["buffer_count_max"]
    if top is not None:
        row("max_num_buffer", prof.max_num_buffer, top,
            prof.max_num_buffer <= top,
            "" if prof.max_num_buffer <= top else
            f"above StreamBufferCountMax {top}")
    lim = d["link_throughput_limit"]
    if lim and lim.get("max"):
        need = prof.frame_width * prof.frame_height * prof.frame_rate
        row("frame_width x frame_height x frame_rate", need, lim["max"],
            need <= lim["max"],
            "" if need <= lim["max"] else
            f"needs {need / 1e6:.1f} MB/s, the link carries at most "
            f"{lim['max'] / 1e6:.1f} MB/s")
    if spec.flir.block_id_source == "trigger_counter":
        ok = "CounterValue" in d["chunk_selectors"]
        row("camera.flir.block_id_source", "trigger_counter",
            d["chunk_selectors"], ok,
            "" if ok else "ChunkSelector offers no CounterValue")
    if d["interface"] != "GigE":
        for key in ("packet_size", "packet_delay"):
            if getattr(spec.flir, key) is not None:
                row(f"camera.flir.{key}", getattr(spec.flir, key),
                    d["interface"], False, "applies to GigE cameras only")
    return rows


def _init(p: Probe, dev):
    """Initialise a camera for raw access; None or the error text."""
    try:
        p.api.init(dev.handle)
        return None
    except Exception as e:
        return _err(e)


def _deinit(p: Probe, dev):
    try:
        if p.api.is_streaming(dev.handle):
            p.api.end(dev.handle)
    except Exception:
        pass
    try:
        if p.api.is_initialized(dev.handle):
            p.api.deinit(dev.handle)
    except Exception as e:
        print(f"[probe] {dev.serial}: deinit failed: {_err(e)}", flush=True)


def stage_list(p: Probe):
    devs = p.devices(fresh=True)
    if not devs:
        p.check("list", "cameras enumerated", "FAIL",
                "no FLIR camera found. Check power and cabling, and that "
                "SpinView lists the cameras.")
        return
    p.check("list", "cameras enumerated", "PASS", f"{len(devs)} camera(s)")
    p.report["cameras"] = []
    for dev in devs:
        err = _init(p, dev)
        if err is not None:
            p.report["cameras"].append({"serial": dev.serial,
                                        "model": dev.model,
                                        "interface": dev.interface,
                                        "error": err})
            p.check("list", f"{dev.serial} initialises", "FAIL",
                    f"{err}. Close SpinView or any other program that holds "
                    f"the camera.")
            continue
        try:
            d = _describe_camera(p, dev)
        finally:
            _deinit(p, dev)
        d["spec_validation"] = _validate_spec(p, d)
        p.report["cameras"].append(d)
        _list_answers(p, d)
        bad = [r for r in d["spec_validation"] if not r["ok"]]
        if p.profile is not None and d["spec_validation"]:
            p.check("list", f"{dev.serial} fits the profile's camera settings",
                    "FAIL" if bad else "PASS",
                    "; ".join(f"{r['field']} {r['asked']}: {r['message']}"
                              for r in bad))
    if p.profile is not None and p.profile.camera_serials:
        found = {d.serial for d in devs}
        for s in p.profile.camera_serials:
            p.check("list", f"profile serial {s} is connected",
                    "PASS" if str(s) in found else "FAIL",
                    "" if str(s) in found else
                    "not enumerated; check its cable and power")
    serials = sorted(d.serial for d in devs)
    print("[probe] cameras found:", flush=True)
    for d in p.report["cameras"]:
        print(f"[probe]   {d['serial']:<12} {d.get('model') or '?':<28} "
              f"{d.get('interface') or '?'}", flush=True)
    print(f"[probe] for the profile:  n_cameras: {len(serials)}", flush=True)
    print("[probe]                   camera_serials: ["
          + ", ".join(f'"{s}"' for s in serials) + "]", flush=True)


def _list_answers(p: Probe, d: dict):
    s = d["serial"]
    per = d["counters"]["per_counter"]
    if per:
        width = {}
        for sel, v in per.items():
            top = (v.get("value") or {}).get("max")
            width[sel] = (None if top is None else
                          {"max": top, "period": top + 1 if 0 < top < 2 ** 31
                           else None})
        parts = []
        for sel, w in width.items():
            if w is None:
                parts.append(f"{sel} max unreadable")
            elif w["period"]:
                parts.append(f"{sel} max {w['max']} (wraps at {w['period']})")
            else:
                parts.append(f"{sel} max {w['max']} (no wrap below 2**31)")
        p.answer("counter_width", s, ", ".join(parts), counters=width)
    else:
        p.answer("counter_width", s, "the camera has no counters")
    chunks = d["chunk_selectors"]
    ccs = d["chunk_counter_selectors"]
    ans = ("CounterValue chunk offered" if "CounterValue" in chunks
           else "no CounterValue chunk")
    ans += (f"; ChunkCounterSelector offers {', '.join(ccs)}" if ccs
            else "; no ChunkCounterSelector")
    p.answer("counter_chunk_supported", s, ans,
             chunk_selectors=chunks,
             chunk_counter_selectors=d["chunk_counter_selectors"])
    _temperature_answers(p, s, d["temperature"])
    p.answer("stream_buffer_count_max", s, d["stream"]["buffer_count_max"])
    if d["gige"] is not None:
        g = d["gige"]
        p.answer("gige_transport", s,
                 f"StreamMode {g['stream_mode']} of {g['stream_modes']}, "
                 f"packet size {g['packet_size']}, extended IDs "
                 f"{g['extended_id_mode']} of {g['extended_id_modes']}",
                 **g)
    else:
        p.answer("gige_transport", s, f"not GigE ({d['interface']})")


# =========================================================== --find-line
def _read_lines(raw: _Raw, names: list):
    """{line: high} now. LineStatusAll when the camera has it (one read),
    else LineStatus under each LineSelector entry."""
    bits = raw.read("LineStatusAll")
    if bits is not None:
        out = {}
        for name in names:
            b = _line_bit(name)
            if b is not None:
                out[name] = bool((int(bits) >> b) & 1)
        return out, "LineStatusAll"
    out = {}
    for name in names:
        with raw.under("LineSelector", name):
            v = raw.read("LineStatus")
        if v is not None:
            out[name] = bool(v)
    return out, "LineStatus"


def _watch_lines(raw: _Raw, names: list, seconds: float) -> dict:
    """Transitions per line over `seconds` of reads."""
    toggles = dict.fromkeys(names, 0)
    prev = None
    samples = 0
    how = None
    t_end = time.perf_counter() + seconds
    while time.perf_counter() < t_end:
        now, how = _read_lines(raw, names)
        samples += 1
        if prev is not None:
            for k, v in now.items():
                if k in prev and prev[k] != v:
                    toggles[k] += 1
        prev = now
        time.sleep(FIND_LINE_SAMPLE_S)
    return {"toggles": toggles, "samples": samples, "read_by": how}


def stage_find_line(p: Probe):
    prof = p.profile
    if prof is None or prof.camera is None:
        p.check("find_line", "stage runs", "FAIL",
                "needs a FLIR profile, for the trigger board or source and the "
                "trigger line")
        return
    devs = p.selected(fresh=True)
    if not devs:
        p.check("find_line", "cameras", "FAIL", "no camera of the profile "
                "is connected")
        return
    src = p.source()
    rep = {"source": src.kind, "hz": FIND_LINE_HZ if src.host_started
           else prof.frame_rate, "per_camera": {}}
    p.report["find_line"] = rep
    raws = {}
    for dev in devs:
        err = _init(p, dev)
        if err is not None:
            p.check("find_line", f"{dev.serial} initialises", "FAIL", err)
            continue
        raws[dev.serial] = (_Raw(p.api, dev.handle, dev.label), dev)
    try:
        why = src.open()
        if why is not None:
            p.check("find_line", f"{src.describe()} is usable", "FAIL", why)
            return
        if src.kind == "board":
            p.check("find_line", "trigger board runs the recording-only "
                    "sketch", "PASS", f"sketch {src.heard_id}")
        # Lines that can be inputs, per camera.
        names = {}
        for serial, (raw, _dev) in raws.items():
            names[serial] = [n for n in raw.entries("LineSelector")] or \
                [n for n in raw.entries("TriggerSource") if n.startswith("Line")]
        rate = FIND_LINE_HZ if src.host_started else prof.frame_rate
        if not src.start(rate, "find-line"):
            p.check("find_line", f"{src.describe()} starts at {rate:g} Hz",
                    "FAIL", "the board did not confirm the start (no RDY ack)")
            return
        if not src.host_started:
            from gui_app.trigger_source import FIRST_TRIGGER_TIMEOUT_S
            deadline = time.perf_counter() + FIRST_TRIGGER_TIMEOUT_S
            seen = False
            while time.perf_counter() < deadline and not seen:
                for serial, (raw, _d) in raws.items():
                    w = _watch_lines(raw, names[serial], 0.25)
                    if any(v >= 2 for v in w["toggles"].values()):
                        seen = True
                        break
            if not seen:
                p.check("find_line", "a trigger reaches a camera", "FAIL",
                        f"no input line changed within "
                        f"{FIRST_TRIGGER_TIMEOUT_S:.0f} s of the prompt")
                return
        else:
            time.sleep(0.2)
        for serial, (raw, _d) in raws.items():
            rep["per_camera"][serial] = _watch_lines(raw, names[serial],
                                                     FIND_LINE_S)
    finally:
        if src.host_started and src.teensy is not None:
            src.stop()
        elif not src.host_started:
            src.stop("find-line")
        closed = src.close()
        if src.kind == "board" and closed is not None:
            p.check("find_line", "trigger board stood down",
                    "PASS" if closed else "FAIL",
                    "" if closed else "the stop was not confirmed; "
                    "power-cycle the board")
        for _raw, dev in raws.values():
            _deinit(p, dev)
    lines_for = {}
    for serial, w in rep["per_camera"].items():
        wired = [n for n, k in w["toggles"].items()
                 if k >= (FIND_LINE_MIN_TOGGLES if src.host_started else 2)]
        w["wired"] = wired
        want = p.spec_for(serial).trigger.line
        w["profile_line"] = want
        if not wired:
            p.check("find_line", f"{serial} trigger wire found", "FAIL",
                    "no input line toggled. Check the wiring, the ground "
                    "reference and the input's voltage rating.")
            continue
        if len(wired) > 1:
            p.check("find_line", f"{serial} trigger wire found", "WARN",
                    f"several lines toggled: {', '.join(wired)}")
        else:
            p.check("find_line", f"{serial} trigger wire found", "PASS",
                    wired[0])
        lines_for[serial] = wired[0]
        p.check("find_line", f"{serial} profile trigger line matches the wire",
                "PASS" if want in wired else "FAIL",
                "" if want in wired else
                f"the profile says {want}, the wire is on {', '.join(wired)}")
    if lines_for:
        distinct = sorted(set(lines_for.values()))
        if len(distinct) == 1:
            print(f"[probe] write in the profile:  camera: trigger: line: "
                  f"{distinct[0]}", flush=True)
        else:
            print("[probe] the cameras are wired to different lines; write "
                  "one per camera:", flush=True)
            print("[probe]   camera:\n[probe]     per_camera:", flush=True)
            for serial, line in lines_for.items():
                print(f'[probe]       "{serial}": {{trigger_line: {line}}}',
                      flush=True)


# ============================================================ --selftest
def _grab(p: Probe, h, timeout_ms: int):
    """One image's facts, released before returning; None on a timeout."""
    from gui_app.backends.flir import FlirTimeout
    api = p.api
    try:
        img = api.next_image(h, timeout_ms)
    except FlirTimeout:
        return None
    host = time.perf_counter()
    try:
        return {"id": int(api.image_frame_id(img)),
                "ts": int(api.image_timestamp(img)), "host": host,
                "incomplete": bool(api.image_incomplete(img)),
                "ptr": int(api.image_data_ptr(img)),
                "padding": list(api.image_padding(img)),
                "bpp": api.image_bpp(img), "stride": api.image_stride(img),
                "dims": list(api.image_dims(img))}
    finally:
        api.image_release(img)


def _baseline(raw: _Raw, user_set: str) -> dict:
    """The user set, then the free-run baseline every measurement starts
    from. Returns the user-set probe's findings."""
    out = {"user_set": user_set, "before": {}, "after": {}, "error": None}
    for n in USER_SET_NODES:
        out["before"][n] = raw.read(n)
    if user_set != "none":
        if raw.has("UserSetSelector") and raw.has("UserSetLoad"):
            e1 = raw.write("UserSetSelector", user_set)
            e2 = raw.execute("UserSetLoad") if e1 is None else None
            out["error"] = e1 or e2
        else:
            out["error"] = "no UserSetSelector / UserSetLoad node"
    for n in USER_SET_NODES:
        out["after"][n] = raw.read(n)
    out["changed"] = sorted(n for n in USER_SET_NODES
                            if out["before"][n] != out["after"][n])
    sels = raw.entries("TriggerSelector")
    for sel in [s for s in ("AcquisitionStart", "FrameBurstStart",
                            "FrameStart") if s in sels] or [None]:
        if sel is not None:
            raw.write("TriggerSelector", sel)
        raw.write("TriggerMode", "Off")
    if raw.has("AcquisitionMode"):
        raw.write("AcquisitionMode", "Continuous")
    for n in ("ExposureAuto", "GainAuto"):
        if raw.has(n):
            raw.write(n, "Off")
    if raw.has("ExposureMode"):
        raw.write("ExposureMode", "Timed")
    if "Mono8" in raw.entries("PixelFormat"):
        raw.write("PixelFormat", "Mono8")
    rg = raw.rng("ExposureTime")
    if rg and rg.get("min") is not None:
        raw.write("ExposureTime", float(max(rg["min"], min(1000.0, rg["max"]))))
    return out


def _set_rate(raw: _Raw, fps: float):
    """Free-run rate `fps` where the camera lets it be set; the rate written
    or None."""
    if raw.has("AcquisitionFrameRateEnable"):
        raw.write("AcquisitionFrameRateEnable", True)
    rg = raw.rng("AcquisitionFrameRate")
    if not rg or rg.get("max") is None or not raw.writable("AcquisitionFrameRate"):
        return None
    want = float(min(max(fps, rg["min"]), rg["max"]))
    return want if raw.write("AcquisitionFrameRate", want) is None else None


def _exposure_follow(raw: _Raw) -> dict:
    """ExposureTime's maximum at two frame rates and with the frame rate
    off."""
    out = {"measured": False}
    if not (raw.has("AcquisitionFrameRate")
            and raw.writable("AcquisitionFrameRateEnable")):
        out["why_not"] = "no writable AcquisitionFrameRate / Enable"
        return out
    r1 = _set_rate(raw, 30.0)
    m1 = (raw.rng("ExposureTime") or {}).get("max")
    r2 = _set_rate(raw, 15.0)
    m2 = (raw.rng("ExposureTime") or {}).get("max")
    raw.write("AcquisitionFrameRateEnable", False)
    m_off = (raw.rng("ExposureTime") or {}).get("max")
    out.update({"measured": True, "rate_1": r1, "max_1_us": m1, "rate_2": r2,
                "max_2_us": m2, "max_rate_off_us": m_off})
    if None in (r1, r2, m1, m2) or r1 == r2:
        out["follows"] = None
    else:
        out["follows"] = abs(m1 - m2) > 1.0
        out["readout_us_at_1"] = (None if not out["follows"]
                                  else round(1e6 / r1 - m1, 1))
    return out


def _trigger_delay_probe(raw: _Raw, line) -> dict:
    """TriggerDelay's range and value with TriggerMode Off and On."""
    out = {"present": raw.has("TriggerDelay")}
    if not out["present"]:
        return out
    sels = raw.entries("TriggerSelector")
    if "FrameStart" in sels:
        raw.write("TriggerSelector", "FrameStart")
    out["off"] = {"readable": raw.readable("TriggerDelay"),
                  "writable": raw.writable("TriggerDelay"),
                  "range": raw.rng("TriggerDelay")}
    sources = [s for s in raw.entries("TriggerSource") if s != "Software"]
    src = line if line in sources else (sources[0] if sources else None)
    if src is not None:
        raw.write("TriggerSource", src)
    err = raw.write("TriggerMode", "On")
    out["on"] = {"source": src, "mode_error": err,
                 "readable": raw.readable("TriggerDelay"),
                 "writable": raw.writable("TriggerDelay"),
                 "range": raw.rng("TriggerDelay")}
    raw.write("TriggerMode", "Off")
    return out


def _judge(cycles, rate, tick) -> tuple:
    """(frame_id verdict, timestamp verdict) from the self-test cycles."""
    firsts = [c[0]["id"] for c in cycles if c]
    steps = sorted({b["id"] - a["id"] for c in cycles for a, b in zip(c, c[1:])})
    counts = bool(steps) and steps[0] == 1
    if firsts and all(f == 1 for f in firsts):
        fid = {"restarts": True, "base": 1}
    elif firsts and all(f == 0 for f in firsts):
        fid = {"restarts": True, "base": 0}
    else:
        fid = {"restarts": False, "base": None}
    fid.update({"first_ids": firsts, "steps": steps, "counts_frames": counts})
    ts = {"zero_seen": any(f["ts"] == 0 for c in cycles for f in c),
          "rate_fps": rate, "tick": tick}
    intervals = [b["ts"] - a["ts"] for c in cycles for a, b in zip(c, c[1:])]
    ts["increasing"] = bool(intervals) and all(i > 0 for i in intervals)
    ratio = None
    if rate and intervals and ts["increasing"]:
        ratio = statistics.median(intervals) / (1e9 / rate)
    ts["unit_ratio"] = ratio
    scale = None
    if ratio is not None:
        if abs(ratio - 1) <= TS_UNIT_TOL:
            scale = 1
        else:
            for factor in [v for v in (tick.get("increment_ns"),
                                       (1e9 / tick["tick_frequency"])
                                       if tick.get("tick_frequency") else None)
                           if v]:
                if abs(ratio * factor - 1) <= TS_UNIT_TOL:
                    scale = factor
    ts["scale_to_ns"] = scale
    gaps = []
    for prev, nxt in zip(cycles, cycles[1:]):
        if not prev or not nxt:
            continue
        dev = (nxt[0]["ts"] - prev[-1]["ts"]) * (scale or 1) / 1e9
        host = nxt[0]["host"] - prev[-1]["host"]
        gaps.append({"device_s": round(dev, 6), "host_s": round(host, 6)})
    ts["restart_gaps"] = gaps
    ts["continuous"] = bool(gaps) and all(
        g["device_s"] > 0 and g["device_s"] >= TS_CONTINUITY_SHARE * g["host_s"]
        for g in gaps)
    return fid, ts


def _id_clock(p: Probe, raw: _Raw, h) -> dict:
    rate = _set_rate(raw, SELFTEST_FPS)
    reported = raw.read("AcquisitionResultingFrameRate")
    rate_used = reported or rate
    timeout = int(max(1000, 4000 / (rate_used or 1.0)))
    cycles = []
    for _k in range(SELFTEST_CYCLES):
        frames = []
        p.api.begin(h)
        try:
            for _ in range(SELFTEST_FRAMES):
                f = _grab(p, h, timeout)
                if f is None:
                    break
                frames.append(f)
        finally:
            p.api.end(h)
        cycles.append(frames)
    tick = {"increment_ns": raw.read("TimestampIncrement"),
            "tick_frequency": raw.read("GevTimestampTickFrequency")}
    fid, ts = _judge(cycles, rate_used, tick)
    first = next((c[0] for c in cycles if c), None)
    layout = None if first is None else {
        k: first[k] for k in ("padding", "bpp", "stride", "dims")}
    return {"rate_written": rate, "rate_reported": reported,
            "frames_per_cycle": [len(c) for c in cycles],
            "cycles": [[{k: f[k] for k in ("id", "ts", "host", "incomplete")}
                        for f in c] for c in cycles],
            "frame_id": fid, "timestamp": ts, "layout": layout}


def _zero_copy(p: Probe, raw: _Raw, h, has_counters: bool) -> dict:
    bufs = raw.read("StreamBufferCountManual", "tlstream")
    n = 2 * int(bufs or SELFTEST_BUFFERS)
    ptrs, hosts, stamps = set(), [], []
    counter = {"tried": False}
    p.api.begin(h)
    try:
        for k in range(n):
            f = _grab(p, h, 2000)
            if f is None:
                break
            ptrs.add(f["ptr"])
            hosts.append(f["host"])
            stamps.append(f["ts"])
            if k == 2 and has_counters:
                counter["tried"] = True
                sel = raw.read("CounterSelector")
                v = _counter_read(raw, "Counter0")
                counter["value"] = v
                counter["ok"] = v is not None
                counter["error"] = None if v is not None else (
                    raw.errors[-1] if raw.errors else "unreadable")
                if sel:
                    raw.write("CounterSelector", sel)
    finally:
        p.api.end(h)
    host_ratio = None
    if len(hosts) > 3 and hosts[-1] > hosts[1]:
        host_ratio = (stamps[-1] - stamps[1]) / ((hosts[-1] - hosts[1]) * 1e9)
    return {"frames": len(hosts), "buffers": bufs, "distinct_ptrs": len(ptrs),
            "counter_while_streaming": counter,
            "host_clock_ratio": host_ratio}


def _chunk_probe(p: Probe, raw: _Raw, h) -> dict:
    """Chunk spellings, and which counter the CounterValue chunk carries,
    in free run with Counter0 counting ExposureStart and Counter1 held
    still."""
    entries = raw.entries("ChunkSelector")
    out = {"entries": entries, "enabled": [], "spellings": {}, "frames": []}
    if not entries:
        out["why_not"] = "no ChunkSelector"
        return out
    keep = {"ChunkModeActive": raw.read("ChunkModeActive"),
            "CounterSelector": raw.read("CounterSelector")}
    src_before = {}
    ctr_ok = False
    try:
        raw.write("ChunkModeActive", True)
        for e in ("FrameID", "Timestamp", "CounterValue"):
            if e in entries:
                with raw.under("ChunkSelector", e):
                    if raw.write("ChunkEnable", True) is None:
                        out["enabled"].append(e)
        sels = raw.entries("CounterSelector")
        if "CounterValue" in out["enabled"] and {"Counter0", "Counter1"} <= set(sels):
            for sel in ("Counter0", "Counter1"):
                with raw.under("CounterSelector", sel):
                    src_before[sel] = raw.read("CounterEventSource")
            srcs = raw.entries("CounterEventSource")
            if "ExposureStart" in srcs and "Off" in srcs:
                raw.write("CounterSelector", "Counter1")
                raw.write("CounterEventSource", "Off")
                out["counter1_before"] = raw.read("CounterValue")
                raw.write("CounterSelector", "Counter0")
                raw.write("CounterEventSource", "ExposureStart")
                raw.execute("CounterReset")
                if raw.has("ChunkCounterSelector"):
                    out["chunk_counter_selector_set"] = raw.write(
                        "ChunkCounterSelector", "Counter0") is None
                # CounterSelector names Counter1 when acquisition starts, so
                # a chunk that follows it reads Counter1's still value.
                raw.write("CounterSelector", "Counter1")
                ctr_ok = True
        p.api.begin(h)
        try:
            for _k in range(SELFTEST_FRAMES):
                try:
                    img = p.api.next_image(h, 2000)
                except Exception as e:
                    out["grab_error"] = _err(e)
                    break
                try:
                    fr = {"id": int(p.api.image_frame_id(img))}
                    for key in out["enabled"]:
                        for spelling in (key, "Chunk" + key):
                            try:
                                v = int(p.api.chunk_int(img, spelling))
                            except Exception:
                                continue
                            fr[key] = v
                            out["spellings"].setdefault(key, [])
                            if spelling not in out["spellings"][key]:
                                out["spellings"][key].append(spelling)
                    out["frames"].append(fr)
                finally:
                    p.api.image_release(img)
        finally:
            p.api.end(h)
    finally:
        for e in out["enabled"]:
            with raw.under("ChunkSelector", e):
                raw.write("ChunkEnable", False)
        if keep["ChunkModeActive"] is not None:
            raw.write("ChunkModeActive", bool(keep["ChunkModeActive"]))
        for sel, src in src_before.items():
            if src is not None:
                with raw.under("CounterSelector", sel):
                    raw.write("CounterEventSource", src)
        if keep["CounterSelector"]:
            raw.write("CounterSelector", keep["CounterSelector"])
    frames = [f for f in out["frames"] if "CounterValue" in f]
    if ctr_ok and len(frames) >= 3:
        k = [f["id"] - frames[0]["id"] + 1 for f in frames]
        vals = [f["CounterValue"] for f in frames]
        diffs = {v - i for v, i in zip(vals, k)}
        still = len(set(vals)) == 1
        if still:
            out["carrier"] = ("the counter CounterSelector names when "
                              "acquisition starts (Counter1)")
        elif len(diffs) == 1 and diffs <= {0, -1}:
            out["carrier"] = ("Counter0, the counter ChunkCounterSelector names"
                              if raw.has("ChunkCounterSelector") else
                              "Counter0, whatever CounterSelector names")
            out["exposure_latch"] = ("counts the image's own exposure start"
                                     if diffs == {0} else
                                     "latched before the image's exposure "
                                     "start")
        else:
            out["carrier"] = None
            out["carrier_note"] = f"values {vals} against frames {k}"
    return out


def _gil_probe(p: Probe, raw: _Raw, h) -> dict:
    """The competitor-thread GIL meter: controls, then a thread waiting in
    next_image on a camera free-running at GIL_FPS."""
    out = {"fps": _set_rate(raw, GIL_FPS)}
    meter = _GilMeter()
    try:
        def release(stop):
            n = 0
            while not stop.is_set():
                time.sleep(0.05)
                n += 1
            return n

        hold_sleep = _gil_holding_sleep()

        def hold(stop):
            n = 0
            while not stop.is_set():
                hold_sleep(0.05)
                n += 1
            return n

        out["control_release"] = meter.held(release, GIL_CONTROL_S)
        out["control_hold"] = meter.held(hold, GIL_CONTROL_S)
        p.api.begin(h)
        try:
            api = p.api

            def wait(stop):
                from gui_app.backends.flir import FlirTimeout
                frames = 0
                while not stop.is_set():
                    try:
                        img = api.next_image(h, 2000)
                    except FlirTimeout:
                        continue
                    except Exception:
                        time.sleep(0.05)
                        continue
                    api.image_release(img)
                    frames += 1
                return frames

            out["wait"] = meter.held(wait, GIL_WINDOW_S)
        finally:
            p.api.end(h)
    finally:
        meter.close()
    rel = out["control_release"].get("held_fraction")
    hol = out["control_hold"].get("held_fraction")
    out["meter_ok"] = (rel is not None and hol is not None
                       and rel < 0.25 and hol > 0.75)
    out["capi_wait_gil_held_fraction"] = out["wait"].get("held_fraction")
    return out


def _backend_open(p: Probe, dev):
    """Open `dev` through the FLIR backend with the profile; (cam, error)."""
    prof = p.profile
    try:
        cam = p.backend.open(dev, "", prof.max_num_buffer, prof.camera,
                             frame_size=(prof.frame_width, prof.frame_height),
                             frame_rate=prof.frame_rate)
        return cam, None
    except Exception as e:
        return None, _err(e)


def stage_selftest(p: Probe):
    devs = p.selected(fresh=True)
    if not devs:
        p.check("selftest", "cameras", "FAIL", "no camera to test")
        return
    user_set = (p.profile.camera.flir.user_set
                if p.profile is not None and p.profile.camera is not None
                else "Default")
    first = True
    for s in [d.serial for d in devs]:
        dev = p.device(s)
        rec = {"serial": s, "model": dev.model}
        p.report["selftest"][s] = rec
        err = _init(p, dev)
        if err is not None:
            rec["error"] = err
            p.check("selftest", f"{s} initialises", "FAIL", err)
            continue
        raw = _Raw(p.api, dev.handle, dev.label)
        line = None
        spec = p.spec_for(s)
        if spec is not None:
            line = spec.trigger.line
        try:
            rec["user_set_load"] = _baseline(raw, user_set)
            rec["exposure_vs_frame_rate"] = _exposure_follow(raw)
            rec["trigger_delay"] = _trigger_delay_probe(raw, line)
            if raw.has("StreamBufferCountMode", "tlstream"):
                raw.write("StreamBufferCountMode", "Manual", "tlstream")
            raw.write("StreamBufferCountManual", SELFTEST_BUFFERS, "tlstream")
            rec.update(_id_clock(p, raw, dev.handle))
            has_counters = bool(raw.entries("CounterSelector"))
            rec["zero_copy"] = _zero_copy(p, raw, dev.handle, has_counters)
            rec["chunks"] = _chunk_probe(p, raw, dev.handle)
            if first:
                rec["gil"] = _gil_probe(p, raw, dev.handle)
                first = False
        except Exception as e:
            rec["error"] = _err(e)
            p.error("selftest", e)
            p.check("selftest", f"{s} free-run tests", "FAIL", _err(e))
        finally:
            rec["raw_errors"] = raw.errors
            _deinit(p, dev)
        _selftest_checks(p, s, rec)
        if p.profile is not None and p.profile.camera is not None:
            cam, err = _backend_open(p, p.device(s))
            rec["backend_open"] = {"ok": err is None, "error": err}
            if cam is not None:
                rec["backend_open"].update({
                    "selftest": cam.selftest, "block_id_source":
                    cam.block_id_source, "id_offset": cam.id_offset,
                    "id16": cam.id16, "ts_source": cam.ts_source,
                    "ts_scale": cam.ts_scale, "applied": cam.applied,
                    "counters": cam.counters,
                    "counter_chunk_ok": cam.counter_chunk_ok})
                p.backend.close(cam)
            p.check("selftest", f"{s} opens through the FLIR backend with the "
                    f"profile", "PASS" if err is None else "FAIL",
                    "" if err is None else err)


def _selftest_checks(p: Probe, s: str, rec: dict):
    fpc = rec.get("frames_per_cycle") or []
    got = sum(fpc)
    p.check("selftest", f"{s} free-run frames arrive",
            "PASS" if fpc and all(n == SELFTEST_FRAMES for n in fpc) else "FAIL",
            f"{got} of {SELFTEST_CYCLES * SELFTEST_FRAMES}")
    fid = rec.get("frame_id")
    if fid:
        if fid["restarts"] and fid["counts_frames"]:
            ans = f"restarts, {fid['base']}-based, steps {fid['steps']}"
            p.check("selftest", f"{s} frame ID restarts at every acquisition",
                    "PASS", ans)
        else:
            ans = ("does not restart" if not fid["restarts"] else
                   f"restarts but steps {fid['steps']}")
            ans += f" (first IDs {fid['first_ids']})"
            p.check("selftest", f"{s} frame ID restarts at every acquisition",
                    "FAIL", ans + "; the backend then needs the trigger-counter "
                    "chunk")
        p.answer("frame_id_restart_and_base", s, ans, **fid)
    ts = rec.get("timestamp")
    if ts:
        if ts["zero_seen"]:
            ans, ok = "the image timestamp reads 0", False
        elif not ts["increasing"]:
            ans, ok = "the timestamp does not increase within an acquisition", False
        elif ts["scale_to_ns"] is None:
            ans, ok = (f"unknown unit: intervals {ts['unit_ratio']} times the "
                       f"frame period in ns"), False
        else:
            unit = ("ns" if ts["scale_to_ns"] == 1 else
                    f"ticks of {ts['scale_to_ns']:g} ns")
            ans = unit + (", continuous across restarts" if ts["continuous"]
                          else ", restarts with the acquisition")
            ok = ts["continuous"]
        p.check("selftest", f"{s} device clock", "PASS" if ok else "FAIL", ans)
        p.answer("timestamp_unit_and_continuity", s, ans, **ts)
    zc = rec.get("zero_copy")
    if zc:
        bufs = zc.get("buffers") or SELFTEST_BUFFERS
        view = zc["frames"] > bufs and zc["distinct_ptrs"] <= bufs
        ans = (f"{zc['distinct_ptrs']} distinct buffer addresses over "
               f"{zc['frames']} frames with {bufs} buffers: "
               + ("the pool's own buffers (a view)" if view else
                  "not decided" if zc["frames"] <= bufs else
                  "a new address per frame (a copy)"))
        p.check("selftest", f"{s} zero-copy buffers cycle through the pool",
                "PASS" if view else "WARN", ans)
        p.answer("zero_copy_capi", s, ans, **{k: zc[k] for k in
                                              ("frames", "buffers",
                                               "distinct_ptrs")})
        c = zc.get("counter_while_streaming") or {}
        if c.get("tried"):
            p.answer("counters_read_while_streaming", s,
                     "readable in free run" if c.get("ok") else
                     f"not readable in free run ({c.get('error')})", **c)
    ef = rec.get("exposure_vs_frame_rate")
    if ef:
        if not ef.get("measured"):
            p.answer("exposure_max_follows_frame_rate", s, None,
                     why_not=ef.get("why_not"))
        else:
            f = ef.get("follows")
            ans = (None if f is None else
                   f"follows: {ef['max_1_us']:g} us at {ef['rate_1']:g} fps, "
                   f"{ef['max_2_us']:g} us at {ef['rate_2']:g} fps" if f else
                   f"does not follow: {ef['max_1_us']:g} us at both rates")
            p.answer("exposure_max_follows_frame_rate", s, ans, **ef)
    td = rec.get("trigger_delay")
    if td:
        if not td["present"]:
            p.answer("trigger_delay_behaviour", s, "no TriggerDelay node")
        else:
            off, on = td["off"], td["on"]
            ans = (f"trigger mode off: readable {off['readable']}, writable "
                   f"{off['writable']}, value "
                   f"{(off['range'] or {}).get('value')}; on: readable "
                   f"{on['readable']}, writable {on['writable']}, value "
                   f"{(on['range'] or {}).get('value')}")
            p.answer("trigger_delay_behaviour", s, ans, **td)
    us = rec.get("user_set_load")
    if us:
        ans = (f"UserSetLoad {us['user_set']} failed: {us['error']}"
               if us["error"] else
               f"UserSetLoad {us['user_set']} changed "
               + (", ".join(us["changed"]) or "nothing the backend writes"))
        p.answer("user_set_load", s, ans, **us)
    ch = rec.get("chunks")
    if ch:
        if ch.get("spellings"):
            p.answer("chunk_name_spelling", s,
                     "; ".join(f"{k}: {', '.join(v)}"
                               for k, v in ch["spellings"].items()),
                     spellings=ch["spellings"], enabled=ch["enabled"])
        else:
            p.answer("chunk_name_spelling", s, None,
                     why_not=ch.get("why_not") or "no chunk value read")
        if "carrier" in ch:
            p.answer("counter_chunk_carrier", s, ch["carrier"],
                     free_run={k: ch.get(k) for k in
                               ("frames", "counter1_before", "exposure_latch",
                                "carrier_note")})
    g = rec.get("gil")
    if g:
        frac = g.get("capi_wait_gil_held_fraction")
        if not g.get("meter_ok"):
            status, ans = "WARN", "the GIL meter's controls failed"
        elif frac is None:
            status, ans = "WARN", "not measured"
        elif frac > GIL_HELD_FAIL:
            status, ans = "FAIL", (f"the wait holds the GIL ({frac:.0%} of the "
                                   f"time)")
        else:
            status, ans = "PASS", f"released (held {frac:.0%} of the wait)"
        p.check("selftest", "GIL released while waiting for an image",
                status, ans)
        p.answer("gil_capi_wait", "host", ans, measured_on=s, **g)


# ============================================================ --triggered
class _Grabber(threading.Thread):
    """One camera's grab thread for the probe: retrieve, read the result's
    fields, look at the zero-copy view, release. It records every frame's
    block ID, device time and the thread CPU the per-frame work took."""

    def __init__(self, backend, cam, clock: _CycleClock):
        super().__init__(daemon=True, name=f"probe-grab-{cam.serial}")
        self.be = backend
        self.cam = cam
        self.clock = clock
        self.ids: list = []
        self.ts: list = []
        self.failed_ids: list = []
        self.hot_us: list = []
        self.frames_retrieved = 0
        self.frame_errors = 0
        self.timeouts = 0
        self.errors: list = []
        self.first_frame_at = None
        self.last_frame_at = None
        self.stop_at = None
        self.first_timeout_after_stop = None
        self.abort = False
        self.padding = 0

    def run(self):
        from gui_app.backends.flir import FlirFrameError
        be, cam = self.be, self.cam
        timeout_exc = be.TimeoutException
        consecutive = 0
        while not self.abort:
            try:
                res = be.retrieve(cam, GRAB_TIMEOUT_MS)
            except timeout_exc:
                now = time.perf_counter()
                if self.stop_at is not None and now >= self.stop_at:
                    self.first_timeout_after_stop = now - self.stop_at
                    return
                self.timeouts += 1
                continue
            except FlirFrameError as e:
                self.frame_errors += 1
                consecutive += 1
                if len(self.errors) < 20:
                    self.errors.append(_err(e))
                if consecutive >= MAX_FRAME_ERRORS:
                    return
                continue
            except Exception as e:
                self.errors.append(_err(e))
                return
            now = time.perf_counter()
            self.frames_retrieved += 1
            if self.first_frame_at is None:
                self.first_frame_at = now
            self.last_frame_at = now
            c0 = self.clock.now()
            bid = ts = None
            ok = False
            try:
                ok = res.GrabSucceeded()
                bid = res.BlockID
                ts = res.TimeStamp
                px, py = res.PaddingX, res.PaddingY
                if px or py:
                    self.padding += 1
                elif ok:
                    with res.GetArrayZeroCopy() as img:
                        img[0, 0]
                consecutive = 0
            except Exception as e:
                # FlirFrameError (a 16-bit wrap the clock cannot decide) or a
                # failed accessor: counted, as the grab loop counts it.
                bid = None
                self.frame_errors += 1
                consecutive += 1
                if len(self.errors) < 20:
                    self.errors.append(_err(e))
            finally:
                res.Release()
            c1 = self.clock.now()
            if len(self.hot_us) < 100000:
                self.hot_us.append(self.clock.us(c1 - c0))
            if bid is None:
                if consecutive >= MAX_FRAME_ERRORS:
                    return
                continue
            if ok:
                self.ids.append(int(bid))
                self.ts.append(int(ts))
            else:
                self.failed_ids.append(int(bid))

    def silent_for(self) -> float:
        if self.last_frame_at is None:
            return math.inf
        return time.perf_counter() - self.last_frame_at


def _open_for_trigger(p: Probe, stage: str):
    """Open the profile's cameras through the FLIR backend and put them in
    trigger mode; (cams, per-camera info) or (None, None) after a FAIL."""
    prof = p.profile
    reason = prof.settings_ready()
    if reason is not None:
        p.check(stage, "the profile's cameras can be opened", "FAIL", reason)
        return None, None
    devs = p.selected(fresh=True)
    want = prof.n_cameras or len(prof.camera_serials or []) or len(devs)
    if len(devs) != want or not devs:
        p.check(stage, "the profile's cameras are connected", "FAIL",
                f"{len(devs)} of {want} found")
        return None, None
    cams, info = [], {}
    for dev in devs:
        cam, err = _backend_open(p, dev)
        if err is not None:
            p.check(stage, f"{dev.serial} opens with the profile", "FAIL", err)
            for c in cams:
                p.backend.close(c)
            return None, None
        cams.append(cam)
    fps = float(prof.frame_rate)
    for i, cam in enumerate(cams):
        rec = {"model": cam.model, "interface": cam.interface,
               "block_id_source": cam.block_id_source,
               "id_offset": cam.id_offset, "id16": cam.id16,
               "ts_source": cam.ts_source, "ts_scale": cam.ts_scale,
               "applied": dict(cam.applied), "counters": dict(cam.counters),
               "counter_chunk_ok": cam.counter_chunk_ok,
               "selftest": cam.selftest}
        try:
            p.backend.set_triggered(cam, prof.trigger_rate_limit,
                                    announce=(i == 0))
            ceiling = p.backend.exposure_ceiling_us(cam, fps,
                                                    prof.trigger_rate_limit)
            rec["ceiling_us"] = ceiling
            exposure = cam.applied.get("exposure_us", prof.camera.exposure_us)
            ok = exposure <= 0.9 * ceiling
            p.check(stage, f"{cam.serial} exposure fits the ceiling at "
                    f"{fps:g} fps", "PASS" if ok else "FAIL",
                    f"{exposure:g} us against 90% of {ceiling:.0f} us")
        except Exception as e:
            rec["error"] = _err(e)
            p.check(stage, f"{cam.serial} trigger mode at {fps:g} fps", "FAIL",
                    _err(e))
            for c in cams:
                p.backend.close(c)
            return None, None
        rec["thermals_at_open"] = p.backend.thermals(cam)
        info[cam.serial] = rec
    return cams, info


def _run_triggered(p: Probe, stage: str, cams, src, fps: float,
                   seconds: float, record_pass: bool = True) -> dict | None:
    """Arm every camera, start the source, grab for `seconds`, stop, and
    return the run's raw results, or None after a FAIL. `record_pass` False
    keeps the start and stop checks out of the table unless they fail."""
    be = p.backend
    from gui_app.trigger_source import (FIRST_TRIGGER_TIMEOUT_S, STOP_WAIT_S,
                                        ExternalTriggerSource)
    src.before_arm(fps)
    grabbers = []
    started = stopped = False
    run = {"source": src.kind, "fps": fps}
    try:
        for cam in cams:
            be.start_grabbing(cam)
        for cam in cams:
            g = _Grabber(be, cam, p.clock)
            grabbers.append(g)
            g.start()
        if not src.host_started:
            time.sleep(ExternalTriggerSource.settle_s(fps))
            early = {g.cam.serial: g.frames_retrieved for g in grabbers}
            run["frames_before_arm"] = early
            refusal = ExternalTriggerSource.early_frame_refusal(early)
            if refusal is not None:
                p.check(stage, "no frame arrives before every camera is armed",
                        "FAIL", refusal.replace("\n", " "))
                return None
            if record_pass:
                p.check(stage, "no frame arrives before every camera is armed",
                        "PASS")
        started = src.start(fps, "probe run")
        t_start = time.perf_counter()
        run["start_confirmed"] = started
        if not started:
            p.check(stage, f"{src.describe()} starts at {fps:g} Hz", "FAIL",
                    "the board did not confirm the start (no RDY ack)")
            return None
        if src.host_started:
            if record_pass:
                p.check(stage, f"{src.describe()} starts at {fps:g} Hz",
                        "PASS", "RDY ack")
        else:
            deadline = t_start + FIRST_TRIGGER_TIMEOUT_S
            while time.perf_counter() < deadline and not any(
                    g.frames_retrieved for g in grabbers):
                time.sleep(0.05)
            if not any(g.frames_retrieved for g in grabbers):
                p.check(stage, "a trigger reaches the cameras", "FAIL",
                        ExternalTriggerSource.no_trigger_text(
                            fps, FIRST_TRIGGER_TIMEOUT_S, "the probe")
                        .replace("\n", " "))
                return None
            t_start = min(g.first_frame_at for g in grabbers
                          if g.first_frame_at is not None)
        end = t_start + seconds
        while time.perf_counter() < end:
            time.sleep(min(0.1, max(0.0, end - time.perf_counter())))
        t_stop = time.perf_counter()
        run["seconds"] = round(t_stop - t_start, 3)
        stop_ok = src.stop("probe run")
        stopped = True
        run["stop_confirmed"] = stop_ok
        if src.host_started:
            if record_pass or not stop_ok:
                p.check(stage, "the board confirms the stop",
                        "PASS" if stop_ok else "FAIL",
                        "" if stop_ok else "no RDY ack for the stop; the board "
                        "may still be triggering")
            for g in grabbers:
                g.stop_at = t_stop
            limit = t_stop + STOP_GRACE_S
        else:
            silent = ExternalTriggerSource.stop_silence_s(fps)
            limit_wait = time.perf_counter() + STOP_WAIT_S
            while time.perf_counter() < limit_wait and not all(
                    g.silent_for() > silent for g in grabbers):
                time.sleep(0.05)
            run["source_stopped"] = all(g.silent_for() > silent
                                        for g in grabbers)
            p.check(stage, "your trigger source stopped when asked",
                    "PASS" if run["source_stopped"] else "FAIL",
                    "" if run["source_stopped"] else
                    f"frames were still arriving {STOP_WAIT_S:.0f} s after the "
                    f"prompt")
            now = time.perf_counter()
            for g in grabbers:
                g.stop_at = now
            limit = now + STOP_GRACE_S
        for g in grabbers:
            g.join(max(0.0, limit - time.perf_counter()))
        run["still_receiving"] = [g.cam.serial for g in grabbers
                                  if g.is_alive()]
        run["t_start"] = t_start
        run["t_stop"] = t_stop
        run["grabbers"] = grabbers
        return run
    finally:
        for g in grabbers:
            g.abort = True
        for g in grabbers:
            g.join(2.0)
        run["threads_alive"] = [g.cam.serial for g in grabbers if g.is_alive()]
        if started and not stopped:
            # A run that ends early still stops the triggers it started.
            with contextlib.suppress(Exception):
                src.stop("probe run")


def _stop_cams(p: Probe, cams, grabbers) -> dict:
    """Stream statistics before and after StopGrabbing, and the backend's
    witness sentences, per camera."""
    out = {}
    frames = {g.cam.serial: g.frames_retrieved for g in grabbers}
    for cam in cams:
        rec = {"stats_before_end": p.backend.stream_stats(cam)}
        try:
            p.backend.stop_grabbing(cam)
        except Exception as e:
            rec["stop_error"] = _err(e)
        rec["stats_after_end"] = p.backend.stream_stats(cam)
        rec["witness"] = dict(cam.witness) if cam.witness else None
        rec["acquisition_warnings"] = p.backend.acquisition_warnings(
            cam, frames.get(cam.serial, 0))
        out[cam.serial] = rec
    return out


def _analyse(p: Probe, stage: str, run: dict, stops: dict, fps: float) -> dict:
    from gui_app.backends import block_rate_hints
    from gui_app.frame_sync import check_block_id_rate, source_name
    hints = dict(block_rate_hints("flir"))
    hints["source_hint"] = source_name(p.profile.trigger_source)
    per = {}
    grabbers = run["grabbers"]
    seen = max((max(g.ids) for g in grabbers if g.ids), default=0)
    expected_time = fps * run.get("seconds", 0.0)
    for g in grabbers:
        s = g.cam.serial
        ids = g.ids
        uniq = sorted(set(ids))
        gaps = 0
        max_gap = 0
        for a, b in zip(uniq, uniq[1:]):
            if b - a > 1:
                gaps += b - a - 1
                max_gap = max(max_gap, b - a - 1)
        rate_msg = check_block_id_rate(ids, [t / 1e9 for t in g.ts], int(fps),
                                       name=s, **hints) if ids else None
        hot = sorted(g.hot_us)
        rec = {"frames": len(ids), "frames_retrieved": g.frames_retrieved,
               "triggers_seen": seen, "expected_from_time": round(expected_time),
               "first_id": ids[0] if ids else None,
               "last_id": ids[-1] if ids else None,
               "gaps": gaps, "max_gap": max_gap,
               "duplicates": len(ids) - len(uniq),
               "incomplete": len(g.failed_ids), "padding": g.padding,
               "frame_errors": g.frame_errors, "errors": g.errors,
               "timeouts_before_stop": g.timeouts,
               "block_rate": {"message": rate_msg},
               "first_timeout_after_stop_ms":
                   None if g.first_timeout_after_stop is None
                   else round(g.first_timeout_after_stop * 1000, 1),
               "hot_path_cpu_us_per_frame": {
                   "median": round(statistics.median(hot), 1) if hot else None,
                   "p95": round(hot[int(0.95 * (len(hot) - 1))], 1) if hot
                   else None, "basis": p.clock.basis}}
        rec.update(stops.get(s, {}))
        w = rec.get("witness") or {}
        rec["counter_edges"] = w.get("edges")
        rec["counter_exposures"] = w.get("exposures")
        per[s] = rec
        if not ids:
            p.check(stage, f"{s} receives frames", "FAIL", "no frame arrived")
            continue
        p.check(stage, f"{s} receives frames", "PASS",
                f"{len(ids)} frames, {seen} triggers seen")
        p.check(stage, f"{s} first block ID is 1",
                "PASS" if ids[0] == 1 else "FAIL" if ids[0] < 1 else "WARN",
                f"first ID {ids[0]}")
        if len(ids) > seen:
            p.check(stage, f"{s} frames do not outnumber the triggers", "FAIL",
                    f"{len(ids)} frames for {seen} triggers")
        p.check(stage, f"{s} block IDs advance at the trigger rate",
                "PASS" if rate_msg is None else "FAIL", rate_msg or "")
        sentences = rec.get("acquisition_warnings") or []
        p.check(stage, f"{s} trigger witness", "FAIL" if sentences else "PASS",
                " ".join(sentences) if sentences else
                (f"edges {w.get('edges')}, exposures {w.get('exposures')}"
                 if w else "no witness (no counters)"))
        if gaps or rec["incomplete"]:
            p.check(stage, f"{s} frames lost", "WARN",
                    f"{gaps} gap(s), {rec['incomplete']} incomplete")
        stopped = g.first_timeout_after_stop is not None
        p.check(stage, f"{s} stops receiving once the triggers stop",
                "PASS" if stopped else "FAIL",
                f"first timeout {rec['first_timeout_after_stop_ms']} ms after "
                f"the stop" if stopped else
                "frames were still arriving after the stop")
        med = rec["hot_path_cpu_us_per_frame"]["median"]
        if med is not None and med > HOT_PATH_BUDGET_US:
            p.check(stage, f"{s} per-frame CPU of the backend's hot path",
                    "WARN", f"median {med:.0f} us, above {HOT_PATH_BUDGET_US:g}")
        p.answer("hot_path_cost", s,
                 None if med is None else f"median {med:.1f} us per frame "
                 f"(thread CPU, {p.clock.basis})",
                 **rec["hot_path_cpu_us_per_frame"])
        before = rec.get("stats_before_end") or {}
        after = rec.get("stats_after_end") or {}
        key = "StreamStartedFrameCount"
        if key in before and key in after:
            p.answer("stream_counters_across_end", s,
                     "reset at EndAcquisition" if after[key] == 0 < before[key]
                     else "kept across EndAcquisition"
                     if after[key] == before[key] else
                     f"{key} {before[key]} before, {after[key]} after",
                     before=before, after=after)
    counts = {s: r["frames"] for s, r in per.items()}
    sets = [set(g.ids) for g in grabbers]
    common = set.intersection(*sets) if sets else set()
    cross = {"equal_counts": len(set(counts.values())) <= 1,
             "common_ids": len(common), "counts": counts}
    if len(grabbers) > 1:
        p.check(stage, "every camera caught the same triggers",
                "PASS" if cross["equal_counts"] else "WARN",
                ", ".join(f"{s} {n}" for s, n in counts.items()))
        p.check(stage, "the cameras share block IDs",
                "PASS" if common else "FAIL", f"{len(common)} common IDs")
    return {"per_camera": per, "cross_camera": cross}


def stage_triggered(p: Probe):
    prof = p.profile
    if prof is None or prof.camera is None:
        p.check("triggered", "stage runs", "FAIL", "needs a FLIR profile")
        return
    seconds = float(p.args.triggered)
    fps = float(prof.frame_rate)
    rep = {"fps": fps, "duration_s": seconds, "source": prof.trigger_source}
    p.report["triggered"] = rep
    cams, info = _open_for_trigger(p, "triggered")
    if cams is None:
        return
    rep["open"] = info
    src = p.source()
    try:
        why = src.open()
        if why is not None:
            p.check("triggered", f"{src.describe()} is usable", "FAIL", why)
            return
        rep["board"] = ({"port": src.port, "sketch_id": src.heard_id}
                        if src.kind == "board" else None)
        run = _run_triggered(p, "triggered", cams, src, fps, seconds)
        if run is None:
            for cam in cams:
                with contextlib.suppress(Exception):
                    p.backend.stop_grabbing(cam)
            return
        rep["seconds"] = run.get("seconds")
        rep["start_confirmed"] = run.get("start_confirmed")
        rep["stop_confirmed"] = run.get("stop_confirmed")
        rep["still_receiving"] = run.get("still_receiving")
        stops = _stop_cams(p, cams, run["grabbers"])
        rep.update(_analyse(p, "triggered", run, stops, fps))
        for cam in cams:
            info[cam.serial]["thermals_after"] = p.backend.thermals(cam)
        p.save()
        if p.args.no_counter_check:
            for uid in ("counter_chunk_latch", "exposure_latency_after_edge",
                        "delayed_exposure_at_end", "exposes_while_host_stopped",
                        "counters_count_as_set"):
                p.not_measured(uid, "the counter checks were skipped "
                                    "(--no-counter-check)")
        elif src.kind != "board":
            why = ("the counter checks need Panopticon's trigger board: they "
                   "reset the counters and arm each camera before the first "
                   "pulse, which a source the host cannot start does not "
                   "allow. Run them with a profile that has trigger_source: "
                   "board.")
            p.check("triggered", "counter checks", "SKIP", why)
            for uid in ("counter_chunk_latch", "exposure_latency_after_edge",
                        "delayed_exposure_at_end", "exposes_while_host_stopped",
                        "counters_count_as_set"):
                p.not_measured(uid, why)
        else:
            rep["counter_checks"] = _counter_checks(p, cams, src)
    finally:
        closed = src.close()
        if src.kind == "board" and closed is not None:
            p.check("triggered", "trigger board stood down",
                    "PASS" if closed else "FAIL",
                    "" if closed else "the stop was not confirmed; "
                    "power-cycle the board")
        for cam in cams:
            with contextlib.suppress(Exception):
                p.backend.close(cam)


# -------------------------------------------------------- counter checks
def _drain(p: Probe, h, stop: threading.Event, pause: threading.Event,
           out: list):
    """Take images from camera `h` until `stop`, holding off while `pause`
    is set; append (frame_id, chunk counter or None, host time)."""
    api = p.api
    from gui_app.backends.flir import FlirTimeout
    spelling = [None]
    while not stop.is_set():
        if pause.is_set():
            time.sleep(0.01)
            continue
        try:
            img = api.next_image(h, GRAB_TIMEOUT_MS)
        except FlirTimeout:
            continue
        except Exception:
            time.sleep(0.01)
            continue
        try:
            fid = int(api.image_frame_id(img))
            ctr = None
            for name in ([spelling[0]] if spelling[0] else
                         ("CounterValue", "ChunkCounterValue")):
                try:
                    ctr = int(api.chunk_int(img, name))
                    spelling[0] = name
                    break
                except Exception:
                    continue
            out.append((fid, ctr, time.perf_counter()))
        finally:
            api.image_release(img)


def _drain_until_quiet(p: Probe, h, out: list):
    """After the board stops: take the queued images until one timeout (or
    any other error, which ends the drain as well)."""
    api = p.api
    while True:
        try:
            img = api.next_image(h, GRAB_TIMEOUT_MS)
        except Exception:
            return
        try:
            out.append((int(api.image_frame_id(img)), None, time.perf_counter()))
        finally:
            api.image_release(img)


def _counter_checks(p: Probe, cams, src) -> dict:
    """Runs A, B and C and the delay test on the open, triggered cameras,
    with the board at COUNTER_HZ. Results per camera, and the unknowns they
    answer."""
    out = {"hz": COUNTER_HZ, "per_camera": {}}
    rigs = []
    for cam in cams:
        raw = _Raw(p.api, cam.handle, cam.who)
        rec = {"counters": dict(cam.counters)}
        out["per_camera"][cam.serial] = rec
        rigs.append((cam, raw, rec))
    with_counters = [(c, r, rec) for c, r, rec in rigs if c.counters]
    for c, r, rec in rigs:
        if not c.counters:
            for uid in ("counter_chunk_latch", "exposure_latency_after_edge",
                        "delayed_exposure_at_end", "exposes_while_host_stopped",
                        "counters_count_as_set"):
                p.answer(uid, c.serial, None, why_not="the camera has no counters")
    if not with_counters:
        p.check("triggered", "counter checks", "SKIP", "no camera has counters")
        return out
    try:
        _run_a(p, with_counters, src)
        _run_b(p, with_counters, src)
        _run_c(p, with_counters, src)
        _run_delay(p, with_counters, src)
    except Exception as e:
        p.error("triggered", e)
        p.check("triggered", "counter checks", "FAIL", _err(e))
    return out


def _begin_all(p: Probe, rigs):
    for c, _r, _rec in rigs:
        p.api.begin(c.handle)


def _end_all(p: Probe, rigs):
    for c, _r, _rec in rigs:
        with contextlib.suppress(Exception):
            if p.api.is_streaming(c.handle):
                p.api.end(c.handle)


def _run_a(p: Probe, rigs, src):
    """The CounterValue chunk in trigger mode: its carrier and its latch."""
    active = []
    for c, raw, rec in rigs:
        a = rec["run_a"] = {"frames": []}
        if "CounterValue" not in raw.entries("ChunkSelector"):
            a["why_not"] = "no CounterValue chunk"
            p.answer("counter_chunk_latch", c.serial, None,
                     why_not="no CounterValue chunk")
            continue
        a["chunk_mode_before"] = raw.read("ChunkModeActive")
        with raw.under("ChunkSelector", "CounterValue"):
            a["chunk_enable_before"] = raw.read("ChunkEnable")
        raw.write("ChunkModeActive", True)
        with raw.under("ChunkSelector", "CounterValue"):
            raw.write("ChunkEnable", True)
        a["chunk_counter_selector"] = raw.has("ChunkCounterSelector")
        if a["chunk_counter_selector"]:
            raw.write("ChunkCounterSelector", "Counter0")
        raw.write("CounterSelector", "Counter0")
        raw.execute("CounterReset")
        a["counter1_before"] = (_counter_read(raw, "Counter1")
                                if "Counter1" in c.counters else None)
        # Left on Counter1: a chunk that follows CounterSelector reads it.
        raw.write("CounterSelector", "Counter1" if "Counter1" in c.counters
                  else "Counter0")
        active.append((c, raw, rec))
    if not active:
        return
    stop, pause = threading.Event(), threading.Event()
    threads = []
    _begin_all(p, active)
    started = started_ok = False
    try:
        for c, _raw, rec in active:
            t = threading.Thread(target=_drain, daemon=True,
                                 args=(p, c.handle, stop, pause,
                                       rec["run_a"]["frames"]))
            threads.append(t)
            t.start()
        started = src.start(COUNTER_HZ)
        if started:
            time.sleep(RUN_A_S)
        src.stop()
        started_ok, started = started, False
        time.sleep(0.3)
    finally:
        if started:
            with contextlib.suppress(Exception):
                src.stop()
        stop.set()
        for t in threads:
            t.join(3.0)
        for c, _raw, rec in active:
            _drain_until_quiet(p, c.handle, rec["run_a"]["frames"])
        _end_all(p, active)
    for c, raw, rec in active:
        a = rec["run_a"]
        a["edges_after"] = _counter_read(raw, "Counter0")
        raw.write("CounterSelector", "Counter0")
        if c.block_id_source != "trigger_counter":
            with raw.under("ChunkSelector", "CounterValue"):
                raw.write("ChunkEnable", bool(a["chunk_enable_before"]))
            if a["chunk_mode_before"] is not None:
                raw.write("ChunkModeActive", bool(a["chunk_mode_before"]))
        frames = [(f, v) for f, v, _t in a["frames"] if v is not None]
        a["frames"] = [[f, v] for f, v, _t in a["frames"]]
        if not started_ok:
            p.answer("counter_chunk_latch", c.serial, None,
                     why_not="the board did not start")
            continue
        if len(frames) < 3:
            p.answer("counter_chunk_latch", c.serial, None,
                     why_not=f"{len(frames)} chunk values read")
            continue
        fid = (c.selftest or {}).get("frame_id") or {}
        if fid.get("restarts") and fid.get("counts_frames"):
            # The ordinal of each image's trigger, from the frame ID.
            off = 1 if fid.get("base") == 0 else 0
            k = [f + off for f, _v in frames]
            a["ordinal_from"] = "frame ID"
        else:
            base = frames[0][0]
            k = [f - base + 1 for f, _v in frames]
            a["ordinal_from"] = "the first image (the frame ID does not restart)"
        vals = [v for _f, v in frames]
        before = a.get("counter1_before")
        diffs = sorted({v - i for v, i in zip(vals, k)})
        latch = None
        if len(set(vals)) == 1:
            carrier = "the counter CounterSelector names when acquisition starts"
        elif before is None or before < 3:
            carrier = None
            a["carrier_note"] = (f"Counter1 read {before} before the run, so "
                                 f"the two counters read alike")
            if len(diffs) == 1 and diffs[0] in (0, -1):
                latch = ("counts the edge that started the image"
                         if diffs == [0] else
                         "latched before the edge that started the image")
        elif all(v >= before for v in vals) and len(diffs) == 1                 and diffs[0] >= before - 1:
            carrier = "the counter CounterSelector names when acquisition starts"
        elif len(diffs) == 1 and diffs[0] in (0, -1):
            carrier = ("Counter0 (ChunkCounterSelector)"
                       if a["chunk_counter_selector"] else
                       "Counter0, whatever CounterSelector names")
            latch = ("counts the edge that started the image" if diffs == [0]
                     else "latched before the edge that started the image")
        else:
            carrier = None
        a.update({"carrier": carrier, "latch": latch, "diffs": diffs})
        p.answer("counter_chunk_latch", c.serial, latch, diffs=diffs,
                 first_frames=frames[:5])
        prev = p.report["unknowns"]["counter_chunk_carrier"]["answers"].get(
            c.serial, {})
        p.answer("counter_chunk_carrier", c.serial, carrier,
                 free_run=prev.get("free_run"), triggered={
                     "counter1_before": before, "diffs": diffs})


def _run_b(p: Probe, rigs, src):
    """Counter reads while the camera streams, the exposure latency after
    each edge, and whether exposures continue while no frame is taken."""
    active = []
    for c, raw, rec in rigs:
        b = rec["run_b"] = {"frames": [], "samples": [], "read_errors": 0}
        b["buffers_before"] = raw.read("StreamBufferCountManual", "tlstream")
        raw.write("StreamBufferCountManual", RUN_B_BUFFERS, "tlstream")
        for sel in c.counters:
            raw.write("CounterSelector", sel)
            raw.execute("CounterReset")
        raw.write("CounterSelector", "Counter0")
        b["delay_s"] = getattr(c, "_trigger_delay_s", 0.0)
        active.append((c, raw, rec))
    stop, pause = threading.Event(), threading.Event()
    poll_stop = threading.Event()
    threads, pollers = [], []

    def poll(raw, b, sels):
        read_s = []
        while not poll_stop.is_set():
            t0 = time.perf_counter()
            row = {"t": t0, "phase": b.get("phase")}
            for sel, what in sels:
                v = _counter_read(raw, sel)
                if v is None:
                    b["read_errors"] += 1
                row[what] = v
            read_s.append(time.perf_counter() - t0)
            b["samples"].append(row)
            time.sleep(COUNTER_POLL_S)
        b["read_time_ms"] = (round(1000 * statistics.median(read_s), 3)
                             if read_s else None)

    _begin_all(p, active)
    started = started_ok = False
    try:
        for c, raw, rec in active:
            b = rec["run_b"]
            b["phase"] = "drain"
            t = threading.Thread(target=_drain, daemon=True,
                                 args=(p, c.handle, stop, pause, b["frames"]))
            threads.append(t)
            t.start()
            sels = [(s, w) for s, w in c.counters.items()]
            pt = threading.Thread(target=poll, daemon=True, args=(raw, b, sels))
            pollers.append(pt)
            pt.start()
        started = src.start(COUNTER_HZ)
        if started:
            time.sleep(RUN_B_DRAIN_S)
            for _c, _raw, rec in active:
                rec["run_b"]["phase"] = "pause"
            pause.set()
            time.sleep(RUN_B_PAUSE_S)
            for c, _raw, rec in active:
                rec["run_b"]["phase"] = "resume"
                rec["run_b"]["stats_after_pause"] = p.backend.stream_stats(c)
            pause.clear()
            time.sleep(RUN_B_RESUME_S)
        src.stop()
        started_ok, started = started, False
        time.sleep(0.3)
    finally:
        if started:
            with contextlib.suppress(Exception):
                src.stop()
        poll_stop.set()
        for t in pollers:
            t.join(3.0)
        stop.set()
        pause.clear()
        for t in threads:
            t.join(3.0)
        for c, _raw, rec in active:
            _drain_until_quiet(p, c.handle, rec["run_b"]["frames"])
        _end_all(p, active)
    for c, raw, rec in active:
        b = rec["run_b"]
        final = {what: _counter_read(raw, sel) for sel, what in c.counters.items()}
        raw.write("CounterSelector", "Counter0")
        if b["buffers_before"] is not None:
            raw.write("StreamBufferCountManual", int(b["buffers_before"]),
                      "tlstream")
        b["final"] = final
        frames = [f for f, _v, _t in b["frames"]]
        b["frame_count"] = len(frames)
        b["max_frame_id"] = max(frames) if frames else None
        samples = b.pop("samples")
        b["samples_kept"] = samples[:: max(1, len(samples) // 200)]
        if not started_ok:
            for uid in ("exposure_latency_after_edge", "exposes_while_host_stopped",
                        "counters_count_as_set", "counters_read_while_streaming"):
                p.answer(uid, c.serial, None, why_not="the board did not start")
            continue
        p.answer("counters_read_while_streaming", c.serial,
                 "readable while triggered frames stream"
                 if b["read_errors"] == 0 and samples else
                 f"{b['read_errors']} failed reads of {len(samples)}",
                 read_time_ms=b.get("read_time_ms"),
                 free_run=p.report["unknowns"]["counters_read_while_streaming"]
                 ["answers"].get(c.serial))
        _latency_answer(p, c, b, samples)
        _pause_answer(p, c, b, samples)
        b["counts_verdict"] = _counts_verdict(c, b)


def _first_time(samples, key, n):
    for row in samples:
        v = row.get(key)
        if v is not None and v >= n:
            return row["t"]
    return None


def _latency_answer(p: Probe, c, b: dict, samples: list):
    if "exposures" not in c.counters.values():
        p.answer("exposure_latency_after_edge", c.serial, None,
                 why_not="no exposure counter")
        return
    drain = [r for r in samples if r["phase"] == "drain"]
    top = max((r.get("edges") or 0 for r in drain), default=0)
    lat = []
    for n in range(1, top + 1):
        te = _first_time(drain, "edges", n)
        tx = _first_time(samples, "exposures", n)
        if te is not None and tx is not None:
            lat.append(tx - te)
    if not lat:
        p.answer("exposure_latency_after_edge", c.serial, None,
                 why_not="no edge was counted while frames were taken")
        return
    read_ms = b.get("read_time_ms") or 0.0
    delay_ms = 1000 * (b.get("delay_s") or 0.0)
    med = 1000 * statistics.median(lat)
    worst = 1000 * max(lat)
    within = worst <= delay_ms + 2 * read_ms + 1000 * COUNTER_POLL_S + 1.0
    ans = (f"median {med:.2f} ms, worst {worst:.2f} ms after the edge "
           f"(TriggerDelay {delay_ms:g} ms, one read {read_ms:g} ms): "
           + ("within the delay and one read" if within else
              "later than the delay and one read"))
    p.answer("exposure_latency_after_edge", c.serial, ans,
             median_ms=round(med, 3), worst_ms=round(worst, 3),
             delay_ms=delay_ms, read_ms=read_ms, edges=len(lat),
             within_assumption=within)


def _pause_answer(p: Probe, c, b: dict, samples: list):
    pause = [r for r in samples if r["phase"] == "pause"]
    if len(pause) < 2 or "exposures" not in c.counters.values():
        p.answer("exposes_while_host_stopped", c.serial, None,
                 why_not="no exposure counter or no reads during the pause")
        return
    first, last = pause[0], pause[-1]
    de = (last.get("edges") or 0) - (first.get("edges") or 0)
    dx = (last.get("exposures") or 0) - (first.get("exposures") or 0)
    lost = (b.get("stats_after_pause") or {}).get("StreamLostFrameCount")
    if de <= 0:
        ans = None
    elif dx >= de - 1:
        ans = (f"keeps exposing: {dx} exposures for {de} edges while no frame "
               f"was taken (host pool full, {lost} lost)")
    elif dx == 0:
        ans = (f"not decided: the exposure counter did not move while no frame "
               f"was taken ({de} edges), so it may count only as frames are "
               f"taken")
    elif dx <= RUN_B_BUFFERS + 1:
        ans = (f"stops exposing once the host pool is full: {dx} exposures "
               f"for {de} edges")
    else:
        ans = f"not decided: {dx} exposures for {de} edges"
    p.answer("exposes_while_host_stopped", c.serial, ans, edges=de,
             exposures=dx, stream_lost=lost,
             note="a full host pool stands in for a stalled stream")


def _counts_verdict(c, b: dict) -> tuple:
    """(text, ok) for run B's counts: ok True when the counters agree with
    each other and with the frame IDs, False when one counted nothing, None
    when the counts do not settle it."""
    final = b["final"]
    e, x = final.get("edges"), final.get("exposures")
    mx = b["max_frame_id"]
    n = b["frame_count"]
    if e is None:
        ans, ok = "the edge counter could not be read", False
    elif n and not e:
        ans, ok = (f"Counter0 counted no edges on {c.trigger_line} while "
                   f"{n} frames arrived"), False
    elif "exposures" in c.counters.values() and n and not x:
        ans, ok = "Counter1 counted no exposures while frames arrived", False
    elif "exposures" in c.counters.values() and e == x and (
            mx is None or abs(mx - x) <= 1):
        ans, ok = (f"both count as set: {e} edges, {x} exposures, last frame "
                   f"ID {mx}"), True
    elif "exposures" not in c.counters.values() and mx is not None \
            and abs(e - mx) <= 1:
        ans, ok = f"the edge counter counts as set: {e} edges, last frame ID {mx}", True
    else:
        ans, ok = (f"not decided: {e} edges, {x} exposures, {n} frames, last "
                   f"frame ID {mx}"), None
    return ans, ok


def _run_c(p: Probe, rigs, src):
    """Make every camera ignore triggers: an exposure longer than the trigger
    period leaves the camera busy for the next edge. The witness must then
    count edges the exposures do not, which shows each counter counts the
    event it was set to (two counters that count the same event agree
    whatever the camera ignores)."""
    want = IGNORE_EXPOSURE_PERIODS * 1e6 / COUNTER_HZ
    active = []
    for c, raw, rec in rigs:
        cc = rec["run_c"] = {"exposure_us": want}
        if "exposures" not in c.counters.values():
            cc["why_not"] = "no exposure counter"
            _counts_answer(p, c, rec)
            continue
        rg = raw.rng("ExposureTime") or {}
        if rg.get("max") is None or rg["max"] < want \
                or not raw.writable("ExposureTime"):
            cc["why_not"] = (f"ExposureTime cannot be set to {want:.0f} us "
                             f"(range {rg.get('min')} to {rg.get('max')})")
            _counts_answer(p, c, rec)
            continue
        cc["exposure_before"] = rg.get("value")
        err = raw.write("ExposureTime", float(want))
        if err is not None:
            cc["why_not"] = err
            _counts_answer(p, c, rec)
            continue
        for sel in c.counters:
            raw.write("CounterSelector", sel)
            raw.execute("CounterReset")
        raw.write("CounterSelector", "Counter0")
        cc["frames"] = []
        active.append((c, raw, rec))
    if not active:
        return
    stop, pause = threading.Event(), threading.Event()
    threads = []
    _begin_all(p, active)
    started = started_ok = False
    t0 = t1 = time.perf_counter()
    try:
        for c, _raw, rec in active:
            t = threading.Thread(target=_drain, daemon=True,
                                 args=(p, c.handle, stop, pause,
                                       rec["run_c"]["frames"]))
            threads.append(t)
            t.start()
        started = src.start(COUNTER_HZ)
        t0 = time.perf_counter()
        if started:
            time.sleep(RUN_C_S)
        t1 = time.perf_counter()
        src.stop()
        started_ok, started = started, False
        time.sleep(0.3)
    finally:
        if started:
            with contextlib.suppress(Exception):
                src.stop()
        stop.set()
        for t in threads:
            t.join(3.0)
        for c, _raw, rec in active:
            _drain_until_quiet(p, c.handle, rec["run_c"]["frames"])
        _end_all(p, active)
    for c, raw, rec in active:
        cc = rec["run_c"]
        final = {what: _counter_read(raw, sel) for sel, what in c.counters.items()}
        raw.write("CounterSelector", "Counter0")
        if cc.get("exposure_before") is not None:
            raw.write("ExposureTime", float(cc["exposure_before"]))
        cc["frames"] = len(cc["frames"])
        cc.update(final)
        cc["pulses_estimate"] = (round(COUNTER_HZ * (t1 - t0)) if started_ok
                                 else None)
        if not started_ok:
            cc["why_not"] = "the board did not start"
        _counts_answer(p, c, rec)


def _ignore_verdict(cc: dict) -> tuple:
    """(text, ok) for run C."""
    if cc.get("why_not"):
        return f"run C not measured: {cc['why_not']}", None
    e, x, n = cc.get("edges"), cc.get("exposures"), cc.get("frames")
    pulses = cc.get("pulses_estimate")
    if e is None or x is None:
        return "run C not decided: a counter could not be read", None
    ignored = e - n
    if ignored >= 3 and abs(x - n) <= 1:
        return (f"the counters tell ignored triggers apart: at a "
                f"{cc['exposure_us']:.0f} us exposure the camera took {n} "
                f"frames for {e} edges and counted {x} exposures"), True
    if ignored >= 3 and abs(x - e) <= 1:
        return (f"Counter1 counted {x} for {e} edges while only {n} frames "
                f"arrived, so it counts edges, not exposures, and an ignored "
                f"trigger would not show"), False
    if ignored < 3 and pulses and abs(e - n) <= 1 and e <= 0.75 * pulses:
        return (f"Counter0 counted {e}, about the {n} frames, for about "
                f"{pulses} pulses the board sent, so it may count exposures "
                f"instead of edges on the trigger line"), False
    return (f"run C not decided: {e} edges, {x} exposures, {n} frames at a "
            f"{cc['exposure_us']:.0f} us exposure (about {pulses} pulses)"), None


def _counts_answer(p: Probe, c, rec: dict):
    """The answer to counters_count_as_set from runs B and C together: PASS
    needs both, and either one failing fails it."""
    b_ans, b_ok = (rec.get("run_b") or {}).get("counts_verdict") or (
        "run B did not run", None)
    c_ans, c_ok = _ignore_verdict(rec.get("run_c") or {})
    rec.setdefault("run_c", {})["verdict"] = c_ans
    if b_ok is False or c_ok is False:
        ok = False
    elif b_ok and c_ok:
        ok = True
    else:
        ok = None
    ans = f"run B: {b_ans}; run C: {c_ans}"
    p.answer("counters_count_as_set", c.serial,
             None if b_ok is None and c_ok is None else ans,
             run_b=b_ans, run_c=c_ans, ok=ok)
    p.check("triggered", f"{c.serial} counters count what the backend sets",
            "PASS" if ok else "FAIL" if ok is False else "WARN", ans)


def _run_delay(p: Probe, rigs, src):
    """End the acquisition while a delayed exposure is pending, then see
    whether the exposure counter counts it."""
    active = []
    for c, raw, rec in rigs:
        d = rec["delay_test"] = {}
        if "exposures" not in c.counters.values():
            p.answer("delayed_exposure_at_end", c.serial, None,
                     why_not="no exposure counter")
            continue
        rg = raw.rng("TriggerDelay")
        if not rg or not raw.writable("TriggerDelay") or rg.get("max") is None:
            p.answer("delayed_exposure_at_end", c.serial, None,
                     why_not="TriggerDelay is not writable in trigger mode")
            continue
        period_us = 1e6 / COUNTER_HZ
        delay = float(min(rg["max"], DELAY_TEST_MAX_US, 0.5 * period_us))
        d["delay_before"] = rg.get("value")
        err = raw.write("TriggerDelay", delay)
        if err is not None:
            d["error"] = err
            p.answer("delayed_exposure_at_end", c.serial, None, why_not=err)
            continue
        d["delay_us"] = raw.read("TriggerDelay")
        for sel in c.counters:
            raw.write("CounterSelector", sel)
            raw.execute("CounterReset")
        raw.write("CounterSelector", "Counter0")
        active.append((c, raw, rec))
    if not active:
        return
    _begin_all(p, active)
    started = started_ok = False
    try:
        started = src.start(COUNTER_HZ)
        pending = {c.serial for c, _r, _rec in active}
        deadline = time.perf_counter() + 3.0
        base = {c.serial: _counter_read(raw, "Counter0") for c, raw, _rec in active}
        while started and pending and time.perf_counter() < deadline:
            for c, raw, rec in active:
                if c.serial not in pending:
                    continue
                e = _counter_read(raw, "Counter0")
                if e is not None and base[c.serial] is not None \
                        and e > base[c.serial]:
                    t_edge = time.perf_counter()
                    x = _counter_read(raw, "Counter1")
                    with contextlib.suppress(Exception):
                        p.api.end(c.handle)
                    rec["delay_test"].update({
                        "edges_at_end": e, "exposures_at_end": x,
                        "end_after_edge_ms": round(
                            1000 * (time.perf_counter() - t_edge), 3)})
                    pending.discard(c.serial)
            time.sleep(0.0005)
        time.sleep(max(r["delay_test"].get("delay_us") or 0.0
                       for _c, _raw, r in active) * 1e-6 + 0.03)
        for c, raw, rec in active:
            rec["delay_test"]["exposures_after"] = _counter_read(raw, "Counter1")
        src.stop()
        started_ok, started = started, False
        time.sleep(0.2)
    finally:
        if started:
            with contextlib.suppress(Exception):
                src.stop()
        _end_all(p, active)
    for c, raw, rec in active:
        d = rec["delay_test"]
        rec["delay_test"]["edges_final"] = _counter_read(raw, "Counter0")
        raw.write("CounterSelector", "Counter0")
        if d.get("delay_before") is not None:
            raw.write("TriggerDelay", float(d["delay_before"]))
        e, x0, x1 = (d.get("edges_at_end"), d.get("exposures_at_end"),
                     d.get("exposures_after"))
        if not started_ok or e is None:
            ans = None
            why = ("the board did not start" if not started_ok else
                   "no edge arrived within 3 s")
            p.answer("delayed_exposure_at_end", c.serial, ans, why_not=why, **d)
            continue
        ended_in_time = (d.get("end_after_edge_ms") or 0) < \
            0.5 * (d.get("delay_us") or 0) / 1000
        if x0 is None or x1 is None or x0 != e - 1 or not ended_in_time:
            ans = (f"not decided: {e} edges and {x0} exposures when "
                   f"EndAcquisition ran {d.get('end_after_edge_ms')} ms after "
                   f"the edge, {x1} exposures after the delay")
        elif x1 == e:
            ans = "exposed: the pending exposure was counted after EndAcquisition"
        else:
            ans = "cancelled: EndAcquisition dropped the pending exposure"
        p.answer("delayed_exposure_at_end", c.serial, ans, **d)


# ======================================================= --exposure-sweep
def _sweep_step(p: Probe, cams, src, fps: float, e: float, tol: float):
    """One exposure step: arm, trigger for SWEEP_STEP_S, stop. The row, and
    whether every camera acquired every trigger (None when the run
    failed)."""
    prof = p.profile
    row = {"exposure_us": round(e, 1), "per_camera": {}}
    for cam in cams:
        # A fresh triggered arm, so the witness counts this step alone.
        p.backend.set_triggered(cam, prof.trigger_rate_limit)
        got, _g = p.backend.set_exposure_gain(cam, exposure_us=e)
        row["per_camera"][cam.serial] = {"exposure_set": got}
    run = _run_triggered(p, "exposure_sweep", cams, src, fps, SWEEP_STEP_S,
                         record_pass=False)
    if run is None:
        return row, None
    stops = _stop_cams(p, cams, run["grabbers"])
    all_ok = True
    for g in run["grabbers"]:
        r = row["per_camera"][g.cam.serial]
        ids, ts = g.ids, g.ts
        r["frames"] = len(ids)
        if len(ids) > 2 and ts[-1] > ts[0]:
            rate = (ids[-1] - ids[0]) / ((ts[-1] - ts[0]) / 1e9)
            r["block_rate"] = round(rate, 3)
            r["ignored_share"] = round(max(0.0, 1 - rate / fps), 5)
        w = stops[g.cam.serial].get("witness") or {}
        r["edges"], r["exposures"] = w.get("edges"), w.get("exposures")
        r["ok"] = r.get("ignored_share") is not None and \
            r["ignored_share"] <= tol
        all_ok = all_ok and r["ok"]
    return row, all_ok


def stage_exposure_sweep(p: Probe):
    """Raise the exposure at the profile's frame rate until triggers go
    missing: a coarse grid from the profile's exposure to past the trigger
    period, then SWEEP_BISECT halvings between the longest exposure every
    camera kept up at and the shortest one any camera did not. A trigger is
    counted missing from the block-ID rate against the device clock, the
    check frame_sync applies to recordings."""
    prof = p.profile
    if prof is None or prof.camera is None:
        p.check("exposure_sweep", "stage runs", "FAIL", "needs a FLIR profile")
        return
    if prof.trigger_source != "board":
        why = ("the exposure sweep needs Panopticon's trigger board: it arms "
               "and starts the triggers once per step")
        p.check("exposure_sweep", "stage runs", "SKIP", why)
        p.not_measured("exposure_ceiling_real", why)
        return
    from gui_app.frame_sync import BLOCK_RATE_TOL
    fps = float(prof.frame_rate)
    period = 1e6 / fps
    e0 = float(prof.camera.exposure_us)
    top = SWEEP_TOP_PERIODS * period
    grid = [e0 + (top - e0) * k / (SWEEP_STEPS - 1) for k in range(SWEEP_STEPS)]
    rep = {"fps": fps, "steps": [], "per_camera": {}}
    p.report["exposure_sweep"] = rep
    cams, info = _open_for_trigger(p, "exposure_sweep")
    if cams is None:
        return
    src = p.source()
    try:
        why = src.open()
        if why is not None:
            p.check("exposure_sweep", f"{src.describe()} is usable", "FAIL", why)
            return
        lo = hi = None
        for e in grid:
            row, ok = _sweep_step(p, cams, src, fps, e, BLOCK_RATE_TOL)
            rep["steps"].append(row)
            if ok is None:
                break
            if ok:
                lo = e
            else:
                hi = e
                break
        for _k in range(SWEEP_BISECT):
            if lo is None or hi is None:
                break
            mid = 0.5 * (lo + hi)
            row, ok = _sweep_step(p, cams, src, fps, mid, BLOCK_RATE_TOL)
            rep["steps"].append(row)
            if ok is None:
                break
            if ok:
                lo = mid
            else:
                hi = mid
        for cam in cams:
            p.backend.set_exposure_gain(cam, exposure_us=e0)
            mine = [(r["exposure_us"], r["per_camera"].get(cam.serial, {}))
                    for r in rep["steps"]]
            ok_steps = [e for e, r in mine if r.get("ok")]
            bad = [e for e, r in mine if r.get("ok") is False]
            longest = max(ok_steps) if ok_steps else None
            first_bad = min(bad) if bad else None
            ceiling = info[cam.serial].get("ceiling_us")
            rep["per_camera"][cam.serial] = {
                "longest_ok_us": longest, "first_failing_us": first_bad,
                "backend_ceiling_us": ceiling}
            if longest is None:
                ans = None
            else:
                ans = f"every trigger acquired up to {longest:.0f} us"
                ans += (f"; triggers ignored from {first_bad:.0f} us"
                        if first_bad else " (the longest step tried)")
                if ceiling is not None:
                    ans += f"; the backend's ceiling is {ceiling:.0f} us"
            p.answer("exposure_ceiling_real", cam.serial, ans,
                     **rep["per_camera"][cam.serial])
            safe = ceiling is not None and longest is not None and (
                first_bad is None or 0.9 * ceiling < first_bad)
            p.check("exposure_sweep", f"{cam.serial} the backend's ceiling "
                    f"keeps 10% below the first exposure that loses triggers",
                    "PASS" if safe else "FAIL", ans or "no step completed")
    finally:
        closed = src.close()
        if src.kind == "board" and closed is not None:
            p.check("exposure_sweep", "trigger board stood down",
                    "PASS" if closed else "FAIL")
        for cam in cams:
            with contextlib.suppress(Exception):
                p.backend.close(cam)


# ============================================================ --wrap-test
def stage_wrap_test(p: Probe):
    devs = p.selected(fresh=True)
    rep = {"per_camera": {}}
    p.report["wrap_test"] = rep
    done_models = set()
    for dev in devs:
        key = (dev.model, dev.interface)
        if key in done_models:
            continue
        done_models.add(key)
        err = _init(p, dev)
        if err is not None:
            p.check("wrap_test", f"{dev.serial} initialises", "FAIL", err)
            continue
        raw = _Raw(p.api, dev.handle, dev.label)
        try:
            user_set = (p.profile.camera.flir.user_set
                        if p.profile is not None and p.profile.camera is not None
                        else "Default")
            _baseline(raw, user_set)
            rec = {"model": dev.model, "interface": dev.interface}
            for n in ("OffsetX", "OffsetY"):
                raw.write(n, 0)
            for n in ("Width", "Height"):
                rg = raw.rng(n)
                if rg and rg.get("min") is not None:
                    raw.write(n, int(rg["min"]))
            rg = raw.rng("ExposureTime")
            if rg and rg.get("min") is not None:
                raw.write("ExposureTime", float(rg["min"]))
            if raw.has("AcquisitionFrameRateEnable"):
                raw.write("AcquisitionFrameRateEnable", False)
            raw.write("StreamBufferCountMode", "Manual", "tlstream")
            raw.write("StreamBufferCountManual", 200, "tlstream")
            modes = ["Off", "On"] if raw.has("GevGVSPExtendedIDMode") else [None]
            for mode in modes:
                if mode is not None:
                    raw.write("GevGVSPExtendedIDMode", mode)
                rec[f"extended_{mode or 'native'}"] = _wrap_run(p, dev.handle)
            rep["per_camera"][dev.serial] = rec
            parts = []
            for k, v in rec.items():
                if not k.startswith("extended_"):
                    continue
                parts.append(f"{k[9:]}: {v['verdict']}")
            ans = "; ".join(parts)
            p.answer("wrap_16bit", dev.serial, ans, **rec)
            bad = any(v["verdict"].startswith("not decided")
                      for k, v in rec.items() if k.startswith("extended_"))
            p.check("wrap_test", f"{dev.serial} frame-ID wrap",
                    "WARN" if bad else "PASS", ans)
        finally:
            _deinit(p, dev)


def _wrap_run(p: Probe, h) -> dict:
    """Free-run until the frame ID wraps or passes 65535 by WRAP_PAST, or
    until WRAP_TIMEOUT_S."""
    api = p.api
    from gui_app.backends.flir import FlirTimeout
    prev = None
    tail = []
    frames = 0
    t_end = time.perf_counter() + WRAP_TIMEOUT_S
    verdict = None
    around = None
    api.begin(h)
    try:
        while time.perf_counter() < t_end:
            try:
                img = api.next_image(h, 2000)
            except FlirTimeout:
                continue
            try:
                fid = int(api.image_frame_id(img))
            finally:
                api.image_release(img)
            frames += 1
            tail.append(fid)
            tail = tail[-8:]
            if prev is not None and fid < prev:
                around = list(tail)
                verdict = f"wraps {prev} -> {fid}"
                if prev >= 65535:
                    verdict += " (skips 0)" if fid == 1 else ""
                break
            if fid >= 65535 + WRAP_PAST:
                verdict = f"no wrap: IDs passed 65535 (reached {fid})"
                around = list(tail)
                break
            prev = fid
    finally:
        api.end(h)
    return {"frames": frames, "verdict": verdict or
            f"not decided: {frames} frames, last ID {prev}", "ids": around}


# =============================================================== --pyspin
def _ps_node(ps, nodemap, name, kind):
    ptr = {"enum": "CEnumerationPtr", "int": "CIntegerPtr",
           "float": "CFloatPtr", "bool": "CBooleanPtr",
           "string": "CStringPtr"}[kind]
    return getattr(ps, ptr)(nodemap.GetNode(name))


def _ps_set(ps, nodemap, name, value) -> bool:
    try:
        if isinstance(value, bool):
            n = _ps_node(ps, nodemap, name, "bool")
        elif isinstance(value, int):
            n = _ps_node(ps, nodemap, name, "int")
        elif isinstance(value, float):
            n = _ps_node(ps, nodemap, name, "float")
        else:
            n = _ps_node(ps, nodemap, name, "enum")
            if not (ps.IsAvailable(n) and ps.IsWritable(n)):
                return False
            e = n.GetEntryByName(value)
            if not (ps.IsAvailable(e) and ps.IsReadable(e)):
                return False
            n.SetIntValue(e.GetValue())
            return True
        if not (ps.IsAvailable(n) and ps.IsWritable(n)):
            return False
        if isinstance(value, float):
            value = min(max(value, n.GetMin()), n.GetMax())
        n.SetValue(value)
        return True
    except Exception:
        return False


def _ps_get_string(ps, nodemap, name):
    try:
        n = _ps_node(ps, nodemap, name, "string")
        return n.GetValue() if ps.IsAvailable(n) and ps.IsReadable(n) else None
    except Exception:
        return None


def stage_pyspin(p: Probe):
    sdk = p.report["sdk"]
    try:
        ps = importlib.import_module("PySpin")
    except Exception as e:
        sdk["pyspin_import_error"] = _err(e)
        p.check("pyspin", "PySpin is installed", "SKIP",
                "PySpin is not installed; this optional stage measures it "
                "only when it is")
        p.not_measured("pyspin_getndarray", "PySpin is not installed")
        return
    if p.fake and not getattr(ps, "PANOPTICON_FAKE", False):
        p.check("pyspin", "stage runs", "SKIP",
                "--fake runs simulated cameras, and this PySpin would open "
                "the real ones")
        p.not_measured("pyspin_getndarray", "skipped under --fake")
        return
    rep = {}
    p.report["pyspin"] = rep
    import numpy as np
    p.release_devices()
    system = ps.System.GetInstance()
    try:
        v = system.GetLibraryVersion()
        sdk["pyspin_version"] = ".".join(str(getattr(v, k, "?")) for k in
                                         ("major", "minor", "type", "build"))
    except Exception as e:
        sdk["pyspin_version"] = None
        sdk["pyspin_version_error"] = _err(e)
    cams = system.GetCameras()
    want = [str(s) for s in (getattr(p.profile, "camera_serials", None) or [])]
    cam = None
    try:
        for i in range(cams.GetSize()):
            c = cams.GetByIndex(i)
            serial = _ps_get_string(ps, c.GetTLDeviceNodeMap(),
                                    "DeviceSerialNumber")
            if not want or serial in want:
                cam, rep["serial"] = c, serial
                break
            del c
        if cam is None:
            p.check("pyspin", "a camera", "FAIL", "PySpin lists no camera")
            return
        cam.Init()
        try:
            nm = cam.GetNodeMap()
            snm = cam.GetTLStreamNodeMap()
            for sel in ("FrameStart", "AcquisitionStart", "FrameBurstStart"):
                if _ps_set(ps, nm, "TriggerSelector", sel):
                    _ps_set(ps, nm, "TriggerMode", "Off")
            _ps_set(ps, nm, "AcquisitionMode", "Continuous")
            _ps_set(ps, snm, "StreamBufferCountMode", "Manual")
            _ps_set(ps, snm, "StreamBufferCountManual", SELFTEST_BUFFERS)
            _ps_set(ps, nm, "AcquisitionFrameRateEnable", True)
            _ps_set(ps, nm, "AcquisitionFrameRate", SELFTEST_FPS)
            shares, own, base, ptrs, n = [], [], set(), set(), 0
            cam.BeginAcquisition()
            try:
                for _k in range(2 * SELFTEST_BUFFERS):
                    img = cam.GetNextImage(2000)
                    try:
                        if img.IsIncomplete():
                            continue
                        a = img.GetNDArray()
                        b = img.GetNDArray()
                        shares.append(bool(np.shares_memory(a, b)))
                        own.append(bool(a.flags.owndata))
                        base.add(type(a.base).__name__)
                        ptrs.add(int(a.ctypes.data))
                        n += 1
                        del a, b
                    finally:
                        img.Release()
            finally:
                cam.EndAcquisition()
            zc = {"frames": n, "buffers": SELFTEST_BUFFERS,
                  "shares_memory": all(shares) if shares else None,
                  "owndata": any(own) if own else None,
                  "base_type": sorted(base), "distinct_ptrs": len(ptrs)}
            rep["zero_copy"] = zc
            _ps_set(ps, nm, "AcquisitionFrameRate", GIL_FPS)
            meter = _GilMeter()
            try:
                cam.BeginAcquisition()
                try:
                    def wait(stop):
                        k = 0
                        while not stop.is_set():
                            try:
                                im = cam.GetNextImage(2000)
                            except Exception as e:
                                # A timeout (-1011) retries at once; any
                                # other error waits, so the meter does not
                                # measure a spinning loop.
                                if getattr(e, "errorcode", None) != -1011:
                                    time.sleep(0.05)
                                continue
                            im.Release()
                            k += 1
                        return k
                    rep["gil"] = meter.held(wait, GIL_WINDOW_S)
                finally:
                    cam.EndAcquisition()
            finally:
                meter.close()
        finally:
            cam.DeInit()
    finally:
        del cam
        cams.Clear()
        try:
            system.ReleaseInstance()
        except Exception as e:
            rep["release_error"] = _err(e)
    zc = rep.get("zero_copy", {})
    view = zc.get("shares_memory") and not zc.get("owndata")
    frac = rep.get("gil", {}).get("held_fraction")
    held = ("not measured" if frac is None else
            f"held {frac:.0%} of the time")
    ans = (("GetNDArray is a view of the image buffer" if view else
            "GetNDArray copies") + f" ({zc.get('distinct_ptrs')} addresses over "
           f"{zc.get('frames')} frames); during a PySpin wait the GIL is "
           f"{held}")
    rep["pyspin_wait_gil_held_fraction"] = frac
    p.answer("pyspin_getndarray", "host", ans, measured_on=rep.get("serial"),
             zero_copy=zc, gil=rep.get("gil"))
    p.check("pyspin", "PySpin measured", "INFO", ans)


# ============================================================== --collect
def _collect_plan(p: Probe, root: Path) -> dict:
    """What the zip holds: (archive name, source path) pairs, and what was
    looked for and not found."""
    files, missing = [], []
    names = set(COLLECT_NAMES)
    found = {n: 0 for n in COLLECT_NAMES}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name in names:
            files.append((f"session/{path.relative_to(root).as_posix()}", path))
            found[path.name] += 1
    missing += [n for n, k in found.items() if not k]
    logs = []
    for meta in root.rglob("session_metadata.json"):
        try:
            logf = json.loads(meta.read_text(encoding="utf-8")).get(
                "log", {}).get("file")
        except (OSError, ValueError, AttributeError):
            logf = None
        if logf and Path(logf).is_file():
            logs.append(Path(logf))
    log_dir = Path(p.args.log_dir)
    main_logs = sorted(q for q in log_dir.glob("panopticon_*.log")
                       if not re.search(r"_w\d+\.log$", q.name))
    if main_logs:
        logs.append(main_logs[-1])
    worker = []
    for q in list(logs):
        worker += sorted(log_dir.glob(q.stem + "_w*.log"))
        worker += sorted(q.parent.glob(q.stem + "_w*.log"))
    seen = set()
    for q in logs + worker:
        key = str(q.resolve())
        if key not in seen:
            seen.add(key)
            files.append((f"logs/{q.name}", q))
    if not logs:
        missing.append(f"a GUI log (logs/panopticon_*.log in {log_dir})")
    probes = sorted(p.out_dir.glob("flir_probe_*.json"))[-COLLECT_PROBE_JSONS:]
    for q in probes:
        files.append((f"probe/{q.name}", q))
        lq = q.with_suffix(".log")
        if lq.is_file():
            files.append((f"probe/{lq.name}", lq))
    return {"files": files, "missing": missing}


def stage_collect(p: Probe):
    root = Path(p.args.collect)
    rep = {"session_dir": str(root), "zip": None, "files": [], "missing": []}
    p.report["collect"] = rep
    if not root.is_dir():
        p.check("collect", "session folder exists", "FAIL", str(root))
        return
    plan = _collect_plan(p, root)
    zpath = p.out_dir / (p.json_path.stem.replace("flir_probe_",
                                                  "flir_collect_", 1) + ".zip")
    rep["zip"] = str(zpath)
    rep["files"] = [{"name": n, "bytes": q.stat().st_size} for n, q in
                    plan["files"]]
    rep["missing"] = plan["missing"]
    p.save()
    _write_zip(zpath, plan["files"], rep)
    got = {n.rsplit("/", 1)[-1] for n, _q in plan["files"]}
    if "session_metadata.json" not in got or "blockids.npy" not in got:
        p.check("collect", "the session's metadata and block IDs are in the "
                "zip", "FAIL", "no session_metadata.json or blockids.npy under "
                f"{root}; point --collect at a recording's folder")
    else:
        p.check("collect", "the session's metadata and block IDs are in the "
                "zip", "PASS", f"{len(plan['files'])} files, {zpath.name}")
    if plan["missing"]:
        p.check("collect", "every file looked for was found", "WARN",
                "missing: " + ", ".join(plan["missing"]))
    print(f"[probe] collected {len(plan['files'])} files into {zpath}. Send "
          f"this zip; it holds no video.", flush=True)


def _write_zip(zpath: Path, files, rep: dict):
    tmp = zpath.with_suffix(".zip.tmp")
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as z:
        lines = [f"Panopticon probe_flir.py --collect, {_now_iso()}",
                 f"session folder: {rep['session_dir']}", ""]
        for name, q in files:
            z.write(q, name)
            lines.append(f"{name}  {q.stat().st_size} bytes  "
                         f"sha256 {_sha256(q)}")
        lines += ["", "not found: " + (", ".join(rep["missing"]) or "nothing")]
        z.writestr("MANIFEST.txt", "\n".join(lines) + "\n")
    os.replace(tmp, zpath)


def _refresh_zip_json(p: Probe):
    """Put this run's final JSON and its log so far into the zip, in place
    of the copies taken while the collect stage ran."""
    rep = p.report["collect"]
    zpath = Path(rep["zip"])
    if not zpath.is_file():
        return
    logging_setup.flush()
    fresh = {f"probe/{q.name}": q for q in (p.json_path, p.log_path)
             if q.is_file()}
    tmp = zpath.with_suffix(".zip.tmp")
    with zipfile.ZipFile(zpath) as src, zipfile.ZipFile(
            tmp, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename not in fresh:
                dst.writestr(item, src.read(item.filename))
        for name, q in fresh.items():
            dst.write(q, name)
    os.replace(tmp, zpath)


# ================================================================= report
_TOP_KEYS = {"schema": str, "created": str, "fake": bool, "argv": list,
             "host": dict, "sdk": dict, "profile": dict, "stages": dict,
             "cameras": list, "selftest": dict, "unknowns": dict,
             "checks": list, "errors": list}


def validate_report(report) -> list:
    """What makes `report` (as JSON data) not a panopticon.flir_probe/1
    document; [] when it is one."""
    out = []
    if not isinstance(report, dict):
        return ["the report is not an object"]
    if report.get("schema") != SCHEMA:
        out.append(f"schema is {report.get('schema')!r}, not {SCHEMA!r}")
    for key, tp in _TOP_KEYS.items():
        if key not in report:
            out.append(f"missing {key}")
        elif not isinstance(report[key], tp):
            out.append(f"{key} is {type(report[key]).__name__}, not "
                       f"{tp.__name__}")
    for key in ("find_line", "triggered", "exposure_sweep", "wrap_test",
                "pyspin", "collect"):
        if key not in report:
            out.append(f"missing {key}")
        elif report[key] is not None and not isinstance(report[key], dict):
            out.append(f"{key} is neither null nor an object")
    for name, st in (report.get("stages") or {}).items():
        if name not in STAGES:
            out.append(f"unknown stage {name!r}")
        if not isinstance(st, dict) or st.get("status") not in STATUSES:
            out.append(f"stage {name} has no valid status")
    for i, c in enumerate(report.get("checks") or []):
        if not isinstance(c, dict) or not {"stage", "check", "status",
                                           "detail"} <= set(c):
            out.append(f"check {i} lacks stage/check/status/detail")
        elif c["status"] not in STATUSES:
            out.append(f"check {i} has status {c['status']!r}")
    for i, cam in enumerate(report.get("cameras") or []):
        if not isinstance(cam, dict) or not {"serial", "model",
                                             "interface"} <= set(cam):
            out.append(f"camera {i} lacks serial/model/interface")
    unknowns = report.get("unknowns") or {}
    for uid in UNKNOWNS:
        u = unknowns.get(uid)
        if not isinstance(u, dict):
            out.append(f"unknown {uid} missing")
            continue
        for key in ("question", "where", "stage", "answers", "settled",
                    "not_measured"):
            if key not in u:
                out.append(f"unknown {uid} lacks {key}")
        answers = u.get("answers")
        if not isinstance(answers, dict):
            out.append(f"unknown {uid} answers is not an object")
        elif not answers and not u.get("not_measured"):
            out.append(f"unknown {uid} has no answer and no reason")
        else:
            for who, a in answers.items():
                if not isinstance(a, dict) or "answer" not in a:
                    out.append(f"unknown {uid} answer for {who} lacks answer")
    return out


def print_table(report: dict, json_path: Path):
    checks = report["checks"]
    width = max([len(c["check"]) for c in checks] + [20])
    width = min(width, 64)
    print("", flush=True)
    print("===== probe_flir results =====", flush=True)
    print(f"{'stage':<15}{'check':<{width + 2}}result  detail", flush=True)
    for c in checks:
        detail = c["detail"].replace("\n", " ")
        if len(detail) > 90:
            detail = detail[:87] + "..."
        print(f"{c['stage']:<15}{c['check'][:width]:<{width + 2}}"
              f"{c['status']:<8}{detail}", flush=True)
    print("", flush=True)
    print("===== what the cameras answered =====", flush=True)
    for uid, u in report["unknowns"].items():
        if u["answers"]:
            for who, a in u["answers"].items():
                text = a.get("answer")
                if text is None:
                    text = "not measured: " + str(a.get("why_not") or "")
                print(f"{uid} [{who}]: {str(text)[:150]}", flush=True)
        else:
            print(f"{uid}: not measured ({u['not_measured']})", flush=True)
    fails = sum(1 for c in checks if c["status"] == "FAIL")
    warns = sum(1 for c in checks if c["status"] == "WARN")
    print("", flush=True)
    print(f"Overall: {'FAIL' if fails else 'PASS'} ({fails} failed, {warns} "
          f"warnings). JSON: {json_path}", flush=True)


# =================================================================== main
def _parse(argv):
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true",
                    help="enumerate the cameras and dump what they report")
    ap.add_argument("--find-line", action="store_true",
                    help="find the input line each camera's trigger wire is on")
    ap.add_argument("--selftest", action="store_true",
                    help="free-run self-tests: frame ID, clock, zero-copy, "
                         "chunks, GIL")
    ap.add_argument("--triggered", nargs="?", type=float, const=None,
                    default=argparse.SUPPRESS, metavar="SECONDS",
                    help=f"triggered capture through the FLIR backend "
                         f"(default {TRIGGERED_DEFAULT_S:g} s)")
    ap.add_argument("--all", action="store_true",
                    help="--list, --find-line, --selftest and --triggered")
    ap.add_argument("--exposure-sweep", action="store_true",
                    help="find the longest exposure that acquires every "
                         "trigger at frame_rate")
    ap.add_argument("--wrap-test", action="store_true",
                    help="free-run past frame ID 65535 once per camera model")
    ap.add_argument("--pyspin", action="store_true",
                    help="measure PySpin's GetNDArray and GIL, when installed")
    ap.add_argument("--collect", metavar="SESSION_DIR",
                    help="zip a session folder's logs, metadata and block IDs "
                         "(no video) with the probe JSONs")
    ap.add_argument("--no-counter-check", action="store_true",
                    help="skip the counter checks after --triggered")
    ap.add_argument("--profile", help="profile name or path to a profile file")
    ap.add_argument("--fake", action="store_true",
                    help="simulated cameras and trigger board; no SDK, no "
                         "serial port")
    ap.add_argument("--fake-interface", choices=("USB3", "GigE"),
                    default="USB3", help="--fake: the simulated interface")
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR),
                    help="where the JSON, log and zip go")
    ap.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR),
                    help="where the GUI's logs are, for --collect")
    probe_guard.add_force_argument(ap)
    args = ap.parse_args(argv)
    given = hasattr(args, "triggered")
    if not given:
        args.triggered = None
    elif args.triggered is None:
        args.triggered = FAKE_TRIGGERED_S if args.fake else TRIGGERED_DEFAULT_S
    stages = (args.list or args.find_line or args.selftest or given
              or args.exposure_sweep or args.wrap_test or args.pyspin)
    if args.all or (args.fake and not stages and not args.collect):
        args.list = args.find_line = args.selftest = True
        if args.triggered is None:
            args.triggered = FAKE_TRIGGERED_S if args.fake else TRIGGERED_DEFAULT_S
    elif not stages and not args.collect:
        args.list = True
    args.argv = list(sys.argv[1:] if argv is None else argv)
    return args


def _collect_only(args) -> bool:
    return bool(args.collect) and not any(
        (args.list, args.find_line, args.selftest, args.triggered is not None,
         args.exposure_sweep, args.wrap_test, args.pyspin))


def main(argv=None) -> int:
    args = _parse(argv)
    if not _collect_only(args):
        # Every stage but --collect opens cameras or the trigger board.
        probe_guard.refuse_if_panopticon_running(force=args.force)
    # PyQt5 loads before the Spinnaker library, in the GUI's order, because
    # the SDK's folder also holds Qt5 DLLs.
    with contextlib.suppress(Exception):
        import PyQt5.QtCore  # noqa: F401
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = _unique_base(out_dir, f"flir_probe_{_host_tag()}_{stamp}")
    installed_here = False
    if not logging_setup.installed():
        installed_here = logging_setup.install(out_dir / f"{base}.log") is not None
    level = logging_setup.level()
    try:
        print(f"[probe] probe_flir.py {' '.join(args.argv)}", flush=True)
        probe = Probe(args, out_dir, base)
        try:
            return probe.run()
        except KeyboardInterrupt:
            probe.check("probe", "run", "FAIL", "interrupted")
            probe.save()
            return 130
    finally:
        logging_setup.set_level(level)
        if installed_here:
            logging_setup.shutdown()


if __name__ == "__main__":
    sys.exit(main())
