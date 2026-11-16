"""Regressions for skills-mcp fix round 7.

Ownership decisions replay the journal into memory. A sidecar that cannot be
written, or that has been deleted, does not choose what apply releases.
``import_existing`` appends an ``imported`` event so recover keeps the claim.
"""

from __future__ import annotations

import json
from pathlib import Path

from src.mcp_hub import Registry, Server, apply_all, import_existing, rollback, save_registry
from src.mcp_hub.apply import recover
from src.mcp_hub.journal import entry_for, snapshot_ids
from src.mcp_hub.store import load_managed, load_registry, managed_path


def _registry(*names: str, agents: tuple[str, ...] = ("claude",)) -> Registry:
    return Registry(
        servers={
            name: Server(name=name, command=f"cmd-{name}", args=[], targets=agents)
            for name in names
        }
    )


def _servers(path: Path) -> dict:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("mcpServers") or {}
    return raw if isinstance(raw, dict) else {}


def _write_servers(path: Path, servers: dict) -> None:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = {}
    payload["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_persistent_sidecar_failure_keeps_a_user_owned_name(tmp_path: Path) -> None:
    """A failed cache write does not raise, and a user-owned name survives.

    A rollback with a failing sidecar still restores the bytes and marks the
    journal. A later empty-registry apply releases the owned ``base`` key but
    leaves the user's recreated ``hubbed`` key alone.
    """
    save_registry(tmp_path, _registry("base"))
    apply_all(tmp_path, apply=True)
    save_registry(tmp_path, _registry("base", "hubbed"))
    second = apply_all(tmp_path, apply=True)
    claude = tmp_path / ".claude.json"
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic persistent sidecar failure")

    apply_mod.save_managed = fail
    try:
        rolled = rollback(tmp_path, second["snapshot_id"], apply=True)
        assert rolled["snapshot_id"] == second["snapshot_id"]
        assert entry_for(tmp_path, second["snapshot_id"], "claude")["state"] == (
            "rolled_back"
        )
        stale = sorted(load_managed(tmp_path)["claude"])
        assert stale == ["base", "hubbed"]
        servers = _servers(claude)
        servers["hubbed"] = {"command": "user-owned-tool", "args": []}
        _write_servers(claude, servers)
        save_registry(tmp_path, Registry())
        before = claude.read_bytes()
        apply_all(tmp_path, apply=True)
    finally:
        apply_mod.save_managed = real_save

    # base is owned and its server was removed, so apply releases it. hubbed
    # is user-owned after the rollback, so it stays.
    assert claude.read_bytes() != before
    final = _servers(claude)
    assert "base" not in final
    assert final["hubbed"]["command"] == "user-owned-tool"
    assert sorted(load_managed(tmp_path)["claude"]) == ["base", "hubbed"]


def test_deleted_sidecar_does_not_block_a_release(tmp_path: Path) -> None:
    """A missing cache does not hide a removed server. Apply still releases it.

    The journal is the truth, so the owned key is deleted even though the cache
    write fails; the failure is reported as a warning, not raised.
    """
    save_registry(tmp_path, _registry("hubbed"))
    apply_all(tmp_path, apply=True)
    claude = tmp_path / ".claude.json"
    assert "hubbed" in _servers(claude)
    managed_path(tmp_path).unlink()
    save_registry(tmp_path, Registry())
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic persistent sidecar failure")

    apply_mod.save_managed = fail
    try:
        result = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.save_managed = real_save

    assert "hubbed" not in _servers(claude)
    assert result["diffs"]["claude"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    assert result["warnings"]
    assert not managed_path(tmp_path).exists()


def test_import_claim_survives_recover_and_registry_removal(tmp_path: Path) -> None:
    """Import ownership is a journal event, for more than one agent."""
    agents = ("claude", "cursor")
    save_registry(tmp_path, _registry("base", agents=agents))
    apply_all(tmp_path, apply=True)
    paths = {
        "claude": tmp_path / ".claude.json",
        "cursor": tmp_path / ".cursor" / "mcp.json",
    }
    for path in paths.values():
        servers = _servers(path)
        servers["imported"] = {"command": "echo", "args": ["user-tool"]}
        _write_servers(path, servers)

    imported = import_existing(tmp_path)
    assert "imported" in imported
    assert sorted(load_managed(tmp_path)["claude"]) == ["base", "imported"]
    assert sorted(load_managed(tmp_path)["cursor"]) == ["base", "imported"]
    for agent in agents:
        found = _imported_event(tmp_path, agent)
        assert found is not None
        assert found["state"] == "imported"
        assert set(found["owned"]) == {"base", "imported"}

    recover(tmp_path)
    assert sorted(load_managed(tmp_path)["claude"]) == ["base", "imported"]
    assert sorted(load_managed(tmp_path)["cursor"]) == ["base", "imported"]
    managed_path(tmp_path).unlink()
    recover(tmp_path)
    assert sorted(load_managed(tmp_path)["claude"]) == ["base", "imported"]
    assert sorted(load_managed(tmp_path)["cursor"]) == ["base", "imported"]

    registry = load_registry(tmp_path)
    current = registry.servers["imported"]
    registry.servers["imported"] = Server(
        name="imported",
        command="echo-changed",
        args=list(current.args),
        targets=current.targets,
    )
    save_registry(tmp_path, registry)
    apply_all(tmp_path, apply=True)
    for path in paths.values():
        body = _servers(path)["imported"]
        assert body["command"] == "echo-changed"

    registry = load_registry(tmp_path)
    registry.servers.pop("imported")
    save_registry(tmp_path, registry)
    apply_all(tmp_path, apply=True)
    for agent, path in paths.items():
        servers = _servers(path)
        # The imported server was removed, so its owned key is released.
        assert "imported" not in servers
        assert "base" in servers


def _imported_event(home: Path, agent: str) -> dict | None:
    for snapshot_id in reversed(snapshot_ids(home)):
        found = entry_for(home, snapshot_id, agent)
        if found is not None and found.get("state") == "imported":
            return found
    return None
