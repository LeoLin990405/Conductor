"""Collection of expired daemon output logs on a real PostgreSQL (migration 140).

Run like ``test_daemon_output_postgres.py``. The statement is a recursive walk
over the primary key, so what it deletes (and what it must leave alone) is
pinned against the real planner rather than a mock.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.integration import test_daemon_output_postgres as base  # noqa: E402

pytestmark = pytest.mark.integration

# The scratch-database fixture and helpers are shared, not copied.
pg = base.pg
_dsn = base._dsn
_post = base._post


async def _seed(pg, rows):
    """Insert ``(epoch, seq, age)`` events for the fixture's Ticket."""
    async with pg.probe.cursor() as cur:
        for epoch, seq, age in rows:
            await cur.execute(
                "INSERT INTO autonomy_daemon_output_events "
                "(ticket_id, epoch, seq, data, created_at) "
                "VALUES (%s, %s, %s, '{}'::jsonb, NOW() - %s::interval)",
                (pg.ticket, epoch, seq, age),
            )


async def _epochs(pg):
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "SELECT epoch, array_agg(seq ORDER BY seq) FROM "
            "autonomy_daemon_output_events WHERE ticket_id=%s "
            "GROUP BY epoch ORDER BY epoch",
            (pg.ticket,),
        )
        return {epoch: seqs for epoch, seqs in await cur.fetchall()}


@pytest.mark.asyncio
async def test_partly_old_logs_ahead_cannot_starve_the_expired_ones(pg):
    from src.server.database.autonomy import daemon_output as output_db

    await _post(pg, [{"kind": "text", "text": "keep me\n"}])
    live = await _epochs(pg)
    # 12 partly-old logs sort ahead of 8 wholly expired ones; a batch of 5
    # must not be spent on the partial logs (they are never deleted).
    partial = [(100 + n, s, "0 days" if s == 3 else "2 days") for n in range(12) for s in (1, 2, 3)]
    expired = [(200 + n, s, "3 days") for n in range(8) for s in (1, 2)]
    await _seed(pg, partial + expired)
    for _ in range(10):
        if await output_db.expire_old_logs(5) < 5:
            break
    remaining = await _epochs(pg)
    assert not [e for e in remaining if 200 <= e < 300], "an expired log survived"
    assert all(remaining[100 + n] == [1, 2, 3] for n in range(12))
    assert {e: remaining[e] for e in live} == live


@pytest.mark.asyncio
async def test_a_pass_with_nothing_due_collects_nothing(pg):
    from src.server.database.autonomy import daemon_output as output_db

    await _post(pg, [{"kind": "text", "text": "fresh\n"}])
    before = await _epochs(pg)
    assert await output_db.expire_old_logs() == 0
    assert await _epochs(pg) == before


@pytest.mark.asyncio
async def test_a_second_collector_yields_while_one_holds_the_lock(pg):
    from src.server.database.autonomy import daemon_output as output_db

    await _post(pg, [{"kind": "text", "text": "fresh\n"}])
    await _seed(pg, [(300, 1, "3 days"), (300, 2, "3 days")])
    holder = await psycopg.AsyncConnection.connect(_dsn())  # explicit transaction
    try:
        await holder.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            ("autonomy_daemon_output_gc",),
        )
        assert await output_db.expire_old_logs() == 0
        assert 300 in await _epochs(pg), "a second collector must not delete"
    finally:
        await holder.rollback()
        await holder.close()
    assert await output_db.expire_old_logs() >= 1
    assert 300 not in await _epochs(pg)


@pytest.mark.asyncio
async def test_a_log_silent_for_a_day_is_collected_and_its_watcher_is_reset_once(pg):
    from src.server.database.autonomy import daemon_output as output_db

    first = await _post(pg, [{"kind": "text", "text": "one\n"}])
    epoch = first.epoch
    await _post(pg, [{"kind": "text", "text": "two\n"}], epoch=epoch, seq=2)
    mid = await pg.store.read_output(pg.ticket, f"{epoch}.1")
    assert [e["seq"] for e in mid.events] == [2]
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "UPDATE autonomy_daemon_output_events SET created_at=NOW()-INTERVAL '3 days' "
            "WHERE ticket_id=%s",
            (pg.ticket,),
        )
    assert await output_db.expire_old_logs() >= 1
    # The lease is still live, so the writer carries on past the collected log.
    later = await _post(pg, [{"kind": "text", "text": "three\n"}], epoch=epoch, seq=3)
    assert later.stored is True
    # A watcher that stood inside the collected log is told, not stitched on.
    stale = await pg.store.read_output(pg.ticket, f"{epoch}.1")
    assert stale.reset == "gap"
    # One that was at the head sees only what came after, with no hole.
    head = await pg.store.read_output(pg.ticket, f"{epoch}.2")
    assert head.reset is None and [e["seq"] for e in head.events] == [3]


@pytest.mark.asyncio
async def test_a_fresh_watcher_of_a_collected_log_is_not_reset_for_a_gap(pg):
    from src.server.database.autonomy import daemon_output as output_db

    first = await _post(pg, [{"kind": "text", "text": "one\n"}])
    epoch = first.epoch
    await _post(pg, [{"kind": "text", "text": "two\n"}], epoch=epoch, seq=2)
    async with pg.probe.cursor() as cur:
        await cur.execute(
            "UPDATE autonomy_daemon_output_events SET created_at=NOW()-INTERVAL '3 days' "
            "WHERE ticket_id=%s",
            (pg.ticket,),
        )
    assert await output_db.expire_old_logs() >= 1
    # The session row stays (its epoch and head are the writer's), the events go.
    page = await pg.store.read_output(pg.ticket, None)
    assert page.reset is None and page.events == []
    assert page.cursor == f"{epoch}.2"
    again = await pg.store.read_output(pg.ticket, page.cursor)
    assert again.reset is None and again.events == []
