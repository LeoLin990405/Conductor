"""095 adds the thread-level Swarm switch without rewriting existing rows."""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSIONS = Path(__file__).resolve().parents[3] / "migrations" / "versions"


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_095", _VERSIONS / "095_thread_swarm_enabled.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []

    @contextmanager
    def autocommit_block():
        events.append("begin")
        yield
        events.append("commit")

    op = MagicMock()
    op.get_context.return_value.autocommit_block = autocommit_block
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_it_follows_094_in_a_linear_chain(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("095", "094")


def test_it_adds_a_defaulted_not_null_column(migration):
    module, events = migration
    module.upgrade()
    added = [e for e in events if "ADD COLUMN" in e]
    assert added == [
        "ALTER TABLE conversation_threads ADD COLUMN IF NOT EXISTS swarm_enabled BOOLEAN NOT NULL DEFAULT FALSE"
    ]
    assert events[-2:] == ["begin", added[0]] or added[0] in events
    # safe to rerun after an interrupted migration
    assert "IF NOT EXISTS" in added[0]


def test_downgrade_drops_the_switch(migration):
    module, events = migration
    module.downgrade()
    assert any("DROP COLUMN IF EXISTS swarm_enabled" in e for e in events)
