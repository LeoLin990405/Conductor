"""Daemon lease recovery against a real PostgreSQL (the full schema applied).

Run with ``TEST_DB_HOST/TEST_DB_PORT/TEST_DB_NAME/TEST_DB_USER/TEST_DB_PASSWORD``
pointing at a migrated scratch database and ``pytest -m integration``. The default
suite deselects it.
The unit tests mock the cursor, which cannot see a NOT NULL column.
"""

from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
import pytest_asyncio

from tests.integration.db_guard import require_test_database_name

psycopg = pytest.importorskip("psycopg")

pytestmark = pytest.mark.integration


def _dsn() -> str:
    return (
        f"host={os.getenv('TEST_DB_HOST', 'localhost')} "
        f"port={os.getenv('TEST_DB_PORT', '5432')} "
        f"dbname={require_test_database_name()} "
        f"user={os.getenv('TEST_DB_USER', 'postgres')} "
        f"password={os.getenv('TEST_DB_PASSWORD', 'postgres')} "
        f"sslmode={os.getenv('TEST_DB_SSLMODE', 'prefer')}"
    )


@pytest_asyncio.fixture
async def pg(monkeypatch):
    from src.server.database.autonomy import daemons as daemons_db

    try:
        probe = await psycopg.AsyncConnection.connect(_dsn(), autocommit=True)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"postgres unavailable: {exc}")
    async with probe.cursor() as cur:
        await cur.execute("SELECT to_regclass('autonomy_daemons')")
        if (await cur.fetchone())[0] is None:
            pytest.skip("the autonomy schema is not applied to this database")
    daemon_id = str(uuid.uuid4())
    user = f"it-{uuid.uuid4().hex[:8]}"
    async with probe.cursor() as cur:
        await cur.execute(
            "INSERT INTO autonomy_daemons (daemon_id, user_id, name, token_hash, provider) "
            "VALUES (%s, %s, 'it', %s, 'codex')",
            (daemon_id, user, uuid.uuid4().hex),
        )

    @asynccontextmanager
    async def connection(conn=None):
        async with await psycopg.AsyncConnection.connect(_dsn(), autocommit=True) as c:
            yield c

    monkeypatch.setattr(daemons_db, "get_db_connection", connection)
    created: list[str] = []

    async def running_ticket(*, expires: str, error: str = "") -> str:
        ticket_id = str(uuid.uuid4())
        created.append(ticket_id)
        async with probe.cursor() as cur:
            await cur.execute(
                "INSERT INTO autonomy_tickets (ticket_id, user_id, workspace_id, status, "
                "worker_daemon_id, worker_lease_id, worker_lease_expires_at, error) "
                f"VALUES (%s, %s, 'ws', 'running', %s, %s, NOW() + INTERVAL '{expires}', %s)",
                (ticket_id, user, daemon_id, str(uuid.uuid4()), error),
            )
        return ticket_id

    try:
        yield SimpleNamespace(db=daemons_db, probe=probe, running_ticket=running_ticket)
    finally:
        async with probe.cursor() as cur:
            for ticket_id in created:
                await cur.execute(
                    "DELETE FROM autonomy_tickets WHERE ticket_id=%s", (ticket_id,)
                )
            await cur.execute("DELETE FROM autonomy_daemons WHERE daemon_id=%s", (daemon_id,))
        await probe.close()


async def _row(pg, ticket_id):
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "SELECT status, worker_lease_id, error, attention FROM autonomy_tickets "
            "WHERE ticket_id=%s",
            (ticket_id,),
        )
        return await cur.fetchone()


@pytest.mark.asyncio
async def test_an_expired_daemon_lease_is_recovered_on_the_real_schema(pg):
    """``error`` is ``NOT NULL DEFAULT ''``; recovery used to set it to NULL and
    raise on the first expired lease, which starved the scheduler pass."""
    expired = await pg.running_ticket(expires="-1 second", error="old failure")
    live = await pg.running_ticket(expires="1 hour", error="")

    assert await pg.db.recover_expired_daemon_leases() >= 1

    status, lease, error, attention = await _row(pg, expired)
    assert (status, lease, error, attention) == ("queued", None, "", None)
    status, lease, _, _ = await _row(pg, live)
    assert status == "running" and lease is not None
