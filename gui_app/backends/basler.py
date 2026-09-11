"""Basler / pypylon camera backend.

The only module in `gui_app/` that knows what a Basler camera is, apart from the
hot loop in `grab_thread.py` (which uses the native grab-result object directly —
see `gui_app/backends/__init__.py` for why).

Everything here is device configuration and runs a handful of times per session,
so it is written for clarity over speed. Most of it encodes a hard-won fact
about these cameras; the comments are the point, not decoration.
"""
from __future__ import annotations

import pypylon.pylon as pylon


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
        """
        ok = False
        try:
            node = cam.GetNodeMap().GetNode("GevGVSPExtendedIDMode")
            if node is not None:
                node.FromString("On")
                ok = True
        except Exception as e:
            print(f"[cam{i+1}] GevGVSPExtendedIDMode unavailable: {e}", flush=True)
        try:
            node = cam.GetStreamGrabberNodeMap().GetNode("UseExtendedIdIfAvailable")
            if node is not None:
                node.SetValue(True)
                ok = True
        except Exception as e:
            print(f"[cam{i+1}] UseExtendedIdIfAvailable unavailable: {e}", flush=True)
        print(f"[cam{i+1}] extended (64-bit) block IDs: "
              f"{'enabled' if ok else 'UNAVAILABLE — relying on software unwrap'}",
              flush=True)
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

    # ------------------------------------------------------------------- modes
    def set_freerun(self, cam, fps: float = 30.0) -> None:
        """Untriggered preview mode."""
        try:
            cam.StopGrabbing()
        except Exception:
            pass
        cam.TriggerMode.SetValue("Off")
        cam.AcquisitionFrameRateEnable.SetValue(True)
        cam.AcquisitionFrameRate.SetValue(float(fps))

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
    @staticmethod
    def get_exposure_gain(cam) -> tuple:
        """(exposure_us, gain_db) as the .pfs left them, or (None, None).

        Read once at open so the recording settings can be RESTORED exactly
        rather than reconstructed. Node names differ across pylon generations,
        hence the fallbacks.
        """
        exp = gain = None
        for n in ("ExposureTime", "ExposureTimeAbs"):
            try:
                exp = getattr(cam, n).GetValue()
                break
            except Exception:
                continue
        for n in ("Gain", "GainRaw"):
            try:
                gain = getattr(cam, n).GetValue()
                break
            except Exception:
                continue
        return exp, gain

    @staticmethod
    def set_exposure_gain(cam, exposure_us=None, gain_db=None) -> tuple:
        """Apply exposure/gain. Returns what was actually set, for logging.

        The caller is responsible for the exposure CEILING — in trigger mode the
        frame-rate timer starts after exposure ends, so the minimum interval is
        `exposure + 1/AcquisitionFrameRate`, and exceeding the trigger period
        silently halves the frame rate rather than erroring.
        """
        applied_exp = applied_gain = None
        if exposure_us is not None:
            for n in ("ExposureTime", "ExposureTimeAbs"):
                try:
                    node = getattr(cam, n)
                    lo = getattr(node, "Min", None)
                    hi = getattr(node, "Max", None)
                    v = float(exposure_us)
                    if lo is not None and hi is not None:
                        v = max(lo.GetValue() if hasattr(lo, "GetValue") else lo,
                                min(v, hi.GetValue() if hasattr(hi, "GetValue") else hi))
                    node.SetValue(v)
                    applied_exp = node.GetValue()
                    break
                except Exception:
                    continue
        if gain_db is not None:
            for n in ("Gain", "GainRaw"):
                try:
                    node = getattr(cam, n)
                    node.SetValue(float(gain_db))
                    applied_gain = node.GetValue()
                    break
                except Exception:
                    continue
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
        return out
