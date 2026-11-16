"""L5 ticket ergonomics: short-id resolution and the follow-up route.

Fake DB layers only: every database helper the routes call is patched.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.database.autonomy import tickets as tickets_db_module
from tests.conftest import create_test_app

USER_ID = "test-user-123"
TICKET_ID = "66666666-6666-4666-8666-666666666666"
OTHER_ID = "66666666-7777-4777-8777-777777777777"
DAEMON_ID = "55555555-5555-4555-8555-555555555555"


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        with patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ):
            yield c


def _ticket(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    row = {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "ws-1",
        "title": "Fix the save button",
        "scope": "Change the label",
        "status": "running",
        "mandate": {"strength": "explicit", "source": "manual", "cwd": "/repo/wt"},
        "origin_trigger_id": None,
        "origin_thread_key": "manual:test-user-123:abc",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "codex",
        "worker_model": "gpt-5",
        "worker_daemon_id": DAEMON_ID,
        "attempts": 1,
        "attention": None,
        "summary": "Renamed the button.",
        "error": "",
        "revision": 3,
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "ended_at": None,
        "closed_at": None,
    }
    row.update(overrides)
    return row


# -- short ids ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_is_routed_before_the_uuid_typed_ticket_route(client):
    find = AsyncMock(
        return_value=[{"ticket_id": TICKET_ID, "title": "Fix", "status": "running", "updated_at": None}]
    )
    with patch.object(tickets_db_module, "find_tickets_by_id_prefix", new=find):
        resp = await client.get("/api/v1/autonomy/tickets/resolve", params={"prefix": "6666"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ticket_id"] == TICKET_ID
    assert body["matches"][0]["status"] == "running"
    assert body["more"] is False
    find.assert_awaited_once_with(USER_ID, "6666", limit=11)


@pytest.mark.asyncio
async def test_resolve_lists_every_candidate_of_an_ambiguous_prefix(client):
    rows = [
        {"ticket_id": TICKET_ID, "title": "A", "status": "running", "updated_at": None},
        {"ticket_id": OTHER_ID, "title": "B", "status": "failed", "updated_at": None},
    ]
    with patch.object(tickets_db_module, "find_tickets_by_id_prefix", new=AsyncMock(return_value=rows)):
        resp = await client.get("/api/v1/autonomy/tickets/resolve", params={"prefix": "66666666-"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["ticket_id"] is None
    assert [m["ticket_id"] for m in body["matches"]] == [TICKET_ID, OTHER_ID]


@pytest.mark.asyncio
async def test_resolve_caps_the_candidates_and_flags_more(client):
    rows = [
        {"ticket_id": f"66666666-0000-4000-8000-{index:012d}", "title": "", "status": "queued", "updated_at": None}
        for index in range(11)
    ]
    with patch.object(tickets_db_module, "find_tickets_by_id_prefix", new=AsyncMock(return_value=rows)):
        resp = await client.get("/api/v1/autonomy/tickets/resolve", params={"prefix": "6"})
    body = resp.json()
    assert len(body["matches"]) == 10
    assert body["more"] is True
    assert body["ticket_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["abc%", "zz", "ab_c", "'; drop"])
async def test_resolve_refuses_a_prefix_that_is_not_an_id(client, prefix):
    find = AsyncMock()
    with patch.object(tickets_db_module, "find_tickets_by_id_prefix", new=find):
        resp = await client.get("/api/v1/autonomy/tickets/resolve", params={"prefix": prefix})
    assert resp.status_code == 400
    find.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_tickets_by_id_prefix_scopes_to_the_user_and_anchors_the_prefix(monkeypatch):
    executed: list[tuple[str, tuple]] = []

    class Cursor:
        async def execute(self, sql, params):
            executed.append((sql, params))

        async def fetchall(self):
            return [{"ticket_id": TICKET_ID, "title": "t", "status": "queued", "updated_at": None}]

    class Conn:
        def cursor(self, **_kwargs):
            @asynccontextmanager
            async def cm():
                yield Cursor()

            return cm()

    @asynccontextmanager
    async def fake_connection(conn=None):
        yield Conn()

    monkeypatch.setattr(tickets_db_module, "get_db_connection", fake_connection)
    rows = await tickets_db_module.find_tickets_by_id_prefix(USER_ID, "abc1", limit=4)
    assert rows == [{"ticket_id": TICKET_ID, "title": "t", "status": "queued", "updated_at": None}]
    sql, params = executed[0]
    assert "user_id = %s" in sql and "LIKE %s" in sql
    assert params == (USER_ID, "abc1%", 4)


# -- follow-ups --------------------------------------------------------------


@pytest.mark.asyncio
async def test_followup_on_a_running_daemon_ticket_is_stored_for_the_worker(client):
    """The reply route refuses this state; a follow-up is kept as an instruction."""
    add_input = AsyncMock(return_value={"input_id": "in-1"})
    with (
        patch.object(tickets_db_module, "get_ticket", new=AsyncMock(return_value=_ticket())),
        patch("src.server.services.autonomy.worker.ticket_db.add_input", new=add_input),
    ):
        reply = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/reply", json={"text": "also fix the icon"}
        )
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/followup", json={"text": "also fix the icon"}
        )
    assert reply.status_code == 409
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"input_id": "in-1", "status": "queued_for_daemon"}
    add_input.assert_awaited_once_with(
        ticket_id=TICKET_ID, trigger_id=None, kind="instruction", text="also fix the icon"
    )


@pytest.mark.asyncio
async def test_followup_on_a_waiting_daemon_ticket_is_a_reply(client):
    add_input = AsyncMock(return_value={"input_id": "in-2"})
    with (
        patch.object(
            tickets_db_module, "get_ticket", new=AsyncMock(return_value=_ticket(status="waiting"))
        ),
        patch.object(tickets_db_module, "add_input", new=add_input),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/followup", json={"text": "use main"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"input_id": "in-2", "status": "queued_for_daemon"}
    assert add_input.await_args.kwargs["kind"] == "answer"


@pytest.mark.asyncio
async def test_followup_on_a_failed_ticket_keeps_the_instruction_and_retries(client):
    failed = _ticket(status="failed", error="tests failed")
    queued = _ticket(status="queued")
    add_input = AsyncMock(return_value={"input_id": "in-3"})
    update = AsyncMock(return_value=queued)
    enqueue = AsyncMock()
    with (
        patch.object(tickets_db_module, "get_ticket", new=AsyncMock(return_value=failed)),
        patch("src.server.services.autonomy.worker.ticket_db.add_input", new=add_input),
        patch.object(tickets_db_module, "update_ticket", new=update),
        patch(
            "src.server.services.autonomy.ownership.revoked_mandate_reason",
            new=AsyncMock(return_value=None),
        ),
        patch("src.server.database.autonomy.effects.enqueue_effect", new=enqueue),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/followup", json={"text": "skip the flaky test"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "retry_queued"
    assert body["input_id"] == "in-3"
    assert body["ticket"]["status"] == "queued"
    assert add_input.await_args.kwargs["kind"] == "instruction"
    assert update.await_args.kwargs["status"] == "queued"
    enqueue.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "completed"},
        {"status": "cancelled"},
        {"status": "archived"},
        {"status": "waiting", "closed_at": datetime.now(timezone.utc)},
    ],
)
async def test_followup_on_an_ended_ticket_creates_a_new_followup_ticket(client, overrides):
    parent = _ticket(**overrides)
    created = _ticket(ticket_id=OTHER_ID, status="queued", title="Follow-up: Fix the save button")
    create = AsyncMock(return_value=created)
    add_input = AsyncMock()
    with (
        patch.object(tickets_db_module, "get_ticket", new=AsyncMock(return_value=parent)),
        patch.object(tickets_db_module, "add_input", new=add_input),
        patch("src.server.app.autonomy._workspace", new=AsyncMock(return_value={"workspace_id": "ws-1"})),
        patch(
            "src.server.app.autonomy._bind_daemon",
            new=AsyncMock(return_value=(DAEMON_ID, "codex")),
        ),
        patch("src.server.services.autonomy.manual.create_manual_task", new=create),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/followup", json={"text": "now add a tooltip"}
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "created_followup"
    assert body["parent_ticket_id"] == TICKET_ID
    assert body["ticket"]["ticket_id"] == OTHER_ID
    add_input.assert_not_awaited()
    kwargs = create.await_args.kwargs
    assert kwargs["workspace_id"] == "ws-1"
    assert kwargs["worker_daemon_id"] == DAEMON_ID
    assert kwargs["worker_provider"] == "codex"
    assert kwargs["worker_model"] == "gpt-5"
    assert kwargs["cwd"] == "/repo/wt"
    assert kwargs["title"] == "Follow-up: Fix the save button"
    assert TICKET_ID in kwargs["prompt"]
    assert "Renamed the button." in kwargs["prompt"]
    assert kwargs["prompt"].rstrip().endswith("now add a tooltip")


@pytest.mark.asyncio
async def test_followup_is_not_found_for_another_users_ticket(client):
    with patch.object(
        tickets_db_module, "get_ticket", new=AsyncMock(return_value=_ticket(user_id="someone-else"))
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/followup", json={"text": "x"}
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_followup_needs_text(client):
    resp = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/followup", json={"text": ""})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_refused_followup_retry_rolls_the_instruction_back(client):
    """A failed retry must not leave the follow-up behind for a later run."""
    failed = _ticket(status="failed", error="tests failed")
    add_input = AsyncMock(return_value={"input_id": "in-4"})
    discard = AsyncMock(return_value=True)
    with (
        patch.object(tickets_db_module, "get_ticket", new=AsyncMock(return_value=failed)),
        patch("src.server.services.autonomy.worker.ticket_db.add_input", new=add_input),
        # The Ticket changed under the retry: its guarded update matches nothing.
        patch.object(tickets_db_module, "update_ticket", new=AsyncMock(return_value=None)),
        patch(
            "src.server.services.autonomy.ownership.revoked_mandate_reason",
            new=AsyncMock(return_value=None),
        ),
        patch.object(tickets_db_module, "discard_undelivered_input", new=discard, create=True),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/followup", json={"text": "skip it"}
        )
    assert resp.status_code == 409
    add_input.assert_awaited_once()
    discard.assert_awaited_once_with("in-4")


@pytest.mark.asyncio
async def test_discard_undelivered_input_only_deletes_an_untaken_row(monkeypatch):
    executed: list[tuple[str, tuple]] = []

    class Cursor:
        rowcount = 1

        async def execute(self, sql, params):
            executed.append((sql, params))

    class Conn:
        def cursor(self, **_kwargs):
            @asynccontextmanager
            async def cm():
                yield Cursor()

            return cm()

    @asynccontextmanager
    async def fake_connection(conn=None):
        yield Conn()

    monkeypatch.setattr(tickets_db_module, "get_db_connection", fake_connection)
    assert await tickets_db_module.discard_undelivered_input("in-9") is True
    sql, params = executed[0]
    assert sql.startswith("DELETE FROM autonomy_ticket_inputs")
    assert "delivered_at IS NULL" in sql
    assert params == ("in-9",)
