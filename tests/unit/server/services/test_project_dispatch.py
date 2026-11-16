"""``ProjectDispatcher``'s steer-only dispatch.

A notice that may only reach a running turn (a Super-agent peer's context
message) is posted with ``steer_only`` and the origin that names it as an
agent hand-off, so the thread's admission decides: a thread with no running
turn refuses the notice instead of starting a turn of its own.
"""

from unittest.mock import patch

import pytest

from src.server.services.projects.dispatch import DispatchError, ProjectDispatcher


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, responses: list[_Resp]) -> None:
        self._responses = responses
        self.posts: list[dict] = []

    def post(self, url, *, json=None, headers=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self._responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _dispatch(session: _Session, **kwargs):
    with patch("aiohttp.ClientSession", return_value=session):
        return await ProjectDispatcher().dispatch_turn(
            user_id="u-1",
            thread_id="worker-1",
            workspace_id="w-1",
            prompt="peer context",
            request_key="autonomy:input:in-1",
            **kwargs,
        )


@pytest.mark.asyncio
async def test_a_notice_is_posted_as_a_steer_only_hand_off():
    session = _Session([_Resp(200, {"status": "steered", "run_id": "run-live"})])

    receipt = await _dispatch(
        session,
        platform="superagent_worker",
        steer_only=True,
        origin={"type": "agent", "agent": "superagent_peer"},
    )

    body = session.posts[0]["json"]
    assert body["steer_only"] is True
    assert body["origin"] == {"type": "agent", "agent": "superagent_peer"}
    assert receipt.status == "steered"
    assert receipt.run_id == "run-live"


@pytest.mark.asyncio
async def test_an_ordinary_turn_asks_for_no_steer():
    session = _Session([_Resp(200, {"status": "dispatched", "run_id": "run-1"})])

    receipt = await _dispatch(session, platform="superagent_worker")

    body = session.posts[0]["json"]
    assert "steer_only" not in body
    assert "origin" not in body
    assert receipt.status == "dispatched"
    assert receipt.run_id == "run-1"


@pytest.mark.asyncio
async def test_a_refusal_because_no_turn_runs_is_a_receipt_not_an_error():
    """The thread had no running turn to steer and the request may not start
    one: the caller is told so, so it can leave the message stored."""
    session = _Session(
        [_Resp(409, {"detail": {"code": "not_running", "message": "No turn is running."}})]
    )

    receipt = await _dispatch(
        session,
        platform="superagent_worker",
        steer_only=True,
        origin={"type": "agent", "agent": "superagent_peer"},
    )

    assert receipt.status == "not_running"
    assert receipt.run_id is None


@pytest.mark.asyncio
async def test_a_conflict_that_is_not_a_refusal_is_still_an_error():
    session = _Session([_Resp(409, {"detail": {"code": "running"}})])

    with pytest.raises(DispatchError):
        await _dispatch(
            session,
            platform="superagent_worker",
            steer_only=True,
            origin={"type": "agent", "agent": "superagent_peer"},
        )


@pytest.mark.asyncio
async def test_a_retransmit_adopts_the_run_the_earlier_copy_started():
    session = _Session([_Resp(409, {"detail": {"run_id": "run-1"}})])

    receipt = await _dispatch(session, platform="superagent_worker")

    assert receipt.run_id == "run-1"
    assert receipt.status == "dispatched"
