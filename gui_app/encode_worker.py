"""Background worker that turns each camera's capture file into an mp4.

Two source shapes, picked per camera in run():

- ``stream.h264`` (the DEFAULT, real-time encode path): the GPU already encoded
  every frame during capture, so this is a stream-copy remux — seconds, no
  re-encode, no NVENC session. A camera whose encoder died mid-recording also
  has ``raw_tail.bin`` (the frames after the death, raw); that tail is encoded
  to an elementary stream and appended before the remux.
- ``raw.bin`` (``realtime_encode: false``, or a camera whose NVENC init failed,
  or a camera whose stream.h264 is empty): a real H.264 encode of the raw
  mono8 frames.

Each camera is a small job that moves through stages (optional tail encode,
then remux/encode), and every stage is one ffmpeg process scheduled through
the same ``max_parallel`` slots, so NVENC use is bounded and progress moves.
For the raw branch that concurrency is bounded by disk read bandwidth and the
NVENC concurrent-session limit rather than the CPU. The driver sets that
limit and it differs between driver versions, so probe it with
nvenc.probe_max_sessions rather than assuming a number.

``0_encode.py`` runs the same job without the GUI, for an acquisition whose
encode step did not finish.

Every ffmpeg command is assembled from ``gui_app.ffmpeg_cmd`` so ``-g <fps>``
and ``-movflags +faststart`` cannot be dropped from any mp4 this file writes.
"""
import json
import os
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np
from PyQt5.QtCore import QThread, pyqtSignal

from gui_app import ffmpeg_cmd

# Written beside stream.h264 by the capture router when an encoder died and
# frames were spilled raw: {"encoded": k, "spilled": m}. The first k entries
# of blockids.npy/frametimes.npy describe stream.h264; the rest describe
# raw_tail.bin. Without it a failed tail merge cannot say how many frames the
# mp4 holds, and the camera must fail rather than over-claim.
#
# RULE: "encoded" is the CODED-picture count -- the pictures the encoder
# emitted into stream.h264 -- and not the frames fed to Encode(). REASON: an
# encoder that died still accepted frames that were never coded, so the fed
# count over-claims by whatever was in flight, and truncating the metadata to
# it would map the mp4's frames onto the wrong triggers, which is the silent
# failure this file exists to refuse. grab_thread.write_split_point writes
# it. The key is spelled "encoded" so that recordings already on disk keep
# reading correctly; its value is the coded count.
ENCODED_JSON = "encoded.json"

# Below this size a file cannot be an mp4 with a moov atom and one frame, so a
# zero exit code with a smaller output is still a failed encode.
MIN_MP4_BYTES = 1024


def _read_encoded_count(cam_dir: Path):
    """The coded-picture count encoded.json records for stream.h264, or None.

    That is the number of frames the mp4 made from stream.h264 holds, and the
    count the metadata is cut to when the raw tail cannot be merged.
    """
    p = cam_dir / ENCODED_JSON
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        k = int(data["encoded"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return k if k >= 0 else None


def _save_atomic(path: Path, arr: np.ndarray) -> None:
    tmp = path.with_name(path.stem + ".tmp.npy")
    np.save(tmp, arr)
    os.replace(tmp, path)


def _truncate_metadata(cam_dir: Path, k: int) -> None:
    """Cut blockids.npy/frametimes.npy to their first k entries, atomically.

    blockids.npy must only describe frames the mp4 contains; after a failed
    tail merge that is the encoded head, and both arrays are in arrival order
    so the first k entries are exactly those frames. The full arrays are kept
    as ``*.full.npy`` because they are the only record of which triggers the
    frames in raw_tail.bin belong to, which a later manual merge needs.
    """
    bpath = cam_dir / "blockids.npy"
    if bpath.exists():
        b = np.load(bpath)
        if b.ndim == 1 and b.size > k:
            _save_atomic(cam_dir / "blockids.full.npy", b)
            _save_atomic(bpath, b[:k])
    fpath = cam_dir / "frametimes.npy"
    if fpath.exists():
        ft = np.load(fpath)
        if ft.ndim == 2 and ft.shape[1] > k:
            _save_atomic(cam_dir / "frametimes.full.npy", ft)
            _save_atomic(fpath, ft[:, :k])
        elif ft.ndim == 1 and ft.size > k:
            _save_atomic(cam_dir / "frametimes.full.npy", ft)
            _save_atomic(fpath, ft[:k])


def _append_file(dst: Path, src: Path) -> None:
    with open(dst, "ab") as d, open(src, "rb") as s:
        while chunk := s.read(8 << 20):
            d.write(chunk)


class _Job:
    """One camera's path to an mp4. ``stage`` is 'tail', 'remux' or 'encode'."""

    def __init__(self, idx: int, cam: str, cam_dir: Path, src: Path,
                 mp4_path: Path, n_frames: int, stage: str):
        self.idx = idx
        self.cam = cam
        self.cam_dir = cam_dir
        self.src = src
        self.mp4_path = mp4_path
        self.n_frames = n_frames
        self.stage = stage
        self.tail_bin = cam_dir / "raw_tail.bin"
        self.tail_h264 = cam_dir / "tail.h264"
        # True once the tail merge failed: the source is the only file that
        # can still receive the tail, so it survives a successful remux.
        self.keep_source = False
        self.proc = None
        self.err_fd = None
        self.err_path = None


class EncodeWorker(QThread):
    progress = pyqtSignal(int, int)
    finished_all = pyqtSignal(list)   # [(cam, n_frames, ok)] in camera order

    def __init__(self, video_dir: Path, camera_names: list[str],
                 acq_type: str, w: int, h: int, fps: int, quality: int,
                 date: str, session_id: str, max_parallel: int = 0,
                 realtime: bool = False, backend: str | None = None):
        super().__init__()
        self._video_dir = Path(video_dir)
        self._camera_names = camera_names
        self._acq_type = acq_type
        self._w = w
        self._h = h
        self._fps = fps
        self._quality = quality
        self._date = date
        self._session_id = session_id
        # 0 => encode all cameras concurrently
        self._max_parallel = max_parallel
        # realtime: frames were already H.264-encoded on the GPU during capture;
        # here we only wrap the .h264 elementary stream into mp4 (stream copy).
        self._realtime = realtime
        # Post-hoc encoder for raw frames; None means ffmpeg_cmd's default.
        self._backend = backend
        self._stop = threading.Event()
        self._ffmpeg = None
        # Outcomes that are not failures but must reach the operator: a camera
        # whose mp4 holds fewer frames than the capture intended, with its
        # metadata truncated to match. Read after finished_all fires.
        self.warnings: list[str] = []

    def request_stop(self) -> None:
        """Stop launching ffmpeg and kill the processes in flight.

        Checked before every Popen. Without it a quit that kills the running
        ffmpegs is followed by fresh launches for the cameras still queued,
        which hold files open while the caller tries to clean up. Every
        camera not finished is reported as a failure with its source kept.
        """
        self._stop.set()

    # ------------------------------------------------------------------ commands
    def _cmd(self, job: _Job) -> list:
        ff = self._ffmpeg
        if job.stage == "tail":
            return [ff, *ffmpeg_cmd.global_args("warning"),
                    *ffmpeg_cmd.rawvideo_input_args(self._w, self._h, self._fps),
                    "-i", str(job.tail_bin),
                    *ffmpeg_cmd.h264_encoder_args(self._fps, self._quality,
                                                  self._backend),
                    *ffmpeg_cmd.h264_stream_args(), str(job.tail_h264)]
        if job.stage == "remux":
            # Stream-copy remux: no re-encode, finishes in seconds, no GPU.
            return [ff, *ffmpeg_cmd.global_args("warning"),
                    "-fflags", "+genpts", "-r", str(self._fps),
                    "-i", str(job.src),
                    *ffmpeg_cmd.stream_copy_args(), str(job.mp4_path)]
        return [ff, *ffmpeg_cmd.global_args("warning"),
                *ffmpeg_cmd.rawvideo_input_args(self._w, self._h, self._fps),
                "-i", str(job.src),
                *ffmpeg_cmd.h264_encoder_args(self._fps, self._quality,
                                              self._backend),
                *ffmpeg_cmd.mp4_container_args(), str(job.mp4_path)]

    # ---------------------------------------------------------------- job setup
    def _make_job(self, i: int, cam: str):
        """Pick the source for one camera, or return an error string."""
        cam_dir = self._video_dir / cam
        h264_path = cam_dir / "stream.h264"
        raw_bin = cam_dir / "raw.bin"
        tail_bin = cam_dir / "raw_tail.bin"
        mp4_path = cam_dir / (
            f"{self._date}-{self._session_id}-{cam}-{self._acq_type}.mp4")
        h264_size = h264_path.stat().st_size if h264_path.exists() else -1
        has_tail = tail_bin.exists() and tail_bin.stat().st_size > 0
        # stream.h264 counts as a source only when it holds data or a raw
        # tail exists to extend it. A zero-byte stream.h264 is what a failed
        # encoder init leaves behind, and remuxing it would fail while the
        # full raw.bin beside it went unmentioned.
        if self._realtime and (h264_size > 0 or (h264_size == 0 and has_tail)):
            ft = cam_dir / "frametimes.npy"
            try:
                n_frames = int(np.load(ft).shape[1]) if ft.exists() else 0
            except Exception:
                n_frames = 0
            return _Job(i, cam, cam_dir, h264_path, mp4_path, n_frames,
                        "tail" if has_tail else "remux")
        if raw_bin.exists() and raw_bin.stat().st_size > 0:
            if h264_size == 0:
                print(f"[encode] {cam}: stream.h264 is empty, encoding raw.bin "
                      f"instead", flush=True)
            n_frames = os.path.getsize(raw_bin) // (self._w * self._h)
            return _Job(i, cam, cam_dir, raw_bin, mp4_path, n_frames, "encode")
        have = [p.name for p in (h264_path, raw_bin, tail_bin) if p.exists()]
        return (f"no usable source: expected stream.h264 or raw.bin in "
                f"{cam_dir} (present: {', '.join(have) or 'nothing'})")

    # ------------------------------------------------------------- stage results
    def _launch(self, job: _Job) -> bool:
        """Start the ffmpeg for ``job``'s current stage; False on launch failure."""
        # ffmpeg's stderr goes to a per-camera file so a failed encode is
        # diagnosable (kept on failure, removed on success).
        name = "tail_error.log" if job.stage == "tail" else "encode_error.log"
        job.err_path = job.cam_dir / name
        try:
            job.err_fd = open(job.err_path, "wb")
            job.proc = subprocess.Popen(
                self._cmd(job), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=job.err_fd,
                **ffmpeg_cmd.quiet_popen_kwargs())
            return True
        except Exception as e:
            # Close the stderr fd on the launch-failure path: on Windows a
            # leaked handle keeps encode_error.log open, so the camera
            # directory cannot be deleted and the next recording's
            # stale-artifact unlink fails too.
            self._close_err(job)
            print(f"[encode] {job.cam}: ffmpeg launch failed ({job.stage}): {e}",
                  flush=True)
            return False

    @staticmethod
    def _close_err(job: _Job) -> None:
        try:
            if job.err_fd is not None:
                job.err_fd.close()
        except Exception:
            pass
        job.err_fd = None

    def _warn(self, job: _Job, msg: str) -> None:
        """Record an operator-facing warning in memory, on disk and in the log."""
        text = f"{job.cam}: {msg}"
        print(f"[encode] WARNING: {text}", flush=True)
        self.warnings.append(text)
        try:
            with open(job.cam_dir / "WARNINGS.txt", "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError as e:
            print(f"[encode] could not write WARNINGS.txt for {job.cam}: {e}",
                  flush=True)

    @staticmethod
    def _unlink_quiet(job: _Job, path: Path) -> None:
        """Best-effort unlink of a staging file; a failure is logged, never raised.

        The staging files (tail.h264, raw_tail.bin, the stderr log) are
        derived data once the merge is decided, so a locked file (an AV
        scanner or an Explorer preview holding it) must not change the
        outcome of the merge or abort the worker.
        """
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            print(f"[encode] {job.cam}: could not remove {path.name}: {e}",
                  flush=True)

    def _tail_done(self, job: _Job, ret: int) -> bool:
        """Finish the tail stage. True => job proceeds to the remux stage."""
        tail_ok = (ret == 0 and job.tail_h264.exists()
                   and job.tail_h264.stat().st_size > 0)
        if tail_ok:
            # Only the append decides the merge. The size before appending is
            # the rollback point: a write error midway leaves frames in
            # stream.h264 that the metadata does not describe, so the file is
            # cut back to the head before the failure path truncates the
            # metadata to the same split point.
            head = job.src.stat().st_size
            try:
                _append_file(job.src, job.tail_h264)
            except Exception as e:
                reason = f"append failed: {e}"
                try:
                    os.truncate(job.src, head)
                except OSError as e2:
                    self._unlink_quiet(job, job.tail_h264)
                    self._fail(job, f"raw tail merge FAILED ({reason}) and "
                                    f"stream.h264 could not be restored to its "
                                    f"{head}-byte head ({e2}); stream.h264, "
                                    f"raw_tail.bin and metadata KEPT, no mp4 "
                                    f"written")
                    return False
            else:
                # The stream now holds every frame the metadata describes.
                # Removing the staging files is cleanup, not part of the merge:
                # a failed unlink here must not be reported as a failed merge,
                # because that would truncate the metadata to the head while
                # the mp4 holds the full recording, and the alignment pass would
                # then trim every other camera down to that head.
                for p in (job.tail_h264, job.tail_bin, job.err_path):
                    self._unlink_quiet(job, p)
                print(f"[encode] {job.cam}: appended raw tail "
                      f"({os.path.getsize(job.src)} bytes total)", flush=True)
                return True
        else:
            reason = (f"ffmpeg exited {ret}; stderr in {job.err_path}: "
                      f"{_log_tail(job.err_path)}")
        # The tail is safe on disk in raw_tail.bin but is NOT going into the
        # mp4. The metadata must describe only the frames the mp4 will hold,
        # so truncate it to the router's recorded split point; without that
        # record the camera fails rather than over-claim frames.
        self._unlink_quiet(job, job.tail_h264)
        k = _read_encoded_count(job.cam_dir)
        if k is None or k > max(job.n_frames, 0):
            self._fail(job, f"raw tail merge FAILED ({reason}) and "
                            f"{ENCODED_JSON} does not record the split point; "
                            f"stream.h264 and raw_tail.bin KEPT, no mp4 written")
            return False
        # No mp4 can come out of an empty head, so nothing is truncated: the
        # metadata must go on describing the frames in raw_tail.bin, exactly as
        # in the no-split-point branch. Truncating to zero frames first would
        # leave an empty blockids.npy that fails the whole recording's
        # alignment pass, not just this camera.
        if k == 0 or job.src.stat().st_size == 0:
            self._fail(job, f"raw tail merge FAILED ({reason}) and stream.h264 "
                            f"holds no frames ({ENCODED_JSON} encoded={k}, "
                            f"{job.src.stat().st_size} bytes); raw_tail.bin and "
                            f"metadata KEPT, no mp4 written")
            return False
        try:
            _truncate_metadata(job.cam_dir, k)
        except Exception as e:
            self._fail(job, f"raw tail merge FAILED ({reason}) and metadata "
                            f"truncation to {k} frames failed ({e}); "
                            f"stream.h264 and raw_tail.bin KEPT")
            return False
        job.n_frames = k
        job.keep_source = True
        self._warn(job, f"raw tail merge FAILED ({reason}). The mp4 holds only "
                        f"the {k} frames encoded before the encoder died; "
                        f"blockids.npy/frametimes.npy were truncated to {k} so "
                        f"frame indices still map to the right triggers (full "
                        f"arrays kept as *.full.npy). stream.h264 and the "
                        f"unmerged raw_tail.bin are KEPT.")
        return True

    def _final_done(self, job: _Job, ret: int) -> None:
        """Finish the remux/encode stage: verify the mp4 before removing the source."""
        # A zero exit code is not proof of an output file. Stat the mp4
        # before deleting the ONLY other copy of the data — the source is
        # removed below and cannot be recovered.
        ok = (ret == 0 and job.mp4_path.exists()
              and job.mp4_path.stat().st_size > MIN_MP4_BYTES)
        if ret == 0 and not ok:
            print(f"[encode] {job.cam}: ffmpeg reported success but "
                  f"{job.mp4_path.name} is missing or empty; KEEPING "
                  f"{job.src.name}", flush=True)
        if ok:
            try:
                if not job.keep_source:
                    os.remove(job.src)
                job.err_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._result(job, True)
            return
        self._fail(job, f"ffmpeg exited {ret}; source kept at {job.src}; "
                        f"stderr in {job.err_path}:\n{_log_tail(job.err_path)}")

    def _fail(self, job: _Job, msg: str) -> None:
        # A partial mp4 beside a kept source would be picked up as the
        # recording by every later consumer, so it does not survive a failure.
        try:
            job.mp4_path.unlink(missing_ok=True)
        except OSError:
            pass
        print(f"[encode] {job.cam}: {msg}", flush=True)
        self._result(job, False)

    def _result(self, job: _Job, ok: bool) -> None:
        self._results[job.idx] = (job.cam, job.n_frames, ok)
        self._done += 1
        self.progress.emit(self._done, self._total)

    # ---------------------------------------------------------------------- run
    def run(self):
        total = self._total = len(self._camera_names)
        self._results = [None] * total
        self._done = 0
        self.warnings = []

        try:
            self._ffmpeg = ffmpeg_cmd.ffmpeg_exe()
        except Exception as e:
            print(f"[encode] {e}; every source file is KEPT", flush=True)
            self.finished_all.emit([(cam, 0, False) for cam in self._camera_names])
            return

        # Build the job list; cameras with no source file are immediate failures.
        pending = deque()
        for i, cam in enumerate(self._camera_names):
            job = self._make_job(i, cam)
            if isinstance(job, str):
                print(f"[encode] {cam}: {job}", flush=True)
                self._results[i] = (cam, 0, False)
                self._done += 1
                continue
            pending.append(job)
        if self._done:
            self.progress.emit(self._done, total)

        max_par = self._max_parallel if self._max_parallel > 0 else max(1, len(pending))
        running: dict[int, _Job] = {}
        while pending or running:
            if self._stop.is_set():
                while pending:
                    self._fail(pending.popleft(),
                               "stopped before ffmpeg launched; source kept")
                for job in running.values():
                    if job.proc.poll() is None:
                        job.proc.kill()
            while pending and len(running) < max_par and not self._stop.is_set():
                job = pending.popleft()
                if self._launch(job):
                    running[job.idx] = job
                else:
                    self._fail(job, f"ffmpeg launch failed ({job.stage}); "
                                    f"source kept at {job.src}")

            for idx in list(running.keys()):
                job = running[idx]
                ret = job.proc.poll()
                if ret is None:
                    continue
                self._close_err(job)
                del running[idx]
                if self._stop.is_set():
                    # A killed tail encode must not be read as a merge
                    # failure: truncating the metadata then would discard the
                    # tail's trigger record while the tail itself is still on
                    # disk. Report the stop and keep every source.
                    self._unlink_quiet(job, job.tail_h264)
                    self._fail(job, f"stopped during {job.stage}; source kept "
                                    f"at {job.src}")
                    continue
                if job.stage == "tail":
                    if self._tail_done(job, ret):
                        job.stage = "remux"
                        pending.appendleft(job)
                else:
                    self._final_done(job, ret)

            if pending or running:
                time.sleep(0.15)

        results = [r if r is not None else (self._camera_names[i], 0, False)
                   for i, r in enumerate(self._results)]
        self.finished_all.emit(results)


def _log_tail(path: Path, n: int = 2000) -> str:
    try:
        return Path(path).read_text(errors="replace")[-n:].strip()
    except OSError:
        return "(no stderr captured)"
