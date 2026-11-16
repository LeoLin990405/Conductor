"""Rendering preserves unmanaged text and never copies env values."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from src.mcp_hub import (
    McpHubError,
    Registry,
    Server,
    check_server,
    diff_all,
    import_existing,
    list_status,
    save_registry,
)
from src.mcp_hub.store import load_registry


def _cursor(home: Path, body: dict) -> Path:
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


def test_import_stores_env_keys_not_values(tmp_path: Path) -> None:
    secret = "supersecret-value"
    path = _cursor(
        tmp_path,
        {"mcpServers": {"hubbed": {"command": "uvx", "args": ["pkg"], "env": {"TOKEN": secret}}}},
    )
    before = path.read_bytes()
    imported = import_existing(tmp_path)
    assert "hubbed" in imported
    registry = load_registry(tmp_path)
    assert registry.servers["hubbed"].env == {"TOKEN": "${TOKEN}"}
    stored = (tmp_path / ".conductor" / "mcp" / "servers.toml").read_text(encoding="utf-8")
    assert secret not in stored
    assert path.read_bytes() == before


def test_import_skips_hostile_names(tmp_path: Path) -> None:
    _cursor(
        tmp_path,
        {
            "mcpServers": {
                "../x": {"command": "echo", "args": ["nope"]},
                "ok": {"command": "uvx", "args": ["pkg"]},
            }
        },
    )
    imported = import_existing(tmp_path)
    assert set(imported) == {"ok"}
    stored = (tmp_path / ".conductor" / "mcp" / "servers.toml").read_text(encoding="utf-8")
    assert "../x" not in stored
    assert "pwned" not in stored


def test_keychain_reference_is_skipped_unless_allowed(tmp_path: Path) -> None:
    save_registry(
        tmp_path,
        Registry(
            servers={
                "sec": Server(
                    name="sec",
                    command="echo",
                    args=["a"],
                    env={"TOKEN": "keychain:svc/acct"},
                    targets=("cursor",),
                )
            }
        ),
    )
    preview = diff_all(tmp_path)
    assert preview["cursor"]["unified_diff"] == ""
    assert preview["cursor"]["skipped"]
    assert "keychain" in preview["cursor"]["skipped"][0]["reason"]
    assert not (tmp_path / ".cursor" / "mcp.json").exists()

    from src.mcp_hub import apply_all

    apply_all(tmp_path, apply=True, allow_inline_secret=["sec"])
    written = tmp_path / ".cursor" / "mcp.json"
    assert "keychain:svc/acct" in written.read_text(encoding="utf-8")
    assert written.stat().st_mode & 0o777 == 0o600


def test_check_server_does_not_spawn_or_return_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_registry(
        tmp_path,
        Registry(servers={"hubbed": Server(name="hubbed", command="uvx", env={"TOKEN": "${TOKEN}"})}),
    )

    def explode(*_args, **_kwargs):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(subprocess, "run", explode)
    idle = check_server(tmp_path, "hubbed")
    assert idle["status"] == "not-run"
    assert idle["tool_count"] is None
    assert "env" not in idle

    def runner(server: Server) -> dict:
        return {
            "tool_count": 3,
            "elapsed_ms": 12,
            "env": {"TOKEN": "supersecret-value"},
            "reason": f"saw {server.env['TOKEN']}",
        }

    result = check_server(tmp_path, "hubbed", runner=runner)
    assert result["tool_count"] == 3
    assert result["elapsed_ms"] == 12
    encoded = json.dumps(result)
    assert "supersecret-value" not in encoded
    assert "env" not in result
    assert "${TOKEN}" not in encoded
    status = list_status(tmp_path)
    assert status["servers"][0]["name"] == "hubbed"
    with pytest.raises(McpHubError):
        check_server(tmp_path, "missing")
