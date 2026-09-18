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
#: recordings. Named because the disk budget is only honest when the number it
#: multiplies is the one the chosen encode path actually writes.
H264_BYTES_PER_FRAME = 4600

#: Presets the launch bench measures, and that the preflight text reports.
BENCH_PRESETS = ("ultrafast", "veryfast")


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


def estimate_disk_speed(target_dir: Path, size_mb: int = 16) -> float:
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return -1.0
    test_file = target_dir / ".panopticon_speed_test"
    data = np.random.bytes(size_mb * 1024 * 1024)
    try:
        t0 = time.perf_counter()
        with open(test_file, "wb") as f:
            f.write(data)
            f.flush()
        elapsed = time.perf_counter() - t0
        return size_mb / elapsed if elapsed > 0 else -1.0
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
    report.disk_write_mb_s = estimate_disk_speed(target)
    report.has_nvenc = check_nvenc()
    report.nvenc_runtime = check_nvenc_runtime()
    _ffmpeg_nvenc_ok = report.has_nvenc

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
            "failed AND PyNvVideoCodec is unavailable. Encoding falls back to "
            "libx264 on the CPU for both the recording and the post-hoc passes. "
            "Set `encoder: x264` in the rig profile to make that the deliberate "
            "choice, and check the camera count in this report against the "
            "cameras you actually run."
        )
    elif not report.nvenc_runtime:
        report.warnings.append(
            "The real-time GPU encode path (PyNvVideoCodec) is unavailable, "
            "though ffmpeg's h264_nvenc works. Recording will use libx264 on "
            "the CPU or, with `realtime_encode: false`, raw.bin plus a "
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

    RULE: an NVENC init failure during a recording invalidates this cache.
    REASON: the cache keeps the HIGHEST count ever confirmed, and the early
    return means that once a probe granted enough sessions no later Record
    re-probes. Sessions taken afterwards by another process — an orphaned
    h264_nvenc ffmpeg from a tail merge, a browser's hardware encode — are
    then invisible to preflight, while the router's partial-failure path drops
    each camera beyond the cap onto raw.bin at ~129 GiB per 10 min with the
    4.6 KB/frame disk budget.

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
            _nvenc_sessions = max(got, _nvenc_sessions or 0)
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


def _install(encoder: str) -> None:
    """Point the real-time seam and the post-hoc writers at `encoder`.

    The grab threads and `SyncEncodeRouter` resolve `encoders.get_default_factory()`
    and never learn which encoder they got, so this one call is the whole
    switch. `ffmpeg_cmd` is switched alongside it because a machine that cannot
    encode in real time on the GPU cannot do the tail merge or the alignment
    re-encode there either.
    """
    if encoder == "x264":
        encoders.set_default_factory(cpu_encode.x264_factory)
        ffmpeg_cmd.set_default_backend("x264")
    elif encoder == "nvenc":
        encoders.set_default_factory(None)
        ffmpeg_cmd.set_default_backend("nvenc")
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

    if want == "raw" or not realtime:
        choice = EncoderChoice(
            encoder="raw",
            reason=("the profile selects raw capture, so frames are written "
                    "whole and encoded after the session"))
        _install("raw")
        return choice

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
    the disk budget uses.
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
    raw_mode = (not realtime) or path == "raw"
    encodes_realtime = not raw_mode

    if not raw_mode and path in ("auto", "nvenc"):
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
                f"~{frame_b*fps/2**30*600:.0f} GiB per 10 min. Watch the disk, "
                f"or set `encoder: x264` to encode on the CPU instead.")
        elif got < n_cams:
            if path == "auto" and cpu_cams >= n_cams:
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
                    f"fewer cameras, set `encoder: x264` in the rig profile to "
                    f"encode on the CPU, or set `realtime_encode: false` to "
                    f"put every camera on the raw path deliberately.")
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
                f"A shorter recording is fine — this assumes {minutes:g} minutes.")
        elif need_disk_gb > 0.8 * free_gb:
            warnings.append(
                f"Disk is tight: a {minutes:g}-minute recording needs "
                f"~{need_disk_gb:.0f} GiB of {free_gb:.0f} GiB free.")
    if not encodes_realtime and per_s / 2 ** 30 > 1.5:
        warnings.append(
            f"Raw capture will write {per_s / 2**30:.2f} GiB/s. Spread the "
            f"output across both NVMe drives — a single 990 PRO drops to "
            f"~1.6 GB/s once its SLC cache is exhausted.")
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
    if report.nvenc_sessions >= 0:
        lines.append(f"       {report.nvenc_sessions} concurrent encode sessions granted")
    # The CPU fallback's measured ceiling, so the operator can compare it with
    # the cameras they intend to run instead of discovering it mid-recording.
    for preset in BENCH_PRESETS:
        rate = report.x264_fps_per_core.get(preset, -1.0)
        if rate is None or rate < 0:
            continue
        cams = report.x264_max_cams.get(preset, 0)
        lines.append(f"x264:  {preset:<10} {rate:.0f} fps per core "
                     f"-> up to {cams} cameras")
    if report.encoder:
        lines.append(f"Using: {report.encoder}"
                     + (f" ({report.encoder_reason})" if report.encoder_reason else ""))
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
