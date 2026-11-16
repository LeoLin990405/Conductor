"""Worker concurrency is bounded by AUTONOMY_MAX_RUNNING_WORKERS."""

from unittest.mock import AsyncMock, patch
import asyncio
from types import SimpleNamespace

import pytest

from src.server.services.autonomy import worker


class _Cursor:
    def __init__(self, count: int):
        self.count = count

    async def execute(self, sql, params=None):
        return self

    async def fetchone(self):
        return (self.count,)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self, count: int):
        self.count = count

    def cursor(self):
        return _Cursor(self.count)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def test_max_worker_slots_defaults_to_four(monkeypatch):
    monkeypatch.delenv("AUTONOMY_MAX_RUNNING_WORKERS", raising=False)
    assert worker.max_worker_slots() == 4
    monkeypatch.setenv("AUTONOMY_MAX_RUNNING_WORKERS", "2")
    assert worker.max_worker_slots() == 2
    monkeypatch.setenv("AUTONOMY_MAX_RUNNING_WORKERS", "bad")
    assert worker.max_worker_slots() == 4


@pytest.mark.asyncio
async def test_slot_availability_compares_running_count(monkeypatch):
    monkeypatch.setenv("AUTONOMY_MAX_RUNNING_WORKERS", "2")
    with patch(
        "src.server.services.autonomy.worker.get_db_connection",
        return_value=_Conn(2),
    ):
        assert not await worker._worker_slot_available()
    with patch(
        "src.server.services.autonomy.worker.get_db_connection",
        return_value=_Conn(1),
    ):
        assert await worker._worker_slot_available()


@pytest.mark.asyncio
async def test_local_daemon_claim_obeys_the_shared_worker_slot_lock(monkeypatch):
    class Lock:
        def __init__(self, acquired):
            self.acquired = acquired

        async def __aenter__(self):
            return self.acquired

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(worker, "_launch_lock", lambda: Lock(False))
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.recover_expired_daemon_leases",
        AsyncMock(return_value=0),
    )
    available = AsyncMock(return_value=True)
    monkeypatch.setattr(worker, "_worker_slot_available", available)
    claim = AsyncMock(return_value={"ticket_id": "t1"})
    monkeypatch.setattr("src.server.database.autonomy.daemons.claim_ticket", claim)
    # This test exercises the queued path; never find a resumable Ticket.
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.resume_ticket",
        AsyncMock(return_value=None),
    )
    assert await worker.claim_daemon_ticket("d1") is None
    available.assert_not_awaited()
    claim.assert_not_awaited()

    monkeypatch.setattr(worker, "_launch_lock", lambda: Lock(True))
    monkeypatch.setattr(worker, "_worker_slot_available", AsyncMock(return_value=False))
    assert await worker.claim_daemon_ticket("d1") is None
    claim.assert_not_awaited()

    monkeypatch.setattr(worker, "_launch_lock", lambda: Lock(True))
    monkeypatch.setattr(worker, "_worker_slot_available", AsyncMock(return_value=True))
    claim.return_value = {"ticket_id": "t1"}
    assert await worker.claim_daemon_ticket("d1") == {"ticket_id": "t1"}
    claim.assert_awaited_once_with("d1", None, policy_version="")


@pytest.mark.asyncio
async def test_local_daemon_replays_a_claim_before_global_slot_check(monkeypatch):
    ticket = {
        "ticket_id": "t1",
        "lease_id": "l1",
        "inputs": [{"input_id": "i1", "kind": "answer", "text": "Proceed"}],
    }
    replay = AsyncMock(return_value=ticket)
    monkeypatch.setattr("src.server.database.autonomy.daemons.active_claim", replay)
    monkeypatch.setattr(worker, "_worker_slot_available", AsyncMock(return_value=False))
    monkeypatch.setattr(
        worker, "_launch_lock", lambda: pytest.fail("replay should not take slot lock")
    )

    assert await worker.claim_daemon_ticket("d1", "claim-1") == ticket
    replay.assert_awaited_once_with("d1", "claim-1")


@pytest.mark.asyncio
async def test_explicit_claim_id_gets_retryable_busy_when_another_claim_holds_lock(
    monkeypatch,
):
    class Lock:
        async def __aenter__(self):
            return False

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(worker, "_launch_lock", lambda: Lock())
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.active_claim",
        AsyncMock(return_value=None),
    )
    with pytest.raises(worker.DaemonClaimBusy):
        await worker.claim_daemon_ticket("d1", "claim-1")


@pytest.mark.asyncio
async def test_concurrent_daemon_claims_obey_per_daemon_and_global_caps(monkeypatch):
    lock = asyncio.Lock()
    running: list[tuple[str, str]] = []
    queued = ["t1", "t2", "t3", "t4"]
    daemon_limits = {"d1": 2, "d2": 2}
    global_limit = 2

    class Lock:
        async def __aenter__(self):
            await lock.acquire()
            return True

        async def __aexit__(self, *exc):
            lock.release()
            return False

    async def has_slot():
        available = len(running) < global_limit
        await asyncio.sleep(0)
        return available

    async def claim(daemon_id, claim_id=None, policy_version=""):
        in_use = sum(owner == daemon_id for owner, _ in running)
        if in_use >= daemon_limits[daemon_id] or not queued:
            return None
        ticket_id = queued.pop(0)
        running.append((daemon_id, ticket_id))
        return {"ticket_id": ticket_id}

    monkeypatch.setattr(worker, "_launch_lock", Lock)
    monkeypatch.setattr(worker, "_worker_slot_available", has_slot)
    recover = AsyncMock()
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.recover_expired_daemon_leases",
        recover,
    )
    monkeypatch.setattr("src.server.database.autonomy.daemons.claim_ticket", claim)
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.resume_ticket",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(worker, "max_worker_slots", lambda: global_limit)

    results = await asyncio.gather(
        *(worker.claim_daemon_ticket("d1") for _ in range(5))
    )
    assert [row["ticket_id"] for row in results if row] == ["t1", "t2"]
    assert len({ticket_id for _, ticket_id in running}) == len(running)
    assert recover.await_count == 5

    running.clear()
    queued[:] = ["t3", "t4"]
    global_limit = 2
    cross_daemon = await asyncio.gather(
        worker.claim_daemon_ticket("d1"), worker.claim_daemon_ticket("d2")
    )
    assert sum(row is not None for row in cross_daemon) == 2
    assert {owner for owner, _ in running} == {"d1", "d2"}
    assert await worker.claim_daemon_ticket("d1") is None
    assert len(running) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mandate",
    [
        {"kind": "main_agent_root", "orchestration_only": True},
        {"kind": "main_agent_child", "orchestration_child": True},
    ],
)
async def test_orchestration_tickets_never_launch_a_cloud_worker(monkeypatch, mandate):
    reject = AsyncMock()
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.get_ticket",
        AsyncMock(return_value={
            "ticket_id": "t1", "status": "queued", "revision": 1,
            "worker_daemon_id": None, "mandate": mandate,
        }),
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.tickets.fail_unisolated_cloud_worker", reject
    )

    await worker.launch_worker_effect(SimpleNamespace(payload={"ticket_id": "t1"}))

    reject.assert_not_awaited()


def test_native_speculative_prompt_keeps_observe_mode_read_only():
    prompt = worker.worker_prompt(
        {"ticket_id": "t1", "scope": "Review this"},
        {},
        native=True,
        speculative=True,
    )
    assert "Do not modify files" in prompt
    assert '"needs_review"' in prompt
    assert "AUTONOMY_STATE" not in prompt
