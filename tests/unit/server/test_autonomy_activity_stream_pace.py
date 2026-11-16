"""Daemon-output read pace of the activity stream: fast while output flows,
slow while a quiet log is watched."""

from __future__ import annotations

from unittest.mock import patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app
from tests.unit.server.test_autonomy_activity_stream import (
    MODULE,
    _activity_patches,
    _collect,
    _daemon_ticket,
    _enter,
    _entry,
    _exit,
    _page,
)


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http:
        yield http


def test_daemon_poll_delay_backs_off_only_after_the_fast_hold():
    from src.server.app import ticket_activity_stream as stream_module

    hold = stream_module.ACTIVITY_STREAM_DAEMON_FAST_HOLD_S
    fast = stream_module.ACTIVITY_STREAM_DAEMON_POLL_S
    idle = stream_module.ACTIVITY_STREAM_DAEMON_IDLE_POLL_S
    mid = stream_module.ACTIVITY_STREAM_DAEMON_MID_POLL_S
    mid_hold = stream_module.ACTIVITY_STREAM_DAEMON_MID_HOLD_S
    assert stream_module.daemon_poll_delay(0.0) == fast
    assert stream_module.daemon_poll_delay(hold - 0.01) == fast
    assert stream_module.daemon_poll_delay(hold) == mid
    assert stream_module.daemon_poll_delay(mid_hold - 0.01) == mid
    assert stream_module.daemon_poll_delay(mid_hold) == idle
    assert fast < mid < idle
    # A model pausing between bursts (a few seconds) never waits more than half
    # a second for its next token to show.
    assert mid <= 0.5


@pytest.mark.asyncio
async def test_an_idle_daemon_watcher_backs_off_and_new_output_restores_the_fast_pace(
    client,
):
    """A quiet log is read at the idle cadence; the first event moves the next
    read back to the fast one.

    Asserted on the delays the stream chose, in the order it chose them, not on
    when reads landed on the wall clock: a stalled event loop (a loaded CI box)
    then changes how long the test takes and never what it sees. The event is
    held back until the stream has chosen the idle delay, and the ticket ends a
    few choices after it.
    """
    from src.server.app import ticket_activity_stream as stream_module

    fast, idle, hold = 0.01, 0.05, 0.03
    real_delay = stream_module.daemon_poll_delay
    chosen: list[tuple[float, float]] = []  # (seconds without movement, delay)
    delivered_at: list[int] = []  # len(chosen) when the event was served

    def recording_delay(idle_s: float) -> float:
        delay = real_delay(idle_s)
        chosen.append((idle_s, delay))
        return delay

    async def fake_read(ticket_id: str, cursor, *, count=64):
        if not delivered_at and chosen and chosen[-1][1] == idle:
            delivered_at.append(len(chosen))
            return _page(
                2, [_entry(2, 1, {"kind": "text", "text": "hi", "run_id": "r1"})]
            )
        return _page(2, [], cursor=cursor or "2.0")

    async def fake_get(_ticket_id: str):
        done = bool(delivered_at) and len(chosen) >= delivered_at[0] + 3
        return _daemon_ticket(
            status="completed" if done else "running",
            summary="done" if done else "",
        )

    patches = _activity_patches(tickets=[_daemon_ticket(status="running")]) + (
        patch(f"{MODULE}.tickets_db.get_ticket", new=fake_get),
        patch(f"{MODULE}.daemon_poll_delay", new=recording_delay),
        patch(f"{MODULE}.ACTIVITY_STREAM_DAEMON_POLL_S", fast),
        patch(f"{MODULE}.ACTIVITY_STREAM_DAEMON_IDLE_POLL_S", idle),
        patch(f"{MODULE}.ACTIVITY_STREAM_DAEMON_FAST_HOLD_S", hold),
        # No middle tier here: the idle cadence follows the fast hold directly.
        patch(f"{MODULE}.ACTIVITY_STREAM_DAEMON_MID_HOLD_S", hold),
    )
    _enter(patches)
    try:
        with patch(f"{MODULE}.daemon_output_store.read_output", new=fake_read):
            frames = await _collect(client, timeout=10.0)
    finally:
        _exit(patches)

    assert any(d.get("text") == "hi" for _, e, d in frames if e == "output")
    assert delivered_at, "the stream never backed off to the idle delay"
    served = delivered_at[0]
    # A fresh attach reads fast; only a quiet stretch of at least the hold earns
    # the idle delay.
    assert chosen[0][1] == fast
    assert all(idle_s >= hold for idle_s, delay in chosen[:served] if delay == idle)
    # The event moved the cursor, so the very next choice starts from zero.
    assert chosen[served] == (0.0, fast)
