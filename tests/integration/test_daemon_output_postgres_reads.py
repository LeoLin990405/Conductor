"""Watcher reads against a real PostgreSQL (migration 140 applied).

Run like ``test_daemon_output_postgres.py``. A reader that stands at the head
skips the events query, which is only lossless if the head and the events really
do commit together; that is checked here with readers chasing a live writer.
"""

from __future__ import annotations

import asyncio

import pytest

psycopg = pytest.importorskip("psycopg")

from tests.integration import test_daemon_output_postgres as base  # noqa: E402

pytestmark = pytest.mark.integration

# The scratch-database fixture and helpers are shared, not copied.
pg = base.pg
_post = base._post


@pytest.mark.asyncio
async def test_a_read_at_the_head_is_empty_and_the_next_append_is_delivered(pg):
    first = await _post(pg, [{"kind": "text", "text": "one\n"}])
    head = await pg.store.read_output(pg.ticket, None)
    idle = await pg.store.read_output(pg.ticket, head.cursor)
    assert (idle.events, idle.reset, idle.cursor) == ([], None, head.cursor)
    await _post(pg, [{"kind": "text", "text": "two\n"}], epoch=first.epoch, seq=2)
    nxt = await pg.store.read_output(pg.ticket, head.cursor)
    assert [e["seq"] for e in nxt.events] == [2] and nxt.reset is None


@pytest.mark.asyncio
async def test_readers_chasing_a_writer_miss_nothing_and_see_nothing_twice(pg):
    first = await _post(pg, [{"kind": "text", "text": "0\n"}])
    total = 60
    done = asyncio.Event()

    async def write() -> None:
        for n in range(1, total):
            await _post(
                pg, [{"kind": "text", "text": f"{n}\n"}], epoch=first.epoch, seq=n + 1
            )
        done.set()

    async def read() -> list[int]:
        cursor, seen = None, []
        while True:
            finished = done.is_set()
            page = await pg.store.read_output(pg.ticket, cursor)
            assert page.reset is None, page.reset
            seen += [e["seq"] for e in page.events]
            cursor = page.cursor
            if finished and not page.events:
                return seen
            await asyncio.sleep(0.005)

    seen_by_reader = await asyncio.wait_for(
        asyncio.gather(write(), read(), read(), read()), 60
    )
    for seen in seen_by_reader[1:]:
        assert seen == list(range(1, total + 1))
