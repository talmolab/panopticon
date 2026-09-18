"""Prove the trigger-board handshake degrades safely in every direction.

Background: the serial connection is now held open across recordings, because
opening it resets the Arduino and floats every pin for ~1-2 s — long enough for
a connected laser driver to fire. The cost is that a start command may land in
the sketch's loop() reconfigure branch rather than a freshly reset setup(). On
2026-07-26 that path silently failed and produced a 15 s recording with zero
triggers, so every start is now confirmed by an `RDY <n_cams> <fps>` ack.

The rules pinned down here:
  - confirmed on the open connection   -> no reset, no laser flash
  - not confirmed                      -> reopen (reset) and retry
  - firmware with no ack support       -> proceed after the reset (old behaviour)
  - board that acked before, now mute  -> hard failure, refuse to record

The last two are the ones that matter. Conflating them either locks anyone on
stock trigger.ino out of recording, or lets a real fault through as an empty
session — which is the exact bug this whole mechanism exists to catch.

    python test_serial_handshake.py
"""
import sys
import types

# Stub pyserial so this runs with no dependency and no COM port.
_fake = types.ModuleType("serial")
class SerialException(Exception):
    pass
_fake.SerialException = SerialException
_fake.Serial = lambda *a, **k: None
sys.modules.setdefault("serial", _fake)

from gui_app.serial_controller import TeensyController

PINS = [2, 4, 6, 8, 10, 12]
START = "6,2,4,6,8,10,12,100"
ACK = b"RDY 6 100\r\n"


class FakePort:
    """pyserial stand-in. `respond(cmd, generation) -> bytes` lets a test model a
    board whose behaviour changes across resets; generation counts port opens."""

    def __init__(self, respond, generation, log):
        self.is_open = True
        self._respond = respond
        self._generation = generation
        self._log = log
        self._out = b""

    def reset_input_buffer(self):
        self._out = b""

    def write(self, data):
        cmd = data.decode()
        self._log.append(("write", cmd))
        self._out = self._respond(cmd.strip(), self._generation)

    def read(self, n):
        chunk, self._out = self._out[:n], self._out[n:]
        return chunk

    def close(self):
        self.is_open = False
        self._log.append(("close",))


def controller(respond):
    """TeensyController wired to a FakePort. Returns (controller, log, state);
    state['opens'] counts reopens *after* construction, i.e. forced resets."""
    log = []
    state = {"opens": 0, "generation": 1}
    c = TeensyController(port="COMTEST")
    c.ACK_TIMEOUT = 0.3          # keep the no-ack paths quick

    def fake_open(retries=10):
        state["opens"] += 1
        state["generation"] += 1
        log.append(("open",))
        c._ser = FakePort(respond, state["generation"], log)
        return True

    c.open = fake_open
    c._ser = FakePort(respond, 1, log)   # initial connection, not counted
    return c, log, state


def test_confirmed_no_reset():
    c, log, state = controller(lambda cmd, gen: ACK if cmd == START else b"")
    assert c.start_triggers(PINS, 100) is True
    assert state["opens"] == 0, "reopened the port despite a valid ack"
    assert [k for k, *_ in log] == ["write"], f"unexpected traffic: {log}"
    print("1) ack on the open connection -> no reset, no flash: PASS")


def test_retry_after_reset_succeeds():
    """First attempt mute (a wedged reconfigure); the forced reset recovers it."""
    c, log, state = controller(lambda cmd, gen: ACK if gen >= 2 else b"")
    assert c.start_triggers(PINS, 100) is True
    assert state["opens"] == 1, "did not force a reset after the silent attempt"
    assert [k for k, *_ in log] == ["write", "close", "open", "write"], log
    print("2) silence -> reopen (reset) -> confirmed: PASS")


def test_legacy_firmware_still_records():
    """Stock trigger.ino never sends RDY. It must not be locked out."""
    c, log, state = controller(lambda cmd, gen: b"")
    assert c.start_triggers(PINS, 100) is True, \
        "pre-RDY firmware refused — this would brick camera-only recording"
    assert c._speaks_rdy is False
    assert state["opens"] == 1, "should still have reset before giving up"
    print("3) firmware without RDY support -> proceeds after reset: PASS")


def test_regression_is_a_hard_failure():
    """A board that has acked before going quiet is a genuine fault, not old
    firmware. This is the 2026-07-26 zero-trigger case; it must abort."""
    alive = {"v": True}
    c, log, state = controller(
        lambda cmd, gen: ACK if (alive["v"] and cmd == START) else b"")
    assert c.start_triggers(PINS, 100) is True    # teaches it this board acks
    assert c._speaks_rdy is True
    alive["v"] = False                            # board goes quiet, stays quiet
    assert c.start_triggers(PINS, 100) is False, \
        "a known-acking board went silent and we recorded anyway"
    print("4) known-acking board goes silent -> refuses to record: PASS")


def test_ack_must_match_the_command():
    """A board that answers RDY with the wrong numbers has mis-parsed the
    config and will fire nothing. It speaks RDY, so it is NOT legacy firmware
    and the start must fail rather than fall through to the legacy exemption."""
    c, log, state = controller(lambda cmd, gen: b"RDY 6 0\r\n")   # fps 0, not 100
    assert c.start_triggers(PINS, 100) is False, \
        "a mismatched RDY was treated as pre-RDY firmware and recording proceeded"
    assert c._speaks_rdy is True, "a RDY line was seen but not classified as such"
    assert state["opens"] == 1, "should have retried once after a reset"
    print("5) ack with mismatched fps -> start refused, board marked RDY: PASS")


def test_ack_is_matched_as_a_whole_line():
    """`RDY 6 1000` must not satisfy a request for 100 fps: the ack exists to
    catch parse errors, and an extra digit is exactly what a stray byte makes."""
    c, log, state = controller(lambda cmd, gen: b"RDY 6 1000\r\n")
    assert c.start_triggers(PINS, 100) is False, "superstring ack accepted"
    # Integer comparison, not text: leading zeros and CR line endings are fine.
    c, log, state = controller(lambda cmd, gen: b"RDY 06 0100\r\n")
    assert c.start_triggers(PINS, 100) is True
    # A line split across two reads is still recognised.
    c, log, state = controller(lambda cmd, gen: b"RDY 6 100\r\n")
    c._ser.read = lambda n, _r=c._ser.read: _r(3)
    assert c.start_triggers(PINS, 100) is True, "ack split across reads was lost"
    # Noise before the ack does not hide it.
    c, log, state = controller(lambda cmd, gen: b"\xff\x00boot\r\nRDY 6 100\r\n")
    assert c.start_triggers(PINS, 100) is True
    print("5b) ack matched as a whole line with integer comparison: PASS")


def board(n_cams):
    """A well-behaved Panopticon sketch: echoes the camera count and the fps it
    parsed, clamping the stop's -1 to 0 the way readFPS() does."""
    def respond(cmd, gen):
        fields = cmd.split(",")
        fps = max(int(fields[-1]), 0)
        return f"RDY {n_cams} {fps}\r\n".encode()
    return respond


def test_test_mode_command_shape():
    """The Test button sends zero camera pins, so no TTLs reach the cameras."""
    c, log, state = controller(board(0))
    assert c.start_triggers([], 100) is True
    assert log[0] == ("write", "0,100\n"), log
    assert c.stop_triggers([]) is True
    assert log[-1] == ("write", "0,-1\n"), log
    print("6) test-mode sends 0 camera pins, stop sends -1: PASS")


def test_stop_is_confirmed_on_rdy_firmware():
    """A stop is what ends a paradigm, so on RDY firmware it is confirmed by the
    `RDY n 0` the sketch prints from its reconfigure branch; the write leaving
    the host proves nothing about the board."""
    c, log, state = controller(board(6))
    assert c.start_triggers(PINS, 100) is True
    assert c.stop_triggers(PINS) is True, "stop ack `RDY 6 0` not accepted"
    assert [k for k, *_ in log] == ["write", "write"], log

    # Same board, but it goes mute on the stop: the write succeeds and the
    # caller must still hear that the board did not stand down.
    c, log, state = controller(
        lambda cmd, gen: b"RDY 6 100\r\n" if cmd == START else b"")
    c.STOP_ACK_TIMEOUT = 0.3
    assert c.start_triggers(PINS, 100) is True
    assert c.stop_triggers(PINS) is False, "unconfirmed stop reported as success"

    # A stop acked with the wrong camera count is a mis-parse, not a stand-down.
    c, log, state = controller(
        lambda cmd, gen: b"RDY 6 100\r\n" if cmd == START else b"RDY 2 0\r\n")
    assert c.start_triggers(PINS, 100) is True
    assert c.stop_triggers(PINS) is False

    # Firmware that never spoke RDY keeps write-only semantics, so a stock
    # trigger.ino rig is not shown a dialog it cannot act on.
    c, log, state = controller(lambda cmd, gen: b"")
    assert c.stop_triggers(PINS) is True
    assert c._speaks_rdy is False
    print("8) stop confirmed by `RDY n 0` on RDY firmware, write-only on legacy: PASS")


def test_stop_failure_paths_return_false():
    """The two ways a stop can fail before the board is even asked."""
    c, log, state = controller(board(6))
    c._ser = None
    assert c.stop_triggers(PINS) is False, "no link reported as a stop"

    c, log, state = controller(board(6))
    def broken_write(data):
        raise SerialException("write timeout")
    c._ser.write = broken_write
    assert c.stop_triggers(PINS) is False, "a failed write reported as a stop"
    print("9) stop with no link / failing write -> False: PASS")


def test_trailing_newline():
    """Terminates the sketch's final parseFloat instead of burning its 1 s
    timeout. Harmless to pre-RDY firmware."""
    c, log, state = controller(lambda cmd, gen: ACK)
    c.start_triggers(PINS, 100)
    assert log[0][1].endswith("\n"), "no terminator on the config command"
    print("7) config command is newline-terminated: PASS")


def test_board_id_is_captured_from_the_ack():
    """Firmware appends an 8-hex sketch id to the RDY line; the controller
    keeps it as board_id so the host can compare it with the wanted sketch.
    Older firmware without the field leaves board_id None."""
    c, log, state = controller(lambda cmd, gen: b"RDY 6 100 0AbC12ef\r\n")
    assert c.board_id is None
    assert c.start_triggers(PINS, 100) is True, "an ack with an id was rejected"
    assert c.board_id == "0abc12ef", c.board_id
    # A stop ack from the same sketch carries the id too.
    c._ser._respond = lambda cmd, gen: b"RDY 6 0 0abc12ef\r\n"
    assert c.stop_triggers(PINS) is True
    # Firmware without the field: still accepted, id unknown.
    c, log, state = controller(lambda cmd, gen: ACK)
    assert c.start_triggers(PINS, 100) is True
    assert c.board_id is None
    # A malformed id is not an ack at all (the line is not judged), so a board
    # printing garbage after the numbers cannot pass as a confirmed start.
    c, log, state = controller(lambda cmd, gen: b"RDY 6 100 xyz\r\n")
    assert c.start_triggers(PINS, 100) is False
    print("11) sketch id in the RDY line is captured as board_id: PASS")


def test_reopen_failure_and_error_text():
    """A reopen that fails ends the start with False, and open() keeps the
    reason so the operator can tell a missing port from a held one."""
    c, log, state = controller(lambda cmd, gen: b"")
    c.open = lambda retries=10: False
    assert c.start_triggers(PINS, 100) is False, "recorded with no port at all"

    import gui_app.serial_controller as scm
    real_serial, real_sleep = scm.serial.Serial, scm.time.sleep
    attempts = []
    def failing_serial(**kw):
        attempts.append(kw["port"])
        raise SerialException("could not open port 'COM9': PermissionError(13)")
    scm.serial.Serial, scm.time.sleep = failing_serial, lambda s: None
    try:
        fresh = TeensyController(port="COM9")
        assert fresh.open(retries=3) is False
        assert len(attempts) == 3, attempts
        assert "PermissionError" in fresh.last_error, fresh.last_error
        assert fresh.REOPEN_RETRIES <= 3, "in-start reopen would block the UI too long"
    finally:
        scm.serial.Serial, scm.time.sleep = real_serial, real_sleep
    print("10) reopen failure -> start False; open() records the error text: PASS")


def main():
    test_confirmed_no_reset()
    test_retry_after_reset_succeeds()
    test_legacy_firmware_still_records()
    test_regression_is_a_hard_failure()
    test_ack_must_match_the_command()
    test_ack_is_matched_as_a_whole_line()
    test_test_mode_command_shape()
    test_trailing_newline()
    test_stop_is_confirmed_on_rdy_firmware()
    test_stop_failure_paths_return_false()
    test_reopen_failure_and_error_text()
    test_board_id_is_captured_from_the_ack()
    print("\nALL SERIAL HANDSHAKE TESTS PASS")


if __name__ == "__main__":
    main()
