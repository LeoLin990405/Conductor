"""Turn-context wiring for explicitly bound, read-only memory sources."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any

from src.config.models import MemoryFederationUserBinding

from .conductor import ConductorPgMemory
from .health import (
    REASON_LOCAL_MEMFW_UNAVAILABLE,
    _validated_base,
    check_contract,
    record_memory_failure,
    safe_endpoint,
)
from .memfw_reader import MemfwMemory
from .models import MemoryHit
from .provider import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    MAX_QUERY_LENGTH,
    MemoryFederation,
    memory_hit_metadata,
)
from .remote import RemoteMemfwMemory, resolve_memfw_token

logger = logging.getLogger(__name__)
_remote_source_warning_emitted = False
_remote_recall_warning_at: float | None = None
_REMOTE_RECALL_WARNING_INTERVAL_SECONDS = 300.0


def _error_code_of(exc: Exception) -> str:
    """The contract error code of a failure, never its text.

    A failure that carries a contract code keeps it. A local timeout is a
    timeout. Anything else is an unreachable peer -- the two things a client
    can say about a source it could not use. The text of the exception never
    reaches a log line: it may carry a URL, a query or a peer message.
    """
    code = getattr(exc, "error_code", None)
    if isinstance(code, str) and code:
        return code
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "unreachable"


def _warn_remote_source_unavailable(exc: Exception) -> None:
    global _remote_source_warning_emitted
    if _remote_source_warning_emitted:
        return
    logger.warning(
        "Remote MemFW memory source unavailable (%s); retaining Conductor PG recall",
        _error_code_of(exc),
    )
    _remote_source_warning_emitted = True


def _warn_remote_recall_failed(exc: Exception) -> None:
    global _remote_recall_warning_at
    now = time.monotonic()
    if (
        _remote_recall_warning_at is not None
        and now - _remote_recall_warning_at < _REMOTE_RECALL_WARNING_INTERVAL_SECONDS
    ):
        return
    # A contract failure carries its own code, which is what an operator needs
    # to tell a conflict from an unreachable peer. The code is the only thing
    # this line reports: the text of the failure stays out of the log.
    logger.warning(
        "Remote MemFW recall failed (%s); using Conductor PG recall",
        _error_code_of(exc),
    )
    _remote_recall_warning_at = now


def _configured_read_token() -> str:
    """The configured MemFW read token, or an empty string.

    Whether an operator configured a read token is a local fact: it is read
    from the environment, never from the endpoint. Nothing is sent over the
    network to find out.
    """
    try:
        return resolve_memfw_token("READONLY")
    except Exception:
        return ""


async def _build_remote_source(
    *, endpoint: str, user_id: str, tenant: str | None
) -> RemoteMemfwMemory | None:
    """Negotiate the contract, then build the remote source for a URL binding.

    Contract section 1: the client checks the major version once per endpoint
    before it uses a remote source.

    - No read token is a local configuration fact, so no request is made at
      all and the caller falls back to Conductor PG recall. This is a debug
      line, never a warning: an operator who never configured MemFW must not
      be warned about it on every turn.
    - An endpoint that is not a root address -- a path of its own, a userinfo,
      a query or a fragment -- is refused before any request, through the same
      validation boundary the health probe uses, and the caller falls back to
      Conductor PG recall.
    - An incompatible endpoint is reported once per endpoint by ``health.py``;
      an unreachable one is a debug line there too. Integration must not add
      a warning of its own on top of that.
    """
    if not _configured_read_token():
        logger.debug(
            "No MemFW read token is configured; using Conductor PG recall only"
        )
        return None
    # One validated normalized base per endpoint: the probe below and the
    # client it authorizes both rebuild every request line from it, so the
    # configured string -- with whatever path it carried -- reaches neither a
    # request line nor a transport that would log one. A URL that is not a root
    # address is refused here with the same stable reason the probe answers,
    # and nothing is sent at all.
    base = _validated_base(endpoint)
    if base is None:
        logger.debug(
            "MemFW endpoint configuration refused (%s); "
            "using Conductor PG recall only",
            safe_endpoint(endpoint),
        )
        return None
    state = await check_contract(base)
    if not state.compatible:
        logger.debug(
            "MemFW endpoint %s is not usable (compatible=%s, reachable=%s, "
            "contract_major=%s); using Conductor PG recall only",
            safe_endpoint(endpoint),
            state.compatible,
            state.reachable,
            state.major if state.major is not None else "<unknown>",
        )
        return None
    return RemoteMemfwMemory(base, owner_id=user_id, tenant=tenant)


class _RemoteRecallAttempt:
    def __init__(self, provider: RemoteMemfwMemory) -> None:
        self._provider = provider
        self.failure: Exception | None = None

    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]:
        try:
            return await self._provider.recall(
                user_id=user_id,
                query=query,
                k=k,
                tiers=tiers,
                workspace_id=workspace_id,
                thread_id=thread_id,
            )
        except asyncio.CancelledError:
            self.failure = TimeoutError()
            raise
        except Exception as exc:
            self.failure = exc
            raise


def _as_context_rows(hits: Sequence[MemoryHit]) -> list[Mapping[str, Any]]:
    return [
        {
            "entry_key": hit.key,
            "index_text": hit.text,
            "content_hash": str(
                hit.provenance.get("content_hash")
                or hit.provenance.get("document_hash")
                or ""
            ),
            "status": "active",
            "metadata": memory_hit_metadata(hit),
            "source": hit.source,
            "provenance": dict(hit.provenance),
        }
        for hit in hits
    ]


async def configured_memory_search(
    *,
    user_id: str,
    binding: MemoryFederationUserBinding,
    workspace_id: str | None,
    thread_id: str | None,
):
    """Build a per-turn federation from this user's immutable config binding."""
    pg = ConductorPgMemory(recall_budget_seconds=DEFAULT_PROVIDER_TIMEOUT_SECONDS)
    providers = [pg]
    memfw = None
    if binding.memfw_url:
        # Contract section 1: negotiate the contract before a remote source
        # exists. An unusable endpoint leaves PG recall in place.
        try:
            memfw = await _build_remote_source(
                endpoint=binding.memfw_url,
                user_id=user_id,
                tenant=binding.memfw_tenant,
            )
        except Exception as exc:
            _warn_remote_source_unavailable(exc)
    else:
        try:
            memfw = await asyncio.wait_for(
                asyncio.to_thread(
                    MemfwMemory, root=binding.memfw_path, owner_id=user_id
                ),
                timeout=1.0,
            )
        except Exception:
            # The local vault constructor failed before any recall could be
            # made: Conductor PG recall is retained, and the drop is counted
            # under a fixed reason instead of staying a debug line. The
            # exception text can carry a filesystem path, so only the reason
            # code reaches the log.
            record_memory_failure(REASON_LOCAL_MEMFW_UNAVAILABLE)
            logger.warning(
                "MemFW memory source unavailable reason=%s",
                REASON_LOCAL_MEMFW_UNAVAILABLE,
            )
    primary = os.environ.get("MEMFW_PRIMARY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    remote_primary = memfw if primary and binding.memfw_url else None
    if memfw is not None:
        # Opt-in: MemFW becomes the primary recall source while Conductor's
        # PG memory remains the proposal destination. If MemFW is unavailable,
        # PG recall is retained rather than silently returning no memory.
        if primary:
            providers = [memfw]
        else:
            providers.append(memfw)
    federation = MemoryFederation(
        providers,
        proposal_writer=pg,
        provider_timeout_seconds=DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    )
    pg_fallback = (
        MemoryFederation(
            [pg],
            proposal_writer=pg,
            provider_timeout_seconds=DEFAULT_PROVIDER_TIMEOUT_SECONDS,
        )
        if remote_primary is not None
        else None
    )

    # Cache only within this per-turn closure. Besides bounding stale data to one
    # turn, this makes repeated user-tier selection share the same remote call.
    user_recall_cache: dict[
        tuple[str, str | None, str | None],
        tuple[int, Sequence[Mapping[str, Any]]],
    ] = {}

    async def search(
        requested_user: str,
        tier: str,
        query: str,
        limit: int,
        requested_thread: str | None = None,
        requested_workspace: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        if requested_user != user_id:
            return []
        text = (query or "").strip()[:MAX_QUERY_LENGTH]
        effective_workspace = requested_workspace or workspace_id
        effective_thread = requested_thread or thread_id
        cache_key = (text, effective_workspace, effective_thread)
        cached = user_recall_cache.get(cache_key)
        if tier == "user" and memfw is not None and cached is not None:
            cached_limit, cached_rows = cached
            if cached_limit >= limit:
                return cached_rows[:limit]
        recall_federation = federation
        attempt = None
        if remote_primary is not None:
            attempt = _RemoteRecallAttempt(remote_primary)
            recall_federation = MemoryFederation(
                [attempt],
                proposal_writer=pg,
                provider_timeout_seconds=DEFAULT_PROVIDER_TIMEOUT_SECONDS,
            )
        hits = await recall_federation.recall(
            user_id=user_id,
            query=text,
            k=limit,
            tiers=(tier,),
            workspace_id=effective_workspace,
            thread_id=effective_thread,
        )
        if (
            attempt is not None
            and attempt.failure is not None
            and pg_fallback is not None
        ):
            _warn_remote_recall_failed(attempt.failure)
            hits = await pg_fallback.recall(
                user_id=user_id,
                query=text,
                k=limit,
                tiers=(tier,),
                workspace_id=effective_workspace,
                thread_id=effective_thread,
            )
        rows = _as_context_rows(hits)
        if tier == "user" and memfw is not None:
            user_recall_cache[cache_key] = (limit, rows)
        return rows

    return search


async def notify_configured_forget(
    *, user_id: str, entry_id: str, content_hash: str
) -> Mapping[str, str]:
    """Ask the active, bounded provider set to acknowledge a tombstone."""
    from src.config import settings
    from src.config.features import system_flag
    from src.server.services.features import user_feature_enabled

    providers = [ConductorPgMemory()]
    unavailable: dict[str, str] = {}
    remote_forget = False
    try:
        binding = settings.get_memory_federation_binding(user_id)
    except Exception:
        binding = None
        unavailable["memfw"] = "unavailable"
    if binding is not None:
        # Contract section 8: the remote MemFW has no forget endpoint at all.
        # A URL binding therefore answers "unsupported", never "unavailable"
        # and never "read_only" -- the reason is structural, not an outage.
        remote_forget = bool(binding.memfw_url)
        try:
            if system_flag("memory_federation").enabled and await user_feature_enabled(
                user_id, "memory_federation"
            ):
                if binding.memfw_url:
                    try:
                        providers.append(
                            RemoteMemfwMemory(
                                binding.memfw_url,
                                owner_id=user_id,
                                tenant=binding.memfw_tenant,
                            )
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.debug(
                            "Remote MemFW forget source unavailable (%s); "
                            "remote memfw has no forget endpoint",
                            type(exc).__name__,
                        )
                else:
                    providers.append(
                        await asyncio.wait_for(
                            asyncio.to_thread(
                                MemfwMemory, root=binding.memfw_path, owner_id=user_id
                            ),
                            timeout=1.0,
                        )
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            unavailable["memfw"] = "unavailable"
    from .provider import MemoryFederation

    result = await MemoryFederation(providers).notify_forget(
        user_id=user_id, entry_id=entry_id, content_hash=content_hash
    )
    merged = {**result, **unavailable}
    if remote_forget:
        merged["memfw"] = "unsupported"
        merged["memfw_reason"] = "remote memfw has no forget endpoint"
    return merged
