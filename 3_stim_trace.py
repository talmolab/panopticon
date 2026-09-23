"""Write a per-frame stimulus trace next to a recording's videos.

New recordings get ``stim_trace.csv`` automatically at stop, and
``2_align.py --replace`` rewrites it after it replaces videos. This is for
recordings made before the trace existed, for a trace a crash left stale, or to
regenerate after editing a paradigm (the ``blocks``/``edges`` graph in
``stim_paradigm.json`` is recompiled, so an edit to the block list changes the
trace).

Runs in the project environment, so use the project's interpreter:

    uv run python 3_stim_trace.py <recording_dir>          # one recording
    uv run python 3_stim_trace.py data --all               # every recording under a root
    uv run python 3_stim_trace.py <recording_dir> --fps 100

The mapping is exact because one Arduino drives both the camera triggers and the
stimulus: frame -> block ID (trigger ordinal) -> seconds since stim t=0. See
``gui_app/stim_trace.py`` for the details, including why this is a prediction of
what the paradigm delivered rather than an observation that it did.

Each row is one trigger, with a ``frame_<cam>`` column per camera. When the
cameras of a recording do not hold the same block IDs, its videos are not
trigger-aligned: the trace is still written, a WARN line says so, and the
warning is appended to that recording's WARNINGS.txt. Cameras with a
RETIRED.json are not held to the others' block IDs.

Exit status: 0 when every target was written from cameras that agree; 1 when a
single target was skipped or when any recording in a ``--all`` batch failed
(the batch itself continues past the failure and prints a SKIP line for it); 2
when every target was written but some recording's cameras disagree.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gui_app import recording_meta  # noqa: E402
from gui_app.stim_trace import PARADIGM_NAME, write_trace_result  # noqa: E402

DEFAULT_FPS = 100.0


def _fps_for(recording_dir: Path, override: float | None) -> float:
    """Trigger rate: --fps, else the acquisition's metadata, else 100.

    Every fallback is printed, because the rate scales every time in the
    trace: a wrong one labels frames with the stimulus of another instant.
    """
    if override:
        return override
    params = recording_meta.acquisition_params(recording_dir)
    for w in params["warnings"]:
        print(f"WARN {recording_dir}: {w}", file=sys.stderr)
    if params["acq_fps"]:
        return params["acq_fps"]
    print(f"WARN {recording_dir}: no session_metadata.json records the frame "
          f"rate; assuming {DEFAULT_FPS:g} fps. Pass --fps if the recording "
          f"ran at another rate.", file=sys.stderr)
    return DEFAULT_FPS


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path,
                    help="a recording directory, or a data root with --all")
    ap.add_argument("--all", action="store_true",
                    help="recurse and process every recording with a paradigm")
    ap.add_argument("--fps", type=float, default=None,
                    help="trigger rate (default: the acquisition's "
                         "session_metadata.json, else 100 with a warning)")
    args = ap.parse_args()

    if args.all:
        targets = sorted(p.parent for p in args.path.rglob(PARADIGM_NAME))
        if not targets:
            print(f"no recordings with a {PARADIGM_NAME} under {args.path}")
            return 1
    else:
        targets = [args.path]

    failures = disagreements = 0
    for rec in targets:
        # One corrupt recording (non-monotonic block IDs, a truncated json, a
        # half-written .npy) must cost that recording a SKIP line, not the
        # rest of the batch.
        disagreement = None
        try:
            r = write_trace_result(rec, _fps_for(rec, args.fps))
            out, msg, disagreement = r.path, r.message, r.disagreement
        except Exception as e:
            out, msg = None, f"{type(e).__name__}: {e}"
        if out is None:
            print(f"SKIP {rec}: {msg}")
            failures += 1
            continue
        print(f"OK   {out}: {msg}")
        if disagreement:
            disagreements += 1
            text = f"stim_trace.csv: {disagreement}."
            where = recording_meta.append_warning(rec, text)
            print(f"WARN {rec}: {text}"
                  + (f" (added to {where.name})" if where else
                     f" ({recording_meta.WARNINGS_NAME} could not be written)"),
                  file=sys.stderr)
    if failures:
        return 1
    return 2 if disagreements else 0


if __name__ == "__main__":
    raise SystemExit(main())
