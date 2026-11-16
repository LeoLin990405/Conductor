"""The Super-agent shell maps commands to the real-ish API surface."""

from __future__ import annotations

import re
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

# The ptc-cli lock does not install the repo's conductor_cli. The line shell
# imports it through ptc_cli.superagent.text, so a fresh `uv run` venv cannot
# collect this module until src is on the path.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.client import ActivityStreamUnavailableError  # noqa: E402
from ptc_cli.superagent.shell import SuperAgentShell  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.base_url = "http://test"
        self.user_id = "u1"

    async def close(self) -> None:
        pass

    async def overview(self) -> dict[str, Any]:
        return {"counts": {"attention": 1}, "sources": 2, "enabled_sources": 1}

    async def tickets(self, **params: Any) -> dict[str, Any]:
        self.calls.append(("tickets", params))
        return {"tickets": [{"ticket_id": "ticket-1", "status": "failed", "title": "Fix"}]}

    async def ticket(self, ticket_id: str) -> dict[str, Any]:
        return {
            "ticket_id": ticket_id,
            "status": "failed",
            "title": "Fix",
            "attention": {"question": "Need input"},
            "reports": [],
        }

    async def activity(self, ticket_id: str) -> dict[str, Any]:
        return {"events": [{"event_id": "e1", "kind": "created", "summary": "created"}]}

    async def home_workspace(self) -> str:
        return "home"

    async def create_task(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create", prompt, kwargs))
        return {"ticket_id": "ticket-new", "status": "queued", "title": prompt}

    async def reply(self, ticket_id: str, text: str) -> dict[str, Any]:
        self.calls.append(("reply", ticket_id, text))
        return {"ticket_id": ticket_id}


@pytest.mark.asyncio
async def test_jobs_flags_reach_the_ticket_endpoint():
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    await shell.list_jobs(["--status", "failed", "--attention", "--limit", "5"])
    assert shell._jobs[0]["ticket_id"] == "ticket-1"
    assert shell.client.calls[0] == (
        "tickets",
        {"limit": 5, "status": "failed", "attention": "true"},
    )


def test_an_index_selects_the_last_listing():
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell._jobs = [{"ticket_id": "ticket-1"}, {"ticket_id": "ticket-2"}]
    assert shell.target("2") == "ticket-2"
    assert shell.target("ticket-3") == "ticket-3"


@pytest.mark.asyncio
async def test_home_stores_the_displayed_ticket_order_for_indexes():
    from unittest.mock import AsyncMock

    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client.tickets = AsyncMock(
        return_value={
            "tickets": [
                {"ticket_id": "t1", "status": "running", "title": "Recent"},
                {"ticket_id": "t2", "status": "failed", "title": "Older"},
            ]
        }
    )
    await shell.show_home()
    assert shell.target("1") == "t1"
    assert shell.target("2") == "t2"


@pytest.mark.asyncio
async def test_job_index_follows_the_rendered_roster():
    from unittest.mock import AsyncMock

    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client.tickets = AsyncMock(
        return_value={
            "tickets": [
                {"ticket_id": "blocked-1", "status": "blocked", "title": "Blocked"},
                {"ticket_id": "failed-2", "status": "failed", "title": "Failed"},
            ]
        }
    )
    await shell.list_jobs(["--failed", "--status", "failed"])
    assert shell.target("1") == "failed-2"

    shell.client.tickets = AsyncMock(
        return_value={
            "tickets": [
                {"ticket_id": "running-1", "status": "running", "title": "Run"},
                {
                    "ticket_id": "need-2",
                    "status": "needs-you",
                    "title": "Need",
                    "attention": {"question": "?"},
                },
            ]
        }
    )
    await shell.list_jobs(["--attention"])
    assert shell.target("1") == "need-2"


@pytest.mark.asyncio
async def test_plain_text_creates_a_ticket_without_blocking_the_prompt():
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = FakeClient()
    shell.client = fake  # type: ignore[assignment]
    await shell.handle("Fix the button")
    create = next(call for call in fake.calls if call[0] == "create")
    assert create[1] == "Fix the button"
    assert create[2]["workspace_id"] == "home"


@pytest.mark.asyncio
async def test_a_non_integer_limit_is_a_clean_command_error():
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    with pytest.raises(Exception, match="--limit must be an integer"):
        await shell.list_jobs(["--limit", "abc"])


@pytest.mark.asyncio
async def test_markup_in_a_server_title_does_not_crash_rendering():
    class MarkupClient(FakeClient):
        async def tickets(self, **params):
            return {"tickets": [{"ticket_id": "t", "status": "queued", "title": "[/bold] x"}]}

    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = MarkupClient()  # type: ignore[assignment]
    await shell.list_jobs([])


def test_ticket_text_is_escaped_before_rich_markup():
    from ptc_cli.superagent.shell import _text

    assert _text("[bold]not markup[/bold]") == r"\[bold]not markup\[/bold]"


def test_terminal_control_sequences_are_removed():
    from ptc_cli.superagent.shell import _text

    assert _text("\x1b[2J\x1b]52;c;ZXZpbA==\x07safe") == "safe"


def test_osc_hyperlinks_keep_their_visible_text():
    from ptc_cli.superagent.shell import _text

    hyperlink = "\x1b]8;;https://example.com\x1b\\before\x1b]8;;\x1b\\after"
    assert _text(hyperlink) == "beforeafter"


@pytest.mark.asyncio
async def test_reply_accepts_tabs_and_preserves_the_message():
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = FakeClient()
    shell.client = fake  # type: ignore[assignment]
    await shell.handle("/jobs reply ticket-1\tanswer with spaces")
    assert ("reply", "ticket-1", "answer with spaces") in fake.calls


def test_age_reads_timezone_aware_timestamps():
    from datetime import datetime, timedelta

    from ptc_cli.superagent.shell import _age

    moment = datetime.now(UTC) - timedelta(hours=1)
    assert _age(moment.isoformat()) == "1h"


def test_jobs_lines_ages_timestamp_only_api_rows():
    from datetime import datetime, timedelta

    from ptc_cli.superagent.shell import _age, jobs_lines

    hour = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    minutes = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    text = "\n".join(
        jobs_lines(
            [
                {"ticket_id": "t1", "status": "running", "title": "Recent", "updated_at": hour},
                {"ticket_id": "t2", "status": "queued", "title": "Queued", "updated_at": minutes},
            ],
            120,
            "en",
        )
    )
    assert _age(hour) in text
    assert _age(minutes) in text


def test_relative_worktree_root_is_normalized(monkeypatch, tmp_path):
    from conductor_cli import lines

    monkeypatch.chdir(tmp_path)
    # The current name wins over the legacy alias. Clear it so this test
    # still checks that a relative CONDUCTOR_WT_ROOT resolves against cwd.
    monkeypatch.delenv("CONDUCTOR_WORKTREE_ROOT", raising=False)
    monkeypatch.setenv("CONDUCTOR_WT_ROOT", "fleet")
    assert lines.worktree_root() == (tmp_path / "fleet").resolve()


# ---------------------------------------------------------------------------
# /jobs watch over the live Ticket activity stream
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict[str, Any] | None = None, event_id: str | None = None):
    from ptc_cli.superagent.client import ActivityStreamEvent

    return ActivityStreamEvent(event=event, data=data or {}, event_id=event_id)


class StreamClient(FakeClient):
    """A client whose stream_activity replays scripted rounds of events."""

    def __init__(self, rounds: list) -> None:
        super().__init__()
        self.rounds = list(rounds)
        self.stream_calls: list[tuple[str, str | None]] = []
        self.poll_calls: list[tuple[str, Any]] = []

    def stream_activity(self, ticket_id: str, *, last_event_id: str | None = None):
        self.stream_calls.append((ticket_id, last_event_id))
        round_events = self.rounds.pop(0) if self.rounds else [_sse("end", {"status": "completed", "summary": "done"})]

        async def stream():
            if isinstance(round_events, BaseException):
                raise round_events
            for item in round_events:
                if isinstance(item, BaseException):
                    raise item
                yield item

        return stream()

    async def ticket(self, ticket_id: str) -> dict[str, Any]:
        self.poll_calls.append(("ticket", ticket_id))
        return {
            "ticket_id": ticket_id,
            "status": "failed",
            "title": "Fix",
            "attention": {"question": "Need input"},
            "reports": [],
        }

    async def activity(self, ticket_id: str) -> dict[str, Any]:
        self.poll_calls.append(("activity", ticket_id))
        return {"events": [{"event_id": "e1", "kind": "created", "summary": "created"}]}


def _fast_reconnect(monkeypatch):
    from ptc_cli.superagent import shell as shell_module

    monkeypatch.setattr(shell_module, "_SSE_BACKOFF_BASE_S", 0.0)
    monkeypatch.setattr(shell_module, "_SSE_BACKOFF_MAX_S", 0.0)


@pytest.mark.asyncio
async def test_watch_job_streams_activity_tokens_and_terminal_status(capsys):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse(
                    "activity",
                    {
                        "event_id": "created:t1",
                        "kind": "created",
                        "at": "2026-11-04T12:00:00+00:00",
                        "summary": "Fix the button",
                    },
                    "created:t1~~0",
                ),
                _sse("output", {"kind": "text", "text": "work"}),
                _sse(
                    "status",
                    {"status": "completed", "summary": "done", "question": ""},
                    "created:t1~~8",
                ),
                # The terminal status is not the end of the stream: a Worker
                # can persist its report and keep emitting output.
                _sse("output", {"kind": "text", "text": "ing"}, "created:t1~~9"),
                _sse("end", {"status": "completed", "summary": "done"}, "created:t1~~9"),
            ]
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "Fix the button" in out
    assert "working" in out
    assert "completed" in out
    assert fake.stream_calls == [("ticket-1", None)]
    # The live stream satisfied the watch: no activity polling was needed.
    assert fake.poll_calls == []


@pytest.mark.asyncio
async def test_watch_job_reconnects_with_the_last_event_id(capsys, monkeypatch):
    from ptc_cli.superagent.client import ActivityStreamUnavailableError

    _fast_reconnect(monkeypatch)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "part"}, "a~run-1~4"),
                ActivityStreamUnavailableError("connection reset"),
            ],
            [
                _sse(
                    "status",
                    {"status": "completed", "summary": "done", "question": ""},
                    "a~run-1~5",
                ),
                _sse("end", {"status": "completed", "summary": "done"}, "a~run-1~5"),
            ],
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    assert fake.stream_calls == [("ticket-1", None), ("ticket-1", "a~run-1~4")]
    assert "part" in capsys.readouterr().out
    assert fake.poll_calls == []


@pytest.mark.asyncio
async def test_watch_job_falls_back_to_polling_when_the_stream_is_missing(capsys, monkeypatch):
    from ptc_cli.superagent.client import ActivityStreamUnavailableError

    _fast_reconnect(monkeypatch)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient([ActivityStreamUnavailableError("HTTP 404")])
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "polling the event record" in out
    assert "failed" in out
    assert "Need input" in out
    assert fake.poll_calls == [("ticket", "ticket-1"), ("activity", "ticket-1")]
    assert len(fake.stream_calls) == 1


@pytest.mark.asyncio
async def test_watch_job_reconnect_attempts_are_bounded_then_poll(capsys, monkeypatch):
    from ptc_cli.superagent.client import ActivityStreamUnavailableError

    _fast_reconnect(monkeypatch)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "x"}, f"a~run-1~{index}"),
                ActivityStreamUnavailableError("reset"),
            ]
            for index in range(shell_module_attempts() + 2)
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    from ptc_cli.superagent import shell as shell_module

    assert len(fake.stream_calls) == shell_module._SSE_RECONNECT_ATTEMPTS + 1
    assert fake.poll_calls == [("ticket", "ticket-1"), ("activity", "ticket-1")]
    assert "failed" in capsys.readouterr().out


def shell_module_attempts() -> int:
    from ptc_cli.superagent import shell as shell_module

    return shell_module._SSE_RECONNECT_ATTEMPTS


def test_token_output_strips_terminal_controls_without_markup_escaping(capsys):
    from ptc_cli.superagent.shell import _plain

    hostile = "\x1b[2J\x1b]52;c;ZXZpbA==\x07safe [bold]not markup[/bold]"
    assert _plain(hostile) == "safe [bold]not markup[/bold]"
    assert "\x1b" not in _plain(hostile)


@pytest.mark.asyncio
async def test_watch_job_output_is_sanitized(capsys, monkeypatch):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse(
                    "output",
                    {"kind": "text", "text": "\x1b[2Jtoken\x1b]52;c;eA==\x07"},
                ),
                _sse("end", {"status": "completed", "summary": ""}, "a~r~1"),
            ]
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "token" in out
    assert "\x1b" not in out


# ---------------------------------------------------------------------------
# SSE wire parsing and transport fallbacks in the client
# ---------------------------------------------------------------------------


def test_sse_block_parses_id_event_and_json_data():
    from ptc_cli.superagent.client import parse_sse_block

    frame = parse_sse_block('id: created:t1~run-1~7\nevent: output\ndata: {"kind": "text", "text": "hi"}')
    assert frame is not None
    assert frame.event_id == "created:t1~run-1~7"
    assert frame.event == "output"
    assert frame.data == {"kind": "text", "text": "hi"}


def test_sse_keepalive_comment_is_skipped():
    from ptc_cli.superagent.client import parse_sse_block

    assert parse_sse_block(": ping") is None
    assert parse_sse_block("") is None


def test_sse_multiline_data_is_joined():
    from ptc_cli.superagent.client import parse_sse_block

    frame = parse_sse_block('event: activity\ndata: {"a":\ndata: 1}')
    assert frame is not None
    assert frame.data == {"a": 1}


def test_sse_bad_json_raises_stream_unavailable():
    from ptc_cli.superagent.client import (
        ActivityStreamUnavailableError,
        parse_sse_block,
    )

    with pytest.raises(ActivityStreamUnavailableError):
        parse_sse_block("event: output\ndata: not-json")


def _mock_client(handler):
    import httpx

    from ptc_cli.superagent.client import SuperAgentClient

    client = SuperAgentClient("http://test", "u1")
    client._client = httpx.AsyncClient(
        base_url="http://test",
        headers={"X-User-Id": "u1"},
        transport=httpx.MockTransport(handler),
    )
    return client


@pytest.mark.asyncio
async def test_client_stream_activity_parses_frames_and_sends_last_event_id():
    import httpx

    seen: dict[str, str] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["id"] = request.headers.get("last-event-id", "")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(b'id: created:t1~run-1~1\nevent: output\ndata: {"kind": "text", "text": "hi"}\n\n: ping\n\n'),
        )

    client = _mock_client(handler)
    events = [event async for event in client.stream_activity("t1", last_event_id="prior")]
    await client.close()

    assert seen["id"] == "prior"
    assert [(event.event, event.event_id, event.data) for event in events] == [("output", "created:t1~run-1~1", {"kind": "text", "text": "hi"})]


@pytest.mark.asyncio
async def test_client_stream_activity_missing_route_is_unavailable():
    import httpx

    from ptc_cli.superagent.client import ActivityStreamUnavailableError

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    client = _mock_client(handler)
    with pytest.raises(ActivityStreamUnavailableError):
        async for _ in client.stream_activity("t1"):
            pass
    await client.close()


@pytest.mark.asyncio
async def test_client_stream_activity_non_sse_body_is_unavailable():
    import httpx

    from ptc_cli.superagent.client import ActivityStreamUnavailableError

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ticket_id": "t1", "events": []})

    client = _mock_client(handler)
    with pytest.raises(ActivityStreamUnavailableError):
        async for _ in client.stream_activity("t1"):
            pass
    await client.close()


@pytest.mark.asyncio
async def test_watch_job_reconnects_through_the_real_client_with_last_event_id(capsys, monkeypatch):
    import httpx

    _fast_reconnect(monkeypatch)
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            # A clean close without an end frame: the watch must re-attach.
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(b'id: created:t1~run-1~1\nevent: output\ndata: {"kind": "text", "text": "par"}\n\n'),
            )
        assert request.headers.get("last-event-id") == "created:t1~run-1~1"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b"id: created:t1~run-1~2\nevent: status\n"
                b'data: {"status": "completed", "summary": "done", "question": ""}\n\n'
                b"id: created:t1~run-1~2\nevent: end\n"
                b'data: {"status": "completed", "summary": "done", "question": ""}\n\n'
            ),
        )

    client = _mock_client(handler)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = client  # type: ignore[assignment]
    try:
        await shell.watch_job("ticket-1", timeout=5.0)
    finally:
        await client.close()

    out = capsys.readouterr().out
    assert "par" in out
    assert "completed" in out
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_watch_job_keeps_tail_output_until_the_end_frame(capsys):
    """The terminal status can precede the Worker's final tokens.

    The watch must stay attached until the server's end frame.
    """
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "early "}),
                _sse(
                    "status",
                    {"status": "completed", "summary": "done", "question": ""},
                    "a~r~1",
                ),
                _sse("output", {"kind": "text", "text": "tail"}, "a~r~2"),
                _sse("end", {"status": "completed", "summary": "done"}, "a~r~2"),
            ]
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "early tail" in out
    # The terminal status is printed once, even though status and end agree.
    assert out.count("completed") == 1
    assert fake.stream_calls == [("ticket-1", None)]
    assert fake.poll_calls == []


def test_token_sanitizer_removes_osc_with_either_terminator():
    from ptc_cli.superagent.shell import _TokenSanitizer

    # BEL-terminated OSC split between its payload and terminator.
    bel = _TokenSanitizer()
    assert bel.feed("\x1b]0;SYNTHETIC_OSC_MARKER") == ""
    assert bel.feed("\x07tail") == "tail"
    assert bel.flush() == ""

    # ESC-backslash (ST) terminator split before the backslash.
    st = _TokenSanitizer()
    assert st.feed("\x1b]0;SYNTHETIC_OSC_MARKER\x1b") == ""
    assert st.feed("\\tail") == "tail"
    assert st.flush() == ""

    # Both complete inside one chunk.
    whole = _TokenSanitizer()
    assert whole.feed("\x1b]0;SECRET\x07a\x1b]0;SECRET\x1b\\b") == "ab"
    assert whole.flush() == ""

    # The 8-bit (C1) forms are the same terminal sequences and must be removed
    # just as completely, split or whole.
    c1_bel = _TokenSanitizer()
    assert c1_bel.feed("before\x9d0;SYNTHETIC_HIDDEN\x07after") == "beforeafter"
    c1_st = _TokenSanitizer()
    assert c1_st.feed("before\x9d0;SYNTHETIC_") == "before"
    assert c1_st.feed("HIDDEN\x9cafter") == "after"
    c1_whole = _TokenSanitizer()
    assert c1_whole.feed("a\x9b31mb\x90dcs\x9cc") == "abc"
    assert c1_whole.flush() == ""

    # BEL is data inside DCS: only the ST terminator ends it.
    dcs = _TokenSanitizer()
    assert dcs.feed("a\x90HIDDEN_A\x07HIDDEN_B") == "a"
    assert dcs.feed("\x9cSAFE") == "SAFE"
    assert dcs.flush() == ""


def test_token_sanitizer_drops_an_unterminated_escape_at_flush():
    from ptc_cli.superagent.shell import _TokenSanitizer

    sanitizer = _TokenSanitizer()
    assert sanitizer.feed("safe\x1b]0;SYNTHETIC_OSC_MARKER") == "safe"
    assert sanitizer.flush() == ""


def test_token_sanitizer_filters_split_csi_sequences():
    from ptc_cli.superagent.shell import _TokenSanitizer

    sanitizer = _TokenSanitizer()
    assert sanitizer.feed("a\x1b[3") == "a"
    assert sanitizer.feed("1mred") == "red"
    assert sanitizer.flush() == ""


@pytest.mark.asyncio
async def test_watch_job_keeps_escape_state_across_an_activity_event(capsys):
    """An activity row between tokens must not expose a split OSC payload.

    An OSC terminated by either BEL or ST is removed completely.
    """
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "before\x1b]0;SYNTH"}),
                _sse(
                    "activity",
                    {
                        "event_id": "created:t1",
                        "kind": "created",
                        "at": "2026-11-04T12:00:00+00:00",
                        "summary": "Fix",
                    },
                ),
                _sse("output", {"kind": "text", "text": "ETIC_OSC_MARKER\x07after"}),
                _sse("output", {"kind": "text", "text": " more\x1b]0;SECOND\x1b"}),
                _sse("output", {"kind": "text", "text": "\\end"}),
                _sse("end", {"status": "completed", "summary": ""}, "a~r~1"),
            ]
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "before" in out
    assert "after moreend" in out
    assert "SYNTHETIC_OSC_MARKER" not in out
    assert "SECOND" not in out
    assert "\x1b" not in out


@pytest.mark.asyncio
async def test_watch_job_sanitizes_an_escape_sequence_split_across_tokens(capsys):
    """A single token chunk cannot be sanitized in isolation.

    An OSC/CSI sequence split across two frames must not leak its payload.
    """
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "safe\x1b]52;c;ZXZ"}),
                _sse("output", {"kind": "text", "text": "pbA==\x07tail"}),
                _sse("end", {"status": "completed", "summary": ""}, "a~r~1"),
            ]
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "safetail" in out
    assert "ZXZ" not in out
    assert "52;c" not in out
    assert "\x1b" not in out


@pytest.mark.asyncio
async def test_watch_job_does_not_outlive_its_deadline(capsys):
    """An attached stream that goes quiet must still release at the deadline."""
    import asyncio
    import time

    class QuietClient(StreamClient):
        def stream_activity(self, ticket_id, *, last_event_id=None):
            self.stream_calls.append((ticket_id, last_event_id))

            async def silent():
                await asyncio.sleep(10)
                yield _sse("output", {"kind": "text", "text": "late"})

            return silent()

    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = QuietClient([])
    shell.client = fake  # type: ignore[assignment]

    started = time.monotonic()
    await shell.watch_job("ticket-1", timeout=0.15)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert "watch timed out" in capsys.readouterr().out


def test_sse_frame_delimiters_survive_chunk_boundaries():
    from ptc_cli.superagent.client import next_sse_block

    # A CRLF delimiter split across two transport chunks is still a delimiter.
    buffer = "id: 1\r"
    block, buffer = next_sse_block(buffer)
    assert block is None
    buffer += "\nevent: output\r"
    block, buffer = next_sse_block(buffer)
    assert block is None
    buffer += "\n\r\ndata: x\r\n\r\n"
    block, buffer = next_sse_block(buffer)
    assert block == "id: 1\r\nevent: output"
    # The trailing CRLF frame is complete in the same chunk.
    block, buffer = next_sse_block(buffer)
    assert block is not None
    assert buffer == ""


def test_sse_parser_keeps_unicode_line_separators():
    import json

    from ptc_cli.superagent.client import parse_sse_block

    text = "hello\u2028world\u2029!\x85end"
    data = json.dumps({"kind": "text", "text": text}, ensure_ascii=False)
    frame = parse_sse_block(f"event: output\ndata: {data}")
    assert frame is not None
    assert frame.data["text"] == text


@pytest.mark.asyncio
async def test_watch_job_removes_c1_control_sequences(capsys):
    """The 8-bit equivalents of OSC/CSI/DCS are terminal sequences too.

    Their payloads must not be printed, including when split across frames.
    """
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "before\x9d0;SYNTH"}),
                _sse("output", {"kind": "text", "text": "ETIC_HIDDEN\x07after"}),
                _sse("output", {"kind": "text", "text": " more\x9d0;C1_"}),
                _sse("output", {"kind": "text", "text": "ST_HIDDEN\x9cend"}),
                _sse("output", {"kind": "text", "text": " csi\x9b31mred"}),
                _sse("end", {"status": "completed", "summary": ""}, "a~r~1"),
            ]
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "beforeafter" in out
    assert " moreend" in out
    assert " csi" in out
    assert "red" in out
    assert "SYNTHETIC_HIDDEN" not in out
    assert "C1_ST_HIDDEN" not in out
    assert "\x9d" not in out
    assert "\x9b" not in out
    assert "\x9c" not in out


def test_plain_removes_c1_and_esc_string_controls():
    from ptc_cli.superagent.shell import _plain

    # C1 OSC with BEL and with 8-bit ST; C1 DCS; ESC DCS.
    assert _plain("\x9d0;SECRET\x07safe") == "safe"
    assert _plain("\x9d0;SECRET\x9cvisible") == "visible"
    assert _plain("a\x90payload\x9cb") == "ab"
    # BEL is not a DCS terminator (only OSC accepts it): the earlier payload
    # belongs to the DCS and must not become visible.
    assert _plain("\x90HIDDEN_A\x07HIDDEN_B\x9cSAFE") == "SAFE"
    assert _plain("a\x1bPpayload\x1b\\b") == "ab"
    assert _plain("\x9d0;SECRET") == ""


@pytest.mark.asyncio
async def test_watch_job_forgets_a_terminal_status_when_the_ticket_revives(capsys, monkeypatch):
    """Waiting -> disconnect -> queued -> disconnect -> failure must poll.

    The stale waiting result must not be printed after the Ticket revives.
    """
    _fast_reconnect(monkeypatch)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse(
                    "status",
                    {"status": "waiting", "summary": "waiting", "question": "?"},
                    "a~r~1",
                ),
                ActivityStreamUnavailableError("cut"),
            ],
            [
                _sse(
                    "status",
                    {"status": "queued", "summary": "", "question": ""},
                    "a~r~2",
                ),
                ActivityStreamUnavailableError("cut"),
            ],
            ActivityStreamUnavailableError("stream gone"),
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "polling the event record" in out
    assert "failed" in out
    assert "waiting" not in out
    assert fake.poll_calls == [("ticket", "ticket-1"), ("activity", "ticket-1")]


@pytest.mark.asyncio
async def test_watch_job_prints_an_archived_terminal_status(capsys):
    """The server treats archived as terminal too; ending silently hides it."""
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient([[_sse("end", {"status": "archived", "summary": "closed"}, "a~r~1")]])
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "archived" in out
    assert "closed" in out


@pytest.mark.asyncio
async def test_watch_job_keeps_escape_state_across_a_reconnect(capsys, monkeypatch):
    """An SSE error reconnects the same Worker run.

    A DCS split by the error must still be removed, not printed as text.
    """
    _fast_reconnect(monkeypatch)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "\x90HIDDEN_A", "run": "r1"}),
                _sse("error", {"message": "connection reset"}),
            ],
            [
                _sse(
                    "output",
                    {"kind": "text", "text": "HIDDEN_B\x9cSAFE", "run": "r1"},
                ),
                _sse("end", {"status": "completed", "summary": ""}, "a~r1~9"),
            ],
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "SAFE" in out
    assert "HIDDEN_A" not in out
    assert "HIDDEN_B" not in out
    assert len(fake.stream_calls) == 2


@pytest.mark.asyncio
async def test_watch_job_resets_escape_state_on_a_new_worker_run(capsys):
    """A retired run's half-parsed escape must not swallow the next run.

    The output frame names its run generation, which resets the sanitizer.
    """
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "\x90HIDDEN_A", "run": "r1"}),
                _sse(
                    "status",
                    {"status": "queued", "summary": "", "question": ""},
                    "a~~0",
                ),
                _sse(
                    "output",
                    {"kind": "text", "text": "NEW_RUN_TEXT", "run": "r2"},
                    "a~r2~1",
                ),
                _sse("end", {"status": "completed", "summary": ""}, "a~r2~1"),
            ]
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "NEW_RUN_TEXT" in out
    assert "HIDDEN_A" not in out


def _output(kind: str, text: str, *, epoch: int | None = None, run: str = "daemon:r1"):
    data: dict[str, Any] = {"kind": kind, "text": text, "run": run}
    if epoch is not None:
        data["epoch"] = epoch
    return _sse("output", data)


def _output_reset(reason: str, epoch: int, run: str = "daemon:r1"):
    return _sse(
        "output",
        {"kind": "output_reset", "epoch": epoch, "reason": reason, "run": run},
    )


async def _watch_output(capsys, events: list) -> str:
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = StreamClient(  # type: ignore[assignment]
        [[*events, _sse("end", {"status": "completed", "summary": ""}, "a~r~9")]]
    )
    await shell.watch_job("ticket-1", timeout=5.0)
    return capsys.readouterr().out


@pytest.mark.asyncio
async def test_watch_job_output_reset_never_joins_the_fragments(capsys):
    """A credential split across a retry must not be stitched back together."""
    out = await _watch_output(
        capsys,
        [
            _output("text", "prefix Bearer ", epoch=1),
            _output_reset("continuity_lost", epoch=2),
            _output("text", "abcdef12345678 tail", epoch=2),
        ],
    )

    assert "Bearer abcdef" not in out
    lines = [line.rstrip() for line in out.splitlines()]
    assert "prefix Bearer" in lines
    assert "abcdef12345678 tail" in lines
    assert "live output restarted (continuity lost)" in lines
    assert lines.index("prefix Bearer") < lines.index("live output restarted (continuity lost)") < lines.index("abcdef12345678 tail")
    # The reset forgot the epoch, so the new fragment does not restart again.
    assert out.count("live output restarted") == 1


@pytest.mark.asyncio
async def test_watch_job_output_reset_drops_a_half_open_escape(capsys):
    out = await _watch_output(
        capsys,
        [
            _output("text", "before\x1b]0;ti", epoch=1),
            _output_reset("gap", epoch=2),
            _output("text", "visible after the reset", epoch=2),
        ],
    )

    assert "before" in out
    assert "visible after the reset" in out
    assert "0;ti" not in out
    assert "\x1b" not in out


@pytest.mark.asyncio
async def test_watch_job_epoch_change_without_a_reset_acts_as_one(capsys):
    out = await _watch_output(
        capsys,
        [
            _output("text", "prefix Bearer ", epoch=1),
            _output("text", "abcdef12345678 tail", epoch=2),
        ],
    )

    assert "Bearer abcdef" not in out
    lines = [line.rstrip() for line in out.splitlines()]
    assert "live output restarted (new epoch)" in lines
    assert lines.index("prefix Bearer") < lines.index("live output restarted (new epoch)") < lines.index("abcdef12345678 tail")


@pytest.mark.asyncio
async def test_watch_job_epoch_change_drops_a_half_open_escape(capsys):
    out = await _watch_output(
        capsys,
        [
            _output("reasoning", "think\x90HIDDEN", epoch=1),
            _output("text", "visible answer", epoch=2),
        ],
    )

    assert "visible answer" in out
    assert "HIDDEN" not in out


@pytest.mark.asyncio
async def test_watch_job_same_or_missing_epoch_does_not_restart(capsys):
    out = await _watch_output(
        capsys,
        [
            _output("text", "ab", epoch=3),
            _output("text", "cd", epoch=3),
            # An older payload without an epoch leaves the tracked epoch alone.
            _output("text", "ef"),
            _output("reasoning", "gh", epoch=3),
        ],
    )

    assert "abcdef" in out
    assert "restarted" not in out


@pytest.mark.asyncio
async def test_watch_job_payloads_without_epoch_behave_as_before(capsys):
    out = await _watch_output(capsys, [_output("text", "part "), _output("text", "part2")])
    assert "part" in out
    assert "part2" in out
    assert "restarted" not in out


@pytest.mark.asyncio
async def test_watch_job_a_change_of_run_is_a_new_fragment_even_without_epochs(capsys):
    """A change of run closes the old fragment.

    Cloud run to cloud run, or cloud run to daemon: the old fragment is
    closed with a visible notice, never continued.
    """
    out = await _watch_output(
        capsys,
        [
            _output("text", "password "),
            _output("text", "=ordinary-value", run="daemon:r9", epoch=3),
        ],
    )
    assert "live output restarted (new epoch)" in out
    assert "password =ordinary-value" not in " ".join(out.split())


@pytest.mark.asyncio
async def test_watch_job_output_reset_reason_is_terminal_safe(capsys):
    out = await _watch_output(
        capsys,
        [
            _output_reset("\x1b]52;c;ZXZpbA==\x07[bold]odd[/bold]", epoch=1),
            _output("text", "after", epoch=1),
        ],
    )

    assert "\x1b" not in out
    assert "\x07" not in out
    # Server text is escaped, never interpreted as Rich markup.
    assert "[bold]odd[/bold]" in out
    assert "after" in out


@pytest.mark.asyncio
async def test_watch_job_revalidates_a_cached_terminal_after_failed_recovery(capsys, monkeypatch):
    """A cached terminal status is not final without its end frame.

    If the reattach fails, the polling fallback must re-read durable state
    (the Ticket may have been retried to queued since).
    """
    _fast_reconnect(monkeypatch)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    fake = StreamClient(
        [
            [
                _sse(
                    "status",
                    {"status": "waiting", "summary": "waiting", "question": "?"},
                    "a~r~1",
                ),
                ActivityStreamUnavailableError("cut"),
            ],
            ActivityStreamUnavailableError("stream gone"),
        ]
    )
    shell.client = fake  # type: ignore[assignment]

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    assert "polling the event record" in out
    assert "failed" in out
    assert "waiting" not in out
    assert fake.poll_calls == [("ticket", "ticket-1"), ("activity", "ticket-1")]


@pytest.mark.asyncio
async def test_line_shell_bang_runs_locally_after_permission(monkeypatch):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.permissions.allow_session("local-shell")
    calls: list[str] = []
    monkeypatch.setattr(
        "ptc_cli.superagent.shell.run_shell",
        lambda command, cwd=None: calls.append(command) or "hello",
    )

    dispatched: list[str] = []

    def _record(prompt: str) -> None:
        dispatched.append(prompt)

    monkeypatch.setattr(shell, "create_task", _record)
    assert await shell.handle("!echo hello") is True
    assert calls == ["echo hello"]
    assert dispatched == []  # not dispatched as a Ticket


@pytest.mark.asyncio
async def test_watch_job_a_tool_from_one_source_then_text_from_another_restarts(capsys):
    out = await _watch_output(
        capsys,
        [
            _sse("output", {"kind": "tool", "tools": ["Bearer"], "run": "run-old"}),
            _output("text", "abcdefgh", run="daemon:r9", epoch=3),
        ],
    )
    assert "live output restarted (new epoch)" in out


class HostileOverview(FakeClient):
    async def overview(self) -> dict[str, Any]:
        return {
            "counts": {
                "attention": "\x1b[2J\x1b]52;c;QUJD\x07[bold]x[/bold]",
                "running": "\x9b2J",
                "queued": "\u202e",
                "failed": "\x1bc",
                "blocked": "\u200b9",
                "completed": "\x1b]0;t\x1b\\9",
            },
            "sources": "\u202e\x1b[31m",
            "enabled_sources": "\x1bc[red]",
        }

    async def daemons(self) -> dict[str, Any]:
        return {"daemons": []}

    async def sources(self) -> dict[str, Any]:
        return {"sources": []}


@pytest.mark.asyncio
async def test_counts_and_names_from_the_server_overview_are_escaped(capsys):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = HostileOverview()  # type: ignore[assignment]
    await shell.startup()
    await shell.signals()
    out = capsys.readouterr().out
    assert not {c for c in out if c in "\x1b\x9b\x9d\u202e\u200b"}
    assert "[bold]" in out  # printed as text, not interpreted as markup


def test_a_ticket_id_with_controls_is_cleaned_in_the_job_line():
    from ptc_cli.superagent.shell import _ticket_line

    line = _ticket_line({"ticket_id": "\x1b[2J\x1b]0;t\x07[x]abcdefgh", "status": "q", "title": "t"})
    assert line == "\\[x]abcd q t"


@pytest.mark.asyncio
async def test_a_gap_frame_ends_the_fragment_and_its_escape_state(capsys):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "kept \x1b]52;c;", "run": "r", "epoch": 1}),
                _sse("output", {"kind": "gap", "text": "Some Worker output was dropped"}),
                _sse("output", {"kind": "text", "text": "visible after\n", "run": "r", "epoch": 1}),
                _sse("end", {"status": "completed", "summary": "done"}),
            ]
        ]
    )
    await shell.watch_job("ticket-1", timeout=5.0)
    out = capsys.readouterr().out
    assert "Some Worker output was dropped" in out
    assert "visible after" in out  # the unfinished sequence did not swallow it


@pytest.mark.asyncio
async def test_a_tool_name_from_a_new_epoch_restarts_the_output_before_it_is_printed(capsys):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "Bearer ", "run": "daemon:r1", "epoch": 1}),
                _sse("output", {"kind": "tool", "tools": ["x"], "run": "daemon:r1", "epoch": 2}),
                _sse("output", {"kind": "text", "text": "tok\n", "run": "daemon:r1", "epoch": 2}),
                _sse("end", {"status": "completed", "summary": "done"}),
            ]
        ]
    )
    await shell.watch_job("ticket-1", timeout=5.0)
    out = capsys.readouterr().out
    assert out.index("live output restarted") < out.index("tool: x") < out.index("tok")
    assert out.count("live output restarted") == 1  # the text after it is the same epoch


@pytest.mark.asyncio
async def test_the_reconnect_notice_starts_on_its_own_line(capsys, monkeypatch):
    _fast_reconnect(monkeypatch)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = StreamClient(
        [
            [
                _sse("output", {"kind": "text", "text": "partial tex", "run": "r"}),
                ActivityStreamUnavailableError("reset by peer"),
            ],
            [
                _sse("output", {"kind": "text", "text": "t continues\n", "run": "r"}),
                _sse("end", {"status": "completed", "summary": "done"}),
            ],
        ]
    )
    await shell.watch_job("ticket-1", timeout=5.0)
    out = capsys.readouterr().out
    assert "partial tex\n" in out
    assert "partial texlive" not in out
    assert "t continues" in out


@pytest.mark.asyncio
async def test_a_bare_stream_error_reads_in_the_locale(capsys, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    _fast_reconnect(monkeypatch)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = StreamClient(
        [
            [
                _sse("status", {"status": "running"}),
                _sse("error", {}),
            ],
            [
                _sse("end", {"status": "completed", "summary": "done"}),
            ],
        ]
    )
    await shell.watch_job("ticket-1", timeout=5.0)
    out = capsys.readouterr().out
    assert "实时流已丢失（Ticket 活动流失败）" in out  # noqa: RUF001
    assert "stream failed" not in out
    assert "已完成" in out


def test_help_commands_match_the_shared_list():
    from ptc_cli.superagent.shell import HELP_COMMANDS
    from ptc_cli.superagent.tui_palette import COMMANDS

    assert tuple(command.slash or command.name for command in COMMANDS) == HELP_COMMANDS
    assert "/dispatch" in HELP_COMMANDS
    assert "approve" in HELP_COMMANDS
    assert "reject" in HELP_COMMANDS


def test_a_zero_count_is_the_digits_not_a_rich_text(capsys):
    from ptc_cli.superagent.shell import _count

    assert _count(0) == "0"
    assert _count(0.0) == "0"
    assert "Text(" not in _count(0)


@pytest.mark.asyncio
async def test_line_shell_prints_running_zero(capsys):
    class ZeroCounts(FakeClient):
        async def overview(self) -> dict[str, Any]:
            return {
                "counts": {"running": 0, "attention": 0, "queued": 0},
                "sources": 1,
                "enabled_sources": 0,
            }

        async def daemons(self) -> dict[str, Any]:
            return {"daemons": []}

    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = ZeroCounts()  # type: ignore[assignment]
    await shell.startup()
    await shell.handle("/status")
    out = capsys.readouterr().out
    assert "running=0" in out
    assert "Text(" not in out
    assert "repr" not in out.lower() or "running=0" in out


@pytest.mark.asyncio
async def test_help_lists_the_shared_commands(capsys):
    from ptc_cli.superagent.tui_completion import SLASH_COMMANDS
    from ptc_cli.superagent.tui_palette import COMMANDS

    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    await shell.handle("/help")
    out = capsys.readouterr().out
    for command in COMMANDS:
        assert (command.slash or command.name) in out
    for name in SLASH_COMMANDS:
        assert name in out


def test_line_help_and_tui_help_list_the_same_commands():
    """Both /help screens teach the S04 table; the line shell drops none of the TUI's."""
    from ptc_cli.superagent.shell import HELP_COMMANDS, help_lines
    from ptc_cli.superagent.tui import HELP_TEXT
    from ptc_cli.superagent.tui_palette import COMMANDS

    tui_names = {
        line.split()[0]
        for line in HELP_TEXT.split("\nCommands\n", 1)[1].splitlines()
        if line.strip()
    }
    for locale in ("en", "zh-CN"):
        rows = help_lines(1000, locale)
        listed = [row.split()[0] for row in rows if row.startswith("  ") and row.split()[0].startswith("/")]
        # The line shell spells every table command as a slash command it runs.
        table = [command.slash or f"/{command.name}" for command in COMMANDS]
        assert listed[: len(table)] == table
        assert list(HELP_COMMANDS) == [command.slash or command.name for command in COMMANDS]
        text = "\n".join(rows)
        missing = sorted(name for name in tui_names if name not in text)
        assert not missing, f"{locale}: TUI /help lists {missing}, the line shell does not"


@pytest.mark.asyncio
async def test_worker_workspace_and_memory_say_the_server_has_no_view(capsys, monkeypatch):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    await shell.handle("/worker")
    await shell.handle("/workspace")
    await shell.handle("/memory")
    assert capsys.readouterr().out.splitlines() == ["the server does not support this yet"] * 3

    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    await shell.handle("/worker")
    await shell.handle("/workspace")
    await shell.handle("/memory")
    assert capsys.readouterr().out.splitlines() == ["服务端暂不支持"] * 3


@pytest.mark.asyncio
async def test_skills_and_mcp_call_conductor_status_json(monkeypatch, capsys):
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))

        class Completed:
            returncode = 0
            stdout = '{"ok": true}\n'
            stderr = ""

        return Completed()

    monkeypatch.setattr("ptc_cli.superagent.shell.subprocess.run", fake_run)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    await shell.handle("/skills")
    await shell.handle("/mcp extra")
    assert calls[0][0] == ["conductor", "skills", "status", "--json"]
    assert calls[1][0] == ["conductor", "mcp", "status", "--json"]
    assert calls[0][1]["shell"] is False
    assert calls[1][1]["shell"] is False
    assert calls[0][1]["timeout"] == 5
    assert calls[1][1]["timeout"] == 5
    assert '"ok": true' in capsys.readouterr().out


def test_line_text_renders_a_table_without_pipes_or_trailers():
    from ptc_cli.superagent.shell import render_line_text

    rendered = render_line_text(
        "| a | b |\n| --- | --- |\n| 1 | 2 |\nAUTONOMY_STATE: secret\nhello",
        width=40,
    )
    assert "AUTONOMY_" not in rendered
    assert "hello" in rendered
    assert not any(line.strip().startswith("|") and line.strip().endswith("|") for line in rendered.splitlines())


@pytest.mark.asyncio
async def test_resume_does_not_create_a_session_directory(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("CONDUCTOR_SUPERAGENT_SESSIONS", str(tmp_path / "sessions"))
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    await shell.handle("/resume")
    assert "no saved sessions" in capsys.readouterr().out
    assert not (tmp_path / "sessions").exists()


def test_shell_notices_are_bilingual():
    from ptc_cli.superagent.shell import STRINGS
    from ptc_cli.superagent.tui_i18n import check_parity

    check_parity(STRINGS)


def test_terminal_status_uses_the_locale_word(monkeypatch, capsys):
    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell._show_terminal("completed")
    assert "已完成" in capsys.readouterr().out
    shell._show_terminal("failed")
    assert "失败" in capsys.readouterr().out
    shell._show_terminal("waiting")
    assert "需要你" in capsys.readouterr().out
    shell._show_terminal("cancelled")
    assert "已取消" in capsys.readouterr().out
    monkeypatch.setenv("CONDUCTOR_LOCALE", "en")
    shell._show_terminal("completed")
    assert "completed" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_permission_labels_use_the_locale(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    monkeypatch.setenv("CONDUCTOR_LOCALE", "en")
    with pytest.raises(Exception, match="run shell command: echo hello"):
        await shell.handle("!echo hello")
    with pytest.raises(Exception, match="conductor merge line --into main"):
        await shell.merge(["line", "--into", "main"])
    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    with pytest.raises(Exception, match="运行 shell 命令: echo hello") as caught:
        await shell.handle("!echo hello")
    assert "run shell command:" not in str(caught.value)
    with pytest.raises(Exception, match="执行 conductor merge line --into main") as merge_caught:
        await shell.merge(["line", "--into", "main"])
    assert "执行 conductor merge" in str(merge_caught.value)


def _own_the_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The test pty is not pytest's controlling tty; say this group owns it."""
    import os

    monkeypatch.setattr(os, "tcgetpgrp", lambda _fd: os.getpgrp())


def _tty_mode(fd: int) -> list[object]:
    """The tty attributes minus PENDIN, which the kernel sets on its own."""
    import termios

    mode = termios.tcgetattr(fd)
    mode[3] &= ~getattr(termios, "PENDIN", 0)
    return mode


@pytest.mark.asyncio
async def test_escape_stops_watching(monkeypatch):
    """Esc on a normal (canonical) tty stops the watch; the tty mode comes back."""
    import asyncio
    import os
    import pty
    import termios
    from contextlib import suppress
    from types import SimpleNamespace

    from ptc_cli.superagent.client import ActivityStreamEvent

    _own_the_tty(monkeypatch)
    master, slave = pty.openpty()
    before = _tty_mode(slave)
    # The pty starts canonical, as a shell prompt leaves it. No setraw here.
    assert before[3] & termios.ICANON
    saved = sys.stdin
    stdin = os.fdopen(slave)
    sys.stdin = stdin
    task: asyncio.Task[None] | None = None
    try:

        async def stream(*_args: object, **_kwargs: object):
            yield ActivityStreamEvent("status", {"status": "running"})
            await asyncio.Event().wait()

        shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
        shell.client = SimpleNamespace(stream_activity=stream)  # type: ignore[assignment]
        task = asyncio.create_task(shell.watch_job("t1", timeout=5))
        await asyncio.sleep(0.1)
        assert not termios.tcgetattr(slave)[3] & termios.ICANON
        os.write(master, b"\x1b")
        await asyncio.sleep(0.2)
        assert task.done()
        await task
        assert _tty_mode(slave) == before
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        sys.stdin = saved
        stdin.close()
        os.close(master)


@pytest.mark.asyncio
async def test_a_cancelled_watch_restores_the_tty_mode(monkeypatch):
    import asyncio
    import os
    import pty
    from contextlib import suppress
    from types import SimpleNamespace

    from ptc_cli.superagent.client import ActivityStreamEvent

    _own_the_tty(monkeypatch)
    master, slave = pty.openpty()
    before = _tty_mode(slave)
    saved = sys.stdin
    stdin = os.fdopen(slave)
    sys.stdin = stdin
    try:

        async def stream(*_args: object, **_kwargs: object):
            yield ActivityStreamEvent("status", {"status": "running"})
            await asyncio.Event().wait()

        shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
        shell.client = SimpleNamespace(stream_activity=stream)  # type: ignore[assignment]
        task = asyncio.create_task(shell.watch_job("t1", timeout=5))
        await asyncio.sleep(0.1)
        assert _tty_mode(slave) != before
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert _tty_mode(slave) == before
    finally:
        sys.stdin = saved
        stdin.close()
        os.close(master)


def _help_spellings() -> list[str]:
    """Every slash spelling the line-shell /help teaches: labels and aliases."""
    from ptc_cli.superagent.shell import help_lines

    found: list[str] = []
    for row in help_lines(1000, "en"):
        for token in row.replace(",", " ").split():
            if token.startswith("/") and re.fullmatch(r"/[a-z?]+", token) and token not in found:
                found.append(token)
    return found


@pytest.mark.asyncio
async def test_every_command_help_lists_is_run_or_explained(monkeypatch, tmp_path, capsys):
    """Nothing /help teaches answers "unknown command"."""

    class Completed:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr("ptc_cli.superagent.shell.subprocess.run", lambda *a, **k: Completed())
    monkeypatch.setenv("CONDUCTOR_SUPERAGENT_SESSIONS", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    spellings = _help_spellings()
    for expected in ("/attach", "/?", "/approve", "/reject", "/clear", "/model", "/collapse"):
        assert expected in spellings
    for spelling in spellings:
        shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
        shell.client = FakeClient()  # type: ignore[assignment]
        error: Exception | None = None
        try:
            await shell.handle(spelling)
        except Exception as caught:  # noqa: BLE001 - usage errors are fine, unknown is not
            error = caught
        assert "unknown command" not in str(error or ""), spelling
    assert "unknown command" not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_tui_only_commands_say_so_in_the_locale(monkeypatch, capsys):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    await shell.handle("/clear")
    assert capsys.readouterr().out.strip() == "/clear works in the full-screen TUI only"
    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    await shell.handle("/details")
    assert capsys.readouterr().out.strip() == "/details 仅在全屏 TUI 中可用"


@pytest.mark.asyncio
async def test_slash_question_mark_prints_help(capsys):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    await shell.handle("/?")
    out = capsys.readouterr().out
    assert "/dispatch" in out
    assert "quit the line shell" in out
    assert "TUI key map" not in out
    assert "quit the TUI" not in out


@pytest.mark.asyncio
async def test_attach_follows_a_ticket_like_jobs_watch(monkeypatch):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    watched: list[str] = []

    async def fake_watch(target: str, **_kwargs: object) -> None:
        watched.append(target)

    monkeypatch.setattr(shell, "watch_job", fake_watch)
    await shell.handle("/attach 2")
    assert watched == ["2"]
    with pytest.raises(Exception, match="usage: /attach"):
        await shell.handle("/attach")


@pytest.mark.asyncio
async def test_approve_and_reject_post_the_decision(capsys, monkeypatch):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    client = FakeClient()
    posted: list[tuple[str, str]] = []

    async def request(method: str, path: str, **_kwargs: object) -> None:
        posted.append((method, path))

    client._request = request  # type: ignore[attr-defined]
    shell.client = client  # type: ignore[assignment]
    await shell.handle("/approve dispatch-123456789")
    await shell.handle("/reject d2")
    assert posted == [
        ("POST", "/api/v1/dispatch/dispatch-123456789/approve"),
        ("POST", "/api/v1/dispatch/d2/reject"),
    ]
    assert "approve requested for dispatch" in capsys.readouterr().out
    assert client.calls == []  # no Ticket was created
    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    with pytest.raises(Exception, match="用法: /approve <派发 id>"):
        await shell.handle("/approve")


def test_suggestions_are_slash_commands_the_shell_runs():
    from ptc_cli.superagent.shell import _suggestions

    spellings = set(_help_spellings())
    for typed in ("/aprove", "/rejct", "/dsp", "/atach", "/modl"):
        found = _suggestions(typed)
        assert found, typed
        for suggestion in found:
            assert suggestion.startswith("/"), (typed, suggestion)
            assert suggestion in spellings, (typed, suggestion)
    assert "/approve" in _suggestions("/aprove")


def test_only_a_lone_escape_stops_the_watch():
    from ptc_cli.superagent.shell import _is_lone_escape

    assert _is_lone_escape(b"\x1b")
    assert _is_lone_escape(b"ab\x1b")
    assert not _is_lone_escape(b"\x1b[A")
    assert not _is_lone_escape(b"\x1bOP")
    assert not _is_lone_escape(b"plain")


@pytest.mark.asyncio
async def test_arrow_keys_do_not_stop_watching(monkeypatch):
    import asyncio
    import os
    import pty
    from contextlib import suppress
    from types import SimpleNamespace

    from ptc_cli.superagent.client import ActivityStreamEvent

    _own_the_tty(monkeypatch)
    master, slave = pty.openpty()
    saved = sys.stdin
    stdin = os.fdopen(slave)
    sys.stdin = stdin
    task: asyncio.Task[None] | None = None
    try:

        async def stream(*_args: object, **_kwargs: object):
            yield ActivityStreamEvent("status", {"status": "running"})
            await asyncio.Event().wait()

        shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
        shell.client = SimpleNamespace(stream_activity=stream)  # type: ignore[assignment]
        task = asyncio.create_task(shell.watch_job("t1", timeout=5))
        await asyncio.sleep(0.1)
        os.write(master, b"\x1b[A")
        await asyncio.sleep(0.2)
        assert not task.done()
        os.write(master, b"\x1b")
        await asyncio.sleep(0.2)
        assert task.done()
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        sys.stdin = saved
        stdin.close()
        os.close(master)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", ["other-group", "error"])
async def test_a_background_watch_leaves_the_tty_alone(monkeypatch, owner):
    """A background job must not touch the tty: tcsetattr would stop it (SIGTTOU)."""
    import asyncio
    import os
    import pty
    import termios

    from ptc_cli.superagent import shell as shell_module

    master, slave = pty.openpty()
    before = _tty_mode(slave)

    def tcgetpgrp(_fd: int) -> int:
        if owner == "error":
            message = "not a controlling tty"
            raise OSError(message)
        return os.getpgrp() + 1

    monkeypatch.setattr(shell_module.os, "tcgetpgrp", tcgetpgrp)
    set_calls: list[int] = []
    real_set = termios.tcsetattr
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, mode: (set_calls.append(fd), real_set(fd, when, mode)))
    saved = sys.stdin
    stdin = os.fdopen(slave)
    sys.stdin = stdin
    try:
        stop = asyncio.Event()
        async with shell_module._escape_listener(stop):
            assert _tty_mode(slave) == before
            os.write(master, b"\x1b")
            await asyncio.sleep(0.1)
            assert not stop.is_set()
        assert set_calls == []
        assert _tty_mode(slave) == before
    finally:
        sys.stdin = saved
        stdin.close()
        os.close(master)


@pytest.mark.asyncio
async def test_an_unknown_command_suggests_dispatch(capsys, monkeypatch):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = FakeClient()  # type: ignore[assignment]
    with pytest.raises(Exception, match="unknown command /dsp") as caught:
        await shell.handle("/dsp")
    assert "/dispatch" in str(caught.value)

    monkeypatch.setenv("CONDUCTOR_LOCALE", "zh-CN")
    with pytest.raises(Exception, match="未知命令 /dsp") as caught_zh:
        await shell.handle("/dsp")
    assert "/dispatch" in str(caught_zh.value)
    assert capsys.readouterr().out == ""


def test_help_home_and_jobs_pass_the_layout_contract():
    from ptc_cli.superagent.shell import help_lines, home_lines, jobs_lines
    from ptc_cli.superagent.tui_theme import status_glyph, status_word
    from tests.unit_tests.tui_layout_contract import check_layout

    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "title": "Fix",
        "worker_model": "grok",
    }
    check_layout(
        help_lines,
        keep=("/dispatch", "approve", "/attach", "/exit"),
    )
    # Home is a table above 40 (glyph and word on one row) and stacks at 40,
    # where the glyph leads the title row and the word leads the meta row.
    check_layout(
        lambda width, locale: home_lines([ticket], 1, width, locale),
        widths=(80, 120),
        states=("running",),
        keep=("Conductor", "Fix"),
    )
    check_layout(
        lambda width, locale: home_lines([ticket], 1, width, locale),
        keep=("Conductor", "Fix", "/jobs watch 1"),
    )
    check_layout(
        lambda width, locale: jobs_lines([ticket], width, locale),
        states=("running",),
        keep=("ticket-1", "Fix"),
    )
    mixed = [
        ticket,
        {"ticket_id": "ticket-2", "status": "failed", "title": "修复登录页面的长标题 Build"},
        {"ticket_id": "ticket-3", "status": "waiting", "title": "Ask", "question": "ok?"},
        {"ticket_id": "ticket-4", "status": "completed", "title": "Done"},
    ]
    mixed_states = ("running", "failed", "needs-you", "completed")
    check_layout(
        lambda width, locale: home_lines(mixed, 1, width, locale),
        widths=(80, 120),
        states=mixed_states,
        keep=("Conductor", "/retry 2", "/reply 3"),
    )
    for locale in ("en", "zh-CN"):
        stacked = home_lines(mixed, 1, 40, locale)
        check_layout(
            lambda width, language: home_lines(mixed, 1, width, language),
            widths=(40,),
            locales=(locale,),
            keep=("Conductor", "/retry 2", "/reply 3"),
            columns=tuple(status_word(state, locale) for state in mixed_states),
        )
        for state in mixed_states:
            glyph, word = status_glyph(state, {}), status_word(state, locale)
            at = next(i for i, row in enumerate(stacked) if row.lstrip().startswith(glyph + "  "))
            assert stacked[at + 1].lstrip().startswith(word), (locale, state, stacked[at : at + 2])
    check_layout(
        lambda width, locale: jobs_lines(mixed, width, locale),
        states=("running", "failed", "needs-you", "completed"),
        keep=("ticket-2", "/retry 2", "/reply 3"),
    )
    check_layout(
        lambda width, locale: jobs_lines([], width, locale),
        keep=("no Jobs",),
    )
