"""The simulated rig must reproduce the failures the capture path guards for.

`gui_app/backends/sim.py` and `sim_board.py` exist so the guards can be proven
without cameras, a trigger board or a vendor SDK: a virtual trigger clock
paces every camera, and each camera's fault schedule stages one of the loss
modes that are invisible on real hardware until a session is spoiled. These
tests drive the real `GrabThread`, `SyncEncodeRouter` and
`FrameSyncCoordinator` against that rig and assert on the GUARDS, not on the
simulation: identical block IDs across cameras, a 16-bit wrap unwrapped, a
stalled stream re-armed and re-based, row padding retiring a camera, a camera
that ignores triggers caught by the block-ID rate check, and `retrieve()`
timing out once the board stops.

pypylon is made un-importable up front, so a regression that pulls an SDK into
the simulated path fails here.

    python test_sim_backend.py
"""
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# The simulated rig must need no vendor SDK. Setting this before any gui_app
# import is what proves it rather than assuming it.
sys.modules["pypylon"] = None
sys.modules["pypylon.pylon"] = None

# GrabThread subclasses QThread, so Qt must exist. Offscreen: no display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt5.QtWidgets import QApplication
_APP = QApplication.instance() or QApplication([])

from gui_app import frame_sync
from gui_app.backends import (CameraBackend, CameraHandleProtocol,
                              GrabResultProtocol, load_backend, sim_board)
from gui_app.backends.sim import SimBackend, SimFaults, SimTimeout
from gui_app.grab_thread import GrabThread
from gui_app.serial_controller import TeensyController
from gui_app.session_config import RigProfile
from gui_app.sync_encode import SyncEncodeRouter

#: Tiny frames: these tests exercise sequencing, not throughput, and the NV12
#: ring is sized per camera from this geometry.
W, H = 64, 48
FPS = 100
PINS = [2, 4, 6, 8, 10, 12]

failures = []


def check(num, name, ok, detail=""):
    print(f"{num}) {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if not ok and detail else ""), flush=True)
    if not ok:
        failures.append(name)


class FakeEncoder:
    """Duck-typed EncoderProtocol: counts frames, produces a byte or two."""

    def __init__(self):
        self.encoded = 0
        self.ended = 0
        self.closed = 0

    def Encode(self, nv12):
        self.encoded += 1
        return b"\x00\x00\x00\x01"

    def EndEncode(self):
        self.ended += 1
        return b""

    def Close(self):
        self.closed += 1


def fake_factory(width, height, quality, fps, notes):
    return FakeEncoder()


def _dirs(tmp, n):
    paths = []
    for i in range(n):
        d = tmp / f"cam{i + 1}"
        d.mkdir(exist_ok=True)
        paths.append(d / "raw.bin")
    return paths


class Rig:
    """One simulated recording: board, backend, cameras, router, grab threads.

    Built and torn down per case so a fault armed in one case cannot leak into
    the next — the board and its clock are process-wide, exactly as the wiring
    is on the rig.
    """

    def __init__(self, tmp, n_cams, faults, speed, max_lag=480, fps=FPS,
                 kick=True):
        self.board = sim_board.reset_board(speed=speed)
        self.backend = SimBackend(n_cameras=n_cams, width=W, height=H,
                                  faults=faults)
        self.cams = [self.backend.open(d, "unused.pfs", 600)
                     for d in self.backend.enumerate_devices()]
        for c in self.cams:
            self.backend.set_triggered(c, 165.0)
        self.paths = _dirs(tmp, n_cams)
        self.router = None
        if kick:
            self.router = SyncEncodeRouter(self.paths, W, H, 21, fps=fps,
                                           max_lag=max_lag,
                                           encoder_factory=fake_factory)
            self.router.start()
        self.threads = [
            GrabThread(i, self.cams[i], self.backend, raw_path=self.paths[i],
                       display_every=10 ** 9, realtime=kick, width=W, height=H,
                       quality=21, fps=fps, router=self.router)
            for i in range(n_cams)]
        self.teensy = TeensyController(port="sim")
        self.fps = fps
        self.speed = speed

    def record(self, seconds_v: float):
        """Arm, start the board, run for `seconds_v` VIRTUAL seconds, stop.

        The order is the application's: every grab thread is ready before the
        board is started, and the stop is the board's command followed by the
        flag the grab loop honours on its next timeout.
        """
        for t in self.threads:
            t.start()
        for t in self.threads:
            assert t.ready.wait(20), "a grab thread never armed"
        assert self.teensy.open(), self.teensy.last_error
        started = self.teensy.start_triggers(PINS, self.fps)
        for t in self.threads:
            t.signal_triggers_started()
        if started:
            time.sleep(seconds_v / self.speed)
            self.teensy.stop_triggers(PINS)
        for t in self.threads:
            t.signal_triggers_stopped()
        for t in self.threads:
            assert t.wait(30000), "a grab thread did not exit after the stop"
        self.teensy.close()
        return self.router.stop() if self.router is not None else None


# 1 -- the backend is registered and implements the contract ------------------
backend = load_backend("sim")
required = ({n for n in dir(CameraBackend) if not n.startswith("_")}
            | set(CameraBackend.__annotations__))
missing = sorted(n for n in required if not hasattr(backend, n))
check(1, "load_backend('sim') returns a SimBackend implementing CameraBackend",
      isinstance(backend, SimBackend) and backend.name == "sim" and not missing,
      f"missing={missing}")

# 2 -- the profile that selects it loads, and the backend is built FROM it ----
# The rig's shape is declared once, in the profile. The backend `load_backend`
# builds must be that shape and enumerate that many cameras: `camera_manager`
# checks the cameras against the PROFILE, so a backend carrying its own copy of
# the numbers would refuse to start the moment the profile was edited, with a
# message ("Expected N cameras but M enumerated") that names neither cause.
prof = RigProfile.load(Path(__file__).parent / "profiles" / "sim.yaml")
shape_from_profile = (backend.n_cameras == prof.n_cameras
                      and (backend.width, backend.height)
                      == (prof.frame_width, prof.frame_height)
                      and len(backend.enumerate_devices()) == prof.n_cameras)
check(2, "profiles/sim.yaml selects the sim backend and the sim port, and the "
         "backend takes its shape from that profile",
      (prof.camera_backend == "sim" and prof.serial_port == "sim"
       and prof.encoder == "auto" and shape_from_profile),
      f"{prof.camera_backend} {prof.serial_port} profile="
      f"{prof.n_cameras}x{prof.frame_width}x{prof.frame_height} backend="
      f"{backend.n_cameras}x{backend.width}x{backend.height}")

# 3 -- geometry, handle and result satisfy the documented protocols -----------
sim_board.reset_board(speed=1.0)
b3 = SimBackend(n_cameras=2, width=W, height=H)
cams3 = [b3.open(d, "unused.pfs", 600) for d in b3.enumerate_devices()]
info = b3.describe(cams3[0])
ser = b3.enumerate_devices()[1].GetSerialNumber()
b3.set_triggered(cams3[0], 165.0)
b3.start_grabbing(cams3[0])
board3 = b3.board
board3.start(FPS, PINS)
res3 = b3.retrieve(cams3[0], 200)
with res3.GetArrayZeroCopy() as img3:
    shape_ok = (img3.shape == (H, W) and img3.dtype.name == "uint8"
                and img3.flags["C_CONTIGUOUS"])
res3.Release()
double_release = False
try:
    res3.Release()
except RuntimeError:
    double_release = True
check(3, "describe() is Mono8, serials are strings, the view is a contiguous "
         "(H, W) uint8 and a result releases exactly once",
      (info["pixel_format"] == "Mono8"
       and (info["width"], info["height"]) == (W, H)
       and isinstance(ser, str) and ser == str(ser)
       and isinstance(res3, GrabResultProtocol)
       and isinstance(cams3[0], CameraHandleProtocol)
       and shape_ok and double_release),
      f"{info} shape_ok={shape_ok} double_release={double_release}")

# 4 -- retrieve() is paced by the clock and times out once the board stops ----
sim_board.reset_board(speed=1.0)
b4 = SimBackend(n_cameras=1, width=W, height=H)
cam4 = b4.open(b4.enumerate_devices()[0], "unused.pfs", 600)
b4.set_triggered(cam4, 165.0)
b4.start_grabbing(cam4)
ser4 = sim_board.SimSerial()
ser4.write(b"6,2,4,6,8,10,12,100\n")
ack4 = ser4.read(64)
stamps = []
for _ in range(60):
    r = b4.retrieve(cam4, 200)
    stamps.append(time.perf_counter())
    r.Release()
intervals = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
mean_ms = 1000.0 * statistics.mean(intervals)
ser4.write(b"6,2,4,6,8,10,12,-1\n")
stop_ack4 = ser4.read(64)
drained = 0
timed_out = False
t_stop = time.perf_counter()
while time.perf_counter() - t_stop < 3.0:
    try:
        b4.retrieve(cam4, 200).Release()
        drained += 1
    except SimTimeout:
        timed_out = True
        break
check(4, "frames are paced at 10 ms +-20% at 100 fps and retrieve() times out "
         "once the board stops",
      (ack4 == b"RDY 6 100\r\n" and stop_ack4 == b"RDY 6 0\r\n"
       and 8.0 <= mean_ms <= 12.0 and timed_out and drained < 10),
      f"mean={mean_ms:.2f}ms drained={drained} timed_out={timed_out}")

# 5 -- the serial board: identity, stop ack, and a mis-parse refused ----------
brd5 = sim_board.reset_board(speed=1.0)
sid = sim_board.accept_upload('const char SKETCH_ID[] = "a1b2c3d4";\n')
t5 = TeensyController(port="sim")
opened5 = t5.open()
started5 = t5.start_triggers(PINS, FPS)
id5, running5 = t5.board_id, brd5.running
stopped5 = t5.stop_triggers(PINS)
after5 = brd5.running
# A board that mis-parses the rate really runs at the wrong one, so the ack
# that reports it is the only thing standing between the operator and a
# recording timed against a rate nobody asked for.
brd5.fps_misparse = 1000
t6 = TeensyController(port="sim")
t6.open()
misparse_refused = t6.start_triggers(PINS, FPS) is False
brd5.fps_misparse = None
brd5.stop()
check(5, "the simulated board acks the start with its sketch id, acks the stop "
         "with 0, and a mis-parsed rate is refused",
      (opened5 and started5 and sid == "a1b2c3d4" and id5 == "a1b2c3d4"
       and running5 and stopped5 and not after5 and misparse_refused),
      f"started={started5} id={id5} stopped={stopped5} "
      f"misparse_refused={misparse_refused}")

# 6 -- a clean 20-virtual-second recording, through a 16-bit wrap -------------
# Every camera starts its counter at 65500, which is what a recording begun
# just before a wrap looks like: the ids roll over 65535 a third of a second
# in, and only the software unwrap keeps the cameras comparable.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    faults = {i: SimFaults(ppm=p, jitter_s=0.001, blockid_start=65500)
              for i, p in enumerate((250e-6, -180e-6, 240e-6))}
    rig = Rig(tmp, 3, faults, speed=4.0, max_lag=480)
    res = rig.record(20.0)
    ids = [r[2] for r in res]
    same = all(x == ids[0] for x in ids)
    contiguous = all(b - a == 1 for a, b in zip(ids[0], ids[0][1:]))
    wrapped = bool(ids[0]) and ids[0][0] < 65535 < ids[0][-1]
    coord = rig.router._coord
    check(6, "20 virtual seconds, three cameras: identical block IDs, no "
             "forced drops, and the 16-bit wrap unwrapped",
          (same and contiguous and wrapped and len(ids[0]) > 1900
           and coord.forced == 0 and rig.router.warnings == []),
          f"n={[len(i) for i in ids]} same={same} contiguous={contiguous} "
          f"wrapped={wrapped} forced={coord.forced} "
          f"warnings={rig.router.warnings}")

# 7 -- faults: gaps, an ignored-trigger camera, and a retirement --------------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    faults = {
        0: SimFaults(ppm=250e-6),
        # Frames lost in transmission and to an exhausted pool: the block IDs
        # are consumed, so the host sees gaps and the coordinator kicks those
        # triggers out for every camera.
        1: SimFaults(drop_every=50, underrun_every=131),
        # The camera that IGNORES triggers: contiguous ids, no gap anywhere,
        # and only the device clock gives it away.
        2: SimFaults(ignore_every=50),
        # A transport that fails outright: the loop must retire the camera
        # rather than let it starve the others through the coordinator.
        3: SimFaults(fail_after=5),
    }
    rig = Rig(tmp, 4, faults, speed=4.0, max_lag=480)
    res = rig.record(8.0)
    coord = rig.router._coord
    retired = [c for c, _r in coord.retired_reasons]
    rate_msgs = [m for m in rig.router.warnings if "block IDs advanced at" in m]
    stats1 = rig.backend.stream_stats(rig.cams[1])
    ids = [r[2] for r in res]
    survivors_agree = ids[0] == ids[2] and len(ids[0]) > 300
    # cam2's lost triggers are kicked out for everyone, so they appear in
    # nobody's recording.
    lost = [i for i in range(1, 700) if i % 50 == 0 or i % 131 == 0]
    kicked = all(i not in ids[0] for i in lost[:5])
    check(7, "dropped triggers are kicked out for every camera, a failing "
             "camera is retired, and the ignored-trigger camera trips the "
             "block-ID rate check",
          (retired == [3] and len(rate_msgs) == 1 and "cam3" in rate_msgs[0]
           and stats1["Failed_Buffer_Count"] > 0
           and stats1["Buffer_Underrun_Count"] > 0
           and coord.dropped > 0 and survivors_agree and kicked),
          f"retired={coord.retired_reasons} rate_msgs={rate_msgs} "
          f"stats={stats1} dropped={coord.dropped} agree={survivors_agree} "
          f"kicked={kicked}")

# 8 -- a camera lagging past max_lag is force-dropped, not waited for ---------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    faults = {0: SimFaults(), 1: SimFaults(lag_frames=40)}
    rig = Rig(tmp, 2, faults, speed=4.0, max_lag=8)
    res = rig.record(4.0)
    coord = rig.router._coord
    check(8, "a camera 40 triggers behind with max_lag 8 force-drops instead "
             "of freezing the recording",
          coord.forced > 0 and coord.forced_by[1] > 0 and res[0][0] > 0,
          f"forced={coord.forced} by={coord.forced_by} n={[r[0] for r in res]}")

# 9 -- row padding retires the camera before a single frame is recorded -------
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    rig = Rig(tmp, 1, {0: SimFaults(padding_x=4)}, speed=4.0, max_lag=8)
    rig.record(1.0)
    t9 = rig.threads[0]
    padding_warning = any("padding" in w for w in t9.warnings)
    coord = rig.router._coord
    check(9, "a camera reporting PaddingX is retired and records nothing",
          (t9.desynced and coord.retired_reasons
           and "padding" in coord.retired_reasons[0][1]),
          f"desynced={t9.desynced} retired={coord.retired_reasons} "
          f"warn={padding_warning}")

# 10 -- a stalled stream is re-armed and re-based onto the true ordinal -------
# The grab loop needs 25 consecutive 200 ms timeouts before it re-arms, so the
# stall has to outlast five real seconds; at speed 4 that is 20 virtual
# seconds of silence. 30 virtual seconds (7.5 real) leaves 2.5 s of margin,
# because each timeout costs slightly MORE than 200 ms — the loop's error
# handling and logging run between them — and on a loaded machine a margin of
# a few per cent starves the re-arm and reports it as a resync bug.
# Over-running is free: a SECOND re-arm would need another five real seconds
# of silence, which this stall is still far short of. Raw mode: this camera's
# own block-ID bookkeeping is what the resync has to get right, and no encoder
# is involved in that.
with tempfile.TemporaryDirectory() as d:
    tmp = Path(d)
    stall_at, stall_s = 320, 30.0
    rig = Rig(tmp, 1, {0: SimFaults(ppm=250e-6, stall_at=stall_at,
                                    stall_s=stall_s)},
              speed=4.0, kick=False)
    rig.record(3.2 + stall_s + 1.0)
    t10 = rig.threads[0]
    ids = t10.block_ids
    before = [b for b in ids if b < stall_at]
    after = [b for b in ids if b > stall_at + stall_s * FPS]
    increasing = all(b > a for a, b in zip(ids, ids[1:]))
    # The re-armed counter restarts at 1; a correct resync puts the first
    # post-stall frame back on its true trigger ordinal, so the gap in the
    # recorded ids is the stall and nothing more.
    gap_ok = bool(after) and abs(after[0] - (stall_at + stall_s * FPS)) <= 3
    check(10, "a stalled stream is re-armed once and its block IDs re-based "
              "onto the true trigger ordinal",
          (t10.rearms == 1 and not t10.desynced and increasing
           and len(before) > 300 and gap_ok),
          f"rearms={t10.rearms} desynced={t10.desynced} before={len(before)} "
          f"after={len(after)} first_after={after[:1]} inc={increasing}")

# 11 -- the rate check is what catches the ignored trigger, on its own --------
sim_board.reset_board(speed=20.0)
b11 = SimBackend(n_cameras=1, width=W, height=H,
                 faults={0: SimFaults(ignore_every=10)})
cam11 = b11.open(b11.enumerate_devices()[0], "unused.pfs", 600)
b11.set_triggered(cam11, 165.0)
b11.start_grabbing(cam11)
ser11 = sim_board.SimSerial()
ser11.write(b"6,2,4,6,8,10,12,100\n")
ser11.read(64)
bids, tss = [], []
while len(bids) < 500:
    try:
        r = b11.retrieve(cam11, 200)
    except SimTimeout:
        break
    bids.append(r.BlockID)
    tss.append(r.TimeStamp * 1e-9)
    r.Release()
ser11.write(b"6,2,4,6,8,10,12,-1\n")
gapless = all(b - a == 1 for a, b in zip(bids, bids[1:]))
msg11 = frame_sync.check_block_id_rate(bids, tss, FPS, "cam1")
check(11, "an ignored trigger leaves NO gap in the block IDs and is caught "
          "only by the rate check",
      (gapless and msg11 is not None and "ignored roughly" in msg11
       and b11.stream_stats(cam11)["Ignored_Trigger_Count"] > 0),
      f"gapless={gapless} msg={(msg11 or '')[:80]}")

# 12 -- a pulse the BOARD never fires costs every camera the same trigger ----
brd12 = sim_board.reset_board(speed=20.0)
brd12.miss_pulses = {10, 20, 30}
b12 = SimBackend(n_cameras=2, width=W, height=H)
cams12 = [b12.open(d, "unused.pfs", 600) for d in b12.enumerate_devices()]
for c in cams12:
    b12.set_triggered(c, 165.0)
    b12.start_grabbing(c)
ser12 = sim_board.SimSerial()
ser12.write(b"6,2,4,6,8,10,12,100\n")
ser12.read(64)
got12 = [[], []]
for k in range(2):
    while len(got12[k]) < 47:
        try:
            r = b12.retrieve(cams12[k], 200)
        except SimTimeout:
            break
        got12[k].append(r.BlockID)
        r.Release()
ser12.write(b"6,2,4,6,8,10,12,-1\n")
check(12, "a missed board pulse costs every camera the same trigger and "
          "consumes no block ID, so the recording stays aligned",
      (got12[0] == got12[1] == list(range(1, 48))
       and b12.stream_stats(cams12[0])["Failed_Buffer_Count"] == 0),
      f"n={[len(g) for g in got12]} tail={got12[0][-3:]}")

# 13 -- a zero-copy view that escapes the with block is caught ---------------
# The fixture exists to prove the grab loop's hardest invariant: `img` is a
# window onto a buffer that is about to be reused, so a consumer must copy out
# of it and never store it. pypylon's own exit guard counts references; a
# stand-in that did not would pass a regression here and fail only on the rig.
sim_board.reset_board(speed=1.0)
b13 = SimBackend(n_cameras=1, width=W, height=H)
cam13 = b13.open(b13.enumerate_devices()[0], "unused.pfs", 600)
b13.set_triggered(cam13, 165.0)
b13.start_grabbing(cam13)
ser13 = sim_board.SimSerial()
ser13.write(b"6,2,4,6,8,10,12,100\n")
ser13.read(64)
# A consumer that copies out is the correct one and must not be refused.
r13 = b13.retrieve(cam13, 200)
copied_ok = True
try:
    with r13.GetArrayZeroCopy() as img13:
        taken = img13.copy()
except RuntimeError:
    copied_ok = False
r13.Release()
# A consumer that STORES the view is the bug, and it must be named.
r13b = b13.retrieve(cam13, 200)
kept = []
escape_caught = False
try:
    with r13b.GetArrayZeroCopy() as img13b:
        kept.append(img13b)
except RuntimeError as e:
    escape_caught = "outlive" in str(e)
kept.clear()
r13b.Release()
# A numpy VIEW kept on the buffer is the same leak wearing a disguise: it
# holds the base array alive, so the guard must catch it too.
r13c = b13.retrieve(cam13, 200)
view_caught = False
try:
    with r13c.GetArrayZeroCopy() as img13c:
        kept.append(img13c[::2, ::2])
except RuntimeError:
    view_caught = True
kept.clear()
r13c.Release()
ser13.write(b"6,2,4,6,8,10,12,-1\n")
check(13, "an escaped zero-copy view is refused on leaving the with block, "
          "while a consumer that copies out is not",
      (copied_ok and taken.shape == (H, W) and escape_caught and view_caught),
      f"copied_ok={copied_ok} escape_caught={escape_caught} "
      f"view_caught={view_caught}")

# 14 -- closing a camera releases it and its buffers -------------------------
# One backend lives for the life of the process while `open_all` runs again on
# every profile switch and preview restart, so anything a close leaves behind
# is stranded for the whole session rather than for one recording.
sim_board.reset_board(speed=1.0)
b14 = SimBackend(n_cameras=3, width=W, height=H)
cams14 = [b14.open(d, "unused.pfs", 600) for d in b14.enumerate_devices()]
held = cams14[0]
opened14 = len(b14.cameras)
for c in cams14:
    b14.close(c)
# A second open/close cycle must leave exactly as much behind as the first.
for c in [b14.open(d, "unused.pfs", 600) for d in b14.enumerate_devices()]:
    b14.close(c)
check(14, "closing a camera drops it from the backend and frees its buffer "
          "pool, so repeated sessions do not strand memory",
      (opened14 == 3 and b14.cameras == [] and held._buffers == []
       and held._free == [] and not held.is_open),
      f"opened={opened14} left={len(b14.cameras)} "
      f"buffers={len(held._buffers)} free={len(held._free)}")

# 15 -- a failed grab is counted once, like a real camera --------------------
# Total_Buffer_Count is what a session log reports and what a loss rate is
# computed from: one buffer must contribute one count, whether or not the grab
# succeeded, or the arithmetic reports loss that never happened.
sim_board.reset_board(speed=20.0)
b15 = SimBackend(n_cameras=1, width=W, height=H,
                 faults={0: SimFaults(failed_grab_every=3)})
cam15 = b15.open(b15.enumerate_devices()[0], "unused.pfs", 600)
b15.set_triggered(cam15, 165.0)
b15.start_grabbing(cam15)
ser15 = sim_board.SimSerial()
ser15.write(b"6,2,4,6,8,10,12,100\n")
ser15.read(64)
buffers15, failed15 = 0, 0
while buffers15 < 30:
    try:
        r = b15.retrieve(cam15, 200)
    except SimTimeout:
        break
    buffers15 += 1
    failed15 += 0 if r.GrabSucceeded() else 1
    r.Release()
ser15.write(b"6,2,4,6,8,10,12,-1\n")
st15 = b15.stream_stats(cam15)
check(15, "every third grab fails and Total_Buffer_Count still equals the "
          "number of buffers the camera handed over",
      (buffers15 == 30 and failed15 == 10
       and st15["Total_Buffer_Count"] == buffers15
       and st15["Failed_Buffer_Count"] == failed15
       and st15["Buffer_Underrun_Count"] == 0),
      f"buffers={buffers15} failed={failed15} stats={st15}")

# 16 -- a camera armed between two pulse trains anchors to the NEW one -------
# The application arms every camera BEFORE it tells the board to start, so the
# ordinal a camera computes at StartGrabbing belongs to the train that has just
# ended. `SimBoard.start` restarts ordinals at 1, so without a re-anchor the
# camera ignores every trigger of the new train until that stale number comes
# round -- and it is silent, because the frames that do arrive still carry
# block IDs from 1 and stay contiguous. The witness is the DELIVERED TRIGGER,
# recovered from the result's device timestamp against the new epoch: block ID
# 1 alone proves nothing, since the counter restarts with the arm either way.
brd16 = sim_board.reset_board(speed=4.0)
b16 = SimBackend(n_cameras=1, width=W, height=H)
cam16 = b16.open(b16.enumerate_devices()[0], "unused.pfs", 600)
b16.set_triggered(cam16, 165.0)
#: Virtual seconds the first train runs. Long enough that waiting out its
#: ordinal would be unmistakable: 50 triggers at 100 fps, against the one
#: trigger period a re-anchored camera waits.
FIRST_TRAIN_S = 0.5
b16.start_grabbing(cam16)
brd16.start(FPS, PINS)
time.sleep(FIRST_TRAIN_S / brd16.speed)
brd16.stop()
# Re-armed while the board is stopped, exactly as a second acquisition does.
b16.stop_grabbing(cam16)
b16.start_grabbing(cam16)
stale16 = b16.cameras[0]._next
epoch16 = brd16.start(FPS, PINS)
st16 = brd16.state()
t16 = time.perf_counter()
r16 = b16.retrieve(cam16, 2000)
waited16 = time.perf_counter() - t16
bid16 = r16.BlockID
# trigger_v(i) = epoch_v + i / fps, and the timestamp is that instant in ns,
# skewed by the camera's oscillator error (zero here).
ordinal16 = round((r16.TimeStamp / 1e9 - st16.epoch_v) * st16.fps)
r16.Release()
brd16.stop()
b16.close(cam16)
check(16, "a camera armed before the board starts delivers trigger 1 of the "
          "NEW pulse train, not the ordinal left over from the old one",
      (stale16 > 1 and ordinal16 == 1 and bid16 == 1
       and waited16 < (FIRST_TRAIN_S / 2) / brd16.speed),
      f"stale_next={stale16} delivered_ordinal={ordinal16} bid={bid16} "
      f"waited={waited16 * 1000:.1f}ms")

print()
if failures:
    print(f"{len(failures)} SIM-BACKEND TEST(S) FAILED: {failures}")
    sys.exit(1)
print("ALL SIM-BACKEND TESTS PASS")
