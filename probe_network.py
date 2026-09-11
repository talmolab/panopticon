"""Camera network diagnostics: which switch is each camera on, and does the
path carry jumbo frames?

Two questions this answers that nothing else here can.

**Which switch is a camera plugged into?** pylon's enumeration silently omits a
camera whose IP is outside the host adapter's subnet, so a camera moved to a
different switch looks *identical to a dead one*: absent from pylon Viewer,
absent from the GUI, no error anywhere. A raw GVCP discovery broadcast is
answered by every camera on the layer-2 segment regardless of its address, so
sending one per adapter tells you where each camera physically is. That is how
two "missing" cameras were found in ten seconds on 2026-09-10 after being
plugged back into each other's switches.

**Does the path carry 9000-byte packets?** A switch left at the default
1500-byte MTU discards every GVSP data packet while link, enumeration and ICMP
all look perfectly healthy. **Ping cannot detect this**: these cameras answer
only tiny ICMP echoes, so even a 1472-byte ping fails on a known-good jumbo
path, and a ping-based test reports every switch as broken. The only valid test
is a real grab with `GevSCPSPacketSize` swept upward, which is what `--sweep`
does: a clean cutoff between two sizes is an MTU wall in the path.

    uv run probe_network.py                  # discovery + subnet check
    uv run probe_network.py --sweep          # also sweep packet size per camera
    uv run probe_network.py --sweep --cam 3  # sweep one camera (index, 1-based)

Read-only. Discovery sends only a query; --sweep opens cameras and applies the
profile's .pfs, which is what every session does anyway.
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
from ipaddress import IPv4Address, IPv4Interface

GVCP_PORT = 3956
DISCOVERY_CMD = 0x0002
DISCOVERY_ACK = 0x0003

# Sizes either side of the common MTU walls (1500 default, 9014 jumbo).
SWEEP_SIZES = [1500, 2000, 4000, 6000, 8000, 9000]


# --------------------------------------------------------------- GVCP discovery
def _discovery_packet(req_id: int = 1) -> bytes:
    # magic 0x42, flags 0x11 (ack required | broadcast ack permitted)
    return struct.pack(">BBHHH", 0x42, 0x11, DISCOVERY_CMD, 0x0000, req_id)


def _cstr(raw: bytes) -> str:
    return raw.split(b"\x00")[0].decode("ascii", "replace").strip()


def _parse_ack(data: bytes) -> dict | None:
    """Decode a GigE Vision DISCOVERY_ACK. Offsets are the bootstrap register
    layout from the GigE Vision spec; the payload is a fixed 248 bytes."""
    if len(data) < 8:
        return None
    _status, ack, _length, _ack_id = struct.unpack(">HHHH", data[:8])
    if ack != DISCOVERY_ACK:
        return None
    p = data[8:]
    if len(p) < 0xF8:
        return None
    return {
        "mac": "".join(f"{b:02X}" for b in p[0x0A:0x10]),
        "ip": socket.inet_ntoa(p[0x24:0x28]),
        "mask": socket.inet_ntoa(p[0x34:0x38]),
        "model": _cstr(p[0x68:0x88]),
        "serial": _cstr(p[0xD8:0xE8]),
    }


def discover(bind_ip: str, timeout: float = 2.0) -> dict[str, dict]:
    """Every camera answering on the segment reachable from `bind_ip`."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind((bind_ip, 0))
    except OSError as exc:
        print(f"  cannot bind {bind_ip}: {exc}")
        return {}
    sock.settimeout(timeout)
    # Three tries: discovery is UDP and a single broadcast can be missed.
    for attempt in range(3):
        try:
            sock.sendto(_discovery_packet(attempt + 1),
                        ("255.255.255.255", GVCP_PORT))
        except OSError as exc:
            print(f"  broadcast from {bind_ip} failed: {exc}")
            break
    found: dict[str, dict] = {}
    while True:
        try:
            data, _addr = sock.recvfrom(4096)
        except socket.timeout:
            break
        info = _parse_ack(data)
        if info:
            found.setdefault(info["serial"], info)
    sock.close()
    return found


def host_camera_interfaces() -> list[tuple[str, IPv4Interface]]:
    """Host adapters that plausibly face cameras: private, non-loopback,
    non-link-local IPv4 addresses. psutil keeps this vendor-neutral."""
    import psutil

    out = []
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            ip = IPv4Address(a.address)
            if ip.is_loopback or ip.is_link_local:
                continue
            out.append((name, IPv4Interface(f"{a.address}/{a.netmask}")))
    return sorted(out, key=lambda kv: kv[0])


# ------------------------------------------------------------- packet-size sweep
def pick_profile(explicit: str | None):
    """The profile whose .pfs the sweep should apply.

    This repo ships more than one rig profile, and they point at different
    cameras: applying 3dface's .pfs to the 3dpose GigE cameras throws out of
    FeaturePersistence. So mirror what the GUI does -- the profile last selected
    there, else the first whose .pfs exists -- rather than taking whichever
    filename sorts first.
    """
    from pathlib import Path

    from gui_app.session_config import RigProfile

    paths = RigProfile.list_profiles()
    if not paths:
        print("no profiles found in profiles/")
        return None
    profiles = [(p, RigProfile.load(p)) for p in paths]

    if explicit:
        for path, prof in profiles:
            if explicit.lower() in (prof.name.lower(), path.stem.lower()):
                return prof
        print(f"no profile named {explicit!r}. Available: "
              + ", ".join(pr.name for _p, pr in profiles))
        return None

    try:
        from PyQt5.QtCore import QSettings
        want = QSettings("Salk", "Panopticon").value("profile_name")
    except Exception:
        want = None
    if want:
        for _path, prof in profiles:
            if prof.name == want:
                return prof

    for _path, prof in profiles:
        if prof.pfs_path and Path(prof.pfs_path).exists():
            if len(profiles) > 1:
                print(f"  (using profile {prof.name!r}; override with --profile)")
            return prof
    return profiles[0][1]


def sweep(serial_filter: int | None, profile_name: str | None) -> int:
    from gui_app.backends import load_backend

    profile = pick_profile(profile_name)
    if profile is None:
        return 1
    pfs = profile.pfs_path
    print(f"\n=== packet-size sweep (profile {profile.name}, {pfs}) ===")
    print("A clean cutoff between two sizes is an MTU wall in that camera's path.\n")

    be = load_backend("basler")
    devices = be.enumerate_devices()
    if not devices:
        print("no cameras enumerated")
        return 1

    problems = 0
    for idx, dev in enumerate(devices, start=1):
        if serial_filter is not None and idx != serial_filter:
            continue
        print(f"cam{idx}  {dev.GetSerialNumber()}  {dev.GetIpAddress()}")
        for size in SWEEP_SIZES:
            cam = None
            try:
                cam = be.open(dev, pfs, 100)
                be.set_freerun(cam, 30.0)
                cam.GevSCPSPacketSize.SetValue(size)
                be.start_grabbing(cam)
                ok = 0
                for _ in range(10):
                    try:
                        res = be.retrieve(cam, 1500)
                    except be.TimeoutException:
                        continue
                    try:
                        ok += 1 if res.GrabSucceeded() else 0
                    finally:
                        res.Release()
                be.stop_grabbing(cam)
                verdict = "ok" if ok == 10 else "FAIL"
                if ok < 10 and size <= 9000:
                    problems += 1
                print(f"    packet={size:<5} complete={ok:>2}/10  {verdict}")
            except Exception as exc:
                print(f"    packet={size:<5} ERROR {type(exc).__name__}: {exc}")
                problems += 1
            finally:
                if cam is not None:
                    try:
                        be.close(cam)
                    except Exception:
                        pass
        print()
    return 1 if problems else 0


# ------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", action="store_true",
                    help="also sweep GevSCPSPacketSize per camera (opens cameras)")
    ap.add_argument("--cam", type=int, default=None,
                    help="restrict --sweep to one camera by 1-based index")
    ap.add_argument("--profile", default=None,
                    help="rig profile whose .pfs --sweep applies "
                         "(default: the one the GUI last used)")
    args = ap.parse_args()

    interfaces = host_camera_interfaces()
    if not interfaces:
        print("no candidate camera adapters found")
        return 1

    print("=== GVCP discovery, per host adapter ===")
    print("Answers arrive from every camera on the segment, whatever its address,")
    print("so the adapter that hears a camera is the switch it is plugged into.\n")

    # Group by ADAPTER, not by address. An adapter with two IPv4 addresses (a
    # leftover temporary one, say) would otherwise be listed twice and its
    # cameras flagged WRONG SUBNET against the address they do not belong to --
    # a false alarm that briefly looked like the rig had been recabled.
    by_adapter: dict[str, list] = {}
    for name, iface in interfaces:
        by_adapter.setdefault(name, []).append(iface)

    everywhere: dict[str, tuple[str, dict, list]] = {}
    for name, ifaces in by_adapter.items():
        cams = {}
        for iface in ifaces:
            cams.update(discover(str(iface.ip)))
        if not cams:
            continue
        nets = ", ".join(str(i.network) for i in ifaces)
        print(f"{name}  ({nets})")
        for serial, info in sorted(cams.items()):
            # In-subnet for ANY of this adapter's addresses is in-subnet.
            in_subnet = any(IPv4Address(info["ip"]) in i.network for i in ifaces)
            flag = "" if in_subnet else "   <-- WRONG SUBNET FOR THIS SWITCH"
            print(f"  {serial}  ip={info['ip']:<16} mac={info['mac']}  "
                  f"{info['model']}{flag}")
            everywhere[serial] = (name, info, ifaces)
        print()

    if not everywhere:
        print("No camera answered on any adapter. Check power, cabling and link")
        print("lights. Windows Firewall can also block the inbound UDP reply:")
        print("  New-NetFirewallRule -DisplayName 'GVCP' -Direction Inbound "
              "-Protocol UDP -LocalPort 3956 -Action Allow")
        return 1

    stranded = [(s, v) for s, v in everywhere.items()
                if not any(IPv4Address(v[1]["ip"]) in i.network for i in v[2])]

    print("=== summary ===")
    print(f"  {len(everywhere)} camera(s) answered discovery")
    if stranded:
        print(f"  {len(stranded)} on the WRONG SUBNET for the switch they are "
              f"plugged into:")
        for serial, (name, info, ifaces) in stranded:
            nets = ", ".join(str(i.network) for i in ifaces)
            print(f"    {serial}  has {info['ip']}, but {name} serves {nets}")
        print("\n  These are invisible to pylon and to the GUI even though the")
        print("  switch is forwarding their traffic perfectly. Either move the")
        print("  cable to the matching switch, or give the camera an address on")
        print("  this segment (pylon IP Configurator, or")
        print("  GigETransportLayer.BroadcastIpConfiguration by MAC).")
    else:
        print("  every camera is on the correct subnet for its switch")

    rc = 0
    if args.sweep:
        rc = sweep(args.cam, args.profile)
    else:
        print("\n  (add --sweep to test whether the paths carry 9000-byte packets)")
    return 1 if stranded else rc


if __name__ == "__main__":
    sys.exit(main())
