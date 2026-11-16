"""Dispatch workspace API: CRUD, the repo lookup and the fetch route."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.services.dispatch_workspaces import (
    WorkspaceInvalid,
    WorkspaceNameTaken,
    WorkspaceNotFound,
)
from tests.conftest import create_test_app

USER_ID = "test-user-123"
MODULE = "src.server.app.dispatch_workspaces"

pytestmark = pytest.mark.asyncio

ROW = {
    "workspace_id": "11111111-1111-1111-1111-111111111111",
    "user_id": USER_ID,
    "name": "conductor",
    "path": "/src/conductor",
    "git_remote": "git@example.com:conductor.git",
    "sandbox_policy": "workspace-write",
    "default_worker": "codex",
    "created_at": "2026-11-13T00:00:00+00:00",
    "updated_at": "2026-11-13T00:00:00+00:00",
}


@pytest_asyncio.fixture
async def client():
    from src.server.app.dispatch_workspaces import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


async def test_list_returns_the_users_workspaces(client) -> None:
    with patch(f"{MODULE}.list_workspaces", AsyncMock(return_value=[ROW])):
        response = await client.get("/api/v1/dispatch-workspaces")
    assert response.status_code == 200
    assert response.json()["workspaces"][0]["name"] == "conductor"


async def test_create_registers_a_checkout(client) -> None:
    create = AsyncMock(return_value=ROW)
    with patch(f"{MODULE}.create_workspace", create):
        response = await client.post(
            "/api/v1/dispatch-workspaces",
            json={
                "name": "conductor",
                "path": "/src/conductor",
                "default_worker": "codex",
            },
        )
    assert response.status_code == 201
    assert response.json()["git_remote"] == "git@example.com:conductor.git"
    create.assert_awaited_once_with(
        user_id=USER_ID,
        name="conductor",
        path="/src/conductor",
        git_remote=None,
        sandbox_policy="",
        default_worker="codex",
    )


async def test_create_maps_invalid_and_taken_names(client) -> None:
    with patch(
        f"{MODULE}.create_workspace",
        AsyncMock(side_effect=WorkspaceInvalid("not a directory")),
    ):
        invalid = await client.post(
            "/api/v1/dispatch-workspaces", json={"name": "x", "path": "/nope"}
        )
    assert invalid.status_code == 400
    with patch(
        f"{MODULE}.create_workspace", AsyncMock(side_effect=WorkspaceNameTaken("x"))
    ):
        taken = await client.post(
            "/api/v1/dispatch-workspaces", json={"name": "x", "path": "/tmp"}
        )
    assert taken.status_code == 409


async def test_show_is_a_404_for_an_unknown_id(client) -> None:
    with patch(
        f"{MODULE}.get_workspace", AsyncMock(side_effect=WorkspaceNotFound("nope"))
    ):
        response = await client.get("/api/v1/dispatch-workspaces/nope")
    assert response.status_code == 404


async def test_update_passes_only_the_supplied_fields(client) -> None:
    update = AsyncMock(return_value={**ROW, "default_worker": "claude"})
    with patch(f"{MODULE}.update_workspace", update):
        response = await client.patch(
            f"/api/v1/dispatch-workspaces/{ROW['workspace_id']}",
            json={"default_worker": "claude"},
        )
    assert response.status_code == 200
    update.assert_awaited_once_with(
        USER_ID, ROW["workspace_id"], default_worker="claude"
    )


async def test_remove_reports_the_deletion(client) -> None:
    with patch(f"{MODULE}.delete_workspace", AsyncMock(return_value=None)):
        response = await client.delete(
            f"/api/v1/dispatch-workspaces/{ROW['workspace_id']}"
        )
    assert response.status_code == 200
    assert response.json()["deleted"] is True


async def test_repo_lookup_returns_the_resolved_root_and_remote(client) -> None:
    repo = {
        "workspace_id": ROW["workspace_id"],
        "name": "conductor",
        "path": "/src/conductor",
        "repo_root": "/src/conductor",
        "git_remote": "git@example.com:conductor.git",
        "sandbox_policy": "workspace-write",
        "default_worker": "codex",
    }
    with patch(f"{MODULE}.workspace_repo", AsyncMock(return_value=repo)):
        response = await client.get(
            f"/api/v1/dispatch-workspaces/{ROW['workspace_id']}/repo"
        )
    assert response.status_code == 200
    assert response.json()["repo_root"] == "/src/conductor"


async def test_fetch_returns_the_git_result(client) -> None:
    fetched = {
        "workspace_id": ROW["workspace_id"],
        "repo_root": "/src/conductor",
        "remote": "origin",
        "fetched": True,
        "returncode": 0,
        "stdout": "",
        "stderr": "",
    }
    with patch(f"{MODULE}.fetch_before_start", AsyncMock(return_value=fetched)):
        response = await client.post(
            f"/api/v1/dispatch-workspaces/{ROW['workspace_id']}/fetch"
        )
    assert response.status_code == 200
    assert response.json()["fetched"] is True
