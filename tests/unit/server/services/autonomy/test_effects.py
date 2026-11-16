"""Autonomy effect runner: claim, retry, idempotent settle, no in-memory truth."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.server.services.autonomy.effects import AutonomyEffectRunner


class FakeEffectStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.acked: list[str] = []
        self.nacked: list[tuple[str, str, int]] = []
        self.dead: list[str] = []
        self.dead_lettered: list[tuple[str, str, int]] = []
        self.deferred: list[tuple[str, int, float]] = []

    def add(self, effect_id: str, kind: str, attempts: int = 1) -> None:
        self.rows.append(
            {
                "effect_id": effect_id,
                "user_id": "u1",
                "kind": kind,
                "idempotency_key": f"key:{effect_id}",
                "payload": {},
                "attempts": attempts,
            }
        )

    async def park_exhausted_effects(self, *, max_attempts: int) -> list[dict]:
        return []

    async def claim_due_effects(
        self, *, limit: int, lease_seconds: float, max_attempts: int
    ) -> list[dict]:
        claimed, self.rows = self.rows[:limit], self.rows[limit:]
        return claimed

    async def ack_effect(self, *, effect_id: str, attempts: int) -> bool:
        self.acked.append(effect_id)
        return True

    async def nack_effect(
        self,
        *,
        effect_id: str,
        attempts: int,
        error: str,
        retry_delay_seconds: float,
        max_attempts: int,
    ) -> bool:
        self.nacked.append((effect_id, error, attempts))
        return True

    async def dead_letter_effect(
        self, *, effect_id: str, attempts: int, error: str
    ) -> bool:
        self.dead_lettered.append((effect_id, error, attempts))
        return True


    async def defer_effect(
        self, *, effect_id: str, attempts: int, delay_seconds: float
    ) -> bool:
        self.deferred.append((effect_id, attempts, delay_seconds))
        return True


@pytest.mark.asyncio
async def test_successful_handler_is_acked_once():
    store = FakeEffectStore()
    store.add("e1", "reply")
    seen: list[str] = []

    async def handler(effect):
        seen.append(effect.effect_id)

    runner = AutonomyEffectRunner(store=store)
    runner.register("reply", handler)
    assert await runner.run_once() == 1
    await runner.wait_idle()
    assert seen == ["e1"]
    assert store.acked == ["e1"]
    assert store.nacked == []


@pytest.mark.asyncio
async def test_failed_handler_nacks_with_backoff_and_attempt_fence():
    store = FakeEffectStore()
    store.add("e1", "reply")

    async def handler(effect):
        raise RuntimeError("github 502")

    runner = AutonomyEffectRunner(store=store)
    runner.register("reply", handler)
    await runner.run_once()
    await runner.wait_idle()
    assert store.acked == []
    assert store.nacked == [("e1", "github 502", 1)]


@pytest.mark.asyncio
async def test_unknown_kind_is_nacked_not_silently_dropped():
    store = FakeEffectStore()
    store.add("e1", "future_kind")
    runner = AutonomyEffectRunner(store=store)
    await runner.run_once()
    await runner.wait_idle()
    assert store.acked == []
    assert store.nacked[0][0] == "e1"
    assert "no handler" in store.nacked[0][1]


@pytest.mark.asyncio
async def test_permanent_handler_error_is_dead_lettered_without_retry():
    from src.server.services.autonomy.effects import PermanentEffectError

    store = FakeEffectStore()
    store.add("e1", "reply")

    async def handler(effect):
        raise PermanentEffectError("source deleted")

    runner = AutonomyEffectRunner(store=store)
    runner.register("reply", handler)
    await runner.run_once()
    await runner.wait_idle()
    assert store.acked == []
    assert store.nacked == []
    assert store.dead_lettered == [("e1", "source deleted", 1)]


@pytest.mark.asyncio
async def test_a_deferred_effect_is_put_back_not_acked_nacked_or_dead_lettered():
    """Not due yet is not a failure: the row waits, and no attempt is spent."""
    from src.server.services.autonomy.effects import DeferEffect

    store = FakeEffectStore()
    store.add("e1", "launch_worker", attempts=2)

    async def handler(effect):
        raise DeferEffect(7)

    runner = AutonomyEffectRunner(store=store)
    runner.register("launch_worker", handler)
    await runner.run_once()
    await runner.wait_idle()
    assert store.deferred == [("e1", 2, 7.0)]
    assert store.acked == [] and store.nacked == [] and store.dead_lettered == []


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_does_not_lose_an_inflight_handler():
    store = FakeEffectStore()
    store.add("e1", "reply")
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(effect):
        started.set()
        await release.wait()

    runner = AutonomyEffectRunner(store=store)
    runner.register("reply", handler)
    await runner.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    release.set()
    await runner.stop()
    await runner.stop()
    assert store.acked == ["e1"]
