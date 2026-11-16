"""Codex ``app-server`` transport: token deltas and real approvals.

``codex exec`` is one-shot: it reports completed items and cannot ask the host
for a decision.  ``codex app-server`` is the bidirectional protocol: it streams
``item/agentMessage/delta`` token deltas and sends
``item/commandExecution/requestApproval`` reverse requests.  This runtime keeps
the process alive when such a request arrives, parks it in the native approval
broker, and lets the existing LangGraph HITL resume send the decision back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Any, AsyncIterator, Mapping

from src.llms.native_cli.base import NativeCliError, NativeCliUnavailable, NativeCliUsage
from src.llms.native_cli.isolation import prepare_process
from src.llms.native_cli.process import (
    _PROCESS_TRACKER,
    _kill_tree,
    kill_child_group,
    spawn_group,
    terminate_child,
)
from src.server.services.native_runtime.approvals import (
    PendingNativeApproval,
    get_approval_broker,
)
from src.server.services.native_runtime.contracts import (
    NativeCapabilities,
    NativeEvent,
)

logger = logging.getLogger(__name__)

# One queued item that tells ``next_event`` the reader is gone. A bare EOF on
# stdout used to leave a waiter blocked forever, which also hid a reader crash
# from the runtime.
_READER_CLOSED = object()

_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file_change",
    "item/permissions/requestApproval": "permissions",
}


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


@dataclass(frozen=True)
class _ApprovalRequest:
    request_id: int
    kind: str
    params: Mapping[str, Any]

    def action_request(self) -> dict[str, Any]:
        params = dict(self.params)
        if self.kind == "command":
            name = "mcp__codex__command"
            args = {
                "command": params.get("command"),
                "cwd": params.get("cwd"),
            }
            if params.get("reason"):
                args["reason"] = params.get("reason")
        elif self.kind == "file_change":
            name = "mcp__codex__file_change"
            args = {
                "item_id": params.get("itemId"),
                "reason": params.get("reason"),
                "grant_root": params.get("grantRoot"),
            }
        else:
            name = "mcp__codex__permissions"
            args = {"permissions": params.get("permissions")}
        return {
            "name": name,
            "args": args,
            "description": f"Codex is requesting approval for {name}",
        }


class _AppServerClient:
    """Minimal line-delimited JSON-RPC client for ``codex app-server``."""

    def __init__(self, argv: list[str], *, cwd: str | None, env: Mapping[str, str] | None = None) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = dict(env or {})
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._incoming: asyncio.Queue[object] = asyncio.Queue()
        self._next_id = 1
        self._closed = False
        self._reader_failure: BaseException | None = None
        self._tracker = None
        #: Removed once the process is gone (an isolated CODEX_HOME, P0).
        self.isolated_home: Any = None

    async def start(self) -> None:
        # Its own session: the group is what ``close`` kills, and a child in this
        # server's group could not be killed without killing the server.
        argv, env, cwd = prepare_process(
            tuple(self._argv), self._cwd, self._env or None
        )
        process = await spawn_group(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=16 * 1024 * 1024,
        )
        if self._closed:
            # close() ran while the child was being started and found nothing
            # to stop; nobody else will, so the start ends it.
            kill_child_group(process)
            await process.wait()
            raise NativeCliError("codex app-server was closed while it started")
        self._proc = process
        self._tracker = _PROCESS_TRACKER.get()
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8", "replace"))
                except (ValueError, UnicodeError) as exc:
                    # A frame that is not JSON at all is protocol corruption,
                    # not noise to filter: skipping it would let a partial or
                    # broken reader pass for a working one. Raise without the
                    # frame's content — the surfaced error must never carry
                    # run text.
                    raise NativeCliError(
                        "codex app-server sent a malformed JSON frame"
                    ) from exc
                if not isinstance(message, dict):
                    raise NativeCliError(
                        "codex app-server sent a non-object JSON frame"
                    )
                request_id = message.get("id")
                if request_id is not None and ("result" in message or "error" in message):
                    future = self._pending.pop(int(request_id), None)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue
                # Server-initiated request or notification.
                await self._incoming.put(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - surfaced through next_event
            self._reader_failure = exc
        finally:
            if not self._closed:
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(
                            NativeCliError("codex app-server closed stdout")
                        )
                self._pending.clear()
                await self._incoming.put(_READER_CLOSED)

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text:
                logger.debug("[CODEX_APP_SERVER] %s", text[:1000])

    async def request(
        self, method: str, params: Mapping[str, Any] | None = None, *, timeout: float = 60.0
    ) -> dict[str, Any]:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise NativeCliError("codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = {"id": request_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        try:
            # The write/drain is part of the request budget too: a pipe that
            # stops draining must not outlive the deadline that bounds waiting.
            await asyncio.wait_for(
                self._write_payload(payload), timeout=max(0.0, deadline - loop.time())
            )
            message = await asyncio.wait_for(
                future, timeout=max(0.0, deadline - loop.time())
            )
        except asyncio.TimeoutError as exc:
            raise NativeCliError(
                f"codex app-server {method} request timed out"
            ) from exc
        finally:
            self._pending.pop(request_id, None)
        if "error" in message:
            error = message.get("error") or {}
            raise NativeCliError(str(error.get("message") or "codex app-server request failed"))
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def next_event(self) -> dict[str, Any]:
        item = await self._incoming.get()
        if item is _READER_CLOSED:
            detail = (
                f": {type(self._reader_failure).__name__}: {self._reader_failure}"
                if self._reader_failure is not None
                else ""
            )
            exit_code = self._proc.returncode if self._proc is not None else None
            exit_detail = f" (exit {exit_code})" if exit_code is not None else ""
            raise NativeCliError(
                f"codex app-server reader stopped{detail}{exit_detail}"
            )
        if not isinstance(item, dict):
            raise NativeCliError("codex app-server queued an invalid event")
        return item

    async def _write_payload(self, payload: Mapping[str, Any]) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise NativeCliError("codex app-server is not running")
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = dict(params)
        await self._write_payload(payload)

    async def respond(self, request_id: int, result: Mapping[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise NativeCliError("codex app-server is not running")
        payload = {"id": request_id, "result": dict(result)}
        await self._write_payload(payload)

    async def close(self) -> None:
        self._closed = True
        try:
            await self._close_process()
        finally:
            if self.isolated_home is not None:
                from src.llms.native_cli.personal_memory import remove_isolated_codex_home

                remove_isolated_codex_home(self.isolated_home)
                self.isolated_home = None

    async def _close_process(self) -> None:
        if self._proc is not None:
            if self._proc.returncode is None and self._tracker is not None:
                _kill_tree(self._proc, self._tracker)
                try:
                    await self._proc.wait()
                except Exception:  # noqa: BLE001 - already going away
                    pass
            else:
                await terminate_child(self._proc, grace=3.0)
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


#: ``codex -c`` overrides that turn the memories feature off for one process
#: (the app-server form of exec's ``--disable memories``).
APP_SERVER_MEMORY_OFF_OVERRIDES: dict[str, str] = {
    "features.memories": "false",
    "memories.use_memories": "false",
    "memories.generate_memories": "false",
}


class CodexAppServerRuntime:
    """Native runtime backed by Codex's bidirectional app-server protocol."""

    provider = "codex"

    def __init__(
        self,
        *,
        binary: str | None = None,
        cwd: str | None = None,
        thread_id: str | None = None,
        broker_thread_id: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        model: str | None = None,
        timeout_seconds: float = 600.0,
        approval_policy: str = "on-request",
        sandbox: str = "workspace-write",
        config_overrides: Mapping[str, str] | None = None,
        developer_instructions: str | None = None,
        isolate_user_instructions: bool = False,
        isolate_personal_memory: bool = False,
        **_unused: Any,
    ) -> None:
        self._binary = binary
        self._cwd = cwd
        self._thread_id = thread_id
        self._broker_key = broker_thread_id
        self._mcp_servers = list(mcp_servers or ())
        self._model = model
        self._timeout_seconds = timeout_seconds
        self._approval_policy = approval_policy
        self._sandbox = sandbox
        self._config_overrides = dict(config_overrides or {})
        if isolate_user_instructions:
            # app-server does not accept exec's --ignore-user-config flag.
            # Its supported config override disables automatic AGENTS.md
            # discovery while keeping the user's authentication config.
            self._config_overrides["project_doc_max_bytes"] = "0"
        # P0: a Worker (Ticket) run, or a delegation a Worker started, must
        # not load the operator's Codex memories; the process also gets an
        # isolated CODEX_HOME without memories, global AGENTS.md or MCP servers.
        self._isolate_personal_memory = isolate_user_instructions or isolate_personal_memory
        if self._isolate_personal_memory:
            self._config_overrides.update(APP_SERVER_MEMORY_OFF_OVERRIDES)
        self._developer_instructions = developer_instructions
        self._client: _AppServerClient | None = None
        # The thread id belongs to the process that resumed it. A new client
        # is a new process and must thread/resume; this is that process.
        self._thread_client: _AppServerClient | None = None
        self._turn_deadline: float | None = None
        self._text_parts: list[str] = []
        self._streamed_items: set[str] = set()
        self._item_phase: dict[str, str] = {}
        self._broker = get_approval_broker()
        self._parked = False
        self._resuming = False
        self._env: dict[str, str] = {}
        for server in self._mcp_servers:
            self._env.update(
                {str(k): str(v) for k, v in (server.get("env") or {}).items()}
            )

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_CODEX_BIN")
        if explicit:
            return explicit
        found = shutil.which("codex")
        if not found:
            raise NativeCliUnavailable(
                "codex CLI not found; install @openai/codex or set NATIVE_CLI_CODEX_BIN"
            )
        return found

    def capabilities(self) -> NativeCapabilities:
        return NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            cache_usage=True,
            approvals=True,
            mcp=bool(self._mcp_servers),
            cancel=True,
            workspace_files=True,
        )

    def _argv(self) -> list[str]:
        argv = [self.binary]
        for key, value in self._config_overrides.items():
            argv.extend(["-c", f"{key}={value}"])
        argv.extend(["app-server", "--listen", "stdio://"])
        for server in self._mcp_servers:
            name = str(server.get("name") or "")
            if not name:
                continue
            url = str(server.get("url") or "").strip()
            if url:
                headers = {
                    str(k).lower(): str(v)
                    for k, v in (server.get("headers") or {}).items()
                }
                auth = headers.get("authorization", "")
                if auth.lower().startswith("bearer ") and len(headers) == 1:
                    token = auth.split(None, 1)[1].strip()
                    env_name = (
                        "CONDUCTOR_MCP_"
                        + "".join(ch if ch.isalnum() else "_" for ch in name).upper()
                        + "_TOKEN"
                    )
                    self._env[env_name] = token
                    argv.extend(["-c", f"mcp_servers.{name}.url={_toml_string(url)}"])
                    argv.extend(
                        [
                            "-c",
                            f"mcp_servers.{name}.bearer_token_env_var={_toml_string(env_name)}",
                        ]
                    )
                elif headers:
                    # App-server has no arbitrary-header MCP config; skip the
                    # server rather than send it unauthenticated.
                    pass
                else:
                    argv.extend(["-c", f"mcp_servers.{name}.url={_toml_string(url)}"])
                continue
            command = str(server.get("command") or "")
            args = [str(arg) for arg in (server.get("args") or [])]
            if not command:
                continue
            argv.extend(["-c", f"mcp_servers.{name}.command={_toml_string(command)}"])
            cells = ", ".join(_toml_string(arg) for arg in args)
            argv.extend(["-c", f"mcp_servers.{name}.args=[{cells}]"])
            env_map = {str(k): str(v) for k, v in (server.get("env") or {}).items()}
            if env_map:
                names = ", ".join(_toml_string(k) for k in env_map)
                argv.extend(["-c", f"mcp_servers.{name}.env_vars=[{names}]"])
        return argv

    async def _ensure_client(self) -> None:
        if self._client is not None:
            return
        env = dict(self._env)
        isolated_home = None
        if self._isolate_personal_memory:
            from src.llms.native_cli.personal_memory import (
                isolated_codex_home_parent,
                prepare_isolated_codex_home,
                source_codex_home,
            )

            parent = isolated_codex_home_parent()
            if parent is not None:
                isolated_home = prepare_isolated_codex_home(source_codex_home(), parent)
            if isolated_home is not None:
                env["CODEX_HOME"] = str(isolated_home)
        client = _AppServerClient(self._argv(), cwd=self._cwd, env=env)
        client.isolated_home = isolated_home
        # Published before the process exists, so a ``cancel()`` from another
        # task reaches it during the start and the handshake; a cancel or a
        # failure of this task's own closes it below.
        self._client = client
        try:
            # Process startup is part of the run budget too: a missing binary
            # or a child that never answers must not hold the turn open.
            await asyncio.wait_for(
                client.start(), timeout=self._rpc_timeout(30.0)
            )
            await client.request(
                "initialize",
                {
                    "clientInfo": {"name": "conductor-native", "version": "1.0.0"},
                    "capabilities": {"experimentalApi": True},
                },
                timeout=self._rpc_timeout(30.0),
            )
            await client.notify("initialized")
        except BaseException:
            if self._client is client:
                self._client = None
            await client.close()
            raise

    async def _ensure_thread(self, session_id: str | None, cwd: str | None) -> None:
        assert self._client is not None
        # Same process, already resumed. A replacement process keeps the id
        # and has a new client; returning here would skip thread/resume and
        # send the notice to a process that does not have the session.
        if self._thread_id is not None and self._thread_client is self._client:
            return
        retained = self._thread_id or session_id
        params: dict[str, Any] = {
            "cwd": cwd or self._cwd,
            "approvalPolicy": self._approval_policy,
            "sandbox": self._sandbox,
        }
        if self._model:
            params["model"] = self._model
        if self._developer_instructions:
            params["developerInstructions"] = self._developer_instructions
        if retained:
            params["threadId"] = retained
            await self._client.request(
                "thread/resume", params, timeout=self._rpc_timeout(self._timeout_seconds)
            )
            self._thread_id = retained
        else:
            result = await self._client.request(
                "thread/start", params, timeout=self._rpc_timeout(self._timeout_seconds)
            )
            thread = result.get("thread") or {}
            thread_id = str(thread.get("id") or "")
            if not thread_id:
                raise NativeCliError("codex app-server did not return a thread id")
            self._thread_id = thread_id
        self._thread_client = self._client
        if self._broker_key is None:
            self._broker_key = self._thread_id

    def _rpc_timeout(self, default: float) -> float:
        if self._turn_deadline is None:
            return default
        remaining = self._turn_deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise NativeCliError("codex app-server turn timed out")
        return min(default, remaining)

    async def _start_turn(self, prompt: str) -> None:
        assert self._client is not None and self._thread_id
        await self._client.request(
            "turn/start",
            {
                "threadId": self._thread_id,
                "input": [{"type": "text", "text": prompt}],
            },
            timeout=self._rpc_timeout(self._timeout_seconds),
        )

    def _park(self, approval: _ApprovalRequest) -> PendingNativeApproval:
        assert self._thread_id
        assert self._broker_key
        action = approval.action_request()
        payload = {
            "action_requests": [action],
            "kind": "tool_approval",
        }
        pending = PendingNativeApproval(
            thread_id=self._broker_key,
            runtime=self,
            request_id=approval.request_id,
            approval_id=f"codex-{approval.request_id}",
            kind=approval.kind,
            interrupt_payload=payload,
            text_parts=list(self._text_parts),
        )
        self._parked = True
        self._broker.park(pending)
        return pending

    async def _respond(self, pending: PendingNativeApproval, response: Any) -> None:
        assert self._client is not None
        decision = pending.decision_for(response)
        if pending.kind == "command":
            result: dict[str, Any] = {"decision": decision}
        elif pending.kind == "file_change":
            result = {"decision": decision}
        else:
            # Permission-profile prompts need a granted profile. Until one is
            # selected, declining is the safe, protocol-valid answer.
            result = {"permissions": None, "scope": "turn"}
        await self._client.respond(pending.request_id, result)
        self._broker.clear(pending.thread_id)

    async def _iter_turn(self) -> AsyncIterator[NativeEvent | _ApprovalRequest]:
        assert self._client is not None
        while True:
            if self._turn_deadline is not None:
                remaining = self._turn_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise NativeCliError("codex app-server turn timed out")
                message = await asyncio.wait_for(
                    self._client.next_event(), timeout=remaining
                )
            else:
                message = await self._client.next_event()
            method = str(message.get("method") or "")
            params = message.get("params")
            if not isinstance(params, Mapping):
                params = {}
            request_id = message.get("id")
            if method in _APPROVAL_METHODS and request_id is not None:
                yield _ApprovalRequest(
                    request_id=int(request_id),
                    kind=_APPROVAL_METHODS[method],
                    params=params,
                )
                return
            if method == "item/agentMessage/delta":
                delta = str(params.get("delta") or "")
                item_id = str(params.get("itemId") or "")
                if delta:
                    self._streamed_items.add(item_id)
                    phase = self._item_phase.get(item_id, "")
                    if phase == "commentary":
                        yield NativeEvent(
                            kind="thinking_delta", text=delta, session_id=self._thread_id
                        )
                    else:
                        self._text_parts.append(delta)
                        # The item id marks message boundaries: a Worker's
                        # completion report is its last message only.
                        yield NativeEvent(
                            kind="text_delta",
                            text=delta,
                            session_id=self._thread_id,
                            data={"item_id": item_id},
                        )
                continue
            if method in {"item/started", "item/completed"}:
                item = params.get("item")
                if isinstance(item, Mapping):
                    for event in self._item_event(item, started=method == "item/started"):
                        yield event
                continue
            if method == "thread/tokenUsage/updated":
                usage = (params.get("tokenUsage") or {}).get("last") or {}
                parsed = NativeCliUsage.from_mapping(usage)
                yield NativeEvent(
                    kind="usage",
                    session_id=self._thread_id,
                    data={
                        "input_tokens": parsed.input_tokens,
                        "output_tokens": parsed.output_tokens,
                        "cache_read_tokens": parsed.cache_read_tokens,
                        "cache_write_tokens": parsed.cache_write_tokens,
                        "total_tokens": parsed.total_tokens,
                    },
                )
                continue
            if method == "turn/completed":
                turn = params.get("turn") or {}
                status = str(turn.get("status") or "completed")
                if status not in {"completed", "interrupted"}:
                    error = turn.get("error") or {}
                    yield NativeEvent(
                        kind="error",
                        text=str(error.get("message") or f"codex turn {status}"),
                    )
                    return
                yield NativeEvent(kind="turn_completed", session_id=self._thread_id)
                return
            if method == "error":
                error = params.get("error") or {}
                if params.get("willRetry"):
                    continue
                yield NativeEvent(kind="error", text=str(error.get("message") or "codex error"))
                return
            # Unknown server request: answer so Codex never hangs on us.
            if request_id is not None and method:
                try:
                    await self._client.respond(int(request_id), {})
                except Exception:
                    logger.debug("failed to answer codex server request %s", method, exc_info=True)

    def _item_event(self, item: Mapping[str, Any], *, started: bool) -> list[NativeEvent]:
        item_type = str(item.get("type") or "")
        item_id = str(item.get("id") or "") or None
        if item_type == "agentMessage":
            self._item_phase[item_id or ""] = str(item.get("phase") or "")
            if started:
                return []
            if item_id in self._streamed_items:
                return []
            text = str(item.get("text") or "")
            if not text:
                return []
            if self._item_phase.get(item_id or "") == "commentary":
                return [
                    NativeEvent(
                        kind="thinking_delta", text=text, session_id=self._thread_id
                    )
                ]
            self._text_parts.append(text)
            return [
                NativeEvent(
                    kind="text_delta",
                    text=text,
                    session_id=self._thread_id,
                    data={"item_id": item_id or ""},
                )
            ]
        if item_type == "reasoning":
            if started:
                return []
            content = item.get("summary") or item.get("content") or []
            text = "\n".join(str(part) for part in content if part)
            return (
                [NativeEvent(kind="thinking_delta", text=text, session_id=self._thread_id)]
                if text
                else []
            )
        if item_type in {"commandExecution", "mcpToolCall", "fileChange"}:
            if started:
                name = item_type
                args: dict[str, Any]
                if item_type == "commandExecution":
                    args = {"command": item.get("command"), "cwd": item.get("cwd")}
                elif item_type == "mcpToolCall":
                    name = f"{item.get('server') or 'mcp'}.{item.get('tool') or 'tool'}"
                    args = dict(item.get("arguments") or {})
                else:
                    args = {"changes": item.get("changes")}
                return [
                    NativeEvent(
                        kind="tool_started",
                        tool_name=name,
                        tool_call_id=item_id,
                        data={"id": item_id, "name": name, "args": args},
                    )
                ]
            status = str(item.get("status") or "completed")
            content = (
                item.get("aggregatedOutput")
                or item.get("output")
                or item.get("error")
                or item.get("status")
                or status
            )
            return [
                NativeEvent(
                    kind="tool_result",
                    tool_name=item_type,
                    tool_call_id=item_id,
                    data={
                        "id": item_id,
                        "content": str(content)[:20000],
                        "status": "error" if status in {"failed", "declined"} else "success",
                    },
                )
            ]
        return []

    async def _finish_or_park(self) -> None:
        """Close the protocol process only when no approval is left parked."""

        # A resume may still hold the process while an older turn generator is
        # being finalized; closing then would kill the resumed turn.
        if self._resuming:
            return
        if not self._parked and self._client is not None:
            await self._client.close()
            self._client = None

    async def stream_turn(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[NativeEvent]:
        self._parked = False
        loop = asyncio.get_running_loop()
        self._turn_deadline = loop.time() + max(1.0, float(self._timeout_seconds))
        try:
            await self._ensure_client()
            await self._ensure_thread(session_id, cwd)
            await self._start_turn(prompt)
            self._text_parts = []
            async for event in self._iter_turn():
                if isinstance(event, _ApprovalRequest):
                    self._park(event)
                    yield NativeEvent(
                        kind="approval_requested",
                        text="Codex is waiting for approval",
                        session_id=self._thread_id,
                        data={"approval_id": f"codex-{event.request_id}", "kind": event.kind},
                    )
                    return
                yield event
        finally:
            await self._finish_or_park()

    async def resume_turn(self, response: Any) -> AsyncIterator[NativeEvent]:
        pending = self._broker.get(self._broker_key)
        if pending is None:
            raise NativeCliError("native approval resume has no parked turn")
        # Human approval time is not execution time: start this resumed turn
        # with a fresh budget instead of inheriting the expired deadline.
        self._turn_deadline = (
            asyncio.get_running_loop().time()
            + max(1.0, float(self._timeout_seconds))
        )
        self._parked = False
        self._resuming = True
        try:
            await self._respond(pending, response)
            # The pre-approval text lives in the pending record; the graph
            # seeds its final projection from it, so only post-approval deltas
            # stream.
            async for event in self._iter_turn():
                if isinstance(event, _ApprovalRequest):
                    self._park(event)
                    yield NativeEvent(
                        kind="approval_requested",
                        text="Codex is waiting for approval",
                        session_id=self._thread_id,
                        data={"approval_id": f"codex-{event.request_id}", "kind": event.kind},
                    )
                    return
                yield event
        finally:
            self._resuming = False
            await self._finish_or_park()

    async def cancel(self) -> None:
        if self._broker_key:
            self._broker.clear(self._broker_key)
        if self._client is not None:
            await self._client.close()
            self._client = None
