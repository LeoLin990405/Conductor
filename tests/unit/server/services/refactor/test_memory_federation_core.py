from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from src.server.services.refactor.memory_federation import (
    FarmRetrieval,
    MemoryEntry,
    MemoryFederation,
    MemoryHit,
    MemoryProposal,
    NoWritableMemoryProvider,
    ReadOnlyMemoryProviderError,
    fuse_hits,
)
from src.server.services.refactor.memory_federation.farm import MAX_FARM_RESPONSE_BYTES
from src.server.services.refactor.memory_federation.provider import MAX_QUERY_LENGTH

pytestmark = pytest.mark.asyncio


class FakeProvider:
    def __init__(self, rows: list[MemoryHit] | None = None, error: Exception | None = None):
        self.rows = rows or []
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def recall(
        self,
        user_id: str,
        query: str,
        k: int,
        tiers: Sequence[str],
        workspace_id: str | None = None,
        thread_id: str | None = None,
    ) -> list[MemoryHit]:
        self.calls.append(
            {
                "user_id": user_id,
                "query": query,
                "k": k,
                "tiers": tuple(tiers),
                "workspace_id": workspace_id,
                "thread_id": thread_id,
            }
        )
        if self.error:
            raise self.error
        return self.rows

    async def propose(self, user_id: str, entry: MemoryEntry) -> str:
        return f"proposal:{entry.key}"


def hit(
    source: str,
    key: str,
    *,
    score: float = 1.0,
    tier: str | None = "user",
    workspace_id: str | None = None,
) -> MemoryHit:
    return MemoryHit(source, key, f"{source}:{key}", score, {"origin": source}, tier, workspace_id)


async def test_federation_scope_bounds_fail_open_and_rrf_ignore_raw_scores() -> None:
    pg = FakeProvider(
        [
            hit("conductor_pg", "shared", score=0.01),
            hit("conductor_pg", "other-workspace", workspace_id="private"),
            hit("conductor_pg", "wrong-tier", tier="session"),
        ]
    )
    farm = FakeProvider(
        [
            hit("farm", "shared", score=9999),
            hit("farm", "farm-only"),
        ]
    )
    broken = FakeProvider(error=RuntimeError("source unavailable"))
    federation = MemoryFederation([pg, farm, broken], provider_timeout_seconds=0.2)

    rows = await federation.recall(
        "user", "  query  ", 10, ["user"], workspace_id="workspace-1", thread_id="thread-1"
    )

    assert [(row.source, row.key) for row in rows] == [
        ("conductor_pg", "shared"),
        ("farm", "shared"),
        ("farm", "farm-only"),
    ]
    assert rows[0].score == pytest.approx(1 / 61)
    assert rows[1].score == pytest.approx(1 / 61)
    assert rows[0].provenance["origin"] == "conductor_pg"
    assert pg.calls[0]["workspace_id"] == "workspace-1"
    assert pg.calls[0]["thread_id"] == "thread-1"
    assert pg.calls[0]["k"] == 10


async def test_recall_cancellation_propagates_and_bad_user_is_rejected() -> None:
    started = asyncio.Event()

    class WaitingProvider(FakeProvider):
        async def recall(self, *args: object, **kwargs: object) -> list[MemoryHit]:
            started.set()
            await asyncio.Event().wait()
            return []

    federation = MemoryFederation([WaitingProvider()], provider_timeout_seconds=5)
    task = asyncio.create_task(federation.recall("user", "query", 2, ["user"]))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ValueError, match="user_id"):
        await federation.recall("  ", "query", 2, ["user"])


async def test_unscoped_recall_drops_workspace_tagged_rows() -> None:
    provider = FakeProvider(
        [hit("memfw", "global"), hit("memfw", "scoped", workspace_id="workspace-x")]
    )
    rows = await MemoryFederation([provider]).recall("user", "query", 5, ["user"])
    assert [row.key for row in rows] == ["global"]


async def test_federation_filters_stale_lifecycle_before_rrf_and_keeps_metadata() -> None:
    provider = FakeProvider(
        [
            MemoryHit(
                "memfw", "expired", "expired", 1.0,
                {"memory_metadata": {"valid_until": "2000-01-01T00:00:00Z"}},
                "user",
            ),
            MemoryHit(
                "memfw", "future", "future", 1.0,
                {"memory_metadata": {"valid_from": "2999-01-01T00:00:00Z"}},
                "user",
            ),
            MemoryHit(
                "memfw", "superseded", "old", 1.0,
                {"memory_metadata": {"status": "superseded"}},
                "user",
            ),
            MemoryHit(
                "farm", "conflicting-expiry", "old", 1.0,
                {
                    "metadata": {"valid_until": "2000-01-01T00:00:00Z"},
                    "memory_metadata": {
                        "valid_until": "2999-01-01T00:00:00Z",
                        "status": "active",
                    },
                },
                "user",
            ),
            MemoryHit(
                "farm", "conflicting-status", "old", 1.0,
                {
                    "metadata": {"status": "superseded"},
                    "memory_metadata": {"status": "active"},
                },
                "user",
            ),
            MemoryHit(
                "memfw", "current", "current", 1.0,
                {
                    "memory_metadata": {
                        "valid_until": "2999-01-01T00:00:00Z",
                        "confidence": 0.25,
                    }
                },
                "user",
            ),
        ]
    )

    rows = await MemoryFederation([provider]).recall("user", "query", 10, ["user"])

    assert [row.key for row in rows] == ["current"]
    assert rows[0].provenance["memory_metadata"]["confidence"] == 0.25
    assert rows[0].provenance["memory_metadata"]["valid_until"] == "2999-01-01T00:00:00Z"


async def test_federation_filters_secret_and_invalid_nested_hit_metadata() -> None:
    provider = FakeProvider(
        [
            MemoryHit(
                "farm", "secret-nested", "secret", 1.0,
                {"sensitivity": "public", "memory_metadata": {"sensitivity": "secret"}},
                "user",
            ),
            MemoryHit(
                "farm", "malformed-wrapper", "bad", 1.0,
                {"sensitivity": "public", "metadata": "bad"}, "user",
            ),
            MemoryHit("farm", "unknown", "unknown", 1.0, {"sensitivity": "internal"}, "user"),
            MemoryHit("farm", "private", "private", 1.0, {"sensitivity": "private"}, "user"),
            MemoryHit("farm", "public", "public", 1.0, {"sensitivity": "public"}, "user"),
        ]
    )
    rows = await MemoryFederation([provider]).recall("user", "query", 10, ["user"])
    assert [row.key for row in rows] == ["private", "public"]


@pytest.mark.parametrize(
    "provenance",
    [
        {"sensitivity": "secret"},
        {"sensitivity": "internal"},
        {"metadata": {"sensitivity": "secret"}},
    ],
)
async def test_federation_proposal_egress_rejects_secret_policy(provenance) -> None:
    writer = FakeProvider()
    federation = MemoryFederation([], proposal_writer=writer)
    entry = MemoryEntry("key", "body", "user", provenance=provenance)
    with pytest.raises(ValueError, match="secret memory"):
        await federation.propose("user", entry)


async def test_federation_proposal_egress_allows_private_memory() -> None:
    writer = FakeProvider()
    federation = MemoryFederation([], proposal_writer=writer)
    entry = MemoryEntry("key", "body", "user", provenance={"sensitivity": "private"})
    assert await federation.propose("user", entry) == "proposal:key"


async def test_federation_proposal_egress_checks_dedicated_metadata_field() -> None:
    writer = FakeProvider()
    federation = MemoryFederation([], proposal_writer=writer)
    entry = MemoryEntry(
        "key", "body", "user", metadata={"sensitivity": "secret"}
    )
    with pytest.raises(ValueError, match="secret memory"):
        await federation.propose("user", entry)


async def test_duplicate_source_rows_do_not_reward_rrf_and_cross_source_keys_stay_distinct() -> None:
    rows = fuse_hits(
        [
            [hit("memfw", "same", score=0.2), hit("memfw", "same", score=0.9)],
            [hit("memfw", "same", score=0.1)],
            [hit("farm", "same", score=50)],
        ],
        10,
        rrf_k=10,
    )
    by_id = {(row.source, row.key): row for row in rows}
    assert by_id[("memfw", "same")].score == pytest.approx(1 / 11)
    assert by_id[("farm", "same")].score == pytest.approx(1 / 11)
    assert by_id[("memfw", "same")].provenance["federated"]["source_ranks"] == {"memfw": 1}


async def test_farm_is_disabled_without_request_and_validates_injected_schema() -> None:
    class Response:
        def __init__(self, body: object):
            self.body = body
            self.content = b"{}"

        def json(self) -> object:
            return self.body

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, body: object):
            self.body = body
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def get(self, url: str, *, params: dict[str, object]) -> Response:
            self.calls.append((url, params))
            return Response(self.body)

    client = Client({"hits": []})
    disabled = FarmRetrieval("https://farm.invalid/retrieve", client)
    assert await disabled.recall("user", "query", 2, ["user"]) == []
    assert client.calls == []

    farm = FarmRetrieval("https://farm.invalid/retrieve", client, enabled=True)
    assert await farm.recall("user", "query", 2, ["user"]) == []
    assert client.calls[0][0] == "https://farm.invalid/retrieve"

    valid = Client(
        {
            "hits": [
                {
                    "source": "upstream",
                    "key": "farm-key",
                    "text": "farm memory",
                    "score": 20,
                    "tier": "user",
                    "workspace_id": "workspace-1",
                    "provenance": {"record": "fixture"},
                }
            ]
        }
    )
    valid_rows = await FarmRetrieval(
        "https://farm.invalid/retrieve", valid, enabled=True
    ).recall("user", "query", 2, ["user"], workspace_id="workspace-1")
    assert len(valid_rows) == 1
    assert valid_rows[0].source == "farm"
    assert valid_rows[0].provenance["farm_source"] == "upstream"
    assert valid_rows[0].provenance["record"] == "fixture"
    assert valid.calls[0][1]["workspace_id"] == "workspace-1"

    malformed = Client(
        {"hits": [{"source": "farm", "key": "k", "text": "x", "score": 1, "extra": True}]}
    )

    assert await FarmRetrieval("https://farm.invalid/retrieve", malformed, enabled=True).recall(
        "user", "query", 2, ["user"]
    ) == []


async def test_farm_direct_recall_filters_stale_hits_and_preserves_validity_metadata() -> None:
    class Response:
        content = b"{}"

        def json(self) -> object:
            return {
                "hits": [
                    {
                        "source": "farm",
                        "key": "expired",
                        "text": "old fact",
                        "score": 10,
                        "tier": "user",
                        "provenance": {
                            "memory_metadata": {
                                "valid_until": "2000-01-01T00:00:00Z"
                            }
                        },
                    },
                    {
                        "source": "farm",
                        "key": "superseded",
                        "text": "old revision",
                        "score": 9,
                        "tier": "user",
                        "provenance": {"metadata": {"status": "superseded"}},
                    },
                    {
                        "source": "farm",
                        "key": "current",
                        "text": "current fact",
                        "score": 1,
                        "tier": "user",
                        "provenance": {
                            "memory_metadata": {
                                "valid_from": "2000-01-01T00:00:00Z",
                                "valid_until": "2999-01-01T00:00:00Z",
                                "confidence": 0.4,
                            }
                        },
                    },
                ]
            }

        def raise_for_status(self) -> None:
            return None

    class Client:
        async def get(self, url: str, *, params: dict[str, object]) -> Response:
            return Response()

    rows = await FarmRetrieval(
        "https://farm.invalid/retrieve", Client(), enabled=True
    ).recall("user", "query", 5, ["user"])

    assert [row.key for row in rows] == ["current"]
    assert rows[0].provenance["memory_metadata"]["confidence"] == 0.4


async def test_overlong_query_is_truncated_instead_of_dropping_all_sources() -> None:
    provider = FakeProvider([hit("conductor_pg", "survives")])
    rows = await MemoryFederation([provider]).recall("user", "x" * 600, 5, ["user"])
    assert [row.key for row in rows] == ["survives"]
    assert len(provider.calls[0]["query"]) == MAX_QUERY_LENGTH


async def test_external_providers_are_read_only_and_proposals_require_explicit_writer() -> None:
    farm = FarmRetrieval()
    with pytest.raises(ReadOnlyMemoryProviderError):
        await farm.propose("user", MemoryEntry("k", "text", "user"))
    with pytest.raises(NoWritableMemoryProvider):
        await MemoryFederation([]).propose("user", MemoryEntry("k", "text", "user"))
    writer = FakeProvider()
    federation = MemoryFederation([], proposal_writer=writer)
    assert await federation.propose("user", MemoryEntry("k", "text", "user")) == "proposal:k"


async def test_farm_bounds_response_bytes_and_timeout() -> None:
    class Response:
        content = b"x" * (MAX_FARM_RESPONSE_BYTES + 1)

        def json(self) -> object:
            raise AssertionError("oversized body must be rejected before parsing")

        def raise_for_status(self) -> None:
            return None

    class Client:
        async def get(self, url: str, *, params: dict[str, object]) -> Response:
            return Response()

    result = await FarmRetrieval(
        "https://farm.invalid/retrieve", Client(), enabled=True
    ).recall("user", "query", 2, ["user"])
    assert result == []

    class SlowClient:
        async def get(self, url: str, *, params: dict[str, object]) -> object:
            await asyncio.Event().wait()

    result = await FarmRetrieval(
        "https://farm.invalid/retrieve", SlowClient(), enabled=True, timeout_seconds=0.01
    ).recall("user", "query", 2, ["user"])
    assert result == []


async def test_provider_timeout_falls_back_to_available_source() -> None:
    class SlowProvider(FakeProvider):
        async def recall(self, *args: object, **kwargs: object) -> list[MemoryHit]:
            await asyncio.Event().wait()

    available = FakeProvider([hit("conductor_pg", "available")])
    rows = await MemoryFederation(
        [SlowProvider(), available], provider_timeout_seconds=0.01
    ).recall("user", "query", 5, ["user"])
    assert [(row.source, row.key) for row in rows] == [("conductor_pg", "available")]


async def test_non_finite_provider_timeouts_are_rejected() -> None:
    with pytest.raises(ValueError, match="timeout"):
        MemoryFederation([], provider_timeout_seconds=float("nan"))
    with pytest.raises(ValueError, match="timeout"):
        FarmRetrieval(timeout_seconds=float("inf"))


async def test_memory_models_validate_types_and_copy_provenance() -> None:
    provenance = {"source": "local"}
    row = MemoryHit("memfw", "key", "text", 1, provenance)
    provenance["source"] = "changed"
    assert row.provenance["source"] == "local"
    with pytest.raises(TypeError, match="source and key"):
        MemoryHit(1, "key", "text", 1, {})  # type: ignore[arg-type]
    proposal = MemoryProposal("p", "capture", ("k",), "pending")
    assert proposal.status == "pending"
