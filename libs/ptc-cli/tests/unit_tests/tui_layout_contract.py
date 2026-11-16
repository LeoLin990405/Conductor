"""The layout contract every TUI screen test imports.

``docs/design/terminal-native/tui/SPEC.md`` §1.1 gates every screen at 40, 80 and
120 columns in English and zh-CN. This module is that gate for the renderers of the
terminal-native screens: each one is a pure function of its state, so a test can
call it at every width and language and ask :func:`check_layout` whether the rows
still hold the contract.

The five rules, and the argument that carries each of them:

* a row is never wider than the terminal, measured with ``rich.cells.cell_len`` so
  a CJK character counts as the two cells it takes;
* a row the screen must keep is still there — the composer prompt, the status bar
  (``keep``);
* a state still shows its glyph and its word (``states``);
* a prose row has not lost a character, wrapping allowed (``prose``);
* a narrow row stacks its columns instead of dropping one (``columns``).

Widths are in terminal cells, and a label is never truncated: the state word, a
command name, a key hint and an error hint are measured in cells too, so a column
sized for a label fits the widest label of both languages.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from rich.cells import cell_len

from ptc_cli.superagent.tui_theme import status_glyph, status_word

# §1.1 gates at 40, 80 and 120; the mock adds the base width of 100.
WIDTHS = (40, 80, 120)
LOCALES = ("en", "zh-CN")
# §3.3: at 40 columns and below a row stacks instead of shedding columns.
STACK_MAX = 40

Renderer = Callable[[int, str], "Iterable[str] | str"]


class LayoutError(AssertionError):
    """The frame broke the layout contract. The message lists every problem."""


def _rows(render: Renderer, width: int, locale: str) -> list[str]:
    """The rows of one frame. A single string is split on its newlines."""
    value = render(width, locale)
    rows = value.splitlines() if isinstance(value, str) else [str(row) for row in value]
    return [row.rstrip("\r\n") for row in rows]


def _overwide(rows: Sequence[str], width: int, label: str) -> list[str]:
    """The rows wider than the terminal, measured in cells so CJK counts two."""
    return [f"{label}: row {index} is {used} cells wide: {row!r}" for index, row in enumerate(rows) if (used := cell_len(row)) > width]


def _lost(needles: Sequence[str], haystack: str, label: str, what: str) -> list[str]:
    """The needles the frame does not contain, as one problem each."""
    return [f"{label}: {what}: {needle!r}" for needle in needles if needle not in haystack]


def _prose_payload(rows: Sequence[str]) -> str:
    """Join row text without the indent and continuation rail that frame it."""
    pieces: list[str] = []
    for row in rows:
        payload = row.lstrip()
        payload = payload[1:].removeprefix(" ") if payload.startswith("│") else row
        pieces.append(payload)
    return "".join("".join(pieces).split())


def check_layout(
    render: Renderer,
    *,
    widths: Sequence[int] = WIDTHS,
    locales: Sequence[str] = LOCALES,
    keep: Sequence[str] = (),
    states: Sequence[str] = (),
    prose: Sequence[str] = (),
    columns: Sequence[str] = (),
    stack_at: int = STACK_MAX,
    ascii_glyphs: bool = False,
) -> None:
    """Check one renderer against the layout contract, or raise :class:`LayoutError`.

    ``render(width, locale)`` returns the rows of the frame. ``keep`` lists the
    rows that must survive at every width — the composer prompt, the status bar.
    ``states`` lists Ticket state ids the frame shows; each must keep its glyph
    and its word together on one row. ``prose`` lists free text that must keep every character, which
    is what a wrapped row does and a cut row does not. ``columns`` lists the cells
    of a row that must still be on screen at ``stack_at`` and below, where the row
    stacks them instead of dropping them. ``ascii_glyphs`` checks the
    ``PTC_ASCII=1`` glyphs instead of the Unicode ones.
    """
    problems: list[str] = []
    for width in widths:
        for locale in locales:
            rows = _rows(render, width, locale)
            label = f"{locale}@{width}"
            if not rows:
                problems.append(f"{label}: the renderer returned no rows")
                continue
            problems.extend(_overwide(rows, width, label))
            flat = "\n".join(rows)
            compact = "".join(flat.split())
            prose_payload = _prose_payload(rows)
            problems.extend(_lost(keep, flat, label, "lost the row it must keep"))
            for status_id in states:
                environ = {"PTC_ASCII": "1"} if ascii_glyphs else {}
                glyph = status_glyph(status_id, environ)
                word = status_word(status_id, locale)
                if any(glyph in row and word in row for row in rows):
                    continue
                if glyph not in flat:
                    problems.append(f"{label}: state {status_id} lost its glyph {glyph!r}")
                if word not in flat:
                    problems.append(f"{label}: state {status_id} lost its word {word!r}")
                if glyph in flat and word in flat:
                    problems.append(f"{label}: state {status_id} glyph and word are not on the same row")
            for text in prose:
                squeezed = "".join(text.split())
                if squeezed and squeezed not in prose_payload:
                    problems.append(f"{label}: a prose row lost a character: {text!r}")
            if width <= stack_at:
                for text in columns:
                    squeezed = "".join(text.split())
                    if squeezed and squeezed not in compact:
                        problems.append(f"{label}: a narrow row dropped the column {text!r}")
    if problems:
        raise LayoutError("\n".join(problems))
