"""Direction-changing and invisible code points never reach the terminal.

Server text (Worker tokens, summaries, activity) is untrusted for display: the
bidirectional controls reorder what is drawn, the tag block and variation
selectors carry text nobody can see, and the zero-width characters hide inside
words. Every path that prints server text strips them with the escape
sequences, so each path is driven here with the same hostile string.
"""

from __future__ import annotations

import io
import sys
import unicodedata
from pathlib import Path
from typing import Any

import pytest

# tui imports conductor_cli, which lives in the repo src tree; the ptc-cli
# lockfile's conductor-core is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent import text as text_module  # noqa: E402
from ptc_cli.superagent.client import ActivityStreamEvent  # noqa: E402
from ptc_cli.superagent.shell import SuperAgentShell  # noqa: E402
from ptc_cli.superagent.text import TokenSanitizer, plain, text  # noqa: E402
from ptc_cli.superagent.tui import SuperAgentTui, run_print_mode  # noqa: E402
from ptc_cli.superagent.tui_blocks import Conversation, PermissionBlock, ToolBlock  # noqa: E402
from ptc_cli.superagent.tui_completion import Attachment, TuiCompleter, expand_mentions  # noqa: E402
from ptc_cli.superagent.tui_events import (  # noqa: E402
    Activity,
    End,
    Gap,
    OutputChunk,
    OutputError,
    OutputReset,
    ToolCall,
)
from ptc_cli.superagent.tui_sessions import SessionStore  # noqa: E402
from tests.unit_tests.test_superagent_shell import FakeClient as ShellClient  # noqa: E402
from tests.unit_tests.test_superagent_shell import StreamClient, _sse  # noqa: E402
from tests.unit_tests.test_superagent_tui_app import FakeClient as TuiClient  # noqa: E402

# One representative of every class the table covers, plus the range edges.
HIDDEN_CLASSES = {
    "bidi mark ALM": "\u061c",
    "bidi mark LRM": "\u200e",
    "bidi mark RLM": "\u200f",
    "bidi embedding": "\u202a\u202b\u202c",
    "bidi override": "\u202d\u202e",
    "bidi isolate": "\u2066\u2067\u2068\u2069",
    "tag character": "\U000e0001\U000e0041\U000e007f",
    "tag block reserved": "\U000e0080\U000e00ff\U000e01f0\U000e0fff",
    "variation selector": "\ufe00\ufe0e\ufe0f",
    "variation selector supplement": "\U000e0100\U000e01ef",
    "zero width": "\u200b\u200c\u200d\u2060\ufeff",
    "invisible operators": "\u2061\u2062\u2063\u2064\u206a\u206f",
    "soft hyphen and joiner": "\u00ad\u034f",
    "fillers": "\u115f\u1160\u3164\uffa0",
    "mongolian": "\u180b\u180e\u180f",
    "arabic format": "\u0600\u0605\u06dd\u070f\u08e2",
    "specials": "\ufff9\ufffb",
    # Built at run time: a literal pair of escapes is joined into one character.
    "lone surrogate": chr(0xD800) + chr(0xDFFF),
}
EVERYTHING = "".join(HIDDEN_CLASSES.values())
POISON = f"A{EVERYTHING}B"
CLEAN = "AB"


def _no_hidden(output: str) -> None:
    leaked = sorted({hex(ord(c)) for c in output if c in EVERYTHING})
    assert not leaked, leaked


@pytest.mark.parametrize("sample", HIDDEN_CLASSES.values(), ids=HIDDEN_CLASSES)
def test_each_hidden_class_is_stripped_by_every_entry_point(sample):
    hostile = f"a{sample}b"
    assert plain(hostile) == "ab"
    assert text(hostile) == "ab"
    whole = TokenSanitizer()
    assert whole.feed(hostile) + whole.flush() == "ab"
    # Any split of a stream filters like the whole text.
    pieces = TokenSanitizer()
    assert "".join(pieces.feed(char) for char in hostile) + pieces.flush() == "ab"


def test_every_format_character_is_covered():
    """A Unicode release that adds a format character must not slip past."""
    format_characters = [
        chr(code) for code in range(0x110000) if unicodedata.category(chr(code)) == "Cf"
    ]
    assert [hex(ord(c)) for c in format_characters if plain(c) != ""] == []


def test_visible_text_is_kept():
    kept = (
        "\u4e2d\u6587 caf\u00e9 na\u00efve \u05e9\u05dc\u05d5\u05dd \u0645\u0631\u062d\u0628\u0627 "
        "\U0001f1e8\U0001f1f3 \U0001f44d\U0001f3fd "
        "\U0001f600 \ttab\nnewline \u2260 \u2010 \u2014 \u2026 \ufb01"
    )
    assert plain(kept) == kept
    assert TokenSanitizer().feed(kept) == kept


def test_a_control_sequence_with_a_hidden_character_inside_is_removed_whole():
    assert plain("a\x1b\u200b]52;c;QQ==\x07b") == "ab"
    assert plain("a\x1b[3\u202e1mb") == "ab"
    sanitizer = TokenSanitizer()
    assert sanitizer.feed("a\x1b") == "a"
    assert sanitizer.feed("\u200b]52;c;QQ==\x07b") == "b"


def test_attachments_keep_what_the_model_should_read():
    """Display filtering must not rewrite a file that is sent as the prompt."""
    body = "x\u200dy\ufe0f\u202e z\x1b[31m!"
    prompt, _, errors = expand_mentions(
        "look @f.txt",
        root="/",
        reader=lambda path: Attachment(path=str(path), content=body, size=len(body)),
    )
    assert errors == []
    assert "x\u200dy\ufe0f\u202e z!" in prompt
    assert "\x1b" not in prompt
    assert text_module.strip_controls(body) == "x\u200dy\ufe0f\u202e z!"


# ---------------------------------------------------------------------------
# line shell
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_line_shell_live_stream_strips_hidden_characters(capsys):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = StreamClient(
        [
            [
                _sse("activity", {"event_id": "e", "at": POISON, "kind": POISON, "summary": POISON}),
                _sse("output", {"kind": "text", "text": POISON, "run": "r", "epoch": 1}),
                _sse("output", {"kind": "reasoning", "text": POISON, "run": "r", "epoch": 1}),
                _sse("output", {"kind": "tool", "tools": [POISON], "run": "r"}),
                _sse("output", {"kind": "error", "text": POISON}),
                _sse("output", {"kind": "gap", "text": POISON}),
                _sse("output", {"kind": "output_reset", "reason": POISON, "run": "r", "epoch": 2}),
                _sse("end", {"status": "completed", "summary": POISON, "question": POISON}),
            ]
        ]
    )

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    _no_hidden(out)
    assert CLEAN in out
    assert "completed" in out


@pytest.mark.asyncio
async def test_line_shell_tokens_split_inside_a_hidden_run_print_clean(capsys):
    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    pieces = [POISON[i : i + 3] for i in range(0, len(POISON), 3)]
    shell.client = StreamClient(
        [
            [_sse("output", {"kind": "text", "text": piece, "run": "r"}) for piece in pieces]
            + [_sse("end", {"status": "completed", "summary": ""})]
        ]
    )

    await shell.watch_job("ticket-1", timeout=5.0)

    out = capsys.readouterr().out
    _no_hidden(out)
    assert CLEAN in out


@pytest.mark.asyncio
async def test_line_shell_ticket_detail_and_event_record_strip_hidden_characters(capsys):
    class Poisoned(ShellClient):
        async def ticket(self, ticket_id: str) -> dict[str, Any]:
            return {
                "ticket_id": "ticket-1",
                "status": "failed",
                "title": POISON,
                "scope": POISON,
                "worker_provider": POISON,
                "worker_model": POISON,
                "origin_thread_key": POISON,
                "attention": {"question": POISON},
                "reports": [{"state": POISON, "summary": POISON}],
            }

        async def activity(self, ticket_id: str) -> dict[str, Any]:
            return {"events": [{"event_id": "e", "at": POISON, "kind": POISON, "summary": POISON}]}

        async def tickets(self, **params: Any) -> dict[str, Any]:
            return {"tickets": [{"ticket_id": "ticket-1", "status": POISON, "title": POISON}]}

    shell = SuperAgentShell(server_url="http://test", user_id="u1", no_splash=True)
    shell.client = Poisoned()  # type: ignore[assignment]

    await shell.handle("/jobs")
    await shell.handle("/jobs ticket-1")

    out = capsys.readouterr().out
    _no_hidden(out)
    assert CLEAN in out


# ---------------------------------------------------------------------------
# print mode
# ---------------------------------------------------------------------------


def _all_frames() -> list[ActivityStreamEvent]:
    def frame(name: str, data: dict[str, Any], event_id: str) -> ActivityStreamEvent:
        return ActivityStreamEvent(event=name, data=data, event_id=event_id)

    return [
        frame("activity", {"event_id": "e", "at": POISON, "kind": POISON, "summary": POISON, "detail": POISON}, "1"),
        frame("output", {"kind": "text", "text": POISON, "run": "r", "epoch": 1}, "2"),
        frame("output", {"kind": "tool", "tools": [POISON]}, "3"),
        frame("output", {"kind": "error", "text": POISON}, "4"),
        frame("output", {"kind": "gap", "text": POISON}, "5"),
        frame("output", {"kind": "output_reset", "reason": POISON, "epoch": 2}, "6"),
        frame("end", {"status": "completed", "summary": POISON, "question": POISON}, "7"),
    ]


@pytest.mark.asyncio
async def test_print_mode_strips_hidden_characters_from_every_frame():
    writes: list[str] = []
    code = await run_print_mode(
        server_url="http://test",
        user_id="u1",
        prompt="x",
        client=TuiClient(stream=_all_frames()),
        write=writes.append,
    )

    out = "".join(writes)
    assert code == 0
    _no_hidden(out)
    assert CLEAN in out


@pytest.mark.asyncio
async def test_print_mode_survives_a_lone_surrogate_on_a_strict_stream(monkeypatch):
    """A surrogate cannot be encoded; it must not abort the watch mid-stream."""
    raw = io.BytesIO()
    strict = io.TextIOWrapper(raw, encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", strict)
    stream = [
        ActivityStreamEvent(event="output", data={"kind": "text", "text": "a\ud800b"}, event_id="1"),
        ActivityStreamEvent(event="end", data={"status": "completed", "summary": "ok"}, event_id="2"),
    ]

    code = await run_print_mode(
        server_url="http://test", user_id="u1", prompt="x", client=TuiClient(stream=stream)
    )

    strict.flush()
    assert code == 0
    assert b"ab" in raw.getvalue()
    assert b"codec" not in raw.getvalue()


# ---------------------------------------------------------------------------
# full-screen TUI
# ---------------------------------------------------------------------------


def _render(conversation: Conversation) -> str:
    rows = "\n".join(value for _, value in conversation.formatted_lines())
    return rows + "\n" + conversation.transcript_text()


def test_tui_blocks_strip_hidden_characters_from_every_event():
    conversation = Conversation()
    for event in (
        OutputChunk(kind="text", text=POISON, run_id="r", agent=POISON, epoch=1),
        OutputChunk(kind="reasoning", text=POISON, run_id="r", epoch=1),
        Activity(at=POISON, kind=POISON, summary=POISON, detail=POISON, event_id="e"),
        ToolCall(names=(POISON,), run_id=POISON),
        OutputError(text=POISON),
        Gap(text=POISON),
        OutputReset(reason=POISON),
        End(status=POISON, summary=POISON, question=POISON),
    ):
        conversation.feed(event)

    out = _render(conversation)
    _no_hidden(out)
    assert CLEAN in out


def test_tui_tool_lines_are_stripped_when_drawn_not_only_when_stored():
    """A restored session file is as untrusted as the stream was."""
    restored = Conversation.from_json(
        [
            {
                "kind": "tool",
                "name": POISON,
                "status": POISON,
                "lines": [POISON + "\x1b[2J"],
            },
            {"kind": "permission", "action": POISON, "label": POISON, "detail": POISON, "decision": POISON},
        ]
    )
    live = Conversation(
        [ToolBlock(name="t", status="done", lines=[POISON]), PermissionBlock("a", "l", decision=POISON)]
    )

    for conversation in (restored, live):
        out = _render(conversation)
        _no_hidden(out)
        assert "\x1b" not in out


def test_tui_status_bar_strips_hidden_characters(tmp_path: Path):
    tui = SuperAgentTui(
        server_url="http://test",
        user_id="u1",
        client=TuiClient(),
        session_store=SessionStore(tmp_path / "sessions"),
        history_path=tmp_path / "history",
    )
    # The one status row shows the mode, not the router, daemon, or session.
    tui.interaction_mode = POISON
    tui.status.router_model = POISON
    tui.status.daemon = POISON
    tui.status.session = POISON

    out = "".join(value for _, value in tui._status_fragments())

    _no_hidden(out)
    assert CLEAN in out


def test_the_at_completion_menu_draws_local_file_names_clean(tmp_path: Path):
    """The menu shows a name like ``notes<RLO>txt.sh`` as the other paths do.

    Accepting a completion inserts the real name.
    """
    from prompt_toolkit.completion import CompleteEvent
    from prompt_toolkit.document import Document

    names = {
        "notes\u202etxt.sh": "notestxt.sh",
        "ok\u200b.py": "ok.py",
        "tag\U000e0041\U000e0042.md": "tag.md",
        "plain.txt": "plain.txt",
    }
    for name in names:
        (tmp_path / name).write_text("x", encoding="utf-8")
    items = list(TuiCompleter(root=tmp_path).get_completions(Document("see @"), CompleteEvent()))

    shown = {"".join(part for _, part in item.display) for item in items}
    assert shown == set(names.values())
    _no_hidden("".join(shown))
    assert {item.text for item in items} == set(names)


@pytest.mark.asyncio
async def test_tui_jobs_screen_strips_hidden_characters(tmp_path: Path):
    """The S06 /jobs rows are one of the TUI's server-text paths."""

    class Poisoned(TuiClient):
        async def tickets(self, **params: Any) -> dict[str, Any]:
            del params
            return {
                "tickets": [
                    {
                        "ticket_id": "ticket-1",
                        "status": "failed",
                        "title": POISON,
                        "worker_model": POISON,
                    }
                ]
            }

    tui = SuperAgentTui(
        server_url="http://test",
        user_id="u1",
        client=Poisoned(),
        session_store=SessionStore(tmp_path / "sessions"),
        history_path=tmp_path / "history",
        local_root=tmp_path,
        worktree_root=tmp_path,
        no_splash=True,
    )
    await tui.startup()
    await tui.handle_command("/jobs")
    out = "\n".join(value for _style, value in tui.present_transcript())
    _no_hidden(out)
    assert CLEAN in out
    await tui.close()
