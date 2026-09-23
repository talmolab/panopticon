"""Align multi-camera recordings by block ID (trigger ordinal).

The host or the network occasionally drops a frame, which breaks the
assumption that frame *i* is the same trigger across cameras: after the first
dropped frame every later frame is off by one or more, so frame-by-frame use
compares different moments in time.

Each camera's ``blockids.npy`` records the block ID (trigger ordinal) of every
recorded frame. The block IDs common to all the aligned cameras are the
triggers every one of them captured, and the hardware trigger fires all
cameras at once, so those frames are a synchronized, equal-length set.

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
camera's blockids.npy + frametimes.npy are rewritten to match. When the
recording has a ``stim_paradigm.json``, its ``stim_trace.csv`` is then
rewritten from the new block IDs, because the old trace labels the replaced
frames with the stimulus of other triggers.

A replace overwrites each camera's only copy, and a camera that ended early
or started late (a retirement, a truncated tail) cuts every other camera to
its length. --replace therefore refuses while such a camera takes part, names
it, and changes no video. Then choose one:

  --exclude cam3            align the other cameras; cam3's files stay as recorded
  --truncate-to-shortest    cut every camera to the common triggers anyway

A camera the GUI retired during the recording has a RETIRED.json in its
directory and is excluded by default; --include-retired aligns it with the
others.

The frame rate and the re-encode quality default to the values the acquisition
recorded in its own ``session_metadata.json`` (inside ``<recording_dir>``). A
recording without one falls back to the session-level copy one directory up,
with a warning, because that copy describes the session's first acquisition.
The rate stamps the re-encoded videos and is the reference for the block-rate
check, so a wrong rate mis-stamps every video and flags every camera at once.

Exit status: 0 on success; 1 on an error, a refused replace, when --replace
left any camera unreplaced, or when the stim trace could not be rewritten; 2
when the only problem is a warning: a block-rate warning (a camera whose block
IDs did not advance at the trigger rate, i.e. it ignored triggers), or a stim
trace rewritten from cameras that still disagree. Every stim-trace problem is
also appended to the acquisition's WARNINGS.txt.

In post-hoc mode (``realtime_kick: false``) the GUI runs this alignment itself
after each recording; this CLI is for reprocessing existing sessions.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from gui_app import alignment, ffmpeg_cmd, recording_meta, stim_trace

DEFAULT_FPS = 100
DEFAULT_QUALITY = 21


def session_defaults(rec_dir: Path) -> tuple:
    """(fps, quality, fps_source, quality_source, warnings) for a recording.

    Values come from ``recording_meta.acquisition_params``; a value no file
    records is the fallback here, with source None so the run can say so.
    """
    params = recording_meta.acquisition_params(rec_dir)
    fps = params["acq_fps"]
    quality = params["quality"]
    try:
        quality = int(quality) if quality is not None else None
    except (TypeError, ValueError):
        params["warnings"].append(
            f"quality {quality!r} in session_metadata.json is not a number; "
            f"using {DEFAULT_QUALITY}")
        quality = None
    return (int(round(fps)) if fps else DEFAULT_FPS,
            quality if quality is not None else DEFAULT_QUALITY,
            params["sources"].get("acq_fps") if fps else None,
            params["sources"].get("quality") if quality is not None else None,
            params["warnings"])


def print_table(an: alignment.Analysis) -> None:
    print(f"Recording: {an.rec_dir}")
    print(f"Cameras:   {', '.join(an.names)}")
    for nm, why in an.excluded.items():
        print(f"Excluded:  {nm} ({why}); its files stay as recorded")
    print(f"Trigger span (union): {an.full_span}")
    print(f"Common (aligned) frames: {an.common.size}\n")
    print(f"{'cam':6} {'recorded':>9} {'dropped':>8} {'%drop':>7} "
          f"{'first':>8} {'last':>8}")
    for nm, pc in an.per_camera().items():
        mark = "  short" if nm in an.short_cams else ""
        print(f"{nm:6} {pc['recorded']:>9} {pc['dropped']:>8} "
              f"{100 * pc['dropped'] / an.full_span:>6.2f}% "
              f"{pc['first_trigger']:>8} {pc['last_trigger']:>8}{mark}")
    for nm in an.empty_cams:
        print(f"{nm:6} {0:>9} {'-':>8} {'-':>7} {'-':>8} {'-':>8}  no frames")
    total_drop = an.full_span - an.common.size
    print(f"\nAligned set keeps {an.common.size} of {an.full_span} triggers; drops "
          f"{total_drop} ({100 * total_drop / an.full_span:.2f}%) any camera missed.")
    for nm, why in an.short_cams.items():
        print(f"Short camera {nm}: {why}")
    print()
    if an.rate_checked:
        print(f"Block-rate check judged: {', '.join(an.rate_checked)}")
    for nm, why in an.rate_skipped.items():
        print(f"Block-rate check skipped {nm}: {why}")
    if not an.rate_checked:
        print("\nWARNING (block rate): no camera could be checked, so nothing "
              "here shows that the block IDs are trigger ordinals.",
              file=sys.stderr)
    for msg in an.rate_warnings:
        print(f"\nWARNING (block rate): {msg}", file=sys.stderr)


def exclusions(rec_dir: Path, requested: list, include_retired: bool) -> dict:
    """camera -> reason for every camera this run leaves out.

    The cameras named on the command line, plus every camera with a
    RETIRED.json unless ``include_retired``.
    """
    out = {}
    if not include_retired:
        for nm, why in recording_meta.retired_cameras(rec_dir).items():
            out[nm] = f"retired during the recording: {why}"
    for item in requested:
        for nm in (x.strip() for x in item.split(",")):
            if nm:
                out[nm] = "excluded on the command line"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recording_dir", type=Path)
    ap.add_argument("--replace", action="store_true",
                    help="trim+replace the per-camera mp4s (re-encodes)")
    ap.add_argument("--quality", type=int, default=None,
                    help="QP for the --replace re-encode (default: the "
                         "acquisition's session_metadata.json, else 21)")
    ap.add_argument("--fps", type=int, default=None,
                    help="trigger rate: stamps re-encoded videos and is the "
                         "block-rate reference (default: the acquisition's "
                         "session_metadata.json, else 100 with a warning)")
    ap.add_argument("--encoder", choices=ffmpeg_cmd.BACKENDS, default=None,
                    help="H.264 encoder for --replace (default nvenc; x264 "
                         "for a machine without an NVIDIA GPU)")
    ap.add_argument("--parallel", type=int, default=3,
                    help="cameras re-encoded concurrently with --replace")
    ap.add_argument("--exclude", action="append", default=[], metavar="CAM",
                    help="leave this camera out of the alignment; its files "
                         "stay as recorded (repeat, or separate with commas)")
    ap.add_argument("--truncate-to-shortest", action="store_true",
                    help="with --replace, cut every camera to the common "
                         "triggers even when a camera ended early or started "
                         "late")
    ap.add_argument("--include-retired", action="store_true",
                    help="align cameras that have a RETIRED.json with the "
                         "others instead of excluding them")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only (including the block-rate check); write nothing")
    args = ap.parse_args()

    (meta_fps, meta_quality, fps_source, quality_source,
     meta_warnings) = session_defaults(args.recording_dir)
    fps = args.fps if args.fps is not None else meta_fps
    quality = args.quality if args.quality is not None else meta_quality
    for w in meta_warnings:
        print(f"WARNING: {w}", file=sys.stderr)
    if args.fps is None:
        if fps_source:
            print(f"fps {fps} from {fps_source}")
        else:
            print(f"WARNING: no session_metadata.json records the frame rate "
                  f"of {args.recording_dir}; assuming {fps} fps. Pass --fps if "
                  f"the recording ran at another rate.", file=sys.stderr)
    if args.replace and args.quality is None:
        print(f"quality {quality} from {quality_source}" if quality_source else
              f"quality {quality} (the default: no session_metadata.json "
              f"records one; pass --quality to change it)")

    excluded = exclusions(args.recording_dir, args.exclude,
                          args.include_retired)
    try:
        an = alignment.analyse(args.recording_dir, fps, exclude=excluded)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print_table(an)
    would_refuse = alignment.refusal_reason(an, args.truncate_to_shortest)
    if would_refuse and (args.dry_run or not args.replace):
        print(f"\nNOTE: --replace would be refused. {would_refuse}")

    if args.dry_run:
        return 2 if an.rate_warnings else 0

    def _progress(done, total, msg):
        print(f"  [{done}/{total}] {msg}")

    print("\nAligning..." + (" (re-encode + replace)" if args.replace else
                             " (index only)"))
    try:
        # The table above was printed from ``an``; passing it in means the
        # block IDs are loaded and intersected once per run.
        summary = alignment.align_recording(
            args.recording_dir, fps=fps, quality=quality,
            replace=args.replace, parallel=args.parallel, progress=_progress,
            backend=args.encoder, analysis=an, exclude=excluded,
            truncate_to_shortest=args.truncate_to_shortest)
    except Exception as e:
        print(f"ERROR: alignment failed: {e}", file=sys.stderr)
        return 1

    rc = 0
    if summary.get("index_error"):
        # The videos are in their final state whatever happened to the index,
        # so this is reported on its own rather than as "NOT replaced".
        rc = 1
        print(f"\nERROR: {summary['index_error']} (the aligned/ index is "
              f"derived data; the videos and metadata are as reported below)",
              file=sys.stderr)
    if summary.get("refused"):
        rc = 1
        print(f"\nERROR: {summary['refused']}", file=sys.stderr)
        print("Wrote the aligned/ index only; no video was changed.")
    elif args.replace and summary["needed"]:
        if summary["replaced"] and not excluded:
            print(f"\nReplaced all videos with {summary['common_frames']}-frame "
                  "aligned versions.")
        elif summary["replaced"]:
            print(f"\nReplaced the videos of {', '.join(summary['replaced_cams'])} "
                  f"with {summary['common_frames']}-frame aligned versions. "
                  f"Left as recorded: {', '.join(excluded)}.")
        else:
            rc = 1
            print(f"\nERROR: {len(summary['failed_cams'])} camera(s) NOT replaced "
                  f"(originals kept): {', '.join(summary['failed_cams'])}",
                  file=sys.stderr)
            for w in summary["failures"]:
                if w != summary.get("index_error"):
                    print(f"  - {w}", file=sys.stderr)
            if summary["replaced_cams"]:
                print(f"Replaced: {', '.join(summary['replaced_cams'])}",
                      file=sys.stderr)
    elif not summary["needed"]:
        print("\nNo loss — videos already aligned, nothing re-encoded."
              + (f" Left out: {', '.join(excluded)}." if excluded else ""))
    else:
        print(f"\nWrote aligned/ index ({summary['common_frames']} common "
              "frames). Re-run with --replace to trim the videos.")
    trace_rc = rewrite_stim_trace(args.recording_dir, fps, summary, excluded)
    if trace_rc == 1:
        rc = 1
    if rc == 0 and (summary["rate_warnings"] or trace_rc == 2):
        rc = 2
    return rc


def rewrite_stim_trace(rec_dir: Path, fps: int, summary: dict,
                       excluded: dict) -> int:
    """Rewrite stim_trace.csv after a replace changed any camera's block IDs.

    Returns 0 when nothing needed doing or the new trace is consistent, 2 when
    it was written from cameras that disagree, 1 when it could not be written.
    The trace is derived from blockids.npy, which a replace rewrites, so a
    trace left as it was labels frames with the stimulus of other triggers.
    Every problem is printed and appended to the acquisition's WARNINGS.txt.
    """
    rec_dir = Path(rec_dir)
    if not summary.get("replaced_cams") or \
            not (rec_dir / stim_trace.PARADIGM_NAME).exists():
        return 0
    try:
        r = stim_trace.write_trace_result(rec_dir, fps, exclude=excluded)
        path, msg, disagreement = r.path, r.message, r.disagreement
    except Exception as e:
        path, msg, disagreement = None, f"{type(e).__name__}: {e}", None
    if path is None:
        text = (f"{stim_trace.TRACE_NAME} could not be rewritten after "
                f"alignment replaced {', '.join(summary['replaced_cams'])} "
                f"({msg}). The old file describes the frames before the "
                f"alignment. Re-run 3_stim_trace.py on {rec_dir}.")
        recording_meta.append_warning(rec_dir, text)
        print(f"\nERROR: {text}", file=sys.stderr)
        return 1
    print(f"\nRewrote {path.name}: {msg}")
    if disagreement:
        text = f"{stim_trace.TRACE_NAME}: {disagreement}."
        where = recording_meta.append_warning(rec_dir, text)
        print(f"WARNING: {text}"
              + (f" (added to {where.name})" if where else ""),
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
