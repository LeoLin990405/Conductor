"""Daemon live-output sink: a flood of tool records cannot hold the event loop.

One stdout read can carry a thousand tool records and each is scanned against
full windows, so the sink has to hand control back between records or
cancellation, lease renewal and the flusher wait out the whole read. These tests
count loop turns instead of timing them, so they do not depend on the machine.
"""

from __future__ import annotations

import asyncio

import pytest

from scripts import conductor_local_daemon as daemon
from tests.unit.scripts.test_conductor_local_daemon_stream import FakeApi


@pytest.mark.asyncio
async def test_each_tool_record_gives_the_event_loop_a_turn():
    turns = 0

    async def other_work() -> None:
        nonlocal turns
        while True:
            turns += 1
            await asyncio.sleep(0)

    task = asyncio.create_task(other_work())
    sink = daemon._DaemonOutputStream(FakeApi(), "t", "l", "r")
    records = 40
    try:
        for index in range(records):
            await sink.add({"kind": "tool", "tools": [f"mcp__s__t{index}"]})
    finally:
        task.cancel()

    # Without a yield in the tool path the other task never runs at all.
    assert turns >= records
    assert len(sink._pending) == records


@pytest.mark.asyncio
async def test_a_stop_that_lands_during_the_yield_keeps_the_tool_name_out():
    sink = daemon._DaemonOutputStream(FakeApi(), "t", "l", "r")

    async def stop_meanwhile() -> None:
        # What the flusher does when an acknowledgement taints the epoch.
        sink.stop_text("server_taint", drop_pending=True, notify=False)

    task = asyncio.create_task(stop_meanwhile())
    await sink.add({"kind": "tool", "tools": ["mcp__s__late"]})
    await task

    # The stop is checked after the yield: a name queued behind it would
    # outlive the text and form a projection no window tracks.
    assert sink.text_disabled is True
    assert sink._pending == []
