"""Interactive shell for Conductor's Super-agent surface."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from rich.cells import cell_len
from rich.console import Console
from rich.errors import MarkupError
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text

from . import tui_jobs as _tui_jobs
from .client import (
    ActivityStreamEvent,
    ActivityStreamUnavailableError,
    SuperAgentClient,
    SuperAgentError,
)

# The control-character filters live in :mod:`ptc_cli.superagent.text`; the
# historic private names stay importable here for the line shell's tests.
from .text import TokenSanitizer as _TokenSanitizer
from .text import plain as _plain
from .text import strip_protocol_trailers
from .text import text as _text
from .tui_home import home_rows
from .tui_i18n import resolve_locale, t
from .tui_jobs import job_detail_rows, jobs_rows, watch_header_rows
from .tui_local import run_shell
from .tui_palette import COMMANDS, PaletteCommand, command_hint, rank_commands
from .tui_palette import wrap as _wrap_text
from .tui_permissions import PermissionManager
from .tui_theme import status_word


# Help order is the S04 command table. ``HELP_COMMANDS`` stays the slash name
# (or the canonical name, when a command has no slash name) so older tests can
# still import the list. It is derived, not a second copy of the table.
def _help_label(command: PaletteCommand) -> str:
    return command.slash or command.name


HELP_COMMANDS: tuple[str, ...] = tuple(_help_label(command) for command in COMMANDS)


def _shell_label(command: PaletteCommand) -> str:
    """The spelling the line shell runs: always a slash command.

    The table names ``approve`` and ``reject`` without a slash because the
    TUI palette runs them by name. A bare word in the line shell is a Ticket,
    so ``/help`` and the suggestions teach ``/approve`` and ``/reject``.
    """
    return command.slash or f"/{command.name}"


# Table commands that only mean something in the full-screen TUI: they act
# on its transcript or its panes. The line shell names them and says so.
# Commands both shells run that the S04 palette table does not carry (R6
# ``/roles``). They follow the palette rows in ``/help``, so the line shell
# lists every command the TUI ``/help`` lists.
_HELP_EXTRAS: tuple[tuple[str, str], ...] = (("/roles", "help_roles"),)
_TUI_ONLY: frozenset[str] = frozenset({"/clear", "/details", "/model", "/compact", "/expand", "/collapse"})
# Hints the palette writes for the TUI, reworded for the line shell.
_LINE_HINTS: dict[str, str] = {"help": "hint_help", "quit": "hint_quit"}
_HELP_NAME_WIDTH = 12
_SUGGESTION_LIMIT = 3
_MINUTE_S = 60
_HOUR_S = 3600
_DAEMON_FRESH_S = 180
_MIN_PIPE_ROWS = 2
_JOBS_WITH_TARGET = 2
_JOBS_REPLY_PARTS = 3
_ESC = 0x1B

# Every sentence the line shell composes. Server text is interpolated already
# sanitized; the words around it come from here so en and zh-CN stay in step.
STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "unsupported": "the server does not support this yet",
        "server_line": "[dim]server:[/dim] {url}  [dim]user:[/dim] {user}",
        "startup_unavailable": "[yellow]startup summary unavailable:[/yellow] {error}",
        "startup_counts": (
            "[dim]signals:[/dim] {sources} sources ([green]{enabled} enabled[/green])  "
            "[dim]jobs:[/dim] attention={attention} running={running} "
            "queued={queued} failed={failed} blocked={blocked}"
        ),
        "no_recent_daemon": ("[yellow]no daemon was seen in the last 3 minutes; Tickets may stay queued until one starts[/yellow]"),
        "execution_daemon": "[dim]execution:[/dim] ● local daemon {label} [dim](BYOM)[/dim]",
        "no_daemon": ("[yellow]✖ no local daemon registered; cloud Worker may be disabled on this deployment[/yellow]"),
        "startup_hint": ("[dim]/help for commands; plain text creates a Super-agent Ticket. Use /jobs watch <id> to follow it.[/dim]"),
        "dispatched": "[green]dispatched[/green] {ticket}  [dim]{state} · {title}{daemon}[/dim]",
        "dispatched_daemon": " · daemon {label}",
        "follow_hint": "[dim]use /jobs watch <id> to follow this Ticket[/dim]",
        "no_job_index": "no job at index {value}; run /jobs first",
        "watch_timeout": "[yellow]watch timed out; the Ticket is still running[/yellow]",
        "stream_polling": "[dim]live activity stream unavailable; polling the event record[/dim]",
        "poll_retrying": "[dim]lost contact ({error}); retrying[/dim]",
        "ticket_missing": "[yellow]this Ticket no longer exists[/yellow]",
        "needs_you": "[yellow]◆ needs you:[/yellow] {question}",
        "output_restarted": "live output restarted",
        "output_restarted_reason": "live output restarted ({reason})",
        "tool_line": "[dim]⚙ tool: {names}[/dim]",
        "worker_error": "[yellow]worker error:[/yellow] {text}",
        "stream_lost": "[dim]live stream lost ({error}); reconnecting[/dim]",
        "stream_failed": "ticket activity stream failed",
        "stream_interrupted": "[dim]live stream interrupted ({error}); reconnecting[/dim]",
        "stop_requested": "[green]stop requested[/green] {ticket}",
        "retry_queued": "[green]retry queued[/green] {ticket}",
        "reply_sent": "[green]reply sent[/green] {ticket}",
        "eval_requeued": "[green]eval requeued[/green] {eval_id}",
        "fleet_unavailable": "[yellow]fleet unavailable:[/yellow] {error}",
        "native_unavailable": "[yellow]native runtimes unavailable:[/yellow] {error}",
        "daemons_unavailable": "[yellow]daemons unavailable:[/yellow] {error}",
        "job_list_cleared": "[dim]job list cleared[/dim]",
        "unknown": "unknown command {command}; try /help",
        "tui_only": "{command} works in the full-screen TUI only",
        "tui_only_mark": "TUI only",
        "hint_help": "show these commands",
        "hint_quit": "quit the line shell",
        "usage_attach": "usage: /attach <id|index>",
        "usage_decision": "usage: /{decision} <dispatch id>",
        "decision_requested": "{decision} requested for {id}",
        "unknown_suggest": "unknown command {command}; did you mean {suggestions}?",
        "no_ticket": "no Ticket",
        "no_progress": "no progress for {ticket}",
        "no_active_dispatches": "no active dispatches",
        "dispatch_not_found": "dispatch not found",
        "no_saved_sessions": "no saved sessions",
        "saved_sessions": "saved sessions (newest first):",
        "resume_with": "resume with /resume <id>",
        "session_row": "  {session}  {title}  ticket {ticket}",
        "resumed_session": ("resumed session {session} (line mode; last Ticket {ticket}; daemon {daemon})"),
        "goodbye": "\nGoodbye!",
        "goodbye_quit": "Goodbye!",
        "interrupted": "[yellow]interrupted[/yellow]",
        "confirm": ("[yellow]{label} requires confirmation ([a]llow once / [s]ession allow / [d]eny): [/yellow]"),
        "confirm_required": "{label} requires local confirmation; run it interactively",
        "denied": "denied: {label}",
        "raw_usage": "usage: /raw <request>",
        "limit_integer": "--limit must be an integer",
        "limit_positive": "--limit must be positive",
        "unknown_jobs_option": "unknown /jobs option {flag}",
        "usage_jobs_new": "usage: /jobs new <prompt>",
        "usage_jobs_watch": "usage: /jobs watch <id|index>",
        "usage_jobs_retry": "usage: /jobs retry <id|index>",
        "usage_jobs_reply": "usage: /jobs reply <id|index> <text>",
        "usage_jobs_requeue": "usage: /jobs requeue <eval-id>",
        "usage_reply": "usage: /reply <id|index> <text>",
        "usage_retry": "usage: /retry <id|index>",
        "usage_cancel": "usage: /cancel <id|index>",
        "usage_stop": "usage: /stop <id|index>",
        "usage_bang": "usage: !<shell command>",
        "usage_review": ("usage: /review run <line> --by <line> | /review status [line] | /review verdict <id> --verdict MERGE|BLOCK"),
        "usage_merge": "usage: /merge <line> --into <branch> [--test-cmd CMD]",
        "usage_agents_logs": "usage: /agents logs <agent-id> [--lines N]",
        "lines_integer": "--lines must be an integer",
        "unknown_status_group": "unknown status group {group}",
        "conductor_timeout": "conductor {group} status timed out",
        "conductor_missing": "conductor command not found",
        "no_session": "no session starts with {prefix}",
        "ambiguous_session": "ambiguous session prefix {prefix}: {names}",
        "event_record": "[bold]Event record[/bold]",
        "reports": "[bold]reports[/bold]",
        "origin": "origin: {origin}",
        "log_heading": "[bold]log {agent}[/bold]",
        "status_line": (
            "[dim]status:[/dim] attention={attention} running={running} queued={queued} failed={failed} blocked={blocked} completed={completed}"
        ),
        "signals_jobs": ("[dim]Jobs:[/dim] attention={attention} running={running} queued={queued} completed={completed}"),
        "help_also": ("also: /jobs new|watch|retry|reply|requeue, /stop, /agents, /signals, /review, /merge, /new, !<command>"),
        "help_plain": "Plain text creates a Ticket on Home; /jobs watch <id> follows it live.",
        "help_raw": "Prefix a request with /raw to run it as written, skipping refinement.",
        "help_roles": "role registry; /roles set <role> default <name> reason <text>",
        "yes": "yes",
        "no": "no",
        "daemon_auto": "auto",
        "never": "never",
        "fleet_title": "Local agent fleet",
        "native_title": "Native runtimes",
        "daemons_title": "Local daemons",
        "signals_title": "Signals",
        "conductor_failed": "conductor command failed",
        "shell_command": "run shell command: {command}",
        "merge_action": "conductor merge {args}",
        "state_cancelled": "cancelled",
        "stopped_watching": "[dim]stopped watching; the Ticket keeps running[/dim]",
        "attention_mark": "  [?] {question}",
        "col_id": "id",
        "col_role": "role",
        "col_status": "status",
        "col_branch": "branch",
        "col_ahead": "ahead",
        "col_pid": "pid",
        "col_provider": "provider",
        "col_installed": "installed",
        "col_live": "live",
        "col_binary": "binary",
        "col_name": "name",
        "col_last_seen": "last seen",
        "col_source": "source",
        "col_kind": "kind",
        "col_enabled": "enabled",
        "col_state": "state",
    },
    "zh-CN": {
        "unsupported": "服务端暂不支持",
        "server_line": "[dim]服务器:[/dim] {url}  [dim]用户:[/dim] {user}",
        "startup_unavailable": "[yellow]启动摘要不可用:[/yellow] {error}",
        "startup_counts": (
            "[dim]信号:[/dim] {sources} 个来源（[green]{enabled} 个已启用[/green]）  "
            "[dim]任务:[/dim] 需要你={attention} 运行中={running} "
            "排队={queued} 失败={failed} 阻塞={blocked}"
        ),
        "no_recent_daemon": ("[yellow]过去 3 分钟内没有守护进程出现；在有进程启动前，Ticket 可能会一直排队[/yellow]"),
        "execution_daemon": "[dim]执行:[/dim] ● 本地守护进程 {label} [dim](BYOM)[/dim]",
        "no_daemon": "[yellow]✖ 没有注册本地守护进程；此部署可能禁用了云端 Worker[/yellow]",
        "startup_hint": ("[dim]输入 /help 查看命令；纯文本会创建 Super-agent Ticket。用 /jobs watch <id> 跟随它。[/dim]"),
        "dispatched": "[green]已派发[/green] {ticket}  [dim]{state} · {title}{daemon}[/dim]",
        "dispatched_daemon": " · 守护进程 {label}",
        "follow_hint": "[dim]用 /jobs watch <id> 跟随这个 Ticket[/dim]",
        "no_job_index": "序号 {value} 没有任务；请先运行 /jobs",
        "watch_timeout": "[yellow]跟随已超时，Ticket 仍在运行[/yellow]",
        "stream_polling": "[dim]实时活动流不可用；正在轮询事件记录[/dim]",
        "poll_retrying": "[dim]连接中断（{error}）；正在重试[/dim]",
        "ticket_missing": "[yellow]这个 Ticket 已经不存在[/yellow]",
        "needs_you": "[yellow]◆ 需要你:[/yellow] {question}",
        "output_restarted": "实时输出已重新开始",
        "output_restarted_reason": "实时输出已重新开始（{reason}）",
        "tool_line": "[dim]⚙ 工具: {names}[/dim]",
        "worker_error": "[yellow]Worker 错误:[/yellow] {text}",
        "stream_lost": "[dim]实时流已丢失（{error}）；正在重连[/dim]",
        "stream_failed": "Ticket 活动流失败",
        "stream_interrupted": "[dim]实时流已中断（{error}）；正在重连[/dim]",
        "stop_requested": "[green]已请求停止[/green] {ticket}",
        "retry_queued": "[green]已排队重试[/green] {ticket}",
        "reply_sent": "[green]已发送回复[/green] {ticket}",
        "eval_requeued": "[green]已重新排队评估[/green] {eval_id}",
        "fleet_unavailable": "[yellow]舰队不可用:[/yellow] {error}",
        "native_unavailable": "[yellow]原生运行时不可用:[/yellow] {error}",
        "daemons_unavailable": "[yellow]守护进程不可用:[/yellow] {error}",
        "job_list_cleared": "[dim]任务列表已清空[/dim]",
        "unknown": "未知命令 {command}；试试 /help",
        "tui_only": "{command} 仅在全屏 TUI 中可用",
        "tui_only_mark": "仅 TUI",
        "hint_help": "显示这些命令",
        "hint_quit": "退出行模式 shell",
        "usage_attach": "用法: /attach <id|index>",
        "usage_decision": "用法: /{decision} <派发 id>",
        "decision_requested": "已请求 {decision} {id}",
        "unknown_suggest": "未知命令 {command}；你是指 {suggestions} 吗？",
        "no_ticket": "没有 Ticket",
        "no_progress": "没有 {ticket} 的进度",
        "no_active_dispatches": "没有活跃的派发",
        "dispatch_not_found": "未找到派发",
        "no_saved_sessions": "没有已保存的会话",
        "saved_sessions": "已保存的会话（新的在前）:",
        "resume_with": "用 /resume <id> 恢复",
        "session_row": "  {session}  {title}  ticket {ticket}",
        "resumed_session": "已恢复会话 {session}（行模式；上一个 Ticket {ticket}；守护进程 {daemon}）",
        "goodbye": "\n再见！",
        "goodbye_quit": "再见！",
        "interrupted": "[yellow]已中断[/yellow]",
        "confirm": "[yellow]{label} 需要确认（a 允许一次 / s 本次会话允许 / d 拒绝）: [/yellow]",
        "confirm_required": "{label} 需要本地确认；请在交互模式中运行",
        "denied": "已拒绝: {label}",
        "raw_usage": "用法: /raw <request>",
        "limit_integer": "--limit 必须是整数",
        "limit_positive": "--limit 必须是正数",
        "unknown_jobs_option": "未知的 /jobs 选项 {flag}",
        "usage_jobs_new": "用法: /jobs new <prompt>",
        "usage_jobs_watch": "用法: /jobs watch <id|index>",
        "usage_jobs_retry": "用法: /jobs retry <id|index>",
        "usage_jobs_reply": "用法: /jobs reply <id|index> <text>",
        "usage_jobs_requeue": "用法: /jobs requeue <eval-id>",
        "usage_reply": "用法: /reply <id|index> <text>",
        "usage_retry": "用法: /retry <id|index>",
        "usage_cancel": "用法: /cancel <id|index>",
        "usage_stop": "用法: /stop <id|index>",
        "usage_bang": "用法: !<shell command>",
        "usage_review": ("用法: /review run <line> --by <line> | /review status [line] | /review verdict <id> --verdict MERGE|BLOCK"),
        "usage_merge": "用法: /merge <line> --into <branch> [--test-cmd CMD]",
        "usage_agents_logs": "用法: /agents logs <agent-id> [--lines N]",
        "lines_integer": "--lines 必须是整数",
        "unknown_status_group": "未知的状态组 {group}",
        "conductor_timeout": "conductor {group} status 超时",
        "conductor_missing": "未找到 conductor 命令",
        "no_session": "没有以 {prefix} 开头的会话",
        "ambiguous_session": "会话前缀 {prefix} 有多个匹配: {names}",
        "event_record": "[bold]事件记录[/bold]",
        "reports": "[bold]报告[/bold]",
        "origin": "来源: {origin}",
        "log_heading": "[bold]日志 {agent}[/bold]",
        "status_line": ("[dim]状态:[/dim] 需要你={attention} 运行中={running} 排队={queued} 失败={failed} 阻塞={blocked} 已完成={completed}"),
        "signals_jobs": ("[dim]任务:[/dim] 需要你={attention} 运行中={running} 排队={queued} 已完成={completed}"),
        "help_also": ("另外: /jobs new|watch|retry|reply|requeue, /stop, /agents, /signals, /review, /merge, /new, !<command>"),
        "help_plain": "纯文本会在 Home 上创建 Ticket；/jobs watch <id> 会实时跟随它。",
        "help_raw": "在请求前加上 /raw，会按原文运行并跳过改写。",
        "help_roles": "角色登记；/roles set <role> default <name> reason <text>",
        "yes": "是",
        "no": "否",
        "daemon_auto": "自动",
        "never": "从未",
        "fleet_title": "本地代理舰队",
        "native_title": "原生运行时",
        "daemons_title": "本地守护进程",
        "signals_title": "信号",
        "conductor_failed": "conductor 命令失败",
        "shell_command": "运行 shell 命令: {command}",
        "merge_action": "执行 conductor merge {args}",
        "state_cancelled": "已取消",
        "stopped_watching": "[dim]已停止跟随；Ticket 会继续运行[/dim]",
        "attention_mark": "  ［?］ {question}",
        "col_id": "id",
        "col_role": "角色",
        "col_status": "状态",
        "col_branch": "分支",
        "col_ahead": "领先",
        "col_pid": "pid",
        "col_provider": "提供方",
        "col_installed": "已安装",
        "col_live": "在线",
        "col_binary": "程序",
        "col_name": "名称",
        "col_last_seen": "最近出现",
        "col_source": "来源",
        "col_kind": "类型",
        "col_enabled": "已启用",
        "col_state": "状态",
    },
}

# A leading /raw runs the request as written, skipping the server's prompt
# refiner for this one submission.
_RAW_PREFIX = "/raw"


def _msg(key: str, **values: object) -> str:
    """One shell sentence in the process locale.

    Values are already display text. The catalog holds the words around them,
    including the Rich tags the line shell uses for colour.
    """
    template = t(STRINGS, key, resolve_locale())
    if not values:
        return template
    return template.format(**values)


def _say(key: str, **values: object) -> None:
    console.print(_msg(key, **values))


def _fail(key: str, **values: object) -> SuperAgentError:
    """Build one shell error from the locale table."""
    return SuperAgentError(_msg(key, **values))


def _raise(key: str, /, cause: BaseException | None = None, **values: object) -> None:
    """Raise one locale-table error, keeping ``cause`` on the chain."""
    error = _fail(key, **values)
    if cause is None:
        raise error
    raise error from cause


def _pad_cells(text: str, width: int) -> str:
    missing = width - cell_len(text)
    return text + " " * missing if missing > 0 else text


def _prefixed(prefix: str, body: str, width: int) -> list[str]:
    """Wrap ``body`` into the room ``prefix`` leaves, keeping every character."""
    room = width - cell_len(prefix)
    if room < 1:
        return _wrap_text(f"{prefix}{body}", max(1, width))
    wrapped = _wrap_text(body, room)
    pad = " " * cell_len(prefix)
    return [prefix + wrapped[0], *(pad + line for line in wrapped[1:])]


def _alias_labels(command: PaletteCommand) -> tuple[str, ...]:
    """Slash spellings of a command that are not already its help label."""
    label = _shell_label(command)
    seen = {label.casefold(), command.name.casefold()}
    if command.slash:
        seen.add(command.slash.casefold())
    extras: list[str] = []
    for alias in command.aliases:
        text = alias if str(alias).startswith("/") else f"/{alias}"
        if text.casefold() in seen:
            continue
        seen.add(text.casefold())
        extras.append(text)
    return tuple(extras)


def help_lines(width: int, locale: str | None = None) -> list[str]:
    """``/help`` rows from the S04 command table, wrapped to ``width``.

    The hint is the palette's own hint for ``locale``, so the line shell and
    the palette describe a command the same way. Aliases that the table spells
    as another slash name (``/attach``, ``/exit``) stay on the row: ``/help``
    has to teach the same commands the TUI lists.
    """
    resolved = locale or resolve_locale()
    columns = max(1, int(width))
    rows: list[str] = []
    for command in COMMANDS:
        label = _shell_label(command)
        hint_key = _LINE_HINTS.get(command.name)
        hint = t(STRINGS, hint_key, resolved) if hint_key else command_hint(command, resolved)
        if label in _TUI_ONLY:
            hint = f"{hint} ({t(STRINGS, 'tui_only_mark', resolved)})"
        extras = _alias_labels(command)
        if extras:
            hint = f"{hint} · {', '.join(extras)}"
        prefix = f"  {_pad_cells(label, _HELP_NAME_WIDTH)} "
        rows.extend(_prefixed(prefix, hint, columns))
    for label, key in _HELP_EXTRAS:
        prefix = f"  {_pad_cells(label, _HELP_NAME_WIDTH)} "
        rows.extend(_prefixed(prefix, t(STRINGS, key, resolved), columns))
    rows.append("")
    for key in ("help_also", "help_plain", "help_raw"):
        if key == "help_plain":
            rows.append("")
        rows.extend(_prefixed("  ", t(STRINGS, key, resolved), columns))
    return rows


def home_lines(
    tickets: object,
    attention: object = 0,
    width: int = 80,
    locale: str | None = None,
) -> list[str]:
    """Home rows at the terminal width, drawn by :func:`tui_home.home_rows`."""
    return home_rows(tickets, attention, None, locale, max(1, int(width)))


def _visible_tickets(
    rows: list[dict[str, Any]],
    *,
    status: str,
    attention: bool,
    failed: bool,
) -> list[dict[str, Any]]:
    """The roster ``jobs_rows`` will number, in that order.

    The line shell asks the API for a filter and the shared renderer applies
    the same filter again. Indexes have to follow the second pass, because
    that is the list on screen. The predicates are the renderer's own.
    """
    status_of = _tui_jobs._status
    question_of = _tui_jobs._question
    candidates = [row for row in rows if isinstance(row, dict)]
    wanted = status_of(status) if status else ""
    if wanted:
        candidates = [row for row in candidates if status_of(row.get("status")) == wanted]
    if failed:
        candidates = [row for row in candidates if status_of(row.get("status")) in {"failed", "blocked"}]
    if attention:
        candidates = [row for row in candidates if status_of(row.get("status")) == "needs-you" or question_of(row)]
    return candidates


def jobs_lines(
    tickets: object,
    width: int = 80,
    locale: str | None = None,
    *,
    status: str | None = None,
    attention: bool = False,
    failed: bool = False,
    now: float | None = None,
) -> list[str]:
    """``/jobs`` rows at the terminal width, drawn by :func:`tui_jobs.jobs_rows`.

    ``selected`` stays unset: the line shell prints the roster, it does not
    mark a current row the way the full-screen list does. ``now`` is the clock
    that turns ``updated_at`` into an age. Ordinary API rows carry a timestamp
    and no elapsed field, so the line shell always supplies one.
    """
    return jobs_rows(
        tickets,
        None,
        locale,
        max(1, int(width)),
        selected=None,
        status=status,
        attention=attention,
        failed=failed,
        now=time.time() if now is None else now,
    )


def _print_rows(rows: list[str]) -> None:
    """Print rows that are already wrapped. Rich must not wrap them again."""
    for row in rows:
        console.print(row, markup=False, highlight=False, overflow="ignore", soft_wrap=True)


def _suggestions(command: str, limit: int = _SUGGESTION_LIMIT) -> tuple[str, ...]:
    """D1 suggestions for an unknown command, best match first.

    A suggestion that is the typed token itself is skipped: ``/clear`` naming
    the clear command is not a hint, it is the command the line shell does
    not run.
    """
    typed = command.casefold()
    found: list[str] = []
    for ranked in rank_commands(command):
        label = _shell_label(ranked.command)
        if label.casefold() == typed or label in found:
            continue
        found.append(label)
        if len(found) >= limit:
            break
    return tuple(found)


def _unknown_command(command: str) -> str:
    suggestions = _suggestions(command)
    if suggestions:
        return _msg("unknown_suggest", command=command, suggestions=", ".join(suggestions))
    return _msg("unknown", command=command)


def _reset_notice(reason: object) -> str:
    """The live-output restart notice, in the process locale.

    Underscores in the reason become spaces, matching the notice the TUI
    already prints for the same frame.
    """
    label = _text(str(reason or "")[:80]).replace("_", " ").strip()
    if label:
        return _msg("output_restarted_reason", reason=label)
    return _msg("output_restarted")


def strip_raw_prefix(line: str) -> tuple[str, bool]:
    """Split a leading ``/raw`` off a submission; ``(request, raw)``.

    Only a real prefix counts — ``/rawfix`` is an ordinary request — and the
    remainder stays stripped so the caller can refuse an empty one.
    """
    text = line or ""
    if not text.startswith(_RAW_PREFIX):
        return text, False
    rest = text[len(_RAW_PREFIX) :]
    if rest and not rest[:1].isspace():
        return text, False
    return rest.strip(), True


# The line shell has no composer, so it does not print a prompt glyph.
# Color is not the only carrier of state.
_STATE_GLYPHS: dict[str, str] = {
    "completed": "✔",
    "done": "✔",
    "succeeded": "✔",
    "success": "✔",
    "waiting": "◆",
    "blocked": "◆",
    "attention": "◆",
    "failed": "✖",
    "error": "✖",
    "cancelled": "✖",
    "canceled": "✖",
    "queued": "○",
}

# Spellings the line shell already prints, folded onto the theme's state ids.
# English keeps the raw token: watch tests count the lowercase word ``completed``.
_STATE_CANONICAL = {
    "done": "completed",
    "succeeded": "completed",
    "success": "completed",
    "completed": "completed",
    "waiting": "needs-you",
    "attention": "needs-you",
    "needs_you": "needs-you",
    "needs-you": "needs-you",
    "error": "failed",
    "failed": "failed",
    "queued": "queued",
    "running": "running",
    "blocked": "blocked",
    "archived": "archived",
}

_STATUS_TIMEOUT_S = 5.0
_PIPE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_PIPE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")

TERMINAL = {"completed", "cancelled", "failed", "blocked", "waiting", "archived"}
ACTIVE = {"queued", "running", "waiting", "blocked", "failed"}

# The CLI can be configured to wait up to a day for one merge; the shell's
# subprocess guard follows that setting instead of a fixed 2h.
_SHELL_TIMEOUT_FLOOR = 11_400.0
_SHELL_TIMEOUT_CEILING = 90000.0


def _conductor_timeout(env: dict[str, str]) -> float:
    raw = (env.get("CONDUCTOR_MERGE_TIMEOUT_SECONDS") or "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return max(_SHELL_TIMEOUT_FLOOR, min(_SHELL_TIMEOUT_CEILING, value + 120.0))
    return _SHELL_TIMEOUT_FLOOR


def _short(value: str, width: int = 8) -> str:
    return value if len(value) <= width else value[:width]


def _count(value: object) -> str:
    """A job count, including zero.

    ``plain(0)`` is empty (``str(value or "")``), and a Rich ``Text`` of that
    blank prints as nothing or as its repr. A real number is ``str(int(...))``.
    Anything else stays display text, still sanitized.
    """
    if isinstance(value, bool):
        return _text(value)
    if isinstance(value, int):
        return str(int(value))
    if isinstance(value, float) and math.isfinite(value):
        whole = int(value)
        if whole == value:
            return str(whole)
    if isinstance(value, str):
        cleaned = _plain(value).strip()
        try:
            return str(int(cleaned, 10))
        except ValueError:
            pass
    return _text(value)


def _state_key(status: object) -> str:
    return _plain(status).strip().lower().replace("_", "-").replace(" ", "-")


def _state_word(status: object) -> str:
    """The state word. English stays the raw token; zh-CN uses the known name."""
    label = _text(status)
    if resolve_locale() == "en":
        return label
    key = _state_key(status)
    if key in {"cancelled", "canceled"}:
        return _msg("state_cancelled")
    canonical = _STATE_CANONICAL.get(key)
    if canonical is None:
        return label
    return status_word(canonical, resolve_locale())


def _state_mark(status: object) -> str:
    """Glyph plus the status word. Unknown states stay the word alone."""
    label = _state_word(status)
    glyph = _STATE_GLYPHS.get(_plain(status).strip().lower(), "")
    if not glyph:
        return label
    return f"{glyph} {label}".strip()


def _terminal_width() -> int:
    return max(20, shutil.get_terminal_size((80, 24)).columns)


def _stdin_fd() -> int | None:
    """The controlling tty, or None when Esc cannot be read."""
    stdin = sys.stdin
    try:
        if stdin is None or stdin.closed or not stdin.isatty():
            return None
        return stdin.fileno()
    except (AttributeError, ValueError, OSError):
        return None


def _in_foreground(fd: int) -> bool:
    """True when this process group owns the tty.

    A background job (``... &``) that changes the tty mode gets SIGTTOU, and
    one that reads it gets SIGTTIN; both stop the process. Errors count as
    background: the listener is a convenience, never a reason to stop.
    """
    try:
        return os.tcgetpgrp(fd) == os.getpgrp()
    except (AttributeError, OSError):
        return False


def _is_lone_escape(chunk: bytes) -> bool:
    """A bare Esc press, not the lead byte of an arrow or function key."""
    for index, byte in enumerate(chunk):
        if byte != _ESC:
            continue
        follower = chunk[index + 1 : index + 2]
        if follower not in (b"[", b"O"):
            return True
    return False


@asynccontextmanager
async def _escape_listener(stop: asyncio.Event) -> AsyncIterator[None]:
    """Set ``stop`` when the foreground tty receives a lone Escape.

    Other fds, pipes and background jobs are left alone.
    """
    fd = _stdin_fd()
    if fd is None or not _in_foreground(fd):
        yield
        return
    try:
        import termios
    except ImportError:  # Windows has no termios; there is no Esc stop there.
        yield
        return
    loop = asyncio.get_running_loop()
    # A canonical tty holds Esc in its line buffer until Enter, so the reader
    # would never see it. Switch to cbreak (no line buffering, no echo, signals
    # kept so Ctrl-C still works) for the watch and always put the mode back.
    try:
        saved_mode: list[Any] | None = termios.tcgetattr(fd)
    except (termios.error, OSError):
        saved_mode = None
    if saved_mode is not None:
        cbreak = termios.tcgetattr(fd)
        cbreak[3] &= ~(termios.ICANON | termios.ECHO)
        cbreak[6][termios.VMIN] = 1
        cbreak[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(fd, termios.TCSANOW, cbreak)
        except (termios.error, OSError):
            saved_mode = None

    def _restore_mode() -> None:
        if saved_mode is not None:
            with suppress(termios.error, OSError):
                termios.tcsetattr(fd, termios.TCSADRAIN, saved_mode)

    def _read() -> None:
        try:
            chunk = os.read(fd, 64)
        except (BlockingIOError, InterruptedError, OSError):
            return
        if _is_lone_escape(chunk):
            stop.set()

    try:
        loop.add_reader(fd, _read)
    except (NotImplementedError, RuntimeError, OSError, ValueError):
        _restore_mode()
        yield
        return
    try:
        yield
    finally:
        loop.remove_reader(fd)
        _restore_mode()


async def _wait_first(work: Awaitable[Any], stop: asyncio.Event, limit: float | None) -> tuple[str, Any]:
    """Race ``work`` against Escape and an optional deadline.

    Returns ``("done", result)``, ``("escape", None)`` or ``("timeout", None)``.
    """
    work_task = asyncio.ensure_future(work)
    stop_task = asyncio.ensure_future(stop.wait())
    try:
        done, pending = await asyncio.wait(
            {work_task, stop_task},
            timeout=limit,
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError:
        work_task.cancel()
        stop_task.cancel()
        await asyncio.gather(work_task, stop_task, return_exceptions=True)
        raise
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    if work_task in done and not work_task.cancelled():
        error = work_task.exception()
        if error is not None:
            raise error
        return "done", work_task.result()
    if not done:
        return "timeout", None
    return "escape", None


def _visible_markup(value: str) -> str:
    """Rich tags we wrote become their text; escaped server brackets stay."""
    try:
        return Text.from_markup(value).plain
    except MarkupError:
        return value


def _is_rich_renderable(value: object) -> bool:
    return hasattr(value, "__rich_console__") or hasattr(value, "__rich__")


def _load_render_plain() -> Callable[..., object] | None:
    """The shared renderer, imported at print time so a missing module is fine."""
    try:
        from .tui_render import render_plain
    except (ImportError, SyntaxError):
        return None
    return render_plain if callable(render_plain) else None


def _coerce_rendered(value: object) -> str | None:
    if isinstance(value, str):
        return value.rstrip("\n")
    if not isinstance(value, list):
        return None
    lines: list[str] = []
    for row in value:
        if isinstance(row, str):
            lines.append(row)
            continue
        if isinstance(row, list):
            parts: list[str] = []
            for cell in row:
                if isinstance(cell, tuple) and cell:
                    parts.append(str(cell[-1]))
                elif isinstance(cell, str):
                    parts.append(cell)
            lines.append("".join(parts))
            continue
        lines.append(str(row))
    return "\n".join(lines).rstrip("\n")


def _try_render_plain(render: Callable[..., object], source: object, width: int) -> str | None:
    """Call ``render_plain`` with a width when that parameter exists."""
    calls = (
        lambda: render(source, width=width),
        lambda: render(source),
        lambda: render(source, width),
    )
    for call in calls:
        try:
            return _coerce_rendered(call())
        except TypeError:
            continue
        except (ValueError, AttributeError, RuntimeError, OSError):
            # A half-written renderer must not take down the line shell.
            return None
    return None


def _capture_rich(renderable: object, width: int) -> str:
    from .tui_rich import render_plain

    return render_plain(renderable, width=width)


def _pipe_row(line: str) -> bool:
    return _PIPE_ROW.match(line) is not None


def _normalize_pipe_tables(text: str) -> str:
    """Give a header-only pipe block the separator Rich's Markdown needs."""
    lines = text.splitlines()
    out: list[str] = []
    index = 0
    while index < len(lines):
        if not _pipe_row(lines[index]):
            out.append(lines[index])
            index += 1
            continue
        block: list[str] = []
        while index < len(lines) and _pipe_row(lines[index]):
            block.append(lines[index])
            index += 1
        if len(block) >= _MIN_PIPE_ROWS and _PIPE_RULE.match(block[1]) is None:
            columns = max(block[0].count("|") - 1, 1)
            block.insert(1, "| " + " | ".join(["---"] * columns) + " |")
        out.extend(block)
    return "\n".join(out)


def _without_pipe_rows(text: str) -> str:
    """A raw ``| a | b |`` row is a markdown dump, never the line-shell layout."""
    cleaned: list[str] = []
    for line in text.splitlines():
        if _pipe_row(line):
            cleaned.append(re.sub(r"\s*\|\s*", "  ", line).strip())
        else:
            cleaned.append(line.rstrip())
    return "\n".join(cleaned).strip("\n")


def _fallback_markdown(visible: str, width: int) -> str:
    document = _normalize_pipe_tables(strip_protocol_trailers(visible))
    rendered = _capture_rich(
        Markdown(document, hyperlinks=False, inline_code_lexer=None),
        width,
    )
    return _without_pipe_rows(strip_protocol_trailers(rendered))


def _looks_like_markdown(text: str) -> bool:
    if "```" in text or "**" in text:
        return True
    if text.startswith("#") or "\n#" in text:
        return True
    return any(_pipe_row(line) for line in text.splitlines())


def render_line_text(visible: str, *, width: int | None = None) -> str:
    """Plain text for the line shell: shared renderer, else the same layout.

    Import of :mod:`tui_render` stays inside the print path. Trailers and raw
    markdown pipe rows do not survive either path.
    """
    width = _terminal_width() if width is None else max(20, int(width))
    body = strip_protocol_trailers(visible)
    render = _load_render_plain()
    if render is not None:
        rendered = _try_render_plain(render, body, width)
        if rendered is not None and (rendered.strip() or not body.strip()):
            return _without_pipe_rows(strip_protocol_trailers(rendered))
    if _looks_like_markdown(body):
        return _fallback_markdown(body, width)
    return _without_pipe_rows(body)


class _LineConsole:
    """Print through ``tui_render.render_plain`` when that module is present.

    Live token writes pass ``end=""`` or ``markup=False`` and stay on the Rich
    console: they are already sanitized fragments, not a markdown document.
    """

    def __init__(self) -> None:
        self._rich = Console()

    def print(
        self,
        *objects: object,
        sep: str = " ",
        end: str = "\n",
        markup: bool = True,
        highlight: bool = True,
        style: str = "",
        **kwargs: Any,
    ) -> None:
        if end == "" or markup is False:
            extra: dict[str, Any] = dict(kwargs)
            if style:
                extra["style"] = style
            self._rich.print(
                *objects,
                sep=sep,
                end=end,
                markup=False,
                highlight=highlight,
                **extra,
            )
            return
        if not objects:
            self._rich.print(end=end)
            return
        width = _terminal_width()
        if all(_is_rich_renderable(obj) for obj in objects):
            rendered = sep.join(_without_pipe_rows(strip_protocol_trailers(_capture_rich(obj, width))) for obj in objects)
            self._rich.print(rendered, end=end, markup=False, highlight=False)
            return
        parts: list[str] = []
        for obj in objects:
            if isinstance(obj, str):
                parts.append(_visible_markup(obj))
            elif _is_rich_renderable(obj):
                parts.append(_capture_rich(obj, width))
            else:
                parts.append(str(obj))
        self._rich.print(
            render_line_text(sep.join(parts), width=width),
            end=end,
            markup=False,
            highlight=False,
        )

    def input(self, prompt: str = "") -> str:
        return self._rich.input(prompt)


console = _LineConsole()


# Live stream reconnection: bounded retries with exponential backoff before
# the watch gives up on SSE and polls the activity endpoint instead.
_SSE_RECONNECT_ATTEMPTS = 5
_SSE_BACKOFF_BASE_S = 0.5
_SSE_BACKOFF_MAX_S = 8.0
# The server ends one stream at its ceiling (15 minutes) with a ``timeout``
# frame. That is a rotation, not the end of the watch: reattach after this
# pause with the last event id. A watch has no deadline of its own unless the
# caller passes one; it ends on a terminal Ticket or Ctrl+C.
_SSE_ROTATE_DELAY_S = 0.5
# Polling fallback: an unreachable server or a 5xx/408/429 is retried with
# backoff up to this ceiling; any other error ends the watch.
_POLL_INTERVAL_S = 2.0
_POLL_BACKOFF_MAX_S = 30.0
_POLL_STATUS = re.compile(r" -> (\d{3}):")
_POLL_RETRYABLE_4XX = frozenset({408, 425, 429})
_HTTP_SERVER_ERROR = 500


def _poll_error_is_transient(exc: SuperAgentError) -> bool:
    text = str(exc)
    if text.startswith("cannot reach "):
        return True
    match = _POLL_STATUS.search(text)
    if match is None:
        return False
    status = int(match.group(1))
    return status >= _HTTP_SERVER_ERROR or status in _POLL_RETRYABLE_4XX


def _age(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        moment = datetime.fromisoformat(str(iso))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        age = max(0, int((datetime.now(UTC) - moment).total_seconds()))
    except (ValueError, OverflowError):
        return "-"
    if age < _MINUTE_S:
        return f"{age}s"
    if age < _HOUR_S:
        return f"{age // _MINUTE_S}m"
    return f"{age // _HOUR_S}h"


def _ticket_line(ticket: dict[str, Any]) -> str:
    question = (ticket.get("attention") or {}).get("question") or ""
    body = f"{_short(_text(ticket.get('ticket_id')))} {_state_mark(ticket.get('status'))} {_text(ticket.get('title'))}"
    if not question:
        return body
    return body + _msg("attention_mark", question=_text(question, 100))


class SuperAgentShell:
    """The Rich line shell: commands, Home, and live Ticket watch."""

    def __init__(
        self,
        *,
        server_url: str,
        user_id: str,
        command: str | None = None,
        no_splash: bool = False,
        permissions: PermissionManager | None = None,
    ) -> None:
        """Open a client for ``server_url`` and remember a one-shot command."""
        self.client = SuperAgentClient(server_url, user_id)
        self.permissions = permissions or PermissionManager()
        self.command = command
        self.no_splash = no_splash
        self._workspace_id: str | None = None
        self._jobs: list[dict[str, Any]] = []
        # None until a screen has numbered tickets. Empty means that screen
        # showed no rows, which is different from "use the raw endpoint list".
        self._shown: list[dict[str, Any]] | None = None
        self._focus_ticket: str | None = None
        self._daemon_id: str | None = os.getenv("CONDUCTOR_DAEMON_ID") or None
        self._daemon_label = ""

    async def close(self) -> None:
        """Close the Super-agent client."""
        await self.client.close()

    async def _recent_tickets(self) -> list[dict[str, Any]]:
        """The three newest Tickets Home shows. A missing route is an empty Home."""
        fetch = getattr(self.client, "tickets", None)
        if fetch is None:
            return []
        try:
            data = await fetch(limit=3)
        except SuperAgentError:
            return []
        rows = data.get("tickets") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        return [row for row in rows if isinstance(row, dict)][:3]

    async def show_home(self, attention: object = 0) -> None:
        """Print Home at the terminal width through :func:`home_lines`.

        The numbered actions on those rows are the index space ``target``
        reads, so the tickets Home just printed become the current listing.
        """
        tickets = await self._recent_tickets()
        self._jobs = tickets
        self._shown = tickets
        _print_rows(
            home_lines(
                tickets,
                attention,
                _terminal_width(),
                resolve_locale(),
            )
        )

    async def startup(self) -> None:
        """Print the server line, counts, Home, and the daemon selection."""
        attention: object = 0
        _say("server_line", url=_text(self.client.base_url), user=_text(self.client.user_id))
        try:
            overview = await self.client.overview()
            counts = overview.get("counts") or {}
            attention = counts.get("attention", 0)
            _say(
                "startup_counts",
                sources=_count(overview.get("sources", 0)),
                enabled=_count(overview.get("enabled_sources", 0)),
                attention=_count(attention),
                running=_count(counts.get("running", 0)),
                queued=_count(counts.get("queued", 0)),
                failed=_count(counts.get("failed", 0)),
                blocked=_count(counts.get("blocked", 0)),
            )
        except SuperAgentError as exc:
            _say("startup_unavailable", error=_text(exc))
        if not self.no_splash:
            await self.show_home(attention)
        try:
            daemons = (await self.client.daemons()).get("daemons", [])
        except SuperAgentError:
            daemons = []
        if daemons and self._daemon_id is None:
            now = datetime.now(UTC)
            live: list[dict[str, Any]] = []
            for row in daemons:
                try:
                    seen = datetime.fromisoformat(str(row.get("last_seen_at") or ""))
                    if seen.tzinfo is None:
                        seen = seen.replace(tzinfo=UTC)
                except ValueError:
                    continue
                if (now - seen).total_seconds() <= _DAEMON_FRESH_S:
                    live.append(row)
            pool = live or daemons
            active = sorted(
                pool,
                key=lambda row: str(row.get("last_seen_at") or ""),
                reverse=True,
            )
            picked = active[0]
            self._daemon_id = str(picked.get("daemon_id") or "") or None
            self._daemon_label = str(picked.get("name") or "")
            if not live:
                _say("no_recent_daemon")
        if self._daemon_id:
            _say(
                "execution_daemon",
                label=_text(self._daemon_label or self._daemon_id),
            )
        else:
            _say("no_daemon")
        _say("startup_hint")

    async def workspace(self) -> str:
        """Return the Home workspace id, fetching it once."""
        if self._workspace_id is None:
            self._workspace_id = await self.client.home_workspace()
        return self._workspace_id

    def _indexed_tickets(self) -> list[dict[str, Any]]:
        """The numbered rows on screen, or the raw list before any screen."""
        if self._shown is None:
            return self._jobs
        return self._shown

    def target(self, value: str) -> str:
        """Resolve a Ticket id, or a 1-based index from the roster on screen."""
        value = value.strip()
        if value.isdigit():
            tickets = self._indexed_tickets()
            index = int(value) - 1
            if 0 <= index < len(tickets):
                return str(tickets[index]["ticket_id"])
            _raise("no_job_index", value=value)
        return value

    async def create_task(
        self,
        prompt: str,
        *,
        title: str = "",
        daemon_id: str = "",
        no_refine: bool = False,
    ) -> dict[str, Any]:
        """Create a Ticket and print the dispatch line."""
        workspace_id = await self.workspace()
        ticket = await self.client.create_task(
            prompt,
            title=(title or prompt.strip().splitlines()[0])[:120],
            workspace_id=workspace_id,
            worker_daemon_id=daemon_id or (self._daemon_id or ""),
            no_refine=no_refine,
        )
        ticket_id = str(ticket.get("ticket_id") or "")
        if ticket_id:
            self._focus_ticket = ticket_id
        daemon = ""
        if self._daemon_id:
            daemon = _msg("dispatched_daemon", label=_text(self._daemon_label))
        _say(
            "dispatched",
            ticket=_text(ticket.get("ticket_id")),
            state=_state_mark(ticket.get("status")),
            title=_text(ticket.get("title")),
            daemon=daemon,
        )
        _say("follow_hint")
        return ticket

    async def list_jobs(self, flags: list[str]) -> None:
        """Print ``/jobs`` through the shared roster at the terminal width."""
        status = ""
        attention = False
        failed = False
        limit = 30
        index = 0
        while index < len(flags):
            flag = flags[index]
            if flag == "--status" and index + 1 < len(flags):
                status = flags[index + 1]
                index += 2
            elif flag == "--attention":
                attention = True
                index += 1
            elif flag == "--failed":
                failed = True
                index += 1
            elif flag == "--limit" and index + 1 < len(flags):
                try:
                    limit = int(flags[index + 1])
                except ValueError as exc:
                    _raise("limit_integer", cause=exc)
                if limit < 1:
                    _raise("limit_positive")
                index += 2
            else:
                _raise("unknown_jobs_option", flag=flag)
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if attention:
            params["attention"] = "true"
        if failed:
            params["failed"] = "true"
        rows = (await self.client.tickets(**params)).get("tickets", [])
        api_rows = [row for row in rows if isinstance(row, dict)]
        visible = _visible_tickets(api_rows, status=status, attention=attention, failed=failed)
        # ``_jobs`` stays the endpoint payload (callers assert that). The
        # numbered roster is the renderer's sequence, and that is what is drawn.
        self._jobs = api_rows
        self._shown = visible
        _print_rows(
            jobs_lines(
                visible,
                _terminal_width(),
                resolve_locale(),
                status=status or None,
                attention=attention,
                failed=failed,
            )
        )

    async def show_job(self, target: str) -> None:
        """Print one Ticket's identity, origin, scope, reports, and activity."""
        ticket = await self.client.ticket(self.target(target))
        ticket_id = str(ticket.get("ticket_id"))
        self._focus_ticket = ticket_id
        width = _terminal_width()
        locale = resolve_locale()
        _print_rows(job_detail_rows(ticket, None, locale, width))
        origin = _text(ticket.get("origin_thread_key"))
        if origin:
            _say("origin", origin=origin)
        scope = ticket.get("scope")
        if scope:
            console.print(_text(scope, 1200))
        reports = ticket.get("reports") or []
        if reports:
            _say("reports")
            for report in reports[:8]:
                console.print(f"  [cyan]{_state_mark(report.get('state'))}[/cyan]")
                summary = report.get("summary")
                if summary:
                    console.print(_text(summary, 380))
        await self.show_activity(ticket_id)

    async def show_activity(self, ticket_id: str, seen: set[str] | None = None) -> set[str]:
        """Print new activity events and return every event id seen so far."""
        activity = await self.client.activity(ticket_id)
        seen = seen if seen is not None else set()
        new_seen = set(seen)
        fresh: list[dict[str, Any]] = []
        for event in activity.get("events", []):
            event_id = str(event.get("event_id") or "")
            if event_id in seen:
                continue
            new_seen.add(event_id)
            fresh.append(event)
        if fresh:
            _say("event_record")
            for event in fresh:
                console.print(f"[dim]{_text(event.get('at'))}[/dim] [cyan]{_text(event.get('kind'))}[/cyan]")
                summary = event.get("summary")
                if summary:
                    console.print(_text(summary, 160))
        return new_seen

    def _show_terminal(self, status: str, summary: object = "", question: object = "") -> None:
        console.print(f"[green]{_state_mark(status)}[/green]")
        if str(summary or "").strip():
            console.print(_text(summary))
        if question:
            _say("needs_you", question=_text(question))

    async def watch_job(self, target: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """Follow a Ticket until it is terminal, preferring its live stream.

        ``timeout`` None (the default) means no deadline: the server's stream
        ceiling only rotates the connection. Escape stops the local watch. The
        Ticket keeps running.
        """
        ticket_id = self.target(target)
        self._focus_ticket = ticket_id
        stop = asyncio.Event()
        async with _escape_listener(stop):
            _print_rows(
                watch_header_rows(
                    {"ticket_id": ticket_id},
                    None,
                    resolve_locale(),
                    _terminal_width(),
                )
            )
            deadline = math.inf if timeout is None else time.monotonic() + timeout
            seen: set[str] = set()
            if await self._watch_job_stream(ticket_id, seen, deadline, stop):
                if stop.is_set():
                    _say("stopped_watching")
                return
            if stop.is_set():
                _say("stopped_watching")
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _say("watch_timeout")
                return
            _say("stream_polling")
            await self._watch_job_polling(ticket_id, seen, deadline, stop)
            if stop.is_set():
                _say("stopped_watching")

    async def _watch_job_stream(  # noqa: PLR0911
        self,
        ticket_id: str,
        seen: set[str],
        deadline: float,
        stop: asyncio.Event,
    ) -> bool:
        """Follow the SSE stream.

        Returns True when the watch is over (terminal end, timeout, server
        end) and False when the caller must fall back to polling. Reconnects
        with the last event id on transport failure, bounded by backoff
        attempts; an endpoint that is missing or not SSE on the first attach is
        not retried.

        A local daemon/native Worker (Codex) streams its model text line by
        line through the daemon output channel when the server has it; other
        native providers' output arrives as the progress/report activity rows
        they persist. A Ticket with a Worker run streams that run token-by-token.
        A change of live-output source (run, daemon epoch) restarts the output
        with a visible notice instead of continuing the old fragment.

        The terminal ``status`` frame is not the end of the stream: the Worker
        can persist its report (which flips the Ticket terminal) and keep
        emitting model output inside the same run, so the watch waits for the
        closing ``end`` frame instead of leaving early.
        """
        state: dict[str, Any] = {
            "last_event_id": None,
            "open_output": False,
            "output_style": "",
            "tokens": _TokenSanitizer(),
            "output_run_id": None,
            # Last stream epoch seen on text/reasoning output; None = none yet
            # (or forgotten by a reset). Payloads without an epoch leave it be.
            "output_epoch": None,
            "attempts": 0,
            "got_event": False,
            "terminal": None,
            "terminal_shown": False,
            "rotated": False,
        }

        def write_raw(text: str) -> None:
            if not text:
                return
            style = str(state["output_style"] or "")
            console.print(
                text,
                end="",
                markup=False,
                highlight=False,
                **({"style": style} if style else {}),
            )

        def emit_tokens(text: object) -> None:
            body = state["tokens"].feed(text)
            if body:
                write_raw(body)
                state["open_output"] = True

        def close_output() -> None:
            """End the current output line.

            Does not touch the token sanitizer: a split escape sequence may
            resume after an interleaved activity/tool/token event.
            """
            if state["open_output"]:
                console.print()
                state["open_output"] = False

        def finish_output() -> None:
            """Discard an unterminated escape tail and close the output line."""
            body = state["tokens"].flush()
            if body:
                write_raw(body)
            if state["open_output"]:
                console.print()
                state["open_output"] = False

        def show_terminal(data: dict[str, Any]) -> None:
            if state["terminal_shown"]:
                return
            data = data or state["terminal"] or {}
            state["terminal_shown"] = True
            finish_output()
            status = str(data.get("status") or "")
            if status == "missing":
                _say("ticket_missing")
            elif status in TERMINAL:
                self._show_terminal(status, data.get("summary"), data.get("question"))

        def give_up() -> bool:
            """Close the line, then defer to polling.

            A terminal status frame without its ``end`` frame is not proof the
            Ticket is still terminal: a retry can have revived it (queued) and
            the reattach can fail before the next frame. Failed recovery must
            revalidate durable state through the polling fallback instead of
            printing the cached result.
            """
            finish_output()
            return bool(state["terminal_shown"])

        def restart_output(reason: object) -> None:
            """The live output restarted: what came before is a closed fragment.

            Drops the half-parsed escape state (so it can neither swallow nor
            be completed by the text that follows), ends the output line so the
            two sides never read as one string, and forgets the run and epoch.
            """
            state["tokens"] = _TokenSanitizer()
            close_output()
            state["output_run_id"] = None
            state["output_epoch"] = None
            console.print(f"[dim]{_reset_notice(reason)}[/dim]")

        def write_output(data: dict[str, Any]) -> None:
            kind = str(data.get("kind") or "")
            if kind == "output_reset":
                restart_output(data.get("reason"))
                return
            if kind in {"text", "reasoning"}:
                epoch = data.get("epoch")
                if isinstance(epoch, int) and not isinstance(epoch, bool):
                    if state["output_epoch"] is not None and epoch != state["output_epoch"]:
                        # Defense in depth: a new epoch without an announced
                        # reset must not continue the previous fragment.
                        restart_output("new_epoch")
                    state["output_epoch"] = epoch
                run_id = str(data.get("run") or "")
                if state["output_run_id"] is not None and run_id != state["output_run_id"]:
                    # The Worker generation changed (retry/line switch, cloud
                    # run <-> local daemon): the old fragment is closed for good
                    # and an unfinished escape tail from it must not swallow
                    # the new run's text.
                    restart_output("new_epoch")
                    if isinstance(epoch, int) and not isinstance(epoch, bool):
                        state["output_epoch"] = epoch
                state["output_run_id"] = run_id
                state["output_style"] = "dim" if kind == "reasoning" else ""
                emit_tokens(data.get("text"))
                return
            close_output()
            if kind == "tool":
                tool_run = str(data.get("run") or "")
                tool_epoch = data.get("epoch")
                has_epoch = isinstance(tool_epoch, int) and not isinstance(tool_epoch, bool)
                if tool_run or has_epoch:
                    # Tool names belong to a source too: a name from one source
                    # followed by text from another is a new fragment.
                    if (tool_run and state["output_run_id"] is not None and tool_run != state["output_run_id"]) or (
                        has_epoch and state["output_epoch"] is not None and tool_epoch != state["output_epoch"]
                    ):
                        restart_output("new_epoch")
                    if tool_run:
                        state["output_run_id"] = tool_run
                    if has_epoch:
                        state["output_epoch"] = tool_epoch
                names = [str(name) for name in data.get("tools") or [] if name]
                if names:
                    _say("tool_line", names=_text(", ".join(names)))
            elif kind == "error":
                _say("worker_error", text=_text(data.get("text")))
            elif kind == "gap":
                # Dropped output is a discontinuity, like a reset: the text on
                # either side must not be joined by a sanitizer or a run label.
                state["tokens"] = _TokenSanitizer()
                state["output_run_id"] = None
                state["output_epoch"] = None
                console.print(f"[yellow]{_text(data.get('text'))}[/yellow]")

        def render(event: ActivityStreamEvent) -> bool:
            """Render one frame; True when the watch is over."""
            if event.event_id:
                state["last_event_id"] = event.event_id
            name = event.event.strip()
            data = event.data or {}
            if name == "activity":
                close_output()
                event_id = str(data.get("event_id") or "")
                if event_id and event_id in seen:
                    return False
                if event_id:
                    seen.add(event_id)
                console.print(f"[dim]{_text(data.get('at'))}[/dim] [cyan]{_text(data.get('kind'))}[/cyan] {_text(data.get('summary'), 160)}")
            elif name == "output":
                write_output(data)
            elif name == "status":
                if str(data.get("status") or "") in TERMINAL:
                    # Not the end of the stream: the Worker's tail can follow.
                    state["terminal"] = data
                else:
                    # A retry can revive a Ticket after a terminal read; the
                    # cached result must not outlive that.
                    state["terminal"] = None
            elif name == "end":
                show_terminal(data)
                return True
            elif name == "timeout":
                # The server's per-connection ceiling, not the watch's: the
                # loop reattaches with the last event id.
                close_output()
                state["rotated"] = True
                return True
            elif name == "error":
                # Keep the token sanitizer state: a reconnect replays the same
                # Worker run, and its split escape sequence may still complete.
                close_output()
                raise ActivityStreamUnavailableError(str(data.get("message") or _msg("stream_failed")))
            return False

        async def consume(stream: AsyncIterator[ActivityStreamEvent]) -> bool:
            async for event in stream:
                state["got_event"] = True
                if render(event):
                    return True
            return False

        while time.monotonic() < deadline:
            if stop.is_set():
                finish_output()
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            state["got_event"] = False
            state["rotated"] = False
            stream = self.client.stream_activity(ticket_id, last_event_id=state["last_event_id"])
            try:
                kind, result = await _wait_first(consume(stream), stop, None if math.isinf(remaining) else remaining)
                # An attached stream must not outlive the watch deadline.
                if kind == "timeout":
                    return give_up()
                if kind == "escape":
                    finish_output()
                    return True
                if result and not state["rotated"]:
                    return True
            except ActivityStreamUnavailableError as exc:
                if not state["got_event"]:
                    return give_up()
                close_output()
                _say("stream_lost", error=_text(exc))
            except SuperAgentError as exc:
                if not state["got_event"]:
                    return give_up()
                close_output()
                _say("stream_interrupted", error=_text(exc))
            finally:
                with suppress(Exception):
                    await stream.aclose()
            if state["rotated"]:
                # A clean rotation is not a failed attempt.
                state["attempts"] = 0
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                kind, _result = await _wait_first(
                    asyncio.sleep(min(_SSE_ROTATE_DELAY_S, remaining)),
                    stop,
                    None if math.isinf(remaining) else remaining,
                )
                if kind == "escape":
                    finish_output()
                    return True
                continue
            if not state["got_event"]:
                # An empty clean close names an endpoint with no live path.
                return give_up()
            state["attempts"] += 1
            if state["attempts"] > _SSE_RECONNECT_ATTEMPTS:
                return give_up()
            delay = min(
                _SSE_BACKOFF_BASE_S * (2 ** (state["attempts"] - 1)),
                _SSE_BACKOFF_MAX_S,
            )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            kind, _result = await _wait_first(
                asyncio.sleep(min(delay, remaining)),
                stop,
                None if math.isinf(remaining) else remaining,
            )
            if kind == "escape":
                return True
            if kind == "timeout":
                break
        return give_up()

    async def _watch_job_polling(
        self,
        ticket_id: str,
        seen: set[str],
        deadline: float,
        stop: asyncio.Event,
    ) -> None:
        """The pre-stream activity loop, kept as the fallback."""
        failures = 0
        while time.monotonic() < deadline:
            if stop.is_set():
                return
            try:
                ticket = await self.client.ticket(ticket_id)
                seen = await self.show_activity(ticket_id, seen)
            except SuperAgentError as exc:
                if not _poll_error_is_transient(exc):
                    raise
                failures += 1
                if failures == 1:
                    _say("poll_retrying", error=_text(exc))
                delay = min(_POLL_INTERVAL_S * (2 ** (failures - 1)), _POLL_BACKOFF_MAX_S)
                remaining = deadline - time.monotonic()
                kind, _result = await _wait_first(
                    asyncio.sleep(max(0.0, min(delay, remaining))),
                    stop,
                    None if math.isinf(remaining) else max(0.0, remaining),
                )
                if kind == "escape":
                    return
                continue
            failures = 0
            status = str(ticket.get("status") or "")
            if status in TERMINAL:
                question = (ticket.get("attention") or {}).get("question")
                self._show_terminal(status, ticket.get("summary"), question)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0 or stop.is_set():
                break
            kind, _result = await _wait_first(
                asyncio.sleep(min(_POLL_INTERVAL_S, remaining)),
                stop,
                None if math.isinf(remaining) else remaining,
            )
            if kind == "escape":
                return
            if kind == "timeout":
                break
        if stop.is_set():
            return
        _say("watch_timeout")

    async def stop_job(self, target: str) -> None:
        """Ask the server to stop a Ticket."""
        ticket_id = self.target(target)
        await self.client.stop(ticket_id)
        _say("stop_requested", ticket=_text(ticket_id))

    async def retry_job(self, target: str) -> None:
        """Queue a retry for a Ticket."""
        ticket_id = self.target(target)
        await self.client.retry(ticket_id)
        _say("retry_queued", ticket=_text(ticket_id))

    async def reply_job(self, target: str, text: str) -> None:
        """Send an operator reply to a Ticket."""
        if not text:
            _raise("usage_jobs_reply")
        ticket_id = self.target(target)
        await self.client.reply(ticket_id, text)
        _say("reply_sent", ticket=_text(ticket_id))

    async def requeue_eval(self, eval_id: str) -> None:
        """Requeue one autonomy eval."""
        await self.client._request("POST", f"/api/v1/autonomy/evals/{eval_id}/requeue")
        _say("eval_requeued", eval_id=_text(eval_id))

    async def show_agents(self) -> None:
        """Print the local fleet, native runtimes, and daemons."""
        try:
            fleet = await self.client.fleet()
            rows = fleet.get("agents", [])
            table = Table(title=_msg("fleet_title"), show_lines=False)
            for column in ("col_id", "col_role", "col_status", "col_branch", "col_ahead", "col_pid"):
                table.add_column(_msg(column))
            for row in rows:
                table.add_row(
                    _text(row.get("id")),
                    _text(row.get("role")),
                    _text(row.get("status")),
                    _text(row.get("branch")),
                    _text(row.get("commits_ahead")),
                    _text(row.get("pid") or "-"),
                )
            console.print(table)
            if fleet.get("error"):
                console.print(f"[yellow]{_text(fleet['error'])}[/yellow]")
        except SuperAgentError as exc:
            _say("fleet_unavailable", error=_text(exc))
        try:
            native = await self.client.native_agents()
            table = Table(title=_msg("native_title"), show_lines=False)
            table.add_column(_msg("col_provider"))
            table.add_column(_msg("col_installed"))
            table.add_column(_msg("col_live"))
            table.add_column(_msg("col_binary"))
            for row in native.get("runtimes", []):
                table.add_row(
                    _text(row.get("provider")),
                    _msg("yes") if row.get("installed") else _msg("no"),
                    _msg("yes") if row.get("live_verified") else _msg("no"),
                    _text(row.get("binary")),
                )
            console.print(table)
        except SuperAgentError as exc:
            _say("native_unavailable", error=_text(exc))
        try:
            daemons = (await self.client.daemons()).get("daemons", [])
            if daemons:
                table = Table(title=_msg("daemons_title"), show_lines=False)
                for column in ("col_id", "col_name", "col_provider", "col_last_seen"):
                    table.add_column(_msg(column))
                for row in daemons:
                    table.add_row(
                        _short(_text(row.get("daemon_id"))),
                        _text(row.get("name")),
                        _text(row.get("provider")),
                        _text(row.get("last_seen_at") or _msg("never")),
                    )
                console.print(table)
        except SuperAgentError as exc:
            _say("daemons_unavailable", error=_text(exc))

    async def show_agent_logs(self, args: list[str]) -> None:
        """Print one agent's log. ``--lines N`` limits how much is fetched."""
        if not args:
            _raise("usage_agents_logs")
        agent_id = args[0]
        lines = 80
        if "--lines" in args:
            try:
                lines = int(args[args.index("--lines") + 1])
            except (IndexError, ValueError) as exc:
                _raise("lines_integer", cause=exc)
        row = await self.client.fleet_logs(agent_id, lines=lines)
        _say("log_heading", agent=_text(agent_id))
        console.print(_text(row.get("text") or row.get("lines")))

    async def signals(self) -> None:
        """Print Sources and the job counts."""
        overview = await self.client.overview()
        counts = overview.get("counts") or {}
        sources = (await self.client.sources()).get("sources", [])
        table = Table(title=_msg("signals_title"), show_lines=False)
        table.add_column(_msg("col_source"), style="cyan")
        table.add_column(_msg("col_kind"))
        table.add_column(_msg("col_enabled"))
        table.add_column(_msg("col_state"))
        for row in sources:
            state = row.get("state") or {}
            table.add_row(
                _text((row.get("config") or {}).get("repository") or (row.get("config") or {}).get("chat_id") or row.get("source_id")),
                _text(row.get("kind")),
                _msg("yes") if row.get("enabled") else _msg("no"),
                _text(state.get("last_error") or state.get("last_poll_at")),
            )
        console.print(table)
        _say(
            "signals_jobs",
            attention=_count(counts.get("attention", 0)),
            running=_count(counts.get("running", 0)),
            queued=_count(counts.get("queued", 0)),
            completed=_count(counts.get("completed", 0)),
        )

    def conductor(self, args: list[str]) -> str:
        """Run ``conductor_cli`` locally and return its text."""
        command = [
            sys.executable,
            "-m",
            "conductor_cli",
            "--url",
            self.client.base_url,
            *args,
        ]
        worktree_root = Path(os.environ.get("CONDUCTOR_WT_ROOT", str(Path.home() / "conductor-wt"))).expanduser().resolve()
        child_env = os.environ.copy()
        child_env["CONDUCTOR_WT_ROOT"] = str(worktree_root)
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=worktree_root if worktree_root.is_dir() else None,
            env=child_env,
            capture_output=True,
            text=True,
            check=False,
            # A merge can spend the server's full test + Codex budget (the CLI
            # merge client defaults to 2h), so the shell must not kill it first
            # and must follow a larger configured CLI wait.
            timeout=_conductor_timeout(child_env),
        )
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or _msg("conductor_failed")).strip()
            raise SuperAgentError(detail[-2000:])
        output = completed.stdout.strip()
        try:
            return json.dumps(json.loads(output), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return output

    def _require_local_permission(self, action: str, label: str) -> None:
        """The line shell's Allow/Session/Deny gate for local mutation.

        The TUI asks with an in-app row; the line fallback asks on stdin. A
        non-interactive run cannot confirm, so a local merge/review is refused
        rather than executed unconfirmed.
        """
        if not self.permissions.needs_confirmation(action):
            return
        if not sys.stdin.isatty():
            _raise("confirm_required", label=label)
        answer = console.input(_msg("confirm", label=_text(label))).strip().lower()
        if answer == "s":
            self.permissions.allow_session(action)
            return
        if answer == "a":
            return
        _raise("denied", label=label)

    async def review(self, args: list[str]) -> None:
        """Run ``conductor review`` and print its output."""
        if not args:
            _raise("usage_review")
        console.print(_text(self.conductor(["review", *args])))

    async def merge(self, args: list[str]) -> None:
        """Run ``conductor merge`` after the local confirmation gate."""
        if not args:
            _raise("usage_merge")
        self._require_local_permission("local-merge", _msg("merge_action", args=_text(" ".join(args))))
        console.print(_text(self.conductor(["merge", *args])))

    async def handle(self, line: str) -> bool:
        """Run one line. Return False when the operator asked to quit."""
        line = line.strip()
        if not line:
            return True
        if line.startswith("!"):
            command = line[1:].strip()
            if not command:
                _raise("usage_bang")
            self._require_local_permission("local-shell", _msg("shell_command", command=_text(command)))
            output = run_shell(command, cwd=Path.cwd())
            body = _plain(output).strip()
            if body:
                console.print(body, markup=False, highlight=False)
            return True
        prompt, raw = strip_raw_prefix(line)
        if raw:
            if not prompt:
                _raise("raw_usage")
            await self.create_task(prompt, no_refine=True)
            return True
        if line.startswith("/"):
            parts = line.split(None, 1)
            command = parts[0].lower()
            rest = parts[1].strip() if len(parts) > 1 else ""
            args = rest.split()
            if command in {"/exit", "/quit", "/q"}:
                return False
            if command in {"/help", "/?"}:
                self.help()
            elif command in _TUI_ONLY:
                _say("tui_only", command=command)
            elif command == "/attach":
                if not args:
                    _raise("usage_attach")
                await self.watch_job(args[0])
            elif command in {"/approve", "/reject"}:
                await self.decide_dispatch(command[1:], args)
            elif command == "/status":
                await self.show_status()
            elif command in {"/jobs", "/j"}:
                await self.jobs_command(rest, args)
            elif command == "/worker":
                self._unsupported()
            elif command == "/dispatch":
                await self.show_dispatch(rest)
            elif command == "/roles":
                await self.roles_command(rest)
            elif command == "/reply":
                reply_parts = rest.split(None, 1)
                if len(reply_parts) < _JOBS_WITH_TARGET or not reply_parts[1].strip():
                    _raise("usage_reply")
                await self.reply_job(reply_parts[0], reply_parts[1])
            elif command == "/retry":
                if not args:
                    _raise("usage_retry")
                await self.retry_job(args[0])
            elif command == "/cancel":
                if not args:
                    _raise("usage_cancel")
                await self.stop_job(args[0])
            elif command == "/resume":
                self.show_resume(args)
            elif command == "/workspace":
                self._unsupported()
            elif command == "/skills":
                self.conductor_status("skills")
            elif command == "/mcp":
                self.conductor_status("mcp")
            elif command == "/memory":
                self._unsupported()
            elif command in {"/stop", "/s"}:
                if not args:
                    _raise("usage_stop")
                await self.stop_job(args[0])
            elif command in {"/agents", "/a"}:
                if args[:1] == ["logs"]:
                    await self.show_agent_logs(args[1:])
                else:
                    await self.show_agents()
            elif command in {"/review", "/r"}:
                await self.review(args)
            elif command in {"/merge", "/m"}:
                await self.merge(args)
            elif command == "/signals":
                await self.signals()
            elif command == "/new":
                self._jobs = []
                self._shown = []
                _say("job_list_cleared")
            else:
                raise SuperAgentError(_unknown_command(command))
            return True
        await self.create_task(line)
        return True

    async def jobs_command(self, rest: str, args: list[str]) -> None:
        """Dispatch ``/jobs`` and its subcommands."""
        if not rest:
            await self.list_jobs([])
            return
        sub = args[0]
        if sub == "new":
            parts = rest.split(None, 1)
            if len(parts) < _JOBS_WITH_TARGET or not parts[1].strip():
                _raise("usage_jobs_new")
            await self.create_task(parts[1].strip())
        elif sub == "watch":
            if len(args) < _JOBS_WITH_TARGET:
                _raise("usage_jobs_watch")
            await self.watch_job(args[1])
        elif sub == "retry":
            if len(args) < _JOBS_WITH_TARGET:
                _raise("usage_jobs_retry")
            await self.retry_job(args[1])
        elif sub == "reply":
            parts = rest.split(None, 2)
            if len(parts) < _JOBS_REPLY_PARTS or not parts[2].strip():
                _raise("usage_jobs_reply")
            await self.reply_job(parts[1], parts[2])
        elif sub == "requeue":
            if len(args) < _JOBS_WITH_TARGET:
                _raise("usage_jobs_requeue")
            await self.requeue_eval(args[1])
        elif sub.startswith("--"):
            await self.list_jobs(args)
        else:
            await self.show_job(sub)

    def _unsupported(self) -> None:
        """There is no local worker, workspace, or memory view."""
        _say("unsupported")

    def conductor_status(self, group: str) -> None:
        """``conductor <group> status --json``. Fixed argv, no shell, 5s."""
        if group not in {"skills", "mcp"}:
            _raise("unknown_status_group", group=group)
        command = ["conductor", group, "status", "--json"]
        try:
            completed = subprocess.run(  # noqa: S603
                command,
                shell=False,
                timeout=_STATUS_TIMEOUT_S,
                capture_output=True,
                text=True,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            _raise("conductor_timeout", cause=exc, group=group)
        except FileNotFoundError as exc:
            _raise("conductor_missing", cause=exc)
        if completed.returncode:
            detail = (completed.stderr or completed.stdout or _msg("conductor_failed")).strip()
            raise SuperAgentError(detail[-2000:])
        output = (completed.stdout or "").strip()
        if not output:
            return
        with suppress(json.JSONDecodeError):
            output = json.dumps(json.loads(output), ensure_ascii=False, indent=2)
        console.print(_text(output))

    async def show_status(self) -> None:
        """Print overview counts and Supervisor progress for the focused Ticket."""
        overview = await self.client.overview()
        counts = overview.get("counts") or {}
        _say(
            "status_line",
            attention=_count(counts.get("attention", 0)),
            running=_count(counts.get("running", 0)),
            queued=_count(counts.get("queued", 0)),
            failed=_count(counts.get("failed", 0)),
            blocked=_count(counts.get("blocked", 0)),
            completed=_count(counts.get("completed", 0)),
        )
        if not self._focus_ticket:
            _say("no_ticket")
            return
        progress_fn = getattr(self.client, "progress", None)
        if progress_fn is None:
            _say("no_progress", ticket=_text(self._focus_ticket))
            return
        progress = await progress_fn(self._focus_ticket)
        if not progress:
            _say("no_progress", ticket=_text(self._focus_ticket))
            return
        console.print(_text(json.dumps(progress, ensure_ascii=False, indent=2)))

    async def roles_command(self, rest: str) -> None:
        """R6 L1: show the role registry, or change one role with a reason."""
        from . import roles_view

        try:
            action, role, body = roles_view.parse(rest)
        except roles_view.RolesUsageError as exc:
            console.print(_text(str(exc)))
            return
        if action == "set":
            payload = await self.client.set_role(role, body)
        else:
            payload = await self.client.roles()
        console.print(_text("\n".join(roles_view.render(payload))))

    async def show_dispatch(self, rest: str) -> None:
        """Print active dispatches, or one dispatch when an id is given."""
        from .dispatch_view import render_dispatch_lines

        if not rest.strip():
            rows = await self.client.active_dispatches()
            if not rows:
                _say("no_active_dispatches")
                return
            for row in rows:
                console.print(_text("\n".join(render_dispatch_lines(row, listing=True))))
            return
        payload = await self.client.dispatch(rest.split(maxsplit=1)[0])
        if payload is None:
            _say("dispatch_not_found")
            return
        console.print(_text("\n".join(render_dispatch_lines(payload))))

    async def decide_dispatch(self, decision: str, args: list[str]) -> None:
        """Approve or reject one dispatch through the dispatch API, as the TUI does."""
        from .dispatch_view import render_dispatch_lines

        if not args:
            _raise("usage_decision", decision=decision)
        ident = args[0]
        request = getattr(self.client, "_request", None)
        if request is None:
            self._unsupported()
            return
        payload = await request("POST", f"/api/v1/dispatch/{ident}/{decision}", json={})
        if isinstance(payload, dict):
            console.print(_text("\n".join(render_dispatch_lines(payload))))
        else:
            _say("decision_requested", decision=decision, id=_text(ident[:8]))

    def show_resume(self, args: list[str]) -> None:
        """Read a saved TUI session. The line shell has no transcript to paint."""
        from .tui_sessions import SessionStore

        store = SessionStore()
        if not args:
            sessions = store.list_sessions()
            if not sessions:
                _say("no_saved_sessions")
                return
            _say("saved_sessions")
            for record in sessions[:10]:
                ticket = record.last_ticket_id[:8] if record.last_ticket_id else "-"
                _say(
                    "session_row",
                    session=_text(record.session_id),
                    title=_text(record.title),
                    ticket=_text(ticket),
                )
            _say("resume_with")
            return
        query = str(args[0]).strip().lower()
        sessions = store.list_sessions()
        exact = [record for record in sessions if record.session_id == query]
        matches = exact or [record for record in sessions if record.session_id.startswith(query)]
        if not matches:
            _raise("no_session", prefix=args[0])
        if len(matches) > 1:
            names = ", ".join(record.session_id for record in matches[:5])
            _raise("ambiguous_session", prefix=args[0], names=names)
        record = matches[0]
        if record.daemon_id:
            self._daemon_id = record.daemon_id
        if record.daemon_label:
            self._daemon_label = record.daemon_label
        if record.last_ticket_id:
            self._focus_ticket = record.last_ticket_id
        target = record.last_ticket_id[:8] if record.last_ticket_id else "none"
        _say(
            "resumed_session",
            session=_text(record.session_id),
            ticket=_text(target),
            daemon=_text(record.daemon_label) or _msg("daemon_auto"),
        )

    def help(self) -> None:
        """Print the S04 command table at the terminal width."""
        _print_rows(help_lines(_terminal_width(), resolve_locale()))

    async def run(self) -> int:
        """Start the shell and return a process status code."""
        await self.startup()
        try:
            if self.command:
                try:
                    await self.handle(self.command)
                except KeyboardInterrupt:
                    return 0
                except SuperAgentError as exc:
                    console.print(f"[red]{_text(exc)}[/red]")
                    return 1
                return 0
            history = FileHistory(str(Path.home() / ".ptc-agent" / "superagent-history"))
            session: PromptSession[str] = PromptSession(history=history)
            while True:
                try:
                    line = await session.prompt_async("superagent> ")
                except (EOFError, KeyboardInterrupt):
                    _say("goodbye")
                    return 0
                try:
                    if not await self.handle(line):
                        _say("goodbye_quit")
                        return 0
                except KeyboardInterrupt:
                    _say("interrupted")
                except SuperAgentError as exc:
                    console.print(f"[red]{_text(exc)}[/red]")
        finally:
            await self.close()


async def run_super_agent(
    *,
    server_url: str,
    user_id: str,
    command: str | None = None,
    no_splash: bool = False,
    print_prompt: str | None = None,
    continue_session: bool = False,
    no_tui: bool = False,
) -> int:
    """Backwards-compatible alias for the mode-selecting runner.

    The TUI and the line shell share this entry point; keeping the name here
    means code that imported it from ``shell`` keeps working.
    """
    from .runner import run_super_agent as run

    return await run(
        server_url=server_url,
        user_id=user_id,
        command=command,
        no_splash=no_splash,
        print_prompt=print_prompt,
        continue_session=continue_session,
        no_tui=no_tui,
    )
