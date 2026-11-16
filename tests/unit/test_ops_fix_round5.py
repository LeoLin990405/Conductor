"""Ops-5: MCP registry readiness uses lstat, not Path.exists()."""

from __future__ import annotations

import os

from src.server.app.ready import ReadyProbes, evaluate, probe_mcp_registry


def _ok() -> dict[str, object]:
    return {"ok": True, "detail": "ok"}


def _backend_probes(mcp) -> ReadyProbes:
    return ReadyProbes(
        database=_ok,
        redis=_ok,
        mcp_registry=mcp,
        dispatch_config=_ok,
        daemon_heartbeats=_ok,
    )


def _assert_unreadable(monkeypatch, path) -> None:
    monkeypatch.setenv("CONDUCTOR_MCP_REGISTRY", str(path))
    result = probe_mcp_registry()
    assert result == {"ok": False, "detail": "unreadable"}
    status, body = evaluate(_backend_probes(probe_mcp_registry), scope="backend")
    assert status == 503
    assert body["status"] == "not_ready"
    assert body["checks"]["mcp_registry"] == result


def test_dangling_mcp_registry_link_is_unreadable(tmp_path, monkeypatch):
    path = tmp_path / "servers.toml"
    path.symlink_to(tmp_path / "missing.toml")
    _assert_unreadable(monkeypatch, path)


def test_self_loop_mcp_registry_link_is_unreadable(tmp_path, monkeypatch):
    path = tmp_path / "servers.toml"
    path.symlink_to(path)
    _assert_unreadable(monkeypatch, path)


def test_inaccessible_mcp_registry_parent_is_unreadable(tmp_path, monkeypatch):
    parent = tmp_path / "locked"
    parent.mkdir()
    path = parent / "servers.toml"
    path.write_text("[servers]\n", encoding="utf-8")
    os.chmod(parent, 0o000)
    try:
        _assert_unreadable(monkeypatch, path)
    finally:
        os.chmod(parent, 0o700)
