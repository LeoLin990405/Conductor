"""Contract v2 behaviour of the MemFW write bridge.

Every request is served by a mock transport, so no host is ever contacted. The
tokens are fake values that must not show up in a result, in a log line or in
an exception message.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.server.services.refactor.memory_federation import write_bridge

FIXTURES = Path(__file__).resolve().parents[5] / "fixtures" / "memfw_contract_v2"

WRITE_TOKEN = "write-token-secret"
READ_TOKEN = "read-token-secret"
ENDPOINT = "http://memfw.invalid:18080"
#: The token the audit uses to prove that a value reached a log line.
SENTINEL = "sentinel-audit-token"

_STAGE_OK = {
    "contract": "memfw-http/2",
    "staged": True,
    "duplicate": False,
    "candidate_id": "conductor-proposal-1",
    "path": "_candidates/remember/conductor-proposal-1.md",
    "authority": False,
}


class _Recorder:
    """A mock transport handler that records every request it is given."""

    def __init__(self, handler=None) -> None:
        self.requests: list[httpx.Request] = []
        self.clients = 0
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._handler is None:
            return httpx.Response(200, json=_STAGE_OK)
        return self._handler(request)


def _mock_http(monkeypatch, recorder: _Recorder) -> None:
    """Route the bridge's HTTP client through the recorder."""
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(recorder)

    def factory(*args, **kwargs):
        recorder.clients += 1
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(write_bridge.httpx, "AsyncClient", factory)


async def _stage(*args, endpoint=ENDPOINT, **kwargs):
    return await write_bridge.stage_approved_proposal(
        *args, endpoint=endpoint, **kwargs
    )


@pytest.fixture
def bridge_env(monkeypatch):
    """An enabled bridge with a write token and an unreachable host."""
    monkeypatch.setenv("MEMFW_WRITE_BRIDGE", "1")
    monkeypatch.setenv("MEMFW_WRITE_URL", ENDPOINT)
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", WRITE_TOKEN)
    monkeypatch.delenv("MEMFW_WRITE_TOKEN_FILE", raising=False)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_READONLY_TOKEN_FILE", raising=False)


def _content_of_bytes(size: int) -> str:
    """Content of exactly ``size`` UTF-8 bytes, built from multi-byte characters."""
    filler = "€"  # three bytes in UTF-8
    text = filler * (size // 3)
    return text + "a" * (size - len(text.encode("utf-8")))


def _fixture(name: str) -> Any:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"contract fixture {name} is not available")
    return json.loads(path.read_text(encoding="utf-8"))


def _strings(value: Any) -> list[str]:
    """Every string reachable inside a result dictionary."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [item for part in value.values() for item in _strings(part)]
    if isinstance(value, (list, tuple)):
        return [item for part in value for item in _strings(part)]
    return []


# ---------------------------------------------------------------------------
# Token handling
# ---------------------------------------------------------------------------


def test_write_token_environment_precedes_file(monkeypatch, tmp_path):
    token_file = tmp_path / "write-token"
    token_file.write_text("file-token\n", encoding="utf-8")
    monkeypatch.setenv("MEMFW_WRITE_TOKEN", "env-token")
    monkeypatch.setenv("MEMFW_WRITE_TOKEN_FILE", str(token_file))

    assert write_bridge._token() == "env-token"

    monkeypatch.delenv("MEMFW_WRITE_TOKEN")
    assert write_bridge._token() == "file-token"


def test_write_token_is_never_taken_from_the_read_credential(monkeypatch):
    monkeypatch.delenv("MEMFW_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_WRITE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)

    assert write_bridge._token() == ""


@pytest.mark.asyncio
async def test_missing_write_token_sends_no_request(monkeypatch, caplog):
    monkeypatch.setenv("MEMFW_WRITE_BRIDGE", "1")
    monkeypatch.setenv("MEMFW_WRITE_URL", ENDPOINT)
    monkeypatch.delenv("MEMFW_WRITE_TOKEN", raising=False)
    monkeypatch.delenv("MEMFW_WRITE_TOKEN_FILE", raising=False)
    monkeypatch.setenv("MEMFW_READONLY_TOKEN", READ_TOKEN)
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)
    caplog.set_level(logging.DEBUG)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "write_token_missing",
    }
    assert recorder.requests == []
    assert recorder.clients == 0
    assert READ_TOKEN not in caplog.text


def test_write_bridge_never_mentions_the_read_credential():
    source = Path(write_bridge.__file__).read_text(encoding="utf-8")

    assert "READONLY" not in source
    assert "READONLY_TOKEN" not in source


# ---------------------------------------------------------------------------
# Size checks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_content_at_the_64_kib_boundary_is_staged(monkeypatch, bridge_env):
    content = _content_of_bytes(65536)
    assert len(content.encode("utf-8")) == 65536
    assert len(content) < 65536
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content=content
    )

    assert result["staged"] is True
    assert len(recorder.requests) == 1
    sent = json.loads(recorder.requests[0].content)
    assert sent["content"] == content


@pytest.mark.asyncio
async def test_content_one_byte_over_the_limit_is_refused(monkeypatch, caplog, bridge_env):
    content = _content_of_bytes(65537)
    assert len(content.encode("utf-8")) == 65537
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)
    caplog.set_level(logging.WARNING, logger=write_bridge.logger.name)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content=content
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "too_large",
    }
    assert recorder.requests == []
    assert "65537" in caplog.text
    assert "€" not in caplog.text
    assert WRITE_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_request_body_over_128_kib_is_refused(monkeypatch, caplog, bridge_env):
    # Control characters are escaped in JSON, so a content that is small enough
    # still produces a body that is too large.
    content = "\x00" * 25000
    assert len(content.encode("utf-8")) <= 65536
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)
    caplog.set_level(logging.WARNING, logger=write_bridge.logger.name)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content=content
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "too_large",
    }
    assert recorder.requests == []
    counts = [int(value) for value in re.findall(r"\b\d{5,}\b", caplog.text)]
    assert any(count > 131072 for count in counts)
    assert WRITE_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_only_one_warning_is_logged_for_an_oversized_candidate(
    monkeypatch, caplog, bridge_env
):
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)
    caplog.set_level(logging.WARNING, logger=write_bridge.logger.name)

    await _stage(
        {"proposal_id": "proposal-1"}, content=_content_of_bytes(70000)
    )

    warnings = [
        record
        for record in caplog.records
        if record.levelno >= logging.WARNING
        and record.name == write_bridge.logger.name
    ]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_the_measured_body_is_the_body_that_is_sent(monkeypatch, bridge_env):
    content = "€" * 1000
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    await _stage(
        {"proposal_id": "proposal-1"}, content=content
    )

    sent = recorder.requests[0].content
    measured = json.dumps(
        json.loads(sent),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert sent == measured


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_proposal_id_is_refused_and_never_truncated(
    monkeypatch, bridge_env
):
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal id with spaces"}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "bad_request",
    }
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_a_too_long_proposal_id_is_refused_instead_of_cut(
    monkeypatch, bridge_env
):
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "a" * 129}, content="approved content"
    )

    assert result["error_code"] == "bad_request"
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_proposal_id_at_the_limit_is_sent_verbatim(monkeypatch, bridge_env):
    proposal_id = "a" * 128
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": proposal_id}, content="approved content"
    )

    assert result["staged"] is True
    assert json.loads(recorder.requests[0].content)["proposal_id"] == proposal_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proposal_id",
    [
        pytest.param(" proposal-1", id="leading-space"),
        pytest.param("proposal-1 ", id="trailing-space"),
        pytest.param("proposal-1\n", id="trailing-newline"),
        pytest.param("\nproposal-1", id="leading-newline"),
        pytest.param("proposal-1\t", id="trailing-tab"),
        pytest.param("proposal-1\r\n", id="trailing-crlf"),
        pytest.param("proposal 1", id="inner-space"),
        pytest.param("", id="empty"),
        pytest.param("a" * 129, id="too-long"),
    ],
)
async def test_a_proposal_id_is_validated_as_a_whole_string(
    monkeypatch, bridge_env, proposal_id
):
    """Contract section 5: the id is trimmed by nobody.

    ``"proposal-1\\n"`` is a different id than ``"proposal-1"``, and the
    server would write the candidate under the other name. The rule is the
    contract's own: ``[A-Za-z0-9_-]{1,128}``, matched against the whole
    string. An invalid id is refused and no request is sent.
    """
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": proposal_id}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "bad_request",
    }
    assert recorder.requests == []
    assert recorder.clients == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proposal_id",
    [
        pytest.param("a", id="one-character"),
        pytest.param("a" * 128, id="128-characters"),
        pytest.param("Proposal_1-2", id="mixed-charset"),
    ],
)
async def test_a_valid_proposal_id_is_sent_verbatim(monkeypatch, bridge_env, proposal_id):
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": proposal_id}, content="approved content"
    )

    assert result["staged"] is True
    assert json.loads(recorder.requests[0].content)["proposal_id"] == proposal_id


@pytest.mark.asyncio
async def test_the_id_key_and_the_proposal_id_key_are_both_read(monkeypatch, bridge_env):
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"id": "proposal-from-id"}, content="approved content"
    )

    assert result["staged"] is True
    assert json.loads(recorder.requests[0].content)["proposal_id"] == "proposal-from-id"


@pytest.mark.asyncio
async def test_a_proposal_without_an_id_is_refused(monkeypatch, bridge_env):
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage({}, content="approved content")

    assert result["error_code"] == "bad_request"
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_a_non_string_proposal_id_is_refused(monkeypatch, bridge_env):
    """An id that is not a string is not converted into one."""
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": 12345}, content="approved content"
    )

    assert result["error_code"] == "bad_request"
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_title_is_capped_at_200_characters(monkeypatch, bridge_env):
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1", "title": "t" * 250},
        content="approved content",
    )

    assert result["staged"] is True
    assert json.loads(recorder.requests[0].content)["title"] == "t" * 200


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_returns_the_contract_fields(monkeypatch, bridge_env):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{ENDPOINT}/stage"
        assert request.headers["X-Memfw-Token"] == WRITE_TOKEN
        assert request.headers["Content-Type"] == "application/json"
        payload = json.loads(request.content)
        assert payload["proposal_id"] == "proposal-1"
        assert payload["content"] == "approved content"
        assert payload["tags"] == ["feedback"]
        assert payload["tenant"] == "tenant-a"
        assert payload["source"] == "conductor"
        return httpx.Response(200, json=_STAGE_OK)

    recorder = _Recorder(handler)
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1", "title": "Memory"},
        content="approved content",
        tags=["feedback"],
        tenant="tenant-a",
    )

    assert result == {
        "enabled": True,
        "staged": True,
        "duplicate": False,
        "candidate_id": "conductor-proposal-1",
        "path": "_candidates/remember/conductor-proposal-1.md",
        "authority": False,
    }


@pytest.mark.asyncio
async def test_duplicate_is_reported(monkeypatch, bridge_env):
    body = {**_STAGE_OK, "duplicate": True}
    recorder = _Recorder(lambda request: httpx.Response(200, json=body))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result["staged"] is True
    assert result["duplicate"] is True


@pytest.mark.asyncio
async def test_a_body_from_another_contract_version_is_refused(monkeypatch, bridge_env):
    """Contract section 5: the success body carries the contract marker."""
    body = {**_STAGE_OK, "contract": "memfw-http/1"}
    recorder = _Recorder(lambda request: httpx.Response(200, json=body))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "invalid_response",
    }


@pytest.mark.asyncio
async def test_a_body_without_the_contract_marker_is_refused(monkeypatch, bridge_env):
    body = {key: value for key, value in _STAGE_OK.items() if key != "contract"}
    recorder = _Recorder(lambda request: httpx.Response(200, json=body))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "invalid_response",
    }


@pytest.mark.asyncio
async def test_conflict_is_reported(monkeypatch, bridge_env):
    body = {
        "contract": "memfw-http/2",
        "error": {"code": "conflict", "message": "candidate exists"},
    }
    recorder = _Recorder(lambda request: httpx.Response(409, json=body))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "conflict",
    }


@pytest.mark.asyncio
async def test_unauthorized_is_reported(monkeypatch, bridge_env):
    body = {
        "contract": "memfw-http/2",
        "error": {"code": "unauthorized", "message": "bad token"},
    }
    recorder = _Recorder(lambda request: httpx.Response(401, json=body))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result["error_code"] == "unauthorized"


@pytest.mark.asyncio
async def test_forbidden_is_reported(monkeypatch, bridge_env):
    recorder = _Recorder(
        lambda request: httpx.Response(403, json=_fixture("stage.forbidden_read_token.response.json"))
    )
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result["error_code"] == "forbidden"


@pytest.mark.asyncio
async def test_a_non_contract_error_body_uses_the_status_table(monkeypatch, bridge_env):
    recorder = _Recorder(lambda request: httpx.Response(503, text="unavailable"))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "internal",
    }


@pytest.mark.asyncio
async def test_a_missing_resource_uses_the_status_table(monkeypatch, bridge_env):
    recorder = _Recorder(lambda request: httpx.Response(404, text="nope"))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result["error_code"] == "not_found"


@pytest.mark.asyncio
async def test_a_timeout_is_reported(monkeypatch, bridge_env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    recorder = _Recorder(handler)
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "timeout",
    }


@pytest.mark.asyncio
async def test_a_connection_failure_is_reported(monkeypatch, bridge_env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    recorder = _Recorder(handler)
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "unreachable",
    }


@pytest.mark.asyncio
async def test_an_unparseable_body_is_reported(monkeypatch, bridge_env):
    recorder = _Recorder(lambda request: httpx.Response(200, text="not json"))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "invalid_response",
    }


@pytest.mark.asyncio
async def test_a_body_that_is_not_an_object_is_reported(monkeypatch, bridge_env):
    recorder = _Recorder(lambda request: httpx.Response(200, json=["staged"]))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result["error_code"] == "invalid_response"


@pytest.mark.asyncio
async def test_process_wide_write_url_is_not_used_without_a_user_endpoint(
    monkeypatch, bridge_env
):
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content", endpoint=""
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "bad_config",
    }
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_secret_sensitivity_makes_no_request(monkeypatch, bridge_env):
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"},
        content="approved content",
        sensitivity="secret",
    )

    assert result["error_code"] == "secret_forbidden"
    assert recorder.requests == []


@pytest.mark.asyncio
async def test_a_disabled_bridge_makes_no_request(monkeypatch):
    monkeypatch.delenv("MEMFW_WRITE_BRIDGE", raising=False)
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result == {"enabled": False, "staged": False}
    assert recorder.requests == []
    assert recorder.clients == 0


# ---------------------------------------------------------------------------
# Secrets never leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_token_never_reaches_the_result_or_the_log(monkeypatch, caplog, bridge_env):
    recorder = _Recorder(
        lambda request: httpx.Response(
            500,
            json={"contract": "memfw-http/2", "error": {"code": "internal", "message": "x"}},
        )
    )
    _mock_http(monkeypatch, recorder)
    caplog.set_level(logging.DEBUG)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert all(WRITE_TOKEN not in text for text in _strings(result))
    assert WRITE_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_the_token_never_reaches_an_exception(monkeypatch, bridge_env):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out with {WRITE_TOKEN}", request=request)

    recorder = _Recorder(handler)
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result["error_code"] == "timeout"
    assert all(WRITE_TOKEN not in text for text in _strings(result))


# ---------------------------------------------------------------------------
# URL validation (the boundary the write bridge shares with the health probe)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "write_url",
    [
        pytest.param(ENDPOINT + "/stage?token=" + SENTINEL, id="query"),
        pytest.param(ENDPOINT + "/stage#" + SENTINEL, id="fragment"),
        pytest.param(
            "http://user:" + SENTINEL + "@memfw.invalid:18080", id="userinfo"
        ),
    ],
)
@pytest.mark.parametrize("outcome", ["ok", "http_error", "raises"])
@pytest.mark.asyncio
async def test_a_secret_write_url_is_refused_without_a_request(
    monkeypatch, caplog, bridge_env, write_url, outcome
):
    """A URL that carries a userinfo, a query or a fragment is refused.

    The write bridge shares the health probe's validation boundary, so such a
    URL cannot be reduced to a bare peer and nothing is sent: the transport is
    armed to succeed, fail or raise, yet none of that matters because a refused
    config never reaches it. The sentinel can reach neither a request line nor a
    log line. The audit runs over the whole captured text at DEBUG, across every
    logger -- including ``httpx`` and ``httpcore`` -- unfiltered and unsliced.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if outcome == "raises":
            raise httpx.ConnectError("connection refused", request=request)
        if outcome == "http_error":
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json=_STAGE_OK)

    monkeypatch.setenv("MEMFW_WRITE_URL", write_url)
    recorder = _Recorder(handler)
    _mock_http(monkeypatch, recorder)
    caplog.set_level(logging.DEBUG)

    result = await _stage(
        {"proposal_id": "proposal-1"},
        content="approved content",
        endpoint=write_url,
    )

    assert result == {
        "enabled": True,
        "staged": False,
        "error_code": "bad_config",
    }
    assert recorder.requests == [], "a refused config must not send a request"
    assert recorder.clients == 0
    assert SENTINEL not in caplog.text


@pytest.mark.asyncio
async def test_the_stage_url_is_rebuilt_from_the_validated_peer(
    monkeypatch, caplog, bridge_env
):
    """The request line is the validated peer plus the fixed stage path.

    The configured value is reduced to its peer, so nothing of it rides along:
    the bridge posts to the validated scheme, host and port plus ``/stage`` and
    nothing else. The audit runs over the whole captured text at DEBUG.
    """
    monkeypatch.setenv("MEMFW_WRITE_URL", ENDPOINT + "/")
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)
    caplog.set_level(logging.DEBUG)

    result = await _stage(
        {"proposal_id": "proposal-1"}, content="approved content"
    )

    assert result["staged"] is True
    assert len(recorder.requests) == 1
    assert str(recorder.requests[0].url) == ENDPOINT + "/stage"
    assert "configured" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write_url", "expected"),
    [
        pytest.param(
            "http://[::1]:8765", "http://[::1]:8765/stage", id="ipv6-with-port"
        ),
        pytest.param("http://[::1]", "http://[::1]/stage", id="ipv6-without-port"),
        pytest.param(
            "http://127.0.0.1:8765", "http://127.0.0.1:8765/stage", id="ipv4"
        ),
    ],
)
async def test_the_stage_url_keeps_ipv6_brackets_and_leaves_ipv4_alone(
    monkeypatch, bridge_env, write_url, expected
):
    """The rebuild keeps IPv6 brackets and leaves IPv4 and hostnames alone.

    ``urlsplit`` reports an IPv6 host without its brackets, so the shared
    rebuild wraps it back: ``http://[::1]:8765`` is requested verbatim rather
    than mangled into ``http://::1:8765``. The write bridge uses the same
    function as the health probe, so it inherits the fix. The URL the transport
    received is asserted exactly.
    """
    monkeypatch.setenv("MEMFW_WRITE_URL", write_url)
    recorder = _Recorder()
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "proposal-1"},
        content="approved content",
        endpoint=write_url,
    )

    assert result["staged"] is True
    assert str(recorder.requests[0].url) == expected


# ---------------------------------------------------------------------------
# Shared contract samples
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_shared_stage_sample_is_parsed(monkeypatch, bridge_env):
    body = _fixture("stage.ok.response.json")
    recorder = _Recorder(lambda request: httpx.Response(200, json=body))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "sample-ok"}, content="approved content"
    )

    assert result["staged"] is True
    assert result["duplicate"] is False
    assert isinstance(result["candidate_id"], str)
    assert isinstance(result["path"], str)
    assert result["path"].startswith("_candidates/")
    assert result["authority"] is False


@pytest.mark.asyncio
async def test_the_shared_conflict_sample_is_reported(monkeypatch, bridge_env):
    body = _fixture("stage.conflict.response.json")
    recorder = _Recorder(lambda request: httpx.Response(409, json=body))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "sample-conflict"}, content="approved content"
    )

    assert result["error_code"] == "conflict"


@pytest.mark.asyncio
async def test_the_shared_duplicate_sample_is_reported(monkeypatch, bridge_env):
    body = _fixture("stage.duplicate.response.json")
    recorder = _Recorder(lambda request: httpx.Response(200, json=body))
    _mock_http(monkeypatch, recorder)

    result = await _stage(
        {"proposal_id": "sample-duplicate"}, content="approved content"
    )

    assert result["staged"] is True
    assert result["duplicate"] is True
