"""Authenticated, per-user runtime readiness diagnostics."""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel

from src.server.services.autonomy.notifications import unread_count
from src.server.services.system_readiness import get_readiness
from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1/system", tags=["System"])


class ReadinessCheck(BaseModel):
    id: str
    status: Literal["ok", "missing", "degraded", "unknown"]
    required: bool
    summary: str
    verification: Literal["observed", "configuration_only", "not_checked"]


class ReadinessResponse(BaseModel):
    status: Literal["ready", "degraded", "needs_action"]
    checked_at: datetime
    verification: Literal["local_checks_only"]
    checks: list[ReadinessCheck]


class NotificationBadgeResponse(BaseModel):
    unread: int


@router.get("/readiness", response_model=ReadinessResponse)
async def readiness(user_id: CurrentUserId, response: Response) -> dict:
    """Report dependency/configuration state without external model calls."""
    response.headers["Cache-Control"] = "private, no-store"
    return await get_readiness(user_id)


@router.get("/notifications/badge", response_model=NotificationBadgeResponse)
async def notification_badge(user_id: CurrentUserId, response: Response) -> dict:
    """Unread operator alerts for the signed-in user.

    The count is the conditions that are still true (a Ticket that needs the
    user, a failure, a stuck Ticket, a dead daemon, a worker out of quota),
    after de-duplication. It is not a cacheable total.
    """
    response.headers["Cache-Control"] = "private, no-store"
    return {"unread": await unread_count(user_id)}
