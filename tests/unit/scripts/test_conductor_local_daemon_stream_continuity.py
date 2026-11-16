"""Daemon live-output sink: its guard state, acknowledgements and gap handling.

The sink judges every event against a local copy of the server's continuity
guard, so the copy has to describe exactly the events the server will have
received, and an acknowledgement it cannot read has to end the stream. These
tests pin those contracts; the broader sink behaviour is in
``test_conductor_local_daemon_stream.py``.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest

from scripts import conductor_local_daemon as daemon
from src.server.utils import output_guard
from tests.unit.scripts.test_conductor_local_daemon_stream import FakeApi, _jsonl


def _replay(events: list[dict]) -> output_guard.GuardState:
    """The guard state a reader holds after exactly ``events``, in order."""
    state = output_guard.GuardState()
    for event in events:
        if event["kind"] == "tool":
            state = output_guard.scan(
                state,
                "\n".join(event["tools"]) + "\n",
                channel=output_guard.CHANNEL_TOOL,
            ).state
        elif event["kind"] in {"text", "reasoning"}:
            state = output_guard.scan(
                state,
                event["text"],
                channel=event["kind"],
                join=bool(event.get("join")),
            ).state
    return state


def _sent(api: FakeApi) -> list[dict]:
    return [event for call in api.calls for event in call[3]]


async def _fill_queue(sink: daemon._DaemonOutputStream, last: str = "") -> None:
    """Fill the queue to its cap with text events (the flusher is not running)."""
    for index in range(daemon._OUTPUT_STREAM_MAX_EVENTS - (1 if last else 0)):
        await sink.add({"kind": "text", "text": f"w{index} "})
    if last:
        await sink.add({"kind": "text", "text": last})
    assert len(sink._pending) == daemon._OUTPUT_STREAM_MAX_EVENTS  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_dropped_tool_name_leaves_the_local_guard_where_the_server_is():
    """A name the full queue dropped never reaches the server, so it must not
    sit in the window the next event is judged against: "Bearer" followed by a
    token-like word would stop text the server accepts."""
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await _fill_queue(sink)
    await sink.add({"kind": "tool", "tools": ["Bearer"]})  # dropped: queue full
    assert len(sink._pending) == daemon._OUTPUT_STREAM_MAX_EVENTS  # noqa: SLF001
    assert await sink._flush_once() is True  # room for one more batch
    await sink.add({"kind": "text", "text": "abcdefgh "})

    assert sink.text_disabled is False and sink.disabled is False
    assert sink._pending[-1]["text"] == "abcdefgh "  # noqa: SLF001
    assert sink._guard == _replay(_sent(api) + sink._pending)  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_tool_name_the_server_would_refuse_is_refused_locally_after_a_drop():
    """The server holds "sk-ab" and never saw the dropped name, so "cdefghij"
    completes a key there; the sink must judge it the same way and not send it."""
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await _fill_queue(sink, last="see sk-ab")
    await sink.add({"kind": "tool", "tools": ["zz"]})  # dropped: queue full
    assert await sink._flush_once() is True
    await sink.add({"kind": "tool", "tools": ["cdefghij"]})

    assert sink.stop_reason == "guard:credential_shape"
    await sink._flush_all()
    assert "cdefghij" not in str(_sent(api))


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", range(40))
async def test_the_local_guard_always_equals_what_the_server_will_have_received(
    monkeypatch, seed
):
    """Random adds and partial flushes against a tiny queue, so names drop."""
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_MAX_EVENTS", 6)
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_BATCH_EVENTS", 2)
    rnd = random.Random(seed)
    words = ["alpha", "sk-ab", "Bearer", "pass", "cdefghij", "Basic", "x1", "note"]
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    for _ in range(rnd.randint(8, 40)):
        if sink.text_disabled:
            break
        roll = rnd.random()
        if roll < 0.45:
            await sink.add({"kind": "tool", "tools": [rnd.choice(words)]})
        elif roll < 0.8:
            await sink.add({"kind": "text", "text": rnd.choice(words) + " "})
        else:
            assert await sink._flush_once() is True
        if not sink.text_disabled:
            assert sink._guard == _replay(_sent(api) + sink._pending)  # noqa: SLF001


@pytest.mark.asyncio
async def test_a_gap_stops_text_before_any_io_and_survives_a_dead_network():
    """The runner may call ``discontinuity`` again if the first report failed,
    and it runs inside the CLI's read loop: it must be a local state change, and
    a stop the network then refuses must not reach the Worker."""
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "hello"})
    assert await sink._flush_once() is True
    api.error = ConnectionError("network down")
    sent = len(api.calls)

    sink.discontinuity("oversized_record")
    sink.discontinuity("oversized_record")  # offered again: still one stop
    assert sink.text_disabled is True and len(api.calls) == sent
    await sink.add({"kind": "text", "text": "after the gap"})
    assert sink._pending == [{"kind": "stop"}]  # noqa: SLF001

    await sink.finalize()  # the stop cannot be posted; nothing is raised
    assert sink.disabled is True
    assert sink.stop_reason == "discontinuity:oversized_record"
    assert api.texts() == "hello\n"


@pytest.mark.asyncio
async def test_a_failing_sink_discontinuity_still_keeps_text_out_of_the_stream(
    monkeypatch,
):
    """The runner swallows a raising callback and carries on, so the daemon must
    taint its own continuity before it asks the sink to stop."""
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")

    def broken(reason: str) -> None:
        raise RuntimeError("sink bug")

    monkeypatch.setattr(sink, "discontinuity", broken)

    async def fake_stream(invocation, *, on_discontinuity=None, **kwargs):
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "before the gap"},
        )
        try:
            on_discontinuity("oversized_record")
        except RuntimeError:
            pass  # what the runner's reporter does with a raising callback
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "after the gap"},
        )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()

    assert "after the gap" not in api.texts()


class _Missing:
    """A key the acknowledgement does not carry (stable repr: it names test ids)."""

    def __repr__(self) -> str:
        return "missing"


_MISSING = _Missing()


def _ack(stored=True, tainted=False, **extra) -> dict:
    ack = {"epoch": 7, **extra}
    if stored is not _MISSING:
        ack["stored"] = stored
    if tainted is not _MISSING:
        ack["tainted"] = tainted
    return ack


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ack", "reason"),
    [
        *[
            pytest.param(
                _ack(tainted=value), "bad_ack", id=f"tainted-{value!r}".replace(" ", "")
            )
            for value in (_MISSING, None, 0, "", [], {}, 1, "true", "false")
        ],
        *[
            pytest.param(_ack(stored=value), "bad_ack", id=f"stored-{value!r}")
            for value in (_MISSING, None, 1, "true")
        ],
        pytest.param(_ack(stored=False), "not_stored", id="stored-false"),
        pytest.param(_ack(stored=False, tainted=_MISSING), "not_stored", id="no-flags"),
    ],
)
async def test_an_acknowledgement_that_is_not_plainly_clean_ends_the_stream(
    ack, reason
):
    """Only exactly ``stored: true, tainted: false`` keeps the channel open: a
    missing or non-boolean flag is an answer the client cannot read."""
    api = FakeApi(ack=ack)
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "hello"})
    assert await sink._flush_once() is False
    assert sink.disabled is True and sink.stop_reason == reason

    await sink.add({"kind": "text", "text": "later"})
    assert await sink._flush_once() is True  # nothing left to send
    assert len(api.calls) == 1


@pytest.mark.asyncio
async def test_a_clean_acknowledgement_keeps_the_channel_open():
    api = FakeApi(ack=_ack())
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "hello"})
    assert await sink._flush_once() is True
    assert sink.disabled is False and sink.text_disabled is False
    assert sink.epoch == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [True, False])
async def test_an_acknowledgement_that_says_tainted_is_a_server_taint(stored):
    """``tainted: true`` is the server's own verdict: text stops, and no
    ``stop`` event is sent back for a stream the server already closed."""
    api = FakeApi(ack=_ack(stored=stored, tainted=True, reason="credential_key"))
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "hello"})
    await sink.add({"kind": "text", "text": "queued behind it"})
    assert await sink._flush_once() is False
    assert sink.text_disabled is True and sink.disabled is False
    assert sink.stop_reason == "server_taint:credential_key"
    assert sink._pending == []  # noqa: SLF001 - no stop event is announced
