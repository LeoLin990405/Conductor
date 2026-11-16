"""Migration 147 adds per-Ticket worktree path, generation, and cleanup retry."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parents[3]
_VERSIONS = _ROOT / "migrations" / "versions"


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_147", _VERSIONS / "147_ticket_worktree.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_it_follows_146_in_the_chain(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("147", "146")
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["151"]
    assert script.get_revision("151").down_revision == "150"
    assert script.get_revision("150").down_revision == "149"
    assert script.get_revision("147").down_revision == "146"


def test_upgrade_adds_path_generation_and_cleanup_retry_columns(migration):
    module, events = migration
    module.upgrade()
    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        (
            "ALTER TABLE autonomy_tickets "
            "ADD COLUMN IF NOT EXISTS worktree_path TEXT NOT NULL DEFAULT '', "
            "ADD COLUMN IF NOT EXISTS worktree_generation INTEGER NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS worktree_cleanup_pending BOOLEAN NOT NULL DEFAULT FALSE, "
            "ADD COLUMN IF NOT EXISTS worktree_cleanup_attempts INTEGER NOT NULL DEFAULT 0, "
            "ADD COLUMN IF NOT EXISTS worktree_cleanup_next_retry_at TIMESTAMPTZ, "
            "ADD COLUMN IF NOT EXISTS worktree_branch TEXT NOT NULL DEFAULT '', "
            "ADD COLUMN IF NOT EXISTS worktree_base_sha TEXT NOT NULL DEFAULT '', "
            "ADD COLUMN IF NOT EXISTS worktree_base_branch TEXT NOT NULL DEFAULT '', "
            "ADD COLUMN IF NOT EXISTS worktree_repo TEXT NOT NULL DEFAULT '', "
            "ADD COLUMN IF NOT EXISTS worktree_host TEXT NOT NULL DEFAULT '', "
            "ADD COLUMN IF NOT EXISTS worktree_daemon_id UUID, "
            "ADD COLUMN IF NOT EXISTS worktree_lease_id UUID, "
            "ADD COLUMN IF NOT EXISTS worktree_stale_cleanups JSONB NOT NULL DEFAULT '[]'::jsonb"
        ),
    ]


def test_downgrade_drops_the_columns_in_one_statement(migration):
    module, events = migration
    module.downgrade()
    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        (
            "ALTER TABLE autonomy_tickets "
            "DROP COLUMN IF EXISTS worktree_stale_cleanups, "
            "DROP COLUMN IF EXISTS worktree_lease_id, "
            "DROP COLUMN IF EXISTS worktree_daemon_id, "
            "DROP COLUMN IF EXISTS worktree_host, "
            "DROP COLUMN IF EXISTS worktree_repo, "
            "DROP COLUMN IF EXISTS worktree_base_branch, "
            "DROP COLUMN IF EXISTS worktree_base_sha, "
            "DROP COLUMN IF EXISTS worktree_branch, "
            "DROP COLUMN IF EXISTS worktree_cleanup_next_retry_at, "
            "DROP COLUMN IF EXISTS worktree_cleanup_attempts, "
            "DROP COLUMN IF EXISTS worktree_cleanup_pending, "
            "DROP COLUMN IF EXISTS worktree_generation, "
            "DROP COLUMN IF EXISTS worktree_path"
        ),
    ]
