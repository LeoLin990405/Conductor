"""132 widens the ticket-event check for actions that do not change status.

Forced acknowledgement and mandate refusal need their own action names.
The previous check only allowed the status transitions from 131.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

_VERSION = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "132_ticket_event_actions.py"
)


def _migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_132", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_131_and_adds_the_two_actions(monkeypatch):
    module, events = _migration(monkeypatch)
    assert (module.revision, module.down_revision) == ("132", "131")

    module.upgrade()
    sql = " ".join(events)

    assert "DROP CONSTRAINT IF EXISTS autonomy_ticket_events_action_check" in sql
    assert "'stop_acknowledged'" in sql
    assert "'mandate_revoked'" in sql
    assert "'claimed', 'stopped', 'resumed', 'reassigned', 'archived'" in sql


def test_downgrade_drops_the_new_actions_before_restoring_the_check(monkeypatch):
    module, events = _migration(monkeypatch)

    module.downgrade()
    sql = " ".join(events)

    delete_at = sql.index("DELETE FROM autonomy_ticket_events")
    restore_at = sql.rindex("ADD CONSTRAINT autonomy_ticket_events_action_check")
    assert delete_at < restore_at
    assert "action IN ('stop_acknowledged', 'mandate_revoked')" in sql
    restored = sql[restore_at:]
    assert "'stop_acknowledged'" not in restored
    assert "'mandate_revoked'" not in restored
