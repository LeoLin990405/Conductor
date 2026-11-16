"""Regressions for skills-mcp fix round 4 (journal, CAS rollback).

A sidecar OSError used to report no success and leave rollback with no
post-hash. A ``before_replace_hook`` edit used to be overwritten. An older
snapshot used the latest sidecar hash and undid a later apply.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.conductor_cli.cli import main
from src.mcp_hub import Registry, Server, apply_all, recover, rollback, save_registry
from src.mcp_hub.journal import entry_for
from src.mcp_hub.store import load_managed, managed_path


def _registry(home: Path, arg: str = "one", *agents: str) -> None:
    chosen = agents or ("claude", "cursor")
    save_registry(
        home,
        Registry(
            servers={
                "hubbed": Server(
                    name="hubbed",
                    command="echo",
                    args=[arg],
                    targets=chosen,
                )
            }
        ),
    )


def _journal_text(home: Path, snapshot_id: str) -> dict:
    path = home / ".conductor" / "mcp" / "journal" / f"{snapshot_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _state(home: Path, snapshot_id: str, agent: str = "claude") -> str:
    found = entry_for(home, snapshot_id, agent)
    assert found is not None
    return str(found["state"])


def test_sidecar_failure_keeps_the_publish_and_the_journal(tmp_path: Path) -> None:
    """The file lands, the result warns, and rollback uses the journal post-hash."""
    _registry(tmp_path)
    claude = tmp_path / ".claude.json"
    import src.mcp_hub.apply as apply_mod
    import src.mcp_hub.atomic as atomic

    real_save = apply_mod.save_managed
    real_write = apply_mod.atomic_write
    seen: dict[str, object] = {}

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic sidecar write failed")

    def watch(path: Path, data: bytes, **kwargs: object) -> None:
        if Path(path) == claude and "journal" not in seen:
            journal = tmp_path / ".conductor" / "mcp" / "journal"
            seen["journal"] = next(journal.glob("*.json")).read_text(encoding="utf-8")
            seen["payload"] = data
        real_write(path, data, **kwargs)

    apply_mod.save_managed = fail_sidecar
    apply_mod.atomic_write = watch
    try:
        result = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.save_managed = real_save
        apply_mod.atomic_write = real_write
        atomic.before_replace_hook = None

    published = claude.read_bytes()
    assert b"hubbed" in published
    journal_at_publish = seen["journal"]
    assert isinstance(journal_at_publish, str)
    intent = json.loads(journal_at_publish)
    entry = intent["entries"][0]
    assert entry["state"] == "intent"
    assert entry["target"] == ".claude.json"
    assert entry["pre_hash"] is None
    assert entry["post_hash"] == hashlib.sha256(published).hexdigest()

    assert result["partial"] is True
    assert result["succeeded"] == ["claude"]
    assert result["failed"] == []
    warning = result["warnings"][0]
    assert warning["status"] == "published_unregistered"
    assert "synthetic sidecar write failed" in warning["reason"]
    assert result["rollback_command"] == (
        f"conductor mcp rollback {result['snapshot_id']} --apply"
    )
    assert "hubbed" not in load_managed(tmp_path)["claude"]
    stored = _journal_text(tmp_path, result["snapshot_id"])
    assert stored["entries"][0]["state"] == "intent"
    assert _state(tmp_path, result["snapshot_id"]) == "published"
    assert not (tmp_path / ".cursor" / "mcp.json").exists()

    restored = rollback(tmp_path, result["snapshot_id"], apply=True)

    assert not claude.exists()
    assert ".claude.json" in restored["restored"]
    assert restored["modified_since_apply"] == []


def test_recover_registers_a_published_journal_entry(tmp_path: Path) -> None:
    """``recover`` finishes the sidecar from the journal in a later call."""
    _registry(tmp_path, "one", "claude")
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic sidecar write failed")

    apply_mod.save_managed = fail_sidecar
    try:
        result = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.save_managed = real_save

    claude = tmp_path / ".claude.json"
    published = claude.read_bytes()
    finished = recover(tmp_path)

    assert finished["recovered"][0]["agent"] == "claude"
    assert "hubbed" in load_managed(tmp_path)["claude"]
    assert claude.read_bytes() == published
    assert _journal_text(tmp_path, result["snapshot_id"])["entries"][0]["state"] == "intent"
    assert _state(tmp_path, result["snapshot_id"]) == "registered"
    assert recover(tmp_path)["recovered"] == []


def test_mcp_recover_command_registers_after_a_sidecar_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CONDUCTOR_URL", raising=False)
    _registry(home, "one", "claude")
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic sidecar write failed")

    apply_mod.save_managed = fail_sidecar
    try:
        apply_all(home, apply=True)
    finally:
        apply_mod.save_managed = real_save

    import contextlib
    import io

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(["mcp", "recover", "--json"])

    assert code == 0
    envelope = json.loads(stdout.getvalue())
    assert envelope["data"]["recovered"][0]["agent"] == "claude"
    assert "hubbed" in load_managed(home)["claude"]


def test_rollback_hook_preserves_an_edit_before_replace(tmp_path: Path) -> None:
    """An edit planted after the hash check and before replace stays on disk."""
    target = tmp_path / ".claude.json"
    target.write_text('{"mcpServers": {}}\n', encoding="utf-8")
    _registry(tmp_path, "one", "claude")
    applied = apply_all(tmp_path, apply=True)
    user = b'{"manual": "concurrent user edit", "mcpServers": {}}\n'
    import src.mcp_hub.atomic as atomic

    def edit() -> None:
        if atomic.before_replace_hook is not None:
            atomic.before_replace_hook = None
            target.write_bytes(user)

    atomic.before_replace_hook = edit
    try:
        restored = rollback(tmp_path, applied["snapshot_id"], apply=True)
    finally:
        atomic.before_replace_hook = None

    assert target.read_bytes() == user
    assert restored["restored"] == []
    assert restored["modified_since_apply"][0]["reason"] == "modified_since_apply"


def test_rollback_hook_preserves_an_edit_before_delete(tmp_path: Path) -> None:
    """Deleting a published file also compare-and-swaps after the hook."""
    _registry(tmp_path, "one", "claude")
    applied = apply_all(tmp_path, apply=True)
    target = tmp_path / ".claude.json"
    published = target.read_bytes()
    user = b'{"manual": "planted before delete"}\n'
    import src.mcp_hub.atomic as atomic

    def edit() -> None:
        if atomic.before_replace_hook is not None:
            atomic.before_replace_hook = None
            target.write_bytes(user)

    atomic.before_replace_hook = edit
    try:
        restored = rollback(tmp_path, applied["snapshot_id"], apply=True)
    finally:
        atomic.before_replace_hook = None

    assert target.read_bytes() == user
    assert target.read_bytes() != published
    assert restored["restored"] == []
    assert restored["modified_since_apply"][0]["reason"] == "modified_since_apply"


def test_older_snapshot_does_not_undo_a_later_apply(tmp_path: Path) -> None:
    """Rollback of snapshot S skips a target a later apply still owns."""
    _registry(tmp_path, "one", "claude")
    first = apply_all(tmp_path, apply=True)
    first_bytes = (tmp_path / ".claude.json").read_bytes()
    _registry(tmp_path, "two", "claude")
    second = apply_all(tmp_path, apply=True)
    second_bytes = (tmp_path / ".claude.json").read_bytes()
    assert second_bytes != first_bytes

    skipped = rollback(tmp_path, first["snapshot_id"], apply=True)

    assert (tmp_path / ".claude.json").read_bytes() == second_bytes
    assert skipped["restored"] == []
    assert skipped["superseded_by_later_apply"][0]["reason"] == (
        "superseded_by_later_apply"
    )

    newest = rollback(tmp_path, second["snapshot_id"], apply=True)
    assert (tmp_path / ".claude.json").read_bytes() == first_bytes
    assert ".claude.json" in newest["restored"]

    oldest = rollback(tmp_path, first["snapshot_id"], apply=True)
    assert not (tmp_path / ".claude.json").exists()
    assert ".claude.json" in oldest["restored"]
    assert _state(tmp_path, second["snapshot_id"]) == "rolled_back"
    assert _state(tmp_path, first["snapshot_id"]) == "rolled_back"


def test_rollback_then_recreate_then_recover_keeps_user_bytes(tmp_path: Path) -> None:
    """A rolled-back journal must not reclaim a file the user wrote again."""
    _registry(tmp_path, "one", "claude")
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic sidecar write failed")

    apply_mod.save_managed = fail_sidecar
    try:
        applied = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.save_managed = real_save

    target = tmp_path / ".claude.json"
    restored = rollback(tmp_path, applied["snapshot_id"], apply=True)
    assert not target.exists()
    assert ".claude.json" in restored["restored"]
    assert _state(tmp_path, applied["snapshot_id"]) == "rolled_back"
    user = b'{"mcpServers":{"hubbed":{"command":"user-owned-tool"}}}\n'
    target.write_bytes(user)
    owned_before = sorted(load_managed(tmp_path)["claude"])

    recovered = recover(tmp_path)

    assert recovered["recovered"] == []
    assert sorted(load_managed(tmp_path)["claude"]) == owned_before
    assert "hubbed" not in load_managed(tmp_path)["claude"]
    save_registry(tmp_path, Registry())
    later = apply_all(tmp_path, apply=True)

    assert later["applied"] is True
    assert target.read_bytes() == user


def test_rolled_back_entry_does_not_reclaim_the_same_bytes(tmp_path: Path) -> None:
    """Matching bytes are not enough after the journal is ``rolled_back``."""
    _registry(tmp_path, "one", "claude")
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic sidecar write failed")

    apply_mod.save_managed = fail_sidecar
    try:
        applied = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.save_managed = real_save

    target = tmp_path / ".claude.json"
    published = target.read_bytes()
    rollback(tmp_path, applied["snapshot_id"], apply=True)
    target.write_bytes(published)
    recovered = recover(tmp_path)

    assert recovered["recovered"] == []
    assert "hubbed" not in load_managed(tmp_path)["claude"]
    assert _state(tmp_path, applied["snapshot_id"]) == "rolled_back"
    save_registry(tmp_path, Registry())
    apply_all(tmp_path, apply=True)
    assert target.read_bytes() == published


def test_recover_abandons_an_intent_whose_bytes_never_landed(tmp_path: Path) -> None:
    """An intent is registered only when the file still has the post-hash."""
    _registry(tmp_path, "one", "claude")
    import src.mcp_hub.apply as apply_mod

    real_entry = apply_mod.write_entry

    def fail_published(root: Path, snapshot_id: str, entry: dict) -> None:
        if entry["state"] == "published":
            raise OSError("synthetic published-state fail")
        real_entry(root, snapshot_id, entry)

    apply_mod.write_entry = fail_published
    try:
        applied = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.write_entry = real_entry

    target = tmp_path / ".claude.json"
    assert target.is_file()
    assert _journal_text(tmp_path, applied["snapshot_id"])["entries"][0]["state"] == (
        "intent"
    )
    target.write_bytes(b'{"mcpServers":{"other":true}}\n')
    recovered = recover(tmp_path)

    assert recovered["recovered"] == []
    assert "hubbed" not in load_managed(tmp_path)["claude"]
    assert _journal_text(tmp_path, applied["snapshot_id"])["entries"][0]["state"] == "intent"
    assert _state(tmp_path, applied["snapshot_id"]) == "abandoned"
    assert recover(tmp_path)["recovered"] == []


def test_recover_registers_an_intent_whose_bytes_match(tmp_path: Path) -> None:
    """The crash window after the file lands and before ``published`` is recoverable."""
    _registry(tmp_path, "one", "claude")
    import src.mcp_hub.apply as apply_mod

    real_entry = apply_mod.write_entry

    def fail_published(root: Path, snapshot_id: str, entry: dict) -> None:
        if entry["state"] == "published":
            raise OSError("synthetic published-state fail")
        real_entry(root, snapshot_id, entry)

    apply_mod.write_entry = fail_published
    try:
        applied = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.write_entry = real_entry

    target = tmp_path / ".claude.json"
    published = target.read_bytes()
    recovered = recover(tmp_path)

    assert recovered["recovered"][0]["agent"] == "claude"
    assert "hubbed" in load_managed(tmp_path)["claude"]
    assert target.read_bytes() == published
    assert _journal_text(tmp_path, applied["snapshot_id"])["entries"][0]["state"] == "intent"
    assert _state(tmp_path, applied["snapshot_id"]) == "registered"


def test_recover_leaves_a_published_entry_when_bytes_differ(tmp_path: Path) -> None:
    """A published mismatch is not registered and is not marked abandoned."""
    _registry(tmp_path, "one", "claude")
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic sidecar write failed")

    apply_mod.save_managed = fail_sidecar
    try:
        applied = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.save_managed = real_save

    target = tmp_path / ".claude.json"
    target.write_bytes(b'{"mcpServers":{"other":true}}\n')
    recovered = recover(tmp_path)

    assert recovered["recovered"] == []
    assert "hubbed" not in load_managed(tmp_path)["claude"]
    assert _state(tmp_path, applied["snapshot_id"]) == "published"


def _named(home: Path, names: list[str]) -> None:
    save_registry(
        home,
        Registry(
            servers={
                name: Server(
                    name=name,
                    command="echo",
                    args=[name],
                    targets=("claude",),
                )
                for name in names
            }
        ),
    )


def test_deleted_sidecar_is_rebuilt_from_the_journal(tmp_path: Path) -> None:
    """The sidecar is a cache. Deleting it loses nothing the journal recorded."""
    _registry(tmp_path, "one", "claude")
    applied = apply_all(tmp_path, apply=True)
    target = tmp_path / ".claude.json"
    published = target.read_bytes()
    sidecar = managed_path(tmp_path)
    assert sidecar.is_file()
    sidecar.unlink()

    recovered = recover(tmp_path)

    assert recovered["recovered"] == []
    assert "hubbed" in load_managed(tmp_path)["claude"]
    assert target.read_bytes() == published
    assert _state(tmp_path, applied["snapshot_id"]) == "registered"
    assert sidecar.is_file()


def test_crash_between_journal_entries_replays_the_intent(tmp_path: Path) -> None:
    """A crash after ``intent`` and before ``published`` is a new event on replay."""
    _registry(tmp_path, "one", "claude")
    import src.mcp_hub.apply as apply_mod

    real_entry = apply_mod.write_entry

    def fail_published(root: Path, snapshot_id: str, entry: dict) -> None:
        if entry["state"] == "published":
            raise OSError("synthetic gap between journal entries")
        real_entry(root, snapshot_id, entry)

    apply_mod.write_entry = fail_published
    try:
        applied = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.write_entry = real_entry

    journal = tmp_path / ".conductor" / "mcp" / "journal"
    names = sorted(path.name for path in journal.glob("*.json"))
    assert names == [f"{applied['snapshot_id']}.json"]
    assert _journal_text(tmp_path, applied["snapshot_id"])["entries"][0]["state"] == (
        "intent"
    )
    target = tmp_path / ".claude.json"
    published = target.read_bytes()

    recovered = recover(tmp_path)

    assert recovered["recovered"][0]["snapshot_id"] == applied["snapshot_id"]
    assert _journal_text(tmp_path, applied["snapshot_id"])["entries"][0]["state"] == (
        "intent"
    )
    assert _state(tmp_path, applied["snapshot_id"]) == "registered"
    assert f"{applied['snapshot_id']}.1.json" in {
        path.name for path in journal.glob("*.json")
    }
    assert "hubbed" in load_managed(tmp_path)["claude"]
    assert target.read_bytes() == published


def test_rollback_sidecar_failure_keeps_a_later_user_entry(tmp_path: Path) -> None:
    """A failed sidecar write on rollback does not raise, and the user's name stays."""
    _named(tmp_path, ["base"])
    apply_all(tmp_path, apply=True)
    _named(tmp_path, ["base", "hubbed"])
    second = apply_all(tmp_path, apply=True)
    target = tmp_path / ".claude.json"
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic rollback sidecar failure")

    apply_mod.save_managed = fail_sidecar
    try:
        rolled = rollback(tmp_path, second["snapshot_id"], apply=True)
    finally:
        apply_mod.save_managed = real_save

    assert rolled["snapshot_id"] == second["snapshot_id"]
    assert rolled["dry_run"] is False
    assert _state(tmp_path, second["snapshot_id"]) == "rolled_back"
    retry = rollback(tmp_path, second["snapshot_id"], apply=True)
    assert retry["restored"] == []
    assert "hubbed" not in load_managed(tmp_path)["claude"]

    document = json.loads(target.read_text(encoding="utf-8"))
    document["mcpServers"]["hubbed"] = {"command": "user-owned-tool"}
    target.write_text(json.dumps(document), encoding="utf-8")
    recover(tmp_path)
    save_registry(tmp_path, Registry())
    apply_all(tmp_path, apply=True)

    servers = json.loads(target.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["hubbed"]["command"] == "user-owned-tool"


def test_older_bytes_do_not_reclaim_a_newer_apply(tmp_path: Path) -> None:
    """A newer apply stays the owner when the user writes an older apply's bytes.

    The newer apply released ``old`` and owns ``new``. Writing ``old``'s bytes
    back does not roll the newer apply back, because those bytes are the older
    apply's post-image.
    """
    _named(tmp_path, ["old"])
    import src.mcp_hub.apply as apply_mod

    real_save = apply_mod.save_managed

    def fail_sidecar(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic sidecar write failed")

    apply_mod.save_managed = fail_sidecar
    try:
        first = apply_all(tmp_path, apply=True)
    finally:
        apply_mod.save_managed = real_save

    target = tmp_path / ".claude.json"
    old_bytes = target.read_bytes()
    _named(tmp_path, ["new"])
    second = apply_all(tmp_path, apply=True)
    assert load_managed(tmp_path)["claude"] == {"new"}
    target.write_bytes(old_bytes)

    recovered = recover(tmp_path)

    assert recovered["recovered"] == []
    assert load_managed(tmp_path)["claude"] == {"new"}
    assert _state(tmp_path, second["snapshot_id"]) == "released"
    assert _state(tmp_path, first["snapshot_id"]) != "rolled_back"
    save_registry(tmp_path, Registry())
    apply_all(tmp_path, apply=True)
    assert target.read_bytes() == old_bytes


def test_failed_rollback_append_is_filled_on_the_next_replay(tmp_path: Path) -> None:
    """The file is restored first. A missed ``rolled_back`` event is retried."""
    _registry(tmp_path, "one", "claude")
    applied = apply_all(tmp_path, apply=True)
    target = tmp_path / ".claude.json"
    assert target.is_file()
    import src.mcp_hub.journal as journal_mod

    real_entry = journal_mod.write_entry

    def fail_rolled_back(home: Path, snapshot_id: str, entry: dict) -> None:
        if entry.get("state") == "rolled_back":
            raise OSError("synthetic rollback append failed")
        real_entry(home, snapshot_id, entry)

    journal_mod.write_entry = fail_rolled_back
    try:
        with pytest.raises(OSError, match="synthetic rollback append failed"):
            rollback(tmp_path, applied["snapshot_id"], apply=True)
    finally:
        journal_mod.write_entry = real_entry

    assert not target.exists()
    assert _state(tmp_path, applied["snapshot_id"]) == "registered"

    retry = rollback(tmp_path, applied["snapshot_id"], apply=True)

    assert not target.exists()
    assert retry["restored"] == []
    assert _state(tmp_path, applied["snapshot_id"]) == "rolled_back"
    found = entry_for(tmp_path, applied["snapshot_id"], "claude")
    assert found is not None
    assert found["rollback_of"] == applied["snapshot_id"]
    assert "hubbed" not in load_managed(tmp_path)["claude"]
