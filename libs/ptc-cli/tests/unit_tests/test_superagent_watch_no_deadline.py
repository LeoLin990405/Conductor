"""`/jobs watch` keeps following past the server's 15-minute stream ceiling."""

from __future__ import annotations

from typing import Any

import pytest

from ptc_cli.superagent import shell as shell_module
from ptc_cli.superagent.client import ActivityStreamEvent, ActivityStreamUnavailableError, SuperAgentError
from ptc_cli.superagent.shell import SuperAgentShell


def _sse(event: str, data: dict[str, Any] | None = None, event_id: str | None = None):
    return ActivityStreamEvent(event=event, data=data or {}, event_id=event_id)


class RotatingClient:
    """Each round is one connection; ``timeout`` is the server's 900 s frame."""

    def __init__(self, rounds: list, tickets: list | None = None) -> None:
        self.rounds = list(rounds)
        self.tickets = list(tickets or [])
        self.stream_calls: list[str | None] = []
        self.poll_calls = 0

    def stream_activity(self, ticket_id: str, *, last_event_id: str | None = None):
        self.stream_calls.append(last_event_id)
        events = self.rounds.pop(0)

        async def stream():
            for item in events:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return stream()

    async def ticket(self, ticket_id: str) -> dict[str, Any]:
        self.poll_calls += 1
        item = self.tickets.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def activity(self, ticket_id: str) -> dict[str, Any]:
        return {"events": []}


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(shell_module, "_SSE_ROTATE_DELAY_S", 0.0)
    monkeypatch.setattr(shell_module, "_SSE_BACKOFF_BASE_S", 0.0)
    monkeypatch.setattr(shell_module, "_SSE_BACKOFF_MAX_S", 0.0)
    monkeypatch.setattr(shell_module, "_POLL_INTERVAL_S", 0.0)


def _shell(client) -> SuperAgentShell:
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = client  # type: ignore[assignment]
    return shell


@pytest.mark.asyncio
async def test_a_server_timeout_frame_reattaches_instead_of_ending_the_watch(capsys):
    # More rotations than the reconnect budget: a rotation is not a failure.
    rotations = shell_module._SSE_RECONNECT_ATTEMPTS + 3
    rounds = [
        [_sse("output", {"kind": "text", "text": f"p{n} "}, f"a~run-1~{n}"), _sse("timeout")]
        for n in range(rotations)
    ]
    rounds.append(
        [
            _sse("status", {"status": "completed", "summary": "all done"}, "a~run-1~99"),
            _sse("end", {"status": "completed", "summary": "all done"}, "a~run-1~99"),
        ]
    )
    client = RotatingClient(rounds)

    await _shell(client).watch_job("ticket-1")

    out = capsys.readouterr().out
    assert "watch timed out" not in out
    assert "all done" in out
    assert client.stream_calls == [None] + [f"a~run-1~{n}" for n in range(rotations)]
    assert client.poll_calls == 0


@pytest.mark.asyncio
async def test_the_default_watch_has_no_deadline(monkeypatch, capsys):
    """The old default gave up 900 s after the start, rotation or not."""
    clock = {"now": 0.0}
    import time as real_time

    class FakeTime:
        def __getattr__(self, name):
            return getattr(real_time, name)

        @staticmethod
        def monotonic() -> float:
            return clock["now"]

    # Only the shell's clock moves; asyncio keeps the real one.
    monkeypatch.setattr(shell_module, "time", FakeTime())

    class SlowClient(RotatingClient):
        def stream_activity(self, ticket_id, *, last_event_id=None):
            clock["now"] += 901.0
            return super().stream_activity(ticket_id, last_event_id=last_event_id)

    client = SlowClient(
        [
            [_sse("output", {"kind": "text", "text": "x"}, "e1"), _sse("timeout")],
            [_sse("output", {"kind": "text", "text": "y"}, "e2"), _sse("timeout")],
            [_sse("end", {"status": "completed", "summary": "finished late"}, "e3")],
        ]
    )

    await _shell(client).watch_job("ticket-1")

    out = capsys.readouterr().out
    assert "finished late" in out
    assert "watch timed out" not in out
    assert len(client.stream_calls) == 3


@pytest.mark.asyncio
async def test_polling_fallback_rides_out_a_transient_outage(capsys):
    client = RotatingClient(
        [[ActivityStreamUnavailableError("HTTP 404")]],
        tickets=[
            {"status": "running"},
            SuperAgentError("cannot reach http://test: connection refused"),
            SuperAgentError("GET /api/v1/autonomy/tickets/t -> 503: busy"),
            {"status": "running"},
            {"status": "completed", "summary": "ok"},
        ],
    )

    await _shell(client).watch_job("ticket-1")

    out = capsys.readouterr().out
    assert "lost contact" in out
    assert "completed" in out
    assert client.poll_calls == 5


@pytest.mark.asyncio
async def test_polling_fallback_ends_on_a_permanent_error():
    client = RotatingClient(
        [[ActivityStreamUnavailableError("HTTP 404")]],
        tickets=[SuperAgentError("GET /api/v1/autonomy/tickets/t -> 404: Ticket not found")],
    )
    with pytest.raises(SuperAgentError):
        await _shell(client).watch_job("ticket-1")


@pytest.mark.asyncio
async def test_an_explicit_timeout_is_still_honoured(capsys):
    client = RotatingClient(
        [[ActivityStreamUnavailableError("HTTP 404")]],
        tickets=[{"status": "running"}] * 1000,
    )
    await _shell(client).watch_job("ticket-1", timeout=0.0)
    assert "watch timed out" in capsys.readouterr().out
