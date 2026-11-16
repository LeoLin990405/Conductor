"""Delegate one self-contained task to a native agent CLI.

This is the first merge point for the native-CLI layer: the model gets a tool
that runs an official coding-agent CLI as an external backend (the pattern DSH
uses for Codex/Claude Code/ACP). It is deliberately not a chat-completions
transport: the CLI owns its tools, session and prompt cache, and returns one
final answer.

The feature is off unless ``NATIVE_CLI_ENABLED`` is set; the tool never runs in
a subagent's roster because it is appended after the parent tool snapshot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from src.llms.native_cli import (
    NativeCliError,
    NativeCliSessionStore,
    get_adapter,
    run_with_session,
)

DEFAULT_PROVIDER = "deepseek-dsh"


def _auto_candidates(
    prompt: str, *, headroom: dict[str, str] | None = None
) -> tuple[list[str], str]:
    """Ordered live native providers for an auto delegation.

    The router's fallbacks are already filtered to installed/live candidates;
    PTC is deliberately excluded because this tool is a native delegation.
    """
    from src.server.services.native_runtime.registry import list_statuses
    from src.server.services.native_runtime.router import RuntimeRequest, route_runtime

    decision = route_runtime(
        list_statuses(),
        RuntimeRequest(task=prompt, allow_ptc=False),
        headroom=headroom,
    )
    if decision.runtime in {"ptc", "none"}:
        return [], decision.reason
    candidates = [decision.runtime, *decision.fallbacks]
    return [name for name in candidates if name not in {"ptc", "manus"}], decision.reason


async def _auto_candidates_async(prompt: str) -> tuple[list[str], str]:
    from src.server.services.native_runtime.usage_headroom import load_usage_headroom

    return _auto_candidates(prompt, headroom=await load_usage_headroom())


def _auto_provider(prompt: str) -> tuple[str | None, str]:
    candidates, reason = _auto_candidates(prompt)
    return (candidates[0] if candidates else None), reason



def native_cli_enabled() -> bool:
    return os.getenv("NATIVE_CLI_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


def _workspace_for(thread_id: str) -> str:
    """A stable private cwd per thread, so native sessions and caches align."""
    base = Path(
        os.getenv(
            "NATIVE_CLI_WORKSPACE_ROOT",
            str(Path.home() / ".cache" / "conductor" / "native-cli" / "workspaces"),
        )
    )
    safe = "".join(ch for ch in (thread_id or "default") if ch.isalnum() or ch in "-_")
    path = base / (safe or "default")
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


async def _native_mcp_servers(
    *,
    thread_id: str,
    user_id: str | None,
    workspace_id: str | None,
) -> list[dict]:
    """Conductor's explicit bridges for a delegated native-agent call.

    Only the secret-less builtin servers plus the memory/skills bridge are
    forwarded. This is the same bridge contract as a native thread; the
    delegation tool must not silently invent a different tool surface.
    """
    if not user_id:
        return []
    try:
        from src.server.services.native_runtime.mcp import (
            NativeMCPServer,
            collect_builtin_mcp_servers,
        )
    except Exception:
        return []
    servers: list[dict] = []
    try:
        builtin = await collect_builtin_mcp_servers(
            user_id=user_id, workspace_id=workspace_id
        )
        servers.extend(server.as_dict() for server in builtin)
    except Exception:
        pass
    try:
        from src.server.services.native_runtime.memory_token import issue_memory_token

        repo_root = Path(__file__).resolve().parents[4]
        capability_env = (
            (
                "CONDUCTOR_API_BASE",
                os.environ.get("NATIVE_MEMORY_API_BASE", "http://127.0.0.1:8000"),
            ),
            (
                "CONDUCTOR_MEMORY_TOKEN",
                issue_memory_token(
                    user_id, thread_id, workspace_id=workspace_id
                ),
            ),
        )
        for name, script in (
            ("conductor_memory", "native_memory_mcp_server.py"),
            ("conductor_skills", "native_skills_mcp_server.py"),
        ):
            servers.insert(
                0,
                NativeMCPServer(
                    name=name,
                    command="uv",
                    args=(
                        "run",
                        "--directory",
                        str(repo_root),
                        "python",
                        str(repo_root / "mcp_servers" / script),
                    ),
                    env=capability_env,
                ).as_dict(),
            )
    except Exception:
        pass
    return servers


def _progress(payload: dict) -> None:
    """Best-effort custom-stream progress for a native delegation.

    The PTC graph's custom stream is converted to SSE by the producer. Outside
    a graph (unit tests, scripts) ``get_stream_writer`` is absent; progress is
    optional and never changes the tool result.
    """
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
        if writer is not None:
            writer(payload)
    except Exception:
        return


def _session_key(thread: str, provider: str, lane: str) -> str:
    if lane == "main":
        return f"thread:{thread}:{provider}"
    return f"thread:{thread}:{lane}:{provider}"


async def _credential_kwargs(provider: str, user_id: str | None) -> dict:
    """Provider credentials the CLI needs when Conductor owns the key.

    MiniMax/MiMo do not necessarily have a logged-in CLI account; the native
    thread lane resolves the user's Conductor key. Delegation must do the same or
    the adapter silently falls back to an unauthenticated CLI.
    """
    if not user_id:
        return {}
    try:
        from src.server.services.native_runtime.credentials import (
            mimo_api_key,
            minimax_api_key,
        )
    except Exception:
        return {}
    if provider == "minimax":
        key = await minimax_api_key(user_id)
        return {"api_key": key, "model": "MiniMax-M3"} if key else {}
    if provider == "mimo":
        key, base = await mimo_api_key(user_id)
        return {"api_key": key, "base_url": base, "model": "xiaomi/mimo-v2.6-pro"} if key else {}
    return {}


async def _request_approval(
    prompt: str, candidates: list[str], *, batch_size: int = 1
) -> bool:
    """Ask the user before a native delegation runs.

    This is the PTC graph's existing HITL interrupt, not a vendor approval. It
    gates the whole delegation; per-command approvals inside the CLI remain a
    separate, unsolved surface.
    """
    from langgraph.types import interrupt

    # ``interrupt`` raises GraphInterrupt to pause the graph; it must propagate
    # so the existing HITL pipeline can checkpoint and show the card.
    response = interrupt({
        "action_requests": [{
            "name": "NativeAgent",
            "args": {
                "providers": candidates,
                "batch_size": batch_size,
                "prompt": prompt[:500],
            },
            "description": (
                f"Run {batch_size} native agent task(s) with "
                f"{', '.join(candidates)}?"
            ),
        }]
    })
    decisions = response.get("decisions", []) if isinstance(response, dict) else []
    if not decisions:
        return False
    first = decisions[0] if isinstance(decisions[0], dict) else {}
    return str(first.get("type") or "").lower() in {"approve", "approved", "accept"}


_STREAMING_PROVIDERS = {"codex", "deepseek-dsh", "minimax"}


def _approval_required(explicit: bool) -> bool:
    if explicit:
        return True
    return os.getenv("NATIVE_CLI_REQUIRE_APPROVAL", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def _stream_delegation_providers() -> set[str]:
    """Providers allowed to use the streaming delegation path.

    ``NATIVE_CLI_STREAM_DELEGATION`` accepts ``1/all/true`` or a comma-separated
    provider allowlist, so a host can gray-launch one CLI at a time.
    """
    raw = os.getenv("NATIVE_CLI_STREAM_DELEGATION", "").strip().lower()
    if raw in {"1", "true", "yes", "on", "all", "*"}:
        return set(_STREAMING_PROVIDERS)
    return {
        part.strip() for part in raw.split(",")
        if part.strip() in _STREAMING_PROVIDERS
    }


async def _run_streamed(
    adapter: Any,
    provider: str,
    prompt: str,
    *,
    session_id: str | None,
    cwd: str | None,
    lane: str,
) -> tuple[str, str | None, dict]:
    """Run a stream-capable adapter and forward its tool events as progress.

    The vendor still owns its internal loop; these events are surfaced to the
    main chat as native_agent tool notifications. Text is collected into the
    final tool result rather than emitted token-by-token.
    """
    from src.server.services.native_runtime.codex_stream import stream_codex_turn
    from src.server.services.native_runtime.dsh_stream import stream_dsh_turn
    from src.server.services.native_runtime.minimax_stream import stream_minimax_turn

    streamer = {
        "codex": stream_codex_turn,
        "deepseek-dsh": stream_dsh_turn,
        "minimax": stream_minimax_turn,
    }[provider]
    parts: list[str] = []
    active_session = session_id
    usage: dict = {}
    async for event in streamer(
        adapter, prompt, session_id=session_id, cwd=cwd
    ):
        if event.session_id:
            active_session = event.session_id
        if event.kind == "text_delta" and event.text:
            parts.append(event.text)
        elif event.kind == "tool_started":
            _progress({
                "type": "native_agent",
                "phase": "tool",
                "provider": provider,
                "lane": lane,
                "tool_name": event.tool_name or "tool",
                "tool_status": "started",
            })
        elif event.kind == "tool_result":
            _progress({
                "type": "native_agent",
                "phase": "tool",
                "provider": provider,
                "lane": lane,
                "tool_name": event.tool_name or "tool",
                "tool_status": "completed",
            })
        elif event.kind == "usage":
            usage = dict(event.data or {})
    text = "".join(parts).strip()
    if not text:
        raise NativeCliError(f"{provider} produced no final answer")
    return text, active_session, usage


def _codex_hitl_key(thread: str, prompt: str) -> str:
    digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]
    return f"{thread}:nativeagent:{digest}"


async def _delegate_codex_hitl(
    *,
    prompt: str,
    thread: str,
    sessions: NativeCliSessionStore,
    mcp_servers: list[dict],
    lane: str,
    worker_id: str = "",
    cancel_state: dict[str, bool] | None = None,
    timeout_seconds: float = 300.0,
    worker_role: bool = False,
) -> str:
    """Delegate to Codex app-server with real per-command HITL.

    The parked app-server process lives in the native approval broker under a
    stable (thread, prompt) key. On a LangGraph interrupt the tool re-executes,
    finds the parked runtime, asks interrupt() for the resume value, and sends
    it back before continuing the same Codex turn.
    """

    from src.server.services.native_runtime.approvals import get_approval_broker

    try:
        async with asyncio.timeout(max(5.0, min(float(timeout_seconds), 900.0))):
            return await _run_codex_hitl_inner(
                prompt=prompt,
                thread=thread,
                sessions=sessions,
                mcp_servers=mcp_servers,
                lane=lane,
                worker_id=worker_id,
                cancel_state=cancel_state,
                worker_role=worker_role,
            )
    except TimeoutError as exc:
        # A timed-out command-approval turn may still have a parked app-server
        # process. Close it so the broker and its MCP children do not leak.
        pending = get_approval_broker().get(_codex_hitl_key(thread, prompt))
        if pending is not None:
            try:
                await pending.runtime.cancel()
            finally:
                get_approval_broker().clear(pending.thread_id)
        raise NativeCliError("codex command-approval delegation timed out") from exc


async def _run_codex_hitl_inner(
    *,
    prompt: str,
    thread: str,
    sessions: NativeCliSessionStore,
    mcp_servers: list[dict],
    lane: str,
    worker_id: str = "",
    cancel_state: dict[str, bool] | None = None,
    worker_role: bool = False,
) -> str:
    from langgraph.types import interrupt

    from src.server.services.native_runtime.approvals import get_approval_broker
    from src.server.services.native_runtime.codex_app_server import CodexAppServerRuntime

    key = _codex_hitl_key(thread, prompt)
    broker = get_approval_broker()
    session_key = _session_key(thread, "codex", lane)
    cwd = _workspace_for(thread)
    parts: list[str] = []
    usage: dict = {}
    active_session = sessions.get(session_key)
    pending = broker.get(key)
    if pending is not None:
        parts.extend(pending.text_parts)
        runtime = pending.runtime
        response = interrupt(pending.interrupt_payload)
        stream = runtime.resume_turn(response)
    else:
        runtime = CodexAppServerRuntime(
            cwd=cwd,
            broker_thread_id=key,
            mcp_servers=mcp_servers,
            approval_policy="on-request",
            sandbox="read-only",
            # Keep the delegated turn in the main thread: Codex's internal
            # multi-agent handoff runs the command on a child thread whose
            # completion events are not on this client's subscription.
            config_overrides={"features.multi_agent": "false"},
            developer_instructions=(
                "Run commands directly in this thread. Do not delegate work to "
                "subagents or internal helper agents."
            ),
            # P0: a delegation a Worker started gets no operator memory.
            isolate_personal_memory=worker_role,
        )
        stream = runtime.stream_turn(
            prompt, session_id=active_session, cwd=cwd
        )

    from src.server.services.orchestration import register_cancel, unregister_cancel

    async def _cancel_codex() -> None:
        if cancel_state is not None:
            cancel_state["cancelled"] = True
        try:
            await runtime.cancel()
        finally:
            broker.clear(key)

    register_cancel(worker_id, _cancel_codex)
    try:
        return await _run_codex_hitl_loop(
            stream=stream,
            runtime=runtime,
            broker=broker,
            key=key,
            session_key=session_key,
            sessions=sessions,
            lane=lane,
            parts=parts,
            usage=usage,
            active_session=active_session,
        )
    finally:
        unregister_cancel(worker_id)


async def _run_codex_hitl_loop(
    *,
    stream,
    runtime,
    broker,
    key: str,
    session_key: str,
    sessions: NativeCliSessionStore,
    lane: str,
    parts: list[str],
    usage: dict,
    active_session: str | None = None,
) -> str:
    from langgraph.types import interrupt

    while True:
        approval = None
        async for event in stream:
            if event.session_id:
                active_session = event.session_id
            if event.kind == "approval_requested":
                approval = event
                break
            if event.kind == "text_delta" and event.text:
                parts.append(event.text)
            elif event.kind == "tool_started":
                _progress({
                    "type": "native_agent",
                    "phase": "tool",
                    "provider": "codex",
                    "lane": lane,
                    "tool_name": event.tool_name or "tool",
                    "tool_status": "started",
                })
            elif event.kind == "tool_result":
                _progress({
                    "type": "native_agent",
                    "phase": "tool",
                    "provider": "codex",
                    "lane": lane,
                    "tool_name": event.tool_name or "tool",
                    "tool_status": "completed",
                })
            elif event.kind == "usage":
                usage = dict(event.data or {})
        if approval is None:
            break
        pending = broker.get(key)
        if pending is None:
            raise NativeCliError("codex requested approval without a parked turn")
        response = interrupt(pending.interrupt_payload)
        stream = runtime.resume_turn(response)

    text = "".join(parts).strip()
    if not text:
        raise NativeCliError("codex produced no final answer")
    if active_session:
        sessions.set(session_key, active_session)
    _progress({
        "type": "native_agent",
        "phase": "completed",
        "provider": "codex",
        "lane": lane,
        "cache_read_tokens": usage.get("cache_read_tokens", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    })
    return text


async def _run_cancelable(worker_id: str, coro, *, on_cancel=None):
    """Run one CLI/remote call in its own task and publish a cancel handle.

    The orchestration API runs in a different request task; cancelling this
    child kills the local CLI process tree, while ``on_cancel`` stops a remote
    task (for example Manus ``task.stop``) before the child is cancelled.
    """
    import asyncio as _asyncio
    import inspect

    from src.server.services.orchestration import register_cancel, unregister_cancel

    task = _asyncio.create_task(coro)

    def _cancel() -> None:
        if on_cancel is not None:
            result = on_cancel()
            if inspect.isawaitable(result):
                _asyncio.create_task(result)
        task.cancel()

    if worker_id:
        register_cancel(worker_id, _cancel)
    try:
        return await task
    finally:
        unregister_cancel(worker_id)


async def _delegate(
    *,
    prompt: str,
    provider: str,
    thread: str,
    sessions: NativeCliSessionStore,
    mcp_servers: list[dict],
    user_id: str | None = None,
    lane: str = "main",
    timeout_seconds: float | None = None,
    require_approval: bool = False,
    require_command_approval: bool = False,
    worker_role: bool = False,
) -> str:
    """Run one delegation, trying auto candidates in order.

    ``worker_role``: the calling turn is a Worker (another vendor's model on
    delegated work), so a Codex delegation runs without the operator's
    personal memory (memory-off flags and an isolated CODEX_HOME).
    """
    requested = (provider or "").strip()
    provider = requested
    candidates: list[str] = []
    reason = ""
    if provider == "auto":
        candidates, reason = await _auto_candidates_async(prompt)
        if not candidates:
            return f"NativeAgent (auto) failed: {reason}"
    elif not provider:
        candidates = [os.getenv("NATIVE_CLI_PROVIDER", DEFAULT_PROVIDER).strip()]
    else:
        candidates = [provider]

    if require_approval and not await _request_approval(prompt, candidates):
        return "NativeAgent rejected by user"
    failures: list[str] = []

    async def _orchestration_start(candidate: str) -> str:
        try:
            from src.server.services.orchestration import WorkerKind, start_worker

            bridge_names = {
                str(server.get("name") if isinstance(server, dict) else getattr(server, "name", ""))
                for server in (mcp_servers or [])
            }
            return await start_worker(
                thread_id=thread,
                kind=WorkerKind.NATIVE,
                provider=candidate,
                label=f"Native · {candidate}",
                prompt=prompt,
                meta={
                    "lane": lane,
                    "requested": requested,
                    "memory": "conductor_memory" in bridge_names,
                    "skills": "conductor_skills" in bridge_names,
                },
            )
        except Exception:  # noqa: BLE001 - orchestration telemetry must not block a run
            return ""

    async def _orchestration_finish(
        worker_id: str, status: str, *, result: str = "", error: str = ""
    ) -> None:
        if not worker_id:
            return
        try:
            from src.server.services.orchestration import WorkerStatus, finish_worker

            await finish_worker(
                worker_id,
                status=WorkerStatus(status),
                result=result,
                error=error,
            )
        except Exception:  # noqa: BLE001
            pass

    from src.server.services.native_runtime.registry import disabled_provider_reason

    for candidate in candidates:
        disabled_reason = disabled_provider_reason(candidate)
        if disabled_reason:
            failures.append(f"{candidate}: {disabled_reason}")
            if provider != "auto":
                break
            continue
        worker_id = await _orchestration_start(candidate)
        cancel_state: dict[str, bool] = {}
        _progress({
            "type": "native_agent",
            "phase": "started",
            "provider": candidate,
            "lane": lane,
            "prompt_chars": len(prompt),
        })
        try:
            if require_command_approval and candidate == "codex":
                text = await _delegate_codex_hitl(
                    prompt=prompt,
                    thread=thread,
                    sessions=sessions,
                    mcp_servers=mcp_servers,
                    lane=lane,
                    worker_id=worker_id,
                    cancel_state=cancel_state,
                    timeout_seconds=timeout_seconds or 300.0,
                    worker_role=worker_role,
                )
                if cancel_state.get("cancelled"):
                    await _orchestration_finish(worker_id, "cancelled", error="cancelled by user")
                    return "Native worker codex cancelled by user."
                await _orchestration_finish(worker_id, "completed", result=text)
                return text
            adapter_kwargs: dict = {}
            if timeout_seconds is not None:
                adapter_kwargs["timeout_seconds"] = max(5.0, min(float(timeout_seconds), 900.0))
            adapter_kwargs.update(await _credential_kwargs(candidate, user_id))
            if candidate in {"codex", "deepseek-dsh", "minimax", "mimo"} and mcp_servers:
                kwargs: dict = {"mcp_servers": mcp_servers, **adapter_kwargs}
                if candidate == "codex":
                    # The delegation runs in its own private cwd; workspace-write
                    # is required for MCP bridges that persist memory/skills.
                    # ``approve-for-me`` lets Codex's automatic reviewer approve
                    # MCP tool calls in this non-interactive delegation; it is
                    # not a user-facing HITL prompt.
                    kwargs["read_only"] = False
                    kwargs["approve_for_me"] = True
                    if worker_role:
                        kwargs["isolate_personal_memory"] = True
                try:
                    adapter = get_adapter(candidate, **kwargs)
                except TypeError:
                    if worker_role and candidate == "codex":
                        raise
                    adapter = get_adapter(candidate)
            else:
                if worker_role and candidate == "codex":
                    adapter_kwargs["isolate_personal_memory"] = True
                adapter = get_adapter(candidate, **adapter_kwargs) if adapter_kwargs else get_adapter(candidate)
            session_key = _session_key(thread, candidate, lane)
            if candidate in _stream_delegation_providers():
                text, result_session, usage = await _run_cancelable(
                    worker_id,
                    on_cancel=getattr(adapter, "cancel_remote", None),
                    coro=_run_streamed(
                        adapter,
                        candidate,
                        prompt,
                        session_id=sessions.get(session_key),
                        cwd=_workspace_for(thread),
                        lane=lane,
                    ),
                )
                if result_session:
                    sessions.set(session_key, result_session)
                _progress({
                    "type": "native_agent",
                    "phase": "completed",
                    "provider": candidate,
                    "lane": lane,
                    "cache_read_tokens": usage.get("cache_read_tokens", 0),
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                })
                await _orchestration_finish(worker_id, "completed", result=text)
                return text
            result = await _run_cancelable(
                worker_id,
                on_cancel=getattr(adapter, "cancel_remote", None),
                coro=run_with_session(
                    adapter,
                    prompt,
                    session_key=session_key,
                    store=sessions,
                    cwd=_workspace_for(thread),
                ),
            )
            _progress({
                "type": "native_agent",
                "phase": "completed",
                "provider": candidate,
                "lane": lane,
                "cache_read_tokens": result.usage.cache_read_tokens,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
            })
            await _orchestration_finish(worker_id, "completed", result=result.text)
            return result.text
        except asyncio.CancelledError:
            await _orchestration_finish(worker_id, "cancelled", error="cancelled by user")
            _progress({
                "type": "native_agent",
                "phase": "cancelled",
                "provider": candidate,
                "lane": lane,
            })
            return f"Native worker {candidate} cancelled by user."
        except NativeCliError as exc:
            if cancel_state.get("cancelled"):
                await _orchestration_finish(worker_id, "cancelled", error="cancelled by user")
                return "Native worker codex cancelled by user."
            await _orchestration_finish(worker_id, "failed", error=str(exc))
            _progress({
                "type": "native_agent",
                "phase": "failed",
                "provider": candidate,
                "lane": lane,
                "error": str(exc)[:300],
            })
            failures.append(f"{candidate}: {exc}")
            if provider != "auto":
                break
    if provider == "auto":
        label = "auto->" + "->".join(candidates)
    else:
        label = candidates[0] if candidates else (provider or "unknown")
    return f"NativeAgent ({label}) failed: " + "; ".join(failures)


def _parse_tasks(tasks: list[dict] | str) -> list[dict]:
    if isinstance(tasks, str):
        try:
            tasks = json.loads(tasks)
        except json.JSONDecodeError as exc:
            raise NativeCliError(f"tasks is not valid JSON: {exc}") from exc
    if not isinstance(tasks, list) or not tasks:
        raise NativeCliError("tasks must be a non-empty list")
    clean: list[dict] = []
    for task in tasks:
        if isinstance(task, dict):
            prompt = str(task.get("prompt") or "").strip()
            provider = str(task.get("provider") or "").strip()
        else:
            prompt, provider = str(task or "").strip(), ""
        if prompt:
            timeout = task.get("timeout_seconds") if isinstance(task, dict) else None
            depends_on = task.get("depends_on") if isinstance(task, dict) else []
            if depends_on is None:
                depends_on = []
            if not isinstance(depends_on, list):
                raise NativeCliError("depends_on must be a list of earlier indexes")
            deps: list[int] = []
            for dep in depends_on:
                if isinstance(dep, bool) or not isinstance(dep, int):
                    raise NativeCliError("depends_on entries must be integers")
                if dep < 0 or dep >= len(clean):
                    raise NativeCliError(
                        f"depends_on {dep} must reference an earlier task index"
                    )
                if dep not in deps:
                    deps.append(dep)
            clean.append({
                "prompt": prompt,
                "provider": provider,
                "timeout_seconds": timeout,
                "depends_on": deps,
            })
    if not clean:
        raise NativeCliError("no task had a non-empty prompt")
    return clean[:4]


def _format_batch(results: list[tuple[str, str]], tasks: list[dict]) -> str:
    sections: list[str] = []
    for index, (task, (label, text)) in enumerate(zip(tasks, results), start=1):
        sections.append(f"### Native task {index} [{label}]\n{text}")
    return "\n\n".join(sections)


def create_native_agent_tools(
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
    worker_role: bool = False,
):
    """Build the single and parallel main-agent delegation tools.

    ``worker_role`` marks a Worker-role turn: its Codex delegations run
    without the operator's personal memory. The operator's own turns keep
    the default.
    """
    thread = thread_id or "default"
    sessions = NativeCliSessionStore()
    mcp_servers = None

    async def _mcp() -> list[dict]:
        nonlocal mcp_servers
        if mcp_servers is None:
            mcp_servers = await _native_mcp_servers(
                thread_id=thread,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        return mcp_servers

    @tool("NativeAgent", parse_docstring=True)
    async def native_agent(
        prompt: str,
        provider: str = "",
        timeout_seconds: int | None = None,
        require_approval: bool = False,
        require_command_approval: bool = False,
    ) -> str:
        """Delegate a self-contained task to a native coding-agent CLI.

        Use for tasks where the native agent's own harness is the point — a
        second opinion, a long isolated research pass, or work that benefits
        from that CLI's native prompt cache. It does not see this conversation
        or your files: put every fact, path and output requirement in
        `prompt`. When the backend has explicit MCP/memory/skills bridges they
        are forwarded to the CLI, not invented here. It may take minutes. Do
        not delegate a step you can finish with one direct tool call.

        Args:
            prompt: The complete, self-contained task and expected output.
            provider: Optional native agent id (deepseek-dsh, codex,
                claude-code, cursor, kimi, zcode, minimax, mimo, grok,
                stepfun), or ``auto`` to use the capability-aware router.
                Empty uses the deployment default. The CLI and its account
                must already be installed and authenticated on the backend
                host.
            timeout_seconds: Optional per-call timeout (5-900 seconds).
            require_approval: Ask the user through the PTC HITL card before
                starting the delegation. This gates the whole call, not each
                command the CLI runs.
            require_command_approval: For Codex only, run the bidirectional
                app-server and pause for each command/file approval it requests.
                This is real per-command HITL; other providers ignore it.
        """
        return await _delegate(
            prompt=prompt,
            provider=provider,
            thread=thread,
            sessions=sessions,
            mcp_servers=await _mcp(),
            user_id=user_id,
            timeout_seconds=timeout_seconds,
            require_approval=_approval_required(require_approval),
            require_command_approval=require_command_approval,
            worker_role=worker_role,
        )

    @tool("NativeAgents", parse_docstring=True)
    async def native_agents(
        tasks: list[dict] | str, require_approval: bool = False
    ) -> str:
        """Run up to four independent native-agent tasks in parallel.

        Use only for genuinely independent workstreams. Each task gets its own
        isolated native session and private cwd; results are returned in input
        order. A failed task reports its own error instead of failing the batch.

        Args:
            tasks: A list of ``{"prompt": "...", "provider": "auto",
                "depends_on": [0]}`` objects (or a JSON string with the same
                shape). Provider may be a native id or ``auto``; omit it for
                the deployment default. ``depends_on`` references earlier
                indexes; dependents run only after their prerequisites finish.
        """
        try:
            clean = _parse_tasks(tasks)
        except NativeCliError as exc:
            return f"NativeAgents failed: {exc}"
        if _approval_required(require_approval):
            providers = [
                str(task.get("provider") or "default") for task in clean
            ]
            if not await _request_approval("", providers, batch_size=len(clean)):
                return "NativeAgents rejected by user"
        servers = await _mcp()
        try:
            configured_limit = int(os.getenv("NATIVE_CLI_MAX_PARALLEL", "4") or 4)
        except ValueError:
            configured_limit = 4
        limit = max(1, min(configured_limit, 4))

        async def run_one(index: int, task: dict) -> tuple[str, str]:
            label = task["provider"] or "default"
            prompt = task["prompt"]
            if task.get("depends_on"):
                context = "\n\n".join(
                    f"[task {dep} result]\n{results[dep][1]}"
                    for dep in task["depends_on"]
                )
                if context:
                    prompt = f"{prompt}\n\n{context}"
            text = await _delegate(
                prompt=prompt,
                provider=task["provider"],
                thread=thread,
                sessions=sessions,
                mcp_servers=servers,
                user_id=user_id,
                lane=f"batch{index}",
                timeout_seconds=task.get("timeout_seconds"),
                worker_role=worker_role,
            )
            return label, text

        # Results are invocation-local execution context. Start newly-ready
        # tasks as soon as their own prerequisites settle, without waiting for
        # unrelated branches to finish.
        results: dict[int, tuple[str, str]] = {}
        pending = set(range(len(clean)))
        active: dict[asyncio.Task, int] = {}
        try:
            while pending or active:
                ready = [
                    index
                    for index in sorted(pending)
                    if all(dep in results for dep in clean[index]["depends_on"])
                ]
                while ready and len(active) < limit:
                    index = ready.pop(0)
                    pending.remove(index)
                    active[asyncio.create_task(run_one(index, clean[index]))] = index

                if not active:
                    break  # unreachable with validated backward-only dependencies

                done, _ = await asyncio.wait(
                    active, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    index = active.pop(task)
                    try:
                        results[index] = task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        results[index] = (
                            clean[index]["provider"] or "default",
                            f"failed: {exc}",
                        )
        finally:
            # If the parent turn is cancelled, don't leave native CLI work
            # detached from the tool invocation.
            for task in active:
                task.cancel()
            if active:
                await asyncio.gather(*active, return_exceptions=True)
        normalized = [
            results.get(
                index,
                (clean[index]["provider"] or "default", "failed: dependency cycle"),
            )
            for index in range(len(clean))
        ]
        return _format_batch(normalized, clean)

    return native_agent, native_agents


def create_native_agent_tool(
    *,
    thread_id: str | None = None,
    user_id: str | None = None,
    workspace_id: str | None = None,
):
    """Backward-compatible single-tool factory."""
    return create_native_agent_tools(
        thread_id=thread_id, user_id=user_id, workspace_id=workspace_id
    )[0]
