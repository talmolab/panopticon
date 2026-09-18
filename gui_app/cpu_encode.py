"""CPU H.264 fallback: libx264 behind the `gui_app.encoders` seam.

The real-time capture path is NVENC by design, and a machine without an NVIDIA
GPU (or one whose driver session cap is already spent) has no GPU encoder to
give every camera. Its only other option today is `raw.bin` at ~500x the
H.264 size, which fills a disk in minutes. This module is the middle ground:
one `ffmpeg -c:v libx264` child process per camera, fed the Y plane of each
NV12 frame over a pipe, producing the same Annex-B elementary stream that
`stream.h264` already holds. Nothing downstream changes -- the remux, the
block-ID bookkeeping and the labeler see the same bytes.

RULE: libx264 is a fallback, never the default. REASON: it costs a CPU core
per camera at a rate the launch bench measures, and those cores are the same
ones the grab threads need; `hardware_check.select_encoder` only reaches for it
when the NVENC probe cannot serve every camera.

RULE: every command line here carries `-g <fps>`. REASON: the LUC3D labeler
seeks by IDR, and a stream with one IDR is unseekable -- the same invariant
`gui_app/ffmpeg_cmd.py` exists to protect for the post-hoc writers.

The encoder argument list below duplicates part of `ffmpeg_cmd.h264_encoder_args`
because the real-time path needs `-preset`, `-tune zerolatency` and `-threads`
per encoder while that helper fixes them. Consolidating the two belongs with
the next change to `ffmpeg_cmd.py`, which this package may not edit.
"""
from __future__ import annotations

import subprocess
import threading
import time

from gui_app import ffmpeg_cmd

#: Bytes drained from the child's stdout per read. One frame of 1920x1200 at
#: qp21 is ~4.6 KB, so this is several frames per syscall without ever holding
#: a large buffer.
_READ_CHUNK = 1 << 16

#: stderr lines kept for the message a failed encode raises. Bounded because a
#: wedged ffmpeg can print without limit and this object lives in the capture
#: process.
_STDERR_TAIL_LINES = 20

#: How long `EndEncode()` waits for the child to flush and exit before the
#: stream is declared lost and the process killed. Generous: the child may
#: still hold a few frames of lookahead when stdin closes.
_END_TIMEOUT_S = 30.0

#: Presets the preflight benches and the profile may name, fastest first.
PRESETS = ("ultrafast", "superfast", "veryfast", "faster", "fast", "medium")


def x264_encoder_args(fps: int, quality: int, preset: str = "ultrafast",
                      threads: int = 1) -> list:
    """libx264 options for a real-time, per-camera encode.

    `-tune zerolatency` because a frame must leave the encoder in bounded time:
    the calling thread returns the bytes to the router, and lookahead would
    turn a 10 ms cycle into a multi-second hostage. `-bf 0` for the same reason
    and because the labeler seeks by frame number, so decode-order reordering
    buys nothing. `-threads 1` by default because there is one of these per
    camera and the grab threads need the remaining cores.
    """
    fps, quality, threads = int(fps), int(quality), max(1, int(threads))
    if preset not in PRESETS:
        raise ValueError(f"unknown libx264 preset {preset!r}; "
                         f"choose one of {PRESETS}")
    return ["-c:v", "libx264", "-preset", preset, "-tune", "zerolatency",
            "-qp", str(quality), "-g", str(fps), "-bf", "0",
            "-threads", str(threads)]


def x264_pipe_command(width: int, height: int, fps: int, quality: int,
                      preset: str = "ultrafast", threads: int = 1) -> list:
    """The full `ffmpeg` argv for one camera's stdin-to-stdout encode.

    Split out from the encoder object so a test can assert the invariants
    (`-g <fps>`, no B-frames, gray input of the right geometry) without
    starting a process.
    """
    return ([ffmpeg_cmd.ffmpeg_exe()]
            + ffmpeg_cmd.global_args()
            + ffmpeg_cmd.rawvideo_input_args(width, height, fps)
            + ["-i", "pipe:0"]
            + x264_encoder_args(fps, quality, preset, threads)
            + ffmpeg_cmd.h264_stream_args()
            # Without this the raw muxer holds finished packets in its AVIO
            # buffer, so `Encode()` returns nothing for a while and then a
            # burst; the router's bookkeeping is unharmed either way, but a
            # stalled child then looks identical to a working one.
            + ["-flush_packets", "1", "pipe:1"])


class X264Encoder:
    """One `ffmpeg -c:v libx264` child, driven frame by frame over pipes.

    Implements the `gui_app.encoders.EncoderProtocol`: `Encode(nv12)` takes the
    NV12 frame the ring holds, writes its Y plane (the gray image) to the
    child's stdin and returns the Annex-B bytes a reader thread has drained so
    far; `EndEncode()` closes stdin and returns the remainder; `Close()` is the
    kill path.

    RULE: a reader thread drains stdout for the whole life of the child.
    REASON: the pipe holds only a few tens of kilobytes, so an encoder nobody
    reads blocks inside its own write and the `Encode()` call that fed it never
    returns -- the capture thread would be wedged in a native call, which is
    exactly the state `SyncEncodeRouter.abandon()` cannot recover from.
    """

    def __init__(self, width: int, height: int, fps: int, quality: int,
                 preset: str = "ultrafast", threads: int = 1):
        self._width = int(width)
        self._height = int(height)
        self._fps = int(fps)
        self.preset = preset
        self.cmd = x264_pipe_command(width, height, fps, quality, preset,
                                     threads)
        self._chunks: list = []
        self._lock = threading.Lock()
        self._stderr: list = []
        self._closed = False
        self._proc = subprocess.Popen(
            self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, **ffmpeg_cmd.quiet_popen_kwargs())
        self._reader = threading.Thread(target=self._drain_stdout,
                                        name="x264-out", daemon=True)
        self._reader.start()
        self._errreader = threading.Thread(target=self._drain_stderr,
                                           name="x264-err", daemon=True)
        self._errreader.start()

    # -- child plumbing ----------------------------------------------------
    def _drain_stdout(self):
        stream = self._proc.stdout
        read = getattr(stream, "read1", None) or stream.read
        try:
            while True:
                data = read(_READ_CHUNK)
                if not data:
                    return
                with self._lock:
                    self._chunks.append(data)
        except Exception:
            # The pipe was closed under the reader (kill path). There is
            # nothing to report: the caller already knows it killed the child.
            return

    def _drain_stderr(self):
        try:
            for line in iter(self._proc.stderr.readline, b""):
                text = line.decode("utf-8", "replace").rstrip()
                if not text:
                    continue
                self._stderr.append(text)
                del self._stderr[:-_STDERR_TAIL_LINES]
        except Exception:
            return

    def _take(self) -> bytes:
        with self._lock:
            if not self._chunks:
                return b""
            chunks, self._chunks = self._chunks, []
        return b"".join(chunks)

    def _why(self) -> str:
        code = self._proc.poll()
        tail = "; ".join(self._stderr[-3:]) or "no stderr output"
        return f"ffmpeg exit={code}: {tail}"

    # -- EncoderProtocol ---------------------------------------------------
    def Encode(self, nv12) -> bytes:
        """Feed one frame's Y plane and return whatever has been encoded.

        The return may be empty: libx264 answers a frame's worth of bytes a
        frame or two later even at `-tune zerolatency`, and the router appends
        whatever it gets to `stream.h264` in order, so bytes are never lost by
        arriving late.
        """
        if self._closed:
            raise RuntimeError("libx264 encoder is closed")
        plane = nv12[:self._height] if hasattr(nv12, "shape") else nv12
        if hasattr(plane, "flags") and not plane.flags["C_CONTIGUOUS"]:
            # A view that is not contiguous cannot be written as a buffer, and
            # copying it here is correct but must be visible in a profile.
            import numpy as np
            plane = np.ascontiguousarray(plane)
        buf = plane.data if hasattr(plane, "data") else plane
        try:
            self._proc.stdin.write(buf)
            self._proc.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"libx264 encoder died: {self._why()}") from e
        return self._take()

    def EndEncode(self) -> bytes:
        """Close stdin, let the child flush, and return the remaining bytes.

        Idempotent: the encoder threads call this and then `Close()`, and the
        router's failure paths may call it again on an object it already
        released.
        """
        if self._closed:
            return b""
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        self._reader.join(timeout=_END_TIMEOUT_S)
        try:
            self._proc.wait(timeout=_END_TIMEOUT_S)
        except Exception:
            # A child that has not exited after its stdin closed is wedged in
            # the encoder. Killing it loses the tail of the stream, which the
            # caller must see: the alternative is a capture thread that never
            # finishes its join.
            print(f"[x264] child did not exit after stdin close; killing "
                  f"({self._why()})", flush=True)
            self.kill()
        self._errreader.join(timeout=1.0)
        rest = self._take()
        self._closed = True
        self._close_pipes()
        return rest

    def Close(self) -> None:
        """Release the child unconditionally (the encoder threads' last step)."""
        if not self._closed:
            self.kill()
        self._close_pipes()
        self._closed = True

    def kill(self) -> None:
        """Abandon path: end the child now, without waiting for its stream.

        Used when a recording is torn down rather than finished. An ffmpeg
        child holds no GPU session, but it does hold the pipe the encoder
        thread may be blocked writing into, so killing it is what lets that
        thread leave `Encode()`.
        """
        try:
            if self._proc.poll() is None:
                self._proc.kill()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5.0)
        except Exception:
            pass
        self._reader.join(timeout=1.0)
        self._errreader.join(timeout=1.0)

    def _close_pipes(self) -> None:
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass

    @property
    def returncode(self):
        return self._proc.poll()

    def __del__(self):
        try:
            self.Close()
        except Exception:
            pass


def create_x264_encoder(width: int, height: int, fps: int, quality: int,
                        preset: str = "ultrafast", threads: int = 1
                        ) -> X264Encoder:
    """Start one CPU H.264 encoder for a camera of this geometry."""
    return X264Encoder(width, height, fps, quality, preset=preset,
                       threads=threads)


#: Preset the factory uses unless `set_factory_preset` changes it. `ultrafast`
#: because the preflight bench is what decides whether the machine can encode
#: at all, and a slower preset only lowers the camera count it allows.
_factory_preset = "ultrafast"
_factory_threads = 1


def set_factory_options(preset: str = "ultrafast", threads: int = 1) -> None:
    """Choose what `x264_factory` builds. Called by the encoder selection."""
    if preset not in PRESETS:
        raise ValueError(f"unknown libx264 preset {preset!r}; "
                         f"choose one of {PRESETS}")
    global _factory_preset, _factory_threads
    _factory_preset, _factory_threads = preset, max(1, int(threads))


def x264_factory(width: int, height: int, quality: int, fps: int,
                 notes: list) -> X264Encoder:
    """`gui_app.encoders` factory for the CPU path.

    The argument order is the seam's, not `create_x264_encoder`'s. Every
    recording made this way carries a note, because a CPU encode is a
    degradation from what the profile's NVENC path promises and the operator
    reads that from WARNINGS.txt, not from the console.
    """
    notes.append(
        f"Real-time encoding runs on the CPU (libx264, preset "
        f"{_factory_preset}, {_factory_threads} thread per camera) because "
        f"NVENC was not available for every camera. Quality at the same qp is "
        f"comparable, but the cores are shared with capture: check the "
        f"encoder queue-full count in this recording's warnings.")
    return create_x264_encoder(width, height, fps, quality,
                               preset=_factory_preset,
                               threads=_factory_threads)


# --- launch bench ---------------------------------------------------------
#: Fraction of one core a camera's capture side costs (grab thread, the NV12
#: ring copy, the router submit) at the measured 0.8 ms per 10 ms cycle, with
#: headroom. Used to turn a single-core encode rate into a camera count.
CAPTURE_CORE_FRACTION = 0.2

#: Cores reserved for the GUI, the preview decimation and the OS.
RESERVED_CORES = 1.0


def x264_bench(width: int, height: int, fps: int, preset: str = "ultrafast",
               quality: int = 21, seconds: float = 2.0,
               timeout: float = 120.0) -> float:
    """Frames per second one libx264 thread sustains at this geometry.

    Synthesises its own input with lavfi `testsrc2` rather than reading a file,
    so the bench needs no recording on disk and can run at launch. The source
    synthesis is inside the timed region, which makes the answer CONSERVATIVE:
    the real encoder is fed frames that already exist.

    Returns -1.0 when ffmpeg is missing or the bench fails, which the caller
    must read as "unknown", never as "fast enough".
    """
    frames = max(1, int(round(fps * seconds)))
    try:
        exe = ffmpeg_cmd.ffmpeg_exe()
    except Exception as e:
        print(f"[x264] bench skipped, no ffmpeg: {e}", flush=True)
        return -1.0
    cmd = ([exe] + ffmpeg_cmd.global_args()
           + ["-f", "lavfi",
              "-i", f"testsrc2=size={int(width)}x{int(height)}:rate={int(fps)}",
              "-frames:v", str(frames)]
           + x264_encoder_args(fps, quality, preset, threads=1)
           + ["-pix_fmt", "yuv420p", "-f", "null", "-"])
    try:
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout,
                              **ffmpeg_cmd.quiet_popen_kwargs())
        elapsed = time.perf_counter() - t0
    except Exception as e:
        print(f"[x264] bench could not run: {e}", flush=True)
        return -1.0
    if proc.returncode != 0 or elapsed <= 0:
        tail = (proc.stderr or b"").decode("utf-8", "replace").strip()
        print(f"[x264] bench failed (exit {proc.returncode}): "
              f"{tail.splitlines()[-1] if tail else 'no output'}", flush=True)
        return -1.0
    return frames / elapsed


def sustainable_cameras(fps_per_core: float, fps: int, cores: int) -> int:
    """How many cameras this machine can encode on the CPU at `fps`.

    Each camera costs `fps / fps_per_core` cores of encoding plus
    `CAPTURE_CORE_FRACTION` of a core of capture, and `RESERVED_CORES` are kept
    for the GUI. The answer is a whole number of cameras and is floored, so it
    never promises the marginal one.
    """
    if fps_per_core <= 0 or fps <= 0 or cores <= 0:
        return 0
    per_cam = CAPTURE_CORE_FRACTION + (float(fps) / float(fps_per_core))
    usable = float(cores) - RESERVED_CORES
    if usable <= 0 or per_cam <= 0:
        return 0
    return max(0, int(usable / per_cam))


def _cpu_name() -> str:
    """Model string of this machine's CPU, or a placeholder.

    Read from the registry rather than `wmic`, which recent Windows builds no
    longer ship, and without a subprocess so the bench output stays honest
    about what it measured.
    """
    import platform
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        with key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except Exception:
        return platform.processor() or "unknown CPU"


def _main(argv) -> int:
    """`python -m gui_app.cpu_encode --bench W H FPS` -- the documented bench."""
    if len(argv) < 4 or argv[0] != "--bench":
        print("usage: python -m gui_app.cpu_encode --bench WIDTH HEIGHT FPS")
        return 2
    import psutil
    w, h, fps = int(argv[1]), int(argv[2]), int(argv[3])
    cores = psutil.cpu_count(logical=False) or 1
    threads = psutil.cpu_count(logical=True) or cores
    print(f"CPU: {_cpu_name()} ({cores} cores / {threads} threads)")
    print(f"Geometry: {w}x{h} at {fps} fps")
    for preset in ("ultrafast", "veryfast"):
        rate = x264_bench(w, h, fps, preset=preset)
        if rate < 0:
            print(f"  {preset:<10} bench failed")
            continue
        cams = sustainable_cameras(rate, fps, cores)
        print(f"  {preset:<10} {rate:7.1f} fps per core  ->  {cams} cameras "
              f"at {fps} fps")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
