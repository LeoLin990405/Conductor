"""Qwen Code adapter: ``~/.qwen/settings.json`` ``mcpServers``.

All values are synthetic. Every test runs under ``isolated_home``.
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
from src.mcp_hub.adapters.qwen import generic, native
from src.mcp_hub.agents import agent_path
from src.mcp_hub.render import project_server
from src.mcp_hub.store import load_registry

HEAD = '{\n  "$version" : 4,\n  "general": { "vimMode" : true },\n  "mcpServers": '
HAND_A = '"alpha": { "command" : "fake-alpha-bin", "args" : ["--flag", "x"] }'
HAND_B = (
    '"beta": {\n      "command": "fake-beta",\n      "args": [],\n'
    '      "env": { "FAKE_TOKEN": "${FAKE_TOKEN}" }\n    }'
)
TAIL = ',\n  "ui" : { "theme" : "FAKE-THEME" }\n}\n'
SEED = HEAD + "{\n    " + HAND_A + ",\n    " + HAND_B + "\n  }" + TAIL


def _path(home: Path) -> Path:
    return agent_path(home, "qwen")


def _write(home: Path, text: str, mode: int = 0o600) -> Path:
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


def _qwen(name: str = "gamma", **kw) -> Server:
    kw.setdefault("command", "fake-gamma")
    kw.setdefault("args", ["serve"])
    return Server(name=name, targets=("qwen",), **kw)


def _servers(home: Path) -> dict:
    return json.loads(_path(home).read_text(encoding="utf-8"))["mcpServers"]


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


def _assert_siblings_kept(text: str) -> None:
    assert text.startswith(HEAD)
    assert text.endswith(TAIL)
    assert HAND_A in text
    assert HAND_B in text


def test_spec_path(isolated_home: Path) -> None:
    assert _path(isolated_home) == isolated_home / ".qwen" / "settings.json"


def test_import_then_diff_is_empty(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    imported = import_existing(isolated_home)
    assert {"alpha", "beta"} <= set(imported)
    registry = load_registry(isolated_home)
    assert registry.servers["alpha"].command == "fake-alpha-bin"
    assert registry.servers["alpha"].args == ("--flag", "x")
    assert registry.servers["beta"].env == {"FAKE_TOKEN": "${FAKE_TOKEN}"}
    assert diff_all(isolated_home)["qwen"]["unified_diff"] == ""
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_apply_adds_server_then_rollback_restores_bytes(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED, mode=0o640)
    before = path.read_bytes()
    _register(isolated_home, _qwen(env={"FAKE_KEY": "${FAKE_KEY}"}))
    assert diff_all(isolated_home)["qwen"]["unified_diff"]
    result = apply_all(isolated_home, apply=True)
    snapshot_id = result["snapshot_id"]
    assert snapshot_id
    text = path.read_text(encoding="utf-8")
    _assert_siblings_kept(text)
    data = json.loads(text)
    assert data["$version"] == 4
    assert data["mcpServers"]["gamma"] == {
        "command": "fake-gamma",
        "args": ["serve"],
        "env": {"FAKE_KEY": "${FAKE_KEY}"},
    }
    assert diff_all(isolated_home)["qwen"]["unified_diff"] == ""

    rollback(isolated_home, snapshot_id, apply=True)
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o640


def test_fresh_file_is_created_private(isolated_home: Path) -> None:
    _register(isolated_home, _qwen())
    apply_all(isolated_home, apply=True)
    path = _path(isolated_home)
    assert path.stat().st_mode & 0o777 == 0o600
    assert _servers(isolated_home) == {
        "gamma": {"command": "fake-gamma", "args": ["serve"]}
    }


def test_remote_projection(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    _register(
        isolated_home,
        _qwen("web", transport="http", url="http://127.0.0.1:39999/mcp", command="",
              args=[]),
        _qwen("events", transport="sse", url="http://127.0.0.1:39998/sse", command="",
              args=[]),
    )
    apply_all(isolated_home, apply=True)
    servers = _servers(isolated_home)
    assert servers["web"] == {"httpUrl": "http://127.0.0.1:39999/mcp"}
    assert servers["events"] == {"url": "http://127.0.0.1:39998/sse"}
    _assert_siblings_kept(path.read_text(encoding="utf-8"))
    assert diff_all(isolated_home)["qwen"]["unified_diff"] == ""


def test_native_never_writes_enabled_or_type() -> None:
    stdio = native(_eff(command="c", args=["a"]))
    assert stdio == {"command": "c", "args": ["a"]}
    http = native(_eff(transport="http", url="http://127.0.0.1:1/mcp"))
    assert http == {"httpUrl": "http://127.0.0.1:1/mcp"}
    sse = native(_eff(transport="sse", url="http://127.0.0.1:1/sse"))
    assert sse == {"url": "http://127.0.0.1:1/sse"}


def test_generic_mirrors_qwen_precedence() -> None:
    assert generic({"httpUrl": "http://h/mcp", "url": "http://s", "command": "c"}) == {
        "transport": "http",
        "url": "http://h/mcp",
    }
    assert generic({"url": "http://s/sse", "command": "c"}) == {
        "transport": "sse",
        "url": "http://s/sse",
    }
    assert generic({"command": "c", "args": ["a"], "env": {"K": "${K}"}, "trust": True}) == {
        "transport": "stdio",
        "command": "c",
        "args": ["a"],
        "env": {"K": "${K}"},
    }


def test_refusals() -> None:
    keychain = _qwen("kc", env={"TOKEN": "keychain:fake-svc/fake-acct"})
    body, reason = project_server(keychain, "qwen")
    assert body is None and "keychain" in (reason or "")
    body, reason = project_server(keychain, "qwen", allow={"kc"})
    assert reason is None and body is not None

    remote_env = _qwen(
        "re", transport="http", url="http://127.0.0.1:1/mcp", command="", args=[],
        env={"K": "${K}"},
    )
    assert project_server(remote_env, "qwen")[0] is None
    remote_args = _qwen(
        "ra", transport="sse", url="http://127.0.0.1:1/sse", command="", args=["x"]
    )
    assert project_server(remote_args, "qwen")[0] is None


def test_remove_deletes_only_owned_key(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    _register(isolated_home, _qwen())
    apply_all(isolated_home, apply=True)
    assert "gamma" in _servers(isolated_home)
    registry = load_registry(isolated_home)
    registry.servers.pop("gamma")
    save_registry(isolated_home, registry)
    apply_all(isolated_home, apply=True)
    text = path.read_text(encoding="utf-8")
    _assert_siblings_kept(text)
    assert set(_servers(isolated_home)) == {"alpha", "beta"}
    assert json.loads(text) == json.loads(SEED)


def test_disabled_server_key_is_deleted(isolated_home: Path) -> None:
    _write(isolated_home, SEED)
    _register(isolated_home, _qwen())
    apply_all(isolated_home, apply=True)
    registry = load_registry(isolated_home)
    gamma = registry.servers["gamma"]
    registry.servers["gamma"] = _qwen(command=gamma.command, args=list(gamma.args),
                                      enabled=False)
    save_registry(isolated_home, registry)
    apply_all(isolated_home, apply=True)
    assert "gamma" not in _servers(isolated_home)
    assert "enabled" not in _path(isolated_home).read_text(encoding="utf-8")


def test_unmanaged_same_name_is_not_claimed(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    _register(isolated_home, _qwen("alpha", command="hub-alpha"))
    result = diff_all(isolated_home)["qwen"]
    assert any("refusing to claim" in item["reason"] for item in result["skipped"])
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


@pytest.mark.parametrize(
    "text",
    [
        '{\n  // qwen allows comments\n  "$version": 4,\n  "mcpServers": {}\n}\n',
        '{\n  "$version": 4,\n  /* block */ "mcpServers": {}\n}\n',
        '{ "$version": 4, "mcpServers": { ',
    ],
)
def test_commented_or_invalid_json_is_refused(isolated_home: Path, text: str) -> None:
    path = _write(isolated_home, text)
    before = path.read_bytes()
    import_existing(isolated_home)
    _register(isolated_home, _qwen())
    with pytest.raises(McpHubError):
        apply_all(isolated_home, apply=True)
    assert path.read_bytes() == before
