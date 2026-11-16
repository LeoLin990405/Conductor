"""Minimal Agent Client Protocol (ACP) stdio client.

ACP is JSON-RPC 2.0 over newline-delimited JSON. The client owns a subprocess
and multiplexes three kinds of traffic:

- responses to its requests;
- notifications (typically ``session/update``);
- server-initiated requests such as ``session/request_permission``.

This module is transport only: mapping ACP into Conductor's NativeEvent and
approval broker lives in the native runtime layer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from typing import Any

from src.llms.native_cli.base import NativeCliError
from src.llms.native_cli.isolation import (
    prepare_process,
    redact_diagnostic,
    redact_value,
)
from src.llms.native_cli.process import (
    kill_child_group,
    spawn_group,
    terminate_child,
)

logger = logging.getLogger(__name__)


class AcpClient:
    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._cwd = cwd
        self._env = dict(env or {})
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._next_id = 1
        self._closed = False

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    async def start(self) -> None:
        try:
            argv, child_env, child_cwd = prepare_process(
                tuple(self._argv), self._cwd, self._env
            )
            process = await spawn_group(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=child_cwd,
                env=child_env if child_env is not None else {**os.environ, **self._env},
                close_fds=True,
            )
        except FileNotFoundError as exc:
            raise NativeCliError(f"ACP CLI not found: {self._argv[0]}") from exc
        if self._closed:
            # close() ran while the child was being started and found nothing
            # to stop; nobody else will, so the start ends it.
            kill_child_group(process)
            await process.wait()
            raise NativeCliError("ACP client was closed while its process started")
        self._proc = process
        self._reader = asyncio.create_task(self._read_stdout())
        self._stderr = asyncio.create_task(self._read_stderr())

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line.decode("utf-8", "replace"))
            except (ValueError, UnicodeError):
                logger.debug(
                    "[ACP] received unparseable stdout line",
                )
                continue
            if not isinstance(message, dict):
                continue
            message = redact_value(message, self._env)
            request_id = message.get("id")
            if request_id is not None and ("result" in message or "error" in message):
                future = self._pending.pop(int(request_id), None)
                if future is not None and not future.done():
                    future.set_result(message)
                continue
            await self._incoming.put(message)
        if not self._closed:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(NativeCliError("ACP connection closed"))
            self._pending.clear()

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text:
                logger.debug("[ACP] %s", redact_diagnostic(text, self._env)[:1000])

    async def request(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise NativeCliError("ACP process is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()
        try:
            message = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in message:
            error = message.get("error") or {}
            raise NativeCliError(
                f"ACP {method} failed: {redact_diagnostic(str(error.get('message') or error), self._env)}"
            )
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def notify(
        self, method: str, params: Mapping[str, Any] | None = None
    ) -> None:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise NativeCliError("ACP process is not running")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def respond(self, request_id: int, result: Mapping[str, Any]) -> None:
        if self._closed or self._proc is None or self._proc.stdin is None:
            raise NativeCliError("ACP process is not running")
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": dict(result),
        }
        self._proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self._proc.stdin.drain()

    async def next_message(self) -> dict[str, Any]:
        return await self._incoming.get()

    async def close(self) -> None:
        self._closed = True
        if self._proc is not None:
            await terminate_child(self._proc, grace=3.0)
        for task in (self._reader, self._stderr):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
