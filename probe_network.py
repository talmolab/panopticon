"""Camera network diagnostics: which switch is each camera on, and does the
path carry jumbo frames?

Two questions this answers that nothing else here can.

**Which switch is a camera plugged into?** A camera SDK can leave out a
camera whose IP address is outside the host adapter's subnet (pylon does), so
a camera moved to another switch looks the same as a dead one: it is absent
from the vendor's viewer and from the GUI, and nothing reports an error. A raw
GVCP discovery broadcast is answered by every GigE Vision camera on the
layer-2 segment whatever its address, so sending one per adapter tells you
where each camera physically is, including a camera plugged into the wrong
switch. Basler and FLIR GigE cameras both answer it.

**Does the path carry 9000-byte packets?** A switch left at the default
1500-byte MTU discards every GVSP data packet while link, enumeration and ICMP
all look healthy. Ping cannot detect this: these cameras answer only small
ICMP echoes, so even a 1472-byte ping fails on a known-good jumbo path, and a
ping-based test reports every switch as broken. The valid test is a real grab
with the stream packet size swept upward, which is what `--sweep` does: a
clean cutoff between two sizes is an MTU wall in the path.

    uv run probe_network.py                          # discovery + subnet check
    uv run probe_network.py --sweep                  # also sweep packet size per camera
    uv run probe_network.py --sweep --cam 3          # sweep one camera (index, 1-based)
    uv run probe_network.py --sweep --profile NAME   # sweep with another profile

Discovery sends only a query. `--sweep` opens the cameras through the
profile's camera backend (`camera_backend`) with the profile's camera
settings, which is what every session does anyway. It needs a backend that can
set the stream packet size (the `set_packet_size` member, which the Basler and
FLIR backends have), and it skips USB3 cameras, which have no packets to size.
"""
from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path

# The checkout this file sits in, so `python probe_network.py` run from
# another folder imports this copy of gui_app.
sys.path.insert(0, str(Path(__file__).resolve().parent))

GVCP_PORT = 3956
DISCOVERY_CMD = 0x0002
DISCOVERY_ACK = 0x0003
#: Discovery broadcasts per adapter, and the pause between them. Discovery is
#: UDP, so one broadcast can be lost; spacing the repeats lets a burst of
#: traffic that dropped the first one pass before the next goes out.
DISCOVERY_TRIES = 3
DISCOVERY_SPACING_S = 0.2

# Sizes either side of the common MTU walls (1500 default, 9014 jumbo).
SWEEP_SIZES = [1500, 2000, 4000, 6000, 8000, 9000]
#: Frames grabbed at each packet size; a size passes when all of them arrive
#: complete.
SWEEP_FRAMES = 10
#: Driver buffers the sweep opens each camera with.
SWEEP_BUFFERS = 100


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
        sock.close()
        return {}
    sock.settimeout(timeout)
    # The answers queue in the socket while the repeats go out, so they are
    # read once the last broadcast is sent.
    for attempt in range(DISCOVERY_TRIES):
        if attempt:
            time.sleep(DISCOVERY_SPACING_S)
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
        except OSError as exc:
            print(f"  reading answers on {bind_ip} failed: {exc}")
            break
        info = _parse_ack(data)
        if info:
            found.setdefault(info["serial"], info)
    sock.close()
    return found


def host_camera_interfaces() -> list[tuple[str, IPv4Interface]]:
    """Host adapters that plausibly face cameras: private, non-loopback,
    non-link-local IPv4 addresses. A camera network is a private subnet, so
    an adapter with a public address is the office or campus link, and a
    discovery broadcast out of it would reach nothing but other people's
    machines. psutil keeps this vendor-neutral."""
    import psutil

    out = []
    for name, addrs in psutil.net_if_addrs().items():
        for a in addrs:
            if a.family != socket.AF_INET or not a.netmask:
                continue
            ip = IPv4Address(a.address)
            if ip.is_loopback or ip.is_link_local or not ip.is_private:
                continue
            out.append((name, IPv4Interface(f"{a.address}/{a.netmask}")))
    return sorted(out, key=lambda kv: kv[0])


# ------------------------------------------------------------- packet-size sweep
def _load_profiles():
    """`[(path, profile)]` for every profile that loads, printing one line
    for each that does not. One broken file (a half-written copy of a
    template) must not stop the sweep on the profiles that are fine."""
    from gui_app.session_config import ProfileError, RigProfile

    loaded = []
    for path in RigProfile.list_profiles():
        try:
            loaded.append((path, RigProfile.load(path)))
        except ProfileError as e:
            print(f"  skipping profile {path.name}: {e}")
    return loaded


def pick_profile(explicit: str | None):
    """The profile whose camera settings the sweep opens the cameras with.

    This repo ships more than one rig profile, and they point at different
    cameras: applying 3dface's .pfs to the 3dpose GigE cameras throws out of
    FeaturePersistence. So the choice mirrors the GUI: the profile last
    selected there, else the first whose camera settings are ready to open
    (`settings_ready`), rather than whichever filename sorts first.

    `explicit` is a profile name, a file name without .yaml, or a path to a
    profile file (for a template or a copy outside profiles/).
    """
    from gui_app.session_config import ProfileError, RigProfile
    from gui_app.settings import KEY_PROFILE, app_settings

    if explicit and Path(explicit).suffix.lower() in (".yaml", ".yml") \
            and Path(explicit).is_file():
        try:
            return RigProfile.load(Path(explicit))
        except ProfileError as e:
            print(f"profile {explicit} cannot be used: {e}")
            return None

    if explicit:
        # A bad file is reported by name when it is the one asked for.
        from gui_app.session_config import PROFILES_DIR
        wanted = PROFILES_DIR / f"{explicit}.yaml"
        profiles = _load_profiles()
        for path, prof in profiles:
            if explicit.lower() in (prof.name.lower(), path.stem.lower()):
                return prof
        if wanted.is_file():
            try:
                return RigProfile.load(wanted)
            except ProfileError as e:
                print(f"profile {explicit!r} cannot be used: {e}")
                return None
        print(f"no profile named {explicit!r}. Available: "
              + (", ".join(pr.name for _p, pr in profiles) or "none"))
        return None

    profiles = _load_profiles()
    if not profiles:
        print("no loadable profiles in profiles/")
        return None
    try:
        want = app_settings().value(KEY_PROFILE)
    except Exception:
        want = None
    if want:
        for _path, prof in profiles:
            if prof.name == want:
                return prof

    for _path, prof in profiles:
        if prof.settings_ready() is None:
            if len(profiles) > 1:
                print(f"  (using profile {prof.name!r}; override with --profile)")
            return prof
    return profiles[0][1]


def _open_kwargs(profile) -> dict:
    """Keywords for backend.open beyond the positional three. The camera:
    block goes only to a profile that has one, because the Basler backend
    refuses the keyword with any value but None."""
    spec = getattr(profile, "camera", None)
    return {} if spec is None else {"camera_spec": spec}


def _grab_complete(be, cam, frames: int) -> int:
    """Frames of `frames` retrieved complete, with the stream started and
    stopped around them."""
    be.start_grabbing(cam)
    ok = 0
    try:
        for _ in range(frames):
            try:
                res = be.retrieve(cam, 1500)
            except be.TimeoutException:
                continue
            try:
                ok += 1 if res.GrabSucceeded() else 0
            finally:
                res.Release()
    finally:
        be.stop_grabbing(cam)
    return ok


def _sweep_camera(be, dev, profile) -> int:
    """Open one camera, grab SWEEP_FRAMES frames at every size in
    SWEEP_SIZES, and return how many sizes failed."""
    problems = 0
    cam = None
    try:
        try:
            cam = be.open(dev, profile.pfs_path, SWEEP_BUFFERS,
                          **_open_kwargs(profile))
            be.set_freerun(cam, 30.0)
        except Exception as exc:
            print(f"    cannot open: {type(exc).__name__}: {exc}")
            return 1
        for size in SWEEP_SIZES:
            try:
                got = be.set_packet_size(cam, size)
                note = "" if got == size else f" (camera holds {got})"
                ok = _grab_complete(be, cam, SWEEP_FRAMES)
                verdict = "ok" if ok == SWEEP_FRAMES else "FAIL"
                if ok < SWEEP_FRAMES:
                    problems += 1
                print(f"    packet={size:<5} complete={ok:>2}/{SWEEP_FRAMES}  "
                      f"{verdict}{note}")
            except Exception as exc:
                print(f"    packet={size:<5} ERROR {type(exc).__name__}: {exc}")
                problems += 1
    finally:
        if cam is not None:
            try:
                be.close(cam)
            except Exception:
                pass
    return problems


def sweep(serial_filter: int | None, profile_name: str | None,
          force: bool = False) -> int:
    from gui_app.backends import load_backend
    from gui_app.probe_guard import refuse_if_panopticon_running

    # The sweep opens cameras with the profile's settings, so it must not
    # run beside a session that already holds them. Discovery above is a UDP
    # query and stays unguarded, which is why probe_network.py is not itself
    # a marker in probe_guard.PANOPTICON_MARKERS.
    refuse_if_panopticon_running(force=force)

    profile = pick_profile(profile_name)
    if profile is None:
        return 1
    backend_name = profile.camera_backend
    print(f"\n=== packet-size sweep (profile {profile.name}, camera backend "
          f"{backend_name}) ===")
    reason = profile.settings_ready()
    if reason is not None:
        print(f"the profile's cameras cannot be opened: {reason}")
        return 1
    try:
        be = load_backend(backend_name,
                          camera_spec=getattr(profile, "camera", None))
    except (ImportError, ValueError) as exc:
        print(f"cannot load camera backend {backend_name!r}: {exc}")
        return 1
    if getattr(be, "set_packet_size", None) is None:
        print(f"the {backend_name} backend cannot set a stream packet size, "
              f"so there is nothing to sweep; the sweep is skipped. The "
              f"discovery above still shows where each GigE camera is.")
        return 0
    address = getattr(be, "device_address", None)
    print("A clean cutoff between two sizes is an MTU wall in that camera's path.\n")

    devices = be.enumerate_devices()
    if not devices:
        print("no cameras enumerated")
        return 1

    problems = 0
    for idx, dev in enumerate(devices, start=1):
        if serial_filter is not None and idx != serial_filter:
            continue
        ip = address(dev) if address is not None else "address unknown"
        print(f"cam{idx}  {dev.GetSerialNumber()}  {ip or 'no IP address'}")
        if ip is None:
            print("    skipped: a camera without an IP address (USB3) has no "
                  "stream packets to size\n")
            continue
        problems += _sweep_camera(be, dev, profile)
        print()
    return 1 if problems else 0


# ------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sweep", action="store_true",
                    help="also sweep the stream packet size per camera "
                         "(opens cameras)")
    ap.add_argument("--cam", type=int, default=None,
                    help="restrict --sweep to one camera by 1-based index")
    ap.add_argument("--profile", default=None,
                    help="rig profile whose camera settings --sweep opens the "
                         "cameras with: a name, or a path to a profile file "
                         "(default: the one the GUI last used)")
    from gui_app.probe_guard import add_force_argument
    add_force_argument(ap)
    args = ap.parse_args()

    interfaces = host_camera_interfaces()
    if not interfaces:
        print("no candidate camera adapters found (a camera network uses a "
              "private IPv4 subnet, and no adapter has an address on one)")
        return 1

    print("=== GVCP discovery, per host adapter ===")
    print("Answers arrive from every camera on the segment, whatever its address,")
    print("so the adapter that hears a camera is the switch it is plugged into.\n")

    # Group by adapter, not by address. An adapter with two IPv4 addresses (a
    # leftover temporary one, say) would otherwise be listed twice and its
    # cameras flagged WRONG SUBNET against the address they do not belong to,
    # a false alarm that reads as a recabled rig.
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
            # In the subnet of any of this adapter's addresses counts as in-subnet.
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
        print("USB3 cameras do not answer GVCP discovery; this check covers "
              "GigE cameras only.")
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
        print("\n  A camera SDK may not list these, so the GUI does not show")
        print("  them, even though the switch forwards their traffic. Either")
        print("  move the cable to the matching switch, or give the camera an")
        print("  address on this segment with the vendor's IP tool (pylon IP")
        print("  Configurator for Basler, SpinView for FLIR).")
    else:
        print("  every camera is on the correct subnet for its switch")

    rc = 0
    if args.sweep:
        rc = sweep(args.cam, args.profile, force=args.force)
    else:
        print("\n  (add --sweep to test whether the paths carry 9000-byte packets)")
    return 1 if stranded else rc


if __name__ == "__main__":
    sys.exit(main())
