"""121 adds the dispatches table without touching any other table."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSIONS = Path(__file__).resolve().parents[3] / "migrations" / "versions"


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_121", _VERSIONS / "121_dispatch.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_it_follows_the_previous_head_in_a_linear_chain(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("121", "120")


def test_upgrade_creates_the_dispatch_table_with_its_state_guard(migration):
    module, events = migration
    module.upgrade()
    sql = "\n".join(events)
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert "CREATE TABLE IF NOT EXISTS dispatches" in sql
    for state in module._STATES:
        assert f"'{state}'" in sql
    assert "lines JSONB NOT NULL DEFAULT '[]'::jsonb" in sql
    assert "outcomes JSONB NOT NULL DEFAULT '{}'::jsonb" in sql
    assert "ALTER TABLE" not in sql
    assert "DROP TABLE" not in sql


def test_upgrade_indexes_the_two_read_paths(migration):
    module, events = migration
    module.upgrade()
    indexes = [event for event in events if "CREATE INDEX" in event]
    assert indexes == [
        "CREATE INDEX IF NOT EXISTS idx_dispatches_user_state "
        "ON dispatches (user_id, state, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_dispatches_state_created "
        "ON dispatches (state, created_at DESC)",
    ]


def test_downgrade_drops_the_indexes_then_the_table(migration):
    module, events = migration
    module.downgrade()
    assert events == [
        "DROP INDEX IF EXISTS idx_dispatches_state_created",
        "DROP INDEX IF EXISTS idx_dispatches_user_state",
        "DROP TABLE IF EXISTS dispatches",
    ]


def test_the_state_guard_covers_every_lifecycle_state(migration):
    module, _events = migration
    assert module._STATES == (
        "SUBMITTED", "PLANNING", "ASSIGNED", "LAUNCHING", "MONITORING",
        "REVIEW", "APPROVAL", "MERGE", "COMPLETE", "ABORT", "ERROR",
    )
    # The CHECK constraint and the Python enum have to name the same states, or
    # a row the machine writes is rejected by the database.
    from src.tools.auto_dispatch.types import DispatchState

    assert set(module._STATES) == {state.value for state in DispatchState}
