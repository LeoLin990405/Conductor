"""StepFun Step CLI adapter for stateless text calls and native coding turns."""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from typing import Any, Mapping

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
    NativeCliUsage,
    raise_if_auth_failure,
)
from src.llms.native_cli.failure import (
    CONFIRMATION_REQUIRED_REASON,
    FailureClass,
    register_failure_rule,
)
from src.llms.native_cli.isolation import current_worker_isolation
from src.llms.native_cli.process import run_process


def _text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_text_from_value(part) for part in value)
    if isinstance(value, Mapping):
        for key in ("text", "content", "message", "response", "result"):
            if key in value:
                return _text_from_value(value[key])
    return ""


def parse_step_result(stdout: str) -> tuple[str, str | None, NativeCliUsage, Mapping[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("step returned no output")
    payload: Any
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        records: list[Mapping[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, Mapping):
                records.append(item)
        if not records:
            raise NativeCliError("step returned non-JSON output")
        payload = records
    items = payload if isinstance(payload, list) else [payload]
    result: Mapping[str, Any] = {}
    session_id: str | None = None
    answer = ""
    usage_raw: Any = None
    error: str | None = None
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("error"):
            raw_error = item.get("error")
            error = (
                " ".join(
                    str(raw_error.get(key) or "")
                    for key in ("code", "type", "message")
                )
                if isinstance(raw_error, Mapping)
                else str(raw_error)
            )
            raise_if_auth_failure("step", error)
        if item.get("sessionId") or item.get("session_id"):
            session_id = str(item.get("sessionId") or item.get("session_id"))
        if item.get("id") and str(item.get("type") or "") == "session":
            session_id = session_id or str(item.get("id"))
        if str(item.get("type") or "") == "message_end":
            message = item.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                candidate = _text_from_value(message.get("content"))
                if candidate:
                    answer = candidate
                    usage_raw = message.get("usage")
                    result = item
        if str(item.get("type") or "") == "turn_end":
            message = item.get("message")
            if isinstance(message, Mapping):
                candidate = _text_from_value(message.get("content"))
                if candidate:
                    answer = candidate
                    usage_raw = message.get("usage")
                    result = item
        candidate = _text_from_value(item.get("result"))
        if candidate and str(item.get("type") or "") not in {"session", "tool_call", "tool_result"}:
            answer = candidate
            result = item
            usage_raw = item.get("usage") or usage_raw
    if error:
        raise NativeCliError("step reported an error")
    if not answer:
        raise NativeCliError("step produced no final answer")
    return answer, session_id, NativeCliUsage.from_mapping(usage_raw), result


def parse_step_text(stdout: str) -> str:
    """Return the completed answer from Step's default text output."""
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("step returned no output")
    # Some CLI builds or provider errors may still emit a structured response
    # in text mode. Classify only an explicit error object; answer text remains
    # opaque and is never searched for auth markers.
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(payload, Mapping) and payload.get("error"):
        error = payload["error"]
        diagnostic = (
            " ".join(str(error.get(key) or "") for key in ("code", "type", "message"))
            if isinstance(error, Mapping)
            else str(error)
        )
        raise_if_auth_failure("StepFun", diagnostic)
        raise NativeCliError("step reported a request error")
    return text


# Distinct from JSON null, which is a real document with no records.
_NOT_JSON = object()


def _json_value(text: str) -> Any:
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError, TypeError, RecursionError):
        return _NOT_JSON


def _mapping_records(parsed: Any) -> list[Mapping[str, Any]]:
    if isinstance(parsed, Mapping):
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, Mapping)]
    return []


def _json_records(stdout: str) -> list[Mapping[str, Any]]:
    """One JSON document, or NDJSON in which every line is a JSON value.

    A truncated document is not NDJSON. Falling through to the lines that
    happen to parse would treat a nested object inside the broken document
    as a finished assistant message.
    """
    stripped = stdout.strip()
    if not stripped:
        return []
    parsed = _json_value(stripped)
    if parsed is not _NOT_JSON:
        return _mapping_records(parsed)
    records: list[Mapping[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parsed = _json_value(line)
        if parsed is _NOT_JSON:
            return []
        records.extend(_mapping_records(parsed))
    return records


def _visible_assistant_text(message: Mapping[str, Any]) -> str:
    """User-visible text of one assistant message.

    Thinking and tool-call parts are not the message. A stream of deltas is
    not a message either; callers pass the message object Step already built.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts: list[Any] | None
    if isinstance(content, list):
        parts = list(content)
    elif isinstance(content, Mapping):
        parts = [content]
    else:
        text = message.get("text")
        return text if isinstance(text, str) else ""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
            continue
        if not isinstance(part, Mapping):
            continue
        if part.get("type") not in (None, "text"):
            continue
        text = part.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _last_assistant_text(found: str | None, message: Any) -> str | None:
    if not isinstance(message, Mapping) or message.get("role") != "assistant":
        return found
    text = _visible_assistant_text(message)
    return text if text else found


def final_message_from_json(stdout: str) -> str | None:
    """Last assistant message text in Step ``--mode json`` stdout, or None.

    ``parse_step_result`` still owns the finished answer, the session and the
    usage, and it raises when the turn did not finish. This helper only reads
    the string it is given: malformed JSON, a result-only document, and a
    stream with no assistant text all return None.
    """
    if not isinstance(stdout, str) or not stdout.strip():
        return None
    found: str | None = None
    for record in _json_records(stdout):
        found = _last_assistant_text(found, record)
        found = _last_assistant_text(found, record.get("message"))
        messages = record.get("messages")
        if isinstance(messages, list):
            for message in messages:
                found = _last_assistant_text(found, message)
    return found


@dataclass(frozen=True)
class StepFunJsonResult(NativeCliResult):
    """JSON-mode StepFun turn.

    ``text`` is still ``parse_step_result``. ``final_message`` is
    ``final_message_from_json`` of the same stdout, which is None when the
    stream has no assistant text.
    """

    final_message: str | None = None


class StepFunAdapter:
    provider = "stepfun"

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
        self._model = model or os.environ.get("NATIVE_CLI_STEPFUN_MODEL")
        self._read_only = read_only

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_STEPFUN_BIN")
        if explicit:
            return explicit
        found = shutil.which("step")
        if not found:
            raise NativeCliUnavailable("step CLI not found; set NATIVE_CLI_STEPFUN_BIN")
        return found

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        argv = [self.binary, "--mode", "json", "-p", prompt]
        if self._model:
            argv.extend(["--model", self._model])
        isolation = current_worker_isolation()
        if self._read_only:
            isolated_read_only = isolation is not None and isolation.read_only
            approval_mode = "auto" if isolated_read_only else "confirm"
            approval_fallback = "allow" if isolated_read_only else "deny"
            argv.extend([
                "--approval-mode", approval_mode,
                "--non-interactive-approval", approval_fallback,
            ])
        else:
            argv.extend([
                "--approval-mode", "auto",
                "--non-interactive-approval", "allow",
            ])
        effective_cwd = cwd if cwd is not None else self._cwd
        if session_id:
            argv.extend(["--session-id", session_id])
        return CliInvocation(
            argv=tuple(argv),
            cwd=effective_cwd,
            timeout_seconds=self._timeout_seconds,
        )

    def build_text_invocation(self, prompt: str, *, cwd: str | None = None) -> CliInvocation:
        """Build the isolated one-shot command used by ordinary chat calls.

        ``--no-tools`` only withholds the model's tools: a folder the user trusted still
        runs its own ``.stepcode/extensions`` code at start-up, and trust is inherited from
        a parent folder. So extension discovery and project-local files are off as well.
        """
        argv = [self.binary, "-p", "--no-session", "--no-tools", "--no-extensions", "--no-approve"]
        if self._model:
            argv.extend(["--model", self._model])
        argv.extend(["--", prompt])
        effective_cwd = cwd if cwd is not None else self._cwd
        return CliInvocation(
            argv=tuple(argv),
            cwd=effective_cwd,
            timeout_seconds=self._timeout_seconds,
        )

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> NativeCliResult:
        started = time.monotonic()
        isolation = current_worker_isolation()
        read_only_isolated = (
            self._read_only and isolation is not None and isolation.read_only
        )
        coding_compat = session_id is not None or not self._read_only or read_only_isolated
        try:
            invocation = (
                self.build_invocation(prompt, session_id=session_id, cwd=cwd)
                if coding_compat
                else self.build_text_invocation(prompt, cwd=cwd)
            )
            output = await run_process(invocation)
        except NativeCliUnavailable as exc:
            if "not logged in or has no usable credentials" in str(exc):
                raise NativeCliUnavailable(
                    "StepFun is not logged in or has no usable credentials"
                ) from None
            raise
        except NativeCliError as exc:
            # run_process includes provider stderr in generic failures. Keep the
            # exception useful while preventing a CLI from echoing private data.
            if "timed out after" in str(exc):
                raise
            raise NativeCliError("StepFun CLI request failed; details omitted") from None
        if coding_compat:
            text, returned_session_id, usage, _payload = parse_step_result(output.stdout)
            # The answer stays parse_step_result. JSON mode also surfaces the
            # last assistant message, which a result-only stream does not have.
            return StepFunJsonResult(
                provider=self.provider,
                text=text,
                session_id=returned_session_id or session_id,
                usage=usage,
                duration_seconds=time.monotonic() - started,
                events=1,
                final_message=final_message_from_json(output.stdout),
            )
        text = parse_step_text(output.stdout)
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=session_id,
            usage=NativeCliUsage(),
            duration_seconds=time.monotonic() - started,
            events=1,
        )


# Step's own wording, read out of the Step CLI binary: it groups its failure
# codes into quota, rate limit, transient, auth and network sets, and it
# recognises a context that did not fit by its own phrases.
register_failure_rule(
    "stepfun",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="stepfun_quota_exhausted",
    patterns=(
        "account quota unavailable. retry when online",
        "account quota temporarily unavailable",
        "billing_hard_limit_reached",
        "billing_not_active",
        "billing_error",
        "usage_limit_reached",
        "quota_exceeded",
        "gousagelimiterror",
        "freeusagelimiterror",
    ),
)
register_failure_rule(
    "stepfun",
    FailureClass.AUTH_FAILED,
    reason_code="stepfun_auth_unverified",
    patterns=(
        "authentication could not be verified. retry or check your key",
        "permission_error",
    ),
)
register_failure_rule(
    "stepfun",
    FailureClass.AUTH_FAILED,
    reason_code="stepfun_api_key_unavailable",
    patterns=("no api key found",),
)
register_failure_rule(
    "stepfun",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="stepfun_context_length_exceeded",
    patterns=(
        "maximum context length is",
        "exceeds the maximum context length",
    ),
)
register_failure_rule(
    "stepfun",
    FailureClass.RATE_LIMITED,
    reason_code="stepfun_rate_limited",
    patterns=(
        "429 rate limited",
        "too_many_requests",
        "rate_limit_error",
        "rate_limited",
    ),
)
register_failure_rule(
    "stepfun",
    FailureClass.OVERLOADED,
    reason_code="stepfun_transient_error",
    patterns=(
        "overloaded_error",
        "server_error",
        "service_unavailable",
        "internal_error",
    ),
)
register_failure_rule(
    "stepfun",
    FailureClass.NETWORK,
    reason_code="stepfun_network_error",
    patterns=(
        "enotfound",
        "eai_again",
        "enetunreach",
        "ehostunreach",
    ),
)
# Step's dangerous-command guard (N39), seen live in the R8b-2 run:
# "Dangerous command requires confirmation (recursive-force-remove)". The
# rule matches only that line shape -- the sentence at the start of a line,
# followed by the guard's rule name in parentheses -- because the log also
# carries the model's text and tool output, which may quote the sentence.
register_failure_rule(
    "stepfun",
    FailureClass.CONFIRMATION_REQUIRED,
    reason_code=CONFIRMATION_REQUIRED_REASON,
    line_patterns=(r"^[ \t]*dangerous command requires confirmation \([a-z0-9_-]+\)",),
)
