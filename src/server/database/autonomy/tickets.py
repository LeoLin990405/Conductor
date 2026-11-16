"""Ticket rows: the durable unit of work a Router accepts."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Any, Iterable
from uuid import UUID

from psycopg.errors import LockNotAvailable
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection, open_session_connection
from src.server.utils.pg_sanitize import SafeJson
from src.server.utils.sanitize import sanitize_report_text

TICKET_COLUMNS = """
    ticket_id, user_id, workspace_id, title, scope, status, mandate,
    policy_version, origin_trigger_id, origin_thread_key, router_thread_id,
    worker_thread_id, worker_run_id, worker_provider, worker_model,
    worker_daemon_id, attempts, attention, summary, error, revision, switches,
    context_scope_id, worktree_path, worktree_generation,
    worktree_cleanup_pending, worktree_cleanup_attempts,
    worktree_cleanup_next_retry_at, worktree_branch, worktree_base_sha,
    worktree_base_branch, worktree_repo, worktree_host, worktree_daemon_id,
    worktree_lease_id, worktree_stale_cleanups, created_at, updated_at,
    started_at, ended_at, closed_at, close_reason
"""
_TERMINAL = ("completed", "cancelled", "archived")
# Statuses that may still own a live checkout. Kept here (not imported from
# lines) so Ticket worktree writes do not create a database-layer cycle.
OPEN_STATUSES = ("queued", "running", "waiting", "blocked", "awaiting_clarification")
# Live subscription rows end with the Ticket on a terminal status and on
# failure. ``failed`` stays out of ``_TERMINAL``: ``owns_followup`` still
# treats a failed Ticket as able to own a follow-up.
_ENDS_SUBSCRIPTIONS = (*_TERMINAL, "failed")
_TICKET_COLUMNS = TICKET_COLUMNS
_REPORT_BODY_LIMIT = 64 * 1024
_REPORT_BODY_TRUNCATION = "\n[… report truncated …]"
# Which host owns a Ticket's checkout (``worktree_host``, migration 147). The
# server never removes a daemon-host checkout, and a daemon never gets a server
# path.
WORKTREE_HOST_SERVER = "server"
WORKTREE_HOST_DAEMON = "daemon"
_WORKTREE_HOSTS = (WORKTREE_HOST_SERVER, WORKTREE_HOST_DAEMON)
# The binding columns. They are written only through the Ticket worktree
# helpers below (bind / pin / clear / cleanup-pending), never by update_ticket,
# so a whole-row or whole-mandate write cannot drop or forge a binding.
WORKTREE_COLUMNS = (
    "worktree_path",
    "worktree_generation",
    "worktree_cleanup_pending",
    "worktree_cleanup_attempts",
    "worktree_cleanup_next_retry_at",
    "worktree_branch",
    "worktree_base_sha",
    "worktree_base_branch",
    "worktree_repo",
    "worktree_host",
    "worktree_daemon_id",
    "worktree_lease_id",
    "worktree_stale_cleanups",
)
_WORKTREE_TEXT_LIMIT = 500
_WORKTREE_SHA_LIMIT = 64
# Advisory-lock namespace for ticket_worktree_lock (thread routing uses 733).
_WORKTREE_LOCK_SEED = 7147
# Soft lease while a sweep removes a claimed cleanup row; other sweeps skip it
# until the lease runs out or the row is cleared / rescheduled.
_WORKTREE_CLEANUP_CLAIM_SECONDS = 60.0
# A cleanup that failed this many times is left for an operator: the retry
# sweep stops claiming it (the row stays pending and visible).
MAX_WORKTREE_CLEANUP_ATTEMPTS = 20


def _report_body(value: str | None) -> str | None:
    """Store a bounded, credential-scrubbed final report body."""
    if value is None:
        return None
    # Vendor and worker output can contain lone UTF-16 surrogates. Normalize
    # them at this storage boundary so byte sizing and persistence stay safe.
    value = value.encode("utf-8", errors="replace").decode("utf-8")
    raw = value.encode("utf-8")
    truncated = len(raw) > _REPORT_BODY_LIMIT
    bounded = raw[:_REPORT_BODY_LIMIT].decode("utf-8", errors="ignore") if truncated else value
    clean = sanitize_report_text(bounded, limit=0)
    encoded = clean.encode("utf-8")
    marker = _REPORT_BODY_TRUNCATION.encode("utf-8")
    if not truncated and len(encoded) <= _REPORT_BODY_LIMIT:
        return clean
    prefix = encoded[: _REPORT_BODY_LIMIT - len(marker)].decode("utf-8", errors="ignore")
    return prefix + _REPORT_BODY_TRUNCATION

# Subscription end reasons (CHECK-enforced, migration 127).
END_UNSUBSCRIBED = "unsubscribed"
END_REASSIGNED = "reassigned"
END_TICKET_ENDED = "ticket_ended"

# The Worker slot pool: one advisory key for the two paths that start a Worker
# against the cap, a daemon claim and a cloud launch, so each one counts and flips
# under the same guard. (A follow-up delivered to a Ticket sets it running without
# the cap; see CLOUD-WORKERS.md, "Concurrency".) A daemon claim holds it as a
# session lock (``worker.claim_daemon_ticket``), a cloud launch as a transaction
# lock (``update_ticket(slot_limit=...)``); the two conflict. A claim only tries
# it. A launch waits for it, but only that long and as the first statement of its
# own transaction, so it holds no row lock of its own when it waits and no wait
# cycle can form with the row locks the writes take. (A caller may still hold the
# instruction-handoff lock of its own Ticket around the call; the holders a launch
# can wait on there are a claim, which never takes that lock, and another launch,
# which holds at most its own Ticket's.)
WORKER_SLOT_LOCK = 0x5A17_0001
# How long a launch waits for the guard, and afterwards for its Ticket's row. The
# guard is held for milliseconds, even by a daemon that polls hard, so a wait this
# long ends only when something is stuck. It also caps how long a stuck writer on
# one Ticket can keep every claim and launch out.
_SLOT_LOCK_WAIT = "1s"
# What "running" means to the cap, defined once for the claim and the launch.
RUNNING_WORKERS_SQL = (
    "SELECT COUNT(*) AS running FROM autonomy_tickets WHERE status = 'running' "
    "AND mandate->>'orchestration_only' IS DISTINCT FROM 'true'"
)


class WorkerSlotsFull(Exception):
    """Every Worker slot is taken (or the slot guard is busy); nothing was written."""


def is_open_accepted_speculative_plan(
    row: dict[str, Any], *, user_id: str
) -> bool:
    """True when this Ticket is still a reviewable speculative plan.

    Reuse may store a later signal only while the row matches this check:
    owned by ``user_id``, still ``waiting``, speculative, and the recorded
    outcome is still ``accepted``. A discard, a completion, or any other
    settlement fails the check so the signal is not consumed.
    """
    if str(row.get("user_id") or "") != user_id:
        return False
    if str(row.get("status") or "") != "waiting":
        return False
    mandate = row.get("mandate") if isinstance(row.get("mandate"), dict) else {}
    if not mandate.get("speculative"):
        return False
    outcome = mandate.get("speculative_outcome")
    return isinstance(outcome, dict) and outcome.get("outcome") == "accepted"


def is_ended_speculative_plan(row: dict[str, Any]) -> bool:
    """A completed speculative Ticket must not take another signal.

    Discard and a quiet drop both finish the Ticket while ``speculative``
    stays set. Selecting that row revives a PTC Worker or stores a daemon
    input that never launches. An ordinary completed Ticket still owns its
    Thread.
    """
    if str(row.get("status") or "") != "completed":
        return False
    mandate = row.get("mandate") if isinstance(row.get("mandate"), dict) else {}
    return bool(mandate.get("speculative"))


def owns_followup(row: dict[str, Any]) -> bool:
    """False once a follow-up must not resume this Ticket.

    A terminal Ticket never returns to running: a signal that lands after the
    status write stays pending for a new Eval instead of reviving the old one.
    That covers the cancelled work and the ended speculative plans the
    speculative line fenced, and every other completed Ticket under the
    lifecycle line's rule. The check is the in-process twin of the delivery
    admission guard.
    """
    if str(row.get("status") or "") in _TERMINAL:
        return False
    mandate = row.get("mandate")
    normalized = dict(row)
    normalized["mandate"] = dict(mandate) if isinstance(mandate, dict) else {}
    return not is_ended_speculative_plan(normalized)


_APPEND_PENDING_TRIGGER = """
UPDATE autonomy_threads
SET pending_trigger_ids = (
        SELECT ARRAY(
            SELECT DISTINCT unnest(
                pending_trigger_ids || ARRAY[%s]::TEXT[]
            )
        )
    ),
    eval_expiries = 0,
    updated_at = NOW()
WHERE thread_key = %s
"""


def _json_safe(value: Any) -> Any:
    """Coerce a value tree into data ``json.dumps`` accepts.

    Handoff state is assembled from raw report rows, whose timestamps arrive
    as datetimes that ``Json`` refuses to bind; anything else exotic falls
    back to str so one odd leaf cannot abort a reassignment after ownership
    already moved.
    """
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def ticket_from_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in (
        "ticket_id",
        "router_thread_id",
        "worker_thread_id",
        "worker_run_id",
        "worker_daemon_id",
        "context_scope_id",
        "worktree_daemon_id",
        "worktree_lease_id",
    ):
        if out.get(key):
            out[key] = str(out[key])
    out["mandate"] = dict(out.get("mandate") or {})
    out["attention"] = dict(out["attention"]) if out.get("attention") else None
    if "worktree_stale_cleanups" in out:
        out["worktree_stale_cleanups"] = [
            dict(note) for note in out.get("worktree_stale_cleanups") or []
        ]
    return out


_ticket = ticket_from_row


def _uuid_text(value: Any) -> str:
    return str(value) if value else ""


async def create_ticket(
    *,
    user_id: str,
    workspace_id: str,
    title: str,
    scope: str,
    mandate: dict[str, Any],
    policy_version: str,
    origin_trigger_id: str,
    origin_thread_key: str,
    router_thread_id: str | None = None,
    worker_provider: str = "",
    worker_model: str = "",
    worker_daemon_id: str | None = None,
    initial_status: str = "queued",
    context_scope_id: str | None = None,
) -> dict[str, Any]:
    if initial_status not in {"queued", "awaiting_clarification"}:
        raise ValueError("initial_status must be queued or awaiting_clarification")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if not context_scope_id:
                # Closed by default: a new Ticket starts in its own isolated
                # scope, so sharing requires an explicit scope choice.
                await cur.execute(
                    """
                    INSERT INTO autonomy_context_scopes (user_id, name, kind)
                    VALUES (%s, 'isolated:' || gen_random_uuid()::text, 'isolated')
                    RETURNING scope_id
                    """,
                    (user_id,),
                )
                scope_row = await cur.fetchone()
                context_scope_id = str(scope_row["scope_id"])
            await cur.execute(
                f"""
                INSERT INTO autonomy_tickets (
                    user_id, workspace_id, title, scope, mandate,
                    policy_version, origin_trigger_id, origin_thread_key,
                    router_thread_id, worker_provider, worker_model,
                    worker_daemon_id, status, context_scope_id
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING {_TICKET_COLUMNS}
                """,
                (
                    user_id,
                    workspace_id,
                    title,
                    scope,
                    Json(mandate),
                    policy_version,
                    origin_trigger_id,
                    origin_thread_key,
                    router_thread_id,
                    worker_provider,
                    worker_model,
                    worker_daemon_id,
                    initial_status,
                    context_scope_id,
                ),
            )
            return _ticket(await cur.fetchone())  # type: ignore[return-value]


async def get_ticket(
    ticket_id: str, *, conn=None, for_update: bool = False
) -> dict[str, Any] | None:
    """One ticket; ``for_update`` locks the row until the caller's transaction
    ends so a status revalidation cannot race a concurrent completion."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_TICKET_COLUMNS} FROM autonomy_tickets WHERE ticket_id = %s"
                + (" FOR UPDATE" if for_update else ""),
                (ticket_id,),
            )
            return _ticket(await cur.fetchone())


async def find_tickets_by_id_prefix(
    user_id: str, prefix: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    """The caller's Tickets whose id starts with ``prefix``, newest first.

    ``prefix`` must already be lower-case hex and dashes (the route checks),
    so it carries no LIKE wildcard. Used to resolve a short id typed in a CLI.
    """
    async with get_db_connection() as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT ticket_id, title, status, updated_at FROM autonomy_tickets"
                " WHERE user_id = %s AND ticket_id::text LIKE %s"
                " ORDER BY updated_at DESC, ticket_id LIMIT %s",
                (user_id, f"{prefix}%", limit),
            )
            rows = await cur.fetchall()
    return [{**dict(row), "ticket_id": str(row["ticket_id"])} for row in rows]


def _worktree_path_arg(worktree_path: str) -> str:
    path = str(worktree_path or "").strip()
    if not path:
        raise ValueError("worktree_path is required")
    if len(path) > _WORKTREE_TEXT_LIMIT:
        # Truncating would name a different directory; refuse instead.
        raise ValueError("worktree_path is too long")
    return path


def _base_sha_arg(base_sha: str) -> str:
    sha = str(base_sha or "").strip()
    if not sha:
        raise ValueError("base_sha is required")
    if len(sha) > _WORKTREE_SHA_LIMIT:
        # Truncating would pin a different (or no) commit; refuse instead.
        raise ValueError("base_sha is too long")
    return sha


# The binding in the row belongs to a daemon that no longer holds the Ticket:
# a daemon-host checkout whose daemon is not the Ticket's current worker (the
# Ticket was handed to the server or to another daemon after the old lease
# was lost). Only such a binding, at the generation the caller observed, may
# be replaced by a new owner's bind.
_DEPARTED_DAEMON_BINDING = (
    "worktree_host = 'daemon'",
    "worktree_path <> ''",
    "worktree_generation = %(replace_generation)s",
    "worktree_daemon_id IS DISTINCT FROM worker_daemon_id",
)


def _worktree_bind_fences(
    host: str, *, expected_revision: int | None, replace_generation: int | None = None
) -> list[str]:
    """WHERE conjuncts every bind statement (lock read and write) repeats.

    A daemon bind checks the live lease in the same statement as the write, so
    a lease that expired, was revoked, or moved to another daemon after the
    HTTP layer looked at it cannot still bind. With ``replace_generation`` the
    slot may also hold a departed daemon's binding at exactly that generation.
    """
    slot = [
        # A pending cleanup refuses every other path; the same host (and
        # daemon, below) binding the same path takes that checkout over.
        "(NOT worktree_cleanup_pending OR worktree_path = %(worktree_path)s)",
        # Never silently replace a different live checkout, nor take over one
        # another host (or another daemon) owns under the same path string.
        "worktree_path IN ('', %(worktree_path)s)",
        "(worktree_path = '' OR worktree_host = %(host)s)",
    ]
    fences = [
        "ticket_id = %(ticket_id)s",
        "status = ANY(%(open_statuses)s)",
        "closed_at IS NULL",
        "worktree_base_sha = %(base_sha)s",
    ]
    if host == WORKTREE_HOST_SERVER:
        fences.append("worker_daemon_id IS NULL")
        if expected_revision is not None:
            fences.append("revision = %(expected_revision)s")
    else:
        fences += [
            "worker_daemon_id = %(daemon_id)s::uuid",
            "worker_lease_id = %(lease_id)s::uuid",
            "worker_lease_expires_at > clock_timestamp()",
            "NOT EXISTS (SELECT 1 FROM autonomy_daemon_claims c"
            " WHERE c.lease_id = %(lease_id)s::uuid AND c.revoked_at IS NOT NULL)",
        ]
        slot.append("(worktree_path = '' OR worktree_daemon_id = %(daemon_id)s::uuid)")
    if replace_generation is None:
        return fences + slot
    return [
        *fences,
        f"(({' AND '.join(slot)}) OR ({' AND '.join(_DEPARTED_DAEMON_BINDING)}))",
    ]


def _is_departed_daemon_binding(row: dict[str, Any], replace_generation: int | None) -> bool:
    return (
        replace_generation is not None
        and row.get("worktree_host") == WORKTREE_HOST_DAEMON
        and bool(row.get("worktree_path"))
        and int(row.get("worktree_generation") or 0) == replace_generation
        and _uuid_text(row.get("worktree_daemon_id")) != _uuid_text(row.get("worker_daemon_id"))
    )


# A departed daemon's cleanup notes are settled by its report; one that never
# reports again would otherwise grow the list forever (N68). The oldest are
# dropped first: a later report for a dropped note is then answered as
# unknown, and nothing else reads them.
MAX_STALE_CLEANUP_NOTES = 20


def capped_stale_cleanup_notes(existing: Any, note: dict[str, Any]) -> list[Any]:
    notes = [*(existing if isinstance(existing, list) else []), note]
    return notes[-MAX_STALE_CLEANUP_NOTES:]


def _stale_cleanup_note(row: dict[str, Any]) -> dict[str, Any]:
    """The replaced binding, as the old daemon's cleanup report will name it."""
    return {
        "daemon_id": _uuid_text(row.get("worktree_daemon_id")) or None,
        "worktree_path": str(row.get("worktree_path") or ""),
        "worktree_generation": int(row.get("worktree_generation") or 0),
        "lease_id": _uuid_text(row.get("worktree_lease_id")) or None,
    }


def _where(fences: Iterable[str]) -> str:
    return "\n                   AND ".join(fences)


async def pin_ticket_worktree_base(
    ticket_id: str, *, base_sha: str, base_branch: str
) -> dict[str, Any] | None:
    """Record the durable base commit once; the first write wins.

    Every later claim, bind, and recreated checkout starts from this commit,
    so two claims racing before the first bind cannot pick different bases.
    No revision bump: pinning is not an ownership change. Returns the current
    row (whose pin may be an earlier one), or None when the Ticket is missing.
    """
    sha = _base_sha_arg(base_sha)
    params = {
        "ticket_id": ticket_id,
        "base_sha": sha,
        "base_branch": str(base_branch or "").strip()[:_WORKTREE_TEXT_LIMIT],
    }
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    UPDATE autonomy_tickets
                       SET worktree_base_sha = %(base_sha)s,
                           worktree_base_branch = %(base_branch)s
                     WHERE ticket_id = %(ticket_id)s
                       AND worktree_base_sha = ''
                    RETURNING {_TICKET_COLUMNS}
                    """,
                    params,
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        f"""
                        SELECT {_TICKET_COLUMNS}
                          FROM autonomy_tickets
                         WHERE ticket_id = %(ticket_id)s
                        """,
                        params,
                    )
                    row = await cur.fetchone()
                return _ticket(row)


async def bind_ticket_worktree(
    ticket_id: str,
    *,
    worktree_path: str,
    branch: str,
    base_sha: str,
    repo: str,
    host: str,
    expected_revision: int | None = None,
    daemon_id: str | None = None,
    lease_id: str | None = None,
    replace_generation: int | None = None,
) -> dict[str, Any] | None:
    """Bind a checkout to an open Ticket in one fenced write.

    The row is locked with the same fences the write repeats: open and not
    closed, the pinned base matches, and the slot is empty or already this
    checkout. A pending cleanup refuses any other path, but the same host
    (and, for ``host='daemon'``, the same daemon) binding the same path takes
    the checkout over: pending, attempts and next retry are reset and the
    generation advances, so a sweep that claimed the old attempt fences out. ``host='server'`` additionally requires a
    server-owned Ticket (``worker_daemon_id IS NULL``, so a local-to-daemon
    reassignment fences out a server path) and the caller's revision when
    given; ``host='daemon'`` requires this daemon's live, unrevoked lease.

    Replaying the same path, host, branch, and lease returns the row unchanged.
    Any other successful bind advances ``worktree_generation``, the attempt
    every cleanup fences on. Returns None when fenced out.

    ``replace_generation`` lets a new owner replace a departed daemon's
    binding (a daemon-host checkout whose daemon is no longer the Ticket's
    worker, pending or not) when the row is still at that generation; a stale
    read of the row therefore fences out. The replaced binding is appended to
    ``worktree_stale_cleanups`` in the same write, so the old daemon's later
    cleanup report can be settled by ``resolve_stale_worktree_cleanup``.
    """
    if host not in _WORKTREE_HOSTS:
        raise ValueError(f"unknown worktree host {host!r}")
    path = _worktree_path_arg(worktree_path)
    branch_name = str(branch or "").strip()
    if not branch_name:
        raise ValueError("branch and base_sha are required")
    sha = _base_sha_arg(base_sha)
    if replace_generation is not None:
        replace_generation = int(replace_generation)
        if replace_generation < 1:
            raise ValueError("replace_generation names a bound attempt (>= 1)")
    if host == WORKTREE_HOST_DAEMON:
        if not daemon_id or not lease_id:
            raise ValueError("a daemon bind needs daemon_id and lease_id")
        daemon_id, lease_id = str(UUID(str(daemon_id))), str(UUID(str(lease_id)))
    elif daemon_id is not None or lease_id is not None:
        raise ValueError("a server bind carries no daemon lease")
    params = {
        "ticket_id": ticket_id,
        "open_statuses": list(OPEN_STATUSES),
        "base_sha": sha,
        "worktree_path": path,
        "branch": branch_name[:_WORKTREE_TEXT_LIMIT],
        "repo": str(repo or "")[:_WORKTREE_TEXT_LIMIT],
        "host": host,
        "daemon_id": daemon_id,
        "lease_id": lease_id,
        "expected_revision": expected_revision,
        "replace_generation": replace_generation,
    }
    where = _where(
        _worktree_bind_fences(
            host, expected_revision=expected_revision, replace_generation=replace_generation
        )
    )
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    SELECT {_TICKET_COLUMNS}
                      FROM autonomy_tickets
                     WHERE {where}
                       FOR UPDATE
                    """,
                    params,
                )
                current = await cur.fetchone()
                if current is None:
                    return None
                if (
                    not current.get("worktree_cleanup_pending")
                    and current.get("worktree_path") == path
                    and current.get("worktree_host") == host
                    and current.get("worktree_branch") == params["branch"]
                    and _uuid_text(current.get("worktree_lease_id")) == (lease_id or "")
                ):
                    return _ticket(current)
                stale = ""
                if _is_departed_daemon_binding(current, replace_generation):
                    params["stale_notes"] = SafeJson(
                        capped_stale_cleanup_notes(
                            current.get("worktree_stale_cleanups"),
                            _stale_cleanup_note(current),
                        )
                    )
                    stale = "worktree_stale_cleanups = %(stale_notes)s::jsonb,"
                await cur.execute(
                    f"""
                    UPDATE autonomy_tickets
                       SET {stale}
                           worktree_path = %(worktree_path)s,
                           worktree_branch = %(branch)s,
                           worktree_repo = %(repo)s,
                           worktree_host = %(host)s,
                           worktree_daemon_id = %(daemon_id)s::uuid,
                           worktree_lease_id = %(lease_id)s::uuid,
                           worktree_cleanup_pending = FALSE,
                           worktree_cleanup_attempts = 0,
                           worktree_cleanup_next_retry_at = NULL,
                           worktree_generation = worktree_generation + 1,
                           revision = revision + 1,
                           updated_at = NOW()
                     WHERE {where}
                    RETURNING {_TICKET_COLUMNS}
                    """,
                    params,
                )
                return _ticket(await cur.fetchone())


def _worktree_attempt_fences(
    *,
    lease_id: str | None,
    host: str | None = None,
    daemon_id: str | None = None,
) -> list[str]:
    """The (path, generation[, lease, host, daemon]) key a cleanup write fences on.

    A report for an older attempt cannot match a newer bind of the same path,
    because every bind advances the generation. ``host`` / ``daemon_id`` keep
    one host's (or one daemon's) cleanup off a binding another one owns.
    """
    fences = [
        "ticket_id = %(ticket_id)s",
        "worktree_path = %(worktree_path)s",
        "worktree_generation = %(worktree_generation)s",
    ]
    if lease_id is not None:
        fences.append("worktree_lease_id = %(lease_id)s::uuid")
    if host is not None:
        fences.append("worktree_host = %(host)s")
    if daemon_id is not None:
        fences.append("worktree_daemon_id = %(daemon_id)s::uuid")
    return fences


def _attempt_params(
    ticket_id: str,
    worktree_path: str,
    worktree_generation: int,
    lease_id: str | None,
    *,
    host: str | None = None,
    daemon_id: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    if host is not None and host not in _WORKTREE_HOSTS:
        raise ValueError(f"unknown worktree host {host!r}")
    return {
        "ticket_id": ticket_id,
        "worktree_path": _worktree_path_arg(worktree_path),
        "worktree_generation": int(worktree_generation),
        "lease_id": str(UUID(str(lease_id))) if lease_id is not None else None,
        "host": host,
        "daemon_id": str(UUID(str(daemon_id))) if daemon_id is not None else None,
        **extra,
    }


def _attempt_where(params: dict[str, Any]) -> str:
    return _where(
        _worktree_attempt_fences(
            lease_id=params["lease_id"],
            host=params["host"],
            daemon_id=params["daemon_id"],
        )
    )


async def clear_ticket_worktree_binding(
    ticket_id: str,
    *,
    worktree_path: str,
    worktree_generation: int,
    lease_id: str | None = None,
    host: str | None = None,
    daemon_id: str | None = None,
) -> bool:
    """Forget a removed checkout, for this exact attempt only.

    Keeps the base pin (a recreated checkout starts from the same commit) and
    the generation (the next bind still advances past this attempt). With
    ``host`` / ``daemon_id`` the binding must also belong to that host/daemon.
    """
    if not str(worktree_path or "").strip():
        return False  # no binding to fence on
    params = _attempt_params(
        ticket_id,
        worktree_path,
        worktree_generation,
        lease_id,
        host=host,
        daemon_id=daemon_id,
    )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_tickets
                   SET worktree_path = '',
                       worktree_branch = '',
                       worktree_repo = '',
                       worktree_host = '',
                       worktree_daemon_id = NULL,
                       worktree_lease_id = NULL,
                       worktree_cleanup_pending = FALSE,
                       worktree_cleanup_attempts = 0,
                       worktree_cleanup_next_retry_at = NULL,
                       revision = revision + 1,
                       updated_at = NOW()
                 WHERE {_attempt_where(params)}
                """,
                params,
            )
            return cur.rowcount > 0


async def record_worktree_cleanup_pending(
    ticket_id: str,
    *,
    worktree_path: str,
    worktree_generation: int,
    lease_id: str | None = None,
    delay_seconds: float = 0.0,
    host: str | None = None,
    daemon_id: str | None = None,
) -> bool:
    """Make a failed remove durable so the retry sweep picks it up.

    Writes only the pending columns, never the binding: a failed cleanup for
    an older attempt cannot overwrite a newer checkout's path or branch. With
    ``host`` / ``daemon_id`` the binding must also belong to that host/daemon.
    """
    if not str(worktree_path or "").strip():
        return False  # no binding to fence on
    params = _attempt_params(
        ticket_id,
        worktree_path,
        worktree_generation,
        lease_id,
        host=host,
        daemon_id=daemon_id,
        delay_seconds=max(0.0, float(delay_seconds)),
    )
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_tickets
                   SET worktree_cleanup_pending = TRUE,
                       worktree_cleanup_attempts = worktree_cleanup_attempts + 1,
                       worktree_cleanup_next_retry_at =
                           clock_timestamp() + make_interval(secs => %(delay_seconds)s),
                       updated_at = NOW()
                 WHERE {_attempt_where(params)}
                """,
                params,
            )
            return cur.rowcount > 0


async def cancel_worktree_cleanup_pending(
    ticket_id: str,
    *,
    worktree_path: str,
    worktree_generation: int,
    host: str,
    daemon_id: str | None = None,
) -> bool:
    """Drop a cleanup that is no longer owed because the Ticket is open again.

    A reopened Ticket that still owns its host's checkout keeps it: pending,
    attempts and next retry are reset and the binding stays. Fenced on this
    exact attempt (path, generation), the owning host (and daemon for
    ``host='daemon'``), an open, unclosed Ticket, and a Ticket that is not
    orphaned from that host (a server checkout needs a server-owned Ticket;
    a daemon checkout needs the Ticket to be on that same daemon). Returns
    False when nothing pending matched.
    """
    if host not in _WORKTREE_HOSTS:
        raise ValueError(f"unknown worktree host {host!r}")
    if host == WORKTREE_HOST_DAEMON:
        if not daemon_id:
            raise ValueError("a daemon cleanup cancel needs daemon_id")
    elif daemon_id is not None:
        raise ValueError("a server checkout has no daemon_id")
    if not str(worktree_path or "").strip():
        return False  # no binding to fence on
    params = _attempt_params(
        ticket_id,
        worktree_path,
        worktree_generation,
        None,
        host=host,
        daemon_id=daemon_id,
        open_statuses=list(OPEN_STATUSES),
    )
    fences = [
        *_worktree_attempt_fences(lease_id=None, host=host, daemon_id=params["daemon_id"]),
        "worktree_cleanup_pending",
        "status = ANY(%(open_statuses)s)",
        "closed_at IS NULL",
        (
            "worker_daemon_id IS NULL"
            if host == WORKTREE_HOST_SERVER
            else "worker_daemon_id = worktree_daemon_id"
        ),
    ]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_tickets
                   SET worktree_cleanup_pending = FALSE,
                       worktree_cleanup_attempts = 0,
                       worktree_cleanup_next_retry_at = NULL,
                       updated_at = NOW()
                 WHERE {_where(fences)}
                """,
                params,
            )
            return cur.rowcount > 0


_STALE_CLEANUP_OUTCOMES = ("removed", "failed")


async def resolve_stale_worktree_cleanup(
    ticket_id: str,
    *,
    daemon_id: str,
    worktree_path: str,
    worktree_generation: int,
    outcome: str,
) -> bool:
    """Settle a departed daemon's report about a checkout a new owner replaced.

    True when ``worktree_stale_cleanups`` holds a note for exactly this daemon,
    path and generation. ``outcome='removed'`` drops that note; ``'failed'``
    keeps it (the daemon retries and reports again). False when no note
    matches (an unknown, already settled, or forged report); nothing changes.
    The live binding is never touched.
    """
    if outcome not in _STALE_CLEANUP_OUTCOMES:
        raise ValueError(f"unknown cleanup outcome {outcome!r}")
    if not daemon_id:
        raise ValueError("a stale cleanup report needs daemon_id")
    if not str(worktree_path or "").strip():
        return False  # no binding to match
    params = _attempt_params(
        ticket_id, worktree_path, worktree_generation, None, daemon_id=daemon_id
    )
    key = (params["daemon_id"], params["worktree_path"], params["worktree_generation"])

    def _matches(note: Any) -> bool:
        if not isinstance(note, dict):
            return False
        try:
            generation = int(note.get("worktree_generation"))
        except (TypeError, ValueError):
            return False
        daemon = _uuid_text(note.get("daemon_id"))
        return (daemon, str(note.get("worktree_path") or ""), generation) == key

    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT worktree_stale_cleanups
                      FROM autonomy_tickets
                     WHERE ticket_id = %(ticket_id)s
                       FOR UPDATE
                    """,
                    params,
                )
                row = await cur.fetchone()
                notes = list((row or {}).get("worktree_stale_cleanups") or [])
                if not any(_matches(note) for note in notes):
                    return False
                if outcome == "failed":
                    return True
                params["remaining"] = SafeJson([n for n in notes if not _matches(n)])
                await cur.execute(
                    """
                    UPDATE autonomy_tickets
                       SET worktree_stale_cleanups = %(remaining)s::jsonb,
                           updated_at = NOW()
                     WHERE ticket_id = %(ticket_id)s
                    """,
                    params,
                )
                return cur.rowcount > 0


async def claim_worktree_cleanup_pending(
    *,
    host: str,
    limit: int = 20,
    lease_seconds: float = _WORKTREE_CLEANUP_CLAIM_SECONDS,
) -> list[dict[str, Any]]:
    """Lease due cleanup-pending rows of one host for a retry sweep.

    ``FOR UPDATE SKIP LOCKED`` plus pushing the next retry by the lease keeps
    concurrent sweeps on other server workers off the same row while one is
    removing it. The returned rows carry the path and generation to fence on.
    Rows that already failed ``MAX_WORKTREE_CLEANUP_ATTEMPTS`` times are not
    claimed again.
    """
    if host not in _WORKTREE_HOSTS:
        raise ValueError(f"unknown worktree host {host!r}")
    params = {
        "host": host,
        "limit": max(1, min(200, int(limit))),
        "lease_seconds": max(1.0, float(lease_seconds)),
        "max_attempts": MAX_WORKTREE_CLEANUP_ATTEMPTS,
    }
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_tickets
                   SET worktree_cleanup_next_retry_at =
                           clock_timestamp() + make_interval(secs => %(lease_seconds)s),
                       updated_at = NOW()
                 WHERE ticket_id IN (
                    SELECT ticket_id
                      FROM autonomy_tickets
                     WHERE worktree_cleanup_pending
                       AND worktree_path <> ''
                       AND worktree_host = %(host)s
                       AND worktree_cleanup_attempts < %(max_attempts)s
                       AND (worktree_cleanup_next_retry_at IS NULL
                            OR worktree_cleanup_next_retry_at <= clock_timestamp())
                     ORDER BY worktree_cleanup_next_retry_at NULLS FIRST, ticket_id
                     LIMIT %(limit)s
                       FOR UPDATE SKIP LOCKED
                 )
                RETURNING {_TICKET_COLUMNS}
                """,
                params,
            )
            return [_ticket(row) for row in await cur.fetchall() if row]


async def reschedule_worktree_cleanup_pending(
    ticket_id: str,
    *,
    worktree_path: str,
    worktree_generation: int,
    delay_seconds: float,
) -> bool:
    """Count another failed retry and push the next one out (pending rows only)."""
    if not str(worktree_path or "").strip():
        return False  # no binding to fence on
    params = _attempt_params(
        ticket_id,
        worktree_path,
        worktree_generation,
        None,
        delay_seconds=max(0.0, float(delay_seconds)),
    )
    fences = [*_worktree_attempt_fences(lease_id=None), "worktree_cleanup_pending"]
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_tickets
                   SET worktree_cleanup_attempts = worktree_cleanup_attempts + 1,
                       worktree_cleanup_next_retry_at =
                           clock_timestamp() + make_interval(secs => %(delay_seconds)s),
                       updated_at = NOW()
                 WHERE {_where(fences)}
                """,
                params,
            )
            return cur.rowcount > 0


@asynccontextmanager
async def ticket_worktree_lock(ticket_id: str, *, timeout_seconds: float = 30.0):
    """Serialize one Ticket's checkout bind, validation, removal, and clear.

    The server runs several worker processes, so an in-process lock is not
    enough: this is a session advisory lock on a dedicated connection, held
    for the block, outside row-lock order (same shape as
    ``thread_routing_lock``). Raises ``TimeoutError`` when not acquired.
    """
    db = await open_session_connection()
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    try:
        while True:
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, %s))",
                    (str(ticket_id), _WORKTREE_LOCK_SEED),
                )
                acquired = bool((await cur.fetchone() or (False,))[0])
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("the Ticket worktree is busy; retry later")
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, %s))",
                    (str(ticket_id), _WORKTREE_LOCK_SEED),
                )
    finally:
        await db.close()


@asynccontextmanager
async def thread_routing_lock(thread_key: str, *, timeout_seconds: float = 30.0):
    """Serialize Thread-ownership routing against reassignment for one Thread.

    Intake's live-subscriber read + input insert and the Router's reassignment
    transition both take this lock, so a follow-up is never routed by a
    snapshot taken before a handoff (and a handoff never misses a follow-up
    inserted against the previous owner). It is a session-level advisory lock,
    not a row lock: it sits outside row-lock order, so it cannot deadlock with
    ticket updates that also touch subscription rows (a completion ends its
    subscriptions while holding the ticket row).
    """
    db = await open_session_connection()
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    try:
        acquired = False
        while not acquired:
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 733))",
                    (thread_key,),
                )
                acquired = bool((await cur.fetchone() or (False,))[0])
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("thread routing is busy; retry the request")
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            async with db.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 733))",
                    (thread_key,),
                )
    finally:
        await db.close()


async def get_ticket_by_origin_trigger(trigger_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_TICKET_COLUMNS} FROM autonomy_tickets WHERE origin_trigger_id = %s LIMIT 1",
                (trigger_id,),
            )
            return _ticket(await cur.fetchone())


async def get_ticket_by_worker_thread(worker_thread_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_TICKET_COLUMNS} FROM autonomy_tickets WHERE worker_thread_id = %s",
                (worker_thread_id,),
            )
            return _ticket(await cur.fetchone())


async def list_tickets(
    user_id: str,
    *,
    statuses: Iterable[str] | None = None,
    source_id: str | None = None,
    schedule_id: str | None = None,
    attention: bool = False,
    q: str | None = None,
    sort: str = "updated",
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    params: list[Any] = [user_id]
    where = "user_id = %s"
    states = tuple(statuses or ())
    if states:
        where += " AND status = ANY(%s)"
        params.append(list(states))
    if source_id:
        where += " AND mandate->>'source_id' = %s"
        params.append(source_id)
    if schedule_id:
        where += " AND mandate->>'schedule_id' = %s"
        params.append(schedule_id)
    if attention:
        where += " AND attention IS NOT NULL"
    if q:
        where += (
            " AND (title ILIKE %s OR scope ILIKE %s"
            " OR origin_thread_key ILIKE %s OR summary ILIKE %s"
            " OR error ILIKE %s)"
        )
        needle = f"%{q}%"
        params.extend([needle, needle, needle, needle, needle])
    params.extend([limit, offset])
    order_by = {
        "updated": "updated_at DESC",
        "created": "created_at DESC",
        "title": "COALESCE(NULLIF(title, ''), origin_thread_key) ASC",
    }.get(sort, "updated_at DESC")
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_TICKET_COLUMNS}
                FROM autonomy_tickets
                WHERE {where}
                ORDER BY {order_by}
                LIMIT %s OFFSET %s
                """,
                params,
            )
            return [_ticket(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def counts(user_id: str) -> dict[str, int]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE attention IS NOT NULL) AS attention,
                    COUNT(*) FILTER (WHERE status = 'running') AS running,
                    COUNT(*) FILTER (WHERE status = 'queued') AS queued,
                    COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                    COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                    COUNT(*) FILTER (WHERE status = 'blocked') AS blocked
                FROM autonomy_tickets
                WHERE user_id = %s
                """,
                (user_id,),
            )
            row = await cur.fetchone() or {}
            return {
                key: int(row.get(key) or 0)
                for key in ("attention", "running", "queued", "completed", "failed", "blocked")
            }


async def _require_free_worker_slot(cur: Any, slot_limit: int) -> None:
    """Take the slot guard for this transaction, or refuse a full pool.

    The lock lasts until the caller's transaction ends, so the count and the
    write that makes the Ticket running are one step to every other claimer.
    It is waited for rather than tried: a daemon that polls fast would win every
    try, and queued waiters are served before a later try, so waiting is what
    keeps a launch from starving while slots are free. Keep this the first
    statement of the transaction, so the wait holds no row lock. The one lock a
    caller may hold meanwhile, the Ticket's instruction handoff lock, is never
    taken by a claim, and another launch holds at most its own Ticket's; those
    two are all that can hold the guard while waiting. The
    timeout set here also covers the row wait of the write that follows.
    """
    await cur.execute(f"SET LOCAL lock_timeout = '{_SLOT_LOCK_WAIT}'")
    await cur.execute("SELECT pg_advisory_xact_lock(%s)", (WORKER_SLOT_LOCK,))
    await cur.execute(RUNNING_WORKERS_SQL)
    if int((await cur.fetchone())["running"]) >= slot_limit:
        raise WorkerSlotsFull(f"all {slot_limit} Worker slots are in use")


@asynccontextmanager
async def _lock_wait_is_a_refusal(slot_limit: int | None):
    """A launch that waited out its lock timeout is refused, not failed."""
    try:
        yield
    except LockNotAvailable as exc:
        if slot_limit is None:
            raise
        raise WorkerSlotsFull(
            "the Worker slot guard or the Ticket row stayed locked"
        ) from exc


async def update_ticket(
    ticket_id: str,
    *,
    expected_status: str | None = None,
    expected_revision: int | None = None,
    expected_thread_id: str | None = None,
    expected_run_id: str | None = None,
    event: dict[str, Any] | None = None,
    slot_limit: int | None = None,
    preserve_updated_at: datetime | None = None,
    **fields: Any,
) -> dict[str, Any] | None:
    """Update one ticket, fenced by status, revision, and Worker thread when given.

    Ownership writes pass ``expected_revision`` so a stale stop or archive
    cannot land on a newer row. Switch and report paths pass
    ``expected_thread_id`` so a line that has already moved is not overwritten.
    ``event`` is inserted on this transaction only when the fence matches, so
    the audit row cannot commit without the status change.

    ``slot_limit`` marks the write as a Worker launch (queued to running) that
    may start only while fewer than that many Tickets run. The count is the one
    a daemon claim makes, under the same guard and in this transaction, so two
    launches, or a launch and a claim, cannot both take the last slot. A full
    pool, or a guard or row that stays locked past ``_SLOT_LOCK_WAIT``, raises
    ``WorkerSlotsFull`` and writes nothing.

    No ``worktree_*`` column is writable here: the binding is written only
    through :func:`bind_ticket_worktree` and the pin / clear / cleanup-pending
    helpers, which carry the open-state, revision, lease, and attempt fences.

    ``preserve_updated_at`` keeps the quiet clock. ``updated_at`` is not a
    caller-writable column — every ordinary write moves it to now — but the
    stuck scan reads it for a queued, waiting, blocked, or not-yet-started
    running Ticket. A watchdog attention flag is not progress. Leaving the
    clock at now would hide that stall on the next scan, and the notification
    retain would delete the alert, including a banner that has not been shown.
    """
    if slot_limit is not None and not (
        fields.get("status") == "running" and expected_status == "queued"
    ):
        raise ValueError("slot_limit only fences a queued -> running launch")
    worktree_fields = sorted(key for key in fields if key.startswith("worktree_"))
    if worktree_fields:
        raise ValueError(
            f"{', '.join(worktree_fields)}: worktree_* fields are written only "
            "through the Ticket worktree helpers (bind_ticket_worktree and friends)"
        )
    if not fields:
        return await get_ticket(ticket_id)
    allowed = {
        "title",
        "scope",
        "status",
        "mandate",
        "router_thread_id",
        "worker_thread_id",
        "worker_run_id",
        "attempts",
        "attention",
        "summary",
        "error",
        "started_at",
        "ended_at",
        "closed_at",
        "close_reason",
    }
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"unsupported ticket field {key!r}")
        if key in ("mandate", "attention"):
            sets.append(f"{key} = %s")
            params.append(Json(value) if value is not None else None)
        else:
            sets.append(f"{key} = %s")
            params.append(value)
    if preserve_updated_at is not None:
        # Not NOW(): this write must not look like the Ticket moved.
        sets.append("updated_at = %s")
        params.append(preserve_updated_at)
    else:
        sets.append("updated_at = NOW()")
    sets.append("revision = revision + 1")
    where = "ticket_id = %s"
    params.append(ticket_id)
    if expected_status is not None:
        where += " AND status = %s"
        params.append(expected_status)
    if expected_revision is not None:
        where += " AND revision = %s"
        params.append(expected_revision)
    if expected_thread_id is not None:
        where += " AND worker_thread_id = %s"
        params.append(expected_thread_id)
    if expected_run_id is not None:
        where += " AND worker_run_id = %s"
        params.append(expected_run_id)
    async with _lock_wait_is_a_refusal(slot_limit), get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                if slot_limit is not None:
                    await _require_free_worker_slot(cur, slot_limit)
                await cur.execute(
                    f"""
                    UPDATE autonomy_tickets SET {", ".join(sets)}
                    WHERE {where}
                    RETURNING {_TICKET_COLUMNS}
                    """,
                    params,
                )
                row = await cur.fetchone()
                if row is not None and event is not None:
                    from src.server.database.autonomy.lines import insert_ticket_event

                    await insert_ticket_event(cur, **event)
                if row is not None and fields.get("status") in _ENDS_SUBSCRIPTIONS:
                    # Intake routes by active subscription rows, so a terminal or
                    # failed Ticket must end them here. ``failed`` is not in
                    # ``_TERMINAL``; follow-up ownership is unchanged.
                    await cur.execute(
                        """
                        UPDATE autonomy_subscriptions
                        SET ended_at = NOW(), end_reason = %s
                        WHERE ticket_id = %s AND ended_at IS NULL
                        """,
                        (END_TICKET_ENDED, ticket_id),
                    )
                return _ticket(row)


async def touch_isolated_worker_heartbeat(
    ticket_id: str,
    *,
    thread_id: str,
    run_id: str,
    heartbeat_at: str,
) -> bool:
    """Atomically refresh mandate.cloud_worker.heartbeat_at for the live run.

    A full-mandate replace is unsafe: a heartbeat that read the mandate before
    the start marker was written could write the marker back to false. This
    update only touches the heartbeat field, so every other marker (notably
    ``container_started``) is preserved regardless of write ordering.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_tickets
                SET mandate = jsonb_set(
                        jsonb_set(
                            COALESCE(mandate, '{}'::jsonb),
                            '{cloud_worker}',
                            COALESCE(mandate->'cloud_worker', '{}'::jsonb),
                            true
                        ),
                        '{cloud_worker,heartbeat_at}',
                        to_jsonb(%s::text),
                        true
                    ),
                    updated_at = NOW(),
                    revision = revision + 1
                WHERE ticket_id = %s
                  AND status = 'running'
                  AND worker_thread_id = %s
                  AND worker_run_id = %s
                """,
                (heartbeat_at, ticket_id, thread_id, run_id),
            )
            return bool(cur.rowcount)


async def update_mandate_record(
    ticket_id: str,
    key: str,
    *,
    set_fields: dict[str, Any] | None = None,
    remove_fields: Iterable[str] = (),
    expected_status: str | None = None,
    expected_thread_id: str | None = None,
    expected_revision: int | None = None,
    touch: bool = True,
) -> dict[str, Any] | None:
    """Merge fields into one record of the Ticket's mandate.

    The write is targeted, so it leaves every other key of the mandate alone:
    a full-mandate replace would drop what another writer added. The record
    is read and changed in the same statement, so a field removed here is
    gone and a field set here is the newer one.

    ``expected_revision`` fences the write on the revision after the sibling
    write that just landed (the input row, or the delivered stamp). Fencing
    on the revision from before that sibling write refuses the update, because
    those writes increment the revision themselves.

    ``touch=False`` is an observe-only write (a shadow record): it leaves
    ``revision`` and ``updated_at`` as they are, so no writer fenced on the
    revision is refused and no staleness clock is reset by it.
    """
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        raise ValueError("mandate key must be a plain identifier")
    removed = [str(name) for name in remove_fields]
    fields = dict(set_fields or {})
    params: list[Any] = [key, key, removed, Json(fields), ticket_id]
    where = "ticket_id = %s"
    if expected_status is not None:
        where += " AND status = %s"
        params.append(expected_status)
    if expected_thread_id is not None:
        where += " AND worker_thread_id = %s"
        params.append(expected_thread_id)
    if expected_revision is not None:
        where += " AND revision = %s"
        params.append(expected_revision)
    touched = ", updated_at = NOW(), revision = revision + 1" if touch else ""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_tickets
                SET mandate = jsonb_set(
                        COALESCE(mandate, '{{}}'::jsonb),
                        ARRAY[%s]::text[],
                        (COALESCE(mandate->%s, '{{}}'::jsonb) - %s::text[])
                            || %s::jsonb,
                        true
                    ){touched}
                WHERE {where}
                RETURNING {_TICKET_COLUMNS}
                """,
                params,
            )
            row = await cur.fetchone()
            return _ticket(row) if row is not None else None


async def fail_ticket_with_report(
    ticket_id: str,
    *,
    error: str,
    summary: str,
    expected_status: str | None = None,
    expected_revision: int | None = None,
    expected_thread_id: str | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Commit a failed transition and its failure report in one transaction.

    The isolated Worker's pre-thread failure path needs both rows to land
    together: a committed failure without its report would make a retried
    effect skip the already-failed Ticket and never record why it failed.
    Once the status UPDATE returns a row, the same transaction ends live
    subscriptions, matching ``update_ticket``.
    """
    sets = [
        "status = %s",
        "error = %s",
        "ended_at = NOW()",
        "updated_at = NOW()",
        "revision = revision + 1",
    ]
    params: list[Any] = ["failed", error[:1000]]
    where = "ticket_id = %s"
    params.append(ticket_id)
    if expected_status is not None:
        where += " AND status = %s"
        params.append(expected_status)
    if expected_revision is not None:
        where += " AND revision = %s"
        params.append(expected_revision)
    if expected_thread_id is not None:
        where += " AND worker_thread_id = %s"
        params.append(expected_thread_id)
    if expected_run_id is not None:
        where += " AND worker_run_id = %s"
        params.append(expected_run_id)
    async with get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_tickets SET {", ".join(sets)}
                WHERE {where}
                RETURNING {_TICKET_COLUMNS}
                """,
                params,
            )
            row = await cur.fetchone()
            if row is None:
                return None
            await cur.execute(
                """
                UPDATE autonomy_subscriptions
                SET ended_at = NOW(), end_reason = %s
                WHERE ticket_id = %s AND ended_at IS NULL
                """,
                (END_TICKET_ENDED, ticket_id),
            )
            await cur.execute(
                """
                INSERT INTO autonomy_ticket_reports (ticket_id, state, summary, question)
                VALUES (%s, 'failed', %s, NULL)
                """,
                (ticket_id, summary[:2000]),
            )
            return _ticket(row)


async def record_switch(
    ticket_id: str,
    *,
    expected_status: str,
    expected_model: tuple[str, str],
    record: dict[str, Any],
    handoff_text: str,
    launch_effect: tuple[str, str] | None = None,
) -> dict[str, Any] | None:
    """Append a model switch, move the Ticket to the new model and stage the handoff.

    Fenced by status and current provider/model so two racing switches cannot both apply.
    A running Ticket goes back to ``queued`` with its thread, run and daemon lease
    dropped in the same statement: a native thread's provider cannot change, and an old
    lease must not be able to report a completion. ``launch_effect`` (user_id, kind)
    is inserted in the same transaction so the replacement launch cannot be lost.
    """
    to = record["to"]
    # The pool is autocommit: without an explicit transaction the requeue and its
    # launch effect would commit separately and a failed insert strands the Ticket.
    async with get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_tickets
                   SET switches = switches || %s::jsonb,
                       worker_provider = %s, worker_model = %s,
                       mandate = jsonb_set(mandate, '{{handoff}}', to_jsonb(%s::text)),
                       status = CASE WHEN status = 'running' THEN 'queued' ELSE status END,
                       worker_thread_id = CASE WHEN status = 'running' THEN NULL ELSE worker_thread_id END,
                       worker_run_id = CASE WHEN status = 'running' THEN NULL ELSE worker_run_id END,
                       worker_lease_id = CASE WHEN status = 'running' THEN NULL ELSE worker_lease_id END,
                       worker_lease_claim_id = CASE WHEN status = 'running' THEN NULL ELSE worker_lease_claim_id END,
                       worker_lease_expires_at = CASE WHEN status = 'running' THEN NULL ELSE worker_lease_expires_at END,
                       updated_at = NOW(), revision = revision + 1
                 WHERE ticket_id = %s AND status = %s
                   AND worker_provider = %s AND worker_model = %s
                RETURNING {_TICKET_COLUMNS}
                """,
                (
                    Json([record]),
                    to["provider"],
                    to["model"],
                    handoff_text,
                    ticket_id,
                    expected_status,
                    expected_model[0],
                    expected_model[1],
                ),
            )
            row = _ticket(await cur.fetchone())
            if row is not None and launch_effect is not None and expected_status == "running":
                from src.server.database.autonomy.effects import insert_effect

                await insert_effect(
                    cur,
                    user_id=launch_effect[0],
                    kind=launch_effect[1],
                    idempotency_key=f"autonomy:switch:{ticket_id}:{len(row.get('switches') or [])}",
                    payload={"ticket_id": ticket_id},
                )
            return row


async def clear_handoff_if(ticket_id: str, handoff_text: str) -> bool:
    """Drop the staged handoff only if it is still the delivered one."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_tickets
                   SET mandate = mandate - 'handoff', revision = revision + 1
                 WHERE ticket_id = %s AND mandate->>'handoff' = %s
                """,
                (ticket_id, handoff_text),
            )
            return cur.rowcount > 0


async def complete_discarded_plan(
    ticket_id: str,
    *,
    expected_status: str,
    mandate: dict[str, Any],
    ended_at: Any,
    summary: str = "",
) -> dict[str, Any] | None:
    """Complete a speculative plan and end its subscriptions together.

    Intake routes a Thread signal to whatever Ticket still subscribes to it.
    Marking the plan completed while its subscription rows stay live leaves it
    selected: a PTC Worker is resumed, and a daemon Ticket stores the signal
    with no launch. The rows are soft-ended (``ended_at`` /
    ``END_TICKET_ENDED``) like every other terminal path, so the plan's
    reports stay in thread history and the subscription audit listing. The
    status write and the subscription end commit in one transaction. A lost
    race against ``expected_status`` applies neither change.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    UPDATE autonomy_tickets
                    SET status = 'completed',
                        summary = %s,
                        attention = NULL,
                        mandate = %s,
                        ended_at = %s,
                        updated_at = NOW(),
                        revision = revision + 1
                    WHERE ticket_id = %s AND status = %s
                    RETURNING {_TICKET_COLUMNS}
                    """,
                    (summary, Json(mandate), ended_at, ticket_id, expected_status),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                await cur.execute(
                    """
                    UPDATE autonomy_subscriptions
                    SET ended_at = NOW(), end_reason = %s
                    WHERE ticket_id = %s AND ended_at IS NULL
                    """,
                    (END_TICKET_ENDED, ticket_id),
                )
                await _release_undelivered_trigger_inputs(cur, ticket_id)
                return _ticket(row)


async def _release_undelivered_trigger_inputs(cur: Any, ticket_id: str) -> None:
    """Put signals that never reached a worker back on the eval queue.

    The ticket row lock is the same one delivery admission takes. When
    discard wins, an input inserted a moment earlier must not stay stranded
    on the completed plan, and it must not resume that plan later. Marking
    the row delivered is what stops the daemon resume claim.
    """
    await cur.execute(
        """
        SELECT i.input_id, i.trigger_id, tr.thread_key
        FROM autonomy_ticket_inputs i
        JOIN autonomy_triggers tr ON tr.trigger_id = i.trigger_id
        WHERE i.ticket_id = %s
          AND i.delivered_at IS NULL
          AND i.kind = 'trigger'
        """,
        (ticket_id,),
    )
    for item in await cur.fetchall():
        await cur.execute(
            _APPEND_PENDING_TRIGGER,
            (item["trigger_id"], item["thread_key"]),
        )
        await cur.execute(
            """
            UPDATE autonomy_ticket_inputs
            SET delivered_at = COALESCE(delivered_at, NOW())
            WHERE input_id = %s
            """,
            (item["input_id"],),
        )


async def admit_followup_delivery(ticket_id: str) -> dict[str, Any] | None:
    """Set a ticket running only while it still accepts a follow-up.

    The row lock is taken before the prior status is read, so the status this
    admission overwrites is the status under the lock, not a statement-snapshot
    value: a concurrent admission that changed the status before ours is what we
    report, and a failed dispatch restores exactly what this delivery replaced.
    The guarded update then refuses every terminal state (completed, cancelled
    or archived), not only a discarded plan: a Ticket that settled by any path
    stays settled, and the signal stays pending for a new Eval.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT status
                    FROM autonomy_tickets
                    WHERE ticket_id = %s
                    FOR UPDATE
                    """,
                    (ticket_id,),
                )
                locked = await cur.fetchone()
                if locked is None:
                    return None
                prior_status = str(locked.get("status") or "")
                await cur.execute(
                    f"""
                    UPDATE autonomy_tickets
                    SET status = 'running',
                        attention = NULL,
                        updated_at = NOW(),
                        revision = revision + 1
                    WHERE ticket_id = %s
                      AND status NOT IN ('completed', 'cancelled', 'archived')
                    RETURNING {_TICKET_COLUMNS}
                    """,
                    (ticket_id,),
                )
                updated = await cur.fetchone()
                if updated is None:
                    return None
                row = _ticket(updated)
                row["prior_status"] = prior_status
                return row



async def fail_unisolated_cloud_worker(
    ticket: dict[str, Any], *, error: str
) -> dict[str, Any] | None:
    """Fence the fail-closed transition to the exact cloud-launch snapshot.

    A row that fails ends its live subscriptions in the same transaction.
    """
    status = str(ticket.get("status") or "")
    revision = ticket.get("revision")
    resumable_cloud_ticket = status == "queued" or (
        status == "running"
        and bool(ticket.get("worker_thread_id"))
        and ticket.get("worker_run_id") is None
    )
    if not resumable_cloud_ticket or not isinstance(revision, int):
        return None
    if ticket.get("worker_daemon_id") is not None:
        return None
    ticket_id = str(ticket["ticket_id"])
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    UPDATE autonomy_tickets
                    SET status = 'failed', error = %s, ended_at = NOW(),
                        updated_at = NOW(),
                        revision = revision + 1
                    WHERE ticket_id = %s
                      AND status = %s
                      AND revision = %s
                      AND worker_daemon_id IS NULL
                      AND worker_thread_id IS NOT DISTINCT FROM %s
                      AND worker_run_id IS NOT DISTINCT FROM %s
                      AND (status = 'queued' OR
                           (status = 'running' AND worker_thread_id IS NOT NULL
                            AND worker_run_id IS NULL))
                      AND mandate->>'orchestration_only' IS DISTINCT FROM 'true'
                      AND mandate->>'orchestration_child' IS DISTINCT FROM 'true'
                      AND COALESCE(mandate->>'kind', '') NOT IN
                          ('main_agent_root', 'main_agent_child')
                    RETURNING {_TICKET_COLUMNS}
                    """,
                    (
                        error,
                        ticket_id,
                        status,
                        revision,
                        ticket.get("worker_thread_id"),
                        ticket.get("worker_run_id"),
                    ),
                )
                row = await cur.fetchone()
                if row is not None:
                    await cur.execute(
                        """
                        UPDATE autonomy_subscriptions
                        SET ended_at = NOW(), end_reason = %s
                        WHERE ticket_id = %s AND ended_at IS NULL
                        """,
                        (END_TICKET_ENDED, ticket_id),
                    )
                return _ticket(row)




async def mark_input_handed_back(input_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE autonomy_ticket_inputs
                SET handed_back = TRUE
                WHERE input_id = %s AND handed_back = FALSE
                RETURNING input_id, ticket_id, trigger_id, kind, text, handed_back
                """,
                (input_id,),
            )
            row = await cur.fetchone()
            if row is not None:
                await cur.execute(
                    "UPDATE autonomy_tickets SET revision = revision + 1 WHERE ticket_id = %s",
                    (row["ticket_id"],),
                )
            return dict(row) if row else None


async def _insert_input_row(
    cur: Any,
    *,
    ticket_id: str,
    trigger_id: str | None,
    kind: str,
    text: str,
) -> dict[str, Any]:
    await cur.execute(
        """
        INSERT INTO autonomy_ticket_inputs (ticket_id, trigger_id, kind, text)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (ticket_id, trigger_id) WHERE trigger_id IS NOT NULL
        DO NOTHING
        RETURNING input_id, ticket_id, trigger_id, kind, text,
                  delivered_at, read_at, created_at
        """,
        (ticket_id, trigger_id, kind, text),
    )
    row = await cur.fetchone()
    ticket_revision = None
    if row is not None:
        await cur.execute(
            """
            UPDATE autonomy_tickets
            SET revision = revision + 1
            WHERE ticket_id = %s
            RETURNING revision
            """,
            (ticket_id,),
        )
        revised = await cur.fetchone()
        if isinstance(revised, dict):
            ticket_revision = revised.get("revision")
        elif revised is not None:
            ticket_revision = revised[0]
    if row is None and trigger_id is not None:
        await cur.execute(
            """
            SELECT input_id, ticket_id, trigger_id, kind, text,
                   delivered_at, read_at, created_at
            FROM autonomy_ticket_inputs
            WHERE ticket_id = %s AND trigger_id = %s
            """,
            (ticket_id, trigger_id),
        )
        row = await cur.fetchone()
    out = dict(row or {})
    out["input_id"] = str(out.get("input_id") or "")
    out["ticket_id"] = str(out.get("ticket_id") or "")
    # The revision after this insert. A later mandate fence must use this
    # value: the Ticket's revision from before the insert no longer matches.
    if ticket_revision is not None:
        out["ticket_revision"] = int(ticket_revision)
    return out


async def add_input(
    *,
    ticket_id: str,
    trigger_id: str | None,
    kind: str,
    text: str,
    only_if_owning: bool = False,
) -> dict[str, Any] | None:
    """Insert one input. ``None`` when ``only_if_owning`` and the ticket lost the thread.

    The ownership check and the insert share a transaction and a row lock, so
    a discard that commits first is visible and writes nothing. Callers then
    leave the trigger pending instead of reporting a successful attach.
    """
    async with get_db_connection() as conn:
        if only_if_owning:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        """
                        SELECT ticket_id, status, mandate
                        FROM autonomy_tickets
                        WHERE ticket_id = %s
                        FOR UPDATE
                        """,
                        (ticket_id,),
                    )
                    locked = await cur.fetchone()
                    if locked is None or not owns_followup(dict(locked)):
                        return None
                    return await _insert_input_row(
                        cur,
                        ticket_id=ticket_id,
                        trigger_id=trigger_id,
                        kind=kind,
                        text=text,
                    )
        async with conn.cursor(row_factory=dict_row) as cur:
            return await _insert_input_row(
                cur,
                ticket_id=ticket_id,
                trigger_id=trigger_id,
                kind=kind,
                text=text,
            )


async def get_input(input_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT input_id, ticket_id, trigger_id, kind, text,
                       delivered_at, read_at, created_at
                FROM autonomy_ticket_inputs
                WHERE input_id = %s
                """,
                (input_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            out = dict(row)
            out["input_id"] = str(out["input_id"])
            out["ticket_id"] = str(out["ticket_id"])
            return out


async def mark_input_delivered(input_id: str) -> int | None:
    """Stamp an instruction delivered once a worker session has taken it.

    Follow-up delivery uses ``claim_input_delivery``, which also fences the
    owning ticket. Instructions recorded at launch still use this stamp.

    Returns the Ticket revision after the stamp. The stamp increments the
    revision, so a mandate fence taken before this call no longer matches.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE autonomy_ticket_inputs SET delivered_at = COALESCE(delivered_at, NOW()) WHERE input_id = %s",
                (input_id,),
            )
            await cur.execute(
                """
                UPDATE autonomy_tickets SET revision = revision + 1
                WHERE ticket_id = (
                    SELECT ticket_id FROM autonomy_ticket_inputs WHERE input_id = %s
                )
                RETURNING revision
                """,
                (input_id,),
            )
            revised = await cur.fetchone()
            if revised is None:
                return None
            value = revised["revision"] if isinstance(revised, dict) else revised[0]
            return int(value) if value is not None else None


async def discard_undelivered_input(input_id: str) -> bool:
    """Delete an instruction no Worker has taken yet (a rolled-back follow-up)."""
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM autonomy_ticket_inputs"
                " WHERE input_id = %s AND delivered_at IS NULL",
                (input_id,),
            )
            return bool(cur.rowcount)


async def claim_input_delivery(
    input_id: str, expected_ticket_id: str, *, conn=None
) -> dict[str, Any] | None:
    """Fence one delivery on current ownership, returning the claim timestamp.

    The guarded UPDATE commits only while the input still belongs to the
    expected ticket and is undelivered. A reassignment that moved the row
    first, or a retried effect for an input someone else already delivered,
    claims nothing — the caller skips instead of leaking text onto the wrong
    Worker thread or marking another ticket's pending input delivered.
    """
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE autonomy_ticket_inputs
                SET delivered_at = NOW()
                WHERE input_id = %s
                  AND ticket_id = %s
                  AND delivered_at IS NULL
                RETURNING input_id, ticket_id, delivered_at
                """,
                (input_id, expected_ticket_id),
            )
            row = await cur.fetchone()
            if row is not None:
                await cur.execute(
                    """
                    UPDATE autonomy_tickets SET revision = revision + 1
                    WHERE ticket_id = %s
                    """,
                    (expected_ticket_id,),
                )
            return dict(row) if row else None


async def release_input_delivery(
    input_id: str, expected_ticket_id: str, claimed_at: Any, *, conn=None
) -> bool:
    """Undo a claim whose dispatch failed, so the retried effect re-sends.

    Only a row still carrying this exact claim timestamp is cleared: a release
    cannot unset a newer claim made by another path.
    """
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            await cur.execute(
                """
                UPDATE autonomy_ticket_inputs
                SET delivered_at = NULL
                WHERE input_id = %s
                  AND ticket_id = %s
                  AND delivered_at = %s
                """,
                (input_id, expected_ticket_id, claimed_at),
            )
            if cur.rowcount > 0:
                await cur.execute(
                    """
                    UPDATE autonomy_tickets SET revision = revision + 1
                    WHERE ticket_id = %s
                    """,
                    (expected_ticket_id,),
                )
            return cur.rowcount > 0


async def requeue_input_trigger(input_id: str, *, conn=None) -> str | None:
    """Put the Trigger behind a delivered input back on its Thread's eval queue.

    A delivery claim that lost the row lock to a terminal settlement is
    neither released nor stranded: the input keeps its delivery mark (nothing
    may double-send it) and its Trigger returns to pending so a new Eval can
    route the signal to a live Ticket. Inputs without a Trigger (human
    answers) have nothing to re-queue and return None.
    """
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT i.trigger_id, tr.thread_key
                FROM autonomy_ticket_inputs i
                JOIN autonomy_triggers tr ON tr.trigger_id = i.trigger_id
                WHERE i.input_id = %s
                """,
                (input_id,),
            )
            row = await cur.fetchone()
            if row is None or row.get("trigger_id") is None:
                return None
            await cur.execute(
                _APPEND_PENDING_TRIGGER, (row["trigger_id"], row["thread_key"])
            )
            return str(row["trigger_id"])


async def requeue_terminal_trigger_input(
    input_id: str, expected_ticket_id: str
) -> bool:
    """Atomically return a terminal Ticket's undelivered Trigger to its queue.

    The input row lock, the Thread queue append and the delivered stamp commit
    in one transaction: a retry after a failure can neither queue the Trigger
    twice nor lose it. Only an undelivered ``trigger`` row still owned by the
    Ticket is re-queued; any other input is a no-op.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT i.ticket_id, i.trigger_id, i.kind, i.delivered_at,
                           tr.thread_key
                    FROM autonomy_ticket_inputs i
                    JOIN autonomy_triggers tr ON tr.trigger_id = i.trigger_id
                    WHERE i.input_id = %s
                    FOR UPDATE OF i
                    """,
                    (input_id,),
                )
                row = await cur.fetchone()
                if (
                    row is None
                    or str(row.get("ticket_id") or "") != expected_ticket_id
                    or row.get("delivered_at") is not None
                    or str(row.get("kind") or "") != "trigger"
                    or row.get("trigger_id") is None
                ):
                    return False
                await cur.execute(
                    _APPEND_PENDING_TRIGGER,
                    (row["trigger_id"], row["thread_key"]),
                )
                await cur.execute(
                    """
                    UPDATE autonomy_ticket_inputs
                    SET delivered_at = NOW()
                    WHERE input_id = %s
                      AND ticket_id = %s
                      AND delivered_at IS NULL
                    """,
                    (input_id, expected_ticket_id),
                )
                if cur.rowcount == 0:
                    # Roll back the queue append: the input was delivered
                    # concurrently despite the row lock.
                    raise RuntimeError(
                        f"trigger input {input_id} was delivered concurrently"
                    )
                await cur.execute(
                    "UPDATE autonomy_tickets SET revision = revision + 1 "
                    "WHERE ticket_id = %s",
                    (expected_ticket_id,),
                )
                return True


UNDELIVERED_INSTRUCTION_KINDS = ("answer", "instruction")

_LIST_UNDELIVERED_INSTRUCTIONS = """
SELECT input_id, ticket_id, trigger_id, kind, text,
       delivered_at, read_at, created_at
FROM autonomy_ticket_inputs
WHERE ticket_id = %s
  AND delivered_at IS NULL
  AND kind = ANY(%s)
  AND btrim(COALESCE(text, '')) <> ''
ORDER BY created_at
LIMIT %s OFFSET %s
"""


async def list_undelivered_instructions(
    ticket_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    """Instructions no worker session has taken, oldest first.

    Delivered rows and source triggers are not part of this page. A lookup
    that pages ``list_inputs`` instead fills the first page with those rows
    and never reaches an instruction stored after them.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                _LIST_UNDELIVERED_INSTRUCTIONS,
                (ticket_id, list(UNDELIVERED_INSTRUCTION_KINDS), limit, offset),
            )
            return [
                {
                    **dict(row),
                    "input_id": str(row["input_id"]),
                    "ticket_id": str(row["ticket_id"]),
                }
                for row in await cur.fetchall()
            ]


async def list_inputs(
    ticket_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT input_id, ticket_id, trigger_id, kind, text,
                       delivered_at, read_at, created_at
                FROM autonomy_ticket_inputs
                WHERE ticket_id = %s
                ORDER BY created_at
                LIMIT %s OFFSET %s
                """,
                (ticket_id, limit, offset),
            )
            return [
                {**dict(row), "input_id": str(row["input_id"]), "ticket_id": str(row["ticket_id"])}
                for row in await cur.fetchall()
            ]


async def add_report(
    *, ticket_id: str, state: str, summary: str, question: str | None = None,
    body: str | None = None,
) -> dict[str, Any]:
    clean_body = await asyncio.to_thread(_report_body, body)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO autonomy_ticket_reports (ticket_id, state, summary, question, body)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING report_id, ticket_id, state, summary, question, body, created_at
                """,
                (ticket_id, state, summary, question, clean_body),
            )
            row = dict(await cur.fetchone() or {})
            await cur.execute(
                "UPDATE autonomy_tickets SET revision = revision + 1 WHERE ticket_id = %s",
                (ticket_id,),
            )
            row["report_id"] = str(row.get("report_id") or "")
            row["ticket_id"] = str(row.get("ticket_id") or "")
            return row


async def record_worker_report(
    *,
    ticket_id: str,
    expected_thread_id: str,
    state: str,
    summary: str,
    question: str | None = None,
    body: str | None = None,
    git_check: str | None = None,
    ticket_fields: dict[str, Any] | None = None,
    expected_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Fence a Worker line and commit its report and ticket projection together.

    A report whose new status is in ``_ENDS_SUBSCRIPTIONS`` also ends the
    Ticket's live subscriptions in the same transaction (the migration-127
    invariant ``update_ticket`` upholds): intake routes by active subscription
    rows, so a settled Ticket must not leave any behind.
    """
    if not expected_thread_id:
        raise ValueError("expected_thread_id is required")
    if git_check not in {None, "unsupported_runtime"}:
        raise ValueError("worker report git_check is invalid")
    fields = dict(ticket_fields or {})
    allowed = {"status", "summary", "attention", "ended_at"}
    if not fields or fields.keys() - allowed:
        raise ValueError("worker report ticket fields are invalid")
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        sets.append(f"{key} = %s")
        params.append(Json(value) if key == "attention" and value is not None else value)
    sets.extend(("updated_at = NOW()", "revision = revision + 1"))
    where = "ticket_id = %s AND status = 'running' AND worker_thread_id = %s"
    params.extend((ticket_id, expected_thread_id))
    if expected_run_id is not None:
        # A resumed line deliberately reuses its thread with a new run id, so
        # the thread alone cannot fence an older run's report.
        where += " AND worker_run_id = %s"
        params.append(expected_run_id)

    clean_body = await asyncio.to_thread(_report_body, body)

    # The guarded UPDATE takes the row lock before the report insert. A switch
    # that wins the lock first makes this predicate fail; a report that wins
    # commits both rows before the switch can move the line.
    async with get_db_connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                UPDATE autonomy_tickets SET {", ".join(sets)}
                WHERE {where}
                RETURNING {_TICKET_COLUMNS}
                """,
                params,
            )
            if await cur.fetchone() is None:
                return None
            if fields.get("status") in _ENDS_SUBSCRIPTIONS:
                # A terminal or failed Ticket stops being a subscriber: intake
                # routes by active subscription rows, so the row must end with
                # the Ticket (same invariant as update_ticket, same transaction
                # as the report). ``failed`` stays out of ``_TERMINAL``.
                await cur.execute(
                    """
                    UPDATE autonomy_subscriptions
                    SET ended_at = NOW(), end_reason = %s
                    WHERE ticket_id = %s AND ended_at IS NULL
                    """,
                    (END_TICKET_ENDED, ticket_id),
                )
            await cur.execute(
                """
                INSERT INTO autonomy_ticket_reports (ticket_id, state, summary, question, body, git_check)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING report_id, ticket_id, state, summary, question, body, git_check, created_at
                """,
                (
                    ticket_id, state, summary, question, clean_body, git_check,
                ),
            )
            row = dict(await cur.fetchone() or {})
            # Revision already advanced in the guarded UPDATE above.
            row["report_id"] = str(row.get("report_id") or "")
            row["ticket_id"] = str(row.get("ticket_id") or "")
            return row


async def list_reports(
    ticket_id: str, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT report_id, ticket_id, state, summary, question, body, git_check, created_at
                FROM autonomy_ticket_reports
                WHERE ticket_id = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (ticket_id, limit, offset),
            )
            return [
                {
                    **dict(row),
                    "report_id": str(row["report_id"]),
                    "ticket_id": str(row["ticket_id"]),
                }
                for row in await cur.fetchall()
            ]


async def latest_report(ticket_id: str, *, conn=None) -> dict[str, Any] | None:
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT report_id, ticket_id, state, summary, question, body, git_check, created_at
                FROM autonomy_ticket_reports
                WHERE ticket_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (ticket_id,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            out = dict(row)
            out["report_id"] = str(out["report_id"])
            out["ticket_id"] = str(out["ticket_id"])
            return out


async def list_subscriptions(
    ticket_id: str, *, active_only: bool = False
) -> list[dict[str, Any]]:
    """Thread subscriptions of one Ticket, own and context alike.

    Ended rows (unsubscribe, reassignment, ticket end) are included unless
    ``active_only`` — audit wants the history, launch context wants live rows.
    """
    where = "WHERE ticket_id = %s"
    if active_only:
        where += " AND ended_at IS NULL"
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT ticket_id, thread_key, kind, ended_at, end_reason
                FROM autonomy_subscriptions
                {where}
                ORDER BY thread_key
                """,
                (ticket_id,),
            )
            return [
                {**dict(row), "ticket_id": str(row["ticket_id"])}
                for row in await cur.fetchall()
            ]


async def list_reports_for_thread(
    thread_key: str, *, limit: int = 20, offset: int = 0
) -> list[dict[str, Any]]:
    """Reports of every Ticket subscribed to the Thread, newest first."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT r.report_id, r.ticket_id, r.state, r.summary, r.question, r.body, r.git_check,
                       r.created_at
                FROM autonomy_ticket_reports r
                JOIN autonomy_subscriptions s ON s.ticket_id = r.ticket_id
                WHERE s.thread_key = %s
                ORDER BY r.created_at DESC
                LIMIT %s OFFSET %s
                """,
                (thread_key, limit, offset),
            )
            return [
                {
                    **dict(row),
                    "report_id": str(row["report_id"]),
                    "ticket_id": str(row["ticket_id"]),
                }
                for row in await cur.fetchall()
            ]


async def list_tickets_for_thread(thread_key: str) -> list[dict[str, Any]]:
    """Live subscribers of one Thread, ordered by Ticket creation.

    Ended subscriptions are not subscribers: after an unsubscribe or a
    reassignment the previous owner must not receive this Thread's follow-ups.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT t.ticket_id, t.user_id, t.workspace_id, t.title, t.scope,
                       t.status, t.mandate, t.policy_version, t.origin_trigger_id,
                       t.origin_thread_key, t.router_thread_id, t.worker_thread_id,
                       t.worker_run_id, t.worker_provider, t.worker_model,
                       t.attempts, t.attention, t.summary, t.error,
                       t.created_at, t.updated_at, t.started_at, t.ended_at,
                       s.kind AS subscription_kind
                FROM autonomy_tickets t
                JOIN autonomy_subscriptions s ON s.ticket_id = t.ticket_id
                WHERE s.thread_key = %s AND s.ended_at IS NULL
                ORDER BY t.created_at
                """,
                (thread_key,),
            )
            return [_ticket(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def list_watchdog_candidates(
    *,
    queued_seconds: int,
    waiting_seconds: int,
    running_seconds: int,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Tickets with no attention that have not changed within their threshold.

    ``stall_seconds`` is returned so the caller can word the attention message
    without a second clock read.
    """
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_TICKET_COLUMNS},
                       EXTRACT(
                           EPOCH FROM (NOW() - CASE
                               WHEN status = 'running'
                               THEN COALESCE(started_at, updated_at)
                               ELSE updated_at
                           END)
                       )::int AS stall_seconds
                FROM autonomy_tickets
                WHERE attention IS NULL
                  AND mandate->>'orchestration_only' IS DISTINCT FROM 'true'
                  AND (
                    mandate->>'watchdog_reviewed_at' IS NULL
                    OR (mandate->>'watchdog_reviewed_at')::timestamptz
                       < NOW() - INTERVAL '6 hours'
                  )
                  AND (
                    (status = 'queued'
                     AND updated_at < NOW() - (%s * INTERVAL '1 second'))
                    OR (status = 'waiting'
                     AND updated_at < NOW() - (%s * INTERVAL '1 second'))
                    OR (status = 'running'
                     AND COALESCE(started_at, updated_at)
                         < NOW() - (%s * INTERVAL '1 second'))
                  )
                ORDER BY updated_at ASC
                LIMIT %s
                """,
                (queued_seconds, waiting_seconds, running_seconds, limit),
            )
            return [_ticket(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def list_worker_idle_candidates(
    *,
    idle_seconds: int,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Running Tickets whose current attempt has been silent for ``idle_seconds``.

    The clock is the run ledger row of ``worker_run_id`` (that attempt's
    creation time). The Ticket's ``started_at`` is kept across a resume, so
    measuring from it would treat a fresh attempt as already idle. A Ticket
    with no run row falls back to ``updated_at``, which a resume moves.

    Rows are ordered by ``ticket_id`` and paged with ``offset``. The caller
    walks the following pages in the same sweep. Stopping after the first
    page would keep returning the same active Workers and never reach an
    idle Worker further down the order. ``stall_seconds`` is that attempt's
    age, so the caller needs no second clock read.
    """
    columns = ", ".join(
        f"autonomy_tickets.{name.strip()}"
        for name in _TICKET_COLUMNS.split(",")
        if name.strip()
    )
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {columns},
                       EXTRACT(
                           EPOCH FROM (
                               NOW() - COALESCE(
                                   attempt.created_at,
                                   autonomy_tickets.updated_at
                               )
                           )
                       )::int AS stall_seconds
                FROM autonomy_tickets
                LEFT JOIN conversation_responses AS attempt
                  ON attempt.conversation_response_id
                     = autonomy_tickets.worker_run_id
                WHERE autonomy_tickets.status = 'running'
                  AND autonomy_tickets.attention IS NULL
                  AND autonomy_tickets.mandate->>'orchestration_only'
                      IS DISTINCT FROM 'true'
                  AND COALESCE(
                        attempt.created_at, autonomy_tickets.updated_at
                      )
                      < NOW() - (%s * INTERVAL '1 second')
                ORDER BY autonomy_tickets.ticket_id ASC
                LIMIT %s
                OFFSET %s
                """,
                (idle_seconds, limit, offset),
            )
            return [_ticket(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def list_open_for_user(user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Open Tickets (anything not in a terminal status) for a Policy notice."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_TICKET_COLUMNS}
                FROM autonomy_tickets
                WHERE user_id = %s
                  AND status NOT IN ('completed', 'cancelled', 'archived')
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            )
            return [_ticket(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def other_open_subscriber(thread_key: str, *, ticket_id: str) -> str | None:
    """Another live Ticket subscribed to the Thread, or None."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT t.ticket_id
                FROM autonomy_subscriptions s
                JOIN autonomy_tickets t ON t.ticket_id = s.ticket_id
                WHERE s.thread_key = %s
                  AND s.kind = 'own'
                  AND s.ended_at IS NULL
                  AND t.ticket_id <> %s
                  AND t.status NOT IN ('completed', 'cancelled', 'archived')
                ORDER BY t.updated_at DESC
                LIMIT 1
                """,
                (thread_key, ticket_id),
            )
            row = await cur.fetchone()
            return str(row["ticket_id"]) if row else None


async def end_subscription(
    ticket_id: str, thread_key: str, *, reason: str, conn=None
) -> dict[str, Any] | None:
    """End one active subscription; None when it was already ended.

    The guarded UPDATE makes the operation idempotent so a retried
    unsubscribe or reassignment cannot double-end a row.
    """
    if reason not in {END_UNSUBSCRIBED, END_REASSIGNED, END_TICKET_ENDED}:
        raise ValueError("unsupported subscription end reason")
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE autonomy_subscriptions
                SET ended_at = NOW(), end_reason = %s
                WHERE ticket_id = %s AND thread_key = %s AND ended_at IS NULL
                RETURNING ticket_id, thread_key, kind, ended_at, end_reason
                """,
                (reason, ticket_id, thread_key),
            )
            row = await cur.fetchone()
            if row is not None:
                await cur.execute(
                    "UPDATE autonomy_tickets SET revision = revision + 1 WHERE ticket_id = %s",
                    (ticket_id,),
                )
            return dict(row) if row else None


async def add_subscription(
    ticket_id: str, thread_key: str, *, kind: str = "context", conn=None
) -> None:
    if kind not in {"own", "context"}:
        raise ValueError("subscription kind must be own or context")
    async with get_db_connection(conn) as db:
        async with db.cursor() as cur:
            # ON CONFLICT reactivates an ended row. A LIVE 'own' stays 'own'
            # (ownership is sticky while held), but an ENDED row takes the
            # requested kind: a context-only follow after an unsubscribe or a
            # handoff must not resurrect ownership the ticket gave up — after
            # A->B, A following the Thread again is a context subscriber.
            await cur.execute(
                """
                INSERT INTO autonomy_subscriptions (ticket_id, thread_key, kind)
                VALUES (%s, %s, %s)
                ON CONFLICT (ticket_id, thread_key)
                DO UPDATE SET kind = CASE
                    WHEN autonomy_subscriptions.ended_at IS NULL
                         AND autonomy_subscriptions.kind = 'own' THEN 'own'
                    ELSE EXCLUDED.kind
                END,
                ended_at = NULL,
                end_reason = NULL
                """,
                (ticket_id, thread_key, kind),
            )


async def move_undelivered_inputs(
    *, from_ticket_id: str, to_ticket_id: str, thread_key: str, conn=None
) -> list[dict[str, Any]]:
    """Move a Thread's undelivered inputs to the Ticket inheriting it.

    Handed-back and already-delivered inputs stay with the previous owner —
    the handoff is about work still owed, not history. An input whose Trigger
    the target already holds (typically a context copy on a subscriber) is
    adopted instead of skipped: the actionable payload takes the target's row
    over and the previous owner's duplicate is deleted, so reassigning to a
    context subscriber cannot leave pending work stranded on the Ticket that
    just lost the Thread.
    """
    if from_ticket_id == to_ticket_id:
        raise ValueError("cannot move inputs to the same ticket")
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE autonomy_ticket_inputs tgt
                   SET kind = src.kind,
                       text = src.text,
                       delivered_at = NULL,
                       handed_back = FALSE
                  FROM autonomy_ticket_inputs src
                  JOIN autonomy_triggers tr ON tr.trigger_id = src.trigger_id
                 WHERE src.ticket_id = %s
                   AND tr.thread_key = %s
                   AND src.delivered_at IS NULL
                   AND src.handed_back = FALSE
                   AND src.trigger_id IS NOT NULL
                   AND tgt.ticket_id = %s
                   AND tgt.trigger_id = src.trigger_id
                 RETURNING tgt.input_id, src.trigger_id, src.kind, src.text
                """,
                (from_ticket_id, thread_key, to_ticket_id),
            )
            adopted = [dict(row) for row in await cur.fetchall()]
            if adopted:
                await cur.execute(
                    """
                    DELETE FROM autonomy_ticket_inputs
                    WHERE ticket_id = %s
                      AND delivered_at IS NULL
                      AND handed_back = FALSE
                      AND trigger_id = ANY(%s)
                    """,
                    (from_ticket_id, [row["trigger_id"] for row in adopted]),
                )
            await cur.execute(
                """
                UPDATE autonomy_ticket_inputs i
                   SET ticket_id = %s
                  FROM autonomy_triggers tr
                 WHERE i.trigger_id = tr.trigger_id
                   AND tr.thread_key = %s
                   AND i.ticket_id = %s
                   AND i.delivered_at IS NULL
                   AND i.handed_back = FALSE
                   AND NOT EXISTS (
                       SELECT 1 FROM autonomy_ticket_inputs x
                        WHERE x.ticket_id = %s AND x.trigger_id = i.trigger_id
                   )
                RETURNING i.input_id, i.trigger_id, i.kind, i.text
                """,
                (to_ticket_id, thread_key, from_ticket_id, to_ticket_id),
            )
            moved = [dict(row) for row in await cur.fetchall()]
            if adopted or moved:
                await cur.execute(
                    """
                    UPDATE autonomy_tickets SET revision = revision + 1
                    WHERE ticket_id = ANY(%s)
                    """,
                    ([from_ticket_id, to_ticket_id],),
                )
            out = adopted + moved
            for row in out:
                row["input_id"] = str(row["input_id"])
            return out


async def record_handoff(
    *,
    thread_key: str,
    user_id: str,
    from_ticket_id: str,
    to_ticket_id: str,
    reason: str,
    state: dict[str, Any],
    conn=None,
) -> dict[str, Any]:
    """Persist one reassignment with the state the new owner inherits."""
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO autonomy_handoffs (
                    thread_key, user_id, from_ticket_id, to_ticket_id,
                    reason, state
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING handoff_id, created_at
                """,
                (
                    thread_key,
                    user_id,
                    from_ticket_id,
                    to_ticket_id,
                    reason[:500],
                    SafeJson(_json_safe(state)),
                ),
            )
            row = dict(await cur.fetchone() or {})
            row["handoff_id"] = str(row.get("handoff_id") or "")
            return row


async def list_handoffs_for_ticket(
    ticket_id: str, *, limit: int = 10
) -> list[dict[str, Any]]:
    """Handoffs into one Ticket, newest first — its inheritance record."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT handoff_id, thread_key, user_id, from_ticket_id,
                       to_ticket_id, reason, state, created_at
                FROM autonomy_handoffs
                WHERE to_ticket_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (ticket_id, limit),
            )
            rows = []
            for row in await cur.fetchall():
                item = dict(row)
                item["handoff_id"] = str(item.get("handoff_id") or "")
                item["state"] = dict(item.get("state") or {})
                rows.append(item)
            return rows


async def context_subscribers(
    thread_key: str, *, exclude_ticket_id: str = ""
) -> list[dict[str, Any]]:
    """Live Tickets following this Thread only for context."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT t.*
                FROM autonomy_tickets t
                JOIN autonomy_subscriptions s ON s.ticket_id = t.ticket_id
                WHERE s.thread_key = %s
                  AND s.kind = 'context'
                  AND s.ended_at IS NULL
                  AND t.status NOT IN ('completed', 'cancelled', 'archived')
                  AND t.ticket_id <> %s
                  AND t.context_scope_id IS NOT NULL
                  AND t.context_scope_id = (
                      SELECT origin.context_scope_id
                      FROM autonomy_tickets origin
                      WHERE origin.ticket_id = %s
                  )
                  AND EXISTS (
                      SELECT 1 FROM autonomy_context_scopes cs
                      WHERE cs.scope_id = t.context_scope_id
                        AND cs.kind = 'shared'
                  )
                ORDER BY t.created_at
                """,
                (thread_key, exclude_ticket_id, exclude_ticket_id),
            )
            return [_ticket(row) for row in await cur.fetchall() if row]  # type: ignore[misc]
