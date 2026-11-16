from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
VERSION = ROOT / "migrations" / "versions" / "135_memory_change_kind.py"


class _Op:
    def __init__(self):
        self.sql = []

    def execute(self, statement):
        self.sql.append(" ".join(statement.split()))


@pytest.fixture
def migration(monkeypatch):
    op = _Op()
    monkeypatch.setattr("alembic.op.execute", op.execute)
    spec = importlib.util.spec_from_file_location("migration_135", VERSION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, op


def test_migration_adds_closed_durable_change_contract(migration):
    module, op = migration
    assert (module.revision, module.down_revision) == ("135", "134")
    module.upgrade()
    sql = "\n".join(op.sql)
    assert "DEFAULT 'long_term'" in sql
    assert "CHECK (change_kind IN ('long_term', 'correction'))" in sql
    assert "replaces_entry_key IS NOT NULL" in sql
    assert "reason IS NOT NULL" in sql


def test_migration_downgrade_only_drops_added_contract(migration):
    module, op = migration
    module.downgrade()
    sql = "\n".join(op.sql)
    assert "DROP COLUMN IF EXISTS reason" in sql
    assert "DROP COLUMN IF EXISTS replaces_entry_key" in sql
    assert "DROP COLUMN IF EXISTS change_kind" in sql
    assert "DROP TABLE" not in sql
