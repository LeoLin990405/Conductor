"""TodoWrite snapshot reconciliation against real PostgreSQL (integration).

The service is wired into the SSE producer behind ``REFACTOR_TASK_TREE_ENABLED``;
this test proves the snapshot semantics against the migrated schema: titles are
the identity, status changes update one row, items missing from the next snapshot
are cancelled, and an empty snapshot closes the open set. It also covers the
owner resolution (thread → workspace → user) the producer relies on.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed_thread(pool) -> tuple[str, str]:
    user_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, email) VALUES (%s, %s)",
            (user_id, "refactor-todo@example.com"),
        )
        await conn.execute(
            "INSERT INTO workspaces (workspace_id, user_id, name, name_key, status)"
            " VALUES (%s, %s, 'Research', 'research', 'running')",
            (workspace_id, user_id),
        )
        await conn.execute(
            "INSERT INTO conversation_threads"
            " (conversation_thread_id, workspace_id, current_status, thread_index)"
            " VALUES (%s, %s, 'completed', 1)",
            (thread_id, workspace_id),
        )
    return user_id, thread_id


async def _rows(pool, thread_id: str) -> list[dict]:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT title, status, user_id::text AS user_id FROM agent_tasks"
            " WHERE thread_id = %s AND source = 'todo' ORDER BY title",
            (thread_id,),
        )
        return [dict(row) for row in await cur.fetchall()]


class TestTodoSnapshot:
    async def test_snapshot_inserts_and_reconciles(self, refactor_db):
        from src.server.services.refactor.tasks import record_todo_snapshot

        user_id, thread_id = await _seed_thread(refactor_db)
        result = await record_todo_snapshot(
            thread_id=thread_id,
            todos=[
                {"content": "collect changelogs", "status": "in_progress"},
                {"content": "build model", "status": "pending"},
            ],
        )
        assert result == {"written": 2, "rows": 2, "open": 2, "completed": 0}
        rows = await _rows(refactor_db, thread_id)
        assert {row["user_id"] for row in rows} == {str(user_id)}

        # The model rewrites the list: one completes, one disappears, one is new.
        await record_todo_snapshot(
            thread_id=thread_id,
            todos=[
                {"content": "collect changelogs", "status": "completed"},
                {"content": "write memo", "status": "pending"},
            ],
        )
        rows = await _rows(refactor_db, thread_id)
        assert [(row["title"], row["status"]) for row in rows] == [
            ("build model", "cancelled"),
            ("collect changelogs", "completed"),
            ("write memo", "pending"),
        ]

        # An empty snapshot closes everything still open.
        await record_todo_snapshot(thread_id=thread_id, todos=[])
        rows = await _rows(refactor_db, thread_id)
        assert [row["status"] for row in rows] == ["cancelled", "completed", "cancelled"]

    async def test_unknown_thread_is_a_noop(self, refactor_db):
        from src.server.services.refactor.tasks import record_todo_snapshot

        assert (
            await record_todo_snapshot(
                thread_id=str(uuid.uuid4()),
                todos=[{"content": "x", "status": "pending"}],
            )
            is None
        )

    async def test_flag_off_writes_nothing(self, refactor_db, monkeypatch):
        from src.server.services.refactor.tasks import record_todo_snapshot

        monkeypatch.delenv("REFACTOR_TASK_TREE_ENABLED", raising=False)
        _user_id, thread_id = await _seed_thread(refactor_db)
        assert (
            await record_todo_snapshot(
                thread_id=thread_id,
                todos=[{"content": "x", "status": "pending"}],
            )
            is None
        )
        assert await _rows(refactor_db, thread_id) == []
