"""USB ADB Reverse Tunnel & RNDIS / LAN Network Interface Discovery."""

from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psutil

PROJECT_ROOT = Path(__file__).resolve().parent.parent

USB_IFACE_KEYWORDS = (
    "rndis",
    "remote ndis",
    "usb",
    "ncm",
    "cdc",
    "tether",
    "android",
    "iphone",
    "apple mobile device",
)

# Well-known Android RNDIS / USB tethering & iOS USB Personal Hotspot subnets
KNOWN_USB_SUBNETS = (
    ipaddress.ip_network("192.168.42.0/24"),   # Classic Android RNDIS USB tethering
    ipaddress.ip_network("192.168.44.0/24"),   # Android USB / Bluetooth PAN
    ipaddress.ip_network("192.168.225.0/24"),  # Cellular / RNDIS modem subnet
    ipaddress.ip_network("172.20.10.0/28"),    # Apple iOS USB Personal Hotspot
)


@dataclass
class NetworkEndpoint:
    interface: str
    ip: str
    netmask: str
    url: str
    is_usb_tether: bool
    description: str = ""


@dataclass
class AdbReverseResult:
    adb_found: bool
    adb_path: str | None = None
    tunnel_active: bool = False
    port: int = 8000
    devices: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def find_adb_binary() -> str | None:
    """Locate `adb` on system PATH or in local `./platform-tools` directories."""
    # 1. Check system PATH first
    for binary_name in ("adb", "adb.exe"):
        found = shutil.which(binary_name)
        if found:
            return str(Path(found).resolve())

    # 2. Check local ./platform-tools relative to CWD and project root
    exe_names = ("adb.exe", "adb") if os.name == "nt" else ("adb", "adb.exe")
    search_roots = [Path.cwd(), PROJECT_ROOT]
    seen_roots: set[Path] = set()

    for root in search_roots:
        resolved_root = root.resolve()
        if resolved_root in seen_roots:
            continue
        seen_roots.add(resolved_root)
        for exe in exe_names:
            candidate = resolved_root / "platform-tools" / exe
            if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return str(candidate)

    return None


def _list_adb_devices(adb_path: str) -> list[str]:
    """Return serial numbers of connected Android devices in `device` state."""
    try:
        proc = subprocess.run(
            [adb_path, "devices"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    serials: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        parts = line.strip().split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def setup_adb_reverse(port: int = 8000) -> AdbReverseResult:
    """Check for ADB and establish `adb reverse tcp:<port> tcp:<port>`."""
    adb_path = find_adb_binary()
    if not adb_path:
        return AdbReverseResult(
            adb_found=False,
            port=port,
            message=(
                "ADB not found on PATH or ./platform-tools. "
                "Install Android Platform-Tools or use USB Tethering / Wi-Fi."
            ),
        )

    devices = _list_adb_devices(adb_path)

    try:
        proc = subprocess.run(
            [adb_path, "reverse", f"tcp:{port}", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return AdbReverseResult(
            adb_found=True,
            adb_path=adb_path,
            tunnel_active=False,
            port=port,
            devices=devices,
            message=f"Failed executing adb reverse: {exc}",
        )

    if proc.returncode == 0:
        return AdbReverseResult(
            adb_found=True,
            adb_path=adb_path,
            tunnel_active=True,
            port=port,
            devices=devices,
            message=f"ADB reverse active (tcp:{port} -> tcp:{port}) for {len(devices) or 1} device(s).",
        )

    stderr = (proc.stderr or proc.stdout or "").strip()

    # If multiple devices are attached, reverse each connected device explicitly
    if len(devices) > 1:
        succeeded: list[str] = []
        for serial in devices:
            sub = subprocess.run(
                [adb_path, "-s", serial, "reverse", f"tcp:{port}", f"tcp:{port}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if sub.returncode == 0:
                succeeded.append(serial)
        if succeeded:
            return AdbReverseResult(
                adb_found=True,
                adb_path=adb_path,
                tunnel_active=True,
                port=port,
                devices=succeeded,
                message=f"ADB reverse active on devices: {', '.join(succeeded)}",
            )

    return AdbReverseResult(
        adb_found=True,
        adb_path=adb_path,
        tunnel_active=False,
        port=port,
        devices=devices,
        message=f"ADB found ({adb_path}), but tunnel not active: {stderr or 'no authorized USB device'}",
    )


def _windows_adapter_descriptions() -> dict[str, str]:
    """Map IPv4 addresses to adapter descriptions on Windows via `ipconfig /all`."""
    if sys.platform != "win32":
        return {}
    try:
        proc = subprocess.run(
            ["ipconfig", "/all"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}

    ip_to_desc: dict[str, str] = {}
    current_desc = ""
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        lower = line.lower()
        if lower.startswith("description"):
            _, _, val = line.partition(":")
            current_desc = val.strip()
        elif "ipv4 address" in lower or "ip address" in lower:
            _, _, val = line.partition(":")
            cleaned = val.split("(")[0].strip()
            if cleaned and current_desc:
                ip_to_desc[cleaned] = current_desc
    return ip_to_desc


def discover_network_endpoints(port: int = 8000) -> list[NetworkEndpoint]:
    """Detect USB Tethering / RNDIS subnets and Wi-Fi / LAN network interfaces."""
    endpoints: list[NetworkEndpoint] = []
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    win_descriptions = _windows_adapter_descriptions()

    for iface_name, addr_list in addrs.items():
        iface_stat = stats.get(iface_name)
        if iface_stat is not None and not iface_stat.isup:
            continue

        for snic in addr_list:
            if snic.family != socket.AF_INET:
                continue
            ip_str = snic.address
            try:
                ip_obj = ipaddress.IPv4Address(ip_str)
            except ipaddress.AddressValueError:
                continue

            if ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_multicast:
                continue

            desc = win_descriptions.get(ip_str, "")
            combined_label = f"{iface_name} {desc}".lower()

            is_usb = any(kw in combined_label for kw in USB_IFACE_KEYWORDS) or any(
                ip_obj in subnet for subnet in KNOWN_USB_SUBNETS
            )

            endpoints.append(
                NetworkEndpoint(
                    interface=iface_name,
                    ip=ip_str,
                    netmask=snic.netmask or "255.255.255.0",
                    url=f"http://{ip_str}:{port}",
                    is_usb_tether=is_usb,
                    description=desc,
                )
            )

    # Sort USB tethering / RNDIS interfaces first, then LAN/Wi-Fi
    endpoints.sort(key=lambda ep: (not ep.is_usb_tether, ep.interface, ep.ip))
    return endpoints


def print_startup_banner(adb_result: AdbReverseResult, endpoints: list[NetworkEndpoint], audio_summary: str) -> None:
    """Print connection options, USB ADB status, RNDIS IPs, and audio target to console."""
    usb_endpoints = [ep for ep in endpoints if ep.is_usb_tether]
    lan_endpoints = [ep for ep in endpoints if not ep.is_usb_tether]

    sep = "=" * 72
    print(f"\n{sep}")
    print("  iMic — Ultra-Low-Latency Smartphone-to-PC Microphone (48kHz PCM16)")
    print(sep)
    print(f"  [Audio Target]  {audio_summary}")
    print("-" * 72)

    if adb_result.tunnel_active:
        print(f"  [1] ZERO-LATENCY USB (ADB Reverse ACTIVE):")
        print(f"      Open on phone:  http://localhost:{adb_result.port}")
        if adb_result.devices:
            print(f"      USB Device(s):  {', '.join(adb_result.devices)}")
    else:
        print(f"  [1] USB ADB Reverse Status:")
        print(f"      {adb_result.message}")
        print(f"      (Once connected via USB Debugging, open http://localhost:{adb_result.port})")

    print("-" * 72)
    print("  [2] USB Tethering / RNDIS Interfaces:")
    if usb_endpoints:
        for ep in usb_endpoints:
            extra = f" ({ep.description})" if ep.description else ""
            print(f"      • {ep.interface}{extra}: {ep.url}")
    else:
        print("      • None detected (enable 'USB Tethering' on phone if not using ADB)")

    print("-" * 72)
    print("  [3] Wi-Fi / LAN Fallback Interfaces:")
    if lan_endpoints:
        for ep in lan_endpoints:
            extra = f" ({ep.description})" if ep.description else ""
            print(f"      • {ep.interface}{extra}: {ep.url}")
    else:
        print("      • No active LAN/Wi-Fi IPv4 interfaces detected")

    print(f"{sep}\n", flush=True)
