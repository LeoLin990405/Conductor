from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
VERSION = ROOT / "migrations" / "versions" / "138_memory_tool_denials.py"


@pytest.fixture
def migration(monkeypatch):
    op = MagicMock()
    spec = importlib.util.spec_from_file_location("migration_138", VERSION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", op)
    return module, op


def _sql(op) -> str:
    return " ".join(" ".join(call.args[0].split()) for call in op.execute.call_args_list)


def test_migration_creates_owner_scoped_frozen_authority_after_137(migration):
    module, op = migration
    assert (module.revision, module.down_revision) == ("138", "137")

    module.upgrade()
    sql = _sql(op)
    assert "CREATE TABLE memory_tool_denials" in sql
    assert "entry_id UUID NOT NULL" in sql
    assert "REFERENCES memory_entries" not in sql
    assert "PRIMARY KEY (user_id, entry_id)" in sql
    assert "approved_content_hash TEXT NOT NULL" in sql
    assert "valid_from TIMESTAMPTZ" in sql and "valid_until TIMESTAMPTZ" in sql
    assert "approval_source = 'manual'" in sql
    assert "jsonb_array_length(denied_tools) BETWEEN 1 AND 64" in sql
    assert "WHERE active IS TRUE" in sql


def test_downgrade_refuses_to_drop_active_authority_rows(migration):
    module, op = migration
    module.downgrade()
    sql = _sql(op)
    assert "IF EXISTS (SELECT 1 FROM memory_tool_denials WHERE active IS TRUE)" in sql
    assert "retire active memory tool denials" in sql
    assert "DROP TABLE IF EXISTS memory_tool_denials" in sql
