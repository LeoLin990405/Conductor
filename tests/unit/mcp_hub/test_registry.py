"""Registry names, env references, and the servers.toml file."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.mcp_hub import McpHubError, Registry, Server, load_registry, save_registry


def test_rejects_injected_and_path_names() -> None:
    with pytest.raises(McpHubError):
        Server(name='foo"\n[mcp_servers.pwned]', command="echo")
    with pytest.raises(McpHubError):
        Server(name="../x", command="echo")
    with pytest.raises(McpHubError):
        Server(name="", command="echo")


def test_rejects_plaintext_env_and_accepts_references() -> None:
    with pytest.raises(McpHubError):
        Server(name="ok", command="echo", env={"TOKEN": "supersecret"})
    server = Server(
        name="ok",
        command="echo",
        env={"TOKEN": "${TOKEN}", "OTHER": "keychain:svc/acct"},
    )
    assert server.env["TOKEN"] == "${TOKEN}"
    assert server.env["OTHER"] == "keychain:svc/acct"


def test_registry_roundtrip_and_mode(tmp_path: Path) -> None:
    server = Server(
        name="hubbed",
        command="uvx",
        args=["pkg"],
        env={"TOKEN": "${TOKEN}"},
        targets=("claude", "codex"),
        overrides={"stepfun": {"startup_timeout_sec": 15}},
    )
    path = save_registry(tmp_path, Registry(servers={"hubbed": server}))
    assert path.stat().st_mode & 0o777 == 0o600
    loaded = load_registry(tmp_path)
    assert loaded.servers["hubbed"].command == "uvx"
    assert loaded.servers["hubbed"].args == ("pkg",)
    assert loaded.servers["hubbed"].env == {"TOKEN": "${TOKEN}"}
    assert loaded.servers["hubbed"].targets == ("claude", "codex")
    assert loaded.servers["hubbed"].overrides["stepfun"]["startup_timeout_sec"] == 15
    assert "supersecret" not in path.read_text(encoding="utf-8")

    os.chmod(path, 0o640)
    save_registry(tmp_path, Registry(servers={"hubbed": server}))
    assert path.stat().st_mode & 0o777 == 0o640


def test_load_missing_registry_is_empty(tmp_path: Path) -> None:
    assert load_registry(tmp_path).servers == {}
    assert not (tmp_path / ".conductor").exists()
