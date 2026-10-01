"""Give a recording's mp4s frame timing that a frame-index seek can trust.

A real-time stream encoded without its frame rate says 30 fps in its timing
information. The capture's remux then gave each video's last frame 1/30 s, so
the mp4's average frame rate reads slightly under the real one. A reader that
seeks by frame index from that average (OpenCV's CAP_PROP_POS_FRAMES, which
sleap-io tries first) returns a later frame than the one asked for: one frame
late from about frame 5,000 at 100 fps, two from about 13,000. Sequential
reads are unaffected.

`retime_video` rewrites one mp4 without re-encoding. It sets the stream's tick
rate to the acquisition's frame rate and remuxes the way the capture does, and
it replaces the file only once the rewrite is proven picture for picture.

    uv run python -m gui_app.retime FOLDER [FOLDER ...]
    uv run python -m gui_app.retime --check FOLDER      # report, change nothing

A FOLDER is a session, an acquisition, or any folder above them. The frame
rate comes from each acquisition's session_metadata.json.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from gui_app import ffmpeg_cmd, recording_meta

#: The codecs whose timing this module can set without re-encoding.
SUPPORTED_CODECS = ("h264",)


class RetimeError(RuntimeError):
    """A rewrite that could not be proven identical; the original is kept."""


def _run(cmd: list, what: str) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True,
                       **ffmpeg_cmd.quiet_popen_kwargs())
    if p.returncode:
        raise RetimeError(f"{what} failed (ffmpeg exit {p.returncode}): "
                          f"{p.stderr.strip()[-400:]}")
    return p.stdout


def packets(mp4: Path) -> tuple[str, list]:
    """(codec, [(duration, is_key, crc), ...]) for every packet, without decoding."""
    out = _run([ffmpeg_cmd.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel",
                "error", "-i", str(mp4), "-map", "0:v:0", "-c", "copy", "-f",
                "framecrc", "-"], f"reading the packets of {mp4.name}")
    codec, rows = "", []
    for line in out.splitlines():
        if line.startswith("#codec_id 0:"):
            codec = line.split(":", 1)[1].strip()
        if not line or line.startswith("#"):
            continue
        f = [x.strip() for x in line.split(",")]
        # framecrc prints a flags field only when it differs from "key".
        flags = next((x for x in f[6:] if x.startswith("F=")), None)
        is_key = flags is None or int(flags[2:], 16) & 1 == 1
        rows.append((int(f[3]), is_key, f[5]))
    return codec, rows


def _keyframe_md5s(mp4: Path) -> list:
    out = _run([ffmpeg_cmd.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel",
                "error", "-skip_frame", "nokey", "-i", str(mp4), "-map", "0:v:0",
                "-fps_mode", "passthrough", "-f", "framemd5", "-"],
               f"decoding the keyframes of {mp4.name}")
    return [l.split(",")[-1].strip() for l in out.splitlines()
            if l and not l.startswith("#")]


def _frame_md5s(mp4: Path) -> list:
    out = _run([ffmpeg_cmd.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel",
                "error", "-i", str(mp4), "-map", "0:v:0", "-fps_mode",
                "passthrough", "-f", "framemd5", "-"], f"decoding {mp4.name}")
    return [l.split(",")[-1].strip() for l in out.splitlines()
            if l and not l.startswith("#")]


def needs_retime(mp4: Path) -> bool:
    """True when the video's frames do not all last the same time."""
    _, rows = packets(mp4)
    return len({d for d, _, _ in rows}) > 1


def retime_video(mp4: Path, fps: float, check_only: bool = False,
                 full_check: bool = False) -> str:
    """Rewrite one mp4 so every frame lasts 1/fps. Returns what was done.

    RULE: the original is replaced only when the rewrite holds the same number
    of frames, every frame lasts one period, every non-key packet is
    byte-identical and every keyframe decodes to the same picture. REASON:
    the rewrite changes only the stream's timing information, and those four
    together mean every decoded picture is unchanged. `full_check` also
    decodes every frame of both files and compares them.
    """
    mp4 = Path(mp4)
    codec, before = packets(mp4)
    if codec not in SUPPORTED_CODECS:
        return f"skipped: codec {codec or 'unknown'} (only {', '.join(SUPPORTED_CODECS)})"
    if len({d for d, _, _ in before}) <= 1:
        return "unchanged: every frame already lasts one period"
    if check_only:
        durs = sorted({d for d, _, _ in before})
        return f"needs retiming: frame durations {durs} (in the file's time base)"
    rate = Fraction(fps).limit_denominator(1001)
    stream = mp4.with_name(mp4.stem + ".retime.h264")
    out = mp4.with_name(mp4.stem + ".retime.mp4")
    ff = ffmpeg_cmd.ffmpeg_exe()
    try:
        # A frame is two fields in H.264's timing information, so the tick
        # rate is twice the frame rate.
        _run([ff, *ffmpeg_cmd.global_args(), "-i", str(mp4), "-map", "0:v:0",
              "-c:v", "copy", "-bsf:v",
              f"h264_metadata=tick_rate={rate.numerator * 2}/{rate.denominator}",
              "-f", "h264", str(stream)], f"retagging {mp4.name}")
        # The capture's own remux (encode_worker), so the result is what a
        # recording made today would be.
        _run([ff, *ffmpeg_cmd.global_args(), "-fflags", "+genpts", "-r",
              f"{rate.numerator}/{rate.denominator}", "-i", str(stream),
              *ffmpeg_cmd.stream_copy_args(), str(out)], f"remuxing {mp4.name}")
        _, after = packets(out)
        problems = []
        if len(after) != len(before):
            problems.append(f"{len(after)} frames instead of {len(before)}")
        if len({d for d, _, _ in after}) != 1:
            problems.append(f"frame durations still differ: "
                            f"{sorted({d for d, _, _ in after})}")
        changed = sum(1 for (_, k1, c1), (_, k2, c2) in zip(before, after)
                      if not (k1 or k2) and c1 != c2)
        if changed:
            problems.append(f"{changed} non-key packets differ")
        if not problems:
            k1, k2 = _keyframe_md5s(mp4), _keyframe_md5s(out)
            if k1 != k2 or not k1:
                problems.append(f"keyframes decode differently "
                                f"({sum(a != b for a, b in zip(k1, k2))} of {len(k1)})")
        if not problems and full_check and _frame_md5s(mp4) != _frame_md5s(out):
            problems.append("decoded frames differ")
        if problems:
            raise RetimeError(f"{mp4.name}: rewrite not proven identical, "
                              f"original kept: {'; '.join(problems)}")
        os.replace(out, mp4)
        return (f"retimed: {len(after)} frames, each 1/{fps:g} s, pictures "
                f"identical")
    finally:
        for p in (stream, out):
            try:
                p.unlink()
            except FileNotFoundError:
                pass


def acquisitions(root: Path) -> list:
    """Every acquisition folder at or below `root`: one holding cam*/ with an mp4."""
    root = Path(root)
    found = set()
    for mp4 in root.rglob("*.mp4"):
        cam = mp4.parent
        if cam.name.startswith("cam") and ".retime" not in mp4.name:
            found.add(cam.parent)
    return sorted(found)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("folders", nargs="+", type=Path)
    ap.add_argument("--check", action="store_true",
                    help="report which videos need retiming, change nothing")
    ap.add_argument("--full-check", action="store_true",
                    help="also decode every frame of both files and compare")
    ap.add_argument("--fps", type=float, default=None,
                    help="frame rate, when no session_metadata.json records it")
    a = ap.parse_args(argv)
    failed = 0
    for folder in a.folders:
        acqs = acquisitions(folder)
        if not acqs:
            print(f"{folder}: no acquisition folder (cam*/ with an mp4) found")
            failed += 1
        for acq in acqs:
            fps = a.fps or recording_meta.acquisition_params(acq)["acq_fps"]
            if not fps:
                print(f"{acq}: no frame rate recorded; pass --fps")
                failed += 1
                continue
            for cam in sorted((d for d in acq.iterdir() if d.is_dir()),
                              key=lambda d: recording_meta.camera_sort_key(d.name)):
                for mp4 in sorted(cam.glob("*.mp4")):
                    if ".retime" in mp4.name:
                        continue
                    try:
                        what = retime_video(mp4, fps, a.check, a.full_check)
                    except RetimeError as e:
                        what, failed = f"FAILED: {e}", failed + 1
                    print(f"{mp4}: {what}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
