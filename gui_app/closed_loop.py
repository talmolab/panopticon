"""Closed-loop stimulation: track cameras live and tell the trigger board
whether a mouse is in each zone.

A zone is a set of shapes (polygons, rectangles, ellipses) drawn on one
camera and named by a letter (stim_compiler.ZONE_NAMES). A paradigm block
ticked In zone follows one zone. The tracker runs in a worker process
(gui_app/tracker_worker.py), started with the interpreter the profile's
``closed_loop.python`` names, so its ONNX runtime and GPU context stay out of
the capture process. Each camera a zone is drawn on publishes a
full-resolution frame at ``rate_hz`` (GrabThread.set_tap); a thread here
copies each new one into shared memory, asks the worker for the mice in it,
decides the state of every zone on that camera and, during a recording,
sends it to the board (TeensyController.send_zone), which runs each zone
block only while its zone is in.

The board turns a zone off when no byte for it has come for
stim_compiler.ZONE_WATCHDOG_MS, so this thread resends every zone's state
every RESEND_S whether or not a new frame came, and turns a zone off itself
once its camera has not been tracked for ``lost_s``.

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

from gui_app.stim_compiler import ZONE_NAMES, ZONE_WATCHDOG_MS
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


# ------------------------------------------------------------------ the zones
#: Each zone's colour wherever it is drawn, by letter: saturated hues that
#: stand out on a backlit floor and on a dark mouse alike. Never white or
#: black, which vanish on the cameras' views.
ZONE_COLOURS = {
    "A": "#ffb000", "B": "#00d0ff", "C": "#ff3cdc", "D": "#ff7a1a",
    "E": "#a678ff", "F": "#3c8cff", "G": "#ff5c8a", "H": "#00e0b0",
    "I": "#e0d000", "J": "#00a0ff", "K": "#d070ff", "L": "#ff9d5c",
    "M": "#5cc8ff", "N": "#ff70c0", "O": "#30ffd0", "P": "#ffd060"}
#: A tracked mouse's mark: lime, a hue no zone takes.
MOUSE_COLOUR = "#9dff3c"

#: The shapes a zone is drawn with. A polygon gives its corners; a rectangle
#: and an ellipse give two opposite corners of their bounding box.
SHAPE_KINDS = ("polygon", "rect", "ellipse")


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
class Shape:
    """One shape of a zone, in its camera's full-resolution pixels."""
    kind: str
    points: list

    def bounds(self) -> tuple:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return min(xs), min(ys), max(xs), max(ys)

    def contains(self, x: float, y: float) -> bool:
        if self.kind == "polygon":
            return len(self.points) >= 3 and point_in_polygon(x, y,
                                                              self.points)
        x0, y0, x1, y1 = self.bounds()
        if self.kind == "rect":
            return x0 <= x <= x1 and y0 <= y <= y1
        rx, ry = (x1 - x0) / 2, (y1 - y0) / 2
        if rx <= 0 or ry <= 0:
            return False
        cx, cy = x0 + rx, y0 + ry
        return ((x - cx) / rx) ** 2 + ((y - cy) / ry) ** 2 <= 1.0

    @property
    def usable(self) -> bool:
        """A shape with an area: three corners, or a box of some size."""
        if self.kind == "polygon":
            return len(self.points) >= 3
        x0, y0, x1, y1 = self.bounds()
        return x1 > x0 and y1 > y0

    def to_dict(self) -> dict:
        return {"kind": self.kind,
                "points": [[round(float(x), 1), round(float(y), 1)]
                           for x, y in self.points]}

    @classmethod
    def from_dict(cls, d: dict) -> "Shape":
        kind = d.get("kind")
        if kind not in SHAPE_KINDS:
            raise ValueError(f"a shape is {kind!r}, not one of "
                             f"{', '.join(SHAPE_KINDS)}")
        pts = [(float(x), float(y)) for x, y in d.get("points", [])]
        if not all(math.isfinite(v) for pt in pts for v in pt):
            raise ValueError(f"a {kind} has a corner that is not a number")
        need = 3 if kind == "polygon" else 2
        if len(pts) < need:
            raise ValueError(f"a {kind} needs {need} corners, and has "
                             f"{len(pts)}")
        return cls(kind, pts if kind == "polygon" else pts[:2])


@dataclass
class Zone:
    """A named zone: shapes on one camera, any of which counts. A paradigm
    block ticked In zone names the zone it follows."""
    name: str
    camera: str
    shapes: list = field(default_factory=list)

    def contains(self, x: float, y: float) -> bool:
        return any(s.usable and s.contains(x, y) for s in self.shapes)

    @property
    def empty(self) -> bool:
        return not any(s.usable for s in self.shapes)

    @property
    def cam_index(self) -> int:
        """Its camera's rig-wide index, 0-based."""
        return int(self.camera[3:]) - 1

    def to_dict(self) -> dict:
        return {"camera": self.camera,
                "shapes": [s.to_dict() for s in self.shapes]}


def zones_to_dict(zones: dict, width: int, height: int) -> dict:
    """Zones as a paradigm file stores them, with the frame size their
    pixels belong to."""
    return {"frame_size": [int(width), int(height)],
            "zones": {n: zones[n].to_dict() for n in sorted(zones)}}


def zones_from_dict(d, width: int, height: int,
                    n_cameras: int | None = None) -> dict:
    """The zones a paradigm file stores, or {} for none. Raises ValueError for
    zones drawn on frames of another size or a camera the rig lacks: their
    pixels would name other places."""
    if not d:
        return {}
    if not isinstance(d, dict):
        raise ValueError("the zones are not a mapping")
    size = [int(v) for v in d.get("frame_size", [])]
    if size != [int(width), int(height)]:
        raise ValueError(
            f"the zones were drawn on {size[0]}x{size[1]} frames, and the "
            f"cameras give {width}x{height}" if len(size) == 2 else
            "the zones give no frame_size")
    out = {}
    for name, z in (d.get("zones") or {}).items():
        if name not in ZONE_NAMES or len(name) != 1:
            raise ValueError(f"zone {name!r} is not one of {ZONE_NAMES}")
        cam = str(z.get("camera", ""))
        if not (cam.startswith("cam") and cam[3:].isdigit()
                and int(cam[3:]) >= 1):
            raise ValueError(f"zone {name}'s camera {cam!r} is not a camera "
                             f"name")
        if n_cameras and int(cam[3:]) > n_cameras:
            raise ValueError(f"zone {name} is drawn on {cam}, and the rig "
                             f"has {n_cameras} cameras")
        out[name] = Zone(name, cam, [Shape.from_dict(s)
                                     for s in z.get("shapes", [])])
    return out


class ZoneLogic:
    """One zone's state from tracked frames of its camera: on after
    ``enter`` consecutive frames with a confident mouse in the zone, off
    after ``leave`` consecutive frames without one, and off at once when
    tracking is lost.
    """

    def __init__(self, min_confidence: float, enter: int, leave: int):
        self.min_confidence = float(min_confidence)
        self.enter, self.leave = int(enter), int(leave)
        self.reset()

    def reset(self) -> None:
        self.state = False
        self._run_in = 0
        self._run_out = 0

    def update(self, zone: "Zone | None", kept) -> tuple:
        """Take one frame's confident detections [(x, y, confidence), ...].

        Returns (inside, state): whether each detection lies in the zone,
        and the zone state after this frame.
        """
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
        return inside, self.state


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
    #: The zones the recording sends, as they were when it started; the
    #: editor cannot change them while it runs.
    zones: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    events: list = field(default_factory=list)
    sent: int = 0
    refused: int = 0
    lapses: int = 0
    #: The first MAX_ERRORS tracker failures, and how many there were.
    errors: list = field(default_factory=list)
    error_count: int = 0
    #: (perf_counter of the grab, block ID) of the newest tracked frame of
    #: any camera, which places a send on the trigger count.
    ref: tuple | None = None
    #: Per zone: the state the board holds, and when its last byte landed.
    board_state: dict = field(default_factory=dict)
    last_ok: dict = field(default_factory=dict)

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

    def event(self, zone: str, t: float, state: bool, reason: str) -> None:
        bid = self.bid_at(t)
        if bid is None:
            return
        self.events.append({"zone": zone, "t_s": round(t - self.t0, 4),
                            "blockid": bid, "in": bool(state),
                            "reason": reason})
        self.board_state[zone] = bool(state)

    def lapse_check(self, zone: str, now: float) -> None:
        """A gap longer than the board's watchdog turned the zone off there,
        so the record has it off from the watchdog's expiry."""
        wd = ZONE_WATCHDOG_MS / 1000.0
        last = self.last_ok.get(zone)
        if (self.board_state.get(zone) and last is not None
                and now - last > wd):
            self.lapses += 1
            self.event(zone, last + wd, False, "watchdog")


def write_recording(recording_dir, log: RecordingLog, spec,
                    tracker_info: dict) -> list:
    """Write closed_loop.csv and closed_loop.json into a recording folder.
    Returns the WARNINGS.txt lines this recording earned."""
    recording_dir = Path(recording_dir)
    with open(recording_dir / CLOSED_LOOP_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "camera", "blockid", "found", "kept", "in_zones",
                    "zones_on", "sent", "tracker_ms", "detections"])
        w.writerows(log.rows)
    n = len(log.rows)
    per_cam: dict = {}
    for r in log.rows:
        per_cam.setdefault(r[1], []).append(r)
    rates, empty_by_cam = {}, {}
    for cam, rows in per_cam.items():
        span = rows[-1][0] - rows[0][0] if len(rows) > 1 else 0.0
        rates[cam] = round((len(rows) - 1) / span, 3) if span > 0 else 0.0
        empty_by_cam[cam] = sum(1 for r in rows if r[4] == 0)
    on_s: dict = {}
    end_t = log.rows[-1][0] if n else 0.0
    for name in log.zones:
        evs = [e for e in log.events if e["zone"] == name]
        total = 0.0
        for e0, e1 in zip(evs, evs[1:] + [None]):
            if e0["in"]:
                total += max(0.0, (e1["t_s"] if e1 else end_t) - e0["t_s"])
        on_s[name] = round(total, 3)
    record = {
        "settings": spec.to_dict(), "tracker": tracker_info,
        "zones": {k: z.to_dict() for k, z in sorted(log.zones.items())},
        "frame_rate": log.fps, "tracked_frames": n,
        "tracked_rate_hz": rates, "frames_without_mouse": empty_by_cam,
        "zone_on_s": on_s, "zone_bytes_sent": log.sent,
        "zone_bytes_not_sent": log.refused, "watchdog_lapses": log.lapses,
        "tracker_errors": log.error_count,
        "tracker_error_reasons": log.errors, "events": log.events,
    }
    (recording_dir / CLOSED_LOOP_NAME).write_text(
        json.dumps(record, indent=2), encoding="utf-8")

    warn = []
    if n == 0:
        warn.append("closed loop: no frame was tracked, so every zone stayed "
                    "off and no zone block ran.")
    if log.error_count:
        warn.append(f"closed loop: the tracker failed {log.error_count} "
                    f"time(s), first: {log.errors[0]}. The zones were off "
                    f"while it failed.")
    if log.refused:
        warn.append(f"closed loop: {log.refused} zone update(s) did not reach "
                    f"the board (the log says why).")
    if log.lapses:
        warn.append(f"closed loop: zone updates stopped {log.lapses} time(s) "
                    f"for longer than the board's {ZONE_WATCHDOG_MS} ms "
                    f"watchdog, which turned the zone off each time.")
    for cam, rate in sorted(rates.items()):
        if len(per_cam[cam]) > 1 and rate < SLOW_RATE_FRACTION * spec.rate_hz:
            warn.append(f"closed loop: {cam} was tracked {rate:.1f} times a "
                        f"second, below the rate_hz of {spec.rate_hz:g}.")
        k = len(per_cam[cam])
        if k and empty_by_cam[cam] > EMPTY_FRAME_FRACTION * k:
            warn.append(f"closed loop: no mouse was found in "
                        f"{empty_by_cam[cam]} of {k} tracked frames of {cam} "
                        f"({100 * empty_by_cam[cam] / k:.0f}%).")
    return warn


# ------------------------------------------------------------- the loop thread
class ClosedLoop(threading.Thread):
    """Tracks the cameras the zones are drawn on, and any camera the zone
    editor is showing; sends the zones during a recording.

    Start it once the camera manager is up. ``set_zones`` and ``watch``
    choose what is tracked. ``begin_recording`` arms the sending after the
    board's start, ``end_recording`` disarms it before the stop and returns
    the recording's log. ``close`` stops everything.
    """

    def __init__(self, camera_mgr, spec, height: int, width: int):
        super().__init__(daemon=True, name="closed-loop")
        self.spec = spec
        self._mgr = camera_mgr
        self._proc = TrackerProcess(spec, height, width)
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._zones: dict = {}
        self._logic: dict = {}
        self._watched: set = set()
        self._tapped: set = set()
        self._controller = None
        self._send_zones: tuple = ()
        self._log: RecordingLog | None = None
        #: Per zone, when its last byte went out (perf_counter).
        self._zone_sent: dict = {}
        self._last_update: dict = {}
        self._status = "starting"
        self._error: str | None = None
        self._cams: dict = {}
        self._times: dict = {}
        #: Why frames are not being tracked, or None while they are.
        self._untracked: str | None = None

    # -------------------------------------------------------- window side
    def set_zones(self, zones: dict | None) -> None:
        """The zones to decide, by name. A zone the recording sends cannot
        change until it ends (the editor locks itself while armed)."""
        with self._lock:
            self._zones = dict(zones or {})
            for name in list(self._logic):
                if name not in self._zones:
                    del self._logic[name]
            for name in self._zones:
                self._logic.setdefault(name, ZoneLogic(
                    self.spec.min_confidence, self.spec.enter_updates,
                    self.spec.leave_updates))
        self._retap()

    def watch(self, cam_indices) -> None:
        """Also track these cameras (rig-wide, 0-based), for the editor."""
        with self._lock:
            self._watched = set(int(c) for c in cam_indices)
        self._retap()

    def _wanted(self) -> set:
        with self._lock:
            return ({z.cam_index for z in self._zones.values()}
                    | set(self._watched))

    def _retap(self) -> None:
        if self._status != "running":
            return
        want = self._wanted()
        try:
            for cam in want - self._tapped:
                self._mgr.set_tap(cam, self.spec.rate_hz, owner=self)
            for cam in self._tapped - want:
                self._mgr.set_tap(cam, 0.0, owner=self)
        except Exception as e:
            print(f"[closed-loop] could not set the cameras' taps: {e}",
                  flush=True)
        self._tapped = want

    @property
    def ready(self) -> bool:
        return self._status == "running"

    @property
    def tracker_info(self) -> dict:
        return dict(self._proc.info)

    def snapshot(self) -> dict:
        """For the windows: status, error, whether a recording is sending,
        per camera its newest tracked frame (``t``, ``dets`` above
        min_confidence, ``found``, ``ms``, ``bid``, ``hz``), and per zone its
        state (``zones``)."""
        with self._lock:
            return {"status": self._status, "error": self._error,
                    "armed": self._controller is not None,
                    "cameras": {k: dict(v) for k, v in self._cams.items()},
                    "zones": {n: lg.state for n, lg in self._logic.items()}}

    def begin_recording(self, controller, fps: float, zones) -> None:
        """Arm the sending of `zones` (names) to ``controller``, whose
        arm_zone() the caller has already called for this start."""
        with self._lock:
            for lg in self._logic.values():
                lg.reset()
            self._send_zones = tuple(sorted(zones))
            self._log = RecordingLog(
                fps=float(fps), t0=time.perf_counter(),
                zones={n: self._zones[n] for n in self._send_zones
                       if n in self._zones})
            self._controller = controller
            self._zone_sent = {}

    def end_recording(self) -> RecordingLog | None:
        """Disarm before the stop: one last 'out' per zone, then no more
        bytes. Returns the recording's log, or None when none was armed."""
        with self._lock:
            ctl, self._controller = self._controller, None
            log, self._log = self._log, None
            names, self._send_zones = self._send_zones, ()
        now = time.perf_counter()
        for name in names:
            ok = ctl is not None and ctl.send_zone(name, False)
            if log is None:
                continue
            if ok:
                log.sent += 1
            # Updates that stopped for good: the board's watchdog dropped
            # the zone that long after the last one that reached it.
            log.lapse_check(name, now)
            if log.board_state.get(name):
                log.event(name, now, False, "end")
        return log

    def close(self) -> None:
        """Stop sending, stop the loop and end its worker. The window calls
        this on its own thread, so nothing here waits more than half a
        second: the worker is reaped in the background."""
        self.end_recording()
        self._halt.set()
        self._release_taps()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=0.5)
        self._proc.close(block=False)

    def _release_taps(self) -> None:
        try:
            for cam in list(self._tapped):
                self._mgr.set_tap(cam, 0.0, owner=self)
        except Exception as e:
            print(f"[closed-loop] could not stop the cameras' taps: {e}",
                  flush=True)
        self._tapped = set()

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
            self._retap()
            last_seq: dict = {}
            while not self._halt.is_set():
                tracked = False
                for cam in sorted(self._tapped):
                    tap = self._mgr.latest_tap(cam)
                    if tap is not None and tap[2] != last_seq.get(cam):
                        last_seq[cam] = tap[2]
                        self._track(cam, tap)
                        tracked = True
                if not tracked:
                    self._halt.wait(0.002)
                self._keep_alive()
                if not self._proc.alive:
                    self._fail("the tracker process has exited")
                    return
        except Exception as e:
            # Say why the thread ended, instead of a status left at running
            # with nothing behind it. The board's watchdog empties the zones
            # once the updates stop.
            self._fail(f"the closed loop stopped: {type(e).__name__}: {e}")
        finally:
            # The taps and the worker end with the loop, so a camera never
            # copies frames for a loop that is gone. A loop that is not
            # running is started afresh (MainWindow._ensure_closed_loop).
            self._release_taps()
            self._proc.close(block=False)
            if self._status == "running":
                self._status = "stopped"

    def _fail(self, why: str) -> None:
        print(f"[closed-loop] tracker unavailable: {why}", flush=True)
        with self._lock:
            self._status = "error"
            self._error = why
            for lg in self._logic.values():
                lg.reset()
            if self._log is not None:
                self._log.error(why)

    def _track(self, cam: int, tap) -> None:
        frame, bid, _seq, t_grab = tap[:4]
        name = f"cam{cam + 1}"
        try:
            dets, ms = self._proc.infer(frame)
        except TrackerError as e:
            with self._lock:
                for zn, z in self._zones.items():
                    if z.cam_index == cam:
                        self._logic[zn].reset()
                if self._log is not None:
                    self._log.error(f"{name}: {e}")
            # Logged when the reason changes, never per frame.
            if str(e) != self._untracked:
                print(f"[closed-loop] frames not tracked: {e}", flush=True)
                self._untracked = str(e)
            return
        if self._untracked is not None:
            print("[closed-loop] tracking again", flush=True)
            self._untracked = None
        now = time.perf_counter()
        kept = [d for d in dets if d[2] >= self.spec.min_confidence]
        with self._lock:
            in_zones, states = [], {}
            for zn, z in self._zones.items():
                if z.cam_index != cam:
                    continue
                inside, state = self._logic[zn].update(z, kept)
                states[zn] = state
                if any(inside):
                    in_zones.append(zn)
            self._last_update[cam] = now
            times = [t for t in self._times.get(cam, []) if now - t < 2.0]
            self._times[cam] = times + [now]
            self._cams[name] = {"t": now, "dets": kept, "found": len(dets),
                                "ms": ms, "bid": bid,
                                "hz": len(times) / 2.0}
            log = self._log
            if log is not None and bid is not None:
                log.ref = (t_grab, bid)
            sent = all(self._send_locked(zn, st, "tracked")
                       for zn, st in sorted(states.items())
                       if zn in self._send_zones) if log is not None else False
        if log is not None:
            log.rows.append([
                round(t_grab - log.t0, 4), name,
                "" if bid is None else int(bid), len(dets), len(kept),
                " ".join(sorted(in_zones)),
                " ".join(f"{zn}:{int(st)}" for zn, st in sorted(states.items())),
                int(sent), round(ms, 2),
                ";".join(f"{x:.1f}:{y:.1f}:{c:.3f}" for x, y, c in dets)])

    def _keep_alive(self) -> None:
        """Between frames: resend each armed zone's state once RESEND_S has
        passed since its last byte, and turn a zone off once its camera has
        not been tracked for lost_s. Kept per zone, so a busy camera's
        frames cannot starve a zone on another camera of its resends."""
        if self._controller is None:
            return
        now = time.perf_counter()
        with self._lock:
            for zn in self._send_zones:
                if now - self._zone_sent.get(zn, 0.0) < RESEND_S:
                    continue
                lg, z = self._logic.get(zn), self._zones.get(zn)
                if lg is None or z is None:
                    continue
                reason = "resend"
                last = self._last_update.get(z.cam_index, 0.0)
                if lg.state and now - last > self.spec.lost_s:
                    lg.reset()
                    reason = "lost"
                self._send_locked(zn, lg.state, reason)

    def _send_locked(self, zone: str, state: bool, reason: str) -> bool:
        """One zone's byte. Called under the loop's lock, so end_recording()
        cannot detach the controller between this check and the write, which
        would put an 'in' on the board after its last 'out'. send_zone()
        never waits for the link."""
        ctl, log = self._controller, self._log
        if ctl is None or log is None:
            return False
        ok = ctl.send_zone(zone, state)
        now = time.perf_counter()
        self._zone_sent[zone] = now
        if not ok:
            log.refused += 1
            return False
        log.sent += 1
        log.lapse_check(zone, now)
        log.last_ok[zone] = now
        if state != log.board_state.get(zone, False):
            log.event(zone, now, state, reason)
        return True
