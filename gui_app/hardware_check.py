"""Startup hardware screening — warns about insufficient resources."""
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import psutil
from imageio_ffmpeg import get_ffmpeg_exe
from PyQt5.QtCore import QThread, pyqtSignal


@dataclass
class HardwareReport:
    cpu_cores: int = 0
    cpu_threads: int = 0
    ram_total_gb: float = 0.0
    ram_available_gb: float = 0.0
    disk_free_gb: float = 0.0
    disk_write_mb_s: float = -1.0
    has_nvenc: bool = False
    warnings: list = field(default_factory=list)


def check_nvenc() -> bool:
    try:
        ffmpeg = get_ffmpeg_exe()
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        return "h264_nvenc" in result.stdout
    except Exception:
        return False


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


def run_hardware_check(output_dir: str = "") -> HardwareReport:
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
    if not report.has_nvenc:
        # There is NO CPU fallback: every mp4 writer hardcodes `-c:v h264_nvenc`
        # (encode_worker._cmd, encode_worker._append_raw_tail,
        # alignment.extract_aligned). Without it those passes fail outright, so
        # do not describe this as merely slower.
        report.warnings.append(
            "NVENC not found in ffmpeg — there is no CPU fallback, so encoding "
            "raw.bin to mp4 and the post-hoc alignment re-encode will FAIL. "
            "(The real-time path's .h264 -> mp4 step is a stream copy and is "
            "unaffected, but a recording that falls back to raw would be "
            "stranded.)"
        )

    return report


_nvenc_sessions: int | None = None    # highest count CONFIRMED grantable
_nvenc_saturated = False              # last probe stopped at its limit, not at a failure


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
    global _nvenc_sessions, _nvenc_saturated
    if _nvenc_sessions is not None and not force and _nvenc_sessions >= want:
        return _nvenc_sessions
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
            got = nvenc.probe_max_sessions_isolated(width, height, limit=limit)
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


def check_capacity(n_cams: int, width: int, height: int,
                   ring_n: int, max_num_buffer: int,
                   realtime: bool, output_dir: str = "",
                   minutes: float = 10.0, fps: int = 100) -> tuple[list, list]:
    """Refuse-or-warn check run at acquisition start. Returns (blocking, warnings).

    Everything here scales linearly with camera count, which is why it exists:
    the numbers that were comfortable at 6 cameras are not at 9, and each of
    these limits currently fails SILENTLY — a camera dropping to `raw.bin`
    (~129 GiB/10 min for that camera alone: raw.bin holds the mono8 frame, so
    it is width*height bytes each, ~500x the H.264 size), a MemoryError inside
    a grab thread, or a disk filling mid-session.
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
        blocking.append(
            f"Not enough RAM for {n_cams} cameras: {detail}. Lower "
            f"MaxNumBuffer or kick_max_lag, or close other applications.")
    elif need_gb > 0.75 * avail_gb:
        warnings.append(f"RAM is tight for {n_cams} cameras: {detail}.")

    # --- NVENC sessions ------------------------------------------------------
    if realtime:
        got = nvenc_session_capacity(width, height, n_cams + 2)
        if got == 0:
            blocking.append("NVENC granted no encode sessions, so real-time "
                            "encoding cannot start. To record anyway, set "
                            "`realtime_encode: false` in the rig profile — that "
                            "writes raw frames and encodes after the session, "
                            "which needs ~500x the disk.")
        elif 0 < got < n_cams:
            blocking.append(
                f"NVENC granted only {got} concurrent sessions but {n_cams} "
                f"cameras need one each. The driver caps this. Cameras beyond "
                f"the cap would silently fall back to raw.bin at ~{frame_b*fps/2**30*600:.0f} "
                f"GiB per 10 min each. Record fewer cameras, or set "
                f"`realtime_encode: false` in the rig profile to put every "
                f"camera on the raw path deliberately.")

    # --- disk ----------------------------------------------------------------
    # Real-time H.264 is ~4.6 KB/frame; raw is the full frame every frame.
    per_s = n_cams * fps * (4600 if realtime else frame_b)
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
    if not realtime and per_s / 2 ** 30 > 1.5:
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
    lines.append(f"NVENC: {'Available' if report.has_nvenc else 'Not found'}")
    if report.warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in report.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines)


class HardwareCheckThread(QThread):
    finished = pyqtSignal(object)

    def __init__(self, output_dir: str = ""):
        super().__init__()
        self._output_dir = output_dir

    def run(self):
        report = run_hardware_check(self._output_dir)
        self.finished.emit(report)
