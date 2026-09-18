"""The cold path: what CameraManager does before and after a recording.

Opening cameras is where a whole session is won or lost silently. Geometry is
read back from the cameras, but nothing used to compare it with the profile's
frame_width/frame_height -- and those two numbers size the NV12 ring and the
raw decode, so a .pfs edited in pylon Viewer could shear or empty a
full-length recording with no error anywhere. The agreed geometry was also
never cleared, so switching to a rig with a different ROI refused every camera
against a rig that was no longer open. Camera names are positional and every
extrinsic attaches to a name, so a device that shifts a position attaches a
camera's calibration to a different physical camera. And exposure/gain are
written on every acquisition start, where a value that does not land halves
that camera's frame rate with nothing in the log.

Each of those is exercised here against a stub backend: no cameras, no vendor
SDK, no grab threads. The hot path lives in test_grab_failure.py.

    python test_camera_manager.py
"""
import inspect
import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# No vendor SDK is needed or wanted: the manager talks to a backend instance.
sys.modules["pypylon"] = None
sys.modules["pypylon.pylon"] = None

from gui_app import camera_manager as cm                       # noqa: E402
from gui_app import rig_setup                                  # noqa: E402
from gui_app.camera_manager import (CameraManager,             # noqa: E402
                                    AcquisitionStartRefused)

failures = []
_n = 0


def check(name, cond, detail=""):
    global _n
    _n += 1
    print(f"{_n}) {name}: {'PASS' if cond else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


def raises(exc, fn, *args, **kwargs):
    """The exception raised by fn(), or None."""
    try:
        fn(*args, **kwargs)
    except exc as e:
        return e
    return None


class StubDevice:
    def __init__(self, serial):
        self._serial = serial

    def GetSerialNumber(self):
        return self._serial


class StubCamera:
    def __init__(self, serial, width, height, pixel_format="Mono8",
                 exposure=3000.0, gain=6.0):
        self.serial = serial
        self.width, self.height = width, height
        self.pixel_format = pixel_format
        self.exposure, self.gain = exposure, gain
        self.closed = False


class StubBackend:
    """Every call the cold path makes on a backend.

    Geometry, pixel format and the exposure/gain read-back are per camera, so
    a test can make one camera disagree with the rest or with the profile.
    """
    name = "stub"

    def __init__(self, cameras):
        self._cameras = {c.serial: c for c in cameras}
        self.order = [c.serial for c in cameras]
        self.opened, self.closed, self.stopped = [], [], []
        self.freerun, self.triggered = [], []
        self.exposure_calls, self.bandwidth_calls = [], []
        #: serial -> (exposure, gain) to report back instead of what was asked.
        self.applied = {}
        self.exposure_error = None

    # -- enumeration and open ------------------------------------------------
    def enumerate_devices(self):
        return [StubDevice(s) for s in self.order]

    def open(self, dev, pfs_path, max_num_buffer):
        cam = self._cameras[dev.GetSerialNumber()]
        self.opened.append(cam.serial)
        return cam

    def describe(self, cam):
        return {"width": cam.width, "height": cam.height,
                "pixel_format": cam.pixel_format, "serial": cam.serial}

    def get_exposure_gain(self, cam):
        return (cam.exposure, cam.gain)

    def enable_extended_block_ids(self, i, cam):
        return True

    def select_gige_driver(self, i, cam, driver):
        return True

    def set_bandwidth_reserve(self, cam, percent=None, accumulation=None):
        self.bandwidth_calls.append((cam.serial, percent, accumulation))
        return {"GevSCBWR": percent, "GevSCBWRA": accumulation}

    # -- run state -----------------------------------------------------------
    def set_freerun(self, cam, fps):
        self.freerun.append(cam.serial)

    def set_triggered(self, cam, limit, announce=False):
        self.triggered.append((cam.serial, limit))

    def set_exposure_gain(self, cam, exposure_us=None, gain_db=None,
                          gain_unit=None):
        self.exposure_calls.append((cam.serial, exposure_us, gain_db,
                                    gain_unit))
        if self.exposure_error is not None:
            raise self.exposure_error
        return self.applied.get(cam.serial, (exposure_us, gain_db))

    def stream_stats(self, cam):
        return {"Statistic_Failed_Buffer_Count": 0, "GevSCFJM": 48000}

    def stop_grabbing(self, cam):
        self.stopped.append(cam.serial)

    def close(self, cam):
        cam.closed = True
        self.closed.append(cam.serial)


def manager(backend):
    """A CameraManager on `backend` whose grab threads are stubbed out.

    Starting real grab threads would need cameras; every test here is about
    what the manager does to the cameras, so the thread start is recorded
    rather than performed.
    """
    saved = cm.load_backend
    cm.load_backend = lambda name: backend
    try:
        mgr = CameraManager("stub")
    finally:
        cm.load_backend = saved
    mgr.started_threads = []
    mgr._start_grab_threads = lambda *a, **kw: mgr.started_threads.append(kw)
    mgr._stop_grab_threads = lambda: None
    mgr.errors = []
    mgr.error.connect(mgr.errors.append)
    return mgr


def rig(serials=("21111111", "21111112", "21111113"), width=1920, height=1200,
        **kw):
    return StubBackend([StubCamera(s, width, height, **kw) for s in serials])


def opened(mgr, **kw):
    """open_all with the arguments a profile would supply; returns (result, log)."""
    out = io.StringIO()
    with redirect_stdout(out):
        result = mgr.open_all("rig.pfs", **kw)
    return result, out.getvalue()


# --- geometry ---------------------------------------------------------------
b = rig()
m = manager(b)
ok, log = opened(m, expect_cameras=3, expect_geometry=(1920, 1200))
check("open_all accepts cameras that match the profile's geometry",
      ok is True and m.num_cameras == 3 and m.geometry == (1920, 1200),
      f"{ok!r} {m.geometry!r}")
check("every camera is named and logged by position",
      all(f"[cam{i+1}]" in log for i in range(3)), log)

check("geometry is read-only: the cameras are the source, not the caller",
      raises(AttributeError, setattr, m, "geometry", (640, 480)) is not None)

m.close_all()
check("close_all clears the agreed geometry", m.geometry is None)

b2 = rig(width=1280, height=1024)
m2 = manager(b2)
ok2, log2 = opened(m2, expect_geometry=(1280, 1024))
check("a 1280x1024 rig opens on a fresh manager", ok2 is True
      and m2.geometry == (1280, 1024), log2)

# The same manager instance, to pin that the reset is in open_all as well as
# in close_all: this is the live profile switch.
m3 = manager(rig())
opened(m3, expect_geometry=(1920, 1200))
m3._backend = rig(width=1280, height=1024)
ok3, log3 = opened(m3, expect_geometry=(1280, 1024))
check("a profile switch to a different-resolution rig is not refused against "
      "the previous rig", ok3 is True and m3.geometry == (1280, 1024),
      log3 + str(m3.errors))

b4 = rig()
m4 = manager(b4)
ok4, log4 = opened(m4, expect_geometry=(1280, 1024))
check("a camera whose ROI differs from the profile refuses the open",
      not ok4 and "1920x1200" in str(m4.errors)
      and "Fix the .pfs" in str(m4.errors), str(m4.errors))
check("the refused open leaves no cameras open",
      m4.num_cameras == 0 and m4.geometry is None and len(b4.closed) >= 1,
      str(b4.closed))

mixed = StubBackend([StubCamera("21111111", 1920, 1200),
                     StubCamera("21111112", 1280, 1024)])
m5 = manager(mixed)
ok5, _ = opened(m5)
check("cameras that disagree with each other still refuse the open",
      not ok5 and "differs from camera 1" in str(m5.errors), str(m5.errors))

# --- start_acquisition refuses a profile/camera mismatch --------------------
b6 = rig()
m6 = manager(b6)
opened(m6, expect_geometry=(1920, 1200))
m6.started_threads.clear()          # the preview threads the open started
e = raises(AcquisitionStartRefused, m6.start_acquisition, [], width=1280,
           height=1024, fps=100)
check("start_acquisition refuses a width/height the cameras are not "
      "configured for", e is not None and "1920x1200" in str(e), str(e))
check("the refusal happens before any camera is reconfigured",
      b6.triggered == [] and m6.started_threads == [],
      f"{b6.triggered} {m6.started_threads}")

# --- rig_setup --------------------------------------------------------------


class _Profile:
    """The open-path fields of a RigProfile, without loading a file."""
    pfs_path = "rig.pfs"
    gige_driver = "socket"
    trigger_rate_limit = 165.0
    n_cameras = 3
    max_num_buffer = 600
    camera_serials = None
    camera_backend = "basler"
    gev_bandwidth_reserve_pct = None
    gev_bandwidth_reserve_accum = None
    frame_width = 1920
    frame_height = 1200
    pin_capture_threads = False
    encoder_pcores = False
    pin_encoder_threads = False


kw = rig_setup.open_kwargs(CameraManager, _Profile())
check("open_kwargs emits expect_geometry as one (width, height) keyword",
      kw.get("expect_geometry") == (1920, 1200), str(kw))
check("every keyword open_kwargs emits is accepted by open_all",
      set(kw) <= set(inspect.signature(CameraManager.open_all).parameters),
      str(sorted(kw)))

# ----------------------------------------------------------------------------
if failures:
    print(f"\n{len(failures)} FAILED: {failures}")
    sys.exit(1)
print(f"\nALL {_n} CAMERA-MANAGER TESTS PASS")
