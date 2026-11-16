"""Daemon live-output streaming: API sink, Codex incremental parse, failures."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts import conductor_local_daemon as daemon
from src.llms.native_cli.base import NativeCliError


def _jsonl(**event) -> str:
    return json.dumps(event)


AGENT_MESSAGE = _jsonl(
    type="item.completed",
    item={"id": "i1", "type": "agent_message", "text": "answer"},
)
TOOL_STARTED = _jsonl(
    type="item.started",
    item={"id": "t1", "type": "command_execution", "command": "cat secret"},
)
TOOL_RESULT = _jsonl(
    type="item.completed",
    item={"id": "t1", "type": "command_execution", "aggregated_output": "secret"},
)
THREAD_STARTED = _jsonl(type="thread.started", thread_id="thread-1")
TURN_COMPLETED = _jsonl(
    type="turn.completed",
    usage={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
)


class FakeApi:
    """A server stand-in that allocates epoch 7 and then enforces the fence."""

    def __init__(self, *, error: Exception | None = None, ack: dict | None = None):
        self.calls: list[tuple] = []
        self.fence: list[tuple] = []
        self.error = error
        self.ack = ack
        self.epoch = 7

    def output(
        self,
        ticket_id,
        lease_id,
        run_id,
        events,
        *,
        epoch=None,
        batch_seq=1,
        timeout=None,
    ):
        self.calls.append((ticket_id, lease_id, run_id, list(events), timeout))
        self.fence.append((epoch, batch_seq))
        if self.error is not None:
            raise self.error
        if self.ack is not None:
            return dict(self.ack)
        return {"stored": True, "epoch": self.epoch, "tainted": False}

    def texts(self) -> str:
        return "".join(
            str(event.get("text") or "")
            for call in self.calls
            for event in call[3]
            if event.get("kind") in {"text", "reasoning"}
        )


class FakeStreamer:
    """Records what the Codex loop hands the sink (the sink is tested apart)."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.text_stopped: str | None = None
        self.discontinuities: list[str] = []

    @property
    def disabled(self) -> bool:
        return False

    @property
    def text_disabled(self) -> bool:
        return self.text_stopped is not None

    def stop_text(self, reason: str) -> None:
        self.text_stopped = self.text_stopped or reason

    def discontinuity(self, reason: str) -> None:
        self.discontinuities.append(reason)
        self.stop_text(f"discontinuity:{reason}")

    async def add(self, event) -> None:
        self.events.append(dict(event))


async def _stream_through_real_sink(monkeypatch, messages, *, api=None):
    """Run the Codex loop over ``messages`` into the real sink + fake server."""
    api = api or FakeApi()

    async def fake_stream(invocation, **kwargs):
        for text in messages:
            yield _jsonl(
                type="item.completed",
                item={"type": "agent_message", "text": text},
            )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()
    return sink, api


def test_api_output_posts_the_daemon_batch():
    api = daemon.Api("https://conductor.example", "daemon-secret")
    with patch.object(api, "_call", return_value={"stored": True}) as call:
        result = api.output(
            "ticket-1",
            "lease-1",
            "run-1",
            [{"kind": "text", "text": "hi"}],
            timeout=3.0,
        )
    assert result == {"stored": True}
    call.assert_called_once_with(
        "POST",
        "/api/v1/autonomy/daemon/tickets/ticket-1/output",
        {
            "lease_id": "lease-1",
            "run_id": "run-1",
            "batch_seq": 1,
            "events": [{"kind": "text", "text": "hi"}],
        },
        timeout=3.0,
    )


def test_api_output_echoes_only_a_server_allocated_epoch():
    api = daemon.Api("https://conductor.example", "daemon-secret")
    with patch.object(api, "_call", return_value={"stored": True}) as call:
        api.output(
            "ticket-1",
            "lease-1",
            "run-1",
            [{"kind": "text", "text": "hi\n"}],
            epoch=4,
            batch_seq=3,
        )
    body = call.call_args.args[2]
    assert body["epoch"] == 4 and body["batch_seq"] == 3


def test_parse_codex_output_events_forwards_only_visible_output():
    from src.llms.native_cli.codex import parse_codex_output_events

    assert parse_codex_output_events(AGENT_MESSAGE) == [
        {"kind": "text", "text": "answer"}
    ]
    tool = parse_codex_output_events(TOOL_STARTED)
    assert tool == [{"kind": "tool", "tools": ["command_execution"]}]
    # Tool output/arguments never cross the daemon stream boundary.
    assert parse_codex_output_events(TOOL_RESULT) == []
    assert parse_codex_output_events(THREAD_STARTED) == []
    assert parse_codex_output_events("not-json") == []


def test_parse_codex_output_events_reports_corruption_not_filtering():
    from src.llms.native_cli.codex import parse_codex_output_events

    seen: list[int] = []
    report = lambda: seen.append(1)  # noqa: E731
    for noise in ("not-json", '{"type": "item.comp', '"a string"', "[1, 2]", "42"):
        assert parse_codex_output_events(noise, on_malformed=report) == []
    assert len(seen) == 5
    # Blank lines and well-formed non-visible records are not corruption.
    for fine in ("", "   ", THREAD_STARTED, TOOL_RESULT, TURN_COMPLETED):
        parse_codex_output_events(fine, on_malformed=report)
    assert len(seen) == 5


@pytest.mark.parametrize(
    "damaged",
    [
        "{}",  # no record type
        '{"type": 7}',
        '{"type": "item.completed", "item": null}',
        '{"type": "item.completed"}',
        '{"type": "item.completed", "item": "text"}',
        '{"type": "item.completed", "item": {}}',
        '{"type": "item.completed", "item": {"type": "agent_message", "text": 5}}',
        '{"type": "item.completed", "item": {"type": "agent_message"}}',
        '{"type": "item.started", "item": {"type": "command_execution", "name": 3}}',
        '{"type": "item.started", "item": {"type": "mcp_tool_call", "tool": ["x"]}}',
    ],
)
def test_parse_codex_output_events_rejects_damaged_recognized_records(damaged):
    from src.llms.native_cli.codex import parse_codex_output_events

    seen: list[int] = []
    assert parse_codex_output_events(damaged, on_malformed=lambda: seen.append(1)) == []
    assert seen == [1]


@pytest.mark.asyncio
async def test_a_damaged_recognized_record_stops_text_in_the_real_sink(monkeypatch):
    async def fake_stream(invocation, **kwargs):
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "before"},
        )
        yield '{"type": "item.completed", "item": null}'
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "after"},
        )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()
    assert api.texts() == "before\n"
    assert sink.text_disabled is True


def test_token_stream_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DAEMON_TOKEN_STREAM", "0")
    assert daemon._token_stream_enabled() is False
    monkeypatch.setenv("CONDUCTOR_DAEMON_TOKEN_STREAM", "1")
    assert daemon._token_stream_enabled() is True
    monkeypatch.delenv("CONDUCTOR_DAEMON_TOKEN_STREAM")
    assert daemon._token_stream_enabled() is True


@pytest.mark.asyncio
async def test_output_sink_posts_visible_batches(monkeypatch):
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_FLUSH_S", 0.01)
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    sink.start()
    try:
        await sink.add({"kind": "text", "text": "hello"})
        await sink.add({"kind": "tool", "tools": ["bash"]})
        for _ in range(50):
            if api.calls:
                break
            await asyncio.sleep(0.01)
    finally:
        await sink.finalize()

    assert api.calls, "the sink should have posted its buffered batch"
    ticket_id, lease_id, run_id, events, timeout = api.calls[0]
    assert (ticket_id, lease_id, run_id) == ("ticket-1", "lease-1", "run-1")
    assert timeout == daemon._OUTPUT_STREAM_REQUEST_TIMEOUT_S
    assert [event["kind"] for event in events] == ["text", "tool"]
    # Every text event ends in whitespace (the stream never stops in a word).
    assert events[0]["text"] == "hello\n"
    # The first batch carries no epoch; the server allocates it.
    assert api.fence[0] == (None, 1)


@pytest.mark.asyncio
async def test_output_sink_never_raises_on_transport_failure(monkeypatch):
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_FLUSH_S", 0.01)
    api = FakeApi(error=RuntimeError("server unreachable"))
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    sink.start()
    await sink.add({"kind": "text", "text": "hello"})
    await asyncio.sleep(0.05)
    await sink.finalize()  # must not raise
    assert api.calls
    assert sink.disabled is True


@pytest.mark.asyncio
async def test_output_sink_disables_itself_on_a_missing_endpoint(monkeypatch):
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_FLUSH_S", 0.01)
    api = FakeApi(error=daemon.ApiError("POST ... -> HTTP 404", 404))
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    sink.start()
    await sink.add({"kind": "text", "text": "hello"})
    for _ in range(50):
        if sink.disabled:
            break
        await asyncio.sleep(0.01)
    await sink.finalize()

    assert sink.disabled is True
    await sink.add({"kind": "text", "text": "after"})
    assert not sink._pending  # noqa: SLF001 - buffered state is the behaviour


@pytest.mark.asyncio
async def test_output_sink_stops_text_on_buffer_overflow_and_announces_the_stop():
    """Dropping buffered text would leave a hole between what the server holds
    and what follows, so the stream stops for good. The queued text is
    discarded and a single stop marker follows what is already in flight."""
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "first message"})
    assert await sink._flush_once() is True  # acknowledged: the server holds text
    for index in range(daemon._OUTPUT_STREAM_MAX_EVENTS * 2):
        await sink.add({"kind": "text", "text": f"x{index} "})
    assert sink.text_disabled is True and sink.disabled is False
    assert sink.stop_reason == "buffer_overflow"
    kinds = [event["kind"] for event in sink._pending]  # noqa: SLF001
    assert kinds == ["stop"]
    await sink.add({"kind": "text", "text": "after"})
    await sink.add({"kind": "tool", "tools": ["bash"]})
    await sink._flush_all()
    sent = [event["kind"] for call in api.calls for event in call[3]]
    assert sent == ["text", "stop"]  # nothing, tool names included, follows a stop
    assert "after" not in api.texts() and "x0" not in api.texts()


@pytest.mark.asyncio
async def test_a_full_buffer_of_tool_names_drops_names_without_stopping_text():
    sink = daemon._DaemonOutputStream(FakeApi(), "t", "l", "r")
    for index in range(daemon._OUTPUT_STREAM_MAX_EVENTS + 20):
        await sink.add({"kind": "tool", "tools": [f"t{index}"]})
    assert sink.disabled is False and sink.text_disabled is False
    assert len(sink._pending) == daemon._OUTPUT_STREAM_MAX_EVENTS  # noqa: SLF001


@pytest.mark.asyncio
async def test_overflow_before_anything_was_sent_stays_quiet():
    """Nothing reached the server and the queued text is dropped, so there is
    nothing to announce; the sink just stops text."""
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    for index in range(daemon._OUTPUT_STREAM_MAX_EVENTS * 2):
        await sink.add({"kind": "text", "text": f"x{index} "})
    assert sink.text_disabled is True
    assert not sink._pending  # noqa: SLF001
    await sink._flush_all()
    assert api.calls == []


@pytest.mark.asyncio
async def test_run_codex_streaming_parses_result_and_taints_after_redaction(
    monkeypatch,
):
    async def fake_stream(invocation, **kwargs):
        for line in (
            THREAD_STARTED,
            _jsonl(
                type="item.completed",
                item={"type": "agent_message", "text": "my SECRET answer"},
            ),
            TOOL_STARTED,
            TURN_COMPLETED,
        ):
            yield line

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.redact_diagnostic",
        lambda text, env: str(text).replace("SECRET", "[redacted]"),
    )
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    streamer = FakeStreamer()

    result = await daemon._run_codex_streaming(adapter, "prompt", "/work", streamer)

    assert result.text == "my SECRET answer"
    # A redaction removed part of the message, so the daemon taints the rest
    # of the text channel rather than forward an unprovable subsequence; tool
    # names carry no text and still flow.
    text_events = [event for event in streamer.events if event["kind"] == "text"]
    assert text_events == []
    tool_events = [event for event in streamer.events if event["kind"] == "tool"]
    assert tool_events == [{"kind": "tool", "tools": ["command_execution"]}]
    assert "cat secret" not in json.dumps(tool_events)


@pytest.mark.asyncio
async def test_run_codex_streaming_without_events_matches_a_failed_adapter(monkeypatch):
    async def fake_stream(invocation, **kwargs):
        if False:  # pragma: no cover - an async generator that yields nothing
            yield ""

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    with pytest.raises(NativeCliError, match="no machine-readable events"):
        await daemon._run_codex_streaming(adapter, "prompt", "/work", None)


@pytest.mark.asyncio
async def test_run_codex_streaming_propagates_cli_failure(monkeypatch):
    async def fake_stream(invocation, **kwargs):
        raise NativeCliError("native CLI exited 1: boom")
        yield ""  # pragma: no cover

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    with pytest.raises(NativeCliError, match="boom"):
        await daemon._run_codex_streaming(adapter, "prompt", "/work", None)


def test_stream_policy_gate_withholds_refused_text(monkeypatch):
    monkeypatch.setattr(
        daemon,
        "_result_refusal",
        lambda policy, capability, text: ("refused" if "git push" in str(text) else ""),
    )
    gate = daemon._StreamPolicyGate(SimpleNamespace(), object())
    assert gate.allow_text("a harmless checkpoint") is True
    assert gate.allow_text("git") is True  # half a command: allowed so far
    assert gate.allow_text(" push origin main") is False
    assert gate.blocked is True
    assert gate.allow_text("anything after") is False


@pytest.mark.asyncio
async def test_run_codex_streaming_never_sends_a_credential_shape(monkeypatch):
    """The sink scans each message with the shared guard before it is queued: a
    provider-key shape in the middle of a message is never sent, and the text
    channel stops for the rest of the run."""
    secret = "sk-" + "A" * 24
    message = "x" * 1990 + " " + secret + " tail"

    sink, api = await _stream_through_real_sink(
        monkeypatch, [message, "later harmless text"]
    )

    assert secret not in api.texts()
    assert api.texts() == ""
    assert sink.text_disabled is True
    assert sink.disabled is False  # tool names may still flow


@pytest.mark.asyncio
async def test_run_codex_streaming_withholds_env_values_split_across_messages(
    monkeypatch,
):
    """A credential value split between two completed messages must not be
    forwarded as two fragments that the server could store contiguously."""
    from src.llms.native_cli import isolation

    monkeypatch.setattr(
        isolation, "credential_values", lambda overrides=None: ("SUPERSECRETVALUE123",)
    )
    first = "prefix SUPERSECRETVALUE"
    second = "123 tail"

    async def fake_stream(invocation, **kwargs):
        for text in (first, second):
            yield _jsonl(
                type="item.completed",
                item={"type": "agent_message", "text": text},
            )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    streamer = FakeStreamer()

    await daemon._run_codex_streaming(adapter, "prompt", "/work", streamer)

    forwarded = "".join(
        str(event.get("text") or "")
        for event in streamer.events
        if event.get("kind") == "text"
    )
    # The first message ends inside the value, so it is withheld whole: stopping
    # at the second would leave all but its last characters already shown.
    assert forwarded == ""
    assert streamer.text_stopped == "credential_value"


@pytest.mark.parametrize("head", ["SUPER", "S"])
@pytest.mark.asyncio
async def test_a_short_head_of_an_env_value_may_end_a_message_but_not_complete_it(
    monkeypatch, head
):
    """Under six characters a head is not worth a stop; the rest still is."""
    from src.llms.native_cli import isolation

    monkeypatch.setattr(
        isolation, "credential_values", lambda overrides=None: ("SUPERSECRETVALUE123",)
    )
    rest = "SUPERSECRETVALUE123"[len(head) :]

    async def fake_stream(invocation, **kwargs):
        for text in (f"prefix {head}", f"{rest} tail", "later text"):
            yield _jsonl(
                type="item.completed",
                item={"type": "agent_message", "text": text},
            )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    streamer = FakeStreamer()

    await daemon._run_codex_streaming(adapter, "prompt", "/work", streamer)

    forwarded = "".join(
        str(event.get("text") or "")
        for event in streamer.events
        if event.get("kind") == "text"
    )
    assert forwarded == f"prefix {head}"
    assert streamer.text_stopped == "credential_value"


def test_env_continuity_withholds_a_message_that_ends_inside_a_value():
    value = "9f3a7c1d5b2e8a40c6d1f7e39b5a2c84"
    continuity = daemon._EnvContinuity((value,))
    assert continuity.observe("the key is ") is True
    for cut in range(len(value) - 1, 5, -1):  # six characters or more
        fresh = daemon._EnvContinuity((value,))
        assert fresh.observe("the key is " + value[:cut]) is False, cut
        assert fresh.tainted
    for cut in range(1, 6):
        fresh = daemon._EnvContinuity((value,))
        assert fresh.observe("the key is " + value[:cut]) is True, cut
    # A head that is followed by anything else is no prefix of a value at the end.
    assert daemon._EnvContinuity((value,)).observe("note " + value[:12] + " and more") is True
    # The head can start in the window as well as in the message.
    split = daemon._EnvContinuity((value,))
    assert split.observe("ab" + value[:3]) is True
    assert split.observe(value[3:9]) is False


def test_env_continuity_prefix_check_is_cheap_on_degenerate_values():
    value = "a" * 4000
    continuity = daemon._EnvContinuity((value,))
    started = time.perf_counter()
    assert continuity.observe("a" * 3000) is False
    assert time.perf_counter() - started < 1.0


@pytest.mark.asyncio
async def test_run_codex_streaming_taints_on_ansi_interleaved_split(
    monkeypatch,
):
    """An ANSI sequence between the two halves disappears at the server, so
    continuity must compare sanitized visible text; once the split is seen the
    text channel taints and a later tail cannot complete the value."""
    from src.llms.native_cli import isolation

    monkeypatch.setattr(
        isolation, "credential_values", lambda overrides=None: ("SUPERSECRETVALUE123",)
    )
    first = "prefix SUPER"
    second = "\x1b[31mSECRETVALUE123"
    filler = "f" * 2000
    tail = "SECRETVALUE123 tail"

    async def fake_stream(invocation, **kwargs):
        for text in (first, second, filler, tail):
            yield _jsonl(
                type="item.completed",
                item={"type": "agent_message", "text": text},
            )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    streamer = FakeStreamer()

    await daemon._run_codex_streaming(adapter, "prompt", "/work", streamer)

    forwarded = "".join(
        str(event.get("text") or "")
        for event in streamer.events
        if event.get("kind") == "text"
    )
    assert "SUPERSECRETVALUE123" not in forwarded
    assert forwarded == "prefix SUPER"


@pytest.mark.asyncio
async def test_run_codex_streaming_taints_at_a_pem_boundary(monkeypatch):
    """A PEM marker stops the text channel: the message before it was complete
    and clean, nothing at or after the marker is sent."""
    begin = "prefix SUPERSECRETVALUE"
    boundary = "-----BEGIN " "PRIVATE KEY-----"
    body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQ" + "A" * 20
    end = "-----END PRIVATE KEY-----"

    sink, api = await _stream_through_real_sink(
        monkeypatch, [begin, boundary, body, end, "123 tail"]
    )

    assert api.texts() == begin + "\n"
    assert sink.text_disabled is True


@pytest.mark.asyncio
async def test_stream_runner_skips_a_newline_terminated_oversized_record():
    """A record over the transcript cap is skipped even when it is newline
    terminated, matching the unterminated path."""
    import sys

    from src.llms.native_cli.base import CliInvocation
    from src.llms.native_cli.process import stream_process

    code = (
        "import sys\n"
        "sys.stdout.write('z' * (16 * 1024 * 1024 + 1))\n"
        "sys.stdout.write('\\n')\n"
        "sys.stdout.write('small\\n')\n"
    )
    invocation = CliInvocation(argv=(sys.executable, "-c", code), timeout_seconds=20.0)
    lines = [line async for line in stream_process(invocation)]
    assert lines == ["small"]


@pytest.mark.asyncio
async def test_run_codex_streaming_withholds_policy_refused_text(monkeypatch):
    async def fake_stream(invocation, **kwargs):
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "git push origin main"},
        )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    monkeypatch.setattr(
        daemon,
        "_result_refusal",
        lambda policy, capability, text: ("refused" if "git push" in str(text) else ""),
    )
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    streamer = FakeStreamer()
    gate = daemon._StreamPolicyGate(SimpleNamespace(), object())

    await daemon._run_codex_streaming(adapter, "prompt", "/work", streamer, gate=gate)

    assert streamer.events == []
    assert gate.blocked is True


@pytest.mark.asyncio
async def test_output_sink_serializes_batches(monkeypatch):
    """Two flushes must never be in flight at once (the stream cannot reorder)."""
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_BATCH_EVENTS", 1)
    events: list[tuple[str, str]] = []

    def output(
        ticket_id, lease_id, run_id, batch, *, epoch=None, batch_seq=1, timeout=None
    ):
        events.append(("start", batch[0]["text"]))
        time.sleep(0.03)
        events.append(("end", batch[0]["text"]))
        return {"stored": True, "epoch": 5, "tainted": False}

    api = SimpleNamespace(output=output)
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "first"})
    await sink.add({"kind": "text", "text": "second"})
    await asyncio.gather(sink._flush_once(), sink._flush_once())
    await sink.finalize()

    # Each start is followed by its own end before the next start.
    assert events[:4] == [
        ("start", "first\n"),
        ("end", "first\n"),
        ("start", "second\n"),
        ("end", "second\n"),
    ]


@pytest.mark.asyncio
async def test_output_sink_finalize_propagates_cancellation():
    sink = daemon._DaemonOutputStream(FakeApi(), "t", "l", "r")
    sink._task = asyncio.create_task(asyncio.sleep(10))  # noqa: SLF001
    task = asyncio.create_task(sink.finalize())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_codex_streaming_tolerates_a_long_jsonl_record(monkeypatch):
    """The previous runner tolerates records above asyncio's 64 KiB line limit;
    the streaming runner must not turn a valid message into a Worker failure."""
    from src.llms.native_cli.base import CliInvocation

    import sys

    big = "y" * 70_000
    line = _jsonl(
        type="item.completed",
        item={"id": "i1", "type": "agent_message", "text": big},
    )
    # A complete JSONL record above asyncio's default 64 KiB StreamReader line
    # limit; the runner must parse it instead of failing the Worker.
    invocation = CliInvocation(
        argv=(
            sys.executable,
            "-c",
            f"import sys; sys.stdout.write({(line + chr(10))!r})",
        ),
    )
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: invocation)
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.redact_diagnostic",
        lambda text, env: text,
    )

    result = await daemon._run_codex_streaming(adapter, "prompt", "/work", None)

    assert result.text == big


def test_stream_policy_gate_keeps_a_true_trailing_window(monkeypatch):
    """An operation split across a large message and the next one must be
    caught: the gate keeps trailing characters, not whole-message entries."""
    monkeypatch.setattr(
        daemon,
        "_result_refusal",
        lambda policy, capability, text: ("refused" if "git push" in str(text) else ""),
    )
    gate = daemon._StreamPolicyGate(SimpleNamespace(), object())
    padded = (" harmless explanation" * 220) + " The command is gi"
    assert len(padded) > 4096
    assert gate.allow_text(padded) is True
    assert gate.allow_text("t push origin main") is False
    assert gate.blocked is True


@pytest.mark.asyncio
async def test_run_codex_streaming_skips_an_oversized_jsonl_record(monkeypatch):
    """A >16 MiB but valid JSONL record must be drained, not raised: the
    non-streaming runner accepted this transcript and returned its answer."""
    import sys

    code = (
        "import json,sys\n"
        "small=json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'answer'}})\n"
        "big=json.dumps({'type':'item.completed','item':{'type':'command_execution','aggregated_output':'z'*17_000_000}})\n"
        "sys.stdout.write(small+'\\n'+big+'\\n')\n"
    )
    from src.llms.native_cli.base import CliInvocation

    invocation = CliInvocation(argv=(sys.executable, "-c", code))
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: invocation)
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.redact_diagnostic",
        lambda text, env: text,
    )

    result = await daemon._run_codex_streaming(adapter, "prompt", "/work", None)

    assert result.text == "answer"


def test_cap_stream_message_does_not_split_a_token():
    text = "a" * (daemon._OUTPUT_STREAM_TEXT_LIMIT - 5) + "supersecrettoken"
    capped = daemon._cap_stream_message(text)
    assert len(capped) <= daemon._OUTPUT_STREAM_TEXT_LIMIT
    assert "supersecret" not in capped
    assert daemon._cap_stream_message("short message") == "short message"


def test_stream_policy_gate_checks_large_incoming_message_before_trim(
    monkeypatch,
):
    """A long incoming message must be checked against the retained overlap
    before the window is trimmed, or the command's first half is lost."""
    monkeypatch.setattr(
        daemon,
        "_result_refusal",
        lambda policy, capability, text: ("refused" if "git push" in str(text) else ""),
    )
    gate = daemon._StreamPolicyGate(SimpleNamespace(), object())
    first = (" harmless explanation" * 220) + " The command is gi"
    second = "t push origin main " + (" harmless explanation" * 300)
    assert len(first) > 4096
    assert len(second) > 4096
    assert gate.allow_text(first) is True
    assert gate.allow_text(second) is False
    assert gate.blocked is True


@pytest.mark.asyncio
async def test_stream_runner_feeds_stdin_while_draining_large_stdout():
    """A CLI that floods stdout before reading a large prompt must not
    deadlock: stdin feeding happens concurrently with output draining, as in
    the non-streaming runner."""
    import sys

    from src.llms.native_cli.base import CliInvocation
    from src.llms.native_cli.process import stream_process

    code = (
        "import sys\n"
        "sys.stdout.write('x' * (34 * 1024 * 1024))\n"
        "sys.stdout.flush()\n"
        "sys.stdin.read()\n"
        "sys.stdout.write('\\ndone\\n')\n"
    )
    invocation = CliInvocation(
        argv=(sys.executable, "-c", code),
        stdin="p" * 1_000_000,
        timeout_seconds=20.0,
    )
    lines = [line async for line in stream_process(invocation)]
    assert any("done" in line for line in lines)


@pytest.mark.asyncio
async def test_run_codex_streaming_taints_on_any_control_string_opener(
    monkeypatch,
):
    """A DCS opener between the two halves of a credential taints the channel
    on sight of the opener; nothing after it is forwarded."""
    from src.llms.native_cli import isolation

    monkeypatch.setattr(
        isolation, "credential_values", lambda overrides=None: ("SUPERSECRETVALUE123",)
    )
    messages = [
        "prefix SUPER",
        "\x1bP",
        *(["F" * 1100] * 5),
        "\x1b\\",
        "SECRETVALUE123 tail",
    ]

    sink, api = await _stream_through_real_sink(monkeypatch, messages)

    assert "SUPERSECRETVALUE123" not in api.texts()
    assert api.texts() == "prefix SUPER\n"
    assert sink.text_disabled is True


@pytest.mark.asyncio
async def test_run_codex_streaming_taints_on_a_split_control_opener(monkeypatch):
    """ESC and the opener head arriving in separate messages also taint."""
    from src.llms.native_cli import isolation

    monkeypatch.setattr(
        isolation, "credential_values", lambda overrides=None: ("SUPERSECRETVALUE123",)
    )

    sink, api = await _stream_through_real_sink(
        monkeypatch, ["prefix SUPER", "\x1b", "PSECRETVALUE123 tail", "later text"]
    )

    assert "SUPERSECRETVALUE123" not in api.texts()
    assert api.texts() == "prefix SUPER\n"
    assert sink.text_disabled is True


@pytest.mark.asyncio
async def test_daemon_output_disables_after_a_not_stored_ack():
    """A 200 with stored:false drops the batch; later text must not be sent."""
    api = FakeApi(ack={"stored": False, "epoch": 7, "events": 1})
    stream = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    await stream.add({"kind": "text", "text": "prefix SUPERSECRETVALUE"})
    assert await stream._flush_once() is False
    await stream.add({"kind": "text", "text": "123 tail"})
    assert await stream._flush_once() is True
    assert len(api.calls) == 1
    assert stream.disabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ack", "reason"),
    [
        (None, "bad_ack"),
        ("ok", "bad_ack"),
        # Clean flags with no usable epoch: the epoch check is what stops these,
        # so each carries the flags the real server sends and no more.
        ({"stored": True, "tainted": False}, "bad_epoch"),
        ({"stored": True, "tainted": False, "epoch": None}, "bad_epoch"),
        ({"stored": True, "tainted": False, "epoch": 0}, "bad_epoch"),
        ({"stored": True, "tainted": False, "epoch": -3}, "bad_epoch"),
        ({"stored": True, "tainted": False, "epoch": True}, "bad_epoch"),
        ({"stored": True, "tainted": False, "epoch": "7"}, "bad_epoch"),
        ({"stored": True, "tainted": False, "epoch": 7.0}, "bad_epoch"),
        # A taint verdict is only honoured on a valid epoch.
        ({"stored": True, "tainted": True, "epoch": 0}, "bad_epoch"),
    ],
)
async def test_daemon_output_disables_on_an_ack_without_a_valid_epoch(ack, reason):
    class OddApi(FakeApi):
        def output(self, *args, **kwargs):
            return ack

    stream = daemon._DaemonOutputStream(OddApi(), "t", "l", "r")
    await stream.add({"kind": "text", "text": "hello"})
    assert await stream._flush_once() is False
    assert stream.disabled is True
    assert stream.stop_reason == reason


@pytest.mark.asyncio
async def test_daemon_output_echoes_the_server_epoch_with_a_sequential_batch_seq(
    monkeypatch,
):
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_BATCH_EVENTS", 1)
    api = FakeApi()
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    for word in ("one", "two", "three"):
        await stream.add({"kind": "text", "text": word})
    await stream._flush_all()
    assert api.fence == [(None, 1), (7, 2), (7, 3)]
    assert stream.epoch == 7 and stream.disabled is False


@pytest.mark.asyncio
async def test_daemon_output_stops_when_the_server_changes_the_epoch(monkeypatch):
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_BATCH_EVENTS", 1)
    api = FakeApi()
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    await stream.add({"kind": "text", "text": "one"})
    await stream.add({"kind": "text", "text": "two"})
    await stream.add({"kind": "text", "text": "three"})
    assert await stream._flush_once() is True
    api.epoch = 8  # another worker/epoch took over
    assert await stream._flush_once() is False
    assert stream.disabled is True
    assert stream.stop_reason == "epoch_mismatch"
    assert await stream._flush_once() is True  # nothing more is sent
    assert len(api.calls) == 2


@pytest.mark.asyncio
async def test_daemon_output_stops_on_a_stale_epoch_rejection():
    api = FakeApi(error=daemon.ApiError("POST ... -> HTTP 409 stale_output_epoch", 409))
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    await stream.add({"kind": "text", "text": "hello"})
    await stream.add({"kind": "tool", "tools": ["bash"]})
    assert await stream._flush_once() is False
    assert stream.disabled is True and stream.text_disabled is True
    await stream.add({"kind": "tool", "tools": ["bash"]})
    assert not stream._pending  # noqa: SLF001


@pytest.mark.asyncio
async def test_daemon_output_taint_ack_stops_everything_that_follows():
    api = FakeApi(
        ack={"stored": False, "epoch": 7, "tainted": True, "reason": "credential_key"}
    )
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    await stream.add({"kind": "text", "text": "password"})
    await stream.add({"kind": "tool", "tools": ["bash"]})
    assert await stream._flush_once() is False
    assert stream.text_disabled is True
    await stream.add({"kind": "text", "text": "never sent"})
    await stream.add({"kind": "tool", "tools": ["python"]})
    assert not stream._pending  # noqa: SLF001
    await stream._flush_all()
    sent = [event for call in api.calls for event in call[3]]
    assert all("never sent" not in str(event) for event in sent)
    assert {"kind": "tool", "tools": ["python"]} not in sent


@pytest.mark.asyncio
async def test_daemon_output_discontinuity_stops_text_and_keeps_tools():
    api = FakeApi()
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    await stream.add({"kind": "text", "text": "before"})
    stream.discontinuity("oversized_record")
    await stream.add({"kind": "text", "text": "after"})
    await stream.add({"kind": "tool", "tools": ["bash"]})
    await stream._flush_all()
    assert stream.text_disabled is True and stream.disabled is False
    assert stream.stop_reason == "discontinuity:oversized_record"
    # What was queued before the gap is a complete clean prefix and is sent;
    # nothing after the gap is, tool names included.
    assert api.texts() == "before\n"
    assert [e["kind"] for call in api.calls for e in call[3]] == ["text", "stop"]


@pytest.mark.asyncio
async def test_run_codex_streaming_stops_text_when_the_runner_reports_a_gap(
    monkeypatch,
):
    async def fake_stream(invocation, *, on_discontinuity=None, **kwargs):
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "before the gap"},
        )
        on_discontinuity("oversized_record")  # a record was skipped here
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "after the gap"},
        )
        yield TOOL_STARTED

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()

    assert "after the gap" not in api.texts()
    assert sink.text_disabled is True
    # Only the clean prefix before the skipped record was sent.
    assert api.texts() == "before the gap\n"
    assert not any(
        event.get("kind") == "tool" for call in api.calls for event in call[3]
    )


@pytest.mark.asyncio
async def test_daemon_output_splits_a_long_message_at_whitespace():
    api = FakeApi()
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    message = " ".join(["word"] * 1500)  # ~7.5 KB, several transport events
    await stream.add({"kind": "text", "text": message})
    await stream._flush_all()
    assert stream.text_disabled is False
    sent = [event["text"] for call in api.calls for event in call[3]]
    assert len(sent) >= 4
    assert all(text[-1].isspace() and len(text) <= 2000 for text in sent)
    assert "".join(sent) == message + "\n"


@pytest.mark.asyncio
async def test_daemon_output_stops_on_an_unbroken_run_longer_than_one_event():
    api = FakeApi()
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    await stream.add({"kind": "text", "text": "x" * 6000})
    await stream.add({"kind": "text", "text": "later text"})
    await stream._flush_once()
    assert api.calls == []
    assert stream.text_disabled is True


@pytest.mark.asyncio
async def test_daemon_output_stops_on_a_message_over_the_transport_cap():
    api = FakeApi()
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    await stream.add(
        {"kind": "text", "text": "word " * (daemon._OUTPUT_STREAM_TEXT_LIMIT // 4)}
    )
    await stream.add({"kind": "text", "text": "later text"})
    await stream._flush_once()
    assert api.calls == []
    assert stream.text_disabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        # The old window-rollover repros: a key, filler beyond any window, then
        # the separator and value.
        ["Cookie: pad=" + "x" * 400, " " * 1900, " SID=review-cookie-secret"],
        ['"Cookie": pad=' + "x" * 400, " " * 1900, " SID=review-cookie-secret"],
        # Pending key with more than 16 KiB of blanks before the separator.
        ["password", *([" " * 2000] * 9), "=reviewsecret"],
        # Bearer scheme, its token and a tail in separate messages.
        ["prefix Bearer ", "abcdef0123456789abcdef", " tail"],
        # A JWT header, payload and signature in separate messages.
        ["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "SflKxwRJSMeKKF2QT4fw"],
        # Split ANSI sequences.
        ["prefix SUPERSECRETVA\x1b[", "31m", "LUE123 tail"],
    ],
)
async def test_daemon_output_never_sends_a_credential_continuation(messages):
    api = FakeApi()
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    for message in messages:
        await stream.add({"kind": "text", "text": message})
    await stream._flush_all()

    sent = api.texts()
    for secret in ("review-cookie-secret", "reviewsecret", "abcdef0123456789abcdef"):
        assert secret not in sent
    assert "SflKxwRJSMeKKF2QT4fw" not in sent
    assert "LUE123" not in sent
    assert stream.text_disabled is True


@pytest.mark.asyncio
async def test_malformed_stdout_record_stops_text_instead_of_being_skipped(monkeypatch):
    """Valid text, a corrupt line, valid text: the corrupt line is a gap in what
    the CLI said, so the second message must not be published."""

    async def fake_stream(invocation, **kwargs):
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "before the corrupt line"},
        )
        yield '{"type": "item.completed", "item": {"type": "agent_mess'
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "after the corrupt line"},
        )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()

    assert api.texts() == "before the corrupt line\n"
    assert sink.text_disabled is True
    assert sink.stop_reason == "discontinuity:malformed_record"


@pytest.mark.asyncio
async def test_blank_lines_and_filtered_metadata_are_not_corruption(monkeypatch):
    async def fake_stream(invocation, **kwargs):
        yield ""
        yield THREAD_STARTED
        yield TOOL_RESULT
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "still streaming"},
        )
        yield TURN_COMPLETED

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()
    assert sink.text_disabled is False
    assert api.texts() == "still streaming\n"


@pytest.mark.asyncio
async def test_a_local_stop_tells_the_server_so_watchers_get_a_reset():
    api = FakeApi()
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    await stream.add({"kind": "text", "text": "clean first message"})
    await stream.add({"kind": "text", "text": "Authorization: x"})  # local refusal
    await stream._flush_all()
    sent = [event for call in api.calls for event in call[3]]
    assert [event["kind"] for event in sent] == ["text", "stop"]
    assert api.texts() == "clean first message\n"
    # One marker, no matter how many later refusals or discontinuities.
    stream.discontinuity("oversized_record")
    await stream.add({"kind": "tool", "tools": ["bash"]})
    await stream._flush_all()
    kinds = [event["kind"] for call in api.calls for event in call[3]]
    assert kinds == ["text", "stop"]


@pytest.mark.asyncio
async def test_no_stop_marker_when_nothing_was_ever_sent_or_the_server_tainted():
    api = FakeApi()
    quiet = daemon._DaemonOutputStream(api, "t", "l", "r")
    await quiet.add({"kind": "text", "text": "password: x"})  # refused up front
    await quiet._flush_all()
    assert api.calls == []

    tainted_api = FakeApi(ack={"stored": False, "epoch": 7, "tainted": True})
    stream = daemon._DaemonOutputStream(tainted_api, "t", "l", "r")
    await stream.add({"kind": "text", "text": "hello"})
    await stream._flush_once()
    assert stream.text_disabled is True
    await stream._flush_all()
    assert all(
        event["kind"] != "stop" for call in tainted_api.calls for event in call[3]
    )


@pytest.mark.asyncio
async def test_a_stop_during_the_first_post_is_still_announced():
    """The first batch has left the queue and its epoch is unknown while the
    request is in flight; a discontinuity then must still queue the stop."""
    import threading

    started = threading.Event()
    release = threading.Event()
    calls: list[list[dict]] = []

    class BarrierApi:
        def output(
            self, ticket_id, lease_id, run_id, events, *, epoch=None, batch_seq=1, timeout=None
        ):
            calls.append(list(events))
            if len(calls) == 1:
                started.set()
                assert release.wait(5)
            return {"stored": True, "epoch": 9, "tainted": False}

    stream = daemon._DaemonOutputStream(BarrierApi(), "t", "l", "r")
    await stream.add({"kind": "text", "text": "clean prefix"})
    first = asyncio.create_task(stream._flush_once())
    while not started.is_set():
        await asyncio.sleep(0.005)
    assert stream.epoch is None and not stream._pending  # noqa: SLF001
    stream.discontinuity("oversized_record")  # the race
    release.set()
    assert await first is True
    await stream._flush_all()

    kinds = [event["kind"] for batch in calls for event in batch]
    assert kinds == ["text", "stop"]
    assert stream.text_disabled is True and stream.disabled is False


@pytest.mark.asyncio
async def test_a_multi_chunk_message_cannot_publish_its_suffix_after_an_overflow():
    """Reviewer repro: a safe prefix is flushed, 255 tool names plus one text
    event fill the queue, then a long message arrives. The overflow stops text
    at the first chunk; no later chunk of the message may be queued behind the
    hole, and the stop marker must survive the cleanup."""
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "safe prefix"})
    assert await sink._flush_once() is True
    for index in range(daemon._OUTPUT_STREAM_MAX_EVENTS - 1):
        await sink.add({"kind": "tool", "tools": [f"t{index}"]})
    await sink.add({"kind": "text", "text": "one more"})
    await sink.add({"kind": "text", "text": "tail " * 1000})
    await sink.add({"kind": "text", "text": "tail " * 1000})

    assert sink.text_disabled is True and sink.stop_reason == "buffer_overflow"
    kinds = [event["kind"] for event in sink._pending]  # noqa: SLF001
    assert kinds.count("stop") == 1
    assert "text" not in kinds
    await sink._flush_all()
    for _ in range(20):  # the remaining tool names drain batch by batch
        await sink._flush_all()
    sent = [event for call in api.calls for event in call[3]]
    assert [e["text"] for e in sent if e["kind"] == "text"] == ["safe prefix\n"]
    assert sum(1 for e in sent if e["kind"] == "stop") == 1
    assert "tail" not in api.texts()


@pytest.mark.asyncio
async def test_a_repeated_stop_never_removes_the_queued_marker():
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "prefix"})
    assert await sink._flush_once() is True
    sink.stop_text("first")
    sink.stop_text("second", drop_pending=True)
    sink.stop_text("third", drop_pending=True)
    assert [event["kind"] for event in sink._pending] == ["stop"]  # noqa: SLF001


@pytest.mark.asyncio
async def test_the_sink_counts_tool_names_in_its_local_window():
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "tool", "tools": ["Bearer"]})
    await sink.add({"kind": "text", "text": "abcdefgh"})  # would read as a token
    await sink._flush_all()
    assert api.texts() == ""
    assert sink.text_disabled is True
    sent = [event["kind"] for call in api.calls for event in call[3]]
    assert sent == ["tool", "stop"]

    other = FakeApi()
    sink = daemon._DaemonOutputStream(other, "t", "l", "r")
    await sink.add({"kind": "text", "text": "prefix Bearer "})
    await sink.add({"kind": "tool", "tools": ["abcdefgh12345678"]})
    await sink._flush_all()
    assert other.texts() == "prefix Bearer "
    sent = [event["kind"] for call in other.calls for event in call[3]]
    assert sent == ["text", "stop"]  # the tool name itself was never sent


@pytest.mark.asyncio
async def test_the_sink_judges_reasoning_and_text_projections_separately():
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "password"})
    await sink.add({"kind": "reasoning", "text": "pausing to think"})
    await sink.add({"kind": "text", "text": "=ordinary-value"})
    await sink._flush_all()
    assert "ordinary-value" not in api.texts()
    assert sink.text_disabled is True


@pytest.mark.asyncio
async def test_after_a_stop_no_tool_name_is_sent_at_all():
    """`Basic` in one event and its token in the next would form a projection no
    window tracks, so names stop with the text."""
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "Authorization: x"})  # refused locally
    assert sink.text_disabled is True
    for name in ("Basic", "dGVzdDp1c2VyOnBhc3N3b3Jk", "pass\u200bword: synthetic_value"):
        await sink.add({"kind": "tool", "tools": [name]})
    await sink._flush_all()
    assert api.calls == []


@pytest.mark.asyncio
async def test_a_policy_refusal_stops_the_sink_and_announces_it(monkeypatch):
    """Safe prefix, then a denied operation, then later text: the sink must stop
    (and say so) instead of forwarding the later text behind a hole."""

    async def fake_stream(invocation, **kwargs):
        for text in ("safe prefix", "git push origin main", "later text"):
            yield _jsonl(
                type="item.completed", item={"type": "agent_message", "text": text}
            )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    monkeypatch.setattr(
        daemon,
        "_result_refusal",
        lambda policy, capability, text: ("refused" if "git push" in str(text) else ""),
    )
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    gate = daemon._StreamPolicyGate(SimpleNamespace(), object())
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink, gate=gate)
    await sink.finalize()

    assert api.texts() == "safe prefix\n"
    assert sink.text_disabled is True and sink.stop_reason == "policy_refused"
    assert [e["kind"] for call in api.calls for e in call[3]] == ["text", "stop"]


def test_split_stream_message_marks_only_a_synthetic_final_newline():
    assert daemon._split_stream_message("done.") == [("done.\n", True)]
    assert daemon._split_stream_message("done.\n") == [("done.\n", False)]
    assert daemon._split_stream_message("") == []
    many = daemon._split_stream_message(" ".join(["word"] * 1200))
    assert [join for _, join in many] == [False] * (len(many) - 1) + [True]
    assert all(text[-1].isspace() for text, _ in many)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "messages",
    [
        ["eyJ", "hbGciOiJIUzI1NiJ9", " tail"],
        ["Bearer", " abcdef0123456789", " tail"],
        ["-----BEG", "IN PRIVATE KEY-----", "body"],
        ["password", ": hunter2value"],
    ],
)
async def test_a_credential_split_over_messages_is_never_sent_whole(messages):
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    for message in messages:
        await sink.add({"kind": "text", "text": message})
    await sink._flush_all()
    sent = api.texts()
    assert sink.text_disabled is True
    for fragment in ("hbGciOiJIUzI1NiJ9", "abcdef0123456789", "IN PRIVATE", "hunter2value"):
        assert fragment not in sent
    first = [event for call in api.calls for event in call[3]][0]
    assert first.get("join") is True  # the head was sent, flagged as joined


@pytest.mark.asyncio
async def test_the_sink_flags_only_messages_that_needed_a_synthetic_newline():
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await sink.add({"kind": "text", "text": "ends cleanly\n"})
    await sink.add({"kind": "text", "text": "ends mid-word"})
    await sink._flush_all()
    sent = [event for call in api.calls for event in call[3]]
    assert "join" not in sent[0] and sent[1]["join"] is True


@pytest.mark.asyncio
async def test_the_sink_judges_the_displayed_and_the_written_text():
    """`word`, `Bearer`, ` abcdef...` as three model messages."""
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    for message in ("word", "Bearer", " abcdef0123456789", " tail"):
        await sink.add({"kind": "text", "text": message})
    await sink._flush_all()
    assert "abcdef0123456789" not in api.texts() and "tail" not in api.texts()
    assert sink.text_disabled is True
    assert api.texts() == "word\nBearer\n"


@pytest.mark.asyncio
async def test_repeated_unterminated_introducers_do_not_stall_the_event_loop(
    monkeypatch,
):
    """Reviewer repro: sanitizing a 32 KiB message of repeated `ESC P` with no
    terminator took seconds and froze cancellation, lease handling and flushing.
    Controls are now refused before any per-character processing."""
    import time as _time

    hostile = "\x1bP" * 16_000

    async def fake_stream(invocation, **kwargs):
        yield _jsonl(
            type="item.completed", item={"type": "agent_message", "text": hostile}
        )
        yield _jsonl(
            type="item.completed",
            item={"type": "agent_message", "text": "\x1b]" * 16_000},
        )

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    ticks: list[float] = []

    async def ticker():
        while True:
            ticks.append(_time.monotonic())
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    started = _time.monotonic()
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink)
    elapsed = _time.monotonic() - started
    task.cancel()
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert elapsed < 1.0
    assert not gaps or max(gaps) < 0.5  # the loop kept turning
    assert api.texts() == "" and sink.text_disabled is True


@pytest.mark.asyncio
async def test_upstream_redaction_of_a_key_word_stops_text_through_the_real_runner(
    monkeypatch,
):
    """Reviewer repro, through the actual boundary (no stubbed stream): an
    environment credential whose value is `password` is redacted out of the
    transcript line by the runner, which would hide the key from the guards.
    The runner reports the redaction, so the daemon stops text."""
    import sys

    from src.llms.native_cli.base import CliInvocation

    lines = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "password"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": " " * 3000}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "=unknown-value"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "safe tail"}},
    ]
    code = (
        "import json,sys\n"
        f"for row in {lines!r}:\n"
        "    sys.stdout.write(json.dumps(row) + '\\n')\n"
    )
    invocation = CliInvocation(argv=(sys.executable, "-c", code), timeout_seconds=20.0)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: invocation)
    monkeypatch.setattr(
        "src.llms.native_cli.process.redact_diagnostic",
        lambda text, env: text.replace("password", "[redacted]"),
    )
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "t", "l", "r")
    await daemon._run_codex_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()

    assert "unknown-value" not in api.texts() and "safe tail" not in api.texts()
    assert sink.text_disabled is True
    assert sink.stop_reason == "discontinuity:redacted"


@pytest.mark.asyncio
@pytest.mark.parametrize("tainted", [True, 1, "yes", {"x": 1}])
async def test_any_truthy_tainted_flag_in_an_ack_stops_the_stream(tainted):
    api = FakeApi(ack={"stored": True, "epoch": 7, "tainted": tainted})
    stream = daemon._DaemonOutputStream(api, "t", "l", "r")
    await stream.add({"kind": "text", "text": "hello"})
    assert await stream._flush_once() is False
    assert stream.text_disabled is True


@pytest.mark.asyncio
async def test_concurrent_adds_keep_the_guard_equal_to_the_state_of_the_queue():
    """A second producer must not overwrite the guard advance of the first: the
    sink's copy has to be the state the server holds after the queued events."""
    from src.server.utils import output_guard

    api = FakeApi()
    stream = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    long_text = ("word " * 410).strip()  # two chunks: the text path yields between them
    await asyncio.gather(
        stream.add({"kind": "text", "text": long_text}),
        stream.add({"kind": "tool", "tools": ["Bearer"]}),
    )

    state = output_guard.GuardState()
    for event in stream._pending:
        if event["kind"] == "tool":
            result = output_guard.scan(
                state, "\n".join(event["tools"]) + "\n", channel="tool"
            )
        else:
            result = output_guard.scan(
                state, event["text"], channel=event["kind"], join=bool(event.get("join"))
            )
        assert result.ok, event
        state = result.state
    assert stream._guard == state
    assert stream.stop_reason == ""


class _SlowApi(FakeApi):
    """An alive server that answers every batch, slowly."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay

    def output(self, *args, **kwargs):
        time.sleep(self.delay)
        return super().output(*args, **kwargs)


async def _queue_many(sink, count: int) -> None:
    for index in range(count):
        await sink.add({"kind": "text", "text": f"line {index}"})


@pytest.mark.asyncio
async def test_finalize_is_bounded_however_many_batches_a_slow_server_would_take(
    monkeypatch,
):
    """250 queued events are 8 batches; at 0.2 s each the flush would take 1.6 s.
    The whole final flush has one budget, and what is left is dropped."""
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_FINAL_FLUSH_S", 0.3)
    api = _SlowApi(0.2)
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    await _queue_many(sink, 250)
    assert len(sink._pending) == 250  # noqa: SLF001

    started = time.monotonic()
    await sink.finalize()
    elapsed = time.monotonic() - started

    assert elapsed < 0.9
    assert 1 <= len(api.calls) < 8
    assert sink.disabled is True and sink.stop_reason == "flush_timeout"
    await sink.add({"kind": "text", "text": "after"})  # nothing more is queued
    assert not sink._pending  # noqa: SLF001


@pytest.mark.asyncio
async def test_finalize_keeps_a_normal_flush_complete_within_the_budget():
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    await _queue_many(sink, 70)
    await sink.finalize()
    assert sum(len(call[3]) for call in api.calls) == 70
    assert sink.disabled is False and sink.stop_reason == ""


@pytest.mark.asyncio
async def test_finalize_without_a_flush_sends_nothing_and_does_not_wait(monkeypatch):
    """A lost lease or a torn-down run: the stop acknowledgement comes first."""
    monkeypatch.setattr(daemon, "_OUTPUT_STREAM_FLUSH_S", 0.01)
    api = _SlowApi(0.5)
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    sink.start()
    await _queue_many(sink, 100)
    await asyncio.sleep(0.05)  # the flusher has a request in flight

    started = time.monotonic()
    await sink.finalize(flush=False)

    assert time.monotonic() - started < 0.3
    assert not sink._pending  # noqa: SLF001
    assert len(api.calls) <= 1
    await asyncio.sleep(0.6)  # the thread of the abandoned request finishes alone
    assert len(api.calls) <= 1


def test_codex_app_server_transport_defaults_on_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_CODEX_APP_SERVER", raising=False)
    assert daemon._codex_app_server_enabled() is True
    monkeypatch.setenv("CONDUCTOR_CODEX_APP_SERVER", "0")
    assert daemon._codex_app_server_enabled() is False


@pytest.mark.asyncio
async def test_run_codex_app_server_forwards_token_deltas(monkeypatch):
    class FakeRuntime:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.cancelled = False

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            for event in (
                SimpleNamespace(
                    kind="text_delta", text="TOK", session_id="thread-1",
                    tool_name=None, data={},
                ),
                SimpleNamespace(
                    kind="text_delta", text="EN", session_id="thread-1",
                    tool_name=None, data={},
                ),
                SimpleNamespace(
                    kind="tool_started", text="", session_id="thread-1",
                    tool_name="shell", data={},
                ),
                SimpleNamespace(
                    kind="usage", text="", session_id="thread-1",
                    tool_name=None,
                    data={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                ),
                SimpleNamespace(
                    kind="turn_completed", text="", session_id="thread-1",
                    tool_name=None, data={},
                ),
            ):
                yield event

        async def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    adapter = SimpleNamespace(
        binary="/fake/codex",
        _model="gpt-test",
        _timeout_seconds=10.0,
        _read_only=True,
        _outer_sandboxed=True,
        _mcp_servers=[],
    )
    streamer = FakeStreamer()

    result = await daemon._run_codex_app_server_streaming(
        adapter, "prompt", "/work", streamer
    )

    assert result.text == "TOKEN"
    assert result.session_id == "thread-1"
    assert streamer.events[0]["kind"] == "text"
    assert streamer.events[0]["text"] == "TOK"
    assert streamer.events[1]["kind"] == "text"
    assert streamer.events[1]["text"] == "EN"
    assert streamer.events[2] == {"kind": "tool", "tools": ["shell"]}


@pytest.mark.asyncio
async def test_app_server_report_survives_a_disabled_stream(monkeypatch):
    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            for event in (
                SimpleNamespace(
                    kind="text_delta", text="partial ", session_id="thread-1",
                    tool_name=None, data={},
                ),
                SimpleNamespace(
                    kind="text_delta", text="AUTONOMY_STATE: blocked\n",
                    session_id="thread-1", tool_name=None, data={},
                ),
                SimpleNamespace(
                    kind="text_delta", text="AUTONOMY_QUESTION: confirm?\n",
                    session_id="thread-1", tool_name=None, data={},
                ),
                SimpleNamespace(
                    kind="turn_completed", text="", session_id="thread-1",
                    tool_name=None, data={},
                ),
            ):
                yield event

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    result = await daemon._run_codex_app_server_streaming(
        adapter, "prompt", "/work", None
    )

    assert "AUTONOMY_STATE: blocked" in result.text
    assert "AUTONOMY_QUESTION: confirm?" in result.text


@pytest.mark.asyncio
async def test_app_server_deltas_do_not_gain_synthetic_newlines(monkeypatch):
    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            for text in ("TOK", "EN"):
                yield SimpleNamespace(
                    kind="text_delta", text=text, session_id="thread-1",
                    tool_name=None, data={},
                )
            yield SimpleNamespace(
                kind="turn_completed", text="", session_id="thread-1",
                tool_name=None, data={},
            )

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")
    sink.start()

    await daemon._run_codex_app_server_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()
    assert api.texts() == "TOKEN"


@pytest.mark.asyncio
async def test_app_server_large_delta_is_split_not_tainted(monkeypatch):
    text = ("word " * 1600).strip()  # 8000 chars, whitespace at the split point

    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            yield SimpleNamespace(
                kind="text_delta", text=text, session_id="thread-1",
                tool_name=None, data={},
            )
            yield SimpleNamespace(
                kind="turn_completed", text="", session_id="thread-1",
                tool_name=None, data={},
            )

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )
    api = FakeApi()
    sink = daemon._DaemonOutputStream(api, "ticket-1", "lease-1", "run-1")

    await daemon._run_codex_app_server_streaming(adapter, "prompt", "/work", sink)
    await sink.finalize()

    assert sink.disabled is False
    assert api.texts() == text


@pytest.mark.asyncio
async def test_app_server_report_is_not_truncated_by_the_live_cap(monkeypatch):
    huge = ("x" * 70000) + "\\nAUTONOMY_STATE: blocked\\nAUTONOMY_QUESTION: confirm?\\n"

    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            yield SimpleNamespace(
                kind="text_delta", text=huge, session_id="thread-1",
                tool_name=None, data={},
            )
            yield SimpleNamespace(
                kind="turn_completed", text="", session_id="thread-1",
                tool_name=None, data={},
            )

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    result = await daemon._run_codex_app_server_streaming(
        adapter, "prompt", "/work", None
    )

    assert "AUTONOMY_STATE: blocked" in result.text
    assert "AUTONOMY_QUESTION: confirm?" in result.text


@pytest.mark.asyncio
async def test_app_server_failure_before_output_falls_back_to_exec(monkeypatch):
    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            raise NativeCliError("app-server unavailable")
            yield  # pragma: no cover

        async def cancel(self):
            pass

    async def fake_exec(*args, **kwargs):
        return SimpleNamespace(text="fallback answer", session_id="s", usage=None, events=1)

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    monkeypatch.setattr(daemon, "_run_codex_streaming", fake_exec)
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    result = await daemon._run_codex_app_server_streaming(
        adapter, "prompt", "/work", None
    )

    assert result.text == "fallback answer"


@pytest.mark.asyncio
async def test_app_server_failure_after_output_does_not_fall_back(monkeypatch):
    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            yield SimpleNamespace(
                kind="text_delta", text="visible", session_id="thread-1",
                tool_name=None, data={},
            )
            raise NativeCliError("late failure")

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    fallback_called = False

    async def fake_exec(*args, **kwargs):
        nonlocal fallback_called
        fallback_called = True
        return SimpleNamespace(text="fallback", session_id="s", usage=None, events=1)

    monkeypatch.setattr(daemon, "_run_codex_streaming", fake_exec)
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    with pytest.raises(NativeCliError, match="late failure"):
        await daemon._run_codex_app_server_streaming(
            adapter, "prompt", "/work", FakeStreamer()
        )
    assert fallback_called is False


def test_app_server_circuit_opens_after_repeated_failures(monkeypatch):
    monkeypatch.setattr(daemon, "_APP_SERVER_FAILURES", 0)
    monkeypatch.setattr(daemon, "_APP_SERVER_OPEN_UNTIL", 0.0)
    monkeypatch.setattr(daemon, "_APP_SERVER_OPEN", False)
    monkeypatch.setenv("CONDUCTOR_CODEX_APP_SERVER_FAILURE_LIMIT", "2")
    monkeypatch.setenv("CONDUCTOR_CODEX_APP_SERVER_COOLDOWN", "60")

    assert daemon._app_server_available() is True
    daemon._record_app_server_failure()
    assert daemon._app_server_available() is True
    daemon._record_app_server_failure()
    assert daemon._app_server_available() is False
    daemon._record_app_server_success()
    assert daemon._app_server_available() is True


@pytest.mark.asyncio
async def test_cancellation_does_not_count_as_an_app_server_failure(monkeypatch):
    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    daemon._reset_app_server_circuit()
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    with pytest.raises(asyncio.CancelledError):
        await daemon._run_codex_app_server_streaming(
            adapter, "prompt", "/work", None
        )
    assert daemon._APP_SERVER_FAILURES == 0
    daemon._reset_app_server_circuit()


def test_the_failure_classifier_counts_only_transport_failures():
    """G3: turn failures (server ``error`` events) and cancels never count."""
    assert daemon._app_server_failure_counts(asyncio.CancelledError()) is False
    assert (
        daemon._app_server_failure_counts(
            daemon._app_server_turn_error("model exploded")
        )
        is False
    )
    # Transport: startup, RPC, reader and protocol failures, and the empty
    # answer, all count.
    assert (
        daemon._app_server_failure_counts(
            NativeCliError("codex app-server initialize request timed out")
        )
        is True
    )
    assert (
        daemon._app_server_failure_counts(
            NativeCliError("codex app-server thread/start request failed")
        )
        is True
    )
    assert (
        daemon._app_server_failure_counts(
            NativeCliError("codex app-server reader stopped (exit 1)")
        )
        is True
    )
    assert (
        daemon._app_server_failure_counts(
            NativeCliError(
                "codex app-server requested approval in a non-interactive run"
            )
        )
        is True
    )
    assert (
        daemon._app_server_failure_counts(
            NativeCliError("codex app-server produced no final answer")
        )
        is True
    )
    assert daemon._app_server_failure_counts(OSError("spawn failed")) is True


@pytest.mark.asyncio
async def test_a_completed_silent_turn_neither_counts_nor_falls_back(monkeypatch):
    """A completed turn without a message is a missing report, not a transport
    failure: no circuit count and no exec re-run of the whole turn."""
    daemon._reset_app_server_circuit()

    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            yield SimpleNamespace(
                kind="turn_completed", text="", session_id="thread-1",
                tool_name=None, data={},
            )

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    successes: list[bool] = []
    monkeypatch.setattr(
        daemon, "_record_app_server_success", lambda: successes.append(True)
    )
    fallbacks: list[str] = []

    async def fake_exec(*args, **kwargs):
        fallbacks.append("exec")
        return SimpleNamespace(
            text="exec answer", session_id="s", usage=None, events=1
        )

    monkeypatch.setattr(daemon, "_run_codex_streaming", fake_exec)
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    from src.llms.native_cli.base import NativeCliNoFinalAnswer

    with pytest.raises(NativeCliNoFinalAnswer):
        await daemon._run_codex_app_server_streaming(adapter, "prompt", "/work", None)

    assert fallbacks == []
    assert daemon._APP_SERVER_FAILURES == 0
    assert successes == [True]
    daemon._reset_app_server_circuit()


@pytest.mark.asyncio
async def test_empty_answer_after_a_published_tool_raises_without_counting(
    monkeypatch,
):
    daemon._reset_app_server_circuit()

    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            yield SimpleNamespace(
                kind="tool_started", text="", session_id="thread-1",
                tool_name="shell", data={},
            )
            yield SimpleNamespace(
                kind="turn_completed", text="", session_id="thread-1",
                tool_name=None, data={},
            )

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )

    async def fake_exec(*args, **kwargs):
        raise AssertionError("a published stream must not fall back")

    monkeypatch.setattr(daemon, "_run_codex_streaming", fake_exec)
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    from src.llms.native_cli.base import NativeCliNoFinalAnswer

    with pytest.raises(NativeCliNoFinalAnswer, match="no final answer"):
        await daemon._run_codex_app_server_streaming(
            adapter, "prompt", "/work", FakeStreamer()
        )
    assert daemon._APP_SERVER_FAILURES == 0
    daemon._reset_app_server_circuit()


def test_a_completed_turn_without_a_message_never_counts_on_the_circuit():
    from src.llms.native_cli.base import NativeCliNoFinalAnswer

    assert (
        daemon._app_server_failure_counts(
            NativeCliNoFinalAnswer("codex app-server produced no final answer")
        )
        is False
    )


@pytest.mark.asyncio
async def test_a_model_error_event_does_not_count_and_falls_back(monkeypatch):
    daemon._reset_app_server_circuit()

    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            yield SimpleNamespace(
                kind="error", text="model exploded", session_id="thread-1",
                tool_name=None, data={},
            )

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    fallbacks: list[str] = []

    async def fake_exec(*args, **kwargs):
        fallbacks.append("exec")
        return SimpleNamespace(
            text="exec answer", session_id="s", usage=None, events=1
        )

    monkeypatch.setattr(daemon, "_run_codex_streaming", fake_exec)
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    result = await daemon._run_codex_app_server_streaming(
        adapter, "prompt", "/work", None
    )

    assert result.text == "exec answer"
    assert fallbacks == ["exec"]
    assert daemon._APP_SERVER_FAILURES == 0
    daemon._reset_app_server_circuit()


@pytest.mark.asyncio
async def test_a_model_error_event_after_publication_raises_but_does_not_count(
    monkeypatch,
):
    daemon._reset_app_server_circuit()

    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            yield SimpleNamespace(
                kind="text_delta", text="visible", session_id="thread-1",
                tool_name=None, data={},
            )
            yield SimpleNamespace(
                kind="error", text="model exploded", session_id="thread-1",
                tool_name=None, data={},
            )

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )

    async def fake_exec(*args, **kwargs):
        raise AssertionError("a published stream must not fall back")

    monkeypatch.setattr(daemon, "_run_codex_streaming", fake_exec)
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    with pytest.raises(NativeCliError, match="model exploded"):
        await daemon._run_codex_app_server_streaming(
            adapter, "prompt", "/work", FakeStreamer()
        )
    assert daemon._APP_SERVER_FAILURES == 0
    daemon._reset_app_server_circuit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "match"),
    [
        (NativeCliError("codex app-server turn/start request timed out"), "timed out"),
        (NativeCliError("codex app-server reader stopped (exit 1)"), "reader stopped"),
    ],
)
async def test_transport_failures_count_toward_the_circuit(monkeypatch, error, match):
    daemon._reset_app_server_circuit()
    monkeypatch.setenv("CONDUCTOR_CODEX_APP_SERVER_FALLBACK", "0")

    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            raise error
            yield  # pragma: no cover

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    adapter = SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )

    with pytest.raises(NativeCliError, match=match):
        await daemon._run_codex_app_server_streaming(
            adapter, "prompt", "/work", None
        )
    assert daemon._APP_SERVER_FAILURES == 1
    assert daemon._app_server_available() is True
    daemon._reset_app_server_circuit()


def test_the_circuit_opens_after_consecutive_transport_failures_with_one_warning(
    monkeypatch, caplog
):
    daemon._reset_app_server_circuit()
    monkeypatch.setenv("CONDUCTOR_CODEX_APP_SERVER_FAILURE_LIMIT", "2")
    monkeypatch.setenv("CONDUCTOR_CODEX_APP_SERVER_COOLDOWN", "60")

    with caplog.at_level(logging.WARNING, logger="scripts.conductor_local_daemon"):
        daemon._record_app_server_failure()
        assert daemon._app_server_available() is True
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        daemon._record_app_server_failure()
    assert daemon._app_server_available() is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "2 consecutive" in message
    assert "60" in message
    assert "re-enable at" in message
    daemon._reset_app_server_circuit()


def test_the_circuit_reopens_after_the_cooldown_with_one_info(monkeypatch, caplog):
    daemon._reset_app_server_circuit()
    monkeypatch.setenv("CONDUCTOR_CODEX_APP_SERVER_FAILURE_LIMIT", "1")
    monkeypatch.setenv("CONDUCTOR_CODEX_APP_SERVER_COOLDOWN", "60")
    daemon._record_app_server_failure()
    assert daemon._app_server_available() is False

    monkeypatch.setattr(daemon, "_APP_SERVER_OPEN_UNTIL", time.monotonic() - 1.0)
    with caplog.at_level(logging.INFO, logger="scripts.conductor_local_daemon"):
        assert daemon._app_server_available() is True
        # A second look at an already closed circuit stays quiet.
        assert daemon._app_server_available() is True
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    daemon._reset_app_server_circuit()


def test_a_success_logs_info_only_when_it_resets_a_degraded_circuit(
    monkeypatch, caplog
):
    daemon._reset_app_server_circuit()
    with caplog.at_level(logging.INFO, logger="scripts.conductor_local_daemon"):
        daemon._record_app_server_success()
        assert not [r for r in caplog.records if r.levelno == logging.INFO]
        daemon._record_app_server_failure()
        daemon._record_app_server_success()
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 1
    daemon._reset_app_server_circuit()


def test_circuit_logs_are_visible_in_a_default_started_process(monkeypatch):
    """B2: both close paths must show up without any logging configuration.

    The subprocess starts like the real daemon does — no caplog, no handler
    setup, no level overrides — so this proves the module's own handler makes
    the INFO lines observable, and that nothing from the environment (a
    planted prompt marker included) leaks into the log lines.
    """
    worktree = str(Path(daemon.__file__).resolve().parents[1])
    script = "\n".join(
        [
            "import sys, time",
            f"sys.path.insert(0, {worktree!r})",
            "import os",
            'os.environ["CONDUCTOR_CODEX_APP_SERVER_FAILURE_LIMIT"] = "1"',
            'os.environ["CONDUCTOR_CODEX_APP_SERVER_COOLDOWN"] = "60"',
            "from scripts import conductor_local_daemon as daemon",
            "daemon._record_app_server_failure()",
            "assert daemon._app_server_available() is False",
            "daemon._APP_SERVER_OPEN_UNTIL = time.monotonic() - 1.0",
            "assert daemon._app_server_available() is True",
            "daemon._APP_SERVER_FAILURES = 1",
            "daemon._record_app_server_success()",
        ]
    )
    env = dict(os.environ)
    env["CONDUCTOR_TEST_PROMPT_MARKER"] = "SECRET-PROMPT-TEXT-MARKER"
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    stderr = result.stderr
    assert "WARNING" in stderr
    assert "codex app-server circuit open after 1 consecutive transport" in stderr
    assert "INFO" in stderr
    assert (
        "codex app-server circuit closed after the cooldown; "
        "codex turns use the app-server transport again"
    ) in stderr
    assert (
        "codex app-server circuit closed after a successful turn; "
        "codex turns use the app-server transport again"
    ) in stderr
    assert "SECRET-PROMPT-TEXT-MARKER" not in stderr


_MALFORMED_FAKE = r'''#!/usr/bin/env python3
import json, sys

def reply(req, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req, "result": result}) + "\n")
    sys.stdout.flush()

def emit(method, params):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    req = msg.get("id")
    if method == "initialize":
        reply(req, {})
    elif method == "thread/start":
        reply(req, {"thread": {"id": "fake-thread"}})
    elif method == "turn/start":
        prompt = msg["params"]["input"][0]["text"]
        reply(req, {"turn": {"id": "turn-1"}})
        if prompt == "garbage now":
            sys.stdout.write("not-json{\n")
        elif prompt == "array now":
            sys.stdout.write("[1, 2, 3]\n")
        elif prompt == "text then garbage":
            emit("item/agentMessage/delta", {"itemId": "msg-1", "delta": "hello "})
            sys.stdout.write("not-json{\n")
        sys.stdout.flush()
        sys.exit(0)
'''


def _malformed_binary(tmp_path) -> str:
    path = tmp_path / "codex-malformed"
    path.write_text(_MALFORMED_FAKE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.mark.asyncio
async def test_malformed_frame_before_output_falls_back_and_counts(
    monkeypatch, tmp_path
):
    """B1, unpublished: the parse failure goes through the real reader into
    the daemon, counts once, and falls back to exec exactly once."""
    daemon._reset_app_server_circuit()
    adapter = SimpleNamespace(
        binary=_malformed_binary(tmp_path), _model="gpt-test",
        _timeout_seconds=10.0, _read_only=True, _outer_sandboxed=True,
        _mcp_servers=[],
    )
    successes: list[bool] = []
    monkeypatch.setattr(
        daemon, "_record_app_server_success", lambda: successes.append(True)
    )
    fallbacks: list[str] = []

    async def fake_exec(*args, **kwargs):
        fallbacks.append("exec")
        return SimpleNamespace(
            text="exec answer", session_id="s", usage=None, events=1
        )

    monkeypatch.setattr(daemon, "_run_codex_streaming", fake_exec)

    result = await daemon._run_codex_app_server_streaming(
        adapter, "garbage now", str(tmp_path), None
    )

    assert result.text == "exec answer"
    assert fallbacks == ["exec"]
    assert daemon._APP_SERVER_FAILURES == 1
    assert successes == []
    daemon._reset_app_server_circuit()


@pytest.mark.asyncio
async def test_malformed_frame_after_output_raises_and_counts(monkeypatch, tmp_path):
    """B1, published: the delta was already streamed, so the surfaced reader
    failure propagates (no fallback) and still counts."""
    daemon._reset_app_server_circuit()
    adapter = SimpleNamespace(
        binary=_malformed_binary(tmp_path), _model="gpt-test",
        _timeout_seconds=10.0, _read_only=True, _outer_sandboxed=True,
        _mcp_servers=[],
    )
    successes: list[bool] = []
    monkeypatch.setattr(
        daemon, "_record_app_server_success", lambda: successes.append(True)
    )

    async def fake_exec(*args, **kwargs):
        raise AssertionError("a published stream must not fall back")

    monkeypatch.setattr(daemon, "_run_codex_streaming", fake_exec)
    streamer = FakeStreamer()

    with pytest.raises(NativeCliError) as captured:
        await daemon._run_codex_app_server_streaming(
            adapter, "text then garbage", str(tmp_path), streamer
        )

    # The delta really was published before the reader died on the frame.
    assert streamer.events and streamer.events[0]["text"] == "hello "
    # The surfaced transport error carries no raw frame content.
    assert "not-json" not in str(captured.value)
    assert daemon._APP_SERVER_FAILURES == 1
    assert successes == []
    daemon._reset_app_server_circuit()


# --- Completion report: only the final assistant message is the report ------


_REPORT = json.dumps(
    {
        "status": "done",
        "summary": "Implemented.",
        "artifacts": {"commits": [], "files": [], "pr": None},
        "verification": [],
        "next": "",
        "handbacks": [],
    }
)


@pytest.mark.asyncio
async def test_run_codex_streaming_returns_the_final_message_apart(monkeypatch):
    async def fake_stream(invocation, **kwargs):
        for line in (
            THREAD_STARTED,
            _jsonl(
                type="item.completed",
                item={"type": "agent_message", "text": "Working on it."},
            ),
            _jsonl(type="item.completed", item={"type": "agent_message", "text": _REPORT}),
            TURN_COMPLETED,
        ):
            yield line

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())

    result = await daemon._run_codex_streaming(adapter, "prompt", "/work", None)

    assert result.text == f"Working on it.\n{_REPORT}"
    assert result.final_text == _REPORT
    assert daemon.parse_report(result.final_text)["state"] == "ready"
    assert daemon.parse_report(result.text)["state"] == "waiting"


@pytest.mark.asyncio
async def test_run_codex_streaming_completed_turn_without_message_has_no_report(
    monkeypatch,
):
    from src.llms.native_cli.base import NativeCliNoFinalAnswer

    async def fake_stream(invocation, **kwargs):
        for line in (THREAD_STARTED, TURN_COMPLETED):
            yield line

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())

    with pytest.raises(NativeCliNoFinalAnswer, match="no final answer"):
        await daemon._run_codex_streaming(adapter, "prompt", "/work", None)


@pytest.mark.asyncio
async def test_run_codex_streaming_failed_turn_without_message_stays_a_failure(
    monkeypatch,
):
    from src.llms.native_cli.base import NativeCliNoFinalAnswer

    async def fake_stream(invocation, **kwargs):
        for line in (
            THREAD_STARTED,
            _jsonl(type="error", message="You've hit your usage limit."),
            _jsonl(type="turn.failed", error={"message": "usage limit"}),
        ):
            yield line

    monkeypatch.setattr("src.llms.native_cli.process.stream_process", fake_stream)
    adapter = SimpleNamespace(build_invocation=lambda prompt, cwd=None: object())

    with pytest.raises(NativeCliError, match="no final answer") as raised:
        await daemon._run_codex_streaming(adapter, "prompt", "/work", None)
    assert not isinstance(raised.value, NativeCliNoFinalAnswer)


def _app_server_events(*items, completed=True):
    events = []
    for item_id, text in items:
        for part in (text[: len(text) // 2], text[len(text) // 2 :]):
            events.append(
                SimpleNamespace(
                    kind="text_delta", text=part, session_id="thread-1",
                    tool_name=None, data={"item_id": item_id},
                )
            )
    if completed:
        events.append(
            SimpleNamespace(
                kind="turn_completed", text="", session_id="thread-1",
                tool_name=None, data={},
            )
        )
    return events


def _fake_app_server(monkeypatch, events):
    class FakeRuntime:
        def __init__(self, **kwargs):
            pass

        async def stream_turn(self, prompt, *, session_id=None, cwd=None):
            for event in events:
                yield event

        async def cancel(self):
            pass

    monkeypatch.setattr(
        "src.server.services.native_runtime.codex_app_server.CodexAppServerRuntime",
        FakeRuntime,
    )
    return SimpleNamespace(
        binary="/fake/codex", _model="gpt-test", _timeout_seconds=10.0,
        _read_only=True, _outer_sandboxed=True, _mcp_servers=[],
    )


@pytest.mark.asyncio
async def test_app_server_final_text_is_the_last_agent_message(monkeypatch):
    adapter = _fake_app_server(
        monkeypatch,
        _app_server_events(("msg-1", "Working on it. "), ("msg-2", _REPORT)),
    )

    result = await daemon._run_codex_app_server_streaming(
        adapter, "prompt", "/work", None
    )

    assert result.text == "Working on it. " + _REPORT
    assert result.final_text == _REPORT
    assert daemon.parse_report(result.final_text)["state"] == "ready"


@pytest.mark.asyncio
async def test_app_server_completed_turn_without_text_has_no_report(monkeypatch):
    from src.llms.native_cli.base import NativeCliNoFinalAnswer

    monkeypatch.setattr(daemon, "_codex_app_server_fallback_enabled", lambda: False)
    adapter = _fake_app_server(monkeypatch, _app_server_events())

    with pytest.raises(NativeCliNoFinalAnswer):
        await daemon._run_codex_app_server_streaming(adapter, "prompt", "/work", None)


@pytest.mark.asyncio
async def test_app_server_stream_ending_without_completion_stays_a_failure(
    monkeypatch,
):
    from src.llms.native_cli.base import NativeCliNoFinalAnswer

    monkeypatch.setattr(daemon, "_codex_app_server_fallback_enabled", lambda: False)
    adapter = _fake_app_server(monkeypatch, _app_server_events(completed=False))

    with pytest.raises(NativeCliError, match="no final answer") as raised:
        await daemon._run_codex_app_server_streaming(adapter, "prompt", "/work", None)
    assert not isinstance(raised.value, NativeCliNoFinalAnswer)
