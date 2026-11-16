"""Unified worker registry: lifecycle, ordering and isolation by thread."""

import pytest

from src.server.services import orchestration
from src.server.services.orchestration import WorkerKind, WorkerStatus


@pytest.mark.asyncio
async def test_worker_lifecycle_is_visible_in_the_snapshot():
    worker_id = await orchestration.start_worker(
        thread_id="t-orch-1",
        kind=WorkerKind.NATIVE,
        provider="mimo",
        label="Native · mimo",
        prompt="research this",
        meta={"memory": True, "skills": True},
    )
    rows = await orchestration.snapshot("t-orch-1")
    assert rows[0]["id"] == worker_id
    assert rows[0]["meta"]["memory"] is True
    assert rows[0]["meta"]["skills"] is True
    assert rows[0]["status"] == "running"
    assert rows[0]["provider"] == "mimo"

    await orchestration.finish_worker(
        worker_id, status=WorkerStatus.COMPLETED, result="done"
    )
    rows = await orchestration.snapshot("t-orch-1")
    assert rows[0]["status"] == "completed"
    assert rows[0]["result_preview"] == "done"
    assert rows[0]["finished_at"]


@pytest.mark.asyncio
async def test_failure_and_thread_isolation():
    failed = await orchestration.start_worker(
        thread_id="t-orch-2", kind=WorkerKind.NATIVE, provider="codex"
    )
    await orchestration.finish_worker(
        failed, status=WorkerStatus.FAILED, error="boom"
    )
    await orchestration.start_worker(
        thread_id="t-orch-3", kind=WorkerKind.SUBAGENT, provider="analyst"
    )
    row = (await orchestration.snapshot("t-orch-2"))[0]
    assert row["status"] == "failed" and row["error"] == "boom"
    assert len(await orchestration.snapshot("t-orch-3")) == 1
