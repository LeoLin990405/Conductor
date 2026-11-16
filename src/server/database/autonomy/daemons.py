"""BYOM local daemons: token identity and queued-Ticket claims."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID, uuid4

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.autonomy.tickets import _report_body
from src.server.database.pool import get_db_connection

# Mandate key holding the recorded outcome of a speculative run; kept in sync
# with src/server/services/autonomy/host.py.
SPECULATIVE_OUTCOME_KEY = "speculative_outcome"

_TICKET_COLUMNS = """
    ticket_id, user_id, workspace_id, title, scope, status, mandate,
    policy_version, origin_trigger_id, origin_thread_key,
    router_thread_id, worker_thread_id, worker_run_id,
    worker_provider, worker_model, worker_daemon_id,
    worker_lease_id, worker_lease_claim_id, worker_lease_expires_at,
    attempts, attention, summary, error, revision, worktree_path,
    worktree_generation, worktree_cleanup_pending, worktree_cleanup_attempts,
    worktree_cleanup_next_retry_at, worktree_branch, worktree_base_sha,
    worktree_base_branch, worktree_repo, worktree_host, worktree_daemon_id,
    worktree_lease_id, worktree_stale_cleanups,
    created_at, updated_at, started_at, ended_at,
    closed_at, close_reason
"""
LEASE_SECONDS = 90
# Claim payloads always carry these lease fields alongside the Ticket row so a
# daemon can heartbeat and report without a second round-trip.
_CLAIM_LEASE_FIELDS = ("lease_id", "lease_expires_at", "lease_duration_seconds")


async def create_daemon(
    *,
    user_id: str,
    name: str,
    token_hash: str,
    provider: str,
    max_concurrency: int = 1,
) -> dict[str, Any]:
    concurrency = max(1, min(32, int(max_concurrency)))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO autonomy_daemons
                    (user_id, name, token_hash, provider, max_concurrency)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING daemon_id, user_id, name, provider, max_concurrency,
                          last_seen_at, created_at
                """,
                (user_id, name[:120], token_hash, provider[:64], concurrency),
            )
            row = await cur.fetchone()
    return {**dict(row or {}), "daemon_id": str((row or {}).get("daemon_id") or "")}


async def list_daemons(user_id: str) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT daemon_id, user_id, name, provider, max_concurrency, last_seen_at, created_at
                FROM autonomy_daemons
                WHERE user_id = %s
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            return [
                {**dict(row), "daemon_id": str(row["daemon_id"])}
                for row in await cur.fetchall()
            ]


async def get_daemon(user_id: str, daemon_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT daemon_id, user_id, name, provider, max_concurrency, last_seen_at, created_at
                FROM autonomy_daemons WHERE user_id = %s AND daemon_id = %s
                """,
                (user_id, daemon_id),
            )
            row = await cur.fetchone()
    return {**dict(row), "daemon_id": str(row["daemon_id"])} if row else None


async def delete_daemon(user_id: str, daemon_id: str) -> str:
    """Refuse deletion while configuration or nonterminal Tickets depend on it."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM autonomy_daemons WHERE user_id = %s "
                    "AND daemon_id = %s FOR UPDATE",
                    (user_id, daemon_id),
                )
                if await cur.fetchone() is None:
                    return "not_found"
                await cur.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM autonomy_tickets
                        WHERE worker_daemon_id = %s
                          AND status NOT IN ('completed', 'cancelled', 'archived')
                    ) OR EXISTS (
                        SELECT 1 FROM autonomy_schedules WHERE worker_daemon_id = %s
                    ) OR EXISTS (
                        SELECT 1 FROM autonomy_sources
                        WHERE config->>'worker_daemon_id' = %s
                    ) OR EXISTS (
                        SELECT 1 FROM autonomy_stop_requests
                        WHERE daemon_id = %s AND verification = 'pending'
                    )
                    """,
                    (daemon_id, daemon_id, daemon_id, daemon_id),
                )
                in_use = bool((await cur.fetchone() or (True,))[0])
                if in_use:
                    return "in_use"
                await cur.execute(
                    "DELETE FROM autonomy_daemons WHERE user_id = %s AND daemon_id = %s",
                    (user_id, daemon_id),
                )
                return "deleted" if cur.rowcount else "not_found"


async def daemon_for_token(token_hash: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE autonomy_daemons
                SET last_seen_at = NOW()
                WHERE token_hash = %s
                RETURNING daemon_id, user_id, name, provider, max_concurrency,
                          last_seen_at, created_at
                """,
                (token_hash,),
            )
            row = await cur.fetchone()
    return {**dict(row), "daemon_id": str(row["daemon_id"])} if row else None


async def heartbeat(
    daemon_id: str, leases: list[dict[str, str]] | None = None
) -> list[dict[str, Any]]:
    results = []
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE autonomy_daemons SET last_seen_at = NOW() WHERE daemon_id = %s",
                    (daemon_id,),
                )
                # A stable ticket lock order avoids deadlocks when daemons submit
                # overlapping lease batches in opposite orders.
                for lease in sorted(leases or [], key=lambda item: item["ticket_id"]):
                    await cur.execute(
                        """UPDATE autonomy_tickets SET worker_lease_expires_at =
                         clock_timestamp() + make_interval(secs => %s),
                         worker_lease_heartbeat_at = clock_timestamp()
                       WHERE ticket_id = %s AND worker_daemon_id = %s AND status = 'running'
                         AND worker_lease_id = %s AND worker_lease_expires_at > clock_timestamp()
                       RETURNING ticket_id""",
                        (
                            LEASE_SECONDS,
                            lease["ticket_id"],
                            daemon_id,
                            lease["lease_id"],
                        ),
                    )
                    results.append(
                        {
                            **lease,
                            "renewed": await cur.fetchone() is not None,
                            "lease_duration_seconds": LEASE_SECONDS,
                        }
                    )
    return results


async def update_daemon_concurrency(
    user_id: str, daemon_id: str, max_concurrency: int
) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE autonomy_daemons SET max_concurrency = %s "
                "WHERE user_id = %s AND daemon_id = %s "
                "RETURNING daemon_id, user_id, name, provider, max_concurrency, last_seen_at, created_at",
                (max_concurrency, user_id, daemon_id),
            )
            row = await cur.fetchone()
    return {**dict(row), "daemon_id": str(row["daemon_id"])} if row else None


async def claim_ticket(
    daemon_id: str, claim_id: str | None = None, policy_version: str = ""
) -> dict[str, Any] | None:
    """Claim one queued ticket; each attempt has an independent fencing lease.

    The lease records the policy version the daemon reported, so the device
    can refuse a lease it granted under a policy it has since replaced.
    """
    claim_id = str(UUID(claim_id)) if claim_id else str(uuid4())
    policy_version = str(policy_version or "")[:64]
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                # Serialize claimers on durable daemon state. The subsequent
                # count uses a new READ COMMITTED statement snapshot.
                await cur.execute(
                    "SELECT daemon_id, user_id, name, provider, max_concurrency FROM autonomy_daemons "
                    "WHERE daemon_id = %s FOR UPDATE",
                    (daemon_id,),
                )
                daemon = await cur.fetchone()
                if daemon is None:
                    return None
                provider = str(daemon["provider"] or "")
                await cur.execute(
                    "SELECT COUNT(*) AS running_count FROM autonomy_tickets "
                    "WHERE worker_daemon_id = %s AND user_id = %s "
                    "AND status = 'running' AND worker_lease_expires_at > clock_timestamp()",
                    (daemon_id, daemon["user_id"]),
                )
                running_row = await cur.fetchone()
                running = int((running_row or {}).get("running_count") or 0)
                await cur.execute(
                    f"SELECT {_TICKET_COLUMNS}, "
                    "(SELECT input_snapshot FROM autonomy_daemon_claims c "
                    "WHERE c.lease_id = autonomy_tickets.worker_lease_id) AS input_snapshot, "
                    "(SELECT policy_version FROM autonomy_daemon_claims c "
                    "WHERE c.lease_id = autonomy_tickets.worker_lease_id) AS policy_version, "
                    "GREATEST(0, FLOOR(EXTRACT(EPOCH FROM "
                    "(worker_lease_expires_at-clock_timestamp()))))::int AS remaining "
                    "FROM autonomy_tickets WHERE worker_daemon_id = %s "
                    "AND user_id = %s AND worker_lease_claim_id = %s "
                    "AND status = 'running' AND worker_lease_expires_at > clock_timestamp() LIMIT 1",
                    (daemon_id, daemon["user_id"], claim_id),
                )
                replay = await cur.fetchone()
                if replay is not None:
                    return _claim_result(
                        replay,
                        lease_id=str(replay["worker_lease_id"]),
                        lease_expires_at=replay["worker_lease_expires_at"],
                        lease_duration_seconds=int(replay["remaining"] or 0),
                        policy_version=str(replay.get("policy_version") or ""),
                    )
                if running >= int(daemon.get("max_concurrency") or 1):
                    return None
                await cur.execute(
                    "SELECT 1 FROM autonomy_daemon_claims WHERE claim_id=%s",
                    (claim_id,),
                )
                if await cur.fetchone() is not None:
                    return None
                await cur.execute(
                    """
                    WITH next AS (
                        SELECT ticket_id
                        FROM autonomy_tickets
                        WHERE status = 'queued' AND worker_daemon_id = %s
                          AND user_id = %s
                          AND (worker_provider = '' OR worker_provider = %s)
                          AND mandate->>'orchestration_only' IS DISTINCT FROM 'true'
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE autonomy_tickets t
                    SET status = 'running',
                        worker_provider = COALESCE(NULLIF(t.worker_provider, ''), %s),
                        attempts = t.attempts + 1,
                        worker_lease_id = %s::uuid,
                        worker_lease_claim_id = %s::uuid,
                        worker_lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                        worker_lease_heartbeat_at = clock_timestamp(),
                        started_at = COALESCE(t.started_at, NOW()),
                        revision = t.revision + 1,
                        updated_at = NOW()
                    FROM next
                    WHERE t.ticket_id = next.ticket_id
                    RETURNING t.*
                    """,
                    (
                        daemon_id,
                        daemon["user_id"],
                        provider,
                        provider,
                        claim_id,
                        claim_id,
                        LEASE_SECONDS,
                    ),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                await cur.execute(
                    "INSERT INTO autonomy_daemon_claims (claim_id, daemon_id, ticket_id, lease_id, policy_version) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (claim_id, daemon_id, row["ticket_id"], claim_id, policy_version),
                )
                await cur.execute(
                    """
                    INSERT INTO autonomy_ticket_events (
                        ticket_id, user_id, actor, action, from_owner, to_owner, detail
                    )
                    VALUES (%s, %s, %s, 'claimed', 'cloud', %s, %s)
                    """,
                    (
                        row["ticket_id"],
                        daemon["user_id"],
                        f"daemon:{daemon.get('name') or daemon_id}",
                        daemon_id,
                        Json({"claim_id": claim_id}),
                    ),
                )
                return _claim_result(
                    row,
                    lease_id=claim_id,
                    lease_expires_at=row["worker_lease_expires_at"],
                    lease_duration_seconds=LEASE_SECONDS,
                    policy_version=policy_version,
                )


def _claim_result(
    row: dict[str, Any],
    *,
    lease_id: str,
    lease_expires_at: Any,
    lease_duration_seconds: int,
    policy_version: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Ticket row plus the lease grant fields every claim/replay must keep.

    ``worktree_path`` is the persisted host checkout ('' if none), so a dirty
    blocked Ticket resumes its own checkout on the daemon that holds it.
    """
    out = dict(row)
    out["worktree_path"] = str(out.get("worktree_path") or "")
    out["worktree_generation"] = int(out.get("worktree_generation") or 0)
    for key in ("worktree_daemon_id", "worktree_lease_id"):
        out[key] = str(out[key]) if out.get(key) else None
    out.update(
        lease_id=lease_id,
        lease_expires_at=lease_expires_at,
        lease_duration_seconds=int(lease_duration_seconds),
        policy_version=policy_version,
        **extra,
    )
    # Pin the lease contract so a future reshape cannot drop these keys.
    for key in _CLAIM_LEASE_FIELDS:
        if key not in out:
            raise RuntimeError(f"daemon claim result missing {key}")
    return out


async def active_claim(daemon_id: str, claim_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT t.*, worker_lease_id, c.input_snapshot, c.policy_version,
                worker_lease_expires_at,
                GREATEST(0, FLOOR(EXTRACT(EPOCH FROM
                    (worker_lease_expires_at-clock_timestamp()))))::int AS remaining
                FROM autonomy_daemon_claims c JOIN autonomy_tickets t ON t.ticket_id=c.ticket_id
                WHERE c.daemon_id=%s AND c.claim_id=%s AND t.worker_lease_id=c.lease_id
                AND t.status='running' AND t.worker_lease_expires_at>clock_timestamp() LIMIT 1""",
                (daemon_id, claim_id),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return _claim_result(
        row,
        lease_id=str(row["worker_lease_id"]),
        lease_expires_at=row["worker_lease_expires_at"],
        lease_duration_seconds=int(row["remaining"] or 0),
        policy_version=str(row.get("policy_version") or ""),
        inputs=list(row.get("input_snapshot") or []),
    )


async def recover_expired_daemon_leases() -> int:
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""UPDATE autonomy_tickets SET status='queued', worker_lease_id=NULL,
                worker_lease_claim_id=NULL, worker_lease_expires_at=NULL, worker_lease_heartbeat_at=NULL,
                worker_run_id=NULL, worker_thread_id=NULL, attention=NULL, error='',
                revision=revision+1, updated_at=NOW()
                WHERE worker_daemon_id IS NOT NULL AND status='running'
                  AND (worker_lease_expires_at IS NULL OR worker_lease_expires_at <= clock_timestamp())""")
            return cur.rowcount


async def release_daemon_claim(
    daemon_id: str,
    ticket_id: str,
    lease_id: str,
    *,
    heartbeat_at: Any = None,
) -> bool:
    """Give back a claim the daemon never received, requeueing the Ticket now.

    Used when the server cannot build the claim's launch payload for a reason
    that is not the Ticket's own (a server-wide outage). The same columns as
    ``recover_expired_daemon_leases`` are reset, fenced on this daemon's live
    lease, and the claim is marked revoked so nothing can report on it. The
    claim's attempt is handed back so an outage does not inflate the count.

    A claim that shows it reached the daemon is never given back: one with a
    report receipt, or (when ``heartbeat_at`` is the lease heartbeat the
    caller read with the claim) one the daemon has heartbeated since. Only
    the daemon heartbeats a lease, and only once it holds the claim.
    """
    heartbeat_fence = ""
    params: tuple[Any, ...] = (ticket_id, daemon_id, lease_id)
    if heartbeat_at is not None:
        heartbeat_fence = (
            " AND worker_lease_heartbeat_at IS NOT DISTINCT FROM %s::timestamptz"
        )
        params = (*params, heartbeat_at)
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """UPDATE autonomy_tickets SET status='queued',
                        attempts=GREATEST(attempts - 1, 0), worker_lease_id=NULL,
                        worker_lease_claim_id=NULL, worker_lease_expires_at=NULL,
                        worker_lease_heartbeat_at=NULL, worker_run_id=NULL,
                        worker_thread_id=NULL, attention=NULL, error='',
                        revision=revision+1, updated_at=NOW()
                    WHERE ticket_id=%s AND worker_daemon_id=%s AND status='running'
                      AND worker_lease_id=%s
                      AND worker_lease_expires_at>clock_timestamp()
                      AND NOT EXISTS (SELECT 1 FROM autonomy_daemon_claims rc
                          WHERE rc.lease_id=autonomy_tickets.worker_lease_id
                            AND rc.report_result IS NOT NULL)"""
                    + heartbeat_fence,
                    params,
                )
                if cur.rowcount != 1:
                    return False
                await cur.execute(
                    """UPDATE autonomy_daemon_claims SET revoked_at=NOW()
                    WHERE lease_id=%s AND daemon_id=%s AND ticket_id=%s
                      AND report_result IS NULL AND revoked_at IS NULL""",
                    (lease_id, daemon_id, ticket_id),
                )
                return True


async def live_lease_ticket(
    daemon_id: str, ticket_id: str, lease_id: str
) -> bool:
    """Whether this daemon holds the live lease for a running Ticket.

    The same predicate ``record_progress`` fences on, without its write side:
    a revoked/expired lease, a reassigned owner or a finished Ticket all read
    as not live, so a stale device cannot keep streaming.
    """
    async with get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT 1 FROM autonomy_tickets t
                JOIN autonomy_daemon_claims c
                  ON c.ticket_id=t.ticket_id AND c.lease_id=t.worker_lease_id
                WHERE t.ticket_id=%s AND t.worker_daemon_id=%s
                  AND t.user_id=(SELECT user_id FROM autonomy_daemons WHERE daemon_id=%s)
                  AND t.status='running' AND t.worker_lease_id=%s
                  AND t.worker_lease_expires_at>clock_timestamp()
                  AND c.daemon_id=%s AND c.revoked_at IS NULL""",
                (ticket_id, daemon_id, daemon_id, lease_id, daemon_id),
            )
            return await cur.fetchone() is not None


async def record_progress(
    daemon_id: str, ticket_id: str, lease_id: str, summary: str
) -> str | None:
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(
                    """UPDATE autonomy_tickets SET summary=%s, revision=revision+1, updated_at=NOW()
                  WHERE ticket_id=%s AND worker_daemon_id=%s
                    AND user_id=(SELECT user_id FROM autonomy_daemons WHERE daemon_id=%s)
                    AND status='running' AND worker_lease_id=%s AND worker_lease_expires_at>clock_timestamp()
                  RETURNING ticket_id""",
                    (summary[:2000], ticket_id, daemon_id, daemon_id, lease_id),
                )
                if await cur.fetchone() is None:
                    return None
                await cur.execute(
                    "INSERT INTO autonomy_ticket_reports (ticket_id,state,summary) "
                    "VALUES (%s,'progress',%s) RETURNING report_id",
                    (ticket_id, summary),
                )
                row = await cur.fetchone()
                return str(row[0]) if row else None


async def daemon_report_context(
    daemon_id: str, ticket_id: str, lease_id: str
) -> dict[str, Any] | None:
    """Read a live lease's ticket, or its durable accepted report receipt."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT c.report_result, t.* FROM autonomy_daemon_claims c
                JOIN autonomy_tickets t ON t.ticket_id=c.ticket_id
                WHERE c.daemon_id=%s AND c.ticket_id=%s AND c.lease_id=%s
                  AND (c.report_result IS NOT NULL OR (t.status='running'
                    AND t.worker_lease_id=c.lease_id AND t.worker_lease_expires_at>clock_timestamp()))""",
                (daemon_id, ticket_id, lease_id),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    out = dict(row)
    if out.get("report_result") is not None:
        return {"report_result": dict(out["report_result"])}
    out.pop("report_result", None)
    out["mandate"] = dict(out.get("mandate") or {})
    return out


async def commit_daemon_report(
    daemon_id: str,
    ticket_id: str,
    lease_id: str,
    *,
    state: str,
    summary: str,
    question: str | None,
    body: str | None = None,
    transition: dict[str, Any],
    reported_daemon_id: str = "",
    reported_policy_version: str = "",
    mandate_patch: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Fence transition, enqueue relay and save the replay receipt atomically.

    The signed binding and the revocation flag are rechecked under the claim
    row's lock: checking them before the transaction leaves a window in which
    the owner revokes the lease and the report still commits.
    """
    from psycopg.types.json import Json

    from src.server.database.autonomy.effects import insert_effect

    clean_body = await asyncio.to_thread(_report_body, body)
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """SELECT report_result, daemon_id, policy_version,
                    lease_grant, revoked_at FROM autonomy_daemon_claims
                    WHERE daemon_id=%s AND ticket_id=%s AND lease_id=%s FOR UPDATE""",
                    (daemon_id, ticket_id, lease_id),
                )
                receipt = await cur.fetchone()
                if receipt is None:
                    return None
                if receipt["report_result"] is not None:
                    return dict(receipt["report_result"])
                from src.server.services.autonomy.daemon_grants import (
                    report_divergence,
                )

                binding = {
                    "daemon_id": str(receipt.get("daemon_id") or ""),
                    "policy_version": str(receipt.get("policy_version") or ""),
                    "grant": receipt.get("lease_grant"),
                    "revoked_at": receipt.get("revoked_at"),
                }
                if not isinstance(binding["grant"], dict):
                    binding["grant"] = None
                divergence = report_divergence(
                    binding,
                    daemon_id=reported_daemon_id or daemon_id,
                    policy_version=reported_policy_version,
                    state=state,
                )
                if divergence:
                    return {"refused": divergence}
                await cur.execute(
                    """SELECT user_id, mandate FROM autonomy_tickets
                    WHERE ticket_id=%s AND worker_daemon_id=%s AND status='running'
                      AND worker_lease_id=%s AND worker_lease_expires_at>clock_timestamp()
                      AND user_id=(SELECT user_id FROM autonomy_daemons WHERE daemon_id=%s)
                    FOR UPDATE""",
                    (ticket_id, daemon_id, lease_id, daemon_id),
                )
                ticket = await cur.fetchone()
                if ticket is None:
                    return None
                mandate = dict(ticket.get("mandate") or {})
                await cur.execute(
                    """INSERT INTO autonomy_ticket_reports
                    (ticket_id,state,summary,question,body) VALUES (%s,%s,%s,%s,%s)
                    RETURNING report_id""",
                    (ticket_id, state, summary, question, clean_body),
                )
                report = await cur.fetchone() or {}
                if mandate_patch is not None:
                    # Persist a speculative run's recorded outcome and evidence
                    # with the same settlement the PTC Worker path records.
                    # The report id lands in the evidence once the row exists.
                    outcome = mandate_patch.get(SPECULATIVE_OUTCOME_KEY)
                    report_id = str((report or {}).get("report_id") or "").strip()
                    raw_evidence = (
                        outcome.get("evidence") if isinstance(outcome, dict) else None
                    )
                    if isinstance(outcome, dict) and report_id:
                        evidence = dict(raw_evidence) if isinstance(raw_evidence, dict) else {}
                        evidence["report_id"] = report_id
                        mandate_patch = {
                            **mandate_patch,
                            SPECULATIVE_OUTCOME_KEY: {**outcome, "evidence": evidence},
                        }
                    mandate.update(mandate_patch)
                await cur.execute(
                    """UPDATE autonomy_tickets SET status=%s, summary=%s, attention=%s,
                    mandate=%s,
                    ended_at=CASE WHEN %s THEN NOW() ELSE ended_at END,
                    worker_lease_expires_at=NULL, worker_lease_heartbeat_at=NULL,
                    revision=revision+1, updated_at=NOW()
                    WHERE ticket_id=%s""",
                    (
                        transition["status"],
                        transition.get("summary", summary),
                        Json(transition["attention"])
                        if transition["attention"]
                        else None,
                        Json(mandate),
                        bool(transition["ended"]),
                        ticket_id,
                    ),
                )
                if transition["status"] in (
                    "completed",
                    "cancelled",
                    "archived",
                ):
                    # Same rule as ticket_db.update_ticket: a terminal Ticket
                    # stops being a subscriber in the same transaction.
                    from src.server.database.autonomy.tickets import END_TICKET_ENDED

                    await cur.execute(
                        """UPDATE autonomy_subscriptions
                        SET ended_at = NOW(), end_reason = %s
                        WHERE ticket_id = %s AND ended_at IS NULL""",
                        (END_TICKET_ENDED, ticket_id),
                    )
                manual = False
                if transition.get("source_id"):
                    await cur.execute(
                        "SELECT kind FROM autonomy_sources WHERE source_id=%s",
                        (transition["source_id"],),
                    )
                    source = await cur.fetchone()
                    manual = bool(source and source["kind"] == "manual")
                mandate = dict(ticket.get("mandate") or {})
                main_agent_child = bool(
                    mandate.get("orchestration_child")
                    and mandate.get("kind") == "main_agent_child"
                    and mandate.get("main_agent_run_id")
                    and mandate.get("task_id")
                )
                if main_agent_child:
                    await insert_effect(
                        cur,
                        user_id=str(ticket["user_id"]),
                        kind="main_agent_advance",
                        idempotency_key=f"main-agent:daemon-report:{lease_id}",
                        payload={
                            "run_id": str(mandate["main_agent_run_id"]),
                            "user_id": str(ticket["user_id"]),
                        },
                    )
                main_agent_internal = main_agent_child or mandate.get("kind") in {
                    "main_agent_deliberation",
                    "main_agent_participant",
                }
                if transition["relay"] and not manual and not main_agent_internal:
                    await insert_effect(
                        cur,
                        user_id=str(ticket["user_id"]),
                        kind="relay_report",
                        idempotency_key=f"autonomy:daemon-relay:{lease_id}",
                        payload={"ticket_id": ticket_id, "state": state},
                    )
                result = {
                    key: transition[key]
                    for key in ("ticket_id", "state", "speculative")
                    if key in transition
                }
                if "useful" in transition:
                    result["useful"] = transition["useful"]
                if mandate_patch is not None:
                    result["speculative_outcome"] = (
                        mandate_patch.get(SPECULATIVE_OUTCOME_KEY, {}).get("outcome")
                        if isinstance(
                            mandate_patch.get(SPECULATIVE_OUTCOME_KEY), dict
                        )
                        else None
                    )
                if main_agent_child:
                    result["main_agent_child"] = {
                        "run_id": str(mandate["main_agent_run_id"]),
                        "user_id": str(ticket["user_id"]),
                        "task_id": str(mandate["task_id"]),
                    }
                await cur.execute(
                    "UPDATE autonomy_daemon_claims SET report_result=%s WHERE lease_id=%s",
                    (Json(result), lease_id),
                )
                return result


# A context row on an open accepted speculative plan records a later signal.
# It is not a follow-up. An undelivered row of that kind must not resume the
# observe Worker or clear the plan's review attention. Answers and triggers
# still resume, including on blocked Tickets and on plans that are no longer
# an accepted speculative wait.
#
# The speculative comparisons are NULL-safe. Manual Tickets omit those mandate
# fields. In SQL, `NULL = 'true'` is NULL, and NOT NULL is NULL, so WHERE
# rejects the row. A waiting daemon Ticket with a context update would then
# never resume. COALESCE makes a missing field a definite non-match.
_PASSIVE_CONTEXT_EXCLUSION = """
NOT (
    i.kind = 'context'
    AND t.status = 'waiting'
    AND COALESCE(t.mandate->>'speculative', '') = 'true'
    AND COALESCE(t.mandate->'speculative_outcome'->>'outcome', '') = 'accepted'
)
"""

_RESUMABLE_INPUT_EXISTS = f"""
EXISTS (
    SELECT 1 FROM autonomy_ticket_inputs i
    WHERE i.ticket_id = t.ticket_id
      AND i.delivered_at IS NULL
      AND i.kind IN ('trigger', 'answer')
      AND {_PASSIVE_CONTEXT_EXCLUSION}
)
"""


async def resume_ticket(
    daemon_id: str,
    max_running: int,
    claim_id: str | None = None,
    policy_version: str = "",
) -> dict[str, Any] | None:
    """Resume a waiting Ticket with an idempotent, fenced claim.

    Claim receipt, lease, status flip, and input delivery commit together so a
    lost response can replay the same claim without consuming the input twice.
    The lease records the policy version the daemon reported.
    """
    claim_id = str(UUID(claim_id)) if claim_id else str(uuid4())
    policy_version = str(policy_version or "")[:64]
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT daemon_id, user_id, provider, max_concurrency "
                    "FROM autonomy_daemons WHERE daemon_id = %s FOR UPDATE",
                    (daemon_id,),
                )
                daemon = await cur.fetchone()
                if daemon is None:
                    return None
                user_id = str(daemon["user_id"])
                provider = str(daemon["provider"] or "")
                limit = min(max(1, int(max_running)), int(daemon.get("max_concurrency") or 1))
                await cur.execute(
                f"SELECT {_TICKET_COLUMNS}, "
                "(SELECT input_snapshot FROM autonomy_daemon_claims c "
                "WHERE c.lease_id = autonomy_tickets.worker_lease_id) AS input_snapshot, "
                "(SELECT policy_version FROM autonomy_daemon_claims c "
                "WHERE c.lease_id = autonomy_tickets.worker_lease_id) AS policy_version, "
                    "GREATEST(0, FLOOR(EXTRACT(EPOCH FROM "
                    "(worker_lease_expires_at-clock_timestamp()))))::int AS remaining "
                    "FROM autonomy_tickets WHERE worker_daemon_id = %s "
                    "AND user_id = %s AND worker_lease_claim_id = %s "
                    "AND status = 'running' AND worker_lease_expires_at > clock_timestamp() LIMIT 1",
                    (daemon_id, user_id, claim_id),
                )
                replay = await cur.fetchone()
                if replay is not None:
                    return _claim_result(
                        replay,
                        lease_id=str(replay["worker_lease_id"]),
                        lease_expires_at=replay["worker_lease_expires_at"],
                        lease_duration_seconds=int(replay["remaining"] or 0),
                        policy_version=str(replay.get("policy_version") or ""),
                        inputs=list(replay.get("input_snapshot") or []),
                    )
                await cur.execute(
                    "SELECT COUNT(*) AS running_count FROM autonomy_tickets "
                    "WHERE worker_daemon_id = %s AND user_id = %s "
                    "AND status = 'running' AND worker_lease_expires_at > clock_timestamp()",
                    (daemon_id, user_id),
                )
                running = int((await cur.fetchone() or {}).get("running_count") or 0)
                if running >= limit:
                    return None
                await cur.execute(
                    "SELECT 1 FROM autonomy_daemon_claims WHERE claim_id = %s",
                    (claim_id,),
                )
                if await cur.fetchone() is not None:
                    return None
                await cur.execute(
                    f"""
                    SELECT t.ticket_id
                    FROM autonomy_tickets t
                    WHERE t.worker_daemon_id = %s
                      AND t.user_id = %s
                      AND (t.worker_provider = '' OR t.worker_provider = %s)
                      AND t.status IN ('blocked', 'waiting')
                      AND t.mandate->>'orchestration_only' IS DISTINCT FROM 'true'
                      AND {_RESUMABLE_INPUT_EXISTS}
                    ORDER BY t.updated_at DESC
                    LIMIT 1
                    FOR UPDATE OF t SKIP LOCKED
                    """,
                    (daemon_id, user_id, provider),
                )
                candidate = await cur.fetchone()
                if candidate is None:
                    return None
                await cur.execute(
                    f"""
                    UPDATE autonomy_tickets t
                    SET status = 'running',
                        worker_provider = COALESCE(NULLIF(t.worker_provider, ''), %s),
                        worker_lease_id = %s::uuid,
                        worker_lease_claim_id = %s::uuid,
                        worker_lease_expires_at = clock_timestamp() + make_interval(secs => %s),
                        worker_lease_heartbeat_at = clock_timestamp(),
                        attention = NULL,
                        attempts = t.attempts + 1,
                        revision = t.revision + 1,
                        updated_at = NOW()
                    WHERE t.ticket_id = %s
                      AND t.worker_daemon_id = %s
                      AND t.status IN ('blocked', 'waiting')
                      AND t.mandate->>'orchestration_only' IS DISTINCT FROM 'true'
                      AND {_RESUMABLE_INPUT_EXISTS}
                    RETURNING {_TICKET_COLUMNS}
                    """,
                    (provider, claim_id, claim_id, LEASE_SECONDS, candidate["ticket_id"], daemon_id),
                )
                row = await cur.fetchone()
                if row is None:
                    return None
                await cur.execute(
                    "INSERT INTO autonomy_daemon_claims "
                    "(claim_id, daemon_id, ticket_id, lease_id, policy_version) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (claim_id, daemon_id, row["ticket_id"], claim_id, policy_version),
                )
                await cur.execute(
                    """
                    WITH delivered AS (
                        UPDATE autonomy_ticket_inputs
                        SET delivered_at = NOW()
                        WHERE ticket_id = %s AND delivered_at IS NULL
                        RETURNING input_id, kind, text, created_at
                    )
                    SELECT input_id, kind, text, created_at
                    FROM delivered ORDER BY created_at
                    """,
                    (row["ticket_id"],),
                )
                inputs = []
                for input_row in await cur.fetchall():
                    item = {**dict(input_row), "input_id": str(input_row["input_id"])}
                    created_at = item.get("created_at")
                    if hasattr(created_at, "isoformat"):
                        item["created_at"] = created_at.isoformat()
                    inputs.append(item)
                await cur.execute(
                    "UPDATE autonomy_daemon_claims SET input_snapshot = %s WHERE claim_id = %s",
                    (Json(inputs), claim_id),
                )
                out = _claim_result(
                    row,
                    lease_id=claim_id,
                    lease_expires_at=row["worker_lease_expires_at"],
                    lease_duration_seconds=LEASE_SECONDS,
                    policy_version=policy_version,
                    inputs=inputs,
                )
                for key in (
                    "ticket_id", "router_thread_id", "worker_thread_id",
                    "worker_run_id", "worker_daemon_id", "worker_lease_id",
                ):
                    if out.get(key):
                        out[key] = str(out[key])
                return out


def _grant_dict(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else None
    if isinstance(value, dict):
        return dict(value)
    return None


async def read_lease_grant(daemon_id: str, lease_id: str) -> dict[str, Any] | None:
    """The grant already stored for this lease, if the server signed one."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT lease_grant FROM autonomy_daemon_claims
                   WHERE daemon_id = %s AND lease_id = %s""",
                (daemon_id, lease_id),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return _grant_dict(row.get("lease_grant"))


async def store_lease_grant(
    daemon_id: str, lease_id: str, grant: dict[str, Any]
) -> dict[str, Any] | None:
    """Persist the first grant and return the grant that stands.

    A retried claim loses the race to the row already written and returns
    that one, so the daemon never sees a second signature for the same lease.
    """
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """UPDATE autonomy_daemon_claims
                       SET lease_grant = %s
                       WHERE daemon_id = %s AND lease_id = %s
                         AND lease_grant IS NULL
                       RETURNING lease_grant""",
                    (Json(grant), daemon_id, lease_id),
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        """SELECT lease_grant FROM autonomy_daemon_claims
                           WHERE daemon_id = %s AND lease_id = %s""",
                        (daemon_id, lease_id),
                    )
                    row = await cur.fetchone()
    if row is None:
        return None
    return _grant_dict(row.get("lease_grant"))


async def lease_binding(daemon_id: str, lease_id: str) -> dict[str, Any] | None:
    """The identity a report has to match, for a lease this daemon holds."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT daemon_id, policy_version, lease_grant, revoked_at
                   FROM autonomy_daemon_claims
                   WHERE daemon_id = %s AND lease_id = %s""",
                (daemon_id, lease_id),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    return {
        "daemon_id": str(row["daemon_id"]),
        "policy_version": str(row.get("policy_version") or ""),
        "grant": _grant_dict(row.get("lease_grant")),
        "revoked_at": row.get("revoked_at"),
    }


async def lease_status(daemon_id: str, lease_id: str) -> dict[str, Any] | None:
    """Whether this daemon's lease is still allowed to run."""
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """SELECT lease_id, daemon_id, policy_version, revoked_at
                   FROM autonomy_daemon_claims
                   WHERE daemon_id = %s AND lease_id = %s""",
                (daemon_id, lease_id),
            )
            row = await cur.fetchone()
    if row is None:
        return None
    revoked_at = row.get("revoked_at")
    return {
        "lease_id": str(row["lease_id"]),
        "daemon_id": str(row["daemon_id"]),
        "policy_version": str(row.get("policy_version") or ""),
        "revoked": revoked_at is not None,
        "revoked_at": revoked_at,
    }


async def revoke_lease(user_id: str, lease_id: str) -> str:
    """Mark the owner's lease revoked. Idempotent for a lease already revoked."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """UPDATE autonomy_daemon_claims AS claims
                       SET revoked_at = NOW()
                       FROM autonomy_daemons AS daemons
                       WHERE claims.lease_id = %s
                         AND claims.daemon_id = daemons.daemon_id
                         AND daemons.user_id = %s
                         AND claims.revoked_at IS NULL
                       RETURNING claims.lease_id""",
                    (lease_id, user_id),
                )
                if await cur.fetchone() is not None:
                    return "revoked"
                await cur.execute(
                    """SELECT claims.revoked_at
                       FROM autonomy_daemon_claims AS claims
                       JOIN autonomy_daemons AS daemons
                         ON daemons.daemon_id = claims.daemon_id
                       WHERE claims.lease_id = %s AND daemons.user_id = %s""",
                    (lease_id, user_id),
                )
                row = await cur.fetchone()
    if row is None:
        return "not_found"
    return "revoked"


async def owned_ticket(daemon_id: str, ticket_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_TICKET_COLUMNS}
                FROM autonomy_tickets
                WHERE ticket_id = %s AND worker_daemon_id = %s
                  AND user_id = (SELECT user_id FROM autonomy_daemons WHERE daemon_id = %s)
                """,
                (ticket_id, daemon_id, daemon_id),
            )
            row = await cur.fetchone()
    return dict(row) if row else None
