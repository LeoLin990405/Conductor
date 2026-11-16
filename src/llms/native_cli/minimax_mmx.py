"""MiniMax's ``mmx`` text API adapter, separate from the ``mcode`` agent CLI."""

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


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part["text"]
            for part in content
            if isinstance(part, Mapping)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def parse_mmx_result(
    stdout: str,
) -> tuple[str, NativeCliUsage, Mapping[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("mmx returned no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NativeCliError("mmx returned non-JSON output") from exc
    if not isinstance(payload, Mapping):
        raise NativeCliError("mmx returned an unexpected JSON shape")
    error = payload.get("error")
    base_resp = payload.get("base_resp")
    error_message = ""
    if isinstance(error, Mapping):
        error_message = " ".join(
            str(value)
            for value in (
                error.get("type"),
                error.get("code"),
                error.get("message"),
            )
            if value
        )
    elif isinstance(error, str):
        error_message = error
    failed = bool(error)
    if isinstance(base_resp, Mapping):
        raw_status_code = base_resp.get("status_code", 0)
        try:
            if isinstance(raw_status_code, bool) or not isinstance(
                raw_status_code, (int, str)
            ):
                raise ValueError
            status_code = int(raw_status_code or 0)
            failed = failed or status_code != 0
        except (OverflowError, TypeError, ValueError) as exc:
            raise NativeCliError("mmx returned an invalid API status") from exc
        status_msg = str(base_resp.get("status_msg") or "")
        error_message = " ".join(
            part
            for part in (
                error_message,
                f"status code {status_code}",
                status_msg,
            )
            if part
        )
    if failed:
        # The CLI can serialize an API error even when its own process exits 0.
        # Do not echo provider messages, which can contain request details.
        raise_if_auth_failure("mmx", error_message)
        raise NativeCliError("mmx returned an API error")
    if str(payload.get("stop_reason") or payload.get("finish_reason") or "") in {
        "max_tokens",
        "length",
    }:
        raise NativeCliError("mmx response is incomplete because it reached max tokens")
    answer = _content_text(payload.get("content"))
    if not answer.strip():
        raise NativeCliError("mmx produced no final answer")
    usage = payload.get("usage")
    raw_usage = usage if isinstance(usage, Mapping) else {}
    normalized_usage = {
        "input_tokens": raw_usage.get(
            "prompt_tokens", raw_usage.get("input_tokens", 0)
        ),
        "output_tokens": raw_usage.get(
            "completion_tokens", raw_usage.get("output_tokens", 0)
        ),
        "total_tokens": raw_usage.get("total_tokens", 0),
        "cache_read_tokens": raw_usage.get(
            "cached_tokens", raw_usage.get("cache_read_tokens", 0)
        ),
    }
    try:
        normalized = NativeCliUsage.from_mapping(normalized_usage)
    except (OverflowError, TypeError, ValueError) as exc:
        raise NativeCliError("mmx returned invalid usage data") from exc
    return answer.strip(), normalized, payload


class MiniMaxMmxAdapter:
    """One stateless text response through MiniMax Platform's ``mmx`` CLI."""

    provider = "minimax-mmx"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 120.0,
        cwd: str | None = None,
        model: str | None = None,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model or os.environ.get("NATIVE_CLI_MINIMAX_MMX_MODEL")

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_MMX_BIN")
        if explicit:
            return explicit
        found = shutil.which("mmx")
        if not found:
            raise NativeCliUnavailable(
                "mmx CLI not found; install mmx-cli or set NATIVE_CLI_MMX_BIN"
            )
        return found

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        if session_id:
            raise NativeCliError("mmx text adapter does not support session resume")
        argv = [
            self.binary,
            "text",
            "chat",
            "--non-interactive",
            "--output",
            "json",
            "--message",
            f"user:{prompt}",
        ]
        if self._model:
            argv.extend(["--model", self._model])
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
        started = time.monotonic()
        invocation = self.build_invocation(
            prompt, session_id=session_id, cwd=cwd
        )
        try:
            output = await run_process(invocation)
        except NativeCliUnavailable:
            raise
        except NativeCliError as exc:
            raise_if_auth_failure(self.provider, str(exc))
            raise NativeCliError(
                "mmx request failed; check local CLI diagnostics"
            ) from None
        answer, usage, payload = parse_mmx_result(output.stdout)
        return NativeCliResult(
            provider=self.provider,
            text=answer,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=1,
            raw=(payload,),
        )


# mmx's own wording, read out of the mmx CLI bundle: it has a message for
# every class of failure, and it names a spent balance and a rejected key
# in its own words.
register_failure_rule(
    "minimax-mmx",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="mmx_quota_exhausted",
    patterns=(
        "quota or balance exhausted",
        "insufficient_balance_error",
        "minimax quota or balance is insufficient",
    ),
)
register_failure_rule(
    "minimax-mmx",
    FailureClass.RATE_LIMITED,
    reason_code="mmx_rate_limit_or_quota",
    patterns=("rate limit or quota exceeded",),
)
register_failure_rule(
    "minimax-mmx",
    FailureClass.AUTH_FAILED,
    reason_code="mmx_api_key_rejected",
    patterns=(
        "api key rejected",
        "oauth session expired and could not be refreshed",
    ),
)
register_failure_rule(
    "minimax-mmx",
    FailureClass.OVERLOADED,
    reason_code="mmx_temporarily_unavailable",
    patterns=("minimax is temporarily unavailable",),
)
