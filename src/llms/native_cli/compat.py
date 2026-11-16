"""Custom OpenAI-compatible providers through the DSH headless profile."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from src.config.compat import ADAPTER_CONTROLLED_ENV_NAMES, CompatWorkerConfig
from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
)
from src.llms.native_cli.events import parse_ndjson
from src.llms.native_cli.isolation import (
    approved_env_value,
    current_worker_isolation,
    private_home,
    secure_private_write,
)
from src.llms.native_cli.process import run_process

_TIMEOUT_ERROR = re.compile(r"native CLI timed out after \d+s\Z")


def _yaml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _redact_string_leaves(value: Any, credential: str) -> Any:
    if isinstance(value, str):
        return value.replace(credential, "[redacted]") if credential else value
    if isinstance(value, dict):
        return {
            _redact_string_leaves(key, credential): _redact_string_leaves(
                item, credential
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_string_leaves(item, credential) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_string_leaves(item, credential) for item in value)
    return value


def _safe_process_error(
    exc: NativeCliError, timeout_seconds: float, credential: str
) -> NativeCliError:
    message = str(exc)
    redacted = _redact_string_leaves(message, credential)
    suffix = " [redacted]" if redacted != message else ""
    if isinstance(exc, NativeCliUnavailable):
        return NativeCliUnavailable(f"dsh compat provider is unavailable{suffix}")
    if _TIMEOUT_ERROR.fullmatch(message):
        return NativeCliError(f"dsh CLI timed out after {timeout_seconds:.0f}s{suffix}")
    return NativeCliError(f"dsh compat request failed{suffix}")


class CompatDshAdapter:
    review_capable = False

    def __init__(
        self,
        config: CompatWorkerConfig,
        *,
        binary: str | None = None,
        harness_home: str | None = None,
        profile: str = "headless",
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
    ) -> None:
        if config.api_key_env in ADAPTER_CONTROLLED_ENV_NAMES:
            raise NativeCliUnavailable("dsh compat credential environment is invalid")
        self._config = config
        self.provider = config.name
        self.review_capable = False
        self.context_window_tokens = config.context_window_tokens
        self._binary = binary
        self._harness_home = harness_home
        self._profile = profile
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_DSH_BIN")
        if explicit:
            return explicit
        found = shutil.which("dsh")
        if not found:
            raise NativeCliUnavailable(
                "dsh CLI not found; install @deepseek-ai/dsh or set NATIVE_CLI_DSH_BIN"
            )
        return found

    @property
    def harness_home(self) -> str:
        isolation = current_worker_isolation()
        explicit = self._harness_home or approved_env_value("NATIVE_CLI_DSH_HOME")
        if isolation is not None:
            if explicit and not Path(explicit).expanduser().resolve().is_relative_to(
                isolation.home.resolve()
            ):
                raise NativeCliUnavailable("dsh compat home is outside workspace home")
            return str(
                Path(explicit).expanduser() if explicit else private_home() / ".dsh"
            )
        if explicit:
            return str(Path(explicit).expanduser())
        return str(private_home() / ".cache" / "conductor" / "dsh-compat")

    def _overlay(self) -> str:
        try:
            context_window = int(self._config.context_window_tokens)
        except (TypeError, ValueError):
            raise NativeCliUnavailable("dsh compat context window is invalid") from None
        if context_window <= 0:
            raise NativeCliUnavailable("dsh compat context window is invalid")
        return f"""- id: agent-default-model
  config:
    provider: {_yaml_string(self._config.name)}
    model: {_yaml_string(self._config.model)}
- id: llm-pi-ai
  config:
    providers:
      {_yaml_string(self._config.name)}:
        displayName: {_yaml_string(self._config.name)}
        api: {_yaml_string(self._config.protocol)}
        baseURL: {_yaml_string(self._config.base_url)}
        apiKeyEnv: {_yaml_string(self._config.api_key_env)}
        models:
          - id: {_yaml_string(self._config.model)}
            name: {_yaml_string(self._config.model)}
            contextWindow: {context_window}
            maxTokens: {min(4096, context_window)}
            input: [text]
            compat:
              supportsUsageInStreaming: true
              supportsFinishReason: true
- id: session-title
  disabled: true
- id: session-title-llm
  disabled: true
- id: otel
  disabled: true
- id: session-telemetry-otel
  disabled: true
"""

    def _write_overlay(self, body: str) -> Path:
        home = Path(self.harness_home)
        if current_worker_isolation() is not None:
            path = home / f".compat-{secrets.token_hex(16)}.yml"
            secure_private_write(path, body.encode("utf-8"), mode=0o600)
            return path

        try:
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            home.chmod(0o700)
            descriptor, name = tempfile.mkstemp(
                prefix=".compat-", suffix=".yml", dir=home
            )
            path = Path(name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(body.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                path.chmod(0o600)
            except BaseException:
                path.unlink(missing_ok=True)
                raise
            return path
        except OSError:
            raise NativeCliUnavailable(
                "dsh compat overlay could not be written"
            ) from None

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        binary = self.binary
        api_key = approved_env_value(self._config.api_key_env)
        if not api_key:
            raise NativeCliUnavailable("dsh compat credentials are unavailable")
        harness_home = Path(self.harness_home)
        overlay = self._write_overlay(self._overlay())
        argv = [
            binary,
            "--profile",
            self._profile,
            "--patch",
            str(overlay),
            "--json",
        ]
        if session_id:
            argv.extend(["--session-id", session_id])
        argv.extend(["--", prompt])
        return CliInvocation(
            argv=tuple(argv),
            cwd=cwd if cwd is not None else self._cwd,
            env={
                "HOME": str(harness_home.parent),
                "DSH_HOME": str(harness_home),
                self._config.api_key_env: api_key,
            },
            timeout_seconds=self._timeout_seconds,
        )

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> NativeCliResult:
        invocation = self.build_invocation(prompt, session_id=session_id, cwd=cwd)
        overlay = Path(invocation.argv[invocation.argv.index("--patch") + 1])
        started = time.monotonic()
        credential = invocation.env[self._config.api_key_env]
        try:
            try:
                output = await run_process(invocation)
            except NativeCliError as exc:
                raise _safe_process_error(
                    exc, self._timeout_seconds, credential
                ) from None
            stdout = _redact_string_leaves(output.stdout, credential)
            try:
                parsed = parse_ndjson(stdout)
                text = _redact_string_leaves(parsed.text, credential)
                if not text:
                    raise NativeCliError("native CLI produced no final answer")
                result_session_id = _redact_string_leaves(
                    parsed.session_id or session_id, credential
                )
                raw = tuple(
                    {"type": event["type"], "content": "[redacted]"}
                    if event.get("type") in {"text", "thinking"}
                    else _redact_string_leaves(event, credential)
                    for event in parsed.events
                    if event.get("type") != "compat_final_text"
                ) + ({"type": "compat_final_text", "text": text},)
            except (NativeCliError, ValueError, TypeError) as exc:
                message = str(exc)
                redacted = _redact_string_leaves(message, credential)
                suffix = " [redacted]" if redacted != message else ""
                raise NativeCliError(
                    f"dsh compat response could not be parsed{suffix}"
                ) from None
            return NativeCliResult(
                provider=self.provider,
                text=text,
                session_id=result_session_id,
                usage=parsed.usage,
                duration_seconds=time.monotonic() - started,
                events=len(parsed.events),
                raw=raw,
            )
        finally:
            try:
                overlay.unlink(missing_ok=True)
            except OSError:
                pass
