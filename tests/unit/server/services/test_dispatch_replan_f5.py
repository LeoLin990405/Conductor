"""F5: wakeup recovery waits until wakeup_retry.next_at."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.server.services.dispatch_wakeup import DispatchWakeup
from src.tools.auto_dispatch import Dispatch, DispatchState, InMemoryDispatchStore

pytestmark = pytest.mark.asyncio

WHEN = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


async def test_f5_backoff_skips_until_next_at() -> None:
    store = InMemoryDispatchStore()
    await store.create(
        Dispatch(
            id="retry",
            goal="test",
            user_id="u",
            created_at="now",
            state=DispatchState.MONITORING,
        )
    )
    now = [WHEN]
    calls = 0

    async def recover(_dispatch_id: str) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("safe-test-error")

    wakeup = DispatchWakeup(
        store=store,
        recover=recover,
        poll_interval_seconds=10,
        clock=lambda: now[0],
    )
    await wakeup.consume()
    await wakeup.consume()
    await wakeup.consume()
    assert calls == 1
    row = await store.get("retry")
    assert row is not None
    retry = row.gates["wakeup_retry"]
    assert retry["attempts"] == 1
    due = datetime.fromisoformat(str(retry["next_at"]))
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    assert now[0] < due
    now[0] = due - timedelta(microseconds=1)
    await wakeup.consume()
    assert calls == 1
    now[0] = due
    await wakeup.consume()
    assert calls == 2
    row = await store.get("retry")
    assert row is not None
    assert row.gates["wakeup_retry"]["attempts"] == 2
