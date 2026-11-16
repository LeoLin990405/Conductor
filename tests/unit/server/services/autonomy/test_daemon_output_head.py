"""A watcher that already stands at the log's head costs one statement."""

from __future__ import annotations

import pytest

from src.server.services.autonomy import daemon_output as store
from tests.unit.server.services.autonomy import test_daemon_output as base

# The fake store fixture and helpers are shared, not copied.
db = base.db


@pytest.mark.asyncio
async def test_a_watcher_at_the_head_runs_no_events_query_and_still_gets_the_next_event(
    db, monkeypatch
):
    epoch = await base.open_stream([base.text("one\n")])
    head = await store.read_output(base.TICKET, None)
    queried: list[tuple] = []
    real = db.read_events

    async def spy(*args):
        queried.append(args)
        return await real(*args)

    monkeypatch.setattr(db, "read_events", spy)
    idle = await store.read_output(base.TICKET, head.cursor)
    assert (idle.events, idle.reset, idle.cursor) == ([], None, head.cursor)
    assert queried == []
    await base.post([base.text("two\n")], epoch=epoch, seq=2)
    nxt = await store.read_output(base.TICKET, head.cursor)
    assert [e["seq"] for e in nxt.events] == [2]
    assert len(queried) == 1
