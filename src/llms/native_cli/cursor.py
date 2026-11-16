"""Cursor Agent CLI adapter (``cursor-agent --print --output-format stream-json``).

The outer workspace isolation owns read-only enforcement for daemon Workers;
Cursor plan mode cannot run even read-only shell commands. ``--resume <chat id>``
is the session/cache lever.
The event stream starts with ``{"type":"system","subtype":"init","session_id"}``
and ends with a ``result`` event carrying the final answer and usage.
"""

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
from src.llms.native_cli.isolation import current_worker_isolation
from src.llms.native_cli.process import run_process


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, Mapping) and part.get("type") == "text":
            text = str(part.get("text") or "")
            if text:
                parts.append(text)
    return "".join(parts)


def parse_cursor_stream(stdout: str) -> tuple[str, str | None, NativeCliUsage, list[Mapping[str, Any]]]:
    """Parse Cursor's NDJSON stream: init, assistant deltas, final result."""
    events: list[Mapping[str, Any]] = []
    answer = ""
    session_id: str | None = None
    usage = NativeCliUsage()
    error = ""
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
        etype = str(event.get("type") or "")
        if etype == "system" and event.get("subtype") == "init":
            session_id = str(event.get("session_id") or "") or session_id
        elif etype == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, Mapping) else message
            text = _text_from_content(content)
            if text:
                answer = text
        elif etype == "result":
            session_id = str(event.get("session_id") or "") or session_id
            result = event.get("result")
            if result:
                answer = str(result)
            if event.get("is_error") or event.get("subtype") == "error":
                error = str(result or event.get("error") or "cursor-agent reported an error")
            usage = NativeCliUsage.from_mapping(event.get("usage"))
    if error:
        raise NativeCliError(error[:1000])
    if not events:
        raise NativeCliError("cursor-agent returned no machine-readable events")
    if not answer:
        raise NativeCliError("cursor-agent produced no final answer")
    return answer, session_id, usage, events


class CursorAgentAdapter:
    provider = "cursor"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
        model: str | None = None,
        read_only: bool = True,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model
        self._read_only = read_only

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_CURSOR_BIN")
        if explicit:
            return explicit
        found = shutil.which("cursor-agent")
        if not found:
            raise NativeCliUnavailable(
                "cursor-agent CLI not found; install Cursor CLI or set NATIVE_CLI_CURSOR_BIN"
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
            "--print",
            "--output-format",
            "stream-json",
        ]
        isolation = current_worker_isolation()
        if isolation is not None:
            # Trust only the fresh isolated workspace. The installed CLI's
            # sandbox is disabled here because it cannot nest under the OS
            # worker sandbox; the outer boundary still enforces write policy.
            # --force lets shell tools run non-interactively under that outer
            # boundary. Outside isolation, preserve Cursor's own approval flow.
            argv.extend(("--trust", "--sandbox", "disabled", "--force"))
        # Outside Worker isolation, preserve plan mode by default. A daemon
        # Worker uses the outer sandbox as the enforcement boundary; its mode
        # follows the ticket's read_only state when no outer sandbox is active.
        if self._read_only and not (isolation is not None and isolation.read_only):
            argv.extend(("--mode", "plan"))
        if self._model:
            argv.extend(["--model", self._model])
        if session_id:
            argv.extend(["--resume", session_id])
        argv.append(prompt)
        return CliInvocation(
            argv=tuple(argv),
            cwd=cwd if cwd is not None else self._cwd,
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
        output = await run_process(invocation)
        text, new_session_id, usage, payload = parse_cursor_stream(output.stdout)
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=new_session_id or session_id,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=len(payload),
            raw=payload,
        )


# Cursor's own wording, read out of the Cursor Agent CLI bundle: it names a
# failure by its own error code (its ``ErrorDetails.Error`` enum), and the
# codes say which kind of failure it is.
register_failure_rule(
    "cursor",
    FailureClass.RATE_LIMITED,
    reason_code="cursor_rate_limit_exceeded",
    patterns=(
        "free_user_rate_limit_exceeded",
        "pro_user_rate_limit_exceeded",
        "generic_rate_limit_exceeded",
        "waiting out a rate limit",
    ),
)
register_failure_rule(
    "cursor",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="cursor_usage_limit_exceeded",
    patterns=(
        "free_user_usage_limit",
        "pro_user_usage_limit",
        "usage_limit_exceeded",
        "usage_pricing_required",
    ),
)
# Cursor's RESOURCE_EXHAUSTED names two different things, and the word beside
# it tells them apart. On its own it is the provider throttling the Worker --
# a rate limit that a back-off answers. With a word about money beside it,
# the same code says the account has nothing left to spend, and only a refill
# answers that. The companion word is what tells the two apart, and the quota
# rule outranks the rate-limit rule through the classifier's fixed priority.
register_failure_rule(
    "cursor",
    FailureClass.RATE_LIMITED,
    reason_code="cursor_resource_exhausted",
    patterns=("resource_exhausted",),
)
register_failure_rule(
    "cursor",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="cursor_quota_exhausted",
    patterns=("resource_exhausted",),
    requires=("quota", "billing", "credit", "balance", "insufficient"),
)
register_failure_rule(
    "cursor",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="cursor_context_too_long",
    patterns=(
        "conversation_too_long",
        "max_tokens",
    ),
)
register_failure_rule(
    "cursor",
    FailureClass.OVERLOADED,
    reason_code="cursor_provider_error",
    patterns=("provider_error",),
)
register_failure_rule(
    "cursor",
    FailureClass.NETWORK,
    reason_code="cursor_network_error",
    patterns=(
        "network_error",
        "socket hang up",
        "premature close",
        "ping timed out",
        "enotfound",
        "eai_again",
        "ehostunreach",
    ),
)
register_failure_rule(
    "cursor",
    FailureClass.AUTH_FAILED,
    reason_code="cursor_login_required",
    patterns=(
        "not logged in. run /login first",
        "not_logged_in",
        "bad_api_key",
        "agent_requires_login",
        "auth_token_not_found",
        "auth_token_expired",
    ),
)
