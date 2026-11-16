"""Anthropic Claude Code CLI adapter (``claude -p --output-format json``)."""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any, Mapping

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
    NativeCliUsage,
)
from src.llms.native_cli.failure import FailureClass, register_failure_rule
from src.llms.native_cli.personal_memory import claude_memory_off_env
from src.llms.native_cli.process import run_process


def parse_claude_result(stdout: str) -> tuple[str, str | None, NativeCliUsage, Mapping[str, Any]]:
    """Parse Claude Code's single JSON result document."""
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("claude returned no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # A noisy wrapper may print a notice before the JSON result.
        start = text.find("{")
        payload = None
        if start >= 0:
            try:
                payload, _ = json.JSONDecoder().raw_decode(text[start:])
            except json.JSONDecodeError:
                payload = None
        if not isinstance(payload, dict):
            raise NativeCliError("claude returned non-JSON output")
    if not isinstance(payload, dict):
        raise NativeCliError("claude returned a non-object result")
    if payload.get("is_error"):
        raise NativeCliError("claude reported an error")
    answer = str(payload.get("result") or "").strip()
    if not answer:
        raise NativeCliError("claude produced no final answer")
    session_id = str(payload.get("session_id") or "") or None
    usage = NativeCliUsage.from_mapping(payload.get("usage"))
    return answer, session_id, usage, payload


class ClaudeCodeAdapter:
    provider = "claude-code"
    routing_priority = "lowest"
    fallback_only = True

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
        model: str | None = None,
        isolated: bool = False,
        safe_mode: bool = False,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model
        self._isolated = isolated
        self._safe_mode = safe_mode

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_CLAUDE_BIN")
        if explicit:
            return explicit
        found = shutil.which("claude")
        if not found:
            raise NativeCliUnavailable(
                "claude CLI not found; install Claude Code or set NATIVE_CLI_CLAUDE_BIN"
            )
        return found

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        argv = [
            self.binary,
            "-p",
            "--model",
            self._model or "haiku",
            "--output-format",
            "json",
            "--exclude-dynamic-system-prompt-sections",
            "--permission-mode",
            "plan",
        ]
        if self._safe_mode or self._isolated:
            # Claude Code documents this as disabling customizations
            # (CLAUDE.md, skills, plugins, hooks, MCP) while preserving the
            # authenticated CLI account. A reviewer gets it too.
            argv.append("--safe-mode")
        if self._isolated:
            argv.extend(
                [
                    "--restricted",
                    "--strict-mcp-config",
                    "--disable-slash-commands",
                    "--tools",
                    "",
                ]
            )
        if session_id:
            argv.extend(["--resume", session_id])
        argv.extend(["--", prompt])
        return CliInvocation(
            argv=tuple(argv),
            cwd=cwd if cwd is not None else self._cwd,
            # A reviewer must not read or write the operator's auto-memory.
            env=claude_memory_off_env() if self._isolated else None,
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
            if "timed out" in str(exc).lower():
                raise NativeCliError("claude CLI timed out") from None
            raise NativeCliError(
                "claude CLI failed; check local CLI diagnostics"
            ) from None
        text, new_session_id, usage, payload = parse_claude_result(output.stdout)
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=new_session_id or session_id,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=1,
            raw=(payload,),
        )


# Claude Code's own wording, read out of the Claude Code CLI binary: it names
# a spent plan ("Usage limit reached"), a long prompt, an overloaded API and
# a sign-in it wants ("Please run /login") in its own words.
register_failure_rule(
    "claude-code",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="claude_usage_limit_reached",
    patterns=(
        "usage limit reached",
        "credit balance is too low",
        "credit balance too low",
    ),
)
register_failure_rule(
    "claude-code",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="claude_prompt_too_long",
    patterns=("prompt is too long",),
)
register_failure_rule(
    "claude-code",
    FailureClass.AUTH_FAILED,
    reason_code="claude_login_required",
    patterns=(
        "please run /login",
        "run claude auth login",
        "authentication required",
    ),
)
register_failure_rule(
    "claude-code",
    FailureClass.OVERLOADED,
    reason_code="claude_overloaded",
    patterns=(
        "overloaded_error",
        "api overloaded",
    ),
)
register_failure_rule(
    "claude-code",
    FailureClass.RATE_LIMITED,
    reason_code="claude_rate_limited",
    patterns=(
        "rate_limit_error",
        "rate limit exceeded",
    ),
)
register_failure_rule(
    "claude-code",
    FailureClass.NETWORK,
    reason_code="claude_transport_failed",
    patterns=(
        "connection closed before the response finished",
        "connection lost",
        "no response from api",
    ),
)
