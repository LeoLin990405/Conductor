"""Optional API embeddings for hybrid memory retrieval.

The canonical memory remains the Markdown store; this module only fills the
projection's optional vector payload. It is deliberately fail-open: an
unconfigured key, a provider outage, or a malformed response all degrade to the
existing FTS + trigram search rather than failing the memory page.

Provider shape:

- ``minimax`` (default): ``POST /v1/embeddings`` with ``texts`` and ``type``
  (``db`` for documents, ``query`` for the query), response ``vectors``;
- ``openai``: the standard ``input`` / ``data[].embedding`` shape.

Vectors are L2-normalised before storage, so cosine similarity is a dot
product. ``JSONB`` is used instead of pgvector because the local Postgres image
does not ship the extension; the interface is shaped so a pgvector migration
can replace the storage without changing retrieval callers.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)
from src.server.database.refactor import memory as db_memory

logger = logging.getLogger(__name__)

_MAX_BATCH = 16
_RRF_K = 60
_MIN_SIMILARITY = 0.15


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    url: str
    model: str
    api_key: str
    dimensions: int | None


def embedding_config() -> EmbeddingConfig | None:
    """The configured provider, or None when embeddings are intentionally off."""
    if os.getenv("MEMORY_EMBEDDING_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    provider = os.getenv("MEMORY_EMBEDDING_PROVIDER", "minimax").strip().lower()
    if provider == "minimax":
        url = os.getenv("MEMORY_EMBEDDING_API_URL", "https://api.minimax.cn/v1/embeddings").strip()
        model = os.getenv("MEMORY_EMBEDDING_MODEL", "embo-01").strip()
        key = os.getenv("MEMORY_EMBEDDING_API_KEY", "").strip() or os.getenv("MINIMAX_API_KEY", "").strip()
    else:
        url = os.getenv("MEMORY_EMBEDDING_API_URL", "").strip()
        model = os.getenv("MEMORY_EMBEDDING_MODEL", "").strip()
        key = os.getenv("MEMORY_EMBEDDING_API_KEY", "").strip()
    if not url or not model or not key:
        return None
    raw_dim = os.getenv("MEMORY_EMBEDDING_DIMENSIONS", "").strip()
    try:
        dimensions = int(raw_dim) if raw_dim else None
    except ValueError:
        dimensions = None
    return EmbeddingConfig(provider=provider, url=url, model=model, api_key=key, dimensions=dimensions)


def _normalise(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(float(value) * float(value) for value in vector))
    if norm <= 0:
        return [0.0 for _ in vector]
    return [float(value) / norm for value in vector]


async def embed_texts(texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
    """Embed a bounded batch; raises on provider failure so callers can fall back."""
    config = embedding_config()
    if config is None:
        return []
    if not texts:
        return []
    payload: dict[str, Any]
    if config.provider == "minimax":
        payload = {
            "model": config.model,
            "texts": list(texts),
            "type": "query" if query else "db",
        }
    else:
        payload = {"model": config.model, "input": list(texts)}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            config.url,
            headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    if config.provider == "minimax":
        base = data.get("base_resp") or {}
        if base.get("status_code") not in (None, 0):
            raise RuntimeError(f"embedding provider refused: {base.get('status_msg')}")
        vectors = data.get("vectors")
    else:
        vectors = [entry.get("embedding") for entry in data.get("data") or []]
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise RuntimeError("embedding provider returned an unexpected vector count")
    normalised = [_normalise(vector) for vector in vectors if isinstance(vector, list)]
    if len(normalised) != len(texts):
        raise RuntimeError("embedding provider returned an invalid vector")
    if config.dimensions and any(len(vector) != config.dimensions for vector in normalised):
        raise RuntimeError("embedding provider returned an unexpected dimension")
    return normalised


async def ensure_embeddings(
    user_id: str,
    *,
    tier: str | None = None,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Rows with fresh embeddings, filling missing/stale payloads in batches.

    A provider failure returns the rows as-is (with whatever vectors they have),
    so the caller can still use lexical retrieval.
    """
    config = embedding_config()
    rows = await db_memory.list_retrieval_rows(
        user_id=user_id,
        tier=tier,
        thread_id=thread_id,
        workspace_id=workspace_id,
        limit=limit,
    )
    # Keep the egress boundary here too: callers may replace the DB selector,
    # and secret content must never reach an embedding provider.
    rows = [
        row
        for row in rows
        if normalize_memory_policy_metadata_layers(row.get("metadata"))["sensitivity"]
        != "secret"
    ]
    if config is None or not rows:
        return rows
    missing = [
        row for row in rows
        if not row.get("embedding")
        or row.get("embedding_model") != config.model
        or row.get("embedding_content_hash") != row.get("content_hash")
    ]
    for start in range(0, len(missing), _MAX_BATCH):
        batch = missing[start:start + _MAX_BATCH]
        texts = [str(row.get("index_text") or "") for row in batch]
        try:
            vectors = await embed_texts(texts, query=False)
        except Exception as exc:  # noqa: BLE001 - lexical search remains usable
            logger.warning("[memory] embedding fill failed: %s", type(exc).__name__)
            break
        for row, vector in zip(batch, vectors):
            try:
                await db_memory.save_embedding(
                    user_id=user_id,
                    tier=str(row.get("tier") or ""),
                    entry_key=str(row.get("entry_key") or ""),
                    embedding=vector,
                    model=config.model,
                    content_hash=str(row.get("content_hash") or ""),
                )
                row.update({
                    "embedding": vector,
                    "embedding_model": config.model,
                    "embedding_content_hash": row.get("content_hash"),
                })
            except Exception:  # noqa: BLE001
                logger.warning("[memory] embedding save failed", exc_info=True)
    return rows


def _cosine(a: Sequence[float] | None, b: Sequence[float] | None) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(float(x) * float(y) for x, y in zip(a, b))


async def hybrid_search(
    *,
    user_id: str,
    query: str,
    tier: str | None = None,
    thread_id: str | None = None,
    workspace_id: str | None = None,
    limit: int = 20,
    semantic_deadline: float | None = None,
) -> list[dict[str, Any]]:
    """Lexical results, plus semantic RRF when an embedding provider is on.

    Session and task rows are only searched with an explicit thread id; a query
    without one must never see another thread's thread-scoped memory. ``semantic_deadline``
    is an absolute ``time.monotonic()`` instant for the optional stage: the
    lexical query runs first and its elapsed time is charged to that same
    instant, so a slow lexical query cannot push the embedding round-trip past
    a caller's budget. When the deadline is already spent (or expires) the
    lexical rows are returned rather than discarded.
    """
    if tier in {"session", "task"} and not thread_id:
        return []
    lexical = await db_memory.search_entries(
        user_id=user_id,
        query=query,
        tier=tier,
        thread_id=thread_id,
        workspace_id=workspace_id,
        limit=limit,
    )
    config = embedding_config()
    if config is None or not query.strip():
        return lexical
    remaining: float | None = None
    if semantic_deadline is not None:
        remaining = semantic_deadline - time.monotonic()
        # No time left for the optional stage; the lexical rows are complete on
        # their own, so don't start an embedding round-trip that must be abandoned.
        if remaining <= 0:
            return lexical
    try:
        async with asyncio.timeout(remaining):
            query_vector = (await embed_texts([query], query=True))[0]
            rows = await ensure_embeddings(
                user_id,
                tier=tier,
                thread_id=thread_id,
                workspace_id=workspace_id,
                limit=max(100, limit * 8),
            )
    except TimeoutError:
        logger.warning(
            "[memory] semantic search exceeded its budget; using lexical results"
        )
        return lexical
    except Exception as exc:  # noqa: BLE001
        logger.warning("[memory] semantic search unavailable: %s", type(exc).__name__)
        return lexical
    if not query_vector:
        return lexical
    semantic = sorted(
        (
            (_cosine(query_vector, row.get("embedding")), row)
            for row in rows
            if row.get("embedding")
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    semantic = [(score, row) for score, row in semantic if score >= _MIN_SIMILARITY][:limit]

    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    scores: dict[tuple[str, str], float] = {}
    for rank, row in enumerate(lexical):
        key = (str(row.get("tier") or ""), str(row.get("entry_key") or ""))
        by_key[key] = row
        scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
    for rank, (_, row) in enumerate(semantic):
        key = (str(row.get("tier") or ""), str(row.get("entry_key") or ""))
        by_key.setdefault(key, row)
        scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return [
        by_key[key]
        for key, _ in sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[:limit]
    ]


async def embedding_health(user_id: str, *, tier: str | None = None) -> dict[str, Any]:
    config = embedding_config()
    if config is None:
        return {"enabled": False, "model": None, "embedded": 0, "pending": 0}
    try:
        rows = await db_memory.list_retrieval_rows(user_id=user_id, tier=tier, limit=500)
        embedded = sum(
            1 for row in rows
            if row.get("embedding") and row.get("embedding_model") == config.model
            and row.get("embedding_content_hash") == row.get("content_hash")
        )
    except Exception:  # noqa: BLE001
        rows, embedded = [], 0
    return {
        "enabled": True,
        "model": config.model,
        "embedded": embedded,
        "pending": max(0, len(rows) - embedded),
    }
