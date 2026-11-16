"""A7, A10, A13, A15, and A18: resize, composer, motion, attention, cache."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.utils import get_cwidth

# tui imports conductor_cli, which lives in the repo src tree; the ptc-cli
# lockfile's conductor-core is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui import (  # noqa: E402
    ConversationControl,
    SuperAgentTui,
    reduced_motion_enabled,
    row_text,
)
from ptc_cli.superagent.tui_blocks import AssistantBlock, ProgressBlock  # noqa: E402
from ptc_cli.superagent.tui_sessions import SessionStore  # noqa: E402

_BOX = "╭╮╰╯│─└┘┌┐├┤┬┴┼"


class _OverviewClient:
    def __init__(self, attention: int) -> None:
        self.attention = attention
        self.base_url = "http://test"
        self.user_id = "u1"

    async def close(self) -> None:
        return None

    async def overview(self) -> dict[str, Any]:
        return {
            "counts": {"running": 0, "queued": 0, "attention": self.attention},
            "router_model": "test-model",
        }

    async def daemons(self) -> dict[str, Any]:
        return {"daemons": []}

    async def home_workspace(self) -> str:
        return "home"

    async def tickets(self, **_params: Any) -> dict[str, Any]:
        return {"tickets": []}


def _tui(tmp_path: Path, **kwargs: Any) -> SuperAgentTui:
    return SuperAgentTui(
        server_url="http://test",
        user_id="u1",
        client=kwargs.pop("client", _OverviewClient(0)),
        session_store=SessionStore(tmp_path / "sessions"),
        history_path=tmp_path / "history",
        local_root=tmp_path,
        worktree_root=tmp_path,
        no_splash=True,
        **kwargs,
    )


def _display_width(text: str) -> int:
    return sum(get_cwidth(char) for char in text)


def _visual_lines(tui: SuperAgentTui, width: int) -> list[str]:
    control = ConversationControl(tui._conversation_rows_for_control)
    content = control.create_content(width, 24)
    lines: list[str] = []
    for index in range(content.line_count):
        text = "".join(
            piece
            for style, piece in content.get_line(index)
            if style != "[SetCursorPosition]"
        )
        lines.append(text)
    return lines


def test_reduced_motion_env_and_dumb_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PTC_REDUCED_MOTION", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert reduced_motion_enabled() is False
    monkeypatch.setenv("PTC_REDUCED_MOTION", "1")
    assert reduced_motion_enabled() is True
    monkeypatch.setenv("PTC_REDUCED_MOTION", "0")
    monkeypatch.setenv("TERM", "dumb")
    assert reduced_motion_enabled() is True


def test_a13_reduced_motion_frames_match_when_elapsed_is_frozen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two paints 300 ms apart stay identical only when motion is reduced."""
    monkeypatch.delenv("PTC_REDUCED_MOTION", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    def paints(reduced: bool) -> tuple[str, str]:
        tui = _tui(tmp_path / ("on" if reduced else "off"), reduced_motion=reduced)
        mono = {"now": 5.0}
        tui._monotonic = lambda: mono["now"]
        tui._clock = lambda: 100.0
        tui.conversation.add(
            ProgressBlock(
                ticket_id="ticket-1",
                phase="running",
                stage="working",
                started_at=90.0,
            )
        )
        first = "\n".join(row_text(row) for row in tui.present_transcript())
        mono["now"] = 5.3
        second = "\n".join(row_text(row) for row in tui.present_transcript())
        return first, second

    moving_a, moving_b = paints(False)
    assert moving_a != moving_b
    assert moving_a.split(" ", 1)[0] != moving_b.split(" ", 1)[0]

    still_a, still_b = paints(True)
    assert still_a == still_b
    assert still_a.startswith("\u25cf ")


def test_a13_refresh_interval_slows_to_one_second(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PTC_REDUCED_MOTION", "1")
    monkeypatch.setenv("TERM", "xterm-256color")
    tui = _tui(tmp_path)
    app = tui.build_application(output=DummyOutput())
    assert app.refresh_interval == 1.0
    plain = _tui(tmp_path / "motion", reduced_motion=False)
    plain_app = plain.build_application(output=DummyOutput())
    assert plain_app.refresh_interval == 0.5


def test_a18_heartbeat_calls_the_renderer_at_most_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """800 unchanged blocks plus one running card: one tick, almost no renders."""
    import ptc_cli.superagent.tui_render as render_mod

    calls = {"n": 0}
    original = render_mod.render_markdown

    def counting(text: str, width: int, theme: Any = None, *args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if theme is None:
            return original(text, width, *args, **kwargs)
        return original(text, width, theme, *args, **kwargs)

    monkeypatch.setattr(render_mod, "render_markdown", counting)
    tui = _tui(tmp_path)
    clock = {"now": 1000.0}
    tui._clock = lambda: clock["now"]
    tui._monotonic = lambda: 20.0
    for index in range(800):
        tui.conversation.add(AssistantBlock(text=f"**step {index}** uses `tool`"))
    tui.conversation.add(
        ProgressBlock(
            ticket_id="ticket-live",
            phase="running",
            stage="still working",
            started_at=clock["now"],
        )
    )
    tui.present_transcript()
    warmed = calls["n"]
    assert warmed >= 800
    calls["n"] = 0
    clock["now"] += 0.1
    tui._monotonic = lambda: 20.1
    tui.present_transcript()
    assert calls["n"] <= 2


def test_a7_resize_repaints_borders_and_drops_frozen_panels(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    widths = {"columns": 120}
    tui._columns = lambda: widths["columns"]  # type: ignore[method-assign]
    tui.build_application(output=DummyOutput())
    wide = str(tui._border_fragments("╭", "╮")[0][1])
    assert _display_width(wide) == 120
    widths["columns"] = 80
    narrow = str(tui._border_fragments("╭", "╮")[0][1])
    assert _display_width(narrow) == 80
    frozen = "╭" + "─" * 70 + "╮\n│ frozen panel │\n╰" + "─" * 70 + "╯"
    tui.conversation.add(AssistantBlock(text=frozen))
    for width in (120, 80):
        widths["columns"] = width
        lines = _visual_lines(tui, width)
        assert "frozen panel" not in "\n".join(lines)
        for line in lines:
            assert _display_width(line) <= width
            if line[:1] in _BOX:
                assert line[-1:] in _BOX


def _composer_heights(tui: SuperAgentTui) -> tuple[int, int, int]:
    """Top border, composer preferred height, bottom border."""
    body = tui._app.layout.container
    children = list(body.children)
    top = children[2].content.height
    preferred = tui.input_area.window.height.preferred
    bottom = children[4].content.height
    return int(top), int(preferred), int(bottom)


def test_a10_empty_composer_stays_three_rows(tmp_path: Path) -> None:
    tui = _tui(tmp_path)
    tui._columns = lambda: 100  # type: ignore[method-assign]
    tui.build_application(output=DummyOutput())
    assert tui._border_enabled() is True
    assert _composer_heights(tui) == (1, 1, 1)
    height = tui.input_area.window.height
    assert height.min == 1
    assert height.max == 1
    for _index in range(40):
        tui.conversation.add(AssistantBlock(text="a transcript row"))
    assert _composer_heights(tui) == (1, 1, 1)
    again = _tui(tmp_path / "long")
    again._columns = lambda: 100  # type: ignore[method-assign]
    for _index in range(40):
        again.conversation.add(AssistantBlock(text="a transcript row"))
    again.build_application(output=DummyOutput())
    assert _composer_heights(again) == (1, 1, 1)


@pytest.mark.asyncio
async def test_a15_footer_attention_matches_overview(tmp_path: Path) -> None:
    tui = _tui(tmp_path, client=_OverviewClient(20))
    await tui._refresh_overview()
    assert tui.status.attention == 20
    segments = tui._status_segments()
    assert any(piece.endswith("20") for piece in segments)
