"""The ``/review`` transcript rows for the terminal-native TUI.

``/review`` answers one question: which cross-reviews are waiting for a
decision. The screen is the queue title, the table of the reviews, and the
decision that is in front of the operator — the G6 human gate, the commit it
pinned, and the command that resolves it (``SPEC.md`` §7.4).

Three states, three renderers, all pure:

* :func:`usage_rows` — the usage error of a ``/review`` line with no arguments;
* :func:`progress_rows` — a queued or running review, its progress and the
  spinner frame :mod:`tui_theme` hands out;
* :func:`result_rows` — the queue itself: the title, the header, one row per
  review, and the G6 block.

A cross-review verdict and a cross-review decision are states too (§4.2): each
carries a glyph next to its word, and ``PTC_ASCII=1`` swaps the glyph for its
ASCII twin while the word stays. Every string lives in :data:`STRINGS` and is
read through :func:`ptc_cli.superagent.tui_i18n.t`.

The renderer reads no clock and calls no client — ``/review run`` shells out to
the operator CLI through :mod:`tui_local`, and this module only decides what the
transcript says. At 40 columns and below a row stacks (§3.3): an identity line
and a meta line joined by `` · ``, in the order verdict · decision · fix · gate
· reviewer, so a narrow terminal shows less and never less of what it shows.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from rich.cells import cell_len

from .text import plain
from .tui_i18n import normalize_locale, resolve_locale, t
from .tui_theme import spinner_frames, status_glyph, status_word

__all__ = ["STRINGS", "ReviewGate", "progress_rows", "result_rows", "usage_rows"]

# §3.1: every region starts two columns in, and table columns are two apart.
INSET = 2
GAP = 2
# §3.3: at this width and below a row stacks instead of shedding a column.
STACK_MAX = 40
_DEFAULT_WIDTH = 80
_ELLIPSIS = "…"
_SEPARATOR = " · "
# §3.3: the meta line sits two columns under the identity line.
_META_INDENT = 2
_MIN_TEXT = 1
_WIDE_CELL = 2
_CLOSING_MARKS = frozenset("，。、；：？！")  # noqa: RUF001

# §7.4: line(4) subject(flexible) reviewer(8) gate(8) fix(3) verdict(7)
# decision(9). The priorities are the ladder of §3.2: the reviewer is the first
# convenience column to go, the line and the subject the last. The verdict and
# the decision carry the state of the row (§4.2), so they outrank the subject.
_LINE_WIDTH = 4
_REVIEWER_WIDTH = 8
_GATE_WIDTH = 8
_FIX_WIDTH = 3
_VERDICT_WIDTH = 7
_DECISION_WIDTH = 9
# §3.2: the flexible column sits at priority 80 on the ladder, so every column
# above it (the verdict at 85, the decision at 90) is kept while the subject can
# still give up room.
_SUBJECT_PRIORITY = 80
# §3.2: the subject shrinks to the room that is left. One cell is the floor —
# the ellipsis alone — because a column narrower than that is not a column.
_SUBJECT_FLOOR = 1


@dataclass(frozen=True, slots=True)
class _Column:
    """One queue column: a fixed width (or None for the subject) and a priority."""

    key: str
    width: int | None
    priority: int
    pinned: bool = False


_COLUMNS = (
    _Column("line", _LINE_WIDTH, 100, pinned=True),
    _Column("subject", None, _SUBJECT_PRIORITY),
    _Column("reviewer", _REVIEWER_WIDTH, 45),
    _Column("gate", _GATE_WIDTH, 60),
    _Column("fix", _FIX_WIDTH, 60),
    _Column("verdict", _VERDICT_WIDTH, 85),
    _Column("decision", _DECISION_WIDTH, 90),
)
# §3.3 and the stack plan of the design mock: the identity line keeps the line
# and the subject; every other column moves under it, verdict first.
_STACK_BOTTOM = ("verdict", "decision", "fix", "gate", "reviewer")

STRINGS = {
    "en": {
        "usage": "usage: /review run <line> --by <line> | /review status [line]",
        "title": "review queue",
        "empty": "no cross-review is queued",
        "col.line": "line",
        "col.subject": "subject",
        "col.reviewer": "reviewer",
        "col.gate": "gate",
        "col.fix": "fix",
        "col.verdict": "verdict",
        "col.decision": "decision",
        "verdict.merge": "MERGE",
        "verdict.block": "BLOCK",
        "verdict.none": "—",
        "decision.approve": "approve",
        "decision.reject": "reject",
        "decision.pending": "pending",
        "gate.reviewed": "reviewed",
        "gate.build": "build",
        "gate.none": "—",
        "reviewer": "reviewer {line}",
        "head": "head {sha}",
        "gate": "G6 human gate  approve {line}",
        "pinned": "the reviewed commit is pinned at {sha}",
        "command": "conductor dispatch approve {dispatch}",
    },
    "zh-CN": {
        "usage": "用法：/review run <line> --by <line> | /review status [line]",  # noqa: RUF001
        "title": "评审队列",
        "empty": "没有排队中的交叉评审",
        "col.line": "线",
        "col.subject": "主题",
        "col.reviewer": "评审者",
        "col.gate": "闸门",
        "col.fix": "修复",
        "col.verdict": "结论",
        "col.decision": "决定",
        "verdict.merge": "合并",
        "verdict.block": "阻断",
        "verdict.none": "—",
        "decision.approve": "批准",
        "decision.reject": "驳回",
        "decision.pending": "待定",
        "gate.reviewed": "已评审",
        "gate.build": "构建中",
        "gate.none": "—",
        "reviewer": "评审者 {line}",
        "head": "提交 {sha}",
        "gate": "G6 人工闸门  approve {line}",
        "pinned": "评审过的提交固定在 {sha}",
        "command": "conductor dispatch approve {dispatch}",
    },
}


@dataclass(frozen=True, slots=True)
class _Mark:
    """One review state: a glyph, its ASCII twin and the key of its word."""

    glyph: str
    ascii: str
    key: str
    # "none yet" is a dash and nothing else: the glyph *is* the word, so a row
    # prints one dash instead of a glyph and a word that say the same thing.
    solo: bool = False


# §4.2. The ASCII twin of a state is the ASCII twin of the ink it is painted
# in: ``ok`` is ``+``, ``error`` is ``x`` and ``hold`` is ``?``. "None yet" is
# the one dash a verdict and an absent gate share: the glyph *is* the word, so a
# row prints the dash alone, and ``PTC_ASCII=1`` swaps it for its hyphen twin.
_NONE_MARK = _Mark("—", "-", "verdict.none", solo=True)
_VERDICTS = {
    "merge": _Mark("✔", "+", "verdict.merge"),
    "block": _Mark("✖", "x", "verdict.block"),
    "none": _NONE_MARK,
}
_DECISIONS = {
    "approve": _Mark("✔", "+", "decision.approve"),
    "reject": _Mark("✖", "x", "decision.reject"),
    "pending": _Mark("◆", "?", "decision.pending"),
}
# §4.2: the gate of a line is a word, not a glyph; an absent gate is the dash.
_GATE_WORDS = {"reviewed": "gate.reviewed", "build": "gate.build"}

# The server spells a review state ``queued`` until a reviewer CLI claims it;
# the token set owns the word that goes with it.
_PROGRESS_STATES = {
    "queued": "queued",
    "pending": "queued",
    "waiting": "queued",
    "new": "queued",
    "running": "running",
    "active": "running",
    "in-progress": "running",
    "in_progress": "running",
    "claimed": "running",
    "completed": "completed",
    "complete": "completed",
    "done": "completed",
}
_VERDICT_IDS = {"merge": "merge", "block": "block"}
_DECISION_IDS = {
    "approve": "approve",
    "approved": "approve",
    "reject": "reject",
    "rejected": "reject",
    "pending": "pending",
}


@dataclass(frozen=True, slots=True)
class ReviewGate:
    """The decision in front of the operator: the G6 human gate of one line."""

    line: str = ""
    sha: str = ""
    dispatch: str = ""


def usage_rows(
    locale: str | None = None,
    width: int = _DEFAULT_WIDTH,
    theme: object = None,
    detail: str = "",
) -> list[str]:
    """Render the usage error of a ``/review`` line that named no arguments.

    Args:
        locale: ``en`` or ``zh-CN`` (a ``zh_*`` locale normalises to ``zh-CN``).
        width: The terminal width in cells.
        theme: A :class:`ptc_cli.superagent.tui_theme.Theme`. It selects the
            ASCII glyphs when the theme was resolved with ``PTC_ASCII=1``.
        detail: What was wrong with the line, when the caller knows more than
            the usage string does.

    Returns:
        The rows of the error, one string per visual line. The usage line is
        prose: it wraps (§3.4) and never loses a character.
    """
    resolved = normalize_locale(locale) if locale else resolve_locale()
    width = _DEFAULT_WIDTH if width is None else max(1, int(width))
    rows = _prose_rows(f"{_error_mark(theme)} {t(STRINGS, 'usage', resolved)}", width)
    rows.extend(_prose_rows(_text(detail), width))
    return rows


def progress_rows(
    review: object,
    locale: str | None = None,
    width: int = _DEFAULT_WIDTH,
    theme: object = None,
    frame: int = 0,
) -> list[str]:
    """Render one queued or running cross-review and its progress.

    Args:
        review: The review record. A mapping or an object may carry ``line``,
            ``subject``/``title``, ``state``, ``reviewer``, ``model``/
            ``provider``/``reviewer_provider``, ``head_sha`` and a
            ``note``/``stage``.
        locale: ``en`` or ``zh-CN`` (a ``zh_*`` locale normalises to ``zh-CN``).
        width: The terminal width in cells.
        theme: A :class:`ptc_cli.superagent.tui_theme.Theme`. It selects the
            ASCII glyphs and the spinner frames when it was resolved with
            ``PTC_ASCII=1``.
        frame: The index of the spinner frame to draw. The caller advances it
            the way ``tui.py`` does; ``PTC_REDUCED_MOTION`` and ``TERM=dumb``
            leave :func:`tui_theme.spinner_frames` with one static frame.

    Returns:
        The rows of the card: the identity line, the meta line, and the note
        when the record carries one.
    """
    resolved = normalize_locale(locale) if locale else resolve_locale()
    width = _DEFAULT_WIDTH if width is None else max(1, int(width))
    line = _text(_field(review, "line", "line_id", "id")) or "—"
    subject = _text(_field(review, "subject", "title", "goal")) or "—"
    prefix = " " * INSET + _frame(theme, frame) + " " + _pad(_clip(line, _LINE_WIDTH), _LINE_WIDTH) + " " * GAP
    rows = _identity_rows(prefix, subject, width)

    reviewer = _text(_field(review, "reviewer", "reviewer_line", "by")) or "—"
    parts = [status_word(_progress_state(review), resolved)]
    parts.append(t(STRINGS, "reviewer", resolved).format(line=reviewer))
    model = _reviewer_model(review)
    if model:
        parts.append(model)
    sha = _text(_field(review, "head_sha", "head", "sha", "commit"))
    if sha:
        parts.append(t(STRINGS, "head", resolved).format(sha=sha))
    rows.extend(_meta_rows([part for part in parts if part], width))
    rows.extend(_prose_rows(_text(_field(review, "note", "stage", "summary")), width))
    return rows


def result_rows(
    reviews: Iterable[object] | None,
    gate: object = None,
    locale: str | None = None,
    width: int = _DEFAULT_WIDTH,
    theme: object = None,
) -> list[str]:
    """Render the review queue: the title, the table, and the G6 block.

    Args:
        reviews: The queued reviews. A mapping or an object may carry ``line``,
            ``subject``/``title``, ``reviewer``, ``model``/``provider``,
            ``gate``, ``fix``, ``verdict`` and ``decision``.
        gate: The decision in front of the operator — a :class:`ReviewGate` or
            a mapping with ``line``, ``sha``/``head_sha`` and
            ``dispatch``/``dispatch_id``. ``None`` renders no G6 block.
        locale: ``en`` or ``zh-CN`` (a ``zh_*`` locale normalises to ``zh-CN``).
        width: The terminal width in cells.
        theme: A :class:`ptc_cli.superagent.tui_theme.Theme`. It selects the
            ASCII glyphs when the theme was resolved with ``PTC_ASCII=1``.

    Returns:
        The rows of the transcript, one string per visual line.
    """
    resolved = normalize_locale(locale) if locale else resolve_locale()
    width = _DEFAULT_WIDTH if width is None else max(1, int(width))
    rows = _prose_rows(t(STRINGS, "title", resolved), width)
    cells = [_review_cells(review, resolved, theme) for review in (reviews or ())]
    if cells:
        header = {column.key: t(STRINGS, f"col.{column.key}", resolved) for column in _COLUMNS}
        rows.extend(_queue_rows(header, cells, width))
    else:
        rows.extend(_prose_rows(t(STRINGS, "empty", resolved), width))
    if gate is not None:
        rows.append("")
        rows.extend(_gate_rows(gate, resolved, width))
    return rows


def _queue_rows(header: dict[str, str], cells: list[dict[str, str]], width: int) -> list[str]:
    """The header and one row per review, stacked at 40 and tabled above it."""
    natural = max([cell_len(header["subject"]), *(cell_len(cell["subject"]) for cell in cells)])
    if width <= STACK_MAX:
        rows = _stacked_identity(header, width)
        for cell in cells:
            rows.extend(_stacked_identity(cell, width))
            parts = [cell[key] for key in _STACK_BOTTOM if cell[key]]
            rows.extend(_meta_rows(parts, width))
        return rows
    kept = _kept_columns(width, natural)
    subject_width = _subject_width(kept, width, natural)
    return [_table_row(row, kept, subject_width) for row in (header, *cells)]


def _kept_columns(width: int, natural: int) -> list[_Column]:
    """The columns that survive in ``width``, lowest priority sacrificed first.

    The rule is §3.2: a fixed column whose priority is below the flexible
    subject's is dropped before the subject shrinks — the reviewer first, then
    the gate and the fix count. The verdict and the decision carry the state of
    the row (§4.2), so they outrank the subject: the subject gives up its room
    down to :data:`_SUBJECT_FLOOR` before either state goes, and only a row with
    no room at all drops one. The line and the subject are never dropped.
    """
    kept = list(_COLUMNS)
    while True:
        if not any(column.width is None for column in kept):
            return kept
        if _spare(kept, width) >= natural:
            return kept
        victims = [column for column in kept if _droppable(column) and column.priority < _SUBJECT_PRIORITY]
        if not victims and _spare(kept, width) < _SUBJECT_FLOOR:
            # No room is left for the subject at all, so the states go too —
            # lowest priority first, and only then.
            victims = [column for column in kept if _droppable(column)]
        if not victims:
            return kept
        kept.remove(min(victims, key=lambda column: (column.priority, _COLUMNS.index(column))))


def _droppable(column: _Column) -> bool:
    """A column the ladder may drop: a fixed one, and never the pinned line."""
    return column.width is not None and not column.pinned


def _spare(kept: list[_Column], width: int) -> int:
    """The room the flexible column has left once the fixed ones are placed."""
    fixed = sum(column.width or 0 for column in kept)
    gaps = GAP * (len(kept) - 1)
    return max(0, width - INSET - fixed - gaps)


def _subject_width(kept: list[_Column], width: int, natural: int) -> int:
    """The subject is as wide as its content, never wider than the room left."""
    return max(0, min(natural, _spare(kept, width)))


def _table_row(row: dict[str, str], kept: list[_Column], subject_width: int) -> str:
    """One table row laid out on the columns that survived."""
    parts: list[str] = []
    for column in kept:
        limit = subject_width if column.width is None else column.width
        parts.append(_pad(_clip(row[column.key], limit), limit))
    return (" " * INSET + (" " * GAP).join(parts)).rstrip()


def _stacked_identity(row: dict[str, str], width: int) -> list[str]:
    """The identity line of a stacked row: the line column and the subject.

    A subject that does not fit wraps under the line column instead of pushing
    it out of the row (§3.3), so a Chinese title keeps the marker with it.
    """
    prefix = " " * INSET + _pad(_clip(row["line"], _LINE_WIDTH), _LINE_WIDTH) + " " * GAP
    return _identity_rows(prefix, row["subject"], width)


def _identity_rows(prefix: str, subject: str, width: int) -> list[str]:
    """The identity line of one row, with the subject wrapped under the prefix."""
    lines = _wrap(subject, max(_MIN_TEXT, width - cell_len(prefix)))
    rows = [(prefix + lines[0]).rstrip()]
    rows.extend((" " * cell_len(prefix) + line).rstrip() for line in lines[1:])
    return rows


def _meta_rows(parts: list[str], width: int) -> list[str]:
    """The meta line of a stacked row: every other column, `` · ``-joined."""
    indent = " " * (INSET + _META_INDENT)
    room = max(_MIN_TEXT, width - INSET - _META_INDENT)
    return [(indent + line).rstrip() for line in _wrap_parts(parts, room)]


def _prose_rows(text: str, width: int) -> list[str]:
    """A prose row wrapped into the inset, keeping every character (§3.4)."""
    if not text:
        return []
    room = max(_MIN_TEXT, width - INSET)
    return [(" " * INSET + line).rstrip() for line in _wrap(text, room)]


def _gate_rows(gate: object, locale: str, width: int) -> list[str]:
    """The G6 human gate: the decision, the pinned commit, the command."""
    line, sha, dispatch = _gate_fields(gate)
    rows = _prose_rows(t(STRINGS, "gate", locale).format(line=line or "—"), width)
    if sha:
        rows.extend(_prose_rows(t(STRINGS, "pinned", locale).format(sha=sha), width))
    if dispatch:
        rows.extend(_prose_rows(t(STRINGS, "command", locale).format(dispatch=dispatch), width))
    return rows


def _gate_fields(gate: object) -> tuple[str, str, str]:
    """The line, the pinned commit and the dispatch of one human gate."""
    return (
        _text(_field(gate, "line", "line_id")),
        _text(_field(gate, "sha", "head_sha", "head", "commit", "pinned")),
        _text(_field(gate, "dispatch", "dispatch_id", "id")),
    )


def _review_cells(review: object, locale: str, theme: object) -> dict[str, str]:
    """The seven column values of one review, before layout."""
    return {
        "line": _text(_field(review, "line", "line_id", "id")) or "—",
        "subject": _text(_field(review, "subject", "title", "goal")) or "—",
        "reviewer": _reviewer_cell(review),
        "gate": _gate_word(review, locale, theme),
        "fix": _fix_count(review),
        "verdict": _mark(_VERDICTS, _verdict_id(review), locale, theme),
        "decision": _mark(_DECISIONS, _decision_id(review), locale, theme),
    }


def _reviewer_cell(review: object) -> str:
    """The reviewer cell: the reviewer line, and its model when there is one."""
    line = _text(_field(review, "reviewer", "reviewer_line", "by")) or "—"
    model = _reviewer_model(review)
    return f"{line} {model}" if model else line


def _reviewer_model(review: object) -> str:
    """The model the reviewer runs, from whichever field carries it."""
    return _text(_field(review, "reviewer_model", "model", "provider", "reviewer_provider"))


def _gate_word(review: object, locale: str, theme: object) -> str:
    """The gate of a line is a word: reviewed, build, or the dash of §4.2.

    The dash is the shared :data:`_NONE_MARK`, so an absent gate is ``-`` under
    ``PTC_ASCII=1`` just as a verdict with no word yet is.
    """
    raw = _text(_field(review, "gate", "gate_phase", "phase")).strip().lower()
    key = _GATE_WORDS.get(raw)
    if key is None:
        return _NONE_MARK.ascii if _ascii_mode(theme) else _NONE_MARK.glyph
    return t(STRINGS, key, locale)


def _fix_count(review: object) -> str:
    """The fix round of the review, ``0`` when the line has not been fixed."""
    value = _field(review, "fix", "fix_round", "fix_rounds", "fixes", "fix_count")
    if value is None or isinstance(value, bool):
        return "0"
    if isinstance(value, (int, float)):
        return str(int(value))
    return _text(value) or "0"


def _verdict_id(review: object) -> str:
    """The verdict of a review, or ``none`` when the reviewer has not spoken."""
    raw = _text(_field(review, "verdict", "review_verdict", "result")).strip().lower()
    return _VERDICT_IDS.get(raw, "none")


def _decision_id(review: object) -> str:
    """The operator's decision; a queue row waits for one by default."""
    raw = _text(_field(review, "decision", "operator_decision")).strip().lower()
    return _DECISION_IDS.get(raw, "pending")


def _mark(table: dict[str, _Mark], ident: str, locale: str, theme: object) -> str:
    """One review state as a glyph and a word, or the dash when there is none."""
    mark = table.get(ident) or table["none"]
    glyph = mark.ascii if _ascii_mode(theme) else mark.glyph
    if mark.solo:
        return glyph
    return f"{glyph} {t(STRINGS, mark.key, locale)}"


def _progress_state(review: object) -> str:
    """The token-set state of a review in flight, with the server's spelling."""
    raw = _text(_field(review, "state", "status", "phase")).strip().lower().replace(" ", "-")
    return _PROGRESS_STATES.get(raw, "queued")


def _ascii_mode(theme: object = None, environ: Mapping[str, str] | None = None) -> bool:
    """True when the glyphs are the ASCII twins rather than the Unicode ones.

    A theme resolved with ``PTC_ASCII=1`` says so through its glyph set; a
    caller without a theme falls back to the variable itself, the way
    :func:`tui_theme.status_glyph` does.
    """
    glyphs = getattr(theme, "glyphs", None)
    if getattr(glyphs, "done", "") == "+":
        return True
    environment = os.environ if environ is None else environ
    return str(environment.get("PTC_ASCII", "")).strip() == "1"


def _error_mark(theme: object = None) -> str:
    """The error mark of a usage line: ``✖``, or ``x`` in ASCII.

    The twin comes from :func:`tui_theme.status_glyph` rather than from the
    theme's own glyph set, so an ASCII theme reads the same ``x`` the
    ``PTC_ASCII=1`` variable selects for the rest of the screen.
    """
    if _ascii_mode(theme):
        return status_glyph("failed", {"PTC_ASCII": "1"})
    return status_glyph("failed")


def _frame(theme: object, index: int) -> str:
    """The spinner frame a queued or running row draws.

    The frames belong to :mod:`tui_theme` — ``♪ ♫ ♬ ♫``, one static frame under
    reduced motion, ``*`` in ASCII — and :func:`tui_theme.spinner_frames` is the
    one place that rule is written down. A resolved theme carries an animated
    glyph set but never the motion switch, so a frozen :func:`spinner_frames`
    (reduced motion or ASCII) wins over the spinner a supplied theme brings.

    A frozen frame is the glyph of the running state, and :func:`spinner_frames`
    reads the locale rather than the theme: under ``TERM=dumb`` it hands out the
    Unicode ``●`` whatever the caller's theme says. An ASCII theme still gets
    that state's ASCII twin, so the mark matches the rest of the row.
    """
    frames = spinner_frames()
    if len(frames) == 1:
        return _frozen_frame(frames[0], theme)
    glyphs = getattr(theme, "glyphs", None)
    supplied = getattr(glyphs, "spinner", None)
    if isinstance(supplied, (list, tuple)) and supplied:
        candidate = tuple(str(item) for item in supplied if str(item))
        if candidate:
            frames = candidate
    return frames[int(index) % len(frames)]


def _frozen_frame(frozen: str, theme: object) -> str:
    """The one frame a frozen row draws, in the glyph set of its theme."""
    if not _ascii_mode(theme):
        return frozen
    return status_glyph("running", {"PTC_ASCII": "1"})


def _wrap_parts(parts: list[str], room: int) -> list[str]:
    """Join `` · ``-separated parts, wrapping whole parts before cutting one.

    A part wider than a whole line wraps inside itself, because §3.3 allows a
    narrow terminal to show less, never to lose a column.
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

    Every piece is kept: the rule of §3.4 is that a prose row wraps, and a word
    longer than the line is cut across as many lines as it needs.
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


def _field(value: object, *names: str) -> object:
    """The first non-empty field of a mapping or an object."""
    for name in names:
        found = value.get(name) if isinstance(value, Mapping) else getattr(value, name, None)
        if found is not None and found != "":
            return found
    return None


def _text(value: object) -> str:
    """One line of plain display text, with controls removed and spaces collapsed."""
    if value is None:
        return ""
    return " ".join(plain(value).split())
