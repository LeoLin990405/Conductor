"""Markdown and word wrap for the Conductor TUI.

Rich draws the layout. Prompt-toolkit only receives already-wrapped rows.
``AUTONOMY_*`` trailer lines are removed before anything is drawn.
"""

from __future__ import annotations

import contextvars
import re
import unicodedata
from collections.abc import Iterable

from markdown_it import MarkdownIt
from rich import box
from rich.console import Console, Group
from rich.markdown import CodeBlock, Heading, Markdown, TableElement
from rich.style import Style as RichStyle
from rich.table import Table
from rich.text import Text
from rich.theme import Theme as RichTheme

from .text import strip_protocol_trailers as _strip_trailers
from .tui_theme import Theme, resolve_theme

_KINSOKU = frozenset("，。、；：？！）」』")  # noqa: RUF001
_MIN_FENCE = 3
_ABS_PATH = re.compile(r"\((/[^)\s]{8,})\)")
_FOLD_TABLES: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "conductor_fold_tables", default=True
)


def strip_protocol_trailers(text: object) -> str:
    """Remove ``AUTONOMY_*`` trailer lines. A mention without the colon stays."""
    return _strip_trailers(text)


class _LeftHeading(Heading):
    """Headings stay left-aligned. Rich centres h1 and that padding is unstyled."""

    def __rich_console__(self, console: Console, options):  # type: ignore[no-untyped-def]
        del console, options
        text = self.text.copy()
        text.justify = "left"
        yield text


class _PlainCode(CodeBlock):
    """A fenced block as one style, with a gutter. No Pygments colours."""

    def __rich_console__(self, console: Console, options):  # type: ignore[no-untyped-def]
        del console
        code = str(self.text).rstrip("\n")
        lines = code.split("\n") if code else [""]
        width = max(1, int(options.max_width))
        gutter = "│ "
        room = max(1, width - _display_width(gutter))
        body: list[str] = []
        for line in lines:
            wrapped = _wrap_code_line(line, room) or [""]
            for part in wrapped:
                body.append(f"{gutter}{part}" if width >= _display_width(gutter) else part)
        yield Text("\n".join(body), style="markdown.code_block")


class _FoldTable(TableElement):
    """A markdown table that folds cell text and keeps four body rows."""

    def __rich_console__(self, console: Console, options):  # type: ignore[no-untyped-def]
        table = Table(
            box=box.SIMPLE_HEAD,
            pad_edge=False,
            show_edge=False,
            collapse_padding=True,
            padding=(0, 1),
        )
        if self.header is not None and self.header.row is not None:
            for column in self.header.row.cells:
                heading = column.content.copy()
                heading.stylize("markdown.table.header")
                table.add_column(heading, overflow="fold", no_wrap=False)
        body_rows = list(self.body.rows) if self.body is not None else []
        shown = body_rows[:4] if _FOLD_TABLES.get() else body_rows
        for row in shown:
            table.add_row(*(element.content for element in row.cells))
        yield table
        hidden = len(body_rows) - len(shown)
        if hidden > 0:
            yield Text(f"… {hidden} more rows", style="markdown.item")


class _Markdown(Markdown):
    elements = dict(Markdown.elements)
    elements["table_open"] = _FoldTable
    elements["heading_open"] = _LeftHeading
    elements["fence"] = _PlainCode
    elements["code_block"] = _PlainCode


# Rich styles carry a class name in meta. The TUI paints that class from
# tokens, so a Pygments theme never reaches the terminal.
_MARKDOWN_CLASSES = {
    "markdown.h1": "class:tui-heading",
    "markdown.h2": "class:tui-heading",
    "markdown.h3": "class:tui-heading",
    "markdown.h4": "class:tui-heading",
    "markdown.h5": "class:tui-heading",
    "markdown.h6": "class:tui-heading",
    "markdown.strong": "class:tui-strong",
    "markdown.code": "class:tui-inline",
    "markdown.code_block": "class:tui-code",
    "markdown.table.header": "class:tui-strong",
    "markdown.link": "class:tui-inline",
    "markdown.item": "class:tui-meta",
    "markdown.item.bullet": "class:tui-bullet",
    "markdown.item.number": "class:tui-bullet",
    "markdown.paragraph": "class:tui-agent",
}
_RICH_THEME = RichTheme(
    {
        name: RichStyle(meta={"tui_class": class_name})
        for name, class_name in _MARKDOWN_CLASSES.items()
    }
)
_PROSE = "class:tui-agent"


def _console(width: int, *, color: bool) -> Console:
    return Console(
        width=max(1, width),
        force_terminal=True,
        color_system="truecolor" if color else None,
        legacy_windows=False,
        highlight=False,
        soft_wrap=False,
        theme=_RICH_THEME,
    )


def _shorten_paths(line: str) -> str:
    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        leaf = path.rsplit("/", 1)[-1]
        return f"(…/{leaf})"

    return _ABS_PATH.sub(replace, line)


def _scrub(line: str) -> str:
    """Trim a plain fallback line. Paths and code characters stay as written."""
    return line.rstrip()


def _fence_marker(stripped: str) -> str | None:
    if stripped.startswith("```"):
        marker = "```"
    elif stripped.startswith("~~~"):
        marker = "~" * (len(stripped) - len(stripped.lstrip("~")))
        if len(marker) < _MIN_FENCE:
            return None
    else:
        return None
    if stripped.startswith("```"):
        marker = "`" * (len(stripped) - len(stripped.lstrip("`")))
        if len(marker) < _MIN_FENCE:
            return None
        rest = stripped[len(marker) :]
        if "`" in rest:
            return None
    return marker


def _closes_fence(stripped: str, fence: str) -> bool:
    return stripped.startswith(fence) and stripped.strip(" \t" + fence[0]) == ""


def _span_class(style: object) -> str:
    meta = getattr(style, "meta", None)
    if isinstance(meta, dict):
        name = meta.get("tui_class")
        if isinstance(name, str) and name:
            return name
    return ""


def _coalesce(spans: list[tuple[str, str]]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for style, text in spans:
        if not text:
            continue
        if merged and merged[-1][0] == style:
            merged[-1] = (style, merged[-1][1] + text)
        else:
            merged.append((style, text))
    return merged


_CODE_SPAN_CLASSES = frozenset({"class:tui-code", "class:tui-inline"})


def _scrub_spans(spans: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep prose, code, and link text. Path shortening is not a body transform."""
    cleaned: list[tuple[str, str]] = []
    for style, text in spans:
        cleaned.append((style, text.replace("\n", "")))
    while cleaned and cleaned[0][1].strip() == "":
        cleaned.pop(0)
    while cleaned and cleaned[-1][1].strip() == "":
        cleaned.pop()
    if not cleaned:
        return []
    style, text = cleaned[-1]
    cleaned[-1] = (style, text.rstrip())
    return _coalesce(cleaned)


def _segment_rows(renderable: object, width: int, *, color: bool) -> list[list[tuple[str, str]]]:
    """Visual rows of ``(class, text)`` spans. Rich colours are not copied."""
    console = _console(width, color=color)
    lines = console.render_lines(renderable, console.options.update(width=width), pad=False)
    rows: list[list[tuple[str, str]]] = []
    for line in lines:
        spans: list[tuple[str, str]] = []
        for segment in line:
            if getattr(segment, "control", None):
                continue
            text = str(segment.text or "").replace("\n", "")
            if not text:
                continue
            spans.append((_span_class(segment.style) or _PROSE, text))
        rows.append(_scrub_spans(spans))
    while rows and not any(text.strip() for _style, text in rows[-1]):
        rows.pop()
    return rows


def _style_rows(lines: Iterable[str], token: str = _PROSE) -> list[list[tuple[str, str]]]:
    return [[(token, line)] for line in lines]


def _wrap_code_line(line: str, width: int) -> list[str]:
    """Wrap one code line without a hanging indent. Markers inside the line stay."""
    if line == "":
        return [""]
    return wrap_words(line, max(1, width), hanging=0) or [""]


def _seal_table_rows(text: str) -> str:
    """Join a split table row only outside fences and indented code.

    A newline inside a cell must not become its own line. Fenced and indented
    code are already complete, so a ``|`` line there is left untouched.
    """
    lines = text.splitlines()
    sealed: list[str] = []
    index = 0
    fence: str | None = None
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        if fence is not None:
            sealed.append(line)
            if _closes_fence(stripped, fence):
                fence = None
            index += 1
            continue
        opened = _fence_marker(stripped)
        if opened is not None:
            fence = opened
            sealed.append(line)
            index += 1
            continue
        if line.startswith(("    ", "\t")):
            sealed.append(line)
            index += 1
            continue
        if stripped.startswith("|") and not stripped.endswith("|"):
            parts = [stripped]
            index += 1
            while index < len(lines):
                nxt = lines[index]
                nxt_stripped = nxt.lstrip()
                if (
                    not nxt_stripped
                    or _fence_marker(nxt_stripped) is not None
                    or nxt.startswith(("    ", "\t"))
                ):
                    break
                parts.append(nxt_stripped)
                index += 1
                if nxt_stripped.endswith("|"):
                    break
            sealed.append(" ".join(parts))
            continue
        sealed.append(line)
        index += 1
    return "\n".join(sealed)


_MARKDOWN_IT = MarkdownIt("commonmark", {"html": False, "linkify": False})
_MARKDOWN_IT.enable("table")


def _token_href(token: object) -> str:
    attrs = getattr(token, "attrs", None) or {}
    if isinstance(attrs, dict):
        return str(attrs.get("href") or "")
    return ""


def _consume_until(children: list[object], index: int, close: str) -> tuple[list[object], int]:
    inner: list[object] = []
    while index < len(children) and getattr(children[index], "type", "") != close:
        inner.append(children[index])
        index += 1
    if index < len(children):
        index += 1
    return inner, index


def _inline_spans(children: list[object] | None, *, table: bool) -> list[tuple[str, str]]:
    """Inline tokens. Code, link text, and link targets are not rewritten."""
    spans: list[tuple[str, str]] = []
    tokens = list(children) if children else []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        kind = str(getattr(token, "type", "") or "")
        if kind == "text":
            text = str(getattr(token, "content", "") or "").replace("\n", " ")
            if table:
                # A cell is not an open link. A literal closer stays out of the frame.
                text = text.replace("](", " ")
            if text:
                spans.append((_PROSE, text))
            index += 1
            continue
        if kind == "code_inline":
            spans.append(("class:tui-inline", str(getattr(token, "content", "") or "")))
            index += 1
            continue
        if kind == "softbreak":
            spans.append((_PROSE, " "))
            index += 1
            continue
        if kind == "hardbreak":
            spans.append((_PROSE, "\n"))
            index += 1
            continue
        if kind == "strong_open":
            inner, index = _consume_until(tokens, index + 1, "strong_close")
            for _style, text in _inline_spans(inner, table=table):
                spans.append(("class:tui-strong", text))
            continue
        if kind == "link_open":
            href = _token_href(token)
            inner, index = _consume_until(tokens, index + 1, "link_close")
            label = "".join(text for _style, text in _inline_spans(inner, table=False))
            shown = label
            if href and href not in label:
                shown = f"{label} ({href})".strip()
            if shown:
                spans.append(("class:tui-inline", shown))
            continue
        if kind == "image":
            alt_children = getattr(token, "children", None)
            alt = "".join(text for _style, text in _inline_spans(alt_children, table=table))
            alt = " ".join(alt.split()) or " ".join(str(getattr(token, "content", "") or "").split())
            spans.append(("class:tui-inline", f"[image: {alt}]" if alt else "[image]"))
            index += 1
            continue
        if kind in {"em_open", "s_open"}:
            close = "em_close" if kind == "em_open" else "s_close"
            inner, index = _consume_until(tokens, index + 1, close)
            spans.extend(_inline_spans(inner, table=table))
            continue
        index += 1
    return _coalesce(spans)


def _paint_spans(
    spans: list[tuple[str, str]],
    width: int,
    *,
    hanging: int = 0,
) -> list[list[tuple[str, str]]]:
    if not spans:
        return []
    paragraphs: list[list[tuple[str, str]]] = [[]]
    for style, text in spans:
        pieces = text.split("\n")
        for offset, piece in enumerate(pieces):
            if offset:
                paragraphs.append([])
            if piece:
                paragraphs[-1].append((style, piece))
    rows: list[list[tuple[str, str]]] = []
    for paragraph in paragraphs:
        if not paragraph:
            rows.append([])
            continue
        plain_text = "".join(text for _style, text in paragraph)
        if _display_width(plain_text) <= width:
            rows.append(_coalesce(paragraph))
            continue
        wrapped = wrap_words(plain_text, width, hanging=hanging)
        source: list[tuple[str, str]] = []
        for style, text in paragraph:
            source.extend((style, char) for char in text)
        cursor = 0
        for line in wrapped:
            # Scrubbing drops whitespace-only lead spans, so a list item's
            # indent is put back as one span after the row is painted.
            lead = len(line) - len(line.lstrip(" ")) if hanging > 0 else 0
            row: list[tuple[str, str]] = []
            for char in line[lead:]:
                if cursor < len(source) and source[cursor][1] == char:
                    row.append(source[cursor])
                    cursor += 1
                    continue
                if char == " ":
                    row.append((_PROSE, " "))
                    continue
                while cursor < len(source) and source[cursor][1] != char:
                    cursor += 1
                if cursor < len(source):
                    row.append(source[cursor])
                    cursor += 1
                else:
                    row.append((_PROSE, char))
            painted = _scrub_spans(row) or _coalesce(row)
            if lead and painted:
                painted = [(_PROSE, " " * lead), *painted]
            rows.append(painted)
    return rows


def _append_block(
    rows: list[list[tuple[str, str]]],
    block: list[list[tuple[str, str]]],
    *,
    gap: bool = True,
) -> None:
    if not block:
        return
    if gap and rows and rows[-1]:
        rows.append([])
    rows.extend(block)


def _list_start(token: object) -> int:
    attrs = getattr(token, "attrs", None) or {}
    raw = attrs.get("start") if isinstance(attrs, dict) else None
    try:
        return int(raw) if raw is not None else 1
    except (TypeError, ValueError):
        return 1


def _token_kind(token: object) -> str:
    return str(getattr(token, "type", "") or "")


_LIST_OPEN = frozenset({"bullet_list_open", "ordered_list_open"})
_LIST_CLOSE = frozenset({"bullet_list_close", "ordered_list_close"})


def _code_rows(content: str, width: int) -> list[list[tuple[str, str]]]:
    lines = content.splitlines() or [""]
    gutter = "│ "
    room = max(1, width - _display_width(gutter))
    rows: list[list[tuple[str, str]]] = []
    for line in lines:
        for part in _wrap_code_line(line, room) or [""]:
            shown = f"{gutter}{part}" if width >= _display_width(gutter) else part
            rows.append([("class:tui-code", shown)])
    return rows


def _render_table(
    tokens: list[object],
    start: int,
    _width: int,
    *,
    fold: bool,
) -> tuple[list[list[tuple[str, str]]], int]:
    header_rows: list[list[list[tuple[str, str]]]] = []
    body_rows: list[list[list[tuple[str, str]]]] = []
    current: list[list[list[tuple[str, str]]]] | None = None
    row: list[list[tuple[str, str]]] | None = None
    cell: list[tuple[str, str]] | None = None
    index = start + 1
    while index < len(tokens):
        kind = str(getattr(tokens[index], "type", "") or "")
        if kind == "table_close":
            break
        if kind == "thead_open":
            current = header_rows
        elif kind == "tbody_open":
            current = body_rows
        elif kind == "tr_open":
            row = []
            if current is not None:
                current.append(row)
        elif kind in {"th_open", "td_open"}:
            cell = []
            if row is not None:
                row.append(cell)
        elif kind == "inline" and cell is not None:
            children = getattr(tokens[index], "children", None)
            cell.extend(_inline_spans(children, table=True))
        elif kind in {"th_close", "td_close"}:
            cell = None
        index += 1
    rows: list[list[tuple[str, str]]] = []
    # Column widths come from every row, including folded ones, so the visible
    # columns do not jump when the tail is hidden.
    all_rows = [*header_rows, *body_rows]
    column_count = max((len(item) for item in all_rows), default=0)
    column_widths = [0] * column_count
    shown_body = body_rows if not fold else body_rows[:4]
    for item in [*header_rows, *body_rows]:
        for column, cell_spans in enumerate(item):
            text = "".join(fragment for _style, fragment in cell_spans)
            column_widths[column] = max(column_widths[column], _display_width(text))

    def paint(item: list[list[tuple[str, str]]], *, header: bool) -> list[tuple[str, str]]:
        spans: list[tuple[str, str]] = []
        for column, cell_spans in enumerate(item):
            if spans:
                spans.append(("class:tui-strong" if header else _PROSE, "  "))
            if header:
                text = "".join(fragment for _style, fragment in cell_spans)
                spans.append(("class:tui-strong", text))
                pad = column_widths[column] - _display_width(text)
            else:
                spans.extend(cell_spans or [(_PROSE, "")])
                text = "".join(fragment for _style, fragment in cell_spans)
                pad = column_widths[column] - _display_width(text)
            if column < len(item) - 1 and pad > 0:
                spans.append(("class:tui-strong" if header else _PROSE, " " * pad))
        return _coalesce(spans)

    header_lines = [paint(item, header=True) for item in header_rows]
    rows.extend(header_lines)
    if header_lines:
        rule = "─" * max(1, _display_width("".join(text for _style, text in header_lines[0])))
        rows.append([("class:tui-meta", rule)])
    for item in shown_body:
        painted = paint(item, header=False)
        if painted:
            rows.append(painted)
    hidden = len(body_rows) - len(shown_body)
    if hidden > 0:
        rows.append([("class:tui-meta", f"… {hidden} more rows")])
    return rows, index


def _rows_from_tokens(
    tokens: list[object],
    width: int,
    *,
    fold: bool,
    source: str,
) -> list[list[tuple[str, str]]]:
    del source
    rows: list[list[tuple[str, str]]] = []
    index = 0
    # Lists always render compact. Other blocks, including those inside a
    # blockquote, get one blank row between them.
    list_kinds: list[str] = []
    list_numbers: list[int] = []
    pending_marker = ""
    list_gap = False

    def take_gap() -> bool:
        nonlocal list_gap
        gap = not list_kinds or list_gap
        list_gap = False
        return gap

    while index < len(tokens):
        kind = _token_kind(tokens[index])
        if kind in _LIST_OPEN:
            if not list_kinds:
                list_gap = True
            ordered = kind == "ordered_list_open"
            list_kinds.append("ordered" if ordered else "bullet")
            list_numbers.append(_list_start(tokens[index]) if ordered else 0)
        elif kind in _LIST_CLOSE:
            if list_kinds:
                list_kinds.pop()
                list_numbers.pop()
                list_gap = False
        elif kind == "list_item_open":
            indent = "  " * max(0, len(list_kinds) - 1)
            if list_kinds and list_kinds[-1] == "ordered":
                pending_marker = f"{indent}{list_numbers[-1]}. "
                list_numbers[-1] += 1
            else:
                pending_marker = f"{indent}• "
        elif kind == "list_item_close":
            pending_marker = ""
        elif kind == "heading_open":
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            children = getattr(inline, "children", None) if getattr(inline, "type", "") == "inline" else None
            text = "".join(fragment for _style, fragment in _inline_spans(children, table=False))
            _append_block(
                rows, _paint_spans([("class:tui-heading", text)], width), gap=take_gap()
            )
            while index < len(tokens) and getattr(tokens[index], "type", "") != "heading_close":
                index += 1
        elif kind == "paragraph_open":
            inline = tokens[index + 1] if index + 1 < len(tokens) else None
            children = getattr(inline, "children", None) if getattr(inline, "type", "") == "inline" else None
            spans = _inline_spans(children, table=False)
            hanging = 0
            if pending_marker:
                spans = [("class:tui-bullet", pending_marker), *spans]
                hanging = _display_width(pending_marker)
                pending_marker = ""
            _append_block(
                rows, _paint_spans(spans, width, hanging=hanging), gap=take_gap()
            )
            while index < len(tokens) and getattr(tokens[index], "type", "") != "paragraph_close":
                index += 1
        elif kind in {"fence", "code_block"}:
            block = _code_rows(str(getattr(tokens[index], "content", "") or ""), width)
            if pending_marker:
                block = [[("class:tui-bullet", pending_marker.rstrip())], *block]
                pending_marker = ""
            _append_block(rows, block, gap=take_gap())
        elif kind == "table_open":
            block, index = _render_table(tokens, index, width, fold=fold)
            if pending_marker:
                block = [[("class:tui-bullet", pending_marker.rstrip())], *block]
                pending_marker = ""
            _append_block(rows, block, gap=take_gap())
        index += 1
    while rows and not rows[-1]:
        rows.pop()
    return rows


def render_markdown(
    text: object,
    width: int,
    theme: Theme | None = None,
    *,
    fold_tables: bool = True,
) -> list[list[tuple[str, str]]]:
    """Render markdown from markdown-it tokens.

    Fences and indented code are sealed off before a broken table row is
    joined. Code, inline code, and link text are copied from the tokens.
    """
    del theme
    cleaned = strip_protocol_trailers(text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.strip("\n")
    if not cleaned.strip():
        return []
    width = max(1, int(width))
    source = _seal_table_rows(cleaned)
    try:
        tokens = _MARKDOWN_IT.parse(source)
        return _rows_from_tokens(tokens, width, fold=bool(fold_tables), source=source)
    except (ValueError, TypeError, KeyError, AttributeError):
        fallback = _scrub(cleaned).splitlines() or [_scrub(cleaned)]
        return _style_rows(fallback)


def render_plain(text: object, width: int = 80) -> list[str]:
    """The same layout as :func:`render_markdown`, without styles."""
    rows = render_markdown(
        text,
        width,
        resolve_theme({"CONDUCTOR_THEME": "none"}),
        fold_tables=False,
    )
    return ["".join(fragment for _token, fragment in row) for row in rows]


def _char_width(char: str) -> int:
    if char in "\x00\n\r":
        return 0
    east = unicodedata.east_asian_width(char)
    if east in {"W", "F"}:
        return 2
    if unicodedata.category(char) in {"Mn", "Me", "Cf"}:
        return 0
    return 1


def _display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def wrap_words(text: object, width: int, *, hanging: int = 2) -> list[str]:
    """Word wrap with a hanging indent and CJK line-start punctuation pulled back.

    An ASCII word is split only when it is longer than ``width``. A hanging
    indent is dropped when it would push a word that already fits past the
    width. Width below 1 is clamped to 1.
    """
    width = max(1, int(width))
    hanging = max(0, min(int(hanging), width - 1))
    source = strip_protocol_trailers(text)
    if source == "":
        return []
    wrapped: list[str] = []
    for paragraph in source.splitlines():
        if paragraph == "":
            wrapped.append("")
            continue
        wrapped.extend(_wrap_paragraph(paragraph, width, hanging))
    return wrapped


def _glue_kinsoku(tokens: list[str]) -> list[str]:
    """Attach kinsoku punctuation to the previous non-space token."""
    glued: list[str] = []
    for token in tokens:
        if len(token) == 1 and token in _KINSOKU and glued and not glued[-1].isspace():
            glued[-1] += token
            continue
        if (
            len(token) == 1
            and token in _KINSOKU
            and len(glued) > 1
            and glued[-1].isspace()
        ):
            space = glued.pop()
            glued[-1] += token
            glued.append(space)
            continue
        glued.append(token)
    return glued


def _wrap_tokens(paragraph: str) -> list[str]:
    """Whitespace, ASCII words, and one CJK character per token."""
    tokens: list[str] = []
    index = 0
    length = len(paragraph)
    while index < length:
        char = paragraph[index]
        if char.isspace():
            end = index + 1
            while end < length and paragraph[end].isspace():
                end += 1
            tokens.append(paragraph[index:end])
            index = end
            continue
        if unicodedata.category(char) in {"Mn", "Me"} and tokens and not tokens[-1].isspace():
            tokens[-1] += char
            index += 1
            continue
        if char.isascii() and char not in _KINSOKU:
            end = index + 1
            while (
                end < length
                and paragraph[end].isascii()
                and not paragraph[end].isspace()
                and paragraph[end] not in _KINSOKU
            ):
                end += 1
            tokens.append(paragraph[index:end])
            index = end
            continue
        tokens.append(char)
        index += 1
    return _glue_kinsoku(tokens)


def _only_hanging(buf: list[str], hanging: int) -> bool:
    return bool(hanging) and buf == [" " * hanging]


def _wrap_paragraph(paragraph: str, width: int, hanging: int) -> list[str]:
    lines: list[str] = []
    buf: list[str] = []
    used = 0

    def finish() -> None:
        nonlocal buf, used
        if not buf:
            return
        text = "".join(buf).rstrip()
        if text:
            lines.append(text)
        buf = []
        used = 0

    def begin_continuation() -> None:
        nonlocal buf, used
        if hanging <= 0:
            return
        buf.append(" " * hanging)
        used = hanging

    def place(token: str, token_width: int, *, hard: bool) -> None:
        nonlocal buf, used
        if token_width > width and hard:
            if used > 0 and not _only_hanging(buf, hanging):
                finish()
                begin_continuation()
            for piece in token:
                piece_width = _char_width(piece)
                if used + piece_width > width and used > 0:
                    finish()
                    begin_continuation()
                    if used + piece_width > width:
                        buf = []
                        used = 0
                buf.append(piece)
                used += piece_width
            return
        if used + token_width > width:
            finish()
            begin_continuation()
            if used + token_width > width:
                buf = []
                used = 0
        buf.append(token)
        used += token_width

    for token in _wrap_tokens(paragraph):
        if token.isspace():
            # A continuation already has its hanging indent. A space that
            # opened the next line is the break, so it is not copied. Leading
            # spaces on the first line are the row's own indent and stay.
            if _only_hanging(buf, hanging) or (used == 0 and lines):
                continue
            space_width = _display_width(token)
            if used + space_width > width:
                finish()
                begin_continuation()
                continue
            buf.append(token)
            used += space_width
            continue
        ascii_word = token.isascii()
        place(token, _display_width(token), hard=ascii_word and _display_width(token) > width)
    finish()
    return lines or [""]


def fold_rows(
    rows: list[list[tuple[str, str]]],
    *,
    max_rows: int,
) -> tuple[list[list[tuple[str, str]]], int]:
    """Keep ``max_rows`` visual rows and report how many were hidden."""
    if max_rows < 0:
        max_rows = 0
    if len(rows) <= max_rows:
        return rows, 0
    hidden = len(rows) - max_rows
    return rows[:max_rows], hidden


def render_group(parts: Iterable[object], width: int) -> list[str]:
    """Plain-render several Rich renderables as one block."""
    return [
        "".join(text for _style, text in row)
        for row in _segment_rows(Group(*parts), width, color=False)
    ]
