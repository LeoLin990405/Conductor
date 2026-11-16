"""Supervisor progress is a phase and a result, not a Worker transcript."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.services.autonomy.progress import build_ticket_progress
from src.server.services.autonomy.task_interaction import project_task_activity
from tests.conftest import create_test_app

USER_ID = "test-user-123"
TICKET_ID = "77777777-7777-4777-8777-777777777777"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
CREATED = NOW - timedelta(seconds=31, milliseconds=200)

_KEYS = {
    "ticket_id",
    "status",
    "phase",
    "elapsed_seconds",
    "steps",
    "summary",
    "body",
    "question",
    "error",
}


def _ticket(**overrides) -> dict:
    ticket = {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Say hello",
        "scope": "Do not touch files",
        "status": "queued",
        "mandate": {"prompt": "PROMPT_SECRET_do_not_return"},
        "origin_thread_key": "manual:task",
        "worker_thread_id": "thread-SECRET",
        "worker_run_id": "run-SECRET",
        "worker_provider": "codex",
        "worker_model": "gpt",
        "attempts": 0,
        "attention": None,
        "summary": "",
        "body": None,
        "error": "",
        "created_at": CREATED,
        "updated_at": NOW,
        "started_at": None,
        "ended_at": None,
    }
    ticket.update(overrides)
    return ticket


def _report(**overrides) -> dict:
    row = {
        "report_id": "r1",
        "ticket_id": TICKET_ID,
        "state": "ready",
        "summary": "",
        "question": "",
        "created_at": NOW - timedelta(seconds=1),
    }
    row.update(overrides)
    return row


def _rendered(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False)


def _assert_public(body: dict) -> None:
    assert set(body) == _KEYS
    for step in body["steps"]:
        assert set(step) == {"key", "label", "state"}
    rendered = _rendered(body)
    assert "thread-SECRET" not in rendered
    assert "run-SECRET" not in rendered
    assert "PROMPT_SECRET_do_not_return" not in rendered
    assert "invoke_tool:read_file_SECRET" not in rendered
    assert "wk-token-SECRET-9f3a" not in rendered
    assert "RAW_FILE_src/server/app.py" not in rendered


def test_queued_shows_only_the_created_step():
    body = build_ticket_progress(_ticket(), [], now=NOW)
    assert body["status"] == "queued"
    assert body["phase"] == "queued"
    assert body["steps"] == [
        {"key": "created", "label": "任务已创建", "state": "done"},
    ]
    assert body["summary"] == ""
    assert body["question"] == ""
    assert body["error"] == ""
    assert body["elapsed_seconds"] == 31.2
    _assert_public(body)


def test_running_marks_the_worker_active_and_keeps_a_progress_summary():
    body = build_ticket_progress(
        _ticket(
            status="running",
            started_at=CREATED + timedelta(seconds=2),
            summary="正在看保存按钮",
        ),
        [
            _report(state="progress", summary="正在看保存按钮"),
            {
                "kind": "tool",
                "tools": ["invoke_tool:read_file_SECRET"],
                "text": "RAW_FILE_src/server/app.py",
                "prompt": "PROMPT_SECRET_do_not_return",
            },
            {"kind": "text", "text": "wk-token-SECRET-9f3a"},
        ],
        now=NOW,
    )
    assert body["phase"] == "running"
    assert body["steps"] == [
        {"key": "created", "label": "任务已创建", "state": "done"},
        {"key": "worker", "label": "执行中", "state": "active"},
    ]
    assert body["summary"] == "正在看保存按钮"
    assert body["question"] == ""
    assert body["elapsed_seconds"] == 31.2
    _assert_public(body)


def test_completed_returns_the_result_and_elapsed_until_ended_at():
    summary = "你好！此 Ticket 无需修改文件。"
    body = build_ticket_progress(
        _ticket(
            status="completed",
            started_at=CREATED + timedelta(seconds=1),
            ended_at=CREATED + timedelta(milliseconds=31200),
            summary=summary,
            error="stale error must stay hidden",
        ),
        [_report(state="ready", summary=summary, question="ignored once it is done")],
        now=NOW + timedelta(hours=3),
    )
    assert body["status"] == "completed"
    assert body["phase"] == "completed"
    assert body["elapsed_seconds"] == 31.2
    assert body["steps"] == [
        {"key": "created", "label": "任务已创建", "state": "done"},
        {"key": "worker", "label": "执行中", "state": "done"},
        {"key": "report", "label": "结果已生成", "state": "done"},
    ]
    assert body["summary"] == summary
    assert body["question"] == ""
    assert body["error"] == ""
    assert "stale error must stay hidden" not in _rendered(body)
    assert "ignored once it is done" not in _rendered(body)
    _assert_public(body)


def test_failed_returns_error_and_summary_without_a_question():
    body = build_ticket_progress(
        _ticket(
            status="failed",
            started_at=CREATED + timedelta(seconds=1),
            ended_at=NOW,
            summary="无法完成保存按钮",
            error="worker exited 1",
            attention={"question": "this question belongs to waiting, not failed"},
        ),
        [_report(state="failed", summary="无法完成保存按钮", question="hidden question")],
        now=NOW + timedelta(hours=1),
    )
    assert body["status"] == "failed"
    assert body["phase"] == "failed"
    assert body["summary"] == "无法完成保存按钮"
    assert body["error"] == "worker exited 1"
    assert body["question"] == ""
    assert body["elapsed_seconds"] == 31.2
    assert [step["state"] for step in body["steps"]] == ["done", "failed", "failed"]
    assert [step["key"] for step in body["steps"]] == ["created", "worker", "report"]
    assert "hidden question" not in _rendered(body)
    assert "this question belongs to waiting, not failed" not in _rendered(body)
    _assert_public(body)


def test_failure_before_a_worker_starts_does_not_invent_an_execution_step():
    body = build_ticket_progress(
        _ticket(
            status="failed",
            ended_at=NOW,
            summary="cloud slot refused",
            error="isolation unavailable",
        ),
        [],
        now=NOW,
    )
    assert [step["key"] for step in body["steps"]] == ["created", "report"]
    assert body["steps"][1]["state"] == "failed"
    assert body["error"] == "isolation unavailable"
    assert body["summary"] == "cloud slot refused"


def test_waiting_and_blocked_return_the_question():
    waiting = build_ticket_progress(
        _ticket(
            status="waiting",
            started_at=CREATED,
            summary="需要一个人确认",
            attention={"question": "用哪个分支？"},
        ),
        [
            _report(
                state="waiting",
                summary="older report the ticket no longer shows",
                question="旧问题",
                created_at=CREATED,
            )
        ],
        now=NOW,
    )
    blocked = build_ticket_progress(
        _ticket(
            status="blocked",
            started_at=CREATED,
            summary="需要一个人确认",
            attention={"question": "用哪个分支？"},
        ),
        [],
        now=NOW,
    )
    for body, status in ((waiting, "waiting"), (blocked, "blocked")):
        assert body["status"] == status
        assert body["phase"] == "waiting"
        assert body["question"] == "用哪个分支？"
        assert body["summary"] == "需要一个人确认"
        assert body["error"] == ""
        assert [step["key"] for step in body["steps"]] == ["created", "worker", "report"]
        assert [step["state"] for step in body["steps"]] == ["done", "done", "done"]
        assert "旧问题" not in _rendered(body)
        assert "older report the ticket no longer shows" not in _rendered(body)
        _assert_public(body)


def test_waiting_uses_the_newest_report_question_when_attention_is_empty():
    body = build_ticket_progress(
        _ticket(status="blocked", started_at=CREATED, attention={}),
        [
            _report(state="blocked", question="新问题", created_at=NOW),
            _report(
                report_id="r0",
                state="blocked",
                question="旧问题",
                created_at=CREATED,
            ),
            _report(report_id="r2", state="progress", question="进度里的问题", created_at=NOW),
        ],
        now=NOW,
    )
    assert body["question"] == "新问题"
    assert "旧问题" not in _rendered(body)
    assert "进度里的问题" not in _rendered(body)


def test_summary_is_the_ticket_summary_unchanged():
    summary = "  你好！此 Ticket 无需修改文件。  "
    body = build_ticket_progress(
        _ticket(status="completed", ended_at=NOW, summary=summary),
        [_report(state="ready", summary="WORKER_DRAFT_do_not_replace")],
        now=NOW,
    )
    assert body["summary"] == summary
    assert "WORKER_DRAFT_do_not_replace" not in _rendered(body)


def test_projected_activity_does_not_leak_instructions_or_worker_text():
    ticket = _ticket(
        status="waiting",
        started_at=CREATED,
        summary="需要确认",
        attention={"question": "继续吗？"},
    )
    events = project_task_activity(
        ticket,
        [
            {
                "input_id": "i1",
                "kind": "instruction",
                "text": "PROMPT_SECRET_do_not_return RAW_FILE_src/server/app.py",
                "created_at": CREATED + timedelta(seconds=1),
            }
        ],
        [
            _report(
                state="waiting",
                summary="需要确认",
                question="继续吗？",
                created_at=NOW - timedelta(seconds=1),
            )
        ],
    )
    events.append(
        {
            "kind": "tool",
            "tools": ["invoke_tool:read_file_SECRET"],
            "text": "wk-token-SECRET-9f3a",
        }
    )
    body = build_ticket_progress(ticket, events, now=NOW)
    assert body["phase"] == "waiting"
    assert body["question"] == "继续吗？"
    assert body["summary"] == "需要确认"
    assert [step["key"] for step in body["steps"]] == ["created", "worker", "report"]
    _assert_public(body)


def test_elapsed_uses_now_while_open_and_clamps_a_reversed_end():
    open_body = build_ticket_progress(
        _ticket(created_at="2026-11-06T11:59:50Z"),
        [],
        now=NOW,
    )
    assert open_body["elapsed_seconds"] == 10.0
    reversed_body = build_ticket_progress(
        _ticket(ended_at=CREATED - timedelta(seconds=5)),
        [],
        now=NOW,
    )
    assert reversed_body["elapsed_seconds"] == 0.0
    missing = build_ticket_progress(_ticket(created_at=None), [], now=NOW)
    assert missing["elapsed_seconds"] == 0.0


def test_cancelled_and_archived_share_the_cancelled_phase():
    cancelled = build_ticket_progress(
        _ticket(
            status="cancelled",
            started_at=CREATED,
            ended_at=NOW,
            summary="",
        ),
        [],
        now=NOW,
    )
    archived = build_ticket_progress(
        _ticket(status="archived", ended_at=NOW, summary="已归档的结果"),
        [_report(state="ready", summary="已归档的结果")],
        now=NOW,
    )
    assert cancelled["phase"] == "cancelled"
    assert cancelled["status"] == "cancelled"
    assert [step["key"] for step in cancelled["steps"]] == ["created", "worker"]
    assert cancelled["steps"][1]["state"] == "done"
    assert archived["status"] == "archived"
    assert archived["phase"] == "cancelled"
    assert [step["key"] for step in archived["steps"]] == ["created", "worker", "report"]
    assert archived["summary"] == "已归档的结果"
    assert archived["question"] == ""


def test_requeued_ticket_does_not_look_finished():
    body = build_ticket_progress(
        _ticket(status="queued", summary=""),
        [_report(state="ready", summary="previous attempt")],
        now=NOW,
    )
    assert body["phase"] == "queued"
    assert [step["key"] for step in body["steps"]] == ["created"]
    assert "previous attempt" not in _rendered(body)


def test_progress_returns_latest_report_body_or_null_for_legacy_rows():
    rows = iter([
        {"report_id": "older", "state": "ready", "summary": "old", "body": "old body", "created_at": NOW - timedelta(seconds=1)},
        {"report_id": "newer", "state": "ready", "summary": "new", "body": None, "created_at": NOW},
    ])
    result = build_ticket_progress(_ticket(status="completed"), rows, now=NOW)
    assert result["body"] is None
    legacy = build_ticket_progress(
        _ticket(status="completed"),
        [{"report_id": "legacy", "state": "ready", "summary": "old"}],
        now=NOW,
    )
    assert legacy["body"] is None


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


def _feature(enabled: bool = True):
    return patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=enabled),
    )


@pytest.mark.asyncio
async def test_progress_route_matches_the_service_for_the_owner(client):
    ticket = _ticket(
        status="completed",
        started_at=CREATED,
        ended_at=CREATED + timedelta(milliseconds=31200),
        summary="你好！此 Ticket 无需修改文件。",
    )
    reports = [
        _report(
            state="ready",
            summary=ticket["summary"],
            body="First result line.\nSecond result line.",
        )
    ]
    with (
        _feature(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_reports",
            new=AsyncMock(return_value=reports),
        ),
    ):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/progress")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ticket_id"] == TICKET_ID
    assert body["status"] == "completed"
    assert body["phase"] == "completed"
    assert body["summary"] == "你好！此 Ticket 无需修改文件。"
    assert body["body"] == "First result line.\nSecond result line."
    assert body["question"] == ""
    assert body["error"] == ""
    assert body["elapsed_seconds"] == 31.2
    assert [step["state"] for step in body["steps"]] == ["done", "done", "done"]
    _assert_public(body)


@pytest.mark.asyncio
async def test_progress_route_returns_null_body_for_legacy_report(client):
    ticket = _ticket(
        status="completed",
        started_at=CREATED,
        ended_at=CREATED + timedelta(seconds=2),
        summary="Legacy result",
    )
    with (
        _feature(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.app.autonomy.tickets_db.list_reports",
            new=AsyncMock(return_value=[_report(state="ready", summary="Legacy result")]),
        ),
    ):
        response = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/progress")

    assert response.status_code == 200
    assert response.json()["body"] is None


@pytest.mark.asyncio
async def test_progress_404_matches_the_ticket_endpoint(client):
    reports = AsyncMock(return_value=[_report()])
    with (
        _feature(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=None),
        ),
        patch("src.server.app.autonomy.tickets_db.list_reports", new=reports),
    ):
        missing = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/progress")
    assert missing.status_code == 404
    assert missing.json()["detail"] == "Ticket not found"
    reports.assert_not_awaited()

    reports = AsyncMock(return_value=[_report(question="其他用户的问题")])
    with (
        _feature(),
        patch(
            "src.server.app.autonomy.tickets_db.get_ticket",
            new=AsyncMock(return_value=_ticket(user_id="someone-else", status="waiting")),
        ),
        patch("src.server.app.autonomy.tickets_db.list_reports", new=reports),
    ):
        foreign = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/progress")
    assert foreign.status_code == 404
    assert foreign.json()["detail"] == "Ticket not found"
    assert "其他用户的问题" not in foreign.text
    reports.assert_not_awaited()

    get_ticket = AsyncMock(return_value=_ticket())
    with (
        _feature(False),
        patch("src.server.app.autonomy.tickets_db.get_ticket", new=get_ticket),
    ):
        hidden = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/progress")
    assert hidden.status_code == 404
    assert hidden.json()["detail"] == "Feature not enabled"
    get_ticket.assert_not_awaited()
