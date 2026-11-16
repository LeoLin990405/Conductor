"""Migration 148 adds the memory ontology columns ``class`` and ``about``.

The ontology gives every memory a class (what kind of claim it is) and an
``about`` (the concept id of the thing it is about). Both are nullable: a
memory written before the ontology existed has neither, and the projection
never invents one for it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from unittest.mock import MagicMock

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest

_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _ROOT / "migrations" / "versions"
_MIGRATION = _VERSIONS / "148_memory_class_about.py"
_DOC = _ROOT / "MIGRATIONS.md"


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_148", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def _added_columns() -> list[str]:
    source = _MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    return re.findall(r"ADD COLUMN IF NOT EXISTS (\w+)", upgrade)


def test_it_follows_147_in_a_linear_chain(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("148", "147")
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["151"]
    assert script.get_revision("151").down_revision == "150"
    assert script.get_revision("150").down_revision == "149"
    assert script.get_revision("149").down_revision == "148"


def test_upgrade_adds_two_nullable_columns_and_the_pair_index(migration):
    module, events = migration
    module.upgrade()
    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        "ALTER TABLE memory_entries ADD COLUMN IF NOT EXISTS class TEXT,"
        " ADD COLUMN IF NOT EXISTS about TEXT",
        "CREATE INDEX IF NOT EXISTS ix_memory_entries_class_about"
        " ON memory_entries (class, about)",
    ]


def test_the_columns_are_nullable_and_carry_no_default(migration):
    """A memory written before the ontology has neither, and a column with a
    default would say every memory names something."""
    module, events = migration
    module.upgrade()
    statement = events[1]
    assert "NOT NULL" not in statement
    assert "DEFAULT" not in statement


def test_downgrade_drops_the_index_before_the_columns(migration):
    """Dropping a column takes its indexes with it; the order is the point."""
    module, events = migration
    module.downgrade()
    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        "DROP INDEX IF EXISTS ix_memory_entries_class_about",
        "ALTER TABLE memory_entries DROP COLUMN IF EXISTS class,"
        " DROP COLUMN IF EXISTS about",
    ]


def test_migrations_md_names_every_added_column():
    rows = [
        line
        for line in _DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("| 148 |")
    ]
    assert len(rows) == 1, rows
    named = set(re.findall(r"`(?:memory_entries\.)?(\w+)`", rows[0]))
    missing = [column for column in _added_columns() if column not in named]
    assert not missing, f"MIGRATIONS.md row 148 does not name {missing}"
