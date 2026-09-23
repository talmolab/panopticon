"""Serial controller for the camera-trigger / stim board.

The board is an Arduino Mega 2560 (see stim_compiler.FQBN), not a Teensy — the
class name is legacy from campy and is kept because tests, probe scripts and
main_window all refer to it.
"""
import re
import threading
import time
import serial

from gui_app.stim_compiler import SIM_PORT, is_sim_port  # noqa: F401 (SIM_PORT re-exported)

#: One complete ack line from the sketch: ``RDY <n_cams> <fps>`` with an
#: optional 8-hex sketch identity appended by firmware that knows its own
#: build. Applied with fullmatch() to the WHOLE stripped line, never searched
#: for inside it, because the ack exists to prove the board printed exactly
#: this line: a superstring such as ``RDY 6 1000`` cannot satisfy a request
#: for 100 fps, and a stray byte in front of the token (``xRDY 6 100``) or a
#: boot message run into it (``bootRDY 6 100``) is not something the sketch
#: prints, so it is a garbled ack, not a confirmed start.
_RDY_LINE = re.compile(r"RDY (\d+) (-?\d+)(?: ([0-9a-fA-F]{8}))?")


class TeensyController:
    """Serial link to the Arduino camera-trigger / stim board.

    Meant to be held open for the life of the GUI. Opening the port pulses DTR,
    which auto-resets the board, and for the ~1-2 s of reset + bootloader every
    pin is high-Z — long enough for a connected laser driver to read its floating
    modulation input as ON. Keeping the port open across recordings moves that
    reset out of the experiment: main_window opens this EAGERLY at launch
    (_warm_serial) and holds it until quit, so the reset happens at launch and
    on upload, never at the start of a recording. Opening it lazily instead
    would just move the flash into recording #1.

    Because the board is no longer guaranteed to be freshly reset when a start
    command arrives, every start is confirmed by an `RDY <n_cams> <fps>` ack from
    the sketch. Without that check a mis-parsed config is indistinguishable from
    a good one until the recording comes back empty.

    THREADS. The start runs on the start worker and the stop, the quit and a
    bench Test run on the UI thread, all on this one object. RULE: every
    exchange with the board holds ``_lock``, and a start does not reset and
    retry once the owner has stopped or closed the link during it. REASON:
    without both, a quit that lands while a start waits for its ack stops the
    board, closes the port, and then the start's retry reopens the port and
    starts the board again, so the process exits with the board triggering and
    any paradigm running.
    """

    # Both budgets are headroom over the sketch's fixed ack latency, which is
    # ~1.5 s on EVERY config path, not 0.5 s: after the command is parsed the
    # sketch runs delay(500) and then drains its input with parseFloat().
    # readFPS()'s parseFloat stops AT the trailing newline without consuming
    # it, so the drain sees that one byte, discards it, and waits the Stream
    # default timeout (1000 ms; the sketch never calls setTimeout) for a digit
    # that never comes. Add USB/CDC latency and the 0.1 s read granularity.
    # The success path returns at the ack, so a wider budget costs nothing
    # when the board answers; only the fault path waits longer.
    #
    # A start after a forced reopen adds the board's reset and bootloader wait
    # (~1-2 s) to the 1.5 s, so an ack can take ~3.5 s; 4.0 s covers it.
    ACK_TIMEOUT = 4.0
    # A stop lands in loop()'s reconfigure branch: the same 1.5 s drain but no
    # bootloader wait. A budget of 2.0 s left ~0.35 s of margin, and a miss is
    # not benign on RDY firmware: it raises the "STOP NOT CONFIRMED" dialog at
    # the end of every ordinary acquisition and at quit. 3.0 s keeps ~1.4 s of
    # margin and stays below ACK_TIMEOUT because a stop runs on the UI thread.
    STOP_ACK_TIMEOUT = 3.0
    # Attempts for the reopen inside start_triggers(); the eager open at launch
    # keeps its own, longer count via open(retries=...).
    REOPEN_RETRIES = 3

    def __init__(self, port: str = "COM3", baudrate: int = 115200):
        self._port = port
        self._baudrate = baudrate
        self._ser = None
        #: Text of the SerialException from the most recent failed open(), for
        #: the caller's dialog; None after a successful open.
        self.last_error: str | None = None
        # True once ANY `RDY` line has been seen from this board, matching or
        # not. A board that answers `RDY 6 0` to a 100 fps request has
        # mis-parsed the config, so it must be refused rather than treated as
        # pre-RDY firmware: the legacy exemption exists only for boards that
        # never speak RDY at all.
        self._speaks_rdy = False
        #: Sketch identity (8 hex characters) carried by the most recent ack, or
        #: None when that ack carried none or did not arrive. Lets the host
        #: tell which sketch the board actually runs instead of trusting a
        #: per-machine record of what was last uploaded.
        #:
        #: RULE: cleared by every open() and at the start of every ack wait.
        #: REASON: an id kept across a reopen or a missed ack names the sketch
        #: the board ran BEFORE, typically the one a flash just replaced, and
        #: the host then decides on the wrong firmware.
        self.board_id: str | None = None
        #: Held for every exchange with the board. Re-entrant, because
        #: identify() stands the board down through stop_triggers().
        self._lock = threading.RLock()
        #: Counts the owner's stops and closes. start_triggers() compares it
        #: before its reset-and-retry, and a change means the owner stood the
        #: board down while the start was waiting for its ack.
        self._owner_interrupts = 0
        #: True when the most recent start_triggers() reset the board and sent
        #: the start a second time. Kept for the caller: the cameras were
        #: already armed during the first attempt.
        self.last_start_retried = False

    @property
    def _acks(self) -> bool:
        """Back-compat alias for _speaks_rdy (probe scripts read it)."""
        return self._speaks_rdy

    @property
    def speaks_rdy(self) -> bool:
        """True once any RDY line has come from this board.

        Firmware that has never printed one predates the handshake, and for it
        the reset-and-retry inside start_triggers() is the normal way a start
        takes effect.
        """
        return self._speaks_rdy

    def open(self, retries: int = 10) -> bool:
        """Open the port, retrying once a second. False after `retries` failures.

        The text of the last SerialException is kept in ``last_error`` and
        printed once, because pyserial folds the cause into the message
        ("could not open port ... FileNotFoundError" for a wrong COM number or an
        unplugged board, "PermissionError" for a port held by another program)
        and the two need different actions from the operator.
        """
        with self._lock:
            return self._open_locked(retries)

    def _open_locked(self, retries: int) -> bool:
        self.last_error = None
        # Opening resets the board, so whatever it said before is history.
        self.board_id = None
        if is_sim_port(self._port):
            return self._open_sim()
        for _ in range(retries):
            try:
                # NOTE: this pulses DTR and resets the board. That reset is
                # LOAD-BEARING — it returns the sketch to setup() with a cleared
                # serial RX buffer. Never suppress it (dtr=False before open):
                # the board then ignores the config and emits zero triggers
                # (Total_Packet_Count 0 on every camera).
                # The laser flash it causes is a hardware problem; the fix is to
                # keep this connection open rather than to defeat the reset.
                # write_timeout matters for safety, not just tidiness: without it
                # a write to a wedged or unplugged board blocks forever, and the
                # one write that must never hang is stop_triggers() — the command
                # that ends a paradigm and drives the laser pin low. With it, a
                # failed stop raises SerialTimeoutException (a SerialException
                # subclass) and the caller can tell the user instead of hanging.
                self._ser = serial.Serial(port=self._port, baudrate=self._baudrate,
                                          timeout=0.1, write_timeout=1.0)
                time.sleep(1.0)
                return True
            except serial.SerialException as e:
                self.last_error = str(e)
                time.sleep(1)
        print(f"[teensy] could not open {self._port} after {retries} attempt(s): "
              f"{self.last_error}", flush=True)
        return False

    def _open_sim(self) -> bool:
        """Stand up the simulated board in place of a serial port.

        gui_app.backends.sim_board is imported lazily so a rig without it pays
        nothing and the absence reads as a clear message rather than an import
        error at GUI start. SimSerial takes the same keywords as serial.Serial
        so the rest of this class cannot tell the difference. No sleep: there
        is no bootloader to wait for.
        """
        try:
            from gui_app.backends import sim_board
        except ImportError as e:
            self.last_error = (f"simulated board requested (port {self._port!r}) "
                               f"but gui_app.backends.sim_board is not available: {e}")
            print(f"[teensy] {self.last_error}", flush=True)
            return False
        self._ser = sim_board.SimSerial(port=self._port, baudrate=self._baudrate,
                                        timeout=0.1, write_timeout=1.0)
        print("[teensy] simulated board opened", flush=True)
        return True

    def start_triggers(self, pins: list[int], fps: int, may_retry=None) -> bool:
        """Configure the board for acquisition and confirm it understood.

        Tries the existing connection first (no reset, no laser flash). If the
        board does not confirm, reopens the port to force a reset — the proven
        path — and retries. Returns False only when the board is genuinely
        unreachable, so the caller can abort instead of recording nothing.

        The retry is skipped, and the start fails, in two cases:

        - The owner stopped or closed the link while the first attempt waited
          for its ack. The stop is the owner's last word, and a retry after it
          would start a board the owner has just stood down.
        - ``may_retry``, a zero-argument callable asked immediately before the
          reset, returns False. The caller knows what the reset cannot undo:
          cameras armed during the first attempt have already counted every
          trigger it fired.

        ``last_start_retried`` records whether this call reset and resent.
        """
        with self._lock:
            interrupts = self._owner_interrupts
            self.last_start_retried = False
            if not self._ser:
                print("[teensy] start_triggers called but port not open", flush=True)
                return False

            if self._send(pins, fps):
                return True

            if self._owner_interrupts != interrupts:
                print("[teensy] no ack, and the link was stopped or closed "
                      "while the start waited: not retrying", flush=True)
                return False
            if may_retry is not None and not may_retry():
                print("[teensy] no ack, and the caller refused the reset and "
                      "retry", flush=True)
                return False
            print("[teensy] no ack — reopening port to force a board reset", flush=True)
            self.last_start_retried = True
            self._close_port()
            # Few retries here: this runs on the start worker at Record and on
            # the UI thread for a bench Test, and the port was open a moment
            # ago, so a failure now is a vanished or seized device that ten
            # more seconds of retrying cannot bring back.
            if not self.open(retries=self.REOPEN_RETRIES):
                print(f"[teensy] could not reopen port: {self.last_error}", flush=True)
                return False
            return self._finish_retry(pins, fps)

    def _finish_retry(self, pins: list[int], fps: int) -> bool:
        """The second attempt of start_triggers(), after the forced reset."""
        if self._send(pins, fps):
            return True

        if self._speaks_rdy:
            # This board speaks RDY, so a missing or mismatched ack is a real
            # fault: either it went silent or it mis-parsed the config. Both
            # would record an empty session, so refuse.
            print("[teensy] board speaks RDY but did not confirm — aborting", flush=True)
            return False
        # No RDY line has ever come from this board: firmware predating the
        # handshake (stock trigger.ino). It has just been reset, which is what
        # that firmware needs, so let the recording proceed.
        print("[teensy] no ack support detected — assuming pre-RDY firmware", flush=True)
        return True

    def _send(self, pins: list[int], fps: int) -> bool:
        # The trailing newline terminates readFPS()'s parseFloat immediately
        # instead of letting it burn its 1 s timeout; the sketch's drain still
        # burns one timeout on that newline, which the ack budgets cover.
        # Harmless to older firmware.
        cmd = ",".join(str(x) for x in [len(pins)] + list(pins) + [fps]) + "\n"
        try:
            self._ser.reset_input_buffer()
            self._ser.write(cmd.encode())
        except (serial.SerialException, OSError) as e:
            print(f"[teensy] write failed: {e}", flush=True)
            return False
        print(f"[teensy] sent: {cmd!r}", flush=True)
        return self._await_ack(len(pins), fps)

    def _await_ack(self, n_pins: int, fps: int, timeout: float | None = None) -> bool:
        """Wait for one complete ``RDY <n_pins> <fps>`` line and return whether
        the board echoed exactly the configuration that was sent.

        Lines are matched whole and compared as integers, because the ack
        exists to catch a mis-parsed config and a substring test would let
        ``RDY 6 1000`` stand in for ``RDY 6 100``. Any RDY line, matching or
        not, marks the board as RDY-speaking; a mismatched line fails at once
        because the board has already answered and a second, correct line is
        not coming.
        """
        want_n, want_fps = int(n_pins), max(int(fps), 0)
        want = f"RDY {want_n} {want_fps}"
        # The identity is whatever THIS ack says; a missed ack leaves none.
        self.board_id = None
        ser = self._ser
        if ser is None:
            print(f"[teensy] wanted {want!r}, but the link is closed", flush=True)
            return False
        deadline = time.monotonic() + (self.ACK_TIMEOUT if timeout is None else timeout)
        buf = ""
        while time.monotonic() < deadline:
            try:
                chunk = ser.read(64)
            except (serial.SerialException, OSError) as e:
                print(f"[teensy] read failed: {e}", flush=True)
                return False
            if not chunk:
                continue
            buf += chunk.decode("ascii", "ignore")
            # Only complete lines are judged; a partial tail stays in buf for
            # the next read so a line split across two reads is not lost.
            *lines, buf = buf.split("\n")
            for line in lines:
                if "RDY " not in line:
                    continue
                # The board speaks RDY, whatever the rest of the line says, so
                # the pre-RDY exemption in start_triggers no longer applies.
                self._speaks_rdy = True
                m = _RDY_LINE.fullmatch(line.strip())
                if not m:
                    print(f"[teensy] wanted {want!r}, got garbled ack "
                          f"{line.strip()!r}", flush=True)
                    return False
                self.board_id = (m.group(3) or "").lower() or None
                got_n, got_fps = int(m.group(1)), int(m.group(2))
                if (got_n, got_fps) == (want_n, want_fps):
                    print(f"[teensy] ack {want!r}"
                          + (f" id={self.board_id}" if self.board_id else ""),
                          flush=True)
                    return True
                print(f"[teensy] wanted {want!r}, got {line.strip()!r} — "
                      f"the board mis-parsed the command", flush=True)
                return False
        if buf.strip():
            print(f"[teensy] wanted {want!r}, got {buf.strip()!r}", flush=True)
        return False

    def stop_triggers(self, pins: list[int]) -> bool:
        """Stop triggering and drive the stim pins low.

        Returns True when the write succeeded and, on RDY firmware, the board
        acked the stop with ``RDY <n_pins> 0``. On firmware that has never
        spoken RDY the write alone counts, so stock trigger.ino rigs do not get
        a "stop not confirmed" dialog they cannot act on.

        This cannot be fire-and-forget. The sketch's reconfigure branch runs
        `camsLow(); allStimLow(); FPS_OUT=0`, so this command is what ends a
        paradigm — and a *looping* stim chain otherwise runs forever, driving the
        laser pin with the GUI showing IDLE. A write that leaves the host is not
        proof the board acted: a wedged sketch, a board still in its bootloader
        after an upload, or foreign firmware all accept the bytes and stop
        nothing, so the ack is what confirms the stand-down.

        Waiting for the ack also serialises stop and start on the host: the
        sketch drains its input for ~1.5 s before acking (see STOP_ACK_TIMEOUT),
        and a start written into that window is swallowed, times out and forces
        a port reset — the laser flash the long-lived connection exists to avoid.

        The caller MUST NOT infer success from the port being open: pyserial's
        `is_open` stays True after the USB device disappears, so an unplugged
        cable looks healthy right up until the write fails.

        A stop issued while a start waits for its ack on another thread runs
        after that attempt, and the start then does not reset and retry.
        """
        self._owner_interrupts += 1
        with self._lock:
            return self._stop_locked(pins)

    def _stop_locked(self, pins: list[int]) -> bool:
        if not self._ser:
            print("[teensy] STOP NOT SENT: no serial link. The board may still be "
                  "triggering and any stim paradigm may still be running.",
                  flush=True)
            return False
        cmd = ",".join(str(x) for x in [len(pins)] + list(pins) + [-1]) + "\n"
        try:
            # Discard anything the board printed since the last exchange so the
            # ack read below judges only the reply to this command.
            self._ser.reset_input_buffer()
            self._ser.write(cmd.encode())
        except (serial.SerialException, OSError) as e:
            print(f"[teensy] STOP WRITE FAILED: {e} — the board may still be "
                  f"triggering and any stim paradigm may still be running. "
                  f"Power-cycle the board and key off the laser.", flush=True)
            return False
        print(f"[teensy] sent stop: {cmd!r}", flush=True)
        if not self._speaks_rdy:
            return True
        # readFPS() clamps the -1 to 0, so the stop is acked as `RDY <n> 0`.
        if self._await_ack(len(pins), 0, timeout=self.STOP_ACK_TIMEOUT):
            return True
        print("[teensy] STOP NOT CONFIRMED: the board speaks RDY but did not ack "
              "the stop — it may still be triggering and any stim paradigm may "
              "still be running. Power-cycle the board and key off the laser.",
              flush=True)
        return False

    def identify(self, pins: list[int] = ()) -> str | None:
        """Stand the board down and read the sketch identity it answers with.

        RULE: the ack is awaited once even when this controller has never
        heard a RDY line. REASON: stop_triggers() skips the ack until one has
        been seen, and at launch none has — a freshly constructed controller,
        before any start — so firmware that DOES speak RDY is mislabelled
        pre-RDY, no identity is ever read, and the launch-time decision falls
        back to the per-machine hint. A board flashed from the Arduino IDE,
        swapped, or shared with a second rig is exactly what the hint cannot
        see and exactly what the identity is for. Pre-RDY firmware answers
        nothing and costs one STOP_ACK_TIMEOUT, once per launch.

        A stop is the one config always safe to send: it drives the camera
        pins and every stim pin LOW, which is also the right state for a board
        found carrying a previous session's paradigm.

        Returns ``board_id`` — None when the link is down, when the stop's ack
        did not arrive, or when the firmware prints no identity. An id from an
        earlier exchange is never returned, because after a reflash it names
        the sketch the flash replaced.
        """
        pins = list(pins)
        with self._lock:
            self.board_id = None
            self.stop_triggers(pins)
            if self._ser and not self._speaks_rdy:
                # readFPS() clamps the stop's -1 to 0, so the board acks it as
                # `RDY <n> 0` and the identity rides on that line.
                try:
                    self._await_ack(len(pins), 0, timeout=self.STOP_ACK_TIMEOUT)
                except (serial.SerialException, OSError) as e:
                    print(f"[teensy] could not read the board's identity: {e}",
                          flush=True)
            return self.board_id

    def close(self):
        """Close the link. A start in flight on another thread does not reset
        and retry after this, so it cannot reopen the port behind the owner."""
        self._owner_interrupts += 1
        with self._lock:
            self._close_port()

    def stop_and_close(self, pins: list[int]) -> bool:
        """Stand the board down and close the link, with no start between.

        RULE: the quit path uses this, never stop_triggers() then close().
        REASON: between those two calls the lock is free, so a start waiting
        on it writes its command after the stop, and the process exits with
        the board triggering. Returns what the stop returned.
        """
        self._owner_interrupts += 1
        with self._lock:
            try:
                return self._stop_locked(pins)
            finally:
                self._close_port()

    def _close_port(self):
        ser, self._ser = self._ser, None
        if ser is not None and ser.is_open:
            ser.close()

    def port_alive(self) -> bool:
        """False when the link is closed or the device behind it is gone.

        pyserial keeps ``is_open`` True after the USB device disappears, so
        the port is asked for its input count, which raises on a vanished
        device. Asked without waiting: while another thread holds the link
        for an exchange, that exchange reports its own failure, so the link
        counts as alive here. A stand-in port with no device behind it (the
        simulated board) has no input count and counts as alive while open.
        """
        ser = self._ser
        if ser is None or not ser.is_open:
            return False
        if not self._lock.acquire(blocking=False):
            return True
        try:
            ser.in_waiting
        except AttributeError:
            return True
        except (serial.SerialException, OSError):
            return False
        finally:
            self._lock.release()
        return True

    @property
    def port(self) -> str:
        return self._port

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open
