"""127 gives subscriptions an end and adds the Thread handoff ledger."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSION = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "127_subscription_lifecycle.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_127", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    op.get_context.return_value.autocommit_block.side_effect = (
        lambda: _nullcontext()
    )
    monkeypatch.setattr(module, "op", op)
    return module, events


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_migration_follows_126_and_adds_end_columns_backfill_and_ledger(migration):
    module, events = migration
    assert (module.revision, module.down_revision) == ("127", "126")
    module.upgrade()
    sql = " ".join(events)

    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert "ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ" in sql
    assert "ADD COLUMN IF NOT EXISTS end_reason TEXT" in sql
    assert "end_reason IN ( 'unsubscribed', 'reassigned', 'ticket_ended' )" in sql
    # Subscriptions of already-terminal Tickets end with the backfill.
    backfill = next(
        event for event in events if event.startswith("UPDATE autonomy_subscriptions s")
    )
    assert "t.status IN ('completed', 'cancelled')" in backfill
    assert "end_reason = 'ticket_ended'" in backfill
    assert any(
        event.startswith("CREATE TABLE IF NOT EXISTS autonomy_handoffs")
        for event in events
    )
    assert any(
        event.startswith("CREATE INDEX IF NOT EXISTS idx_autonomy_handoffs_thread")
        for event in events
    )
    # The CHECK validates after the fast catalog work (052/115 discipline).
    assert any(
        "VALIDATE CONSTRAINT autonomy_subscriptions_end_reason_check" in event
        for event in events
    )


def test_downgrade_drops_the_ledger_then_the_columns(migration):
    module, events = migration
    module.downgrade()
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert events[1] == "DROP TABLE IF EXISTS autonomy_handoffs"
    drop = next(
        event
        for event in events
        if event.startswith("ALTER TABLE autonomy_subscriptions DROP COLUMN")
    )
    assert "DROP COLUMN IF EXISTS end_reason" in drop
    assert "DROP COLUMN IF EXISTS ended_at" in drop
    assert any(
        "DROP CONSTRAINT IF EXISTS autonomy_subscriptions_end_reason_check" in event
        for event in events
    )
