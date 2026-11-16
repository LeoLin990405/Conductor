"""Federated forget fan-out stays bounded and never implies remote erasure."""

from __future__ import annotations

import asyncio

import pytest

from src.server.services.refactor.memory_federation.farm import FarmRetrieval
from src.server.services.refactor.memory_federation.provider import MemoryFederation

pytestmark = pytest.mark.asyncio


class FakeProvider:
    def __init__(self, provider_id, result="deleted", *, failure=None, delay=0):
        self.provider_id = provider_id
        self.result = result
        self.failure = failure
        self.delay = delay
        self.calls = []

    async def recall(self, *args, **kwargs):
        return []

    async def propose(self, *args, **kwargs):
        return "proposal"

    async def notify_forget(self, *, user_id, entry_id, content_hash):
        self.calls.append((user_id, entry_id, content_hash))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        return self.result


class ReadOnlyProviderWithoutMethod:
    provider_id = "legacy_read_only"

    async def recall(self, *args, **kwargs):
        return []

    async def propose(self, *args, **kwargs):
        raise AssertionError("forget notification must not call propose")


class NoNetworkClient:
    def __init__(self):
        self.calls = []

    async def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("Farm forget must never issue HTTP")


async def test_fanout_reports_each_provider_and_sanitizes_failures():
    erased = FakeProvider("erased", "deleted")
    readonly = FakeProvider("readonly", "read_only")
    failing = FakeProvider("failing", failure=RuntimeError("private body text"))
    implicit_readonly = ReadOnlyProviderWithoutMethod()
    federation = MemoryFederation(
        [erased, readonly, failing, implicit_readonly], provider_timeout_seconds=0.2
    )

    result = await federation.notify_forget(
        user_id="owner-id",
        entry_id="entry-id",
        content_hash="sha256:hash-only",
    )

    assert result == {
        "erased": "deleted",
        "readonly": "read_only",
        "failing": "unavailable",
        "legacy_read_only": "read_only",
    }
    assert erased.calls == [("owner-id", "entry-id", "sha256:hash-only")]
    assert failing.calls == [("owner-id", "entry-id", "sha256:hash-only")]
    assert "private body text" not in repr(result)


async def test_slow_provider_times_out_without_holding_up_other_providers():
    slow = FakeProvider("slow", delay=0.1)
    fast = FakeProvider("fast", "deleted")
    federation = MemoryFederation([slow, fast], provider_timeout_seconds=0.01)

    result = await federation.notify_forget(
        user_id="owner-id", entry_id="entry-id", content_hash="sha256:hash-only"
    )

    assert result == {"slow": "unavailable", "fast": "deleted"}
    assert fast.calls


async def test_malformed_provider_status_is_unavailable():
    malformed = FakeProvider("malformed", {"status": "deleted"})
    federation = MemoryFederation([malformed])

    result = await federation.notify_forget(
        user_id="owner-id", entry_id="entry-id", content_hash="sha256:hash-only"
    )

    assert result == {"malformed": "unavailable"}


async def test_farm_readonly_status_does_not_contact_gateway():
    client = NoNetworkClient()
    provider = FarmRetrieval(
        endpoint="https://farm.example.invalid/recall", http_client=client, enabled=True
    )

    result = await provider.notify_forget(
        user_id="owner-id", entry_id="entry-id", content_hash="sha256:hash-only"
    )

    assert result == "read_only"
    assert client.calls == []


async def test_invalid_identity_is_rejected_before_any_provider_call():
    provider = FakeProvider("p")
    federation = MemoryFederation([provider])

    with pytest.raises(ValueError):
        await federation.notify_forget(
            user_id="", entry_id="entry-id", content_hash="sha256:hash-only"
        )
    assert provider.calls == []
