"""Moonshot Kimi CLI adapter (``kimi --prompt <p> --output-format stream-json``).

Kimi has no cache counters in its print stream, so this adapter does not claim
cache verification. It does reuse the native session, and the id of that
session is Kimi's own.

Kimi 2.5 resumes a session and creates none under a caller's id:
``--session <id>`` looks the id up and answers ``Session "<id>" not found``
when Kimi never created it. So a first turn takes no session flag at all; Kimi
creates the session and names it in its stream (a ``session.resume_hint``
meta event carrying ``session_id``). The next turn resumes that id.

``--continue`` is never used: it continues whichever session the working
directory saw last, and two lines can share a directory. An earlier adapter
stored the word ``continue`` in place of an id; a turn that finds it starts a
fresh session (see ``resumable_session_id``).
"""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any, Mapping, Sequence

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
    NativeCliUsage,
    raise_if_auth_failure,
    resumable_session_id,
)
from src.llms.native_cli.failure import FailureClass, register_failure_rule
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


def parse_kimi_stream(stdout: str) -> tuple[str, list[Mapping[str, Any]]]:
    """Take the latest assistant message from Kimi's JSONL stream."""
    answer = ""
    events: list[Mapping[str, Any]] = []
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
        if event.get("error"):
            raw_error = event.get("error")
            diagnostic = (
                " ".join(
                    str(raw_error.get(key) or "")
                    for key in ("code", "type", "message")
                )
                if isinstance(raw_error, Mapping)
                else str(raw_error)
            )
            raise_if_auth_failure("kimi", diagnostic)
            raise NativeCliError("kimi reported an error")
        if event.get("role") == "assistant":
            text = _text_from_content(event.get("content"))
            if text:
                answer = text
    if not events:
        raise NativeCliError("kimi returned no machine-readable events")
    if not answer:
        raise NativeCliError("kimi produced no final answer")
    return answer, events


def kimi_session_id(events: Sequence[Mapping[str, Any]]) -> str | None:
    """The id of the session Kimi ran, as Kimi itself reported it.

    Only Kimi's own ``{"role": "meta", "type": "session.resume_hint",
    "session_id": ...}`` event counts. The worker's stdout and stderr share
    one log, so a line from an MCP server, a child process or a Kimi subagent
    that happens to carry a ``session_id`` is not Kimi's session. The last
    valid hint wins. A stream without one gives None: the id is never
    invented here.
    """
    reported: str | None = None
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("role") != "meta" or event.get("type") != "session.resume_hint":
            continue
        value = resumable_session_id(event.get("session_id"))
        if value:
            reported = value
    return reported


class KimiCliAdapter:
    provider = "kimi"

    # Kimi resumes only a session it created (``--session <id>``) and has no
    # flag that starts one under a caller's id. Dispatch reads this.
    session_modes: frozenset[str] = frozenset({"resume"})

    # A self-report turn runs from the launcher's directory, not the line
    # worktree the session belongs to, so it cannot reach the line's session.
    # Dispatch reads this and writes the deterministic note instead.
    supports_self_report: bool = False

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
        explicit = self._binary or os.environ.get("NATIVE_CLI_KIMI_BIN")
        if explicit:
            return explicit
        found = shutil.which("kimi")
        if not found:
            raise NativeCliUnavailable(
                "kimi CLI not found; install kimi-cli or set NATIVE_CLI_KIMI_BIN"
            )
        return found

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        # kimi-code 2.x: one-shot mode is -p/--prompt (no --print, no stdin
        # prompt); --auto cannot be combined with --prompt.
        argv = [
            self.binary,
            "--prompt",
            prompt,
            "--output-format",
            "stream-json",
        ]
        if self._model:
            argv.extend(["--model", self._model])
        resume = resumable_session_id(session_id)
        if resume:
            # Resume the session Kimi created. Without an id Kimi starts a new
            # session and names it in its stream. Never --continue.
            argv.extend(["--session", resume])
        return CliInvocation(
            argv=tuple(argv),
            stdin="",
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
        text, events = parse_kimi_stream(output.stdout)
        return NativeCliResult(
            provider=self.provider,
            text=text,
            # The session Kimi ran, named by Kimi; the next turn resumes it.
            session_id=kimi_session_id(events) or resumable_session_id(session_id),
            usage=NativeCliUsage(),
            duration_seconds=time.monotonic() - started,
            events=len(events),
            raw=tuple(events),
        )


# Kimi's own wording, read out of the Kimi CLI package itself (kimi-code
# 2.1.1): it names a context overflow by its own error code and its own
# message, and it keeps "the request is too large" in a separate group from
# the context overflow -- so the two are registered apart here as well.
# The request-too-large group is read with the word beside it: "request too
# large" with a word about tokens, a context or a length is a context that
# ran out, and the class that rotates the session belongs to that one.
# Without such a word the request was simply too big for the provider to
# accept, which is a crash of the turn.
register_failure_rule(
    "kimi",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="kimi_context_overflow",
    patterns=(
        "context_length_exceeded",
        "input token count exceeds maximum number of tokens",
        "input token count exceeds the maximum number of tokens",
        "exceeds the context window",
    ),
)
register_failure_rule(
    "kimi",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="kimi_request_too_large_context",
    patterns=("request too large",),
    requires=("token", "context", "length"),
)
# Kimi 2.5 resuming an id it does not know prints exactly
# `error: failed to run prompt: Session "<id>" not found.` (verified against
# kimi 2.5.2). The stored id is dead; dispatch drops it and runs fresh once
# (N22). Match the quoted form only: every stream-json log mentions
# "session" (the session.resume_hint line), so a bare "not found" such as
# "command not found" must not count as a lost session.
register_failure_rule(
    "kimi",
    FailureClass.CRASH,
    reason_code="kimi_session_not_found",
    patterns=('" not found',),
    requires=('session "',),
)
register_failure_rule(
    "kimi",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="kimi_insufficient_quota",
    patterns=("insufficient_quota",),
)
register_failure_rule(
    "kimi",
    FailureClass.RATE_LIMITED,
    reason_code="kimi_rate_limit_exceeded",
    patterns=("rate_limit_exceeded",),
)
register_failure_rule(
    "kimi",
    FailureClass.OVERLOADED,
    reason_code="kimi_provider_overloaded",
    patterns=("overload",),
)
register_failure_rule(
    "kimi",
    FailureClass.CRASH,
    reason_code="kimi_request_too_large",
    patterns=(
        "payload too large",
        "request too large",
        "request entity too large",
        "request exceeds the maximum size",
    ),
)
