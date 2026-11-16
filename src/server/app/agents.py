"""Read-only agent fleet dashboard endpoints.

The fleet is a property of the host the backend runs on: its process table, its
worktrees, its log files. Like the local update endpoints, these routes are
loopback-only, so a remote caller in a hosted deployment cannot read the host's
process list, and nothing here mutates state.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, HTTPException, Request
from src.config.settings import get_agent_fleet_config
from src.server.models.agent import AgentFleet, AgentLogs
from src.server.services.agent_scanner import (
    AgentLogNotFound,
    ScanSettings,
    read_agent_logs,
    scan_agents,
)
from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1", tags=["agents"])

logger = structlog.get_logger(__name__)

_MAX_LOG_LINES = 500

# One wording for both endpoints, so a disabled deployment reads the same
# whether the operator opened the dashboard or asked for one agent's log.
_DISABLED_DETAIL = "Agent fleet discovery is disabled on this deployment."

def _require_loopback(request: Request) -> None:
    client = request.client.host if request.client else ""
    if client not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(
            status_code=403,
            detail="The agent fleet is only available on the machine that runs it.",
        )


@router.get("/agents/fleet", response_model=AgentFleet)
async def get_agent_fleet(user_id: CurrentUserId, request: Request) -> AgentFleet:
    """Scan the local agent worktrees and report one row per agent."""
    _require_loopback(request)
    config = get_agent_fleet_config()
    settings = ScanSettings.from_config(config)
    scan_ts = datetime.now(timezone.utc)
    if not config.enabled:
        return AgentFleet(
            scan_ts=scan_ts,
            stall_threshold_mins=config.stall_mins,
            error=_DISABLED_DETAIL,
        )
    try:
        agents = await scan_agents(settings)
    except Exception as exc:  # a scan bug must not take the dashboard down
        logger.warning("agent fleet scan failed", error=str(exc))
        return AgentFleet(
            scan_ts=scan_ts,
            stall_threshold_mins=config.stall_mins,
            error="The fleet scan failed; the worktrees were not read.",
        )
    return AgentFleet(
        agents=agents, scan_ts=scan_ts, stall_threshold_mins=config.stall_mins
    )

@router.get("/agents/{agent_id}/logs", response_model=AgentLogs)
async def get_agent_logs(
    agent_id: str, user_id: CurrentUserId, request: Request, lines: int = 100
) -> AgentLogs:
    """Return a bounded, redacted slice of one agent's log.

    The agent id is resolved against the configured log directory server-side;
    a client never supplies a path, and the text arrives pre-redacted.
    """
    _require_loopback(request)
    if lines < 1 or lines > _MAX_LOG_LINES:
        raise HTTPException(
            status_code=400, detail=f"lines must be between 1 and {_MAX_LOG_LINES}"
        )
    config = get_agent_fleet_config()
    if not config.enabled:
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)
    try:
        return await read_agent_logs(
            agent_id,
            ScanSettings.from_config(config),
            max_lines=lines,
        )
    except AgentLogNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
