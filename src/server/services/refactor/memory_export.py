"""Owner-scoped JSONL export from the canonical LangGraph Store."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

import yaml

from src.server.services.store_backends.langgraph_store import store_label, user_memory_namespace
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.services.refactor.memory_projection import POINTER_RE

MAX_EXPORT_PAGE_SIZE = 200
MAX_EXPORT_SCAN_PER_PAGE = 1_000
MAX_EXPORT_CURSOR_OFFSET = 1_000_000
MAX_INDEX_POINTER_CHECKS = 32
STORE_READ_TIMEOUT_SECONDS = 2.0
_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(?:(?P<metadata>.*?)\r?\n)?---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_POINTER_RE = POINTER_RE
_POINTER_START_RE = re.compile(r"^\s*-\s*\[")


class MemoryExportError(ValueError):
    """Invalid owner export request or Store response."""


class MemoryExportInputError(MemoryExportError):
    """Invalid client-supplied owner export parameters."""


def _cursor_position(cursor: str | None) -> tuple[int, int]:
    if cursor is None or cursor == "":
        return 0, 0
    try:
        scope_text, offset_text = cursor.split(":", 1)
        scope, offset = int(scope_text), int(offset_text)
    except (AttributeError, ValueError):
        raise MemoryExportInputError("invalid memory export cursor") from None
    if scope not in (0, 1, 2) or not 0 <= offset <= MAX_EXPORT_CURSOR_OFFSET:
        raise MemoryExportInputError("invalid memory export cursor")
    return scope, offset


def _content(item: Any) -> str | None:
    value = getattr(item, "value", None)
    if not isinstance(value, Mapping):
        return None
    raw = value.get("content")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list) and all(isinstance(line, str) for line in raw):
        return "\n".join(raw)
    return None


def _policy(content: str) -> dict[str, Any]:
    if not content.startswith("---"):
        return {"sensitivity": "private"}
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        return {"sensitivity": "secret"}
    try:
        metadata = yaml.safe_load(match.group("metadata") or "") or {}
    except yaml.YAMLError:
        return {"sensitivity": "secret"}
    if not isinstance(metadata, Mapping):
        return {"sensitivity": "secret"}
    return normalize_memory_policy_metadata_layers(metadata)


async def _safe_index_content(
    store: Any, namespace: tuple[str, ...], content: str
) -> tuple[str, int]:
    """Remove pointers that cannot be proven not to name secret detail files."""
    lines = content.splitlines()
    matches = [
        (index, match.group("path").removeprefix("./"))
        for index, line in enumerate(lines)
        if (match := _POINTER_RE.match(line))
    ]
    checked = matches[:MAX_INDEX_POINTER_CHECKS]

    async def check(path: str) -> tuple[str, bool]:
        if (
            not path
            or path.startswith(("/", "proposed/", "http://", "https://", "#"))
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            return path, False
        try:
            item = await asyncio.wait_for(
                store.aget(namespace, path), timeout=STORE_READ_TIMEOUT_SECONDS
            )
        except Exception:  # noqa: BLE001 - unverifiable pointers are omitted
            return path, False
        detail = _content(item)
        return path, bool(detail and _policy(detail)["sensitivity"] != "secret")

    results = await asyncio.gather(*(check(path) for _, path in checked))
    safe_paths = {path for path, safe in results if safe}
    checked_lines = {index for index, _ in checked}
    output = []
    redacted_pointer_count = 0
    for index, line in enumerate(lines):
        if index in checked_lines:
            match = _POINTER_RE.match(line)
            if match and match.group("path").removeprefix("./") in safe_paths:
                output.append(line)
            else:
                redacted_pointer_count += 1
        elif line.strip() in ("", "# Memory"):
            output.append(line)
        elif _POINTER_START_RE.match(line):
            # Never expose pointers outside the bounded detail verification set.
            redacted_pointer_count += 1
    result = "\n".join(output)
    if content.endswith("\n") and result:
        result += "\n"
    return result, redacted_pointer_count


def _scope(namespace: tuple[str, ...], owner: str) -> dict[str, Any] | None:
    if namespace == (owner, "memory"):
        return {"tier": "user"}
    if len(namespace) == 4 and namespace[0] == owner and namespace[-1] == "memory":
        if namespace[1] == "workspaces":
            return {"tier": "workspace", "workspace_id": namespace[2]}
        if namespace[1] == "threads":
            return {"tier": "session", "thread_id": namespace[2]}
    return None


async def export_memory_page(
    *,
    store: Any,
    user_id: str,
    limit: int = 100,
    cursor: str | None = None,
    include_secret: bool = False,
) -> dict[str, Any]:
    """Return one bounded owner page with original Store content and scope."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise MemoryExportInputError("memory export needs a non-empty owner")
    if store is None:
        raise MemoryExportError("long-term Store is unavailable")
    include_secret = include_secret is True
    try:
        page_size = max(1, min(int(limit), MAX_EXPORT_PAGE_SIZE))
    except (TypeError, ValueError):
        raise MemoryExportInputError("memory export limit must be an integer") from None
    start_scope, offset = _cursor_position(cursor)
    owner = store_label(user_id)
    prefixes = (
        user_memory_namespace(user_id),
        (owner, "workspaces"),
        (owner, "threads"),
    )
    records: list[dict[str, Any]] = []
    scanned = 0
    scope = start_scope

    while scope < len(prefixes) and len(records) < page_size and scanned < MAX_EXPORT_SCAN_PER_PAGE:
        raw_limit = min(page_size - len(records), MAX_EXPORT_SCAN_PER_PAGE - scanned)
        try:
            items = await asyncio.wait_for(
                store.asearch(prefixes[scope], limit=raw_limit, offset=offset),
                timeout=STORE_READ_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - surface bounded Store failure
            raise MemoryExportError("could not read the canonical memory Store") from exc
        if not isinstance(items, list):
            raise MemoryExportError("memory Store returned an invalid page")
        scanned += len(items)
        offset += len(items)
        for item in items:
            namespace = tuple(str(part) for part in (getattr(item, "namespace", ()) or ()))
            scope_metadata = _scope(namespace, owner)
            key = getattr(item, "key", None)
            content = _content(item)
            if scope_metadata is None or not isinstance(key, str) or content is None:
                continue
            policy = _policy(content)
            if policy["sensitivity"] == "secret" and not include_secret:
                continue
            redacted_pointer_count = 0
            if key == "memory.md" and not include_secret:
                content, redacted_pointer_count = await _safe_index_content(
                    store, namespace, content
                )
            updated_at = getattr(item, "updated_at", None)
            record = {
                "namespace": list(namespace),
                "key": key,
                "content": content,
                "metadata": policy,
                "scope": scope_metadata,
                "updated_at": (
                    updated_at.isoformat()
                    if isinstance(updated_at, datetime)
                    else str(updated_at) if updated_at is not None else None
                ),
            }
            if redacted_pointer_count:
                record["redacted"] = True
                record["redacted_pointer_count"] = redacted_pointer_count
            records.append(record)
        if len(items) < raw_limit:
            scope += 1
            offset = 0

    next_cursor = None
    if scope < len(prefixes):
        next_cursor = f"{scope}:{offset}"
    return {"items": records, "next_cursor": next_cursor}
