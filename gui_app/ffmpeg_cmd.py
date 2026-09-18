"""One place for the ffmpeg command fragments every post-hoc writer shares.

Every mp4 the rig writes needs ``-g <fps>`` (one IDR per second) AND
``-movflags +faststart`` (moov atom at the front). Both exist so the browser
labeler (LUC3D) can seek and start playing without reading the whole file, and
both are easy to drop when a new encode path is added by copying an argument
list. This module is that argument list: a writer that builds its command from
``h264_encoder_args`` + ``mp4_container_args`` cannot lose either flag.

The real-time capture path (PyNvVideoCodec in ``gui_app/nvenc.py``) is NVIDIA
only by design. The post-hoc writers here are the route a machine without an
NVIDIA GPU takes, so they also offer ``libx264`` through the same interface.

Nothing here touches the binary at import time: a missing imageio-ffmpeg wheel
must not stop the GUI from starting, only from encoding.
"""
import subprocess
import sys

BACKENDS = ("nvenc", "x264")

# Which post-hoc encoder callers get when they do not name one. The hardware
# preflight switches it to "x264" on a machine whose NVENC probe fails, so the
# workers and the CLI need not know how the choice was made.
_default_backend = "nvenc"


def set_default_backend(name: str) -> None:
    """Choose the encoder that ``h264_encoder_args(backend=None)`` resolves to."""
    if name not in BACKENDS:
        raise ValueError(f"unknown ffmpeg H.264 backend {name!r}; "
                         f"choose one of {BACKENDS}")
    global _default_backend
    _default_backend = name


def get_default_backend() -> str:
    return _default_backend


def ffmpeg_exe() -> str:
    """Path of the bundled ffmpeg binary, resolved on first use.

    Resolved lazily so that importing a worker module never fails on a machine
    whose imageio-ffmpeg wheel has no binary; the failure surfaces where an
    encode is attempted, with a message that names the cause.
    """
    try:
        from imageio_ffmpeg import get_ffmpeg_exe
        return get_ffmpeg_exe()
    except Exception as e:  # ImportError, RuntimeError from imageio-ffmpeg
        raise FileNotFoundError(
            f"ffmpeg binary not available via imageio-ffmpeg: {e}") from e


def global_args(loglevel: str = "error") -> list:
    """Flags that precede every input.

    ``-nostdin`` because ffmpeg otherwise polls stdin for interactive commands,
    and under the pythonw launcher stdin is not a valid handle.
    """
    return ["-y", "-nostdin", "-hide_banner", "-loglevel", loglevel]


def rawvideo_input_args(w: int, h: int, fps: int) -> list:
    """Demuxer options for a headerless mono8 frame stream (raw.bin, a pipe).

    The caller appends ``-i <source>`` itself, because the source may be a
    file path or ``-`` for stdin.
    """
    return ["-f", "rawvideo", "-vcodec", "rawvideo",
            "-pix_fmt", "gray", "-s", f"{int(w)}x{int(h)}",
            "-r", str(int(fps)), "-an"]


def h264_encoder_args(fps: int, quality: int, backend: str | None = None) -> list:
    """Encoder options for the chosen backend, always with ``-g <fps>``.

    ``-qp`` is a constant quantizer, so the extra IDRs cost almost nothing;
    B-frames are disabled because decode-order reordering is useless for a
    labeler that seeks by frame number.
    """
    backend = backend or _default_backend
    fps, quality = int(fps), int(quality)
    if backend == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "fast", "-qp", str(quality),
                "-g", str(fps), "-bf:v", "0", "-gpu", "0"]
    if backend == "x264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-qp", str(quality),
                "-g", str(fps), "-bf", "0"]
    raise ValueError(f"unknown ffmpeg H.264 backend {backend!r}; "
                     f"choose one of {BACKENDS}")


def mp4_container_args() -> list:
    """Output options for an mp4 the labeler will open.

    ``yuv420p`` for decoder compatibility; ``+faststart`` because LUC3D reads
    the file in 1 MB pieces from byte 0 and stops when moov parses, so a
    moov-at-end file costs a full read per camera before frame 1 appears.
    """
    return ["-pix_fmt", "yuv420p", "-movflags", "+faststart"]


def h264_stream_args() -> list:
    """Output options for an Annex-B elementary stream (``.h264``).

    Used for the raw-tail encode that is appended to ``stream.h264`` before a
    stream-copy remux; the remux applies ``mp4_container_args`` at that point.
    """
    return ["-pix_fmt", "yuv420p", "-f", "h264"]


def stream_copy_args() -> list:
    """Output options for wrapping an existing H.264 stream into mp4 unchanged.

    No ``-pix_fmt`` here: with ``-c:v copy`` there is no encoder to apply it
    to. ``+faststart`` still applies because the muxer writes the container.
    """
    return ["-c:v", "copy", "-movflags", "+faststart"]


def quiet_popen_kwargs() -> dict:
    """``subprocess`` keyword arguments that keep a child from opening a console.

    Under pythonw every console child would otherwise flash a window over the
    GUI. The dict carries no stdio arguments, so callers add their own
    ``stdin``/``stdout``/``stderr``.
    """
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return {"startupinfo": si,
            "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def check_mp4_command(cmd: list, fps: int) -> None:
    """Raise if an mp4-writing command lacks either labeler invariant.

    A guard for tests and for writers assembled by hand: ``-g <fps>`` (unless
    the stream is copied, in which case the GOP is already in the stream) and
    ``-movflags +faststart``.
    """
    copy = "copy" in cmd
    if not copy:
        try:
            g = cmd[cmd.index("-g") + 1]
        except (ValueError, IndexError):
            raise AssertionError(f"mp4 command has no -g <fps>: {cmd}")
        if int(g) != int(fps):
            raise AssertionError(f"mp4 command GOP {g} != fps {fps}: {cmd}")
    try:
        flags = cmd[cmd.index("-movflags") + 1]
    except (ValueError, IndexError):
        raise AssertionError(f"mp4 command has no -movflags: {cmd}")
    if "+faststart" not in flags:
        raise AssertionError(f"mp4 command lacks +faststart: {cmd}")
