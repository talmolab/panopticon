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

# ===========================================================================
# GainRaw cameras: integer steps, not dB (A3-13)
# ===========================================================================

# 9 -- gain_unit names the node's unit, or None without a gain control.
n += 1
units = (B.gain_unit(StubCamera({"Gain": StubNode(0.0)})),
         B.gain_unit(StubCamera({"GainRaw": StubNode(36)})),
         B.gain_unit(StubCamera({})))
check(n, "gain_unit: Gain -> dB, GainRaw -> raw, none -> None",
      units == ("dB", "raw", None), str(units))

# 10 -- on a GainRaw camera the write is an integer clamped into range; the
#       baseline read by get_exposure_gain restores exactly.
n += 1
cam = StubCamera({"GainRaw": StubNode(36, lo=StubNode(0), hi=StubNode(63))})
_, base = B.get_exposure_gain(cam)
_, g1 = B.set_exposure_gain(cam, None, 6.4)
_, g2 = B.set_exposure_gain(cam, None, 1000)
_, g3 = B.set_exposure_gain(cam, None, base)
w = cam._nodes["GainRaw"].writes
check(n, "GainRaw: integer writes, clamped, baseline restore round-trips",
      w == [6, 63, 36] and all(isinstance(x, int) for x in w)
      and (g1, g2, g3) == (6, 63, 36), f"writes={w}")

# ===========================================================================
# Importing the backend without pypylon (A3-02)
# ===========================================================================

# 11 -- a fresh interpreter with pypylon blocked must fail at the backend import
#      with an ImportError that names pypylon AND the camera_backend field.
n += 1
code = """
import sys
for m in ('pypylon', 'pypylon.pylon', 'pypylon.genicam'):
    sys.modules[m] = None
try:
    import gui_app.backends.basler
except ImportError as e:
    print('IMPORTERROR:' + str(e))
else:
    print('NO ERROR')
"""
r = subprocess.run([PY, "-c", code], cwd=ROOT, capture_output=True, text=True)
msg = r.stdout.strip()
check(n, "import without pypylon raises ImportError naming pypylon and camera_backend",
      msg.startswith("IMPORTERROR:") and "pypylon" in msg
      and "camera_backend" in msg and r.returncode == 0,
      msg[:120] + (" | stderr: " + r.stderr.strip()[-200:] if r.stderr.strip() else ""))

# ===========================================================================
# set_freerun: select FrameStart before disarming the trigger (A3-11)
# ===========================================================================

def _freerun_cam(symbolics):
    return StubCamera({
        "TriggerSelector": StubNode("AcquisitionStart", symbolics=symbolics),
        "TriggerMode": StubNode("On"),
        "AcquisitionFrameRateEnable": StubNode(False),
        "AcquisitionFrameRate": StubNode(0.0),
    })


# 12 -- the selector is written BEFORE TriggerMode Off, on a camera whose
#       .pfs left another selector active.
n += 1
cam = _freerun_cam(symbolics=None)
B().set_freerun(cam, 30.0)
sets = [c for c in cam.calls if c[0] in ("TriggerSelector", "TriggerMode")]
check(n, "set_freerun selects FrameStart before writing TriggerMode Off",
      sets[:2] == [("TriggerSelector", "FrameStart"), ("TriggerMode", "Off")]
      and cam._nodes["TriggerSelector"].value == "FrameStart"
      and cam._nodes["AcquisitionFrameRate"].value == 30.0, str(sets))

# 13 -- other selectors the camera offers are disarmed too, and the selector
#       ends on FrameStart so set_triggered and the .pfs agree afterwards.
n += 1
cam = _freerun_cam(symbolics=("FrameStart", "AcquisitionStart", "ExposureActive"))
B().set_freerun(cam, 30.0)
sets = [c for c in cam.calls if c[0] in ("TriggerSelector", "TriggerMode")]
check(n, "offered AcquisitionStart trigger is disarmed, selector left on FrameStart",
      sets == [("TriggerSelector", "FrameStart"), ("TriggerMode", "Off"),
               ("TriggerSelector", "AcquisitionStart"), ("TriggerMode", "Off"),
               ("TriggerSelector", "FrameStart")], str(sets))

# 14 -- a selector the camera does not offer is never written (it would raise).
n += 1
cam = _freerun_cam(symbolics=("FrameStart", "FrameBurstStart"))
B().set_freerun(cam, 30.0)
written = {c[1] for c in cam.calls if len(c) == 2 and c[0] == "TriggerSelector"}
check(n, "only offered selectors are written",
      written == {"FrameStart", "FrameBurstStart"}, str(written))

# ===========================================================================
# GigE transport knobs: GevSCFTD, GevSCBWR/GevSCBWRA, and GevSCFJM in
# stream_stats. Opt-in, no GUI caller; the contract pinned here is what P02
# and P19 will call.
# ===========================================================================

# 15 -- set_transmission_delay writes GevSCFTD and returns the read-back; the
#       module-level spelling is the same function.
n += 1
cam = StubCamera({"GevSCFTD": StubNode(0)})
got = basler.set_transmission_delay(cam, 12500)
check(n, "set_transmission_delay writes GevSCFTD and returns it",
      got == 12500 and cam._nodes["GevSCFTD"].writes == [12500]
      and basler.set_transmission_delay == B.set_transmission_delay, f"got={got}")

# 16 -- a camera without the node is a configuration error naming the node;
#       a write error propagates.
n += 1
raised = None
try:
    B.set_transmission_delay(StubCamera({}), 1)
except RuntimeError as e:
    raised = e
wboom = RuntimeError("OutOfRangeException")
raised2 = None
try:
    B.set_transmission_delay(StubCamera({"GevSCFTD": StubNode(0, fail=wboom)}), 1)
except Exception as e:
    raised2 = e
check(n, "missing GevSCFTD raises naming the node; write error propagates",
      raised is not None and "GevSCFTD" in str(raised) and raised2 is wboom,
      f"{raised} / {raised2!r}")

# 17 -- set_bandwidth_reserve selects Manual mode first, writes both knobs,
#       and reports the assigned bandwidth when the camera publishes it.
n += 1
cam = StubCamera({"BandwidthReserveMode": StubNode("Standard"),
                  "GevSCBWR": StubNode(5), "GevSCBWRA": StubNode(3),
                  "GevSCBWA": StubNode(118_000_000)})
out = B.set_bandwidth_reserve(cam, percent=15, accumulation=6)
check(n, "set_bandwidth_reserve: Manual mode first, both knobs written, GevSCBWA reported",
      cam._nodes["BandwidthReserveMode"].writes == ["Manual"]
      and out.get("GevSCBWR") == 15 and out.get("GevSCBWRA") == 6
      and out.get("GevSCBWA") == 118_000_000
      and out.get("BandwidthReserveMode") == "Manual", str(out))

# 18 -- None leaves a knob alone; both None writes nothing at all (not even
#       the mode), so an unset profile is a true no-op.
n += 1
cam = StubCamera({"BandwidthReserveMode": StubNode("Standard"),
                  "GevSCBWR": StubNode(5), "GevSCBWRA": StubNode(3)})
out_none = B.set_bandwidth_reserve(cam)
out_one = B.set_bandwidth_reserve(cam, percent=20)
check(n, "set_bandwidth_reserve: None leaves alone, all-None is a no-op",
      out_none == {} and cam._nodes["BandwidthReserveMode"].writes == ["Manual"]
      and cam._nodes["GevSCBWRA"].writes == [] and out_one.get("GevSCBWR") == 20
      and "GevSCBWRA" not in out_one, f"{out_none} / {out_one}")

# 19 -- a camera without the reserve nodes (USB, or Manual mode absent)
#       raises for the knob, but the mode node is optional.
n += 1
raised = None
try:
    B.set_bandwidth_reserve(StubCamera({"GevSCBWR": StubNode(5)}), accumulation=4)
except RuntimeError as e:
    raised = e
ok_no_mode = B.set_bandwidth_reserve(StubCamera({"GevSCBWR": StubNode(5)}), percent=7)
check(n, "missing GevSCBWRA raises; absent BandwidthReserveMode is tolerated",
      raised is not None and "GevSCBWRA" in str(raised)
      and ok_no_mode == {"GevSCBWR": 7}, f"{raised} / {ok_no_mode}")

# 20 -- stream_stats reads GevSCFJM and the transport knobs from the camera
#       node map when present, and omits them when absent, next to the
#       stream-grabber counters.
n += 1
cam = StubCamera({"GevSCFJM": StubNode(48_000), "GevSCFTD": StubNode(0),
                  "GevSCBWR": StubNode(10)},
                 sg_nodes={"Statistic_Failed_Buffer_Count": StubNode(0),
                           "Statistic_Resend_Request_Count": StubNode(11)})
st = B().stream_stats(cam)
st_bare = B().stream_stats(StubCamera({}, sg_nodes={}))
check(n, "stream_stats carries GevSCFJM and the knobs when present, omits when absent",
      st.get("GevSCFJM") == 48_000 and st.get("GevSCFTD") == 0
      and st.get("GevSCBWR") == 10 and "GevSCBWRA" not in st
      and st.get("Resend_Request_Count") == 11 and st.get("Failed_Buffer_Count") == 0
      and st_bare == {}, f"{st} / {st_bare}")

# ===========================================================================
# enable_extended_block_ids: 'enabled' only when BOTH halves took (A3-14)
# ===========================================================================
import contextlib  # noqa: E402
import io  # noqa: E402


def _ext_ids(cam_side, grabber_side):
    nodes = {"GevGVSPExtendedIDMode": StubNode("Off")} if cam_side else {}
    sg = {"UseExtendedIdIfAvailable": StubNode(False)} if grabber_side else {}
    cam = StubCamera(nodes, sg_nodes=sg)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = B.enable_extended_block_ids(0, cam)
    return ok, buf.getvalue()


# 21 -- both halves present: negotiated, reported as enabled.
n += 1
ok, log = _ext_ids(True, True)
check(n, "64-bit IDs: both halves set -> True and 'enabled'",
      ok is True and "enabled" in log and "UNAVAILABLE" not in log, log.strip())

# 22 -- the grabber flag alone does not put 64-bit IDs on the wire: False,
#       and the log names the camera-side half that did not take.
n += 1
ok, log = _ext_ids(False, True)
check(n, "64-bit IDs: grabber flag alone -> False, camera half named",
      ok is False and "UNAVAILABLE" in log and "GevGVSPExtendedIDMode" in log
      and "UseExtendedIdIfAvailable" not in log.split("UNAVAILABLE")[1],
      log.strip())

# 23 -- the camera mode alone is likewise not 'enabled'; the grabber half is named.
n += 1
ok, log = _ext_ids(True, False)
check(n, "64-bit IDs: camera mode alone -> False, grabber half named",
      ok is False and "UseExtendedIdIfAvailable" in log.split("UNAVAILABLE")[1],
      log.strip())

# ===========================================================================
# cpu_affinity: efficiency-class parsing on hosts this machine is not
# (A3-23 top class only, A3-24 processor groups)
# ===========================================================================

def cpu_set_record(logical, eff, group=0, cid=None, size=32, rtype=0):
    """One SYSTEM_CPU_SET_INFORMATION record: Size Type Id Group Logical Core
    LLC Numa EfficiencyClass AllFlags Reserved AllocationTag, padded to size."""
    cid = 256 + logical + 64 * group if cid is None else cid
    head = struct.pack("<IIIHBBBBBB", size, rtype, cid, group, logical,
                       logical // 2, 0, 0, eff, 0)
    return head + bytes(size - len(head))


def cpu_set_buffer(spec, **kw):
    """spec: iterable of (logical, efficiency_class[, group])."""
    return b"".join(cpu_set_record(*entry, **kw) for entry in spec)


# The reference host: 285K, 8 P-cores interleaved with 16 E-cores, one group.
P285K = [0, 1, 10, 11, 12, 13, 22, 23]
two_class = cpu_set_buffer([(c, 1 if c in P285K else 0) for c in range(24)])

# 24 -- two classes: the top class is the P-core set, exactly as before.
n += 1
r = ca.classify_cpu_sets(two_class)
check(n, "two-class host: top class is the P-core set",
      r.fast == P285K and r.slow == [c for c in range(24) if c not in P285K]
      and r.groups == {0} and r.reason == "", f"fast={r.fast} reason={r.reason!r}")

# 25 -- three classes (LP-E = 0, E = 1, P = 2): ONLY class 2 is fast; the
#       E-cores in the middle class must not be pinned as P-cores.
n += 1
three_class = cpu_set_buffer([(c, 2) for c in range(0, 12)]        # 6 P-cores x2 threads
                             + [(c, 1) for c in range(12, 20)]     # 8 E-cores
                             + [(c, 0) for c in range(20, 22)])    # 2 LP E-cores
r = ca.classify_cpu_sets(three_class)
check(n, "three-class host: only the top class is fast, both lower classes are slow",
      r.fast == list(range(12)) and r.slow == list(range(12, 22))
      and sorted(r.by_class) == [0, 1, 2], f"fast={r.fast} slow={r.slow}")

# 26 -- the middle class is never in `fast` even when it is the largest class.
n += 1
check(n, "E-cores (middle class) excluded from the P-core set",
      not (set(range(12, 20)) & set(r.fast)), str(r.fast))

# 27 -- more than one processor group: indices repeat per group and a 64-bit
#       mask cannot address the machine, so pinning is refused with a reason.
n += 1
multi_group = cpu_set_buffer([(c, 1 if c < 8 else 0, 0) for c in range(64)]
                             + [(c, 1 if c < 8 else 0, 1) for c in range(40)])
r = ca.classify_cpu_sets(multi_group)
check(n, "multiple processor groups disable pinning",
      r.fast == [] and r.slow == [] and r.groups == {0, 1}
      and "processor groups" in r.reason and r.ids == {},
      f"fast={r.fast} reason={r.reason!r}")

# 28 -- the log line names the groups so the host is recognisable.
n += 1
line = ca.describe_classes(r)
check(n, "describe_classes names every class and the group count",
      "class 0:" in line and "class 1:" in line and "processor groups [0, 1]" in line
      and "no pinning" in line, line)

# 29 -- one class: not hybrid, nothing to prefer.
n += 1
r = ca.classify_cpu_sets(cpu_set_buffer([(c, 0) for c in range(16)]))
check(n, "single class: not hybrid, empty P-core set with reason",
      r.fast == [] and r.slow == [] and "not hybrid" in r.reason, r.reason)

# 30 -- the process affinity mask prunes P-cores the process may not use, and
#       an empty intersection refuses rather than pins partially.
n += 1
mask = sum(1 << c for c in range(24) if c not in (0, 1))
r = ca.classify_cpu_sets(two_class, process_mask=mask)
r2 = ca.classify_cpu_sets(two_class, process_mask=(1 << 5))
check(n, "process mask prunes the P-core set; empty intersection refuses",
      r.fast == [10, 11, 12, 13, 22, 23] and r2.fast == []
      and "process mask" in r2.reason, f"{r.fast} / {r2.reason!r}")

# 31 -- records are walked by their own Size field (a newer Windows may grow
#       the struct) and records of another Type are skipped.
n += 1
grown = cpu_set_buffer([(c, 1 if c in P285K else 0) for c in range(24)], size=40)
foreign = cpu_set_record(99, 7, rtype=1)
r = ca.classify_cpu_sets(foreign + grown)
check(n, "variable record size honoured, non-CPU-set records skipped",
      r.fast == P285K and 99 not in r.slow, f"fast={r.fast}")

# 32 -- CPU Set ids are kept per logical CPU (SetThreadSelectedCpuSets takes
#       ids, not indices) and a truncated buffer does not raise.
n += 1
r = ca.classify_cpu_sets(two_class)
truncated = ca.classify_cpu_sets(two_class[:50])
check(n, "CPU Set ids mapped per logical CPU; truncated buffer parses what it can",
      r.ids[10] == 266 and len(r.ids) == 24 and len(truncated.by_class) >= 1,
      f"ids[10]={r.ids.get(10)} n={len(r.ids)}")

# 33 -- the live path on this host: enumerates without raising and agrees
#       with its own classification (any Windows host, hybrid or not).
n += 1
live = ca.performance_cores()
classes = ca.cpu_classes()
check(n, "live enumeration is consistent with classify_cpu_sets",
      isinstance(live, list) and live == list(classes.fast)
      and set(live).isdisjoint(ca.efficiency_cores()),
      f"fast={live} classes={ {k: len(v) for k, v in classes.by_class.items()} }")

# ===========================================================================
# Opt-in scheduling knobs: CPU Sets, power-throttling opt-out, MMCSS. These
# have no GUI caller; the contract pinned here is "callable on any host,
# never raises, reports what it did", exercised on a worker thread so the
# test's own main thread is left alone.
# ===========================================================================
import threading  # noqa: E402

knob_out = {}


def _knob_body():
    fast = ca.performance_cores()
    knob_out["ids"] = ca.cpu_set_ids(fast)
    knob_out["ids_match"] = knob_out["ids"] == [ca.cpu_classes().ids[c] for c in fast]
    knob_out["restrict"] = ca.restrict_current_thread_cpu_sets(fast)
    knob_out["clear"] = ca.clear_current_thread_cpu_sets()
    knob_out["unknown"] = ca.restrict_current_thread_cpu_sets([9999])
    knob_out["throttle_off"] = ca.disable_current_thread_power_throttling()
    knob_out["throttle_sys"] = ca.set_current_thread_power_throttling(None)
    h = ca.mmcss_register_current_thread("Capture", ca.AVRT_PRIORITY_HIGH)
    knob_out["mmcss"] = h
    knob_out["revert"] = ca.mmcss_revert(h)
    knob_out["revert_none"] = ca.mmcss_revert(None)


t = threading.Thread(target=_knob_body)
t.start()
t.join(10)

# 34 -- CPU Set ids come from the enumeration, an unknown CPU never pins, and
#       restrict/clear report booleans. On a hybrid Windows host the restrict
#       takes; on a non-hybrid one `fast` is empty, so the same call CLEARS
#       and also returns True -- the assertion must not tie True to hybrid.
#       Clearing (an empty set) is True on any Windows host, False elsewhere.
n += 1
hybrid = bool(ca.performance_cores())
on_windows = sys.platform == "win32"
check(n, "CPU Sets helpers: ids from enumeration, unknown CPU refused, clear works",
      not t.is_alive() and knob_out.get("ids_match") is True
      and knob_out.get("unknown") is False
      and isinstance(knob_out.get("restrict"), bool)
      and (not (hybrid and on_windows) or knob_out.get("restrict") is True)
      and knob_out.get("clear") is on_windows,
      f"hybrid={hybrid} "
      f"{ {k: knob_out.get(k) for k in ('ids', 'restrict', 'clear', 'unknown')} }")

# 35 -- the power-throttling opt-out and its reset both take on Windows.
n += 1
check(n, "power-throttling opt-out and system reset return booleans (True on Windows)",
      isinstance(knob_out.get("throttle_off"), bool)
      and isinstance(knob_out.get("throttle_sys"), bool)
      and (knob_out.get("throttle_off") is True) == (sys.platform == "win32"),
      f"off={knob_out.get('throttle_off')} sys={knob_out.get('throttle_sys')}")

# 36 -- MMCSS registration hands back a handle that revert accepts; reverting
#       nothing is False rather than an error.
n += 1
h = knob_out.get("mmcss")
check(n, "MMCSS register/revert round-trip; revert(None) is False",
      (h is None or (h and knob_out.get("revert") is True))
      and knob_out.get("revert_none") is False,
      f"handle={h} revert={knob_out.get('revert')}")

# ===========================================================================
# Review follow-ups. Appended after the live cases so the numbers above stay
# stable: commit messages and handoff notes cite them.
# ===========================================================================

# 37 -- (A3-13) set_exposure_gain refuses a value in the wrong unit BEFORE
#       writing: a dB profile value on a GainRaw camera, or a raw step count
#       on a dB camera, raises ValueError and the gain node is untouched; a
#       matching unit or None (the node's own unit, the baseline restore)
#       writes as before. The exposure is applied before the gain is checked.
n += 1
raw_cam = StubCamera({"ExposureTime": StubNode(2000.0),
                      "GainRaw": StubNode(36, lo=StubNode(0), hi=StubNode(63))})
db_cam = StubCamera({"Gain": StubNode(0.0, lo=StubNode(0.0), hi=StubNode(24.0))})
refused = {}
for label, cam_, val, unit in (("dB->raw", raw_cam, 6.0, "dB"),
                               ("raw->dB", db_cam, 6, "raw"),
                               ("bogus", db_cam, 6.0, "steps")):
    try:
        B.set_exposure_gain(cam_, 3000.0, val, gain_unit=unit)
        refused[label] = None
    except ValueError as e:
        refused[label] = str(e)
raw_ok = B.set_exposure_gain(raw_cam, None, 40, gain_unit="raw")
raw_none = B.set_exposure_gain(raw_cam, None, 36, gain_unit=None)
db_ok = B.set_exposure_gain(db_cam, None, 6.0, gain_unit="dB")
check(n, "gain_unit: dB value refused on GainRaw, raw refused on Gain, no write on refusal",
      all(refused[k] for k in ("dB->raw", "raw->dB", "bogus"))
      and "GainRaw" in refused["dB->raw"] and "dB" in refused["dB->raw"]
      and raw_cam._nodes["GainRaw"].writes == [40, 36]
      and db_cam._nodes["Gain"].writes == [6.0]
      and raw_cam._nodes["ExposureTime"].writes == [3000.0]
      and (raw_ok[1], raw_none[1], db_ok[1]) == (40, 36, 6.0),
      f"refused={ {k: bool(v) for k, v in refused.items()} } "
      f"raw_writes={raw_cam._nodes['GainRaw'].writes} "
      f"db_writes={db_cam._nodes['Gain'].writes}")

# 38 -- the process affinity mask prunes the E-core set too: a
#       SetThreadAffinityMask with any CPU outside the process mask fails
#       whole, which would leave every encoder thread unpinned. An empty
#       `slow` is not an error (no E-core to confine to, so no pin).
n += 1
ecores = [c for c in range(24) if c not in P285K]
mask_no_low_e = sum(1 << c for c in range(24) if c not in (2, 3, 4))
r = ca.classify_cpu_sets(two_class, process_mask=mask_no_low_e)
mask_p_only = sum(1 << c for c in P285K)
r2 = ca.classify_cpu_sets(two_class, process_mask=mask_p_only)
check(n, "process mask prunes the E-core set; P-only mask leaves slow empty, fast intact",
      r.fast == P285K and r.slow == [c for c in ecores if c not in (2, 3, 4)]
      and r2.fast == P285K and r2.slow == [] and r2.reason == "",
      f"slow={r.slow} / fast2={r2.fast} slow2={r2.slow}")

# 39 -- a failing log line never costs the pin: performance_cores caches the
#       classification BEFORE printing, and a print that raises (closed or
#       full log file under pythonw) leaves the P-core set intact. Runs on
#       any host by substituting the enumeration and the platform flag.
n += 1
saved = (ca._pcores_cache, ca._classes_cache, ca._IS_WINDOWS,
         ca._read_cpu_set_buffer, ca._process_mask, ca.describe_classes)


def _boom(_classes):
    raise OSError(28, "No space left on device")


try:
    ca._pcores_cache = None
    ca._classes_cache = None
    ca._IS_WINDOWS = True
    ca._read_cpu_set_buffer = lambda: two_class
    ca._process_mask = lambda: 0
    ca.describe_classes = _boom
    got = ca.performance_cores()
    got_classes = ca.cpu_classes()
finally:
    (ca._pcores_cache, ca._classes_cache, ca._IS_WINDOWS,
     ca._read_cpu_set_buffer, ca._process_mask, ca.describe_classes) = saved
check(n, "performance_cores keeps the P-core set when the log line fails",
      got == P285K and got_classes.fast == P285K and got_classes.reason == "",
      f"got={got} reason={got_classes.reason!r}")

print()
if failures:
    print(f"{len(failures)} FAILURE(S): " + ", ".join(failures))
    sys.exit(1)
print("ALL CPU AFFINITY / BASLER BACKEND TESTS PASS")
