"""Write a per-frame stimulus trace next to a recording's videos.

New recordings get ``stim_trace.csv`` automatically at stop; this is for
recordings made before that existed, or to regenerate after editing a paradigm
(the ``blocks``/``edges`` graph in ``stim_paradigm.json`` is recompiled, so an
edit to the block list changes the trace).

Runs in the project environment, so use the project's interpreter:

    uv run python 3_stim_trace.py <recording_dir>          # one recording
    uv run python 3_stim_trace.py data --all               # every recording under a root
    uv run python 3_stim_trace.py <recording_dir> --fps 100

The mapping is exact because one Arduino drives both the camera triggers and the
stimulus: frame -> block ID (trigger ordinal) -> seconds since stim t=0. See
``gui_app/stim_trace.py`` for the details, including why this is a prediction of
what the paradigm delivered rather than an observation that it did.

Exit status: 0 when every target was written; 1 when a single target was
skipped or when any recording in a ``--all`` batch failed (the batch itself
continues past the failure and prints a SKIP line for it).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gui_app.stim_trace import PARADIGM_NAME, write_trace


def _fps_for(recording_dir: Path, override: float | None) -> float:
    """Trigger rate: --fps, else the session metadata, else 100."""
    if override:
        return override
    meta = recording_dir.parent / "session_metadata.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text())
            key = ("calibration_frame_rate"
                   if recording_dir.name == "calibration" else "frame_rate")
            if data.get(key):
                return float(data[key])
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return 100.0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", type=Path,
                    help="a recording directory, or a data root with --all")
    ap.add_argument("--all", action="store_true",
                    help="recurse and process every recording with a paradigm")
    ap.add_argument("--fps", type=float, default=None,
                    help="trigger rate (default: from session_metadata.json, else 100)")
    args = ap.parse_args()

    if args.all:
        targets = sorted(p.parent for p in args.path.rglob(PARADIGM_NAME))
        if not targets:
            print(f"no recordings with a {PARADIGM_NAME} under {args.path}")
            return 1
    else:
        targets = [args.path]

    failures = 0
    for rec in targets:
        # One corrupt recording (non-monotonic block IDs, a truncated json, a
        # half-written .npy) must cost that recording a SKIP line, not the
        # rest of the batch.
        try:
            out, msg = write_trace(rec, _fps_for(rec, args.fps))
        except Exception as e:
            out, msg = None, f"{type(e).__name__}: {e}"
        if out is None:
            print(f"SKIP {rec}: {msg}")
            failures += 1
        else:
            print(f"OK   {out}: {msg}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
