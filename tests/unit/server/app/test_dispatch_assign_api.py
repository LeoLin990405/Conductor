"""N73 L5: GET /api/v1/dispatch/{id}/assignments and the summary field."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.server.app import dispatch as api
from src.server.utils.api import get_current_user_id
from src.tools.auto_dispatch.superagent_assign.record import append_assignment
from src.tools.auto_dispatch.types import Dispatch, Line
from tests.conftest import create_test_app

pytestmark = pytest.mark.asyncio


async def test_assignments_route_lists_entries(monkeypatch) -> None:
    gates = append_assignment(
        {"lines": {"l1": {}}}, line_id="l1", run_id="l1:assign:0", mode="shadow",
        source="superagent",
        choice={"line_id": "l1", "model": "stepfun", "effort": "high", "runtime": "default",
                "reason": "docs", "confidence": "high", "model_id": ""},
        selector_choice={"model": "kimi"}, feasibility=[{"status": "ok"}],
    )
    dispatch = Dispatch(
        id="d1", goal="g", user_id="leo", created_at="now",
        lines=[Line(id="l1", name="l1", task="t")], gates=gates,
    )

    async def owned(user_id, dispatch_id):
        return dispatch

    monkeypatch.setattr(api, "_owned", owned)
    app = create_test_app(api.router)
    app.dependency_overrides[get_current_user_id] = lambda: "leo"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/dispatch/d1/assignments")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dispatch_id"] == "d1"
    assert body["lines"]["l1"][0]["choice"]["model"] == "stepfun"
    assert "assignment" in api.DispatchSummary.model_fields
