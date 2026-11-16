"""Database contracts for bounded, owner-scoped memory forgetting."""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from src.server.database.refactor import memory as db_memory

pytestmark = pytest.mark.asyncio

OWNER = "11111111-1111-4111-8111-111111111111"
NS = (OWNER, "memory")
ENTRY = "22222222-2222-4222-8222-222222222222"
ENTRY_IDENTITY = "identity-a"
ENTRY_HASH = "sha256:entry-a"


class FakeCursor:
    def __init__(
        self, *, rows=(), proposal_rows=(), rowcount=0,
        proposal_delete_rowcount=None,
    ):
        self.rows = list(rows)
        self.proposal_rows = list(proposal_rows)
        self.rowcount = rowcount
        self.proposal_delete_rowcount = proposal_delete_rowcount
        self.executed: list[tuple[str, tuple | None]] = []
        self.executemany_calls: list[tuple[str, list[tuple]]] = []

    async def execute(self, query, params=None):
        self.executed.append((str(query), params))
        if str(query).lstrip().startswith("DELETE FROM memory_entries"):
            self.rowcount = len(self.rows)
        elif str(query).lstrip().startswith("DELETE FROM agent_memory_proposals"):
            self.rowcount = (
                len(self.proposal_rows)
                if self.proposal_delete_rowcount is None
                else self.proposal_delete_rowcount
            )
            if self.rowcount == len(self.proposal_rows):
                self.proposal_rows.clear()

    async def executemany(self, query, params):
        values = list(params)
        self.executemany_calls.append((str(query), values))
        self.rowcount = len(values)

    async def fetchall(self):
        if self.executed and "FROM agent_memory_proposals" in self.executed[-1][0]:
            return list(self.proposal_rows)
        return list(self.rows)

    async def fetchone(self):
        if self.executed and "FROM agent_memory_proposals" in self.executed[-1][0]:
            return self.proposal_rows[0] if self.proposal_rows else None
        return self.rows[0] if self.rows else None


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.transaction_entered = 0
        self.transaction_exit = None

    @asynccontextmanager
    async def cursor(self, **_kwargs):
        yield self.fake_cursor

    @asynccontextmanager
    async def transaction(self):
        self.transaction_entered += 1
        try:
            yield self.fake_cursor
        except BaseException as exc:
            self.transaction_exit = type(exc)
            raise
        else:
            self.transaction_exit = None


@pytest.fixture
def fake_db(monkeypatch):
    state = {}

    @asynccontextmanager
    async def connection():
        yield state["connection"]

    monkeypatch.setattr(db_memory.pool, "get_db_connection", connection)

    def install(cursor):
        state["connection"] = FakeConnection(cursor)
        return state["connection"]

    return install


def _row(**overrides):
    return {
        "entry_id": ENTRY,
        "user_id": OWNER,
        "tier": "user",
        "namespace": [OWNER, "memory"],
        "entry_key": "prefs/language.md",
        "content_hash": ENTRY_HASH,
        "identity_hash": ENTRY_IDENTITY,
        "source_entry_id": None,
        **overrides,
    }


async def test_forget_closure_is_owner_scoped_recursive_and_bounded(fake_db):
    cursor = FakeCursor(rows=[_row()])
    fake_db(cursor)

    rows = await db_memory.get_forget_closure(user_id=OWNER, entry_id=ENTRY)

    assert rows == [_row()]
    sql, params = cursor.executed[0]
    assert "WITH RECURSIVE closure" in sql
    assert " UNION " in sql and "UNION ALL" not in sql
    assert "child.user_id = parent.user_id" in sql
    assert "WHERE child.user_id = %s" in sql
    assert "memory_entry_lineage l" in sql
    assert "child.tier = parent.tier" in sql
    assert "child.namespace = parent.namespace" in sql
    assert "LIMIT 257" in sql
    assert params == (OWNER, ENTRY, OWNER)


async def test_record_multi_source_lineage_is_owner_tier_and_namespace_scoped(fake_db):
    source_ids = [
        "33333333-3333-4333-8333-333333333333",
        "44444444-4444-4444-8444-444444444444",
    ]
    cursor = FakeCursor(rows=[(2,)])
    connection = fake_db(cursor)

    count = await db_memory.record_entry_lineage(
        user_id=OWNER,
        entry_id=ENTRY,
        source_entry_ids=source_ids,
    )

    assert count == 2
    assert connection.transaction_entered == 1
    insert_sql, insert_params = cursor.executed[0]
    assert "INSERT INTO memory_entry_lineage" in insert_sql
    assert "s.namespace = c.namespace" in insert_sql
    assert "c.tier = 'user'" in insert_sql and "s.tier = 'user'" in insert_sql
    assert "c.status IN ('active', 'superseded')" in insert_sql
    assert insert_params == (source_ids, OWNER, ENTRY)


async def test_forget_closure_refuses_over_bound_lineage(fake_db):
    cursor = FakeCursor(rows=[_row(entry_id=str(index)) for index in range(257)])
    fake_db(cursor)

    with pytest.raises(ValueError, match="safe bound"):
        await db_memory.get_forget_closure(user_id=OWNER, entry_id=ENTRY)

    assert "LIMIT 257" in cursor.executed[0][0]


async def test_tombstones_store_only_owner_ids_hashes_and_reason(fake_db):
    cursor = FakeCursor()
    connection = fake_db(cursor)
    row = _row(
        index_text="fixture text omitted from tombstone",
        provenance={"private_detail": "fixture text omitted from tombstone"},
    )

    inserted = await db_memory.record_forget_tombstones(
        user_id=OWNER, rows=[row], root_entry_id=ENTRY, reason="user_request"
    )

    assert inserted == 1
    assert connection.transaction_entered == 1
    sql, values = cursor.executemany_calls[0]
    assert "memory_forget_tombstones" in sql
    assert "identity_hash" in sql and "content_hash" in sql
    assert values == [(OWNER, ENTRY, ENTRY, ENTRY_IDENTITY, ENTRY_HASH, "user_request")]
    assert "fixture text omitted" not in repr(values)


async def test_tombstone_group_lookup_is_owner_scoped_and_bounded(fake_db):
    rows = [{"entry_id": ENTRY, "content_hash": ENTRY_HASH, "reason": "duplicate"}]
    cursor = FakeCursor(rows=rows)
    fake_db(cursor)

    result = await db_memory.get_forget_tombstone_group(
        user_id=OWNER, root_entry_id=ENTRY
    )

    assert result == rows
    sql, params = cursor.executed[0]
    assert "FROM memory_forget_tombstones" in sql
    assert "WHERE user_id = %s AND root_entry_id = %s" in sql
    assert "ORDER BY entry_id LIMIT 257" in sql
    assert params == (OWNER, ENTRY)


async def test_tombstone_locator_match_uses_only_exact_owner_scoped_identity_hash(fake_db):
    matched = {
        "entry_id": ENTRY,
        "tier": "user",
        "namespace": [OWNER, "memory"],
        "entry_key": "prefs/language.md",
    }
    cursor = FakeCursor(rows=[matched])
    fake_db(cursor)

    result = await db_memory.match_forget_tombstone_locators(
        user_id=OWNER,
        root_entry_id=ENTRY,
        candidates=[{
            "tier": "user",
            "namespace": [OWNER, "memory"],
            "entry_key": "prefs/language.md",
        }],
    )

    assert result == [matched]
    sql, params = cursor.executed[0]
    assert "JOIN memory_forget_tombstones" in sql
    assert "md5(jsonb_build_array(c.tier, c.namespace, c.entry_key)::text)" in sql
    assert "f.user_id = %s AND f.root_entry_id = %s" in sql
    assert params == ["user", [OWNER, "memory"], "prefs/language.md", OWNER, ENTRY]


async def test_tombstone_locator_match_rejects_unbounded_candidates(fake_db):
    fake_db(FakeCursor())
    candidates = [
        {"tier": "user", "namespace": [OWNER, "memory"], "entry_key": f"{i}.md"}
        for i in range(258)
    ]
    with pytest.raises(ValueError, match="locator scan exceeds"):
        await db_memory.match_forget_tombstone_locators(
            user_id=OWNER, root_entry_id=ENTRY, candidates=candidates
        )


async def test_forget_rows_locks_and_hash_checks_before_atomic_delete(fake_db):
    cursor = FakeCursor(
        rows=[(ENTRY, ENTRY_HASH)], proposal_rows=[("prefs/language",)]
    )
    connection = fake_db(cursor)

    deleted = await db_memory.forget_entry_rows(
        user_id=OWNER,
        rows=[_row()],
        proposal_keys=["prefs/language", "prefs/language"],
    )

    assert deleted == {"entries": 1, "proposals": 1}
    assert connection.transaction_entered == 1
    assert connection.transaction_exit is None
    assert len(cursor.executed) == 5
    select_sql, select_params = cursor.executed[0]
    assert "FOR UPDATE" in select_sql
    assert "content_hash" in select_sql
    assert select_params == (OWNER, [ENTRY])
    delete_sql, delete_params = cursor.executed[1]
    assert "DELETE FROM memory_entries" in delete_sql
    assert delete_params == (OWNER, [ENTRY])
    proposal_select_sql, proposal_select_params = cursor.executed[2]
    assert "SELECT proposal_key FROM agent_memory_proposals" in proposal_select_sql
    assert "FOR UPDATE" in proposal_select_sql
    # Key matches plus producer proposals that copied a forgotten source.
    assert "source_entry_ids && %s::uuid[]" in proposal_select_sql
    assert proposal_select_params == (OWNER, ["prefs/language"], [ENTRY])
    proposal_sql, proposal_params = cursor.executed[3]
    assert "DELETE FROM agent_memory_proposals" in proposal_sql
    assert "source_entry_ids && %s::uuid[]" in proposal_sql
    assert proposal_params == (OWNER, ["prefs/language"], [ENTRY])
    assert "SELECT 1 FROM agent_memory_proposals" in cursor.executed[4][0]


async def test_get_entries_by_ids_is_owner_scoped(fake_db):
    cursor = FakeCursor(rows=[{"entry_id": ENTRY, "entry_key": "fact.md"}])
    fake_db(cursor)

    rows = await db_memory.get_entries_by_ids(
        user_id=OWNER, entry_ids=[ENTRY, "not-a-uuid"]
    )

    assert rows == [{"entry_id": ENTRY, "entry_key": "fact.md"}]
    sql, params = cursor.executed[0]
    assert "user_id = %s AND entry_id = ANY(%s::uuid[])" in sql
    assert params == (OWNER, [ENTRY])


async def test_forget_deletes_proposals_that_copied_a_forgotten_source(fake_db):
    """A consolidation proposal's key is unrelated; its source links must match."""
    cursor = FakeCursor(
        rows=[(ENTRY, ENTRY_HASH)],
        proposal_rows=[("memory-consolidation-abc",)],
    )
    fake_db(cursor)

    deleted = await db_memory.forget_entry_rows(
        user_id=OWNER, rows=[_row()], proposal_keys=[]
    )

    assert deleted == {"entries": 1, "proposals": 1}
    proposal_select_sql, proposal_select_params = cursor.executed[2]
    assert "source_entry_ids && %s::uuid[]" in proposal_select_sql
    assert proposal_select_params == (OWNER, [], [ENTRY])


async def test_forget_reports_no_matching_proposal_rows_as_absent(fake_db):
    cursor = FakeCursor(rows=[(ENTRY, ENTRY_HASH)])
    fake_db(cursor)
    result = await db_memory.forget_entry_rows(
        user_id=OWNER, rows=[_row()], proposal_keys=["prefs/language"]
    )
    assert result == {"entries": 1, "proposals": 0}


async def test_forget_rolls_back_if_proposal_delete_rowcount_is_incomplete(fake_db):
    cursor = FakeCursor(
        rows=[(ENTRY, ENTRY_HASH)],
        proposal_rows=[("prefs/language",)],
        proposal_delete_rowcount=0,
    )
    connection = fake_db(cursor)

    with pytest.raises(RuntimeError, match="full proposal set"):
        await db_memory.forget_entry_rows(
            user_id=OWNER, rows=[_row()], proposal_keys=["prefs/language"]
        )

    assert connection.transaction_exit is RuntimeError


async def test_forget_rows_rolls_back_on_hash_mismatch_before_deleting(fake_db):
    cursor = FakeCursor(rows=[(ENTRY, "sha256:changed")])
    connection = fake_db(cursor)

    with pytest.raises(RuntimeError, match="content changed"):
        await db_memory.forget_entry_rows(user_id=OWNER, rows=[_row()])

    assert connection.transaction_entered == 1
    assert connection.transaction_exit is RuntimeError
    assert len(cursor.executed) == 1
    assert "FOR UPDATE" in cursor.executed[0][0]


@pytest.mark.parametrize(
    ("query", "search_marker"),
    [("documents", "websearch_to_tsquery"), ("文档", "word_similarity")],
)
async def test_lexical_search_paths_exclude_tombstoned_identities(
    fake_db, query, search_marker
):
    cursor = FakeCursor()
    fake_db(cursor)

    await db_memory.search_entries(user_id=OWNER, query=query, tier="user")

    sql = cursor.executed[-1][0]
    assert search_marker in sql
    assert "NOT EXISTS (SELECT 1 FROM memory_forget_tombstones" in sql
    assert "f.identity_hash = memory_entries.identity_hash" in sql


async def test_entry_and_vector_reads_exclude_tombstoned_identities(fake_db):
    cursor = FakeCursor()
    fake_db(cursor)

    assert await db_memory.get_entry(
        user_id=OWNER, tier="user", entry_key="prefs/language.md"
    ) is None
    get_sql = cursor.executed[-1][0]
    assert "NOT EXISTS (SELECT 1 FROM memory_forget_tombstones" in get_sql

    await db_memory.list_retrieval_rows(user_id=OWNER, tier="user")
    vector_sql = cursor.executed[-1][0]
    assert "embedding, embedding_model, embedding_content_hash" in vector_sql
    assert "NOT EXISTS (SELECT 1 FROM memory_forget_tombstones" in vector_sql


async def test_approval_identity_read_allows_future_validity_rows(fake_db):
    cursor = FakeCursor(rows=[_row(metadata={"kind": "constraint", "valid_from": "2099-01-01"})])
    fake_db(cursor)

    row = await db_memory.get_active_entry_for_approval(
        user_id=OWNER, tier="user", entry_key="prefs/language.md"
    )

    assert row["entry_id"] == ENTRY
    sql = cursor.executed[-1][0]
    assert "status = 'active'" in sql
    assert "NOT EXISTS (SELECT 1 FROM memory_forget_tombstones" in sql
    assert "is_current" not in sql
    assert "valid_from" not in sql
