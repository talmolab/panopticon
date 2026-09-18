"""Calibration solve: component choice, edge weights, metadata, failure codes,
the GUI workers, and an end-to-end synthetic solve.

The behaviour these guard: a disconnected coverage graph must keep the LARGEST
group of cameras, not the group that happens to hold cam1, and the result must
say so (partial flag, dropped list, report) instead of exiting 0 with a toml
that quietly lacks cameras. Every quality figure the solve computes must land
in calibration.toml's [metadata] and calibration_report.json.

Needs the project environment (OpenCV contrib, numpy, PyQt5). Never touches
data/: the end-to-end case renders a synthetic ChArUco board into temporary
mp4s under the scratch directory.

    uv run python test_calibrate.py
"""
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pyproject allows 3.10, where tomllib is absent
    import tomli as tomllib

import numpy as np
from PyQt5.QtCore import Qt

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))
PY = sys.executable

failures = []


def check(num, name, ok, detail=""):
    print(f"{num}) {name}: {'PASS' if ok else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""), flush=True)
    if not ok:
        failures.append(name)


def load_calibrate():
    spec = importlib.util.spec_from_file_location("calibrate_mod", REPO / "1_calibrate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cal = load_calibrate()
from gui_app import charuco  # noqa: E402
from gui_app import board_detector  # noqa: E402
from gui_app import calibration_worker as cw  # noqa: E402
from gui_app.coverage_worker import CoverageWorker  # noqa: E402

I3 = np.eye(3)
T0 = np.zeros((3, 1))


def pairs_from(edges):
    """{(a, b): (R, T, rms, n)} from [(a, b, rms, n), ...]."""
    return {(a, b): (I3, T0, float(rms), int(n)) for a, b, rms, n in edges}


# 1-4 ---------------------------------------------------------------------------
# Nine cameras: cam1 sits in a 2-node component, the other seven are connected.
# The old BFS from cam_names[0] kept {cam1, cam2} and dropped seven cameras.
cams = [f"cam{i}" for i in range(1, 10)]
pw = pairs_from([
    ("cam1", "cam2", 0.5, 30),
    ("cam3", "cam4", 1.0, 30), ("cam4", "cam5", 1.2, 30), ("cam5", "cam6", 0.9, 30),
    ("cam6", "cam7", 1.1, 30), ("cam7", "cam8", 1.3, 30), ("cam8", "cam9", 0.8, 30),
    ("cam3", "cam9", 2.0, 12),
])
tree, kept, dropped, comps = cal.build_graph(cams, pw)
check(1, "largest component is kept, not the one holding cam1",
      kept == [f"cam{i}" for i in range(3, 10)] and dropped == ["cam1", "cam2"],
      f"kept={kept} dropped={dropped}")
check(2, "components are reported largest first",
      [len(c) for c in comps] == [7, 2] and comps[1] == ["cam1", "cam2"])
check(3, "the tree spans the kept component",
      len(tree) == 6 and {c for e in tree for c in e} == set(kept))

# Tie-break: two 2-node components, the one with more summed frames wins.
pw_tie = pairs_from([("cam1", "cam2", 0.5, 8), ("cam3", "cam4", 0.9, 25)])
_, kept, dropped, _ = cal.build_graph(["cam1", "cam2", "cam3", "cam4"], pw_tie)
check(4, "equal-size components: the one with more shared frames wins",
      kept == ["cam3", "cam4"] and dropped == ["cam1", "cam2"], f"kept={kept}")

# 5-7 — edge weight ---------------------------------------------------------------
w_few = cal.edge_weight(0.5, 3)      # low RMS, three frames
w_many = cal.edge_weight(1.5, 30)    # honest RMS, well observed
check(5, "a 3-frame pair with a lower RMS weighs MORE than a 30-frame pair",
      w_few > w_many, f"{w_few:.3f} vs {w_many:.3f}")
check(6, "weight is rms/sqrt(min(n, 30)) for a full-strength pair",
      abs(cal.edge_weight(2.0, 16) - 2.0 / 4.0) < 1e-12
      and abs(cal.edge_weight(2.0, 90) - 2.0 / np.sqrt(30)) < 1e-12)
# Prim's must take the well-observed path even when the weak edge has a
# lower RMS: cam1-cam3 direct (rms 0.1, n 3) versus cam1-cam2-cam3 (30 frames).
pw_w = pairs_from([("cam1", "cam3", 0.1, 3), ("cam1", "cam2", 1.0, 30),
                   ("cam2", "cam3", 1.0, 30)])
tree, kept, _, _ = cal.build_graph(["cam1", "cam2", "cam3"], pw_w)
check(7, "the tree avoids the under-observed low-RMS edge",
      sorted(tuple(sorted(e)) for e in tree) == [("cam1", "cam2"), ("cam2", "cam3")],
      f"tree={tree}")
check(8, "an under-observed edge still connects a camera nothing else reaches",
      cal.build_graph(["cam1", "cam2"], pairs_from([("cam1", "cam2", 0.1, 3)]))[1]
      == ["cam1", "cam2"])

# 9-13 — report, partial flag and metadata TOML ------------------------------------
board_cfg = dict(board_x=8, board_y=8, square_length=15.0, marker_length=10.0,
                 marker_bits=4, dict_size=1000, board_legacy=True)
tree, kept, dropped, comps = cal.build_graph(cams, pw)
report = cal.build_report(
    board_cfg, "cam3", kept, tree, pw,
    {c: {"rms": 0.3, "frames": 60, "detections": 400} for c in cams},
    comps, {"no_video": [], "unreadable": [], "few_detections": [],
            "failed_intrinsics": [], "isolated": dropped},
    hints_used=True, skip=3, warnings=["cam3-cam9: only 12 shared frames"])
check(9, "report carries partial=true and the dropped cameras",
      report["partial"] is True and report["dropped"]["isolated"] == ["cam1", "cam2"]
      and report["ref_camera"] == "cam3" and report["hints_used"] is True)
check(10, "report tree rows carry per-edge rms and frames",
      all({"from", "to", "rms", "frames"} <= set(r) for r in report["tree"])
      and any(r["frames"] == 12 for r in report["tree"]) is False
      and len(report["tree"]) == 6)

with tempfile.TemporaryDirectory() as td:
    toml_path = Path(td) / "calibration.toml"
    K = np.array([[600.0, 0, 320], [0, 600, 240], [0, 0, 1]])
    intr = {c: (K, np.zeros(5)) for c in kept}
    extr = {c: (np.zeros(3), np.zeros(3)) for c in kept}
    sizes = {c: (640, 480) for c in kept}
    cal.write_calibration_toml(toml_path, kept, intr, extr, sizes, meta=report)
    doc = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    md = doc["metadata"]
    check(11, "calibration.toml parses and keeps the aniposelib camera tables",
          [doc[f"cam_{i}"]["name"] for i in range(7)] == kept
          and doc["cam_0"]["matrix"][0][0] == 600.0)
    check(12, "[metadata] carries created/opencv/board/ref_camera/tree/hints_used/skip/warnings/partial",
          {"created", "opencv", "board", "ref_camera", "tree", "hints_used", "skip",
           "warnings", "partial", "dropped", "components"} <= set(md)
          and md["partial"] is True and md["board"]["dictionary"] == "DICT_4X4_1000"
          and md["board"]["board_legacy"] is True
          and md["tree"][0]["frames"] == 30 and md["skip"] == 3
          and md["dropped"]["isolated"] == ["cam1", "cam2"]
          and md["warnings"] == ["cam3-cam9: only 12 shared frames"])
    check(13, "[metadata.intrinsics.<cam>] and [metadata.pairs] carry rms and frames",
          md["intrinsics"]["cam3"] == {"rms": 0.3, "frames": 60, "detections": 400}
          and "cam1" not in md["intrinsics"]
          and md["pairs"]["cam3-cam9"] == {"rms": 2.0, "frames": 12})

# 14-15 — unknown dictionary ---------------------------------------------------------
bad = dict(board_cfg, marker_bits=5, dict_size=200)
try:
    charuco.resolve_dictionary(bad)
    ok, msg = False, "no exception"
except ValueError as e:
    msg = str(e)
    ok = "DICT_5X5_200" in msg and "DICT_4X4_1000" in msg
check(14, "resolve_dictionary raises ValueError naming the bad and the valid names",
      ok, msg[:80])
try:
    cal.create_board_and_dict(bad)
    ok = False
except ValueError:
    ok = True
check(15, "the solve's board construction raises the same error (no silent default)", ok)

# 16-20 — worker error classification ---------------------------------------------
msg = cw._parse_calibration_error("", "  WARNING: x\nERROR_CODE=NO_VIDEOS\nERROR: none under foo", 1)
check(16, "ERROR_CODE is classified first", msg.startswith("No readable calibration videos")
      and "ERROR: none under foo" in msg)
msg = cw._parse_calibration_error(
    "", "Traceback...\n  File x.py\nValueError: not enough values to unpack (expected 9, got 7)", 1)
check(17, "a tuple-unpack ValueError is no longer reported as 'no board detections'",
      "ChArUco" not in msg and "exit code 1" in msg)
msg = cw._parse_calibration_error(
    "", "  File \"C:/x/numpy/linalg/linalg.py\"\nRuntimeError: boom", 1)
check(18, "a numpy/linalg path in a traceback does not mean 'singular matrix'",
      "singular" not in msg)
msg = cw._parse_calibration_error("", "numpy.linalg.LinAlgError: Singular matrix", 1)
check(19, "LinAlgError is classified as the singular-matrix failure", "singular matrix" in msg)
summary = cw.summarize_report(report)
check(20, "summarize_report names the dropped cameras, the groups and the warnings",
      "solved 7 of 9" in summary and "cam1, cam2" in summary
      and "{cam1,cam2}" in summary and "cam3-cam9" in summary, summary.replace("\n", " | ")[:120])
check(21, "a clean report summarises to an empty message",
      cw.summarize_report({"partial": False, "dropped": {}, "cameras": ["a"], "warnings": []}) == "")


# 22-26 — CoverageWorker lifecycle --------------------------------------------------
class _Mgr:
    def __init__(self, n):
        self.n = n

    @property
    def latest_full_frames(self):
        return [None] * self.n

    @property
    def latest_frames(self):
        return [None] * self.n

    @property
    def frame_counts(self):
        return [0] * self.n


class _Det:
    def __init__(self, raise_every=False):
        self.calls = 0
        self.ticks_per_s = 0.0
        self.raise_every = raise_every
        self.seen_threads = set()

    def update(self, frames, frame_counts=None):
        import cv2
        self.calls += 1
        self.ticks_per_s = 42.0
        self.seen_threads.add(cv2.getNumThreads())
        if self.raise_every:
            raise RuntimeError("stub failure")


import cv2  # noqa: E402
threads_before = cv2.getNumThreads()

det = _Det()
w = CoverageWorker(det, _Mgr(3), interval_ms=5)
w.stop()                       # lands BEFORE start(): must still win
w.start()
joined = w.wait(3000)
check(22, "stop() before start() exits the loop immediately and is joinable",
      joined and det.calls == 0, f"joined={joined} calls={det.calls}")

det = _Det()
w = CoverageWorker(det, _Mgr(3), interval_ms=5)
w.start()
time.sleep(0.15)
w.stop()
joined = w.wait(3000)
check(23, "a running worker stops within one tick and is joinable",
      joined and det.calls >= 3 and not w.isRunning(), f"calls={det.calls}")
check(24, "ticks_per_s is published from the detector",
      w.ticks_per_s == 42.0 and w.ticks == det.calls)
check(25, "the OpenCV pool is capped to 2 during the loop and restored afterwards",
      det.seen_threads == {2} and cv2.getNumThreads() == threads_before,
      f"seen={det.seen_threads} after={cv2.getNumThreads()}")

det = _Det(raise_every=True)
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    w = CoverageWorker(det, _Mgr(3), interval_ms=1)
    w.start()
    time.sleep(0.12)
    w.stop()
    w.wait(3000)
n_err = buf.getvalue().count("[hud] detection error")
check(26, "a persistent detector exception is logged once, not at tick rate",
      n_err == 1 and det.calls > 5, f"errors={n_err} calls={det.calls}")


# 27-42 — end-to-end synthetic solve ------------------------------------------------
def look_at(pos, target):
    z = np.asarray(target, float) - np.asarray(pos, float)
    z /= np.linalg.norm(z)
    up = np.array([0.0, -1.0, 0.0])
    x = np.cross(up, z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    R = np.stack([x, y, z])            # world -> camera rows
    t = -R @ np.asarray(pos, float)
    return R, t.reshape(3, 1)


def make_session(root, seed=0):
    """Five cameras. cam1+cam2 see the board only in frames 0-89, cam3-5 only
    in frames 90-179: two components, the larger one WITHOUT cam1."""
    rng = np.random.default_rng(seed)
    cfg = dict(board_x=8, board_y=8, square_length=15.0, marker_length=10.0,
               marker_bits=4, dict_size=1000, board_legacy=False)
    board, _ = charuco.make_board(cfg)
    bimg = board.generateImage((480, 480), marginSize=40)
    mm_per_px = 120.0 / 400.0
    S = np.array([[mm_per_px, 0, -40 * mm_per_px],
                  [0, mm_per_px, -40 * mm_per_px], [0, 0, 1]])
    W, H = 640, 480
    K = np.array([[600.0, 0, 320], [0, 600, 240], [0, 0, 1]])
    centre = np.array([60.0, 60.0, 300.0])   # board centre when unperturbed
    poses = {
        "cam1": look_at([0, 0, 0], centre), "cam2": look_at([90, 0, 0], centre),
        "cam3": look_at([0, 0, 0], centre), "cam4": look_at([90, 0, 0], centre),
        "cam5": look_at([-90, 0, 0], centre),
    }
    n_frames = 180
    board_poses = []
    for _ in range(n_frames):
        rvec = rng.uniform(-0.4, 0.4, 3)
        tvec = np.array([rng.uniform(-30, 30), rng.uniform(-30, 30),
                         rng.uniform(260, 340)])
        Rb, _ = cv2.Rodrigues(rvec)
        board_poses.append((Rb, tvec.reshape(3, 1)))
    calib = Path(root) / "calibration"
    for cam, (Rc, tc) in poses.items():
        d = calib / cam
        d.mkdir(parents=True)
        wr = cv2.VideoWriter(str(d / f"{cam}_calibration.mp4"),
                             cv2.VideoWriter_fourcc(*"mp4v"), 30, (W, H))
        assert wr.isOpened(), "cv2.VideoWriter could not open an mp4"
        group_a = cam in ("cam1", "cam2")
        for k, (Rb, tb) in enumerate(board_poses):
            visible = (k < 90) if group_a else (k >= 90)
            if visible:
                M = Rc @ Rb
                Hm = K @ np.column_stack([M[:, 0], M[:, 1], (Rc @ tb + tc).ravel()]) @ S
                frame = cv2.warpPerspective(bimg, Hm, (W, H), borderValue=128)
            else:
                frame = np.full((H, W), 128, np.uint8)
            wr.write(cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR))
        wr.release()
    import yaml
    (Path(root) / "board.yaml").write_text(yaml.safe_dump(cfg))
    return Path(root), Path(root) / "board.yaml", n_frames


def run_solve(session, board_yaml, *extra):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    return subprocess.run(
        [PY, str(REPO / "1_calibrate.py"), str(session), "--board-config",
         str(board_yaml), *extra],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(REPO), env=env, timeout=600)


# One TemporaryDirectory owns the whole e2e tree so nothing outlives the run;
# a separately created parent would stay behind in %TEMP% on every run.
with tempfile.TemporaryDirectory(prefix="calib_e2e_",
                                 dir=os.environ.get("CLAUDE_SCRATCH") or None) as td:
    session, board_yaml, n_frames = make_session(td)
    calib = session / "calibration"

    t0 = time.perf_counter()
    r = run_solve(session, board_yaml)
    dt = time.perf_counter() - t0
    check(27, "synthetic 5-camera solve exits 0 (partial solve is not a failure)",
          r.returncode == 0, f"rc={r.returncode} {dt:.1f}s stderr_tail={r.stderr[-300:]!r}")
    toml_path = calib / "calibration.toml"
    report_path = calib / "calibration_report.json"
    ok_files = toml_path.exists() and report_path.exists()
    check(28, "calibration.toml and calibration_report.json are written", ok_files)
    if ok_files:
        doc = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        md = doc["metadata"]
        rep = json.loads(report_path.read_text(encoding="utf-8"))
        names = [doc[k]["name"] for k in sorted(doc) if k.startswith("cam_")]
        check(29, "the LARGER component (cam3, cam4, cam5) is solved, cam1's group dropped",
              names == ["cam3", "cam4", "cam5"], f"names={names}")
        check(30, "metadata.partial is true and lists cam1/cam2 as isolated",
              md["partial"] is True and md["dropped"]["isolated"] == ["cam1", "cam2"]
              and rep["partial"] is True and rep["dropped"]["isolated"] == ["cam1", "cam2"])
        check(31, "components are recorded largest first",
              md["components"] == [["cam3", "cam4", "cam5"], ["cam1", "cam2"]])
        check(32, "the reference camera falls back into the solved group and says so",
              md["ref_camera"] == "cam3"
              and any("reference camera cam1" in w for w in md["warnings"]))
        # Pairs of DROPPED cameras stay in the report: they are the evidence of
        # what the isolated group did see, and cost nothing.
        check(33, "per-camera intrinsics rms/frames and per-pair rms/frames are persisted",
              set(md["intrinsics"]) == {"cam3", "cam4", "cam5"}
              and all(0 < v["rms"] < 3 and v["frames"] >= 20 for v in md["intrinsics"].values())
              and set(md["pairs"]) == {"cam1-cam2", "cam3-cam4", "cam3-cam5", "cam4-cam5"}
              and all(v["frames"] >= 10 for v in md["pairs"].values()),
              f"intr={md['intrinsics']} pairs={md['pairs']}")
        check(34, "tree edges carry frames and the tree spans the solved cameras",
              len(md["tree"]) == 2 and all(e["frames"] >= 10 for e in md["tree"]))
        check(35, "the disconnected-graph diagnosis goes to stderr, not only stdout",
              "disconnected" in r.stderr and "dropping cam1, cam2" in r.stderr
              and "Solved 3 of 5 cameras" in r.stderr, r.stderr[-200:].replace("\n", " | "))
        check(36, "hints_used is false on a full scan and REPORT_PATH is printed",
              md["hints_used"] is False and "REPORT_PATH=" in r.stdout)

        # Stale hints: video identity that does not match => ignored, full scan.
        hints_path = calib / "codet_frames.json"
        board_detector.write_codet_frames(
            hints_path, [{0: 1000, 1: 1000}], ["cam3", "cam4"],
            videos={"cam3": {"name": "cam3_calibration.mp4", "size": 1}})
        r2 = run_solve(session, board_yaml)
        rep2 = json.loads(report_path.read_text(encoding="utf-8"))
        check(37, "a hint file whose video identity mismatches is ignored with a warning",
              r2.returncode == 0 and rep2["hints_used"] is False
              and "hints ignored" in r2.stderr, r2.stderr[-200:].replace("\n", " | "))

        # Fresh hints via the GUI's writer + stamp: grabbed ordinals are mp4
        # index + 1, so the last hint sits one past the end and is clamped.
        ticks = [{2: k + 1, 3: k + 1, 4: k + 1} for k in range(91, n_frames, 2)]
        board_detector.write_codet_frames(hints_path, ticks, ["cam1", "cam2", "cam3", "cam4", "cam5"])
        stamped = board_detector.stamp_codet_videos(calib)
        worker = cw.CalibrationWorker(session, REPO / "1_calibrate.py", str(board_yaml))
        got = {}
        # DirectConnection: no Qt event loop runs here, so a queued emission
        # from the worker thread would never be delivered.
        worker.finished_solve.connect(lambda ok, m: got.update(ok=ok, msg=m), Qt.DirectConnection)
        worker.report_ready.connect(lambda rep: got.update(report=rep), Qt.DirectConnection)
        statuses = []
        worker.status.connect(statuses.append, Qt.DirectConnection)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            worker.start()
            worker.wait(600_000)
        rep3 = json.loads(report_path.read_text(encoding="utf-8"))
        check(38, "stamped hints are used by the solve launched through CalibrationWorker",
              stamped == 3 and got.get("ok") is True and rep3["hints_used"] is True
              and rep3["cameras"] == ["cam3", "cam4", "cam5"],
              f"ok={got.get('ok')} hints_used={rep3.get('hints_used')} msg={got.get('msg', '')[:80]!r}")
        check(39, "out-of-range hints are clamped, not lost",
              "hints clamped into range" in buf.getvalue())
        check(40, "the worker streams progress and hands the GUI a partial-solve summary",
              any(s.startswith("Detecting") for s in statuses)
              and any(s.startswith("Intrinsics") for s in statuses)
              and got.get("report", {}).get("partial") is True
              and "solved 3 of 5" in got.get("msg", ""),
              f"statuses={statuses[:3]} msg={got.get('msg', '')[:60]!r}")

    # Worker failure path against an empty session: ERROR_CODE drives the text.
    empty = Path(td) / "empty"
    (empty / "calibration" / "cam1").mkdir(parents=True)
    worker = cw.CalibrationWorker(empty, REPO / "1_calibrate.py", str(board_yaml))
    got = {}
    worker.finished.connect(lambda ok, m: got.update(ok=ok, msg=m), Qt.DirectConnection)
    with contextlib.redirect_stdout(io.StringIO()):
        worker.start()
        worker.wait(120_000)
    check(41, "an empty session fails through the worker with the NO_VIDEOS text",
          got.get("ok") is False and got.get("msg", "").startswith("No readable calibration videos"),
          got.get("msg", "")[:80].replace("\n", " "))

    # A legacy flat hint file (a GUI that predates the video identity) is used,
    # and the layout notice stays on stderr: anything in report["warnings"]
    # raises the GUI's warning dialog, and a layout detail is not a quality
    # problem, so it must not turn every clean solve into "(with warnings)".
    if ok_files:
        (calib / "codet_frames.json").write_text(json.dumps(
            {c: [k for k in range(92, n_frames, 2)] for c in ("cam3", "cam4", "cam5")}))
        r4 = run_solve(session, board_yaml)
        rep4 = json.loads(report_path.read_text(encoding="utf-8"))
        check(42, "a legacy flat hint file is used and leaves no trace in the report warnings",
              r4.returncode == 0 and rep4["hints_used"] is True
              and "carries no video identity" in r4.stderr
              and not any("video identity" in w for w in rep4["warnings"]),
              f"hints_used={rep4.get('hints_used')} warnings={rep4.get('warnings')}")


# 43-44 — hint-file notices: stderr-only versus actionable ----------------------------
with tempfile.TemporaryDirectory() as td:
    calib = Path(td) / "calibration"
    (calib / "cam1").mkdir(parents=True)
    (calib / "cam1" / "cam1_calibration.mp4").write_bytes(b"x" * 10)
    hints_path = calib / "codet_frames.json"
    hints_path.write_text(json.dumps({"cam1": [3, 5, 8]}))
    warnings = []
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        hints = cal.load_codet_hints(hints_path, calib, warnings)
    check(43, "a legacy flat hint file yields hints, an empty warnings list and a stderr notice",
          hints == {"cam1": [3, 5, 8]} and warnings == []
          and "carries no video identity" in err.getvalue(),
          f"hints={hints} warnings={warnings}")
    hints_path.write_text(json.dumps({
        "frames": {"cam1": [3, 5, 8]},
        "videos": {"cam1": {"name": "cam1_calibration.mp4", "size": 999}}}))
    warnings = []
    with contextlib.redirect_stderr(io.StringIO()):
        hints = cal.load_codet_hints(hints_path, calib, warnings)
    check(44, "a video-identity mismatch drops the hints AND lands in warnings (actionable)",
          hints is None and len(warnings) == 1 and "hints ignored" in warnings[0],
          f"hints={hints} warnings={warnings}")


# 45-46 — one ArUco API gate for board and detector -----------------------------------
# The HUD engine and the solve must both take their marker detector from
# charuco, so no call site keeps a private hasattr() heuristic that can
# disagree with uses_new_api() on an intermediate OpenCV build.
src_hud = (REPO / "gui_app" / "board_detector.py").read_text(encoding="utf-8")
src_solve = (REPO / "1_calibrate.py").read_text(encoding="utf-8")
calls = []
_real_mmd = charuco.make_marker_detector
charuco.make_marker_detector = lambda d: calls.append(d) or (lambda gray: ([], None))
try:
    eng = board_detector._CharucoEngine(board_cfg)
    n, centroid = eng.detect(np.zeros((32, 32), np.uint8))
finally:
    charuco.make_marker_detector = _real_mmd
check(45, "the HUD engine takes its marker detector from charuco and keeps no API heuristic",
      len(calls) == 1 and n == 0 and centroid is None
      and "hasattr(aruco" not in src_hud and "hasattr(aruco" not in src_solve
      and "charuco.make_marker_detector(" in src_solve)

cfg_new = dict(board_cfg, board_legacy=False)
board, _ = charuco.make_board(cfg_new)
bimg = board.generateImage((480, 480), marginSize=40)
n, centroid = board_detector._CharucoEngine(cfg_new).detect(bimg)
check(46, "the shared detector finds the rendered board's markers through the HUD engine",
      n >= 20 and centroid is not None and abs(centroid[0] - 0.5) < 0.05
      and abs(centroid[1] - 0.5) < 0.05, f"n={n} centroid={centroid}")


# 47-48 — the worker kills a child tree only while the child is alive --------------------
class _FakeProc:
    """A Popen stand-in whose stdout raises mid-stream. ``alive`` selects what
    poll() reports at that moment."""

    def __init__(self, alive):
        self.pid = 987654
        self.returncode = None if alive else 0
        self.stderr = iter(())
        self._alive = alive

    @property
    def stdout(self):
        yield "Detecting corners...\n"
        raise RuntimeError("decoder blew up")

    def wait(self, timeout=None):
        return self.returncode

    def poll(self):
        return None if self._alive else 0


def run_fake_worker(alive):
    killed = []
    real_sub, real_kill = cw.subprocess, cw._kill_tree
    cw.subprocess = types.SimpleNamespace(
        Popen=lambda *a, **k: _FakeProc(alive), PIPE=subprocess.PIPE,
        TimeoutExpired=subprocess.TimeoutExpired)
    cw._kill_tree = killed.append
    got = {}
    try:
        worker = cw.CalibrationWorker(Path("."), REPO / "1_calibrate.py")
        worker.finished.connect(lambda ok, m: got.update(ok=ok, msg=m), Qt.DirectConnection)
        with contextlib.redirect_stdout(io.StringIO()):
            worker.start()
            worker.wait(10_000)
    finally:
        cw.subprocess, cw._kill_tree = real_sub, real_kill
    return killed, got


killed, got = run_fake_worker(alive=False)
check(47, "an exception after the child exited does not kill its (reusable) pid",
      killed == [] and got.get("ok") is False and "decoder blew up" in got.get("msg", ""),
      f"killed={killed} got={got}")
killed, got = run_fake_worker(alive=True)
check(48, "an exception while the child is alive still kills the tree",
      killed == [987654] and got.get("ok") is False, f"killed={killed} got={got}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL CALIBRATE TESTS PASS")
