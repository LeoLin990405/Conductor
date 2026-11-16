"""Memory provider failures are WARNING-visible with fixed reason codes.

A provider that drops out used to leave a debug line behind, so federation
looked healthy while a source contributed nothing. Each boundary now logs a
WARNING that carries only a fixed reason token and bumps the process-local
``MEMORY_FAILURES`` counter, which ``memory_failure_status()`` and the existing
``/health`` output expose. The covered boundaries are the federation recall
failure, the local ``MemfwMemory`` constructor fallback in ``integration.py``,
the MemFW ``/health`` probe (a non-200 answer, a transport error or timeout, and
a body the contract cannot read), and the memory context selection failure in
``memory_context.py``.

Only monkeypatched fakes are used here: no network, no database.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import httpx
import pytest

import src.server.services.mcp_core.registry as mcp_registry
from src.config.models import MemoryFederationUserBinding
from src.server.app.utilities import health_check
from src.server.services.refactor.memory_federation import (
    MemoryEntry,
    MemoryFederation,
    MemoryHit,
)
from src.server.services.refactor.memory_federation import integration
from src.server.services.refactor.memory_federation.health import (
    REASON_HEALTH_PROBE_FAILED,
    REASON_LOCAL_MEMFW_UNAVAILABLE,
    REASON_MEMORY_SELECTION_FAILED,
    REASON_PROVIDER_RECALL_FAILED,
    HealthCache,
    HealthState,
    memory_failure_status,
    record_memory_failure,
)

PROVIDER_LOGGER = "src.server.services.refactor.memory_federation.provider"
INTEGRATION_LOGGER = "src.server.services.refactor.memory_federation.integration"
HEALTH_LOGGER = "src.server.services.refactor.memory_federation.health"
MEMORY_CONTEXT_LOGGER = "src.server.services.refactor.memory_context"


@pytest.fixture(autouse=True)
def _reset_failure_counters():
    """Counters are process-local, so every test starts from zero."""
    from src.server.services.refactor.memory_federation import health as mem_health

    mem_health.MEMORY_FAILURES.clear()
    yield
    mem_health.MEMORY_FAILURES.clear()


def _async(value):
    """An awaitable stub that answers ``value``."""

    from unittest.mock import AsyncMock

    return AsyncMock(return_value=value)


def _counts() -> dict[str, int]:
    return memory_failure_status()["failure_counts"]


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno >= logging.WARNING
    ]


class _FakeResponse:
    def __init__(self, status_code: int, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeClient:
    """Stands in for the transport the probe would otherwise open."""

    def __init__(
        self,
        response: _FakeResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self._response = response
        self._error = error
        self.urls: list[str] = []

    async def get(self, url: str) -> _FakeResponse:
        self.urls.append(url)
        if self._error is not None:
            raise self._error
        return self._response


class _SlowClient:
    """A transport that never answers inside the probe's bounded timeout."""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    async def get(self, url: str) -> _FakeResponse:
        await asyncio.sleep(self._delay)
        return _FakeResponse(200, {"contract": "memfw-http/2"})


def test_failure_status_starts_empty():
    assert _counts() == {}


def test_record_memory_failure_rejects_unknown_reason_codes():
    with pytest.raises(ValueError, match="unknown memory failure reason"):
        record_memory_failure("not_a_real_reason")
    assert _counts() == {}


def test_record_memory_failure_counts_each_reason_separately():
    record_memory_failure(REASON_PROVIDER_RECALL_FAILED)
    record_memory_failure(REASON_PROVIDER_RECALL_FAILED)
    record_memory_failure(REASON_HEALTH_PROBE_FAILED)
    assert _counts() == {
        REASON_PROVIDER_RECALL_FAILED: 2,
        REASON_HEALTH_PROBE_FAILED: 1,
    }


class _FailingRecallProvider:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]:
        raise self.error

    async def propose(self, user_id: str, entry: MemoryEntry) -> str:
        return f"proposal:{entry.key}"


class _OkRecallProvider:
    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]:
        return [
            MemoryHit(
                "conductor_pg",
                "kept",
                "kept fact",
                1.0,
                {"origin": "conductor_pg"},
                "user",
                None,
            )
        ]

    async def propose(self, user_id: str, entry: MemoryEntry) -> str:
        return f"proposal:{entry.key}"


@pytest.mark.asyncio
async def test_recall_failure_warns_and_counts(caplog):
    """A provider that raises still fails open, but no longer silently."""
    federation = MemoryFederation(
        [
            _FailingRecallProvider(RuntimeError("peer down token=leak")),
            _OkRecallProvider(),
        ],
        provider_timeout_seconds=0.2,
    )

    with caplog.at_level(logging.WARNING, logger=PROVIDER_LOGGER):
        rows = await federation.recall("user-a", "query", 5, ["user"])

    assert [row.key for row in rows] == ["kept"]
    warnings = _warnings(caplog)
    assert any(REASON_PROVIDER_RECALL_FAILED in line for line in warnings)
    # The exception text may carry a token or a URL; only the code is logged.
    assert "token=leak" not in caplog.text
    assert "RuntimeError" not in caplog.text
    assert _counts() == {REASON_PROVIDER_RECALL_FAILED: 1}


@pytest.mark.asyncio
async def test_recall_timeout_warns_and_counts(caplog):
    """A provider that overruns the bounded timeout is the same drop."""
    class _SlowProvider:
        async def recall(self, *args, **kwargs) -> list[MemoryHit]:
            await asyncio.sleep(5)
            return []

        async def propose(self, user_id: str, entry: MemoryEntry) -> str:
            return f"proposal:{entry.key}"

    federation = MemoryFederation([_SlowProvider()], provider_timeout_seconds=0.05)

    with caplog.at_level(logging.WARNING, logger=PROVIDER_LOGGER):
        rows = await federation.recall("user-a", "query", 5, ["user"])

    assert rows == []
    assert any(REASON_PROVIDER_RECALL_FAILED in line for line in _warnings(caplog))
    assert _counts() == {REASON_PROVIDER_RECALL_FAILED: 1}


@pytest.mark.asyncio
async def test_unusable_provider_result_warns_and_counts(caplog):
    """A provider answering a shape the contract cannot read is counted too."""

    class _BadShapeProvider:
        async def recall(self, *args, **kwargs):
            return "not-a-sequence"

        async def propose(self, user_id: str, entry: MemoryEntry) -> str:
            return f"proposal:{entry.key}"

    with caplog.at_level(logging.WARNING, logger=PROVIDER_LOGGER):
        rows = await MemoryFederation([_BadShapeProvider()]).recall(
            "user-a", "query", 5, ["user"]
        )

    assert rows == []
    assert any(REASON_PROVIDER_RECALL_FAILED in line for line in _warnings(caplog))
    assert _counts() == {REASON_PROVIDER_RECALL_FAILED: 1}


@pytest.mark.asyncio
async def test_healthy_recall_is_silent_and_uncounted(caplog):
    with caplog.at_level(logging.WARNING, logger=PROVIDER_LOGGER):
        rows = await MemoryFederation([_OkRecallProvider()]).recall(
            "user-a", "query", 5, ["user"]
        )

    assert [row.key for row in rows] == ["kept"]
    assert _warnings(caplog) == []
    assert _counts() == {}


class _PgOnly:
    """Stands in for Conductor's own PG recall: no database is touched."""

    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]:
        return [
            MemoryHit(
                "conductor_pg",
                "pg-key",
                "pg line",
                1.0,
                {"content_hash": "sha256:pg"},
                tiers[0] if tiers else "user",
                workspace_id,
            )
        ]

    async def propose(self, user_id: str, entry: MemoryEntry) -> str:
        return "proposal-id"


@pytest.mark.asyncio
async def test_local_memfw_constructor_failure_warns_and_counts(caplog, monkeypatch):
    """The local MemfwMemory fallback happens before any recall is attempted."""
    monkeypatch.setattr(integration, "ConductorPgMemory", lambda **kwargs: _PgOnly())

    def unavailable(*, root, owner_id):
        raise OSError(f"vault missing at {root} token=leak")

    monkeypatch.setattr(integration, "MemfwMemory", unavailable)

    with caplog.at_level(logging.WARNING, logger=INTEGRATION_LOGGER):
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_path="/missing/user-a"),
            workspace_id=None,
            thread_id=None,
        )
        rows = await search("user-a", "user", "query", 10)

    assert [row["source"] for row in rows] == ["conductor_pg"]
    warnings = _warnings(caplog)
    assert any(REASON_LOCAL_MEMFW_UNAVAILABLE in line for line in warnings)
    # Neither the exception text nor the configured path reaches the log.
    assert "token=leak" not in caplog.text
    assert "/missing/user-a" not in caplog.text
    assert _counts() == {REASON_LOCAL_MEMFW_UNAVAILABLE: 1}


@pytest.mark.asyncio
async def test_local_memfw_constructor_failure_counts_every_occurrence(
    caplog, monkeypatch
):
    monkeypatch.setattr(integration, "ConductorPgMemory", lambda **kwargs: _PgOnly())

    def unavailable(*, root, owner_id):
        raise OSError("vault unavailable")

    monkeypatch.setattr(integration, "MemfwMemory", unavailable)

    with caplog.at_level(logging.WARNING, logger=INTEGRATION_LOGGER):
        for _ in range(2):
            search = await integration.configured_memory_search(
                user_id="user-a",
                binding=MemoryFederationUserBinding(memfw_path="/missing/user-a"),
                workspace_id=None,
                thread_id=None,
            )
            await search("user-a", "user", "query", 10)

    assert _counts() == {REASON_LOCAL_MEMFW_UNAVAILABLE: 2}


@pytest.mark.asyncio
async def test_health_probe_503_warns_and_counts(caplog):
    """A 503 from /health is a drop the caller cannot see on its own."""
    cache = HealthCache(cache_seconds=300.0, timeout=1.0)
    client = _FakeClient(_FakeResponse(503))

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        state = await cache.check("http://memfw.invalid:18080", client=client)

    assert state == HealthState(compatible=False, reachable=False)
    assert client.urls == ["http://memfw.invalid:18080/health"]
    warnings = _warnings(caplog)
    assert any(REASON_HEALTH_PROBE_FAILED in line for line in warnings)
    assert any("503" in line for line in warnings)
    assert _counts() == {REASON_HEALTH_PROBE_FAILED: 1}


@pytest.mark.asyncio
async def test_health_probe_503_counts_every_probe(caplog):
    """A failed probe is not cached, so each one is counted."""
    cache = HealthCache(cache_seconds=300.0, timeout=1.0)
    client = _FakeClient(_FakeResponse(503))

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        for _ in range(3):
            await cache.check("http://memfw.invalid:18080", client=client)

    assert _counts() == {REASON_HEALTH_PROBE_FAILED: 3}
    assert len(client.urls) == 3


@pytest.mark.asyncio
async def test_health_probe_transport_error_warns_and_counts(caplog):
    """A transport error fails closed without raising; it must still count."""
    cache = HealthCache(cache_seconds=300.0, timeout=1.0)
    client = _FakeClient(
        error=httpx.ConnectError("dial tcp memfw.invalid:18080 token=leak")
    )

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        state = await cache.check("http://memfw.invalid:18080", client=client)

    assert state == HealthState(compatible=False, reachable=False)
    assert client.urls == ["http://memfw.invalid:18080/health"]
    assert any(REASON_HEALTH_PROBE_FAILED in line for line in _warnings(caplog))
    # The transport error may carry a peer address; only the code is logged.
    assert "token=leak" not in caplog.text
    assert _counts() == {REASON_HEALTH_PROBE_FAILED: 1}


@pytest.mark.asyncio
async def test_health_probe_timeout_warns_and_counts(caplog):
    """A probe that overruns the bounded timeout is the same invisible drop."""
    cache = HealthCache(cache_seconds=300.0, timeout=0.05)
    client = _SlowClient(delay=5.0)

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        state = await cache.check("http://memfw.invalid:18080", client=client)

    assert state == HealthState(compatible=False, reachable=False)
    assert any(REASON_HEALTH_PROBE_FAILED in line for line in _warnings(caplog))
    assert _counts() == {REASON_HEALTH_PROBE_FAILED: 1}


@pytest.mark.asyncio
async def test_health_probe_unreadable_body_warns_and_counts(caplog):
    """A 200 whose body the contract cannot read is a drop the caller cannot see."""
    cache = HealthCache(cache_seconds=300.0, timeout=1.0)
    client = _FakeClient(_FakeResponse(200, ValueError("bad json token=leak")))

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        state = await cache.check("http://memfw.invalid:18080", client=client)

    assert state == HealthState(compatible=False, reachable=False)
    assert any(REASON_HEALTH_PROBE_FAILED in line for line in _warnings(caplog))
    # The service's answer is untrusted input: none of it reaches the log.
    assert "bad json" not in caplog.text
    assert "token=leak" not in caplog.text
    assert _counts() == {REASON_HEALTH_PROBE_FAILED: 1}


@pytest.mark.asyncio
async def test_health_probe_unreadable_body_counts_every_probe(caplog):
    """An unreadable body is not cached, so each probe is counted."""
    cache = HealthCache(cache_seconds=300.0, timeout=1.0)
    client = _FakeClient(_FakeResponse(200, ValueError("bad json")))

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        for _ in range(2):
            await cache.check("http://memfw.invalid:18080", client=client)

    assert _counts() == {REASON_HEALTH_PROBE_FAILED: 2}
    assert len(client.urls) == 2


@pytest.mark.asyncio
async def test_health_probe_200_is_silent_and_uncounted(caplog):
    cache = HealthCache(cache_seconds=300.0, timeout=1.0)
    client = _FakeClient(_FakeResponse(200, {"contract": "memfw-http/2"}))

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        state = await cache.check("http://memfw.invalid:18080", client=client)

    assert state == HealthState(compatible=True, reachable=True, major=2)
    assert _warnings(caplog) == []
    assert _counts() == {}


@pytest.mark.asyncio
async def test_health_probe_503_keeps_the_endpoint_reduced(caplog):
    """A configured URL with a path must not reach the log or the request."""
    cache = HealthCache(cache_seconds=300.0, timeout=1.0)
    client = _FakeClient(_FakeResponse(503))
    endpoint = "http://user:password@memfw.invalid:18080/v1?token=secret"

    with caplog.at_level(logging.WARNING, logger=HEALTH_LOGGER):
        state = await cache.check(endpoint, client=client)

    # Not a root address: refused before any request, nothing sent at all.
    assert state == HealthState(
        compatible=False, reachable=False, reason="bad_config"
    )
    assert client.urls == []
    assert "secret" not in caplog.text
    assert "/v1" not in caplog.text
    assert _counts() == {}


@pytest.mark.asyncio
async def test_health_endpoint_exposes_memory_failure_counts(monkeypatch):
    """Operator-visible /health readback includes the process-local counters."""
    monkeypatch.setattr(mcp_registry, "_GLOBAL_REGISTRY", None)
    monkeypatch.setattr(
        "src.server.utils.checkpointer.get_checkpointer_health",
        _async({"status": "healthy"}),
    )
    record_memory_failure(REASON_PROVIDER_RECALL_FAILED)
    record_memory_failure(REASON_PROVIDER_RECALL_FAILED)
    record_memory_failure(REASON_LOCAL_MEMFW_UNAVAILABLE)

    result = await health_check()

    assert isinstance(result, dict)
    assert result["memory_federation"] == {
        "failure_counts": {
            REASON_PROVIDER_RECALL_FAILED: 2,
            REASON_LOCAL_MEMFW_UNAVAILABLE: 1,
        }
    }


@pytest.mark.asyncio
async def test_memory_selection_failure_warns_and_counts(caplog, monkeypatch):
    """A selection failure outside the federation is counted, not just warned."""
    from unittest.mock import AsyncMock

    from src.server.services.refactor import memory_runtime_controls
    from src.server.services.refactor.memory_context import build_turn_memory_block

    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
    monkeypatch.setattr(
        memory_runtime_controls, "automatic_memory_enabled", AsyncMock(return_value=True)
    )

    async def failed_search(*_args, **_kwargs):
        raise RuntimeError("offline detail must stay out of the log")

    with caplog.at_level(logging.WARNING, logger=MEMORY_CONTEXT_LOGGER):
        planned = await build_turn_memory_block(
            user_id="user-a", query="q", search=failed_search
        )

    assert planned is not None
    # The turn keeps an empty block rather than falling back to raw memory.md.
    assert planned[1] == ""
    assert any(REASON_MEMORY_SELECTION_FAILED in line for line in _warnings(caplog))
    # Fixed reason only: the failure text may carry a query or store detail.
    assert "offline detail" not in caplog.text
    assert _counts() == {REASON_MEMORY_SELECTION_FAILED: 1}


@pytest.mark.asyncio
async def test_memory_selection_failure_counts_every_occurrence(caplog, monkeypatch):
    from unittest.mock import AsyncMock

    from src.server.services.refactor import memory_runtime_controls
    from src.server.services.refactor.memory_context import build_turn_memory_block

    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
    monkeypatch.setattr(
        memory_runtime_controls, "automatic_memory_enabled", AsyncMock(return_value=True)
    )

    async def failed_search(*_args, **_kwargs):
        raise RuntimeError("offline")

    with caplog.at_level(logging.WARNING, logger=MEMORY_CONTEXT_LOGGER):
        for _ in range(2):
            planned = await build_turn_memory_block(
                user_id="user-a", query="q", search=failed_search
            )
            assert planned is not None

    assert _counts() == {REASON_MEMORY_SELECTION_FAILED: 2}


@pytest.mark.asyncio
async def test_memory_selection_failure_is_exposed_on_health(monkeypatch):
    """The selection-failure counter is readable through the existing /health."""
    from unittest.mock import AsyncMock

    from src.server.services.refactor import memory_runtime_controls
    from src.server.services.refactor.memory_context import build_turn_memory_block

    monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
    monkeypatch.setattr(
        memory_runtime_controls, "automatic_memory_enabled", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(mcp_registry, "_GLOBAL_REGISTRY", None)
    monkeypatch.setattr(
        "src.server.utils.checkpointer.get_checkpointer_health",
        _async({"status": "healthy"}),
    )

    async def failed_search(*_args, **_kwargs):
        raise RuntimeError("offline")

    planned = await build_turn_memory_block(
        user_id="user-a", query="q", search=failed_search
    )
    assert planned is not None

    assert memory_failure_status() == {
        "failure_counts": {REASON_MEMORY_SELECTION_FAILED: 1}
    }
    result = await health_check()
    assert isinstance(result, dict)
    assert result["memory_federation"] == {
        "failure_counts": {REASON_MEMORY_SELECTION_FAILED: 1}
    }
