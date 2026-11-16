"""Provider protocol and bounded, fail-open federation orchestration."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from src.server.contracts.memory_validity import is_current
from src.server.contracts.memory_policy_metadata import (
    normalize_memory_policy_metadata_layers,
)

from .fusion import fuse_hits
from .health import REASON_PROVIDER_RECALL_FAILED, record_memory_failure
from .models import MemoryEntry, MemoryHit

logger = logging.getLogger(__name__)

MAX_RECALL_K = 100
MAX_QUERY_LENGTH = 512
MAX_PROVIDER_HITS = 100
MAX_PROVIDER_COUNT = 16
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 1.0


class ReadOnlyMemoryProviderError(RuntimeError):
    """Raised when a provider intentionally exposes retrieval without writes."""


class NoWritableMemoryProvider(RuntimeError):
    """Raised when federation has no explicit proposal destination."""


def memory_hit_metadata(hit: MemoryHit) -> dict[str, object]:
    """Flatten provider metadata for context while retaining outer annotations."""
    result = {
        key: value
        for key, value in hit.provenance.items()
        if key not in {"metadata", "memory_metadata"}
    }
    for key in ("metadata", "memory_metadata"):
        nested = hit.provenance.get(key)
        if isinstance(nested, Mapping):
            result.update(nested)
    return result


def memory_hit_is_recallable(hit: MemoryHit) -> bool:
    """Reject stale lifecycle states before a provider can influence RRF."""
    provenance = hit.provenance
    # Policy-relevant metadata can be wrapped more than one level deep (a
    # MemFW hit carries its own metadata inside memory_metadata). Collect every
    # nested mapping to a bounded depth so a nested lifecycle/expiry still
    # filters the hit; an outer private/active layer must not hide it.
    metadata_layers = []
    pending = [provenance]
    seen_ids = set()
    while pending and len(metadata_layers) < 64:
        layer = pending.pop()
        if id(layer) in seen_ids:
            continue
        seen_ids.add(id(layer))
        metadata_layers.append(layer)
        for key in ("metadata", "memory_metadata"):
            wrapped = layer.get(key)
            if isinstance(wrapped, Mapping):
                pending.append(wrapped)
    policy = normalize_memory_policy_metadata_layers(provenance)
    if policy["sensitivity"] == "secret":
        return False
    # Check each representation independently: one active-looking layer must
    # never override an expired or superseded value carried by another layer.
    for metadata in metadata_layers:
        if (
            metadata.get("superseded") is True
            or metadata.get("superseded_by")
            or not is_current(metadata)
        ):
            return False
        status = metadata.get("status")
        if status is not None and str(status).strip().casefold() not in {
            "active",
            "current",
        }:
            return False
    return True


def memory_entry_is_proposable(entry: MemoryEntry) -> bool:
    """Reject secret policy at the federation write boundary."""
    return (
        normalize_memory_policy_metadata_layers(entry.provenance)["sensitivity"]
        != "secret"
        and normalize_memory_policy_metadata_layers(entry.metadata)["sensitivity"]
        != "secret"
    )


@runtime_checkable
class MemoryProvider(Protocol):
    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]: ...

    async def propose(self, user_id: str, entry: MemoryEntry) -> str: ...

    async def notify_forget(
        self, *, user_id: str, entry_id: str, content_hash: str
    ) -> str: ...


class MemoryFederation:
    """Merge independent providers while allowing a failed source to fall out."""

    def __init__(
        self,
        providers: Sequence[MemoryProvider],
        *,
        proposal_writer: MemoryProvider | None = None,
        rrf_k: int = 60,
        provider_timeout_seconds: float = DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        max_provider_hits: int = MAX_PROVIDER_HITS,
    ) -> None:
        if rrf_k < 1:
            raise ValueError("rrf_k must be at least 1")
        if not math.isfinite(provider_timeout_seconds) or provider_timeout_seconds <= 0:
            raise ValueError("provider timeout must be positive")
        if max_provider_hits < 1:
            raise ValueError("max_provider_hits must be positive")
        self._providers = tuple(providers[:MAX_PROVIDER_COUNT])
        self._proposal_writer = proposal_writer
        self._rrf_k = rrf_k
        self._timeout = provider_timeout_seconds
        self._max_provider_hits = min(max_provider_hits, MAX_PROVIDER_HITS)

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
            raise ValueError("memory recall needs a non-empty user_id")
        # Truncate rather than raise: the turn's whole memory block (including
        # Conductor's own memories) must survive a query longer than the source
        # bound. The bound still applies to every provider call below.
        text = (query or "").strip()[:MAX_QUERY_LENGTH]
        result_limit = max(0, min(int(k), MAX_RECALL_K))
        if result_limit == 0 or not text or not self._providers:
            return []
        selected_tiers = tuple(dict.fromkeys(str(tier) for tier in tiers))

        async def fetch(provider: MemoryProvider) -> list[MemoryHit]:
            try:
                rows = await asyncio.wait_for(
                    provider.recall(
                        user_id=user_id,
                        query=text,
                        k=min(result_limit, self._max_provider_hits),
                        tiers=selected_tiers,
                        workspace_id=workspace_id,
                        thread_id=thread_id,
                    ),
                    timeout=self._timeout,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Fail open for this source, but make the drop visible: a fixed
                # reason code -- never the exception text, which may carry a
                # URL, a query or a peer message -- plus the process counter
                # that ``memory_failure_status()`` and ``/health`` expose.
                record_memory_failure(REASON_PROVIDER_RECALL_FAILED)
                logger.warning(
                    "Memory provider recall failed reason=%s",
                    REASON_PROVIDER_RECALL_FAILED,
                )
                return []
            if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
                # A provider that answers with a shape the contract cannot read
                # is the same drop: this source contributes nothing, and an
                # operator must be able to see that it happened.
                record_memory_failure(REASON_PROVIDER_RECALL_FAILED)
                logger.warning(
                    "Memory provider returned a non-sequence result reason=%s",
                    REASON_PROVIDER_RECALL_FAILED,
                )
                return []
            allowed_tiers = set(selected_tiers)
            accepted: list[MemoryHit] = []
            for hit in rows[: self._max_provider_hits]:
                if not isinstance(hit, MemoryHit):
                    continue
                if allowed_tiers and hit.tier is not None and hit.tier not in allowed_tiers:
                    continue
                if hit.workspace_id != workspace_id and hit.workspace_id is not None:
                    continue
                if not memory_hit_is_recallable(hit):
                    continue
                accepted.append(hit)
            return accepted

        hit_lists = await asyncio.gather(*(fetch(provider) for provider in self._providers))
        return fuse_hits(hit_lists, result_limit, rrf_k=self._rrf_k)

    async def propose(self, user_id: str, entry: MemoryEntry) -> str:
        if self._proposal_writer is None:
            raise NoWritableMemoryProvider("no proposal writer is configured")
        if not memory_entry_is_proposable(entry):
            raise ValueError("secret memory cannot leave through proposal egress")
        return await self._proposal_writer.propose(user_id=user_id, entry=entry)

    async def notify_forget(
        self, *, user_id: str, entry_id: str, content_hash: str
    ) -> dict[str, str]:
        """Ask every bounded provider to erase a hash-identified memory.

        A missing notifier is treated as read-only. The caller receives one
        status per provider so a local success cannot hide an external copy.
        """
        if not isinstance(user_id, str) or not user_id.strip():
            raise ValueError("memory forget needs a non-empty user_id")
        if not isinstance(entry_id, str) or not entry_id.strip():
            raise ValueError("memory forget needs a non-empty entry_id")
        if not isinstance(content_hash, str) or not content_hash.strip():
            raise ValueError("memory forget needs a non-empty content_hash")

        async def notify(provider: MemoryProvider) -> tuple[str, str]:
            name = str(
                getattr(provider, "provider_id", None)
                or {
                    "ConductorPgMemory": "conductor_pg",
                    "FarmRetrieval": "farm",
                    "MemfwMemory": "memfw",
                }.get(type(provider).__name__, type(provider).__name__.lower())
            )
            method = getattr(provider, "notify_forget", None)
            if not callable(method):
                return name, "read_only"
            try:
                result = await asyncio.wait_for(
                    method(
                        user_id=user_id,
                        entry_id=entry_id,
                        content_hash=content_hash,
                    ),
                    timeout=self._timeout,
                )
            except asyncio.CancelledError:
                raise
            except ReadOnlyMemoryProviderError:
                return name, "read_only"
            except Exception as exc:
                logger.debug("Memory provider forget failed (%s)", type(exc).__name__)
                return name, "unavailable"
            if not isinstance(result, str) or result not in {
                "deleted",
                "read_only",
                "unavailable",
                "not_configured",
                "unsupported",
            }:
                return name, "unavailable"
            return name, result

        results = await asyncio.gather(
            *(notify(provider) for provider in self._providers)
        )
        statuses: dict[str, str] = {}
        for name, status in results:
            if name in statuses:
                suffix = 2
                while f"{name}_{suffix}" in statuses:
                    suffix += 1
                name = f"{name}_{suffix}"
            statuses[name] = status
        return statuses


def memory_hit_from_mapping(value: Mapping[str, object]) -> MemoryHit:
    """Parse a provider row without accepting malformed fields or hidden defaults."""
    source = value.get("source")
    key = value.get("key")
    text = value.get("text")
    score = value.get("score")
    provenance = value.get("provenance", {})
    tier = value.get("tier")
    workspace_id = value.get("workspace_id")
    if not isinstance(source, str) or not isinstance(key, str) or not isinstance(text, str):
        raise ValueError("memory hit requires string source, key, and text")
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        raise ValueError("memory hit score must be numeric")
    if not isinstance(provenance, Mapping):
        raise ValueError("memory hit provenance must be an object")
    if tier is not None and not isinstance(tier, str):
        raise ValueError("memory hit tier must be a string or null")
    if workspace_id is not None and not isinstance(workspace_id, str):
        raise ValueError("memory hit workspace_id must be a string or null")
    return MemoryHit(source, key, text, float(score), provenance, tier, workspace_id)
