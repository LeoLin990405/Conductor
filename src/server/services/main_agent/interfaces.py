"""Ports used by the coordinator; implementations own I/O and agent work."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, AsyncContextManager, Protocol

from .models import RunRecord, TaskRecord


class MainAgentRepository(Protocol):
    """Durable CAS boundary. Every run/task mutation advances its revision."""

    def run_guard(self, run_id: str) -> AsyncContextManager[bool]: ...

    async def create_run(self, user_id: str, request: dict[str, Any]) -> RunRecord: ...

    async def get_run(
        self, run_id: str, user_id: str | None = None
    ) -> RunRecord | None: ...

    async def transition_run(
        self, run_id: str, expected_revision: int, patch: dict[str, Any]
    ) -> RunRecord | None: ...

    async def list_runs(
        self,
        user_id: str | None,
        statuses: Sequence[str] | None = None,
        limit: int = 500,
        *,
        oldest_first: bool = False,
    ) -> list[RunRecord]: ...

    async def claim_task(
        self,
        run_id: str,
        task_id: str,
        agent_id: str,
        expected_revision: int,
        lease_seconds: int,
        *,
        daemon_id: str | None = None,
        user_id: str | None = None,
    ) -> TaskRecord | None: ...

    async def heartbeat_task(
        self,
        run_id: str,
        task_id: str,
        agent_id: str,
        expected_revision: int,
        current_step: str | None = None,
        *,
        lease_seconds: int = 300,
        user_id: str | None = None,
        claim_token: str,
    ) -> TaskRecord | None: ...

    async def complete_task(
        self,
        run_id: str,
        task_id: str,
        agent_id: str,
        expected_revision: int,
        result: dict[str, Any],
        *,
        user_id: str | None = None,
        claim_token: str,
    ) -> TaskRecord | None: ...

    async def fail_task(
        self,
        run_id: str,
        task_id: str,
        agent_id: str,
        expected_revision: int,
        error: dict[str, Any],
        retryable: bool,
        *,
        user_id: str | None = None,
        claim_token: str,
    ) -> TaskRecord | None: ...

    async def retry_task(
        self, run_id: str, task_id: str, expected_revision: int, *, user_id: str
    ) -> TaskRecord | None: ...

    async def record_alert_once(
        self, run_id: str, task_id: str, alert_key: str, record: dict[str, Any]
    ) -> bool: ...


class ClarificationGate(Protocol):
    async def check(self, request: Mapping[str, Any]) -> dict[str, Any]: ...
    async def confirm(self, request: Mapping[str, Any], text: str) -> dict[str, Any]: ...


class Deliberation(Protocol):
    async def deliberate(
        self, request: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]: ...


class Divider(Protocol):
    async def divide(self, request: Mapping[str, Any]) -> list[dict[str, Any]]: ...


class Planner(Protocol):
    async def plan(
        self,
        request: Mapping[str, Any],
        tasks: Sequence[Mapping[str, Any]],
        deliberation: Mapping[str, Any],
    ) -> dict[str, Any]: ...


class ResultMerger(Protocol):
    async def merge(
        self, request: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]: ...


class ReplySynthesizer(Protocol):
    async def synthesize(
        self, request: Mapping[str, Any], merged: Mapping[str, Any]
    ) -> dict[str, Any]: ...
