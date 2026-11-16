from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.server.app.main_agent import router
from src.server.database.main_agent import SQLiteMainAgentRepository
from src.server.services.main_agent.models import RunNotFound
from src.server.services.main_agent.adapters import (
    DefaultReplySynthesizer,
    DefaultResultMerger,
    PassthroughClarificationGate,
    StubDeliberationAdapter,
    StubDivider,
    StubPlanner,
)
from src.server.services.main_agent.orchestrator import MainAgentOrchestrator
from tests.conftest import create_test_app


class _Orchestrator:
    def __init__(self):
        self.submit = AsyncMock(return_value={"id": "r1", "status": "queued"})
        self.get = AsyncMock(side_effect=RunNotFound("r1"))
        self.get_status_board = AsyncMock(
            return_value={
                "agents": [
                    {
                        "run_id": "r1",
                        "ticket_id": None,
                        "task_id": "t1",
                        "agent_id": "a1",
                        "daemon_id": "d1",
                        "state": "running",
                        "last_heartbeat": "2026-11-02T00:00:00+00:00",
                        "current_step": "plan",
                        "stalled": False,
                        "stall_reason": None,
                    }
                ],
                "alerts": [],
                "thresholds": {"heartbeat_seconds": 120, "progress_seconds": 600},
            }
        )


@pytest_asyncio.fixture
async def client():
    app: FastAPI = create_test_app(router)
    app.state.main_agent_orchestrator = _Orchestrator()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, app.state.main_agent_orchestrator


@pytest.mark.asyncio
async def test_status_board_returns_service_contract_without_projection(client):
    c, orchestrator = client
    response = await c.get("/api/v1/main-agent/status-board")
    assert response.status_code == 200
    assert response.json()["agents"][0]["task_id"] == "t1"
    assert response.json()["thresholds"] == {
        "heartbeat_seconds": 120,
        "progress_seconds": 600,
    }
    orchestrator.get_status_board.assert_awaited_once_with("test-user-123")


@pytest.mark.asyncio
async def test_submit_uses_authenticated_user_and_normalized_request(client):
    c, orchestrator = client
    response = await c.post(
        "/api/v1/main-agent/runs", json={"goal": "Review this", "context": {"x": 1}}
    )
    assert response.status_code == 200
    orchestrator.submit.assert_awaited_once_with(
        "test-user-123", {"objective": "Review this", "context": {"x": 1}}
    )


@pytest.mark.asyncio
async def test_run_lookup_hides_missing_or_foreign_run(client):
    c, _orchestrator = client
    response = await c.get("/api/v1/main-agent/runs/r1")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_sqlite_asgi_lifecycle_claim_callbacks_and_reconciliation(tmp_path):
    app = create_test_app(router)
    app.state.main_agent_orchestrator = MainAgentOrchestrator(
        repository=SQLiteMainAgentRepository(tmp_path / "api-lifecycle.sqlite"),
        divider=StubDivider(),
        deliberation=StubDeliberationAdapter(),
        planner=StubPlanner(),
        merger=DefaultResultMerger(),
        reply_synthesizer=DefaultReplySynthesizer(),
        clarification_gate=PassthroughClarificationGate(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        submitted = await c.post("/api/v1/main-agent/runs", json={"goal": "review"})
        assert submitted.status_code == 200
        run_id = submitted.json()["id"]

        ready = await c.get(f"/api/v1/main-agent/runs/{run_id}/tasks/ready")
        assert ready.status_code == 200
        task_id = ready.json()["tasks"][0]["id"]
        claim = await c.post(
            f"/api/v1/main-agent/runs/{run_id}/tasks/{task_id}/claim",
            json={"agent_id": "worker-1", "daemon_id": "daemon-1"},
        )
        assert claim.status_code == 200
        claim_token = claim.json()["claim_token"]

        conflict = await c.post(
            f"/api/v1/main-agent/runs/{run_id}/tasks/{task_id}/claim",
            json={"agent_id": "worker-2"},
        )
        assert conflict.status_code == 409

        heartbeat = await c.post(
            f"/api/v1/main-agent/runs/{run_id}/tasks/{task_id}/heartbeat",
            json={
                "agent_id": "worker-1",
                "claim_token": "wrong",
                "current_step": "read",
            },
        )
        assert heartbeat.status_code == 409
        heartbeat = await c.post(
            f"/api/v1/main-agent/runs/{run_id}/tasks/{task_id}/heartbeat",
            json={
                "agent_id": "worker-1",
                "claim_token": claim_token,
                "current_step": "read",
            },
        )
        assert heartbeat.status_code == 200

        completed = await c.post(
            f"/api/v1/main-agent/runs/{run_id}/tasks/{task_id}/complete",
            json={
                "agent_id": "worker-1",
                "claim_token": "wrong",
                "result": {"ok": True},
            },
        )
        assert completed.status_code == 409
        completed = await c.post(
            f"/api/v1/main-agent/runs/{run_id}/tasks/{task_id}/complete",
            json={
                "agent_id": "worker-1",
                "claim_token": claim_token,
                "result": {"ok": True},
            },
        )
        assert completed.status_code == 200

        final = await c.post(f"/api/v1/main-agent/runs/{run_id}/tick")
        assert final.status_code == 200
        assert final.json()["status"] == "completed"

        await app.state.main_agent_orchestrator.repository.create_run(
            "other-user",
            {
                "id": "foreign-run",
                "status": "queued",
                "phase": "execute",
                "request": {"objective": "private"},
                "tasks": [],
                "alerts": [],
            },
        )
        foreign = await c.get("/api/v1/main-agent/runs/foreign-run")
        assert foreign.status_code == 404
