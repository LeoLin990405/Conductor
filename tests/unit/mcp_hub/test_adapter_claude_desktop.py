"""Claude desktop adapter: ``Library/Application Support/Claude/claude_desktop_config.json``.

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
from src.mcp_hub.agents import agent_path
from src.mcp_hub.store import load_registry

AGENT = "claude-desktop"

# Odd whitespace, a big nested ``preferences`` sibling and account-keyed
# siblings, like the file the desktop app keeps rewriting.
SEED = (
    "{\n"
    '  "preferences" :{\n'
    '      "sidebarMode":"FAKE-MODE",  "quickEntryShortcut" : "off",\n'
    '    "nested": {"deep": {"deeper": [1, 2, {"x" : "FAKE-DEEP"}], "flag":true}},\n'
    '\t"list" : [ "a" ,"b" ]\n'
    "  },\n"
    '  "mcpServers":   {\n'
    '      "alpha" : { "command" : "fake-alpha-bin", "args" : ["--flag", "x"] }\n'
    "  },\n"
    '  "00000000-fake-account-key" : {"seen": true, "count" : 3},\n'
    '  "theme":"FAKE-THEME"\n'
    "}\n"
)


def _path(home: Path) -> Path:
    return agent_path(home, AGENT)


def _write(home: Path, text: str, mode: int = 0o644) -> Path:
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


def _desktop(name: str = "gamma", **kw) -> Server:
    kw.setdefault("command", "fake-gamma")
    kw.setdefault("args", ["serve"])
    return Server(name=name, targets=(AGENT,), **kw)


def _data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _without_servers(data: dict) -> dict:
    return {key: value for key, value in data.items() if key != "mcpServers"}


def _skips(home: Path, **kw) -> dict[str, str]:
    view = diff_all(home, **kw)[AGENT]
    return {row["server"]: row["reason"] for row in view["skipped"]}


def test_path_has_space_under_library(isolated_home: Path) -> None:
    path = _path(isolated_home)
    assert path == (
        isolated_home
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json"
    )
    assert " " in str(path)


def test_import_then_diff_is_empty(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    imported = import_existing(isolated_home)
    assert "alpha" in imported
    server = load_registry(isolated_home).servers["alpha"]
    assert server.command == "fake-alpha-bin"
    assert server.args == ("--flag", "x")
    assert diff_all(isolated_home)[AGENT]["unified_diff"] == ""
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_apply_writes_only_mcp_servers_then_rollback(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED, mode=0o640)
    before = path.read_bytes()
    _register(isolated_home, _desktop())
    assert diff_all(isolated_home)[AGENT]["unified_diff"]
    apply_all(isolated_home, apply=True)

    after = _data(path)
    assert after["mcpServers"]["gamma"] == {"command": "fake-gamma", "args": ["serve"]}
    assert after["mcpServers"]["alpha"] == {
        "command": "fake-alpha-bin",
        "args": ["--flag", "x"],
    }
    assert _without_servers(after) == _without_servers(json.loads(before))
    text = path.read_text(encoding="utf-8")
    head = SEED.split('"mcpServers"')[0]
    tail = SEED.split('"00000000-fake-account-key"')[1]
    assert text.startswith(head)
    assert text.endswith(tail)
    assert '"alpha" : { "command" : "fake-alpha-bin", "args" : ["--flag", "x"] }' in text
    assert diff_all(isolated_home)[AGENT]["unified_diff"] == ""

    rollback(isolated_home, apply=True)
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o640


def test_apply_creates_file_under_spaced_dir(isolated_home: Path) -> None:
    path = _path(isolated_home)
    _register(isolated_home, _desktop())
    apply_all(isolated_home, apply=True)
    assert _data(path)["mcpServers"]["gamma"]["command"] == "fake-gamma"
    rollback(isolated_home, apply=True)
    assert not path.exists()


def test_remote_server_skipped_with_reason(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    before = path.read_bytes()
    _register(
        isolated_home,
        Server(
            name="remote",
            transport="http",
            url="https://fake.example.invalid/mcp",
            targets=(AGENT,),
        ),
    )
    reason = _skips(isolated_home)["remote"]
    assert "stdio" in reason and "connector" in reason
    apply_all(isolated_home, apply=True)
    assert path.read_bytes() == before


def test_env_ref_refused_unless_allowed(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    before = path.read_bytes()
    _register(isolated_home, _desktop(env={"TOKEN": "${FAKE_TOKEN}"}))
    reason = _skips(isolated_home)["gamma"]
    assert "TOKEN" in reason and "literally" in reason
    apply_all(isolated_home, apply=True)
    assert path.read_bytes() == before

    assert "gamma" not in _skips(isolated_home, allow_inline_secret=["gamma"])
    apply_all(isolated_home, apply=True, allow_inline_secret=["gamma"])
    assert _data(path)["mcpServers"]["gamma"]["env"] == {"TOKEN": "${FAKE_TOKEN}"}


def test_remove_deletes_only_owned_key(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    import_existing(isolated_home)
    _register(isolated_home, _desktop())
    apply_all(isolated_home, apply=True)
    assert "gamma" in _data(path)["mcpServers"]

    registry = load_registry(isolated_home)
    registry.servers.pop("gamma")
    save_registry(isolated_home, Registry(servers=registry.servers))
    apply_all(isolated_home, apply=True)
    data = _data(path)
    assert set(data["mcpServers"]) == {"alpha"}
    assert data == json.loads(SEED)
    assert diff_all(isolated_home)[AGENT]["unified_diff"] == ""


def test_unmanaged_same_name_not_claimed(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    before = path.read_bytes()
    _register(isolated_home, _desktop("alpha", command="fake-other"))
    reason = _skips(isolated_home)["alpha"]
    assert "refusing to claim" in reason
    apply_all(isolated_home, apply=True)
    assert path.read_bytes() == before


def test_claude_code_file_untouched(isolated_home: Path) -> None:
    _write(isolated_home, SEED)
    code = isolated_home / ".claude.json"
    code.write_text(
        '{ "userID" : "FAKE-USER",\n  "mcpServers" : {"keep": {"command": "k"}} }\n',
        encoding="utf-8",
    )
    os.chmod(code, 0o600)
    before = code.read_bytes()
    stat = code.stat()
    _register(isolated_home, _desktop())
    apply_all(isolated_home, apply=True)
    assert "gamma" in _data(_path(isolated_home))["mcpServers"]
    assert code.read_bytes() == before
    assert code.stat().st_mtime_ns == stat.st_mtime_ns
    rollback(isolated_home, apply=True)
    assert code.read_bytes() == before
