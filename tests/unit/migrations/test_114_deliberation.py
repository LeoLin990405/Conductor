"""114 adds deliberation tables without altering ticket claim behavior."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSIONS = Path(__file__).resolve().parents[3] / "migrations" / "versions"

_TABLES = (
    "autonomy_deliberations",
    "autonomy_deliberation_participants",
    "autonomy_deliberation_rounds",
    "autonomy_deliberation_messages",
    "autonomy_deliberation_votes",
    "autonomy_deliberation_dissents",
    "autonomy_deliberation_steps",
)
_DROP_ORDER = (
    "autonomy_deliberation_steps",
    "autonomy_deliberation_dissents",
    "autonomy_deliberation_votes",
    "autonomy_deliberation_messages",
    "autonomy_deliberation_rounds",
    "autonomy_deliberation_participants",
    "autonomy_deliberations",
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_114", _VERSIONS / "114_deliberation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_it_follows_110_in_a_linear_chain(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("114", "113")


def test_upgrade_creates_deliberation_tables(migration):
    module, events = migration
    module.upgrade()
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    sql = "\n".join(events)
    for name in _TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {name}" in sql
    active = [event for event in events if "uq_autonomy_deliberations_active_ticket" in event]
    assert len(active) == 1
    assert "UNIQUE" in active[0]
    assert "draft" in active[0] and "running" in active[0]
    assert "ALTER TABLE" not in sql


def test_downgrade_drops_all_seven_tables(migration):
    module, events = migration
    module.downgrade()
    drops = [event for event in events if "DROP TABLE" in event]
    assert drops == [f"DROP TABLE IF EXISTS {name}" for name in _DROP_ORDER]
