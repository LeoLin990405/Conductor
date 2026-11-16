"""Worker cancel reports failure instead of swallowing it."""

from __future__ import annotations

from unittest.mock import patch

import aiohttp
import pytest

from src.server.services.projects.dispatch import ProjectDispatcher


class _Resp:
    def __init__(self, status: int, body: dict | None) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    async def read(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, response: _Resp | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.posts: list[str] = []

    def post(self, url, headers=None):
        self.posts.append(url)
        if self.error is not None:
            raise self.error
        return self.response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _cancel(session: _Session) -> bool:
    with patch(
        "src.server.services.projects.dispatch.aiohttp.ClientSession",
        return_value=session,
    ):
        return await ProjectDispatcher().cancel_thread(
            user_id="u-1", thread_id="thread-1", run_id="run-1"
        )


@pytest.mark.asyncio
async def test_cancel_succeeds_when_the_run_is_stopped_or_already_gone():
    session = _Session(_Resp(200, {"cancelled": True, "state": "requested"}))
    assert await _cancel(session) is True
    assert session.posts[0].endswith("/api/v1/threads/thread-1/cancel?run_id=run-1")

    finished = _Session(_Resp(200, {"cancelled": False, "state": "already_finished"}))
    assert await _cancel(finished) is True


@pytest.mark.asyncio
async def test_cancel_fails_when_the_request_fails_or_a_run_stays_active():
    refused = _Session(_Resp(500, {"detail": "Failed to cancel the turn."}))
    assert await _cancel(refused) is False

    active = _Session(_Resp(200, {"cancelled": False, "state": "another_run_active"}))
    assert await _cancel(active) is False

    down = _Session(error=aiohttp.ClientError("connection refused"))
    assert await _cancel(down) is False
