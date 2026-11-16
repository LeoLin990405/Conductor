"""The read-only profile of ``conductor-collab-mcp`` (N80 L3, DSH round).

``conductor-collab-mcp --app <app_id> --read-only`` serves only the N80 tools
whose scope is ``read`` plus one synthesized tool, ``superagent_context``.
It is app-neutral: any registered app can run it. The first user is DeepSeek
Harness (DSH), which keeps one Super-agent session whose context Conductor
owns (``docs/plans/next/ref-dsh-frontend-spike.md``). Set-up for DSH:

1. ``conductor apps register dsh --scopes read`` (read only; the token goes to
   the keychain entry ``conductor-collab/dsh``, never to a file).
2. Point the DSH MCP row ``conductor`` at
   ``conductor-collab-mcp --app dsh --read-only`` (stdio).

Guarantees of this profile, on top of the server's own checks:

* every tool outside :data:`READ_ONLY_TOOLS` is refused with ``scope_denied``
  **before** any HTTP request, so a front end cannot reach a write route even
  when its token was later granted more scopes. The server's scope check stays
  the real gate; this is defense in depth;
* ``agent_inbox`` never consumes: ``peek`` is always forced to ``true`` (a
  caller's ``peek: false`` is overridden, not refused, so reading still works);
* ``superagent_context`` calls only read tools and returns one collab envelope
  with a compact snapshot. A failing section is marked unavailable with its
  code; the rest still answers. Collab mode off (``CONDUCTOR_COLLAB_MODE``
  default) shows up as that section's refusal code, never hidden.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import mcp_types as types

from mcp.server import Server
from src.collab.scopes import TOOL_SCOPES
from src.collab_mcp.client import CollabClientError
from src.collab_mcp.server import UNTRUSTED_NOTE, CollabMCP, _refusal, tool_definitions

SUPERAGENT_CONTEXT_TOOL = "superagent_context"

#: Every N80 tool with the ``read`` scope, in protocol order, plus the snapshot.
READ_ONLY_TOOLS: tuple[str, ...] = (
    "whoami",
    "task_list",
    "task_get",
    "handoff_list",
    "review_list",
    "agent_inbox",
    SUPERAGENT_CONTEXT_TOOL,
)

#: The literal prefix of this profile's server instructions. The DSH compat
#: suite checks that it reaches the model request.
INSTRUCTIONS_MARKER = "conductor-collab (read-only):"
READ_ONLY_INSTRUCTIONS = (
    f"{INSTRUCTIONS_MARKER} this server only reads Conductor state. It cannot claim, "
    "report, review, hand off or send anything; those need a person in Conductor. "
    + UNTRUSTED_NOTE
)

SERVER_NAME = "conductor-collab-readonly"

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 50

_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "limit": {"type": "integer", "minimum": 1, "maximum": _MAX_LIMIT, "default": _DEFAULT_LIMIT}
    },
}

#: Snapshot section -> (read tool, arguments). Only read tools, by construction.
_SECTIONS: tuple[tuple[str, str], ...] = (
    ("app", "whoami"),
    ("tasks", "task_list"),
    ("handoffs", "handoff_list"),
    ("reviews", "review_list"),
    ("inbox", "agent_inbox"),
)


def protocol_read_tools() -> tuple[str, ...]:
    """The N80 tools whose scope is ``read`` (the source of truth is TOOL_SCOPES)."""
    return tuple(tool for tool, scope in TOOL_SCOPES.items() if scope == "read")


def read_only_tool_definitions() -> list[types.Tool]:
    by_name = {tool.name: tool for tool in tool_definitions()}
    tools = [by_name[name] for name in READ_ONLY_TOOLS if name in by_name]
    tools.append(
        types.Tool(
            name=SUPERAGENT_CONTEXT_TOOL,
            description=(
                "A compact read-only snapshot for a Super-agent session: this app's identity "
                "and scopes, visible tasks, handoffs offered, pending reviews and unread "
                "message count. Built from read tools only; a failing section is marked "
                f"unavailable. {UNTRUSTED_NOTE}"
            ),
            input_schema=json.loads(json.dumps(_CONTEXT_SCHEMA)),
        )
    )
    return tools


def _limit(arguments: Mapping[str, Any]) -> int | None:
    value = arguments.get("limit", _DEFAULT_LIMIT)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_LIMIT:
        return None
    return value


class ReadOnlyCollabMCP(CollabMCP):
    """:class:`CollabMCP` restricted to :data:`READ_ONLY_TOOLS`."""

    def call_sync(self, tool: str, arguments: Mapping[str, Any] | None) -> dict[str, Any]:
        args = dict(arguments or {})
        if tool not in READ_ONLY_TOOLS:
            # No HTTP request, no heartbeat activity: this profile has no lease.
            return self.client.scrub(
                _refusal("scope_denied", f"{tool} is not available in the read-only profile")
            )
        if tool == SUPERAGENT_CONTEXT_TOOL:
            self.heartbeat.note_activity()
            return self.client.scrub(self._superagent_context(args))
        if tool == "agent_inbox":
            args["peek"] = True
        return super().call_sync(tool, args)

    def _section(self, tool: str, limit: int) -> dict[str, Any]:
        args: dict[str, Any] = {} if tool == "whoami" else {"limit": limit}
        if tool == "agent_inbox":
            args["peek"] = True
        try:
            envelope = super().call_sync(tool, args)
        except CollabClientError as exc:  # pragma: no cover - call_sync already wraps it
            envelope = _refusal("internal_error", str(exc))
        return envelope if isinstance(envelope, dict) else _refusal("internal_error", "bad envelope")

    def _superagent_context(self, args: Mapping[str, Any]) -> dict[str, Any]:
        if set(args) - {"limit"}:
            return _refusal("invalid_schema", "superagent_context takes only 'limit'")
        limit = _limit(args)
        if limit is None:
            return _refusal("invalid_schema", f"limit must be an integer 1..{_MAX_LIMIT}")
        data: dict[str, Any] = {"profile": "read-only"}
        untrusted: list[str] = []
        redacted = 0
        unavailable: list[str] = []
        for section, tool in _SECTIONS:
            envelope = self._section(tool, limit)
            ok = envelope.get("ok") is True and envelope.get("code") == "ok"
            redacted += int(envelope.get("redacted") or 0)
            if not ok:
                unavailable.append(section)
                data[section] = {
                    "available": False,
                    "code": envelope.get("code") or "internal_error",
                    "message": str(envelope.get("message") or "")[:300],
                }
                continue
            payload = envelope.get("data") if isinstance(envelope.get("data"), Mapping) else {}
            if section == "inbox":
                # Counts and senders only: message text stays in agent_inbox.
                messages = payload.get("messages") or []
                data[section] = {
                    "available": True,
                    "unread": len(messages),
                    "from": sorted({str(m.get("from")) for m in messages if isinstance(m, Mapping)}),
                }
                continue
            data[section] = {"available": True, **payload}
            for path in envelope.get("untrusted_fields") or []:
                text = str(path)
                rest = text[len("data."):] if text.startswith("data.") else text
                untrusted.append(f"data.{section}.{rest}")
        data["unavailable"] = unavailable
        return {
            "ok": True,
            "code": "ok",
            "data": data,
            "untrusted_fields": untrusted,
            "redacted": redacted,
        }


def build_read_only_server(core: ReadOnlyCollabMCP) -> Server[Any]:
    """The MCP server object for the read-only profile (both transports)."""
    tools = read_only_tool_definitions()

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        envelope = await core.call(params.name, params.arguments)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=json.dumps(envelope, ensure_ascii=False))],
            structured_content=envelope,
            is_error=not (envelope.get("ok") is True and envelope.get("code") == "ok"),
        )

    return Server(
        SERVER_NAME,
        version="1",
        instructions=READ_ONLY_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


__all__ = [
    "INSTRUCTIONS_MARKER",
    "READ_ONLY_INSTRUCTIONS",
    "READ_ONLY_TOOLS",
    "SUPERAGENT_CONTEXT_TOOL",
    "ReadOnlyCollabMCP",
    "build_read_only_server",
    "protocol_read_tools",
    "read_only_tool_definitions",
]
