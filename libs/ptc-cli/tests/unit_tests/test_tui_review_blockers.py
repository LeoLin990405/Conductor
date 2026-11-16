"""Regressions for the Codex BLOCK on feat/tui-redesign (B1–B11)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys
from prompt_toolkit.output import DummyOutput

# tui imports conductor_cli, which lives in the repo src tree; the ptc-cli
# lockfile's conductor-core is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.client import SuperAgentError  # noqa: E402
from ptc_cli.superagent.tui import (  # noqa: E402
    HELP_TEXT,
    ConversationControl,
    SuperAgentTui,
    row_text,
)
from ptc_cli.superagent.tui_blocks import (  # noqa: E402
    AssistantBlock,
    Conversation,
    EndBlock,
    ProgressBlock,
    ToolBlock,
)
from ptc_cli.superagent.tui_render import render_markdown, render_plain, wrap_words  # noqa: E402
from ptc_cli.superagent.tui_sessions import SessionStore  # noqa: E402

_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def close(self) -> None:
        return None

    async def reply(self, ticket_id: str, text: str) -> dict[str, str]:
        self.calls.append((ticket_id, text))
        return {"ok": "1"}

    async def stop(self, ticket_id: str) -> dict[str, str]:
        self.calls.append(("stop", ticket_id))
        return {"ok": "1"}

    async def overview(self) -> dict[str, object]:
        return {}

    async def daemons(self) -> dict[str, object]:
        return {"daemons": []}


def _tui(tmp_path: Path, client: _Client | None = None) -> SuperAgentTui:
    return SuperAgentTui(
        server_url="http://test",
        user_id="u1",
        client=client or _Client(),
        session_store=SessionStore(tmp_path / "sessions"),
        history_path=tmp_path / "history",
        local_root=tmp_path,
        worktree_root=tmp_path,
        no_splash=True,
    )


def _screen(tui: SuperAgentTui, width: int = 80) -> str:
    tui._columns = lambda: width  # type: ignore[method-assign]
    control = ConversationControl(lambda: (tui.present_transcript(), 0))
    content = control.create_content(width, 24)
    lines: list[str] = []
    for index in range(content.line_count):
        lines.append(
            "".join(
                piece
                for style, piece in content.get_line(index)
                if style != "[SetCursorPosition]"
            )
        )
    return "\n".join(lines)


def _press(tui: SuperAgentTui, *keys: Keys) -> None:
    app = tui._app or tui.build_application(output=DummyOutput())
    processor = app.key_processor
    for key in keys:
        processor.feed(KeyPress(key))
        processor.process_keys()


def test_explicit_reply_targets_the_other_ticket(tmp_path: Path) -> None:
    client = _Client()
    tui = _tui(tmp_path, client)
    watching = ProgressBlock(ticket_id=_A, phase="running")
    tui._progress_block = watching
    tui.conversation.blocks.append(watching)
    tui._timeout = 0

    asyncio.run(tui._reply_command(f"{_B} answer"))

    assert client.calls == [(_B, "answer")]
    assert _B not in client.calls[0][1]

    tui._jobs = [
        {"ticket_id": "aaaaaaaa-1111-1111-1111-111111111111"},
        {"ticket_id": "aaaaaaaa-2222-2222-2222-222222222222"},
    ]
    client.calls.clear()
    with pytest.raises(SuperAgentError, match="more than one"):
        asyncio.run(tui._reply_command("aaaaaaaa answer"))
    assert client.calls == []


def test_home_rows_align_worker_and_duration(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui._recent_tickets = [
        {
            "ticket_id": _A,
            "status": "completed",
            "title": "A",
            "worker_model": "kimi",
            "elapsed_seconds": 12,
            "updated_at": "2026-11-08T01:02:00",
        },
        {
            "ticket_id": _B,
            "status": "waiting",
            "title": "Need a much longer host name",
            "worker_model": "deepseek",
            "elapsed_seconds": 7552.5,
            "updated_at": "2026-11-08T03:04:00",
        },
    ]
    rows = tui._home_rows()
    lines = [row_text(row) for row in rows]
    first = next(line for line in lines if "kimi" in line)
    second = next(line for line in lines if "deepseek" in line)

    assert first.index("kimi") == second.index("deepseek")
    assert "12s" in first
    assert "2h05m" in second
    assert "01:02" in first
    assert "/jobs 1" in first
    assert "03:04" in second
    assert "/reply 2" in second


def test_attention_count_uses_hold_colour(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui._columns = lambda: 80  # type: ignore[method-assign]
    tui._online_workers = 1
    tui.status.stream = "live"
    tui.status.phase = "idle"
    tui.status.attention = 4
    live = list(tui._status_fragments())
    assert any(
        style == "class:status-hold" and any(char.isdigit() for char in text)
        for style, text in live
    )
    tui.status.attention = 0
    idle = "".join(text for _style, text in tui._status_fragments())
    assert "◆" not in idle
    assert "needing you" not in idle
    # The ruled hints keep their order. "? help" is idle-only and drops first.
    assert idle.index("shift+enter newline") < idle.index("alt+m mode")
    assert idle.rstrip().endswith("alt+m mode · ? help")


def test_footer_presence_uses_hold_while_reconnecting(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui._columns = lambda: 80  # type: ignore[method-assign]
    tui.status.stream = "live"
    live = list(tui._status_fragments())
    assert any("online" in text for _style, text in live)
    assert all(style != "class:status-hold" for style, _text in live)

    tui.status.stream = "reconnecting"
    held = list(tui._status_fragments())
    assert ("class:status-hold", "reconnecting") in held
    assert not any("online" in text for _style, text in held)

    tui.status.stream = "live"
    restored = "".join(text for _style, text in tui._status_fragments())
    assert "online" in restored
    assert "reconnecting" not in restored


def test_code_literals_survive_markdown() -> None:
    source = "```python\nx = 2 ** 3\n```\n\n```sh\necho `date`\n```\n"
    rendered = "\n".join(render_plain(source, 40))
    assert "x = 2 ** 3" in rendered
    assert "echo `date`" in rendered
    rows = render_markdown(source, 40)
    flat = "\n".join("".join(text for _style, text in row) for row in rows)
    assert "2 ** 3" in flat
    assert "`date`" in flat


def test_tables_fold_by_default_and_export_keeps_every_row() -> None:
    body = "| n |\n| --- |\n" + "\n".join(f"| {index} |" for index in range(1, 8))
    folded = "\n".join(
        "".join(text for _style, text in row) for row in render_markdown(body, 20)
    )
    full = "\n".join(render_plain(body, 20))
    assert "… 3 more rows" in folded
    assert "7" not in folded.split("…", 1)[0]
    assert "… " not in full
    assert "7" in full


def test_result_sentence_is_painted_once(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    sentence = "The answer is 42."
    progress = ProgressBlock(
        ticket_id=_A,
        phase="completed",
        activity_kind="report",
        activity_summary=sentence,
        summary=sentence,
    )
    end = EndBlock(
        status="completed",
        summary=sentence,
        body=f"{sentence}\n\nAnother paragraph.",
    )
    tui.conversation.blocks.extend([progress, end])
    screen = _screen(tui)
    assert screen.count(sentence) == 1
    assert "activity: report" not in screen
    assert "Another paragraph." in screen


def test_menu_window_follows_the_selection(tmp_path: Path) -> None:
    from prompt_toolkit.input import create_pipe_input

    tui = _tui(tmp_path)
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            app = tui.build_application(input=pipe, output=DummyOutput())
            running = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text("/")
            for _ in range(40):
                if len(tui._menu_items()) >= 9:
                    break
                await asyncio.sleep(0.05)
            pipe.send_text("\x1b[B" * 8)
            for _ in range(40):
                if tui._menu_index >= 8:
                    break
                await asyncio.sleep(0.05)
            items = tui._menu_items()
            assert len(items) >= 9
            ninth = items[tui._menu_index][0]
            shown = "".join(text for _style, text in tui._menu_fragments())
            assert ninth in shown
            assert items[0][0] not in shown
            assert any(style == "class:slash-menu-selected" and ninth in text for style, text in tui._menu_fragments())
            pipe.send_text("\x1b[B" * (len(items) - 1 - tui._menu_index))
            for _ in range(40):
                if tui._menu_index == len(items) - 1:
                    break
                await asyncio.sleep(0.05)
            last = items[-1][0]
            shown = "".join(text for _style, text in tui._menu_fragments())
            assert last in shown
            assert items[0][0] not in shown
            pipe.send_text("\r")
            await asyncio.sleep(0.15)
            assert submitted == [last]
            app.exit(result=0)
            await running

    asyncio.run(scenario())


def test_expand_and_ctrl_o_share_one_flag(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui.show_worker_details = True
    tool = ToolBlock(name="shell", lines=[f"line {index}" for index in range(1, 8)])
    tui.conversation.blocks.append(tool)

    asyncio.run(tui.handle_command("/expand"))
    assert tool.expanded
    assert "line 5" in _screen(tui)

    asyncio.run(tui.handle_command("/collapse"))
    assert not tool.expanded
    assert "line 5" not in _screen(tui)

    tui._toggle_newest_output()
    assert tool.expanded
    assert "line 5" in _screen(tui)
    asyncio.run(tui.handle_command("/collapse"))
    assert not tool.expanded
    assert "line 5" not in _screen(tui)


def test_second_escape_stops_the_ticket(tmp_path: Path) -> None:
    client = _Client()
    tui = _tui(tmp_path, client)
    progress = ProgressBlock(ticket_id=_A, phase="running")
    tui._progress_block = progress
    tui.conversation.blocks.append(progress)

    async def scenario() -> None:
        from prompt_toolkit.input import create_pipe_input

        async def watch() -> None:
            await asyncio.sleep(30)

        tui._watch_task = asyncio.create_task(watch())
        with create_pipe_input() as pipe:
            app = tui.build_application(input=pipe, output=DummyOutput())
            running = asyncio.create_task(app.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text("\x1b")
            for _ in range(40):
                if tui._interrupt_confirm:
                    break
                await asyncio.sleep(0.05)
            assert tui._interrupt_confirm
            assert client.calls == []
            assert tui._watch_task is not None and not tui._watch_task.done()
            pipe.send_text("\x1b")
            await asyncio.sleep(0.3)
            assert ("stop", _A) in client.calls
            app.exit(result=0)
            await running

    asyncio.run(scenario())


def test_escape_while_waiting_does_not_stop(tmp_path: Path) -> None:
    client = _Client()
    tui = _tui(tmp_path, client)
    progress = ProgressBlock(ticket_id=_A, phase="waiting", question="pick")
    tui._progress_block = progress
    tui.status.phase = "waiting"
    tui.conversation.blocks.append(progress)

    async def scenario() -> None:
        async def watch() -> None:
            await asyncio.sleep(30)

        tui._watch_task = asyncio.create_task(watch())
        app = tui.build_application(output=DummyOutput())
        # Escape is not eager (it prefixes the Alt chords): a lone Escape
        # resolves on the key processor's timeout, which needs the app.
        with set_app(app):
            _press(tui, Keys.Escape)
            await asyncio.sleep(0.2)

    asyncio.run(scenario())
    assert client.calls == []
    assert any("answer later" in block.text for block in tui.conversation.blocks if hasattr(block, "text"))


def _laid_out_composer(
    tui: SuperAgentTui, rows: int, text: str = "", *, columns: int = 80
) -> tuple[int, int, str, int]:
    import io

    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.output.vt100 import Vt100_Output

    async def render() -> tuple[int, int, str, int]:
        buffer = io.StringIO()
        output = Vt100_Output(
            buffer,
            lambda: Size(rows=rows, columns=columns),
            term="xterm-256color",
            enable_cpr=False,
        )
        app = tui.build_application(output=output, history=InMemoryHistory())
        if tui.input_area is not None:
            tui.input_area.buffer.text = text
            tui.input_area.buffer.cursor_position = len(text)
        with set_app(app):
            app.renderer.render(app, app.layout)
        positions = app.renderer._last_screen.visible_windows_to_write_positions
        assert tui.input_area is not None
        height = positions[tui.input_area.window].height
        borders = sum(
            pos.height
            for window, pos in positions.items()
            if getattr(window, "style", "") == "class:composer-border"
        )
        cursor = tui.input_area.buffer.cursor_position if tui.input_area is not None else 0
        return height, borders, buffer.getvalue(), cursor

    return asyncio.run(render())


def test_empty_composer_is_one_content_row(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    for rows in (24, 42):
        content, borders, _raw, _cursor = _laid_out_composer(tui, rows)
        assert content == 1
        assert borders == 2
        assert content + borders == 3
    content, borders, _raw, _cursor = _laid_out_composer(tui, 24, "one\ntwo")
    assert content == 2
    assert borders == 2


def test_wrap_keeps_words_and_cjk_punctuation() -> None:
    wrapped = wrap_words("abc abcdefg", 8)
    assert "abcdefg" in wrapped
    assert all(len(line) <= 8 for line in wrapped)
    assert wrap_words("ab cd", 20) == ["ab cd"]
    assert wrap_words("1. read the file", 80) == ["1. read the file"]
    wide = wrap_words("a" * 78, 79)
    assert wide == ["a" * 78]
    cjk = wrap_words("汉" * 40 + "，尾", 80)
    for line in cjk:
        assert sum(2 if ord(char) > 127 else 1 for char in line) <= 80
        assert line.lstrip(" ")[:1] not in set("，。、；：？！）」』")
    narrow = wrap_words("读取文件，然后列出名字", 8, hanging=0)
    for line in narrow:
        assert sum(2 if ord(char) > 127 else 1 for char in line) <= 8
        assert not line.startswith(tuple("，。、；：？！）」』"))


def test_progress_steps_reach_the_screen_and_the_session(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    progress = ProgressBlock(ticket_id=_A, phase="running")
    end = EndBlock(status="completed", summary="done")
    tui._progress_block = progress
    tui.conversation.blocks.extend([progress, end])
    tui._apply_progress_payload(
        {
            "phase": "completed",
            "ticket_id": _A,
            "steps": [{"title": "read the file"}, {"title": "write the note"}],
        }
    )
    screen = _screen(tui)
    assert "○ read the file" in screen
    assert "○ write the note" in screen
    restored = Conversation.from_json(tui.conversation.to_json())
    saved_steps = [
        getattr(block, "steps", [])
        for block in restored.blocks
        if getattr(block, "steps", [])
    ]
    assert saved_steps
    assert saved_steps[0][0]["title"] == "read the file"


def test_help_is_not_part_of_the_token_estimate(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    assert tui.conversation.token_estimate() == 0
    asyncio.run(tui.handle_command("/help"))
    assert tui.conversation.token_estimate() == 0
    restored = Conversation.from_json(tui.conversation.to_json())
    assert restored.token_estimate() == 0
    tui.conversation.add(AssistantBlock(text=HELP_TEXT))
    assert tui.conversation.token_estimate() > 0


def test_card_shows_worker_branch_and_activity_without_worker_tokens(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui._columns = lambda: 80  # type: ignore[method-assign]
    tui.show_worker_details = False
    tui.status.worker = "kimi"
    tui.status.branch = "feat/tui"
    tui.status.ticket_short = _A[:8]
    progress = ProgressBlock(ticket_id=_A, phase="running")
    progress.activity_kind = "route"
    progress.activity_summary = "Routed to kimi (branch feat/tui)"
    progress.tool_line = "read_file libs/ptc-cli/registry.py"
    tui._progress_block = progress
    hidden = ToolBlock(
        name="read_secret",
        lines=["worker-token"],
        worker_detail=True,
        expanded=True,
    )
    tui.conversation.blocks.extend([progress, hidden])
    screen = _screen(tui)
    assert "kimi" in screen
    assert "feat/tui" in screen
    assert "♩ Routed to kimi" in screen
    assert "▸ read_file" in screen
    assert "╰ libs/ptc-cli/registry.py" in screen
    assert "read_secret" not in screen
    assert "worker-token" not in screen
    assert "phase:" not in screen
    assert "status:" not in screen


def test_b12_reused_tool_identity_does_not_keep_the_old_duration(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui.show_worker_details = True
    tui._columns = lambda: 80  # type: ignore[method-assign]
    first = ToolBlock(name="read", status="done", duration=1.0, lines=["out"])
    second = ToolBlock(name="read", status="done", duration=9.0, lines=["out"])
    key_first = tui._row_cache_key(first)
    key_second = tui._row_cache_key(second)
    assert key_first is not None and key_second is not None
    assert key_first[1:] != key_second[1:]
    tui.conversation.blocks.append(first)
    assert "done 1.0s" in _screen(tui)
    assert key_first in tui._row_cache
    # A collected block can be reallocated at the same id. Plant the old
    # rendered rows under that id with the previous content key.
    tui._row_cache[(id(second), *key_first[1:])] = tui._row_cache[key_first]
    tui.conversation.blocks[:] = [second]
    screen = _screen(tui)
    assert "done 9.0s" in screen
    assert "done 1.0s" not in screen


def test_finished_cards_do_not_share_the_latest_identity(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    first = EndBlock(status="completed", summary="alpha done", ticket_title="Alpha")
    first.worker = "kimi"
    first.branch = "feat/a"
    first.ticket_short = _A[:8]
    second = EndBlock(status="completed", summary="beta done", ticket_title="Beta")
    second.worker = "grok"
    second.branch = "feat/b"
    second.ticket_short = _B[:8]
    tui.status.ticket_title = "Beta"
    tui.status.worker = "grok"
    tui.status.branch = "feat/b"
    tui.status.ticket_short = _B[:8]
    tui.conversation.blocks.extend([first, second])
    screen = _screen(tui)
    assert "Alpha" in screen and "feat/a" in screen and "kimi" in screen
    assert "Beta" in screen and "feat/b" in screen
    assert screen.count("Beta") == 1
    assert screen.count("feat/b") == 1


def test_reply_number_follows_the_home_list_not_old_jobs(tmp_path: Path) -> None:
    client = _Client()
    tui = _tui(tmp_path, client)
    tui._recent_tickets = [
        {"ticket_id": _A, "title": "A"},
        {"ticket_id": _B, "title": "B"},
        {"ticket_id": "cccccccc-cccc-cccc-cccc-cccccccccccc", "title": "C"},
    ]
    tui._jobs = [
        {"ticket_id": "cccccccc-cccc-cccc-cccc-cccccccccccc"},
        {"ticket_id": _A},
        {"ticket_id": _B},
    ]
    tui._timeout = 0
    tui._home_rows()
    asyncio.run(tui._reply_command("2 answer"))
    assert client.calls == [(_B, "answer")]


def test_fresh_reply_number_uses_the_home_list_on_screen(tmp_path: Path) -> None:
    client = _Client()
    tui = _tui(tmp_path, client)
    tui._recent_tickets = [{"ticket_id": _A}, {"ticket_id": _B}]
    tui._timeout = 0
    asyncio.run(tui._reply_command("2 answer"))
    assert client.calls == [(_B, "answer")]


def test_reply_hash_and_words(tmp_path: Path) -> None:
    client = _Client()
    tui = _tui(tmp_path, client)
    watching = ProgressBlock(ticket_id=_A, phase="waiting")
    tui._progress_block = watching
    tui.status.phase = "waiting"
    tui._timeout = 0
    tui._write_list_snapshot([{"ticket_id": _A}, {"ticket_id": _B}])
    asyncio.run(tui._reply_command("re-run the check"))
    asyncio.run(tui._reply_command("read-only please"))
    asyncio.run(tui._reply_command("#2 yes"))
    assert client.calls == [
        (_A, "re-run the check"),
        (_A, "read-only please"),
        (_B, "yes"),
    ]


def test_other_ticket_payload_does_not_relabel_the_open_card(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    block = ProgressBlock(
        ticket_id=_A, phase="running", ticket_title="Alpha", worker="kimi"
    )
    tui._progress_block = block
    tui._apply_progress_payload(
        {"ticket_id": _B, "title": "Beta", "worker_model": "grok", "summary": "nope"}
    )
    assert block.ticket_title == "Alpha"
    assert block.worker == "kimi"
    assert block.summary == ""


def test_finished_body_is_shown_once(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui.conversation.blocks.append(
        EndBlock(
            status="completed",
            summary="First note.\nThe answer is 42.",
            body="The answer is 42.\nMore detail.",
            ticket_title="Done card",
        )
    )
    screen = _screen(tui)
    assert screen.count("The answer is 42") == 1
    assert "More detail." in screen
    assert "First note." not in screen


def _layout_text(container: object) -> str:
    chunks: list[str] = []
    content = getattr(container, "content", None)
    text_source = getattr(content, "text", None) if content is not None else None
    if callable(text_source):
        value = text_source()
        if isinstance(value, str):
            chunks.append(value)
        else:
            chunks.append("".join(str(piece[1]) for piece in value))
    else:
        children = getattr(container, "children", None)
        if children:
            for child in children:
                chunks.append(_layout_text(child))
        elif content is not None:
            chunks.append(_layout_text(content))
    return "\n".join(chunk for chunk in chunks if chunk)


def test_footer_hints_follow_the_phase_and_drop_stop_watch(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui._columns = lambda: 100
    tui.status.phase = "running"
    running = "".join(text for _style, text in tui._status_fragments())
    assert "esc interrupt" in running
    assert "esc later" not in running
    tui.status.phase = "waiting"
    tui._progress_block = ProgressBlock(ticket_id=_A, phase="waiting")
    waiting = "".join(text for _style, text in tui._status_fragments())
    assert "esc later" in waiting
    app = tui.build_application(output=DummyOutput())
    painted = _layout_text(app.layout.container)
    assert "Esc stop watch" not in painted
    assert "Enter send" not in painted


def test_composer_grows_by_visual_rows(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui._columns = lambda: 80
    tui.build_application(output=DummyOutput())
    assert tui._composer_visual_rows("") == 1
    assert tui._composer_visual_rows("one\ntwo") == 2
    assert tui._composer_visual_rows("X" * 240) > 1
    assert tui._composer_visual_rows("X" * 5000) == 8


def test_home_redraw_keeps_the_jobs_snapshot(tmp_path: Path) -> None:
    client = _Client()
    tui = _tui(tmp_path, client)
    tui._jobs = [{"ticket_id": _B, "title": "B"}, {"ticket_id": _A, "title": "A"}]
    tui._write_list_snapshot(tui._jobs)
    tui._recent_tickets = [{"ticket_id": _A, "title": "A"}, {"ticket_id": _B, "title": "B"}]
    tui._timeout = 0
    tui._home_rows()
    assert tui._snapshot_ids() == [_B, _A]
    asyncio.run(tui._reply_command("2 answer"))
    assert client.calls == [(_A, "answer")]


def test_out_of_range_digits_match_a_unique_prefix(tmp_path: Path) -> None:
    client = _Client()
    tui = _tui(tmp_path, client)
    ticket = "12345678-abcd-4abc-8abc-abcdabcdabcd"
    tui._jobs = [{"ticket_id": ticket}]
    tui._progress_block = ProgressBlock(ticket_id=_A, phase="waiting")
    tui.status.phase = "waiting"
    tui._list_snapshot = {1: {"ticket_id": _A, "shown_at": 1.0}}
    tui._timeout = 0
    asyncio.run(tui._reply_command("12345678 answer"))
    assert client.calls == [(ticket, "answer")]


def test_attach_is_jobs_watch(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    seen: list[str] = []

    async def record(rest: str) -> None:
        seen.append(rest)

    tui._jobs_command = record  # type: ignore[method-assign]
    asyncio.run(tui.handle_command("/attach 2"))
    assert seen == ["watch 2"]


def test_compare_result_is_not_a_placeholder(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    _style, text = tui._card_title(
        "completed",
        elapsed="",
        title="Compare result",
        short="abcd1234",
    )
    assert "Compare result" in text
    assert "abcd1234" in text
    assert "Done" not in text


def test_shared_short_id_stays_unbound(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    short = _A[:8]
    restored = Conversation.from_json(
        [
            {"kind": "progress", "ticket_id": _A, "phase": "completed"},
            {"kind": "end", "status": "completed", "summary": "one", "ticket_short": short},
            {"kind": "end", "status": "completed", "summary": "two", "ticket_short": short},
        ]
    )
    ends = [block for block in restored.blocks if isinstance(block, EndBlock)]
    assert [block.unbound for block in ends] == [True, True]
    assert all(not block.ticket_id for block in ends)
    tui.conversation = restored
    tui._progress_block = next(block for block in restored.blocks if isinstance(block, ProgressBlock))
    tui.status.phase = "completed"
    tui.status.summary = "replaced"
    tui._overwrite_result()
    assert [block.summary for block in ends] == ["one", "two"]


def test_unique_short_id_binds_and_updates_that_card(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    other = EndBlock(status="completed", summary="other", ticket_id=_B, ticket_short=_B[:8])
    restored = Conversation.from_json(
        [
            {"kind": "progress", "ticket_id": _A, "phase": "completed"},
            {"kind": "end", "status": "completed", "summary": "kept", "ticket_short": _A[:8]},
        ]
    )
    restored.blocks.append(other)
    bound = next(block for block in restored.blocks if isinstance(block, EndBlock) and block.summary == "kept")
    assert bound.ticket_id == _A
    assert bound.unbound is False
    tui.conversation = restored
    tui._progress_block = next(block for block in restored.blocks if isinstance(block, ProgressBlock))
    tui.status.phase = "completed"
    tui.status.summary = "replaced"
    tui._overwrite_result()
    assert bound.summary == "replaced"
    assert other.summary == "other"


def test_finished_footer_has_no_interrupt_hint(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui._columns = lambda: 100
    tui.status.phase = "completed"
    finished = "".join(text for _style, text in tui._status_fragments())
    assert "esc interrupt" not in finished
    tui.status.phase = "failed"
    failed = "".join(text for _style, text in tui._status_fragments())
    assert "esc interrupt" not in failed


def test_ordered_list_keeps_its_start_number() -> None:
    assert render_plain("7. Seven\n8. Eight", 40) == ["7. Seven", "8. Eight"]


def test_nested_list_restores_the_outer_marker() -> None:
    assert render_plain("- outer\n  1. first\n  2. second\n- next", 40) == [
        "• outer",
        "  1. first",
        "  2. second",
        "• next",
    ]
    source = "1. one\n   - a\n   - b\n2. two\n   1. x\n   2. y\n3. three"
    assert render_plain(source, 40) == [
        "1. one",
        "  • a",
        "  • b",
        "2. two",
        "  1. x",
        "  2. y",
        "3. three",
    ]


def test_list_spacing_follows_the_source() -> None:
    assert render_plain("Intro.\n\n- a\n- b\n\nAfter.", 40) == [
        "Intro.",
        "",
        "• a",
        "• b",
        "",
        "After.",
    ]
    assert render_plain("1. one\n\n2. two", 40) == ["1. one", "2. two"]
    assert render_plain("1. Run:\n   ```sh\n   make test\n   ```\n2. Check", 40) == [
        "1. Run:",
        "│ make test",
        "2. Check",
    ]
    assert render_plain("- ```\n  code\n  ```\n- after", 40) == ["•", "│ code", "• after"]
    loose_outer = (
        "- outer\n\n  - child one\n  - child two\n\n  ```\n  code\n  ```\n- next"
    )
    assert render_plain(loose_outer, 40) == [
        "• outer",
        "  • child one",
        "  • child two",
        "│ code",
        "• next",
    ]
    fence_first = (
        "- ```\n  code\n  ```\n  - child\n  ```\n  end\n  ```\n- after"
    )
    fence_first_rows = ["•", "│ code", "  • child", "│ end", "• after"]
    for width in (40, 80, 120):
        assert render_plain(fence_first, width) == fence_first_rows
    ordered_fence_first = (
        "1. ```\n   code\n   ```\n   1. child\n   ```\n   end\n   ```\n2. after"
    )
    ordered_rows = ["1.", "│ code", "  1. child", "│ end", "2. after"]
    for width in (40, 80, 120):
        assert render_plain(ordered_fence_first, width) == ordered_rows
    assert render_plain("- a\n  - b\n  - c\n- d", 40) == ["• a", "  • b", "  • c", "• d"]
    assert render_plain("- a\n\n  - b\n\n  - c\n\n- d", 40) == [
        "• a",
        "  • b",
        "  • c",
        "• d",
    ]
    quoted = "> - a\n>\n> - b"
    quoted_rows = ["• a", "• b"]
    for width in (40, 80, 120):
        assert render_plain(quoted, width) == quoted_rows
    quoted_ordered = "> 1. a\n>\n> 2. b"
    quoted_ordered_rows = ["1. a", "2. b"]
    for width in (40, 80, 120):
        assert render_plain(quoted_ordered, width) == quoted_ordered_rows


@pytest.mark.parametrize(
    ("source", "rows"),
    [
        ("1. one\n\n2. two", ["1. one", "2. two"]),
        (
            "- a\n\n  - b\n\n  - c\n\n- d",
            ["• a", "  • b", "  • c", "• d"],
        ),
        (
            "- ```\n  code\n  ```\n  - child\n  ```\n  end\n  ```\n- after",
            ["•", "│ code", "  • child", "│ end", "• after"],
        ),
        (
            "1. ```\n   code\n   ```\n   1. child\n   ```\n   end\n   ```\n2. after",
            ["1.", "│ code", "  1. child", "│ end", "2. after"],
        ),
        (
            "- outer\n\n  - child one\n  - child two\n\n  ```\n  code\n  ```\n- next",
            ["• outer", "  • child one", "  • child two", "│ code", "• next"],
        ),
        ("Before.\n\n> quoted", ["Before.", "", "quoted"]),
        ("# Heading\n\n> quoted", ["Heading", "", "quoted"]),
        ("- first\n\n> quoted", ["• first", "", "quoted"]),
        ("> first\n\n> second", ["first", "", "second"]),
        ("- > quoted\n- next", ["• quoted", "• next"]),
        ("1. > quoted\n2. next", ["1. quoted", "2. next"]),
        (
            "> before\n>\n> | h |\n> | --- |\n> | v |",
            ["before", "", "h", "─", "v"],
        ),
        (
            "> | h |\n> | --- |\n> | v |\n>\n> after",
            ["h", "─", "v", "", "after"],
        ),
        ("> > a\n> >\n> > b", ["a", "", "b"]),
        ("> - a\n>\n> - b", ["• a", "• b"]),
        ("> 1. a\n>\n> 2. b", ["1. a", "2. b"]),
        ("> - a\n> - b", ["• a", "• b"]),
        (
            "> before\n>\n> ```\n> a\n>\n> b\n> ```\n>\n> after",
            ["before", "", "│ a", "│ ", "│ b", "", "after"],
        ),
    ],
)
def test_compact_layout_contract(source: str, rows: list[str]) -> None:
    for width in (40, 80, 120):
        assert render_plain(source, width) == rows


def test_wrapped_list_item_hangs_under_its_text() -> None:
    rows = render_plain("- outer\n  1. " + "nested " * 12 + "\n- next", 40)
    assert rows[0] == "• outer"
    assert rows[1].startswith("  1. nested")
    assert all(row.startswith("     nested") for row in rows[2:-1])
    assert rows[-1] == "• next"


def test_image_alt_text_is_readable(tmp_path: Path) -> None:
    source = "Before ![important alt text](https://invalid.test/img) after"
    assert render_plain(source, 80) == ["Before [image: important alt text] after"]
    assert render_plain("x ![](https://invalid.test/img) y", 80) == ["x [image] y"]
    exported = Conversation([EndBlock(status="completed", body=source)]).plain_lines()
    assert "Before [image: important alt text] after" in exported
    tui = _tui(tmp_path)
    tui._columns = lambda: 80  # type: ignore[method-assign]
    rows = tui._markdown_or_literal(source)
    shown = "\n".join(
        "".join(piece for _style, piece in row) if isinstance(row, list) else str(row[1])
        for row in rows
    )
    assert "[image: important alt text]" in shown
    tui.conversation.add(EndBlock(status="completed", body=source))
    transcript = "".join(str(piece[1]) for piece in tui.present_transcript())
    assert "[image: important alt text]" in transcript


def test_worker_details_have_no_blank_row_after_streamed_text(tmp_path: Path) -> None:
    from ptc_cli.superagent.tui_blocks import ActivityBlock

    tui = _tui(tmp_path)
    tui._columns = lambda: 80  # type: ignore[method-assign]
    tui.show_worker_details = True
    tui.conversation.add(
        AssistantBlock(text="Reading registry.py to list the adapters…\n", worker_detail=True)
    )
    tui.conversation.add(
        ActivityBlock(at="2023-11-14T22:13:05Z", event_kind="tool", summary="read_file registry.py")
    )
    rows = [str(piece[1]) for piece in tui.present_transcript()]
    reading = next(index for index, text in enumerate(rows) if "Reading registry.py" in text)
    assert "read_file registry.py" in rows[reading + 1]


def test_running_footer_at_80_keeps_the_ruled_hint_order(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui._online_workers = 1
    tui.status.stream = "live"
    tui.status.phase = "running"
    tui.status.attention = 2

    def footer(width: int) -> str:
        tui._columns = lambda: width  # type: ignore[method-assign]
        return "".join(text for _style, text in tui._status_fragments())

    ruled = ["◆ 2", "esc interrupt", "shift+enter newline", "alt+m mode"]
    wide = footer(80)
    assert "ctrl+o" not in wide
    assert all(hint in wide for hint in ruled), wide
    positions = [wide.index(hint) for hint in ruled]
    assert positions == sorted(positions), wide
    assert wide.rstrip().endswith("alt+m mode")
    narrow = footer(60)
    assert "alt+m mode" not in narrow
    assert all(hint in narrow for hint in ruled[:3]), narrow
    tighter = footer(50)
    assert "shift+enter newline" not in tighter
    assert all(hint in tighter for hint in ruled[:2]), tighter
    assert "Ctrl+O" in HELP_TEXT


def test_composer_uses_prompt_toolkit_wrap(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    sample = "x" * 77
    ascii_row, _borders, raw, cursor = _laid_out_composer(tui, 24, sample)
    assert ascii_row == 2
    assert cursor == len(sample)
    assert sample[:20] in raw
    assert sample[-20:] in raw
    cjk = "汉" * 39
    cjk_row, _borders, raw, cursor = _laid_out_composer(tui, 24, cjk)
    assert cjk_row == 2
    assert cursor == len(cjk)
    assert cjk[:2] in raw
    assert cjk[-2:] in raw
    graphemes = "e\u0301" * 40
    grapheme_row, _borders, raw, cursor = _laid_out_composer(tui, 24, graphemes)
    assert grapheme_row == 1
    assert cursor == len(graphemes)
    assert "e" in raw
    wide, _borders, _raw, _cursor = _laid_out_composer(tui, 24, sample, columns=120)
    assert wide == 1
