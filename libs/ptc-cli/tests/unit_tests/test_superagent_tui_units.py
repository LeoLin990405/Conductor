"""Unit tests for the Super-agent TUI's non-terminal parts.

These cover the conversation reducer, the live-event watch (SSE, reconnect,
polling fallback, timeout), session persistence, the permission gate,
completions/attachments, local command construction, and mode selection. The
prompt_toolkit application itself is exercised in ``test_superagent_tui_app``.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# tui imports conductor_cli, which lives in the repo src tree; the ptc-cli
# lockfile's conductor-core is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.client import (  # noqa: E402
    ActivityStreamEvent,
    ActivityStreamUnavailableError,
)
from ptc_cli.superagent.text import TokenSanitizer, plain, text  # noqa: E402
from ptc_cli.superagent.tui import (  # noqa: E402
    HELP_TEXT,
    align_status_line,
    build_default_style,
    choose_mode,
    fit_status_line,
)
from ptc_cli.superagent.tui_blocks import (  # noqa: E402
    ActivityBlock,
    AssistantBlock,
    Conversation,
    EndBlock,
    ErrorBlock,
    NoticeBlock,
    PermissionBlock,
    UserBlock,
    collect_mentions,
    estimate_tokens,
    format_duration,
    parse_unified_diff,
    split_text_and_diffs,
)
from ptc_cli.superagent.tui_completion import (  # noqa: E402
    SLASH_COMMANDS,
    AtFileCompleter,
    SlashCompleter,
    TuiCompleter,
    expand_mentions,
)
from ptc_cli.superagent.tui_events import (  # noqa: E402
    Activity,
    End,
    Gap,
    OutputChunk,
    OutputError,
    OutputReset,
    Status,
    StreamNotice,
    TimedOut,
    ToolCall,
    _map_stream_event,
    watch_ticket,
)
from ptc_cli.superagent.tui_local import (  # noqa: E402
    parse_conductor_args,
    run_conductor,
    run_shell,
    worktree_root,
)
from ptc_cli.superagent.tui_permissions import (  # noqa: E402
    LOCAL_MUTATING_ACTIONS,
    PermissionManager,
    PermissionRequest,
    decision_from_key,
)
from ptc_cli.superagent.tui_sessions import SessionStore  # noqa: E402

# ---------------------------------------------------------------------------
# text / blocks
# ---------------------------------------------------------------------------


def test_text_sanitizes_terminal_controls_and_keeps_visible_text():
    hostile = "\x1b[2J\x1b]52;c;ZXZpbA==\x07safe [bold]x[/bold]"

    assert plain(hostile) == "safe [bold]x[/bold]"
    assert "\x1b" not in text(hostile)
    assert text(hostile) == r"safe \[bold]x\[/bold]"


def test_stream_sanitizer_holds_split_escape_sequences():
    sanitizer = TokenSanitizer()

    assert sanitizer.feed("before\x1b]52;c;ZX") == "before"
    assert sanitizer.feed("pbsA==\x07after") == "after"
    assert sanitizer.feed("tail\x1b") == "tail"
    assert sanitizer.flush() == ""


def test_estimate_tokens_counts_cjk_and_latin():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("中文中文中文中文") == 8
    assert estimate_tokens("中abc") == 2


def test_format_duration_units():
    assert format_duration(None) == ""
    assert format_duration(0.24) == "240ms"
    assert format_duration(1.26) == "1.3s"
    assert format_duration(42) == "42.0s"
    assert format_duration(61) == "1m01s"
    assert format_duration(7552.5) == "2h05m"
    assert format_duration(28635.4) == "7h57m"


def test_parse_unified_diff_colors_lines_and_rejects_prose():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
        " context\n"
    )

    parsed = parse_unified_diff(diff)
    assert parsed is not None
    assert [(line.marker, line.text) for line in parsed[:4]] == [
        ("header", "diff --git a/x.py b/x.py"),
        ("header", "--- a/x.py"),
        ("header", "+++ b/x.py"),
        ("hunk", "@@ -1 +1 @@"),
    ]
    assert [line.marker for line in parsed[4:]] == ["del", "add", "context"]
    assert parse_unified_diff("a - b\n- not a diff") is None


def test_split_text_and_diffs_keeps_order():
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    body = f"intro\n{diff}outro"

    pieces = split_text_and_diffs(body)
    assert pieces[0] == ("text", "intro\n")
    assert pieces[1][0] == "diff"
    assert pieces[2] == ("text", "outro")


def test_conversation_streams_assistant_text_and_reasoning_apart():
    conversation = Conversation()

    conversation.feed(OutputChunk(kind="text", text="hello ", run_id="r1"))
    conversation.feed(OutputChunk(kind="text", text="world", run_id="r1"))
    conversation.feed(OutputChunk(kind="reasoning", text="thinking", run_id="r1"))
    conversation.feed(OutputChunk(kind="text", text="!", run_id="r1"))

    agents = [block for block in conversation.blocks if isinstance(block, AssistantBlock)]
    assert [block.reasoning for block in agents] == [False, True, False]
    assert agents[0].text == "hello world"
    assert agents[2].text == "!"


def test_conversation_groups_streamed_text_by_sub_agent():
    conversation = Conversation()

    conversation.feed(OutputChunk(kind="text", text="main ", run_id="r1"))
    conversation.feed(OutputChunk(kind="text", text="review", run_id="r1", agent="reviewer"))
    conversation.feed(OutputChunk(kind="text", text=" done", run_id="r1", agent="reviewer"))
    conversation.feed(OutputChunk(kind="text", text="back", run_id="r1"))

    agents = [block for block in conversation.blocks if isinstance(block, AssistantBlock)]
    assert [(block.agent, block.text) for block in agents] == [
        ("", "main "),
        ("reviewer", "review done"),
        ("", "back"),
    ]
    assert any("sub-agent reviewer" in line for line in conversation.plain_lines())
    restored = Conversation.from_json(conversation.to_json())
    assert restored.blocks[1].agent == "reviewer"


def test_conversation_tool_blocks_get_status_and_duration():
    now = iter([10.0, 10.0, 12.5])
    conversation = Conversation()

    conversation.feed(
        ToolCall(names=("read_file", "grep")),
        now=lambda: next(now),
    )
    tools = conversation.tool_blocks()
    assert [tool.name for tool in tools] == ["read_file", "grep"]
    assert all(tool.status == "running" for tool in tools)

    conversation.feed(End(status="completed", summary="done"), now=lambda: 12.5)
    assert [tool.status for tool in tools] == ["done", "done"]
    assert tools[0].duration == pytest.approx(2.5)


def test_conversation_error_marks_running_tools_failed():
    conversation = Conversation()
    conversation.feed(ToolCall(names=("shell",)), now=lambda: 1.0)
    conversation.feed(OutputError(text="boom"), now=lambda: 2.0)

    assert conversation.last_tool().status == "failed"
    assert any(isinstance(block, ErrorBlock) for block in conversation.blocks)


def test_conversation_collapses_and_expands_tool_blocks():
    conversation = Conversation()
    conversation.feed(ToolCall(names=("one",)), now=lambda: 1.0)
    conversation.feed(ToolCall(names=("two",)), now=lambda: 2.0)

    assert conversation.toggle_last_tool().name == "two"
    assert conversation.last_tool().expanded is True
    assert conversation.collapse_tools() == 1
    assert conversation.expand_tools() == 2

    collapsed = conversation.last_tool()
    collapsed.expanded = False
    rendered = collapsed.render_plain()
    assert len(rendered) == 1
    assert "two" in rendered[0]


def test_conversation_compact_drops_notices_and_duplicate_activity():
    conversation = Conversation()
    conversation.add(NoticeBlock(text="noise"))
    conversation.add(ActivityBlock(at="t", event_kind="report", summary="same"))
    conversation.add(ActivityBlock(at="t", event_kind="report", summary="same"))
    conversation.add(EndBlock(status="completed"))

    removed = conversation.compact()
    kinds = [type(block) for block in conversation.blocks]
    assert removed == 2
    assert NoticeBlock not in kinds
    assert kinds.count(ActivityBlock) == 1
    assert EndBlock in kinds


def test_conversation_token_estimate_and_session_round_trip():
    conversation = Conversation()
    conversation.add(UserBlock(text="hello " * 10))
    conversation.feed(OutputChunk(kind="text", text="response " * 10))

    assert conversation.token_estimate() > 20
    restored = Conversation.from_json(conversation.to_json())
    assert [type(block) for block in restored.blocks] == [
        type(block) for block in conversation.blocks
    ]
    assert restored.token_estimate() == conversation.token_estimate()


def test_permission_block_renders_options_until_decided():
    block = PermissionBlock(action="local-merge", label="merge line")

    assert any("[a]llow once" in line for line in block.render_plain())
    block.decision = "deny"
    rendered = block.render_plain()
    assert any("decision: deny" in line for line in rendered)
    assert not any("[a]llow once" in line for line in rendered)


def test_collect_mentions_is_unique_and_ignores_email():
    assert collect_mentions("mail a@b and @x/y.py then @x/y.py") == ["x/y.py"]


def test_help_text_lists_every_required_command():
    for command in (
        "/jobs",
        "/agents",
        "/review",
        "/merge",
        "/stop",
        "/signals",
        "/help",
        "/clear",
        "/model",
        "/resume",
        "/compact",
    ):
        assert command in HELP_TEXT
        assert command in SLASH_COMMANDS


# ---------------------------------------------------------------------------
# live watch
# ---------------------------------------------------------------------------


class FakeStreamClient:
    """Scripted SuperAgentClient: one SSE attach per stream script entry."""

    def __init__(
        self,
        *,
        streams: list[list[ActivityStreamEvent] | Exception] | None = None,
        activity_rows: list[dict[str, Any]] | None = None,
        ticket_statuses: list[str] | None = None,
    ) -> None:
        self.streams = list(streams or [])
        self.activity_rows = list(activity_rows or [])
        self.ticket_statuses = list(ticket_statuses or [])
        self.last_ids: list[str | None] = []
        self.activity_calls = 0

    async def stream_activity(
        self, ticket_id: str, *, last_event_id: str | None = None
    ):
        self.last_ids.append(last_event_id)
        script = self.streams.pop(0) if self.streams else []
        if isinstance(script, Exception):
            raise script
        for event in script:
            if isinstance(event, Exception):
                raise event
            yield event

    async def activity(self, ticket_id: str) -> dict[str, Any]:
        self.activity_calls += 1
        return {"events": self.activity_rows}

    async def ticket(self, ticket_id: str) -> dict[str, Any]:
        status = self.ticket_statuses.pop(0) if self.ticket_statuses else "running"
        return {"ticket_id": ticket_id, "status": status, "summary": "", "attention": None}


def _event(name: str, data: dict[str, Any], event_id: str | None = None) -> ActivityStreamEvent:
    return ActivityStreamEvent(event=name, data=data, event_id=event_id)


@pytest.mark.asyncio
async def test_watch_maps_stream_frames_to_events():
    client = FakeStreamClient(
        streams=[
            [
                _event("activity", {"event_id": "e1", "kind": "created", "summary": "made"}, "1"),
                _event("output", {"kind": "text", "text": "hi"}),
                _event("output", {"kind": "tool", "tools": ["read_file"]}),
                _event("end", {"status": "completed", "summary": "done"}),
            ]
        ]
    )

    events = [event async for event in watch_ticket(client, "t1")]

    assert isinstance(events[0], Activity)
    assert isinstance(events[1], OutputChunk)
    assert events[1].text == "hi"
    assert isinstance(events[2], ToolCall)
    assert events[2].names == ("read_file",)
    assert isinstance(events[-1], End)
    assert events[-1].status == "completed"
    assert client.last_ids == [None]


@pytest.mark.asyncio
async def test_watch_ignores_tool_arguments_and_diffs_a_payload_carries():
    """A tool event is its names, and nothing else.

    Whatever else a payload holds (a proxy, a modified or an older deployment) is
    never parsed, kept or drawn.
    """
    leaked = "TOP-SECRET-ARGUMENT-VALUE"
    client = FakeStreamClient(
        streams=[
            [
                _event(
                    "output",
                    {
                        "kind": "tool",
                        "tools": ["write_file"],
                        "args": {"path": "a.py", "content": leaked},
                        "diff": f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+{leaked}\n",
                        "run": "r1",
                        "epoch": 3,
                    },
                ),
                _event("end", {"status": "completed", "summary": "done"}),
            ]
        ]
    )

    events = [event async for event in watch_ticket(client, "t1")]

    tool = events[0]
    assert isinstance(tool, ToolCall)
    assert tool.names == ("write_file",)
    assert (tool.run_id, tool.epoch) == ("r1", 3)
    assert not hasattr(tool, "args")
    assert not hasattr(tool, "diff")

    conversation = Conversation()
    for event in events:
        conversation.feed(event)
    drawn = "\n".join(conversation.plain_lines())
    drawn += "\n" + "\n".join(text for _, text in conversation.formatted_lines())
    drawn += "\n" + json.dumps(conversation.to_json())
    assert "write_file" in drawn
    assert leaked not in drawn
    assert "params" not in drawn


@pytest.mark.asyncio
async def test_watch_reconnects_with_the_last_event_id():
    client = FakeStreamClient(
        streams=[
            [
                _event("output", {"kind": "text", "text": "part1"}, "7"),
                ActivityStreamUnavailableError("transport lost"),
            ],
            [
                _event("output", {"kind": "text", "text": "part2"}, "8"),
                _event("end", {"status": "completed"}),
            ],
        ]
    )

    events = [event async for event in watch_ticket(client, "t1", sleep=_no_sleep)]

    assert client.last_ids == [None, "7"]
    assert any(isinstance(event, StreamNotice) for event in events)
    text_parts = "".join(e.text for e in events if isinstance(e, OutputChunk))
    assert text_parts == "part1part2"
    assert isinstance(events[-1], End)


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_watch_falls_back_to_polling_when_stream_is_missing():
    client = FakeStreamClient(
        streams=[ActivityStreamUnavailableError("no stream here")],
        activity_rows=[
            {"event_id": "a1", "kind": "created", "summary": "made", "at": "now"},
        ],
        ticket_statuses=["running", "completed"],
    )

    events = [event async for event in watch_ticket(client, "t1", sleep=_no_sleep)]

    assert client.last_ids == [None]
    assert any(
        isinstance(event, StreamNotice) and "polling" in event.text for event in events
    )
    assert any(isinstance(event, Activity) for event in events)
    assert isinstance(events[-1], End)


@pytest.mark.asyncio
async def test_watch_deduplicates_activity_rows_across_reconnects():
    client = FakeStreamClient(
        streams=[
            [_event("activity", {"event_id": "e1", "kind": "created", "summary": "one"}, "1")],
            [_event("activity", {"event_id": "e1", "kind": "created", "summary": "one"}, "2"),
             _event("activity", {"event_id": "e2", "kind": "report", "summary": "two"}, "3"),
             _event("end", {"status": "completed"})],
        ]
    )

    events = [event async for event in watch_ticket(client, "t1", sleep=_no_sleep)]

    # The reconnect replays e1; the watch must not show it twice.
    activities = [event for event in events if isinstance(event, Activity)]
    assert [event.event_id for event in activities] == ["e1", "e2"]
    assert isinstance(events[-1], End)


@pytest.mark.asyncio
async def test_watch_reports_a_timeout_without_a_terminal_frame():
    client = FakeStreamClient(streams=[[]])

    events = [event async for event in watch_ticket(client, "t1", timeout=0.0)]

    assert isinstance(events[-1], TimedOut)


@pytest.mark.asyncio
async def test_watch_honours_should_stop():
    stop = {"value": False}

    async def stream(ticket_id: str, *, last_event_id=None):
        yield _event("output", {"kind": "text", "text": "a"}, "1")
        stop["value"] = True
        yield _event("output", {"kind": "text", "text": "b"}, "2")

    class Client:
        stream_activity = staticmethod(stream)

    client = Client()
    events = [
        event
        async for event in watch_ticket(client, "t1", should_stop=lambda: stop["value"])
    ]

    assert [e.text for e in events if isinstance(e, OutputChunk)] == ["a"]


@pytest.mark.asyncio
async def test_watch_terminal_status_frame_is_not_the_end():
    # A retry can revive a Ticket after a terminal status read; only the end
    # frame settles the watch.
    client = FakeStreamClient(
        streams=[
            [
                _event("status", {"status": "completed", "summary": "done"}),
                _event("status", {"status": "queued"}),
                _event("end", {"status": "completed", "summary": "done"}),
            ]
        ]
    )

    events = [event async for event in watch_ticket(client, "t1")]

    assert isinstance(events[0], Status)
    assert events[0].status == "completed"
    assert isinstance(events[1], Status)
    assert events[1].status == "queued"
    assert isinstance(events[-1], End)


# ---------------------------------------------------------------------------
# stream fencing: output_reset and epochs
# ---------------------------------------------------------------------------


def _text_frame(text: str, epoch: int | None = None, run: str = "daemon:r1"):
    data: dict[str, Any] = {"kind": "text", "text": text, "run": run}
    if epoch is not None:
        data["epoch"] = epoch
    return _event("output", data)


def _reset_frame(reason: str, epoch: int, run: str = "daemon:r1"):
    return _event(
        "output", {"kind": "output_reset", "epoch": epoch, "reason": reason, "run": run}
    )


def test_map_stream_event_maps_output_reset_and_epochs():
    reset = _map_stream_event(_reset_frame("continuity_lost", 4))
    assert reset == OutputReset(reason="continuity_lost", epoch=4)

    chunk = _map_stream_event(_text_frame("hi", epoch=2))
    assert isinstance(chunk, OutputChunk)
    assert (chunk.text, chunk.run_id, chunk.epoch) == ("hi", "daemon:r1", 2)

    # No epoch (older cloud payloads), or one that is not a plain integer.
    for data in ({}, {"epoch": None}, {"epoch": "2"}, {"epoch": True}, {"epoch": 1.5}):
        old = _map_stream_event(_event("output", {"kind": "text", "text": "x", **data}))
        assert isinstance(old, OutputChunk)
        assert old.epoch == 0

    bare = _map_stream_event(_event("output", {"kind": "output_reset"}))
    assert bare == OutputReset(reason="", epoch=0)
    # A reset carries no text and is not dropped as an empty chunk.
    assert not hasattr(bare, "text")


@pytest.mark.asyncio
async def test_watch_yields_an_explicit_reset_in_order():
    client = FakeStreamClient(
        streams=[
            [
                _text_frame("prefix Bearer ", epoch=1),
                _reset_frame("continuity_lost", 2),
                _text_frame("abcdef tail", epoch=2),
                _event("end", {"status": "completed"}),
            ]
        ]
    )

    events = [event async for event in watch_ticket(client, "t1")]

    assert [type(event) for event in events] == [
        OutputChunk,
        OutputReset,
        OutputChunk,
        End,
    ]
    assert events[1] == OutputReset(reason="continuity_lost", epoch=2)
    # The reset forgot the epoch: no second, synthetic reset is inserted.
    assert sum(isinstance(event, OutputReset) for event in events) == 1


@pytest.mark.asyncio
async def test_watch_inserts_a_reset_before_a_chunk_from_a_new_epoch():
    client = FakeStreamClient(
        streams=[
            [
                _text_frame("a", epoch=1),
                _text_frame("b", epoch=1),
                _text_frame("c", epoch=2),
                _text_frame("d", epoch=2),
                _event("end", {"status": "completed"}),
            ]
        ]
    )

    events = [event async for event in watch_ticket(client, "t1")]

    assert [
        (event.text if isinstance(event, OutputChunk) else type(event).__name__)
        for event in events
    ] == ["a", "b", "OutputReset", "c", "d", "End"]
    assert events[2] == OutputReset(reason="new_epoch", epoch=2)


@pytest.mark.asyncio
async def test_watch_remembers_the_epoch_across_a_reconnect():
    client = FakeStreamClient(
        streams=[
            [
                _text_frame("part1", epoch=1),
                ActivityStreamUnavailableError("transport lost"),
            ],
            [
                _text_frame("part2", epoch=2),
                _event("end", {"status": "completed"}),
            ],
        ]
    )

    events = [event async for event in watch_ticket(client, "t1", sleep=_no_sleep)]

    resets = [event for event in events if isinstance(event, OutputReset)]
    assert resets == [OutputReset(reason="new_epoch", epoch=2)]
    texts = [
        event.text if isinstance(event, OutputChunk) else type(event).__name__
        for event in events
        if isinstance(event, (OutputChunk, OutputReset))
    ]
    assert texts == ["part1", "OutputReset", "part2"]


@pytest.mark.asyncio
async def test_watch_without_epochs_is_unchanged():
    client = FakeStreamClient(
        streams=[
            [
                _text_frame("a"),
                _text_frame("b"),
                _event("end", {"status": "completed"}),
            ]
        ]
    )

    events = [event async for event in watch_ticket(client, "t1")]

    assert [type(event) for event in events] == [OutputChunk, OutputChunk, End]
    assert [event.epoch for event in events if isinstance(event, OutputChunk)] == [0, 0]


@pytest.mark.asyncio
async def test_watch_inserts_a_reset_when_the_run_changes_without_epochs():
    """Cloud run -> another run, or cloud -> daemon: a different source."""
    client = FakeStreamClient(
        streams=[
            [
                _text_frame("a"),
                _text_frame("b", run="cloud:r2"),
                _event("end", {"status": "completed"}),
            ]
        ]
    )

    events = [event async for event in watch_ticket(client, "t1")]

    assert [type(event) for event in events] == [
        OutputChunk,
        OutputReset,
        OutputChunk,
        End,
    ]


@pytest.mark.asyncio
async def test_a_reset_alone_counts_as_a_frame_and_a_tool_carries_a_source():
    client = FakeStreamClient(
        streams=[
            [
                _reset_frame("cursor_invalid", 7),
                _event("output", {"kind": "tool", "tools": ["x"], "epoch": 9}),
                _text_frame("t", epoch=7),
                _event("end", {"status": "completed"}),
            ]
        ]
    )

    events = [event async for event in watch_ticket(client, "t1")]

    # A reset is a real frame (no polling fallback). Tool names are rendered
    # beside the text, so a tool of epoch 9 followed by text of epoch 7 is a
    # change of source and gets a reset between them.
    assert client.activity_calls == 0
    assert [type(event) for event in events] == [
        OutputReset,
        ToolCall,
        OutputReset,
        OutputChunk,
        End,
    ]


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------


def test_session_store_round_trip_and_private_permissions(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    record = store.new(title="first run")
    record.last_ticket_id = "abcd1234"
    record.router_model = "deepseek-flash-account"
    record.transcript = [{"kind": "user", "text": "hello"}]

    path = store.save(record)
    loaded = store.load(record.session_id)

    assert loaded is not None
    assert loaded.title == "first run"
    assert loaded.last_ticket_id == "abcd1234"
    assert loaded.transcript == [{"kind": "user", "text": "hello"}]
    assert path.parent == store.root
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (store.root.stat().st_mode & 0o777) == 0o700


def test_session_store_lists_newest_first_and_skips_corrupt_files(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    older = store.new(title="older")
    older.updated_at = 10.0
    store.save(older)
    newer = store.new(title="newer")
    newer.updated_at = 20.0
    store.save(newer)
    (store.root / "deadbeefdeadbeef.json").write_text("{not json", encoding="utf-8")

    sessions = store.list_sessions()

    assert [record.title for record in sessions] == ["newer", "older"]
    assert store.latest().title == "newer"


def test_session_store_rejects_an_invalid_id(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")

    assert store.load("../../etc/passwd") is None
    with pytest.raises(ValueError, match="invalid session id"):
        store.path_for("../../etc/passwd")


def test_session_store_refuses_symlinked_paths(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "sessions"
    root.symlink_to(outside, target_is_directory=True)
    store = SessionStore(root)

    with pytest.raises(OSError, match="symlinked session directory"):
        store.save(store.new(title="x"))

    real = SessionStore(tmp_path / "real")
    record = real.new(title="x")
    real.save(record)
    target = real.path_for(record.session_id)
    link = real.root / "feedfacefeedface.json"
    link.symlink_to(target)
    assert real.load(link.stem) is None


def test_session_store_prunes_to_the_cap(tmp_path: Path, monkeypatch):
    from ptc_cli.superagent import tui_sessions

    monkeypatch.setattr(tui_sessions, "MAX_SESSIONS", 3)
    store = SessionStore(tmp_path / "sessions")
    for index in range(5):
        record = store.new(title=f"session {index}")
        record.updated_at = float(index)
        store.save(record)

    assert len(store.list_sessions()) == 3


# ---------------------------------------------------------------------------
# permissions
# ---------------------------------------------------------------------------


def test_permission_gate_only_covers_local_mutating_actions():
    manager = PermissionManager()

    assert manager.needs_confirmation("local-merge") is True
    assert manager.needs_confirmation("local-review") is True
    assert manager.needs_confirmation("local-shell") is True
    assert manager.needs_confirmation("anything-else") is False
    assert {"local-merge", "local-review", "local-shell"} == LOCAL_MUTATING_ACTIONS


def test_permission_session_allow_and_auto_approve():
    manager = PermissionManager()
    manager.allow_session("local-shell")
    assert manager.needs_confirmation("local-shell") is False
    assert manager.session_allowed("local-shell") is True

    auto = PermissionManager(auto_approve=True)
    assert auto.needs_confirmation("local-merge") is False


def test_permission_decision_keys():
    assert decision_from_key("a") == "allow"
    assert decision_from_key("S") == "session"
    assert decision_from_key("d") == "deny"
    assert decision_from_key("x") == "pending"
    assert "merge" in PermissionRequest(
        action="local-merge", label="merge line"
    ).render()


# ---------------------------------------------------------------------------
# completions and attachments
# ---------------------------------------------------------------------------


def test_slash_completer_completes_commands():
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    completer = SlashCompleter()
    completions = list(
        completer.get_completions(Document("/jo"), CompleteEvent())
    )
    assert [item.text for item in completions] == ["/jobs"]
    assert "list Tickets" in completions[0].display_meta_text

    assert list(completer.get_completions(Document("/jobs "), CompleteEvent())) == []


def test_at_file_completer_walks_the_local_root(tmp_path: Path):
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "lib.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("hi\n", encoding="utf-8")
    (tmp_path / ".hidden").write_text("secret\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("x", encoding="utf-8")

    completer = AtFileCompleter(tmp_path)
    completions = list(
        completer.get_completions(Document("look @pk"), CompleteEvent())
    )
    texts = [item.text for item in completions]
    assert "pkg/" in texts
    assert not any("hidden" in value for value in texts)
    assert not any("node_modules" in value for value in texts)


def test_tui_completer_routes_slash_and_at():
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    completer = TuiCompleter(root=Path.cwd())
    assert list(completer.get_completions(Document("/he"), CompleteEvent()))
    assert "slash" not in "".join(
        item.text for item in completer.get_completions(Document("/he"), CompleteEvent())
    )
    assert completer.files is not None


def test_expand_mentions_attaches_text_and_reports_errors(tmp_path: Path):
    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01")
    (tmp_path / "big.py").write_text("x" * 200_001, encoding="utf-8")

    prompt, attachments, errors = expand_mentions(
        "look at @a.py and @missing.py", root=tmp_path
    )

    assert "print(1)" in prompt
    assert "Attached file:" in prompt
    assert [attachment.path for attachment in attachments] == ["a.py"]
    assert any("missing.py" in error for error in errors)

    _prompt, _attachments, binary_errors = expand_mentions(
        "read @bin.dat", root=tmp_path
    )
    assert any("binary" in error for error in binary_errors)

    _prompt, _attachments, big_errors = expand_mentions(
        "read @big.py", root=tmp_path
    )
    assert any("attachment limit" in error for error in big_errors)


def test_expand_mentions_caps_attachments(tmp_path: Path):
    names = []
    for index in range(10):
        name = f"f{index}.txt"
        (tmp_path / name).write_text(f"{index}\n", encoding="utf-8")
        names.append(f"@{name}")

    _prompt, attachments, errors = expand_mentions(
        " ".join(names), root=tmp_path, max_attachments=3
    )

    assert len(attachments) == 3
    assert any("attachments are sent" in error for error in errors)


# ---------------------------------------------------------------------------
# local actions
# ---------------------------------------------------------------------------


class _Completed:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_run_conductor_builds_the_cli_command(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CONDUCTOR_WT_ROOT", str(tmp_path))
    captured: dict[str, Any] = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return _Completed(stdout='{"ok": true}\n')

    output = run_conductor(["review", "status"], base_url="http://test", runner=runner)

    assert output == '{"ok": true}'
    assert captured["command"][:5] == [
        __import__("sys").executable,
        "-m",
        "conductor_cli",
        "--url",
        "http://test",
    ]
    assert captured["command"][5:] == ["review", "status"]
    assert captured["kwargs"]["env"]["CONDUCTOR_WT_ROOT"] == str(tmp_path)


def test_run_conductor_surfaces_a_failure(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CONDUCTOR_WT_ROOT", str(tmp_path))

    def runner(command, **kwargs):
        del command, kwargs
        return _Completed(returncode=2, stderr="line worktree is dirty")

    with pytest.raises(Exception, match="line worktree is dirty"):
        run_conductor(["merge", "x"], base_url="http://test", runner=runner)


def test_run_conductor_times_out(monkeypatch, tmp_path: Path):
    import subprocess

    monkeypatch.setenv("CONDUCTOR_WT_ROOT", str(tmp_path))

    def runner(command, **kwargs):
        del command, kwargs
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    with pytest.raises(Exception, match="timed out"):
        run_conductor(["review"], base_url="http://test", runner=runner)


def test_run_shell_runs_the_command_and_tails_output(monkeypatch):
    def runner(command, **kwargs):
        del kwargs
        return _Completed(stdout=f"ran {command}\n")

    assert run_shell("echo hi", runner=runner) == "ran echo hi"


def test_run_shell_error_includes_stderr():
    def runner(command, **kwargs):
        del command, kwargs
        return _Completed(returncode=1, stderr="nope")

    with pytest.raises(Exception, match="nope"):
        run_shell("false", runner=runner)


def test_parse_conductor_args_and_worktree_root(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("CONDUCTOR_WT_ROOT", str(tmp_path))
    assert worktree_root() == tmp_path.resolve()
    assert parse_conductor_args('run line-a --by "line b"') == [
        "run",
        "line-a",
        "--by",
        "line b",
    ]


# ---------------------------------------------------------------------------
# mode selection and runner dispatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("is_tty", "columns", "no_tui", "print_prompt", "env", "expected"),
    [
        (True, 120, False, None, {}, "tui"),
        (False, 120, False, None, {}, "line"),
        (True, 60, False, None, {}, "line"),
        (True, 120, True, None, {}, "line"),
        (True, 120, False, None, {"CONDUCTOR_NO_TUI": "1"}, "line"),
        (True, 120, False, "hello", {}, "print"),
    ],
)
def test_choose_mode(is_tty, columns, no_tui, print_prompt, env, expected):
    assert (
        choose_mode(
            is_tty=is_tty,
            columns=columns,
            no_tui=no_tui,
            print_prompt=print_prompt,
            env=env,
        )
        == expected
    )


@pytest.mark.asyncio
async def test_runner_dispatches_print_mode(monkeypatch):
    from ptc_cli.superagent import runner

    calls: dict[str, Any] = {}

    async def fake_print(**kwargs):
        calls.update(kwargs)
        return 0

    monkeypatch.setattr(runner, "run_print_mode", fake_print)
    code = await runner.run_super_agent(
        server_url="http://test", user_id="u", print_prompt="hello"
    )

    assert code == 0
    assert calls["prompt"] == "hello"


@pytest.mark.asyncio
async def test_runner_dispatches_the_tui(monkeypatch):
    from ptc_cli.superagent import runner

    calls: dict[str, Any] = {}

    async def fake_tui(**kwargs):
        calls.update(kwargs)
        return 7

    monkeypatch.setattr(runner, "run_tui", fake_tui)
    monkeypatch.setattr(runner, "detect_mode", lambda **kwargs: "tui")
    code = await runner.run_super_agent(
        server_url="http://test",
        user_id="u",
        continue_session=True,
    )

    assert code == 7
    assert calls["continue_session"] is True


@pytest.mark.asyncio
async def test_runner_dispatches_the_line_fallback(monkeypatch):
    from ptc_cli.superagent import runner

    class FakeShell:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._daemon_id = ""
            self._daemon_label = ""

        async def run(self):
            return 3

    monkeypatch.setattr(runner, "SuperAgentShell", FakeShell)
    code = await runner.run_super_agent(server_url="http://test", user_id="u")

    assert code == 3


@pytest.mark.asyncio
async def test_asyncio_watch_loop_is_cancellable():
    """The TUI cancels the watch task; the generator must not swallow it."""

    class Client:
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            await asyncio.sleep(30)
            yield _event("end", {"status": "completed"})

    generator = watch_ticket(Client(), "t1")
    task = asyncio.create_task(generator.__anext__())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_rich_views_render_plain_panels_and_tables():
    from ptc_cli.superagent.tui_rich import (
        help_panel,
        model_table,
        render_plain,
        welcome_panel,
    )

    welcome = welcome_panel(width=80)
    assert "Conductor Super-agent TUI" in welcome
    assert "╭" in welcome  # Rich drew the panel border

    help_text = help_panel("Commands\n  /jobs list", width=80)
    assert "/jobs list" in help_text

    table = model_table("deepseek-flash-account", {"codex": ["gpt-5"]}, width=80)
    assert "deepseek-flash-account" in table
    assert "gpt-5" in table

    assert "\x1b" not in render_plain("plain", width=80)


@pytest.mark.asyncio
async def test_watch_timeout_bounds_a_stalled_stream():
    class Stalled:
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            await asyncio.sleep(30)
            yield _event("end", {"status": "completed"})

    started = asyncio.get_running_loop().time()
    events = [
        event
        async for event in watch_ticket(Stalled(), "t1", timeout=0.05)
    ]
    elapsed = asyncio.get_running_loop().time() - started

    assert isinstance(events[-1], TimedOut)
    assert elapsed < 2.0  # not the 30s stall


@pytest.mark.asyncio
async def test_watch_polls_after_an_empty_resumed_stream():
    client = FakeStreamClient(
        streams=[
            [_event("activity", {"event_id": "e1", "kind": "created", "summary": "made"}, "1")],
            [],  # clean close without a frame on the reattach
        ],
        activity_rows=[
            {"event_id": "e2", "kind": "report", "summary": "second", "at": "now"}
        ],
        ticket_statuses=["completed"],
    )

    events = [event async for event in watch_ticket(client, "t1", sleep=_no_sleep)]

    assert any(
        isinstance(event, StreamNotice) and "polling" in event.text for event in events
    )
    assert any(isinstance(event, Activity) and event.event_id == "e2" for event in events)
    assert isinstance(events[-1], End)


def test_corrupt_session_shapes_are_skipped(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    store._ensure_root()
    (store.root / "feedfacefeedface.json").write_text(
        '{"session_id": "feedfacefeedface", "transcript": 1, "attachments": 5}',
        encoding="utf-8",
    )

    assert store.list_sessions() == []




def test_main_parses_the_new_super_agent_flags(monkeypatch):
    import sys

    from ptc_cli.main import parse_args

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "ptc-agent",
            "--super-agent",
            "-p",
            "hello",
            "--continue",
            "--no-tui",
        ],
    )
    args = parse_args()

    assert args.super_agent is True
    assert args.print_prompt == "hello"
    assert args.continue_session is True
    assert args.no_tui is True


def test_formatted_rows_are_one_line_each():
    conversation = Conversation()
    conversation.add(UserBlock(text="one\ntwo\nthree"))
    conversation.feed(OutputChunk(kind="text", text="a\nb"))

    rows = conversation.formatted_lines()

    assert [text for _style, text in rows] == [
        "> one",
        "two",
        "three",
        "",
        "a",
        "b",
    ]


def test_standard_unified_diff_without_git_header_is_colored():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
    parsed = parse_unified_diff(diff)
    assert parsed is not None
    assert [line.marker for line in parsed] == ["header", "header", "hunk", "del", "add"]


def test_session_file_tool_params_and_diff_are_not_read_back():
    """A file saved before tool detail was dropped still opens, without it."""
    leaked = "OLD-SESSION-ARGUMENT"
    restored = Conversation.from_json(
        [
            {
                "kind": "tool",
                "name": "write_file",
                "params": f"path=a.py content={leaked}",
                "status": "done",
                "duration": 1.5,
                "lines": ["ok"],
                "diff": [{"marker": "add", "text": f"+{leaked}"}],
                "expanded": True,
            }
        ]
    )
    (tool,) = restored.tool_blocks()
    assert (tool.name, tool.status, tool.lines) == ("write_file", "done", ["ok"])
    assert not hasattr(tool, "params")
    assert not hasattr(tool, "diff")

    drawn = "\n".join(restored.plain_lines())
    drawn += "\n" + "\n".join(text for _, text in restored.formatted_lines())
    drawn += "\n" + json.dumps(restored.to_json())
    assert "write_file" in drawn
    assert leaked not in drawn
    assert "params" not in drawn
    assert '"diff"' not in drawn


def test_conversation_json_tolerates_nested_bad_blocks():
    conversation = Conversation.from_json(
        [
            {"kind": "user", "text": "ok", "attachments": 1},
            {"kind": "tool", "name": "t", "lines": 3, "diff": 4},
            "not a block",
        ]
    )
    assert [type(block).__name__ for block in conversation.blocks] == ["UserBlock", "ToolBlock"]


def test_session_read_through_symlinked_root_is_refused(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    store = SessionStore(real)
    record = store.new(title="x")
    store.save(record)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    linked = SessionStore(link)

    assert linked.list_sessions() == []
    assert linked.load(record.session_id) is None


def test_malformed_session_id_and_duration_are_safe(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    store._ensure_root()
    raw = {
        "session_id": "bad",
        "transcript": [
            {"kind": "user", "text": "hi"},
            {"kind": "tool", "name": "t", "duration": 1e400},
        ],
    }
    (store.root / "cafebabecafebabe.json").write_text(json.dumps(raw), encoding="utf-8")

    record = store.load("cafebabecafebabe")
    assert record is not None
    assert record.session_id == "cafebabecafebabe"
    # Saving must not raise now that the filename id is authoritative.
    assert store.save(record).exists()

    conversation = Conversation.from_json(record.transcript)
    assert conversation.tool_blocks()[0].duration is None
    assert conversation.formatted_lines()  # rendering a 1e400 duration is safe


def test_format_duration_ignores_non_finite_values():
    assert format_duration(float("inf")) == ""
    assert format_duration(float("nan")) == ""
    assert format_duration(1.5) == "1.5s"


def test_status_line_drops_whole_segments_and_stays_one_row():
    segments = ["auto", "● 1 online", "◆ 0 needing you", "esc interrupt"]
    wide = fit_status_line(segments, 200)
    assert "\n" not in wide
    assert wide == " · ".join(segments)
    narrow = fit_status_line(segments, 8)
    assert narrow == "auto"
    assert "\n" not in narrow


def test_hints_right_align_and_drop_before_counts():
    wide = align_status_line(
        ["auto", "● 1 online"],
        ["shift+enter newline", "alt+m mode"],
        80,
    )
    assert wide.startswith("auto")
    assert wide.endswith("alt+m mode")
    assert "  " in wide
    assert wide.index("online") < wide.index("shift+enter")
    tight = align_status_line(
        ["auto", "● 1 online", "◆ 2 needing you"],
        ["esc interrupt", "ctrl+o expand"],
        36,
    )
    assert "online" in tight
    assert "needing you" in tight
    assert "esc interrupt" not in tight
    assert "ctrl+o" not in tight
    assert "\n" not in tight


def _style_value(style: Any, name: str) -> str:
    for selector, value in style.style_rules:
        if name in str(selector):
            return str(value)
    return ""


def test_no_color_changes_the_style_and_the_transcript_has_no_background():
    painted = build_default_style({"CONDUCTOR_THEME": "dark"})
    quiet = build_default_style({"NO_COLOR": "1", "CONDUCTOR_THEME": "dark"})
    painted_row = _style_value(painted, "conversation")
    quiet_row = _style_value(quiet, "conversation")
    assert "bg:" not in painted_row
    assert "bg:" not in quiet_row
    assert "fg:" in painted_row
    assert "fg:" not in quiet_row


def test_assistant_blocks_split_on_a_run_id_change():
    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="a", run_id="r1"))
    conversation.feed(OutputChunk(kind="text", text="b", run_id="r1"))
    conversation.feed(OutputChunk(kind="text", text="c", run_id="r2"))

    agents = [block for block in conversation.blocks if isinstance(block, AssistantBlock)]
    assert [(block.run_id, block.text) for block in agents] == [
        ("r1", "ab"),
        ("r2", "c"),
    ]
    restored = Conversation.from_json(conversation.to_json())
    assert restored.blocks[1].run_id == "r2"


def _assistants(conversation: Conversation) -> list[AssistantBlock]:
    return [b for b in conversation.blocks if isinstance(b, AssistantBlock)]


def test_output_reset_starts_a_new_assistant_block_and_a_notice():
    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="prefix Bearer ", run_id="r1", epoch=1))
    first = _assistants(conversation)[0]

    conversation.feed(OutputReset(reason="continuity_lost", epoch=2))
    conversation.feed(OutputChunk(kind="text", text="abcdef12345678", run_id="r1", epoch=2))

    blocks = _assistants(conversation)
    assert len(blocks) == 2
    assert blocks[0] is first
    assert first.text == "prefix Bearer "
    assert blocks[1].text == "abcdef12345678"
    assert any(
        isinstance(b, NoticeBlock) and b.text == "live output restarted (continuity lost)"
        for b in conversation.blocks
    )
    assert "Bearer abcdef" not in "\n".join(conversation.plain_lines())
    assert not any(
        "Bearer abcdef" in row[1] for row in conversation.formatted_lines()
    )


def test_output_reset_stays_sealed_when_the_notice_is_compacted_away():
    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="prefix Bearer ", run_id="r1"))
    conversation.feed(OutputReset(reason="gap"))

    # /compact drops notices, so the old fragment is the last block again.
    assert conversation.compact() == 1
    assert isinstance(conversation.blocks[-1], AssistantBlock)
    conversation.feed(OutputChunk(kind="text", text="abcdef", run_id="r1"))

    assert [b.text for b in _assistants(conversation)] == ["prefix Bearer ", "abcdef"]


def test_a_sealed_fragment_keeps_a_visible_boundary_through_compact_and_trim():
    """`/compact` and block trimming drop notices; the boundary between two
    stream epochs must not be blank space alone, or `password ` and
    `=ordinary-value` read as one assignment."""
    from ptc_cli.superagent.tui_blocks import SEALED_BOUNDARY

    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="password ", run_id="r1"))
    conversation.feed(OutputReset(reason="new_epoch"))
    conversation.feed(OutputChunk(kind="text", text="=ordinary-value", run_id="r1"))
    conversation.compact()
    assert not [b for b in conversation.blocks if isinstance(b, NoticeBlock)]

    transcript = conversation.transcript_text()
    assert SEALED_BOUNDARY in transcript
    before, _, after = transcript.partition(SEALED_BOUNDARY)
    assert before.strip().endswith("password") and after.strip() == "=ordinary-value"
    assert "password =ordinary-value" not in " ".join(transcript.split())

    rows = [text for _style, text in conversation.formatted_lines()]
    boundary = [i for i, text in enumerate(rows) if SEALED_BOUNDARY in text]
    assert boundary and rows[boundary[0] - 1].strip() == "password"
    assert rows[boundary[0] + 1 :] and "=ordinary-value" in rows[-1]


def test_a_chunk_from_another_run_never_joins_either():
    from ptc_cli.superagent.tui_blocks import SEALED_BOUNDARY

    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="password \n", run_id="cloud:r1"))
    conversation.add(NoticeBlock(text="between"))
    conversation.feed(
        OutputChunk(kind="text", text="=ordinary-value\n", run_id="daemon:r2", epoch=2)
    )
    conversation.compact()
    transcript = conversation.transcript_text()
    assert SEALED_BOUNDARY in transcript
    before, _, after = transcript.partition(SEALED_BOUNDARY)
    assert before.strip().endswith("password") and after.strip() == "=ordinary-value"


def test_a_chunk_from_another_epoch_never_joins_even_without_a_reset():
    """A rewatch (a fresh `watch_ticket`) forgets its epoch tracking; the
    conversation must not."""
    from ptc_cli.superagent.tui_blocks import SEALED_BOUNDARY

    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="password \n", run_id="r1", epoch=1))
    conversation.add(NoticeBlock(text="between"))
    conversation.feed(
        OutputChunk(kind="text", text="=ordinary-value\n", run_id="r1", epoch=2)
    )
    texts = [b.text for b in _assistants(conversation)]
    assert texts == ["password \n", "=ordinary-value\n"]
    conversation.compact()  # drops the notice
    transcript = conversation.transcript_text()
    assert SEALED_BOUNDARY in transcript
    before, _, after = transcript.partition(SEALED_BOUNDARY)
    assert before.strip().endswith("password") and after.strip() == "=ordinary-value"


def test_the_epoch_survives_a_session_restore_and_still_fences():
    from ptc_cli.superagent.tui_blocks import _block_to_json, block_from_json

    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="password \n", run_id="r1", epoch=1))
    restored = Conversation(
        [block_from_json(_block_to_json(b)) for b in conversation.blocks]
    )
    assert _assistants(restored)[0].epoch == 1
    restored.feed(OutputChunk(kind="text", text="=value\n", run_id="r1", epoch=2))
    assert [b.text for b in _assistants(restored)] == ["password \n", "=value\n"]
    assert _assistants(restored)[0].sealed is True
    # Pre-epoch sessions (no key) still load.
    legacy = block_from_json({"kind": "assistant", "text": "old", "run_id": "r"})
    assert legacy.epoch == 0 and legacy.sealed is False


def test_sealing_for_a_new_watch_leaves_panels_the_tui_wrote_alone():
    conversation = Conversation([AssistantBlock(text="welcome panel")])
    conversation.seal_live_output()
    assert conversation.blocks[0].sealed is False
    conversation.feed(OutputChunk(kind="text", text="live\n", run_id="r1"))
    conversation.seal_live_output()
    assert _assistants(conversation)[-1].sealed is True


def test_a_reset_seals_the_fragment_across_tool_blocks_and_survives_compact():
    """text, tool, reset, compact, text: the notice is compacted away and the
    tool block alone separates the fragments in the block list; the earlier
    text must still carry its own visible boundary."""
    from ptc_cli.superagent.tui_blocks import SEALED_BOUNDARY

    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="password \n", run_id="r1", epoch=1))
    conversation.feed(ToolCall(names=("bash",)))
    conversation.feed(OutputReset(reason="continuity_lost", epoch=1))
    conversation.compact()
    conversation.feed(OutputChunk(kind="text", text="=ordinary\n", run_id="r1", epoch=1))

    first, second = _assistants(conversation)
    assert first.sealed is True and second.sealed is False
    assert SEALED_BOUNDARY in conversation.transcript_text()


def test_output_reset_seals_reasoning_and_sub_agent_fragments_too():
    conversation = Conversation()
    conversation.feed(OutputChunk(kind="reasoning", text="Bearer ", run_id="r1"))
    conversation.feed(OutputReset(reason="new_epoch"))
    conversation.feed(OutputChunk(kind="reasoning", text="abcdef", run_id="r1"))
    conversation.feed(OutputChunk(kind="text", text="tok ", run_id="r1", agent="rev"))
    conversation.feed(OutputReset(reason="gap"))
    conversation.feed(OutputChunk(kind="text", text="en", run_id="r1", agent="rev"))

    assert [(b.reasoning, b.agent, b.text) for b in _assistants(conversation)] == [
        (True, "", "Bearer "),
        (True, "", "abcdef"),
        (False, "rev", "tok "),
        (False, "rev", "en"),
    ]


def test_output_reset_without_prior_output_is_a_plain_notice():
    conversation = Conversation()

    block = conversation.feed(OutputReset(reason=""))
    conversation.feed(OutputChunk(kind="text", text="x", run_id="r1"))

    assert isinstance(block, NoticeBlock)
    assert block.text == "live output restarted"
    assert [b.text for b in _assistants(conversation)] == ["x"]


def test_output_reset_finishes_running_tools_like_a_gap():
    conversation = Conversation()
    conversation.feed(ToolCall(names=("read_file",)), now=lambda: 1.0)
    assert conversation.tool_blocks()[0].status == "running"

    conversation.feed(OutputReset(reason="gap"), now=lambda: 3.0)

    tool = conversation.tool_blocks()[0]
    assert tool.status == "done"
    assert tool.duration == 2.0


def test_output_reset_notice_is_sanitized_when_rendered():
    conversation = Conversation()
    conversation.feed(OutputReset(reason="x\x1b]52;c;ZXZpbA==\x07y\x1b[2Jz"))

    rendered = "\n".join(conversation.plain_lines())
    rows = "".join(row[1] for row in conversation.formatted_lines())
    for value in (rendered, rows):
        assert "\x1b" not in value
        assert "\x07" not in value
        assert "live output restarted (xyz)" in value


def test_sealed_assistant_block_round_trips_and_stays_backward_compatible():
    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="prefix Bearer ", run_id="r1"))
    conversation.feed(OutputReset(reason="gap"))
    rows = conversation.to_json()
    assert rows[0]["sealed"] is True

    restored = Conversation.from_json(rows)
    assert _assistants(restored)[0].sealed is True
    restored.compact()
    restored.feed(OutputChunk(kind="text", text="abcdef", run_id="r1"))
    assert [b.text for b in _assistants(restored)] == ["prefix Bearer ", "abcdef"]

    # Unsealed blocks serialize exactly as before; old sessions load unsealed.
    plain_rows = Conversation()
    plain_rows.feed(OutputChunk(kind="text", text="hi", run_id="r1"))
    assert plain_rows.to_json() == [
        {
            "kind": "assistant",
            "text": "hi",
            "reasoning": False,
            "agent": "",
            "run_id": "r1",
            "worker_detail": True,
        }
    ]
    legacy = Conversation.from_json(plain_rows.to_json())
    assert _assistants(legacy)[0].sealed is False
    legacy.feed(OutputChunk(kind="text", text=" there", run_id="r1"))
    assert [b.text for b in _assistants(legacy)] == ["hi there"]


def test_overflowing_session_numbers_do_not_crash_restore(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    store._ensure_root()
    huge = 10**400
    raw = {
        "session_id": "feedfacefeedface",
        "created_at": huge,
        "updated_at": huge,
        "transcript": [
            {"kind": "tool", "name": "t", "duration": huge},
        ],
    }
    (store.root / "feedfacefeedface.json").write_text(json.dumps(raw), encoding="utf-8")

    record = store.load("feedfacefeedface")
    assert record is not None
    assert record.created_at == 0.0

    conversation = Conversation.from_json(record.transcript)
    assert conversation.tool_blocks()[0].duration is None
    assert conversation.formatted_lines()
    assert format_duration(huge) == ""
    assert format_duration(float("inf")) == ""


@pytest.mark.asyncio
async def test_polling_is_bounded_by_the_watch_deadline():
    class SlowPoll:
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            message = "no stream"
            raise ActivityStreamUnavailableError(message)
            yield  # pragma: no cover

        async def ticket(self, ticket_id: str):
            await asyncio.sleep(0.2)
            return {"status": "running"}

        async def activity(self, ticket_id: str):
            await asyncio.sleep(0.2)
            return {"events": []}

    started = asyncio.get_running_loop().time()
    events = [event async for event in watch_ticket(SlowPoll(), "t1", timeout=0.05)]
    elapsed = asyncio.get_running_loop().time() - started

    assert isinstance(events[-1], TimedOut)
    assert elapsed < 1.0


def test_negative_and_overflowing_durations_are_rejected():
    conversation = Conversation.from_json(
        [
            {"kind": "tool", "name": "a", "duration": -1e308},
            {"kind": "tool", "name": "b", "duration": 10**400},
            {"kind": "tool", "name": "c", "duration": 1e400},
        ]
    )

    assert [tool.duration for tool in conversation.tool_blocks()] == [None, None, None]
    assert format_duration(-1e308) == ""
    assert format_duration(10**400) == ""


def test_negative_session_timestamps_are_dropped(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions")
    store._ensure_root()
    raw = {
        "session_id": "feedfacefeedface",
        "created_at": -1e308,
        "updated_at": -1,
    }
    (store.root / "feedfacefeedface.json").write_text(json.dumps(raw), encoding="utf-8")

    record = store.load("feedfacefeedface")
    assert record is not None
    assert record.created_at == 0.0
    assert record.updated_at == 0.0


@pytest.mark.asyncio
async def test_polling_tolerates_missing_payloads():
    class NonePoll:
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            message = "no stream"
            raise ActivityStreamUnavailableError(message)
            yield  # pragma: no cover

        async def ticket(self, ticket_id: str):
            return None

        async def activity(self, ticket_id: str):
            return None

    events = [event async for event in watch_ticket(NonePoll(), "t1", timeout=0.05)]

    assert isinstance(events[-1], TimedOut)


@pytest.mark.asyncio
async def test_polling_tolerates_malformed_event_collections():
    class BadEvents:
        async def stream_activity(self, ticket_id: str, *, last_event_id=None):
            message = "no stream"
            raise ActivityStreamUnavailableError(message)
            yield  # pragma: no cover

        async def ticket(self, ticket_id: str):
            return {"status": "running"}

        async def activity(self, ticket_id: str):
            return {"events": 1}

    events = [event async for event in watch_ticket(BadEvents(), "t1", timeout=0.05)]

    assert isinstance(events[-1], TimedOut)


@pytest.mark.asyncio
async def test_client_home_workspace_validates_the_response(monkeypatch):
    from ptc_cli.superagent.client import SuperAgentClient, SuperAgentError

    client = SuperAgentClient("http://test", "u1")

    async def null_request(*args, **kwargs):
        return None

    async def empty_request(*args, **kwargs):
        return {}

    monkeypatch.setattr(client, "_request", null_request)
    with pytest.raises(SuperAgentError, match="not a JSON object"):
        await client.home_workspace()

    monkeypatch.setattr(client, "_request", empty_request)
    with pytest.raises(SuperAgentError, match="workspace id"):
        await client.home_workspace()



@pytest.mark.asyncio
async def test_a_cloud_tool_then_daemon_text_is_a_change_of_source():
    """Reviewer repro for `cond -p`: `tool: Bearer` from one source followed by
    `abcdefgh` from another must not read as one fragment."""
    client = FakeStreamClient(
        streams=[
            [
                _event("output", {"kind": "tool", "tools": ["Bearer"], "run": "run-old"}),
                _text_frame("abcdefgh", run="daemon:r2", epoch=3),
                _event("end", {"status": "completed"}),
            ]
        ]
    )
    events = [event async for event in watch_ticket(client, "t1")]
    assert [type(event) for event in events] == [ToolCall, OutputReset, OutputChunk, End]
    assert events[0].run_id == "run-old"


def test_a_gap_frame_seals_the_fragment_like_a_reset():
    """Dropped output is a discontinuity, like a reset.

    The notice between the two sides does not survive /compact or trimming, so
    the block itself must be closed.
    """
    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="sk-ab", run_id="r1"))
    conversation.feed(Gap(text="Some Worker output was dropped"))

    assert conversation.compact() == 1
    conversation.feed(OutputChunk(kind="text", text="cdefghij", run_id="r1"))

    blocks = _assistants(conversation)
    assert [b.text for b in blocks] == ["sk-ab", "cdefghij"]
    assert blocks[0].sealed is True
    assert blocks[1].sealed is False


def test_hidden_worker_boundary_seals_before_details_are_shown_again():
    from ptc_cli.superagent.tui_events import OutputReset

    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="prefix", run_id="r1", epoch=1))
    conversation.feed(OutputReset(reason="gap", epoch=2), show_worker_details=False)
    conversation.feed(
        OutputChunk(kind="text", text="suffix", run_id="r1", epoch=1)
    )
    conversation.feed(
        OutputChunk(kind="text", text="later", run_id="r2", epoch=2)
    )

    text = conversation.transcript_text()
    assert "prefixsuffix" not in text
    assert "prefix" in text and "later" in text


def test_legacy_streamed_blocks_are_supervisor_hidden():
    restored = Conversation.from_json(
        [
            {
                "kind": "assistant",
                "text": "old worker detail",
                "reasoning": False,
                "agent": "",
                "run_id": "legacy-run",
                "worker_detail": False,
            }
        ]
    )
    rows = restored.formatted_lines(show_worker_details=False)
    assert "old worker detail" not in "\n".join(text for _, text in rows)
    assert "old worker detail" in "\n".join(
        text for _, text in restored.formatted_lines(show_worker_details=True)
    )


@pytest.mark.parametrize(
    ("status", "title", "question", "error"),
    [
        ("completed", "✓ result", "", ""),
        ("failed", "✗ result", "", "worker exploded"),
        ("cancelled", "• result", "", ""),
        ("waiting", "? result", "approve the plan?", ""),
        ("blocked", "• result", "approve the plan?", ""),
        ("archived", "• result", "", ""),
    ],
)
def test_supervisor_result_cards_cover_terminal_and_waiting_states(
    status: str,
    title: str,
    question: str,
    error: str,
):
    conversation = Conversation()
    conversation.feed(
        End(
            status=status,
            summary="final summary",
            question=question,
            error=error,
        ),
        show_worker_details=False,
    )

    rendered = "\n".join(
        text
        for _style, text in conversation.formatted_lines(show_worker_details=False)
    )
    assert title in rendered
    assert f"status: {status}" in rendered
    if status in {"completed", "failed", "cancelled", "archived"}:
        assert "summary: final summary" in rendered
    else:
        assert "final summary" not in rendered
        assert "summary:" not in rendered
    if error:
        assert f"error: {error}" in rendered
    else:
        assert "error:" not in rendered
    if question:
        assert f"needs you: {question}" in rendered
    else:
        assert "needs you:" not in rendered


@pytest.mark.asyncio
async def test_failed_stream_end_loads_the_ticket_error():
    class FailedClient(FakeStreamClient):
        async def ticket(self, ticket_id: str) -> dict[str, Any]:
            return {
                "ticket_id": ticket_id,
                "status": "failed",
                "summary": "worker stopped",
                "error": "durable failure detail",
            }

    client = FailedClient(
        streams=[
            [_event("end", {"status": "failed", "summary": "worker stopped"})]
        ]
    )

    events = [event async for event in watch_ticket(client, "t1")]

    assert isinstance(events[-1], End)
    assert events[-1].status == "failed"
    assert events[-1].error == "durable failure detail"


@pytest.mark.asyncio
async def test_failed_poll_preserves_error_and_question():
    class FailedClient(FakeStreamClient):
        async def ticket(self, ticket_id: str) -> dict[str, Any]:
            return {
                "ticket_id": ticket_id,
                "status": "failed",
                "summary": "worker stopped",
                "error": "polled failure detail",
                "attention": {"question": "retry the Ticket?"},
            }

    client = FailedClient(
        streams=[ActivityStreamUnavailableError("no stream")],
    )

    events = [event async for event in watch_ticket(client, "t1", sleep=_no_sleep)]

    assert isinstance(events[-1], End)
    assert events[-1].error == "polled failure detail"
    assert events[-1].question == "retry the Ticket?"


def test_progress_card_round_trips_through_the_session():
    from ptc_cli.superagent.tui_blocks import ProgressBlock

    conversation = Conversation(
        [
            ProgressBlock(
                ticket_id="ticket-a",
                phase="completed",
                stage="Ticket completed",
                started_at=100.0,
                finished_at=110.0,
                activity_kind="closed",
                activity_summary="done",
                summary="short summary",
            )
        ]
    )

    restored = Conversation.from_json(conversation.to_json())
    progress = restored.blocks[0]
    assert isinstance(progress, ProgressBlock)
    assert progress.ticket_id == "ticket-a"
    assert progress.elapsed(999.0) == 10.0
    assert progress.activity_kind == "closed"
    assert "summary: short summary" in "\n".join(progress.render_plain())
    rendered = "\n".join(
        text
        for _style, text in restored.formatted_lines(
            show_worker_details=False,
            now=lambda: 999.0,
        )
    )
    assert "phase: completed" in rendered
    assert "elapsed: 10.0s" in rendered
    assert "activity: closed done" in rendered


def test_progress_card_does_not_render_report_body():
    from ptc_cli.superagent.tui_blocks import ProgressBlock

    conversation = Conversation.from_json(
        [
            {
                "kind": "progress",
                "ticket_id": "ticket-a",
                "phase": "completed",
                "summary": "short summary",
                "body": "full report",
            }
        ]
    )
    progress = conversation.blocks[0]
    assert isinstance(progress, ProgressBlock)

    plain_rows = progress.render_plain()
    formatted_rows = [text for _style, text in conversation.formatted_lines()]
    assert "summary: short summary" in plain_rows
    assert "summary: short summary" in formatted_rows
    assert "full report" not in "\n".join([*plain_rows, *formatted_rows])
    assert not hasattr(progress, "body")


def test_result_card_renders_report_body():
    conversation = Conversation(
        [EndBlock(status="completed", summary="short summary", body="full report")]
    )

    assert conversation.blocks[0].render_plain() == [
        "status: completed",
        "full report",
    ]
    assert [text for _style, text in conversation.formatted_lines()] == [
        "✓ result",
        "status: completed",
        "full report",
    ]


def test_result_card_skips_body_matching_summary():
    conversation = Conversation(
        [EndBlock(status="completed", summary="final summary", body=" final summary\n")]
    )

    assert conversation.blocks[0].render_plain() == [
        "status: completed",
        "final summary",
    ]
    assert [text for _style, text in conversation.formatted_lines()] == [
        "✓ result",
        "status: completed",
        "final summary",
    ]


def test_result_card_without_body_is_unchanged():
    conversation = Conversation(
        [EndBlock(status="completed", summary="final summary")]
    )

    assert conversation.blocks[0].render_plain() == [
        "status: completed",
        "final summary",
    ]
    assert [text for _style, text in conversation.formatted_lines()] == [
        "✓ result",
        "status: completed",
        "summary: final summary",
    ]


def test_archived_result_card_renders_body_like_cancelled():
    archived = Conversation(
        [EndBlock(status="archived", summary="final summary", body="full report")]
    )
    cancelled = Conversation(
        [EndBlock(status="cancelled", summary="final summary", body="full report")]
    )

    assert archived.blocks[0].render_plain() == [
        "status: archived",
        "full report",
    ]
    assert archived.blocks[0].render_plain()[1:] == cancelled.blocks[0].render_plain()[1:]
    archived_rows = [text for _style, text in archived.formatted_lines()]
    cancelled_rows = [text for _style, text in cancelled.formatted_lines()]
    assert archived_rows == ["• result", "status: archived", "full report"]
    assert archived_rows[2:] == cancelled_rows[2:]


def test_null_result_body_renders_no_body_row():
    conversation = Conversation(
        [EndBlock(status="completed", summary="final summary", body=None)]
    )

    assert conversation.blocks[0].render_plain() == [
        "status: completed",
        "final summary",
    ]
    assert "body:" not in "\n".join(text for _style, text in conversation.formatted_lines())


def test_legacy_reasoning_and_subagent_text_are_hidden_without_a_run_id():
    restored = Conversation.from_json(
        [
            {
                "kind": "assistant",
                "text": "legacy private reasoning",
                "reasoning": True,
                "agent": "reviewer",
            }
        ]
    )

    hidden = "\n".join(
        text for _style, text in restored.formatted_lines(show_worker_details=False)
    )
    shown = "\n".join(
        text for _style, text in restored.formatted_lines(show_worker_details=True)
    )
    assert "legacy private reasoning" not in hidden
    assert "legacy private reasoning" in shown


def test_worker_reset_and_gap_notices_follow_details_after_restore():
    conversation = Conversation()
    conversation.feed(OutputChunk(kind="text", text="worker-token", run_id="r1"))
    conversation.feed(OutputReset(reason="gap"), show_worker_details=True)
    conversation.feed(Gap(text="Some Worker output was dropped"), show_worker_details=True)
    rows = conversation.to_json()
    for row in rows:
        if row["kind"] == "notice":
            row.pop("worker_detail", None)

    restored = Conversation.from_json(rows)
    hidden = "\n".join(
        text for _style, text in restored.formatted_lines(show_worker_details=False)
    )
    shown = "\n".join(
        text for _style, text in restored.formatted_lines(show_worker_details=True)
    )
    for worker_text in (
        "worker-token",
        "live output restarted",
        "Some Worker output was dropped",
    ):
        assert worker_text not in hidden
        assert worker_text in shown
