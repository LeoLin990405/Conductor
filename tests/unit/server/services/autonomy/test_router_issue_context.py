"""Issue context rides on Router candidate cards, batches, and snapshots."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.config import settings
from src.server.services.autonomy import evals as eval_service
from src.server.services.autonomy import router
from src.server.services.autonomy.evals import candidate_card


def _source() -> dict:
    return {
        "source_id": "source-1",
        "kind": "github",
        "config": {"repository": "acme/widgets", "allowed_logins": ["alice"]},
    }


def _trigger(**overrides) -> dict:
    trigger = {
        "trigger_id": "trg_1",
        "source_id": "source-1",
        "thread_key": "github:acme/widgets#12",
        "actor_login": "alice",
        "actor_is_bot": False,
        "addressed": True,
        "text": "please fix the bug",
        "url": "https://github.com/acme/widgets/issues/12#issuecomment-1",
    }
    trigger.update(overrides)
    return trigger


def _issue_context() -> dict:
    return {
        "title": "Save button drops the draft",
        "body": "Steps: open the editor and click Save.",
        "truncated": False,
        "url": "https://github.com/acme/widgets/issues/12",
        "labels": ["bug"],
        "number": 12,
    }


def test_candidate_card_copies_issue_context_routing_fields():
    card = candidate_card(
        _trigger(payload={"issue_context": _issue_context()}),
        _source(),
    )
    assert card["issue_context"] == {
        "title": "Save button drops the draft",
        "body": "Steps: open the editor and click Save.",
        "truncated": False,
        "url": "https://github.com/acme/widgets/issues/12",
    }
    assert card["text_is_untrusted_source_content"] is True


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"issue_context": None},
        {"issue_context": []},
        {"issue_context": "Save button"},
        {"issue_context": {}},
        {"issue_context": {"labels": ["bug"], "number": 12}},
        {
            "issue_context": {
                "title": None,
                "body": 12,
                "url": None,
                "truncated": "false",
            }
        },
        {"issue_context": {"title": None, "truncated": None}},
    ],
)
def test_candidate_card_omits_issue_context_when_absent(payload):
    overrides = {}
    if payload is not None:
        overrides["payload"] = payload
    card = candidate_card(_trigger(**overrides), _source())
    assert "issue_context" not in card


def test_candidate_card_ignores_issue_context_outside_the_payload():
    card = candidate_card(
        _trigger(issue_context=_issue_context()),
        _source(),
    )
    assert "issue_context" not in card


def test_candidate_card_keeps_a_partial_issue_context():
    card = candidate_card(
        _trigger(
            payload={"issue_context": {"title": "Only a title", "truncated": True}}
        ),
        _source(),
    )
    assert card["issue_context"] == {"title": "Only a title", "truncated": True}


def _prompt_payload(prompt: str) -> dict:
    start = prompt.index("\n{")
    return json.loads(prompt[start + 1 :])


def _cached_snapshot_row() -> dict:
    snapshot = router._router_snapshot()
    raw = json.dumps(
        snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return {
        "snapshot_json": snapshot,
        "created_at": datetime.now(timezone.utc),
        "invalidated_at": None,
        "generation": 1,
        "integrity_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


def test_legacy_router_batch_prompt_carries_issue_context():
    card = candidate_card(
        _trigger(payload={"issue_context": _issue_context()}),
        _source(),
    )
    card["eval_id"] = "eval-1"
    prompt = router._router_batch_prompt(
        {
            "kind": "superagent_candidates",
            "eval_id": "eval-1",
            "eval_ids": ["eval-1"],
            "accept_mode": "explicit",
            "candidates": [card],
            "thread_context": {},
        }
    )
    assert (
        _prompt_payload(prompt)["candidates"][0]["issue_context"]
        == card["issue_context"]
    )


@pytest.mark.asyncio
async def test_snapshot_router_prompt_carries_issue_context():
    card = candidate_card(
        _trigger(payload={"issue_context": _issue_context()}),
        _source(),
    )
    card["eval_id"] = "eval-1"
    card["channel"] = "github"
    payload = {
        "kind": "superagent_candidates",
        "eval_id": "eval-1",
        "eval_ids": ["eval-1"],
        "accept_mode": "explicit",
        "candidates": [card],
        "thread_context": {
            "threads": {
                "github:acme/widgets#12": {
                    "thread_key": "github:acme/widgets#12",
                    "scope": "current_router_batch",
                    "candidates": [{"eval_id": "eval-1", "trigger_id": "trg_1"}],
                }
            }
        },
    }
    with (
        patch.object(
            settings, "get_autonomy_router_snapshot_enabled", return_value=True
        ),
        patch.object(
            router.router_db,
            "get_router_baseline_snapshot",
            AsyncMock(return_value=_cached_snapshot_row()),
        ),
        patch.object(router.router_db, "save_router_baseline_snapshot", AsyncMock()),
    ):
        prompt = await router._candidate_batch_prompt("user-1", payload)
    assert (
        _prompt_payload(prompt)["candidates"][0]["issue_context"]
        == card["issue_context"]
    )


def _effect() -> object:
    return type(
        "Effect",
        (),
        {"payload": {"user_id": "user-1", "eval_ids": ["eval-1"], "shard": 0}},
    )()


def _eval_row() -> dict:
    return {
        "eval_id": "eval-1",
        "status": "queued",
        "thread_key": "github:acme/widgets#12",
        "trigger_ids": ["trg_1"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [False, True])
async def test_route_eval_effect_candidate_batch_carries_issue_context(snapshot):
    trigger = _trigger(payload={"issue_context": _issue_context()})
    dispatch = AsyncMock()
    with (
        patch.object(
            eval_service.router, "router_snapshot_enabled", return_value=snapshot
        ),
        patch.object(
            eval_service.eval_db, "get_eval", AsyncMock(return_value=_eval_row())
        ),
        patch.object(
            eval_service.trigger_db, "get_trigger", AsyncMock(return_value=trigger)
        ),
        patch.object(
            eval_service.source_db,
            "get_source_any_user",
            AsyncMock(return_value=_source()),
        ),
        patch.object(
            eval_service.policy,
            "current_policy",
            AsyncMock(
                return_value={
                    "version": "v1",
                    "accept_mode": "explicit",
                    "admission": {},
                }
            ),
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
        patch.object(
            eval_service.router_db,
            "latest_router_decision_for_batch",
            AsyncMock(return_value=None),
        ),
        patch.object(eval_service.router, "dispatch_candidate_batch", dispatch),
        patch.object(eval_service.eval_db, "mark_delivered", AsyncMock()),
    ):
        await eval_service.route_eval_effect(_effect())

    candidates = dispatch.await_args.kwargs["candidates"]
    assert candidates[0]["issue_context"] == {
        "title": "Save button drops the draft",
        "body": "Steps: open the editor and click Save.",
        "truncated": False,
        "url": "https://github.com/acme/widgets/issues/12",
    }
    if snapshot:
        assert candidates[0]["channel"] == ""
        context = dispatch.await_args.kwargs["thread_context"]["threads"][
            "github:acme/widgets#12"
        ]
        assert context["scope"] == "current_router_batch"


def test_candidate_card_carries_the_stored_overflow_url():
    """``build_issue_context`` stores the spill link as ``overflow_url``."""
    from src.server.services.autonomy.issue_context import (
        ISSUE_BODY_CAP,
        build_issue_context,
    )

    stored = build_issue_context(
        {
            "title": "Save button drops the draft",
            "body": "B" * (ISSUE_BODY_CAP + 500),
            "html_url": "https://github.com/acme/widgets/issues/12",
        }
    )
    assert stored["overflow_url"] == "https://github.com/acme/widgets/issues/12"
    assert "url" not in stored

    card = candidate_card(_trigger(payload={"issue_context": stored}), _source())
    assert card["issue_context"]["url"] == "https://github.com/acme/widgets/issues/12"
    assert card["issue_context"]["truncated"] is True

    both = {**stored, "url": "https://github.com/acme/widgets/issues/12#top"}
    card = candidate_card(_trigger(payload={"issue_context": both}), _source())
    assert card["issue_context"]["url"] == "https://github.com/acme/widgets/issues/12#top"
