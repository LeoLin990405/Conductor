"""Router handoff API (R6 L3): which model serves as the Super-agent, and switch it.

``GET /api/v1/router`` returns the active Router model, its candidates and the
recent handoffs; ``POST /api/v1/router/handoff`` switches the Router to another
candidate with a reason (audited as a role-registry change). A target that is
not a Router candidate, or is already active, is a 422.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.server.services.autonomy import router_handoff
from src.server.services.autonomy.role_registry import get_service
from src.server.services.autonomy.role_types import ROLE_ROUTER, RoleRegistryError
from src.server.app.operator_guard import require_operator
from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1/router", tags=["router-handoff"])


class HandoffRequest(BaseModel):
    to_model: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)


@router.get("")
async def get_router(user_id: CurrentUserId) -> dict[str, Any]:
    registry = await get_service().get()
    entry = registry.entry(ROLE_ROUTER)
    history = entry.extra.get("handoffs")
    return {
        "active_model": router_handoff.active_router_model(),
        "candidates": list(entry.candidate_names),
        "source": registry.source,
        "handoffs": list(history) if isinstance(history, list) else [],
    }


@router.post("/handoff")
async def post_handoff(
    body: HandoffRequest, user_id: str = Depends(require_operator)
) -> dict[str, Any]:
    try:
        event = await router_handoff.handoff(
            body.to_model, reason=body.reason, actor=f"operator:{user_id}"
        )
    except RoleRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record = event.to_dict()
    record.pop("sessions", None)
    return {"handoff": record, "active_model": router_handoff.active_router_model()}
