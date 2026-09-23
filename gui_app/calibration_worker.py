"""Background worker that runs the ChArUco calibration solve as a subprocess.

The solve (``1_calibrate.py``) runs in the PROJECT environment, so it is
launched with ``sys.executable``, the interpreter this GUI already runs in,
with ``cwd`` set to the repository root. Going through ``uv run`` would make
the environment depend on the caller's cwd (the ``panopticon`` console script
and ``conda run`` both start elsewhere) and put an intermediate process
between the GUI and the solve, which a timeout kill would orphan.

Progress lines are streamed as they arrive (the script line-buffers its
stdout) and forwarded as ``status``; on timeout the whole process tree is
killed with psutil, because the solve's ProcessPoolExecutor workers hold the
mp4s open and a killed parent does not take them along on Windows.

Failure classification reads the script's ``ERROR_CODE=<code>`` stderr line
first; the text heuristics below are only the fallback for an unexpected
traceback, and they are deliberately narrow (``LinAlgError``, not any path
containing ``linalg``; no tuple-unpack matching) so an unrelated exception is
never reported as "no board detections".

Signals. ``finished_solve(bool, str)`` carries the result under a name that
does not shadow ``QThread.finished``, and it is the one the application
connects to; connect new code there. ``finished(bool, str)`` carries the same
payload and shadows ``QThread.finished``, so anything that relies on the
built-in signal's no-argument form gets this one instead; the application
does not connect to it. ``report_ready(dict)`` fires before either with the parsed
``calibration_report.json`` (empty dict when the script produced none); the
same dict is left in ``self.report``. On success the message is a summary
built from the report (cameras dropped and why, warnings), so the dialog names
the bad camera or pair instead of showing a generic sentence.
"""
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

from PyQt5.QtCore import QThread, pyqtSignal

#: Wall-clock cap on a solve. Marker detection dominates and scales with video
#: length x camera count, so this is headroom to tell "slow" from "hung", not
#: an estimate.
SOLVE_TIMEOUT_S = 1800

#: Progress line prefixes worth forwarding to the status bar.
_STATUS_PREFIXES = ("Detecting", "Intrinsics", "Pairwise", "Camera graph",
                    "Calibration complete", "  Using co-detection",
                    "  cam")

#: Human text for the script's ERROR_CODE values.
ERROR_MESSAGES = {
    "NO_CALIB_DIR": "Calibration directory not found.\n\nThe session folder "
                    "has no calibration/ subfolder. Record a calibration first.",
    "NO_VIDEOS": "No readable calibration videos found.\n\nEvery camera folder "
                 "under calibration/ needs a *calibration*.mp4. If the encode "
                 "is still running, wait for it to finish.",
    "BAD_BOARD_CONFIG": "The board config cannot be used with this OpenCV "
                        "build.\n\nCheck marker_bits / dict_size (the "
                        "dictionary must exist) and board_legacy.",
    "NO_DETECTIONS": "No ChArUco board detections found.\n\nMake sure the "
                     "ChArUco board was clearly visible to the cameras during "
                     "the calibration recording, and that the board parameters "
                     "in your board config YAML match the physical board.",
    "NO_INTRINSICS": "Fewer than two cameras produced valid intrinsics.\n\n"
                     "Each camera needs at least 20 frames with 6 or more "
                     "charuco corners. Record longer with the board filling "
                     "more of each camera's view.",
    "NO_PAIRS": "No camera pair saw the board together.\n\nStereo calibration "
                "needs frames where two cameras see the board at the same "
                "time. Record with the board visible to several cameras at "
                "once.",
    "DISCONNECTED": "The coverage graph is disconnected.\n\nFewer than two "
                    "cameras share enough board sightings to be solved "
                    "together. Record with the board visible to neighbouring "
                    "cameras at the same time.",
}


class CalibrationWorker(QThread):
    status = pyqtSignal(str)
    finished = pyqtSignal(bool, str)
    finished_solve = pyqtSignal(bool, str)
    report_ready = pyqtSignal(dict)

    def __init__(self, session_dir: Path, script_path: Path, board_config: str = "",
                 timeout_s: float = SOLVE_TIMEOUT_S):
        super().__init__()
        self._session_dir = Path(session_dir)
        self._script_path = Path(script_path)
        self._board_config = board_config
        self._timeout_s = timeout_s
        self.report: dict = {}

    # -- lifecycle -----------------------------------------------------------

    def _done(self, ok: bool, msg: str):
        self.report_ready.emit(self.report)
        self.finished_solve.emit(ok, msg)
        self.finished.emit(ok, msg)

    def run(self):
        if not self._script_path.exists():
            self._done(False, f"Script not found: {self._script_path}")
            return

        self.status.emit("Running calibration...")
        cmd = [sys.executable, str(self._script_path), str(self._session_dir)]
        if self._board_config and Path(self._board_config).exists():
            cmd.extend(["--board-config", self._board_config])

        # The script prints non-ASCII; force UTF-8 on both ends so a pipe with
        # a legacy code page cannot raise UnicodeEncodeError inside the solve.
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(self._script_path.parent),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=env,
            )
        except Exception as e:
            self._done(False, f"Could not start the solve: {e}")
            return

        def _drain_stderr():
            for line in proc.stderr:
                stderr_lines.append(line.rstrip("\n"))

        err_thread = threading.Thread(target=_drain_stderr, daemon=True)
        err_thread.start()

        # Reading stdout here streams progress; a separate timer thread kills
        # the tree if the deadline passes while a read is blocked.
        timed_out = threading.Event()

        def _watchdog():
            try:
                proc.wait(self._timeout_s)
            except subprocess.TimeoutExpired:
                timed_out.set()
                _kill_tree(proc.pid)

        wd = threading.Thread(target=_watchdog, daemon=True)
        wd.start()

        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                stdout_lines.append(line)
                print(line, flush=True)
                if line.startswith(_STATUS_PREFIXES):
                    self.status.emit(line.strip()[:120])
            proc.wait()
            err_thread.join(timeout=5)
        except Exception as e:
            # Kill only a child that is still alive: the pid of an exited child
            # is free for reuse and psutil would walk an unrelated process tree.
            if proc.poll() is None:
                _kill_tree(proc.pid)
            self._done(False, str(e))
            return

        stdout = "\n".join(stdout_lines).strip()
        stderr = "\n".join(stderr_lines).strip()

        if timed_out.is_set():
            self._done(False, f"Calibration timed out ({int(self._timeout_s // 60)} min)")
            return

        self.report = _load_report(stdout, self._session_dir)
        if proc.returncode == 0:
            self.status.emit("Calibration complete")
            self._done(True, summarize_report(self.report) if self.report else stderr)
        else:
            self._done(False, _parse_calibration_error(stdout, stderr, proc.returncode))


def _kill_tree(pid: int):
    """Kill a process and every descendant. The solve's pool workers are
    separate processes holding the mp4s open, so killing the parent alone
    leaves them running for minutes."""
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            root = psutil.Process(pid)
            procs = root.children(recursive=True) + [root]
        except psutil.Error:
            return
        for p in procs:
            try:
                p.kill()
            except psutil.Error:
                pass
        psutil.wait_procs(procs, timeout=5)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _load_report(stdout: str, session_dir: Path) -> dict:
    """Find and parse calibration_report.json: the REPORT_PATH= line first,
    then the conventional location. {} when neither exists."""
    candidates = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("REPORT_PATH="):
            candidates.append(Path(line[len("REPORT_PATH="):]))
    candidates.append(Path(session_dir) / "calibration" / "calibration_report.json")
    for path in candidates:
        try:
            if path.exists():
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            continue
    return {}


def summarize_report(report: dict) -> str:
    """The operator-facing text for a successful solve, or "" when there is
    nothing to say (all cameras solved, no warnings)."""
    if not report:
        return ""
    lines = []
    dropped = report.get("dropped") or {}
    n_dropped = sum(len(v) for v in dropped.values())
    cams = report.get("cameras") or []
    if report.get("partial") or n_dropped:
        lines.append("PARTIAL: solved {} of {} cameras.".format(
            len(cams), len(cams) + n_dropped))
        reasons = {"no_video": "no video", "unreadable": "unreadable video",
                   "few_detections": "too few detections",
                   "failed_intrinsics": "intrinsics failed",
                   "isolated": "not connected to the main group"}
        for key, names in dropped.items():
            if names:
                lines.append("  {}: {}".format(
                    ", ".join(names), reasons.get(key, key)))
        comps = report.get("components") or []
        if len(comps) > 1:
            lines.append("  groups: " + "  ".join(
                "{" + ",".join(c) + "}" for c in comps))
    warnings = report.get("warnings") or []
    if warnings:
        if lines:
            lines.append("")
        lines.append("Warnings:")
        lines.extend("  - {}".format(w) for w in warnings)
    return "\n".join(lines)


def _parse_calibration_error(stdout: str, stderr: str, returncode: int) -> str:
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("ERROR_CODE="):
            code = line[len("ERROR_CODE="):].strip()
            detail = ""
            for l2 in stderr.splitlines():
                if l2.strip().startswith("ERROR:"):
                    detail = l2.strip()
            text = ERROR_MESSAGES.get(code, f"Calibration failed ({code}).")
            return text + (f"\n\n{detail}" if detail else "")

    combined = f"{stdout}\n{stderr}"

    if "No module named" in combined or "ModuleNotFoundError" in combined:
        module = ""
        for line in stderr.split("\n"):
            if "no module named" in line.lower():
                module = line.strip()
                break
        return (f"Missing dependency:\n{module}\n\n"
                f"The solve runs in the project environment, so this means the "
                f"environment is incomplete rather than stale. Run `uv sync` in "
                f"the repository folder to install everything pyproject.toml "
                f"asks for, then try again.")

    if "LinAlgError" in combined or "singular matrix" in combined.lower():
        return (
            "Calibration solve failed (singular matrix).\n\n"
            "This usually means one or more cameras had too few board detections, "
            "or the board was only visible from a single angle. Try recording "
            "calibration videos with more board orientations."
        )

    # Fall back to last meaningful lines from stderr
    error_lines = [
        line for line in stderr.split("\n")
        if line.strip() and not line.startswith("  ")
    ]
    if error_lines:
        tail = "\n".join(error_lines[-5:])
        return f"Calibration failed (exit code {returncode}):\n\n{tail}"

    return f"Calibration failed (exit code {returncode})"
