"""Stop release against conductor_test, rolled back.

Service tests fake the cursor. These call the real statements: a verified
receipt for an older lease must not release the live one, and an archived
ticket's daemon must still be able to confirm the stop. Skipped when that
database is not reachable. Never connects to any other database.
"""

from __future__ import annotations

import socket
import uuid
from contextlib import asynccontextmanager

import pytest

pytestmark = pytest.mark.enable_socket

USER = "ob-tickets-fix3"
OLD_LEASE = "11111111-1111-4111-8111-111111111111"
LIVE_LEASE = "22222222-2222-4222-8222-222222222222"


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


async def _connect():
    import os

    import psycopg

    host = os.getenv("TEST_DB_HOST", "localhost")
    port = int(os.getenv("TEST_DB_PORT", "5432"))
    name = os.getenv("TEST_DB_NAME", "conductor_test")
    user = os.getenv("TEST_DB_USER", "postgres")
    password = os.getenv("TEST_DB_PASSWORD", "postgres")
    if name != "conductor_test":
        pytest.skip("refusing a database that is not conductor_test")
    if not _port_open(host, port):
        pytest.skip("conductor_test is not available")
    try:
        conn = await psycopg.AsyncConnection.connect(
            host=host,
            port=port,
            dbname=name,
            user=user,
            password=password,
            connect_timeout=2,
            autocommit=False,
            application_name="ob-tickets-fix3-test",
        )
    except Exception:
        pytest.skip("conductor_test is not available")
    row = await (await conn.execute("SELECT current_database()")).fetchone()
    if row is None or row[0] != "conductor_test":
        await conn.close()
        pytest.skip("connected database is not conductor_test")
    return conn


async def _schema_ready(conn) -> bool:
    row = await (
        await conn.execute(
            "SELECT to_regclass('public.autonomy_stop_requests') IS NOT NULL"
            " AND to_regclass('public.autonomy_ticket_events') IS NOT NULL"
        )
    ).fetchone()
    if not row or not row[0]:
        return False
    check = await (
        await conn.execute(
            """
            SELECT pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'autonomy_tickets'::regclass
              AND conname = 'autonomy_tickets_status_check'
            """
        )
    ).fetchone()
    return bool(check and "archived" in check[0])


async def _allow_event_actions(conn) -> None:
    """Let this transaction store the new actions even if 131 is not applied."""
    from psycopg import sql

    names = await (
        await conn.execute(
            """
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'autonomy_ticket_events'::regclass
              AND contype = 'c'
              AND pg_get_constraintdef(oid) ILIKE '%%action%%'
            """
        )
    ).fetchall()
    for (name,) in names:
        await conn.execute(
            sql.SQL("ALTER TABLE autonomy_ticket_events DROP CONSTRAINT {}").format(
                sql.Identifier(name)
            )
        )
    await conn.execute(
        """
        ALTER TABLE autonomy_ticket_events
            ADD CONSTRAINT autonomy_ticket_events_action_check
            CHECK (action IN (
                'claimed', 'stopped', 'resumed', 'reassigned', 'archived',
                'stop_acknowledged', 'mandate_revoked'
            ))
        """
    )


def _bind(monkeypatch, conn):
    @asynccontextmanager
    async def get_connection(_conn=None):
        yield conn

    for module_name in (
        "src.server.database.autonomy.stops",
        "src.server.database.autonomy.tickets",
        "src.server.database.autonomy.lines",
    ):
        module = __import__(module_name, fromlist=["get_db_connection"])
        monkeypatch.setattr(module, "get_db_connection", get_connection)


async def _daemon(conn, daemon_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO autonomy_daemons (daemon_id, user_id, name, token_hash, provider)
        VALUES (%s, %s, 'daemon', %s, 'codex')
        """,
        (daemon_id, USER, daemon_id),
    )


async def _running(conn, ticket_id: str, daemon_id: str, lease_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO autonomy_tickets (
            ticket_id, user_id, workspace_id, title, status,
            worker_daemon_id, worker_provider, attempts, revision,
            worker_lease_id, worker_lease_claim_id,
            worker_lease_expires_at, worker_lease_heartbeat_at, started_at
        )
        VALUES (
            %s, %s, 'ws', 'ticket', 'running',
            %s, 'codex', 2, 3,
            %s, %s,
            clock_timestamp() + interval '1 hour', clock_timestamp(),
            clock_timestamp()
        )
        """,
        (ticket_id, USER, daemon_id, lease_id, lease_id),
    )


async def _verified_stop(conn, ticket_id: str, daemon_id: str, lease_id: str) -> None:
    await conn.execute(
        """
        INSERT INTO autonomy_stop_requests (
            ticket_id, user_id, daemon_id, lease_id, mode, verification,
            requested_at, deadline_at, verified_at, reason
        )
        VALUES (
            %s, %s, %s, %s, 'daemon', 'verified',
            clock_timestamp() - interval '1 hour',
            clock_timestamp() - interval '30 minutes',
            clock_timestamp() - interval '30 minutes',
            ''
        )
        """,
        (ticket_id, USER, daemon_id, lease_id),
    )


async def _lease_and_stop(conn, ticket_id: str):
    ticket = await (
        await conn.execute(
            """
            SELECT status, worker_lease_id::text
            FROM autonomy_tickets WHERE ticket_id = %s
            """,
            (ticket_id,),
        )
    ).fetchone()
    stop = await (
        await conn.execute(
            """
            SELECT verification, lease_id::text
            FROM autonomy_stop_requests WHERE ticket_id = %s
            """,
            (ticket_id,),
        )
    ).fetchone()
    return ticket, stop


@pytest.mark.asyncio
async def test_real_sql_stale_verified_stop_does_not_clear_a_live_lease(monkeypatch):
    from src.server.services.autonomy.ownership import TicketOpError, reassign_ticket

    conn = await _connect()
    try:
        await conn.execute("SET lock_timeout = '3s'")
        await conn.execute("SET statement_timeout = '15s'")
        if not await _schema_ready(conn):
            pytest.skip("conductor_test has no archived ticket stop schema")
        await _allow_event_actions(conn)
        _bind(monkeypatch, conn)
        daemon_id = str(uuid.uuid4())
        ticket_id = str(uuid.uuid4())
        await _daemon(conn, daemon_id)
        await _running(conn, ticket_id, daemon_id, LIVE_LEASE)
        await _verified_stop(conn, ticket_id, daemon_id, OLD_LEASE)

        with pytest.raises(TicketOpError):
            await reassign_ticket(user_id=USER, ticket_id=ticket_id, daemon_id=None)

        ticket, stop = await _lease_and_stop(conn, ticket_id)
        assert ticket[0] == "cancelled"
        assert ticket[1] == LIVE_LEASE
        assert stop[0] == "pending"
        assert stop[1] == LIVE_LEASE
    finally:
        await conn.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_real_sql_archived_ticket_can_confirm_the_fresh_stop(monkeypatch):
    from src.server.database.autonomy.stops import acknowledge_stop
    from src.server.services.autonomy.ownership import archive_ticket

    conn = await _connect()
    try:
        await conn.execute("SET lock_timeout = '3s'")
        await conn.execute("SET statement_timeout = '15s'")
        if not await _schema_ready(conn):
            pytest.skip("conductor_test has no archived ticket stop schema")
        await _allow_event_actions(conn)
        _bind(monkeypatch, conn)
        daemon_id = str(uuid.uuid4())
        ticket_id = str(uuid.uuid4())
        await _daemon(conn, daemon_id)
        await _running(conn, ticket_id, daemon_id, LIVE_LEASE)
        await _verified_stop(conn, ticket_id, daemon_id, OLD_LEASE)

        archived = await archive_ticket(user_id=USER, ticket_id=ticket_id)
        assert archived["status"] == "archived"
        ticket, stop = await _lease_and_stop(conn, ticket_id)
        assert ticket[0] == "archived"
        assert ticket[1] == LIVE_LEASE
        assert stop[0] == "pending"
        assert stop[1] == LIVE_LEASE

        result = await acknowledge_stop(
            daemon_id=daemon_id,
            ticket_id=ticket_id,
            lease_id=LIVE_LEASE,
            process_gone=True,
            evidence={"processes": [{"pid": 1, "alive": False}]},
        )
        assert result["outcome"] == "ok"
        assert result["stop"]["verification"] == "verified"
        assert result["stop"]["lease_id"] == LIVE_LEASE
        ticket, stop = await _lease_and_stop(conn, ticket_id)
        assert ticket[1] is None
        assert stop[0] == "verified"
    finally:
        await conn.rollback()
        await conn.close()


@pytest.mark.asyncio
async def test_real_sql_force_rebinds_a_previous_daemons_verified_receipt(monkeypatch):
    from src.server.database.autonomy.stops import force_acknowledge_unverified

    conn = await _connect()
    try:
        await conn.execute("SET lock_timeout = '3s'")
        await conn.execute("SET statement_timeout = '15s'")
        if not await _schema_ready(conn):
            pytest.skip("conductor_test has no archived ticket stop schema")
        await _allow_event_actions(conn)
        _bind(monkeypatch, conn)
        previous = str(uuid.uuid4())
        current = str(uuid.uuid4())
        ticket_id = str(uuid.uuid4())
        expired = str(uuid.uuid4())
        await _daemon(conn, previous)
        await _daemon(conn, current)
        await conn.execute(
            """
            INSERT INTO autonomy_tickets (
                ticket_id, user_id, workspace_id, title, status,
                worker_daemon_id, worker_provider, attempts, revision,
                worker_lease_id, worker_lease_expires_at, started_at
            )
            VALUES (
                %s, %s, 'ws', 'ticket', 'running',
                %s, 'codex', 2, 4,
                %s, clock_timestamp() - interval '1 minute', clock_timestamp()
            )
            """,
            (ticket_id, USER, current, expired),
        )
        await _verified_stop(conn, ticket_id, previous, OLD_LEASE)

        result = await force_acknowledge_unverified(
            user_id=USER, ticket_id=ticket_id, reason="daemon is dead"
        )

        assert result["outcome"] == "ok"
        assert result["stop"]["daemon_id"] == current
        assert result["stop"]["lease_id"] == expired
        assert result["stop"]["verification"] == "verified"
        ticket, _stop = await _lease_and_stop(conn, ticket_id)
        assert ticket[1] is None
        event = await (
            await conn.execute(
                """
                SELECT action, detail->>'from_status'
                FROM autonomy_ticket_events
                WHERE ticket_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (ticket_id,),
            )
        ).fetchone()
        assert event[0] == "stop_acknowledged"
        assert event[1] == "running"
    finally:
        await conn.rollback()
        await conn.close()
