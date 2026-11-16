"""ACP-backed native runtime for CLIs that expose Agent Client Protocol.

DSH (``dsh --profile acp``) and MiMo (``mimo acp``) both speak ACP v1. ACP is
the supported surface for streamed assistant/tool updates and
``session/request_permission``; the old one-shot adapters cannot expose either.

The ACP server owns its own session. A turn either creates a session
(``session/new``) or resumes the persisted one (``session/resume``). On a
permission request the live client/process is parked in the native approval
broker, exactly like the Codex app-server path, and the native graph raises the
existing HITL interrupt.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from collections.abc import AsyncIterator, Mapping
from typing import Any

from src.llms.native_cli.acp import AcpClient
from src.llms.native_cli.base import NativeCliError
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


def _mcp_servers(servers: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for server in servers or []:
        name = str(server.get("name") or "").strip()
        if not name:
            continue
        url = str(server.get("url") or "").strip()
        if url:
            headers = server.get("headers") or {}
            out.append({
                "type": "http",
                "name": name,
                "url": url,
                "headers": [
                    {"name": str(k), "value": str(v)} for k, v in headers.items()
                ],
            })
            continue
        command = str(server.get("command") or "").strip()
        if not command:
            continue
        if not os.path.isabs(command):
            resolved = shutil.which(command)
            if not resolved:
                logger.warning("[ACP] skipping MCP %s: command %r is not absolute", name, command)
                continue
            command = resolved
        env = server.get("env") or {}
        out.append({
            "name": name,
            "command": command,
            "args": [str(arg) for arg in (server.get("args") or [])],
            "env": [{"name": str(k), "value": str(v)} for k, v in env.items()],
        })
    return out


def _text(content: Any) -> str:
    if isinstance(content, Mapping):
        if content.get("type") == "text":
            return str(content.get("text") or "")
        return ""
    if isinstance(content, list):
        return "".join(_text(part) for part in content)
    return str(content or "")


class AcpNativeRuntime:
    provider: str

    def __init__(
        self,
        *,
        provider: str,
        argv: list[str],
        cwd: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        env: Mapping[str, str] | None = None,
        broker_thread_id: str | None = None,
        model_config: str | None = None,
        config_options: Mapping[str, str] | None = None,
        approval_policy: str = "on-request",
        **_unused: Any,
    ) -> None:
        self.provider = provider
        self._argv = list(argv)
        self._cwd = cwd
        self._mcp_servers = _mcp_servers(mcp_servers)
        self._env = dict(env or {})
        self._broker_key = broker_thread_id
        self._model_config = model_config
        self._config_options = dict(config_options or {})
        self._approval_policy = approval_policy
        self._client: AcpClient | None = None
        self._session_id: str | None = None
        self._broker = get_approval_broker()
        self._parked = False
        self._prompt_task: asyncio.Task[dict[str, Any]] | None = None

    def capabilities(self) -> NativeCapabilities:
        return NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            approvals=True,
            mcp=bool(self._mcp_servers),
            workspace_files=True,
            cancel=True,
        )

    async def _ensure(self, cwd: str | None, session_id: str | None) -> None:
        # The session id belongs to the process that created it. Closing the
        # transport drops the client and keeps the id; the next turn is a new
        # process and must initialize and resume that id. Returning here
        # would send the notice to a process that never resumed the session.
        client_is_new = self._client is None
        if client_is_new:
            self._client = AcpClient(self._argv, cwd=cwd or self._cwd, env=self._env)
            await self._client.start()
        if self._session_id is not None and not client_is_new:
            return
        retained = self._session_id or session_id
        initialized = await self._client.request(
            "initialize",
            {"protocolVersion": 1, "clientCapabilities": {}},
            timeout=30,
        )
        if not initialized:
            raise NativeCliError("ACP initialize returned no result")
        params: dict[str, Any] = {
            "cwd": cwd or self._cwd,
            "mcpServers": self._mcp_servers,
        }
        if retained:
            try:
                result = await self._client.request(
                    "session/resume",
                    {"sessionId": retained, **params},
                    timeout=60,
                )
            except NativeCliError:
                logger.warning(
                    "[ACP] resume failed for %s; creating a new session", retained
                )
                result = await self._client.request("session/new", params, timeout=60)
        else:
            result = await self._client.request("session/new", params, timeout=60)
        real_session = str(result.get("sessionId") or retained or "").strip()
        if not real_session:
            raise NativeCliError("ACP session/new returned no sessionId")
        self._session_id = real_session
        if self._model_config:
            await self._client.request(
                "session/set_config_option",
                {
                    "sessionId": real_session,
                    "configId": "model",
                    "value": self._model_config,
                },
                timeout=30,
            )
        for config_id, value in self._config_options.items():
            await self._client.request(
                "session/set_config_option",
                {
                    "sessionId": real_session,
                    "configId": config_id,
                    "value": value,
                },
                timeout=30,
            )

    def _permission_payload(self, params: Mapping[str, Any]) -> dict[str, Any]:
        tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), Mapping) else {}
        title = str(tool_call.get("title") or tool_call.get("kind") or "ACP tool")
        return {
            "action_requests": [{
                "name": str(tool_call.get("toolName") or tool_call.get("title") or "mcp__acp__tool"),
                "args": dict(tool_call.get("rawInput") or tool_call.get("input") or {}),
                "description": title,
            }],
            "kind": "tool_approval",
        }

    def _options(self, params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        options = params.get("options")
        return [option for option in options or [] if isinstance(option, Mapping)]

    def _decision_result(self, response: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        decision = "cancel"
        if isinstance(response, Mapping):
            entries = list(response.values())
            value = entries[0] if entries and isinstance(entries[0], Mapping) else response
            decisions = value.get("decisions") if isinstance(value, Mapping) else None
            if isinstance(decisions, list) and decisions:
                first = decisions[0]
                if isinstance(first, Mapping):
                    decision = str(first.get("type") or "cancel").lower()
        if decision in {"approve", "approved", "accept"}:
            wanted = "allow"
        elif decision in {"reject", "deny", "declined"}:
            wanted = "reject"
        else:
            return {"outcome": {"outcome": "cancelled"}}
        for option in self._options(params):
            kind = str(option.get("kind") or "").lower()
            if wanted == "allow" and kind.startswith("allow"):
                return {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": str(option.get("optionId") or ""),
                    }
                }
            if wanted == "reject" and (kind.startswith("reject") or kind.startswith("deny")):
                return {
                    "outcome": {
                        "outcome": "selected",
                        "optionId": str(option.get("optionId") or ""),
                    }
                }
        # ACP's cancel outcome is the safe fallback when the server gave no
        # matching option.
        return {"outcome": {"outcome": "cancelled"}}

    def _update_events(self, update: Mapping[str, Any]) -> list[NativeEvent]:
        kind = str(update.get("sessionUpdate") or "")
        if kind == "agent_message_chunk":
            text = _text(update.get("content"))
            return [NativeEvent(kind="text_delta", text=chunk) for chunk in chunk_text(text)]
        if kind == "agent_thought_chunk":
            text = _text(update.get("content"))
            return [NativeEvent(kind="thinking_delta", text=chunk) for chunk in chunk_text(text)]
        if kind in {"tool_call", "tool_call_update"}:
            raw = update.get("rawInput") or update.get("input") or {}
            call_id = str(update.get("toolCallId") or "")
            name = str(update.get("title") or update.get("kind") or "tool")
            status = str(update.get("status") or "")
            if kind == "tool_call" or status in {"pending", "in_progress", "running"}:
                return [NativeEvent(
                    kind="tool_started",
                    tool_name=name,
                    tool_call_id=call_id,
                    data={"id": call_id, "name": name, "args": dict(raw) if isinstance(raw, Mapping) else {}},
                )]
            if status in {"completed", "failed", "cancelled"}:
                content = update.get("rawOutput") or update.get("output") or status or ""
                return [NativeEvent(
                    kind="tool_result",
                    tool_name=name,
                    tool_call_id=call_id,
                    data={
                        "id": call_id,
                        "name": name,
                        "content": str(content)[:20000],
                        "status": "error" if status == "failed" else "success",
                    },
                )]
        if kind == "usage_update":
            used = update.get("used")
            size = update.get("size")
            if isinstance(used, int):
                return [NativeEvent(kind="usage", data={"total_tokens": used, "context_tokens": size or 0})]
        return []

    async def _next_event(self) -> tuple[str, Any]:
        assert self._client is not None
        next_task = asyncio.create_task(self._client.next_message())
        waiters: list[asyncio.Task[Any]] = [next_task]
        if self._prompt_task is not None:
            waiters.append(self._prompt_task)
        done, _pending = await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED
        )
        if self._prompt_task is not None and self._prompt_task in done:
            next_task.cancel()
            return ("prompt", await self._prompt_task)
        # A message won the race; the prompt request must keep running.
        return ("message", await next_task)

    async def stream_turn(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[NativeEvent]:
        # The agent is stopped on every way out that is not a parked approval:
        # a cancelled task and an abandoned stream (the graph closes it on an
        # error event) end here too, and nothing else holds the client.
        try:
            await self._ensure(cwd, session_id)
            assert self._client is not None
            self._prompt_task = asyncio.create_task(
                self._client.request(
                    "session/prompt",
                    {
                        "sessionId": self._session_id,
                        "prompt": [{"type": "text", "text": prompt}],
                    },
                    timeout=900,
                )
            )
            async for event in self._consume():
                yield event
        finally:
            await self._finish_or_park()

    async def _consume(self) -> AsyncIterator[NativeEvent]:
        assert self._client is not None
        while True:
            if self._prompt_task is not None and self._prompt_task.done():
                try:
                    await self._prompt_task
                except Exception:
                    pass
                yield NativeEvent(
                    kind="turn_completed", session_id=self._session_id
                )
                await self._finish_or_park()
                return
            kind, value = await self._next_event()
            if kind == "prompt":
                if isinstance(value, Mapping) and value.get("stopReason") == "cancelled":
                    yield NativeEvent(kind="turn_completed", session_id=self._session_id)
                else:
                    yield NativeEvent(kind="turn_completed", session_id=self._session_id)
                await self._finish_or_park()
                return
            message = value
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
            request_id = message.get("id")
            if method == "session/update":
                update = params.get("update")
                if isinstance(update, Mapping):
                    for event in self._update_events(update):
                        if event.session_id is None:
                            event = NativeEvent(
                                kind=event.kind,
                                text=event.text,
                                session_id=self._session_id,
                                tool_name=event.tool_name,
                                tool_call_id=event.tool_call_id,
                                data=event.data,
                                raw=event.raw,
                            )
                        yield event
                continue
            if method == "session/request_permission" and request_id is not None:
                assert self._broker_key
                pending = PendingNativeApproval(
                    thread_id=self._broker_key,
                    runtime=self,
                    request_id=int(request_id),
                    approval_id=f"acp-{request_id}",
                    kind="tool_approval",
                    interrupt_payload=self._permission_payload(params),
                )
                self._pending_params = dict(params)
                self._parked = True
                self._broker.park(pending)
                yield NativeEvent(
                    kind="approval_requested",
                    session_id=self._session_id,
                    text="ACP is waiting for approval",
                    data={"approval_id": pending.approval_id, "kind": "tool_approval"},
                )
                return
            if method:
                # Unknown client-facing request: answer with an empty object so
                # the ACP server never hangs waiting for us.
                try:
                    await self._client.respond(int(request_id), {})
                except Exception:
                    logger.debug("[ACP] failed to answer %s", method, exc_info=True)

    async def resume_turn(self, response: Any) -> AsyncIterator[NativeEvent]:
        # The ACP options are retained on the runtime that parked the request;
        # the broker hands this same runtime back to the graph.
        options = getattr(self, "_pending_params", {})
        assert self._broker_key
        pending = self._broker.get(self._broker_key)
        if pending is None:
            raise NativeCliError("ACP approval resume has no parked request")
        assert self._client is not None
        result = self._decision_result(
            response, options if isinstance(options, Mapping) else {}
        )
        self._parked = False
        self._broker.clear(self._broker_key)
        try:
            await self._client.respond(pending.request_id, result)
            async for event in self._consume():
                yield event
        finally:
            await self._finish_or_park()

    async def _finish_or_park(self) -> None:
        if self._parked:
            return
        if self._client is not None:
            client, self._client = self._client, None
            if self._prompt_task is not None and not self._prompt_task.done():
                # Its request can never be answered once the agent is gone.
                self._prompt_task.cancel()
            await client.close()

    async def cancel(self) -> None:
        if self._client is not None:
            try:
                if self._session_id:
                    await self._client.notify(
                        "session/cancel", {"sessionId": self._session_id}
                    )
            except Exception:
                pass
            await self._client.close()
            self._client = None
        if self._broker_key:
            self._broker.clear(self._broker_key)
