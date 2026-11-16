"""Contract version negotiation against the MemFW ``/health`` endpoint.

Contract section 1: every response carries ``"contract": "memfw-http/2"``, and a
client checks the major version once per endpoint before it builds a remote
source. The answer is cached for five minutes; a *failed* probe is not cached,
so the next recall tries again instead of giving up on a service that was down
a moment ago.

Everything the service answers is untrusted input. A log line keeps the
endpoint down to scheme, host and port, and the contract down to the major
version that was validated -- never the raw contract string, and never the
path, the query or the fragment of the URL.

The configured URL is validated before anything is sent, and it may only be a
ROOT address: ``scheme://host[:port]`` with an empty path or ``/``. One that
carries a userinfo, a query, a fragment or a path of its own cannot be reduced
to a bare peer, so it is refused with a stable reason and no request is made.
After validation there is exactly one normalized base, and every request line
is rebuilt from that base plus the contract's fixed path for that call, so the
raw configured string never reaches the client or a transport that would log
it.

This module also owns the process-local counters for the memory provider
boundaries that fail open. They live beside the health cache because both are
worker-local visibility rather than cross-worker truth, and both are read back
through ``memory_failure_status()`` on the server's ``/health`` output.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .errors import CONTRACT_MAJOR, CONTRACT_NAME, contract_major

logger = logging.getLogger(__name__)

#: Contract section 4: ``/health`` gets one second.
HEALTH_TIMEOUT_SECONDS = 1.0
#: Contract section 1: the probe answer is cached for five minutes.
HEALTH_CACHE_SECONDS = 300.0
#: The one fixed path the probe requests. The request line is rebuilt from the
#: validated base plus this path, so the configured string -- with whatever
#: path, query or fragment it carried -- never reaches the client.
HEALTH_PATH = "/health"
#: The one fixed path the search request targets (contract section 3). Every
#: request is the validated base plus the fixed path of its own call and
#: nothing else, so this constant is the only place ``/search`` is spelled.
SEARCH_PATH = "/search"
#: The stable reason a configured endpoint is refused before any request: its
#: URL was not a root address -- it carried a userinfo, a query, a fragment or
#: a path -- so it cannot be reduced to a bare peer and must not be sent
#: anywhere.
REASON_BAD_CONFIG = "bad_config"

#: Fixed reason codes for the memory provider failures that used to be
#: debug-only. A WARNING log line and the process counter both carry one of
#: these tokens and nothing from the exception text, so an operator can alert on
#: the code without the log becoming a free-text channel.
REASON_PROVIDER_RECALL_FAILED = "provider_recall_failed"
REASON_LOCAL_MEMFW_UNAVAILABLE = "local_memfw_unavailable"
REASON_HEALTH_PROBE_FAILED = "health_probe_failed"
#: Memory context selection -- the recall path outside the federation -- failed
#: and the turn runs without a memory block. The failure is counted here too,
#: because the caller keeps the turn alive and nothing else reports the drop.
REASON_MEMORY_SELECTION_FAILED = "memory_selection_failed"

_FAILURE_REASON_CODES: frozenset[str] = frozenset(
    {
        REASON_PROVIDER_RECALL_FAILED,
        REASON_LOCAL_MEMFW_UNAVAILABLE,
        REASON_HEALTH_PROBE_FAILED,
        REASON_MEMORY_SELECTION_FAILED,
    }
)


class MemoryFailureCounter:
    """Process-local counts of memory provider failures.

    Counters live beside the health cache because both are worker-local
    visibility rather than cross-worker truth: another process cannot see them,
    so a status probe answers for this worker only.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def record(self, reason: str) -> None:
        """Increment one fixed reason code; an unknown code is refused."""
        if reason not in _FAILURE_REASON_CODES:
            raise ValueError(f"unknown memory failure reason: {reason}")
        with self._lock:
            self._counts[reason] = self._counts.get(reason, 0) + 1

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()

    def as_dict(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)


#: Process-wide failure counters exposed through ``memory_failure_status()``.
MEMORY_FAILURES = MemoryFailureCounter()


def record_memory_failure(reason: str) -> None:
    """Count one memory provider failure under a fixed reason code."""
    MEMORY_FAILURES.record(reason)


def memory_failure_status() -> dict[str, dict[str, int]]:
    """The process-local memory failure counts for this worker."""
    return {"failure_counts": MEMORY_FAILURES.as_dict()}


def safe_endpoint(endpoint: str) -> str:
    """The endpoint reduced to scheme, host and port.

    The endpoint comes from an operator's configuration, and a URL may carry a
    password, a path, a query string with a token, or a fragment. A log line
    needs enough to identify the peer and nothing else, so everything but the
    scheme, the host and the port is dropped -- whether or not the URL carries
    credentials. An endpoint that cannot be read at all becomes a fixed
    placeholder rather than an echo of whatever was configured.
    """
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return "<endpoint>"
    if not parts.scheme or not host:
        return "<endpoint>"
    return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"


def _validated_base(endpoint: str) -> str | None:
    """The endpoint reduced to ``scheme://host[:port]``, or ``None`` to refuse.

    This is the validation boundary, and a configured endpoint may only be a
    ROOT address: ``scheme://host[:port]`` with an empty path or ``/``. A
    configured URL may also carry a password in its userinfo, a query string
    holding a token, or a fragment, and none of those may ride along on a
    request line or reach a transport that logs one. A URL that carries any of
    them, or a path of its own, is **refused** (``None``): the caller treats it
    as a bad configuration and sends nothing at all. A URL that cannot be read
    into a scheme, a host and a port is refused for the same reason.

    The reduced base is the one normalized base every request is rebuilt from
    -- the health probe, the remote client and the write bridge each append
    their own fixed contract path to it. A URL that merely carries a path is
    therefore not quietly rewritten into a request against some other path: it
    is refused, so a misconfigured endpoint cannot pass a health check and then
    have its search and related calls land on a URL the service does not serve.
    Unlike ``safe_endpoint`` this is not a logging helper: it decides whether a
    request may be made at all, so it fails closed rather than echoing a
    placeholder.

    This is the single validation boundary for a configured MemFW endpoint: the
    health probe, the remote client and the write bridge all call it, so all of
    them refuse the same URLs and rebuild from the same
    ``scheme://host[:port]``. An IPv6 literal keeps its square brackets
    (``http://[::1]:8765``), because a host without them is not a valid
    authority.
    """
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return None
    if "@" in (parts.netloc or "") or parts.query or parts.fragment:
        return None
    # A configured endpoint may only be a root address. Every request is this
    # base plus one fixed contract path, so a configured path has nowhere to
    # go: dropping it would silently retarget the request, and keeping it would
    # ask a URL the service does not serve. It is refused instead.
    if parts.path not in ("", "/"):
        return None
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    # ``hostname`` reports an IPv6 literal without its square brackets (``::1``
    # for ``[::1]``), so the rebuild wraps it back: ``http://[::1]:8765`` is a
    # valid authority, ``http://::1:8765`` is not. A hostname or an IPv4 literal
    # carries no colon and is left exactly as it is.
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}:{port}" if port else f"{parts.scheme}://{host}"


@dataclass(frozen=True)
class HealthState:
    """What the client learned about one endpoint.

    Only the **major** version of the contract is kept. The raw contract
    string comes from the service and is never needed: the major version
    answers "which version does that endpoint speak?", and keeping the raw
    string would mean keeping a value that has no safe place to go.
    """

    compatible: bool
    reachable: bool = True
    major: int | None = None
    #: A stable reason the endpoint was refused before any request, else ``None``.
    #: ``bad_config`` means the configured URL was not a root address -- it
    #: carried a userinfo, a query, a fragment or a path of its own -- so
    #: nothing was sent. A probe that ran leaves this ``None`` even when it
    #: failed.
    reason: str | None = None


class HealthCache:
    """Per-endpoint health cache with a five-minute window.

    A successful probe is cached, including a version mismatch -- the service
    is reachable, it just does not speak this contract. A failed probe is
    **not** cached: the endpoint is treated as temporarily unavailable and the
    next recall tries again.
    """

    def __init__(
        self,
        *,
        cache_seconds: float = HEALTH_CACHE_SECONDS,
        timeout: float = HEALTH_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cache_seconds = cache_seconds
        self._timeout = timeout
        self._clock = clock
        self._entries: dict[str, tuple[float, HealthState]] = {}
        self._warned: set[str] = set()

    def clear(self) -> None:
        """Forget every cached answer and every emitted warning."""
        self._entries.clear()
        self._warned.clear()

    async def check(
        self, endpoint: str, *, client: httpx.AsyncClient | None = None
    ) -> HealthState:
        """The contract state of one endpoint, probing only when needed."""
        base = _validated_base(endpoint)
        if base is None:
            # A URL that is not a root address -- it carries a userinfo, a
            # query, a fragment or a path of its own -- is refused here, before
            # anything is sent: the request line must never be able to carry it,
            # and neither may a log line. The reason is stable and the reduced
            # peer is all that is named. A refused config is not cached -- it is
            # a local fact, cheap to re-check, and a failed probe is deliberately
            # not cached either.
            logger.debug(
                "MemFW endpoint configuration refused (%s); "
                "using Conductor PG recall only",
                safe_endpoint(endpoint),
            )
            return HealthState(
                compatible=False, reachable=False, reason=REASON_BAD_CONFIG
            )
        moment = self._clock()
        cached = self._entries.get(base)
        if cached is not None and moment - cached[0] < self._cache_seconds:
            return cached[1]
        state = await self._probe(base, client=client)
        if state.reachable:
            self._entries[base] = (moment, state)
        return state

    async def _probe(
        self, endpoint: str, *, client: httpx.AsyncClient | None
    ) -> HealthState:
        # ``endpoint`` is the already-validated ``scheme://host[:port]`` base.
        # The request line is that base plus the one fixed health path, so the
        # configured string -- with whatever path, query or fragment it carried
        # -- is never handed to the client or to a transport that logs it.
        health_url = endpoint + HEALTH_PATH

        async def request() -> httpx.Response:
            if client is not None:
                return await client.get(health_url)
            async with httpx.AsyncClient(
                timeout=self._timeout, follow_redirects=False
            ) as session:
                return await session.get(health_url)

        try:
            response = await asyncio.wait_for(request(), timeout=self._timeout)
        except Exception:
            # A transport error or an overrun of the bounded timeout fails
            # closed without raising, so nothing upstream ever sees the drop.
            # The exception text may carry a URL, a query or a peer message, so
            # only the fixed reason reaches the log.
            record_memory_failure(REASON_HEALTH_PROBE_FAILED)
            logger.warning(
                "MemFW health probe failed for %s; treating it as temporarily "
                "unavailable reason=%s",
                safe_endpoint(endpoint),
                REASON_HEALTH_PROBE_FAILED,
            )
            return HealthState(compatible=False, reachable=False)
        if response.status_code != 200:
            # A service that answers at all but not 200 -- a 503 above all -- is
            # a drop the caller cannot see: the probe fails closed without
            # raising, so nothing upstream turns it into a visible failure. The
            # status code is the service's own number, the reduced peer is all
            # that is named of the endpoint, and the fixed reason is the only
            # thing an operator alerts on.
            record_memory_failure(REASON_HEALTH_PROBE_FAILED)
            logger.warning(
                "MemFW health probe for %s returned HTTP %s reason=%s",
                safe_endpoint(endpoint),
                response.status_code,
                REASON_HEALTH_PROBE_FAILED,
            )
            return HealthState(compatible=False, reachable=False)
        try:
            body = response.json()
        except Exception:
            # A service that answers 200 with a body the contract cannot read
            # is the same invisible drop: the probe fails closed without
            # raising. The service's answer is untrusted input, so only the
            # fixed reason is named.
            record_memory_failure(REASON_HEALTH_PROBE_FAILED)
            logger.warning(
                "MemFW health probe for %s returned an unreadable body reason=%s",
                safe_endpoint(endpoint),
                REASON_HEALTH_PROBE_FAILED,
            )
            return HealthState(compatible=False, reachable=False)
        contract = body.get("contract") if isinstance(body, dict) else None
        major = contract_major(contract)
        if major != CONTRACT_MAJOR:
            self._warn_once(endpoint, major)
            return HealthState(compatible=False, reachable=True, major=major)
        return HealthState(compatible=True, reachable=True, major=major)

    def _warn_once(self, endpoint: str, major: int | None) -> None:
        """Warn once per endpoint that it does not speak this contract.

        The warning names the **major version** the endpoint answered with and
        nothing else. The raw contract string is not logged: it comes from the
        service, it is not validated, and a log line must not be able to carry
        a value the service chose.
        """
        if endpoint in self._warned:
            return
        self._warned.add(endpoint)
        logger.warning(
            "MemFW endpoint %s speaks contract major %s, not %s/%d; "
            "keeping Conductor PG recall",
            safe_endpoint(endpoint),
            major if major is not None else "<unknown>",
            CONTRACT_NAME,
            CONTRACT_MAJOR,
        )


#: The process-wide cache. ``/health`` is per endpoint, so the same answer is
#: reused by every user bound to that endpoint.
HEALTH = HealthCache()


async def check_contract(
    endpoint: str,
    *,
    client: httpx.AsyncClient | None = None,
    cache: HealthCache | None = None,
) -> HealthState:
    """The contract state of one endpoint."""
    return await (cache or HEALTH).check(endpoint, client=client)


__all__ = [
    "CONTRACT_MAJOR",
    "HEALTH",
    "HEALTH_CACHE_SECONDS",
    "HEALTH_PATH",
    "HEALTH_TIMEOUT_SECONDS",
    "MEMORY_FAILURES",
    "REASON_BAD_CONFIG",
    "REASON_HEALTH_PROBE_FAILED",
    "REASON_LOCAL_MEMFW_UNAVAILABLE",
    "REASON_MEMORY_SELECTION_FAILED",
    "REASON_PROVIDER_RECALL_FAILED",
    "SEARCH_PATH",
    "HealthCache",
    "HealthState",
    "MemoryFailureCounter",
    "check_contract",
    "contract_major",
    "memory_failure_status",
    "record_memory_failure",
    "safe_endpoint",
]
