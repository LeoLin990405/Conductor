"""Authenticated review queue and host-local, loopback-only merge API.

Review is an ordinary authenticated surface: the server owns the queue, so a
CLI on any machine sees the same records, and the self-review rule is enforced
server-side where a second client cannot skip it.

Merge is not remote-available. It is only reachable from a loopback peer on
the machine running the backend: it executes ``git`` and a test command against
host worktrees, and a hosted deployment that fronts the API with a proxy sees
the proxy as the peer (non-loopback), so no forwarding header can open it. The
route also requires the per-user ``super_agent`` feature, like the rest of the
Super-agent surface.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request

from src.server.models.review_merge import (
    AuditList,
    AuditView,
    MergeRequest,
    MergeResult,
    ReviewCreate,
    ReviewList,
    ReviewVerdictRequest,
    ReviewView,
)
from src.server.services import features
from src.server.services.autonomy import review_merge as service
from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1", tags=["Review & merge"])


async def _require_feature(user_id: str) -> None:
    if not await features.user_feature_enabled(user_id, "super_agent"):
        # Hidden surface, not a permission error: the feature catalog is the
        # only thing that should tell a client this exists.
        raise HTTPException(status_code=404, detail="Feature not enabled")


def _is_local_merge(request: Request) -> bool:
    """Loopback peer + configured local Host + no forwarding headers."""
    return service.is_local_merge_request(
        peer=request.client.host if request.client else "",
        hostname=request.url.hostname,
        headers=request.headers,
    )


def _raise(exc: service.ReviewMergeError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.as_detail())


@router.post("/reviews", response_model=ReviewView, status_code=201)
async def create_review(
    body: ReviewCreate, x_user_id: CurrentUserId
) -> ReviewView:
    """Queue a cross-review; a line reviewing itself is refused here."""
    await _require_feature(x_user_id)
    try:
        row = await service.queue_review(
            user_id=x_user_id,
            line=body.line,
            reviewer=body.reviewer,
            provider=body.provider,
            claimed_root=body.root,
        )
    except service.ReviewMergeError as exc:
        _raise(exc)
    return ReviewView.model_validate(row)


@router.get("/reviews", response_model=ReviewList)
async def list_reviews(
    x_user_id: CurrentUserId,
    line: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
) -> ReviewList:
    await _require_feature(x_user_id)
    rows = await service.list_reviews(x_user_id, line=line, limit=limit)
    return ReviewList(reviews=[ReviewView.model_validate(row) for row in rows])


@router.get("/reviews/{review_id}", response_model=ReviewView)
async def get_review(review_id: UUID, x_user_id: CurrentUserId) -> ReviewView:
    await _require_feature(x_user_id)
    try:
        row = await service.get_review(x_user_id, str(review_id))
    except service.ReviewMergeError as exc:
        _raise(exc)
    return ReviewView.model_validate(row)


@router.post("/reviews/{review_id}/verdict", response_model=ReviewView)
async def record_review_verdict(
    review_id: UUID, body: ReviewVerdictRequest, x_user_id: CurrentUserId
) -> ReviewView:
    """Finish a queued review, binding the verdict to the reviewed commit."""
    await _require_feature(x_user_id)
    try:
        row = await service.complete_review(
            user_id=x_user_id,
            review_id=str(review_id),
            verdict=body.verdict,
            note=body.note,
            claimed_root=body.root,
        )
    except service.ReviewMergeError as exc:
        _raise(exc)
    return ReviewView.model_validate(row)


@router.get("/review-audit", response_model=AuditList)
async def list_review_audit(
    x_user_id: CurrentUserId,
    line: str | None = Query(None, max_length=200),
    action: str | None = Query(None, max_length=64),
    limit: int = Query(100, ge=1, le=200),
) -> AuditList:
    """The durable audit trail: review requests/verdicts and merge attempts."""
    await _require_feature(x_user_id)
    try:
        rows = await service.list_audit(
            x_user_id, line=line, action=action, limit=limit
        )
    except service.ReviewMergeError as exc:
        _raise(exc)
    return AuditList(audit=[AuditView.model_validate(row) for row in rows])


@router.post("/merges", response_model=MergeResult)
async def merge_line(
    body: MergeRequest, x_user_id: CurrentUserId, request: Request
) -> MergeResult:
    """Rehearse and land a merge; only a loopback client may call this."""
    await _require_feature(x_user_id)
    if not _is_local_merge(request):
        await service.audit_local_denial(
            x_user_id, line=body.line, into=body.into
        )
        detail = {
            "code": "local_only",
            "message": (
                "Merge is only available to a local client on the Conductor host "
                "(loopback peer, a configured local Host, and no forwarding headers)."
            ),
            "remediation": (
                "Run the merge from the machine that runs the Conductor backend, "
                "using http://127.0.0.1:8000 or http://localhost:8000."
            ),
            "detail": {},
        }
        raise HTTPException(status_code=403, detail=detail)
    try:
        result = await service.merge(
            user_id=x_user_id,
            line=body.line,
            into=body.into,
            test_cmd=body.test_cmd,
            claimed_root=body.root,
        )
    except service.ReviewMergeError as exc:
        _raise(exc)
    return MergeResult.model_validate(result)
