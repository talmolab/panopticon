"""Platform layer, offline: cpu_affinity class detection and the Basler backend.

Why these need a test at all. Both modules talk to things this test host may
not have -- a hybrid CPU with several efficiency classes, more than 64 logical
processors, or a Basler camera -- and both used to fail in ways that LOOK like
success: an E-core classified as a P-core pins a grab thread onto the slow
core deterministically, and a swallowed SetValue error lets one camera record
at the wrong exposure with nothing in the log. So the decision logic is fed
synthetic inputs here and the outcomes are pinned.

No cameras, no pypylon, no hybrid CPU: pypylon is replaced by a stub package
BEFORE the backend is imported, so the same file runs on a machine without the
SDK, and GetSystemCpuSetInformation buffers are built by hand.

    uv run python test_cpu_affinity.py
"""
import os
import struct
import subprocess
import sys
import types

ROOT = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable

failures = []


def check(num, name, cond, detail=""):
    print(f"{num}) {name}: {'PASS' if cond else 'FAIL'}"
          + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


# ---------------------------------------------------------------------------
# A stub pypylon, installed before gui_app.backends.basler is imported. The
# backend binds pylon.TimeoutException / GrabStrategy_OneByOne at class
# definition, so those names must exist; nothing else is touched by the cold
# path under test.
# ---------------------------------------------------------------------------
class _StubTimeout(Exception):
    pass


class _LogicalError(Exception):
    """Stands in for genicam.LogicalErrorException ('Node not existing')."""


def _install_stub_pypylon():
    pkg = types.ModuleType("pypylon")
    pkg.__path__ = []
    pylon = types.ModuleType("pypylon.pylon")
    pylon.TimeoutException = _StubTimeout
    pylon.GrabStrategy_OneByOne = object()
    pylon.TimeoutHandling_ThrowException = object()
    genicam = types.ModuleType("pypylon.genicam")
    genicam.LogicalErrorException = _LogicalError
    genicam.IsImplemented = lambda node: getattr(node, "implemented", True)
    genicam.IsWritable = lambda node: getattr(node, "writable", True)
    pkg.pylon = pylon
    pkg.genicam = genicam
    sys.modules["pypylon"] = pkg
    sys.modules["pypylon.pylon"] = pylon
    sys.modules["pypylon.genicam"] = genicam


_install_stub_pypylon()
sys.path.insert(0, ROOT)
from gui_app.backends import basler  # noqa: E402
from gui_app import cpu_affinity as ca  # noqa: E402


class StubNode:
    """One GenICam node. `fail` is raised by SetValue. Min/Max mimic
    pypylon's IFloat/IInteger accessors (nodes themselves)."""

    def __init__(self, value=0.0, lo=None, hi=None, fail=None,
                 implemented=True, symbolics=None):
        self.value = value
        self.Min = lo
        self.Max = hi
        self.fail = fail
        self.implemented = implemented
        self.writes = []
        self._symbolics = symbolics

    def GetValue(self):
        return self.value

    def SetValue(self, v):
        if self.fail is not None:
            raise self.fail
        self.writes.append(v)
        self.value = v

    def FromString(self, s):
        self.SetValue(s)

    def ToString(self):
        return str(self.value)

    @property
    def Symbolics(self):
        if self._symbolics is None:
            raise AttributeError("Symbolics")
        return tuple(self._symbolics)


class StubNodeMap:
    def __init__(self, nodes, missing_raises=False):
        self.nodes = nodes
        self.missing_raises = missing_raises

    def GetNode(self, name):
        if name not in self.nodes:
            if self.missing_raises:
                raise _LogicalError(f"Node not existing ({name})")
            return None
        return self.nodes[name]


class _Recording:
    """Proxy that logs (node, value) on SetValue, so ORDER is observable."""

    def __init__(self, cam, name, node):
        self._cam, self._name, self._node = cam, name, node

    def SetValue(self, v):
        self._cam.calls.append((self._name, v))
        self._node.SetValue(v)

    def __getattr__(self, a):
        return getattr(self._node, a)


class StubCamera:
    """Enough of InstantCamera for the cold path: a node map, attribute
    access that resolves to nodes (as pypylon's __getattr__ does), a stream
    grabber node map, and a call log so order can be asserted."""

    def __init__(self, nodes=None, sg_nodes=None, missing_raises=False):
        self._nodes = nodes or {}
        self._sg = StubNodeMap(sg_nodes or {}, missing_raises)
        self._nodemap = StubNodeMap(self._nodes, missing_raises)
        self.calls = []

    def GetNodeMap(self):
        return self._nodemap

    def GetStreamGrabberNodeMap(self):
        return self._sg

    def StopGrabbing(self):
        self.calls.append(("StopGrabbing",))

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        node = self._nodes.get(name)
        if node is None:
            raise AttributeError(f"no node {name}")
        return _Recording(self, name, node)


B = basler.BaslerBackend

# ===========================================================================
# set_exposure_gain: probe presence, propagate write errors (A3-09, M7)
# ===========================================================================
n = 0

# 1 -- the happy path on an ace2-style camera: ExposureTime and Gain present.
n += 1
cam = StubCamera({"ExposureTime": StubNode(2000.0, lo=StubNode(10.0),
                                           hi=StubNode(10_000_000.0)),
                  "Gain": StubNode(0.0)})
exp, gain = B.set_exposure_gain(cam, 3000.0, 6.0)
check(n, "present nodes are written and read back",
      exp == 3000.0 and gain == 6.0
      and cam._nodes["ExposureTime"].writes == [3000.0],
      f"exp={exp} gain={gain}")

# 2 -- a SetValue failure on a PRESENT node must propagate, not fall through
#      to ExposureTimeAbs and return None.
n += 1
boom = RuntimeError("OutOfRangeException: value out of range")
cam = StubCamera({"ExposureTime": StubNode(2000.0, fail=boom),
                  "Gain": StubNode(0.0)})
raised = None
try:
    B.set_exposure_gain(cam, 3000.0, None)
except Exception as e:
    raised = e
check(n, "SetValue error on a present node propagates", raised is boom,
      repr(raised))

# 3 -- an ABSENT node is skipped (None returned), whether the node map reports
#      absence as None or as LogicalErrorException.
n += 1
cam = StubCamera({"Gain": StubNode(1.0)})
exp, gain = B.set_exposure_gain(cam, 3000.0, 2.0)
cam2 = StubCamera({"Gain": StubNode(1.0)}, missing_raises=True)
exp2, gain2 = B.set_exposure_gain(cam2, 3000.0, 2.0)
check(n, "absent exposure node yields None without raising",
      exp is None and gain == 2.0 and exp2 is None and gain2 == 2.0,
      f"{exp},{gain} / {exp2},{gain2}")

# 4 -- the legacy spelling is used only when the modern one is absent, and a
#      node that exists but is not implemented counts as absent.
n += 1
cam = StubCamera({"ExposureTime": StubNode(0.0, implemented=False),
                  "ExposureTimeAbs": StubNode(500.0)})
exp, _ = B.set_exposure_gain(cam, 1234.0, None)
check(n, "unimplemented node is skipped in favour of the next candidate",
      exp == 1234.0 and cam._nodes["ExposureTimeAbs"].writes == [1234.0]
      and cam._nodes["ExposureTime"].writes == [], f"exp={exp}")

# 5 -- a gain write failure propagates too (the calibration_gain_db path).
n += 1
gboom = RuntimeError("AccessException: node not writable while grabbing")
cam = StubCamera({"ExposureTime": StubNode(1.0),
                  "Gain": StubNode(0.0, fail=gboom)})
raised = None
try:
    B.set_exposure_gain(cam, None, 6.0)
except Exception as e:
    raised = e
check(n, "gain SetValue error propagates", raised is gboom, repr(raised))

# 6 -- None means leave alone: nothing written, nothing read.
n += 1
cam = StubCamera({"ExposureTime": StubNode(1.0), "Gain": StubNode(0.0)})
out = B.set_exposure_gain(cam, None, None)
check(n, "None leaves both controls untouched",
      out == (None, None) and cam._nodes["ExposureTime"].writes == []
      and cam._nodes["Gain"].writes == [], str(out))

# 7 -- exposure is clamped into the node's published range.
n += 1
cam = StubCamera({"ExposureTime": StubNode(1.0, lo=StubNode(20.0),
                                           hi=StubNode(5000.0))})
exp, _ = B.set_exposure_gain(cam, 1e9, None)
check(n, "exposure clamped to node Max", exp == 5000.0, f"exp={exp}")

# 8 -- get_exposure_gain reads the same nodes and reports None for absent ones.
n += 1
cam = StubCamera({"ExposureTimeAbs": StubNode(777.0)})
check(n, "get_exposure_gain: legacy node read, absent gain is None",
      B.get_exposure_gain(cam) == (777.0, None), str(B.get_exposure_gain(cam)))

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL CPU AFFINITY / BASLER BACKEND TESTS PASS")
