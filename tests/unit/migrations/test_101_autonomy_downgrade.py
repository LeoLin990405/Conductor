"""101's downgrade drops every per-user autonomy table, including later ones."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSION = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "101_autonomy.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_101", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_downgrade_drops_peer_messages_with_the_per_user_tables(migration):
    module, events = migration
    module.downgrade()
    drops = [sql for sql in events if sql.startswith("DROP TABLE")]
    assert "DROP TABLE IF EXISTS autonomy_peer_messages" in drops
    assert drops.index("DROP TABLE IF EXISTS autonomy_peer_messages") < drops.index(
        "DROP TABLE IF EXISTS autonomy_tickets"
    )
