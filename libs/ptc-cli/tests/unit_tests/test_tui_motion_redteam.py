"""Red-team probes for reduced-motion painting and the transcript caches.

A probe fails when a new block is painted with a dead block's text, or when a
newline in the conducting title or stage opens another status row.
"""

from __future__ import annotations

import gc
import sys
from pathlib import Path

import pytest

# tui imports conductor_cli, which lives in the repo src tree; the ptc-cli
# lockfile's conductor-core is a different PyPI project.
_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.tui import ConversationControl, SuperAgentTui, row_text  # noqa: E402
from ptc_cli.superagent.tui_blocks import AssistantBlock, ProgressBlock  # noqa: E402
from ptc_cli.superagent.tui_sessions import SessionStore  # noqa: E402


class _OverviewClient:
    def __init__(self) -> None:
        self.base_url = "http://test"
        self.user_id = "u1"

    async def close(self) -> None:
        return None


def _tui(tmp_path: Path) -> SuperAgentTui:
    return SuperAgentTui(
        server_url="http://test",
        user_id="u1",
        client=_OverviewClient(),
        session_store=SessionStore(tmp_path / "sessions"),
        history_path=tmp_path / "history",
        local_root=tmp_path,
        worktree_root=tmp_path,
        no_splash=True,
    )


def _visual_lines(tui: SuperAgentTui, width: int) -> list[str]:
    control = ConversationControl(lambda: (tui.present_transcript(), 0))
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


def test_recycled_assistant_id_does_not_replay_previous_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A freed AssistantBlock's rows must not paint on the next block.

    Both blocks are appended once, so each version is 1. The row cache keys
    on ``id(block)`` and keeps the dead entry, and CPython reuses that id.
    """
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("PTC_REDUCED_MOTION", raising=False)
    tui = _tui(tmp_path)
    tui._columns = lambda: 80  # type: ignore[method-assign]
    for index in range(40):
        previous = AssistantBlock(text=f"ALPHA-{index}-PREVIOUS")
        previous.append("!")
        assert previous.version == 1
        tui.conversation.blocks[:] = [previous]
        tui.present_transcript()
        stale_id = id(previous)
        tui.conversation.blocks.clear()
        del previous
        gc.collect()
        fresh = AssistantBlock(text=f"BETA-{index}-NEXT")
        fresh.append("?")
        assert fresh.version == 1
        if id(fresh) != stale_id:
            del fresh
            gc.collect()
            continue
        tui.conversation.blocks[:] = [fresh]
        shown = "\n".join(row_text(row) for row in tui.present_transcript())
        assert f"BETA-{index}-NEXT" in shown
        assert f"ALPHA-{index}-PREVIOUS" not in shown
        return
    pytest.fail("CPython did not reuse an AssistantBlock id; the cache key was not exercised")


def test_hostile_title_and_stage_stay_one_status_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newlines in the conducting title and stage stay on that one row.

    ``plain()`` turns a carriage return into a line feed and keeps it. Home
    titles and the footer collapse that whitespace; the card does not, so the
    wrap splits one status row into several.
    """
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("PTC_REDUCED_MOTION", raising=False)
    tui = _tui(tmp_path)
    tui._columns = lambda: 80  # type: ignore[method-assign]
    tui._clock = lambda: 20.0  # type: ignore[method-assign]
    tui._monotonic = lambda: 4.0  # type: ignore[method-assign]
    tui.status.daemon = "worker\r\nEVIL-DAEMON"
    block = ProgressBlock(
        ticket_id="ticket-1",
        phase="running",
        stage="stage\nEVIL-STAGE\rTAIL",
        started_at=10.0,
    )
    tui.conversation.add(block)
    # Only the open card may read the status daemon. A historical card must not.
    tui._progress_block = block
    visual = _visual_lines(tui, 80)
    title_lines = [line for line in visual if "worker" in line or "EVIL-DAEMON" in line]
    stage_lines = [
        line
        for line in visual
        if "EVIL-STAGE" in line or "TAIL" in line or line.strip() == "stage"
    ]
    bare = [line for line in visual if line.strip() in {"EVIL-DAEMON", "EVIL-STAGE", "TAIL"}]
    problems: list[str] = []
    title_ok = (
        len(title_lines) == 1
        and "worker" in title_lines[0]
        and "EVIL-DAEMON" in title_lines[0]
        and "\n" not in title_lines[0]
        and "\r" not in title_lines[0]
    )
    stage_ok = (
        len(stage_lines) == 1
        and "stage" in stage_lines[0]
        and "EVIL-STAGE" in stage_lines[0]
        and "TAIL" in stage_lines[0]
        and "\n" not in stage_lines[0]
        and "\r" not in stage_lines[0]
    )
    if not title_ok:
        problems.append(f"title split across {title_lines!r}")
    if not stage_ok:
        problems.append(f"stage split across {stage_lines!r}")
    if bare:
        problems.append(f"injected own rows {bare!r}")
    assert problems == []
