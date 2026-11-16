"""JSON-shaped values for the durable main-agent coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TypedDict


RunStatus = Literal[
    "accepted", "clarification_required", "queued", "running", "completed", "failed"
]
TaskStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class TaskRecord(TypedDict, total=False):
    id: str
    revision: int
    description: str
    deliberation_agent_id: str | None
    depends_on: list[str]
    status: TaskStatus
    assigned_agent_id: str | None
    daemon_id: str | None
    claim_token: str | None
    lease_until: str | None
    heartbeat_at: str | None
    progress_at: str | None
    attempt: int
    result: dict[str, Any] | None
    error: dict[str, Any] | None


class RunRecord(TypedDict, total=False):
    id: str
    user_id: str
    status: RunStatus
    revision: int
    request: dict[str, Any]
    phase: str
    clarification: dict[str, Any] | None
    deliberation: dict[str, Any] | None
    plan: dict[str, Any] | None
    division: list[TaskRecord]
    tasks: list[TaskRecord]
    alerts: list[dict[str, Any]]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class MainAgentThresholds:
    heartbeat_stale_seconds: int = 120
    progress_stale_seconds: int = 600
    lease_seconds: int = 180


class MainAgentError(Exception):
    """Base class for coordinator contract errors."""


class RunNotFound(MainAgentError, LookupError):
    pass


class ConcurrentUpdate(MainAgentError):
    pass


class CoordinatorBusy(ConcurrentUpdate):
    """A live coordinator owns the run; callers can retry without lost input."""


class InvalidPlan(MainAgentError):
    pass


class TaskOwnershipError(MainAgentError):
    pass
