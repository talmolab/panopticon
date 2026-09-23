# Runs in the PROJECT environment (uv sync), NOT an isolated PEP 723 one.
# It used to carry inline dependency metadata, which made `uv run` resolve a
# separate environment on first use -- so a rig with no network could install
# completely and then fail at the first solve. Its requirements live in
# pyproject.toml instead, which makes `uv sync` a one-shot install.
#
# Two of those requirements are load-bearing:
#   opencv-contrib-python >= 4.7 -- CharucoBoard.setLegacyPattern exists only
#     from 4.7. A legacy board silently returns 0 charuco corners without it,
#     and 4.6 moved chessboardCorners from an attribute to a method.
#   matplotlib -- only for the reprojection histogram, which is skipped if
#     absent. Nothing else needs it.
"""Multi-view camera calibration from ChArUco corner detections.

Run with: uv run 1_calibrate.py <session_dir> --board-config <board.yaml>

The session directory should contain a calibration/ subfolder with per-camera
video subdirectories (cam1/, cam2/, ...) produced by the Panopticon GUI.

Outputs, all into the calibration directory:
  calibration.toml          aniposelib-compatible cameras plus a [metadata]
                            block with every quality figure of the solve
  calibration_report.json   the same figures as JSON for the GUI
  reprojection_error_histogram.png   pairwise stereo RMS bar chart

Detections are paired across cameras by trigger ordinal: frame i of a camera's
video is trigger ``blockids.npy[i]`` (unwrapped), and two cameras pair on the
triggers both detected the board in. Cameras drop frames independently, so
frame i of two videos is the same trigger only after alignment; pairing by
ordinal holds before it as well. When any camera lacks a ``blockids.npy`` that
matches its video, every camera pairs by frame index instead, which is right
only for videos already aligned across cameras, and the report records the
rule used (``pairing``).

Exit protocol: every failure prints ``ERROR_CODE=<code>`` to stderr before
exiting 1, so the GUI classifies the failure from the code instead of guessing
from traceback text. A solve that drops cameras (too few detections, failed
intrinsics, unreadable video, a disconnected coverage graph) still exits 0 and
writes ``partial = true`` with the dropped list into the metadata, because the
solved cameras are usable and the GUI reports "solved N of M".
"""
import argparse
import json
import math
import os
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

# The script lives at the repository root next to gui_app/; make that import
# work whatever the caller's cwd is.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from gui_app import charuco  # noqa: E402
from gui_app.board_detector import (  # noqa: E402
    CODET_KEY_BLOCK_IDS, calibration_video, codet_hint_key, codet_indices)
from gui_app.frame_sync import unwrap_blockids  # noqa: E402


# ---------------------------------------------------------------------------
# Failure protocol
# ---------------------------------------------------------------------------

#: Machine-readable failure codes, printed as ``ERROR_CODE=<code>`` on stderr.
ERROR_CODES = {
    "NO_CALIB_DIR": "calibration/ not found in the session directory",
    "NO_VIDEOS": "no calibration videos found",
    "BAD_BOARD_CONFIG": "the board config is unreadable, lacks a required key, "
                        "or names a dictionary or layout this OpenCV build "
                        "cannot express",
    "NO_DETECTIONS": "fewer than 2 cameras with board detections",
    "NO_INTRINSICS": "fewer than 2 cameras with valid intrinsics",
    "NO_PAIRS": "no camera pair with co-detections",
    "DISCONNECTED": "fewer than 2 cameras in the largest connected group",
}


def fail(code: str, message: str):
    """Print the code and message to stderr and exit 1."""
    assert code in ERROR_CODES, code
    print("ERROR_CODE={}".format(code), file=sys.stderr, flush=True)
    print("ERROR: {}".format(message), file=sys.stderr, flush=True)
    sys.exit(1)


def warn(message: str, warnings: list | None = None):
    """Print a diagnostic to stderr (the stream the GUI shows) and record it."""
    print("  WARNING: {}".format(message), file=sys.stderr, flush=True)
    if warnings is not None:
        warnings.append(message)


def notice(message: str):
    """Print a note to stderr without adding it to the report's warnings.

    The report's warnings raise a dialog in the GUI, so a note the operator
    cannot act on (an older file layout, a full scan instead of hints) goes
    here instead.
    """
    print("  NOTE: {}".format(message), file=sys.stderr, flush=True)


def _write_text_atomic(path, text: str) -> None:
    """Write ``text`` through a sibling temporary file and os.replace.

    RULE: an output file is replaced whole or not at all. REASON: the GUI
    reads the report and copies calibration.toml into the recording, and a
    solve that dies mid-write would otherwise leave a truncated file that
    reads as a finished calibration.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Board setup
# ---------------------------------------------------------------------------

#: ChArUco corners a view needs before it is used at all, and the corners two
#: views of one trigger must share before they enter a stereo pair.
MIN_CORNERS = 6
#: Detection frames a camera needs before its intrinsics are solved.
INTRINSICS_MIN_FRAMES = 20
#: Pose-diverse cap on the frames the intrinsics are fitted on.
INTRINSICS_MAX_FRAMES = 60


def load_board(path):
    """``(board_cfg, board)`` for a board config file, or exit BAD_BOARD_CONFIG.

    Every way a board config can be wrong ends here with the code and the
    file named: unreadable or not YAML, a required key missing or not a
    number, an unknown dictionary, or a legacy layout this OpenCV build
    cannot express.
    """
    try:
        cfg = charuco.load_board_config(path)
        board, _ = charuco.make_board(cfg)
    except (ValueError, RuntimeError, KeyError, TypeError, cv2.error) as e:
        msg = str(e)
        if str(path) not in msg:
            msg = "{}: {}".format(path, msg)
        fail("BAD_BOARD_CONFIG", msg)
    return cfg, board


def get_charuco_obj_points(board):
    """Return {corner_id: ndarray(3,)} — each charuco corner's 3D position."""
    # OpenCV renamed this across the 4.6/4.7 boundary (attribute -> getter).
    pts = (board.getChessboardCorners() if hasattr(board, "getChessboardCorners")
           else board.chessboardCorners)
    return {i: pts[i].ravel().astype(np.float32) for i in range(len(pts))}


# ---------------------------------------------------------------------------
# Camera directories and trigger ordinals
# ---------------------------------------------------------------------------

def camera_dirs(calib_dir):
    """The ``cam*`` directories under ``calib_dir``, sorted by name."""
    return sorted(d for d in Path(calib_dir).iterdir()
                  if d.is_dir() and d.name.startswith("cam"))


def camera_ordinals(cam_dir):
    """``(ordinals, None)`` or ``(None, reason)`` for one camera directory.

    ``ordinals`` is the camera's ``blockids.npy`` unwrapped, so
    ``ordinals[i]`` is the trigger of video frame i. The detection worker
    checks the length against the video, because a list that describes
    another number of frames maps every detection to the wrong trigger.
    """
    path = Path(cam_dir) / "blockids.npy"
    if not path.exists():
        return None, "no blockids.npy"
    try:
        ids = np.load(path)
    except (OSError, ValueError) as e:
        return None, "blockids.npy is unreadable ({})".format(e)
    if ids.ndim != 1 or ids.size == 0:
        return None, "blockids.npy is empty or not one-dimensional"
    try:
        return unwrap_blockids(ids), None
    except ValueError as e:
        return None, "blockids.npy: {}".format(e)


# ---------------------------------------------------------------------------
# Detection (parallelized across cameras)
# ---------------------------------------------------------------------------

def _detect_one_camera(task):
    """Detect ChArUco corners in one camera's video.

    ``task`` is a dict: ``video`` (path), ``board`` (config), ``skip`` (the
    full-scan stride), ``hint_kind`` (``"block_ids"``, ``"frames"`` or None),
    ``hints`` (list or None) and ``ordinals`` (the camera's unwrapped
    blockids.npy as an int64 array, or None).

    Returns a dict: cam, frames (video indices with a detection), keys (the
    trigger ordinals of those frames, or None without usable ordinals),
    corners, ids, total, size, error (None or a message), ordinal_error (why
    the ordinals are unusable, or None), hint_note (why this camera's hints
    were not used, or None), absent (hinted block IDs its video lacks) and
    clamped (frame hints pulled into range).

    Any exception is caught and returned as ``error``: one camera whose video
    breaks the decoder is dropped as unreadable, and the others still solve.
    """
    out = {"cam": Path(task["video"]).parent.name, "frames": [], "keys": None,
           "corners": [], "ids": [], "total": 0, "size": (0, 0),
           "error": None, "ordinal_error": None, "hint_note": None,
           "absent": 0, "clamped": 0}
    try:
        _detect_into(task, out)
    except Exception as e:
        out.update(frames=[], keys=None, corners=[], ids=[],
                   error="{}: {}".format(type(e).__name__, e))
    return out


def _detect_into(task, out):
    """The body of ``_detect_one_camera``; fills ``out`` in place."""
    video_path = task["video"]
    board_cfg = task["board"]
    # Each worker process is one camera; two OpenCV threads apiece keeps a
    # worker per camera from oversubscribing the host.
    cv2.setNumThreads(2)
    aruco = cv2.aruco
    # Board and marker detector share one API choice with the HUD, so the
    # markers the HUD counted are the markers the solve interpolates from.
    board, aruco_dict = charuco.make_board(board_cfg)
    _detect_markers = charuco.make_marker_detector(aruco_dict)

    def _detect(gray):
        mc, mi = _detect_markers(gray)
        if mi is None or len(mi) < 2:
            return None, None
        ret, cc, ci = aruco.interpolateCornersCharuco(mc, mi, gray, board)
        if ci is None or len(ci) < MIN_CORNERS:
            return None, None
        return cc.reshape(-1, 2), ci.ravel()

    cap = cv2.VideoCapture(str(video_path))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        out["total"], out["size"] = total, (w, h)
        if not cap.isOpened() or total <= 0:
            out["error"] = "could not open {} (opened={}, frames={})".format(
                video_path, cap.isOpened(), total)
            return

        ordinals = task.get("ordinals")
        if ordinals is not None and len(ordinals) != total:
            out["ordinal_error"] = ("blockids.npy lists {} frames but the "
                                    "video has {}".format(len(ordinals), total))
            ordinals = None

        targets = None
        kind, hints = task.get("hint_kind"), task.get("hints")
        if kind == "block_ids" and hints is not None:
            if ordinals is None:
                out["hint_note"] = ("its hints are block IDs and it has no "
                                    "usable blockids.npy to map them to "
                                    "frames; scanned in full")
            else:
                targets, out["absent"] = codet_indices(hints, ordinals)
        elif kind == "frames" and hints is not None:
            # Frame hints (format 2 and the flat layout) are GRABBED-frame
            # counts read after the frame copy, so they sit one above the
            # 0-based video index, and the last would point one past the end.
            # They are clamped into range and used as neighbourhood hints.
            targets = [min(max(int(t), 0), total - 1) for t in hints]
            out["clamped"] = sum(1 for t in hints if not 0 <= int(t) < total)

        frames_with_det, all_corners, all_ids = [], [], []
        if targets is not None:
            targets = set(targets)
            last_target = max(targets) if targets else -1
            frame_n = 0
            while frame_n <= last_target:
                if frame_n in targets:
                    ret, frame = cap.read()
                    if not ret:
                        break
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    corners, ids = _detect(gray)
                    if ids is not None:
                        frames_with_det.append(frame_n)
                        all_corners.append(corners.astype(np.float32))
                        all_ids.append(ids.astype(np.int32))
                else:
                    if not cap.grab():
                        break
                frame_n += 1
        else:
            # Search stride plus burst: every skip-th frame is examined while
            # the board is absent, and a hit re-arms a burst of `skip`
            # consecutive frames so a visible board is sampled densely.
            # `--skip` therefore thins the search, not the detections.
            skip = max(1, int(task.get("skip", 3)))
            burst = skip
            frame_n = 0
            go = 0
            while True:
                ret, frame = cap.read()
                if not ret:
                    break
                if frame_n % skip != 0 and go <= 0:
                    frame_n += 1
                    continue
                go = max(0, go - 1)
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                corners, ids = _detect(gray)
                if ids is not None:
                    frames_with_det.append(frame_n)
                    all_corners.append(corners.astype(np.float32))
                    all_ids.append(ids.astype(np.int32))
                    go = burst
                frame_n += 1
    finally:
        cap.release()

    out["frames"], out["corners"], out["ids"] = (
        frames_with_det, all_corners, all_ids)
    if ordinals is not None:
        out["keys"] = [int(ordinals[i]) for i in frames_with_det]


def load_codet_hints(codet_path, calib_dir, warnings):
    """Read the HUD's co-detection hint file, or None for a full scan.

    Returns ``{"kind", "format", "cameras"}``: ``kind`` is ``"block_ids"``
    (format 3) or ``"frames"`` (format 2 and the flat legacy layout, which
    list grabbed-frame counts), and ``cameras`` maps camera name to its list.

    A stamped file is used only when the videos it names (name and size) are
    the ones about to be opened: a hint file left behind by a previous
    calibration would otherwise decode the previous run's frames from the new
    videos, which yields sparse or empty detections with no error. Any
    mismatch discards the whole file. A camera the stamp left out (it had no
    single calibration mp4 then) is scanned in full, and so is every camera
    of a file that was never stamped, because such a file cannot be told
    apart from a stale one. A file that cannot be read or parsed is set aside
    with a warning and the solve scans in full.

    The flat legacy layout carries no video identity and is used as-is. The
    notes about it and about an unstamped file go to stderr only, not into
    ``warnings``: the report's warning list raises a dialog in the GUI, and a
    layout detail or a slower full scan is nothing the operator can act on.
    """
    codet_path = Path(codet_path)
    if not codet_path.exists():
        return None
    try:
        with open(codet_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        warn("{} is unreadable ({}); scanning the videos".format(
            codet_path.name, e), warnings)
        return None
    key = codet_hint_key(doc)
    if key is None:
        lists, videos, fmt = doc, None, 1
    else:
        lists, videos, fmt = doc[key], doc.get("videos") or {}, doc.get("format")
    kind = "block_ids" if key == CODET_KEY_BLOCK_IDS else "frames"
    if not isinstance(lists, dict) or not lists:
        warn("{} holds no hints; scanning the videos".format(codet_path.name),
             warnings)
        return None
    try:
        cameras = {str(cam): [int(x) for x in v] for cam, v in lists.items()}
    except (TypeError, ValueError) as e:
        warn("{} is malformed ({}); scanning the videos".format(
            codet_path.name, e), warnings)
        return None
    if videos is None:
        notice("{} carries no video identity (older GUI), so a stale hint "
               "file cannot be detected; hints used as-is".format(
                   codet_path.name))
        return {"kind": kind, "format": fmt, "cameras": cameras}
    if not isinstance(videos, dict) or not videos:
        notice("{} was never stamped against its videos (the acquisition "
               "did not finish its post-processing), so it cannot be told "
               "from a stale file; scanning the videos".format(
                   codet_path.name))
        return None
    for cam, ident in videos.items():
        try:
            mp4 = calibration_video(Path(calib_dir) / cam)
        except ValueError as e:
            warn("{}: cannot tell which video {}'s hints describe ({}); "
                 "hints ignored, scanning the videos".format(
                     codet_path.name, cam, e), warnings)
            return None
        actual = (None if mp4 is None
                  else {"name": mp4.name, "size": mp4.stat().st_size})
        if actual != ident:
            warn("{} was recorded for {} but the video is {}; hints "
                 "ignored, scanning the videos".format(
                     codet_path.name, ident, actual), warnings)
            return None
    unstamped = sorted(set(cameras) - set(videos))
    if unstamped:
        notice("{}: {} not stamped with a video identity; scanned in "
               "full".format(codet_path.name, ", ".join(unstamped)))
    cameras = {cam: v for cam, v in cameras.items() if cam in videos}
    if not cameras:
        return None
    return {"kind": kind, "format": fmt, "cameras": cameras}


def detect_all_cameras(cam_dirs, board_cfg, excluded, hints=None,
                       warnings=None, skip=3):
    """Run corner detection on every camera directory with a calibration mp4.

    Returns ``(results, sizes, notes)``. ``results`` maps cam to the worker's
    dict (see ``_detect_one_camera``), ``sizes`` maps cam to (w, h), and
    ``notes`` has ``unreadable`` (cameras whose video could not be opened,
    decoded or chosen) and ``no_video`` (camera directories without a
    calibration mp4). Both are reported on stderr, because a camera that
    vanishes from the toml with no message is the failure that looks like
    success.
    """
    if hints:
        print("  Using co-detection hints ({} cameras, {})".format(
            len(hints["cameras"]), "block IDs" if hints["kind"] == "block_ids"
            else "frame numbers"))
    notes = {"unreadable": [], "no_video": []}
    tasks = []
    for cam_dir in cam_dirs:
        if cam_dir.name in excluded:
            continue
        try:
            mp4 = calibration_video(cam_dir)
        except ValueError as e:
            notes["unreadable"].append(cam_dir.name)
            warn("{}: {}; skipped".format(cam_dir.name, e), warnings)
            continue
        if mp4 is None:
            notes["no_video"].append(cam_dir.name)
            warn("{}: no calibration mp4 in {}; skipped".format(
                cam_dir.name, cam_dir), warnings)
            continue
        ordinals, why = camera_ordinals(cam_dir)
        tasks.append({
            "video": str(mp4), "board": board_cfg, "skip": skip,
            "hint_kind": hints["kind"] if hints else None,
            "hints": hints["cameras"].get(cam_dir.name) if hints else None,
            "ordinals": ordinals, "ordinals_missing": why,
        })

    results, sizes = {}, {}
    if not tasks:
        return results, sizes, notes
    with ProcessPoolExecutor(max_workers=len(tasks)) as pool:
        for task, r in zip(tasks, pool.map(_detect_one_camera, tasks)):
            cam = r["cam"]
            if r["error"]:
                notes["unreadable"].append(cam)
                warn("{}: {}".format(cam, r["error"]), warnings)
                continue
            if r["ordinal_error"] is None and task["ordinals"] is None:
                r["ordinal_error"] = task["ordinals_missing"]
            results[cam] = r
            sizes[cam] = r["size"]
            n_corners = sum(len(i) for i in r["ids"])
            avg = n_corners / len(r["frames"]) if r["frames"] else 0
            extra = ""
            if r["clamped"]:
                extra += "  ({} hints clamped into range)".format(r["clamped"])
            if r["absent"]:
                extra += "  ({} hinted block IDs not in this video)".format(
                    r["absent"])
            print("  {}: {}/{} frames with corners (avg {:.1f}/frame){}".format(
                cam, len(r["frames"]), r["total"], avg, extra), flush=True)
            if r["hint_note"]:
                warn("{}: {}".format(cam, r["hint_note"]), warnings)
    return results, sizes, notes


def choose_pairing(results, warnings):
    """``(pairing, dets)``: the pairing rule and cam -> (keys, corners, ids).

    ``pairing`` is ``"block_id"`` when every readable camera has trigger
    ordinals for its frames, and ``"frame_index"`` otherwise, for every
    camera, because an ordinal and a frame index cannot be compared. Some
    cameras without usable ordinals while others have them means the session
    is inconsistent, and that is a warning; no camera with them (videos from
    elsewhere) is only a note.
    """
    missing = {cam: r["ordinal_error"] for cam, r in results.items()
               if r["keys"] is None}
    if not missing:
        return "block_id", {cam: (r["keys"], r["corners"], r["ids"])
                            for cam, r in results.items()}
    why = "; ".join("{}: {}".format(cam, missing[cam])
                    for cam in sorted(missing))
    text = ("pairing detections by frame index, which is right only for "
            "videos aligned across cameras, because {} of {} cameras have no "
            "usable trigger ordinals ({})".format(
                len(missing), len(results), why))
    if len(missing) == len(results) and all(
            m == "no blockids.npy" for m in missing.values()):
        notice(text)
    else:
        warn(text, warnings)
    return "frame_index", {cam: (r["frames"], r["corners"], r["ids"])
                           for cam, r in results.items()}


# ---------------------------------------------------------------------------
# Correspondences: charuco corner -> 3D/2D point arrays
# ---------------------------------------------------------------------------

def _build_pts(corners, ids, corner_obj):
    """Build (obj_pts, img_pts) for one frame. Returns None pair if empty."""
    mask = np.isin(ids, list(corner_obj.keys()))
    valid_ids = ids[mask]
    valid_corners = corners[mask]  # (M, 2)
    if len(valid_ids) == 0:
        return None, None
    obj = np.stack([corner_obj[int(m)] for m in valid_ids])  # (M, 3)
    return (obj.reshape(-1, 1, 3).astype(np.float32),
            valid_corners.reshape(-1, 1, 2).astype(np.float32))


# ---------------------------------------------------------------------------
# Intrinsic calibration
# ---------------------------------------------------------------------------

def _poses_from_pts(obj_list, img_list, K, dist_coeffs):
    """Run solvePnP per frame -> (rvec, tvec, mean_reproj_err, n_points) or None."""
    poses = []
    for obj, img in zip(obj_list, img_list):
        obj_3d = obj.reshape(-1, 3)
        img_2d = img.reshape(-1, 2)
        ok, rvec, tvec = cv2.solvePnP(obj_3d, img_2d, K, dist_coeffs)
        if not ok:
            poses.append(None)
            continue
        projected, _ = cv2.projectPoints(obj_3d, rvec, tvec, K, dist_coeffs)
        err = np.linalg.norm(projected.reshape(-1, 2) - img_2d, axis=1).mean()
        poses.append((rvec.ravel(), tvec.ravel(), err, obj_3d.shape[0]))
    return poses


def _pose_diverse_sample(poses, k, reproj_percentile=90):
    """Select k indices maximising 6-D pose diversity after quality filtering.

    Drops frames whose solvePnP reprojection error exceeds the given percentile,
    then farthest-point samples in normalised [rvec, tvec] space so the selected
    subset spans the full range of board orientations and positions.
    """
    valid = [(i, p) for i, p in enumerate(poses) if p is not None]
    if len(valid) <= k:
        return [i for i, _ in valid]

    errors = np.array([p[2] for _, p in valid])
    cutoff = np.percentile(errors, reproj_percentile)
    filtered = [(i, p) for i, p in valid if p[2] <= cutoff]
    if len(filtered) <= k:
        return [i for i, _ in filtered]

    feats = np.array([np.concatenate([p[0], p[1]]) for _, p in filtered])
    stds = feats.std(axis=0)
    stds[stds < 1e-12] = 1.0
    normed = feats / stds

    sel = [0]
    min_d = np.full(len(filtered), np.inf)
    for _ in range(k - 1):
        d = np.linalg.norm(normed - normed[sel[-1]], axis=1)
        min_d = np.minimum(min_d, d)
        min_d[sel] = -1
        sel.append(int(np.argmax(min_d)))

    return sorted(filtered[s][0] for s in sel)


def calibrate_intrinsics(corners_list, ids_list, corner_obj, image_size,
                         min_corners=MIN_CORNERS,
                         max_frames=INTRINSICS_MAX_FRAMES,
                         min_frames=INTRINSICS_MIN_FRAMES):
    obj_all, img_all = [], []
    for corners, ids in zip(corners_list, ids_list):
        if len(ids) < min_corners:
            continue
        obj, img = _build_pts(corners, ids, corner_obj)
        if obj is None:
            continue
        obj_all.append(obj)
        img_all.append(img)

    if len(obj_all) < min_frames:
        return None

    if len(obj_all) > max_frames:
        w, h = image_size
        K_rough = np.array([[w, 0, w * 0.5], [0, w, h * 0.5], [0, 0, 1]],
                           dtype=np.float64)
        poses = _poses_from_pts(obj_all, img_all, K_rough, np.zeros(5))
        indices = _pose_diverse_sample(poses, max_frames)
        obj_all = [obj_all[i] for i in indices]
        img_all = [img_all[i] for i in indices]

    flags = (cv2.CALIB_FIX_ASPECT_RATIO
             | cv2.CALIB_FIX_K3
             | cv2.CALIB_ZERO_TANGENT_DIST)
    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_all, img_all, image_size, None, None, flags=flags)
    return rms, K, dist, len(obj_all)


# ---------------------------------------------------------------------------
# Pairwise stereo calibration
# ---------------------------------------------------------------------------

#: Shared frames a pair needs before it is solved at all. Below this the
#: relative pose is too poorly conditioned to be worth reporting.
PAIR_MIN_FRAMES = 5
#: Shared frames a pair needs to rank as a full-strength tree edge. Pairs with
#: fewer still connect the graph, but only when nothing better does.
MIN_TREE_FRAMES = 10
#: Pose-diverse cap on shared frames per pair, and the saturation point of the
#: edge weight's frame-count term.
STEREO_MAX_FRAMES = 30


def shared_views(data_a, data_b, corner_obj, min_corners=MIN_CORNERS):
    """The views two cameras share, as ``(obj, img_a, img_b)`` lists.

    ``data_*`` is ``(keys, corners, ids)``; a key is a trigger ordinal or a
    frame index (see ``choose_pairing``), and the two cameras share a view on
    every key both detected with at least ``min_corners`` corners in common.
    """
    keys_a, corners_a, ids_a = data_a
    keys_b, corners_b, ids_b = data_b
    idx_a = {k: i for i, k in enumerate(keys_a)}
    idx_b = {k: i for i, k in enumerate(keys_b)}
    shared = sorted(set(keys_a) & set(keys_b))

    obj_list, img_a_list, img_b_list = [], [], []
    for key in shared:
        ia, ib = idx_a[key], idx_b[key]
        ca, ida = corners_a[ia], ids_a[ia]
        cb, idb = corners_b[ib], ids_b[ib]

        common = np.intersect1d(ida, idb)
        common = common[np.isin(common, list(corner_obj.keys()))]
        if len(common) < min_corners:
            continue

        # Vectorized gather: for each common corner, find its index in each camera
        idx_in_a = np.searchsorted(np.sort(ida), common)
        sort_order_a = np.argsort(ida)
        idx_in_a = sort_order_a[idx_in_a]

        idx_in_b = np.searchsorted(np.sort(idb), common)
        sort_order_b = np.argsort(idb)
        idx_in_b = sort_order_b[idx_in_b]

        obj = np.stack([corner_obj[int(m)] for m in common])  # (M, 3)
        obj_list.append(obj.reshape(-1, 1, 3).astype(np.float32))
        img_a_list.append(ca[idx_in_a].reshape(-1, 1, 2).astype(np.float32))
        img_b_list.append(cb[idx_in_b].reshape(-1, 1, 2).astype(np.float32))
    return obj_list, img_a_list, img_b_list


def calibrate_pair(data_a, data_b, corner_obj, K_a, d_a, K_b, d_b,
                   image_size, min_corners=MIN_CORNERS,
                   min_frames=PAIR_MIN_FRAMES):
    """``(R, T, rms, frames_used, codetections)`` for a pair, or None.

    ``codetections`` is the number of views the two cameras share
    (``shared_views``); at most ``STEREO_MAX_FRAMES`` of them, chosen for pose
    diversity, are fitted.
    """
    obj_list, img_a_list, img_b_list = shared_views(
        data_a, data_b, corner_obj, min_corners)
    n_shared = len(obj_list)
    if n_shared < min_frames:
        return None

    if n_shared > STEREO_MAX_FRAMES:
        poses = _poses_from_pts(obj_list, img_a_list, K_a, d_a)
        idx = _pose_diverse_sample(poses, STEREO_MAX_FRAMES)
        obj_list = [obj_list[i] for i in idx]
        img_a_list = [img_a_list[i] for i in idx]
        img_b_list = [img_b_list[i] for i in idx]

    rms, _, _, _, _, R, T, _, _ = cv2.stereoCalibrate(
        obj_list, img_a_list, img_b_list,
        K_a, d_a, K_b, d_b, image_size,
        flags=cv2.CALIB_FIX_INTRINSIC)
    return R, T, rms, len(obj_list), n_shared


# ---------------------------------------------------------------------------
# Global extrinsics via spanning tree
# ---------------------------------------------------------------------------

#: Added to the weight of a pair below MIN_TREE_FRAMES so every full-strength
#: frontier edge ranks ahead of it in Prim's step.
WEAK_EDGE_PENALTY = 1e6


def edge_weight(rms, n, min_tree_frames=MIN_TREE_FRAMES):
    """Prim's weight for a stereo pair.

    ``rms / sqrt(min(n, STEREO_MAX_FRAMES))``: a pose fitted on few frames can
    report a low RMS while its baseline is poorly conditioned, so the frame
    count discounts the RMS, saturating at the pose-diverse cap. Pairs under
    ``min_tree_frames`` carry ``WEAK_EDGE_PENALTY`` so they join the tree only
    when no better-observed edge reaches the same camera.
    """
    w = float(rms) / math.sqrt(max(1, min(int(n), STEREO_MAX_FRAMES)))
    if n < min_tree_frames:
        w += WEAK_EDGE_PENALTY
    return w


def pair_entry(pairwise, a, b):
    """The (R, T, rms, n) tuple for a pair in either key order, or None."""
    if (a, b) in pairwise:
        return pairwise[(a, b)]
    return pairwise.get((b, a))


def connected_components(cam_names, pairwise):
    """Connected components of the solved-pair graph.

    Union-find over every solved pair. Sorted largest first; ties broken by the
    summed shared-frame count of the component's pairs, then by first camera
    name, so the choice of "largest" is deterministic.
    """
    names = list(cam_names)
    index = {c: i for i, c in enumerate(names)}
    parent = list(range(len(names)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for (a, b) in pairwise:
        if a in index and b in index:
            parent[find(index[a])] = find(index[b])

    groups: dict[int, list[str]] = {}
    for c in names:
        groups.setdefault(find(index[c]), []).append(c)

    def frames_in(group):
        gs = set(group)
        return sum(int(v[3]) for (a, b), v in pairwise.items()
                   if a in gs and b in gs)

    comps = [sorted(g) for g in groups.values()]
    comps.sort(key=lambda g: (-len(g), -frames_in(g), g[0]))
    return comps


def build_graph(cam_names, pairwise, min_tree_frames=MIN_TREE_FRAMES):
    """Choose the cameras to solve and their spanning tree.

    Returns ``(tree_edges, kept, dropped, components)``. The LARGEST connected
    component is kept (ties by summed frames), never the one that happens to
    hold the first camera name, and every other camera is ``dropped``. The tree
    is Prim's over the kept component with ``edge_weight``.
    """
    components = connected_components(cam_names, pairwise)
    if not components:
        return [], [], [], []
    kept = components[0]
    dropped = sorted(c for g in components[1:] for c in g)

    adj = defaultdict(list)
    for (a, b), (_, _, rms, n) in pairwise.items():
        if a in kept and b in kept:
            w = edge_weight(rms, n, min_tree_frames)
            adj[a].append((b, w))
            adj[b].append((a, w))

    in_tree = {kept[0]}
    edges = []
    while len(in_tree) < len(kept):
        best = None
        for node in sorted(in_tree):
            for nb, w in adj[node]:
                if nb not in in_tree and (best is None or w < best[2]):
                    best = (node, nb, w)
        if best is None:
            break
        edges.append((best[0], best[1]))
        in_tree.add(best[1])
    return edges, kept, dropped, components


def chain_extrinsics(cam_names, tree_edges, pairwise, ref_cam):
    """Chain pairwise R,T along spanning tree into global poses."""
    g_R = {ref_cam: np.eye(3, dtype=np.float64)}
    g_t = {ref_cam: np.zeros((3, 1), dtype=np.float64)}

    adj = defaultdict(list)
    for a, b in tree_edges:
        adj[a].append(b)
        adj[b].append(a)

    queue, visited = [ref_cam], {ref_cam}
    while queue:
        node = queue.pop(0)
        for nb in adj[node]:
            if nb in visited:
                continue
            visited.add(nb)
            queue.append(nb)

            if (node, nb) in pairwise:
                R_ab, T_ab = pairwise[(node, nb)][:2]
            else:
                R_ba, T_ba = pairwise[(nb, node)][:2]
                R_ab, T_ab = R_ba.T, -R_ba.T @ T_ba

            g_R[nb] = R_ab @ g_R[node]
            g_t[nb] = R_ab @ g_t[node] + T_ab

    out = {}
    for cam in cam_names:
        rvec, _ = cv2.Rodrigues(g_R[cam])
        out[cam] = (rvec.ravel(), g_t[cam].ravel())
    return out


# ---------------------------------------------------------------------------
# Reprojection error histogram
# ---------------------------------------------------------------------------

def save_reprojection_histogram(path, pair_rms):
    """Save a pairwise stereo RMS bar chart as PNG."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib not available, skipping histogram")
        return

    labels = sorted(pair_rms.keys())
    values = [pair_rms[k] for k in labels]
    if not values:
        return

    fig, ax = plt.subplots(figsize=(max(6, len(labels) * 0.8), 4))
    colors = ["#4CAF50" if v < 10 else "#FF9800" if v < 20 else "#F44336"
              for v in values]
    ax.bar(range(len(labels)), values, color=colors)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Stereo RMS (px)")
    ax.set_title("Pairwise calibration quality")
    ax.axhline(10, color="gray", linestyle="--", alpha=0.5, label="good (<10px)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        fig.savefig(str(tmp), dpi=120, format="png")
        os.replace(tmp, path)
    finally:
        plt.close(fig)
        if tmp.exists():
            tmp.unlink()
    print("  Histogram: {}".format(path))


# ---------------------------------------------------------------------------
# Quality report and metadata
# ---------------------------------------------------------------------------

def build_report(board_cfg, ref, active, tree, pairwise, intrinsic_stats,
                 components, dropped, hints_used, skip, warnings,
                 pairing=None, codetections=None):
    """Assemble every quality figure of a solve into one plain dict.

    Written verbatim as ``calibration_report.json`` and rendered into the
    toml's ``[metadata]``. ``dropped`` maps reason -> camera names for every
    camera that had (or should have had) a video but is not in the toml;
    ``partial`` is true when any of them is non-empty, so a consumer can tell a
    full solve from one that quietly lost cameras.

    ``pairing`` (``"block_id"`` or ``"frame_index"``) and ``codetections``
    (pair -> views shared before the pose-diverse cap) are recorded when
    given.
    """
    tree_rows = []
    for a, b in tree:
        entry = pair_entry(pairwise, a, b)
        tree_rows.append({"from": a, "to": b,
                          "rms": float(entry[2]), "frames": int(entry[3])})
    pairs = {}
    for (a, b), (_, _, rms, n) in sorted(pairwise.items()):
        row = {"rms": float(rms), "frames": int(n)}
        if codetections and (a, b) in codetections:
            row["codetections"] = int(codetections[(a, b)])
        pairs["{}-{}".format(a, b)] = row
    dropped = {k: sorted(v) for k, v in dropped.items()}
    report = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "opencv": cv2.__version__,
        "board": charuco.board_summary(board_cfg),
        "ref_camera": ref,
        "cameras": list(active),
        "partial": any(dropped.values()),
        "dropped": dropped,
        "components": [list(c) for c in components],
        "tree": tree_rows,
        "hints_used": bool(hints_used),
        "skip": int(skip),
        "intrinsics": {cam: {"rms": float(s["rms"]), "frames": int(s["frames"]),
                             "detections": int(s["detections"])}
                       for cam, s in intrinsic_stats.items() if cam in active},
        "pairs": pairs,
        "warnings": list(warnings),
    }
    if pairing is not None:
        report["pairing"] = pairing
    return report


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        return repr(float(v))
    if isinstance(v, str):
        # TOML basic strings share JSON's escapes for everything json.dumps
        # emits (\", \\, \n, \t, \uXXXX).
        return json.dumps(v)
    if isinstance(v, dict):
        return "{ " + ", ".join("{} = {}".format(_toml_key(k), _toml_value(x))
                                for k, x in v.items()) + " }"
    if isinstance(v, (list, tuple)):
        if not v:
            return "[]"
        return "[ " + ", ".join(_toml_value(x) for x in v) + ",]"
    raise TypeError("no TOML form for {!r}".format(type(v)))


def _toml_key(k):
    k = str(k)
    if k and all(c.isalnum() or c in "-_" for c in k):
        return k
    return json.dumps(k)


def metadata_toml_lines(meta):
    """Render a report dict as the ``[metadata]`` block of calibration.toml.

    Scalars and arrays sit under ``[metadata]``; nested dicts become
    ``[metadata.<name>]`` tables (``board``, ``dropped``) or one table per key
    (``intrinsics.<cam>``, ``pairs."a-b"``). aniposelib reads ``metadata`` as an
    opaque dict, so every key here is free-form.
    """
    lines = ["[metadata]"]
    tables = []
    for k, v in meta.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append("{} = {}".format(_toml_key(k), _toml_value(v)))
    lines.append("")
    for k, v in tables:
        nested = v and all(isinstance(x, dict) for x in v.values())
        if nested:
            for sub, row in v.items():
                lines.append("[metadata.{}.{}]".format(_toml_key(k), _toml_key(sub)))
                for kk, vv in row.items():
                    lines.append("{} = {}".format(_toml_key(kk), _toml_value(vv)))
                lines.append("")
        else:
            lines.append("[metadata.{}]".format(_toml_key(k)))
            for kk, vv in v.items():
                lines.append("{} = {}".format(_toml_key(kk), _toml_value(vv)))
            lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Output (aniposelib-compatible calibration.toml)
# ---------------------------------------------------------------------------

def write_calibration_toml(path, cam_names, intrinsics, extrinsics, sizes,
                           meta=None):
    """Write calibration.toml, one section per camera in ``cam_names`` order."""
    lines = []
    for i, cam in enumerate(cam_names):
        K, dist = intrinsics[cam]
        rvec, tvec = extrinsics[cam]
        w, h = sizes[cam]
        d = dist.ravel()
        if len(d) < 5:
            d = np.concatenate([d, np.zeros(5 - len(d))])
        lines.append("[cam_{}]".format(i))
        lines.append('name = "{}"'.format(cam))
        lines.append("size = [ {}, {},]".format(w, h))
        lines.append("matrix = [ [ {}, 0.0, {},], [ 0.0, {}, {},], [ 0.0, 0.0, 1.0,],]".format(
            repr(float(K[0, 0])), repr(float(K[0, 2])),
            repr(float(K[1, 1])), repr(float(K[1, 2]))))
        lines.append("distortions = [ {}, {}, {}, {}, {},]".format(
            repr(float(d[0])), repr(float(d[1])), repr(float(d[2])),
            repr(float(d[3])), repr(float(d[4]))))
        lines.append("rotation = [ {}, {}, {},]".format(
            repr(float(rvec[0])), repr(float(rvec[1])), repr(float(rvec[2]))))
        lines.append("translation = [ {}, {}, {},]".format(
            repr(float(tvec[0])), repr(float(tvec[1])), repr(float(tvec[2]))))
        lines.append("")
    lines.extend(metadata_toml_lines(meta or {}))
    _write_text_atomic(path, "\n".join(lines))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Multi-view calibration from ChArUco corner detections.")
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--board-config", type=Path, required=True)
    parser.add_argument("--excluded-views", nargs="*", default=[])
    parser.add_argument("--ref-camera", type=str, default="cam1")
    parser.add_argument("--skip", type=int, default=3,
                        help="Full-scan search stride (default 3): while the "
                             "board is absent every Nth frame is examined; a "
                             "hit re-arms a burst of N consecutive frames, so "
                             "a visible board is sampled densely. Ignored "
                             "when codet_frames.json hints are used.")
    args = parser.parse_args()

    sys.stdout.reconfigure(line_buffering=True)
    board_cfg, board = load_board(args.board_config)
    print("Board: {}x{}, sq={}, mk={}, legacy={}".format(
        board_cfg["board_x"], board_cfg["board_y"],
        board_cfg["square_length"], board_cfg["marker_length"],
        board_cfg.get("board_legacy", False)))
    corner_obj = get_charuco_obj_points(board)
    print("  {} charuco corners".format(len(corner_obj)))

    calib_dir = args.session_dir / "calibration"
    if not calib_dir.exists():
        fail("NO_CALIB_DIR", "calibration/ not found in {}".format(args.session_dir))

    cam_dirs = camera_dirs(calib_dir)
    non_cam = {d.name for d in calib_dir.iterdir()
               if d.is_dir() and not d.name.startswith("cam")}
    excluded = set(args.excluded_views) | non_cam

    warnings: list[str] = []
    dropped: dict[str, list[str]] = {
        "no_video": [], "unreadable": [], "few_detections": [],
        "failed_intrinsics": [], "isolated": []}

    # --- Detect ---
    hints = load_codet_hints(calib_dir / "codet_frames.json", calib_dir, warnings)
    if hints:
        print("\nDetecting corners (co-detection hints)...")
    else:
        print("\nDetecting corners (full scan, skip={})...".format(args.skip))
    results, all_sizes, notes = detect_all_cameras(
        cam_dirs, board_cfg, excluded, hints=hints, warnings=warnings,
        skip=args.skip)
    dropped["no_video"] = notes["no_video"]
    dropped["unreadable"] = notes["unreadable"]
    if not results:
        fail("NO_VIDEOS", "no readable calibration videos under {} "
             "(camera dirs: {})".format(calib_dir, len(cam_dirs)))
    pairing, all_dets = choose_pairing(results, warnings)
    print("  Pairing detections by {}".format(
        "trigger ordinal (blockids.npy)" if pairing == "block_id"
        else "frame index"))

    active = sorted(c for c in all_dets if len(all_dets[c][0]) >= 5)
    dropped["few_detections"] = sorted(set(all_dets) - set(active))
    if dropped["few_detections"]:
        warn("dropping {} (<5 detections)".format(
            ", ".join(dropped["few_detections"])), warnings)
    if len(active) < 2:
        fail("NO_DETECTIONS", "fewer than 2 cameras with detections")

    # --- Intrinsics (parallel — cv2 releases the GIL) ---
    print("\nIntrinsics...")

    def _intrinsic_job(cam):
        _keys, corners, ids = all_dets[cam]
        return cam, calibrate_intrinsics(corners, ids, corner_obj,
                                         all_sizes[cam])

    intrinsics = {}
    intrinsic_stats = {}
    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        for cam, result in pool.map(_intrinsic_job, active):
            if result is None:
                dropped["failed_intrinsics"].append(cam)
                warn("{}: intrinsics FAILED ({} detection frames, {} needed "
                     "with >= {} corners)".format(
                         cam, len(all_dets[cam][0]), INTRINSICS_MIN_FRAMES,
                         MIN_CORNERS), warnings)
                continue
            rms, K, dist, n = result
            intrinsics[cam] = (K, dist)
            intrinsic_stats[cam] = {"rms": rms, "frames": n,
                                    "detections": len(all_dets[cam][0])}
            print("  {}: RMS={:.3f}px  fx={:.0f}  ({} frames)".format(
                cam, rms, K[0, 0], n))

    active = [c for c in active if c in intrinsics]
    if len(active) < 2:
        fail("NO_INTRINSICS", "fewer than 2 cameras with valid intrinsics")

    # --- Pairwise stereo (parallel — cv2 releases the GIL) ---
    print("\nPairwise stereo...")
    pairs = [(active[i], active[j])
             for i in range(len(active)) for j in range(i + 1, len(active))]

    def _stereo_job(pair):
        ca, cb = pair
        Ka, da = intrinsics[ca]
        Kb, db = intrinsics[cb]
        result = calibrate_pair(
            all_dets[ca], all_dets[cb], corner_obj,
            Ka, da, Kb, db, all_sizes[ca])
        return ca, cb, result

    pairwise = {}
    codetections = {}
    with ThreadPoolExecutor(max_workers=min(len(pairs), 8)) as pool:
        for ca, cb, result in pool.map(_stereo_job, pairs):
            if result is None:
                continue
            R, T, rms, n, shared = result
            pairwise[(ca, cb)] = (R, T, rms, n)
            codetections[(ca, cb)] = shared
            print("  {}-{}: RMS={:.3f}  {} frames of {} co-detections".format(
                ca, cb, rms, n, shared))

    if not pairwise:
        fail("NO_PAIRS", "no camera pairs with co-detections")

    # --- Global extrinsics ---
    print("\nCamera graph...")
    tree, kept, isolated, components = build_graph(active, pairwise)
    if isolated:
        groups = "  ".join("{" + ",".join(c) + "}" for c in components)
        warn("coverage graph is disconnected: {}; keeping the largest group "
             "({} cameras), dropping {}".format(
                 groups, len(kept), ", ".join(isolated)), warnings)
        dropped["isolated"] = isolated
        active = list(kept)
    if len(active) < 2:
        fail("DISCONNECTED", "too few connected cameras")

    ref = args.ref_camera if args.ref_camera in active else active[0]
    if ref != args.ref_camera:
        warn("reference camera {} is not in the solve; using {}".format(
            args.ref_camera, ref), warnings)
    print("  ref={}, tree: {}".format(
        ref, " ".join("{}->{}".format(a, b) for a, b in tree)))

    extrinsics = chain_extrinsics(active, tree, pairwise, ref)

    # --- Quality summary + histogram ---
    print("\nPairwise quality:")
    pair_rms = {}
    for (ca, cb), (_, _, rms, n) in sorted(pairwise.items()):
        pair_rms["{}-{}".format(ca, cb)] = rms
        print("  {}-{}: RMS={:.1f}px  ({} frames)".format(ca, cb, rms, n))
        if rms > 20:
            warn("{}-{}: high stereo RMS ({:.1f}px)".format(ca, cb, rms), warnings)
        elif n < MIN_TREE_FRAMES:
            warn("{}-{}: only {} shared frames ({}+ needed for a full-strength "
                 "tree edge)".format(ca, cb, n, MIN_TREE_FRAMES), warnings)

    for cam in active:
        keys = all_dets[cam][0]
        if len(keys) < 30:
            warn("{}: only {} detection frames (30+ recommended)".format(
                cam, len(keys)), warnings)

    hist_path = calib_dir / "reprojection_error_histogram.png"
    save_reprojection_histogram(hist_path, pair_rms)

    # --- Write ---
    report = build_report(board_cfg, ref, active, tree, pairwise, intrinsic_stats,
                          components, dropped, hints_used=bool(hints),
                          skip=args.skip, warnings=warnings, pairing=pairing,
                          codetections=codetections)
    out = calib_dir / "calibration.toml"
    write_calibration_toml(out, active, intrinsics, extrinsics, all_sizes,
                           meta=report)
    report_path = calib_dir / "calibration_report.json"
    _write_text_atomic(report_path, json.dumps(report, indent=1))

    print("\nCalibration complete{}.".format(
        " (PARTIAL)" if report["partial"] else ""))
    print("  {}".format(out))
    print("  REPORT_PATH={}".format(report_path))
    print("  Cameras: {}".format(" ".join(active)))
    if report["partial"]:
        n_expected = len(active) + sum(len(v) for v in dropped.values())
        print("  Solved {} of {} cameras; dropped: {}".format(
            len(active), n_expected,
            "; ".join("{}: {}".format(k, ", ".join(v))
                      for k, v in dropped.items() if v)),
            file=sys.stderr)
    if warnings:
        print("\nWARNINGS:")
        for w in warnings:
            print("  - {}".format(w))
        print("\n  Consider recording calibration longer with the board "
              "visible to more cameras simultaneously.", file=sys.stderr)


if __name__ == "__main__":
    main()
