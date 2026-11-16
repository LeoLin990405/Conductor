"""The contract negotiation probe and the log lines it produces.

Contract section 1: the client checks the major version once per endpoint
before it uses a remote source. Everything the probe learns comes from the
service, and everything the endpoint comes from an operator's configuration,
so neither the raw contract string nor the URL beyond scheme, host and port
may reach a log line of ours.

The configured URL is validated before anything is sent: one that carries a
userinfo, a query, a fragment or a non-root path cannot be reduced to a bare
peer, so it is refused with a stable reason and no request is made. When a
request is made its line is rebuilt from the validated scheme, host and port
plus the one fixed health path, so the raw configured string never reaches the
client.

Every audit below runs over the **whole** captured text at DEBUG, across every
logger -- including ``httpx`` and ``httpcore``, which echo the request URL on
their own. Nothing is filtered by logger name and no slice of the text is taken,
so a secret that reaches any log line fails the test instead of being hidden.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from src.server.services.refactor.memory_federation.health import (
    HEALTH_PATH,
    REASON_BAD_CONFIG,
    HealthCache,
    check_contract,
    safe_endpoint,
)

#: A reserved host name: it never resolves, so a stray request cannot reach
#: a real machine.
ENDPOINT = "http://memfw.invalid:18080"

#: The token the audit uses to prove that a value reached a log line.
SENTINEL = "sentinel-audit-token"
#: The path token the audit uses to prove that a configured path reached a
#: request line or a log line.
PATH_SENTINEL = "path-marker"


class _Recorder:
    """An ``httpx`` transport handler that records every request it is handed.

    ``outcome`` chooses what the handler answers: a ``200`` contract body
    (``"ok"``), a non-200 status (``"http_error"``) or a raised transport error
    (``"raises"``). Because the recorder *is* the transport, ``calls`` is
    exactly the set of requests the client was asked to send -- the audit's
    proof that a refused configuration sends none.
    """

    def __init__(self, *, outcome: str = "ok") -> None:
        self.outcome = outcome
        self.calls: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.outcome == "raises":
            raise httpx.ConnectError("connection refused", request=request)
        if self.outcome == "http_error":
            return httpx.Response(503, json={"error": "unavailable"}, request=request)
        return httpx.Response(
            200, json={"ok": True, "contract": "memfw-http/3"}, request=request
        )


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://memfw.invalid:18080", "http://memfw.invalid:18080"),
        ("https://memfw.invalid", "https://memfw.invalid"),
        ("http://memfw.invalid:18080/", "http://memfw.invalid:18080"),
        ("http://memfw.invalid:18080/v1/search", "http://memfw.invalid:18080"),
        (
            "http://memfw.invalid:18080/v1?token=" + SENTINEL,
            "http://memfw.invalid:18080",
        ),
        (
            "http://memfw.invalid:18080/v1#token=" + SENTINEL,
            "http://memfw.invalid:18080",
        ),
        (
            "http://user:password@memfw.invalid:18080/v1",
            "http://memfw.invalid:18080",
        ),
        ("not a url at all", "<endpoint>"),
        ("", "<endpoint>"),
    ],
)
def test_safe_endpoint_keeps_only_scheme_host_and_port(endpoint, expected):
    """A log line keeps what identifies the peer and nothing else."""
    assert safe_endpoint(endpoint) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://memfw.invalid:18080/v1?token=" + SENTINEL,
        "http://user:" + SENTINEL + "@memfw.invalid:18080/v1",
        "http://memfw.invalid:18080/v1#" + SENTINEL,
    ],
)
def test_safe_endpoint_never_carries_the_sentinel(endpoint):
    assert SENTINEL not in safe_endpoint(endpoint)


# --- the configured URL is validated before any request ---------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        pytest.param(ENDPOINT + "/v1?token=" + SENTINEL, id="query"),
        pytest.param(ENDPOINT + "/v1#" + SENTINEL, id="fragment"),
        pytest.param(
            "http://user:" + SENTINEL + "@memfw.invalid:18080/v1", id="userinfo"
        ),
    ],
)
@pytest.mark.parametrize("outcome", ["ok", "http_error", "raises"])
@pytest.mark.asyncio
async def test_a_config_with_a_secret_url_is_refused_without_a_request(
    caplog, endpoint, outcome
):
    """A URL that carries a userinfo, a query or a fragment is refused.

    The refusal happens before any request, so the transport is never asked to
    send one and the sentinel cannot reach the request line or the ``httpx``
    logger that echoes it. The transport is armed to succeed, fail or raise, yet
    none of that can matter: a refused configuration sends nothing. The
    assertion runs over the whole captured text at DEBUG -- every logger.
    """
    caplog.set_level(logging.DEBUG)
    recorder = _Recorder(outcome=outcome)
    cache = HealthCache(cache_seconds=300.0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        state = await check_contract(endpoint, client=client, cache=cache)

    assert state.compatible is False
    assert state.reachable is False
    assert state.reason == REASON_BAD_CONFIG
    assert recorder.calls == [], "a refused config must not send a request"
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize("outcome", ["ok", "http_error", "raises"])
@pytest.mark.asyncio
async def test_the_probe_rebuilds_the_request_line_from_the_validated_peer(
    caplog, outcome
):
    """The request line is rebuilt from the validated peer plus the fixed path.

    The configured string is handed to the client only after it has been reduced
    to scheme, host and port and rebuilt with the fixed health path, so the
    configured value never reaches ``httpx`` -- which logs the request URL on its
    own. This holds whether the request succeeds, fails or raises.
    """
    caplog.set_level(logging.DEBUG)
    endpoint = ENDPOINT + "/"
    recorder = _Recorder(outcome=outcome)
    cache = HealthCache(cache_seconds=300.0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        state = await check_contract(endpoint, client=client, cache=cache)

    assert len(recorder.calls) == 1
    assert str(recorder.calls[0].url) == ENDPOINT + HEALTH_PATH
    assert SENTINEL not in caplog.text
    # The probe ran, so its own outcome is reported faithfully and no stable
    # config-refusal reason is attached to a request that was actually made.
    assert state.reason is None


@pytest.mark.parametrize("outcome", ["ok", "http_error", "raises"])
@pytest.mark.asyncio
async def test_an_endpoint_with_a_non_root_path_is_refused_without_a_request(
    caplog, outcome
):
    """A MemFW endpoint may only be a root address.

    The Planner's ruling: the configured endpoint is ``scheme://host[:port]``
    with an empty path, and a non-root path is refused with ``bad_config`` and
    **no request**. The refusal happens before anything is sent, so the
    transport is never asked to send one and the path cannot reach a request
    line -- nor the ``httpx`` and ``httpcore`` loggers that echo one on their
    own. The transport is armed to succeed, fail or raise, and none of that can
    matter: a refused configuration sends nothing.

    The audit runs over the **whole** captured text at DEBUG, across every
    logger, with no filtering and no slicing of ``caplog.text``, so the path
    token fails the test wherever it lands. A second assertion proves the
    capture is live, so the audit cannot pass by having logged nothing at all.
    """
    caplog.set_level(logging.DEBUG)
    endpoint = ENDPOINT + "/" + PATH_SENTINEL
    recorder = _Recorder(outcome=outcome)
    cache = HealthCache(cache_seconds=300.0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        state = await check_contract(endpoint, client=client, cache=cache)

    assert state.compatible is False
    assert state.reachable is False
    assert state.reason == REASON_BAD_CONFIG
    assert recorder.calls == [], "a non-root path must not send a request"
    assert PATH_SENTINEL not in caplog.text
    # The audit is live: the refusal really was logged, so an empty capture
    # cannot pass by silence.
    assert "configuration refused" in caplog.text


@pytest.mark.asyncio
async def test_a_root_address_with_a_trailing_slash_is_normalised_before_the_probe(
    caplog,
):
    """A root address with a trailing slash is normalised, not doubled.

    ``scheme://host[:port]`` plus ``/`` is still a root address, so it is
    accepted and normalised to the bare peer: the request line carries one
    separator, never two. The URL the transport received is asserted exactly,
    and the whole DEBUG capture across every logger carries no path token.
    """
    caplog.set_level(logging.DEBUG)
    endpoint = ENDPOINT + "/"
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"ok": True, "contract": "memfw-http/2"}, request=request
        )

    cache = HealthCache(cache_seconds=300.0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        state = await check_contract(endpoint, client=client, cache=cache)

    assert state.compatible is True
    assert state.reason is None
    assert state.major == 2
    assert [str(call.url) for call in calls] == [ENDPOINT + HEALTH_PATH]
    assert PATH_SENTINEL not in caplog.text


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        pytest.param(
            "http://[::1]:8765", "http://[::1]:8765" + HEALTH_PATH, id="ipv6-with-port"
        ),
        pytest.param(
            "http://[::1]", "http://[::1]" + HEALTH_PATH, id="ipv6-without-port"
        ),
        pytest.param(
            "http://127.0.0.1:8765", "http://127.0.0.1:8765" + HEALTH_PATH, id="ipv4"
        ),
        pytest.param(ENDPOINT, ENDPOINT + HEALTH_PATH, id="hostname"),
    ],
)
@pytest.mark.asyncio
async def test_the_probe_keeps_ipv6_brackets_and_leaves_ipv4_and_hostnames_alone(
    caplog, endpoint, expected
):
    """The rebuilt request line keeps IPv6 brackets and is otherwise unchanged.

    ``urlsplit`` reports an IPv6 host without its square brackets, so the
    rebuild wraps it back in ``[...]``: ``http://[::1]:8765`` is requested
    verbatim rather than mangled into ``http://::1:8765`` and refused. IPv4
    literals and hostnames carry no colon and are left exactly as configured.
    The URL the transport received is asserted exactly.
    """
    caplog.set_level(logging.DEBUG)
    recorder = _Recorder(outcome="ok")
    cache = HealthCache(cache_seconds=300.0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as client:
        state = await check_contract(endpoint, client=client, cache=cache)

    assert len(recorder.calls) == 1
    assert str(recorder.calls[0].url) == expected
    assert state.reason is None


@pytest.mark.asyncio
async def test_the_probe_logs_no_token_from_the_contract_string(caplog):
    """The contract string comes from the service and is not trusted."""
    endpoint = ENDPOINT

    def handler(request):
        return httpx.Response(
            200,
            json={"ok": True, "contract": "memfw-http/3/" + SENTINEL},
            request=request,
        )

    cache = HealthCache(cache_seconds=300.0)
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        state = await check_contract(endpoint, client=client, cache=cache)

    assert state.compatible is False
    assert state.major is None
    # The whole captured text, every logger, carries no part of the contract.
    assert SENTINEL not in caplog.text
    # The warning names the version it could not validate, and nothing else.
    assert "speaks contract major" in caplog.text
    assert "<unknown>" in caplog.text


@pytest.mark.asyncio
async def test_the_probe_logs_the_validated_major_and_not_the_raw_contract(caplog):
    """A contract of another major version is named by its major only."""

    def handler(request):
        return httpx.Response(
            200,
            json={"ok": True, "contract": "memfw-http/3"},
            request=request,
        )

    cache = HealthCache(cache_seconds=300.0)
    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        state = await check_contract(ENDPOINT, client=client, cache=cache)

    assert state.compatible is False
    assert state.major == 3
    assert "contract major 3" in caplog.text
    # The raw contract string never reaches a log line -- only its major does.
    assert "memfw-http/3" not in caplog.text


@pytest.mark.asyncio
async def test_a_successful_probe_keeps_the_major_and_nothing_else():
    def handler(request):
        return httpx.Response(
            200,
            json={"ok": True, "contract": "memfw-http/2"},
            request=request,
        )

    cache = HealthCache(cache_seconds=300.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        state = await check_contract(ENDPOINT, client=client, cache=cache)

    assert state.compatible is True
    assert state.reachable is True
    assert state.major == 2
    # The raw contract string is not kept anywhere in the negotiated state.
    assert not hasattr(state, "contract")
