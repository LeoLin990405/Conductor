"""GET /api/v1/agents/fleet and /api/v1/agents/{id}/logs — read-only fleet API.

The scan and the log reader are patched at the router's imports, so these tests
pin the wire shape, the loopback gate and the validation, never a real scan.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.models.agent import Agent, AgentFleet, AgentLogs, AgentStatus
from src.server.services.agent_scanner import AgentLogNotFound
from tests.conftest import create_test_app

FLEET_PATH = "/api/v1/agents/fleet"


def _agent(**overrides) -> Agent:
    base = dict(
        id="agent-fleet",
        provider="codex",
        role="dev",
        status=AgentStatus.RUNNING,
        pid=4242,
        worktree="/Users/you/conductor-wt/agent-fleet",
        branch="feat/agent-fleet",
        log_file="agent-fleet.log",
        log_tail="starting",
    )
    base.update(overrides)
    return Agent(**base)


@pytest_asyncio.fixture
async def client():
    from src.server.app.agents import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as async_client:
        yield async_client


@pytest_asyncio.fixture
async def remote_client():
    from src.server.app.agents import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.7", 5555)),
        base_url="http://test",
    ) as async_client:
        yield async_client


class TestGetFleet:
    @pytest.mark.asyncio
    async def test_returns_agents_with_scan_context(self, client):
        agents = [_agent(), _agent(id="memfw-r2", status=AgentStatus.DONE, pid=None)]
        with patch(
            "src.server.app.agents.scan_agents", new=AsyncMock(return_value=agents)
        ):
            resp = await client.get(FLEET_PATH)
        assert resp.status_code == 200
        body = resp.json()
        assert [row["id"] for row in body["agents"]] == ["agent-fleet", "memfw-r2"]
        assert body["stall_threshold_mins"] == 5
        assert datetime.fromisoformat(body["scan_ts"]).tzinfo is not None
        assert body["error"] is None
        row = body["agents"][0]
        assert row["status"] == "running"
        assert row["provider_balance"] == "unknown"
        assert set(row) >= {
            "id",
            "model",
            "provider",
            "role",
            "pid",
            "worktree",
            "branch",
            "status",
            "elapsed_secs",
            "last_activity_ts",
            "log_file",
            "log_tail",
            "commits_ahead",
            "tests_state",
            "provider_balance",
        }

    @pytest.mark.asyncio
    async def test_disabled_deployment_reports_instead_of_scanning(self, client):
        config = patch(
            "src.server.app.agents.get_agent_fleet_config",
            return_value=type(
                "Config",
                (),
                {"enabled": False, "stall_mins": 5},
            )(),
        )
        with config, patch(
            "src.server.app.agents.scan_agents", new=AsyncMock(return_value=[])
        ) as scan:
            resp = await client.get(FLEET_PATH)
        assert resp.status_code == 200
        assert resp.json()["agents"] == []
        assert "disabled" in resp.json()["error"]
        scan.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scan_failure_is_reported_not_raised(self, client):
        with patch(
            "src.server.app.agents.scan_agents",
            new=AsyncMock(side_effect=RuntimeError("ps exploded")),
        ):
            resp = await client.get(FLEET_PATH)
        assert resp.status_code == 200
        assert resp.json()["agents"] == []
        assert "failed" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_remote_caller_is_refused(self, remote_client):
        resp = await remote_client.get(FLEET_PATH)
        assert resp.status_code == 403


class TestGetAgentLogs:
    @pytest.mark.asyncio
    async def test_returns_redacted_lines(self, client):
        logs = AgentLogs(
            agent_id="agent-fleet",
            log_file="agent-fleet.log",
            lines=["starting", "Authorization: [REDACTED]"],
            truncated=False,
        )
        with patch(
            "src.server.app.agents.read_agent_logs",
            new=AsyncMock(return_value=logs),
        ):
            resp = await client.get("/api/v1/agents/agent-fleet/logs?lines=2")
        assert resp.status_code == 200
        assert resp.json()["lines"] == ["starting", "Authorization: [REDACTED]"]

    @pytest.mark.asyncio
    async def test_unknown_agent_is_404(self, client):
        with patch(
            "src.server.app.agents.read_agent_logs",
            new=AsyncMock(side_effect=AgentLogNotFound("no log for agent 'ghost'")),
        ):
            resp = await client.get("/api/v1/agents/ghost/logs")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_disabled_deployment_refuses_to_read_a_log(self, client):
        config = patch(
            "src.server.app.agents.get_agent_fleet_config",
            return_value=type("Config", (), {"enabled": False, "stall_mins": 5})(),
        )
        # A log the disabled deployment would have handed back anyway, so the
        # regression reads as "the log came back" rather than a validation error.
        leaked = AgentLogs(
            agent_id="agent-fleet", log_file="agent-fleet.log", lines=["secret"], truncated=False
        )
        with config, patch(
            "src.server.app.agents.read_agent_logs", new=AsyncMock(return_value=leaked)
        ) as read:
            resp = await client.get("/api/v1/agents/agent-fleet/logs")
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"]
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_line_count_is_bounded(self, client):
        with patch("src.server.app.agents.read_agent_logs", new=AsyncMock()) as read:
            resp = await client.get("/api/v1/agents/agent-fleet/logs?lines=9000")
        assert resp.status_code == 400
        read.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_remote_caller_is_refused(self, remote_client):
        resp = await remote_client.get("/api/v1/agents/agent-fleet/logs")
        assert resp.status_code == 403


class TestAgentFleetModel:
    def test_fleet_defaults(self):
        fleet = AgentFleet(scan_ts=datetime.now(timezone.utc), stall_threshold_mins=5)
        assert fleet.agents == []
        assert fleet.error is None
