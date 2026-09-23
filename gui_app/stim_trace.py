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

Each row is one trigger. ``frame`` is the frame number in the reference
camera's video (the lowest-numbered camera that takes part in the alignment),
and a ``frame_<cam>`` column per camera gives that camera's own frame number,
blank where it has no frame for the trigger. When the cameras agree, as
kick-out and a completed alignment make them, every ``frame_<cam>`` equals
``frame``. When they disagree the rows cover every trigger any of them
recorded, the trace is still right for each camera through its own column,
and ``TraceResult.disagreement`` says so. A camera left out of the alignment
(retired, or excluded by the caller) gets its column for those rows only.
Anything that rewrites ``blockids.npy`` must rewrite this file too
(``2_align.py --replace`` does, and ``trace_mismatch`` finds a trace an
interrupted rewrite left stale).

IMPORTANT — this is derived, not observed. It says what the paradigm *should*
have delivered given the firmware that was uploaded. It cannot know whether the
laser was keyed on, the interlock was in, or the beam was blocked. For a real
witness, put the laser's sync LED in a camera's field of view (and note that at
100 fps with a ~2 ms exposure you resolve block envelopes, not individual
pulses).
"""
import csv
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from gui_app import recording_meta
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


def build_rows(paradigm: dict, blockids: np.ndarray, fps: float,
               frames=None, camera_frames: dict | None = None):
    """Return (fieldnames, rows): one row per block ID in ``blockids``.

    ``frames`` gives each row's ``frame`` value, -1 for a trigger the
    reference camera did not record (written blank); by default row k is
    frame k. ``camera_frames`` maps camera name -> per-row frame numbers in
    that camera's video (-1 where it has none), written as ``frame_<name>``
    columns after all the others, so the columns a reader already indexes do
    not move.
    """
    chains = resolve_chains(paradigm)
    pins = sorted({int(s["pin"]) for c in chains for s in c["steps"]})
    camera_frames = camera_frames or {}

    fields = ["frame", "blockid", "t_s", "any_active"]
    for i, _c in enumerate(chains):
        fields += [f"chain{i}_step", f"chain{i}_active",
                   f"chain{i}_freq_hz", f"chain{i}_pw_ms"]
    fields += [f"pin{p}_ttl" for p in pins]
    fields += [f"frame_{nm}" for nm in camera_frames]

    b = _unwrap_blockids(np.asarray(blockids))
    rows = []
    for k, bid in enumerate(b):
        t = (int(bid) - 1) / fps          # blockid is 1-based from trigger start
        frame = k if frames is None else int(frames[k])
        row = {"frame": frame if frame >= 0 else "", "blockid": int(bid),
               "t_s": round(t, 6)}
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
        for nm, per_row in camera_frames.items():
            v = int(per_row[k])
            row[f"frame_{nm}"] = v if v >= 0 else ""
        rows.append(row)
    return fields, rows


@dataclass
class TraceResult:
    """What write_trace_result did.

    ``path`` is the written stim_trace.csv, or None when none was written and
    ``message`` says why. ``disagreement`` is set when the cameras that take
    part in the alignment do not hold the same block IDs (the videos are not
    trigger-aligned), or when none of them has frames (no camera numbers the
    rows). A caller must report it where the operator sees it.
    """
    path: Path | None
    message: str
    disagreement: str | None = None


def _frames_of(ids: np.ndarray, rows: np.ndarray) -> np.ndarray:
    """Position of each of ``rows`` in the sorted ``ids``, -1 where absent."""
    pos = np.searchsorted(ids, rows)
    ok = pos < ids.size
    ok[ok] = ids[pos[ok]] == rows[ok]
    return np.where(ok, pos, -1)


def _camera_blockids(recording_dir: Path):
    """(camera -> unwrapped block IDs, camera -> why it has none).

    A camera whose blockids.npy is missing or unreadable (truncated, not 1-D,
    a non-positive or non-monotonic ID) is reported with the reason rather
    than raised. The caller treats it like a camera with no frames, which the
    disagreement names, and builds the trace from the others: one corrupt
    camera must not cost every other camera its trace.
    """
    arrays, missing = {}, {}
    for d in camera_dirs(recording_dir):
        f = d / "blockids.npy"
        if not f.exists():
            missing[d.name] = "no blockids.npy"
            continue
        try:
            b = np.load(f)
            if b.ndim != 1:
                raise ValueError(f"expected 1-D block IDs, got shape {b.shape}")
            arrays[d.name] = _unwrap_blockids(b)
        except (OSError, EOFError, ValueError) as e:
            missing[d.name] = f"unreadable blockids.npy ({e})"
    return arrays, missing


def write_trace_result(recording_dir: Path, fps: float,
                       exclude=()) -> TraceResult:
    """Write stim_trace.csv beside the videos and report how it went.

    Cameras in ``exclude`` and cameras with a RETIRED.json still get their
    ``frame_<cam>`` column, but they are not held to the others' block IDs,
    never serve as the reference and add no rows: a camera left out of the
    alignment is expected to differ. A camera whose blockids.npy is missing
    or unreadable gets no column, and unless it is left out it is a
    disagreement that names the reason.
    """
    recording_dir = Path(recording_dir)
    paradigm_path = recording_dir / PARADIGM_NAME
    if not paradigm_path.exists():
        return TraceResult(None, "no stimulus paradigm recorded for this session")
    paradigm = json.loads(paradigm_path.read_text())

    arrays, missing = _camera_blockids(recording_dir)
    if not arrays:
        unreadable = {nm: why for nm, why in missing.items()
                      if why != "no blockids.npy"}
        if unreadable:
            return TraceResult(None, "no camera has a readable blockids.npy: "
                               + "; ".join(f"{nm} {why}"
                                           for nm, why in unreadable.items()))
        return TraceResult(None, "no blockids.npy (recording never stopped cleanly?)")
    left_out = dict(recording_meta.retired_cameras(recording_dir))
    left_out.update({nm: "excluded" for nm in exclude})
    held = [nm for nm in arrays if nm not in left_out]
    held_missing = {nm: why for nm, why in missing.items() if nm not in left_out}
    with_frames = [nm for nm in held if arrays[nm].size]
    # The reference is the lowest-numbered camera that takes part and has
    # frames; a camera with none cannot number the rows.
    ref = next(iter(with_frames), held[0] if held else next(iter(arrays)))
    ref_ids = arrays[ref]
    # RULE: agreement needs at least one participating camera with frames.
    # REASON: with every camera left out, or every held camera empty, the
    # comparison below is vacuously true, and a trace no camera's frames
    # number would be reported as consistent.
    agree = (bool(with_frames)
             and all(np.array_equal(arrays[nm], ref_ids) for nm in held)
             and not held_missing)
    # Rows are the triggers of the cameras that take part, so 'frame' is
    # never blank while they agree; a left-out camera only gets a column.
    held_arrays = [arrays[nm] for nm in held] or list(arrays.values())
    if all(np.array_equal(a, ref_ids) for a in held_arrays):
        rows_ids = ref_ids
    else:
        rows_ids = np.unique(np.concatenate(held_arrays))
    frames = _frames_of(ref_ids, rows_ids)
    camera_frames = {nm: _frames_of(a, rows_ids) for nm, a in arrays.items()}

    notes = []
    disagreement = None
    no_ids = "; ".join(f"{nm} has {why}" for nm, why in held_missing.items())
    if not held:
        disagreement = (
            f"no camera takes part in the alignment (left out: "
            f"{', '.join(left_out)}{'; ' + no_ids if no_ids else ''}), so no "
            f"camera is held to the others. stim_trace.csv has one row per "
            f"trigger any camera recorded, and 'frame' is {ref}'s frame number")
    elif not with_frames:
        disagreement = (
            f"no camera that takes part in the alignment has frames "
            f"({', '.join(held)} recorded none"
            f"{'; ' + no_ids if no_ids else ''}), so stim_trace.csv has no rows")
    elif not agree:
        lengths = {nm: int(arrays[nm].size) if nm in arrays else 0
                   for nm in [*held, *held_missing]}
        disagreement = (
            f"cameras disagree on block IDs {lengths}: the videos are not "
            f"trigger-aligned. stim_trace.csv has one row per trigger any of "
            f"them recorded; frame_<cam> gives each camera's frame for it, "
            f"blank where that camera has none, and 'frame' is {ref}'s frame "
            f"number{' (' + no_ids + ')' if no_ids else ''}")
    if disagreement:
        notes.append(disagreement)
    if rows_ids.size and int(rows_ids[0]) != 1:
        lead = int(rows_ids[0]) - 1
        notes.append(f"first block ID is {int(rows_ids[0])}, so {lead} leading "
                     f"trigger(s) were dropped — times account for this")

    fields, rows = build_rows(paradigm, rows_ids, fps, frames=frames,
                              camera_frames=camera_frames)
    out = recording_dir / TRACE_NAME
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    active = sum(r["any_active"] for r in rows)
    msg = (f"{len(rows)} frames, {active} with stimulation active "
           f"({100 * active / max(len(rows), 1):.1f}%)")
    if notes:
        msg = f"{msg} [{'; '.join(notes)}]"
    return TraceResult(out, msg, disagreement)


def trace_mismatch(recording_dir: Path, blockids) -> str | None:
    """Why stim_trace.csv does not describe cameras that all hold ``blockids``.

    Returns None when it does. For cameras that agree, the trace has one row
    per block ID in ``blockids``, in order, and ``frame`` counts 0, 1, 2, ...
    A trace that differs was written for other frames, typically before an
    alignment replaced the videos and stopped before rewriting it. Only the
    ``blockid`` and ``frame`` columns are read, so a long recording's trace
    is never held in memory whole.
    """
    path = Path(recording_dir) / TRACE_NAME
    if not path.exists():
        return f"there is no {TRACE_NAME}"
    want = _unwrap_blockids(np.asarray(blockids))
    try:
        with open(path, newline="") as f:
            reader = csv.reader(f)
            header = next(reader)
            i_bid, i_frame = header.index("blockid"), header.index("frame")
            n = 0
            for row in reader:
                if n == want.size:
                    return (f"it has more than {n} rows; the videos hold "
                            f"{want.size} frames")
                if int(row[i_bid]) != int(want[n]) or row[i_frame] != str(n):
                    return (f"row {n + 1} is block ID {row[i_bid]}, frame "
                            f"{row[i_frame] or 'blank'}; the videos hold block "
                            f"ID {int(want[n])} as frame {n}")
                n += 1
    except (OSError, StopIteration, ValueError, IndexError, csv.Error) as e:
        return f"{TRACE_NAME} is unreadable ({type(e).__name__}: {e})"
    if n != want.size:
        return f"it has {n} rows; the videos hold {want.size} frames"
    return None


def write_trace(recording_dir: Path, fps: float) -> tuple[Path | None, str]:
    """Write stim_trace.csv beside the videos. Returns (path, message).

    ``write_trace_result`` is the same with the disagreement reported
    separately; a caller that can show a warning should use that.
    """
    r = write_trace_result(recording_dir, fps)
    return r.path, r.message
