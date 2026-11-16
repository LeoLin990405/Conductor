"""SQL for daemon live-output sessions and events (migration 140).

Every write goes through :func:`commit_batch`, which runs the lease check, the
session lock, the caller's decision, the session update and the event inserts in
ONE transaction. The decision is made by a pure function on the locked row, so
two workers can never decide against the same state, and a failure anywhere
leaves neither an event nor a changed guard behind.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection

# A stuck row lock or slow statement must fail the batch, not hang the request.
_LOCK_TIMEOUT = "2s"
_STATEMENT_TIMEOUT = "5s"
# Whole expired logs collected per transaction (at most this many times the
# per-log row cap in one statement).
EXPIRE_BATCH = 100
_EXPIRE_STATEMENT_TIMEOUT = "30s"
_EXPIRE_LOCK_KEY = "autonomy_daemon_output_gc"

_SESSION_COLUMNS = (
    "ticket_id, epoch, lease_id, run_id, next_seq, last_batch, tainted, "
    "taint_reason, guard_tail, guard_state"
)
# What a watcher needs from the session: the guard window and state are
# kilobytes it never reads, and it polls several times a second.
_HEAD_COLUMNS = "ticket_id, epoch, lease_id, run_id, next_seq, tainted"

# The lease fence takes the claim row and then the Ticket row, the order the
# report path uses. A revoke updates only the claim row and a stop/lease change
# updates the Ticket row, so either one waits for this transaction and any write
# that lands afterwards re-reads the changed row and is refused.
#
# The claim is locked FOR NO KEY UPDATE, not FOR SHARE: a new share locker joins
# the holders already on the row instead of queuing behind a waiting writer, so
# appends that overlap on one claim (they all wait on the session row below)
# could keep a revoke from ever running. A no-key-update locker queues first in,
# first out, so a revoke waits only for the appends ahead of it and those behind
# it see the revoked row. FOR KEY SHARE would never make the revoke wait, which
# breaks the fence.
CLAIM_LOCK_SQL = """
    SELECT 1 FROM autonomy_daemon_claims c
    WHERE c.daemon_id=%s AND c.ticket_id=%s AND c.lease_id=%s
      AND c.revoked_at IS NULL
    FOR NO KEY UPDATE OF c
"""
TICKET_LOCK_SQL = """
    SELECT 1 FROM autonomy_tickets t
    WHERE t.ticket_id=%s AND t.worker_daemon_id=%s
      AND t.user_id=(SELECT user_id FROM autonomy_daemons WHERE daemon_id=%s)
      AND t.status='running' AND t.worker_lease_id=%s
      AND t.worker_lease_expires_at>clock_timestamp()
    FOR SHARE OF t
"""
EPOCH_SQL = "SELECT nextval('autonomy_daemon_output_epoch_seq')"
# Walks the logs in primary-key order (one index probe each, whatever their
# size) and judges each by its newest row, so a live log is never mistaken for
# an expired one and cannot crowd one out of the batch; the DELETE removes whole
# (ticket, epoch) logs, never single rows (a log with a hole in the middle would
# join the text on either side of it). The EXISTS guard makes an idle pass one
# probe of the created_at index.
EXPIRE_SQL = """
    WITH RECURSIVE logs AS (
        (SELECT ticket_id, epoch FROM autonomy_daemon_output_events
         WHERE EXISTS (SELECT 1 FROM autonomy_daemon_output_events
                       WHERE created_at < NOW() - INTERVAL '1 day')
         ORDER BY ticket_id, epoch LIMIT 1)
        UNION ALL
        SELECT nxt.ticket_id, nxt.epoch FROM logs l CROSS JOIN LATERAL (
            SELECT ticket_id, epoch FROM autonomy_daemon_output_events
            WHERE (ticket_id, epoch) > (l.ticket_id, l.epoch)
            ORDER BY ticket_id, epoch LIMIT 1) nxt
    ), due AS MATERIALIZED (
        SELECT l.ticket_id, l.epoch FROM logs l
        WHERE (SELECT e.created_at FROM autonomy_daemon_output_events e
               WHERE e.ticket_id=l.ticket_id AND e.epoch=l.epoch
               ORDER BY e.seq DESC LIMIT 1) < NOW() - INTERVAL '1 day'
        LIMIT %s
    ), gone AS (
        DELETE FROM autonomy_daemon_output_events e
        USING due WHERE e.ticket_id=due.ticket_id AND e.epoch=due.epoch
    )
    SELECT count(*) FROM due
"""


async def commit_batch(
    *,
    daemon_id: str,
    ticket_id: str,
    lease_id: str,
    allocate: bool,
    decide: Callable[[dict[str, Any], int | None], Any],
) -> tuple[bool, Any]:
    """Apply one daemon batch atomically.

    The lease check locks the claim row (queueing fairly with a revoke) and
    share-locks the Ticket row: a revoke, stop or reassignment waits for this
    transaction, so a lease that is live here cannot be revoked between the
    check and the commit.

    ``allocate`` asks for a fresh epoch from the database-wide sequence, passed
    to ``decide`` with the locked session row (``decide(row, epoch)``). Epochs
    never come from the session row, so they cannot repeat after a session is
    deleted and recreated.

    Returns ``(False, None)`` when the lease is not live. Otherwise
    ``(True, plan)`` where ``plan`` is whatever ``decide`` returned; the plan's
    ``write`` flag and fields say what was persisted. Any exception rolls the
    whole transaction back.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
                await cur.execute(
                    f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"
                )
                await cur.execute(CLAIM_LOCK_SQL, (daemon_id, ticket_id, lease_id))
                if await cur.fetchone() is None:
                    return False, None
                await cur.execute(
                    TICKET_LOCK_SQL, (ticket_id, daemon_id, daemon_id, lease_id)
                )
                if await cur.fetchone() is None:
                    return False, None
                await cur.execute(
                    "INSERT INTO autonomy_daemon_output_sessions (ticket_id) "
                    "VALUES (%s) ON CONFLICT (ticket_id) DO NOTHING",
                    (ticket_id,),
                )
                await cur.execute(
                    f"SELECT {_SESSION_COLUMNS} FROM autonomy_daemon_output_sessions "
                    "WHERE ticket_id=%s FOR UPDATE",
                    (ticket_id,),
                )
                row = await cur.fetchone()
                if row is None:  # pragma: no cover - the insert above guarantees it
                    raise RuntimeError("output session row missing after upsert")
                new_epoch: int | None = None
                if allocate:
                    await cur.execute(EPOCH_SQL)
                    epoch_row = await cur.fetchone()
                    new_epoch = int(next(iter(dict(epoch_row).values())))
                # The decision is pure CPU (the scanner): run it off the event
                # loop so a large batch cannot stall other requests while this
                # transaction holds its locks.
                plan = await asyncio.to_thread(decide, dict(row), new_epoch)
                if not plan.write:
                    return True, plan
                session = plan.session
                if plan.purge_older_epochs:
                    await cur.execute(
                        "DELETE FROM autonomy_daemon_output_events "
                        "WHERE ticket_id=%s AND epoch<%s",
                        (ticket_id, session["epoch"]),
                    )
                await cur.execute(
                    """UPDATE autonomy_daemon_output_sessions SET
                        epoch=%s, lease_id=%s, run_id=%s, next_seq=%s,
                        last_batch=%s, tainted=%s, taint_reason=%s,
                        guard_tail=%s, guard_state=%s, updated_at=NOW()
                      WHERE ticket_id=%s""",
                    (
                        session["epoch"],
                        session["lease_id"],
                        session["run_id"],
                        session["next_seq"],
                        session["last_batch"],
                        session["tainted"],
                        session["taint_reason"],
                        session["guard_tail"],
                        Json(session["guard_state"]),
                        ticket_id,
                    ),
                )
                if plan.rows:
                    await cur.executemany(
                        "INSERT INTO autonomy_daemon_output_events "
                        "(ticket_id, epoch, seq, data) VALUES (%s, %s, %s, %s)",
                        [
                            (ticket_id, session["epoch"], seq, Json(data))
                            for seq, data in plan.rows
                        ],
                    )
                if plan.trim_through > 0:
                    await cur.execute(
                        "DELETE FROM autonomy_daemon_output_events "
                        "WHERE ticket_id=%s AND epoch=%s AND seq<=%s",
                        (ticket_id, session["epoch"], plan.trim_through),
                    )
                return True, plan


async def expire_old_logs(batch: int = EXPIRE_BATCH) -> int:
    """Collect up to ``batch`` logs that are entirely older than a day.

    Runs from the scheduler pass, not the write path, so a backlog can neither
    slow nor fail a run's first batch. Returns the logs collected: ``0`` when
    nothing is due or another instance is collecting (the transaction lock keeps
    a hand-over overlap from making two deletes wait on each other).
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'")
                await cur.execute(
                    f"SET LOCAL statement_timeout = '{_EXPIRE_STATEMENT_TIMEOUT}'"
                )
                await cur.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))",
                    (_EXPIRE_LOCK_KEY,),
                )
                locked = await cur.fetchone()
                if not locked or not locked[0]:
                    return 0
                await cur.execute(EXPIRE_SQL, (batch,))
                row = await cur.fetchone()
    return int(row[0]) if row else 0


async def read_session(ticket_id: str) -> dict[str, Any] | None:
    """The session head (epoch, lease, last sequence, taint), or None when none exists.

    It leaves out the guard window and state; only :func:`commit_batch` reads those.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_HEAD_COLUMNS} FROM autonomy_daemon_output_sessions "
                "WHERE ticket_id=%s",
                (ticket_id,),
            )
            row = await cur.fetchone()
    return dict(row) if row else None


async def read_events(
    ticket_id: str, epoch: int, after_seq: int, limit: int
) -> list[tuple[int, dict[str, Any]]]:
    """Events of one epoch after ``after_seq``, oldest first."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT seq, data FROM autonomy_daemon_output_events "
                "WHERE ticket_id=%s AND epoch=%s AND seq>%s ORDER BY seq LIMIT %s",
                (ticket_id, epoch, after_seq, limit),
            )
            rows = await cur.fetchall()
    return [(int(row["seq"]), row["data"]) for row in rows]
