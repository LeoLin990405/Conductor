from __future__ import annotations

import importlib.util
import re
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.server.database.refactor import memory_consolidation as database


@pytest.fixture(autouse=True)
def _noop_owner_lock(monkeypatch):
    """record_page holds the owner memory lock; its connection is not this fake."""

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lock(_user_id):
        yield

    monkeypatch.setattr(
        "src.server.database.refactor.memory_revisions.owner_memory_lock", _lock
    )


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.queries = []
        self.rowcount = 0

    async def execute(self, query, params=()):
        self.queries.append((str(query), params))

    async def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    async def fetchall(self):
        return self.rows


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor

    @asynccontextmanager
    async def transaction(self):
        yield

    @asynccontextmanager
    async def cursor(self, **kwargs):
        yield self.fake_cursor


@asynccontextmanager
async def _connection(connection):
    yield connection


@pytest.mark.asyncio
async def test_daily_due_date_uses_database_utc_cutoff(monkeypatch):
    cursor = FakeCursor(rows=[(date(2026, 11, 3),)])
    connection = FakeConnection(cursor)
    monkeypatch.setattr(database.pool, "get_db_connection", lambda: _connection(connection))

    result = await database.due_run_date()

    query, params = cursor.queries[0]
    assert result == date(2026, 11, 3)
    assert "timezone('UTC', clock_timestamp())" in query
    assert "value::time >= %s::time" in query
    assert params == ("03:00:00",)


@pytest.mark.asyncio
async def test_daily_claim_is_canonical_owner_bound_and_attempt_fenced(monkeypatch):
    cursor = FakeCursor(
        rows=[
            {
                "run_id": "00000000-0000-0000-0000-000000000001",
                "user_id": "00000000-0000-0000-0000-000000000002",
                "raw_user_id": "raw-owner",
                "run_date": date(2026, 11, 3),
                "status": "pending",
                "cursor_key": "",
                "groups_processed": 0,
                "proposals_created": 0,
                "attempts": 0,
            },
            {
                "run_id": "00000000-0000-0000-0000-000000000001",
                "user_id": "00000000-0000-0000-0000-000000000002",
                "raw_user_id": "raw-owner",
                "run_date": date(2026, 11, 3),
                "status": "running",
                "cursor_key": "",
                "groups_processed": 0,
                "proposals_created": 0,
                "attempts": 1,
            },
        ]
    )
    connection = FakeConnection(cursor)
    monkeypatch.setattr(database.pool, "get_db_connection", lambda: _connection(connection))

    result = await database.claim_daily_run(
        user_id="00000000-0000-0000-0000-000000000002",
        run_date=date(2026, 11, 3),
    )

    assert result["attempts"] == 1
    assert "ON CONFLICT (user_id, run_date, policy_version) DO NOTHING" in cursor.queries[0][0]
    assert "FOR UPDATE" in cursor.queries[1][0]
    assert "lease_until <= clock_timestamp()" in cursor.queries[2][0]
    assert "retry_after <= clock_timestamp()" in cursor.queries[2][0]


@pytest.mark.asyncio
async def test_stale_attempt_cannot_insert_or_advance_a_page(monkeypatch):
    cursor = FakeCursor(rows=[None])
    connection = FakeConnection(cursor)
    monkeypatch.setattr(database.pool, "get_db_connection", lambda: _connection(connection))

    result = await database.record_page(
        run_id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        attempt=1,
        expected_cursor="",
        next_cursor="after-group",
        groups_processed=1,
        proposals=({"source_entry_ids": ["00000000-0000-0000-0000-000000000003"]},),
        complete=False,
    )

    assert result is None
    assert len(cursor.queries) == 1
    assert "attempts = %s" in cursor.queries[0][0]
    assert "cursor_key = %s" in cursor.queries[0][0]


@pytest.mark.asyncio
async def test_proposal_insert_targets_partial_unique_index(monkeypatch):
    migration_path = (
        Path(__file__).resolve().parents[5]
        / "migrations/versions/137_memory_consolidation.py"
    )
    spec = importlib.util.spec_from_file_location("migration_133", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration_op = MagicMock()
    monkeypatch.setattr(migration, "op", migration_op)
    migration.upgrade()
    migration_sql = " ".join(
        " ".join(call.args[0].split())
        for call in migration_op.execute.call_args_list
    )
    migration_index = re.search(
        r"CREATE UNIQUE INDEX uq_memory_consolidation_proposal_identity "
        r"ON agent_memory_proposals \(([^)]*)\)\s+WHERE\s+(.+?)"
        r"(?=CREATE TABLE|$)",
        migration_sql,
    )
    assert migration_index is not None
    expected_columns = tuple(
        column.strip() for column in migration_index.group(1).split(",")
    )
    expected_predicate = " ".join(migration_index.group(2).split())

    class ProposalCursor(FakeCursor):
        def __init__(self):
            super().__init__(rows=[(1,), (1,)])
            self.insert_rowcounts = iter((1, 0))
            self.last_query = ""

        async def execute(self, query, params=()):
            self.last_query = str(query)
            await super().execute(query, params)
            if "INSERT INTO agent_memory_proposals" not in query:
                return
            normalized = " ".join(query.split())
            target = re.search(
                r"ON CONFLICT\s*\(([^)]*)\)\s*WHERE\s*(.*?)\s*DO NOTHING",
                normalized,
            )
            assert target is not None
            actual_columns = tuple(
                column.strip() for column in target.group(1).split(",")
            )
            actual_predicate = " ".join(target.group(2).split())
            assert actual_columns == expected_columns
            assert actual_predicate == expected_predicate
            self.rowcount = next(self.insert_rowcounts)

        async def fetchone(self):
            if "FROM memory_forget_tombstones" in self.last_query:
                return None
            return await super().fetchone()

    cursor = ProposalCursor()
    connection = FakeConnection(cursor)
    monkeypatch.setattr(database.pool, "get_db_connection", lambda: _connection(connection))

    proposal = {
        "source_entry_ids": [
            "00000000-0000-0000-0000-000000000003",
            "00000000-0000-0000-0000-000000000004",
        ],
        "text": "duplicate body",
        "text_hash": "body-hash",
    }
    common = {
        "run_id": "00000000-0000-0000-0000-000000000001",
        "user_id": "00000000-0000-0000-0000-000000000002",
        "attempt": 1,
        "groups_processed": 1,
        "proposals": (proposal,),
        "complete": False,
    }
    first = await database.record_page(
        **common, expected_cursor="", next_cursor="duplicate-group"
    )
    retry = await database.record_page(
        **common, expected_cursor="duplicate-group", next_cursor="next-group"
    )

    updates = [
        params
        for query, params in cursor.queries
        if "UPDATE memory_consolidation_runs" in query
    ]
    assert first == 1
    assert retry == 0
    assert [params[0] for params in updates] == ["duplicate-group", "next-group"]
    assert [params[1] for params in updates] == [1, 1]
    assert [params[2] for params in updates] == [1, 0]


@pytest.mark.asyncio
async def test_record_page_skips_a_proposal_whose_source_was_forgotten(monkeypatch):
    """The forget fence: a source tombstoned after the group was computed must
    never publish its copied body as a producer proposal."""

    class FencedCursor(FakeCursor):
        def __init__(self):
            super().__init__(rows=[(1,)])
            self.last_query = ""

        async def execute(self, query, params=()):
            self.last_query = str(query)
            await super().execute(query, params)

        async def fetchone(self):
            if "FROM memory_forget_tombstones" in self.last_query:
                return (1,)
            return await super().fetchone()

    cursor = FencedCursor()
    connection = FakeConnection(cursor)
    monkeypatch.setattr(database.pool, "get_db_connection", lambda: _connection(connection))

    created = await database.record_page(
        run_id="00000000-0000-0000-0000-000000000001",
        user_id="00000000-0000-0000-0000-000000000002",
        attempt=1,
        expected_cursor="",
        next_cursor="after-group",
        groups_processed=1,
        proposals=(
            {
                "source_entry_ids": ["00000000-0000-0000-0000-000000000003"],
                "text": "forgotten body",
                "text_hash": "body-hash",
            },
        ),
        complete=False,
    )

    assert created == 0
    assert not any(
        "INSERT INTO agent_memory_proposals" in query for query, _ in cursor.queries
    )


@pytest.mark.asyncio
async def test_candidate_query_suppresses_only_the_same_source_version(monkeypatch):
    cursor = FakeCursor()
    connection = FakeConnection(cursor)
    monkeypatch.setattr(database.pool, "get_db_connection", lambda: _connection(connection))

    await database.list_candidate_groups(
        user_id="00000000-0000-0000-0000-000000000002",
        after_cursor="cursor",
        limit=5,
    )

    query = cursor.queries[0][0]
    assert "p.source_entry_ids = candidate_groups.source_ids" in query
    assert "p.producer_metadata->>'source_signature'" in query
    assert "p.status IN ('proposed', 'approved', 'rejected')" in query
    assert "m.user_id = %s" in query
    assert "m.tier = 'user'" in query
    assert "m.metadata->>'kind', '') <> 'constraint'" in query
    assert "NOT jsonb_exists(m.metadata::jsonb, 'denied_tools')" in query
    assert "FROM memory_tool_denials d" in query
    assert "d.entry_id = m.entry_id" in query
    assert "d.active IS TRUE" in query
    # PostgreSQL has no min(uuid); the cast has to happen inside the aggregate.
    assert "min(entry_id::text) AS first_entry_id" in query
    assert "min(entry_id)::text" not in query
    assert cursor.queries[0][1][-1] == 5


@pytest.mark.asyncio
async def test_manual_approval_source_lookup_is_bounded_and_owner_scoped(monkeypatch):
    cursor = FakeCursor(rows=[])
    connection = FakeConnection(cursor)
    monkeypatch.setattr(database.pool, "get_db_connection", lambda: _connection(connection))
    source_ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]

    rows = await database.get_active_source_entries(
        user_id="00000000-0000-0000-0000-000000000003",
        source_entry_ids=source_ids,
    )

    query, params = cursor.queries[0]
    assert rows == []
    assert "m.user_id = %s" in query
    assert "m.status = 'active'" in query
    assert "m.entry_id = ANY(%s::uuid[])" in query
    assert "memory_forget_tombstones" in query
    assert params == (
        "00000000-0000-0000-0000-000000000003",
        source_ids,
    )
