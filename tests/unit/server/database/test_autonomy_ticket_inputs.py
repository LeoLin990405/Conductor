"""Ticket input deduplication must not signal a change to the ticket."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.server.database.autonomy import tickets


class _Cursor:
    def __init__(self, fetchone_results):
        self.fetchone_results = iter(fetchone_results)
        self.executions: list[tuple[str, tuple]] = []

    async def execute(self, sql: str, params=()):
        self.executions.append((" ".join(sql.split()), tuple(params)))

    async def fetchone(self):
        return next(self.fetchone_results)


class _Connection:
    def __init__(self, cursor: _Cursor):
        self.fake_cursor = cursor

    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield self.fake_cursor

    @asynccontextmanager
    async def transaction(self):
        yield self


def _patch_connection(monkeypatch, cursor: _Cursor) -> None:
    @asynccontextmanager
    async def get_connection():
        yield _Connection(cursor)

    monkeypatch.setattr(tickets, "get_db_connection", get_connection)


@pytest.mark.asyncio
async def test_add_input_bumps_revision_when_insert_succeeds(monkeypatch):
    inserted = {
        "input_id": "input-1",
        "ticket_id": "ticket-1",
        "trigger_id": "trigger-1",
        "kind": "reply",
        "text": "Continue",
    }
    cursor = _Cursor([inserted, {"revision": 8}])
    _patch_connection(monkeypatch, cursor)

    result = await tickets.add_input(
        ticket_id="ticket-1",
        trigger_id="trigger-1",
        kind="reply",
        text="Continue",
    )

    assert result["input_id"] == "input-1"
    # The revision this insert moved to, so a later fence can use it.
    assert result["ticket_revision"] == 8
    assert [sql for sql, _params in cursor.executions] == [
        "INSERT INTO autonomy_ticket_inputs (ticket_id, trigger_id, kind, text) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (ticket_id, trigger_id) "
        "WHERE trigger_id IS NOT NULL DO NOTHING RETURNING input_id, ticket_id, "
        "trigger_id, kind, text, delivered_at, read_at, created_at",
        "UPDATE autonomy_tickets SET revision = revision + 1 WHERE ticket_id = %s "
        "RETURNING revision",
    ]
    assert cursor.executions[1][1] == ("ticket-1",)


@pytest.mark.asyncio
async def test_add_input_dedupe_noop_returns_existing_input_without_revision_bump(
    monkeypatch,
):
    existing = {
        "input_id": "input-1",
        "ticket_id": "ticket-1",
        "trigger_id": "trigger-1",
        "kind": "reply",
        "text": "Continue",
    }
    cursor = _Cursor([None, existing])
    _patch_connection(monkeypatch, cursor)

    result = await tickets.add_input(
        ticket_id="ticket-1",
        trigger_id="trigger-1",
        kind="reply",
        text="Continue",
    )

    assert result["input_id"] == "input-1"
    assert result["ticket_id"] == "ticket-1"
    assert [sql for sql, _params in cursor.executions] == [
        "INSERT INTO autonomy_ticket_inputs (ticket_id, trigger_id, kind, text) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (ticket_id, trigger_id) "
        "WHERE trigger_id IS NOT NULL DO NOTHING RETURNING input_id, ticket_id, "
        "trigger_id, kind, text, delivered_at, read_at, created_at",
        "SELECT input_id, ticket_id, trigger_id, kind, text, delivered_at, "
        "read_at, created_at FROM autonomy_ticket_inputs WHERE ticket_id = %s "
        "AND trigger_id = %s",
    ]
    assert cursor.executions[-1][1] == ("ticket-1", "trigger-1")


@pytest.mark.asyncio
async def test_owned_insert_refuses_a_plan_discarded_before_the_write(monkeypatch):
    locked = {
        "ticket_id": "ticket-1",
        "status": "completed",
        "mandate": {"speculative": True},
    }
    cursor = _Cursor([locked])
    _patch_connection(monkeypatch, cursor)

    result = await tickets.add_input(
        ticket_id="ticket-1",
        trigger_id="trigger-1",
        kind="trigger",
        text="again",
        only_if_owning=True,
    )

    assert result is None
    assert [sql for sql, _params in cursor.executions] == [
        "SELECT ticket_id, status, mandate FROM autonomy_tickets "
        "WHERE ticket_id = %s FOR UPDATE"
    ]


@pytest.mark.asyncio
async def test_owned_insert_still_writes_when_the_ticket_owns_the_thread(monkeypatch):
    locked = {
        "ticket_id": "ticket-1",
        "status": "waiting",
        "mandate": {"speculative": True},
    }
    inserted = {
        "input_id": "input-1",
        "ticket_id": "ticket-1",
        "trigger_id": "trigger-1",
        "kind": "trigger",
        "text": "again",
    }
    cursor = _Cursor([locked, inserted, {"revision": 4}])
    _patch_connection(monkeypatch, cursor)

    result = await tickets.add_input(
        ticket_id="ticket-1",
        trigger_id="trigger-1",
        kind="trigger",
        text="again",
        only_if_owning=True,
    )

    assert result is not None
    assert result["input_id"] == "input-1"
    sqls = [sql for sql, _params in cursor.executions]
    assert sqls[0].endswith("FOR UPDATE")
    assert sqls[1].startswith("INSERT INTO autonomy_ticket_inputs")
    assert "revision = revision + 1" in sqls[2]


@pytest.mark.asyncio
async def test_owned_insert_refuses_an_ordinary_completed_ticket(monkeypatch):
    """The lifecycle rule makes every terminal Ticket settled for good: the
    ownership fence refuses an ordinary completed Ticket, not only a
    discarded speculative plan, so the signal stays pending for a new Eval."""
    locked = {
        "ticket_id": "ticket-1",
        "status": "completed",
        "mandate": {"source_id": "s1"},
    }
    inserted = {
        "input_id": "input-1",
        "ticket_id": "ticket-1",
        "trigger_id": "trigger-1",
        "kind": "trigger",
        "text": "again",
    }
    cursor = _Cursor([locked, inserted])
    _patch_connection(monkeypatch, cursor)

    result = await tickets.add_input(
        ticket_id="ticket-1",
        trigger_id="trigger-1",
        kind="trigger",
        text="again",
        only_if_owning=True,
    )

    assert result is None
    assert not any(
        sql.startswith("INSERT INTO autonomy_ticket_inputs")
        for sql, _params in cursor.executions
    )
