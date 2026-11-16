"""A stale quota wake does not walk the remaining Ticket pages."""

import uuid

import pytest

from src.server.services.autonomy.wakers import (
    _apply_quota_locked,
    quota_history,
    quota_store,
    runtime,
    scheduling,
)
from src.server.services.autonomy.wakers.base import WakeEvent

_IDS = sorted(str(uuid.uuid4()) for _ in range(1001))


def _row(ticket_id):
    return {
        "ticket_id": ticket_id,
        "user_id": "u",
        "status": "queued",
        "mandate": {},
        "attention": None,
        "worker_provider": "glm",
        "line_id": "",
        "revision": 1,
    }


def _patch(monkeypatch, *, stale):
    calls = {"enqueued": [], "saves": 0}

    async def list_items(provider_names, *, ticket_id=None, limit=200, **_kw):
        if ticket_id:
            return [_row(ticket_id)]
        return [_row(t) for t in _IDS][:limit]

    async def horizon(_provider):
        return ""

    async def outranks(_provider, _effect_id):
        return stale

    async def apply(*_a, **_kw):
        calls["saves"] += 1
        return True

    async def enqueue(*, user_id, idempotency_key, payload):
        calls["enqueued"].append(idempotency_key)
        return True

    monkeypatch.setattr(quota_store, "list_quota_work_items", list_items)
    monkeypatch.setattr(quota_history, "latest_quota_observed_at", horizon)
    monkeypatch.setattr(quota_history, "quota_observation_outranks_effect", outranks)
    monkeypatch.setattr(quota_store, "apply_quota_schedule", apply)
    monkeypatch.setattr(runtime, "enqueue_waker_effect", enqueue)
    return calls


def _event():
    return WakeEvent(
        source="codexbar",
        kind="quota_exhausted",
        idempotency_key="k1",
        provider="glm",
        observed_at="2026-11-10T00:00:00+00:00",
        effect_id=str(uuid.uuid4()),
    )


@pytest.mark.asyncio
async def test_a_stale_full_page_writes_nothing_and_enqueues_no_continuation(
    monkeypatch,
):
    calls = _patch(monkeypatch, stale=True)
    await _apply_quota_locked(_event(), store=scheduling.DbQuotaStore(), max_retries=3)
    assert calls["saves"] == 0
    assert calls["enqueued"] == []


@pytest.mark.asyncio
async def test_a_current_full_page_still_enqueues_its_continuation(monkeypatch):
    calls = _patch(monkeypatch, stale=False)
    await _apply_quota_locked(_event(), store=scheduling.DbQuotaStore(), max_retries=3)
    assert calls["saves"] > 0
    assert calls["enqueued"] == [f"k1:after:{_IDS[999]}"]
