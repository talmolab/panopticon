"""The launch preflight's branches, with every hardware probe stubbed.

These are the paths that decide whether a recording starts and how much disk
it is expected to need, and every one of them had been reached only by running
the GUI on a machine with the right defect. Here the NVENC session probe, the
libx264 bench and the ffmpeg capability test are all replaced, so the branch
under test is the only thing that varies.

No camera, no GPU, no trigger board, no ffmpeg required.
"""
import subprocess
import sys

from gui_app import cpu_encode, encoders, ffmpeg_cmd, hardware_check as hw

W, H, FPS = 1920, 1200, 100
N_CAMS = 6
RING_N, MAX_NUM_BUFFER = 64, 600


class FakeProfile:
    """The two RigProfile fields the encoder selection reads."""

    def __init__(self, encoder="auto", realtime_encode=True, n_cameras=N_CAMS):
        self.encoder = encoder
        self.realtime_encode = realtime_encode
        self.n_cameras = n_cameras
        self.frame_width, self.frame_height, self.frame_rate = W, H, FPS


def stub_sessions(monkey: dict, got: int):
    """Make the session probe answer `got` without touching a GPU."""
    monkey["nvenc_session_capacity"] = hw.nvenc_session_capacity
    hw.nvenc_session_capacity = lambda w, h, want, force=False: got


def stub_bench(fps_per_core: float):
    hw._x264_bench.clear()
    if fps_per_core > 0:
        for preset in hw.BENCH_PRESETS:
            hw._x264_bench[preset] = fps_per_core


def restore(monkey: dict):
    for name, value in monkey.items():
        setattr(hw, name, value)
    monkey.clear()


def capacity(got: int, bench: float = -1.0, encoder: str = "auto",
             realtime: bool = True, n_cams: int = N_CAMS):
    monkey: dict = {}
    stub_sessions(monkey, got)
    stub_bench(bench)
    hw._nvenc_probe_error = ""
    try:
        return hw.check_capacity(
            n_cams=n_cams, width=W, height=H, ring_n=RING_N,
            max_num_buffer=MAX_NUM_BUFFER, realtime=realtime,
            output_dir="", fps=FPS, encoder=encoder)
    finally:
        restore(monkey)


def test_unavailable_is_not_a_pass():
    blocking, _warnings = capacity(got=-1)
    assert blocking, "got == -1 (NVENC unavailable) must refuse, not pass"
    assert "unavailable" in blocking[0], blocking[0]
    blocking, _warnings = capacity(got=0)
    assert blocking and "no encode sessions" in blocking[0], blocking
    print("1) an unavailable GPU (-1) and a zero-session GPU both refuse: PASS")


def test_shortfall_and_surplus():
    blocking, _ = capacity(got=N_CAMS - 1)
    assert blocking and f"only {N_CAMS - 1}" in blocking[0], blocking
    blocking, warnings = capacity(got=N_CAMS + 2)
    assert not blocking, blocking
    assert not any("NVENC" in w for w in warnings), warnings
    print("2) n-1 sessions refuses and names the count; n+2 passes silently: PASS")


def test_disk_budget_follows_the_real_write_rate():
    frame_b = W * H
    for got, realtime, per_frame in ((N_CAMS + 2, True, hw.H264_BYTES_PER_FRAME),
                                     (-1, True, frame_b),
                                     (N_CAMS + 2, False, frame_b)):
        real_free = hw._get_disk_free
        hw._get_disk_free = lambda p: 1.0    # 1 GiB free forces the warning
        try:
            _b, warnings = capacity(got=got, realtime=realtime)
        finally:
            hw._get_disk_free = real_free
        need = N_CAMS * FPS * per_frame * 600 / 2 ** 30
        text = " ".join(warnings)
        assert f"~{need:.0f} GiB" in text, (got, realtime, need, text)
    print("3) the disk budget uses the H.264 rate only when the run really "
          "encodes in real time, and the full frame otherwise: PASS")


def test_cpu_fallback_covers_a_short_gpu():
    # Enough CPU for six cameras at 100 fps: 24 cores, 400 fps per core.
    blocking, warnings = capacity(got=1, bench=400.0, encoder="auto")
    assert not blocking, blocking
    assert any("libx264" in w for w in warnings), warnings
    # Not enough CPU: the refusal comes back.
    blocking, _ = capacity(got=1, bench=20.0, encoder="auto")
    assert blocking, "a short GPU and a short CPU must refuse"
    print("4) auto falls to libx264 with a warning when the CPU covers the "
          "cameras, and still refuses when it does not: PASS")


def test_forced_x264_is_checked_against_the_bench():
    blocking, warnings = capacity(got=0, bench=400.0, encoder="x264")
    assert not blocking, blocking
    blocking, _ = capacity(got=0, bench=20.0, encoder="x264")
    assert blocking and "libx264" in blocking[0], blocking
    _b, warnings = capacity(got=0, bench=-1.0, encoder="x264")
    assert any("not been measured" in w for w in warnings), warnings
    print("5) a forced x264 profile is refused when the bench is short and "
          "warned about when the bench never ran: PASS")


def test_probe_timeout_warns_and_never_blocks():
    monkey: dict = {}
    stub_sessions(monkey, -1)
    stub_bench(-1.0)
    hw._nvenc_probe_error = "probe did not finish within 120 s"
    try:
        blocking, warnings = hw.check_capacity(
            n_cams=N_CAMS, width=W, height=H, ring_n=RING_N,
            max_num_buffer=MAX_NUM_BUFFER, realtime=True, output_dir="",
            fps=FPS, encoder="auto")
    finally:
        hw._nvenc_probe_error = ""
        restore(monkey)
    assert not blocking, "an unprobeable cap is unknown, not zero"
    assert any("could not be probed" in w for w in warnings), warnings
    print("6) a session probe that timed out warns rather than refusing: PASS")


def test_isolated_probe_timeout_does_not_probe_in_process():
    from gui_app import nvenc

    real_run, real_inproc, real_avail = (subprocess.run,
                                         nvenc.probe_max_sessions,
                                         nvenc.available)
    hw.invalidate_nvenc_cache("test")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("probe", 0.01)

    def must_not_run(*a, **k):
        raise AssertionError("the in-process probe ran after a timeout")

    subprocess.run = boom
    nvenc.probe_max_sessions = must_not_run
    nvenc.available = lambda: True
    try:
        try:
            nvenc.probe_max_sessions_isolated(64, 64, limit=2, timeout=0.01)
        except TimeoutError as e:
            assert "did not finish" in str(e), e
        else:
            raise AssertionError("a timeout must raise TimeoutError, not "
                                 "return -1")
        got = hw.nvenc_session_capacity(64, 64, 3)
        assert got == -1, got
        assert "did not finish" in hw.nvenc_probe_error(), hw.nvenc_probe_error()
    finally:
        subprocess.run = real_run
        nvenc.probe_max_sessions = real_inproc
        nvenc.available = real_avail
        hw.invalidate_nvenc_cache("test")
    print("7) an isolated-probe timeout raises and the caller answers "
          "'unknown' instead of probing in-process: PASS")


def test_probe_counts_only_real_session_caps():
    from gui_app import nvenc

    class Enc:
        def EndEncode(self):
            return b""

    class FakeNvc:
        def __init__(self, message):
            self.message, self.n = message, 0

        def CreateEncoder(self, *a, **k):
            self.n += 1
            if self.n > 2:
                raise RuntimeError(self.message)
            return Enc()

    real_nvc, real_loaded = nvenc._nvc, nvenc._loaded
    nvenc._loaded = True
    try:
        for message in ("CreateEncoder Error code : 21", "Error code : 10"):
            nvenc._nvc = FakeNvc(message)
            assert nvenc.probe_max_sessions(64, 64, limit=5) == 2, message
        for message in ("Error code : 8", "something unparseable"):
            nvenc._nvc = FakeNvc(message)
            try:
                nvenc.probe_max_sessions(64, 64, limit=5)
            except RuntimeError:
                continue
            raise AssertionError(f"{message!r} was reported as a session cap")
    finally:
        nvenc._nvc, nvenc._loaded = real_nvc, real_loaded
    print("8) only NVENCSTATUS 21 and 10 end the session count; a config "
          "error is raised, not reported as a cap: PASS")


def test_cache_is_invalidated_after_an_init_failure():
    calls = []
    monkey: dict = {}
    hw.invalidate_nvenc_cache("test")
    real = hw.nvenc_session_capacity

    from gui_app import nvenc
    real_avail, real_iso = nvenc.available, nvenc.probe_max_sessions_isolated
    nvenc.available = lambda: True

    def counted(w, h, limit=24, timeout=120.0):
        calls.append(limit)
        return 12

    nvenc.probe_max_sessions_isolated = counted
    try:
        assert hw.nvenc_session_capacity(W, H, 8) == 12
        assert hw.nvenc_session_capacity(W, H, 8) == 12
        assert len(calls) == 1, "the second call must come from the cache"
        hw.invalidate_nvenc_cache("encoder init failed mid-recording")
        assert hw.nvenc_session_capacity(W, H, 8) == 12
        assert len(calls) == 2, "an invalidated cache must re-probe"
    finally:
        nvenc.available, nvenc.probe_max_sessions_isolated = real_avail, real_iso
        hw.nvenc_session_capacity = real
        hw.invalidate_nvenc_cache("test")
        restore(monkey)
    print("9) the session cache answers from memory until it is invalidated, "
          "then re-probes: PASS")


def test_select_encoder_installs_the_factory():
    previous = encoders.get_default_factory()
    previous_backend = ffmpeg_cmd.get_default_backend()
    monkey: dict = {}
    try:
        # Plenty of sessions -> NVENC, and the post-hoc backend follows.
        stub_sessions(monkey, N_CAMS + 2)
        stub_bench(400.0)
        choice = hw.select_encoder(FakeProfile("auto"), N_CAMS, FPS, W, H)
        assert choice.encoder == "nvenc" and not choice.blocking, choice
        assert encoders.get_default_factory() is encoders.nvenc_factory
        assert ffmpeg_cmd.get_default_backend() == "nvenc"

        # No sessions, a fast CPU -> libx264 on both sides.
        restore(monkey)
        stub_sessions(monkey, 0)
        choice = hw.select_encoder(FakeProfile("auto"), N_CAMS, FPS, W, H)
        assert choice.encoder == "x264" and not choice.blocking, choice
        assert encoders.get_default_factory() is cpu_encode.x264_factory
        assert ffmpeg_cmd.get_default_backend() == "x264"

        # No sessions, a slow CPU -> raw, and the start is refused.
        stub_bench(20.0)
        choice = hw.select_encoder(FakeProfile("auto"), N_CAMS, FPS, W, H)
        assert choice.encoder == "raw" and choice.blocking, choice
        assert "500x the disk" in choice.blocking, choice.blocking

        # The profile forcing a path is honoured even when it is short.
        stub_bench(20.0)
        choice = hw.select_encoder(FakeProfile("x264"), N_CAMS, FPS, W, H)
        assert choice.encoder == "x264" and choice.blocking, choice
        assert encoders.get_default_factory() is cpu_encode.x264_factory
        choice = hw.select_encoder(FakeProfile("raw"), N_CAMS, FPS, W, H)
        assert choice.encoder == "raw" and not choice.blocking, choice
        choice = hw.select_encoder(FakeProfile("auto", realtime_encode=False),
                                   N_CAMS, FPS, W, H)
        assert choice.encoder == "raw" and not choice.blocking, choice
    finally:
        restore(monkey)
        encoders.set_default_factory(previous)
        ffmpeg_cmd.set_default_backend(previous_backend)
        stub_bench(-1.0)
    print("10) select_encoder resolves auto/nvenc/x264/raw and installs the "
          "matching real-time factory and post-hoc backend: PASS")


def test_report_text_states_the_measured_limits():
    report = hw.HardwareReport(cpu_cores=24, cpu_threads=24, ram_total_gb=64.0,
                               ram_available_gb=50.0, disk_free_gb=3000.0,
                               has_nvenc=False, nvenc_runtime=False,
                               nvenc_sessions=0)
    report.x264_fps_per_core = {"ultrafast": 370.0, "veryfast": 110.0}
    report.x264_max_cams = {"ultrafast": 48, "veryfast": 20}
    report.encoder, report.encoder_reason = "x264", "the GPU granted nothing"
    text = hw.format_report(report)
    for needle in ("370 fps per core", "up to 48 cameras", "110 fps per core",
                   "up to 20 cameras", "Using: x264", "NOT available"):
        assert needle in text, (needle, text)
    print("11) the preflight text states the measured fps per core and the "
          "camera count for both presets: PASS")


def test_thread_exposes_a_non_shadowing_signal():
    thread = hw.HardwareCheckThread("")
    assert hasattr(thread, "report_ready"), "report_ready is the new name"
    assert hasattr(thread, "finished"), "finished stays until main_window moves"
    print("12) HardwareCheckThread emits report_ready and still has the "
          "compatibility signal: PASS")


def test_disk_test_is_real_and_optional():
    import tempfile
    from pathlib import Path

    assert hw.estimate_disk_speed(None) == -1.0, (
        "no output directory means no test, never a write into the repo")
    tmp = Path(tempfile.mkdtemp(prefix="p9disk_"))
    try:
        rate = hw.estimate_disk_speed(tmp, size_mb=32)
        assert rate > 0, rate
        assert not list(tmp.iterdir()), "the test file must be removed"
    finally:
        import shutil as _shutil
        _shutil.rmtree(tmp, ignore_errors=True)
    assert hw.run_hardware_check("").disk_write_mb_s == -1.0
    print("13) the disk test fsyncs, cleans up, and is skipped when no output "
          "directory is configured: PASS")


def test_raw_warning_states_a_rule_not_this_rig():
    monkey: dict = {}
    stub_sessions(monkey, 0)
    stub_bench(-1.0)
    try:
        _b, warnings = hw.check_capacity(
            n_cams=9, width=W, height=H, ring_n=RING_N,
            max_num_buffer=MAX_NUM_BUFFER, realtime=False, output_dir="",
            fps=FPS, encoder="raw")
    finally:
        restore(monkey)
    text = " ".join(warnings)
    assert "GiB/s" in text, text
    for rig_specific in ("990 PRO", "both NVMe"):
        assert rig_specific not in text, text
    print("14) the raw-capture warning states the sustained-write rule "
          "instead of this rig's drive models: PASS")


def test_monochrome_capability_is_read_not_assumed():
    from gui_app import nvenc

    class FakeNvc:
        def __init__(self, caps):
            self.caps = caps

        def GetEncoderCaps(self, gpuid=0, codec="h264"):
            if self.caps is None:
                raise RuntimeError("no caps here")
            return self.caps

    real_nvc, real_loaded = nvenc._nvc, nvenc._loaded
    nvenc._loaded = True
    try:
        nvenc._nvc = FakeNvc({"support_monochrome": 1})
        assert nvenc.probe_monochrome_support() == 1
        nvenc._nvc = FakeNvc({"support_monochrome": 0})
        assert nvenc.probe_monochrome_support() == 0
        nvenc._nvc = FakeNvc({})
        assert nvenc.probe_monochrome_support() == -1, "absent means unknown"
        nvenc._nvc = FakeNvc(None)
        assert nvenc.probe_monochrome_support() == -1, "a raising query is unknown"
        nvenc._nvc = None
        assert nvenc.probe_monochrome_support() == -1, "no NVENC means unknown"
    finally:
        nvenc._nvc, nvenc._loaded = real_nvc, real_loaded
    print("15) monochrome support is read from the encoder caps, and an "
          "absent or failing query answers 'unknown', not 'yes': PASS")


def main():
    test_unavailable_is_not_a_pass()
    test_shortfall_and_surplus()
    test_disk_budget_follows_the_real_write_rate()
    test_cpu_fallback_covers_a_short_gpu()
    test_forced_x264_is_checked_against_the_bench()
    test_probe_timeout_warns_and_never_blocks()
    test_isolated_probe_timeout_does_not_probe_in_process()
    test_probe_counts_only_real_session_caps()
    test_cache_is_invalidated_after_an_init_failure()
    test_select_encoder_installs_the_factory()
    test_report_text_states_the_measured_limits()
    test_thread_exposes_a_non_shadowing_signal()
    test_disk_test_is_real_and_optional()
    test_raw_warning_states_a_rule_not_this_rig()
    test_monochrome_capability_is_read_not_assumed()
    print("\nALL HARDWARE CHECK TESTS PASS")


if __name__ == "__main__":
    sys.exit(main())
