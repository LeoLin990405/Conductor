"""Tests for the read-only MemFW HTTP client (``memfw-http/2``).

Every request goes through ``httpx.MockTransport``; no test touches a real
host. Timestamps are fixed literals so nothing depends on the wall clock.
"""

import ast
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from src.server.services.refactor.memory_federation.errors import MemfwContractError
from src.server.services.refactor.memory_federation.health import (
    HEALTH_PATH,
    HealthCache,
    check_contract,
)
from src.server.services.refactor.memory_federation.models import MemoryEntry
from src.server.services.refactor.memory_federation.provider import (
    ReadOnlyMemoryProviderError,
)
from src.server.services.refactor.memory_federation.remote import (
    MAX_QUERY_LENGTH,
    MAX_RESULTS,
    RemoteMemfwMemory,
    resolve_memfw_token,
)

FIXTURE_DIR = Path(__file__).resolve().parents[5] / "fixtures" / "memfw_contract_v2"

#: The logger the remote client writes its failure line to.
remote_logger_name = (
    "src.server.services.refactor.memory_federation.remote"
)

#: A fixed point in time, well before any run of this suite.
VALID_FROM = "2020-01-01T00:00:00Z"
VALID_TO = "2021-01-01T00:00:00Z"

#: The path token the endpoint-refusal audits use.
PATH_SENTINEL = "path-marker"


def _fixture(name: str) -> dict:
    path = FIXTURE_DIR / name
    if not path.is_file():
        pytest.skip(f"contract fixture {name} is not available")
    return json.loads(path.read_text(encoding="utf-8"))


class _Recorder:
    """A transport handler that answers ``/health`` and ``/search``.

    Because the recorder *is* the transport, ``calls`` is exactly the set of
    requests the client was asked to send -- the proof that a refused
    configuration sends none, and that a request that was sent carries the URL
    this test expects.
    """

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.path == HEALTH_PATH:
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
        return httpx.Response(
            200,
            json=_ok_body(
                [{"id": "memory-1", "title": "Title", "summary": "Summary", "score": 0.5}]
            ),
            request=request,
        )


@asynccontextmanager
async def _remote(handler, *, tenant=None, token="test-token", owner_id="user-a"):
    """A remote source whose transport is the given mock handler."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        yield RemoteMemfwMemory(
            "http://memfw:18080",
            owner_id=owner_id,
            token=token,
            client=client,
            tenant=tenant,
        )


def _ok_body(results: list[dict], **extra: object) -> dict:
    body = {"contract": "memfw-http/2", "mode": "fts", "degraded": False}
    body.update(extra)
    body["results"] = results
    return body


def test_read_token_environment_precedes_file(monkeypatch, tmp_path):
    token_file = tmp_path / "read-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", "env-token")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(token_file))

    assert resolve_memfw_token("READONLY") == "env-token"

    monkeypatch.delenv("MEMFW_READONLY_TOKEN")
    assert resolve_memfw_token("READONLY") == "file-token"


def test_remote_source_reads_token_file(monkeypatch, tmp_path):
    token_file = tmp_path / "read-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN_FILE", str(token_file))

    provider = RemoteMemfwMemory("http://memfw:18080", owner_id="user-a")

    assert provider.token == "file-token"


def test_remote_source_requires_a_read_token(monkeypatch):
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)

    with pytest.raises(ValueError, match="MEMFW_READONLY_TOKEN"):
        RemoteMemfwMemory("http://memfw:18080", owner_id="user-a")


@pytest.mark.asyncio
async def test_a_v2_health_check_lets_the_search_request_use_the_validated_base():
    """After a successful v2 probe, ``/search`` rides the validated base.

    The Planner's ruling: every request is built from the single validated
    ``normalized_base`` plus the contract's fixed path. The probe and the
    search therefore share one transport here, and the URLs it received are
    asserted exactly: the negotiated ``/health`` line first, then
    ``<normalized_base>/search``. The configured value carries a trailing
    slash, so this also pins the normalisation -- one separator, never two.
    """
    recorder = _Recorder()
    cache = HealthCache(cache_seconds=300.0)
    endpoint = "http://memfw:18080/"

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        state = await check_contract(endpoint, client=client, cache=cache)
        assert state.compatible is True
        assert state.major == 2
        provider = RemoteMemfwMemory(
            endpoint, owner_id="user-a", token="test-token", client=client
        )
        hits = await provider.recall("user-a", "query", 5, ["user"])

    assert [str(call.url) for call in recorder.calls] == [
        "http://memfw:18080" + HEALTH_PATH,
        "http://memfw:18080/search",
    ]
    assert [hit.key for hit in hits] == ["memory-1"]


@pytest.mark.asyncio
async def test_an_ipv6_endpoint_with_a_port_is_requested_verbatim():
    """``http://[::1]:8765`` keeps its brackets and its port for ``/search``.

    An IPv6 literal is a valid host, so the validated base keeps the square
    brackets the authority needs and the search line is requested verbatim
    rather than mangled into ``http://::1:8765/search`` and refused.
    """
    recorder = _Recorder()

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        provider = RemoteMemfwMemory(
            "http://[::1]:8765", owner_id="user-a", token="test-token", client=client
        )
        hits = await provider.recall("user-a", "query", 5, ["user"])

    assert [str(call.url) for call in recorder.calls] == ["http://[::1]:8765/search"]
    assert [hit.key for hit in hits] == ["memory-1"]


@pytest.mark.asyncio
async def test_an_endpoint_with_a_non_root_path_is_refused_without_a_search_request(
    caplog,
):
    """A non-root path is refused with ``bad_config`` and no request.

    The client runs the shared validation boundary before it holds anything it
    could build a URL from, so a path is refused -- with the contract's own
    ``bad_config`` code, or at the construction boundary that the health probe
    and the write bridge answer with the same code -- and the transport is
    never asked to send anything. Either refusal point satisfies the ruling;
    what this test pins is that **no request is built at all**.

    The audit runs over the whole captured text at DEBUG, every logger, with no
    filtering and no slicing of ``caplog.text``: if a raw-URL request ever comes
    back, ``httpx`` echoes it on its own line and the path token fails the test
    even where the other assertions were satisfied some other way.
    """
    caplog.set_level(logging.DEBUG)
    recorder = _Recorder()

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        with pytest.raises((MemfwContractError, ValueError)) as excinfo:
            provider = RemoteMemfwMemory(
                "http://memfw:18080/" + PATH_SENTINEL,
                owner_id="user-a",
                token="test-token",
                client=client,
            )
            await provider.recall("user-a", "query", 5, ["user"])

    error = excinfo.value
    assert recorder.calls == [], "a non-root path must not send a request"
    assert PATH_SENTINEL not in caplog.text
    if isinstance(error, MemfwContractError):
        assert error.error_code == "bad_config"
    else:
        # The construction boundary names the endpoint it refused.
        assert "endpoint" in str(error)


@pytest.mark.asyncio
async def test_remote_recall_maps_a_normal_hit():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://memfw:18080/search"
        assert request.headers["X-Memfw-Token"] == "test-token"
        assert json.loads(request.content) == {"query": "query", "limit": 5}
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {
                        "id": "memory-1",
                        "title": "Title",
                        "summary": "Summary",
                        "score": 0.8,
                        "tier": "hot",
                        "content_hash": "abc123",
                        "source": "fleet",
                        "tenant": "tenant-a",
                        "layer": "current",
                    }
                ]
            ),
        )

    async with _remote(handler) as provider:
        hits = await provider.recall("user-a", " query ", 5, ["user"])

    assert len(hits) == 1
    hit = hits[0]
    assert hit.key == "memory-1"
    assert hit.text == "Title: Summary"
    # The server's score is already normalised and is used as it is.
    assert hit.score == 0.8
    assert hit.tier == "user"
    assert hit.provenance["memfw_tier"] == "hot"
    assert hit.provenance["memory_metadata"]["tier"] == "hot"
    assert hit.provenance["content_hash"] == "abc123"
    assert hit.provenance["source"] == "fleet"
    assert hit.provenance["tenant"] == "tenant-a"
    assert hit.provenance["memory_metadata"]["layer"] == "current"


@pytest.mark.asyncio
async def test_remote_recall_carries_the_ontology_of_the_row():
    """The remote source's own ontology must survive the adapter.

    A memory that names a concept has to still name it when the Worker reads
    it; an adapter that drops the ontology hands over an annotated memory
    that looks unnamed, and the Ticket audit reports nothing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {
                        "id": "memory-1",
                        "title": "Title",
                        "score": 0.8,
                        "class": "Preference",
                        "about": ["project:conductor", "person:leo"],
                    }
                ]
            ),
        )

    async with _remote(handler) as provider:
        hits = await provider.recall("user-a", "query", 5, ["user"])

    assert hits[0].provenance["memory_metadata"]["class"] == "Preference"
    assert hits[0].provenance["memory_metadata"]["about"] == [
        "project:conductor",
        "person:leo",
    ]


@pytest.mark.asyncio
async def test_remote_recall_clamps_the_server_score():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {"id": "memory-low", "title": "Low", "score": -0.25},
                    {"id": "memory-high", "title": "High", "score": 1.5},
                    {"id": "memory-none", "title": "None", "score": None},
                ]
            ),
        )

    async with _remote(handler) as provider:
        hits = await provider.recall("user-a", "query", 5, ["user"])

    assert [hit.score for hit in hits] == [0.0, 1.0, 0.0]


@pytest.mark.asyncio
async def test_remote_recall_keeps_the_higher_server_score_first():
    """The defect this guards: the client used to invert the ranking."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {"id": "memory-best", "title": "Best", "score": 0.9},
                    {"id": "memory-worse", "title": "Worse", "score": 0.2},
                ]
            ),
        )

    async with _remote(handler) as provider:
        hits = await provider.recall("user-a", "query", 5, ["user"])

    assert [hit.key for hit in hits] == ["memory-best", "memory-worse"]
    assert hits[0].score == 0.9
    assert hits[1].score == 0.2
    assert hits[0].score > hits[1].score


@pytest.mark.asyncio
async def test_remote_recall_carries_temporal_and_related_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {
                        "id": "memory-1",
                        "title": "Title",
                        "summary": "Summary",
                        "score": 0.5,
                        "raw_score": -7.21,
                        "valid_from": VALID_FROM,
                        "valid_to": VALID_TO,
                        "superseded_by": "memory-2",
                        "related": ["memory-2", "memory-3"],
                    }
                ]
            ),
        )

    async with _remote(handler) as provider:
        hits = await provider.recall("user-a", "query", 5, ["user"])

    provenance = hits[0].provenance
    assert provenance["raw_score"] == -7.21
    assert provenance["valid_from"] == VALID_FROM
    assert provenance["valid_to"] == VALID_TO
    assert provenance["superseded_by"] == "memory-2"
    assert provenance["related"] == ["memory-2", "memory-3"]


@pytest.mark.asyncio
async def test_remote_recall_carries_mode_and_degraded():
    def handler(request: httpx.Request) -> httpx.Response:
        body = _ok_body([{"id": "memory-1", "title": "Title", "score": 0.5}])
        body["mode"] = "hybrid"
        body["degraded"] = True
        return httpx.Response(200, json=body)

    async with _remote(handler) as provider:
        hits = await provider.recall("user-a", "query", 5, ["user"])

    assert hits[0].provenance["mode"] == "hybrid"
    assert hits[0].provenance["degraded"] is True


@pytest.mark.asyncio
async def test_remote_recall_sends_the_tenant_when_one_is_set():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json=_ok_body([{"id": "memory-1", "title": "Title", "score": 0.5}])
        )

    async with _remote(handler, tenant="tenant-a") as provider:
        await provider.recall("user-a", "query", 5, ["user"])

    assert bodies[0] == {"query": "query", "limit": 5, "tenant": "tenant-a"}


@pytest.mark.asyncio
async def test_remote_recall_omits_the_tenant_when_none_is_set():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json=_ok_body([{"id": "memory-1", "title": "Title", "score": 0.5}])
        )

    async with _remote(handler) as provider:
        await provider.recall("user-a", "query", 5, ["user"])

    assert bodies[0] == {"query": "query", "limit": 5}
    assert "tenant" not in bodies[0]


@pytest.mark.asyncio
async def test_remote_recall_clamps_the_limit_to_fifty():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {"id": f"memory-{index}", "title": f"Title {index}", "score": 0.5}
                    for index in range(120)
                ]
            ),
        )

    async with _remote(handler) as provider:
        hits = await provider.recall("user-a", "query", 150, ["user"])

    assert bodies[0]["limit"] == MAX_RESULTS == 50
    assert len(hits) == 50
    assert hits[-1].key == "memory-49"


@pytest.mark.asyncio
async def test_remote_recall_clamps_a_limit_below_one():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json=_ok_body([{"id": "memory-1", "title": "Title", "score": 0.5}])
        )

    async with _remote(handler) as provider:
        await provider.recall("user-a", "query", 1, ["user"])

    assert bodies[0]["limit"] == 1


@pytest.mark.asyncio
async def test_remote_recall_truncates_the_query_to_512_characters():
    bodies: list[dict] = []
    query = "q" * 600

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200, json=_ok_body([{"id": "memory-1", "title": "Title", "score": 0.5}])
        )

    async with _remote(handler) as provider:
        await provider.recall("user-a", query, 5, ["user"])

    assert MAX_QUERY_LENGTH == 512
    assert len(bodies[0]["query"]) == 512
    assert bodies[0]["query"] == query[:512]


@pytest.mark.asyncio
async def test_remote_recall_rejects_a_response_without_a_contract():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{"id": "memory-1", "title": "Title", "score": 0.5}]}
        )

    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError) as excinfo:
            await provider.recall("user-a", "query", 5, ["user"])

    assert excinfo.value.error_code == "invalid_response"


@pytest.mark.asyncio
async def test_remote_recall_rejects_a_response_with_the_wrong_contract_major():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "contract": "memfw-http/3",
                "results": [{"id": "memory-1", "title": "Title", "score": 0.5}],
            },
        )

    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError) as excinfo:
            await provider.recall("user-a", "query", 5, ["user"])

    assert excinfo.value.error_code == "invalid_response"


@pytest.mark.asyncio
async def test_remote_recall_maps_a_contract_error_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "contract": "memfw-http/2",
                "error": {"code": "unauthorized", "message": "missing credential"},
            },
        )

    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError) as excinfo:
            await provider.recall("user-a", "query", 5, ["user"])

    assert excinfo.value.error_code == "unauthorized"


@pytest.mark.asyncio
async def test_remote_recall_maps_a_non_contract_error_body_through_the_status_table():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(413, json={"error": "too big"})

    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError) as excinfo:
            await provider.recall("user-a", "query", 5, ["user"])

    assert excinfo.value.error_code == "too_large"


@pytest.mark.asyncio
async def test_remote_recall_maps_an_unmapped_status_to_internal():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError) as excinfo:
            await provider.recall("user-a", "query", 5, ["user"])

    assert excinfo.value.error_code == "internal"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        pytest.param(
            httpx.ReadTimeout, "timeout", id="read-timeout"
        ),
        pytest.param(httpx.ConnectTimeout, "timeout", id="connect-timeout"),
        pytest.param(httpx.WriteTimeout, "timeout", id="write-timeout"),
        pytest.param(httpx.ConnectError, "unreachable", id="connect-error"),
        pytest.param(httpx.ReadError, "unreachable", id="read-error"),
        pytest.param(httpx.RemoteProtocolError, "unreachable", id="protocol-error"),
    ],
)
async def test_a_transport_failure_becomes_a_contract_error(failure, expected_code):
    """Contract section 6: a transport failure carries a contract code.

    The transport exception is not the contract's shape, so the client maps
    it onto its own codes: a timeout is a timeout, everything else is an
    unreachable peer.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("transport failed", request=request)

    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError) as excinfo:
            await provider.recall("user-a", "query", 5, ["user"])

    assert excinfo.value.error_code == expected_code


@pytest.mark.asyncio
async def test_a_transport_failure_does_not_carry_the_exception_text():
    """The transport exception may name the URL and the peer.

    The client reports a code, and the raised error carries nothing else.
    """
    sentinel = "sentinel-audit-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(sentinel, request=request)

    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError) as excinfo:
            await provider.recall("user-a", "query", 5, ["user"])

    error = excinfo.value
    assert error.error_code == "timeout"
    assert sentinel not in str(error)
    assert sentinel not in repr(error)
    assert sentinel not in error.args[0]
    # The chain is cut: the transport exception is neither the cause nor the
    # visible context of the error.
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


@pytest.mark.asyncio
async def test_a_transport_failure_logs_only_the_error_code(caplog):
    sentinel = "sentinel-audit-token"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(sentinel, request=request)

    caplog.set_level(logging.DEBUG, logger=remote_logger_name)
    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError):
            await provider.recall("user-a", "query", 5, ["user"])

    assert "error_code=unreachable" in caplog.text
    assert sentinel not in caplog.text


@pytest.mark.asyncio
async def test_remote_recall_rejects_a_body_that_is_not_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError) as excinfo:
            await provider.recall("user-a", "query", 5, ["user"])

    assert excinfo.value.error_code == "invalid_response"


@pytest.mark.asyncio
async def test_remote_recall_truncates_text_to_five_hundred_characters():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                [{"id": "memory-1", "title": "T" * 300, "summary": "S" * 300}]
            ),
        )

    async with _remote(handler) as provider:
        hits = await provider.recall("user-a", "query", 5, ["user"])

    assert len(hits[0].text) == 500


@pytest.mark.asyncio
async def test_remote_recall_ignores_a_row_without_an_id():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {"title": "No id", "score": 0.5},
                    {"id": "memory-1", "title": "Title", "score": 0.5},
                ]
            ),
        )

    async with _remote(handler) as provider:
        hits = await provider.recall("user-a", "query", 5, ["user"])

    assert [hit.key for hit in hits] == ["memory-1"]


@pytest.mark.asyncio
async def test_the_token_never_reaches_a_log_line(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "contract": "memfw-http/2",
                "error": {"code": "unauthorized", "message": "missing credential"},
            },
        )

    with caplog.at_level(logging.DEBUG):
        async with _remote(handler, token="super-secret-token") as provider:
            with pytest.raises(MemfwContractError) as excinfo:
                await provider.recall("user-a", "query", 5, ["user"])

    assert "super-secret-token" not in caplog.text
    assert "unauthorized" in caplog.text
    # The exception carries the code and nothing else.
    assert str(excinfo.value) == "unauthorized"
    assert "super-secret-token" not in str(excinfo.value)
    assert "super-secret-token" not in repr(excinfo.value)


@pytest.mark.asyncio
async def test_the_token_never_reaches_a_return_value(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_ok_body(
                [
                    {
                        "id": "memory-1",
                        "title": "Title",
                        "summary": "Summary",
                        "score": 0.5,
                        "content_hash": "sha256:abc",
                    }
                ]
            ),
        )

    with caplog.at_level(logging.DEBUG):
        async with _remote(handler, token="super-secret-token") as provider:
            hits = await provider.recall("user-a", "query", 5, ["user"])

    assert "super-secret-token" not in caplog.text
    assert "super-secret-token" not in json.dumps(hits[0].to_dict(), default=str)


@pytest.mark.asyncio
async def test_remote_notify_forget_reports_unsupported():
    provider = RemoteMemfwMemory(
        "http://memfw:18080", owner_id="user-a", token="test-token"
    )

    status = await provider.notify_forget(
        user_id="user-a", entry_id="memory-1", content_hash="abc123"
    )

    assert status == "unsupported"


@pytest.mark.asyncio
async def test_remote_source_rejects_writes():
    provider = RemoteMemfwMemory(
        "http://memfw:18080", owner_id="user-a", token="test-token"
    )
    entry = MemoryEntry(key="memory", text="text", tier="user")

    with pytest.raises(ReadOnlyMemoryProviderError):
        await provider.propose("user-a", entry)


@pytest.mark.asyncio
async def test_remote_recall_ignores_another_user():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request may be sent for another user")

    async with _remote(handler) as provider:
        hits = await provider.recall("user-b", "query", 5, ["user"])

    assert hits == []


@pytest.mark.asyncio
async def test_remote_recall_replays_the_canonical_search_fixture():
    request_body = _fixture("search.ok.request.json")
    response_body = _fixture("search.ok.response.json")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == request_body
        return httpx.Response(200, json=response_body)

    async with _remote(handler) as provider:
        hits = await provider.recall(
            "user-a", request_body["query"], request_body["limit"], ["user"]
        )

    assert len(hits) == len(response_body["results"])
    assert [hit.key for hit in hits] == [row["id"] for row in response_body["results"]]
    for hit, row in zip(hits, response_body["results"]):
        assert 0.0 <= hit.score <= 1.0
        assert hit.provenance["mode"] == response_body["mode"]
        assert hit.provenance["degraded"] == response_body["degraded"]
        assert hit.provenance["raw_score"] == row["raw_score"]
        assert hit.provenance["related"] == row["related"]
        assert hit.provenance["valid_from"] == row["valid_from"]
        assert hit.provenance["valid_to"] == row["valid_to"]
        assert hit.provenance["superseded_by"] == row["superseded_by"]


@pytest.mark.asyncio
async def test_remote_recall_replays_the_canonical_unauthorized_fixture():
    response_body = _fixture("search.unauthorized.response.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json=response_body)

    async with _remote(handler) as provider:
        with pytest.raises(MemfwContractError) as excinfo:
            await provider.recall("user-a", "query", 5, ["user"])

    assert excinfo.value.error_code == "unauthorized"


# --- the configured endpoint flows only into the validation boundary -------
#
# The Planner's ruling, pinned statically: a MemFW endpoint may only be a ROOT
# address, a non-root path is refused with ``bad_config`` and no request, and
# EVERY request is the single validated ``normalized_base`` plus the contract's
# fixed path. The tests above prove it at run time; this one reads the source,
# so a raw-URL request cannot come back without a test noticing. It lives here
# rather than in ``test_integration.py`` because that module carries a
# module-level ``pytestmark = pytest.mark.asyncio`` and this is a source audit.
#
# The audit is a small intra-procedural taint analysis over ``ast``:
#
# * a value is *tainted* when it is the configured endpoint -- read from the
#   environment (``os.environ.get("MEMFW_WRITE_URL")``, the script's
#   ``_env("MEMFW_READONLY_URL")``) or from the binding
#   (``binding.memfw_url``), or handed to a function whose named parameter is
#   the configured endpoint. Taint flows through assignments and through
#   pass-through calls (``.rstrip("/")``, ``.strip()``).
# * a value is *clean* when it comes back from the validation boundary
#   (``_validated_base`` / ``_reduced_base``) or from a validated base plus a
#   fixed path.
#
# Then three assertions, none of which can pass by being vacuous:
#
# 1. no call that puts a request on the wire receives a tainted value --
#   directly, through an alias, or inside a concatenation;
# 2. every such call builds its URL from a clean base plus one of the
#   contract's fixed paths, so no ad-hoc path can be spelled at a call site;
# 3. every module that reads a configured endpoint really does hand it to the
#   validation boundary, so deleting the check cannot pass the audit.

#: The files that read a configured MemFW endpoint, mapped to the functions
#: whose named parameter IS that configured endpoint.
_ENDPOINT_SOURCES: dict[str, dict[str, tuple[str, ...]]] = {
    "src/server/services/refactor/memory_federation/health.py": {
        "check": ("endpoint",),
        "check_contract": ("endpoint",),
    },
    "src/server/services/refactor/memory_federation/remote.py": {
        "__init__": ("endpoint",),
    },
    "src/server/services/refactor/memory_federation/integration.py": {
        "_build_remote_source": ("endpoint",),
    },
    "src/server/services/refactor/memory_federation/write_bridge.py": {
        "stage_approved_proposal": ("endpoint",),
    },
    "scripts/memfw_notes_mcp.py": {"check": ("endpoint",)},
}

#: The one validation boundary every configured endpoint must flow through.
_VALIDATION_FUNCTIONS = frozenset({"_validated_base", "_reduced_base"})

#: The environment readers a configured endpoint may arrive through.
_ENVIRONMENT_READERS = frozenset({"get", "getenv", "_env"})

#: Calls that pass a value through unchanged, so taint survives them.
_PASS_THROUGH = frozenset(
    {"rstrip", "lstrip", "strip", "lower", "upper", "format"}
)

#: The methods that put a request on the wire.
_REQUEST_METHODS = frozenset(
    {
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "head",
        "options",
        "request",
        "send",
        "stream",
    }
)
_REQUEST_TYPES = frozenset({"Request", "urlopen"})

#: The fixed contract paths a request line may append to the validated base.
_FIXED_PATHS = frozenset({"/health", "/search", "/related", "/stage", "/forget"})

_REPO_ROOT = Path(__file__).resolve().parents[6]


def _read_source(relative: str) -> str:
    return (_REPO_ROOT / relative).read_text(encoding="utf-8")


def _called_name(func: ast.expr) -> str:
    """The last name of a callee: ``client.post`` and ``urlopen`` alike."""
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _dotted(node: ast.expr) -> str:
    """``self.base`` as a string, for attribute-level taint."""
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _literal_string(node: ast.expr, constants: dict[str, str]) -> str | None:
    """The string a node names, resolving module-level constants."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """The module-level ``NAME = "literal"`` table."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                constants[target.id] = node.value.value
    return constants


def _is_endpoint_read(node: ast.expr) -> bool:
    """Whether the expression reads the configured endpoint.

    Either an environment read whose name carries ``URL``
    (``os.environ.get("MEMFW_WRITE_URL")``, ``os.getenv(...)``, the script's
    ``_env("MEMFW_READONLY_URL")``) or an attribute that names one
    (``binding.memfw_url``). The search is recursive, so a pass-through wrapper
    such as ``os.environ.get(...).strip()`` still reads as an endpoint read.
    """
    if isinstance(node, ast.Call):
        name = _called_name(node.func)
        first = node.args[0] if node.args else None
        if name in _ENVIRONMENT_READERS and first is not None:
            literal = _literal_string(first, {})
            if literal is not None and "URL" in literal.upper():
                return True
    if isinstance(node, ast.Attribute) and node.attr.upper().endswith("_URL"):
        return True
    return any(_is_endpoint_read(child) for child in ast.iter_child_nodes(node))


def _is_validation_call(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Call) and _called_name(node.func) in _VALIDATION_FUNCTIONS
    )


def _appended_path(node: ast.expr, constants: dict[str, str]) -> str | None:
    """The fixed path a URL expression appends, if it appends one."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_string(node.right, constants)
    return None


def _expression_state(
    node: ast.expr, states: dict[str, str], constants: dict[str, str]
) -> str | None:
    """``"tainted"``, ``"clean"`` or ``None`` for one right-hand side."""
    if _is_validation_call(node):
        return "clean"
    if _is_endpoint_read(node):
        return "tainted"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        # ``base + "/search"``: the path is only reachable when the left operand
        # is not the configured endpoint itself.
        left = _expression_state(node.left, states, constants)
        if left == "tainted":
            return "tainted"
        if _literal_string(node.right, constants) is not None:
            return "clean"
    if isinstance(node, ast.Name):
        return states.get(node.id)
    if isinstance(node, ast.Attribute):
        return states.get(_dotted(node))
    if isinstance(node, ast.Call) and _called_name(node.func) in _PASS_THROUGH:
        # ``endpoint.rstrip("/")`` keeps the configured value: the receiver and
        # the arguments are checked, not the call as a whole.
        receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
        for child in (receiver, *node.args):
            if child is None:
                continue
            if _expression_state(child, states, constants) == "tainted":
                return "tainted"
    return None


def _record(target: ast.expr, state: str, states: dict[str, str]) -> None:
    if isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _record(element, state, states)
        return
    if isinstance(target, ast.Name):
        states[target.id] = state
    elif isinstance(target, ast.Attribute):
        states[_dotted(target)] = state


def _flow_states(
    func: ast.FunctionDef | ast.AsyncFunctionDef,
    endpoint_parameters: tuple[str, ...],
    constants: dict[str, str],
    attributes: dict[str, str] | None = None,
) -> dict[str, str]:
    """``name -> "tainted" | "clean"`` for every assignment in one function.

    The pass follows source order and the last assignment wins, which is exact
    for these modules: every use of the configured endpoint comes after the
    assignment that produced it. ``attributes`` seeds the map with the
    ``self.x`` assignments of the whole module, because a client stores its
    validated base in ``__init__`` and spends it in another method.
    """
    states: dict[str, str] = dict(attributes or {})
    states.update({name: "tainted" for name in endpoint_parameters})
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if value is None:
            continue
        state = _expression_state(value, states, constants)
        if state is None:
            continue
        for target in targets:
            _record(target, state, states)
    return states


def _appended_paths(
    func: ast.FunctionDef | ast.AsyncFunctionDef, constants: dict[str, str]
) -> dict[str, str]:
    """``name -> fixed path`` for every local URL a function builds.

    ``stage_url = base + _STAGE_PATH`` spends the concatenation on a local, and
    the request call then reads ``client.post(stage_url)``. Resolving the local
    back to its path is what lets the audit name the path a call site used.
    """
    paths: dict[str, str] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        path = _appended_path(node.value, constants)
        if path is not None:
            paths[target.id] = path
    return paths


def _request_calls(func: ast.FunctionDef | ast.AsyncFunctionDef):
    """Every call in one function that puts a request on the wire."""
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and _called_name(node.func) in (
            _REQUEST_METHODS | _REQUEST_TYPES
        ):
            yield node


def _url_argument(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "url":
            return keyword.value
    return call.args[0] if call.args else None


def _is_url_expression(node: ast.expr) -> bool:
    """Whether the first argument could be a URL rather than a mapping key."""
    return isinstance(node, (ast.Name, ast.Attribute, ast.BinOp, ast.JoinedStr))


def test_the_configured_endpoint_flows_only_into_the_validation_boundary():
    """No call site may build a request from the raw configured endpoint.

    Every module that reads a configured MemFW endpoint hands it to the one
    validation boundary and keeps only the base it returns, so a request line
    can only ever be that base plus one of the contract's fixed paths. This
    test reads the source of all five of them and reports the first call site
    that consumes the configured value itself -- which is what reintroducing a
    raw-URL request looks like, and what the pre-ruling client did with
    ``self.endpoint + "/search"``.
    """
    offenders: list[str] = []
    appended: set[str] = set()

    trees = {
        relative: ast.parse(_read_source(relative)) for relative in _ENDPOINT_SOURCES
    }
    # One vocabulary of fixed-path constants is shared across these modules --
    # ``SEARCH_PATH`` is defined in ``health.py`` and imported by ``remote.py``
    # -- so a name is resolved against the union, then against the module's own
    # table.
    shared: dict[str, str] = {}
    for tree in trees.values():
        for name, value in _module_constants(tree).items():
            shared.setdefault(name, value)

    for relative, entry_points in _ENDPOINT_SOURCES.items():
        tree = trees[relative]
        constants = {**shared, **_module_constants(tree)}
        functions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        validated = False

        # ``self.x`` assignments cross method boundaries: the client keeps its
        # validated base from ``__init__`` and spends it in ``_post``.
        attributes: dict[str, str] = {}
        for func in functions:
            attributes.update(
                {
                    name: state
                    for name, state in _flow_states(
                        func, entry_points.get(func.name, ()), constants
                    ).items()
                    if "." in name
                }
            )

        for func in functions:
            states = _flow_states(
                func, entry_points.get(func.name, ()), constants, attributes
            )
            paths = _appended_paths(func, constants)
            for node in ast.walk(func):
                if not _is_validation_call(node):
                    continue
                for argument in node.args:
                    if _expression_state(argument, states, constants) == "tainted":
                        validated = True

            for call in _request_calls(func):
                url = _url_argument(call)
                if url is None or not _is_url_expression(url):
                    # ``payload.get("contract")`` and friends: not a request.
                    continue
                if _expression_state(url, states, constants) == "tainted":
                    offenders.append(
                        f"{relative}:{call.lineno} builds a request from the "
                        f"configured endpoint ({ast.unparse(call)})"
                    )
                    continue
                path = _appended_path(url, constants)
                if path is None:
                    # A plain name is only acceptable when this function built
                    # it: ``stage_url = base + _STAGE_PATH``, then
                    # ``client.post(stage_url)``.
                    if not (
                        isinstance(url, ast.Name)
                        and states.get(url.id) in {"clean", "tainted"}
                    ):
                        continue
                    path = paths.get(url.id, "")
                appended.add(path)
                if path and path not in _FIXED_PATHS:
                    offenders.append(
                        f"{relative}:{call.lineno} appends {path!r}, which is "
                        "not one of the contract's fixed paths"
                    )

        if not validated:
            offenders.append(
                f"{relative} never hands the configured endpoint to "
                f"{sorted(_VALIDATION_FUNCTIONS)}"
            )

    assert offenders == [], "\n".join(offenders)
    # The audit is live: the contract's own paths are what these modules spell,
    # so the analysis above really did walk the request sites.
    assert {"/health", "/search", "/related", "/stage"} <= appended
