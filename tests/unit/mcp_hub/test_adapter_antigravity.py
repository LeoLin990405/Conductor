"""antigravity adapter: ``.gemini/config/mcp_config.json`` under ``mcpServers``.

All values below are synthetic. Every test runs under ``isolated_home``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.mcp_hub import (
    Registry,
    Server,
    apply_all,
    diff_all,
    import_existing,
    rollback,
    save_registry,
)
from src.mcp_hub.adapters.antigravity import cannot, native
from src.mcp_hub.agents import agent_path
from src.mcp_hub.store import load_registry

HAND_STDIO = '{ "command" : "fake-ag-hand", "args" : ["--keep"], "type" : "stdio" }'
HAND_REMOTE = '{ "url" : "https://mcp.example.invalid/mcp" }'
SEED = (
    "{\n"
    '  "zzKeep" : "AG-ODD-SIBLING",\n'
    '  "mcpServers": {\n'
    f'    "handstdio": {HAND_STDIO},\n'
    f'    "handremote": {HAND_REMOTE}\n'
    "  },\n"
    '  "trailer" : [1, 2 ,3]\n'
    "}\n"
)
GEMINI_SETTINGS = '{ "theme" : "GEMINI-CLI-ODD", "mcpServers" : {} }\n'


def _path(home: Path) -> Path:
    return agent_path(home, "antigravity")


def _write(home: Path, text: str = SEED, mode: int = 0o600) -> Path:
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


def _ag(name: str = "gamma", **kw) -> Server:
    if "url" not in kw:
        kw.setdefault("command", "fake-ag-gamma")
        kw.setdefault("args", ["serve"])
    return Server(name=name, targets=("antigravity",), **kw)


def _eff(**kw) -> dict:
    base = {
        "transport": "stdio",
        "command": "",
        "args": [],
        "url": "",
        "env": {},
        "enabled": True,
        "startup_timeout_sec": None,
    }
    base.update(kw)
    return base


def test_spec_path(isolated_home: Path) -> None:
    assert _path(isolated_home) == isolated_home / ".gemini" / "config" / "mcp_config.json"


def test_import_then_diff_is_empty(isolated_home: Path) -> None:
    path = _write(isolated_home)
    imported = import_existing(isolated_home)
    assert {"handstdio", "handremote"} <= set(imported)
    registry = load_registry(isolated_home)
    assert registry.servers["handstdio"].command == "fake-ag-hand"
    assert registry.servers["handstdio"].args == ("--keep",)
    assert registry.servers["handremote"].transport == "http"
    assert registry.servers["handremote"].url == "https://mcp.example.invalid/mcp"
    view = diff_all(isolated_home)["antigravity"]
    assert view["unified_diff"] == ""
    assert view["skipped"] == []
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_apply_adds_server_preserving_everything_else(isolated_home: Path) -> None:
    path = _write(isolated_home, mode=0o640)
    gemini = isolated_home / ".gemini" / "settings.json"
    gemini.write_text(GEMINI_SETTINGS, encoding="utf-8")
    _register(isolated_home, _ag(env={"TOKEN": "${FAKE_AG_TOKEN}"}))
    assert diff_all(isolated_home)["antigravity"]["unified_diff"]
    apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    assert f'"handstdio": {HAND_STDIO}' in text
    assert f'"handremote": {HAND_REMOTE}' in text
    assert '"zzKeep" : "AG-ODD-SIBLING"' in text
    assert '"trailer" : [1, 2 ,3]' in text
    assert _servers(isolated_home)["gamma"] == {
        "command": "fake-ag-gamma",
        "args": ["serve"],
        "env": {"TOKEN": "${FAKE_AG_TOKEN}"},
    }
    assert gemini.read_text(encoding="utf-8") == GEMINI_SETTINGS
    assert diff_all(isolated_home)["antigravity"]["unified_diff"] == ""

    rollback(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED
    assert path.stat().st_mode & 0o777 == 0o640
    assert gemini.read_text(encoding="utf-8") == GEMINI_SETTINGS


def test_remote_projection_writes_url(isolated_home: Path) -> None:
    _write(isolated_home)
    _register(
        isolated_home,
        _ag("far", transport="http", url="https://far.example.invalid/mcp"),
    )
    apply_all(isolated_home, apply=True)
    assert _servers(isolated_home)["far"] == {"url": "https://far.example.invalid/mcp"}
    assert diff_all(isolated_home)["antigravity"]["unified_diff"] == ""
    assert native(_eff(transport="http", url="https://x.invalid/mcp")) == {
        "url": "https://x.invalid/mcp"
    }


def test_refusals() -> None:
    sse = _eff(transport="sse", url="https://x.invalid/sse")
    assert "SSE" in (cannot("s", sse, set()) or "")
    remote_env = _eff(transport="http", url="https://x.invalid/mcp", env={"A": "${A}"})
    assert cannot("r", remote_env, set())
    remote_args = _eff(transport="http", url="https://x.invalid/mcp", args=["x"])
    assert cannot("r", remote_args, set())
    keychain = _eff(command="c", env={"K": "keychain:svc/acct"})
    assert "keychain" in (cannot("k", keychain, set()) or "")
    assert cannot("k", keychain, {"k"}) is None
    assert cannot("n", _eff(), set())
    assert cannot("ok", _eff(command="c", env={"A": "${A}"}), set()) is None


def test_keychain_skipped_without_allow(isolated_home: Path) -> None:
    path = _write(isolated_home)
    _register(isolated_home, _ag("kc", env={"K": "keychain:fake-svc/fake-acct"}))
    view = diff_all(isolated_home)["antigravity"]
    assert any(item["server"] == "kc" for item in view["skipped"])
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_sse_server_is_skipped(isolated_home: Path) -> None:
    path = _write(isolated_home)
    _register(isolated_home, _ag("old", transport="sse", url="https://o.invalid/sse"))
    view = diff_all(isolated_home)["antigravity"]
    assert any("SSE" in item["reason"] for item in view["skipped"])
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_remove_deletes_only_owned_key(isolated_home: Path) -> None:
    path = _write(isolated_home)
    _register(isolated_home, _ag())
    apply_all(isolated_home, apply=True)
    assert "gamma" in _servers(isolated_home)
    registry = load_registry(isolated_home)
    registry.servers.pop("gamma")
    save_registry(isolated_home, registry)
    apply_all(isolated_home, apply=True)
    assert set(_servers(isolated_home)) == {"handstdio", "handremote"}
    text = path.read_text(encoding="utf-8")
    assert f'"handstdio": {HAND_STDIO}' in text
    assert f'"handremote": {HAND_REMOTE}' in text
    assert '"zzKeep" : "AG-ODD-SIBLING"' in text


def test_unmanaged_same_name_is_not_claimed(isolated_home: Path) -> None:
    path = _write(isolated_home)
    _register(isolated_home, _ag("handstdio", command="hub-other"))
    view = diff_all(isolated_home)["antigravity"]
    assert any("refusing to claim" in item["reason"] for item in view["skipped"])
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_antigravity_only_apply_leaves_gemini_settings(isolated_home: Path) -> None:
    gemini = isolated_home / ".gemini" / "settings.json"
    gemini.parent.mkdir(parents=True)
    gemini.write_text(GEMINI_SETTINGS, encoding="utf-8")
    os.chmod(gemini, 0o600)
    before = gemini.read_bytes()
    _register(isolated_home, _ag())
    apply_all(isolated_home, apply=True)
    assert gemini.read_bytes() == before
    assert _servers(isolated_home)["gamma"]["command"] == "fake-ag-gamma"
