"""Regression coverage for the Conductor-side MemFW integration fixes."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError

from src.config.models import MemoryFederationConfig, MemoryFederationUserBinding
from src.server.contracts.memory_validity import is_current
from src.server.services.refactor.memory_context import select_memory_context
from src.server.services.refactor.memory_federation import integration, write_bridge
from src.server.services.refactor.memory_federation.errors import MemfwContractError
from src.server.services.refactor.memory_federation.health import HealthCache
from src.server.services.refactor.memory_federation.models import MemoryHit
from src.server.services.refactor.memory_federation.provider import (
    MemoryFederation,
    memory_hit_is_recallable,
)
from src.server.services.refactor.memory_federation.remote import RemoteMemfwMemory

URL = "http://memfw.invalid:18080"


def _body(results: list[dict]) -> dict:
    return {
        "contract": "memfw-http/2",
        "mode": "fts",
        "degraded": False,
        "results": results,
    }


@pytest.mark.asyncio
async def test_hot_memfw_hit_survives_the_federation_user_tier_filter():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=_body(
                [
                    {
                        "id": "hot-1",
                        "title": "MemFW fact",
                        "summary": "kept",
                        "score": 0.9,
                        "tier": "hot",
                    }
                ]
            ),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RemoteMemfwMemory(URL, owner_id="user-a", token="token", client=client)
        federation = MemoryFederation([provider])
        hits = await federation.recall("user-a", "fact", 5, ["user"])
        assert [hit.key for hit in hits] == ["hot-1"]
        assert hits[0].tier == "user"
        assert hits[0].provenance["memfw_tier"] == "hot"
        assert hits[0].provenance["memory_metadata"]["tier"] == "hot"

        assert await federation.recall("user-a", "fact", 5, ["project"]) == []

    assert len(calls) == 1


class _EmptyPg:
    def __init__(self, *, recall_budget_seconds=None):
        pass

    async def recall(self, user_id, query, k, tiers, workspace_id=None, thread_id=None):
        return []


@pytest.mark.asyncio
async def test_one_turn_searches_remote_once_and_reuses_the_user_result(monkeypatch):
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", "token")
    monkeypatch.setattr(integration, "ConductorPgMemory", _EmptyPg)

    async def healthy(endpoint, **kwargs):
        from src.server.services.refactor.memory_federation.health import HealthState

        return HealthState(compatible=True, reachable=True, major=2)

    monkeypatch.setattr(integration, "check_contract", healthy)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json=_body(
                [
                    {
                        "id": "hot-1",
                        "title": "MemFW fact",
                        "summary": "one call",
                        "score": 0.9,
                        "tier": "hot",
                    }
                ]
            ),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(
            integration,
            "RemoteMemfwMemory",
            lambda endpoint, *, owner_id, tenant=None: RemoteMemfwMemory(
                endpoint, owner_id=owner_id, token="token", client=client, tenant=tenant
            ),
        )
        search = await integration.configured_memory_search(
            user_id="user-a",
            binding=MemoryFederationUserBinding(memfw_url=URL),
            workspace_id=None,
            thread_id=None,
        )
        context = await select_memory_context(
            user_id="user-a",
            query="fact",
            budget_tokens=100,
            search=search,
        )
        assert any(entry.source == "memfw" for entry in context.entries)
        rows = await search("user-a", "user", "fact", 5)
        assert rows[0]["entry_key"] == "hot-1"

    # Five Conductor tiers plus a repeated user query still produce one POST.
    assert len(calls) == 1


class _SlowHealthClient:
    async def get(self, url):
        await asyncio.sleep(0.2)
        return httpx.Response(
            200,
            json={"contract": "memfw-http/2", "ok": True, "index_present": True},
        )


class _SlowRemoteClient:
    async def post(self, url, **kwargs):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=_body([]))


class _SlowStageClient:
    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        await asyncio.sleep(0.2)
        return httpx.Response(
            200,
            json={"contract": "memfw-http/2", "staged": True, "duplicate": False},
        )


@pytest.mark.asyncio
async def test_health_probe_has_a_deadline_for_the_whole_request():
    cache = HealthCache(timeout=0.005)
    started = time.monotonic()

    state = await cache.check(URL, client=_SlowHealthClient())

    assert state.reachable is False
    assert time.monotonic() - started < 0.15


@pytest.mark.asyncio
async def test_remote_search_has_a_deadline_for_the_whole_request():
    provider = RemoteMemfwMemory(
        URL,
        owner_id="user-a",
        token="token",
        timeout=0.005,
        client=_SlowRemoteClient(),
    )
    started = time.monotonic()

    with pytest.raises(MemfwContractError) as excinfo:
        await provider.recall("user-a", "fact", 5, ["user"])

    assert excinfo.value.error_code == "timeout"
    assert time.monotonic() - started < 0.15


@pytest.mark.asyncio
async def test_stage_has_a_deadline_for_the_whole_request(monkeypatch):
    monkeypatch.setenv("MEMFW_WRITE_BRIDGE", "1")
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", "token")
    monkeypatch.setattr(write_bridge.httpx, "AsyncClient", _SlowStageClient)
    monkeypatch.setattr(write_bridge, "_STAGE_TIMEOUT_SECONDS", 0.005)
    started = time.monotonic()

    result = await write_bridge.stage_approved_proposal(
        {"proposal_id": "proposal-1"}, content="body", endpoint=URL
    )

    assert result["error_code"] == "timeout"
    assert time.monotonic() - started < 0.15


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("http://127.0.0.1:18771", "http://localhost:18771"),
        ("http://LOCALHOST", "http://127.0.0.1:80/"),
        ("http://[::1]:18771", "http://localhost:18771"),
        ("http://[::1]:18771", "http://[0:0:0:0:0:0:0:1]:18771"),
        ("http://127.0.0.1:18771", "http://[::ffff:127.0.0.1]:18771"),
    ],
)
def test_url_aliases_cannot_bypass_the_one_user_source_boundary(left, right):
    with pytest.raises(ValidationError, match="one user only"):
        MemoryFederationConfig(
            user_bindings={
                "user-a": {"memfw_url": left},
                "user-b": {"memfw_url": right},
            }
        )


def test_distinct_remote_ports_remain_distinct_sources():
    config = MemoryFederationConfig(
        user_bindings={
            "user-a": {"memfw_url": "http://localhost:18771"},
            "user-b": {"memfw_url": "http://localhost:18772"},
        }
    )
    assert set(config.user_bindings) == {"user-a", "user-b"}


def test_temporal_aliases_filter_expired_and_superseded_hits():
    now = datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert is_current({"valid_to": "2000-01-01T00:00:00Z"}, now=now) is False
    assert is_current({"valid_to": "2999-01-01T00:00:00Z"}, now=now) is True

    base = dict(source="memfw", key="key", text="text", score=1.0, tier="user")
    assert memory_hit_is_recallable(
        MemoryHit(**base, provenance={"superseded_by": "newer"})
    ) is False
    assert memory_hit_is_recallable(
        MemoryHit(**base, provenance={"valid_to": "2000-01-01T00:00:00Z"})
    ) is False
    assert memory_hit_is_recallable(
        MemoryHit(**base, provenance={"valid_to": "2999-01-01T00:00:00Z"})
    ) is True


def test_hex_mapped_loopback_cannot_bypass_source_ownership():
    with pytest.raises(ValidationError, match="one user only"):
        MemoryFederationConfig(
            user_bindings={
                "user-a": {"memfw_url": "http://127.0.0.1:18771"},
                "user-b": {"memfw_url": "http://[::ffff:7f00:1]:18771"},
            }
        )


def test_non_loopback_ipv4_mapped_alias_cannot_bypass_source_ownership():
    with pytest.raises(ValidationError, match="one user only"):
        MemoryFederationConfig(
            user_bindings={
                "user-a": {"memfw_url": "http://192.0.2.1:18771"},
                "user-b": {"memfw_url": "http://[::ffff:192.0.2.1]:18771"},
            }
        )


def test_ipv4_mapped_same_host_with_distinct_ports_is_allowed():
    config = MemoryFederationConfig(
        user_bindings={
            "user-a": {"memfw_url": "http://[::ffff:192.0.2.1]:18771"},
            "user-b": {"memfw_url": "http://192.0.2.1:18772"},
        }
    )
    assert set(config.user_bindings) == {"user-a", "user-b"}
