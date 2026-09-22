"""Simulated trigger board: the virtual trigger clock, and a serial stand-in.

The board is the only source of time in the simulated rig. `SimBoard` owns a
virtual clock and decides when each trigger fires; `gui_app.backends.sim`
paces every simulated camera from that same clock, which is what makes the
cameras trigger-synchronised for the same reason the real ones are — one
clock fires them all.

`SimSerial` is what `serial_controller.TeensyController` opens when a
profile's `serial_port` is `sim`. It speaks the sketch's line protocol
(`n,p1..pn,fps` in, `RDY <n> <fps> <id>` out) so the host handshake, the stop
ack and the board-identity check all run unchanged, and the command that
would start the real board starts this clock.

Nothing here opens a port, imports a vendor SDK or touches hardware.

VIRTUAL TIME
`speed` is virtual seconds per real second. Everything a consumer observes —
trigger ordinals, device timestamps, the frame rate the cameras report — is
expressed in virtual seconds, so a 20-virtual-second recording at `speed = 5`
takes four real seconds and still looks like 100 fps to every consumer. Set
`speed` before starting a run: the virtual clock is derived from the real one,
so changing the factor mid-run steps the clock.
"""
import threading
import time
from typing import NamedTuple

#: Virtual seconds per real second for a board nobody has configured. 1.0
#: keeps the simulation honest by default; a caller that needs the sequence
#: rather than the pacing raises it to shorten the wall clock.
DEFAULT_SPEED = 1.0

#: Line speed the simulated port reports. It is the sketch's, so a caller that
#: logs the link describes the board it believes it has.
BAUDRATE = 115200


class BoardState(NamedTuple):
    """One consistent read of the clock, taken under the board's lock.

    RULE: a camera takes this snapshot once per frame and does its arithmetic
    on it. REASON: nine cameras each reading four fields under a lock 100
    times a second would serialise on the board, and a start or stop landing
    between two of those reads would place a frame at a time that never
    existed.
    """
    fps: float
    epoch_v: float
    stop_v: float | None
    running: bool
    t0: float
    speed: float


class SimBoard:
    """The virtual trigger clock, and the fault knobs a trigger board can have.

    Trigger `i` (1-based, as GVSP block IDs are) fires at virtual time
    `epoch_v + i / fps`, where `epoch_v` is the virtual time at which the last
    start command was accepted. Ordinals restart at every start because the
    sketch re-anchors its counter on every configuration command; the virtual
    clock itself never restarts, so device timestamps derived from it stay
    monotonic across a stop, a start and a stream re-arm — which is what
    `GrabThread._resync_offset` needs to re-base a restarted block-ID counter.
    """

    def __init__(self, speed: float = DEFAULT_SPEED):
        self._lock = threading.RLock()
        self._t0 = time.perf_counter()
        self.speed = float(speed)
        self.fps = 0.0
        self.pins: list = []
        self._epoch_v = 0.0
        self._stop_v: float | None = None
        self._running = False
        #: Start and stop commands honoured, so a caller can prove the board
        #: was driven rather than assume it.
        self.starts = 0
        self.stops = 0
        # --- fault knobs ----------------------------------------------------
        #: Trigger ordinals the board does not fire. A missed pulse is a
        #: property of the BOARD, so no camera acquires that trigger and none
        #: consumes a block ID for it: the recording stays aligned and simply
        #: has one trigger fewer.
        self.miss_pulses: set = set()
        #: Frame rate the board parses instead of the one it was sent, or None
        #: for a board that parses correctly. A mis-parse is the failure the
        #: RDY ack exists to catch, so the board runs at the wrong rate as
        #: well as reporting it.
        self.fps_misparse: int | None = None
        #: Answer nothing, modelling a wedged sketch. The host then reopens
        #: the port to force a reset and finally refuses to record.
        self.silent = False
        #: Seconds before a queued ack becomes readable. The real sketch takes
        #: ~1.5 s on every configuration path; zero is the honest default for
        #: a board whose latency is not what is under test.
        self.ack_delay_s = 0.0
        #: Identity of the last sketch accepted through `accept_upload`, or
        #: None for a board that has never been flashed — which is what
        #: firmware predating the identity line looks like to the host.
        self.sketch_id: str | None = None
        self.last_sketch: str | None = None

    # ------------------------------------------------------------------ time
    def virtual_now(self) -> float:
        """Virtual seconds since this board was constructed."""
        return (time.perf_counter() - self._t0) * self.speed

    def state(self) -> BoardState:
        with self._lock:
            return BoardState(self.fps, self._epoch_v, self._stop_v,
                              self._running, self._t0, self.speed)

    @staticmethod
    def trigger_v(i: int, st: BoardState) -> float:
        """Virtual time at which trigger `i` fires."""
        return st.epoch_v + i / st.fps

    @staticmethod
    def real_time_of(v: float, st: BoardState) -> float:
        """`perf_counter` value at which virtual time `v` arrives."""
        return st.t0 + v / st.speed

    def ordinal_now(self, st: BoardState | None = None) -> int:
        """Highest trigger ordinal that has already fired; 0 before the first.

        A camera arming mid-run starts from the NEXT ordinal, because a real
        camera cannot deliver a trigger that has already passed.
        """
        st = st or self.state()
        if st.fps <= 0:
            return 0
        v = self.virtual_now() if st.running else (st.stop_v or st.epoch_v)
        return max(0, int((v - st.epoch_v) * st.fps))

    def fired(self, i: int) -> bool:
        """Whether trigger `i` is a pulse this board puts on the wire."""
        return i >= 1 and i not in self.miss_pulses

    def exhausted(self, i: int, st: BoardState) -> bool:
        """Whether trigger `i` falls after the board stopped, i.e. never comes.

        This is what makes a camera's `retrieve()` time out once the triggers
        stop: with no further pulse due, there is nothing left to wait for.
        """
        if st.fps <= 0:
            return True
        return st.stop_v is not None and self.trigger_v(i, st) > st.stop_v

    # --------------------------------------------------------------- driving
    def start(self, fps: float, pins=()) -> float:
        """Begin (or restart) the pulse train. Returns the rate actually run."""
        with self._lock:
            rate = float(self.fps_misparse if self.fps_misparse is not None
                         else fps)
            if rate <= 0:
                return self.stop()
            self.fps = rate
            self.pins = list(pins)
            self._epoch_v = self.virtual_now()
            self._stop_v = None
            self._running = True
            self.starts += 1
            return rate

    def stop(self) -> float:
        """End the pulse train. Triggers due after this moment never fire."""
        with self._lock:
            if self._running:
                self._stop_v = self.virtual_now()
                self._running = False
                self.stops += 1
            return 0.0

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running


# ----------------------------------------------------------- the shared board
#: One board per process, because the cameras and the serial link must agree
#: on when a trigger fires: `sim.SimBackend` reads this clock and `SimSerial`
#: drives it, which is the wiring the rig has.
_SHARED: SimBoard | None = None
_SHARED_LOCK = threading.Lock()


def shared_board() -> SimBoard:
    """The process-wide simulated board, created on first use."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = SimBoard()
        return _SHARED


def reset_board(speed: float = DEFAULT_SPEED) -> SimBoard:
    """Replace the shared board with a fresh one and return it.

    Ordinals, fault knobs and the sketch identity reset together, so one
    simulated run cannot leave a fault armed for the next.
    """
    global _SHARED
    with _SHARED_LOCK:
        _SHARED = SimBoard(speed=speed)
        return _SHARED


def accept_upload(ino_content: str, board: SimBoard | None = None) -> str | None:
    """Take a sketch the host "flashed" and adopt its identity.

    `stim_compiler.upload()` calls this for port `sim` instead of running
    arduino-cli, so the id this board prints in its RDY line tracks the sketch
    the operator applied. That is the point of the identity line: the host
    tells which build the board runs instead of trusting a per-machine record
    of what was last uploaded.
    """
    from gui_app.stim_compiler import sketch_id
    brd = board or shared_board()
    brd.last_sketch = ino_content
    brd.sketch_id = sketch_id(ino_content)
    return brd.sketch_id


class SimSerial:
    """A `serial.Serial` stand-in wired to a `SimBoard`.

    Only what `TeensyController` uses is implemented — `is_open`,
    `reset_input_buffer()`, `write()`, `read()` and `close()` — because the
    controller is the only caller and a wider surface would be untested
    fiction. `board` is the one addition, and it is for test code: it names
    the clock this port drives, which a real `serial.Serial` has no notion of.

    A closed port raises `OSError` rather than `serial.SerialException`: the
    controller catches both, and the builtin keeps the simulated rig free of a
    pyserial import it does not otherwise need.
    """

    def __init__(self, port: str = "sim", baudrate: int = BAUDRATE,
                 timeout: float = 0.1, write_timeout: float = 1.0,
                 board: SimBoard | None = None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.is_open = True
        self._board = board or shared_board()
        self._lock = threading.Lock()
        self._out = bytearray()      # bytes the host has not read yet
        self._partial = bytearray()  # command bytes with no newline yet
        self._ready_at = 0.0         # when the queued reply becomes readable

    @property
    def board(self) -> SimBoard:
        """The clock this port drives."""
        return self._board

    # ------------------------------------------------------------- host side
    def reset_input_buffer(self) -> None:
        """Discard what the board has said but the host has not read.

        The controller calls this before every command so the ack it reads
        afterwards answers THIS command rather than being a leftover line.
        """
        with self._lock:
            self._out.clear()

    def write(self, data: bytes) -> int:
        """Take command bytes; act on each complete line.

        A command split across two writes is held until its newline arrives,
        because the sketch parses whole lines and a half-parsed configuration
        would start the board at a rate nobody asked for.
        """
        if not self.is_open:
            raise OSError("write to a closed simulated port")
        with self._lock:
            self._partial.extend(bytes(data))
            lines = []
            while True:
                nl = self._partial.find(b"\n")
                if nl < 0:
                    break
                lines.append(bytes(self._partial[:nl]))
                del self._partial[:nl + 1]
        for line in lines:
            self._handle(line.decode("ascii", "ignore").strip())
        return len(data)

    def read(self, size: int = 1) -> bytes:
        """Up to `size` bytes, or b"" after `timeout` seconds of silence.

        Blocking for the timeout on an empty buffer is what a real port does,
        and the controller's ack loop reads in a tight `while`: returning at
        once would spin a core for the whole ack budget.
        """
        if not self.is_open:
            raise OSError("read from a closed simulated port")
        with self._lock:
            have = len(self._out)
            ready = self._ready_at
        now = time.perf_counter()
        if not have or now < ready:
            wait = self.timeout or 0.0
            if ready > now:
                wait = min(wait, ready - now) if wait else 0.0
            if wait:
                time.sleep(wait)
            if time.perf_counter() < ready:
                return b""
        with self._lock:
            chunk = bytes(self._out[:size])
            del self._out[:size]
        return chunk

    def close(self) -> None:
        """Drop the host's end of the link.

        The board keeps triggering: closing a port does not stop a sketch, and
        pretending otherwise would hide the very failure the stop ack exists
        to catch.
        """
        self.is_open = False

    # ------------------------------------------------------------ board side
    def _handle(self, line: str) -> None:
        """Parse one `n,p1..pn,fps` command and queue the RDY answer."""
        parts = [p for p in line.split(",") if p != ""]
        if len(parts) < 2:
            return                      # not a command; the sketch ignores it
        try:
            n = int(float(parts[0]))
            pins = [int(float(p)) for p in parts[1:-1]]
            fps = int(float(parts[-1]))
        except ValueError:
            return                      # garbled; no ack, so the host retries
        if fps > 0:
            rate = self._board.start(fps, pins)
        else:
            # readFPS() clamps a negative rate to 0, so a stop is acked as
            # `RDY <n> 0` instead of echoing the -1 the host sent.
            rate = self._board.stop()
        if self._board.silent:
            return
        sid = self._board.sketch_id
        reply = f"RDY {n} {int(rate)}" + (f" {sid}" if sid else "") + "\r\n"
        with self._lock:
            self._out.extend(reply.encode("ascii"))
            self._ready_at = time.perf_counter() + self._board.ack_delay_s
