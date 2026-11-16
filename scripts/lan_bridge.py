#!/usr/bin/env python3
"""Read-only LAN device discovery for Conductor's local sandbox panel.

Conductor's backend runs in Docker and cannot see the host's ARP table. This
small host-side bridge reads the neighbours macOS/Linux already learned,
probes a few useful ports, and exposes a sanitized device list on loopback.

Run on the host:

    python3 scripts/lan_bridge.py --serve --host 0.0.0.0 --port 8766

Then set ``LAN_BRIDGE_URL=http://host.docker.internal:8766/devices`` in
Conductor's local ``.env`` and restart the backend. The bridge sends addresses,
MACs and open-port booleans only: no SSH keys, credentials or traffic
content; reverse-DNS hostnames are included so the list is readable.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# Ports that tell a device apart for the local panel. 22 = SSH; 8000 = an
# Conductor backend; 5173 = the Vite frontend.
PROBE_PORTS = (22, 8000, 5173)
_ARP_IP = re.compile(r"\((\d+\.\d+\.\d+\.\d+)\)")
_ARP_MAC = re.compile(r"at ([0-9a-f]{1,2}(?::[0-9a-f]{1,2}){5})", re.IGNORECASE)
_ARP_IFACE = re.compile(r" on ([a-z0-9]+) ", re.IGNORECASE)
_PHYSICAL_IFACE = re.compile(r"^(en|eth|wl|wlan)\d+$", re.IGNORECASE)
_IFCONFIG_INET = re.compile(r"inet (\d+\.\d+\.\d+\.\d+) netmask (0x[0-9a-f]+|\d+\.\d+\.\d+\.\d+)")


def _run(command: list[str]) -> str:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=8).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""


def _own_addresses() -> set[str]:
    addresses: set[str] = set()
    text = _run(["ifconfig"]) or _run(["ip", "-4", "addr", "show"])
    for match in _IFCONFIG_INET.finditer(text):
        addresses.add(match.group(1))
    return addresses


def _prefix_from_netmask(netmask: str) -> int:
    if netmask.startswith("0x"):
        return bin(int(netmask, 16)).count("1")
    return sum(bin(int(part)).count("1") for part in netmask.split("."))


def _subnets() -> list[str]:
    """Private IPv4 subnets this host is attached to, /24 capped for the scan."""
    subnets: list[str] = []
    text = _run(["ifconfig"])
    current: str | None = None
    for line in text.splitlines():
        header = re.match(r"^([a-z0-9]+):", line)
        if header:
            current = header.group(1)
            continue
        if current and not _PHYSICAL_IFACE.match(current):
            continue
        match = _IFCONFIG_INET.search(line)
        if not match:
            continue
        ip, netmask = match.group(1), match.group(2)
        if ip.startswith(("127.", "169.254.")):
            continue
        prefix = max(24, min(30, _prefix_from_netmask(netmask)))
        octets = ip.split(".")
        subnets.append(f"{octets[0]}.{octets[1]}.{octets[2]}.0/{prefix}")
    return sorted(set(subnets))


def _arp_neighbours() -> dict[str, str | None]:
    """address -> MAC, from the neighbour table the host already has."""
    neighbours: dict[str, str | None] = {}
    text = _run(["arp", "-an"]) or _run(["ip", "neigh"])
    for line in text.splitlines():
        match = _ARP_IP.search(line)
        if not match:
            continue
        address = match.group(1)
        if address.startswith(("0.", "127.", "169.254.", "224.", "239.", "255.")):
            continue
        if address.endswith(".255"):
            continue
        iface = _ARP_IFACE.search(line)
        if iface and not _PHYSICAL_IFACE.match(iface.group(1)):
            continue
        mac = _ARP_MAC.search(line)
        neighbours[address] = mac.group(1).lower() if mac else None
    return neighbours


def probe(address: str, timeout: float = 0.35) -> list[int]:
    open_ports: list[int] = []
    for port in PROBE_PORTS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            if sock.connect_ex((address, port)) == 0:
                open_ports.append(port)
        finally:
            sock.close()
    return open_ports


def _hostname(address: str) -> str | None:
    """Reverse DNS, bounded: the first label only, or None when it is just the IP."""
    try:
        name = socket.gethostbyaddr(address)[0].split(".", 1)[0].strip()
    except (OSError, UnicodeError):
        return None
    return name[:64] or None


def discover() -> dict[str, Any]:
    """Neighbour list plus the ports that make a device useful to Conductor."""
    own = _own_addresses()
    neighbours = {address: mac for address, mac in _arp_neighbours().items() if address not in own}
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as executor:
        ports = dict(zip(neighbours, executor.map(probe, neighbours)))
        names = dict(zip(neighbours, executor.map(_hostname, neighbours)))
    devices = []
    for address, mac in sorted(neighbours.items(), key=lambda item: tuple(int(p) for p in item[0].split("."))):
        open_ports = ports.get(address, [])
        devices.append({
            "address": address,
            "hostname": names.get(address),
            "mac": mac,
            "ports": open_ports,
            "ssh": 22 in open_ports,
            "conductor": 8000 in open_ports or 5173 in open_ports,
        })
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "subnets": _subnets(),
        "devices": devices,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ConductorLanBridge/1.0"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.split("?", 1)[0] not in ("/devices", "/"):
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(self.server.snapshot(), ensure_ascii=False).encode("utf-8")  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: Any) -> None:
        pass


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], ttl: float):
        super().__init__(address, Handler)
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, Any]] | None = None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = time.time()
            if self._cached and now - self._cached[0] < self.ttl:
                return self._cached[1]
            data = discover()
            self._cached = (now, data)
            return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help="Serve /devices instead of printing once")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--ttl", type=float, default=30.0, help="Discovery cache seconds")
    parser.add_argument("--output", help="Write one snapshot to this file (default: stdout)")
    args = parser.parse_args(argv)

    if args.serve:
        server = BridgeServer((args.host, args.port), args.ttl)
        print(f"LAN bridge listening on http://{args.host}:{args.port}/devices", file=sys.stderr)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            return 0
        return 0

    rendered = json.dumps(discover(), ensure_ascii=False, indent=2)
    if args.output:
        from pathlib import Path
        target = Path(args.output).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
        print(target, file=sys.stderr)
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
