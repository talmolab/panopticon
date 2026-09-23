"""What a finished acquisition directory says about itself.

The GUI writes small files beside an acquisition's videos, and the offline
tools (``0_encode.py``, ``2_align.py``, ``3_stim_trace.py``) and the GUI's own
post-processing read them back through this module, so every reader takes each
value from the same file in the same order.

No Qt and no numpy, so every CLI can import it.
"""
import json
import os
from pathlib import Path

from gui_app.session_config import METADATA_FILENAME

#: Written into a camera's directory when that camera was retired during the
#: acquisition. 2_align.py leaves such a camera out of the alignment by
#: default, so a camera that stopped early cannot cut the cameras that kept
#: recording down to its length.
RETIRED_NAME = "RETIRED.json"

#: The session_metadata.json keys acquisition_params() reads as-is, from the
#: acquisition's own file first and the session-level copy second.
_PLAIN_KEYS = ("quality", "encoder", "resolution", "date", "session_id")
_SESSION_KEYS = ("date", "session_id")


def camera_sort_key(name: str) -> tuple:
    """Sort key that orders camera names by number: cam2 before cam10.

    A string sort puts cam10 between cam1 and cam2, so on a rig with ten or
    more cameras every per-camera list built from it (the alignment index
    rows, the stim-trace columns, the calibration sections) stops following
    the camera numbering. Names with no number after ``cam`` sort after the
    numbered ones, by name.
    """
    tail = name[3:] if name.startswith("cam") else name
    if tail.isascii() and tail.isdigit():
        return (0, int(tail), name)
    return (1, 0, name)


def write_retired(cam_dir, reason: str, **extra) -> Path:
    """Record in ``cam_dir/RETIRED.json`` that the camera was retired.

    The file holds a JSON object with ``reason`` plus the ``extra`` keys
    (any further JSON-serialisable detail, for example the last block ID the
    camera recorded); readers need only ``reason``. It is written through a
    temporary file, so a reader never sees half a record.
    """
    path = Path(cam_dir) / RETIRED_NAME
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({**extra, "reason": str(reason)}, indent=2),
                   encoding="utf-8")
    os.replace(tmp, path)
    return path


def retired_cameras(rec_dir) -> dict:
    """camera name -> retirement reason, for each cam*/ holding RETIRED.json.

    In camera-number order. A record that cannot be read still marks its
    camera as retired, with the read error as the reason: leaving a camera
    out of an alignment never touches its files, so that is the reading that
    cannot lose data.
    """
    rec_dir = Path(rec_dir)
    if not rec_dir.is_dir():
        return {}
    out = {}
    cams = sorted((d for d in rec_dir.iterdir()
                   if d.is_dir() and d.name.startswith("cam")
                   and (d / RETIRED_NAME).exists()),
                  key=lambda d: camera_sort_key(d.name))
    for d in cams:
        problems: list[str] = []
        data = _read_json(d / RETIRED_NAME, problems)
        if data is not None and data.get("reason"):
            out[d.name] = str(data["reason"])
        elif problems:
            out[d.name] = f"its {RETIRED_NAME} is unreadable ({problems[0]})"
        else:
            out[d.name] = f"{RETIRED_NAME} gives no reason"
    return out


def _read_json(path: Path, warnings: list):
    """The dict in ``path``, or None when it is absent or unusable.

    An unreadable or malformed file is reported in ``warnings`` and treated as
    absent, so a damaged metadata file makes the caller fall back with a
    message instead of stopping.
    """
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        warnings.append(f"could not read {path}: {e}")
        return None
    if not isinstance(data, dict):
        warnings.append(f"{path} does not hold a JSON object; ignored")
        return None
    return data


def _positive_number(value):
    """``value`` as a float when it is a positive number, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


def rate_key(rec_dir) -> str:
    """The session-level key that holds this acquisition type's trigger rate."""
    return ("calibration_frame_rate" if Path(rec_dir).name == "calibration"
            else "frame_rate")


def acquisition_params(rec_dir) -> dict:
    """The recording parameters an acquisition directory records.

    Returns a dict with ``acq_fps`` (float), ``quality``, ``encoder``,
    ``resolution``, ``date`` and ``session_id``, each None when no file
    records it, plus ``sources`` (key -> ``"<file>:<json key>"``) and
    ``warnings`` (list of str).

    The acquisition's own ``session_metadata.json`` (beside its cam*/
    directories) is read first. The session-level copy one directory up is
    read only for keys the acquisition's file lacks, and every value taken
    from it, except the date and session id, adds a warning. The
    session-level copy is written once, by the session's first acquisition,
    and never updated, so it can describe a different acquisition type or an
    earlier profile. For the same reason its ``acq_fps`` is never used: the
    rate comes from ``frame_rate`` or ``calibration_frame_rate``, chosen by
    the directory name.
    """
    rec_dir = Path(rec_dir)
    warnings: list[str] = []
    out = dict(acq_fps=None, sources={}, warnings=warnings)
    out.update({k: None for k in _PLAIN_KEYS})

    own_path = rec_dir / METADATA_FILENAME
    own = _read_json(own_path, warnings)
    if own is not None:
        for key in ("acq_fps", rate_key(rec_dir)):
            fps = _positive_number(own.get(key))
            if fps is not None:
                out["acq_fps"] = fps
                out["sources"]["acq_fps"] = f"{own_path.name}:{key}"
                break
        for key in _PLAIN_KEYS:
            if own.get(key) is not None:
                out[key] = own[key]
                out["sources"][key] = f"{own_path.name}:{key}"

    missing = [k for k in ("acq_fps", *_PLAIN_KEYS) if out[k] is None]
    if not missing:
        return out
    parent_path = rec_dir.parent / METADATA_FILENAME
    parent = _read_json(parent_path, warnings)
    if parent is None:
        return out
    where = (f"{rec_dir.name}/{METADATA_FILENAME} is missing" if own is None
             else f"{rec_dir.name}/{METADATA_FILENAME} does not record it")
    label = f"../{METADATA_FILENAME}"
    if out["acq_fps"] is None:
        key = rate_key(rec_dir)
        fps = _positive_number(parent.get(key))
        if fps is not None:
            out["acq_fps"] = fps
            out["sources"]["acq_fps"] = f"{label}:{key}"
            warnings.append(
                f"frame rate {fps:g} taken from the session-level "
                f"{METADATA_FILENAME} ({key}) because {where}. That file "
                f"describes the session's first acquisition; pass --fps if "
                f"this one ran at another rate.")
    for key in _PLAIN_KEYS:
        if out[key] is None and parent.get(key) is not None:
            out[key] = parent[key]
            out["sources"][key] = f"{label}:{key}"
            # The date and session id name the session directory itself, so
            # the session-level copy cannot disagree about them.
            if key not in _SESSION_KEYS:
                warnings.append(f"{key} taken from the session-level "
                                f"{METADATA_FILENAME} because {where}.")
    return out
