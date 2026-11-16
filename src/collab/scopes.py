"""Scopes: the one authorization table every collab front end shares.

A registered app starts with ``read`` only. Every write-shaped tool needs its
own scope, so opening one capability never opens another. The table is
intentionally small and explicit; a new tool must name its scope here before
any route or MCP tool can accept it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.collab.types import ScopeDeniedError

#: Every scope a registration may carry.
SCOPES = ("read", "propose", "claim", "report", "ask", "review")
SCOPE_SET = frozenset(SCOPES)

#: Read-only tools are safe for every app and are the default grant.
DEFAULT_SCOPES = frozenset({"read"})
READ_SCOPES = frozenset({"read"})
WRITE_SCOPES = frozenset({"propose", "claim", "report", "ask", "review"})

#: Tool -> required scope. Keys are the MCP/CLI/HTTP tool names from N80 4.1.
TOOL_SCOPES: dict[str, str] = {
    "whoami": "read",
    "task_list": "read",
    "task_get": "read",
    "handoff_list": "read",
    "agent_inbox": "read",
    "review_list": "read",
    "task_create": "propose",
    "task_claim": "claim",
    "task_heartbeat": "claim",
    "task_release": "claim",
    "handoff_request": "claim",
    "handoff_accept": "claim",
    "task_report": "report",
    "review_request": "report",
    "agent_ask": "ask",
    "review_submit": "review",
}

#: The reverse view, useful for help text and the app registration listing.
SCOPE_TOOLS: dict[str, tuple[str, ...]] = {
    scope: tuple(sorted(tool for tool, needed in TOOL_SCOPES.items() if needed == scope))
    for scope in SCOPES
}


def normalize_scopes(value: Any) -> frozenset[str]:
    """Canonicalize a grant and reject an unknown scope instead of ignoring it.

    Accepts ``None`` (the read default), a comma/space separated string, or an
    iterable. The result is a set so callers cannot accidentally apply list
    ordering as policy.
    """
    if value is None:
        return DEFAULT_SCOPES
    if isinstance(value, str):
        parts = [part for part in value.replace(",", " ").split() if part]
    elif isinstance(value, Iterable):
        parts = [str(part).strip() for part in value if str(part).strip()]
    else:
        raise ValueError("scopes must be a string or an iterable of scope names")
    normalized = {part.lower() for part in parts}
    unknown = normalized - SCOPE_SET
    if unknown:
        raise ValueError(f"unknown collaboration scope: {', '.join(sorted(unknown))}")
    return frozenset(normalized)


def parse_scopes(value: Any) -> frozenset[str]:
    return normalize_scopes(value)


def scope_for_tool(tool: str) -> str:
    """Raise for an unknown tool rather than guessing a permissive default."""
    try:
        return TOOL_SCOPES[str(tool)]
    except KeyError as exc:
        raise ValueError(f"unknown collaboration tool: {tool}") from exc


def has_scope(scopes: Any, tool_or_scope: str) -> bool:
    """True when the grant contains the scope a tool needs."""
    required = TOOL_SCOPES.get(str(tool_or_scope), str(tool_or_scope))
    try:
        granted = normalize_scopes(scopes)
    except ValueError:
        return False
    return required in granted


def scope_allows(scopes: Any, tool_or_scope: str) -> bool:
    return has_scope(scopes, tool_or_scope)


def require_scope(scopes: Any, tool_or_scope: str) -> None:
    required = TOOL_SCOPES.get(str(tool_or_scope), str(tool_or_scope))
    if not has_scope(scopes, required):
        raise ScopeDeniedError(
            f"the app token does not grant the {required} scope",
            details={"required_scope": required},
        )


def tools_for_scope(scope: str) -> tuple[str, ...]:
    return SCOPE_TOOLS.get(scope, ())


def can_write(scopes: Any) -> bool:
    """A grant that contains any write-shaped scope, not a capability judgment."""
    try:
        return bool(normalize_scopes(scopes) & WRITE_SCOPES)
    except ValueError:
        return False


__all__ = [
    "DEFAULT_SCOPES",
    "READ_SCOPES",
    "SCOPES",
    "SCOPE_SET",
    "SCOPE_TOOLS",
    "TOOL_SCOPES",
    "WRITE_SCOPES",
    "can_write",
    "has_scope",
    "normalize_scopes",
    "parse_scopes",
    "require_scope",
    "scope_allows",
    "scope_for_tool",
    "tools_for_scope",
]
