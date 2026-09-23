"""Block-ID alignment core, shared by the GUI (align_worker) and 2_align.py.

The host or the network occasionally drops a frame, so frame *i* is not the
same trigger across cameras. Each camera's ``blockids.npy`` records the block
ID (trigger ordinal) of every recorded frame; the block IDs common to all the
aligned cameras are the triggers every one of them captured, and the hardware
trigger fires all cameras at once, so those frames are a synchronized,
equal-length set.

A camera that stopped early (retired, or a truncated tail), started late or
stopped for a while mid-recording (a stall that recovered) holds only part of
the recording, and the common set is then cut to its frames. Replacing the
videos would destroy the other cameras' only copies of everything outside it,
so ``align_recording`` refuses to replace while such a camera takes part,
unless the caller excludes it (its files stay as recorded) or asks for
``truncate_to_shortest``.

Imports are numpy, (lazily) imageio-ffmpeg and gui_app modules without Qt,
so this module is importable from the GUI and from the ``2_align.py`` CLI
alike. Decoding goes through the bundled ffmpeg rather than
OpenCV: a mono source decodes straight to one gray plane instead of a BGR
triple that is converted back per frame.
"""
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

# BLOCKID_WRAP is defined once, in frame_sync, which unwraps the same IDs live.
from gui_app.frame_sync import (BLOCK_RATE_MIN_FRAMES, BLOCK_RATE_MIN_SECONDS,
                                BLOCKID_WRAP)
from gui_app.frame_sync import block_rate_warnings as _block_rate_warnings
from gui_app import ffmpeg_cmd
from gui_app.recording_meta import camera_sort_key

# Name of the per-camera re-encode target. It sits beside the real mp4 while
# ffmpeg writes it, so every mp4 lookup must exclude it and every run must
# clear a stale one left by an interrupted predecessor.
ALIGN_TMP_NAME = "aligned_tmp.mp4"

# Below this size an "mp4" cannot hold a moov atom plus one frame, so a file
# this small is a failed encode whatever ffmpeg's exit status said.
MIN_MP4_BYTES = 1024

# Decoder output options. RULE: decode every coded frame exactly once, in
# order. REASON: the alignment maps decoded frame n to blockids.npy[n], and
# ffmpeg's default sync for a pipe output is constant frame rate, which
# duplicates or drops frames wherever the container's timestamps are uneven
# (a video re-muxed by another tool). Every later frame then maps to the
# wrong trigger while the frame count can still look right.
DECODE_OUTPUT_ARGS = ("-fps_mode", "passthrough")

# Written beside frametimes.npy when a replace pass had to synthesise it from
# block IDs (the original was missing or described other frames). Timestamps
# made from block IDs advance at exactly the configured rate, so the
# block-rate check would measure its own reference and always pass; the
# marker makes every later check report that camera as unverifiable instead.
# The original, when there was one, is kept as SYNTH_ORIGINAL.
SYNTH_MARKER = "frametimes_synthesized.json"
SYNTH_ORIGINAL = "frametimes.orig.npy"


def _unwrap_blockids(b: np.ndarray, period: int = BLOCKID_WRAP) -> np.ndarray:
    """Undo 16-bit block-ID wrap-around so IDs are globally monotonic.

    GigE Vision 16-bit block IDs cycle through 1..65535 (0 is reserved "no
    block id"), so they wrap 65535 -> 1 every 65535 triggers unless extended
    64-bit IDs are enabled. The camera backend tries to enable the 64-bit
    mode; this unwraps a stream that wrapped anyway, including recordings made
    before the 64-bit mode was set.

    All cameras are hardware-triggered together and start at block ID 1, so they
    wrap at the same trigger; unwrapping each stream independently yields trigger
    ordinals that stay consistent across cameras. A wrap is a large negative step
    (~ -(period-1)); a small decrease means genuinely corrupt/reordered data.

    IDs at or below zero are rejected before the wrap test: 0 is the GigE
    "no block id" value and -1 is the grab thread's "could not read the block
    ID" sentinel. Above ID 32767 the -1 sentinel would otherwise read as a wrap
    and shift every later frame of that camera by one period, which empties the
    cross-camera intersection and truncates the recording under --replace.
    """
    b = np.asarray(b).astype(np.int64)
    bad = b <= 0
    if np.any(bad):
        raise ValueError(
            f"{int(bad.sum())} non-positive block ID(s) (0 is reserved, -1 "
            f"means the camera did not report one); first at index "
            f"{int(np.argmax(bad))}")
    if b.size < 2:
        return b
    d = np.diff(b)
    wrap_at = d < -(period // 2)
    if wrap_at.any():
        offsets = np.concatenate([[0], np.cumsum(wrap_at.astype(np.int64))]) * period
        b = b + offsets
    if np.any(np.diff(b) <= 0):
        raise ValueError("block IDs not monotonic even after wrap-unwrap "
                         "(corrupt or reordered)")
    return b


def camera_dirs(rec_dir: Path) -> list[Path]:
    """The cam*/ directories of an acquisition, in camera-number order."""
    return sorted((d for d in Path(rec_dir).iterdir()
                   if d.is_dir() and d.name.startswith("cam")),
                  key=lambda d: camera_sort_key(d.name))


def video_for(cam_dir: Path, acq_type: str | None = None):
    """The one mp4 that is this camera's recording, or None.

    With ``acq_type`` (``"recording"`` or ``"calibration"``) the candidates
    are the mp4s whose name ends in ``-<acq_type>.mp4``, which is how every
    writer names its output (``<date>-<session>-<cam>-<acq_type>.mp4``).
    Without it a name ending in ``-recording.mp4`` is preferred, and any mp4
    is accepted when none does. The suffix, not a substring, decides: a
    session id may itself contain "recording" or "calibration", and a
    substring match would then take the other acquisition's video. The
    re-encode scratch file is never a candidate, and candidates are
    sorted, so the answer does not depend on directory enumeration order.
    More than one candidate is an error rather than a guess: under --replace
    the wrong pick would be re-encoded while the real recording stayed the
    superset.
    """
    cam_dir = Path(cam_dir)
    mp4s = sorted(f for f in cam_dir.iterdir()
                  if f.suffix == ".mp4" and f.name != ALIGN_TMP_NAME
                  and f.is_file())
    if acq_type is not None:
        cands = [f for f in mp4s if f.name.endswith(f"-{acq_type}.mp4")]
    else:
        cands = [f for f in mp4s if f.name.endswith("-recording.mp4")] or mp4s
    if len(cands) > 1:
        raise ValueError(f"{cam_dir}: {len(cands)} mp4 candidates, cannot tell "
                         f"which is the recording: "
                         f"{', '.join(f.name for f in cands)}")
    return cands[0] if cands else None


def _exclusions(exclude) -> dict:
    """``exclude`` as a dict of camera name -> reason.

    Accepts a mapping (the reasons are kept) or an iterable of names.
    """
    if not exclude:
        return {}
    if isinstance(exclude, dict):
        return {str(k): str(v) for k, v in exclude.items()}
    if isinstance(exclude, str):
        exclude = [exclude]
    return {str(nm): "excluded by the caller" for nm in exclude}


def _load_cameras(rec_dir: Path, excluded: dict):
    """(names, blocks, videos, empty) for the cameras not in ``excluded``.

    ``empty`` maps each camera that recorded no frames to the reason. A
    camera directory without ``blockids.npy`` is a camera that recorded no
    frames when another camera has one (a camera retired before its first
    frame writes none); when no camera has one, the recording predates
    block-ID logging.
    """
    cam_dirs = camera_dirs(rec_dir)
    if not cam_dirs:
        raise FileNotFoundError(f"No cam*/ directories in {rec_dir}")
    present = [cd.name for cd in cam_dirs]
    unknown = sorted(set(excluded) - set(present), key=camera_sort_key)
    if unknown:
        raise ValueError(f"cannot exclude {', '.join(unknown)}: {rec_dir} has "
                         f"no such camera directory (it has "
                         f"{', '.join(present)})")
    if not any((cd / "blockids.npy").exists() for cd in cam_dirs):
        raise FileNotFoundError(
            f"no camera directory in {rec_dir} has a blockids.npy: the "
            f"recording predates block-ID logging and cannot be block-ID "
            f"aligned.")
    names, blocks, videos, empty = [], [], [], {}
    for cd in cam_dirs:
        if cd.name in excluded:
            continue
        bpath = cd / "blockids.npy"
        if not bpath.exists():
            empty[cd.name] = ("recorded no frames: it has no blockids.npy "
                              "while other cameras do")
            continue
        b = np.load(bpath)
        if b.ndim != 1:
            raise ValueError(f"{bpath}: expected 1-D block IDs, got {b.shape}")
        if b.size == 0:
            empty[cd.name] = "recorded no frames: its blockids.npy is empty"
            continue
        try:
            b = _unwrap_blockids(b)  # undo 16-bit wrap: globally monotonic IDs
        except ValueError as e:
            raise ValueError(f"{bpath}: {e}") from e
        names.append(cd.name)
        blocks.append(b)
        videos.append(video_for(cd))
    if not names:
        if empty:
            raise ValueError(
                "no camera recorded any frames: "
                + "; ".join(f"{nm} {why}" for nm, why in empty.items()))
        raise ValueError(f"every camera in {rec_dir} is excluded")
    return names, blocks, videos, empty


def load_blockids(rec_dir: Path):
    """Return (cam_names, [blockids], [video paths]) for every camera.

    Raises when a camera recorded no frames, naming it: the recording has no
    frame common to every camera, and aligning would cut every other camera
    to nothing. ``Analysis(exclude=...)`` is how a caller aligns the others.
    """
    names, blocks, videos, empty = _load_cameras(Path(rec_dir), {})
    if empty:
        raise ValueError(
            "; ".join(f"{nm} {why}" for nm, why in empty.items())
            + ". Such a camera cannot be aligned, and the recording has no "
              "frames common to every camera; exclude it to align the others.")
    return names, blocks, videos


def _rate_judgeable(b: np.ndarray, ts: np.ndarray, fps: int):
    """Why frame_sync.check_block_id_rate would abstain on this camera, or None.

    The same limits as the check itself, so a camera listed as checked is
    one the check really judged.
    """
    if fps <= 0:
        return "no reference rate"
    if b.size < BLOCK_RATE_MIN_FRAMES:
        return (f"too short to judge ({b.size} frames; the check needs "
                f"{BLOCK_RATE_MIN_FRAMES} over {BLOCK_RATE_MIN_SECONDS:g} s)")
    dur = float(ts[b.size - 1]) - float(ts[0])
    if dur < BLOCK_RATE_MIN_SECONDS or b[-1] <= b[0]:
        return (f"too short to judge ({dur:.2f} s of device time; the check "
                f"needs {BLOCK_RATE_MIN_SECONDS:g} s)")
    return None


def block_rate_check(rec_dir: Path, names, blocks, fps: int):
    """Check each camera's block IDs really are trigger ordinals.

    Returns ``(warnings, checked, skipped)``: the warning texts, the cameras
    the check judged, and a dict of camera -> the reason it was not judged.
    A summary that lists no camera as checked has verified nothing, and the
    caller must be able to say so.

    The intersection below matches on block ID and nothing else, so it is only
    an alignment if every camera consumed one block ID per trigger. A camera
    that ignored triggers (exposure over the ceiling) still writes gapless
    block IDs and still ends up with the same frame count as everyone else, so
    this pass would report "already aligned" on a recording that is skewed by
    seconds. ``frame_sync.check_block_id_rate`` catches it by comparing the
    block-ID counter against the camera's own device clock.

    Timestamps come from ``frametimes.npy`` row 1 (device seconds, shifted to
    start at zero). A camera without usable timestamps is skipped, not failed,
    so older recordings still align, and its reason is reported. A camera
    whose timestamps an earlier replace pass synthesised (``SYNTH_MARKER``)
    is reported as unverifiable.
    """
    rec_dir = Path(rec_dir)
    ids, times, checked, skipped = [], [], [], {}
    for nm, b in zip(names, blocks):
        cam = rec_dir / nm
        ft_path = cam / "frametimes.npy"
        if (cam / SYNTH_MARKER).exists():
            skipped[nm] = ("unverifiable: its frametimes.npy was synthesised "
                           "from block IDs by an earlier alignment, so it "
                           "holds no device times")
            continue
        if not ft_path.exists():
            skipped[nm] = "no frametimes.npy"
            continue
        try:
            ft = np.load(ft_path)
        except Exception as e:
            skipped[nm] = f"frametimes.npy unreadable: {e}"
            continue
        if ft.ndim != 2 or ft.shape[0] < 2 or ft.shape[1] < b.size:
            skipped[nm] = (f"frametimes.npy has shape {ft.shape}, not "
                           f"(2, {b.size})")
            continue
        why = _rate_judgeable(b, ft[1], fps)
        if why:
            skipped[nm] = why
            continue
        ids.append(b)
        times.append(ft[1])
        checked.append(nm)
    warnings = _block_rate_warnings(ids, times, fps, checked) if checked else []
    return warnings, checked, skipped


def compute_alignment(blocks: list[np.ndarray]):
    """Common block IDs across all cameras + each camera's frame indices.

    frame_index[c, k] = position in camera c's video of common_ids[k].
    """
    common = blocks[0]
    for b in blocks[1:]:
        common = np.intersect1d(common, b, assume_unique=True)
    frame_index = np.empty((len(blocks), common.size), dtype=np.int64)
    for c, b in enumerate(blocks):
        pos = np.searchsorted(b, common)
        if pos.size and np.any(b[pos] != common):
            raise RuntimeError(f"camera {c}: block-ID index mismatch")
        frame_index[c] = pos
    return common, frame_index


def needs_alignment(blocks: list[np.ndarray]) -> bool:
    """True iff some camera holds a frame another camera is missing."""
    common, _ = compute_alignment(blocks)
    return any(b.size > common.size for b in blocks)


def _short_cameras(names, blocks, margin: int) -> dict:
    """camera -> reason, for each camera that holds only part of the recording.

    A camera ended early when its last block ID is more than ``margin``
    triggers before the latest last block ID of any camera, and started late
    when its first is more than ``margin`` after the earliest first. It
    stopped mid-recording when, between two of its consecutive frames, the
    other cameras recorded more than ``margin`` triggers it has no frame for
    (a stall that re-armed and resynced). Cameras triggered together end
    within a few frames of each other and drop frames a few at a time, so a
    larger gap is a camera that stopped recording, not one that dropped
    frames, and the common set loses that stretch from every camera.
    """
    if len(blocks) < 2:
        return {}
    last_max = max(int(b[-1]) for b in blocks)
    first_min = min(int(b[0]) for b in blocks)
    union = np.unique(np.concatenate(blocks))
    out = {}
    for nm, b in zip(names, blocks):
        why = []
        early = last_max - int(b[-1])
        late = int(b[0]) - first_min
        if early > margin:
            why.append(f"ended early: its last trigger is {int(b[-1])}, "
                       f"{early} before the last one another camera "
                       f"recorded ({last_max})")
        if late > margin:
            why.append(f"started late: its first trigger is {int(b[0])}, "
                       f"{late} after the first one another camera recorded "
                       f"({first_min})")
        if b.size >= 2:
            # Triggers in the union strictly between consecutive frames of
            # this camera: every one is a trigger another camera recorded.
            between = np.diff(np.searchsorted(union, b)) - 1
            k = int(np.argmax(between))
            if int(between[k]) > margin:
                why.append(f"stopped mid-recording: other cameras recorded "
                           f"{int(between[k])} triggers between its frames at "
                           f"triggers {int(b[k])} and {int(b[k + 1])}")
        if why:
            out[nm] = "; ".join(why)
    return out


class Analysis:
    """Everything the read-only half of an alignment knows about a recording.

    Built once per run so the CLI's pre-flight table, the dry-run report and
    ``align_recording`` all print from the same numbers instead of each
    loading and intersecting the block IDs again.

    ``exclude`` (camera names, or a dict of name -> reason) leaves cameras
    out of the intersection; their files are never touched. ``short_cams``
    maps each remaining camera that ended early, started late, stopped
    mid-recording or recorded no frames to the reason, with ``short_margin``
    triggers of tolerance (default one second of triggers).
    """

    def __init__(self, rec_dir, fps: int, exclude=(), short_margin=None):
        self.rec_dir = Path(rec_dir)
        self.fps = int(fps)
        self.excluded = _exclusions(exclude)
        (self.names, self.blocks, self.videos,
         self.empty_cams) = _load_cameras(self.rec_dir, self.excluded)
        self.short_margin = (int(short_margin) if short_margin is not None
                             else max(1, self.fps))
        self.common, self.frame_index = compute_alignment(self.blocks)
        self.full_span = int(max(int(b[-1]) for b in self.blocks)
                             - min(int(b[0]) for b in self.blocks) + 1)
        self.needed = any(b.size > self.common.size for b in self.blocks)
        short = _short_cameras(self.names, self.blocks, self.short_margin)
        short.update(self.empty_cams)
        self.short_cams = dict(sorted(short.items(),
                                      key=lambda kv: camera_sort_key(kv[0])))
        # Runs on every path, including the ones that report "already aligned":
        # a camera ignoring triggers keeps its block IDs gapless, so the
        # intersection is total and nothing else here looks wrong.
        (self.rate_warnings, self.rate_checked,
         self.rate_skipped) = block_rate_check(self.rec_dir, self.names,
                                               self.blocks, self.fps)
        for nm in self.excluded:
            self.rate_skipped[nm] = "excluded from this alignment"
        for nm in self.empty_cams:
            self.rate_skipped[nm] = "recorded no frames"

    def per_camera(self) -> dict:
        return {nm: dict(recorded=int(b.size),
                         dropped=int(self.full_span - b.size),
                         first_trigger=int(b[0]), last_trigger=int(b[-1]))
                for nm, b in zip(self.names, self.blocks)}

    def summary(self) -> dict:
        """The public, JSON-serialisable view; no arrays."""
        return dict(recording=str(self.rec_dir), camera_names=list(self.names),
                    trigger_span=self.full_span,
                    common_frames=int(self.common.size), needed=self.needed,
                    per_camera=self.per_camera(),
                    excluded=dict(self.excluded),
                    short_cams=dict(self.short_cams),
                    short_margin=self.short_margin,
                    rate_warnings=list(self.rate_warnings),
                    rate_checked=list(self.rate_checked),
                    rate_skipped=dict(self.rate_skipped))


def analyse(rec_dir, fps: int = 100, exclude=(), short_margin=None) -> Analysis:
    return Analysis(rec_dir, fps, exclude=exclude, short_margin=short_margin)


def _them(names) -> str:
    return "it" if len(names) == 1 else "them"


def refusal_reason(an: Analysis, truncate_to_shortest: bool = False):
    """Why a replace of this analysis must not run, or None when it may.

    A replace re-encodes every aligned camera down to the common triggers and
    overwrites its only copy. That destroys data when the common set is cut
    short by one camera, so a camera that ended early, started late or
    stopped mid-recording blocks the replace unless the caller asked for
    ``truncate_to_shortest``, and a
    camera with no frames, or a recording with no common trigger, blocks it
    always. The text names the cameras and the remedy.
    """
    if an.empty_cams:
        names = list(an.empty_cams)
        return ("Not replacing any video: "
                + "; ".join(f"{nm} {why}" for nm, why in an.empty_cams.items())
                + ". Aligning with a camera that has no frames would leave "
                  "every video empty. Run 2_align.py with --exclude "
                + ",".join(names) + " to align the other cameras and leave "
                + _them(names) + " as recorded.")
    if an.needed and an.common.size == 0:
        return ("Not replacing any video: no trigger is common to every "
                "camera, so every aligned video would be empty. Exclude the "
                "cameras that do not overlap the others.")
    short = {nm: why for nm, why in an.short_cams.items()
             if nm not in an.empty_cams}
    if an.needed and short and not truncate_to_shortest:
        names = list(short)
        return ("Not replacing any video: "
                + "; ".join(f"{nm} {why}" for nm, why in short.items())
                + f". Aligning every camera to {_them(names)} would cut the "
                  f"others to the {an.common.size} of {an.full_span} triggers "
                  f"they share. Run 2_align.py with --exclude "
                + ",".join(names) + " to align the other cameras and leave "
                + _them(names) + " as recorded, or with --truncate-to-shortest "
                  f"to cut every camera to the {an.common.size} common "
                  f"triggers.")
    return None


def _ffmpeg_exe() -> str:
    return ffmpeg_cmd.ffmpeg_exe()


def _open_gray_reader(video: Path):
    """Decode a video to one gray plane per frame with the bundled ffmpeg.

    Returns ``(w, h, frames)`` where ``frames`` yields ``w*h`` bytes per frame
    and must be ``close()``d by the caller so the decoder process is reaped.
    Kept as a seam so tests can feed synthetic frames without a real mp4.
    """
    from imageio_ffmpeg import read_frames
    gen = read_frames(str(video), pix_fmt="gray", bits_per_pixel=8,
                      output_params=list(DECODE_OUTPUT_ARGS))
    meta = next(gen)
    w, h = meta["size"]
    return int(w), int(h), gen


def _log_tail(path: Path, n: int = 2000) -> str:
    try:
        return Path(path).read_text(errors="replace")[-n:].strip()
    except OSError:
        return ""


def extract_aligned(video: Path, frame_idx: np.ndarray, dst: Path,
                    fps: int, quality: int, backend: str | None = None,
                    stop=None, err_log: Path | None = None,
                    expected_frames: int | None = None) -> int:
    """Re-encode only the selected frame indices, in order, into dst (gray).

    Raises unless ffmpeg exited 0 AND ``dst`` exists with a plausible size AND
    every selected frame was decoded and piped. The caller replaces the only
    copy of a camera's recording with ``dst``, so "frames were piped" is not
    good enough: a muxer error, disk-full during the ``+faststart`` rewrite or
    an encoder flush error all exit non-zero after consuming every input byte.

    With ``expected_frames`` (the length of the camera's ``blockids.npy``)
    the whole source is decoded, and a source holding any other number of
    frames raises. Frame n of the video is block ID n only while the two
    counts agree, so a mismatch means the selected indices would pick frames
    from the wrong triggers.

    ffmpeg's stderr goes to ``align_error.log`` beside ``dst`` (removed on
    success) because the GUI runs under pythonw, where inherited stderr has
    nowhere to go. ``stop()`` returning True aborts the encode.
    """
    video, dst = Path(video), Path(dst)
    frame_idx = np.asarray(frame_idx, dtype=np.int64)
    keep = set(int(i) for i in frame_idx)
    last = int(frame_idx[-1]) if frame_idx.size else -1
    err_log = Path(err_log) if err_log else dst.with_name("align_error.log")
    count_all = expected_frames is not None

    w, h, frames = _open_gray_reader(video)
    cmd = [_ffmpeg_exe(), *ffmpeg_cmd.global_args("error"),
           *ffmpeg_cmd.rawvideo_input_args(w, h, fps), "-i", "-",
           *ffmpeg_cmd.h264_encoder_args(fps, quality, backend),
           *ffmpeg_cmd.mp4_container_args(), str(dst)]
    n = written = 0
    stopped = False
    try:
        with open(err_log, "wb") as err:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL, stderr=err,
                                    **ffmpeg_cmd.quiet_popen_kwargs())
            try:
                for frame in frames:
                    if n > last and not count_all:
                        break
                    if stop is not None and stop():
                        stopped = True
                        proc.kill()
                        break
                    if n in keep:
                        proc.stdin.write(frame)
                        written += 1
                        if n == last:
                            # Every selected frame is in; the encoder can
                            # finish while the rest of the source is counted.
                            proc.stdin.close()
                    n += 1
            except OSError:
                # ffmpeg died mid-stream; the exit-status check below reports
                # its stderr. BrokenPipeError (EPIPE) is what POSIX raises;
                # Windows raises a plain OSError with EINVAL for a pipe whose
                # reader has exited, so catching only BrokenPipeError there
                # replaces the diagnosis with "[Errno 22] Invalid argument".
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                proc.wait()
    finally:
        frames.close()

    if stopped:
        raise RuntimeError("stopped before the re-encode finished")
    size = dst.stat().st_size if dst.exists() else 0
    if proc.returncode != 0 or size < MIN_MP4_BYTES:
        tail = _log_tail(err_log)
        raise RuntimeError(
            f"ffmpeg exited {proc.returncode}, output {size} bytes"
            + (f": {tail}" if tail else ""))
    if written != frame_idx.size:
        raise RuntimeError(
            f"decoded {written}/{frame_idx.size} selected frames (source has "
            f"{n} frames, index needs up to {last + 1})")
    if count_all and n != expected_frames:
        raise RuntimeError(
            f"the video decodes to {n} frames but blockids.npy lists "
            f"{expected_frames}, so its frame numbers are not its block IDs; "
            f"original kept")
    err_log.unlink(missing_ok=True)
    return written


def _save_atomic(path: Path, arr: np.ndarray) -> None:
    """np.save via a sibling .npy and os.replace, so a reader never sees a
    half-written file and a failure leaves the original untouched."""
    path = Path(path)
    tmp = path.with_name(path.stem + ".tmp.npy")
    np.save(tmp, arr)
    os.replace(tmp, path)


def _rewrite_metadata(cam_dir: Path, cam_blockids: np.ndarray,
                      frame_idx: np.ndarray, common: np.ndarray,
                      fps: int) -> bool:
    """Replace blockids.npy + frametimes.npy with the aligned (common) set.

    Returns True when the new frametimes.npy holds synthesised timestamps.

    Both arrays are computed first and written atomically afterwards, so a
    failure between the two files cannot leave one describing the aligned set
    and the other the original. When the original timestamps do not describe
    this camera's frames (missing, or another length), they are synthesised
    from the uniform hardware trigger. The original is then kept as
    ``SYNTH_ORIGINAL``, and ``SYNTH_MARKER`` is written before frametimes.npy,
    so no reader ever sees synthesised times without the marker.
    """
    ft_path = cam_dir / "frametimes.npy"
    marker = cam_dir / SYNTH_MARKER
    m = common.size
    frame_nums = np.arange(1, m + 1, dtype=np.float64)
    ts = None
    ft = None
    if ft_path.exists():
        ft = np.load(ft_path)
        if (ft.ndim == 2 and ft.shape[0] >= 2
                and ft.shape[1] == cam_blockids.size):
            ts = ft[1][frame_idx].astype(np.float64)
            ts = ts - ts[0]
    synthesized = ts is None or marker.exists()
    if ts is None:
        ts = (common - common[0]).astype(np.float64) / float(fps)
        original = None
        if ft is not None:
            original = SYNTH_ORIGINAL
            if not (cam_dir / SYNTH_ORIGINAL).exists():
                _save_atomic(cam_dir / SYNTH_ORIGINAL, ft)
        why = ("frametimes.npy was missing" if ft is None else
               f"frametimes.npy had shape {ft.shape}, which does not describe "
               f"the camera's {cam_blockids.size} frames")
        if not marker.exists():
            tmp = marker.with_name(marker.name + ".tmp")
            tmp.write_text(json.dumps({
                "reason": f"{why}; timestamps synthesised as "
                          f"(blockid - first blockid) / {fps}",
                "original": original}, indent=2))
            os.replace(tmp, marker)
    _save_atomic(cam_dir / "blockids.npy", common.astype(np.int64))
    _save_atomic(ft_path, np.stack([frame_nums, ts]))
    return synthesized


def _write_index(out: Path, an: Analysis, frame_index: np.ndarray,
                 replaced_flags: list, manifest: dict) -> None:
    out.mkdir(exist_ok=True)
    np.savez(out / "alignment.npz", common_block_ids=an.common,
             frame_index=frame_index, camera_names=np.array(an.names),
             video_is_common=np.array(replaced_flags, dtype=bool))
    with open(out / "alignment.json", "w") as f:
        json.dump(manifest, f, indent=2)


def clear_stale_tmp(rec_dir: Path) -> list[Path]:
    """Remove ``aligned_tmp.mp4`` left behind by an interrupted run.

    Called before the video lookup so the scratch file of a killed
    predecessor can neither be mistaken for a recording nor block this run's
    own ``os.replace``.
    """
    removed = []
    for cd in camera_dirs(rec_dir):
        p = cd / ALIGN_TMP_NAME
        if p.exists():
            p.unlink()
            removed.append(p)
    return removed


def align_recording(rec_dir, fps: int = 100, quality: int = 21,
                    replace: bool = False, parallel: int = 3,
                    progress=None, backend: str | None = None,
                    should_stop=None, analysis: Analysis | None = None,
                    exclude=(), truncate_to_shortest: bool = False,
                    short_margin=None) -> dict:
    """Align a recording by block ID.

    Always writes ``aligned/alignment.{npz,json}`` (the lossless index). When
    ``replace`` and some camera has extra frames, re-encodes each camera's mp4
    down to the common frames, atomically replaces the original, and only then
    rewrites that camera's blockids.npy + frametimes.npy to match — video
    first, because a locked mp4 (open in a player) must not leave metadata
    describing frames the video does not have.

    ``exclude`` (names, or a dict of name -> reason) leaves cameras out: they
    take no part in the intersection and their files are not touched. A
    replace is refused while ``short_cams`` is non-empty (see
    ``refusal_reason``) unless ``truncate_to_shortest``; a refused replace
    changes no video, writes the index only, and reports the reason in
    ``refused`` and in ``failures``/``warnings``.

    Every camera is attempted and every outcome recorded: the summary carries
    ``failures`` (per-camera messages), ``replaced_cams``, ``failed_cams`` and
    ``replaced`` (True only when alignment was needed and NO camera failed).
    Block-rate warnings are informational and live in ``rate_warnings``; they
    never make a replaced recording report itself as kept, because the GUI
    regenerates stim_trace.csv from ``replaced``. ``warnings`` is the display
    list, rate warnings followed by failures.

    The index is written after the replacement pass, with ``frame_index`` for
    a replaced camera set to ``arange(common.size)`` and ``video_is_common``
    marking it, so a consumer cannot apply a pre-replacement index to a video
    that is now the common set. The index is derived data; the summary is the
    record of what happened to the videos. A failure writing it is therefore
    reported in ``index_error`` (and appended to ``failures``/``warnings``)
    instead of raised, because raising after the videos were replaced would
    make the caller report them as left as-is.

    ``analysis`` lets a caller that already built the ``Analysis`` for this
    recording (the CLI prints its table from one) pass it in, so the block
    IDs are loaded and intersected once per run; it must describe the same
    recording at the same fps with the same exclusions.

    ``progress(done, total, msg)`` is called as cameras complete (thread-safe).
    ``should_stop()`` returning True skips cameras not yet started and aborts
    the one in flight, each recorded as a failure with its original kept.
    """
    rec_dir = Path(rec_dir)
    clear_stale_tmp(rec_dir)
    if analysis is None:
        an = analyse(rec_dir, fps, exclude=exclude, short_margin=short_margin)
    else:
        an = analysis
        if Path(an.rec_dir).resolve() != rec_dir.resolve() or an.fps != int(fps):
            raise ValueError(
                f"analysis describes {an.rec_dir} at {an.fps} fps, not "
                f"{rec_dir} at {fps} fps")
        if set(an.excluded) != set(_exclusions(exclude)):
            raise ValueError(
                f"analysis describes {an.rec_dir} excluding "
                f"{sorted(an.excluded) or 'no camera'}, not "
                f"{sorted(_exclusions(exclude)) or 'no camera'}")
    names, blocks, videos = an.names, an.blocks, an.videos
    common, frame_index = an.common, an.frame_index
    need = an.needed
    stop = should_stop or (lambda: False)
    refused = refusal_reason(an, truncate_to_shortest) if replace else None

    for msg in an.rate_warnings:
        print(f"[align] WARNING: {msg}", flush=True)
    if refused:
        print(f"[align] WARNING: {refused}", flush=True)

    total = len(names)
    failures: list[str] = []
    replaced_cams: list[str] = []
    failed_cams: list[str] = []
    lock = threading.Lock()
    done = [0]

    def _finish(nm: str, msg: str):
        with lock:
            done[0] += 1
            if progress:
                progress(done[0], total, msg)

    def _fail(nm: str, why: str, msg: str):
        with lock:
            failures.append(f"{nm}: {why}")
            failed_cams.append(nm)
        _finish(nm, msg)

    def _one(c):
        nm, vid = names[c], videos[c]
        tmp = None
        video_replaced = False
        try:
            if stop():
                raise RuntimeError("stopped before re-encode; original kept")
            if vid is None:
                raise FileNotFoundError("no video; metadata left as recorded")
            cam_dir = vid.parent
            tmp = cam_dir / ALIGN_TMP_NAME
            extract_aligned(vid, frame_index[c], tmp, fps, quality,
                            backend=backend, stop=stop,
                            expected_frames=int(blocks[c].size))
            os.replace(tmp, vid)  # atomic; original disjoint mp4 replaced
            video_replaced = True
            _rewrite_metadata(cam_dir, blocks[c], frame_index[c], common, fps)
        except Exception as e:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
            if video_replaced:
                # The video is the common set but the metadata may not be.
                # Say so explicitly: this camera needs its metadata fixed by
                # hand, not another re-encode.
                _fail(nm, f"video REPLACED but metadata rewrite failed: {e}",
                      f"{nm} metadata rewrite FAILED")
            else:
                _fail(nm, f"{e}", f"{nm} FAILED, kept original")
            return
        with lock:
            replaced_cams.append(nm)
        _finish(nm, f"{nm} aligned")

    if refused:
        failures.append(refused)
        if progress:
            progress(total, total, "not replaced")
    elif need and replace:
        workers = max(1, min(parallel or total, total))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_one, range(total)))
    elif not need and progress:
        progress(total, total, "already aligned")

    replaced_flags = [nm in replaced_cams for nm in names]
    post_index = frame_index.copy()
    for c, flag in enumerate(replaced_flags):
        if flag:
            post_index[c] = np.arange(common.size, dtype=np.int64)

    summary = an.summary()
    summary.update(
        replaced=bool(need and replace and not failures),
        failures=list(failures), replaced_cams=list(replaced_cams),
        failed_cams=list(failed_cams), stopped=bool(stop()),
        warnings=list(an.rate_warnings) + list(failures),
        index_error=None, refused=refused,
        truncate_to_shortest=bool(truncate_to_shortest),
    )
    for nm, flag in zip(names, replaced_flags):
        summary["per_camera"][nm]["video_is_common"] = bool(flag)
        summary["per_camera"][nm]["frametimes_synthesized"] = (
            rec_dir / nm / SYNTH_MARKER).exists()

    # ``replaced`` is settled above from the camera outcomes alone: the videos
    # and their metadata are already on disk in their final form, so a disk
    # full, a locked alignment.npz or a stray file named "aligned" cannot
    # unmake that, and the caller must still regenerate what derives from the
    # replaced videos.
    manifest = {k: v for k, v in summary.items() if k != "warnings"}
    try:
        _write_index(rec_dir / "aligned", an, post_index, replaced_flags, manifest)
    except Exception as e:
        msg = f"aligned/: index write failed: {e}"
        print(f"[align] WARNING: {msg}", flush=True)
        summary["index_error"] = msg
        summary["failures"].append(msg)
        summary["warnings"].append(msg)
    return summary
