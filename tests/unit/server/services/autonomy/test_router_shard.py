"""Configurable Router sharding: threshold, assignment and dispatch wiring."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import evals as eval_service
from src.server.services.autonomy import router, shard as router_shard


def test_threshold_and_max_shards_read_positive_ints_from_env(monkeypatch):
    monkeypatch.delenv("AUTONOMY_ROUTER_SHARD_THRESHOLD", raising=False)
    monkeypatch.delenv("AUTONOMY_ROUTER_MAX_SHARDS", raising=False)
    assert router_shard.router_shard_threshold() == 32
    assert router_shard.router_max_shards() == 4

    monkeypatch.setenv("AUTONOMY_ROUTER_SHARD_THRESHOLD", "8")
    monkeypatch.setenv("AUTONOMY_ROUTER_MAX_SHARDS", "2")
    assert router_shard.router_shard_threshold() == 8
    assert router_shard.router_max_shards() == 2

    monkeypatch.setenv("AUTONOMY_ROUTER_SHARD_THRESHOLD", "zero")
    monkeypatch.setenv("AUTONOMY_ROUTER_MAX_SHARDS", "0")
    assert router_shard.router_shard_threshold() == 32
    assert router_shard.router_max_shards() == 4


def test_shard_count_is_one_at_or_below_threshold():
    assert router_shard.router_shard_count(0, threshold=8, max_shards=4) == 1
    assert router_shard.router_shard_count(8, threshold=8, max_shards=4) == 1
    assert router_shard.router_shard_count(9, threshold=8, max_shards=4) == 2
    assert router_shard.router_shard_count(17, threshold=8, max_shards=4) == 3


def test_shard_count_caps_at_max_shards():
    assert router_shard.router_shard_count(1000, threshold=8, max_shards=4) == 4


def test_shard_for_is_stable_in_range_and_zero_when_unsharded():
    assert router_shard.router_shard_for("batch-a", 1) == 0
    first = router_shard.router_shard_for("batch-a", 3)
    assert first == router_shard.router_shard_for("batch-a", 3)
    assert 0 <= first < 3
    spread = {router_shard.router_shard_for(f"batch-{i}", 3) for i in range(24)}
    assert spread == {0, 1, 2}


@pytest.mark.asyncio
async def test_start_due_evals_assigns_batches_to_shards_above_threshold(monkeypatch):
    # 20 evals with threshold 8 → 3 shards; batches still split at 8.
    monkeypatch.setenv("AUTONOMY_ROUTER_SHARD_THRESHOLD", "8")
    monkeypatch.setenv("AUTONOMY_ROUTER_MAX_SHARDS", "4")
    eval_ids = [f"eval-{i}" for i in range(20)]
    enqueue = AsyncMock(return_value=True)
    with (
        patch.object(
            eval_service.eval_db,
            "list_queued_evals_without_effect",
            AsyncMock(
                return_value=[
                    {"eval_id": eval_id, "user_id": "user-1"}
                    for eval_id in eval_ids
                ]
            ),
        ),
        patch.object(
            eval_service.eval_db, "list_pending_threads", AsyncMock(return_value=[])
        ),
        patch.object(eval_service, "enqueue_effect", enqueue),
    ):
        started = await eval_service.start_due_evals()

    assert started == 20
    assert enqueue.await_count == 3
    shard_count = router_shard.router_shard_count(20, threshold=8, max_shards=4)
    assert shard_count == 3
    for call in enqueue.await_args_list:
        payload = call.kwargs["payload"]
        batch_key = hashlib.sha256(
            ",".join(payload["eval_ids"]).encode()
        ).hexdigest()[:20]
        assert payload["shard"] == router_shard.router_shard_for(batch_key, 3)
    assigned = {
        call.kwargs["payload"]["shard"] for call in enqueue.await_args_list
    }
    assert assigned <= {0, 1, 2}


@pytest.mark.asyncio
async def test_start_due_evals_keeps_small_load_on_shard_zero(monkeypatch):
    monkeypatch.setenv("AUTONOMY_ROUTER_SHARD_THRESHOLD", "32")
    enqueue = AsyncMock(return_value=True)
    with (
        patch.object(
            eval_service.eval_db,
            "list_queued_evals_without_effect",
            AsyncMock(
                return_value=[
                    {"eval_id": f"eval-{i}", "user_id": "user-1"} for i in range(3)
                ]
            ),
        ),
        patch.object(
            eval_service.eval_db, "list_pending_threads", AsyncMock(return_value=[])
        ),
        patch.object(eval_service, "enqueue_effect", enqueue),
    ):
        await eval_service.start_due_evals()

    assert enqueue.await_count == 1
    assert enqueue.await_args.kwargs["payload"]["shard"] == 0


async def _route_one_batch(payload: dict) -> AsyncMock:
    effect = type("Effect", (), {"payload": payload})()
    eval_rows = {
        "eval-b": {
            "eval_id": "eval-b",
            "status": "queued",
            "thread_key": "thread-b",
            "trigger_ids": ["trigger-b"],
        },
    }
    trigger = {
        "trigger_id": "trigger-b",
        "source_id": "source-b",
        "thread_key": "thread-b",
        "actor_login": "alice",
        "actor_is_bot": False,
        "addressed": True,
        "text": "please inspect",
        "url": "",
    }
    source = {
        "source_id": "source-b",
        "kind": "github",
        "config": {"allowed_logins": ["alice"]},
    }
    dispatch = AsyncMock()
    with (
        patch.object(
            eval_service.eval_db,
            "get_eval",
            AsyncMock(side_effect=lambda eid: eval_rows[eid]),
        ),
        patch.object(
            eval_service.trigger_db, "get_trigger", AsyncMock(return_value=trigger)
        ),
        patch.object(
            eval_service.source_db,
            "get_source_any_user",
            AsyncMock(return_value=source),
        ),
        patch.object(
            eval_service.policy,
            "current_policy",
            AsyncMock(return_value={"accept_mode": "explicit", "admission": {}}),
        ),
        patch.object(
            eval_service.trigger_db,
            "list_triggers_for_thread",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            eval_service.ticket_db,
            "list_tickets_for_thread",
            AsyncMock(return_value=[]),
        ),
        patch.object(eval_service.router, "dispatch_candidate_batch", dispatch),
        patch.object(eval_service.eval_db, "mark_delivered", AsyncMock()),
    ):
        await eval_service.route_eval_effect(effect)
    return dispatch


@pytest.mark.asyncio
async def test_route_eval_effect_dispatches_to_the_payload_shard():
    dispatch = await _route_one_batch(
        {"user_id": "user-1", "eval_ids": ["eval-b"], "shard": 2}
    )
    assert dispatch.await_args.kwargs["shard"] == 2


@pytest.mark.asyncio
async def test_route_eval_effect_defaults_to_shard_zero():
    dispatch = await _route_one_batch(
        {"user_id": "user-1", "eval_ids": ["eval-b"]}
    )
    assert dispatch.await_args.kwargs["shard"] == 0


@pytest.mark.asyncio
async def test_ensure_router_session_uses_one_session_per_shard():
    sessions: dict[int, dict] = {}

    async def fake_get(user_id: str, shard: int = 0):
        return sessions.get(shard)

    async def fake_save(user_id, workspace_id, thread_id, shard: int = 0):
        row = {
            "user_id": user_id,
            "workspace_id": workspace_id,
            "thread_id": thread_id,
            "shard": shard,
        }
        sessions[shard] = row
        return row

    dispatcher = AsyncMock()
    dispatcher.create_thread.side_effect = (
        lambda **kwargs: f"thread-{kwargs['title']}"
    )
    with (
        patch.object(router.router_db, "get_router_session", side_effect=fake_get),
        patch.object(router.router_db, "save_router_session", side_effect=fake_save),
        patch(
            "src.server.services.autonomy.router.get_home_workspace_id",
            return_value="home-ws",
        ),
    ):
        zero = await router.ensure_router_session("user-1", dispatcher=dispatcher)
        two = await router.ensure_router_session(
            "user-1", dispatcher=dispatcher, shard=2
        )
        again = await router.ensure_router_session(
            "user-1", dispatcher=dispatcher, shard=2
        )

    assert zero["shard"] == 0
    assert two["shard"] == 2
    assert two["thread_id"] == again["thread_id"]
    assert two["thread_id"] != zero["thread_id"]
    titles = [
        call.kwargs["title"] for call in dispatcher.create_thread.await_args_list
    ]
    assert titles == ["Super-agent Router", "Super-agent Router shard 2"]
