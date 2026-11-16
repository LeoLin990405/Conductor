import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.llms.native_cli import zcode
from src.llms.native_cli.base import NativeCliError
from src.llms.native_cli.process import ProcessOutput
from src.server.services.native_runtime.zcode_stream import stream_zcode_turn

from src.server.services.native_runtime.zcode_stream import parse_zcode_stream_line


def _line(payload):
    return json.dumps({"type": "model.streaming", "sessionId": "sess-1", "payload": payload})


def test_zcode_text_and_thinking_deltas():
    assert parse_zcode_stream_line(_line({"kind": "text_delta", "delta": "hello"}))[0].text == "hello"
    assert parse_zcode_stream_line(_line({"kind": "reasoning_delta", "delta": "think"}))[0].kind == "thinking_delta"


def test_zcode_tool_start_result_and_usage():
    started = parse_zcode_stream_line(
        json.dumps({
            "type": "tool.updated",
            "sessionId": "sess-1",
            "payload": {
                "kind": "started",
                "toolCallId": "c1",
                "toolName": "Write",
                "input": {"file_path": "/tmp/x"},
            },
        })
    )
    assert started[0].kind == "tool_started"
    result = parse_zcode_stream_line(
        json.dumps({
            "type": "tool.updated",
            "sessionId": "sess-1",
            "payload": {
                "kind": "result",
                "toolCallId": "c1",
                "toolName": "Write",
                "result": {"success": True, "content": "ok"},
            },
        })
    )
    assert result[0].kind == "tool_result"
    usage = parse_zcode_stream_line(
        json.dumps({
            "type": "turn.completed",
            "sessionId": "sess-1",
            "payload": {
                "resultType": "success",
                "usage": {"inputTokens": 10, "outputTokens": 2, "totalTokens": 12},
            },
        })
    )
    assert [event.kind for event in usage] == ["usage", "turn_completed"]
    assert usage[0].data["total_tokens"] == 12


def test_zcode_stream_argv_uses_stream_json():
    from src.llms.native_cli.zcode import ZCodeCliAdapter

    invocation = ZCodeCliAdapter(binary="/opt/zcode").build_invocation(
        "hello", cwd="/tmp", stream=True
    )
    assert invocation.argv[invocation.argv.index("--output-format") + 1] == "stream-json"


def test_zcode_permission_event():
    event = parse_zcode_stream_line(
        json.dumps({
            "type": "permission.requested",
            "sessionId": "sess-1",
            "payload": {
                "requestId": "perm-1",
                "toolName": "Write",
                "input": {"file_path": "/tmp/x"},
                "reason": "requires approval",
                "options": [{"optionId": "allow_once"}],
            },
        })
    )[0]
    assert event.kind == "approval_requested"
    assert event.data["request_id"] == "perm-1"
    assert event.data["tool_name"] == "Write"


def test_zcode_approval_patch_replaces_broker():
    from src.server.services.native_runtime.zcode_approval import (
        _BRIDGE_CLASS,
        _DENY_CLASS,
        _patched_source,
    )

    patched = _patched_source("prefix;" + _DENY_CLASS + ";suffix")
    assert _DENY_CLASS not in patched
    assert _BRIDGE_CLASS in patched
    assert "CONDUCTOR_ZCODE_PERMISSION_DIR" in patched


def test_zcode_approvals_capability_is_opt_in(monkeypatch):
    from src.server.services.native_runtime import registry

    monkeypatch.delenv("NATIVE_CLI_ZCODE_APPROVALS", raising=False)
    assert registry.status_for("zcode").capabilities.approvals is False
    monkeypatch.setenv("NATIVE_CLI_ZCODE_APPROVALS", "1")
    status = registry.status_for("zcode")
    assert status.capabilities.approvals is True
    assert status.capabilities.streaming is True


def test_zcode_approval_binary_cache_is_hash_keyed(tmp_path, monkeypatch):
    from src.server.services.native_runtime.zcode_approval import (
        _DENY_CLASS,
        ensure_approval_binary,
    )

    source = tmp_path / "zcode.cjs"
    source.write_text("prefix;" + _DENY_CLASS + ";suffix")
    monkeypatch.setenv("NATIVE_CLI_ZCODE_APPROVAL_DIR", str(tmp_path / "cache"))
    first = ensure_approval_binary(str(source))
    assert first.is_file()
    source.write_text("prefix;" + _DENY_CLASS + ";// upgraded")
    second = ensure_approval_binary(str(source))
    assert second != first
    assert second.is_file()


_RESULT_LINE = json.dumps({"type": "result", "response": "ok", "sessionId": "s1"})


def _isolated(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(zcode, "_LOCK_PATH", tmp_path / "zcode.lock")
    return home


@pytest.mark.asyncio
async def test_concurrent_streams_each_see_only_their_own_mcp_servers(
    monkeypatch, tmp_path
):
    home = _isolated(monkeypatch, tmp_path)
    user_config = home / ".zcode" / "cli" / "config.json"
    seen: list[tuple[str, ...]] = []

    async def fake_stream(_invocation):
        data = json.loads(user_config.read_text()) if user_config.is_file() else {}
        seen.append(tuple(sorted(data.get("mcp", {}).get("servers", {}))))
        await asyncio.sleep(0.05)
        yield _RESULT_LINE

    monkeypatch.setattr(
        "src.server.services.native_runtime.zcode_stream.stream_process", fake_stream
    )
    adapters = [
        zcode.ZCodeCliAdapter(
            binary="/opt/zcode",
            mcp_servers=[{"name": name, "url": f"https://{name}.example.test/mcp"}],
        )
        for name in ("alpha", "beta")
    ]

    async def drain(adapter):
        return [
            event
            async for event in stream_zcode_turn(
                adapter, "hi", session_id=None, cwd=str(tmp_path / "w")
            )
        ]

    await asyncio.gather(*(drain(a) for a in adapters))

    assert sorted(seen) == [("alpha",), ("beta",)]
    assert not user_config.exists()


@pytest.mark.asyncio
async def test_queued_call_writes_its_provider_config_under_the_lock(
    monkeypatch, tmp_path
):
    _isolated(monkeypatch, tmp_path)
    provider = tmp_path / "provider.json"
    monkeypatch.setattr(zcode, "_personal_provider_path", lambda: provider)
    models: list[str] = []

    async def fake_run(_invocation, **_kwargs):
        await asyncio.sleep(0.02)
        models.append(json.loads(provider.read_text())["config"]["defaultModelSelection"]["modelId"])
        await asyncio.sleep(0.05)
        return ProcessOutput(stdout=_RESULT_LINE, stderr="", returncode=0)

    monkeypatch.setattr(zcode, "run_process", fake_run)
    adapters = [
        zcode.ZCodeCliAdapter(binary="/opt/zcode", model=m, api_key="dummy")
        for m in ("model-a", "model-b")
    ]
    await asyncio.gather(*(a.run("hi") for a in adapters))

    assert sorted(models) == ["model-a", "model-b"]


def _lock_is_free(lock_path: Path) -> bool:
    """Whether the provider lock can be taken right now."""
    import fcntl
    import os

    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False
    finally:
        os.close(descriptor)


async def _drain_stream(adapter, cwd):
    return [
        event
        async for event in stream_zcode_turn(
            adapter, "hi", session_id=None, cwd=cwd
        )
    ]


@pytest.mark.asyncio
async def test_the_stream_shares_one_budget_with_the_lock_wait(
    monkeypatch, tmp_path
):
    """A queued stream does not get a second full timeout.

    The wait for the provider lock is part of the turn, so the process run
    gets only what is left of the turn's single budget. Handing the run a
    fresh full timeout would let a queued turn take twice the timeout it
    was given.
    """
    import fcntl
    import os

    _isolated(monkeypatch, tmp_path)
    lock_path = tmp_path / "zcode.lock"
    seen: list[float] = []

    async def fake_stream(invocation):
        seen.append(invocation.timeout_seconds)
        yield _RESULT_LINE

    monkeypatch.setattr(
        "src.server.services.native_runtime.zcode_stream.stream_process", fake_stream
    )
    adapter = zcode.ZCodeCliAdapter(binary="/opt/zcode-cli", timeout_seconds=1.0)

    # Another turn holds the provider lock, so this one has to wait for it.
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def release_soon():
        await asyncio.sleep(0.2)
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    releaser = asyncio.ensure_future(release_soon())
    try:
        await asyncio.wait_for(
            _drain_stream(adapter, str(tmp_path / "w")), timeout=10
        )
    finally:
        await releaser

    assert seen, "the CLI was never launched"
    # The wait spent part of the budget, so the run gets less than all of it.
    assert 0 < seen[0] < 0.9, seen[0]


@pytest.mark.asyncio
async def test_a_stream_whose_lock_wait_used_up_its_budget_never_runs(
    monkeypatch, tmp_path
):
    """The turn has one budget, and the lock wait is part of it.

    A stream that waited out its whole budget in the queue fails instead of
    launching a CLI that would then run for another full timeout.
    """
    import fcntl
    import os

    _isolated(monkeypatch, tmp_path)
    lock_path = tmp_path / "zcode.lock"
    launched: list[bool] = []

    async def fake_stream(_invocation):
        launched.append(True)
        yield _RESULT_LINE

    monkeypatch.setattr(
        "src.server.services.native_runtime.zcode_stream.stream_process", fake_stream
    )
    adapter = zcode.ZCodeCliAdapter(binary="/opt/zcode-cli", timeout_seconds=0.1)

    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(NativeCliError, match="timed out"):
            await asyncio.wait_for(
                _drain_stream(adapter, str(tmp_path / "w")), timeout=10
            )
        assert not launched
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _approval_runtime(monkeypatch, tmp_path, stdout_lines):
    """A ZCode approval runtime whose CLI process is a fake."""
    from src.server.services.native_runtime import zcode_approval

    monkeypatch.setattr(
        zcode_approval, "ensure_approval_binary", lambda binary: Path(binary)
    )
    binary = tmp_path / "zcode"
    binary.write_text("fake zcode binary")
    stdout = asyncio.StreamReader()
    for line in stdout_lines:
        stdout.feed_data((line + "\n").encode())
    stdout.feed_eof()
    stderr = asyncio.StreamReader()
    stderr.feed_eof()
    process = SimpleNamespace(
        stdout=stdout, stderr=stderr, wait=AsyncMock(return_value=0)
    )
    runtime = zcode_approval.ZCodeApprovalRuntime(
        binary=str(binary), cwd=str(tmp_path), broker_thread_id="thread-approval"
    )
    return runtime, process


@pytest.mark.asyncio
async def test_an_errored_approval_turn_releases_the_provider_lock(
    monkeypatch, tmp_path
):
    """A turn that ends in an error must give the provider lock back.

    The lock is held for the whole turn, so a turn whose error event ends
    the stream has to release it. Otherwise every later ZCode call on the
    host waits for a lock that nobody will ever free.
    """
    from src.server.services.native_runtime.graph import _consume_event
    from src.server.services.native_runtime.contracts import NativeRuntimeError

    _isolated(monkeypatch, tmp_path)
    lock_path = tmp_path / "zcode.lock"
    runtime, process = _approval_runtime(
        monkeypatch,
        tmp_path,
        [
            json.dumps(
                {
                    "type": "turn.completed",
                    "sessionId": "s1",
                    "payload": {"resultType": "error"},
                }
            )
        ],
    )

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=process)
        )
        stream = runtime.stream_turn("hi", cwd=str(tmp_path / "w"))
        event = await anext(stream)
        assert event.kind == "error"
        with pytest.raises(NativeRuntimeError):
            await _consume_event(
                event,
                writer=lambda *_: None,
                text_parts=[],
                message_id="m1",
                tool_projection=[],
                tool_calls_seen=set(),
                on_usage=None,
            )
        await stream.aclose()

    assert _lock_is_free(lock_path)


@pytest.mark.asyncio
async def test_a_paused_approval_turn_keeps_the_lock_until_it_resumes(
    monkeypatch, tmp_path
):
    """An approval pause keeps the lock; the resumed turn gives it back.

    The CLI process is still alive while it waits for a human decision and
    still needs this turn's config, so the lock stays held for the pause.
    Releasing it there would let another turn overwrite the config under a
    live process.
    """
    from src.server.services.native_runtime.approvals import get_approval_broker

    _isolated(monkeypatch, tmp_path)
    lock_path = tmp_path / "zcode.lock"
    runtime, process = _approval_runtime(
        monkeypatch,
        tmp_path,
        [
            json.dumps(
                {
                    "type": "permission.requested",
                    "sessionId": "s1",
                    "payload": {
                        "requestId": "perm-1",
                        "toolName": "Write",
                        "input": {"file_path": "/tmp/x"},
                        "reason": "requires approval",
                    },
                }
            )
        ],
    )
    broker = get_approval_broker()
    broker.clear("thread-approval")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(
            "asyncio.create_subprocess_exec", AsyncMock(return_value=process)
        )
        stream = runtime.stream_turn("hi", cwd=str(tmp_path / "w"))
        event = await anext(stream)
        assert event.kind == "approval_requested"
        # The turn is paused, so the lock is still held.
        assert not _lock_is_free(lock_path)

        resumed = [
            item
            async for item in runtime.resume_turn({"decision": "allow"})
        ]
        assert resumed == []

    # The resumed turn is over, so the lock is free again.
    assert _lock_is_free(lock_path)
    broker.clear("thread-approval")
