"""Startup hardware screening — warns about insufficient resources.

Two jobs, and the difference matters. `run_hardware_check` is the launch-time
survey (CPU, RAM, disk, which H.264 paths actually work) that also probes the
NVENC session cap and benches libx264, so Record never pays for either.
`check_capacity` is the refuse-or-warn gate run at acquisition start against
the camera count that is really open.

RULE: every capability here is MEASURED, never inferred from a version string
or a compiled-in feature list. REASON: the whole point of a preflight is to
disagree with the machine's own optimism; a check that cannot fail is worse
than no check, because the warning it owns never fires.
"""
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psutil
from PyQt5.QtCore import QThread, pyqtSignal

from gui_app import cpu_encode, encoders, ffmpeg_cmd

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

    # RULE: measure the GOP from the bitstream at launch, not from the options
    # the encoder was given. REASON: an encoder library drops an unrecognised
    # option silently, and the result is a recording with one IDR that nobody
    # notices until they scrub it. Cheap enough to do every launch.
    if report.nvenc_runtime:
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
            f"Disk: {report.disk_free_gb:.0f} GB free (500 GB+ recommended — the "
            f"default real-time encode needs far less, but the raw fallback "
            f"writes ~129 GiB per camera per 10 min)"
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
            "camera falls back to raw.bin at ~129 GiB per camera per 10 min. "
            "Check the camera count in this report against the cameras you "
            "actually run."
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


_nvenc_sessions: int | None = None    # highest count CONFIRMED grantable
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

    The cap is real and finite — measured 12 on this rig — and NVIDIA has moved
    it across driver generations (2 → 3 → 5 → 8 → 12), so it must be PROBED and
    never hardcoded. Six cameras never revealed it because 6 < 12.

    Probing is capped at `want` because every session is a real allocation, and
    that makes the result a LOWER BOUND whenever the probe stops at its own
    limit rather than at a refusal. Caching such a value as if it were the cap
    is wrong — it made a 6-camera probe (limit 8) report "8" and then wrongly
    block a 9-camera start. So `_nvenc_saturated` records which kind of answer
    we have, and a larger request re-probes only when the previous answer was
    limit-bound. Returns -1 if NVENC is unavailable entirely.
    """
    global _nvenc_sessions, _nvenc_saturated, _nvenc_probe_error
    if _nvenc_sessions is not None and not force and _nvenc_sessions >= want:
        return _nvenc_sessions
    _nvenc_probe_error = ""
    # Cached value is below what we need — ALWAYS re-probe rather than trusting
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


def x264_bench_fps(preset: str = "ultrafast") -> float:
    """Benched frames per second per core, or -1.0 when never measured."""
    return _x264_bench.get(preset, -1.0)


def run_x264_bench(width: int, height: int, fps: int,
                   presets: tuple = BENCH_PRESETS) -> dict:
    """Measure and cache the CPU encode rate for each preset. Slow (seconds).

    RULE: call this from a worker thread at launch, never from a UI slot.
    REASON: each preset runs a real 2 s encode, and the window is frozen for
    the whole of it with no busy indicator.
    """
    for preset in presets:
        _x264_bench[preset] = cpu_encode.x264_bench(width, height, fps,
                                                    preset=preset)
        print(f"[hw] libx264 {preset}: {_x264_bench[preset]:.1f} fps per core "
              f"at {width}x{height}", flush=True)
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
    (raw) costs ~500x the disk, so it is chosen deliberately in the profile or
    not at all.
    """
    encoder: str = "nvenc"
    reason: str = ""
    blocking: str = ""
    sessions: int = -1
    fps_per_core: float = -1.0
    max_cams: int = 0


#: What `select_encoder` last installed, "" until it has run in this process.
_selected_encoder = ""


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
        # cannot get a session falls silently to raw.bin at ~500x the disk,
        # with a preflight that said nothing.
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
                "frames deliberately (~500x the disk), or set `encoder` to "
                "`auto`, `nvenc` or `x264`."))

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
                      f"(~500x the disk)."))

    _install("raw")
    return EncoderChoice(
        encoder="raw", sessions=sessions, fps_per_core=rate, max_cams=cams,
        reason="neither NVENC nor libx264 can encode every camera in real time",
        blocking=(
            f"No encoder on this machine can keep up with {n_cams} cameras at "
            f"{fps} fps: " + _nvenc_shortfall_text(sessions, n_cams) + " "
            + cpu_text + ". Record fewer cameras, or set "
            "`realtime_encode: false` in the rig profile to write raw frames "
            "and encode after the session — which needs ~500x the disk."))


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
    the numbers that were comfortable at 6 cameras are not at 9, and each of
    these limits currently fails SILENTLY — a camera dropping to `raw.bin`
    (~129 GiB/10 min for that camera alone: raw.bin holds the mono8 frame, so
    it is width*height bytes each, ~500x the H.264 size), a MemoryError inside
    a grab thread, or a disk filling mid-session.

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
    detail = (f"{need_gb:.1f} GiB needed ({pool_gb:.1f} pylon pool"
              + (f" + {ring_gb:.1f} NV12 ring" if realtime else "")
              + f"), {avail_gb:.1f} GiB available")
    if need_gb > avail_gb:
        # RULE: name the profile field, `max_num_buffer`. REASON: the pool
        # depth actually used comes from the profile; MAX_NUM_BUFFER in
        # camera_manager is only the fallback default, and "MaxNumBuffer" is
        # the pylon node name — neither string exists in the YAML the operator
        # must edit to act on this message.
        blocking.append(
            f"Not enough RAM for {n_cams} cameras: {detail}. Lower "
            f"`max_num_buffer` or `kick_max_lag` in the rig profile, or close "
            f"other applications.")
    elif need_gb > 0.75 * avail_gb:
        warnings.append(f"RAM is tight for {n_cams} cameras: {detail}.")

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
            # misbehaves: 6 cams x 100 fps x 2.3 MB x 600 s is ~772 GiB demanded
            # for what may be a one-minute test.
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


class HardwareCheckThread(QThread):
    """The launch survey, the NVENC session probe and the libx264 bench.

    RULE: the probe and the bench run HERE, not at Record. REASON: both cost
    seconds — the probe starts an interpreter, imports PyNvVideoCodec and
    allocates every session the driver will grant, and the bench runs two real
    encodes — and at Record they execute on the Qt main thread, freezing the
    window with no busy indicator, once per Record for as long as the count is
    short.
    """

    #: The report. RULE: new code connects `report_ready`. REASON: `finished`
    #: shadows QThread.finished, so anything relying on the built-in signal
    #: (deleteLater patterns, wait helpers) silently gets this one instead;
    #: `finished` is kept only until the main_window connection moves across,
    #: exactly as CalibrationWorker keeps its own.
    report_ready = pyqtSignal(object)
    finished = pyqtSignal(object)

    def __init__(self, output_dir: str = "", profile=None, n_cams: int = 0):
        super().__init__()
        self._output_dir = output_dir
        self._profile = profile
        self._n_cams = int(n_cams)

    def run(self):
        report = run_hardware_check(self._output_dir)
        if self._profile is not None:
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
        self.report_ready.emit(report)
        self.finished.emit(report)

    def _preflight_encoders(self, report: HardwareReport) -> None:
        p = self._profile
        n_cams = self._n_cams or int(getattr(p, "n_cameras", 0) or 1)
        fps = int(getattr(p, "frame_rate", 100) or 100)
        w = int(getattr(p, "frame_width", 1920) or 1920)
        h = int(getattr(p, "frame_height", 1200) or 1200)
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
