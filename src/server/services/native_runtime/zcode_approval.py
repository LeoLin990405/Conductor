"""ZCode approvals via a runtime-patched permission broker.

ZCode's headless CLI has no permission reply channel: its stream emits
``permission.requested`` but the default broker immediately resolves it as
"no permission client configured". REA traced that to a single minified
``DenyPermissionBroker`` class. This module generates a cached *copy* of the
vendor CLI with that class replaced by a file-backed broker, then drives the
copy with the normal stream-json protocol. The vendor app is never modified in
place.

Request/response protocol:

  <dir>/<requestId>.request.json   written by the patched CLI
  <dir>/<requestId>.response.json  written by Conductor after HITL

The copy is opt-in via ``NATIVE_CLI_ZCODE_APPROVALS=1``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from src.llms.native_cli.base import NativeCliError
from src.llms.native_cli.process import kill_child_group, merged_env, spawn_group
from src.llms.native_cli.zcode import ZCodeCliAdapter
from src.server.services.native_runtime.approvals import (
    PendingNativeApproval,
    get_approval_broker,
)
from src.server.services.native_runtime.contracts import (
    NativeCapabilities,
    NativeEvent,
)
from src.server.services.native_runtime.zcode_stream import parse_zcode_stream_line

logger = logging.getLogger(__name__)

_DENY_CLASS = (
    'N0t=class{static{r(this,"DenyPermissionBroker")}async requestPermission(t)'
    '{if(t.mode==="guarded")throw $r(nr.ConfigurationError,`No permission client '
    'configured for ${t.toolName}`,{recoverable:!0});return{decision:"deny",'
    'reason:`No permission client configured for ${t.toolName}`,resolvedAt:new Date}}}'
)

_BRIDGE_CLASS = (
    'N0t=class{static{r(this,"DenyPermissionBroker")}async requestPermission(t)'
    '{const fs=require("node:fs"),p=require("node:path"),d=process.env.CONDUCTOR_ZCODE_PERMISSION_DIR;'
    'if(!d)return{decision:"deny",reason:`No permission client configured for ${t.toolName}`,resolvedAt:new Date};'
    'try{fs.mkdirSync(d,{recursive:true});const i=String(t.requestId||`perm-${Date.now()}-${Math.random().toString(16).slice(2)}`),'
    'q=p.join(d,`${i}.request.json`),w=p.join(d,`${i}.response.json`);'
    'fs.writeFileSync(q,JSON.stringify({requestId:i,toolName:t.toolName,input:t.input,mode:t.mode,riskLevel:t.riskLevel,reason:t.reason,sideEffectScope:t.sideEffectScope}));'
    'const n=Date.now()+600000;while(Date.now()<n){if(fs.existsSync(w)){const v=JSON.parse(fs.readFileSync(w,"utf8"));'
    'try{fs.unlinkSync(w)}catch{};try{fs.unlinkSync(q)}catch{};'
    'return{decision:v.decision==="allow"?"allow":"deny",reason:v.reason||"alpha approval",resolvedAt:new Date}}'
    'await new Promise(r=>setTimeout(r,100))}}catch{}'
    'return{decision:"deny",reason:"alpha approval timeout",resolvedAt:new Date}}}'
)


def _patched_source(source: str) -> str:
    if source.count(_DENY_CLASS) != 1:
        raise NativeCliError(
            "ZCode CLI layout changed: cannot locate the permission broker class"
        )
    return source.replace(_DENY_CLASS, _BRIDGE_CLASS, 1)


def ensure_approval_binary(binary: str) -> Path:
    """Return a cached copy of the vendor CLI with the file-backed broker."""
    source_path = Path(binary)
    raw = source_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()[:16]
    root = Path(
        os.environ.get("NATIVE_CLI_ZCODE_APPROVAL_DIR")
        or (Path.home() / ".cache" / "conductor" / "native-cli" / "zcode")
    )
    target = root / f"zcode-alpha-approval-{digest}.cjs"
    if target.is_file() and target.stat().st_size == len(raw) + (
        len(_BRIDGE_CLASS) - len(_DENY_CLASS)
    ):
        return target
    text = raw.decode("utf-8")
    patched = _patched_source(text)
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    tmp = target.with_suffix(".tmp")
    tmp.write_text(patched)
    try:
        os.chmod(tmp, 0o700)
    except OSError:
        pass
    tmp.replace(target)
    logger.info("[ZCODE_APPROVAL] generated patched CLI copy at %s", target)
    return target


class ZCodeApprovalRuntime:
    provider = "zcode"

    def __init__(
        self,
        *,
        cwd: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        broker_thread_id: str | None = None,
        binary: str | None = None,
        model: str | None = None,
        **_unused: Any,
    ) -> None:
        base = ZCodeCliAdapter(binary=binary, read_only=False, mode="build")
        self._source_binary = base.binary
        self._adapter = ZCodeCliAdapter(
            binary=str(ensure_approval_binary(base.binary)),
            cwd=cwd,
            read_only=False,
            mode="build",
            mcp_servers=mcp_servers,
            model=model,
        )
        self._cwd = cwd
        self._broker_key = broker_thread_id
        self._broker = get_approval_broker()
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending_response: tuple[str, str] | None = None
        self._parked = False
        self._hold = contextlib.AsyncExitStack()
        self._permission_dir = Path(tempfile.mkdtemp(prefix="alpha-zcode-perm-"))

    def capabilities(self) -> NativeCapabilities:
        return NativeCapabilities(
            streaming=True,
            tool_events=True,
            session_resume=True,
            approvals=True,
            mcp=True,
            workspace_files=True,
            cancel=True,
        )

    async def _start(self, prompt: str, session_id: str | None, cwd: str | None) -> None:
        # The provider lock is held until the turn is finished, so the shared
        # provider and MCP config stay this turn's for as long as it runs.
        self._hold = contextlib.AsyncExitStack()
        try:
            turn = await self._hold.enter_async_context(
                self._adapter.turn_scope(
                    prompt, session_id=session_id, cwd=cwd, stream=True
                )
            )
        except BaseException:
            await self._hold.aclose()
            raise
        invocation = turn.invocation
        env = merged_env(invocation.env)
        env["CONDUCTOR_ZCODE_PERMISSION_DIR"] = str(self._permission_dir)
        if not env.get("ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"):
            bundled = (
                Path(self._source_binary).resolve().parent.parent
                / "config"
                / "provider"
                / "zcode-builtin.json"
            )
            if bundled.is_file():
                env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = str(bundled)
        try:
            self._process = await spawn_group(
                *invocation.argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=invocation.cwd,
                env=env,
            )
        except FileNotFoundError as exc:
            await self._hold.aclose()
            raise NativeCliError("ZCode approval binary not found") from exc
        except BaseException:
            await self._hold.aclose()
            raise
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            logger.debug("[ZCODE_APPROVAL] %s", line.decode("utf-8", "replace")[:1000])

    def _approval_payload(self, event: NativeEvent) -> dict[str, Any]:
        data = event.data if isinstance(event.data, Mapping) else {}
        return {
            "action_requests": [{
                "name": str(data.get("tool_name") or "zcode_tool"),
                "args": data.get("input") or {},
                "description": str(data.get("reason") or "ZCode tool approval"),
            }],
            "kind": "tool_approval",
        }

    async def _events(self) -> AsyncIterator[NativeEvent]:
        try:
            async with contextlib.aclosing(self._read_events()) as events:
                async for event in events:
                    yield event
        except BaseException:
            # An error or a closed stream must not strand the provider lock;
            # a parked approval is the one exit that keeps the turn open.
            if not self._parked:
                await self._finish()
            raise

    async def _read_events(self) -> AsyncIterator[NativeEvent]:
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
            for event in parse_zcode_stream_line(line):
                if event.kind == "approval_requested":
                    data = event.data if isinstance(event.data, Mapping) else {}
                    request_id = str(data.get("request_id") or "")
                    assert self._broker_key
                    pending = PendingNativeApproval(
                        thread_id=self._broker_key,
                        runtime=self,
                        request_id=request_id,
                        approval_id=f"zcode-{request_id}",
                        kind="tool_approval",
                        interrupt_payload=self._approval_payload(event),
                    )
                    self._parked = True
                    self._broker.park(pending)
                    yield NativeEvent(
                        kind="approval_requested",
                        session_id=event.session_id,
                        text=event.text,
                        tool_name=event.tool_name,
                        data={
                            "approval_id": pending.approval_id,
                            "kind": "tool_approval",
                            "request_id": request_id,
                        },
                    )
                    return
                yield event

    async def _release_turn(self) -> None:
        """Give the provider lock back when the turn is over.

        The lock is held for the whole turn, so every way out of the turn
        has to release it: a failed turn, a cancelled turn, a closed stream
        and an abandoned stream all end here. An intentional approval pause
        is the one exception -- the CLI process is still alive and still
        needs this turn's config, so the lock stays until the turn resumes
        or is cancelled.
        """
        if self._parked:
            return
        await self._finish()

    async def stream_turn(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> AsyncIterator[NativeEvent]:
        await self._start(prompt, session_id, cwd)
        try:
            async with contextlib.aclosing(self._events()) as events:
                async for event in events:
                    yield event
        finally:
            # The consumer may stop reading at any point -- the graph raises
            # on an error event -- and the lock must not stay held until the
            # generator is collected.
            await self._release_turn()

    def _response_decision(self, response: Any) -> str:
        if isinstance(response, Mapping):
            entries = list(response.values())
            value = entries[0] if entries and isinstance(entries[0], Mapping) else response
            decisions = value.get("decisions") if isinstance(value, Mapping) else None
            if isinstance(decisions, list) and decisions:
                first = decisions[0]
                if isinstance(first, Mapping):
                    return str(first.get("type") or "reject").lower()
        return "reject"

    async def resume_turn(self, response: Any) -> AsyncIterator[NativeEvent]:
        assert self._broker_key
        pending = self._broker.get(self._broker_key)
        if pending is None:
            raise NativeCliError("ZCode approval resume has no parked request")
        decision = self._response_decision(response)
        allow = decision in {"approve", "approved", "accept"}
        path = self._permission_dir / f"{pending.request_id}.response.json"
        path.write_text(
            json.dumps({
                "decision": "allow" if allow else "deny",
                "reason": "Approved from Conductor" if allow else "Rejected from Conductor",
            })
        )
        self._parked = False
        self._broker.clear(self._broker_key)
        try:
            async with contextlib.aclosing(self._events()) as events:
                async for event in events:
                    yield event
        finally:
            await self._release_turn()

    async def _finish(self) -> None:
        self._parked = False
        try:
            process = self._process
            if process is not None:
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
            # Also after a cancel: the provider lock and the folder go with the
            # turn, and nothing else will give them back.
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                self._stderr_task = None
            await self._hold.aclose()
            shutil.rmtree(self._permission_dir, ignore_errors=True)

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
