"""ProcessCameraManager: the CameraManager surface over capture worker processes.

With `capture_processes: N` (N > 0) the cameras are captured in N worker
processes (gui_app.mp.worker) instead of in the GUI's or probe's own
process. This class stands where CameraManager stands: the GUI and the
probes call the same methods, in the same order, and get the same results
(`rig_setup.make_manager` picks the class from the profile).

The parent holds no camera handle and no encoder. It enumerates the
cameras (enumeration opens nothing), resolves cam1..camN exactly as
CameraManager.open_all does, and deals them to the workers in contiguous
groups by camera index. Each worker then runs the one configuration path
(`rig_setup.apply_profile_to_manager`, then `open_all` on its share).

What runs in the parent:

- the coordinator thread, which polls the cross-process ledger every
  POLL_S during an acquisition and drives the one FrameSyncCoordinator that
  decides every trigger for every camera;
- the supervisor thread, which routes each worker's replies and detects a
  worker's exit from its process sentinel; a worker that exits during an
  acquisition has its cameras retired, so the others keep recording;
- the log reader thread, which prints each worker's lines unchanged (their
  [grabN] and [camN] prefixes carry the rig-wide index);
- the NVENC session broker: an acquisition is refused when its cameras need
  more sessions than `session_cap`, when a caller has set it, and a worker's
  grant goes back to zero when it stops, is cancelled or exits.

Every request to a worker has a bound; the ones a GUI thread may wait on
are the ones CameraManager already makes callers run off the Qt main
thread (open, start, stop, close).
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import threading
import time
from multiprocessing.connection import wait as _mp_wait
from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal

from gui_app import sync_encode
from gui_app.backends import load_backend
from gui_app.camera_manager import (FPS_DECAY_S, STOP_FORCED_EXIT_S,
                                    STOP_NORMAL_EXIT_S, AcquisitionStartRefused,
                                    AcquisitionStopIncomplete, CameraManager,
                                    CameraOpenError, resolve_device_order)
from gui_app.grab_thread import SOURCE_SILENT_S
from gui_app.mp import shm
from gui_app.mp import worker as wk
from gui_app.mp.ledger import create_shared
from gui_app.mp.winjob import WorkerJob

#: Seconds between the coordinator's polls of the ledger. A frame waits in
#: its worker for at most about this long after the last camera announced
#: its trigger, which the worker's NV12 ring absorbs many times over.
POLL_S = 0.002
#: Seconds between the coordinator thread's reads of the workers' status:
#: camera silence, the rig-wide source-silence flag, and hang detection.
STATUS_READ_S = 0.05
#: Seconds between the periodic cross-camera lag reports in the log.
LAG_REPORT_S = 5.0
#: A worker whose heartbeat is older than this while triggers run, and whose
#: cameras' frontiers have not moved for as long, has its cameras retired.
HANG_S = 5.0
#: Bounds on each request to a worker, in seconds.
HELLO_TIMEOUT_S = 20.0
OPEN_TIMEOUT_S = 60.0
ARM_TIMEOUT_S = 45.0
READY_MARGIN_S = 15.0
SIGNAL_TIMEOUT_S = 5.0
VIEW_TIMEOUT_S = 5.0
THERMALS_TIMEOUT_S = 5.0
RESUME_TIMEOUT_S = 20.0
ABANDON_TIMEOUT_S = 8.0
CLOSE_TIMEOUT_S = 10.0
#: How long stop waits for every camera to reach end of stream before it
#: retires the ones that have not: a worker's grab loops exit within
#: STOP_NORMAL_EXIT_S of the trigger stop, and within STOP_FORCED_EXIT_S
#: more when told to stop outright.
STOP_EOS_S = STOP_NORMAL_EXIT_S + STOP_FORCED_EXIT_S + 5.0
#: How long stop waits for a worker's results once the ledger is flushed:
#: its encoders drain (a full queue is about a second of encoding, with a
#: bound of DRAIN_SENTINEL_TIMEOUT_S + DRAIN_JOIN_TIMEOUT_S) and it
#: reconciles every camera.
STOP_RESULTS_S = (sync_encode.DRAIN_SENTINEL_TIMEOUT_S
                  + sync_encode.DRAIN_JOIN_TIMEOUT_S + 60.0)


def split_contiguous(n: int, groups: int) -> list:
    """Camera indices 0..n-1 in `groups` contiguous groups, the first ones
    one camera larger when n does not divide evenly; empty groups are left
    out. split_contiguous(9, 3) is [[0,1,2],[3,4,5],[6,7,8]] and
    split_contiguous(9, 4) is [[0,1,2],[3,4],[5,6],[7,8]]."""
    n, groups = int(n), max(1, int(groups))
    base, extra = divmod(n, groups)
    out, start = [], 0
    for g in range(groups):
        size = base + (1 if g < extra else 0)
        if size:
            out.append(list(range(start, start + size)))
        start += size
    return out


def windowless_executable() -> str | None:
    """The interpreter a worker should run when this process runs windowless.

    In a virtual environment, multiprocessing starts the base interpreter
    in place of the venv's (bpo-35797). Under the venv's pythonw.exe that
    base interpreter is python.exe, a console program, and a worker started
    from a parent with no console can get a console window of its own,
    whose closing kills the worker. Returns the base pythonw.exe to use
    instead (with __PYVENV_LAUNCHER__ set so the worker still runs in the
    venv, see use_executable), or None when nothing needs changing.
    """
    exe = Path(sys.executable)
    if sys.platform != "win32" or exe.name.lower() != "pythonw.exe":
        return None
    base = Path(getattr(sys, "_base_executable", sys.executable))
    pyw = base.with_name("pythonw.exe")
    if not pyw.exists() or pyw.resolve() == exe.resolve():
        return None
    return str(pyw)


def use_executable(path: str) -> None:
    """Start spawned workers with `path` and keep them in this process's venv.

    multiprocessing then launches `path` itself, and __PYVENV_LAUNCHER__
    (inherited by the child) points the interpreter at the venv, as the
    venv's own launcher would.
    """
    multiprocessing.get_context("spawn").set_executable(path)
    os.environ["__PYVENV_LAUNCHER__"] = sys.executable


class WorkerFailed(RuntimeError):
    """A worker refused a request, did not answer in time, or exited."""

    def __init__(self, message: str, kind: str = "error", data=None):
        super().__init__(message)
        self.kind = kind
        self.data = data


class _Call:
    __slots__ = ("event", "reply", "op")

    def __init__(self, op):
        self.op = op
        self.event = threading.Event()
        self.reply = None


class _Worker:
    """The parent's handle on one worker process."""

    def __init__(self, wid: int, cams: list, serials: list):
        self.wid = wid
        self.cams = list(cams)
        self.serials = list(serials)
        self.process = None
        self.conn = None
        self.log_conn = None
        self.pid = None
        self.hello = threading.Event()
        self.status = None
        self._status_seg = None
        self.preview = None
        self.frames = None
        self._image_segs: list = []
        self._calls: dict = {}
        self._next = 0
        self._lock = threading.Lock()
        self.dead = False
        self.exitcode = None
        self.closing = False
        self.fatal = None
        self.armed = False
        self.ready_info = None
        self.hung = False

    @property
    def names(self) -> str:
        return ", ".join(f"cam{g + 1}" for g in self.cams)

    def call(self, op: str, **payload) -> _Call:
        c = _Call(op)
        with self._lock:
            self._next += 1
            rid = self._next
            if self.dead:
                c.reply = {"ok": False, "kind": "exited",
                           "error": self._exit_text()}
                c.event.set()
                return c
            self._calls[rid] = c
            try:
                self.conn.send({"id": rid, "op": op, **payload})
            except Exception as e:
                self._calls.pop(rid, None)
                c.reply = {"ok": False, "kind": "exited",
                           "error": f"the request could not be sent "
                                    f"({type(e).__name__}: {e})"}
                c.event.set()
        return c

    def resolve(self, msg: dict) -> None:
        with self._lock:
            c = self._calls.pop(msg.get("id"), None)
        if c is not None:
            c.reply = msg
            c.event.set()

    def fail_all(self, text: str) -> None:
        with self._lock:
            calls, self._calls = self._calls, {}
        for c in calls.values():
            c.reply = {"ok": False, "kind": "exited", "error": text}
            c.event.set()

    def _exit_text(self) -> str:
        return (f"the capture process for {self.names} exited"
                + ("" if self.exitcode is None else
                   f" with code {self.exitcode}"))

    def close_segments(self) -> None:
        for seg in (self.preview, self.frames, self.status):
            if seg is not None:
                seg.close()
        self.preview = self.frames = self.status = None
        for s in self._image_segs + [self._status_seg]:
            if s is None:
                continue
            try:
                s.close()
            except BufferError:
                pass
        self._image_segs = []
        self._status_seg = None


def _await(calls: dict, timeout_s: float) -> dict:
    """{worker: (True, result) | (False, WorkerFailed)} once every call has
    a reply or `timeout_s` has passed for all of them together."""
    deadline = time.monotonic() + max(0.0, timeout_s)
    out = {}
    for w, c in calls.items():
        c.event.wait(max(0.0, deadline - time.monotonic()))
        r = c.reply
        if r is None:
            out[w] = (False, WorkerFailed(
                f"the capture process for {w.names} did not answer "
                f"'{c.op}' within {timeout_s:g} s", kind="timeout"))
        elif r.get("ok"):
            out[w] = (True, r.get("result"))
        else:
            out[w] = (False, WorkerFailed(str(r.get("error")),
                                          kind=r.get("kind", "error"),
                                          data=r.get("data")))
    return out


class _CameraView:
    """One camera of a worker, read the way a probe reads a grab thread.

    During an acquisition the counters come from the worker's status
    segment; after stop they are the figures the worker returned, frozen,
    because the thread they described has ended.
    """

    def __init__(self, mgr: "ProcessCameraManager", g: int):
        self._mgr = mgr
        self._cam_index = g
        self.final: dict | None = None
        self.pin_result = None

    def _live(self, field: str, default=0):
        return self._mgr._status_get(self._cam_index, field, default)

    def _pick(self, key: str, field: str, default=0):
        if self.final is not None and key in self.final:
            return self.final[key]
        return self._live(field, default)

    @property
    def frame_count(self) -> int:
        return int(self._pick("frame_count", "frame_count"))

    @property
    def drops(self) -> int:
        return int(self._pick("drops", "drops_local"))

    @property
    def failed_grabs(self) -> int:
        return int(self._pick("failed_grabs", "failed_grabs"))

    @property
    def rearms(self) -> int:
        return int(self._pick("rearms", "rearms"))

    @property
    def ring_full_drops(self) -> int:
        return int(self._pick("ring_full_drops", "ring_refused"))

    @property
    def frames_before_barrier(self) -> int:
        return int(self._pick("frames_before_barrier", "early_frame"))

    @property
    def frames_retrieved(self) -> int:
        if self.final is not None:
            return int(self.final.get("frames_retrieved", 0))
        return self.frame_count

    @property
    def current_fps(self) -> float:
        v = self._live("current_fps", 0.0)
        return 0.0 if v != v else float(v)

    @property
    def delivery_lag_s(self) -> float:
        v = self._live("delivery_lag_s", 0.0)
        return 0.0 if v != v else float(v)

    @property
    def retrieve_loop_exited(self) -> bool:
        if self.final is not None:
            return True
        return bool(self._live("retrieve_loop_exited", 0))

    @property
    def retired_reason(self):
        why = self._mgr._retired_reason(self._cam_index)
        if why is None and self.final is not None:
            why = self.final.get("retired_reason")
        return why

    @property
    def desynced(self) -> bool:
        return self.retired_reason is not None

    @property
    def source_down_stalls(self) -> int:
        return int((self.final or {}).get("source_down_stalls", 0))

    @property
    def source_down_since(self):
        return (self.final or {}).get("source_down_since")

    @property
    def stream_stats_at_stop(self):
        return (self.final or {}).get("stream_stats_at_stop")

    def failed_grabs_per_second(self) -> list:
        return [tuple(x) for x in
                (self.final or {}).get("failed_grabs_by_second", [])]

    def isRunning(self) -> bool:
        return self.final is None and self._mgr._worker_alive(self._cam_index)

    def seconds_since_frame(self, now=None) -> float:
        return self._mgr._silence_of(self._cam_index, now)


class _RouterView:
    """The parts of SyncEncodeRouter a probe reads, over the parent's
    coordinator: `_coord` is the FrameSyncCoordinator that decides every
    trigger, and dropped_full / backlog_peak are the workers' totals once
    the acquisition has stopped."""

    def __init__(self, coordinator, max_lag: int):
        self._ledger_coordinator = coordinator
        self._coord = coordinator.core
        self.max_lag = max_lag
        self.dropped_full = 0
        self.backlog_peak = 0
        self.warnings: list = []

    def lag_report(self) -> str:
        return self._coord.lag_report()

    def lag_frames(self) -> list:
        return self._coord.lag_frames()

    def pending(self) -> int:
        return self._coord.pending_depth()


class ProcessCameraManager(QObject):
    error = pyqtSignal(str)

    #: The same thread-placement flags CameraManager carries; they are sent
    #: to every worker, which applies them to its own manager.
    pin_capture_threads = False
    pin_encoder_threads = False
    encoder_pcores = False
    #: Encoder factory for the workers: None (the process default), a
    #: module-level function, or a gui_app.mp.worker.FactorySpec.
    encoder_factory = None
    #: NVENC sessions the driver allows, as the launch probe measured, or
    #: None when unknown. A start whose cameras need more is refused.
    session_cap = None

    # CameraManager methods whose code reads only attributes this class
    # also has, used as they are rather than restated.
    _settings_source = CameraManager._settings_source
    geometry_mismatch = CameraManager.geometry_mismatch
    pinning_report = CameraManager.pinning_report
    _source_silence_warnings = CameraManager._source_silence_warnings

    def __init__(self, profile, backend: str | None = None,
                 log_dir: Path | None = None):
        super().__init__()
        self._profile = profile
        self.capture_processes = int(getattr(profile, "capture_processes", 0))
        if self.capture_processes < 1:
            raise ValueError("ProcessCameraManager needs capture_processes "
                             "of at least 1; use CameraManager for 0")
        self._backend_name = backend or getattr(profile, "camera_backend",
                                                "basler") or "basler"
        self._backend_obj = None
        self._log_dir = Path(log_dir) if log_dir is not None else None
        self._stamp = time.strftime("%Y%m%d_%H%M%S")
        self._workers: list = []
        self._where: dict = {}
        self._serials: list = []
        self._geometry = None
        self._camera_info: tuple = ()
        self._uses_camera_block = False
        self._keep_full = False
        self._job = WorkerJob()
        self._gate = None
        self._rig_seg = self._rig = None
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._supervisor = None
        self._log_reader = None
        self._grab_threads: list = []
        self._router = None
        self._ledger_seg = None
        self._coordinator = None
        self._coord_thread = None
        self._coord_stop = threading.Event()
        self._triggers_running = False
        self._board_started_t = None
        self._marked = False
        self._barrier = {}
        self._activity: dict = {}
        self._grants: dict = {}
        self._max_lag = 0
        self._fps = 0
        self._snap_base: dict = {}
        self.last_open_error = None
        self.last_warnings: list = []
        self.last_stream_stats: list = []
        self.last_results: list = []
        self.last_retired: dict = {}
        self.last_encoder_failures: list = []
        #: Encoder records the workers' factories returned (see
        #: FactorySpec.records), after the last stop.
        self.last_encoder_records: list = []

    # -- backend (enumeration only) ----------------------------------------------

    @property
    def _backend(self):
        if self._backend_obj is None:
            self._backend_obj = load_backend(self._backend_name)
        return self._backend_obj

    @_backend.setter
    def _backend(self, backend) -> None:
        self._backend_obj = backend
        self._backend_name = getattr(backend, "name", self._backend_name)

    @property
    def backend_name(self) -> str:
        return self._backend_name

    @property
    def backend_loaded(self) -> bool:
        return self._backend_obj is not None

    # -- read-only state ---------------------------------------------------------------

    @property
    def num_cameras(self) -> int:
        return len(self._serials)

    @property
    def geometry(self):
        return self._geometry

    @property
    def camera_info(self) -> list:
        return [dict(d) for d in self._camera_info]

    def workers_info(self) -> list:
        """[{"worker", "pid", "cameras"}] for every worker, for workers.json:
        tools that attribute CPU time or stack dumps need each camera's
        process."""
        return [{"worker": w.wid, "pid": w.pid,
                 "cameras": [f"cam{g + 1}" for g in w.cams]}
                for w in self._workers]

    def sessions_granted(self) -> int:
        """NVENC sessions the broker has granted to workers now."""
        return sum(self._grants.values())

    def _loc(self, g: int):
        return self._where.get(g, (None, None))

    def _status_get(self, g: int, field: str, default=0):
        w, k = self._loc(g)
        if w is None or w.status is None:
            return default
        try:
            return w.status.get(k, field)
        except Exception:
            return default

    def _worker_alive(self, g: int) -> bool:
        w, _k = self._loc(g)
        return bool(w is not None and not w.dead)

    def _retired_reason(self, g: int):
        """Why camera g was retired in the current (or last) acquisition,
        from the coordinator that decided it, or None."""
        coord = self._coordinator
        core = coord.core if coord is not None else self._last_core
        if core is None:
            return None
        for cam, reason in core.retired_reasons:
            if cam == g:
                return reason
        return None

    _last_core = None

    def _slot(self, g: int, which: str):
        w, k = self._loc(g)
        if w is None:
            return None
        seg = w.preview if which == "preview" else w.frames
        if seg is None:
            return None
        try:
            if which == "preview":
                return seg.read(k)
            return seg.read(k, which)
        except Exception:
            return None

    @property
    def latest_frames(self) -> list:
        out = []
        for g in range(self.num_cameras):
            r = self._slot(g, "preview")
            out.append(None if r is None else r.data)
        return out

    @property
    def latest_full_frames(self) -> list:
        return [None if p is None else p[0]
                for p in self.latest_full_frames_with_bids()]

    def latest_full_frames_with_bids(self) -> list:
        """Per camera, (full-resolution frame, block ID) of one frame, or
        None; the worker published the pair in one seqlock slot."""
        out = []
        for g in range(self.num_cameras):
            r = self._slot(g, "full") if self._keep_full else None
            out.append(None if r is None else
                       (r.data, None if r.bid < 0 else int(r.bid)))
        return out

    @property
    def snapshots(self) -> list:
        out = []
        for g in range(self.num_cameras):
            base = self._snap_base.get(g)
            seq = self._status_get(g, "snapshot_seq", 0)
            if base is None or seq <= base:
                out.append(None)
                continue
            r = self._slot(g, "snapshot")
            out.append(None if r is None else r.data)
        return out

    @property
    def current_fps(self) -> list:
        now = time.perf_counter()
        out = []
        for g in range(self.num_cameras):
            fps = self._status_get(g, "current_fps", 0.0)
            fps = 0.0 if fps != fps else float(fps)
            if self._idle_s(g, now) > FPS_DECAY_S:
                fps = 0.0
            out.append(fps)
        return out

    @property
    def delivery_lags(self) -> list:
        out = []
        for g in range(self.num_cameras):
            v = self._status_get(g, "delivery_lag_s", 0.0)
            out.append(0.0 if v != v else float(v))
        return out

    @property
    def frame_counts(self) -> list:
        return [int(self._status_get(g, "frame_count", 0))
                for g in range(self.num_cameras)]

    @property
    def frontier_lags(self) -> list:
        coord = self._coordinator
        if coord is None:
            return []
        try:
            return coord.lag_frames()
        except Exception:
            return []

    # -- camera activity, as the parent sees it ------------------------------------

    def _activity_count(self, g: int) -> int:
        return (int(self._status_get(g, "frame_count", 0))
                + int(self._status_get(g, "failed_grabs", 0))
                + int(self._status_get(g, "preview_seq", 0)))

    def _note_activity(self, now: float) -> None:
        for g in range(self.num_cameras):
            n = self._activity_count(g)
            seen = self._activity.get(g)
            if seen is None or seen[0] != n:
                self._activity[g] = (n, now)

    def _idle_s(self, g: int, now: float) -> float:
        self._note_activity(now)
        seen = self._activity.get(g)
        return 0.0 if seen is None else max(0.0, now - seen[1])

    def _silence_of(self, g: int, now=None) -> float:
        """Seconds since camera g last delivered a result, 0.0 before the
        trigger source was marked started (CameraManager's rule), counted
        from the mark until its first result."""
        now = time.perf_counter() if now is None else now
        start = self._board_started_t
        if start is None:
            return 0.0
        self._note_activity(now)
        seen = self._activity.get(g)
        last = start if seen is None else max(seen[1], start)
        return max(0.0, now - last)

    def _active(self) -> list:
        coord = self._coordinator
        retired = ({c for c, _r in coord.core.retired_reasons}
                   if coord is not None else set())
        return [g for g in range(self.num_cameras)
                if g not in retired and self._worker_alive(g)
                and not self._status_get(g, "retrieve_loop_exited", 0)]

    def seconds_since_frame(self) -> list:
        now = time.perf_counter()
        return [self._silence_of(g, now) for g in range(self.num_cameras)]

    def source_silent(self, threshold_s: float = SOURCE_SILENT_S) -> bool:
        now = time.perf_counter()
        active = self._active()
        return bool(active) and all(self._silence_of(g, now) > threshold_s
                                    for g in active)

    def _rig_source_down(self, now: float) -> bool:
        active = self._active()
        return len(active) >= 2 and all(
            self._silence_of(g, now) > SOURCE_SILENT_S for g in active)

    def _publish_rig(self, source_down: bool) -> None:
        rig = self._rig
        if rig is not None:
            try:
                rig.record.write({"source_down": 1.0 if source_down else 0.0})
            except Exception:
                pass

    # -- errors --------------------------------------------------------------------------

    def _open_failed(self, message: str) -> CameraOpenError:
        self.last_open_error = message
        self.error.emit(message)
        return CameraOpenError(message)

    # -- workers ---------------------------------------------------------------------------

    def _log_path(self, wid: int):
        if self._log_dir is None:
            return None
        return str(self._log_dir / f"panopticon_{self._stamp}_w{wid}.log")

    def _ensure_threads(self) -> None:
        if self._supervisor is None or not self._supervisor.is_alive():
            self._shutdown.clear()
            self._supervisor = threading.Thread(
                target=self._supervise, daemon=True, name="mp-supervisor")
            self._supervisor.start()
        if self._log_reader is None or not self._log_reader.is_alive():
            self._log_reader = threading.Thread(
                target=self._read_logs, daemon=True, name="mp-log-reader")
            self._log_reader.start()

    def _supervise(self) -> None:
        while not self._shutdown.is_set():
            live = [w for w in list(self._workers) if not w.dead
                    and w.conn is not None]
            if not live:
                time.sleep(0.05)
                continue
            objs = {}
            for w in live:
                objs[w.conn] = ("conn", w)
                objs[w.process.sentinel] = ("exit", w)
            try:
                ready = _mp_wait(list(objs), timeout=0.1)
            except (OSError, ValueError):
                time.sleep(0.01)
                continue
            for o in ready:
                kind, w = objs[o]
                if w.dead:
                    continue
                if kind == "conn":
                    try:
                        msg = w.conn.recv()
                    except (EOFError, OSError):
                        self._on_exit(w)
                        continue
                    if "id" in msg:
                        w.resolve(msg)
                    elif msg.get("event") == "hello":
                        w.pid = msg.get("pid")
                        self._attach_status(w, msg.get("status"))
                        w.hello.set()
                    elif msg.get("event") == "fatal":
                        w.fatal = msg.get("error")
                        print(f"[mp] capture process for {w.names} failed to "
                              f"start: {w.fatal}", flush=True)
                else:
                    self._on_exit(w)

    def _read_logs(self) -> None:
        while not self._shutdown.is_set():
            conns = {w.log_conn: w for w in list(self._workers)
                     if w.log_conn is not None}
            if not conns:
                time.sleep(0.05)
                continue
            try:
                ready = _mp_wait(list(conns), timeout=0.2)
            except (OSError, ValueError):
                time.sleep(0.01)
                continue
            for c in ready:
                w = conns[c]
                try:
                    line = c.recv_bytes()
                except (EOFError, OSError):
                    try:
                        c.close()
                    except Exception:
                        pass
                    w.log_conn = None
                    continue
                print(line.decode("utf-8", "replace"), flush=True)

    def _attach_status(self, w: _Worker, spec) -> None:
        if not spec:
            return
        try:
            seg = shm.SharedSegment.attach(spec[0])
            w._status_seg = seg
            w.status = shm.StatusSegment.attach(seg.buf, epoch=int(spec[1]))
        except Exception as e:
            print(f"[mp] could not attach the status of the capture process "
                  f"for {w.names}: {e}", flush=True)

    def _on_exit(self, w: _Worker) -> None:
        if w.dead:
            return
        w.dead = True
        try:
            w.process.join(timeout=1.0)
        except Exception:
            pass
        w.exitcode = w.process.exitcode
        w.fail_all(w._exit_text())
        w.hello.set()
        self._grants.pop(w.wid, None)
        if w.closing:
            return
        coord = self._coordinator
        reason = (f"capture process for {w.names} exited"
                  + ("" if w.exitcode is None else f" with code {w.exitcode}"))
        print(f"[mp] {reason}", flush=True)
        if coord is not None and w.armed:
            for g in w.cams:
                try:
                    coord.retire(g, reason)
                except Exception as e:
                    print(f"[mp] retiring cam{g + 1} failed: {e}", flush=True)

    def _spawn(self, groups: list, serials: list) -> str | None:
        """Start one worker per group; None, or why they could not start."""
        ctx = multiprocessing.get_context("spawn")
        exe = windowless_executable()
        if exe is not None:
            use_executable(exe)
        state = None
        spawn_state = getattr(self._backend, "spawn_state", None)
        if spawn_state is not None:
            try:
                state = spawn_state()
            except Exception as e:
                return f"{e}"
        if self._gate is None:
            self._gate = ctx.Semaphore(1)
        if self._rig is None:
            epoch = shm.new_epoch()
            self._rig_seg = shm.SharedSegment.create(
                shm.segment_name("rig", epoch=epoch),
                shm.RecordSegment.size_for(wk.RIG_FIELDS))
            self._rig = shm.RecordSegment.create(self._rig_seg.buf,
                                                 wk.RIG_FIELDS, epoch=epoch)
            self._rig_spec = (self._rig_seg.name, epoch)
            self._publish_rig(False)
        self._ensure_threads()
        workers = []
        for wid, idx in enumerate(groups, start=1):
            w = _Worker(wid, idx, [serials[i] for i in idx])
            args = {"worker": wid, "cameras": list(zip(w.cams, w.serials)),
                    "profile": self._profile, "parent_pid": os.getpid(),
                    "backend": self._backend_name, "backend_state": state,
                    "switch_interval": sys.getswitchinterval(),
                    "log_path": self._log_path(wid), "rig": self._rig_spec}
            parent_conn, child_conn = ctx.Pipe(duplex=True)
            log_r, log_w = ctx.Pipe(duplex=False)
            p = ctx.Process(target=wk.worker_main,
                            args=(args, child_conn, log_w, self._gate),
                            name=f"panopticon-capture-w{wid}", daemon=True)
            w.process, w.conn, w.log_conn = p, parent_conn, log_r
            try:
                p.start()
            except Exception as e:
                parent_conn.close()
                log_r.close()
                return (f"the capture process for {w.names} could not be "
                        f"started: {type(e).__name__}: {e}")
            finally:
                child_conn.close()
                log_w.close()
            handle = getattr(getattr(p, "_popen", None), "_handle", None)
            if self._job.available and not self._job.assign(handle):
                print(f"[mp] WARNING: the capture process for {w.names} is "
                      f"not in the kill-on-close job ({self._job.reason}); "
                      f"it still exits when this process does", flush=True)
            workers.append(w)
            with self._lock:
                self._workers.append(w)
        deadline = time.monotonic() + HELLO_TIMEOUT_S
        for w in workers:
            w.hello.wait(max(0.0, deadline - time.monotonic()))
        bad = [w for w in workers if w.dead or w.pid is None]
        if bad:
            w = bad[0]
            why = (w.fatal or (w._exit_text() if w.dead else
                               f"it did not report within {HELLO_TIMEOUT_S:g} s"))
            return f"the capture process for {w.names} did not start: {why}"
        for w in workers:
            for k, g in enumerate(w.cams):
                self._where[g] = (w, k)
            print(f"[mp] capture process w{w.wid} pid {w.pid}: {w.names}",
                  flush=True)
        return None

    def _stop_workers(self, op: str = "close",
                      timeout_s: float = CLOSE_TIMEOUT_S) -> None:
        """Ask every worker to `op` (close or abandon) and exit; terminate
        the ones that do not, then release their segments."""
        workers = list(self._workers)
        for w in workers:
            w.closing = True
        calls = {w: w.call(op) for w in workers if not w.dead}
        _await(calls, timeout_s)
        if op != "close":
            calls = {w: w.call("close") for w in workers if not w.dead}
            _await(calls, CLOSE_TIMEOUT_S)
        deadline = time.monotonic() + 5.0
        for w in workers:
            if w.process is not None:
                w.process.join(max(0.0, deadline - time.monotonic()))
        for w in workers:
            if w.process is not None and w.process.is_alive():
                print(f"[mp] the capture process for {w.names} did not exit; "
                      f"terminating it", flush=True)
                w.process.terminate()
                w.process.join(5.0)
            w.dead = True
            w.fail_all("the capture process was closed")
            try:
                w.conn.close()
            except Exception:
                pass
            w.close_segments()
        with self._lock:
            self._workers = []
        self._where = {}
        self._grants = {}

    # -- open and close ---------------------------------------------------------------------

    def open_all(self, pfs_path: str, gige_driver: str = "socket",
                 trigger_rate_limit: float = 165.0, expect_cameras: int = 0,
                 max_num_buffer: int = 1000, only_serials=None,
                 backend: str | None = None, expect_geometry=None,
                 gev_bandwidth_reserve_pct=None,
                 gev_bandwidth_reserve_accum=None, camera_spec=None):
        """CameraManager.open_all, with the cameras opened by the workers.

        The parent resolves cam1..camN and applies the camera-count
        interlock (resolve_device_order), deals the cameras to
        capture_processes workers in contiguous groups, and has each open
        its own share with the same keywords. The cameras must agree on
        their geometry, as they must in one process. Returns True, or a
        falsy CameraOpenError carrying the reason.
        """
        if self._workers:
            self.close_all()
        self._trigger_rate_limit = trigger_rate_limit
        self.last_open_error = None
        self._geometry = None
        self._camera_info = ()
        self._serials = []
        self._uses_camera_block = camera_spec is not None
        if backend is not None and backend != self._backend_name:
            self._backend_name = backend
            self._backend_obj = None
        devices = self._backend.enumerate_devices()
        if len(devices) == 0:
            return self._open_failed("No cameras found")
        devs, refusal = resolve_device_order(devices, only_serials,
                                             expect_cameras)
        if refusal:
            return self._open_failed(refusal)
        serials = [str(d.GetSerialNumber()) for d in devs]
        groups = split_contiguous(len(serials), self.capture_processes)
        if len(groups) < self.capture_processes:
            print(f"[mp] {len(serials)} cameras for capture_processes "
                  f"{self.capture_processes}: starting {len(groups)} "
                  f"processes", flush=True)
        why = self._spawn(groups, serials)
        if why:
            self._stop_workers()
            return self._open_failed(why)
        kwargs = {"pfs_path": pfs_path, "gige_driver": gige_driver,
                  "trigger_rate_limit": trigger_rate_limit,
                  "max_num_buffer": max_num_buffer,
                  "expect_geometry": expect_geometry,
                  "gev_bandwidth_reserve_pct": gev_bandwidth_reserve_pct,
                  "gev_bandwidth_reserve_accum": gev_bandwidth_reserve_accum,
                  "camera_spec": camera_spec}
        calls = {w: w.call("open", kwargs=kwargs, flags=self._flags(),
                           affinity=self._affinity())
                 for w in self._workers}
        replies = _await(calls, OPEN_TIMEOUT_S)
        failed = [(w, r) for w, (ok, r) in replies.items() if not ok]
        if failed:
            w, err = failed[0]
            self._stop_workers()
            return self._open_failed(str(err))
        geometry = None
        infos = []
        for w in self._workers:
            res = replies[w][1]
            wh = tuple(res["geometry"])
            if geometry is not None and wh != geometry:
                self._stop_workers()
                return self._open_failed(
                    f"Camera {w.serials[0]} failed to open/configure:\n"
                    f"resolution {wh[0]}x{wh[1]} differs from camera 1 "
                    f"({geometry[0]}x{geometry[1]}); all cameras must match."
                    f"\n\nPower-cycle it (or close the app holding it) and "
                    f"reselect the profile.")
            geometry = wh
            infos.extend(res["camera_info"])
            self._attach_images(w, res)
        self._geometry = geometry
        self._camera_info = tuple(infos)
        self._serials = serials
        return True

    def _attach_images(self, w: _Worker, res: dict) -> None:
        for seg in (w.preview, w.frames):
            if seg is not None:
                seg.close()
        for s in w._image_segs:
            try:
                s.close()
            except BufferError:
                pass
        w._image_segs = []
        pname, pe = res["preview"]
        fname, fe = res["frames"]
        pseg = shm.SharedSegment.attach(pname)
        fseg = shm.SharedSegment.attach(fname)
        w._image_segs = [pseg, fseg]
        w.preview = shm.PreviewSegment.attach(pseg.buf, epoch=int(pe))
        w.frames = shm.FrameSegment.attach(fseg.buf, epoch=int(fe))

    def _flags(self) -> dict:
        return {"pin_capture_threads": self.pin_capture_threads,
                "pin_encoder_threads": bool(self.pin_encoder_threads),
                "encoder_pcores": bool(self.encoder_pcores)}

    @staticmethod
    def _affinity() -> dict:
        """The process-wide capture-core settings in force here, which a
        probe may have changed after the profile set them."""
        try:
            from gui_app import cpu_affinity as ca
        except Exception:
            return {}
        order = getattr(ca, "_core_order", None)
        return {"core_order": None if order is None else list(order),
                "grab_priority": getattr(ca, "GRAB_THREAD_PRIORITY", None),
                "overflow_priority": getattr(ca, "_overflow_priority", None)}

    def close_all(self) -> None:
        """Close every worker (and so every camera)."""
        if self._coordinator is not None:
            self._end_acquisition(abandon=True)
        self._stop_workers("close", CLOSE_TIMEOUT_S)
        self._geometry = None
        self._camera_info = ()
        self._serials = []
        self._grab_threads = []
        self._close_rig()

    def _close_rig(self) -> None:
        if self._rig is not None:
            self._rig.close()
            self._rig = None
        if self._rig_seg is not None:
            try:
                self._rig_seg.close()
            except BufferError:
                pass
            self._rig_seg.unlink()
            self._rig_seg = None

    def shutdown(self) -> None:
        """close_all, then end the supervisor and log threads and close the
        job, which terminates any worker still in it."""
        self.close_all()
        self._shutdown.set()
        for t in (self._supervisor, self._log_reader):
            if t is not None:
                t.join(timeout=2.0)
        self._job.close()

    # -- acquisition -------------------------------------------------------------------------

    def start_acquisition(self, raw_paths, display_every: int = 10,
                          realtime: bool = False, width: int = 0,
                          height: int = 0, quality: int = 21, fps: int = 100,
                          realtime_kick: bool = False, kick_max_lag: int = 240,
                          exposure_us=None, gain_db=None):
        """CameraManager.start_acquisition over the workers.

        The ledger for this acquisition is created and the coordinator
        thread started before any worker arms, so the first announce finds
        its coordinator. A refusal from any worker undoes the start in
        every worker (cancel) and raises AcquisitionStartRefused, with the
        cameras back in preview.
        """
        mismatch = self.geometry_mismatch(width, height)
        if mismatch:
            raise AcquisitionStartRefused(
                f"{mismatch}\n\nNothing was recorded and the cameras are "
                f"still in preview.")
        if not (realtime and realtime_kick):
            raise AcquisitionStartRefused(
                "Capture in several processes (capture_processes > 0) runs "
                "only on the real-time kick-out path: turn on realtime_encode "
                "and realtime_kick, or set capture_processes: 0.\n\nNothing "
                "was recorded.")
        if not self._workers:
            raise AcquisitionStartRefused("No cameras are open.")
        if self._coordinator is not None:
            print("[acq] WARNING: start_acquisition called with a live "
                  "acquisition; abandoning it first", flush=True)
            self._end_acquisition(abandon=True)
        self.last_warnings = []
        self.last_stream_stats = []
        self.last_retired = {}
        self.last_encoder_failures = []
        self.last_encoder_records = []
        self._board_started_t = None
        self._marked = False
        self._barrier = {}
        self._activity = {}
        self._triggers_running = False
        n = self.num_cameras
        cap = self.session_cap
        if cap is not None and n > int(cap):
            raise AcquisitionStartRefused(
                f"{n} cameras need {n} NVENC sessions and the driver allows "
                f"{int(cap)}.\n\nNothing was recorded.")
        try:
            spec = wk.factory_spec(self.encoder_factory)
        except ValueError as e:
            raise AcquisitionStartRefused(f"{e}\n\nNothing was recorded.")
        upload = None
        if spec.nvenc:
            from gui_app import nvenc
            upload = nvenc.upload_config()
        self._ledger_seg, self._coordinator = create_shared(n, kick_max_lag)
        self._max_lag, self._fps = int(kick_max_lag), int(fps)
        self._router = _RouterView(self._coordinator, kick_max_lag)
        self._last_core = self._coordinator.core
        self._grab_threads = [_CameraView(self, g) for g in range(n)]
        self._coord_stop.clear()
        self._coord_thread = threading.Thread(target=self._coordinate,
                                              daemon=True,
                                              name="mp-coordinator")
        self._coord_thread.start()
        ledger = (self._ledger_seg.name, self._coordinator.epoch)
        calls = {}
        for w in self._workers:
            calls[w] = w.call(
                "arm", raw_paths=[str(raw_paths[g]) for g in w.cams],
                display_every=display_every, realtime=realtime, width=width,
                height=height, quality=quality, fps=fps,
                realtime_kick=realtime_kick, kick_max_lag=kick_max_lag,
                exposure_us=exposure_us, gain_db=gain_db, ledger=ledger,
                factory=spec, nvenc_upload=upload, flags=self._flags(),
                affinity=self._affinity())
            w.armed = True
            self._grants[w.wid] = len(w.cams)
        replies = _await(calls, ARM_TIMEOUT_S)
        refused = [(w, r) for w, (ok, r) in replies.items() if not ok]
        if refused:
            for w, err in refused:
                if err.data and err.data.get("encoder_failures"):
                    self.last_encoder_failures.extend(err.data["encoder_failures"])
            # A worker that refused through AcquisitionStartRefused has put
            # its cameras back in preview itself; every other worker may
            # have started, so it undoes its start.
            undo = [w for w, (good, r) in replies.items()
                    if good or r.kind != "AcquisitionStartRefused"]
            _await({w: w.call("cancel") for w in undo}, RESUME_TIMEOUT_S)
            self._end_acquisition(abandon=True)
            w, err = refused[0]
            msg = str(err)
            if err.kind != "AcquisitionStartRefused":
                msg = (f"The capture process for {w.names} could not start "
                       f"the acquisition: {msg}\n\nNothing was recorded.")
            raise AcquisitionStartRefused(msg)
        for w in self._workers:
            self.last_warnings.extend(replies[w][1].get("warnings", []))
        print(f"[acq] multi-process capture: {len(self._workers)} capture "
              f"processes, {n} cameras", flush=True)

    def _coordinate(self) -> None:
        """The coordinator thread: poll the ledger; keep the rig's silence
        flag, the hang check and the lag report current."""
        coord = self._coordinator
        next_status = 0.0
        next_lag = time.monotonic() + LAG_REPORT_S
        while not self._coord_stop.is_set():
            try:
                coord.poll()
            except Exception as e:
                print(f"[mp] coordinator poll failed: {type(e).__name__}: "
                      f"{e}", flush=True)
            now_m = time.monotonic()
            if now_m >= next_status:
                next_status = now_m + STATUS_READ_S
                now = time.perf_counter()
                try:
                    self._publish_rig(self._rig_source_down(now))
                    if self._triggers_running:
                        self._check_hangs(coord)
                except Exception as e:
                    print(f"[mp] status read failed: {type(e).__name__}: "
                          f"{e}", flush=True)
            if self._triggers_running and now_m >= next_lag:
                next_lag = now_m + LAG_REPORT_S
                print(coord.lag_report(), flush=True)
            time.sleep(POLL_S)

    def _check_hangs(self, coord) -> None:
        for w in list(self._workers):
            if w.dead or w.hung or not w.armed or w.status is None:
                continue
            ages = [w.status.heartbeat_age_s(k) for k in range(len(w.cams))]
            if not ages or any(a is None or a < HANG_S for a in ages):
                continue
            if any(coord.progress_age_s(g) < HANG_S for g in w.cams):
                continue
            w.hung = True
            reason = (f"capture process for {w.names} stopped responding "
                      f"(no heartbeat for {min(ages):.0f} s)")
            print(f"[mp] {reason}", flush=True)
            for g in w.cams:
                coord.retire(g, reason)

    def wait_until_ready(self, timeout_s: float = 30.0) -> tuple:
        """CameraManager.wait_until_ready over every worker at once:
        (cameras ready, cameras in the acquisition)."""
        calls = {w: w.call("ready", timeout=float(timeout_s))
                 for w in self._workers if w.armed}
        replies = _await(calls, float(timeout_s) + READY_MARGIN_S)
        ready = total = 0
        for w, (ok, res) in replies.items():
            total += len(w.cams)
            if ok:
                w.ready_info = res
                ready += int(res.get("ready", 0))
                for g, pin in (res.get("pins") or {}).items():
                    g = int(g)
                    if 0 <= g < len(self._grab_threads):
                        self._grab_threads[g].pin_result = pin
            else:
                w.ready_info = {"ready": 0, "total": len(w.cams),
                                "not_ready": list(w.cams),
                                "error": str(res)}
        return ready, total

    def not_ready(self) -> list:
        """Zero-based rig-wide indices of the cameras whose worker has not
        reported them armed (see wait_until_ready)."""
        out = []
        for w in self._workers:
            info = w.ready_info
            if info is None:
                out.extend(w.cams)
            else:
                out.extend(int(g) for g in info.get("not_ready", []))
        return sorted(out)

    def ready_times_ns(self) -> dict:
        """{worker: shm.now_ns() when its wait_until_ready returned}."""
        return {w.wid: (w.ready_info or {}).get("ready_ns")
                for w in self._workers}

    def native_thread_ids(self) -> dict:
        """{camera index: (worker pid, native id of its grab thread)} from
        the last wait_until_ready, for tools that read per-thread CPU time."""
        out = {}
        for w in self._workers:
            info = w.ready_info or {}
            for g, tid in (info.get("native_ids") or {}).items():
                out[int(g)] = (info.get("pid"), int(tid))
        return out

    def mark_board_starting(self) -> None:
        """CameraManager.mark_board_starting in every worker, before the
        trigger board is told to start. The frames each camera retrieved
        before the mark come back with it (frames_before_barrier)."""
        self._board_started_t = time.perf_counter()
        calls = {w: w.call("mark") for w in self._workers if w.armed}
        counts = {}
        for w, (ok, res) in _await(calls, SIGNAL_TIMEOUT_S).items():
            if ok:
                counts.update(res.get("frames_before_barrier", {}))
            else:
                print(f"[acq] WARNING: {res}", flush=True)
        self._barrier = counts
        self._marked = True

    def frames_before_barrier(self) -> dict:
        """{"camN": frames retrieved in trigger mode before the barrier}, in
        camera order (CameraManager.frames_before_barrier)."""
        if not self._marked:
            calls = {w: w.call("barrier_counts") for w in self._workers
                     if w.armed}
            counts = {}
            for w, (ok, res) in _await(calls, SIGNAL_TIMEOUT_S).items():
                if ok:
                    counts.update(res.get("frames_before_barrier", {}))
        else:
            counts = self._barrier
        return {f"cam{g + 1}": int(counts.get(f"cam{g + 1}", 0))
                for g in range(self.num_cameras)}

    def signal_triggers_started(self) -> None:
        calls = {w: w.call("go") for w in self._workers if w.armed}
        for w, (ok, res) in _await(calls, SIGNAL_TIMEOUT_S).items():
            if not ok and self._coordinator is not None:
                reason = (f"capture process for {w.names} did not confirm "
                          f"the trigger start ({res})")
                print(f"[acq] {reason}", flush=True)
                for g in w.cams:
                    self._coordinator.retire(g, reason)
        self._triggers_running = True

    def stop_acquisition(self) -> list:
        """CameraManager.stop_acquisition over the workers.

        Each worker stops its grab threads, marks its cameras' end of
        stream and waits for the flush; the parent flushes once every
        active camera has reached end of stream or been retired (a camera
        that does not within STOP_EOS_S is retired), and each worker then
        routes the final decisions, drains its encoders and reconciles its
        cameras' block IDs. The cross-camera checks (forced drops,
        kick-outs, the block-ID rate, retirements, a silent trigger source)
        run here, over every camera's results.

        Returns [(count, timestamps, block_ids)] per camera. Raises
        AcquisitionStopIncomplete, with the results on it, when a worker
        reports a grab thread still running.
        """
        coord = self._coordinator
        if coord is None:
            raise RuntimeError("stop_acquisition called with no acquisition")
        self._triggers_running = False
        armed = [w for w in self._workers if w.armed]
        calls = {w: w.call("stop") for w in armed}
        deadline = time.monotonic() + STOP_EOS_S
        while time.monotonic() < deadline:
            if coord.all_eos():
                break
            if all(c.event.is_set() for c in calls.values()):
                break
            time.sleep(0.01)
        for g in coord.eos_missing():
            coord.retire(g, "its capture process did not reach the end of "
                            "the recording within "
                            f"{STOP_EOS_S:g} s of the stop")
        try:
            coord.flush()
        except Exception as e:
            print(f"[mp] flush: {e}; flushing without the cameras still "
                  f"missing", flush=True)
            coord.flush(require_eos=False)
        replies = _await(calls, STOP_RESULTS_S)
        self._coord_stop.set()
        if self._coord_thread is not None:
            self._coord_thread.join(timeout=2.0)
        n = self.num_cameras
        results = [(0, [], []) for _ in range(n)]
        warnings: list = []
        retired: dict = {}
        failures: list = []
        stats = [{"error": "no report from its capture process"}
                 for _ in range(n)]
        stuck: list = []
        dropped_full = backlog_peak = 0
        records: list = []
        for w in armed:
            ok, res = replies[w]
            if not ok:
                if w.dead:
                    text = (f"{w.names}: the capture process exited during "
                            f"the recording"
                            + ("" if w.exitcode is None else
                               f" (code {w.exitcode})")
                            + ". Its stream.h264 is partial and the "
                              "frame-to-trigger mapping is UNVERIFIED; the "
                              "block IDs of the frames it had handed to the "
                              "encoder are in blockids.partial beside the "
                              "stream.")
                else:
                    text = (f"{w.names}: the capture process did not return "
                            f"its results ({res}). Its stream.h264 is kept "
                            f"and the frame-to-trigger mapping is "
                            f"UNVERIFIED; see blockids.partial beside the "
                            f"stream.")
                print(f"[acq] WARNING: {text}", flush=True)
                warnings.append(text)
                continue
            for k, g in enumerate(w.cams):
                if k < len(res["results"]):
                    c, ts, ids = res["results"][k]
                    results[g] = (int(c), list(ts), list(ids))
                if k < len(res["stream_stats"]):
                    stats[g] = res["stream_stats"][k]
                det = res["details"].get(g)
                if det is not None:
                    self._grab_threads[g].final = det
                    dropped_full += int(det.get("dropped_full", 0))
            warnings.extend(res["warnings"])
            for name, reason in res["retired"].items():
                retired.setdefault(name, reason)
            failures.extend(res["encoder_failures"])
            stuck.extend(res["stuck"])
            backlog_peak = max(backlog_peak, int(res["backlog_peak"]))
            for rec in res["encoder_records"]:
                rec = dict(rec)
                rec["worker"] = w.wid
                records.append(rec)
        core = coord.core
        print(f"[sync] released={core.released} dropped={core.dropped} "
              f"forced={core.forced} queue_full_drops={dropped_full}",
              flush=True)
        warnings.extend(sync_encode.session_warnings(
            core, [r[1] for r in results], [r[2] for r in results],
            self._fps, self._max_lag))
        for cam, reason in core.retired_reasons:
            retired.setdefault(f"cam{cam + 1}", reason)
        warnings.extend(self._source_silence_warnings(self._grab_threads))
        if self._router is not None:
            self._router.dropped_full = dropped_full
            self._router.backlog_peak = backlog_peak
        self.last_warnings = warnings
        self.last_results = results
        self.last_retired = retired
        self.last_encoder_failures = failures
        self.last_stream_stats = stats
        self.last_encoder_records = records
        spec = self.encoder_factory
        if isinstance(spec, wk.FactorySpec) and spec.records is not None:
            spec.records.extend(records)
        self._end_acquisition(abandon=False)
        if stuck:
            names = ", ".join(f"cam{g + 1}" for g in sorted(stuck))
            raise AcquisitionStopIncomplete(
                f"grab thread(s) for {names} did not exit after the stop "
                f"timeouts (GPU or driver wedged?). The cameras were NOT "
                f"reconfigured; restart the application before recording "
                f"again. The captured streams are on disk, but blockids.npy "
                f"and frametimes.npy have NOT been written yet: save "
                f"last_results before abandoning.", results, sorted(stuck))
        return results

    def _end_acquisition(self, abandon: bool) -> None:
        """Stop the coordinator thread and release the ledger and the
        session grants."""
        coord = self._coordinator
        if coord is not None and abandon:
            try:
                coord.abandon()
            except Exception:
                pass
        self._coord_stop.set()
        t = self._coord_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._coord_thread = None
        self._triggers_running = False
        if coord is not None:
            self._last_core = coord.core
            coord.close()
        self._coordinator = None
        seg, self._ledger_seg = self._ledger_seg, None
        if seg is not None:
            try:
                seg.close()
            except BufferError:
                pass
            seg.unlink()
        for w in self._workers:
            w.armed = False
        self._grants = {}
        self._router = None

    def resume_preview(self, preview_fps: float = 30.0) -> None:
        calls = {w: w.call("resume_preview", preview_fps=preview_fps)
                 for w in self._workers}
        failed = [str(r) for ok, r in _await(calls, RESUME_TIMEOUT_S).values()
                  if not ok]
        self._grab_threads = []
        if failed:
            raise RuntimeError("; ".join(failed))

    def abandon(self) -> None:
        """Tear down at once, without draining (CameraManager.abandon):
        every worker abandons its acquisition and closes its cameras, and
        this returns only once each has closed its files or been
        terminated, so the caller can delete the session directory."""
        if self._coordinator is not None:
            try:
                self._coordinator.abandon()
            except Exception:
                pass
        self._stop_workers("abandon", ABANDON_TIMEOUT_S)
        self._end_acquisition(abandon=True)
        self.last_stream_stats = []
        self._geometry = None
        self._camera_info = ()
        self._serials = []
        self._grab_threads = []
        self._close_rig()

    # -- views and cold-path reads --------------------------------------------------------

    def thermals(self) -> list:
        calls = {w: w.call("thermals") for w in self._workers}
        replies = _await(calls, THERMALS_TIMEOUT_S)
        out = [{"error": "no capture process"} for _ in range(self.num_cameras)]
        for w, (ok, res) in replies.items():
            for k, g in enumerate(w.cams):
                if ok and k < len(res["readings"]):
                    out[g] = res["readings"][k]
                elif not ok:
                    out[g] = {"error": "timeout" if res.kind == "timeout"
                              else str(res)}
        return out

    def request_snapshots(self) -> None:
        self._snap_base = {g: self._status_get(g, "snapshot_seq", 0)
                           for g in range(self.num_cameras)}
        for w in self._workers:
            w.call("snapshot")

    def set_keep_full(self, flag: bool) -> None:
        self._keep_full = bool(flag)
        calls = {w: w.call("set_keep_full", flag=bool(flag))
                 for w in self._workers}
        _await(calls, VIEW_TIMEOUT_S)
