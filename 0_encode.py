"""Turn a finished acquisition's capture files into mp4s, without the GUI.

The GUI does this at the end of every acquisition. When that step did not
finish (Panopticon was closed during ENCODING, or the finalize failed), the
capture files stay in each camera directory, and this script runs the same
job on them (``gui_app.encode_worker.EncodeWorker``, headless):

  uv run python 0_encode.py <acquisition_dir>
  uv run python 0_encode.py <acquisition_dir> --fps 100 --encoder x264

``<acquisition_dir>`` is the directory holding cam1/, cam2/, ... (for example
``<output_dir>/<date>/<session>/recording``). For each camera:

- ``stream.h264`` (real-time encode): stream-copied into an mp4, in seconds.
  When the encoder died mid-recording, ``raw_tail.bin`` is encoded and
  appended first.
- ``raw.bin`` (no real-time encode, or a camera whose encoder never started):
  a full H.264 encode.

A source file is deleted only after its mp4 is verified, as in the GUI. A
camera with an mp4 and no capture file is already done and is skipped, and so
is a camera that recorded no frames.

The frame rate, frame size, quality, date and session id come from the
acquisition's own ``session_metadata.json``, then from the session-level copy
with a warning; the options below override them. Without a frame rate the
script refuses, because the remux stamps it into every mp4.

Run ``2_align.py`` on the directory afterwards: it checks the block-ID rate,
and a recording made with ``realtime_kick: false`` needs its ``--replace``.

Exit status: 0 when every camera has its mp4; 1 when any camera failed or
nothing could be done; 2 when every camera was encoded but one holds fewer
frames than were captured (a failed tail merge, described in that camera's
WARNINGS.txt).
"""
import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from gui_app import alignment, ffmpeg_cmd, recording_meta  # noqa: E402

DEFAULT_QUALITY = 21
#: Capture files EncodeWorker turns into an mp4.
SOURCES = ("stream.h264", "raw.bin", "raw_tail.bin")


def _resolution(value):
    """(w, h) from session_metadata.json's ``resolution``, or None."""
    try:
        w, h = (int(v) for v in value)
    except (TypeError, ValueError):
        return None
    return (w, h) if w > 0 and h > 0 else None


def _recorded_nothing(d: Path) -> bool:
    """True when a camera directory holds no frame at all: no block IDs, and
    every capture file empty (a camera retired before its first frame)."""
    b = d / "blockids.npy"
    if b.exists():
        try:
            if np.load(b, mmap_mode="r").size > 0:
                return False
        except Exception:
            return False            # unreadable is not provably empty
    return all((d / n).stat().st_size == 0 for n in SOURCES if (d / n).exists())


def plan_cameras(video_dir: Path, mp4_name, frame_bytes):
    """Sort the cameras into (to_encode, done, empty, refused).

    ``done`` cameras already have their mp4 and no capture file; ``empty``
    cameras recorded no frame, so there is nothing to encode. ``refused``
    maps camera -> reason for a camera this script must not touch:
    re-encoding it would describe frames the metadata does not, or leave two
    mp4s in one directory. ``frame_bytes`` is w*h, or None when the frame
    size is unknown.
    """
    todo, done, empty, refused = [], [], [], {}
    for d in alignment.camera_dirs(video_dir):
        cam = d.name
        present = [n for n in SOURCES if (d / n).exists()]
        try:
            existing = alignment.video_for(d)
        except ValueError as e:
            refused[cam] = str(e)
            continue
        if not present:
            if existing is not None:
                done.append(cam)
            elif _recorded_nothing(d):
                empty.append(cam)
            else:
                refused[cam] = ("no capture file (stream.h264, raw.bin) and no "
                                "mp4")
            continue
        if _recorded_nothing(d):
            empty.append(cam)
            continue
        if (d / "blockids.full.npy").exists():
            refused[cam] = (
                "an earlier tail merge failed and cut blockids.npy and "
                "frametimes.npy to the frames in stream.h264; the full arrays "
                "are in blockids.full.npy and frametimes.full.npy. Copy them "
                "over blockids.npy and frametimes.npy before re-encoding this "
                "camera, or its mp4 would hold frames its block IDs do not "
                "list.")
            continue
        others = sorted(f.name for f in d.glob("*.mp4")
                        if f.name not in (mp4_name(cam), alignment.ALIGN_TMP_NAME))
        if others:
            refused[cam] = (f"already holds {', '.join(others)}, and the new "
                            f"mp4 would be {mp4_name(cam)}. Move the old file "
                            f"away, or pass --date/--session-id to match it.")
            continue
        needs_size = [n for n in ("raw.bin", "raw_tail.bin") if n in present
                      and (d / n).stat().st_size > 0]
        if needs_size and frame_bytes is None:
            refused[cam] = (f"{needs_size[0]} needs the frame size, which no "
                            f"session_metadata.json records; pass --width and "
                            f"--height")
            continue
        bad = [n for n in needs_size if (d / n).stat().st_size % frame_bytes]
        if bad:
            refused[cam] = (f"{bad[0]} is {(d / bad[0]).stat().st_size} bytes, "
                            f"not a whole number of {frame_bytes}-byte frames; "
                            f"check the frame size (--width, --height)")
            continue
        todo.append(cam)
    return todo, done, empty, refused


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video_dir", type=Path,
                    help="the acquisition directory holding cam1/, cam2/, ...")
    ap.add_argument("--fps", type=int, default=None,
                    help="trigger rate stamped into the mp4s (default: "
                         "session_metadata.json)")
    ap.add_argument("--width", type=int, default=None,
                    help="frame width in pixels, for raw.bin and raw_tail.bin "
                         "(default: session_metadata.json)")
    ap.add_argument("--height", type=int, default=None,
                    help="frame height in pixels (default: session_metadata.json)")
    ap.add_argument("--quality", type=int, default=None,
                    help="QP for raw.bin and raw_tail.bin encodes (default: "
                         f"session_metadata.json, else {DEFAULT_QUALITY})")
    ap.add_argument("--encoder", choices=ffmpeg_cmd.BACKENDS, default=None,
                    help="H.264 encoder for raw.bin and raw_tail.bin (default "
                         "nvenc; x264 for a machine without an NVIDIA GPU)")
    ap.add_argument("--parallel", type=int, default=3,
                    help="cameras processed at once; each raw encode holds one "
                         "NVENC session")
    ap.add_argument("--date", default=None,
                    help="date part of the mp4 names (default: "
                         "session_metadata.json, else the directory layout)")
    ap.add_argument("--session-id", default=None,
                    help="session part of the mp4 names (default: "
                         "session_metadata.json, else the directory layout)")
    args = ap.parse_args()

    video_dir = args.video_dir
    if not video_dir.is_dir() or not alignment.camera_dirs(video_dir):
        print(f"ERROR: {video_dir} holds no cam*/ directories", file=sys.stderr)
        return 1
    params = recording_meta.acquisition_params(video_dir)
    for w in params["warnings"]:
        print(f"WARNING: {w}", file=sys.stderr)

    fps = args.fps if args.fps is not None else (
        int(round(params["acq_fps"])) if params["acq_fps"] else None)
    if not fps or fps <= 0:
        print(f"ERROR: no session_metadata.json records the frame rate of "
              f"{video_dir}; pass --fps. The remux stamps it into every mp4, "
              f"so a guessed rate would play every video at the wrong speed.",
              file=sys.stderr)
        return 1
    if args.fps is None:
        print(f"fps {fps} from {params['sources']['acq_fps']}")
    quality = args.quality
    if quality is None:
        try:
            quality = int(params["quality"])
            print(f"quality {quality} from {params['sources']['quality']}")
        except (TypeError, ValueError):
            quality = DEFAULT_QUALITY
    size = _resolution(params["resolution"])
    w = args.width if args.width is not None else (size[0] if size else None)
    h = args.height if args.height is not None else (size[1] if size else None)
    frame_bytes = w * h if w and h and w > 0 and h > 0 else None
    date = args.date or params["date"] or video_dir.parent.parent.name
    session_id = args.session_id or params["session_id"] or video_dir.parent.name
    acq_type = video_dir.name

    def mp4_name(cam: str) -> str:
        # The same name EncodeWorker gives it, so the check below sees the
        # file the encode will write.
        return f"{date}-{session_id}-{cam}-{acq_type}.mp4"

    todo, done, empty, refused = plan_cameras(video_dir, mp4_name, frame_bytes)
    for cam in done:
        print(f"{cam}: already has its mp4 and no capture file; skipped")
    for cam in empty:
        print(f"{cam}: recorded no frames, so there is no mp4 to make; skipped")
    for cam, why in refused.items():
        print(f"ERROR: {cam}: not encoded: {why}", file=sys.stderr)
    if not todo:
        if refused:
            return 1
        print("Nothing to encode.")
        return 0

    from gui_app.encode_worker import EncodeWorker
    worker = EncodeWorker(video_dir, todo, acq_type, w or 0, h or 0, fps,
                          quality, str(date), str(session_id),
                          max_parallel=max(0, args.parallel), realtime=True,
                          backend=args.encoder)
    results = []
    worker.progress.connect(lambda d, t: print(f"  [{d}/{t}]", flush=True))
    worker.finished_all.connect(results.extend)
    print(f"Encoding {len(todo)} camera(s) at {fps} fps: {', '.join(todo)}")
    worker.run()

    failed = [cam for cam, _n, ok in results if not ok]
    print()
    for cam, n, ok in results:
        print(f"{cam}: {'OK    ' if ok else 'FAILED'} {n} frames")
    problems = [f"0_encode.py: {cam} not encoded: {why}"
                for cam, why in refused.items()]
    if failed:
        problems.append(f"0_encode.py: {', '.join(failed)} produced no usable "
                        f"video; their source files are kept (see "
                        f"encode_error.log and tail_error.log in each).")
    problems += [f"0_encode.py: {w}" for w in worker.warnings]
    for text in problems:
        recording_meta.append_warning(video_dir, text)
    if failed or refused:
        print(f"\nERROR: {len(failed) + len(refused)} camera(s) have no mp4: "
              f"{', '.join([*failed, *refused])}", file=sys.stderr)
        return 1
    if worker.warnings:
        print(f"\nWARNING: {len(worker.warnings)} camera(s) hold fewer frames "
              f"than were captured; see WARNINGS.txt", file=sys.stderr)
        return 2
    print(f"\nEvery camera has its mp4. Next: python 2_align.py {video_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
