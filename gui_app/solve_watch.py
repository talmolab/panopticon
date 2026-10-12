"""A once-a-second log line about the computer while a calibration solve runs.

The solve keeps every CPU busy for minutes, and the window pauses the preview
for it. Each line records what the computer was doing that second (how busy
the CPUs were, the busiest CPU's DPC and interrupt load, free RAM) and, if the
preview runs, what each camera delivered. A camera lost during a solve can
then be put on a timeline against the load. No Qt here: the window owns the
timer and prints the line.
"""
from __future__ import annotations

import time

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a project dependency
    psutil = None


def _cpu_times():
    return psutil.cpu_times(percpu=True) if psutil is not None else None


def _free_bytes():
    return psutil.virtual_memory().available if psutil is not None else None


def _total(t) -> float:
    """A CPU's total time. On Windows `system` already holds the DPC and
    interrupt time psutil reports beside it, so those two are not added."""
    if hasattr(t, "dpc"):
        return t.user + t.system + t.idle
    return float(sum(t))


class SolveWatch:
    """Builds one line per call to `line()`, each covering the time since the
    previous call. The sources are injectable for tests: `cpu_times` returns
    per-CPU named tuples with `idle` and, on Windows, `dpc` and `interrupt`
    (psutil.cpu_times(percpu=True)); `free_bytes` returns available RAM."""

    def __init__(self, clock=time.monotonic, cpu_times=_cpu_times,
                 free_bytes=_free_bytes):
        self._clock = clock
        self._cpu_times = cpu_times
        self._free_bytes = free_bytes
        self._t0 = clock()
        self._last_cpu = self._read_cpu()
        self._last_failed = None

    def _read_cpu(self):
        try:
            return self._cpu_times()
        except Exception:
            return None

    def _cpu_text(self) -> str | None:
        now = self._read_cpu()
        last, self._last_cpu = self._last_cpu, now
        if not now or not last or len(now) != len(last):
            return None
        totals, idles = [], []
        for a, b in zip(last, now):
            totals.append(max(1e-9, _total(b) - _total(a)))
            idles.append(b.idle - a.idle)
        busy = 100.0 * (1.0 - sum(idles) / sum(totals))
        text = f"CPU {busy:.0f}% busy"
        for field, label in (("dpc", "DPC"), ("interrupt", "interrupts")):
            if not hasattr(now[0], field):
                continue
            shares = [(getattr(b, field) - getattr(a, field)) / t
                      for a, b, t in zip(last, now, totals)]
            k = max(range(len(shares)), key=shares.__getitem__)
            text += f", {label} {100.0 * shares[k]:.0f}% on CPU{k}"
        return text

    def _free_text(self) -> str | None:
        try:
            free = self._free_bytes()
        except Exception:
            return None
        return None if free is None else f"{free / 2**30:.1f} GiB free"

    def line(self, fps, failed) -> str:
        """The line for the second just ended. `fps` is each camera's preview
        frame rate (CameraManager.current_fps, which reads 0 for a silent
        camera), or None while the preview is paused, and `failed` each
        camera's failed-grab count (CameraManager.failed_grab_counts)."""
        parts = [f"{self._clock() - self._t0:.0f} s"]
        for text in (self._cpu_text(), self._free_text()):
            if text:
                parts.append(text)
        if fps is None:
            parts.append("preview paused")
            return "[solve-watch] " + "; ".join(parts)
        parts.append("preview fps " + " ".join(f"{f:.0f}" for f in fps))
        failed = [int(n) for n in failed]
        last, self._last_failed = self._last_failed, failed
        if last is not None and len(last) == len(failed):
            new = [(i, n - m) for i, (m, n) in enumerate(zip(last, failed))
                   if n > m]
            if new:
                parts.append("failed grabs " + ", ".join(
                    f"cam{i + 1} +{d}" for i, d in new))
        return "[solve-watch] " + "; ".join(parts)
