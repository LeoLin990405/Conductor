"""086–090 against real PostgreSQL: additive schema, constraints, and the way back.

Seeds a database at 085 (user, workspace, thread, responses), upgrades to 090,
checks the new tables/columns/the generated FTS column and the database
invariants a service could rely on, downgrades (the documented rollback), and
upgrades again to show the chain re-runs cleanly and loses only the rows that
lived in the new tables.

Its own database: the test downgrades the schema, which the shared session
database cannot survive. Unit coverage for the SQL<->contract binding lives in
``tests/unit/migrations/test_refactor_migrations.py``; this file proves the DDL
actually applies to Postgres.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest
import pytest_asyncio
from psycopg.types.json import Jsonb

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

SCRATCH_DB = "conductor_migration_090"

USER = "00000000-0000-4000-8000-000000000090"
WORKSPACE = str(uuid.uuid4())
THREAD = str(uuid.uuid4())
RESPONSE = str(uuid.uuid4())
RESPONSE2 = str(uuid.uuid4())

NEW_TABLES = (
    "agent_goals",
    "agent_plans",
    "agent_run_contracts",
    "agent_events",
    "memory_entries",
    "agent_tasks",
)


def _uri(dbname: str) -> str:
    from tests.integration.conftest import _build_db_uri

    parts = urlparse(_build_db_uri())
    return urlunparse(parts._replace(path=f"/{dbname}"))


async def _alembic(uri: str, action: str, revision: str) -> None:
    """Threaded, as the suite's upgrade helper is: a migration calls
    ``asyncio.run`` internally."""
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option(
        "sqlalchemy.url", uri.replace("postgresql://", "postgresql+psycopg://", 1)
    )
    cfg.config_file_name = None
    run = {"upgrade": command.upgrade, "downgrade": command.downgrade}[action]
    await asyncio.to_thread(run, cfg, revision)


async def _execute(uri: str, statements: list[tuple[str, tuple]]) -> None:
    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        for sql, params in statements:
            await conn.execute(sql, params)


async def _fetch(uri: str, sql: str, params: tuple = ()) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def _fetch_one(uri: str, sql: str, params: tuple = ()) -> Any:
    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def _expect_error(uri: str, sql: str, params: tuple, error: type[Exception]) -> None:
    with pytest.raises(error):
        await _execute(uri, [(sql, params)])


def _seed() -> list[tuple[str, tuple]]:
    return [
        (
            "INSERT INTO users (user_id, email) VALUES (%s, %s)",
            (USER, "migration-090@example.com"),
        ),
        (
            "INSERT INTO workspaces (workspace_id, user_id, name, name_key, status)"
            " VALUES (%s, %s, 'Research', 'research', 'running')",
            (WORKSPACE, USER),
        ),
        (
            "INSERT INTO conversation_threads"
            " (conversation_thread_id, workspace_id, current_status, thread_index)"
            " VALUES (%s, %s, 'completed', 1)",
            (THREAD, WORKSPACE),
        ),
        # The lifecycle guard (migration 018) only allows runs to be born
        # in_progress, and a partial unique index allows one in-flight run per
        # thread: seed each response, settle it, then seed the next.
        (
            "INSERT INTO conversation_responses"
            " (conversation_response_id, conversation_thread_id, turn_index, status,"
            "  created_at)"
            " VALUES (%s, %s, 1, 'in_progress', NOW())",
            (RESPONSE, THREAD),
        ),
        (
            "UPDATE conversation_responses SET status = 'completed'"
            " WHERE conversation_response_id = %s",
            (RESPONSE,),
        ),
        (
            "INSERT INTO conversation_responses"
            " (conversation_response_id, conversation_thread_id, turn_index, status,"
            "  created_at)"
            " VALUES (%s, %s, 2, 'in_progress', NOW())",
            (RESPONSE2, THREAD),
        ),
        (
            "UPDATE conversation_responses SET status = 'completed'"
            " WHERE conversation_response_id = %s",
            (RESPONSE2,),
        ),
    ]


_GOAL_INSERT = (
    "INSERT INTO agent_goals"
    " (user_id, thread_id, title, objective, stop_conditions, status)"
    " VALUES (%s, %s, %s, %s, %s, %s)"
    " RETURNING goal_id"
)


async def _schema(uri: str) -> dict[str, Any]:
    present = await _fetch(
        uri,
        "SELECT " + ", ".join(f"to_regclass('{t}') IS NOT NULL" for t in NEW_TABLES),
    )
    subagent_columns = await _fetch(
        uri,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'subagent_runs'"
        "   AND column_name IN ('contract_version','context_budget',"
        "                       'tool_allowlist','result','verification')"
        " ORDER BY column_name",
    )
    memory_columns = await _fetch(
        uri,
        "SELECT column_name FROM information_schema.columns"
        " WHERE table_name = 'memory_entries'"
        "   AND column_name IN ('tier','status','index_text','search')"
        " ORDER BY column_name",
    )
    table_presence = tuple(present[0])
    assert not any(table_presence) or all(table_presence), {
        "partial migration 090 schema": table_presence
    }
    counts = (
        await _fetch(
            uri,
            "SELECT (SELECT count(*) FROM agent_goals),"
            "       (SELECT count(*) FROM memory_entries),"
            "       (SELECT count(*) FROM agent_events),"
            "       (SELECT count(*) FROM agent_tasks)",
        )
        if all(table_presence)
        else [(0, 0, 0, 0)]
    )
    return {
        "present": table_presence,
        "subagent_columns": [row[0] for row in subagent_columns],
        "memory_columns": [row[0] for row in memory_columns],
        "counts": counts[0],
    }


@pytest_asyncio.fixture(scope="module")
async def run() -> dict[str, Any]:
    admin, uri = _uri("template1"), _uri(SCRATCH_DB)

    async def _recreate(create: bool) -> None:
        async with await psycopg.AsyncConnection.connect(admin, autocommit=True) as c:
            await c.execute(f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
            if create:
                await c.execute(f'CREATE DATABASE "{SCRATCH_DB}"')

    await _recreate(create=True)
    out: dict[str, Any] = {}
    try:
        with pytest.MonkeyPatch.context() as mp, tempfile.TemporaryDirectory() as tmp:
            # Earlier migrations read the bundled servers; keep them as shipped.
            config = Path(tmp) / "agent_config.yaml"
            config.write_text("{}\n")
            mp.setenv("PTC_CONFIG_FILE", str(config))

            await _alembic(uri, "upgrade", "085")
            await _execute(uri, _seed())
            out["before"] = await _fetch(
                uri,
                "SELECT to_regclass('agent_goals'), to_regclass('memory_entries'),"
                "       to_regclass('agent_tasks')",
            )

            await _alembic(uri, "upgrade", "090")
            out["up"] = await _schema(uri)

            goal_id = (
                await _fetch_one(
                    uri,
                    _GOAL_INSERT,
                    (
                        USER,
                        THREAD,
                        "Research 600519",
                        "Summarize the last four quarterly reports",
                        Jsonb(["each quarter has a citation"]),
                        "active",
                    ),
                )
            )[0]
            await _execute(
                uri,
                [
                    (
                        "INSERT INTO agent_plans (goal_id, version, steps, status)"
                        " VALUES (%s, %s, %s, 'awaiting_approval')",
                        (str(goal_id), 1, Jsonb([{"step": "collect filings"}])),
                    ),
                    (
                        "INSERT INTO agent_run_contracts"
                        " (conversation_response_id, permission_mode, context_budget,"
                        "  injected_memory)"
                        " VALUES (%s, 'auto', %s, %s)",
                        (
                            RESPONSE,
                            Jsonb({"max_tokens": 8000}),
                            Jsonb(["memory/user/prefs/language"]),
                        ),
                    ),
                    (
                        "INSERT INTO agent_events"
                        " (user_id, thread_id, response_id, seq, type, payload)"
                        " VALUES (%s, %s, %s, 1, 'goal.created', %s)",
                        (USER, THREAD, RESPONSE, Jsonb({"goal_id": str(goal_id)})),
                    ),
                    (
                        "INSERT INTO memory_entries"
                        " (user_id, tier, namespace, entry_key, content_hash, size,"
                        "  index_text, status, provenance)"
                        " VALUES (%s, 'user', %s, 'prefs/language', 'sha256:abc', 42,"
                        "         'prefs language: prefers Chinese answers', 'active', %s)",
                        (USER, ["user-id", "memory"], Jsonb({"source": "manual"})),
                    ),
                    (
                        "INSERT INTO agent_tasks"
                        " (user_id, thread_id, title, status, source)"
                        " VALUES (%s, %s, 'collect filings', 'in_progress', 'todo')",
                        (USER, THREAD),
                    ),
                ],
            )
            out["rows"] = await _fetch(
                uri,
                "SELECT g.status, p.status, c.permission_mode,"
                "       e.type, m.tier, m.status"
                " FROM agent_goals g"
                " JOIN agent_plans p ON p.goal_id = g.goal_id"
                " JOIN agent_run_contracts c ON c.conversation_response_id = %s"
                " JOIN agent_events e ON e.response_id = c.conversation_response_id"
                " JOIN memory_entries m ON m.user_id = g.user_id",
                (RESPONSE,),
            )
            out["fts"] = await _fetch_one(
                uri,
                "SELECT count(*) FROM memory_entries"
                " WHERE search @@ to_tsquery('simple', 'prefs')",
            )

            # One live goal per thread, history allowed.
            await _expect_error(
                uri,
                _GOAL_INSERT,
                (USER, THREAD, "second", "second objective", Jsonb(["done"]), "active"),
                psycopg.errors.UniqueViolation,
            )
            await _execute(
                uri,
                [
                    (
                        _GOAL_INSERT,
                        (USER, THREAD, "older", "old objective", Jsonb(["done"]), "completed"),
                    )
                ],
            )
            # CHECK constraints reject vocabulary drift.
            await _expect_error(
                uri,
                _GOAL_INSERT,
                (USER, THREAD, "bad", "bad objective", Jsonb(["done"]), "done"),
                psycopg.errors.CheckViolation,
            )
            await _expect_error(
                uri,
                "INSERT INTO agent_run_contracts"
                " (conversation_response_id, permission_mode) VALUES (%s, 'always')",
                (RESPONSE2,),
                psycopg.errors.CheckViolation,
            )
            await _expect_error(
                uri,
                "INSERT INTO memory_entries"
                " (user_id, tier, namespace, entry_key, content_hash, status)"
                " VALUES (%s, 'global', '{}', 'k', 'h', 'active')",
                (USER,),
                psycopg.errors.CheckViolation,
            )
            await _expect_error(
                uri,
                "INSERT INTO memory_entries"
                " (user_id, tier, namespace, entry_key, content_hash, status)"
                " VALUES (%s, 'user', '{}', 'k2', 'h', 'gone')",
                (USER,),
                psycopg.errors.CheckViolation,
            )
            await _expect_error(
                uri,
                "INSERT INTO agent_tasks (user_id, thread_id, title, status, source)"
                " VALUES (%s, %s, 'bad', 'done', 'todo')",
                (USER, THREAD),
                psycopg.errors.CheckViolation,
            )
            await _expect_error(
                uri,
                "INSERT INTO agent_tasks (user_id, thread_id, title, status, source)"
                " VALUES (%s, %s, 'bad', 'pending', 'manual')",
                (USER, THREAD),
                psycopg.errors.CheckViolation,
            )
            out["counts"] = await _fetch_one(
                uri,
                "SELECT (SELECT count(*) FROM agent_goals),"
                "       (SELECT count(*) FROM memory_entries),"
                "       (SELECT count(*) FROM agent_events),"
                "       (SELECT count(*) FROM agent_tasks)",
            )

            await _alembic(uri, "downgrade", "085")
            out["down"] = await _schema(uri)

            await _alembic(uri, "upgrade", "090")
            out["again"] = await _schema(uri)
        yield out
    finally:
        await _recreate(create=False)


class TestUpgrade:
    async def test_new_tables_and_columns_exist(self, run):
        assert run["before"] == [(None, None, None)]
        assert all(run["up"]["present"]), run["up"]["present"]
        assert run["up"]["subagent_columns"] == [
            "context_budget",
            "contract_version",
            "result",
            "tool_allowlist",
            "verification",
        ]
        assert run["up"]["memory_columns"] == ["index_text", "search", "status", "tier"]

    async def test_goal_plan_contract_event_and_memory_rows_write(self, run):
        assert run["rows"] == [
            ("active", "awaiting_approval", "auto", "goal.created", "user", "active")
        ]

    async def test_generated_search_column_is_queryable(self, run):
        assert run["fts"] == (1,)

    async def test_one_live_goal_per_thread_but_history_allowed(self, run):
        assert run["counts"][0] == 2  # the active goal + the completed history row


class TestConstraints:
    async def test_status_tier_and_source_checks_reject_unknown_values(self, run):
        # The fixture asserted CheckViolation for status='done'/'gone'/'manual'
        # and tier='global'; the surviving rows are the valid ones.
        assert run["counts"] == (2, 1, 1, 1)

    async def test_permission_mode_check_rejects_unknown_values(self, run):
        assert run["rows"][0][2] == "auto"


class TestRollback:
    async def test_downgrade_removes_tables_and_columns(self, run):
        assert run["down"]["present"] == (False,) * len(run["down"]["present"])
        assert run["down"]["subagent_columns"] == []

    async def test_upgrade_again_is_clean_and_empty(self, run):
        assert all(run["again"]["present"])
        assert run["again"]["counts"] == (0, 0, 0, 0)
