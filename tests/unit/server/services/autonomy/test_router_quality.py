"""Deterministic routing fakes exercise the real host route success boundary."""

import hashlib
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.config import settings
from src.server.services.autonomy import host, router
from src.server.services.autonomy import evals as eval_service


def _checksum(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _snapshot_row() -> dict:
    snapshot = router._router_snapshot()
    return {
        "snapshot_json": snapshot,
        "created_at": datetime.now(timezone.utc),
        "invalidated_at": None,
        "generation": 1,
        "integrity_sha256": _checksum(snapshot),
    }


class DeterministicRouter:
    """A fake model ranks semantic fields, then invokes the production host."""

    def __init__(self, tool_host, receipt):
        self.tool_host = tool_host
        self.receipt = receipt
        self.decisions = []

    async def dispatch_turn(self, **kwargs):
        payload = json.loads(kwargs["prompt"].split("\n", 1)[1])
        groups = {}
        for item in payload["candidates"]:
            groups.setdefault(item["eval_id"], []).append(item)
        for eval_id in sorted(groups):
            candidates = groups[eval_id]
            if all(item.get("strength") == "ignore" for item in candidates):
                selected = min(candidates, key=lambda item: item["trigger_id"])
                decision = "ignore"
            else:
                selected = min(
                    candidates,
                    key=lambda item: (
                        0 if item.get("strength") == "explicit" else 1,
                        0 if item.get("addressed") else 1,
                        item["trigger_id"],
                    ),
                )
                decision = "accept"
            self.decisions.append((selected["trigger_id"], decision))
            await self.tool_host.route(
                trigger_id=selected["trigger_id"],
                decision=decision,
                title="deterministic candidate",
                scope="review the selected request",
                reason="selected by explicit strength and address state",
            )
        return self.receipt


@pytest.mark.asyncio
async def test_fake_router_is_stable_under_reordering_and_records_real_host_outcome():
    triggers = {}
    candidates = []
    eval_ids = []
    expected = []
    for index in range(10):
        eval_id = f"eval-{index}"
        eval_ids.append(eval_id)
        for suffix, strength, addressed in (
            ("inferred", "inferred", False),
            ("explicit", "explicit", True),
        ):
            trigger_id = f"trigger-{index:02d}-{suffix}"
            triggers[trigger_id] = {
                "trigger_id": trigger_id,
                "eval_id": eval_id,
                "user_id": "user-1",
                "source_id": "source-1",
                "thread_key": f"thread-{index}",
                "actor_login": "alice",
                "actor_is_bot": False,
                "addressed": addressed,
            }
            candidates.append({
                "eval_id": eval_id,
                "trigger_id": trigger_id,
                "source": "github",
                "source_id": "source-1",
                "thread_key": f"thread-{index}",
                "addressed": addressed,
                "strength": strength,
                "text": f"{strength} request {index}",
                "text_is_untrusted_source_content": True,
            })
        expected.append(f"trigger-{index:02d}-explicit")
    triggers["trigger-ignored"] = {
        "trigger_id": "trigger-ignored",
        "eval_id": "eval-ignore",
        "user_id": "user-1",
        "source_id": "source-1",
        "thread_key": "thread-ignore",
        "actor_login": "alice",
        "actor_is_bot": False,
        "addressed": False,
    }
    receipt = object()
    fake = DeterministicRouter(host.AutonomyToolHost(user_id="user-1", thread_id="home"), receipt)
    decisions_written = AsyncMock()
    metric_rows = {}

    async def reserve(**kwargs):
        metric_rows[kwargs["eval_id"]] = kwargs
        return kwargs["decision_id"]

    async def decide_eval(*, eval_id, decision, **_kwargs):
        if decision == "ignore":
            return True
        return {"ticket_id": f"ticket-{eval_id}"}

    settings_patch = patch.object(settings, "get_autonomy_router_snapshot_enabled", return_value=True)
    with (
        settings_patch,
        patch.object(router.router_db, "get_router_baseline_snapshot", AsyncMock(return_value=_snapshot_row())),
        patch.object(router.router_db, "save_router_baseline_snapshot", AsyncMock()),
        patch.object(router.router_db, "reserve_router_dispatch_metric", AsyncMock(side_effect=reserve)),
        patch.object(router.router_db, "complete_router_dispatch_metric", AsyncMock()),
        patch.object(router, "ensure_router_session", AsyncMock(return_value={"thread_id": "home", "workspace_id": "home-ws"})),
        patch.object(router, "get_autonomy_router_model", return_value="deterministic"),
        patch.object(host.eval_db, "active_eval_for_trigger", AsyncMock(side_effect=lambda tid: {"eval_id": triggers[tid]["eval_id"]})),
        patch.object(host.trigger_db, "get_trigger", AsyncMock(side_effect=lambda tid: triggers[tid])),
        patch.object(host.source_db, "get_source_any_user", AsyncMock(return_value={
            "source_id": "source-1",
            "user_id": "user-1",
            "kind": "github",
            "enabled": True,
            "config": {"allowed_logins": ["alice"]},
        })),
        patch.object(host.policy, "current_policy", AsyncMock(return_value={
            "version": "policy-1",
            "accept_mode": "explicit",
            "admission": {},
        })),
        patch.object(host.eval_db, "decide_eval", AsyncMock(side_effect=decide_eval)),
        patch.object(host, "clarify_enabled", return_value=False),
        patch.object(host, "gate", AsyncMock(return_value="held")),
        patch.object(host, "enqueue_effect", AsyncMock()),
        patch.object(host.router_db, "record_router_route_decision", decisions_written),
    ):
        result_a = await router.dispatch_candidate_batch(
            user_id="user-1",
            eval_id=eval_ids[0],
            eval_ids=eval_ids,
            candidates=candidates,
            accept_mode="explicit",
            policy_version="policy-1",
            dispatcher=fake,
        )
        rerouted_eval_ids = [f"reroute-{index}" for index in range(10)]
        rerouted_candidates = []
        for index, eval_id in enumerate(rerouted_eval_ids):
            original = [item for item in candidates if item["eval_id"] == f"eval-{index}"]
            for item in original:
                rerouted_candidates.append({**item, "eval_id": eval_id})
                triggers[item["trigger_id"]]["eval_id"] = eval_id
        result_b = await router.dispatch_candidate_batch(
            user_id="user-1",
            eval_id=rerouted_eval_ids[0],
            eval_ids=rerouted_eval_ids,
            candidates=list(reversed(rerouted_candidates)),
            accept_mode="explicit",
            policy_version="policy-1",
            dispatcher=fake,
        )
        ignored = await router.dispatch_candidate_batch(
            user_id="user-1",
            eval_id="eval-ignore",
            candidates=[{
                "eval_id": "eval-ignore",
                "trigger_id": "trigger-ignored",
                "source": "github",
                "source_id": "source-1",
                "thread_key": "thread-1",
                "addressed": False,
                "strength": "ignore",
                "text": "unadmitted",
                "text_is_untrusted_source_content": True,
            }],
            accept_mode="explicit",
            policy_version="policy-1",
            dispatcher=fake,
        )

    assert result_a is receipt and result_b is receipt and ignored is receipt
    assert [trigger_id for trigger_id, _ in fake.decisions[:10]] == expected
    assert [trigger_id for trigger_id, _ in fake.decisions[10:20]] == expected
    assert all(decision == "accept" for _, decision in fake.decisions[:20])
    assert fake.decisions[20] == ("trigger-ignored", "ignore")
    for index in range(10):
        assert metric_rows[f"eval-{index}"]["stable_decision_key"] == metric_rows[
            f"reroute-{index}"
        ]["stable_decision_key"]
    assert decisions_written.await_count == 21
    assert [
        call.kwargs["trigger_id"] for call in decisions_written.await_args_list
    ] == expected + expected + ["trigger-ignored"]
    assert [
        call.kwargs["decision"] for call in decisions_written.await_args_list
    ] == ["accept"] * 20 + ["ignore"]


@pytest.mark.asyncio
async def test_flag_off_keeps_legacy_prompt_and_skips_snapshot_metrics():
    payload = {
        "kind": "superagent_candidates",
        "eval_id": "eval-1",
        "eval_ids": ["eval-1"],
        "accept_mode": "explicit",
        "candidates": [{"eval_id": "eval-1", "trigger_id": "trigger-1"}],
        "thread_context": {},
    }
    with (
        patch.object(settings, "get_autonomy_router_snapshot_enabled", return_value=False),
        patch.object(router.router_db, "get_router_baseline_snapshot", AsyncMock()) as get_snapshot,
        patch.object(router.router_db, "reserve_router_dispatch_metric", AsyncMock()) as reserve,
        patch.object(router.router_db, "complete_router_dispatch_metric", AsyncMock()) as complete,
        patch.object(
            router,
            "ensure_router_session",
            AsyncMock(return_value={"thread_id": "home", "workspace_id": "home-ws"}),
        ),
        patch.object(router, "get_autonomy_router_model", return_value="test-model"),
    ):
        prompt = await router._candidate_batch_prompt("user-1", payload)
        dispatcher = AsyncMock(return_value=object())
        await router.dispatch_candidate_batch(
            user_id="user-1",
            eval_id="eval-1",
            candidates=payload["candidates"],
            dispatcher=dispatcher,
        )

    assert prompt == router._router_batch_prompt(payload)
    get_snapshot.assert_not_awaited()
    reserve.assert_not_awaited()
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_stalled_metric_reservation_does_not_block_dispatch():
    never = asyncio.Event()

    async def stall_reservation(**_kwargs):
        await never.wait()

    receipt = object()
    dispatcher = type(
        "Dispatcher", (), {"dispatch_turn": AsyncMock(return_value=receipt)}
    )()
    with (
        patch.object(settings, "get_autonomy_router_snapshot_enabled", return_value=True),
        patch.object(router, "_candidate_batch_prompt", AsyncMock(return_value="prompt")),
        patch.object(router, "ensure_router_session", AsyncMock(return_value={
            "thread_id": "home",
            "workspace_id": "home-ws",
        })),
        patch.object(router, "ROUTER_METRIC_TIMEOUT_SECONDS", 0.001),
        patch.object(router.router_db, "reserve_router_dispatch_metric", stall_reservation),
        patch.object(router.router_db, "complete_router_dispatch_metric", AsyncMock()) as complete,
    ):
        result = await router.dispatch_candidate_batch(
            user_id="user-1",
            eval_id="eval-1",
            candidates=[{"eval_id": "eval-1", "trigger_id": "trigger-1"}],
            dispatcher=dispatcher,
        )

    assert result is receipt
    dispatcher.dispatch_turn.assert_awaited_once()
    complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_dispatch_leaves_metric_unknown():
    async def cancel_dispatch(**_kwargs):
        raise asyncio.CancelledError

    class CancelDispatcher:
        async def dispatch_turn(self, **kwargs):
            return await cancel_dispatch(**kwargs)

    complete = AsyncMock()
    with (
        patch.object(settings, "get_autonomy_router_snapshot_enabled", return_value=True),
        patch.object(router, "_candidate_batch_prompt", AsyncMock(return_value="prompt")),
        patch.object(router, "ensure_router_session", AsyncMock(return_value={
            "thread_id": "home",
            "workspace_id": "home-ws",
        })),
        patch.object(
            router.router_db,
            "reserve_router_dispatch_metric",
            AsyncMock(side_effect=lambda **kwargs: kwargs["decision_id"]),
        ),
        patch.object(router.router_db, "complete_router_dispatch_metric", complete),
    ):
        with pytest.raises(asyncio.CancelledError):
            await router.dispatch_candidate_batch(
                user_id="user-1",
                eval_id="eval-1",
                candidates=[{"eval_id": "eval-1", "trigger_id": "trigger-1"}],
                    dispatcher=CancelDispatcher(),
            )

    complete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lookup_result",
    [
        {
            "eval_id": "eval-prior",
            "routed_trigger_id": "trigger-live",
            "route_decision": "ignore",
            "decided_at": "2026-11-03T00:00:00+00:00",
        },
        RuntimeError("metrics temporarily unavailable"),
    ],
)
async def test_snapshot_path_skips_history_reads_and_bounds_recent_decision(
    lookup_result,
):
    effect = type(
        "Effect",
        (),
        {"payload": {"user_id": "user-1", "eval_ids": ["eval-live"]}},
    )()
    trigger = {
        "trigger_id": "trigger-live",
        "source_id": "source-1",
        "thread_key": "thread-1",
        "actor_login": "alice",
        "actor_is_bot": False,
        "addressed": True,
        "text": "current request",
        "url": "",
    }
    source = {
        "source_id": "source-1",
        "kind": "github",
        "config": {"allowed_logins": ["alice"], "channel": "github"},
    }
    dispatch = AsyncMock()
    list_triggers = AsyncMock(side_effect=AssertionError("history read in snapshot mode"))
    list_tickets = AsyncMock(side_effect=AssertionError("history read in snapshot mode"))
    lookup = AsyncMock(
        side_effect=(
            [lookup_result]
            if isinstance(lookup_result, Exception)
            else [lookup_result]
        )
    )
    if isinstance(lookup_result, Exception):
        lookup.side_effect = lookup_result
    with (
        patch.object(router, "router_snapshot_enabled", return_value=True),
        patch.object(eval_service.eval_db, "get_eval", AsyncMock(return_value={
            "eval_id": "eval-live",
            "status": "queued",
            "thread_key": "thread-1",
            "trigger_ids": ["trigger-live"],
        })),
        patch.object(eval_service.trigger_db, "get_trigger", AsyncMock(return_value=trigger)),
        patch.object(eval_service.source_db, "get_source_any_user", AsyncMock(return_value=source)),
        patch.object(eval_service.policy, "current_policy", AsyncMock(return_value={
            "version": "policy-1",
            "accept_mode": "explicit",
            "admission": {},
        })),
        patch.object(eval_service.trigger_db, "list_triggers_for_thread", list_triggers),
        patch.object(eval_service.ticket_db, "list_tickets_for_thread", list_tickets),
        patch.object(eval_service.router_db, "latest_router_decision_for_batch", lookup),
        patch.object(eval_service.router, "dispatch_candidate_batch", dispatch),
        patch.object(eval_service.eval_db, "mark_delivered", AsyncMock()),
    ):
        await eval_service.route_eval_effect(effect)

    kwargs = dispatch.await_args.kwargs
    assert kwargs["candidates"][0]["trigger_id"] == "trigger-live"
    assert kwargs["policy_version"] == "policy-1"
    context = kwargs["thread_context"]["threads"]["thread-1"]
    assert context["scope"] == "current_router_batch"
    assert context["candidates"] == [
        {"eval_id": "eval-live", "trigger_id": "trigger-live"}
    ]
    if isinstance(lookup_result, dict):
        assert context["recent_decision"]["decision"] == "ignore"
        assert context["recent_decision"]["informational_only"] is True
        assert context["recent_decision"]["candidate_ids"] == ["trigger-live"]
    else:
        assert "recent_decision" not in context
    list_triggers.assert_not_awaited()
    list_tickets.assert_not_awaited()
