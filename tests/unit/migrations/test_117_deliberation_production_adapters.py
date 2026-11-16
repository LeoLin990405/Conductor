"""117 widens participant adapter values for production deliberation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSION = Path(__file__).resolve().parents[3] / "migrations" / "versions" / "117_deliberation_production_adapters.py"


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_117", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_116_and_accepts_production_adapters(migration):
    module, events = migration
    assert (module.revision, module.down_revision) == ("117", "116")
    module.upgrade()
    sql = " ".join(events)
    assert "adapter IN ('mock', 'cli', 'llm', 'daemon')" in sql
    assert "VALIDATE CONSTRAINT autonomy_deliberation_participants_adapter_check" in sql


def test_downgrade_restores_original_adapter_values(migration):
    module, events = migration
    module.downgrade()
    assert any("adapter IN ('mock', 'cli')" in event for event in events)
