"""Dispatch workspace registry API.

A workspace names a git checkout an auto-dispatch run may use: its repo root,
the remote a fetch updates, the sandbox policy its workers inherit, and the
default worker provider. These routes are the CRUD surface plus the two reads a
dispatch start needs (the resolved repo root and remote, and a fetch).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.server.services.dispatch_workspaces import (
    WorkspaceInvalid,
    WorkspaceNameTaken,
    WorkspaceNotFound,
    create_workspace,
    delete_workspace,
    fetch_before_start,
    get_workspace,
    list_workspaces,
    update_workspace,
    workspace_repo,
)
from src.server.utils.api import CurrentUserId, handle_api_exceptions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/dispatch-workspaces", tags=["dispatch-workspaces"])


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    path: str = Field(min_length=1)
    git_remote: str | None = None
    sandbox_policy: str = ""
    default_worker: str = ""


class WorkspaceUpdateRequest(BaseModel):
    name: str | None = None
    path: str | None = None
    git_remote: str | None = None
    sandbox_policy: str | None = None
    default_worker: str | None = None


class WorkspaceResponse(BaseModel):
    workspace_id: str
    user_id: str
    name: str
    path: str
    git_remote: str
    sandbox_policy: str
    default_worker: str
    created_at: str | None = None
    updated_at: str | None = None


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceResponse]


class WorkspaceRepoResponse(BaseModel):
    workspace_id: str
    name: str
    path: str
    repo_root: str
    git_remote: str
    sandbox_policy: str = ""
    default_worker: str = ""


class WorkspaceFetchResponse(BaseModel):
    workspace_id: str
    repo_root: str
    remote: str
    fetched: bool
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""


#: Service exceptions to HTTP statuses. Kept in one place so every route answers
#: the same way; a 404 never says whether the id exists for another user.
def _http(error: Exception) -> HTTPException:
    if isinstance(error, WorkspaceNotFound):
        return HTTPException(status_code=404, detail="Workspace not found")
    if isinstance(error, WorkspaceNameTaken):
        # Fixed text: the name the caller sent is not read back to them.
        return HTTPException(
            status_code=409,
            detail={
                "code": "workspace_name_taken",
                "message": "A workspace with that name already exists",
            },
        )
    if isinstance(error, WorkspaceInvalid):
        return HTTPException(
            status_code=400,
            detail={
                "code": "workspace_invalid",
                "message": "The workspace path or fields are invalid",
            },
        )
    return HTTPException(status_code=500, detail="Workspace request failed")


@router.get("", response_model=WorkspaceListResponse)
@handle_api_exceptions("list dispatch workspaces", logger)
async def list_dispatch_workspaces(user_id: CurrentUserId) -> WorkspaceListResponse:
    """The caller's registered checkouts, by name."""
    rows = await list_workspaces(user_id)
    return WorkspaceListResponse(workspaces=[WorkspaceResponse(**row) for row in rows])


@router.post("", response_model=WorkspaceResponse, status_code=201)
@handle_api_exceptions("create a dispatch workspace", logger)
async def create_dispatch_workspace(
    request: WorkspaceCreateRequest, user_id: CurrentUserId
) -> WorkspaceResponse:
    """Register a checkout; an omitted remote is read from its ``origin``."""
    try:
        row = await create_workspace(
            user_id=user_id,
            name=request.name,
            path=request.path,
            git_remote=request.git_remote,
            sandbox_policy=request.sandbox_policy,
            default_worker=request.default_worker,
        )
    except (WorkspaceInvalid, WorkspaceNameTaken) as exc:
        raise _http(exc) from exc
    return WorkspaceResponse(**row)


@router.get("/{workspace_id}", response_model=WorkspaceResponse)
@handle_api_exceptions("load a dispatch workspace", logger)
async def get_dispatch_workspace(
    workspace_id: str, user_id: CurrentUserId
) -> WorkspaceResponse:
    """One workspace, by id or by name."""
    try:
        row = await get_workspace(user_id, workspace_id)
    except WorkspaceNotFound as exc:
        raise _http(exc) from exc
    return WorkspaceResponse(**row)


@router.patch("/{workspace_id}", response_model=WorkspaceResponse)
@handle_api_exceptions("update a dispatch workspace", logger)
async def update_dispatch_workspace(
    workspace_id: str,
    request: WorkspaceUpdateRequest,
    user_id: CurrentUserId,
) -> WorkspaceResponse:
    """Change the supplied fields; omitted fields keep their stored value."""
    try:
        row = await update_workspace(
            user_id,
            workspace_id,
            **request.model_dump(exclude_unset=True),
        )
    except (WorkspaceNotFound, WorkspaceInvalid, WorkspaceNameTaken) as exc:
        raise _http(exc) from exc
    return WorkspaceResponse(**row)


@router.delete("/{workspace_id}", response_model=dict)
@handle_api_exceptions("remove a dispatch workspace", logger)
async def delete_dispatch_workspace(workspace_id: str, user_id: CurrentUserId) -> dict:
    """Remove a workspace row; the checkout on disk is left alone."""
    try:
        await delete_workspace(user_id, workspace_id)
    except WorkspaceNotFound as exc:
        raise _http(exc) from exc
    return {"workspace_id": workspace_id, "deleted": True}


@router.get("/{workspace_id}/repo", response_model=WorkspaceRepoResponse)
@handle_api_exceptions("resolve a dispatch workspace repo", logger)
async def dispatch_workspace_repo(
    workspace_id: str, user_id: CurrentUserId
) -> WorkspaceRepoResponse:
    """The resolved repo root and remote a dispatch start would use."""
    try:
        repo = await workspace_repo(workspace_id, user_id=user_id)
    except WorkspaceNotFound as exc:
        raise _http(exc) from exc
    return WorkspaceRepoResponse(**repo)


@router.post("/{workspace_id}/fetch", response_model=WorkspaceFetchResponse)
@handle_api_exceptions("fetch a dispatch workspace", logger)
async def fetch_dispatch_workspace(
    workspace_id: str, user_id: CurrentUserId
) -> WorkspaceFetchResponse:
    """Run ``git fetch`` in the workspace, before a dispatch starts.

    A failed fetch is reported in the body (``fetched: false``) rather than as a
    5xx: the repository is reachable, the remote was not.
    """
    try:
        result = await fetch_before_start(workspace_id, user_id=user_id)
    except WorkspaceNotFound as exc:
        raise _http(exc) from exc
    return WorkspaceFetchResponse(**result)


__all__ = ["router"]
