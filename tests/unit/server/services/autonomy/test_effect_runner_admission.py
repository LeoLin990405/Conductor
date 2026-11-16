"""The runner leases only the effects it can start, so waiting never spends attempts.

A claim spends one of the row's few attempts and starts its lease clock. A row
claimed while every execution slot is busy would sit at the runner's semaphore
with that clock running, and a long-running launch holds its slot for its whole
Worker run, so the row's lease would expire, be claimed again and expire again
until its attempts were gone and it was parked dead without its handler ever
running. These tests drive the production runner against an in-memory lease
store that keeps the queue's rules (attempt count, lease expiry, the attempt
ceiling, the fence on settlement) so that path is exercised without a database.
The same rules on real rows are in ``tests/integration``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.server.services.autonomy import effects as effects_module
from src.server.services.autonomy.effects import (
    AutonomyEffectRunner,
    DeferEffect,
)

LEASE_SECONDS = 60.0
MAX_ATTEMPTS = 6
SLOTS = 5


class LeaseStore:
    """The queue's lease, attempt and fence rules over a clock the test moves.

    Mirrors ``database/autonomy/effects.py``: a claim takes a pending row that
    is due or a claimed row whose lease ran out and that still has attempts,
    oldest first, adds one attempt and leases it; a row whose lease ran out at
    the attempt ceiling is parked dead; every settlement is fenced on the
    attempt count the claim returned.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.rows: dict[str, dict[str, Any]] = {}
        self.claim_limits: list[int] = []

    def add(self, effect_id: str, kind: str) -> None:
        self.rows[effect_id] = {
            "effect_id": effect_id,
            "user_id": "u1",
            "kind": kind,
            "idempotency_key": f"key:{effect_id}",
            "payload": {},
            "status": "pending",
            "attempts": 0,
            "due_at": self.now,
            "lease_until": None,
            "seq": len(self.rows),
        }

    def status(self, effect_id: str) -> str:
        return str(self.rows[effect_id]["status"])

    def attempts(self, effect_id: str) -> int:
        return int(self.rows[effect_id]["attempts"])

    async def park_exhausted_effects(self, *, max_attempts: int) -> list[dict]:
        parked = []
        for row in self.rows.values():
            if (
                row["status"] == "claimed"
                and row["lease_until"] <= self.now
                and row["attempts"] >= max_attempts
            ):
                row["status"] = "dead"
                row["lease_until"] = None
                parked.append(dict(row))
        return parked

    async def claim_due_effects(
        self, *, limit: int, lease_seconds: float, max_attempts: int
    ) -> list[dict]:
        self.claim_limits.append(limit)
        due = [
            row
            for row in self.rows.values()
            if (row["status"] == "pending" and row["due_at"] <= self.now)
            or (
                row["status"] == "claimed"
                and row["lease_until"] <= self.now
                and row["attempts"] < max_attempts
            )
        ]
        due.sort(key=lambda row: (row["due_at"], row["seq"]))
        claimed = []
        for row in due[:limit]:
            row["status"] = "claimed"
            row["attempts"] += 1
            row["lease_until"] = self.now + lease_seconds
            claimed.append(dict(row))
        return claimed

    def _fenced(self, effect_id: str, attempts: int) -> dict[str, Any] | None:
        row = self.rows[effect_id]
        if row["status"] == "claimed" and row["attempts"] == attempts:
            return row
        return None

    async def extend(self, effect_id: str, attempts: int) -> bool:
        row = self._fenced(effect_id, attempts)
        if row is None:
            return False
        row["lease_until"] = self.now + LEASE_SECONDS
        return True

    async def ack_effect(self, *, effect_id: str, attempts: int) -> bool:
        row = self._fenced(effect_id, attempts)
        if row is None:
            return False
        row["status"], row["lease_until"] = "done", None
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
        row = self._fenced(effect_id, attempts)
        if row is None:
            return False
        row["status"] = "dead" if attempts >= max_attempts else "pending"
        row["lease_until"] = None
        row["due_at"] = self.now + retry_delay_seconds
        return True

    async def dead_letter_effect(
        self, *, effect_id: str, attempts: int, error: str
    ) -> bool:
        row = self._fenced(effect_id, attempts)
        if row is None:
            return False
        row["status"], row["lease_until"] = "dead", None
        return True

    async def defer_effect(
        self, *, effect_id: str, attempts: int, delay_seconds: float
    ) -> bool:
        row = self._fenced(effect_id, attempts)
        if row is None:
            return False
        row["status"], row["lease_until"] = "pending", None
        row["attempts"] = max(row["attempts"] - 1, 0)
        row["due_at"] = self.now + delay_seconds
        return True


class Workers:
    """A pool of ``SLOTS`` Worker slots, the way the cap is, shared by launches.

    A launch that finds a slot takes it and stays until released, renewing its
    own lease the way the real launch does; one that finds none defers.
    """

    def __init__(self, store: LeaseStore) -> None:
        self.store = store
        self.release = asyncio.Event()
        self.running: dict[str, int] = {}
        self.reached: list[str] = []

    async def launch(self, effect) -> None:
        self.reached.append(effect.effect_id)
        if len(self.running) >= SLOTS:
            raise DeferEffect(10)
        self.running[effect.effect_id] = effect.attempts
        try:
            await self.release.wait()
        finally:
            self.running.pop(effect.effect_id, None)

    async def renew(self) -> None:
        for effect_id, attempts in list(self.running.items()):
            assert await self.store.extend(effect_id, attempts)


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _pass(
    store: LeaseStore, runner: AutonomyEffectRunner, workers: Workers, *, seconds: float
) -> int:
    """One runner pass ``seconds`` later, after the running Workers renewed."""
    store.now += seconds
    await workers.renew()
    claimed = await runner.run_once()
    await _settle()
    return claimed


def _runner(store: LeaseStore) -> AutonomyEffectRunner:
    return AutonomyEffectRunner(
        store=store,
        lease_seconds=LEASE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        concurrency=SLOTS,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["launch_worker", "reply"])
async def test_an_effect_waiting_for_a_busy_runner_is_never_parked_dead(kind):
    """Five long launches fill the slots; a sixth effect must keep its attempts.

    Reproduces the review finding: the sixth effect was leased on the first pass,
    waited at the semaphore, and every lease expiry spent an attempt until it was
    dead without its handler having run.
    """
    store = LeaseStore()
    workers = Workers(store)
    runner = _runner(store)
    runner.register("launch_worker", workers.launch)
    runner.register("reply", workers.launch)
    for index in range(SLOTS):
        store.add(f"long{index}", "launch_worker")
    store.add("sixth", kind)

    try:
        for _ in range(MAX_ATTEMPTS + 4):
            await _pass(store, runner, workers, seconds=LEASE_SECONDS + 1)

        assert sorted(workers.running) == [f"long{i}" for i in range(SLOTS)]
        assert store.status("sixth") == "pending"
        assert store.attempts("sixth") == 0
        assert "sixth" not in workers.reached

        # The Workers end and free their slots: the waiting effect is leased
        # once, on its first claim, and its handler runs.
        workers.release.set()
        await _settle()
        workers.release = asyncio.Event()
        await _pass(store, runner, workers, seconds=1)

        assert "sixth" in workers.reached
        assert store.status("sixth") == "claimed"
        assert store.attempts("sixth") == 1
    finally:
        workers.release.set()
        await runner.wait_idle()


@pytest.mark.asyncio
async def test_a_pass_leases_no_more_than_the_execution_slots_it_has_free():
    store = LeaseStore()
    workers = Workers(store)
    runner = _runner(store)
    runner.register("launch_worker", workers.launch)
    for index in range(12):
        store.add(f"e{index}", "launch_worker")

    try:
        assert await runner.run_once() == SLOTS
        await _settle()
        assert store.claim_limits == [SLOTS]

        # Every slot is busy: the pass leases nothing and does not even ask.
        assert await runner.run_once() == 0
        assert store.claim_limits == [SLOTS]
        assert sum(row["status"] == "pending" for row in store.rows.values()) == 7
    finally:
        workers.release.set()
        await runner.wait_idle()


@pytest.mark.asyncio
async def test_a_settled_effect_frees_its_slot_for_the_next_pass():
    store = LeaseStore()
    runner = AutonomyEffectRunner(
        store=store,
        lease_seconds=LEASE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        concurrency=2,
    )
    gates = {name: asyncio.Event() for name in ("e0", "e1")}
    seen: list[str] = []

    async def handler(effect):
        seen.append(effect.effect_id)
        gate = gates.get(effect.effect_id)
        if gate is not None:
            await gate.wait()

    runner.register("reply", handler)
    for name in ("e0", "e1", "e2", "e3"):
        store.add(name, "reply")

    assert await runner.run_once() == 2
    await _settle()
    assert await runner.run_once() == 0

    gates["e0"].set()
    await _settle()
    assert await runner.run_once() == 1
    gates["e1"].set()
    await runner.wait_idle()
    assert await runner.run_once() == 1
    await runner.wait_idle()

    assert seen == ["e0", "e1", "e2", "e3"]
    assert all(store.status(name) == "done" for name in ("e0", "e1", "e2", "e3"))
    assert all(store.attempts(name) == 1 for name in ("e0", "e1", "e2", "e3"))


@pytest.mark.asyncio
async def test_a_freed_slot_is_refilled_without_waiting_out_the_poll_interval():
    """Leasing by capacity must not cap throughput at one batch per poll."""
    store = LeaseStore()
    runner = AutonomyEffectRunner(
        store=store,
        lease_seconds=LEASE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        concurrency=2,
        poll_seconds=30.0,
    )
    seen: list[str] = []

    async def handler(effect):
        seen.append(effect.effect_id)
        await asyncio.sleep(0.01)

    runner.register("reply", handler)
    for index in range(9):
        store.add(f"e{index}", "reply")

    await runner.start()
    try:

        async def drained() -> None:
            while len(seen) < 9 or runner._inflight:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(drained(), timeout=5)
    finally:
        await runner.stop()
    assert sorted(seen) == [f"e{index}" for index in range(9)]
    assert all(row["status"] == "done" for row in store.rows.values())


@pytest.mark.asyncio
async def test_an_idle_runner_still_waits_out_the_poll_interval():
    """With nothing due, the loop polls once per interval, not once per wake."""
    store = LeaseStore()
    runner = AutonomyEffectRunner(
        store=store,
        lease_seconds=LEASE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        poll_seconds=30.0,
    )
    await runner.start()
    try:
        await asyncio.sleep(0.2)
    finally:
        await runner.stop()
    assert len(store.claim_limits) == 1


async def _until(predicate, *, seconds: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out waiting"
        await asyncio.sleep(0.002)


def _quick_runner(store: LeaseStore, *, concurrency: int) -> AutonomyEffectRunner:
    """A runner whose poll interval is long enough that only a wake or a stop can
    bring the loop back within a test's few milliseconds."""
    return AutonomyEffectRunner(
        store=store,
        lease_seconds=LEASE_SECONDS,
        max_attempts=MAX_ATTEMPTS,
        concurrency=concurrency,
        poll_seconds=30.0,
    )


async def _noop(effect) -> None:
    return None


@pytest.mark.asyncio
async def test_a_failed_pass_backs_off_instead_of_spinning_on_a_wake_left_set():
    """After a full batch settles, the wake is set and rows may remain. If the
    next pass then fails before it clears the wake, the loop must wait out the
    poll interval, not retry at once with no yield in between."""
    store = LeaseStore()
    runner = _quick_runner(store, concurrency=2)
    runner.register("reply", _noop)
    for name in ("e0", "e1"):
        store.add(name, "reply")
    park_calls = 0
    database_gone = False
    park = store.park_exhausted_effects
    claim = store.claim_due_effects

    async def failing_park(*, max_attempts: int) -> list[dict]:
        nonlocal park_calls
        park_calls += 1
        if park_calls >= 200:  # a runaway loop ends the test instead of hanging it
            runner._stopping.set()
        if database_gone:
            raise RuntimeError("database unavailable")
        return await park(max_attempts=max_attempts)

    async def claim_then_lose_the_database(**kwargs) -> list[dict]:
        nonlocal database_gone
        rows = await claim(**kwargs)
        database_gone = True  # right after a full batch
        return rows

    store.park_exhausted_effects = failing_park
    store.claim_due_effects = claim_then_lose_the_database

    await runner.start()
    await asyncio.sleep(0.05)
    passes_in_the_window = park_calls
    await asyncio.wait_for(runner.stop(), timeout=2)

    # The pass that leased the batch, and one that failed; then the backoff.
    assert passes_in_the_window == 2
    assert all(store.status(name) == "done" for name in ("e0", "e1"))


@pytest.mark.asyncio
async def test_a_failing_claim_does_not_spin_either():
    store = LeaseStore()
    runner = _quick_runner(store, concurrency=1)
    runner.register("reply", _noop)
    store.add("e0", "reply")
    claims = 0
    claim = store.claim_due_effects

    async def claim_then_fail(**kwargs) -> list[dict]:
        nonlocal claims
        claims += 1
        if claims >= 200:
            runner._stopping.set()
        if claims > 1:
            raise RuntimeError("database unavailable")
        return await claim(**kwargs)

    store.claim_due_effects = claim_then_fail
    await runner.start()
    await asyncio.sleep(0.05)
    passes_in_the_window = claims
    await asyncio.wait_for(runner.stop(), timeout=2)

    assert passes_in_the_window == 2


@pytest.mark.asyncio
async def test_a_stop_requested_during_a_pass_is_not_lost_when_the_pass_clears_the_wake(
    monkeypatch,
):
    """``stop`` sets the wake, but a pass in progress clears it. The loop must
    still see the stop, instead of waiting out the poll interval for it."""
    monkeypatch.setattr(effects_module, "_STOP_GRACE_SECONDS", 0.05)
    store = LeaseStore()
    runner = _quick_runner(store, concurrency=1)
    hold = asyncio.Event()

    async def handler(effect) -> None:
        if effect.effect_id == "e1":
            await hold.wait()  # no task ends after the stop to wake the loop

    runner.register("reply", handler)
    for name in ("e0", "e1"):
        store.add(name, "reply")
    in_second_pass = asyncio.Event()
    carry_on = asyncio.Event()
    park_calls = 0
    park = store.park_exhausted_effects

    async def slow_second_park(*, max_attempts: int) -> list[dict]:
        nonlocal park_calls
        park_calls += 1
        if park_calls == 2:
            in_second_pass.set()
            await carry_on.wait()
        return await park(max_attempts=max_attempts)

    store.park_exhausted_effects = slow_second_park
    await runner.start()
    await asyncio.wait_for(in_second_pass.wait(), timeout=2)

    loop_task = runner._loop_task
    stopping = asyncio.create_task(runner.stop())
    await asyncio.sleep(0)
    carry_on.set()
    # The second pass leases e1, a full page again, so more rows may be due. The
    # loop must end on its own account, not by being cancelled once ``stop``
    # has waited out its grace.
    await _until(lambda: loop_task.done() and not loop_task.cancelled())

    assert park_calls == 2
    assert store.status("e1") == "claimed"
    await asyncio.wait_for(stopping, timeout=2)
    hold.set()
    await runner.wait_idle()


@pytest.mark.asyncio
async def test_a_restart_whose_first_pass_fails_does_not_spin_on_what_the_stop_left(
    monkeypatch,
):
    """A stop leaves the wake set and, when its last pass leased a full page,
    ``_left_due`` too. A restart whose first pass fails must back off, not retry
    on that stale wake."""
    monkeypatch.setattr(effects_module, "_STOP_GRACE_SECONDS", 0.05)
    store = LeaseStore()
    runner = _quick_runner(store, concurrency=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(effect) -> None:
        started.set()
        await release.wait()

    runner.register("reply", handler)
    store.add("e0", "reply")
    await runner.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await runner.stop()  # the pass that leased e0 was a full page
    release.set()
    await runner.wait_idle()

    park_calls = 0

    async def failing_park(*, max_attempts: int) -> list[dict]:
        nonlocal park_calls
        park_calls += 1
        if park_calls >= 200:
            runner._stopping.set()
        raise RuntimeError("database unavailable")

    store.park_exhausted_effects = failing_park
    await runner.start()
    await asyncio.sleep(0.05)
    passes_in_the_window = park_calls
    await asyncio.wait_for(runner.stop(), timeout=2)

    assert passes_in_the_window == 1


@pytest.mark.asyncio
async def test_a_handler_that_outlived_a_stop_still_holds_its_place_after_a_restart(
    monkeypatch,
):
    """``stop`` gives running handlers a grace period and then leaves them
    running; the scheduler restarts the same runner when it retakes the pass
    lock. The survivor still holds a place, so the next effect must not be
    leased: it would wait for that place with its lease running."""
    monkeypatch.setattr(effects_module, "_STOP_GRACE_SECONDS", 0.05)
    store = LeaseStore()
    runner = _quick_runner(store, concurrency=1)
    started = asyncio.Event()
    release = asyncio.Event()
    reached: list[str] = []

    async def handler(effect) -> None:
        reached.append(effect.effect_id)
        if effect.effect_id == "survivor":
            started.set()
            await release.wait()

    runner.register("reply", handler)
    store.add("survivor", "reply")
    await runner.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await runner.stop()
    assert not release.is_set()

    store.add("next", "reply")
    await runner.start()
    await asyncio.sleep(0.02)

    assert store.status("next") == "pending"
    assert store.attempts("next") == 0
    assert reached == ["survivor"]

    # The survivor ends and its place frees: the effect is leased once and runs.
    release.set()
    await _until(lambda: store.status("next") == "done")
    assert store.attempts("next") == 1
    assert reached == ["survivor", "next"]
    await runner.stop()


@pytest.mark.asyncio
async def test_a_second_stop_does_not_wait_out_the_grace_again(monkeypatch):
    """The scheduler stops its runner from two places on shutdown; the survivors
    of the first have had their grace already."""
    monkeypatch.setattr(effects_module, "_STOP_GRACE_SECONDS", 0.05)
    store = LeaseStore()
    runner = _quick_runner(store, concurrency=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(effect) -> None:
        started.set()
        await release.wait()

    runner.register("reply", handler)
    store.add("survivor", "reply")
    await runner.start()
    await asyncio.wait_for(started.wait(), timeout=2)
    await runner.stop()

    monkeypatch.setattr(effects_module, "_STOP_GRACE_SECONDS", 30.0)
    await asyncio.wait_for(runner.stop(), timeout=2)

    release.set()
    await runner.wait_idle()
