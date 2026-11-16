"""RA-3: the message door is a Super-agent door.

Only a Router or a Worker-class lane may send a turn, and only as a background
dispatch. Every other surface — a person's chat, a channel thread, a legacy
automation — answers ``410 chat_retired`` with a migration hint before any
durable work happens.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from tests.unit.server.app.test_threads_dispatch_auth import (
    _BODY,
    TOKEN,
    USER,
    _app,
    _post,
    _stub_workflow,
)

THREADS_MOD = "src.server.app.threads.messaging"
DISPATCH = {"X-Dispatch": "background", "X-Service-Token": TOKEN}


def _meta(platform, *, user=USER):
    return {
        "user_id": user,
        "workspace_id": "ws-placeholder",
        "llm_model": None,
        "platform": platform,
    }


async def _post_with_meta(monkeypatch, *, meta, body=None, headers=DISPATCH):
    monkeypatch.setattr("src.config.settings.HOST_MODE", "platform")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    app = _app()
    with (
        _stub_workflow(),
        patch(
            f"{THREADS_MOD}.get_thread_auth_meta",
            new=AsyncMock(return_value=meta),
        ),
        patch(
            "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
            new=AsyncMock(return_value={"ticket_id": "tk-1", "user_id": USER}),
        ),
    ):
        return await _post(app, headers=headers, body=body or _BODY)


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["web", "telegram", "slack", "discord", None])
async def test_a_non_superagent_thread_is_gone(monkeypatch, platform):
    body = {**_BODY, "platform": platform} if platform else dict(_BODY)
    resp = await _post_with_meta(monkeypatch, meta=_meta(platform), body=body)

    assert resp.status_code == 410, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "chat_retired"
    assert "Ticket" in detail["hint"]


@pytest.mark.asyncio
async def test_a_stored_ordinary_surface_cannot_be_relabelled(monkeypatch):
    """The stored thread surface wins over a client-claimed ``platform``."""
    resp = await _post_with_meta(
        monkeypatch,
        meta=_meta("web"),
        body={**_BODY, "platform": "superagent_router"},
    )

    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "chat_retired"


@pytest.mark.asyncio
async def test_a_new_thread_with_no_platform_is_gone(monkeypatch):
    # No stored row: a brand-new id. The request names no surface either, which
    # is what the retired web chat used to send.
    resp = await _post_with_meta(monkeypatch, meta=None)

    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "chat_retired"


@pytest.mark.asyncio
async def test_a_router_thread_must_still_be_dispatched(monkeypatch):
    """A Router thread is live, but only the server's dispatch may start it."""
    resp = await _post_with_meta(
        monkeypatch,
        meta=_meta("superagent_router"),
        body={**_BODY, "platform": "superagent_router"},
        headers={},
    )

    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "chat_retired"


@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["superagent_router", "superagent_worker"])
async def test_router_and_worker_dispatch_still_run(monkeypatch, platform):
    resp = await _post_with_meta(
        monkeypatch,
        meta=_meta(platform),
        body={**_BODY, "platform": platform},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "dispatched"


@pytest.mark.asyncio
async def test_a_watchdog_dispatch_still_runs(monkeypatch):
    """Watchdog is a Worker-class review lane, not a person's chat."""
    resp = await _post_with_meta(
        monkeypatch,
        meta=_meta("superagent_watchdog"),
        body={**_BODY, "platform": "superagent_watchdog"},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "dispatched"


# --- Codex review fixes: the stored surface is the only authority -----------


async def _dispatch_capture(monkeypatch, *, meta, body):
    """POST a dispatch on a thread whose row is ``meta``; return the response
    and the kwargs ``astream_ptc_workflow`` was handed (empty if refused)."""
    from tests.unit.server.app.test_threads_dispatch_auth import _stub_ptc_workflow

    monkeypatch.setattr("src.config.settings.HOST_MODE", "platform")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    seen: dict = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return _stub_ptc_workflow(**kwargs)

    with (
        _stub_workflow(),
        patch(
            f"{THREADS_MOD}.get_thread_auth_meta",
            new=AsyncMock(return_value=meta),
        ),
        patch("src.server.handlers.chat.astream_ptc_workflow", new=_capture),
        patch(
            "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
            new=AsyncMock(return_value={"ticket_id": "tk-1", "user_id": USER}),
        ),
    ):
        resp = await _post(_app(), headers=DISPATCH, body=body)
    return resp, seen


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claimed", ["superagent_router", "superagent_worker", "superagent_watchdog"]
)
async def test_a_stored_null_surface_cannot_claim_a_live_lane(monkeypatch, claimed):
    """Finding 1: an existing thread with ``platform=NULL`` is a retired chat
    thread; a dispatch claiming a live lane on the request does not open it."""
    resp, seen = await _dispatch_capture(
        monkeypatch, meta=_meta(None), body={**_BODY, "platform": claimed}
    )

    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "chat_retired"
    assert seen == {}


@pytest.mark.asyncio
async def test_a_row_read_without_the_surface_column_is_retired(monkeypatch):
    """A row that does not say its surface is not trusted to be a live one."""
    meta = {"user_id": USER, "workspace_id": "ws-placeholder", "llm_model": None}
    resp, seen = await _dispatch_capture(
        monkeypatch, meta=meta, body={**_BODY, "platform": "superagent_worker"}
    )

    assert resp.status_code == 410, resp.text
    assert resp.json()["detail"]["code"] == "chat_retired"
    assert seen == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [None, "superagent_router", "superagent_watchdog"])
async def test_a_worker_thread_runs_as_a_worker_whatever_the_request_says(
    monkeypatch, sent
):
    """Finding 2: a Worker report-back omits ``platform``; a mismatched one
    names another lane. Either way the turn runs with the Worker role, host
    and tools of the thread it is."""
    from src.server.services.autonomy.host import AutonomyWorkerHost
    from src.tools.autonomy import AUTONOMY_WORKER_TOOLS

    body = {**_BODY, **({"platform": sent} if sent else {})}
    resp, seen = await _dispatch_capture(
        monkeypatch, meta=_meta("superagent_worker"), body=body
    )

    assert resp.status_code == 200, resp.text
    assert seen["route"].role == "worker"
    assert seen["request"].platform == "superagent_worker"
    assert isinstance(seen["extra_configurable"]["autonomy_host"], AutonomyWorkerHost)
    assert seen["extra_tools"] is AUTONOMY_WORKER_TOOLS
    assert seen["superagent_lane"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [None, "superagent_worker"])
async def test_a_router_thread_runs_as_the_router_whatever_the_request_says(
    monkeypatch, sent
):
    from src.tools.autonomy import AUTONOMY_ROUTER_TOOLS

    body = {**_BODY, **({"platform": sent} if sent else {})}
    resp, seen = await _dispatch_capture(
        monkeypatch, meta=_meta("superagent_router"), body=body
    )

    assert resp.status_code == 200, resp.text
    assert seen["route"].role == "supervisor"
    assert seen["request"].platform == "superagent_router"
    assert seen["extra_tools"] is AUTONOMY_ROUTER_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [None, "superagent_router"])
async def test_a_watchdog_thread_keeps_its_tools_whatever_the_request_says(
    monkeypatch, sent
):
    from src.tools.autonomy import AUTONOMY_WATCHDOG_TOOLS

    body = {**_BODY, **({"platform": sent} if sent else {})}
    resp, seen = await _dispatch_capture(
        monkeypatch, meta=_meta("superagent_watchdog"), body=body
    )

    assert resp.status_code == 200, resp.text
    assert seen["route"].role == "generalist"
    assert seen["request"].platform == "superagent_watchdog"
    assert seen["extra_tools"] is AUTONOMY_WATCHDOG_TOOLS


@pytest.mark.asyncio
async def test_a_new_thread_is_classified_by_the_request_that_creates_it(
    monkeypatch,
):
    resp, seen = await _dispatch_capture(
        monkeypatch, meta=None, body={**_BODY, "platform": "superagent_worker"}
    )

    assert resp.status_code == 200, resp.text
    assert seen["route"].role == "worker"


# --- Finding 3: the retired wiring is absent ---------------------------------


def test_router_and_worker_graphs_are_built_without_subagents():
    """Only the Watchdog (generalist) keeps Task/TaskOutput/RunWorkflow; the
    Worker's roster is empty, so it no longer gets the dead Task wiring."""
    from src.server.contracts.agent_roles import ALL_ROLES
    from src.server.handlers.chat.ptc_run import NO_SUBAGENT_ROLES

    assert NO_SUBAGENT_ROLES == {"supervisor", "worker"}
    assert not NO_SUBAGENT_ROLES & ALL_ROLES


def test_the_chief_of_staff_report_back_has_no_executor(monkeypatch):
    """Only the live hook types are registered: the analyst ``report_back``
    (Chief of Staff hand-off) went with direct chat."""
    import src.server.services.hook_outbox as hook_outbox
    from src.server.services.runs.reports import steer_executor, subagent
    from src.server.services.user_feed import register_outbox_executors

    registered: list[str] = []
    monkeypatch.setattr(
        hook_outbox,
        "register_hook_executor",
        lambda hook_type, *a, **k: registered.append(hook_type),
    )
    subagent.register_outbox_executors()
    steer_executor.register_outbox_executors()
    register_outbox_executors()

    assert "report_back" not in registered
    assert {"task_report_back", "steer"} <= set(registered)
