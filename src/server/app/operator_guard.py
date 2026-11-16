"""Operator-only guard for deployment-wide writes (R6 review fix 2).

The role registry and the Router handoff change behaviour for every user of
the deployment, so a signed-in user is not enough: the request must carry the
internal service token (``X-Service-Token``), the credential the N80 operator
routes and the local ``cond`` operator verbs already use. With no
``INTERNAL_SERVICE_TOKEN`` configured these writes are refused.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from src.server.utils import api as api_utils


async def require_operator(request: Request) -> str:
    """The operator's ``X-User-Id`` (or ``operator``); 403 otherwise."""
    matched, user_id = api_utils._service_token_user_id(request)
    if not matched:
        raise HTTPException(status_code=403, detail="operator credential required")
    return str(user_id or "operator")
