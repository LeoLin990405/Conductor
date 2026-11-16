"""Codex app-server transport: true deltas and park/resume approvals."""

from __future__ import annotations

import stat
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.server.services.native_runtime.approvals import get_approval_broker
from src.server.services.native_runtime.codex_app_server import CodexAppServerRuntime

FAKE = r'''#!/usr/bin/env python3
import json, sys

thread_id = "fake-thread"
approval_pending = False
approval_item = "exec-1"
prompt = ""

def emit(method, params):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    req = msg.get("id")
    if method == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req, "result": {}}) + "\n"); sys.stdout.flush()
    elif method == "thread/start":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req, "result": {"thread": {"id": thread_id}}}) + "\n"); sys.stdout.flush()
    elif method == "turn/start":
        prompt = msg["params"]["input"][0]["text"]
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req, "result": {"turn": {"id": "turn-1"}}}) + "\n"); sys.stdout.flush()
        emit("item/agentMessage/delta", {"threadId": thread_id, "turnId": "turn-1", "itemId": "msg-1", "delta": "hello "})
        if "approval" in prompt:
            emit("item/started", {"threadId": thread_id, "turnId": "turn-1", "startedAtMs": 1, "item": {"type": "commandExecution", "id": approval_item, "command": "echo hi", "cwd": "/tmp", "status": "inProgress"}})
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": 0, "method": "item/commandExecution/requestApproval", "params": {"threadId": thread_id, "turnId": "turn-1", "itemId": approval_item, "startedAtMs": 1, "command": "echo hi", "cwd": "/tmp", "availableDecisions": ["accept", "decline", "cancel"]}}) + "\n"); sys.stdout.flush()
            approval_pending = True
        else:
            emit("item/agentMessage/delta", {"threadId": thread_id, "turnId": "turn-1", "itemId": "msg-1", "delta": "world"})
            emit("thread/tokenUsage/updated", {"threadId": thread_id, "turnId": "turn-1", "tokenUsage": {"last": {"inputTokens": 10, "outputTokens": 2, "cachedInputTokens": 3, "cacheWriteInputTokens": 0, "totalTokens": 15}}})
            emit("turn/completed", {"threadId": thread_id, "turn": {"id": "turn-1", "status": "completed", "items": []}})
    elif approval_pending and msg.get("id") == 0:
        decision = (msg.get("result") or {}).get("decision")
        approval_pending = False
        emit("item/completed", {"threadId": thread_id, "turnId": "turn-1", "startedAtMs": 1, "item": {"type": "commandExecution", "id": approval_item, "command": "echo hi", "cwd": "/tmp", "status": "completed", "aggregatedOutput": "hi\n", "exitCode": 0}})
        emit("item/agentMessage/delta", {"threadId": thread_id, "turnId": "turn-1", "itemId": "msg-1", "delta": "approved-" + str(decision)})
        emit("turn/completed", {"threadId": thread_id, "turn": {"id": "turn-1", "status": "completed", "items": []}})
'''


def _fake_binary(tmp_path) -> str:
    path = tmp_path / "codex"
    path.write_text(FAKE)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# Answers the handshake, then reports a server-side turn failure (a bare
# ``error`` method, or a ``turn/completed`` that failed) or dies mid-turn with
# no word at all — the three event shapes the daemon's transport/turn failure
# classification is written against.
FAKE_TURN_FAILURES = r'''#!/usr/bin/env python3
import json, sys

thread_id = "fake-thread"

def emit(method, params):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n")
    sys.stdout.flush()

def reply(req, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    req = msg.get("id")
    if method == "initialize":
        reply(req, {})
    elif method == "thread/start":
        reply(req, {"thread": {"id": thread_id}})
    elif method == "turn/start":
        prompt = msg["params"]["input"][0]["text"]
        reply(req, {"turn": {"id": "turn-1"}})
        if prompt == "server error":
            emit("error", {"error": {"message": "model exploded"}})
        elif prompt == "failed turn":
            emit("turn/completed", {"threadId": thread_id, "turn": {"id": "turn-1", "status": "failed", "error": {"message": "tool ran too long"}}})
        elif prompt == "die early":
            # Answer the RPC, then exit with no event at all: stdout hits EOF
            # and the reader must wake the waiter with a transport error.
            sys.exit(0)
'''


def _fake_turn_failures_binary(tmp_path) -> str:
    path = tmp_path / "codex-failures"
    path.write_text(FAKE_TURN_FAILURES)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


# Answers the handshake, then, per prompt, writes a stdout frame that is not
# JSON at all or a JSON frame that is not an object — protocol corruption the
# reader must turn into a transport failure instead of silently skipping.
FAKE_MALFORMED_FRAMES = r'''#!/usr/bin/env python3
import json, sys

thread_id = "fake-thread"

def reply(req, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req, "result": result}) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    req = msg.get("id")
    if method == "initialize":
        reply(req, {})
    elif method == "thread/start":
        reply(req, {"thread": {"id": thread_id}})
    elif method == "turn/start":
        prompt = msg["params"]["input"][0]["text"]
        reply(req, {"turn": {"id": "turn-1"}})
        if prompt == "garbage now":
            sys.stdout.write("not-json{\n"); sys.stdout.flush(); sys.exit(0)
        elif prompt == "array now":
            sys.stdout.write("[1, 2, 3]\n"); sys.stdout.flush(); sys.exit(0)
'''


def _fake_malformed_binary(tmp_path) -> str:
    path = tmp_path / "codex-malformed"
    path.write_text(FAKE_MALFORMED_FRAMES)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.mark.asyncio
async def test_app_server_streams_real_text_deltas(tmp_path):
    get_approval_broker().clear("thread-a")
    runtime = CodexAppServerRuntime(
        binary=_fake_binary(tmp_path), cwd=str(tmp_path), broker_thread_id="thread-a"
    )
    events = [
        event
        async for event in runtime.stream_turn("hello", cwd=str(tmp_path))
    ]
    assert [event.text for event in events if event.kind == "text_delta"] == [
        "hello ",
        "world",
    ]
    usage = next(event for event in events if event.kind == "usage")
    assert usage.data["cache_read_tokens"] == 3
    assert any(event.kind == "turn_completed" for event in events)
    await runtime.cancel()


@pytest.mark.asyncio
async def test_ticket_developer_instructions_reach_app_server_thread_start():
    runtime = CodexAppServerRuntime(
        developer_instructions="Follow the Ticket delivery contract."
    )
    client = MagicMock()
    client.request = AsyncMock(return_value={"thread": {"id": "thread-ticket"}})
    runtime._client = client

    await runtime._ensure_thread(None, "/tmp/work")

    args = client.request.await_args
    assert args.args[0] == "thread/start"
    assert args.args[1]["developerInstructions"] == (
        "Follow the Ticket delivery contract."
    )
    assert args.args[1]["approvalPolicy"] == "on-request"


@pytest.mark.asyncio
async def test_a_replacement_process_resumes_the_thread_before_the_notice():
    """A new app-server process must thread/resume the id it kept.

    The wake closes the transport and reuses the runtime. The thread id
    survives that close. Skipping resume would send the notice to a process
    that does not have the session.
    """
    runtime = CodexAppServerRuntime()
    first = MagicMock()
    first.request = AsyncMock(return_value={"thread": {"id": "thr-kept"}})
    runtime._client = first
    await runtime._ensure_thread(None, "/tmp/work")
    assert first.request.await_args.args[0] == "thread/start"

    second = MagicMock()
    second.request = AsyncMock(return_value={})
    runtime._client = second
    await runtime._ensure_thread(runtime._thread_id, "/tmp/work")

    assert second.request.await_args.args[0] == "thread/resume"
    assert second.request.await_args.args[1]["threadId"] == "thr-kept"


@pytest.mark.asyncio
async def test_a_constructed_thread_id_is_resumed_on_the_first_client():
    """An id set before any client exists is not a reason to skip resume."""
    runtime = CodexAppServerRuntime(thread_id="thr-existing")
    client = MagicMock()
    client.request = AsyncMock(return_value={})
    runtime._client = client

    await runtime._ensure_thread("thr-existing", "/tmp/work")

    assert client.request.await_args.args[0] == "thread/resume"
    assert client.request.await_args.args[1]["threadId"] == "thr-existing"


@pytest.mark.asyncio
async def test_the_same_app_server_process_does_not_resume_twice():
    runtime = CodexAppServerRuntime()
    client = MagicMock()
    client.request = AsyncMock(return_value={"thread": {"id": "thr-1"}})
    runtime._client = client

    await runtime._ensure_thread(None, "/tmp/work")
    await runtime._ensure_thread("thr-1", "/tmp/work")

    assert client.request.await_count == 1


@pytest.mark.asyncio
async def test_app_server_parks_process_until_approval_resume(tmp_path):
    get_approval_broker().clear("thread-b")
    runtime = CodexAppServerRuntime(
        binary=_fake_binary(tmp_path), cwd=str(tmp_path), broker_thread_id="thread-b"
    )
    first = [
        event
        async for event in runtime.stream_turn("run approval command", cwd=str(tmp_path))
    ]
    assert [event.kind for event in first[-2:]] == ["tool_started", "approval_requested"]
    pending = get_approval_broker().get("thread-b")
    assert pending is not None
    assert pending.interrupt_payload["action_requests"][0]["name"] == "mcp__codex__command"

    second = [
        event
        async for event in pending.runtime.resume_turn(
            {"interrupt-id": {"decisions": [{"type": "approve"}]}}
        )
    ]
    assert [event.text for event in second if event.kind == "text_delta"] == [
        "approved-accept"
    ]
    assert get_approval_broker().get("thread-b") is None
    await runtime.cancel()


@pytest.mark.asyncio
async def test_app_server_runtime_pauses_graph_for_approval(tmp_path):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from src.server.services.native_runtime.contracts import NativeRuntimeSpec
    from src.server.services.native_runtime.graph import build_native_graph

    get_approval_broker().clear("thread-graph")
    runtime = CodexAppServerRuntime(
        binary=_fake_binary(tmp_path), cwd=str(tmp_path), broker_thread_id="thread-graph"
    )
    graph = build_native_graph(
        runtime=runtime,
        spec=NativeRuntimeSpec(provider="codex"),
        checkpointer=MemorySaver(),
        thread_id="thread-graph",
    )
    config = {"configurable": {"thread_id": "thread-graph"}}
    first = await graph.ainvoke(
        {"messages": [HumanMessage(content="run approval command")]}, config=config
    )
    assert first.get("__interrupt__")
    pending = get_approval_broker().get("thread-graph")
    assert pending is not None
    second = await graph.ainvoke(
        Command(resume={"iid": {"decisions": [{"type": "approve"}]}}), config=config
    )
    projection = second["messages"][-1]
    assert isinstance(projection, AIMessage)
    assert "approved-accept" in projection.content


def test_config_overrides_are_passed_before_app_server(tmp_path):
    from src.server.services.native_runtime.codex_app_server import CodexAppServerRuntime

    runtime = CodexAppServerRuntime(
        binary="codex",
        cwd=str(tmp_path),
        config_overrides={"features.multi_agent": "false"},
    )
    argv = runtime._argv()
    assert argv[:3] == [runtime.binary, "-c", "features.multi_agent=false"]
    assert argv[3:6] == ["app-server", "--listen", "stdio://"]


def test_ticket_isolation_uses_supported_app_server_config_override(tmp_path):
    from src.server.services.native_runtime.registry import get_runtime

    runtime = get_runtime(
        "codex",
        transport="app-server",
        binary="codex",
        cwd=str(tmp_path),
        isolate_user_instructions=True,
    )
    argv = runtime._argv()
    assert "--ignore-user-config" not in argv
    assert argv[:3] == ["codex", "-c", "project_doc_max_bytes=0"]
    # P0: the memories feature is off for a Ticket run too.
    assert argv[3:9] == [
        "-c", "features.memories=false",
        "-c", "memories.use_memories=false",
        "-c", "memories.generate_memories=false",
    ]
    assert argv[9:12] == ["app-server", "--listen", "stdio://"]


def test_ordinary_app_server_does_not_override_project_instructions(tmp_path):
    from src.server.services.native_runtime.registry import get_runtime

    runtime = get_runtime(
        "codex", transport="app-server", binary="codex", cwd=str(tmp_path)
    )
    assert "project_doc_max_bytes=0" not in runtime._argv()


@pytest.mark.asyncio
async def test_finalizing_old_stream_does_not_close_a_resumed_client(tmp_path):
    from src.server.services.native_runtime.approvals import get_approval_broker

    get_approval_broker().clear("thread-resume-guard")
    runtime = CodexAppServerRuntime(
        binary=_fake_binary(tmp_path), cwd=str(tmp_path), broker_thread_id="thread-resume-guard"
    )
    first = [
        event
        async for event in runtime.stream_turn("run approval command", cwd=str(tmp_path))
    ]
    assert first[-1].kind == "approval_requested"
    pending = get_approval_broker().get("thread-resume-guard")
    assert pending is not None
    client = runtime._client
    assert client is not None

    # The old turn generator may be finalized only after resume_turn has
    # started (it sets _parked=False). That must not close the shared client.
    runtime._resuming = True
    runtime._parked = False
    await runtime._finish_or_park()
    assert runtime._client is client
    runtime._resuming = False
    await runtime.cancel()


@pytest.mark.asyncio
async def test_app_server_reader_failure_wakes_event_waiter():
    from src.server.services.native_runtime.codex_app_server import (
        _AppServerClient,
        _READER_CLOSED,
    )
    from src.llms.native_cli.base import NativeCliError

    client = _AppServerClient([], cwd=None)
    await client._incoming.put(_READER_CLOSED)
    with pytest.raises(NativeCliError, match="reader stopped"):
        await client.next_event()


@pytest.mark.asyncio
async def test_app_server_turn_deadline_is_enforced():
    from src.server.services.native_runtime.codex_app_server import (
        CodexAppServerRuntime,
    )
    from src.llms.native_cli.base import NativeCliError

    runtime = CodexAppServerRuntime(binary="fake-codex", timeout_seconds=10)
    runtime._client = object()  # type: ignore[assignment]
    runtime._turn_deadline = 0.0

    with pytest.raises(NativeCliError, match="timed out"):
        async for _event in runtime._iter_turn():
            pass


@pytest.mark.asyncio
async def test_app_server_initialization_respects_the_turn_budget(tmp_path):
    from src.llms.native_cli.base import NativeCliError

    slow = tmp_path / "codex"
    slow.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(30)\n")
    slow.chmod(slow.stat().st_mode | stat.S_IEXEC)
    runtime = CodexAppServerRuntime(
        binary=str(slow), cwd=str(tmp_path), timeout_seconds=1.0
    )
    with pytest.raises(NativeCliError):
        async for _event in runtime.stream_turn("hello", cwd=str(tmp_path)):
            pass
    await runtime.cancel()


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [("server error", "model exploded"), ("failed turn", "tool ran too long")],
)
@pytest.mark.asyncio
async def test_app_server_reports_turn_failures_as_error_events(
    tmp_path, prompt, expected
):
    """A server-side failure surfaces as one ``error`` event, then the stream ends."""
    runtime = CodexAppServerRuntime(
        binary=_fake_turn_failures_binary(tmp_path), cwd=str(tmp_path),
        timeout_seconds=10.0,
    )
    events = [
        event async for event in runtime.stream_turn(prompt, cwd=str(tmp_path))
    ]
    assert [(event.kind, event.text) for event in events] == [("error", expected)]
    await runtime.cancel()


@pytest.mark.asyncio
async def test_app_server_reader_death_raises_a_transport_error(tmp_path):
    from src.llms.native_cli.base import NativeCliError

    runtime = CodexAppServerRuntime(
        binary=_fake_turn_failures_binary(tmp_path), cwd=str(tmp_path),
        timeout_seconds=10.0,
    )
    with pytest.raises(NativeCliError, match="reader stopped"):
        async for _event in runtime.stream_turn("die early", cwd=str(tmp_path)):
            pass
    await runtime.cancel()


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [("garbage now", "malformed JSON frame"), ("array now", "non-object JSON frame")],
)
@pytest.mark.asyncio
async def test_app_server_malformed_frames_are_transport_failures(tmp_path, prompt, expected):
    from src.llms.native_cli.base import NativeCliError

    runtime = CodexAppServerRuntime(
        binary=_fake_malformed_binary(tmp_path), cwd=str(tmp_path), timeout_seconds=10.0
    )
    with pytest.raises(NativeCliError, match=expected) as captured:
        async for _event in runtime.stream_turn(prompt, cwd=str(tmp_path)):
            pass
    # The surfaced transport error must not carry the raw frame.
    assert "not-json" not in str(captured.value)
    assert "[1, 2, 3]" not in str(captured.value)
    await runtime.cancel()
