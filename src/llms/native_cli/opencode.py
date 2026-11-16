"""OpenCode CLI adapter with standalone sessions and isolated configuration."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
    NativeCliUsage,
    raise_if_auth_failure,
)
from src.llms.native_cli.failure import (
    FailureClass,
    classify_failure,
    register_failure_rule,
)
from src.llms.native_cli.isolation import current_worker_isolation
from src.llms.native_cli.process import run_process


def _usage(tokens: Any) -> NativeCliUsage:
    if not isinstance(tokens, Mapping):
        return NativeCliUsage()
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), Mapping) else {}
    return NativeCliUsage(
        input_tokens=int(tokens.get("input") or 0),
        output_tokens=int(tokens.get("output") or 0),
        cache_read_tokens=int(cache.get("read") or 0),
        cache_write_tokens=int(cache.get("write") or 0),
        total_tokens=int(tokens.get("total") or 0),
    )


def _raise_safe(diagnostic: str) -> None:
    raise_if_auth_failure("opencode", diagnostic)
    classified = classify_failure("opencode", diagnostic, exit_code=1)
    if classified.failure_class is FailureClass.AUTH_FAILED:
        raise NativeCliUnavailable(
            "opencode is not logged in or has no usable credentials"
        ) from None
    if classified.failure_class in {
        FailureClass.QUOTA_EXHAUSTED,
        FailureClass.CONTEXT_EXHAUSTED,
        FailureClass.TURN_BUDGET_EXHAUSTED,
    }:
        raise NativeCliError(f"opencode failure: {classified.reason_code}") from None
    raise NativeCliError("opencode request failed; check local CLI diagnostics") from None


def parse_opencode_stream(
    stdout: str,
) -> tuple[str, str | None, NativeCliUsage, list[Mapping[str, Any]]]:
    events: list[Mapping[str, Any]] = []
    answers: list[str] = []
    session_id: str | None = None
    usage = NativeCliUsage()
    diagnostics: list[str] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, Mapping):
            continue
        events.append(event)
        session_id = session_id or str(
            event.get("sessionID") or event.get("sessionId") or ""
        ) or None
        kind = event.get("type")
        part = event.get("part")
        if kind == "text" and isinstance(part, Mapping):
            text = str(part.get("text") or "").strip()
            if text:
                answers.append(text)
        elif kind == "step_finish" and isinstance(part, Mapping):
            usage = _usage(part.get("tokens"))
        elif kind == "error":
            raw_error = event.get("error")
            diagnostic = ""
            if isinstance(raw_error, Mapping):
                data = raw_error.get("data")
                if isinstance(data, Mapping):
                    diagnostic = str(data.get("message") or "")
                diagnostic = " ".join(
                    item
                    for item in (
                        diagnostic,
                        str(raw_error.get("message") or ""),
                        str(raw_error.get("name") or ""),
                    )
                    if item
                )
            else:
                diagnostic = str(raw_error or "")
            diagnostics.append(diagnostic)
    if not events:
        raise NativeCliError("opencode returned no machine-readable events")
    if diagnostics:
        _raise_safe(" ".join(diagnostics))
    if not answers:
        raise NativeCliError("opencode produced no final answer")
    return answers[-1], session_id, usage, events


class OpenCodeAdapter:
    """Run OpenCode without contacting the user's background server or config."""

    provider = "opencode"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 120.0,
        cwd: str | None = None,
        model: str | None = None,
        auto: bool = False,
        config_dir: str | None = None,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model
        self._auto = auto
        self._config_dir = config_dir
        self._owned_config_dir: Path | None = None

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_OPENCODE_BIN")
        if explicit:
            return explicit
        found = shutil.which("opencode")
        if not found:
            raise NativeCliUnavailable(
                "opencode CLI not found; install opencode or set NATIVE_CLI_OPENCODE_BIN"
            )
        return found

    def _isolated_config(self) -> str:
        if self._config_dir:
            return self._config_dir
        if self._owned_config_dir is None:
            isolation = current_worker_isolation()
            self._owned_config_dir = Path(
                tempfile.mkdtemp(
                    prefix="opencode-config-",
                    dir=isolation.home if isolation is not None else None,
                )
            )
            self._owned_config_dir.chmod(0o700)
            self._config_dir = str(self._owned_config_dir)
        return self._config_dir

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        argv = [self.binary, "run", "--format", "json", "--standalone"]
        if self._auto:
            argv.append("--auto")
        if self._model:
            argv.extend(["--model", self._model])
        if session_id:
            argv.extend(["--session", session_id])
        if cwd is not None or self._cwd is not None:
            argv.extend(["--dir", cwd if cwd is not None else str(self._cwd)])
        argv.extend(["--", prompt])
        return CliInvocation(
            argv=tuple(argv),
            cwd=cwd if cwd is not None else self._cwd,
            env={"OPENCODE_CONFIG_DIR": self._isolated_config()},
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
        started = time.monotonic()
        try:
            output = await run_process(invocation)
        except NativeCliUnavailable:
            raise
        except NativeCliError as exc:
            if str(exc).startswith("native CLI timed out after "):
                raise NativeCliError(str(exc)) from None
            _raise_safe(str(exc))
        finally:
            if self._owned_config_dir is not None:
                shutil.rmtree(self._owned_config_dir, ignore_errors=True)
                self._owned_config_dir = None
                self._config_dir = None
        text, new_session_id, usage, events = parse_opencode_stream(output.stdout)
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=new_session_id or session_id,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=len(events),
            raw=tuple(events),
        )


register_failure_rule(
    "opencode",
    FailureClass.AUTH_FAILED,
    reason_code="opencode_login_required",
    patterns=("not logged in", "authentication failed", "opencode_login_required"),
)
register_failure_rule(
    "opencode",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="opencode_quota_exceeded",
    patterns=(
        "quota exceeded",
        "usage limit exceeded",
        "opencode_quota_exceeded",
        "worker_quota_exhausted",
    ),
)
register_failure_rule(
    "opencode",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="opencode_context_overflow",
    patterns=(
        "context window exceeded",
        "input exceeds context window",
        "opencode_context_overflow",
        "worker_context_exhausted",
    ),
)
