"""The per-thread subagent switch: create, PATCH, reads, and its DB read.

The switch lives on the thread row, NULL while the thread follows the owner's
default. A create may name it; after that PATCH is its only writer, null
included, and publishes a feed hint; GET and list report the stored value; the
agent reads the effective value with ``read_thread_subagents_allowed``.
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.server.utils.api import get_current_user_id
from tests.conftest import create_test_app
from tests.unit.server.app.test_threads_thread_model import (
    DISPATCH,
    USER as SENDER,
    _send_stubs,
)

THREADS_MOD = "src.server.app.threads.crud"
AUTH_MOD = "src.server.utils.api"
USER = "test-user-123"


def _app(user=USER):
    from src.server.app.threads import router

    app = create_test_app(router)
    app.dependency_overrides[get_current_user_id] = lambda: user
    return app


def _row(**overrides):
    now = datetime.now(timezone.utc)
    row = {
        "conversation_thread_id": "t-1",
        "workspace_id": "ws-1",
        "current_status": "completed",
        "msg_type": "ptc",
        "thread_index": 0,
        "title": "Test Thread",
        "subagents_allowed": True,
        "created_at": now,
        "updated_at": now,
    }
    row.update(overrides)
    return row


async def _request(method, path, body=None, app=None):
    async with AsyncClient(
        transport=ASGITransport(app=app or _app()), base_url="http://test"
    ) as c:
        return await c.request(method, path, json=body)


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [True, False, None])
async def test_get_thread_reports_the_stored_switch(stored):
    with (
        patch(f"{AUTH_MOD}.require_thread_owner", new=AsyncMock()),
        patch(
            f"{THREADS_MOD}.get_thread_by_id",
            new=AsyncMock(return_value=_row(subagents_allowed=stored)),
        ),
    ):
        resp = await _request("GET", "/api/v1/threads/t-1")

    assert resp.status_code == 200
    assert resp.json()["subagents_allowed"] is stored




# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------

CREATE_MOD = "src.server.app.threads.create"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra, named", [({"subagents_allowed": False}, False), ({}, None)]
)
async def test_create_hands_the_switch_to_the_insert_and_reports_the_row(extra, named):
    from src.server.services.turn_runtime import TurnRoute

    with (
        patch(f"{CREATE_MOD}.HOST_MODE", "oss"),
        patch(
            f"{CREATE_MOD}.requested_workspace",
            new=AsyncMock(return_value={"workspace_id": "ws-1", "status": "active"}),
        ),
        patch(
            f"{CREATE_MOD}.turn_route",
            return_value=TurnRoute("ws-1", "generalist"),
        ),
        patch(
            f"{CREATE_MOD}.create_thread",
            new=AsyncMock(return_value=_row(subagents_allowed=named)),
        ) as create,
        patch(f"{CREATE_MOD}.schedule_title_generation", return_value=None),
    ):
        resp = await _request(
            "POST",
            "/api/v1/threads",
            {"workspace_id": "ws-1", "first_query": "hello", **extra},
        )

    assert resp.status_code == 201
    assert create.await_args.kwargs["subagents_allowed"] is named
    assert resp.json()["subagents_allowed"] is named
    # The insert owns the producer; the response reports what it stored.
    assert "msg_type" not in create.await_args.kwargs
    assert resp.json()["msg_type"] == "ptc"


# ---------------------------------------------------------------------------
# Send path
# ---------------------------------------------------------------------------


async def _post(path, extra):
    from src.server.app.threads import router
    from src.server.dependencies.usage_limits import ChatAuthResult, enforce_chat_limit

    app = create_test_app(router)
    app.dependency_overrides[enforce_chat_limit] = lambda: ChatAuthResult(
        user_id=SENDER, access_tier=0
    )
    body = {
        "messages": [{"role": "user", "content": "test query"}],
        "workspace_id": "ws-placeholder",
        **extra,
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        # The Router's dispatch: the only send the door still admits (RA-3).
        resp = await c.post(path, json=body, headers=DISPATCH)
    for _ in range(3):
        await asyncio.sleep(0)
    return resp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path, owner",
    [
        ("/api/v1/threads/tid-mine/messages", SENDER),
    ],
)
@pytest.mark.parametrize(
    "extra, expected", [({"subagents_allowed": False}, False), ({}, None)]
)
async def test_a_send_hands_the_switch_to_the_run(path, owner, extra, expected):
    with _send_stubs(owner_id=owner) as m:
        resp = await _post(path, extra)

    assert resp.status_code == 200
    assert m["astream"].call_args.kwargs["request"].subagents_allowed is expected


# ---------------------------------------------------------------------------
# read_thread_subagents_allowed
# ---------------------------------------------------------------------------


def _pool_returning(row=None, error=None):
    cur = MagicMock()
    cur.execute = AsyncMock(side_effect=error)
    cur.fetchone = AsyncMock(return_value=row)

    @asynccontextmanager
    async def _cursor(*_a, **_k):
        yield cur

    conn = MagicMock()
    conn.cursor = _cursor

    @asynccontextmanager
    async def _conn():
        yield conn

    return MagicMock(side_effect=_conn)


@pytest.mark.asyncio
async def test_read_of_a_malformed_id_is_none_without_a_query():
    from src.server.database.conversation import threads_read

    getter = _pool_returning(row={"allowed": False})
    with patch.object(threads_read.pool, "get_db_connection", new=getter):
        result = await threads_read.read_thread_subagents_allowed("not-a-uuid")

    assert result is None
    getter.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row, expected",
    [({"allowed": False}, False), ({"allowed": True}, True), (None, None)],
)
async def test_read_reports_the_effective_switch_or_none_for_no_row(row, expected):
    from src.server.database.conversation import threads_read

    with patch.object(
        threads_read.pool,
        "get_db_connection",
        new=_pool_returning(row=row),
    ):
        result = await threads_read.read_thread_subagents_allowed(
            "123e4567-e89b-12d3-a456-426614174000"
        )

    assert result is expected


@pytest.mark.asyncio
async def test_a_failed_read_raises_for_the_caller_to_decide():
    from src.server.database.conversation import threads_read

    with (
        patch.object(
            threads_read.pool,
            "get_db_connection",
            new=_pool_returning(error=RuntimeError("boom")),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await threads_read.read_thread_subagents_allowed(
            "123e4567-e89b-12d3-a456-426614174000"
        )
