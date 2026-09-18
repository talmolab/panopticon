"""Apply a RigProfile to a CameraManager: one configuration path for every
entry point.

The GUI and the headless probes must configure the cameras identically, or a
probe's numbers say nothing about the GUI: pool depth, thread placement and
the expected camera count each changed what the rig measured, and each was
once set in one entry point and forgotten in the other. Both call the two
functions here, so a profile field that affects capture is applied in exactly
one place.

This module imports no camera SDK and no Qt; the manager is duck-typed so the
helpers also work against a stub in tests.
"""
import functools
import inspect

from gui_app.session_config import RigProfile

#: Attributes on the manager that are copied from same-named profile fields
#: before open_all. They are read at start_acquisition, so setting them in the
#: open path also covers a live profile switch, which re-enters open.
MANAGER_FLAGS = ("pin_capture_threads", "encoder_pcores", "pin_encoder_threads")

#: Profile field -> open_all keyword. Only keywords the installed manager's
#: signature accepts are passed (see open_kwargs), so a profile field whose
#: consumer has not landed is declared without breaking open.
_OPEN_KWARG_FIELDS = {
    "pfs_path": "pfs_path",
    "gige_driver": "gige_driver",
    "trigger_rate_limit": "trigger_rate_limit",
    "n_cameras": "expect_cameras",
    "max_num_buffer": "max_num_buffer",
    "camera_serials": "only_serials",
    "camera_backend": "backend",
    "gev_bandwidth_reserve_pct": "gev_bandwidth_reserve_pct",
    "gev_bandwidth_reserve_accum": "gev_bandwidth_reserve_accum",
}


def apply_profile_to_manager(mgr, profile: RigProfile,
                             log=functools.partial(print, flush=True)) -> list | None:
    """Copy the thread-placement flags onto ``mgr`` and set the capture core
    pool from ``profile.capture_core_exclude``.

    Returns the core pool that was set, or None when affinity is unavailable
    (non-Windows, non-hybrid CPU, or an import failure). Affinity is an
    optimisation and never a reason not to open cameras, so its failure is
    logged through ``log`` rather than raised.

    ``log`` is any callable taking one string (``logging.getLogger().info``,
    a list's ``append``) and is only ever called as ``log(message)``; the
    default prints unbuffered so the line reaches a redirected log file
    before the cameras open.
    """
    for name in MANAGER_FLAGS:
        setattr(mgr, name, bool(getattr(profile, name, False)))
    try:
        from gui_app import cpu_affinity
        pool = cpu_affinity.capture_core_pool(profile.capture_core_exclude)
        cpu_affinity.set_core_order(pool)
        log(f"[rig] capture core pool {pool} "
            f"(excluding {profile.capture_core_exclude})")
        return pool
    except Exception as e:
        log(f"[rig] could not set capture core pool: {e}")
        return None


def open_kwargs(mgr, profile: RigProfile) -> dict:
    """The ``open_all`` keyword arguments for ``profile`` that ``mgr`` accepts.

    The candidate set is every profile field that configures the open path;
    it is filtered by ``inspect.signature(mgr.open_all)`` so a manager built
    before a field's consumer landed still opens, and a manager that takes
    ``**kwargs`` receives everything. Optional fields whose value is None are
    left out because None means "keep the camera file's value".

        mgr.open_all(**open_kwargs(mgr, profile))
    """
    candidates = {}
    for attr, kw in _OPEN_KWARG_FIELDS.items():
        value = getattr(profile, attr, None)
        if value is None:
            continue
        if kw == "only_serials" and not value:
            continue
        candidates[kw] = value
    try:
        params = inspect.signature(mgr.open_all).parameters
    except (TypeError, ValueError):
        return candidates
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return candidates
    return {k: v for k, v in candidates.items() if k in params}
