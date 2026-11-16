"""Migration 149 adds the dispatch workspace registry.

A workspace names a git checkout: its repo root, the remote a fetch updates, a
sandbox policy and a default worker. The tests pin the table's key columns and
the index and table drops, because the registry is what a dispatch start
resolves before it runs.
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
_MIGRATION = _VERSIONS / "149_dispatch_workspaces.py"
_DOC = _ROOT / "MIGRATIONS.md"


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_149", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def _created_columns() -> list[str]:
    source = _MIGRATION.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade", 1)[1].split("def downgrade", 1)[0]
    body = re.search(
        r"CREATE TABLE IF NOT EXISTS dispatch_workspaces \((.*?)\)\s*\"\"\"",
        upgrade,
        re.DOTALL,
    )
    assert body, "the migration does not create dispatch_workspaces"
    return re.findall(r"^\s*(\w+)\s+[A-Z]", body.group(1), re.MULTILINE)


def test_it_follows_148_in_a_linear_chain(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("149", "148")
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["151"]
    assert script.get_revision("151").down_revision == "150"
    assert script.get_revision("150").down_revision == "149"
    assert script.get_revision("149").down_revision == "148"


def test_upgrade_creates_the_registry_and_its_lookup_indexes(migration):
    module, events = migration
    module.upgrade()
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert "CREATE TABLE IF NOT EXISTS dispatch_workspaces" in events[1]
    assert events[2] == (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_dispatch_workspaces_user_name "
        "ON dispatch_workspaces (user_id, name)"
    )
    assert events[3] == (
        "CREATE INDEX IF NOT EXISTS idx_dispatch_workspaces_user "
        "ON dispatch_workspaces (user_id, updated_at DESC)"
    )
    assert events[4] == (
        "CREATE INDEX IF NOT EXISTS idx_dispatch_workspaces_user_path "
        "ON dispatch_workspaces (user_id, path)"
    )


def test_the_registry_records_the_path_remote_policy_and_worker(migration):
    module, events = migration
    module.upgrade()
    statement = events[1]
    assert _created_columns() == [
        "workspace_id",
        "user_id",
        "name",
        "path",
        "git_remote",
        "sandbox_policy",
        "default_worker",
        "created_at",
        "updated_at",
    ]
    for column in ("git_remote", "sandbox_policy", "default_worker"):
        assert f"{column} TEXT NOT NULL DEFAULT ''" in statement
    assert "path TEXT NOT NULL" in statement


def test_upgrade_copies_the_workspace_snapshot_onto_dispatches(migration):
    """A live run keeps its repo, policy and worker after the registry changes."""
    module, events = migration
    module.upgrade()
    alters = [event for event in events if event.startswith("ALTER TABLE dispatches")]
    assert len(alters) == 1
    for column in ("workspace_id", "repo_root", "sandbox_policy", "default_worker"):
        assert f"ADD COLUMN IF NOT EXISTS {column} TEXT NOT NULL DEFAULT ''" in alters[0]


def test_downgrade_drops_indexes_before_the_table(migration):
    module, events = migration
    module.downgrade()
    assert events[-1] == "DROP TABLE IF EXISTS dispatch_workspaces"
    assert events.index(
        "DROP INDEX IF EXISTS idx_dispatch_workspaces_user_path"
    ) < events.index("DROP TABLE IF EXISTS dispatch_workspaces")
    assert events.index(
        "DROP INDEX IF EXISTS uq_dispatch_workspaces_user_name"
    ) < events.index("DROP TABLE IF EXISTS dispatch_workspaces")


def test_migrations_md_names_every_created_column():
    rows = [
        line
        for line in _DOC.read_text(encoding="utf-8").splitlines()
        if line.startswith("| 149 |")
    ]
    assert len(rows) == 1, rows
    named = set(re.findall(r"`(\w+)`", rows[0]))
    missing = [column for column in _created_columns() if column not in named]
    assert not missing, f"MIGRATIONS.md row 149 does not name {missing}"
