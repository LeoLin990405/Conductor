"""N76 L3: ``POST /api/v1/dispatch/{id}/lines/{line}/recover``.

The route is the unblock route's twin: the same ownership check (404, never
403), a refusal is 409, a bad mode is 422. The service half runs the real
service and machine with fakes: a parked row is recovered and re-driven, and a
request made while a drive runs is queued and performed by that drive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.tools.auto_dispatch import LineStatus
from src.tools.auto_dispatch.liveness import modes, record
from src.tools.auto_dispatch.operator import RECOVER_MARKER
from tests.conftest import create_test_app
from tests.unit.server.app.test_dispatch_operator_routes import (
    BASE,
    GET_SERVICE,
    USER_ID,
    _dispatch,
)
from tests.unit.server.services.test_dispatch_unwedge import (
    ScriptedLauncher,
    _assigned,
    _drive,
    _setup,
)

pytestmark = pytest.mark.asyncio


class FakeService:
    def __init__(self) -> None:
        self.row = _dispatch()
        self.get = AsyncMock(side_effect=lambda _id: self.row)
        self.recover = AsyncMock(side_effect=self._done)
        self.unblock = AsyncMock(side_effect=self._done)

    async def _done(self, *_args, **_kwargs):
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


# -- route ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "mode", "note"),
    [
        ({}, "next", ""),
        ({"mode": "next", "note": "go"}, "next", "go"),
        ({"mode": "SPLIT"}, "split", ""),
        ({"mode": "human", "note": "mine"}, "human", "mine"),
    ],
)
async def test_recover_calls_the_service(client, service, body, mode, note) -> None:
    response = await client.post(f"{BASE}/d1/lines/line-0/recover", json=body)
    assert response.status_code == 200
    assert response.json()["id"] == "d1"
    assert "liveness" in response.json()
    service.recover.assert_awaited_once_with("d1", "line-0", mode, note)


async def test_a_bad_mode_is_422(client, service) -> None:
    response = await client.post(f"{BASE}/d1/lines/line-0/recover", json={"mode": "later"})
    assert response.status_code == 422
    service.recover.assert_not_awaited()


async def test_a_refusal_is_409_with_its_code(client, service) -> None:
    service.recover.side_effect = ValueError("needs_human")
    response = await client.post(f"{BASE}/d1/lines/line-0/recover", json={})
    assert response.status_code == 409
    assert "needs_human" in response.text


@pytest.mark.parametrize("owner", ["someone-else", None])
async def test_a_dispatch_the_caller_does_not_own_is_404(client, service, owner) -> None:
    service.row = _dispatch(user_id=owner) if owner else None
    response = await client.post(f"{BASE}/d1/lines/line-0/recover", json={})
    assert response.status_code == 404
    service.recover.assert_not_awaited()


async def test_the_owner_check_is_the_unblock_route_check() -> None:
    """Both routes call the same ``_owned`` before the service is touched."""
    import inspect

    from src.server.app import dispatch as routes

    recover_src = inspect.getsource(routes.recover_line)
    unblock_src = inspect.getsource(routes.unblock_line)
    assert "await _owned(user_id, dispatch_id)" in recover_src
    assert "await _owned(user_id, dispatch_id)" in unblock_src
    assert USER_ID


# -- service -------------------------------------------------------------------


async def _exited(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(modes.MODE_ENV, modes.SHADOW)
    launcher = ScriptedLauncher({"a": ["no_commit"]})
    store, launcher, gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "deepseek"), _assigned("b", "kimi")],
        launcher=launcher,
    )
    await _drive(service, row.id)
    parked = await store.get(row.id)
    assert parked.line("a").status is LineStatus.BLOCKED
    assert parked.line("a").failure_reason == "no_new_commit"
    return store, launcher, service, row


async def test_service_recover_on_a_parked_row_runs_the_step_on_the_next_drive(
    tmp_path: Path, monkeypatch
) -> None:
    store, launcher, service, row = await _exited(tmp_path, monkeypatch)

    requested = await service.recover(row.id, "a", "next", "operator says go")
    assert isinstance(requested.gates["lines"]["a"][RECOVER_MARKER], dict)
    assert not requested.gates.get("human_reassigns")

    await _drive(service, row.id)
    after = await store.get(row.id)
    assert RECOVER_MARKER not in after.gates["lines"]["a"]
    assert after.line("a").status is LineStatus.PASSED
    assert after.line("a").assigned_model != "deepseek"
    assert launcher.calls[-1][0] == "a"
    done = record.load(after.gates["lines"]["a"])
    assert done is not None
    assert any(step.detail.startswith("operator recover") for step in done.history)
    kinds = [item.get("kind") for item in after.gates["scheduler"]["audit"]]
    assert "human_recover" in kinds


async def test_service_recover_while_a_drive_runs_is_queued_then_performed(
    tmp_path: Path, monkeypatch
) -> None:
    store, launcher, service, row = await _exited(tmp_path, monkeypatch)
    service.active_drives[row.id] = object()
    try:
        queued = await service.recover(row.id, "a")
    finally:
        service.active_drives.pop(row.id, None)
    assert queued.gates["human_reassigns"]["a"]["kind"] == "recover"
    assert RECOVER_MARKER not in queued.gates["lines"]["a"]

    await _drive(service, row.id)
    after = await store.get(row.id)
    assert after.gates["human_reassigns"] == {}
    assert after.line("a").status is LineStatus.PASSED


async def test_service_recover_refusals_keep_their_codes(tmp_path: Path, monkeypatch) -> None:
    store, launcher, service, row = await _exited(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="^line_not_parked$"):
        await service.recover(row.id, "b")
    with pytest.raises(ValueError, match="^invalid_mode"):
        await service.recover(row.id, "a", "sideways")
    with pytest.raises(ValueError, match="no such line"):
        await service.recover(row.id, "zzz")
    human = await service.recover(row.id, "a", "human")
    assert record.load(human.gates["lines"]["a"]).needs_human
    with pytest.raises(ValueError, match="^already_needs_human$"):
        await service.recover(row.id, "a", "human")
    with pytest.raises(ValueError, match="^needs_human$"):
        await service.recover(row.id, "a")
    # The existing verbs answer exactly as before.
    with pytest.raises(ValueError, match="^line_not_parked$"):
        await service.unblock(row.id, "b")


async def test_status_summary_carries_the_liveness_view(tmp_path: Path, monkeypatch) -> None:
    from src.server.services.dispatch import dispatch_summary

    store, launcher, service, row = await _exited(tmp_path, monkeypatch)
    await service.recover(row.id, "a", "human")
    summary = dispatch_summary(await store.get(row.id))
    assert summary["liveness"]["mode"] == modes.SHADOW
    assert summary["liveness"]["needs_human"] == ["a"]
    entry = summary["liveness"]["lines"]["a"]
    assert entry["owner"] == "needs_human"
    assert {"next_step", "next_at", "budget_left"} <= set(entry)
    await asyncio.sleep(0)
