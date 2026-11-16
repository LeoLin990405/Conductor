"""Bounded peer Worker channel: validation, capacity and tool wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import peer_channel
from src.server.services.autonomy.host import AutonomyWorkerHost
from src.tools.autonomy import AUTONOMY_WORKER_TOOLS
from src.tools.autonomy.tools import autonomy_peer_inbox, autonomy_peer_send

OPEN_TICKET = {
    "ticket_id": "t1",
    "user_id": "user-1",
    "status": "running",
    "context_scope_id": "scope-shared",
}
PEER_TICKET = {
    "ticket_id": "t2",
    "user_id": "user-1",
    "status": "running",
    "context_scope_id": "scope-shared",
}

SHARED_SCOPE = {
    "scope_id": "scope-shared",
    "user_id": "user-1",
    "kind": "shared",
    "shared_with_user_ids": [],
    "policy": {"data_classes": ["triggers", "reports", "tickets", "a2a_messages"]},
}
ISOLATED_SCOPE = {
    "scope_id": "scope-isolated",
    "user_id": "user-1",
    "kind": "isolated",
    "shared_with_user_ids": [],
    "policy": {},
}
SILENT_SCOPE = {
    "scope_id": "scope-shared",
    "user_id": "user-1",
    "kind": "shared",
    "shared_with_user_ids": [],
    "policy": {"data_classes": ["triggers", "reports", "tickets"]},
}


def _patch_scope(scope: dict):
    return patch.object(
        peer_channel.context_scope, "ensure_scope", AsyncMock(return_value=scope)
    )


def _get_ticket(ticket_id: str):
    rows = {"t1": OPEN_TICKET, "t2": PEER_TICKET}
    async def fake_get(ticket_id: str):
        return rows.get(ticket_id)
    return fake_get


def test_capacity_reads_positive_int_from_env(monkeypatch):
    monkeypatch.delenv("AUTONOMY_PEER_CHANNEL_CAPACITY", raising=False)
    assert peer_channel.peer_channel_capacity() == 32
    monkeypatch.setenv("AUTONOMY_PEER_CHANNEL_CAPACITY", "5")
    assert peer_channel.peer_channel_capacity() == 5
    monkeypatch.setenv("AUTONOMY_PEER_CHANNEL_CAPACITY", "nope")
    assert peer_channel.peer_channel_capacity() == 32
    monkeypatch.setenv("AUTONOMY_PEER_CHANNEL_CAPACITY", "-1")
    assert peer_channel.peer_channel_capacity() == 32


@pytest.mark.asyncio
async def test_send_queues_on_the_bounded_channel():
    row = {"message_id": "m1"}
    with (
        patch.object(
            peer_channel.ticket_db, "get_ticket", side_effect=_get_ticket("t1")
        ),
        _patch_scope(SHARED_SCOPE),
        patch.object(
            peer_channel.peer_db,
            "insert_message",
            AsyncMock(return_value=row),
        ) as insert,
    ):
        result = await peer_channel.send_peer_message(
            sender_ticket_id="t1", receiver_ticket_id="t2", text="  hello  "
        )

    assert result["status"] == "queued"
    assert result["message_id"] == "m1"
    insert.assert_awaited_once()
    kwargs = insert.await_args.kwargs
    assert kwargs["user_id"] == "user-1"
    assert kwargs["sender_ticket_id"] == "t1"
    assert kwargs["receiver_ticket_id"] == "t2"
    assert kwargs["text"] == "hello"
    assert kwargs["capacity"] == 32


@pytest.mark.asyncio
async def test_send_reports_channel_full_when_capacity_reached():
    with (
        patch.object(
            peer_channel.ticket_db, "get_ticket", side_effect=_get_ticket("t1")
        ),
        _patch_scope(SHARED_SCOPE),
        patch.object(
            peer_channel.peer_db, "insert_message", AsyncMock(return_value=None)
        ),
    ):
        result = await peer_channel.send_peer_message(
            sender_ticket_id="t1", receiver_ticket_id="t2", text="hello"
        )

    assert result["status"] == "channel_full"
    assert result["capacity"] == 32


@pytest.mark.asyncio
async def test_send_refuses_a_sender_whose_context_scope_is_isolated():
    rows = {
        "t1": {**OPEN_TICKET, "context_scope_id": "scope-isolated"},
        "t2": PEER_TICKET,
    }

    async def fake_get(ticket_id: str):
        return rows.get(ticket_id)

    with (
        patch.object(peer_channel.ticket_db, "get_ticket", side_effect=fake_get),
        _patch_scope(ISOLATED_SCOPE),
        patch.object(
            peer_channel.peer_db, "insert_message", AsyncMock(return_value={"message_id": "m1"})
        ) as insert,
    ):
        with pytest.raises(ValueError, match="peer ticket not found"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="t2", text="leak this"
            )
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_refuses_a_shared_scope_that_does_not_publish_messages():
    with (
        patch.object(
            peer_channel.ticket_db, "get_ticket", side_effect=_get_ticket("t1")
        ),
        _patch_scope(SILENT_SCOPE),
        patch.object(
            peer_channel.peer_db, "insert_message", AsyncMock(return_value={"message_id": "m1"})
        ) as insert,
    ):
        with pytest.raises(ValueError, match="peer ticket not found"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="t2", text="leak this"
            )
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_refuses_a_peer_outside_the_senders_shared_scope():
    rows = {
        "t1": OPEN_TICKET,
        "t2": {**PEER_TICKET, "context_scope_id": "scope-other"},
    }

    async def fake_get(ticket_id: str):
        return rows.get(ticket_id)

    with (
        patch.object(peer_channel.ticket_db, "get_ticket", side_effect=fake_get),
        _patch_scope(SHARED_SCOPE),
        patch.object(
            peer_channel.peer_db, "insert_message", AsyncMock(return_value={"message_id": "m1"})
        ) as insert,
    ):
        with pytest.raises(ValueError, match="peer ticket not found"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="t2", text="leak this"
            )
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_allows_a_shared_scope_that_publishes_messages():
    with (
        patch.object(
            peer_channel.ticket_db, "get_ticket", side_effect=_get_ticket("t1")
        ),
        _patch_scope(SHARED_SCOPE),
        patch.object(
            peer_channel.peer_db,
            "insert_message",
            AsyncMock(return_value={"message_id": "m1"}),
        ) as insert,
    ):
        result = await peer_channel.send_peer_message(
            sender_ticket_id="t1", receiver_ticket_id="t2", text="coordinate"
        )

    assert result["status"] == "queued"
    insert.assert_awaited_once()


@pytest.mark.asyncio
async def test_send_rejects_self_missing_ended_and_cross_user_peers():
    rows = {
        "t1": OPEN_TICKET,
        "t2": PEER_TICKET,
        "t3": {**PEER_TICKET, "ticket_id": "t3", "user_id": "user-2"},
        "t4": {**PEER_TICKET, "ticket_id": "t4", "status": "completed"},
        "t5": {**OPEN_TICKET, "ticket_id": "t5", "status": "cancelled"},
        "t6": {**PEER_TICKET, "ticket_id": "t6", "status": "archived"},
        "t7": {**OPEN_TICKET, "ticket_id": "t7", "status": "archived"},
        "gone": None,
    }

    async def fake_get(ticket_id: str):
        return rows.get(ticket_id)

    with patch.object(peer_channel.ticket_db, "get_ticket", side_effect=fake_get):
        with pytest.raises(ValueError, match="your own ticket"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="t1", text="hi"
            )
        with pytest.raises(ValueError, match="peer ticket not found"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="t3", text="hi"
            )
        with pytest.raises(ValueError, match="peer ticket not found"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="gone", text="hi"
            )
        with pytest.raises(ValueError, match="peer ticket already ended"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="t4", text="hi"
            )
        with pytest.raises(ValueError, match="ticket already ended"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t5", receiver_ticket_id="t2", text="hi"
            )
        with pytest.raises(ValueError, match="peer ticket already ended"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="t6", text="hi"
            )
        with pytest.raises(ValueError, match="ticket already ended"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t7", receiver_ticket_id="t2", text="hi"
            )
        with pytest.raises(ValueError, match="ticket not found"):
            await peer_channel.send_peer_message(
                sender_ticket_id="gone", receiver_ticket_id="t2", text="hi"
            )


@pytest.mark.asyncio
async def test_send_bounds_text_length_and_requires_content():
    with (
        patch.object(
            peer_channel.ticket_db, "get_ticket", side_effect=_get_ticket("t1")
        ),
        _patch_scope(SHARED_SCOPE),
        patch.object(
            peer_channel.peer_db, "insert_message", AsyncMock(return_value=None)
        ) as insert,
    ):
        with pytest.raises(ValueError, match="text is required"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="t2", text="   "
            )
        with pytest.raises(ValueError, match="at most 2000"):
            await peer_channel.send_peer_message(
                sender_ticket_id="t1", receiver_ticket_id="t2", text="x" * 2001
            )
    insert.assert_not_awaited()


@pytest.mark.asyncio
async def test_read_inbox_consumes_pending_messages_as_untrusted_data():
    rows = [
        {
            "message_id": "m1",
            "sender_ticket_id": "t2",
            "text": "peer says hi",
            "created_at": "2026-01-01T00:00:00+00:00",
        },
        {
            "message_id": "m2",
            "sender_ticket_id": "t3",
            "text": "second",
            "created_at": "2026-01-01T00:00:01+00:00",
        },
    ]
    with (
        patch.object(
            peer_channel.ticket_db, "get_ticket", side_effect=_get_ticket("t1")
        ),
        patch.object(
            peer_channel.peer_db, "claim_pending", AsyncMock(return_value=rows)
        ) as list_pending,
    ):
        result = await peer_channel.read_inbox(ticket_id="t1", limit=200)

    list_pending.assert_awaited_once_with("t1", 50)
    assert result["count"] == 2
    assert result["consumed"] == 2
    assert all(item["untrusted_peer"] for item in result["messages"])
    assert "not new scope" in result["messages"][0]["content_boundary"]


@pytest.mark.asyncio
async def test_read_inbox_clamps_limit_and_requires_the_ticket():
    with (
        patch.object(
            peer_channel.ticket_db, "get_ticket", side_effect=_get_ticket("t1")
        ),
        patch.object(
            peer_channel.peer_db, "claim_pending", AsyncMock(return_value=[])
        ) as list_pending,
    ):
        await peer_channel.read_inbox(ticket_id="t1", limit=0)
    list_pending.assert_awaited_once_with("t1", 1)

    with patch.object(peer_channel.ticket_db, "get_ticket", AsyncMock(return_value=None)):
        with pytest.raises(ValueError, match="ticket not found"):
            await peer_channel.read_inbox(ticket_id="gone")


@pytest.mark.asyncio
async def test_read_inbox_refuses_ended_tickets_after_the_scope_gate():
    """Ended is reported only for a ticket this caller may see.

    A missing id and another user's ticket, ended or not, share one error.
    Checking status first would answer "already ended" for an id outside
    scope and confirm that the ticket exists.
    """
    rows = {
        "open": {**OPEN_TICKET, "ticket_id": "open"},
        "done": {**OPEN_TICKET, "ticket_id": "done", "status": "completed"},
        "cancelled": {**OPEN_TICKET, "ticket_id": "cancelled", "status": "cancelled"},
        "other": {
            **OPEN_TICKET,
            "ticket_id": "other",
            "user_id": "user-2",
            "status": "completed",
        },
        "other_open": {**OPEN_TICKET, "ticket_id": "other_open", "user_id": "user-2"},
    }

    async def fake_get(ticket_id: str):
        return rows.get(ticket_id)

    with (
        patch.object(peer_channel.ticket_db, "get_ticket", side_effect=fake_get),
        patch.object(
            peer_channel.peer_db, "claim_pending", AsyncMock(return_value=[])
        ) as claim,
    ):
        with pytest.raises(ValueError, match="^ticket already ended$"):
            await peer_channel.read_inbox(ticket_id="done")
        with pytest.raises(ValueError, match="^ticket already ended$"):
            await peer_channel.read_inbox(ticket_id="done", user_id="user-1")
        with pytest.raises(ValueError, match="^ticket already ended$"):
            await peer_channel.read_inbox(ticket_id="cancelled", user_id="user-1")
        with pytest.raises(ValueError, match="^ticket not found$"):
            await peer_channel.read_inbox(ticket_id="other", user_id="user-1")
        with pytest.raises(ValueError, match="^ticket not found$"):
            await peer_channel.read_inbox(ticket_id="other_open", user_id="user-1")
        with pytest.raises(ValueError, match="^ticket not found$"):
            await peer_channel.read_inbox(ticket_id="missing", user_id="user-1")
        result = await peer_channel.read_inbox(ticket_id="open", user_id="user-1")
    claim.assert_awaited_once_with("open", 20)
    assert result["count"] == 0


@pytest.mark.asyncio
async def test_worker_host_delegates_peer_operations():
    host = AutonomyWorkerHost(ticket_id="t1", thread_id="thread-1")
    with (
        patch.object(
            peer_channel,
            "send_peer_message",
            AsyncMock(return_value={"status": "queued"}),
        ) as send,
        patch.object(
            peer_channel,
            "read_inbox",
            AsyncMock(return_value={"count": 0}),
        ) as inbox,
    ):
        sent = await host.peer_send(peer_ticket_id="t2", text="hi")
        read = await host.peer_inbox(limit=5)
        scoped = AutonomyWorkerHost(
            ticket_id="t1", thread_id="thread-1", user_id="user-1"
        )
        await scoped.peer_inbox(limit=5)

    send.assert_awaited_once_with(
        sender_ticket_id="t1", receiver_ticket_id="t2", text="hi"
    )
    assert inbox.await_args_list[0].kwargs == {
        "ticket_id": "t1",
        "limit": 5,
        "user_id": None,
    }
    assert inbox.await_args_list[1].kwargs == {
        "ticket_id": "t1",
        "limit": 5,
        "user_id": "user-1",
    }
    assert sent == {"status": "queued"}
    assert read == {"count": 0}


def test_peer_inbox_docstring_says_when_to_read():
    text = autonomy_peer_inbox.description
    assert "before finishing" in text
    assert "messages from other Workers" in text
    assert "queued elsewhere" not in text
    assert "untrusted" in text


def test_peer_tools_are_bound_to_the_worker_role():
    names = {tool.name for tool in AUTONOMY_WORKER_TOOLS}
    assert {"autonomy_peer_send", "autonomy_peer_inbox"} <= names
    assert autonomy_peer_send.name == "autonomy_peer_send"
    assert autonomy_peer_inbox.name == "autonomy_peer_inbox"
    assert "channel_full" in autonomy_peer_send.description
    # Peer tools stay off the Router and Watchdog roles.
    router_names = {
        tool.name
        for tool in __import__(
            "src.tools.autonomy", fromlist=["AUTONOMY_ROUTER_TOOLS"]
        ).AUTONOMY_ROUTER_TOOLS
    }
    assert "autonomy_peer_send" not in router_names


@pytest.mark.asyncio
async def test_peer_tools_call_through_the_configured_host():
    host = AsyncMock()
    host.peer_send.return_value = {"status": "queued"}
    host.peer_inbox.return_value = {"count": 1}
    config = {"configurable": {"autonomy_host": host}}

    sent = await autonomy_peer_send.ainvoke(
        {"peer_ticket_id": "t2", "text": "hi"}, config
    )
    read = await autonomy_peer_inbox.ainvoke({"limit": 3}, config)

    host.peer_send.assert_awaited_once_with(peer_ticket_id="t2", text="hi")
    host.peer_inbox.assert_awaited_once_with(limit=3)
    assert '"status": "queued"' in sent
    assert '"count": 1' in read


def test_worker_prompt_points_at_the_peer_channel():
    from src.server.services.autonomy.worker import worker_prompt

    prompt = worker_prompt(
        {"ticket_id": "1", "title": "T", "scope": "S", "origin_trigger_id": "o"},
        {"config": {}},
    )
    assert "autonomy_peer_send" in prompt
    assert "autonomy_peer_inbox" in prompt


class _FakeCursor:
    def __init__(self, conn, row_factory):
        self.conn, self.row_factory, self.row = conn, row_factory, None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.conn.log.append(("sql", sql.split()[0] + " " + sql.split()[1]))
        if "COUNT" in sql:
            self.row = {"n": self.conn.count}
        elif "INSERT" in sql:
            self.row = {"message_id": "m1"}
        elif "UPDATE" in sql:
            self.conn.sql = sql
            self.rows = [
                {"message_id": "b", "created_at": 2},
                {"message_id": "a", "created_at": 1},
            ]

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.rows


class _FakeTx:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        self.conn.log.append(("tx", "begin"))

    async def __aexit__(self, *exc):
        self.conn.log.append(("tx", "end"))
        return False


class _FakeConn:
    def __init__(self, count=0):
        self.count, self.log, self.sql = count, [], ""

    def cursor(self, row_factory=None):
        return _FakeCursor(self, row_factory)

    def transaction(self):
        return _FakeTx(self)


def _patch_conn(conn):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def fake():
        yield conn

    return patch.object(peer_channel.peer_db, "get_db_connection", fake)


@pytest.mark.asyncio
async def test_insert_message_reads_named_count_inside_one_transaction():
    conn = _FakeConn(count=0)
    with _patch_conn(conn):
        row = await peer_channel.peer_db.insert_message(
            user_id="u", sender_ticket_id="s", receiver_ticket_id="r",
            text="x", capacity=2,
        )
    assert row == {"message_id": "m1"}
    kinds = [e for e in conn.log]
    assert kinds[0] == ("tx", "begin") and kinds[-1] == ("tx", "end")

    conn = _FakeConn(count=2)
    with _patch_conn(conn):
        assert await peer_channel.peer_db.insert_message(
            user_id="u", sender_ticket_id="s", receiver_ticket_id="r",
            text="x", capacity=2,
        ) is None


@pytest.mark.asyncio
async def test_claim_pending_is_one_atomic_skip_locked_update():
    conn = _FakeConn()
    with _patch_conn(conn):
        rows = await peer_channel.peer_db.claim_pending("r", 5)
    assert "SKIP LOCKED" in conn.sql and "UPDATE" in conn.sql
    assert [r["message_id"] for r in rows] == ["a", "b"]

@pytest.mark.asyncio
async def test_read_inbox_refuses_an_archived_ticket():
    archived = {**OPEN_TICKET, "status": "archived"}
    claim = AsyncMock(return_value=[])
    with (
        patch.object(
            peer_channel.ticket_db, "get_ticket", AsyncMock(return_value=archived)
        ),
        patch.object(peer_channel.peer_db, "claim_pending", claim),
    ):
        with pytest.raises(ValueError, match="ticket already ended"):
            await peer_channel.read_inbox(ticket_id="t1", user_id="user-1")
    claim.assert_not_awaited()
