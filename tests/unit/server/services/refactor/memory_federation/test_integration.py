import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from src.config.models import MemoryFederationUserBinding
from src.server.services.refactor.memory_federation import integration
from src.server.services.refactor.memory_federation.health import (
    HEALTH,
    HealthCache,
    HealthState,
)
from src.server.services.refactor.memory_federation.health import (
    check_contract as real_check_contract,
)
from src.server.services.refactor.memory_federation.models import MemoryHit
from src.server.services.refactor.memory_federation.provider import (
    DEFAULT_PROVIDER_TIMEOUT_SECONDS,
    MemoryFederation,
    ReadOnlyMemoryProviderError,
)
from src.server.services.refactor.memory_federation.remote import RemoteMemfwMemory

pytestmark = pytest.mark.asyncio

#: A reserved host name: it never resolves, so a stray request cannot reach
#: a real machine.
REMOTE_URL = "http://memfw.invalid:18080"


class _FakeClock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _offline_memfw(monkeypatch):
    """Keep every test off the network and out of the shared health cache.

    The contract probe is stubbed by default; a test that wants a different
    answer replaces it. ``HEALTH.clear()`` keeps one test's cached probe from
    answering the next one.
    """
    HEALTH.clear()
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", "test-read-token")
    monkeypatch.delenv("MEMFW_PRIMARY", raising=False)
    calls: list[str] = []

    async def fake_check(endpoint, **kwargs):
        calls.append(endpoint)
        return HealthState(
            compatible=True, reachable=True, major=2
        )

    monkeypatch.setattr(integration, "check_contract", fake_check)
    yield calls
    HEALTH.clear()


class _FakePg:
    last_budget: float | None = None

    def __init__(self, *, recall_budget_seconds=None):
        _FakePg.last_budget = recall_budget_seconds
        self.recall_calls = 0

    async def recall(self, user_id, query, k, tiers, workspace_id=None, thread_id=None):
        self.recall_calls += 1
        return [
            MemoryHit(
                source="conductor_pg",
                key="pg-key",
                text="pg line",
                score=1.0,
                provenance={"content_hash": "sha256:pg"},
                tier=tiers[0],
                workspace_id=workspace_id,
            )
        ]

    async def propose(self, user_id, entry):
        return "proposal-id"


class _FakeMemfw:
    def __init__(self, *, root, owner_id):
        self.root = root
        self.owner_id = owner_id

    async def recall(self, user_id, query, k, tiers, workspace_id=None, thread_id=None):
        if user_id != self.owner_id or "user" not in tiers:
            return []
        return [
            MemoryHit(
                source="memfw",
                key="memfw-key",
                text="legacy line",
                score=1.0,
                provenance={
                    "document_hash": "sha256:memfw",
                    "layer": "current",
                    "memory_metadata": {"confidence": 0.4},
                },
                tier="user",
            )
        ]

    async def propose(self, user_id, entry):
        raise RuntimeError("read-only")


async def test_configured_search_constructs_per_user_providers_and_tags_rows(monkeypatch):
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    monkeypatch.setattr(integration, "MemfwMemory", _FakeMemfw)
    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_path="/srv/user-a"),
        workspace_id="workspace-a",
        thread_id="thread-a",
    )
    rows = await search("user-a", "user", "query", 10, "thread-a")
    assert _FakePg.last_budget == DEFAULT_PROVIDER_TIMEOUT_SECONDS
    assert {row["source"] for row in rows} == {"conductor_pg", "memfw"}
    memfw = next(row for row in rows if row["source"] == "memfw")
    assert memfw["entry_key"] == "memfw-key"
    assert memfw["content_hash"] == "sha256:memfw"
    assert memfw["provenance"]["layer"] == "current"
    assert memfw["metadata"]["confidence"] == 0.4
    assert rows[0]["index_text"] in {"pg line", "legacy line"}


async def test_long_query_still_returns_conductor_rows(monkeypatch):
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    monkeypatch.setattr(integration, "MemfwMemory", _FakeMemfw)
    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_path="/srv/user-a"),
        workspace_id=None,
        thread_id=None,
    )
    rows = await search("user-a", "user", "x" * 600, 10)
    assert {row["source"] for row in rows} == {"conductor_pg", "memfw"}


async def test_invalid_local_source_keeps_read_only_conductor_provider(monkeypatch):
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)

    def unavailable(*, root, owner_id):
        raise OSError("source unavailable")

    monkeypatch.setattr(integration, "MemfwMemory", unavailable)
    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_path="/missing/user-a"),
        workspace_id=None,
        thread_id=None,
    )
    rows = await search("user-a", "user", "query", 10)
    assert len(rows) == 1
    assert rows[0]["source"] == "conductor_pg"


async def test_memfw_primary_switch_drops_pg_from_recall(monkeypatch):
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    monkeypatch.setattr(integration, "MemfwMemory", _FakeMemfw)
    monkeypatch.setenv("MEMFW_PRIMARY", "1")
    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_path="/srv/user-a"),
        workspace_id="workspace-a",
        thread_id="thread-a",
    )
    rows = await search("user-a", "user", "query", 10)
    assert {row["source"] for row in rows} == {"memfw"}


class _FakeRemoteMemfw(_FakeMemfw):
    def __init__(self, endpoint, *, owner_id, tenant=None):
        super().__init__(root=endpoint, owner_id=owner_id)
        self.endpoint = endpoint
        self.tenant = tenant


async def test_configured_search_uses_remote_url_without_local_path(monkeypatch):
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    monkeypatch.setattr(integration, "RemoteMemfwMemory", _FakeRemoteMemfw)
    monkeypatch.setenv("MEMFW_PRIMARY", "1")
    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
        workspace_id=None,
        thread_id=None,
    )
    rows = await search("user-a", "user", "query", 10)
    assert {row["source"] for row in rows} == {"memfw"}


async def test_configured_search_builds_remote_source_from_token_file(
    monkeypatch, tmp_path
):
    from src.server.services.refactor.memory_federation.remote import (
        RemoteMemfwMemory,
    )

    token_file = tmp_path / "memfw-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    constructed = []

    class TokenCheckingRemote(_FakeRemoteMemfw):
        def __init__(self, endpoint, *, owner_id, tenant=None):
            constructed.append(RemoteMemfwMemory(endpoint, owner_id=owner_id))
            super().__init__(endpoint, owner_id=owner_id)

    monkeypatch.setattr(integration, "RemoteMemfwMemory", TokenCheckingRemote)
    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
        workspace_id=None,
        thread_id=None,
    )

    rows = await search("user-a", "user", "query", 10)
    assert constructed[0].token == "file-token"
    assert {row["source"] for row in rows} == {"conductor_pg", "memfw"}


async def test_without_memfw_environment_recall_is_pg_only_and_never_warns(
    monkeypatch, caplog, _offline_memfw
):
    """A deployment without MemFW configured must not be warned about it."""
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MEMFW_PRIMARY", raising=False)
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    monkeypatch.setattr(integration, "_remote_source_warning_emitted", False)
    caplog.set_level(logging.DEBUG)

    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
        workspace_id=None,
        thread_id=None,
    )
    rows = await search("user-a", "user", "query", 10)

    assert [row["source"] for row in rows] == ["conductor_pg"]
    # No token means no request at all: the probe never runs.
    assert _offline_memfw == []
    assert [record for record in caplog.records if record.levelno >= logging.WARNING] == []
    assert any(
        record.levelno == logging.DEBUG and "read token" in record.message
        for record in caplog.records
    )


def _install_mock_remote(monkeypatch, client):
    def build_remote(endpoint, *, owner_id, tenant=None):
        return RemoteMemfwMemory(
            endpoint,
            owner_id=owner_id,
            token="secret-token",
            client=client,
            tenant=tenant,
        )

    monkeypatch.setattr(integration, "RemoteMemfwMemory", build_remote)


def _install_counting_pg(monkeypatch):
    providers = []

    def build_pg(**kwargs):
        provider = _FakePg(**kwargs)
        providers.append(provider)
        return provider

    monkeypatch.setattr(integration, "ConductorPgMemory", build_pg)
    return providers


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        # A transport failure is mapped onto the contract's own code, so the
        # warning names "timeout" rather than the name of the transport
        # exception; a contract failure reports the contract's error code.
        ("timeout", "timeout"),
        (500, "internal"),
        (401, "unauthorized"),
    ],
)
async def test_primary_remote_failure_falls_back_to_pg_and_warns_once(
    monkeypatch, caplog, failure, expected_reason
):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("sensitive-query", request=request)
        return httpx.Response(failure, request=request)

    pg = _install_counting_pg(monkeypatch)
    monkeypatch.setenv("MEMFW_PRIMARY", "1")
    monkeypatch.setattr(integration, "_remote_recall_warning_at", None)
    caplog.set_level(logging.WARNING, logger=integration.logger.name)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _install_mock_remote(monkeypatch, client)
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
            workspace_id=None,
            thread_id=None,
        )
        for _ in range(2):
            rows = await search("user-a", "user", "sensitive-query", 10)
            assert [row["source"] for row in rows] == ["conductor_pg"]

    assert len(requests) == 1
    assert pg[0].recall_calls == 1
    warnings = [
        record
        for record in caplog.records
        if "Remote MemFW recall failed" in record.message
    ]
    assert len(warnings) == 1
    assert expected_reason in warnings[0].message
    for sensitive in ("sensitive-query", "secret-token", REMOTE_URL):
        assert sensitive not in warnings[0].message


async def test_primary_remote_normal_empty_result_does_not_fall_back(
    monkeypatch, caplog
):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"contract": "memfw-http/2", "results": []}, request=request
        )

    pg = _install_counting_pg(monkeypatch)
    monkeypatch.setenv("MEMFW_PRIMARY", "1")
    monkeypatch.setattr(integration, "_remote_recall_warning_at", None)
    caplog.set_level(logging.WARNING, logger=integration.logger.name)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _install_mock_remote(monkeypatch, client)
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
            workspace_id=None,
            thread_id=None,
        )
        assert await search("user-a", "user", "sensitive-query", 10) == []

    assert len(requests) == 1
    assert pg[0].recall_calls == 0
    assert not [
        record
        for record in caplog.records
        if "Remote MemFW recall failed" in record.message
    ]


async def test_primary_remote_federation_timeout_falls_back_to_pg(monkeypatch):
    async def handler(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json={"results": []}, request=request)

    pg = _install_counting_pg(monkeypatch)
    monkeypatch.setenv("MEMFW_PRIMARY", "1")
    monkeypatch.setattr(integration, "DEFAULT_PROVIDER_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(integration, "_remote_recall_warning_at", None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _install_mock_remote(monkeypatch, client)
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
            workspace_id=None,
            thread_id=None,
        )
        rows = await search("user-a", "user", "query", 10)

    assert [row["source"] for row in rows] == ["conductor_pg"]
    assert pg[0].recall_calls == 1


async def test_non_primary_remote_failure_keeps_existing_pg_recall(monkeypatch, caplog):
    def handler(request):
        return httpx.Response(500, request=request)

    pg = _install_counting_pg(monkeypatch)
    monkeypatch.setenv("MEMFW_PRIMARY", "0")
    monkeypatch.setattr(integration, "_remote_recall_warning_at", None)
    caplog.set_level(logging.WARNING, logger=integration.logger.name)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _install_mock_remote(monkeypatch, client)
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
            workspace_id=None,
            thread_id=None,
        )
        rows = await search("user-a", "user", "query", 10)

    assert [row["source"] for row in rows] == ["conductor_pg"]
    assert pg[0].recall_calls == 1
    assert not [
        record
        for record in caplog.records
        if "Remote MemFW recall failed" in record.message
    ]


async def test_a_transport_failure_reaches_no_log_line_and_no_result(
    monkeypatch, caplog
):
    """The transport exception may carry a URL, a query and a peer message.

    The warning names the contract's code and nothing else, and the rows the
    turn keeps are built by Conductor's own provider.
    """
    sentinel = "sentinel-audit-token"

    def handler(request):
        raise httpx.ReadTimeout(sentinel, request=request)

    pg = _install_counting_pg(monkeypatch)
    monkeypatch.setenv("MEMFW_PRIMARY", "1")
    monkeypatch.setattr(integration, "_remote_recall_warning_at", None)
    caplog.set_level(logging.DEBUG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _install_mock_remote(monkeypatch, client)
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
            workspace_id=None,
            thread_id=None,
        )
        rows = await search("user-a", "user", "query", 10)

    assert [row["source"] for row in rows] == ["conductor_pg"]
    assert pg[0].recall_calls == 1
    warnings = [
        record
        for record in caplog.records
        if "Remote MemFW recall failed" in record.message
    ]
    assert len(warnings) == 1
    assert "timeout" in warnings[0].message
    assert sentinel not in warnings[0].message
    assert sentinel not in repr(rows)
    assert sentinel not in caplog.text


async def test_a_transport_failure_in_the_additive_path_keeps_pg_recall(
    monkeypatch, caplog
):
    """MemFW as an additional source: a transport failure keeps PG recall."""
    sentinel = "sentinel-audit-token"

    def handler(request):
        raise httpx.ConnectError(sentinel, request=request)

    pg = _install_counting_pg(monkeypatch)
    monkeypatch.setenv("MEMFW_PRIMARY", "0")
    monkeypatch.setattr(integration, "_remote_recall_warning_at", None)
    caplog.set_level(logging.DEBUG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _install_mock_remote(monkeypatch, client)
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
            workspace_id=None,
            thread_id=None,
        )
        rows = await search("user-a", "user", "query", 10)

    assert [row["source"] for row in rows] == ["conductor_pg"]
    assert pg[0].recall_calls == 1
    assert sentinel not in repr(rows)
    assert sentinel not in caplog.text


async def test_the_source_warning_names_a_code_and_not_the_exception_text(
    monkeypatch, caplog
):
    """A source that cannot be built is reported with a code as well."""
    sentinel = "sentinel-audit-token"

    async def failing_check(endpoint, **kwargs):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(integration, "check_contract", failing_check)
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    monkeypatch.setattr(integration, "_remote_source_warning_emitted", False)
    caplog.set_level(logging.DEBUG, logger=integration.logger.name)

    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
        workspace_id=None,
        thread_id=None,
    )
    rows = await search("user-a", "user", "query", 10)

    assert [row["source"] for row in rows] == ["conductor_pg"]
    warnings = [
        record
        for record in caplog.records
        if "Remote MemFW memory source unavailable" in record.message
    ]
    assert len(warnings) == 1
    assert "unreachable" in warnings[0].message
    assert sentinel not in warnings[0].message
    assert sentinel not in caplog.text


async def test_external_cancellation_does_not_fall_back_to_pg(monkeypatch):
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    pg = _install_counting_pg(monkeypatch)
    monkeypatch.setenv("MEMFW_PRIMARY", "1")
    monkeypatch.setattr(integration, "_remote_recall_warning_at", None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        _install_mock_remote(monkeypatch, client)
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
            workspace_id=None,
            thread_id=None,
        )
        task = asyncio.create_task(search("user-a", "user", "query", 10))
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert pg[0].recall_calls == 0


async def test_health_probe_runs_before_the_remote_source_is_built(monkeypatch):
    """Contract section 1: negotiate before a remote source exists."""
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    order: list[str] = []

    async def fake_check(endpoint, **kwargs):
        order.append("health")
        return HealthState(
            compatible=True, reachable=True, major=2
        )

    def build_remote(endpoint, *, owner_id, tenant=None):
        order.append("remote")
        return _FakeRemoteMemfw(endpoint, owner_id=owner_id)

    monkeypatch.setattr(integration, "check_contract", fake_check)
    monkeypatch.setattr(integration, "RemoteMemfwMemory", build_remote)

    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
        workspace_id=None,
        thread_id=None,
    )
    rows = await search("user-a", "user", "query", 10)

    assert order == ["health", "remote"]
    assert {row["source"] for row in rows} == {"conductor_pg", "memfw"}


async def test_incompatible_contract_keeps_pg_only(monkeypatch, caplog):
    """A major version other than 2 disables the remote source."""
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    caplog.set_level(logging.DEBUG, logger=integration.logger.name)

    async def fake_check(endpoint, **kwargs):
        return HealthState(
            compatible=False, reachable=True, major=1
        )

    def build_remote(endpoint, *, owner_id, tenant=None):
        raise AssertionError("an incompatible endpoint must not build a source")

    monkeypatch.setattr(integration, "check_contract", fake_check)
    monkeypatch.setattr(integration, "RemoteMemfwMemory", build_remote)

    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
        workspace_id=None,
        thread_id=None,
    )
    rows = await search("user-a", "user", "query", 10)

    assert [row["source"] for row in rows] == ["conductor_pg"]
    # health.py owns the version warning; integration adds none of its own.
    assert [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ] == []
    # The debug line names the version the endpoint actually speaks, so an
    # operator can see what the probe learned without turning on warnings.
    # Only the validated major is named: the raw contract string is not
    # logged, because it comes from the service.
    assert any("contract_major=1" in record.message for record in caplog.records)
    assert not any(
        "memfw-http/1" in record.message for record in caplog.records
    )


async def test_failed_health_probe_keeps_pg_only_and_probes_again_next_turn(
    monkeypatch,
):
    """A failed probe is not cached, so the next turn tries the endpoint again."""
    attempts: list[str] = []
    clock = _FakeClock()

    def handler(request):
        attempts.append(str(request.url))
        raise httpx.ConnectError("unreachable", request=request)

    cache = HealthCache(cache_seconds=300.0, clock=clock)
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def fake_check(endpoint, **kwargs):
            return await real_check_contract(endpoint, client=client, cache=cache)

        monkeypatch.setattr(integration, "check_contract", fake_check)

        for _ in range(2):
            search = await integration.configured_memory_search(
                user_id="user-a",
                binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
                workspace_id=None,
                thread_id=None,
            )
            rows = await search("user-a", "user", "query", 10)
            assert [row["source"] for row in rows] == ["conductor_pg"]

    assert len(attempts) == 2
    assert all(url.endswith("/health") for url in attempts)


async def test_successful_health_probe_is_cached_within_its_window(monkeypatch):
    """A reachable endpoint is probed once per cache window, not per turn."""
    attempts: list[str] = []
    clock = _FakeClock()

    def handler(request):
        attempts.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "service": "memfw",
                "contract": "memfw-http/2",
                "index_present": True,
            },
            request=request,
        )

    cache = HealthCache(cache_seconds=300.0, clock=clock)
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    monkeypatch.setattr(integration, "RemoteMemfwMemory", _FakeRemoteMemfw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def fake_check(endpoint, **kwargs):
            return await real_check_contract(endpoint, client=client, cache=cache)

        monkeypatch.setattr(integration, "check_contract", fake_check)

        for _ in range(2):
            search = await integration.configured_memory_search(
                user_id="user-a",
                binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
                workspace_id=None,
                thread_id=None,
            )
            await search("user-a", "user", "query", 10)

        assert len(attempts) == 1

        clock.advance(301.0)
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_url=REMOTE_URL),
            workspace_id=None,
            thread_id=None,
        )
        await search("user-a", "user", "query", 10)

    assert len(attempts) == 2


async def test_a_non_root_path_binding_sends_no_request_and_keeps_pg_only(monkeypatch):
    """A URL binding that is not a root address is refused before any request.

    The Planner's ruling: a MemFW endpoint may only be a root address, and a
    non-root path is refused with ``bad_config`` and **no request**. The probe
    here is the real one behind a mock transport, so the refusal is proved by
    the transport never being asked for anything -- not by a stub that was
    never going to answer. Conductor's own PG recall stays in place.
    """
    attempts: list[str] = []

    def handler(request):
        attempts.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "ok": True,
                "service": "memfw",
                "contract": "memfw-http/2",
                "index_present": True,
            },
            request=request,
        )

    clock = _FakeClock()
    cache = HealthCache(cache_seconds=300.0, clock=clock)
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:

        async def fake_check(endpoint, **kwargs):
            return await real_check_contract(endpoint, client=client, cache=cache)

        monkeypatch.setattr(integration, "check_contract", fake_check)

        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(
                memfw_url=REMOTE_URL + "/path-marker"
            ),
            workspace_id=None,
            thread_id=None,
        )
        rows = await search("user-a", "user", "query", 10)

    assert [row["source"] for row in rows] == ["conductor_pg"]
    assert attempts == [], "a non-root path must not send a request"


async def test_binding_tenant_reaches_the_remote_source(monkeypatch):
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePg)
    captured: list[dict] = []

    def build_remote(endpoint, *, owner_id, tenant=None):
        captured.append(
            {"endpoint": endpoint, "owner_id": owner_id, "tenant": tenant}
        )
        return _FakeRemoteMemfw(endpoint, owner_id=owner_id)

    monkeypatch.setattr(integration, "RemoteMemfwMemory", build_remote)

    search = await integration.configured_memory_search(
        user_id="user-a",
        binding=MemoryFederationUserBinding(
            memfw_url=REMOTE_URL, memfw_tenant="acme"
        ),
        workspace_id=None,
        thread_id=None,
    )
    await search("user-a", "user", "query", 10)

    assert captured == [
        {"endpoint": REMOTE_URL, "owner_id": "user-a", "tenant": "acme"}
    ]


class _FakePgForget:
    async def notify_forget(self, *, user_id, entry_id, content_hash):
        return "deleted"


class _FakeRemoteForget:
    """A remote provider that cannot forget, whatever it answers."""

    provider_id = "memfw"

    def __init__(self, endpoint, *, owner_id, tenant=None):
        self.endpoint = endpoint
        self.owner_id = owner_id
        self.tenant = tenant

    async def notify_forget(self, *, user_id, entry_id, content_hash):
        raise ReadOnlyMemoryProviderError("remote memfw has no forget endpoint")


def _install_forget_env(monkeypatch, binding):
    from src.config import features as config_features
    from src.config import settings
    from src.server.services import features as service_features

    monkeypatch.setattr(
        settings, "get_memory_federation_binding", lambda user_id: binding
    )
    monkeypatch.setattr(
        config_features,
        "system_flag",
        lambda key: SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        service_features, "user_feature_enabled", AsyncMock(return_value=True)
    )


async def test_url_binding_forget_reports_unsupported(monkeypatch):
    """Contract section 8: v2 has no remote forget endpoint."""
    binding = MemoryFederationUserBinding(memfw_url=REMOTE_URL)
    _install_forget_env(monkeypatch, binding)
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePgForget)

    def unexpected(*args, **kwargs):
        raise AssertionError("a URL binding must not touch memfw_path")

    monkeypatch.setattr(integration, "MemfwMemory", unexpected)

    result = await integration.notify_configured_forget(
        user_id="user-a", entry_id="entry-1", content_hash="sha256:abc"
    )

    assert result["memfw"] == "unsupported"
    assert result["memfw_reason"] == "remote memfw has no forget endpoint"
    assert result["memfw"] not in {"unavailable", "read_only"}


async def test_url_binding_forget_reports_unsupported_when_the_provider_cannot(
    monkeypatch,
):
    """A provider that refuses to forget is still reported as unsupported."""
    binding = MemoryFederationUserBinding(memfw_url=REMOTE_URL)
    _install_forget_env(monkeypatch, binding)
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePgForget)
    monkeypatch.setattr(integration, "RemoteMemfwMemory", _FakeRemoteForget)

    result = await integration.notify_configured_forget(
        user_id="user-a", entry_id="entry-1", content_hash="sha256:abc"
    )

    assert result["memfw"] == "unsupported"
    assert result["memfw_reason"] == "remote memfw has no forget endpoint"


async def test_url_binding_forget_without_a_token_still_reports_unsupported(
    monkeypatch, caplog
):
    """The reason is structural, so a missing token changes nothing."""
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)
    binding = MemoryFederationUserBinding(memfw_url=REMOTE_URL)
    _install_forget_env(monkeypatch, binding)
    monkeypatch.setattr(integration, "ConductorPgMemory", _FakePgForget)
    caplog.set_level(logging.DEBUG)

    result = await integration.notify_configured_forget(
        user_id="user-a", entry_id="entry-1", content_hash="sha256:abc"
    )

    assert result["memfw"] == "unsupported"
    assert result["memfw_reason"] == "remote memfw has no forget endpoint"
    assert [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ] == []


async def test_the_federation_reports_the_unsupported_forget_status():
    """"unsupported" is a capability answer, not a failure to report."""

    class _UnsupportedProvider:
        provider_id = "memfw"

        async def notify_forget(self, *, user_id, entry_id, content_hash):
            return "unsupported"

    result = await MemoryFederation([_UnsupportedProvider()]).notify_forget(
        user_id="user-a", entry_id="entry-1", content_hash="sha256:abc"
    )

    assert result["memfw"] == "unsupported"
