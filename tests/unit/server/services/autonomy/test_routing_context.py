"""Per-Source routing, Eval batch identity, and bounded JSON contracts."""

import json

import pytest
from unittest.mock import AsyncMock, patch

from src.server.services.autonomy import policy
from src.server.services.autonomy.evals import router_eval_batches
from src.server.services.autonomy.routing import bounded_json, source_mode
from src.server.services.autonomy.context import thread_context
from src.server.models.autonomy import FeishuSourceConfig, GitHubSourceConfig
from src.server.services.autonomy import router


def _trigger(*, addressed: bool = False, login: str = "alice") -> dict:
    return {"actor_login": login, "addressed": addressed, "actor_is_bot": False}


def test_source_modes_layer_over_admission_and_accept_mode():
    config = {"allowed_logins": ["alice"]}
    assert policy.is_source_candidate(_trigger(addressed=True), config, "explicit")
    assert policy.is_source_candidate(_trigger(), config, "observe")
    assert not policy.is_source_candidate(
        _trigger(), config, "explicit"
    )
    assert policy.is_source_candidate(
        _trigger(), {**config, "routing_mode": "candidate"}, "observe"
    )
    assert not policy.is_source_candidate(
        _trigger(addressed=True), {**config, "routing_mode": "ignore"}, "auto"
    )
    assert not policy.is_source_candidate(
        _trigger(), {**config, "routing_mode": "respond-only-explicit"}, "auto"
    )


def test_invalid_stored_source_mode_fails_closed_but_absent_mode_is_legacy():
    assert source_mode({}) == "candidate"
    assert source_mode({"routing_mode": "bogus"}) == "ignore"
    assert not policy.is_source_candidate(
        _trigger(addressed=True), {"allowed_logins": ["alice"], "routing_mode": "bogus"}
    )


def test_source_config_models_validate_the_routing_mode():
    assert FeishuSourceConfig(
        chat_id="chat-1", workspace_id="workspace-1", routing_mode="ignore"
    ).routing_mode == "ignore"
    with pytest.raises(ValueError):
        FeishuSourceConfig(
            chat_id="chat-1", workspace_id="workspace-1", routing_mode="unknown"
        )
    with pytest.raises(ValueError):
        GitHubSourceConfig(
            repository="org/repo",
            secret_name="github",
            workspace_id="workspace-1",
            routing_mode="unknown",
        )


def test_retry_mandate_revalidation_applies_source_routing_mode():
    ok, reason = policy.revalidate(
        _trigger(addressed=False),
        {"allowed_logins": ["alice"], "routing_mode": "respond-only-explicit"},
        None,
        strength="inferred",
        accept_mode="auto",
    )
    assert not ok
    assert "explicit" in reason
    ok, reason = policy.revalidate(
        _trigger(addressed=True),
        {"allowed_logins": ["alice"], "routing_mode": "ignore"},
        None,
        strength="explicit",
        accept_mode="auto",
    )
    assert not ok
    assert "ignored" in reason


def test_router_eval_batches_are_bounded_ordered_and_deduplicated():
    batches = router_eval_batches([f"eval-{i}" for i in range(10)] + ["eval-0"])
    assert batches == [[f"eval-{i}" for i in range(8)], ["eval-8", "eval-9"]]
    assert all(0 < len(batch) <= 8 for batch in batches)


def test_bounded_json_keeps_candidate_ids_and_is_valid_under_budget():
    result = bounded_json(
        {
            "kind": "superagent_candidates",
            "eval_ids": ["eval-a"],
            "candidates": [
                {
                    "eval_id": "eval-a",
                    "trigger_id": "trigger-a",
                    "thread_key": "thread-a",
                    "text": "汉" * 20_000,
                }
            ],
        },
        max_chars=900,
    )
    parsed = json.loads(result)
    assert len(result) <= 900
    assert parsed["candidates"][0]["eval_id"] == "eval-a"
    assert parsed["candidates"][0]["trigger_id"] == "trigger-a"


def test_bounded_json_rejects_a_budget_that_cannot_preserve_resource_ids():
    with pytest.raises(ValueError, match="preserve required resource IDs"):
        bounded_json(
            {"candidates": [{"eval_id": "eval-a", "trigger_id": "trigger-a"}]},
            max_chars=10,
        )


def test_thread_context_budget_is_exact_for_multibyte_and_many_long_ids():
    rows = [
        {"trigger_id": f"触发-{index}-" + "x" * 1800, "text": "雪" * 1000}
        for index in range(40)
    ]
    context = thread_context(rows, [], max_chars=1600, thread_key="thread-雪")
    encoded = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    assert len(encoded) <= 1600
    assert context["budget"]["used_chars"] == len(encoded)
    assert context["spill_omitted"]["triggers"] > 0
    assert "autonomy_read" in context["spill"]


@pytest.mark.parametrize("budget", [0, 10])
def test_thread_context_rejects_budget_below_usable_envelope(budget: int):
    with pytest.raises(ValueError, match="minimum envelope"):
        thread_context([], [], max_chars=budget)


@pytest.mark.asyncio
async def test_candidate_batch_uses_optional_router_model_override():
    dispatcher = AsyncMock()
    with (
        patch.object(
            router, "ensure_router_session",
            AsyncMock(return_value={"thread_id": "router-thread", "workspace_id": "home"}),
        ),
        patch("src.server.services.autonomy.router.get_autonomy_router_model", return_value="router-model"),
    ):
        await router.dispatch_candidate_batch(
            user_id="user-1",
            eval_id="eval-1",
            candidates=[{"eval_id": "eval-1", "trigger_id": "trigger-1"}],
            dispatcher=dispatcher,
        )
    assert dispatcher.dispatch_turn.await_args.kwargs["model"] == "router-model"
