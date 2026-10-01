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


#: The colour description of every video Panopticon writes.
#:
#: RULE: the camera's Mono8 values are the luma, unchanged from 0 to 255, and
#: the stream says so: full range, with BT.709 primaries, transfer and matrix.
#: REASON: a stream that declares no range is read as 16-235 by every decoder
#: (OpenCV, imageio, browsers), which stretches the image, so everything below
#: 16 shows as 0 and everything above 235 as 255. Chrome, and so LUC3D, honours
#: the range only when the primaries, transfer and matrix are declared too.
#: The chroma is neutral, so the matrix changes no value of a gray image.
COLOUR_ARGS = ("-color_range", "pc", "-color_primaries", "bt709",
               "-color_trc", "bt709", "-colorspace", "bt709")

#: The same description, written into an existing H.264 stream by a remux.
H264_COLOUR_BSF = ("h264_metadata=video_full_range_flag=1:colour_primaries=1:"
                   "transfer_characteristics=1:matrix_coefficients=1")


def full_range_args() -> list:
    """Output options that keep a gray source's 0-255 as the luma, and declare it.

    RULE: the scale filter is explicit. REASON: without it the conversion from
    gray to yuv420p squeezes the values into 16-235 (Y = 16 + v x 219/255),
    and whether a build keeps them depends on how it negotiates ranges.

    RULE: `setparams` puts the description on the frames as well as in
    COLOUR_ARGS. REASON: an encoder takes the frames' description over the
    command line's, and after the scale filter the primaries and transfer are
    unknown, which Chrome reads as no description at all.
    """
    return ["-vf", "scale=in_range=full:out_range=full,setparams=range=pc:"
                   "color_primaries=bt709:color_trc=bt709:colorspace=bt709",
            "-pix_fmt", "yuv420p", *COLOUR_ARGS]


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

    ``yuv420p`` for decoder compatibility, in full range with its colour
    description (``full_range_args``), and the description in the container's
    ``colr`` box too, which is where browsers read it. ``+faststart`` because
    LUC3D reads the file in 1 MB pieces from byte 0 and stops when moov parses,
    so a moov-at-end file costs a full read per camera before frame 1 appears.
    """
    return [*full_range_args(), "-movflags", "+faststart+write_colr"]


def h264_stream_args() -> list:
    """Output options for an Annex-B elementary stream (``.h264``).

    Used for the raw-tail encode that is appended to ``stream.h264`` and for
    the libx264 real-time encoder; ``realtime_remux_args`` then wraps the
    stream. Full range with its colour description, as every writer.
    """
    return [*full_range_args(), "-f", "h264"]


def stream_copy_args() -> list:
    """Output options for wrapping an existing H.264 stream into mp4 unchanged.

    No ``-pix_fmt`` here: with ``-c:v copy`` there is no encoder to apply it
    to. ``+faststart`` still applies because the muxer writes the container.
    The stream's colour description stays as it is, so a video recorded before
    the description was written keeps reading as it always has.
    """
    return ["-c:v", "copy", "-movflags", "+faststart"]


def realtime_remux_args() -> list:
    """Output options for wrapping a recording's ``stream.h264`` into its mp4.

    A stream copy (nothing is re-encoded) that also declares the colour
    description. NVENC stores the camera's values unchanged but declares no
    range, so the bitstream filter writes it into the stream and the muxer
    into the ``colr`` box. A raw tail merged into the stream carries the same
    values, from ``h264_stream_args``.
    """
    return ["-c:v", "copy", "-bsf:v", H264_COLOUR_BSF, *COLOUR_ARGS,
            "-movflags", "+faststart+write_colr"]


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
