"""xAI Grok CLI adapter.

Headless surfaces:

- ``grok -p <prompt> --output-format json`` returns one result document;
- ``--output-format streaming-json`` returns NDJSON ACP-style session updates
  (text / thought / tool_call / tool_call_update / usage / end).

Native threads use the streaming form; `--permission-mode auto` lets the CLI
own its tool loop while staying in the native lane. No approval bridge is
claimed for Grok.

The session id is Grok's own. Grok resumes a session it created
(``--resume <id>``) and reports the id it ran (``sessionId`` in the result
document and in the streaming ``end`` event). A caller-owned id is never sent:
``--resume`` of an id Grok never saved fails with ``No saved copy of session
<id> was found remotely``. ``--continue`` is never used either.
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
    resumable_session_id,
)
from src.llms.native_cli.failure import FailureClass, register_failure_rule
from src.llms.native_cli.process import run_process
from src.llms.native_cli.isolation import current_worker_isolation


def ensure_grok_approval_home() -> str:
    """Isolated Grok home with ask-before-edit behavior.

    The user's ~/.grok/config.toml may set permission_mode = "always-approve";
    ACP permissions then never ask. This copies only the cached auth into a
    private cache dir and writes config.toml with permission_mode = "default".
    """
    from pathlib import Path as _Path

    isolation = current_worker_isolation()
    if isolation is not None:
        # A worker must never import the daemon user's shared ~/.grok auth.
        # Provision credentials into this workspace home through the local policy.
        root = isolation.home / ".grok"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (root / "auth.json").is_file():
            raise NativeCliUnavailable("Grok has no workspace-scoped credentials")
        return str(root)
    root = _Path(
        os.environ.get("NATIVE_CLI_GROK_HOME")
        or (_Path.home() / ".cache" / "conductor" / "native-cli" / "grok-home")
    )
    root.mkdir(parents=True, exist_ok=True)
    source_auth = _Path.home() / ".grok" / "auth.json"
    if not source_auth.is_file():
        raise NativeCliUnavailable("Grok is not logged in: ~/.grok/auth.json missing")
    target_auth = root / "auth.json"
    try:
        if (
            not target_auth.is_file()
            or source_auth.stat().st_mtime > target_auth.stat().st_mtime
        ):
            target_auth.write_bytes(source_auth.read_bytes())
            os.chmod(target_auth, 0o600)
    except OSError as exc:
        raise NativeCliUnavailable("cannot prepare isolated Grok auth") from exc
    config = root / "config.toml"
    text = config.read_text() if config.is_file() else ""
    if "permission_mode" in text:
        lines = []
        for line in text.splitlines():
            if line.strip().startswith("permission_mode"):
                lines.append('permission_mode = "default"')
            else:
                lines.append(line)
        text = "\n".join(lines) + "\n"
    else:
        text = (
            text.rstrip()
            + ("\n" if text.strip() else "")
            + 'permission_mode = "default"\n'
        )
    config.write_text(text)
    return str(root)


def parse_grok_result(
    stdout: str,
) -> tuple[str, str | None, NativeCliUsage, Mapping[str, Any]]:
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("grok returned no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise NativeCliError("grok returned non-JSON output") from exc
    if not isinstance(payload, Mapping):
        raise NativeCliError("grok returned an unexpected JSON shape")
    error = payload.get("error")
    if error:
        raise NativeCliError(str(error)[:1000])
    answer = str(payload.get("text") or "").strip()
    if not answer:
        raise NativeCliError("grok produced no final answer")
    return (
        answer,
        grok_session_id(payload),
        NativeCliUsage.from_mapping(payload.get("usage")),
        payload,
    )


def grok_session_id(payload: Mapping[str, Any]) -> str | None:
    """The id of the session Grok ran, from its single ``json`` result document."""
    return resumable_session_id(payload.get("sessionId"))


def grok_stream_session_id(stdout: str) -> str | None:
    """The session id Grok reported in a ``streaming-json`` NDJSON log.

    Only Grok's own ``{"type": "end", "sessionId": ...}`` event counts. The
    worker's stdout and stderr share one log, so another JSON line that
    carries a ``sessionId`` (an MCP server, a child process) is not Grok's
    session. The last valid end event wins.
    """
    reported: str | None = None
    for raw in (stdout or "").splitlines():
        try:
            event = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, Mapping) or event.get("type") != "end":
            continue
        value = resumable_session_id(event.get("sessionId"))
        if value:
            reported = value
    return reported


class GrokCliAdapter:
    provider = "grok"

    # Grok resumes only a session it created and saved. Dispatch reads this:
    # a fresh run takes no id, and the id Grok reports is what a later turn
    # resumes.
    session_modes: frozenset[str] = frozenset({"resume"})

    # A self-report turn runs from the launcher's directory, not the line
    # worktree, so it cannot reach the line's session. Unsupported until it
    # runs in the line worktree.
    supports_self_report: bool = False

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
        model: str | None = None,
        read_only: bool = True,
        mode: str | None = None,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model or os.environ.get("NATIVE_CLI_GROK_MODEL")
        self._read_only = read_only
        self._mode = mode

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_GROK_BIN")
        if explicit:
            return explicit
        found = shutil.which("grok")
        if not found:
            raise NativeCliUnavailable("grok CLI not found; set NATIVE_CLI_GROK_BIN")
        return found

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
        stream: bool = False,
    ) -> CliInvocation:
        permission = self._mode or ("plan" if self._read_only else "auto")
        argv = [
            self.binary,
            "-p",
            prompt,
            "--output-format",
            "streaming-json" if stream else "json",
            "--permission-mode",
            permission,
        ]
        if self._model:
            argv.extend(["--model", self._model])
        effective_cwd = cwd if cwd is not None else self._cwd
        if effective_cwd:
            argv.extend(["--cwd", str(effective_cwd)])
        resume = resumable_session_id(session_id)
        if resume:
            # Only an id Grok created and reported reaches --resume.
            argv.extend(["--resume", resume])
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
        output = await run_process(
            self.build_invocation(prompt, session_id=session_id, cwd=cwd)
        )
        text, new_session_id, usage, _payload = parse_grok_result(output.stdout)
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=new_session_id or resumable_session_id(session_id),
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=1,
        )


# Grok's own wording, read out of the Grok CLI binary: it names a context
# that did not fit, a spent free allowance and an overloaded service under
# its own error codes.
register_failure_rule(
    "grok",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="grok_context_size_exceeded",
    patterns=(
        "exceed_context_size_error",
        "too long for this model",
        "maximum prompt length",
    ),
)
# Grok resuming an id it never saved: `No saved copy of session <id> was
# found remotely`. The stored id is dead; dispatch drops it and runs fresh
# once (N22).
register_failure_rule(
    "grok",
    FailureClass.CRASH,
    reason_code="grok_session_not_saved",
    patterns=("no saved copy of session",),
)
register_failure_rule(
    "grok",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="grok_free_usage_exhausted",
    patterns=(
        "free-usage-exhausted",
        "free_usage_exhausted",
        "you've reached your free grok build usage limit",
    ),
)
register_failure_rule(
    "grok",
    FailureClass.OVERLOADED,
    reason_code="grok_service_unavailable",
    patterns=(
        "service_unavailable_error",
        "grok is temporarily overloaded",
        "model is temporarily overloaded",
    ),
)
register_failure_rule(
    "grok",
    FailureClass.AUTH_FAILED,
    reason_code="grok_login_required",
    patterns=(
        "run `grok login`",
        "token expired. run `grok login`",
    ),
)
# Grok stopping at `--max-turns`: it prints `Error: max turns reached` on its
# own line and exits non-zero, and its streaming `end` event says
# `"stopReason":"max_turn_requests"` (the generic terminal rule). The run
# answered every request before the stop, so this outranks any credential
# wording the model or a tool left in the same log: it is a spent turn
# budget, worth continuing, never a login failure.
register_failure_rule(
    "grok",
    FailureClass.TURN_BUDGET_EXHAUSTED,
    reason_code="grok_max_turns_reached",
    line_patterns=(r"^\s*error:\s*max turns reached\b",),
    terminal=True,
)
