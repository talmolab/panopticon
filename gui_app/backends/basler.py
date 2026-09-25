"""Basler / pypylon camera backend.

The only module in `gui_app/` that knows what a Basler camera is, apart from the
hot loop in `grab_thread.py` (which uses the native grab-result object directly —
see `gui_app/backends/__init__.py` for why).

Everything here is device configuration and runs a handful of times per session,
so it is written for clarity over speed. Most of it encodes a hard-won fact
about these cameras; the comments are the point, not decoration.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from gui_app import logging_setup

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
    def open(self, device, pfs_path: str, max_num_buffer: int,
             camera_spec=None):
        """Open one camera and apply the .pfs. Raises on any failure.

        A `camera_spec` (the profile's `camera:` block) is refused before the
        camera is touched. A Basler camera takes every setting from the .pfs,
        and a value that could live in two places drifts between them. The
        profile loader refuses the block first; this is the backend's own
        check for a caller that bypassed it.

        The camera's timestamp tick rate is logged, because the capture path
        reads `TimeStamp` as nanoseconds (see `acquisition_warnings`).
        """
        if camera_spec is not None:
            raise ValueError(
                "the basler backend takes its camera settings from the .pfs "
                "named by pfs_path and does not accept a camera: block. Remove "
                "the camera: block from the profile, or set camera_backend to "
                "a backend that uses it.")
        cam = pylon.InstantCamera(
            pylon.TlFactory.GetInstance().CreateDevice(device))
        cam.Open()
        # Validation is disabled because the .pfs is often generated on a
        # different camera of the same model; describe() is what actually
        # checks the result.
        pylon.FeaturePersistence.Load(pfs_path, cam.GetNodeMap(), False)
        cam.MaxNumBuffer.SetValue(max_num_buffer)
        serial = device.GetSerialNumber()
        self._log_timestamp_clock(cam, serial)
        if logging_setup.verbose():
            self._log_pfs_readback(cam, pfs_path, serial)
            self._log_readback(cam, serial, "open", (
                ("MaxNumBuffer", max_num_buffer, self._read_attr(
                    cam, "MaxNumBuffer")),))
        return cam

    def describe(self, cam) -> dict:
        """Read geometry and format back FROM THE CAMERA.

        Never trust the profile here. Users edit the .pfs in pylon Viewer
        (INSTALLATION.md step 6), where pixel format and ROI are one click
        apart, and a Mono12 .pfs makes every frame uint16 — which the NV12
        copy then truncates **mod 256 with no error at all**, producing a
        full-length, perfectly aligned, visually shredded recording.
        """
        out = {
            "width": cam.Width.GetValue(),
            "height": cam.Height.GetValue(),
            "pixel_format": cam.PixelFormat.GetValue(),
            "serial": cam.GetDeviceInfo().GetSerialNumber(),
        }
        out.update(self._device_facts(cam))
        return out

    #: Device classes pylon reports, as the session header words them.
    INTERFACES = {"BaslerGigE": "GigE", "BaslerUsb": "USB3",
                  "BaslerCamEmu": "emulated"}

    @classmethod
    def _device_facts(cls, cam) -> dict:
        """Model, firmware, interface and link speed for the session header,
        each only when the camera reports it. Never raises: a fact the
        camera does not give is left out, and the header says so."""
        out = {}
        try:
            info = cam.GetDeviceInfo()
        except Exception:
            info = None
        for key, getter in (("model", "GetModelName"),
                            ("interface", "GetDeviceClass")):
            try:
                value = str(getattr(info, getter)())
            except Exception:
                continue
            if value:
                out[key] = (cls.INTERFACES.get(value, value)
                            if key == "interface" else value)
        firmware = cls._read(cam, "DeviceFirmwareVersion")
        if not cls._unread(firmware):
            out["firmware"] = firmware
        elif info is not None:
            try:
                version = str(info.GetDeviceVersion())
                if version:
                    out["firmware"] = f"device version {version}"
            except Exception:
                pass
        for node, unit in cls.LINK_SPEED_NODES:
            value = cls._read(cam, node)
            if cls._unread(value):
                continue
            if unit == "B/s":
                try:
                    value = f"{float(value) * 8 / 1e6:g} Mb/s"
                except ValueError:
                    pass
            elif unit:
                value = f"{value} {unit}"
            out["link_speed"] = f"{value} ({node})"
            break
        return out

    #: Nodes that report a camera's link speed, in the order tried, with the
    #: unit each is in: SFNC's DeviceLinkSpeed (bytes/s), the GigE Vision
    #: GevLinkSpeed (Mb/s), and the USB3 speed mode (a word).
    LINK_SPEED_NODES = (("DeviceLinkSpeed", "B/s"), ("GevLinkSpeed", "Mb/s"),
                        ("BslUSBSpeedMode", ""))

    # ------------------------------------------------------ read-back logging
    # At verbose and above, every setting written at open and at each mode
    # change is logged with the value requested and the value the camera
    # reads back afterwards. These run on the thread configuring the camera
    # (open, and the start and preview-restore paths), never in the grab
    # loop.

    @staticmethod
    def _unread(text) -> bool:
        """Whether `text` is _read's word for a node it could not read. A
        value that is not text (a caller's own int read-back) never is."""
        return isinstance(text, str) and (text == "absent"
                                          or text.startswith("unreadable"))

    @staticmethod
    def _read(cam, name: str, grabber: bool = False) -> str:
        """A node's current value as text, "absent" when the camera has no
        such node, or "unreadable (...)"; never raises."""
        try:
            nodemap = (cam.GetStreamGrabberNodeMap() if grabber
                       else cam.GetNodeMap())
            node = nodemap.GetNode(name)
            if node is None:
                return "absent"
            if hasattr(node, "ToString"):
                return str(node.ToString())
            return str(node.GetValue())
        except Exception as e:
            return f"unreadable ({type(e).__name__})"

    @staticmethod
    def _read_attr(cam, name: str) -> str:
        """Like _read, for an InstantCamera parameter (MaxNumBuffer) that is
        an attribute of the camera object rather than a node-map node."""
        try:
            return str(getattr(cam, name).GetValue())
        except Exception as e:
            return f"unreadable ({type(e).__name__})"

    @staticmethod
    def _same(want, got) -> bool:
        """Whether a read-back matches the value written: as numbers when
        both are numbers (3000.0 and 3000 match, and 1 and True), else as
        text."""
        words = {"true": 1.0, "false": 0.0}

        def num(v):
            if isinstance(v, bool):
                return float(v)
            text = str(v).strip().lower()
            if text in words:
                return words[text]
            return float(text)
        try:
            a, b = num(want), num(got)
            return abs(a - b) <= max(1e-6, 1e-4 * abs(a))
        except (TypeError, ValueError):
            return str(want).strip() == str(got).strip()

    @classmethod
    def _log_readback(cls, cam, serial, phase: str, rows) -> None:
        """One line per phase: 'name requested -> read back' for each
        (name, requested, read back) in `rows`, with the ones that differ
        marked.

        RULE: never raises. REASON: it runs inside the open and mode-change
        writes at the default level, and a line that cannot be formatted
        must cost the line, not the camera's configuration.
        """
        try:
            parts = []
            for name, want, got in rows:
                if cls._unread(got):
                    # An absent node, or a read that failed: not a mismatch.
                    mark = " (not read back)"
                else:
                    mark = "" if cls._same(want, got) else " (differs)"
                parts.append(f"{name} {want} -> {got}{mark}")
            if parts:
                print(f"[basler] {serial} {phase} read-back: "
                      + ", ".join(parts), flush=True)
        except Exception as e:
            print(f"[basler] {serial} {phase} read-back could not be logged: "
                  f"{type(e).__name__}: {e}", flush=True)

    @staticmethod
    def _serial(cam) -> str:
        try:
            return str(cam.GetDeviceInfo().GetSerialNumber())
        except Exception:
            return "?"

    #: A selector qualifier on a GenApi 3.5 .pfs line: "{TriggerSelector=
    #: FrameStart}".
    _QUALIFIER = re.compile(r"\{\s*([^=}]+?)\s*=\s*([^}]*?)\s*\}")

    @staticmethod
    def _is_selector(name: str) -> bool:
        """A GenICam selector, by the SFNC name every selector has
        (TriggerSelector, LineSelector, GainSelector)."""
        return name.endswith("Selector")

    @classmethod
    def pfs_features(cls, pfs_path) -> tuple:
        """What the camera holds after loading the .pfs, as far as the file
        says: ([(name, value)] to read back, in file order; the number of
        writes not read back because they were made under another selector
        value). Returns None for a file that cannot be read.

        A .pfs writes a selector-dependent feature once per selector value,
        either on qualified lines (GenApi 3.5: name, "{Selector=Value}",
        value) or in sequential sections (GenApi 3.1: a "Selector Value"
        line, the feature's line, the next selector value), and writes the
        selector back after each feature. After the load each selector holds
        the last value the file gives it, and a dependent feature reads its
        value for that selector value only.

        RULE: a write is read back only when every selector it was made
        under is at its final value; the others are counted, not compared.
        REASON: comparing an earlier section's write (TriggerMode for
        FrameBurstStart) with what the camera reads under the final selector
        value (FrameStart) marks a setting that landed as "(differs)", dozens
        of times per camera on a 3.1 file, which hides a real one. Each
        selector is read back at its final value, and a feature written
        twice under the final values at its last write.

        RULE: a selector whose last write in the file is a qualifier with
        another value than its last plain line has no known final value,
        and nothing written under it is read back. REASON: a 3.5 file can
        end a group with qualified lines and no plain line after them
        (LineSource, which only the output lines have), and the camera then
        holds either value, depending on whether the loader puts a selector
        back after a qualified write.
        """
        try:
            text = Path(pfs_path).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return None
        # (name, value, {selector: value it was written under}), in order.
        writes = []
        plain: dict = {}    # selector -> (line number, value) of its last line
        qualified: dict = {}  # selector -> (line number, value), last qualifier
        above: dict = {}    # the selector lines directly above this line
        for i, line in enumerate(text.splitlines()):
            if not line.strip() or line.startswith("#"):
                continue
            fields = [f.strip() for f in line.split("\t")]
            if len(fields) >= 3 and fields[1].startswith("{"):
                # A qualifier that does not parse names no selector the
                # file sets, so its write is counted, never compared.
                under = dict(cls._QUALIFIER.findall(fields[1]))
                for sel, val in under.items():
                    qualified[sel] = (i, val)
                writes.append((fields[0], fields[2], under or {"?": "?"}))
                above = {}
            elif len(fields) >= 2:
                name, value = fields[0], fields[1]
                if cls._is_selector(name):
                    plain[name] = (i, value)
                    above[name] = value
                    writes.append((name, value, None))
                    continue
                writes.append((name, value, dict(above)))
                above = {}
        final = {}
        for sel, (i, value) in plain.items():
            q = qualified.get(sel)
            if q is None or q[0] < i or q[1] == value:
                final[sel] = value
        held: dict = {}
        scoped = 0
        for name, value, under in writes:
            if under is None:
                if name in final:
                    held[name] = final[name]
                else:
                    scoped += 1
            elif all(final.get(s) == v for s, v in under.items()):
                held[name] = value
            else:
                scoped += 1
        return list(held.items()), scoped

    @classmethod
    def _log_pfs_readback(cls, cam, pfs_path, serial) -> None:
        """At verbose, the .pfs features read back after the load: a count,
        and a line for each feature the camera holds at another value; at
        debug, a line for every feature."""
        features = cls.pfs_features(pfs_path)
        if features is None:
            print(f"[basler] {serial} .pfs read-back skipped: {pfs_path} "
                  f"could not be read here", flush=True)
            return
        held, scoped = features
        differ = same = unread = 0
        for name, want in held:
            got = cls._read(cam, name)
            if cls._unread(got):
                unread += 1
                ok = None
            else:
                ok = cls._same(want, got)
                same += bool(ok)
                differ += not ok
            if ok is False or logging_setup.debug():
                verdict = ("" if ok else " (differs)" if ok is False
                           else " (not read back)")
                print(f"[basler] {serial} .pfs {name}: file {want}, camera "
                      f"{got}{verdict}", flush=True)
        print(f"[basler] {serial} .pfs read-back of {Path(pfs_path).name}: "
              f"{same} features as written, {differ} differ, {unread} not "
              f"readable; writes under another selector value, not read "
              f"back: {scoped}", flush=True)

    # ---------------------------------------------------------- timestamp clock
    #: The tick rate the capture path assumes: `GrabResultProtocol.TimeStamp`
    #: is nanoseconds.
    TIMESTAMP_HZ = 1_000_000_000
    #: Node that reports a GigE camera's timestamp tick rate. USB3 Vision
    #: cameras do not have it, and their timestamps are nanoseconds by the
    #: USB3 Vision standard.
    TICK_FREQUENCY_NODE = "GevTimestampTickFrequency"

    @classmethod
    def timestamp_tick_hz(cls, cam):
        """The camera's timestamp ticks per second, or None when it does not
        report a rate (USB3 cameras) or the node cannot be read."""
        try:
            node = cls._optional_node(cam, cls.TICK_FREQUENCY_NODE)
            if node is None:
                return None
            return int(node.GetValue())
        except Exception:
            return None

    @classmethod
    def _timestamp_unit_problem(cls, hz):
        """A sentence saying why a clock of `hz` ticks per second is not the
        nanosecond clock the capture path assumes, or None when it is (or
        when the camera does not report a rate)."""
        if hz is None or hz <= 0 or hz == cls.TIMESTAMP_HZ:
            return None
        return (f"the camera's device clock ticks at {hz} Hz "
                f"({cls.TICK_FREQUENCY_NODE}), but the capture path reads "
                f"TimeStamp as nanoseconds ({cls.TIMESTAMP_HZ} Hz). The "
                f"delivery lag, the stall resync and the block-ID rate check "
                f"are wrong for this camera by a factor of "
                f"{cls.TIMESTAMP_HZ / hz:g}")

    @classmethod
    def _log_timestamp_clock(cls, cam, serial) -> None:
        """Print this camera's timestamp tick rate, with a WARNING when it is
        not the nanosecond clock the capture path assumes."""
        hz = cls.timestamp_tick_hz(cam)
        problem = cls._timestamp_unit_problem(hz)
        if problem is not None:
            print(f"[basler] WARNING {serial}: {problem}", flush=True)
            return
        rate = ("not reported" if hz is None
                else f"{hz} Hz ({cls.TICK_FREQUENCY_NODE})")
        print(f"[basler] {serial}: timestamp clock {rate}", flush=True)

    def acquisition_warnings(self, cam, frames_acquired: int) -> list:
        """Camera-side problems with the acquisition that just ended.

        A Basler camera has no trigger counter in use here, so the only
        problem reported is a timestamp clock that is not nanoseconds. It is
        reported for every recording, so it reaches that recording's
        WARNINGS.txt. `frames_acquired` is part of the contract and unused
        here. Never raises.
        """
        try:
            problem = self._timestamp_unit_problem(self.timestamp_tick_hz(cam))
        except Exception:
            return []
        return [] if problem is None else [problem]

    # -------------------------------------------------------- exposure ceiling
    @staticmethod
    def exposure_ceiling_us(cam, fps: float, rate_limit: float) -> float:
        """Longest exposure, in us, at which the camera acquires every trigger.

        In trigger mode the camera's frame-rate timer starts after exposure
        ends, so the shortest interval between acquisitions is
        `exposure + 1/AcquisitionFrameRate`, and `set_triggered` sets
        AcquisitionFrameRate to `rate_limit`. The ceiling is therefore
        `1e6/fps - 1e6/rate_limit`. A trigger arriving inside the interval is
        ignored, which halves the frame rate with no error.

        `rate_limit <= 0` means the limiter is disabled, so the bound left is
        the trigger period `1e6/fps`. The result is 0 or negative when `fps`
        is at or above `rate_limit`, and is returned as is so the caller can
        report it. The caller applies its own 0.9 margin. The formula is the
        limiter's physics, so `cam` is not consulted.
        """
        fps = float(fps)
        limit = float(rate_limit or 0.0)
        if limit > 0:
            return 1e6 / fps - 1e6 / limit
        return 1e6 / fps

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
        if logging_setup.verbose():
            BaslerBackend._log_readback(
                cam, BaslerBackend._serial(cam), "block IDs", (
                    ("GevGVSPExtendedIDMode", "On", BaslerBackend._read(
                        cam, "GevGVSPExtendedIDMode")),
                    ("UseExtendedIdIfAvailable", True, BaslerBackend._read(
                        cam, "UseExtendedIdIfAvailable", grabber=True))))
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
        single-frame gaps per camera) under 6x100 fps load. It
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
            rows = []
            if sym is not None:
                rows.append(("Type", sym, t.ToString()))
            if t.ToString() == "SocketDriver":
                # Max the per-stream socket receive buffer: more slack for the
                # receive thread when the encoders contend for CPU.
                try:
                    sbs = sg.GetNode("SocketBufferSize")
                    sbs_max = sg.GetNode("SocketBufferSize_Max")
                    if sbs is not None and sbs_max is not None:
                        want = sbs_max.GetValue()
                        sbs.SetValue(want)
                        extra = f" (SocketBufferSize={sbs.GetValue()} KB)"
                        rows.append(("SocketBufferSize", want, sbs.GetValue()))
                except Exception:
                    pass
            print(f"[cam{i+1}] GigE stream driver: {t.ToString()}{extra}", flush=True)
            if logging_setup.verbose():
                BaslerBackend._log_readback(
                    cam, BaslerBackend._serial(cam), "stream driver", rows)
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

        The camera holds each frame back by `ticks` ticks of its timestamp
        clock before putting it on the wire. The tick rate is the camera's
        GevTimestampTickFrequency (see `timestamp_tick_hz`); it differs
        between camera models, so convert a delay in time with the rate the
        camera reports rather than an assumed one.

        Basler documents this as the knob for
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
        rows = []
        mode = cls._optional_node(cam, "BandwidthReserveMode")
        if mode is not None:
            mode.FromString("Manual")
            out["BandwidthReserveMode"] = mode.ToString()
            rows.append(("BandwidthReserveMode", "Manual", mode.ToString()))
        if percent is not None:
            node = cls._require_node(cam, "GevSCBWR")
            node.SetValue(int(percent))
            out["GevSCBWR"] = int(node.GetValue())
            rows.append(("GevSCBWR", int(percent), out["GevSCBWR"]))
        if accumulation is not None:
            node = cls._require_node(cam, "GevSCBWRA")
            node.SetValue(int(accumulation))
            out["GevSCBWRA"] = int(node.GetValue())
            rows.append(("GevSCBWRA", int(accumulation), out["GevSCBWRA"]))
        if logging_setup.verbose():
            cls._log_readback(cam, cls._serial(cam), "bandwidth reserve", rows)
        bwa = cls._optional_node(cam, "GevSCBWA")
        if bwa is not None:
            try:
                out["GevSCBWA"] = bwa.GetValue()
            except Exception:
                pass
        return out

    @classmethod
    def set_packet_size(cls, cam, n: int) -> int:
        """Write GevSCPSPacketSize (stream packet size, bytes) and return the
        value read back. For `probe_network.py --sweep`, which looks for the
        MTU wall in each camera's path. A camera without the node raises, and
        a write error propagates, so the sweep reports that size as failed.
        """
        node = cls._require_node(
            cam, "GevSCPSPacketSize",
            hint="the packet-size sweep applies to GigE cameras only")
        node.SetValue(int(n))
        return int(node.GetValue())

    @staticmethod
    def device_address(device):
        """The IPv4 address of an `enumerate_devices()` entry as dotted text,
        or None for a device without one (a USB3 camera). Never raises."""
        try:
            available = getattr(device, "IsIpAddressAvailable", None)
            if available is not None and not available():
                return None
            addr = device.GetIpAddress()
        except Exception:
            return None
        return str(addr) if addr else None

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
    def _require_node(cls, cam, name, hint=None):
        """Like _optional_node, but a missing node is a configuration error.
        `hint` replaces the default advice at the end of the message."""
        node = cls._optional_node(cam, name)
        if node is None:
            advice = hint or ("remove the profile setting that asks for it or "
                              "use a camera that implements it")
            raise RuntimeError(
                f"{name} is not available on this camera (a GigE Vision "
                f"transport feature); {advice}")
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
        if logging_setup.verbose():
            self._log_mode_readback(cam, "free-run", (
                ("TriggerSelector", "FrameStart"), ("TriggerMode", "Off"),
                ("AcquisitionFrameRateEnable", True),
                ("AcquisitionFrameRate", float(fps))))

    @classmethod
    def _log_mode_readback(cls, cam, phase: str, requested) -> None:
        """Read back each (node, requested value) of a mode change. The
        selector is FrameStart after both mode changes, so TriggerMode reads
        FrameStart's."""
        cls._log_readback(cam, cls._serial(cam), phase,
                          [(name, want, cls._read(cam, name))
                           for name, want in requested])

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

        Setting it to 0 removes that ceiling but costs 8-15% of frames IN
        TRANSMISSION, because the limiter also paces readout and without it every
        camera bursts onto the link at once. Keep it above the trigger rate.

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
        requested = [("TriggerSelector", "FrameStart"), ("TriggerMode", "On"),
                     ("TriggerSource", "Line1"),
                     ("TriggerActivation", "RisingEdge")]
        if rate_limit and rate_limit > 0:
            cam.AcquisitionFrameRateEnable.SetValue(True)
            cam.AcquisitionFrameRate.SetValue(float(rate_limit))
            requested += [("AcquisitionFrameRateEnable", True),
                          ("AcquisitionFrameRate", float(rate_limit))]
        else:
            cam.AcquisitionFrameRateEnable.SetValue(False)
            requested.append(("AcquisitionFrameRateEnable", False))
            if announce:
                print("[cam] trigger-rate limiter DISABLED "
                      "(exposure bounded by sensor readout only)", flush=True)
        if logging_setup.verbose():
            self._log_mode_readback(cam, "trigger mode", requested)

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
        'raw' camera (refuse, or convert with the model's step), and states
        the unit of the value it passes via set_exposure_gain(gain_unit=...)
        so a mismatch is refused there; the baseline restore needs no
        decision because it reads and writes the same node.
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
    def set_exposure_gain(cls, cam, exposure_us=None, gain_db=None,
                          gain_unit=None) -> tuple:
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
        node's range. `gain_unit` names the unit the VALUE is in, so a value
        in the wrong unit is refused instead of written:
          - None: the value is in the node's own unit. This is the baseline
            restore, which writes back what get_exposure_gain read from the
            same node, so it is exact on either kind of camera.
          - 'dB': a profile value such as calibration_gain_db. On a 'raw'
            camera it raises ValueError BEFORE any write, because the step
            size is model-specific and 6 dB written as 6 steps is a wrong
            gain that nothing in the log would reveal.
          - 'raw': a step count; refused on a 'dB' camera for the same reason.
        The exposure is applied before the gain is checked, so a refused gain
        never leaves the exposure unset.
        """
        if gain_unit not in (None, "dB", "raw"):
            raise ValueError(f"gain_unit must be None, 'dB' or 'raw', "
                             f"not {gain_unit!r}")
        applied_exp = applied_gain = None
        if exposure_us is not None:
            _, node = cls._find_node(cam, cls.EXPOSURE_NODES)
            if node is not None:
                node.SetValue(cls._clamp_to_node(node, float(exposure_us)))
                applied_exp = node.GetValue()
        if gain_db is not None:
            name, node = cls._find_node(cam, cls.GAIN_NODES)
            if node is not None:
                unit = cls.GAIN_UNITS.get(name)
                if gain_unit is not None and gain_unit != unit:
                    raise ValueError(
                        f"gain value {gain_db!r} is in {gain_unit} but this "
                        f"camera's {name} node takes {unit}; no dB<->raw "
                        f"conversion exists because the step size is "
                        f"model-specific")
                if unit == "raw":
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
        what is mounted next to them. Record it every session so a camera found above
        the 76 C `Critical` threshold can be told apart from one that has always
        run there.

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
                                   frames IS per-camera drift. Cameras behind a
                                   switch with flow control disabled run
                                   thousands of resends per 90 s against ~10 for
                                   their siblings and become the laggards, having
                                   lost no frames at all. Treat a count
                                   three orders of magnitude above the other
                                   cameras as a fault; check switch flow
                                   control first.
        Statistic_Failed_Packet_Count is NOT included: it reads absurd values on
        this hardware (tens of millions against 11 M total) and is untrustworthy.

        The `CANONICAL_STREAM_STATS` keys repeat four of these under the names
        every backend shares, each only when its counter was read:
        buffers_total, buffers_failed, buffers_underrun and resend_requests.

        The socket driver's ReceiveThreadPriority, and whether
        ReceiveThreadPriorityOverride is on (without it pylon uses its own
        default), are read alongside and never written, so a session records
        the priority its receive threads ran at. The filter driver and USB3
        cameras do not have these nodes, and nothing is reported for them.

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
        for canonical, native in self.CANONICAL_STATS:
            if native in out:
                out[canonical] = out[native]
        for key in self.RECEIVE_THREAD_NODES:
            try:
                node = cam.GetStreamGrabberNodeMap().GetNode(key)
                if node is not None:
                    out[key] = node.GetValue()
            except Exception:
                pass
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

    #: `CANONICAL_STREAM_STATS` key -> the stream_stats key it repeats.
    CANONICAL_STATS = (("buffers_total", "Total_Buffer_Count"),
                       ("buffers_failed", "Failed_Buffer_Count"),
                       ("buffers_underrun", "Buffer_Underrun_Count"),
                       ("resend_requests", "Resend_Request_Count"))

    #: Socket-driver stream grabber nodes stream_stats reads and never writes.
    RECEIVE_THREAD_NODES = ("ReceiveThreadPriorityOverride",
                            "ReceiveThreadPriority")

    # -------------------------------------------------------------- SDK report
    @staticmethod
    def sdk_report() -> str:
        """pypylon's version, the pylon runtime version it wraps, and where
        pypylon was imported from. Never raises."""
        version = "unknown version"
        try:
            from importlib import metadata
            version = metadata.version("pypylon")
        except Exception:
            pass
        runtime = ""
        try:
            fn = getattr(pylon, "GetPylonVersionString", None)
            if fn is not None:
                runtime = f", pylon {fn()}"
        except Exception:
            pass
        where = ""
        try:
            where = f" ({os.path.dirname(os.path.abspath(pylon.__file__))})"
        except Exception:
            pass
        return f"pypylon {version}{runtime}{where}"


#: Module-level spellings of the transport knobs, for callers that hold the
#: module rather than a backend instance (probes, experiments).
set_transmission_delay = BaslerBackend.set_transmission_delay
set_bandwidth_reserve = BaslerBackend.set_bandwidth_reserve
