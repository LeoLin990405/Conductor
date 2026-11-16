"""Autonomy scope and A2A routes: ownership, schema and bearer/signature auth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.database.autonomy import context_scopes as scope_db
from src.server.services.autonomy import a2a as a2a_service
from tests.conftest import create_test_app

USER_ID = "test-user-123"
_AGENT_TICKET = str(uuid4())
_RECIPIENT = str(uuid4())
_SCOPE_ID = str(uuid4())
_NOW = datetime.now(timezone.utc)
_TOKEN = "B" * 43


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _scope() -> dict:
    return {
        "scope_id": _SCOPE_ID,
        "user_id": USER_ID,
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
        "created_at": _NOW,
        "updated_at": _NOW,
    }


def _ticket() -> dict:
    base = {
        "ticket_id": _AGENT_TICKET,
        "user_id": USER_ID,
        "context_scope_id": _SCOPE_ID,
        "origin_thread_key": "thread-1",
        "status": "running",
    }
    return base


def _message_row(envelope) -> dict:
    return {
        "message_id": str(envelope.message_id),
        "sender_agent_id": str(uuid4()),
        "sender_ticket_id": str(envelope.sender_ticket_id),
        "recipient_ticket_id": str(envelope.recipient_ticket_id),
        "scope_id": _SCOPE_ID,
        "message_key": str(envelope.message_id),
        "payload_hash": a2a_service.payload_hash(envelope),
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
        "replayed": False,
    }


def _envelope_body() -> dict:
    # The server checks the envelope's clock window against the request time,
    # so it is anchored to the call rather than to import time: a slow suite
    # must not turn a valid message into a stale one.
    issued = datetime.now(timezone.utc)
    return {
        "protocol": "conductor.a2a/1",
        "message_id": str(uuid4()),
        "sender_ticket_id": _AGENT_TICKET,
        "recipient_ticket_id": _RECIPIENT,
        "intent": "inform",
        "subject": "Heads up",
        "body": "The shared report is ready.",
        "allowed_actions": ["read_context"],
        "provenance": {
            "source_kind": "agent",
            "trust": "untrusted",
            "source_id": _AGENT_TICKET,
            "actor": "worker",
        },
        "issued_at": issued.isoformat(),
        "expires_at": (issued + timedelta(hours=1)).isoformat(),
    }


@pytest.mark.asyncio
async def test_scope_creation_is_feature_gated(client):
    with patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=False),
    ):
        resp = await client.post(
            "/api/v1/autonomy/context-scopes",
            json={"name": "team", "kind": "shared"},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_scope_creation_returns_the_policy_and_members(client):
    scope = _scope()
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.scope_db.create_scope",
            new=AsyncMock(return_value=scope),
        ) as create,
        patch(
            "src.server.app.autonomy.scope_db.list_scope_ticket_ids",
            new=AsyncMock(return_value=[_AGENT_TICKET]),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/context-scopes",
            json={
                "name": "team",
                "kind": "shared",
                "policy": {"allow_a2a": True},
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["kind"] == "shared"
    assert body["policy"]["allow_a2a"] is True
    assert body["ticket_ids"] == [_AGENT_TICKET]
    assert create.await_args.kwargs["shared_with_user_ids"] == []


@pytest.mark.asyncio
async def test_cross_user_scope_needs_a_named_peer(client):
    with patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=True),
    ):
        resp = await client.post(
            "/api/v1/autonomy/context-scopes",
            json={
                "name": "team",
                "kind": "shared",
                "policy": {"allow_cross_user": True},
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_an_isolated_scope_cannot_name_another_user(client):
    with patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=True),
    ):
        resp = await client.post(
            "/api/v1/autonomy/context-scopes",
            json={
                "name": "mine",
                "kind": "isolated",
                "shared_with_user_ids": ["someone-else"],
            },
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_isolated_scope_creation_defaults_to_one_participant(client):
    scope = {
        **_scope(),
        "kind": "isolated",
        "policy": {
            "data_classes": ["triggers", "reports", "tickets", "a2a_messages"],
            "allow_a2a": False,
            "allow_cross_user": False,
            "max_participants": 1,
            "retention_days": 90,
        },
    }
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.scope_db.create_scope",
            new=AsyncMock(return_value=scope),
        ) as create,
        patch(
            "src.server.app.autonomy.scope_db.list_scope_ticket_ids",
            new=AsyncMock(return_value=[]),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/context-scopes",
            json={"name": "mine", "kind": "isolated"},
        )
    assert resp.status_code == 201, resp.text
    assert create.await_args.kwargs["policy"]["max_participants"] == 1
    assert create.await_args.kwargs["policy"]["allow_a2a"] is False


@pytest.mark.asyncio
async def test_attaching_a_foreign_ticket_is_not_found(client):
    scope = _scope()
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.scope_db.get_scope",
            new=AsyncMock(return_value=scope),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/context-scopes/{scope['scope_id']}/tickets",
            json={"ticket_id": _AGENT_TICKET},
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ticket_attach_passes_the_cap_to_one_atomic_database_operation(client):
    scope = _scope()
    attach = AsyncMock(
        return_value={
            "ticket_id": _AGENT_TICKET,
            "user_id": USER_ID,
            "context_scope_id": _SCOPE_ID,
        }
    )
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.scope_db.get_scope",
            new=AsyncMock(return_value=scope),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value={**_ticket(), "context_scope_id": str(uuid4())}),
        ),
        patch("src.server.app.autonomy.scope_db.set_ticket_scope", new=attach),
        patch(
            "src.server.app.autonomy.scope_db.list_scope_ticket_ids",
            new=AsyncMock(return_value=[_AGENT_TICKET]),
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/context-scopes/{scope['scope_id']}/tickets",
            json={"ticket_id": _AGENT_TICKET},
        )
    assert resp.status_code == 201, resp.text
    assert attach.await_args.kwargs["max_participants"] == 10


@pytest.mark.asyncio
async def test_ticket_attach_reports_a_full_scope(client):
    scope = _scope()
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.scope_db.get_scope",
            new=AsyncMock(return_value=scope),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value={**_ticket(), "context_scope_id": str(uuid4())}),
        ),
        patch(
            "src.server.app.autonomy.scope_db.set_ticket_scope",
            new=AsyncMock(side_effect=scope_db.ScopeFullError("full")),
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/context-scopes/{scope['scope_id']}/tickets",
            json={"ticket_id": _AGENT_TICKET},
        )
    assert resp.status_code == 409
    assert "full" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_agent_registration_returns_the_token_once(client):
    agent = {
        "agent_id": str(uuid4()),
        "ticket_id": _AGENT_TICKET,
        "user_id": USER_ID,
        "name": "reviewer",
        "revoked_at": None,
        "created_at": _NOW,
    }
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.a2a_service.register_agent",
            new=AsyncMock(return_value=(agent, _TOKEN)),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/a2a/agents",
            json={"ticket_id": _AGENT_TICKET, "name": "reviewer"},
        )
    assert resp.status_code == 201
    assert resp.json()["token"] == _TOKEN
    assert resp.json()["ticket_id"] == _AGENT_TICKET


@pytest.mark.asyncio
async def test_message_route_requires_a_bearer_token(client):
    resp = await client.post("/api/v1/autonomy/a2a/messages", json=_envelope_body())
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_message_schema_rejects_unknown_fields_before_service(client):
    with patch(
        "src.server.app.autonomy.a2a_service.authenticate",
        new=AsyncMock(return_value={"ticket_id": _AGENT_TICKET}),
    ) as authenticate:
        resp = await client.post(
            "/api/v1/autonomy/a2a/messages",
            json={**_envelope_body(), "extra": True},
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 422
    authenticate.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_route_forwards_the_signature_to_the_service(client):
    from src.server.models.autonomy import A2AEnvelope

    envelope = A2AEnvelope.model_validate(_envelope_body())
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.a2a_service.authenticate",
            new=AsyncMock(
                return_value={"ticket_id": _AGENT_TICKET, "user_id": USER_ID}
            ),
        ),
        patch(
            "src.server.app.autonomy.a2a_service.send_message",
            new=AsyncMock(return_value=_message_row(envelope)),
        ) as send,
    ):
        resp = await client.post(
            "/api/v1/autonomy/a2a/messages",
            json=_envelope_body(),
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "X-A2A-Signature": "signature-value",
            },
        )
    assert resp.status_code == 201
    assert resp.json()["intent"] == "inform"
    assert send.await_args.kwargs["signature"] == "signature-value"


@pytest.mark.asyncio
async def test_message_route_verifies_the_hmac_and_delivers_context(client):
    from src.server.models.autonomy import A2AEnvelope

    body = _envelope_body()
    envelope = A2AEnvelope.model_validate(body)
    signature = a2a_service.sign(_TOKEN, envelope)
    agent = {
        "agent_id": str(uuid4()),
        "ticket_id": _AGENT_TICKET,
        "user_id": USER_ID,
        "name": "",
        "revoked_at": None,
        "created_at": _NOW,
    }
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.services.autonomy.a2a.a2a_db.agent_for_token_hash",
            new=AsyncMock(return_value=agent),
        ),
        patch(
            "src.server.services.autonomy.a2a.ticket_db.get_ticket",
            new=AsyncMock(
                side_effect=[
                    _ticket(),
                    {**_ticket(), "ticket_id": _RECIPIENT, "worker_thread_id": None},
                ]
            ),
        ),
        patch(
            "src.server.services.autonomy.a2a.context_scope.ensure_scope",
            new=AsyncMock(return_value=_scope()),
        ),
        patch(
            "src.server.services.autonomy.a2a.a2a_db.deliver_message",
            new=AsyncMock(return_value=(_message_row(envelope), True)),
        ),
        patch(
            "src.server.services.autonomy.a2a.a2a_db.touch_agent",
            new=AsyncMock(),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/a2a/messages",
            json=body,
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "X-A2A-Signature": signature,
            },
        )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "delivered"


@pytest.mark.asyncio
async def test_missing_and_out_of_scope_recipients_share_one_detail_message(client):
    """The API detail is identical for a missing and an out-of-scope recipient."""
    from src.server.models.autonomy import A2AEnvelope

    agent = {
        "agent_id": str(uuid4()),
        "ticket_id": _AGENT_TICKET,
        "user_id": USER_ID,
        "name": "",
        "revoked_at": None,
        "created_at": _NOW,
    }

    async def post(recipient_lookup: dict | None):
        body = _envelope_body()
        envelope = A2AEnvelope.model_validate(body)
        signature = a2a_service.sign(_TOKEN, envelope)
        with (
            patch(
                "src.server.app.autonomy.features.user_feature_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "src.server.services.autonomy.a2a.a2a_db.agent_for_token_hash",
                new=AsyncMock(return_value=agent),
            ),
            patch(
                "src.server.services.autonomy.a2a.ticket_db.get_ticket",
                new=AsyncMock(side_effect=[_ticket(), recipient_lookup]),
            ),
            patch(
                "src.server.services.autonomy.a2a.context_scope.ensure_scope",
                new=AsyncMock(return_value=_scope()),
            ),
        ):
            return await client.post(
                "/api/v1/autonomy/a2a/messages",
                json=body,
                headers={
                    "Authorization": f"Bearer {_TOKEN}",
                    "X-A2A-Signature": signature,
                },
            )

    missing = await post(None)
    out_of_scope = await post(
        {**_ticket(), "ticket_id": _RECIPIENT, "context_scope_id": str(uuid4())}
    )
    assert missing.status_code == 403
    assert out_of_scope.status_code == 403
    assert missing.json()["detail"] == out_of_scope.json()["detail"]


@pytest.mark.asyncio
async def test_message_route_rejects_a_tampered_envelope(client):
    from src.server.models.autonomy import A2AEnvelope

    body = _envelope_body()
    envelope = A2AEnvelope.model_validate(body)
    signature = a2a_service.sign(_TOKEN, envelope)
    agent = {
        "agent_id": str(uuid4()),
        "ticket_id": _AGENT_TICKET,
        "user_id": USER_ID,
        "name": "",
        "revoked_at": None,
        "created_at": _NOW,
    }
    tampered = {**body, "body": "changed after signing"}
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.services.autonomy.a2a.a2a_db.agent_for_token_hash",
            new=AsyncMock(return_value=agent),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/a2a/messages",
            json=tampered,
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "X-A2A-Signature": signature,
            },
        )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_feature_disabled_is_indistinguishable_from_a_bad_token(client):
    """A disabled feature must not confirm a guessed token (401, same shape)."""
    from src.server.models.autonomy import A2AEnvelope

    body = _envelope_body()
    envelope = A2AEnvelope.model_validate(body)
    agent = {
        "agent_id": str(uuid4()),
        "ticket_id": _AGENT_TICKET,
        "user_id": USER_ID,
        "name": "",
        "revoked_at": None,
        "created_at": _NOW,
    }
    with (
        patch(
            "src.server.app.autonomy.a2a_service.authenticate",
            new=AsyncMock(return_value=agent),
        ),
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "src.server.app.autonomy.a2a_service.send_message",
            new=AsyncMock(),
        ) as send,
    ):
        resp = await client.post(
            "/api/v1/autonomy/a2a/messages",
            json=body,
            headers={
                "Authorization": f"Bearer {_TOKEN}",
                "X-A2A-Signature": a2a_service.sign(_TOKEN, envelope),
            },
        )
    assert resp.status_code == 401
    assert resp.json()["detail"] == {
        "code": "unauthenticated",
        "message": "invalid A2A token",
    }
    send.assert_not_awaited()

    # A well-formed token that was never issued must answer byte-for-byte the
    # same as the disabled feature: a distinct message would confirm the guess
    # is a real token shape worth refining.
    unknown = "A" * 43
    with patch(
        "src.server.services.autonomy.a2a.a2a_db.agent_for_token_hash",
        new=AsyncMock(return_value=None),
    ):
        bad = await client.post(
            "/api/v1/autonomy/a2a/messages",
            json=body,
            headers={"Authorization": f"Bearer {unknown}"},
        )
    assert bad.status_code == 401
    assert bad.json()["detail"] == resp.json()["detail"]


@pytest.mark.asyncio
async def test_a2a_token_failures_share_one_error_shape(client):
    """Every bearer failure, on both message routes, answers the same shape."""
    post = await client.post("/api/v1/autonomy/a2a/messages", json=_envelope_body())
    assert post.status_code == 401
    assert post.json()["detail"] == {
        "code": "unauthenticated",
        "message": "invalid A2A token",
    }

    get = await client.get("/api/v1/autonomy/a2a/messages")
    assert get.status_code == 401
    assert get.json()["detail"] == post.json()["detail"]

    with patch(
        "src.server.app.autonomy.a2a_service.authenticate",
        new=AsyncMock(
            side_effect=a2a_service.A2AError(
                "invalid A2A token", status_code=401, code="unauthenticated"
            )
        ),
    ):
        service_error = await client.get(
            "/api/v1/autonomy/a2a/messages",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert service_error.status_code == 401
    assert service_error.json()["detail"] == post.json()["detail"]


@pytest.mark.asyncio
async def test_list_messages_route_scopes_to_the_authenticated_ticket(client):
    agent = {"ticket_id": _AGENT_TICKET, "user_id": USER_ID}
    with (
        patch(
            "src.server.app.autonomy.a2a_service.authenticate",
            new=AsyncMock(return_value=agent),
        ),
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.a2a_service.list_messages",
            new=AsyncMock(return_value=[]),
        ) as messages,
    ):
        resp = await client.get(
            "/api/v1/autonomy/a2a/messages",
            headers={"Authorization": f"Bearer {_TOKEN}"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"messages": []}
    messages.assert_awaited_once_with(_AGENT_TICKET, limit=50)
