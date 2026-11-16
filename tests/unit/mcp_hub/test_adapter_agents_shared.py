"""agents adapter: the shared cross-tool ``~/.agents/mcp.json``.

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
from src.mcp_hub.agents import agent_path
from src.mcp_hub.store import load_registry

AGENT = "agents"

SEED = (
    "{\n"
    '  "$comment" : "FAKE shared file",\n'
    '  "mcpServers":   {\n'
    '      "alpha" : { "command" : "fake-alpha-bin", "args" : ["--flag", "x"] },\n'
    '    "beta": {"command": "fake-beta", "args": [], "env": {"B_KEY": "${B_KEY}"}}\n'
    "  },\n"
    '  "other":{"keep" : [1, 2]}\n'
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


def _shared(name: str = "gamma", **kw) -> Server:
    kw.setdefault("command", "fake-gamma")
    kw.setdefault("args", ["serve"])
    return Server(name=name, targets=(AGENT,), **kw)


def _data(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _skips(home: Path, **kw) -> dict[str, str]:
    view = diff_all(home, **kw)[AGENT]
    return {row["server"]: row["reason"] for row in view["skipped"]}


def _neighbours(home: Path) -> dict[Path, bytes]:
    skill = home / ".agents" / "skills" / "fake-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: fake-skill\n---\nFAKE\n", encoding="utf-8")
    guide = home / ".agents" / "AGENTS.md"
    guide.write_text("# FAKE shared instructions\n", encoding="utf-8")
    return {skill: skill.read_bytes(), guide: guide.read_bytes()}


def test_path_is_shared_agents_mcp_json(isolated_home: Path) -> None:
    assert _path(isolated_home) == isolated_home / ".agents" / "mcp.json"


def test_import_then_diff_is_empty(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    imported = import_existing(isolated_home)
    assert {"alpha", "beta"} <= set(imported)
    registry = load_registry(isolated_home)
    assert registry.servers["alpha"].command == "fake-alpha-bin"
    assert registry.servers["alpha"].args == ("--flag", "x")
    assert registry.servers["beta"].env == {"B_KEY": "${B_KEY}"}
    assert diff_all(isolated_home)[AGENT]["unified_diff"] == ""
    apply_all(isolated_home, apply=True)
    assert path.read_text(encoding="utf-8") == SEED


def test_apply_adds_server_then_rollback(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED, mode=0o640)
    before = path.read_bytes()
    neighbours = _neighbours(isolated_home)
    _register(isolated_home, _shared())
    assert diff_all(isolated_home)[AGENT]["unified_diff"]
    apply_all(isolated_home, apply=True)

    data = _data(path)
    assert data["mcpServers"]["gamma"] == {"command": "fake-gamma", "args": ["serve"]}
    text = path.read_text(encoding="utf-8")
    assert '"alpha" : { "command" : "fake-alpha-bin", "args" : ["--flag", "x"] }' in text
    assert (
        '"beta": {"command": "fake-beta", "args": [], "env": {"B_KEY": "${B_KEY}"}}'
        in text
    )
    assert text.startswith(SEED.split('"mcpServers"')[0])
    assert text.endswith('  "other":{"keep" : [1, 2]}\n}\n')
    assert diff_all(isolated_home)[AGENT]["unified_diff"] == ""

    rollback(isolated_home, apply=True)
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o640
    for neighbour, raw in neighbours.items():
        assert neighbour.read_bytes() == raw


def test_apply_creates_file_next_to_skills(isolated_home: Path) -> None:
    neighbours = _neighbours(isolated_home)
    path = _path(isolated_home)
    _register(isolated_home, _shared())
    apply_all(isolated_home, apply=True)
    assert _data(path) == {
        "mcpServers": {"gamma": {"command": "fake-gamma", "args": ["serve"]}}
    }
    for neighbour, raw in neighbours.items():
        assert neighbour.read_bytes() == raw
    rollback(isolated_home, apply=True)
    assert not path.exists()
    for neighbour, raw in neighbours.items():
        assert neighbour.read_bytes() == raw
    assert sorted(p.name for p in (isolated_home / ".agents").iterdir()) == [
        "AGENTS.md",
        "skills",
    ]


def test_remove_deletes_only_owned_key(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    import_existing(isolated_home)
    _register(isolated_home, _shared())
    apply_all(isolated_home, apply=True)
    assert "gamma" in _data(path)["mcpServers"]

    registry = load_registry(isolated_home)
    registry.servers.pop("gamma")
    save_registry(isolated_home, Registry(servers=registry.servers))
    apply_all(isolated_home, apply=True)
    data = _data(path)
    assert set(data["mcpServers"]) == {"alpha", "beta"}
    assert data == json.loads(SEED)
    assert diff_all(isolated_home)[AGENT]["unified_diff"] == ""


def test_unmanaged_same_name_not_claimed(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    before = path.read_bytes()
    _register(isolated_home, _shared("alpha", command="fake-other"))
    reason = _skips(isolated_home)["alpha"]
    assert "refusing to claim" in reason
    apply_all(isolated_home, apply=True)
    assert path.read_bytes() == before


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
    assert "stdio" in reason
    apply_all(isolated_home, apply=True)
    assert path.read_bytes() == before


def test_env_ref_refused_unless_allowed(isolated_home: Path) -> None:
    path = _write(isolated_home, SEED)
    before = path.read_bytes()
    _register(isolated_home, _shared(env={"TOKEN": "${FAKE_TOKEN}"}))
    reason = _skips(isolated_home)["gamma"]
    assert "TOKEN" in reason and "literally" in reason
    apply_all(isolated_home, apply=True)
    assert path.read_bytes() == before

    assert "gamma" not in _skips(isolated_home, allow_inline_secret=["gamma"])
    apply_all(isolated_home, apply=True, allow_inline_secret=["gamma"])
    assert _data(path)["mcpServers"]["gamma"]["env"] == {"TOKEN": "${FAKE_TOKEN}"}


def test_symlinked_agents_dir_outside_home_refused(
    isolated_home: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-agents"
    outside.mkdir()
    target = outside / "mcp.json"
    target.write_text(SEED, encoding="utf-8")
    os.chmod(target, 0o644)
    (isolated_home / ".agents").symlink_to(outside, target_is_directory=True)
    _register(isolated_home, _shared())
    before = target.read_bytes()

    with pytest.raises(McpHubError):
        apply_all(isolated_home, apply=True)

    assert target.read_bytes() == before
    assert sorted(p.name for p in outside.iterdir()) == ["mcp.json"]
    assert (isolated_home / ".agents").is_symlink()
