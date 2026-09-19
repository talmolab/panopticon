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
             realtime: bool = True, n_cams: int = N_CAMS,
             cpu_installed: bool = False, selection_ran: bool = True):
    """check_capacity with every probe stubbed.

    `cpu_installed` is the seam `select_encoder` would have moved, and
    `selection_ran` is whether that selection ran at all: both are read by
    check_capacity, so both are part of the case, not of the fixture.
    """
    monkey: dict = {}
    stub_sessions(monkey, got)
    stub_bench(bench)
    hw._nvenc_probe_error = ""
    previous_factory = encoders.set_default_factory(
        cpu_encode.x264_factory if cpu_installed else None)
    previous_selected = hw._selected_encoder
    hw._selected_encoder = "x264" if selection_ran else ""
    try:
        return hw.check_capacity(
            n_cams=n_cams, width=W, height=H, ring_n=RING_N,
            max_num_buffer=MAX_NUM_BUFFER, realtime=realtime,
            output_dir="", fps=FPS, encoder=encoder)
    finally:
        hw._selected_encoder = previous_selected
        encoders.set_default_factory(previous_factory)
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
    # Enough CPU for six cameras at 100 fps: 24 cores, 400 fps per core, and
    # the selection installed the CPU factory.
    blocking, warnings = capacity(got=1, bench=400.0, encoder="auto",
                                  cpu_installed=True)
    assert not blocking, blocking
    assert any("libx264" in w for w in warnings), warnings
    # Not enough CPU: the refusal comes back.
    blocking, _ = capacity(got=1, bench=20.0, encoder="auto",
                           cpu_installed=True)
    assert blocking, "a short GPU and a short CPU must refuse"
    print("4) auto falls to libx264 with a warning when the CPU covers the "
          "cameras, and still refuses when it does not: PASS")


def test_cpu_claim_is_gated_on_the_installed_seam():
    """A bench result alone must not pass a start onto a GPU factory.

    The bench and the selection are separate calls and the preflight thread
    swallows an exception from the second, so "recording on the CPU instead"
    has to be read off the seam.
    """
    blocking, warnings = capacity(got=1, bench=400.0, encoder="auto",
                                  cpu_installed=False)
    assert blocking, ("a bench that covers the cameras must not pass the start "
                      "while the GPU factory is still installed")
    assert not any("Recording on the CPU" in w for w in warnings), warnings
    print("16) the CPU-fallback warning is gated on the installed factory, not "
          "on the bench cache: PASS")


def test_preflight_failure_drops_the_bench_result():
    """A selection that raises must not leave a bench that reads as live."""
    real_select, real_bench = hw.select_encoder, hw.run_x264_bench

    def boom(*a, **k):
        raise RuntimeError("selection exploded")

    def fake_bench(w, h, fps, presets=hw.BENCH_PRESETS):
        for preset in presets:
            hw._x264_bench[preset] = 400.0
        return dict(hw._x264_bench)

    real_check = hw.run_hardware_check
    report = hw.HardwareReport()
    hw.select_encoder, hw.run_x264_bench = boom, fake_bench
    hw.run_hardware_check = lambda output_dir="": report
    try:
        thread = hw.HardwareCheckThread("", profile=FakeProfile(), n_cams=N_CAMS)
        thread.run()
    finally:
        hw.select_encoder, hw.run_x264_bench = real_select, real_bench
        hw.run_hardware_check = real_check
    try:
        assert hw.x264_bench_fps("ultrafast") == -1.0, hw._x264_bench
        assert not report.x264_fps_per_core, report.x264_fps_per_core
        # And the branch check_capacity would take off that cache.
        blocking, warnings = capacity(got=0, encoder="auto",
                                      bench=hw.x264_bench_fps("ultrafast"))
        assert blocking, "a cleared bench must not pass the start"
    finally:
        hw._x264_bench.clear()
    print("17) a preflight whose selection raises clears the bench cache, so "
          "check_capacity cannot read it as a live CPU path: PASS")


def test_encoder_raw_without_realtime_encode_is_refused():
    """`encoder: raw` does not switch the capture path; only `realtime_encode`.

    With the two disagreeing the run really encodes in real time, so budgeting
    raw bytes and skipping the session check is how every camera ends up on
    raw.bin with a preflight that said nothing.
    """
    for got in (-1, 0, N_CAMS + 2):
        blocking, _warnings = capacity(got=got, encoder="raw", realtime=True)
        assert blocking, (got, "encoder: raw with realtime_encode: true must "
                               "not pass silently")
        assert "realtime_encode" in blocking[0], blocking[0]
    # The honest combination still passes.
    blocking, _warnings = capacity(got=-1, encoder="raw", realtime=False)
    assert not blocking, blocking

    previous = encoders.get_default_factory()
    previous_backend = ffmpeg_cmd.get_default_backend()
    monkey: dict = {}
    try:
        stub_sessions(monkey, -1)
        stub_bench(-1.0)
        choice = hw.select_encoder(FakeProfile("raw"), N_CAMS, FPS, W, H)
        assert choice.encoder == "raw" and choice.blocking, choice
        assert "realtime_encode: false" in choice.blocking, choice.blocking
        choice = hw.select_encoder(FakeProfile("raw", realtime_encode=False),
                                   N_CAMS, FPS, W, H)
        assert choice.encoder == "raw" and not choice.blocking, choice
    finally:
        restore(monkey)
        encoders.set_default_factory(previous)
        ffmpeg_cmd.set_default_backend(previous_backend)
    print("18) `encoder: raw` with `realtime_encode: true` is refused by both "
          "select_encoder and check_capacity, naming the field that acts: PASS")


def test_advice_names_a_field_that_does_something():
    """The refusal must not tell the operator to set a field nothing reads."""
    blocking, _ = capacity(got=0, bench=20.0, encoder="auto",
                           selection_ran=False)
    assert blocking, blocking
    assert "encoder: x264" not in blocking[0], blocking[0]
    assert "cannot be selected from the rig profile" in blocking[0], blocking[0]
    blocking, _ = capacity(got=0, bench=20.0, encoder="auto",
                           selection_ran=True)
    assert blocking and "`encoder: x264`" in blocking[0], blocking[0]
    print("19) the CPU-path advice appears only where the encoder selection "
          "actually runs: PASS")


def test_disk_estimate_on_the_cpu_path_is_a_lower_bound():
    real_free = hw._get_disk_free
    hw._get_disk_free = lambda p: 1.0          # 1 GiB free forces the warning
    try:
        _b, warnings = capacity(got=0, bench=400.0, encoder="x264")
        assert any("lower bound" in w for w in warnings), warnings
        _b, warnings = capacity(got=N_CAMS + 2, bench=-1.0, encoder="nvenc")
        assert not any("lower bound" in w for w in warnings), warnings
    finally:
        hw._get_disk_free = real_free
    print("20) the disk estimate is labelled a lower bound on the CPU path, "
          "where the measured bytes-per-frame came from NVENC: PASS")


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
        real_ffmpeg_ok = hw._ffmpeg_nvenc_ok
        hw._ffmpeg_nvenc_ok = True
        choice = hw.select_encoder(FakeProfile("auto"), N_CAMS, FPS, W, H)
        assert choice.encoder == "nvenc" and not choice.blocking, choice
        assert encoders.get_default_factory() is encoders.nvenc_factory
        assert ffmpeg_cmd.get_default_backend() == "nvenc"

        # The real-time path can be NVENC while ffmpeg's h264_nvenc is broken;
        # the post-hoc writers must follow the measurement, not the choice.
        hw._ffmpeg_nvenc_ok = False
        choice = hw.select_encoder(FakeProfile("nvenc"), N_CAMS, FPS, W, H)
        assert choice.encoder == "nvenc" and not choice.blocking, choice
        assert encoders.get_default_factory() is encoders.nvenc_factory
        assert ffmpeg_cmd.get_default_backend() == "x264", (
            "the post-hoc backend must not point at a proven-broken h264_nvenc")
        hw._ffmpeg_nvenc_ok = real_ffmpeg_ok

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
    # A report with no selection must say so: a missing `Using:` line reads as
    # "NVENC, as configured" while it means the profile was never consulted.
    report.encoder, report.encoder_reason = "", ""
    text = hw.format_report(report)
    assert "the encoder selection did not run" in text, text
    print("11) the preflight text states the measured fps per core and the "
          "camera count for every benched preset, and says when no encoder "
          "was selected: PASS")


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
    # Stubbed, because this suite starts no ffmpeg and allocates no GPU
    # session: run_hardware_check would otherwise run a real h264_nvenc test
    # encode, load PyNvVideoCodec, and leave `_ffmpeg_nvenc_ok` set for every
    # later case.
    monkey: dict = {"check_nvenc": hw.check_nvenc,
                    "check_nvenc_runtime": hw.check_nvenc_runtime}
    previous_ok = hw._ffmpeg_nvenc_ok
    hw.check_nvenc = lambda: False
    hw.check_nvenc_runtime = lambda: False
    try:
        assert hw.run_hardware_check("").disk_write_mb_s == -1.0
    finally:
        restore(monkey)
        hw._ffmpeg_nvenc_ok = previous_ok
    assert hw._ffmpeg_nvenc_ok is previous_ok, (
        "the survey's module state must not leak into the later cases")
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
    test_cpu_claim_is_gated_on_the_installed_seam()
    test_preflight_failure_drops_the_bench_result()
    test_encoder_raw_without_realtime_encode_is_refused()
    test_advice_names_a_field_that_does_something()
    test_disk_estimate_on_the_cpu_path_is_a_lower_bound()
    print("\nALL HARDWARE CHECK TESTS PASS")


if __name__ == "__main__":
    sys.exit(main())
