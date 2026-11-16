"""A cloud launch needs a Worker slot, and a refused one waits queued.

The cap is ``AUTONOMY_MAX_RUNNING_WORKERS``, shared with daemon claims. These
tests pin the launch's side of that contract with the database faked; the real
guard under concurrent claims and launches is pinned on PostgreSQL in
``tests/integration/test_cloud_worker_slots_postgres.py``.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import tickets as ticket_db
from src.server.services.autonomy import cloud_runtime, worker
from src.server.services.autonomy.effects import DeferEffect


def _ticket(**overrides):
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "status": "queued",
        "revision": 7,
        "attempts": 0,
        "worker_daemon_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "codex",
        "worker_model": "gpt-x",
        "mandate": {},
    }
    ticket.update(overrides)
    return ticket


def _effect():
    return SimpleNamespace(
        payload={"ticket_id": "ticket-1"}, effect_id="effect-1", attempts=1
    )


@pytest.fixture
def launch(monkeypatch):
    """A launch effect with the isolated runtime on; the slot pool is the variable."""
    state = SimpleNamespace(
        ticket=_ticket(),
        isolated=AsyncMock(),
        newer=AsyncMock(return_value=False),
        refuse=AsyncMock(),
        available=AsyncMock(return_value=True),
    )

    async def get_ticket(_ticket_id):
        return state.ticket

    async def allowed():
        return True

    monkeypatch.setattr(worker.ticket_db, "get_ticket", get_ticket)
    monkeypatch.setattr(cloud_runtime, "cloud_worker_launch_allowed_async", allowed)
    monkeypatch.setattr(worker, "_worker_slot_available", state.available)
    monkeypatch.setattr(worker, "_launch_isolated_cloud_worker", state.isolated)
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", state.refuse)
    monkeypatch.setattr(
        "src.server.database.autonomy.effects.has_newer_live_effect", state.newer
    )
    return state


@pytest.mark.asyncio
async def test_a_full_pool_leaves_the_ticket_queued_and_defers_the_effect(launch):
    launch.available.return_value = False

    with pytest.raises(DeferEffect) as deferred:
        await worker.launch_worker_effect(_effect())

    assert deferred.value.delay_seconds == worker._SLOT_RETRY_SECONDS
    launch.isolated.assert_not_awaited()
    launch.refuse.assert_not_awaited()
    launch.newer.assert_awaited_once_with(effect_id="effect-1")


@pytest.mark.asyncio
async def test_a_full_pool_ends_a_launch_that_a_newer_effect_already_waits_for(launch):
    """A Ticket has several launch effects; only the newest keeps waiting."""
    launch.available.return_value = False
    launch.newer.return_value = True

    await worker.launch_worker_effect(_effect())

    launch.isolated.assert_not_awaited()
    launch.refuse.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_free_slot_runs_the_launch(launch):
    await worker.launch_worker_effect(_effect())

    launch.available.assert_awaited_once()
    launch.isolated.assert_awaited_once()
    launch.newer.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_line_that_is_already_running_needs_no_new_slot(launch):
    """A legacy resume holds its slot as a running Ticket; it must not wait on one."""
    launch.ticket = _ticket(status="running", worker_thread_id="thread-1")
    launch.available.return_value = False

    await worker.launch_worker_effect(_effect())

    launch.available.assert_not_awaited()
    launch.isolated.assert_awaited_once()
    launch.newer.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_default_refusal_never_looks_at_the_slots(monkeypatch):
    """The runtime is off by default: the Ticket is refused exactly as before."""
    monkeypatch.setattr(
        cloud_runtime, "get_autonomy_cloud_worker_runtime", lambda: None
    )
    monkeypatch.setattr(
        worker.ticket_db, "get_ticket", AsyncMock(return_value=_ticket())
    )
    available = AsyncMock(return_value=False)
    monkeypatch.setattr(worker, "_worker_slot_available", available)
    refuse = AsyncMock(return_value={"ticket_id": "ticket-1"})
    monkeypatch.setattr(worker.ticket_db, "fail_unisolated_cloud_worker", refuse)

    await worker.launch_worker_effect(_effect())

    refuse.assert_awaited_once()
    available.assert_not_awaited()


def _refused_write(monkeypatch, *, newer=False):
    """An isolated launch whose publishing write is refused for want of a slot."""
    monkeypatch.setattr(worker, "_claim_effect_lease", AsyncMock(return_value=True))

    async def _idle(_effect, _lease_lost, _uncertain):
        await asyncio.sleep(3600)

    monkeypatch.setattr(worker, "_extend_effect_lease", _idle)
    monkeypatch.setattr(
        "src.server.database.autonomy.effects.has_newer_live_effect",
        AsyncMock(return_value=newer),
    )
    failed = AsyncMock()
    monkeypatch.setattr(worker, "_fail_isolated_launch", failed)
    guarded = AsyncMock()
    monkeypatch.setattr(worker, "_run_isolated_launch_guarded", guarded)
    return failed, guarded


@pytest.mark.asyncio
async def test_a_slot_refused_at_the_write_defers_instead_of_failing_the_ticket(
    monkeypatch,
):
    failed, guarded = _refused_write(monkeypatch)
    monkeypatch.setattr(
        worker,
        "_prepare_isolated_launch",
        AsyncMock(side_effect=ticket_db.WorkerSlotsFull("all 4 Worker slots are in use")),
    )

    with pytest.raises(DeferEffect):
        await worker._launch_isolated_cloud_worker(_ticket(), "ticket-1", _effect())

    failed.assert_not_awaited()
    guarded.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_write_ends_the_launch_when_a_newer_effect_waits(monkeypatch):
    failed, guarded = _refused_write(monkeypatch, newer=True)
    monkeypatch.setattr(
        worker,
        "_prepare_isolated_launch",
        AsyncMock(side_effect=ticket_db.WorkerSlotsFull("full")),
    )

    await worker._launch_isolated_cloud_worker(_ticket(), "ticket-1", _effect())

    failed.assert_not_awaited()
    guarded.assert_not_awaited()


class _Threads:
    def __init__(self):
        self.cancelled = []

    async def create_thread(self, **_kwargs):
        return "thread-new"

    async def cancel_thread(self, **kwargs):
        self.cancelled.append(kwargs["thread_id"])
        return True


def _prepare_fakes(monkeypatch, *, publish=None):
    threads = _Threads()
    monkeypatch.setattr(worker, "ProjectDispatcher", lambda: threads)
    monkeypatch.setattr(
        worker, "_cloud_worker_prompt", AsyncMock(return_value=("prompt", ""))
    )
    monkeypatch.setattr(
        worker, "_prompt_with_pending_instructions", AsyncMock(return_value=("prompt", []))
    )
    monkeypatch.setattr(worker, "_task_stopped", lambda _ticket: False)
    if publish is not None:
        monkeypatch.setattr(worker, "_publish_thread_and_queue_late_instructions", publish)
    return threads


@pytest.mark.asyncio
async def test_a_fresh_launch_takes_its_slot_in_the_publishing_write(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MAX_RUNNING_WORKERS", "3")
    publish = AsyncMock(return_value=_ticket(status="running"))
    _prepare_fakes(monkeypatch, publish=publish)

    prepared = await worker._prepare_isolated_launch(_ticket(), "ticket-1", "run-1")

    assert prepared is not None
    assert publish.await_args.kwargs["slot_limit"] == 3


@pytest.mark.asyncio
async def test_a_refused_fresh_launch_cancels_the_thread_it_created(monkeypatch):
    publish = AsyncMock(side_effect=ticket_db.WorkerSlotsFull("full"))
    threads = _prepare_fakes(monkeypatch, publish=publish)

    with pytest.raises(ticket_db.WorkerSlotsFull):
        await worker._prepare_isolated_launch(_ticket(), "ticket-1", "run-1")

    assert threads.cancelled == ["thread-new"]


class _HangingThreads(_Threads):
    """A thread service that cannot cancel: the total deadline fires, as it does
    for a slow backend, and the error is not an aiohttp client error."""

    async def cancel_thread(self, **kwargs):
        raise asyncio.TimeoutError


@pytest.mark.asyncio
async def test_a_failed_cleanup_does_not_replace_the_slot_refusal(monkeypatch):
    publish = AsyncMock(side_effect=ticket_db.WorkerSlotsFull("full"))
    _prepare_fakes(monkeypatch, publish=publish)
    monkeypatch.setattr(worker, "ProjectDispatcher", _HangingThreads)

    with pytest.raises(ticket_db.WorkerSlotsFull):
        await worker._prepare_isolated_launch(_ticket(), "ticket-1", "run-1")


@pytest.mark.asyncio
async def test_a_failed_cleanup_still_defers_instead_of_failing_the_ticket(monkeypatch):
    """The refusal is routine under contention. A slow thread service at that
    moment must not turn a Ticket that only waits for a slot into a failed one."""
    failed, _ = _refused_write(monkeypatch)
    publish = AsyncMock(side_effect=ticket_db.WorkerSlotsFull("full"))
    _prepare_fakes(monkeypatch, publish=publish)
    monkeypatch.setattr(worker, "ProjectDispatcher", _HangingThreads)

    with pytest.raises(DeferEffect):
        await worker._launch_isolated_cloud_worker(_ticket(), "ticket-1", _effect())

    failed.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_resumed_queued_line_takes_its_slot_in_the_binding_write(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MAX_RUNNING_WORKERS", "3")
    _prepare_fakes(monkeypatch)
    queued = _ticket(worker_thread_id="thread-1")
    monkeypatch.setattr(worker.ticket_db, "get_ticket", AsyncMock(return_value=queued))
    update = AsyncMock(side_effect=ticket_db.WorkerSlotsFull("full"))
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)

    with pytest.raises(ticket_db.WorkerSlotsFull):
        await worker._prepare_isolated_launch(queued, "ticket-1", "run-1")

    assert update.await_args.kwargs["slot_limit"] == 3
    assert update.await_args.kwargs["expected_status"] == "queued"


@pytest.mark.asyncio
async def test_binding_a_line_that_already_runs_takes_no_slot(monkeypatch):
    _prepare_fakes(monkeypatch)
    running = _ticket(status="running", worker_thread_id="thread-1")
    monkeypatch.setattr(worker.ticket_db, "get_ticket", AsyncMock(return_value=running))
    update = AsyncMock(return_value={**running, "worker_run_id": "run-1"})
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)

    await worker._prepare_isolated_launch(running, "ticket-1", "run-1")

    assert "slot_limit" not in update.await_args.kwargs


@pytest.mark.asyncio
async def test_publication_passes_the_limit_only_when_asked(monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lock(_ticket_id):
        yield

    update = AsyncMock(return_value=_ticket(status="running"))
    monkeypatch.setattr(worker, "_instruction_handoff_lock", _lock)
    monkeypatch.setattr(worker.ticket_db, "update_ticket", update)
    monkeypatch.setattr(worker, "_pending_instruction_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(worker, "_enqueue_instruction_deliveries", AsyncMock())

    await worker._publish_thread_and_queue_late_instructions(
        _ticket(), "ticket-1", "thread-1", set()
    )
    assert "slot_limit" not in update.await_args.kwargs

    await worker._publish_thread_and_queue_late_instructions(
        _ticket(), "ticket-1", "thread-1", set(), slot_limit=2
    )
    assert update.await_args.kwargs["slot_limit"] == 2
