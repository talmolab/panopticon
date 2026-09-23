"""Map a recording's frames onto the stimulus paradigm that ran alongside them.

One Arduino drives both the camera triggers and the stimulus, and the sketch sets
``FRAME_START`` and calls ``initStim()`` microseconds apart — so stim t=0 *is*
trigger t=0, with no host clock involved. That makes the mapping exact:

    t = (unwrapped_blockid - 1) / fps

Exact also requires modelling the block lengths the board executes, not the
ones the operator typed: step lengths come from ``duration_ms`` (see
``_step_seconds``), which is the integer the sketch counts in.

Block IDs, not frame indices: cameras drop frames independently over GigE, so
frame *i* is not trigger *i*. ``blockids.npy`` records each frame's trigger
ordinal, and 16-bit IDs wrap every 65535 triggers (~11 min at 100 fps), which
``alignment._unwrap_blockids`` undoes.

IMPORTANT — this is derived, not observed. It says what the paradigm *should*
have delivered given the firmware that was uploaded. It cannot know whether the
laser was keyed on, the interlock was in, or the beam was blocked. For a real
witness, put the laser's sync LED in a camera's field of view (and note that at
100 fps with a ~2 ms exposure you resolve block envelopes, not individual
pulses).
"""
import csv
import json
from pathlib import Path

import numpy as np

from gui_app.alignment import _unwrap_blockids, camera_dirs

TRACE_NAME = "stim_trace.csv"
PARADIGM_NAME = "stim_paradigm.json"


def _step_seconds(step: dict) -> float:
    """One step's length, in the rounding the board itself uses.

    RULE: read ``duration_ms`` where the provenance carries it and fall back to
    ``duration_s`` only for older records. REASON: the sketch counts whole
    milliseconds (``blk_start_ms += dur_ms``), so a duration off the
    millisecond grid puts a block boundary up to half a millisecond away from
    the board's — and a looping chain accumulates that difference every cycle,
    so the trace labels frames with the wrong block from the first boundary
    onward while claiming the mapping is exact.
    """
    ms = step.get("duration_ms")
    if ms is None:
        return float(step["duration_s"])
    return float(ms) / 1000.0


def _chain_totals(steps: list[dict], loop_to):
    total = sum(_step_seconds(s) for s in steps)
    head = sum(_step_seconds(s) for s in steps[:loop_to]) if loop_to is not None else 0.0
    return total, head


def locate(steps: list[dict], loop_to, t: float):
    """Which step of a chain is running at time t, and how far into it.

    Returns (step_index, t_into_step), or (None, None) once a non-looping chain
    has finished. Mirrors the sketch: a looping chain replays steps[loop_to:]
    forever, while the steps before loop_to run once as a lead-in.
    """
    if not steps or t < 0:
        return None, None
    total, head = _chain_totals(steps, loop_to)
    if loop_to is None:
        if t >= total:
            return None, None
        tt = t
    else:
        cycle = total - head
        if cycle <= 0:
            return None, None
        tt = t if t < head else head + (t - head) % cycle
    acc = 0.0
    for i, s in enumerate(steps):
        d = _step_seconds(s)
        if tt < acc + d:
            return i, tt - acc
        acc += d
    return None, None


def ttl_level(step: dict, t_into_step: float) -> int:
    """Modelled pin level at an instant inside a step.

    Matches the firmware: 0 Hz or 0 pulse width holds LOW, a pulse width at or
    above the period is a constant HIGH, and otherwise the pulse *leads* each
    period. Sampled at the trigger instant — the exposure spans ~2 ms after it,
    so treat this as indicative for a pulse train, exact for on/off blocks.
    """
    freq = float(step["freq_hz"])
    pw_ms = float(step["pulse_width_ms"])
    if freq <= 0 or pw_ms <= 0:
        return 0
    period_ms = 1000.0 / freq
    if pw_ms >= period_ms:
        return 1
    return 1 if (t_into_step * 1000.0) % period_ms < pw_ms else 0


def step_active(step: dict) -> bool:
    """Whether a step drives its pin at all.

    Decided from the numbers the firmware uses (a positive frequency AND a
    positive pulse width), never from the human-readable ``mode`` label:
    the label is display text that can be reworded, and a trace that keyed
    on it would flip every off-period frame to active without a test noticing.
    """
    return float(step["freq_hz"]) > 0 and float(step["pulse_width_ms"]) > 0


def resolve_chains(paradigm: dict) -> list[dict]:
    """The chains a paradigm describes, rebuilt from its node graph when present.

    ``stim_paradigm.json`` stores both the graph (``blocks``/``edges``) and
    the pre-resolved ``chains`` the editor derived from it at record time.
    The graph is the editable half, so regenerating a trace after editing the
    file must compile the graph again; the stored ``chains`` are used only for
    a file that has no graph.
    """
    blocks, edges = paradigm.get("blocks"), paradigm.get("edges")
    if isinstance(blocks, list) and isinstance(edges, list):
        from gui_app.stim_compiler import describe
        return describe(blocks, edges)
    return list(paradigm.get("chains", []))


def build_rows(paradigm: dict, blockids: np.ndarray, fps: float):
    """Return (fieldnames, rows) — one row per recorded frame."""
    chains = resolve_chains(paradigm)
    pins = sorted({int(s["pin"]) for c in chains for s in c["steps"]})

    fields = ["frame", "blockid", "t_s", "any_active"]
    for i, _c in enumerate(chains):
        fields += [f"chain{i}_step", f"chain{i}_active",
                   f"chain{i}_freq_hz", f"chain{i}_pw_ms"]
    fields += [f"pin{p}_ttl" for p in pins]

    b = _unwrap_blockids(np.asarray(blockids))
    rows = []
    for frame, bid in enumerate(b):
        t = (int(bid) - 1) / fps          # blockid is 1-based from trigger start
        row = {"frame": frame, "blockid": int(bid), "t_s": round(t, 6)}
        levels = {p: 0 for p in pins}
        any_active = False
        for i, ch in enumerate(chains):
            idx, into = locate(ch["steps"], ch.get("loops_back_to_step"), t)
            if idx is None:
                row |= {f"chain{i}_step": "", f"chain{i}_active": 0,
                        f"chain{i}_freq_hz": "", f"chain{i}_pw_ms": ""}
                continue
            step = ch["steps"][idx]
            active = step_active(step)
            any_active |= active
            row |= {f"chain{i}_step": idx,
                    f"chain{i}_active": int(active),
                    f"chain{i}_freq_hz": step["freq_hz"],
                    f"chain{i}_pw_ms": step["pulse_width_ms"]}
            pin = int(step["pin"])
            levels[pin] = max(levels[pin], ttl_level(step, into))
        row["any_active"] = int(any_active)
        row |= {f"pin{p}_ttl": levels[p] for p in pins}
        rows.append(row)
    return fields, rows


def _pick_blockids(recording_dir: Path):
    """Any camera's block IDs will do — kick-out makes them identical — but check
    that assumption rather than trusting it, and say so if it fails."""
    files = [d / "blockids.npy" for d in camera_dirs(recording_dir)
             if (d / "blockids.npy").exists()]
    if not files:
        return None, "no blockids.npy (recording never stopped cleanly?)"
    arrays = {f.parent.name: np.load(f) for f in files}
    lengths = {n: len(a) for n, a in arrays.items()}
    first = next(iter(arrays.values()))
    if len(set(lengths.values())) > 1 or not all(
            np.array_equal(a, first) for a in arrays.values()):
        return first, (f"cameras disagree on block IDs {lengths} — videos are not "
                       f"trigger-aligned; trace uses {files[0].parent.name}")
    return first, None


def write_trace(recording_dir: Path, fps: float) -> tuple[Path | None, str]:
    """Write stim_trace.csv beside the videos. Returns (path, message)."""
    recording_dir = Path(recording_dir)
    paradigm_path = recording_dir / PARADIGM_NAME
    if not paradigm_path.exists():
        return None, "no stimulus paradigm recorded for this session"
    paradigm = json.loads(paradigm_path.read_text())

    blockids, warning = _pick_blockids(recording_dir)
    if blockids is None:
        return None, warning
    if len(blockids) and int(blockids[0]) != 1:
        lead = int(blockids[0]) - 1
        warning = (f"{(warning + '; ') if warning else ''}first block ID is "
                   f"{int(blockids[0])}, so {lead} leading trigger(s) were dropped "
                   f"— times account for this")

    fields, rows = build_rows(paradigm, blockids, fps)
    out = recording_dir / TRACE_NAME
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    active = sum(r["any_active"] for r in rows)
    msg = (f"{len(rows)} frames, {active} with stimulation active "
           f"({100 * active / max(len(rows), 1):.1f}%)")
    return out, (f"{msg} [{warning}]" if warning else msg)
