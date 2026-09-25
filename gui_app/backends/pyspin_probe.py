"""PySpin measurements for probe_flir.py's optional --pyspin stage.

Panopticon's FLIR backend (flir.py) drives the Spinnaker C API through ctypes
(_spinc.py) and never uses PySpin. This module is the one place PySpin is
imported, so the vendor code stays under gui_app/backends/. It answers two
questions on one camera: is PySpin's GetNDArray a view of the image buffer or
a copy, and does a PySpin wait for an image hold the GIL. Nothing imports it
unless the stage runs, and it imports PySpin only when called.

The probe owns the reporting: it passes in the dicts to fill, its GIL meter
and its error formatter, and turns the results into checks and answers.
"""
from __future__ import annotations

import importlib
import time

#: Spinnaker's timeout error code, which a wait loop retries at once.
_TIMEOUT = -1011


def load():
    """Import PySpin and return the module. Raises what the import raises."""
    return importlib.import_module("PySpin")


def _node(ps, nodemap, name, kind):
    ptr = {"enum": "CEnumerationPtr", "int": "CIntegerPtr",
           "float": "CFloatPtr", "bool": "CBooleanPtr",
           "string": "CStringPtr"}[kind]
    return getattr(ps, ptr)(nodemap.GetNode(name))


def _set(ps, nodemap, name, value) -> bool:
    """Write one node; False when the camera lacks it or refuses it."""
    try:
        if isinstance(value, bool):
            n = _node(ps, nodemap, name, "bool")
        elif isinstance(value, int):
            n = _node(ps, nodemap, name, "int")
        elif isinstance(value, float):
            n = _node(ps, nodemap, name, "float")
        else:
            n = _node(ps, nodemap, name, "enum")
            if not (ps.IsAvailable(n) and ps.IsWritable(n)):
                return False
            e = n.GetEntryByName(value)
            if not (ps.IsAvailable(e) and ps.IsReadable(e)):
                return False
            n.SetIntValue(e.GetValue())
            return True
        if not (ps.IsAvailable(n) and ps.IsWritable(n)):
            return False
        if isinstance(value, float):
            value = min(max(value, n.GetMin()), n.GetMax())
        n.SetValue(value)
        return True
    except Exception:
        return False


def _get_string(ps, nodemap, name):
    try:
        n = _node(ps, nodemap, name, "string")
        return n.GetValue() if ps.IsAvailable(n) and ps.IsReadable(n) else None
    except Exception:
        return None


def measure(ps, serials, rep: dict, sdk: dict, *, buffers: int,
            selftest_fps: float, gil_fps: float, gil_window_s: float,
            make_meter, err) -> bool:
    """Measure GetNDArray and the GIL during a wait on one camera.

    ``serials`` limits the camera to the profile's (empty takes the first).
    Results go into ``rep`` (serial, zero_copy, gil, release_error) and
    ``sdk`` (pyspin_version) as they are measured, so a failure part way
    keeps what came before it. ``make_meter()`` returns the probe's GIL meter
    (``held(load, seconds)`` and ``close()``); ``err(e)`` formats an
    exception. Returns False when PySpin lists no matching camera.
    """
    import numpy as np
    system = ps.System.GetInstance()
    try:
        v = system.GetLibraryVersion()
        sdk["pyspin_version"] = ".".join(str(getattr(v, k, "?")) for k in
                                         ("major", "minor", "type", "build"))
    except Exception as e:
        sdk["pyspin_version"] = None
        sdk["pyspin_version_error"] = err(e)
    cams = system.GetCameras()
    want = [str(s) for s in (serials or [])]
    cam = None
    try:
        for i in range(cams.GetSize()):
            c = cams.GetByIndex(i)
            serial = _get_string(ps, c.GetTLDeviceNodeMap(),
                                 "DeviceSerialNumber")
            if not want or serial in want:
                cam, rep["serial"] = c, serial
                break
            del c
        if cam is None:
            return False
        cam.Init()
        try:
            nm = cam.GetNodeMap()
            snm = cam.GetTLStreamNodeMap()
            for sel in ("FrameStart", "AcquisitionStart", "FrameBurstStart"):
                if _set(ps, nm, "TriggerSelector", sel):
                    _set(ps, nm, "TriggerMode", "Off")
            _set(ps, nm, "AcquisitionMode", "Continuous")
            _set(ps, snm, "StreamBufferCountMode", "Manual")
            _set(ps, snm, "StreamBufferCountManual", buffers)
            _set(ps, nm, "AcquisitionFrameRateEnable", True)
            _set(ps, nm, "AcquisitionFrameRate", selftest_fps)
            shares, own, base, ptrs, n = [], [], set(), set(), 0
            cam.BeginAcquisition()
            try:
                for _k in range(2 * buffers):
                    img = cam.GetNextImage(2000)
                    try:
                        if img.IsIncomplete():
                            continue
                        a = img.GetNDArray()
                        b = img.GetNDArray()
                        shares.append(bool(np.shares_memory(a, b)))
                        own.append(bool(a.flags.owndata))
                        base.add(type(a.base).__name__)
                        ptrs.add(int(a.ctypes.data))
                        n += 1
                        del a, b
                    finally:
                        img.Release()
            finally:
                cam.EndAcquisition()
            rep["zero_copy"] = {
                "frames": n, "buffers": buffers,
                "shares_memory": all(shares) if shares else None,
                "owndata": any(own) if own else None,
                "base_type": sorted(base), "distinct_ptrs": len(ptrs)}
            _set(ps, nm, "AcquisitionFrameRate", gil_fps)
            meter = make_meter()
            try:
                cam.BeginAcquisition()
                try:
                    def wait(stop):
                        seen = {"frames": 0, "errors": 0, "last_error": None}
                        while not stop.is_set():
                            try:
                                im = cam.GetNextImage(2000)
                            except Exception as e:
                                # A timeout retries at once; any other error
                                # is counted and waits, so the meter does not
                                # measure a spinning loop.
                                if getattr(e, "errorcode", None) != _TIMEOUT:
                                    seen["errors"] += 1
                                    seen["last_error"] = err(e)
                                    time.sleep(0.05)
                                continue
                            im.Release()
                            seen["frames"] += 1
                        return seen
                    rep["gil"] = meter.held(wait, gil_window_s)
                finally:
                    cam.EndAcquisition()
            finally:
                meter.close()
        finally:
            cam.DeInit()
    finally:
        del cam
        cams.Clear()
        try:
            system.ReleaseInstance()
        except Exception as e:
            rep["release_error"] = err(e)
    return True
