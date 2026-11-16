"""StepFun RPC runtime with real per-tool HITL approvals.

`step --mode rpc` is a JSON command/event protocol. Tool approval is an
`extension_ui_request` with method `confirm`; the client answers with
`extension_ui_response`. This runtime keeps the process alive across the
native graph interrupt, exactly like the ACP/Codex app-server lanes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from src.llms.native_cli.base import NativeCliError
from src.llms.native_cli.process import kill_child_group, merged_env, spawn_group
from src.llms.native_cli.stepfun import StepFunAdapter
from src.server.services.native_runtime.approvals import (
    PendingNativeApproval,
    get_approval_broker,
)
from src.server.services.native_runtime.contracts import (
    NativeCapabilities,
    NativeEvent,
    chunk_text,
)

logger = logging.getLogger(__name__)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "text"
        )
    return ""


def _usage(message: Mapping[str, Any]) -> dict[str, int]:
    usage = message.get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    return {
        "input_tokens": int(usage.get("input") or 0),
        "output_tokens": int(usage.get("output") or 0),
        "cache_read_tokens": int(usage.get("cacheRead") or 0),
        "cache_write_tokens": int(usage.get("cacheWrite") or 0),
        "total_tokens": int(usage.get("totalTokens") or 0),
    }


class StepFunRpcRuntime:
    provider = "stepfun"

    def __init__(
        self,
        *,
        cwd: str | None = None,
        broker_thread_id: str | None = None,
        binary: str | None = None,
        model: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        **_unused: Any,
    ) -> None:
        adapter = StepFunAdapter(binary=binary, read_only=False, cwd=cwd, model=model)
        self._binary = adapter.binary
        self._cwd = cwd
        self._model = model
        self._broker_key = broker_thread_id
        self._broker = get_approval_broker()
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._session_id: str | None = None
        self._parked = False

    def capabilities(self) -> NativeCapabilities:
        return NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            approvals=True,
            workspace_files=True,
            cancel=True,
        )

    async def _start(self, prompt: str, session_id: str | None, cwd: str | None) -> None:
        argv = [
            self._binary,
            "--mode",
            "rpc",
            "--approval-mode",
            "confirm",
            "--non-interactive-approval",
            "deny",
        ]
        if self._model:
            argv.extend(["--model", self._model])
        effective_session = session_id or self._session_id
        if effective_session:
            argv.extend(["--session-id", effective_session])
        try:
            self._process = await spawn_group(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd if cwd is not None else self._cwd,
                env=merged_env(None),
            )
        except FileNotFoundError as exc:
            raise NativeCliError("step CLI not found") from exc
        assert self._process.stdin is not None and self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._send({"id": "alpha-prompt", "type": "prompt", "message": prompt})

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            logger.debug("[STEPFUN_RPC] %s", line.decode("utf-8", "replace")[:1000])

    def _send(self, payload: Mapping[str, Any]) -> None:
        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write((json.dumps(dict(payload)) + "\n").encode())
        # RPC writes are tiny; drain lazily through the event loop.
        asyncio.create_task(self._process.stdin.drain())

    def _approval_event(self, raw: Mapping[str, Any]) -> NativeEvent:
        title = str(raw.get("title") or "StepFun tool approval")
        message = str(raw.get("message") or "")
        return NativeEvent(
            kind="approval_requested",
            text=title,
            data={
                "approval_id": str(raw.get("id") or ""),
                "kind": "tool_approval",
                "title": title,
                "message": message,
                "options": [],
            },
        )

    async def _events(self) -> AsyncIterator[NativeEvent]:
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                break
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, Mapping):
                continue
            kind = str(raw.get("type") or "")
            if kind == "extension_ui_request":
                if raw.get("method") == "confirm":
                    assert self._broker_key
                    request_id = str(raw.get("id") or "")
                    pending = PendingNativeApproval(
                        thread_id=self._broker_key,
                        runtime=self,
                        request_id=request_id,
                        approval_id=f"stepfun-{request_id}",
                        kind="tool_approval",
                        interrupt_payload={
                            "action_requests": [{
                                "name": str(raw.get("title") or "stepfun_tool"),
                                "args": {},
                                "description": str(raw.get("message") or ""),
                            }],
                            "kind": "tool_approval",
                        },
                    )
                    self._parked = True
                    self._broker.park(pending)
                    yield self._approval_event(raw)
                    return
                continue
            if kind == "message_update":
                update = raw.get("assistantMessageEvent")
                update = update if isinstance(update, Mapping) else {}
                update_kind = str(update.get("type") or "")
                delta = str(update.get("delta") or "")
                if update_kind == "text_delta" and delta:
                    for chunk in chunk_text(delta):
                        yield NativeEvent(kind="text_delta", text=chunk, session_id=self._session_id)
                elif update_kind == "thinking_delta" and delta:
                    for chunk in chunk_text(delta):
                        yield NativeEvent(kind="thinking_delta", text=chunk, session_id=self._session_id)
                elif update_kind == "toolcall_end":
                    tool = update.get("toolCall")
                    tool = tool if isinstance(tool, Mapping) else {}
                    call_id = str(tool.get("id") or "") or None
                    yield NativeEvent(
                        kind="tool_started",
                        session_id=self._session_id,
                        tool_name=str(tool.get("name") or "tool"),
                        tool_call_id=call_id,
                        data={"id": call_id, "name": str(tool.get("name") or "tool"), "args": dict(tool.get("arguments") or {})},
                    )
                continue
            if kind == "tool_execution_end":
                call_id = str(raw.get("toolCallId") or "") or None
                result = raw.get("result")
                if isinstance(result, Mapping):
                    text = _content_text(result.get("content")) or str(result)[:4000]
                else:
                    text = str(result or "")
                yield NativeEvent(
                    kind="tool_result",
                    session_id=self._session_id,
                    tool_call_id=call_id,
                    data={"id": call_id, "content": text[:20000], "status": "error" if raw.get("isError") else "success"},
                )
                continue
            if kind == "turn_end":
                message = raw.get("message")
                if isinstance(message, Mapping):
                    yield NativeEvent(kind="usage", session_id=self._session_id, data=_usage(message))
                continue
            if kind == "agent_end":
                yield NativeEvent(kind="turn_completed", session_id=self._session_id)
                return

    async def stream_turn(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[NativeEvent]:
        try:
            await self._start(prompt, session_id, cwd)
            async for event in self._events():
                yield event
        finally:
            await self._stop_unless_parked()

    def _approved(self, response: Any) -> bool:
        if isinstance(response, Mapping):
            entries = list(response.values())
            value = entries[0] if entries and isinstance(entries[0], Mapping) else response
            decisions = value.get("decisions") if isinstance(value, Mapping) else None
            if isinstance(decisions, list) and decisions:
                first = decisions[0]
                if isinstance(first, Mapping):
                    return str(first.get("type") or "").lower() in {"approve", "approved", "accept"}
        return False

    async def resume_turn(self, response: Any) -> AsyncIterator[NativeEvent]:
        assert self._broker_key
        pending = self._broker.get(self._broker_key)
        if pending is None:
            raise NativeCliError("StepFun approval resume has no parked request")
        self._parked = False
        self._broker.clear(self._broker_key)
        self._send({
            "type": "extension_ui_response",
            "id": pending.request_id,
            "confirmed": self._approved(response),
        })
        try:
            async for event in self._events():
                yield event
        finally:
            await self._stop_unless_parked()

    async def _stop_unless_parked(self) -> None:
        """Stop the agent when the turn ends any way but a pause for approval.

        A completed turn, a cancelled task, an error and an abandoned stream
        (the graph closes it on an error event) all end here: this runtime lives
        for one turn, a later turn builds a new one, and nothing else holds the
        process, so nothing else would stop it. A completed turn needs it most:
        the RPC server does not exit after ``agent_end``, it idles on stdin.
        """
        if not self._parked:
            await self._finish()

    async def _finish(self) -> None:
        self._parked = False
        try:
            process = self._process
            if process is not None:
                # EOF on stdin is how an RPC server is told to exit; without it
                # the grace wait below would always run out and end in a kill.
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except (OSError, RuntimeError):
                        pass  # the pipe is already gone
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    kill_child_group(process)
                    await process.wait()
                except BaseException:
                    # A cancel during the grace wait is the last chance to stop it.
                    kill_child_group(process)
                    raise
                finally:
                    self._process = None
        finally:
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                self._stderr_task = None

    async def cancel(self) -> None:
        # A leader that already exited left a group id that may be free by now,
        # and this runtime can have been parked for hours.
        if self._process is not None and self._process.returncode is None:
            kill_child_group(self._process)
            try:
                await self._process.wait()
            except Exception:
                pass
        if self._broker_key:
            self._broker.clear(self._broker_key)
        await self._finish()
