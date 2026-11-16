"""The role registry API (R6 L1, N2).

``GET /api/v1/roles`` shows the four roles (candidates, default, constraints)
and the hard rules; ``PUT /api/v1/roles/{role}`` changes one role with a
reason, validated and audited, and needs the operator credential
(``X-Service-Token``) because the registry is deployment-wide; ``GET /api/v1/roles/audit`` lists the changes.
A refused change (a hard rule, an unknown field) is a 422 with the reason; a
stale ``expected_revision`` is a 409.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.server.services.autonomy.role_registry import (
    RegistryConflictError,
    describe,
    get_service,
)
from src.server.services.autonomy.role_types import ROLES, RoleRegistryError
from src.server.app.operator_guard import require_operator
from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1/roles", tags=["role-registry"])


class RoleUpdateRequest(BaseModel):
    candidates: list[str] | None = None
    default: str | None = None
    constraints: dict[str, Any] | None = None
    extra: dict[str, Any] | None = None
    reason: str = Field(min_length=1, max_length=2000)
    expected_revision: int | None = None


@router.get("")
async def get_roles(user_id: CurrentUserId) -> dict[str, Any]:
    registry = await get_service().get()
    return describe(registry)


@router.get("/audit")
async def get_audit(
    user_id: CurrentUserId,
    role: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    try:
        rows = await get_service().audit(limit=limit, role=role)
    except RoleRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"audit": rows}


@router.put("/{role}")
async def put_role(
    role: str,
    body: RoleUpdateRequest,
    user_id: str = Depends(require_operator),
) -> dict[str, Any]:
    if role not in ROLES:
        raise HTTPException(status_code=404, detail="unknown role")
    changes = {
        key: value
        for key, value in body.model_dump().items()
        if key in {"candidates", "default", "constraints", "extra"} and value is not None
    }
    if not changes:
        raise HTTPException(status_code=422, detail="nothing to change")
    service = get_service()
    try:
        registry = await service.update_role(
            role,
            changes,
            actor=f"operator:{user_id}",
            reason=body.reason,
            expected_revision=body.expected_revision,
        )
    except RegistryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RoleRegistryError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    service.install()
    return describe(registry)
