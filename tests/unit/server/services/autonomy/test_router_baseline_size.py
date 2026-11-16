"""Prompt byte checks compare real legacy and snapshot render paths."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.config import settings
from src.server.services.autonomy.channels import resolve_channel
from src.server.services.autonomy import router
from src.server.services.autonomy.context import thread_context


def _cached_row() -> dict:
    snapshot = router._router_snapshot()
    raw = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        "snapshot_json": snapshot,
        "created_at": datetime.now(timezone.utc),
        "invalidated_at": None,
        "generation": 1,
        "integrity_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    }


@pytest.mark.asyncio
async def test_snapshot_hit_reduces_prompt_bytes_and_keeps_current_batch_norms(caplog):
    old_context = thread_context(
        [
            {
                "trigger_id": f"old-trigger-{index}",
                "actor_login": "alice",
                "addressed": bool(index % 2),
                "text": f"prior trigger {index}: " + ("history " * 55),
            }
            for index in range(8)
        ],
        [
            {
                "ticket_id": f"old-ticket-{index}",
                "title": f"Ticket {index}",
                "status": "running",
                "summary": "prior ticket context " * 25,
            }
            for index in range(8)
        ],
        thread_key="thread-1",
    )
    active_context = {
        "thread_key": "thread-1",
        "scope": "current_router_batch",
        "candidates": [{"eval_id": "eval-1", "trigger_id": "trigger-live"}],
        "recent_decision": {
            "eval_id": "eval-prior",
            "trigger_id": "trigger-live",
            "decision": "ignore",
            "scope": "same_trigger_batch",
            "informational_only": True,
        },
    }
    candidate = {
        "eval_id": "eval-1",
        "trigger_id": "trigger-live",
        "source": "github",
        "source_id": "source-1",
        "thread_key": "thread-1",
        "actor": "alice",
        "addressed": True,
        "strength": "explicit",
        "text": "Please review this change.",
        "text_is_untrusted_source_content": True,
        "url": "",
    }
    old_payload = {
        "kind": "superagent_candidates",
        "eval_id": "eval-1",
        "eval_ids": ["eval-1"],
        "accept_mode": "explicit",
        "candidates": [candidate],
        "thread_context": {"threads": {"thread-1": old_context}},
    }
    new_payload = {
        **old_payload,
        "thread_context": {"threads": {"thread-1": active_context}},
    }
    old_prompt = router._router_batch_prompt(old_payload)
    with (
        patch.object(settings, "get_autonomy_router_snapshot_enabled", return_value=True),
        patch.object(
            router.router_db,
            "get_router_baseline_snapshot",
            AsyncMock(return_value=_cached_row()),
        ),
        patch.object(router.router_db, "save_router_baseline_snapshot", AsyncMock()),
        caplog.at_level(logging.INFO, logger="src.server.services.autonomy.router"),
    ):
        new_prompt = await router._candidate_batch_prompt("user-1", new_payload)

    old_bytes = len(old_prompt.encode("utf-8"))
    new_bytes = len(new_prompt.encode("utf-8"))
    assert new_bytes <= old_bytes * 0.75
    assert "GitHub replies" in new_prompt
    assert "Slack replies" not in new_prompt
    assert "trigger-live" in new_prompt
    assert '"informational_only": true' in new_prompt
    assert "informational_only" in new_prompt
    runtime_legacy_bytes = len(router._router_batch_prompt(new_payload).encode("utf-8"))
    assert f"legacy={runtime_legacy_bytes}" in caplog.text
    assert f"snapshot={new_bytes}" in caplog.text


@pytest.mark.asyncio
async def test_configured_channel_override_uses_its_current_norm():
    candidate = {
        "source": "github",
        "channel": "email",
    }
    norms = router._candidate_norms([candidate])
    assert norms == resolve_channel("github", {"channel": "email"}).norm
    assert "Email replies" in norms
    assert "GitHub replies" not in norms
