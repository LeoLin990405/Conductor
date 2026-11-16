"""``conductor memory`` must call routes the server really exposes.

The CLI once called ``GET /api/v1/memory/search`` and ``POST
/api/v1/memory/proposals`` while the server had neither, so ``recall`` and
``propose`` always failed with "endpoint is absent". These tests pin the CLI's
paths to the real FastAPI app's route table and exercise the two thin routes
that back them.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from starlette.routing import Match

from src.conductor_cli import memory_cli
from tests.conftest import create_test_app

OWNER = "test-user-123"


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def get(self, path, *, params=None):
        self.calls.append(("GET", path))
        return {"results": []}

    def post(self, path, body):
        self.calls.append(("POST", path))
        return {"proposal_id": "x"}


def _cli_calls() -> list[tuple[str, str]]:
    client = _RecordingClient()
    memory_cli.handle_recall(SimpleNamespace(query="q", limit=3), client)
    memory_cli.handle_propose(SimpleNamespace(entry="likes tea"), client)
    proposal = "11111111-1111-4111-8111-111111111111"
    memory_cli.handle_approve(SimpleNamespace(proposal_id=proposal), client)
    memory_cli.handle_reject(SimpleNamespace(proposal_id=proposal), client)
    return client.calls


def _route_matches(app, method: str, path: str) -> bool:
    scope = {"type": "http", "method": method, "path": path, "root_path": ""}
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match is Match.FULL:
            return True
    return False


def test_every_memory_cli_path_is_a_route_of_the_real_app():
    from src.server.app import setup

    calls = _cli_calls()
    assert len(calls) == 4
    missing = [call for call in calls if not _route_matches(setup.app, *call)]
    assert missing == []


@pytest.fixture
def projection_on(monkeypatch):
    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")


@pytest_asyncio.fixture
async def client():
    from src.server.app.memory import router

    async with AsyncClient(
        transport=ASGITransport(app=create_test_app(router)), base_url="http://test"
    ) as http:
        yield http


@pytest.mark.asyncio
async def test_search_returns_ranked_rows_for_the_owner(client, projection_on, monkeypatch):
    from src.server.services.refactor import memory as memory_svc

    search = AsyncMock(return_value=[{"entry_key": "prefs/tea", "index_text": "likes tea"}])
    monkeypatch.setattr(memory_svc, "search_memory", search)

    response = await client.get("/api/v1/memory/search", params={"q": "tea", "limit": 3})

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "query": "tea",
        "results": [{"entry_key": "prefs/tea", "index_text": "likes tea"}],
    }
    search.assert_awaited_once_with(user_id=OWNER, query="tea", limit=3)


@pytest.mark.asyncio
async def test_search_is_empty_not_an_error_when_the_index_is_off(client, monkeypatch):
    monkeypatch.delenv("REFACTOR_MEMORY_INDEX_ENABLED", raising=False)

    response = await client.get("/api/v1/memory/search", params={"q": "tea"})

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "query": "tea", "results": []}


@pytest.mark.asyncio
async def test_propose_queues_a_user_proposal_and_returns_its_id(
    client, projection_on, monkeypatch
):
    from src.server.database.refactor import proposals as db_proposals

    seen: dict = {}

    async def insert(*, user_id, thread_id, task_run_id, proposals):
        seen.update(user_id=user_id, task_run_id=task_run_id, proposals=proposals)
        return 1

    async def listing(*, user_id, status, limit):
        return [
            {"proposal_id": "other", "task_run_id": "not-this-run"},
            {"proposal_id": "p-1", "task_run_id": seen["task_run_id"]},
        ]

    monkeypatch.setattr(db_proposals, "insert_proposals", insert)
    monkeypatch.setattr(db_proposals, "list_proposals", listing)

    response = await client.post("/api/v1/memory/proposals", json={"text": " likes tea "})

    assert response.status_code == 201
    assert response.json() == {
        "proposal_id": "p-1",
        "type": "user",
        "text": "likes tea",
        "status": "proposed",
    }
    assert seen["user_id"] == OWNER
    [row] = seen["proposals"]
    assert row["type"] == "user" and row["origin"] == "user" and row["text"] == "likes tea"


@pytest.mark.asyncio
async def test_propose_is_unavailable_when_the_index_is_off(client, monkeypatch):
    monkeypatch.delenv("REFACTOR_MEMORY_INDEX_ENABLED", raising=False)

    response = await client.post("/api/v1/memory/proposals", json={"text": "likes tea"})

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_propose_rejects_blank_and_oversized_text(client, projection_on):
    blank = await client.post("/api/v1/memory/proposals", json={"text": "   "})
    huge = await client.post("/api/v1/memory/proposals", json={"text": "x" * 1001})

    assert blank.status_code == 422
    assert huge.status_code == 422
