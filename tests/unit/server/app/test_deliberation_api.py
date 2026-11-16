"""Deliberation HTTP API: feature gate, ownership, and the live state machine."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.services.deliberation.store import MemoryDeliberationStore
from src.server.services.deliberation.thread_port import MemoryThreadPort
from tests.conftest import create_test_app

USER_ID = "test-user-123"
TICKET_ID = "22222222-2222-4222-8222-222222222222"
FEATURE = "src.server.app.deliberation.features.user_feature_enabled"
GET_TICKET = "src.server.app.deliberation.tickets_db.get_ticket"
GET_STORE = "src.server.app.deliberation.get_store"
CONTINUE_DELIBERATION = "src.server.app.deliberation.continue_deliberation"
CANCEL_DELIBERATION = "src.server.app.deliberation.cancel_deliberation"


@pytest_asyncio.fixture
async def client():
    from src.server.app.deliberation import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        with patch(
            "src.server.app.deliberation._thread_port",
            return_value=MemoryThreadPort(),
        ):
            yield http


def _ticket(**overrides) -> dict:
    row = {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "toy",
        "scope": "add",
        "status": "queued",
    }
    row.update(overrides)
    return row


def _create_body() -> dict:
    return {
        "objective": "Add add(a,b)",
        "max_rounds": 3,
        "participants": [
            {"agent_key": "codex", "role": "participant", "adapter": "mock"},
            {"agent_key": "kimi", "role": "participant", "adapter": "mock"},
            {"agent_key": "grok", "role": "participant", "adapter": "mock"},
            {"agent_key": "planner", "role": "planner", "adapter": "mock"},
        ],
    }


def _assert_no_token(response) -> None:
    assert "access_token" not in response.text


def _store_is_empty(store: MemoryDeliberationStore) -> bool:
    pending: list[object] = [store]
    seen: set[int] = set()
    while pending:
        obj = pending.pop()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, (dict, list, tuple, set)):
            if len(obj) > 0:
                return False
            continue
        state = getattr(obj, "__dict__", None)
        if isinstance(state, dict):
            pending.extend(state.values())
    return True


def _kinds(transcript: list[dict]) -> list[str]:
    return [entry["kind"] for entry in transcript]


@pytest.mark.asyncio
async def test_create_is_hidden_when_feature_is_off(client) -> None:
    store = MemoryDeliberationStore()
    with (
        patch(FEATURE, new=AsyncMock(return_value=False)),
        patch(GET_TICKET, new=AsyncMock(return_value=_ticket())),
        patch(GET_STORE, return_value=store),
    ):
        response = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/deliberations",
            json=_create_body(),
        )
    assert response.status_code == 404
    _assert_no_token(response)


@pytest.mark.parametrize("ticket", [None, "foreign"], ids=["missing", "other-user"])
@pytest.mark.asyncio
async def test_missing_or_foreign_ticket_is_hidden(client, ticket) -> None:
    if ticket == "foreign":
        ticket = _ticket(user_id="someone-else")
    store = MemoryDeliberationStore()
    with (
        patch(FEATURE, new=AsyncMock(return_value=True)),
        patch(GET_TICKET, new=AsyncMock(return_value=ticket)),
        patch(GET_STORE, return_value=store) as get_store,
    ):
        response = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/deliberations",
            json=_create_body(),
        )
    assert response.status_code == 404
    _assert_no_token(response)
    assert get_store.call_count == 0 or _store_is_empty(store)


@pytest.mark.asyncio
async def test_consensus_start_returns_parallel_handoff(client) -> None:
    store = MemoryDeliberationStore()
    with (
        patch(FEATURE, new=AsyncMock(return_value=True)),
        patch(GET_TICKET, new=AsyncMock(return_value=_ticket())),
        patch(GET_STORE, return_value=store),
    ):
        created = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/deliberations",
            json=_create_body(),
        )
        assert created.status_code == 201
        created_body = created.json()
        assert created_body["status"] == "draft"
        deliberation_id = created_body["deliberation_id"]
        _assert_no_token(created)

        started = await client.post(
            f"/api/v1/autonomy/deliberations/{deliberation_id}/start",
            json={"script": "consensus"},
        )
        assert started.status_code == 200
        started_body = started.json()
        assert started_body["status"] == "consensus"
        assert started_body["handoff"]["parallel"] is True
        assert len(started_body["handoff"]["dispatched"]) == 2
        assert {step["step_key"] for step in started_body["handoff"]["dispatched"]} == {
            "spec",
            "impl",
        }
        _assert_no_token(started)

        transcript = await client.get(
            f"/api/v1/autonomy/deliberations/{deliberation_id}"
        )
        assert transcript.status_code == 200
        body = transcript.json()
        assert body["status"] == "consensus"
        kinds = _kinds(body["transcript"])
        assert "proposal" in kinds
        assert "synthesis" in kinds
        _assert_no_token(transcript)

        summary = await client.get(
            f"/api/v1/autonomy/deliberations/{deliberation_id}/summary"
        )
        assert summary.status_code == 200
        assert summary.json()["summary"]
        assert summary.json()["consensus_plan"]["steps"]
        _assert_no_token(summary)


@pytest.mark.asyncio
async def test_holdout_start_records_one_dissent(client) -> None:
    store = MemoryDeliberationStore()
    with (
        patch(FEATURE, new=AsyncMock(return_value=True)),
        patch(GET_TICKET, new=AsyncMock(return_value=_ticket())),
        patch(GET_STORE, return_value=store),
    ):
        created = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/deliberations",
            json=_create_body(),
        )
        assert created.status_code == 201
        deliberation_id = created.json()["deliberation_id"]
        _assert_no_token(created)

        started = await client.post(
            f"/api/v1/autonomy/deliberations/{deliberation_id}/start",
            json={"script": "holdout", "holdout_key": "grok"},
        )
    assert started.status_code == 200
    body = started.json()
    assert body["status"] == "decided"
    assert len(body["dissents"]) == 1
    assert body["dissents"][0]["agent_key"] == "grok"
    _assert_no_token(started)


@pytest.mark.asyncio
async def test_start_passes_provider_group_execution_config(client) -> None:
    store = MemoryDeliberationStore()
    grouped = AsyncMock(
        return_value={"deliberation_id": "d1", "status": "consensus"}
    )
    with (
        patch(FEATURE, new=AsyncMock(return_value=True)),
        patch(GET_TICKET, new=AsyncMock(return_value=_ticket())),
        patch(GET_STORE, return_value=store),
        patch("src.server.app.deliberation.start_deliberation", new=grouped),
    ):
        created = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/deliberations",
            json=_create_body(),
        )
        deliberation_id = created.json()["deliberation_id"]
        response = await client.post(
            f"/api/v1/autonomy/deliberations/{deliberation_id}/start",
            json={
                "provider_groups": [
                    {"group_key": "frontier", "agent_keys": ["codex", "kimi"]},
                    {"group_key": "review", "agent_keys": ["grok"]},
                ]
            },
        )

    assert response.status_code == 200
    assert grouped.await_args.kwargs["provider_groups"] == [
        {"group_key": "frontier", "agent_keys": ["codex", "kimi"]},
        {"group_key": "review", "agent_keys": ["grok"]},
    ]
    _assert_no_token(response)


@pytest.mark.asyncio
async def test_start_rejects_provider_groups_that_do_not_partition_participants(
    client,
) -> None:
    store = MemoryDeliberationStore()
    with (
        patch(FEATURE, new=AsyncMock(return_value=True)),
        patch(GET_TICKET, new=AsyncMock(return_value=_ticket())),
        patch(GET_STORE, return_value=store),
    ):
        created = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/deliberations",
            json=_create_body(),
        )
        deliberation_id = created.json()["deliberation_id"]
        response = await client.post(
            f"/api/v1/autonomy/deliberations/{deliberation_id}/start",
            json={
                "provider_groups": [
                    {"group_key": "incomplete", "agent_keys": ["codex", "kimi"]}
                ]
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_provider_groups"
    assert (await store.load(deliberation_id))["status"] == "draft"
    _assert_no_token(response)


@pytest.mark.asyncio
async def test_continue_and_cancel_use_owned_deliberation(client) -> None:
    store = MemoryDeliberationStore()
    continued = AsyncMock(
        return_value={
            "deliberation_id": "child",
            "parent_deliberation_id": "parent",
        }
    )
    cancelled = AsyncMock(
        return_value={"deliberation_id": "parent", "status": "cancelled"}
    )
    with (
        patch(FEATURE, new=AsyncMock(return_value=True)),
        patch(GET_TICKET, new=AsyncMock(return_value=_ticket())),
        patch(GET_STORE, return_value=store),
        patch(CONTINUE_DELIBERATION, new=continued),
        patch(CANCEL_DELIBERATION, new=cancelled),
    ):
        created = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/deliberations",
            json=_create_body(),
        )
        deliberation_id = created.json()["deliberation_id"]
        continued_response = await client.post(
            f"/api/v1/autonomy/deliberations/{deliberation_id}/continue",
            json={
                "objective": "Continue with the edge cases",
                "additional_context": "Preserve API compatibility",
                "max_rounds": 2,
            },
        )
        cancelled_response = await client.post(
            f"/api/v1/autonomy/deliberations/{deliberation_id}/cancel"
        )

    assert continued_response.status_code == 201
    assert continued.await_args.args == (store, deliberation_id)
    assert {
        key: value
        for key, value in continued.await_args.kwargs.items()
        if key != "thread_port"
    } == {
        "objective": "Continue with the edge cases",
        "additional_context": "Preserve API compatibility",
        "max_rounds": 2,
    }
    assert isinstance(continued.await_args.kwargs["thread_port"], MemoryThreadPort)
    assert continued_response.json()["parent_deliberation_id"] == "parent"
    assert cancelled_response.status_code == 200
    cancelled.assert_awaited_once_with(store, deliberation_id)
    assert cancelled_response.json()["status"] == "cancelled"
    _assert_no_token(continued_response)
    _assert_no_token(cancelled_response)


@pytest.mark.parametrize("action", ["continue", "cancel", "summary"])
@pytest.mark.asyncio
async def test_continue_and_cancel_hide_foreign_deliberation(client, action) -> None:
    store = MemoryDeliberationStore()
    await store.create(
        {
            "deliberation_id": "foreign-deliberation",
            "ticket_id": TICKET_ID,
            "user_id": "someone-else",
            "objective": "foreign",
            "status": "consensus",
            "round_index": 1,
            "max_rounds": 1,
            "planner_agent_key": "planner",
            "thread_key": "thread-foreign",
            "consensus_plan": None,
            "decision_reason": "",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    )
    await store.save_participant(
        {
            "participant_id": "p1",
            "deliberation_id": "foreign-deliberation",
            "agent_key": "planner",
            "role": "planner",
            "adapter": "mock",
            "display_name": "",
            "daemon_id": None,
        }
    )
    with (
        patch(FEATURE, new=AsyncMock(return_value=True)),
        patch(GET_STORE, return_value=store),
        patch(CONTINUE_DELIBERATION, new=AsyncMock()) as continue_call,
        patch(CANCEL_DELIBERATION, new=AsyncMock()) as cancel_call,
    ):
        if action == "summary":
            response = await client.get(
                f"/api/v1/autonomy/deliberations/foreign-deliberation/{action}"
            )
        else:
            response = await client.post(
                f"/api/v1/autonomy/deliberations/foreign-deliberation/{action}",
                json={"objective": "continue"} if action == "continue" else None,
            )

    assert response.status_code == 404
    continue_call.assert_not_awaited()
    cancel_call.assert_not_awaited()
    _assert_no_token(response)


@pytest.mark.parametrize("action", ["continue", "cancel", "summary"])
@pytest.mark.asyncio
async def test_new_deliberation_routes_are_hidden_when_feature_is_off(client, action) -> None:
    with (
        patch(FEATURE, new=AsyncMock(return_value=False)),
        patch(GET_STORE) as get_store,
    ):
        if action == "summary":
            response = await client.get(
                f"/api/v1/autonomy/deliberations/d1/{action}"
            )
        else:
            response = await client.post(
                f"/api/v1/autonomy/deliberations/d1/{action}",
                json={"objective": "continue"} if action == "continue" else None,
            )

    assert response.status_code == 404
    get_store.assert_not_called()
    _assert_no_token(response)
