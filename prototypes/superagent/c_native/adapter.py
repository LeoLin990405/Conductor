"""Native-CLI SuperAgent adapter: Claude Code, an MCP server, a model shim.

The Router and the Watchdog run inside the Claude Code CLI. The CLI's own
shell, file and web tools are switched off, so the only tools the model can
reach are the autonomy tools the MCP server publishes. The CLI's model
traffic goes to a shim that serves Claude Code's own model API and owns the
turn behind it.

Three processes are launched, and every one of them is declared in
``runtime_argv`` before the adapter is built:

- the **model shim**, which serves Anthropic's Messages API on a unix socket
  and forwards every request to the harness's scripted model;
- the **MCP server**, which speaks MCP on its standard input and output and
  dispatches the calls to the bridge's ``tools.call``;
- the **Claude Code CLI**, once per turn, exactly like ``claude -p``.

The turn itself lives in the shim, not in the CLI: the credit gate, the
memory, the transcript, the events and the store are all behind the model
API. The CLI is a client, so the real binary can take its place.

The CLI patterns come from ``src/llms/native_cli``: the flag set from
``claude.py``, the process handling from ``process.py`` (a new session, closed
file descriptors, a minimal environment, and a process-group kill), and the
"only these paths may be written" rule from ``isolation.py``. Those files are
read, not changed: the prototype keeps its own copy so it stays importable
without the product's isolation stack.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
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
from src.tools.autonomy.tools import AUTONOMY_ROUTER_TOOLS, AUTONOMY_WATCHDOG_TOOLS

_PACKAGE = Path(__file__).resolve().parent
_SHIM = _PACKAGE / "model_shim.py"
_MCP = _PACKAGE / "mcp_server.py"
_CLI = _PACKAGE / "fake_claude_code.py"

# The built-in tools the CLI must not offer. ``--tools ""`` already removes
# them; the deny list is the second lock on the same door.
_DISALLOWED_TOOLS = (
    "Bash Read Write Edit Glob Grep WebFetch WebSearch Task NotebookEdit LS MultiEdit"
)
_ALLOWED_TOOLS = "mcp__autonomy__*"

# The only parent variables a child sees. See ``_environment``.
_ENV_ALLOWLIST = (
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PYTHONPATH",
    "SUPERAGENT_OFFLINE",
)

_READY_TIMEOUT_SECONDS = 30.0
_TURN_TIMEOUT_SECONDS = 120.0


def _tool_definitions(role: str) -> list[dict[str, Any]]:
    """The MCP schemas for a role, taken from the product tool registry."""

    tools = AUTONOMY_ROUTER_TOOLS if role == "router" else AUTONOMY_WATCHDOG_TOOLS
    definitions: list[dict[str, Any]] = []
    for tool in tools:
        schema = tool.get_input_schema().model_json_schema()
        definitions.append(
            {
                "name": tool.name,
                "description": (tool.description or "").strip(),
                "inputSchema": {
                    "type": "object",
                    "properties": schema.get("properties") or {},
                    "required": schema.get("required") or [],
                },
            }
        )
    return definitions


def cli_argv(runtime: RuntimeContext) -> list[str]:
    """The command the Claude Code CLI is launched with, exactly.

    These are the real Claude Code flags: ``--tools ""`` removes every
    built-in tool, ``--strict-mcp-config`` and ``--mcp-config`` pin the only
    MCP server the turn may use, and ``--allowed-tools`` pre-approves its
    tools so ``dontAsk`` does not deny them. ``--bare`` skips hooks, plugins,
    CLAUDE.md discovery, auto-memory and the keychain, and drops the
    ``DeferredToolPlaceholder`` entry Claude Code otherwise offers;
    ``--setting-sources ""`` loads no user, project or local settings, and
    ``--no-session-persistence`` writes no session to disk. The fake CLI
    refuses to run without any of them. See the package README for what was
    verified against the real binary and what was only read off
    ``claude --help``.
    """

    return [
        sys.executable,
        str(_CLI),
        "-p",
        "--bare",
        "--no-session-persistence",
        "--setting-sources",
        "",
        "--verbose",
        "--output-format",
        "stream-json",
        "--model",
        "scripted",
        "--tools",
        "",
        "--disallowed-tools",
        _DISALLOWED_TOOLS,
        "--allowed-tools",
        _ALLOWED_TOOLS,
        "--strict-mcp-config",
        "--mcp-config",
        str(runtime.scratch_dir / "mcp.json"),
        "--permission-mode",
        "dontAsk",
        "--disable-slash-commands",
        "--",
    ]


def runtime_argv(runtime: RuntimeContext) -> list[list[str]]:
    """Every command this adapter will launch, exactly as it will launch it."""

    return [
        [sys.executable, str(_SHIM), runtime.bridge_path, str(runtime.scratch_dir)],
        [sys.executable, str(_MCP), runtime.bridge_path, str(runtime.scratch_dir)],
        cli_argv(runtime),
    ]


def make_adapter(
    *,
    model: ScriptedFakeChatModel,
    store: FakePostgresStore,
    credit_gate: FakeCreditGate,
    memory: FakeMemoryRecall,
    subscriber: EventSubscriber,
    runtime: RuntimeContext,
) -> NativeCliAdapter:
    """Build the native-CLI adapter from the contract doubles.

    A model whose ``provider_name`` is not ``scripted`` is refused, so this
    package cannot be pointed at a hosted provider by swapping the double.
    The doubles themselves are reached over the bridge by the runtime
    processes, never from here.
    """

    provider = getattr(model, "provider_name", None)
    if provider != "scripted":
        raise RuntimeError(f"c_native adapter refuses provider {provider!r}")
    del model, credit_gate, memory, subscriber
    return NativeCliAdapter(runtime, store)


class NativeCliAdapter:
    """Drives the Claude Code CLI and its two sidecars."""

    provider_name = "scripted"

    def __init__(self, runtime: RuntimeContext, store: FakePostgresStore) -> None:
        self.runtime = runtime
        self.store = store
        self._shim: subprocess.Popen[Any] | None = None
        self._mcp: subprocess.Popen[Any] | None = None
        self._cli: asyncio.subprocess.Process | None = None
        self._mcp_channel: socket.socket | None = None
        self._cold_start_ms = 0.0
        self.last_cli_output = ""

    # -- adapter surface -------------------------------------------------

    def tool_names(self, role: str) -> list[str]:
        return autonomy_tool_names(role)

    def cold_start_ms(self) -> float:
        return self._cold_start_ms

    async def start(self, session_id: str, *, role: str, user_id: str) -> None:
        """Open the session and boot the runtime processes."""

        started = time.perf_counter()
        row = self.store.get_session(session_id)
        if row is None:
            with self.store.transaction() as txn:
                txn.upsert_session(
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "role": canonical_role(role),
                        "status": "ready",
                        "next_seq": 1,
                    }
                )
        else:
            # The stored identity is the session. A restarted process often
            # passes a default role; overwriting it would retarget the tools.
            canonical_role(str(row["role"]))
        self._write_manifests(session_id)
        self._boot_services()
        self._cold_start_ms = (time.perf_counter() - started) * 1000.0

    async def turn(self, session_id: str, user_text: str) -> list[StreamEvent]:
        """Run one turn in the CLI and return the events it published."""

        before = {int(row["seq"]) for row in self.store.event_rows(session_id)}
        await self._run_cli(session_id, user_text)
        rows = self.store.event_rows(session_id)
        return [
            StreamEvent(seq=int(row["seq"]), kind=str(row["kind"]), text=str(row["text"]))
            for row in rows
            if int(row["seq"]) not in before
        ]

    def transcript(self, session_id: str) -> Transcript:
        rows = self.store.transcript_rows(session_id)
        return Transcript(
            session_id=session_id,
            entries=[
                TranscriptEntry(
                    seq=int(row["seq"]), kind=str(row["kind"]), text=str(row["text"])
                )
                for row in rows
            ],
        )

    def events(self, session_id: str) -> list[StreamEvent]:
        return [
            StreamEvent(seq=int(row["seq"]), kind=str(row["kind"]), text=str(row["text"]))
            for row in self.store.event_rows(session_id)
        ]

    def session(self, session_id: str) -> SessionRecord:
        row = self.store.get_session(session_id)
        if row is None:
            raise RuntimeError(f"session {session_id} is not started")
        return SessionRecord(
            session_id=session_id,
            user_id=str(row["user_id"]),
            role=str(row["role"]),
            status=str(row["status"]),
        )

    def close(self) -> None:
        """Stop every process this adapter started. Safe to call twice."""

        self._stop_cli()
        for process in (self._shim, self._mcp):
            self._stop(process)
        self._shim = None
        self._mcp = None
        if self._mcp_channel is not None:
            try:
                self._mcp_channel.close()
            except OSError:
                pass
            self._mcp_channel = None

    # -- the runtime -----------------------------------------------------

    def _write_manifests(self, session_id: str) -> None:
        """The MCP config, the session, and the role's tool schemas.

        All three live in the scratch directory. The MCP config is the one
        the real Claude Code reads: a stdio server named with its command.
        """

        scratch = self.runtime.scratch_dir
        roles = {
            "router": _tool_definitions("router"),
            "watchdog": _tool_definitions("watchdog"),
        }
        (scratch / "roles.json").write_text(
            json.dumps(roles, ensure_ascii=False), encoding="utf-8"
        )
        (scratch / "session.json").write_text(
            json.dumps({"session_id": session_id}, ensure_ascii=False),
            encoding="utf-8",
        )
        config = {
            "mcpServers": {
                "autonomy": {
                    "command": sys.executable,
                    "args": [str(_MCP), self.runtime.bridge_path, str(scratch)],
                    "env": {},
                }
            }
        }
        (scratch / "mcp.json").write_text(
            json.dumps(config, ensure_ascii=False), encoding="utf-8"
        )

    def _boot_services(self) -> None:
        """Start the shim and the MCP server, and wait for them."""

        if self._shim is not None and self._shim.poll() is None:
            return
        shim_argv, mcp_argv, _ = runtime_argv(self.runtime)
        self._shim = self._spawn(shim_argv)
        self._await_ready("model.ready", self._shim)
        self._mcp, self._mcp_channel = self._spawn_mcp(mcp_argv)
        self._await_ready("mcp.ready", self._mcp)

    def _spawn_mcp(
        self, argv: list[str]
    ) -> tuple[subprocess.Popen[Any], socket.socket]:
        """Start the MCP server on a stdio channel the CLI inherits.

        The server speaks MCP on its standard input and output, the way the
        real Claude Code talks to a server it spawns. The channel is a
        socket pair, so the CLI gets the same byte stream the server's own
        standard input and output would carry.
        """

        parent_end, child_end = socket.socketpair()
        log_path = self.runtime.scratch_dir / "mcp_server.log"
        handle = log_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                argv,
                env=self._environment(),
                stdin=child_end,
                stdout=child_end,
                stderr=handle,
                text=True,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            handle.close()
            child_end.close()
        return process, parent_end

    def _await_ready(
        self, marker: str, process: subprocess.Popen[Any] | None = None
    ) -> None:
        """Wait for a sidecar to come up. A dead child fails at once."""

        path = self.runtime.scratch_dir / marker
        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        while not path.exists():
            if process is not None and process.poll() is not None:
                raise RuntimeError(
                    f"the runtime did not come up ({marker}): "
                    f"{self._sidecar_log(marker)}"
                )
            if time.monotonic() > deadline:
                raise RuntimeError(f"the runtime did not come up: {marker} is missing")
            time.sleep(0.005)

    def _sidecar_log(self, marker: str) -> str:
        """The tail of a sidecar's log, so a boot failure says why."""

        name = marker.split(".")[0]
        path = self.runtime.scratch_dir / f"{name}.log"
        try:
            return path.read_text(encoding="utf-8").strip()[-400:]
        except OSError:
            return "no log"

    def _environment(self, session_id: str = "") -> dict[str, str]:
        """The child environment: a short allow list, plus the plumbing.

        Nothing else of the parent's environment reaches a child: no
        credentials, no ``ANTHROPIC_*`` the parent happens to carry, no
        ``CLAUDE_*`` switch. ``PYTHONPATH`` is passed on, so the children
        find the bridge module and the offline guard's ``sitecustomize``
        without being told where the repository is. ``HOME`` is the scratch
        directory, so a CLI that looks for ``~/.claude`` finds nothing of the
        user's.
        """

        env = {
            name: os.environ[name] for name in _ENV_ALLOWLIST if name in os.environ
        }
        env.update(
            {
                "HOME": str(self.runtime.scratch_dir),
                "C_NATIVE_BRIDGE": self.runtime.bridge_path,
                "C_NATIVE_SESSION_ID": session_id,
            }
        )
        return env

    def _spawn(self, argv: list[str]) -> subprocess.Popen[Any]:
        """Start a sidecar in its own session, with its output in the scratch."""

        log_path = self.runtime.scratch_dir / f"{Path(argv[1]).stem}.log"
        handle = log_path.open("w", encoding="utf-8")
        try:
            process = subprocess.Popen(
                argv,
                env=self._environment(),
                # A pipe, not ``subprocess.DEVNULL``: the file guard refuses
                # every write outside the scratch directory, and /dev/null is
                # outside it. The sidecars read nothing on standard input.
                stdin=subprocess.PIPE,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                # The CLI patterns: a new session gives the child its own
                # process group, so a stop can take the whole group down.
                start_new_session=True,
                close_fds=True,
            )
        finally:
            handle.close()
        if process.stdin is not None:
            process.stdin.close()
        return process

    async def _run_cli(self, session_id: str, user_text: str) -> None:
        """Run one turn in the CLI, with the prompt on its standard input."""

        argv = cli_argv(self.runtime)
        env = self._environment(session_id)
        env["ANTHROPIC_BASE_URL"] = f"unix://{self.runtime.scratch_dir / 'model.sock'}"
        env["ANTHROPIC_API_KEY"] = "sk-shim"
        if self._mcp_channel is None:
            raise RuntimeError("the MCP server is not running")
        env["C_NATIVE_MCP_FD"] = str(self._mcp_channel.fileno())
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # The CLI patterns: a new session gives the child its own process
            # group, so a stop can take the whole group down.
            start_new_session=True,
            # The MCP channel is the server's standard input and output.
            pass_fds=(self._mcp_channel.fileno(),),
        )
        self._cli = process
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(user_text.encode("utf-8")),
                timeout=_TURN_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            self._stop_cli()
            raise
        except TimeoutError:
            self._stop_cli()
            raise RuntimeError("the Claude Code CLI did not finish its turn") from None
        finally:
            if self._cli is process:
                self._cli = None
        self.last_cli_output = stdout.decode("utf-8", "replace")
        if process.returncode != 0:
            raise RuntimeError(
                "the Claude Code CLI failed "
                f"(rc={process.returncode}): {stderr.decode('utf-8', 'replace').strip()}"
            )

    def _stop_cli(self) -> None:
        """Stop the CLI that is running now, and its process group.

        Synchronous on purpose: ``close()`` is a plain method, and the CLI
        must be gone by the time it returns. ``Process.wait`` is a coroutine,
        so the wait here is the pid itself, not the event loop.
        """

        process = self._cli
        if process is None:
            return
        self._cli = None
        if process.returncode is None:
            # The leader first, then its group: the CLI may have spawned
            # children of its own, and they must not outlive it.
            process.terminate()
            self._signal_group(process.pid, signal.SIGTERM)
            if not self._wait_gone(process.pid):
                process.kill()
                self._signal_group(process.pid, signal.SIGKILL)
                self._wait_gone(process.pid)
        self._close_transport(process)

    @staticmethod
    def _wait_gone(pid: int, timeout: float = 5.0) -> bool:
        """True once the pid is gone. Bounded, so close() always returns."""

        deadline = time.monotonic() + timeout
        while True:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(0.01)

    @staticmethod
    def _close_transport(process: asyncio.subprocess.Process) -> None:
        """Let the event loop drop a killed CLI's pipes."""

        transport = getattr(process, "_transport", None)
        if transport is None:
            return
        try:
            transport.close()
        except (OSError, RuntimeError):
            return

    def _stop(self, process: subprocess.Popen[Any] | None) -> None:
        """Stop a child and everything in its process group."""

        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            self._signal_group(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                self._signal_group(process.pid, signal.SIGKILL)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                try:
                    pipe.close()
                except OSError:
                    pass

    @staticmethod
    def _signal_group(pid: int, sig: int) -> None:
        """Signal a child's group, never our own."""

        try:
            pgid = os.getpgid(pid)
        except (OSError, ProcessLookupError):
            return
        if pgid <= 1 or pgid == os.getpgrp():
            return
        try:
            os.killpg(pgid, sig)
        except (OSError, ProcessLookupError):
            return
