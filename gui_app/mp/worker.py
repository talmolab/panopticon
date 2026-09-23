"""Capture worker process: grabs and encodes one share of the rig's cameras.

A worker is spawned once per camera open by `gui_app.mp.manager.
ProcessCameraManager` and lives through previews, calibrations and
recordings until the cameras are closed. Inside it everything is today's
in-process code: a CameraManager over the worker's cameras (its
`global_indices` seam names each camera by its index in the whole rig),
the unchanged GrabThread per camera, and one `sync_encode._CameraSink` per
camera. Only the kick-out decision moves out: `WorkerRouter` announces each
grabbed trigger in the cross-process ledger (gui_app.mp.ledger) and routes a
frame to its sink once the parent's coordinator has decided it.

Frames never leave the process. What crosses to the parent is the ledger,
a status segment (heartbeat and counters, written every STATUS_PERIOD_S by
a status thread, never by a grab thread), preview and full-resolution
slots copied from what the grab threads already publish, the control pipe's
replies, and the log pipe.

The control loop runs on the main thread, which also owns the
QCoreApplication the grab threads (QThreads) need. Requests arrive as
{"id", "op", ...} dicts and each gets one reply, {"id", "ok", "result"} or
{"id", "ok": False, "error", "kind"}. The ops are open, arm, ready, mark,
barrier_counts, go, stop, cancel, resume_preview, set_keep_full, snapshot,
thermals, abandon and close.

The worker exits when its parent does: a watcher thread waits on the
parent's process sentinel. The job object the parent assigns it to
(gui_app.mp.winjob) is the backstop.
"""
from __future__ import annotations

import faulthandler
import gc
import importlib
import inspect
import os
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from gui_app import encoders
from gui_app import sync_encode
from gui_app.frame_sync import UnwrapState, unwrap_one
from gui_app.mp import shm
from gui_app.mp.ledger import LedgerState, attach_shared

#: Seconds between status-segment updates: heartbeat, counters, preview and
#: full-resolution copies, and the harvest of cameras that are not
#: submitting.
STATUS_PERIOD_S = 0.05
#: A camera whose grab thread has not submitted for this long has its
#: decided frames collected by the status thread instead. At the trigger
#: rate a streaming camera submits far more often, so the two never contend.
IDLE_HARVEST_S = 0.02
#: How long a stopping worker waits for the parent to flush the ledger once
#: its cameras reached end of stream. The parent flushes as soon as every
#: camera has, so this bounds a parent that died or wedged.
FLUSH_WAIT_S = 60.0
#: Frames between appends to a camera's blockids.partial.
PARTIAL_EVERY = 100
#: Record dtype of blockids.partial: each frame handed to the encoder, in
#: order, as (unwrapped block ID, device timestamp in s).
PARTIAL_DTYPE = np.dtype([("block_id", "<i8"), ("timestamp_s", "<f8")])
#: Name of the rig-wide record the parent publishes (see RIG_FIELDS).
RIG_FIELDS = {"source_down": 1}
#: The factory a worker builds encoders with when its parent names none.
DEFAULT_FACTORY = "gui_app.encoders:nvenc_factory"


def _downsample_default() -> int:
    """The preview decimation GrabThread applies when the manager passes
    none, read from its signature so the preview slot is sized from the
    same number."""
    from gui_app.grab_thread import GrabThread
    return int(inspect.signature(GrabThread.__init__)
               .parameters["downsample"].default)


# -- encoder factories by name ------------------------------------------------

@dataclass(frozen=True)
class FactorySpec:
    """An encoder factory a worker can build: "module:attr", and options.

    With no options, `module.attr` is the factory itself
    (encoders.EncoderFactory). With options, `module.attr(**options)` is
    called once in the worker and returns the factory; that is how a probe
    passes its instrumented factory. `uses_nvenc` says whether the encoders
    come from gui_app.nvenc, so the worker applies the parent's upload
    setting and warms NVENC under the cross-process gate; None infers it
    from `target`.

    A factory with a `records` attribute (a list) has it returned with the
    stop results, and the parent appends those entries to the `records`
    list of the spec it was given (`records` is not pickled).
    """
    target: str = DEFAULT_FACTORY
    options: tuple = ()
    uses_nvenc: bool | None = None
    records: list | None = field(default=None, compare=False)

    def __getstate__(self):
        return {"target": self.target, "options": self.options,
                "uses_nvenc": self.uses_nvenc}

    def __setstate__(self, state):
        object.__setattr__(self, "target", state["target"])
        object.__setattr__(self, "options", tuple(state["options"]))
        object.__setattr__(self, "uses_nvenc", state["uses_nvenc"])
        object.__setattr__(self, "records", None)

    @property
    def nvenc(self) -> bool:
        if self.uses_nvenc is not None:
            return bool(self.uses_nvenc)
        return self.target == DEFAULT_FACTORY


def _import_target(target: str):
    module, _, attr = str(target).partition(":")
    if not module or not attr:
        raise ValueError(f"an encoder factory is named 'module:attr', got "
                         f"{target!r}")
    obj = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def factory_spec(factory) -> FactorySpec:
    """The FactorySpec a worker rebuilds `factory` from.

    None means the process default (encoders.get_default_factory()). A plain
    function is named by its module and qualified name, and refused with
    ValueError unless that name leads back to the same object, because a
    worker that built a different encoder would record the wrong arm under
    the caller's name.
    """
    if isinstance(factory, FactorySpec):
        return factory
    if factory is None:
        factory = encoders.get_default_factory()
    if factory is encoders.nvenc_factory:
        return FactorySpec(DEFAULT_FACTORY)
    module = getattr(factory, "__module__", None)
    qual = getattr(factory, "__qualname__", None)
    in_script = module == "__main__"
    if in_script:
        # The worker imports the parent's script by its file name. It is
        # not imported here: that would run the script's top level again.
        path = getattr(sys.modules.get("__main__"), "__file__", None)
        module = Path(path).stem if path else None
    target = f"{module}:{qual}"
    ok = False
    if module and qual and "<locals>" not in qual:
        if in_script:
            ok = True
        else:
            try:
                ok = _import_target(target) is factory
            except Exception:
                ok = False
    if not ok:
        raise ValueError(
            f"the encoder factory {factory!r} cannot be named as "
            f"'module:attr', so a capture worker process cannot build it. "
            f"Install a module-level function, or pass a "
            f"gui_app.mp.worker.FactorySpec.")
    return FactorySpec(target, uses_nvenc=False)


def build_factory(spec: FactorySpec):
    """The factory `spec` names, built in this process."""
    obj = _import_target(spec.target)
    if spec.options:
        obj = obj(**dict(spec.options))
    if not callable(obj):
        raise TypeError(f"{spec.target} is not an encoder factory")
    return obj


# -- the worker's kick-out router -------------------------------------------------

class WorkerRouter:
    """The kick-out router a worker's grab threads submit to.

    It has the members GrabThread uses (max_lag, attach_ring, submit,
    retire, pending) and the ones CameraManager uses (available,
    unavailable_reason, start, stop, abandon, warnings, retired_reasons,
    encoder_failures, lag_frames). Cameras are keyed by their rig-wide
    index, which is the index each GrabThread carries.

    Per frame, submit() unwraps the block ID with frame_sync.unwrap_one (the
    rule the in-process coordinator applies), announces the trigger in the
    ledger and collects every trigger of this camera the coordinator has
    decided since: a released frame goes to the camera's _CameraSink, a
    dropped one gives its ring slot back. The ring's free list (attach_ring)
    is the ring guard: a slot is written only once the frame in it has been
    dropped or encoded, so a coordinator that stops deciding makes the grab
    thread find no free slot, and the frame is announced without pixels and
    dropped from this camera's video, never written over a pending one.

    Each camera has a lock. Its grab thread takes it per submit; the status
    thread takes it without waiting, only for a camera that has not
    submitted for IDLE_HARVEST_S, to collect decisions while the camera is
    silent (a stalled camera must still be harvested at least every
    ring_bits triggers, see the ledger).
    """

    def __init__(self, cams, ledgers, raw_paths, width: int, height: int,
                 quality: int, fps: int = 100, max_lag: int = 240,
                 pin_encoders: bool = False, enc_pcores: bool = False,
                 encoder_factory=None, rate_hints=None, threads_fn=None):
        self.cams = [int(c) for c in cams]
        self.max_lag = max_lag
        self._fps = int(fps)
        self._ledger = dict(zip(self.cams, ledgers))
        self._locks = {c: threading.Lock() for c in self.cams}
        self._unwrap = {c: UnwrapState() for c in self.cams}
        #: Per camera, (trigger, ts, buf) for every announced frame whose
        #: fate is not yet known, in the ledger's order.
        self._held = {c: deque() for c in self.cams}
        self._last_submit = {c: 0.0 for c in self.cams}
        #: Native thread id of each camera's grab thread, recorded when it
        #: attaches its ring, for tools that read per-thread CPU time.
        self.native_ids: dict = {}
        self._retire_req: dict = {}
        self._threads_fn = threads_fn
        self._stopping = False
        self.warnings: list = []
        self.encoder_failures: list = []
        self.backlog_peak = 0
        factory = encoder_factory or encoders.get_default_factory()
        sinks, reason = sync_encode.open_sinks(
            self.cams, raw_paths, width, height, quality, fps, factory,
            self.warnings, pin_encoders=pin_encoders, enc_pcores=enc_pcores)
        self.available = not reason
        self.unavailable_reason = reason
        self._sinks = dict(zip(self.cams, sinks))
        self._partial_written = {c: 0 for c in self.cams}

    # -- what GrabThread uses ---------------------------------------------------

    def attach_ring(self, cam: int, slots):
        self.native_ids[cam] = threading.get_native_id()
        return self._sinks[cam].attach_ring(slots)

    def submit(self, cam: int, block_id: int, ts: float, buf) -> None:
        sink = self._sinks[cam]
        with self._locks[cam]:
            self._last_submit[cam] = time.perf_counter()
            if block_id <= 0:
                # The rule FrameSyncCoordinator.submit applies: 0 is reserved
                # and a negative ID means the camera did not report one, and
                # either would read as a 16-bit wrap. The grab thread counts
                # the exception as a frame error.
                sink.give_back(buf)
                raise ValueError(
                    f"cam{cam + 1}: block ID {block_id} is not a trigger "
                    f"ordinal (0 is reserved, negative means unreported)")
            t = unwrap_one(block_id, self._unwrap[cam])
            try:
                entered = self._ledger[cam].announce(t)
            except ValueError:
                sink.give_back(buf)
                raise
            if entered:
                self._held[cam].append((t, ts, buf))
            else:
                # Retired, retirement requested, or the coordinator has not
                # decided a trigger ring_bits older: dropped before it was
                # announced, so it is a gap in this camera's blockids.npy.
                sink.give_back(buf)
            self._collect_locked(cam)

    def retire(self, cam: int, reason: str = "") -> None:
        """Ask the coordinator to drop this camera from the alignment set.
        Its grab thread calls this when it gives up; the coordinator honours
        it on its next poll and prints the retirement."""
        with self._locks[cam]:
            self._retire_locked(cam, reason)

    def pending(self) -> int:
        return max((len(h) for h in self._held.values()), default=0)

    # -- what CameraManager uses ------------------------------------------------

    def start(self) -> None:
        for sink in self._sinks.values():
            sink.et.start()

    @property
    def retired_reasons(self) -> list:
        """(camera index, reason) for every retirement this worker asked for."""
        return list(self._retire_req.items())

    def lag_frames(self) -> list:
        """The cross-camera lag lives in the parent's coordinator."""
        return []

    def live_encoders(self) -> int:
        return sum(1 for s in self._sinks.values() if s.et.is_alive())

    def dropped_full(self, cam: int) -> int:
        return self._sinks[cam].dropped_full

    def no_slot(self, cam: int) -> int:
        return self._sinks[cam].no_slot

    # -- collection -------------------------------------------------------------

    def _retire_locked(self, cam: int, reason: str) -> None:
        if cam in self._retire_req:
            return
        self._retire_req[cam] = reason
        self._ledger[cam].request_retire(reason)

    def _route_locked(self, cam: int, fates) -> None:
        sink = self._sinks[cam]
        held = self._held[cam]
        for t, released in fates:
            while held and held[0][0] < t:
                # A trigger announced here that the ledger never returned;
                # it cannot be placed, so its slot goes back.
                _t, _ts, buf = held.popleft()
                sink.give_back(buf)
            if not held or held[0][0] != t:
                continue
            _t, ts, buf = held.popleft()
            if released:
                if sink.route(t, ts, buf):
                    if len(sink.backlog) > self.backlog_peak:
                        self.backlog_peak = len(sink.backlog)
            else:
                sink.give_back(buf)

    def _collect_locked(self, cam: int) -> None:
        led = self._ledger[cam]
        sink = self._sinks[cam]
        if sink.backlog:
            sink.pump()
        fates = led.harvest()
        if fates:
            self._route_locked(cam, fates)
        if led.lag_error and cam not in self._retire_req:
            # The coordinator decided a ring's worth of triggers past one
            # this camera still holds, so the decision bits it would read may
            # belong to later triggers. Its held frames cannot be placed.
            self._retire_locked(cam, led.lag_error)
            self._route_locked(cam, led.drop_pending())

    def pump_idle(self) -> None:
        """Collect decisions for cameras that are not submitting (status
        thread). Never waits for a lock a grab thread holds, and does
        nothing once stop() or abandon() has begun, which collect on their
        own."""
        if self._stopping:
            return
        now = time.perf_counter()
        for cam in self.cams:
            if now - self._last_submit[cam] < IDLE_HARVEST_S:
                continue
            lock = self._locks[cam]
            if not lock.acquire(blocking=False):
                continue
            try:
                self._collect_locked(cam)
            finally:
                lock.release()

    def write_partial(self) -> None:
        """Append each camera's newly recorded frames to blockids.partial.

        A worker that dies mid-recording takes its block-ID list with it,
        and its stream.h264 is partial. This file keeps, in encoder order,
        the (block ID, timestamp) of every frame handed to the encoder up to
        the last append, so the stream's frames can still be placed. The
        stream may hold fewer frames than the file lists (the encoder had
        not finished them), never more.
        """
        if self._stopping:
            return
        for cam, sink in self._sinks.items():
            n = len(sink.block_ids)
            w = self._partial_written[cam]
            if n - w < PARTIAL_EVERY:
                continue
            ids = sink.block_ids[w:n]
            ts = sink.timestamps[w:n]
            k = min(len(ids), len(ts))
            rec = np.empty(k, PARTIAL_DTYPE)
            rec["block_id"] = ids[:k]
            rec["timestamp_s"] = ts[:k]
            try:
                with open(sink.dir / "blockids.partial", "ab") as f:
                    f.write(rec.tobytes())
                self._partial_written[cam] = w + k
            except OSError as e:
                print(f"[w] cam{cam + 1}: blockids.partial append failed: {e}",
                      flush=True)

    # -- stop ---------------------------------------------------------------------

    def _thread_done(self, cam: int) -> bool:
        threads = self._threads_fn() if self._threads_fn else []
        for gt in threads:
            if getattr(gt, "_cam_index", None) == cam:
                running = gt.isRunning() if hasattr(gt, "isRunning") else False
                return (not running) or bool(getattr(gt, "retrieve_loop_exited",
                                                     False))
        return True

    def stop(self):
        """End of stream, the parent's flush, then the per-camera finalize.

        Called by CameraManager.stop_acquisition once the grab threads have
        stopped. Every camera whose grab loop has exited reaches end of
        stream in the ledger after its last announce; a camera whose thread
        is still running asks to retire instead, so the flush never waits
        for it. The parent flushes once every camera has done one or the
        other, and the final decisions are routed before the encoders drain.
        Returns (count, timestamps, block_ids) per camera, in `cams` order.
        """
        self._stopping = True
        for cam in self.cams:
            with self._locks[cam]:
                self._collect_locked(cam)
                if self._thread_done(cam):
                    self._ledger[cam].set_eos()
                else:
                    self._retire_locked(
                        cam, "its grab thread was still running when the "
                             "recording stopped")
        deadline = time.monotonic() + FLUSH_WAIT_S
        first = self._ledger[self.cams[0]]
        flushed = False
        while True:
            if first.flushed:
                flushed = True
                break
            if first.state == LedgerState.ABANDONED:
                break
            for cam in self.cams:
                with self._locks[cam]:
                    self._collect_locked(cam)
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)
        for cam in self.cams:
            with self._locks[cam]:
                self._collect_locked(cam)
                if self._held[cam]:
                    # Only an unflushed ledger leaves frames undecided.
                    self._route_locked(cam, self._ledger[cam].drop_pending())
        sinks = [self._sinks[c] for c in self.cams]
        if not flushed:
            for sink in sinks:
                sink.warn(
                    f"cam{sink.cam + 1}: the coordinator in the parent process "
                    f"did not finish deciding the recording's triggers within "
                    f"{FLUSH_WAIT_S:g} s of the stop, so the frames still "
                    f"undecided were dropped. This camera's video ends early; "
                    f"blockids.npy matches it.")
        sync_encode.drain_backlogs(
            sinks, time.monotonic() + sync_encode.DRAIN_SENTINEL_TIMEOUT_S)
        sync_encode.finish_encoders(sinks)
        for sink in sinks:
            failure = sink.failure()
            if failure:
                self.encoder_failures.append(failure)
        sync_encode.reconcile_sinks(sinks)
        for cam in self.cams:
            refused = self._ledger[cam].refused_span
            if refused:
                self._sinks[cam].warn(
                    f"cam{cam + 1}: {refused} frames were dropped before they "
                    f"were announced, because the coordinator in the parent "
                    f"process had stopped deciding triggers. They are gaps in "
                    f"this camera's blockids.npy.")
        for sink in sinks:
            sink.write_warnings_file()
            try:
                (sink.dir / "blockids.partial").unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"[w] cam{sink.cam + 1}: could not remove "
                      f"blockids.partial: {e}", flush=True)
        return [(len(s.block_ids), s.timestamps, s.block_ids) for s in sinks]

    def abandon(self, timeout_s: float = sync_encode.ABANDON_TIMEOUT_S) -> None:
        """Tear down without draining, as SyncEncodeRouter.abandon does."""
        self._stopping = True
        deadline = time.monotonic() + max(0.0, timeout_s)
        for cam in self.cams:
            with self._locks[cam]:
                sink = self._sinks[cam]
                self._held[cam].clear()
                sink.backlog.clear()
        for cam in self.cams:
            self._sinks[cam].abandon(deadline)
        gc.collect()


# -- logging --------------------------------------------------------------------

class PipeLog:
    """sys.stdout / sys.stderr of a worker: lines up the log pipe, and a
    copy in the worker's own log file.

    A print never waits on the pipe: lines go on a queue and a sender
    thread writes them, so a parent that is slow to read cannot stall a
    grab thread that prints. The file keeps every line when the parent is
    gone; a line still queued when the process dies is lost from both.
    """

    def __init__(self, conn, path: Path | None):
        self._conn = conn
        self._lock = threading.Lock()
        self._partial = ""
        self._lines: deque = deque()
        self._wake = threading.Event()
        self._file = None
        self.encoding = "utf-8"
        self.errors = "replace"
        if path is not None:
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self._file = open(path, "a", encoding="utf-8",
                                  errors="replace", buffering=1)
            except OSError:
                self._file = None
        self._thread = threading.Thread(target=self._send_loop, daemon=True,
                                        name="worker-log")
        self._thread.start()

    @property
    def file(self):
        return self._file

    def write(self, s) -> int:
        s = str(s)
        with self._lock:
            text = self._partial + s
            parts = text.split("\n")
            self._partial = parts.pop()
            if parts:
                self._lines.extend(parts)
                self._wake.set()
        return len(s)

    def flush(self) -> None:
        self._wake.set()

    def isatty(self) -> bool:
        return False

    def drain(self, timeout_s: float = 2.0) -> None:
        """Send what is queued, the unfinished line included."""
        with self._lock:
            if self._partial:
                self._lines.append(self._partial)
                self._partial = ""
        self._wake.set()
        end = time.monotonic() + timeout_s
        while self._lines and time.monotonic() < end:
            time.sleep(0.01)

    def _send_loop(self) -> None:
        conn = self._conn
        while True:
            self._wake.wait(0.5)
            self._wake.clear()
            while self._lines:
                line = self._lines.popleft()
                if self._file is not None:
                    try:
                        self._file.write(line + "\n")
                    except (OSError, ValueError):
                        self._file = None
                if conn is not None:
                    try:
                        conn.send_bytes(line.encode("utf-8", "replace"))
                    except (OSError, EOFError, ValueError):
                        conn = None


# -- status -----------------------------------------------------------------------

class _Status:
    """The status thread: counters, heartbeat, preview and full-resolution
    copies, and the harvest of silent cameras, every STATUS_PERIOD_S."""

    def __init__(self, worker: "Worker"):
        self.w = worker
        self._stop = threading.Event()
        self._last_preview: dict = {}
        self._last_full: dict = {}
        self._last_snap: dict = {}
        self._next_partial = 0.0
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="worker-status")

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self.thread.join(timeout=2.0)

    def _run(self):
        while not self._stop.wait(STATUS_PERIOD_S):
            try:
                self._tick()
            except Exception as e:
                print(f"[w{self.w.wid}] status update failed: "
                      f"{type(e).__name__}: {e}", flush=True)

    def _tick(self):
        w = self.w
        st = w.status
        if st is None:
            return
        now = shm.now_ns()
        threads = {getattr(gt, "_cam_index", None): gt
                   for gt in list(w.mgr._grab_threads)}
        router = w.router
        preview, frames = w.preview, w.frames
        for k, g in enumerate(w.cams):
            st.beat(k, now)
            st.set(k, "state", w.state)
            gt = threads.get(g)
            if gt is None:
                continue
            st.set(k, "frame_count", int(gt.frame_count))
            st.set(k, "rearms", int(getattr(gt, "rearms", 0)))
            st.set(k, "drops_local", int(getattr(gt, "drops", 0)))
            st.set(k, "failed_grabs", int(getattr(gt, "failed_grabs", 0)))
            st.set(k, "ring_refused", int(getattr(gt, "ring_full_drops", 0)))
            st.set(k, "retrieve_loop_exited",
                   int(bool(getattr(gt, "retrieve_loop_exited", False))
                       or not gt.isRunning()))
            ready = getattr(gt, "ready", None)
            st.set(k, "ready", int(bool(ready is not None and ready.is_set())))
            st.set(k, "early_frame", int(getattr(gt, "frames_before_barrier", 0)))
            st.set(k, "current_fps", float(gt.current_fps))
            st.set(k, "delivery_lag_s", float(getattr(gt, "delivery_lag_s", 0.0)))
            pin = getattr(gt, "pin_result", None) or {}
            if pin.get("cpu") is not None:
                try:
                    st.set(k, "pinned_cpu", int(pin["cpu"]))
                except (TypeError, ValueError):
                    pass
            if router is not None:
                sink = router._sinks.get(g)
                st.set(k, "sessions_held",
                       int(sink is not None and sink.et is not None
                           and sink.et.is_alive()))
            if preview is not None:
                fr = gt.latest_frame
                if fr is not None and fr is not self._last_preview.get(k):
                    self._last_preview[k] = fr
                    if fr.size <= preview.capacity:
                        st.set(k, "preview_seq",
                               preview.write(k, fr, frame_n=int(gt.frame_count)))
            if frames is not None:
                pair = getattr(gt, "latest_full_with_bid", None)
                if (pair is not None and gt._keep_full
                        and pair is not self._last_full.get(k)):
                    self._last_full[k] = pair
                    full, bid = pair
                    if full is not None and full.size <= frames.capacity:
                        st.set(k, "full_seq", frames.write(
                            k, "full", full, frame_n=int(gt.frame_count),
                            bid=-1 if bid is None else int(bid)))
                snap = gt.snapshot_frame
                if snap is not None and snap is not self._last_snap.get(k):
                    self._last_snap[k] = snap
                    if snap.size <= frames.capacity:
                        st.set(k, "snapshot_seq",
                               frames.write(k, "snapshot", snap))
        if router is not None:
            router.pump_idle()
            if time.monotonic() >= self._next_partial:
                self._next_partial = time.monotonic() + 1.0
                router.write_partial()


# -- the worker --------------------------------------------------------------------

class WorkerError(RuntimeError):
    """A request the worker could not carry out; `kind` names the error
    class for the parent, `data` carries what the parent still needs."""

    def __init__(self, message: str, kind: str = "error", data=None):
        super().__init__(message)
        self.kind = kind
        self.data = data


class Worker:
    """State and request handlers of one capture worker process."""

    def __init__(self, args: dict, conn, gate):
        from gui_app.backends import load_backend
        from gui_app.camera_manager import CameraManager

        self.args = args
        self.wid = int(args["worker"])
        self.cams = [int(g) for g, _s in args["cameras"]]
        self.serials = [str(s) for _g, s in args["cameras"]]
        self.profile = args["profile"]
        self.parent_pid = int(args["parent_pid"])
        self.conn = conn
        self.gate = gate
        self.state = shm.WorkerState.SPAWNED
        backend = load_backend(args["backend"])
        restore = getattr(backend, "restore_spawn_state", None)
        if args.get("backend_state") is not None:
            if restore is None:
                raise RuntimeError(f"the {args['backend']} backend cannot take "
                                   f"a parent's spawn state")
            restore(args["backend_state"])
        self.mgr = CameraManager(args["backend"])
        self.mgr._backend = backend
        self.mgr.global_indices = list(self.cams)
        self.mgr.report_source_silence = False
        self.mgr.announce_unlisted = False
        self._rig_seg = self._rig = None
        rig = args.get("rig")
        if rig is not None:
            self._rig_seg = shm.SharedSegment.attach(rig[0])
            self._rig = shm.RecordSegment.attach(self._rig_seg.buf, RIG_FIELDS,
                                                 epoch=int(rig[1]))
            self.mgr.source_down_check = self._source_down
        # The status segment exists from the start, so the parent can watch
        # this worker's heartbeat before any camera opens.
        epoch = shm.new_epoch()
        size = shm.StatusSegment.size_for(len(self.cams))
        self._status_seg = shm.SharedSegment.create(
            shm.segment_name("status", parent_pid=self.parent_pid,
                             worker=self.wid, epoch=epoch), size)
        self.status = shm.StatusSegment.create(
            self._status_seg.buf, len(self.cams), epoch=epoch, worker=self.wid,
            global_indices=self.cams)
        self.status_spec = (self._status_seg.name, epoch)
        self.preview = self.frames = None
        self._image_segs: list = []
        self.router = None
        self._ledger_seg = None
        self._ledgers: list = []
        self._status_thread = _Status(self)
        self._status_thread.start()

    # -- helpers ------------------------------------------------------------------

    def _source_down(self) -> bool:
        rec = self._rig
        if rec is None:
            return False
        got = rec.record.read()
        return bool(got and got["source_down"] > 0)

    def _name(self, g: int) -> str:
        return f"cam{g + 1}"

    def _apply_config(self, msg: dict) -> None:
        """The manager flags and the process-wide placement settings the
        parent had in force, applied after the profile's own."""
        for name, value in (msg.get("flags") or {}).items():
            setattr(self.mgr, name, value)
        aff = msg.get("affinity") or {}
        if aff:
            try:
                from gui_app import cpu_affinity as ca
                if aff.get("core_order") is not None:
                    ca.set_core_order(list(aff["core_order"]))
                if aff.get("grab_priority") is not None:
                    ca.GRAB_THREAD_PRIORITY = int(aff["grab_priority"])
                if "overflow_priority" in aff:
                    ca.set_overflow_priority(aff["overflow_priority"])
            except Exception as e:
                print(f"[w{self.wid}] could not apply the capture-core "
                      f"settings: {e}", flush=True)

    def _close_images(self) -> None:
        for seg in (self.preview, self.frames):
            if seg is not None:
                seg.close()
        self.preview = self.frames = None
        for s in self._image_segs:
            try:
                s.close()
            except BufferError:
                pass
        self._image_segs = []

    def _close_ledger(self) -> None:
        for led in self._ledgers:
            led.close()
        self._ledgers = []
        if self._ledger_seg is not None:
            try:
                self._ledger_seg.close()
            except BufferError:
                pass
            self._ledger_seg = None
        self.router = None

    def _threads_by_cam(self) -> dict:
        return {getattr(gt, "_cam_index", None): gt
                for gt in list(self.mgr._grab_threads)}

    # -- ops ---------------------------------------------------------------------

    def op_open(self, msg: dict) -> dict:
        from gui_app import rig_setup
        self._close_images()
        rig_setup.apply_profile_to_manager(self.mgr, self.profile)
        self._apply_config(msg)
        kwargs = dict(msg.get("kwargs") or {})
        kwargs.pop("backend", None)
        kwargs["only_serials"] = list(self.serials)
        kwargs["expect_cameras"] = len(self.serials)
        ok = self.mgr.open_all(**kwargs)
        if not ok:
            raise WorkerError(str(self.mgr.last_open_error or ok or
                                  "the cameras could not be opened"),
                              kind="CameraOpenError")
        w, h = self.mgr.geometry
        n = len(self.cams)
        d = _downsample_default()
        cap = (-(-h // d)) * (-(-w // d))
        pe = shm.new_epoch()
        pseg = shm.SharedSegment.create(
            shm.segment_name("preview", parent_pid=self.parent_pid,
                             worker=self.wid, epoch=pe),
            shm.PreviewSegment.size_for(n, cap))
        fe = shm.new_epoch()
        fseg = shm.SharedSegment.create(
            shm.segment_name("frames", parent_pid=self.parent_pid,
                             worker=self.wid, epoch=fe),
            shm.FrameSegment.size_for(n, w * h))
        self._image_segs = [pseg, fseg]
        self.preview = shm.PreviewSegment.create(pseg.buf, n, cap, epoch=pe)
        self.frames = shm.FrameSegment.create(fseg.buf, n, w * h, epoch=fe)
        self.state = shm.WorkerState.OPEN
        return {"geometry": [w, h], "camera_info": self.mgr.camera_info,
                "preview": (pseg.name, pe), "frames": (fseg.name, fe),
                "baseline": [list(x) for x in self.mgr._baseline_exp_gain]}

    def op_arm(self, msg: dict) -> dict:
        self._close_ledger()
        self._apply_config(msg)
        spec: FactorySpec = msg["factory"]
        upload = msg.get("nvenc_upload")
        if spec.nvenc and upload:
            from gui_app import nvenc
            nvenc.configure_upload(upload["upload"], upload["context"])
        factory = build_factory(spec)
        if spec.nvenc and msg.get("realtime"):
            # The first Encode in a process imports more of PyNvVideoCodec,
            # and encoder threads that make it at once wedge the process.
            # One throwaway encoder warms the path in use, under a gate
            # shared by every worker, so the extra session is one at a time
            # across the rig.
            with self.gate:
                self._warm_nvenc(upload)
        self.mgr.encoder_factory = factory
        self._ledger_seg, self._ledgers = attach_shared(
            msg["ledger"][0], self.cams, epoch=int(msg["ledger"][1]))
        cams, ledgers = list(self.cams), list(self._ledgers)

        def make_router(raw_paths, width, height, quality, **kw):
            return WorkerRouter(cams, ledgers, raw_paths, width, height,
                                quality, threads_fn=lambda:
                                list(self.mgr._grab_threads), **kw)

        self.mgr.router_factory = make_router
        from gui_app.camera_manager import AcquisitionStartRefused
        try:
            self.mgr.start_acquisition(
                [Path(p) for p in msg["raw_paths"]],
                display_every=msg["display_every"], realtime=msg["realtime"],
                width=msg["width"], height=msg["height"],
                quality=msg["quality"], fps=msg["fps"],
                realtime_kick=msg["realtime_kick"],
                kick_max_lag=msg["kick_max_lag"],
                exposure_us=msg.get("exposure_us"), gain_db=msg.get("gain_db"))
        except AcquisitionStartRefused as e:
            self._close_ledger()
            raise WorkerError(str(e), kind="AcquisitionStartRefused",
                              data={"encoder_failures":
                                    list(self.mgr.last_encoder_failures)})
        self.router = self.mgr._router
        self.state = shm.WorkerState.ARMED
        return {"warnings": list(self.mgr.last_warnings)}

    def _warm_nvenc(self, upload) -> None:
        try:
            from gui_app import nvenc
            if not nvenc.available():
                return
            if upload and upload.get("upload") == "pinned":
                enc = nvenc.create_h264_encoder(256, 256, 21, fps=100)
                try:
                    enc.Encode(np.full((384, 256), 128, np.uint8))
                    enc.EndEncode()
                finally:
                    close = getattr(enc, "Close", None)
                    if close is not None:
                        close()
                    del enc
                    gc.collect()
        except Exception as e:
            print(f"[w{self.wid}] nvenc warm-up skipped: {e}", flush=True)

    def op_ready(self, msg: dict) -> dict:
        ready, total = self.mgr.wait_until_ready(float(msg.get("timeout", 30.0)))
        t_ns = shm.now_ns()
        threads = list(self.mgr._grab_threads)
        not_ready = [self.cams[i] for i in self.mgr.not_ready()]
        retired = {self._name(getattr(gt, "_cam_index", 0)): str(r)
                   for gt in threads
                   if (r := getattr(gt, "retired_reason", None))}
        pins = {getattr(gt, "_cam_index", 0): getattr(gt, "pin_result", None)
                for gt in threads}
        native = {} if self.router is None else dict(self.router.native_ids)
        return {"ready": ready, "total": total, "not_ready": not_ready,
                "retired": retired, "pins": pins, "ready_ns": t_ns,
                "native_ids": native, "pid": os.getpid()}

    def op_mark(self, msg: dict) -> dict:
        self.mgr.mark_board_starting()
        return self._barrier_counts()

    def _barrier_counts(self) -> dict:
        threads = list(self.mgr._grab_threads)
        counts = self.mgr.frames_before_barrier()
        retired = {self._name(getattr(gt, "_cam_index", 0)): str(r)
                   for gt in threads
                   if (r := getattr(gt, "retired_reason", None))}
        return {"frames_before_barrier": counts, "retired": retired}

    def op_barrier_counts(self, msg: dict) -> dict:
        return self._barrier_counts()

    def op_go(self, msg: dict) -> dict:
        self.mgr.signal_triggers_started()
        self.state = shm.WorkerState.RUNNING
        return {}

    def op_stop(self, msg: dict) -> dict:
        from gui_app.camera_manager import AcquisitionStopIncomplete
        router = self.router
        # Taken first: stop_acquisition empties the manager's thread list.
        threads = self._threads_by_cam()
        stuck = []
        try:
            results = self.mgr.stop_acquisition()
        except AcquisitionStopIncomplete as e:
            results = e.results
            stuck = [self.cams[i] for i in e.stuck]
        details = {}
        for k, g in enumerate(self.cams):
            gt = threads.get(g)
            if gt is None:
                continue
            details[g] = {
                "frames_retrieved": int(getattr(gt, "frames_retrieved", 0)),
                "frame_count": int(getattr(gt, "frame_count", 0)),
                "failed_grabs": int(getattr(gt, "failed_grabs", 0)),
                "failed_grabs_by_second": [
                    [int(s), int(c)] for s, c in gt.failed_grabs_per_second()],
                "frames_before_barrier": int(getattr(gt, "frames_before_barrier", 0)),
                "rearms": int(getattr(gt, "rearms", 0)),
                "ring_full_drops": int(getattr(gt, "ring_full_drops", 0)),
                "drops": int(getattr(gt, "drops", 0)),
                "retired_reason": getattr(gt, "retired_reason", None),
                "pin_result": getattr(gt, "pin_result", None),
                "source_down_stalls": int(getattr(gt, "source_down_stalls", 0)),
                "source_down_since": getattr(gt, "source_down_since", None),
                "stream_stats_at_stop": getattr(gt, "stream_stats_at_stop", None),
                "dropped_full": (router.dropped_full(g) if router is not None
                                 else 0),
                "no_slot": router.no_slot(g) if router is not None else 0,
            }
        records = getattr(self.mgr.encoder_factory, "records", None)
        out = {"results": [(int(c), list(ts), list(ids))
                           for c, ts, ids in results],
               "warnings": list(self.mgr.last_warnings),
               "retired": dict(self.mgr.last_retired),
               "encoder_failures": list(self.mgr.last_encoder_failures),
               "stream_stats": list(self.mgr.last_stream_stats),
               "details": details, "stuck": stuck,
               "backlog_peak": 0 if router is None else router.backlog_peak,
               "encoder_records": list(records) if records else []}
        if records:
            records.clear()
        self._close_ledger()
        self.state = shm.WorkerState.STOPPED
        return out

    def op_cancel(self, msg: dict) -> dict:
        self.mgr.cancel_acquisition()
        self._close_ledger()
        self.state = shm.WorkerState.OPEN
        return {}

    def op_resume_preview(self, msg: dict) -> dict:
        self.mgr.resume_preview(float(msg.get("preview_fps", 30.0)))
        self.state = shm.WorkerState.OPEN
        return {}

    def op_set_keep_full(self, msg: dict) -> dict:
        self.mgr.set_keep_full(bool(msg.get("flag")))
        return {}

    def op_snapshot(self, msg: dict) -> dict:
        self.mgr.request_snapshots()
        return {}

    def op_thermals(self, msg: dict) -> dict:
        return {"readings": self.mgr.thermals()}

    def op_abandon(self, msg: dict) -> dict:
        self.mgr.abandon()
        self._close_ledger()
        self.state = shm.WorkerState.ABANDONED
        return {"closed": True}

    def op_close(self, msg: dict) -> dict:
        if self.mgr._router is not None:
            self.mgr.abandon()
        else:
            self.mgr.close_all()
        self._close_ledger()
        self.state = shm.WorkerState.CLOSED
        return {"closed": True}

    # -- loop ----------------------------------------------------------------------

    def handle(self, msg: dict) -> dict:
        op = msg.get("op")
        fn = getattr(self, f"op_{op}", None)
        if fn is None:
            return {"id": msg.get("id"), "ok": False, "kind": "error",
                    "error": f"unknown request {op!r}"}
        try:
            return {"id": msg.get("id"), "ok": True, "result": fn(msg)}
        except WorkerError as e:
            return {"id": msg.get("id"), "ok": False, "kind": e.kind,
                    "error": str(e), "data": e.data}
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[w{self.wid}] {op} failed: {type(e).__name__}: {e}\n{tb}",
                  flush=True)
            return {"id": msg.get("id"), "ok": False, "kind": "error",
                    "error": f"{type(e).__name__}: {e}"}

    def shutdown(self) -> None:
        self._status_thread.stop()
        self._close_ledger()
        self._close_images()
        if self.status is not None:
            self.status.close()
            self.status = None
        try:
            self._status_seg.close()
        except BufferError:
            pass
        if self._rig is not None:
            self._rig.close()
            self._rig = None
            try:
                self._rig_seg.close()
            except BufferError:
                pass
        release = getattr(self.mgr._backend_obj, "release_spawn_state", None)
        if release is not None:
            release()


def _watch_parent(log: PipeLog | None) -> None:
    """Exit when the parent process does."""
    import multiprocessing
    parent = multiprocessing.parent_process()
    if parent is None:
        return
    from multiprocessing.connection import wait
    wait([parent.sentinel])
    print("[w] the parent process is gone; this capture worker exits",
          flush=True)
    if log is not None:
        log.drain(1.0)
    os._exit(4)


def worker_main(args: dict, conn, log_conn, gate) -> None:
    """Entry point of a capture worker process (multiprocessing target).

    `args` is the dict ProcessCameraManager builds (every value picklable),
    `conn` the duplex control pipe, `log_conn` the write end of the log pipe
    and `gate` the cross-process semaphore that serialises NVENC warm-ups.
    """
    log = PipeLog(log_conn, args.get("log_path"))
    sys.stdout = log
    sys.stderr = log
    if log.file is not None:
        try:
            faulthandler.enable(file=log.file, all_threads=True)
        except Exception:
            pass
    threading.Thread(target=_watch_parent, args=(log,), daemon=True,
                     name="worker-parent-watch").start()
    si = args.get("switch_interval")
    if si:
        sys.setswitchinterval(float(si))
    from gui_app.mp.winjob import opt_out_of_power_throttling
    why = opt_out_of_power_throttling()
    if why:
        print(f"[w{args.get('worker')}] power throttling left as Windows "
              f"sets it: {why}", flush=True)
    from PyQt5.QtCore import QCoreApplication
    app = QCoreApplication.instance() or QCoreApplication([])
    wid = args.get("worker")
    try:
        worker = Worker(args, conn, gate)
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[w{wid}] could not start: {type(e).__name__}: {e}\n{tb}",
              flush=True)
        try:
            conn.send({"event": "fatal", "error": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
        log.drain()
        return
    print(f"[w{wid}] capture worker pid {os.getpid()} for "
          + ", ".join(f"cam{g + 1}" for g in worker.cams), flush=True)
    conn.send({"event": "hello", "pid": os.getpid(),
               "status": worker.status_spec})
    try:
        while True:
            if conn.poll(STATUS_PERIOD_S):
                try:
                    msg = conn.recv()
                except (EOFError, OSError):
                    break
                reply = worker.handle(msg)
                try:
                    conn.send(reply)
                except (OSError, EOFError):
                    break
                if msg.get("op") == "close":
                    break
            app.processEvents()
    finally:
        try:
            worker.shutdown()
        except Exception as e:
            print(f"[w{wid}] shutdown: {type(e).__name__}: {e}", flush=True)
        log.drain()
