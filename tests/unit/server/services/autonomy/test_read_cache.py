"""The autonomy read cache is TTL-bounded and only used for immutable reads."""

from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy import read_cache


def test_ttl_cache_expires_and_evicts():
    cache = read_cache.TTLCache(ttl_seconds=-1, max_entries=2)
    cache.set("a", {"id": 1})
    assert cache.get("a") is None

    live = read_cache.TTLCache(ttl_seconds=60, max_entries=2)
    live.set("a", 1)
    live.set("b", 2)
    live.set("c", 3)
    assert live.get("a") is None
    assert live.get("c") == 3


@pytest.mark.asyncio
async def test_cached_trigger_reads_once_until_cleared():
    read_cache.TRIGGER_CACHE.clear()
    loader = AsyncMock(return_value={"trigger_id": "trg1", "text": "x"})
    first = await read_cache.cached_trigger("trg1", loader)
    second = await read_cache.cached_trigger("trg1", loader)
    assert first == second == {"trigger_id": "trg1", "text": "x"}
    assert loader.await_count == 1

    read_cache.TRIGGER_CACHE.clear()
    await read_cache.cached_trigger("trg1", loader)
    assert loader.await_count == 2
