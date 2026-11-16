from __future__ import annotations

import importlib.util
from pathlib import Path

from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[3]
VERSION = ROOT / "migrations" / "versions" / "136_memory_policy_metadata.py"


@pytest.fixture
def migration(monkeypatch):
    op = MagicMock()
    spec = importlib.util.spec_from_file_location("migration_136", VERSION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "op", op)
    return module, op


def test_migration_adds_object_metadata_after_change_kind(migration):
    module, op = migration
    assert (module.revision, module.down_revision) == ("136", "135")
    module.upgrade()
    sql = " ".join(" ".join(call.args[0].split()) for call in op.execute.call_args_list)
    assert "ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb" in sql
    assert "CHECK (jsonb_typeof(metadata) = 'object')" in sql


def test_downgrade_only_removes_new_column_and_constraint(migration):
    module, op = migration
    module.downgrade()
    sql = " ".join(" ".join(call.args[0].split()) for call in op.execute.call_args_list)
    assert "DROP CONSTRAINT IF EXISTS ck_memory_proposal_metadata_object" in sql
    assert "DROP COLUMN IF EXISTS metadata" in sql
    assert "DROP TABLE" not in sql
