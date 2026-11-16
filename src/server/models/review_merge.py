"""Wire shapes for the host-local review queue and merge API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ReviewState = Literal["queued", "completed", "failed"]
ReviewVerdict = Literal["merge", "block"]


class ReviewCreate(BaseModel):
    """Queue one cross-review of ``line`` by ``reviewer``."""

    line: str = Field(..., min_length=1, max_length=200)
    reviewer: str = Field(..., min_length=1, max_length=200)
    # The cross-review queue is the Codex queue; another provider is recorded
    # but never unlocks a merge.
    provider: str = Field("codex", max_length=64)
    root: str | None = Field(None, max_length=4096)


class ReviewVerdictRequest(BaseModel):
    """Finish a queued review with the reviewer's verdict."""

    verdict: ReviewVerdict
    note: str = Field("", max_length=2000)
    root: str | None = Field(None, max_length=4096)


class ReviewView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # ``id`` keeps the JSON shape the file-backed queue printed.
    id: str
    line: str
    reviewer: str
    provider: str
    state: ReviewState
    verdict: ReviewVerdict | None = None
    head_sha: str = ""
    note: str = ""
    created_at: datetime
    completed_at: datetime | None = None


class ReviewList(BaseModel):
    reviews: list[ReviewView]


class AuditView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    audit_id: str
    action: str
    line: str = ""
    reviewer: str = ""
    target_branch: str = ""
    outcome: str
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AuditList(BaseModel):
    audit: list[AuditView]


class MergeRequest(BaseModel):
    """Merge one line into the worktree checked out on ``into``."""

    line: str = Field(..., min_length=1, max_length=200)
    into: str = Field(..., min_length=1, max_length=200)
    test_cmd: str | None = Field(
        None,
        max_length=4096,
        description=(
            "Command run in the temporary rehearsal worktree. Omit to use the "
            "server's configured default."
        ),
    )
    root: str | None = Field(None, max_length=4096)


class MergeResult(BaseModel):
    """The merge that landed; the keys the CLI envelope already exposes."""

    merged: bool
    line: str
    branch: str
    into: str
    commit: str
    tested_sha: str
    tests: str
    pushed: bool = False
    tested_in_temporary_worktree: bool = True
    codex_verdict: str = "merge"
    # The merge is landed as a ref CAS; the target worktree is not rewritten by
    # the server, so the caller must sync its checkout.
    landed_ref: str = ""
    worktree_sync_required: bool = True
