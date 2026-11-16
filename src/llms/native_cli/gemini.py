"""Google Gemini CLI adapter (``gemini -p ... --output-format json``).

Gemini's JSON result is ``{"response": ..., "stats": {...}}``. The CLI has no
session id in that document, so the adapter mints a UUID for the first turn and
passes it with ``--session-id``; later turns pass ``--resume <id>``. That gives
the same cache/session identity contract as DSH/Claude/Codex.
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
)
from src.llms.native_cli.process import run_process


def _first_int(raw: Any) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def _gemini_usage(stats: Any) -> NativeCliUsage:
    """Pull token counts out of Gemini's model-usage tree, best effort."""
    if not isinstance(stats, Mapping):
        return NativeCliUsage()
    flat: dict[str, Any] = {}

    def walk(node: Any) -> None:
        if not isinstance(node, Mapping):
            return
        for key, value in node.items():
            if isinstance(value, Mapping):
                walk(value)
            elif isinstance(key, str):
                flat[key] = value

    walk(stats)
    # Gemini's stats key names vary by version; take the first present spelling.
    def pick(*names: str) -> int:
        for name in names:
            if name in flat:
                return _first_int(flat[name])
        return 0

    return NativeCliUsage(
        input_tokens=pick("input_tokens", "inputTokens", "promptTokenCount"),
        output_tokens=pick("output_tokens", "outputTokens", "candidatesTokenCount"),
        cache_read_tokens=pick(
            "cached_input_tokens", "cachedInputTokens", "cachedContentTokenCount"
        ),
        cache_write_tokens=pick("cache_write_input_tokens", "cacheWriteInputTokens"),
        total_tokens=pick("total_tokens", "totalTokens", "totalTokenCount"),
    )


def parse_gemini_result(stdout: str) -> tuple[str, NativeCliUsage, Mapping[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("gemini returned no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start < 0:
            raise NativeCliError("gemini returned non-JSON output")
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError as exc:
            raise NativeCliError("gemini returned malformed JSON") from exc
    if not isinstance(payload, Mapping):
        raise NativeCliError("gemini returned a non-object result")
    error = payload.get("error")
    if error:
        message = error.get("message") if isinstance(error, Mapping) else error
        raise NativeCliError(str(message or "gemini reported an error")[:1000])
    answer = str(payload.get("response") or "").strip()
    if not answer:
        raise NativeCliError("gemini produced no final answer")
    return answer, _gemini_usage(payload.get("stats")), payload


class GeminiCliAdapter:
    provider = "gemini"

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
        explicit = self._binary or os.environ.get("NATIVE_CLI_GEMINI_BIN")
        if explicit:
            return explicit
        found = shutil.which("gemini")
        if not found:
            raise NativeCliUnavailable(
                "gemini CLI not found; install @google/gemini-cli or set NATIVE_CLI_GEMINI_BIN"
            )
        return found

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        # Gemini creates a session id itself, but does not print it in JSON
        # output mode. Minting one keeps the adapter's session contract
        # uniform and lets the store resume it on the next turn.
        next_session_id = session_id or str(uuid.uuid4())
        argv = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "json",
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
        text, usage, payload = parse_gemini_result(output.stdout)
        chosen_session = session_id or invocation.argv[
            invocation.argv.index("--session-id") + 1
        ]
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=chosen_session,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=1,
            raw=(payload,),
        )
