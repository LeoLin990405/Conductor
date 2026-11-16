"""The S10 keymap, palette mount and status/composer layout contract.

The tests drive the real prompt_toolkit application through a pipe, the way
a terminal would: Shift+Tab is the previous focus stop, Alt+M cycles the
mode (and is inert while a permission waits), Ctrl+K and ``/`` at column 0
open the S04 palette, and Tab/Enter/Esc follow D2/D3. The last block gates
the status bar plus composer frame through the shared layout contract.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.input import DummyInput, create_pipe_input
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len

# tui imports conductor_cli, which lives in the repo src tree; the ptc-cli
# lockfile's conductor-core is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui import (  # noqa: E402
    HELP_TEXT,
    SuperAgentTui,
    align_status_line,
    build_default_style,
    composer_placeholder,
)
from ptc_cli.superagent.tui_home import home_rows  # noqa: E402
from ptc_cli.superagent.tui_permissions import PermissionRequest  # noqa: E402
from ptc_cli.superagent.tui_sessions import SessionStore  # noqa: E402
from ptc_cli.superagent.tui_theme import STATUS_IDS, resolve_theme, status_glyph, status_word  # noqa: E402
from tests.unit_tests.test_superagent_tui_app import FakeClient  # noqa: E402
from tests.unit_tests.tui_layout_contract import check_layout  # noqa: E402

WIDTHS = (40, 80, 120)
LOCALES = ("en", "zh-CN")

TICKETS = [
    {
        "ticket_id": f"0000000{index}-0000-4000-8000-00000000000{index}",
        "status": state,
        "title": f"Ticket {index} on the terminal-native surface",
        "worker_model": "kimi" if index % 2 else "deepseek",
        "elapsed_seconds": 12 * index,
        "updated_at": f"2026-05-20T09:1{index}:00Z",
    }
    for index, state in enumerate(STATUS_IDS, 1)
]


def _tui(tmp_path: Path) -> SuperAgentTui:
    return SuperAgentTui(
        server_url="http://test",
        user_id="u1",
        client=FakeClient(),
        session_store=SessionStore(tmp_path / "sessions"),
        history_path=tmp_path / "history",
        local_root=tmp_path,
        worktree_root=tmp_path,
        no_splash=True,
    )


async def _start(tui: SuperAgentTui, pipe):
    app = tui.build_application(input=pipe, output=DummyOutput())
    task = asyncio.create_task(app.run_async())
    await asyncio.sleep(0.05)
    return app, task


async def _stop(pipe, task) -> None:
    pipe.send_text("\x03")
    await asyncio.sleep(0.02)
    pipe.send_text("\x03")
    assert await asyncio.wait_for(task, timeout=5) == 0


def _footer(tui: SuperAgentTui, width: int) -> str:
    tui._columns = lambda: width  # type: ignore[method-assign]
    counts, hints = tui._status_parts()
    return align_status_line(counts, hints, width)


# ---------------------------------------------------------------------------
# focus and mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shift_tab_moves_to_the_previous_focus_stop(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        assert app.layout.has_focus(tui.input_area)

        pipe.send_text("\x1b[Z")
        await asyncio.sleep(0.2)
        assert app.layout.has_focus(tui.conversation_window)
        assert tui._focus_target == "main"

        pipe.send_text("\x1b[Z")
        await asyncio.sleep(0.2)
        assert app.layout.has_focus(tui.input_area)
        assert tui._focus_target == "composer"
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_shift_tab_moves_focus_while_a_permission_is_pending(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    waiting = asyncio.create_task(
        tui.request_permission(
            PermissionRequest(action="local-shell", label="run shell", detail="echo hi")
        )
    )
    await asyncio.sleep(0)
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        assert app.layout.has_focus(tui.input_area)

        pipe.send_text("\x1b[Z")
        await asyncio.sleep(0.2)
        assert app.layout.has_focus(tui.conversation_window)

        pipe.send_text("\x1b[Z")
        await asyncio.sleep(0.2)
        assert app.layout.has_focus(tui.input_area)

        pipe.send_text("d")
        await asyncio.sleep(0.1)
        assert await waiting == "deny"
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_pending_permission_suppresses_newline_enter_and_arrows(
    tmp_path: Path,
) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    waiting = asyncio.create_task(
        tui.request_permission(
            PermissionRequest(action="local-shell", label="run shell", detail="echo hi")
        )
    )
    await asyncio.sleep(0)
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        assert tui._pending is not None
        assert app.layout.has_focus(tui.input_area)

        # Plain typing and every newline route are off behind the prompt.
        for keys in ("x", "\x0a", "\r", "\x1b[13;2u", "\x1b[27;2;13~"):
            pipe.send_text(keys)
            await asyncio.sleep(0.08)
        pipe.send_text("\x1b[A")
        await asyncio.sleep(0.08)
        pipe.send_text("\x1b[B")
        await asyncio.sleep(0.08)
        assert tui.input_area.buffer.text == ""

        # The answers still work while the draft cannot move.
        pipe.send_text("d")
        await asyncio.sleep(0.1)
        assert await waiting == "deny"
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_alt_m_cycles_the_mode_and_is_suppressed_while_pending(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    waiting = asyncio.create_task(
        tui.request_permission(
            PermissionRequest(action="local-shell", label="run shell", detail="echo hi")
        )
    )
    await asyncio.sleep(0)
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        assert tui.interaction_mode == "auto"
        pipe.send_text("\x1bm")
        await asyncio.sleep(0.25)
        assert tui.interaction_mode == "auto"
        pipe.send_text("d")
        await asyncio.sleep(0.1)
        assert await waiting == "deny"
        pipe.send_text("\x1bm")
        await asyncio.sleep(0.25)
        assert tui.interaction_mode == "dispatch"
        pipe.send_text("\x1bm")
        await asyncio.sleep(0.25)
        assert tui.interaction_mode == "read-only"
        pipe.send_text("\x1bm")
        await asyncio.sleep(0.25)
        assert tui.interaction_mode == "auto"
        await _stop(pipe, task)
    await tui.close()


# ---------------------------------------------------------------------------
# the palette
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_k_opens_the_palette_and_tab_accepts(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        pipe.send_text("\x0b")
        await asyncio.sleep(0.15)
        assert tui._palette_open
        assert tui._palette_query == ""
        assert app.layout.has_focus(tui.palette_window)

        pipe.send_text("dsp")
        await asyncio.sleep(0.1)
        state = tui._palette_state()
        assert state.rows[0].name == "dispatch"

        pipe.send_text("\t")
        await asyncio.sleep(0.1)
        assert tui._palette_query == "dispatch"
        assert tui._palette_open

        # D3: Shift+Tab is inert inside the trap.
        pipe.send_text("\x1b[Z")
        await asyncio.sleep(0.2)
        assert tui._palette_open
        assert app.layout.has_focus(tui.palette_window)

        pipe.send_text("\x1b")
        await asyncio.sleep(0.2)
        assert not tui._palette_open
        assert app.layout.has_focus(tui.input_area)
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_escape_returns_to_the_focus_stop_that_opened_the_palette(
    tmp_path: Path,
) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        pipe.send_text("\x1b[Z")
        await asyncio.sleep(0.2)
        assert app.layout.has_focus(tui.conversation_window)

        pipe.send_text("\x0b")
        await asyncio.sleep(0.15)
        assert tui._palette_open
        assert app.layout.has_focus(tui.palette_window)

        pipe.send_text("\x1b")
        await asyncio.sleep(0.2)
        assert not tui._palette_open
        assert app.layout.has_focus(tui.conversation_window)
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_slash_at_column_zero_opens_the_palette_and_elsewhere_inserts(
    tmp_path: Path,
) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        pipe.send_text("/")
        await asyncio.sleep(0.15)
        assert tui._palette_open
        assert tui._palette_query == "/"
        pipe.send_text("dsp")
        await asyncio.sleep(0.1)
        pipe.send_text("\t")
        await asyncio.sleep(0.1)
        assert tui._palette_query == "/dispatch"
        pipe.send_text("\x1b")
        await asyncio.sleep(0.2)

        pipe.send_text("x/")
        await asyncio.sleep(0.1)
        assert tui.input_area is not None
        assert tui.input_area.buffer.text == "x/"
        assert not tui._palette_open
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_ambiguous_argument_prefix_inserts_nothing(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]
    tui._palette_dispatches = [
        {"id": "7f3a91c2", "state": "awaiting_human", "goal": "ship it"},
        {"id": "7f9e02b1", "state": "awaiting_human", "goal": "pin it"},
    ]
    tui._open_palette("approve 7f")
    state = tui._palette_state()
    assert len(state.rows) == 2
    tui._palette_accept()
    assert tui._palette_query == "approve 7f"
    tui._palette_run_or_insert()
    assert tui._palette_query == "approve 7f"
    assert submitted == []
    await tui.close()


@pytest.mark.asyncio
async def test_argument_accept_keeps_the_slash_command(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    tui._palette_dispatches = [
        {"id": "7f3a91c2", "state": "running", "goal": "ship it"},
    ]
    tui._open_palette("/dispatch 7f3a")
    tui._palette_accept()
    assert tui._palette_query == "/dispatch 7f3a91c2"
    await tui.close()


@pytest.mark.asyncio
async def test_approve_without_an_id_shows_the_hint_and_sends_nothing(
    tmp_path: Path,
) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        pipe.send_text("\x0b")
        await asyncio.sleep(0.15)
        pipe.send_text("approve")
        await asyncio.sleep(0.1)
        pipe.send_text("\r")
        await asyncio.sleep(0.15)
        assert tui._palette_open
        assert "approve needs a dispatch id" in tui._palette_state().note
        assert submitted == []
        pipe.send_text("\x1b")
        await asyncio.sleep(0.2)
        await _stop(pipe, task)
    await tui.close()

    # With one id on offer, Enter inserts it instead of running bare.
    tui = _tui(tmp_path)
    await tui.startup()
    tui._palette_dispatches = [{"id": "7f3a91c2", "state": "APPROVAL", "goal": "ship it"}]
    tui._open_palette("approve ")
    tui._palette_run_or_insert()
    assert tui._palette_query == "approve 7f3a91c2"
    assert tui._palette_open
    await tui.close()


# ---------------------------------------------------------------------------
# the frame contract
# ---------------------------------------------------------------------------


def _screen_rows(tui: SuperAgentTui, app, width: int, height: int) -> list[str]:
    """The cells the real layout paints: transcript, composer and status bar."""
    screen = Screen()
    with set_app(app):
        tui._sync_composer_height()
        app.layout.container.write_to_screen(
            screen,
            MouseHandlers(),
            WritePosition(0, 0, width, height),
            "",
            erase_bg=False,
            z_index=None,
        )
        screen.draw_all_floats()
    return [
        "".join(screen.data_buffer[y][x].char for x in range(width)).rstrip()
        for y in range(height)
    ]


async def _real_frame(tmp_path: Path, width: int, locale: str) -> tuple[list[str], str]:
    """Render the mounted app with /jobs in the transcript; return rows + status."""
    os.environ["CONDUCTOR_LOCALE"] = locale
    tui = _tui(tmp_path)
    tui.client.tickets_payload = {"tickets": TICKETS}
    await tui.startup()
    tui._columns = lambda: width  # type: ignore[method-assign]
    tui._theme = None
    await tui.handle_command("/jobs")
    app = tui.build_application(input=DummyInput(), output=DummyOutput())
    rows = _screen_rows(tui, app, width, 90)
    with set_app(app):
        status = "".join(text for _style, text, *_ in tui._status_fragments())
    await tui.close()
    while rows and not rows[0]:
        rows.pop(0)
    return rows, status


def _placeholder(width: int, locale: str) -> str:
    """The SPEC §2.2 form the composer row must show whole at this width."""
    forms = {
        "en": (
            "Describe a task, / for commands, @ to attach a file",
            "Describe a task, / for commands",
        ),
        "zh-CN": ("描述一个任务，/ 打开命令，@ 附加文件", "描述一个任务，/ 打开命令"),  # noqa: RUF001
    }[locale]
    room = width - 2 - (1 if width > 60 else 0)  # the caret, and the wall
    return forms[0] if cell_len(forms[0]) <= room else forms[1]


@pytest.mark.asyncio
async def test_status_and_composer_frame_hold_the_layout_contract(tmp_path: Path) -> None:
    frames: dict[tuple[int, str], tuple[list[str], str]] = {}
    try:
        for width in WIDTHS:
            for locale in LOCALES:
                frames[width, locale] = await _real_frame(tmp_path, width, locale)
    finally:
        os.environ.pop("CONDUCTOR_LOCALE", None)

    # §1.1 on the real frame, every state with its glyph and its word: the
    # /jobs stack at 40 keeps the pair on its meta line.
    check_layout(
        lambda width, locale: frames[width, locale][0],
        widths=WIDTHS,
        locales=LOCALES,
        keep=("❯",),  # noqa: RUF001
        states=STATUS_IDS,
    )
    for (width, locale), (rows, status) in frames.items():
        label = f"{locale}@{width}"
        # The status bar is the last row, painted whole: its own text is no
        # wider than the terminal, so the screen did not clip it.
        assert status.strip(), label
        assert cell_len(status) <= width, label
        assert rows[-1] == status.rstrip(), label
        mode = "auto" if locale == "en" else "自动"
        assert rows[-1].startswith(mode), label
        # The composer row shows the localized placeholder whole, on one row.
        placeholder = _placeholder(width, locale)
        composer = [row for row in rows if "❯" in row]  # noqa: RUF001
        assert len(composer) == 1, (label, composer)
        assert placeholder in composer[0], (label, composer[0])
        assert cell_len(composer[0]) <= width, label
        if width > 60:
            assert rows[-4].startswith("╭"), label
            assert rows[-3].startswith("│❯"), label  # noqa: RUF001
            assert rows[-3].endswith("│"), label
            assert rows[-2].startswith("╰"), label
        else:
            assert rows[-2].startswith("❯"), label  # noqa: RUF001


def test_composer_placeholder_has_localized_long_and_short_forms() -> None:
    long_en = "Describe a task, / for commands, @ to attach a file"
    short_en = "Describe a task, / for commands"
    assert composer_placeholder(80, "en") == long_en
    assert composer_placeholder(cell_len(long_en), "en") == long_en
    assert composer_placeholder(38, "en") == short_en
    assert composer_placeholder(38, "zh-CN") == "描述一个任务，/ 打开命令，@ 附加文件"  # noqa: RUF001
    assert composer_placeholder(30, "zh-CN") == "描述一个任务，/ 打开命令"  # noqa: RUF001
    cut = composer_placeholder(10, "en")
    assert cut.endswith("…")
    assert cell_len(cut) <= 10
    assert composer_placeholder(0, "en") == ""


def test_forty_columns_split_the_home_glyph_from_its_word(tmp_path: Path) -> None:
    """§3.3: Home at 40 keeps the glyph on the identity line, the word below."""
    theme = resolve_theme({"CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})
    check_layout(
        lambda width, locale: home_rows(TICKETS, 2, theme, locale, width),
        widths=(80, 120),
        locales=LOCALES,
        states=STATUS_IDS,
    )
    for locale in LOCALES:
        rows = home_rows(TICKETS, 2, theme, locale, 40)
        assert all(cell_len(row) <= 40 for row in rows)
        for index, state in enumerate(STATUS_IDS, 1):
            glyph = status_glyph(state)
            word = status_word(state, locale)
            identity = [row for row in rows if f"Ticket {index}" in row]
            assert identity, state
            assert any(glyph in row for row in identity), state
            assert any(word in row for row in rows), state
            assert not any(
                glyph in row and word in row and f"Ticket {index}" in row
                for row in rows
            ), state


def test_eighty_columns_cut_the_title_and_forty_keep_it_whole(tmp_path: Path) -> None:
    title = "Read the adapter registry " * 6
    ticket = {
        "ticket_id": "7f782add",
        "status": "completed",
        "title": title.strip(),
        "worker_model": "kimi",
        "elapsed_seconds": 12,
    }
    theme = resolve_theme({"CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})
    wide = "\n".join(home_rows([ticket], 0, theme, "en", 80))
    narrow = "\n".join(home_rows([ticket], 0, theme, "en", 40))
    assert "…" in wide
    assert title.strip() not in wide
    assert "…" not in narrow
    assert "".join(narrow.split()).find("Readtheadapterregistry") >= 0


def test_help_lists_alt_m_shift_tab_and_ctrl_k() -> None:
    assert "Alt+M" in HELP_TEXT
    assert "cycle mode: auto, dispatch, read-only" in HELP_TEXT
    assert "Shift+Tab" in HELP_TEXT
    assert "previous focus" in HELP_TEXT
    assert "Ctrl+K" in HELP_TEXT
    assert "palette" in HELP_TEXT


def test_footer_hint_is_alt_m_mode_and_the_styles_still_resolve(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    footer = _footer(tui, 80)
    assert "alt+m mode" in footer
    assert "shift+tab mode" not in footer
    assert cell_len(footer) <= 80
    style = build_default_style({"CONDUCTOR_THEME": "dark", "LANG": "en_US.UTF-8"})
    rules = {selector for selector, _value in style.style_rules}
    assert "permission" in rules
    assert "palette-label" in rules


def test_unsupported_notice_has_both_languages() -> None:
    from ptc_cli.superagent.tui import _unsupported

    assert _unsupported("en") == "not supported by the server yet"
    assert _unsupported("zh-CN") == "服务端暂不支持"


# ---------------------------------------------------------------------------
# review round 3: Meta chords, pending suppression, focus, palette aliases
# ---------------------------------------------------------------------------


def _pending(tui: SuperAgentTui) -> asyncio.Task[str]:
    return asyncio.create_task(
        tui.request_permission(
            PermissionRequest(action="local-shell", label="run shell", detail="echo hi")
        )
    )


@pytest.mark.asyncio
async def test_alt_n_and_alt_m_do_not_close_the_palette(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        tui._open_palette()
        pipe.send_text("\x1bn")
        await asyncio.sleep(0.3)
        assert tui._palette_open
        assert tui._palette_nav
        assert tui._palette_query == ""
        assert tui.input_area.buffer.text == ""

        pipe.send_text("\x1bm")
        await asyncio.sleep(0.3)
        assert tui._palette_open
        assert tui.interaction_mode == "dispatch"
        assert tui._palette_query == ""
        assert tui.input_area.buffer.text == ""

        # A bare Escape still closes it once the escape flush passes.
        pipe.send_text("\x1b")
        await asyncio.sleep(0.3)
        assert not tui._palette_open
        assert app.layout.has_focus(tui.input_area)
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_meta_chords_while_pending_answer_nothing_and_escape_denies(
    tmp_path: Path,
) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    waiting = _pending(tui)
    await asyncio.sleep(0)
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        for chord in ("\x1bm", "\x1b\r", "\x1b[13;2u", "\x1b[27;2;13~"):
            pipe.send_text(chord)
            await asyncio.sleep(0.3)
            assert not waiting.done(), chord
            assert tui.interaction_mode == "auto", chord
        assert tui.input_area.buffer.text == ""
        pipe.send_text("\x1b")
        await asyncio.sleep(0.3)
        assert await waiting == "deny"
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_pending_permission_keeps_the_draft_and_the_caret(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        tui.input_area.text = "ab\ncd"
        tui.input_area.buffer.cursor_position = 5
        waiting = _pending(tui)
        await asyncio.sleep(0.05)
        pipe.send_text("\r\x0a\x1b[A")
        await asyncio.sleep(0.3)
        pipe.send_text("\x1b[B")
        await asyncio.sleep(0.2)
        assert tui.input_area.text == "ab\ncd"
        assert tui.input_area.buffer.cursor_position == 5
        pipe.send_text("a")
        await asyncio.sleep(0.1)
        assert await waiting == "allow"
        assert tui.input_area.text == "ab\ncd"
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_enter_on_the_transcript_stop_keeps_the_draft_and_j_k_scroll(
    tmp_path: Path,
) -> None:
    from ptc_cli.superagent.tui_blocks import NoticeBlock

    tui = _tui(tmp_path)
    await tui.startup()
    for index in range(80):
        tui.conversation.add(NoticeBlock(text=f"row {index}"))
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        tui.input_area.text = "unsent draft"
        pipe.send_text("\x1b[Z")
        await asyncio.sleep(0.3)
        assert app.layout.has_focus(tui.conversation_window)
        pipe.send_text("\r")
        await asyncio.sleep(0.2)
        assert submitted == []
        assert tui.input_area.text == "unsent draft"

        assert tui._follow_tail
        pipe.send_text("k")
        await asyncio.sleep(0.2)
        assert not tui._follow_tail
        upper = tui._scroll_offset
        pipe.send_text("k")
        await asyncio.sleep(0.2)
        assert tui._scroll_offset == upper - 1
        pipe.send_text("j")
        await asyncio.sleep(0.2)
        assert tui._scroll_offset == upper
        assert tui.input_area.text == "unsent draft"

        # Back on the composer, Enter sends the draft.
        pipe.send_text("\t")
        await asyncio.sleep(0.2)
        assert app.layout.has_focus(tui.input_area)
        pipe.send_text("\r")
        await asyncio.sleep(0.2)
        assert submitted == ["unsent draft"]
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "typed",
    ["/jobs --attention", "/jobs watch 7f3a91c2", "/attach 123", "/jobs 1", "/j 2"],
)
async def test_palette_enter_keeps_the_typed_slash_command(
    tmp_path: Path, typed: str
) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        pipe.send_text("/")
        await asyncio.sleep(0.15)
        assert tui._palette_open
        pipe.send_text(typed[1:])
        await asyncio.sleep(0.15)
        assert tui._palette_query == typed
        pipe.send_text("\r")
        await asyncio.sleep(0.15)
        assert submitted == [typed]
        assert not tui._palette_open
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_palette_runs_canonical_names_as_their_slash_command(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]
    tui._open_palette("tickets --attention")
    tui._palette_run_or_insert()
    tui._open_palette("attach 7f3a91c2")
    tui._palette_run_or_insert()
    assert submitted == ["/jobs --attention", "/attach 7f3a91c2"]
    await tui.close()


@pytest.mark.asyncio
async def test_unique_prefix_enter_completes_the_id_before_running(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]
    tui._palette_dispatches = [
        {"id": "7f3a91c2", "state": "APPROVAL", "goal": "ship it"},
        {"id": "7f9e02b1", "state": "APPROVAL", "goal": "pin it"},
        {"id": "2c81e0d4", "state": "running", "goal": "busy"},
    ]
    tui._open_palette("approve 7f3")
    tui._palette_run_or_insert()
    assert tui._palette_query == "approve 7f3a91c2"
    assert submitted == []
    tui._palette_run_or_insert()
    assert submitted == ["/approve 7f3a91c2"]

    # A dispatch that is not awaiting a person is never approved.
    tui._open_palette("approve 2c81e0d4")
    tui._palette_run_or_insert()
    assert submitted == ["/approve 7f3a91c2"]
    assert tui._palette_open
    assert "approve needs a dispatch id" in tui._palette_state().note
    await tui.close()


@pytest.mark.asyncio
async def test_attention_numbers_resolve_to_the_rows_it_shows(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    tui.client.tickets_payload = {
        "tickets": [
            {"ticket_id": "aaaaaaaa", "status": "running", "title": "running"},
            {"ticket_id": "bbbbbbbb", "status": "needs-you", "title": "question"},
        ]
    }
    await tui.handle_command("/jobs --attention")
    shown = tui.conversation.blocks[-1].text
    assert "bbbbbbbb" in shown
    assert "aaaaaaaa" not in shown
    assert tui._resolve_target("1") == "bbbbbbbb"
    await tui.handle_command("/jobs")
    assert tui._resolve_target("1") == "aaaaaaaa"
    await tui.close()


@pytest.mark.asyncio
async def test_review_status_reads_the_server_queue_with_its_line_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    import ptc_cli.superagent.tui as tui_module

    tui = _tui(tmp_path)
    await tui.startup()
    calls: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> str:
        calls.append(list(args))
        return json.dumps(
            {
                "reviews": [
                    {
                        "id": "r1",
                        "line": "line-7",
                        "reviewer": "line-2",
                        "provider": "codex",
                        "state": "queued",
                        "verdict": None,
                    }
                ]
            }
        )

    monkeypatch.setattr(tui_module, "run_conductor", fake_run)
    tui.permissions.allow_session("local-review")
    await tui.handle_command("/review status line-7")
    assert calls == [["review", "status", "line-7"]]
    text = tui.conversation.blocks[-1].text
    assert "review queue" in text
    assert "pending" in text
    assert "no cross-review is queued" not in text
    assert tui._reviews
    assert tui._reviews[0]["line"] == "line-7"

    # Output that is not the queue payload is shown as text, as before.
    monkeypatch.setattr(tui_module, "run_conductor", lambda *_a, **_k: "queued review line-7")
    await tui.handle_command("/review status line-7")
    assert "queued review line-7" in "\n".join(tui.conversation.plain_lines())
    await tui.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "no_jobs", "denied", "again", "compacted"),
    [
        ("en", "no Jobs", "denied: run shell", "press Esc again to interrupt", "compacted 0 block(s)"),
        ("zh-CN", "没有 Job", "已拒绝：run shell", "再按一次 Esc 中断", "已压缩 0 个块"),  # noqa: RUF001
    ],
)
async def test_shell_global_notices_speak_both_languages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    locale: str,
    no_jobs: str,
    denied: str,
    again: str,
    compacted: str,
) -> None:
    from ptc_cli.superagent.tui import _notice

    monkeypatch.setenv("CONDUCTOR_LOCALE", locale)
    assert _notice("noJobs") == no_jobs
    tui = _tui(tmp_path)
    await tui.startup()
    await tui.handle_command("/compact")
    assert tui.conversation.blocks[-1].text == compacted
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        waiting = asyncio.create_task(
            tui._gated_local("local-shell", "run shell", "echo hi")
        )
        await asyncio.sleep(0.05)
        pipe.send_text("d")
        await asyncio.sleep(0.1)
        assert await waiting is False
        assert any(getattr(block, "text", "") == denied for block in tui.conversation.blocks)

        async def watch() -> None:
            await asyncio.sleep(30)

        tui._watch_task = asyncio.create_task(watch())
        pipe.send_text("\x1b")
        await asyncio.sleep(0.3)
        assert any(getattr(block, "text", "") == again for block in tui.conversation.blocks)
        tui._watch_task.cancel()
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_tab_completes_an_at_path_and_keeps_the_composer(tmp_path: Path) -> None:
    (tmp_path / "notes-unique.md").write_text("hi")
    tui = _tui(tmp_path)
    await tui.startup()
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        pipe.send_text("read @notes-u")
        await asyncio.sleep(0.1)
        pipe.send_text("\t")
        await asyncio.sleep(0.3)
        assert tui.input_area.buffer.text == "read @notes-unique.md"
        assert app.layout.has_focus(tui.input_area)
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_tab_without_a_completion_moves_to_the_next_stop(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        pipe.send_text("hello")
        await asyncio.sleep(0.1)
        pipe.send_text("\t")
        await asyncio.sleep(0.3)
        assert app.layout.has_focus(tui.conversation_window)
        assert tui.input_area.buffer.text == "hello"
        pipe.send_text("\t")
        await asyncio.sleep(0.3)
        assert app.layout.has_focus(tui.input_area)
        await _stop(pipe, task)
    await tui.close()


@pytest.mark.asyncio
async def test_palette_ignores_esc_enter_and_keeps_a_pasted_first_line(
    tmp_path: Path,
) -> None:
    tui = _tui(tmp_path)
    await tui.startup()
    submitted: list[str] = []
    tui.spawn_submit = submitted.append  # type: ignore[method-assign]
    with create_pipe_input() as pipe:
        app, task = await _start(tui, pipe)
        tui.input_area.text = "draft"
        tui._open_palette()
        pipe.send_text("\x1b\r")
        await asyncio.sleep(0.3)
        assert tui._palette_open
        assert tui.input_area.text == "draft"
        assert submitted == []

        pipe.send_text("\x1b[200~jobs\nsecond line\x1b[201~")
        await asyncio.sleep(0.3)
        assert tui._palette_query == "jobs"
        assert tui.input_area.text == "draft"
        await _stop(pipe, task)
    await tui.close()
