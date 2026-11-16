"""The subagent contract call site: projected once, after the winning finalize.

The ledger CAS is the truth; the contract projection must run only when the CAS
won (a retry that lost must not re-project) and must never be allowed to affect
the finalize's return value.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import src.server.services.refactor.dual_write as dual
import src.server.services.runs.task_run_coordinator as coordinator_mod
from src.server.services.runs.task_run_coordinator import SubagentRunCoordinator

pytestmark = pytest.mark.asyncio


def _finalized_row(*, applied: bool, status: str = "completed"):
    return {
        "applied": applied,
        "run": {
            "task_id": "task-1",
            "status": status,
            "parent_run_id": None,
            "failure": None,
        },
    }


async def _arrange(monkeypatch, result):
    coordinator = SubagentRunCoordinator("thread-1")
    monkeypatch.setattr(
        coordinator_mod.sr_db,
        "finalize_task_run_idempotent",
        AsyncMock(return_value=result),
    )
    monkeypatch.setattr(coordinator_mod, "_refresh_launching_turn", lambda *a, **k: None)
    monkeypatch.setattr(coordinator, "_append_v2_frame", AsyncMock())
    record = AsyncMock(return_value={"contract": {}, "event": None})
    monkeypatch.setattr(dual, "record_subagent_result", record)
    return coordinator, record


class TestCallSite:
    async def test_applied_finalize_projects_the_result(self, monkeypatch):
        coordinator, record = await _arrange(monkeypatch, _finalized_row(applied=True))
        result = await coordinator.finalize_task_run("run-1", "completed")
        assert result["applied"] is True
        record.assert_awaited_once()
        assert record.await_args.kwargs == {
            "thread_id": "thread-1",
            "task_run_id": "run-1",
            "task_id": "task-1",
            "status": "completed",
            "failure": None,
        }

    async def test_lost_finalize_cas_does_not_project(self, monkeypatch):
        coordinator, record = await _arrange(monkeypatch, _finalized_row(applied=False))
        await coordinator.finalize_task_run("run-1", "completed")
        record.assert_not_awaited()

    async def test_projection_failure_does_not_fail_the_finalize(self, monkeypatch):
        coordinator, record = await _arrange(monkeypatch, _finalized_row(applied=True))
        record.side_effect = RuntimeError("projection down")
        result = await coordinator.finalize_task_run("run-1", "completed")
        assert result["applied"] is True

    async def test_error_run_passes_failure_through(self, monkeypatch):
        row = _finalized_row(applied=True, status="error")
        row["run"]["failure"] = {"error": "boom"}
        coordinator, record = await _arrange(monkeypatch, row)
        await coordinator.finalize_task_run("run-1", "error", failure={"error": "boom"})
        assert record.await_args.kwargs["failure"] == {"error": "boom"}
        assert record.await_args.kwargs["status"] == "error"
