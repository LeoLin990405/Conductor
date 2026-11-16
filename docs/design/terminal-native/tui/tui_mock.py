#!/usr/bin/env python3
"""Terminal-native mock of every Super-agent TUI screen.

The mock draws the screens the terminal-native style has to cover — home,
``/jobs``, ``/dispatch``, ``/review``, the running ticket stream, the
palette completion (a fuzzy command query and an argument completion), and
the permission prompt — with the colours read from the shared token set in
``../system/tokens.json``. It imports rich and the standard library only::

    python docs/design/terminal-native/tui/tui_mock.py

Every screen is drawn in English and in zh-CN. Next to this file it writes:

* ``tui-<screen>.svg`` / ``tui-<screen>.txt`` — the primary render, English,
  100 x 32 (the base width of the native view);
* ``tui-<screen>-<lang>-w<cols>.svg`` / ``.txt`` — every language at the
  three contract widths 40, 80 and 120, 32 rows tall.

Any other ``tui-*.svg`` / ``tui-*.txt`` in the output directory is an
obsolete artefact and is deleted, so the directory always matches the script.

The layout contract is the one the TUI unit tests use: a screen has to hold at
40, 80 and 120 columns (``test_compact_layout_contract`` in
``libs/ptc-cli/tests/unit_tests/test_tui_review_blockers.py``, and the glyph
goldens in ``test_tui_snapshots.py``). Every check runs before anything is
written; when one fails the script prints the problems, writes nothing and
exits 1.

The token set is the sibling line's deliverable. A branch that does not have
it yet can still prove the layout, because the layout does not depend on the
colour: the script then checks the contract without colour and leaves the
coloured artefacts untouched, rather than rewriting them in no colour at all.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from rich.console import CONSOLE_SVG_FORMAT, Console, Group
from rich.terminal_theme import TerminalTheme
from rich.text import Text

HERE = Path(__file__).resolve().parent
DEFAULT_TOKENS = HERE.parent / "system" / "tokens.json"

# The base width the artefacts are drawn at. It is the width the shipped TUI
# falls back to when the terminal size cannot be read, and the width the
# Rich panels use (`DEFAULT_WIDTH` in `tui_rich.py`).
BASE_WIDTH = 100
FRAME_HEIGHT = 32

# The four widths the specification describes: the base width and the three
# contract widths the TUI unit tests use.
WIDTHS = (120, 100, 80, 40)

# The contract widths that get their own artefact.
CONTRACT_WIDTHS = (40, 80, 120)

# At or below this width a table row stacks: the row becomes two lines, the
# identity on the first and the meta columns on the second, instead of
# dropping a column.
STACK_MAX = 40

# The widths the prose rows have to wrap at. A row that does not fit is
# never cut, so the text of a screen is the same at every width.
LOSSLESS_WIDTHS = (40, 80)

# space.2: two columns between list columns, and the inset a row glyph
# already carries.
GAP = 2
INSET = 2

# The composer wall is drawn above 60 columns, the same cut the shipped TUI
# uses (`_border_enabled` in `libs/ptc-cli/ptc_cli/superagent/tui.py`).
COMPOSER_WALL_MIN_WIDTH = 60

# The terminal monospace the token set names. The SVG template rich ships
# points at a webfont on a CDN; this one points at local fonts only, so the
# artefact opens without a network request.
FONT_STACK = (
    '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, '
    '"Noto Sans Mono CJK SC", "Noto Sans SC", "PingFang SC", monospace'
)


def svg_format() -> str:
    """Point rich's SVG template at a local font instead of a CDN.

    The template rich ships loads Fira Code from a webfont CDN. The token set
    names JetBrains Mono, and an artefact that opens without a network
    request is worth the few lines this takes.
    """
    lines: list[str] = []
    for line in CONSOLE_SVG_FORMAT.splitlines():
        if 'src: local("FiraCode-Regular")' in line:
            lines.append(
                '        src: local("JetBrainsMono-Regular"), local("JetBrains Mono");'
            )
            continue
        if 'src: local("FiraCode-Bold")' in line:
            lines.append(
                '        src: local("JetBrainsMono-Bold"), local("JetBrains Mono");'
            )
            continue
        if "url(" in line and "FiraCode" in line:
            continue
        lines.append(
            line.replace('font-family: "Fira Code";', 'font-family: "JetBrains Mono";')
        )
    text = "\n".join(lines)
    if CONSOLE_SVG_FORMAT.endswith("\n"):
        text += "\n"
    return text.replace(
        "font-family: Fira Code, monospace;",
        f"font-family: {FONT_STACK};",
    )


SVG_FORMAT = svg_format()


# --------------------------------------------------------------------------
# Cell widths
# --------------------------------------------------------------------------


# East-Asian wide characters take two cells, combining marks take none. The
# ranges are the ones a terminal cell is built on: the CJK blocks, the
# Hangul syllables, the fullwidth forms, and the emoji planes.
_WIDE_RANGES = (
    (0x1100, 0x115F),
    (0x2E80, 0x303E),
    (0x3041, 0x33FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xA000, 0xA4CF),
    (0xAC00, 0xD7A3),
    (0xF900, 0xFAFF),
    (0xFE10, 0xFE19),
    (0xFE30, 0xFE6F),
    (0xFF00, 0xFF60),
    (0xFFE0, 0xFFE6),
    (0x1F300, 0x1F64F),
    (0x1F900, 0x1F9FF),
    (0x20000, 0x2FFFD),
    (0x30000, 0x3FFFD),
)

# Combining marks ride on the character before them.
_ZERO_RANGES = (
    (0x0300, 0x036F),
    (0x1AB0, 0x1AFF),
    (0x1DC0, 0x1DFF),
    (0x20D0, 0x20FF),
    (0xFE20, 0xFE2F),
)

# The closing marks that never open a line. A line that would start with
# one of them takes the character above it down with it.
CJK_BREAKS = "，。、；：？！"


def _in_ranges(code: int, ranges: tuple[tuple[int, int], ...]) -> bool:
    for low, high in ranges:
        if low <= code <= high:
            return True
    return False


def char_width(char: str) -> int:
    """The columns one character takes."""
    code = ord(char)
    if _in_ranges(code, _ZERO_RANGES):
        return 0
    if _in_ranges(code, _WIDE_RANGES):
        return 2
    return 1


def display_width(text: str) -> int:
    """Columns a string occupies in a monospace cell.

    The TUI measures the same way (``_display_width`` in ``tui.py``).
    """
    return sum(char_width(char) for char in text)


def take_columns(text: str, limit: int) -> str:
    """Cut a string to ``limit`` columns, without splitting a wide cell."""
    if limit <= 0:
        return ""
    out: list[str] = []
    used = 0
    for char in text:
        width = char_width(char)
        if used + width > limit:
            break
        out.append(char)
        used += width
    return "".join(out)


def clip(text: str, width: int, *, word: bool = True) -> str:
    """Shorten a string to ``width`` columns, marking the cut with an ellipsis.

    ``word=True`` cuts on a word boundary, so a narrow terminal never ends a
    line mid-word. A string without spaces is cut by column count.
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    limit = width - 1
    if word:
        kept: list[str] = []
        for piece in text.split(" "):
            candidate = piece if not kept else f"{' '.join(kept)} {piece}"
            if display_width(candidate) > limit:
                break
            kept.append(piece)
        if kept:
            return f"{' '.join(kept)}…"
    return f"{take_columns(text, limit)}…"


def pad(text: str, width: int) -> str:
    """Pad on the right so the next column starts in the same place."""
    gap = width - display_width(text)
    return text + " " * gap if gap > 0 else text


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


# The words that go with the state glyphs. The token set names them as
# well; this copy is what a branch without the token set draws.
STATUS_LABELS = {
    "queued": "Queued",
    "running": "Running",
    "blocked": "Blocked",
    "needs-you": "Needs you",
    "failed": "Failed",
    "completed": "Completed",
    "archived": "Archived",
}

# The glyphs and their ASCII twins, in the order of `status[]` of the token
# set. The token set is the source; this copy is what a branch without it
# draws, and `check_tokens` fails the build when the two disagree.
STATUS_GLYPHS = {
    "queued": "○",
    "running": "●",
    "blocked": "▣",
    "needs-you": "◆",
    "failed": "✖",
    "completed": "✔",
    "archived": "⊘",
}
STATUS_ASCII = {
    "queued": "o",
    "running": "*",
    "blocked": "#",
    "needs-you": "?",
    "failed": "x",
    "completed": "+",
    "archived": "-",
}

# The zh-CN words of the seven states. The glyph is the same in every
# language; only the word is localized (SPEC.md section 4.4).
STATUS_LABELS_ZH = {
    "queued": "排队中",
    "running": "运行中",
    "blocked": "已阻塞",
    "needs-you": "需要你",
    "failed": "失败",
    "completed": "已完成",
    "archived": "已归档",
}


@dataclass(frozen=True)
class Palette:
    """One resolved theme: the token names the screens are written against."""

    name: str
    page: str
    canvas: str
    elevated: str
    selection: str
    accent: str
    fg: str
    muted: str
    baton: str
    tempo: str
    ok: str
    hold: str
    error: str
    on_accent: str
    faint: str
    rule: str
    glyphs: dict[str, str]
    labels: dict[str, str]

    def colour(self, token: str) -> str:
        """The hex behind a token name. An unknown name paints nothing."""
        return {
            "page": self.page,
            "canvas": self.canvas,
            "elevated": self.elevated,
            "selection": self.selection,
            "accent": self.accent,
            "fg": self.fg,
            "muted": self.muted,
            "baton": self.baton,
            "tempo": self.tempo,
            "ok": self.ok,
            "hold": self.hold,
            "error": self.error,
            "onAccent": self.on_accent,
            "faint": self.faint,
            "rule": self.rule,
        }.get(token, "")

    def style(self, expression: str) -> str:
        """Turn ``"baton bold"`` into a rich style string."""
        parts = expression.split()
        if not parts:
            return ""
        colour = self.colour(parts[0])
        rest = parts[1:]
        if not colour:
            return " ".join(rest)
        return " ".join([colour, *rest])

    @classmethod
    def blank(cls) -> "Palette":
        """A palette with no colour in it.

        The layout of a screen does not depend on its colour, so a branch
        that does not have the token set yet can still prove the layout.
        """
        return cls(
            name="none",
            page="",
            canvas="",
            elevated="",
            selection="",
            accent="",
            fg="",
            muted="",
            baton="",
            tempo="",
            ok="",
            hold="",
            error="",
            on_accent="",
            faint="",
            rule="",
            glyphs=dict(STATUS_GLYPHS),
            labels=dict(STATUS_LABELS),
        )

    def terminal_theme(self) -> TerminalTheme:
        """A rich terminal theme whose grounds are the token hexes."""
        normal = [
            _rgb(self.page),
            _rgb(self.error),
            _rgb(self.ok),
            _rgb(self.hold),
            _rgb(self.baton),
            _rgb(self.baton),
            _rgb(self.tempo),
            _rgb(self.fg),
        ]
        bright = [
            _rgb(self.muted),
            _rgb(self.error),
            _rgb(self.ok),
            _rgb(self.hold),
            _rgb(self.baton),
            _rgb(self.baton),
            _rgb(self.tempo),
            _rgb(self.fg),
        ]
        return TerminalTheme(
            background=_rgb(self.page),
            foreground=_rgb(self.fg),
            normal=normal,
            bright=bright,
        )


def _rgb(value: str) -> tuple[int, int, int]:
    """``"#8B9CFF"`` to an RGB triple."""
    text = value.strip().lstrip("#")
    if len(text) != 6:
        return (0, 0, 0)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def load_tokens(path: Path) -> dict:
    """Read the shared token set.

    A missing file is not an error here: the caller decides what a branch
    without the token set can still do.
    """
    raw = path.read_text(encoding="utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"token file is not valid JSON: {path} ({exc})") from exc
    if "themes" not in document:
        raise SystemExit(f"token file has no themes: {path}")
    return document


def build_palette(document: dict, theme: str = "dark") -> Palette:
    """Resolve one theme out of the token set."""
    themes = document.get("themes", {})
    if theme not in themes:
        raise SystemExit(f"unknown theme {theme!r} in the token set")
    surfaces = themes[theme].get("surfaces", {})
    ink = themes[theme].get("ink", {})
    glyphs: dict[str, str] = dict(STATUS_GLYPHS)
    # The token set owns the glyphs and the labels; the script's copy fills
    # in a state the token set does not name.
    labels: dict[str, str] = dict(STATUS_LABELS)
    for row in document.get("status", []):
        if not isinstance(row, dict):
            continue
        ident = str(row.get("id") or "")
        glyph = str(row.get("glyph") or "")
        if ident and glyph:
            glyphs[ident] = glyph
        label = str(row.get("label") or "")
        if ident and label:
            labels[ident] = label
    return Palette(
        name=theme,
        page=str(surfaces.get("page", "")),
        canvas=str(surfaces.get("canvas", "")),
        elevated=str(surfaces.get("elevated", "")),
        selection=str(surfaces.get("selection", "")),
        accent=str(surfaces.get("accent", "")),
        fg=str(ink.get("fg", "")),
        muted=str(ink.get("muted", "")),
        baton=str(ink.get("baton", "")),
        tempo=str(ink.get("tempo", "")),
        ok=str(ink.get("ok", "")),
        hold=str(ink.get("hold", "")),
        error=str(ink.get("error", "")),
        on_accent=str(ink.get("onAccent", "")),
        faint=str(ink.get("faint", "")),
        rule=str(ink.get("rule", "")),
        glyphs=glyphs,
        labels=labels,
    )


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Col:
    """One column of a list.

    ``width`` is the column's fixed width. A column with ``width=None`` is the
    flexible one: it takes whatever the row has left. ``priority`` decides
    which column is dropped first when the row does not fit; the flexible
    column is the last one to go. ``gap`` is the space after the column, so a
    marker can sit against its text while the other columns keep the
    space.2 separation. ``sep`` replaces that space with a literal, for a
    row that reads as ``a · b · c``. ``truncatable`` marks a flexible column
    whose text may be cut with an ellipsis: a hint gives up its room, the
    title of a row does not. ``pinned`` marks a column that is never
    dropped: the marker of the focused row, the glyph of its state and the
    word that goes with it. A title that does not fit wraps or is cut, but
    it never pushes the marker of the row out of it.
    """

    key: str
    width: int | None = None
    min_width: int = 0
    priority: int = 0
    align: str = "left"
    gap: int = GAP
    sep: str = ""
    truncatable: bool = False
    pinned: bool = False

    def separator(self) -> str:
        """The text that follows this column."""
        return self.sep if self.sep else " " * self.gap


@dataclass(frozen=True)
class Cell:
    """One styled piece of a row."""

    text: str
    style: str = "fg"


@dataclass(frozen=True)
class StackPlan:
    """How a table row stacks at the narrow width.

    ``top`` names the columns that stay on the first line. ``bottom`` names
    the columns that move to the second line, joined by ``sep``. Nothing is
    dropped by the stack itself: the row keeps every column it had, on one
    line or on two.
    """

    top: tuple[str, ...]
    bottom: tuple[str, ...]
    sep: str = " · "
    indent: int = 2


@dataclass(frozen=True)
class Row:
    """A row of a screen.

    A ``text`` row is one styled line. A ``table`` row is a list of cells
    laid out on the given columns. A text row may carry a ``prefix``: the
    rail of a card is drawn in its own ink while the body keeps its own. A
    ``divider`` row is a rule inside a box.
    """

    kind: str = "text"
    text: str = ""
    style: str = "fg"
    prefix: str = ""
    prefix_style: str = "fg"
    cells: tuple[Cell, ...] = ()
    columns: tuple[Col, ...] = ()
    indent: int = 0
    stack: StackPlan | None = None
    header: bool = False

    def cell(self, key: str) -> Cell:
        """The cell of a column, by key."""
        for index, column in enumerate(self.columns):
            if column.key == key:
                return self.cells[index]
        raise KeyError(key)


@dataclass
class Screen:
    """One TUI screen: the body, the command region, and the chrome."""

    name: str
    title: str
    lang: str = "en"
    body: list[Row] = field(default_factory=list)
    region_header: Row | None = None
    region: list[Row] = field(default_factory=list)
    composer: str = ""
    composer_style: str = "prompt"
    placeholder: tuple[str, str] = ("", "")
    session: str = ""
    counts: tuple[str, ...] = ()
    hints: tuple[str, ...] = ()
    permission: str = ""
    permission_keys: tuple[str, ...] = ()
    stream: tuple[str, str] = ("○", "idle")
    anchor: str = "bottom"
    anchors: tuple[str, ...] = ()
    wide_anchors: tuple[str, ...] = ()
    narrow_anchors: tuple[str, ...] = ()
    forbidden: tuple[str, ...] = ()


def table_row(
    columns: Sequence[Col],
    cells: Sequence[tuple[str, str]],
    *,
    indent: int = 0,
    stack: StackPlan | None = None,
    header: bool = False,
) -> Row:
    """Build a table row from ``(text, style)`` pairs."""
    return Row(
        kind="table",
        cells=tuple(Cell(text, style) for text, style in cells),
        columns=tuple(columns),
        indent=indent,
        stack=stack,
        header=header,
    )


def text_row(
    text: str,
    style: str = "fg",
    *,
    indent: int = 0,
    prefix: str = "",
    prefix_style: str = "fg",
) -> Row:
    """Build a one-line row."""
    return Row(
        kind="text",
        text=text,
        style=style,
        indent=indent,
        prefix=prefix,
        prefix_style=prefix_style,
    )


def _row_room(
    columns: Sequence[Col],
    kept: Sequence[int],
    room: int,
) -> tuple[int, int]:
    """The width the fixed columns take and the room that is left.

    The separators are the space between the columns, so a row that keeps
    fewer columns is a row that is shorter by exactly the space it drops.
    """
    fixed = sum(columns[index].width or 0 for index in kept)
    seps = sum(display_width(columns[index].separator()) for index in kept[:-1])
    return fixed + seps, room - fixed - seps


def _fit_columns(
    columns: Sequence[Col],
    room: int,
    desired: dict[str, int],
) -> list[int]:
    """The columns that survive in ``room`` columns.

    The row is laid out in the order the columns are given. When it does not
    fit, the lowest-priority column is sacrificed: a fixed column is dropped
    and the flexible one keeps its place. A flexible column that may be
    truncated — a hint — gives up its room first; a flexible column that
    carries the identity of the row — a title — is cut only when there is
    nothing left to drop. A pinned column is never dropped: the marker of
    the focused row and the glyph of its state stay, whatever the title
    does.
    """
    kept = list(range(len(columns)))
    while True:
        used, spare = _row_room(columns, kept, room)
        flexible = [index for index in kept if columns[index].width is None]
        if not flexible:
            return kept
        wanted = max(desired.get(columns[index].key, 0) for index in flexible)
        if spare >= wanted:
            return kept
        column = columns[flexible[0]]
        if column.truncatable and spare >= column.min_width:
            # The hint gives up its room: the row stays whole.
            return kept
        victims = [
            index
            for index in kept
            if columns[index].width is not None and not columns[index].pinned
        ]
        if not victims:
            return kept
        victim = min(victims, key=lambda index: (columns[index].priority, index))
        kept.remove(victim)


def row_room(row: Row, width: int) -> int:
    """The columns the text of a row has left.

    The indent and the rail take their columns first: the text never
    competes with the rail for room, and a row that does not fit wraps
    instead of being cut.
    """
    return max(1, width - row.indent - display_width(row.prefix))


def text_row_lines(row: Row, width: int) -> list[str]:
    """The text of a text row, one entry per visual line.

    The room is what the terminal has left after the indent and the rail.
    A text that does not fit is wrapped, and every character of it is kept.
    """
    room = row_room(row, width)
    if display_width(row.text) <= room:
        return [row.text]
    return wrap_text(row.text, room)


def wrap_row(row: Row, width: int) -> list[list[Cell]]:
    """Lay a text row out, wrapping the text when it does not fit."""
    out: list[list[Cell]] = []
    for line in text_row_lines(row, width):
        cells: list[Cell] = [Cell(" " * row.indent, "fg")]
        if row.prefix:
            cells.append(Cell(row.prefix, row.prefix_style))
        cells.append(Cell(line, row.style))
        out.append(cells)
    return out


def layout_row(
    row: Row,
    width: int,
    natural: dict[tuple[Col, ...], dict[str, int]] | None = None,
) -> list[list[Cell]]:
    """Lay one row out at ``width`` and return its visual lines."""
    if row.kind == "table":
        table = (natural or {}).get(row.columns, {})
        if row.stack is not None and width <= STACK_MAX:
            return _layout_stacked(row, width, table)
        return _layout_table(row, width, table)
    return wrap_row(row, width)


def _layout_stacked(
    row: Row,
    width: int,
    natural: dict[str, int],
) -> list[list[Cell]]:
    """Lay a table row out as an identity line and a meta line.

    The stack is the narrow form of a table. The columns named in the plan's
    ``top`` stay on the first line — the identity of the row — and the
    columns named in the ``bottom`` move to a line under it, joined by the
    plan's separator. The meta line wraps over as many lines as it needs:
    nothing is dropped, because a narrow terminal shows less, not less of
    what it shows.
    """
    plan = row.stack
    assert plan is not None
    lines = _layout_identity(row, width, natural, plan)
    if not plan.bottom:
        return lines
    room = max(1, width - row.indent - plan.indent)
    for chunk in _stack_parts(row, plan, room):
        lines.append([Cell(" " * (row.indent + plan.indent), "fg"), *chunk])
    return lines


def _layout_identity(
    row: Row,
    width: int,
    natural: dict[str, int],
    plan: StackPlan,
) -> list[list[Cell]]:
    """Lay the identity line of a stacked row out.

    The identity line is the one line of a row that says what the row is:
    the marker of the focused row, the glyph of its state and its title.
    The marker and the glyph are never dropped. A title that does not fit
    wraps under them instead, so a narrow terminal shows the title whole
    and never loses the mark of the row — a Chinese title is twice as wide
    as an English one of the same length, and it must not push the marker
    out of the row.
    """
    top_columns = tuple(col for col in row.columns if col.key in plan.top)
    top_cells = tuple(row.cell(key) for key in plan.top)
    flexible = [
        index for index, col in enumerate(top_columns) if col.width is None
    ]
    if not flexible or flexible[0] != len(top_columns) - 1:
        # A group without a flexible column, or with a column behind it,
        # is a plain table row.
        return _layout_table(
            Row(
                kind="table",
                cells=top_cells,
                columns=top_columns,
                indent=row.indent,
            ),
            width,
            natural,
        )
    prefix = top_columns[: flexible[0]]
    title = top_cells[flexible[0]]
    prefix_width = sum(col.width or 0 for col in prefix)
    seps = sum(display_width(col.separator()) for col in prefix)
    start = row.indent + prefix_width + seps
    room = max(1, width - start)
    out: list[list[Cell]] = []
    for index, line in enumerate(wrap_text(title.text, room)):
        if index:
            # The title continues under itself: the marker stays on the
            # first line, the title keeps its column.
            out.append([Cell(" " * start, "fg"), Cell(line, title.style)])
            continue
        cells: list[Cell] = [Cell(" " * row.indent, "fg")]
        for position, col in enumerate(prefix):
            cell = top_cells[position]
            width_of = col.width or 0
            cells.append(Cell(pad(clip(cell.text, width_of), width_of), cell.style))
            cells.append(Cell(col.separator(), "fg"))
        cells.append(Cell(line, title.style))
        out.append(cells)
    return out


def _stack_parts(row: Row, plan: StackPlan, room: int) -> list[list[Cell]]:
    """The meta line of a stacked row, wrapped over as many lines as it needs.

    The parts keep their order and their ink. A part that does not fit on
    the line starts the next line; the separator is left behind with the
    line it ended. A part that does not fit on a line of its own wraps over
    as many lines as it needs: no character of it is cut.
    """
    lines: list[list[Cell]] = []
    current: list[Cell] = []
    used = 0
    for key in plan.bottom:
        cell = row.cell(key)
        text = cell.text
        width = display_width(text)
        separator = plan.sep if current else ""
        sep_width = display_width(separator)
        if current and used + sep_width + width > room:
            lines.append(current)
            current = []
            used = 0
            separator = ""
            sep_width = 0
        if not current and width > room:
            pieces = wrap_text(text, room)
            for piece in pieces[:-1]:
                lines.append([Cell(piece, cell.style)])
            text = pieces[-1]
            width = display_width(text)
        if separator:
            current.append(Cell(separator, "muted"))
            used += sep_width
        current.append(Cell(text, cell.style))
        used += width
    if current:
        lines.append(current)
    return lines


def _layout_table(
    row: Row,
    width: int,
    natural: dict[str, int],
) -> list[list[Cell]]:
    """Lay a table row out, dropping the columns that do not fit."""
    columns = row.columns
    cells = list(row.cells)
    indent = " " * row.indent
    room = max(0, width - row.indent)
    desired = {
        column.key: natural.get(column.key, column.min_width)
        for column in columns
    }
    kept = _fit_columns(columns, room, desired)
    _used, spare = _row_room(columns, kept, room)
    widths: dict[int, int] = {}
    for index in kept:
        col = columns[index]
        if col.width is not None:
            widths[index] = col.width
            continue
        # The flexible column is as wide as its content, never wider than
        # the room the row has left.
        wanted = desired.get(col.key, col.min_width)
        widths[index] = max(0, min(wanted, spare))
    out: list[Cell] = []
    for position, index in enumerate(kept):
        col = columns[index]
        cell = cells[index] if index < len(cells) else Cell("")
        cell_width = widths[index]
        if col.width is None:
            # The flexible column is padded as well: a title that was cut on
            # a word boundary must not push the next column out of line.
            out.append(Cell(pad(clip(cell.text, cell_width), cell_width), cell.style))
        else:
            out.append(Cell(pad(clip(cell.text, cell_width), col.width), cell.style))
        if position < len(kept) - 1:
            out.append(Cell(col.separator(), "fg"))
    line = [Cell(indent, "fg"), *out]
    # A row that still does not fit is cut, never wrapped into a second line.
    used = row.indent + sum(display_width(cell.text) for cell in out)
    if used > width:
        line = _cut_cells(line, width)
    return [line]


def natural_widths(rows: Sequence[Row]) -> dict[tuple[Col, ...], dict[str, int]]:
    """The content width of every table's flexible column.

    A list column is as wide as its widest cell, the way the shipped TUI
    sizes its home table, so the columns after it start in the same place on
    every row. The map is keyed by the table's columns, because two tables
    may carry a column of the same name and measure it differently.
    """
    widths: dict[tuple[Col, ...], dict[str, int]] = {}
    for row in rows:
        if row.kind != "table":
            continue
        table = widths.setdefault(row.columns, {})
        for index, column in enumerate(row.columns):
            if column.width is not None:
                continue
            wanted = column.min_width if column.truncatable else 0
            for other in rows:
                if other.kind != "table" or other.columns != row.columns:
                    continue
                if index < len(other.cells):
                    wanted = max(wanted, display_width(other.cells[index].text))
            table[column.key] = wanted
    return widths


def _cut_cells(cells: Sequence[Cell], width: int) -> list[Cell]:
    """Cut a laid-out row at ``width`` columns."""
    out: list[Cell] = []
    used = 0
    for cell in cells:
        room = width - used
        if room <= 0:
            break
        if display_width(cell.text) <= room:
            out.append(cell)
            used += display_width(cell.text)
            continue
        out.append(Cell(take_columns(cell.text, room), cell.style))
        used = width
    return out


def wrap_text(text: str, width: int) -> list[str]:
    """Wrap prose on word boundaries, the way the transcript wraps a block.

    No character is dropped: a word longer than the line is cut across as
    many lines as it needs, and every piece of it is kept.
    """
    room = max(1, width)
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = word if not current else f"{current} {word}"
        if display_width(candidate) <= room:
            current = candidate
            continue
        # A word that has to be cut anyway, and a CJK run (which has no
        # spaces to break on), start on the line they are on: the line is
        # filled first, so a short head never sits alone above a long word.
        breakable = display_width(word) > room or (bool(word) and char_width(word[0]) == 2)
        if current and breakable:
            left = room - display_width(current) - 1
            head = take_columns(word, left) if left > 0 else ""
            rest = word[len(head) :]
            if head and rest and rest[0] in CJK_BREAKS:
                # A line never opens with a closing mark.
                head, rest = head[:-1], head[-1] + rest
            if head:
                lines.append(f"{current} {head}")
                current = ""
                word = rest
        if current:
            lines.append(current)
            current = ""
        # The word is longer than the line. Cut it, and keep every piece: a
        # path that does not fit is shown in full, across as many lines as it
        # needs.
        while display_width(word) > room:
            head = take_columns(word, room) or word[0]
            rest = word[len(head) :]
            if rest and rest[0] in CJK_BREAKS and len(head) > 1:
                # A line never opens with a closing mark: the character
                # above it goes down with it.
                lines.append(head[:-1])
                word = head[-1] + rest
                continue
            lines.append(head)
            word = rest
        current = word
    if current:
        lines.append(current)
    return lines or [""]


# --------------------------------------------------------------------------
# Chrome
# --------------------------------------------------------------------------


def identity_row(
    width: int,
    stream: tuple[str, str],
    session: str = "  deepseek-flash · session bad2c655",
) -> list[Cell]:
    """The identity bar: the one pinned row above the transcript.

    The bar is chrome, not scrollback, so it stays on screen while the
    transcript moves. Whole segments drop by rank when the terminal is
    narrow (the lowest rank first); a segment is never sliced.
    """
    dot, label = stream
    segments = (
        (" Conductor ", "baton bold", 100),
        ("Super-agent", "fg bold", 80),
        (session, "muted", 40),
        (f"  {dot} {label}", "ok" if dot == "●" else "muted", 60),
    )
    kept = list(segments)
    while kept and sum(display_width(part) for part, _style, _rank in kept) > width:
        victim = min(kept, key=lambda part: part[2])
        kept.remove(victim)
    return [Cell(part, style) for part, style, _rank in kept]


def status_row(screen: Screen, width: int) -> list[Cell]:
    """The status bar. Counts on the left, hints on the right.

    The permission gate takes the bar over: the label gives way, the three
    keys never do.
    """
    if screen.permission:
        legend = " · ".join(screen.permission_keys or ("a allow", "s session", "d deny"))
        label = screen.permission
        room = max(0, width - display_width(legend) - 3)
        return [
            Cell(" " + clip(label, room) + "  ", "hold"),
            Cell(legend, "hold"),
        ]
    left = " · ".join(screen.counts)
    hints = list(screen.hints)
    right = ""
    for index in range(len(hints), 0, -1):
        trial = " · ".join(hints[:index])
        if display_width(left) + display_width(trial) + 1 <= width:
            right = trial
            break
    if not right:
        return [Cell(clip(left, width), "muted")]
    pad_to = width - display_width(left) - display_width(right)
    return [
        Cell(left, "muted"),
        Cell(" " * max(1, pad_to), "muted"),
        Cell(right, "muted"),
    ]


def region_rows(
    screen: Screen,
    width: int,
    natural: dict[tuple[Col, ...], dict[str, int]],
) -> list[list[Cell]]:
    """The command region: the palette fallback, or the slash menu.

    The region is docked: it sits between the transcript and the composer and
    it takes its rows from the transcript, never from the composer. It is the
    fallback palette of the screen — the commands that act on it — and the
    slash menu is the same region with the query locked to ``/``.
    """
    out: list[list[Cell]] = []
    if screen.region_header is not None:
        for line in layout_row(screen.region_header, width, natural):
            out.append(line)
    for row in screen.region:
        for line in layout_row(row, width, natural):
            out.append(line)
    return out


def composer_rows(screen: Screen, width: int) -> list[list[Cell]]:
    """The composer, with its wall when the terminal is wide enough."""
    wall = width > COMPOSER_WALL_MIN_WIDTH
    text = screen.composer
    rows: list[list[Cell]] = []
    if wall:
        rows.append([Cell("╭" + "─" * max(0, width - 2) + "╮", "rule")])
    body = screen.composer_style
    prefix = "│" if wall else ""
    cells = [Cell(prefix, "rule"), Cell("❯ ", body)]
    if text:
        cells.append(Cell(text, "fg"))
    else:
        # The placeholder has a long and a short form. The room is what the
        # row has left after the wall and the caret; the short form is used
        # when the long one does not fit, and is cut only below that.
        long_form, short_form = screen.placeholder
        if not long_form:
            long_form = "Describe a task, / for commands, @ to attach a file"
            short_form = "Describe a task, / for commands"
        room = width - sum(display_width(cell.text) for cell in cells)
        room -= 1 if wall else 0
        hint = long_form if display_width(long_form) <= room else short_form
        cells.append(Cell(clip(hint, room), "muted"))
    if wall:
        used = sum(display_width(cell.text) for cell in cells)
        cells.append(Cell(" " * max(0, width - used - 1), "fg"))
        cells.append(Cell("│", "rule"))
    rows.append(_cut_cells(cells, width))
    if wall:
        rows.append([Cell("╰" + "─" * max(0, width - 2) + "╯", "rule")])
    return rows


def build_frame(
    screen: Screen,
    width: int,
    height: int,
    palette: Palette,
) -> list[list[Cell]]:
    """Lay a whole screen out at ``width`` x ``height``."""
    natural = natural_widths([*screen.body, *screen.region])
    chrome: list[list[Cell]] = []
    chrome.extend(region_rows(screen, width, natural))
    chrome.extend(composer_rows(screen, width))
    chrome.append(status_row(screen, width))

    body = body_rows(screen, width, natural)

    transcript_height = max(1, height - 1 - len(chrome))
    if len(body) > transcript_height:
        body = body[-transcript_height:]
    else:
        filler = [[Cell("", "fg")] for _ in range(transcript_height - len(body))]
        # Home sits at the top of the transcript; a command result sits at
        # its tail, because the transcript follows the last thing written.
        body = filler + body if screen.anchor == "bottom" else body + filler
    session = screen.session or "  deepseek-flash · session bad2c655"
    return [identity_row(width, screen.stream, session), *body, *chrome]


def body_rows(
    screen: Screen,
    width: int,
    natural: dict[tuple[Col, ...], dict[str, int]],
) -> list[list[Cell]]:
    """Lay the body of a screen out.

    A prose row that does not fit wraps on word boundaries, the way the
    transcript wraps a block. The room it wraps into is what the terminal
    has left after the indent and the rail, so a row that carries a rail
    never loses characters to it. A wrapped body is taller than the frame,
    so the transcript keeps its tail, the way the TUI follows a running
    Ticket.
    """
    rows: list[list[Cell]] = []
    for row in screen.body:
        if row.kind == "text":
            rows.extend(wrap_row(row, width))
            continue
        rows.extend(layout_row(row, width, natural))
    return rows


# --------------------------------------------------------------------------
# Locales
# --------------------------------------------------------------------------


# The languages every screen is drawn in. English is the primary render.
LANGS = ("en", "zh-CN")
PRIMARY_LANG = "en"

# Every string a localized UI shows. Command names, slash names, key names,
# ids, branches and tool output are not localized: they are what the
# operator types or what a program printed. The prose around them is.
STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "stream.idle": "idle",
        "stream.live": "live",
        "session": "  deepseek-flash · session bad2c655",
        "placeholder": "Describe a task, / for commands, @ to attach a file",
        "placeholder.short": "Describe a task, / for commands",
        "count.mode": "auto",
        "count.fleet": "● 3 online",
        "hint.attention": "◆ 2",
        "hint.newline": "shift+enter newline",
        "hint.mode": "alt+m mode",
        "hint.help": "? help",
        "hint.interrupt": "esc interrupt",
        "hint.accept": "tab accept",
        "hint.list": "ctrl+down list",
        "hint.close": "esc close",
        "hint.nav": "alt+n nav",
        "region.commands": "commands",
        "palette.label": "palette › ",
        "palette.matches": "{n} match · tab accepts · enter runs",
        "palette.matches.many": "{n} matches · tab accepts · enter runs",
        "palette.none": "no command matches",
        "palette.args": "{n} waiting for a person · tab or enter inserts the id",
        "palette.args.ambiguous": "{n} ids match · type more of the id",
        "palette.needs": "{command} needs a dispatch id",
        "palette.needs.text": "{command} needs text",
        "palette.needs.args": "{command} needs an argument",
        "greeting": "Describe a task and press Enter. It runs as a Ticket on Home.",
        "greeting.keys": "/ commands    @ attach    ? keys",
        "attention": "◆ 2 need you — /jobs --attention",
        "jobs.title": "jobs",
        "jobs.summary": "5 Tickets · 1 needs you · 1 running",
        "col.state": "state",
        "col.title": "title",
        "col.worker": "worker",
        "col.age": "age",
        "col.next": "next",
        "col.line": "line",
        "col.status": "status",
        "col.model": "model",
        "col.gate": "gate",
        "col.fix": "fix",
        "col.verdict": "verdict",
        "col.depends": "depends",
        "col.subject": "subject",
        "col.reviewer": "reviewer",
        "col.decision": "decision",
        "dispatch.title": "dispatch",
        "dispatch.head": "7f3a91c2  APPROVAL  62%  integrate/r2",
        "dispatch.subject": "Ship the terminal-native review queue screen",
        "dispatch.waiting": "waiting on a human for G6",
        "dispatch.roster": "roster",
        "dispatch.dag": "dag",
        "dispatch.unassigned": "unassigned",
        "dispatch.pool": "L4  worker pool exhausted",
        "review.title": "review queue",
        "review.gate": "G6 human gate  approve L3",
        "review.pinned": "the reviewed commit is pinned at 4b2f7803",
        "running.prompt": "List the registered adapters.",
        "running.conductor": "♩ kimi is conducting on branch feat/tui.",
        "running.registry": "╰ registry.py adapters",
        "running.subject": "Add the review queue screen",
        "running.executing": "Worker is executing the Ticket",
        "running.answer": (
            "There are 2 registered names and no file was changed. The "
            "registered adapter names stay alphabetical across the native "
            "registry today."
        ),
        "permission.mark": "◆ permission: merge feat/tui",
        "permission.action": "action: local-merge",
        "permission.into": "into integrate/r2",
        "permission.keys": "[a]llow once  [s]ession allow  [d]eny",
        "permission.label": "◆ merge feat/tui (local-merge) — into integrate/r2",
        "permission.allow": "a allow",
        "permission.session": "s session",
        "permission.deny": "d deny",
        "title.home": "home — Super-agent TUI",
        "title.jobs": "/jobs — Super-agent TUI",
        "title.dispatch": "/dispatch — Super-agent TUI",
        "title.review": "/review — Super-agent TUI",
        "title.running": "running ticket — Super-agent TUI",
        "title.completion": "palette completion — Super-agent TUI",
        "title.completion-args": "argument completion — Super-agent TUI",
        "title.permission": "permission prompt — Super-agent TUI",
    },
    "zh-CN": {
        "stream.idle": "空闲",
        "stream.live": "实时",
        "session": "  deepseek-flash · 会话 bad2c655",
        "placeholder": "描述一个任务，/ 打开命令，@ 附加文件",
        "placeholder.short": "描述一个任务，/ 打开命令",
        "count.mode": "自动",
        "count.fleet": "● 3 在线",
        "hint.attention": "◆ 2",
        "hint.newline": "shift+enter 换行",
        "hint.mode": "alt+m 模式",
        "hint.help": "? 帮助",
        "hint.interrupt": "esc 中断",
        "hint.accept": "tab 接受",
        "hint.list": "ctrl+down 列表",
        "hint.close": "esc 关闭",
        "hint.nav": "alt+n 导航",
        "region.commands": "命令",
        "palette.label": "命令面板 › ",
        "palette.matches": "{n} 个匹配 · tab 接受 · enter 运行",
        "palette.matches.many": "{n} 个匹配 · tab 接受 · enter 运行",
        "palette.none": "没有匹配的命令",
        "palette.args": "{n} 个等待人工 · tab 或 enter 插入 id",
        "palette.args.ambiguous": "{n} 个 id 匹配 · 请继续输入 id",
        "palette.needs": "{command} 需要一个 dispatch id",
        "palette.needs.text": "{command} 需要文字",
        "palette.needs.args": "{command} 需要参数",
        "greeting": "描述一个任务后按 Enter，它会作为 Ticket 在 Home 上运行。",
        "greeting.keys": "/ 命令    @ 附加    ? 按键",
        "attention": "◆ 2 个需要你 — /jobs --attention",
        "jobs.title": "任务列表",
        "jobs.summary": "5 个 Ticket · 1 个需要你 · 1 个运行中",
        "col.state": "状态",
        "col.title": "标题",
        "col.worker": "执行者",
        "col.age": "用时",
        "col.next": "下一步",
        "col.line": "线",
        "col.status": "状态",
        "col.model": "模型",
        "col.gate": "闸门",
        "col.fix": "修复",
        "col.verdict": "结论",
        "col.depends": "依赖",
        "col.subject": "主题",
        "col.reviewer": "评审者",
        "col.decision": "决定",
        "dispatch.title": "派发",
        "dispatch.head": "7f3a91c2  待审批  62%  integrate/r2",
        "dispatch.subject": "把终端原生的评审队列屏幕上线",
        "dispatch.waiting": "G6 正在等待人工决定",
        "dispatch.roster": "名册",
        "dispatch.dag": "依赖图",
        "dispatch.unassigned": "未分配",
        "dispatch.pool": "L4  执行者池已耗尽",
        "review.title": "评审队列",
        "review.gate": "G6 人工闸门  approve L3",
        "review.pinned": "评审过的提交固定在 4b2f7803",
        "running.prompt": "列出已注册的适配器。",
        "running.conductor": "♩ kimi 正在 feat/tui 分支上指挥。",
        "running.registry": "╰ registry.py 适配器",
        "running.subject": "添加评审队列屏幕",
        "running.executing": "Worker 正在执行这个 Ticket",
        "running.answer": (
            "共 2 个注册名，未改任何文件。原生注册表里的适配器名称至今仍按"
            "字母顺序排列，新加的适配器也遵守这个顺序。"
        ),
        "permission.mark": "◆ 权限：merge feat/tui",
        "permission.action": "动作：local-merge",
        "permission.into": "合入 integrate/r2",
        "permission.keys": "[a]允许一次  [s]本会话允许  [d]拒绝",
        "permission.label": "◆ merge feat/tui（local-merge）— 合入 integrate/r2",
        "permission.allow": "a 允许",
        "permission.session": "s 会话",
        "permission.deny": "d 拒绝",
        "title.home": "首页 — Super-agent TUI",
        "title.jobs": "/jobs — Super-agent TUI",
        "title.dispatch": "/dispatch — Super-agent TUI",
        "title.review": "/review — Super-agent TUI",
        "title.running": "运行中的 Ticket — Super-agent TUI",
        "title.completion": "命令面板补全 — Super-agent TUI",
        "title.completion-args": "参数补全 — Super-agent TUI",
        "title.permission": "权限提示 — Super-agent TUI",
    },
}

# A review verdict and decision are words too, and a localized UI writes
# them in its language. The glyph is the same in every language.
VERDICT_WORDS = {
    "en": {"MERGE": "MERGE", "BLOCK": "BLOCK", "—": "—"},
    "zh-CN": {"MERGE": "合并", "BLOCK": "阻断", "—": "—"},
}
DECISION_WORDS = {
    "en": {"approve": "approve", "reject": "reject", "pending": "pending"},
    "zh-CN": {"approve": "批准", "reject": "驳回", "pending": "待定"},
}
GATE_WORDS = {
    "en": {"reviewed": "reviewed", "build": "build", "—": "—"},
    "zh-CN": {"reviewed": "已评审", "build": "构建中", "—": "—"},
}


def tr(lang: str, key: str, **values: object) -> str:
    """One string of a language, with its ``{placeholders}`` filled."""
    text = STRINGS[lang][key]
    return text.format(**values) if values else text


def state_word(palette: Palette, lang: str, state: str) -> str:
    """The word of a state, in a language. English comes from the tokens."""
    if lang == "zh-CN":
        return STATUS_LABELS_ZH.get(state, state)
    return palette.labels.get(state, state)


def state_glyph(palette: Palette, state: str) -> str:
    """The glyph of a state, from the tokens."""
    return palette.glyphs.get(state, STATUS_GLYPHS.get(state, "·"))


def status_hints(lang: str, *keys: str) -> tuple[str, ...]:
    """The status-bar hints, in a language."""
    return tuple(tr(lang, f"hint.{key}") for key in keys)


# --------------------------------------------------------------------------
# Screens
# --------------------------------------------------------------------------


# The Ticket list drops its convenience columns before its state label: the
# glyph and the word say what the state is, and the word has to be whole.
JOBS_COLUMNS = (
    Col("mark", width=1, priority=100, pinned=True),
    Col("index", width=3, priority=30),
    Col("id", width=8, priority=90),
    Col("glyph", width=1, priority=100, pinned=True),
    Col("state", width=9, priority=95, pinned=True),
    Col("title", width=None, min_width=16, priority=80),
    Col("worker", width=8, priority=40),
    Col("age", width=6, priority=35),
    Col("next", width=13, priority=75),
)

# The Home roster is the Ticket list without the index and the id: the
# operator sees what needs them, and the id is one `/jobs` away.
HOME_COLUMNS = (
    Col("glyph", width=1, priority=100, pinned=True),
    Col("state", width=9, priority=95, pinned=True),
    Col("title", width=None, min_width=16, priority=80),
    Col("worker", width=8, priority=45),
    Col("age", width=6, priority=40),
    Col("when", width=5, priority=30),
    Col("next", width=13, priority=75),
)

# The dispatch roster carries the state of every line of the dispatch, in
# the same glyph and word the Ticket list uses.
ROSTER_COLUMNS = (
    Col("line", width=4, priority=100, pinned=True),
    Col("glyph", width=1, priority=100, pinned=True),
    Col("status", width=9, priority=95, pinned=True),
    Col("model", width=8, priority=70),
    Col("gate", width=8, priority=60),
    Col("fix", width=3, priority=50),
    Col("verdict", width=7, priority=80),
    Col("depends", width=7, priority=40),
)

REVIEW_COLUMNS = (
    Col("line", width=4, priority=100, pinned=True),
    Col("subject", width=None, min_width=16, priority=80),
    Col("reviewer", width=8, priority=40),
    Col("gate", width=8, priority=50),
    Col("fix", width=3, priority=60),
    Col("verdict", width=7, priority=85),
    Col("decision", width=9, priority=90),
)

# The card title of a running ticket reads as one line of `·` separated
# parts, so its columns carry a literal separator instead of a gap.
CARD_COLUMNS = (
    Col("mark", width=1, priority=100, gap=0, sep=" ", pinned=True),
    Col("subject", width=None, min_width=16, priority=80, gap=0, sep=" · "),
    Col("worker", width=8, priority=40, gap=0, sep=" · "),
    Col("branch", width=11, priority=35, gap=0, sep=" · "),
    Col("id", width=8, priority=90, gap=0, sep=" · "),
    Col("age", width=5, priority=30, gap=0, sep=""),
)

# The palette row: the marker, the canonical name, the slash name, and the
# hint. The slash name is the first thing to go when the region is narrow:
# the canonical name is what the command is, the hint is what it does, and
# the hint gives up its room before either of them does.
FALLBACK_COLUMNS = (
    Col("mark", width=2, priority=100, gap=0, pinned=True),
    Col("name", width=12, priority=95, pinned=True),
    Col("slash", width=10, priority=90),
    Col("hint", width=None, min_width=16, priority=50, truncatable=True),
)

# The argument row of the palette: the marker, the dispatch id, the state
# of the dispatch as glyph + word, and its subject. The id and the state are
# what the operator chooses by; the subject is a hint and is cut first.
ARG_COLUMNS = (
    Col("mark", width=2, priority=100, gap=0, pinned=True),
    Col("id", width=12, priority=95, pinned=True),
    Col("glyph", width=1, priority=100, gap=1, pinned=True),
    Col("state", width=9, priority=95, pinned=True),
    Col("title", width=None, min_width=8, priority=50, truncatable=True),
)

# At the narrow width a row stacks: the identity of the row on the first
# line, the rest of its columns on the meta line under it. Every column of
# the table is on one line or the other — a narrow terminal shows less, not
# less of what it shows. The header row keeps only the identity: the labels
# of the meta columns would only repeat the values under them.
JOBS_STACK = StackPlan(
    top=("mark", "glyph", "title"),
    bottom=("state", "next", "id", "worker", "age", "index"),
)
JOBS_HEADER_STACK = StackPlan(top=("mark", "glyph", "title"), bottom=())

HOME_STACK = StackPlan(
    top=("glyph", "title"),
    bottom=("state", "next", "worker", "age", "when"),
)

ROSTER_STACK = StackPlan(
    top=("line", "glyph", "status"),
    bottom=("model", "gate", "fix", "verdict", "depends"),
)
ROSTER_HEADER_STACK = StackPlan(top=("line", "glyph", "status"), bottom=())

REVIEW_STACK = StackPlan(
    top=("line", "subject"),
    bottom=("verdict", "decision", "fix", "gate", "reviewer"),
)
REVIEW_HEADER_STACK = StackPlan(top=("line", "subject"), bottom=())

CARD_STACK = StackPlan(
    top=("mark", "subject"),
    bottom=("worker", "branch", "id", "age"),
)

# (index, id, state, worker, age, next); the titles are per language.
JOBS = (
    ("1", "7f782add", "completed", "kimi", "12s", "/jobs 1"),
    ("2", "6bfa630f", "needs-you", "deepseek", "2h05m", "/reply 2"),
    ("3", "4b2f7803", "failed", "cursor", "7h57m", "/retry 3"),
    ("4", "1a91c0de", "running", "minimax", "4m12s", "/jobs watch 4"),
    ("5", "0c73ab19", "queued", "—", "—", "/jobs 5"),
)
JOB_TITLES = {
    "en": (
        "Read the adapter registry",
        "Needs a host with bash",
        "Sandbox denied",
        "Add the review queue screen",
        "Audit the token contrast",
    ),
    "zh-CN": (
        "读取适配器注册表",
        "需要一台带 bash 的主机",
        "沙箱拒绝了这次执行",
        "添加评审队列屏幕",
        "审查令牌的对比度",
    ),
}

# The titles the narrow form has to survive: a long English one and a long
# Chinese one, which is twice as wide. A title that does not fit wraps in
# the narrow form and never takes the marker of the row with it.
LONG_TITLES = (
    "Ship the terminal-native review queue screen",
    "把终端原生的评审队列屏幕一起带到这一行里",
)

# (state, worker, age, when, next); the titles are the first three jobs.
HOME = (
    ("completed", "kimi", "12s", "09:41", "/jobs 1"),
    ("needs-you", "deepseek", "2h05m", "09:12", "/reply 2"),
    ("failed", "cursor", "7h57m", "01:35", "/retry 3"),
)

ROSTER = (
    ("L1", "completed", "kimi", "reviewed", "1", "MERGE", "—"),
    ("L2", "running", "deepseek", "build", "0", "—", "L1"),
    ("L3", "blocked", "cursor", "—", "0", "BLOCK", "L2"),
    ("L4", "queued", "minimax", "—", "0", "—", "L2"),
)

# (line, reviewer, gate, fix, verdict, decision); subjects per language.
REVIEWS = (
    ("L3", "L1", "reviewed", "1", "MERGE", "approve"),
    ("L4", "L2", "build", "0", "BLOCK", "reject"),
    ("L5", "L1", "reviewed", "0", "—", "pending"),
)
REVIEW_SUBJECTS = {
    "en": (
        "Add the review queue screen",
        "Fix the narrow layout",
        "Audit the token contrast",
    ),
    "zh-CN": (
        "添加评审队列屏幕",
        "修复窄屏布局",
        "审查令牌的对比度",
    ),
}


@dataclass(frozen=True)
class Dispatch:
    """An active dispatch, as argument completion sees it.

    ``phase`` is the dispatch phase of the server (``awaiting_human`` is the
    phase ``approve`` and ``reject`` accept); ``state`` is the status id the
    row draws with its glyph and word.
    """

    id: str
    phase: str
    state: str
    titles: tuple[str, str]

    def title(self, lang: str) -> str:
        return self.titles[0] if lang == "en" else self.titles[1]


# The active dispatches. Two wait for a person and share the prefix
# ``7f``, so the ambiguous prefix has something to show; one runs.
DISPATCHES = (
    Dispatch(
        "7f3a91c2",
        "awaiting_human",
        "needs-you",
        ("Ship the terminal-native review queue screen", "把终端原生的评审队列屏幕上线"),
    ),
    Dispatch(
        "7f9e02b1",
        "awaiting_human",
        "needs-you",
        ("Fix the narrow layout", "修复窄屏布局"),
    ),
    Dispatch(
        "2c81e0d4",
        "running",
        "running",
        ("Audit the token contrast", "审查令牌的对比度"),
    ),
)


# --------------------------------------------------------------------------
# The palette: one matcher, and argument completion
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Command:
    """One command of the shared palette.

    The canonical name is what the palette shows; the slash name is what the
    composer takes. A command with no slash name — ``approve``,
    ``reject`` — is reachable by its canonical name only. ``needs`` names
    the argument a form cannot run without: ``dispatch-id`` for approve and
    reject, ``text`` for reply, empty when the bare command runs.
    """

    name: str
    slash: str = ""
    aliases: tuple[str, ...] = ()
    hint: str = ""
    hint_zh: str = ""
    needs: str = ""

    def hint_in(self, lang: str) -> str:
        return self.hint_zh if lang == "zh-CN" and self.hint_zh else self.hint


# The shared palette, in the idle order of the shared token set (28
# commands). The mock may not import the registry, so it carries this copy;
# `check_tokens` fails the build when it drifts from `palette.idleOrder` and
# `palette.commands` of the token set.
PALETTE_COMMANDS = (
    Command("workers", "/worker", ("worker",),
            "show which worker has the current Ticket",
            "显示当前 Ticket 由哪个 worker 执行"),
    Command("dispatch", "/dispatch", (),
            "dispatch roster; /dispatch <id> reads one",
            "派发名册；/dispatch <id> 查看一个"),
    Command("approve", "", (),
            "approve a dispatch that is waiting for a person",
            "批准一个等待人工的派发", needs="dispatch-id"),
    Command("reject", "", (),
            "reject a dispatch that is waiting for a person",
            "驳回一个等待人工的派发", needs="dispatch-id"),
    Command("tickets", "/jobs", ("/j", "jobs", "/attach"),
            "list Tickets; /jobs <id> shows one; /jobs watch <id> follows it",
            "列出 Ticket；/jobs <id> 查看一个；/jobs watch <id> 跟随它"),
    Command("workspace", "/workspace", (), "switch the Home workspace",
            "切换 Home 工作区"),
    Command("reply", "/reply", (), "answer the Ticket that needs you",
            "回复需要你的 Ticket", needs="text"),
    Command("retry", "/retry", (), "retry a failed or waiting Ticket",
            "重试失败或等待中的 Ticket"),
    Command("cancel", "/cancel", (), "stop a running Ticket (/cancel <id|index>)",
            "停止运行中的 Ticket（/cancel <id|index>）"),
    Command("resume", "/resume", (),
            "list saved sessions; /resume <id> reopens one",
            "列出已保存的会话；/resume <id> 重新打开一个"),
    Command("status", "/status", (),
            "reread Supervisor progress for the current Ticket",
            "重新读取当前 Ticket 的 Supervisor 进度"),
    Command("help", "/help", ("?",), "show the TUI key map and commands",
            "显示 TUI 按键和命令"),
    Command("quit", "/quit", ("/exit", "/q", "exit"), "quit the TUI", "退出 TUI"),
    Command("skills", "/skills", (), "local skills status", "本地技能状态"),
    Command("mcp", "/mcp", (), "local MCP server status", "本地 MCP 服务器状态"),
    Command("memory", "/memory", (), "browse remembered memory", "浏览已记住的记忆"),
    Command("agents", "/agents", (), "local fleet, native runtimes, daemons",
            "本地机群、原生运行时、守护进程"),
    Command("review", "/review", (),
            "queue a local cross-review (/review run <line> --by <line>)",
            "排队一次本地交叉评审（/review run <line> --by <line>）", needs="line"),
    Command("merge", "/merge", (),
            "merge a line locally after its tests (/merge <line> --into <branch>)",
            "测试通过后在本地合并一条线（/merge <line> --into <branch>）", needs="line"),
    Command("stop", "/stop", (), "stop a running Ticket (/stop <id|index>)",
            "停止运行中的 Ticket（/stop <id|index>）", needs="target"),
    Command("signals", "/signals", (), "Sources and job counts", "来源和任务计数"),
    Command("clear", "/clear", (), "clear the conversation", "清空对话"),
    Command("details", "/details", (),
            "toggle Worker details (default: Supervisor view)",
            "切换 Worker 详情（默认：Supervisor 视图）"),
    Command("model", "/model", (),
            "show the Router model and configured providers",
            "显示 Router 模型和已配置的提供方"),
    Command("compact", "/compact", (),
            "drop activity noise and duplicate rows from the transcript",
            "从记录中去掉活动噪声和重复行"),
    Command("expand", "/expand", (), "expand every tool block", "展开所有工具块"),
    Command("collapse", "/collapse", (), "collapse every tool block", "折叠所有工具块"),
    Command("new", "/new", (), "start a fresh session (persisted)",
            "开始一个新会话（会保存）"),
)


def command(name: str) -> Command:
    """A palette command by canonical name."""
    for item in PALETTE_COMMANDS:
        if item.name == name:
            return item
    raise KeyError(name)


# The row limit of the shared completion, `palette.completionLimit`.
COMPLETION_LIMIT = 60

# The window: how many rows the palette shows at a time.
WINDOW_ROWS = 8

# The caret of the palette query line.
CARET = "▏"


def fold(text: str) -> str:
    """The case fold of the matcher: Unicode casefold (`palette.fuzzy.fold`)."""
    return text.casefold()


def palette_needle(query: str) -> str:
    """The needle of a query: one leading ``/`` removed, then folded."""
    text = query.strip()
    if text.startswith("/"):
        text = text[1:]
    return fold(text)


def _haystacks(item: Command) -> tuple[str, ...]:
    """The strings a command answers to, folded.

    The canonical name, the slash name without its slash, and the aliases
    with one leading slash stripped — the haystacks of the shared token set.
    """
    out = [item.name]
    if item.slash:
        out.append(item.slash[1:])
    for alias in item.aliases:
        out.append(alias[1:] if alias.startswith("/") else alias)
    return tuple(fold(text) for text in out)


def _subsequence_indexes(haystack: str, needle: str) -> list[int]:
    """The indexes of the needle in the haystack, in order.

    The match is greedy and left to right: the leftmost later character
    wins, the way the shared token set aligns a match.
    """
    indexes: list[int] = []
    start = 0
    for char in needle:
        found = haystack.find(char, start)
        if found < 0:
            return []
        indexes.append(found)
        start = found + 1
    return indexes


def gap_cost(haystack: str, needle: str) -> int | None:
    """``endIndex - startIndex - (needleLength - 1)``, or None on a miss."""
    indexes = _subsequence_indexes(fold(haystack), fold(needle))
    if not indexes:
        return None
    return indexes[-1] - indexes[0] - (len(needle) - 1)


def palette_rank(
    item: Command,
    needle: str,
) -> tuple[int, int, int, int, str] | None:
    """The rank of a command against a folded needle, or None on a miss.

    D1: a case-insensitive **subsequence** of the canonical name, of the
    slash name without its slash, or of an alias. The rank is an exact
    haystack match first, then a prefix, then the lower gap cost, then the
    earlier start index, then the canonical name. The best haystack counts.
    """
    best: tuple[int, int, int, int, str] | None = None
    for haystack in _haystacks(item):
        indexes = _subsequence_indexes(haystack, needle)
        if not indexes:
            continue
        exact = 0 if haystack == needle else 1
        prefix = 0 if haystack.startswith(needle) else 1
        cost = indexes[-1] - indexes[0] - (len(needle) - 1)
        rank = (exact, prefix, cost, indexes[0], item.name)
        if best is None or rank < best:
            best = rank
    return best


def palette_items(query: str, limit: int = COMPLETION_LIMIT) -> list[Command]:
    """The commands a palette query matches.

    An empty needle is the idle list, in the idle order. Otherwise the
    commands are filtered by the fuzzy match and sorted by its rank. The
    same function answers Ctrl+K and the ``/`` at column 0.
    """
    needle = palette_needle(query)
    if not needle:
        return list(PALETTE_COMMANDS)[:limit]
    ranked = [(palette_rank(item, needle), item) for item in PALETTE_COMMANDS]
    matched = sorted(
        (entry for entry in ranked if entry[0] is not None),
        key=lambda entry: entry[0],
    )
    return [item for _rank, item in matched][:limit]


def resolve_command(token: str) -> Command | None:
    """The command a typed token names: an exact folded haystack match."""
    needle = palette_needle(token)
    for item in PALETTE_COMMANDS:
        if needle in _haystacks(item):
            return item
    return None


@dataclass(frozen=True)
class PaletteState:
    """What the palette shows for a query.

    ``mode`` is ``command`` while the query is one token and ``argument``
    once the command token is followed by a space. ``ambiguous`` is a
    non-empty argument prefix that more than one id starts with. ``error``
    is the hint of an incomplete form that has nothing to offer.
    """

    query: str
    mode: str
    commands: tuple[Command, ...] = ()
    command: Command | None = None
    arguments: tuple[Dispatch, ...] = ()
    prefix: str = ""
    ambiguous: bool = False
    complete: bool = False
    error: str = ""


def argument_candidates(
    item: Command,
    dispatches: Sequence[Dispatch] = DISPATCHES,
) -> tuple[Dispatch, ...]:
    """The ids a command's argument can be.

    ``dispatch`` takes any active dispatch; ``approve`` and ``reject`` take
    only a dispatch whose phase is ``awaiting_human``. ``reply`` takes free
    text and offers nothing.
    """
    if item.name == "dispatch":
        return tuple(dispatches)
    if item.name in ("approve", "reject"):
        return tuple(d for d in dispatches if d.phase == "awaiting_human")
    return ()


def palette_state(
    query: str,
    lang: str = "en",
    dispatches: Sequence[Dispatch] = DISPATCHES,
) -> PaletteState:
    """Resolve a query into the rows the palette shows (D1 and D2)."""
    body = query[1:] if query.startswith("/") else query
    if " " not in body:
        return PaletteState(query=query, mode="command", commands=tuple(palette_items(query)))
    token, _space, rest = body.partition(" ")
    item = resolve_command(token)
    if item is None:
        return PaletteState(query=query, mode="command", commands=())
    candidates = argument_candidates(item, dispatches)
    prefix = rest.strip()
    # D2: the argument match is a unique, case-sensitive PREFIX of an id or
    # of a name — never fuzzy.
    matched = tuple(
        d for d in candidates
        if d.id.startswith(prefix) or d.title(lang).startswith(prefix)
    )
    ambiguous = bool(prefix) and len(matched) > 1
    exact = any(d.id == prefix for d in candidates)
    if item.needs == "dispatch-id":
        complete = exact
    elif item.needs:
        complete = bool(prefix)
    else:
        complete = not prefix or exact
    error = ""
    if not complete and not matched:
        key = {
            "dispatch-id": "palette.needs",
            "text": "palette.needs.text",
        }.get(item.needs, "palette.needs.args")
        error = tr(lang, key, command=item.name)
    return PaletteState(
        query=query,
        mode="argument",
        command=item,
        arguments=matched,
        prefix=prefix,
        ambiguous=ambiguous,
        complete=complete,
        error=error,
    )


def _accept_command(query: str, item: Command) -> str:
    """The query after accepting a command token.

    The slash name is inserted when the palette was opened with ``/`` and
    the command has one; the canonical name otherwise. A trailing space
    follows when the command cannot run without an argument.
    """
    token = item.slash if query.startswith("/") and item.slash else item.name
    return f"{token} " if item.needs else token


def press_tab(state: PaletteState, highlighted: int = 0) -> str:
    """Tab: accept the highlighted token. Tab never leaves the palette."""
    if state.mode == "command":
        if not state.commands:
            return state.query
        return _accept_command(state.query, state.commands[highlighted])
    if state.ambiguous or not state.arguments:
        return state.query
    head = state.query[: len(state.query) - len(state.query.lstrip())]
    body = state.query[len(head):]
    token = body.split(" ", 1)[0]
    return f"{head}{token} {state.arguments[highlighted].id}"


def press_enter(state: PaletteState, highlighted: int = 0) -> tuple[str, str]:
    """Enter: run a complete form, or insert the highlighted argument.

    Returns ``("run", query)``, ``("insert", new query)``, ``("none",
    query)`` for an ambiguous prefix, or ``("error", hint)`` for an
    incomplete form with nothing to offer.
    """
    if state.mode == "command":
        if not state.commands:
            return ("none", state.query)
        item = state.commands[highlighted]
        if item.needs:
            return ("insert", _accept_command(state.query, item))
        return ("run", _accept_command(state.query, item))
    if state.complete:
        return ("run", state.query)
    if state.ambiguous:
        return ("none", state.query)
    if state.arguments:
        return ("insert", press_tab(state, highlighted))
    return ("error", state.error)


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------


def _verdict_ink(verdict: str) -> str:
    """The ink a review verdict is painted in."""
    if verdict == "MERGE":
        return "ok"
    if verdict == "BLOCK":
        return "error"
    return "muted"


def _decision_ink(decision: str) -> str:
    """The ink a review decision is painted in."""
    if decision == "approve":
        return "ok"
    if decision == "reject":
        return "error"
    return "hold"


# A review verdict and a review decision are states too: they carry a glyph
# next to their word, so the ink is never the only signal.
_VERDICT_GLYPHS = {"MERGE": "✔", "BLOCK": "✖"}
_DECISION_GLYPHS = {"approve": "✔", "reject": "✖", "pending": "◆"}


def _with_glyph(glyphs: dict[str, str], ident: str, word: str) -> str:
    """A word with the glyph of its state in front of it."""
    glyph = glyphs.get(ident, "")
    return f"{glyph} {word}" if glyph else word


def _state_ink(state: str) -> str:
    """The ink a status row is painted in (`status[].ink` of the tokens)."""
    return {
        "completed": "ok",
        "running": "tempo",
        "queued": "muted",
        "needs-you": "hold",
        "blocked": "hold",
        "failed": "error",
        "archived": "muted",
    }.get(state, "muted")


def fallback_rows(
    commands: Sequence[Command],
    lang: str,
    selected: int = 0,
) -> list[Row]:
    """Palette rows: the canonical name, the slash name, and the hint.

    Docked under a screen (the palette closed), it is that screen's
    fallback — the commands that act on it, the first being the one the
    screen would act on by default. Opened, it is the result list.
    """
    rows: list[Row] = []
    for index, item in enumerate(commands):
        chosen = index == selected
        rows.append(
            table_row(
                FALLBACK_COLUMNS,
                [
                    ("▸ " if chosen else "  ", "baton" if chosen else "fg"),
                    (item.name, "fg bold" if chosen else "fg"),
                    (item.slash or "—", "muted"),
                    (item.hint_in(lang), "muted"),
                ],
                indent=INSET,
            )
        )
    return rows


def argument_rows(
    state: PaletteState,
    palette: Palette,
    lang: str,
    selected: int = 0,
) -> list[Row]:
    """The argument list of the open palette: one row per offered id."""
    rows: list[Row] = []
    for index, dispatch in enumerate(state.arguments[:WINDOW_ROWS]):
        chosen = index == selected
        rows.append(
            table_row(
                ARG_COLUMNS,
                [
                    ("▸ " if chosen else "  ", "baton" if chosen else "fg"),
                    (dispatch.id, "fg bold" if chosen else "fg"),
                    (state_glyph(palette, dispatch.state), _state_ink(dispatch.state)),
                    (state_word(palette, lang, dispatch.state), "fg"),
                    (dispatch.title(lang), "muted"),
                ],
                indent=INSET,
            )
        )
    return rows


def query_row(query: str, lang: str) -> Row:
    """The query line of the open palette: the label, the query, the caret."""
    return text_row(
        query + CARET,
        "fg",
        indent=INSET,
        prefix=tr(lang, "palette.label"),
        prefix_style="baton bold",
    )


def palette_footer(state: PaletteState, lang: str) -> Row:
    """The last row of the open palette: the count, the keys, or the error."""
    if state.mode == "command":
        count = len(state.commands)
        if not count:
            return text_row(tr(lang, "palette.none"), "muted", indent=INSET)
        key = "palette.matches" if count == 1 else "palette.matches.many"
        return text_row(tr(lang, key, n=count), "muted", indent=INSET)
    if state.error:
        return text_row(state.error, "error", indent=INSET)
    if state.ambiguous:
        return text_row(
            tr(lang, "palette.args.ambiguous", n=len(state.arguments)),
            "hold",
            indent=INSET,
        )
    return text_row(tr(lang, "palette.args", n=len(state.arguments)), "muted", indent=INSET)


def _chrome(lang: str) -> dict:
    """The chrome every screen of a language shares."""
    return {
        "lang": lang,
        "placeholder": (tr(lang, "placeholder"), tr(lang, "placeholder.short")),
        "session": tr(lang, "session"),
        "counts": (tr(lang, "count.mode"), tr(lang, "count.fleet")),
        "stream": ("○", tr(lang, "stream.idle")),
        "forbidden": ("shift+tab",),
    }


def _greeting(lang: str) -> list[Row]:
    return [
        text_row("Conductor", "baton bold", indent=INSET),
        text_row(tr(lang, "greeting"), "muted", indent=INSET),
    ]


def home_screen(palette: Palette, lang: str = "en") -> Screen:
    """The landing view: what is running, and what needs the operator."""
    body: list[Row] = [
        *_greeting(lang),
        text_row(tr(lang, "greeting.keys"), "muted", indent=INSET),
        text_row(tr(lang, "attention"), "hold", indent=INSET),
    ]
    titles = JOB_TITLES[lang]
    for position, (state, worker, elapsed, when, next_command) in enumerate(HOME):
        body.append(
            table_row(
                HOME_COLUMNS,
                [
                    (state_glyph(palette, state), _state_ink(state)),
                    (state_word(palette, lang, state), "fg"),
                    (titles[position], "fg"),
                    (worker, "muted"),
                    (elapsed, "muted"),
                    (when, "muted"),
                    (next_command, "baton"),
                ],
                indent=INSET,
                stack=HOME_STACK,
            )
        )
    states = [state_word(palette, lang, row[0]) for row in HOME]
    return Screen(
        name="home",
        title=tr(lang, "title.home"),
        body=body,
        region_header=text_row(tr(lang, "region.commands"), "baton bold", indent=INSET),
        region=fallback_rows(
            [command("tickets"), command("reply"), command("retry"), command("help")],
            lang,
        ),
        anchor="top",
        hints=status_hints(lang, "attention", "newline", "mode", "help"),
        anchors=("Conductor", "❯", tr(lang, "count.mode"), "✔", "◆", "✖", *states),
        wide_anchors=("kimi", "deepseek", "12s", "/jobs 1", tr(lang, "hint.mode")),
        narrow_anchors=("kimi", "/jobs 1", "09:41"),
        **_chrome(lang),
    )


def jobs_screen(palette: Palette, lang: str = "en") -> Screen:
    """``/jobs``: one row per Ticket, newest first."""
    body: list[Row] = [
        text_row(tr(lang, "jobs.title"), "baton bold", indent=INSET),
        table_row(
            JOBS_COLUMNS,
            [
                ("", "fg"),
                ("#", "muted"),
                ("id", "muted"),
                ("", "fg"),
                (tr(lang, "col.state"), "muted"),
                (tr(lang, "col.title"), "muted"),
                (tr(lang, "col.worker"), "muted"),
                (tr(lang, "col.age"), "muted"),
                (tr(lang, "col.next"), "muted"),
            ],
            indent=INSET,
            stack=JOBS_HEADER_STACK,
            header=True,
        ),
    ]
    titles = JOB_TITLES[lang]
    for position, (index, ticket_id, state, worker, elapsed, next_command) in enumerate(JOBS):
        body.append(
            table_row(
                JOBS_COLUMNS,
                [
                    ("▸" if index == "1" else " ", "baton"),
                    (index, "muted"),
                    (ticket_id, "fg"),
                    (state_glyph(palette, state), _state_ink(state)),
                    (state_word(palette, lang, state), "fg"),
                    (titles[position], "fg"),
                    (worker, "muted"),
                    (elapsed, "muted"),
                    (next_command, "baton"),
                ],
                indent=INSET,
                stack=JOBS_STACK,
            )
        )
    body.append(text_row(tr(lang, "jobs.summary"), "muted", indent=INSET))
    # The state word is a word, and it stays a whole word at every width:
    # "Needs you" is never "Needs yo…", "需要你" is never "需要…".
    states = [state_word(palette, lang, row[2]) for row in JOBS]
    return Screen(
        name="jobs",
        title=tr(lang, "title.jobs"),
        body=body,
        region_header=text_row(tr(lang, "region.commands"), "baton bold", indent=INSET),
        region=fallback_rows(
            [command("tickets"), command("reply"), command("retry"), command("cancel")],
            lang,
        ),
        hints=status_hints(lang, "attention", "newline", "mode"),
        anchors=(
            "❯",
            tr(lang, "count.mode"),
            "7f782add",
            "6bfa630f",
            "4b2f7803",
            "✔",
            "◆",
            "✖",
            "●",
            "○",
            *states,
        ),
        wide_anchors=(tr(lang, "col.state"), tr(lang, "col.next")),
        narrow_anchors=("7f782add", "/jobs 1", "minimax"),
        **_chrome(lang),
    )


def dispatch_screen(palette: Palette, lang: str = "en") -> Screen:
    """``/dispatch``: one dispatch, its roster, and its DAG."""
    verdicts = VERDICT_WORDS[lang]
    gates = GATE_WORDS[lang]
    body: list[Row] = [
        text_row(tr(lang, "dispatch.title"), "baton bold", indent=INSET),
        text_row(tr(lang, "dispatch.head"), "fg", indent=INSET),
        text_row(tr(lang, "dispatch.subject"), "fg", indent=INSET),
        text_row(tr(lang, "dispatch.waiting"), "muted", indent=INSET),
        text_row(tr(lang, "dispatch.roster"), "baton", indent=INSET),
        table_row(
            ROSTER_COLUMNS,
            [
                (tr(lang, "col.line"), "muted"),
                ("", "fg"),
                (tr(lang, "col.status"), "muted"),
                (tr(lang, "col.model"), "muted"),
                (tr(lang, "col.gate"), "muted"),
                (tr(lang, "col.fix"), "muted"),
                (tr(lang, "col.verdict"), "muted"),
                (tr(lang, "col.depends"), "muted"),
            ],
            indent=INSET,
            stack=ROSTER_HEADER_STACK,
            header=True,
        ),
    ]
    for line, status, model, gate, fix, verdict, depends in ROSTER:
        body.append(
            table_row(
                ROSTER_COLUMNS,
                [
                    (line, "fg"),
                    (state_glyph(palette, status), _state_ink(status)),
                    (state_word(palette, lang, status), "fg"),
                    (model, "fg"),
                    (gates[gate], "muted"),
                    (fix, "muted"),
                    (
                        _with_glyph(_VERDICT_GLYPHS, verdict, verdicts[verdict]),
                        _verdict_ink(verdict),
                    ),
                    (depends, "muted"),
                ],
                indent=INSET,
                stack=ROSTER_STACK,
            )
        )
    body.extend(
        [
            text_row(tr(lang, "dispatch.dag"), "baton", indent=INSET),
            text_row("L0: L1", "muted", indent=INSET),
            text_row("L1: L2, L4", "muted", indent=INSET),
            text_row("L2: L3", "muted", indent=INSET),
            text_row(tr(lang, "dispatch.unassigned"), "baton", indent=INSET),
            text_row(tr(lang, "dispatch.pool"), "hold", indent=INSET),
        ]
    )
    return Screen(
        name="dispatch",
        title=tr(lang, "title.dispatch"),
        body=body,
        region_header=text_row(tr(lang, "region.commands"), "baton bold", indent=INSET),
        region=fallback_rows(
            [command("dispatch"), command("approve"), command("reject"), command("review")],
            lang,
        ),
        hints=status_hints(lang, "attention", "newline", "mode"),
        anchors=(
            "❯",
            tr(lang, "count.mode"),
            tr(lang, "dispatch.roster"),
            tr(lang, "dispatch.dag"),
            "L1",
            "L4",
            verdicts["MERGE"],
            verdicts["BLOCK"],
            "approve",
            "▣",
            state_word(palette, lang, "blocked"),
        ),
        wide_anchors=("L0: L1", "L1: L2, L4"),
        narrow_anchors=("L0: L1",),
        **_chrome(lang),
    )


def review_screen(palette: Palette, lang: str = "en") -> Screen:
    """``/review``: the cross-review queue and the decision in front of it."""
    verdicts = VERDICT_WORDS[lang]
    decisions = DECISION_WORDS[lang]
    gates = GATE_WORDS[lang]
    body: list[Row] = [
        text_row(tr(lang, "review.title"), "baton bold", indent=INSET),
        table_row(
            REVIEW_COLUMNS,
            [
                (tr(lang, "col.line"), "muted"),
                (tr(lang, "col.subject"), "muted"),
                (tr(lang, "col.reviewer"), "muted"),
                (tr(lang, "col.gate"), "muted"),
                (tr(lang, "col.fix"), "muted"),
                (tr(lang, "col.verdict"), "muted"),
                (tr(lang, "col.decision"), "muted"),
            ],
            indent=INSET,
            stack=REVIEW_HEADER_STACK,
            header=True,
        ),
    ]
    subjects = REVIEW_SUBJECTS[lang]
    for position, (line, reviewer, gate, fix, verdict, decision) in enumerate(REVIEWS):
        body.append(
            table_row(
                REVIEW_COLUMNS,
                [
                    (line, "fg"),
                    (subjects[position], "fg"),
                    (reviewer, "muted"),
                    (gates[gate], "muted"),
                    (fix, "muted"),
                    (
                        _with_glyph(_VERDICT_GLYPHS, verdict, verdicts[verdict]),
                        _verdict_ink(verdict),
                    ),
                    (
                        _with_glyph(_DECISION_GLYPHS, decision, decisions[decision]),
                        _decision_ink(decision),
                    ),
                ],
                indent=INSET,
                stack=REVIEW_STACK,
            )
        )
    body.extend(
        [
            text_row("", "fg", indent=INSET),
            text_row(tr(lang, "review.gate"), "hold", indent=INSET),
            text_row(tr(lang, "review.pinned"), "muted", indent=INSET),
            text_row("conductor dispatch approve 7f3a91c2", "baton", indent=INSET),
        ]
    )
    return Screen(
        name="review",
        title=tr(lang, "title.review"),
        body=body,
        region_header=text_row(tr(lang, "region.commands"), "baton bold", indent=INSET),
        region=fallback_rows(
            [command("review"), command("approve"), command("reject"), command("merge")],
            lang,
        ),
        hints=status_hints(lang, "attention", "newline", "mode"),
        anchors=(
            "❯",
            tr(lang, "count.mode"),
            tr(lang, "review.title"),
            "L3",
            "L5",
            decisions["approve"],
            decisions["reject"],
            decisions["pending"],
        ),
        wide_anchors=(gates["reviewed"], tr(lang, "col.fix")),
        narrow_anchors=("L3", decisions["approve"]),
        **_chrome(lang),
    )


def running_screen(palette: Palette, lang: str = "en") -> Screen:
    """A Ticket in flight: the prompt, the tool steps, the progress card."""

    def railed(text: str, style: str) -> Row:
        return text_row(text, style, indent=INSET, prefix="│ ", prefix_style="hold")

    body: list[Row] = [
        text_row(tr(lang, "running.prompt"), "baton", indent=INSET),
        text_row(tr(lang, "running.conductor"), "baton", indent=INSET),
        text_row("▸ read_file [done 1.2s]", "baton", indent=INSET),
        text_row(tr(lang, "running.registry"), "muted", indent=INSET),
        table_row(
            CARD_COLUMNS,
            [
                ("♬", "tempo bold"),
                (tr(lang, "running.subject"), "tempo bold"),
                ("kimi", "tempo"),
                ("feat/tui", "muted"),
                ("7f782add", "muted"),
                ("42s", "muted"),
            ],
            indent=INSET,
            stack=CARD_STACK,
        ),
        railed(tr(lang, "running.executing"), "fg"),
        railed('▸ grep "review"', "baton"),
        railed("╰ src/server/services/autonomy/review_merge.py", "muted"),
        railed("▸ pytest tests/unit", "baton"),
        railed("╰ 128 passed in 4.21s", "muted"),
        railed("▸ git diff --stat", "baton"),
        railed("╰ 3 files changed, 214 insertions", "muted"),
        railed(tr(lang, "running.answer"), "fg"),
    ]
    chrome = _chrome(lang)
    chrome["stream"] = ("●", tr(lang, "stream.live"))
    return Screen(
        name="running",
        title=tr(lang, "title.running"),
        body=body,
        region_header=text_row(tr(lang, "region.commands"), "baton bold", indent=INSET),
        region=fallback_rows(
            [command("status"), command("details"), command("stop"), command("compact")],
            lang,
        ),
        hints=status_hints(lang, "interrupt", "newline", "mode"),
        anchors=("❯", tr(lang, "count.mode"), "kimi", "♩", "▸", "read_file", "merge.py"),
        wide_anchors=("feat/tui", tr(lang, "stream.live")),
        narrow_anchors=("7f782add", "42s"),
        **chrome,
    )


# The two completion states the mock draws. `/dsp` is the `/` at column 0
# path: the palette opens prefilled with `/`, the operator types `dsp`, and
# the fuzzy matcher answers `dispatch` (gap cost 1). `approve ` is argument
# completion: only the ids whose phase is awaiting_human are offered.
COMPLETION_QUERY = "/dsp"
ARGUMENT_QUERY = "approve "


def completion_screen(palette: Palette, lang: str = "en") -> Screen:
    """The open palette with a fuzzy command query."""
    state = palette_state(COMPLETION_QUERY, lang)
    rows = fallback_rows(state.commands[:WINDOW_ROWS], lang)
    return Screen(
        name="completion",
        title=tr(lang, "title.completion"),
        body=_greeting(lang),
        region_header=query_row(COMPLETION_QUERY, lang),
        region=[*rows, palette_footer(state, lang)],
        hints=status_hints(lang, "accept", "list", "close", "nav"),
        anchors=(
            "❯",
            tr(lang, "count.mode"),
            "▸",
            # The list has to be the answer to the query on the query line.
            COMPLETION_QUERY + CARET,
            "dispatch",
            tr(lang, "palette.matches", n=1),
        ),
        wide_anchors=("/dispatch", tr(lang, "hint.accept")),
        forbidden=("shift+tab", "tickets", "reply"),
        **{k: v for k, v in _chrome(lang).items() if k != "forbidden"},
    )


def completion_args_screen(palette: Palette, lang: str = "en") -> Screen:
    """The open palette completing the argument of ``approve``."""
    state = palette_state(ARGUMENT_QUERY, lang)
    rows = argument_rows(state, palette, lang)
    offered = tuple(d.id for d in state.arguments)
    hidden = tuple(
        d.id for d in DISPATCHES if d.phase != "awaiting_human"
    )
    return Screen(
        name="completion-args",
        title=tr(lang, "title.completion-args"),
        body=_greeting(lang),
        region_header=query_row(ARGUMENT_QUERY, lang),
        region=[*rows, palette_footer(state, lang)],
        hints=status_hints(lang, "accept", "list", "close", "nav"),
        anchors=(
            "❯",
            tr(lang, "count.mode"),
            "▸",
            ARGUMENT_QUERY + CARET,
            "◆",
            state_word(palette, lang, "needs-you"),
            *offered,
        ),
        wide_anchors=(tr(lang, "hint.accept"),),
        # A dispatch that is not awaiting_human is never offered to approve.
        forbidden=("shift+tab", *hidden),
        **{k: v for k, v in _chrome(lang).items() if k != "forbidden"},
    )


def permission_screen(palette: Palette, lang: str = "en") -> Screen:
    """The gate in front of a local action: merge, review, shell."""
    keys = (
        tr(lang, "permission.allow"),
        tr(lang, "permission.session"),
        tr(lang, "permission.deny"),
    )
    return Screen(
        name="permission",
        title=tr(lang, "title.permission"),
        body=[
            *_greeting(lang),
            text_row("", "fg", indent=INSET),
            text_row(tr(lang, "permission.mark"), "hold", indent=INSET),
            # The detail of the permission hangs under its mark. The hang is
            # an indent and not a part of the text: a text that wraps keeps
            # its indent, a text that starts with spaces loses them.
            text_row(tr(lang, "permission.action"), "muted", indent=INSET + 2),
            text_row(tr(lang, "permission.into"), "muted", indent=INSET + 2),
            text_row(tr(lang, "permission.keys"), "muted", indent=INSET + 2),
        ],
        region_header=text_row(tr(lang, "region.commands"), "baton bold", indent=INSET),
        region=fallback_rows(
            [command("approve"), command("reject"), command("help")],
            lang,
        ),
        permission=tr(lang, "permission.label"),
        permission_keys=keys,
        hints=(),
        anchors=("❯", "◆", *keys, "local-merge"),
        narrow_anchors=keys,
        **_chrome(lang),
    )


SCREENS = (
    home_screen,
    jobs_screen,
    dispatch_screen,
    review_screen,
    running_screen,
    completion_screen,
    completion_args_screen,
    permission_screen,
)


def all_screens(palette: Palette) -> list[Screen]:
    """Every screen in every language, in a stable order."""
    return [factory(palette, lang) for factory in SCREENS for lang in LANGS]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def render_console(
    screen: Screen,
    width: int,
    height: int,
    palette: Palette,
) -> Console:
    """Render a screen into a recording console."""
    console = Console(
        file=io.StringIO(),
        record=True,
        width=width,
        height=height,
        force_terminal=True,
        color_system="truecolor",
        legacy_windows=False,
    )
    frame = build_frame(screen, width, height, palette)
    renderables = []
    for line in frame:
        text = Text(no_wrap=True, overflow="crop")
        for cell in line:
            text.append(cell.text, style=palette.style(cell.style) or None)
        renderables.append(text)
    console.print(Group(*renderables))
    return console


def plain_frame(screen: Screen, width: int, height: int, palette: Palette) -> list[str]:
    """The glyph grid of a screen, one string per row."""
    return [
        "".join(cell.text for cell in line)
        for line in build_frame(screen, width, height, palette)
    ]


def _grid(console: Console) -> str:
    """The glyph grid of a rendered console.

    The grid drops the padding a column leaves behind, the way the snapshot
    goldens do, so the artefact diffs on content only.
    """
    text = console.export_text(styles=False, clear=False)
    return "\n".join(line.rstrip() for line in text.splitlines()) + "\n"


def artefact_name(screen: Screen, width: int | None, suffix: str) -> str:
    """The file name of one artefact.

    ``tui-<screen>.<suffix>`` is the primary render (English, 100 x 32);
    ``tui-<screen>-<lang>-w<cols>.<suffix>`` is every language at every
    contract width.
    """
    if width is None:
        return f"tui-{screen.name}.{suffix}"
    return f"tui-{screen.name}-{screen.lang}-w{width}.{suffix}"


def render_artifacts(screen: Screen, palette: Palette) -> dict[str, str]:
    """The artefacts of one screen in one language, by file name."""
    out: dict[str, str] = {}
    targets: list[int | None] = []
    if screen.lang == PRIMARY_LANG:
        targets.append(None)
    targets.extend(CONTRACT_WIDTHS)
    for target in targets:
        width = BASE_WIDTH if target is None else target
        console = render_console(screen, width, FRAME_HEIGHT, palette)
        out[artefact_name(screen, target, "svg")] = console.export_svg(
            title=f"{screen.title}  ·  {screen.lang}  ·  {width}×{FRAME_HEIGHT}",
            theme=palette.terminal_theme(),
            code_format=SVG_FORMAT,
            clear=False,
        )
        out[artefact_name(screen, target, "txt")] = _grid(console)
    return out


def write_artifacts(files: dict[str, str], out_dir: Path) -> tuple[list[Path], list[Path]]:
    """Write the artefacts and delete the obsolete ones.

    A ``tui-*.svg`` or ``tui-*.txt`` in the directory that this run did not
    write is an artefact of an older layout of the mock, and it goes.
    """
    written: list[Path] = []
    for name in sorted(files):
        path = out_dir / name
        path.write_text(files[name], encoding="utf-8")
        written.append(path)
    keep = set(files)
    removed: list[Path] = []
    for pattern in ("tui-*.svg", "tui-*.txt"):
        for path in sorted(out_dir.glob(pattern)):
            if path.name not in keep:
                path.unlink()
                removed.append(path)
    return written, removed


# --------------------------------------------------------------------------
# Layout contract
# --------------------------------------------------------------------------


# The wrapping rules, checked on the cases that broke before: an ASCII word
# longer than the line, CJK text with a closing mark in it, and a wide
# character in a room of one column.
WRAP_CASES = (
    ("abcdefghij", 4),
    ("intro intro intro", 5),
    ("读取文件，然后列出名字", 4),
    ("src/server/services/autonomy/review_merge.py", 38),
    ("共共", 1),
)

# A row whose text competes with its own rail for columns. The indent and
# the rail take their columns first; the text wraps into what is left, and
# no character of it is lost to the rail. The first case is the one the
# review named: 19 Chinese characters (38 cells), indent 2, rail '│ ', in a
# 40-column terminal — 2 + 2 + 38 = 42 cells, so it must wrap.
PREFIX_WRAP_CASE = ("共" * 19, 2, "│ ", 40)
ROW_WRAP_CASES = (
    PREFIX_WRAP_CASE,
    ("共" * 18, 2, "│ ", 40),
    ("读取文件，然后列出名字", 2, "│ ", 40),
    ("共 2 个注册名，未改任何文件。", 2, "│ ", 24),
    ("src/server/services/autonomy/review_merge.py", 2, "│ ", 40),
    ("Merge the ticket into integrate/r2 once the tests pass.", 2, "│ ", 40),
    ("命令面板补全的查询行", 2, "命令面板 › ", 20),
)


def _row_text(lines: Sequence[Sequence[Cell]], row: Row) -> tuple[str, list[int]]:
    """The text a laid-out row carries, and the cell width of every line.

    The indent and the rail are taken off each line before the text is
    joined, so the comparison is on the text alone; the widths are of the
    whole line, rail and indent included.
    """
    pieces: list[str] = []
    widths: list[int] = []
    for line in lines:
        widths.append(sum(display_width(cell.text) for cell in line))
        body = list(line)
        if body and body[0].text == " " * row.indent:
            body = body[1:]
        if row.prefix and body and body[0].text == row.prefix:
            body = body[1:]
        pieces.append("".join(cell.text for cell in body))
    return "".join(pieces), widths


def _rich_widths(lines: Sequence[Sequence[Cell]], width: int) -> list[int]:
    """The widths rich gives the lines, rendered in a terminal of ``width``.

    The layout measures cells itself; this proves rich agrees, and that it
    did not wrap a line the layout believed fits.
    """
    console = Console(
        file=io.StringIO(), record=True, width=width, force_terminal=True,
        color_system=None, legacy_windows=False,
    )
    for line in lines:
        console.print(Text("".join(cell.text for cell in line), no_wrap=False))
    text = console.export_text(styles=False, clear=False)
    return [display_width(row) for row in text.splitlines()]


def check_wrapping() -> list[str]:
    """Check that wrapping keeps every character of the text.

    A word that does not fit is cut across as many lines as it needs, and
    every piece of it is kept. A line never opens with a closing mark.
    """
    problems: list[str] = []
    for text, width in WRAP_CASES:
        lines = wrap_text(text, width)
        kept = "".join(lines).replace(" ", "") == text.replace(" ", "")
        if not kept:
            problems.append(f"wrap lost text: {text!r} at {width} columns")
        for line in lines:
            # A wide character in a room of one column is the one line that
            # may be wider than the room: it is never dropped.
            if display_width(line) > max(width, 2 if len(line) == 1 else width):
                problems.append(f"wrap made a line too wide: {text!r} at {width}")
            if line.startswith(tuple(CJK_BREAKS)):
                problems.append(f"wrap opened a line with a closing mark: {text!r}")
    return problems


def check_prefix_wrap() -> list[str]:
    """Check that wrapping counts the indent and the rail.

    The exact case of the review: 19 Chinese characters, indent 2, rail
    ``│ ``, 40 columns. Every line, rail and indent included, is at most 40
    cells — measured by the layout and by rich — and every character is
    kept. Then the same for every case of ``ROW_WRAP_CASES``.
    """
    problems: list[str] = []
    for text, indent, prefix, width in ROW_WRAP_CASES:
        row = text_row(text, "fg", indent=indent, prefix=prefix)
        lines = wrap_row(row, width)
        kept, widths = _row_text(lines, row)
        label = f"{text!r} (indent {indent}, prefix {prefix!r}) at {width}"
        if kept.replace(" ", "") != text.replace(" ", ""):
            problems.append(f"prefix wrap lost text: {label}")
        if indent + display_width(prefix) + display_width(text) > width and len(lines) < 2:
            problems.append(f"prefix wrap cut instead of wrapping: {label}")
        for used in widths:
            if used > width:
                problems.append(f"prefix wrap made a line {used} cells wide: {label}")
        rich = _rich_widths(lines, width)
        if len(rich) != len(lines) or any(used > width for used in rich):
            problems.append(f"rich wrapped or widened a laid-out line: {label}")
        for line in text_row_lines(row, width):
            if line.startswith(tuple(CJK_BREAKS)):
                problems.append(f"prefix wrap opened a line with a closing mark: {label}")
    return problems


def check_lossless(screen: Screen, palette: Palette) -> list[str]:
    """Check that every prose row of a screen keeps all of its text.

    Every text row — in the transcript and in the command region, with or
    without a rail — at every width: the text is whole after wrapping, and
    no line, indent and rail included, is wider than the terminal.
    """
    problems: list[str] = []
    rows = [*screen.body]
    if screen.region_header is not None:
        rows.append(screen.region_header)
    rows.extend(screen.region)
    label = f"{screen.name}[{screen.lang}]"
    for width in WIDTHS:
        for row in rows:
            if row.kind != "text" or not row.text:
                continue
            lines = wrap_row(row, width)
            kept, widths = _row_text(lines, row)
            if kept.replace(" ", "") != row.text.replace(" ", ""):
                problems.append(f"{label}@{width}: row lost text: {row.text!r}")
            for used in widths:
                if used > width:
                    problems.append(
                        f"{label}@{width}: a line of {row.text!r} is {used} cells wide"
                    )
    return problems


def check_palette_match() -> list[str]:
    """Check the palette matcher (D1) and argument completion (D2).

    D1: one case-insensitive fuzzy subsequence matcher for Ctrl+K and for
    the ``/`` at column 0. D2: after the command token and a space, a
    case-sensitive unique prefix of an id, never fuzzy.
    """
    problems: list[str] = []

    def first(query: str) -> list[str]:
        return [item.name for item in palette_items(query)]

    expectations = (
        ("dsp", "dispatch"),
        ("DSP", "dispatch"),
        ("/dsp", "dispatch"),
        ("Disp", "dispatch"),
        ("jobs", "tickets"),
        ("/jobs", "tickets"),
        ("tkt", "tickets"),
        ("apv", "approve"),
    )
    for query, expected in expectations:
        names = first(query)
        if names[:1] != [expected]:
            problems.append(f"palette: {query!r} matched {names}, expected {expected!r} first")
    if gap_cost("dispatch", "dsp") != 1:
        problems.append("palette: the gap cost of 'dsp' in 'dispatch' is not 1")
    if first("zzz"):
        problems.append(f"palette: 'zzz' matched {first('zzz')}, expected no rows")
    if palette_footer(palette_state("zzz"), "en").text != "no command matches":
        problems.append("palette: the empty state is not 'no command matches'")
    if first("") != [item.name for item in PALETTE_COMMANDS]:
        problems.append("palette: the empty query is not the idle order")
    if len(palette_items("", limit=COMPLETION_LIMIT)) > COMPLETION_LIMIT:
        problems.append("palette: more rows than the completion limit")

    # Tab and Enter in command mode.
    if press_tab(palette_state("dsp")) != "dispatch":
        problems.append("palette: tab on 'dsp' did not insert 'dispatch'")
    if press_tab(palette_state("/dsp")) != "/dispatch":
        problems.append("palette: tab on '/dsp' did not insert '/dispatch'")
    if press_tab(palette_state("apv")) != "approve ":
        problems.append("palette: tab on 'apv' did not insert 'approve ' with its space")
    if press_enter(palette_state("apv")) != ("insert", "approve "):
        problems.append("palette: enter on 'apv' ran an incomplete form")

    # Argument completion.
    def ids(query: str, **kwargs) -> list[str]:
        return [d.id for d in palette_state(query, **kwargs).arguments]

    every = [d.id for d in DISPATCHES]
    waiting = [d.id for d in DISPATCHES if d.phase == "awaiting_human"]
    if ids("dispatch ") != every:
        problems.append(f"args: 'dispatch ' offered {ids('dispatch ')}, expected {every}")
    for verb in ("approve", "reject"):
        if ids(f"{verb} ") != waiting:
            problems.append(f"args: '{verb} ' offered {ids(verb + ' ')}, expected {waiting}")
    if ids("reply "):
        problems.append("args: 'reply ' offered ids; it takes free text")
    state = palette_state("dispatch ")
    if press_tab(state) != f"dispatch {every[0]}":
        problems.append("args: tab on 'dispatch ' did not insert the highlighted id")
    state = palette_state("approve 7f")
    if not state.ambiguous or len(state.arguments) != 2:
        problems.append("args: 'approve 7f' is not an ambiguous prefix of two ids")
    if press_tab(state) != "approve 7f" or press_enter(state)[0] != "none":
        problems.append("args: an ambiguous prefix inserted an id")
    state = palette_state("approve 7f3")
    if press_tab(state) != "approve 7f3a91c2":
        problems.append("args: a unique prefix was not completed")
    if palette_state("approve 7F3").arguments:
        problems.append("args: the id prefix matched case-insensitively")
    if press_enter(palette_state("approve ")) != ("insert", "approve 7f3a91c2"):
        problems.append("args: enter on 'approve ' did not insert the highlighted id")
    if press_enter(palette_state("approve 7f3a91c2"))[0] != "run":
        problems.append("args: enter on a complete approve did not run it")
    nobody = [d for d in DISPATCHES if d.phase != "awaiting_human"]
    for lang, expected in (
        ("en", "approve needs a dispatch id"),
        ("zh-CN", "approve 需要一个 dispatch id"),
    ):
        verdict = press_enter(palette_state("approve ", lang, nobody))
        if verdict != ("error", expected):
            problems.append(f"args[{lang}]: incomplete approve gave {verdict}")
    if press_enter(palette_state("approve 2c81e0d4"))[0] == "run":
        problems.append("args: approve ran on a dispatch that is not awaiting_human")
    return problems


def check_long_titles() -> list[str]:
    """Check that a long title keeps the markers of its row.

    A title that does not fit — an English one, or a Chinese one, which is
    twice as wide — must not push the marker of the focused row or the
    glyph of its state out of the row. The narrow form wraps the title, so
    the title is whole there; the wide form cuts the tail of the title,
    but the markers stay.
    """
    problems: list[str] = []
    for title in LONG_TITLES:
        row = table_row(
            JOBS_COLUMNS,
            [
                ("▸", "baton"),
                ("1", "muted"),
                ("7f782add", "fg"),
                ("●", "tempo"),
                ("Running", "fg"),
                (title, "fg"),
                ("kimi", "muted"),
                ("12s", "muted"),
                ("/jobs 1", "baton"),
            ],
            indent=INSET,
            stack=JOBS_STACK,
        )
        natural = natural_widths([row])
        for width in WIDTHS:
            lines = layout_row(row, width, natural)
            flat = "".join("".join(cell.text for cell in line) for line in lines)
            wide = display_width(title)
            if "▸" not in flat:
                problems.append(
                    f"a title of {wide} columns lost the marker at {width} columns"
                )
            if "●" not in flat:
                problems.append(
                    f"a title of {wide} columns lost the state glyph at "
                    f"{width} columns"
                )
            if width <= STACK_MAX:
                compact = flat.replace(" ", "")
                if title.replace(" ", "") not in compact:
                    problems.append(
                        f"a title of {wide} columns lost text at {width} columns"
                    )
            for line in lines:
                used = sum(display_width(cell.text) for cell in line)
                if used > width:
                    problems.append(
                        f"a title of {wide} columns made a row {used} columns "
                        f"wide at {width} columns"
                    )
    return problems


def check_stack_coverage(screen: Screen) -> list[str]:
    """Check that the narrow form of a row keeps every column it had.

    The narrow form stacks a row instead of dropping its columns: every
    column is on the identity line or on the meta line, and the meta line
    keeps the whole text of every cell.
    """
    problems: list[str] = []
    label = f"{screen.name}[{screen.lang}]"
    natural = natural_widths([*screen.body, *screen.region])
    for row in screen.body:
        if row.kind != "table" or row.stack is None or row.header:
            # A header row keeps the identity label only: the labels of the
            # meta columns would only repeat the values under them.
            continue
        plan = row.stack
        for column in row.columns:
            if column.key in plan.top or column.key in plan.bottom:
                continue
            problems.append(
                f"{label}: column {column.key!r} is on neither line of the narrow form"
            )
        lines = layout_row(row, STACK_MAX, natural)
        compact = "".join("".join(cell.text for cell in line) for line in lines)
        compact = compact.replace(" ", "")
        for key in plan.bottom:
            text = row.cell(key).text.replace(" ", "")
            if text and text not in compact:
                problems.append(f"{label}: the meta line lost {row.cell(key).text!r}")
    return problems


def check_contract(screen: Screen, palette: Palette) -> list[str]:
    """Check one screen against the layout contract.

    The contract is the one the TUI unit tests use: at 40, 80 and 120 columns
    (and the base width, 100) a screen keeps its rows, keeps its required
    glyphs, and never paints a row wider than the terminal — measured by
    the layout and by rich's own export.
    """
    problems: list[str] = []
    for width in WIDTHS:
        rows = plain_frame(screen, width, FRAME_HEIGHT, palette)
        label = f"{screen.name}[{screen.lang}]@{width}"
        if len(rows) != FRAME_HEIGHT:
            problems.append(f"{label}: {len(rows)} rows, expected {FRAME_HEIGHT}")
        for index, row in enumerate(rows):
            if display_width(row) > width:
                problems.append(
                    f"{label}: row {index} is {display_width(row)} columns wide"
                )
        grid = _grid(render_console(screen, width, FRAME_HEIGHT, palette)).splitlines()
        if grid != [row.rstrip() for row in rows]:
            problems.append(f"{label}: rich drew a different grid than the layout")
        flat = "\n".join(rows)
        # A path that wraps over two lines is still one path: the check has
        # to see it whole.
        compact = "".join(row.replace(" ", "") for row in rows)
        for anchor in screen.anchors:
            if anchor not in flat and anchor.replace(" ", "") not in compact:
                problems.append(f"{label}: missing {anchor!r}")
        if width >= 80:
            for anchor in screen.wide_anchors:
                if anchor not in flat:
                    problems.append(f"{label}: missing {anchor!r}")
        if width <= 40:
            for anchor in screen.narrow_anchors:
                if anchor not in flat and anchor.replace(" ", "") not in compact:
                    problems.append(f"{label}: missing {anchor!r}")
        for needle in screen.forbidden:
            if needle in flat:
                problems.append(f"{label}: unexpected {needle!r}")
        if "❯" not in flat:
            problems.append(f"{label}: the composer prompt is gone")
        if not rows[-1].strip():
            problems.append(f"{label}: the status bar is empty")
    return problems


def check_locales() -> list[str]:
    """Check that every language has every string the screens use."""
    problems: list[str] = []
    english = set(STRINGS["en"])
    for lang in LANGS:
        keys = set(STRINGS[lang])
        for missing in sorted(english - keys):
            problems.append(f"locale {lang}: missing {missing!r}")
        for extra in sorted(keys - english):
            problems.append(f"locale {lang}: unknown {extra!r}")
    for state in STATUS_GLYPHS:
        if state not in STATUS_LABELS_ZH:
            problems.append(f"locale zh-CN: no word for the state {state!r}")
    for item in PALETTE_COMMANDS:
        if not item.hint_zh:
            problems.append(f"locale zh-CN: no hint for the command {item.name!r}")
    return problems


def check_tokens(document: dict) -> tuple[list[str], list[str]]:
    """Check the mock against the shared token set (D1, D3, D4).

    Returns the problems and the notes. The names, the order and the hints
    of the 28 commands, the seven states with their glyph, ASCII twin, word
    and ink, the key names the TUI reads, and the case rule of the matcher
    (D1: case-insensitive, with a fold) have to agree.
    """
    problems: list[str] = []
    notes: list[str] = []
    rows = [row for row in document.get("status", []) if isinstance(row, dict)]
    ids = [str(row.get("id")) for row in rows]
    if ids != list(STATUS_GLYPHS):
        problems.append(f"tokens: status ids are {ids}, the mock draws {list(STATUS_GLYPHS)}")
    for row in rows:
        ident = str(row.get("id"))
        for key, table in (("glyph", STATUS_GLYPHS), ("ascii", STATUS_ASCII), ("label", STATUS_LABELS)):
            if ident in table and row.get(key) != table[ident]:
                problems.append(
                    f"tokens: status {ident} {key} is {row.get(key)!r}, the mock has {table[ident]!r}"
                )
        if ident in STATUS_GLYPHS and row.get("ink") != _state_ink(ident):
            problems.append(f"tokens: status {ident} ink is {row.get('ink')!r}")
    palette = document.get("palette", {})
    order = list(palette.get("idleOrder", []))
    mine = [item.name for item in PALETTE_COMMANDS]
    if order != mine:
        problems.append(f"tokens: palette.idleOrder is {order}, the mock has {mine}")
    if len(mine) != 28:
        problems.append(f"the mock carries {len(mine)} commands, expected 28")
    if palette.get("completionLimit") != COMPLETION_LIMIT:
        problems.append("tokens: palette.completionLimit is not 60")
    by_name = {
        str(entry.get("name")): entry
        for entry in palette.get("commands", [])
        if isinstance(entry, dict)
    }
    for item in PALETTE_COMMANDS:
        entry = by_name.get(item.name)
        if entry is None:
            problems.append(f"tokens: palette.commands has no {item.name!r}")
            continue
        if (entry.get("slash") or "") != item.slash:
            problems.append(f"tokens: {item.name} slash is {entry.get('slash')!r}")
        if tuple(entry.get("aliases") or ()) != item.aliases:
            problems.append(f"tokens: {item.name} aliases are {entry.get('aliases')!r}")
        if entry.get("hint") != item.hint:
            problems.append(f"tokens: {item.name} hint is {entry.get('hint')!r}")
        runs_bare = any(not form.get("steps") for form in entry.get("forms", []))
        if runs_bare == bool(item.needs):
            problems.append(f"tokens: {item.name} forms disagree with needs={item.needs!r}")
    keyboard = document.get("keyboard", {})
    opens = [entry.get("key") for entry in keyboard.get("openPalette", [])]
    if opens != ["ctrl+k", "/"]:
        problems.append(f"tokens: keyboard.openPalette is {opens}")
    for path, expected in (
        (("closePalette", "key"), "escape"),
        (("navigationMode", "toggle"), "alt+n"),
        (("focusResultList", "key"), "ctrl+down"),
        (("focusQuery", "key"), "ctrl+up"),
    ):
        value: object = keyboard
        for part in path:
            value = value.get(part) if isinstance(value, dict) else None
        if value != expected:
            problems.append(f"tokens: keyboard.{'.'.join(path)} is {value!r}, expected {expected!r}")
    if keyboard.get("focusOrder") != ["main-list-or-transcript", "composer"]:
        problems.append("tokens: keyboard.focusOrder changed")
    if keyboard.get("paletteTrap") != ["query", "result-list"]:
        problems.append("tokens: keyboard.paletteTrap changed")
    fuzzy = palette.get("fuzzy", {})
    if fuzzy.get("caseSensitive") is not False:
        problems.append(
            "tokens: palette.fuzzy.caseSensitive is "
            f"{fuzzy.get('caseSensitive')!r}; D1 makes the matcher case-insensitive"
        )
    if "fold" not in fuzzy:
        problems.append("tokens: palette.fuzzy.fold is missing (D1)")
    if fuzzy.get("algorithm") != "subsequence":
        problems.append("tokens: palette.fuzzy.algorithm is not subsequence")
    return problems, notes


def run_checks(palette: Palette, document: dict | None) -> tuple[list[str], list[str], int]:
    """Every check of the contract. Returns problems, notes and the count."""
    problems: list[str] = []
    notes: list[str] = []
    count = 0
    for check in (check_wrapping, check_prefix_wrap, check_palette_match,
                  check_long_titles, check_locales):
        problems.extend(check())
        count += 1
    if document is not None:
        found, said = check_tokens(document)
        problems.extend(found)
        notes.extend(said)
        count += 1
    for screen in all_screens(palette):
        problems.extend(check_stack_coverage(screen))
        problems.extend(check_contract(screen, palette))
        problems.extend(check_lossless(screen, palette))
        count += 3
    return problems, notes, count


def main(argv: Sequence[str] | None = None) -> int:
    """Check the layout contract, then render every screen."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tokens",
        type=Path,
        default=DEFAULT_TOKENS,
        help="the token file to read colours from",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=HERE,
        help="the directory the artefacts are written to",
    )
    parser.add_argument(
        "--theme",
        default="dark",
        help="the theme to render (dark is primary)",
    )
    args = parser.parse_args(argv)

    try:
        document = load_tokens(args.tokens)
    except FileNotFoundError:
        return _check_without_tokens(args)
    palette = build_palette(document, args.theme)

    problems, notes, count = run_checks(palette, document)
    for note in notes:
        print(f"note: {note}")
    if problems:
        return _report(problems, count)

    files: dict[str, str] = {}
    for screen in all_screens(palette):
        files.update(render_artifacts(screen, palette))
    args.out.mkdir(parents=True, exist_ok=True)
    written, removed = write_artifacts(files, args.out)
    for path in written:
        print(f"wrote {_shown(path)}")
    for path in removed:
        print(f"removed obsolete {_shown(path)}")
    screens = len(SCREENS)
    print(
        f"{screens} screens x {len(LANGS)} languages x {len(CONTRACT_WIDTHS)} widths, "
        f"plus {screens} primary renders: {len(written)} files"
    )
    return _report(problems, count)


def _shown(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _check_without_tokens(args: argparse.Namespace) -> int:
    """Check the layout on a branch that does not have the token set yet.

    The token set is the sibling line's deliverable. A branch without it can
    still prove the layout, because the layout does not depend on the
    colour. The coloured artefacts are left untouched: without the tokens
    there is nothing to colour them with.
    """
    print(f"token set not found: {args.tokens}")
    print(
        "the layout contract was checked without colour; "
        "the coloured artefacts were left untouched"
    )
    problems, _notes, count = run_checks(Palette.blank(), None)
    return _report(problems, count)


def _report(problems: list[str], count: int) -> int:
    """Print the contract result and turn it into an exit code."""
    if problems:
        print(f"layout contract failed ({len(problems)} problems):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("nothing was written", file=sys.stderr)
        return 1
    print(
        f"layout contract holds: {count} checks, {len(LANGS)} languages, "
        f"{', '.join(str(w) for w in WIDTHS)} columns"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
