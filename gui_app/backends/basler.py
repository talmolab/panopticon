"""Basler / pypylon camera backend.

The only module in `gui_app/` that knows what a Basler camera is, apart from the
hot loop in `grab_thread.py` (which uses the native grab-result object directly —
see `gui_app/backends/__init__.py` for why).

Everything here is device configuration and runs a handful of times per session,
so it is written for clarity over speed. Most of it encodes a hard-won fact
about these cameras; the comments are the point, not decoration.
"""
from __future__ import annotations

try:
    import pypylon.genicam as genicam
    import pypylon.pylon as pylon
except ImportError as _e:
    # The SDK is imported exactly here so that a rig without it fails in ONE
    # place with a message that says what to do, instead of a bare
    # ModuleNotFoundError from whichever module happened to load the backend.
    raise ImportError(
        "The 'basler' camera backend needs pypylon, which is not importable "
        f"in this environment ({_e}). Install pypylon into the project "
        "environment, or set the profile's `camera_backend` field to another "
        "backend registered in gui_app/backends/__init__.py.") from _e


class BaslerBackend:
    """`CameraBackend` for Basler cameras via pypylon."""

    name = "basler"

    #: Raised by retrieve() when no frame arrives in time. A timeout is NORMAL
    #: (the triggers stopped, or the stream went quiet) and the grab loop treats
    #: it very differently from a real error, so it must be distinguishable.
    TimeoutException = pylon.TimeoutException

    #: Oldest-first delivery. This is what makes a slow grab loop show up as
    #: increasingly STALE frames rather than dropped ones — the failure mode
    #: that hid a 1.5% per-frame deficit for eleven minutes.
    GRAB_STRATEGY = pylon.GrabStrategy_OneByOne

    # ---------------------------------------------------------------- discovery
    def enumerate_devices(self) -> list:
        """Attached cameras, sorted by serial number.

        The sort is load-bearing: position in this list becomes `cam1`..`camN`,
        and those names are baked into the calibration extrinsics. An unstable
        order would silently attach every extrinsic to the wrong camera.
        """
        devices = pylon.TlFactory.GetInstance().EnumerateDevices()
        return sorted(devices, key=lambda d: d.GetSerialNumber())

    # ------------------------------------------------------------------ opening
    def open(self, device, pfs_path: str, max_num_buffer: int):
        """Open one camera and apply the .pfs. Raises on any failure."""
        cam = pylon.InstantCamera(
            pylon.TlFactory.GetInstance().CreateDevice(device))
        cam.Open()
        # Validation is disabled because the .pfs is often generated on a
        # different camera of the same model; describe() is what actually
        # checks the result.
        pylon.FeaturePersistence.Load(pfs_path, cam.GetNodeMap(), False)
        cam.MaxNumBuffer.SetValue(max_num_buffer)
        return cam

    def describe(self, cam) -> dict:
        """Read geometry and format back FROM THE CAMERA.

        Never trust the profile here. `CLAUDE.md` tells users to edit the .pfs in
        pylon Viewer, where pixel format and ROI are one click apart, and a
        Mono12 .pfs makes every frame uint16 — which the NV12 copy then truncates
        **mod 256 with no error at all**, producing a full-length, perfectly
        aligned, visually shredded recording.
        """
        return {
            "width": cam.Width.GetValue(),
            "height": cam.Height.GetValue(),
            "pixel_format": cam.PixelFormat.GetValue(),
            "serial": cam.GetDeviceInfo().GetSerialNumber(),
        }

    # ------------------------------------------------------------ GigE specifics
    @staticmethod
    def enable_extended_block_ids(i: int, cam) -> bool:
        """Ask for 64-bit GVSP block IDs.

        The 16-bit default wraps at 65535 — about 11 minutes at 100 fps — and a
        wrap once left a 15-minute recording silently disjoint because the
        alignment pass read the wrap as a camera running impossibly far ahead.
        `alignment._unwrap_blockids` is the software fallback and handles it
        either way, so this is an optimisation, not a requirement.

        Two independent settings must BOTH take: the camera-side
        GevGVSPExtendedIDMode makes the camera send 64-bit IDs, and the
        stream-grabber-side UseExtendedIdIfAvailable makes pylon consume them.
        The grabber flag alone changes nothing on the wire, so 'enabled' is
        reported only when both succeeded; otherwise the log names the half
        that failed, and the return value means 'actually negotiated'.
        """
        cam_ok = grabber_ok = False
        try:
            node = cam.GetNodeMap().GetNode("GevGVSPExtendedIDMode")
            if node is not None:
                node.FromString("On")
                cam_ok = True
        except Exception as e:
            print(f"[cam{i+1}] GevGVSPExtendedIDMode unavailable: {e}", flush=True)
        try:
            node = cam.GetStreamGrabberNodeMap().GetNode("UseExtendedIdIfAvailable")
            if node is not None:
                node.SetValue(True)
                grabber_ok = True
        except Exception as e:
            print(f"[cam{i+1}] UseExtendedIdIfAvailable unavailable: {e}", flush=True)
        ok = cam_ok and grabber_ok
        if ok:
            status = "enabled"
        else:
            missing = [name for name, took in
                       (("camera GevGVSPExtendedIDMode", cam_ok),
                        ("grabber UseExtendedIdIfAvailable", grabber_ok))
                       if not took]
            status = ("UNAVAILABLE (" + ", ".join(missing) +
                      " not set) — relying on software unwrap")
        print(f"[cam{i+1}] extended (64-bit) block IDs: {status}", flush=True)
        return ok

    @staticmethod
    def select_gige_driver(i: int, cam, which: str = "socket") -> None:
        """Select the GigE receive driver per the profile's `gige_driver`.

        "socket": user-space — costs more host CPU, but its packet resends
        reliably recover lost packets. This is the proven setting.
        "filter": in-kernel pylon GigE Vision driver — far less CPU, but with
        default resend settings it silently dropped ~23% of frames (~5,800
        single-frame gaps per camera) under 6x100 fps load on 2026-06-12. It
        discards a frame with a lost packet instead of asking for it again.
        "auto": leave pylon's default. No-op for non-GigE cameras.
        """
        sym = {"socket": "SocketDriver", "filter": "WindowsFilterDriver"}.get(which)
        try:
            sg = cam.GetStreamGrabberNodeMap()
            t = sg.GetNode("Type")
            if t is None:
                return
            if sym is not None:
                avail = sg.GetNode(f"TypeIs{sym}Available")
                if avail is None or avail.GetValue():
                    t.FromString(sym)
            extra = ""
            if t.ToString() == "SocketDriver":
                # Max the per-stream socket receive buffer: more slack for the
                # receive thread when the encoders contend for CPU.
                try:
                    sbs = sg.GetNode("SocketBufferSize")
                    sbs_max = sg.GetNode("SocketBufferSize_Max")
                    if sbs is not None and sbs_max is not None:
                        sbs.SetValue(sbs_max.GetValue())
                        extra = f" (SocketBufferSize={sbs.GetValue()} KB)"
                except Exception:
                    pass
            print(f"[cam{i+1}] GigE stream driver: {t.ToString()}{extra}", flush=True)
        except Exception as e:
            print(f"[cam{i+1}] GigE driver selection skipped: {e}", flush=True)

    # ------------------------------------------------------ GigE transport knobs
    # Opt-in per-camera transport settings. Nothing applies them unless a
    # profile field is set: the shipped .pfs values (GevSCFTD 0,
    # BandwidthReserveMode Standard) are the rig-validated baseline and these
    # exist so that baseline can be A/B'd from a profile rather than by
    # editing the .pfs on every camera.

    @classmethod
    def set_transmission_delay(cls, cam, ticks: int) -> int:
        """Write GevSCFTD (frame transmission delay) and return the value set.

        The camera holds each frame back by `ticks` timestamp ticks
        (GevTimestampTickFrequency, 125 MHz on ace GigE, so 1 tick = 8 ns)
        before putting it on the wire. Basler documents this as the knob for
        cameras "triggered simultaneously": staggering the start of each
        camera's burst spreads the switch load without touching the exposure
        or readout timer, which is what the trigger_rate_limit pacing costs.
        The node is GigE-only; a camera without it raises, because a profile
        that sets a delay for a camera that cannot honour it is a config
        error, not something to skip in silence. A write error propagates for
        the same reason.
        """
        node = cls._require_node(cam, "GevSCFTD")
        node.SetValue(int(ticks))
        return int(node.GetValue())

    @classmethod
    def set_bandwidth_reserve(cls, cam, percent=None, accumulation=None) -> dict:
        """Write GevSCBWR (reserve percent) and/or GevSCBWRA (accumulation).

        GevSCBWR is the share of the assigned bandwidth held back for packet
        resends; GevSCBWRA multiplies how many resends can be pooled. Both
        are only writable while BandwidthReserveMode is Manual, so that mode
        is selected first when the camera offers it (the .pfs default is
        Standard). More reserve lowers the assigned bandwidth GevSCBWA, which
        is why this is opt-in per profile rather than a default. None leaves a
        setting alone. Returns the values read back, plus GevSCBWA when
        readable. Missing nodes and write errors raise (see
        set_transmission_delay).
        """
        out = {}
        if percent is None and accumulation is None:
            return out
        mode = cls._optional_node(cam, "BandwidthReserveMode")
        if mode is not None:
            mode.FromString("Manual")
            out["BandwidthReserveMode"] = mode.ToString()
        if percent is not None:
            node = cls._require_node(cam, "GevSCBWR")
            node.SetValue(int(percent))
            out["GevSCBWR"] = int(node.GetValue())
        if accumulation is not None:
            node = cls._require_node(cam, "GevSCBWRA")
            node.SetValue(int(accumulation))
            out["GevSCBWRA"] = int(node.GetValue())
        bwa = cls._optional_node(cam, "GevSCBWA")
        if bwa is not None:
            try:
                out["GevSCBWA"] = bwa.GetValue()
            except Exception:
                pass
        return out

    @staticmethod
    def _optional_node(cam, name):
        """The camera node map's `name`, or None when absent/unimplemented."""
        try:
            node = cam.GetNodeMap().GetNode(name)
        except genicam.LogicalErrorException:
            return None
        if node is None or not genicam.IsImplemented(node):
            return None
        return node

    @classmethod
    def _require_node(cls, cam, name):
        """Like _optional_node, but a missing node is a configuration error."""
        node = cls._optional_node(cam, name)
        if node is None:
            raise RuntimeError(
                f"{name} is not available on this camera (a GigE Vision "
                f"transport feature); remove the profile setting that asks "
                f"for it or use a camera that implements it")
        return node

    # ------------------------------------------------------------------- modes
    #: Trigger selectors other than FrameStart that a .pfs may have armed.
    #: Each is switched Off when the camera offers it, because any armed
    #: trigger gates free-run just as FrameStart does.
    OTHER_TRIGGER_SELECTORS = ("AcquisitionStart", "FrameBurstStart")

    def set_freerun(self, cam, fps: float = 30.0) -> None:
        """Untriggered preview mode.

        TriggerMode is a per-selector value, so the selector is set to
        FrameStart BEFORE TriggerMode is written (mirroring set_triggered).
        Writing TriggerMode Off against whatever selector the .pfs left active
        would leave FrameStart armed on a .pfs saved with another selector, and
        the preview would then wait for a trigger that never comes: no frames,
        no error. The other selectors the camera offers are disarmed as well,
        best effort, and the selector is left on FrameStart.
        """
        try:
            cam.StopGrabbing()
        except Exception:
            pass
        cam.TriggerSelector.SetValue("FrameStart")
        cam.TriggerMode.SetValue("Off")
        for sel in self._other_trigger_selectors(cam):
            try:
                cam.TriggerSelector.SetValue(sel)
                cam.TriggerMode.SetValue("Off")
            except Exception as e:
                print(f"[cam] trigger selector {sel} could not be disarmed: {e}",
                      flush=True)
        cam.TriggerSelector.SetValue("FrameStart")
        cam.AcquisitionFrameRateEnable.SetValue(True)
        cam.AcquisitionFrameRate.SetValue(float(fps))

    @classmethod
    def _other_trigger_selectors(cls, cam) -> list:
        """OTHER_TRIGGER_SELECTORS entries this camera's TriggerSelector offers.

        The enumeration's symbolic list is consulted so a selector the camera
        lacks is never written (which would raise). A camera that does not
        publish the list yields nothing, keeping the FrameStart path the only
        one that can fail.
        """
        try:
            node = cam.GetNodeMap().GetNode("TriggerSelector")
            if node is None:
                return []
            syms = getattr(node, "Symbolics", None)
            if syms is None and hasattr(node, "GetSymbolics"):
                syms = node.GetSymbolics()
            offered = set(syms or ())
        except Exception:
            return []
        return [s for s in cls.OTHER_TRIGGER_SELECTORS if s in offered]

    def set_triggered(self, cam, rate_limit: float = 165.0,
                      announce: bool = False) -> None:
        """Hardware-trigger mode on Line1, rising edge.

        `rate_limit` is the camera's internal rate generator. It does nothing
        useful while externally triggered, but it still enforces a minimum
        interval of `exposure + 1/rate_limit` — which is what caps usable
        exposure at ~3.94 ms at 165, and what caused the original 50 fps bug
        when it was left at 100 (every second trigger was skipped).

        Setting it to 0 removes that ceiling and was tried on 2026-08-11: it
        cost 8-15% of frames IN TRANSMISSION, because the limiter also paces
        readout and without it every camera bursts onto the link at once.
        Reverted the same day. Keep it above the trigger rate.

        `announce` is set for camera 0 only: the disabled-limiter warning is a
        property of the rig, so it is printed once rather than once per camera.
        """
        try:
            cam.StopGrabbing()
        except Exception:
            pass
        cam.TriggerSelector.SetValue("FrameStart")
        cam.TriggerMode.SetValue("On")
        cam.TriggerSource.SetValue("Line1")
        cam.TriggerActivation.SetValue("RisingEdge")
        if rate_limit and rate_limit > 0:
            cam.AcquisitionFrameRateEnable.SetValue(True)
            cam.AcquisitionFrameRate.SetValue(float(rate_limit))
        else:
            cam.AcquisitionFrameRateEnable.SetValue(False)
            if announce:
                print("[cam] trigger-rate limiter DISABLED "
                      "(exposure bounded by sensor readout only)", flush=True)

    # ------------------------------------------------------------ exposure/gain
    #: Candidate node names per control, newest SFNC spelling first. The
    #: names differ across pylon generations, so the first IMPLEMENTED one wins.
    EXPOSURE_NODES = ("ExposureTime", "ExposureTimeAbs")
    GAIN_NODES = ("Gain", "GainRaw")
    #: Unit of each gain node. `Gain` is a float in dB (SFNC 2, ace2);
    #: `GainRaw` is an INTEGER in sensor steps whose dB size is model-specific,
    #: so the two are not interchangeable and no conversion is attempted here.
    GAIN_UNITS = {"Gain": "dB", "GainRaw": "raw"}

    @classmethod
    def gain_unit(cls, cam):
        """'dB', 'raw', or None when the camera has no gain control.

        The caller decides what a dB-denominated profile value means on a
        'raw' camera (refuse, or convert with the model's step); the baseline
        restore needs no decision because it reads and writes the same node.
        """
        name, _ = cls._find_node(cam, cls.GAIN_NODES)
        return cls.GAIN_UNITS.get(name)

    @classmethod
    def _find_node(cls, cam, names):
        """(name, node) of the first implemented candidate, else (None, None).

        Presence is probed here and ONLY here, so that a failure to write a node
        that exists is never mistaken for the node being absent. The two mean
        opposite things: an absent node is a camera without that control and
        the caller gets None; a write that fails on a present node is a real
        error (out of range, camera busy, access denied) and must propagate,
        because a swallowed one lets a camera record at the wrong exposure with
        nothing in the log.
        """
        for n in names:
            node = cls._optional_node(cam, n)
            if node is not None:
                return n, node
        return None, None

    @staticmethod
    def _clamp_to_node(node, v):
        """Clamp v into the node's [Min, Max] when the node publishes them."""
        lo = getattr(node, "Min", None)
        hi = getattr(node, "Max", None)
        if lo is None or hi is None:
            return v
        lo = lo.GetValue() if hasattr(lo, "GetValue") else lo
        hi = hi.GetValue() if hasattr(hi, "GetValue") else hi
        return max(lo, min(v, hi))

    @classmethod
    def get_exposure_gain(cls, cam) -> tuple:
        """(exposure_us, gain) as the .pfs left them, or None per missing control.

        Read once at open so the recording settings can be RESTORED exactly
        rather than reconstructed. A read failure on a present node propagates
        for the same reason a write failure does (see _find_node).
        """
        exp = gain = None
        _, node = cls._find_node(cam, cls.EXPOSURE_NODES)
        if node is not None:
            exp = node.GetValue()
        _, node = cls._find_node(cam, cls.GAIN_NODES)
        if node is not None:
            gain = node.GetValue()
        return exp, gain

    @classmethod
    def set_exposure_gain(cls, cam, exposure_us=None, gain_db=None) -> tuple:
        """Apply exposure/gain. Returns what was actually set, for logging.

        A control the camera does not implement is skipped and reported as
        None. A control it does implement is written exactly once, and any
        error from that write PROPAGATES: the caller logs it per camera, and a
        calibration exposure left on one camera would otherwise halve that
        camera's frame rate in the next recording with no trace in the log.

        The caller is responsible for the exposure CEILING: in trigger mode the
        frame-rate timer starts after exposure ends, so the minimum interval is
        `exposure + 1/AcquisitionFrameRate`, and exceeding the trigger period
        silently halves the frame rate rather than erroring.

        Gain is written in the UNIT OF THE NODE FOUND (see gain_unit): a float
        in dB to `Gain`, an integer step count to `GainRaw`, clamped into the
        node's range. Restoring the baseline read by get_exposure_gain is
        therefore exact on either kind of camera. A dB value from the profile
        is only meaningful on a 'dB' camera; the caller checks gain_unit()
        before applying one, because this function cannot tell the two
        sources apart.
        """
        applied_exp = applied_gain = None
        if exposure_us is not None:
            _, node = cls._find_node(cam, cls.EXPOSURE_NODES)
            if node is not None:
                node.SetValue(cls._clamp_to_node(node, float(exposure_us)))
                applied_exp = node.GetValue()
        if gain_db is not None:
            name, node = cls._find_node(cam, cls.GAIN_NODES)
            if node is not None:
                if cls.GAIN_UNITS.get(name) == "raw":
                    v = int(round(float(gain_db)))
                else:
                    v = float(gain_db)
                node.SetValue(cls._clamp_to_node(node, v))
                applied_gain = node.GetValue()
        return applied_exp, applied_gain

    # ----------------------------------------------------------------- grabbing
    def start_grabbing(self, cam) -> None:
        cam.StartGrabbing(self.GRAB_STRATEGY)

    def stop_grabbing(self, cam) -> None:
        cam.StopGrabbing()

    def is_grabbing(self, cam) -> bool:
        return cam.IsGrabbing()

    def retrieve(self, cam, timeout_ms: int):
        """Next frame, or raise TimeoutException. Returns a native grab result
        satisfying `GrabResultProtocol` — pypylon's own object already does."""
        return cam.RetrieveResult(timeout_ms, pylon.TimeoutHandling_ThrowException)

    def close(self, cam) -> None:
        cam.Close()

    # -------------------------------------------------------------- diagnostics
    @staticmethod
    def thermals(cam) -> dict:
        """Core-board temperature, its high-water mark, and the thresholds.

        Worth recording for the same reason as the GPU driver version: it moves
        underneath a working rig and is undiagnosable afterwards. These cameras
        have no fan and cool by conduction through the mount, so temperature is
        a property of the INSTALLATION, not the camera — two identical cameras
        differ by tens of degrees depending on bracket material, airflow and
        what is mounted next to them. Nothing read this until 2026-09-10, when
        three cameras turned out to be sitting above the 76 C `Critical`
        threshold with no record of whether that was new.

        `DeviceTemperature` decays after a session ends, so `BslTemperatureMax`
        is the number worth keeping. All keys are optional: a camera that does
        not expose them returns what it has rather than raising.
        """
        out = {}
        for key, node in (("temp_c", "DeviceTemperature"),
                          ("temp_max_c", "BslTemperatureMax"),
                          ("temp_status", "BslTemperatureStatus"),
                          ("temp_critical_c", "BsliCriticalTemperature"),
                          ("temp_shutdown_c", "BsliOverTemperature"),
                          ("temp_error_count", "BslTemperatureStatusErrorCount")):
            try:
                out[key] = getattr(cam, node).GetValue()
            except Exception:
                pass
        return out

    def stream_stats(self, cam) -> dict:
        """Per-stream counters, read at stop before StopGrabbing resets them.

        These are what separate *host* starvation from *network* loss, which is
        the distinction every capture problem here has eventually reduced to:
          Buffer_Underrun_Count  — the pool ran dry: the HOST could not keep up.
          Failed_Buffer_Count    — a frame was given up on (resends exhausted).
          Resend_Request_Count   — packets lost and asked for again. A high
                                   count with Failed_Buffer_Count near zero is
                                   NOT harmless: the resend arrives after the
                                   rest of the buffer, so that camera completes
                                   late, and a camera completing late every few
                                   frames IS per-camera drift. On 2026-09-11 the
                                   cameras behind a switch with flow control
                                   disabled ran 16,800 resends per 90 s against
                                   10 for their siblings and were the laggards,
                                   having lost no frames at all. Treat a count
                                   three orders of magnitude above the other
                                   cameras as a fault; check switch flow
                                   control first.
        Statistic_Failed_Packet_Count is NOT included: it reads absurd values on
        this hardware (tens of millions against 11 M total) and is untrustworthy.

        The camera-side transport settings are read alongside, each only when
        the camera implements it, so a session's network state is on record:
          GevSCFJM   — frame jitter max: the read-only bound on how late a
                       frame can START because of a resend burst, i.e. the
                       camera's own estimate of resend-induced lateness.
          GevSCFTD, GevSCBWR, GevSCBWRA, GevSCBWA — the transmission delay and
                       bandwidth reserve knobs and the assigned bandwidth they
                       produce (see set_transmission_delay / set_bandwidth_reserve).
        """
        out = {}
        try:
            sg = cam.GetStreamGrabberNodeMap()
            for key in ("Statistic_Total_Buffer_Count",
                        "Statistic_Failed_Buffer_Count",
                        "Statistic_Buffer_Underrun_Count",
                        "Statistic_Total_Packet_Count",
                        "Statistic_Resend_Request_Count",
                        "Statistic_Resend_Packet_Count"):
                node = sg.GetNode(key)
                if node is not None:
                    out[key.replace("Statistic_", "")] = node.GetValue()
        except Exception as e:
            out["error"] = str(e)
        for key in self.TRANSPORT_NODES:
            try:
                node = self._optional_node(cam, key)
                if node is not None:
                    out[key] = node.GetValue()
            except Exception as e:
                out[key] = f"error: {e}"
        return out

    #: Camera-side GigE transport nodes reported by stream_stats.
    TRANSPORT_NODES = ("GevSCFJM", "GevSCFTD", "GevSCBWR", "GevSCBWRA", "GevSCBWA")


#: Module-level spellings of the transport knobs, for callers that hold the
#: module rather than a backend instance (probes, experiments).
set_transmission_delay = BaslerBackend.set_transmission_delay
set_bandwidth_reserve = BaslerBackend.set_bandwidth_reserve
