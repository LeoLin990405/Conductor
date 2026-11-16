"""Authenticated, bounded Router decision observations."""

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from src.server.services import features
from src.server.services.autonomy.router_metrics import router_metrics_for_user
from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1/metrics", tags=["Metrics"])


@router.get("/router")
async def get_router_metrics(
    user_id: CurrentUserId,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict[str, Any]:
    """Return only this user's compact decision facts, never prompt/source data."""
    if not await features.user_feature_enabled(user_id, "super_agent"):
        raise HTTPException(status_code=404, detail="Feature not enabled")
    return await router_metrics_for_user(user_id, limit=limit)
