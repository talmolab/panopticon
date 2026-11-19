"""Closed-loop stimulation: track one camera live and tell the trigger board
whether a mouse is in the zone.

The tracker runs in a worker process (gui_app/tracker_worker.py), started
with the interpreter the profile's ``closed_loop.python`` names, so its ONNX
runtime and GPU context stay out of the capture process. One grab thread
publishes a full-resolution frame at ``rate_hz`` (GrabThread.set_tap); a
thread here copies each new one into shared memory, asks the worker for the
mice in it, decides the zone state and, during a recording, sends it to the
board (TeensyController.send_zone), which runs the paradigm's zone blocks
only while the zone is in.

The board turns the zone off when no zone byte has come for
stim_compiler.ZONE_WATCHDOG_MS, so this thread resends the state every
RESEND_S whether or not a new frame came, and turns the zone off itself once
no frame has been tracked for ``lost_s``.

Nothing here imports Qt. The window polls ``ClosedLoop.snapshot()``.
"""
import csv
import json
import math
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from gui_app.stim_compiler import ZONE_WATCHDOG_MS
from gui_app.stim_trace import CLOSED_LOOP_NAME

#: The per-tracked-frame record of a recording, beside closed_loop.json.
CLOSED_LOOP_CSV = "closed_loop.csv"
#: Seconds between zone bytes when no new frame forces one. A fifth of the
#: board's watchdog, so four in a row can be lost before the zone drops.
RESEND_S = ZONE_WATCHDOG_MS / 5 / 1000.0
#: Seconds the worker may take to load its model and run its first frames.
#: A GPU new to the runtime compiles its kernels on the first run.
STARTUP_TIMEOUT_S = 180.0
#: Seconds one frame may take before the worker counts as stalled.
INFER_TIMEOUT_S = 2.0
#: A recording's warnings name a tracked rate below this share of rate_hz,
#: and frames with no mouse found above this share.
SLOW_RATE_FRACTION = 0.8
EMPTY_FRAME_FRACTION = 0.2
#: Tracker failures a recording's record keeps the reason of; the rest are
#: counted.
MAX_ERRORS = 50

WORKER = Path(__file__).with_name("tracker_worker.py")


class TrackerError(RuntimeError):
    """The worker process could not start, or stopped answering."""


# ------------------------------------------------------------------ the zone
def point_in_polygon(x: float, y: float, poly) -> bool:
    """Even-odd rule: whether (x, y) lies inside the polygon ``poly``, a
    sequence of (x, y) vertices."""
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            if x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
    return inside


@dataclass
class Zone:
    """Where a mouse turns the zone on: polygons in the tracked camera's
    full-resolution pixels, any of which counts.

    Saved and loaded as JSON beside nothing else: a zone is chosen for a
    session in the Zone window, never restored at launch, because it decides
    what reaches the board.
    """
    camera: str
    width: int
    height: int
    polygons: list = field(default_factory=list)

    def contains(self, x: float, y: float) -> bool:
        return any(len(p) >= 3 and point_in_polygon(x, y, p)
                   for p in self.polygons)

    @property
    def empty(self) -> bool:
        return not any(len(p) >= 3 for p in self.polygons)

    def to_dict(self) -> dict:
        return {"camera": self.camera, "frame_size": [self.width, self.height],
                "polygons": [[[round(float(x), 1), round(float(y), 1)]
                              for x, y in p] for p in self.polygons]}

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path, camera: str, width: int, height: int) -> "Zone":
        """Read a zone file, refusing one drawn on another camera or frame
        size: its pixels would name other places."""
        d = json.loads(Path(path).read_text(encoding="utf-8"))
        if d.get("camera") != camera:
            raise ValueError(f"the zone was drawn on {d.get('camera')}, and "
                             f"this profile tracks {camera}")
        size = [int(v) for v in d.get("frame_size", [])]
        if size != [width, height]:
            raise ValueError(f"the zone was drawn on {size[0]}x{size[1]} "
                             f"frames, and the camera gives {width}x{height}"
                             if len(size) == 2 else
                             "the zone file gives no frame_size")
        polys = []
        for p in d.get("polygons", []):
            pts = [(float(x), float(y)) for x, y in p]
            if len(pts) < 3:
                raise ValueError("a polygon in the zone file has fewer than "
                                 "3 corners")
            if not all(math.isfinite(v) for pt in pts for v in pt):
                raise ValueError("a polygon in the zone file has a corner "
                                 "that is not a number")
            polys.append(pts)
        return cls(camera, width, height, polys)


class ZoneLogic:
    """The zone state from tracked frames: on after ``enter`` consecutive
    frames with a confident mouse in the zone, off after ``leave``
    consecutive frames without one, and off at once when tracking is lost.
    """

    def __init__(self, min_confidence: float, enter: int, leave: int):
        self.min_confidence = float(min_confidence)
        self.enter, self.leave = int(enter), int(leave)
        self.zone: Zone | None = None
        self.reset()

    def reset(self) -> None:
        self.state = False
        self._run_in = 0
        self._run_out = 0

    def update(self, dets):
        """Take one frame's detections [(x, y, confidence), ...].

        Returns (kept, inside, state): the detections at or above
        min_confidence, whether each lies in the zone, and the zone state
        after this frame.
        """
        kept = [d for d in dets if d[2] >= self.min_confidence]
        zone = self.zone
        inside = ([zone.contains(d[0], d[1]) for d in kept]
                  if zone is not None else [False] * len(kept))
        if any(inside):
            self._run_in += 1
            self._run_out = 0
        else:
            self._run_out += 1
            self._run_in = 0
        if not self.state and self._run_in >= self.enter:
            self.state = True
        elif self.state and self._run_out >= self.leave:
            self.state = False
        return kept, inside, self.state


# ---------------------------------------------------------- the worker process
class TrackerProcess:
    """One tracker_worker.py process and the shared memory it reads frames
    from. ``start()`` blocks until the worker is ready or refuses."""

    def __init__(self, spec, height: int, width: int):
        self.spec = spec
        self.height, self.width = int(height), int(width)
        self.info: dict = {}
        self._p = None
        self._shm = None
        self._frame = None
        self._lines: queue.Queue = queue.Queue()
        self._seq = 0

    def command(self, shm_name: str) -> list:
        s = self.spec
        cmd = [s.python or sys.executable, str(WORKER), "--tracker", s.tracker,
               "--shm", shm_name, "--height", str(self.height),
               "--width", str(self.width)]
        if s.tracker == "onnx":
            cmd += ["--model", str(s.model)]
        else:
            cmd += ["--threshold", str(s.threshold),
                    "--min-area-px", str(s.min_area_px)]
        return cmd

    def start(self, timeout_s: float = STARTUP_TIMEOUT_S) -> dict:
        from multiprocessing import shared_memory
        from gui_app.ffmpeg_cmd import quiet_popen_kwargs
        self._shm = shared_memory.SharedMemory(
            create=True, size=self.height * self.width)
        self._frame = np.ndarray((self.height, self.width), np.uint8,
                                 buffer=self._shm.buf)
        cmd = self.command(self._shm.name)
        print(f"[tracker] starting: {' '.join(cmd)}", flush=True)
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0, **quiet_popen_kwargs())
        except OSError as e:
            self.close()
            raise TrackerError(f"could not start {cmd[0]}: {e}") from None
        self._p = proc
        # Each reader holds its own process, so a close() that lands during
        # the start cannot hand it a None.
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True,
                         name="tracker-out").start()
        threading.Thread(target=self._read_stderr, args=(proc,), daemon=True,
                         name="tracker-err").start()
        first = self._next(timeout_s)
        if first is None:
            self.close()
            raise TrackerError(f"the tracker did not get ready within "
                               f"{timeout_s:g} s")
        if not first.get("ready"):
            self.close()
            raise TrackerError(first.get("error") or f"the tracker refused: "
                               f"{first}")
        self.info = first
        return first

    def _read_stdout(self, proc) -> None:
        for raw in iter(proc.stdout.readline, b""):
            try:
                self._lines.put(json.loads(raw))
            except ValueError:
                print(f"[tracker] unreadable answer: {raw[:120]!r}", flush=True)
        self._lines.put(None)            # the worker has gone

    def _read_stderr(self, proc) -> None:
        for raw in iter(proc.stderr.readline, b""):
            # A native library on Windows can write UTF-16, which reads as
            # text with a NUL after every character.
            enc = "utf-16-le" if raw.count(b"\x00") > len(raw) // 4 else "utf-8"
            text = raw.decode(enc, "replace").replace("\x00", "").strip()
            if text:
                print(f"[tracker] {text}", flush=True)

    def _next(self, timeout_s: float):
        try:
            return self._lines.get(timeout=timeout_s)
        except queue.Empty:
            return None

    @property
    def alive(self) -> bool:
        return self._p is not None and self._p.poll() is None

    def infer(self, frame: np.ndarray, timeout_s: float = INFER_TIMEOUT_S):
        """(detections, worker milliseconds) for one frame. Raises
        TrackerError when the worker has gone or does not answer in time."""
        dest, p = self._frame, self._p
        if dest is None or p is None:
            raise TrackerError("the tracker has been closed")
        if frame.shape != dest.shape:
            raise TrackerError(f"the frame is {frame.shape[1]}x"
                               f"{frame.shape[0]}, and the tracker was started "
                               f"for {self.width}x{self.height}")
        if p.poll() is not None:
            raise TrackerError("the tracker process has exited")
        dest[...] = frame
        self._seq += 1
        seq = self._seq
        try:
            p.stdin.write(json.dumps({"seq": seq}).encode() + b"\n")
            p.stdin.flush()
        except OSError as e:
            raise TrackerError(f"the tracker stopped reading: {e}") from None
        deadline = time.perf_counter() + timeout_s
        while True:
            msg = self._next(max(0.0, deadline - time.perf_counter()))
            if msg is None:
                if not self.alive:
                    raise TrackerError("the tracker process has exited")
                raise TrackerError(f"no answer within {timeout_s:g} s")
            if msg.get("seq") != seq:
                continue                 # a late answer to a timed-out frame
            if "error" in msg:
                raise TrackerError(msg["error"])
            return msg.get("det", []), float(msg.get("ms", 0.0))

    def close(self, block: bool = True) -> None:
        """End the worker: closing its stdin ends it, and one still running
        3 s later is killed. ``block`` False leaves that wait to a background
        thread, so the window never waits on a worker."""
        p, self._p = self._p, None
        if p is not None:
            try:
                p.stdin.close()
            except Exception:
                pass
            if block:
                _reap(p)
            else:
                threading.Thread(target=_reap, args=(p,), daemon=True,
                                 name="tracker-reap").start()
        self._frame = None
        shm, self._shm = self._shm, None
        if shm is not None:
            try:
                shm.close()
                shm.unlink()
            except (BufferError, FileNotFoundError, OSError) as e:
                # A frame copy still in flight holds a view; the block goes
                # when its last handle does.
                print(f"[tracker] shared memory left to the system: {e}",
                      flush=True)


def _reap(p) -> None:
    """Wait for a worker whose stdin is closed, and kill it if it stays."""
    try:
        p.wait(timeout=3)
    except Exception:
        p.kill()
        try:
            p.wait(timeout=3)
        except Exception:
            pass


# ---------------------------------------------------------- the recording log
@dataclass
class RecordingLog:
    """What one recording's closed loop did, written at its finalize."""
    fps: float
    t0: float
    #: The zone the recording started with; the window locks it while the
    #: recording runs.
    zone: "Zone | None" = None
    rows: list = field(default_factory=list)
    events: list = field(default_factory=list)
    sent: int = 0
    refused: int = 0
    lapses: int = 0
    #: The first MAX_ERRORS tracker failures, and how many there were.
    errors: list = field(default_factory=list)
    error_count: int = 0
    #: (perf_counter of the grab, block ID) of the newest tracked frame,
    #: which places a send on the trigger count.
    ref: tuple | None = None
    board_state: bool = False
    last_ok: float | None = None

    def bid_at(self, t: float) -> int | None:
        """The first trigger a zone byte sent at perf_counter ``t`` can act
        on: the board reads zone bytes at each trigger period's boundary."""
        if self.ref is None or not self.fps:
            return None
        t_grab, bid = self.ref
        return int(bid) + int(math.floor((t - t_grab) * self.fps)) + 1

    def error(self, why: str) -> None:
        """Count a tracker failure, keeping the first MAX_ERRORS reasons."""
        self.error_count += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append(why)

    def event(self, t: float, state: bool, reason: str) -> None:
        bid = self.bid_at(t)
        if bid is None:
            return
        self.events.append({"t_s": round(t - self.t0, 4), "blockid": bid,
                            "zone": bool(state), "reason": reason})
        self.board_state = bool(state)


def write_recording(recording_dir, log: RecordingLog, spec,
                    tracker_info: dict) -> list:
    """Write closed_loop.csv and closed_loop.json into a recording folder.
    Returns the WARNINGS.txt lines this recording earned."""
    recording_dir = Path(recording_dir)
    with open(recording_dir / CLOSED_LOOP_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "blockid", "found", "kept", "in_zone", "zone",
                    "sent", "tracker_ms", "detections"])
        w.writerows(log.rows)
    n = len(log.rows)
    duration = (log.rows[-1][0] - log.rows[0][0]) if n > 1 else 0.0
    rate = (n - 1) / duration if duration > 0 else 0.0
    empty = sum(1 for r in log.rows if r[3] == 0)
    on_s = 0.0
    for a, b in zip(log.events, log.events[1:] + [None]):
        if a["zone"]:
            end = b["t_s"] if b else (log.rows[-1][0] if n else a["t_s"])
            on_s += max(0.0, end - a["t_s"])
    record = {
        "camera": spec.camera, "settings": spec.to_dict(),
        "tracker": tracker_info,
        "zone": log.zone.to_dict() if log.zone else None,
        "frame_rate": log.fps, "tracked_frames": n,
        "tracked_rate_hz": round(rate, 3), "frames_without_mouse": empty,
        "zone_on_s": round(on_s, 3), "zone_bytes_sent": log.sent,
        "zone_bytes_not_sent": log.refused, "watchdog_lapses": log.lapses,
        "tracker_errors": log.error_count,
        "tracker_error_reasons": log.errors, "events": log.events,
    }
    (recording_dir / CLOSED_LOOP_NAME).write_text(
        json.dumps(record, indent=2), encoding="utf-8")

    warn = []
    if n == 0:
        warn.append("closed loop: no frame was tracked, so the zone stayed "
                    "off and no zone block ran.")
    if log.error_count:
        warn.append(f"closed loop: the tracker failed {log.error_count} "
                    f"time(s), first: {log.errors[0]}. The zone was off "
                    f"while it failed.")
    if log.refused:
        warn.append(f"closed loop: {log.refused} zone update(s) did not reach "
                    f"the board (the log says why).")
    if log.lapses:
        warn.append(f"closed loop: zone updates stopped {log.lapses} time(s) "
                    f"for longer than the board's {ZONE_WATCHDOG_MS} ms "
                    f"watchdog, which turned the zone off each time.")
    if n > 1 and rate < SLOW_RATE_FRACTION * spec.rate_hz:
        warn.append(f"closed loop: tracked {rate:.1f} frames/s, below the "
                    f"rate_hz of {spec.rate_hz:g}.")
    if n and empty > EMPTY_FRAME_FRACTION * n:
        warn.append(f"closed loop: no mouse was found in {empty} of {n} "
                    f"tracked frames ({100 * empty / n:.0f}%).")
    return warn


# ------------------------------------------------------------- the loop thread
class ClosedLoop(threading.Thread):
    """Tracks one camera while it runs; sends the zone during a recording.

    Start it once the camera manager is up. ``begin_recording`` arms the
    sending after the board's start, ``end_recording`` disarms it before the
    stop and returns the recording's log. ``close`` stops everything.
    """

    def __init__(self, camera_mgr, spec, height: int, width: int):
        super().__init__(daemon=True, name="closed-loop")
        self.spec = spec
        self.cam_index = int(spec.camera[3:]) - 1
        self._mgr = camera_mgr
        self._proc = TrackerProcess(spec, height, width)
        self.logic = ZoneLogic(spec.min_confidence, spec.enter_updates,
                               spec.leave_updates)
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._controller = None
        self._log: RecordingLog | None = None
        self._last_send = 0.0
        self._last_update = 0.0
        self._status = "starting"
        self._error: str | None = None
        self._snap: dict = {}
        self._times: list = []
        #: Why frames are not being tracked, or None while they are.
        self._untracked: str | None = None

    # -------------------------------------------------------- window side
    def set_zone(self, zone: Zone | None) -> None:
        with self._lock:
            self.logic.zone = zone

    @property
    def ready(self) -> bool:
        return self._status == "running"

    @property
    def tracker_info(self) -> dict:
        return dict(self._proc.info)

    def snapshot(self) -> dict:
        """The newest tracked frame's result, for the Zone window: status,
        error, the kept detections and whether each is in the zone, the zone
        state, the tracked rate and the worker's time per frame."""
        with self._lock:
            return dict(self._snap, status=self._status, error=self._error,
                        armed=self._controller is not None)

    def begin_recording(self, controller, fps: float) -> None:
        """Arm the sending: from now on the zone goes to ``controller``,
        whose arm_zone() the caller has already called for this start."""
        with self._lock:
            self.logic.reset()
            self._log = RecordingLog(fps=float(fps), t0=time.perf_counter(),
                                     zone=self.logic.zone)
            self._controller = controller
            self._last_send = 0.0

    def end_recording(self) -> RecordingLog | None:
        """Disarm before the stop: one last 'out', then no more bytes.
        Returns the recording's log, or None when none was armed."""
        with self._lock:
            ctl, self._controller = self._controller, None
            log, self._log = self._log, None
        ok = ctl is not None and ctl.send_zone(False)
        if log is not None:
            now = time.perf_counter()
            if ok:
                log.sent += 1
            # Updates that stopped for good: the board's watchdog dropped
            # the zone that long after the last one that reached it.
            wd = ZONE_WATCHDOG_MS / 1000.0
            if (log.board_state and log.last_ok is not None
                    and now - log.last_ok > wd):
                log.lapses += 1
                log.event(log.last_ok + wd, False, "watchdog")
            if log.board_state:
                log.event(now, False, "end")
        return log

    def close(self) -> None:
        """Stop sending, stop the loop and end its worker. The window calls
        this on its own thread, so nothing here waits more than half a
        second: the worker is reaped in the background."""
        self.end_recording()
        self._halt.set()
        self._release_tap()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=0.5)
        self._proc.close(block=False)

    def _release_tap(self) -> None:
        try:
            self._mgr.set_tap(None, owner=self)
        except Exception as e:
            print(f"[closed-loop] could not stop the camera's tap: {e}",
                  flush=True)

    # ---------------------------------------------------------- the thread
    def run(self) -> None:
        try:
            try:
                info = self._proc.start()
            except Exception as e:
                self._fail(str(e) if isinstance(e, TrackerError)
                           else f"{type(e).__name__}: {e}")
                return
            if self._halt.is_set():
                return                   # closed while the worker started
            print(f"[closed-loop] tracker ready: {info}", flush=True)
            self._status = "running"
            self._mgr.set_tap(self.cam_index, self.spec.rate_hz, owner=self)
            last_seq = None
            while not self._halt.is_set():
                tap = self._mgr.latest_tap(self.cam_index)
                if tap is not None and tap[2] != last_seq:
                    last_seq = tap[2]
                    self._track(tap)
                else:
                    self._halt.wait(0.002)
                self._keep_alive()
                if not self._proc.alive:
                    self._fail("the tracker process has exited")
                    return
        except Exception as e:
            # Say why the thread ended, instead of a status left at running
            # with nothing behind it. The board's watchdog empties the zone
            # once the updates stop.
            self._fail(f"the closed loop stopped: {type(e).__name__}: {e}")
        finally:
            # The tap and the worker end with the loop, so a camera never
            # copies frames for a loop that is gone. A loop that is not
            # running is started afresh (MainWindow._ensure_closed_loop).
            self._release_tap()
            self._proc.close(block=False)
            if self._status == "running":
                self._status = "stopped"

    def _fail(self, why: str) -> None:
        print(f"[closed-loop] tracker unavailable: {why}", flush=True)
        with self._lock:
            self._status = "error"
            self._error = why
            self.logic.reset()
            if self._log is not None:
                self._log.error(why)

    def _track(self, tap) -> None:
        frame, bid, _seq, t_grab = tap[:4]
        try:
            dets, ms = self._proc.infer(frame)
        except TrackerError as e:
            with self._lock:
                self.logic.reset()
                if self._log is not None:
                    self._log.error(str(e))
            # Logged when the reason changes, never per frame.
            if str(e) != self._untracked:
                print(f"[closed-loop] frames not tracked: {e}", flush=True)
                self._untracked = str(e)
            return
        if self._untracked is not None:
            print("[closed-loop] tracking again", flush=True)
            self._untracked = None
        now = time.perf_counter()
        with self._lock:
            kept, inside, state = self.logic.update(dets)
            self._last_update = now
            self._times = [t for t in self._times if now - t < 2.0] + [now]
            log = self._log
            if log is not None and bid is not None:
                log.ref = (t_grab, bid)
            self._snap = {"t": now, "dets": kept, "inside": inside,
                          "state": state,
                          "found": len(dets), "ms": ms, "bid": bid,
                          "hz": (len(self._times) - 1) / 2.0}
        sent = self._send(state, "tracked") if log is not None else False
        if log is not None:
            log.rows.append([
                round(t_grab - log.t0, 4), "" if bid is None else int(bid),
                len(dets), len(kept), sum(inside), int(state), int(sent),
                round(ms, 2),
                ";".join(f"{x:.1f}:{y:.1f}:{c:.3f}" for x, y, c in dets)])

    def _keep_alive(self) -> None:
        """Between frames: resend the state, and turn the zone off once no
        frame has been tracked for lost_s."""
        if self._controller is None:
            return
        now = time.perf_counter()
        if now - self._last_send < RESEND_S:
            return
        with self._lock:
            reason = "resend"
            if self.logic.state and now - self._last_update > self.spec.lost_s:
                self.logic.reset()
                reason = "lost"
            self._send_locked(self.logic.state, reason)

    def _send(self, state: bool, reason: str) -> bool:
        # Under the lock, so end_recording() cannot detach the controller
        # between this check and the write, which would put an 'in' on the
        # board after its last 'out'. send_zone() never waits for the link.
        with self._lock:
            return self._send_locked(state, reason)

    def _send_locked(self, state: bool, reason: str) -> bool:
        ctl, log = self._controller, self._log
        if ctl is None or log is None:
            return False
        ok = ctl.send_zone(state)
        now = time.perf_counter()
        self._last_send = now
        if not ok:
            log.refused += 1
            return False
        log.sent += 1
        # A gap longer than the board's watchdog turned its zone off, so
        # the record has it off from the watchdog's expiry.
        if (log.last_ok is not None and log.board_state
                and now - log.last_ok > ZONE_WATCHDOG_MS / 1000.0):
            log.lapses += 1
            log.event(log.last_ok + ZONE_WATCHDOG_MS / 1000.0, False,
                      "watchdog")
        log.last_ok = now
        if state != log.board_state:
            log.event(now, state, reason)
        return True
