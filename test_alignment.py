"""Offline tests for the post-hoc pipeline: alignment, encode_worker, ffmpeg_cmd,
stim_trace and the two CLIs.

No camera, no GPU, no real ffmpeg: every ffmpeg launch goes to a stub script
whose behaviour (succeed, exit non-zero, write a truncated file, fail one stage
or one camera) is chosen per case through environment variables, and the video
decoder is replaced by a synthetic frame source. What is under test is the
bookkeeping around ffmpeg: which files survive, what the metadata says, and
what the summaries report. Those decide whether a recording is replaced,
truncated or kept, so they are the branches worth pinning.

Run with the project interpreter (needs numpy and PyQt5):
    uv run python test_alignment.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from gui_app import alignment, ffmpeg_cmd, stim_trace  # noqa: E402
from gui_app.alignment import _unwrap_blockids  # noqa: E402

PY = sys.executable

# --------------------------------------------------------------------- stub ffmpeg
STUB_SRC = r'''
"""ffmpeg stand-in. Behaviour from FFSTUB_MODE:
  ok            exit 0, write 4096 bytes to the output (last argument)
  fail          exit 1, write nothing
  truncate      exit 0, write 16 bytes (an "mp4" too small to be real)
  tail_fail     exit 1 for a '-f h264' (tail) command, ok otherwise
  fail_match:S  exit 1 when the output path contains S, ok otherwise
Every invocation's argv is appended to FFSTUB_LOG as one JSON line.
"""
import json, os, sys
args = sys.argv[1:]
log = os.environ.get("FFSTUB_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(args) + "\n")
if "-" in args:  # rawvideo on stdin: drain it so the writer never blocks
    sys.stdin.buffer.read()
mode = os.environ.get("FFSTUB_MODE", "ok")
out = args[-1]
def write(n):
    with open(out, "wb") as f:
        f.write(b"STUB" * (n // 4))
if mode == "fail":
    sys.stderr.write("stub: simulated encoder failure\n"); sys.exit(1)
if mode == "truncate":
    write(16); sys.exit(0)
try:  # output format = the '-f' after '-i' (absent for an mp4 output)
    out_fmt = args[args.index("-f", args.index("-i")) + 1]
except ValueError:
    out_fmt = None
if mode == "tail_fail" and out_fmt == "h264":
    sys.stderr.write("stub: tail encode failed\n"); sys.exit(1)
if mode.startswith("fail_match:") and mode.split(":", 1)[1] in out:
    sys.stderr.write("stub: simulated failure for " + out + "\n"); sys.exit(1)
write(4096)
sys.exit(0)
'''


def make_stub(tmp: Path) -> str:
    """Write the stub and a launcher ffmpeg_cmd.ffmpeg_exe() can return."""
    script = tmp / "ffstub.py"
    script.write_text(STUB_SRC)
    if sys.platform == "win32":
        bat = tmp / "ffstub.bat"
        bat.write_text(f'@echo off\r\n"{PY}" "{script}" %*\r\nexit /b %ERRORLEVEL%\r\n')
        return str(bat)
    sh = tmp / "ffstub.sh"
    sh.write_text(f'#!/bin/sh\nexec "{PY}" "{script}" "$@"\n')
    sh.chmod(0o755)
    return str(sh)


def stub_mode(mode: str, log: Path | None = None):
    os.environ["FFSTUB_MODE"] = mode
    if log is not None:
        log.write_text("")
        os.environ["FFSTUB_LOG"] = str(log)
    else:
        os.environ.pop("FFSTUB_LOG", None)


def read_log(log: Path) -> list:
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]


# ------------------------------------------------------------------ fixtures
def synth_recording(root: Path, blocks: dict, fps=100, video_bytes=b"ORIGINAL" * 256,
                    with_video=True, times=None) -> Path:
    """Write cam*/blockids.npy (+ frametimes.npy, + an mp4) under root/recording."""
    rec = root / "recording"
    rec.mkdir(parents=True, exist_ok=True)
    for cam, b in blocks.items():
        d = rec / cam
        d.mkdir(exist_ok=True)
        b = np.asarray(b, dtype=np.int64)
        np.save(d / "blockids.npy", b)
        ts = (times[cam] if times and cam in times
              else (b - b[0]) / fps)
        np.save(d / "frametimes.npy",
                np.stack([np.arange(1, b.size + 1, dtype=np.float64),
                          np.asarray(ts, dtype=np.float64)]))
        if with_video is True or (isinstance(with_video, (set, list)) and cam in with_video):
            (d / f"20260101-s1-{cam}-recording.mp4").write_bytes(video_bytes)
    return rec


def fake_reader(n_frames: int, w=8, h=4):
    """Stand-in for alignment._open_gray_reader: n synthetic gray frames."""
    def _open(video):
        def gen():
            for i in range(n_frames):
                yield bytes([i % 256]) * (w * h)
        g = gen()
        return w, h, g
    return _open


# ---------------------------------------------------------------------- tests
def test_unwrap():
    assert _unwrap_blockids(np.array([1, 2, 3])).tolist() == [1, 2, 3]
    # wrap 65535 -> 1
    assert _unwrap_blockids(np.array([65534, 65535, 1, 2])).tolist() == \
        [65534, 65535, 65536, 65537]
    # drops across the wrap still unwrap
    assert _unwrap_blockids(np.array([65533, 65535, 3])).tolist() == \
        [65533, 65535, 65538]
    for bad in ([40000, 40001, -1, 40003], [100, 101, -1, 103], [0, 1, 2],
                [65534, 65535, 0, 1]):
        try:
            _unwrap_blockids(np.array(bad))
        except ValueError as e:
            assert "non-positive" in str(e), e
        else:
            raise AssertionError(f"{bad} accepted; -1/0 must never read as a wrap")
    try:
        _unwrap_blockids(np.array([5, 4, 6]))
    except ValueError as e:
        assert "monotonic" in str(e)
    else:
        raise AssertionError("small decrease accepted")
    print("1) unwrap: wrap, drops across wrap, -1/0 rejected, non-monotonic rejected: PASS")


def test_compute_alignment():
    blocks = [np.array([1, 2, 3, 4, 5]), np.array([1, 3, 4, 5]), np.array([1, 2, 3, 5])]
    common, fi = alignment.compute_alignment(blocks)
    assert common.tolist() == [1, 3, 5]
    assert fi.tolist() == [[0, 2, 4], [0, 1, 3], [0, 2, 3]]
    assert alignment.needs_alignment(blocks)
    assert not alignment.needs_alignment([np.array([1, 2]), np.array([1, 2])])
    # a retired camera holds a strict prefix
    common, _ = alignment.compute_alignment([np.arange(1, 11), np.arange(1, 5)])
    assert common.tolist() == [1, 2, 3, 4]
    print("2) compute_alignment / needs_alignment incl. retired prefix: PASS")


def test_video_for(tmp: Path):
    d = tmp / "vf" / "cam1"
    d.mkdir(parents=True)
    assert alignment.video_for(d) is None
    (d / alignment.ALIGN_TMP_NAME).write_bytes(b"x")
    assert alignment.video_for(d) is None, "aligned_tmp.mp4 is never a recording"
    (d / "20260101-s1-cam1-recording.mp4").write_bytes(b"x")
    assert alignment.video_for(d).name == "20260101-s1-cam1-recording.mp4"
    (d / "20260101-s1-cam1-calibration.mp4").write_bytes(b"x")
    assert alignment.video_for(d).name.endswith("recording.mp4"), "recording preferred"
    (d / "20260102-s2-cam1-recording.mp4").write_bytes(b"x")
    try:
        alignment.video_for(d)
    except ValueError as e:
        assert "2 mp4 candidates" in str(e)
    else:
        raise AssertionError("two recordings must be an error, not a guess")
    print("3) video_for: excludes aligned_tmp, prefers recording, refuses ambiguity: PASS")


def test_ffmpeg_cmd():
    for backend in ffmpeg_cmd.BACKENDS:
        enc = ffmpeg_cmd.h264_encoder_args(100, 21, backend)
        assert enc[enc.index("-g") + 1] == "100", enc
        assert "-qp" in enc and enc[enc.index("-qp") + 1] == "21"
        cmd = ["ffmpeg", *ffmpeg_cmd.global_args(), *ffmpeg_cmd.rawvideo_input_args(1920, 1200, 100),
               "-i", "raw.bin", *enc, *ffmpeg_cmd.mp4_container_args(), "out.mp4"]
        ffmpeg_cmd.check_mp4_command(cmd, 100)
        assert "-nostdin" in cmd and "1920x1200" in cmd
    assert "libx264" in ffmpeg_cmd.h264_encoder_args(30, 21, "x264")
    assert "h264_nvenc" in ffmpeg_cmd.h264_encoder_args(30, 21, "nvenc")
    assert "-movflags" in ffmpeg_cmd.stream_copy_args()
    ffmpeg_cmd.check_mp4_command(["f", "-i", "s.h264", *ffmpeg_cmd.stream_copy_args(), "o.mp4"], 100)
    for bad in (["f", "-c:v", "h264_nvenc", "-movflags", "+faststart", "o"],
                ["f", "-c:v", "h264_nvenc", "-g", "100", "o"],
                ["f", "-c:v", "h264_nvenc", "-g", "30", "-movflags", "+faststart", "o"]):
        try:
            ffmpeg_cmd.check_mp4_command(bad, 100)
        except AssertionError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")
    try:
        ffmpeg_cmd.h264_encoder_args(100, 21, "quicksync")
    except ValueError:
        pass
    else:
        raise AssertionError("unknown backend accepted")
    kw = ffmpeg_cmd.quiet_popen_kwargs()
    assert not ({"stdin", "stdout", "stderr"} & set(kw)), "callers own stdio"
    assert ffmpeg_cmd.get_default_backend() == "nvenc"
    ffmpeg_cmd.set_default_backend("x264")
    assert "libx264" in ffmpeg_cmd.h264_encoder_args(100, 21)
    ffmpeg_cmd.set_default_backend("nvenc")
    print("4) ffmpeg_cmd: -g <fps> and +faststart for nvenc and x264, guard rejects omissions: PASS")


def test_extract_aligned(tmp: Path, stub: str):
    saved = (alignment._ffmpeg_exe, alignment._open_gray_reader)
    alignment._ffmpeg_exe = lambda: stub
    alignment._open_gray_reader = fake_reader(10)
    d = tmp / "ex"
    d.mkdir()
    dst = d / alignment.ALIGN_TMP_NAME
    log = d / "log.txt"
    try:
        stub_mode("fail", log)
        try:
            alignment.extract_aligned(d / "v.mp4", np.array([0, 2, 4]), dst, 100, 21)
        except RuntimeError as e:
            assert "exited 1" in str(e) and "simulated encoder failure" in str(e), e
        else:
            raise AssertionError("non-zero exit accepted")
        assert (d / "align_error.log").exists(), "stderr must be kept on failure"
        cmd = read_log(log)[0]
        ffmpeg_cmd.check_mp4_command(cmd, 100)
        assert "-nostdin" in cmd and "8x4" in cmd

        stub_mode("truncate")
        try:
            alignment.extract_aligned(d / "v.mp4", np.array([0, 2, 4]), dst, 100, 21)
        except RuntimeError as e:
            assert "16 bytes" in str(e), e
        else:
            raise AssertionError("16-byte output accepted as an mp4")

        stub_mode("ok")
        n = alignment.extract_aligned(d / "v.mp4", np.array([0, 2, 4]), dst, 100, 21)
        assert n == 3 and dst.stat().st_size == 4096
        assert not (d / "align_error.log").exists(), "log removed on success"

        # the index asks for frame 12 but the source has 10 frames
        try:
            alignment.extract_aligned(d / "v.mp4", np.array([0, 12]), dst, 100, 21)
        except RuntimeError as e:
            assert "decoded 1/2" in str(e), e
        else:
            raise AssertionError("short source accepted")

        # stop() aborts
        try:
            alignment.extract_aligned(d / "v.mp4", np.array([0, 2]), dst, 100, 21,
                                      stop=lambda: True)
        except RuntimeError as e:
            assert "stopped" in str(e)
        else:
            raise AssertionError("stop ignored")
    finally:
        alignment._ffmpeg_exe, alignment._open_gray_reader = saved
    print("5) extract_aligned: non-zero exit, tiny output, short source and stop all raise; "
          "stderr kept in align_error.log: PASS")


def _rec_for_replace(tmp: Path, name: str, rate_warn_cam=None):
    """3 cameras x 10 frames; cam2 misses trigger 4, cam3 misses trigger 7.
    Optionally make one camera's clock say it ran at half rate (a block-rate
    warning) using enough frames for the check to engage."""
    n = 1200
    ids = np.arange(1, n + 1)
    blocks = {"cam1": ids, "cam2": np.delete(ids, 3), "cam3": np.delete(ids, 6)}
    times = None
    if rate_warn_cam:
        times = {rate_warn_cam: (blocks[rate_warn_cam] - 1) * 2 / 100.0}
    return synth_recording(tmp / name, blocks, fps=100, times=times)


def test_align_replace_success(tmp: Path, stub: str):
    saved = (alignment._ffmpeg_exe, alignment._open_gray_reader)
    alignment._ffmpeg_exe = lambda: stub
    alignment._open_gray_reader = fake_reader(1200)
    try:
        rec = _rec_for_replace(tmp, "ok_case", rate_warn_cam="cam3")
        stale = rec / "cam1" / alignment.ALIGN_TMP_NAME
        stale.write_bytes(b"stale")
        stub_mode("ok")
        msgs = []
        s = alignment.align_recording(rec, fps=100, replace=True, parallel=2,
                                      progress=lambda d, t, m: msgs.append((d, t, m)))
        assert not stale.exists(), "stale aligned_tmp.mp4 must be cleared first"
        assert s["needed"] and s["replaced"], s
        assert s["rate_warnings"] and "cam3" in s["rate_warnings"][0], s["rate_warnings"]
        assert s["failures"] == [] and s["failed_cams"] == []
        assert s["replaced_cams"] == ["cam1", "cam2", "cam3"] or sorted(s["replaced_cams"]) == ["cam1", "cam2", "cam3"]
        assert s["warnings"] == s["rate_warnings"], "warnings = rate + failures"
        assert s["common_frames"] == 1198
        for cam in ("cam1", "cam2", "cam3"):
            vid = alignment.video_for(rec / cam)
            assert vid.read_bytes().startswith(b"STUB"), f"{cam} video not replaced"
            b = np.load(rec / cam / "blockids.npy")
            assert b.size == 1198 and 4 not in b and 7 not in b
            ft = np.load(rec / cam / "frametimes.npy")
            assert ft.shape == (2, 1198)
            assert not (rec / cam / alignment.ALIGN_TMP_NAME).exists()
            assert not (rec / cam / "blockids.tmp.npy").exists()
        man = json.loads((rec / "aligned" / "alignment.json").read_text())
        assert man["replaced"] is True and man["failed_cams"] == []
        assert all(pc["video_is_common"] for pc in man["per_camera"].values())
        npz = np.load(rec / "aligned" / "alignment.npz")
        assert npz["video_is_common"].all()
        assert (npz["frame_index"] == np.arange(1198)).all(), \
            "post-replacement index must address the videos as they exist now"
        assert msgs[-1][0] == 3 and msgs[-1][1] == 3
    finally:
        alignment._ffmpeg_exe, alignment._open_gray_reader = saved
    print("6) align --replace success: rate warning does not unset replaced; video before "
          "metadata; manifest written after with video_is_common: PASS")


def test_align_replace_failures(tmp: Path, stub: str):
    saved = (alignment._ffmpeg_exe, alignment._open_gray_reader)
    alignment._ffmpeg_exe = lambda: stub
    alignment._open_gray_reader = fake_reader(1200)
    try:
        # every camera fails: originals and metadata untouched
        rec = _rec_for_replace(tmp, "fail_case")
        stub_mode("fail")
        s = alignment.align_recording(rec, fps=100, replace=True, parallel=3)
        assert s["needed"] and not s["replaced"]
        assert sorted(s["failed_cams"]) == ["cam1", "cam2", "cam3"] and s["replaced_cams"] == []
        assert len(s["failures"]) == 3 and all("exited 1" in f for f in s["failures"])
        for cam in ("cam1", "cam2", "cam3"):
            assert alignment.video_for(rec / cam).read_bytes().startswith(b"ORIGINAL")
            assert np.load(rec / cam / "blockids.npy").size in (1200, 1199)
            assert not (rec / cam / alignment.ALIGN_TMP_NAME).exists()
            assert (rec / cam / "align_error.log").exists()
        man = json.loads((rec / "aligned" / "alignment.json").read_text())
        assert man["replaced"] is False and sorted(man["failed_cams"]) == ["cam1", "cam2", "cam3"]
        assert not any(pc["video_is_common"] for pc in man["per_camera"].values())

        # truncated output: same outcome
        rec = _rec_for_replace(tmp, "trunc_case")
        stub_mode("truncate")
        s = alignment.align_recording(rec, fps=100, replace=True)
        assert not s["replaced"] and len(s["failed_cams"]) == 3
        assert alignment.video_for(rec / "cam1").read_bytes().startswith(b"ORIGINAL")

        # one camera fails: the others are replaced and both lists say which
        rec = _rec_for_replace(tmp, "mixed_case")
        stub_mode("fail_match:cam2")
        s = alignment.align_recording(rec, fps=100, replace=True)
        assert not s["replaced"], "replaced is all-or-nothing"
        assert s["failed_cams"] == ["cam2"] and sorted(s["replaced_cams"]) == ["cam1", "cam3"]
        assert alignment.video_for(rec / "cam2").read_bytes().startswith(b"ORIGINAL")
        assert alignment.video_for(rec / "cam1").read_bytes().startswith(b"STUB")
        assert np.load(rec / "cam2" / "blockids.npy").size == 1199
        assert np.load(rec / "cam1" / "blockids.npy").size == 1198
        npz = np.load(rec / "aligned" / "alignment.npz")
        assert npz["video_is_common"].tolist() == [True, False, True]
        assert (npz["frame_index"][0] == np.arange(1198)).all()
        # common = 1,2,3,5,6,8,...; cam2 = 1,2,3,5,6,7,8,... so common[5]=8 is cam2 frame 6
        assert npz["frame_index"][1][5] == 6, "unreplaced camera keeps its original index"

        # a camera without a video is a failure, not a warning
        rec = synth_recording(tmp / "novideo", {"cam1": np.arange(1, 6), "cam2": np.arange(1, 5)},
                              with_video={"cam1"})
        alignment._open_gray_reader = fake_reader(5)
        stub_mode("ok")
        s = alignment.align_recording(rec, fps=100, replace=True)
        assert s["failed_cams"] == ["cam2"] and "no video" in s["failures"][0]
        assert not s["replaced"]

        # stop requested: nothing launched, everything kept
        rec = _rec_for_replace(tmp, "stop_case")
        log = tmp / "stop_log.txt"
        stub_mode("ok", log)
        s = alignment.align_recording(rec, fps=100, replace=True, should_stop=lambda: True)
        assert s["stopped"] and len(s["failed_cams"]) == 3 and not s["replaced"]
        assert read_log(log) == [], "no ffmpeg may launch after a stop"
        assert alignment.video_for(rec / "cam1").read_bytes().startswith(b"ORIGINAL")

        # already aligned / index only
        rec = synth_recording(tmp / "noneed", {"cam1": np.arange(1, 6), "cam2": np.arange(1, 6)})
        s = alignment.align_recording(rec, fps=100, replace=True)
        assert not s["needed"] and not s["replaced"] and s["failures"] == []
        assert (rec / "aligned" / "alignment.json").exists()
        rec = _rec_for_replace(tmp, "index_only")
        s = alignment.align_recording(rec, fps=100, replace=False)
        assert s["needed"] and not s["replaced"] and s["replaced_cams"] == []
        assert alignment.video_for(rec / "cam1").read_bytes().startswith(b"ORIGINAL")
    finally:
        alignment._ffmpeg_exe, alignment._open_gray_reader = saved
    print("7) align --replace failures: originals kept per camera, failures/failed_cams/"
          "replaced_cams reported, stop launches nothing: PASS")


def _worker(video_dir, cams, realtime=True, max_parallel=0, w=8, h=4, fps=100, **kw):
    from gui_app.encode_worker import EncodeWorker
    return EncodeWorker(video_dir, cams, "recording", w, h, fps, 21, "20260101", "s1",
                        max_parallel=max_parallel, realtime=realtime, **kw)


def _cam(video_dir: Path, cam: str, n: int, h264=None, raw=None, tail=None, encoded=None):
    d = video_dir / cam
    d.mkdir(parents=True, exist_ok=True)
    np.save(d / "blockids.npy", np.arange(1, n + 1))
    np.save(d / "frametimes.npy", np.stack([np.arange(1, n + 1, dtype=float),
                                            np.arange(n) / 100.0]))
    if h264 is not None:
        (d / "stream.h264").write_bytes(h264)
    if raw is not None:
        (d / "raw.bin").write_bytes(raw)
    if tail is not None:
        (d / "raw_tail.bin").write_bytes(tail)
    if encoded is not None:
        (d / "encoded.json").write_text(json.dumps({"encoded": encoded, "spilled": n - encoded}))
    return d


def _run(worker):
    got = []
    worker.finished_all.connect(got.append)
    worker.run()
    assert len(got) == 1, "finished_all must fire exactly once"
    return got[0]


def test_encode_worker(tmp: Path, stub: str):
    saved = ffmpeg_cmd.ffmpeg_exe
    ffmpeg_cmd.ffmpeg_exe = lambda: stub
    try:
        # A) plain remux + a tail merge that succeeds
        vd = tmp / "enc_a"
        _cam(vd, "cam1", 50, h264=b"HEAD" * 100)
        _cam(vd, "cam2", 60, h264=b"HEAD" * 100, tail=b"\0" * (8 * 4 * 10))
        log = tmp / "enc_a.log"
        stub_mode("ok", log)
        res = _run(_worker(vd, ["cam1", "cam2"]))
        assert res == [("cam1", 50, True), ("cam2", 60, True)], res
        assert not (vd / "cam1" / "stream.h264").exists(), "remuxed source removed"
        assert not (vd / "cam2" / "raw_tail.bin").exists() and not (vd / "cam2" / "tail.h264").exists()
        assert not (vd / "cam2" / "stream.h264").exists()
        assert alignment.video_for(vd / "cam2").stat().st_size == 4096
        cmds = read_log(log)
        assert len(cmds) == 3, cmds
        tail_cmd = [c for c in cmds if c[-1].endswith("tail.h264")][0]
        assert tail_cmd[tail_cmd.index("-g") + 1] == "100" and "-nostdin" in tail_cmd
        for c in cmds:
            if c[-1].endswith(".mp4"):
                ffmpeg_cmd.check_mp4_command(c, 100)
        assert not (vd / "cam1" / "encode_error.log").exists()

        # B) tail merge fails, encoded.json known: metadata truncated, h264 kept, mp4 ok
        vd = tmp / "enc_b"
        _cam(vd, "cam1", 60, h264=b"HEAD" * 100, tail=b"\0" * (8 * 4 * 20), encoded=40)
        stub_mode("tail_fail")
        w = _worker(vd, ["cam1"])
        res = _run(w)
        assert res == [("cam1", 40, True)], res
        assert np.load(vd / "cam1" / "blockids.npy").tolist() == list(range(1, 41))
        assert np.load(vd / "cam1" / "frametimes.npy").shape == (2, 40)
        assert np.load(vd / "cam1" / "blockids.full.npy").size == 60
        assert (vd / "cam1" / "stream.h264").exists(), "source kept: the tail is unmerged"
        assert (vd / "cam1" / "raw_tail.bin").exists()
        assert not (vd / "cam1" / "tail.h264").exists()
        assert alignment.video_for(vd / "cam1") is not None
        assert len(w.warnings) == 1 and "truncated to 40" in w.warnings[0], w.warnings
        assert "truncated to 40" in (vd / "cam1" / "WARNINGS.txt").read_text()
        assert (vd / "cam1" / "tail_error.log").exists()

        # C) tail merge fails, no encoded.json: camera fails, everything kept, no mp4
        vd = tmp / "enc_c"
        _cam(vd, "cam1", 60, h264=b"HEAD" * 100, tail=b"\0" * (8 * 4 * 20))
        stub_mode("tail_fail")
        res = _run(_worker(vd, ["cam1"]))
        assert res == [("cam1", 60, False)], res
        assert (vd / "cam1" / "stream.h264").exists() and (vd / "cam1" / "raw_tail.bin").exists()
        assert np.load(vd / "cam1" / "blockids.npy").size == 60, "no split point => no truncation"
        assert alignment.video_for(vd / "cam1") is None

        # D) empty stream.h264 beside raw.bin: encode raw.bin, mp4 command carries the invariants
        vd = tmp / "enc_d"
        _cam(vd, "cam1", 5, h264=b"", raw=b"\0" * (8 * 4 * 5))
        log = tmp / "enc_d.log"
        stub_mode("ok", log)
        res = _run(_worker(vd, ["cam1"]))
        assert res == [("cam1", 5, True)], res
        assert not (vd / "cam1" / "raw.bin").exists()
        c = read_log(log)[0]
        assert c[c.index("-i") + 1].endswith("raw.bin") and "h264_nvenc" in c
        ffmpeg_cmd.check_mp4_command(c, 100)

        # D2) x264 backend selected per worker
        vd = tmp / "enc_d2"
        _cam(vd, "cam1", 5, raw=b"\0" * (8 * 4 * 5))
        stub_mode("ok", log)
        res = _run(_worker(vd, ["cam1"], realtime=False, backend="x264"))
        c = read_log(log)[0]
        assert "libx264" in c and res[0][2]
        ffmpeg_cmd.check_mp4_command(c, 100)

        # E) empty stream.h264 and no raw.bin: fail, message names both
        vd = tmp / "enc_e"
        _cam(vd, "cam1", 5, h264=b"")
        stub_mode("ok")
        res = _run(_worker(vd, ["cam1"]))
        assert res == [("cam1", 0, False)], res

        # F) remux fails: source kept, partial mp4 removed
        vd = tmp / "enc_f"
        _cam(vd, "cam1", 5, h264=b"HEAD" * 100)
        stub_mode("truncate")
        res = _run(_worker(vd, ["cam1"]))
        assert res == [("cam1", 5, False)]
        assert (vd / "cam1" / "stream.h264").exists()
        assert alignment.video_for(vd / "cam1") is None, "a 16-byte mp4 must not be left as the recording"
        assert (vd / "cam1" / "encode_error.log").exists()

        # G) request_stop before run: nothing launched, sources kept
        vd = tmp / "enc_g"
        _cam(vd, "cam1", 5, h264=b"HEAD" * 100)
        _cam(vd, "cam2", 5, raw=b"\0" * 160)
        log = tmp / "enc_g.log"
        stub_mode("ok", log)
        w = _worker(vd, ["cam1", "cam2"])
        w.request_stop()
        res = _run(w)
        assert res == [("cam1", 5, False), ("cam2", 5, False)], res
        assert read_log(log) == []
        assert (vd / "cam1" / "stream.h264").exists() and (vd / "cam2" / "raw.bin").exists()

        # H) ffmpeg missing: every camera fails, nothing raises
        vd = tmp / "enc_h"
        _cam(vd, "cam1", 5, h264=b"HEAD" * 100)

        def _missing():
            raise FileNotFoundError("no ffmpeg here")
        ffmpeg_cmd.ffmpeg_exe = _missing
        res = _run(_worker(vd, ["cam1"]))
        assert res == [("cam1", 0, False)]
        assert (vd / "cam1" / "stream.h264").exists()
    finally:
        ffmpeg_cmd.ffmpeg_exe = saved
    print("8) encode_worker: tail merge ok/failed(+encoded.json)/failed(no split point), "
          "empty h264 -> raw.bin, remux failure keeps source, stop, missing ffmpeg: PASS")


def test_stim_trace():
    from gui_app import stim_compiler
    B = lambda bid, freq, pw, dur, pin=53: {"id": bid, "x": 0, "y": 0, "pin": pin,
                                            "freq": freq, "pw": pw, "dur": dur,
                                            "start": bid == "A", "end": False}
    blocks = [B("A", 0.0, 0.0, 1.0), B("B", 20.0, 10.0, 1.0)]
    edges = [{"src": "A", "dst": "B"}]
    chains = stim_compiler.describe(blocks, edges)
    paradigm = {"chains": chains, "blocks": blocks, "edges": edges}
    ids = np.arange(1, 201)  # 2 s at 100 fps
    fields, rows = stim_trace.build_rows(paradigm, ids, 100)
    assert sum(r["any_active"] for r in rows) == 100, "second half active"
    # the label is display text: rewording it must not change the trace
    relabelled = json.loads(json.dumps(paradigm))
    for ch in relabelled["chains"]:
        for s in ch["steps"]:
            s["mode"] = "OFF"
    relabelled.pop("blocks"); relabelled.pop("edges")
    _, rows2 = stim_trace.build_rows(relabelled, ids, 100)
    assert [r["any_active"] for r in rows2] == [r["any_active"] for r in rows]
    assert stim_trace.step_active({"freq_hz": 20, "pulse_width_ms": 10})
    assert not stim_trace.step_active({"freq_hz": 0, "pulse_width_ms": 10})
    assert not stim_trace.step_active({"freq_hz": 20, "pulse_width_ms": 0})
    # editing the graph changes the trace: make the first block 1.5 s
    edited = json.loads(json.dumps(paradigm))
    edited["blocks"][0]["dur"] = 1.5
    _, rows3 = stim_trace.build_rows(edited, ids, 100)
    assert sum(r["any_active"] for r in rows3) == 50, "chains must be rebuilt from blocks/edges"
    print("9) stim_trace: active from numbers not labels; chains rebuilt from the graph: PASS")


def test_cli_2_align(tmp: Path):
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    # calibration dir at 30 fps: fps must come from session_metadata, and the
    # clean recording must exit 0 from --dry-run
    sess = tmp / "cli" / "sess"
    n = 1200
    ids = np.arange(1, n + 1)
    synth_recording(sess, {"cam1": ids, "cam2": np.delete(ids, 5)}, fps=30)
    rec = sess / "recording"
    cal = sess / "calibration"
    shutil.copytree(rec, cal)
    (sess / "session_metadata.json").write_text(json.dumps(
        {"frame_rate": 30, "calibration_frame_rate": 30, "quality": 23}))
    r = subprocess.run([PY, str(ROOT / "2_align.py"), str(cal), "--dry-run"],
                       capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert "fps 30 from session_metadata.json:calibration_frame_rate" in r.stdout, r.stdout
    assert "Common (aligned) frames: 1199" in r.stdout
    assert not (cal / "aligned").exists(), "--dry-run writes nothing"
    # the wrong reference rate flags every camera: exit 2 and a WARNING line
    r = subprocess.run([PY, str(ROOT / "2_align.py"), str(cal), "--dry-run", "--fps", "100"],
                       capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)
    assert "WARNING (block rate)" in r.stderr, r.stderr
    # missing recording: exit 1
    r = subprocess.run([PY, str(ROOT / "2_align.py"), str(tmp / "nope"), "--dry-run"],
                       capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r.returncode == 1 and "ERROR" in r.stderr
    # index-only run on the clean directory: exit 0, aligned/ written, videos untouched
    r = subprocess.run([PY, str(ROOT / "2_align.py"), str(rec)],
                       capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert (rec / "aligned" / "alignment.json").exists()
    assert alignment.video_for(rec / "cam1").read_bytes().startswith(b"ORIGINAL")
    for f in (ROOT / "2_align.py", ROOT / "3_stim_trace.py"):
        assert "/// script" not in f.read_text(), f"{f.name} still carries a PEP 723 header"
    print("10) 2_align CLI: fps from session_metadata, dry-run runs the block-rate check, "
         "exit codes 0/1/2, no PEP 723 header: PASS")


def test_cli_3_stim_trace(tmp: Path):
    from gui_app import stim_compiler
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    root = tmp / "st"
    blocks = [{"id": "A", "x": 0, "y": 0, "pin": 53, "freq": 20.0, "pw": 10.0,
               "dur": 1.0, "start": True, "end": False}]
    paradigm = {"chains": stim_compiler.describe(blocks, []), "blocks": blocks, "edges": []}
    good = synth_recording(root / "good", {"cam1": np.arange(1, 51)})
    bad = synth_recording(root / "bad", {"cam1": np.array([1, 2, 5, 4])})
    for rec in (good, bad):
        (rec / stim_trace.PARADIGM_NAME).write_text(json.dumps(paradigm))
    r = subprocess.run([PY, str(ROOT / "3_stim_trace.py"), str(root), "--all"],
                       capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r.returncode == 1, (r.returncode, r.stdout, r.stderr)
    assert "Traceback" not in r.stderr, r.stderr
    assert f"SKIP {bad}" in r.stdout and "ValueError" in r.stdout, r.stdout
    assert f"OK   {good / stim_trace.TRACE_NAME}" in r.stdout, r.stdout
    assert (good / stim_trace.TRACE_NAME).exists() and not (bad / stim_trace.TRACE_NAME).exists()
    print("11) 3_stim_trace --all: a corrupt recording is a SKIP line, the batch continues, "
         "exit 1: PASS")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="test_alignment_"))
    try:
        stub = make_stub(tmp)
        test_unwrap()
        test_compute_alignment()
        test_video_for(tmp)
        test_ffmpeg_cmd()
        test_extract_aligned(tmp, stub)
        test_align_replace_success(tmp, stub)
        test_align_replace_failures(tmp, stub)
        test_encode_worker(tmp, stub)
        test_stim_trace()
        test_cli_2_align(tmp)
        test_cli_3_stim_trace(tmp)
    finally:
        os.environ.pop("FFSTUB_MODE", None)
        os.environ.pop("FFSTUB_LOG", None)
        shutil.rmtree(tmp, ignore_errors=True)
    print("\nALL test_alignment PASS")


if __name__ == "__main__":
    main()
