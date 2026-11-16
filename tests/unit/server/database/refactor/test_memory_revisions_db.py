from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.server.database.refactor import memory_revisions

USER = "00000000-0000-0000-0000-000000000001"
THREAD = "00000000-0000-0000-0000-000000000010"
RESPONSE = "00000000-0000-0000-0000-000000000011"
TASK_RUN = "00000000-0000-0000-0000-000000000012"
POOL_ROW = (None,)


class FakeCursor:
    def __init__(self, row):
        self.row = row
        self.sql = None
        self.params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, sql, params):
        self.sql = sql
        self.params = params

    async def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.transaction_started = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def cursor(self, **_kwargs):
        return self.fake_cursor

    @asynccontextmanager
    async def transaction(self):
        self.transaction_started = True
        yield


@pytest.mark.asyncio
async def test_revision_lookup_is_owner_scoped_and_uses_metadata_json(monkeypatch):
    cursor = FakeCursor({"entry_id": RESPONSE, "metadata": {"lifecycle": "active"}})
    connection = FakeConnection(cursor)
    monkeypatch.setattr(memory_revisions.pool, "get_db_connection", lambda: connection)

    result = await memory_revisions.get_revision(user_id=USER, entry_id=RESPONSE)

    assert result["entry_id"] == RESPONSE
    assert "valid_from" not in cursor.sql
    assert "valid_until" not in cursor.sql
    assert "user_id = %s AND entry_id = %s" in cursor.sql
    assert cursor.params == (USER, RESPONSE)


@pytest.mark.asyncio
async def test_source_slice_lookup_binds_thread_and_response(monkeypatch):
    cursor = FakeCursor(
        {
            "thread_id": THREAD,
            "response_id": RESPONSE,
            "slice_codec": "codec",
            "slice": bytearray(b"slice"),
        }
    )
    connection = FakeConnection(cursor)
    monkeypatch.setattr(memory_revisions.pool, "get_db_connection", lambda: connection)

    result = await memory_revisions.get_source_turn_slice(
        thread_id=THREAD, response_id=RESPONSE
    )

    assert result["slice"] == b"slice"
    assert "conversation_thread_id = %s" in cursor.sql
    assert "conversation_response_id = %s" in cursor.sql
    assert cursor.params == (THREAD, RESPONSE)


@pytest.mark.asyncio
async def test_task_run_maps_to_parent_response_without_id_relabeling(monkeypatch):
    cursor = FakeCursor((RESPONSE,))
    connection = FakeConnection(cursor)
    monkeypatch.setattr(memory_revisions.pool, "get_db_connection", lambda: connection)

    result = await memory_revisions.get_parent_response_id(
        thread_id=THREAD, task_run_id=TASK_RUN
    )

    assert result == RESPONSE
    assert "parent_run_id::text" in cursor.sql
    assert "thread_id = %s AND task_run_id = %s" in cursor.sql
    assert cursor.params == (THREAD, TASK_RUN)


@pytest.mark.asyncio
async def test_owner_lock_does_not_consume_the_shared_pool(monkeypatch):
    """A one-slot pool must be able to serve the work under the owner lock."""
    cursor = FakeCursor((None,))
    connection = FakeConnection(cursor)

    @asynccontextmanager
    async def _dedicated():
        yield connection

    monkeypatch.setattr(memory_revisions.pool, "get_dedicated_connection", _dedicated)

    pool_connection = FakeConnection(FakeCursor(POOL_ROW))
    in_use = 0

    @asynccontextmanager
    async def _single_slot_pool():
        nonlocal in_use
        in_use += 1
        assert in_use == 1, "the owner lock must not hold a shared-pool slot"
        try:
            yield pool_connection
        finally:
            in_use -= 1

    monkeypatch.setattr(memory_revisions.pool, "get_db_connection", _single_slot_pool)

    async with memory_revisions.owner_memory_lock(USER):
        async with memory_revisions.pool.get_db_connection() as conn:
            assert conn is pool_connection


@pytest.mark.asyncio
async def test_owner_lock_uses_a_dedicated_connection_and_manual_namespace(monkeypatch):
    class LockCursor(FakeCursor):
        def __init__(self):
            super().__init__((None,))
            self.sqls = []

        async def execute(self, sql, params):
            self.sqls.append((str(sql), params))
            await super().execute(sql, params)

    cursor = LockCursor()
    connection = FakeConnection(cursor)

    @asynccontextmanager
    async def _dedicated():
        yield connection

    monkeypatch.setattr(
        memory_revisions.pool, "get_dedicated_connection", _dedicated
    )

    async with memory_revisions.owner_memory_lock(USER):
        # Session-level lock on a connection outside the shared pool.
        assert "pg_advisory_lock" in cursor.sql
        assert "pg_advisory_xact_lock" not in cursor.sql

    assert [sql.split("(")[0].strip() for sql, _ in cursor.sqls] == [
        "SELECT pg_advisory_lock",
        "SELECT pg_advisory_unlock",
    ]
    assert cursor.sqls[0][1] == (f"conductor:memory-approval:{USER}",)
