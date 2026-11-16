"""Card-facing block behaviour: durations, result text, collapse, cache version."""

from __future__ import annotations

import sys
from pathlib import Path

# conductor_cli lives in the repo src tree; the ptc-cli venv does not install it.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui_blocks import (  # noqa: E402
    AssistantBlock,
    Conversation,
    EndBlock,
    PermissionBlock,
    ProgressBlock,
    ToolBlock,
    UserBlock,
    format_duration,
)
from ptc_cli.superagent.tui_events import ToolCall  # noqa: E402
from ptc_cli.superagent.tui_rich import help_panel, welcome_panel  # noqa: E402


def test_a14_format_duration_hours_and_sub_minute():
    assert format_duration(42) == "42.0s"
    assert format_duration(28635.4) == "7h57m"
    assert format_duration(7552.5) == "2h05m"
    # Leftover seconds under an hour are kept; under a minute stays one decimal.
    assert format_duration(90) == "1m30s"
    assert format_duration(61) == "1m01s"
    assert format_duration(0.24) == "240ms"
    # 2h05m52s displays as 2h05m.
    assert format_duration(7200 + 5 * 60 + 52) == "2h05m"


def test_end_block_persists_body_and_steps_and_old_rows_still_load():
    steps = [{"key": "created", "label": "任务已创建", "state": "done"}]
    block = EndBlock(
        status="completed",
        summary="short summary",
        body="full report",
        steps=steps,
    )
    raw = Conversation([block]).to_json()[0]

    assert raw["body"] == "full report"
    assert raw["steps"] == steps
    assert raw["summary"] == "short summary"
    assert raw["status"] == "completed"

    restored = Conversation.from_json([raw]).blocks[0]
    assert isinstance(restored, EndBlock)
    assert restored.body == "full report"
    assert restored.steps == steps

    legacy = Conversation.from_json(
        [{"kind": "end", "status": "completed", "summary": "restored result"}]
    ).blocks[0]
    assert isinstance(legacy, EndBlock)
    assert legacy.summary == "restored result"
    assert legacy.body == ""
    assert legacy.steps == []


def test_mutable_blocks_start_at_version_zero_and_bump_on_content():
    assistant = AssistantBlock(text="hi")
    progress = ProgressBlock(ticket_id="t", phase="queued", stage="Waiting", started_at=1.0)
    tool = ToolBlock(name="bash", started=1.0, worker_detail=True)
    result = EndBlock(status="completed", summary="s", question="q", error="e")
    permission = PermissionBlock("a", "l", decision="pending")
    UserBlock(text="line", attachments=["note.txt"])

    assert assistant.version == 0
    assert progress.version == 0
    assert tool.version == 0
    assert result.version == 0
    assert permission.version == 0
    assert tool.expanded is False

    assistant.append("!")
    assert assistant.version == 1
    assistant.sealed = True
    assert assistant.version == 2
    assistant.sealed = True
    assert assistant.version == 2

    progress.phase = "running"
    assert progress.version == 1
    progress.phase = "running"
    assert progress.version == 1

    tool.add_line("out")
    assert tool.version == 1
    tool.expanded = True
    assert tool.version == 2

    result.body = "report"
    assert result.version == 1
    result.steps = [{"key": "worker", "state": "done"}]
    assert result.version == 2

    permission.decision = "deny"
    assert permission.version == 1


def test_collapsed_tool_preview_uses_glyph_and_expand_still_opens_it():
    lines = [f"line {index}" for index in range(6)]
    tool = ToolBlock(name="bash", status="done", duration=1.5, lines=lines)
    assert tool.expanded is False

    collapsed = tool.render_plain()
    assert collapsed[0].startswith("⚙ bash")
    assert collapsed[1:4] == ["╰ line 0", "╰ line 1", "╰ line 2"]
    assert collapsed[4] == "… 3 more lines (ctrl+o to expand)"

    conversation = Conversation([tool])
    formatted = [text for _style, text in conversation.formatted_lines()]
    assert "╰ line 0" in formatted
    assert "… 3 more lines (ctrl+o to expand)" in formatted

    assert conversation.expand_tools() == 1
    assert tool.expanded is True
    opened = tool.render_plain()
    assert "line 5" in opened[-1]
    assert "ctrl+o" not in "\n".join(opened)
    assert conversation.collapse_tools() == 1
    assert tool.expanded is False
    assert conversation.toggle_last_tool() is tool
    assert tool.expanded is True

    # A file that stored the flag still loads it. A missing key stays expanded,
    # which is what those older transcripts showed.
    stored = Conversation.from_json(
        [{"kind": "tool", "name": "bash", "lines": lines, "expanded": False}]
    ).tool_blocks()[0]
    assert stored.expanded is False
    missing = Conversation.from_json(
        [{"kind": "tool", "name": "bash", "lines": ["only"]}]
    ).tool_blocks()[0]
    assert missing.expanded is True


def test_new_tool_calls_start_collapsed_and_the_expand_api_still_works():
    conversation = Conversation()
    conversation.feed(ToolCall(names=("one", "two")), now=lambda: 1.0)

    assert [tool.expanded for tool in conversation.tool_blocks()] == [False, False]
    assert conversation.expand_tools() == 2
    assert conversation.collapse_tools() == 2
    assert conversation.toggle_last_tool().name == "two"
    assert conversation.last_tool().expanded is True


def test_prerendered_welcome_panel_is_not_stored_as_assistant_text():
    panel = welcome_panel(width=80)
    conversation = Conversation()
    conversation.add(AssistantBlock(text=panel))
    assert conversation.blocks == []
    assert Conversation.from_json(
        [{"kind": "assistant", "text": panel}]
    ).blocks == []
    assert Conversation([AssistantBlock(text=panel)]).blocks == []

    conversation.add(AssistantBlock(text="welcome panel"))
    conversation.add(AssistantBlock(text=help_panel("Commands\n  /help", width=40)))
    assert [block.text for block in conversation.blocks if isinstance(block, AssistantBlock)]
    assert all("Conductor Super-agent TUI" not in block.text for block in conversation.blocks)


def test_result_body_is_not_repeated_and_summary_loses_when_both_exist():
    block = EndBlock(
        status="completed",
        summary="short summary",
        body="full report\nsecond line",
    )
    rows = block.render_plain()
    joined = "\n".join(rows)

    assert rows[0] == "status: completed"
    assert joined.count("full report") == 1
    assert joined.count("second line") == 1
    assert "short summary" not in joined
    assert "body:" not in joined

    formatted = "\n".join(
        text for _style, text in Conversation([block]).formatted_lines()
    )
    assert formatted.count("full report") == 1
    assert formatted.count("second line") == 1
    assert "summary: short summary" not in formatted
    assert "short summary" not in formatted

    same = EndBlock(status="completed", summary="same text", body="same text")
    same_rows = same.render_plain()
    assert "\n".join(same_rows).count("same text") == 1


def test_completed_plain_rows_strip_protocol_trailers(monkeypatch):
    """Completed plain rows call tui_render and do not print the body twice."""
    import sys
    from types import ModuleType

    calls: list[tuple[str, str]] = []
    module = ModuleType("ptc_cli.superagent.tui_render")

    def strip_protocol_trailers(text: str) -> str:
        calls.append(("strip", text))
        kept = [
            line
            for line in str(text).splitlines()
            if not line.strip().startswith("AUTONOMY_")
        ]
        return "\n".join(kept)

    def render_plain(text: str, width: int = 100) -> str:
        del width
        calls.append(("render", text))
        return text

    module.strip_protocol_trailers = strip_protocol_trailers
    module.render_plain = render_plain
    monkeypatch.setitem(sys.modules, "ptc_cli.superagent.tui_render", module)

    block = EndBlock(
        status="completed",
        summary="short summary",
        body="full report\nAUTONOMY_STATE: secret",
    )
    joined = "\n".join(block.render_plain())

    assert [name for name, _text in calls] == ["strip", "render"]
    assert "AUTONOMY_STATE" not in calls[1][1]
    assert calls[1][1] == "full report"
    assert joined.count("full report") == 1
    assert "short summary" not in joined
    assert "AUTONOMY_STATE" not in joined
    assert "secret" not in joined


def test_completed_plain_rows_drop_autonomy_trailers_without_repeating():
    block = EndBlock(
        status="completed",
        summary="short summary",
        body="full report\nAUTONOMY_STATE: secret\nsecond line",
    )
    joined = "\n".join(block.render_plain())

    assert joined.count("full report") == 1
    assert joined.count("second line") == 1
    assert "short summary" not in joined
    assert "AUTONOMY_STATE" not in joined
    assert "secret" not in joined


def test_phase_visibility_without_a_body_is_unchanged():
    failed = "\n".join(
        EndBlock(
            status="failed",
            summary="boom-summary",
            question="hidden-question",
            error="boom-error",
        ).render_plain()
    )
    assert "boom-summary" in failed
    assert "error: boom-error" in failed
    assert "hidden-question" not in failed

    waiting = EndBlock(
        status="waiting",
        summary="NONTERMINAL-SUMMARY",
        question="approve?",
        error="hidden-error",
    )
    plain = "\n".join(waiting.render_plain())
    assert "NONTERMINAL-SUMMARY" not in plain
    assert "needs you: approve?" in plain
    assert "hidden-error" not in plain

    done = EndBlock(status="completed", summary="done-summary")
    assert "done-summary" in "\n".join(done.render_plain())
    formatted = "\n".join(
        text for _style, text in Conversation([done]).formatted_lines()
    )
    assert "summary: done-summary" in formatted
