"""Pure types and validation for owner-approved tool-denial snapshots."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Literal

_MAX_DENIALS = 64
_MAX_IDENTIFIER = 128
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
EXECUTION_SURFACE_TOOLS = frozenset(
    {"ExecuteCode", "Bash", "BashOutput", "NativeAgent", "NativeAgents"}
)
MEMORY_ENTRY_KINDS = frozenset(
    {"preference", "constraint", "thesis", "decision", "fact", "entity", "event"}
)


class MemoryToolPolicyError(ValueError):
    """A policy cannot be safely interpreted as exact tool identifiers."""


@dataclass(frozen=True, slots=True)
class ToolIdentifier:
    """An exact registered tool name or an exact MCP server/tool pair."""

    type: Literal["tool", "mcp"]
    name: str | None = None
    server: str | None = None
    tool: str | None = None

    def __post_init__(self) -> None:
        if self.type == "tool":
            if self.name is None or not _valid_identifier(self.name):
                raise MemoryToolPolicyError("tool identifier requires a valid exact name")
            if self.server is not None or self.tool is not None:
                raise MemoryToolPolicyError("tool identifier has unexpected MCP fields")
        elif self.type == "mcp":
            if (
                self.name is not None
                or self.server is None
                or self.tool is None
                or not _valid_identifier(self.server)
                or not _valid_identifier(self.tool)
            ):
                raise MemoryToolPolicyError("MCP identifier requires exact server and tool names")
        else:
            raise MemoryToolPolicyError("unknown tool identifier type")


def _valid_identifier(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_IDENTIFIER
        and _IDENTIFIER_RE.fullmatch(value) is not None
    )


def normalize_denied_tools(value: Any) -> tuple[ToolIdentifier, ...]:
    """Validate an explicit list; prose, aliases, and unknown shapes fail closed."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise MemoryToolPolicyError("denied_tools must be a list")
    if len(value) > _MAX_DENIALS:
        raise MemoryToolPolicyError("denied_tools exceeds the maximum size")

    identifiers: list[ToolIdentifier] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise MemoryToolPolicyError("each denied tool must be a typed object")
        kind = raw.get("type")
        if kind == "tool" and set(raw) == {"type", "name"}:
            identifier = ToolIdentifier(type="tool", name=raw["name"])
            key = ("tool", str(identifier.name), "")
        elif kind == "mcp" and set(raw) == {"type", "server", "tool"}:
            identifier = ToolIdentifier(
                type="mcp", server=raw["server"], tool=raw["tool"]
            )
            key = ("mcp", str(identifier.server), str(identifier.tool))
        else:
            raise MemoryToolPolicyError("denied tool has an invalid typed shape")
        if key not in seen:
            identifiers.append(identifier)
            seen.add(key)
    return tuple(identifiers)


def denied_tools_as_json(value: Any) -> list[dict[str, str]]:
    """Return the canonical JSON representation used by the approval table."""
    result: list[dict[str, str]] = []
    for item in normalize_denied_tools(value):
        if item.type == "tool":
            result.append({"type": "tool", "name": str(item.name)})
        else:
            result.append(
                {"type": "mcp", "server": str(item.server), "tool": str(item.tool)}
            )
    return result


def normalize_tool_policy_metadata(value: Any) -> dict[str, Any]:
    """Validate typed tool policy fields carried by a memory proposal.

    Policy fields may be nested in the same metadata wrappers used by memory
    records. Conflicting layers fail closed instead of letting an outer value
    silently replace an inner approval candidate.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise MemoryToolPolicyError("memory tool-policy metadata must be an object")
    occurrences: dict[str, list[Any]] = {
        field: [] for field in ("kind", "denied_tools", "valid_from", "valid_until")
    }
    pending: list[tuple[Mapping[str, Any], int]] = [(value, 0)]
    seen: set[int] = set()
    while pending:
        layer, depth = pending.pop()
        if id(layer) in seen:
            continue
        seen.add(id(layer))
        for field in occurrences:
            if field in layer:
                occurrences[field].append(layer[field])
        if depth >= 8:
            if "metadata" in layer or "memory_metadata" in layer:
                raise MemoryToolPolicyError("memory tool-policy metadata is nested too deeply")
            continue
        for wrapper in ("metadata", "memory_metadata"):
            nested = layer.get(wrapper)
            if nested is not None:
                if not isinstance(nested, Mapping):
                    raise MemoryToolPolicyError(f"{wrapper} must be an object")
                pending.append((nested, depth + 1))

    result: dict[str, Any] = {}
    resolved: dict[str, Any] = {}
    for field, values in occurrences.items():
        if not values:
            continue
        if field == "denied_tools":
            normalized_values = [denied_tools_as_json(item) for item in values]
        elif field in ("valid_from", "valid_until"):
            normalized_values = [normalize_validity_bound(item, field) for item in values]
        else:
            normalized_values = values
        canonical = {
            json.dumps(item, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            for item in normalized_values
        }
        if len(canonical) != 1:
            raise MemoryToolPolicyError(f"conflicting {field} values across metadata layers")
        resolved[field] = normalized_values[0]

    kind = resolved.get("kind")
    if kind is not None and (not isinstance(kind, str) or kind not in MEMORY_ENTRY_KINDS):
        raise MemoryToolPolicyError("kind is not a recognized memory entry kind")
    if "denied_tools" in resolved and kind != "constraint":
        raise MemoryToolPolicyError("denied_tools requires kind=constraint")
    if kind is not None:
        result["kind"] = kind
    if kind == "constraint":
        result["denied_tools"] = resolved.get("denied_tools", [])
    elif "denied_tools" in resolved:
        result["denied_tools"] = resolved["denied_tools"]
    for field in ("valid_from", "valid_until"):
        if field in resolved:
            result[field] = resolved[field]
    if "valid_from" in result and "valid_until" in result:
        normalize_validity_window(result["valid_from"], result["valid_until"])
    return result


def normalize_memory_tool_policy_metadata(value: Any) -> dict[str, Any]:
    """Named proposal-metadata entry point for exact tool-policy fields."""
    return normalize_tool_policy_metadata(value)


def normalize_validity_bound(value: Any, field: str) -> str | None:
    """Parse an approved validity bound into a bounded UTC timestamp."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, str) and len(value) <= 40:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MemoryToolPolicyError(f"{field} is not a valid ISO timestamp") from exc
    else:
        raise MemoryToolPolicyError(f"{field} is not a bounded ISO timestamp")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed.year < 1970 or parsed.year > 2200:
        raise MemoryToolPolicyError(f"{field} is outside the supported time range")
    return parsed.isoformat()


def normalize_validity_window(valid_from: Any, valid_until: Any) -> tuple[str | None, str | None]:
    """Normalize the validity captured by the owner approval decision."""
    start = normalize_validity_bound(valid_from, "valid_from")
    end = normalize_validity_bound(valid_until, "valid_until")
    if start and end and datetime.fromisoformat(start) >= datetime.fromisoformat(end):
        raise MemoryToolPolicyError("valid_from must be earlier than valid_until")
    return start, end


@dataclass(frozen=True, slots=True)
class MemoryToolPolicySnapshot:
    """Immutable policy attached to one main-agent/subagent execution tree."""

    denied_tools: tuple[ToolIdentifier, ...] = ()
    source_entry_ids: tuple[str, ...] = ()
    integrity_mismatch_entry_ids: tuple[str, ...] = ()
    digest: str = ""
    deny_all: bool = False

    @property
    def blocks_code_execution(self) -> bool:
        # PTC wrappers, arbitrary shell programs, and delegated native CLIs
        # execute below the policy boundary. Close them until their transports
        # can enforce this snapshot directly.
        return self.deny_all or bool(self.denied_tools)

    @property
    def native_runtime_allowed(self) -> bool:
        """Native providers lack this gate and must be bypassed when uncertain."""
        return not self.deny_all and not self.denied_tools

    def blocks(self, tool_name: str, stamp: Mapping[str, Any] | None = None) -> bool:
        if self.deny_all:
            return True
        if self.blocks_code_execution and tool_name in EXECUTION_SURFACE_TOOLS:
            return True
        for identifier in self.denied_tools:
            if identifier.type == "tool" and identifier.name == tool_name:
                return True
            if identifier.type == "mcp" and stamp is not None:
                if (
                    stamp.get("server") == identifier.server
                    and stamp.get("tool") == identifier.tool
                ):
                    return True
        return False


def _digest(rows: list[dict[str, Any]]) -> str:
    canonical = json.dumps(rows, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_from_authority_rows(rows: Any) -> MemoryToolPolicySnapshot:
    """Freeze and validate every active authority row read for this turn."""
    try:
        if not isinstance(rows, list):
            raise ValueError("tool policy source returned an invalid result")
        normalized_rows: list[dict[str, Any]] = []
        identifiers: list[ToolIdentifier] = []
        entry_ids: list[str] = []
        integrity_mismatch_ids: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("entry_id"), str):
                raise ValueError("tool policy source returned a malformed row")
            valid_from, valid_until = normalize_validity_window(
                row.get("valid_from"), row.get("valid_until")
            )
            now = datetime.now(timezone.utc)
            if (
                valid_from is not None
                and now < datetime.fromisoformat(valid_from)
            ) or (
                valid_until is not None
                and now >= datetime.fromisoformat(valid_until)
            ):
                continue
            row_ids = normalize_denied_tools(row.get("denied_tools"))
            identifiers.extend(row_ids)
            entry_ids.append(row["entry_id"])
            mismatch = row.get("integrity_mismatch") is True
            if mismatch:
                integrity_mismatch_ids.append(row["entry_id"])
            normalized_rows.append(
                {
                    "entry_id": row["entry_id"],
                    "valid_from": valid_from,
                    "valid_until": valid_until,
                    "integrity_mismatch": mismatch,
                    "denied_tools": [
                        {"type": i.type, "name": i.name}
                        if i.type == "tool"
                        else {"type": i.type, "server": i.server, "tool": i.tool}
                        for i in row_ids
                    ],
                }
            )
        unique: dict[tuple[str, str, str], ToolIdentifier] = {}
        for item in identifiers:
            key = (item.type, item.name or item.server or "", item.tool or "")
            unique[key] = item
        return MemoryToolPolicySnapshot(
            denied_tools=tuple(unique.values()),
            source_entry_ids=tuple(sorted(set(entry_ids))),
            integrity_mismatch_entry_ids=tuple(sorted(set(integrity_mismatch_ids))),
            digest=_digest(normalized_rows),
        )
    except Exception:
        return MemoryToolPolicySnapshot(deny_all=True, digest="load-error")
