from datetime import datetime, timezone

import pytest

from src.server.database.refactor import memory_tool_policy


class _Cursor:
    def __init__(self, rows):
        self.rows = rows
        self.query = ""
        self.params = None
        self.queries = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, query, params):
        self.query = query
        self.params = params
        self.queries.append(query)

    async def fetchall(self):
        return self.rows

    async def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def cursor(self, **kwargs):
        return self._cursor

    def transaction(self):
        return _Transaction()


class _Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


@pytest.mark.asyncio
async def test_load_reads_owner_active_unforgotten_constraints_only(monkeypatch):
    cursor = _Cursor(
        [
            {
                "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "approved_content_hash": "sha256:approved",
                "current_content_hash": "sha256:edited",
                "current_status": "superseded",
                "current_metadata": {"kind": "user", "lifecycle": "wrong"},
                "valid_from": None,
                "valid_until": None,
            },
        ]
    )
    monkeypatch.setattr(
        memory_tool_policy.pool,
        "get_db_connection",
        lambda: _Connection(cursor),
    )

    rows = await memory_tool_policy.list_active_denials(
        user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b"
    )

    assert rows == [
        {
            "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
            "denied_tools": [{"type": "tool", "name": "Bash"}],
            "valid_from": None,
            "valid_until": None,
            "integrity_mismatch": True,
        }
    ]
    assert "d.user_id = %s" in cursor.query
    assert "d.approval_source = 'manual'" in cursor.query
    assert "d.active IS TRUE" in cursor.query
    assert "f.entry_id = d.entry_id" in cursor.query
    assert "e.status" not in cursor.query.split(" WHERE ", 1)[1]
    assert cursor.params == ("8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",)


@pytest.mark.asyncio
async def test_missing_projection_marks_drift_but_keeps_frozen_denial(monkeypatch):
    cursor = _Cursor(
        [
            {
                "entry_id": "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "approved_content_hash": "sha256:approved",
                "current_content_hash": None,
                "current_status": None,
                "current_metadata": None,
                "valid_from": None,
                "valid_until": None,
            },
        ]
    )
    monkeypatch.setattr(
        memory_tool_policy.pool,
        "get_db_connection",
        lambda: _Connection(cursor),
    )

    rows = await memory_tool_policy.list_active_denials(
        user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b"
    )

    assert rows[0]["integrity_mismatch"] is True
    assert rows[0]["denied_tools"] == [{"type": "tool", "name": "Bash"}]


@pytest.mark.asyncio
async def test_active_authority_lookup_uses_frozen_table_not_memory_metadata(monkeypatch):
    cursor = _Cursor([(True,)])
    monkeypatch.setattr(
        memory_tool_policy.pool,
        "get_db_connection",
        lambda: _Connection(cursor),
    )

    found = await memory_tool_policy.has_active_denials(
        user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
        entry_ids=("a2f1650c-4fe1-473e-9106-e3341e5edc93",),
    )

    assert found is True
    assert "FROM memory_tool_denials d" in cursor.query
    assert "d.active IS TRUE" in cursor.query
    assert "memory_entries" not in cursor.query


@pytest.mark.asyncio
async def test_empty_replacement_denials_can_retire_nonconstraint_revision(monkeypatch):
    cursor = _Cursor(
        [
            ("sha256:" + "a" * 64,),
        ]
    )
    monkeypatch.setattr(
        memory_tool_policy.pool,
        "get_db_connection",
        lambda: _Connection(cursor),
    )

    await memory_tool_policy.replace_denials(
        user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
        entry_id="a2f1650c-4fe1-473e-9106-e3341e5edc93",
        denied_tools=[],
        approval_source="manual",
        approved_content_hash="sha256:" + "a" * 64,
        replaced_entry_ids=("74b99e34-12d7-4589-ae37-306b65e408c5",),
    )

    entry_query = next(q for q in cursor.queries if "SELECT m.content_hash" in q)
    assert "m.metadata->>'kind'" not in entry_query
    assert any("UPDATE memory_tool_denials SET active = FALSE" in q for q in cursor.queries)


@pytest.mark.asyncio
async def test_forget_purge_requires_tombstones_before_deleting_authority(monkeypatch):
    cursor = _Cursor([(2,)])
    monkeypatch.setattr(
        memory_tool_policy.pool,
        "get_db_connection",
        lambda: _Connection(cursor),
    )
    ids = (
        "a2f1650c-4fe1-473e-9106-e3341e5edc93",
        "74b99e34-12d7-4589-ae37-306b65e408c5",
    )

    purged = await memory_tool_policy.purge_forgotten_denials(
        user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b", entry_ids=ids
    )

    assert purged == 0
    assert len(cursor.queries) == 2
    assert "memory_forget_tombstones" in cursor.queries[0]
    assert "DELETE FROM memory_tool_denials" in cursor.queries[1]
    assert cursor.params == (
        "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
        list(ids),
    )


@pytest.mark.asyncio
async def test_forget_purge_refuses_before_any_authority_delete_without_full_tombstone_set(
    monkeypatch,
):
    cursor = _Cursor([(1,)])
    monkeypatch.setattr(
        memory_tool_policy.pool,
        "get_db_connection",
        lambda: _Connection(cursor),
    )
    ids = (
        "a2f1650c-4fe1-473e-9106-e3341e5edc93",
        "74b99e34-12d7-4589-ae37-306b65e408c5",
    )

    with pytest.raises(ValueError, match="tombstoned first"):
        await memory_tool_policy.purge_forgotten_denials(
            user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b", entry_ids=ids
        )

    assert len(cursor.queries) == 1
    assert "DELETE FROM memory_tool_denials" not in cursor.queries[0]


@pytest.mark.asyncio
async def test_reverted_policy_copy_keeps_frozen_ids_and_window(monkeypatch):
    valid_from = datetime(2026, 1, 1, tzinfo=timezone.utc)
    valid_until = datetime(2027, 1, 1, tzinfo=timezone.utc)
    cursor = _Cursor(
        [
            {"content_hash": "sha256:" + "b" * 64, "kind": "constraint"},
            {
                "denied_tools": [{"type": "tool", "name": "Bash"}],
                "valid_from": valid_from,
                "valid_until": valid_until,
            },
        ]
    )
    monkeypatch.setattr(
        memory_tool_policy.pool,
        "get_db_connection",
        lambda: _Connection(cursor),
    )

    copied = await memory_tool_policy.copy_reverted_denials(
        user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
        source_entry_id="a2f1650c-4fe1-473e-9106-e3341e5edc93",
        restored_entry_id="74b99e34-12d7-4589-ae37-306b65e408c5",
        restored_content_hash="sha256:" + "b" * 64,
        replaced_entry_ids=("decc8d40-a49c-4d48-b1c1-6fbd30e99621",),
    )

    assert copied is True
    insert_query = next(q for q in cursor.queries if "INSERT INTO memory_tool_denials" in q)
    assert "valid_from, valid_until" in insert_query
    assert cursor.params[:2] == (
        "8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
        "74b99e34-12d7-4589-ae37-306b65e408c5",
    )
    assert cursor.params[2].obj == [{"type": "tool", "name": "Bash"}]
    assert cursor.params[3:] == (
        "sha256:" + "b" * 64,
        valid_from,
        valid_until,
    )


@pytest.mark.asyncio
async def test_forgotten_source_does_not_regain_policy_on_revert(monkeypatch):
    cursor = _Cursor([{"content_hash": "sha256:" + "b" * 64, "kind": "constraint"}])
    monkeypatch.setattr(
        memory_tool_policy.pool,
        "get_db_connection",
        lambda: _Connection(cursor),
    )

    copied = await memory_tool_policy.copy_reverted_denials(
        user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
        source_entry_id="a2f1650c-4fe1-473e-9106-e3341e5edc93",
        restored_entry_id="74b99e34-12d7-4589-ae37-306b65e408c5",
        restored_content_hash="sha256:" + "b" * 64,
        replaced_entry_ids=("decc8d40-a49c-4d48-b1c1-6fbd30e99621",),
    )

    assert copied is False
    assert len(cursor.queries) == 3
    assert any("memory_forget_tombstones" in query for query in cursor.queries)
    assert any("UPDATE memory_tool_denials SET active = FALSE" in query for query in cursor.queries)


@pytest.mark.asyncio
async def test_revert_to_nonconstraint_retires_current_policy(monkeypatch):
    cursor = _Cursor([{"content_hash": "sha256:" + "b" * 64, "kind": "preference"}])
    monkeypatch.setattr(
        memory_tool_policy.pool,
        "get_db_connection",
        lambda: _Connection(cursor),
    )

    copied = await memory_tool_policy.copy_reverted_denials(
        user_id="8b5f4822-d24d-48d4-83b5-48bdb23d0f2b",
        source_entry_id="a2f1650c-4fe1-473e-9106-e3341e5edc93",
        restored_entry_id="74b99e34-12d7-4589-ae37-306b65e408c5",
        restored_content_hash="sha256:" + "b" * 64,
        replaced_entry_ids=("decc8d40-a49c-4d48-b1c1-6fbd30e99621",),
    )

    assert copied is False
    assert any("UPDATE memory_tool_denials SET active = FALSE" in query for query in cursor.queries)
