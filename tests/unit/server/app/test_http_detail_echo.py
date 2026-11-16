"""A sentence a route writes by hand does not read the caller's free text back.

``test_request_validation_echo_routes.py`` holds the 422 handler and the
validators behind it. These are the ``HTTPException`` and ``ValueError``
sentences the routes write themselves, one family at a time, driven through the
real app with the database stubbed at its edge. Each sends a recognisable
secret where a form field takes free text and asserts the answer holds no run
of 8 characters of it, in either case (``_assert_no_piece``, the same measure
the 422 handler cuts a sentence by), so the whole value, its first eight
characters and a stretch from the middle all fail. The sentences that name a value on purpose are
either about something the server parsed or matched (a UUID, a ``Literal``, a
number) or listed in ``ALLOWED`` beside the structural test at the end, each
with its reason; the test at the end fails when a new one is not.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import pathlib
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.app import setup

SECRET = "Zq8mW3xLp0vTn5Rk"
HEADERS = {"X-User-Id": "u1"}


@pytest_asyncio.fixture
async def client():
    from src.server.utils.api import get_current_user_id

    setup.app.dependency_overrides[get_current_user_id] = lambda: "u1"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=setup.app), base_url="http://test"
        ) as http:
            yield http
    finally:
        setup.app.dependency_overrides.pop(get_current_user_id, None)


# Any run of this many characters of the sentinel counts as it read back, as
# the 422 handler's own filter does: a sentence that quotes half the value, or
# its first eight characters, has still told the caller what they sent.
RUN = 8
PIECES = {SECRET.lower()[i : i + RUN] for i in range(len(SECRET) - RUN + 1)}


def _assert_no_piece(text: str) -> None:
    """``text`` holds no run of ``RUN`` characters of the sentinel, in any case."""
    folded = text.lower()
    read_back = sorted(piece for piece in PIECES if piece in folded)
    assert not read_back, f"read back {read_back} in {text!r}"


def _assert_value_free(response, *, status: int) -> None:
    assert response.status_code == status, response.text
    _assert_no_piece(response.text)


@pytest.mark.parametrize(
    "text",
    [
        SECRET,
        SECRET.upper(),
        SECRET[:RUN],  # the first eight characters
        SECRET[-RUN:],  # the last eight
        f"[{SECRET[4:4 + RUN]}]",  # a run from the middle
        f"x{SECRET[3:3 + RUN].lower()}y",
    ],
)
def test_the_helper_rejects_the_secret_and_any_run_of_it(text):
    with pytest.raises(AssertionError):
        _assert_no_piece(text)


@pytest.mark.parametrize(
    "text", [SECRET[: RUN - 1], SECRET[RUN - 1 : 2 * RUN - 2], f"{SECRET[:4]} {SECRET[4:8]}", ""]
)
def test_the_helper_accepts_what_is_shorter_than_a_run(text):
    _assert_no_piece(text)


# ---------------------------------------------------------------------------
# Preferences: custom models, custom providers, per-model profiles, avatar
# ---------------------------------------------------------------------------

USERS = "src.server.app.users"


def _preferences_put(client, body):
    return client.put("/api/v1/users/me/preferences", headers=HEADERS, json=body)


@pytest.fixture
def stubbed_preferences():
    conn = SimpleNamespace(transaction=nullcontext)
    with (
        patch(f"{USERS}.db_get_user", new_callable=AsyncMock, return_value={"user_id": "u1"}),
        patch(
            f"{USERS}.db_get_user_preferences",
            new_callable=AsyncMock,
            return_value={"model_preference": {}},
        ),
        patch(f"{USERS}.get_db_connection", lambda: nullcontext(conn)),
    ):
        yield


def _model(**fields):
    return {"name": "mine", "model_id": "m-1", "provider": "openai", **fields}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_preference",
    [
        {"custom_models": [_model(name=f"{SECRET} with a space")]},
        {"custom_models": [_model(name=SECRET), _model(name=SECRET)]},
        {"custom_models": [_model(provider=SECRET)]},
        {"custom_models": [_model(input_modalities=[SECRET])]},
        {"custom_models": [_model(reasoning={"efforts": ["low"], "write": SECRET})]},
        {
            "custom_models": [
                _model(
                    reasoning={
                        "efforts": ["none", "high"],
                        "on": {SECRET: "enabled"},
                        "off": {"parameters.thinking.type": "disabled"},
                    }
                )
            ]
        },
        {
            "custom_models": [
                _model(reasoning={"efforts": [SECRET], "write": "parameters.reasoning.effort"})
            ]
        },
        {"custom_providers": [{"name": f"{SECRET}!", "parent_provider": "openai"}]},
        {
            "custom_providers": [
                {"name": SECRET, "parent_provider": "openai"},
                {"name": SECRET, "parent_provider": "openai"},
            ]
        },
        {"custom_providers": [{"name": "mine", "parent_provider": SECRET}]},
        {"profiles": {SECRET: "not an object"}},
        {"profiles": {SECRET: {SECRET + "_setting": 1}}},
        {"profiles": {SECRET: {"fast_mode": "yes"}}},
        {"profiles": {SECRET: {"reasoning_effort": "low"}}},
    ],
)
async def test_a_preferences_refusal_does_not_read_the_value_back(
    client, stubbed_preferences, model_preference
):
    response = await _preferences_put(client, {"model_preference": model_preference})
    _assert_value_free(response, status=400)


@pytest.mark.asyncio
async def test_a_profile_is_named_when_the_server_holds_the_model(
    client, stubbed_preferences
):
    """The name stays where it is the server's own: a manifest model."""
    from src.llms.llm import LLM

    known = next(iter(LLM.get_model_config().llm_config))
    response = await _preferences_put(
        client, {"model_preference": {"profiles": {known: "not an object"}}}
    )
    assert response.status_code == 400
    assert response.json()["detail"] == f"profiles[{known}] must be an object"


@pytest.mark.asyncio
async def test_an_unnamed_profile_is_named_by_its_place(client, stubbed_preferences):
    response = await _preferences_put(
        client,
        {"model_preference": {"profiles": {"first-" + SECRET: None, SECRET: "x"}}},
    )
    _assert_value_free(response, status=400)
    assert response.json()["detail"] == "profiles entry 2 must be an object"


@pytest.mark.asyncio
async def test_a_profile_for_a_model_only_this_request_adds_is_not_named(
    client, stubbed_preferences
):
    """The model is the request's own text until the account holds it, however
    well it is shaped, so a profile on it is named by its place."""
    response = await _preferences_put(
        client,
        {
            "model_preference": {
                "custom_models": [_model(name=SECRET)],
                "profiles": {SECRET: {"fast_mode": "yes"}},
            }
        },
    )
    _assert_value_free(response, status=400)
    assert response.json()["detail"].startswith("profiles entry 1")


@pytest.mark.asyncio
async def test_a_profile_is_named_when_the_account_already_holds_the_model(
    client, stubbed_preferences
):
    held = {"model_preference": {"custom_models": [_model(name="mine")]}}
    with patch(f"{USERS}.db_get_user_preferences", AsyncMock(return_value=held)):
        response = await _preferences_put(
            client, {"model_preference": {"profiles": {"mine": {"fast_mode": "yes"}}}}
        )
    assert response.status_code == 400
    assert response.json()["detail"].startswith("profiles[mine]")


@pytest.mark.asyncio
async def test_an_avatar_of_a_made_up_type_is_not_read_back(client):
    with patch(f"{USERS}.db_get_user", new_callable=AsyncMock, return_value={"user_id": "u1"}):
        response = await client.post(
            "/api/v1/users/me/avatar",
            headers=HEADERS,
            files={"file": ("a.png", b"x", f"{SECRET}/png")},
        )
    _assert_value_free(response, status=400)
    assert "image/png" in response.json()["detail"]


def test_a_reasoning_surface_keeps_its_text_for_the_author_and_not_for_the_response():
    from src.llms.reasoning import ReasoningSurfaceError, validate_surface

    for surface in (
        {"write": SECRET},
        {"on": {SECRET: "x"}},
        {"off": {SECRET: "x"}},
        {"efforts": [SECRET], "write": "parameters.reasoning.effort"},
    ):
        with pytest.raises(ReasoningSurfaceError) as caught:
            validate_surface("m", surface)
        assert SECRET in str(caught.value)  # the manifest suite and the spec build keep it
        _assert_no_piece(caught.value.public)
        assert caught.value.public.startswith("m: reasoning.")


# ---------------------------------------------------------------------------
# BYOK: the provider a path or body names
# ---------------------------------------------------------------------------

API_KEYS = "src.server.app.api_keys"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path, body, status",
    [
        ("DELETE", f"/api/v1/users/me/api-keys/{SECRET}", None, 400),
        ("POST", "/api/v1/keys/test", {"provider": SECRET, "api_key": "k"}, 400),
        ("GET", f"/api/v1/providers/{SECRET}/models", None, 400),
        ("GET", f"/api/v1/providers/{SECRET}/visible-models", None, 404),
    ],
)
async def test_a_provider_the_server_does_not_know_is_not_read_back(
    client, method, path, body, status
):
    with patch(f"{API_KEYS}._get_custom_providers", new_callable=AsyncMock, return_value=[]):
        response = await client.request(method, path, headers=HEADERS, json=body)
    _assert_value_free(response, status=status)


# Each case is a key the pinned HTTP client (httpx 0.28.1) refuses to send, with
# the refusal it raises and whether that refusal's own text quotes the key.
#
# OpenAI and Anthropic carry the key in a header. The client's HTTP/1.1 layer
# refuses a header value that ends in whitespace or a line break
# (LocalProtocolError: "Illegal header value b'Bearer <key>...'"), so its text
# is the key read back: these are the cases that detect an echo.
#
# Gemini carries the key in the query string. A line break there is refused by
# the URL parser (InvalidURL), whose text names a position and not the key, so
# these two cases prove the refusal is reached and that the answer says the kind
# of failure, but would pass under a route that echoed. A Gemini key with a
# trailing space is not a case at all: the client sends it as ``%20``, the
# request is well formed, and what fails is the listener hanging up on it
# (RemoteProtocolError), a different path from the one this test is about.
_REFUSED_KEYS = [
    pytest.param("openai", "\n", "LocalProtocolError", True, id="openai-newline"),
    pytest.param("openai", " ", "LocalProtocolError", True, id="openai-space"),
    pytest.param("openai", "\r\n", "LocalProtocolError", True, id="openai-crlf"),
    pytest.param("anthropic", "\n", "LocalProtocolError", True, id="anthropic-newline"),
    pytest.param("anthropic", " ", "LocalProtocolError", True, id="anthropic-space"),
    pytest.param("anthropic", "\r\n", "LocalProtocolError", True, id="anthropic-crlf"),
    pytest.param("gemini", "\n", "InvalidURL", False, id="gemini-newline"),
    pytest.param("gemini", "\r\n", "InvalidURL", False, id="gemini-crlf"),
]


@contextlib.asynccontextmanager
async def _listener_that_hangs_up():
    """A loopback server that closes every connection and records the first
    line each client sent, which is empty when the client sent nothing."""
    first_lines: list[bytes] = []

    async def _accept(reader, writer):
        first_lines.append((await reader.read(4096)).split(b"\r\n")[0])
        writer.close()

    server = await asyncio.start_server(_accept, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], first_lines
    finally:
        server.close()
        await server.wait_closed()


async def _key_test(client, provider, key, base_url=None):
    with patch(f"{API_KEYS}._get_custom_providers", new_callable=AsyncMock, return_value=[]):
        return await client.post(
            "/api/v1/keys/test",
            headers=HEADERS,
            json={"provider": provider, "api_key": key, "base_url": base_url},
        )


@pytest.mark.enable_socket  # a listener on loopback: the client's own refusal is the subject
@pytest.mark.asyncio
@pytest.mark.parametrize("provider, trailer, refusal, quotes_the_key", _REFUSED_KEYS)
async def test_a_key_the_http_client_refuses_to_send_is_not_read_back(
    client, provider, trailer, refusal, quotes_the_key
):
    """The 200 body says that the test could not be completed, and which kind
    of failure, never the key. The first half pins the premise: the client
    itself refused (and put nothing on the wire), with the text claimed."""
    from src.server.app import api_keys

    async with _listener_that_hangs_up() as (port, first_lines):
        base_url = f"http://127.0.0.1:{port}"
        test_fn = api_keys._SDK_TEST_DISPATCH[provider][0]
        with pytest.raises(Exception) as refused:
            await test_fn(SECRET + trailer, "m", base_url, 5.0)
        assert type(refused.value).__name__ == refusal
        assert (SECRET in str(refused.value)) is quotes_the_key
        assert all(line == b"" for line in first_lines), first_lines  # no request line

        response = await _key_test(client, provider, SECRET + trailer, base_url)

    _assert_value_free(response, status=200)
    body = response.json()
    assert body["success"] is False
    assert f"could not be completed ({refusal})" in body["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider, raised",
    [
        pytest.param(
            "openai",
            httpx.LocalProtocolError(f"Illegal header value b'Bearer {SECRET}\n'"),
            id="a-header-the-client-refused",
        ),
        pytest.param(
            "gemini",
            # Gemini's key rides in the query. This is the shape of text that
            # carries it without a scheme, which a scrub of URLs does not match.
            httpx.LocalProtocolError(
                f"Illegal request target b'/v1beta/models?key={SECRET}&pageSize=5'"
            ),
            id="a-request-target-the-client-refused",
        ),
        pytest.param("anthropic", ValueError(f"unusable key {SECRET}"), id="a-value-error"),
        pytest.param("openai", RuntimeError(f"key={SECRET}"), id="a-runtime-error"),
    ],
)
async def test_whatever_a_failed_key_test_says_about_the_key_is_not_returned(
    client, provider, raised
):
    """Forged client text, so every case carries the key whichever client
    version is installed: this is the route's catch-all branch on its own, the
    one that detects a revert to returning ``str(e)``."""
    from src.server.app import api_keys

    dispatch = {provider: (AsyncMock(side_effect=raised), "m")}
    with patch.dict(api_keys._SDK_TEST_DISPATCH, dispatch):
        response = await _key_test(client, provider, SECRET)

    _assert_value_free(response, status=200)
    assert f"could not be completed ({type(raised).__name__})" in response.json()["error"]


@pytest.mark.asyncio
async def test_a_key_test_still_says_what_the_provider_answered_and_what_it_found(client):
    """What the vendor says about a refused key, and the server's own sentence
    for a host with no compatible endpoint, are kept: neither is the caller's."""
    from src.server.app import api_keys

    refused = httpx.HTTPStatusError(
        "401",
        request=httpx.Request("GET", "https://api.example.test/v1/models"),
        response=httpx.Response(401, json={"error": {"message": "Incorrect API key provided."}}),
    )
    with (
        patch(f"{API_KEYS}._get_custom_providers", new_callable=AsyncMock, return_value=[]),
        patch.dict(api_keys._SDK_TEST_DISPATCH, {"openai": (AsyncMock(side_effect=refused), "m")}),
    ):
        response = await client.post(
            "/api/v1/keys/test", headers=HEADERS, json={"provider": "openai", "api_key": "k"}
        )
    assert response.json()["error"] == "Incorrect API key provided."

    absent = api_keys._NoCompatibleEndpoint("Server reachable but could not verify key")
    with (
        patch(f"{API_KEYS}._get_custom_providers", new_callable=AsyncMock, return_value=[]),
        patch.dict(api_keys._SDK_TEST_DISPATCH, {"openai": (AsyncMock(side_effect=absent), "m")}),
    ):
        response = await client.post(
            "/api/v1/keys/test", headers=HEADERS, json={"provider": "openai", "api_key": "k"}
        )
    assert response.json()["error"] == "Server reachable but could not verify key"


# ---------------------------------------------------------------------------
# Autonomy: a Source's config and a schedule's timezone
# ---------------------------------------------------------------------------

AUTONOMY = "src.server.app.autonomy"


@pytest.fixture
def autonomy_feature():
    with (
        patch(f"{AUTONOMY}.features.user_feature_enabled", new=AsyncMock(return_value=True)),
        patch(f"{AUTONOMY}._workspace", new=AsyncMock(return_value={"workspace_id": "w1"})),
    ):
        yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind, config",
    [
        ("feishu", {"chat_id": "oc_1", "workspace_id": "w1", "poll_seconds": SECRET}),
        ("feishu", {"chat_id": "oc_1", "workspace_id": "w1", "admission": {SECRET: 1}}),
        ("feishu", {"chat_id": "oc_1", "workspace_id": "w1", "worker_model": SECRET * 20}),
        (
            "email",
            {
                "from_address": "a@example.com",
                "to_address": "b@example.com",
                "secret_name": "S",
                "workspace_id": "w1",
                "smtp_host": "smtp.example.com",
                "smtp_port": SECRET,
            },
        ),
        (
            "slack",
            {"channel_name": "c", "webhook_secret_name": SECRET * 20, "workspace_id": "w1"},
        ),
        (
            "github",
            {
                "repository": "acme/widgets",
                "secret_name": "GITHUB_TOKEN",
                "workspace_id": "w1",
                "allowed_logins": ["alice"],
                "credential_allowlist": {SECRET: ["x"]},
            },
        ),
    ],
)
async def test_a_source_config_refusal_does_not_read_the_value_back(
    client, autonomy_feature, kind, config
):
    response = await client.post(
        "/api/v1/autonomy/sources", headers=HEADERS, json={"kind": kind, "config": config}
    )
    _assert_value_free(response, status=400)
    # It still says which field and why: a form marks the field from this.
    assert response.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize("zone", [SECRET, f"/{SECRET}", f"../{SECRET}", f"{SECRET}\x00"])
async def test_a_timezone_the_server_does_not_know_is_not_read_back(
    client, autonomy_feature, zone
):
    response = await client.post(
        "/api/v1/autonomy/schedules",
        headers=HEADERS,
        json={
            "prompt": "p",
            "workspace_id": "w1",
            "cron_expression": "0 9 * * *",
            "timezone": zone,
        },
    )
    _assert_value_free(response, status=400)
    assert response.json()["detail"] == "unknown timezone"


# ---------------------------------------------------------------------------
# Lookups by a name or id the caller put in the path, query or body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_missing_dispatch_and_line_are_not_named_back(client):
    from unittest.mock import patch as _patch

    from src.server.services.dispatch import DispatchService
    from src.tools.auto_dispatch.types import Dispatch

    empty = Dispatch(id="d1", goal="g", user_id="u1", created_at="2026-01-01T00:00:00+00:00")
    with _patch.object(DispatchService, "get", AsyncMock(return_value=None)):
        response = await client.get(f"/api/v1/dispatch/{SECRET}", headers=HEADERS)
    _assert_value_free(response, status=404)

    with (
        _patch.object(DispatchService, "get", AsyncMock(return_value=empty)),
        _patch.object(DispatchService, "_require", AsyncMock(return_value=empty)),
    ):
        response = await client.post(
            f"/api/v1/dispatch/d1/lines/{SECRET}/reassign",
            headers=HEADERS,
            json={"model": "m"},
        )
    _assert_value_free(response, status=409)


@pytest.mark.asyncio
async def test_a_feature_the_catalog_does_not_hold_is_not_named_back(client):
    response = await client.put(
        f"/api/v1/features/{SECRET}", headers=HEADERS, json={"enabled": True}
    )
    _assert_value_free(response, status=404)


@pytest.mark.asyncio
async def test_a_native_provider_without_a_login_is_not_named_back(client):
    response = await client.post(
        f"/api/v1/native-agents/{SECRET}/login", headers=HEADERS
    )
    _assert_value_free(response, status=400)


@pytest.mark.asyncio
async def test_an_agent_log_that_does_not_exist_is_not_named_back(tmp_path):
    from src.server.services.agent_scanner import (
        AgentLogNotFound,
        ScanSettings,
        read_agent_logs,
    )

    settings = ScanSettings(
        root=tmp_path,
        logs_dir=tmp_path / "logs",
        prompts_dir=tmp_path / "prompts",
        stall_secs=300,
        base_ref="main",
        log_tail_bytes=4096,
        max_worktrees=4,
        command_timeout=1.0,
    )
    # One shape the id check refuses and one it lets through to the lookup.
    for agent_id in (f"../{SECRET}", SECRET):
        with pytest.raises(AgentLogNotFound) as caught:
            await read_agent_logs(agent_id, settings)
        _assert_no_piece(str(caught.value))


@pytest.mark.asyncio
async def test_a_package_the_install_refuses_is_named_by_its_place(client):
    response = await client.post(
        "/api/v1/workspaces/66666666-6666-4666-8666-666666666666/sandbox/packages",
        headers=HEADERS,
        json={"packages": ["requests", f"!{SECRET}"]},
    )
    _assert_value_free(response, status=400)
    assert "package 2" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_fleet_agent_the_config_does_not_hold_is_not_named_back(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from src.server.app.fleet_skills import router
    from src.server.utils.api import get_current_user_id

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CONDUCTOR_SKILLS_REPO", raising=False)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        response = await http.get("/api/v1/fleet-skills", params={"agent": SECRET})
    _assert_value_free(response, status=404)
    assert "known:" in response.json()["detail"]


@pytest.mark.asyncio
async def test_a_subagent_the_roster_does_not_hold_is_not_named_back(monkeypatch):
    from fastapi import FastAPI

    from src.server.app.subagents import router
    from src.server.utils.api import get_current_user_id
    from tests.unit.server.services.subagents._fakes import install

    world = install(monkeypatch)
    world.custom("alpha")
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        gone = await http.delete(f"/api/v1/subagents/{SECRET}", params={"version": "0" * 64})
        copied = await http.put(
            "/api/v1/subagents/new-one",
            json={"version": None, "start_from": SECRET, "fields": {}},
        )
    _assert_value_free(gone, status=404)
    _assert_value_free(copied, status=422)
    assert any(e["field"] == "start_from" for e in copied.json()["detail"]["errors"])


@pytest.mark.asyncio
async def test_a_subagent_setting_that_does_not_exist_is_not_named_back(monkeypatch):
    """The key of ``fields`` is the caller's: an unknown one is a field named
    ``<key>`` in the answer, as the 422 handler spells any name the route did
    not define, and a setting a subagent has is still named."""
    from fastapi import FastAPI

    from src.server.app.subagents import router
    from src.server.utils.api import get_current_user_id
    from tests.unit.server.services.subagents._fakes import install

    install(monkeypatch)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user_id] = lambda: "u1"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as http:
        response = await http.put(
            "/api/v1/subagents/new-one",
            json={"version": None, "fields": {SECRET: "x", "enabled": "maybe"}},
        )
    _assert_value_free(response, status=422)
    fields = {e["field"] for e in response.json()["detail"]["errors"]}
    assert {"<key>", "enabled"} <= fields


# ---------------------------------------------------------------------------
# MCP tool bindings and plugin installs
# ---------------------------------------------------------------------------


def test_a_tool_binding_refusal_names_the_entry_by_its_place():
    from src.server.services.tool_binding import validate_overrides

    cases = (
        {SECRET: "direct"},  # not relayable: only the sandbox wrapper
        {"first": "ptc", SECRET: "direct"},
        {SECRET: "ptc", SECRET.upper(): "ptc"},  # one tool in two spellings
    )
    for overrides in cases:
        reason = validate_overrides(None, overrides, relayable=False)
        assert reason, overrides
        _assert_no_piece(reason)
    assert validate_overrides(None, {"first": "ptc", SECRET: "direct"}, relayable=False) == (
        "tool_binding_set entry 2 can only be bound as a sandbox wrapper"
    )


def test_supplied_secret_names_are_counted_not_named():
    from src.server.services.plugins.errors import PluginRejected
    from src.server.services.plugins.supplied_secrets import validate_supplied

    extension = SimpleNamespace(secrets=[SimpleNamespace(name="ASKED")])
    with pytest.raises(PluginRejected) as caught:
        validate_supplied(
            extension, {"ASKED": "v", SECRET: "x", SECRET + "2": "y"}, entries=[]
        )
    _assert_no_piece(str(caught.value))
    assert str(caught.value).startswith("2 of the supplied secrets")


@pytest.mark.asyncio
async def test_a_plugin_update_does_not_read_the_packages_name_back():
    from fastapi import HTTPException

    from src.server.app.plugins import _update_response

    with pytest.raises(HTTPException) as caught:
        await _update_response(
            "u1",
            {"name": "installed-name"},
            SimpleNamespace(name=SECRET),
            source_ref=None,
        )
    assert caught.value.status_code == 409
    _assert_no_piece(caught.value.detail)
    assert "installed-name" in caught.value.detail  # the user's own plugin


@pytest.mark.asyncio
@pytest.mark.parametrize("subdir", [f"../{SECRET}", f"/{SECRET}", f"{SECRET}\\x"])
async def test_a_plugin_subdirectory_the_server_refuses_is_not_read_back(client, subdir):
    response = await client.post(
        "/api/v1/plugins",
        headers=HEADERS,
        json={"source_url": "https://github.com/acme/widgets", "subdir": subdir},
    )
    _assert_value_free(response, status=409)


def _resolving_to(monkeypatch, address: str | None) -> None:
    """The one boundary the egress guard crosses before it can refuse a host."""
    import socket

    def getaddrinfo(host, port, *args, **kwargs):
        if address is None:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_url, address",
    [
        (f"https://example.com:{SECRET}/a.zip", "93.184.216.34"),  # a port that is text
        (f"https://example.com:99{SECRET}/a.zip", "93.184.216.34"),
        (f"https://{SECRET}.invalid/a.zip", None),  # does not resolve
        (f"https://{SECRET}.example.com/a.zip", "127.0.0.1"),  # resolves inward
    ],
)
async def test_a_plugin_source_the_egress_guard_refuses_is_not_read_back(
    client, monkeypatch, source_url, address
):
    _resolving_to(monkeypatch, address)
    response = await client.post(
        "/api/v1/plugins", headers=HEADERS, json={"source_url": source_url}
    )
    _assert_value_free(response, status=422)
    # It still says why, so the form can tell a typo from a blocked address.
    assert "refused" in response.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_a_host_the_transport_refuses_is_not_called_a_bad_answer(client, monkeypatch):
    """The same route, a refusal of another kind: the transport pins the host
    under its punycode name and refuses the request, before anything is sent,
    because the request reports it in Unicode. The sentence says the host name
    did not match, not that an answer was compressed or too large."""
    _resolving_to(monkeypatch, "93.184.216.34")
    response = await client.post(
        "/api/v1/plugins",
        headers=HEADERS,
        json={"source_url": "https://xn--bcher-kva.example/pkg.zip"},
    )
    assert response.status_code == 422, response.text
    message = response.json()["detail"]["message"]
    assert "host name" in message
    assert "compressed" not in message
    assert "larger than" not in message


@pytest.mark.asyncio
async def test_a_plugin_source_that_fails_in_transit_is_not_read_back(monkeypatch):
    """What the transport says about a host (a certificate for another name,
    a body past the cap) quotes the host the caller typed."""
    from contextlib import asynccontextmanager
    from unittest.mock import MagicMock

    import httpx2

    from src.server.services.mcp_oauth.http import HopRefusal, OAuthHopBlocked
    from src.server.services.plugins import fetch
    from src.server.services.plugins.errors import PluginFatal

    async def _pin(url):
        return url

    monkeypatch.setattr(fetch, "pin_public_url", _pin)
    failures = (
        httpx2.ConnectError(f"certificate is not valid for '{SECRET}'"),
        OSError(f"[Errno 8] {SECRET}: nodename nor servname provided"),
        # Every rule the transport can name, each with the host in its text.
        *(OAuthHopBlocked(f"{SECRET} refused: {rule}", refusal=rule) for rule in HopRefusal),
    )
    for failure in failures:

        @asynccontextmanager
        async def _client(target, max_bytes=None, _failure=failure):
            yield MagicMock(get=AsyncMock(side_effect=_failure))

        monkeypatch.setattr(fetch, "pinned_stream_client", _client)
        with pytest.raises(PluginFatal) as caught:
            await fetch.fetch_plugin_source("https://vendor.test/pkg.zip")
        _assert_no_piece(str(caught.value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url, https_only, answer",
    [
        (f"https://example.com:{SECRET}/", True, "93.184.216.34"),  # port that is text
        (f"https://example.com:99{SECRET}/", True, "93.184.216.34"),  # port out of range
        (f"https://[{SECRET}/", True, "93.184.216.34"),  # not parseable
        (f"http://{SECRET}.example.com/", True, "93.184.216.34"),  # not https
        (f"ftp://{SECRET}.example.com/", False, "93.184.216.34"),  # another scheme
        (f"https://{SECRET}@example.com/", True, "93.184.216.34"),  # userinfo
        (f"https:///{SECRET}", True, "93.184.216.34"),  # no host
        (f"https://{SECRET}.example.com/", True, None),  # does not resolve
        (f"https://{SECRET}.example.com/", True, "127.0.0.1"),  # resolves inward
        (f"https://{SECRET}.example.com/", True, ""),  # resolves to nothing
    ],
)
async def test_every_egress_refusal_has_a_reason_without_the_url(
    monkeypatch, url, https_only, answer
):
    """``str(e)`` keeps the host for the log; ``reason`` is what a response says."""
    import socket

    from src.server.utils.egress_guard import EgressBlockedError, pin_public_url

    if answer == "":
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [])
    else:
        _resolving_to(monkeypatch, answer)
    with pytest.raises(EgressBlockedError) as caught:
        await pin_public_url(url, require_https=https_only)
    assert caught.value.reason
    _assert_no_piece(caught.value.reason)


def test_a_refusal_raised_without_a_reason_says_nothing_about_the_url():
    from src.server.utils.egress_guard import EgressBlockedError

    refusal = EgressBlockedError(f"egress to {SECRET!r} is blocked")
    assert SECRET in str(refusal)  # the log keeps it
    _assert_no_piece(refusal.reason)


# ---------------------------------------------------------------------------
# The tripwire: a new sentence in src/server/app that formats the request
# ---------------------------------------------------------------------------

APP_DIR = pathlib.Path(__file__).resolve().parents[4] / "src" / "server" / "app"

# An annotation that makes a parameter the server's own vocabulary: an id the
# server parsed, a closed set, a number or a flag.
_CLOSED = {"UUID", "Literal", "CurrentUserId", "OptionalUserId", "bool", "int", "float"}

#: (file, function, formatted expression) -> why the sentence may name it.
ALLOWED = {
    ("computers.py", "_computer_action_errors", "action"): (
        "the verb the server's own route passes ('create', 'start', ...), never request text"
    ),
    ("workspaces.py", "_workspace_action_errors", "action"): (
        "the verb the server's own route passes ('start', 'stop', ...), never request text"
    ),
    ("features.py", "put_feature_override", "key"): (
        "FeatureNotOverridableError is raised only for a key the catalog resolved to a "
        "registered feature, so this is the server's own feature key"
    ),
    ("plugins.py", "_update_response", "plugin['name']"): (
        "the caller's own installed plugin, read from its row; the package's name is not named"
    ),
}


def _closed(annotation: ast.expr | None) -> bool:
    if annotation is None:
        return False
    if isinstance(annotation, ast.Constant):  # None
        return annotation.value is None
    if isinstance(annotation, ast.Name):
        return annotation.id in _CLOSED
    if isinstance(annotation, ast.Attribute):
        return annotation.attr in _CLOSED
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _closed(annotation.left) and _closed(annotation.right)
    if isinstance(annotation, ast.Subscript):
        head = annotation.value
        name = head.id if isinstance(head, ast.Name) else getattr(head, "attr", "")
        if name == "Literal":
            return True
        if name in {"Optional", "Annotated"}:
            inner = annotation.slice
            first = inner.elts[0] if isinstance(inner, ast.Tuple) else inner
            return _closed(first)
    return False


def _names(expression: ast.expr):
    """The names an expression reads, not looking inside ``len(...)``: a count
    of the request's text is not the text."""
    stack = [expression]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "len":
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            yield node.id
        stack.extend(ast.iter_child_nodes(node))


def _root(node: ast.expr) -> str | None:
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _texty(node: ast.expr) -> bool:
    """An expression that is text by its own shape: a string literal, an
    f-string, or a ``+`` or ``%`` that builds one."""
    if isinstance(node, ast.JoinedStr):
        return True
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return _texty(node.left) or (isinstance(node.op, ast.Add) and _texty(node.right))
    return False


def _formatted(node: ast.AST) -> list[ast.expr]:
    """What a node, if it builds a sentence, puts into it.

    The four ways of building one that a route has to hand: an f-string, a
    ``+`` with a string, ``"..." % value`` and ``"...".format(value)``.
    """
    if isinstance(node, ast.JoinedStr):
        return [part.value for part in node.values if isinstance(part, ast.FormattedValue)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)) and _texty(node):
        return [node.right] if isinstance(node.op, ast.Mod) else [node.left, node.right]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and isinstance(node.func.value, (ast.Constant, ast.JoinedStr))
    ):
        return [*node.args, *(kw.value for kw in node.keywords)]
    return []


class _Echoes(ast.NodeVisitor):
    """HTTPException details that format request input into a sentence.

    A detail is built by f-string, ``+``, ``%`` or ``.format``, directly or
    through a local of the same function that is assigned such an expression.
    Request input is a parameter of the enclosing function that is not
    annotated as an id the server parsed, a closed set, a number or a flag, or
    the item of a ``for`` loop that iterates directly over one (the index of an
    ``enumerate`` is not the item). ``HTTPException`` is found under the name
    it is imported as. It does not follow a value once it has been copied to
    a local, nor the text of an exception a service raised, nor ``str(exc)``,
    so it is a tripwire for the common shapes and not a proof.
    """

    def __init__(self, aliases: frozenset[str] = frozenset({"HTTPException"})) -> None:
        self.aliases = aliases
        self.functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        self.hits: list[tuple[str, str, int]] = []

    def visit_FunctionDef(self, node) -> None:
        self.functions.append(node)
        self.generic_visit(node)
        self.functions.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _inputs(self) -> set[str]:
        params: dict[str, ast.expr | None] = {}
        for function in self.functions:
            args = function.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None and arg.arg not in {"self", "cls"}:
                    params[arg.arg] = arg.annotation
        inputs = {name for name, ann in params.items() if not _closed(ann)}
        for loop in ast.walk(self.functions[-1]):
            if not isinstance(loop, (ast.For, ast.AsyncFor)):
                continue
            source, target = loop.iter, loop.target
            if (
                isinstance(source, ast.Call)
                and getattr(source.func, "id", "") == "enumerate"
                and source.args
            ):
                source = source.args[0]
                if isinstance(target, ast.Tuple):
                    target = ast.Tuple(elts=target.elts[1:], ctx=ast.Store())
            if _root(source) in inputs:
                inputs.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        return inputs

    def _built(self, detail: ast.expr) -> list[ast.AST]:
        """The detail's nodes, and those of what its locals were assigned."""
        nodes = list(ast.walk(detail))
        assigned: dict[str, list[ast.expr]] = {}
        for statement in ast.walk(self.functions[-1]):
            if isinstance(statement, ast.Assign):
                names = [t.id for t in statement.targets if isinstance(t, ast.Name)]
            elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)) and statement.value:
                names = [statement.target.id] if isinstance(statement.target, ast.Name) else []
            else:
                continue
            for name in names:
                assigned.setdefault(name, []).append(statement.value)
        seen: set[str] = set()
        queue = [n.id for n in nodes if isinstance(n, ast.Name) and n.id in assigned]
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            for value in assigned[name]:
                found = list(ast.walk(value))
                nodes.extend(found)
                queue.extend(n.id for n in found if isinstance(n, ast.Name) and n.id in assigned)
        return nodes

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if getattr(func, "id", getattr(func, "attr", "")) in self.aliases and self.functions:
            detail = next((kw.value for kw in node.keywords if kw.arg == "detail"), None)
            if detail is None and len(node.args) >= 2:
                detail = node.args[1]
            if detail is not None:
                inputs = self._inputs()
                for built in self._built(detail):
                    for part in _formatted(built):
                        if inputs & set(_names(part)):
                            self.hits.append(
                                (self.functions[-1].name, ast.unparse(part), node.lineno)
                            )
        self.generic_visit(node)


def _echoes(source: str) -> list[tuple[str, str, int]]:
    tree = ast.parse(source)
    aliases = {"HTTPException"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "HTTPException" and alias.asname
    }
    visitor = _Echoes(frozenset(aliases))
    visitor.visit(tree)
    return visitor.hits


FLAGGED = '''
from fastapi import HTTPException
async def route(provider: str, body, names: list[str]):
    for pkg in names:
        raise HTTPException(status_code=400, detail=f"bad {pkg}")
    raise HTTPException(400, f"bad {provider}")
async def another(body: Body):
    raise HTTPException(status_code=400, detail={"message": f"bad {body.provider}"})
'''

#: One sentence per way of building it, each flagged, so a rewrite of the
#: checker that stops seeing a shape fails here and not on a real route.
SHAPES = {
    "plus": 'raise HTTPException(status_code=400, detail="bad " + provider)',
    "plus, name first": 'raise HTTPException(status_code=400, detail=provider + " is bad")',
    "percent": 'raise HTTPException(status_code=400, detail="bad %s" % provider)',
    "percent tuple": 'raise HTTPException(status_code=400, detail="bad %s %s" % (1, provider))',
    "format": 'raise HTTPException(status_code=400, detail="bad {}".format(provider))',
    "format keyword": 'raise HTTPException(status_code=400, detail="bad {p}".format(p=provider))',
    "a local": 'message = f"bad {provider}"\n    raise HTTPException(status_code=400, detail=message)',
    "a local in a dict": (
        'message = "bad " + provider\n'
        '    raise HTTPException(status_code=400, detail={"message": message})'
    ),
    "an alias": 'raise Refused(status_code=400, detail=f"bad {provider}")',
}

NOT_FLAGGED = '''
from fastapi import HTTPException
from typing import Literal
async def route(thing_id: UUID, kind: Literal["a", "b"], count: int | None, names: list[str]):
    status = "x"
    message = "fixed " + status
    for position, name in enumerate(names, start=1):
        raise HTTPException(status_code=400, detail=f"entry {position}")
    raise HTTPException(400, f"{thing_id} {kind} {count} {status} {len(names)}")
async def sums(provider: str, count: int):
    total = count + 1
    raise HTTPException(status_code=400, detail="x" + str(total))
async def fixed(provider: str):
    note = provider.strip()
    raise HTTPException(status_code=400, detail="bad %s" % "fixed")
async def not_an_exception(provider: str):
    return Other(status_code=400, detail=f"bad {provider}")
'''


def _shape(statement: str, *, alias: bool = False) -> str:
    imports = (
        "from fastapi import HTTPException as Refused\n" if alias
        else "from fastapi import HTTPException\n"
    )
    return f"{imports}async def route(provider: str):\n    {statement}\n"


def test_a_new_http_detail_does_not_format_request_input():
    """No ``HTTPException`` detail in ``src/server/app`` formats text the request chose.

    The checker is tried on the shapes the audit found first, so a rewrite that
    stops seeing one of them fails here instead of letting a real one through.
    Fix a hit by naming the field or the rule; if the value is the server's own
    (a verb it passes, a key it resolved), add it to ``ALLOWED`` with the reason.
    """
    assert [h[1] for h in _echoes(FLAGGED)] == ["pkg", "provider", "body.provider"]
    assert _echoes(NOT_FLAGGED) == []
    for shape, statement in SHAPES.items():
        hits = _echoes(_shape(statement, alias=shape == "an alias"))
        assert hits and all("provider" in h[1] for h in hits), shape

    found: dict[tuple[str, str, str], int] = {}
    for path in sorted(APP_DIR.rglob("*.py")):
        for function, expression, line in _echoes(path.read_text()):
            found.setdefault((path.relative_to(APP_DIR).as_posix(), function, expression), line)
    unexplained = {key: line for key, line in found.items() if key not in ALLOWED}
    assert not unexplained, f"a detail formats request input: {unexplained}"
    stale = sorted(set(ALLOWED) - set(found))
    assert not stale, f"ALLOWED entries that no longer match a sentence: {stale}"
