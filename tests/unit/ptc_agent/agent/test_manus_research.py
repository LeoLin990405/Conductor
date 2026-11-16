"""ManusResearch is an information-retrieval tool, not a provider."""

import asyncio

import pytest

from ptc_agent.agent.tools import manus_research
from src.llms.native_cli.base import NativeCliResult
from src.server.services import orchestration


def test_manus_research_enabled_uses_secure_key_availability_gate(monkeypatch):
    calls = []
    monkeypatch.setattr(
        manus_research,
        "manus_api_key_available",
        lambda: calls.append("check") or True,
    )

    monkeypatch.setenv("MANUS_ENABLED", "true")
    assert manus_research.manus_research_enabled() is True
    assert calls == ["check"]

    monkeypatch.setenv("MANUS_ENABLED", "false")
    assert manus_research.manus_research_enabled() is False
    assert calls == ["check"]

    monkeypatch.setenv("MANUS_ENABLED", "1")
    monkeypatch.setattr(manus_research, "manus_api_key_available", lambda: False)
    assert manus_research.manus_research_enabled() is False


@pytest.mark.asyncio
async def test_manus_research_registers_direct_worker(monkeypatch):
    class FakeAdapter:
        def __init__(self, **_kwargs):
            self.last_task_url = "https://manus.im/app/task-1"

        async def run(self, prompt):
            assert prompt == "research topic"
            return NativeCliResult(provider="manus", text="cited answer", session_id="task-1")

        async def get_credit_balance(self):
            return {"amount": 12, "currency": "CREDITS"}

        async def cancel_remote(self):
            pass

    monkeypatch.setattr(manus_research, "ManusAdapter", FakeAdapter)
    tool = manus_research.create_manus_research_tool(thread_id="t-manus-tool")
    out = await tool.ainvoke({"prompt": "research topic", "timeout_seconds": 60})
    assert "cited answer" in out
    assert "https://manus.im/app/task-1" in out
    rows = await orchestration.snapshot("t-manus-tool")
    assert rows[0]["kind"] == "direct"
    assert rows[0]["provider"] == "manus"
    assert rows[0]["meta"]["role"] == "research"
    assert rows[0]["meta"]["balance_state"] == "available"
    assert rows[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_manus_research_rejects_coding_task_before_adapter(monkeypatch):
    class FakeAdapter:
        def __init__(self, **_kwargs):
            pytest.fail("adapter must not be constructed for coding tasks")

    monkeypatch.setattr(manus_research, "ManusAdapter", FakeAdapter)
    tool = manus_research.create_manus_research_tool(thread_id="t-manus-rejected")
    out = await tool.ainvoke({"prompt": "Implement a Python function to parse CSV"})
    assert "only available for research" in out
    assert await orchestration.snapshot("t-manus-rejected") == []


@pytest.mark.asyncio
async def test_manus_research_refuses_exhausted_balance_before_worker(monkeypatch):
    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        async def get_credit_balance(self):
            return {"amount": 0, "currency": "CREDITS"}

        async def run(self, _prompt):
            pytest.fail("zero balance must refuse before task creation")

    monkeypatch.setattr(manus_research, "ManusAdapter", FakeAdapter)
    tool = manus_research.create_manus_research_tool(thread_id="t-manus-no-credit")
    out = await tool.ainvoke({"prompt": "Research a topic"})
    assert "zero available credits" in out
    assert await orchestration.snapshot("t-manus-no-credit") == []


@pytest.mark.asyncio
async def test_manus_research_failure_is_reported_and_worker_finished(monkeypatch):
    class FakeAdapter:
        def __init__(self, **_kwargs):
            self.last_task_url = None

        async def run(self, _prompt):
            raise RuntimeError("upstream rejected key sk-" "FAKE_SECRET")

        async def get_credit_balance(self):
            return None

        async def cancel_remote(self):
            pass

    monkeypatch.setattr(manus_research, "ManusAdapter", FakeAdapter)
    tool = manus_research.create_manus_research_tool(thread_id="t-manus-failed")
    out = await tool.ainvoke({"prompt": "Research a topic"})

    assert "ManusResearch failed: RuntimeError" in out
    assert "sk-" "FAKE_SECRET" not in out
    rows = await orchestration.snapshot("t-manus-failed")
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "RuntimeError during Manus research"
    assert "sk-" "FAKE_SECRET" not in rows[0]["error"]
    assert rows[0]["meta"]["balance_state"] == "unknown"


@pytest.mark.asyncio
async def test_manus_research_cancellation_cancels_remote_task(monkeypatch):
    started = asyncio.Event()
    remote_cancelled = asyncio.Event()

    class FakeAdapter:
        def __init__(self, **_kwargs):
            self.last_task_url = None

        async def run(self, _prompt):
            started.set()
            await asyncio.Event().wait()

        async def get_credit_balance(self):
            return None

        async def cancel_remote(self):
            remote_cancelled.set()

    monkeypatch.setattr(manus_research, "ManusAdapter", FakeAdapter)
    tool = manus_research.create_manus_research_tool(thread_id="t-manus-cancelled")
    invocation = asyncio.create_task(tool.ainvoke({"prompt": "Research a topic"}))
    await started.wait()

    rows = await orchestration.snapshot("t-manus-cancelled")
    assert await orchestration.cancel_worker(rows[0]["id"]) is True
    out = await invocation
    await remote_cancelled.wait()

    assert "cancelled by user" in out
    rows = await orchestration.snapshot("t-manus-cancelled")
    assert rows[0]["status"] == "cancelled"
    assert rows[0]["meta"]["balance_state"] == "unknown"
