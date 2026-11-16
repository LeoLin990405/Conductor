"""Read views over the refactor tables — and the legacy hand-off rule.

Readers are the Phase-1 "reader parity" half: services and (later) routes can ask
for a goal, an event page, a task tree or a memory index, and get a JSON-safe
view instead of raw SQL rows. They are dual-read by construction: when the
owning flag is **off** a reader returns ``None`` (or ``None``-shaped) rather than
guessing, so the caller keeps using today's SSE/store-derived path. ``None``
means "legacy path", ``[]`` means "the new path ran and found nothing" — the
distinction matters at every call site and is part of the contract.

``compare_memory_projection`` is the parity check the backfill uses: it compares
the store's entries with the projection rows and reports missing/extra/changed
keys. It is pure so it can run in the memory eval and in a backfill dry run
without a database.

Views convert volatile scalar types (datetimes, UUIDs, Decimals) to JSON-safe
values and whitelist fields; a new column does not silently reach a client.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from src.server.contracts.refactor import ContractError, MEMORY_TIERS
from src.server.database.refactor import events as db_events
from src.server.database.refactor import goals as db_goals
from src.server.database.refactor import memory as db_memory
from src.server.database.refactor import tasks as db_tasks
from src.server.services.refactor_flags import refactor_flag_enabled

_GOAL_FIELDS = (
    "goal_id",
    "thread_id",
    "workspace_id",
    "title",
    "objective",
    "stop_conditions",
    "status",
    "budget",
    "verifier_config",
    "verifier_verdict",
    "created_at",
    "updated_at",
    "completed_at",
)
_PLAN_FIELDS = (
    "plan_id",
    "version",
    "steps",
    "status",
    "feedback",
    "created_at",
    "decided_at",
)
_EVENT_FIELDS = (
    "event_id",
    "thread_id",
    "response_id",
    "task_run_id",
    "seq",
    "type",
    "payload",
    "created_at",
)
_TASK_FIELDS = (
    "task_node_id",
    "parent_node_id",
    "title",
    "status",
    "source",
    "subagent_task_ref",
    "created_at",
    "updated_at",
)
_MEMORY_FIELDS = (
    "entry_id",
    "tier",
    "entry_key",
    "index_text",
    "status",
    "provenance",
    "source_thread_id",
    "created_at",
    "updated_at",
)

_MAX_TREE_LIMIT = 2000


def _json_value(value: Any) -> Any:
    """Convert a database scalar to a JSON-safe value (recursively)."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _view(row: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {field: _json_value(row[field]) for field in fields if field in row}


def plan_view(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The JSON-safe plan view for a raw plan row (``None`` stays ``None``)."""
    return None if row is None else _view(row, _PLAN_FIELDS)


async def read_goal(*, thread_id: str) -> dict[str, Any] | None:
    """Live goal + its latest plan, or ``None`` when off/no goal.

    ``None`` doubles as "goals flag off" and "no live goal": both mean the caller
    has nothing new to render, and the legacy thread state is the source.
    """
    if not refactor_flag_enabled("goals"):
        return None
    goal = await db_goals.get_live_goal(thread_id)
    if goal is None:
        return None
    plan = await db_goals.get_latest_plan(str(goal["goal_id"]))
    return {
        "goal": _view(goal, _GOAL_FIELDS),
        "plan": _view(plan, _PLAN_FIELDS) if plan is not None else None,
    }


async def read_events(
    *,
    thread_id: str,
    after_event_id: int = 0,
    limit: int = 200,
) -> list[dict[str, Any]] | None:
    """A thread's event page, or ``None`` when the events flag is off."""
    if not refactor_flag_enabled("events"):
        return None
    rows = await db_events.list_events(
        thread_id=thread_id, after_event_id=after_event_id, limit=limit
    )
    return [_view(row, _EVENT_FIELDS) for row in rows]


async def read_task_tree(
    *,
    thread_id: str,
    limit: int = 500,
) -> list[dict[str, Any]] | None:
    """The persisted task tree as nested roots, or ``None`` when the flag is off."""
    if not refactor_flag_enabled("task_tree"):
        return None
    if limit < 1 or limit > _MAX_TREE_LIMIT:
        raise ValueError(f"task tree limit must be between 1 and {_MAX_TREE_LIMIT}")
    rows = await db_tasks.list_tasks(thread_id=thread_id, limit=limit)
    return build_task_tree([_view(row, _TASK_FIELDS) for row in rows])


def build_task_tree(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Nest flat task rows by ``parent_node_id``.

    Orphans (parent not in the page) and cycles are attached at the root rather
    than dropped: a bounded page can legitimately cut a parent, and a tree that
    silently loses nodes is worse than one with an extra root. Input order is the
    DB's creation order, which the tree preserves.
    """
    nodes: dict[str, dict[str, Any]] = {}
    for row in rows:
        node = {**row, "children": []}
        node_id = str(node.get("task_node_id") or "")
        if not node_id:
            continue
        node["task_node_id"] = node_id
        nodes[node_id] = node

    roots: list[dict[str, Any]] = []
    for node_id, node in nodes.items():
        parent = str(node.get("parent_node_id") or "")
        if parent and parent in nodes and parent != node_id:
            nodes[parent]["children"].append(node)
        else:
            roots.append(node)

    # A parent cycle (a -> b -> a) leaves both nodes attached to each other and
    # never in roots; break it by promoting the first node of each remaining
    # component. Depth-first from roots marks reachable nodes.
    reachable: set[str] = set()

    def mark(node: dict[str, Any]) -> None:
        if node["task_node_id"] in reachable:
            return
        reachable.add(node["task_node_id"])
        for child in node["children"]:
            mark(child)

    for root in roots:
        mark(root)
    for node_id, node in nodes.items():
        if node_id in reachable:
            continue
        # Detach the node from its parent (not the reverse): the node keeps its
        # own subtree, so promoting one edge of a cycle does not split it.
        parent = str(node.get("parent_node_id") or "")
        if parent in nodes:
            parent_node = nodes[parent]
            parent_node["children"] = [
                child
                for child in parent_node["children"]
                if child.get("task_node_id") != node_id
            ]
        node["parent_node_id"] = None
        roots.append(node)
        mark(node)
    return roots


@dataclass(frozen=True)
class MemoryParityReport:
    """Store-vs-projection comparison used by the backfill and memory eval."""

    missing: tuple[tuple[str, str], ...] = ()
    extra: tuple[tuple[str, str], ...] = ()
    changed: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return not self.missing and not self.extra and not self.changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "missing": [list(key) for key in self.missing],
            "extra": [list(key) for key in self.extra],
            "changed": [list(key) for key in self.changed],
        }


def _memory_identity(entry: Mapping[str, Any]) -> tuple[str, str]:
    return (str(entry.get("tier") or ""), str(entry.get("entry_key") or ""))


def compare_memory_projection(
    *,
    store_entries: Sequence[Mapping[str, Any]],
    projected_entries: Sequence[Mapping[str, Any]],
) -> MemoryParityReport:
    """Compare store entries with projection rows by ``(tier, entry_key)``.

    ``changed`` means content_hash or index_text differs; the projection is a
    pure function of the store, so either difference means the backfill has work
    to do (or a dual write was dropped).
    """
    store = {_memory_identity(entry): entry for entry in store_entries}
    projected = {_memory_identity(entry): entry for entry in projected_entries}
    missing = tuple(sorted(set(store) - set(projected)))
    extra = tuple(sorted(set(projected) - set(store)))
    changed = tuple(
        sorted(
            key
            for key in set(store) & set(projected)
            if store[key].get("content_hash") != projected[key].get("content_hash")
            or store[key].get("index_text") != projected[key].get("index_text")
        )
    )
    return MemoryParityReport(missing=missing, extra=extra, changed=changed)


async def read_memory_index(
    *,
    user_id: str,
    tier: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]] | None:
    """Active memory entries as index-line views, or ``None`` when the flag is off."""
    if not refactor_flag_enabled("memory_index"):
        return None
    if tier is not None and tier not in MEMORY_TIERS:
        raise ContractError(f"unknown memory tier {tier!r}")
    rows = await db_memory.list_entries(
        user_id=user_id, tier=tier, status="active", limit=limit
    )
    return [_view(row, _MEMORY_FIELDS) for row in rows]
