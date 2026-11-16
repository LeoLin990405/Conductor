"""Small bounded TTL cache for immutable autonomy reads.

Triggers never change after intake, so reading one for a Router/Watchdog turn
does not need a database round trip every time. The cache is process-local and
deliberately narrow: mutable Tickets and Threads are never cached here.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable


class TTLCache:
    def __init__(self, *, ttl_seconds: float = 60.0, max_entries: int = 256) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max(1, max_entries)
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: Any) -> None:
        self._data[key] = (time.monotonic() + self.ttl_seconds, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_entries:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


TRIGGER_CACHE = TTLCache()
# Ticket details include mutable Inputs/Reports; the key embeds the Ticket
# revision so a write invalidates the cached detail without a fan-out invalidation.
TICKET_CACHE = TTLCache(ttl_seconds=15.0, max_entries=256)


async def cached_trigger(
    trigger_id: str,
    loader: Callable[[str], Awaitable[dict[str, Any] | None]],
    *,
    namespace: str = "",
) -> dict[str, Any] | None:
    cache_key = f"{namespace}:{trigger_id}" if namespace else trigger_id
    hit = TRIGGER_CACHE.get(cache_key)
    if hit is not None:
        return hit
    row = await loader(trigger_id)
    if row is not None:
        TRIGGER_CACHE.set(cache_key, row)
    return row
