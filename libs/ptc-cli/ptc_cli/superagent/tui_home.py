"""The Home screen rows for the terminal-native TUI.

Home answers one question: what is running, and what needs the operator. The
screen is the greeting, the needs-you count, and the recent Tickets, in that
order. Each Ticket row carries the state glyph and word from :mod:`tui_theme`,
its title, worker, age and clock time, and the command that acts on it with its
digit index.

The renderer is pure: it reads no clock and calls no client. That is what lets
the screen tests call it at every width and language and compare the result with
``docs/design/terminal-native/tui/SPEC.md`` §7.1. The caller passes the recent
Tickets (the shipped TUI passes at most three) and the needs-you count from the
status snapshot; only an omitted locale or theme falls back to the environment.

At 40 columns the row stacks instead of shedding columns (§3.3): the identity
line keeps the glyph and wraps the title under it, and the meta line carries
the state word, next command, worker, age and clock joined by `` · ``. Above 40
the row is a table whose convenience columns drop in priority order; the state
word and glyph never drop, and a title that still does not fit is cut on a cell
boundary with ``…``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from rich.cells import cell_len

from .text import plain
from .tui_i18n import normalize_locale, resolve_locale, t
from .tui_theme import status_glyph, status_row, status_word

__all__ = ["STRINGS", "home_rows"]

# §3.1: every region starts two columns in, and table columns are two apart.
INSET = 2
GAP = 2
# §3.3: at this width and below a row stacks instead of dropping a column.
STACK_MAX = 40
_DEFAULT_WIDTH = 80
_ELLIPSIS = "…"
_SEPARATOR = " · "
_MIN_TITLE = 1
_WIDE_CELL = 2
# §4.1: the state column is as wide as the longest English word.
_STATE_WIDTH = 9
_SECONDS_PER_MINUTE = 60
_MINUTES_PER_HOUR = 60
_MAX_CLOCK_HOUR = 23
_MAX_CLOCK_MINUTE = 59
_CLOSING_MARKS = frozenset("，。、；：？！")  # noqa: RUF001

STRINGS = {
    "en": {
        "heading": "Conductor",
        "greeting": "Describe a task and press Enter. It runs as a Ticket on Home.",
        "keys": "/ commands    @ attach    ? keys",
        "attention": "{glyph} {count} need you — /jobs --attention",
    },
    "zh-CN": {
        "heading": "Conductor",
        "greeting": "描述一个任务后按 Enter，它会作为 Ticket 在 Home 上运行。",  # noqa: RUF001
        "keys": "/ 命令    @ 附加    ? 按键",
        "attention": "{glyph} {count} 个需要你 — /jobs --attention",
    },
}

# The server and the stream spell a few states differently from the token set.
_STATE_ALIASES = {
    "waiting": "needs-you",
    "needs_you": "needs-you",
    "needs you": "needs-you",
    "needs-you": "needs-you",
    "queued": "queued",
    "pending": "queued",
    "running": "running",
    "in-progress": "running",
    "in_progress": "running",
    "blocked": "blocked",
    "held": "blocked",
    "failed": "failed",
    "error": "failed",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
    "archived": "archived",
    "cancelled": "archived",
    "canceled": "archived",
}
_NEXT_COMMAND = {
    "needs-you": "/reply",
    "blocked": "/reply",
    "failed": "/retry",
    "running": "/jobs watch",
    "queued": "/jobs",
    "completed": "/jobs",
    "archived": "/jobs",
}


@dataclass(frozen=True, slots=True)
class _Column:
    """One table column: a fixed width (or None for the title) and a priority."""

    key: str
    width: int | None
    priority: int
    pinned: bool = False


# §7.1: glyph(1) state(9) title(flexible) worker(8) age(6) when(5) next(13).
_COLUMNS = (
    _Column("glyph", 1, 100, pinned=True),
    _Column("state", _STATE_WIDTH, 95, pinned=True),
    _Column("title", None, 80),
    _Column("worker", 8, 45),
    _Column("age", 6, 40),
    _Column("when", 5, 30),
    _Column("next", 13, 75),
)


def home_rows(
    tickets: Iterable[object] | None,
    attention: object = 0,
    theme: object = None,
    locale: str | None = None,
    width: int = _DEFAULT_WIDTH,
) -> list[str]:
    """Render the Home transcript rows (``SPEC.md`` §7.1).

    Args:
        tickets: The recent Tickets, newest first. A mapping or an object may
            carry ``status``, ``title``, ``worker_model``/``worker_provider``,
            ``elapsed_seconds`` and ``updated_at``.
        attention: The needs-you count. ``None`` counts the Tickets instead; a
            sequence counts its length, and a mapping reads ``count``.
        theme: A :class:`ptc_cli.superagent.tui_theme.Theme`. It selects the
            ASCII glyphs when the theme was resolved with ``PTC_ASCII=1``.
        locale: ``en`` or ``zh-CN`` (a ``zh_*`` locale normalises to ``zh-CN``).
        width: The terminal width in cells.

    Returns:
        The rows of the transcript, one string per visual line.
    """
    resolved = normalize_locale(locale) if locale else resolve_locale()
    width = _DEFAULT_WIDTH if width is None else max(1, int(width))
    rows = _prose_rows(t(STRINGS, "heading", resolved), width)
    rows.extend(_prose_rows(t(STRINGS, "greeting", resolved), width))
    rows.extend(_prose_rows(t(STRINGS, "keys", resolved), width))

    recent = [
        ticket
        for ticket in (tickets or ())
        if isinstance(ticket, Mapping) or any(hasattr(ticket, name) for name in ("status", "state", "title", "ticket_id"))
    ]
    count = _attention_count(attention, recent)
    if count > 0:
        attention_line = t(STRINGS, "attention", resolved).format(
            glyph=_glyph("needs-you", theme),
            count=count,
        )
        rows.extend(_prose_rows(attention_line, width))

    cells = [_ticket_cells(ticket, index, resolved, theme) for index, ticket in enumerate(recent, 1)]
    if width <= STACK_MAX:
        for row in cells:
            rows.extend(_stacked_row(row, width))
        return rows
    kept, title_width = _fit_columns(cells, max(0, width - INSET))
    rows.extend(_table_row(row, kept, title_width) for row in cells)
    return rows


def _ticket_cells(ticket: object, index: int, locale: str, theme: object) -> dict[str, str]:
    """The seven column values of one Ticket, before layout."""
    state = _state(ticket)
    return {
        "glyph": _glyph(state, theme),
        "state": status_word(state, locale),
        "title": _title(ticket),
        "worker": _worker(ticket),
        "age": _age(ticket),
        "when": _when(ticket),
        "next": f"{_NEXT_COMMAND.get(state, '/jobs')} {index}",
    }


def _table_row(row: dict[str, str], kept: list[_Column], title_width: int) -> str:
    """One table row laid out on the columns that survived."""
    parts: list[str] = []
    for column in kept:
        value = row[column.key]
        if column.width is None:
            parts.append(_pad(_clip(value, title_width), title_width))
            continue
        # A glyph and a state word are never cut: the token set sizes them.
        if not column.pinned:
            value = _clip(value, column.width)
        parts.append(_pad(value, column.width))
    return (" " * INSET + (" " * GAP).join(parts)).rstrip()


def _fit_columns(rows: list[dict[str, str]], room: int) -> tuple[list[_Column], int]:
    """The columns that survive in ``room`` and the width left to the title.

    The rule is §3.2: the lowest-priority fixed column goes first, and the
    flexible title shrinks only when there is nothing left to drop. The glyph
    and the state word are pinned, so the row always says what it is.
    """
    kept = list(_COLUMNS)
    wanted = max((cell_len(row["title"]) for row in rows), default=0)
    while True:
        fixed = sum(column.width or 0 for column in kept if column.width is not None)
        gaps = GAP * (len(kept) - 1)
        spare = room - fixed - gaps
        if not any(column.width is None for column in kept):
            return kept, 0
        if spare >= wanted:
            return kept, wanted
        victims = [column for column in kept if column.width is not None and not column.pinned]
        if not victims:
            return kept, max(0, spare)
        kept.remove(min(victims, key=lambda column: (column.priority, _COLUMNS.index(column))))


def _stacked_row(row: dict[str, str], width: int) -> list[str]:
    """The narrow form: an identity line and a wrapped meta line (§3.3)."""
    prefix = " " * INSET + row["glyph"] + " " * GAP
    title_room = max(_MIN_TITLE, width - cell_len(prefix))
    title_lines = _wrap(row["title"], title_room)
    lines = [(prefix + title_lines[0]).rstrip()]
    lines.extend((" " * cell_len(prefix) + line).rstrip() for line in title_lines[1:])

    # The order is the stack plan of the design mock: state, next, worker, age,
    # when. The clock is last because it is the column that drops first.
    parts = [row["state"], row["next"], row["worker"], row["age"], row["when"]]
    meta_room = max(_MIN_TITLE, width - INSET - GAP)
    lines.extend((" " * (INSET + GAP) + line).rstrip() for line in _wrap_parts([part for part in parts if part], meta_room))
    return lines


def _prose_rows(text: str, width: int) -> list[str]:
    """A prose row wrapped into the inset, keeping every character (§3.4)."""
    room = max(_MIN_TITLE, width - INSET)
    return [(" " * INSET + line).rstrip() for line in _wrap(text, room)]


def _wrap_parts(parts: list[str], room: int) -> list[str]:
    """Join `` · ``-separated parts, wrapping whole parts before cutting one.

    A part that is wider than a whole line wraps inside itself, because §3.3
    allows a narrow terminal to show less, never to lose a column.
    """
    lines: list[str] = []
    current = ""
    for part in parts:
        if not part:
            continue
        if not current:
            chunks = _wrap(part, room)
            lines.extend(chunks[:-1])
            current = chunks[-1]
            continue
        candidate = current + _SEPARATOR + part
        if cell_len(candidate) <= room:
            current = candidate
            continue
        lines.append(current)
        chunks = _wrap(part, room)
        lines.extend(chunks[:-1])
        current = chunks[-1]
    if current:
        lines.append(current)
    return lines or [""]


def _wrap(text: str, room: int) -> list[str]:
    """Wrap prose on spaces, cutting a long word or a CJK run by cells.

    Every piece is kept: the rule of §3.4 is that a narrative row wraps, and a
    word longer than the line is cut across as many lines as it needs.
    """
    room = max(1, room)
    if not text:
        return [""]
    if cell_len(text) <= room:
        return [text]
    lines: list[str] = []
    current = ""
    for token in text.split(" "):
        word = token
        candidate = word if not current else f"{current} {word}"
        if cell_len(candidate) <= room:
            current = candidate
            continue
        breakable = cell_len(word) > room or (bool(word) and cell_len(word[0]) == _WIDE_CELL)
        if current and breakable:
            # Fill the line the short head started: `共 2` must not sit alone
            # above a CJK run that has no spaces to break on.
            left = room - cell_len(current) - 1
            head = _take_cells(word, left) if left > 0 else ""
            rest = word[len(head) :]
            if head and rest and rest[0] in _CLOSING_MARKS:
                head, rest = head[:-1], head[-1] + rest
            if head:
                lines.append(f"{current} {head}")
                current = ""
                word = rest
        if current:
            lines.append(current)
            current = ""
        piece = word
        while cell_len(piece) > room:
            head = _take_cells(piece, room)
            if not head:
                head = piece[0]
            rest = piece[len(head) :]
            if rest and rest[0] in _CLOSING_MARKS and len(head) > 1:
                head, rest = head[:-1], head[-1] + rest
            lines.append(head)
            piece = rest
        current = piece
    if current or not lines:
        lines.append(current)
    return lines


def _clip(text: str, width: int) -> str:
    """Cut ``text`` to ``width`` cells with an ellipsis, never half a wide cell."""
    if width <= 0:
        return ""
    if cell_len(text) <= width:
        return text
    if width == 1:
        return _ELLIPSIS
    limit = width - 1
    if " " in text:
        kept: list[str] = []
        for word in text.split(" "):
            candidate = word if not kept else f"{' '.join(kept)} {word}"
            if cell_len(candidate) > limit:
                break
            kept.append(word)
        if kept:
            return " ".join(kept) + _ELLIPSIS
    return _take_cells(text, limit) + _ELLIPSIS


def _take_cells(text: str, limit: int) -> str:
    """The longest prefix of ``text`` that fits in ``limit`` whole cells."""
    if limit <= 0:
        return ""
    out: list[str] = []
    used = 0
    for character in text:
        width = cell_len(character)
        if used + width > limit:
            break
        out.append(character)
        used += width
    return "".join(out)


def _pad(text: str, width: int) -> str:
    """Pad on the right so the next column starts at the same place."""
    gap = width - cell_len(text)
    return text + (" " * gap) if gap > 0 else text


def _attention_count(attention: object, tickets: list[object]) -> int:
    """The needs-you count, from the caller or from the Tickets."""
    if attention is None:
        count = sum(1 for ticket in tickets if _state(ticket) == "needs-you")
    elif isinstance(attention, bool):
        count = int(attention)
    elif isinstance(attention, (int, float)):
        count = max(0, int(attention))
    elif isinstance(attention, Mapping):
        count = 0
        for key in ("count", "needs_you", "needs-you", "attention"):
            if key in attention:
                count = _attention_count(attention[key], tickets)
                break
    elif isinstance(attention, (list, tuple, set, frozenset)):
        count = len(attention)
    elif isinstance(attention, (str, bytes)):
        try:
            count = max(0, int(str(attention).strip()))
        except ValueError:
            count = 0
    else:
        count = 0
    return count


def _state(ticket: object) -> str:
    """The token-set state of a Ticket, with the server's spelling projected."""
    raw = _text(_field(ticket, "status", "state", "phase")).strip().lower().replace(" ", "-")
    key = _STATE_ALIASES.get(raw, raw)
    try:
        status_row(key)
    except KeyError:
        return "queued"
    return key


def _glyph(state: str, theme: object) -> str:
    """The Unicode state mark, or the ASCII twin when the theme asked for one."""
    glyphs = getattr(theme, "glyphs", None)
    ascii_theme = getattr(glyphs, "done", "") == "+"
    return status_glyph(state, {"PTC_ASCII": "1"} if ascii_theme else None)


def _title(ticket: object) -> str:
    """The Ticket title, collapsed to one line; the id is the fallback."""
    value = _field(ticket, "title", "subject", "name", "ticket_id", "id")
    return _text(value) or "ticket"


def _worker(ticket: object) -> str:
    """The worker model, then provider, then a plain worker label."""
    value = _field(ticket, "worker_model", "worker_provider", "worker", "model", "provider")
    if isinstance(value, Mapping):
        value = value.get("model") or value.get("provider") or value.get("name")
    return _text(value)


def _age(ticket: object) -> str:
    """The compact age: ``12s``, ``4m12s`` or ``2h05m``."""
    value = _field(ticket, "age", "elapsed", "duration", "elapsed_seconds", "age_seconds")
    if isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return _duration(float(value))
    return _text(value)


def _duration(seconds: float) -> str:
    """A compact duration. Non-finite and negative values are omitted."""
    if not math.isfinite(seconds) or seconds < 0:
        return ""
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < _SECONDS_PER_MINUTE:
        return f"{int(seconds)}s" if seconds == int(seconds) else f"{seconds:.1f}s"
    whole = int(seconds)
    minutes, rest = divmod(whole, _SECONDS_PER_MINUTE)
    if minutes < _MINUTES_PER_HOUR:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, _MINUTES_PER_HOUR)
    return f"{hours}h{minutes:02d}m"


def _when(ticket: object) -> str:
    """The clock time of the last update, as ``HH:MM``, or ``""``."""
    value = _field(ticket, "updated_at", "updated", "when", "at", "created_at")
    if value is None:
        return ""
    if hasattr(value, "strftime"):
        try:
            return value.strftime("%H:%M")
        except (TypeError, ValueError):
            return ""
    match = re.search(r"(\d{1,2}):(\d{2})", _text(value))
    if match is None:
        return ""
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > _MAX_CLOCK_HOUR or minute > _MAX_CLOCK_MINUTE:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _field(ticket: object, *names: str) -> object:
    """The first non-empty field of a mapping or an object."""
    for name in names:
        value = ticket.get(name) if isinstance(ticket, Mapping) else getattr(ticket, name, None)
        if value is not None and value != "":
            return value
    return None


def _text(value: object) -> str:
    """One line of plain display text, with controls removed and spaces collapsed."""
    if value is None:
        return ""
    return " ".join(plain(value).split())
