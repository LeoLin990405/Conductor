"""Wake effects are deduped, switched, and registered on the runner."""

import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.server.services.autonomy.effects import (
    AutonomyEffectRunner,
    PermanentEffectError,
)
from src.server.services.autonomy.wakers.runtime import (
    enqueue_waker_effect,
    register_waker_effect,
)
from src.server.services.autonomy.models import EffectRecord
from src.server.services.autonomy.wakers.base import EFFECT_WAKE
from src.server.services.autonomy.wakers import (
    apply_waker_effect,
    enqueue_wake_events,
    run_waker_pass,
)
from src.server.services.autonomy.wakers.base import WakeEvent
from src.server.services.autonomy.wakers.scheduling import WorkItem

_TOKEN = "ghp_" + ("d" * 24)


@pytest.fixture(autouse=True)
def _noop_quota_provider_lock(monkeypatch):
    @asynccontextmanager
    async def _lock(provider, **kwargs):
        yield

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.quota_provider_lock",
        _lock,
    )


def _effect(source, kind, key="k-1", **payload):
    body = {"source": source, "kind": kind, "provider": "glm", "idempotency_key": key}
    body.update(payload)
    return EffectRecord(
        effect_id="effect-1",
        user_id="user-1",
        kind=EFFECT_WAKE,
        idempotency_key=key,
        payload=body,
        attempts=1,
    )


class _Store:
    def __init__(self, items):
        self.items = items
        self.loaded = []
        self.saved = []

    async def load(self, event):
        self.loaded.append(event)
        return list(self.items)

    async def save(self, action):
        self.saved.append(action)
        return True


@pytest.mark.asyncio
async def test_enqueue_redacts_and_rejects_an_empty_key(monkeypatch):
    insert = AsyncMock(return_value=True)
    monkeypatch.setattr("src.server.database.autonomy.effects.enqueue_effect", insert)
    with pytest.raises(ValueError, match="idempotency key"):
        await enqueue_waker_effect(user_id="user-1", idempotency_key="  ", payload={})
    insert.assert_not_awaited()

    inserted = await enqueue_waker_effect(
        user_id="user-1",
        idempotency_key="waker:codexbar:quota_low:glm:1",
        payload={
            "api_key": "super-secret",
            "note": f"see {_TOKEN}",
            "kind": "quota_low",
        },
    )
    assert inserted is True
    sent = insert.await_args.kwargs
    assert sent["kind"] == EFFECT_WAKE
    assert sent["idempotency_key"] == "waker:codexbar:quota_low:glm:1"
    blob = str(sent["payload"])
    assert "super-secret" not in blob
    assert _TOKEN not in blob
    assert sent["payload"]["api_key"] == "[REDACTED]"


@pytest.mark.asyncio
async def test_a_repeated_key_is_dropped_in_the_batch_and_by_the_sink():
    seen = []

    async def sink(*, user_id, idempotency_key, payload):
        seen.append(idempotency_key)
        return idempotency_key != "waker:keep-out"

    first = WakeEvent(
        source="codexbar", kind="quota_low", idempotency_key="waker:one", provider="glm"
    )
    duplicate = WakeEvent(
        source="codexbar", kind="quota_low", idempotency_key="waker:one", provider="glm"
    )
    rejected = WakeEvent(
        source="codexbar",
        kind="quota_low",
        idempotency_key="waker:keep-out",
        provider="glm",
    )
    accepted = await enqueue_wake_events(
        [first, duplicate, rejected], user_id="user-1", enqueue=sink
    )
    assert seen == ["waker:one", "waker:keep-out"]
    assert accepted == ["waker:one"]


@pytest.mark.asyncio
async def test_handler_parks_from_the_effect_key_and_refuses_an_unknown_source():
    store = _Store([WorkItem(target_id="t1", provider="glm", status="queued")])
    actions = await apply_waker_effect(
        _effect("codexbar", "quota_exhausted", key="waker:from-effect"),
        store=store,
        codexbar_enabled=True,
        max_retries=3,
    )
    assert store.loaded[0].idempotency_key == "waker:from-effect"
    assert actions[0].action == "park"
    assert store.saved[0].ledger["last_event_key"] == "waker:from-effect"

    with pytest.raises(PermanentEffectError, match="unknown waker source"):
        await apply_waker_effect(_effect("mailbox", "ci_failure"), store=store)


@pytest.mark.asyncio
async def test_a_disabled_waker_does_not_touch_the_store():
    store = _Store([WorkItem(target_id="t1", provider="glm", status="queued")])
    result = await apply_waker_effect(
        _effect("codexbar", "quota_exhausted"),
        store=store,
        codexbar_enabled=False,
    )
    assert result == []
    assert store.loaded == []
    assert store.saved == []


@pytest.mark.asyncio
async def test_the_scheduler_pass_registers_the_wake_handler():
    runner = AutonomyEffectRunner()
    register_waker_effect(runner)
    register_waker_effect(runner)
    assert EFFECT_WAKE in runner.registered

    from src.server.services.autonomy.scheduler import AutonomyScheduler

    assert EFFECT_WAKE in AutonomyScheduler().runner.registered


@pytest.mark.asyncio
async def test_a_disabled_pass_does_no_polling():
    async def states():
        raise AssertionError("states")

    async def poll():
        raise AssertionError("poll")

    result = await run_waker_pass(
        load_states=states,
        poll_repobar=poll,
        codexbar_enabled=False,
        repobar_enabled=False,
    )
    assert result.enqueued == ()
    assert result.failures == ()


@pytest.mark.asyncio
async def test_one_waker_failing_in_the_pass_does_not_skip_the_other():
    async def states():
        raise RuntimeError(f"snapshot {_TOKEN}")

    event = WakeEvent(
        source="repobar",
        kind="pull_request",
        idempotency_key="waker:repo:1",
        repository="acme/app",
    )

    async def poll():
        return [event]

    accepted = []

    async def sink(*, user_id, idempotency_key, payload):
        accepted.append(idempotency_key)
        return True

    result = await run_waker_pass(
        load_states=states,
        poll_repobar=poll,
        enqueue=sink,
        codexbar_enabled=True,
        repobar_enabled=True,
    )
    assert result.enqueued == ("waker:repo:1",)
    assert result.failures[0].waker == "codexbar"
    assert _TOKEN not in result.failures[0].error
    assert accepted == ["waker:repo:1"]


@pytest.mark.asyncio
async def test_codexbar_pass_enqueues_a_transition_once():
    async def states():
        return {"zai": "exhausted"}

    async def previous():
        return {}

    async def exhausted():
        return set()

    keys = []

    async def sink(*, user_id, idempotency_key, payload):
        keys.append((idempotency_key, payload.get("provider"), payload.get("kind")))
        return True

    result = await run_waker_pass(
        load_states=states,
        load_previous=previous,
        load_exhausted=exhausted,
        enqueue=sink,
        codexbar_enabled=True,
        repobar_enabled=False,
    )
    assert len(result.enqueued) == 1
    assert keys[0][1] == "glm"
    assert keys[0][2] == "quota_exhausted"
    assert keys[0][0].startswith("waker:codexbar:quota_exhausted:glm:")


@pytest.mark.asyncio
async def test_waker_failure_log_does_not_include_the_traceback(caplog):
    async def states():
        raise RuntimeError(f"snapshot {_TOKEN}")

    async def sink(*, user_id, idempotency_key, payload):
        return True

    with caplog.at_level(
        logging.ERROR, logger="src.server.services.autonomy.wakers"
    ):
        result = await run_waker_pass(
            load_states=states,
            enqueue=sink,
            codexbar_enabled=True,
            repobar_enabled=False,
        )
    assert result.failures[0].waker == "codexbar"
    assert _TOKEN not in caplog.text
    assert "Traceback" not in caplog.text
    assert "waker failed" in caplog.text


@pytest.mark.asyncio
async def test_codexbar_effect_does_not_query_repository_configuration(monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("repository configuration")

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.source_store.fix_ci_repositories", boom
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.repobar.load_repo_bindings", boom
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_codexbar_enabled", lambda: True
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_repobar_enabled", lambda: True
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_max_quota_retries", lambda: 3
    )
    seen = {}

    async def apply(effect, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.apply_waker_effect", apply
    )
    from src.server.services.autonomy.wakers.runtime import waker_effect

    await waker_effect(_effect("codexbar", "quota_reset"))
    assert seen["fix_ci_repos"] is None
    assert seen["bindings"] is None

    calls = []

    async def repos(user_id):
        calls.append(("repos", user_id))
        return {"acme/app"}

    async def bindings(user_id=""):
        calls.append(("bindings", user_id))
        return []

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.source_store.fix_ci_repositories", repos
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.repobar.load_repo_bindings", bindings
    )
    await waker_effect(_effect("repobar", "ci_failure", repository="acme/app"))
    assert calls == [("repos", "user-1"), ("bindings", "user-1")]

    calls.clear()
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.settings.get_autonomy_waker_repobar_enabled", lambda: False
    )
    await waker_effect(_effect("repobar", "ci_failure", repository="acme/app"))
    assert calls == []


@pytest.mark.asyncio
async def test_a_full_quota_page_enqueues_the_next_page(monkeypatch):
    ids = [str(UUID(int=index)) for index in range(1, 1002)]
    rows = [
        {
            "ticket_id": ticket_id,
            "user_id": "user-1",
            "status": "queued",
            "mandate": {},
            "attention": None,
            "worker_provider": "glm",
            "line_id": "",
            "revision": 0,
        }
        for ticket_id in ids
    ]
    cursors = []

    async def list_items(*args, **kwargs):
        cursors.append(kwargs.get("after_ticket_id"))
        if kwargs.get("after_ticket_id"):
            return rows[1000:]
        return rows

    async def horizon(provider):
        return ""

    async def write(*args, **kwargs):
        return True

    enqueued = []

    async def enqueue(*, user_id, idempotency_key, payload):
        enqueued.append((idempotency_key, payload.get("quota_after_ticket_id")))
        return True

    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.list_quota_work_items", list_items
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_history.latest_quota_observed_at", horizon
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.quota_store.apply_quota_schedule", write
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.wakers.runtime.enqueue_waker_effect", enqueue
    )
    actions = await apply_waker_effect(
        _effect("codexbar", "quota_exhausted", key="waker:page", observed_at="2026-11-10T00:00:00+00:00"),
        codexbar_enabled=True,
    )
    parked = [action for action in actions if action.action == "park"]
    assert len(parked) == 1000
    assert enqueued
    assert enqueued[0][1] == ids[999]
    assert enqueued[0][0].endswith(f":after:{ids[999]}")
    follow = _effect(
        "codexbar",
        "quota_exhausted",
        key=enqueued[0][0],
        observed_at="2026-11-10T00:00:00+00:00",
        quota_after_ticket_id=enqueued[0][1],
    )
    await apply_waker_effect(follow, codexbar_enabled=True)
    assert cursors[1] == ids[999]
