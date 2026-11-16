"""MiniMax Code CLI adapter (``mcode exec --output-format json``).

MiniMax Code's ``exec`` subcommand is the headless entry point. It emits one
stable ``ExecResult`` JSON object with a ``sessionId`` that can be replayed with
``--session``. The prompt goes in over stdin through ``--input -`` so long tasks
never land in argv.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Mapping

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
    NativeCliUsage,
    raise_if_auth_failure,
)
from src.llms.native_cli.failure import FailureClass, register_failure_rule
from src.llms.native_cli.process import run_process
from src.llms.native_cli.isolation import (
    approved_env_value,
    current_worker_isolation,
    private_home,
)


def _safe_process_error(exc: NativeCliError) -> NativeCliError:
    """Keep the known dyld cause without forwarding host library paths."""
    message = str(exc).lower()
    if "dyld" in message and "library not loaded" in message:
        return NativeCliError("dyld: library not loaded")
    return exc


def parse_minimax_result(
    stdout: str,
) -> tuple[str, str | None, NativeCliUsage, Mapping[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("mcode returned no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NativeCliError("mcode returned non-JSON output") from exc
    if not isinstance(payload, Mapping):
        raise NativeCliError("mcode returned an unexpected JSON shape")
    if payload.get("type") != "exec.result":
        raise NativeCliError("mcode returned no exec.result object")

    status = str(payload.get("status") or "")
    if status and status != "succeeded":
        error = payload.get("error")
        message = error.get("message") if isinstance(error, Mapping) else error
        diagnostic = (
            " ".join(str(error.get(key) or "") for key in ("code", "type", "message"))
            if isinstance(error, Mapping)
            else str(error or "")
        )
        raise_if_auth_failure("mcode", diagnostic)
        raise NativeCliError(str(message or f"mcode run ended with {status}")[:1000])

    answer = str(payload.get("output") or payload.get("response") or "").strip()
    if not answer:
        raise NativeCliError("mcode produced no final answer")

    session_id = str(payload.get("sessionId") or "") or None
    usage = NativeCliUsage.from_mapping(payload.get("usage"))
    return answer, session_id, usage, payload


class MiniMaxCodeAdapter:
    """Run one MiniMax Code exec turn and reuse its Session."""

    provider = "minimax"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
        model: str | None = None,
        prompt_mode: str = "coding",
        api_key: str | None = None,
        base_url: str = "https://api.minimax.cn/anthropic",
        data_dir: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model
        self._prompt_mode = prompt_mode
        self._api_key = api_key
        self._base_url = base_url
        self._data_dir = data_dir
        self._mcp_servers = list(mcp_servers or ())
        self._mcp_saved: tuple[str, str | None] | None = None

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_MINIMAX_BIN")
        if explicit:
            return explicit
        found = shutil.which("mcode")
        if not found:
            raise NativeCliUnavailable(
                "mcode CLI not found; install @minimax-ai/code or set "
                "NATIVE_CLI_MINIMAX_BIN"
            )
        return found

    @property
    def data_dir(self) -> str:
        isolation = current_worker_isolation()
        explicit = self._data_dir or approved_env_value("NATIVE_CLI_MINIMAX_DATA_DIR")
        if isolation is not None:
            if explicit and not Path(explicit).expanduser().resolve().is_relative_to(
                isolation.home.resolve()
            ):
                raise NativeCliUnavailable(
                    "MiniMax data directory is outside workspace home"
                )
            return str(
                Path(explicit).expanduser()
                if explicit
                else private_home() / ".cache" / "conductor" / "minimax"
            )
        if explicit:
            return explicit
        return str(Path.home() / ".cache" / "conductor" / "minimax")

    def _write_project_mcp(self, cwd: str | None) -> None:
        if not self._mcp_servers or not cwd:
            return
        from pathlib import Path as _Path

        path = _Path(cwd) / ".mcp.json"
        original: str | None = None
        if path.is_file():
            try:
                original = path.read_text()
                data = json.loads(original)
            except (OSError, json.JSONDecodeError):
                data = {}
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        for server in self._mcp_servers:
            name = str(server.get("name") or "").strip()
            if not name:
                continue
            url = str(server.get("url") or "").strip()
            if url:
                headers = {
                    str(k): str(v) for k, v in (server.get("headers") or {}).items()
                }
                servers[name] = {
                    "type": "http",
                    "url": url,
                    **({"headers": headers} if headers else {}),
                    "enabled": True,
                }
                continue
            command = str(server.get("command") or "").strip()
            if not command:
                continue
            servers[name] = {
                "type": "stdio",
                "command": command,
                "args": [str(a) for a in (server.get("args") or [])],
                "env": {str(k): str(v) for k, v in (server.get("env") or {}).items()},
                "enabled": True,
            }
        data["mcpServers"] = servers
        try:
            path.write_text(json.dumps(data, indent=2))
            path.chmod(0o600)
        except OSError as exc:
            raise NativeCliError(f"could not write {path}: {exc}") from exc
        self._mcp_saved = (str(path), original)

    def _restore_project_mcp(self) -> None:
        saved = self._mcp_saved
        self._mcp_saved = None
        if not saved:
            return
        from pathlib import Path as _Path

        path = _Path(saved[0])
        try:
            if saved[1] is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(saved[1])
                path.chmod(0o600)
        except OSError:
            pass

    async def _ensure_api_provider(self) -> None:
        """Configure a custom MiniMax provider once, without logging the key."""
        if not self._api_key:
            return
        if current_worker_isolation() is not None:
            raise NativeCliUnavailable(
                "MiniMax API-key provisioning is unsupported for isolated workers; "
                "preconfigure provider state inside the workspace home"
            )
        from pathlib import Path as _Path
        from src.llms.native_cli.process import run_process

        data = _Path(self.data_dir)
        data.mkdir(parents=True, exist_ok=True)
        marker = data / ".alpha-provider-ready"
        if marker.is_file():
            return
        env = {
            "MINIMAX_DATA_DIR": str(data),
            "MCODE_PROVIDER_API_KEY": self._api_key,
        }
        model = self._model or "MiniMax-M3"
        setup = CliInvocation(
            argv=(
                self.binary,
                "provider",
                "add",
                "--name",
                "Conductor MiniMax",
                "--base-url",
                self._base_url,
                "--api-format",
                "anthropic-messages",
                "--model",
                model,
                "--api-key-env",
                "MCODE_PROVIDER_API_KEY",
                "--use",
            ),
            env=env,
        )
        try:
            await run_process(setup)
        except NativeCliError:
            # A provider with this name may already exist from an earlier
            # run; point the CLI at the API-key source instead of failing.
            fallback = CliInvocation(
                argv=(self.binary, "provider", "use", "api-key"),
                env=env,
            )
            try:
                await run_process(fallback)
            except NativeCliError:
                raise
        marker.write_text(model)
        try:
            marker.chmod(0o600)
        except OSError:
            pass

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
        stream: bool = False,
    ) -> CliInvocation:
        argv = [
            self.binary,
            "exec",
            "--input",
            "-",
            "--input-format",
            "text",
            "--output-format",
            "stream-json" if stream else "json",
            "--prompt-mode",
            self._prompt_mode,
            # Headless has no TUI to answer an approval prompt. `smart` is
            # the CLI's own safe policy: it resolves routine reads and fails
            # loudly, rather than hanging or bypassing the permission gate.
            "--permission",
            "smart",
        ]
        if self._model and not self._api_key:
            # With the API-key provider the setup command already selected the
            # default model; passing a bare model id would be parsed as
            # ``provider/model`` by mcode.
            argv.extend(["--model", self._model])
        if session_id:
            argv.extend(["--session", session_id])
        env = {"MINIMAX_DATA_DIR": self.data_dir} if self._api_key else None
        return CliInvocation(
            argv=tuple(argv),
            stdin=prompt,
            cwd=cwd if cwd is not None else self._cwd,
            env=env,
            timeout_seconds=self._timeout_seconds,
        )

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> NativeCliResult:
        await self._ensure_api_provider()
        effective_cwd = cwd if cwd is not None else self._cwd
        self._write_project_mcp(effective_cwd)
        try:
            invocation = self.build_invocation(prompt, session_id=session_id, cwd=cwd)
            started = time.monotonic()
            try:
                output = await run_process(invocation)
            except NativeCliError as exc:
                safe_error = _safe_process_error(exc)
                if safe_error is exc:
                    raise
                raise safe_error from None
            text, new_session_id, usage, payload = parse_minimax_result(output.stdout)
        finally:
            self._restore_project_mcp()
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=new_session_id or session_id,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=1,
            raw=(payload,),
        )


# MiniMax Code's own wording, read out of the MiniMax Code CLI bundle: it
# has a protocol code and a message for every class of failure, and it
# recognises a context that did not fit by several phrases of its own.
register_failure_rule(
    "minimax",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="minimax_context_window_exceeded",
    patterns=(
        "model_context_window_exceeded",
        "input is too long for requested model",
        "exceeds the available context size",
        "reduce the length of the messages",
        "prompt is too long",
        "token limit exceeded",
    ),
)
register_failure_rule(
    "minimax",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="minimax_usage_limit_exceeded",
    patterns=(
        "llm usage limit reached",
        "llm credits exhausted",
        "usage_limit_exceeded",
        "available balance",
        "out of budget",
    ),
)
register_failure_rule(
    "minimax",
    FailureClass.RATE_LIMITED,
    reason_code="minimax_tpm_rate_limited",
    patterns=(
        "llm provider token rate limit reached",
        "throttling error",
    ),
)
register_failure_rule(
    "minimax",
    FailureClass.CRASH,
    reason_code="minimax_runtime_dependency_missing",
    patterns=("library not loaded",),
    requires=("dyld",),
)
register_failure_rule(
    "minimax",
    FailureClass.OVERLOADED,
    reason_code="minimax_cluster_overloaded",
    patterns=(
        "llm provider is overloaded",
        "service unavailable:",
    ),
)
register_failure_rule(
    "minimax",
    FailureClass.AUTH_FAILED,
    reason_code="minimax_auth_failed",
    patterns=("llm provider authentication failed",),
)
register_failure_rule(
    "minimax",
    FailureClass.NETWORK,
    reason_code="minimax_transport_failed",
    patterns=(
        "socket hang up",
        "fetch failed",
        "enotfound",
    ),
)
