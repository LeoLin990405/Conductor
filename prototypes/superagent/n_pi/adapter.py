"""Prototype N: a Node service running the Super-agent loop on pi-agent-core.

The Python side only declares and launches ``node service.mjs`` and forwards
``start``, ``turn``, and reads to it over stdin/stdout (one JSON object per
line). The Node service reaches the scripted model, the store, the credit
gate, memory, the subscriber, and the fake tool handlers over the harness's
unix-socket bridge (protocol 2). This adapter never touches the doubles.

The node binary is ``SUPERAGENT_NODE`` when set, else ``node`` on ``PATH``,
resolved to an absolute path so the declared argv is exactly what runs.
Dependencies are ``npm install`` inside this directory (pinned 1.5.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from prototypes.superagent.contract import (
    EventSubscriber,
    FakeCreditGate,
    FakeMemoryRecall,
    FakePostgresStore,
    RuntimeContext,
    ScriptedFakeChatModel,
    SessionRecord,
    StreamEvent,
    Transcript,
    TranscriptEntry,
    autonomy_tool_names,
    canonical_role,
)
from src.tools.autonomy.tools import AUTONOMY_ROUTER_TOOLS, AUTONOMY_WATCHDOG_TOOLS

_HERE = Path(__file__).resolve().parent
_SERVICE = _HERE / "service.mjs"
_CLOSE_TIMEOUT_S = 5


def _node_binary() -> str:
    configured = os.environ.get("SUPERAGENT_NODE")
    found = configured or shutil.which("node")
    if not found:
        raise RuntimeError("n_pi needs node >= 22.19 on PATH (or SUPERAGENT_NODE)")
    return str(Path(found).resolve())


def unavailable_reason() -> str | None:
    """Why this prototype cannot run here, or None when it can.

    The Node dependencies are not committed: install them with
    ``npm ci --ignore-scripts`` in this directory. Node must be >= 22.19.
    """
    found = os.environ.get("SUPERAGENT_NODE") or shutil.which("node")
    if not found:
        return "node >= 22.19 is not on PATH (or SUPERAGENT_NODE)"
    try:
        out = subprocess.run(
            [found, "--version"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.strip().lstrip("v")
        major, minor = (int(part) for part in out.split(".")[:2])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return "cannot read the node version"
    if (major, minor) < (22, 19):
        return f"node {out} is older than 22.19"
    for package in ("pi-agent-core", "pi-ai"):
        if not (_HERE / "node_modules" / "@earendil-works" / package).is_dir():
            return "node dependencies missing: run `npm ci --ignore-scripts` in prototypes/superagent/n_pi"
    return None


def runtime_argv(runtime: RuntimeContext) -> list[list[str]]:
    """Every command this adapter will launch, exactly as it will launch it."""

    return [[_node_binary(), str(_SERVICE), runtime.bridge_path, str(runtime.scratch_dir)]]


def _role_tools() -> dict[str, list[dict[str, str]]]:
    """Name and description of each role tool, in the product list's order."""

    return {
        role: [{"name": tool.name, "description": tool.description} for tool in tools]
        for role, tools in (
            ("router", AUTONOMY_ROUTER_TOOLS),
            ("watchdog", AUTONOMY_WATCHDOG_TOOLS),
        )
    }


def make_adapter(
    *,
    model: ScriptedFakeChatModel,
    store: FakePostgresStore,
    credit_gate: FakeCreditGate,
    memory: FakeMemoryRecall,
    subscriber: EventSubscriber,
    runtime: RuntimeContext,
) -> PiAdapter:
    provider = getattr(model, "provider_name", None)
    if provider != "scripted":
        raise RuntimeError(f"n_pi adapter refuses provider {provider!r}")
    # The doubles are served by the harness on ``runtime.bridge_path``.
    del store, credit_gate, memory, subscriber
    return PiAdapter(runtime)


class PiAdapter:
    provider_name = "scripted"

    def __init__(self, runtime: RuntimeContext) -> None:
        self.runtime = runtime
        self._process: subprocess.Popen[str] | None = None
        self._cold_start_ms = 0.0

    def tool_names(self, role: str) -> list[str]:
        return autonomy_tool_names(role)

    def cold_start_ms(self) -> float:
        return self._cold_start_ms

    async def start(self, session_id: str, *, role: str, user_id: str) -> None:
        started = time.perf_counter()
        if self._process is None:
            self._boot()
        await self._call(
            "start", session_id=session_id, role=canonical_role(role), user_id=user_id
        )
        self._cold_start_ms = (time.perf_counter() - started) * 1000.0

    async def turn(self, session_id: str, user_text: str) -> list[StreamEvent]:
        rows = await self._call("turn", session_id=session_id, user_text=user_text)
        return [StreamEvent(seq=int(r["seq"]), kind=r["kind"], text=r["text"]) for r in rows]

    def transcript(self, session_id: str) -> Transcript:
        rows = self._request("transcript", session_id=session_id)
        return Transcript(
            session_id=session_id,
            entries=[
                TranscriptEntry(seq=int(r["seq"]), kind=r["kind"], text=r["text"])
                for r in rows
            ],
        )

    def events(self, session_id: str) -> list[StreamEvent]:
        rows = self._request("event", session_id=session_id)
        return [StreamEvent(seq=int(r["seq"]), kind=r["kind"], text=r["text"]) for r in rows]

    def session(self, session_id: str) -> SessionRecord:
        row = self._request("session", session_id=session_id)
        return SessionRecord(
            session_id=session_id,
            user_id=str(row["user_id"]),
            role=str(row["role"]),
            status=str(row["status"]),
        )

    def close(self) -> None:
        """Ask the service to exit, then make sure it has. Idempotent."""

        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write(json.dumps({"op": "close"}) + "\n")
                process.stdin.flush()
                process.wait(timeout=_CLOSE_TIMEOUT_S)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=_CLOSE_TIMEOUT_S)
        finally:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()

    def _boot(self) -> None:
        (argv,) = runtime_argv(self.runtime)
        # Looked up on the module so the contract's launch gate sees it.
        self._process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=self.runtime.scratch_dir,
        )
        (self.runtime.scratch_dir / "boot.json").write_text(
            json.dumps({"argv": argv, "pid": self._process.pid}), encoding="utf-8"
        )
        self._request("hello", role_tools=_role_tools())

    async def _call(self, op: str, **params: Any) -> Any:
        # The harness serves the bridge from its own thread, so a blocking
        # exchange cannot starve the service's callbacks.
        return await asyncio.to_thread(self._request, op, **params)

    def _request(self, op: str, **params: Any) -> Any:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("n_pi service is not running")
        try:
            process.stdin.write(json.dumps({"op": op, **params}) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
        except OSError as exc:  # broken pipe: the service died
            raise RuntimeError(f"n_pi service exited (rc={process.poll()})") from exc
        if not line:
            raise RuntimeError(f"n_pi service exited (rc={process.poll()})")
        reply = json.loads(line)
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]
