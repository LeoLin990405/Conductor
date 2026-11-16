"""A2A protocol: signature integrity, schema, scope authorization and delivery."""

from datetime import datetime, timedelta, timezone
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError

from src.server.models.autonomy import A2AEnvelope
from src.server.services.autonomy import a2a

_MOD = "src.server.services.autonomy.a2a"
_TOKEN = "A" * 43
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
_SENDER = uuid4()
_RECIPIENT = uuid4()
_AGENT = {"agent_id": str(uuid4()), "ticket_id": str(_SENDER), "user_id": "u-1"}


def _envelope(**over) -> A2AEnvelope:
    data = {
        "protocol": "conductor.a2a/1",
        "message_id": uuid4(),
        "sender_ticket_id": _SENDER,
        "recipient_ticket_id": _RECIPIENT,
        "intent": "request_review",
        "subject": "Please review",
        "body": "The patch is ready for review.",
        "allowed_actions": ["review"],
        "provenance": {
            "source_kind": "agent",
            "trust": "untrusted",
            "source_id": str(_SENDER),
            "source_thread_key": "github:acme#1",
            "actor": "worker",
            "summary": "ready for review",
        },
        "issued_at": _NOW,
        "expires_at": _NOW + timedelta(hours=1),
    }
    data.update(over)
    return A2AEnvelope.model_validate(data)


def _sender_ticket(**over) -> dict:
    base = {
        "ticket_id": str(_SENDER),
        "user_id": "u-1",
        "context_scope_id": "scope-1",
        "origin_thread_key": "thread-1",
        "status": "running",
    }
    base.update(over)
    return base


def _scope(**over) -> dict:
    base = {
        "scope_id": "scope-1",
        "user_id": "u-1",
        "name": "team",
        "kind": "shared",
        "shared_with_user_ids": [],
        "policy": {
            "data_classes": ["triggers", "reports", "tickets", "a2a_messages"],
            "allow_a2a": True,
            "allow_cross_user": False,
            "max_participants": 10,
            "retention_days": 90,
        },
    }
    base.update(over)
    return base


def _stored(envelope: A2AEnvelope, *, payload_hash: str | None = None) -> dict:
    return {
        "message_id": str(envelope.message_id),
        "sender_agent_id": _AGENT["agent_id"],
        "sender_ticket_id": str(_SENDER),
        "recipient_ticket_id": str(_RECIPIENT),
        "scope_id": "scope-1",
        "message_key": str(envelope.message_id),
        "payload_hash": payload_hash or a2a.payload_hash(envelope),
        "intent": envelope.intent,
        "subject": envelope.subject,
        "body": envelope.body,
        "allowed_actions": list(envelope.allowed_actions),
        "provenance": envelope.provenance.model_dump(mode="json"),
        "issued_at": envelope.issued_at,
        "expires_at": envelope.expires_at,
        "status": "delivered",
        "input_id": "in-1",
        "created_at": _NOW,
    }


def _filtering_list_messages(rows: list[dict]):
    """Stand in for the DB layer: apply the visibility filter it would."""

    async def fake(ticket_id, *, limit, visible_ticket_ids):
        return [
            row
            for row in rows
            if str(row.get("sender_ticket_id") or "") in visible_ticket_ids
            and str(row.get("recipient_ticket_id") or "") in visible_ticket_ids
        ][:limit]

    return fake


def test_signature_covers_the_canonical_envelope():
    envelope = _envelope()
    signature = a2a.sign(_TOKEN, envelope)
    a2a.verify_signature(_TOKEN, envelope, signature)
    tampered = envelope.model_copy(update={"body": "something else"})
    with pytest.raises(a2a.A2AError, match="invalid A2A signature"):
        a2a.verify_signature(_TOKEN, tampered, signature)


def test_schema_forbids_unknown_fields_and_unbounded_windows():
    with pytest.raises(ValidationError):
        A2AEnvelope.model_validate({**_envelope().model_dump(), "extra": True})
    with pytest.raises(ValidationError):
        _envelope(intent="execute")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        _envelope(expires_at=_NOW + timedelta(hours=25))
    with pytest.raises(ValidationError):
        _envelope(allowed_actions=["review", "review"])
    with pytest.raises(ValidationError):
        _envelope(issued_at=datetime(2026, 1, 1, 12, 0))  # naive


@pytest.mark.asyncio
async def test_invalid_token_is_rejected_before_a_database_lookup():
    lookup = AsyncMock()
    with patch(f"{_MOD}.a2a_db.agent_for_token_hash", lookup):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.authenticate("short")
    assert error.value.status_code == 401
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_failures_share_one_message():
    """Malformed and well-formed-but-unknown tokens answer the same 401 shape.

    A distinct message for a well-formed guess would confirm the token is
    worth refining, so every bearer failure in ``authenticate`` raises the
    identical code, status and message.
    """
    shapes = []
    with patch(f"{_MOD}.a2a_db.agent_for_token_hash", AsyncMock(return_value=None)):
        for token in ("short", _TOKEN):
            with pytest.raises(a2a.A2AError) as error:
                await a2a.authenticate(token)
            assert error.value.status_code == 401
            shapes.append((error.value.code, str(error.value)))
    assert shapes == [
        ("unauthenticated", "invalid A2A token"),
        ("unauthenticated", "invalid A2A token"),
    ]


@pytest.mark.asyncio
async def test_a_message_requires_a_shared_a2a_scope():
    envelope = _envelope()
    with (
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(return_value=_sender_ticket())),
        patch(
            f"{_MOD}.context_scope.ensure_scope",
            AsyncMock(return_value=_scope(kind="isolated")),
        ),
    ):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.send_message(
                agent=_AGENT,
                token=_TOKEN,
                envelope=envelope,
                signature=a2a.sign(_TOKEN, envelope),
                now=_NOW,
            )
    assert error.value.code == "isolated_scope"


@pytest.mark.asyncio
async def test_a_happy_path_delivers_one_context_only_input():
    envelope = _envelope()
    deliver = AsyncMock(return_value=(_stored(envelope), True))
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(
                side_effect=[
                    _sender_ticket(),
                    _sender_ticket(
                        ticket_id=str(_RECIPIENT),
                        context_scope_id="scope-1",
                        worker_thread_id=None,
                    ),
                ]
            ),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
        patch(f"{_MOD}.a2a_db.deliver_message", deliver),
        patch(f"{_MOD}.a2a_db.touch_agent", AsyncMock()),
    ):
        row = await a2a.send_message(
            agent=_AGENT,
            token=_TOKEN,
            envelope=envelope,
            signature=a2a.sign(_TOKEN, envelope),
            now=_NOW,
        )
    assert row["replayed"] is False
    kwargs = deliver.await_args.kwargs
    assert kwargs["intent"] == "request_review"
    assert "context only" in kwargs["input_text"]
    assert "context-only" in kwargs["input_text"]
    assert str(envelope.message_id) in kwargs["input_text"]
    assert kwargs["delivery_user_id"] is None


@pytest.mark.asyncio
async def test_a_running_recipient_gets_an_atomic_delivery_effect():
    envelope = _envelope()
    recipient = _sender_ticket(
        ticket_id=str(_RECIPIENT),
        context_scope_id="scope-1",
        worker_thread_id="worker-thread",
    )
    deliver = AsyncMock(return_value=(_stored(envelope), True))
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(side_effect=[_sender_ticket(), recipient]),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
        patch(f"{_MOD}.a2a_db.deliver_message", deliver),
        patch(f"{_MOD}.a2a_db.touch_agent", AsyncMock()),
    ):
        await a2a.send_message(
            agent=_AGENT,
            token=_TOKEN,
            envelope=envelope,
            signature=a2a.sign(_TOKEN, envelope),
            now=_NOW,
        )
    # The effect is inserted by deliver_message in the message transaction,
    # so the service hands it the recipient instead of enqueueing after commit.
    assert deliver.await_args.kwargs["delivery_user_id"] == "u-1"


@pytest.mark.asyncio
async def test_a_replay_returns_the_existing_message_without_redelivery():
    envelope = _envelope()
    deliver = AsyncMock(return_value=(_stored(envelope), False))
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(
                side_effect=[
                    _sender_ticket(),
                    _sender_ticket(ticket_id=str(_RECIPIENT)),
                ]
            ),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
        patch(f"{_MOD}.a2a_db.deliver_message", deliver),
        patch(f"{_MOD}.a2a_db.touch_agent", AsyncMock()) as touch,
    ):
        row = await a2a.send_message(
            agent=_AGENT,
            token=_TOKEN,
            envelope=envelope,
            signature=a2a.sign(_TOKEN, envelope),
            now=_NOW,
        )
    assert row["replayed"] is True
    deliver.assert_awaited_once()
    touch.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_same_message_id_with_different_content_conflicts():
    envelope = _envelope()
    deliver = AsyncMock(return_value=(_stored(envelope, payload_hash="other"), False))
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(
                side_effect=[
                    _sender_ticket(),
                    _sender_ticket(ticket_id=str(_RECIPIENT)),
                ]
            ),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
        patch(f"{_MOD}.a2a_db.deliver_message", deliver),
    ):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.send_message(
                agent=_AGENT,
                token=_TOKEN,
                envelope=envelope,
                signature=a2a.sign(_TOKEN, envelope),
                now=_NOW,
            )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_stale_and_expired_messages_are_refused():
    stale = _envelope(issued_at=_NOW - timedelta(minutes=10))
    with pytest.raises(a2a.A2AError) as error:
        await a2a.send_message(
            agent=_AGENT,
            token=_TOKEN,
            envelope=stale,
            signature=a2a.sign(_TOKEN, stale),
            now=_NOW,
        )
    assert error.value.status_code == 400

    expired = _envelope(
        issued_at=_NOW - timedelta(minutes=2),
        expires_at=_NOW - timedelta(seconds=1),
    )
    with pytest.raises(a2a.A2AError) as error:
        await a2a.send_message(
            agent=_AGENT,
            token=_TOKEN,
            envelope=expired,
            signature=a2a.sign(_TOKEN, expired),
            now=_NOW,
        )
    assert error.value.status_code == 410


@pytest.mark.asyncio
async def test_the_envelope_sender_must_match_the_authenticated_agent():
    envelope = _envelope(sender_ticket_id=uuid4())
    with pytest.raises(a2a.A2AError) as error:
        await a2a.send_message(
            agent=_AGENT,
            token=_TOKEN,
            envelope=envelope,
            signature=a2a.sign(_TOKEN, envelope),
            now=_NOW,
        )
    assert error.value.code == "sender_mismatch"


@pytest.mark.asyncio
async def test_cross_user_recipient_needs_an_explicit_mapping():
    envelope = _envelope()
    recipient = _sender_ticket(ticket_id=str(_RECIPIENT), user_id="u-2")
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(side_effect=[_sender_ticket(), recipient]),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
        patch(f"{_MOD}.a2a_db.deliver_message", AsyncMock()) as deliver,
    ):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.send_message(
                agent=_AGENT,
                token=_TOKEN,
                envelope=envelope,
                signature=a2a.sign(_TOKEN, envelope),
                now=_NOW,
            )
    assert error.value.status_code == 403
    deliver.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_ended_recipient_outside_the_scope_is_not_disclosed():
    """The scope gate answers before the recipient's existence or state.

    A sender outside the recipient's scope used to learn that the recipient
    exists and has ended (409 ``recipient_finished``); the share denial must
    come first.
    """
    envelope = _envelope()
    recipient = _sender_ticket(
        ticket_id=str(_RECIPIENT),
        context_scope_id="other-scope",
        status="completed",
    )
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(side_effect=[_sender_ticket(), recipient]),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
    ):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.send_message(
                agent=_AGENT,
                token=_TOKEN,
                envelope=envelope,
                signature=a2a.sign(_TOKEN, envelope),
                now=_NOW,
            )
    assert error.value.status_code == 403
    assert error.value.code == "scope"


@pytest.mark.asyncio
async def test_a_missing_recipient_outside_the_scope_is_not_disclosed():
    envelope = _envelope()
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(side_effect=[_sender_ticket(), None]),
        ),
        patch(
            f"{_MOD}.context_scope.ensure_scope",
            AsyncMock(return_value=_scope(kind="isolated")),
        ),
    ):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.send_message(
                agent=_AGENT,
                token=_TOKEN,
                envelope=envelope,
                signature=a2a.sign(_TOKEN, envelope),
                now=_NOW,
            )
    assert error.value.code == "isolated_scope"


@pytest.mark.asyncio
async def test_a_missing_recipient_answers_403_even_inside_the_scope():
    """A missing recipient is indistinguishable from one outside the scope.

    A 404 would confirm to any holder of a valid agent token that the guessed
    ticket id does not exist; only the scope denial is safe to answer.
    """
    envelope = _envelope()
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(side_effect=[_sender_ticket(), None]),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
    ):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.send_message(
                agent=_AGENT,
                token=_TOKEN,
                envelope=envelope,
                signature=a2a.sign(_TOKEN, envelope),
                now=_NOW,
            )
    assert error.value.status_code == 403
    assert error.value.code == "scope"


@pytest.mark.asyncio
async def test_missing_and_out_of_scope_recipients_are_indistinguishable():
    """The recipient gate answers one code, status and message for every denial."""
    shapes = []
    messages = []
    recipient_lookups = [
        None,  # missing ticket
        _sender_ticket(ticket_id=str(_RECIPIENT), context_scope_id="other-scope"),
        _sender_ticket(ticket_id=str(_RECIPIENT), user_id="u-2"),
    ]
    for recipient in recipient_lookups:
        envelope = _envelope()
        with (
            patch(
                f"{_MOD}.ticket_db.get_ticket",
                AsyncMock(side_effect=[_sender_ticket(), recipient]),
            ),
            patch(
                f"{_MOD}.context_scope.ensure_scope",
                AsyncMock(return_value=_scope()),
            ),
        ):
            with pytest.raises(a2a.A2AError) as error:
                await a2a.send_message(
                    agent=_AGENT,
                    token=_TOKEN,
                    envelope=envelope,
                    signature=a2a.sign(_TOKEN, envelope),
                    now=_NOW,
                )
        assert error.value.status_code == 403
        shapes.append((error.value.code, error.value.status_code))
        messages.append(str(error.value))
    assert shapes == [("scope", 403)] * len(recipient_lookups)
    assert len(set(messages)) == 1


@pytest.mark.asyncio
async def test_an_ended_recipient_inside_the_scope_still_conflicts():
    """409 stays reachable, but only for a caller inside the scope."""
    envelope = _envelope()
    recipient = _sender_ticket(ticket_id=str(_RECIPIENT), status="completed")
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(side_effect=[_sender_ticket(), recipient]),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
    ):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.send_message(
                agent=_AGENT,
                token=_TOKEN,
                envelope=envelope,
                signature=a2a.sign(_TOKEN, envelope),
                now=_NOW,
            )
    assert error.value.status_code == 409
    assert error.value.code == "recipient_finished"


@pytest.mark.asyncio
async def test_list_messages_withholds_rows_outside_the_ticket_scope():
    """A counterparty that left the readable set takes its message bodies with it."""
    peer_visible = str(uuid4())
    peer_hidden = str(uuid4())
    env_visible = _envelope()
    env_hidden = _envelope()
    rows = [
        {
            **_stored(env_visible),
            "sender_ticket_id": peer_visible,
            "recipient_ticket_id": str(_SENDER),
        },
        {
            **_stored(env_hidden),
            "sender_ticket_id": peer_hidden,
            "recipient_ticket_id": str(_SENDER),
        },
    ]
    with (
        patch(
            f"{_MOD}.a2a_db.list_messages",
            new=_filtering_list_messages(rows),
        ),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value=_sender_ticket()),
        ),
        patch(f"{_MOD}.context_scope.lookup_scope", AsyncMock(return_value=_scope())),
        patch(
            f"{_MOD}.context_scope.readable_ticket_ids",
            AsyncMock(return_value={str(_SENDER), peer_visible}),
        ),
    ):
        visible = await a2a.list_messages(str(_SENDER))
    assert [row["sender_ticket_id"] for row in visible] == [peer_visible]
    assert visible[0]["body"] == env_visible.body


@pytest.mark.asyncio
async def test_list_messages_for_an_isolated_ticket_returns_only_self_rows():
    env = _envelope()
    rows = [
        {
            **_stored(env),
            "sender_ticket_id": str(_SENDER),
            "recipient_ticket_id": str(_RECIPIENT),
        },
        {
            **_stored(env),
            "sender_ticket_id": str(_SENDER),
            "recipient_ticket_id": str(_SENDER),
        },
    ]
    with (
        patch(
            f"{_MOD}.a2a_db.list_messages",
            new=_filtering_list_messages(rows),
        ),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value=_sender_ticket()),
        ),
        patch(
            f"{_MOD}.context_scope.lookup_scope",
            AsyncMock(return_value=_scope(kind="isolated")),
        ),
        patch(
            f"{_MOD}.context_scope.readable_ticket_ids",
            AsyncMock(return_value={str(_SENDER)}),
        ),
    ):
        visible = await a2a.list_messages(str(_SENDER))
    assert [row["recipient_ticket_id"] for row in visible] == [str(_SENDER)]


@pytest.mark.asyncio
async def test_list_messages_without_a_scope_reads_only_self_and_never_creates():
    """A GET narrows to the Ticket's own rows and must not write a scope."""
    env = _envelope()
    rows = [
        {
            **_stored(env),
            "sender_ticket_id": str(_SENDER),
            "recipient_ticket_id": str(_SENDER),
        },
        {
            **_stored(env),
            "sender_ticket_id": str(_SENDER),
            "recipient_ticket_id": str(_RECIPIENT),
        },
    ]
    with (
        patch(
            f"{_MOD}.a2a_db.list_messages",
            new=_filtering_list_messages(rows),
        ),
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(return_value=_sender_ticket(context_scope_id=None)),
        ),
        patch(
            f"{_MOD}.context_scope.ensure_scope",
            AsyncMock(side_effect=AssertionError("a GET must not create a scope")),
        ),
        patch(
            "src.server.database.autonomy.context_scopes.create_isolated_scope",
            AsyncMock(),
        ) as create,
    ):
        visible = await a2a.list_messages(str(_SENDER))
    create.assert_not_awaited()
    assert [row["recipient_ticket_id"] for row in visible] == [str(_SENDER)]


@pytest.mark.asyncio
async def test_list_messages_drops_everything_when_the_ticket_is_gone():
    list_rows = AsyncMock(return_value=[{"x": 1}])
    with (
        patch(f"{_MOD}.a2a_db.list_messages", list_rows),
        patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(return_value=None)),
    ):
        assert await a2a.list_messages(str(_SENDER)) == []
    list_rows.assert_not_awaited()


@pytest.mark.asyncio
async def test_credentials_in_text_are_redacted_before_storage():
    token = "ghp_" + "a" * 30
    provenance = _envelope().provenance.model_dump(mode="json")
    provenance["summary"] = f"also {token}"
    envelope = _envelope(body=f"the token is {token}", provenance=provenance)
    deliver = AsyncMock(return_value=(_stored(envelope), True))
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(
                side_effect=[
                    _sender_ticket(),
                    _sender_ticket(ticket_id=str(_RECIPIENT)),
                ]
            ),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
        patch(f"{_MOD}.a2a_db.deliver_message", deliver),
        patch(f"{_MOD}.a2a_db.touch_agent", AsyncMock()),
    ):
        await a2a.send_message(
            agent=_AGENT,
            token=_TOKEN,
            envelope=envelope,
            signature=a2a.sign(_TOKEN, envelope),
            now=_NOW,
        )
    kwargs = deliver.await_args.kwargs
    assert token not in kwargs["body"]
    assert "[REDACTED]" in kwargs["body"]
    assert token not in kwargs["provenance"]["summary"]
    assert "[REDACTED]" in kwargs["provenance"]["summary"]
    # The rendered context block is built from the redacted copy too.
    assert token not in kwargs["input_text"]

@pytest.mark.asyncio
async def test_an_archived_recipient_inside_the_scope_still_conflicts():
    """Archive is terminal for A2A delivery just like completed/cancelled."""
    envelope = _envelope()
    recipient = _sender_ticket(ticket_id=str(_RECIPIENT), status="archived")
    with (
        patch(
            f"{_MOD}.ticket_db.get_ticket",
            AsyncMock(side_effect=[_sender_ticket(), recipient]),
        ),
        patch(f"{_MOD}.context_scope.ensure_scope", AsyncMock(return_value=_scope())),
    ):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.send_message(
                agent=_AGENT,
                token=_TOKEN,
                envelope=envelope,
                signature=a2a.sign(_TOKEN, envelope),
                now=_NOW,
            )
    assert error.value.status_code == 409
    assert error.value.code == "recipient_finished"


@pytest.mark.asyncio
async def test_an_archived_sender_is_refused():
    envelope = _envelope()
    sender = _sender_ticket(status="archived")
    with patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(return_value=sender)):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.send_message(
                agent=_AGENT,
                token=_TOKEN,
                envelope=envelope,
                signature=a2a.sign(_TOKEN, envelope),
                now=_NOW,
            )
    assert error.value.status_code == 403
    assert error.value.code == "sender"


@pytest.mark.asyncio
async def test_an_archived_ticket_cannot_register_an_a2a_agent():
    sender = _sender_ticket(status="archived")
    with patch(f"{_MOD}.ticket_db.get_ticket", AsyncMock(return_value=sender)):
        with pytest.raises(a2a.A2AError) as error:
            await a2a.register_agent(user_id="u-1", ticket_id=str(_SENDER))
    assert error.value.status_code == 409
    assert error.value.code == "ticket_finished"
