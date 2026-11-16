"""128 adds explicit context scopes and the A2A agent/message tables."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_VERSION = (
    Path(__file__).resolve().parents[3]
    / "migrations"
    / "versions"
    / "128_context_scopes_a2a.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_128", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_127_and_creates_scope_and_a2a_tables(migration):
    module, events = migration
    assert (module.revision, module.down_revision) == ("128", "127")
    module.upgrade()
    sql = " ".join(events)
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert "CREATE TABLE IF NOT EXISTS autonomy_context_scopes" in sql
    assert "kind IN ('isolated', 'shared')" in sql
    assert "ADD COLUMN IF NOT EXISTS context_scope_id UUID" in sql
    assert "REFERENCES autonomy_context_scopes(scope_id)" in sql
    # Existing Tickets are closed by default before the A2A tables exist.
    assert "SELECT user_id, 'isolated:' || ticket_id::text, 'isolated'" in sql
    assert "CREATE TABLE IF NOT EXISTS autonomy_a2a_agents" in sql
    assert "CREATE TABLE IF NOT EXISTS autonomy_a2a_messages" in sql
    assert "UNIQUE (sender_ticket_id, message_key)" in sql
    assert "intent IN ('inform', 'request_review', 'handoff_request')" in sql


def test_downgrade_drops_the_new_tables_and_column_in_reverse_order(migration):
    module, events = migration
    module.downgrade()
    assert events == [
        "DROP TABLE IF EXISTS autonomy_a2a_messages",
        "DROP TABLE IF EXISTS autonomy_a2a_agents",
        "DROP INDEX IF EXISTS idx_autonomy_tickets_context_scope",
        "ALTER TABLE autonomy_tickets DROP COLUMN IF EXISTS context_scope_id",
        "DROP TABLE IF EXISTS autonomy_context_scopes",
    ]


def test_migration_128_is_on_the_chain_and_129_is_the_head():
    revisions: dict[str, str | None] = {}
    for path in _VERSION.parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        values = {}
        for node in tree.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                }:
                    values[target.id] = ast.literal_eval(value)
        if values.get("revision") is not None:
            revisions[str(values["revision"])] = values.get("down_revision")

    parents = {
        parent
        for down_revision in revisions.values()
        for parent in (
            down_revision
            if isinstance(down_revision, tuple)
            else (down_revision,)
            if down_revision is not None
            else ()
        )
    }
    heads = set(revisions) - parents
    assert revisions["128"] == "127"
    # The endpoint chain's 129/130 and this line's 131/132 continue after 128,
    # memory P0 owns the allocated tail 133-138, and 139 adds the review queue
    # and merge audit, so 128 is an ancestor and the AST-discovered head is 142.
    assert revisions["129"] == "128"
    assert revisions["130"] == "129"
    # Memory P0 owns the allocated tail 133-138 after the endpoint/ob-tickets
    # head 132, and 139 follows it, so 132 stays an ancestor and the
    # AST-discovered head is 142.
    assert revisions["133"] == "132"
    assert revisions["134"] == "133"
    assert revisions["135"] == "134"
    assert revisions["136"] == "135"
    assert revisions["137"] == "136"
    assert revisions["138"] == "137"
    assert revisions["139"] == "138"
    assert revisions["140"] == "139"
    assert revisions["141"] == "140"
    assert revisions["144"] == "143"
    assert revisions["145"] == "144"
    assert revisions["149"] == "148"
    assert revisions["150"] == "149"
    assert revisions["151"] == "150"
    assert revisions["146"] == "145"
    assert heads == {"151"}
