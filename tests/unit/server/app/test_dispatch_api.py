"""Auto-dispatch HTTP API: ownership, the read surfaces and the three decisions."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.services.dispatch import DispatchService, DispatchSettings
from src.server.services.dispatch_workspaces import WorkspaceNotFound
from src.tools.auto_dispatch import (
    Dispatch,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    Line,
    LineStatus,
)
from src.tools.auto_dispatch.gates import PLANNER_FAILED
from src.tools.auto_dispatch.planning import MAX_GOAL_BYTES
from tests.conftest import create_test_app

USER_ID = "test-user-123"
GET_SERVICE = "src.server.app.dispatch._service"

pytestmark = pytest.mark.asyncio


class Harness:
    """A service over fakes, so no request reaches the database."""

    def __init__(self, tmp_path) -> None:
        self.store = InMemoryDispatchStore()
        self.gate = FileApprovalGate(tmp_path / "proposed")
        self.service = DispatchService(
            DispatchSettings(proposed_dir=tmp_path / "proposed"),
            store=self.store,
            gate=self.gate,
        )

    async def add(
        self, dispatch_id: str, *, state=DispatchState.SUBMITTED, lines: int = 1
    ) -> Dispatch:
        dispatch = Dispatch(
            id=dispatch_id,
            goal=f"goal {dispatch_id}",
            user_id=USER_ID,
            created_at="now",
        )
        dispatch.state = state
        dispatch.lines = [
            Line(id=f"line-{index}", name=f"n{index}", task=f"task {index}")
            for index in range(lines)
        ]
        await self.store.create(dispatch)
        return dispatch


@pytest_asyncio.fixture
async def harness(tmp_path) -> Harness:
    return Harness(tmp_path)


@pytest_asyncio.fixture
async def client(harness: Harness):
    from src.server.app.dispatch import router

    app = create_test_app(router)
    service = AsyncMock(wraps=harness.service)
    with patch(GET_SERVICE, return_value=service):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http


async def test_active_lists_the_unfinished_dispatches(client, harness: Harness) -> None:
    await harness.add("d1")
    await harness.add("d2", state=DispatchState.APPROVAL)
    await harness.add("d3", state=DispatchState.COMPLETE)

    response = await client.get("/api/v1/dispatch/status/active")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()["dispatches"]] == ["d1", "d2"]


async def test_start_creates_a_dispatch_and_returns_201(client, harness: Harness) -> None:
    response = await client.post("/api/v1/dispatch", json={"goal": "ship the thing"})

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "SUBMITTED"
    assert body["goal"] == "ship the thing"
    assert await harness.service.get(body["id"]) is not None


async def test_start_rejects_an_empty_goal(client) -> None:
    response = await client.post("/api/v1/dispatch", json={"goal": ""})
    assert response.status_code == 422


async def test_start_refuses_a_goal_over_the_planner_ceiling(client) -> None:
    """The API holds a goal to the ceiling the planner holds it to."""
    response = await client.post(
        "/api/v1/dispatch", json={"goal": "x" * (MAX_GOAL_BYTES + 1)}
    )
    assert response.status_code == 422


async def test_start_counts_the_goal_in_bytes_not_characters(client) -> None:
    """Three bytes per character: under the character count, over the byte count."""
    response = await client.post(
        "/api/v1/dispatch", json={"goal": "€" * (MAX_GOAL_BYTES // 3 + 1)}
    )
    assert response.status_code == 422


async def test_start_surfaces_a_service_refusal_as_400(client) -> None:
    service = AsyncMock()
    service.start = AsyncMock(side_effect=ValueError("A dispatch goal is required"))
    with patch(GET_SERVICE, return_value=service):
        response = await client.post("/api/v1/dispatch", json={"goal": "x"})
    assert response.status_code == 400
    assert response.json()["detail"] == "A dispatch goal is required"


async def test_start_fetches_a_registered_workspace_before_planning(client) -> None:
    fetch = AsyncMock(return_value={"fetched": True, "stderr": "", "returncode": 0})
    with (
        patch(
            "src.server.app.dispatch.get_dispatch_workspace",
            AsyncMock(return_value={"workspace_id": "w1", "name": "demo"}),
        ),
        patch("src.server.app.dispatch.fetch_before_start", fetch),
    ):
        response = await client.post(
            "/api/v1/dispatch", json={"goal": "ship it", "workspace": "demo"}
        )
    assert response.status_code == 201
    fetch.assert_awaited_once_with("w1", user_id=USER_ID)


async def test_start_snapshots_the_selected_workspace_checkout(client, tmp_path) -> None:
    """The service receives the resolved repo, policy and worker, not just a name."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    dispatch = Dispatch(
        id="d1", goal="ship it", user_id=USER_ID, created_at="now"
    )
    service = AsyncMock()
    service.start = AsyncMock(return_value=dispatch)
    workspace = {
        "workspace_id": "w1",
        "name": "demo",
        "path": str(checkout),
        "sandbox_policy": "workspace-write",
        "default_worker": "codex",
    }
    fetch = AsyncMock(return_value={"fetched": True, "stderr": "", "returncode": 0})
    with (
        patch(GET_SERVICE, return_value=service),
        patch(
            "src.server.app.dispatch.get_dispatch_workspace",
            AsyncMock(return_value=workspace),
        ),
        patch("src.server.app.dispatch.fetch_before_start", fetch),
    ):
        response = await client.post(
            "/api/v1/dispatch", json={"goal": "ship it", "workspace": "demo"}
        )

    assert response.status_code == 201, response.text
    service.start.assert_awaited_once_with(
        "ship it",
        USER_ID,
        workspace={
            "workspace_id": "w1",
            "repo_root": str(checkout.resolve()),
            "sandbox_policy": "workspace-write",
            "default_worker": "codex",
        },
    )


async def test_start_refuses_an_unknown_workspace(client) -> None:
    with patch(
        "src.server.app.dispatch.get_dispatch_workspace",
        AsyncMock(side_effect=WorkspaceNotFound("nope")),
    ):
        response = await client.post(
            "/api/v1/dispatch", json={"goal": "ship it", "workspace": "nope"}
        )
    assert response.status_code == 404


async def test_get_returns_one_dispatch(client, harness: Harness) -> None:
    await harness.add("d1", state=DispatchState.MONITORING, lines=2)

    response = await client.get("/api/v1/dispatch/d1")

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "MONITORING"
    assert [line["id"] for line in body["lines"]] == ["line-0", "line-1"]
    assert 0 < body["progress_pct"] < 100


async def test_a_foreign_dispatch_is_404_not_403(client, harness: Harness) -> None:
    dispatch = await harness.add("d1")
    dispatch.user_id = "someone-else"
    await harness.store.save(dispatch)

    response = await client.get("/api/v1/dispatch/d1")

    assert response.status_code == 404
    # The same sentence for a dispatch that is not the caller's and one that
    # does not exist, and neither reads the requested id back.
    assert response.json()["detail"] == "Dispatch not found"


async def test_an_unknown_dispatch_is_404(client) -> None:
    response = await client.get("/api/v1/dispatch/nope")
    assert response.status_code == 404


async def test_lines_returns_the_planned_lines(client, harness: Harness) -> None:
    await harness.add("d1", lines=3)

    response = await client.get("/api/v1/dispatch/d1/lines")

    assert response.status_code == 200
    assert [line["id"] for line in response.json()] == ["line-0", "line-1", "line-2"]


async def test_approve_opens_the_gate(client, harness: Harness) -> None:
    dispatch = await harness.add("d1", state=DispatchState.APPROVAL, lines=2)
    line = dispatch.line("line-0")
    assert line is not None
    line.assigned_model = "codex"
    line.status = LineStatus.PASSED
    await harness.store.save(dispatch)

    response = await client.post("/api/v1/dispatch/d1/approve", json={"note": "looks right"})

    assert response.status_code == 200
    assert response.json()["state"] == "APPROVAL"
    assert (harness.gate.root / "d1" / "gate.json").is_file()


async def test_reject_records_the_decision(client, harness: Harness) -> None:
    await harness.add("d1", state=DispatchState.APPROVAL)

    response = await client.post("/api/v1/dispatch/d1/reject", json={"note": "too risky"})

    assert response.status_code == 200
    assert response.json()["state"] == "APPROVAL"
    gate = harness.gate.root / "d1" / "gate.json"
    assert "REJECTED" in gate.read_text(encoding="utf-8")


async def test_a_decision_on_a_dispatch_that_is_not_parked_is_409(
    client, harness: Harness
) -> None:
    await harness.add("d1")

    response = await client.post("/api/v1/dispatch/d1/approve", json={})

    assert response.status_code == 409
    assert "not awaiting approval" in response.json()["detail"]


async def test_rejecting_a_dispatch_parked_on_a_planner_failure_ends_it(
    client, harness: Harness
) -> None:
    """Nothing ran, so there is no branch to move aside: the run just ends."""
    dispatch = await harness.add("d1", state=DispatchState.REVIEW, lines=0)
    dispatch.message = f"{PLANNER_FAILED}:invalid_json"
    await harness.store.save(dispatch)

    response = await client.post(
        "/api/v1/dispatch/d1/reject", json={"note": "the goal was wrong"}
    )

    assert response.status_code == 200
    assert response.json()["state"] == "ABORT"
    stored = await harness.store.get("d1")
    assert stored is not None
    assert stored.state is DispatchState.ABORT
    assert stored.message == (
        f"{PLANNER_FAILED}:invalid_json:cancelled: the goal was wrong"
    )


async def test_rejecting_a_dispatch_parked_before_the_reason_codes_ends_it(
    client, harness: Harness
) -> None:
    """A row parked by the older code carries the bare ``planner_failed``.

    It is still a planner failure, so the reject path still ends it: a row
    that predates the reason codes must not be stranded with a 409.
    """
    dispatch = await harness.add("d1", state=DispatchState.REVIEW, lines=0)
    dispatch.message = PLANNER_FAILED
    await harness.store.save(dispatch)

    response = await client.post(
        "/api/v1/dispatch/d1/reject", json={"note": "the plan never arrived"}
    )

    assert response.status_code == 200
    assert response.json()["state"] == "ABORT"
    stored = await harness.store.get("d1")
    assert stored is not None
    assert stored.state is DispatchState.ABORT
    assert stored.message == (
        f"{PLANNER_FAILED}:planner_error:cancelled: the plan never arrived"
    )


async def test_reassign_overrides_a_blocked_lines_model(client, harness: Harness) -> None:
    dispatch = await harness.add("d1", state=DispatchState.REVIEW, lines=2)
    line = dispatch.line("line-1")
    assert line is not None
    line.status = LineStatus.BLOCKED
    line.blockers = ["no tests"]
    await harness.store.save(dispatch)

    response = await client.post(
        "/api/v1/dispatch/d1/lines/line-1/reassign", json={"model": "kimi"}
    )

    assert response.status_code == 200
    stored = await harness.store.get("d1")
    assert stored is not None
    assert stored.line("line-1").assigned_model == "kimi"
    assert stored.line("line-1").status.value == "ASSIGNED"


async def test_reassign_of_an_unknown_line_is_409(client, harness: Harness) -> None:
    await harness.add("d1")

    response = await client.post(
        "/api/v1/dispatch/d1/lines/line-9/reassign", json={"model": "kimi"}
    )

    assert response.status_code == 409
    assert "no such line" in response.json()["detail"]
    assert "line-9" not in response.json()["detail"]


async def test_reassign_needs_a_model(client, harness: Harness) -> None:
    await harness.add("d1")

    response = await client.post(
        "/api/v1/dispatch/d1/lines/line-0/reassign", json={"model": ""}
    )

    assert response.status_code == 422


async def test_a_backend_failure_is_a_500(client) -> None:
    service = AsyncMock()
    service.active = AsyncMock(side_effect=RuntimeError("boom"))
    with patch(GET_SERVICE, return_value=service):
        response = await client.get("/api/v1/dispatch/status/active")
    assert response.status_code == 500
