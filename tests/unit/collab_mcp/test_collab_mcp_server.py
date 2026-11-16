"""conductor-collab-mcp: transports, loopback-only HTTP, keychain token, no leaks.

The upstream Conductor (the L2 routes) is an in-memory ``httpx.MockTransport``.
The only real socket is the MCP HTTP listener, bound to 127.0.0.1 on an
ephemeral port, with pytest-socket restricted to that address.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os

import anyio
import httpx
import httpx2
import pytest
import pytest_socket

from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from src.collab.schemas import SCHEMAS, TOOL_INPUT_SCHEMAS
from src.collab_mcp import __main__ as entry
from src.collab_mcp import server as server_module
from src.collab_mcp.client import API_PREFIX, ENDPOINTS, CollabClient, KeychainRef
from src.collab_mcp.server import (
    HTTP_PATH,
    LOOPBACK_HOST,
    UNTRUSTED_NOTE,
    CollabMCP,
    RequireAppToken,
    build_server,
    make_http_server,
    run_stdio,
    tool_definitions,
)
from tests.unit.collab_mcp.conftest import FakeKeychain

TICKET = "t:" + "0" * 8 + "-0000-0000-0000-" + "0" * 12


class FakeConductor:
    """The L2 routes in memory: checks the bearer, answers envelopes."""

    def __init__(self, token: str, *, echo_token: bool = False) -> None:
        self.token = token
        self.echo_token = echo_token
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return self._envelope(401, ok=False, code="unauthorized")
        path = request.url.path
        assert path.startswith(API_PREFIX)
        route = path[len(API_PREFIX):]
        data: dict = {}
        if route == "/whoami":
            data = {
                "app_id": "codex",
                "user_id": "u1",
                "scopes": ["read", "claim"],
                "can_write": True,
                "max_leases": 1,
                "enabled": True,
            }
            if self.echo_token:
                data["agent"] = self.token
        elif route.endswith("/claim"):
            data = {
                "lease_id": "lease-1",
                "ref": TICKET,
                "expires_at": "2026-11-13T10:15:00Z",
                "heartbeat_every_seconds": 60,
                "worktree": {"path": "/tmp/wt", "branch": "conductor/ticket/x", "base_sha": "abc1234"},
            }
        elif route == "/tasks":
            data = {"tasks": [], "next_cursor": None}
        return self._envelope(200, data=data)

    def _envelope(self, status: int, *, ok: bool = True, code: str = "ok", data=None) -> httpx.Response:
        body = {"ok": ok, "code": code, "data": data or {}, "untrusted_fields": [], "redacted": 0}
        if self.echo_token:
            body["message"] = f"echo {self.token}"
        return httpx.Response(status, json=body)


def _core(token: str, fake: FakeConductor) -> CollabMCP:
    return CollabMCP(CollabClient(token, transport=httpx.MockTransport(fake)))


def _tool_view(tools) -> list[tuple[str, str, str]]:
    return [
        (tool.name, tool.description or "", json.dumps(tool.input_schema, sort_keys=True))
        for tool in tools
    ]


# -- tool definitions -------------------------------------------------------


def test_every_protocol_tool_is_listed_with_its_schema() -> None:
    tools = {tool.name: tool for tool in tool_definitions()}
    assert set(tools) == set(TOOL_INPUT_SCHEMAS) == set(ENDPOINTS)
    for name, schema_name in TOOL_INPUT_SCHEMAS.items():
        assert tools[name].input_schema == SCHEMAS[schema_name]
        assert UNTRUSTED_NOTE in tools[name].description


# -- stdio and HTTP serve identical tools ------------------------------------


async def _stdio_tools(server, core) -> list[dict]:
    """Run the real stdio transport over pipes and speak raw JSON-RPC to it."""
    server_in_r, server_in_w = os.pipe()
    server_out_r, server_out_w = os.pipe()
    stdin = anyio.wrap_file(os.fdopen(server_in_r, "r", encoding="utf-8"))
    stdout = anyio.wrap_file(os.fdopen(server_out_w, "w", encoding="utf-8"))
    writer = os.fdopen(server_in_w, "w", encoding="utf-8")
    reader = os.fdopen(server_out_r, "r", encoding="utf-8")

    def send(message: dict) -> None:
        writer.write(json.dumps(message) + "\n")
        writer.flush()

    def receive(wanted_id: int) -> dict:
        while True:
            line = reader.readline()
            assert line, "stdio server closed early"
            message = json.loads(line)
            if message.get("id") == wanted_id:
                return message

    async def client() -> list[dict]:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        init = await anyio.to_thread.run_sync(receive, 1)
        assert init["result"]["serverInfo"]["name"] == "conductor-collab"
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = await anyio.to_thread.run_sync(receive, 2)
        writer.close()
        return listed["result"]["tools"]

    result: list[dict] = []

    async def collect() -> None:
        result.extend(await client())

    with anyio.fail_after(20):
        async with anyio.create_task_group() as group:
            group.start_soon(lambda: run_stdio(server, core, stdin=stdin, stdout=stdout))
            await collect()
    reader.close()
    return result


@pytest.mark.enable_socket
@pytest.mark.asyncio
async def test_stdio_and_http_list_identical_tools_and_http_binds_loopback(token) -> None:
    pytest_socket.socket_allow_hosts([LOOPBACK_HOST], allow_unix_socket=True)
    fake = FakeConductor(token)
    core = _core(token, fake)
    server = build_server(core)

    stdio_tools = await _stdio_tools(server, core)

    http = make_http_server(server, token, port=0)
    serving = asyncio.create_task(http.serve())
    try:
        with anyio.fail_after(20):
            while not http.started:
                await asyncio.sleep(0.01)
        addresses = [sock.getsockname() for srv in http.servers for sock in srv.sockets]
        assert addresses and all(address[0] == LOOPBACK_HOST for address in addresses)
        port = addresses[0][1]
        url = f"http://{LOOPBACK_HOST}:{port}{HTTP_PATH}"

        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as authed:
            async with Client(streamable_http_client(url, http_client=authed), mode="legacy") as client:
                http_tools = (await client.list_tools()).tools

            # Without the app token, or with another one, the transport refuses.
            async with httpx2.AsyncClient() as anonymous:
                bare = await anonymous.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                assert bare.status_code == 401
                wrong = await anonymous.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Authorization": "Bearer tok_wrong"},
                )
                assert wrong.status_code == 401
                assert token not in bare.text + wrong.text
    finally:
        http.should_exit = True
        await serving

    stdio_view = [
        (tool["name"], tool.get("description") or "", json.dumps(tool["inputSchema"], sort_keys=True))
        for tool in stdio_tools
    ]
    assert stdio_view == _tool_view(http_tools)
    assert stdio_view == _tool_view(tool_definitions())
    assert [name for name, _, _ in stdio_view] == list(TOOL_INPUT_SCHEMAS)
    # Listing tools never needs the upstream Conductor.
    assert fake.requests == []


# -- no option to bind another address -----------------------------------------


def test_the_entry_point_has_no_bind_address_option() -> None:
    parser = entry.build_parser()
    flags = {flag for action in parser._actions for flag in action.option_strings}
    assert not flags & {"--host", "--bind", "--address", "--listen", "-H", "-b"}
    for attempt in (["--host", "0.0.0.0"], ["--bind", "0.0.0.0"], ["--ho", "0.0.0.0"]):
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--app", "codex", "--http", *attempt])
        assert exc.value.code == 2


def test_the_http_server_factory_takes_no_host_and_pins_loopback(token) -> None:
    assert "host" not in inspect.signature(make_http_server).parameters
    assert LOOPBACK_HOST == "127.0.0.1"
    server = build_server(_core(token, FakeConductor(token)))
    http = make_http_server(server, token, port=0)
    assert http.config.host == "127.0.0.1"
    with pytest.raises(TypeError):
        make_http_server(server, token, port=0, host="0.0.0.0")  # type: ignore[call-arg]


@pytest.mark.asyncio
async def test_the_http_guard_refuses_a_non_loopback_peer(token) -> None:
    reached: list[dict] = []

    async def app(scope, receive, send) -> None:
        reached.append(scope)

    guard = RequireAppToken(app, token)
    sent: list[dict] = []

    async def send(message) -> None:
        sent.append(message)

    headers = [(b"authorization", f"Bearer {token}".encode())]
    await guard({"type": "http", "client": ("10.0.0.7", 5555), "headers": headers}, None, send)
    assert sent[0]["status"] == 403 and reached == []
    sent.clear()
    await guard({"type": "http", "client": ("127.0.0.1", 5555), "headers": headers}, None, send)
    assert sent == [] and len(reached) == 1


# -- token from a keychain reference -----------------------------------------------


def _run_main(monkeypatch, argv, keychain, *, transport=None) -> tuple[int, dict]:
    seen: dict = {}

    async def fake_stdio(server, core, **kwargs) -> None:
        seen["transport"] = "stdio"
        seen["core"] = core

    async def fake_http(server, core, token, *, port) -> None:
        seen["transport"] = "http"
        seen["port"] = port
        seen["token_matches"] = token == keychain.secret

    monkeypatch.setattr(server_module, "run_stdio", fake_stdio)
    monkeypatch.setattr(server_module, "run_http", fake_http)
    code = entry.main(argv, keychain=keychain, transport=transport)
    return code, seen


def test_main_reads_the_token_from_the_default_keychain_reference(monkeypatch, token, capsys) -> None:
    keychain = FakeKeychain(token)
    code, seen = _run_main(monkeypatch, ["--app", "codex"], keychain)
    assert code == 0 and seen["transport"] == "stdio"
    assert keychain.reads == ["keychain:conductor-collab/codex"]
    assert "token=<hidden>" in repr(seen["core"].client)
    out = capsys.readouterr()
    assert token not in out.out + out.err


def test_main_honours_an_explicit_reference_and_http(monkeypatch, token, capsys) -> None:
    keychain = FakeKeychain(token)
    code, seen = _run_main(
        monkeypatch,
        ["--app", "codex", "--token-ref", "keychain:other-service/acct", "--http", "--port", "0"],
        keychain,
    )
    assert code == 0 and seen == {"transport": "http", "port": 0, "token_matches": True}
    assert keychain.reads == ["keychain:other-service/acct"]
    assert token not in "".join(capsys.readouterr())


@pytest.mark.parametrize(
    "argv",
    [
        ["--app", "codex", "--token-ref", "plain-token-value"],
        ["--app", "Not_An_App"],
        ["--app", "codex", "--url", "http://192.0.2.10:8000"],
        ["--app", "codex", "--url", "https://conductor.example.com"],
    ],
)
def test_main_refuses_bad_references_apps_and_remote_urls(monkeypatch, token, capsys, argv) -> None:
    keychain = FakeKeychain(token)
    code, seen = _run_main(monkeypatch, argv, keychain)
    assert code == 2 and seen == {}
    out = capsys.readouterr()
    assert token not in out.out + out.err


def test_main_fails_cleanly_when_the_keychain_has_no_token(monkeypatch, capsys) -> None:
    code, seen = _run_main(monkeypatch, ["--app", "codex"], FakeKeychain(None))
    assert code == 2 and seen == {}
    assert "keychain:conductor-collab/codex" in capsys.readouterr().err


def test_keychain_reference_shape() -> None:
    assert str(KeychainRef.for_app("claude-code")) == "keychain:conductor-collab/claude-code"
    assert KeychainRef.parse("keychain:svc/acct") == KeychainRef("svc", "acct")


def test_keychain_write_keeps_the_secret_out_of_argv(token) -> None:
    from src.collab_mcp.client import Keychain

    calls: list[tuple[list[str], str | None]] = []

    def runner(argv, stdin):
        calls.append((argv, stdin))
        return 0, ""

    Keychain(runner).write(KeychainRef.for_app("codex"), token)
    argv, stdin = calls[0]
    assert argv == ["security", "-i"]
    assert token not in " ".join(argv)
    assert stdin is not None and token in stdin and "-s conductor-collab -a codex" in stdin


# -- calls: forward, validate, scrub ---------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_call_forwards_with_the_app_bearer_and_returns_the_envelope(token) -> None:
    fake = FakeConductor(token)
    async with Client(build_server(_core(token, fake))) as client:
        result = await client.call_tool("whoami", {})
    assert result.is_error is False
    assert result.structured_content["data"]["app_id"] == "codex"
    request = fake.requests[0]
    assert request.method == "GET" and request.url.path == f"{API_PREFIX}/whoami"
    assert request.headers["authorization"] == f"Bearer {token}"
    assert "x-service-token" not in request.headers and "x-user-id" not in request.headers


@pytest.mark.asyncio
async def test_invalid_input_is_refused_locally_without_a_request(token) -> None:
    fake = FakeConductor(token)
    async with Client(build_server(_core(token, fake))) as client:
        result = await client.call_tool("task_list", {"limit": 500})
        unknown = await client.call_tool("task_list", {"surprise": True})
    assert result.is_error is True
    assert result.structured_content["code"] == "invalid_schema"
    assert unknown.structured_content["code"] == "invalid_schema"
    assert fake.requests == []


@pytest.mark.asyncio
async def test_a_token_echoed_by_the_server_never_reaches_mcp_output(token, caplog) -> None:
    fake = FakeConductor(token, echo_token=True)
    async with Client(build_server(_core(token, fake))) as client:
        results = [
            await client.call_tool("whoami", {}),
            await client.call_tool("task_claim", {"ref": TICKET, "revision": 3}),
        ]
    for result in results:
        text = json.dumps(result.structured_content) + "".join(
            block.text for block in result.content if hasattr(block, "text")
        )
        assert token not in text
        assert "[redacted]" in text
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_a_claim_through_mcp_starts_tracking_its_lease(token) -> None:
    fake = FakeConductor(token)
    core = _core(token, fake)
    async with Client(build_server(core)) as client:
        await client.call_tool("task_claim", {"ref": TICKET, "revision": 3})
    assert core.heartbeat.leases == ("lease-1",)
    claim = fake.requests[0]
    assert claim.method == "POST"
    assert claim.url.path == f"{API_PREFIX}/tasks/{TICKET}/claim"
    assert json.loads(claim.content) == {"revision": 3}


def test_parser_namespace_is_plain() -> None:
    # The entry point parses argv only; nothing is read from the environment
    # that could change the bind address.
    args = entry.build_parser().parse_args(["--app", "codex", "--http"])
    assert isinstance(args, argparse.Namespace)
    assert not any("host" in name for name in vars(args))

