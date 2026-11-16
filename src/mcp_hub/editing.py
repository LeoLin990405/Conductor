"""Surgical JSON and TOML edits.

Unmanaged server text is copied through as raw slices. Sibling keys outside
the server map are not re-serialised. TOML lines that are not a managed
``[mcp_servers.NAME]`` table are copied unchanged, comments included, and the
same holds for a server map the preamble declares as one inline table: only
the managed keys inside it move.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass

from .errors import McpHubError

_DECODER = json.JSONDecoder()
_WS = " \t\r\n"
_HEADER_RE = re.compile(r"^\s*\[\[?[A-Za-z0-9_\"'.-]")
_MCP_RE = re.compile(
    r"^\s*\[mcp_servers\.(?:\"((?:\\.|[^\"\\])+)\"|([A-Za-z0-9_-]+))"
    r"(?:\.[A-Za-z0-9_-]+)?\]\s*(?:#.*)?$"
)


@dataclass(frozen=True)
class KeySpan:
    key: str
    key_start: int
    value_start: int
    value_end: int


def _skip_ws(text: str, index: int) -> int:
    length = len(text)
    while index < length and text[index] in _WS:
        index += 1
    return index


def _parse_object(text: str, index: int) -> tuple[int, list[KeySpan]]:
    """``index`` points at ``{``. Returns the index after ``}`` and key spans."""
    if index >= len(text) or text[index] != "{":
        raise McpHubError("expected a JSON object")
    index += 1
    spans: list[KeySpan] = []
    while True:
        index = _skip_ws(text, index)
        if index >= len(text):
            raise McpHubError("unterminated JSON object")
        if text[index] == "}":
            return index + 1, spans
        if text[index] != '"':
            raise McpHubError("expected a JSON key")
        key_start = index
        try:
            key, index = _DECODER.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            raise McpHubError(f"invalid JSON key: {exc.msg}") from exc
        if not isinstance(key, str):
            raise McpHubError("JSON key is not a string")
        index = _skip_ws(text, index)
        if index >= len(text) or text[index] != ":":
            raise McpHubError("expected ':' after a JSON key")
        index += 1
        value_start = _skip_ws(text, index)
        try:
            _value, value_end = _DECODER.raw_decode(text, value_start)
        except json.JSONDecodeError as exc:
            raise McpHubError(f"invalid JSON value: {exc.msg}") from exc
        spans.append(KeySpan(key, key_start, value_start, value_end))
        index = _skip_ws(text, value_end)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "}":
            return index + 1, spans
        raise McpHubError("expected ',' or '}' in a JSON object")


def _top_object(text: str) -> tuple[int, int, list[KeySpan]]:
    start = _skip_ws(text, 0)
    if start >= len(text):
        raise McpHubError("agent config is empty")
    end, spans = _parse_object(text, start)
    if _skip_ws(text, end) != len(text):
        raise McpHubError("trailing data after JSON value")
    return start, end, spans


def _find_key(spans: list[KeySpan], key: str) -> KeySpan | None:
    found: KeySpan | None = None
    for span in spans:
        if span.key == key:
            found = span
    return found


def _require_object(text: str, span: KeySpan) -> tuple[int, list[KeySpan]]:
    if text[span.value_start] != "{":
        raise McpHubError(f"JSON value for {span.key!r} is not an object")
    return _parse_object(text, span.value_start)


def _dump_entry(name: str, value: dict[str, object]) -> str:
    key = json.dumps(name, ensure_ascii=False)
    body = json.dumps(value, indent=2, ensure_ascii=False)
    indented = body.replace("\n", "\n  ")
    return f"{key}: {indented}"


def _canonical_object(entries: dict[str, dict[str, object]]) -> str:
    parts = [_dump_entry(name, entries[name]) for name in sorted(entries)]
    if not parts:
        return "{}"
    return "{\n  " + ",\n  ".join(parts) + "\n}"


def _subset_equal(existing: object, desired: object) -> bool:
    if isinstance(desired, dict):
        if not isinstance(existing, dict):
            return False
        for key, value in desired.items():
            if key not in existing or not _subset_equal(existing[key], value):
                return False
        if "env" in desired and isinstance(existing.get("env"), dict):
            if set(existing["env"]) != set(desired["env"]):
                return False
        return True
    if isinstance(desired, list):
        if not isinstance(existing, list) or len(existing) != len(desired):
            return False
        return all(_subset_equal(left, right) for left, right in zip(existing, desired))
    return existing == desired


def _insert_key(text: str, obj_start: int, obj_end: int, key: str, value: str) -> str:
    key_text = json.dumps(key, ensure_ascii=False)
    insertion = f"{key_text}: {value}"
    close = obj_end - 1
    interior = text[obj_start + 1 : close]
    if not interior.strip():
        return (
            text[obj_start : obj_start + 1]
            + "\n  "
            + insertion
            + "\n"
            + text[close:obj_end]
        )
    prefix = text[obj_start:close].rstrip()
    return prefix + ",\n  " + insertion + "\n" + text[close:obj_end]


def _value_for_missing(
    remaining: tuple[str, ...],
    rebuild,
) -> str | None:
    if not remaining:
        return rebuild(None)
    inner = _value_for_missing(remaining[1:], rebuild)
    if inner is None:
        return None
    return "{\n  " + json.dumps(remaining[0], ensure_ascii=False) + ": " + inner + "\n}"


def _rebuild_object(
    text: str,
    obj_start: int,
    obj_end: int,
    spans: list[KeySpan],
    path: tuple[str, ...],
    rebuild,
) -> str | None:
    """Return a replacement for this object, or None when nothing changes."""
    key = path[0]
    rest = path[1:]
    span = _find_key(spans, key)
    if not rest:
        if span is None:
            new_val = rebuild(None)
            if new_val is None:
                return None
            return _insert_key(text, obj_start, obj_end, key, new_val)
        _end, child_spans = _require_object(text, span)
        new_val = rebuild(child_spans)
        if new_val is None:
            return None
        return (
            text[obj_start : span.value_start]
            + new_val
            + text[span.value_end : obj_end]
        )
    if span is None:
        new_val = _value_for_missing(rest, rebuild)
        if new_val is None:
            return None
        return _insert_key(text, obj_start, obj_end, key, new_val)
    _end, child_spans = _require_object(text, span)
    new_child = _rebuild_object(
        text, span.value_start, span.value_end, child_spans, rest, rebuild
    )
    if new_child is None:
        return None
    return (
        text[obj_start : span.value_start] + new_child + text[span.value_end : obj_end]
    )


def edit_json_container(text: str, path: tuple[str, ...], rebuild) -> str:
    """Replace the server-map object at ``path``. Other bytes stay put.

    ``rebuild(spans)`` receives the map's key spans (or None when the map is
    absent) and returns new object text, or None to leave the file alone.
    """
    if not path:
        raise McpHubError("missing JSON container")
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpHubError(f"agent config is not JSON: {exc.msg}") from exc
    obj_start, obj_end, spans = _top_object(text)
    new_obj = _rebuild_object(text, obj_start, obj_end, spans, path, rebuild)
    if new_obj is None:
        return text
    updated = text[:obj_start] + new_obj + text[obj_end:]
    try:
        json.loads(updated)
    except json.JSONDecodeError as exc:
        raise McpHubError(f"refusing to write invalid JSON: {exc.msg}") from exc
    return updated


def merge_server_object(
    text: str,
    spans: list[KeySpan] | None,
    desired: dict[str, dict[str, object]],
    remove: set[str],
) -> str | None:
    """Rebuild a server map. Returns None when the desired map is already there."""
    if spans is None:
        if not desired:
            return None
        return _canonical_object(desired)
    present = {span.key for span in spans}
    changed = any(name in present for name in remove)
    if not changed:
        for name, value in desired.items():
            span = _find_key(spans, name)
            if span is None:
                changed = True
                break
            parsed = json.loads(text[span.value_start : span.value_end])
            if not _subset_equal(parsed, value):
                changed = True
                break
    if not changed:
        return None
    parts: list[str] = []
    seen: set[str] = set()
    for span in spans:
        if span.key in remove:
            continue
        raw = text[span.key_start : span.value_end]
        if span.key in desired:
            parsed = json.loads(text[span.value_start : span.value_end])
            if _subset_equal(parsed, desired[span.key]):
                parts.append(raw)
            else:
                parts.append(_dump_entry(span.key, desired[span.key]))
            seen.add(span.key)
        else:
            parts.append(raw)
    for name in sorted(desired):
        if name not in seen:
            parts.append(_dump_entry(name, desired[name]))
    if not parts:
        return "{}"
    return "{\n  " + ",\n  ".join(parts) + "\n}"


def json_servers(text: str, path: tuple[str, ...]) -> dict[str, dict[str, object]]:
    """Parsed server map, or empty when the container is absent."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpHubError(f"agent config is not JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise McpHubError("agent config must be a JSON object")
    cursor: object = data
    for key in path:
        if not isinstance(cursor, dict) or key not in cursor:
            return {}
        cursor = cursor[key]
    if cursor is None:
        return {}
    if not isinstance(cursor, dict):
        raise McpHubError("MCP server map is not a JSON object")
    out: dict[str, dict[str, object]] = {}
    for name, value in cursor.items():
        if isinstance(name, str) and isinstance(value, dict):
            out[name] = value
    return out


@dataclass
class _Block:
    lines: list[str]
    server: str | None


def mcp_table_name(line: str) -> str | None:
    match = _MCP_RE.match(line.strip())
    if not match:
        return None
    raw = match.group(1) or match.group(2)
    if raw is None:
        return None
    return raw.replace('\\"', '"').replace("\\\\", "\\")


def _split_toml(text: str) -> tuple[list[str], list[_Block]]:
    lines = text.splitlines(keepends=True)
    preamble: list[str] = []
    blocks: list[_Block] = []
    current: list[str] | None = None
    current_server: str | None = None
    open_quote: str | None = None

    def flush() -> None:
        nonlocal current, current_server
        if current is not None:
            blocks.append(_Block(current, current_server))
        current = None
        current_server = None

    for line in lines:
        # A "[header]" line inside a multi-line string is string content, not
        # a table header, so it never opens a block. Reading it as one split the
        # preamble mid-string, which then failed to parse and made every
        # scheduled key look unreachable.
        if open_quote is None and _HEADER_RE.match(line):
            flush()
            current = [line]
            current_server = mcp_table_name(line)
        elif current is None:
            preamble.append(line)
        else:
            current.append(line)
        open_quote = _multiline_quote(line, open_quote)
    flush()
    return preamble, blocks


def toml_string(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return key
    return toml_string(key)


def toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return toml_string(value)
    if isinstance(value, dict):
        parts = [
            f"{toml_key(str(key))} = {toml_value(value[key])}" for key in sorted(value)
        ]
        if not parts:
            return "{}"
        return "{ " + ", ".join(parts) + " }"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise McpHubError("cannot encode a TOML value of this type")


def render_toml_server(name: str, fields: dict[str, object]) -> str:
    """One ``[mcp_servers.NAME]`` table plus an env subtable when env is set."""
    lines = [f"[mcp_servers.{name}]"]
    for key in ("command", "args", "url", "enabled", "startup_timeout_sec"):
        if key not in fields:
            continue
        lines.append(f"{key} = {toml_value(fields[key])}")
    body = "\n".join(lines) + "\n"
    env = fields.get("env")
    if isinstance(env, dict) and env:
        body += f"\n[mcp_servers.{name}.env]\n"
        for key in sorted(env):
            body += f"{key} = {toml_value(env[key])}\n"
    return body


@dataclass(frozen=True)
class _InlineEntry:
    key: str
    start: int
    end: int


_INLINE_MCP = re.compile(r"^[ \t]*(?:\"mcp_servers\"|mcp_servers)[ \t]*=[ \t]*\{")
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_BASIC_ESCAPE = re.compile(r"\\(u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|.)")
_BASIC_ESCAPES = {
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    '"': '"',
    "\\": "\\",
}


def _preamble_keys(preamble: str, drop: set[str]) -> set[str]:
    """Keys of ``drop`` the preamble still declares under ``mcp_servers``.

    The inline editor only reaches a one-line ``mcp_servers = { ... }`` map.
    A dotted ``mcp_servers.NAME = ...`` declaration, or a map that spans lines,
    survives it, so appending that key's replacement table would declare the
    same key twice and the file would not parse. Bytes that do not parse prove
    nothing, so every dropped key counts as still declared there.
    """
    if not drop:
        return set()
    try:
        data = tomllib.loads(preamble)
    except (tomllib.TOMLDecodeError, ValueError):
        return set(drop)
    raw = data.get("mcp_servers")
    if not isinstance(raw, dict):
        return set()
    return {str(name) for name in raw if str(name) in drop}


def _edit_inline_preamble(
    preamble: str,
    drop: set[str],
    rewrite: dict[str, dict[str, object]],
) -> tuple[str, set[str]]:
    """Edit the inline ``mcp_servers`` table the preamble declares.

    A hand-written config may declare the server map as one inline table
    instead of the ``[mcp_servers.NAME]`` tables this module writes. A key in
    ``drop`` is cut out and a key in ``rewrite`` is re-rendered in place, so
    the map keeps its representation and every other entry keeps its original
    bytes. Only the preamble is scanned: a ``mcp_servers`` table under another
    header is that table's own key and is never touched. Returns the new
    preamble and the keys it handled.
    """
    if not drop or "mcp_servers" not in preamble:
        return preamble, set()
    lines = preamble.splitlines(keepends=True)
    out: list[str] = []
    handled: set[str] = set()
    open_quote: str | None = None
    for line in lines:
        if open_quote is None:
            updated, keys = _edit_inline_line(line, drop, rewrite)
            out.append(updated)
            handled |= keys
        else:
            out.append(line)
        open_quote = _multiline_quote(line, open_quote)
    if not handled:
        return preamble, set()
    return "".join(out), handled


def _edit_inline_line(
    line: str,
    drop: set[str],
    rewrite: dict[str, dict[str, object]],
) -> tuple[str, set[str]]:
    match = _INLINE_MCP.match(line)
    if match is None:
        return line, set()
    opening = match.end() - 1
    closing = _matching(line, opening, "{", "}")
    if closing is None:
        return line, set()
    body = line[opening + 1 : closing]
    entries = _inline_entries(body)
    if entries is None:
        return line, set()
    handled = {entry.key for entry in entries} & (drop | set(rewrite))
    if not handled:
        return line, set()
    parts: list[str] = []
    for entry in entries:
        if entry.key in rewrite:
            parts.append(f"{toml_key(entry.key)} = {toml_value(rewrite[entry.key])}")
            continue
        if entry.key in drop:
            continue
        parts.append(body[entry.start : entry.end])
    kept = _inline_join(body, entries, parts)
    return line[: opening + 1] + kept + line[closing:], handled


def _inline_join(body: str, entries: list[_InlineEntry], parts: list[str]) -> str:
    """Rebuilt inline body. The padding around the entries is kept."""
    if not parts:
        return ""
    lead = body[: entries[0].start]
    trail = body[entries[-1].end :]
    return lead + ", ".join(parts) + trail


def _multiline_quote(line: str, open_quote: str | None) -> str | None:
    """The multi-line string left open at the end of ``line``."""
    cursor = 0
    while cursor < len(line):
        char = line[cursor]
        # Inside a basic (``"""``) multi-line string a backslash escapes the
        # next byte, so ``\"`` is one literal quote and an escaped triple quote
        # ``\"""`` never terminates the string. A literal (``'''``) multi-line
        # string has no escapes, so the backslash is just content there.
        if char == "\\" and open_quote == '"""':
            cursor += 2
            continue
        if char == "#" and open_quote is None:
            return None
        if char not in "\"'":
            cursor += 1
            continue
        if open_quote is not None:
            if line.startswith(open_quote, cursor):
                open_quote = None
                cursor += 3
            else:
                cursor += 1
            continue
        if line.startswith(char * 3, cursor):
            open_quote = char * 3
            cursor += 3
            continue
        end = _skip_string(line, cursor)
        cursor = len(line) if end is None else end
    return open_quote


def _inline_entries(body: str) -> list[_InlineEntry] | None:
    """Key spans of an inline table body, or None when it cannot be scanned."""
    entries: list[_InlineEntry] = []
    cursor = _skip_ws(body, 0)
    while cursor < len(body):
        start = cursor
        key, after = _inline_key(body, cursor)
        if key is None:
            return None
        cursor = _skip_ws(body, after)
        if cursor >= len(body) or body[cursor] != "=":
            return None
        cursor = _skip_ws(body, cursor + 1)
        end = _value_end(body, cursor)
        if end is None:
            return None
        entries.append(_InlineEntry(key, start, end))
        cursor = _skip_ws(body, end)
        if cursor >= len(body):
            return entries
        if body[cursor] != ",":
            return None
        cursor = _skip_ws(body, cursor + 1)
    return entries


def _inline_key(text: str, index: int) -> tuple[str | None, int]:
    """Bare or quoted key at ``index``, and the index just past it."""
    char = text[index]
    if char in "\"'":
        end = _skip_string(text, index)
        if end is None:
            return None, index
        body = text[index + 1 : end - 1]
        if char == "'":
            return body, end
        return _decode_basic_string(body), end
    match = _BARE_KEY.match(text, index)
    if match is None:
        return None, index
    return match.group(0), match.end()


def _decode_basic_string(body: str) -> str:
    """Decode a TOML basic string body, escapes included.

    A key written ``"\\u0061"`` is the key ``a``. Matching the raw text instead
    would read it as ``u0061``, so a release aimed at one key could reach an
    unowned entry, or miss the owned one entirely.
    """

    def replace(match: re.Match[str]) -> str:
        token = match.group(1)
        if token[0] in "uU":
            try:
                return chr(int(token[1:], 16))
            except ValueError:
                return match.group(0)
        return _BASIC_ESCAPES.get(token, token)

    return _BASIC_ESCAPE.sub(replace, body)


def _value_end(text: str, index: int) -> int | None:
    """Index just past the value starting at ``index``."""
    if index >= len(text):
        return None
    char = text[index]
    if char == "{":
        closing = _matching(text, index, "{", "}")
        return None if closing is None else closing + 1
    if char == "[":
        closing = _matching(text, index, "[", "]")
        return None if closing is None else closing + 1
    if char in "\"'":
        return _skip_string(text, index)
    cursor = index
    while cursor < len(text) and text[cursor] not in ",}":
        cursor += 1
    return None if cursor == index else cursor


def _matching(text: str, index: int, opening: str, closing: str) -> int | None:
    """Index of the delimiter that closes the one at ``index``."""
    depth = 0
    cursor = index
    while cursor < len(text):
        char = text[cursor]
        if char in "\"'":
            end = _skip_string(text, cursor)
            if end is None:
                return None
            cursor = end
            continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def _skip_string(text: str, index: int) -> int | None:
    """Index just past the string starting at ``index``, or None."""
    quote = text[index]
    if text.startswith(quote * 3, index):
        closing = text.find(quote * 3, index + 3)
        if closing < 0:
            return None
        return closing + 3
    cursor = index + 1
    while cursor < len(text):
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == quote:
            return cursor + 1
        if char in "\r\n":
            return None
        cursor += 1
    return None


def edit_toml_servers(
    text: str,
    *,
    rewrite: dict[str, dict[str, object]],
    remove: set[str],
) -> str:
    """Drop managed tables that change and append their replacement.

    Every other line, including comments and unrelated tables, is copied.
    A key the preamble declares inside an inline ``mcp_servers`` table is cut
    out, or re-rendered in place, so a hand-written inline config releases the
    same way a ``[mcp_servers.NAME]`` table does. A key the preamble declares
    in a form the inline editor cannot reach, such as a dotted
    ``mcp_servers.NAME = ...`` line, is left where it is and no replacement is
    appended for it. When nothing managed changes, the original text is
    returned as-is.
    """
    drop = set(remove) | set(rewrite)
    if not drop:
        return text
    preamble, blocks = _split_toml(text)
    new_preamble, inline = _edit_inline_preamble("".join(preamble), drop, rewrite)
    # A key the preamble still declares is one the inline editor could not
    # reach: a dotted ``mcp_servers.NAME = ...`` line, or a map that spans
    # lines. Appending its replacement would declare the same key twice, so it
    # is left where it is and the caller keeps its ownership.
    unreached = _preamble_keys(new_preamble, drop)
    append = {
        name: body
        for name, body in rewrite.items()
        if name not in unreached and name not in inline
    }
    kept: list[_Block] = []
    dropped = False
    for block in blocks:
        if block.server is not None and block.server in drop:
            dropped = True
            continue
        kept.append(block)
    if not dropped and not append:
        return new_preamble + "".join(line for block in blocks for line in block.lines)
    parts: list[str] = [new_preamble]
    for block in kept:
        parts.extend(block.lines)
    body = "".join(parts)
    extra = "".join(render_toml_server(name, append[name]) for name in sorted(append))
    if extra:
        if body and not body.endswith("\n"):
            body += "\n"
        if body and not body.endswith("\n\n"):
            body += "\n"
        body += extra
        if not body.endswith("\n"):
            body += "\n"
    elif not body.endswith("\n") and text.endswith("\n"):
        body += "\n"
    return body
