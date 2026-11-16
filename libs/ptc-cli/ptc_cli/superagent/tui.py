"""Full-screen Super-agent TUI: conversation, composer, status bar.

The TUI is a prompt_toolkit application over the same autonomy API the line
shell uses:

* the conversation is a scrollable window of typed blocks (streamed assistant
  text, activity rows, collapsible tool blocks with status/duration and a
  colored diff, errors, end state);
* the composer is fixed below it, with ``/`` command and ``@file`` completion,
  Shift+Enter for a newline, history, Esc to stop watching and a double
  Ctrl+C exit;
* the status bar shows the Router model, active Jobs, daemon, token estimate
  and the live/polling transport state;
* local mutating actions (``/merge``, ``/review run``, ``!shell``) go through
  an Allow once / Session allow / Deny confirmation before they run.

Backend interaction is delegated to :mod:`tui_events` (SSE with reconnect and
polling fallback) and :mod:`tui_local` (local subprocesses); the model and
sessions are in :mod:`tui_blocks` and :mod:`tui_sessions`. This module owns the
terminal application, the command dispatch, and the reduce/notify loop.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from datetime import UTC
from pathlib import Path
from typing import Any

from prompt_toolkit import Application
from prompt_toolkit.completion import CompleteEvent, Completer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.formatted_text.utils import fragment_list_width
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.key_binding.bindings.named_commands import get_by_name
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    ConditionalContainer,
    Dimension,
    FormattedTextControl,
    HSplit,
    Layout,
    ScrollOffsets,
    UIContent,
    Window,
)
from prompt_toolkit.layout.controls import UIControl
from prompt_toolkit.layout.margins import Margin, ScrollbarMargin
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea
from rich.cells import cell_len

from .client import SuperAgentClient, SuperAgentError
from .dispatch_view import render_dispatch_view
from .text import TokenSanitizer, plain
from .tui_blocks import (
    SEALED_BOUNDARY,
    ActivityBlock,
    AssistantBlock,
    Conversation,
    DispatchBlock,
    EndBlock,
    ErrorBlock,
    NoticeBlock,
    PermissionBlock,
    ProgressBlock,
    ToolBlock,
    UserBlock,
    estimate_tokens,
    format_duration,
    split_text_and_diffs,
    visible_progress_fields,
)
from .tui_completion import TuiCompleter, expand_mentions, slash_help_lines
from .tui_events import (
    WATCH_TIMEOUT_S,
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
    output_reset_notice,
    watch_ticket,
)
from .tui_home import home_rows
from .tui_i18n import resolve_locale, t
from .tui_jobs import attention_rows, job_detail_rows, jobs_rows, watch_header_rows
from .tui_local import describe, run_conductor, run_shell
from .tui_palette import (
    COMMANDS as PALETTE_COMMANDS,
)
from .tui_palette import (
    KIND_ARGUMENTS,
    ArgOption,
    PaletteState,
    argument_rows,
    canonical_command,
    command_rows,
    complete_arguments,
    rank_commands,
    render_palette,
)
from .tui_palette import (
    STRINGS as PALETTE_STRINGS,
)
from .tui_permissions import (
    PermissionManager,
    PermissionRequest,
    decision_from_key,
    render_permission_prompt,
)
from .tui_review import progress_rows, result_rows, usage_rows
from .tui_rich import model_table
from .tui_sessions import SessionRecord, SessionStore
from .tui_theme import resolve_theme, status_glyph

MIN_TUI_COLUMNS = 80
DOUBLE_CTRL_C_WINDOW_S = 1.5
MAX_COMPOSER_HEIGHT = 8
_SUPERVISOR_ACTIVITY_KINDS = frozenset(
    {"created", "claimed", "report", "closed", "route"}
)
_ROUTE_WORKER = re.compile(
    r"Routed to\s+(\S+)\s+\(branch\s+([^)]+)\)", re.IGNORECASE
)
_PHASE_DESCRIPTIONS = {
    "queued": "Waiting for a Worker to claim the Ticket",
    "running": "Worker is executing the Ticket",
    "waiting": "Waiting for your decision",
    "completed": "Ticket completed",
    "failed": "Ticket failed",
    "cancelled": "Ticket cancelled",
    "blocked": "Ticket blocked",
    "archived": "Ticket archived",
}
# Terminal phases are peers. SSE may enter this rank from an open phase and
# then must not switch peers or leave. ``waiting`` stays open: the server
# progress projection treats it as the step before a terminal result.
_TERMINAL_PHASES = frozenset({"completed", "failed", "cancelled"})
# Ticket status → Supervisor phase. Same table as the server progress
# projection: blocked is waiting, archived is cancelled, unknown values stay.
_PROGRESS_PHASES = {
    "queued": "queued",
    "running": "running",
    "waiting": "waiting",
    "blocked": "waiting",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "archived": "cancelled",
}
_OPEN_PHASE_RANK = {"queued": 0, "running": 1, "waiting": 2}
_PROGRESS_SOURCE_SERVER = "server"
_PROGRESS_SOURCE_STREAM = "stream"
_PROGRESS_SOURCE_MARK = {
    _PROGRESS_SOURCE_SERVER: "source server",
    _PROGRESS_SOURCE_STREAM: "source stream",
}


# tui_jobs reads these spellings as needs-you; --attention keeps those rows
# and every row that carries an open question.
_NEEDS_YOU_STATUSES = frozenset({"needs-you", "needs_you", "attention", "waiting"})


def _needs_operator(row: dict[str, Any]) -> bool:
    """True for the rows ``/jobs --attention`` shows (the tui_jobs filter)."""
    status = str(row.get("status") or "").strip().lower().replace(" ", "-")
    if status in _NEEDS_YOU_STATUSES:
        return True
    attention = row.get("attention")
    question = (
        attention.get("question") if isinstance(attention, dict) else row.get("question")
    )
    return bool(str(question or "").strip())


def _canonical_phase(status: str) -> str:
    text = str(status or "").strip()
    return _PROGRESS_PHASES.get(text, text)


def _phase_rank(phase: str) -> int | None:
    canonical = _canonical_phase(phase)
    if canonical in _TERMINAL_PHASES:
        return 3
    return _OPEN_PHASE_RANK.get(canonical)


def _phase_moves_forward(current: str, proposed: str) -> bool:
    """SSE phase order: queued < running < waiting < terminal.

    A terminal phase stays where it is. An unknown proposed phase does not
    move the view. A calibration response does not use this guard.
    """
    new_rank = _phase_rank(proposed)
    if new_rank is None:
        return False
    old_rank = _phase_rank(current)
    if old_rank is None:
        return True
    if old_rank >= 3:
        return False
    return new_rank > old_rank


def _progress_field(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _progress_elapsed(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < 0 or number != number or number in {float("inf"), float("-inf")}:
        return None
    return number

HELP_TEXT = """\
Keys
  Enter           send the line, run the menu item, or reply when a Ticket needs you
  Shift+Enter     new line (Ctrl+J; Esc then Enter in terminals that send plain Enter)
  @path           attach a local file to the prompt
  Ctrl+K          palette (also / at column 0); type to filter, Tab accepts, Enter runs
  /               palette prefilled with / when the caret is at column 0
  ?               help, when the composer is empty
  Esc             close the palette, answer later, or interrupt the Ticket after a confirmation
  Alt+M           cycle mode: auto, dispatch, read-only
  Shift+Tab       previous focus
  Ctrl+C          clear the composer; twice within 1.5s quits
  PgUp/PgDn       scroll the conversation
  Up              recall history when the palette is closed
  Ctrl+T          show the task list
  Ctrl+O          expand the newest collapsed output

Commands
""" + "\n".join(f"  {name:<12} {hint}" for name, hint in slash_help_lines())


_TICKET_UUID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _id_prefix(head: str) -> bool:
    """True when ``head`` is at least eight hex characters, hyphens allowed."""
    if not re.fullmatch(r"[0-9a-fA-F-]{8,}", head):
        return False
    return sum(char in "0123456789abcdefABCDEF" for char in head) >= 8
_MODES = ("auto", "dispatch", "read-only")
_SPINNER_FRAMES = ("♪", "♫", "♬", "♫")
_LAYER_RE = re.compile(r"^L(\d+):\s*")
_MARKDOWN_RE = re.compile(r"(\*\*|`|\]\(|^\s*\|)", re.MULTILINE)
_AUTONOMY_RE = re.compile(r"^AUTONOMY_[A-Z0-9_]*:.*$", re.MULTILINE)

# Every notice and hint this module paints, in the two languages the TUI
# speaks. The table goes through tui_i18n, so a missing zh-CN twin is a bug
# here rather than an operator seeing half a sentence.
_TUI_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "unsupported": "not supported by the server yet",
        "placeholder.long": "Describe a task, / for commands, @ to attach a file",
        "placeholder.short": "Describe a task, / for commands",
        "denied": "denied: {label}",
        "pressEscAgain": "press Esc again to interrupt",
        "compacted": "compacted {count} block(s)",
        "expanded": "expanded {count} tool block(s)",
        "collapsed": "collapsed {count} tool block(s)",
        "workerDetailsShown": "worker details shown",
        "workerDetailsHidden": "worker details hidden (Supervisor view)",
        "newSession": "new session {id}",
        "alreadyInSession": "already in session {id}",
        "resumedSession": "resumed session {id}",
        "resumedSessionTicket": "resumed session {id} (last Ticket {ticket}); {count} blocks",
        "daemonGone": "resumed daemon {id} is no longer registered; selecting a live one",
        "serverSummaryUnavailable": "server summary unavailable: {error}",
        "fleetUnavailable": "fleet unavailable: {error}",
        "modelsUnavailable": "models unavailable: {error}",
        "watchTimedOut": "watch timed out; the Ticket is still running",
        "stopRequested": "stop requested for {id}",
        "retryRequested": "retry requested for {id}",
        "decisionRequested": "{decision} requested for {id}",
        "repliedTo": "replied to {id}",
        "runningLocally": "running locally: {label}",
        "runningShell": "running shell: {command}",
        "noOutput": "(no output)",
        "none": "none",
        "readOnly": "read-only — not creating a Ticket",
        "noTicket": "no Ticket",
        "noActiveDispatches": "no active dispatches",
        "dispatchNotFound": "dispatch not found",
        "noJobs": "no Jobs",
        "noSavedSessions": "no saved sessions",
        "answerLater": "answer later",
        "stoppedWatching": "stopped watching (the Ticket keeps running)",
        "switchedWatches": "switched watches (the previous Ticket keeps running)",
        "pressCtrlCAgain": "press Ctrl+C again to quit",
        "decision": "decision",
        "hint.mode": "mode",
        "hint.newline": "newline",
        "hint.help": "help",
        "hint.interrupt": "interrupt",
        "hint.later": "later",
        "hint.allow": "allow",
        "hint.session": "session",
        "hint.deny": "deny",
        "hint.tab": "tab accept",
        "hint.ctrlDown": "ctrl+down list",
        "hint.escClose": "esc close",
        "hint.altNav": "alt+n nav",
        "presence.online": "online",
        "presence.reconnecting": "reconnecting",
        "mode.auto": "auto",
        "mode.dispatch": "dispatch",
        "mode.read-only": "read-only",
    },
    "zh-CN": {
        "unsupported": "服务端暂不支持",
        "placeholder.long": "描述一个任务，/ 打开命令，@ 附加文件",  # noqa: RUF001
        "placeholder.short": "描述一个任务，/ 打开命令",  # noqa: RUF001
        "denied": "已拒绝：{label}",  # noqa: RUF001
        "pressEscAgain": "再按一次 Esc 中断",
        "compacted": "已压缩 {count} 个块",
        "expanded": "已展开 {count} 个工具块",
        "collapsed": "已折叠 {count} 个工具块",
        "workerDetailsShown": "已显示 worker 详情",
        "workerDetailsHidden": "已隐藏 worker 详情（Supervisor 视图）",  # noqa: RUF001
        "newSession": "新会话 {id}",
        "alreadyInSession": "已在会话 {id} 中",
        "resumedSession": "已恢复会话 {id}",
        "resumedSessionTicket": "已恢复会话 {id}（最后的 Ticket {ticket}）；{count} 个块",  # noqa: RUF001
        "daemonGone": "已恢复的 daemon {id} 不再注册；正在选择一个在线的",  # noqa: RUF001
        "serverSummaryUnavailable": "服务端摘要不可用：{error}",  # noqa: RUF001
        "fleetUnavailable": "fleet 不可用：{error}",  # noqa: RUF001
        "modelsUnavailable": "模型不可用：{error}",  # noqa: RUF001
        "watchTimedOut": "跟随超时；Ticket 仍在运行",  # noqa: RUF001
        "stopRequested": "已请求停止 {id}",
        "retryRequested": "已请求重试 {id}",
        "decisionRequested": "已请求 {decision} {id}",
        "repliedTo": "已回复 {id}",
        "runningLocally": "正在本地运行：{label}",  # noqa: RUF001
        "runningShell": "正在运行 shell：{command}",  # noqa: RUF001
        "noOutput": "（无输出）",  # noqa: RUF001
        "none": "无",
        "readOnly": "只读模式 — 不会创建 Ticket",
        "noTicket": "没有 Ticket",
        "noActiveDispatches": "没有活跃的派发",
        "dispatchNotFound": "找不到该派发",
        "noJobs": "没有 Job",
        "noSavedSessions": "没有已保存的会话",
        "answerLater": "稍后回答",
        "stoppedWatching": "已停止跟随（Ticket 会继续运行）",  # noqa: RUF001
        "switchedWatches": "已切换跟随（上一个 Ticket 会继续运行）",  # noqa: RUF001
        "pressCtrlCAgain": "再按一次 Ctrl+C 退出",
        "decision": "决定",
        "hint.mode": "模式",
        "hint.newline": "换行",
        "hint.help": "帮助",
        "hint.interrupt": "中断",
        "hint.later": "稍后",
        "hint.allow": "允许",
        "hint.session": "会话",
        "hint.deny": "拒绝",
        "hint.tab": "tab 接受",
        "hint.ctrlDown": "ctrl+down 列表",
        "hint.escClose": "esc 关闭",
        "hint.altNav": "alt+n 导航",
        "presence.online": "在线",
        "presence.reconnecting": "重连中",
        "mode.auto": "自动",
        "mode.dispatch": "派发",
        "mode.read-only": "只读",
    },
}
# The two spellings of the one notice the server has never carried. Kept as a
# table even though callers use :func:`_unsupported`, so the English twin is
# visible next to the Chinese one.
_UNSUPPORTED = {
    "en": _TUI_STRINGS["en"]["unsupported"],
    "zh-CN": _TUI_STRINGS["zh-CN"]["unsupported"],
}


def _unsupported(locale: str | None = None) -> str:
    return t(_TUI_STRINGS, "unsupported", locale)


def _notice(key: str, locale: str | None = None) -> str:
    return t(_TUI_STRINGS, key, locale)


_COLOR_ALIASES = {
    "fg": ("fg", "text", "foreground"),
    "muted": ("muted", "meta"),
    "faint": ("faint", "rule", "border"),
    "accent": ("accent", "baton", "brand"),
    "user": ("user",),
    "ok": ("ok", "success", "done"),
    "danger": ("danger", "error"),
    "warn": ("warn", "hold", "warning"),
    "tool": ("tool",),
    "tempo": ("tempo", "running"),
    "rule": ("rule", "border"),
    "selection": ("selection", "selection_bg"),
}


def _display_width(text: str) -> int:
    return sum(max(1, get_cwidth(char)) if char != "\t" else 4 for char in text)


def _pad_columns(text: str, width: int) -> str:
    """Pad on the right so the next column starts at the same place on every row."""
    gap = width - _display_width(text)
    if gap > 0:
        return text + (" " * gap)
    return text


def fit_status_line(segments: list[str], width: int) -> str:
    """Join status segments, dropping whole segments from the right.

    A segment is never sliced, so a narrow terminal cannot end mid-word.
    """
    width = max(0, int(width))
    chosen: list[str] = []
    for segment in segments:
        # Newlines and carriage returns in a segment would open extra rows.
        piece = " ".join(str(segment or "").split())
        if not piece:
            continue
        candidate = piece if not chosen else " · ".join([*chosen, piece])
        if _display_width(candidate) <= width:
            chosen.append(piece)
        else:
            break
    return " · ".join(chosen)


def align_status_line(counts: list[str], hints: list[str], width: int) -> str:
    """Counts on the left, hints on the right. Hints drop before a count does."""
    width = max(0, int(width))
    left = fit_status_line(counts, width)
    chosen: list[str] = []
    for hint in hints:
        piece = " ".join(str(hint or "").split())
        if not piece:
            continue
        trial = " · ".join([*chosen, piece])
        gap = 1 if left else 0
        if _display_width(left) + gap + _display_width(trial) <= width:
            chosen.append(piece)
        else:
            break
    if not chosen:
        return left
    right = " · ".join(chosen)
    if not left:
        return right
    pad = width - _display_width(left) - _display_width(right)
    if pad < 1:
        return left
    return f"{left}{' ' * pad}{right}"


def _paint(
    color: str,
    *,
    bold: bool = False,
    reverse: bool = False,
    bg: str = "",
) -> str:
    """A prompt_toolkit style. Background is set only for a selected row."""
    parts: list[str] = []
    if bold:
        parts.append("bold")
    if reverse:
        parts.append("reverse")
    text = str(color or "").strip()
    if text:
        if text.startswith(("fg:", "bg:")) or text == "default":
            parts.append(text)
        elif text.startswith("#") or text.startswith("ansi"):
            parts.append(f"fg:{text}")
        else:
            parts.append(text)
    ground = str(bg or "").strip()
    if ground:
        if ground.startswith("#") or ground.startswith("ansi"):
            parts.append(f"bg:{ground}")
        elif ground.startswith("bg:"):
            parts.append(ground)
    return " ".join(parts)


def _theme_mapping(theme: Any) -> dict[str, Any]:
    if theme is None:
        return {}
    if isinstance(theme, dict):
        return theme
    for name in ("colors", "tokens", "palette"):
        raw = getattr(theme, name, None)
        if isinstance(raw, dict):
            return raw
    return {
        key: getattr(theme, key)
        for key in dir(theme)
        if not key.startswith("_") and isinstance(getattr(theme, key, None), str)
    }


def _lookup_color(theme: Any, token: str) -> str:
    mapping = _theme_mapping(theme)
    for alias in _COLOR_ALIASES.get(token, (token,)):
        value = mapping.get(alias)
        if isinstance(value, str) and value.strip():
            return value.strip()
        attr = getattr(theme, alias, None) if theme is not None else None
        if isinstance(attr, str) and attr.strip():
            return attr.strip()
    return ""


def _lookup_glyph(theme: Any, name: str, fallback: str) -> str:
    glyphs = getattr(theme, "glyphs", None) if theme is not None else None
    if isinstance(theme, dict):
        glyphs = theme.get("glyphs", glyphs)
    value: Any = None
    if isinstance(glyphs, dict):
        value = glyphs.get(name)
    elif glyphs is not None:
        value = getattr(glyphs, name, None)
    if value is None and theme is not None and not isinstance(theme, dict):
        value = getattr(theme, name, None)
    if isinstance(value, (list, tuple)) and value:
        return str(value[0])
    if isinstance(value, str) and value:
        return value
    return fallback


def _spinner_frames(theme: Any) -> tuple[str, ...]:
    glyphs = getattr(theme, "glyphs", None) if theme is not None else None
    if isinstance(theme, dict):
        glyphs = theme.get("glyphs", glyphs)
    value: Any = None
    if isinstance(glyphs, dict):
        value = glyphs.get("spinner")
    elif glyphs is not None:
        value = getattr(glyphs, "spinner", None)
    if isinstance(value, str) and value.strip():
        frames = tuple(ch for ch in value if not ch.isspace())
        return frames or _SPINNER_FRAMES
    if isinstance(value, (list, tuple)) and value:
        return tuple(str(item) for item in value if str(item))
    return _SPINNER_FRAMES


def reduced_motion_enabled(environ: dict[str, str] | None = None) -> bool:
    """Static conducting mark. ``TERM=dumb`` implies the same freeze.

    ``--reduced-motion`` is a constructor flag. The CLI parser lives outside
    this package, so the environment variable is the switch tests and operators
    can set without that parser.
    """
    environment = os.environ if environ is None else environ
    flag = str(environment.get("PTC_REDUCED_MOTION", "")).strip().lower()
    if flag in {"1", "true", "yes", "on"}:
        return True
    return str(environment.get("TERM", "")).strip().lower() == "dumb"


def resolve_app_theme(environ: dict[str, str] | None = None) -> Any:
    """The resolved :mod:`tui_theme` palette for this terminal."""
    environment = os.environ if environ is None else environ
    return resolve_theme(environment)


def build_default_style(environ: dict[str, str] | None = None) -> Style:
    """Style dictionary from theme tokens. The transcript has no background."""
    environment = os.environ if environ is None else environ
    theme = resolve_app_theme(environment)
    theme_name = str(getattr(theme, "name", "") or "").lower()
    no_color = bool(environment.get("NO_COLOR")) or theme_name in {
        "none",
        "no-color",
        "nocolor",
        "plain",
    }

    def color(token: str) -> str:
        if no_color:
            return ""
        return _lookup_color(theme, token)

    fg = color("fg")
    muted = color("muted")
    faint = color("faint")
    accent = color("accent")
    user = color("user") or accent
    ok = color("ok")
    danger = color("danger")
    warn = color("warn")
    tool = color("tool") or accent
    tempo = color("tempo") or accent
    rule = color("rule") or color("faint")
    selection = color("selection")
    rules = {
        # Identity line is not on screen; the classes stay so a heartbeat
        # that still asks for it does not lose its styles.
        "header": _paint(muted),
        "header-brand": _paint(accent, bold=True),
        "header-title": _paint(fg, bold=True),
        "header-meta": _paint(muted),
        "header-live": _paint(ok),
        "header-idle": _paint(muted),
        # No bg on the transcript: the user's terminal shows through.
        # Bold is only the title line, headings, strong text, and the menu.
        "conversation": _paint(fg),
        "tui-user": _paint(user),
        "tui-agent": _paint(fg),
        "tui-conductor": _paint(accent),
        "tui-subagent": _paint(tool),
        "tui-reasoning": _paint(muted),
        "tui-meta": _paint(muted),
        "tui-heading": _paint(accent, bold=True),
        "tui-strong": _paint(fg, bold=True),
        "tui-code": _paint(tempo),
        "tui-inline": _paint(accent),
        "tui-bullet": _paint(fg),
        "tui-tool": _paint(tool),
        "tui-tool-output": _paint(muted),
        "tui-diff-add": _paint(ok),
        "tui-diff-del": _paint(danger),
        "tui-diff-hunk": _paint(accent),
        "tui-diff-header": _paint(tool),
        "tui-diff-context": _paint(muted),
        "tui-error": _paint(danger),
        "tui-error-title": _paint(danger, bold=True),
        "tui-warn": _paint(warn),
        "tui-rail": _paint(warn),
        "tui-end": _paint(ok),
        "tui-result-title": _paint(ok, bold=True),
        "tui-question": _paint(warn),
        "tui-question-title": _paint(warn, bold=True),
        "tui-tempo": _paint(tempo, bold=True),
        "tui-step-run": _paint(tempo),
        "tui-step-done": _paint(ok),
        "rule": _paint(rule),
        "statusbar": _paint(muted),
        "status-hold": _paint(warn),
        "status-label": _paint(muted),
        "status-value": _paint(fg),
        "status-accent": _paint(accent),
        "status-good": _paint(ok),
        "status-sep": _paint(faint),
        "hint": _paint(muted),
        "composer": _paint(fg),
        "composer-border": _paint(rule),
        "composer-hold": _paint(warn),
        "prompt": _paint(accent),
        "prompt-hold": _paint(warn),
        "slash-menu": _paint(fg),
        "slash-menu-selected": _paint(
            fg, bold=True, bg=selection, reverse=no_color or not selection
        ),
        "palette-label": _paint(accent, bold=True),
        "palette-query": _paint(fg),
        "palette-footer": _paint(muted),
        "palette-note": _paint(warn),
        "palette-error": _paint(danger),
        "permission": _paint(warn),
        # The toolkit default track is a dark grey. Keep the bar on tokens.
        "scrollbar.background": _paint("", bg=rule),
        "scrollbar.button": _paint("", bg=muted),
        # prompt_toolkit's default arrow is bold; bold is kept for titles and headings.
        "scrollbar.arrow": f"nobold {_paint(muted)}",
    }
    if no_color:
        rules["menu-selected"] = "reverse"
    return Style.from_dict(rules)


try:
    DEFAULT_STYLE = build_default_style()
except Exception:
    DEFAULT_STYLE = build_default_style({"NO_COLOR": "1", "CONDUCTOR_THEME": "none"})


def _strip_autonomy(text: str) -> str:
    body = _AUTONOMY_RE.sub("", str(text or ""))
    lines = [line for line in body.splitlines() if not line.startswith("AUTONOMY_")]
    return "\n".join(lines).strip()


def _clean_text(text: str) -> str:
    try:
        from .tui_render import strip_protocol_trailers

        return str(strip_protocol_trailers(text) or "")
    except ImportError:
        return _strip_autonomy(text)


def _flatten_markdown(rendered: Any) -> list[str]:
    if rendered is None:
        return []
    if isinstance(rendered, str):
        return rendered.splitlines()
    lines: list[str] = []
    for row in rendered:
        if isinstance(row, str):
            lines.append(row)
        elif isinstance(row, tuple) and len(row) >= 2 and isinstance(row[1], str):
            lines.append(row[1])
        elif isinstance(row, list):
            parts: list[str] = []
            for frag in row:
                if isinstance(frag, tuple) and len(frag) >= 2:
                    parts.append(str(frag[1]))
                else:
                    parts.append(str(frag))
            lines.append("".join(parts))
        else:
            lines.append(str(row))
    return lines


def _crude_markdown(text: str) -> str:
    body = _strip_autonomy(text)
    body = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", body)
    body = body.replace("**", "").replace("`", "")
    kept: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and set(stripped.replace("|", "").replace("-", "").replace(":", "").replace(" ", "")) == set():
            continue
        if stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            kept.append("  ".join(cell for cell in cells if cell))
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _phase_glyph(phase: str, theme: Any) -> str:
    canonical = _canonical_phase(phase)
    if canonical == "completed":
        return _lookup_glyph(theme, "done", "✔")
    if canonical == "waiting":
        return _lookup_glyph(theme, "needs_you", "◆")
    if canonical == "failed":
        return _lookup_glyph(theme, "failed", "✖")
    if canonical == "queued":
        return _lookup_glyph(theme, "queued", "○")
    if canonical == "running":
        return _lookup_glyph(theme, "online", "●")
    if canonical == "cancelled":
        return "⊘"
    return "·"


def _relabel_stage(line: str) -> str:
    match = _LAYER_RE.match(line)
    if match is None:
        return line
    return f"stage {int(match.group(1)) + 1}: {line.split(':', 1)[1].lstrip()}"


def _one_line(value: object) -> str:
    """Collapse controls and newlines so one status row cannot become several."""
    return " ".join(plain(str(value or "")).split())


def _cache_content(block: Any) -> tuple[Any, ...]:
    """Rendered inputs for a block. Object ids are reused after collection."""
    names = (
        "text",
        "agent",
        "reasoning",
        "sealed",
        "epoch",
        "run_id",
        "title",
        "body",
        "label",
        "name",
        "status",
        "summary",
        "detail",
        "event_kind",
        "at",
        "kind",
        "worker_detail",
        "duration",
        "started",
        "expanded",
    )
    values: list[Any] = [type(block).__name__]
    fields = getattr(block, "__dict__", {})
    for name in names:
        if name in fields:
            values.append(fields[name])
    lines = fields.get("lines")
    if isinstance(lines, list):
        values.append(tuple(lines))
    attachments = fields.get("attachments")
    if isinstance(attachments, list):
        values.append(tuple(attachments))
    return tuple(values)


def _status_glyphs(payload: Any) -> str:
    rows: list[Any] = []
    if isinstance(payload, dict):
        for key in ("skills", "servers", "items", "mcp", "results", "agents"):
            value = payload.get(key)
            if isinstance(value, list):
                rows = value
                break
        if not rows:
            rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    lines: list[str] = []
    for row in rows[:12]:
        if isinstance(row, str):
            lines.append(f"✔ {plain(row)}")
            continue
        if not isinstance(row, dict):
            continue
        name = plain(row.get("name") or row.get("id") or row.get("server") or "item")
        status = str(row.get("status") or row.get("state") or "").lower()
        if status in {"ok", "ready", "online", "enabled", "healthy", "installed"}:
            glyph = "✔"
        elif status in {"error", "failed", "offline", "missing", "disabled"}:
            glyph = "✖"
        else:
            glyph = "◆"
        lines.append(f"{glyph} {name}".rstrip())
    return "\n".join(lines) if lines else "◆ empty"


_NUMBERED_OPTION = re.compile(r"^\d+\.\s+\S")


def _question_parts(question: str) -> tuple[str, list[str]]:
    """The question once, then its options. Already-numbered lines stay as written."""
    text = str(question or "").strip()
    if not text:
        return "", []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) >= 2:
        return lines[0], lines[1:]
    if " / " in text:
        parts = [part.strip() for part in text.split(" / ") if part.strip()]
        if len(parts) >= 2:
            return "", parts
    return text, []


def _question_options(question: str) -> list[str]:
    _stem, options = _question_parts(question)
    return options


def _ignore_key(event: Any) -> None:
    """A binding that consumes its key and does nothing."""
    del event


def _bind_newline(kb: KeyBindings, sequence: list[str], filter_: Any) -> None:
    """Bind a multi-key Shift+Enter sequence; the handler is set per app."""
    @kb.add(*sequence, filter=filter_)
    def _handler(event: Any) -> None:
        app = event.app
        controller = getattr(app, "tui_controller", None)
        if controller is not None:
            controller._insert_newline()
        else:
            message = getattr(event, "current_buffer", None)
            if message is not None:
                message.insert_text("\n")


def choose_mode(
    *,
    is_tty: bool,
    columns: int,
    no_tui: bool = False,
    print_prompt: str | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Pick the run mode: ``print`` (one-shot), ``tui`` or ``line``.

    A pipe, a narrow terminal (< 80 columns) or ``CONDUCTOR_NO_TUI`` cannot
    host the fixed-layout TUI; those fall back to the existing Rich line shell.
    """
    environment = env if env is not None else os.environ
    if print_prompt is not None:
        return "print"
    if no_tui or environment.get("CONDUCTOR_NO_TUI", "").lower() in {"1", "true", "yes"}:
        return "line"
    if not is_tty:
        return "line"
    if columns and columns < MIN_TUI_COLUMNS:
        return "line"
    return "tui"


def detect_mode(
    *,
    no_tui: bool = False,
    print_prompt: str | None = None,
) -> str:
    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        columns = shutil.get_terminal_size((80, 24)).columns
    except OSError:
        columns = 80
    return choose_mode(
        is_tty=is_tty,
        columns=columns,
        no_tui=no_tui,
        print_prompt=print_prompt,
    )


class _ComposerWall(Margin):
    """One-column │ on the composer. Hidden when the border is off."""

    def __init__(self, enabled: Callable[[], bool]) -> None:
        self._enabled = enabled

    def get_width(self, get_ui_content: Callable[..., Any]) -> int:
        del get_ui_content
        return 1 if self._enabled() else 0

    def create_margin(
        self, window_render_info: Any, width: int, height: int
    ) -> list[tuple[str, str]]:
        del window_render_info
        if width <= 0 or height <= 0:
            return []
        rows: list[tuple[str, str]] = []
        for index in range(height):
            rows.append(("class:composer-border", "│"))
            if index != height - 1:
                rows.append(("class:composer-border", "\n"))
        return rows


def composer_placeholder(room: int, locale: str | None = None) -> str:
    """The placeholder that fits ``room`` cells (SPEC §2.2).

    The long form when it fits, the short form when only that fits, and the
    short form cut with ``…`` only below that. A room of zero is no text.
    """
    for key in ("placeholder.long", "placeholder.short"):
        text = _notice(key, locale)
        if cell_len(text) <= room:
            return text
    if room <= 0:
        return ""
    short = _notice("placeholder.short", locale)
    kept = ""
    for char in short:
        if cell_len(kept + char) > room - 1:
            break
        kept += char
    return kept.rstrip() + "…"


class _ComposerPlaceholder(Processor):
    """Ghost text while the composer is empty. It is not part of the buffer.

    The text is localized and sized to the row (SPEC §2.2): ``reserved`` is
    the width of the line prefix (the composer wall) that shares the row with
    the prompt and the placeholder.
    """

    def __init__(
        self,
        locale: Callable[[], str] | None = None,
        reserved: Callable[[], int] | None = None,
    ) -> None:
        self._locale = locale or resolve_locale
        self._reserved = reserved or (lambda: 0)

    def apply_transformation(self, transformation_input: Any) -> Transformation:
        fragments = list(transformation_input.fragments)
        document = transformation_input.document
        if document.text or transformation_input.lineno != 0:
            return Transformation(fragments)
        room = (
            int(transformation_input.width)
            - fragment_list_width(fragments)
            - self._reserved()
        )
        text = composer_placeholder(room, self._locale())
        if text:
            fragments.append(("class:tui-meta", text))
        return Transformation(fragments)


class ConversationControl(UIControl):
    """A scrollable conversation where every UIContent line is one visual row.

    ``Window`` wrapping is disabled: the control wraps each logical block row
    to the actual content width itself. That keeps the scroll position (and
    the ``[SetCursorPosition]`` marker) in visual-row units, so a long wrapped
    paragraph can be paged through instead of the cursor jumping between the
    first and last visual rows.
    """

    def __init__(self, provider: Callable[[], tuple[list[tuple[str, str]], int]]) -> None:
        self._provider = provider
        # (width, row signature) → wrapped lines without the cursor marker.
        # A heartbeat that paints the same rows skips the wrap.
        self._cache: tuple[int, tuple[tuple[str, str], ...]] | None = None
        self._cached_lines: list[list[tuple[str, str]]] = []

    def is_focusable(self) -> bool:
        # Shift+Tab lands on the transcript/list stop of the focus order.
        return True

    def create_content(self, width: int, height: int | None) -> UIContent:
        rows, requested = self._provider()
        width = max(1, int(width))
        signature = tuple(tuple(_row_fragments(row)) for row in rows)
        cache_hit = (
            self._cache is not None
            and self._cache[0] == width
            and self._cache[1] == signature
        )
        if cache_hit:
            lines = [list(line) for line in self._cached_lines]
        else:
            lines = []
            for row in rows:
                fragments = _row_fragments(row)
                text = "".join(piece for _style, piece in fragments)
                if len(fragments) <= 1:
                    style = fragments[0][0] if fragments else ""
                    for chunk in _wrap_visual_row(text, width):
                        lines.append([(style, chunk)])
                    continue
                if _display_width(text) <= width:
                    lines.append(fragments)
                    continue
                head_style, head_text = fragments[0]
                rest = "".join(piece for _style, piece in fragments[1:])
                rest_style = fragments[1][0] if len(fragments) > 1 else head_style
                head_width = _display_width(head_text)
                room = max(1, width - head_width)
                pad = " " * head_width
                for index, chunk in enumerate(_wrap_visual_row(rest, room)):
                    if index == 0:
                        lines.append([(head_style, head_text), (rest_style, chunk)])
                    else:
                        lines.append([(rest_style, pad + chunk)])
            if not lines:
                lines = [[]]
            self._cache = (width, signature)
            self._cached_lines = [list(line) for line in lines]
        target = max(0, min(requested, len(lines) - 1))
        lines[target] = [*lines[target], ("[SetCursorPosition]", "")]

        def get_line(index: int) -> list[tuple[str, str]]:
            return lines[index]

        return UIContent(
            get_line=get_line,
            line_count=len(lines),
            cursor_position=Point(x=0, y=target),
            show_cursor=False,
        )


class _PaletteControl(FormattedTextControl):
    """The docked palette region. Focusable so the query holds the keys.

    The rows come from :func:`tui_palette.render_palette`; this control only
    gives them the prompt_toolkit styles and makes the region a real focus
    stop, which is what lets Escape return to the composer.
    """

    def is_focusable(self) -> bool:
        return True


def _report_repeats(kind: str, activity: str, summary: str, body: str) -> bool:
    """A report line is hidden when it repeats the card's own sentence."""
    if kind != "report":
        return False
    text = plain(activity).strip()
    if not text:
        return True
    for other in (summary, body):
        cleaned = plain(other).strip()
        if cleaned == text or cleaned.startswith(text):
            return True
    return False


def _row_fragments(row: Any) -> list[tuple[str, str]]:
    """One logical row, either ``(style, text)`` or a list of those spans."""
    if isinstance(row, tuple) and len(row) == 2 and isinstance(row[1], str):
        return [(str(row[0]), row[1])]
    if isinstance(row, list):
        return [
            (str(style), str(text))
            for style, text in row
            if isinstance(text, str)
        ]
    return [("", "")]


def row_text(row: Any) -> str:
    """The visible text of one row, whether it is a pair or a list of spans."""
    return "".join(piece for _style, piece in _row_fragments(row))


def _collapse_blank_rows(rows: list[Any]) -> list[Any]:
    """One blank row between blocks. Leading and trailing blanks go away."""
    collapsed: list[Any] = []
    previous_blank = False
    for row in rows:
        blank = row_text(row).strip() == ""
        if blank:
            previous_blank = True
            continue
        if previous_blank and collapsed:
            collapsed.append(("", ""))
        collapsed.append(row)
        previous_blank = False
    return collapsed


def _wrap_visual_row(text: str, width: int) -> list[str]:
    """Wrap one logical row on word boundaries so a column cannot split a word.

    A newline inside the row is its own visual row. Continuation lines keep a
    two-column hanging indent. An ASCII word is cut only when it is wider
    than the row.
    """
    if not text:
        return [""]
    width = max(1, int(width))
    from .tui_render import wrap_words

    chunks: list[str] = []
    for piece in str(text).splitlines() or [""]:
        wrapped = wrap_words(piece, width, hanging=2)
        chunks.extend(wrapped or [""])
    return chunks or [""]


class TuiStatus:
    """The one mutable status bar state."""

    def __init__(self) -> None:
        self.router_model = "unknown"
        self.active_jobs = 0
        self.daemon = "no daemon"
        self.daemon_state = ""
        self.stream = "idle"
        self.phase = "idle"
        self.summary = ""
        self.body = ""
        self.question = ""
        self.error = ""
        self.started_monotonic: float | None = None
        self.finished_monotonic: float | None = None
        self.session = ""
        self.mode = "tui"
        self.attention = 0
        self.worker = ""
        self.branch = ""
        self.ticket_title = ""
        self.ticket_short = ""

    def line(self, tokens: int) -> str:
        daemon = self.daemon
        if self.daemon_state:
            daemon = f"{daemon} ({self.daemon_state})"
        return (
            f" model {self.router_model} │ jobs {self.active_jobs} │ "
            f"{daemon} │ ~{tokens} tok │ session {self.session} │ {self.stream} "
        )


class SuperAgentTui:
    """Controller + prompt_toolkit application for the Super-agent TUI."""

    def __init__(
        self,
        *,
        server_url: str,
        user_id: str,
        no_splash: bool = False,
        continue_session: bool = False,
        session_store: SessionStore | None = None,
        client: Any = None,
        local_root: Path | str | None = None,
        worktree_root: Path | str | None = None,
        history_path: Path | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[..., Any] = asyncio.sleep,
        timeout: float = WATCH_TIMEOUT_S,
        show_worker_details: bool = False,
        no_exit_summary: bool = False,
        reduced_motion: bool | None = None,
    ) -> None:
        self.server_url = server_url
        self.user_id = user_id
        self.no_splash = no_splash
        self.client = client or SuperAgentClient(server_url, user_id)
        self.sessions = session_store or SessionStore()
        self.permissions = PermissionManager()
        self.local_root = Path(local_root or Path.cwd())
        self.worktree_root = worktree_root
        self.conversation = Conversation()
        self.status = TuiStatus()
        self._progress_block: ProgressBlock | None = None
        # "" until a calibration is attempted. "server" after /progress wins;
        # "stream" when that read is missing or fails. SSE stays the liveness signal.
        self._progress_source = ""
        # Bumped when the viewed Ticket is replaced or cleared. A progress read
        # captures this, plus a request sequence, and drops the response if
        # either has moved on.
        self._progress_generation = 0
        self._progress_request = 0
        # Increments only when a progress payload is applied. An SSE frame is
        # merged unless a newer successful read landed while it was waiting.
        self._progress_applied = 0
        self._observed_status: str | None = None
        self.session: SessionRecord | None = None
        self._continue = continue_session
        self._workspace_id: str = ""
        self._daemon_id: str = ""
        self._daemon_label: str = ""
        self._jobs: list[dict[str, Any]] = []
        # The numbered list last painted (Home or /jobs). Digit indexes use it.
        self._shown_tickets: list[dict[str, Any]] = []
        self._list_snapshot: dict[int, dict[str, Any]] = {}
        self._pending: tuple[PermissionRequest, asyncio.Future[str], PermissionBlock] | None = None
        self._pending_epoch = 0
        self._permission_lock = asyncio.Lock()
        self._watch_task: asyncio.Task[Any] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._epoch = 0
        self._task_epochs: dict[asyncio.Task[Any], int] = {}
        self._interrupted = False
        self._interrupt_confirm = False
        self._composer_measuring = False
        self._last_ctrl_c = 0.0
        self._app: Application[Any] | None = None
        self._clock = clock
        self._monotonic = monotonic
        self._sleep = sleep
        self._timeout = timeout
        # The product default is the Supervisor view. ``/details`` re-opens the
        # Worker stream for debugging without changing the default experience.
        self.show_worker_details = show_worker_details
        self._follow_tail = True
        self._scroll_offset = 0
        self._history_path = history_path or (
            Path.home() / ".ptc-agent" / "superagent-tui-history"
        )
        self.completer: Completer = TuiCompleter(root=self.local_root)
        # Built in build_application(); exposed for tests.
        self.input_area: TextArea | None = None
        self.conversation_window: Window | None = None
        self.status_window: Window | None = None
        self.no_exit_summary = no_exit_summary
        # None reads PTC_REDUCED_MOTION and TERM=dumb on each paint.
        self._reduced_motion_override = reduced_motion
        self._markdown_cache: dict[tuple[Any, ...], list[str]] = {}
        self._row_cache: dict[tuple[Any, ...], list[tuple[str, str]]] = {}
        self.interaction_mode = "auto"
        self._online_workers = 0
        self._recent_tickets: list[dict[str, Any]] = []
        self._reply_dismissed = False
        self._menu_index = 0
        self._menu_query_seen: str | None = None
        self._opened_outputs: set[int] = set()
        self._theme: Any = None
        self.status.attention = 0
        # The palette is docked above the composer. Its query is separate from
        # the composer draft because Ctrl+K opens it with an empty query and
        # Escape returns the caret to the composer.
        self._palette_open = False
        self._palette_query = ""
        self._palette_selected = 0
        self._palette_results_focused = False
        self._palette_note = ""
        self._palette_kind = "commands"
        self._palette_command = ""
        self._palette_nav = False
        self._palette_dispatches: list[dict[str, Any]] = []
        self.palette_window: Window | None = None
        # ``main`` is the transcript/list stop, ``composer`` the text field.
        self._focus_target = "composer"
        # Where Escape returns the focus when the palette closes: the stop
        # that had it when Ctrl+K opened the palette.
        self._palette_opener = "composer"
        # /review status paints these through tui_review; /review run fills a
        # progress row while the local conductor subprocess is in flight.
        self._reviews: list[dict[str, Any]] = []
        self._review_gate: Any = None

    # -- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        """Restore the session, read the server summary, pick a daemon."""
        if self._continue:
            record = self.sessions.latest()
            if record is not None:
                self._restore_session(record, resumed=True)
        if self.session is None:
            self.session = self.sessions.new()
            self._splash()
        self.status.session = self.session.short_id
        await self._refresh_overview()
        await self._refresh_daemons()
        # Recent tickets paint Home. They must not block startup: /jobs tests
        # pause the same tickets() call and only release it after /new.
        recent = asyncio.get_running_loop().create_task(
            self._refresh_recent_tickets(epoch=self._epoch)
        )
        self._tasks.add(recent)
        recent.add_done_callback(self._tasks.discard)
        self._save_session()
        self._invalidate()

    def _splash(self) -> None:
        # Home is painted live from the current width. A boxed welcome stored
        # in the transcript breaks the next time the terminal is narrower.
        return

    def _restore_session(self, record: SessionRecord, *, resumed: bool) -> None:
        self._advance_progress_generation()
        self.session = record
        self.conversation = Conversation.from_json(record.transcript)
        self._progress_block = next(
            (
                block
                for block in reversed(self.conversation.blocks)
                if isinstance(block, ProgressBlock)
            ),
            None,
        )
        self._workspace_id = record.workspace_id
        self._daemon_id = record.daemon_id
        self._daemon_label = record.daemon_label
        self.status.session = record.short_id
        self.status.daemon = (
            self._daemon_label
            or (self._daemon_id[:8] if self._daemon_id else "none")
        )
        self.status.daemon_state = ""
        self.status.stream = "idle"
        self.status.phase = self._progress_block.phase if self._progress_block else "idle"
        self.status.summary = ""
        self.status.question = ""
        self.status.error = ""
        self._progress_source = ""
        self._observed_status = None
        self.status.started_monotonic = None
        self.status.finished_monotonic = None
        self.status.router_model = record.router_model or "unknown"
        if resumed:
            target = (
                record.last_ticket_id[:8]
                if record.last_ticket_id
                else _notice("none", self._locale())
            )
            self.conversation.add(
                NoticeBlock(
                    text=_notice("resumedSessionTicket", self._locale()).format(
                        id=record.short_id,
                        ticket=target,
                        count=len(self.conversation.blocks),
                    )
                )
            )

    def _save_session(self) -> None:
        if self.session is None:
            return
        self.session.transcript = self.conversation.to_json()
        self.session.workspace_id = self._workspace_id
        self.session.daemon_id = self._daemon_id
        self.session.daemon_label = self._daemon_label
        self.session.router_model = self.status.router_model
        try:
            self.sessions.save(self.session)
        except OSError:
            # A read-only home directory must not break the TUI itself.
            pass

    async def close(self) -> None:
        self._epoch += 1
        if self._pending is not None:
            try:
                self.resolve_permission("d")
            except RuntimeError:
                self._pending = None
        for task in list(self._tasks):
            if task is not asyncio.current_task():
                task.cancel()
        if self._watch_task is not None and not self._watch_task.done():
            self._watch_task.cancel()
            self._watch_task = None
        self._save_session()
        await self.client.close()

    # -- status/data refresh ----------------------------------------------

    async def _refresh_overview(self, *, epoch: int | None = None) -> None:
        epoch = self._epoch if epoch is None else epoch
        try:
            overview = await self.client.overview()
            if not isinstance(overview, dict):
                overview = {}
        except SuperAgentError as exc:
            if epoch == self._epoch:
                self.conversation.add(
                    NoticeBlock(
                        text=_notice("serverSummaryUnavailable", self._locale()).format(
                            error=exc
                        )
                    )
                )
            return
        if epoch != self._epoch:
            return
        counts = overview.get("counts")
        if not isinstance(counts, dict):
            counts = {}
        running = _as_int(counts.get("running"))
        queued = _as_int(counts.get("queued"))
        self.status.active_jobs = running + queued
        self.status.attention = _as_int(counts.get("attention"))
        router_model = str(overview.get("router_model") or "").strip()
        if router_model:
            self.status.router_model = router_model
            if self.session is not None:
                self.session.router_model = router_model

    async def _refresh_daemons(self, *, epoch: int | None = None) -> None:
        epoch = self._epoch if epoch is None else epoch
        failed = False
        try:
            response = await self.client.daemons()
            daemons = (
                response.get("daemons", []) if isinstance(response, dict) else []
            )
            if not isinstance(daemons, list):
                daemons = []
        except SuperAgentError:
            daemons = []
            failed = True
        if epoch != self._epoch:
            return
        daemons = [row for row in daemons if isinstance(row, dict)]
        if not daemons:
            self.status.daemon = "no daemon"
            self._online_workers = 0
            if not failed:
                # A successful empty listing means the previously selected
                # daemon is gone; do not submit to a stale id.
                self._daemon_id = ""
                self._daemon_label = ""
            return
        now = time.time()
        live: list[dict[str, Any]] = []
        for row in daemons:
            seen = _parse_iso(str(row.get("last_seen_at") or ""))
            if seen is not None and now - seen <= 180:
                live.append(row)
        pool = live or daemons
        if not pool:
            self.status.daemon = "no daemon"
            self._online_workers = 0
            return
        active = sorted(pool, key=lambda row: str(row.get("last_seen_at") or ""), reverse=True)
        known = {str(row.get("daemon_id") or "") for row in active}
        if self._daemon_id and self._daemon_id not in known:
            self.conversation.add(
                NoticeBlock(
                    text=_notice("daemonGone", self._locale()).format(
                        id=self._daemon_id[:8]
                    )
                )
            )
            self._daemon_id = ""
            self._daemon_label = ""
        picked = active[0]
        if not self._daemon_id:
            self._daemon_id = str(picked.get("daemon_id") or "")
            self._daemon_label = str(picked.get("name") or self._daemon_id[:8])
        self.status.daemon = self._daemon_label or self._daemon_id[:8] or "daemon"
        self.status.daemon_state = "online" if live else "stale"
        self._online_workers = len(live)

    async def _workspace(self, *, epoch: int | None = None) -> str:
        epoch = self._epoch if epoch is None else epoch
        if not self._workspace_id:
            workspace = await self.client.home_workspace()
            if epoch != self._epoch or not isinstance(workspace, str):
                return ""
            self._workspace_id = workspace
        return self._workspace_id

    # -- application plumbing ---------------------------------------------

    def _transcript_width(self) -> int:
        """The cells a transcript row has: the terminal less the scrollbar.

        The transcript window always carries a one-column scrollbar margin,
        so a screen rendered at the full terminal width would wrap its widest
        row onto a second line.
        """
        return max(1, self._columns() - 1)

    def _view_width(self) -> int:
        try:
            columns = shutil.get_terminal_size((100, 24)).columns
        except OSError:
            columns = 100
        return max(40, columns - 4)

    def _invalidate(self) -> None:
        if self._app is not None:
            self._app.invalidate()

    def _active_theme(self) -> Any:
        if self._theme is None:
            self._theme = resolve_app_theme()
        return self._theme

    def _locale(self) -> str:
        return resolve_locale()

    def _home_row_style(self, line: str) -> str:
        """One ink for a rendered Home row, chosen by the state it carries."""
        text = line.strip()
        if text.startswith(("Conductor", "命令", "描述")):
            return "class:tui-heading" if text.startswith("Conductor") else "class:tui-meta"
        glyphs = (
            (status_glyph("needs-you"), "class:tui-question"),
            (status_glyph("running"), "class:tui-step-run"),
            (status_glyph("queued"), "class:tui-meta"),
            (status_glyph("blocked"), "class:tui-question"),
            (status_glyph("failed"), "class:tui-error"),
            (status_glyph("completed"), "class:tui-step-done"),
            (status_glyph("archived"), "class:tui-meta"),
        )
        for glyph, style in glyphs:
            if glyph and text.startswith(glyph):
                return style
        return "class:tui-meta"

    def _glyph(self, name: str, fallback: str) -> str:
        return _lookup_glyph(self._active_theme(), name, fallback)

    def _reduced_motion(self) -> bool:
        if self._reduced_motion_override is not None:
            return bool(self._reduced_motion_override)
        return reduced_motion_enabled()

    def _refresh_interval(self) -> float:
        # A spinner tick is 100 ms. Reduced motion repaints once a second.
        return 1.0 if self._reduced_motion() else 0.5

    def _columns(self) -> int:
        app = self._app
        if app is not None:
            try:
                return max(1, int(app.output.get_size().columns))
            except Exception:
                pass
        return max(1, self._view_width() + 4)

    def _copy_rows(self, rows: list[Any]) -> list[Any]:
        copied: list[Any] = []
        for row in rows:
            if isinstance(row, list):
                copied.append(list(row))
            else:
                copied.append(row)
        return copied

    def _plain_span_lines(self, text: str) -> list[Any]:
        lines = str(text or "").splitlines() or [""]
        return [("class:tui-agent", line) for line in lines]

    def _render_markdown_lines(self, text: str, *, fold_tables: bool = True) -> list[Any]:
        cleaned = _clean_text(text).strip()
        if not cleaned:
            return []
        width = max(20, self._columns() - 2)
        theme = self._active_theme()
        theme_name = getattr(theme, "name", "") if theme is not None else ""
        key = (cleaned, width, str(theme_name), bool(fold_tables))
        cached = self._markdown_cache.get(key)
        if cached is not None:
            return self._copy_rows(cached)
        try:
            from .tui_render import render_markdown

            try:
                rendered = render_markdown(cleaned, width, theme, fold_tables=fold_tables)
            except TypeError:
                rendered = render_markdown(cleaned, width)
            lines: list[Any] = list(rendered) if rendered else []
            if not lines:
                lines = self._plain_span_lines(_crude_markdown(cleaned) or cleaned)
        except ImportError:
            lines = self._plain_span_lines(_crude_markdown(cleaned) or cleaned)
        except Exception:
            lines = self._plain_span_lines(_crude_markdown(cleaned) or cleaned)
        if len(self._markdown_cache) > 2048:
            self._markdown_cache.clear()
        self._markdown_cache[key] = self._copy_rows(lines)
        return self._copy_rows(lines)

    def _markdown_or_literal(self, text: str, *, fold_tables: bool = True) -> list[Any]:
        cleaned = _clean_text(text).strip()
        if not cleaned:
            return []
        rendered = self._render_markdown_lines(cleaned, fold_tables=fold_tables)
        flat = "\n".join(row_text(row) for row in rendered).strip()
        if flat == cleaned or flat == plain(cleaned):
            return [("class:tui-agent", plain(cleaned))]
        return _collapse_blank_rows(rendered)

    def _show_home(self, blocks: list[Any]) -> bool:
        return not any(isinstance(block, (UserBlock, ProgressBlock, EndBlock)) for block in blocks)

    def _home_rows(self) -> list[Any]:
        """Home through the terminal-native renderer (S05, SPEC §7.1).

        The renderer is pure: it decides the table, the stacking and the state
        words at the current width, while this method supplies the snapshot
        the digit indexes bind to and one prompt_toolkit ink per row.
        """
        # A redraw paints the last list. It does not replace that list.
        self._shown_tickets = self._listed_tickets()
        lines = home_rows(
            self._shown_tickets,
            _as_int(getattr(self.status, "attention", 0)),
            self._active_theme(),
            self._locale(),
            self._transcript_width(),
        )
        return [(self._home_row_style(line), line) for line in lines]

    def _output_open(self, block: ToolBlock) -> bool:
        return bool(block.expanded)

    def _toggle_newest_output(self) -> None:
        tools = [
            block
            for block in self.conversation.blocks
            if isinstance(block, ToolBlock)
        ]
        ends = [
            block
            for block in self.conversation.blocks
            if isinstance(block, EndBlock)
        ]
        if tools:
            newest = tools[-1]
            newest.expanded = not newest.expanded
        if ends:
            card = ends[-1]
            card.expanded = not card.expanded
        if tools or ends:
            self._invalidate()

    def _is_worker_detail(self, block: Any) -> bool:
        # Expanded only changes how many output lines a visible tool draws.
        # A restored session omits ``expanded`` and loads it as open, and
        # those worker tools still stay out of the Supervisor view.
        return isinstance(block, ActivityBlock) or (
            isinstance(block, (AssistantBlock, ToolBlock, NoticeBlock))
            and bool(getattr(block, "worker_detail", False))
        )

    def _is_stored_panel(self, block: AssistantBlock) -> bool:
        if block.run_id or block.epoch or block.worker_detail:
            return False
        text = str(block.text or "").lstrip()
        return text.startswith(("╭", "┌"))

    def _linked_progress(self, blocks: list[Any], index: int) -> ProgressBlock | None:
        for earlier in reversed(blocks[:index]):
            if isinstance(earlier, EndBlock):
                return None
            if isinstance(earlier, ProgressBlock):
                return earlier
        return None

    def _following_end(self, blocks: list[Any], index: int) -> EndBlock | None:
        for later in blocks[index + 1 :]:
            if isinstance(later, ProgressBlock):
                return None
            if isinstance(later, EndBlock):
                return later
        return None

    def _step_rows(self, steps: object) -> list[tuple[str, str]]:
        if not isinstance(steps, list):
            return []
        rows: list[tuple[str, str]] = []
        for step in steps:
            state = ""
            if isinstance(step, dict):
                label = (
                    step.get("title")
                    or step.get("name")
                    or step.get("summary")
                    or step.get("text")
                    or ""
                )
                state = str(step.get("status") or step.get("state") or "").lower()
            else:
                label = step
            text = _one_line(label)
            if not text:
                continue
            if state in {"done", "completed", "complete", "ok", "success"}:
                rows.append(("class:tui-step-done", f"✔ {text}"))
            elif state in {"running", "in_progress", "active", "current"}:
                rows.append(("class:tui-step-run", f"♫ {text}"))
            else:
                rows.append(("class:tui-meta", f"○ {text}"))
        return rows

    def _detail_rows(
        self,
        phase: str,
        summary: str,
        question: str,
        error: str,
        *,
        body: str = "",
        fold_tables: bool = True,
    ) -> list[tuple[str, str]]:
        shown_summary, shown_question, shown_error = visible_progress_fields(
            phase, summary, question, error
        )
        rows: list[tuple[str, str]] = []
        summary_text = _clean_text(shown_summary).strip()
        body_text = _clean_text(body).strip()
        terminal = _canonical_phase(phase) in _TERMINAL_PHASES
        # A terminal body is the result. The summary is only the fallback
        # when that body is empty. Repetition inside the body stays.
        show_body = bool(terminal and body_text)
        show_summary = bool(summary_text) and not show_body
        if show_summary:
            rendered = self._markdown_or_literal(summary_text, fold_tables=fold_tables)
            literal = (
                len(rendered) == 1 and row_text(rendered[0]) == plain(summary_text)
            )
            if not body_text and literal:
                rows.append(("class:tui-agent", f"summary: {plain(summary_text)}"))
            else:
                rows.extend(rendered)
        if show_body:
            rendered = self._markdown_or_literal(body_text, fold_tables=fold_tables)
            rows.extend(rendered)
        if shown_error:
            rows.append(("class:tui-error", f"error: {plain(shown_error)}"))
        if shown_question:
            stem, options = _question_parts(shown_question)
            if stem and not options:
                rows.append(("class:tui-question", f"needs you: {plain(stem)}"))
            elif stem:
                rows.append(("class:tui-question", plain(stem)))
            for index, option in enumerate(options, 1):
                cleaned = plain(option)
                if _NUMBERED_OPTION.match(cleaned):
                    shown = cleaned
                else:
                    shown = f"{index}. {cleaned}"
                rows.append(("class:tui-question", f"  {shown}"))
        return rows

    def _card_title(
        self,
        phase: str,
        *,
        elapsed: str,
        title: str = "",
        worker: str = "",
        branch: str = "",
        short: str = "",
        live: bool = False,
    ) -> tuple[str, str]:
        canonical = _canonical_phase(phase)
        glyph = _phase_glyph(canonical, self._active_theme())
        if canonical in {"queued", "running"} and not self._reduced_motion():
            frame = _spinner_frames(self._active_theme())
            tick = int(self._monotonic() * 10)
            glyph = frame[tick % len(frame)] if frame else glyph
        # A finished card keeps the identity it stored. Only the open card
        # may fall back to the status bar, and only for fields it lacks.
        label = _one_line(title)
        if label:
            from .tui_render import _shorten_paths

            label = _one_line(_shorten_paths(label))
        if not label:
            label = {
                "queued": "Queued",
                "running": "Running",
                "waiting": "Needs you",
                "completed": "Done",
                "failed": "Failed",
                "cancelled": "Cancelled",
            }.get(canonical, canonical)
        pieces = [f"{glyph} {label}".strip()]
        worker_text = _one_line(worker)
        branch_text = _one_line(branch)
        short_text = _one_line(short)
        # The worker replaces the daemon. With no worker yet, the daemon
        # stays on this one line so a newline in the label cannot open a row.
        if worker_text and worker_text not in {"no daemon", "none"}:
            pieces.append(worker_text)
        elif live and canonical in {"queued", "running"}:
            daemon = _one_line(self.status.daemon)
            if daemon and daemon not in {"no daemon", "none"}:
                pieces.append(daemon)
        if branch_text:
            pieces.append(branch_text)
        generic = {"Queued", "Running", "Needs you", "Done", "Failed", "Cancelled"}
        if short_text and (worker_text or label not in generic):
            pieces.append(short_text)
        if elapsed:
            pieces.append(elapsed)
        text = " · ".join(piece for piece in pieces if piece)
        style = {
            "waiting": "class:tui-question-title",
            "failed": "class:tui-error-title",
            "completed": "class:tui-result-title",
            "running": "class:tui-tempo",
            "queued": "class:tui-tempo",
        }.get(canonical, "class:tui-meta")
        return style, text

    def _activity_row(self, kind: str, summary: str) -> tuple[str, str] | None:
        text = _one_line(summary)
        if not text or kind in {"created", "closed", "progress"}:
            return None
        if kind == "route":
            return ("class:tui-conductor", f"♩ {text}")
        return ("class:tui-agent", f"▸ {text}")

    def _tool_activity_rows(self, summary: str) -> list[tuple[str, str]]:
        """▸/╰ for one stream tool step. Worker output blocks stay hidden."""
        text = _one_line(summary)
        if not text:
            return []
        voice = self._glyph("voice", "▸")
        collapsed = self._glyph("collapsed", "╰")
        head, _, tail = text.partition(" ")
        rows = [("class:tui-tool", f"{voice} {head}")]
        if tail:
            rows.append(("class:tui-tool-output", f"{collapsed} {tail}"))
        return rows

    def _with_rail(self, rows: list[Any]) -> list[Any]:
        """Hold-coloured rail. The body stays in its own weight."""
        if len(rows) < 2:
            return rows
        out: list[Any] = [rows[0]]
        for row in rows[1:]:
            fragments = _row_fragments(row)
            if fragments and fragments[0][1].startswith("│"):
                out.append(row)
            else:
                out.append([("class:tui-rail", "│ "), *fragments])
        return out

    def _present_progress(self, block: ProgressBlock, *, now: float) -> list[Any]:
        elapsed = format_duration(block.elapsed(now))
        style, title = self._card_title(
            block.phase,
            elapsed=elapsed,
            title=str(getattr(block, "ticket_title", "") or ""),
            worker=str(getattr(block, "worker", "") or ""),
            branch=str(getattr(block, "branch", "") or ""),
            short=str(getattr(block, "ticket_short", "") or ""),
            live=block is self._progress_block,
        )
        rows: list[Any] = [(style, title)]
        stage = _one_line(getattr(block, "stage", ""))
        if (
            _canonical_phase(block.phase) not in _TERMINAL_PHASES
            and stage
            and not stage.startswith("Ticket ")
        ):
            rows.append(("class:tui-agent", stage))
        if block.activity_kind and not _report_repeats(
            block.activity_kind,
            block.activity_summary,
            block.summary,
            str(getattr(block, "body", "") or ""),
        ):
            activity = self._activity_row(block.activity_kind, block.activity_summary)
            if activity is not None:
                rows.append(activity)
        rows.extend(self._step_rows(getattr(block, "steps", ())))
        if _canonical_phase(block.phase) not in _TERMINAL_PHASES:
            rows.extend(self._tool_activity_rows(str(getattr(block, "tool_line", "") or "")))
        rows.extend(
            self._detail_rows(
                block.phase,
                block.summary,
                block.question,
                block.error,
                body=str(getattr(block, "body", "") or ""),
                fold_tables=not bool(getattr(block, "expanded", False)),
            )
        )
        if _canonical_phase(block.phase) == "waiting":
            return self._with_rail(rows)
        return rows

    def _present_end(
        self,
        block: EndBlock,
        progress: ProgressBlock | None,
        *,
        now: float,
        fold_tables: bool | None = None,
    ) -> list[tuple[str, str]]:
        phase = block.status
        elapsed = ""
        stage = ""
        activity_kind = ""
        activity_summary = ""
        if progress is not None:
            # The end frame's own status decides which lines are visible.
            # A running progress card must not hide a waiting question.
            elapsed = format_duration(progress.elapsed(now))
            stage = progress.stage
            activity_kind = progress.activity_kind
            activity_summary = progress.activity_summary
        if not phase and progress is not None:
            phase = progress.phase
        style, title = self._card_title(
            phase,
            elapsed=elapsed,
            title=str(getattr(block, "ticket_title", "") or ""),
            worker=str(getattr(block, "worker", "") or ""),
            branch=str(getattr(block, "branch", "") or ""),
            short=str(getattr(block, "ticket_short", "") or ""),
        )
        rows: list[Any] = [(style, title)]
        # A finished card does not repeat its stage or status. The title line
        # already carries the outcome, the worker and the elapsed time.
        terminal = _canonical_phase(phase) in _TERMINAL_PHASES
        if not terminal and stage and not _one_line(stage).startswith("Ticket "):
            rows.append(("class:tui-agent", _one_line(stage)))
        if activity_kind and not _report_repeats(
            activity_kind,
            activity_summary,
            block.summary,
            str(getattr(block, "body", "") or ""),
        ):
            activity = self._activity_row(activity_kind, activity_summary)
            if activity is not None:
                rows.append(activity)
        rows.extend(self._step_rows(getattr(block, "steps", ())))
        if fold_tables is None:
            fold_tables = not bool(getattr(block, "expanded", False))
        rows.extend(
            self._detail_rows(
                phase,
                block.summary,
                block.question,
                block.error,
                body=str(getattr(block, "body", "") or ""),
                fold_tables=fold_tables,
            )
        )
        if _canonical_phase(phase) == "waiting":
            return self._with_rail(rows)
        return rows

    def _present_tool(self, block: ToolBlock) -> list[tuple[str, str]]:
        status = block.status if block.duration is None else (
            f"{block.status} {format_duration(block.duration)}"
        )
        voice = self._glyph("voice", "▸")
        collapsed = self._glyph("collapsed", "╰")
        rows = [("class:tui-tool", f"{voice} {plain(block.name)} [{plain(status)}]")]
        lines = list(block.lines)
        if self._output_open(block):
            shown = lines
        else:
            shown = lines[:3]
        for line in shown:
            rows.append(("class:tui-tool-output", f"{collapsed} {plain(line)}"))
        hidden = len(lines) - len(shown)
        if hidden > 0:
            rows.append(
                (
                    "class:tui-meta",
                    f"… {hidden} more lines (ctrl+o to expand)",
                )
            )
        return rows

    def _present_assistant(self, block: AssistantBlock) -> list[tuple[str, str]]:
        if self._is_stored_panel(block):
            return []
        rows: list[tuple[str, str]] = []
        conductor = self._glyph("conductor", "♩")
        voice = self._glyph("voice", "▸")
        if block.agent and not block.reasoning:
            rows.append(("class:tui-subagent", f"{voice} sub-agent {plain(block.agent)}"))
        prefix = f"{conductor} " if not block.worker_detail and not block.reasoning else ""
        if block.worker_detail and not block.reasoning:
            prefix = f"{voice} "
        style = "class:tui-reasoning" if block.reasoning else (
            "class:tui-conductor" if prefix.startswith(conductor) else "class:tui-agent"
        )
        body = _clean_text(block.text)
        first = True
        for piece_kind, value in split_text_and_diffs(plain(body)):
            if piece_kind == "diff":
                for line in value:
                    diff_style = {
                        "add": "class:tui-diff-add",
                        "del": "class:tui-diff-del",
                        "hunk": "class:tui-diff-hunk",
                        "header": "class:tui-diff-header",
                    }.get(line.marker, "class:tui-diff-context")
                    rows.append((diff_style, line.text))
                first = False
            else:
                if _MARKDOWN_RE.search(value):
                    lines: list[Any] = self._markdown_or_literal(value)
                else:
                    # A streamed chunk ends in a newline. That is not a blank row.
                    lines = value.rstrip("\n").split("\n")
                for line in lines:
                    mark = prefix if first else ""
                    first = False
                    if isinstance(line, str):
                        rows.append((style, f"{mark}{line}"))
                        continue
                    fragments = _row_fragments(line)
                    if mark and fragments:
                        head_style, head_text = fragments[0]
                        fragments = [(style, mark), (head_style, head_text), *fragments[1:]]
                    elif mark:
                        fragments = [(style, mark)]
                    if len(fragments) == 1:
                        rows.append(fragments[0])
                    else:
                        rows.append(fragments)
        if block.sealed:
            rows.append(("class:tui-meta", SEALED_BOUNDARY))
        return rows

    def _present_block(
        self,
        block: Any,
        blocks: list[Any],
        index: int,
        *,
        now: float,
    ) -> list[tuple[str, str]]:
        if isinstance(block, UserBlock):
            prompt = self._glyph("prompt", "❯")
            rows = [("class:tui-user", f"{prompt} {plain(block.text)}")]
            for path in block.attachments:
                rows.append(("class:tui-meta", f"  attached: {plain(path)}"))
            return rows
        if isinstance(block, AssistantBlock):
            return self._present_assistant(block)
        if isinstance(block, ActivityBlock):
            tail = f" — {plain(block.detail)}" if block.detail else ""
            voice = self._glyph("voice", "▸")
            return [(
                "class:tui-meta",
                f"{voice} {plain(block.event_kind)} {plain(block.summary)}{tail}",
            )]
        if isinstance(block, ToolBlock):
            return self._present_tool(block)
        if isinstance(block, ErrorBlock):
            failed = self._glyph("failed", "✖")
            return [("class:tui-error", f"{failed} error: {plain(block.text)}")]
        if isinstance(block, NoticeBlock):
            parts = plain(block.text).splitlines() or [""]
            if len(parts) == 1:
                return [("class:tui-meta", f"· {parts[0]}")]
            return [("class:tui-meta", part) for part in parts]
        if isinstance(block, ProgressBlock):
            if self._following_end(blocks, index) is not None:
                return []
            return self._present_progress(block, now=now)
        if isinstance(block, EndBlock):
            return self._present_end(
                block, self._linked_progress(blocks, index), now=now
            )
        if isinstance(block, DispatchBlock):
            rows = []
            for line in block.lines:
                if not str(line).strip():
                    continue
                rows.append(("class:tui-meta", plain(_relabel_stage(str(line)))))
            return rows
        if isinstance(block, PermissionBlock):
            request = PermissionRequest(
                action=block.action,
                label=block.label,
                detail=block.detail,
            )
            rendered = render_permission_prompt(
                request, self._transcript_width(), self._locale()
            )
            # The last row is the status-bar takeover; the real status bar
            # paints that one, so the card keeps only the card rows.
            card = rendered[:-1]
            rows = [("class:tui-warn", line) for line in card]
            if block.decision != "pending":
                rows.append(
                    (
                        "class:tui-meta",
                        f"  {_notice('decision', self._locale())}: "
                        f"{plain(block.decision)}",
                    )
                )
            return rows
        return []

    def _row_cache_key(self, block: Any) -> tuple[Any, ...] | None:
        """Stable blocks reuse rows. A running card's title tracks the clock."""
        if isinstance(block, (ProgressBlock, EndBlock)):
            return None
        version = getattr(block, "version", None)
        if isinstance(block, PermissionBlock):
            version = (getattr(block, "decision", ""), getattr(block, "label", ""))
        elif isinstance(block, ToolBlock):
            version = (
                int(version) if isinstance(version, int) else 0,
                1 if self._output_open(block) else 0,
                tuple(block.lines),
                block.duration,
            )
        elif not isinstance(version, int):
            version = 0
        theme = self._active_theme()
        theme_name = getattr(theme, "name", "") if theme is not None else ""
        return (
            id(block),
            _cache_content(block),
            version,
            self._columns(),
            str(theme_name),
            bool(self.show_worker_details),
        )

    def present_transcript(self) -> list[tuple[str, str]]:
        """One visual pass: glyphs, one result, markdown, no protocol trailers."""
        now = self._clock()
        blocks = [
            block
            for block in self.conversation.blocks
            if self.show_worker_details or not self._is_worker_detail(block)
        ]
        rows: list[tuple[str, str]] = []
        if self._show_home(blocks):
            rows.extend(self._home_rows())
        previous: Any = None
        seen: set[tuple[Any, ...]] = set()
        for index, block in enumerate(blocks):
            key = self._row_cache_key(block)
            rendered: list[tuple[str, str]] | None = None
            if key is not None:
                seen.add(key)
                rendered = self._row_cache.get(key)
            if rendered is None:
                rendered = self._present_block(block, blocks, index, now=now)
                if key is not None:
                    self._row_cache[key] = rendered
            if not rendered:
                continue
            packed = (
                self.show_worker_details
                and isinstance(previous, (ToolBlock, ActivityBlock, AssistantBlock))
                and isinstance(block, (ToolBlock, ActivityBlock, AssistantBlock))
            )
            if rows and not packed and not (
                isinstance(previous, NoticeBlock) and isinstance(block, NoticeBlock)
            ):
                rows.append(("", ""))
            rows.extend(rendered)
            previous = block
        if len(self._row_cache) > len(seen) + 64:
            self._row_cache = {
                key: self._row_cache[key] for key in seen if key in self._row_cache
            }
        return rows

    def _conversation_fragments(self) -> FormattedText:
        """Conversation rows with explicit newlines and a cursor placement hint.

        FormattedTextControl does not put newlines between fragments, and its
        cursor defaults to row 0, which makes Window scroll back to the top on
        every repaint. The ``[SetCursorPosition]`` marker on the row the view
        should show keeps the conversation scrolled where the operator left it
        (or at the tail while following).
        """
        rows = self.present_transcript()
        target = len(rows) - 1 if self._follow_tail else self._scroll_offset
        target = max(0, min(target, max(0, len(rows) - 1)))
        fragments: list[tuple[str, str]] = []
        for index, row in enumerate(rows):
            fragments.extend(_row_fragments(row))
            if index == target:
                fragments.append(("[SetCursorPosition]", ""))
            fragments.append(("", "\n"))
        if not rows:
            fragments.append(("[SetCursorPosition]", ""))
        return FormattedText(fragments)

    def _conversation_rows_for_control(self) -> tuple[list[tuple[str, str]], int]:
        """Logical rows plus the visual row the viewport should show."""
        requested = 10**9 if self._follow_tail else self._scroll_offset
        return self.present_transcript(), requested

    def _follow_end(self) -> None:
        """Keep the view at the tail after new blocks, unless scrolled up."""
        if self._follow_tail:
            self._scroll_offset = 10**9

    def _header_fragments(self) -> FormattedText:
        """One-line identity/context bar above the transcript."""
        stream = self.status.stream or "idle"
        live = stream in {"live", "polling", "reconnecting"}
        dot = "●" if live else "○"
        state_style = "class:header-live" if live else "class:header-idle"
        phase = self.status.phase or "idle"
        elapsed: float | None = None
        if self._progress_block is not None:
            elapsed = self._progress_block.elapsed(self._clock())
        elif self.status.started_monotonic is not None:
            end = (
                self.status.finished_monotonic
                if self.status.finished_monotonic is not None
                else self._monotonic()
            )
            elapsed = max(0.0, end - self.status.started_monotonic)
        if elapsed is not None:
            phase = f"{phase} {format_duration(elapsed)}"
        meta = (
            f"  {plain(self.status.router_model)}  ·  "
            f"session {plain(self.status.session or 'new')}  ·  "
            f"{plain(phase)}"
        )
        # Quiet mark for the Supervisor view only. Worker details keep the
        # previous header; the mark never carries transcript or tool text.
        if not self.show_worker_details:
            mark = _PROGRESS_SOURCE_MARK.get(self._progress_source, "")
            if mark:
                meta += f"  ·  {mark}"
        meta += "  ·  "
        return FormattedText(
            [
                ("class:header-brand", " Conductor "),
                ("class:header-title", "Super-agent"),
                ("class:header-meta", meta),
                (state_style, f"{dot} {plain(stream)} "),
            ]
        )

    def _status_parts(self) -> tuple[list[str], list[str]]:
        """Counts first, hints last, so a narrow line drops hints before counts."""
        locale = self._locale()
        online = self._glyph("online", "●")
        needs = self._glyph("needs_you", "◆")
        attention = _as_int(getattr(self.status, "attention", 0))
        phase = _canonical_phase(self.status.phase)
        watching = self._watch_task is not None and not self._watch_task.done()
        idle = phase in {"", "idle"} and not watching
        terminal = phase in {"completed", "failed", "cancelled"}
        # Order: ◆ N, this phase's Esc hint, shift+enter, alt+m. A narrow line
        # drops hints from the end. A finished card has no interrupt hint.
        hints: list[str] = []
        if attention > 0:
            hints.append(f"{needs} {attention}")
        if self._pending is not None:
            hints.extend(
                [
                    f"a {t(_TUI_STRINGS, 'hint.allow', locale)}",
                    f"s {t(_TUI_STRINGS, 'hint.session', locale)}",
                    f"d {t(_TUI_STRINGS, 'hint.deny', locale)}",
                ]
            )
        elif self._palette_open:
            hints.extend(
                [
                    t(_TUI_STRINGS, "hint.tab", locale),
                    t(_TUI_STRINGS, "hint.ctrlDown", locale),
                    t(_TUI_STRINGS, "hint.escClose", locale),
                    t(_TUI_STRINGS, "hint.altNav", locale),
                ]
            )
        elif self._reply_active() or phase in {"waiting", "blocked"}:
            hints.append(f"esc {t(_TUI_STRINGS, 'hint.later', locale)}")
        elif not idle and not terminal:
            hints.append(f"esc {t(_TUI_STRINGS, 'hint.interrupt', locale)}")
        if not self._palette_open:
            hints.append(f"shift+enter {t(_TUI_STRINGS, 'hint.newline', locale)}")
            hints.append(f"alt+m {t(_TUI_STRINGS, 'hint.mode', locale)}")
        if idle and not self._palette_open:
            hints.append(f"? {t(_TUI_STRINGS, 'hint.help', locale)}")
        if (self.status.stream or "") == "reconnecting":
            presence = t(_TUI_STRINGS, "presence.reconnecting", locale)
        else:
            presence = (
                f"{online} {self._online_workers} "
                f"{t(_TUI_STRINGS, 'presence.online', locale)}"
            )
        mode_key = f"mode.{self.interaction_mode}"
        mode_text = _TUI_STRINGS.get(locale, {}).get(
            mode_key,
            _TUI_STRINGS["en"].get(mode_key, self.interaction_mode),
        )
        counts = [mode_text, presence]
        return [plain(piece) for piece in counts], [plain(piece) for piece in hints]

    def _status_segments(self) -> list[str]:
        counts, hints = self._status_parts()
        return [*counts, *hints]

    def _status_fragments(self) -> FormattedText:
        # The identity line is not a row of its own. Calling it keeps a redraw
        # heartbeat that watches `_header_fragments` counting frames.
        self._header_fragments()
        if self._pending is not None:
            request = self._pending[0]
            # S09 owns the takeover row; the transcript card drops it so the
            # real status bar is the one that paints it.
            rows = render_permission_prompt(
                request, self._columns(), self._locale()
            )
            text = rows[-1] if rows else f"{self._glyph('needs_you', '◆')} {plain(request.render())}"
            return FormattedText([("class:permission", text)])
        counts, hints = self._status_parts()
        text = align_status_line(counts, hints, self._columns())
        fragments: list[tuple[str, str]] = []
        cursor = text
        reconnecting = t(_TUI_STRINGS, "presence.reconnecting", self._locale())
        while cursor:
            if cursor.startswith(reconnecting):
                fragments.append(("class:status-hold", reconnecting))
                cursor = cursor[len(reconnecting) :]
                continue
            needs = self._glyph("needs_you", "◆")
            online = self._glyph("online", "●")
            matched = False
            for glyph, style in ((needs, "class:status-hold"), (online, "class:status-good")):
                if glyph and cursor.startswith(glyph):
                    piece = glyph
                    if style == "class:status-hold":
                        # ◆ N needing you is hold colour, not only the glyph.
                        rest = cursor[len(glyph) :]
                        counted = re.match(r"[ \t]*\d+", rest)
                        if counted:
                            piece += counted.group(0)
                    fragments.append((style, piece))
                    cursor = cursor[len(piece) :]
                    matched = True
                    break
            if matched:
                continue
            positions = [
                cursor.find(mark)
                for mark in (reconnecting, needs, online)
                if mark and cursor.find(mark) > 0
            ]
            cut = min(positions) if positions else len(cursor)
            fragments.append(("class:statusbar", cursor[:cut]))
            cursor = cursor[cut:]
        return FormattedText(fragments)

    def _permission_fragments(self) -> FormattedText:
        if self._pending is None:
            return FormattedText([])
        request = self._pending[0]
        rows = render_permission_prompt(request, self._columns(), self._locale())
        text = rows[-1] if rows else f" {plain(request.render())} "
        return FormattedText([("class:permission", text)])

    def spawn_submit(self, text: str) -> None:
        """Start one composer submission without blocking the UI loop."""
        line = str(text or "").rstrip("\n")
        if not line.strip():
            return
        task = asyncio.get_running_loop().create_task(self.submit(line))
        self._tasks.add(task)
        self._task_epochs[task] = self._epoch
        task.add_done_callback(self._submission_done)

    def _submission_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        epoch = self._task_epochs.pop(task, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is None:
            return
        if epoch is not None and epoch != self._epoch:
            # The submission belonged to a session that was switched away;
            # its failure is not the new session's business.
            return
        self.conversation.add(
            ErrorBlock(text=f"submission failed: {error}")
        )
        self._save_session()
        self._invalidate()

    async def submit(self, text: str) -> None:
        """Route one composer line: shell escape, slash command or prompt."""
        line = str(text or "").strip("\n")
        if not line.strip():
            return
        epoch = self._epoch
        try:
            if line.startswith("!"):
                await self._shell_line(line[1:].strip(), epoch=epoch)
            elif line.startswith("/"):
                await self.handle_command(line)
            elif self._reply_active():
                await self._send_reply(line, epoch=epoch)
            elif self.interaction_mode == "read-only":
                if epoch == self._epoch:
                    self.conversation.add(
                        NoticeBlock(text=_notice("readOnly", self._locale()))
                    )
            else:
                await self._send_prompt(line, epoch=epoch)
        except SuperAgentError as exc:
            if epoch == self._epoch:
                self.conversation.add(ErrorBlock(text=str(exc)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if epoch == self._epoch:
                self.conversation.add(ErrorBlock(text=f"unexpected error: {exc}"))
        finally:
            if epoch == self._epoch:
                self._save_session()
                self._follow_end()
                self._invalidate()

    def _advance_progress_generation(self) -> None:
        """Drop in-flight progress reads for the Ticket view being left."""
        self._progress_generation += 1

    def _start_progress(
        self,
        ticket_id: str,
        phase: str = "queued",
        *,
        started_at: float | None = None,
        started_monotonic: float | None = None,
    ) -> None:
        # Replacing the card is a new view even when the Ticket id is unchanged:
        # a read that started against the previous card must not refill this one.
        self._advance_progress_generation()
        phase = _canonical_phase(str(phase or "queued"))
        started_at = self._clock() if started_at is None else started_at
        started_monotonic = (
            self._monotonic() if started_monotonic is None else started_monotonic
        )
        self.status.phase = phase
        self.status.summary = ""
        self.status.question = ""
        self.status.error = ""
        # The previous Ticket's worker must not land on this card.
        self.status.ticket_title = ""
        self.status.worker = ""
        self.status.branch = ""
        self.status.ticket_short = str(ticket_id or "")[:8]
        self._progress_source = ""
        self._observed_status = None
        self.status.started_monotonic = started_monotonic
        self.status.finished_monotonic = None
        self._reply_dismissed = False
        self._progress_block = ProgressBlock(
            ticket_id=ticket_id,
            phase=phase,
            stage=_PHASE_DESCRIPTIONS.get(phase, f"Ticket {phase}"),
            started_at=started_at,
            ticket_short=self.status.ticket_short,
        )
        self.conversation.add(self._progress_block)

    def _set_progress_phase(self, phase: str, *, authoritative: bool = False) -> None:
        """Set the Supervisor phase.

        ``authoritative=True`` is a ``/progress`` calibration and may move in
        any direction. Otherwise the phase only moves forward.
        """
        canonical = _canonical_phase(str(phase or "idle"))
        if not authoritative and not _phase_moves_forward(self.status.phase, canonical):
            return
        phase = canonical
        if phase == "waiting" and self.status.phase != "waiting":
            self._reply_dismissed = False
        self.status.phase = phase
        if self._progress_block is not None:
            self._progress_block.phase = phase
            self._progress_block.stage = _PHASE_DESCRIPTIONS.get(
                phase, f"Ticket {phase}"
            )
        if phase in _TERMINAL_PHASES:
            if self._progress_block is not None:
                if self._progress_block.finished_at is None:
                    self._progress_block.finished_at = self._clock()
            if self.status.finished_monotonic is None:
                self.status.finished_monotonic = self._monotonic()
        else:
            if self._progress_block is not None:
                self._progress_block.finished_at = None
            self.status.finished_monotonic = None

    async def _send_prompt(self, line: str, *, epoch: int | None = None) -> None:
        epoch = self._epoch if epoch is None else epoch
        prompt, attachments, errors = expand_mentions(line, root=self.local_root)
        if epoch != self._epoch:
            return
        for error in errors:
            self.conversation.add(ErrorBlock(text=error))
        self.conversation.add(
            UserBlock(text=line, attachments=[item.path for item in attachments])
        )
        workspace = await self._workspace(epoch=epoch)
        if epoch != self._epoch:
            return
        if not workspace:
            self.conversation.add(
                ErrorBlock(text="server did not return a usable Home workspace")
            )
            return
        if not self._daemon_id:
            await self._refresh_daemons()
        if epoch != self._epoch:
            return
        if not self._daemon_id:
            self.conversation.add(
                ErrorBlock(
                    text=(
                        "no local daemon is registered; the deployment cannot run "
                        "cloud Workers. Register and start one (conductor daemons)."
                    )
                )
            )
            return
        title = line.splitlines()[0].strip()[:120] if line.strip() else "task"
        submitted_at = self._clock()
        submitted_monotonic = self._monotonic()
        ticket = await self.client.create_task(
            prompt,
            title=title,
            workspace_id=workspace,
            worker_daemon_id=self._daemon_id,
        )
        if epoch != self._epoch:
            return
        if not isinstance(ticket, dict):
            raise SuperAgentError("server returned a malformed Ticket response")
        ticket_id = str(ticket.get("ticket_id") or "")
        if not ticket_id:
            raise SuperAgentError("server did not return a Ticket id")
        self._start_progress(
            ticket_id,
            str(ticket.get("status") or "queued"),
            started_at=submitted_at,
            started_monotonic=submitted_monotonic,
        )
        if self.session is not None:
            self.session.last_ticket_id = ticket_id
        self.status.ticket_short = ticket_id[:8]
        self.status.ticket_title = title
        self._remember_progress_identity(
            title=title, short=ticket_id[:8], ticket_id=ticket_id
        )
        self._save_session()
        if epoch != self._epoch:
            return
        await self._calibrate_progress(ticket_id, epoch=epoch)
        if epoch != self._epoch:
            return
        await self.watch(ticket_id, epoch=epoch)

    async def _send_reply(self, line: str, *, epoch: int | None = None) -> None:
        """Answer the Ticket that is waiting. Does not create a new one."""
        epoch = self._epoch if epoch is None else epoch
        ticket_id = self._current_ticket_id()
        reply = getattr(self.client, "reply", None)
        if reply is None or not ticket_id:
            if epoch == self._epoch:
                self.conversation.add(NoticeBlock(text=_unsupported(self._locale())))
            return
        if epoch != self._epoch:
            return
        self.conversation.add(UserBlock(text=line))
        await reply(ticket_id, line)
        if epoch != self._epoch:
            return
        await self.watch(ticket_id, epoch=epoch)

    def _reply_active(self) -> bool:
        if self._reply_dismissed or self._pending is not None:
            return False
        if _canonical_phase(self.status.phase) != "waiting":
            return False
        return bool(self._current_ticket_id())

    def _dismiss_reply(self) -> None:
        """Esc while waiting: answer later, do not open a new Ticket."""
        self._reply_dismissed = True
        self.conversation.add(NoticeBlock(text=_notice("answerLater", self._locale())))
        self._invalidate()

    def _cycle_mode(self) -> None:
        try:
            index = _MODES.index(self.interaction_mode)
        except ValueError:
            index = 0
        self.interaction_mode = _MODES[(index + 1) % len(_MODES)]
        self._invalidate()

    async def watch(self, ticket_id: str, *, epoch: int | None = None) -> None:
        """Follow one Ticket live, updating the conversation and status bar."""
        epoch = self._epoch if epoch is None else epoch
        if epoch != self._epoch:
            return
        self._interrupted = False
        self.status.stream = "live"
        self.status.active_jobs = max(1, self.status.active_jobs)
        if self._progress_block is None or self._progress_block.ticket_id != ticket_id:
            self._start_progress(ticket_id)
        self._follow_tail = True
        self._invalidate()
        previous = self._watch_task
        if previous is not None and not previous.done():
            previous.cancel()
            self.conversation.add(
                NoticeBlock(text=_notice("switchedWatches", self._locale()))
            )
        # The new attachment replays from its own start: close whatever the
        # earlier one left on screen so the two can never be joined.
        self.conversation.seal_live_output()
        task = asyncio.get_running_loop().create_task(
            self._watch_loop(ticket_id, epoch=epoch)
        )
        self._watch_task = task
        try:
            await task
        finally:
            if self._watch_task is task:
                self._watch_task = None
            if epoch == self._epoch:
                self._follow_tail = True
                self._scroll_offset = 10**9
                self._save_session()
                try:
                    await self._refresh_overview(epoch=epoch)
                    if epoch == self._epoch:
                        await self._refresh_daemons(epoch=epoch)
                except SuperAgentError:
                    # A watch that outlived its session must not fail the task
                    # that started it (the usual helpers catch their own
                    # errors; this keeps injected/future ones harmless).
                    pass
                self._invalidate()

    async def _quiesce(self) -> None:
        """Fence the session and join any in-flight submissions/watch.

        A submit can be awaiting Ticket creation while /new or /resume runs;
        without the epoch fence its late result would be written into the new
        session. The epoch makes every stale continuation a no-op, and pending
        submissions are cancelled and joined here.
        """
        self._epoch += 1
        current = asyncio.current_task()
        stale = [
            task
            for task in self._tasks
            if task is not current and not task.done()
        ]
        for task in stale:
            task.cancel()
        await self._stop_watch()
        if stale:
            await asyncio.gather(*stale, return_exceptions=True)

    async def _stop_watch(self) -> None:
        """Cancel and join the in-flight watch before the session changes."""
        task = self._watch_task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._watch_task = None
        self.status.stream = "idle"

    async def _watch_loop(self, ticket_id: str, *, epoch: int | None = None) -> None:
        """The cancellable body of :meth:`watch`."""
        epoch = self._epoch if epoch is None else epoch
        try:
            async for event in watch_ticket(
                self.client,
                ticket_id,
                timeout=self._timeout,
                sleep=self._sleep,
                clock=self._monotonic,
                should_stop=lambda: self._interrupted,
            ):
                if epoch != self._epoch:
                    return
                if self._interrupted:
                    break
                reconnected = False
                if isinstance(event, StreamNotice):
                    if "polling" in event.text:
                        self.status.stream = "polling"
                    elif "reconnecting" in event.text:
                        self.status.stream = "reconnecting"
                elif self.status.stream == "reconnecting":
                    # A recovered attach is delivering frames again.
                    self.status.stream = "live"
                    reconnected = True
                fed = self.conversation.feed(
                    event,
                    now=self._monotonic,
                    show_worker_details=self.show_worker_details,
                )
                if isinstance(event, End) and isinstance(fed, EndBlock):
                    self._copy_card_identity(fed, self._progress_block)
                    if ticket_id and not getattr(fed, "unbound", False):
                        fed.ticket_id = ticket_id
                await self._track_progress(
                    event, ticket_id=ticket_id, reconnected=reconnected
                )
                if isinstance(event, TimedOut):
                    self.conversation.add(
                        NoticeBlock(
                            text=event.message
                            or _notice("watchTimedOut", self._locale())
                        )
                    )
                if isinstance(event, (End, TimedOut)):
                    self.status.stream = "idle"
                self._follow_end()
                self._invalidate()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self._watch_task is current:
                self.status.stream = "idle"
            self.conversation.add(
                NoticeBlock(text=_notice("stoppedWatching", self._locale()))
            )
            self._follow_end()
            self._invalidate()
            return

    async def _track_progress(
        self,
        event: Any,
        *,
        ticket_id: str = "",
        reconnected: bool = False,
    ) -> None:
        """Keep the Supervisor header and progress card in sync with the stream.

        ``/progress`` is read once when a Status state changes, when the stream
        ends, or when a dropped SSE attach is delivering frames again. The
        frame that proves the attach is back may be an activity row; that row
        still calibrates once and does not move the phase by itself. Progress
        is not read from the redraw heartbeat.
        """
        ticket_id = ticket_id or (
            self._progress_block.ticket_id if self._progress_block is not None else ""
        )
        if isinstance(event, Activity):
            if event.kind == "route":
                match = _ROUTE_WORKER.search(str(event.summary or ""))
                if match:
                    self.status.worker = match.group(1)
                    self.status.branch = match.group(2)
                    self._remember_progress_identity(
                        worker=match.group(1),
                        branch=match.group(2),
                        ticket_id=ticket_id,
                    )
            if self._progress_block is not None and event.kind == "tool":
                # The step line, not the worker's tool block. Output stays hidden.
                self._progress_block.tool_line = str(event.summary or "")
            if (
                self._progress_block is not None
                and event.kind in _SUPERVISOR_ACTIVITY_KINDS
            ):
                self._progress_block.activity_kind = event.kind
                self._progress_block.activity_summary = event.summary
            if reconnected and ticket_id:
                await self._calibrate_progress(ticket_id)
            self._invalidate()
            return
        needs_calibration = reconnected
        if isinstance(event, Status):
            status = str(event.status or "")
            if status != self._observed_status:
                needs_calibration = True
                self._observed_status = status
        elif isinstance(event, End):
            needs_calibration = True
            self._observed_status = str(event.status or "")
        view_epoch = self._epoch
        view_generation = self._progress_generation
        applied_before = self._progress_applied
        calibrated: bool | None = False
        if needs_calibration and ticket_id:
            calibrated = await self._calibrate_progress(ticket_id)
        # The HTTP sequence decides only whether that response is used.
        # The SSE frame is the Ticket's own event: keep it when this view
        # is still current, unless a newer successful read already replaced it.
        if not self._sse_view_current(
            ticket_id=ticket_id,
            epoch=view_epoch,
            generation=view_generation,
        ):
            return
        if calibrated is True or self._progress_applied != applied_before:
            if isinstance(event, End):
                self.status.stream = "idle"
            self._invalidate()
            return
        if isinstance(event, Status):
            self._set_progress_phase(str(event.status or "idle"))
            self.status.summary = str(event.summary or "")
            self._invalidate()
        elif isinstance(event, End):
            self._set_progress_phase(str(event.status or "idle"))
            self.status.summary = str(event.summary or "")
            self.status.question = str(event.question or "")
            self.status.error = str(event.error or "")
            self.status.stream = "idle"
            # The result card was written from the raw frame before this guard.
            # Put its status back on the phase SSE was allowed to keep.
            self._overwrite_result()
            self._invalidate()

    def _sse_view_current(self, *, ticket_id: str, epoch: int, generation: int) -> bool:
        """True when this SSE frame still belongs to the Ticket on screen.

        A newer HTTP read, including one that 404s or raises, does not retire
        the frame. Switching Ticket, session, or view generation does.
        """
        return (
            epoch == self._epoch
            and generation == self._progress_generation
            and bool(ticket_id)
            and ticket_id == self._current_ticket_id()
        )

    def _progress_read_current(
        self,
        *,
        ticket_id: str,
        epoch: int,
        generation: int,
        sequence: int,
    ) -> bool:
        """True when this read is still the one the Supervisor view is waiting on."""
        return (
            epoch == self._epoch
            and generation == self._progress_generation
            and sequence == self._progress_request
            and ticket_id == self._current_ticket_id()
        )

    async def _calibrate_progress(
        self, ticket_id: str, *, epoch: int | None = None
    ) -> bool | None:
        """Apply one ``/progress`` payload.

        True means this read was current and the payload was applied. False
        means it was current but did not replace the view (no progress method,
        HTTP 404, or a bad body); the caller keeps the SSE-derived fields.
        None means this HTTP body was stale and was not applied. The caller
        may still merge the SSE frame when the view is current and no newer
        successful read has been applied. CancelledError still propagates.

        The sequence is taken before the await. Two reads of the same Ticket
        are ordered by that number; the payload's Ticket id is not enough.
        """
        epoch = self._epoch if epoch is None else epoch
        ticket_id = str(ticket_id or "")
        if epoch != self._epoch:
            return None
        if not ticket_id:
            return False
        fetch = getattr(self.client, "progress", None)
        if fetch is None:
            self._progress_source = _PROGRESS_SOURCE_STREAM
            return False
        self._progress_request += 1
        sequence = self._progress_request
        generation = self._progress_generation
        try:
            payload = await fetch(ticket_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._progress_read_current(
                ticket_id=ticket_id,
                epoch=epoch,
                generation=generation,
                sequence=sequence,
            ):
                return None
            self._progress_source = _PROGRESS_SOURCE_STREAM
            return False
        if not self._progress_read_current(
            ticket_id=ticket_id,
            epoch=epoch,
            generation=generation,
            sequence=sequence,
        ):
            return None
        if not isinstance(payload, dict):
            self._progress_source = _PROGRESS_SOURCE_STREAM
            return False
        reported = str(payload.get("ticket_id") or "")
        if reported and reported != ticket_id:
            self._progress_source = _PROGRESS_SOURCE_STREAM
            return False
        self._apply_progress_payload(payload)
        self._progress_source = _PROGRESS_SOURCE_SERVER
        return True

    def _apply_progress_payload(self, payload: dict[str, Any]) -> None:
        """Copy the calibration fields. Extra keys (transcript, tools) are ignored."""
        reported = str(payload.get("ticket_id") or "")
        block = self._progress_block
        if (
            reported
            and block is not None
            and block.ticket_id
            and reported != block.ticket_id
        ):
            return
        phase = _progress_field(payload.get("phase")).strip()
        if phase:
            self._set_progress_phase(phase, authoritative=True)
        elapsed = _progress_elapsed(payload.get("elapsed_seconds"))
        if elapsed is not None:
            self._anchor_elapsed(elapsed)
        self.status.summary = _progress_field(payload.get("summary"))
        body = _progress_field(payload.get("body"))
        self.status.body = body
        self.status.question = _progress_field(payload.get("question"))
        self.status.error = _progress_field(payload.get("error"))
        steps = payload.get("steps")
        if not isinstance(steps, list):
            steps = []
        worker = _progress_field(payload.get("worker_model") or payload.get("worker"))
        branch = _progress_field(payload.get("branch"))
        title = _progress_field(payload.get("title"))
        if worker:
            self.status.worker = worker
        if branch:
            self.status.branch = branch
        if title:
            self.status.ticket_title = title
        short = ""
        if self._progress_block is not None and self._progress_block.ticket_id:
            short = self._progress_block.ticket_id[:8]
            self.status.ticket_short = short
        reported = str(payload.get("ticket_id") or "")
        self._remember_progress_identity(
            title=title,
            worker=worker,
            branch=branch,
            short=short,
            ticket_id=reported,
        )
        if self._progress_block is not None:
            self._progress_block.summary = self.status.summary
            self._progress_block.question = self.status.question
            self._progress_block.error = self.status.error
            self._progress_block.steps = list(steps)
        self._overwrite_result()
        self._progress_applied += 1
        self._invalidate()

    def _anchor_elapsed(self, seconds: float) -> None:
        """Start the local clock from the server elapsed time.

        The redraw heartbeat keeps an open phase moving from this anchor. It
        does not read ``/progress`` again. A terminal phase stays frozen.
        """
        now = self._clock()
        mono = self._monotonic()
        terminal = _phase_rank(self.status.phase) == 3
        if self._progress_block is not None:
            self._progress_block.started_at = now - seconds
            self._progress_block.finished_at = now if terminal else None
        self.status.started_monotonic = mono - seconds
        self.status.finished_monotonic = mono if terminal else None

    def _remember_progress_identity(
        self,
        *,
        title: str = "",
        worker: str = "",
        branch: str = "",
        short: str = "",
        ticket_id: str = "",
    ) -> None:
        """Copy identity onto the open card only when the event names that Ticket."""
        block = self._progress_block
        if block is None:
            return
        if ticket_id and block.ticket_id and ticket_id != block.ticket_id:
            return
        if title:
            block.ticket_title = title
        if worker:
            block.worker = worker
        if branch:
            block.branch = branch
        if short:
            block.ticket_short = short
        if ticket_id and not getattr(block, "unbound", False):
            block.ticket_id = ticket_id

    def _copy_card_identity(self, target: Any, source: Any) -> None:
        """Give ``target`` the source card's ticket id, title, worker, and branch."""
        if source is None or target is None:
            return
        title = str(getattr(source, "ticket_title", "") or "")
        worker = str(getattr(source, "worker", "") or "")
        branch = str(getattr(source, "branch", "") or "")
        short = str(getattr(source, "ticket_short", "") or "")
        ticket_id = str(getattr(source, "ticket_id", "") or "")
        if not short:
            short = ticket_id[:8]
        if title:
            target.ticket_title = title
        if worker:
            target.worker = worker
        if branch:
            target.branch = branch
        if short:
            target.ticket_short = short
        if ticket_id and not getattr(target, "unbound", False):
            target.ticket_id = ticket_id

    def _overwrite_result(self) -> None:
        """Point the current Ticket's result card at the calibration fields."""
        if self._progress_block is None:
            return
        wanted = str(getattr(self._progress_block, "ticket_id", "") or "")
        if not wanted:
            return
        target: EndBlock | None = None
        unstamped: list[EndBlock] = []
        for block in reversed(self.conversation.blocks):
            if not isinstance(block, EndBlock) or getattr(block, "unbound", False):
                continue
            if str(getattr(block, "ticket_id", "") or "") == wanted:
                target = block
                break
            if not getattr(block, "ticket_id", ""):
                unstamped.append(block)
        # A card created in this turn has no id yet. A restored collision stays unbound.
        if target is None and len(unstamped) == 1:
            target = unstamped[0]
            target.ticket_id = wanted
        if target is None:
            return
        block = target
        phase = _canonical_phase(self.status.phase)
        summary, question, error = visible_progress_fields(
            phase,
            self.status.summary,
            self.status.question,
            self.status.error,
        )
        if phase and phase != "idle":
            block.status = phase
        # The card keeps only the lines this phase is allowed to show, so a
        # waiting view cannot leave a running summary on screen.
        block.summary = summary
        block.body = self.status.body if phase in {"completed", "failed", "cancelled"} else ""
        block.question = question
        block.error = error
        if self._progress_block is not None:
            block.steps = list(getattr(self._progress_block, "steps", []) or [])
            self._copy_card_identity(block, self._progress_block)
        return

    def _current_ticket_id(self) -> str:
        if self._progress_block is not None and self._progress_block.ticket_id:
            return self._progress_block.ticket_id
        if self.session is not None and self.session.last_ticket_id:
            return self.session.last_ticket_id
        return ""

    def interrupt(self) -> None:
        """Stop the local watch. A confirmed Esc also calls ``client.stop``."""
        if self._pending is not None:
            self.resolve_permission("d")
            return
        self._interrupt_confirm = False
        if self._watch_task is not None and not self._watch_task.done():
            self._interrupted = True
            self._watch_task.cancel()
            self._invalidate()

    def _confirm_interrupt(self) -> None:
        """Second Esc: stop the Ticket the way ``/cancel`` does, then the watch."""
        ticket = self._current_ticket_id()
        stop = getattr(self.client, "stop", None)
        self.interrupt()
        if stop is None or not ticket:
            self.conversation.add(NoticeBlock(text=_unsupported(self._locale())))
            self._invalidate()
            return

        async def _run() -> None:
            await stop(ticket)
            self.conversation.add(
                NoticeBlock(
                    text=_notice("stopRequested", self._locale()).format(id=ticket[:8])
                )
            )
            self._invalidate()

        try:
            task = asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            self.conversation.add(NoticeBlock(text=_unsupported(self._locale())))
            self._invalidate()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def note_ctrl_c(self, now: float | None = None) -> bool:
        """Arm/confirm the double Ctrl+C exit; True means the app should exit."""
        moment = self._clock() if now is None else now
        if self._pending is not None:
            self.resolve_permission("d")
        if self._watch_task is not None and not self._watch_task.done():
            # Cancelling the watch is the first half of quitting, not a
            # separate gesture: arm the double-press timer on the same press.
            self.interrupt()
        if moment - self._last_ctrl_c <= DOUBLE_CTRL_C_WINDOW_S:
            return True
        self._last_ctrl_c = moment
        self.conversation.add(NoticeBlock(text=_notice("pressCtrlCAgain", self._locale())))
        self._invalidate()
        return False

    def scroll(self, delta: int) -> None:
        self._page(delta)

    # -- permissions -------------------------------------------------------

    async def request_permission(self, request: PermissionRequest) -> str:
        """Show a permission row and wait for allow/session/deny."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        block = PermissionBlock(
            action=request.action, label=request.label, detail=request.detail
        )
        self.conversation.add(block)
        self._pending = (request, future, block)
        self._pending_epoch = self._epoch
        self._invalidate()
        try:
            decision = await future
        finally:
            self._pending = None
            self._invalidate()
        block.decision = decision
        return decision

    def resolve_permission(self, key: str) -> bool:
        """Resolve the pending permission from a composer key.

        A permission belongs to the session/epoch that requested it. If the
        operator switched sessions while it was open, the decision is forced
        to deny: stepping on keys must not grant session-wide access inside a
        session that never asked for it.
        """
        if self._pending is None:
            return False
        request, future, block = self._pending
        decision = decision_from_key(key)
        if decision == "pending":
            return False
        if self._pending_epoch != self._epoch:
            decision = "deny"
        if decision == "session":
            self.permissions.allow_session(request.action)
        if not future.done():
            future.set_result(decision)
        block.decision = decision
        self._invalidate()
        return True

    async def _gated_local(
        self, action: str, label: str, detail: str
    ) -> bool:
        """Ask for permission when needed; True when the action may run."""
        epoch = self._epoch
        # Gated local actions are serialized: a second action waits for the
        # first permission row instead of replacing/stranding it. Recheck
        # inside the lock so a Session grant from the first action is honored.
        async with self._permission_lock:
            if not self.permissions.needs_confirmation(action):
                return True
            decision = await self.request_permission(
                PermissionRequest(action=action, label=label, detail=detail)
            )
        if epoch != self._epoch:
            # The session changed while the prompt was open: do not write an
            # old denial or grant into the new session.
            return False
        if decision == "deny":
            self.conversation.add(
                NoticeBlock(text=_notice("denied", self._locale()).format(label=label))
            )
            return False
        if decision == "session":
            self.permissions.allow_session(action)
        return True

    # -- local actions --------------------------------------------------------

    async def _local_conductor(self, action: str, label: str, args: list[str]) -> None:
        epoch = self._epoch
        if not await self._gated_local(action, label, " ".join(args)):
            return
        if epoch != self._epoch:
            return
        self.conversation.add(
            NoticeBlock(text=_notice("runningLocally", self._locale()).format(label=label))
        )
        self._invalidate()
        try:
            # A merge can consume the server's full test + Codex budget; do
            # not cut the CLI client off at the one-hour default.
            local_timeout = 11_100.0 if args[:1] == ["merge"] else None
            kwargs = {} if local_timeout is None else {"timeout": local_timeout}
            output = await asyncio.to_thread(
                run_conductor,
                args,
                base_url=self.server_url,
                worktree_root_path=self.worktree_root,
                **kwargs,
            )
        except SuperAgentError as exc:
            if epoch == self._epoch:
                self.conversation.add(ErrorBlock(text=str(exc)))
            return
        if epoch != self._epoch:
            return
        if args[:2] == ["review", "status"] and self._show_review_queue(output):
            return
        self._add_output(output)

    def _show_review_queue(self, output: str) -> bool:
        """Paint ``conductor review status`` output through tui_review.

        The CLI prints the ``/api/v1/reviews`` payload (bare or inside the v1
        JSON envelope). Anything else returns False and is shown as text.
        """
        try:
            payload = json.loads(output)
        except (TypeError, ValueError):
            return False
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        reviews = payload.get("reviews") if isinstance(payload, dict) else None
        if not isinstance(reviews, list):
            return False
        self._reviews = [row for row in reviews if isinstance(row, dict)]
        gate = payload.get("gate")
        self._review_gate = gate if isinstance(gate, dict) else None
        self.conversation.add(
            NoticeBlock(
                text="\n".join(
                    result_rows(
                        self._reviews,
                        self._review_gate,
                        locale=self._locale(),
                        width=self._transcript_width(),
                        theme=self._active_theme(),
                    )
                )
            )
        )
        return True

    async def _shell_line(self, command: str, *, epoch: int | None = None) -> None:
        epoch = self._epoch if epoch is None else epoch
        if epoch != self._epoch:
            return
        if not command:
            self.conversation.add(ErrorBlock(text="usage: !<shell command>"))
            return
        if not await self._gated_local("local-shell", "run shell command", command):
            return
        if epoch != self._epoch:
            return
        self.conversation.add(
            NoticeBlock(
                text=_notice("runningShell", self._locale()).format(command=command)
            )
        )
        self._invalidate()
        try:
            output = await asyncio.to_thread(run_shell, command, cwd=self.local_root)
        except SuperAgentError as exc:
            if epoch == self._epoch:
                self.conversation.add(ErrorBlock(text=str(exc)))
            return
        if epoch != self._epoch:
            return
        self._add_output(output)

    def _add_output(self, output: str) -> None:
        body = plain(output).strip()
        if not body:
            self.conversation.add(NoticeBlock(text=_notice("noOutput", self._locale())))
            return
        self.conversation.add(AssistantBlock(text=body))

    # -- slash commands -------------------------------------------------------

    async def handle_command(self, line: str) -> bool:
        parts = line.strip().split(None, 1)
        command = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        args = rest.split()
        if command in {"/exit", "/quit", "/q"}:
            self._save_session()
            if self._app is not None:
                self._app.exit(result=0)
            return False
        if command == "/help":
            self.conversation.add(AssistantBlock(text=HELP_TEXT, chrome=True))
        elif command == "/clear":
            self._advance_progress_generation()
            self.conversation.clear()
            self._progress_block = None
        elif command == "/status":
            await self._status_command()
        elif command == "/dispatch":
            await self._dispatch_command(rest)
        elif command == "/roles":
            await self._roles_command(rest)
        elif command in {"/approve", "/reject"}:
            await self._decision_command(command[1:], args)
        elif command == "/details":
            self.show_worker_details = not self.show_worker_details
            self.conversation.add(
                NoticeBlock(
                    text=_notice(
                        "workerDetailsShown"
                        if self.show_worker_details
                        else "workerDetailsHidden",
                        self._locale(),
                    )
                )
            )
        elif command == "/compact":
            removed = self.conversation.compact()
            self.conversation.add(
                NoticeBlock(text=_notice("compacted", self._locale()).format(count=removed))
            )
        elif command == "/expand":
            changed = self.conversation.expand_tools()
            for block in self.conversation.blocks:
                if isinstance(block, EndBlock) and not block.expanded:
                    block.expanded = True
                    changed += 1
            self.conversation.add(
                NoticeBlock(text=_notice("expanded", self._locale()).format(count=changed))
            )
        elif command == "/collapse":
            changed = self.conversation.collapse_tools()
            for block in self.conversation.blocks:
                if isinstance(block, EndBlock) and block.expanded:
                    block.expanded = False
                    changed += 1
            self.conversation.add(
                NoticeBlock(text=_notice("collapsed", self._locale()).format(count=changed))
            )
        elif command == "/new":
            await self._quiesce()
            self._save_session()
            self.session = self.sessions.new()
            self.conversation.clear()
            self._progress_block = None
            self._advance_progress_generation()
            self._progress_source = ""
            self._observed_status = None
            self.status.phase = "idle"
            self.status.summary = ""
            self.status.question = ""
            self.status.error = ""
            self.status.started_monotonic = None
            self.status.finished_monotonic = None
            self.status.session = self.session.short_id
            self.conversation.add(
                NoticeBlock(
                    text=_notice("newSession", self._locale()).format(
                        id=self.session.short_id
                    )
                )
            )
            self._save_session()
        elif command in {"/jobs", "/j"}:
            await self._jobs_command(rest)
        elif command == "/attach":
            await self._jobs_command(f"watch {rest}".strip())
        elif command == "/stop":
            await self._stop_command(args)
        elif command == "/cancel":
            if not args:
                current = self._current_ticket_id()
                if not current:
                    raise SuperAgentError("usage: /cancel <id|index>")
                args = [current]
            await self._stop_command(args)
        elif command == "/reply":
            await self._reply_command(rest)
        elif command == "/retry":
            await self._retry_command(args)
        elif command in {"/worker", "/workspace", "/memory"}:
            self.conversation.add(NoticeBlock(text=_unsupported(self._locale())))
        elif command == "/skills":
            await self._cli_status_command(["conductor", "skills", "status", "--json"])
        elif command == "/mcp":
            await self._cli_status_command(["conductor", "mcp", "status", "--json"])
        elif command == "/agents":
            await self._agents_command()
        elif command == "/signals":
            await self._signals_command()
        elif command == "/model":
            await self._model_command()
        elif command == "/resume":
            await self._resume_command(args)
        elif command == "/review":
            await self._review_command(args)
        elif command == "/merge":
            if not args:
                raise SuperAgentError("usage: /merge <line> --into <branch>")
            await self._local_conductor(
                "local-merge", f"conductor merge {describe(args)}", ["merge", *args]
            )
        else:
            raise SuperAgentError(f"unknown command {command}; try /help")
        self._invalidate()
        return True

    async def _status_command(self) -> None:
        """Reread Supervisor progress. Does not watch, stop, or open details."""
        ticket_id = self._current_ticket_id()
        if not ticket_id:
            self.conversation.add(NoticeBlock(text=_notice("noTicket", self._locale())))
            return
        await self._calibrate_progress(ticket_id)

    async def _review_command(self, args: list[str]) -> None:
        """The /review screen through the S07 renderers.

        ``/review status`` paints the queue and the G6 gate from the records
        this session has seen; ``/review run ...`` paints a progress card and
        then runs the local conductor command exactly as before.
        """
        locale = self._locale()
        width = self._transcript_width()
        if not args:
            self.conversation.add(
                NoticeBlock(
                    text="\n".join(
                        usage_rows(locale=locale, width=width, theme=self._active_theme())
                    )
                )
            )
            return
        if args[0] in {"status", "queue"}:
            # The queue lives on the server: read it through the operator CLI
            # (as /review status always has) and paint the records it returns.
            status_args = ["review", "status", *args[1:]]
            await self._local_conductor(
                "local-review",
                f"conductor review {describe(status_args[1:])}",
                status_args,
            )
            return
        if args[0] == "run":
            line = args[1] if len(args) > 1 else "—"
            reviewer = "—"
            if "--by" in args:
                position = args.index("--by")
                if position + 1 < len(args):
                    reviewer = args[position + 1]
            self.conversation.add(
                NoticeBlock(
                    text="\n".join(
                        progress_rows(
                            {"line": line, "reviewer": reviewer, "state": "running"},
                            locale=locale,
                            width=width,
                            theme=self._active_theme(),
                            frame=0,
                        )
                    )
                )
            )
        await self._local_conductor(
            "local-review", f"conductor review {describe(args)}", ["review", *args]
        )

    async def _decision_command(self, decision: str, args: list[str]) -> None:
        """Approve or reject one dispatch through the dispatch API."""
        if not args:
            message = f"usage: /{decision} <dispatch id>"
            raise SuperAgentError(message)
        ident = args[0]
        request = getattr(self.client, "_request", None)
        if request is None:
            self.conversation.add(
                NoticeBlock(text=_unsupported(self._locale()))
            )
            return
        payload = await request(
            "POST", f"/api/v1/dispatch/{ident}/{decision}", json={}
        )
        if isinstance(payload, dict):
            self._palette_dispatches = [payload]
            self.conversation.add(
                DispatchBlock(
                    lines=render_dispatch_view(
                        payload, self._transcript_width(), self._locale()
                    )
                )
            )
        else:
            self.conversation.add(
                NoticeBlock(
                    text=_notice("decisionRequested", self._locale()).format(
                        decision=decision, id=ident[:8]
                    )
                )
            )

    async def _roles_command(self, rest: str) -> None:
        """R6 L1: the role registry, read or edited through the API."""
        from . import roles_view

        epoch = self._epoch
        try:
            action, role, body = roles_view.parse(rest)
        except roles_view.RolesUsageError as exc:
            self.conversation.add(NoticeBlock(text=str(exc)))
            return
        if action == "set":
            payload = await self.client.set_role(role, body)
        else:
            payload = await self.client.roles()
        if epoch != self._epoch:
            return
        self.conversation.add(NoticeBlock(text="\n".join(roles_view.render(payload))))

    async def _dispatch_command(self, rest: str) -> None:
        """Read a dispatch once. Repeating the command refreshes; the heartbeat does not."""
        epoch = self._epoch
        locale = self._locale()
        width = self._transcript_width()
        if not rest.strip():
            rows = await self.client.active_dispatches()
            if epoch != self._epoch:
                return
            self._palette_dispatches = list(rows)
            if not rows:
                self.conversation.add(
                    NoticeBlock(text=_notice("noActiveDispatches", locale))
                )
                return
            for row in rows:
                self.conversation.add(
                    DispatchBlock(
                        lines=render_dispatch_view(row, width, locale)
                    )
                )
            return
        payload = await self.client.dispatch(rest.split(maxsplit=1)[0])
        if epoch != self._epoch:
            return
        if payload is None:
            self.conversation.add(
                NoticeBlock(text=_notice("dispatchNotFound", locale))
            )
            return
        self._palette_dispatches = [payload]
        self.conversation.add(
            DispatchBlock(lines=render_dispatch_view(payload, width, locale))
        )

    async def _jobs_command(self, rest: str) -> None:
        epoch = self._epoch
        args = rest.split()
        attention = bool(args) and args[0] in {"--attention", "-a", "attention"}
        if not args or attention:
            data = await self.client.tickets(limit=10)
            if epoch != self._epoch:
                return
            rows = data.get("tickets") if isinstance(data, dict) else None
            self._jobs = [row for row in rows or [] if isinstance(row, dict)]
            # The numbers an operator reads are the numbers /reply 1 and
            # /stop 1 resolve: --attention renumbers its filtered rows, so the
            # snapshot is taken from the same filtered list.
            shown = (
                [row for row in self._jobs if _needs_operator(row)]
                if attention
                else list(self._jobs)
            )
            self._shown_tickets = list(shown)
            self._write_list_snapshot(shown)
            if not self._jobs:
                self.conversation.add(NoticeBlock(text=_notice("noJobs", self._locale())))
                return
            render = attention_rows if attention else jobs_rows
            self.conversation.add(
                NoticeBlock(
                    text="\n".join(
                        render(
                            shown,
                            theme=self._active_theme(),
                            locale=self._locale(),
                            width=self._transcript_width(),
                        )
                    )
                )
            )
            return
        if args[0] in {"watch", "follow"}:
            if len(args) < 2:
                raise SuperAgentError("usage: /jobs watch <id|index>")
            target = self._resolve_target(args[1])
            if epoch != self._epoch:
                return
            self.conversation.add(
                NoticeBlock(
                    text="\n".join(
                        watch_header_rows(
                            {"ticket_id": target},
                            theme=self._active_theme(),
                            locale=self._locale(),
                            width=self._transcript_width(),
                        )
                    )
                )
            )
            await self.watch(target)
            return
        target = self._resolve_target(args[0])
        detail = await self.client.ticket(target)
        if epoch != self._epoch:
            return
        if not isinstance(detail, dict):
            detail = {}
        self.conversation.add(
            NoticeBlock(
                text="\n".join(
                    job_detail_rows(
                        detail,
                        theme=self._active_theme(),
                        locale=self._locale(),
                        width=self._transcript_width(),
                    )
                )
            )
        )
        if len(args) > 1 and args[1] in {"watch", "follow"}:
            await self.watch(target)

    async def _stop_command(self, args: list[str]) -> None:
        epoch = self._epoch
        if not args:
            raise SuperAgentError("usage: /stop <id|index>")
        target = self._resolve_target(args[0])
        await self.client.stop(target)
        if epoch != self._epoch:
            return
        self.conversation.add(
            NoticeBlock(
                text=_notice("stopRequested", self._locale()).format(id=target[:8])
            )
        )

    async def _agents_command(self) -> None:
        epoch = self._epoch
        try:
            fleet = await self.client.fleet()
            if not isinstance(fleet, dict):
                fleet = {}
        except SuperAgentError as exc:
            if epoch == self._epoch:
                self.conversation.add(
                    NoticeBlock(
                        text=_notice("fleetUnavailable", self._locale()).format(error=exc)
                    )
                )
            fleet = {}
        if epoch != self._epoch:
            return
        agents = fleet.get("agents") or []
        lines = [
            f"{plain(row.get('id'))} {plain(row.get('role'))} "
            f"{plain(row.get('status'))} {plain(row.get('branch'))}"
            for row in agents
            if isinstance(row, dict)
        ]
        if lines:
            self.conversation.add(AssistantBlock(text="fleet:\n" + "\n".join(lines)))
        try:
            response = await self.client.daemons()
            daemons = (
                response.get("daemons", []) if isinstance(response, dict) else []
            )
            if not isinstance(daemons, list):
                daemons = []
        except SuperAgentError:
            daemons = []
        if epoch != self._epoch:
            return
        daemon_rows = [row for row in daemons if isinstance(row, dict)]
        if daemon_rows:
            self.conversation.add(
                AssistantBlock(
                    text="daemons:\n"
                    + "\n".join(
                        f"{str(row.get('daemon_id') or '')[:8]} "
                        f"{plain(row.get('name'))} {plain(row.get('provider'))}"
                        for row in daemon_rows
                    )
                )
            )

    async def _signals_command(self) -> None:
        epoch = self._epoch
        overview = await self.client.overview()
        if epoch != self._epoch:
            return
        if not isinstance(overview, dict):
            overview = {}
        counts = overview.get("counts")
        if not isinstance(counts, dict):
            counts = {}
        response = await self.client.sources()
        sources = response.get("sources") if isinstance(response, dict) else []
        if not isinstance(sources, list):
            sources = []
        if epoch != self._epoch:
            return
        lines = [
            f"signals: {overview.get('sources', 0)} sources "
            f"({overview.get('enabled_sources', 0)} enabled)"
        ]
        for row in sources:
            if not isinstance(row, dict):
                continue
            config = row.get("config") or {}
            if not isinstance(config, dict):
                config = {}
            name = config.get("repository") or config.get("chat_id") or row.get("source_id")
            lines.append(
                f"  {plain(name)} {plain(row.get('kind'))} "
                f"{'enabled' if row.get('enabled') else 'disabled'}"
            )
        lines.append(
            "jobs: "
            + " ".join(f"{key}={counts.get(key, 0)}" for key in ("attention", "running", "queued", "failed"))
        )
        self.conversation.add(AssistantBlock(text="\n".join(lines)))

    async def _model_command(self) -> None:
        epoch = self._epoch
        await self._refresh_overview()
        if epoch != self._epoch:
            return
        providers: dict[str, list[str]] = {}
        try:
            models = await self.client.models()
        except SuperAgentError as exc:
            if epoch == self._epoch:
                self.conversation.add(
                    NoticeBlock(
                        text=_notice("modelsUnavailable", self._locale()).format(error=exc)
                    )
                )
        else:
            if epoch != self._epoch:
                return
            entries = models.get("models") if isinstance(models, dict) else None
            if isinstance(entries, dict):
                for provider, entry in entries.items():
                    names = entry.get("models") if isinstance(entry, dict) else entry
                    providers[str(provider)] = [str(name) for name in names or []]
        if epoch != self._epoch:
            return
        self.conversation.add(
            AssistantBlock(
                text=model_table(
                    self.status.router_model, providers, width=self._view_width()
                )
            )
        )

    async def _resume_command(self, args: list[str]) -> None:
        if not args:
            sessions = self.sessions.list_sessions()
            if not sessions:
                self.conversation.add(NoticeBlock(text=_notice("noSavedSessions", self._locale())))
                return
            lines = ["saved sessions (newest first):"]
            for record in sessions[:10]:
                lines.append(
                    f"  {record.session_id}  {plain(record.title)}  "
                    f"ticket {record.last_ticket_id[:8] or '-'}"
                )
            lines.append("resume with /resume <id> (an unambiguous prefix works)")
            self.conversation.add(AssistantBlock(text="\n".join(lines)))
            return
        query = str(args[0]).strip().lower()
        sessions = self.sessions.list_sessions()
        exact = [record for record in sessions if record.session_id == query]
        matches = exact or [
            record for record in sessions if record.session_id.startswith(query)
        ]
        if not matches:
            raise SuperAgentError(f"no session starts with {args[0]}")
        if len(matches) > 1:
            names = ", ".join(record.session_id for record in matches[:5])
            raise SuperAgentError(f"ambiguous session prefix {args[0]}: {names}")
        target = matches[0].session_id
        if self.session is not None and target == self.session.session_id:
            # Self-resume must not replace unsaved conversation with the older
            # on-disk snapshot: persist the live transcript and no-op.
            self._save_session()
            self.conversation.add(
                NoticeBlock(
                    text=_notice("alreadyInSession", self._locale()).format(
                        id=plain(target)
                    )
                )
            )
            self._invalidate()
            return
        record = self.sessions.load(target)
        if record is None:
            raise SuperAgentError(f"no session {target}")
        await self._quiesce()
        self._save_session()
        self._restore_session(record, resumed=False)
        self.conversation.add(
            NoticeBlock(
                text=_notice("resumedSession", self._locale()).format(
                    id=plain(record.session_id)
                )
            )
        )
        self._save_session()
        self._invalidate()

    async def _refresh_recent_tickets(self, *, epoch: int | None = None) -> None:
        epoch = self._epoch if epoch is None else epoch
        fetch = getattr(self.client, "tickets", None)
        if fetch is None:
            return
        try:
            data = await fetch(limit=3)
        except Exception:
            return
        if epoch != self._epoch or not isinstance(data, dict):
            return
        rows = data.get("tickets")
        if not isinstance(rows, list):
            rows = []
        self._recent_tickets = [row for row in rows if isinstance(row, dict)][:3]
        # Startup is a list display. A list the user already opened stays put.
        if not self._list_snapshot:
            self._write_list_snapshot(self._recent_tickets[:3])

    def _listed_tickets(self) -> list[dict[str, Any]]:
        """Home rows in the current snapshot's order. A paint does not record one."""
        if not self._list_snapshot:
            return [row for row in self._recent_tickets[:3] if isinstance(row, dict)]
        by_id: dict[str, dict[str, Any]] = {}
        for row in [*self._jobs, *self._recent_tickets]:
            if not isinstance(row, dict):
                continue
            ticket_id = str(row.get("ticket_id") or "")
            if ticket_id and ticket_id not in by_id:
                by_id[ticket_id] = row
        ordered: list[dict[str, Any]] = []
        for index in sorted(self._list_snapshot):
            ticket_id = str(self._list_snapshot[index].get("ticket_id") or "")
            if not ticket_id:
                continue
            ordered.append(by_id.get(ticket_id, {"ticket_id": ticket_id}))
        return ordered[:3]

    def _looks_like_ticket_id(self, head: str) -> bool:
        """UUID syntax only. A hyphenated word such as ``re-run`` is not an id."""
        return bool(_TICKET_UUID.fullmatch(head))

    def _write_list_snapshot(self, rows: list[dict[str, Any]]) -> None:
        shown_at = time.time()
        snapshot: dict[int, dict[str, Any]] = {}
        for index, row in enumerate(rows, 1):
            ticket_id = str(row.get("ticket_id") or "")
            if ticket_id:
                snapshot[index] = {"ticket_id": ticket_id, "shown_at": shown_at}
        self._list_snapshot = snapshot

    def _snapshot_ids(self) -> list[str]:
        """Numbered ids from the last list. A new session uses the Home list."""
        if self._list_snapshot:
            return [
                str(self._list_snapshot[index]["ticket_id"])
                for index in sorted(self._list_snapshot)
            ]
        return [
            str(row.get("ticket_id") or "")
            for row in self._recent_tickets[:3]
            if row.get("ticket_id")
        ]

    def _known_ticket_ids(self) -> list[str]:
        found: list[str] = []
        for ticket_id in self._snapshot_ids():
            if ticket_id and ticket_id not in found:
                found.append(ticket_id)
        for row in [*self._recent_tickets, *self._jobs]:
            ticket_id = str(row.get("ticket_id") or "")
            if ticket_id and ticket_id not in found:
                found.append(ticket_id)
        current = self._current_ticket_id()
        if current and current not in found:
            found.append(current)
        return found

    def _prefix_matches(self, head: str) -> list[str]:
        return [ticket_id for ticket_id in self._known_ticket_ids() if ticket_id.startswith(head)]

    def _matched_target(self, head: str) -> str | None:
        """Resolve one target token.

        Order: full UUID, an in-range index, then a unique prefix of at least
        eight hex digits. An out-of-range integer falls through to the prefix
        check. ``None`` means the token is reply text, not a target.
        """
        text = str(head or "").strip()
        if not text or any(char.isspace() for char in text):
            return None
        if self._looks_like_ticket_id(text):
            return text
        number = text[1:] if text.startswith("#") and text[1:].isdigit() else text
        if number.isdigit():
            index = int(number)
            rows = self._snapshot_ids()
            if 1 <= index <= len(rows):
                return rows[index - 1]
        if _id_prefix(text):
            prefixed = self._prefix_matches(text)
            if len(prefixed) == 1:
                return prefixed[0]
            if len(prefixed) > 1:
                message = f"{text} matches more than one job: {', '.join(prefixed)}"
                raise SuperAgentError(message)
        return None

    def _explicit_reply_target(self, head: str) -> bool:
        return self._matched_target(head) is not None

    def _explicit_id(self, head: str) -> str:
        return self._matched_target(head) or head

    async def _reply_command(self, rest: str) -> None:
        text = rest.strip()
        if not text:
            raise SuperAgentError("usage: /reply [<id|index>] <text>")
        target = self._current_ticket_id()
        body = text
        head, _, tail = text.partition(" ")
        if self._explicit_reply_target(head):
            target = self._explicit_id(head)
            body = tail.strip()
        elif not target and " " not in text:
            target = self._resolve_target(text)
            body = ""
        if not target or not body:
            raise SuperAgentError("usage: /reply [<id|index>] <text>")
        reply = getattr(self.client, "reply", None)
        if reply is None:
            self.conversation.add(NoticeBlock(text=_unsupported(self._locale())))
            return
        await reply(target, body)
        self.conversation.add(
            NoticeBlock(text=_notice("repliedTo", self._locale()).format(id=target[:8]))
        )
        self._reply_dismissed = False
        await self.watch(target)

    async def _retry_command(self, args: list[str]) -> None:
        if args:
            target = self._resolve_target(args[0])
        else:
            target = self._current_ticket_id()
        if not target:
            raise SuperAgentError("usage: /retry [<id|index>]")
        retry = getattr(self.client, "retry", None)
        if retry is None:
            self.conversation.add(NoticeBlock(text=_unsupported(self._locale())))
            return
        await retry(target)
        self.conversation.add(
            NoticeBlock(
                text=_notice("retryRequested", self._locale()).format(id=target[:8])
            )
        )
        await self.watch(target)

    async def _cli_status_command(self, argv: list[str]) -> None:
        """Run a local ``conductor … status --json``. Never a shell, never skills_hub."""
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True,
                text=True,
                timeout=5,
                shell=False,
                env=os.environ.copy(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.conversation.add(NoticeBlock(text=str(exc)))
            return
        raw = (completed.stdout or "").strip()
        if completed.returncode and not raw:
            detail = (completed.stderr or "").strip() or f"exited {completed.returncode}"
            self.conversation.add(NoticeBlock(text=detail))
            return
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            detail = (completed.stderr or raw or "unreadable status").strip()
            self.conversation.add(NoticeBlock(text=detail))
            return
        self.conversation.add(NoticeBlock(text=_status_glyphs(payload)))

    def _resolve_target(self, value: str) -> str:
        text = str(value or "").strip()
        matched = self._matched_target(text)
        if matched:
            return matched
        number = text[1:] if text.startswith("#") and text[1:].isdigit() else text
        if number.isdigit():
            raise SuperAgentError(f"no job at index {value}")
        return text

    # -- prompt_toolkit application ---------------------------------------

    def _composer_prompt(self) -> list[tuple[str, str]]:
        glyph = self._glyph("prompt", "❯")
        if self._reply_active():
            return [("class:prompt-hold", f"{glyph} ")]
        return [("class:prompt", f"{glyph} ")]

    def _on_composer_changed(self) -> None:
        """Typing is not a confirmation of the armed interrupt."""
        if self._composer_measuring:
            return
        self._interrupt_confirm = False
        self._sync_composer_height()

    def _composer_content_width(self) -> int:
        """Width prompt_toolkit wraps in, after the composer wall."""
        columns = max(1, self._columns())
        if self._border_enabled():
            return max(1, columns - 1)
        return columns

    def _measure_current_composer(self) -> int:
        """Row count from the composer's own BufferControl, capped at 8."""
        area = self.input_area
        if area is None:
            return 1
        control = getattr(area, "control", None)
        window = getattr(area, "window", None)
        if control is None or window is None or not hasattr(control, "preferred_height"):
            return 1
        width = self._composer_content_width()

        def ask() -> int:
            # History loading needs a running loop. A height check before the
            # application is running still uses this control's own wrap.
            sentinel: object = object()
            restore = False
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                if getattr(area.buffer, "_load_history_task", None) is None:
                    area.buffer._load_history_task = sentinel
                    restore = True
            try:
                measured = control.preferred_height(
                    width,
                    MAX_COMPOSER_HEIGHT,
                    wrap_lines=True,
                    get_line_prefix=window.get_line_prefix,
                )
            finally:
                if restore and area.buffer._load_history_task is sentinel:
                    area.buffer._load_history_task = None
            if not isinstance(measured, int) or measured < 1:
                return 1
            return min(MAX_COMPOSER_HEIGHT, measured)

        # get_app() returns a dummy when nothing is running, and the height
        # math only needs that dummy's render counter.
        return ask()

    def _composer_visual_rows(self, text: str) -> int:
        area = self.input_area
        if area is None:
            return 1
        original = area.buffer.text
        cursor = area.buffer.cursor_position
        self._composer_measuring = True
        try:
            if original != text:
                area.buffer.text = text
            return self._measure_current_composer()
        finally:
            if area.buffer.text != original:
                area.buffer.text = original
                area.buffer.cursor_position = min(cursor, len(area.buffer.text))
            self._composer_measuring = False

    def _sync_composer_height(self) -> None:
        """Keep the composer at its visual height. Empty is one row, not the max."""
        area = self.input_area
        if area is None:
            return
        lines = self._measure_current_composer()
        area.window.height = Dimension(min=lines, max=lines, preferred=lines)
        # Window.dont_extend_height is a Filter, not a bool. A fixed max is
        # what stops an empty composer from absorbing the leftover rows.

    def _border_enabled(self) -> bool:
        return self._columns() > 60

    def _transcript_overflows(self) -> bool:
        window = self.conversation_window
        info = getattr(window, "render_info", None) if window is not None else None
        if info is None:
            return False
        return int(info.content_height) > int(info.window_height)

    def _border_fragments(self, left: str, right: str) -> FormattedText:
        if not self._border_enabled():
            return FormattedText([])
        width = max(2, self._columns())
        style = "class:composer-hold" if self._reply_active() else "class:composer-border"
        return FormattedText([(style, left + "─" * max(0, width - 2) + right)])

    # -- palette (S04) ------------------------------------------------------

    def _palette_state(self, query: str | None = None) -> PaletteState:
        """The pure palette state of the current query."""
        text = self._palette_query if query is None else query
        locale = self._locale()
        token, separator, partial = text.partition(" ")
        canonical = canonical_command(token) if separator else None
        note = ""
        if canonical is not None:
            result = complete_arguments(
                canonical,
                partial.strip(),
                provider=self._palette_arg_options,
            )
            rows = argument_rows(result.options)
            kind = KIND_ARGUMENTS
            command = canonical
            if result.needs_id and not result.options:
                note = t(PALETTE_STRINGS, "needsId", locale).format(command=canonical)
            elif len(result.options) > 1 and partial.strip():
                note = t(PALETTE_STRINGS, "ambiguousCount", locale).format(
                    count=len(result.options)
                )
            count = len(rows)
        else:
            ranked = rank_commands(text)
            rows = command_rows(ranked, locale)
            kind = "commands"
            command = ""
            count = len(rows)
        selected = max(0, min(self._palette_selected, max(0, len(rows) - 1)))
        return PaletteState(
            query=text,
            rows=rows,
            selected=selected,
            kind=kind,
            command=command,
            count=count,
            note=note,
        )

    def _palette_arg_options(self, command: str) -> list[ArgOption]:
        """The ids the palette offers for a command (D2)."""
        options: list[ArgOption] = []
        if command in {"dispatch", "approve", "reject"}:
            for row in self._palette_dispatches:
                if not isinstance(row, dict):
                    continue
                ident = str(row.get("id") or row.get("dispatch_id") or "")
                if not ident:
                    continue
                raw = str(row.get("phase") or row.get("state") or "").strip().lower()
                if raw in {"approval", "awaiting_human"}:
                    phase = "awaiting_human"
                elif not raw:
                    phase = "running"
                else:
                    phase = raw
                subject = str(
                    row.get("goal")
                    or row.get("subject")
                    or row.get("title")
                    or ""
                )
                options.append(ArgOption(ident, phase, plain(subject)))
            return options
        if command in {"tickets", "reply", "retry", "cancel", "stop"}:
            for row in self._jobs:
                ident = str(row.get("ticket_id") or "")
                if not ident:
                    continue
                options.append(
                    ArgOption(
                        ident,
                        _canonical_phase(str(row.get("status") or "")),
                        plain(row.get("title") or ""),
                    )
                )
        return options

    def _open_palette(self, query: str = "") -> None:
        """Open the docked palette with ``query`` and give it the keys."""
        if self._focus_target in {"main", "composer"}:
            self._palette_opener = self._focus_target
        self._palette_open = True
        self._palette_query = query
        self._palette_selected = 0
        self._menu_index = 0
        self._palette_results_focused = False
        self._palette_nav = False
        self._palette_note = ""
        self._palette_refresh_dispatches()
        self._focus_target = "palette"
        if self._app is not None and self.palette_window is not None:
            self._app.layout.focus(self.palette_window)
        self._invalidate()

    def _close_palette(self) -> None:
        """Close the palette and restore the focus stop that opened it."""
        opener = self._palette_opener
        self._palette_open = False
        self._palette_query = ""
        self._palette_selected = 0
        self._menu_index = 0
        self._palette_results_focused = False
        self._palette_nav = False
        self._palette_note = ""
        self._focus_target = opener
        if self._app is not None:
            window = (
                self.conversation_window if opener == "main" else self.input_area
            )
            if window is not None:
                self._app.layout.focus(window)
        self._invalidate()

    def _palette_refresh_dispatches(self) -> None:
        """Refresh the ids D2 offers, without blocking the palette opening."""
        fetch = getattr(self.client, "active_dispatches", None)
        if fetch is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._load_palette_dispatches(fetch))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _load_palette_dispatches(self, fetch: Callable[[], Awaitable[object]]) -> None:
        try:
            rows = await fetch()
        except (SuperAgentError, OSError):
            # The ids are a convenience: an unreachable server offers none.
            return
        if self._palette_open and isinstance(rows, list):
            self._palette_dispatches = [row for row in rows if isinstance(row, dict)]
            self._invalidate()

    def _palette_lines(self) -> list[tuple[str, str]]:
        """Styled rows of the palette region, query line to footer."""
        state = self._palette_state()
        lines = render_palette(state, self._columns(), self._locale())
        # The label belongs to the palette's own copy, so the style rule
        # cannot drift from what render_palette drew.
        label = t(PALETTE_STRINGS, "label", self._locale())
        rows: list[tuple[str, str]] = []
        for index, line in enumerate(lines):
            if index == 0 and line.lstrip().startswith(label):
                rows.append(("class:palette-label", line))
                continue
            if index == len(lines) - 1:
                style = "class:palette-footer"
                if state.note:
                    style = (
                        "class:palette-error"
                        if state.note.endswith("id") or "dispatch id" in state.note
                        else "class:palette-note"
                    )
                rows.append((style, line))
                continue
            if line.lstrip().startswith("▸"):
                rows.append(("class:slash-menu-selected", line))
            else:
                rows.append(("class:slash-menu", line))
        return rows

    def _palette_fragments(self) -> FormattedText:
        fragments: list[tuple[str, str]] = []
        for index, (style, text) in enumerate(self._palette_lines()):
            if index:
                fragments.append(("", "\n"))
            fragments.append((style, text))
        return FormattedText(fragments)

    def _palette_accept(self) -> None:
        """Tab: insert the highlighted token, never leave the palette."""
        state = self._palette_state()
        if state.kind == KIND_ARGUMENTS:
            if not state.rows:
                return
            partial = state.query.partition(" ")[2].strip()
            # D2: a prefix that names more than one id inserts nothing; the
            # footer already asks for more of it.
            if partial and len(state.rows) != 1:
                return
            option = state.rows[state.selected].name
            token = state.query.partition(" ")[0]
            self._palette_query = f"{token} {option}"
            self._palette_selected = 0
            self._invalidate()
            return
        if not state.rows:
            return
        row = state.rows[state.selected]
        command = next(
            (item for item in PALETTE_COMMANDS if item.name == row.name), None
        )
        if command is None:
            return
        keep_slash = self._palette_query.startswith("/")
        token = (command.slash if keep_slash and command.slash else command.name)
        suffix = " " if command.requires_arg else ""
        self._palette_query = token + suffix
        self._palette_selected = 0
        self._invalidate()

    def _palette_slash(self, token: str, name: str) -> str:
        """The executable slash spelling of a palette command token.

        The spelling the operator typed wins when the composer knows it
        (``/jobs``, ``/j``, ``/attach``, ``jobs``), so an alias keeps its own
        semantics; a canonical palette name (``tickets``) runs as the
        command's slash name, and a command without one as ``/<name>``.
        """
        item = next((entry for entry in PALETTE_COMMANDS if entry.name == name), None)
        if item is None:
            return token if token.startswith("/") else f"/{token}"
        folded = token.strip().lstrip("/").casefold()
        for spelling in (item.slash, *item.aliases):
            if spelling and spelling.startswith("/") and spelling[1:].casefold() == folded:
                return spelling
        return item.slash or f"/{item.name}"

    def _palette_needs_id(self, command: str) -> None:
        self._palette_note = t(PALETTE_STRINGS, "needsId", self._locale()).format(
            command=command
        )
        self._palette_query = f"{command} "
        self._invalidate()

    def _palette_submit(self, text: str) -> None:
        self._close_palette()
        self.spawn_submit(text.strip())

    def _palette_run_or_insert(self) -> None:
        """Enter: run a complete form, or insert an id / show the hint (D2)."""
        state = self._palette_state()
        if state.kind == KIND_ARGUMENTS:
            self._palette_run_arguments(state)
        else:
            self._palette_run_command(state)

    def _palette_note_for(self, key: str, **values: object) -> None:
        self._palette_note = t(PALETTE_STRINGS, key, self._locale()).format(**values)
        self._invalidate()

    def _palette_run_arguments(self, state: PaletteState) -> None:
        """Enter on an argument query (D2, SPEC §6.4)."""
        token, _, rest = state.query.partition(" ")
        partial = rest.strip()
        names = [row.name for row in state.rows]
        decision = state.command in {"approve", "reject"}
        if decision:
            # §6.4: approve/reject run only an offered awaiting_human id.
            complete = partial in names
        else:
            # A complete form: an offered id typed in full, an index, or
            # free-form arguments (``--attention``, ``watch <id>``) that no
            # offered id completes.
            complete = bool(partial) and (
                partial in names or partial.lstrip("#").isdigit() or not names
            )
        if complete:
            slash = self._palette_slash(token, state.command)
            self._palette_submit(f"{slash} {partial}")
        elif names and partial and len(names) > 1:
            # D2: an ambiguous prefix inserts nothing and runs nothing.
            self._palette_note_for("ambiguousCount", count=len(names))
        elif names:
            # D2: an incomplete form inserts the highlighted id first.
            self._palette_query = f"{token} {names[state.selected]}"
            self._palette_selected = 0
            self._palette_note = ""
            self._invalidate()
        elif decision and partial:
            self._palette_note_for("needsId", command=state.command)
        elif decision:
            self._palette_needs_id(state.command)
        else:
            self._palette_submit(self._palette_slash(token, state.command))

    def _palette_run_command(self, state: PaletteState) -> None:
        """Enter on a command row: run its bare form, or insert it (§6.4)."""
        row = state.rows[state.selected] if state.rows else None
        command = next(
            (item for item in PALETTE_COMMANDS if row and item.name == row.name), None
        )
        if command is None:
            return
        if command.requires_arg:
            keep_slash = self._palette_query.startswith("/")
            token = command.slash if keep_slash and command.slash else command.name
            self._palette_query = token + " "
            self._palette_note_for("needsId", command=command.name)
            return
        typed = self._palette_query.strip()
        if canonical_command(typed) == command.name:
            self._palette_submit(self._palette_slash(typed, command.name))
        else:
            self._palette_submit(command.slash or f"/{command.name}")

    def _palette_move(self, delta: int) -> None:
        state = self._palette_state()
        if not state.rows:
            return
        self._palette_selected = max(
            0, min(len(state.rows) - 1, self._palette_selected + delta)
        )
        self._menu_index = self._palette_selected
        self._invalidate()

    def _palette_type(self, text: str) -> None:
        if not text:
            return
        if self._palette_results_focused:
            self._palette_results_focused = False
        self._palette_query += text
        self._palette_selected = 0
        self._palette_note = ""
        self._invalidate()

    def _palette_backspace(self) -> None:
        if self._palette_query:
            self._palette_query = self._palette_query[:-1]
            self._palette_selected = 0
            self._palette_note = ""
            self._invalidate()

    def _composer_slash_query(self) -> str:
        if self.input_area is None:
            return ""
        first = self.input_area.buffer.text.split("\n", 1)[0]
        if not first.startswith("/") or " " in first:
            return ""
        return first

    def _menu_query(self) -> str:
        if self._palette_open:
            return self._palette_query.lstrip("/").lower()
        first = self._composer_slash_query()
        return first[1:].lower() if first else ""

    def _menu_items(self) -> list[tuple[str, str]]:
        """Compatibility view of the palette rows (slash name, hint)."""
        if self._palette_open:
            state = self._palette_state()
            return [
                (row.slash or row.name, row.hint or row.state)
                for row in state.rows
            ]
        query = self._composer_slash_query()
        if not query:
            return []
        state = self._palette_state(query)
        return [
            (row.slash or row.name, row.hint or row.state)
            for row in state.rows
        ]

    def _move_menu(self, delta: int) -> None:
        if self._palette_open:
            self._palette_move(delta)
            return
        items = self._menu_items()
        if not items:
            return
        self._menu_index = max(0, min(len(items) - 1, self._menu_index + delta))
        self._invalidate()

    def _menu_fragments(self) -> FormattedText:
        if self._palette_open:
            return self._palette_fragments()
        query = self._composer_slash_query()
        if not query:
            return FormattedText([])
        # A directly set composer line (tests and restored sessions) still draws
        # the shared palette instead of the retired prefix menu.
        saved = (
            self._palette_open,
            self._palette_query,
            self._palette_selected,
        )
        try:
            self._palette_open = True
            self._palette_query = query
            self._palette_selected = self._menu_index
            return self._palette_fragments()
        finally:
            self._palette_open, self._palette_query, self._palette_selected = saved

    def _menu_row(self, marker: str, name: str, hint: str) -> str:
        """Kept for the old callers: render one command row of the palette."""
        return f"{marker}{name:<12} {hint}".rstrip()

    def _move_focus(self, delta: int) -> None:
        """Tab / Shift+Tab between the transcript and the composer."""
        order = ("main", "composer")
        try:
            index = order.index(self._focus_target)
        except ValueError:
            index = 1
        target = order[(index + delta) % len(order)]
        self._focus_target = target
        if self._app is not None:
            window = self.conversation_window if target == "main" else self.input_area
            if window is not None:
                self._app.layout.focus(window)
        self._invalidate()

    def build_application(
        self,
        *,
        input: Any = None,
        output: Any = None,
        key_bindings: KeyBindings | None = None,
        style: Style | None = None,
        history: Any = None,
    ) -> Application[Any]:
        """Build (but do not run) the full-screen application."""
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            history_obj = history or FileHistory(str(self._history_path))
        except OSError:
            history_obj = history
        self.input_area = TextArea(
            multiline=True,
            prompt=self._composer_prompt,
            history=history_obj,
            completer=self.completer,
            # The slash menu is our own window. Built-in completion would
            # rewrite the composer when the arrow keys move.
            complete_while_typing=False,
            # SPEC §5.5: while a permission is pending the composer gives up
            # every key but a/s/d and Escape. Read-only drops TextArea's own
            # typing, newline and cursor bindings so Enter and Ctrl+J cannot
            # leave a draft behind the prompt.
            read_only=Condition(lambda: self._pending is not None),
            wrap_lines=True,
            height=Dimension(min=1, preferred=1, max=MAX_COMPOSER_HEIGHT),
            style="class:composer",
            get_line_prefix=lambda _lineno, _wrap: (
                [("class:composer-border", "│")] if self._border_enabled() else []
            ),
            input_processors=[
                _ComposerPlaceholder(
                    locale=self._locale,
                    reserved=lambda: 1 if self._border_enabled() else 0,
                )
            ],
        )
        self.input_area.window.right_margins = [_ComposerWall(self._border_enabled)]
        self._sync_composer_height()
        self.input_area.buffer.on_text_changed.add_handler(
            lambda _buffer: self._on_composer_changed()
        )
        self.conversation_window = Window(
            content=ConversationControl(self._conversation_rows_for_control),
            # The control already wrapped each row to the content width.
            wrap_lines=False,
            right_margins=[
                ScrollbarMargin(
                    display_arrows=Condition(self._transcript_overflows)
                )
            ],
            scroll_offsets=ScrollOffsets(top=0, bottom=0),
            style="class:conversation",
        )
        self.palette_window = Window(
            content=_PaletteControl(text=self._palette_fragments),
            style="class:slash-menu",
            wrap_lines=False,
        )
        top_border = Window(
            content=FormattedTextControl(text=lambda: self._border_fragments("╭", "╮")),
            height=1,
            style="class:composer-border",
        )
        bottom_border = Window(
            content=FormattedTextControl(text=lambda: self._border_fragments("╰", "╯")),
            height=1,
            style="class:composer-border",
        )
        self.status_window = Window(
            content=FormattedTextControl(text=self._status_fragments),
            height=1,
            wrap_lines=False,
            style="class:statusbar",
        )
        bordered = Condition(self._border_enabled)
        palette_open = Condition(lambda: self._palette_open)
        body = HSplit(
            [
                self.conversation_window,
                ConditionalContainer(content=self.palette_window, filter=palette_open),
                ConditionalContainer(content=top_border, filter=bordered),
                self.input_area,
                ConditionalContainer(content=bottom_border, filter=bordered),
                self.status_window,
            ]
        )
        bindings = key_bindings or self._key_bindings()
        self._app = Application(
            layout=Layout(body, focused_element=self.input_area),
            key_bindings=bindings,
            input=input,
            output=output,
            full_screen=True,
            mouse_support=False,
            # Elapsed time and the conducting spinner are computed on redraw.
            refresh_interval=self._refresh_interval(),
            style=style or build_default_style(),
            # A resize does not change the buffer, so the height is recomputed
            # on the render that sees the new width.
            before_render=lambda _app: self._sync_composer_height(),
        )
        # Escape flushes faster than the 0.5s default so denial/interrupt feel
        # immediate; Esc then Enter still works when pressed quickly, and the
        # kitty Shift+Enter sequence / Ctrl+J are the timing-free newline keys.
        self._app.ttimeoutlen = 0.1
        # The Escape bindings are not eager (Escape is also the prefix of the
        # Alt chords). The 0.1 s chord window of SPEC §5.5 is the input
        # parser's: a lone ESC byte is held for ttimeoutlen, so Esc+M or
        # Esc+Enter typed inside it arrive together and match their chord.
        # Once the parser has let a bare Escape go, nothing else is coming,
        # so the key processor resolves it at once instead of waiting
        # prompt_toolkit's default second.
        self._app.timeoutlen = 0.01
        self._app.tui_controller = self
        return self._app

    def _key_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        pending = Condition(lambda: self._pending is not None)
        not_pending = Condition(lambda: self._pending is None)

        palette_open = Condition(lambda: self._palette_open)
        palette_closed = Condition(lambda: not self._palette_open)
        reply_escape = Condition(
            lambda: self._reply_active() and not self._palette_open
        )
        # Enter sends the composer line only from the composer stop; the
        # transcript stop scrolls with j/k instead (SPEC §5.3, §5.4).
        composer_focused = Condition(self._composer_has_focus)
        transcript_focused = Condition(self._transcript_has_focus)
        # No Escape binding is eager: Escape is also the prefix of the Meta
        # chords (Alt+M, Alt+N, Esc Enter), and an eager bare-Escape handler
        # would fire before the chord's second key arrives. A bare Escape
        # still acts as soon as the 0.1 s escape flush (ttimeoutlen) passes.

        @kb.add("c-k", filter=not_pending & palette_closed)
        def _open_palette(event: KeyPressEvent) -> None:
            del event
            self._open_palette("")

        @kb.add("/", filter=not_pending & palette_closed)
        def _slash(event: KeyPressEvent) -> None:
            buffer = (
                self.input_area.buffer
                if self.input_area is not None
                else event.current_buffer
            )
            document = buffer.document
            if document.cursor_position_col == 0 and not document.current_line_before_cursor:
                self._open_palette("/")
                return
            buffer.insert_text("/")

        @kb.add("tab", filter=not_pending & palette_open, eager=True)
        def _palette_tab(event: KeyPressEvent) -> None:
            del event
            self._palette_accept()

        @kb.add("s-tab", filter=not_pending & palette_open)
        def _palette_shift_tab(event: KeyPressEvent) -> None:
            # D3: Shift+Tab never leaves the palette trap.
            del event

        @kb.add("enter", filter=not_pending & palette_open, eager=True)
        def _palette_enter(event: KeyPressEvent) -> None:
            del event
            self._palette_run_or_insert()

        @kb.add("escape", filter=not_pending & palette_open)
        def _palette_escape(event: KeyPressEvent) -> None:
            del event
            self._close_palette()

        @kb.add("up", filter=not_pending & palette_open, eager=True)
        def _palette_up(event: KeyPressEvent) -> None:
            del event
            self._palette_move(-1)

        @kb.add("down", filter=not_pending & palette_open, eager=True)
        def _palette_down(event: KeyPressEvent) -> None:
            del event
            self._palette_move(1)

        @kb.add("c-down", filter=not_pending & palette_open)
        def _palette_results(event: KeyPressEvent) -> None:
            del event
            self._palette_results_focused = True
            self._invalidate()

        @kb.add("c-up", filter=not_pending & palette_open)
        def _palette_query_focus(event: KeyPressEvent) -> None:
            del event
            self._palette_results_focused = False
            self._invalidate()

        @kb.add("escape", "n", filter=not_pending & palette_open)
        def _palette_nav(event: KeyPressEvent) -> None:
            del event
            self._palette_nav = not self._palette_nav
            self._invalidate()

        @kb.add("backspace", filter=not_pending & palette_open)
        @kb.add("c-h", filter=not_pending & palette_open)
        def _palette_backspace(event: KeyPressEvent) -> None:
            del event
            self._palette_backspace()

        @kb.add(Keys.Any, filter=not_pending & palette_open)
        def _palette_char(event: KeyPressEvent) -> None:
            data = event.data
            if data in {"j", "k"} and (
                self._palette_results_focused or self._palette_nav
            ):
                self._palette_move(1 if data == "j" else -1)
                return
            # A paste arrives as one event; the query is one line, so a
            # multi-line paste keeps its first line instead of being dropped.
            if data and len(data) > 1:
                data = data.replace("\r\n", "\n").replace("\r", "\n").split("\n", 1)[0]
                data = "".join(char for char in data if char.isprintable())
            if data and data.isprintable() and data not in {"\r", "\n", "\t"}:
                self._palette_type(data)

        @kb.add("enter", filter=not_pending & palette_closed & composer_focused)
        def _submit(event: KeyPressEvent) -> None:
            buffer = self.input_area.buffer if self.input_area is not None else event.current_buffer
            text = buffer.text
            if text.strip():
                buffer.append_to_history()
            buffer.reset()
            self.spawn_submit(text)

        @kb.add("tab", filter=palette_closed)
        def _tab_focus(event: KeyPressEvent) -> None:
            # SPEC §5.5: in the composer Tab completes an ``@path`` (or a
            # command argument) when a completion applies; only otherwise is
            # it the next focus stop of §5.4.
            if self._composer_has_focus() and self._composer_completes():
                get_by_name("menu-complete").call(event)
                return
            self._move_focus(1)

        @kb.add("enter", filter=not_pending & palette_closed & transcript_focused)
        def _transcript_enter(event: KeyPressEvent) -> None:
            # The transcript has no row to activate; Enter never sends the
            # composer draft from here.
            del event

        @kb.add("j", filter=palette_closed & transcript_focused)
        @kb.add("down", filter=palette_closed & transcript_focused)
        def _transcript_down(event: KeyPressEvent) -> None:
            del event
            self._page(1)

        @kb.add("k", filter=palette_closed & transcript_focused)
        @kb.add("up", filter=palette_closed & transcript_focused)
        def _transcript_up(event: KeyPressEvent) -> None:
            del event
            self._page(-1)

        @kb.add("up", filter=not_pending & palette_closed & composer_focused)
        def _history_back(event: KeyPressEvent) -> None:
            buffer = event.current_buffer
            if buffer.document.cursor_position_row == 0:
                buffer.history_backward()
            else:
                buffer.cursor_up()

        @kb.add("down", filter=not_pending & palette_closed & composer_focused)
        def _history_forward(event: KeyPressEvent) -> None:
            buffer = event.current_buffer
            if buffer.document.cursor_position_row >= buffer.document.line_count - 1:
                buffer.history_forward()
            else:
                buffer.cursor_down()

        @kb.add("c-j", filter=not_pending & palette_closed)
        @kb.add("escape", "enter", filter=not_pending & palette_closed)
        def _newline(event: KeyPressEvent) -> None:
            buffer = self.input_area.buffer if self.input_area is not None else event.current_buffer
            buffer.insert_text("\n")

        # Shift+Enter reaches a raw-mode application as one of two CSI
        # sequences when the terminal reports it (kitty keyboard protocol or
        # xterm modifyOtherKeys). Terminals that send plain CR cannot
        # distinguish it; the hint documents Ctrl+J / Esc,Enter for those.
        for sequence in (
            ["escape", "[", "1", "3", ";", "2", "u"],
            ["escape", "[", "2", "7", ";", "2", ";", "1", "3", "~"],
        ):
            _bind_newline(kb, sequence, not_pending & palette_closed)
            kb.add(*sequence, filter=pending | palette_open)(_ignore_key)
        # A newline chord inside the palette is inert: it must neither close
        # the palette (and run the composer) nor reach the hidden draft.
        kb.add("escape", "enter", filter=not_pending & palette_open)(_ignore_key)

        @kb.add("c-c")
        def _ctrl_c(event: KeyPressEvent) -> None:
            if self.input_area is not None and self.input_area.buffer.text:
                self.input_area.buffer.reset()
                self._invalidate()
            # Clearing a draft is not a separate gesture: an active watch is
            # interrupted and the double-press timer is armed on this press.
            if self.note_ctrl_c():
                event.app.exit(result=0)

        @kb.add("c-d")
        def _ctrl_d(event: KeyPressEvent) -> None:
            if self.input_area is not None and not self.input_area.buffer.text:
                event.app.exit(result=0)

        @kb.add("escape", filter=not_pending & reply_escape)
        def _escape_reply(event: KeyPressEvent) -> None:
            del event
            self._dismiss_reply()

        watching = Condition(
            lambda: self._watch_task is not None and not self._watch_task.done()
        )
        not_watching = Condition(
            lambda: self._watch_task is None or self._watch_task.done()
        )
        idle_escape = Condition(
            lambda: not self._palette_open and not self._reply_active()
        )

        @kb.add("escape", filter=not_pending & idle_escape & watching)
        def _escape(event: KeyPressEvent) -> None:
            del event
            if not self._interrupt_confirm:
                self._interrupt_confirm = True
                self.conversation.add(
                    NoticeBlock(text=_notice("pressEscAgain", self._locale()))
                )
                self._invalidate()
                return
            self._interrupt_confirm = False
            self._confirm_interrupt()

        @kb.add("escape", filter=not_pending & idle_escape & not_watching)
        def _escape_idle(event: KeyPressEvent) -> None:
            del event
            self._interrupt_confirm = False

        @kb.add("escape", filter=pending)
        def _escape_pending(event: KeyPressEvent) -> None:
            # A bare Escape denies once the escape flush passes. A Meta chord
            # (Alt+M, Esc Enter, a CSI key the parser does not know) is caught
            # by the chord binding below and answers nothing.
            del event
            self.resolve_permission("d")

        # SPEC §5.5: while a permission is pending Enter, Up/Down, Alt+M and
        # every newline route are off. TextArea's own Enter / Ctrl+J / arrow
        # bindings would otherwise still edit the draft or move the caret, so
        # each key is consumed here explicitly.
        @kb.add("escape", Keys.Any, filter=pending)
        @kb.add("enter", filter=pending)
        @kb.add("c-j", filter=pending)
        @kb.add("up", filter=pending & ~transcript_focused)
        @kb.add("down", filter=pending & ~transcript_focused)
        def _pending_inert(event: KeyPressEvent) -> None:
            del event

        @kb.add("pageup")
        def _page_up(event: KeyPressEvent) -> None:
            del event
            self._page(-10)

        @kb.add("pagedown")
        def _page_down(event: KeyPressEvent) -> None:
            del event
            self._page(10)

        @kb.add("c-t")
        def _task_list(event: KeyPressEvent) -> None:
            del event
            self.spawn_submit("/jobs")

        @kb.add("c-o")
        def _toggle_output(event: KeyPressEvent) -> None:
            del event
            self._toggle_newest_output()

        empty = Condition(
            lambda: self.input_area is not None and not self.input_area.buffer.text
        )

        @kb.add("?", filter=not_pending & empty & palette_closed)
        def _question_help(event: KeyPressEvent) -> None:
            del event
            self.spawn_submit("/help")

        @kb.add("s-tab", filter=palette_closed)
        def _shift_tab(event: KeyPressEvent) -> None:
            del event
            self._move_focus(-1)

        @kb.add("escape", "m", filter=not_pending)
        def _alt_m(event: KeyPressEvent) -> None:
            del event
            self._cycle_mode()

        for key in ("a", "s", "d"):
            handler = self._permission_handler(key)

            @kb.add(key, filter=pending)
            def _decision(event: Any, handler: Callable[[str], bool] = handler) -> None:
                del event
                handler(key)

        return kb

    def _composer_has_focus(self) -> bool:
        app, area = self._app, self.input_area
        if app is None or area is None:
            return True
        return bool(app.layout.has_focus(area))

    def _composer_completes(self) -> bool:
        """True when the composer's completer has something for the caret."""
        area = self.input_area
        if area is None or self._pending is not None:
            return False
        buffer = area.buffer
        if buffer.complete_state is not None:
            return True
        event = CompleteEvent(completion_requested=True)
        completions = self.completer.get_completions(buffer.document, event)
        return next(iter(completions), None) is not None

    def _transcript_has_focus(self) -> bool:
        app, window = self._app, self.conversation_window
        if app is None or window is None:
            return False
        return bool(app.layout.has_focus(window))

    def _insert_newline(self) -> None:
        if self.input_area is not None:
            self.input_area.buffer.insert_text("\n")

    def _permission_handler(self, key: str) -> Callable[[str], bool]:
        def handler(_key: str) -> bool:
            return self.resolve_permission(key)

        return handler

    def _page(self, delta: int) -> None:
        window = self.conversation_window
        if window is None:
            return
        info = window.render_info
        if self._follow_tail:
            top = info.vertical_scroll if info is not None else 0
        else:
            # Use our logical position, not the viewport top: the viewport top
            # only changes once the marker row leaves it, so reading it back
            # would make repeated PgDn presses target the same row forever.
            top = self._scroll_offset
        content = info.content_height if info is not None else 0
        height = info.window_height if info is not None else 0
        offset = max(0, top + delta)
        if delta > 0 and content and height and offset >= content - height:
            self._follow_tail = True
            self._scroll_offset = 10**9
        else:
            self._follow_tail = False
            self._scroll_offset = offset
            # Move the viewport now: the marker alone only scrolls when the
            # row would otherwise be off-screen.
            window.vertical_scroll = offset
        self._invalidate()

    def _exit_summary_disabled(self) -> bool:
        if self.no_exit_summary:
            return True
        flag = os.environ.get("CONDUCTOR_NO_EXIT_SUMMARY", "").strip().lower()
        return flag in {"1", "true", "yes", "on"}

    def _print_exit_summary(self) -> None:
        """After the alt screen, print the last card and how to resume it."""
        if self._exit_summary_disabled():
            return
        blocks = list(self.conversation.blocks)
        card: EndBlock | ProgressBlock | None = None
        for block in reversed(blocks):
            if isinstance(block, (EndBlock, ProgressBlock)):
                card = block
                break
        if card is None:
            return
        now = self._clock()
        if isinstance(card, EndBlock):
            rows = self._present_end(
                card,
                self._linked_progress(blocks, blocks.index(card)),
                now=now,
                fold_tables=False,
            )
        else:
            rows = self._present_progress(card, now=now)
        lines = [row_text(row).strip() for row in rows if row_text(row).strip()][:30]
        if not lines:
            return
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.write("resume with: cond --continue\n")
        sys.stdout.flush()

    async def run(self) -> int:
        """Start the session and run the application until the operator exits."""
        await self.startup()
        app = self.build_application()
        try:
            return int(await app.run_async())
        finally:
            # prompt_toolkit has already left the alt screen.
            self._print_exit_summary()
            await self.close()


async def run_tui(
    *,
    server_url: str,
    user_id: str,
    no_splash: bool = False,
    continue_session: bool = False,
    no_exit_summary: bool = False,
    reduced_motion: bool | None = None,
) -> int:
    tui = SuperAgentTui(
        server_url=server_url,
        user_id=user_id,
        no_splash=no_splash,
        continue_session=continue_session,
        no_exit_summary=no_exit_summary,
        reduced_motion=reduced_motion,
    )
    return await tui.run()


def _default_write(text: str) -> None:
    """Write raw text (no implicit newline) for ``cond -p`` output."""
    sys.stdout.write(text)
    sys.stdout.flush()


async def run_print_mode(
    *,
    server_url: str,
    user_id: str,
    prompt: str,
    timeout: float = WATCH_TIMEOUT_S,
    write: Callable[[str], None] | None = None,
    client: Any = None,
) -> int:
    """``cond -p``: one prompt, follow the Ticket, print the result, exit.

    ``write`` receives raw text (streamed model text arrives chunk by chunk);
    line-oriented output writes its own newline so tokens are not split onto
    separate lines.
    """
    output = write or _default_write

    def line(text: str = "") -> None:
        output(text + "\n")

    corpus = SuperAgentClient(server_url, user_id) if client is None else client
    try:
        workspace = await corpus.home_workspace()
        if not isinstance(workspace, str) or not workspace.strip():
            line("server did not return a usable Home workspace")
            return 1
        response = await corpus.daemons()
        daemons = (
            response.get("daemons") if isinstance(response, dict) else []
        )
        if not isinstance(daemons, list):
            daemons = []
        daemons = [row for row in daemons if isinstance(row, dict)]
        daemon_id = ""
        if daemons:
            now = time.time()
            live = [
                row
                for row in daemons
                if (_parse_iso(str(row.get("last_seen_at") or "")) or 0) > now - 180
            ]
            pool = live or daemons
            daemon_id = str(sorted(pool, key=lambda row: str(row.get("last_seen_at") or ""), reverse=True)[0].get("daemon_id") or "")
        if not daemon_id:
            line("no local daemon is registered; cannot run a Ticket")
            return 1
        synthetic, attachments, errors = expand_mentions(prompt, root=Path.cwd())
        del attachments
        for error in errors:
            line(f"attach error: {plain(error)}")
        ticket = await corpus.create_task(
            synthetic,
            title=prompt.splitlines()[0].strip()[:120] if prompt.strip() else "task",
            workspace_id=workspace,
            worker_daemon_id=daemon_id,
        )
        if not isinstance(ticket, dict):
            line("server returned a malformed Ticket response")
            return 1
        ticket_id = str(ticket.get("ticket_id") or "")
        if not ticket_id:
            line("server did not return a Ticket id")
            return 1
        line(f"ticket {plain(ticket_id)}")
        final_status = ""
        wrote_text = False
        sanitizer = TokenSanitizer()
        async for event in watch_ticket(corpus, ticket_id, timeout=timeout):
            if isinstance(event, OutputChunk):
                if event.kind == "text":
                    # Streamed model text is untrusted terminal input; the
                    # incremental sanitizer also handles sequences split
                    # across frames.
                    chunk = sanitizer.feed(event.text)
                    if chunk:
                        output(chunk)
                        wrote_text = True
                continue
            if isinstance(event, OutputReset):
                # The live output restarted: what was printed so far is a
                # closed fragment. Drop the half-parsed escape state (a fresh
                # sanitizer) so it cannot swallow, or be completed by, the text
                # that follows, and end the line so the two never read as one.
                sanitizer = TokenSanitizer()
                if wrote_text:
                    line()
                line(plain(output_reset_notice(event.reason)))
                wrote_text = False
                continue
            if isinstance(event, ToolCall) and event.names:
                if wrote_text:
                    line()
                line("tool: " + ", ".join(plain(name) for name in event.names))
                wrote_text = False
                continue
            if isinstance(event, Activity):
                detail = f" — {plain(event.detail)}" if event.detail else ""
                if wrote_text:
                    line()
                line(f"• {plain(event.kind)} {plain(event.summary)}{detail}")
                wrote_text = False
                continue
            if isinstance(event, OutputError):
                if wrote_text:
                    line()
                line(f"worker error: {plain(event.text)}")
                wrote_text = False
                continue
            if isinstance(event, Gap):
                # Dropped output: the text before it is a closed fragment, as
                # after a reset.
                sanitizer = TokenSanitizer()
                if wrote_text:
                    line()
                line(plain(event.text))
                wrote_text = False
                continue
            if isinstance(event, StreamNotice):
                continue
            if isinstance(event, TimedOut):
                if wrote_text:
                    line()
                line("watch timed out")
                wrote_text = False
                continue
            if isinstance(event, End):
                final_status = event.status
                if wrote_text:
                    line()
                line(f"status: {plain(event.status)}")
                if event.summary:
                    line(plain(event.summary))
                if event.error:
                    line(f"error: {plain(event.error)}")
                if event.question:
                    line(f"needs you: {plain(event.question)}")
        return 0 if final_status == "completed" else 1
    except SuperAgentError as exc:
        line(f"error: {plain(exc)}")
        return 1
    except Exception as exc:
        line(f"error: {plain(exc)}")
        return 1
    finally:
        if client is None:
            await corpus.close()


def _as_int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _parse_iso(value: str) -> float:
    from datetime import datetime

    if not value:
        return 0.0
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


__all__ = [
    "DEFAULT_STYLE",
    "HELP_TEXT",
    "MIN_TUI_COLUMNS",
    "SuperAgentTui",
    "TuiStatus",
    "choose_mode",
    "detect_mode",
    "estimate_tokens",
    "run_print_mode",
    "run_tui",
]
