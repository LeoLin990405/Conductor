"""Alibaba Qwen Code CLI adapter (``qwen -p ... --output-format json``).

Qwen's JSON mode returns the full conversation as an array; the final element
of type ``result`` carries ``result``, ``is_error``, ``session_id`` and usage.
Session reuse is ``--session-id`` on the first turn and ``--resume`` afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from typing import Any, Mapping

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
    NativeCliUsage,
    raise_if_auth_failure,
)
from src.llms.native_cli.process import run_process


def parse_qwen_result(stdout: str) -> tuple[str, str | None, NativeCliUsage, Mapping[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("qwen returned no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NativeCliError("qwen returned non-JSON output") from exc
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise NativeCliError("qwen returned an unexpected JSON shape")
    result: Mapping[str, Any] | None = None
    for item in payload:
        if isinstance(item, Mapping) and item.get("type") == "result":
            result = item
    if result is None:
        raise NativeCliError("qwen returned no result object")
    if result.get("is_error"):
        message = str(result.get("result") or "qwen reported an error")
        raise_if_auth_failure("qwen", message)
        raise NativeCliError(message[:1000])
    answer = str(result.get("result") or "").strip()
    if not answer:
        raise NativeCliError("qwen produced no final answer")
    session_id = str(result.get("session_id") or "") or None
    return answer, session_id, NativeCliUsage.from_mapping(result.get("usage")), result


class QwenCliAdapter:
    provider = "qwen"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
        model: str | None = None,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_QWEN_BIN")
        if explicit:
            return explicit
        found = shutil.which("qwen")
        if not found:
            raise NativeCliUnavailable(
                "qwen CLI not found; install @qwen-code/qwen-code or set NATIVE_CLI_QWEN_BIN"
            )
        return found

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        next_session_id = session_id or str(uuid.uuid4())
        argv = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "json",
            # Plan mode is read-only: the external agent must not mutate the
            # user's workspace by accident.
            "--approval-mode",
            "plan",
        ]
        if self._model:
            argv.extend(["--model", self._model])
        if session_id:
            argv.extend(["--resume", session_id])
        else:
            argv.extend(["--session-id", next_session_id])
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
        text, new_session_id, usage, payload = parse_qwen_result(output.stdout)
        chosen_session = session_id or invocation.argv[
            invocation.argv.index("--session-id") + 1
        ]
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=new_session_id or chosen_session,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=1,
            raw=(payload,),
        )
