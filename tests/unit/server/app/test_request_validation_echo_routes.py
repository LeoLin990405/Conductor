"""A validator's sentence repeats nothing the request sent, whatever the form.

Round after round a validator turned up that quotes caller text in a way the
exact-value cut cannot see: a ``repr()`` with escapes, the host cut out of a URL
(lower-cased by the parser), the part a URL parser choked on. The handler cuts
every run of the request's own text from a sentence, and the validators are
value-free as well; these drive the real app so both are held.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.app import setup
from src.server.dependencies.usage_limits import enforce_chat_limit
from src.server.utils.error_sanitization import _cut_echo, caller_runs

TOKEN = "ghp_" + "a" * 36
PASTED = "Basic dXNlcjpwYXNz"


@pytest_asyncio.fixture
async def client():
    setup.app.dependency_overrides[enforce_chat_limit] = lambda: None
    try:
        async with AsyncClient(
            transport=ASGITransport(app=setup.app), base_url="http://test"
        ) as http:
            yield http
    finally:
        setup.app.dependency_overrides.pop(enforce_chat_limit, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [PASTED + "\n", PASTED, "\t" + TOKEN, TOKEN.upper()])
async def test_a_chat_role_is_not_repeated_even_as_a_repr(client, role):
    response = await client.post(
        "/api/v1/threads/66666666-6666-4666-8666-666666666666/messages",
        headers={"X-User-Id": "u1"},
        json={"messages": [{"role": role, "content": "x"}]},
    )
    assert response.status_code == 422
    for piece in (PASTED, TOKEN, TOKEN.upper()):
        assert piece not in response.text
    assert "dXNlcjpwYXNz" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        f"https://{TOKEN}.local/mcp",
        f"https://{TOKEN.upper()}.local/mcp",
        f"https://[{TOKEN}]/mcp",
        f"https://{TOKEN}.internal/mcp",
        f"https://{TOKEN}@example.com/mcp",
        f"https://example.com:{TOKEN}/mcp",
    ],
)
async def test_a_url_the_parser_refuses_is_not_repeated(client, url):
    response = await client.post(
        "/api/v1/mcp/servers/probe",
        headers={"X-User-Id": "u1"},
        json={"url": url},
    )
    assert response.status_code == 422
    assert TOKEN not in response.text
    assert TOKEN.upper() not in response.text


@pytest.mark.parametrize(
    "sentence",
    [
        "role 'Basic dXNlcjpwYXNz\\n' is not allowed",  # repr escape
        "host ghp_" "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa is not allowed",  # folded copy
        "got GHP_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    ],
)
def test_a_run_of_the_requests_own_text_is_cut_from_a_sentence(sentence):
    caller = caller_runs({"role": "Basic dXNlcjpwYXNz\n", "u": "GHP_" + "A" * 36})
    cut = _cut_echo(sentence, caller)
    assert "dXNlcjpw" not in cut
    assert "aaaaaaaa" not in cut.lower()
    assert "[REDACTED]" in cut


def test_the_sentences_own_words_survive():
    caller = caller_runs({"note": "this is some ordinary caller text"})
    sentence = "Value error, url must use https://"
    assert _cut_echo(sentence, caller) == sentence


def test_the_scan_is_bounded_whatever_the_request_holds():
    import time

    huge = {f"k{i}": "x" * 10_000 for i in range(5_000)}
    started = time.perf_counter()
    caller = caller_runs(huge, b"y" * 1_000_000, ["z" * 100] * 100_000)
    _cut_echo("Value error, " + "q" * 3_000, caller)
    assert time.perf_counter() - started < 1.0
    assert len(caller) <= 16 * 1024


@pytest.mark.asyncio
async def test_a_uuid_the_parser_refuses_is_not_repeated_normalized(client):
    """uuid.UUID quotes the hyphen-stripped text it choked on, which an exact
    cut of the caller's value never matches; the run index alone also misses it
    when the hyphens fill the 2 KiB per-string window, so the refusal is
    value-free."""
    key = "-" * 2048 + "Zq8mW3xLp0vTn5Rk" * 2
    response = await client.post(
        "/api/v1/threads/66666666-6666-4666-8666-666666666666/messages",
        headers={"X-User-Id": "u1"},
        json={"messages": [{"role": "user", "content": "x"}], "request_key": key},
    )
    assert response.status_code == 422
    assert "Zq8mW3xLp0vTn5Rk" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["headers", "env"])
async def test_the_mcp_save_path_does_not_name_a_key_it_refused(client, field):
    """These routes validate by hand and answer through ``validation_error_text``,
    not through the app-wide handler."""
    body = {"name": "demo", field: {"Zq8mW3xLp0vTn5Rk": {}}}
    if field == "headers":
        body.update(transport="http", url="https://example.com/mcp")
    else:
        body.update(transport="stdio", command="npx")
    response = await client.post(
        "/api/v1/mcp/servers", headers={"X-User-Id": "u1"}, json=body
    )
    assert response.status_code == 422
    assert "Zq8mW3xLp0vTn5Rk" not in response.text


@pytest.mark.asyncio
async def test_many_errors_cost_little_and_are_capped(client):
    import time

    roles = [{"role": f"bad-role-{i:05d}-{TOKEN}", "content": "x"} for i in range(1_000)]
    pad = {f"pad{i}": "p" * 2048 for i in range(8)}
    started = time.perf_counter()
    response = await client.post(
        "/api/v1/threads/66666666-6666-4666-8666-666666666666/messages",
        headers={"X-User-Id": "u1"},
        json={"messages": roles, "additional_context": pad},
    )
    elapsed = time.perf_counter() - started
    assert response.status_code == 422
    assert TOKEN not in response.text
    assert len(response.json()["detail"]) <= 100
    assert elapsed < 0.5


def test_the_traversal_queues_no_more_than_its_node_budget():
    import tracemalloc

    big = [object()] * 1_000_000
    tracemalloc.start()
    caller_runs({"k": big})
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 2 * 1024 * 1024


class _Route:
    workspace_id = "ws-1"

    async def bind(self, *_args):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("native_provider", "ghp_" + "a" * 36)])
async def test_the_thread_create_native_refusals_do_not_name_the_value(
    client, field, value
):
    from unittest.mock import patch

    async def enabled(*_args, **_kwargs):
        return True

    async def no_workspace(*_args, **_kwargs):
        return None

    with (
        patch("src.server.app.threads.create.HOST_MODE", "oss"),
        patch("src.server.app.threads.create.requested_workspace", no_workspace),
        patch("src.server.app.threads.create.turn_route", lambda *_a, **_k: _Route()),
        patch("src.server.services.features.user_feature_enabled", enabled),
    ):
        response = await client.post(
            "/api/v1/threads",
            headers={"X-User-Id": "u1"},
            json={"first_query": "x", field: value},
        )
    assert response.status_code == 422
    assert value not in response.text
    assert "Zq8mW3xLp0vTn5Rk" not in response.text


def test_the_profile_vocabulary_does_not_repeat_the_rejected_value():
    from src.server.models.profile_vocabulary import check_profile

    secret = "Zq8mW3xLp0vTn5Rk"
    _profile, problems = check_profile(
        {"agent_preference": {"research_depth": secret, "sectors": [secret]}}, None
    )
    assert problems
    assert secret not in " ".join(message for _path, message in problems)


def test_a_native_cwd_the_server_refuses_is_not_repeated():
    from src.server.services.native_runtime.cwd import resolve_native_cwd

    with pytest.raises(ValueError) as caught:
        resolve_native_cwd("/nonexistent/Zq8mW3xLp0vTn5Rk")
    assert "Zq8mW3xLp0vTn5Rk" not in str(caught.value)


@pytest.mark.asyncio
async def test_every_value_of_a_repeated_query_key_is_indexed():
    """dict(query_params) keeps only the last value of a repeated key; the first
    one a validator quotes must still be cut."""
    from typing import Annotated

    from fastapi import FastAPI, Query
    from fastapi.exceptions import RequestValidationError
    from pydantic import AfterValidator

    def quote_first(values: list[str]) -> list[str]:
        raise ValueError(f"first was {values[0].lower()}")

    probe = FastAPI()
    probe.add_exception_handler(RequestValidationError, setup._request_validation_handler)

    async def handler(q):
        return q

    # This module uses postponed annotations; a local name would not resolve.
    handler.__annotations__ = {
        "q": Annotated[list[str], Query(), AfterValidator(quote_first)]
    }
    probe.get("/probe")(handler)

    async with AsyncClient(transport=ASGITransport(app=probe), base_url="http://t") as http:
        response = await http.get("/probe", params=[("q", "Zq8mW3xLp0vTn5Rk"), ("q", "last")])
    assert response.status_code == 422
    assert "zq8mw3xlp0vtn5rk" not in response.text.lower()
