from __future__ import annotations

import pytest

from src.server.services.autonomy import router as router_service


class _Dispatcher:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.dispatched: list[dict] = []

    async def create_thread(self, **kwargs):
        self.created.append(kwargs)
        return f"thread-{kwargs['title']}"

    async def dispatch_turn(self, **kwargs):
        self.dispatched.append(kwargs)
        return {"status": "started"}


@pytest.mark.asyncio
async def test_ensure_router_session_creates_the_requested_shard(monkeypatch):
    seen: dict[str, tuple] = {}

    async def get_session(user_id, shard=0):
        seen["get"] = (user_id, shard)
        return None

    async def save_session(user_id, workspace_id, thread_id, shard=0):
        seen["save"] = (user_id, workspace_id, thread_id, shard)
        return {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "thread_id": thread_id,
            "shard": shard,
        }

    monkeypatch.setattr(router_service.router_db, "get_router_session", get_session)
    monkeypatch.setattr(router_service.router_db, "save_router_session", save_session)
    monkeypatch.setattr(
        router_service, "get_home_workspace_id", lambda _user_id: "home-ws"
    )
    dispatcher = _Dispatcher()

    saved = await router_service.ensure_router_session(
        "u1", dispatcher=dispatcher, shard=2
    )

    assert seen["get"] == ("u1", 2)
    assert seen["save"] == ("u1", "home-ws", "thread-Super-agent Router shard 2", 2)
    assert dispatcher.created[0]["title"] == "Super-agent Router shard 2"
    assert saved["shard"] == 2


@pytest.mark.asyncio
async def test_ensure_router_session_reuses_the_shard_row(monkeypatch):
    async def get_session(user_id, shard=0):
        return {"user_id": user_id, "thread_id": "existing", "shard": shard}

    async def save_session(*_args, **_kwargs):
        raise AssertionError("an existing shard session must not be rewritten")

    monkeypatch.setattr(router_service.router_db, "get_router_session", get_session)
    monkeypatch.setattr(router_service.router_db, "save_router_session", save_session)

    saved = await router_service.ensure_router_session(
        "u1", dispatcher=_Dispatcher(), shard=1
    )
    assert saved == {"user_id": "u1", "thread_id": "existing", "shard": 1}


@pytest.mark.asyncio
async def test_dispatch_candidate_batch_uses_the_shard_session(monkeypatch):
    seen: dict[str, int] = {}

    async def ensure(user_id, *, dispatcher, shard=0):
        seen["shard"] = shard
        return {"user_id": user_id, "thread_id": "t", "workspace_id": "ws", "shard": shard}

    async def prompt(_user_id, _payload):
        return "prompt"

    monkeypatch.setattr(router_service, "ensure_router_session", ensure)
    monkeypatch.setattr(router_service, "_candidate_batch_prompt", prompt)
    monkeypatch.setattr(router_service, "router_snapshot_enabled", lambda: False)
    dispatcher = _Dispatcher()

    result = await router_service.dispatch_candidate_batch(
        user_id="u1",
        eval_id="eval-1",
        candidates=[],
        dispatcher=dispatcher,
        shard=3,
    )

    assert seen["shard"] == 3
    assert dispatcher.dispatched[0]["thread_id"] == "t"
    assert result == {"status": "started"}
