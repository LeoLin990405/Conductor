"""The thread SQL carries the Swarm column with matching placeholders."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.server.database.conversation import threads_write


def _pool(captured: list[tuple[str, tuple]]) -> MagicMock:
    cur = MagicMock()

    async def execute(sql, params=None):
        captured.append((sql, tuple(params or ())))

    cur.execute = AsyncMock(side_effect=execute)
    cur.fetchone = AsyncMock(return_value={"swarm_enabled": True})

    @asynccontextmanager
    async def _cursor(*_a, **_k):
        yield cur

    conn = MagicMock()
    conn.cursor = _cursor

    @asynccontextmanager
    async def _conn():
        yield conn

    return MagicMock(side_effect=_conn)


@pytest.mark.asyncio
async def test_create_thread_includes_swarm_with_matching_placeholders():
    captured: list[tuple[str, tuple]] = []
    with (
        patch.object(threads_write.pool, "get_db_connection", new=_pool(captured)),
        patch.object(
            threads_write,
            "calculate_next_thread_index",
            new=AsyncMock(return_value=0),
        ),
    ):
        row = await threads_write.create_thread(
            conversation_thread_id="t-1",
            workspace_id="ws-1",
            current_status="completed",
            swarm_enabled=True,
        )

    sql, params = captured[0]
    assert "swarm_enabled" in sql
    assert "RETURNING" in sql and "swarm_enabled" in sql
    assert sql.count("%s") == len(params)
    assert True in params  # the Swarm value is bound as a boolean
    assert row["swarm_enabled"] is True


@pytest.mark.asyncio
async def test_update_thread_fields_writes_swarm_as_a_boolean():
    captured: list[tuple[str, tuple]] = []
    with patch.object(threads_write.pool, "get_db_connection", new=_pool(captured)):
        await threads_write.update_thread_fields("t-1", swarm_enabled=True)

    sql, params = captured[0]
    assert "swarm_enabled = %s" in sql
    assert params[0] is True
