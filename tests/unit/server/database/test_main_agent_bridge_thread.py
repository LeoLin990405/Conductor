"""Idempotency keys for deliberation events on a shared Ticket thread."""

from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import NAMESPACE_URL, uuid5

import pytest

from src.server.database import main_agent_bridge
from src.server.database.autonomy import tickets


class _Cursor:
    async def execute(self, _sql, _params):
        pass

    async def fetchone(self):
        return {"ticket_id": "ticket-1"}


class _Connection:
    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield _Cursor()


def _install_fakes(monkeypatch):
    @asynccontextmanager
    async def get_connection():
        yield _Connection()

    calls: list[dict] = []

    async def add_input(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(main_agent_bridge, "get_db_connection", get_connection)
    monkeypatch.setattr(tickets, "add_input", add_input)
    return calls


@pytest.mark.asyncio
async def test_event_key_deduplicates_retry_but_separates_child_from_parent(monkeypatch):
    calls = _install_fakes(monkeypatch)
    append = main_agent_bridge.append_shared_thread_message
    shared = {
        "thread_key": "main-agent:ticket-1",
        "author": "planner",
        "kind": "continuation",
        "body": "Same visible text",
        "round_index": 0,
    }

    await append(**shared, event_key="deliberation:parent-1:continuation")
    await append(**shared, event_key="deliberation:parent-1:continuation")
    await append(**shared, event_key="deliberation:child-1:continuation")

    assert calls[0]["trigger_id"] == calls[1]["trigger_id"]
    assert calls[0]["trigger_id"] != calls[2]["trigger_id"]
    assert all(call["ticket_id"] == "ticket-1" for call in calls)


@pytest.mark.asyncio
async def test_legacy_append_keeps_content_based_trigger_identity(monkeypatch):
    calls = _install_fakes(monkeypatch)
    payload = {
        "thread_key": "main-agent:ticket-1",
        "author": "alpha",
        "kind": "proposal",
        "body": "A proposal",
        "round_index": 2,
    }

    await main_agent_bridge.append_shared_thread_message(**payload)

    dedupe_key = "2:alpha:proposal:A proposal"
    expected = str(
        uuid5(
            NAMESPACE_URL,
            f"main-agent-thread:{payload['thread_key']}:{dedupe_key}",
        )
    )
    assert calls[0]["trigger_id"] == expected

@pytest.mark.asyncio
async def test_finalize_root_ticket_leaves_an_archived_ticket_alone(monkeypatch):
    executed: list[str] = []

    class Cursor:
        async def execute(self, sql, _params=None):
            executed.append(" ".join(sql.split()))

        async def fetchone(self):
            return {
                "ticket_id": "ticket-1",
                "status": "archived",
                "mandate": {"orchestration_only": True},
            }

    class Connection:
        @asynccontextmanager
        async def cursor(self, **_kwargs):
            yield Cursor()

    @asynccontextmanager
    async def get_connection():
        yield Connection()

    monkeypatch.setattr(main_agent_bridge, "get_db_connection", get_connection)

    await main_agent_bridge.finalize_root_ticket(
        ticket_id="ticket-1", user_id="u-1", status="completed", summary="done"
    )

    assert not any("UPDATE autonomy_tickets" in sql for sql in executed)
