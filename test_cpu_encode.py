"""The CPU H.264 fallback: command invariants, a full router round trip, teardown.

Runs entirely offline — no camera, no GPU, no trigger board. The only external
dependency is the bundled ffmpeg binary, which every post-hoc writer already
needs; without it the suite skips rather than fails, because a machine with no
ffmpeg cannot exercise libx264 either.

What is proved here:
  1-3) the command line carries every invariant the labeler depends on;
  4)   the encoder satisfies the gui_app.encoders duck type;
  5)   two cameras' worth of frames go through SyncEncodeRouter with the x264
       factory and come back as a stream whose frame count equals the recorded
       block IDs, with one IDR per second, remuxable with `-c copy`;
  6-8) EndEncode is clean, kill() is the abandon path, and a dead child makes
       Encode raise instead of silently dropping frames;
  9)   the bench and the camera-count arithmetic.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from gui_app import cpu_encode, encoders, ffmpeg_cmd

W, H, FPS, QP = 640, 400, 100, 21
N_FRAMES = 200


def have_ffmpeg() -> bool:
    try:
        return Path(ffmpeg_cmd.ffmpeg_exe()).exists()
    except Exception:
        return False


def nal_types(stream: bytes) -> list:
    """NAL unit types in an Annex-B elementary stream, in order.

    Parsing the stream directly rather than asking ffprobe: the bundled
    imageio-ffmpeg wheel ships ffmpeg only, and the two facts this suite needs
    (how many coded pictures, how many of them are IDRs) are one byte each
    after every start code.
    """
    out = []
    i = 0
    while True:
        j = stream.find(b"\x00\x00\x01", i)
        if j < 0:
            return out
        out.append(stream[j + 3] & 0x1F)
        i = j + 3


def make_frame(w: int, h: int, i: int) -> np.ndarray:
    """One NV12 frame with content that actually costs bits to encode.

    A flat frame compresses to almost nothing, which would make a broken
    encoder and a working one produce similarly small streams.
    """
    nv12 = np.full((h * 3 // 2, w), 128, np.uint8)
    y = np.arange(w, dtype=np.int32)[None, :] + np.arange(h, dtype=np.int32)[:, None]
    nv12[:h] = ((y + i * 7) % 256).astype(np.uint8)
    return nv12


def decode_frame_count(mp4: Path, w: int, h: int) -> int:
    """Frames an mp4 actually decodes to, counted by raw output bytes."""
    cmd = ([ffmpeg_cmd.ffmpeg_exe()] + ffmpeg_cmd.global_args()
           + ["-i", str(mp4), "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    proc = subprocess.run(cmd, capture_output=True,
                          **ffmpeg_cmd.quiet_popen_kwargs())
    assert proc.returncode == 0, proc.stderr[-400:]
    assert len(proc.stdout) % (w * h) == 0, len(proc.stdout)
    return len(proc.stdout) // (w * h)


def test_command_invariants():
    cmd = cpu_encode.x264_pipe_command(W, H, FPS, QP)
    assert cmd[-1] == "pipe:1" and "pipe:0" in cmd, cmd
    assert cmd[cmd.index("-g") + 1] == str(FPS), cmd
    assert cmd[cmd.index("-bf") + 1] == "0", cmd
    assert cmd[cmd.index("-c:v") + 1] == "libx264", cmd
    assert cmd[cmd.index("-s") + 1] == f"{W}x{H}", cmd
    assert cmd[cmd.index("-tune") + 1] == "zerolatency", cmd
    assert "gray" in cmd and "yuv420p" in cmd, cmd
    print("1) the pipe command has -g fps, no B-frames, gray in / yuv420p out: PASS")


def test_gop_follows_fps():
    for fps in (30, 60, 100):
        cmd = cpu_encode.x264_pipe_command(W, H, fps, QP)
        assert cmd[cmd.index("-g") + 1] == str(fps), cmd
        assert cmd[cmd.index("-r") + 1] == str(fps), cmd
    print("2) the GOP follows the profile frame rate, not a constant: PASS")


def test_preset_is_validated():
    for bad in ("placebo", "", "ULTRAFAST"):
        try:
            cpu_encode.x264_encoder_args(FPS, QP, preset=bad)
        except ValueError:
            continue
        raise AssertionError(f"preset {bad!r} was accepted")
    try:
        cpu_encode.set_factory_options(preset="placebo")
    except ValueError:
        pass
    else:
        raise AssertionError("set_factory_options accepted an unknown preset")
    print("3) an unknown preset is refused rather than passed to ffmpeg: PASS")


def test_protocol_conformance():
    enc = cpu_encode.create_x264_encoder(64, 64, FPS, QP)
    try:
        assert isinstance(enc, encoders.EncoderProtocol)
        assert callable(getattr(enc, "Close", None))
        assert callable(getattr(enc, "kill", None))
    finally:
        enc.EndEncode()
        enc.Close()
    print("4) X264Encoder satisfies the encoders.EncoderProtocol duck type: PASS")


def test_router_round_trip():
    from gui_app.sync_encode import SyncEncodeRouter

    tmp = Path(tempfile.mkdtemp(prefix="p9x264_"))
    previous = encoders.set_default_factory(cpu_encode.x264_factory)
    try:
        raw_paths = []
        for cam in range(2):
            d = tmp / f"cam{cam + 1}"
            d.mkdir()
            raw_paths.append(str(d / "raw.bin"))
        router = SyncEncodeRouter(raw_paths, W, H, QP, fps=FPS, max_lag=64)
        assert router.available, router.unavailable_reason
        # The factory's degradation note must reach the recording, not only the
        # console: an operator reads WARNINGS.txt, not stdout.
        assert any("libx264" in w for w in router.warnings), router.warnings
        router.start()
        frames = [make_frame(W, H, i) for i in range(N_FRAMES)]
        for i, nv12 in enumerate(frames):
            for cam in range(2):
                router.submit(cam, i + 1, i / FPS, nv12)
        counts = router.stop()
        assert router.dropped_full == 0, router.dropped_full

        for cam in range(2):
            n_ids, _ts, block_ids = counts[cam]
            assert n_ids == N_FRAMES == len(block_ids), (cam, n_ids)
            stream = (tmp / f"cam{cam + 1}" / "stream.h264").read_bytes()
            types = nal_types(stream)
            slices = [t for t in types if t in (1, 5)]
            idrs = [t for t in types if t == 5]
            assert len(slices) == n_ids, (cam, len(slices), n_ids)
            assert len(idrs) == -(-N_FRAMES // FPS), (cam, len(idrs))

            mp4 = tmp / f"cam{cam + 1}" / "video.mp4"
            cmd = ([ffmpeg_cmd.ffmpeg_exe()] + ffmpeg_cmd.global_args()
                   + ["-fflags", "+genpts", "-r", str(FPS),
                      "-i", str(tmp / f"cam{cam + 1}" / "stream.h264")]
                   + ffmpeg_cmd.stream_copy_args() + [str(mp4)])
            ffmpeg_cmd.check_mp4_command(cmd, FPS)
            proc = subprocess.run(cmd, capture_output=True,
                                  **ffmpeg_cmd.quiet_popen_kwargs())
            assert proc.returncode == 0, proc.stderr[-400:]
            assert decode_frame_count(mp4, W, H) == n_ids, cam
        print("5) 2 cameras x 200 frames through SyncEncodeRouter: frame count "
              "== block IDs, one IDR per second, remuxes with -c copy: PASS")
    finally:
        encoders.set_default_factory(previous)
        shutil.rmtree(tmp, ignore_errors=True)


def test_end_encode_is_clean_and_idempotent():
    enc = cpu_encode.create_x264_encoder(W, H, FPS, QP)
    for i in range(10):
        enc.Encode(make_frame(W, H, i))
    tail = enc.EndEncode()
    assert enc.returncode == 0, enc.returncode
    assert enc.EndEncode() == b"", "a second EndEncode must not raise or invent bytes"
    enc.Close()
    enc.Close()
    assert isinstance(tail, bytes)
    print("6) EndEncode drains the child, exits 0 and is idempotent; Close is "
          "safe twice: PASS")


def test_kill_path():
    enc = cpu_encode.create_x264_encoder(W, H, FPS, QP)
    for i in range(5):
        enc.Encode(make_frame(W, H, i))
    enc.kill()
    assert enc.returncode is not None, "kill() left the child running"
    try:
        enc.Encode(make_frame(W, H, 99))
    except RuntimeError:
        pass
    else:
        raise AssertionError("Encode after kill() must raise, not drop frames")
    enc.Close()
    print("7) kill() ends the child and a later Encode raises: PASS")


def test_dead_child_raises():
    enc = cpu_encode.create_x264_encoder(W, H, FPS, QP)
    enc._proc.kill()
    enc._proc.wait(timeout=10)
    raised = False
    for i in range(50):
        try:
            enc.Encode(make_frame(W, H, i))
        except RuntimeError as e:
            raised = "died" in str(e)
            break
    assert raised, "a dead ffmpeg must surface as RuntimeError from Encode"
    enc.Close()
    print("8) a child that dies mid-stream makes Encode raise, so the encoder "
          "thread spills raw instead of losing frames: PASS")


def test_bench_and_camera_count():
    rate = cpu_encode.x264_bench(256, 256, 50, preset="ultrafast", seconds=0.5)
    assert rate > 0, rate
    cores = 24
    assert cpu_encode.sustainable_cameras(0.0, 100, cores) == 0
    assert cpu_encode.sustainable_cameras(-1.0, 100, cores) == 0
    assert cpu_encode.sustainable_cameras(100.0, 100, 1) == 0, "one core is reserved"
    # 100 fps per core at 100 fps: 1.0 core of encode + 0.2 of capture per
    # camera, 23 usable cores -> 19 cameras.
    assert cpu_encode.sustainable_cameras(100.0, 100, cores) == 19
    # Halving the per-core rate must not raise the camera count.
    assert (cpu_encode.sustainable_cameras(50.0, 100, cores)
            <= cpu_encode.sustainable_cameras(100.0, 100, cores))
    print("9) the bench returns a positive rate and the camera count is "
          "monotonic, floored and reserves a core: PASS")


def main():
    if not have_ffmpeg():
        print("SKIP: no ffmpeg binary available; the CPU fallback cannot run here")
        return
    test_command_invariants()
    test_gop_follows_fps()
    test_preset_is_validated()
    test_protocol_conformance()
    test_router_round_trip()
    test_end_encode_is_clean_and_idempotent()
    test_kill_path()
    test_dead_child_raises()
    test_bench_and_camera_count()
    print("\nALL CPU ENCODE TESTS PASS")


if __name__ == "__main__":
    main()
