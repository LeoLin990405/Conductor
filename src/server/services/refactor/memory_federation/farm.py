"""Disabled-by-default, injection-only adapter for the model-farm gateway."""

from __future__ import annotations

import asyncio
import inspect
import math
from collections.abc import Mapping, Sequence
from typing import Protocol

from .models import MemoryEntry, MemoryHit
from .provider import (
    ReadOnlyMemoryProviderError,
    memory_hit_from_mapping,
    memory_hit_is_recallable,
)

MAX_FARM_QUERY_LENGTH = 512
MAX_FARM_RESPONSE_HITS = 100
MAX_FARM_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_FARM_KEY_LENGTH = 512
MAX_FARM_TEXT_LENGTH = 16_384
_ROOT_FIELDS = frozenset({"hits"})
_HIT_FIELDS = frozenset(
    {"source", "key", "text", "score", "provenance", "tier", "workspace_id"}
)
_REQUIRED_HIT_FIELDS = frozenset({"source", "key", "text", "score"})
class FarmHttpResponse(Protocol):
    content: bytes

    def json(self) -> object: ...

    def raise_for_status(self) -> None: ...


class FarmHttpClient(Protocol):
    async def get(self, url: str, *, params: Mapping[str, object]) -> FarmHttpResponse: ...


class FarmRetrieval:
    """Validate injected gateway responses; no transport is created here."""

    def __init__(
        self,
        endpoint: str | None = None,
        http_client: FarmHttpClient | None = None,
        *,
        enabled: bool = False,
        timeout_seconds: float = 0.5,
        max_response_hits: int = MAX_FARM_RESPONSE_HITS,
    ) -> None:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("farm timeout must be positive")
        if max_response_hits < 1:
            raise ValueError("max_response_hits must be positive")
        if endpoint is not None and not endpoint.strip().startswith(("http://", "https://")):
            raise ValueError("farm endpoint must be an explicit HTTP URL")
        self._endpoint = endpoint
        self._http_client = http_client
        self._enabled = bool(enabled)
        self._timeout = timeout_seconds
        self._max_hits = min(max_response_hits, MAX_FARM_RESPONSE_HITS)

    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]:
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("farm recall needs a non-empty user_id")
        text = (query or "").strip()
        if len(text) > MAX_FARM_QUERY_LENGTH:
            raise ValueError(f"farm query exceeds {MAX_FARM_QUERY_LENGTH} characters")
        limit = max(0, min(int(k), self._max_hits))
        if (
            not self._enabled
            or self._endpoint is None
            or self._http_client is None
            or limit == 0
            or not text
        ):
            return []
        try:
            response = await asyncio.wait_for(
                self._http_client.get(
                    self._endpoint,
                    params={
                        "user_id": user_id,
                        "query": text,
                        "k": limit,
                        "tiers": ",".join(tiers),
                        "workspace_id": workspace_id or "",
                        "thread_id": thread_id or "",
                    },
                ),
                timeout=self._timeout,
            )
            response.raise_for_status()
            if len(response.content) > MAX_FARM_RESPONSE_BYTES:
                return []
            body = response.json()
            if inspect.isawaitable(body):
                body = await asyncio.wait_for(body, timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            return []
        try:
            return self._parse_response(body, tiers, workspace_id, limit)
        except (TypeError, ValueError, KeyError, OverflowError):
            return []

    def _parse_response(
        self,
        response: object,
        tiers: Sequence[str],
        workspace_id: str | None,
        limit: int,
    ) -> list[MemoryHit]:
        if not isinstance(response, Mapping) or set(response) != _ROOT_FIELDS:
            raise ValueError("farm response root schema is invalid")
        rows = response["hits"]
        if not isinstance(rows, list) or len(rows) > self._max_hits:
            raise ValueError("farm response hit list is invalid or over bound")
        allowed_tiers = set(tiers)
        result: list[MemoryHit] = []
        seen: set[tuple[str | None, str | None, str]] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                raise ValueError("farm hit must be an object")
            keys = set(row)
            if not _REQUIRED_HIT_FIELDS <= keys or not keys <= _HIT_FIELDS:
                raise ValueError("farm hit schema is invalid")
            hit = memory_hit_from_mapping(row)
            if len(hit.key) > MAX_FARM_KEY_LENGTH or len(hit.text) > MAX_FARM_TEXT_LENGTH:
                raise ValueError("farm hit exceeds field bounds")
            if not memory_hit_is_recallable(hit):
                continue
            if hit.tier is not None and allowed_tiers and hit.tier not in allowed_tiers:
                continue
            if hit.workspace_id not in (None, workspace_id):
                continue
            # The gateway is one source. Preserve its asserted source as
            # provenance but prevent it from impersonating another provider.
            provenance = dict(hit.provenance)
            if hit.source != "farm":
                provenance["farm_source"] = hit.source
            hit = MemoryHit(
                source="farm",
                key=hit.key,
                text=hit.text,
                score=hit.score,
                provenance=provenance,
                tier=hit.tier,
                workspace_id=hit.workspace_id,
            )
            identity = (hit.tier, hit.workspace_id, hit.key)
            if identity in seen:
                continue
            seen.add(identity)
            result.append(hit)
            if len(result) >= limit:
                break
        return result

    async def propose(self, user_id: str, entry: MemoryEntry) -> str:
        raise ReadOnlyMemoryProviderError("farm retrieval is read-only")

    async def notify_forget(
        self, *, user_id: str, entry_id: str, content_hash: str
    ) -> str:
        """Report read-only status without sending a mutation to the gateway."""
        del user_id, entry_id, content_hash
        return "read_only"
