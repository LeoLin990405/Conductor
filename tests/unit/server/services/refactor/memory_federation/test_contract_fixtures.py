"""The canonical MemFW samples drive the client parsers.

Contract section 9 fixes a set of thirteen request/response pairs. MemFW owns
them; Conductor copies them byte for byte into
``tests/fixtures/memfw_contract_v2/`` and replays its own client against them.
This module proves three things:

* the copy is complete and serialised the way the sample README says;
* the client parses the canonical bodies the way the contract describes --
  structure, types, value ranges and error codes, never a concrete score, id
  or timestamp;
* while both copies are on disk they stay byte identical.

No HTTP ever leaves the process: the samples are replayed through
``httpx.MockTransport`` and a stubbed ``urlopen``.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.server.services.refactor.memory_federation.errors import (
    MemfwContractError,
    parse_error_code,
)
from src.server.services.refactor.memory_federation.health import contract_major
from src.server.services.refactor.memory_federation.remote import RemoteMemfwMemory

ROOT = Path(__file__).resolve().parents[6]
FIXTURES = ROOT / "tests" / "fixtures" / "memfw_contract_v2"
#: The MemFW-side copy of the samples, when it is on this machine. There is no
#: default: a personal path must not be committed, so the comparison is skipped
#: unless an operator points the variable at the MemFW checkout.
_MEMFW_FIXTURES_DIR = os.environ.get("MEMFW_CONTRACT_FIXTURES_DIR", "").strip()
MEMFW_FIXTURES = Path(_MEMFW_FIXTURES_DIR) if _MEMFW_FIXTURES_DIR else None

CONTRACT = "memfw-http/2"

#: The thirteen canonical cases and the HTTP status each one answers with.
#: The status belongs to the case, not to the file (see the sample README).
CASES: dict[str, int] = {
    "health.ok": 200,
    "search.ok": 200,
    "search.as_of_superseded_after_t": 200,
    "search.unauthorized": 401,
    "search.too_large": 413,
    "related.edges_ok": 200,
    "related.fts_degraded": 200,
    "related.not_found": 404,
    "stage.ok": 200,
    "stage.duplicate": 200,
    "stage.conflict": 409,
    "stage.forbidden_read_token": 403,
    "stage.too_large": 413,
}

#: Contract section 7: the reasons vocabulary.
REASONS = frozenset(
    {
        "link",
        "related_field",
        "shared_tag",
        "shared_course",
        "shared_entity",
        "lexical",
        "semantic",
        "section",
    }
)


def _read(name: str) -> dict[str, Any]:
    document = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _load_notes_mcp():
    """The Notes MCP script, loaded the way its own test file loads it."""
    script = ROOT / "scripts" / "memfw_notes_mcp.py"
    spec = importlib.util.spec_from_file_location("memfw_notes_mcp_contract", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_the_canonical_set_is_complete() -> None:
    """All thirteen pairs plus the README are present."""
    assert (FIXTURES / "README.md").is_file()
    for case in CASES:
        assert (FIXTURES / f"{case}.request.json").is_file(), case
        assert (FIXTURES / f"{case}.response.json").is_file(), case
    assert len(list(FIXTURES.glob("*.json"))) == 2 * len(CASES)


def test_every_sample_is_serialised_the_way_the_readme_says() -> None:
    """The files are exactly ``json.dumps(..., indent=2, sort_keys=True) + "\\n"``."""
    for path in sorted(FIXTURES.glob("*.json")):
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
        expected = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        assert raw == expected, path.name


def test_every_response_carries_the_contract_marker() -> None:
    """The contract string is what the client negotiates on."""
    for case in CASES:
        body = _read(f"{case}.response.json")
        assert body.get("contract") == CONTRACT, case
        assert contract_major(body.get("contract")) == 2, case


@pytest.mark.asyncio
async def test_the_client_replays_the_canonical_search_request() -> None:
    """The client's own request is the canonical one, byte for byte."""
    request_body = _read("search.ok.request.json")
    response_body = _read("search.ok.response.json")
    seen: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        return httpx.Response(200, json=response_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RemoteMemfwMemory(
            "http://memfw.invalid:18080",
            owner_id="user-a",
            token="test-token",
            client=client,
        )
        hits = await provider.recall(
            "user-a", request_body["query"], request_body["limit"], ["user"]
        )

    assert json.loads(seen[0]) == request_body
    assert len(hits) == len(response_body["results"])
    for hit in hits:
        assert 0.0 <= hit.score <= 1.0
        assert hit.source == "memfw"
        assert hit.key
    # A higher server score must not be ranked below a lower one.
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_the_client_keeps_the_canonical_fields() -> None:
    """Every field the contract puts on a result reaches the provenance."""
    response_body = _read("search.ok.response.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RemoteMemfwMemory(
            "http://memfw.invalid:18080",
            owner_id="user-a",
            token="test-token",
            client=client,
        )
        hits = await provider.recall("user-a", "corpus keyword", 3, ["user"])

    assert hits
    for hit, row in zip(hits, response_body["results"]):
        provenance = hit.provenance
        for key in (
            "raw_score",
            "valid_from",
            "valid_to",
            "superseded_by",
            "related",
            "mode",
            "degraded",
            "content_hash",
        ):
            assert key in provenance, key
        assert provenance["mode"] == response_body["mode"]
        assert provenance["degraded"] == response_body["degraded"]
        if row.get("raw_score") is not None:
            assert provenance["raw_score"] == pytest.approx(row["raw_score"])


@pytest.mark.asyncio
async def test_the_client_replays_the_canonical_as_of_case() -> None:
    """A document superseded after T is still returned at T."""
    response_body = _read("search.as_of_superseded_after_t.response.json")
    request_body = _read("search.as_of_superseded_after_t.request.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RemoteMemfwMemory(
            "http://memfw.invalid:18080",
            owner_id="user-a",
            token="test-token",
            client=client,
        )
        hits = await provider.recall(
            "user-a", request_body["query"], request_body.get("limit", 10), ["user"]
        )

    assert len(hits) == len(response_body["results"])
    assert any(hit.provenance.get("superseded_by") for hit in hits)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "status", "expected_code"),
    [
        ("search.unauthorized", 401, "unauthorized"),
        ("search.too_large", 413, "too_large"),
    ],
)
async def test_the_client_passes_the_canonical_error_codes_through(
    case: str, status: int, expected_code: str
) -> None:
    """The canonical error bodies produce the contract's error codes."""
    body = _read(f"{case}.response.json")
    assert parse_error_code(body, status) == expected_code

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = RemoteMemfwMemory(
            "http://memfw.invalid:18080",
            owner_id="user-a",
            token="test-token",
            client=client,
        )
        with pytest.raises(MemfwContractError) as caught:
            await provider.recall("user-a", "corpus keyword", 3, ["user"])

    assert caught.value.error_code == expected_code


def _stage_request(case: str = "stage.ok") -> dict[str, Any]:
    return _read(f"{case}.request.json")


async def _stage(monkeypatch, body: dict[str, Any], status: int) -> dict[str, Any]:
    """Run the stage bridge against a canned response."""
    from src.server.services.refactor.memory_federation import write_bridge

    sent: list[Any] = []

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        sent.append(kwargs.get("json"))
        return httpx.Response(status, json=body)

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    # The bridge is off unless it is switched on, and it needs its own
    # endpoint and its own credential. The values are fake and local.
    monkeypatch.setenv("MEMFW_WRITE_BRIDGE", "1")
    monkeypatch.setenv("MEMFW_WRITE_URL", "http://memfw.invalid:18080")
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", "write-token-secret")
    result = await write_bridge.stage_approved_proposal(
        {"id": body.get("proposal_id", "sample-ok"), "title": "Staged sample"},
        content="Staged sample body.",
        endpoint="http://memfw.invalid:18080",
        tags=["sample"],
        tenant="tenant-a",
        source="conductor",
    )
    result["_sent"] = sent
    return result


@pytest.mark.asyncio
async def test_the_write_bridge_replays_the_canonical_stage_request(monkeypatch) -> None:
    """The bridge sends the canonical stage request and reads the reply."""
    request_body = _stage_request("stage.ok")
    response_body = _read("stage.ok.response.json")

    result = await _stage(monkeypatch, response_body, 200)
    sent = result.pop("_sent")

    assert sent, "the bridge did not send a request"
    for key in ("proposal_id", "title", "content", "tags", "tenant", "source"):
        assert sent[0][key] == request_body[key], key
    assert result["staged"] is True
    assert result["duplicate"] == response_body["duplicate"]
    assert result["candidate_id"] == response_body["candidate_id"]
    assert result["path"] == response_body["path"]


@pytest.mark.asyncio
async def test_the_write_bridge_replays_the_canonical_duplicate_case(monkeypatch) -> None:
    """A duplicate is a success that did not rewrite the candidate."""
    response_body = _read("stage.duplicate.response.json")

    result = await _stage(monkeypatch, response_body, 200)
    result.pop("_sent", None)

    assert result["staged"] is True
    assert result["duplicate"] is True
    assert result["candidate_id"] == response_body["candidate_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "status", "expected_code"),
    [
        ("stage.conflict", 409, "conflict"),
        ("stage.forbidden_read_token", 403, "forbidden"),
        ("stage.too_large", 413, "too_large"),
    ],
)
async def test_the_write_bridge_passes_the_canonical_error_codes_through(
    monkeypatch, case: str, status: int, expected_code: str
) -> None:
    """A canonical stage failure becomes the contract's error code."""
    body = _read(f"{case}.response.json")
    assert parse_error_code(body, status) == expected_code

    result = await _stage(monkeypatch, body, status)
    result.pop("_sent", None)

    assert result["staged"] is False
    assert result["error_code"] == expected_code


def test_the_canonical_related_samples_keep_the_contract_shape() -> None:
    """Mode, degraded and the reasons vocabulary are what the contract says.

    The related score is **not** range-checked here: contract section 3 fixes a
    normalised ``[0, 1]`` score for ``/search``, but section 7 states no range
    for ``/related``. The service's edge mode passes the raw edge score
    through, so the two related modes are on different scales. The client must
    pass the value on unchanged rather than clamp it.
    """
    for case in ("related.edges_ok", "related.fts_degraded"):
        body = _read(f"{case}.response.json")
        assert body["mode"] in {"fts", "hybrid", "edges"}
        assert isinstance(body["degraded"], bool)
        assert isinstance(body["results"], list)
        for row in body["results"]:
            assert set(row["reasons"]) <= REASONS
            assert isinstance(row["score"], (int, float))
            assert not isinstance(row["score"], bool)
            assert -1e9 < float(row["score"]) < 1e9


def test_the_degraded_related_sample_says_it_is_degraded() -> None:
    """Without an edge index the service falls back to FTS and says so."""
    body = _read("related.fts_degraded.response.json")
    assert body["mode"] == "fts"
    assert body["degraded"] is True
    for row in body["results"]:
        assert row["reasons"] == ["lexical"]


def test_the_not_found_related_sample_carries_the_error_code() -> None:
    """A missing memory is a 404 with the contract's error shape."""
    body = _read("related.not_found.response.json")
    assert body["error"]["code"] == "not_found"


def test_the_notes_mcp_replays_the_canonical_related_response(monkeypatch) -> None:
    """The Notes MCP passes mode, degraded and reasons through."""
    module = _load_notes_mcp()
    related_body = _read("related.edges_ok.response.json")
    health_body = _read("health.ok.response.json")
    seen: list[str] = []

    class _Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self._payload = payload

        def read(self) -> bytes:
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    def fake_urlopen(request: object, timeout: float | None = None) -> _Response:
        url = str(getattr(request, "full_url", ""))
        seen.append(url)
        return _Response(health_body if url.endswith("/health") else related_body)

    async def fake_get(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        seen.append(str(url))
        return httpx.Response(200, json=health_body)

    async def fake_post(self: httpx.AsyncClient, url: str, **kwargs: Any) -> httpx.Response:
        seen.append(str(url))
        return httpx.Response(200, json=related_body)

    # The script's own opener is the seam, not urllib.request.urlopen: the script
    # builds a private opener that refuses redirects and never calls the
    # module-level urlopen. One line, no assertion changed.
    monkeypatch.setattr(module._OPENER, "open", fake_urlopen)
    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    monkeypatch.setenv("MEMFW_READONLY_URL", "http://memfw.invalid:18080")
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", "test-token")

    result = module.memory_related("mem-alpha", limit=10)

    assert result["ok"] is True
    assert result["mode"] == related_body["mode"]
    assert result["degraded"] == related_body["degraded"]
    assert result["related"] == related_body["results"]
    assert seen, "the tool did not call the service"


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_two_copies_are_byte_identical(case: str) -> None:
    """While both copies exist, they are the same bytes."""
    if MEMFW_FIXTURES is None or not MEMFW_FIXTURES.is_dir():
        pytest.skip("the MemFW sample directory is not available on this machine")
    for suffix in ("request.json", "response.json"):
        name = f"{case}.{suffix}"
        ours = FIXTURES / name
        theirs = MEMFW_FIXTURES / name
        if not theirs.is_file():
            pytest.skip(f"{name} is not in the MemFW sample directory")
        assert ours.read_bytes() == theirs.read_bytes(), name
