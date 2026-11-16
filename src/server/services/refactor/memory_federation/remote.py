"""Read-only MemFW HTTP client for the ``memfw-http/2`` contract.

The contract sections that shape this module:

* section 3 -- ``POST /search`` takes ``{"query", "limit", "tenant"?}`` and
  answers with a **normalised** ``score`` in ``[0, 1]``; the raw bm25 lands in
  ``raw_score``. The client uses that score as it is. Converting it a second
  time (``1 / (1 + abs(score))``) used to invert the ranking and pushed the
  best hit to the bottom.
* section 4 -- ``query`` is at most 512 characters and ``limit`` is 1..50.
* section 6 -- every failure is ``{"contract": ..., "error": {"code": ...}}``
  and the code has to reach the caller instead of a generic exception. A
  transport failure has no such envelope, so it is mapped onto the contract's
  own local codes -- ``timeout`` and ``unreachable`` -- and the transport
  exception is dropped rather than chained.
* section 8 -- v2 has no remote forget at all, so the client says so
  explicitly instead of pretending the call failed.

Every request line is the validated base plus the contract's fixed search path:
the constructor runs the shared validation boundary (``health._validated_base``)
and keeps only the base it returns, so a configured endpoint that is not a root
address -- or that carries a userinfo, a query or a fragment -- is refused
before this object exists. No caller can build a request from the raw
configured string.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx

from src.server.contracts.memory_concept import concept_ontology

from .errors import MemfwContractError, parse_error_code
from .health import CONTRACT_MAJOR, SEARCH_PATH, _validated_base, contract_major
from .models import MemoryEntry, MemoryHit
from .provider import ReadOnlyMemoryProviderError

logger = logging.getLogger(__name__)

#: Contract section 4: the client asks for at most 50 results.
MAX_RESULTS = 50
#: Contract section 3: ``summary`` is at most 500 characters.
MAX_TEXT = 500
#: Contract section 4: ``query`` is 1..512 characters.
MAX_QUERY_LENGTH = 512


def resolve_memfw_token(purpose: str = "READONLY") -> str:
    prefix = f"MEMFW_{purpose}"
    token = os.environ.get(f"{prefix}_TOKEN", "").strip()
    if token:
        return token
    token_file = os.environ.get(f"{prefix}_TOKEN_FILE", "").strip()
    if not token_file:
        return ""
    try:
        return Path(token_file).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _json_or_none(response: httpx.Response) -> Any:
    """The decoded body, or ``None`` when the body is not JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def _normalised_score(value: Any) -> float:
    """The server's score, clamped into ``[0, 1]``.

    Contract section 3: the server already normalises the score, so it is used
    as it is. A value the client cannot read is treated as no relevance.
    """
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(1.0, max(0.0, number))


_LABEL_TEXT_FIELDS = ("sensitivity", "visibility", "project", "repo")


def _policy_labels(row: Mapping[str, Any]) -> dict[str, Any]:
    """The optional privacy/confidence/project labels of a remote row, type-checked."""
    labels: dict[str, Any] = {}
    for key in _LABEL_TEXT_FIELDS:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            labels[key] = value.strip()[:MAX_TEXT]
    tags = row.get("tags")
    if isinstance(tags, list):
        labels["tags"] = [tag[:MAX_TEXT] for tag in tags if isinstance(tag, str)][:64]
    confidence = _optional_float(row.get("confidence"))
    if confidence is not None:
        labels["confidence"] = confidence
    return labels


def _optional_float(value: Any) -> float | None:
    """A numeric field that may be absent, such as ``raw_score``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _related_ids(value: Any) -> list[str]:
    """The ``related`` memory ids of one row."""
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


class RemoteMemfwMemory:
    """The read-only remote source, built from a validated root address.

    ``endpoint`` is the operator's configured URL. The constructor reduces it
    through the shared validation boundary and keeps only the resulting
    ``base``, which is the one normalized base every request is rebuilt from:
    a configured URL that is not a root address, or that carries a userinfo, a
    query or a fragment, is refused here rather than being sent somewhere.
    """

    provider_id = "memfw"

    def __init__(
        self,
        endpoint: str,
        *,
        owner_id: str,
        token: str | None = None,
        timeout: float = 1.0,
        client: httpx.AsyncClient | None = None,
        tenant: str | None = None,
    ) -> None:
        if not owner_id.strip():
            raise ValueError("remote MemFW needs an owner_id")
        if not endpoint.startswith(("http://", "https://")):
            raise ValueError("remote MemFW endpoint must be HTTP(S)")
        # The shared validation boundary decides whether a request may be made
        # at all, and returns the one normalized base every request is rebuilt
        # from. Wiring the check through here -- rather than repeating it --
        # means a client built anywhere (integration, the forget path) cannot
        # end up holding a raw configured string to build a URL from.
        base = _validated_base(endpoint)
        if base is None:
            raise ValueError(
                "remote MemFW endpoint must be a root address "
                "(scheme://host[:port] with no path)"
            )
        self.base = base
        self.owner_id = owner_id
        self.token = token if token is not None else resolve_memfw_token("READONLY")
        if not self.token:
            raise ValueError("MEMFW_READONLY_TOKEN is required for remote MemFW")
        self.timeout = timeout
        self.tenant = tenant.strip() if isinstance(tenant, str) and tenant.strip() else None
        self._client = client

    def _fail(self, error_code: str) -> MemfwContractError:
        """The failure for one call, carrying only the contract error code.

        The log line holds the error code and nothing else: no response body,
        no token and no host path. It is a debug line, because a broken
        endpoint fails on every recall and the federation already treats a
        provider failure as debug-level; the integration layer owns the
        rate-limited warning.
        """
        logger.debug("MemFW search failed with error_code=%s", error_code)
        return MemfwContractError(error_code)

    async def _post(self, payload: dict[str, object]) -> dict[str, Any]:
        headers = {"X-Memfw-Token": self.token, "Content-Type": "application/json"}
        # The request line is the validated base plus the one fixed search path,
        # so the configured string -- with whatever path it carried -- never
        # reaches the client or a transport that would log it.
        search_url = self.base + SEARCH_PATH

        async def request() -> httpx.Response:
            if self._client is not None:
                return await self._client.post(
                    search_url, json=payload, headers=headers
                )
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=False
            ) as client:
                return await client.post(search_url, json=payload, headers=headers)

        try:
            # httpx's per-operation timeouts do not bound a peer that drips bytes
            # forever, so the complete request has its own deadline as well.
            response = await asyncio.wait_for(request(), timeout=self.timeout)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            # Contract section 6: a call that never got an answer is reported
            # with the contract's own code. The transport exception carries
            # the URL and whatever the peer said on the way, so it is
            # dropped: the error is raised `from None` and no exception chain
            # keeps the text alive.
            raise self._fail("timeout") from None
        except httpx.TransportError:
            raise self._fail("unreachable") from None
        if not 200 <= response.status_code < 300:
            code = parse_error_code(_json_or_none(response), response.status_code)
            raise self._fail(code)
        body = _json_or_none(response)
        if not isinstance(body, dict):
            raise self._fail("invalid_response")
        if contract_major(body.get("contract")) != CONTRACT_MAJOR:
            raise self._fail("invalid_response")
        if not isinstance(body.get("results"), list):
            raise self._fail("invalid_response")
        return body

    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]:
        del workspace_id, thread_id
        if user_id != self.owner_id or (tiers and "user" not in tiers):
            return []
        # The query is trimmed and truncated here as well as upstream: the
        # remote source must not depend on the caller having bounded it.
        text = (query.strip() if isinstance(query, str) else "")[:MAX_QUERY_LENGTH]
        if not text:
            return []
        try:
            requested = int(k)
        except (TypeError, ValueError):
            return []
        if requested <= 0:
            return []
        limit = max(1, min(requested, MAX_RESULTS))
        payload: dict[str, Any] = {"query": text, "limit": limit}
        if self.tenant:
            payload["tenant"] = self.tenant
        body = await self._post(payload)
        mode = body.get("mode")
        degraded = body.get("degraded")
        hits: list[MemoryHit] = []
        for row in body["results"][:limit]:
            if not isinstance(row, Mapping):
                continue
            identifier = row.get("id")
            title = row.get("title")
            summary = row.get("summary")
            if not isinstance(identifier, str) or not isinstance(title, str):
                continue
            text_value = f"{title}: {summary}" if isinstance(summary, str) and summary else title
            memfw_tier = row.get("tier")
            hits.append(
                MemoryHit(
                    source="memfw",
                    key=identifier,
                    text=text_value[:MAX_TEXT],
                    score=_normalised_score(row.get("score")),
                    provenance={
                        "content_hash": str(row.get("content_hash") or ""),
                        "source": str(row.get("source") or ""),
                        "tenant": str(row.get("tenant") or ""),
                        "raw_score": _optional_float(row.get("raw_score")),
                        "valid_from": row.get("valid_from"),
                        "valid_to": row.get("valid_to"),
                        "superseded_by": row.get("superseded_by"),
                        "related": _related_ids(row.get("related")),
                        "mode": mode,
                        "degraded": degraded,
                        "memfw_tier": memfw_tier if isinstance(memfw_tier, str) else None,
                        "memory_metadata": {
                            "layer": row.get("layer"),
                            "tier": memfw_tier,
                            "tenant": row.get("tenant"),
                            # The ontology the remote source declared for
                            # this memory: what kind of claim it is and the
                            # concepts it is about. Normalized rather than
                            # copied, so an unusable value is dropped instead
                            # of reaching the Worker as a broken concept. A
                            # memory that names a concept must still name it
                            # when it is read back, or the audit would report
                            # a memory as naming nothing.
                            **concept_ontology(row),
                            # Privacy, confidence and project labels the
                            # server declared, so the Ticket gate
                            # (ticket_memory_policy) can keep a private or
                            # unrelated memory out of a Ticket prompt.
                            **_policy_labels(row),
                        },
                    },
                    # MemFW's hot/cold storage tiers are not Conductor recall
                    # tiers. Every remote hit belongs to the Conductor user tier.
                    tier="user",
                )
            )
        return hits

    async def propose(self, user_id: str, entry: MemoryEntry) -> str:
        del user_id, entry
        raise ReadOnlyMemoryProviderError("remote MemFW is read-only")

    async def notify_forget(
        self, *, user_id: str, entry_id: str, content_hash: str
    ) -> str:
        del user_id, entry_id, content_hash
        # Contract section 8: v2 offers no remote forget. The capability is
        # missing, not failing, so the caller is told "unsupported".
        return "unsupported"


__all__ = [
    "MAX_QUERY_LENGTH",
    "MAX_RESULTS",
    "MAX_TEXT",
    "RemoteMemfwMemory",
    "resolve_memfw_token",
]
