"""141 adds the gates and spec_ref columns to dispatches, and drops them on downgrade."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSIONS = Path(__file__).resolve().parents[3] / "migrations" / "versions"


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "migration_141", _VERSIONS / "141_dispatch_gates.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_it_follows_140_in_a_linear_chain(migration):
    module, _events = migration
    assert (module.revision, module.down_revision) == ("141", "140")


def test_upgrade_adds_both_columns_with_defaults(migration):
    module, events = migration
    module.upgrade()
    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        "ALTER TABLE dispatches ADD COLUMN IF NOT EXISTS gates JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE dispatches ADD COLUMN IF NOT EXISTS spec_ref TEXT NOT NULL DEFAULT ''",
    ]


def test_downgrade_drops_only_the_two_columns_in_reverse_order(migration):
    module, events = migration
    module.downgrade()
    assert events == [
        "SET LOCAL lock_timeout = '5s'",
        "ALTER TABLE dispatches DROP COLUMN IF EXISTS spec_ref",
        "ALTER TABLE dispatches DROP COLUMN IF EXISTS gates",
    ]
