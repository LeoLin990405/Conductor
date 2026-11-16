"""Missed notifications must stop real async execution, without changing its identity."""

import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.server.database.runs import lifecycle as run_db
from src.server.services.runs.task_registry_store import BackgroundRegistryStore
from src.server.services.runs import cancel
from src.server.services.runs.executor import LocalRunExecution, LocalRunExecutor, LocalRunStatus


@pytest.fixture
def local(monkeypatch):
    manager = LocalRunExecutor()
    manager.enable_storage = False
    manager._finalize_run = AsyncMock()
    manager._flush_checkpoint = AsyncMock()
    manager._teardown_subagents_on_stop = AsyncMock()
    monkeypatch.setattr(LocalRunExecutor, "get_instance", lambda: manager)
    store = BackgroundRegistryStore()
    monkeypatch.setattr(BackgroundRegistryStore, "get_instance", lambda: store)
    monkeypatch.setattr(cancel, "_RECONCILE_INTERVAL_S", 0.02)
    monkeypatch.setattr(cancel, "_RECONCILE_FAILURE_BACKOFF_S", 0.15)
    from src.server.services import workspace_status_pubsub
    monkeypatch.setattr(workspace_status_pubsub, "get_shared_pubsub_client", AsyncMock(return_value=None))
    return SimpleNamespace(manager=manager, store=store, listener=cancel.TurnCancelListener())


def _run(local, *, thread_id=None, run_id=None):
    info = LocalRunExecution(
        thread_id=thread_id or str(uuid4()), run_id=run_id or str(uuid4()),
        status=LocalRunStatus.RUNNING, created_at=datetime.now(),
    )
    local.manager.executions[(info.thread_id, info.run_id)] = info
    return info


@pytest.mark.asyncio
async def test_lost_pubsub_message_interrupts_real_waiting_workflow(local, monkeypatch):
    info = _run(local)
    started, unwound = asyncio.Event(), asyncio.Event()
    durable = {}
    effects = []

    async def workflow():
        started.set()
        try:
            await asyncio.Event().wait()
            effects.append("must not execute after stop")
            yield "unreachable"
        finally:
            unwound.set()

    async def read_intents(ids):
        return {run_id: thread for run_id, thread in durable.items() if run_id in ids}

    monkeypatch.setattr(run_db, "get_run_cancel_intents", read_intents)
    info.task = asyncio.create_task(local.manager._run_workflow(
        info.thread_id, info.run_id, workflow(), info.cancel_event,
    ))
    await asyncio.wait_for(started.wait(), 1)
    local.listener.start()
    try:
        # Only the durable row changes: no _handle() or local cancel is called.
        durable[info.run_id] = info.thread_id
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(info.task, 1)
        assert unwound.is_set()
        assert effects == []
        assert info.explicit_cancel and info.user_stop
        local.manager._finalize_run.assert_awaited_once_with(
            info.thread_id, info.run_id, kind="cancelled",
        )
    finally:
        await local.listener.stop()
        if not info.task.done():
            info.task.cancel()
            await asyncio.gather(info.task, return_exceptions=True)


@pytest.mark.asyncio
async def test_result_for_removed_run_never_cancels_its_replacement(local, monkeypatch):
    old = _run(local)
    newer = _run(local, thread_id=old.thread_id)

    async def read_intents(ids):
        del local.manager.executions[(old.thread_id, old.run_id)]
        return {old.run_id: old.thread_id}

    monkeypatch.setattr(run_db, "get_run_cancel_intents", read_intents)
    await local.listener._reconcile_local_cancels()
    assert not newer.cancel_event.is_set()


@pytest.mark.asyncio
async def test_reconciliation_batches_rotate_and_ignore_wrong_thread(local, monkeypatch):
    infos = [_run(local) for _ in range(5)]
    monkeypatch.setattr(run_db, "CANCEL_INTENT_BATCH_SIZE", 2)
    batches = []

    async def read_intents(ids):
        batches.append(ids)
        return {ids[0]: "other-thread"}

    monkeypatch.setattr(run_db, "get_run_cancel_intents", read_intents)
    for _ in range(3):
        await local.listener._reconcile_local_cancels()
    assert all(len(batch) == 2 for batch in batches)
    assert set().union(*map(set, batches)) == {info.run_id for info in infos}
    assert not any(info.cancel_event.is_set() for info in infos)


@pytest.mark.asyncio
async def test_failure_backs_off_and_stop_cancels_inflight_query(local, monkeypatch):
    _run(local)
    failed = asyncio.Event()

    async def unavailable(ids):
        failed.set()
        raise ConnectionError("database unavailable")

    read = AsyncMock(side_effect=unavailable)
    monkeypatch.setattr(run_db, "get_run_cancel_intents", read)
    local.listener.start()
    await asyncio.wait_for(failed.wait(), 1)
    for _ in range(3):
        local.listener._reconcile_requested.set()
        await asyncio.sleep(0.01)
    assert read.await_count == 1
    await local.listener.stop()

    entered, closed = asyncio.Event(), asyncio.Event()

    async def stalled(ids):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(run_db, "get_run_cancel_intents", stalled)
    local.listener.start()
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(local.listener.stop(), 1)
    assert closed.is_set()
    assert local.listener._task is None and local.listener._reconcile_task is None


@pytest.mark.asyncio
async def test_reconnect_rechecks_before_periodic_deadline(local, monkeypatch):
    _run(local)
    read = AsyncMock(return_value={})
    monkeypatch.setattr(run_db, "get_run_cancel_intents", read)
    monkeypatch.setattr(cancel, "_RECONCILE_INTERVAL_S", 60)
    local.listener.start()
    try:
        async with asyncio.timeout(1):
            while read.await_count == 0:
                await asyncio.sleep(0)
        local.listener._reconcile_requested.set()
        async with asyncio.timeout(1):
            while read.await_count < 2:
                await asyncio.sleep(0)
    finally:
        await local.listener.stop()


@pytest.mark.asyncio
async def test_poll_timeout_cancels_query_then_backs_off(local, monkeypatch):
    _run(local)
    closed = asyncio.Event()

    async def stalled(ids):
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    read = AsyncMock(side_effect=stalled)
    monkeypatch.setattr(run_db, "get_run_cancel_intents", read)
    monkeypatch.setattr(cancel, "_RECONCILE_TIMEOUT_S", 0.01)
    local.listener.start()
    try:
        await asyncio.wait_for(closed.wait(), 1)
        local.listener._reconcile_requested.set()
        await asyncio.sleep(0.03)
        assert read.await_count == 1
    finally:
        await local.listener.stop()


@pytest.mark.asyncio
async def test_task_durable_stop_reaches_only_current_round(local, monkeypatch):
    from src.server.database.runs import subagent_runs

    registry = await local.store.get_or_create_registry(str(uuid4()))
    task = await registry.register("tool-call", "task", "prompt", "general-purpose")
    task.task_run_id = str(uuid4())
    old_run = task.task_run_id
    task.task_run_id = str(uuid4())
    assert not await local.store.apply_cancel_intent(registry.thread_id, task.task_id, old_run)
    assert not task.cancelled
    monkeypatch.setattr(subagent_runs, "get_task_run_cancel_intents", AsyncMock(return_value={
        task.task_run_id: (registry.thread_id, task.task_id),
    }))
    await local.listener._reconcile_local_cancels()
    assert task.cancelled
