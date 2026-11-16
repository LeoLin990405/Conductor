"""Out-of-process reference adapter: the path a Node service or a native CLI takes.

The Python side declares its boot command with ``runtime_argv``, launches
exactly that, and forwards ``start``, ``turn``, and reads to the child over
stdin/stdout. The child reaches the scripted model, the store, the credit
gate, memory, the subscriber, and the fake tool handlers over the harness's
unix-socket bridge. This adapter never touches the doubles itself.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
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

_RUNTIME = Path(__file__).resolve().with_name("runtime.py")


def runtime_argv(runtime: RuntimeContext) -> list[list[str]]:
    """Every command this adapter will launch, exactly as it will launch it."""

    return [
        [sys.executable, str(_RUNTIME), runtime.bridge_path, str(runtime.scratch_dir)]
    ]


def make_adapter(
    *,
    model: ScriptedFakeChatModel,
    store: FakePostgresStore,
    credit_gate: FakeCreditGate,
    memory: FakeMemoryRecall,
    subscriber: EventSubscriber,
    runtime: RuntimeContext,
) -> BridgeReferenceAdapter:
    provider = getattr(model, "provider_name", None)
    if provider != "scripted":
        raise RuntimeError(f"reference_bridge adapter refuses provider {provider!r}")
    # The doubles are served by the harness on ``runtime.bridge_path``.
    del store, credit_gate, memory, subscriber
    return BridgeReferenceAdapter(runtime)


class BridgeReferenceAdapter:
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
        return [StreamEvent(**row) for row in rows]

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
        return [
            StreamEvent(seq=int(r["seq"]), kind=r["kind"], text=r["text"]) for r in rows
        ]

    def session(self, session_id: str) -> SessionRecord:
        row = self._request("session", session_id=session_id)
        return SessionRecord(
            session_id=session_id,
            user_id=str(row["user_id"]),
            role=str(row["role"]),
            status=str(row["status"]),
        )

    def close(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        try:
            if process.poll() is None:
                assert process.stdin is not None
                process.stdin.write(json.dumps({"op": "close"}) + "\n")
                process.stdin.flush()
                process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)
        finally:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
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
        )
        (self.runtime.scratch_dir / "boot.json").write_text(
            json.dumps({"argv": argv, "pid": self._process.pid}), encoding="utf-8"
        )
        self._request(
            "hello",
            role_tools={
                "router": autonomy_tool_names("router"),
                "watchdog": autonomy_tool_names("watchdog"),
            },
        )

    async def _call(self, op: str, **params: Any) -> Any:
        # The harness serves the bridge from its own thread, so a blocking
        # exchange cannot starve the child's callbacks.
        return await asyncio.to_thread(self._request, op, **params)

    def _request(self, op: str, **params: Any) -> Any:
        process = self._process
        if process is None or process.stdin is None or process.stdout is None:
            raise RuntimeError("reference_bridge runtime is not running")
        process.stdin.write(json.dumps({"op": op, **params}) + "\n")
        process.stdin.flush()
        line = process.stdout.readline()
        if not line:
            raise RuntimeError(f"reference_bridge runtime exited (rc={process.poll()})")
        reply = json.loads(line)
        if not reply["ok"]:
            raise RuntimeError(reply["error"])
        return reply["result"]
