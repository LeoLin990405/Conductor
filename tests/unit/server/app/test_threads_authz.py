"""IDOR guards in ``_handle_send_message`` (shared by both POST message routes).

Two dimensions: the target thread must belong to the caller (or be brand-new),
and the resolved workspace must belong to the caller — so a send can't reach
another user's thread or run inside their sandbox.
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.config.runtime.agent import CredentialSource
from tests.conftest import create_test_app

THREADS_MOD = "src.server.app.threads.messaging"

CALLER = "usr-caller-001"
OWNER = "usr-owner-002"
TOKEN = "test-internal-service-token"
# RA-3: the door admits only the server's own dispatch on a live Super-agent
# thread, so an admitted send here is one (``_stub_downstream`` sets TOKEN).
DISPATCH = {"X-Dispatch": "background", "X-Service-Token": TOKEN}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config():
    """Stub ``AgentConfig`` returned by ``resolve_llm_config`` (platform key)."""
    cfg = MagicMock()
    cfg.credential_source = CredentialSource.PLATFORM
    cfg.llm_client = None
    cfg.llm = MagicMock()
    cfg.llm.name = "claude-sonnet-placeholder"
    return cfg


def _empty_async_gen():
    async def _gen():
        if False:
            yield ""

    return _gen()


def _dispatched_gen(*_args, **_kwargs):
    """A dispatched generator yields the post-START marker the handler primes to."""

    async def _gen():
        from src.server.handlers.chat.request_prep import DISPATCH_STARTED_MARKER

        yield DISPATCH_STARTED_MARKER

    return _gen()


def _app_for(user_id: str):
    """Threads router with the chat auth dependency pinned to ``user_id``.

    ``access_tier=0`` keeps the no-provider 403 guard from firing first, so the
    IDOR guard is the only thing under test.
    """
    from src.server.app.threads import router
    from src.server.dependencies.usage_limits import ChatAuthResult, enforce_chat_limit

    app = create_test_app(router)
    app.dependency_overrides[enforce_chat_limit] = lambda: ChatAuthResult(
        user_id=user_id, access_tier=0
    )
    return app


@contextmanager
def _stub_downstream(owner_id, ws_owner=CALLER):
    """Patch the IDOR guards' lookups + everything past them.

    With ``owner_id`` matching the caller (or ``None``) AND ``ws_owner`` matching
    the auth user, a background dispatch should sail through both the thread
    and workspace guards and reach its 200 ack.
    """
    from src.server.app import setup as setup_module

    wm_singleton = MagicMock()
    wm_singleton.has_ready_session.return_value = True

    btm = MagicMock()
    btm.get_admission_lock = AsyncMock(return_value=asyncio.Lock())
    btm.wait_for_admission = AsyncMock(return_value=("fresh", None))

    with (
        patch(
            f"{THREADS_MOD}.get_thread_auth_meta",
            new=AsyncMock(
                return_value=(
                    {
                        "user_id": owner_id,
                        "workspace_id": "ws-placeholder",
                        "llm_model": None,
                        # Only a live Super-agent surface is admitted (RA-3).
                        "platform": "superagent_router",
                    }
                    if owner_id is not None
                    else None
                )
            ),
        ),
        # Workspace IDOR guard reads the workspace owner via get_workspace.
        patch(
            "src.server.database.workspace.get_workspace",
            new=AsyncMock(
                return_value={
                    "workspace_id": "ws-placeholder",
                    "user_id": ws_owner,
                    "status": "running",
                }
            ),
        ),
        patch.object(setup_module, "agent_config", MagicMock()),
        patch(
            "src.server.services.llm.config.resolve_llm_config",
            new=AsyncMock(return_value=_make_config()),
        ),
        patch(
            "src.server.dependencies.usage_limits.enforce_credit_limit",
            new=AsyncMock(),
        ),
        patch(
            "src.server.services.workspace_manager.WorkspaceManager.get_instance",
            return_value=wm_singleton,
        ),
        patch(
            "src.server.handlers.chat.astream_ptc_workflow",
            new=_dispatched_gen,
        ),
        patch(
            "src.server.services.runs.executor.LocalRunExecutor.get_instance",
            return_value=btm,
        ),
        patch(f"{THREADS_MOD}._consume_background_gen", new=AsyncMock()),
        patch(f"{THREADS_MOD}._get_service_token", return_value=TOKEN),
        patch(
            f"{THREADS_MOD}.observe_background_chat_turn",
            new=lambda coro, **_: coro,
        ),
        # A 403 unwinds through the burst-slot release; keep it hermetic.
        patch(
            "src.server.dependencies.usage_limits.release_burst_slot",
            new=AsyncMock(),
        ),
        # Transport preflight (I6) would 503 in the Redis-less unit env.
        patch(
            f"{THREADS_MOD}._assert_stream_transport_ready",
            new=AsyncMock(),
        ),
    ):
        yield


_PTC_BODY = {
    "workspace_id": "ws-placeholder",
    "messages": [{"role": "user", "content": "test query"}],
}


async def _post(app, path: str, *, headers=None):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        return await c.post(path, json=_PTC_BODY, headers=headers or {})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_existing_thread_owned_by_other_user_is_forbidden():
    """The core IDOR: caller POSTs to a thread owned by someone else -> 403."""
    app = _app_for(CALLER)
    with _stub_downstream(owner_id=OWNER):
        resp = await _post(app, "/api/v1/threads/tid-existing/messages")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "Forbidden"


@pytest.mark.asyncio
async def test_post_existing_thread_owned_by_caller_proceeds():
    """Caller owns the thread (owner_id == user_id) -> guard passes, 200.

    Sent as the server's own dispatch on a live Router thread, the only turn
    the door still admits, so the IDOR guard is what decides.
    """
    app = _app_for(CALLER)
    with _stub_downstream(owner_id=CALLER):
        resp = await _post(
            app,
            "/api/v1/threads/tid-mine/messages",
            headers=DISPATCH,
        )
        for _ in range(3):
            await asyncio.sleep(0)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "dispatched"




# The former internal-dispatch case (X-Dispatch with no service token, no
# HOST_MODE patch) asserted the old silent foreground downgrade returned 200.
# Its outcome now depends on mode — 403 in platform, a trusted background
# dispatch in oss — so it moved to test_threads_dispatch_auth.py, which patches
# HOST_MODE explicitly and mocks the dispatch path for both.


