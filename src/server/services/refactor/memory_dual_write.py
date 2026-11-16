"""Projection dual write for memory store writes (flag-gated, best-effort).

The store is written by the agent's file tools through ``StoreBackend``; this
adapter sits in front of that store for the duration of a run. When the
``memory_index`` flag is on, each memory-namespace ``aput``/``adelete`` mirrors
into ``memory_entries`` after the store write succeeded. The projection is a
derived read model: a projection failure is logged and dropped, never surfaced
to the agent, and the backfill job repairs it.

Mapping rules are shared with the backfill (``memory_projection``): the index
file supplies pointer lines for details; a detail written before its pointer
falls back to its description and is corrected when ``memory.md`` arrives.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from src.server.contracts.memory_origin import normalize_memory_metadata
from src.server.database.refactor import memory as db_memory
from src.server.services.refactor.memory_projection import (
    MEMORY_INDEX_KEY,
    PROPOSED_PREFIX,
    IndexPointer,
    parse_memory_metadata,
    content_hash,
    find_pointer,
    index_text_for,
    normalize_key,
    parse_memory_index,
    pointer_index_text,
    primary_concept,
)
from src.server.services.refactor_flags import refactor_flag_enabled
from src.server.utils.pg_sanitize import refactor_owner_uuid

logger = logging.getLogger(__name__)

_MEMORY_LABEL_DOT = "%2E"


def _label_to_id(label: str) -> str:
    # ``store_label`` escapes a dot in a user id so the label stays one path
    # component; undo only that escape.
    return str(label).replace(_MEMORY_LABEL_DOT, ".")


def memory_tier_for_namespace(
    namespace: Sequence[Any],
) -> tuple[str, str, str | None] | None:
    """``(tier, user_id, workspace_id)`` for a memory namespace, else None."""
    parts = [str(part) for part in namespace]
    if len(parts) == 2 and parts[1] == "memory":
        return "user", _label_to_id(parts[0]), None
    if len(parts) == 4 and parts[1] == "workspaces" and parts[3] == "memory":
        return "workspace", _label_to_id(parts[0]), _label_to_id(parts[2])
    if len(parts) == 4 and parts[1] == "threads" and parts[3] == "memory":
        return "session", _label_to_id(parts[0]), None
    return None


async def _read_pointer_map(
    store: Any, namespace: Sequence[Any]
) -> Mapping[str, IndexPointer]:
    """The current pointer lines, read from the store's index file (best-effort)."""
    try:
        item = await store.aget(tuple(namespace), MEMORY_INDEX_KEY)
    except Exception:
        logger.debug("[refactor] memory index read failed", exc_info=True)
        return {}
    value = getattr(item, "value", None) if item is not None else None
    content = value.get("content") if isinstance(value, Mapping) else None
    return parse_memory_index(content if isinstance(content, str) else "")


async def project_memory_write(
    *,
    store: Any,
    namespace: Sequence[Any],
    key: str,
    value: Any = None,
    deleted: bool = False,
) -> dict[str, Any] | None:
    """Mirror one store write into the projection; ``None`` when out of scope."""
    target = memory_tier_for_namespace(namespace)
    if target is None:
        return None
    tier, user_label, workspace_id = target
    # OSS store labels are the raw auth id (a non-UUID sentinel); the projection
    # is UUID-typed, so map it the same way reads/writes do.
    user_id = refactor_owner_uuid(user_label)
    if user_id is None:
        return None
    entry_key = normalize_key(str(key))
    if not entry_key:
        return None

    # A proposal is a candidate memory, not memory: never project it as active.
    # On deletion, still supersede a legacy proposed row so the deploy-time
    # cleanup can remove rows written before this rule existed.
    if entry_key.startswith(PROPOSED_PREFIX):
        if deleted:
            return {
                "superseded": await db_memory.supersede_entry(
                    user_id=user_id, tier=tier, entry_key=entry_key
                )
            }
        return None

    if entry_key == MEMORY_INDEX_KEY:
        content = value.get("content") if isinstance(value, Mapping) else None
        if not isinstance(content, str):
            return None
        pointers = parse_memory_index(content)
        return {
            "rows": await db_memory.update_index_texts(
                user_id=user_id,
                tier=tier,
                updates={
                    path: pointer_index_text(pointer)
                    for path, pointer in pointers.items()
                },
            )
        }

    if deleted:
        return {
            "superseded": await db_memory.supersede_entry(
                user_id=user_id, tier=tier, entry_key=entry_key
            )
        }

    content = value.get("content") if isinstance(value, Mapping) else None
    if not isinstance(content, str):
        return None
    pointers = await _read_pointer_map(store, namespace)
    index_text = index_text_for(entry_key, content, pointers)
    if not index_text:
        # The document may have become unprojectable: no usable body, or
        # malformed/unterminated metadata (which fails closed). A row written
        # for an earlier version of this key must not stay active and
        # searchable, so deactivate it and project nothing.
        return {
            "superseded": await db_memory.supersede_entry(
                user_id=user_id, tier=tier, entry_key=entry_key
            )
        }
    metadata = normalize_memory_metadata(parse_memory_metadata(content))
    status = (
        "active"
        if metadata.get("lifecycle") in (None, "active")
        else "superseded"
    )
    if status == "superseded":
        await db_memory.supersede_entry(
            user_id=user_id, tier=tier, entry_key=entry_key
        )
    lineage_ids = metadata.get("supersedes_entry_ids")
    row = await db_memory.upsert_entry(
        user_id=user_id,
        tier=tier,
        namespace=[str(part) for part in namespace],
        entry_key=entry_key,
        content_hash=content_hash(content),
        size=len(content.encode("utf-8")),
        index_text=index_text,
        status=status,
        metadata=metadata,
        source_entry_id=metadata.get("source_entry_id"),
        # The ontology fields of the memory: the class it belongs to and the
        # concept it is about. A memory that names neither stores NULL in both
        # columns, and a memory that stops naming one clears it: the columns
        # say what this memory is now, not what it once was.
        memory_class=metadata.get("class"),
        about=primary_concept(metadata),
        # Row and lineage commit atomically: an activation whose projection
        # failed must not leave a searchable row missing from the forgotten
        # lineage walk.
        source_entry_ids=(
            [str(value) for value in lineage_ids]
            if isinstance(lineage_ids, (list, tuple))
            else None
        ),
        provenance={
            "source": "store_dual_write",
            "store_namespace": ".".join(str(part) for part in namespace),
            "store_key": entry_key,
            "pointed": find_pointer(pointers, entry_key) is not None,
            "workspace_id": workspace_id,
            "thread_id": (
                _label_to_id(str(namespace[2]))
                if tier == "session" and len(namespace) > 2
                else None
            ),
        },
    )
    return {"row": row}


class MemoryProjectionStore:
    """Delegating store facade that mirrors memory writes into the projection."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    async def aget(self, namespace, key, **kwargs):  # noqa: ANN001
        return await self._store.aget(namespace, key, **kwargs)

    async def asearch(self, namespace, *, limit=10, offset=0, **kwargs):  # noqa: ANN001
        return await self._store.asearch(namespace, limit=limit, offset=offset, **kwargs)

    async def aput(self, namespace, key, value, **kwargs):  # noqa: ANN001
        result = await self._store.aput(namespace, key, value, **kwargs)
        try:
            await project_memory_write(
                store=self._store, namespace=namespace, key=key, value=value
            )
        except Exception:
            logger.warning(
                f"[refactor] memory projection write failed for key={key}", exc_info=True
            )
        return result

    async def adelete(self, namespace, key, **kwargs):  # noqa: ANN001
        result = await self._store.adelete(namespace, key, **kwargs)
        try:
            await project_memory_write(
                store=self._store, namespace=namespace, key=key, deleted=True
            )
        except Exception:
            logger.warning(
                f"[refactor] memory projection delete failed for key={key}", exc_info=True
            )
        return result


def maybe_wrap_memory_store(store: Any) -> Any:
    """Wrap a store for the run when the memory-index flag is on."""
    if store is None or not refactor_flag_enabled("memory_index"):
        return store
    return MemoryProjectionStore(store)
