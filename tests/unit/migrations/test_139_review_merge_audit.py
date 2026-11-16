"""139 adds the durable review queue and the review/merge audit log."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
VERSION = ROOT / "migrations" / "versions" / "139_review_merge_audit.py"


@pytest.fixture
def migration(monkeypatch):
    op = MagicMock()
    spec = importlib.util.spec_from_file_location("migration_139", VERSION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", op)
    return module, op


def _sql(op: MagicMock) -> str:
    return " ".join(" ".join(call.args[0].split()) for call in op.execute.call_args_list)


def test_migration_follows_138_and_creates_review_and_audit_tables(migration):
    module, op = migration
    assert (module.revision, module.down_revision) == ("139", "138")

    module.upgrade()
    sql = _sql(op)
    assert "CREATE TABLE IF NOT EXISTS autonomy_review_records" in sql
    # A line may never review itself, in the schema as well as at the route.
    assert "CHECK (line <> reviewer)" in sql
    assert "reviewer_provider TEXT NOT NULL DEFAULT 'codex'" in sql
    assert "state TEXT NOT NULL DEFAULT 'queued'" in sql
    assert "state IN ('queued', 'completed', 'failed')" in sql
    assert "verdict TEXT CHECK (verdict IN ('merge', 'block'))" in sql
    assert "^[0-9a-f]{40}([0-9a-f]{24})?$" in sql
    assert "state = 'completed' AND verdict IS NOT NULL" in sql
    assert "idx_autonomy_review_records_user_line" in sql
    assert "CREATE TABLE IF NOT EXISTS autonomy_review_audit" in sql
    for action in (
        "review_requested",
        "review_completed",
        "merge_refused",
        "merge_started",
        "merge_merged",
        "merge_failed",
    ):
        assert f"'{action}'" in sql
    assert "outcome IN ('ok', 'refused', 'failed')" in sql
    assert "detail JSONB NOT NULL DEFAULT '{}'::jsonb" in sql
    assert "idx_autonomy_review_audit_user_line" in sql


def test_downgrade_refuses_to_erase_merged_history(migration):
    module, op = migration
    module.downgrade()
    sql = _sql(op)
    assert "WHERE action IN ('merge_started', 'merge_merged')" in sql
    assert "archive them before downgrading 139" in sql
    assert "DROP TABLE IF EXISTS autonomy_review_audit" in sql
    assert "DROP TABLE IF EXISTS autonomy_review_records" in sql
    # The guard must run before either DROP of the history it protects.
    calls = [" ".join(call.args[0].split()) for call in op.execute.call_args_list]
    guard_index = next(i for i, text in enumerate(calls) if "RAISE EXCEPTION" in text)
    drop_index = next(
        i for i, text in enumerate(calls) if "DROP TABLE IF EXISTS autonomy_review_audit" in text
    )
    assert guard_index < drop_index
