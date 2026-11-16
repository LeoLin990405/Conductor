"""Router Evals stay independently decidable while sharing bounded turns."""

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import evals as eval_service


@pytest.mark.asyncio
async def test_scheduler_recovers_effect_gap_and_batches_distinct_threads_by_user():
    trigger_rows = {
        "trigger-a": {
            "trigger_id": "trigger-a", "source_id": "source-a",
            "actor_login": "alice", "actor_is_bot": False, "addressed": True,
        },
        "trigger-b": {
            "trigger_id": "trigger-b", "source_id": "source-b",
            "actor_login": "alice", "actor_is_bot": False, "addressed": True,
        },
    }
    source_row = {
        "kind": "github",
        "config": {"allowed_logins": ["alice"]},
    }
    enqueue = AsyncMock(return_value=True)
    with (
        patch.object(
            eval_service.eval_db,
            "list_queued_evals_without_effect",
            AsyncMock(return_value=[{"eval_id": "recovered-eval", "user_id": "user-1"}]),
        ),
        patch.object(
            eval_service.eval_db,
            "list_pending_threads",
            AsyncMock(return_value=[
                {
                    "thread_key": "thread-a", "source_id": "source-a",
                    "user_id": "user-1", "pending_trigger_ids": ["trigger-a"],
                },
                {
                    "thread_key": "thread-b", "source_id": "source-b",
                    "user_id": "user-1", "pending_trigger_ids": ["trigger-b"],
                },
            ]),
        ),
        patch.object(
            eval_service.trigger_db, "get_trigger",
            AsyncMock(side_effect=lambda trigger_id: trigger_rows[trigger_id]),
        ),
        patch.object(
            eval_service.source_db, "get_source_any_user",
            AsyncMock(return_value=source_row),
        ),
        patch.object(
            eval_service.policy, "current_policy",
            AsyncMock(return_value={
                "version": "v1", "accept_mode": "explicit",
                "admission": {"listed_addressed": "explicit"},
            }),
        ),
        patch.object(
            eval_service.eval_db, "start_thread_eval",
            AsyncMock(side_effect=[
                {"eval_id": "eval-a"}, {"eval_id": "eval-b"},
            ]),
        ) as create_eval,
        patch.object(eval_service, "enqueue_effect", enqueue),
    ):
        started = await eval_service.start_due_evals()

    assert started == 3
    assert create_eval.await_count == 2
    enqueue.assert_awaited_once()
    payload = enqueue.await_args.kwargs["payload"]
    assert payload["user_id"] == "user-1"
    assert payload["eval_ids"] == ["recovered-eval", "eval-a", "eval-b"]
    assert enqueue.await_args.kwargs["idempotency_key"].startswith(
        "autonomy:router-batch:"
    )


@pytest.mark.asyncio
async def test_router_batch_retry_omits_an_eval_that_was_already_decided():
    effect = type(
        "Effect", (), {
            "payload": {"user_id": "user-1", "eval_ids": ["eval-a", "eval-b"]}
        }
    )()
    eval_rows = {
        "eval-a": {"eval_id": "eval-a", "status": "decided", "trigger_ids": ["trigger-a"]},
        "eval-b": {"eval_id": "eval-b", "status": "queued", "thread_key": "thread-b", "trigger_ids": ["trigger-b"]},
    }
    trigger = {
        "trigger_id": "trigger-b", "source_id": "source-b", "thread_key": "thread-b",
        "actor_login": "alice", "actor_is_bot": False, "addressed": True,
        "text": "please inspect", "url": "",
    }
    source = {"source_id": "source-b", "kind": "github", "config": {"allowed_logins": ["alice"]}}
    dispatch = AsyncMock()
    with (
        patch.object(eval_service.eval_db, "get_eval", AsyncMock(side_effect=lambda eid: eval_rows[eid])),
        patch.object(eval_service.trigger_db, "get_trigger", AsyncMock(return_value=trigger)),
        patch.object(eval_service.source_db, "get_source_any_user", AsyncMock(return_value=source)),
        patch.object(eval_service.policy, "current_policy", AsyncMock(return_value={"accept_mode": "explicit", "admission": {}})),
        patch.object(eval_service.trigger_db, "list_triggers_for_thread", AsyncMock(return_value=[])),
        patch.object(eval_service.ticket_db, "list_tickets_for_thread", AsyncMock(return_value=[])),
        patch.object(eval_service.router, "dispatch_candidate_batch", dispatch),
        patch.object(eval_service.eval_db, "mark_delivered", AsyncMock()) as mark_delivered,
    ):
        await eval_service.route_eval_effect(effect)

    kwargs = dispatch.await_args.kwargs
    assert kwargs["eval_ids"] == ["eval-b"]
    assert kwargs["candidates"][0]["eval_id"] == "eval-b"
    assert kwargs["request_key"].startswith("autonomy:router-batch:")
    assert mark_delivered.await_args_list[0].args == ("eval-b",)
