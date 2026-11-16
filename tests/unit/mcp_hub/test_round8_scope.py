"""Regressions for skills-mcp fix round 8.

Apply releases owned keys. Removing a server, or disabling one on an agent
with no ``enabled`` field, deletes the owned key and records a ``released``
event. Rollback restores the snapshot's agent bytes and never reads the
managed sidecar.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from src.mcp_hub import Registry, Server, apply_all, rollback, save_registry
from src.mcp_hub.store import managed_path


def _servers(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("mcpServers") or {}
    return raw if isinstance(raw, dict) else {}


def _toml_servers(path: Path) -> dict:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    raw = data.get("mcp_servers") or {}
    return raw if isinstance(raw, dict) else {}


def _write_servers(path: Path, servers: dict) -> None:
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = {}
    payload["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _journal_names(home: Path) -> list[str]:
    directory = home / ".conductor" / "mcp" / "journal"
    return sorted(path.name for path in directory.glob("*.json"))


def _claude(home: Path) -> Path:
    return home / ".claude.json"


def _codex(home: Path) -> Path:
    return home / ".codex" / "config.toml"


def test_noop_release_then_user_recreate_keeps_user_bytes(tmp_path: Path) -> None:
    """A key the user already deleted is not taken back when they recreate it."""
    save_registry(
        tmp_path,
        Registry(
            servers={
                "hubbed": Server(
                    name="hubbed",
                    command="echo",
                    args=["managed"],
                    targets=("claude",),
                )
            }
        ),
    )
    apply_all(tmp_path, apply=True)
    claude = _claude(tmp_path)
    servers = _servers(claude)
    del servers["hubbed"]
    _write_servers(claude, servers)

    save_registry(tmp_path, Registry())
    apply_all(tmp_path, apply=True)

    servers = _servers(claude)
    servers["hubbed"] = {"command": "user-owned-tool", "args": []}
    _write_servers(claude, servers)
    before = claude.read_bytes()
    apply_all(tmp_path, apply=True)

    assert claude.read_bytes() == before
    assert _servers(claude)["hubbed"]["command"] == "user-owned-tool"


@pytest.mark.parametrize("mode", ["remove", "disable"])
def test_registry_remove_and_disable_release_owned_keys(
    tmp_path: Path, mode: str
) -> None:
    """Remove and disable write the registry; apply releases the owned keys.

    claude and codex have no ``enabled`` field, so a disabled server is deleted
    just like a removed one, and each release is a new journal event.
    """
    registry = Registry(
        servers={
            "hubbed": Server(
                name="hubbed",
                command="echo",
                args=["managed"],
                targets=("claude", "codex"),
            )
        }
    )
    save_registry(tmp_path, registry)
    apply_all(tmp_path, apply=True)
    claude = _claude(tmp_path)
    codex = _codex(tmp_path)
    assert "hubbed" in _servers(claude)
    assert "hubbed" in _toml_servers(codex)
    journal_before = _journal_names(tmp_path)

    if mode == "remove":
        del registry.servers["hubbed"]
    else:
        registry.servers["hubbed"].enabled = False
    save_registry(tmp_path, registry)
    result = apply_all(tmp_path, apply=True)

    assert "hubbed" not in _servers(claude)
    assert "hubbed" not in _toml_servers(codex)
    assert result["applied"] is True
    assert result["snapshot_id"] is not None
    assert result["diffs"]["claude"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    assert result["diffs"]["codex"]["released"] == [
        {"key": "hubbed", "action": "delete"}
    ]
    assert _journal_names(tmp_path) != journal_before
    assert result["diffs"]["claude"]["unified_diff"]
    assert result["diffs"]["codex"]["unified_diff"]


def _publish_base_then_hubbed(home: Path) -> tuple[Path, bytes, dict]:
    """Publish ``base``, then ``base`` and ``hubbed``. Return the pre-image."""
    registry = Registry(
        servers={
            "base": Server(
                name="base", command="echo", args=["base"], targets=("claude",)
            )
        }
    )
    save_registry(home, registry)
    apply_all(home, apply=True)
    claude = _claude(home)
    before = claude.read_bytes()
    registry.servers["hubbed"] = Server(
        name="hubbed",
        command="echo",
        args=["managed"],
        targets=("claude",),
    )
    save_registry(home, registry)
    second = apply_all(home, apply=True)
    assert "hubbed" in _servers(claude)
    return claude, before, second


def _plant_sidecar(home: Path, kind: str, snapshot_id: str) -> None:
    """Break the live sidecar, and for two kinds the snapshot copy of it."""
    sidecar = managed_path(home)
    if kind == "live":
        sidecar.write_text("[broken\n", encoding="utf-8")
        return
    manifest_path = (
        home / ".conductor" / "mcp" / "snapshots" / snapshot_id / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][".conductor/mcp/managed.toml"] = {
        "existed": True,
        "mode": 0o600,
        "blob": "managed",
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    if kind == "snapshot_blob":
        (manifest_path.parent / "managed").write_bytes(b"not toml [[")
    sidecar.write_text("[broken\n", encoding="utf-8")


@pytest.mark.parametrize("kind", ["live", "snapshot_blob", "missing_blob"])
def test_rollback_restores_bytes_despite_sidecar(tmp_path: Path, kind: str) -> None:
    """A broken sidecar, live or in the snapshot, does not block restore."""
    claude, before, second = _publish_base_then_hubbed(tmp_path)
    _plant_sidecar(tmp_path, kind, second["snapshot_id"])

    result = rollback(tmp_path, second["snapshot_id"], apply=True)

    assert claude.read_bytes() == before
    assert result["snapshot_id"] == second["snapshot_id"]
    assert result["restored"]


def test_rollback_succeeds_when_sidecar_write_fails(tmp_path: Path) -> None:
    """A failed cache write after the restore does not fail the rollback."""
    claude, before, second = _publish_base_then_hubbed(tmp_path)
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic sidecar failure")

    apply_mod.save_managed = fail
    try:
        rolled = rollback(tmp_path, second["snapshot_id"], apply=True)
    finally:
        apply_mod.save_managed = real_save

    assert rolled["dry_run"] is False
    assert rolled["snapshot_id"] == second["snapshot_id"]
    assert claude.read_bytes() == before


def test_apply_module_never_parses_the_sidecar() -> None:
    """The best-effort sidecar write stays. Parsing it does not."""
    import src.mcp_hub.apply as apply_mod

    text = Path(apply_mod.__file__).read_text(encoding="utf-8")
    for name in ("managed_from_toml", "load_published", "load_managed"):
        assert name not in text
    assert "save_managed" in text
