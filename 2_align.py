"""Align multi-camera recordings by GigE block ID (trigger ordinal).

At 6x100 fps over GigE the host/network occasionally drops a frame, which
breaks the assumption that frame *i* is the same trigger across cameras: after
the first dropped frame every later frame is off by one or more, so naive
frame-by-frame use silently compares different moments in time.

Each camera's ``blockids.npy`` records the GigE block ID (trigger ordinal) of
every recorded frame. The block IDs common to ALL cameras are the triggers
every camera captured, and the hardware trigger fires all cameras at once — so
those frames are a synchronized, equal-length set.

Runs in the project environment (it imports ``gui_app``), so use the
project's interpreter rather than an isolated script environment:

  uv run python 2_align.py <recording_dir>            # write aligned/ index only
  uv run python 2_align.py <recording_dir> --replace  # also trim+replace the mp4s
  uv run python 2_align.py <recording_dir> --dry-run  # report only, incl. the block-rate check

``<recording_dir>`` holds cam1/, cam2/, ... each with ``blockids.npy`` and the
camera's recording ``.mp4``. Always writes ``aligned/alignment.{npz,json}``
(the lossless index: frame_index[c, k] = frame number in camera c's video for
the k-th synchronized sample). With --replace, each camera's mp4 is re-encoded
to only the common frames and atomically replaces the original, and that
camera's blockids.npy + frametimes.npy are rewritten to match.

The frame rate defaults to the one recorded in ``session_metadata.json`` beside
the recording directory (``frame_rate`` for recording/, ``calibration_frame_rate``
for calibration/), because it stamps the re-encoded videos AND is the reference
for the block-rate check; a wrong rate mis-stamps every video and flags every
camera at once.

Exit status: 0 on success; 1 on an error or when --replace left any camera
unreplaced; 2 when the only problem is a block-rate warning (a camera whose
block IDs did not advance at the trigger rate, i.e. it ignored triggers).

The GUI runs this automatically after a realtime recording; this CLI is for
reprocessing existing sessions.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gui_app import alignment, ffmpeg_cmd

DEFAULT_FPS = 100
DEFAULT_QUALITY = 21


def session_defaults(rec_dir: Path) -> tuple:
    """(fps, quality, source) from session_metadata.json, or the fallbacks.

    ``source`` names where the numbers came from so the run can print it.
    """
    meta = Path(rec_dir).parent / "session_metadata.json"
    if meta.exists():
        try:
            data = json.loads(meta.read_text())
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict):
            key = ("calibration_frame_rate" if Path(rec_dir).name == "calibration"
                   else "frame_rate")
            fps = data.get(key)
            quality = data.get("quality")
            if fps:
                return (int(round(float(fps))),
                        int(quality) if quality else DEFAULT_QUALITY,
                        f"{meta.name}:{key}")
    return DEFAULT_FPS, DEFAULT_QUALITY, None


def print_table(an: alignment.Analysis) -> None:
    print(f"Recording: {an.rec_dir}")
    print(f"Cameras:   {', '.join(an.names)}")
    print(f"Trigger span (union): {an.full_span}")
    print(f"Common (aligned) frames: {an.common.size}\n")
    print(f"{'cam':6} {'recorded':>9} {'dropped':>8} {'%drop':>7}")
    for nm, pc in an.per_camera().items():
        print(f"{nm:6} {pc['recorded']:>9} {pc['dropped']:>8} "
              f"{100 * pc['dropped'] / an.full_span:>6.2f}%")
    total_drop = an.full_span - an.common.size
    print(f"\nAligned set keeps {an.common.size} of {an.full_span} triggers; drops "
          f"{total_drop} ({100 * total_drop / an.full_span:.2f}%) any camera missed.")
    for msg in an.rate_warnings:
        print(f"\nWARNING (block rate): {msg}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording_dir", type=Path)
    ap.add_argument("--replace", action="store_true",
                    help="trim+replace the per-camera mp4s (re-encodes)")
    ap.add_argument("--quality", type=int, default=None,
                    help="QP for the --replace re-encode "
                         "(default: session_metadata.json, else 21)")
    ap.add_argument("--fps", type=int, default=None,
                    help="trigger rate: stamps re-encoded videos and is the "
                         "block-rate reference (default: session_metadata.json, "
                         "else 100 with a warning)")
    ap.add_argument("--encoder", choices=ffmpeg_cmd.BACKENDS, default=None,
                    help="H.264 encoder for --replace (default nvenc; x264 "
                         "for a machine without an NVIDIA GPU)")
    ap.add_argument("--parallel", type=int, default=3,
                    help="cameras re-encoded concurrently with --replace")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only (including the block-rate check); write nothing")
    args = ap.parse_args()

    meta_fps, meta_quality, source = session_defaults(args.recording_dir)
    fps = args.fps or meta_fps
    quality = args.quality or meta_quality
    if args.fps is None:
        if source:
            print(f"fps {fps} from {source}")
        else:
            print(f"WARNING: no session_metadata.json beside "
                  f"{args.recording_dir}; assuming {fps} fps. Pass --fps if the "
                  f"recording ran at another rate.", file=sys.stderr)

    try:
        an = alignment.analyse(args.recording_dir, fps)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print_table(an)

    if args.dry_run:
        return 2 if an.rate_warnings else 0

    def _progress(done, total, msg):
        print(f"  [{done}/{total}] {msg}")

    print("\nAligning..." + (" (re-encode + replace)" if args.replace else
                             " (index only)"))
    try:
        summary = alignment.align_recording(
            args.recording_dir, fps=fps, quality=quality,
            replace=args.replace, parallel=args.parallel, progress=_progress,
            backend=args.encoder)
    except Exception as e:
        print(f"ERROR: alignment failed: {e}", file=sys.stderr)
        return 1

    rc = 0
    if args.replace and summary["needed"]:
        if summary["replaced"]:
            print(f"\nReplaced all videos with {summary['common_frames']}-frame "
                  "aligned versions.")
        else:
            rc = 1
            print(f"\nERROR: {len(summary['failed_cams'])} camera(s) NOT replaced "
                  f"(originals kept): {', '.join(summary['failed_cams'])}",
                  file=sys.stderr)
            for w in summary["failures"]:
                print(f"  - {w}", file=sys.stderr)
            if summary["replaced_cams"]:
                print(f"Replaced: {', '.join(summary['replaced_cams'])}",
                      file=sys.stderr)
    elif not summary["needed"]:
        print("\nNo loss — videos already aligned, nothing re-encoded.")
    else:
        print(f"\nWrote aligned/ index ({summary['common_frames']} common "
              "frames). Re-run with --replace to trim the videos.")
    if rc == 0 and summary["rate_warnings"]:
        rc = 2
    return rc


if __name__ == "__main__":
    sys.exit(main())
