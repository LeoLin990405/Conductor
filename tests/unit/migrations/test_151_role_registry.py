"""Migration 151 adds the role registry and its audit table (R6 L1)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest

_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _ROOT / "migrations" / "versions"
_MIGRATION = _VERSIONS / "151_role_registry.py"
_DOC = _ROOT / "MIGRATIONS.md"


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_151", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_it_is_the_single_head_after_150(migration):
    module, _ = migration
    assert (module.revision, module.down_revision) == ("151", "150")
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["151"]


def test_upgrade_creates_one_row_registry_and_append_only_audit(migration):
    module, events = migration
    module.upgrade()
    created = " ".join(events)
    assert "CREATE TABLE IF NOT EXISTS role_registry (" in created
    assert "CHECK (registry_id = 1)" in created
    assert "CREATE TABLE IF NOT EXISTS role_registry_audit (" in created
    assert "role IN ('router', 'planner', 'reviewer', 'worker', '*')" in created
    assert "CREATE TABLE IF NOT EXISTS dispatch_assign_rollout (" in created
    assert "CHECK (stage IN ('shadow', 'sampled', 'full'))" in created
    assert not any(event.startswith("DROP") for event in events)


def test_downgrade_drops_only_its_own_tables(migration):
    module, events = migration
    module.downgrade()
    drops = [event for event in events if event.startswith("DROP")]
    assert drops == [
        "DROP TABLE IF EXISTS dispatch_assign_rollout",
        "DROP TABLE IF EXISTS role_registry_audit",
        "DROP TABLE IF EXISTS role_registry",
    ]


def test_migrations_md_documents_151():
    text = _DOC.read_text(encoding="utf-8")
    assert "## 151" in text and "151_role_registry.py" in text
