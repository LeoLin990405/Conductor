"""gemini adapter: ``~/.gemini/settings.json`` under ``mcpServers``.

All values below are synthetic. Every test runs under ``isolated_home``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from src.mcp_hub import (
    McpHubError,
    Registry,
    Server,
    apply_all,
    diff_all,
    import_existing,
    rollback,
    save_registry,
)
from src.mcp_hub.adapters.gemini import cannot, native
from src.mcp_hub.agents import agent_path
from src.mcp_hub.render import project_server
from src.mcp_hub.store import load_registry

HAND = (
    '"hand" : { "type": "stdio", "command" : "fake-hand-bin", "args" : ["stay"],'
    ' "trust": true, "includeTools": ["fake_tool"] }'
)
REMOTE_HAND = '"far" : { "type": "sse", "url": "http://127.0.0.1:39998/sse" }'
SEED = (
    "{\n"
    '  "security" : { "auth": { "selectedType": "FAKE-AUTH-TYPE" } },\n'
    '  "mcpServers": {\n'
    f"    {HAND},\n"
    f"    {REMOTE_HAND}\n"
    "  },\n"
    '  "mcp": { "excluded": ["fake-excluded"] },\n'
    '  "ui" : { "theme": "FAKE-THEME" }\n'
    "}\n"
)


def _path(home: Path) -> Path:
    return agent_path(home, "gemini")


def _write(home: Path, text: str, mode: int = 0o640) -> Path:
    path = _path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)
    return path


def _register(home: Path, *servers: Server) -> None:
    registry = load_registry(home)
    for server in servers:
        registry.servers[server.name] = server
    save_registry(home, Registry(servers=registry.servers))


def _servers(home: Path) -> dict:
    return json.loads(_path(home).read_text(encoding="utf-8"))["mcpServers"]


def _gemini(name: str = "gamma", **kw) -> Server:
    if kw.get("transport", "stdio") == "stdio":
        kw.setdefault("command", "fake-gamma")
        kw.setdefault("args", ["serve"])
    return Server(name=name, targets=("gemini",), **kw)


def test_spec_path(isolated_home: Path) -> None:
    assert _path(isolated_home) == isolated_home / ".gemini" / "settings.json"


def test_import_then_diff_is_empty(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    imported = import_existing(isolated_home)
    assert {"hand", "far"} <= set(imported)
    registry = load_registry(isolated_home)
    assert registry.servers["hand"].command == "fake-hand-bin"
    assert registry.servers["far"].transport == "sse"
    assert diff_all(isolated_home)["gemini"]["unified_diff"] == ""
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_apply_adds_server_then_rollback(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED, mode=0o640)
    _register(isolated_home, _gemini(env={"TOKEN": "${FAKE_TOKEN}"}))
    assert diff_all(isolated_home)["gemini"]["unified_diff"]
    result = apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    for fragment in (HAND, REMOTE_HAND, '"FAKE-AUTH-TYPE"', '"FAKE-THEME"'):
        assert fragment in text
    assert '"mcp": { "excluded": ["fake-excluded"] }' in text
    assert _servers(isolated_home)["gamma"] == {
        "command": "fake-gamma",
        "args": ["serve"],
        "env": {"TOKEN": "${FAKE_TOKEN}"},
    }
    assert diff_all(isolated_home)["gemini"]["unified_diff"] == ""

    rollback(isolated_home, result["snapshot_id"], apply=True)
    assert path.read_bytes() == SEED.encode("utf-8")
    assert path.stat().st_mode & 0o777 == 0o640


def test_remote_projection(isolated_home: Path) -> None:
    _write(isolated_home, SEED)
    _register(
        isolated_home,
        _gemini("web", transport="http", url="http://127.0.0.1:39997/mcp"),
        _gemini("stream", transport="sse", url="http://127.0.0.1:39996/sse"),
    )
    apply_all(isolated_home, apply=True)
    servers = _servers(isolated_home)
    assert servers["web"] == {"type": "http", "url": "http://127.0.0.1:39997/mcp"}
    assert servers["stream"] == {"type": "sse", "url": "http://127.0.0.1:39996/sse"}
    assert "httpUrl" not in _path(isolated_home).read_text(encoding="utf-8")
    assert diff_all(isolated_home)["gemini"]["unified_diff"] == ""


def test_timeout_maps_to_milliseconds() -> None:
    server = _gemini(overrides={"gemini": {"startup_timeout_sec": 30}})
    body, reason = project_server(server, "gemini")
    assert reason is None
    assert body == {"command": "fake-gamma", "args": ["serve"], "timeout": 30000}


def test_refusals() -> None:
    base = {
        "transport": "stdio",
        "command": "x",
        "args": [],
        "url": "",
        "env": {},
        "enabled": True,
        "startup_timeout_sec": None,
    }
    assert cannot("ok", base, set()) is None
    assert native(base) == {"command": "x"}
    assert "command" in cannot("a", {**base, "command": ""}, set())
    remote = {**base, "transport": "http", "command": "", "url": "http://h/mcp"}
    assert cannot("b", remote, set()) is None
    assert "env" in cannot("c", {**remote, "env": {"T": "${T}"}}, set())
    assert "url" in cannot("d", {**remote, "url": ""}, set())

    keychain = _gemini("kc", env={"T": "keychain:fake-svc/fake-acct"})
    body, reason = project_server(keychain, "gemini")
    assert body is None and "keychain" in reason
    body, reason = project_server(keychain, "gemini", allow={"kc"})
    assert reason is None and body["env"] == {"T": "keychain:fake-svc/fake-acct"}


def test_remove_deletes_only_owned_key(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    _register(isolated_home, _gemini())
    apply_all(isolated_home, apply=True)
    assert "gamma" in _servers(isolated_home)
    registry = load_registry(isolated_home)
    registry.servers.pop("gamma")
    save_registry(isolated_home, registry)
    apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    # Entries and siblings keep their bytes; the editor re-lays out only the
    # whitespace between server entries.
    assert json.loads(text) == json.loads(SEED)
    for fragment in (HAND, REMOTE_HAND, '"FAKE-AUTH-TYPE"', '"FAKE-THEME"'):
        assert fragment in text
    assert text.startswith(SEED.split('"mcpServers"')[0])
    assert text.endswith(SEED.split("\n  },\n", 1)[1])


def test_unmanaged_same_name_not_claimed(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    _register(isolated_home, _gemini("hand", command="fake-other"))
    view = diff_all(isolated_home)["gemini"]
    assert view["unified_diff"] == ""
    assert any("refusing to claim" in item["reason"] for item in view["skipped"])
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_fresh_file_created_private(isolated_home: Path) -> None:
    _register(isolated_home, _gemini())
    apply_all(isolated_home, apply=True)
    path = _path(isolated_home)
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "mcpServers": {"gamma": {"command": "fake-gamma", "args": ["serve"]}}
    }


def test_commented_settings_refused_untouched(isolated_home: Path) -> None:
    # gemini strips JSON comments when it loads settings; the hub's editor
    # does not, so it must refuse rather than rewrite the file.
    commented = SEED.replace('  "ui"', '  // fake user comment\n  "ui"')
    path = _write(isolated_home, commented)
    before = path.read_bytes()
    _register(isolated_home, _gemini())
    try:
        result = apply_all(isolated_home, apply=True)
    except McpHubError:
        result = None
    else:
        assert "gemini" not in str(result.get("applied", ""))
    assert path.read_bytes() == before
    with pytest.raises(McpHubError):
        from src.mcp_hub.render import render_agent

        render_agent(isolated_home, "gemini")
