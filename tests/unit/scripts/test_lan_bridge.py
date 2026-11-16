"""LAN discovery parses the host's neighbour table without touching devices' data."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("lan_bridge", ROOT / "scripts" / "lan_bridge.py")
assert SPEC and SPEC.loader
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


def test_arp_names_are_physical_interfaces_only(monkeypatch):
    arp = """
? (192.0.2.12) at 8c:de:f9:22:a1:4 on en0 ifscope [ethernet]
? (192.0.2.13) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]
? (10.0.0.9) at 11:22:33:44:55:66 on feth1 ifscope [ethernet]
? (192.0.2.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]
"""
    monkeypatch.setattr(bridge, "_run", lambda command: arp if command[0] == "arp" else "")

    found = bridge._arp_neighbours()

    assert set(found) == {"192.0.2.12", "192.0.2.13"}
    assert found["192.0.2.12"] == "8c:de:f9:22:a1:4"


def test_netmask_to_prefix():
    assert bridge._prefix_from_netmask("0xffffff00") == 24
    assert bridge._prefix_from_netmask("255.255.255.0") == 24
