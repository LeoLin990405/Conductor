"""Single-writer autonomy pass, safe across backend workers.

The pass (batching Evals, dispatching Tickets, recovering assignments) must have
one writer per deployment; effects are independently safe because their claim
uses ``FOR UPDATE SKIP LOCKED``. The advisory lock below is the pass's only
liveness truth: it is released by Postgres when a worker dies, and a second
worker simply retries until it can take over.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from src.server.database.autonomy.effects import enqueue_effect
from src.server.database.pool import get_db_connection
from src.server.services.autonomy.effects import (
    AutonomyEffectRunner,
    PermanentEffectError,
)
from src.server.services.autonomy.evals import (
    recover_stale_evals,
    route_eval_effect,
    start_due_evals,
)
from src.server.services.autonomy.executor import (
    ensure_launch_effects,
    poll_running_workers,
    sweep_worktree_cleanups,
)
from src.server.services.autonomy.inputs import deliver_input_effect
from src.server.services.autonomy.models import (
    EFFECT_DELIVER_INPUT,
    EFFECT_LAUNCH_WORKER,
    EFFECT_MAIN_AGENT_ADVANCE,
    EFFECT_POLL_SOURCE,
    EFFECT_RELAY_REPORT,
    EFFECT_REPLY,
    EFFECT_ROUTER_TURN,
    EFFECT_WATCHDOG_REVIEW,
)
from src.server.services.autonomy.poll import enqueue_due_polls, poll_source_effect
from src.server.services.autonomy.replies import relay_report_effect, reply_effect
from src.server.services.autonomy.schedules import run_due_schedules
from src.server.services.autonomy.watchdog import watchdog_sweep
from src.server.services.autonomy.watchdog_review import watchdog_review_effect
from src.server.services.autonomy.worker import launch_worker_effect
from src.server.services.main_agent.orchestrator import run_needs_reconciliation
from src.server.services.main_agent.production import SourceUnavailableError

logger = logging.getLogger(__name__)


async def _notify_operator() -> None:
    """Dead daemons, Tickets that need a person, failures, and quota.

    Each check de-duplicates and swallows its own scan errors. This wrapper
    is what keeps a programming error in one of them off the rest of the pass.
    """
    try:
        from src.server.services.autonomy.notifications import (
            check_dead_daemons,
            check_failures,
            check_needs_user,
            check_worker_quota,
        )

        await check_dead_daemons()
        await check_needs_user()
        await check_failures()
        await check_worker_quota()
    except Exception:  # noqa: BLE001 - operator alerts must not stall the pass
        logger.exception("[autonomy] operator notification checks failed")


PASS_INTERVAL_SECONDS = 2.0
LOCK_RETRY_SECONDS = 5.0
MAIN_AGENT_RECONCILE_LIMIT = 25
# Expired daemon live-output logs are collected here, not on the write path, so
# a backlog can neither slow nor fail a run's first batch.
OUTPUT_GC_INTERVAL_SECONDS = 600.0


class AutonomyScheduler:
    """Process-level execution context for the autonomy pass and effect runner."""

    _instance: "AutonomyScheduler | None" = None
    # time.monotonic() of the next output-log collection. A rate limiter for
    # this pass only: the pass lock makes it single-writer, and a new holder
    # simply starts with a collection.
    _output_gc_due: float = 0.0

    def __init__(
        self,
        runner: AutonomyEffectRunner | None = None,
        main_agent_orchestrator: Any | None = None,
        memory_store: Any | None = None,
    ) -> None:
        self.runner = runner or AutonomyEffectRunner()
        if EFFECT_POLL_SOURCE not in self.runner.registered:
            self.runner.register(EFFECT_POLL_SOURCE, poll_source_effect)
        if EFFECT_ROUTER_TURN not in self.runner.registered:
            self.runner.register(EFFECT_ROUTER_TURN, route_eval_effect)
        if EFFECT_LAUNCH_WORKER not in self.runner.registered:
            self.runner.register(EFFECT_LAUNCH_WORKER, launch_worker_effect)
        if EFFECT_RELAY_REPORT not in self.runner.registered:
            self.runner.register(EFFECT_RELAY_REPORT, relay_report_effect)
        if EFFECT_REPLY not in self.runner.registered:
            self.runner.register(EFFECT_REPLY, reply_effect)
        if EFFECT_DELIVER_INPUT not in self.runner.registered:
            self.runner.register(EFFECT_DELIVER_INPUT, deliver_input_effect)
        if EFFECT_WATCHDOG_REVIEW not in self.runner.registered:
            self.runner.register(EFFECT_WATCHDOG_REVIEW, watchdog_review_effect)
        if EFFECT_MAIN_AGENT_ADVANCE not in self.runner.registered:
            self.runner.register(
                EFFECT_MAIN_AGENT_ADVANCE, self._main_agent_advance_effect
            )
        from src.server.services.autonomy.wakers.runtime import register_waker_effect

        register_waker_effect(self.runner)
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._started_lock = asyncio.Lock()
        self.main_agent_orchestrator = main_agent_orchestrator
        self.memory_store = memory_store

    @classmethod
    def get_instance(
        cls,
        main_agent_orchestrator: Any | None = None,
        memory_store: Any | None = None,
    ) -> "AutonomyScheduler":
        if cls._instance is None:
            cls._instance = cls(
                main_agent_orchestrator=main_agent_orchestrator,
                memory_store=memory_store,
            )
        elif main_agent_orchestrator is not None:
            cls._instance.main_agent_orchestrator = main_agent_orchestrator
        if memory_store is not None:
            cls._instance.memory_store = memory_store
        return cls._instance

    async def start(self) -> None:
        async with self._started_lock:
            if self._task is not None and not self._task.done():
                return
            self._stopping.clear()
            self._task = asyncio.create_task(self._run(), name="autonomy-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            try:
                await asyncio.wait_for(task, timeout=15.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
        await self.runner.stop()

    async def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._run_locked()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a lost DB lock is retry state
                logger.exception("[autonomy] scheduler pass failed")
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=LOCK_RETRY_SECONDS
                )
            except asyncio.TimeoutError:
                pass

    async def _run_locked(self) -> None:
        """Hold the pass advisory lock, run the effect runner, and release it.

        The connection is held for the lock's lifetime on purpose: session
        advisory locks are tied to the physical connection, and a dropped
        connection must release the lock for another worker to take over.
        """
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_try_advisory_lock("
                    "hashtextextended('conductor-autonomy-pass', 0)) AS held"
                )
                row = await cur.fetchone()
            if not row or not row[0]:
                logger.debug("[autonomy] pass lock held by another worker")
                return
            try:
                await self.runner.start()
                while not self._stopping.is_set():
                    # The pass itself is added in the intake/ticket phases;
                    # keep the loop warm and the effect runner draining now.
                    await self._run_pass()
                    try:
                        await asyncio.wait_for(
                            self._stopping.wait(), timeout=PASS_INTERVAL_SECONDS
                        )
                    except asyncio.TimeoutError:
                        pass
            finally:
                await self.runner.stop()
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT pg_advisory_unlock("
                        "hashtextextended('conductor-autonomy-pass', 0))"
                    )
                    await cur.fetchone()

    async def _run_pass(self) -> None:
        """Advance durability work. Intake/ticket stages extend this method."""
        from src.server.database.autonomy.daemons import recover_expired_daemon_leases
        from src.server.database.autonomy.stops import expire_stop_requests

        # First, so a step below that raises on every pass cannot starve it: it
        # absorbs its own failures, the steps below do not.
        await self._collect_expired_output()
        await recover_expired_daemon_leases()
        # After expired leases are back on the queue, say which daemons died
        # and which Tickets need a person, failed, or are out of quota. Stuck
        # Tickets are notified from the watchdog sweep below. A failure here
        # must not skip lease recovery, which already ran, or the sweep.
        await _notify_operator()
        await expire_stop_requests()
        await enqueue_due_polls()
        await recover_stale_evals()
        await start_due_evals()
        await ensure_launch_effects()
        await poll_running_workers()
        # Completed/cancelled Tickets leave every sweep above; their failed or
        # runner-deferred checkout removals are retried here. Absorbs its own
        # failures.
        await sweep_worktree_cleanups()
        # A due schedule runs unattended: nobody is there to confirm a
        # refined rewrite, so its Ticket holds the request for confirmation
        # instead of auto-refining it (AUTONOMY_PROMPT_REFINE).
        await run_due_schedules(hold_for_confirmation=True)
        await watchdog_sweep(main_agent_orchestrator=self.main_agent_orchestrator)
        await self._enqueue_main_agent_advances()
        # This pass is strictly opt-in and proposal-only. Its failures must not
        # stall unrelated autonomy recovery or dispatch work.
        try:
            from src.server.services.refactor.memory_consolidation import (
                run_due_memory_consolidations,
            )

            await run_due_memory_consolidations(store=self.memory_store)
        except Exception:  # noqa: BLE001 - isolate optional consolidation from the pass
            logger.exception("[memory-consolidation] scheduler pass failed")
        # Wakers are opt-in and isolated. A CodexBar or RepoBar failure must
        # not stall ticket recovery, and a disabled switch does no I/O.
        try:
            from src.server.services.autonomy.wakers import run_waker_pass

            await run_waker_pass()
        except Exception:  # noqa: BLE001 - the pass has already done its durable work
            logger.exception("[autonomy] waker pass failed")

    async def _collect_expired_output(self) -> None:
        """Collect one bounded batch of expired live-output logs, now and then.

        A full batch means more may remain, so the next pass continues the drain
        instead of waiting out the interval. A failure only costs this
        housekeeping: it is retried after the interval and never stalls recovery
        or dispatch. An idle pass is one index probe, so running ahead of
        recovery costs it milliseconds.
        """
        from src.server.database.autonomy.daemon_output import (
            EXPIRE_BATCH,
            expire_old_logs,
        )

        now = time.monotonic()
        if now < self._output_gc_due:
            return
        self._output_gc_due = now + OUTPUT_GC_INTERVAL_SECONDS
        try:
            collected = await expire_old_logs()
        except Exception:  # noqa: BLE001 - housekeeping must not stall the pass
            logger.exception("[autonomy] daemon output expiry failed")
            return
        if collected >= EXPIRE_BATCH:
            self._output_gc_due = now

    async def _enqueue_main_agent_advances(self) -> int:
        """Queue bounded, idempotent coordinator work without holding the pass."""
        orchestrator = self.main_agent_orchestrator
        if orchestrator is None:
            return 0
        try:
            runs = await orchestrator.repository.list_runs(
                None,
                ["accepted", "queued", "running"],
                limit=5000,
            )
        except Exception:  # noqa: BLE001 - next scheduler pass retries the durable scan
            logger.exception("[main-agent] could not list runs for reconciliation")
            return 0
        queued = 0
        eligible = [run for run in runs if run_needs_reconciliation(run)]
        for run in eligible[:MAIN_AGENT_RECONCILE_LIMIT]:
            phase = str(run.get("phase") or "")
            try:
                inserted = await enqueue_effect(
                    user_id=str(run["user_id"]),
                    kind=EFFECT_MAIN_AGENT_ADVANCE,
                    idempotency_key=(
                        f"main-agent:advance:{run['id']}:{phase}:{run.get('revision', 0)}"
                    ),
                    payload={"run_id": str(run["id"]), "user_id": str(run["user_id"])},
                )
                queued += int(inserted)
            except Exception:  # noqa: BLE001 - isolate a run's model/error path from peers
                logger.exception(
                    "[main-agent] could not enqueue coordinator work run=%s",
                    run.get("id"),
                )
        return queued

    async def _main_agent_advance_effect(self, effect) -> None:
        orchestrator = self.main_agent_orchestrator
        if orchestrator is None:
            raise RuntimeError("main-agent coordinator is not available")
        payload = dict(effect.payload or {})
        try:
            await orchestrator.tick(str(payload["run_id"]), str(payload["user_id"]))
        except SourceUnavailableError as exc:
            # The run's immutable context can outlive its Source. Replaying this
            # effect cannot repair that mismatch; let the effect runner park it.
            raise PermanentEffectError(str(exc)) from exc


_scheduler: AutonomyScheduler | None = None


def start_autonomy_scheduler(
    *, main_agent_orchestrator: Any | None = None, memory_store: Any | None = None
) -> None:
    """Start the per-process scheduler from the app lifespan."""
    global _scheduler
    _scheduler = AutonomyScheduler.get_instance(
        main_agent_orchestrator, memory_store
    )
    asyncio.get_running_loop().create_task(
        _scheduler.start(), name="autonomy-scheduler-start"
    )


async def stop_autonomy_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop()
        _scheduler = None


def _scheduler_for_tests() -> AutonomyScheduler | None:
    return _scheduler
