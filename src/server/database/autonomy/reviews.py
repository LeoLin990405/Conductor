"""Durable review records and the review/merge audit trail.

The merge-time gate is a Codex review the merge route itself runs; the queue
here is the operator-facing review record. Its verdict is CAS-bound: the line
HEAD is captured when the review is *queued*, and a verdict is only accepted
while the line is still on that commit, so a review can never be stamped onto
work it did not cover.

Writes that mutate a review also append their audit row inside the same
explicit transaction on the same connection: the conversation pool runs with
autocommit, and without the transaction block a committed review could outlive
a failed audit insert and authorize a merge.

Audit detail holds structured, bounded fields (error code, message,
remediation, commit SHAs). Raw test/process output and secret material never go
in.
"""

from __future__ import annotations

from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection

_REVIEW_COLUMNS = """
    review_id, user_id, line, reviewer, reviewer_provider, state,
    verdict, head_sha, note, created_at, completed_at
"""
_AUDIT_COLUMNS = """
    audit_id, user_id, action, line, reviewer, target_branch, outcome,
    detail, created_at
"""
AUDIT_ACTIONS = frozenset(
    {
        "review_requested",
        "review_completed",
        "merge_refused",
        "merge_started",
        "merge_merged",
        "merge_failed",
    }
)
AUDIT_OUTCOMES = frozenset({"ok", "refused", "failed"})


def _review(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    out["review_id"] = str(out.get("review_id") or "")
    return out


def _audit(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["audit_id"] = str(out.get("audit_id") or "")
    out["detail"] = dict(out.get("detail") or {})
    return out


async def insert_audit(
    cur: Any,
    *,
    user_id: str,
    action: str,
    line: str = "",
    reviewer: str = "",
    target_branch: str = "",
    outcome: str = "ok",
    detail: dict[str, Any] | None = None,
) -> None:
    """Record one audit row on the caller's cursor/transaction."""
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unsupported review/merge audit action {action!r}")
    if outcome not in AUDIT_OUTCOMES:
        raise ValueError(f"unsupported review/merge audit outcome {outcome!r}")
    await cur.execute(
        """
        INSERT INTO autonomy_review_audit (
            user_id, action, line, reviewer, target_branch, outcome, detail
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """,
        (
            user_id,
            action,
            line[:200],
            reviewer[:200],
            target_branch[:200],
            outcome,
            Json(detail or {}),
        ),
    )


async def record_audit(
    *,
    user_id: str,
    action: str,
    line: str = "",
    reviewer: str = "",
    target_branch: str = "",
    outcome: str = "ok",
    detail: dict[str, Any] | None = None,
) -> None:
    """Standalone audit write for events whose other effects are git-side."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await insert_audit(
                    cur,
                    user_id=user_id,
                    action=action,
                    line=line,
                    reviewer=reviewer,
                    target_branch=target_branch,
                    outcome=outcome,
                    detail=detail,
                )


async def create_review(
    *,
    user_id: str,
    line: str,
    reviewer: str,
    head_sha: str,
    reviewer_provider: str = "codex",
) -> dict[str, Any]:
    """Queue one cross-review and its audit row in a single transaction."""
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    INSERT INTO autonomy_review_records (
                        user_id, line, reviewer, reviewer_provider, head_sha
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING {_REVIEW_COLUMNS}
                    """,
                    (
                        user_id,
                        line[:200],
                        reviewer[:200],
                        reviewer_provider[:64],
                        head_sha[:64],
                    ),
                )
                row = _review(await cur.fetchone())
                # RETURNING on a successful INSERT always yields a row; the route
                # turns an unexpected None into a generic 500 rather than guessing.
                if row is None:  # pragma: no cover - defensive
                    raise RuntimeError("review insert returned no row")
                await insert_audit(
                    cur,
                    user_id=user_id,
                    action="review_requested",
                    line=row["line"],
                    reviewer=row["reviewer"],
                    detail={
                        "review_id": row["review_id"],
                        "reviewer_provider": row["reviewer_provider"],
                        "head_sha": row["head_sha"],
                    },
                )
                return row


async def list_reviews(
    user_id: str, *, line: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            if line is None:
                await cur.execute(
                    f"""
                    SELECT {_REVIEW_COLUMNS}
                    FROM autonomy_review_records
                    WHERE user_id = %s
                    ORDER BY created_at DESC, review_id DESC
                    LIMIT %s
                    """,
                    (user_id, limit),
                )
            else:
                await cur.execute(
                    f"""
                    SELECT {_REVIEW_COLUMNS}
                    FROM autonomy_review_records
                    WHERE user_id = %s AND line = %s
                    ORDER BY created_at DESC, review_id DESC
                    LIMIT %s
                    """,
                    (user_id, line, limit),
                )
            return [_review(row) for row in await cur.fetchall()]


async def get_review(user_id: str, review_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_REVIEW_COLUMNS}
                FROM autonomy_review_records
                WHERE review_id = %s AND user_id = %s
                """,
                (review_id, user_id),
            )
            return _review(await cur.fetchone())


async def complete_review(
    user_id: str,
    review_id: str,
    *,
    verdict: str,
    note: str = "",
) -> dict[str, Any] | None:
    """Finish a queued review; ``None`` means it was not queued for this user.

    The ``state = 'queued'`` predicate is the CAS: two racing completions both
    reach the UPDATE, exactly one returns a row. ``head_sha`` stays as captured
    when the review was queued; the service refuses the verdict when the line
    has moved since.
    """
    if verdict not in {"merge", "block"}:
        raise ValueError(f"unsupported review verdict {verdict!r}")
    async with get_db_connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"""
                    UPDATE autonomy_review_records
                    SET state = 'completed',
                        verdict = %s,
                        note = %s,
                        completed_at = NOW()
                    WHERE review_id = %s AND user_id = %s AND state = 'queued'
                    RETURNING {_REVIEW_COLUMNS}
                    """,
                    (verdict, note[:2000], review_id, user_id),
                )
                row = _review(await cur.fetchone())
                if row is None:
                    return None
                await insert_audit(
                    cur,
                    user_id=user_id,
                    action="review_completed",
                    line=row["line"],
                    reviewer=row["reviewer"],
                    detail={
                        "review_id": row["review_id"],
                        "verdict": row["verdict"],
                        "head_sha": row["head_sha"],
                        "reviewer_provider": row["reviewer_provider"],
                    },
                )
                return row


async def list_audit(
    user_id: str,
    *,
    line: str | None = None,
    action: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    clauses = ["user_id = %s"]
    params: list[Any] = [user_id]
    if line is not None:
        clauses.append("line = %s")
        params.append(line)
    if action is not None:
        clauses.append("action = %s")
        params.append(action)
    params.append(limit)
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_AUDIT_COLUMNS}
                FROM autonomy_review_audit
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC, audit_id DESC
                LIMIT %s
                """,
                tuple(params),
            )
            return [_audit(row) for row in await cur.fetchall()]
