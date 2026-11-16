"""Xiaomi MiMo Code CLI adapter (``mimo run --format json``).

MiMo's machine mode writes one JSON event per line. Text events carry a complete
message ``part``; step-finish events carry the token/cache accounting. The CLI
persists sessions itself and accepts ``--session <id>`` to continue one.
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
    raise_if_auth_failure,
)
from src.llms.native_cli.failure import FailureClass, register_failure_rule
from src.llms.native_cli.process import run_process

DEFAULT_MODEL = "mimo-v2.6-flash"


def _usage_from_tokens(tokens: Any) -> NativeCliUsage:
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


def parse_mimo_stream(
    stdout: str,
) -> tuple[str, str | None, NativeCliUsage, list[Mapping[str, Any]]]:
    events: list[Mapping[str, Any]] = []
    text_parts: list[str] = []
    session_id: str | None = None
    usage = NativeCliUsage()
    has_error = False

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
                text_parts.append(text)
        elif kind == "step_finish" and isinstance(part, Mapping):
            usage = _usage_from_tokens(part.get("tokens"))
        elif kind == "error":
            raw_error = event.get("error")
            diagnostic = ""
            if isinstance(raw_error, Mapping):
                data = raw_error.get("data")
                if isinstance(data, Mapping):
                    diagnostic = str(data.get("message") or "")
                diagnostic = " ".join(
                    part
                    for part in (
                        diagnostic,
                        str(raw_error.get("message") or ""),
                        str(raw_error.get("name") or ""),
                    )
                    if part
                )
            else:
                diagnostic = str(raw_error or "")
            raise_if_auth_failure("mimo", diagnostic)
            has_error = True

    if not events:
        raise NativeCliError("mimo returned no machine-readable events")
    if has_error:
        raise NativeCliError("mimo returned an API error")
    if not text_parts:
        raise NativeCliError("mimo produced no final answer")
    # Each text event is a completed assistant message. The last one is the
    # final answer; concatenating would splice intermediate turns together.
    return text_parts[-1], session_id, usage, events


class MiMoCodeAdapter:
    """Run one MiMo Code turn and reuse its native Session."""

    provider = "mimo"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 120.0,
        cwd: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        yolo: bool = False,
        pure: bool = False,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._model = model or DEFAULT_MODEL
        self._api_key = api_key
        self._base_url = base_url
        self._mcp_servers = list(mcp_servers or ())
        self._yolo = yolo
        self._pure = pure

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_MIMO_BIN")
        if explicit:
            return explicit
        found = shutil.which("mimo")
        if not found:
            raise NativeCliUnavailable(
                "mimo CLI not found; install mimo-code or set NATIVE_CLI_MIMO_BIN"
            )
        return found

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        argv = [self.binary, "run", "--format", "json"]
        if self._pure:
            argv.append("--pure")
        if self._yolo:
            argv.append("--yolo")
        if self._model:
            cli_model = (
                self._model if "/" in self._model else f"xiaomi/{self._model}"
            )
            argv.extend(["--model", cli_model])
        if session_id:
            argv.extend(["--session", session_id])
        if cwd is not None or self._cwd is not None:
            argv.extend(["--dir", cwd if cwd is not None else str(self._cwd)])
        # ``--`` keeps a prompt beginning with ``-`` from being read as an option.
        argv.extend(["--", prompt])
        env = None
        if self._api_key:
            import json

            model_ref = self._model or "xiaomi/mimo-v2.6-pro"
            bare_model = model_ref.split("/", 1)[-1]
            base = self._base_url or "https://token-plan-sgp.xiaomimimo.com/v1"
            mcp_config: dict[str, Any] = {}
            for server in self._mcp_servers:
                name = str(server.get("name") or "").strip()
                if not name:
                    continue
                url = str(server.get("url") or "").strip()
                if url:
                    headers = {
                        str(k): str(v)
                        for k, v in (server.get("headers") or {}).items()
                    }
                    mcp_config[name] = {
                        "type": "remote",
                        "url": url,
                        **({"headers": headers} if headers else {}),
                        "enabled": True,
                    }
                    continue
                command = str(server.get("command") or "").strip()
                if not command:
                    continue
                mcp_config[name] = {
                    "type": "local",
                    "command": [command, *[str(a) for a in (server.get("args") or [])]],
                    "environment": {
                        str(k): str(v)
                        for k, v in (server.get("env") or {}).items()
                    },
                    "enabled": True,
                }
            config: dict[str, Any] = {
                "$schema": "https://mimo.xiaomi.com/mimocode/config.json",
                "model": model_ref if "/" in model_ref else f"xiaomi/{model_ref}",
                "provider": {
                    "xiaomi": {
                        "options": {
                            "baseURL": base,
                            "apiKey": "{env:MIMOCODE_MIMO_API_KEY}",
                        },
                        "models": {bare_model: {"name": bare_model}},
                    }
                },
                # Native threads own one workspace mirror. Deny file-tool
                # access outside it even though the thread runs with --yolo.
                "permission": {
                    "external_directory": "deny",
                    "edit": "allow",
                    "write": "allow",
                },
            }
            if mcp_config:
                config["mcp"] = mcp_config
            env = {
                "MIMOCODE_MIMO_API_KEY": self._api_key,
                "MIMOCODE_CONFIG_CONTENT": json.dumps(config),
            }
        return CliInvocation(
            argv=tuple(argv),
            cwd=cwd if cwd is not None else self._cwd,
            env=env,
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
            # CLI diagnostics can include request details. Keep the public
            # error safe while still classifying sign-in and API-key failures.
            raise_if_auth_failure(self.provider, str(exc))
            if str(exc).startswith("native CLI timed out after "):
                raise NativeCliError(str(exc)) from None
            raise NativeCliError(
                "mimo request failed; check local CLI diagnostics"
            ) from None
        text, new_session_id, usage, events = parse_mimo_stream(output.stdout)
        return NativeCliResult(
            provider=self.provider,
            text=text,
            session_id=new_session_id or session_id,
            usage=usage,
            duration_seconds=time.monotonic() - started,
            events=len(events),
            raw=tuple(events),
        )


# MiMo's own wording, read out of the MiMo Code CLI binary: it maps the
# provider's error codes onto its own messages, and those messages are what
# reaches a diagnostic.
register_failure_rule(
    "mimo",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="mimo_context_overflow",
    patterns=(
        "input exceeds context window of this model",
        "context_window_exceeded",
    ),
)
register_failure_rule(
    "mimo",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="mimo_quota_exceeded",
    patterns=(
        "quota exceeded. check your plan and billing details",
        "usage_not_included",
    ),
)
register_failure_rule(
    "mimo",
    FailureClass.OVERLOADED,
    reason_code="mimo_server_overloaded",
    patterns=("server_is_overloaded",),
)
register_failure_rule(
    "mimo",
    FailureClass.AUTH_FAILED,
    reason_code="mimo_login_required",
    patterns=("not logged in. run `mimo auth login`",),
)
