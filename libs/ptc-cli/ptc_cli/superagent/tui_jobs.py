"""Pure terminal-native renderers for the Super-agent ``/jobs`` screens."""

# ruff: noqa: RUF001

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from rich.cells import cell_len

from .text import plain
from .tui_blocks import format_duration
from .tui_i18n import resolve_locale, t
from .tui_render import wrap_words
from .tui_rotation import count_rotations
from .tui_theme import STATUS_IDS, Theme, resolve_theme, status_glyph, status_word

INSET = 2
GAP = 2
STACK_MAX = 40

STRINGS = {
    "en": {
        "jobs": "jobs",
        "jobs_attention": "jobs · needs you",
        "no_jobs": "no Jobs",
        "index": "#",
        "id": "id",
        "state": "state",
        "title": "title",
        "worker": "worker",
        "rotated": "rotated {count} times",
        "age": "age",
        "next": "next",
        "summary": "{count} Tickets · {attention} needs you · {running} running",
        "ticket": "Ticket",
        "updated": "updated",
        "needs_you": "needs you",
        "error": "error",
        "watching": "watching Ticket {id}",
        "watch_hint": "Esc to stop watching; the Ticket keeps running",
    },
    "zh-CN": {
        "jobs": "任务列表",
        "jobs_attention": "任务列表 · 需要你",
        "no_jobs": "no Jobs",
        "index": "#",
        "id": "id",
        "state": "状态",
        "title": "标题",
        "worker": "执行者",
        "rotated": "已轮换 {count} 次",
        "age": "用时",
        "next": "下一步",
        "summary": "{count} 个 Ticket · {attention} 个需要你 · {running} 个运行中",
        "ticket": "Ticket",
        "updated": "更新时间",
        "needs_you": "需要你",
        "error": "错误",
        "watching": "正在跟随 Ticket {id}",
        "watch_hint": "按 Esc 停止跟随；Ticket 会继续运行",
    },
}

_STATUS_ALIASES = {
    "attention": "needs-you",
    "needs_you": "needs-you",
    "waiting": "needs-you",
    "done": "completed",
    "success": "completed",
    "succeeded": "completed",
    "cancelled": "archived",
    "canceled": "archived",
    "error": "failed",
}
_DROP_ORDER = ("index", "age", "worker", "next")
_ELLIPSIS = "…"
_MISSING = "—"


@dataclass(frozen=True, slots=True)
class _JobRow:
    index: int
    ticket_id: str
    glyph: str
    state: str
    title: str
    worker: str
    age: str
    next_command: str
    selected: bool


def _text(value: object) -> str:
    return " ".join(plain(value).split())


def _status(value: object) -> str:
    status = _text(value).lower().replace(" ", "-")
    return _STATUS_ALIASES.get(status, status)


def _glyph(status: str, theme: Theme) -> str:
    if status not in STATUS_IDS:
        return ""
    environ = {"PTC_ASCII": "1"} if theme.glyphs.done == "+" else {"LANG": "en_US.UTF-8"}
    return status_glyph(status, environ)


def _word(status: str, locale: str | None) -> str:
    if status in STATUS_IDS:
        return status_word(status, locale or resolve_locale())
    return status


def _ticket_id(ticket: Mapping[str, object]) -> str:
    return _text(ticket.get("ticket_id") or ticket.get("id"))


def _question(ticket: Mapping[str, object]) -> str:
    attention = ticket.get("attention")
    if isinstance(attention, Mapping):
        return _text(attention.get("question"))
    return _text(ticket.get("question"))


def _age(ticket: Mapping[str, object], now: float | None) -> str:
    elapsed = ticket.get("elapsed_seconds")
    if isinstance(elapsed, (int, float)) and not isinstance(elapsed, bool):
        value = float(elapsed)
        if math.isfinite(value) and value >= 0:
            return format_duration(value) or _MISSING
    explicit = _text(ticket.get("age"))
    if explicit:
        return explicit
    if now is None:
        return _MISSING
    raw = _text(ticket.get("updated_at"))
    if not raw:
        return _MISSING
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return _MISSING
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    current = datetime.fromtimestamp(now, UTC)
    return format_duration(max(0.0, (current - moment).total_seconds())) or _MISSING


def _next_command(status: str, index: int) -> str:
    command = {
        "needs-you": "/reply",
        "blocked": "/reply",
        "failed": "/retry",
        "running": "/jobs watch",
    }.get(status, "/jobs")
    return f"{command} {index}"


def _prepare(
    tickets: Iterable[Mapping[str, object]],
    theme: Theme,
    locale: str | None,
    selected: int | None,
    now: float | None,
) -> list[_JobRow]:
    rows: list[_JobRow] = []
    for index, ticket in enumerate(tickets, 1):
        status = _status(ticket.get("status"))
        ticket_id = _ticket_id(ticket)
        title = _text(ticket.get("title")) or ticket_id or "ticket"
        worker = _text(ticket.get("worker_model") or ticket.get("worker_provider") or ticket.get("worker"))
        rows.append(
            _JobRow(
                index=index,
                ticket_id=ticket_id,
                glyph=_glyph(status, theme),
                state=_word(status, locale),
                title=title,
                worker=worker or _MISSING,
                age=_age(ticket, now),
                next_command=_next_command(status, index),
                selected=selected == index - 1,
            )
        )
    return rows


def _truncate(value: str, width: int, *, ellipsis: bool = True) -> str:
    if width <= 0:
        return ""
    if cell_len(value) <= width:
        return value
    room = max(0, width - cell_len(_ELLIPSIS)) if ellipsis else width
    result: list[str] = []
    used = 0
    for char in value:
        size = cell_len(char)
        if used + size > room:
            break
        result.append(char)
        used += size
    body = "".join(result).rstrip()
    return f"{body}{_ELLIPSIS}" if ellipsis else body


def _pad(value: str, width: int, *, ellipsis: bool = False) -> str:
    fitted = _truncate(value, width, ellipsis=ellipsis)
    return fitted + " " * max(0, width - cell_len(fitted))


def _table_width(widths: Mapping[str, int], columns: Iterable[str]) -> int:
    names = list(columns)
    return INSET + sum(widths[name] for name in names) + GAP * (len(names) - 1)


def _wide_rows(rows: list[_JobRow], width: int, locale: str | None, mark: str) -> list[str]:
    headers = {
        "mark": "",
        "index": t(STRINGS, "index", locale),
        "id": t(STRINGS, "id", locale),
        "glyph": "",
        "state": t(STRINGS, "state", locale),
        "title": t(STRINGS, "title", locale),
        "worker": t(STRINGS, "worker", locale),
        "age": t(STRINGS, "age", locale),
        "next": t(STRINGS, "next", locale),
    }
    values = {
        "mark": [mark if row.selected else "" for row in rows],
        "index": [str(row.index) for row in rows],
        "id": [row.ticket_id[:8] for row in rows],
        "glyph": [row.glyph for row in rows],
        "state": [row.state for row in rows],
        "title": [row.title for row in rows],
        "worker": [row.worker for row in rows],
        "age": [row.age for row in rows],
        "next": [row.next_command for row in rows],
    }
    widths = {
        "mark": 1,
        "index": max(3, cell_len(headers["index"]), *(cell_len(value) for value in values["index"])),
        "id": max(8, cell_len(headers["id"])),
        "glyph": 1,
        "state": max(9, cell_len(headers["state"]), *(cell_len(value) for value in values["state"])),
        "worker": max(cell_len(headers["worker"]), *(cell_len(value) for value in values["worker"])),
        "age": max(6, cell_len(headers["age"]), *(cell_len(value) for value in values["age"])),
        "next": max(13, cell_len(headers["next"]), *(cell_len(value) for value in values["next"])),
        "title": max(cell_len(headers["title"]), *(cell_len(value) for value in values["title"])),
    }
    columns = ["mark", "index", "id", "glyph", "state", "title", "worker", "age", "next"]
    for dropped in _DROP_ORDER:
        if _table_width(widths, columns) <= width:
            break
        columns.remove(dropped)
    fixed = [name for name in columns if name != "title"]
    title_room = width - _table_width(widths, fixed) - GAP
    widths["title"] = max(1, min(widths["title"], title_room))

    def line(cells: Mapping[str, str], *, title: bool) -> str:
        parts = [_pad(cells[name], widths[name], ellipsis=title and name == "title") for name in columns]
        return " " * INSET + (" " * GAP).join(parts).rstrip()

    output = [line(headers, title=False)]
    for index, _row in enumerate(rows):
        cells = {name: values[name][index] for name in values}
        output.append(line(cells, title=True))
    return output


def _wrap_parts(parts: list[str], width: int) -> list[str]:
    room = max(1, width - INSET * 2)
    lines: list[str] = []
    current = ""
    for part in parts:
        candidate = part if not current else f"{current} · {part}"
        if current and cell_len(candidate) <= room:
            current = candidate
            continue
        if current:
            lines.append(current)
        wrapped = wrap_words(part, room, hanging=0) or [""]
        lines.extend(wrapped[:-1])
        current = wrapped[-1]
    if current:
        lines.append(current)
    return [" " * (INSET * 2) + line for line in lines]


def _stacked_rows(rows: list[_JobRow], width: int, mark: str) -> list[str]:
    output: list[str] = []
    for row in rows:
        prefix = " " * INSET + _pad(mark if row.selected else "", 1) + " " * GAP
        prefix += _pad(row.glyph, 1) + " " * GAP
        title_width = max(1, width - cell_len(prefix))
        title_lines = wrap_words(row.title, title_width, hanging=0) or [""]
        output.append(prefix + title_lines[0])
        continuation = " " * cell_len(prefix)
        output.extend(continuation + line for line in title_lines[1:])
        state = " ".join(part for part in (row.glyph, row.state) if part)
        meta = [state, row.next_command, row.ticket_id, row.worker, row.age, str(row.index)]
        output.extend(_wrap_parts(meta, width))
    return output


def _prose(value: object, width: int, *, prefix: str = "  ") -> list[str]:
    text = plain(value).strip()
    if not text:
        return []
    room = max(1, width - cell_len(prefix))
    wrapped = wrap_words(text, room, hanging=0) or [""]
    continuation = " " * cell_len(prefix)
    return [prefix + wrapped[0], *(continuation + line for line in wrapped[1:])]


def _summary(rows: list[_JobRow], width: int, locale: str | None) -> list[str]:
    resolved_locale = locale or resolve_locale()
    attention = sum(row.state == status_word("needs-you", resolved_locale) for row in rows)
    running = sum(row.state == status_word("running", resolved_locale) for row in rows)
    text = t(STRINGS, "summary", locale).format(count=len(rows), attention=attention, running=running)
    return _prose(text, width)


def jobs_rows(
    tickets: Iterable[Mapping[str, object]],
    theme: Theme | None = None,
    locale: str | None = None,
    width: int = 80,
    *,
    selected: int | None = 0,
    status: str | None = None,
    attention: bool = False,
    failed: bool = False,
    now: float | None = None,
) -> list[str]:
    """Render the filtered Ticket roster; ``selected`` is a zero-based row index."""
    columns = max(1, int(width))
    resolved = theme or resolve_theme()
    candidates = [ticket for ticket in tickets if isinstance(ticket, Mapping)]
    wanted = _status(status) if status else ""
    if wanted:
        candidates = [ticket for ticket in candidates if _status(ticket.get("status")) == wanted]
    if failed:
        candidates = [ticket for ticket in candidates if _status(ticket.get("status")) in {"failed", "blocked"}]
    if attention:
        candidates = [ticket for ticket in candidates if _status(ticket.get("status")) == "needs-you" or _question(ticket)]
    heading = "jobs_attention" if attention else "jobs"
    output = _prose(t(STRINGS, heading, locale), columns)
    if not candidates:
        output.extend(_prose(t(STRINGS, "no_jobs", locale), columns))
        return output
    rows = _prepare(candidates, resolved, locale, selected, now)
    if columns <= STACK_MAX:
        output.append(" " * (INSET * 4) + t(STRINGS, "title", locale))
        output.extend(_stacked_rows(rows, columns, resolved.glyphs.voice))
    else:
        output.extend(_wide_rows(rows, columns, locale, resolved.glyphs.voice))
    output.extend(_summary(rows, columns, locale))
    return output


def attention_rows(
    tickets: Iterable[Mapping[str, object]],
    theme: Theme | None = None,
    locale: str | None = None,
    width: int = 80,
    *,
    selected: int | None = 0,
    status: str | None = None,
    now: float | None = None,
) -> list[str]:
    """Render only Tickets that need an operator, equivalent to ``--attention``."""
    return jobs_rows(
        tickets,
        theme,
        locale,
        width,
        selected=selected,
        status=status,
        attention=True,
        now=now,
    )


def _detail_title(ticket: Mapping[str, object], theme: Theme, locale: str | None, width: int) -> list[str]:
    status = _status(ticket.get("status"))
    glyph = _glyph(status, theme)
    word = _word(status, locale)
    state = " ".join(part for part in (glyph, word) if part)
    title = _text(ticket.get("title")) or _ticket_id(ticket) or "ticket"
    return _prose(title, width, prefix=f"  {state}  ")


def job_detail_rows(
    ticket: Mapping[str, object],
    theme: Theme | None = None,
    locale: str | None = None,
    width: int = 80,
) -> list[str]:
    """Render one Ticket's identity, progress, question, summary and error."""
    columns = max(1, int(width))
    resolved = theme or resolve_theme()
    output = _prose(f"{t(STRINGS, 'ticket', locale)} {_ticket_id(ticket)}", columns)
    output.extend(_detail_title(ticket, resolved, locale, columns))
    provider = _text(ticket.get("worker_provider") or ticket.get("worker")) or "PTC"
    model = _text(ticket.get("worker_model")) or "-"
    output.extend(_prose(f"{t(STRINGS, 'worker', locale)}: {provider}/{model}", columns))
    rotations = count_rotations(ticket)
    if rotations > 0:
        output.extend(_prose(t(STRINGS, "rotated", locale).format(count=rotations), columns))
    updated = _text(ticket.get("updated_at"))
    if updated:
        output.extend(_prose(f"{t(STRINGS, 'updated', locale)}: {updated}", columns))
    question = _question(ticket)
    if question:
        output.extend(_prose(question, columns, prefix=f"  {t(STRINGS, 'needs_you', locale)}: "))
    output.extend(_prose(ticket.get("summary"), columns))
    error = plain(ticket.get("error")).strip()
    if error:
        output.extend(_prose(error, columns, prefix=f"  {t(STRINGS, 'error', locale)}: "))
    return output


def watch_header_rows(
    ticket: Mapping[str, object],
    theme: Theme | None = None,
    locale: str | None = None,
    width: int = 80,
) -> list[str]:
    """Render the shared ``/jobs watch`` and ``/attach`` attachment header."""
    columns = max(1, int(width))
    resolved = theme or resolve_theme()
    heading = t(STRINGS, "watching", locale).format(id=_ticket_id(ticket))
    output = _prose(heading, columns)
    output.extend(_detail_title(ticket, resolved, locale, columns))
    output.extend(_prose(t(STRINGS, "watch_hint", locale), columns))
    return output


ticket_detail_rows = job_detail_rows


__all__ = [
    "STRINGS",
    "attention_rows",
    "job_detail_rows",
    "jobs_rows",
    "ticket_detail_rows",
    "watch_header_rows",
]
