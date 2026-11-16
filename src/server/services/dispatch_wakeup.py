"""Durable wakeup for one dispatch service. v1 只支持单实例。

Pending events stay in ``dispatch.gates["scheduler"]``. ``DispatchStore``
already persists that object. This module does not add a table or a migration,
and it does not restart the API, the database, or any other process. A second
process takes the Postgres advisory lock or it does not run recovery. Recovery
is the callback the service passes in, which is ``DispatchService._drive``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from src.tools.auto_dispatch.types import TERMINAL_STATES, Dispatch, DispatchState

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]
Recover = Callable[[str], Awaitable[object]]

# Session advisory lock. Not a row, not a migration.
WAKEUP_ADVISORY_LOCK_KEY = 870413
WAKEUP_PAGE = 1000
WAKEUP_RETRY_CAP_SECONDS = 300
RECOVER_CONCURRENCY = 4
RECOVER_TIMEOUT_SECONDS = 30.0


class WakeupLease(Protocol):
    """One holder runs recovery. v1 is a single service instance."""

    async def try_acquire(self) -> bool:
        """Return True when this caller owns the lock."""
        ...

    async def release(self) -> None:
        """Drop the lock. A caller that does not own it does nothing."""
        ...


class MemoryWakeupLease:
    """In-process lease for tests and for a service that has no database."""

    def __init__(self, held: set[int] | None = None) -> None:
        self._held = held if held is not None else set()
        self._owner = False

    async def try_acquire(self) -> bool:
        if WAKEUP_ADVISORY_LOCK_KEY in self._held:
            return False
        self._held.add(WAKEUP_ADVISORY_LOCK_KEY)
        self._owner = True
        return True

    async def release(self) -> None:
        if not self._owner:
            return
        self._held.discard(WAKEUP_ADVISORY_LOCK_KEY)
        self._owner = False


class PostgresWakeupLease:
    """``pg_try_advisory_lock`` on the existing connection. The schema stays put."""

    def __init__(self) -> None:
        self._context = None
        self._conn = None
        self._owner = False

    async def try_acquire(self) -> bool:
        from src.server.database.pool import get_db_connection

        self._context = get_db_connection()
        self._conn = await self._context.__aenter__()
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT pg_try_advisory_lock(%s)",
                (WAKEUP_ADVISORY_LOCK_KEY,),
            )
            row = await cur.fetchone()
        if not row or not row[0]:
            await self._context.__aexit__(None, None, None)
            self._context = None
            self._conn = None
            return False
        self._owner = True
        return True

    async def release(self) -> None:
        if not self._owner or self._conn is None or self._context is None:
            return
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    (WAKEUP_ADVISORY_LOCK_KEY,),
                )
        finally:
            self._owner = False
            await self._context.__aexit__(None, None, None)
            self._context = None
            self._conn = None


def lease_for_store(store: object) -> WakeupLease:
    """The database store takes the advisory lock. Anything else stays in memory."""
    if type(store).__name__ == "DbDispatchStore":
        return PostgresWakeupLease()
    return MemoryWakeupLease()


def _live_states() -> list[str]:
    return [state.value for state in DispatchState if state not in TERMINAL_STATES]


def _row_key(row: Dispatch) -> tuple[str, str]:
    """Fair scan order is ``(updated_at, id)``, then ``created_at``."""
    stamp = row.created_at or ""
    gates = row.gates if isinstance(row.gates, dict) else None
    raw = gates.get("updated_at") if isinstance(gates, dict) else None
    if isinstance(raw, str) and raw:
        stamp = raw
    return (stamp, row.id)


def _retry_payload(previous: object, now: datetime) -> dict[str, object]:
    attempts = 1
    if isinstance(previous, dict):
        try:
            attempts = int(previous.get("attempts") or 0) + 1
        except (TypeError, ValueError):
            attempts = 1
    delay = min(WAKEUP_RETRY_CAP_SECONDS, 2 ** min(max(attempts - 1, 0), 8))
    return {
        "attempts": attempts,
        "next_at": (now + timedelta(seconds=delay)).isoformat(),
    }


class DispatchWakeup:
    """Start the consumer, then recover in the background.

    ``open`` waits until the consumer task exists. Recovery itself runs behind
    that task, four dispatches at a time. A phase keeps its own deadline.
    There is no outer timeout around ``recover``. ``CancelledError`` stops the
    loop. Any other exception is logged as a type name and a reason code, and
    the loop keeps going. A row whose ``wakeup_retry.next_at`` is still ahead
    of the clock is left for a later tick.
    """

    def __init__(
        self,
        *,
        store: object,
        recover: Recover,
        poll_interval_seconds: float,
        lease: WakeupLease | None = None,
        recover_timeout_seconds: float = RECOVER_TIMEOUT_SECONDS,
        clock: Clock | None = None,
    ) -> None:
        self._store = store
        self._recover = recover
        self._interval = poll_interval_seconds
        self._lease = lease if lease is not None else MemoryWakeupLease()
        # Kept so existing callers can still pass it. It does not cancel recover.
        self._timeout = recover_timeout_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()
        self._cursor: tuple[str, str] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        self._sem = asyncio.Semaphore(RECOVER_CONCURRENCY)
        self.skip_reason: str | None = None

    async def start(self) -> None:
        """Return once the consumer is ready. Recovery keeps running after that."""
        if self._task is not None and not self._task.done():
            await self._ready.wait()
            return
        self._ready = asyncio.Event()
        if not await self._lease.try_acquire():
            self.skip_reason = "wakeup_lease_held"
            logger.warning("Dispatch wakeup skipped %s", "wakeup_lease_held")
            self._ready.set()
            return
        self.skip_reason = None
        self._task = asyncio.create_task(self._main(), name="dispatch-wakeup")
        await self._ready.wait()

    async def stop(self) -> None:
        """Cancel and join the consumer and every recovery still in flight.

        Each recovery drive joins its machine before it returns, so this wait
        includes the actor and any save that was already in flight. The drive
        owner is released only after that join, and the lease is released only
        after those drives have been awaited.
        """
        for task in list(self._inflight):
            task.cancel()
        main = self._task
        self._task = None
        if main is not None:
            main.cancel()
            try:
                await main
            except asyncio.CancelledError:
                pass
        for task in list(self._inflight):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._inflight.clear()
        await self._lease.release()

    async def sweep(self) -> None:
        """One fair pass. Tests call this; ``start`` runs it in the background."""
        await self._recover_all()

    async def consume(self) -> None:
        """Every tick reconciles every active dispatch, not only pending events."""
        await self._recover_all()

    async def _main(self) -> None:
        self._ready.set()
        while True:
            try:
                await self._recover_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Dispatch wakeup consumer failed %s %s",
                    type(exc).__name__,
                    "wakeup_consumer_failed",
                )
            await asyncio.sleep(self._interval)

    async def _list_window(self) -> list[Dispatch]:
        """Widen ``list(limit=...)`` until an older row is inside the window."""
        page = WAKEUP_PAGE
        rows: list[Dispatch] = []
        while page <= 100_000:
            rows = list(
                await self._store.list(states=_live_states(), limit=page)  # type: ignore[attr-defined]
            )
            if len(rows) < page or page >= 100_000:
                return rows
            wider = page + WAKEUP_PAGE
            probe = list(
                await self._store.list(states=_live_states(), limit=wider)  # type: ignore[attr-defined]
            )
            if len(probe) <= len(rows):
                return rows
            page = wider
        return rows

    async def _due_rows(self) -> list[Dispatch]:
        """One forward page of ``(updated_at or created_at, id)``, oldest first.

        The store has no cursor. After the window covers the rows, the cursor
        walks forward and wraps to the start so a later round still moves.
        """
        rows = await self._list_window()
        ordered = sorted(rows, key=_row_key)
        cursor = self._cursor
        fresh = [row for row in ordered if cursor is None or _row_key(row) > cursor]
        if not fresh and cursor is not None:
            self._cursor = None
            fresh = list(ordered)
        if fresh:
            self._cursor = _row_key(fresh[-1])
        if len(rows) < WAKEUP_PAGE or len(rows) < 100_000:
            # The window held every active row, so the next tick starts over.
            if len(ordered) == len(rows) and len(rows) < 100_000:
                self._cursor = None
        now = self._clock()
        return [row for row in fresh if self._retry_ready(row, now)]

    async def _recover_all(self) -> None:
        rows = await self._due_rows()
        tasks: list[asyncio.Task[None]] = []
        for row in rows:
            task = asyncio.create_task(
                self._guarded(row.id),
                name=f"dispatch-wakeup-{row.id}",
            )
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            tasks.append(task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _retry_ready(self, row: Dispatch, now: datetime | None = None) -> bool:
        """False while ``wakeup_retry.next_at`` is still ahead of ``now``."""
        gates = row.gates if isinstance(row.gates, dict) else None
        raw = gates.get("wakeup_retry") if isinstance(gates, dict) else None
        if not isinstance(raw, dict):
            return True
        stamp = raw.get("next_at")
        if not isinstance(stamp, str) or not stamp:
            return True
        try:
            due = datetime.fromisoformat(stamp)
        except ValueError:
            return True
        if due.tzinfo is None:
            due = due.replace(tzinfo=timezone.utc)
        current = self._clock() if now is None else now
        if not isinstance(current, datetime):
            return True
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current >= due

    async def _guarded(self, dispatch_id: str) -> None:
        async with self._sem:
            row = await self._store.get(dispatch_id)  # type: ignore[attr-defined]
            if row is not None and not self._retry_ready(row):
                return
            try:
                await self._recover(dispatch_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._remember_retry(dispatch_id)
                logger.warning(
                    "Dispatch wakeup recover failed %s %s %s",
                    dispatch_id,
                    type(exc).__name__,
                    "wakeup_recover_failed",
                )
                return
            await self._clear_retry(dispatch_id)

    async def _mutate(self, dispatch_id: str, fn: Callable[[Dispatch], Dispatch]) -> None:
        """Change one row through the service mutation, or the store when unbound."""
        mutate = getattr(self._store, "apply_mutation", None)
        if mutate is not None:
            try:
                await mutate(dispatch_id, fn)
            except KeyError:
                return
            return
        row = await self._store.get(dispatch_id)  # type: ignore[attr-defined]
        if row is None:
            return
        updated = fn(row)
        if updated is None:
            return
        writer = getattr(self._store, "save")
        await writer(updated)

    async def _remember_retry(self, dispatch_id: str) -> None:
        def mutation(row: Dispatch) -> Dispatch:
            gates = dict(row.gates) if isinstance(row.gates, dict) else {}
            gates["wakeup_retry"] = _retry_payload(
                gates.get("wakeup_retry"),
                self._clock(),
            )
            row.gates = gates
            return row

        await self._mutate(dispatch_id, mutation)

    async def _clear_retry(self, dispatch_id: str) -> None:
        def mutation(row: Dispatch) -> Dispatch:
            if not isinstance(row.gates, dict) or "wakeup_retry" not in row.gates:
                return row
            gates = dict(row.gates)
            gates.pop("wakeup_retry", None)
            row.gates = gates
            return row

        await self._mutate(dispatch_id, mutation)
