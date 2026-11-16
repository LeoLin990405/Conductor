"""A launch write takes its Worker slot under the guard a daemon claim uses.

The ordering (timeout, guard, count, write) and the shared key and count are the
contract. Their effect under real concurrency is pinned on PostgreSQL in
``tests/integration/test_cloud_worker_slots_postgres.py``.
"""

from contextlib import asynccontextmanager

import pytest
from psycopg.errors import LockNotAvailable

from src.server.database.autonomy import tickets
from src.server.services.autonomy import worker


class _Cursor:
    def __init__(self, rows, *, lock_timeout_on=None):
        self.rows = list(rows)
        self.executed: list[tuple[str, tuple]] = []
        self.lock_timeout_on = lock_timeout_on

    async def execute(self, sql, params=()):
        text = " ".join(sql.split())
        self.executed.append((text, tuple(params)))
        if self.lock_timeout_on and self.lock_timeout_on in text:
            raise LockNotAvailable("canceling statement due to lock timeout")

    async def fetchone(self):
        return self.rows.pop(0)


class _Connection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield self.fake_cursor

    @asynccontextmanager
    async def transaction(self):
        yield self


def _use(monkeypatch, rows, *, lock_timeout_on=None):
    cursor = _Cursor(rows, lock_timeout_on=lock_timeout_on)

    @asynccontextmanager
    async def get_connection(conn=None):
        yield _Connection(cursor)

    monkeypatch.setattr(tickets, "get_db_connection", get_connection)
    return cursor


_LAUNCH = {"expected_status": "queued", "status": "running", "worker_thread_id": "th"}


@pytest.mark.asyncio
async def test_the_write_takes_the_guard_and_counts_before_it_updates(monkeypatch):
    cursor = _use(monkeypatch, [{"running": 1}, {"ticket_id": "t1"}])

    updated = await tickets.update_ticket("t1", slot_limit=2, **_LAUNCH)

    assert updated["ticket_id"] == "t1"
    timeout, lock, count, write = cursor.executed
    # The wait is bounded and is the first thing the transaction does, so
    # waiting holds no lock of its own.
    assert timeout[0] == f"SET LOCAL lock_timeout = '{tickets._SLOT_LOCK_WAIT}'"
    assert "pg_advisory_xact_lock" in lock[0] and "try" not in lock[0]
    assert lock[1] == (tickets.WORKER_SLOT_LOCK,)
    assert count[0] == " ".join(tickets.RUNNING_WORKERS_SQL.split())
    assert write[0].startswith("UPDATE autonomy_tickets SET")
    assert "status = %s" in write[0] and "slot_limit" not in write[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting_on", ["pg_advisory_xact_lock", "UPDATE autonomy_tickets"])
async def test_a_lock_that_stays_held_refuses_the_write(monkeypatch, waiting_on):
    """Out of time waiting for the guard, or for the Ticket's row once holding
    it, is a refusal the launch defers on, not a failure that settles the Ticket."""
    _use(monkeypatch, [{"running": 1}], lock_timeout_on=waiting_on)

    with pytest.raises(tickets.WorkerSlotsFull):
        await tickets.update_ticket("t1", slot_limit=2, **_LAUNCH)


@pytest.mark.asyncio
async def test_a_lock_timeout_without_a_limit_is_not_a_refusal(monkeypatch):
    _use(monkeypatch, [], lock_timeout_on="UPDATE autonomy_tickets")

    with pytest.raises(LockNotAvailable):
        await tickets.update_ticket("t1", expected_status="queued", status="running")


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [2, 3])
async def test_a_full_pool_refuses_the_write(monkeypatch, running):
    cursor = _use(monkeypatch, [{"running": running}])

    with pytest.raises(tickets.WorkerSlotsFull):
        await tickets.update_ticket("t1", slot_limit=2, **_LAUNCH)

    assert not any(sql.startswith("UPDATE") for sql, _ in cursor.executed)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "running"},
        {"expected_status": "queued", "summary": "x"},
        {"expected_status": "running", "status": "running"},
    ],
)
async def test_the_slot_limit_only_fences_a_queued_to_running_launch(
    monkeypatch, kwargs
):
    def forbidden():
        raise AssertionError("a misuse must not open the database")

    monkeypatch.setattr(tickets, "get_db_connection", forbidden)

    with pytest.raises(ValueError):
        await tickets.update_ticket("t1", slot_limit=2, **kwargs)


@pytest.mark.asyncio
async def test_a_write_without_a_limit_never_touches_the_guard(monkeypatch):
    cursor = _use(monkeypatch, [{"ticket_id": "t1"}])

    await tickets.update_ticket("t1", expected_status="queued", status="running")

    assert len(cursor.executed) == 1
    assert "advisory" not in cursor.executed[0][0]


@pytest.mark.asyncio
async def test_a_daemon_claim_and_a_launch_use_one_key_and_one_count(monkeypatch):
    cursor = _use(monkeypatch, [(True,), (0,)])

    @asynccontextmanager
    async def get_connection(conn=None):
        yield _Connection(cursor)

    monkeypatch.setattr(worker, "get_db_connection", get_connection)
    async with worker._launch_lock() as acquired:
        assert acquired
        await worker._worker_slot_available()

    lock, count, unlock = cursor.executed
    assert lock[1] == (tickets.WORKER_SLOT_LOCK,)
    assert count[0] == " ".join(tickets.RUNNING_WORKERS_SQL.split())
    assert unlock[1] == (tickets.WORKER_SLOT_LOCK,)
