"""Review requests and verdicts, with app-id self-review refused in SQL too."""

from __future__ import annotations

import logging
import re
from contextlib import nullcontext
from datetime import datetime
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.collab.types import (
    InvalidSchemaError,
    ScopeDeniedError,
    SelfReviewError,
    normalize_app_id,
    normalize_task_ref,
)
from src.server.database.pool import get_db_connection
from src.tools.auto_dispatch.dedup import dedup_mode
from src.tools.auto_dispatch.review_strict import strict_mode

logger = logging.getLogger(__name__)

_REVIEW_COLUMNS = """
    review_id, requester_app_id, author_app_id, reviewer_target,
    reviewer_app_id, target_ref, lease_id, commit_sha, focus, state,
    expires_at, verdict, created_at, completed_at
"""
_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")
_VERDICT_REQUIRED = frozenset({"passed", "blockers", "verdict"})
#: Optional N59 keys (see ``src/tools/auto_dispatch/verdict.schema.json``).
_VERDICT_OPTIONAL = frozenset({"status", "needed_evidence"})
_VERDICT_STATUSES = frozenset({"pass", "fail", "inconclusive"})


def _review(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("review_id", "lease_id", "reviewer_app_id"):
        if out.get(key):
            out[key] = str(out[key])
    verdict = out.get("verdict")
    out["verdict"] = dict(verdict) if isinstance(verdict, dict) else None
    return out


def _reviewer(value: str | None) -> tuple[str, str | None]:
    """Split ``app:<id>`` / ``native`` / ``any`` into (target, expected app)."""
    target = str(value or "native").strip()
    if target.startswith("app:"):
        return target, normalize_app_id(target[4:])
    if target in {"native", "any"}:
        return target, None
    # A bare app id is accepted as a convenience, but stored canonically.
    return f"app:{normalize_app_id(target)}", normalize_app_id(target)


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 20:
        raise InvalidSchemaError(f"review {name} must be a list of at most 20 items")
    if not all(isinstance(item, str) and len(item) <= 500 for item in value):
        raise InvalidSchemaError(f"each review {name} item must be at most 500 characters")
    return list(value)


def _validate_verdict(verdict: Any) -> dict[str, Any]:
    # The optional N59 keys are accepted only while strict review is not
    # ``off``; ``off`` keeps the closed pre-P0 shape (and its message).
    optional = _VERDICT_OPTIONAL if strict_mode() != "off" else frozenset()
    keys = set(verdict) if isinstance(verdict, dict) else set()
    if not (
        isinstance(verdict, dict)
        and _VERDICT_REQUIRED <= keys
        and keys <= _VERDICT_REQUIRED | optional
    ):
        raise InvalidSchemaError(
            "review verdict must contain passed, blockers and verdict, and "
            "optionally status and needed_evidence"
            if optional
            else "review verdict must contain exactly passed, blockers and verdict"
        )
    if not isinstance(verdict["passed"], bool):
        raise InvalidSchemaError("review verdict passed must be a boolean")
    blockers = verdict["blockers"]
    if not isinstance(blockers, list) or len(blockers) > 20:
        raise InvalidSchemaError("review blockers must be a list of at most 20 items")
    if not all(isinstance(item, str) and len(item) <= 500 for item in blockers):
        raise InvalidSchemaError("each review blocker must be at most 500 characters")
    text = verdict["verdict"]
    if not isinstance(text, str) or len(text) > 2000:
        raise InvalidSchemaError("review verdict text must be at most 2000 characters")
    clean: dict[str, Any] = {"passed": verdict["passed"], "blockers": list(blockers), "verdict": text}
    if "status" in verdict:
        if verdict["status"] not in _VERDICT_STATUSES:
            raise InvalidSchemaError("review verdict status must be pass, fail or inconclusive")
        clean["status"] = verdict["status"]
    if "needed_evidence" in verdict:
        clean["needed_evidence"] = _string_list(verdict["needed_evidence"], "needed_evidence")
    return clean


def _dedup_lock_key(requester: str, target: str, commit_sha: str, reviewer_target: str) -> str:
    return f"collab_review_dedup:{requester}:{target}:{commit_sha}:{reviewer_target}"


async def create_review(
    *,
    requester_app_id: str,
    author_app_id: str,
    target_ref: str,
    commit_sha: str,
    reviewer: str = "native",
    focus: str = "",
    lease_id: str | None = None,
    expires_at: datetime | None = None,
    force: bool = False,
    conn: Any = None,
) -> dict[str, Any]:
    """Create a pending review; an author naming itself is refused first.

    De-duplication (``CONDUCTOR_DISPATCH_DEDUP_MODE``): a pending or completed
    review of the same requester, target, commit and reviewer already exists ->
    ``enforce`` returns that row marked ``"deduplicated": True`` unless
    ``force``; ``shadow`` (default) logs it and inserts as before; ``off``
    does not look. The look and the insert run in one transaction under a
    transaction-scoped advisory lock on that key, so two concurrent requests
    cannot both miss the existing row.
    """
    requester = normalize_app_id(requester_app_id)
    author = normalize_app_id(author_app_id)
    reviewer_target, reviewer_app = _reviewer(reviewer)
    normalized_target = normalize_task_ref(target_ref)
    if reviewer_app is not None and reviewer_app == author:
        raise SelfReviewError("an app cannot review its own work")
    if not _COMMIT_PATTERN.fullmatch(str(commit_sha or "")):
        raise InvalidSchemaError("commit must be a 7-40 character lower-case SHA")
    if len(focus) > 1000:
        raise InvalidSchemaError("review focus is longer than 1000 characters")
    mode = dedup_mode()
    async with get_db_connection(conn) as db:
        # A caller that passes ``conn`` owns the transaction (see
        # ``collab_transaction``); otherwise open one so the advisory lock,
        # the duplicate look and the insert share it (``off`` keeps the bare
        # pre-P0 insert).
        locking = mode != "off" and conn is None
        transaction = db.transaction() if locking else nullcontext()
        async with transaction:
            async with db.cursor(row_factory=dict_row) as cur:
                if mode != "off":
                    await cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (_dedup_lock_key(requester, normalized_target, commit_sha, reviewer_target),),
                    )
                    await cur.execute(
                        f"""
                        SELECT {_REVIEW_COLUMNS}
                        FROM collab_reviews
                        WHERE requester_app_id = %s
                          AND target_ref = %s
                          AND commit_sha = %s
                          AND reviewer_target = %s
                          AND state IN ('pending', 'completed')
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        (requester, normalized_target, commit_sha, reviewer_target),
                    )
                    existing = _review(await cur.fetchone())
                    if existing is not None:
                        reuse = mode == "enforce" and not force
                        logger.info(
                            "collab review dedup mode=%s forced=%s %s: %s@%s for %s "
                            "already has review %s (%s)",
                            mode,
                            bool(force),
                            "reusing" if reuse else "would reuse",
                            normalized_target,
                            commit_sha,
                            reviewer_target,
                            existing.get("review_id"),
                            existing.get("state"),
                        )
                        if reuse:
                            existing["deduplicated"] = True
                            return existing
                await cur.execute(
                    f"""
                    INSERT INTO collab_reviews (
                        requester_app_id, author_app_id, reviewer_target,
                        reviewer_app_id, target_ref, lease_id, commit_sha, focus,
                        expires_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s, NOW() + INTERVAL '24 hours')
                    )
                    RETURNING {_REVIEW_COLUMNS}
                    """,
                    (
                        requester,
                        author,
                        reviewer_target,
                        reviewer_app,
                        normalized_target,
                        lease_id,
                        commit_sha,
                        focus,
                        expires_at,
                    ),
                )
                return _review(await cur.fetchone())  # type: ignore[return-value]


async def get_review(review_id: str) -> dict[str, Any] | None:
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"SELECT {_REVIEW_COLUMNS} FROM collab_reviews WHERE review_id = %s",
                (review_id,),
            )
            return _review(await cur.fetchone())


async def list_reviews(
    *,
    app_id: str | None = None,
    reviewer_target: str | None = None,
    state: str = "pending",
    limit: int = 20,
) -> list[dict[str, Any]]:
    conditions = ["state = %s"]
    params: list[Any] = [state]
    if reviewer_target is not None:
        conditions.append("reviewer_target = %s")
        params.append(str(reviewer_target))
    if app_id is not None:
        normalized = normalize_app_id(app_id)
        conditions.append(
            "(reviewer_target = 'any' OR reviewer_target = %s "
            "OR reviewer_app_id = %s)"
        )
        params.extend([f"app:{normalized}", normalized])
    params.append(max(1, min(50, int(limit))))
    async with get_db_connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT {_REVIEW_COLUMNS}
                FROM collab_reviews
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at
                LIMIT %s
                """,
                tuple(params),
            )
            return [_review(row) for row in await cur.fetchall() if row]  # type: ignore[misc]


async def submit_review(
    *,
    review_id: str,
    reviewer_app_id: str,
    verdict: Any,
    conn: Any = None,
) -> dict[str, Any] | None:
    """Complete a pending review; author and assignee are checked again.

    Pass ``conn`` from :func:`collab_transaction` so the verdict and the audit
    row that describes it commit or roll back together.
    """
    reviewer = normalize_app_id(reviewer_app_id)
    clean = _validate_verdict(verdict)
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT author_app_id, reviewer_target, state
                FROM collab_reviews
                WHERE review_id = %s
                """,
                (review_id,),
            )
            existing = await cur.fetchone()
            if existing is None or existing.get("state") != "pending":
                return None
            if str(existing.get("author_app_id") or "") == reviewer:
                raise SelfReviewError("an app cannot review its own work")
            # A row written before the target was selected is an open review;
            # the database default is ``native``, so an empty value is only a
            # legacy/fake row, not an assignment to a different app.
            target = str(existing.get("reviewer_target") or "any")
            if target not in {"any", f"app:{reviewer}"}:
                raise ScopeDeniedError(
                    "the review is assigned to a different reviewer",
                    details={"reviewer_target": target},
                )
            await cur.execute(
                f"""
                UPDATE collab_reviews
                SET state = 'completed',
                    verdict = %s,
                    reviewer_app_id = %s,
                    completed_at = NOW()
                WHERE review_id = %s
                  AND state = 'pending'
                  AND author_app_id <> %s
                RETURNING {_REVIEW_COLUMNS}
                """,
                (Json(clean), reviewer, review_id, reviewer),
            )
            return _review(await cur.fetchone())


async def expire_reviews(
    *, limit: int = 100, conn: Any = None
) -> list[dict[str, Any]]:
    bounded = max(1, min(1000, int(limit)))
    async with get_db_connection(conn) as db:
        async with db.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE collab_reviews
                SET state = 'expired', completed_at = NOW()
                WHERE review_id IN (
                    SELECT review_id
                    FROM collab_reviews
                    WHERE state = 'pending' AND expires_at <= NOW()
                    ORDER BY expires_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING review_id, author_app_id, reviewer_target
                """,
                (bounded,),
            )
            return [dict(row) for row in await cur.fetchall()]


__all__ = [
    "create_review",
    "expire_reviews",
    "get_review",
    "list_reviews",
    "submit_review",
]
