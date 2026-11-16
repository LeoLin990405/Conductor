"""Scope SQL: a ticket is born isolated and context delivery is scope-filtered."""

from unittest.mock import patch

import pytest

from src.server.database.autonomy import context_scopes as scope_db
from src.server.database.autonomy import tickets as ticket_db

_MOD = "src.server.database.autonomy.tickets"


class _Cursor:
    def __init__(self, *, one=None, all_rows=None):
        self.one = list(one or [])
        self.all_rows = list(all_rows or [])
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self.one.pop(0)

    async def fetchall(self):
        return self.all_rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, cursor):
        self.cur = cursor
        self.transactions = 0

    def cursor(self, row_factory=None):
        return self.cur

    def transaction(self):
        self.transactions += 1
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_create_ticket_allocates_an_isolated_scope_in_the_same_connection():
    ticket_row = {
        "ticket_id": "t1",
        "user_id": "u1",
        "context_scope_id": "scope-1",
        "mandate": {},
        "attention": None,
    }
    cur = _Cursor(one=[{"scope_id": "scope-1"}, ticket_row])
    with patch(f"{_MOD}.get_db_connection", return_value=_Conn(cur)):
        row = await ticket_db.create_ticket(
            user_id="u1",
            workspace_id="w1",
            title="Fix",
            scope="Fix it",
            mandate={},
            policy_version="v1",
            origin_trigger_id="trg1",
            origin_thread_key="thread-1",
        )
    assert row["context_scope_id"] == "scope-1"
    scope_sql, scope_params = cur.calls[0]
    assert "INSERT INTO autonomy_context_scopes" in scope_sql
    assert "kind) VALUES (%s, 'isolated:' || gen_random_uuid()::text, 'isolated')" in scope_sql
    assert scope_params == ("u1",)
    insert_sql, insert_params = cur.calls[1]
    assert "context_scope_id" in insert_sql
    assert insert_params[-1] == "scope-1"


@pytest.mark.asyncio
async def test_context_subscribers_selects_only_peers_in_one_shared_scope():
    cur = _Cursor(all_rows=[])
    with patch(f"{_MOD}.get_db_connection", return_value=_Conn(cur)):
        await ticket_db.context_subscribers("thread-1", exclude_ticket_id="t1")
    sql, params = cur.calls[0]
    assert "t.context_scope_id = (" in sql
    assert "origin.ticket_id = %s" in sql
    assert "cs.kind = 'shared'" in sql
    assert params == ("thread-1", "t1", "t1")


@pytest.mark.asyncio
async def test_attach_locks_the_scope_and_counts_under_that_lock():
    cur = _Cursor(
        one=[
            {"scope_id": "scope-1"},
            {"ticket_id": "t2", "user_id": "u1", "context_scope_id": "isolated-1"},
            {"members": 1},
            {"ticket_id": "t2", "user_id": "u1", "context_scope_id": "scope-1"},
        ]
    )
    conn = _Conn(cur)
    with patch(
        "src.server.database.autonomy.context_scopes.get_db_connection",
        return_value=conn,
    ):
        row = await scope_db.set_ticket_scope(
            scope_id="scope-1", ticket_id="t2", max_participants=2
        )

    assert row == {
        "ticket_id": "t2",
        "user_id": "u1",
        "context_scope_id": "scope-1",
    }
    # One transaction holds the scope row lock across count and attach, so two
    # concurrent joins cannot both pass the last free slot.
    assert conn.transactions == 1
    lock_sql, lock_params = cur.calls[0]
    assert "FOR UPDATE" in lock_sql
    assert lock_params == ("scope-1",)
    count_sql, count_params = cur.calls[2]
    assert "COUNT(*)" in count_sql
    assert count_params == ("scope-1",)
    update_sql, update_params = cur.calls[3]
    assert "UPDATE autonomy_tickets" in update_sql
    assert update_params == ("scope-1", "t2")


@pytest.mark.asyncio
async def test_attach_refuses_the_last_slot_holder_under_the_lock():
    cur = _Cursor(
        one=[
            {"scope_id": "scope-1"},
            {"ticket_id": "t2", "user_id": "u1", "context_scope_id": "isolated-1"},
            {"members": 2},
        ]
    )
    conn = _Conn(cur)
    with patch(
        "src.server.database.autonomy.context_scopes.get_db_connection",
        return_value=conn,
    ):
        with pytest.raises(scope_db.ScopeFullError):
            await scope_db.set_ticket_scope(
                scope_id="scope-1", ticket_id="t2", max_participants=2
            )
    assert len(cur.calls) == 3
    assert not any(sql.startswith("UPDATE autonomy_tickets") for sql, _ in cur.calls)


@pytest.mark.asyncio
async def test_attach_of_an_existing_member_skips_the_cap_check():
    cur = _Cursor(
        one=[
            {"scope_id": "scope-1"},
            {"ticket_id": "t2", "user_id": "u1", "context_scope_id": "scope-1"},
        ]
    )
    with patch(
        "src.server.database.autonomy.context_scopes.get_db_connection",
        return_value=_Conn(cur),
    ):
        row = await scope_db.set_ticket_scope(
            scope_id="scope-1", ticket_id="t2", max_participants=1
        )
    assert row["context_scope_id"] == "scope-1"
    assert len(cur.calls) == 2


@pytest.mark.asyncio
async def test_thread_subscribers_ignore_ended_subscriptions():
    cur = _Cursor(all_rows=[])
    with patch(
        "src.server.database.autonomy.context_scopes.get_db_connection",
        return_value=_Conn(cur),
    ):
        assert await scope_db.ticket_ids_for_thread("thread-1") == []

    sql, params = cur.calls[0]
    assert "FROM autonomy_subscriptions" in sql
    # Reassignment keeps the ended row as audit data; it must not be read as a
    # live authorization grant to follow the Thread.
    assert "ended_at IS NULL" in sql
    assert params == ("thread-1",)
