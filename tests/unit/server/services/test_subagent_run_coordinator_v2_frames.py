"""M6-A v2 write contract (STREAM_CONTRACT_V2.md).

Active per-run streams carry no TTL; the attach-grace clock starts only at
a terminal append. lane_open is fail-closed — an anchorless stream must not
start — and run_end is deferred past the wrapper's steering sweep via
``defer_run_end`` + ``append_run_end`` (idempotent by last-frame read).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ptc_agent.agent.middleware.task_runtime.registry import TaskRunRejected
from src.server.services.runs.task_run_coordinator import SubagentRunCoordinator
from tests.unit.redis_mock_pipeline import attach_pipeline

COORD_SRDB = "src.server.services.runs.task_run_coordinator.sr_db"
CACHE = "src.utils.cache.redis_cache.get_cache_client"
TTL = "src.config.settings.get_redis_ttl_workflow_events"


def _cache(*, enabled: bool = True, xadd_fails: bool = False, tail=None):
    cache = MagicMock()
    cache.enabled = enabled
    cache.client = MagicMock()
    cache.client.xadd = AsyncMock(
        side_effect=RuntimeError("redis down") if xadd_fails else None,
        return_value=b"1-0",
    )
    cache.client.expire = AsyncMock(return_value=True)
    cache.client.xrevrange = AsyncMock(return_value=tail or [])
    # The control-lane announce batches xadd+expire into one pipeline; the
    # shim replays them onto these mocks so assertions stay on the commands.
    attach_pipeline(cache.client)
    return cache


def _row(**overrides):
    return {"task_run_id": "run-1", "task_id": "abc123", "status": "completed",
            **overrides}


@pytest.mark.asyncio
async def test_lane_open_failure_refuses_the_admission_and_settles_the_row(
    monkeypatch,
):
    """Fail closed: a run whose anchor can't be written must not spawn, and
    the just-born row is settled here, not stranded for the scanner."""
    start = AsyncMock(return_value=_row())
    settle = AsyncMock(return_value={"applied": True, "run": _row(status="error")})
    monkeypatch.setattr(f"{COORD_SRDB}.start_task_run", start)
    monkeypatch.setattr(f"{COORD_SRDB}.finalize_task_run_idempotent", settle)
    monkeypatch.setattr(CACHE, lambda: _cache(xadd_fails=True))

    with pytest.raises(TaskRunRejected):
        await SubagentRunCoordinator("t-1").start_task_run(
            task_id="abc123", cause="init"
        )

    settle.assert_awaited_once()
    kwargs = settle.await_args.kwargs
    assert kwargs["status"] == "error"
    assert kwargs["failure"]["error_type"] == "transport_lost"


@pytest.mark.asyncio
async def test_lane_open_carries_no_ttl(monkeypatch):
    """Active v2 streams never expire — retention starts at terminal."""
    cache = _cache()
    monkeypatch.setattr(f"{COORD_SRDB}.start_task_run", AsyncMock(return_value=_row()))
    monkeypatch.setattr(CACHE, lambda: cache)

    run_id = await SubagentRunCoordinator("t-1").start_task_run(
        task_id="abc123", cause="init"
    )

    assert run_id == "run-1"
    cache.client.xadd.assert_awaited_once()
    cache.client.expire.assert_not_awaited()


@pytest.mark.asyncio
async def test_lane_open_skipped_without_cache_admission_proceeds(monkeypatch):
    """A no-Redis deployment has no stream transport contract at all."""
    monkeypatch.setattr(f"{COORD_SRDB}.start_task_run", AsyncMock(return_value=_row()))
    monkeypatch.setattr(CACHE, lambda: _cache(enabled=False))

    run_id = await SubagentRunCoordinator("t-1").start_task_run(
        task_id="abc123", cause="init"
    )
    assert run_id == "run-1"


@pytest.mark.asyncio
async def test_finalize_appends_run_end_and_starts_the_attach_grace_ttl(
    monkeypatch,
):
    """Recovery paths keep the immediate append — one terminal frame plus
    the retention stamp, in that order."""
    cache = _cache()
    monkeypatch.setattr(
        f"{COORD_SRDB}.finalize_task_run_idempotent",
        AsyncMock(return_value={"applied": True, "run": _row(status="error")}),
    )
    monkeypatch.setattr(CACHE, lambda: cache)
    monkeypatch.setattr(TTL, lambda: 86400)

    await SubagentRunCoordinator("t-1").finalize_task_run("run-1", "error")

    (key, fields), _ = cache.client.xadd.await_args
    assert key == "subagent:stream:t-1:run-1"
    assert fields[b"type"] == b"run_end"
    cache.client.expire.assert_awaited_once_with(
        "subagent:stream:t-1:run-1", 86400, nx=True
    )


@pytest.mark.asyncio
async def test_finalize_defer_run_end_appends_nothing(monkeypatch):
    """The wrapper's path: the CAS lands but run_end waits for the sweep."""
    cache = _cache()
    monkeypatch.setattr(
        f"{COORD_SRDB}.finalize_task_run_idempotent",
        AsyncMock(return_value={"applied": True, "run": _row()}),
    )
    monkeypatch.setattr(CACHE, lambda: cache)

    await SubagentRunCoordinator("t-1").finalize_task_run(
        "run-1", "completed", defer_run_end=True
    )

    cache.client.xadd.assert_not_awaited()
    cache.client.expire.assert_not_awaited()


@pytest.mark.asyncio
async def test_append_run_end_is_idempotent_by_last_frame(monkeypatch):
    """Racing a recovery finalizer must not double the terminal frame."""
    cache = _cache(tail=[(b"9-0", {b"type": b"run_end"})])
    monkeypatch.setattr(CACHE, lambda: cache)

    await SubagentRunCoordinator("t-1").append_run_end(
        "run-1", task_id="abc123", outcome="completed"
    )

    cache.client.xadd.assert_not_awaited()


@pytest.mark.asyncio
async def test_append_run_end_appends_after_content_and_stamps(monkeypatch):
    cache = _cache(tail=[(b"9-0", {b"type": b"message_chunk"})])
    monkeypatch.setattr(CACHE, lambda: cache)
    monkeypatch.setattr(TTL, lambda: 86400)

    await SubagentRunCoordinator("t-1").append_run_end(
        "run-1", task_id="abc123", outcome="completed"
    )

    (key, fields), _ = cache.client.xadd.await_args
    assert key == "subagent:stream:t-1:run-1"
    assert fields[b"type"] == b"run_end"
    assert b'"outcome": "completed"' in fields[b"payload"]
    cache.client.expire.assert_awaited_once_with(
        "subagent:stream:t-1:run-1", 86400, nx=True
    )


# ---------------------------------------------------------------------------
# M6-B: control-lane discovery announcements
# ---------------------------------------------------------------------------









@pytest.mark.asyncio
async def test_lane_open_failure_marks_its_own_result_delivered(monkeypatch):
    """The rejection raised here becomes the launching call's own reply, and
    a reply that hands the agent a run's terminal fate IS the delivery.
    Unstamped, the report-back executor still thinks a notification is owed
    and opens a synthetic turn to re-announce a failure the agent already has.
    """
    monkeypatch.setattr(f"{COORD_SRDB}.start_task_run", AsyncMock(return_value=_row()))
    monkeypatch.setattr(
        f"{COORD_SRDB}.finalize_task_run_idempotent",
        AsyncMock(return_value={"applied": True, "run": _row(status="error")}),
    )
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr(f"{COORD_SRDB}.mark_result_delivered", delivered)
    monkeypatch.setattr(CACHE, lambda: _cache(xadd_fails=True))

    with pytest.raises(TaskRunRejected):
        await SubagentRunCoordinator("t-1").start_task_run(
            task_id="abc123", cause="init"
        )

    delivered.assert_awaited_once()


@pytest.mark.asyncio
async def test_an_unsettled_row_is_not_marked_delivered(monkeypatch):
    """No settle, no delivery to claim: the scanner still owes this row a
    terminal status, and stamping one it never reached would strand it.
    """
    monkeypatch.setattr(f"{COORD_SRDB}.start_task_run", AsyncMock(return_value=_row()))
    monkeypatch.setattr(
        f"{COORD_SRDB}.finalize_task_run_idempotent",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr(f"{COORD_SRDB}.mark_result_delivered", delivered)
    monkeypatch.setattr(CACHE, lambda: _cache(xadd_fails=True))

    with pytest.raises(TaskRunRejected):
        await SubagentRunCoordinator("t-1").start_task_run(
            task_id="abc123", cause="init"
        )

    delivered.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_delivery_stamp_does_not_swallow_the_rejection(monkeypatch):
    """A missed stamp costs one redundant notification. Letting it replace the
    refusal would cost the launch failure its own reply, which is worse.
    """
    monkeypatch.setattr(f"{COORD_SRDB}.start_task_run", AsyncMock(return_value=_row()))
    monkeypatch.setattr(
        f"{COORD_SRDB}.finalize_task_run_idempotent",
        AsyncMock(return_value={"applied": True, "run": _row(status="error")}),
    )
    monkeypatch.setattr(
        f"{COORD_SRDB}.mark_result_delivered",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    monkeypatch.setattr(CACHE, lambda: _cache(xadd_fails=True))

    with pytest.raises(TaskRunRejected):
        await SubagentRunCoordinator("t-1").start_task_run(
            task_id="abc123", cause="init"
        )


@pytest.mark.asyncio
async def test_finalize_confirms_checkpointed_task_holds_before_its_cas(monkeypatch):
    """The CAS's settle would take a row the run held, whose message its
    checkpoint already carries, as unread."""
    order = []

    async def confirm(thread_id, task_id, task_run_id):
        order.append(("confirm", thread_id, task_id, task_run_id))
        return []

    async def finalize(**kwargs):
        order.append("finalize")
        return {"applied": False, "run": _row(status="cancelled")}

    monkeypatch.setattr(
        "src.server.services.runs.reports.steer.confirm_checkpointed_task_holds",
        confirm,
    )
    monkeypatch.setattr(f"{COORD_SRDB}.finalize_task_run_idempotent", finalize)
    coordinator = SubagentRunCoordinator("t-1")
    monkeypatch.setattr(
        coordinator, "_read_task_checkpoint_tip", AsyncMock(return_value=None)
    )

    await coordinator.finalize_task_run("run-1", "cancelled", task_id="abc123")

    assert order == [("confirm", "t-1", "abc123", "run-1"), "finalize"]
