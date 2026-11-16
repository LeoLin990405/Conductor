"""A real _delegate call lands in the unified orchestration registry."""

import pytest

from ptc_agent.agent.tools import native_agent
from src.llms.native_cli.base import NativeCliResult
from src.server.services import orchestration


@pytest.mark.asyncio
async def test_delegate_registers_and_completes_a_native_worker(monkeypatch, tmp_path):
    async def fake_run(_adapter, prompt, **_kwargs):
        return NativeCliResult(provider="kimi", text=f"done: {prompt}")

    monkeypatch.setattr(native_agent, "_stream_delegation_providers", lambda: set())
    monkeypatch.setattr(native_agent, "_credential_kwargs", lambda *_a, **_k: _async({}))
    monkeypatch.setattr(native_agent, "get_adapter", lambda *_a, **_k: object())
    monkeypatch.setattr(native_agent, "run_with_session", fake_run)
    monkeypatch.setattr(native_agent, "_workspace_for", lambda _thread: str(tmp_path))

    out = await native_agent._delegate(
        prompt="summarize this",
        provider="kimi",
        thread="t-orch-native",
        sessions=native_agent.NativeCliSessionStore(tmp_path / "sessions.json"),
        mcp_servers=[{"name": "conductor_memory"}, {"name": "conductor_skills"}],
    )
    assert out == "done: summarize this"
    rows = await orchestration.snapshot("t-orch-native")
    assert rows[0]["kind"] == "native"
    assert rows[0]["provider"] == "kimi"
    assert rows[0]["status"] == "completed"
    assert rows[0]["meta"]["memory"] is True
    assert rows[0]["meta"]["skills"] is True


async def _async(value):
    return value


@pytest.mark.asyncio
async def test_cancel_handle_stops_a_running_native_worker(monkeypatch, tmp_path):
    import asyncio

    async def slow_run(_adapter, _prompt, **_kwargs):
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(native_agent, "_stream_delegation_providers", lambda: set())
    monkeypatch.setattr(native_agent, "_credential_kwargs", lambda *_a, **_k: _async({}))
    monkeypatch.setattr(native_agent, "get_adapter", lambda *_a, **_k: object())
    monkeypatch.setattr(native_agent, "run_with_session", slow_run)
    monkeypatch.setattr(native_agent, "_workspace_for", lambda _thread: str(tmp_path))

    task = asyncio.create_task(native_agent._delegate(
        prompt="long task",
        provider="kimi",
        thread="t-orch-cancel",
        sessions=native_agent.NativeCliSessionStore(tmp_path / "sessions.json"),
        mcp_servers=[],
    ))
    worker_id = ""
    for _ in range(50):
        rows = await orchestration.snapshot("t-orch-cancel")
        if rows:
            worker_id = rows[0]["id"]
            break
        await asyncio.sleep(0.01)
    assert worker_id, "worker was not registered"
    assert await orchestration.cancel_worker(worker_id) is True
    out = await task
    assert "cancelled by user" in out
    rows = await orchestration.snapshot("t-orch-cancel")
    assert rows[0]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_codex_hitl_registers_a_runtime_cancel_handle(monkeypatch, tmp_path):
    monkeypatch.setattr(native_agent, "_workspace_for", lambda _thread: str(tmp_path))
    import asyncio
    import langgraph.types

    from src.server.services import orchestration
    from src.server.services.native_runtime import approvals

    class FakeRuntime:
        def __init__(self):
            self.cancelled = False
            self.key = ""

        async def cancel(self):
            self.cancelled = True

        def resume_turn(self, _response):
            return _EmptyStream()

    class _EmptyStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    runtime = FakeRuntime()
    key = native_agent._codex_hitl_key("t-codex", "run it")
    runtime.key = key
    async def noop_loop(**_kwargs):
        await asyncio.sleep(30)

    def fake_interrupt(_payload):
        return {}

    approvals.get_approval_broker().park(
        approvals.PendingNativeApproval(
            thread_id=key,
            runtime=runtime,
            request_id=1,
            approval_id="a1",
            kind="exec",
            interrupt_payload={},
        )
    )
    monkeypatch.setattr(langgraph.types, "interrupt", fake_interrupt)
    monkeypatch.setattr(native_agent, "_run_codex_hitl_loop", noop_loop)

    sessions = native_agent.NativeCliSessionStore(tmp_path / "sessions.json")
    task = asyncio.create_task(native_agent._run_codex_hitl_inner(
        prompt="run it", thread="t-codex", sessions=sessions,
        mcp_servers=[], lane="main", worker_id="w-codex",
    ))
    for _ in range(50):
        if await orchestration.cancel_worker("w-codex"):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("cancel handle was not registered")
    assert runtime.cancelled is True
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        approvals.get_approval_broker().clear(key)
