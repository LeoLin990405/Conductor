"""In-memory stand-in for ``database.autonomy.reviews`` in router tests.

The router tests care about permission checks, wire shapes and the merge gate,
not SQL (the database module has its own fake-cursor tests). This store keeps
the same function signatures as the real module so ``monkeypatch.setattr`` on
the module can swap them in wholesale.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any


def _now() -> datetime:
    return datetime.now(UTC)


class FakeReviewStore:
    def __init__(self) -> None:
        self.reviews: dict[str, dict[str, Any]] = {}
        self.audit: list[dict[str, Any]] = []

    # -- reviews ---------------------------------------------------------

    async def create_review(
        self,
        *,
        user_id: str,
        line: str,
        reviewer: str,
        head_sha: str = "",
        reviewer_provider: str = "codex",
    ) -> dict[str, Any]:
        row = {
            "review_id": str(uuid.uuid4()),
            "user_id": user_id,
            "line": line,
            "reviewer": reviewer,
            "reviewer_provider": reviewer_provider,
            "state": "queued",
            "verdict": None,
            "head_sha": head_sha,
            "note": "",
            "created_at": _now(),
            "completed_at": None,
        }
        self.reviews[row["review_id"]] = row
        return dict(row)

    async def list_reviews(
        self, user_id: str, *, line: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self.reviews.values()
            if row["user_id"] == user_id and (line is None or row["line"] == line)
        ]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows[:limit]

    async def get_review(
        self, user_id: str, review_id: str
    ) -> dict[str, Any] | None:
        row = self.reviews.get(review_id)
        if row is None or row["user_id"] != user_id:
            return None
        return dict(row)

    async def complete_review(
        self,
        user_id: str,
        review_id: str,
        *,
        verdict: str,
        note: str = "",
    ) -> dict[str, Any] | None:
        row = self.reviews.get(review_id)
        if (
            row is None
            or row["user_id"] != user_id
            or row["state"] != "queued"
        ):
            return None
        row.update(
            state="completed",
            verdict=verdict,
            note=note,
            completed_at=_now(),
        )
        return dict(row)

    # -- audit -----------------------------------------------------------

    async def record_audit(
        self,
        *,
        user_id: str,
        action: str,
        line: str = "",
        reviewer: str = "",
        target_branch: str = "",
        outcome: str = "ok",
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.audit.append(
            {
                "audit_id": str(uuid.uuid4()),
                "user_id": user_id,
                "action": action,
                "line": line,
                "reviewer": reviewer,
                "target_branch": target_branch,
                "outcome": outcome,
                "detail": dict(detail or {}),
                "created_at": _now(),
            }
        )

    async def list_audit(
        self,
        user_id: str,
        *,
        line: str | None = None,
        action: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self.audit
            if row["user_id"] == user_id
            and (line is None or row["line"] == line)
            and (action is None or row["action"] == action)
        ]
        rows.sort(key=lambda row: row["created_at"], reverse=True)
        return rows[:limit]
