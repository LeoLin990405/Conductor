"""Ticket lines, the ownership audit trail, and reassignment.

A line is a named branch/worktree pair that Tickets group under. The audit
trail (``autonomy_ticket_events``) is written in the same transaction as the
ownership change it records — ``insert_ticket_event`` takes the caller's cursor
for that, with ``record_ticket_event`` as the standalone wrapper.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection
from src.server.database.autonomy.tickets import TICKET_COLUMNS, ticket_from_row

_EVENT_ACTIONS = (
    "claimed",
    "stopped",
    "resumed",
    "reassigned",
    "archived",
    # Forced acknowledgement does not change status. Mandate refusal stamps
    # attention on a ticket that stays cancelled. Both need their own actions
    # so the audit row is not read as a status transition.
    "stop_acknowledged",
    "mandate_revoked",
)
# Anything the scheduler or a daemon could still pick up; reassignment refuses
# terminal Tickets instead of resurrecting them under a new owner.
OPEN_STATUSES = ("queued", "running", "waiting", "blocked", "awaiting_clarification")
TERMINAL_STATUSES = ("completed", "cancelled", "archived")

_LINE_COLUMNS = """
    line_id, user_id, name, branch, worktree_path, status,
    created_at, updated_at
"""


def _line(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    if out.get("line_id"):
        out["line_id"] = str(out["line_id"])
    return out


def _event(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["event_id"] = str(out.get("event_id") or "")
    out["ticket_id"] = str(out.get("ticket_id") or "")
    out["detail"] = dict(out.get("detail") or {})
    return out


def owner_ref(worker_daemon_id: Any) -> str:
    """The canonical owner string for a ticket row: daemon UUID or 'cloud'."""
    return str(worker_daemon_id) if worker_daemon_id else "cloud"


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


async def create_line(
    *, user_id: str, name: str, branch: str = "", worktree_path: str = ""
) -> dict[str, Any]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                INSERT INTO autonomy_lines (user_id, name, branch, worktree_path)
                VALUES (%s, %s, %s, %s)
                RETURNING {_LINE_COLUMNS}
                """,
                (user_id, name.strip()[:120], branch[:200], worktree_path[:500]),
            )
            return _line(await cur.fetchone())  # type: ignore[return-value]


async def get_line(user_id: str, line_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_LINE_COLUMNS} FROM autonomy_lines "
                "WHERE user_id = %s AND line_id = %s",
                (user_id, line_id),
            )
            return _line(await cur.fetchone())


async def list_lines(
    user_id: str, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_LINE_COLUMNS}
                FROM autonomy_lines
                WHERE user_id = %s
                  AND (%s OR status <> 'archived')
                ORDER BY (status = 'archived'), updated_at DESC
                """,
                (user_id, include_archived),
            )
            return [_line(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def archive_line(user_id: str, line_id: str) -> dict[str, Any] | None:
    """Archive a line. Its Tickets keep their rows and stay runnable."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_lines
                SET status = 'archived', updated_at = NOW()
                WHERE user_id = %s AND line_id = %s AND status <> 'archived'
                RETURNING {_LINE_COLUMNS}
                """,
                (user_id, line_id),
            )
            return _line(await cur.fetchone())


async def set_ticket_line(ticket_id: str, line_id: str | None) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_tickets
                SET line_id = %s, revision = revision + 1, updated_at = NOW()
                WHERE ticket_id = %s
                """,
                (line_id, ticket_id),
            )


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


async def insert_ticket_event(
    cur: Any,
    *,
    ticket_id: str,
    user_id: str,
    actor: str,
    action: str,
    from_owner: str = "",
    to_owner: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """Record one ownership event on the caller's cursor/transaction."""
    if action not in _EVENT_ACTIONS:
        raise ValueError(f"unsupported ticket event action {action!r}")
    await cur.execute(
        """
        INSERT INTO autonomy_ticket_events (
            ticket_id, user_id, actor, action, from_owner, to_owner, detail
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            ticket_id,
            user_id,
            actor,
            action,
            from_owner,
            to_owner,
            Json(detail or {}),
        ),
    )


async def record_ticket_event(
    *,
    ticket_id: str,
    user_id: str,
    actor: str,
    action: str,
    from_owner: str = "",
    to_owner: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await insert_ticket_event(
                cur,
                ticket_id=ticket_id,
                user_id=user_id,
                actor=actor,
                action=action,
                from_owner=from_owner,
                to_owner=to_owner,
                detail=detail,
            )


async def list_ticket_events(
    ticket_id: str, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT event_id, ticket_id, user_id, actor, action,
                       from_owner, to_owner, detail, created_at
                FROM autonomy_ticket_events
                WHERE ticket_id = %s
                ORDER BY created_at DESC, event_id DESC
                LIMIT %s OFFSET %s
                """,
                (ticket_id, limit, offset),
            )
            return [_event(row) for row in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Reassignment (the only writer of the owner column besides the claim paths)
# ---------------------------------------------------------------------------


async def reassign_ticket(
    ticket_id: str,
    *,
    user_id: str,
    from_daemon_id: str | None,
    worker_daemon_id: str | None,
    worker_provider: str,
    worker_model: str,
    expected_revision: int,
    expected_status: str,
    expected_lease_id: str | None,
    restart_cancelled: bool = False,
    event: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Swap the owner, fencing the previous one out.

    The revision, status, owner and lease all must match the service snapshot.
    That closes the gap where a daemon claims a queued Ticket after the user's
    read and then has its live lease cleared by a stale reassignment.

    A cancelled Ticket is put back to queued only when ``restart_cancelled``
    is set. The service sets that after the mandate check, so a reassignment
    cannot restart a stopped Ticket on its own. ``event`` is written on this
    transaction when the fence matches.
    """
    restart = bool(restart_cancelled)
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    UPDATE autonomy_tickets
                    SET worker_daemon_id = %s,
                        worker_provider = %s,
                        worker_model = %s,
                        worker_thread_id = NULL,
                        worker_run_id = NULL,
                        worker_lease_id = NULL,
                        worker_lease_claim_id = NULL,
                        worker_lease_expires_at = NULL,
                        worker_lease_heartbeat_at = NULL,
                        status = CASE
                            WHEN status = 'running' THEN 'queued'
                            WHEN status = 'cancelled' AND %s THEN 'queued'
                            ELSE status END,
                        ended_at = CASE
                            WHEN status = 'cancelled' AND %s THEN NULL
                            ELSE ended_at END,
                        attention = CASE
                            WHEN status = 'cancelled' AND %s THEN NULL
                            ELSE attention END,
                        error = CASE
                            WHEN status = 'cancelled' AND %s THEN ''
                            ELSE error END,
                        revision = revision + 1,
                        updated_at = NOW()
                    WHERE ticket_id = %s
                      AND user_id = %s
                      AND worker_daemon_id IS NOT DISTINCT FROM %s
                      AND revision = %s
                      AND status = %s
                      AND worker_lease_id IS NOT DISTINCT FROM %s::uuid
                      AND status = ANY(%s)
                    RETURNING {TICKET_COLUMNS}, worker_lease_id
                    """,
                    (
                        worker_daemon_id,
                        worker_provider,
                        worker_model,
                        restart,
                        restart,
                        restart,
                        restart,
                        ticket_id,
                        user_id,
                        from_daemon_id,
                        expected_revision,
                        expected_status,
                        expected_lease_id,
                        [*OPEN_STATUSES, "cancelled"],
                    ),
                )
                row = await cur.fetchone()
                updated = ticket_from_row(row)
                if updated is not None and event is not None:
                    await insert_ticket_event(cur, **event)
                return updated


# ---------------------------------------------------------------------------
# Line dashboard
# ---------------------------------------------------------------------------


async def ticket_lines_overview(
    user_id: str, *, include_archived: bool = False, ticket_limit: int = 500
) -> dict[str, Any]:
    """Tickets grouped by line, each with status, owner, and last activity.

    ``last_activity_at`` is the greatest of the ticket's ``updated_at`` and
    its latest audit event, so an operator action on an otherwise quiet
    Ticket still reads as activity. With ``include_archived`` false, Tickets
    on archived lines hide along with their line instead of resurfacing as
    ungrouped.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_LINE_COLUMNS}
                FROM autonomy_lines
                WHERE user_id = %s
                  AND (%s OR status <> 'archived')
                ORDER BY (status = 'archived'), updated_at DESC
                """,
                (user_id, include_archived),
            )
            line_rows = [_line(row) for row in await cur.fetchall() if row]
            await cur.execute(
                """
                SELECT t.ticket_id, t.line_id, t.title, t.status, t.attempts,
                       t.worker_provider, t.updated_at,
                       COALESCE(d.name, 'cloud') AS owner,
                       GREATEST(
                           t.updated_at,
                           COALESCE(e.last_event_at, t.updated_at)
                       ) AS last_activity_at
                FROM autonomy_tickets t
                LEFT JOIN autonomy_daemons d ON d.daemon_id = t.worker_daemon_id
                LEFT JOIN autonomy_lines l ON l.line_id = t.line_id
                LEFT JOIN LATERAL (
                    SELECT MAX(created_at) AS last_event_at
                    FROM autonomy_ticket_events e
                    WHERE e.ticket_id = t.ticket_id
                ) e ON TRUE
                WHERE t.user_id = %s
                  AND (%s OR t.status <> 'archived')
                  AND (%s OR l.line_id IS NULL OR l.status <> 'archived')
                ORDER BY t.updated_at DESC
                LIMIT %s
                """,
                (user_id, include_archived, include_archived, ticket_limit),
            )
            ticket_rows = [
                {
                    **dict(row),
                    "ticket_id": str(row["ticket_id"]),
                    "line_id": str(row["line_id"]) if row.get("line_id") else None,
                }
                for row in await cur.fetchall()
            ]

    groups: dict[str | None, dict[str, Any]] = {
        str(line["line_id"]): {"line": line, "tickets": []} for line in line_rows
    }
    ungrouped: dict[str, Any] = {"line": None, "tickets": []}
    for ticket in ticket_rows:
        bucket = groups.get(ticket["line_id"], ungrouped)
        bucket["tickets"].append(
            {
                "ticket_id": ticket["ticket_id"],
                "title": ticket["title"],
                "status": ticket["status"],
                "owner": ticket["owner"],
                "worker_provider": ticket["worker_provider"],
                "attempts": ticket["attempts"],
                "updated_at": ticket["updated_at"],
                "last_activity_at": ticket["last_activity_at"],
            }
        )

    def _finish(group: dict[str, Any]) -> dict[str, Any]:
        tickets = group["tickets"]
        last = max((t["last_activity_at"] for t in tickets if t["last_activity_at"]), default=None)
        return {
            "line": group["line"],
            "tickets": tickets,
            "ticket_count": len(tickets),
            "open_count": sum(
                1 for t in tickets if t["status"] not in TERMINAL_STATUSES
            ),
            "last_activity_at": last,
        }

    return {
        "lines": [_finish(groups[str(line["line_id"])]) for line in line_rows],
        "ungrouped": _finish(ungrouped) if ungrouped["tickets"] else None,
    }
