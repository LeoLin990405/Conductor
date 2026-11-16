"""Local OSS update endpoints for the one-click About/Update card."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from src.server.utils.api import CurrentUserId
from src.server.services import local_updater

router = APIRouter(prefix="/api/v1/system", tags=["system"])


def _require_loopback(request: Request) -> None:
    client = request.client.host if request.client else ""
    if client not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="Local updates are only available on loopback.")


def _raise(exc: local_updater.LocalUpdaterError) -> None:
    raise HTTPException(status_code=409, detail=str(exc))


@router.get("/version")
async def local_version(user_id: CurrentUserId, request: Request):
    _require_loopback(request)
    return local_updater.status()


@router.post("/update/check")
async def local_update_check(user_id: CurrentUserId, request: Request):
    _require_loopback(request)
    try:
        return local_updater.check()
    except local_updater.LocalUpdaterError as exc:
        _raise(exc)


@router.post("/update/apply")
async def local_update_apply(user_id: CurrentUserId, request: Request):
    _require_loopback(request)
    try:
        return local_updater.apply()
    except local_updater.LocalUpdaterError as exc:
        _raise(exc)


@router.post("/update/restart")
async def local_update_restart(user_id: CurrentUserId, request: Request):
    _require_loopback(request)
    try:
        return local_updater.restart()
    except local_updater.LocalUpdaterError as exc:
        _raise(exc)
