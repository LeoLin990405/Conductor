"""Operator routes that unwedge a dispatch: reject --force, cancel, unblock, adjudicate.

The service is a fake: each route must load the dispatch through the ownership
check, hand the service the right arguments, and turn a refusal into a 409.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.tools.auto_dispatch import Dispatch, DispatchState, Line
from src.tools.auto_dispatch.gates import PLANNER_FAILED
from tests.conftest import create_test_app

USER_ID = "test-user-123"
GET_SERVICE = "src.server.app.dispatch._service"
BASE = "/api/v1/dispatch"

pytestmark = pytest.mark.asyncio


def _dispatch(dispatch_id: str = "d1", *, user_id: str = USER_ID, message=None) -> Dispatch:
    dispatch = Dispatch(
        id=dispatch_id, goal=f"goal {dispatch_id}", user_id=user_id, created_at="now"
    )
    dispatch.state = DispatchState.MONITORING
    dispatch.lines = [Line(id="line-0", name="n0", task="task 0")]
    if message is not None:
        # A planner failure parks in REVIEW with no line.
        dispatch.state = DispatchState.REVIEW
        dispatch.lines = []
        dispatch.message = message
    return dispatch


class FakeService:
    """Only the methods the operator routes call; each returns the loaded row."""

    def __init__(self) -> None:
        self.row: Dispatch | None = _dispatch()
        self.get = AsyncMock(side_effect=lambda _id: self.row)
        self.reject = AsyncMock(side_effect=self._done)
        self.cancel = AsyncMock(side_effect=self._done)
        self.unblock = AsyncMock(side_effect=self._done)
        self.adjudicate = AsyncMock(side_effect=self._done)
        self.cancel_planner_failed = AsyncMock(side_effect=self._done)

    async def _done(self, *_args, **_kwargs) -> Dispatch:
        assert self.row is not None
        return self.row


@pytest_asyncio.fixture
async def service() -> FakeService:
    return FakeService()


@pytest_asyncio.fixture
async def client(service: FakeService):
    from src.server.app.dispatch import router

    app = create_test_app(router)
    with patch(GET_SERVICE, return_value=service):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http


# reject ---------------------------------------------------------------------


async def test_reject_without_force_is_the_old_call(client, service) -> None:
    response = await client.post(f"{BASE}/d1/reject", json={"note": "no"})
    assert response.status_code == 200
    assert response.json()["id"] == "d1"
    service.reject.assert_awaited_once_with("d1", "no")


async def test_reject_passes_force(client, service) -> None:
    response = await client.post(f"{BASE}/d1/reject", json={"note": "stop", "force": True})
    assert response.status_code == 200
    service.reject.assert_awaited_once_with("d1", "stop", force=True)


async def test_reject_refusal_is_409_with_the_reason(client, service) -> None:
    service.reject.side_effect = ValueError("line_running: line-0 is still running")
    response = await client.post(f"{BASE}/d1/reject", json={})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("line_running")


async def test_reject_of_a_planner_failure_still_cancels_it(client, service) -> None:
    service.row = _dispatch(message=f"{PLANNER_FAILED}:invalid_json")
    response = await client.post(f"{BASE}/d1/reject", json={"note": "n", "force": True})
    assert response.status_code == 200
    service.cancel_planner_failed.assert_awaited_once_with("d1", "n")
    service.reject.assert_not_awaited()


# cancel ---------------------------------------------------------------------


async def test_cancel_calls_the_service(client, service) -> None:
    response = await client.post(f"{BASE}/d1/cancel", json={"note": "wedged"})
    assert response.status_code == 200
    service.cancel.assert_awaited_once_with("d1", "wedged", force=False)


async def test_cancel_with_an_empty_body_uses_the_defaults(client, service) -> None:
    response = await client.post(f"{BASE}/d1/cancel", json={})
    assert response.status_code == 200
    service.cancel.assert_awaited_once_with("d1", "", force=False)


async def test_cancel_passes_force(client, service) -> None:
    response = await client.post(f"{BASE}/d1/cancel", json={"force": True})
    assert response.status_code == 200
    service.cancel.assert_awaited_once_with("d1", "", force=True)


async def test_cancel_refusal_is_409(client, service) -> None:
    service.cancel.side_effect = ValueError("dispatch is at approval; use reject")
    response = await client.post(f"{BASE}/d1/cancel", json={})
    assert response.status_code == 409
    assert "reject" in response.json()["detail"]


async def test_cancel_of_a_planner_failure_goes_to_cancel_planner_failed(
    client, service
) -> None:
    service.row = _dispatch(message=PLANNER_FAILED)
    response = await client.post(f"{BASE}/d1/cancel", json={"note": "bad goal"})
    assert response.status_code == 200
    service.cancel_planner_failed.assert_awaited_once_with("d1", "bad goal")
    service.cancel.assert_not_awaited()


# unblock --------------------------------------------------------------------


async def test_unblock_calls_the_service(client, service) -> None:
    response = await client.post(f"{BASE}/d1/lines/line-0/unblock", json={"note": "old row"})
    assert response.status_code == 200
    service.unblock.assert_awaited_once_with("d1", "line-0", "old row")


async def test_unblock_refusal_is_409(client, service) -> None:
    service.unblock.side_effect = ValueError("line-0 is not blocked")
    response = await client.post(f"{BASE}/d1/lines/line-0/unblock", json={})
    assert response.status_code == 409


# adjudicate -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "sent"),
    [
        ("in-scope", "in-scope"),
        ("out-of-scope", "out-of-scope"),
        ("in_scope", "in-scope"),
        ("out_of_scope", "out-of-scope"),
    ],
)
async def test_adjudicate_normalises_the_ruling(client, service, given, sent) -> None:
    response = await client.post(
        f"{BASE}/d1/lines/line-0/adjudicate", json={"ruling": given, "note": "ok"}
    )
    assert response.status_code == 200
    service.adjudicate.assert_awaited_once_with("d1", "line-0", sent, "ok")


@pytest.mark.parametrize("body", [{"ruling": "maybe"}, {"ruling": ""}, {"note": "x"}])
async def test_adjudicate_needs_a_known_ruling(client, service, body) -> None:
    response = await client.post(f"{BASE}/d1/lines/line-0/adjudicate", json=body)
    assert response.status_code == 422
    service.adjudicate.assert_not_awaited()


async def test_adjudicate_refusal_is_409(client, service) -> None:
    service.adjudicate.side_effect = ValueError("line-0 has no scope conflict")
    response = await client.post(
        f"{BASE}/d1/lines/line-0/adjudicate", json={"ruling": "in-scope"}
    )
    assert response.status_code == 409


# ownership ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "body", "method"),
    [
        ("d1/reject", {"force": True}, "reject"),
        ("d1/cancel", {}, "cancel"),
        ("d1/lines/line-0/unblock", {}, "unblock"),
        ("d1/lines/line-0/adjudicate", {"ruling": "in-scope"}, "adjudicate"),
    ],
)
@pytest.mark.parametrize("owner", ["someone-else", None])
async def test_a_dispatch_the_caller_does_not_own_is_404(
    client, service, path, body, method, owner
) -> None:
    service.row = _dispatch(user_id=owner) if owner else None
    response = await client.post(f"{BASE}/{path}", json=body)
    assert response.status_code == 404
    getattr(service, method).assert_not_awaited()
