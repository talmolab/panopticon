"""Lock down the stimulus workflow -> Arduino sketch translation.

The graph semantics here are easy to break and hard to notice on the rig: a
mis-resolved start silently produces no chains, a cycle used to hang the walker
forever, and a pulse width at or above the period once compiled to a pin that
never fired. Each of those cost a debugging session, so they are pinned here.

No Qt, no serial, no arduino-cli, so it runs anywhere:
    python test_stim_compiler.py

The per-frame trace tests (10-13) additionally need numpy and skip without it;
`uv run python test_stim_compiler.py` runs the full set.
"""
from gui_app import stim_compiler as sc

try:
    import numpy  # noqa: F401
    _HAS_NUMPY = True
except ImportError:                       # keeps 1-9 runnable on a bare python
    _HAS_NUMPY = False


def B(bid, dur=1.0, pin=53, freq=10.0, pw=10.0, start=False, end=False):
    return {"id": bid, "x": 0, "y": 0, "pin": pin, "freq": freq, "pw": pw,
            "dur": dur, "start": start, "end": end}


def E(src, dst):
    return {"src": src, "dst": dst}


def ids(chain):
    return [b["id"] for b in chain]


def test_start_resolution():
    # Linear: the block with no incoming arrow starts.
    starts, needs = sc.resolve_starts([B("A"), B("B"), B("C")],
                                      [E("A", "B"), E("B", "C")])
    assert starts == {"A"} and needs == set()

    # Fan-in: both sources resolve as starts here; compile_ino then refuses
    # the graph because C would run in two chains at once (test 2b).
    starts, needs = sc.resolve_starts([B("A"), B("B"), B("C")],
                                      [E("A", "C"), E("B", "C")])
    assert starts == {"A", "B"} and needs == set()

    # Pure loop with no flag: nothing can start, everything is flagged as stuck.
    loop_edges = [E("A", "B"), E("B", "A")]
    starts, needs = sc.resolve_starts([B("A"), B("B")], loop_edges)
    assert starts == set() and needs == {"A", "B"}

    # Same loop, one block pinned: that block wins, nothing is stuck.
    starts, needs = sc.resolve_starts([B("A", start=True), B("B")], loop_edges)
    assert starts == {"A"} and needs == set()

    # An explicit flag overrides the no-incoming-arrow rule within its group,
    # so a chain can be made to begin midway.
    starts, _ = sc.resolve_starts([B("A"), B("B", start=True), B("C")],
                                  [E("A", "B"), E("B", "C")])
    assert starts == {"B"}

    # Disconnected groups resolve independently.
    starts, _ = sc.resolve_starts([B("A"), B("B"), B("C"), B("D")],
                                  [E("A", "B"), E("C", "D")])
    assert starts == {"A", "C"}
    print("1) start resolution (linear/fan-in/loop/pinned/split): PASS")


def test_chain_extraction_terminates():
    chains = sc._extract_chains([B("A"), B("B"), B("C")],
                                [E("A", "B"), E("B", "C")])
    assert [(ids(c), l) for c, l in chains] == [(["A", "B", "C"], -1)]

    # Pinned loop closes back on itself instead of running forever.
    chains = sc._extract_chains([B("A", start=True), B("B")],
                                [E("A", "B"), E("B", "A")])
    assert [(ids(c), l) for c, l in chains] == [(["A", "B"], 0)]

    # Rho shape: a tail feeding a loop. This is the case that used to spin
    # forever, because no block in the cycle is a source.
    chains = sc._extract_chains([B("A"), B("B"), B("C")],
                                [E("A", "B"), E("B", "C"), E("C", "B")])
    assert [(ids(c), l) for c, l in chains] == [(["A", "B", "C"], 1)]

    # A loop nobody starts produces nothing rather than a bogus chain.
    assert sc._extract_chains([B("A"), B("B")], [E("A", "B"), E("B", "A")]) == []
    print("2) chain extraction terminates on cycles (linear/loop/rho): PASS")


def test_structural_problems():
    """Shapes the walker would silently collapse are refused with a message
    that names the block, instead of compiling a different paradigm."""
    # Two outgoing arrows: the walker used to keep the last edge and drop B.
    blocks, edges = [B("A"), B("B"), B("C")], [E("A", "B"), E("A", "C")]
    probs = sc.structural_problems(blocks, edges)
    assert len(probs) == 1 and "block A" in probs[0] and "outgoing" in probs[0], probs
    assert refuses(blocks, edges), "two out-edges compiled to a single chain"
    # The same edge listed twice is one successor, not a branch.
    assert sc.structural_problems([B("A"), B("B")], [E("A", "B"), E("A", "B")]) == []

    # Fan-in: A->C, B->C puts C in two concurrent chains. pin_conflicts() used
    # to name the symptom (the pin) with no pin choice that could fix it.
    blocks, edges = [B("A", pin=44), B("B", pin=45), B("C", pin=46)], \
        [E("A", "C"), E("B", "C")]
    probs = sc.structural_problems(blocks, edges)
    assert len(probs) == 1 and "block C" in probs[0] and "fan-in" in probs[0], probs
    assert refuses(blocks, edges), "fan-in compiled with C in two chains"

    # A lead-in feeding a loop (rho) re-enters B from its own chain: one chain
    # looping, so it is accepted and keeps its loop_to.
    rho_b, rho_e = [B("A"), B("B"), B("C")], [E("A", "B"), E("B", "C"), E("C", "B")]
    assert sc.structural_problems(rho_b, rho_e) == []
    assert "{CHAIN_0, CHAIN_0_LEN, 1}" in sc.compile_ino(rho_b, rho_e, [53])
    # Plain shapes are untouched: linear, pinned loop, two independent chains.
    assert sc.structural_problems([B("A"), B("B")], [E("A", "B")]) == []
    assert sc.structural_problems([B("A", start=True), B("B")],
                                  [E("A", "B"), E("B", "A")]) == []
    assert sc.structural_problems([B("A"), B("B", pin=44)], []) == []
    # Edges to unknown ids are ignored, as everywhere else in the compiler.
    assert sc.structural_problems([B("A")], [E("A", "ghost"), E("A", "ghost2")]) == []
    print("2b) two out-edges and fan-in refused with the block named: PASS")


def test_waveform_encoding():
    """freq/pulse-width -> integer microseconds, including the 100%-duty case."""
    def blk_line(freq, pw):
        ino = sc.compile_ino([B("A", dur=5, freq=freq, pw=pw)], [])
        return [l for l in ino.splitlines() if l.startswith("  {53u")][0]

    # 10 Hz / 10 ms = a real train: 100000 us period, 10000 us pulse.
    assert "{53u, 100000UL, 10000UL, 5000UL}" in blk_line(10, 10)
    # 10 Hz / 100 ms: pulse == period. Must still emit the pin, not skip it --
    # the sketch reads pw >= period as constant ON.
    assert "{53u, 100000UL, 100000UL, 5000UL}" in blk_line(10, 100)
    # 0 Hz = period 0 = hold LOW.
    assert "{53u, 0UL," in blk_line(0, 100)

    # No floating point may reach the sketch: updateStim() runs inside the
    # camera trigger busy-wait, where an AVR float divide (~30 us) blunts the
    # trigger edge precision.
    ino = sc.compile_ino([B("A", dur=5, freq=10, pw=10)], [])
    body = ino.split("void updateStim()")[1].split("// ===== SETUP")[0]
    assert "float" not in body and "0f" not in body, "float math in updateStim()"
    print("3) waveform -> integer microseconds, no floats in updateStim: PASS")


def test_safe_pins():
    # The laser pin is held LOW even when the workflow is empty.
    assert "const uint8_t STIM_PINS[] = {53};" in sc.compile_ino([], [], [53])
    # Workflow pins are unioned in, sorted.
    ino = sc.compile_ino([B("A", pin=44)], [], [53])
    assert "const uint8_t STIM_PINS[] = {44, 53};" in ino
    # A rig with no stim hardware must still emit valid C++ (no zero-size array).
    empty = sc.compile_ino([], [], [])
    assert "const uint8_t STIM_PINS[] = {0};" in empty
    assert "const int N_STIM_PINS = 0;" in empty
    # Pins go LOW before Serial.begin -- setup() blocks on the handshake, so
    # anything after it leaves the pin floating until the GUI connects.
    setup = sc.compile_ino([], [], [53]).split("void setup()")[1]
    assert setup.index("allStimLow();") < setup.index("Serial.begin")
    print("4) safe pins: empty workflow, union, no-stim rig, boot order: PASS")


def test_pin_conflicts():
    # Same pin twice in ONE chain is fine -- the blocks run in sequence.
    assert sc.pin_conflicts([B("A", pin=53), B("B", pin=53)], [E("A", "B")]) == []
    # Two independent chains on one pin would fight over the output.
    assert sc.pin_conflicts([B("A", pin=53), B("B", pin=53)], []) == [53]
    # Distinct pins are fine.
    assert sc.pin_conflicts([B("A", pin=53), B("B", pin=44)], []) == []
    print("5) pin conflict detection (within vs across chains): PASS")


def test_forbidden_pins():
    TRIG = [2, 4, 6, 8, 10, 12]
    # A stim block on a camera trigger line injects extra rising edges into ONE
    # camera, so its block IDs advance faster and block-ID N stops meaning the
    # same instant across cameras. frame_sync/alignment/stim_trace all assume
    # that identity, so nothing downstream can detect it -- refuse at compile.
    bad = sc.forbidden_pin_uses([B("A", pin=6)], TRIG)
    assert [p for p, _ in bad] == [6], bad
    assert "trigger" in bad[0][1]
    # RX0/TX0 garble the serial link and the RDY ack. Checked with no profile.
    assert [p for p, _ in sc.forbidden_pin_uses([B("A", pin=0)])] == [0]
    assert [p for p, _ in sc.forbidden_pin_uses([B("A", pin=1)])] == [1]
    # A legitimate stim pin is untouched.
    assert sc.forbidden_pin_uses([B("A", pin=53)], TRIG) == []
    # compile_ino must REFUSE, so the .ino can never reach the board.
    for pin in (6, 0):
        try:
            sc.compile_ino([B("A", pin=pin)], [], [53], TRIG)
        except ValueError:
            pass
        else:
            raise AssertionError(f"compile_ino accepted a block on pin {pin}")
    # And still compiles normally for a good graph.
    assert "void setup()" in sc.compile_ino([B("A", pin=53)], [], [53], TRIG)
    print("5b) forbidden pins: trigger lines + RX0/TX0 refused at compile: PASS")


def refuses(blocks, edges=(), safe=(53,), trig=(2, 4, 6, 8, 10, 12)):
    """True iff compile_ino raises ValueError for this graph."""
    try:
        sc.compile_ino(blocks, list(edges), list(safe), list(trig))
    except ValueError:
        return True
    return False


def test_pin_range():
    """A pin the board does not have compiles to a silent no-op (70..255) or,
    above 255, truncates onto a different physical pin; 530 for 53 is one
    keystroke. Refuse at compile so the .ino never reaches the board."""
    for pin in (-1, 70, 255, 256, 530):
        bad = sc.forbidden_pin_uses([B("A", pin=pin)])
        assert [p for p, _ in bad] == [pin], (pin, bad)
        assert "not a digital pin" in bad[0][1]
        assert refuses([B("A", pin=pin)]), f"compile_ino accepted pin {pin}"
    # The board's full range is accepted: 2 is the first free pin, 69 = A15.
    for pin in (2, 53, 69):
        assert sc.forbidden_pin_uses([B("A", pin=pin)]) == [], pin
    # The ceiling is a parameter, so another board can widen or narrow it.
    assert sc.forbidden_pin_uses([B("A", pin=70)], max_pin=80) == []
    assert sc.MEGA_MAX_DIGITAL_PIN == 69
    print("5d) pins outside 2..69 refused at compile: PASS")


def test_parameter_ranges():
    """Waveform numbers the firmware cannot execute as written are refused
    instead of compiling to a block that silently holds LOW or spins."""
    # freq >= 2 MHz rounds to a 0 us period, which the sketch reads as 'hold LOW'.
    assert refuses([B("A", freq=2e6)]), "2 MHz compiled to a silent LOW block"
    assert [bid for bid, _ in sc.parameter_problems([B("A", freq=2e6)])] == ["A"]
    # A frequency with a pulse width that rounds to 0 us is the same silent LOW.
    assert refuses([B("A", freq=10, pw=0.0001)])
    assert refuses([B("A", freq=10, pw=0)])
    # An off period is freq 0 (pw irrelevant), and that is still accepted.
    assert sc.parameter_problems([B("A", freq=0, pw=0)]) == []
    assert sc.parameter_problems([B("A", freq=0, pw=100)]) == []
    # dur < 1 ms rounds to 0 ms: a zero-length block spins the advance loop.
    assert refuses([B("A", dur=0)]), "dur 0 compiled"
    assert refuses([B("A", dur=0.0004)])
    assert sc.parameter_problems([B("A", dur=0.001)]) == []
    # Anything above uint32 truncates in the sketch's fields.
    assert refuses([B("A", freq=1e-4)]), "period 1e10 us accepted"
    assert refuses([B("A", pw=5e6)]), "pulse width 5e9 us accepted"
    assert refuses([B("A", dur=5e6)]), "duration 5e9 ms accepted"
    # Negative numbers are not a waveform.
    assert refuses([B("A", freq=-10)]) and refuses([B("A", pw=-1)]) \
        and refuses([B("A", dur=-1)])
    # pw >= period is constant ON by design (CLAUDE.md), never a problem.
    assert sc.parameter_problems([B("A", freq=10, pw=100)]) == []
    assert sc.parameter_problems([B("A", freq=10, pw=500)]) == []
    # The 1 MHz ceiling itself is usable.
    assert sc.parameter_problems([B("A", freq=1e6, pw=0.001)]) == []
    print("5e) frequency/pulse/duration ranges refused at compile: PASS")


def test_duration_rounding_parity():
    """The sketch and describe() share one rounding, so the per-frame trace
    can model exactly the block boundaries the board executes."""
    assert sc.dur_to_ms(1.001) == 1001, "truncated 1.001 s to 1000 ms"
    assert sc.dur_to_ms(1.003) == 1003 and sc.dur_to_ms(0.0015) == 2
    ino = sc.compile_ino([B("A", dur=1.001, freq=10, pw=10)], [], [53])
    line = [l for l in ino.splitlines() if l.startswith("  {53u")][0]
    assert "{53u, 100000UL, 10000UL, 1001UL}" in line, line
    step = sc.describe([B("A", dur=1.001, freq=10, pw=10)], [])[0]["steps"][0]
    assert step["duration_ms"] == 1001 and step["duration_s"] == 1.001
    # Across the whole ms grid up to 20 s no duration loses a millisecond.
    for k in range(1, 20001):
        assert sc.dur_to_ms(k / 1000) == k, k
    print("5f) duration rounding: sketch and describe() agree, none truncated: PASS")


def test_recording_only_sketch():
    """Stim must be opt-in per launch, not sticky flash state.

    A paradigm lives in the Arduino's flash, so it survives closing the GUI,
    power cycles and USB unplugs — while the canvas comes up empty and nothing
    can read the firmware back over serial. main_window therefore reflashes this
    sketch at startup whenever the stored hash says the board holds something
    else. These are the properties that makes that safe.
    """
    TRIG = [2, 4, 6, 8, 10, 12]
    blank = sc.recording_only_sketch([53], TRIG)

    # Carries the camera protocol and the safe-pin guard...
    assert "void setup()" in blank and "FRAME_START" in blank
    assert "allStimLow();" in blank
    assert "const uint8_t STIM_PINS[] = {53};" in blank   # declared to be held LOW
    # ...and the guard still runs before Serial.begin, or the pin floats while
    # setup() blocks on the handshake.
    setup = blank.split("void setup()")[1]
    assert setup.index("allStimLow();") < setup.index("Serial.begin")

    # Deterministic, or the startup check would reflash on every launch forever.
    assert sc.sketch_sha(blank) == sc.sketch_sha(
        sc.recording_only_sketch([53], TRIG))

    # And distinguishable from a real paradigm, or an Apply would not force the
    # reflash that clears it next launch.
    paradigm = sc.compile_ino([B("A", pin=53)], [], [53], TRIG)
    assert sc.sketch_sha(paradigm) != sc.sketch_sha(blank)
    print("5c) recording-only sketch: guard intact, deterministic, distinct: PASS")


def test_durations():
    # end_time_s = cumulative duration up to and including the flagged block.
    assert sc.end_time_s([B("A", 5), B("B", 3, end=True)], [E("A", "B")]) == 8.0
    # A parallel timer chain is how you bound a looping paradigm.
    assert sc.end_time_s(
        [B("A", 5, start=True), B("B", 5), B("C", 60, end=True)],
        [E("A", "B"), E("B", "A")]) == 60.0
    # Unflagged, and flagged-but-unreachable, both mean "no automatic stop".
    assert sc.end_time_s([B("A", 5), B("B", 3)], [E("A", "B")]) is None
    assert sc.end_time_s([B("A", 5, end=True), B("B", 5)],
                         [E("A", "B"), E("B", "A")]) is None

    # test_duration_s falls back to the longest terminating chain.
    assert sc.test_duration_s([B("A", 8), B("C", 20)], []) == 20.0
    # ...and is open-ended for a loop.
    assert sc.test_duration_s([B("A", 5, start=True), B("B", 5)],
                              [E("A", "B"), E("B", "A")]) is None
    print("6) end_time_s / test_duration_s across topologies: PASS")


def test_describe():
    chains = sc.describe(
        [B("A", 5, freq=10, pw=100, start=True), B("B", 5, freq=0, pw=0)],
        [E("A", "B"), E("B", "A")])
    assert len(chains) == 1 and chains[0]["loops"] is True
    assert chains[0]["loops_back_to_step"] == 0
    assert [s["mode"] for s in chains[0]["steps"]] == ["constant ON", "off (pin LOW)"]
    assert [s["mode"] for s in sc.describe([B("A", freq=10, pw=10)], [])[0]["steps"]] \
        == ["10% duty"]
    print("7) provenance description of chains: PASS")


def test_generated_sketch_is_wellformed():
    ino = sc.compile_ino(
        [B("A", 5, freq=10, pw=100, start=True), B("B", 5, freq=0, pw=0),
         B("C", 30, pin=44, freq=20, pw=5)],
        [E("A", "B"), E("B", "A")], [53])
    assert ino.count("{") == ino.count("}"), "unbalanced braces"
    assert "const int NUM_CHAINS = 2;" in ino
    assert "{CHAIN_0, CHAIN_0_LEN, 0}" in ino      # loops back to step 0
    assert "{CHAIN_1, CHAIN_1_LEN, -1}" in ino     # one-shot
    # The camera trigger protocol must survive untouched.
    for expected in ("void loop()", "camsHigh();", "FRAME_START += FRAME_PERIOD;",
                     "N_CAMS = readPins();", "updateStim();"):
        assert expected in ino, f"missing {expected!r}"
    print("8) generated sketch structure + camera protocol intact: PASS")


def test_ready_ack():
    """The host gates recording on this ack, so both config paths must emit it."""
    ino = sc.compile_ino([B("A", 5)], [], [53])
    assert 'Serial.print(F("RDY "));' in ino
    setup = ino.split("void setup()")[1].split("// ===== LOOP")[0]
    loop = ino.split("void loop()")[1]
    for name, body in (("setup", setup), ("loop", loop)):
        assert "announceReady();" in body, f"no ack from the {name} config path"
        # Must precede FRAME_START or the ~1 ms print skews the first frame.
        assert body.index("announceReady();") < body.index("FRAME_START = micros();"), \
            f"{name}: ack printed after the timing reference is taken"
    print("9) RDY ack emitted from both setup and loop config paths: PASS")


def test_sketch_identity():
    """The RDY line carries an 8-hex id of the sketch, so the host can learn
    which firmware the board runs rather than trust a per-machine record."""
    TRIG = [2, 4, 6, 8, 10, 12]
    blank = sc.recording_only_sketch([53], TRIG)
    paradigm = sc.compile_ino([B("A", pin=53)], [], [53], TRIG)
    bid, pid = sc.sketch_id(blank), sc.sketch_id(paradigm)
    assert bid and pid and len(bid) == 8 and int(bid, 16) >= 0
    assert bid != pid, "two different sketches share an identity"
    assert bid == sc.sketch_id(sc.recording_only_sketch([53], TRIG)), "id not deterministic"
    # The id is what announceReady prints, appended to the RDY line.
    assert f'const char SKETCH_ID[] = "{bid}";' in blank
    ready = blank.split("void announceReady()")[1].split("}")[0]
    assert "Serial.println(SKETCH_ID);" in ready
    assert ready.index("(long)FPS_OUT") < ready.index("SKETCH_ID"), "id must follow fps"
    assert "@SKETCH_ID@" not in blank, "placeholder leaked into the sketch"
    # A sketch without the line (older firmware) reads as no identity.
    assert sc.sketch_id("void setup() {}") is None
    print("9b) sketch identity: deterministic, distinct, printed in RDY: PASS")


# ── per-frame trace (gui_app/stim_trace.py) ──────────────────────────────────
# Same paradigm semantics, evaluated over time instead of compiled to C. If
# these two drift apart the trace silently mislabels which frames were stimulated.

def test_trace_locate():
    from gui_app.stim_trace import locate
    steps = [{"duration_s": 3.0}, {"duration_s": 3.0}]

    # non-looping: runs once, then the chain is done
    assert locate(steps, None, 0.0) == (0, 0.0)
    assert locate(steps, None, 2.999)[0] == 0
    assert locate(steps, None, 3.0) == (1, 0.0)
    assert locate(steps, None, 6.0) == (None, None)

    # looping back to 0: repeats forever
    assert locate(steps, 0, 6.0) == (0, 0.0)
    assert locate(steps, 0, 7.5) == (0, 1.5)
    assert locate(steps, 0, 9.0) == (1, 0.0)
    assert locate(steps, 0, 6000.0) == (0, 0.0)

    # lead-in then loop: step 0 runs once, then 1<->2 cycle
    three = [{"duration_s": 10.0}, {"duration_s": 2.0}, {"duration_s": 3.0}]
    assert locate(three, 1, 5.0) == (0, 5.0)      # still in the lead-in
    assert locate(three, 1, 10.0) == (1, 0.0)
    assert locate(three, 1, 15.0) == (1, 0.0)     # one cycle later, not back to 0
    assert locate(three, 1, 13.0) == (2, 1.0)
    print("10) trace step location incl. loop + lead-in: PASS")


def test_trace_ttl_matches_firmware():
    from gui_app.stim_trace import ttl_level
    off = {"freq_hz": 0.0, "pulse_width_ms": 10.0}
    assert ttl_level(off, 0.0) == 0 and ttl_level(off, 1.234) == 0
    const = {"freq_hz": 10.0, "pulse_width_ms": 100.0}      # pw == period
    assert ttl_level(const, 0.0) == 1 and ttl_level(const, 4.9) == 1
    train = {"freq_hz": 10.0, "pulse_width_ms": 10.0}       # 10 ms pulse per 100 ms
    assert ttl_level(train, 0.0) == 1, "pulse must lead the period, as the sketch does"
    assert ttl_level(train, 0.005) == 1
    assert ttl_level(train, 0.015) == 0
    assert ttl_level(train, 0.100) == 1                     # next period
    print("11) trace TTL level agrees with the sketch's waveform: PASS")


def test_trace_rows_use_blockids_not_frame_index():
    """Dropped frames must shift time, or every later frame is mislabelled."""
    import numpy as np
    from gui_app.stim_trace import build_rows
    paradigm = {"chains": [{
        "loops": True, "loops_back_to_step": 0,
        "steps": [
            {"pin": 53, "freq_hz": 10.0, "pulse_width_ms": 10.0,
             "duration_s": 3.0, "mode": "10% duty"},
            {"pin": 53, "freq_hz": 0.0, "pulse_width_ms": 0.0,
             "duration_s": 3.0, "mode": "off (pin LOW)"},
        ]}]}

    # contiguous: blockid 1..600 -> t 0..5.99, flipping at exactly 3 s
    fields, rows = build_rows(paradigm, np.arange(1, 601), 100.0)
    assert rows[0]["t_s"] == 0.0 and rows[0]["any_active"] == 1
    assert rows[299]["any_active"] == 1 and rows[300]["any_active"] == 0
    assert "pin53_ttl" in fields

    # drop 100 triggers mid-recording: the frame *after* the gap must jump 1 s
    b = np.concatenate([np.arange(1, 101), np.arange(201, 301)])
    _f, rows = build_rows(paradigm, b, 100.0)
    assert rows[99]["t_s"] == 0.99
    assert rows[100]["t_s"] == 2.00, "gap ignored — trace would drift by 1 s"
    assert rows[100]["frame"] == 100 and rows[100]["blockid"] == 201
    print("12) trace maps frames via block IDs, so drops shift time: PASS")


def test_trace_unwraps_16bit_blockids():
    """Recordings past ~11 min wrap at 65535; without unwrapping, time restarts."""
    import numpy as np
    from gui_app.stim_trace import build_rows
    paradigm = {"chains": [{
        "loops": False, "loops_back_to_step": None,
        "steps": [{"pin": 53, "freq_hz": 20.0, "pulse_width_ms": 20.0,
                   "duration_s": 1e6, "mode": "40% duty"}]}]}
    b = np.concatenate([np.arange(65530, 65536), np.arange(1, 7)])
    _f, rows = build_rows(paradigm, b, 100.0)
    times = [r["t_s"] for r in rows]
    assert times == sorted(times), f"time went backwards across the wrap: {times}"
    assert abs(times[-1] - times[0] - 0.11) < 1e-6
    print("13) trace unwraps 16-bit block-ID rollover: PASS")


# ── the sketch-swap invalidation rule (added 2026-09-04) ────────────────────
# A calibration always flashes the recording-only sketch. If the stim editor is
# not told, its _uploaded_ino still holds the paradigm, so Test finds canvas ==
# uploaded, skips the re-upload prompt, and drives a board with NUM_CHAINS == 0:
# the laser never fires and nothing says so. This pins the decision rule that
# main_window._ensure_sketch_for uses.

def _should_invalidate(want_ino, session_stim_ino):
    """Mirror of the guard in _ensure_sketch_for's success path."""
    return session_stim_ino is not None and want_ino != session_stim_ino


def test_sketch_swap_invalidates_stale_upload():
    TRIG = [2, 4, 6, 8, 10, 12]
    blank = sc.recording_only_sketch([53], TRIG)
    paradigm = sc.compile_ino([B("A", pin=53)], [], [53], TRIG)
    assert blank != paradigm, "test premise broken: the two sketches are equal"

    # A calibration after an Apply: blank goes on, the editor must be told.
    assert _should_invalidate(blank, paradigm), \
        "calibration flashed the blank sketch but left the editor thinking " \
        "its paradigm is still on the board"

    # Recording the same paradigm again: no flash, nothing to invalidate.
    assert not _should_invalidate(paradigm, paradigm), \
        "invalidated an upload that is genuinely still on the board"

    # Never applied anything: there is no stale state to clear, so staying
    # quiet matters — otherwise a plain calibration nags about re-applying a
    # paradigm the user never created.
    assert not _should_invalidate(blank, None), \
        "nagged about a paradigm that was never applied"

    print("14) a sketch swap invalidates a stale upload record: PASS")


def main():
    test_start_resolution()
    test_chain_extraction_terminates()
    test_structural_problems()
    test_waveform_encoding()
    test_safe_pins()
    test_pin_conflicts()
    test_forbidden_pins()
    test_pin_range()
    test_parameter_ranges()
    test_duration_rounding_parity()
    test_recording_only_sketch()
    test_durations()
    test_describe()
    test_generated_sketch_is_wellformed()
    test_ready_ack()
    test_sketch_identity()
    test_sketch_swap_invalidates_stale_upload()
    if _HAS_NUMPY:
        test_trace_locate()
        test_trace_ttl_matches_firmware()
        test_trace_rows_use_blockids_not_frame_index()
        test_trace_unwraps_16bit_blockids()
    else:
        print("10-13) per-frame trace tests SKIPPED — no numpy in this "
              "interpreter; rerun with `uv run python test_stim_compiler.py`")
    print("\nALL STIM COMPILER TESTS PASS")


if __name__ == "__main__":
    main()
