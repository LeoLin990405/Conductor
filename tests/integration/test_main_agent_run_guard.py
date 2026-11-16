"""The per-run coordinator guard serializes independent PostgreSQL sessions."""

from __future__ import annotations

from unittest.mock import patch
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Json

pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]


class _RollbackTestRow(Exception):
    pass


async def test_run_guard_is_shared_by_independent_postgres_sessions_and_released(
    patched_get_db_connection, test_db_uri, test_user_id
):
    from src.server.database.main_agent import PostgresMainAgentRepository

    run_id = f"coordinator-guard-test-{uuid4().hex}"

    async def connect_test_db():
        return await psycopg.AsyncConnection.connect(
            test_db_uri, autocommit=True, prepare_threshold=0
        )

    repository = PostgresMainAgentRepository()
    try:
        async with patched_get_db_connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO main_agent_runs (run_id, user_id, revision, payload) "
                    "VALUES (%s, %s, 0, %s)",
                    (
                        run_id,
                        test_user_id,
                        Json({"id": run_id, "user_id": test_user_id, "revision": 0}),
                    ),
                )
                with patch(
                    "src.server.database.main_agent.open_session_connection",
                    new=connect_test_db,
                ):
                    async with repository.run_guard(run_id) as first:
                        assert first
                        async with repository.run_guard(run_id) as contended:
                            assert not contended
                    async with repository.run_guard(run_id) as after_release:
                        assert after_release
                raise _RollbackTestRow
    except _RollbackTestRow:
        pass

    async with patched_get_db_connection() as conn:
        row = await conn.execute(
            "SELECT count(*) AS n FROM main_agent_runs WHERE run_id = %s", (run_id,)
        )
        assert (await row.fetchone())["n"] == 0
