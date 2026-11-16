"""Fixtures for integration tests.

Provides:
- Database fixtures for integration tests against real PostgreSQL
"""

from __future__ import annotations

import os

from tests.integration.db_guard import require_test_database_name
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


# ---------------------------------------------------------------------------
# Database integration fixtures
# ---------------------------------------------------------------------------

# Tables in dependency (FK) order -- children first so TRUNCATE CASCADE is safe
_ALL_TABLES = [
    # Autonomy rows (dispatch/effects/tickets) leak between integration files
    # when a test's fixture only deletes its own user; the runner inside another
    # test claims any due effect regardless of user. Include them in the session
    # teardown as well as the cloud-worker fixture's setup/teardown.
    "autonomy_effects",
    "autonomy_tickets",
    "autonomy_daemons",
    "platform_secret_rollouts",
    "automation_executions",
    "automations",
    # Deep-refactor tables (migrations 086–090); the ones without an FK to a
    # listed parent (agent_events, memory_entries) would otherwise leak between
    # integration tests in the same session.
    "agent_run_contracts",
    "agent_events",
    "memory_entries",
    "agent_tasks",
    "agent_plans",
    "agent_goals",
    "conversation_feedback",
    "conversation_usages",
    "conversation_responses",
    "conversation_queries",
    "conversation_threads",
    "workspace_mcp_tool_schemas",
    "workspace_mcp_servers",
    "user_mcp_servers",
    "user_mcp_builtin_disables",
    "workspace_skill_disables",
    "user_skills",
    "user_plugins",
    "workspace_files",
    "workspace_file_blobs",
    "user_api_keys",
    "user_preferences",
    "workspaces",
    "users",
]


def _build_db_uri() -> str:
    """Build PostgreSQL connection string from env vars (CI-compatible defaults).

    Uses TEST_DB_* env vars so the app's .env (which sets DB_HOST to
    host.docker.internal for Docker networking) does not bleed into tests
    that run on the host machine.
    """
    host = os.getenv("TEST_DB_HOST", "localhost")
    port = os.getenv("TEST_DB_PORT", "5432")
    name = require_test_database_name()
    user = os.getenv("TEST_DB_USER", "postgres")
    password = os.getenv("TEST_DB_PASSWORD", "postgres")
    # Deliberately not DB_SSLMODE — reading it would let a production-shaped .env
    # dictate test TLS policy, which is what TEST_DB_* exists to prevent.
    sslmode = os.getenv("TEST_DB_SSLMODE", "prefer")
    return f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"


async def _run_alembic_upgrade(db_uri: str) -> None:
    """Run alembic migrations against the given database URI.

    Uses the project's alembic.ini + migrations/ directory as the single
    source of truth, so the test schema always matches production.

    Runs in a thread because the migration uses asyncio.run() internally
    (for LangGraph checkpoint setup), which would conflict with the
    already-running event loop in pytest-asyncio fixtures.
    """
    import asyncio

    from alembic import command
    from alembic.config import Config

    project_root = Path(__file__).resolve().parent.parent.parent
    alembic_cfg = Config(str(project_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(project_root / "migrations"))

    # Convert psycopg URI to SQLAlchemy+psycopg URI for alembic
    sa_url = db_uri.replace("postgresql://", "postgresql+psycopg://", 1)
    alembic_cfg.set_main_option("sqlalchemy.url", sa_url)
    # Alembic's env.py fileConfig replaces pytest's root handlers and disables
    # existing loggers. This in-process migration only needs the DB settings.
    alembic_cfg.config_file_name = None

    await asyncio.to_thread(command.upgrade, alembic_cfg, "head")


@pytest.fixture(scope="session")
def test_db_uri() -> str:
    """Build and return the test database URI."""
    return _build_db_uri()


@pytest_asyncio.fixture(scope="session")
async def test_db_pool(test_db_uri):
    """Session-scoped async connection pool for integration tests.

    Runs alembic migrations (single source of truth) to create the schema,
    yields the pool, then tears down by truncating all tables.
    """
    import psycopg

    # Nuke the whole schema for a clean slate before migrations. A named
    # drop-list goes stale every time a migration adds a table (it silently
    # missed subagent_runs/subagent_tasks/hook_outbox/provenance_*), and a
    # survivor breaks the re-run: alembic starts from scratch against
    # objects that still exist. CI never sees this — its postgres is fresh
    # per run; local reruns are the case this must survive.
    async with await psycopg.AsyncConnection.connect(
        test_db_uri, autocommit=False
    ) as conn:
        async with conn.cursor() as cur:
            await cur.execute("DROP SCHEMA public CASCADE")
            await cur.execute("CREATE SCHEMA public")
        await conn.commit()

    # Run alembic migrations -- the single source of truth for schema
    await _run_alembic_upgrade(test_db_uri)

    # Now create the pool for actual test operations
    pool = AsyncConnectionPool(
        conninfo=test_db_uri,
        min_size=1,
        max_size=5,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
    )
    await pool.open()
    await pool.wait()

    yield pool

    # Teardown: truncate (not drop) so the schema remains for debugging
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            for table in _ALL_TABLES:
                await cur.execute(f"TRUNCATE TABLE {table} CASCADE")

    await pool.close()


def _patch_db_access(monkeypatch, connection) -> None:
    """Point the app's Postgres access at the session pool.

    Most modules reach the pool through ``database.pool.get_db_connection``, but
    several bound the name at import time (``from ... import get_db_connection``).
    A whole-session collection imports those modules before the fixture runs, so
    patching only the pool leaves them holding the application pool that
    integration tests never open (a read then raises "pool is not open" and is
    swallowed as a disabled feature; see the W2b triage). Rebind the source and
    every already-imported ``src.server`` module that carries the name.
    """
    from src.server.database import pool as app_pool

    monkeypatch.setattr(app_pool, "get_db_connection", connection)
    for name, module in list(sys.modules.items()):
        if not name.startswith("src.server") or module is app_pool:
            continue
        if getattr(module, "get_db_connection", None) is None:
            continue
        monkeypatch.setattr(module, "get_db_connection", connection)


@pytest_asyncio.fixture
async def refactor_db(test_db_pool, monkeypatch):
    """Point the refactor SQL layer at the migrated test pool, flags on.

    The refactor modules reach Postgres through ``database.pool.get_db_connection``
    (the application's pool, built from DB_* env), which integration tests do not
    open. Patch it -- and the modules that bound the name at import time -- to the
    session pool instead of booting the app lifespan, and enable both flags each
    test exercises (a test may unset one).
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _connection(conn=None):
        async with test_db_pool.connection() as pooled:
            yield pooled

    _patch_db_access(monkeypatch, _connection)
    monkeypatch.setenv("REFACTOR_EVENTS_ENABLED", "1")
    monkeypatch.setenv("REFACTOR_TASK_TREE_ENABLED", "1")
    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
    yield test_db_pool


@pytest_asyncio.fixture
async def cleanup_tables(test_db_pool):
    """Truncate all tables after each test for isolation.

    NOT autouse — only activated when a test explicitly requests it
    (or transitively via seed_user/seed_workspace).
    """
    yield
    async with test_db_pool.connection() as conn:
        async with conn.cursor() as cur:
            for table in _ALL_TABLES:
                await cur.execute(f"TRUNCATE TABLE {table} CASCADE")


@pytest_asyncio.fixture
async def db_conn(test_db_pool):
    """Yield a single async connection from the test pool.

    The connection is used in autocommit mode (pool default).
    """
    async with test_db_pool.connection() as conn:
        yield conn


@pytest_asyncio.fixture
async def patched_get_db_connection(test_db_pool, test_db_uri):
    """Patch get_db_connection to use the test pool instead of production.

    This allows database module functions (workspace.py, user.py, etc.)
    to transparently use the test database. The shared lock session opens on
    it too, fresh per test so no lock outlives the test that took it.
    """
    from contextlib import asynccontextmanager

    import psycopg

    from src.server.database.session_lock import close_shared_lock_session

    @asynccontextmanager
    async def _test_get_db_connection(conn=None):
        if conn is not None:
            yield conn
            return
        async with test_db_pool.connection() as owned:
            yield owned

    # Every module that does a module-level `from .pool import
    # get_db_connection` holds its own local reference, so each one needs its
    # own patch. Which modules are already loaded depends on what the session
    # ran before this test, so a hand-kept list is wrong in both directions
    # (it once missed mcp_tool_schemas, and later every service a plugin
    # write goes through). Sweep every loaded module still holding the pool's
    # function instead. A module first imported during the test takes the
    # patched source on pool.get_db_connection at import.
    import importlib
    import pkgutil
    import sys

    import src.server.database as _database_pkg
    from src.server.database.pool import get_db_connection as _pool_gdc

    for _info in pkgutil.walk_packages(
        _database_pkg.__path__, prefix="src.server.database."
    ):
        importlib.import_module(_info.name)
    holders = [
        module
        for name, module in list(sys.modules.items())
        if name.startswith(("src.", "ptc_agent."))
        and getattr(module, "get_db_connection", None) is _pool_gdc
    ]
    from contextlib import ExitStack

    async def _test_session_connection():
        return await psycopg.AsyncConnection.connect(
            test_db_uri, autocommit=True, prepare_threshold=0
        )

    with ExitStack() as stack:
        for module in holders:
            stack.enter_context(
                patch.object(module, "get_db_connection", _test_get_db_connection)
            )
        stack.enter_context(
            patch("src.server.database.pool.open_session_connection", _test_session_connection)
        )
        await close_shared_lock_session()
        try:
            yield _test_get_db_connection
        finally:
            await close_shared_lock_session()


@pytest_asyncio.fixture
async def seed_response(patched_get_db_connection):
    """Async callable that seeds a response row the trigger-compliant way.

    The terminal-immutability trigger rejects INSERTs with a terminal
    status, so seeding inserts ``in_progress`` and finalizes with an
    UPDATE, the same shape production takes through the lifecycle API.
    """

    async def _seed(
        conversation_response_id: str,
        conversation_thread_id: str,
        turn_index: int,
        status: str = "completed",
        execution_time=None,
    ):
        async with patched_get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO conversation_responses
                        (conversation_response_id, conversation_thread_id,
                         turn_index, status, created_at)
                    VALUES (%s, %s, %s, 'in_progress', NOW())
                    """,
                    (
                        conversation_response_id,
                        conversation_thread_id,
                        turn_index,
                    ),
                )
                await cur.execute(
                    """
                    UPDATE conversation_responses
                    SET status = %s,
                        execution_time = COALESCE(%s, execution_time)
                    WHERE conversation_response_id = %s
                    """,
                    (status, execution_time, conversation_response_id),
                )

    return _seed


@pytest.fixture
def test_user_id() -> str:
    """Deterministic test user ID."""
    return "test-user-integration-001"


@pytest_asyncio.fixture
async def seed_user(patched_get_db_connection, cleanup_tables, test_user_id):
    """Insert a test user and return the user dict.

    Most database modules require a user row to exist (FK constraints).
    """
    from src.server.database.user import create_user

    user = await create_user(
        user_id=test_user_id,
        email="test@example.com",
        name="Test User",
    )
    return user


@pytest_asyncio.fixture
async def seed_workspace(seed_user, patched_get_db_connection):
    """Insert a test workspace and return its dict.

    Depends on seed_user to satisfy FK constraints.
    """
    from src.server.database.workspace import create_workspace

    ws = await create_workspace(
        user_id=seed_user["user_id"],
        name="Test Workspace",
        description="Integration test workspace",
        status="running",
    )
    return ws
