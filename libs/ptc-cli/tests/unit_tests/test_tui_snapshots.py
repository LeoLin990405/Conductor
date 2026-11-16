"""Headless glyph snapshots of the Super-agent TUI.

``render_frame`` builds the real prompt_toolkit application against a fake
client, paints one frame through ``Vt100_Output``, and reads the grid back
with pyte. Goldens are the glyph grids. A style grid is written beside each
one when the cell colour maps onto a theme token.

Spinner frame index is 0-based, matching ``int(monotonic * 10) % len(frames)``
in ``tui.py``. For ``("♪", "♫", "♬", "♫")`` index 2 is ``♬`` (the brief's
parenthetical ``♫`` is the second glyph, not this index).
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyte
import pytest
from prompt_toolkit.application.current import set_app
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.color_depth import ColorDepth
from prompt_toolkit.output.vt100 import Vt100_Output
from wcwidth import wcwidth

_SRC = Path(__file__).resolve().parents[4] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ptc_cli.superagent.client import ActivityStreamEvent
from ptc_cli.superagent.tui import (  # noqa: E402
    SuperAgentTui,
    _SPINNER_FRAMES,
    format_duration,
)
from ptc_cli.superagent.tui_blocks import (  # noqa: E402
    AssistantBlock,
    EndBlock,
    PermissionBlock,
    ProgressBlock,
    ToolBlock,
    UserBlock,
)
from ptc_cli.superagent.tui_permissions import PermissionRequest  # noqa: E402
import ptc_cli.superagent.tui as tui_module  # noqa: E402
from ptc_cli.superagent.tui_sessions import SessionStore  # noqa: E402
import ptc_cli.superagent.tui_sessions as tui_sessions  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "tui_snapshots"
# Each width is painted at the height it is reviewed at: 80x24 and 120x40,
# and 60x20 for the narrow frames.
HEIGHTS = {60: 20, 80: 24, 120: 40}
CLOCK = 1_700_000_000.0
# 0.25*10 == 2.5, so the spinner tick is index 2.
SPINNER_MONO = 0.25
SESSION_ID = "bad2c65500000001"
TICKET_ID = "7f782add-0000-4000-8000-000000000001"
WAITING_ID = "6bfa630f-0000-4000-8000-000000000002"
FAILED_ID = "4b2f7803-0000-4000-8000-000000000003"

RESULT_BODY = "\n".join(
    [
        "Adapters in the registry",
        "",
        "| Name | Class |",
        "| --- | --- |",
        "| dsh | DeepSeek |",
        "| doubao | DoubaoArk |",
        "",
        "共 2 个注册名，未改任何文件。",
        "The registered adapter names stay alphabetical across the native registry today.",
        "请把注册表里的适配器名字逐项核对清楚，确认没有遗漏，然后再告诉我下一步该怎么处理。",
        "**done** and `registry.py` see [the note](https://example.com/note).",
        "AUTONOMY_STATE: hidden-trailer",
    ]
)
RESULT_PHRASES = (
    "Adapters in the registry",
    "dsh",
    "DeepSeek",
    "doubao",
    "DoubaoArk",
    "共 2 个注册名，未改任何文件。",
    "the note",
)

# Glyphs the design language requires on that screen. Missing any of these
# fails the test instead of accepting a golden that dropped them.
REQUIRED: dict[str, tuple[str, ...]] = {
    "home": ("❯", "✔", "◆", "✖"),
    "slash-menu": ("❯", "/reply", "/resume"),
    "running": ("❯", _SPINNER_FRAMES[2], "♩", "▸", "╰"),
    "completed-table": ("❯", "✔", *RESULT_PHRASES),
    "waiting-reply": ("❯", "◆", "1.", "2."),
    "failed": ("❯", "✖"),
    "permission-pending": ("❯", "◆"),
    # One visual row per Ticket. Home already contains the titles, so the
    # jobs table has to show the id and the state together.
    "jobs-list": (
        "❯",
        "✔",
        "◆",
        "✖",
        "Read registry",
        "Need a host",
        "Sandbox denied",
    ),
    "dispatch-view": ("❯", "L1", "L2"),  # noqa: RUF001
    "review": ("❯", "review queue", "MERGE"),  # noqa: RUF001
    "help": ("❯", "Keys", "/status"),
    # The same help, scrolled to its last row.
    "help-end": ("❯", "/status", "/help"),
    "interrupt-confirm": ("❯", "stopped watching", _SPINNER_FRAMES[2]),
}
# 24 rows hold the top of the help. /status is checked on help-end instead.
REQUIRED_AT: dict[tuple[str, int], tuple[str, ...]] = {
    ("help", 80): ("❯", "Keys"),
}

SCREENS = (
    "home",
    "slash-menu",
    "running",
    "completed-table",
    "waiting-reply",
    "failed",
    "permission-pending",
    "jobs-list",
    "dispatch-view",
    "review",
    "help",
    "help-end",
    "interrupt-confirm",
)

_TOKEN_LETTERS = {
    "fg": "f",
    "text": "f",
    "foreground": "f",
    "muted": "m",
    "meta": "m",
    "accent": "a",
    "baton": "a",
    "brand": "a",
    "ok": "o",
    "success": "o",
    "done": "o",
    "danger": "d",
    "error": "d",
    "warn": "w",
    "hold": "w",
    "warning": "w",
    "tool": "t",
}
_CJK_BREAKS = "，。、；：？！"
_SGR_COLOR = re.compile(r"\x1b\[([0-9;]*)m")
_PIPE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_WORD = re.compile(r"[A-Za-z0-9]+")
_CACHE: dict[tuple[Any, ...], "Frame"] = {}


class FakeClient:
    """API facade with the methods the TUI calls. Nothing here opens a socket."""

    def __init__(self) -> None:
        self.base_url = "http://test"
        self.user_id = "u1"
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.replies: list[tuple[str, str]] = []
        self.stops: list[str] = []
        self.closed = False
        self.block_stream = False
        self.stream_started = asyncio.Event()
        self.tickets_payload = {
            "tickets": [
                {
                    "ticket_id": TICKET_ID,
                    "status": "completed",
                    "title": "Read registry",
                    "worker_model": "kimi",
                    "elapsed_seconds": 12,
                },
                {
                    "ticket_id": WAITING_ID,
                    "status": "waiting",
                    "title": "Need a host",
                    "worker_model": "deepseek",
                    "elapsed_seconds": 7552.5,
                },
                {
                    "ticket_id": FAILED_ID,
                    "status": "failed",
                    "title": "Sandbox denied",
                    "worker_model": "cursor",
                    "elapsed_seconds": 28635.4,
                },
            ]
        }

    async def close(self) -> None:
        self.closed = True

    async def overview(self) -> dict[str, Any]:
        return {
            "counts": {"running": 0, "queued": 0, "attention": 0},
            "sources": 1,
            "enabled_sources": 1,
            "router_model": "deepseek-flash",
        }

    async def daemons(self) -> dict[str, Any]:
        return {
            "daemons": [
                {
                    "daemon_id": "daemon-1",
                    "name": "local",
                    "provider": "codex",
                    "last_seen_at": "2099-01-01T00:00:00Z",
                }
            ]
        }

    async def home_workspace(self) -> str:
        return "home-ws"

    async def create_task(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        self.created.append((prompt, kwargs))
        return {"ticket_id": TICKET_ID, "status": "queued"}

    async def stream_activity(self, ticket_id: str, *, last_event_id: str | None = None):
        del ticket_id, last_event_id
        if self.block_stream:
            self.stream_started.set()
            await asyncio.Event().wait()
        if False:
            yield ActivityStreamEvent(event="end", data={}, event_id=None)

    async def activity(self, ticket_id: str) -> dict[str, Any]:
        del ticket_id
        return {"events": []}

    async def ticket(self, ticket_id: str) -> dict[str, Any]:
        return {
            "ticket_id": ticket_id,
            "status": "completed",
            "summary": "all done",
            "title": "Read registry",
        }

    async def tickets(self, **params: Any) -> dict[str, Any]:
        del params
        return self.tickets_payload

    async def stop(self, ticket_id: str) -> dict[str, Any]:
        self.stops.append(ticket_id)
        return {"ok": True}

    async def reply(self, ticket_id: str, text: str) -> dict[str, Any]:
        self.replies.append((ticket_id, text))
        return {"ok": True}

    async def dispatch(self, dispatch_id: str) -> dict[str, Any]:
        return {
            "id": dispatch_id,
            "state": "APPROVAL",
            "progress_pct": 62,
            "integrate_branch": "integrate/r2",
            "goal": "Ship the terminal-native dispatch screen",
            "lines": [
                {
                    "id": "L1",
                    "status": "completed",
                    "assigned_model": "kimi",
                    "gate": "reviewed",
                    "fix_rounds": 1,
                    "verdict": "MERGE",
                    "depends_on": [],
                },
                {
                    "id": "L2",
                    "status": "running",
                    "assigned_model": "deepseek",
                    "gate": "build",
                    "fix_rounds": 0,
                    "depends_on": ["L1"],
                },
            ],
        }

    async def fleet(self) -> dict[str, Any]:
        return {"agents": []}

    async def sources(self) -> dict[str, Any]:
        return {"sources": []}

    async def models(self) -> dict[str, Any]:
        return {"models": {}}

    async def progress(self, ticket_id: str) -> dict[str, Any]:
        return {"ticket_id": ticket_id, "phase": "running", "elapsed_seconds": 42}


@dataclass
class Frame:
    glyphs: str
    escapes: str
    style: str


def _matrix() -> list[tuple[str, int, str]]:
    rows: list[tuple[str, int, str]] = []
    for screen in SCREENS:
        for width in (80, 120):
            for theme in ("dark", "light"):
                rows.append((screen, width, theme))
        rows.append((screen, 80, "none"))
    for screen in ("home", "completed-table"):
        for theme in ("dark", "light"):
            rows.append((screen, 60, theme))
    return rows


def _apply_theme_env(theme: str) -> dict[str, str | None]:
    """Point the process env at one theme. Returns the previous values.

    Glyphs follow ``LANG``: a non-UTF-8 locale swaps in the ASCII set, which
    would snapshot the wrong alphabet. The render always speaks UTF-8.
    """
    keys = (
        "NO_COLOR",
        "CONDUCTOR_THEME",
        "COLORTERM",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PTC_ASCII",
        "TERM",
        "PTC_REDUCED_MOTION",
    )
    previous = {key: os.environ.get(key) for key in keys}
    os.environ["LANG"] = "en_US.UTF-8"
    os.environ["LC_ALL"] = "en_US.UTF-8"
    os.environ["LC_CTYPE"] = "en_US.UTF-8"
    # Pytest often inherits TERM=dumb, which freezes the spinner. These frames
    # pin the conducting glyph, so they opt into a UTF-8 terminal.
    os.environ["TERM"] = "xterm-256color"
    os.environ.pop("PTC_REDUCED_MOTION", None)
    os.environ.pop("PTC_ASCII", None)
    if theme == "none":
        os.environ["NO_COLOR"] = "1"
        os.environ["CONDUCTOR_THEME"] = "none"
        os.environ.pop("COLORTERM", None)
    else:
        os.environ.pop("NO_COLOR", None)
        os.environ["CONDUCTOR_THEME"] = theme
        os.environ["COLORTERM"] = "truecolor"
    return previous


def _restore_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


_THEME_COLOR_FIELDS = (
    "fg",
    "text",
    "muted",
    "faint",
    "baton",
    "accent",
    "brand",
    "tempo",
    "ok",
    "hold",
    "error",
    "warn",
    "danger",
    "tool",
    "user",
    "rule",
)


def _remember_color(palette: dict[str, str], name: str, color: str) -> None:
    letter = _TOKEN_LETTERS.get(name)
    if letter is None:
        return
    hex_color = str(color or "").strip().lstrip("#").lower()
    if len(hex_color) == 6 and all(ch in "0123456789abcdef" for ch in hex_color):
        palette.setdefault(hex_color, letter)


def _color_palette() -> dict[str, str]:
    """Map a pyte fg hex (no hash) to a one-letter token."""
    palette: dict[str, str] = {}
    try:
        from ptc_cli.superagent.tui_theme import resolve_theme
    except ImportError:
        return palette
    for theme_name in ("dark", "light"):
        theme = resolve_theme(
            {"CONDUCTOR_THEME": theme_name, "LANG": "en_US.UTF-8"}
        )
        mapping: dict[str, Any] = {}
        if isinstance(theme, dict):
            mapping = {key: value for key, value in theme.items() if isinstance(value, str)}
        else:
            for attr in ("colors", "tokens", "palette"):
                raw = getattr(theme, attr, None)
                if isinstance(raw, dict):
                    mapping = raw
                    break
            if not mapping:
                for name in _THEME_COLOR_FIELDS:
                    value = getattr(theme, name, None)
                    if isinstance(value, str):
                        mapping[name] = value
        for name, color in mapping.items():
            if isinstance(color, str):
                _remember_color(palette, str(name), color)
    return palette


def _style_grid(screen: pyte.Screen, width: int, palette: dict[str, str]) -> str:
    lines: list[str] = []
    for y in range(screen.lines):
        cells: list[str] = []
        skip = False
        for x in range(width):
            if skip:
                skip = False
                continue
            char = screen.buffer[y][x]
            if wcwidth(char.data or " ") == 2:
                skip = True
            fg = str(char.fg or "default").lower().lstrip("#")
            cells.append(palette.get(fg, "."))
        lines.append("".join(cells).rstrip("."))
    return "\n".join(lines).rstrip() + "\n"


def _glyph_grid(data: str, width: int, height: int, palette: dict[str, str]) -> tuple[str, str]:
    screen = pyte.Screen(width, height)
    pyte.Stream(screen).feed(data)
    glyphs = "\n".join(line.rstrip() for line in screen.display) + "\n"
    return glyphs, _style_grid(screen, width, palette)


class _Capture(io.StringIO):
    def isatty(self) -> bool:
        return True


def _missing_glyphs(screen: str, glyphs: str, width: int) -> list[str]:
    required = REQUIRED_AT.get((screen, width), REQUIRED[screen])
    return [item for item in required if item not in glyphs]


async def _settle(tui: SuperAgentTui) -> None:
    """Join startup tasks that paint Home.

    ``startup`` schedules the recent-ticket read and returns before it lands,
    so a frame taken immediately would omit the ✔ / ◆ / ✖ rows.
    """
    pending = [task for task in list(tui._tasks) if not task.done()]
    if pending:
        await asyncio.gather(*pending)


async def _prepare(tui: SuperAgentTui, client: FakeClient, screen: str) -> None:
    await tui.startup()
    await _settle(tui)
    if screen == "running":
        tui.conversation.add(UserBlock(text="List the registered adapters."))
        tui.conversation.add(
            AssistantBlock(text="kimi is conducting on branch feat/tui.")
        )
        tui.conversation.add(
            ToolBlock(
                name="read_file",
                status="done",
                duration=1.2,
                lines=["registry.py adapters", "kept the table", "no files changed"],
                expanded=False,
                worker_detail=False,
            )
        )
        block = ProgressBlock(
            ticket_id=TICKET_ID,
            phase="running",
            stage="Worker is executing the Ticket",
            started_at=CLOCK - 42,
            worker="kimi",
            branch="feat/tui",
            ticket_short=TICKET_ID[:8],
        )
        tui.conversation.add(block)
        tui._progress_block = block
        tui.status.phase = "running"
        tui.status.worker = "kimi"
        tui.status.branch = "feat/tui"
        tui.status.ticket_short = TICKET_ID[:8]
        if tui.session is not None:
            tui.session.last_ticket_id = TICKET_ID
    elif screen == "completed-table":
        tui.conversation.add(UserBlock(text="Show the adapter table."))
        tui.conversation.add(
            EndBlock(status="completed", body=RESULT_BODY, summary="")
        )
        tui.status.phase = "completed"
    elif screen == "waiting-reply":
        tui.conversation.add(UserBlock(text="Run the smoke test."))
        tui.conversation.add(
            EndBlock(
                status="waiting",
                question="Re-run on a host with bash / Leave the Ticket waiting",
            )
        )
        tui.status.phase = "waiting"
        if tui.session is not None:
            tui.session.last_ticket_id = WAITING_ID
    elif screen == "failed":
        tui.conversation.add(UserBlock(text="Run the sandbox command."))
        tui.conversation.add(
            EndBlock(
                status="failed",
                summary="The shell was refused on this worker.",
                error="Worker exited 1: sandbox-exec denied",
            )
        )
        tui.status.phase = "failed"
    elif screen == "permission-pending":
        request = PermissionRequest(
            action="local-merge",
            label="merge feat/tui",
            detail="into integrate/r2",
        )
        block = tui.conversation.add(
            PermissionBlock(
                action=request.action,
                label=request.label,
                detail=request.detail,
            )
        )
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        tui._pending = (request, future, block)
        tui._pending_epoch = tui._epoch
    elif screen == "jobs-list":
        await tui.handle_command("/jobs")
    elif screen == "dispatch-view":
        await tui.handle_command("/dispatch 7f3a91c2")
    elif screen == "review":
        # /review status reads the server-backed queue through the operator
        # CLI; the frame paints the records that read returns.
        reviews = [
            {
                "line": "L3",
                "subject": "Add the review queue screen",
                "reviewer": "L1",
                "gate": "reviewed",
                "fix": 1,
                "verdict": "MERGE",
                "decision": "approve",
            },
            {
                "line": "L4",
                "subject": "Pin the reviewed commit",
                "reviewer": "L2",
                "gate": "build",
                "fix": 0,
                "verdict": "BLOCK",
                "decision": "reject",
            },
        ]
        gate = {"line": "L3", "sha": "4b2f7803", "dispatch": "7f3a91c2"}
        payload = json.dumps({"reviews": reviews, "gate": gate})
        tui.permissions.allow_session("local-review")
        saved = tui_module.run_conductor
        tui_module.run_conductor = lambda *_args, **_kwargs: payload
        try:
            await tui.handle_command("/review status")
        finally:
            tui_module.run_conductor = saved
    elif screen == "help":
        await tui.handle_command("/help")
        tui._follow_tail = False
        tui._scroll_offset = 0
    elif screen == "help-end":
        await tui.handle_command("/help")
        tui._follow_tail = True
    elif screen == "interrupt-confirm":
        client.block_stream = True
        watch = asyncio.create_task(tui.watch(TICKET_ID))
        try:
            await asyncio.wait_for(client.stream_started.wait(), timeout=2)
            tui.interrupt()
            await asyncio.wait_for(watch, timeout=5)
        except TimeoutError as exc:
            watch.cancel()
            raise AssertionError(
                "interrupt confirmation did not settle; the watch is still blocked"
            ) from exc
    elif screen not in {"home", "slash-menu"}:
        raise AssertionError(f"unknown screen {screen}")


async def _render_async(width: int, height: int, theme: str, screen_id: str) -> Frame:
    previous_id = tui_sessions.new_session_id
    tui_sessions.new_session_id = lambda: SESSION_ID
    env = _apply_theme_env(theme)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = FakeClient()
            tui = SuperAgentTui(
                server_url="http://test",
                user_id="u1",
                client=client,
                session_store=SessionStore(root / "sessions"),
                history_path=root / "history",
                local_root=root,
                worktree_root=root,
                no_splash=True,
                clock=lambda: CLOCK,
                monotonic=lambda: SPINNER_MONO,
            )
            # Commands run inside a live app, which reads its width from the
            # terminal; the harness prepares the screen before the app exists,
            # so it gives the TUI the same width the frame is drawn at.
            tui._columns = lambda: width  # type: ignore[method-assign]
            await _prepare(tui, client, screen_id)
            capture = _Capture()
            depth = ColorDepth.DEPTH_1_BIT if theme == "none" else ColorDepth.DEPTH_24_BIT
            output = Vt100_Output(
                capture,
                lambda: Size(rows=height, columns=width),
                term="xterm-256color",
                default_color_depth=depth,
                enable_cpr=False,
            )
            with create_pipe_input() as pipe:
                app = tui.build_application(input=pipe, output=output)
                if screen_id == "slash-menu":
                    tui._open_palette("/re")
                tui._theme = None
                with set_app(app):
                    app.renderer.render(app, app.layout)
            escapes = capture.getvalue()
            glyphs, style = _glyph_grid(escapes, width, height, _color_palette())
            await tui.close()
            return Frame(glyphs=glyphs, escapes=escapes, style=style)
    finally:
        tui_sessions.new_session_id = previous_id
        _restore_env(env)


def _parts(width: int, height: int, theme: str, screen_id: str) -> Frame:
    key = (width, height, theme, screen_id)
    cached = _CACHE.get(key)
    if cached is None:
        cached = asyncio.run(_render_async(width, height, theme, screen_id))
        _CACHE[key] = cached
    return cached


def render_frame(width: int, height: int, theme: str, screen_id: str) -> str:
    """Render one screen to a glyph grid (rows joined by newlines)."""
    return _parts(width, height, theme, screen_id).glyphs


def sgr_color_params(escapes: str) -> list[int]:
    """SGR parameters in 30–38, 40–48, or 90–107. Bold and reverse are ignored."""
    found: list[int] = []
    for match in _SGR_COLOR.finditer(escapes):
        body = match.group(1)
        if not body:
            continue
        parts = [int(piece) if piece else 0 for piece in body.split(";")]
        index = 0
        while index < len(parts):
            param = parts[index]
            if param in {38, 48} and index + 1 < len(parts) and parts[index + 1] == 2:
                found.append(param)
                index += 5
                continue
            if param in {38, 48} and index + 1 < len(parts) and parts[index + 1] == 5:
                found.append(param)
                index += 3
                continue
            if 30 <= param <= 38 or 40 <= param <= 48 or 90 <= param <= 107:
                found.append(param)
            index += 1
    return found


def _display_width(text: str) -> int:
    return sum(max(0, wcwidth(char)) if char != "\t" else 4 for char in text)


def _visible_row(line: str) -> str:
    """Drop a scrollbar arrow and the padding that only exists to reach it.

    The arrow sits in the margin. A short transcript row is padded out to
    that column, which would otherwise look like a full-width wrap.
    """
    if len(line) >= 2 and line[-1] in "^v":
        head = line[:-1]
        if not head or head[-1].isspace():
            return head.rstrip()
    return line.rstrip()


def assert_no_word_split(glyphs: str, width: int) -> None:
    """A full visual row must not break an ASCII word or open on CJK punctuation.

    A break after a space is a wrap, not a split: the next row then starts
    with whitespace, or the space was trailing and the row is no longer full.
    """
    lines = glyphs.splitlines()
    limit = max(1, width - 1)
    for index, raw in enumerate(lines):
        line = _visible_row(raw)
        stripped = line.lstrip()
        if stripped and stripped[0] in _CJK_BREAKS:
            raise AssertionError(f"CJK line starts with {stripped[0]!r}: {line!r}")
        if index + 1 >= len(lines):
            continue
        if _display_width(line) < limit:
            continue
        nxt_raw = lines[index + 1]
        if not nxt_raw or nxt_raw[0].isspace():
            continue
        nxt = _visible_row(nxt_raw)
        if not nxt or nxt[0] in _CJK_BREAKS or not nxt[0].isalnum():
            continue
        if not line or not line[-1].isalnum():
            continue
        left = _WORD.search(line[::-1])
        if left is None:
            continue
        broken = left.group(0)[::-1]
        if not line.endswith(broken):
            continue
        right = _WORD.match(nxt)
        if right is None:
            continue
        word = broken + right.group(0)
        if _display_width(word) <= width:
            raise AssertionError(
                f"ASCII word {word!r} is split across lines {index} and {index + 1}"
            )


def _golden_path(screen: str, width: int, theme: str, *, style: bool = False) -> Path:
    suffix = ".style.txt" if style else ".txt"
    return GOLDEN_DIR / f"{screen}.{width}.{theme}{suffix}"


def _assert_required(screen: str, glyphs: str, width: int) -> None:
    if screen == "jobs-list" and "^J" in glyphs:
        raise AssertionError(
            "jobs list paints embedded newlines as ^J, so each Ticket is not "
            "its own row. The golden was not updated to match that screen.\n"
            f"{glyphs}"
        )
    missing = _missing_glyphs(screen, glyphs, width)
    if missing:
        raise AssertionError(
            f"{screen} is missing required glyphs {missing}.\n"
            "The golden was not updated to match a screen that dropped them.\n"
            f"{glyphs}"
        )


@pytest.mark.parametrize(("screen", "width", "theme"), _matrix())
def test_glyph_golden(screen: str, width: int, theme: str) -> None:
    frame = _parts(width, HEIGHTS[width], theme, screen)
    _assert_required(screen, frame.glyphs, width)
    glyph_path = _golden_path(screen, width, theme)
    style_path = _golden_path(screen, width, theme, style=True)
    if os.environ.get("TUI_SNAPSHOT_UPDATE") == "1":
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        glyph_path.write_text(frame.glyphs, encoding="utf-8")
        style_path.write_text(frame.style, encoding="utf-8")
        return
    assert glyph_path.is_file(), f"missing golden {glyph_path}"
    assert glyph_path.read_text(encoding="utf-8") == frame.glyphs
    if style_path.is_file():
        assert style_path.read_text(encoding="utf-8") == frame.style


@pytest.mark.parametrize(("screen", "width", "theme"), _matrix())
def test_golden_height_follows_its_size(screen: str, width: int, theme: str) -> None:
    golden = _golden_path(screen, width, theme).read_text(encoding="utf-8")
    assert len(golden.splitlines()) == HEIGHTS[width]


def test_a1_result_body_lines_appear_once() -> None:
    glyphs = render_frame(80, HEIGHTS[80], "dark", "completed-table")
    for phrase in RESULT_PHRASES:
        assert glyphs.count(phrase) == 1, phrase


@pytest.mark.parametrize(("screen", "width", "theme"), _matrix())
def test_a2_no_raw_markdown(screen: str, width: int, theme: str) -> None:
    for line in render_frame(width, HEIGHTS[width], theme, screen).splitlines():
        assert _PIPE_ROW.match(line) is None, line
        assert "**" not in line
        assert "`" not in line
        assert "](" not in line


@pytest.mark.parametrize(("screen", "width", "theme"), _matrix())
def test_a3_no_autonomy_trailer(screen: str, width: int, theme: str) -> None:
    assert "AUTONOMY_" not in render_frame(width, HEIGHTS[width], theme, screen)


@pytest.mark.parametrize(("screen", "width", "theme"), _matrix())
def test_a8_no_word_split(screen: str, width: int, theme: str) -> None:
    assert_no_word_split(render_frame(width, HEIGHTS[width], theme, screen), width)


@pytest.mark.parametrize("screen", SCREENS)
def test_a9_footer_is_one_row_at_80(screen: str) -> None:
    glyphs = render_frame(80, HEIGHTS[80], "dark", screen)
    lines = glyphs.splitlines()
    assert lines, "empty frame"
    footer = lines[-1]
    # The fixed key-hint row is gone. Phase hints live on the status row.
    assert "Enter send" not in glyphs
    assert "Esc stop watch" not in glyphs
    if screen == "permission-pending":
        assert "allow" in footer, footer
        return
    assert "auto" in footer, footer
    assert any(hint in footer for hint in ("help", "esc", "ctrl", "alt+m")), footer
    assert sum("needing you" in line for line in lines) == 0


def test_a11_contrast_ratio_if_present() -> None:
    try:
        from ptc_cli.superagent import tui_theme
    except ImportError:
        pytest.skip("tui_theme is not importable")
    ratio = getattr(tui_theme, "contrast_ratio", None)
    if not callable(ratio):
        pytest.skip("tui_theme.contrast_ratio is not defined")
    # Prefer the theme's own text-token and ground helpers. A module that
    # only publishes the ratio function is still called.
    text_tokens = getattr(tui_theme, "text_tokens", None)
    grounds_for = getattr(tui_theme, "grounds_for", None)
    resolve = getattr(tui_theme, "resolve_theme", None)
    if not (callable(text_tokens) and callable(grounds_for) and callable(resolve)):
        sample = ratio("#E6E8EE", "#0F1115")
        assert sample >= 4.5
        return
    checked = 0
    for name in ("dark", "light"):
        theme = resolve({"CONDUCTOR_THEME": name, "LANG": "en_US.UTF-8"})
        for token, color in text_tokens(theme).items():
            if not isinstance(color, str) or not color.startswith("#"):
                continue
            for ground in grounds_for(theme):
                checked += 1
                measured = ratio(color, ground)
                assert measured >= 4.5, (name, token, color, ground, measured)
    assert checked > 0


def test_a12_no_color_escape_stream_has_no_sgr_color() -> None:
    frame = _parts(80, HEIGHTS[80], "none", "home")
    colors = sgr_color_params(frame.escapes)
    assert colors == [], colors


def test_dark_frame_does_use_color() -> None:
    """The A12 parser must be able to see color, or a blank stream would pass."""
    frame = _parts(80, HEIGHTS[80], "dark", "home")
    assert sgr_color_params(frame.escapes)


def test_a14_format_duration() -> None:
    assert format_duration(28635.4) == "7h57m"
    assert format_duration(7552.5) == "2h05m"
    assert format_duration(42) == "42.0s"


def test_light_escape_differs_from_dark() -> None:
    """Token letters can match across themes. The escape stream must not."""
    dark = _parts(80, HEIGHTS[80], "dark", "home").escapes
    light = _parts(80, HEIGHTS[80], "light", "home").escapes
    assert sgr_color_params(dark), "dark home emitted no SGR color"
    assert sgr_color_params(light), "light home emitted no SGR color"
    assert dark != light, (
        "light and dark home frames share one escape stream. "
        "CONDUCTOR_THEME is ignored, so the light palette falls back to dark."
    )


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_scrollbar_arrows_are_not_bold(theme: str) -> None:
    width, height = 80, HEIGHTS[80]
    frame = _parts(width, height, theme, "help")
    screen = pyte.Screen(width, height)
    pyte.Stream(screen).feed(frame.escapes)
    arrows = [
        screen.buffer[y][width - 1]
        for y in range(height)
        if screen.buffer[y][width - 1].data in {"^", "v"}
    ]
    assert arrows, frame.glyphs
    assert not any(cell.bold for cell in arrows)


def test_spinner_frame_index_two() -> None:
    assert _SPINNER_FRAMES[2] == "♬"
    tick = int(SPINNER_MONO * 10)
    assert _SPINNER_FRAMES[tick % len(_SPINNER_FRAMES)] == "♬"


_PYTE_MARKDOWN = "\n".join(
    [
        "# Heading",
        "",
        "See `registry.py` and **strong**.",
        "",
        "```",
        "x = 2 ** 3  # ](",
        "```",
        "",
        "After the block.",
    ]
)
_TOKEN_FIELDS = (
    "fg",
    "muted",
    "faint",
    "baton",
    "tempo",
    "ok",
    "hold",
    "error",
    "rule",
    "selection_bg",
    "accent",
    "user",
    "tool",
    "warn",
    "danger",
)


def _theme_hexes(name: str) -> set[str]:
    from ptc_cli.superagent.tui_theme import resolve_theme

    theme = resolve_theme({"CONDUCTOR_THEME": name, "LANG": "en_US.UTF-8"})
    found: set[str] = set()
    for field in _TOKEN_FIELDS:
        value = str(getattr(theme, field, "") or "").strip().lstrip("#").lower()
        if len(value) == 6 and all(ch in "0123456789abcdef" for ch in value):
            found.add(value)
    return found


async def _paint_markdown(width: int, height: int, theme: str) -> tuple[str, set[str], str, str]:
    """One markdown card. Does not read or write snapshot goldens."""
    env = _apply_theme_env(theme)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tui = SuperAgentTui(
                server_url="http://test",
                user_id="u1",
                client=FakeClient(),
                session_store=SessionStore(root / "sessions"),
                history_path=root / "history",
                local_root=root,
                worktree_root=root,
                no_splash=True,
                clock=lambda: CLOCK,
                monotonic=lambda: SPINNER_MONO,
            )
            tui.conversation.add(UserBlock(text="Show the note."))
            tui.conversation.add(EndBlock(status="completed", summary="", body=_PYTE_MARKDOWN))
            tui.status.phase = "completed"
            capture = _Capture()
            output = Vt100_Output(
                capture,
                lambda: Size(rows=height, columns=width),
                term="xterm-256color",
                default_color_depth=ColorDepth.DEPTH_24_BIT,
                enable_cpr=False,
            )
            with create_pipe_input() as pipe:
                app = tui.build_application(input=pipe, output=output)
                tui._theme = None
                with set_app(app):
                    app.renderer.render(app, app.layout)
            data = capture.getvalue()
            screen = pyte.Screen(width, height)
            pyte.Stream(screen).feed(data)
            glyphs = "\n".join(line.rstrip() for line in screen.display)
            hexes: set[str] = set()
            heading_fg = ""
            code_fg = ""
            for y in range(height):
                for x in range(width):
                    char = screen.buffer[y][x]
                    for color in (char.fg, char.bg):
                        text = str(color or "").lower().lstrip("#")
                        if len(text) == 6 and all(ch in "0123456789abcdef" for ch in text):
                            hexes.add(text)
                line = screen.display[y]
                if "Heading" in line and not heading_fg:
                    heading_fg = str(screen.buffer[y][line.find("H")].fg).lower().lstrip("#")
                if "2 ** 3" in line and not code_fg:
                    code_fg = str(screen.buffer[y][line.find("x")].fg).lower().lstrip("#")
            await tui.close()
            return glyphs, hexes, heading_fg, code_fg
    finally:
        _restore_env(env)


@pytest.mark.parametrize(("width", "height"), [(80, 24), (120, 40)])
@pytest.mark.parametrize("theme", ["dark", "light"])
def test_pyte_markdown_frames_use_token_colours(width: int, height: int, theme: str) -> None:
    glyphs, hexes, heading_fg, code_fg = asyncio.run(_paint_markdown(width, height, theme))
    allowed = _theme_hexes(theme)
    assert hexes, glyphs
    assert hexes <= allowed, sorted(hexes - allowed)
    assert "272822" not in hexes
    assert "ff4689" not in hexes
    assert "Heading" in glyphs
    assert "│" in glyphs
    assert "2 ** 3" in glyphs
    assert "](" in glyphs
    assert "Enter send" not in glyphs
    assert "Esc stop watch" not in glyphs
    assert heading_fg in allowed
    assert code_fg in allowed
    assert heading_fg != code_fg
    lines = glyphs.splitlines()
    heading_at = next(index for index, line in enumerate(lines) if "Heading" in line)
    assert any(not lines[index].strip() for index in range(heading_at + 1, heading_at + 4))
