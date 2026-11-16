"""133 adds reversible, identifier-only memory forget audit storage."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSION = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "133_memory_forget.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_133", _VERSION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_the_allocated_predecessor_132_and_adds_hash_only_forget_records(migration):
    module, events = migration

    assert (module.revision, module.down_revision) == ("133", "132")
    module.upgrade()
    sql = " ".join(events)
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert "ADD COLUMN IF NOT EXISTS source_entry_id UUID" in sql
    assert "ADD COLUMN IF NOT EXISTS identity_hash TEXT" in sql
    assert "md5(jsonb_build_array(tier, namespace, entry_key)::text)" in sql
    assert "ALTER COLUMN identity_hash SET NOT NULL" in sql
    assert "memory_forget_tombstones" in sql
    assert "memory_forget_provider_notices" in sql
    assert "CHECK (reason IN (" in sql
    assert all(
        f"'{reason}'" in sql
        for reason in ("user_request", "correction", "duplicate", "other")
    )
    assert "GENERATED ALWAYS" not in sql

    tombstone_sql = next(
        statement for statement in events if "CREATE TABLE IF NOT EXISTS memory_forget_tombstones" in statement
    )
    assert all(
        column in tombstone_sql
        for column in (
            "user_id UUID",
            "entry_id UUID",
            "root_entry_id UUID",
            "identity_hash TEXT",
            "content_hash TEXT",
            "reason TEXT",
        )
    )
    assert all(
        forbidden not in tombstone_sql
        for forbidden in ("entry_key", "index_text", "body TEXT", "text TEXT")
    )


def test_downgrade_removes_only_added_audit_objects_and_columns(migration):
    module, events = migration
    module.downgrade()

    assert events == [
        "DROP TABLE IF EXISTS memory_forget_provider_notices",
        "DROP TABLE IF EXISTS memory_forget_tombstones",
        "DROP INDEX IF EXISTS ix_memory_entries_owner_source",
        "DROP INDEX IF EXISTS ix_memory_entries_owner_identity",
        "ALTER TABLE memory_entries DROP COLUMN IF EXISTS identity_hash",
        "ALTER TABLE memory_entries DROP COLUMN IF EXISTS source_entry_id",
    ]
