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

Cameras are taken in numeric order (cam2 before cam10). The toml numbers its
camera sections in that order, zero-padded to one width (``[cam_00]`` from 11
cameras up), so a string sort of the section keys, which is how aniposelib
reads them, keeps the camera order. Match cameras by each section's ``name``,
never by position.

Detections are paired across cameras by trigger ordinal: frame i of a camera's
video is trigger ``blockids.npy[i]`` (unwrapped), and two cameras pair on the
triggers both detected the board in. Cameras drop frames independently, so
frame i of two videos is the same trigger only after alignment; pairing by
ordinal holds before it as well. When any camera lacks a ``blockids.npy`` that
matches its video, every camera pairs by frame index instead, which is right
only for videos already aligned across cameras, and the report records the
rule used (``pairing``).

The report also records which serial each camera name had during the
calibration (``camera_serials``, from the calibration's
``session_metadata.json``), and warns when another acquisition in the same
session records a different serial under a name the calibration solved.

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
from gui_app.recording_meta import camera_sort_key  # noqa: E402
from gui_app.session_config import METADATA_FILENAME  # noqa: E402


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
#: Above this many views, a camera's views are judged against a reference fit
#: before the final fit. At or below it, every view goes straight into one
#: fit. It is also the size of the pose-diverse pick that one of the two first
#: fits is fitted on.
INTRINSICS_MAX_FRAMES = 60
#: Views, spread evenly through the take, that one of the two first fits is
#: fitted on before any view is judged (_reference_fit).
INTRINSICS_FIRST_PASS_FRAMES = 120
#: Views, spread evenly through the take from those within the cut, that the
#: final fit is fitted on.
INTRINSICS_FINAL_FRAMES = 120
#: A view whose reprojection error under the reference fit exceeds this many
#: times the median view's (and INTRINSICS_OUTLIER_FLOOR_PX) is left out of
#: the final fit: a misdetection or a pose-estimate flip, not a pose.
INTRINSICS_OUTLIER_FACTOR = 3.0
INTRINSICS_OUTLIER_FLOOR_PX = 1.0
#: A lens fit worse than this, in pixels, is warned about as unreliable.
INTRINSICS_RMS_WARN_PX = 1.5
#: A focal length outside this factor of the other cameras' median is warned
#: about when three or more cameras are solved.
INTRINSICS_FX_WARN_FACTOR = 1.6


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
# Camera directories, trigger ordinals and the serial map
# ---------------------------------------------------------------------------

def camera_dirs(calib_dir):
    """The ``cam*`` directories under ``calib_dir``, in numeric order."""
    return sorted((d for d in Path(calib_dir).iterdir()
                   if d.is_dir() and d.name.startswith("cam")),
                  key=lambda d: camera_sort_key(d.name))


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


def camera_serial_map(acq_dir) -> dict:
    """``{camera name: serial}`` from an acquisition's session_metadata.json.

    Empty when the file is missing or unreadable, or records no serials
    (sessions written before the metadata carried them), or when its name
    and serial lists differ in length, since the names could then not be
    matched to serials.
    """
    try:
        meta = json.loads((Path(acq_dir) / METADATA_FILENAME).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(meta, dict):
        return {}
    names, serials = meta.get("camera_names"), meta.get("camera_serials")
    if (not isinstance(names, list) or not isinstance(serials, list)
            or len(names) != len(serials)):
        return {}
    return {str(n): str(s) for n, s in zip(names, serials) if s is not None}


def serial_map_differences(calibration, other, names=None) -> list[str]:
    """``"cam3: 111 in the calibration, 222 here"`` for each name whose serial
    differs between two serial maps, in camera order.

    Only names both maps record are compared, and only those in ``names``
    when it is given (the cameras the solve kept).
    """
    common = set(calibration) & set(other)
    if names is not None:
        common &= set(names)
    return ["{}: {} in the calibration, {} here".format(
                cam, calibration[cam], other[cam])
            for cam in sorted(common, key=camera_sort_key)
            if calibration[cam] != other[cam]]


def check_serial_maps(session_dir, calib_dir, serials, active, warnings):
    """Warn for each other acquisition of the session whose serial map
    differs from the calibration's on a solved camera, and return their
    names.

    The extrinsics describe the physical cameras present during the
    calibration. A camera replaced or re-ordered since then keeps its name,
    so its extrinsics would attach to another camera with nothing else on
    disk to show it.
    """
    if not serials:
        return []
    mismatched = []
    calib_dir = Path(calib_dir).resolve()
    for acq in sorted(Path(session_dir).iterdir()):
        if not acq.is_dir() or acq.resolve() == calib_dir:
            continue
        if not (acq / METADATA_FILENAME).exists():
            continue
        diffs = serial_map_differences(serials, camera_serial_map(acq), active)
        if diffs:
            mismatched.append(acq.name)
            warn("{}/{} records other cameras under names this calibration "
                 "solved ({}). calibration.toml describes the cameras present "
                 "during the calibration, so the extrinsics of those names do "
                 "not fit {}. Recalibrate, or list camera_serials in the "
                 "profile so each name keeps its camera".format(
                     acq.name, METADATA_FILENAME, "; ".join(diffs), acq.name),
                 warnings)
    return mismatched


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
                if hints and not targets:
                    # Decoding nothing would drop the camera for want of
                    # detections that a scan may well find.
                    out["hint_note"] = ("none of its {} hinted block IDs is "
                                        "in its video; scanned in full".format(
                                            len(set(hints))))
                    targets = None
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
    unstamped = sorted(set(cameras) - set(videos), key=camera_sort_key)
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
                    for cam in sorted(missing, key=camera_sort_key))
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


def _homography_ok(obj, img) -> bool:
    """Whether one view's corners determine a board-to-image homography.

    RULE: a view goes to cv2.calibrateCamera only if this holds. REASON:
    calibrateCamera starts from one homography per view
    (initIntrinsicParams2D) and asserts on the first view that cannot give
    one, which kills the whole solve; a partial view whose corners all lie
    on one board row or column is such a view, and a detector that finds
    more partial views finds more of them. This is the same computation on
    the same points, so it rejects exactly the views OpenCV would.
    """
    try:
        H, _mask = cv2.findHomography(
            np.asarray(obj, np.float64).reshape(-1, 3)[:, :2],
            np.asarray(img, np.float64).reshape(-1, 2), 0)
    except cv2.error:
        return False
    return H is not None and H.shape == (3, 3) and bool(np.isfinite(H).all())


def calibrate_intrinsics(corners_list, ids_list, corner_obj, image_size,
                         min_corners=MIN_CORNERS,
                         max_frames=INTRINSICS_MAX_FRAMES,
                         min_frames=INTRINSICS_MIN_FRAMES, stats=None):
    """Fit one camera's intrinsics; None when too few usable views remain.

    `stats`, if given, is a dict that receives ``degenerate``: the views
    dropped because no homography fits their corners (see _homography_ok).
    With more than ``max_frames`` views it also receives ``outliers``: the
    views left out because the reference fit (_reference_fit) reprojects
    them beyond INTRINSICS_OUTLIER_FACTOR times the median view's error.
    """
    obj_all, img_all = [], []
    degenerate = 0
    for corners, ids in zip(corners_list, ids_list):
        if len(ids) < min_corners:
            continue
        obj, img = _build_pts(corners, ids, corner_obj)
        if obj is None:
            continue
        if not _homography_ok(obj, img):
            degenerate += 1
            continue
        obj_all.append(obj)
        img_all.append(img)
    if stats is not None:
        stats["degenerate"] = degenerate

    if len(obj_all) < min_frames:
        return None

    flags = (cv2.CALIB_FIX_ASPECT_RATIO
             | cv2.CALIB_FIX_K3
             | cv2.CALIB_ZERO_TANGENT_DIST)
    if len(obj_all) > max_frames:
        # RULE: views are judged against a reference fit, and the final fit
        # leaves out those beyond the cut. REASON: a long take holds
        # misdetections and pose-estimate flips, and a lens fit that keeps
        # them can have its focal length pulled off by a factor of two or
        # more.
        K0, d0, _poses, errors = _reference_fit(obj_all, img_all, image_size,
                                                flags, max_frames)
        finite = errors[np.isfinite(errors)]
        cut = max(INTRINSICS_OUTLIER_FLOOR_PX,
                  INTRINSICS_OUTLIER_FACTOR * float(np.median(finite))
                  if finite.size else INTRINSICS_OUTLIER_FLOOR_PX)
        keep = [i for i, e in enumerate(errors) if e <= cut]
        # Too few views within the cut means the reference itself is off, so
        # every view goes into the final fit and none is counted as left out.
        if stats is not None:
            stats["outliers"] = (len(obj_all) - len(keep)
                                 if len(keep) >= min_frames else 0)
        if len(keep) >= min_frames:
            obj_all = [obj_all[i] for i in keep]
            img_all = [img_all[i] for i in keep]
        # RULE: the final fit takes INTRINSICS_FINAL_FRAMES views spread
        # through the take from those within the cut, and solves with LU.
        # REASON: a farthest-point pick holds the least typical views, and
        # the lens fit moves with it. The solve chains pairwise poses and
        # never refines them jointly, so that movement can move the tree to
        # another pair and double the error of corners triangulated from the
        # other cameras, while the pair RMS stays the same. A spread sample
        # stands for the whole take. The solver's cost grows with the cube of
        # the view count, so a fit on every view within the cut of a long
        # take runs for many minutes.
        #
        # RULE: the final fit runs from two starts, the reference fit and
        # OpenCV's own guess, and keeps whichever reprojects the median view
        # within the cut closer. REASON: either start can settle on a
        # wrong focal length. OpenCV's guess does on some real takes, and a
        # reference fit that is itself off can lead the fit away from a sound
        # answer. The median view tells the two apart, as in _reference_fit.
        sel = _spread(len(obj_all), INTRINSICS_FINAL_FRAMES)
        obj_sel = [obj_all[i] for i in sel]
        img_sel = [img_all[i] for i in sel]
        best, failure = None, None
        for start in ((K0, d0), None):
            try:
                if start is None:
                    rms, K, dist, _r, _t = cv2.calibrateCamera(
                        obj_sel, img_sel, image_size, None, None,
                        flags=flags | cv2.CALIB_USE_LU)
                else:
                    rms, K, dist, _r, _t = cv2.calibrateCamera(
                        obj_sel, img_sel, image_size, start[0].copy(),
                        start[1].copy(),
                        flags=(flags | cv2.CALIB_USE_LU
                               | cv2.CALIB_USE_INTRINSIC_GUESS))
            except cv2.error as e:
                failure = e
                continue
            if not (np.isfinite(K).all() and np.isfinite(dist).all()):
                continue
            median = _median_view_error(obj_all, img_all, K, dist)
            if best is None or median < best[0]:
                best = (median, rms, K, dist)
        if best is None:
            raise (failure if failure is not None
                   else cv2.error("no start gave a final lens fit"))
        return best[1], best[2], best[3], len(sel)

    rms, K, dist, rvecs, tvecs = cv2.calibrateCamera(
        obj_all, img_all, image_size, None, None, flags=flags)
    return rms, K, dist, len(obj_all)


def _median_view_error(obj_all, img_all, K, dist) -> float:
    """The median over views of each view's mean reprojection error once
    posed under ``K, dist`` (a view PnP cannot pose counts as infinite)."""
    poses = _poses_from_pts(obj_all, img_all, K, dist)
    return float(np.median([p[2] if p is not None else np.inf for p in poses]))


def _spread(n: int, k: int) -> list:
    """``k`` indices spread evenly over ``range(n)`` (all of them when n <= k)."""
    if n <= k:
        return list(range(n))
    return sorted({int(round(x)) for x in np.linspace(0, n - 1, k)})


def _reference_fit(obj_all, img_all, image_size, flags, max_frames):
    """``(K, dist, poses, errors)``: every view posed under the better of two
    first fits, with its mean reprojection error (inf when PnP fails).

    RULE: one first fit is on INTRINSICS_FIRST_PASS_FRAMES views spread
    through the take, the other on the pose-diverse pick of ``max_frames``
    views under rough intrinsics. The views are judged against the one that
    reprojects the median view closer. REASON: either can be pulled off by
    the bad views it holds. A few views that no pose fits drag the spread
    fit, and the pick's own error filter drops them. Farthest-point sampling
    favours views that merely look unusual, which drag the pick, and the
    spread holds few of them. A fit pulled off puts the median view well
    above a sound fit's: the least median of squares criterion (Rousseeuw,
    "Least median of squares regression", JASA 79, 1984).

    RULE: both first fits solve with LU. REASON: the solver's cost grows with
    the cube of the view count, so the default SVD makes a fit on twice the
    views about eight times slower. A first fit only judges views, and LU
    leaves out the same ones tens of times faster.
    """
    w, h = image_size
    K_rough = np.array([[w, 0, w * 0.5], [0, w, h * 0.5], [0, 0, 1]],
                       dtype=np.float64)
    rough = _poses_from_pts(obj_all, img_all, K_rough, np.zeros(5))
    best, failure = None, None
    for sel in (_spread(len(obj_all), INTRINSICS_FIRST_PASS_FRAMES),
                _pose_diverse_sample(rough, max_frames)):
        if not sel:
            continue
        try:
            _rms, K0, dist0, _r, _t = cv2.calibrateCamera(
                [obj_all[i] for i in sel], [img_all[i] for i in sel],
                image_size, None, None, flags=flags | cv2.CALIB_USE_LU)
        except cv2.error as e:
            failure = e
            continue
        poses = _poses_from_pts(obj_all, img_all, K0, dist0)
        errors = np.array([p[2] if p is not None else np.inf for p in poses])
        # A view PnP cannot pose counts against the fit, as an infinite error.
        median = float(np.median(errors))
        if best is None or median < best[0]:
            best = (median, K0, dist0, poses, errors)
    if best is None:
        # Both fits failed: the camera fails, as any OpenCV fit error does.
        raise (failure if failure is not None
               else cv2.error("no view set gave a first fit"))
    return best[1:]


def intrinsics_warnings(intrinsics, intrinsic_stats) -> dict:
    """camera -> why its lens fit looks wrong, for the report and warnings.

    A fit error above INTRINSICS_RMS_WARN_PX, or, with three or more cameras
    solved, a focal length outside INTRINSICS_FX_WARN_FACTOR of the others'
    median. A rig that mixes lenses can trip the second on a camera whose fit
    is sound, so its text says to check it against that camera's lens.
    """
    out = {}
    fxs = {cam: float(K[0, 0]) for cam, (K, _d) in intrinsics.items()}
    for cam, (K, _d) in intrinsics.items():
        why = []
        rms = float(intrinsic_stats.get(cam, {}).get("rms", 0.0))
        if rms > INTRINSICS_RMS_WARN_PX:
            why.append("its fit error is {:.1f} px".format(rms))
        others = [v for c, v in fxs.items() if c != cam]
        if len(others) >= 2:
            med = float(np.median(others))
            fx = fxs[cam]
            if med > 0 and not (med / INTRINSICS_FX_WARN_FACTOR <= fx
                                <= med * INTRINSICS_FX_WARN_FACTOR):
                why.append("its focal length is {:.0f} px against {:.0f} for "
                           "the other cameras".format(fx, med))
        if why:
            out[cam] = "; ".join(why)
    return out


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


def _pair_order(item):
    """Sort key for ``pairwise`` items: both camera names in numeric order."""
    (a, b), _ = item
    return camera_sort_key(a), camera_sort_key(b)


def connected_components(cam_names, pairwise):
    """Connected components of the solved-pair graph.

    Union-find over every solved pair. Sorted largest first; ties broken by the
    summed shared-frame count of the component's pairs, then by first camera
    in numeric order, so the choice of "largest" is deterministic.
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

    comps = [sorted(g, key=camera_sort_key) for g in groups.values()]
    comps.sort(key=lambda g: (-len(g), -frames_in(g), camera_sort_key(g[0])))
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
    dropped = sorted((c for g in components[1:] for c in g),
                     key=camera_sort_key)

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
        for node in sorted(in_tree, key=camera_sort_key):
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
# Floor: the board lying flat for the take's final second
# ---------------------------------------------------------------------------

#: The calibration's session_metadata.json key that asks for the floor step
#: (the sidebar's "Flat final second" box).
FLOOR_KEY = "floor_from_final_second"
#: How far back from each video's end the floor search reads, in seconds.
FLOOR_TAIL_S = 4.0
#: How long the board must lie still at the end of its last detected
#: stretch, in seconds.
FLOOR_STILL_S = 0.5
#: Median corner movement over that time, in pixels, that still counts as
#: still: a board held in a hand moves more, one lying on the floor less.
FLOOR_STILL_PX = 1.0
#: Corners a camera must see on the lying board to count towards the floor.
FLOOR_MIN_CORNERS = 12
#: How far apart two cameras' views of the lying board may put it before the
#: solve warns. Each camera poses the board on its own, so the spread is a
#: direct measure of how well the cameras' poses agree.
FLOOR_SPREAD_WARN_DEG = 2.0
FLOOR_SPREAD_WARN_MM = 10.0


def floor_requested(calib_dir) -> bool:
    """Whether this calibration was recorded with "Flat final second"."""
    try:
        meta = json.loads((Path(calib_dir) / METADATA_FILENAME).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(meta.get(FLOOR_KEY))


def _corner_detector(board_cfg):
    """A ``detect(gray) -> (corners (M, 2), ids (M,)) | (None, None)``
    callable, the same detection the solve runs on every hinted frame."""
    board, aruco_dict = charuco.make_board(board_cfg)
    detect_markers = charuco.make_marker_detector(aruco_dict)

    def detect(gray):
        mc, mi = detect_markers(gray)
        if mi is None or len(mi) < 2:
            return None, None
        _ret, cc, ci = cv2.aruco.interpolateCornersCharuco(mc, mi, gray, board)
        if ci is None or len(ci) < MIN_CORNERS:
            return None, None
        return cc.reshape(-1, 2), ci.ravel()
    return detect


def still_tail(detections, still_frames):
    """The board's corners averaged over the last ``still_frames`` frames of
    the final detected stretch, or ``(None, why)``.

    ``detections`` is one ``(corners, ids)`` or ``(None, None)`` per frame, in
    order. Returns ``((ids, mean_corners, movement_px), None)`` when the board
    was detected in each of those frames with FLOOR_MIN_CORNERS corners in
    common and moved at most FLOOR_STILL_PX (the median over corners of each
    corner's largest distance from its mean).
    """
    last = max((i for i, (c, _ids) in enumerate(detections) if c is not None),
               default=None)
    if last is None:
        return None, "the board was not seen at the end of the take"
    first = last
    while first > 0 and detections[first - 1][0] is not None:
        first -= 1
    if last - first + 1 < still_frames:
        return None, ("the board was seen for only {} frame(s) at the end "
                      "({} needed)".format(last - first + 1, still_frames))
    run = detections[last - still_frames + 1:last + 1]
    common = set(int(i) for i in run[0][1])
    for _c, ids in run[1:]:
        common &= set(int(i) for i in ids)
    if len(common) < FLOOR_MIN_CORNERS:
        return None, ("only {} corners stayed in view at the end ({} "
                      "needed)".format(len(common), FLOOR_MIN_CORNERS))
    ids = np.array(sorted(common))
    stack = []
    for corners, fids in run:
        pos = {int(i): corners[k] for k, i in enumerate(fids)}
        stack.append(np.stack([pos[i] for i in ids]))
    stack = np.stack(stack)                       # (frames, corners, 2)
    mean = stack.mean(axis=0)
    movement = float(np.median(np.linalg.norm(stack - mean, axis=2).max(axis=0)))
    if movement > FLOOR_STILL_PX:
        return None, ("the board was still moving at the end ({:.1f} px, "
                      "{:g} px allowed)".format(movement, FLOOR_STILL_PX))
    return (ids, mean, movement), None


def read_tail(video, detect, fps):
    """``(detections, still_frames)`` for the last FLOOR_TAIL_S of ``video``.

    Raises ValueError for a video that does not open or reports no frame
    count: the tail is found by seeking from the count, and without one the
    read would decode and search the whole take.
    """
    cap = cv2.VideoCapture(str(video))
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if not cap.isOpened() or total <= 0:
            raise ValueError("could not read its video (opened={}, frames={})"
                             .format(cap.isOpened(), total))
        start = max(0, total - int(round(FLOOR_TAIL_S * fps)))
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        dets = []
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
            dets.append(detect(gray))
    finally:
        cap.release()
    return dets, max(2, int(round(FLOOR_STILL_S * fps)))


def floor_frame(tails, intrinsics, extrinsics, corner_obj):
    """The board's pose in the solve's world frame, from the cameras that saw
    it lying still: ``(R, t, info)`` or ``(None, None, why)``.

    ``tails`` maps camera -> ``(ids, mean_corners, movement)``. The camera
    whose board pose reprojects best sets the frame; ``info`` records every
    camera used and how far the others' boards disagree with it (the angle
    between normals, the distance between origins), which says how well the
    floor was measured. The board's Z axis is turned to point at the cameras.
    """
    poses = {}
    for cam, (ids, mean, _mv) in tails.items():
        if cam not in intrinsics or cam not in extrinsics:
            continue
        K, dist = intrinsics[cam]
        obj = np.stack([corner_obj[int(i)] for i in ids]).astype(np.float64)
        ok, rvec, tvec = cv2.solvePnP(obj, mean.astype(np.float64), K, dist)
        if not ok:
            continue
        proj, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
        err = float(np.linalg.norm(proj.reshape(-1, 2) - mean, axis=1).mean())
        R_bc, _ = cv2.Rodrigues(rvec)
        crv, ctv = extrinsics[cam]
        R_c, _ = cv2.Rodrigues(np.asarray(crv, np.float64))
        t_c = np.asarray(ctv, np.float64).reshape(3, 1)
        R_bw = R_c.T @ R_bc
        t_bw = R_c.T @ (tvec.reshape(3, 1) - t_c)
        if not (np.isfinite(R_bw).all() and np.isfinite(t_bw).all()
                and math.isfinite(err)):
            continue
        poses[cam] = (R_bw, t_bw, err, len(ids))
    if not poses:
        return None, None, "no camera's view of the lying board could be posed"
    best = min(poses, key=lambda c: (poses[c][2], -poses[c][3]))
    R_b, t_b, _err, _n = poses[best]
    # Up is towards the cameras: flip the board frame about its X axis when
    # most camera centres sit below the board plane.
    centres = [(-cv2.Rodrigues(np.asarray(rv, np.float64))[0].T
                @ np.asarray(tv, np.float64).reshape(3, 1))
               for rv, tv in extrinsics.values()]
    heights = [float((R_b.T @ (c - t_b))[2, 0]) for c in centres]
    if np.median(heights) < 0:
        R_b = R_b @ np.diag([1.0, -1.0, -1.0])
    normal = R_b[:, 2]
    spread_deg, spread_mm = 0.0, 0.0
    for cam, (Rw, tw, _e, _n) in poses.items():
        if cam == best:
            continue
        ang = math.degrees(math.acos(min(1.0, abs(float(normal @ Rw[:, 2])))))
        spread_deg = max(spread_deg, ang)
        spread_mm = max(spread_mm, float(np.linalg.norm(tw - t_b)))
    info = {"cameras": sorted(poses, key=camera_sort_key), "from": best,
            "spread_deg": round(spread_deg, 3), "spread_mm": round(spread_mm, 2),
            "movement_px": round(max(float(tails[c][2]) for c in poses), 3)}
    return R_b, t_b, info


def apply_floor(extrinsics, R_b, t_b):
    """Re-express every camera's pose in the floor's frame, where the board
    lying on the floor is Z = 0, Z points up and the origin is its first
    corner: X_old = R_b X_new + t_b, so R' = R R_b and t' = R t_b + t."""
    out = {}
    for cam, (rv, tv) in extrinsics.items():
        R_c, _ = cv2.Rodrigues(np.asarray(rv, np.float64))
        t_c = np.asarray(tv, np.float64).reshape(3, 1)
        R_new = R_c @ R_b
        t_new = R_c @ t_b + t_c
        rvec, _ = cv2.Rodrigues(R_new)
        out[cam] = (rvec.ravel(), t_new.ravel())
    return out


def solve_floor(calib_dir, cam_dirs, active, board_cfg, intrinsics,
                extrinsics, corner_obj, warnings):
    """``(extrinsics, floor)``: the poses re-expressed with Z = 0 on the board
    lying at the end of the take, or unchanged with ``floor["skipped"]``
    saying why.

    RULE: the floor step never fails the solve. REASON: it is an extra on a
    calibration that is already good; an unreadable tail, a board still in a
    hand or an OpenCV error costs the floor, never the cameras' poses.
    """
    try:
        floored, info = _find_floor(calib_dir, cam_dirs, active, board_cfg,
                                    intrinsics, extrinsics, corner_obj)
    except Exception as e:
        floored, info = None, "{}: {}".format(type(e).__name__, e)
    if floored is None:
        warn("floor skipped ({}); calibration.toml keeps the reference "
             "camera's frame".format(info), warnings)
        return extrinsics, {"skipped": info}
    print("  floor from {} ({} camera(s); normals agree within {:.2f} deg, "
          "origins within {:.1f} mm)".format(
              info["from"], len(info["cameras"]), info["spread_deg"],
              info["spread_mm"]))
    if (info["spread_deg"] > FLOOR_SPREAD_WARN_DEG
            or info["spread_mm"] > FLOOR_SPREAD_WARN_MM):
        warn("the cameras disagree about where the lying board is (normals "
             "{:.1f} deg apart, origins {:.0f} mm apart): their poses in "
             "calibration.toml disagree by as much".format(
                 info["spread_deg"], info["spread_mm"]), warnings)
    return floored, info


def _find_floor(calib_dir, cam_dirs, active, board_cfg, intrinsics,
                extrinsics, corner_obj):
    """The body of ``solve_floor``: ``(new extrinsics, info)``, or
    ``(None, why)`` when no camera gives the floor."""
    try:
        fps = float(json.loads((Path(calib_dir) / METADATA_FILENAME)
                               .read_text(encoding="utf-8"))
                    .get("acq_fps") or 30)
    except (OSError, ValueError):
        fps = 30.0
    # A board config the detector cannot build skips the floor here, once,
    # rather than once per camera.
    _corner_detector(board_cfg)

    def tail_of(cam_dir):
        # RULE: one camera's unreadable tail costs that camera only, and each
        # thread builds its own detector. REASON: the other cameras can still
        # give the floor, and no OpenCV detector or board object is shared
        # between threads.
        try:
            video = calibration_video(cam_dir)
            if video is None:
                return None, "no calibration video"
            dets, need = read_tail(video, _corner_detector(board_cfg), fps)
            return still_tail(dets, need)
        except Exception as e:
            return None, "{}: {}".format(type(e).__name__, e)

    dirs = [d for d in cam_dirs if d.name in active]
    # OpenCV releases the GIL to decode and detect, so one thread per camera
    # reads the tails side by side.
    with ThreadPoolExecutor(max_workers=max(1, min(len(dirs),
                                                   os.cpu_count() or 1))) as ex:
        found = dict(zip((d.name for d in dirs), ex.map(tail_of, dirs)))
    tails = {cam: tail for cam, (tail, _why) in found.items() if tail is not None}
    if not tails:
        why_not = sorted(((cam, why) for cam, (tail, why) in found.items()),
                         key=lambda kv: camera_sort_key(kv[0]))
        return None, ("; ".join("{}: {}".format(c, w) for c, w in why_not[:3])
                      or "no camera saw the board lying still at the end")
    R_b, t_b, info = floor_frame(tails, intrinsics, extrinsics, corner_obj)
    if R_b is None:
        return None, info
    floored = apply_floor(extrinsics, R_b, t_b)
    if not all(np.isfinite(rv).all() and np.isfinite(tv).all()
               for rv, tv in floored.values()):
        return None, "the floor frame gave a non-finite camera pose"
    return floored, info


# ---------------------------------------------------------------------------
# Pair quality bands
# ---------------------------------------------------------------------------

#: Stereo RMS bands in pixels, used by the plot, the warnings and the report
#: alike. A pair below RMS_GOOD_PX is good; one from RMS_POOR_PX up is poor
#: and warned about; in between it is worth a look. A well-conditioned
#: ChArUco solve reports about 1 px or less per pair (camera-calibration
#: guidance such as MathWorks' treats a mean reprojection error under 1 px as
#: acceptable), and a pair several pixels off points to a board config that
#: does not match the printed board or intrinsics fitted on a narrow spread
#: of views.
RMS_GOOD_PX = 1.5
RMS_POOR_PX = 3.0
#: Bar colour per band in reprojection_error_histogram.png.
GRADE_COLOURS = {"good": "#4CAF50", "check": "#FF9800", "poor": "#F44336"}


def rms_grade(rms) -> str:
    """``"good"``, ``"check"`` or ``"poor"`` for a stereo RMS in pixels."""
    rms = float(rms)
    if rms < RMS_GOOD_PX:
        return "good"
    if rms < RMS_POOR_PX:
        return "check"
    return "poor"


def pair_quality_warnings(pairwise) -> list[str]:
    """The warning text for each pair that is poor or under-observed.

    A poor pair warns whatever its frame count; a pair below
    MIN_TREE_FRAMES shared frames warns that it cannot be a full-strength
    tree edge.
    """
    out = []
    for (ca, cb), (_, _, rms, n) in sorted(pairwise.items(), key=_pair_order):
        if rms_grade(rms) == "poor":
            out.append("{}-{}: stereo RMS {:.1f} px is poor ({} px or more); "
                       "check that the board config matches the printed "
                       "board, and record the board across more of both "
                       "cameras' views".format(ca, cb, rms, RMS_POOR_PX))
        elif n < MIN_TREE_FRAMES:
            out.append("{}-{}: only {} shared frames ({}+ needed for a "
                       "full-strength tree edge)".format(
                           ca, cb, n, MIN_TREE_FRAMES))
    return out


def _tree_side(tree_edges, a, b):
    """The cameras on ``b``'s side of the tree edge ``a``-``b``."""
    adj = defaultdict(list)
    for x, y in tree_edges:
        if {x, y} != {a, b}:
            adj[x].append(y)
            adj[y].append(x)
    seen, stack = {b}, [b]
    while stack:
        for nb in adj[stack.pop()]:
            if nb not in seen:
                seen.add(nb)
                stack.append(nb)
    return seen


def poorly_placed(active, tree_edges, pairwise, ref):
    """camera -> ``{"link": "camA-camB", "rms": px}`` for every camera whose
    position comes through a poor tree link.

    Each camera's pose is chained along the tree, so a poor link misplaces
    only the cameras on one side of it: the smaller side, whose position
    relative to the rest runs through that link. On a tie the side without
    the reference camera is the one named, because the reference is the
    origin. A camera behind several poor links keeps the worst.
    """
    out = {}
    cams = set(active)
    for a, b in tree_edges:
        entry = pair_entry(pairwise, a, b)
        if entry is None or rms_grade(entry[2]) != "poor":
            continue
        side_b = _tree_side(tree_edges, a, b) & cams
        side_a = cams - side_b
        if len(side_b) != len(side_a):
            part = side_b if len(side_b) < len(side_a) else side_a
        else:
            part = side_a if ref in side_b else side_b
        link = "{}-{}".format(*sorted((a, b), key=camera_sort_key))
        for cam in part:
            if cam not in out or entry[2] > out[cam]["rms"]:
                out[cam] = {"link": link, "rms": float(entry[2])}
    return dict(sorted(out.items(), key=lambda kv: camera_sort_key(kv[0])))


def placement_warnings(placed, codetections, k=2):
    """One warning per poorly placed camera, naming the cameras it shares
    the most views with, which are the ones to show the board with it."""
    out = []
    for cam, row in placed.items():
        partners = sorted(
            ((n, b if a == cam else a) for (a, b), n in codetections.items()
             if cam in (a, b) and (b if a == cam else a) not in placed),
            key=lambda t: (-t[0], camera_sort_key(t[1])))
        names = [c for _n, c in partners[:k]]
        along = (" while {} also see it".format(" or ".join(names))
                 if names else "")
        out.append(
            "{} is placed through a poor link ({}, {:.1f} px), so its "
            "position in calibration.toml is unreliable; the other cameras' "
            "positions do not depend on it. Recalibrate with the board close "
            "to {}, tilted and near the edges of its view{}".format(
                cam, row["link"], row["rms"], cam, along))
    return out


def median_pair_rms(pairwise, cams):
    """Median stereo RMS over the pairs whose cameras are both in ``cams``,
    or None when there is no such pair."""
    cams = set(cams)
    values = [float(rms) for (a, b), (_, _, rms, _) in pairwise.items()
              if a in cams and b in cams]
    return float(np.median(values)) if values else None


# ---------------------------------------------------------------------------
# Reprojection error histogram
# ---------------------------------------------------------------------------

def save_reprojection_histogram(path, pair_rms):
    """Save a pairwise stereo RMS bar chart as PNG, coloured by ``rms_grade``."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("  matplotlib not available, skipping histogram")
        return

    labels = sorted(pair_rms.keys(), key=lambda k: [
        camera_sort_key(c) for c in k.split("-")])
    values = [pair_rms[k] for k in labels]
    if not values:
        return

    fig, ax = plt.subplots(figsize=(max(6, len(labels) * 0.8), 4))
    colors = [GRADE_COLOURS[rms_grade(v)] for v in values]
    ax.bar(range(len(labels)), values, color=colors)
    ax.set_xticks(range(len(labels)))
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Stereo RMS (px)")
    ax.set_title("Pairwise calibration quality")
    ax.axhline(RMS_GOOD_PX, color="gray", linestyle="--", alpha=0.5,
               label="good (< {:g} px)".format(RMS_GOOD_PX))
    ax.axhline(RMS_POOR_PX, color="gray", linestyle=":", alpha=0.7,
               label="poor ({:g} px or more)".format(RMS_POOR_PX))
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
                 pairing=None, codetections=None, camera_serials=None,
                 placed=None):
    """Assemble every quality figure of a solve into one plain dict.

    Written verbatim as ``calibration_report.json`` and rendered into the
    toml's ``[metadata]``. ``dropped`` maps reason -> camera names for every
    camera that had (or should have had) a video but is not in the toml;
    ``partial`` is true when any of them is non-empty, so a consumer can tell a
    full solve from one that quietly lost cameras.

    Each pair carries its ``grade`` (``rms_grade``), and the report carries
    the bands (``rms_bands_px``) and the median over the solved cameras'
    pairs (``pair_rms_median``). ``pairing`` (``"block_id"`` or
    ``"frame_index"``), ``codetections`` (pair -> views shared before the
    pose-diverse cap) and ``camera_serials`` (name -> serial during the
    calibration) are recorded when given. ``placed`` becomes
    ``poorly_placed``: camera -> ``{"link", "rms"}`` for every camera whose
    position runs through a poor tree link, or whose lens fit is suspect
    (``"link": "lens fit"``).
    """
    tree_rows = []
    for a, b in tree:
        entry = pair_entry(pairwise, a, b)
        tree_rows.append({"from": a, "to": b,
                          "rms": float(entry[2]), "frames": int(entry[3])})
    pairs = {}
    for (a, b), (_, _, rms, n) in sorted(pairwise.items(), key=_pair_order):
        row = {"rms": float(rms), "frames": int(n), "grade": rms_grade(rms)}
        if codetections and (a, b) in codetections:
            row["codetections"] = int(codetections[(a, b)])
        pairs["{}-{}".format(a, b)] = row
    dropped = {k: sorted(v, key=camera_sort_key) for k, v in dropped.items()}
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
        "rms_bands_px": {"good_below": RMS_GOOD_PX, "poor_from": RMS_POOR_PX},
        "poorly_placed": dict(placed or {}),
        "warnings": list(warnings),
    }
    median = median_pair_rms(pairwise, active)
    if median is not None:
        report["pair_rms_median"] = median
    if pairing is not None:
        report["pairing"] = pairing
    if camera_serials is not None:
        report["camera_serials"] = {
            cam: camera_serials[cam]
            for cam in sorted(camera_serials, key=camera_sort_key)}
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

def camera_section_names(n):
    """The toml section key for each of ``n`` cameras, in camera order.

    RULE: the index is zero-padded to the width of the largest one
    (``cam_0``..``cam_9`` up to ten cameras, ``cam_00``..``cam_10`` from
    eleven). REASON: aniposelib sorts the section keys as strings, so
    ``cam_10`` would otherwise land between ``cam_1`` and ``cam_2``. Rigs of
    ten or fewer cameras keep the unpadded keys.
    """
    width = len(str(max(int(n) - 1, 0)))
    return ["cam_{:0{}d}".format(i, width) for i in range(int(n))]


def write_calibration_toml(path, cam_names, intrinsics, extrinsics, sizes,
                           meta=None):
    """Write calibration.toml, one section per camera in ``cam_names`` order.

    Each section carries the camera's ``name``, which is what consumers
    should match cameras by (``camera_section_names`` has the key rule).
    """
    lines = []
    for key, cam in zip(camera_section_names(len(cam_names)), cam_names):
        K, dist = intrinsics[cam]
        rvec, tvec = extrinsics[cam]
        w, h = sizes[cam]
        d = dist.ravel()
        if len(d) < 5:
            d = np.concatenate([d, np.zeros(5 - len(d))])
        lines.append("[{}]".format(key))
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

    active = sorted((c for c in all_dets if len(all_dets[c][0]) >= 5),
                    key=camera_sort_key)
    dropped["few_detections"] = sorted(set(all_dets) - set(active),
                                       key=camera_sort_key)
    if dropped["few_detections"]:
        warn("dropping {} (<5 detections)".format(
            ", ".join(dropped["few_detections"])), warnings)
    if len(active) < 2:
        fail("NO_DETECTIONS", "fewer than 2 cameras with detections")

    # --- Intrinsics (parallel — cv2 releases the GIL) ---
    print("\nIntrinsics...")

    job_stats = {}

    def _intrinsic_job(cam):
        # RULE: one camera's OpenCV failure is that camera's failure. REASON:
        # an exception here would end the solve for every camera, when the
        # others can still be calibrated and the report can name this one.
        _keys, corners, ids = all_dets[cam]
        st = job_stats.setdefault(cam, {})
        try:
            return cam, calibrate_intrinsics(corners, ids, corner_obj,
                                             all_sizes[cam], stats=st)
        except cv2.error as e:
            st["error"] = str(e).strip().splitlines()[-1]
            return cam, None

    intrinsics = {}
    intrinsic_stats = {}
    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        for cam, result in pool.map(_intrinsic_job, active):
            st = job_stats.get(cam, {})
            if st.get("degenerate"):
                print("  {}: skipped {} view(s) whose corners fit no "
                      "homography (a single row or column of the board)"
                      .format(cam, st["degenerate"]))
            if result is None:
                dropped["failed_intrinsics"].append(cam)
                if st.get("error"):
                    warn("{}: intrinsics FAILED: OpenCV could not fit this "
                         "camera's views ({})".format(cam, st["error"]),
                         warnings)
                else:
                    warn("{}: intrinsics FAILED ({} detection frames, {} "
                         "needed with >= {} corners and a fitting "
                         "homography)".format(
                             cam, len(all_dets[cam][0]),
                             INTRINSICS_MIN_FRAMES, MIN_CORNERS), warnings)
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
    for cam in active:
        n_out = job_stats.get(cam, {}).get("outliers")
        if n_out:
            print("  {}: left out {} view(s) that did not fit its first-pass "
                  "lens fit".format(cam, n_out))
    lens = intrinsics_warnings({c: intrinsics[c] for c in active},
                               intrinsic_stats)
    lens_warnings = [
        "{}: lens fit looks wrong ({}), so its intrinsics and every pair with "
        "it are unreliable. If this camera has the same lens as the others, "
        "recalibrate with the board close to it, tilted and in the corners "
        "of its view".format(cam, why) for cam, why in lens.items()]
    for w in lens_warnings:
        warn(w, warnings)

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

    # --- Floor (only when the take was recorded with "Flat final second") ---
    floor = {"requested": floor_requested(calib_dir)}
    if floor["requested"]:
        print("\nFloor (the board lying flat for the final second)...")
        extrinsics, found = solve_floor(calib_dir, cam_dirs, active, board_cfg,
                                        intrinsics, extrinsics, corner_obj,
                                        warnings)
        floor.update(found)

    # --- Quality summary + histogram ---
    print("\nPairwise quality (good < {:g} px, poor from {:g} px):".format(
        RMS_GOOD_PX, RMS_POOR_PX))
    pair_rms = {}
    for (ca, cb), (_, _, rms, n) in sorted(pairwise.items(), key=_pair_order):
        pair_rms["{}-{}".format(ca, cb)] = rms
        print("  {}-{}: RMS={:.2f}px  {}  ({} frames)".format(
            ca, cb, rms, rms_grade(rms), n))
    median = median_pair_rms(pairwise, active)
    if median is not None:
        print("  Median pair RMS over the solved cameras: {:.2f}px  {}".format(
            median, rms_grade(median)))
    for w in pair_quality_warnings(pairwise):
        warn(w, warnings)
    # RULE: a camera placed through a poor link is named first, as unreliable.
    # REASON: the other warnings say a pair is poor, which reads the same
    # whether the pair fixes one camera's position or is merely one of many.
    # A solve can then report every camera "solved" with one of them placed
    # several pixels wrong.
    placed = poorly_placed(active, tree, pairwise, ref)
    placement = placement_warnings(placed, codetections)
    for w in placement:
        print("  WARNING: {}".format(w))
    # The lens and placement warnings lead, in that order: they name the
    # cameras whose numbers in calibration.toml are wrong.
    for w in lens_warnings:
        warnings.remove(w)
    warnings[:0] = lens_warnings + placement
    # A camera the graph step dropped is already in the report's dropped
    # list, and poorly_placed names only cameras in calibration.toml.
    for cam in lens:
        if cam in active:
            placed.setdefault(cam, {"link": "lens fit", "rms": float(
                intrinsic_stats[cam]["rms"])})
    placed = dict(sorted(placed.items(), key=lambda kv: camera_sort_key(kv[0])))

    for cam in active:
        keys = all_dets[cam][0]
        if len(keys) < 30:
            warn("{}: only {} detection frames (30+ recommended)".format(
                cam, len(keys)), warnings)

    # --- Camera identity ---
    serials = camera_serial_map(calib_dir)
    serial_mismatch = []
    if serials:
        serial_mismatch = check_serial_maps(args.session_dir, calib_dir,
                                            serials, active, warnings)
    else:
        notice("calibration/{} records no camera serials, so the report "
               "cannot say which physical camera each name was".format(
                   METADATA_FILENAME))

    hist_path = calib_dir / "reprojection_error_histogram.png"
    save_reprojection_histogram(hist_path, pair_rms)

    # --- Write ---
    report = build_report(board_cfg, ref, active, tree, pairwise, intrinsic_stats,
                          components, dropped, hints_used=bool(hints),
                          skip=args.skip, warnings=warnings, pairing=pairing,
                          codetections=codetections, camera_serials=serials,
                          placed=placed)
    report["floor"] = floor
    # The acquisitions of this session that recorded other cameras under the
    # names solved here: calibration_worker.recalibration_reasons reads it.
    report["serial_mismatch"] = serial_mismatch
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
    if placed:
        print("  Unreliable: {}".format(", ".join(
            "{} ({}, {:.1f} px)".format(c, r["link"], r["rms"])
            for c, r in placed.items())))
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
