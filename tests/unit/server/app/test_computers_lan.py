"""LAN discovery is a read-only, sanitized host-bridge projection."""
from __future__ import annotations

import json

import pytest

from src.server.app import computers as module


@pytest.mark.asyncio
async def test_lan_snapshot_reads_file_and_drops_unsafe_rows(monkeypatch, tmp_path):
    snapshot = tmp_path / "lan.json"
    snapshot.write_text(
        json.dumps(
            {
                "generated_at": "2026-10-27T00:00:00Z",
                "subnets": ["192.0.2.0/24", "not a subnet"],
                "devices": [
                    {
                        "address": "192.0.2.14",
                        "hostname": "mac-mini",
                        "mac": "8c:de:f9:22:a1:4",
                        "ports": [22, 8000, 99999, "bad"],
                        "ssh": True,
                        "conductor": True,
                        "secret": "should not pass",
                    },
                    {"address": "not-an-ip", "ports": [22]},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("LAN_BRIDGE_URL", raising=False)
    monkeypatch.setenv("LAN_DEVICES_FILE", str(snapshot))
    monkeypatch.setattr(module, "_lan_cache", None)

    result = await module._lan_snapshot()

    assert result["available"] is True
    assert result["subnets"] == ["192.0.2.0/24"]
    assert result["devices"] == [
        {
            "address": "192.0.2.14",
            "hostname": "mac-mini",
            "mac": "8c:de:f9:22:a1:4",
            "ports": [22, 8000],
            "ssh": True,
            "conductor": True,
        }
    ]


@pytest.mark.asyncio
async def test_lan_snapshot_without_config_is_unavailable(monkeypatch):
    monkeypatch.delenv("LAN_BRIDGE_URL", raising=False)
    monkeypatch.delenv("LAN_DEVICES_FILE", raising=False)
    monkeypatch.setattr(module, "_lan_cache", None)

    result = await module._lan_snapshot()

    assert result["available"] is False
    assert result["reason"] == "not_configured"
    assert result["devices"] == []
