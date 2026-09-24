"""Startup hardware screening — warns about insufficient resources.

Two jobs, and the difference matters. `run_hardware_check` is the launch-time
survey (CPU, RAM, disk, which H.264 paths actually work) that also probes the
NVENC session cap and benches libx264, so Record never pays for either.
`check_capacity` is the refuse-or-warn gate run at acquisition start against
the camera count that is really open. `HardwareCheckThread` runs the survey
at launch, and the encoder half of it again after a profile switch, with the
profile's NVENC upload applied first (`configure_nvenc_upload`).

RULE: every capability here is MEASURED, never inferred from a version string
or a compiled-in feature list. REASON: the whole point of a preflight is to
disagree with the machine's own optimism; a check that cannot fail is worse
than no check, because the warning it owns never fires.
"""
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psutil
from PyQt5.QtCore import QThread, pyqtSignal

from gui_app import cpu_encode, cuda_driver, encoders, ffmpeg_cmd

#: Bytes one real-time H.264 frame costs at the rig's qp, measured on real
#: recordings -- which are NVENC recordings. Named because the disk budget is
#: only honest when the number it multiplies is the one the chosen encode path
#: actually writes.
#:
#: RULE: where this is applied to the CPU path, the estimate is labelled a
#: lower bound. REASON: libx264 at `ultrafast` and the same `-qp` emits a
#: materially larger frame than NVENC, and no measurement of the CPU path on
#: rig content exists to replace it -- so the shortfall is stated rather than
#: hidden behind a constant whose comment says "measured".
H264_BYTES_PER_FRAME = 4600

#: Presets the launch bench measures.
#:
#: RULE: only a preset a session can actually run is benched at launch.
#: REASON: a benched preset reads as an offer, and `x264_factory` builds
#: `ultrafast` alone -- the rig profile has no field for the preset, so a
#: second row advertises a choice the operator has no way to make, and the
#: bench costs a real encode per preset on every launch.
BENCH_PRESETS = ("ultrafast",)


@dataclass
class HardwareReport:
    cpu_cores: int = 0
    cpu_threads: int = 0
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_write_mb_s: float = -1.0
    #: ffmpeg's h264_nvenc encodes here (the post-hoc writers' GPU path).
    has_nvenc: bool = False
    #: PyNvVideoCodec loads here (the real-time capture path). The two are
    #: different libraries against the same hardware and can disagree.
    nvenc_runtime: bool = False
    #: The real-time encoder actually applies its GOP setting, measured from
    #: the bitstream. None when NVENC is unavailable or the check could not
    #: run. False means recordings will be a single IDR and unseekable.
    nvenc_gop_ok: bool | None = None
    #: Concurrent NVENC sessions the driver granted, -1 when not probed.
    nvenc_sessions: int = -1
    #: libx264 frames per second per core, by preset, -1.0 when not benched.
    x264_fps_per_core: dict = field(default_factory=dict)
    #: Cameras the CPU path sustains at the profile frame rate, by preset.
    x264_max_cams: dict = field(default_factory=dict)
    #: What `select_encoder` installed for this session, "" when not run.
    encoder: str = ""
    encoder_reason: str = ""
    #: How NVENC receives each frame from here on (`configure_nvenc_upload`),
    #: "" when the upload was not configured. The context is "" for the host
    #: upload, which runs in PyNvVideoCodec's own context.
    nvenc_upload: str = ""
    nvenc_context: str = ""
    nvenc_upload_reason: str = ""
    #: The camera SDK the profile's backend drives, its version and location,
    #: or why it cannot be loaded (`backends.sdk_report`); "" when not asked.
    camera_sdk: str = ""
    #: False for the encoder-only check a profile switch runs: the host
    #: survey (CPU, RAM, disk) is not repeated and its fields stay empty.
    survey: bool = True
    warnings: list = field(default_factory=list)


def check_nvenc() -> bool:
    """Does ffmpeg's h264_nvenc actually encode on this machine?

    RULE: test-encode, never grep `ffmpeg -encoders`. REASON: `-encoders`
    lists what the binary was COMPILED with, and the bundled imageio-ffmpeg
    build always lists h264_nvenc whether or not an NVIDIA GPU and driver are
    present — NVENC availability is only tested at encoder init. So the grep
    could not return False, and the launch warning written for a non-NVIDIA
    machine was dead on exactly that machine.

    0.2 s of black at 256x256: enough for the driver to create and destroy a
    session, small enough to be free at launch.
    """
    try:
        cmd = ([ffmpeg_cmd.ffmpeg_exe()] + ffmpeg_cmd.global_args()
               + ["-f", "lavfi", "-i", "color=c=black:s=256x256:r=25:d=0.2",
                  "-c:v", "h264_nvenc", "-f", "null", "-"])
        result = subprocess.run(cmd, capture_output=True, timeout=20,
                                **ffmpeg_cmd.quiet_popen_kwargs())
        return result.returncode == 0
    except Exception:
        return False


def check_nvenc_runtime() -> bool:
    """Does PyNvVideoCodec — the REAL-TIME path — load here?

    Recorded separately from `check_nvenc` because they are different
    libraries: ffmpeg can encode on the GPU while the capture path cannot
    (no wheel, no cudart), and the report has to name which one is missing.
    """
    try:
        from gui_app import nvenc
        return bool(nvenc.available())
    except Exception:
        return False


#: Set by run_hardware_check, read by check_capacity: None until measured.
_ffmpeg_nvenc_ok: bool | None = None


#: Chunk written repeatedly to make up the test size. Random once and reused,
#: because generating 256 MB of randomness costs more than the write it is
#: meant to time, and NTFS does not compress by default so repetition is free.
_SPEED_TEST_CHUNK_MB = 16


def estimate_disk_speed(target_dir: Path | None, size_mb: int = 256) -> float:
    """Sustained write rate of the output drive in MB/s, or -1 when unknown.

    RULE: write at least 256 MB and `os.fsync` before stopping the clock.
    REASON: a 16 MB write followed by `flush()` only reaches the OS page cache
    and finishes in a few milliseconds on any drive, so the rate came back in
    GB/s on every machine and the `< 500 MB/s` warning could not fire on the
    slow disk it exists for.

    RULE: no target directory means no test. REASON: the fallback was the
    current working directory — the repository — which measures a different
    drive from the one the recording lands on and leaves a large file behind
    if the process dies mid-write.
    """
    if target_dir is None:
        return -1.0
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return -1.0
    test_file = target_dir / ".panopticon_speed_test"
    chunk_mb = min(_SPEED_TEST_CHUNK_MB, max(1, size_mb))
    chunk = np.random.bytes(chunk_mb * 1024 * 1024)
    chunks = max(1, size_mb // chunk_mb)
    written_mb = chunk_mb * chunks
    try:
        t0 = time.perf_counter()
        with open(test_file, "wb") as f:
            for _ in range(chunks):
                f.write(chunk)
            f.flush()
            os.fsync(f.fileno())
        elapsed = time.perf_counter() - t0
        return written_mb / elapsed if elapsed > 0 else -1.0
    except OSError:
        return -1.0
    finally:
        try:
            test_file.unlink(missing_ok=True)
        except OSError:
            pass


def _get_disk_free(target: Path) -> float:
    for p in [target] + list(target.parents):
        try:
            return shutil.disk_usage(p).free / (1024 ** 3)
        except OSError:
            continue
    return -1.0


def physical_cores() -> int:
    """Cores the encode-versus-capture arithmetic is allowed to spend."""
    return psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True) or 1


def run_hardware_check(output_dir: str = "") -> HardwareReport:
    global _ffmpeg_nvenc_ok
    report = HardwareReport()

    report.cpu_cores = psutil.cpu_count(logical=False) or 1
    report.cpu_threads = psutil.cpu_count(logical=True) or 1

    mem = psutil.virtual_memory()
    report.ram_total_gb = mem.total / (1024 ** 3)
    report.ram_available_gb = mem.available / (1024 ** 3)

    target = Path(output_dir) if output_dir else Path(".")
    report.disk_free_gb = _get_disk_free(target)
    # Only the configured output drive is measured; see estimate_disk_speed.
    report.disk_write_mb_s = estimate_disk_speed(target if output_dir else None)
    report.has_nvenc = check_nvenc()
    report.nvenc_runtime = check_nvenc_runtime()
    _ffmpeg_nvenc_ok = report.has_nvenc
    _check_gop(report)

    if report.cpu_cores < 4:
        report.warnings.append(
            f"CPU: {report.cpu_cores} cores detected (4+ recommended for multi-camera capture)"
        )
    if report.ram_total_gb < 16:
        report.warnings.append(
            f"RAM: {report.ram_total_gb:.0f} GB total (16 GB+ recommended)"
        )
    if report.disk_free_gb >= 0 and report.disk_free_gb < 500:
        report.warnings.append(
            f"Disk: {report.disk_free_gb:.0f} GB free (500 GB+ recommended). "
            f"The default real-time encode needs far less, but the raw "
            f"fallback writes every frame whole: frame width x height bytes "
            f"per camera per frame."
        )
    if report.disk_write_mb_s >= 0 and report.disk_write_mb_s < 500:
        report.warnings.append(
            f"Disk write speed: {report.disk_write_mb_s:.0f} MB/s (NVMe SSD with 1000+ MB/s recommended)"
        )
    # Name the path that is missing. The two NVENC libraries fail
    # independently, and the remedy differs: no PyNvVideoCodec costs the
    # real-time encode, no ffmpeg h264_nvenc costs the post-hoc passes, and
    # libx264 covers either at a CPU price the launch bench measures.
    if not report.has_nvenc and not report.nvenc_runtime:
        report.warnings.append(
            "No working NVENC on this machine: ffmpeg's h264_nvenc test encode "
            "failed AND PyNvVideoCodec is unavailable. The post-hoc passes run "
            "on the CPU with libx264. The RECORDING does so only if the "
            "encoder selection ran for this session — the `Using:` line of "
            "this report names what was installed, and without one every "
            "camera falls back to raw.bin, which holds every frame whole "
            "(frame width x height bytes each). Check the camera count in "
            "this report against the cameras you actually run."
        )
    elif not report.nvenc_runtime:
        report.warnings.append(
            "The real-time GPU encode path (PyNvVideoCodec) is unavailable, "
            "though ffmpeg's h264_nvenc works. Recording needs libx264 on the "
            "CPU — the `Using:` line of this report names what was actually "
            "installed — or, with `realtime_encode: false`, raw.bin plus a "
            "post-hoc GPU encode."
        )
    elif not report.has_nvenc:
        report.warnings.append(
            "ffmpeg's h264_nvenc test encode failed, though the real-time path "
            "(PyNvVideoCodec) works. The post-hoc passes — raw.bin to mp4, the "
            "raw-tail merge and the alignment re-encode — will run on the CPU "
            "with libx264 instead, which is slower but not lossy."
        )

    return report


def _check_gop(report: HardwareReport) -> None:
    """Measure whether the real-time encoder applies its GOP, into `report`.

    RULE: measure the GOP from the bitstream, not from the options the
    encoder was given, and measure it in the upload mode the recording uses.
    REASON: an encoder library ignores an unrecognised option without an
    error, and the result is a recording with one IDR that nobody notices
    until they scrub it. `nvenc.gop_is_honoured` builds its encoder through the same factory
    and upload setting as a recording, so it proves the configured path only
    when `configure_nvenc_upload` ran first.
    """
    if not report.nvenc_runtime:
        return
    try:
        from gui_app import nvenc
        report.nvenc_gop_ok = nvenc.gop_is_honoured()
    except Exception:
        report.nvenc_gop_ok = None
    if report.nvenc_gop_ok is False:
        report.warnings.append(
            "NVENC ignores the GOP setting on this build, so recordings "
            "would hold one keyframe and be unseekable in the labeler. "
            "Check the keyword names in gui_app/nvenc.py against the "
            "installed PyNvVideoCodec before recording.")


def run_encoder_check() -> HardwareReport:
    """The NVENC half of `run_hardware_check`, for a profile switch.

    The host survey (CPU, RAM, disk speed, ffmpeg's h264_nvenc) describes the
    machine, not the profile, so it is not repeated. The GOP is measured
    again, because the new profile can select another upload path.
    """
    report = HardwareReport(survey=False)
    report.has_nvenc = bool(_ffmpeg_nvenc_ok)
    report.nvenc_runtime = check_nvenc_runtime()
    _check_gop(report)
    return report


# --- NVENC upload path (gui_app.nvenc.configure_upload) ---------------------
@dataclass
class UploadChoice:
    """How NVENC receives each frame from now on, and why.

    `upload` and `context` are what `nvenc.configure_upload` was given.
    `warnings` is non-empty when that differs from what the profile asked
    for; each one names the reason and the setting used instead.
    """
    upload: str = "host"
    context: str = "shared"
    reason: str = ""
    warnings: list = field(default_factory=list)


def _mib(nbytes: float) -> str:
    return f"{nbytes / 2 ** 20:.0f} MiB"


def own_context_headroom(n_cams: int, drv=None) -> tuple:
    """Whether the GPU has free memory for one more CUDA context per camera.

    Returns (True, detail) when it has, (False, detail) when it has not, and
    (None, reason) when it could not be measured. `drv` is a
    `cuda_driver.Driver`, or None for the process's driver.

    RULE: the cost of a context is measured on this GPU, never assumed.
    REASON: it depends on the GPU, the driver and the CUDA version, so a
    constant would be one machine's number. A measurement context is created,
    then a second one, and the drop in free device memory between the two
    reads is one context's cost. Both are destroyed before returning.

    Only the contexts are counted. The encoders' own GPU memory is needed
    whatever the upload mode, so it is not part of this choice.
    """
    try:
        drv = drv if drv is not None else cuda_driver.get()
    except cuda_driver.CudaUnavailable as e:
        return None, f"the CUDA driver could not be loaded ({e})"
    probe = extra = 0
    try:
        probe = drv.ctx_create(0)
        drv.ctx_push(probe)
        try:
            free0, total = drv.mem_info()
        finally:
            drv.ctx_pop()
        extra = drv.ctx_create(0)
        drv.ctx_push(probe)
        try:
            free1, _total = drv.mem_info()
        finally:
            drv.ctx_pop()
    except Exception as e:
        return None, (f"the GPU memory a context takes could not be measured "
                      f"({type(e).__name__}: {e})")
    finally:
        for ctx in (extra, probe):
            if ctx:
                try:
                    drv.ctx_destroy(ctx)
                except Exception as e:
                    print(f"[hw] WARNING: a measurement CUDA context could not "
                          f"be destroyed: {e}", flush=True)
    per_ctx = max(0, int(free0) - int(free1))
    # Both measurement contexts are gone now, so what they held is free again.
    free = int(free1) + 2 * per_ctx
    need = max(1, int(n_cams)) * per_ctx
    detail = (f"{n_cams} contexts of {_mib(per_ctx)} each need {_mib(need)}, "
              f"and {_mib(free)} of {_mib(total)} is free")
    return free >= need, detail


def configure_nvenc_upload(profile, n_cams: int, width: int, height: int,
                           drv=None) -> UploadChoice:
    """Apply the profile's `nvenc_upload` and `nvenc_context` to gui_app.nvenc.

    The one place the profile reaches `nvenc.configure_upload`. Never raises.

    RULE: called before any encoder is created and before the GOP check, at
    launch and again after a profile switch. REASON: the setting applies to
    every encoder created after it and never to one that exists, and the GOP
    check proves only the path it runs on.

    RULE: the pinned upload is used only after `nvenc.pinned_upload_matches_
    host` passes at this frame size. REASON: the events that keep a staging
    buffer from being rewritten under its upload rely on PyNvVideoCodec
    copying on the encoder's stream, which only that check shows; any other
    answer puts the host upload back, with a warning.

    RULE: context 'own' needs free GPU memory for one extra context per
    camera (`own_context_headroom`). REASON: an own context that cannot be
    created costs that encoder the pinned path, one camera at a time; the
    shared context costs no memory, so it is used instead, with a warning.
    """
    from gui_app import nvenc
    up = str(getattr(profile, "nvenc_upload", "host") or "host")
    ctx = str(getattr(profile, "nvenc_context", "shared") or "shared")
    choice = UploadChoice(upload=up, context=ctx)

    def use_host(reason: str) -> UploadChoice:
        nvenc.configure_upload("host", "shared")
        choice.upload, choice.context, choice.reason = "host", "shared", reason
        return choice

    if up not in nvenc.UPLOAD_MODES or ctx not in nvenc.CONTEXT_MODES:
        choice.warnings.append(
            f"nvenc_upload {up!r} / nvenc_context {ctx!r} is not a setting "
            f"this build knows ({', '.join(nvenc.UPLOAD_MODES)} / "
            f"{', '.join(nvenc.CONTEXT_MODES)}), so NVENC uses the host "
            f"upload.")
        return use_host("the profile's setting is unknown")
    if up == "host":
        return use_host("the profile's nvenc_upload")
    if not nvenc.available():
        # Nothing records through NVENC here, so the setting has no effect;
        # the report's NVENC line already says why.
        return use_host("NVENC is unavailable, so the upload setting has no "
                        "effect")
    if ctx == "own":
        ok, detail = own_context_headroom(n_cams, drv)
        if ok is not True:
            choice.context = "shared"
            why = (f"the GPU is short of memory for them: {detail}"
                   if ok is False else detail)
            choice.warnings.append(
                f"nvenc_context: own gives each of the {n_cams} encoders a "
                f"CUDA context of its own, but {why}. The pinned upload runs "
                f"in the shared context instead.")
            print(f"[hw] WARNING: {choice.warnings[-1]}", flush=True)
        else:
            print(f"[hw] GPU memory for own contexts: {detail}", flush=True)
    # The check builds its own host and pinned encoders whatever is
    # configured, so the pinned setting is applied only once it has passed.
    verdict = nvenc.pinned_upload_matches_host(width, height, choice.context)
    if verdict is True:
        nvenc.configure_upload("pinned", choice.context)
    else:
        state = ("failed" if verdict is False
                 else "could not run (see the [nvenc] lines in the log)")
        choice.warnings.append(
            f"nvenc_upload: pinned needs the launch check that PyNvVideoCodec "
            f"copies each staging buffer on the encoder's stream, and at "
            f"{width}x{height} it {state}. NVENC uses the host upload for "
            f"this session.")
        print(f"[hw] WARNING: {choice.warnings[-1]}", flush=True)
        return use_host("the pinned upload check did not pass")
    choice.reason = ("the profile's nvenc_upload; the pinned upload check "
                     "passed")
    return choice


def usbfs_warning(profile, n_cams: int, platform: str | None = None,
                  param: Path = Path("/sys/module/usbcore/parameters/"
                                     "usbfs_memory_mb")) -> str:
    """Linux: a warning when usbfs cannot hold the buffers USB3 cameras queue.

    On Linux a USB3 camera's image buffers come out of the kernel's usbfs
    pool (`usbfs_memory_mb`, 16 by default), and a pool smaller than the
    buffers the profile queues makes the camera fail to stream. 0 means no
    limit. Returns "" on other systems, when the limit covers the buffers,
    and when it cannot be read. Which cameras are USB3 is not known before
    they are opened, so this is a warning, not a refusal.
    """
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        return ""
    try:
        limit_mb = int(Path(param).read_text().strip())
    except (OSError, ValueError):
        return ""
    w = int(getattr(profile, "frame_width", 0) or 0)
    h = int(getattr(profile, "frame_height", 0) or 0)
    bufs = int(getattr(profile, "max_num_buffer", 0) or 0)
    need_mb = max(1, int(n_cams)) * bufs * w * h / 2 ** 20
    if limit_mb == 0 or need_mb <= limit_mb:
        return ""
    return (f"usbfs_memory_mb is {limit_mb} MB, and {n_cams} cameras x "
            f"{bufs} buffers of {w}x{h} need {need_mb:.0f} MB. USB3 cameras "
            f"on this system draw their buffers from usbfs and fail to "
            f"stream when it is short. Raise "
            f"/sys/module/usbcore/parameters/usbfs_memory_mb above "
            f"{need_mb:.0f}, or 0 for no limit.")


_nvenc_sessions: int | None = None    # what the latest probe granted
_nvenc_saturated = False              # last probe stopped at its limit, not at a failure
_nvenc_probe_error = ""               # why the last probe could not answer at all


def nvenc_probe_error() -> str:
    """Why the last session probe could not answer; '' when it did.

    A probe that could not run is NOT the same answer as "the driver granted
    N". The preflight warns on the first and refuses on the second, so the two
    must be distinguishable by the caller.
    """
    return _nvenc_probe_error


def invalidate_nvenc_cache(reason: str = "") -> None:
    """Forget the cached session count so the next preflight re-probes.

    RULE: an NVENC init failure during a recording invalidates this cache;
    the main window calls this after an acquisition whose camera manager
    reports `last_encoder_failures`, and after a start refused because the
    kick-out encoders could not be created. REASON: the early return means
    that once a probe granted enough sessions no later Record re-probes.
    Sessions taken afterwards by another process (an orphaned h264_nvenc
    ffmpeg from a tail merge, a browser's hardware encode) are then invisible
    to preflight, while the decoupled mode drops each camera beyond the cap
    onto raw.bin, which holds every frame whole, with a disk budget sized for
    H.264.

    Sessions are free once a recording has finished, so calling this at the
    end of an acquisition that reported an encoder failure costs one probe and
    keeps the next start honest.
    """
    global _nvenc_sessions, _nvenc_saturated, _nvenc_probe_error
    if _nvenc_sessions is not None:
        print(f"[hw] NVENC session cache invalidated"
              + (f": {reason}" if reason else ""), flush=True)
    _nvenc_sessions, _nvenc_saturated, _nvenc_probe_error = None, False, ""


def nvenc_session_capacity(width: int, height: int, want: int,
                           force: bool = False) -> int:
    """Concurrent NVENC sessions grantable, at least `want` if possible. Cached.

    The cap is real and finite, it differs between GPUs, and NVIDIA has moved
    it across driver generations, so it is PROBED and never hardcoded.

    Probing is capped at `want` because every session is a real allocation, and
    that makes the result a LOWER BOUND whenever the probe stops at its own
    limit rather than at a refusal. Caching such a value as if it were the cap
    would refuse a later start that needs more cameras than the first probe
    asked for, so `_nvenc_saturated` records which kind of answer the cache
    holds, and a larger request re-probes whenever the cached count is short
    of it. Returns -1 if NVENC is unavailable entirely.
    """
    global _nvenc_sessions, _nvenc_saturated, _nvenc_probe_error
    if _nvenc_sessions is not None and not force and _nvenc_sessions >= want:
        return _nvenc_sessions
    _nvenc_probe_error = ""
    # Cached value is below what this start needs: ALWAYS re-probe rather than trusting
    # it. A shortfall is often transient: another process holding sessions for a
    # second (a browser's hardware encode, an orphaned h264_nvenc ffmpeg), or a
    # session this app deliberately leaked because an encoder thread outlived its
    # join. Latching that reading would block every subsequent Record with
    # "NVENC granted only N" until the GUI is restarted.
    try:
        from gui_app import nvenc
        if not nvenc.available():
            _nvenc_sessions, _nvenc_saturated = -1, False
        else:
            limit = max(1, want)
            # Isolated by default: counting the cap allocates every session the
            # driver will grant, and the router asks for n_cams of them moments
            # later. In-process that release races the allocation and hangs at
            # this very line; a child process's exit frees them for certain.
            try:
                got = nvenc.probe_max_sessions_isolated(width, height,
                                                        limit=limit)
            except TimeoutError as exc:
                # RULE: a probe timeout never falls back to the in-process
                # probe. REASON: the in-process probe is what the isolated one
                # replaced, and running it here would allocate the sessions
                # the timed-out child was struggling for, in the GUI process,
                # on the UI thread. Answer "unknown" and let check_capacity
                # warn: an unknown cap is a reason to be careful, not a reason
                # to hang the window.
                _nvenc_probe_error = str(exc)
                _nvenc_saturated = False
                print(f"[hw] NVENC session probe timed out: {exc}", flush=True)
                return _nvenc_sessions if _nvenc_sessions is not None else -1
            if got < 0:
                got = nvenc.probe_max_sessions(width, height, limit=limit)
            # RULE: the cache holds the latest probe, never the highest count
            # ever seen. REASON: a probe runs only when the cached count is
            # short of what a start needs, and a max() would answer that
            # start with the old, higher count while the fresh probe says
            # another process now holds sessions.
            _nvenc_sessions = got
            _nvenc_saturated = (got >= limit)
        print(f"[hw] NVENC sessions: {_nvenc_sessions}"
              + (" (at least — probe stopped at its limit)" if _nvenc_saturated
                 else " (driver ceiling)")
              + f", needed {want}", flush=True)
    except Exception as e:
        print(f"[hw] NVENC session probe failed: {e}", flush=True)
        _nvenc_sessions, _nvenc_saturated = -1, False
    return _nvenc_sessions


# --- libx264 launch bench --------------------------------------------------
#: preset -> frames per second one libx264 thread sustains at the profile
#: geometry. Filled by `run_x264_bench` at launch; read by the preflight and
#: by the report text, both of which must never bench on the UI thread.
_x264_bench: dict = {}
#: (width, height, fps) the cached bench was measured at, None before one ran.
_x264_bench_key: tuple | None = None


def x264_bench_fps(preset: str = "ultrafast") -> float:
    """Benched frames per second per core, or -1.0 when never measured."""
    return _x264_bench.get(preset, -1.0)


def x264_bench_is_for(width: int, height: int, fps: int) -> bool:
    """Whether the cached bench was measured at this geometry and rate.

    A profile switch re-benches only when this is False: the rate per core
    depends on the frame size, so a bench measured for another profile's
    geometry answers the wrong question.
    """
    return bool(_x264_bench) and _x264_bench_key == (int(width), int(height),
                                                      int(fps))


def run_x264_bench(width: int, height: int, fps: int,
                   presets: tuple = BENCH_PRESETS) -> dict:
    """Measure and cache the CPU encode rate for each preset. Slow (seconds).

    RULE: call this from a worker thread at launch, never from a UI slot.
    REASON: each preset runs a real 2 s encode, and the window is frozen for
    the whole of it with no busy indicator.
    """
    global _x264_bench_key
    for preset in presets:
        _x264_bench[preset] = cpu_encode.x264_bench(width, height, fps,
                                                    preset=preset)
        print(f"[hw] libx264 {preset}: {_x264_bench[preset]:.1f} fps per core "
              f"at {width}x{height}", flush=True)
    _x264_bench_key = (int(width), int(height), int(fps))
    return dict(_x264_bench)


def x264_camera_count(fps: int, preset: str = "ultrafast") -> int:
    """Cameras the benched CPU rate sustains at `fps`, 0 when unknown."""
    return cpu_encode.sustainable_cameras(x264_bench_fps(preset), fps,
                                          physical_cores())


# --- encoder selection -----------------------------------------------------
@dataclass
class EncoderChoice:
    """What the session will encode with, and why.

    `blocking` non-empty means the start must be refused: no path on this
    machine can encode the open cameras in real time, and the remaining option
    (raw) writes every frame whole, hundreds of times the H.264 rate, so it is
    chosen in the profile or not at all.
    """
    encoder: str = "nvenc"
    reason: str = ""
    blocking: str = ""
    sessions: int = -1
    fps_per_core: float = -1.0
    max_cams: int = 0


#: What `select_encoder` last installed, "" until it has run in this process.
_selected_encoder = ""


def selected_encoder() -> str:
    """What `select_encoder` last installed ("nvenc", "x264" or "raw"), or ""
    when it has not run in this process."""
    return _selected_encoder


def installed_encoder(realtime: bool) -> str:
    """The encoder an acquisition started now records with.

    Read off the installed seam rather than from the last selection: the grab
    threads and the router resolve `encoders.get_default_factory()` when they
    build their encoders, and with no selection the built-in NVENC path
    stands. `realtime` is the profile's `realtime_encode`, the only field that
    puts the capture on raw.bin.
    """
    if not realtime:
        return "raw"
    if encoders.get_default_factory() is cpu_encode.x264_factory:
        return "x264"
    return "nvenc"


def _raw_ratio(width: int, height: int) -> str:
    """How many times the H.264 disk rate raw capture writes, as '~Nx'."""
    return f"~{max(1, round(width * height / H264_BYTES_PER_FRAME))}x"


def encoder_selection_live() -> bool:
    """Has the profile's encoder selection run in this process?

    RULE: advice to edit `encoder` in the rig profile is printed only when this
    is true. REASON: the field is read by `select_encoder`, which runs only
    when the launch preflight is handed the rig profile; where it has not run,
    editing the field changes nothing and the operator would edit the profile,
    see the same refusal, and record on the path the message told them to
    leave.
    """
    return bool(_selected_encoder)


def _cpu_path_sentence() -> str:
    """One sentence: how to reach the CPU encoder, or why it is out of reach."""
    if encoder_selection_live():
        return ("Set `encoder: x264` in the rig profile to encode on the CPU "
                "instead.")
    return ("The CPU encoder cannot be selected from the rig profile in this "
            "build: `encoder` is read only by the launch preflight's encoder "
            "selection, which no caller runs yet, so editing the field "
            "changes nothing here.")


def _install(encoder: str) -> None:
    """Point the real-time seam and the post-hoc writers at `encoder`.

    The grab threads and `SyncEncodeRouter` resolve `encoders.get_default_factory()`
    and never learn which encoder they got, so this one call is the whole
    switch. `ffmpeg_cmd` is switched alongside it because a machine that cannot
    encode in real time on the GPU cannot do the tail merge or the alignment
    re-encode there either.
    """
    global _selected_encoder
    _selected_encoder = encoder
    if encoder == "x264":
        encoders.set_default_factory(cpu_encode.x264_factory)
        ffmpeg_cmd.set_default_backend("x264")
    elif encoder == "nvenc":
        encoders.set_default_factory(None)
        # RULE: the post-hoc backend follows the h264_nvenc test encode, not
        # the real-time choice. REASON: the two NVENC libraries fail
        # independently, and on a host where PyNvVideoCodec works but ffmpeg's
        # h264_nvenc does not, this would aim the raw-tail merge and the
        # alignment re-encode at a backend the preflight has just measured as
        # broken while libx264 is available.
        ffmpeg_cmd.set_default_backend(
            "nvenc" if _ffmpeg_nvenc_ok is not False else "x264")
    else:   # raw: no real-time encoder is created; the post-hoc pass needs one
        encoders.set_default_factory(None)
        ffmpeg_cmd.set_default_backend(
            "nvenc" if _ffmpeg_nvenc_ok is not False else "x264")


def select_encoder(profile, n_cams: int, fps: int, width: int,
                   height: int) -> EncoderChoice:
    """Resolve `profile.encoder` against what this machine can actually do.

    `auto` takes NVENC when the session probe grants one per camera (it costs
    almost no CPU, and the CPU is what capture needs), else libx264 when the
    launch bench says the cores are there, else refuses — because the only
    remaining path writes the full frame every frame and that is a decision,
    not a fallback.

    Installs the chosen factory before returning, so a caller that ignores the
    result still gets a consistent session.
    """
    want = str(getattr(profile, "encoder", "auto") or "auto").lower()
    realtime = bool(getattr(profile, "realtime_encode", True))
    n_cams = max(1, int(n_cams))

    if not realtime:
        choice = EncoderChoice(
            encoder="raw",
            reason=("the profile selects raw capture, so frames are written "
                    "whole and encoded after the session"))
        _install("raw")
        return choice

    if want == "raw":
        # RULE: `encoder: raw` is honoured only together with
        # `realtime_encode: false`. REASON: the capture path branches on
        # `realtime_encode` and reads `encoder` nowhere, so this combination
        # really does encode in real time; believing it skips the session
        # check and leaves the GPU factory installed, and every camera that
        # cannot get a session falls to raw.bin, which writes every frame
        # whole, with a preflight that said nothing.
        _install("raw")
        return EncoderChoice(
            encoder="raw",
            reason=("the profile asks for raw capture while real-time "
                    "encoding is still switched on"),
            blocking=(
                "The rig profile sets `encoder: raw`, but `realtime_encode` is "
                "true and only that field switches the capture path, so this "
                "run would encode in real time and every camera that could not "
                "get an encoder would fall back to raw.bin one by one. Set "
                "`realtime_encode: false` in the rig profile to write raw "
                f"frames ({_raw_ratio(width, height)} the disk), "
                "or set `encoder` to `auto`, `nvenc` or `x264`."))

    if want in ("nvenc", "auto"):
        sessions = nvenc_session_capacity(width, height, n_cams + 2)
        if nvenc_probe_error():
            # RULE: an unprobeable cap keeps the profile's path. REASON: the
            # probe timing out says nothing about the GPU's real capacity, and
            # switching a working NVENC rig to CPU encoding on a transient
            # failure costs cores the capture threads need. check_capacity
            # warns instead, so the operator sees the uncertainty.
            _install("nvenc")
            return EncoderChoice(
                encoder="nvenc", sessions=sessions,
                reason=f"the NVENC session cap could not be probed "
                       f"({nvenc_probe_error()}); keeping the GPU path")
        if sessions >= n_cams:
            choice = EncoderChoice(
                encoder="nvenc", sessions=sessions,
                reason=f"NVENC granted {sessions} sessions for {n_cams} cameras")
            _install("nvenc")
            return choice
        if want == "nvenc":
            choice = EncoderChoice(
                encoder="nvenc", sessions=sessions,
                reason="the profile forces NVENC",
                blocking=_nvenc_shortfall_text(sessions, n_cams))
            _install("nvenc")
            return choice
        print(f"[hw] NVENC granted {sessions} of {n_cams} sessions; "
              f"considering the CPU path", flush=True)

    # Either the profile forces x264, or auto could not get a session per
    # camera. The bench is the only honest answer to "can this machine do it".
    sessions = _nvenc_sessions if _nvenc_sessions is not None else -1
    rate = x264_bench_fps("ultrafast")
    cams = x264_camera_count(fps, "ultrafast")
    cpu_text = (f"libx264 sustains ~{rate:.0f} fps per core here, enough for "
                f"{cams} cameras at {fps} fps" if rate > 0 else
                "libx264 has not been benched on this machine")

    if rate > 0 and cams >= n_cams:
        _install("x264")
        return EncoderChoice(encoder="x264", sessions=sessions,
                             fps_per_core=rate, max_cams=cams, reason=cpu_text)

    if want == "x264":
        # Forced, so install it anyway: refusing here and leaving NVENC
        # installed would start the recording on the path the profile
        # rejected. The blocking message stops the start instead.
        _install("x264")
        return EncoderChoice(
            encoder="x264", sessions=sessions, fps_per_core=rate,
            max_cams=cams, reason=cpu_text,
            blocking=(f"The profile selects libx264, but {cpu_text} and "
                      f"{n_cams} cameras are open. Record fewer cameras, "
                      f"lower the frame rate, or set `realtime_encode: false` "
                      f"to write raw frames and encode after the session "
                      f"({_raw_ratio(width, height)} the disk)."))

    _install("raw")
    return EncoderChoice(
        encoder="raw", sessions=sessions, fps_per_core=rate, max_cams=cams,
        reason="neither NVENC nor libx264 can encode every camera in real time",
        blocking=(
            f"No encoder on this machine can keep up with {n_cams} cameras at "
            f"{fps} fps: " + _nvenc_shortfall_text(sessions, n_cams) + " "
            + cpu_text + ". Record fewer cameras, or set "
            "`realtime_encode: false` in the rig profile to write raw frames "
            "and encode after the session, which needs "
            f"{_raw_ratio(width, height)} the disk."))


def _nvenc_shortfall_text(sessions: int, n_cams: int) -> str:
    """One sentence naming what the GPU actually granted."""
    if sessions < 0:
        return "NVENC is unavailable in this process."
    if sessions == 0:
        return "NVENC granted no encode sessions."
    return (f"NVENC granted only {sessions} concurrent sessions but {n_cams} "
            f"cameras need one each (the driver caps this).")


def check_capacity(n_cams: int, width: int, height: int,
                   ring_n: int, max_num_buffer: int,
                   realtime: bool, output_dir: str = "",
                   minutes: float = 10.0, fps: int = 100,
                   encoder: str = "auto") -> tuple[list, list]:
    """Refuse-or-warn check run at acquisition start. Returns (blocking, warnings).

    Everything here scales linearly with camera count, which is why it exists:
    the numbers that are comfortable at a few cameras are not at more, and
    each of these limits otherwise fails without a message: a camera dropping
    to `raw.bin` (it holds the mono8 frame, width*height bytes each, hundreds
    of times the H.264 size), a MemoryError inside a grab thread, or a disk
    filling mid-session.

    `encoder` is the profile's selection (`auto`, `nvenc`, `x264`, `raw`); it
    decides which capability is checked and, with the answer, which byte rate
    the disk budget uses. `realtime` — the profile's `realtime_encode` — is the
    only thing that decides whether frames are written raw, which is why
    `encoder: raw` without it is refused here rather than believed.
    """
    blocking: list[str] = []
    warnings: list[str] = []
    if n_cams <= 0:
        blocking.append("No cameras are open. Recording would run the trigger "
                        "protocol — and any baked-in stim paradigm — while "
                        "saving nothing.")
        return blocking, warnings

    frame_b = width * height                  # mono8 from the camera
    nv12_b = width * (height * 3 // 2)        # what the ring holds

    # --- RAM -----------------------------------------------------------------
    pool_gb = n_cams * max_num_buffer * frame_b / 2 ** 30
    ring_gb = (n_cams * ring_n * nv12_b / 2 ** 30) if realtime else 0.0
    need_gb = pool_gb + ring_gb
    avail_gb = psutil.virtual_memory().available / 2 ** 30
    detail = (f"{need_gb:.1f} GiB needed ({pool_gb:.1f} driver pool"
              + (f" + {ring_gb:.1f} NV12 ring" if realtime else "")
              + f"), {avail_gb:.1f} GiB available")
    # RULE: RAM either refuses the start or raises no warning at all.
    # REASON: a need under what is available records normally, and a prompt
    # before every recording on a rig that always sits near the line trains
    # the operator to click through it. Running out mid-session loses
    # frames, so a need over what is available refuses the start. The figure
    # goes to the log either way.
    print(f"[hw] RAM for {n_cams} cameras: {detail}", flush=True)
    if need_gb > avail_gb:
        # RULE: name the profile field, `max_num_buffer`. REASON: the pool
        # depth actually used comes from the profile; MAX_NUM_BUFFER in
        # camera_manager is only the fallback default, and "MaxNumBuffer" is
        # a camera SDK's node name; neither string exists in the YAML the
        # operator must edit to act on this message.
        blocking.append(
            f"Not enough RAM for {n_cams} cameras: {detail}. Lower "
            f"`max_num_buffer` or `kick_max_lag` in the rig profile, or close "
            f"other applications.")

    # --- the encode path -----------------------------------------------------
    # `raw_mode` and `encodes_realtime` are kept apart on purpose: the first is
    # what the profile asked for, the second is what this machine can deliver,
    # and it is the SECOND that the disk budget below must use. Budgeting
    # 4.6 KB/frame while every camera writes 2.3 MB/frame is how a disk fills
    # mid-session with a preflight that said nothing.
    path = str(encoder or "auto").lower()
    # RULE: only `realtime_encode` decides whether this run writes raw frames.
    # REASON: nothing in the capture path reads `encoder`, so `encoder: raw`
    # with `realtime_encode: true` still encodes in real time; treating it as
    # raw here skipped the session check entirely, and with no session every
    # camera fell back to raw.bin at a rate below the sustained-write warning
    # below, so nothing said a word.
    raw_mode = not realtime
    encodes_realtime = not raw_mode

    if not raw_mode and path == "raw":
        blocking.append(
            f"The rig profile sets `encoder: raw` while `realtime_encode` is "
            f"true, and only `realtime_encode` switches the capture path: this "
            f"run would encode in real time, and every camera that could not "
            f"get an encoder would fall back to raw.bin at "
            f"~{frame_b*fps/2**30*600:.0f} GiB per 10 min. Set "
            f"`realtime_encode: false` in the rig profile to write raw frames "
            f"deliberately, or set `encoder` to `auto`, `nvenc` or `x264`.")
    elif not raw_mode and path in ("auto", "nvenc"):
        got = nvenc_session_capacity(width, height, n_cams + 2)
        cpu_cams = x264_camera_count(fps, "ultrafast")
        if nvenc_probe_error():
            # Unknown is not zero, and it is not the cap either: warn, do not
            # refuse. The disk budget stays on the H.264 rate because the
            # likely reading is that NVENC works and the probe was unlucky;
            # the warning names what it costs if that reading is wrong.
            warnings.append(
                f"The NVENC session cap could not be probed "
                f"({nvenc_probe_error()}). Starting anyway, but a camera that "
                f"cannot get a session falls back to raw.bin at "
                f"~{frame_b*fps/2**30*600:.0f} GiB per 10 min. Watch the "
                f"disk. " + _cpu_path_sentence())
        elif got < n_cams:
            # RULE: the claim "recording on the CPU instead" is gated on the
            # installed seam, not on the bench cache. REASON: the bench and the
            # selection are two calls and `HardwareCheckThread.run()` swallows
            # an exception from the second, so a bench result can outlive a
            # selection that never installed libx264 — and this branch would
            # then pass the start with a warning saying the opposite of what
            # the recording does.
            cpu_installed = (encoders.get_default_factory()
                             is cpu_encode.x264_factory)
            if path == "auto" and cpu_cams >= n_cams and cpu_installed:
                # The CPU path covers it; say so rather than refusing, because
                # select_encoder has already installed libx264 for this run.
                warnings.append(
                    f"{_nvenc_shortfall_text(got, n_cams)} Recording on the "
                    f"CPU with libx264 instead (enough for {cpu_cams} cameras "
                    f"at {fps} fps here). Encoding now competes with capture "
                    f"for cores: check WARNINGS.txt afterwards for dropped "
                    f"frames.")
            else:
                encodes_realtime = False
                fallback = (
                    f"Every camera would silently fall back to raw.bin at "
                    f"~{frame_b*fps/2**30*600:.0f} GiB per 10 min each."
                    if got <= 0 else
                    f"Cameras beyond the cap would silently fall back to "
                    f"raw.bin at ~{frame_b*fps/2**30*600:.0f} GiB per 10 min "
                    f"each.")
                blocking.append(
                    f"{_nvenc_shortfall_text(got, n_cams)} {fallback} Record "
                    f"fewer cameras, or set `realtime_encode: false` in the "
                    f"rig profile to put every camera on the raw path "
                    f"deliberately. " + _cpu_path_sentence())
    elif not raw_mode and path == "x264":
        rate = x264_bench_fps("ultrafast")
        cpu_cams = x264_camera_count(fps, "ultrafast")
        if rate <= 0:
            warnings.append(
                "The libx264 throughput of this machine has not been measured "
                "(the launch bench did not run), so whether the CPU can encode "
                f"{n_cams} cameras at {fps} fps is unverified. A camera whose "
                "encoder falls behind drops frames and its video ends up "
                "shorter than the others.")
        elif cpu_cams < n_cams:
            encodes_realtime = False
            blocking.append(
                f"libx264 encodes about {rate:.0f} fps per core at "
                f"{width}x{height} here, enough for {cpu_cams} cameras at "
                f"{fps} fps, but {n_cams} are open. Record fewer cameras, "
                f"lower the frame rate, or set `realtime_encode: false`.")

    if raw_mode and _ffmpeg_nvenc_ok is False:
        rate = x264_bench_fps("ultrafast")
        warnings.append(
            "Raw capture is selected and ffmpeg's h264_nvenc does not work on "
            "this machine, so the post-hoc encode runs on the CPU with "
            "libx264"
            + (f" at about {rate:.0f} fps per core" if rate > 0 else "")
            + f". Budget the time for {n_cams} cameras, and keep raw.bin until "
              f"the mp4s have been checked.")

    # --- disk ----------------------------------------------------------------
    # Real-time H.264 is ~4.6 KB/frame; raw is the full frame every frame.
    per_s = n_cams * fps * (H264_BYTES_PER_FRAME if encodes_realtime else frame_b)
    need_disk_gb = per_s * minutes * 60 / 2 ** 30
    # The bytes-per-frame figure was measured on NVENC recordings, so on the
    # CPU path the estimate is a floor and has to say so.
    on_cpu = (path == "x264"
              or encoders.get_default_factory() is cpu_encode.x264_factory)
    estimate_note = (
        " That estimate uses the NVENC bytes-per-frame measurement; libx264 at "
        "`ultrafast` writes more at the same qp, so read it as a lower bound."
        if encodes_realtime and on_cpu else "")
    free_gb = _get_disk_free(Path(output_dir) if output_dir else Path("."))
    if free_gb >= 0:
        if need_disk_gb > free_gb:
            # A WARNING, never a blocker. `minutes` is an assumed worst case, not
            # a known recording length, and it is the most speculative number
            # here — so it must not be the one the operator cannot override. It
            # would otherwise refuse a raw-capture profile outright, which
            # CLAUDE.md documents as the fallback when the real-time path
            # misbehaves, over hundreds of GiB demanded for what may be a
            # one-minute test.
            warnings.append(
                f"Disk may be short: a {minutes:g}-minute recording would need "
                f"~{need_disk_gb:.0f} GiB and only {free_gb:.0f} GiB is free. "
                f"A shorter recording is fine — this assumes {minutes:g} "
                f"minutes." + estimate_note)
        elif need_disk_gb > 0.8 * free_gb:
            warnings.append(
                f"Disk is tight: a {minutes:g}-minute recording needs "
                f"~{need_disk_gb:.0f} GiB of {free_gb:.0f} GiB free."
                + estimate_note)
    if not encodes_realtime and per_s / 2 ** 30 > 1.5:
        # RULE: state the rule, not this rig's drive models. REASON: gui_app is
        # shared with other installs, and naming "both NVMe drives" and a
        # "990 PRO" tells an operator elsewhere to do something impossible.
        warnings.append(
            f"Raw capture will write {per_s / 2**30:.2f} GiB/s. Consumer NVMe "
            f"drives fall to ~1-2 GB/s once their SLC cache is exhausted, so "
            f"use a drive rated for sustained writes at this rate, or split "
            f"the cameras across drives.")
    return blocking, warnings


def format_report(report: HardwareReport) -> str:
    if report.survey:
        lines = [
            "Hardware Check Results",
            "=" * 40,
            f"CPU:   {report.cpu_cores} cores / {report.cpu_threads} threads",
            f"RAM:   {report.ram_total_gb:.1f} GB total, {report.ram_available_gb:.1f} GB available",
        ]
        if report.disk_free_gb >= 0:
            lines.append(f"Disk:  {report.disk_free_gb:.0f} GB free")
        if report.disk_write_mb_s >= 0:
            lines[-1] += f", {report.disk_write_mb_s:.0f} MB/s write"
    else:
        lines = ["Encoder Check Results (profile switch)", "=" * 40]
    if report.camera_sdk:
        lines.append(f"Camera SDK: {report.camera_sdk}")
    # Both NVENC libraries, named, because the remedy differs per path.
    lines.append(
        f"NVENC: real-time (PyNvVideoCodec) "
        f"{'available' if report.nvenc_runtime else 'NOT available'}, "
        f"post-hoc (ffmpeg h264_nvenc) "
        f"{'available' if report.has_nvenc else 'NOT available'}")
    if report.nvenc_gop_ok is False:
        lines.append("       GOP NOT APPLIED: recordings would hold one keyframe "
                     "and be unseekable")
    elif report.nvenc_gop_ok:
        lines.append("       GOP verified from the bitstream: one keyframe per second")
    if report.nvenc_sessions >= 0:
        lines.append(f"       {report.nvenc_sessions} concurrent encode sessions granted")
    if report.nvenc_upload:
        where = (f", {report.nvenc_context} CUDA context"
                 if report.nvenc_upload == "pinned" and report.nvenc_context
                 else "")
        lines.append(f"       upload: {report.nvenc_upload}{where}"
                     + (f" ({report.nvenc_upload_reason})"
                        if report.nvenc_upload_reason else ""))
    # The CPU fallback's measured ceiling, so the operator can compare it with
    # the cameras they intend to run instead of discovering it mid-recording.
    # Every preset this report actually measured, rather than the launch
    # bench's list: a report made by the `--bench` entry point carries more.
    for preset in sorted(report.x264_fps_per_core):
        rate = report.x264_fps_per_core.get(preset, -1.0)
        if rate is None or rate < 0:
            continue
        cams = report.x264_max_cams.get(preset, 0)
        lines.append(f"x264:  {preset:<10} {rate:.0f} fps per core "
                     f"-> up to {cams} cameras")
    if report.encoder:
        lines.append(f"Using: {report.encoder}"
                     + (f" ({report.encoder_reason})" if report.encoder_reason else ""))
    else:
        # RULE: say when nothing was selected. REASON: an absent line reads as
        # "NVENC, as configured", while it means the rig profile's `encoder`
        # field was never consulted and the built-in NVENC default stands
        # whatever this machine can do.
        lines.append("Using: the encoder selection did not run for this "
                     "session, so the rig profile's `encoder` field had no "
                     "effect and the built-in NVENC path is installed")
    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in report.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines)


# --- the session header's environment facts ---------------------------------
#: Distributions the header reports by version, read from their installed
#: metadata so that none of them is imported: on a FLIR rig, importing
#: pypylon to read its version would load an SDK the rig does not use.
HEADER_PACKAGES = ("pypylon", "PyNvVideoCodec", "numpy",
                   ("opencv-contrib-python", "opencv-python",
                    "opencv-python-headless"), "PyQt5", "psutil")

#: What Windows reports through a 32-bit bits-per-second counter for a link
#: at or above 2**32 b/s, in the Mb/s psutil gives.
_SPEED_32BIT_MBPS = 2 ** 32 // 10 ** 6

_env_lock = threading.Lock()
#: The facts that do not change while the process runs, gathered once.
_env_static: list | None = None


def _unavailable(e) -> str:
    return f"unavailable ({type(e).__name__}: {e})"


def _quiet_run(cmd, timeout_s: float = 5.0):
    """subprocess.run without a console window over the GUI under pythonw."""
    try:
        quiet = ffmpeg_cmd.quiet_popen_kwargs()
    except Exception:
        quiet = {}
    return subprocess.run(cmd, capture_output=True, text=True,
                          timeout=timeout_s, stdin=subprocess.DEVNULL,
                          **quiet)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _panopticon_version() -> str:
    """The version in pyproject.toml and the git commit, or why not."""
    root = _repo_root()
    try:
        import tomllib
        with open(root / "pyproject.toml", "rb") as f:
            version = str(tomllib.load(f)["project"]["version"])
    except Exception as e:
        version = _unavailable(e)
    try:
        r = _quiet_run(["git", "-C", str(root), "rev-parse", "--short=12",
                        "HEAD"])
        if r.returncode != 0 or not r.stdout.strip():
            return f"{version}, not a git checkout"
        commit = r.stdout.strip()
        r = _quiet_run(["git", "-C", str(root), "status", "--porcelain",
                        "--untracked-files=no"])
        dirty = (" with uncommitted changes to tracked files"
                 if r.returncode == 0 and r.stdout.strip() else "")
        return f"{version}, git commit {commit}{dirty}"
    except FileNotFoundError:
        return f"{version}, not a git checkout (git is not installed)"
    except Exception as e:
        return f"{version}, git commit {_unavailable(e)}"


def _os_text() -> str:
    import platform
    text = platform.platform()
    try:
        v = sys.getwindowsversion()
        text += f" (Windows build {v.build})"
    except AttributeError:
        pass
    return text


def _cpu_model() -> str:
    """The CPU's marketing name: the registry on Windows, /proc/cpuinfo on
    Linux, platform.processor() otherwise."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        try:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("model name"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    import platform
    return platform.processor() or "unavailable (not reported)"


def _cpu_text() -> str:
    try:
        model = _cpu_model()
    except Exception as e:
        model = _unavailable(e)
    try:
        layout = (f"{psutil.cpu_count(logical=False) or '?'} physical cores, "
                  f"{psutil.cpu_count(logical=True) or '?'} logical")
    except Exception as e:
        layout = _unavailable(e)
    try:
        from gui_app import cpu_affinity
        classes = cpu_affinity.describe_classes(cpu_affinity.cpu_classes())
        classes = classes.replace("[affinity] CPU efficiency classes: ",
                                  "efficiency classes ")
    except Exception as e:
        classes = f"core classes {_unavailable(e)}"
    return f"{model}; {layout}; {classes}"


def _gpu_text() -> str:
    try:
        r = _quiet_run(["nvidia-smi", "--query-gpu=name,driver_version,"
                        "memory.total", "--format=csv,noheader"], 10.0)
    except FileNotFoundError:
        return "unavailable (nvidia-smi is not on PATH)"
    except Exception as e:
        return _unavailable(e)
    rows = [row.strip() for row in r.stdout.splitlines() if row.strip()]
    if r.returncode != 0 or not rows:
        return f"unavailable (nvidia-smi exit {r.returncode})"
    out = []
    for i, row in enumerate(rows):
        parts = [x.strip() for x in row.split(",")]
        if len(parts) >= 3:
            out.append(f"GPU {i} {parts[0]}, NVIDIA driver {parts[1]}, "
                       f"{parts[2]}")
        else:
            out.append(f"GPU {i} {row}")
    return "; ".join(out)


def _package_text() -> str:
    from importlib import metadata
    out = []
    for entry in HEADER_PACKAGES:
        names = entry if isinstance(entry, tuple) else (entry,)
        for name in names:
            try:
                out.append(f"{name} {metadata.version(name)}")
                break
            except metadata.PackageNotFoundError:
                continue
            except Exception as e:
                out.append(f"{name} {_unavailable(e)}")
                break
        else:
            out.append(f"{names[0]} not installed")
    return ", ".join(out)


def _network_text() -> str:
    """Every network interface that is up, with the link speed and MTU the
    OS reports. Which camera sits behind which port is probe_network.py's
    job; this records what each port negotiated."""
    try:
        stats = psutil.net_if_stats()
    except Exception as e:
        return _unavailable(e)
    out = []
    for name, st in sorted(stats.items()):
        if not st.isup or name.lower().startswith(("loopback", "lo")):
            continue
        if not st.speed:
            speed = "speed not reported"
        elif st.speed == _SPEED_32BIT_MBPS:
            speed = f"at least {st.speed} Mb/s (the OS counter's top)"
        else:
            speed = f"{st.speed} Mb/s"
        out.append(f"{name} {speed} MTU {st.mtu}")
    return "; ".join(out) or "no interface is up"


def _static_facts() -> list:
    facts = []
    for label, fn in (("panopticon", _panopticon_version),
                      ("python", lambda: f"{sys.version.split()[0]} "
                                         f"({sys.executable})"),
                      ("os", _os_text), ("cpu", _cpu_text),
                      ("gpu", _gpu_text), ("packages", _package_text)):
        try:
            facts.append((label, fn()))
        except Exception as e:
            facts.append((label, _unavailable(e)))
    return facts


def _nvenc_cap_text() -> str:
    if _nvenc_probe_error:
        return f"unknown (the last probe could not run: {_nvenc_probe_error})"
    if _nvenc_sessions is None:
        return "not probed yet (the hardware check reports it)"
    if _nvenc_sessions < 0:
        return "NVENC unavailable in this process"
    if _nvenc_saturated:
        return (f"at least {_nvenc_sessions} (the probe stopped at the count "
                f"it needed)")
    return f"{_nvenc_sessions} (the driver refused the next session)"


def environment_facts(camera_sdk: str = "") -> list:
    """(label, value) pairs for the session header: Panopticon's version
    and git commit, Python, the OS build, the CPU model and core layout,
    RAM, the GPU and NVIDIA driver, the NVENC session cap, package versions,
    the network interfaces, and `camera_sdk` (the loaded backend's
    sdk_report, the Spinnaker version and DLL path on a FLIR rig).

    RULE: never raises, and a fact that cannot be read says "unavailable"
    with the reason. REASON: the header is written at every acquisition
    start, and a missing tool on a volunteer's machine must cost a line of
    the header, not the start.

    RULE: cold path only, never on the UI thread for the first call. REASON:
    the first call runs git and nvidia-smi, which take up to seconds; the
    facts that cannot change while the process runs are kept after it, and
    later calls only read RAM, the network links and the cached NVENC
    count.
    """
    global _env_static
    with _env_lock:
        if _env_static is None:
            _env_static = _static_facts()
        facts = list(_env_static)
    try:
        mem = psutil.virtual_memory()
        facts.insert(4, ("ram", f"{mem.total / 2 ** 30:.1f} GiB total, "
                                f"{mem.available / 2 ** 30:.1f} GiB "
                                f"available"))
    except Exception as e:
        facts.insert(4, ("ram", _unavailable(e)))
    facts.insert(6, ("nvenc session cap", _nvenc_cap_text()))
    # Read on every call, like RAM: a link can renegotiate (10 Gb/s down to
    # 1 Gb/s) between launch and an acquisition, and the header is what
    # records the speed that acquisition ran at.
    try:
        facts.append(("network", _network_text()))
    except Exception as e:
        facts.append(("network", _unavailable(e)))
    facts.append(("camera sdk", camera_sdk or "no camera backend loaded"))
    return facts


class HardwareCheckThread(QThread):
    """The launch survey, the NVENC session probe and the libx264 bench.

    RULE: the probe and the bench run HERE, not at Record. REASON: both cost
    seconds — the probe starts an interpreter, imports PyNvVideoCodec and
    allocates every session the driver will grant, and the bench runs two real
    encodes — and at Record they execute on the Qt main thread, freezing the
    window with no busy indicator, once per Record for as long as the count is
    short.

    `survey=False` is the check a profile switch runs: the host survey is
    skipped, and the NVENC upload, the GOP check, the bench (only when the
    frame size or rate changed) and the encoder selection run for the new
    profile.

    The report is emitted on `report_ready`, once, whatever happens inside:
    the window keeps Record and Calibrate disabled until it arrives. The
    thread's `finished` is QThread's own.
    """

    report_ready = pyqtSignal(object)

    def __init__(self, output_dir: str = "", profile=None, n_cams: int = 0,
                 survey: bool = True):
        super().__init__()
        self._output_dir = output_dir
        self._profile = profile
        self._n_cams = int(n_cams)
        self._survey = bool(survey)

    def run(self):
        report = HardwareReport(survey=self._survey)
        try:
            report = self._check()
        except Exception as e:
            print(f"[hw] hardware check failed: {type(e).__name__}: {e}",
                  flush=True)
            report.warnings.append(
                f"The hardware check could not finish ({type(e).__name__}: "
                f"{e}). Its findings are incomplete; the capacity check still "
                f"runs when an acquisition starts.")
        finally:
            self.report_ready.emit(report)

    def _geometry(self):
        """(cameras, frame rate, width, height) the checks size themselves to:
        the open cameras, else the profile's, with RigProfile's defaults for
        a profile object that lacks a field."""
        from gui_app.session_config import RigProfile
        p, d = self._profile, RigProfile()
        n_cams = self._n_cams or int(getattr(p, "n_cameras", 0) or 1)
        fps = int(getattr(p, "frame_rate", 0) or d.frame_rate)
        w = int(getattr(p, "frame_width", 0) or d.frame_width)
        h = int(getattr(p, "frame_height", 0) or d.frame_height)
        return n_cams, fps, w, h

    def _check(self) -> HardwareReport:
        p = self._profile
        upload = None
        if p is not None:
            # Before the survey, whose GOP check builds its encoder on the
            # upload path configured here.
            n_cams, _fps, w, h = self._geometry()
            upload = configure_nvenc_upload(p, n_cams, w, h)
        report = (run_hardware_check(self._output_dir) if self._survey
                  else run_encoder_check())
        if upload is not None:
            report.nvenc_upload = upload.upload
            report.nvenc_context = (upload.context
                                    if upload.upload == "pinned" else "")
            report.nvenc_upload_reason = upload.reason
            report.warnings.extend(upload.warnings)
        if p is None:
            return report
        backend = str(getattr(p, "camera_backend", "") or "")
        if backend:
            from gui_app import backends
            # With the camera: block, so a backend whose SDK folder the
            # block names (camera.flir.sdk_dir) loads it from there when
            # this report is the first to load it.
            report.camera_sdk = backends.sdk_report(
                backend, camera_spec=getattr(p, "camera", None))
        usbfs = usbfs_warning(p, self._geometry()[0])
        if usbfs:
            report.warnings.append(usbfs)
        try:
            self._preflight_encoders(report)
        except Exception as e:
            # A broken encoder preflight must not cost the operator the
            # rest of the report, which is what warns about RAM and disk.
            #
            # RULE: the bench result is dropped with it. REASON: the bench
            # runs before the selection, so a selection that raised would
            # otherwise leave a cache that check_capacity reads as "the CPU
            # path is live" while the GPU factory is still installed.
            _x264_bench.clear()
            report.x264_fps_per_core.clear()
            report.x264_max_cams.clear()
            print(f"[hw] encoder preflight failed: {e}", flush=True)
        return report

    def _preflight_encoders(self, report: HardwareReport) -> None:
        p = self._profile
        n_cams, fps, w, h = self._geometry()
        # The per-core rate depends on the frame size, so a bench measured
        # for another profile's geometry is measured again; the same
        # geometry keeps the launch measurement.
        if not x264_bench_is_for(w, h, fps):
            run_x264_bench(w, h, fps)
        for preset in BENCH_PRESETS:
            report.x264_fps_per_core[preset] = x264_bench_fps(preset)
            report.x264_max_cams[preset] = x264_camera_count(fps, preset)
        choice = select_encoder(p, n_cams, fps, w, h)
        report.encoder = choice.encoder
        report.encoder_reason = choice.reason
        report.nvenc_sessions = choice.sessions
        if choice.blocking:
            report.warnings.append(choice.blocking)
