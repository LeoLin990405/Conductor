"""Doubao's ArkCLI text adapter (``arkcli +chat --format json``)."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Mapping
from typing import Any

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


def parse_doubao_result(
    stdout: str,
) -> tuple[str, str | None, NativeCliUsage, Mapping[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("arkcli returned no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NativeCliError("arkcli returned non-JSON output") from exc
    if not isinstance(payload, Mapping):
        raise NativeCliError("arkcli returned an unexpected JSON shape")
    error = payload.get("error")
    if error:
        if isinstance(error, Mapping):
            diagnostic = " ".join(
                str(error.get(key) or "") for key in ("code", "type", "message")
            )
        else:
            diagnostic = str(error)
        raise_if_auth_failure("doubao", diagnostic)
        raise NativeCliError("arkcli returned an error")
    if str(payload.get("status") or "").lower() in {"error", "failed", "incomplete"}:
        raise NativeCliError("arkcli did not complete the response")
    content = payload.get("content")
    answer = content.strip() if isinstance(content, str) else ""
    if not answer:
        raise NativeCliError("arkcli produced no final answer")
    raw_usage = payload.get("usage")
    usage = raw_usage if isinstance(raw_usage, Mapping) else {}
    normalized_usage = {
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens", 0)),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens", 0)),
        "total_tokens": usage.get("total_tokens", 0),
    }
    try:
        parsed_usage = NativeCliUsage.from_mapping(normalized_usage)
    except (TypeError, ValueError, OverflowError) as exc:
        raise NativeCliError("arkcli returned invalid usage metadata") from exc
    return answer, str(payload.get("id") or "") or None, parsed_usage, payload


class DoubaoArkCliAdapter:
    """Run one text response through the official ArkCLI JSON contract."""

    provider = "doubao"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 120.0,
        cwd: str | None = None,
        model: str | None = None,
        store: bool = False,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model or os.environ.get("NATIVE_CLI_DOUBAO_MODEL")
        self._store = store

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_DOUBAO_BIN")
        if explicit:
            return explicit
        found = shutil.which("arkcli")
        if not found:
            raise NativeCliUnavailable(
                "arkcli CLI not found; install ArkCLI or set NATIVE_CLI_DOUBAO_BIN"
            )
        return found

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        if session_id and not self._store:
            raise NativeCliError("arkcli session resume requires response storage")
        argv = [self.binary, "+chat", "--format", "json", "--no-progress"]
        if self._model:
            argv.extend(["--model", self._model])
        if self._store:
            argv.append("--store")
        if session_id:
            argv.extend(["--previous-response-id", session_id])
        argv.append("--")
        argv.append(prompt)
        return CliInvocation(
            argv=tuple(argv),
            cwd=cwd if cwd is not None else self._cwd,
            env={
                "ARKCLI_NO_UPDATE_NOTIFIER": "1",
                "ARKCLI_CALLER_TYPE": "ai_agent",
                "ARKCLI_CALLER_NAME": "codex",
                "ARKCLI_SKILL_NAME": "arkcli-chat",
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
        started = time.monotonic()
        invocation = self.build_invocation(prompt, session_id=session_id, cwd=cwd)
        try:
            output = await run_process(invocation)
        except NativeCliUnavailable:
            raise
        except NativeCliError as exc:
            raise_if_auth_failure("doubao", str(exc))
            raise NativeCliError("arkcli request failed; check local CLI diagnostics") from None
        text, response_id, usage, payload = parse_doubao_result(output.stdout)
        return NativeCliResult(
            provider=self.provider,
            text=text,
            # ArkCLI only retains responses when explicitly asked to store them.
            session_id=response_id if self._store else None,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=1,
            raw=(payload,),
        )


# ArkCLI's own wording, read out of the Ark CLI binary: it reports a spent
# balance and a throttled account under its own error codes and its own
# sentence.
register_failure_rule(
    "doubao",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="arkcli_quota_exhausted",
    patterns=(
        "insufficient_balance_error",
        "quota is exhausted",
        "billing is unavailable",
        "quotaexceeded",
        "filequotaexceeded",
    ),
)
register_failure_rule(
    "doubao",
    FailureClass.RATE_LIMITED,
    reason_code="arkcli_rate_limit_exceeded",
    patterns=(
        "apiaccountrpmratelimitexceeded",
        "ratelimitexceeded",
        "requestburstoofast",
    ),
)
register_failure_rule(
    "doubao",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="arkcli_context_length",
    patterns=("context_length",),
)
register_failure_rule(
    "doubao",
    FailureClass.OVERLOADED,
    reason_code="arkcli_server_overloaded",
    patterns=("serveroverloaded",),
)
register_failure_rule(
    "doubao",
    FailureClass.AUTH_FAILED,
    reason_code="arkcli_invalid_api_key",
    patterns=("invalidapikey",),
)
