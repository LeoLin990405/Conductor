"""Lease-based execution of durable autonomy effects.

The database layer owns claiming and settlement. This module owns only the
process-local execution context: which handlers are registered, a bounded
number of concurrent rows, and the stop flag. A restart loses nothing because
the next worker claims the same rows after their leases expire.

A claim spends one of a row's few attempts and starts its lease clock, so the
runner claims a row only when it can start it now: each pass takes at most as
many rows as it has free execution slots. A row leased while every slot is
busy would wait with its lease running (a launch holds its slot for a whole
Worker run), and each lease that expired before a slot freed would cost an
attempt until the row was parked dead without its handler having run.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

from src.server.database.autonomy import effects as effects_db
from src.server.services.autonomy.models import EffectRecord

logger = logging.getLogger(__name__)

EffectHandler = Callable[[EffectRecord], Awaitable[None]]

# How long ``stop`` waits for the pass loop, and then for handlers still running.
_STOP_GRACE_SECONDS = 10.0


class PermanentEffectError(Exception):
    """An effect cannot succeed on retry and should be dead-lettered now."""


class DeferEffect(Exception):
    """The effect is not due yet; run it again later without spending an attempt.

    Not a failure, so it neither nacks nor dead-letters: the same row goes back
    to pending, due after ``delay_seconds``, with the claim's attempt returned.
    """

    def __init__(self, delay_seconds: float) -> None:
        super().__init__(f"deferred {delay_seconds:g}s")
        self.delay_seconds = float(delay_seconds)


class EffectStore(Protocol):
    """The slice of the effects DB the runner needs (faked in unit tests)."""

    async def park_exhausted_effects(self, *, max_attempts: int) -> list[dict]: ...

    async def claim_due_effects(
        self, *, limit: int, lease_seconds: float, max_attempts: int
    ) -> list[dict]: ...

    async def ack_effect(self, *, effect_id: str, attempts: int) -> bool: ...

    async def nack_effect(
        self,
        *,
        effect_id: str,
        attempts: int,
        error: str,
        retry_delay_seconds: float,
        max_attempts: int,
    ) -> bool: ...

    async def dead_letter_effect(
        self, *, effect_id: str, attempts: int, error: str
    ) -> bool: ...

    async def defer_effect(
        self, *, effect_id: str, attempts: int, delay_seconds: float
    ) -> bool: ...


class DbEffectStore:
    """Production adapter over the raw-SQL module."""

    park_exhausted_effects = staticmethod(effects_db.park_exhausted_effects)
    claim_due_effects = staticmethod(effects_db.claim_due_effects)
    ack_effect = staticmethod(effects_db.ack_effect)
    nack_effect = staticmethod(effects_db.nack_effect)
    dead_letter_effect = staticmethod(effects_db.dead_letter_effect)
    defer_effect = staticmethod(effects_db.defer_effect)


def _record(row: dict) -> EffectRecord:
    return EffectRecord(
        effect_id=str(row["effect_id"]),
        user_id=str(row.get("user_id") or ""),
        kind=str(row["kind"]),
        idempotency_key=str(row.get("idempotency_key") or ""),
        payload=dict(row.get("payload") or {}),
        attempts=int(row.get("attempts") or 0),
        due_at=row.get("due_at"),
        lease_until=row.get("lease_until"),
    )


class AutonomyEffectRunner:
    """Claim and execute due effects for one backend worker."""

    def __init__(
        self,
        *,
        store: EffectStore | None = None,
        lease_seconds: float = effects_db.DEFAULT_LEASE_SECONDS,
        max_attempts: int = effects_db.DEFAULT_MAX_ATTEMPTS,
        batch_size: int = 20,
        concurrency: int = 5,
        poll_seconds: float = 1.0,
    ) -> None:
        self._store = store or DbEffectStore()
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._batch_size = max(1, batch_size)
        self._concurrency = max(1, concurrency)
        # Claiming by free slots keeps a claimed row from ever waiting here; the
        # semaphore is the hard bound for anything that reaches ``_execute``
        # without a claim pass, not the queue.
        self._semaphore = asyncio.Semaphore(self._concurrency)
        self._poll_seconds = max(0.1, poll_seconds)
        self._handlers: dict[str, EffectHandler] = {}
        self._stopping = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        # Free slots are counted and spent under this lock, so two passes cannot
        # both spend the same slot.
        self._claim_lock = asyncio.Lock()
        # Set when a task ends (or the runner stops), so a pass that left due
        # rows unclaimed for want of a slot runs again at once.
        self._wake = asyncio.Event()
        self._left_due = False
        # Whether ``stop`` has already waited out its grace on the handlers still
        # running; reset by ``start``.
        self._grace_spent = False

    def register(self, kind: str, handler: EffectHandler) -> None:
        """Bind one effect kind. A second binding for the same kind is a bug."""
        if kind in self._handlers and self._handlers[kind] is not handler:
            raise ValueError(f"effect kind {kind!r} already has a handler")
        self._handlers[kind] = handler

    @property
    def registered(self) -> frozenset[str]:
        return frozenset(self._handlers)

    async def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._stopping.clear()
            self._grace_spent = False
            self._loop_task = asyncio.create_task(
                self._run_forever(), name="autonomy-effect-runner"
            )

    async def stop(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=_STOP_GRACE_SECONDS)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._loop_task.cancel()
            self._loop_task = None
        inflight = [task for task in self._inflight if not task.done()]
        if inflight and not self._grace_spent:
            self._grace_spent = True
            try:
                await asyncio.wait(inflight, timeout=_STOP_GRACE_SECONDS)
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        # A handler that outlives the grace is neither cancelled nor forgotten:
        # cancelling a launch settles its Ticket failed, and it still holds its
        # place, which ``_free_slots`` reads from this set. Only a task's own
        # completion removes it, so a restart cannot lease for a place a
        # survivor still has.

    def _free_slots(self) -> int:
        # Every task made by a pass is here from its creation until it ends, so
        # this counts the places held, plus those about to be taken. A finished
        # task may sit here until its callback runs, and is not counted.
        busy = sum(1 for task in self._inflight if not task.done())
        return max(0, self._concurrency - busy)

    def _on_settled(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        self._wake.set()

    async def run_once(self) -> int:
        """Run one claim/execute pass; returns how many rows were leased.

        Leases at most as many rows as there are free execution slots (and no
        more than ``batch_size``), so every row it leases starts at once; the
        rest stay unleased for a pass that has room, or for another worker.
        """
        await self._store.park_exhausted_effects(max_attempts=self._max_attempts)
        async with self._claim_lock:
            # Cleared before the count, so a task that ends after it wakes the
            # loop instead of being forgotten.
            self._wake.clear()
            limit = min(self._batch_size, self._free_slots())
            if limit <= 0:
                self._left_due = True
                return 0
            rows = await self._store.claim_due_effects(
                limit=limit,
                lease_seconds=self._lease_seconds,
                max_attempts=self._max_attempts,
            )
            # A full page means more may be due; fewer means the queue is dry.
            self._left_due = len(rows) >= limit
            if not rows:
                return 0
            tasks = [
                asyncio.create_task(self._execute(_record(row)), name="autonomy-effect")
                for row in rows
            ]
            self._inflight.update(tasks)
            for task in tasks:
                task.add_done_callback(self._on_settled)
            return len(tasks)

    async def _run_forever(self) -> None:
        while not self._stopping.is_set():
            failed = False
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the drainer alive
                logger.exception("[autonomy] effect pass failed")
                failed = True
            # With due rows left over, the next pass is worth running as soon as
            # a slot frees; otherwise nothing is waiting and the poll interval
            # is the cadence. A failed pass always waits the interval: it may
            # have failed before it cleared ``_wake``, which a finished task left
            # set, and retrying on that would spin. ``stop`` sets both events,
            # but a pass clears ``_wake``, so a stop already requested is
            # checked here too.
            leftover = self._left_due and not failed and not self._stopping.is_set()
            wake = self._wake if leftover else self._stopping
            try:
                await asyncio.wait_for(wake.wait(), timeout=self._poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def wait_idle(self) -> None:
        """Wait for every effect this runner has claimed to settle.

        Tests and graceful tests need this; the production loop keeps running
        and never calls it. A handler may enqueue a new effect, but that row is
        for the next claim pass, not this set of in-flight tasks.
        """
        while self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    async def _execute(self, effect: EffectRecord) -> None:
        async with self._semaphore:
            handler = self._handlers.get(effect.kind)
            if handler is None:
                await self._store.nack_effect(
                    effect_id=effect.effect_id,
                    attempts=effect.attempts,
                    error=f"no handler registered for effect kind {effect.kind!r}",
                    retry_delay_seconds=effects_db.backoff_seconds(effect.attempts),
                    max_attempts=self._max_attempts,
                )
                return
            try:
                await handler(effect)
            except asyncio.CancelledError:
                raise
            except DeferEffect as exc:
                await self._store.defer_effect(
                    effect_id=effect.effect_id,
                    attempts=effect.attempts,
                    delay_seconds=exc.delay_seconds,
                )
                return
            except PermanentEffectError as exc:
                logger.info(
                    "[autonomy] effect %s (%s) dead-lettered: %s",
                    effect.effect_id,
                    effect.kind,
                    exc,
                )
                await self._store.dead_letter_effect(
                    effect_id=effect.effect_id,
                    attempts=effect.attempts,
                    error=str(exc),
                )
                return
            except Exception as exc:  # noqa: BLE001 - handler failure is retry state
                logger.warning(
                    "[autonomy] effect %s (%s) failed: %s",
                    effect.effect_id,
                    effect.kind,
                    exc,
                )
                await self._store.nack_effect(
                    effect_id=effect.effect_id,
                    attempts=effect.attempts,
                    error=str(exc),
                    retry_delay_seconds=effects_db.backoff_seconds(effect.attempts),
                    max_attempts=self._max_attempts,
                )
                return
            await self._store.ack_effect(
                effect_id=effect.effect_id, attempts=effect.attempts
            )
