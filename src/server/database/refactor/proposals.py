"""Memory-proposal queue (SQL layer, migration 094).

Extraction inserts once per ``(task_run_id, proposal_type, text_hash)``; the
review surface reads ``proposed`` rows and the approval/rejection route CASes
them out of that status. The contract jsonb remains the audit copy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from psycopg.rows import dict_row

from src.server.contracts.memory_origin import normalize_memory_metadata
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.contracts.memory_tool_policy import (
    normalize_memory_tool_policy_metadata,
)
from src.server.database import pool
from src.server.utils.pg_sanitize import (
    SafeJson,
    normalize_uuid,
    refactor_owner_uuid,
    strip_pg_nul_str,
)

_PROPOSAL_COLUMNS = (
    "proposal_id::text AS proposal_id, user_id::text AS user_id,"
    " thread_id::text AS thread_id, task_run_id::text AS task_run_id,"
    " proposal_type, proposal_key, text, status, created_at, decided_at"
    ", origin, confidence, evidence_refs, evidence_count, counts_as_evidence"
    ", change_kind, replaces_entry_key, reason, metadata"
    ", producer_kind, producer_id::text AS producer_id, source_entry_ids, producer_metadata"
    ", (SELECT sr.parent_run_id::text FROM subagent_runs sr"
    " WHERE sr.task_run_id = agent_memory_proposals.task_run_id"
    " AND sr.thread_id = agent_memory_proposals.thread_id) AS source_response_id"
)


def _policy_metadata(proposal: Mapping[str, Any]) -> dict[str, Any]:
    raw = {
        field: proposal[field]
        for field in (
            "sensitivity",
            "applies_when",
            "exceptions",
            "evidence_quote_ref",
            "kind",
            "denied_tools",
            "valid_from",
            "valid_until",
        )
        if field in proposal
    }
    if "metadata" in proposal:
        raw["metadata"] = proposal["metadata"]
    return {
        **normalize_memory_policy_metadata_layers(raw),
        **normalize_memory_tool_policy_metadata(raw),
    }


async def insert_proposals(
    *,
    user_id: str,
    thread_id: str,
    task_run_id: str,
    proposals: Sequence[Mapping[str, Any]],
) -> int:
    """Idempotently record extracted proposals; returns the inserted count.

    Each item carries ``type``/``text``/``text_hash``/``proposal_key``; the hash
    is computed by the service so the uniqueness check never hits the btree size
    limit on a long text and re-extraction of the same run is a no-op.
    """
    normalized_user = refactor_owner_uuid(user_id)
    normalized_thread = normalize_uuid(thread_id)
    normalized_run = normalize_uuid(task_run_id)
    if normalized_user is None or normalized_thread is None or normalized_run is None:
        return 0
    if not any(
        isinstance(proposal, Mapping)
        and str(proposal.get("change_kind") or "long_term") in ("long_term", "correction")
        for proposal in proposals
    ):
        return 0
    inserted = 0
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            for proposal in proposals:
                proposal_type = str(proposal.get("type") or "").strip()
                text = str(proposal.get("text") or "").strip()
                text_hash = str(proposal.get("text_hash") or "").strip()
                proposal_key = str(proposal.get("proposal_key") or "").strip()
                origin_metadata = normalize_memory_metadata(proposal)
                policy_metadata = _policy_metadata(proposal)
                change_kind = str(proposal.get("change_kind") or "long_term")
                replaces_entry_key = str(proposal.get("replaces_entry_key") or "").strip() or None
                reason = str(proposal.get("reason") or "").strip() or None
                if proposal_type not in ("user", "feedback", "project", "reference"):
                    continue
                if change_kind not in ("long_term", "correction"):
                    continue
                if change_kind == "correction" and (not replaces_entry_key or not reason):
                    continue
                if not text or len(text) > 1000 or not text_hash:
                    continue
                await cur.execute(
                    """
                    INSERT INTO agent_memory_proposals
                        (user_id, thread_id, task_run_id, proposal_type,
                         proposal_key, text, text_hash, origin, confidence,
                         evidence_refs, evidence_count, counts_as_evidence,
                         change_kind, replaces_entry_key, reason, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s)
                    ON CONFLICT (task_run_id, proposal_type, text_hash) DO NOTHING
                    """,
                    (
                        normalized_user,
                        normalized_thread,
                        normalized_run,
                        proposal_type,
                        strip_pg_nul_str(proposal_key or text[:120]),
                        strip_pg_nul_str(text),
                        text_hash,
                        origin_metadata["origin"],
                        origin_metadata["confidence"],
                        SafeJson(origin_metadata["evidence_refs"]),
                        origin_metadata["evidence_count"],
                        origin_metadata["counts_as_evidence"],
                        change_kind,
                        replaces_entry_key,
                        reason,
                        SafeJson(policy_metadata),
                    ),
                )
                inserted += cur.rowcount or 0
    return inserted


async def insert_proposal_for_run(
    *,
    user_id: str,
    thread_id: str,
    task_run_id: str,
    proposal_type: str,
    proposal_key: str,
    text: str,
    text_hash: str,
    origin: str = "assistant_inference",
    confidence: float = 0.5,
    evidence_refs: Sequence[Mapping[str, Any]] = (),
    change_kind: str = "long_term",
    replaces_entry_key: str | None = None,
    reason: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str | None:
    """Insert idempotently only when the completed run belongs to this owner."""
    normalized_user = refactor_owner_uuid(user_id)
    normalized_thread = normalize_uuid(thread_id)
    normalized_run = normalize_uuid(task_run_id)
    raw_user = str(user_id or "").strip()
    kind = str(proposal_type or "").strip()
    body = str(text or "").strip()
    digest = str(text_hash or "").strip()
    replacement = str(replaces_entry_key or "").strip() or None
    why = str(reason or "").strip() or None
    origin_metadata = normalize_memory_metadata(
        {
            "origin": origin,
            "confidence": confidence,
            "evidence_refs": evidence_refs,
        }
    )
    policy_metadata = _policy_metadata({"metadata": metadata or {}})
    if (
        normalized_user is None
        or normalized_thread is None
        or normalized_run is None
        or not raw_user
        or kind not in ("user", "feedback", "project", "reference")
        or not body
        or len(body) > 1000
        or not digest
        or change_kind not in ("long_term", "correction")
        or (change_kind == "correction" and (not replacement or not why))
    ):
        return None
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO agent_memory_proposals
                    (user_id, thread_id, task_run_id, proposal_type,
                     proposal_key, text, text_hash, origin, confidence,
                     evidence_refs, evidence_count, counts_as_evidence,
                     change_kind, replaces_entry_key, reason, metadata)
                SELECT %s, t.conversation_thread_id, r.task_run_id, %s, %s, %s, %s,
                       %s, %s, %s, %s, %s, %s, %s, %s, %s
                  FROM conversation_threads t
                  JOIN workspaces w ON w.workspace_id = t.workspace_id
                  JOIN subagent_runs r
                    ON r.thread_id = t.conversation_thread_id
                 WHERE t.conversation_thread_id = %s
                   AND w.user_id = %s
                   AND r.task_run_id = %s
                   AND r.status = 'completed'
                ON CONFLICT (task_run_id, proposal_type, text_hash)
                DO UPDATE SET text_hash = EXCLUDED.text_hash
                RETURNING proposal_id::text AS proposal_id
                """,
                (
                    normalized_user,
                    kind,
                    strip_pg_nul_str(proposal_key or body[:120]),
                    strip_pg_nul_str(body),
                    digest,
                    origin_metadata["origin"],
                    origin_metadata["confidence"],
                    SafeJson(origin_metadata["evidence_refs"]),
                    origin_metadata["evidence_count"],
                    origin_metadata["counts_as_evidence"],
                    change_kind,
                    replacement,
                    why,
                    SafeJson(policy_metadata),
                    normalized_thread,
                    raw_user,
                    normalized_run,
                ),
            )
            row = await cur.fetchone()
            return str(row["proposal_id"]) if row is not None else None


async def list_proposals(
    *, user_id: str, status: str = "proposed", limit: int = 50
) -> list[dict[str, Any]]:
    normalized_user = refactor_owner_uuid(user_id)
    if normalized_user is None:
        return []
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_PROPOSAL_COLUMNS} FROM agent_memory_proposals"
                " WHERE user_id = %s AND status = %s"
                " ORDER BY created_at DESC, proposal_id LIMIT %s",
                (normalized_user, status, limit),
            )
            return [dict(row) for row in await cur.fetchall()]


async def count_proposals(*, user_id: str, status: str = "proposed") -> int:
    normalized_user = refactor_owner_uuid(user_id)
    if normalized_user is None:
        return 0
    async with pool.get_db_connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM agent_memory_proposals"
                " WHERE user_id = %s AND status = %s",
                (normalized_user, status),
            )
            row = await cur.fetchone()
            return int(row[0] if row else 0)


async def get_proposal(proposal_id: str) -> dict[str, Any] | None:
    async with pool.get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_PROPOSAL_COLUMNS} FROM agent_memory_proposals"
                " WHERE proposal_id = %s",
                (normalize_uuid(proposal_id),),
            )
            row = await cur.fetchone()
            return dict(row) if row is not None else None


async def decide_proposal(
    *,
    proposal_id: str,
    user_id: str,
    expected_status: str,
    target_status: str,
    approval_source: str,
    before_decide: Callable[[Mapping[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any] | None:
    """Lock, apply an optional source-of-truth write, then decide the proposal.

    The row lock spans the Store write so a concurrent rejection cannot win
    after approval has materialized the memory. A failed Store write rolls back
    without changing the proposal status.
    """
    if target_status == "approved" and approval_source != "manual":
        raise ValueError("proposal approval requires manual authority")
    if target_status == "approved" and before_decide is None:
        raise ValueError("proposal approval requires a source-of-truth write")
    normalized_user = refactor_owner_uuid(user_id)
    if normalized_user is None:
        return None
    # The row lock spans the Store write done by ``before_decide``. That write
    # and its own projection reads acquire pooled connections, so this
    # transaction rides a dedicated unpooled connection instead of holding a
    # shared-pool slot across the callback (a small pool would be exhausted).
    async with pool.get_dedicated_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                if target_status == "approved":
                    # Different proposals share memory.md, so the owner lock
                    # protects its read-modify-write across all workers.
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (f"conductor:memory-approval:{normalized_user}",),
                    )
                    await cur.fetchone()
                await cur.execute(
                    f"SELECT {_PROPOSAL_COLUMNS} FROM agent_memory_proposals"
                    " WHERE proposal_id = %s AND user_id = %s FOR UPDATE",
                    (normalize_uuid(proposal_id), normalized_user),
                )
                current = await cur.fetchone()
                if current is None:
                    return None
                current = dict(current)
                if current["status"] != expected_status:
                    return current
                if before_decide is not None:
                    await before_decide(current)
                await cur.execute(
                    f"""
                    UPDATE agent_memory_proposals
                       SET status = %s, decided_at = NOW()
                     WHERE proposal_id = %s AND user_id = %s AND status = %s
                    RETURNING {_PROPOSAL_COLUMNS}
                    """,
                    (
                        target_status,
                        normalize_uuid(proposal_id),
                        normalized_user,
                        expected_status,
                    ),
                )
                row = await cur.fetchone()
                decided = dict(row) if row is not None else None
                if (
                    decided is not None
                    and target_status == "approved"
                    and decided.get("producer_kind") == "memory_consolidation"
                ):
                    await cur.execute(
                        "UPDATE agent_memory_proposals SET status = 'rejected', "
                        "decided_at = NOW() WHERE user_id = %s "
                        "AND producer_kind = 'memory_consolidation' "
                        "AND source_entry_ids = %s::uuid[] "
                        "AND proposal_id <> %s AND status = 'proposed'",
                        (
                            normalized_user,
                            [str(value) for value in decided.get("source_entry_ids") or ()],
                            normalize_uuid(proposal_id),
                        ),
                    )
                return decided
