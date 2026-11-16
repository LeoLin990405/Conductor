"""Autonomy Source API: feature gating, ownership, and secret-free views."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER_ID = "test-user-123"


def test_autonomy_worker_provider_filter_excludes_research_only_manus(monkeypatch):
    from src.server.app import autonomy

    monkeypatch.setattr(
        "src.llms.native_cli.registry.list_adapters",
        lambda: [{"provider": "codex"}, {"provider": "manus"}],
    )
    assert autonomy._native_cli_providers() == {"codex"}


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _source() -> dict:
    return {
        "source_id": "11111111-1111-4111-8111-111111111111",
        "user_id": USER_ID,
        "kind": "github",
        "config": {
            "repository": "acme/widgets",
            "secret_name": "GITHUB_TOKEN",
            "workspace_id": "w1",
            "allowed_logins": ["alice"],
            "poll_seconds": 60,
        },
        "state": {"agent_login": "conductor-bot", "last_error": ""},
        "enabled": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


@pytest.mark.asyncio
async def test_source_list_is_hidden_when_feature_is_off(client):
    with patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=False),
    ):
        resp = await client.get("/api/v1/autonomy/sources")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_source_list_returns_config_but_never_a_token(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.source_db.list_sources",
            new=AsyncMock(return_value=[_source()]),
        ),
    ):
        resp = await client.get("/api/v1/autonomy/sources")
    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"][0]["config"]["secret_name"] == "GITHUB_TOKEN"
    assert "access_token" not in str(body)
    assert body["sources"][0]["state"]["agent_login"] == "conductor-bot"


def _ticket() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "ticket_id": "22222222-2222-4222-8222-222222222222",
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Fix the save button",
        "scope": "Change the label to Save",
        "status": "queued",
        "mandate": {"strength": "explicit", "source_id": "s1"},
        "origin_trigger_id": "trg_1",
        "origin_thread_key": "github:acme/widgets#12",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "attempts": 0,
        "attention": None,
        "summary": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }


@pytest.mark.asyncio
async def test_ticket_list_and_ownership(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_tickets",
            new=AsyncMock(return_value=[_ticket()]),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket()),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_inputs",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_reports",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.app.autonomy.line_db.list_ticket_events",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.database.autonomy.stops.list_stops",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.database.autonomy.stops.get_stop",
            new=AsyncMock(return_value=None),
        ),
    ):
        listing = await client.get("/api/v1/autonomy/tickets")
        assert listing.status_code == 200
        listed = listing.json()["tickets"][0]
        assert listed["title"] == "Fix the save button"
        assert listed["source_id"] == "s1"
        assert listed["trigger_id"] == "trg_1"

        detail = await client.get(
            "/api/v1/autonomy/tickets/22222222-2222-4222-8222-222222222222"
        )
        assert detail.status_code == 200
        body = detail.json()
        assert body["origin_thread_key"] == "github:acme/widgets#12"
        assert body["source_id"] == "s1"
        assert body["trigger_id"] == "trg_1"


@pytest.mark.asyncio
async def test_reply_refuses_a_ticket_closed_by_task_interaction(client):
    closed = {**_ticket(), "closed_at": datetime.now(timezone.utc)}
    lookup = AsyncMock(return_value=closed)
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch("src.server.app.autonomy.tickets_db.get_ticket", new=lookup),
    ):
        resp = await client.post(
            "/api/v1/autonomy/tickets/22222222-2222-4222-8222-222222222222/reply",
            json={"text": "continue"},
        )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Ticket ended"
    lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_ticket_detail_projects_ids_from_the_row(client):
    from src.server.models.autonomy import TicketDetail

    derived = _ticket()
    assert "source_id" not in derived
    assert "trigger_id" not in derived
    payload = {**derived, "inputs": [], "reports": []}
    TicketDetail.model_validate(payload)
    assert "source_id" not in payload
    assert "trigger_id" not in payload
    assert payload["mandate"] == {"strength": "explicit", "source_id": "s1"}
    assert payload["origin_trigger_id"] == "trg_1"

    bare = {**_ticket(), "mandate": {"strength": "explicit"}, "origin_trigger_id": None}
    kept = {
        **_ticket(),
        "source_id": "kept-source",
        "trigger_id": "kept-trigger",
        "origin_trigger_id": "trg_1",
        "mandate": {"source_id": "s1", "trigger_id": "mandate-trigger"},
    }
    from_mandate = {
        **_ticket(),
        "origin_trigger_id": "",
        "mandate": {"source_id": "s1", "trigger_id": "mandate-trigger"},
    }
    from_mandate.pop("source_id", None)
    from_mandate.pop("trigger_id", None)
    rows = [derived, bare, kept, from_mandate]

    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(side_effect=rows),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_inputs",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_reports",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.app.autonomy.line_db.list_ticket_events",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "src.server.database.autonomy.stops.get_stop",
            new=AsyncMock(return_value=None),
        ),
    ):
        responses = []
        for _row in rows:
            resp = await client.get(
                "/api/v1/autonomy/tickets/22222222-2222-4222-8222-222222222222"
            )
            assert resp.status_code == 200
            responses.append(resp.json())

    assert "source_id" not in derived
    assert "trigger_id" not in derived
    assert responses[0]["source_id"] == "s1"
    assert responses[0]["trigger_id"] == "trg_1"
    assert responses[1]["source_id"] == ""
    assert responses[1]["trigger_id"] == ""
    assert responses[2]["source_id"] == "kept-source"
    assert responses[2]["trigger_id"] == "kept-trigger"
    assert responses[3]["source_id"] == "s1"
    assert responses[3]["trigger_id"] == "mandate-trigger"


@pytest.mark.asyncio
async def test_manual_task_enters_the_same_ticket_queue(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy._workspace",
            new=AsyncMock(return_value={"workspace_id": "w1"}),
        ),
        patch(
            "src.server.services.autonomy.manual.create_manual_task",
            new=AsyncMock(return_value=_ticket()),
        ) as create_manual,
    ):
        resp = await client.post(
            "/api/v1/autonomy/tasks",
            json={"prompt": "Write the note", "title": "Note", "workspace_id": "w1", "cwd": "/worktrees/repo"},
        )
    assert resp.status_code == 201
    assert resp.json()["title"] == "Fix the save button"
    assert create_manual.await_args.kwargs["cwd"] == "/worktrees/repo"


@pytest.mark.asyncio
async def test_delete_source_removes_only_an_owned_source(client):
    source_id = _source()["source_id"]
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.source_db.delete_source",
            new=AsyncMock(return_value=True),
        ) as delete,
    ):
        resp = await client.delete(f"/api/v1/autonomy/sources/{source_id}")
    assert resp.status_code == 204
    delete.assert_awaited_once_with(USER_ID, source_id)

    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.source_db.delete_source",
            new=AsyncMock(return_value=False),
        ),
    ):
        resp = await client.delete(f"/api/v1/autonomy/sources/{source_id}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_source_update_preserves_credential_allowlist_when_omitted(client):
    """A UI rebuild that omits credential_allowlist must not drop the policy."""
    allowlist = {"hosts": ["api.github.com"], "methods": ["GET"], "path_prefixes": ["/user"]}
    existing = _source()
    existing["config"] = {
        **existing["config"],
        "credential_allowlist": allowlist,
    }
    updated = {
        **existing,
        "config": {
            **existing["config"],
            "allowed_logins": ["alice", "bob"],
            "credential_allowlist": allowlist,
        },
    }
    update = AsyncMock(return_value=updated)
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.source_db.get_source",
            new=AsyncMock(return_value=existing),
        ),
        patch(
            "src.server.app.autonomy._workspace",
            new=AsyncMock(return_value={"workspace_id": "w1"}),
        ),
        patch(
            "src.server.app.autonomy.reveal_user_secret",
            new=AsyncMock(return_value="token"),
        ),
        patch(
            "src.server.app.autonomy._check_source",
            new=AsyncMock(
                return_value={
                    "agent_login": "conductor-bot",
                    "repository": "acme/widgets",
                }
            ),
        ),
        patch("src.server.app.autonomy.source_db.update_source", update),
    ):
        resp = await client.patch(
            f"/api/v1/autonomy/sources/{existing['source_id']}",
            json={
                "config": {
                    "repository": "acme/widgets",
                    "secret_name": "GITHUB_TOKEN",
                    "workspace_id": "w1",
                    "allowed_logins": ["alice", "bob"],
                    "poll_seconds": 60,
                }
            },
        )
    assert resp.status_code == 200
    saved = update.await_args.kwargs["config"]
    assert saved["credential_allowlist"] == allowlist
    assert saved["allowed_logins"] == ["alice", "bob"]


@pytest.mark.asyncio
async def test_source_update_preserves_require_approval_when_omitted(client):
    existing = _source()
    existing["config"] = {**existing["config"], "require_approval": True}
    updated = {
        **existing,
        "config": {
            **existing["config"],
            "allowed_logins": ["alice", "bob"],
        },
    }
    update = AsyncMock(return_value=updated)
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.source_db.get_source",
            new=AsyncMock(return_value=existing),
        ),
        patch(
            "src.server.app.autonomy._workspace",
            new=AsyncMock(return_value={"workspace_id": "w1"}),
        ),
        patch(
            "src.server.app.autonomy.reveal_user_secret",
            new=AsyncMock(return_value="token"),
        ),
        patch(
            "src.server.app.autonomy._check_source",
            new=AsyncMock(
                return_value={
                    "agent_login": "conductor-bot",
                    "repository": "acme/widgets",
                }
            ),
        ),
        patch("src.server.app.autonomy.source_db.update_source", update),
    ):
        resp = await client.patch(
            f"/api/v1/autonomy/sources/{existing['source_id']}",
            json={
                "config": {
                    "repository": "acme/widgets",
                    "secret_name": "GITHUB_TOKEN",
                    "workspace_id": "w1",
                    "allowed_logins": ["alice", "bob"],
                    "poll_seconds": 60,
                }
            },
        )
    assert resp.status_code == 200
    assert update.await_args.kwargs["config"]["require_approval"] is True


@pytest.mark.asyncio
async def test_source_update_can_replace_credential_allowlist_explicitly(client):
    existing = _source()
    existing["config"] = {
        **existing["config"],
        "credential_allowlist": {
            "hosts": ["api.github.com"],
            "methods": ["GET"],
            "path_prefixes": ["/user"],
        },
    }
    replacement = {
        "hosts": ["api.github.com"],
        "methods": ["GET", "POST"],
        "path_prefixes": ["/repos/acme/widgets"],
    }
    updated = {
        **existing,
        "config": {**existing["config"], "credential_allowlist": replacement},
    }
    update = AsyncMock(return_value=updated)
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.source_db.get_source",
            new=AsyncMock(return_value=existing),
        ),
        patch(
            "src.server.app.autonomy._workspace",
            new=AsyncMock(return_value={"workspace_id": "w1"}),
        ),
        patch(
            "src.server.app.autonomy.reveal_user_secret",
            new=AsyncMock(return_value="token"),
        ),
        patch(
            "src.server.app.autonomy._check_source",
            new=AsyncMock(
                return_value={
                    "agent_login": "conductor-bot",
                    "repository": "acme/widgets",
                }
            ),
        ),
        patch("src.server.app.autonomy.source_db.update_source", update),
    ):
        resp = await client.patch(
            f"/api/v1/autonomy/sources/{existing['source_id']}",
            json={
                "config": {
                    "repository": "acme/widgets",
                    "secret_name": "GITHUB_TOKEN",
                    "workspace_id": "w1",
                    "allowed_logins": ["alice"],
                    "poll_seconds": 60,
                    "credential_allowlist": replacement,
                }
            },
        )
    assert resp.status_code == 200
    assert update.await_args.kwargs["config"]["credential_allowlist"] == replacement


@pytest.mark.asyncio
async def test_run_schedule_returns_the_created_ticket(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.services.autonomy.schedules.run_schedule_now",
            new=AsyncMock(return_value=_ticket()),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/schedules/11111111-1111-4111-8111-111111111111/run"
        )
    assert resp.status_code == 200
    assert resp.json()["ticket_id"] == _ticket()["ticket_id"]


@pytest.mark.asyncio
async def test_resuming_a_schedule_recomputes_its_next_run(client):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    future = now.replace(year=now.year + 1)
    current = {
        "schedule_id": "11111111-1111-4111-8111-111111111111",
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Daily",
        "prompt": "Summarize",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "worker_provider": "",
        "worker_model": "",
        "enabled": False,
        "next_run_at": now,
        "last_run_at": None,
        "last_error": "",
        "created_at": now,
        "updated_at": now,
    }
    update = AsyncMock()
    update.return_value = {**current, "enabled": True, "next_run_at": future}
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.database.autonomy.schedules.get_schedule",
            new=AsyncMock(return_value=current),
        ),
        patch(
            "src.server.database.autonomy.schedules.update_schedule",
            new=update,
        ),
        patch(
            "src.server.app.autonomy._schedule_next",
            new=AsyncMock(return_value=future),
        ),
    ):
        resp = await client.patch(
            "/api/v1/autonomy/schedules/11111111-1111-4111-8111-111111111111",
            json={"enabled": True},
        )
    assert resp.status_code == 200
    assert update.call_args.kwargs["next_run_at"] == future
    assert update.call_args.kwargs["enabled"] is True


@pytest.mark.asyncio
async def test_a_schedule_can_move_to_another_workspace(client):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    current = {
        "schedule_id": "11111111-1111-4111-8111-111111111111",
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Daily",
        "prompt": "Summarize",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "worker_provider": "",
        "worker_model": "",
        "enabled": True,
        "next_run_at": now,
        "last_run_at": None,
        "last_error": "",
        "created_at": now,
        "updated_at": now,
    }
    update = AsyncMock(return_value={**current, "workspace_id": "w2"})
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.database.autonomy.schedules.get_schedule",
            new=AsyncMock(return_value=current),
        ),
        patch(
            "src.server.database.workspace.get_workspace",
            new=AsyncMock(return_value={"workspace_id": "w2", "user_id": USER_ID}),
        ),
        patch(
            "src.server.database.autonomy.schedules.update_schedule",
            new=update,
        ),
    ):
        resp = await client.patch(
            "/api/v1/autonomy/schedules/11111111-1111-4111-8111-111111111111",
            json={"workspace_id": "w2"},
        )
    assert resp.status_code == 200
    assert update.call_args.kwargs["workspace_id"] == "w2"


@pytest.mark.asyncio
async def test_poll_now_queues_an_immediate_source_poll(client):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    row = {
        "source_id": "11111111-1111-4111-8111-111111111111",
        "user_id": USER_ID,
        "kind": "github",
        "config": {"repository": "acme/widgets"},
        "state": {},
        "enabled": True,
        "created_at": now,
        "updated_at": now,
    }
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.database.autonomy.sources.get_source",
            new=AsyncMock(return_value=row),
        ),
        patch(
            "src.server.services.autonomy.poll.poll_source_now",
            new=AsyncMock(return_value=True),
        ) as queue,
    ):
        resp = await client.post(
            "/api/v1/autonomy/sources/11111111-1111-4111-8111-111111111111/poll"
        )
    assert resp.status_code == 200
    assert resp.json()["source_id"] == row["source_id"]
    queue.assert_awaited_once_with(USER_ID, row["source_id"])


@pytest.mark.asyncio
async def test_poll_now_refuses_a_disabled_source(client):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    row = {
        "source_id": "11111111-1111-4111-8111-111111111111",
        "user_id": USER_ID,
        "kind": "github",
        "config": {"repository": "acme/widgets"},
        "state": {},
        "enabled": False,
        "created_at": now,
        "updated_at": now,
    }
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.database.autonomy.sources.get_source",
            new=AsyncMock(return_value=row),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/sources/11111111-1111-4111-8111-111111111111/poll"
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_ticket_list_passes_source_schedule_and_failed_filters(client):
    listing = AsyncMock(return_value=[])
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_tickets",
            new=listing,
        ),
    ):
        resp = await client.get(
            "/api/v1/autonomy/tickets?source_id=src-1&schedule_id=s-1&failed=true&attention=true&q=widget&sort=title&limit=200&offset=50"
        )
    assert resp.status_code == 200
    assert listing.call_args.kwargs["statuses"] == ["failed", "blocked"]
    assert listing.call_args.kwargs["source_id"] == "src-1"
    assert listing.call_args.kwargs["schedule_id"] == "s-1"
    assert listing.call_args.kwargs["attention"] is True
    assert listing.call_args.kwargs["q"] == "widget"
    assert listing.call_args.kwargs["sort"] == "title"
    assert listing.call_args.kwargs["limit"] == 200
    assert listing.call_args.kwargs["offset"] == 50


@pytest.mark.asyncio
async def test_overview_exposes_failed_and_blocked_counts(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.counts",
            new=AsyncMock(
                return_value={
                    "attention": 1,
                    "running": 2,
                    "queued": 3,
                    "completed": 4,
                    "failed": 5,
                    "blocked": 6,
                }
            ),
        ),
        patch(
            "src.server.app.autonomy.source_db.list_sources",
            new=AsyncMock(return_value=[{"enabled": True}, {"enabled": False}]),
        ),
        patch(
            "src.config.settings.get_autonomy_router_model",
            return_value="deepseek-flash-account",
        ),
    ):
        resp = await client.get("/api/v1/autonomy/overview")
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"]["failed"] == 5
    assert body["counts"]["blocked"] == 6
    assert body["sources"] == 2
    assert body["enabled_sources"] == 1
    # The operator CLI shows this in its status bar.
    assert body["router_model"] == "deepseek-flash-account"


@pytest.mark.asyncio
async def test_policy_can_switch_between_explicit_observe_and_auto(client):
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.services.autonomy.policy.current_policy",
            new=AsyncMock(
                return_value={
                    "accept_mode": "explicit",
                    "version": "v1",
                    "rules": [],
                }
            ),
        ),
        patch(
            "src.server.services.autonomy.policy.set_accept_mode",
            new=AsyncMock(
                return_value={
                    "accept_mode": "observe",
                    "version": "v2",
                    "rules": [],
                }
            ),
        ) as set_mode,
        patch(
            "src.server.services.autonomy.policy.policy_changed",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "src.server.services.autonomy.policy.notify_policy_change",
            new=AsyncMock(return_value=1),
        ),
    ):
        got = await client.get("/api/v1/autonomy/policy")
        changed = await client.patch(
            "/api/v1/autonomy/policy", json={"accept_mode": "observe"}
        )
    assert got.status_code == 200
    assert got.json()["accept_mode"] == "explicit"
    assert changed.status_code == 200
    assert changed.json()["accept_mode"] == "observe"
    set_mode.assert_awaited_once_with(USER_ID, "observe")


@pytest.mark.asyncio
async def test_policy_rejects_an_unknown_mode(client):
    with patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=True),
    ):
        resp = await client.patch(
            "/api/v1/autonomy/policy", json={"accept_mode": "sometimes"}
        )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_retry_clears_the_speculative_flag_before_launching(client):
    now = datetime.now(timezone.utc)
    row = {
        "ticket_id": "22222222-2222-4222-8222-222222222222",
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Observe",
        "scope": "Review",
        "status": "waiting",
        "mandate": {
            "speculative": True,
            "source_id": "s1",
            "speculative_outcome": {
                "outcome": "accepted",
                "evidence": {
                    "worker_state": "waiting",
                    "worker_summary": "PLAN: review the save path",
                    "report_id": "r-1",
                },
            },
        },
        "origin_thread_key": "thread-1",
        "attempts": 1,
        "summary": "PLAN: review",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }
    updated = {
        **row,
        "status": "queued",
        "mandate": {"speculative": False},
        "attention": None,
    }
    update = AsyncMock(return_value=updated)
    enqueue = AsyncMock(return_value=True)
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=row),
        ),
        patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        patch("src.server.database.autonomy.effects.enqueue_effect", enqueue),
    ):
        resp = await client.post(
            "/api/v1/autonomy/tickets/22222222-2222-4222-8222-222222222222/retry"
        )
    assert resp.status_code == 200
    assert update.call_args.kwargs["status"] == "queued"
    mandate = update.call_args.kwargs["mandate"]
    assert mandate["speculative"] is False
    assert mandate["speculative_outcome"]["outcome"] == "approved"
    assert mandate["speculative_outcome"]["evidence"]["actor"] == "user"
    assert mandate["speculative_outcome"]["evidence"]["previous_outcome"] == "accepted"
    assert "PLAN: review the save path" in (
        mandate["speculative_outcome"]["evidence"]["worker_summary"]
    )
    enqueue.assert_awaited_once()
    body = resp.json()
    assert body["speculative_outcome"] is None


@pytest.mark.asyncio
async def test_discard_keeps_speculative_evidence_and_stays_quiet(client):
    now = datetime.now(timezone.utc)
    row = {
        "ticket_id": "22222222-2222-4222-8222-222222222222",
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Observe",
        "scope": "Review",
        "status": "waiting",
        "mandate": {
            "speculative": True,
            "source_id": "s1",
            "speculative_outcome": {
                "outcome": "accepted",
                "evidence": {
                    "worker_state": "waiting",
                    "worker_summary": "PLAN: review the save path",
                    "report_id": "r-1",
                },
            },
        },
        "origin_thread_key": "thread-1",
        "attempts": 1,
        "summary": "PLAN: review",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }
    finish = AsyncMock(side_effect=lambda *args, **kwargs: {**row, **kwargs, "status": "completed"})
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=row),
        ),
        patch("src.server.app.autonomy.tickets_db.complete_discarded_plan", finish),
    ):
        resp = await client.post(
            "/api/v1/autonomy/tickets/22222222-2222-4222-8222-222222222222/discard"
        )
    assert resp.status_code == 200
    assert finish.await_args.args[0] == "22222222-2222-4222-8222-222222222222"
    assert finish.await_args.kwargs["expected_status"] == "waiting"
    assert finish.await_args.kwargs["summary"] == ""
    mandate = finish.await_args.kwargs["mandate"]
    assert mandate["speculative"] is True
    recorded = mandate["speculative_outcome"]
    assert recorded["outcome"] == "discarded"
    assert recorded["evidence"]["actor"] == "user"
    assert recorded["evidence"]["previous_outcome"] == "accepted"
    assert recorded["evidence"]["worker_summary"] == "PLAN: review the save path"
    assert recorded["evidence"]["report_id"] == "r-1"
    body = resp.json()
    assert body["status"] == "completed"
    assert body["speculative_outcome"]["outcome"] == "discarded"


@pytest.mark.asyncio
async def test_discard_refuses_a_live_or_ordinary_ticket(client):
    now = datetime.now(timezone.utc)
    base = {
        "ticket_id": "22222222-2222-4222-8222-222222222222",
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Observe",
        "scope": "Review",
        "origin_thread_key": "thread-1",
        "attempts": 1,
        "summary": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }
    cases = [
        ({**base, "status": "running", "mandate": {"speculative": True}}, "still in progress"),
        ({**base, "status": "waiting", "mandate": {"speculative": False}}, "not a speculative plan"),
        ({**base, "status": "completed", "mandate": {"speculative": True}}, "Ticket ended"),
    ]
    for row, detail in cases:
        update = AsyncMock()
        with (
            patch(
                "src.server.app.autonomy.features.user_feature_enabled",
                new=AsyncMock(return_value=True),
            ),
            patch(
                "src.server.app.autonomy.tickets_db.get_ticket",
                new=AsyncMock(return_value=row),
            ),
            patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        ):
            resp = await client.post(
                "/api/v1/autonomy/tickets/22222222-2222-4222-8222-222222222222/discard"
            )
        assert resp.status_code == 409
        assert detail in resp.json()["detail"]
        update.assert_not_awaited()


@pytest.mark.asyncio
async def test_metrics_are_scoped_to_the_current_user(client):
    data = {
        "pending_triggers": 3,
        "pending_threads": 2,
        "active_evals": 1,
        "pending_effects": 4,
        "claimed_effects": 1,
        "queued_tickets": 2,
        "running_tickets": 1,
        "oldest_pending_effect_seconds": 12,
        "max_workers": 4,
    }
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.database.autonomy.metrics.pipeline_metrics",
            new=AsyncMock(return_value=data),
        ) as metrics,
    ):
        resp = await client.get("/api/v1/autonomy/metrics")
    assert resp.status_code == 200
    assert resp.json() == data
    metrics.assert_awaited_once_with(USER_ID)


@pytest.mark.asyncio
async def test_a_feishu_source_is_validated_without_a_vault_secret(client):
    now = datetime.now(timezone.utc)
    row = {
        "source_id": "33333333-3333-4333-8333-333333333333",
        "user_id": USER_ID,
        "kind": "feishu",
        "config": {
            "chat_id": "oc_1",
            "workspace_id": "w1",
            "allowed_logins": ["ou_alice"],
            "poll_seconds": 60,
            "agent_open_id": "ou_bot",
        },
        "state": {"agent_login": "lark-cli", "repository": "oc_1", "last_error": ""},
        "enabled": True,
        "created_at": now,
        "updated_at": now,
    }
    create = AsyncMock(return_value=row)
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy._workspace",
            new=AsyncMock(return_value={"workspace_id": "w1"}),
        ),
        patch(
            "src.server.app.autonomy.check_feishu_source",
            new=AsyncMock(
                return_value={
                    "agent_login": "lark-cli",
                    "repository": "oc_1",
                    "can_push": True,
                }
            ),
        ),
        patch("src.server.app.autonomy.source_db.create_source", create),
        patch(
            "src.server.app.autonomy.source_db.update_source",
            new=AsyncMock(return_value=row),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/sources",
            json={
                "kind": "feishu",
                "config": {
                    "chat_id": "oc_1",
                    "workspace_id": "w1",
                    "allowed_logins": ["ou_alice"],
                    "poll_seconds": 60,
                    "agent_open_id": "ou_bot",
                },
            },
        )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "feishu"
    create.assert_awaited_once()
    assert create.await_args.kwargs["kind"] == "feishu"
    assert create.await_args.kwargs["config"]["chat_id"] == "oc_1"


@pytest.mark.asyncio
async def test_source_can_bind_a_registered_daemon_and_infer_its_provider(client):
    source_id = "33333333-3333-4333-8333-333333333333"
    daemon_id = "55555555-5555-4555-8555-555555555555"
    create = AsyncMock(return_value={**_source(), "source_id": source_id})
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy._workspace",
            new=AsyncMock(return_value={"workspace_id": "w1"}),
        ),
        patch(
            "src.server.database.autonomy.daemons.get_daemon",
            new=AsyncMock(return_value={"daemon_id": daemon_id, "provider": "codex"}),
        ) as get_daemon,
        patch(
            "src.server.app.autonomy.check_feishu_source",
            new=AsyncMock(
                return_value={"agent_login": "lark-cli", "repository": "oc_1"}
            ),
        ),
        patch("src.server.app.autonomy.source_db.create_source", create),
        patch(
            "src.server.app.autonomy.source_db.update_source",
            new=AsyncMock(return_value={**_source(), "source_id": source_id}),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/sources",
            json={
                "kind": "feishu",
                "config": {
                    "chat_id": "oc_1",
                    "workspace_id": "w1",
                    "allowed_logins": ["ou_alice"],
                    "worker_daemon_id": daemon_id,
                },
            },
        )
    assert resp.status_code == 201
    get_daemon.assert_awaited_once_with(USER_ID, daemon_id)
    assert create.await_args.kwargs["config"]["worker_daemon_id"] == daemon_id
    assert create.await_args.kwargs["config"]["worker_provider"] == "codex"


@pytest.mark.asyncio
async def test_schedule_rejects_a_provider_that_does_not_match_its_daemon(client):
    daemon_id = "55555555-5555-4555-8555-555555555555"
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy._workspace",
            new=AsyncMock(return_value={"workspace_id": "w1"}),
        ),
        patch(
            "src.server.database.autonomy.daemons.get_daemon",
            new=AsyncMock(return_value={"daemon_id": daemon_id, "provider": "codex"}),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/schedules",
            json={
                "prompt": "Review updates",
                "workspace_id": "w1",
                "cron_expression": "0 9 * * *",
                "worker_provider": "kimi",
                "worker_daemon_id": daemon_id,
            },
        )
    assert resp.status_code == 400
    assert "match" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_schedule_patch_null_clears_daemon_binding(client):
    now = datetime.now(timezone.utc)
    schedule_id = "11111111-1111-4111-8111-111111111111"
    current = {
        "schedule_id": schedule_id,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Daily",
        "prompt": "Review",
        "cron_expression": "0 9 * * *",
        "timezone": "UTC",
        "worker_provider": "codex",
        "worker_model": "",
        "worker_daemon_id": "55555555-5555-4555-8555-555555555555",
        "enabled": True,
        "next_run_at": now,
        "last_run_at": None,
        "last_error": "",
        "created_at": now,
        "updated_at": now,
    }
    update = AsyncMock(return_value={**current, "worker_daemon_id": None})
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.database.autonomy.schedules.get_schedule",
            new=AsyncMock(return_value=current),
        ),
        patch("src.server.database.autonomy.schedules.update_schedule", update),
    ):
        resp = await client.patch(
            f"/api/v1/autonomy/schedules/{schedule_id}",
            json={"worker_daemon_id": None},
        )
    assert resp.status_code == 200
    assert update.call_args.kwargs["worker_daemon_id"] is None
    assert resp.json()["worker_daemon_id"] is None


@pytest.mark.asyncio
async def test_policy_can_edit_the_admission_table(client):
    admission = {"unlisted_addressed": "explicit"}
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.services.autonomy.policy.current_policy",
            new=AsyncMock(
                return_value={
                    "accept_mode": "explicit",
                    "version": "v2",
                    "rules": [],
                    "admission": {},
                }
            ),
        ),
        patch(
            "src.server.services.autonomy.policy.set_admission",
            new=AsyncMock(
                return_value={
                    "accept_mode": "explicit",
                    "version": "v3",
                    "rules": [],
                    "admission": admission,
                }
            ),
        ) as set_admission,
        patch(
            "src.server.services.autonomy.policy.policy_changed",
            new=AsyncMock(return_value=1),
        ),
        patch(
            "src.server.services.autonomy.policy.notify_policy_change",
            new=AsyncMock(return_value=1),
        ),
    ):
        resp = await client.patch(
            "/api/v1/autonomy/policy", json={"admission": admission}
        )
    assert resp.status_code == 200
    assert resp.json()["admission"] == admission
    set_admission.assert_awaited_once_with(USER_ID, admission)


@pytest.mark.asyncio
async def test_retry_revokes_a_mandate_when_the_sender_is_no_longer_admitted(client):
    now = datetime.now(timezone.utc)
    row = {
        "ticket_id": "44444444-4444-4444-8444-444444444444",
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Fix",
        "scope": "Fix",
        "status": "waiting",
        "mandate": {"strength": "explicit", "source_id": "s1", "trigger_id": "trg1"},
        "origin_trigger_id": "trg1",
        "origin_thread_key": "github:acme/widgets#1",
        "attempts": 0,
        "summary": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }
    update = AsyncMock(return_value={**row, "status": "cancelled"})
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=row),
        ),
        patch("src.server.app.autonomy.tickets_db.update_ticket", update),
        patch(
            "src.server.app.autonomy.source_db.get_source_any_user",
            new=AsyncMock(
                return_value={
                    "kind": "github",
                    "enabled": True,
                    "config": {"workspace_id": "w1", "allowed_logins": []},
                }
            ),
        ),
        patch(
            "src.server.database.autonomy.triggers.get_trigger",
            new=AsyncMock(
                return_value={
                    "trigger_id": "trg1",
                    "actor_login": "alice",
                    "addressed": True,
                    "actor_is_bot": False,
                }
            ),
        ),
        patch(
            "src.server.services.autonomy.policy.current_policy",
            new=AsyncMock(
                return_value={
                    "accept_mode": "explicit",
                    "version": "v1",
                    "rules": [],
                    "admission": {},
                }
            ),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/tickets/44444444-4444-4444-8444-444444444444/retry"
        )
    assert resp.status_code == 409
    assert update.call_args.kwargs["status"] == "cancelled"
    assert "Mandate revoked" in update.call_args.kwargs["attention"]["question"]


@pytest.mark.asyncio
async def test_unavailable_evals_can_be_listed_and_requeued(client):
    eval_row = {
        "eval_id": "eval-1",
        "thread_key": "thread-1",
        "trigger_ids": ["trg-1"],
        "reason": "router unavailable after 3 attempts",
        "started_at": None,
        "decided_at": None,
    }
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.database.autonomy.evals.list_unavailable_for_user",
            new=AsyncMock(return_value=[eval_row]),
        ),
        patch(
            "src.server.database.autonomy.evals.requeue_unavailable",
            new=AsyncMock(return_value=True),
        ) as requeue,
    ):
        listed = await client.get("/api/v1/autonomy/evals")
        queued = await client.post("/api/v1/autonomy/evals/eval-1/requeue")
    assert listed.status_code == 200
    assert listed.json()["evals"][0]["eval_id"] == "eval-1"
    assert queued.status_code == 200
    requeue.assert_awaited_once_with("eval-1", USER_ID)


@pytest.mark.asyncio
async def test_daemon_creation_returns_the_token_once(client):
    row = {
        "daemon_id": "55555555-5555-4555-8555-555555555555",
        "user_id": USER_ID,
        "name": "local",
        "provider": "codex",
        "max_concurrency": 1,
        "last_seen_at": None,
        "created_at": datetime.now(timezone.utc),
    }
    create = AsyncMock(return_value=row)
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch("src.server.database.autonomy.daemons.create_daemon", create),
    ):
        resp = await client.post(
            "/api/v1/autonomy/daemons", json={"name": "local", "provider": "codex"}
        )
    assert resp.status_code == 201
    assert resp.json()["token"]
    assert resp.json()["daemon_id"] == row["daemon_id"]
    assert create.await_args.kwargs["token_hash"] != resp.json()["token"]
    assert create.await_args.kwargs["max_concurrency"] == 1


@pytest.mark.asyncio
async def test_daemon_concurrency_can_be_updated(client):
    daemon_id = "55555555-5555-4555-8555-555555555555"
    row = {
        "daemon_id": daemon_id, "name": "local", "provider": "codex",
        "max_concurrency": 3, "last_seen_at": None,
        "created_at": datetime.now(timezone.utc),
    }
    update = AsyncMock(return_value=row)
    with (
        patch("src.server.app.autonomy.features.user_feature_enabled", new=AsyncMock(return_value=True)),
        patch("src.server.database.autonomy.daemons.update_daemon_concurrency", update),
    ):
        resp = await client.patch(
            f"/api/v1/autonomy/daemons/{daemon_id}", json={"max_concurrency": 3}
        )
    assert resp.status_code == 200
    assert resp.json()["max_concurrency"] == 3
    update.assert_awaited_once_with(USER_ID, daemon_id, 3)


@pytest.mark.asyncio
async def test_daemon_concurrency_rejects_values_outside_supported_range(client):
    resp = await client.patch(
        "/api/v1/autonomy/daemons/55555555-5555-4555-8555-555555555555",
        json={"max_concurrency": 33},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_a_daemon_can_claim_and_report_a_ticket(client):
    daemon_row = {"daemon_id": "d-1", "user_id": USER_ID, "name": "local"}
    ticket = {
        "ticket_id": "66666666-6666-4666-8666-666666666666",
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "local task",
        "worker_provider": "codex",
        "mandate": {"source_id": "s1", "cwd": "/worktrees/repo", "private_marker": "must not be projected"},
        "lease_id": "77777777-7777-4777-8777-777777777777",
        "lease_expires_at": datetime.now(timezone.utc),
        "lease_duration_seconds": 90,
    }
    with (
        patch(
            "src.server.app.autonomy._require_daemon",
            new=AsyncMock(return_value=daemon_row),
        ),
        patch(
            "src.server.services.autonomy.worker.claim_daemon_ticket",
            new=AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.database.autonomy.sources.get_source_any_user",
            new=AsyncMock(return_value={"kind": "manual", "config": {}}),
        ),
        patch(
            "src.server.services.autonomy.worker.worker_prompt",
            return_value="native prompt",
        ),
    ):
        claimed = await client.post(
            "/api/v1/autonomy/daemon/claim",
            headers={"X-Daemon-Token": "secret"},
            json={"claim_id": "88888888-8888-4888-8888-888888888888"},
        )
    assert claimed.status_code == 200
    assert claimed.json()["prompt"] == "native prompt"
    assert claimed.json()["ticket"]["ticket_id"] == ticket["ticket_id"]
    assert claimed.json()["ticket"]["cwd"] == "/worktrees/repo"
    assert "private_marker" not in claimed.json()["ticket"]


@pytest.mark.asyncio
async def test_daemon_claim_forwards_stable_id_and_heartbeat_reports_each_lease(client):
    daemon_row = {"daemon_id": "d-1", "user_id": USER_ID}
    claim = AsyncMock(return_value=None)
    leases = [
        {
            "ticket_id": "66666666-6666-4666-8666-666666666666",
            "lease_id": "77777777-7777-4777-8777-777777777777",
            "renewed": True,
        },
        {
            "ticket_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "lease_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "renewed": False,
        },
    ]
    heartbeat = AsyncMock(return_value=leases)
    with (
        patch(
            "src.server.app.autonomy._require_daemon",
            new=AsyncMock(return_value=daemon_row),
        ),
        patch("src.server.services.autonomy.worker.claim_daemon_ticket", claim),
        patch("src.server.database.autonomy.daemons.heartbeat", heartbeat),
    ):
        claimed = await client.post(
            "/api/v1/autonomy/daemon/claim",
            headers={"X-Daemon-Token": "secret"},
            json={"claim_id": "88888888-8888-4888-8888-888888888888"},
        )
        renewed = await client.post(
            "/api/v1/autonomy/daemon/heartbeat",
            headers={"X-Daemon-Token": "secret"},
            json={
                "leases": [
                    {
                        "ticket_id": leases[0]["ticket_id"],
                        "lease_id": leases[0]["lease_id"],
                    },
                    {
                        "ticket_id": leases[1]["ticket_id"],
                        "lease_id": leases[1]["lease_id"],
                    },
                ]
            },
        )
    assert claimed.status_code == 200 and claimed.json() == {"ticket": None}
    claim.assert_awaited_once_with(
        "d-1", "88888888-8888-4888-8888-888888888888", policy_version=""
    )
    heartbeat.assert_awaited_once()
    assert heartbeat.await_args.args[0] == "d-1"
    assert heartbeat.await_args.args[1] == [
        {"ticket_id": leases[0]["ticket_id"], "lease_id": leases[0]["lease_id"]},
        {"ticket_id": leases[1]["ticket_id"], "lease_id": leases[1]["lease_id"]},
    ]
    assert renewed.json()["leases"] == leases


@pytest.mark.asyncio
async def test_daemon_rejects_a_malformed_token_without_database_lookup(client):
    lookup = AsyncMock()
    with patch("src.server.database.autonomy.daemons.daemon_for_token", lookup):
        resp = await client.post("/api/v1/autonomy/daemon/heartbeat")
    assert resp.status_code == 401
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_daemon_rejects_an_unknown_well_formed_token(client):
    lookup = AsyncMock(return_value=None)
    with patch("src.server.database.autonomy.daemons.daemon_for_token", lookup):
        resp = await client.post(
            "/api/v1/autonomy/daemon/heartbeat",
            headers={"X-Daemon-Token": "a" * 43},
        )
    assert resp.status_code == 401
    lookup.assert_awaited_once()


@pytest.mark.asyncio
async def test_daemon_registration_rejects_an_unregistered_cli_provider(client):
    create = AsyncMock()
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch("src.server.database.autonomy.daemons.create_daemon", create),
    ):
        resp = await client.post(
            "/api/v1/autonomy/daemons", json={"name": "local", "provider": "unknown"}
        )
    assert resp.status_code == 400
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_rejects_a_daemon_owned_by_another_user(client):
    daemon_id = "55555555-5555-4555-8555-555555555555"
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.app.autonomy._workspace",
            new=AsyncMock(return_value={"workspace_id": "w1"}),
        ),
        patch(
            "src.server.database.autonomy.daemons.get_daemon",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/schedules",
            json={
                "prompt": "Review updates",
                "workspace_id": "w1",
                "cron_expression": "0 9 * * *",
                "worker_daemon_id": daemon_id,
            },
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_daemon_cannot_delete_a_registered_daemon_that_is_still_in_use(client):
    from uuid import UUID

    daemon_id = UUID("55555555-5555-4555-8555-555555555555")
    with (
        patch(
            "src.server.app.autonomy.features.user_feature_enabled",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "src.server.database.autonomy.daemons.delete_daemon",
            new=AsyncMock(return_value="in_use"),
        ) as delete,
    ):
        resp = await client.delete(f"/api/v1/autonomy/daemons/{daemon_id}")
    assert resp.status_code == 409
    delete.assert_awaited_once_with(USER_ID, str(daemon_id))


@pytest.mark.asyncio
async def test_daemon_report_rejects_a_ticket_that_is_not_running(client):
    with (
        patch(
            "src.server.app.autonomy._require_daemon",
            new=AsyncMock(return_value={"daemon_id": "d1", "user_id": USER_ID}),
        ),
        patch(
            "src.server.database.autonomy.daemons.lease_binding",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.host.apply_daemon_worker_report",
            new=AsyncMock(return_value=None),
        ),
    ):
        resp = await client.post(
            "/api/v1/autonomy/daemon/tickets/66666666-6666-4666-8666-666666666666/report",
            json={
                "state": "ready",
                "summary": "done",
                "lease_id": "77777777-7777-4777-8777-777777777777",
            },
            headers={"X-Daemon-Token": "a" * 43},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_daemon_report_forwards_the_uncommitted_changes_attention_reason():
    from uuid import UUID

    from src.server.app import autonomy as autonomy_app
    from src.server.models.autonomy import DaemonReport

    with (
        patch.object(
            autonomy_app,
            "_require_daemon",
            new=AsyncMock(return_value={"daemon_id": "d1", "user_id": USER_ID}),
        ),
        patch(
            "src.server.database.autonomy.daemons.lease_binding",
            new=AsyncMock(return_value={"grant": None, "revoked_at": None}),
        ),
        patch(
            "src.server.services.autonomy.host.apply_daemon_worker_report",
            new=AsyncMock(return_value={"status": "blocked"}),
        ) as apply_report,
    ):
        result = await autonomy_app.daemon_report(
            UUID("66666666-6666-4666-8666-666666666666"),
            DaemonReport(
                lease_id=UUID("77777777-7777-4777-8777-777777777777"),
                state="blocked",
                summary="unfinished changes",
                body="git status --short: M file",
                question="Commit or discard changes.",
                attention_reason="uncommitted_changes",
            ),
            x_daemon_token="a" * 43,
        )
    assert result == {"status": "blocked"}
    assert apply_report.await_args.kwargs["attention_reason"] == "uncommitted_changes"


@pytest.mark.asyncio
async def test_attaching_an_archived_ticket_to_a_scope_is_refused(monkeypatch):
    """Archive is terminal: an archived Ticket cannot join a context scope."""
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    from src.server.app import autonomy as autonomy_app

    archived = {
        "ticket_id": "t-1",
        "status": "archived",
        "user_id": "u-1",
        "context_scope_id": None,
    }
    monkeypatch.setattr(
        autonomy_app.tickets_db, "get_ticket", AsyncMock(return_value=archived)
    )

    with pytest.raises(HTTPException) as error:
        await autonomy_app._attach_ticket_to_scope(
            scope={
                "scope_id": "s-1",
                "user_id": "u-1",
                "kind": "shared",
                "policy": {},
            },
            ticket_id="t-1",
            user_id="u-1",
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Ticket has ended"
