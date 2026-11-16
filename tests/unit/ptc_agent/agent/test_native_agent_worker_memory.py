"""P0: a Codex delegation a Worker-role turn starts gets no operator memory.

``NativeAgent`` ran Codex under the operator's real ``~/.codex``, so a PTC
Worker could reach the operator's Codex memories through it. A Worker-role
turn now builds Codex with the memory-off flags and an isolated CODEX_HOME;
the operator's own turns are unchanged.
"""

from __future__ import annotations

import pytest

from ptc_agent.agent.tools import native_agent
from src.llms.native_cli.base import NativeCliResult


async def _async(value):
    return value


async def _delegate_codex(monkeypatch, tmp_path, *, worker_role: bool, mcp: list[dict]):
    seen: list[dict] = []

    def fake_get_adapter(name, **kwargs):
        seen.append({"name": name, **kwargs})
        return object()

    async def fake_run(_adapter, prompt, **_kwargs):
        return NativeCliResult(provider="codex", text="ok")

    monkeypatch.setattr(native_agent, "_stream_delegation_providers", lambda: set())
    monkeypatch.setattr(native_agent, "_credential_kwargs", lambda *_a, **_k: _async({}))
    monkeypatch.setattr(native_agent, "get_adapter", fake_get_adapter)
    monkeypatch.setattr(native_agent, "run_with_session", fake_run)
    monkeypatch.setattr(native_agent, "_workspace_for", lambda _thread: str(tmp_path))
    await native_agent._delegate(
        prompt="look at this",
        provider="codex",
        thread=f"t-{worker_role}-{len(mcp)}",
        sessions=native_agent.NativeCliSessionStore(tmp_path / "sessions.json"),
        mcp_servers=mcp,
        worker_role=worker_role,
    )
    return seen[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp", [[], [{"name": "conductor_skills"}]])
async def test_a_worker_codex_delegation_isolates_personal_memory(monkeypatch, tmp_path, mcp):
    kwargs = await _delegate_codex(monkeypatch, tmp_path, worker_role=True, mcp=mcp)
    assert kwargs["isolate_personal_memory"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("mcp", [[], [{"name": "conductor_skills"}]])
async def test_the_operators_own_codex_delegation_is_unchanged(monkeypatch, tmp_path, mcp):
    kwargs = await _delegate_codex(monkeypatch, tmp_path, worker_role=False, mcp=mcp)
    assert "isolate_personal_memory" not in kwargs


@pytest.mark.asyncio
async def test_a_worker_hitl_codex_delegation_uses_an_isolated_app_server(monkeypatch, tmp_path):
    import langgraph.types  # noqa: F401  (imported by the inner runner)

    from src.server.services.native_runtime import codex_app_server

    built: list[dict] = []

    class FakeRuntime:
        def __init__(self, **kwargs):
            built.append(kwargs)

        def stream_turn(self, *_a, **_k):
            return self

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def cancel(self):
            return None

    async def fake_loop(**_kwargs):
        return "ok"

    monkeypatch.setattr(codex_app_server, "CodexAppServerRuntime", FakeRuntime)
    monkeypatch.setattr(native_agent, "_run_codex_hitl_loop", fake_loop)
    monkeypatch.setattr(native_agent, "_workspace_for", lambda _thread: str(tmp_path))
    sessions = native_agent.NativeCliSessionStore(tmp_path / "sessions.json")
    for worker_role in (True, False):
        await native_agent._run_codex_hitl_inner(
            prompt=f"run it {worker_role}",
            thread="t-hitl",
            sessions=sessions,
            mcp_servers=[],
            lane="main",
            worker_role=worker_role,
        )
    assert [kw["isolate_personal_memory"] for kw in built] == [True, False]


def test_the_agent_marks_worker_turns_for_native_delegation():
    import inspect

    from ptc_agent.agent import agent

    source = inspect.getsource(agent)
    assert 'worker_role=role == "worker"' in source


@pytest.mark.asyncio
async def test_streamed_codex_runs_under_the_isolated_home_and_removes_it(monkeypatch, tmp_path):
    """The stream path builds its own process, so it must isolate too."""
    import json
    from pathlib import Path

    from src.llms.native_cli.codex import CodexExecAdapter
    from src.server.services.native_runtime import codex_stream

    source = tmp_path / "operator-codex"
    (source / "memories").mkdir(parents=True)
    (source / "auth.json").write_text('{"synthetic": "x"}\n')
    monkeypatch.setenv("CODEX_HOME", str(source))
    seen: list[dict] = []

    async def fake_stream(invocation):
        home = (invocation.env or {}).get("CODEX_HOME")
        seen.append({"home": home, "memories": bool(home) and (Path(home) / "memories").exists()})
        yield json.dumps({"type": "thread.started", "thread_id": "s-1"})

    monkeypatch.setattr(codex_stream, "stream_process", fake_stream)
    adapter = CodexExecAdapter(binary="codex", isolate_personal_memory=True)
    events = [e async for e in codex_stream.stream_codex_turn(adapter, "hi", session_id=None, cwd=None)]
    assert events
    assert seen[0]["home"] and seen[0]["home"] != str(source)
    assert seen[0]["memories"] is False
    assert not Path(seen[0]["home"]).exists()

    seen.clear()
    plain = CodexExecAdapter(binary="codex")
    [e async for e in codex_stream.stream_codex_turn(plain, "hi", session_id=None, cwd=None)]
    assert seen[0]["home"] is None
