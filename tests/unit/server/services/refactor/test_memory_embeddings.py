"""API embedding path: opt-in config, hybrid merge and fail-open fallback."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock

import pytest

from src.server.services.refactor import memory_embeddings as emb

pytestmark = pytest.mark.asyncio


def _enable(monkeypatch):
    monkeypatch.setenv("MEMORY_EMBEDDING_ENABLED", "1")
    monkeypatch.setenv("MEMORY_EMBEDDING_PROVIDER", "minimax")
    monkeypatch.setenv("MEMORY_EMBEDDING_MODEL", "embo-01")
    monkeypatch.setenv("MEMORY_EMBEDDING_API_KEY", "test-key")


def _row(key: str, vector=None):
    return {
        "tier": "user",
        "entry_key": key,
        "index_text": key,
        "content_hash": f"hash:{key}",
        "embedding": vector,
        "embedding_model": "embo-01" if vector else None,
        "embedding_content_hash": f"hash:{key}" if vector else None,
    }


async def test_config_is_disabled_without_flag(monkeypatch):
    monkeypatch.delenv("MEMORY_EMBEDDING_ENABLED", raising=False)
    assert emb.embedding_config() is None


async def test_hybrid_merges_lexical_and_semantic(monkeypatch):
    _enable(monkeypatch)
    lexical = _row("lexical")
    semantic = _row("semantic", [1.0, 0.0])
    monkeypatch.setattr(emb, "embed_texts", AsyncMock(return_value=[[1.0, 0.0]]))
    monkeypatch.setattr(emb.db_memory, "search_entries", AsyncMock(return_value=[lexical]))
    monkeypatch.setattr(
        emb.db_memory, "list_retrieval_rows", AsyncMock(return_value=[lexical, semantic])
    )
    monkeypatch.setattr(emb.db_memory, "save_embedding", AsyncMock())

    rows = await emb.hybrid_search(user_id="u", query="a paraphrase")

    keys = [row["entry_key"] for row in rows]
    assert set(keys) == {"lexical", "semantic"}


async def test_hybrid_falls_back_when_provider_fails(monkeypatch):
    _enable(monkeypatch)
    lexical = _row("lexical")
    monkeypatch.setattr(emb, "embed_texts", AsyncMock(side_effect=RuntimeError("down")))
    monkeypatch.setattr(emb.db_memory, "search_entries", AsyncMock(return_value=[lexical]))

    rows = await emb.hybrid_search(user_id="u", query="a query")

    assert rows == [lexical]


async def test_hybrid_falls_back_when_semantic_stage_exceeds_budget(monkeypatch):
    _enable(monkeypatch)
    lexical = _row("lexical")
    monkeypatch.setattr(emb.db_memory, "search_entries", AsyncMock(return_value=[lexical]))

    async def slow_embed(*args, **kwargs):
        await asyncio.sleep(5)
        return [[1.0, 0.0]]

    monkeypatch.setattr(emb, "embed_texts", slow_embed)
    retrieve = AsyncMock()
    monkeypatch.setattr(emb.db_memory, "list_retrieval_rows", retrieve)

    rows = await emb.hybrid_search(
        user_id="u",
        query="a query",
        semantic_deadline=time.monotonic() + 0.01,
    )

    assert rows == [lexical]
    retrieve.assert_not_awaited()


async def test_hybrid_returns_lexical_without_embeddings_when_budget_is_spent(monkeypatch):
    _enable(monkeypatch)
    lexical = _row("lexical")
    monkeypatch.setattr(emb.db_memory, "search_entries", AsyncMock(return_value=[lexical]))
    embed = AsyncMock()
    monkeypatch.setattr(emb, "embed_texts", embed)

    rows = await emb.hybrid_search(
        user_id="u",
        query="a query",
        semantic_deadline=time.monotonic() - 1.0,
    )

    assert rows == [lexical]
    embed.assert_not_awaited()


async def test_lexical_time_is_charged_to_the_semantic_deadline(monkeypatch):
    """The embedding stage must stop at the absolute deadline, not restart it."""
    _enable(monkeypatch)
    lexical = _row("lexical")
    clock = [100.0]

    def monotonic():
        return clock[0]

    async def slow_lexical(**kwargs):
        clock[0] += 0.4
        return [lexical]

    monkeypatch.setattr(emb.time, "monotonic", monotonic)
    monkeypatch.setattr(emb.db_memory, "search_entries", slow_lexical)
    monkeypatch.setattr(emb, "embed_texts", AsyncMock(return_value=[[1.0, 0.0]]))
    monkeypatch.setattr(
        emb.db_memory, "list_retrieval_rows", AsyncMock(return_value=[lexical])
    )

    seen: list[float | None] = []
    real_timeout = asyncio.timeout

    def recording_timeout(delay):
        seen.append(delay)
        return real_timeout(delay)

    monkeypatch.setattr(emb.asyncio, "timeout", recording_timeout)

    rows = await emb.hybrid_search(
        user_id="u", query="a query", semantic_deadline=101.0
    )

    assert {row["entry_key"] for row in rows} == {"lexical"}
    # 1.0s of absolute budget minus 400ms already spent on lexical retrieval.
    assert seen == [pytest.approx(0.6)]


@pytest.mark.parametrize("tier", ["session", "task"])
@pytest.mark.parametrize("thread_id", [None, ""])
async def test_thread_tier_search_without_thread_returns_nothing(
    monkeypatch, tier, thread_id
):
    _enable(monkeypatch)
    search = AsyncMock(return_value=[])
    retrieval = AsyncMock(return_value=[])
    embed = AsyncMock(return_value=[[1.0, 0.0]])
    monkeypatch.setattr(emb.db_memory, "search_entries", search)
    monkeypatch.setattr(emb.db_memory, "list_retrieval_rows", retrieval)
    monkeypatch.setattr(emb, "embed_texts", embed)

    rows = await emb.hybrid_search(
        user_id="u", query="q", tier=tier, thread_id=thread_id
    )

    assert rows == []
    search.assert_not_awaited()
    retrieval.assert_not_awaited()
    embed.assert_not_awaited()


async def test_task_search_with_thread_uses_lexical_and_vector_paths(monkeypatch):
    _enable(monkeypatch)
    thread_id = "thread-1"
    semantic = {**_row("task-fact", [1.0, 0.0]), "tier": "task"}
    search = AsyncMock(return_value=[])
    retrieval = AsyncMock(return_value=[semantic])
    embed = AsyncMock(return_value=[[1.0, 0.0]])
    monkeypatch.setattr(emb.db_memory, "search_entries", search)
    monkeypatch.setattr(emb.db_memory, "list_retrieval_rows", retrieval)
    monkeypatch.setattr(emb, "embed_texts", embed)

    rows = await emb.hybrid_search(
        user_id="u", query="q", tier="task", thread_id=thread_id
    )

    assert [row["entry_key"] for row in rows] == ["task-fact"]
    assert search.await_args.kwargs["thread_id"] == thread_id
    assert retrieval.await_args.kwargs["thread_id"] == thread_id
    assert retrieval.await_args.kwargs["tier"] == "task"


async def test_workspace_scope_reaches_lexical_and_semantic_retrieval(monkeypatch):
    _enable(monkeypatch)
    search = AsyncMock(return_value=[])
    retrieval = AsyncMock(return_value=[])
    monkeypatch.setattr(emb.db_memory, "search_entries", search)
    monkeypatch.setattr(emb.db_memory, "list_retrieval_rows", retrieval)
    monkeypatch.setattr(emb, "embed_texts", AsyncMock(return_value=[[1.0, 0.0]]))

    await emb.hybrid_search(
        user_id="u", query="q", tier="project", workspace_id="workspace-1"
    )

    assert search.await_args.kwargs["workspace_id"] == "workspace-1"
    assert retrieval.await_args.kwargs["workspace_id"] == "workspace-1"


async def test_ensure_embeddings_fills_only_missing(monkeypatch):
    _enable(monkeypatch)
    missing = _row("missing")
    present = _row("present", [1.0, 0.0])
    save = AsyncMock()
    monkeypatch.setattr(emb, "embed_texts", AsyncMock(return_value=[[0.0, 1.0]]))
    monkeypatch.setattr(emb.db_memory, "list_retrieval_rows", AsyncMock(return_value=[missing, present]))
    monkeypatch.setattr(emb.db_memory, "save_embedding", save)

    rows = await emb.ensure_embeddings(user_id="u")

    save.assert_awaited_once()
    assert rows[0]["embedding"] == [0.0, 1.0]


async def test_ensure_embeddings_never_sends_secret_or_malformed_rows(monkeypatch):
    _enable(monkeypatch)
    secret = {**_row("secret"), "metadata": {"sensitivity": "secret"}}
    malformed = {**_row("malformed"), "metadata": ["invalid"]}
    public = {**_row("public"), "metadata": {"sensitivity": "public"}}
    embed = AsyncMock(return_value=[[0.0, 1.0]])
    monkeypatch.setattr(emb, "embed_texts", embed)
    monkeypatch.setattr(
        emb.db_memory, "list_retrieval_rows", AsyncMock(return_value=[secret, malformed, public])
    )
    monkeypatch.setattr(emb.db_memory, "save_embedding", AsyncMock())

    rows = await emb.ensure_embeddings(user_id="u")

    assert [row["entry_key"] for row in rows] == ["public"]
    embed.assert_awaited_once_with(["public"], query=False)
