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
#: consumer has not landed is declared without breaking open. A field whose
#: value is None is left out, so ``camera`` reaches open_all (and the backend)
#: only when the profile has a camera: block.
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
    "camera": "camera_spec",
}

#: open_all keywords that open_kwargs never drops for a manager that does not
#: accept them. The camera: block is the only source of the settings it
#: names; a manager that lost it would open the cameras on whatever they
#: hold, so open_kwargs refuses instead.
_REQUIRED_OPEN_KWARGS = ("camera_spec",)


def _expect_geometry(profile):
    """The profile's (frame_width, frame_height), or None when either is unset.

    The pair travels as ONE keyword because open_all compares it to the ROI
    the cameras report as a pair; sent as two keywords, the signature filter
    could drop one of them and the check would pass on half the geometry.
    """
    w = getattr(profile, "frame_width", None)
    h = getattr(profile, "frame_height", None)
    if not w or not h:
        return None
    return (int(w), int(h))


#: open_all keywords built from more than one profile field: keyword ->
#: callable(profile) returning the value, or None to leave the keyword out.
_DERIVED_OPEN_KWARGS = {
    "expect_geometry": _expect_geometry,
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

    The candidate set is every profile field that configures the open path,
    plus the keywords derived from more than one field (expect_geometry);
    it is filtered by ``inspect.signature(mgr.open_all)`` so a manager built
    before a field's consumer landed still opens, and a manager that takes
    ``**kwargs`` receives everything. Optional fields whose value is None are
    left out because None means "keep the camera file's value".

    The exception is a keyword in ``_REQUIRED_OPEN_KWARGS``: when the profile
    sets it and the manager does not accept it, RuntimeError names the
    keyword, because opening without it would configure the cameras from
    something other than the profile.

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
    for kw, build in _DERIVED_OPEN_KWARGS.items():
        value = build(profile)
        if value is not None:
            candidates[kw] = value
    try:
        params = inspect.signature(mgr.open_all).parameters
    except (TypeError, ValueError):
        return candidates
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return candidates
    lost = [k for k in _REQUIRED_OPEN_KWARGS if k in candidates and k not in params]
    if lost:
        raise RuntimeError(
            f"Profile {getattr(profile, 'name', '?')!r} has a camera: block, "
            f"but this copy of Panopticon's camera manager does not pass "
            f"{', '.join(lost)} to the camera backend, so the cameras would "
            f"open without the block's settings. Update gui_app/"
            f"camera_manager.py to a version whose open_all takes "
            f"{', '.join(lost)}.")
    return {k: v for k, v in candidates.items() if k in params}


def make_manager(profile: RigProfile, **kwargs):
    """The camera manager ``profile`` asks for.

    ``capture_processes`` 0 (the default in every shipped profile) returns a
    CameraManager, exactly as the GUI and the probes build it today. A
    positive value returns a gui_app.mp.manager.ProcessCameraManager, which
    has the same surface and captures the cameras in that many worker
    processes, dealt to them in contiguous groups by camera index.
    ``kwargs`` go to ProcessCameraManager (``log_dir``, ``backend``).

    Both are configured the same way afterwards:
    ``apply_profile_to_manager(mgr, profile)`` and
    ``mgr.open_all(**open_kwargs(mgr, profile))``.

    The multi-process package is imported only for a positive value, so a
    profile at 0 never loads it.
    """
    if int(getattr(profile, "capture_processes", 0) or 0) > 0:
        from gui_app.mp.manager import ProcessCameraManager
        return ProcessCameraManager(profile, **kwargs)
    from gui_app.camera_manager import CameraManager
    return CameraManager()
