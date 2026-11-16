"""A2A persistence: the message, its Input and its delivery effect are one commit."""

from unittest.mock import patch

import pytest

from src.server.database.autonomy import a2a as a2a_db

_MOD = "src.server.database.autonomy.a2a"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Cursor:
    def __init__(self, *, one=None):
        self.one = list(one or [])
        self.calls = []

    async def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    async def fetchone(self):
        return self.one.pop(0)

    async def fetchall(self):
        return []

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
        return _Tx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _message_row(**overrides) -> dict:
    row = {
        "message_id": "m-1",
        "user_id": "u-1",
        "sender_agent_id": "a-1",
        "sender_ticket_id": "t-1",
        "recipient_ticket_id": "t-2",
        "scope_id": "s-1",
        "message_key": "msg-1",
        "payload_hash": "hash",
        "intent": "inform",
        "subject": "subject",
        "body": "body",
        "allowed_actions": [],
        "provenance": {},
        "issued_at": None,
        "expires_at": None,
        "status": "delivered",
        "rejection": None,
        "input_id": None,
        "created_at": None,
    }
    row.update(overrides)
    return row


async def _deliver(
    cur,
    *,
    delivery_user_id="u-2",
    sender_ticket_id="t-1",
    message_key="msg-1",
):
    with patch(f"{_MOD}.get_db_connection", return_value=_Conn(cur)):
        return await a2a_db.deliver_message(
            user_id="u-1",
            sender_agent_id="a-1",
            sender_ticket_id=sender_ticket_id,
            recipient_ticket_id="t-2",
            scope_id="s-1",
            message_key=message_key,
            payload_hash="hash",
            intent="inform",
            subject="subject",
            body="body",
            allowed_actions=[],
            provenance={},
            issued_at=None,
            expires_at=None,
            input_text="message as context",
            delivery_user_id=delivery_user_id,
        )


@pytest.mark.asyncio
async def test_the_delivery_effect_is_inserted_in_the_message_transaction():
    cur = _Cursor(
        one=[
            _message_row(),
            {"input_id": "in-1"},
            # No delivery effect for this Input yet.
            None,
            {"effect_id": "e-1"},
        ]
    )
    conn = _Conn(cur)
    with patch(f"{_MOD}.get_db_connection", return_value=conn):
        row, created = await a2a_db.deliver_message(
            user_id="u-1",
            sender_agent_id="a-1",
            sender_ticket_id="t-1",
            recipient_ticket_id="t-2",
            scope_id="s-1",
            message_key="msg-1",
            payload_hash="hash",
            intent="inform",
            subject="subject",
            body="body",
            allowed_actions=[],
            provenance={},
            issued_at=None,
            expires_at=None,
            input_text="message as context",
            delivery_user_id="u-2",
        )

    assert created is True
    assert row["input_id"] == "in-1"
    assert conn.transactions == 1
    effect_sql, effect_params = cur.calls[-1]
    assert "INSERT INTO autonomy_effects" in effect_sql
    # The effect is keyed by the Input, which belongs to this message alone.
    assert effect_params[:3] == ("u-2", "deliver_input", "autonomy:input:in-1")
    assert "in-1" in str(effect_params[3])


@pytest.mark.asyncio
async def test_a_daemon_message_stores_no_backend_effect():
    cur = _Cursor(one=[_message_row(), {"input_id": "in-1"}])
    row, created = await _deliver(cur, delivery_user_id=None)

    assert created is True
    assert row["input_id"] == "in-1"
    assert not any("INSERT INTO autonomy_effects" in sql for sql, _ in cur.calls)


@pytest.mark.asyncio
async def test_a_replay_inserts_neither_an_input_nor_a_second_effect():
    cur = _Cursor(
        one=[
            None,
            {**_message_row(), "input_id": "in-1"},
            # The stored message already has its delivery effect.
            {"effect_id": "e-1"},
        ]
    )
    row, created = await _deliver(cur)

    assert created is False
    assert row["input_id"] == "in-1"
    assert not any("INSERT INTO autonomy_ticket_inputs" in sql for sql, _ in cur.calls)
    assert not any("INSERT INTO autonomy_effects" in sql for sql, _ in cur.calls)


@pytest.mark.asyncio
async def test_a_replay_repairs_a_message_whose_delivery_effect_was_lost():
    """A replay stops at the stored message, so a message whose effect was
    lost could never be delivered. The replay repairs it."""
    cur = _Cursor(
        one=[
            None,
            {**_message_row(), "input_id": "in-1"},
            # No delivery effect for this Input.
            None,
            {"effect_id": "e-2"},
        ]
    )
    row, created = await _deliver(cur)

    assert created is False
    assert row["input_id"] == "in-1"
    effect_sql, effect_params = cur.calls[-1]
    assert "INSERT INTO autonomy_effects" in effect_sql
    assert effect_params[:3] == ("u-2", "deliver_input", "autonomy:input:in-1")


@pytest.mark.asyncio
async def test_two_senders_may_use_one_message_id():
    """A message id belongs to its sender: two senders may reuse one, and each
    message's delivery effect is keyed by its own Input, so the second
    sender's effect is not dropped by a globally unique key."""
    first = _Cursor(
        one=[_message_row(), {"input_id": "in-1"}, None, {"effect_id": "e-1"}]
    )
    second = _Cursor(
        one=[
            {**_message_row(), "message_id": "m-2"},
            {"input_id": "in-2"},
            None,
            {"effect_id": "e-2"},
        ]
    )

    await _deliver(first, sender_ticket_id="t-1")
    await _deliver(second, sender_ticket_id="t-3")

    first_key = first.calls[-1][1][2]
    second_key = second.calls[-1][1][2]
    assert first_key == "autonomy:input:in-1"
    assert second_key == "autonomy:input:in-2"
    assert first_key != second_key
