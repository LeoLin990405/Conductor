"""Dual-write entry point for the memory-entry projection.

The store write remains the source of truth; this service is the projection's
validator + flag gate. It also enforces the two bounds the SQL layer cannot
express: a non-empty key/hash (a projection row without identity cannot be
repaired) and a bounded index line (the searchable summary, not a body).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.server.contracts.memory_concept import UNSET, Unset
from src.server.contracts.refactor import (
    MEMORY_ENTRY_STATUSES,
    MEMORY_TIERS,
    ContractError,
)
from src.server.database.refactor import memory as db_memory
from src.server.services.refactor_flags import refactor_flag_enabled

MAX_ENTRY_KEY_LENGTH = 512
MAX_INDEX_TEXT_LENGTH = 2048
MAX_SEARCH_QUERY_LENGTH = 512
_TRUNCATION_SUFFIX = "…[truncated]"


async def project_memory_entry(
    *,
    user_id: str,
    tier: str,
    namespace: Sequence[str],
    entry_key: str,
    content_hash: str,
    size: int = 0,
    index_text: str = "",
    status: str = "active",
    provenance: Mapping[str, Any] | None = None,
    source_thread_id: str | None = None,
    source_response_id: str | None = None,
    source_entry_id: str | None = None,
    memory_class: str | None = None,
    about: str | None = None,
) -> dict[str, Any] | None:
    """Project one store entry, or ``None`` when the ``memory_index`` flag is off."""
    if not refactor_flag_enabled("memory_index"):
        return None
    if tier not in MEMORY_TIERS:
        raise ContractError(f"unknown memory tier {tier!r}")
    if status not in MEMORY_ENTRY_STATUSES:
        raise ContractError(f"unknown memory entry status {status!r}")
    key = (entry_key or "").strip()
    if not key:
        raise ContractError("memory entry needs a non-empty key")
    if len(key) > MAX_ENTRY_KEY_LENGTH:
        raise ContractError(f"memory entry key exceeds {MAX_ENTRY_KEY_LENGTH} chars")
    digest = (content_hash or "").strip()
    if not digest:
        raise ContractError("memory entry needs a content hash")
    text = (index_text or "").strip()
    if len(text) > MAX_INDEX_TEXT_LENGTH:
        text = text[: MAX_INDEX_TEXT_LENGTH - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX
    if not namespace:
        raise ContractError("memory entry needs a namespace")
    return await db_memory.upsert_entry(
        user_id=user_id,
        tier=tier,
        namespace=list(namespace),
        entry_key=key,
        content_hash=digest,
        size=max(0, int(size)),
        index_text=text,
        status=status,
        provenance=provenance,
        source_thread_id=source_thread_id,
        source_response_id=source_response_id,
        source_entry_id=source_entry_id,
        memory_class=memory_class,
        about=about,
    )


async def search_memory(
    *,
    user_id: str,
    query: str,
    tier: str | None = None,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Ranked retrieval over the projection; empty when the flag is off."""
    if not refactor_flag_enabled("memory_index"):
        return []
    if tier is not None and tier not in MEMORY_TIERS:
        raise ContractError(f"unknown memory tier {tier!r}")
    text = (query or "").strip()
    if len(text) > MAX_SEARCH_QUERY_LENGTH:
        raise ContractError(
            f"memory search query exceeds {MAX_SEARCH_QUERY_LENGTH} chars"
        )
    # The embedding path is optional and fail-open; when unconfigured it returns
    # exactly the lexical rows below. Imported lazily to keep the SQL layer out
    # of the embedding client's import graph.
    from src.server.services.refactor.memory_embeddings import hybrid_search

    return await hybrid_search(
        user_id=user_id,
        query=text,
        tier=tier,
        thread_id=thread_id,
        workspace_id=workspace_id,
        limit=limit,
    )


async def supersede_memory(
    *,
    user_id: str,
    tier: str,
    entry_key: str,
) -> int:
    """Mark an active entry superseded (lifecycle move, not a rewrite)."""
    if not refactor_flag_enabled("memory_index"):
        return 0
    if tier not in MEMORY_TIERS:
        raise ContractError(f"unknown memory tier {tier!r}")
    return await db_memory.supersede_entry(
        user_id=user_id, tier=tier, entry_key=entry_key
    )


async def list_memory(
    *,
    user_id: str,
    tier: str | None = None,
    status: str = "active",
    limit: int = 100,
    offset: int = 0,
    memory_class: str | None | Unset = UNSET,
    about: str | None | Unset = UNSET,
) -> list[dict[str, Any]]:
    """The owner's memory rows, optionally narrowed to one concept.

    Each ontology filter has three states and they are not
    interchangeable: ``UNSET`` narrows nothing, a value narrows to the rows
    that name it, and ``None`` narrows to the rows that name nothing -- the
    unnamed group the browse page offers alongside the named ones.

    ``offset`` continues the listing past its first page.
    """
    if not refactor_flag_enabled("memory_index"):
        return []
    if tier is not None and tier not in MEMORY_TIERS:
        raise ContractError(f"unknown memory tier {tier!r}")
    if status not in MEMORY_ENTRY_STATUSES:
        raise ContractError(f"unknown memory entry status {status!r}")
    return await db_memory.list_entries(
        user_id=user_id,
        tier=tier,
        status=status,
        limit=limit,
        offset=offset,
        memory_class=memory_class,
        about=about,
    )


async def list_memory_concepts(
    *,
    user_id: str,
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """The owner's memory concepts: one row per ``(class, about)`` pair.

    A memory that names several concepts appears under each of them.

    ``offset`` continues the listing past its first page.
    """
    if not refactor_flag_enabled("memory_index"):
        return []
    return await db_memory.list_concepts(user_id=user_id, limit=limit, offset=offset)
