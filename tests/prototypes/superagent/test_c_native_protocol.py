"""Protocol checks for the ``c_native`` prototype.

The shared contract in ``test_contract.py`` checks what an adapter must do.
It cannot see the wire between the CLI and the model shim, so four protocol
defects passed it: an invalid content block index, a tool name that did not
match the advertised one, assistant text that vanished before the next
model request, and a turn that was not initialized after a credit stop.

These checks look at the bytes. The first one talks to the shim's own
socket, the way the CLI does, and reads the stream the CLI would read.

The later checks pin the rest of the wire: the argv the fake refuses to run
without, the HTTP status the shim answers with and the fake honours (a
credit stop is an ordinary ``end_turn`` reply; a real failure is an error
the fake retries once if it is a 5xx), the side endpoints that must not
touch the turn, several tool calls in one message, and the environment the
children get.
"""

from __future__ import annotations

import copy
import json
import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from prototypes.superagent.c_native import fake_claude_code as fake
from prototypes.superagent.contract import (
    CREDIT_STOP_TEXT,
    CreditDecision,
    FakeCreditGate,
    ScriptedChunk,
    autonomy_tool_names,
    scripted_tool_call_text,
    scripted_tool_result,
)
from tests.prototypes.superagent.conftest import (
    AdapterFactory,
    load_adapter_module,
    make_doubles,
)

ADVERTISED_TOOL = "mcp__autonomy__autonomy_route"


@pytest.fixture
def c_native_factory(runtime_root: Path):
    """A factory that builds the ``c_native`` adapter and nothing else."""

    module = load_adapter_module("c_native")
    if module is None:
        pytest.skip("prototypes.superagent.c_native.adapter is not installed")
    factory = AdapterFactory(module, runtime_root)
    try:
        yield factory
    finally:
        factory.teardown()


def _request(
    socket_path: Path,
    payload: dict[str, Any] | None,
    *,
    method: str = "POST",
    path: str = "/v1/messages",
) -> tuple[int, dict[str, str], bytes]:
    """One raw HTTP request to the shim. Returns status, headers and body."""

    body = json.dumps(payload).encode("utf-8") if payload is not None else b""
    head = (
        f"{method} {path} HTTP/1.1\r\n"
        "Host: localhost\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("latin-1")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(str(socket_path))
        connection.sendall(head + body)
        stream = connection.makefile("rb")
        status = int(stream.readline().split()[1])
        headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            key, _, value = line.decode("latin-1").partition(":")
            headers[key.strip().lower()] = value.strip()
        return status, headers, stream.read()


def _sse_events(body: bytes) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for raw in body.splitlines():
        text = raw.decode("utf-8", "replace").strip()
        if text.startswith("data:"):
            events.append(json.loads(text[5:].strip()))
    return events


def _post_messages(socket_path: Path, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One Messages request to the shim. Returns the SSE events, in order."""

    status, _, body = _request(socket_path, payload)
    assert status == 200, f"the shim answered {status}: {body!r}"
    return _sse_events(body)


def _messages_payload(text: str = "hi", **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": "scripted",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": [{"type": "text", "text": text}]}],
        "tools": [
            {"name": ADVERTISED_TOOL, "description": "", "input_schema": {"type": "object"}}
        ],
        "stream": True,
    }
    payload.update(extra)
    return payload


def _assert_valid_block_indices(events: list[dict[str, Any]]) -> None:
    """Every block is opened once, deltas only reach open blocks, all closed."""

    opened: list[int] = []
    closed: set[int] = set()
    for event in events:
        kind = event.get("type")
        if kind == "content_block_start":
            index = int(event["index"])
            assert index not in opened, f"content block {index} was opened twice"
            opened.append(index)
        elif kind == "content_block_delta":
            index = int(event["index"])
            assert index in opened, (
                f"content block {index} got a delta before it opened"
            )
            assert index not in closed, (
                f"content block {index} got a delta after it closed"
            )
        elif kind == "content_block_stop":
            index = int(event["index"])
            assert index in opened, f"content block {index} closed without opening"
            closed.add(index)
    assert sorted(closed) == sorted(opened), (
        f"content blocks {sorted(set(opened) - closed)} were never closed"
    )


class _FailFirst(FakeCreditGate):
    """A gate that raises on its first ``failures`` checks, then allows."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    def authorize(self, *, user_id: str, session_id: str) -> CreditDecision:
        self.checks.append({"user_id": user_id, "session_id": session_id})
        if len(self.checks) <= self.failures:
            raise RuntimeError("the credit service is down")
        return CreditDecision(allowed=True, reason="")


class _DenyAt(FakeCreditGate):
    """A gate that allows every check but the one at ``deny_at``."""

    def __init__(self, deny_at: int) -> None:
        super().__init__()
        self.deny_at = deny_at

    def authorize(self, *, user_id: str, session_id: str) -> CreditDecision:
        self.checks.append({"user_id": user_id, "session_id": session_id})
        if len(self.checks) == self.deny_at:
            return CreditDecision(allowed=False, reason=CREDIT_STOP_TEXT)
        return CreditDecision(allowed=True, reason="")


@pytest.mark.asyncio
async def test_model_stream_uses_valid_content_block_indices(c_native_factory) -> None:
    """The text of a turn is one block; the tool request is the next one."""

    deps = make_doubles(
        script=[
            "A",
            "B",
            "C",
            ScriptedChunk(tool_name="autonomy_route", tool_args={"ticket_id": "t1"}),
        ]
    )
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    scratch = c_native_factory.runtimes[0]["runtime"].scratch_dir
    events = _post_messages(
        scratch / "model.sock",
        {
            "model": "scripted",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "tools": [
                {
                    "name": ADVERTISED_TOOL,
                    "description": "",
                    "input_schema": {"type": "object"},
                }
            ],
            "stream": True,
        },
    )
    _assert_valid_block_indices(events)
    starts = [event for event in events if event["type"] == "content_block_start"]
    assert [event["index"] for event in starts] == [0, 1], (
        f"the stream opened {starts!r}"
    )
    assert starts[0]["content_block"]["type"] == "text"
    assert starts[1]["content_block"]["type"] == "tool_use"
    deltas = [event for event in events if event["type"] == "content_block_delta"]
    assert [event["index"] for event in deltas] == [0, 0, 0, 1], (
        "the three text chunks did not share one block"
    )


@pytest.mark.asyncio
async def test_tool_call_on_the_wire_uses_the_advertised_name(
    c_native_factory,
) -> None:
    """The model's reply names a tool the client actually offered."""

    deps = make_doubles(
        script=[
            ScriptedChunk(tool_name="autonomy_route", tool_args={"ticket_id": "t1"}),
            "after",
        ]
    )
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    scratch = c_native_factory.runtimes[0]["runtime"].scratch_dir
    events = _post_messages(
        scratch / "model.sock",
        {
            "model": "scripted",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
            "tools": [
                {
                    "name": ADVERTISED_TOOL,
                    "description": "",
                    "input_schema": {"type": "object"},
                }
            ],
            "stream": True,
        },
    )
    starts = [event for event in events if event["type"] == "content_block_start"]
    tool_block = next(
        event["content_block"]
        for event in starts
        if event["content_block"]["type"] == "tool_use"
    )
    assert tool_block["name"] == ADVERTISED_TOOL, (
        f"the tool was offered as {ADVERTISED_TOOL!r} but called {tool_block['name']!r}"
    )


@pytest.mark.asyncio
async def test_a_prefixed_tool_call_is_authorized(c_native_factory) -> None:
    """The CLI answers with the advertised name, and the shim authorizes it."""

    arguments = {"ticket_id": "t1"}
    deps = make_doubles(
        script=[
            ScriptedChunk(tool_name="autonomy_route", tool_args=arguments),
            "after",
        ]
    )
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    events = await adapter.turn("s1", "route it")
    assert [event.text for event in events if event.kind == "tool_refused"] == []
    assert [event.text for event in events if event.kind == "tool_call"] == [
        scripted_tool_call_text("autonomy_route", arguments)
    ]
    assert [event.text for event in events if event.kind == "tool_result"] == [
        scripted_tool_result("autonomy_route", arguments)
    ]


@pytest.mark.asyncio
async def test_assistant_text_before_a_tool_call_reaches_the_next_request(
    c_native_factory,
) -> None:
    """The text the model wrote before the tool call goes back with it."""

    deps = make_doubles(
        script=[
            ScriptedChunk(
                text="before",
                tool_name="autonomy_route",
                tool_args={"ticket_id": "t1"},
            ),
            "after",
        ]
    )
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    await adapter.turn("s1", "route it")
    assert len(deps["model"].calls) == 2
    second = "\n".join(deps["model"].calls[1].texts())
    assert "before" in second, (
        f"the text before the tool call was lost: {deps['model'].calls[1].texts()!r}"
    )


@pytest.mark.asyncio
async def test_the_next_turn_starts_over_after_a_credit_stop(
    c_native_factory,
) -> None:
    """A turn after a stopped one is a new turn, not a continuation."""

    deps = make_doubles(
        script=[
            ScriptedChunk(tool_name="autonomy_route", tool_args={"ticket_id": "t1"}),
            "after",
        ]
    )
    # Check 1 opens the first turn. Check 2 is the one after the tool call,
    # and it stops the turn in the middle.
    deps["credit_gate"] = _DenyAt(2)
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")

    stopped = await adapter.turn("s1", "one")
    assert [event.kind for event in stopped] == ["tool_call", "tool_result", "stop"], (
        f"the first turn was not stopped mid-turn: {stopped!r}"
    )
    await adapter.turn("s1", "two")

    assert len(deps["memory"].calls) == 2, (
        f"the second turn did not recall memory: {deps['memory'].calls!r}"
    )
    users = [
        entry.text for entry in adapter.transcript("s1").entries if entry.kind == "user"
    ]
    assert users == ["one", "two"], (
        f"the second turn did not record its own user row: {users!r}"
    )
    second = "\n".join(deps["model"].calls[-1].texts())
    assert "two" in second, f"the second turn did not see its own prompt: {second!r}"


# -- the argv --------------------------------------------------------------


def test_the_cli_argv_carries_the_isolation_flags(runtime_root: Path) -> None:
    """The fake refuses the argv without ``--bare`` and its neighbours."""

    from prototypes.superagent.c_native.adapter import cli_argv
    from prototypes.superagent.contract import RuntimeContext

    runtime = RuntimeContext(scratch_dir=runtime_root, bridge_path="unused")
    argv = cli_argv(runtime)
    flags = fake.parse_flags(argv[2:])
    fake.check_tool_restriction(flags)
    assert flags["--bare"] is True
    assert flags["--no-session-persistence"] is True
    assert flags["--setting-sources"] == ""

    def without(*names: str, value_of: str = "") -> list[str]:
        out: list[str] = []
        skip = False
        for token in argv[2:]:
            if skip:
                skip = False
                continue
            if token in names:
                skip = token == value_of
                continue
            out.append(token)
        return out

    for broken, needle in (
        (without("--bare"), "--bare"),
        (without("--no-session-persistence"), "--no-session-persistence"),
        (
            without("--setting-sources", value_of="--setting-sources"),
            "--setting-sources",
        ),
    ):
        with pytest.raises(fake.CliError, match=needle):
            fake.check_tool_restriction(fake.parse_flags(broken))
    loaded = [
        "user" if previous == "--setting-sources" else token
        for previous, token in zip(["", *argv[2:-1]], argv[2:], strict=True)
    ]
    with pytest.raises(fake.CliError, match="--setting-sources must be empty"):
        fake.check_tool_restriction(fake.parse_flags(loaded))


# -- HTTP status -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_credit_stop_is_an_ordinary_end_turn_reply(c_native_factory) -> None:
    """The client sees a 200 end_turn with no tool use; the stop is recorded."""

    deps = make_doubles(script=["never"])
    deps["credit_gate"] = _DenyAt(1)
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    scratch = c_native_factory.runtimes[0]["runtime"].scratch_dir
    # Claude Code adds ``?beta=true``; the query string must not matter.
    status, headers, body = _request(
        scratch / "model.sock", _messages_payload(), path="/v1/messages?beta=true"
    )
    assert status == 200, f"a credit stop was answered with {status}: {body!r}"
    assert headers["content-type"].startswith("text/event-stream")
    events = _sse_events(body)
    _assert_valid_block_indices(events)
    blocks = [e["content_block"] for e in events if e["type"] == "content_block_start"]
    assert [block["type"] for block in blocks] == ["text"]
    stop = [e for e in events if e["type"] == "message_delta"]
    assert stop[-1]["delta"]["stop_reason"] == "end_turn"
    assert [event.kind for event in adapter.events("s1")] == ["stop"]
    assert adapter.session("s1").status == "stopped"
    assert deps["model"].calls == []


@pytest.mark.asyncio
async def test_a_bridge_error_is_a_502_and_drops_the_turn(c_native_factory) -> None:
    """A real failure is an API error, and nothing of the turn is kept."""

    deps = make_doubles(script=["ok"])
    deps["credit_gate"] = _FailFirst(1)
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    scratch = c_native_factory.runtimes[0]["runtime"].scratch_dir
    status, _, body = _request(scratch / "model.sock", _messages_payload("lost"))
    assert status == 502, f"a bridge error was answered with {status}"
    assert json.loads(body)["error"]["type"] == "api_error"
    assert adapter.transcript("s1").entries == []

    await adapter.turn("s1", "two")
    users = [e.text for e in adapter.transcript("s1").entries if e.kind == "user"]
    assert users == ["two"], f"the failed request's user row survived: {users!r}"


@pytest.mark.asyncio
async def test_the_cli_fails_on_a_5xx_after_one_retry(c_native_factory) -> None:
    """The fake retries a 5xx once, then exits non-zero like the real CLI."""

    deps = make_doubles(script=["ok"])
    deps["credit_gate"] = _FailFirst(2)
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    with pytest.raises(RuntimeError, match=r"rc=1.*API Error: 502"):
        await adapter.turn("s1", "one")
    assert len(deps["credit_gate"].checks) == 2, "the 5xx was not retried once"
    assert adapter.session("s1").status == "ready"
    assert adapter.transcript("s1").entries == []

    await adapter.turn("s1", "two")
    users = [e.text for e in adapter.transcript("s1").entries if e.kind == "user"]
    assert users == ["two"]


class _StubEndpoint:
    """A unix-socket HTTP endpoint that plays canned replies in order."""

    def __init__(self, path: Path, replies: list[bytes]) -> None:
        self.path = path
        self.replies = list(replies)
        self.requests = 0
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while self.replies:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            with connection, connection.makefile("rwb") as stream:
                length = 0
                while True:
                    line = stream.readline()
                    if line in (b"\r\n", b"\n", b""):
                        break
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1])
                stream.read(length)
                self.requests += 1
                stream.write(self.replies.pop(0))
                stream.flush()

    def close(self) -> None:
        self._server.close()
        self._thread.join(timeout=5)


def _json_reply(status: int, payload: dict[str, Any]) -> bytes:
    body = json.dumps(payload).encode()
    return (
        f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body


def _sse_reply(*events: dict[str, Any]) -> bytes:
    frames = b"".join(
        f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        for event in events
    )
    return b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n" + frames


_START = {"type": "message_start", "message": {"id": "m", "content": []}}
_STOP = {"type": "message_stop"}


def _end(reason: str) -> dict[str, Any]:
    return {"type": "message_delta", "delta": {"stop_reason": reason}}


def _text_block(index: int, text: str) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": index,
         "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": index},
    ]


def _tool_block(index: int, use_id: str, name: str, args: dict) -> list[dict[str, Any]]:
    return [
        {"type": "content_block_start", "index": index,
         "content_block": {"type": "tool_use", "id": use_id, "name": name, "input": {}}},
        {"type": "content_block_delta", "index": index,
         "delta": {"type": "input_json_delta", "partial_json": json.dumps(args)}},
        {"type": "content_block_stop", "index": index},
    ]


_ERROR = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
_OK = _sse_reply(_START, *_text_block(0, "fine"), _end("end_turn"), _STOP)


@pytest.mark.parametrize(
    ("replies", "requests", "outcome"),
    [
        ([_json_reply(500, _ERROR), _OK], 2, "fine"),
        ([_json_reply(503, _ERROR), _json_reply(502, _ERROR)], 2, 502),
        ([_json_reply(400, _ERROR), _OK], 1, 400),
        ([_json_reply(404, _ERROR), _OK], 1, 404),
        ([_sse_reply(_START, _ERROR), _OK], 1, 400),
    ],
    ids=["5xx-then-ok", "5xx-twice", "4xx", "404", "in-stream-error"],
)
def test_the_fake_honours_the_http_status(
    runtime_root: Path, replies: list[bytes], requests: int, outcome: Any
) -> None:
    """Non-2xx is an API error; only a 5xx is retried, and only once."""

    endpoint = _StubEndpoint(runtime_root / "stub.sock", replies)
    client = fake.AnthropicClient(f"unix://{endpoint.path}", "sk-test", "scripted")
    try:
        if isinstance(outcome, str):
            assert client.messages([], [])["text"] == outcome
        else:
            with pytest.raises(fake.ApiError) as raised:
                client.messages([], [])
            assert raised.value.status == outcome
    finally:
        endpoint.close()
    assert endpoint.requests == requests


# -- side endpoints --------------------------------------------------------


@pytest.mark.asyncio
async def test_side_requests_do_not_touch_the_turn(c_native_factory) -> None:
    """count_tokens is answered; other paths are refused; the turn is intact."""

    deps = make_doubles(script=["ok"])
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    sock = c_native_factory.runtimes[0]["runtime"].scratch_dir / "model.sock"

    status, _, body = _request(
        sock, _messages_payload("side"), path="/v1/messages/count_tokens"
    )
    assert status == 200
    assert json.loads(body)["input_tokens"] > 0
    assert _request(sock, _messages_payload(), path="/v1/messagesX")[0] == 404
    assert _request(sock, _messages_payload(), path="/v1/messages/batches")[0] == 404
    assert _request(sock, None, method="GET")[0] == 405
    assert deps["credit_gate"].checks == []
    assert deps["memory"].calls == []
    assert deps["model"].calls == []
    assert adapter.transcript("s1").entries == []

    await adapter.turn("s1", "hi")
    users = [e.text for e in adapter.transcript("s1").entries if e.kind == "user"]
    assert users == ["hi"]


# -- several tool calls in one message -------------------------------------


def test_the_fake_parses_several_tool_uses_in_one_stream() -> None:
    reply = _sse_reply(
        _START,
        *_text_block(0, "two calls"),
        *_tool_block(1, "toolu_a", ADVERTISED_TOOL, {"ticket_id": "a"}),
        *_tool_block(2, "toolu_b", ADVERTISED_TOOL, {"ticket_id": "b"}),
        _end("tool_use"),
        _STOP,
    )
    body = reply.split(b"\r\n\r\n", 1)[1]
    answer = fake._read_stream(iter(body.splitlines(keepends=True)))
    assert answer["text"] == "two calls"
    assert [(t["id"], t["input"]) for t in answer["tool_uses"]] == [
        ("toolu_a", {"ticket_id": "a"}),
        ("toolu_b", {"ticket_id": "b"}),
    ]


def test_the_fake_runs_every_tool_use_and_answers_them_together() -> None:
    class Model:
        def __init__(self) -> None:
            self.requests: list[list[dict[str, Any]]] = []

        def messages(self, messages, tools):
            self.requests.append(copy.deepcopy(messages))
            if len(self.requests) == 1:
                return {
                    "text": "before",
                    "tool_uses": [
                        {"id": "toolu_a", "name": ADVERTISED_TOOL,
                         "input": {"ticket_id": "a"}},
                        {"id": "toolu_b", "name": "Bash", "input": {"command": "ls"}},
                        {"id": "toolu_c", "name": ADVERTISED_TOOL,
                         "input": {"ticket_id": "c"}},
                    ],
                }
            return {"text": "done", "tool_uses": []}

    class Mcp:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            return f"ran:{arguments['ticket_id']}"

    model, mcp = Model(), Mcp()
    tools = [{"name": ADVERTISED_TOOL, "description": "", "input_schema": {}}]
    answer = fake.run_turn(model, mcp, "go", tools)
    assert answer["text"] == "done"
    assert mcp.calls == [
        ("autonomy_route", {"ticket_id": "a"}),
        ("autonomy_route", {"ticket_id": "c"}),
    ]
    _, assistant, results = model.requests[1]
    assert [block["type"] for block in assistant["content"]] == [
        "text", "tool_use", "tool_use", "tool_use"
    ]
    assert results["role"] == "user"
    assert [
        (block["tool_use_id"], block["content"][0]["text"])
        for block in results["content"]
    ] == [("toolu_a", "ran:a"), ("toolu_b", "refused:Bash"), ("toolu_c", "ran:c")]


@pytest.mark.asyncio
async def test_the_shim_records_several_tool_calls_in_one_message(
    c_native_factory,
) -> None:
    """Each tool_result is matched to its own tool_use by id."""

    first, second = autonomy_tool_names("router")[:2]
    deps = make_doubles(
        script=[ScriptedChunk(tool_name=first, tool_args={"ticket_id": "t1"}), "after"]
    )
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    sock = c_native_factory.runtimes[0]["runtime"].scratch_dir / "model.sock"
    tools = [
        {"name": f"mcp__autonomy__{name}", "description": "", "input_schema": {}}
        for name in (first, second)
    ]
    _post_messages(sock, _messages_payload(tools=tools))
    prompt = _messages_payload()["messages"]
    calls = [("toolu_a", first, {"ticket_id": "t1"}), ("toolu_b", second, {"x": 1})]
    followup = [
        *prompt,
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": use_id, "name": f"mcp__autonomy__{name}",
                 "input": args}
                for use_id, name, args in calls
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": use_id,
                 "content": [{"type": "text", "text": f"result-{use_id}"}]}
                for use_id, _, _ in calls
            ],
        },
    ]
    _post_messages(sock, _messages_payload(messages=followup, tools=tools))
    seen = [(e.kind, e.text) for e in adapter.events("s1") if e.kind.startswith("tool")]
    assert seen == [
        ("tool_call", scripted_tool_call_text(first, {"ticket_id": "t1"})),
        ("tool_call", scripted_tool_call_text(second, {"x": 1})),
        ("tool_result", "result-toolu_a"),
        ("tool_result", "result-toolu_b"),
    ]


# -- the environment -------------------------------------------------------


@pytest.mark.asyncio
async def test_the_children_get_a_minimal_environment(
    c_native_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing of the parent's environment leaks past the allow list."""

    from prototypes.superagent.c_native import adapter as module

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "leaked")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/leaked")
    deps = make_doubles(script=["ok"])
    adapter = c_native_factory(**deps)
    await adapter.start("s1", role="router", user_id="u1")
    scratch = c_native_factory.runtimes[0]["runtime"].scratch_dir
    env = adapter._environment("s1")
    allowed = {*module._ENV_ALLOWLIST, "HOME", "C_NATIVE_BRIDGE", "C_NATIVE_SESSION_ID"}
    assert set(env) <= allowed, f"leaked: {sorted(set(env) - allowed)}"
    assert env["HOME"] == str(scratch)
    events = await adapter.turn("s1", "hi")
    assert [event.kind for event in events][-1] == "done"
