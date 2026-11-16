"""DeepSeek Harness: MCP servers in ``~/.dsh/cordis.patch.yml``.

The file is a top-level YAML array of loader patch ops (dsh-app-boot
``parsePatchList``: anything but an array is a load error, so an empty or
comment-only file breaks the profile). An MCP server is an entry of the
``@deepseek-ai/dsh-mcp-client`` plugin inside an op's ``insert`` list::

    - insert:
        - id: demo-mcp
          name: '@deepseek-ai/dsh-mcp-client'
          config:
            serverName: demo
            transport: streamable-http   # or stdio
            url: http://127.0.0.1:3000/mcp

The hub's file key is ``config.serverName``: it is the namespace dsh reserves
(two entries with one serverName fail to load) and the prefix of every tool
name. ``id`` is only the loader's row handle; an existing id is kept on
rewrite, a new entry gets ``<serverName>-mcp`` (made unique in the file).
Entry-level ``disabled: true`` is the enabled flag. dsh does not expand
``${VAR}`` in env strings, so a ``${VAR}`` reference is written as
``!!js process.env.VAR`` and read back as ``${VAR}``.

A native body is the entry's config without ``serverName``, plus ``enabled``.
Edits are line-level splices; every byte outside a rewritten or removed entry
is copied unchanged, and the result is re-parsed and checked before it is
returned.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import yaml

from ..agents import AgentSpec
from ..editing import _subset_equal
from ..errors import McpHubError
from ..model import require_name
from .base import is_remote

PLUGIN = "@deepseek-ai/dsh-mcp-client"
MARKER = "# conductor mcp hub: managed MCP servers (edit with `conductor mcp`)"
_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_PROCESS_ENV_RE = re.compile(r"^\s*process\.env\.([A-Za-z_][A-Za-z0-9_]*)\s*$")
_VAR_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_MANAGED_CONFIG = frozenset(
    {"serverName", "transport", "command", "args", "env", "url"}
)
_STDIO_ONLY = frozenset({"cwd"})
_HTTP_ONLY = frozenset({"headers"})
_ITEM_META = frozenset({"id", "name", "config", "disabled"})


@dataclass(frozen=True)
class JsExpr:
    """A ``!!js`` expression. Kept opaque; its repr never shows the source."""

    source: str

    def __repr__(self) -> str:
        digest = hashlib.sha256(self.source.encode("utf-8")).hexdigest()[:16]
        return f"JsExpr(sha256:{digest})"


class _Loader(yaml.SafeLoader):
    pass


def _construct_js(loader: yaml.SafeLoader, node: yaml.Node) -> JsExpr:
    if not isinstance(node, yaml.ScalarNode):
        raise yaml.constructor.ConstructorError(
            None, None, "!!js must tag a scalar", node.start_mark
        )
    return JsExpr(str(loader.construct_scalar(node)))


_Loader.add_constructor("tag:yaml.org,2002:js", _construct_js)


# --------------------------------------------------------------------------
# reading


def _load(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_Loader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        raise McpHubError(f"dsh cordis.patch.yml is not valid YAML: {_mark(exc)}") from exc


def _mark(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or "parse error"
    if mark is None:
        return str(problem)
    return f"{problem} at line {mark.line + 1}"


def _root_list(data: Any) -> list[Any]:
    if data is None:
        return []
    if not isinstance(data, list):
        raise McpHubError(
            "dsh cordis.patch.yml must be a top-level YAML array of patch ops"
        )
    return data


def _walk_entries(entries: Any):
    """Yield every loader entry under an insert list, groups included."""
    if not isinstance(entries, list):
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        yield entry
        if entry.get("group") and isinstance(entry.get("config"), list):
            yield from _walk_entries(entry["config"])


def _mcp_entries(ops: list[Any]):
    for op in ops:
        if isinstance(op, dict):
            yield from (
                entry for entry in _walk_entries(op.get("insert")) if _is_mcp(entry)
            )


def _is_mcp(entry: dict[str, Any]) -> bool:
    return entry.get("name") == PLUGIN and isinstance(entry.get("config"), dict)


def _entry_key(entry: dict[str, Any]) -> str | None:
    name = entry["config"].get("serverName")
    if not isinstance(name, str) or not _SERVER_NAME_RE.fullmatch(name):
        return None
    try:
        return require_name(name)
    except McpHubError:
        return None


def _body(entry: dict[str, Any]) -> dict[str, Any]:
    config = entry["config"]
    body: dict[str, Any] = {k: v for k, v in config.items() if k != "serverName"}
    if body.get("transport") == "stdio":
        body.setdefault("args", [])
        env = body.get("env")
        if env is None:
            body["env"] = {}
        elif isinstance(env, dict):
            body["env"] = {k: _env_in(v) for k, v in env.items()}
    for key, value in entry.items():
        if key not in _ITEM_META:
            body.setdefault(key, value)
    disabled = entry.get("disabled")
    if disabled is None or isinstance(disabled, bool):
        body["enabled"] = not disabled
    else:
        body["enabled"] = disabled  # a !!js condition: neither on nor off
    return body


def _tolerate_js(existing: dict[str, Any], desired: dict[str, Any]) -> dict[str, Any]:
    """Hand-written ``!!js`` expressions are the user's, not drift.

    A ``disabled: !!js <condition>`` stands for "enabled" when the hub wants
    the server on, and an env value that is a ``!!js`` expression (say
    ``process.env.T || 'x'``) stands for the ``${T}`` reference the hub would
    write. The hub never rewrites an entry just to replace them, and
    ``_emit_item`` carries them over when it rewrites for another reason.
    """
    out = dict(existing)
    if isinstance(out.get("enabled"), JsExpr) and desired.get("enabled", True) is True:
        out["enabled"] = True
    env = out.get("env")
    want = desired.get("env")
    if isinstance(env, dict) and isinstance(want, dict):
        out["env"] = {
            key: (want[key] if isinstance(value, JsExpr) and key in want else value)
            for key, value in env.items()
        }
    return out


def _env_in(value: Any) -> Any:
    if isinstance(value, JsExpr):
        match = _PROCESS_ENV_RE.fullmatch(value.source)
        if match:
            return "${" + match.group(1) + "}"
    return value


def _parse(text: str) -> tuple[dict[str, dict[str, Any]], list[Any], set[str]]:
    ops = _root_list(_load(text))
    out: dict[str, dict[str, Any]] = {}
    dupes: set[str] = set()
    for entry in _mcp_entries(ops):
        key = _entry_key(entry)
        if key is None:
            continue
        if key in out:
            dupes.add(key)
            continue
        out[key] = _body(entry)
    return out, ops, dupes


# --------------------------------------------------------------------------
# projection


def native(effective: dict[str, Any]) -> dict[str, Any]:
    url = str(effective["url"])
    command = str(effective["command"])
    if is_remote(effective) or (url and not command):
        return {"transport": "streamable-http", "url": url, "enabled": True}
    return {
        "transport": "stdio",
        "command": command,
        "args": list(effective["args"]),
        "env": dict(effective["env"]),
        "enabled": True,
    }


def cannot(name: str, effective: dict[str, Any], allow: set[str]) -> str | None:
    del allow
    if not _SERVER_NAME_RE.fullmatch(name):
        return f"{name}: dsh serverName must be at most 32 letters, digits, '_' or '-'"
    if str(effective["transport"]) == "sse":
        return f"{name}: dsh supports stdio and streamable-http only, not sse"
    body = native(effective)
    if body["transport"] == "streamable-http" and effective["env"]:
        return f"{name}: dsh streamable-http servers have no env field"
    if body["transport"] == "stdio" and not body["command"]:
        return f"{name}: dsh stdio servers need a command"
    return None


class DshCodec:
    def parse(self, text: str) -> dict[str, dict[str, Any]]:
        return _parse(text)[0]

    def generic(self, body: dict[str, Any]) -> dict[str, Any]:
        transport = body.get("transport")
        out: dict[str, Any] = {
            "transport": "http" if transport == "streamable-http" else transport,
            "enabled": body.get("enabled", True),
        }
        for key in ("command", "args", "url", "env"):
            if key in body:
                out[key] = body[key]
        return out

    def matches(self, existing: dict[str, Any], desired: dict[str, Any]) -> bool:
        existing = _tolerate_js(existing, desired)
        if existing.get("enabled", True) is not desired.get("enabled", True):
            return False
        rest = {k: v for k, v in desired.items() if k != "enabled"}
        return _subset_equal(existing, rest)

    def edit(
        self,
        original: str,
        desired: dict[str, dict[str, Any]],
        remove: set[str],
    ) -> str:
        return _edit(original, desired, set(remove))

    def fresh(self, desired: dict[str, dict[str, Any]]) -> str:
        if not desired:
            return "[]\n"
        used: set[str] = set()
        items = "".join(
            _emit_item(key, desired[key], {}, None, 4, used) for key in sorted(desired)
        )
        text = f"{MARKER}\n- insert:\n{items}"
        _verify("", text, {}, desired, set())
        return text


# --------------------------------------------------------------------------
# emitting


_YAML_UNSAFE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x84\x86-\x9f  ﻿￾￿"
    "\ud800-\udfff]"
)


def _q(value: str) -> str:
    """A double-quoted scalar. JSON escapes are valid YAML escapes."""
    text = json.dumps(value, ensure_ascii=False)
    return _YAML_UNSAFE.sub(lambda m: f"\\u{ord(m.group(0)):04x}", text)


def _flow(value: Any) -> str:
    if isinstance(value, JsExpr):
        return f"!!js {_q(value.source)}"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        if isinstance(value, float) and value != value:  # NaN
            raise McpHubError("dsh: refusing to rewrite a NaN value")
        return json.dumps(value)
    if isinstance(value, str):
        return _q(value)
    if isinstance(value, list):
        return "[" + ", ".join(_flow(item) for item in value) + "]"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise McpHubError("dsh: refusing to rewrite a non-string key")
            parts.append(f"{_q(key)}: {_flow(item)}")
        return "{" + ", ".join(parts) + "}"
    raise McpHubError(f"dsh: cannot rewrite a {type(value).__name__} value")


def _env_out(value: Any) -> str:
    if isinstance(value, str):
        match = _VAR_RE.fullmatch(value)
        if match:
            return f"!!js process.env.{match.group(1)}"
    return _flow(value)


def _emit_item(
    key: str,
    body: dict[str, Any],
    old_entry: dict[str, Any],
    old_id: str | None,
    dash: int,
    used_ids: set[str],
) -> str:
    if not _SERVER_NAME_RE.fullmatch(key):
        raise McpHubError(f"{key}: dsh serverName must be at most 32 characters")
    transport = body.get("transport")
    if transport not in {"stdio", "streamable-http"}:
        raise McpHubError(f"{key}: dsh transport must be stdio or streamable-http")
    old_config = old_entry.get("config") if isinstance(old_entry, dict) else None
    old_config = old_config if isinstance(old_config, dict) else {}
    dropped = _HTTP_ONLY if transport == "stdio" else _STDIO_ONLY
    extras: dict[str, Any] = {
        k: v
        for k, v in old_config.items()
        if k not in _MANAGED_CONFIG and k not in dropped
    }
    item_extras: dict[str, Any] = {
        k: v for k, v in (old_entry or {}).items() if k not in _ITEM_META
    }
    for k, v in body.items():
        if k in _MANAGED_CONFIG or k == "enabled":
            continue
        if k in old_config or k not in item_extras:
            if k not in dropped:
                extras[k] = v
        else:
            item_extras[k] = v
    if old_id is None:
        base = f"{key}-mcp"
        candidate = base
        counter = 2
        while candidate in used_ids:
            candidate = f"{base}-{counter}"
            counter += 1
        old_id = candidate
    used_ids.add(old_id)
    pad = " " * dash
    inner = " " * (dash + 2)
    cfg = " " * (dash + 4)
    lines = [f"{pad}- id: {_q(old_id)}", f"{inner}name: {_q(PLUGIN)}"]
    enabled = body.get("enabled", True)
    old_disabled = (old_entry or {}).get("disabled")
    if enabled is True and isinstance(old_disabled, JsExpr):
        # Keep the user's own condition while the hub wants the server on.
        enabled = old_disabled
    if enabled is False:
        lines.append(f"{inner}disabled: true")
    elif isinstance(enabled, JsExpr):
        lines.append(f"{inner}disabled: {_flow(enabled)}")
    for k, v in item_extras.items():
        lines.append(f"{inner}{_q(k)}: {_flow(v)}")
    lines.append(f"{inner}config:")
    lines.append(f"{cfg}serverName: {_q(key)}")
    lines.append(f"{cfg}transport: {_q(transport)}")
    if transport == "stdio":
        lines.append(f"{cfg}command: {_q(str(body.get('command', '')))}")
        lines.append(f"{cfg}args: {_flow(list(body.get('args') or []))}")
        env = body.get("env") or {}
        old_env = old_config.get("env") if isinstance(old_config.get("env"), dict) else {}
        if env:
            lines.append(f"{cfg}env:")
            for env_key, env_value in env.items():
                previous = old_env.get(env_key)
                if isinstance(previous, JsExpr) and isinstance(env_value, str):
                    # A hand-written expression (with a default, say) stays.
                    rendered = _flow(previous)
                else:
                    rendered = _env_out(env_value)
                lines.append(f"{cfg}  {_q(str(env_key))}: {rendered}")
    else:
        lines.append(f"{cfg}url: {_q(str(body.get('url', '')))}")
    for k, v in extras.items():
        lines.append(f"{cfg}{_q(k)}: {_flow(v)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# locating entries in the source


@dataclass
class _Located:
    key: str
    item: yaml.MappingNode
    seq: yaml.SequenceNode
    seq_key: yaml.ScalarNode
    op_index: int


def _compose(text: str) -> yaml.Node | None:
    try:
        for event in yaml.parse(text, Loader=_Loader):
            if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None):
                raise McpHubError("dsh: refusing to edit a file with YAML anchors or aliases")
        return yaml.compose(text, Loader=_Loader)
    except yaml.YAMLError as exc:
        raise McpHubError(f"dsh cordis.patch.yml is not valid YAML: {_mark(exc)}") from exc


def _scalar(node: yaml.Node | None) -> str | None:
    if isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:str":
        return str(node.value)
    return None


def _get(mapping: yaml.MappingNode, name: str) -> tuple[yaml.Node, yaml.Node] | None:
    for key, value in mapping.value:
        if _scalar(key) == name:
            return key, value
    return None


def _locate(root: yaml.Node | None) -> list[_Located]:
    found: list[_Located] = []
    if not isinstance(root, yaml.SequenceNode):
        return found

    def visit(seq_key: yaml.Node, seq: yaml.Node, op_index: int) -> None:
        if not isinstance(seq, yaml.SequenceNode):
            return
        for item in seq.value:
            if not isinstance(item, yaml.MappingNode):
                continue
            name = _get(item, "name")
            config = _get(item, "config")
            group = _get(item, "group")
            if group is not None and config is not None:
                visit(config[0], config[1], op_index)
            if name is None or _scalar(name[1]) != PLUGIN or config is None:
                continue
            if not isinstance(config[1], yaml.MappingNode):
                continue
            server = _get(config[1], "serverName")
            key = _scalar(server[1]) if server else None
            if key is None:
                continue
            found.append(_Located(key, item, seq, seq_key, op_index))  # type: ignore[arg-type]

    for index, op in enumerate(root.value):
        if isinstance(op, yaml.MappingNode):
            insert = _get(op, "insert")
            if insert is not None:
                visit(insert[0], insert[1], index)
    return found


def _end_index(node: yaml.Node) -> int:
    if isinstance(node, yaml.ScalarNode) or getattr(node, "flow_style", False):
        return node.end_mark.index
    best = node.start_mark.index
    if isinstance(node, yaml.MappingNode):
        for key, value in node.value:
            best = max(best, _end_index(key), _end_index(value))
    elif isinstance(node, yaml.SequenceNode):
        for item in node.value:
            best = max(best, _end_index(item))
    return best


def _line_after(text: str, index: int) -> int:
    """Offset just past the line holding ``text[index - 1]``."""
    if index <= 0:
        return 0
    stop = text.find("\n", index - 1)
    return len(text) if stop < 0 else stop + 1


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _item_span(text: str, item: yaml.Node) -> tuple[int, int, int]:
    """``(start, end, dash_column)`` of a block sequence entry, whole lines."""
    pos = item.start_mark.index - 1
    while pos >= 0 and text[pos] in " \t\r\n":
        pos -= 1
    if pos < 0 or text[pos] != "-":
        raise McpHubError("dsh: could not locate an MCP entry surgically; refusing to edit")
    start = _line_start(text, pos)
    if text[start:pos].strip(" "):
        raise McpHubError("dsh: could not locate an MCP entry surgically; refusing to edit")
    return start, _line_after(text, _end_index(item)), pos - start


def _block(node: yaml.Node) -> bool:
    return not getattr(node, "flow_style", False)


# --------------------------------------------------------------------------
# editing


def _edit(original: str, rewrite: dict[str, dict[str, Any]], remove: set[str]) -> str:
    before, ops, dupes = _parse(original)
    touched = (set(rewrite) & set(before)) | (remove & set(before))
    clash = touched & dupes
    if clash:
        raise McpHubError(
            f"dsh: serverName {sorted(clash)[0]} appears more than once; refusing to edit"
        )
    root = _compose(original)
    located = {loc.key: loc for loc in _locate(root)}
    entries = {
        key: entry
        for entry in _mcp_entries(ops)
        if (key := _entry_key(entry)) is not None and key not in dupes
    }
    used_ids = _all_ids(ops)
    splices: list[tuple[int, int, str]] = []
    emptied: dict[int, list[_Located]] = {}
    for key in sorted(touched):
        loc = located.get(key)
        if loc is None:
            raise McpHubError(f"dsh: could not locate {key}; refusing to edit")
        if not (_block(loc.seq) and _block(loc.item)):
            raise McpHubError("dsh: refusing to edit a flow-style insert list")
        start, end, dash = _item_span(original, loc.item)
        if key in remove:
            splices.append((start, end, ""))
            emptied.setdefault(id(loc.seq), []).append(loc)
            continue
        old_id_node = _get(loc.item, "id")
        old_id = _scalar(old_id_node[1]) if old_id_node else None
        text = _emit_item(key, rewrite[key], entries.get(key, {}), old_id, dash, used_ids)
        splices.append((start, end, text))
    # A list whose entries are all removed keeps an explicit empty list, or the
    # hub's own op goes away entirely.
    hub_op = _hub_op(original, root)
    for group in emptied.values():
        seq = group[0].seq
        if len(group) != len(seq.value):
            continue
        if hub_op is not None and group[0].op_index == hub_op[0] and seq is hub_op[2]:
            splices = [s for s in splices if not _inside(s, hub_op[1])]
            splices.append((hub_op[1][0], hub_op[1][1], ""))
            continue
        last_start, last_end = max(
            (_item_span(original, loc.item)[:2] for loc in group), key=lambda s: s[0]
        )
        indent = max(_item_span(original, group[0].item)[2], group[0].seq_key.start_mark.column + 2)
        splices = [s for s in splices if s[0] != last_start]
        splices.append((last_start, last_end, " " * indent + "[]\n"))
    new_keys = sorted(k for k in rewrite if k not in before)
    text = _splice(original, splices)
    if new_keys:
        text = _append(original, text, root, hub_op, splices, new_keys, rewrite, used_ids)
    text = _keep_list(text)
    if "\r\n" in original and not re.search(r"(?<!\r)\n", original):
        # A CRLF file stays CRLF: only the lines the hub wrote need it.
        text = re.sub(r"(?<!\r)\n", "\r\n", text)
    _verify(original, text, before, rewrite, remove)
    return text


def _inside(splice: tuple[int, int, str], span: tuple[int, int]) -> bool:
    return span[0] <= splice[0] and splice[1] <= span[1]


def _splice(text: str, splices: list[tuple[int, int, str]]) -> str:
    out = text
    last = len(text) + 1
    for start, end, repl in sorted(splices, key=lambda s: s[0], reverse=True):
        if end > last:
            raise McpHubError("dsh: overlapping edits; refusing to edit")
        out = out[:start] + repl + out[end:]
        last = start
    return out


def _all_ids(ops: list[Any]) -> set[str]:
    ids: set[str] = set()
    for op in ops:
        if not isinstance(op, dict):
            continue
        if isinstance(op.get("id"), str):
            ids.add(op["id"])
        for entry in _walk_entries(op.get("insert")):
            if isinstance(entry.get("id"), str):
                ids.add(entry["id"])
    return ids


def _hub_op(
    text: str, root: yaml.Node | None
) -> tuple[int, tuple[int, int], yaml.SequenceNode] | None:
    """The hub-appended op: last root op, ``insert`` only, after the marker."""
    if not isinstance(root, yaml.SequenceNode) or not _block(root) or not root.value:
        return None
    op = root.value[-1]
    if not isinstance(op, yaml.MappingNode) or not _block(op) or len(op.value) != 1:
        return None
    insert = _get(op, "insert")
    if insert is None or not isinstance(insert[1], yaml.SequenceNode):
        return None
    if not _block(insert[1]) or not insert[1].value:
        return None
    start, end, _dash = _item_span(text, op)
    marker_start = _line_start(text, max(start - 1, 0))
    if start == 0 or text[marker_start:start].strip() != MARKER:
        return None
    return len(root.value) - 1, (marker_start, end), insert[1]


def _append(
    original: str,
    text: str,
    root: yaml.Node | None,
    hub_op: tuple[int, tuple[int, int], yaml.SequenceNode] | None,
    splices: list[tuple[int, int, str]],
    new_keys: list[str],
    rewrite: dict[str, dict[str, Any]],
    used_ids: set[str],
) -> str:
    hub_removed = hub_op is not None and any(
        (s[0], s[1]) == hub_op[1] and s[2] == "" for s in splices
    )
    if hub_op is not None and not hub_removed:
        first = hub_op[2].value[0]
        dash = _item_span(original, first)[2]
        items = "".join(_emit_item(k, rewrite[k], {}, None, dash, used_ids) for k in new_keys)
        # Insert at the hub op's end, measured in the spliced text.
        end = hub_op[1][1]
        shift = sum(len(r) - (e - s) for s, e, r in splices if e <= end)
        at = end + shift
        prefix = text[:at]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        return prefix + items + text[at:]
    root_dash = 0
    if isinstance(root, yaml.SequenceNode) and root.value:
        if not _block(root):
            raise McpHubError("dsh: refusing to edit a flow-style top-level list")
        root_dash = _item_span(original, root.value[0])[2]
    elif isinstance(root, yaml.SequenceNode) and not _block(root) and not splices:
        # ``[]``: drop the empty flow list; the appended op replaces it.
        text = text[: root.start_mark.index] + text[root.end_mark.index :]
        if not text.strip():
            text = ""
    items = "".join(
        _emit_item(k, rewrite[k], {}, None, root_dash + 4, used_ids) for k in new_keys
    )
    if text and not text.endswith("\n"):
        text += "\n"
    return text + f"{' ' * root_dash}{MARKER}\n{' ' * root_dash}- insert:\n" + items


def _keep_list(text: str) -> str:
    """Never leave a file that loads to nothing: dsh rejects a non-array."""
    try:
        data = _load(text)
    except McpHubError:
        return text
    if data is None:
        if text and not text.endswith("\n"):
            text += "\n"
        return text + "[]\n"
    return text


def _canonical(ops: list[Any], touched: set[str]) -> list[Any]:
    def strip(entries: Any) -> Any:
        if not isinstance(entries, list):
            return entries
        kept = []
        for entry in entries:
            if isinstance(entry, dict) and _is_mcp(entry) and _entry_key(entry) in touched:
                continue
            if isinstance(entry, dict) and entry.get("group") and isinstance(entry.get("config"), list):
                entry = dict(entry, config=strip(entry["config"]))
            kept.append(entry)
        return kept

    out = []
    for op in ops:
        if isinstance(op, dict) and "insert" in op:
            op = dict(op, insert=strip(op["insert"]))
            if set(op) == {"insert"} and not op["insert"]:
                continue
        out.append(op)
    return out


def _verify(
    original: str,
    text: str,
    before: dict[str, dict[str, Any]],
    rewrite: dict[str, dict[str, Any]],
    remove: set[str],
) -> None:
    data = _load(text)
    if not isinstance(data, list):
        raise McpHubError("dsh: edit would not leave a top-level array; refusing")
    after, ops, _dupes = _parse(text)
    codec = DshCodec()
    for key, body in rewrite.items():
        got = after.get(key)
        if got is None or not codec.matches(got, body):
            raise McpHubError(f"dsh: edit did not round-trip {key}; refusing")
    for key in remove:
        if key in after:
            raise McpHubError(f"dsh: edit did not remove {key}; refusing")
    for key, body in before.items():
        if key in rewrite or key in remove:
            continue
        if after.get(key) != body:
            raise McpHubError(f"dsh: edit changed unmanaged {key}; refusing")
    if set(after) != (set(before) - remove) | set(rewrite):
        raise McpHubError("dsh: edit changed the set of servers; refusing")
    touched = set(rewrite) | remove
    old_ops = _root_list(_load(original)) if original else []
    if _canonical(old_ops, touched) != _canonical(ops, touched):
        raise McpHubError("dsh: edit changed unmanaged patch ops; refusing")


SPEC = AgentSpec(
    agent="dsh",
    relative=(".dsh", "cordis.patch.yml"),
    fmt="codec",
    container=(),
    style="dsh",
    native=native,
    cannot=cannot,
    codec=DshCodec(),
    key_pattern=_SERVER_NAME_RE,
)
