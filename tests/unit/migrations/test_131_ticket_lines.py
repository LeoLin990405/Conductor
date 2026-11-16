"""131 gives Tickets a line, an owner-history, and a terminal archived state.

The line groups Tickets by branch/worktree for the dashboard; the event table
is the audit trail behind the exactly-one-owner rule; ``archived`` leaves the
active vocabulary without losing the row. The downgrade must settle archived
rows before the old CHECK is restored, or the previous build could not read
them.
"""

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
    / "131_ticket_lines_ownership.py"
)


@pytest.fixture
def migration(monkeypatch):
    spec = importlib.util.spec_from_file_location("migration_131", _VERSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    events: list[str] = []
    op = MagicMock()
    op.execute.side_effect = lambda sql: events.append(" ".join(str(sql).split()))
    monkeypatch.setattr(module, "op", op)
    return module, events


def test_migration_follows_128_and_adds_lines_events_and_the_archived_state(
    migration,
):
    module, events = migration
    assert (module.revision, module.down_revision) == ("131", "130")

    module.upgrade()
    sql = " ".join(events)

    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert "CREATE TABLE IF NOT EXISTS autonomy_lines" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_autonomy_lines_user_name" in sql
    assert "ADD COLUMN IF NOT EXISTS line_id UUID" in sql
    assert "FOREIGN KEY (line_id) REFERENCES autonomy_lines(line_id)" in sql
    assert "CREATE TABLE IF NOT EXISTS autonomy_ticket_events" in sql
    assert "'claimed', 'stopped', 'resumed', 'reassigned', 'archived'" in sql
    assert "'awaiting_clarification', 'archived'" in sql
    assert "VALIDATE CONSTRAINT autonomy_tickets_status_check" in sql


def test_the_event_table_indexes_the_two_dashboard_read_paths(migration):
    module, events = migration

    module.upgrade()
    sql = " ".join(events)

    assert "CREATE INDEX IF NOT EXISTS idx_autonomy_ticket_events_ticket" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_autonomy_ticket_events_user" in sql
    assert "CREATE INDEX IF NOT EXISTS idx_autonomy_tickets_line" in sql


def test_the_downgrade_settles_archived_rows_before_narrowing_the_check(
    migration,
):
    module, events = migration

    module.downgrade()

    settle = next(
        i
        for i, sql in enumerate(events)
        if sql.startswith("UPDATE autonomy_tickets SET status = 'cancelled'")
    )
    assert events[settle].endswith("WHERE status = 'archived'")
    narrow = next(
        i
        for i, sql in enumerate(events)
        if "ADD CONSTRAINT autonomy_tickets_status_check" in sql
    )
    assert settle < narrow
    assert "'awaiting_clarification'" in events[narrow]
    assert "'archived'" not in events[narrow]
    sql = " ".join(events)
    assert "DROP TABLE IF EXISTS autonomy_ticket_events" in sql
    assert "DROP COLUMN IF EXISTS line_id" in sql
    assert "DROP TABLE IF EXISTS autonomy_lines" in sql


def test_the_downgrade_validates_the_restored_check_in_the_commands_transaction(
    migration,
):
    """``alembic downgrade`` is one transaction, and an autocommit block commits
    everything the command has done so far (140 down to 132 and this revision's
    own update), so a failure at 130 would leave ``alembic_version`` at 131 over
    a half-undone schema. The CHECK is therefore added validated, by the one
    statement that adds it, and every lock wait in this downgrade stays under the
    5 s ``SET LOCAL`` at the top: a plain ``SET lock_timeout`` in a block would
    outlive the block and take that bound away from what the command still has
    to drop."""
    module, events = migration

    module.downgrade()

    module.op.get_context.assert_not_called()
    assert events[0] == "SET LOCAL lock_timeout = '5s'"
    assert not any(sql.startswith("SET lock_timeout") for sql in events)
    narrow = next(
        sql for sql in events if "ADD CONSTRAINT autonomy_tickets_status_check" in sql
    )
    assert "NOT VALID" not in narrow
    assert not any("VALIDATE CONSTRAINT" in sql for sql in events)


def test_migration_graph_has_142_as_its_only_ast_discovered_head():
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
            else (down_revision,) if down_revision is not None else ()
        )
    }
    heads = set(revisions) - parents
    # Final allocation: integrate/r2's 128 (sa-a2a) -> beyond-endpoint 129 and
    # 130 -> this line's 131 -> 132. Revision 130 may be absent from the
    # branch-local tree until the endpoint line lands; the AST graph still sees
    # the link, and the branch is re-synced and re-tested before integration.
    assert revisions["127"] == "126"
    assert revisions["128"] == "127"
    assert revisions["131"] == "130"
    assert revisions["132"] == "131"
        # Memory P0 owns the allocated tail 133-138 after the endpoint/ob-tickets
    # head 132, so 132 stays an ancestor and the AST-discovered head is 142.
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

    order = (Path(__file__).resolve().parents[3] / "MIGRATIONS.md").read_text(
        encoding="utf-8"
    )
    assert "sa-a2a=128" in order
    assert "beyond-endpoint=129-130" in order
    assert "ob-tickets=131-132" in order
