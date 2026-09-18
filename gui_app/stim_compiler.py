"""Generate and upload Arduino Mega 2560 combined camera-trigger + stim sketch."""
import re
import subprocess
import tempfile
import shutil
from pathlib import Path

FQBN = "arduino:avr:mega"

#: Where arduino-cli might live, in priority order. Overridable with the
#: PANOPTICON_ARDUINO_CLI environment variable. Kept as a search rather than a
#: constant because the old hardcoded absolute path silently broke Apply on any
#: machine with a different Arduino install — and the failure surfaced as a
#: generic upload error rather than "the tool is missing".
_ARDUINO_CLI_CANDIDATES = (
    r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
    r"C:\Program Files (x86)\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
    "/usr/local/bin/arduino-cli",
    "/usr/bin/arduino-cli",
    "/opt/homebrew/bin/arduino-cli",
)


def find_arduino_cli() -> Path | None:
    """Locate arduino-cli, or None. Checked at Apply time, not import time."""
    import os
    env = os.environ.get("PANOPTICON_ARDUINO_CLI")
    if env and Path(env).exists():
        return Path(env)
    on_path = shutil.which("arduino-cli")
    if on_path:
        return Path(on_path)
    for c in _ARDUINO_CLI_CANDIDATES:
        if Path(c).exists():
            return Path(c)
    return None


def arduino_cli_help() -> str:
    """A message that says what to actually DO, for when it is not found."""
    return (
        "arduino-cli was not found, so the stim firmware cannot be compiled or "
        "uploaded.\n\n"
        "Panopticon looked in, in order:\n"
        "  1. $PANOPTICON_ARDUINO_CLI\n"
        "  2. arduino-cli on PATH\n"
        + "".join(f"  {i}. {c}\n" for i, c in enumerate(_ARDUINO_CLI_CANDIDATES, 3))
        + "\nFix it either way:\n"
        "  - install the Arduino IDE (which bundles arduino-cli), or\n"
        "  - install arduino-cli standalone and put it on PATH, or\n"
        "  - set PANOPTICON_ARDUINO_CLI to its full path.\n\n"
        "Camera acquisition does NOT need this — only the Stimulation editor's "
        "Apply/Test, which compile and flash the trigger board.")


#: Back-compat for existing callers; None if not installed.
ARDUINO_CLI = find_arduino_cli()

# Fallback for callers that don't pass the rig's pins. The real list comes from
# the profile's `stim_safe_pins` — see RigProfile. These are forced LOW the
# instant the sketch boots, before the serial handshake, so a powered laser
# driver never sits on a floating input pin.
DEFAULT_SAFE_LOW_PINS = (53,)


def resolve_starts(blocks: list[dict],
                   edges: list[dict]) -> tuple[set[str], set[str]]:
    """Work out where each connected group of blocks begins.

    Returns ``(start_ids, needs_start_ids)``.

    Within one weakly-connected group: an explicit ``start`` flag wins; failing
    that every block with no incoming arrow is a start (parallel chains that
    merge). A pure loop has neither, so it lands in ``needs_start_ids`` — the
    user has to tick "Starting" on one of its blocks or it will not run.
    """
    ids = [b["id"] for b in blocks]
    id_set = set(ids)
    flagged = {b["id"]: bool(b.get("start")) for b in blocks}
    incoming = {i: 0 for i in ids}
    adj: dict[str, set[str]] = {i: set() for i in ids}
    for e in edges:
        s, d = e["src"], e["dst"]
        if s not in id_set or d not in id_set:
            continue
        incoming[d] += 1
        adj[s].add(d)
        adj[d].add(s)

    starts: set[str] = set()
    needs: set[str] = set()
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            continue
        comp, stack = [], [i]
        seen.add(i)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for n in adj[cur]:
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        comp.sort(key=ids.index)  # stable, independent of traversal order
        explicit = [c for c in comp if flagged[c]]
        if explicit:
            starts.add(explicit[0])
        else:
            sources = [c for c in comp if incoming[c] == 0]
            if sources:
                starts.update(sources)
            else:
                needs.update(comp)
    return starts, needs


def _extract_chains(blocks: list[dict],
                    edges: list[dict]) -> list[tuple[list[dict], int]]:
    """Follow edges from each start block.

    Returns ``[(ordered_blocks, loop_to), ...]`` where ``loop_to`` is the index
    the chain jumps back to when it reaches the end, or -1 to stop. Walking is
    cycle-safe: revisiting a block closes the loop instead of running forever.
    """
    by_id = {b["id"]: b for b in blocks}
    succ: dict[str, str | None] = {b["id"]: None for b in blocks}
    for e in edges:
        if e["src"] in succ and e["dst"] in by_id:
            succ[e["src"]] = e["dst"]

    starts, _ = resolve_starts(blocks, edges)
    chains: list[tuple[list[dict], int]] = []
    for b in blocks:  # iterate blocks (not the set) to keep output order stable
        if b["id"] not in starts:
            continue
        order: list[dict] = []
        index_of: dict[str, int] = {}
        cur, loop_to = b["id"], -1
        while cur is not None:
            if cur in index_of:
                loop_to = index_of[cur]
                break
            index_of[cur] = len(order)
            order.append(by_id[cur])
            cur = succ[cur]
        chains.append((order, loop_to))
    return chains


def end_time_s(blocks: list[dict], edges: list[dict]) -> float | None:
    """Seconds from paradigm start until the block flagged ``end`` finishes.

    Returns None when nothing is flagged or the flagged block sits outside every
    chain. A looping chain is walked once, so the answer is the block's *first*
    completion — the loop itself keeps running until the recording stops it.
    """
    best = None
    for chain, _loop_to in _extract_chains(blocks, edges):
        t = 0.0
        for blk in chain:
            t += float(blk["dur"])
            if blk.get("end"):
                if best is None or t < best:
                    best = t
                break
    return best


def pin_conflicts(blocks: list[dict], edges: list[dict]) -> list[int]:
    """Pins driven by more than one chain.

    Chains run concurrently, so two of them on the same pin fight over the
    output — one drives it HIGH while the other drives it LOW and the waveform
    is neither. Reusing a pin *within* a chain is fine: those blocks run in
    sequence.
    """
    owners: dict[int, set[int]] = {}
    for i, (chain, _loop_to) in enumerate(_extract_chains(blocks, edges)):
        for pin in {int(b["pin"]) for b in chain}:
            owners.setdefault(pin, set()).add(i)
    return sorted(p for p, chain_ids in owners.items() if len(chain_ids) > 1)


def describe(blocks: list[dict], edges: list[dict]) -> list[dict]:
    """Human-readable chain summary for the provenance record saved with a
    recording — readable without replaying the node graph."""
    out = []
    for chain, loop_to in _extract_chains(blocks, edges):
        steps = []
        for b in chain:
            freq, pw = float(b["freq"]), float(b["pw"])
            if freq <= 0 or pw <= 0:
                mode = "off (pin LOW)"
            elif pw * freq >= 1000:
                mode = "constant ON"
            else:
                mode = f"{pw * freq / 10:g}% duty"
            steps.append({"pin": int(b["pin"]), "freq_hz": freq,
                          "pulse_width_ms": pw, "duration_s": float(b["dur"]),
                          "mode": mode})
        out.append({
            "loops": loop_to >= 0,
            "loops_back_to_step": loop_to if loop_to >= 0 else None,
            "steps": steps,
        })
    return out


def test_duration_s(blocks: list[dict], edges: list[dict]) -> float | None:
    """How long a bench test of this paradigm runs.

    The end block if one is flagged, otherwise the longest terminating chain.
    None means open-ended — a looping chain that only stops when told to.
    """
    end = end_time_s(blocks, edges)
    if end:
        return end
    chains = _extract_chains(blocks, edges)
    if not chains or any(loop_to >= 0 for _c, loop_to in chains):
        return None
    return max(sum(float(b["dur"]) for b in c) for c, _ in chains)


#: Pins a stim chain must never drive, regardless of rig. 0/1 are the Mega's
#: UART RX0/TX0 — the link the GUI talks to the board over.
RESERVED_SERIAL_PINS = (0, 1)


def forbidden_pin_uses(blocks: list[dict],
                       trigger_pins=()) -> list[tuple[int, str]]:
    """Stim blocks assigned to pins that must never carry a stim waveform.

    Two classes, both of which fail SILENTLY — nothing downstream can detect
    either, which is why this is enforced at compile time rather than reviewed:

    - **Camera trigger pins.** The rig's whole alignment model rests on GigE
      BlockID N denoting the same instant on every camera, which holds because
      one board drives every trigger line from one timer. A stim chain on a
      trigger pin makes `updateStim()` inject extra rising edges into ONE
      camera, so that camera's BlockIDs advance faster and BlockID identity
      quietly stops meaning simultaneity. `frame_sync`, `alignment.py` and
      `stim_trace` all take that identity as given.
    - **RX0/TX0 (pins 0 and 1).** Driving them garbles the serial protocol and
      the RDY ack that CLAUDE.md calls "the whole safety property". Pin 0 is
      especially easy to hit because a blank pin field in the editor coerces to
      0.

    No legitimate paradigm drives either. Returns [(pin, reason), ...].
    """
    trig = {int(p) for p in trigger_pins}
    out: list[tuple[int, str]] = []
    for pin in sorted({int(b["pin"]) for b in blocks}):
        if pin in trig:
            out.append((pin, "camera trigger line — extra edges on one camera "
                             "would break cross-camera block-ID alignment"))
        elif pin in RESERVED_SERIAL_PINS:
            out.append((pin, "UART RX0/TX0 — would garble the trigger-board "
                             "serial link and the RDY ack"))
    return out


def compile_ino(blocks: list[dict], edges: list[dict],
                safe_pins=DEFAULT_SAFE_LOW_PINS, trigger_pins=()) -> str:
    """Return the .ino source for the combined camera-trigger + stim sketch.

    safe_pins come from the rig profile's `stim_safe_pins` and are held LOW from
    boot regardless of what the workflow uses.

    trigger_pins come from the profile too, and are refused rather than
    compiled: see forbidden_pin_uses(). Raises ValueError so a graph that would
    corrupt cross-camera alignment can never reach the board. The RX0/TX0 check
    is unconditional; the trigger-pin check needs the profile, so callers that
    have one MUST pass it.
    """
    bad = forbidden_pin_uses(blocks, trigger_pins)
    if bad:
        detail = "; ".join(f"pin {p}: {why}" for p, why in bad)
        raise ValueError(f"stim block on a forbidden pin — {detail}")

    chains = _extract_chains(blocks, edges)
    n = len(chains)

    chain_defs_parts: list[str] = []
    chain_refs: list[str] = []
    all_stim_pins: set[int] = {int(p) for p in safe_pins}

    for i, (chain, loop_to) in enumerate(chains):
        entries = []
        for blk in chain:
            pin = int(blk["pin"])
            freq = float(blk["freq"])
            pw = float(blk["pw"])
            dur_s = float(blk["dur"])
            # Period and pulse width are resolved to integer microseconds here so
            # the sketch does no floating-point math -- updateStim() runs inside
            # the camera trigger's busy-wait, where an AVR float divide (~30 us)
            # would blunt the ~0.35 us edge precision of the original firmware.
            period_us = int(round(1e6 / freq)) if freq > 0 else 0
            pw_us = int(round(pw * 1000.0))
            dur_ms = int(dur_s * 1000)
            entries.append(
                f"  {{{pin}u, {period_us}UL, {pw_us}UL, {dur_ms}UL}},"
                f"   // {freq:g} Hz, {pw:g} ms pulse, {dur_s:g} s"
            )
            all_stim_pins.add(pin)
        chain_defs_parts.append(
            f"const int CHAIN_{i}_LEN = {len(chain)};\n"
            f"StimBlock CHAIN_{i}[] = {{\n" + "\n".join(entries) + "\n};"
        )
        chain_refs.append(f"  {{CHAIN_{i}, CHAIN_{i}_LEN, {loop_to}}}")

    if n:
        chain_defs = "\n\n".join(chain_defs_parts)
        chain_table = (
            "StimChainRef STIM_CHAINS[] = {\n" + ",\n".join(chain_refs) + "\n};"
        )
    else:
        chain_defs = "// No stimulus chains defined."
        chain_table = (
            "StimBlock _dummy_blk = {0, 0UL, 0UL, 0UL};\n"
            "StimChainRef STIM_CHAINS[1] = {{&_dummy_blk, 0, -1}};"
        )

    pins_sorted = sorted(all_stim_pins)
    # A zero-length array is invalid C++, so emit a placeholder the loop skips.
    pin_list = ", ".join(str(p) for p in pins_sorted) if pins_sorted else "0"
    n_pins = len(pins_sorted)
    state_arr = max(n, 1)
    sketch_id = _SKETCH_ID_TOKEN

    body = f"""\
// Panopticon: camera trigger + stimulus paradigm
// Auto-generated by Panopticon Stimulation Editor -- do not edit by hand.

// ===== CAMERA TRIGGER =====
const uint32_t BAUDRATE = 115200;
int CAM_PINS[100];
int N_CAMS = 0;
float FPS_OUT = 0;
unsigned long FRAME_PERIOD = 0;
unsigned long FRAME_START = 0;

// ===== STIM STRUCTURES =====
struct StimBlock {{
  uint8_t pin;
  uint32_t period_us;   // 0 = hold the pin LOW for this block
  uint32_t pw_us;       // >= period_us = hold the pin HIGH for the whole block
  uint32_t dur_ms;
}};
struct StimChainRef {{
  StimBlock* blocks;
  int len;
  int loop_to;   // index to jump back to at the end, or -1 to stop
}};

// Every pin the paradigm can drive, plus the always-safe pins. Driven LOW as
// the very first thing in setup() so nothing fires before the record command.
const uint8_t STIM_PINS[] = {{{pin_list}}};
const int N_STIM_PINS = {n_pins};

{chain_defs}

{chain_table}
const int NUM_CHAINS = {n};

struct ChainState {{
  int idx;
  uint32_t blk_start_ms;
  uint32_t last_toggle_us;
  bool pin_high;
  bool fresh;      // just entered this block -- fire the first pulse immediately
  bool done;
}};
ChainState CS[{state_arr}];
bool STIM_ACTIVE = false;

// ===== CAMERA HELPERS =====
int readPins() {{
  while (!Serial.available()) {{}}
  int np = (int)(unsigned int)Serial.parseFloat();
  for (int i = 0; i < np; i++) {{
    while (!Serial.available()) {{}}
    int p = (int)(unsigned int)Serial.parseFloat();
    CAM_PINS[i] = p;
    pinMode(p, OUTPUT);
    digitalWrite(p, LOW);
  }}
  return np;
}}

float readFPS() {{
  while (!Serial.available()) {{}}
  float f = Serial.parseFloat();
  return (f < 0) ? 0 : f;
}}

void camsLow() {{
  noInterrupts();
  for (int i = 0; i < N_CAMS; i++) digitalWrite(CAM_PINS[i], LOW);
  interrupts();
}}

void camsHigh() {{
  noInterrupts();
  for (int i = 0; i < N_CAMS; i++) digitalWrite(CAM_PINS[i], HIGH);
  interrupts();
}}

// Handshake ack: `RDY <n_cams> <fps> <sketch id>`. The host blocks on this
// before letting the cameras roll, because a mis-parsed config otherwise looks
// identical to a good start and yields a recording with no triggers. Emitted
// from BOTH setup() and the loop() reconfigure branch, so it confirms either
// path. The id names the sketch build, so the host can tell which firmware the
// board actually runs instead of trusting a record of what was last uploaded.
const char SKETCH_ID[] = "{sketch_id}";
void announceReady() {{
  Serial.print(F("RDY "));
  Serial.print(N_CAMS);
  Serial.print(' ');
  Serial.print((long)FPS_OUT);
  Serial.print(' ');
  Serial.println(SKETCH_ID);
}}

// ===== STIM HELPERS =====
// pinMode first, then LOW: writing LOW to a pin still configured as INPUT only
// disables the pullup and leaves it floating -- which is what turns the laser on.
void allStimLow() {{
  for (int i = 0; i < N_STIM_PINS; i++) {{
    pinMode(STIM_PINS[i], OUTPUT);
    digitalWrite(STIM_PINS[i], LOW);
  }}
}}

void initStim() {{
  allStimLow();
  uint32_t nowMs = (uint32_t)millis();
  uint32_t nowUs = (uint32_t)micros();
  for (int c = 0; c < NUM_CHAINS; c++) {{
    CS[c].idx = 0;
    CS[c].blk_start_ms = nowMs;
    CS[c].last_toggle_us = nowUs;
    CS[c].pin_high = false;
    CS[c].fresh = true;
    CS[c].done = false;
  }}
  STIM_ACTIVE = true;
}}

void updateStim() {{
  if (!STIM_ACTIVE || NUM_CHAINS == 0) return;
  uint32_t nowMs = (uint32_t)millis();
  uint32_t nowUs = (uint32_t)micros();
  for (int c = 0; c < NUM_CHAINS; c++) {{
    ChainState* cs = &CS[c];
    if (cs->done) continue;
    // Advance past every block whose duration has elapsed. The guard bounds a
    // zero-duration loop, which would otherwise spin here forever.
    int guard = 0;
    while (!cs->done && guard++ < 64) {{
      StimBlock* blk = &STIM_CHAINS[c].blocks[cs->idx];
      if (nowMs - cs->blk_start_ms < blk->dur_ms) break;
      digitalWrite(blk->pin, LOW);
      cs->pin_high = false;
      cs->blk_start_ms += blk->dur_ms;   // drift-free: no rounding per block
      cs->idx++;
      if (cs->idx >= STIM_CHAINS[c].len) {{
        if (STIM_CHAINS[c].loop_to >= 0) {{
          cs->idx = STIM_CHAINS[c].loop_to;
        }} else {{
          cs->done = true;
          break;
        }}
      }}
      cs->last_toggle_us = nowUs;
      cs->fresh = true;
    }}
    if (cs->done) continue;

    StimBlock* blk = &STIM_CHAINS[c].blocks[cs->idx];

    // 0 Hz or 0 pulse width: this block is an off-period.
    if (blk->period_us == 0 || blk->pw_us == 0) {{
      cs->fresh = false;
      continue;
    }}
    // Pulse >= period is 100% duty -- hold the pin HIGH for the whole block
    // rather than treating it as an unrepresentable waveform.
    if (blk->pw_us >= blk->period_us) {{
      if (!cs->pin_high) {{
        digitalWrite(blk->pin, HIGH);
        cs->pin_high = true;
      }}
      cs->fresh = false;
      continue;
    }}
    // The pulse leads each period, so a block starts stimulating immediately.
    if (cs->fresh) {{
      digitalWrite(blk->pin, HIGH);
      cs->pin_high = true;
      cs->last_toggle_us = nowUs;
      cs->fresh = false;
      continue;
    }}
    uint32_t elapsed = nowUs - cs->last_toggle_us;
    if (cs->pin_high && elapsed >= blk->pw_us) {{
      digitalWrite(blk->pin, LOW);
      cs->pin_high = false;
      cs->last_toggle_us = nowUs;
    }} else if (!cs->pin_high && elapsed >= (blk->period_us - blk->pw_us)) {{
      digitalWrite(blk->pin, HIGH);
      cs->pin_high = true;
      cs->last_toggle_us = nowUs;
    }}
  }}
}}

// ===== SETUP =====
void setup() {{
  allStimLow();          // before Serial: setup() blocks on the handshake below
  Serial.begin(BAUDRATE);
  delay(500);
  N_CAMS = readPins();
  FPS_OUT = readFPS();
  FRAME_PERIOD = (FPS_OUT > 0) ? (unsigned long)(1e6f / FPS_OUT) : 0xFFFFFFFFUL;
  delay(500);
  while (Serial.available()) Serial.parseFloat();
  announceReady();       // before FRAME_START so the ~1 ms print can't skew it
  FRAME_START = micros();
  if (NUM_CHAINS > 0 && FPS_OUT > 0) initStim();
}}

// ===== LOOP =====
void loop() {{
  if (Serial.available()) {{
    camsLow();
    allStimLow();
    STIM_ACTIVE = false;
    N_CAMS = readPins();
    FPS_OUT = readFPS();
    FRAME_PERIOD = (FPS_OUT > 0) ? (unsigned long)(1e6f / FPS_OUT) : 0xFFFFFFFFUL;
    delay(500);
    while (Serial.available()) Serial.parseFloat();
    announceReady();
    FRAME_START = micros();
    if (NUM_CHAINS > 0 && FPS_OUT > 0) initStim();
  }}
  if (FPS_OUT > 0) {{
    camsLow();
    while (micros() - FRAME_START < FRAME_PERIOD / 2) {{ updateStim(); }}
    camsHigh();
    while (micros() - FRAME_START < FRAME_PERIOD) {{ updateStim(); }}
    FRAME_START += FRAME_PERIOD;
  }}
}}
"""
    # The id is a digest of the sketch with the id itself blanked, so it names
    # the build deterministically and two paradigms never share one.
    return body.replace(_SKETCH_ID_TOKEN, _digest_id(body))


#: Placeholder the sketch id replaces; never appears in an emitted sketch.
_SKETCH_ID_TOKEN = "@SKETCH_ID@"
_SKETCH_ID_RE = re.compile(r'const char SKETCH_ID\[\] = "([0-9a-f]{8})";')


def _digest_id(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def sketch_id(ino_content: str) -> str | None:
    """The 8-hex identity a sketch prints in its RDY ack, or None for a sketch
    without one (firmware predating the identity line).

    This is what TeensyController.board_id is compared against: equal ids mean
    the board runs exactly this source.
    """
    m = _SKETCH_ID_RE.search(ino_content)
    return m.group(1) if m else None


def recording_only_sketch(safe_pins=DEFAULT_SAFE_LOW_PINS, trigger_pins=()) -> str:
    """The sketch with NO stimulation: camera triggers plus the safe-pin guard.

    This is the state the board should be in unless a paradigm was deliberately
    Applied. Flashing it is the only way to be *sure* the board carries no stim:
    the paradigm lives in flash memory, so it survives closing the GUI, power
    cycles and USB unplugs, and there is no way to read it back over serial.
    """
    return compile_ino([], [], safe_pins, trigger_pins)


def sketch_sha(ino_content: str) -> str:
    """Stable identity for a sketch, so we can tell what the board last took."""
    import hashlib
    return hashlib.sha256(ino_content.encode("utf-8")).hexdigest()


def upload(ino_content: str, port: str) -> tuple[bool, str]:
    """Compile and upload the .ino to the Arduino. Returns (success, message)."""
    # Resolve at call time, not import time: the tool may be installed while the
    # GUI is open, and a missing tool should read as "install this" rather than
    # as a generic upload failure (the old hardcoded path produced the latter).
    cli = find_arduino_cli()
    if cli is None:
        return False, arduino_cli_help()

    tmp = Path(tempfile.mkdtemp())
    sketch_dir = tmp / "panopticon_stim"
    sketch_dir.mkdir()
    (sketch_dir / "panopticon_stim.ino").write_text(ino_content, encoding="utf-8")
    try:
        r = subprocess.run(
            [str(cli), "compile", "--fqbn", FQBN, str(sketch_dir)],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            return False, (
                f"Compile failed (arduino-cli exit {r.returncode}).\n\n"
                f"This is a problem with the generated sketch or the toolchain, "
                f"not with the board — nothing was flashed, so the board still "
                f"runs whatever it ran before.\n\n"
                f"If the error mentions a missing core, install it:\n"
                f"    arduino-cli core install arduino:avr\n\n"
                f"{r.stderr}\n{r.stdout}")
        r = subprocess.run(
            [str(cli), "upload", "--fqbn", FQBN, "--port", port, str(sketch_dir)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0:
            return False, (
                f"Upload failed on {port} (arduino-cli exit {r.returncode}).\n\n"
                f"The sketch compiled, so this is the link to the board. Common "
                f"causes: the port is held by something else (Arduino Serial "
                f"Monitor, another Panopticon instance), the wrong port is set "
                f"in the profile, or the board is not an {FQBN}.\n\n"
                f"WARNING: an upload that failed part-way leaves the board's "
                f"firmware in an UNKNOWN state, which means the stim/laser pin "
                f"state is also unknown. Power-cycle the board before relying "
                f"on it.\n\n"
                f"{r.stderr}\n{r.stdout}")
        return True, "Upload successful — Arduino will restart and wait for record command."
    except subprocess.TimeoutExpired as e:
        return False, (
            f"Timed out after {e.timeout:.0f}s during compile/upload.\n\n"
            f"If it timed out on UPLOAD the board may have been partially "
            f"flashed and its firmware — including the laser-pin boot guard — "
            f"is in an unknown state. Power-cycle it.")
    except Exception as e:
        return False, (f"{type(e).__name__}: {e}\n\n"
                       f"arduino-cli used: {cli}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
