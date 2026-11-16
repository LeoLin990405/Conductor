"""DeepSeek through the DSH headless profile and its JSON event stream."""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.llms.native_cli.base import (
    CliInvocation,
    NativeCliError,
    NativeCliResult,
    NativeCliUnavailable,
)
from src.llms.native_cli.events import parse_ndjson
from src.llms.native_cli.failure import FailureClass, register_failure_rule
from src.llms.native_cli.process import run_process
from src.llms.native_cli.isolation import (
    approved_env_value,
    current_worker_isolation,
    private_home,
    secure_private_write,
)

_LOAD_DEEPSEEK_ENV = (
    "set +x; set -a; "
    'if [ -n "${DEEPSEEK_API_KEY:-}" ]; then :; '
    'elif [ -r "$HOME/.config/deepseek/env" ]; then '
    '. "$HOME/.config/deepseek/env" >/dev/null 2>&1 || exit 78; '
    "else printf 'deepseek_api_key_unavailable\\n' >&2; exit 78; fi; "
    'set +a; set +x; exec "$@"'
)
_LOAD_DEEPSEEK_ENV_ACCOUNT = (
    "set +x; set -a; "
    'if [ -n "${DEEPSEEK_API_KEY:-}" ]; then :; '
    'elif [ -r "$HOME/.config/deepseek/env" ]; then '
    '. "$HOME/.config/deepseek/env" >/dev/null 2>&1 || exit 78; '
    "else exit 78; fi; "
    'set +a; set +x; exec "$@"'
)
_TIMEOUT_ERROR = re.compile(r"native CLI timed out after \d+s\Z")
_MISSING_KEY_ERROR = re.compile(
    r"native CLI exited 78: deepseek_api_key_unavailable\Z"
)


def _safe_process_error(exc: NativeCliError, timeout_seconds: float) -> NativeCliError:
    """Keep credentials and vendor diagnostics out of tool and stream output."""
    message = str(exc)
    if isinstance(exc, NativeCliUnavailable) and message.startswith(
        "dsh CLI not found; install @deepseek-ai/dsh or set NATIVE_CLI_DSH_BIN"
    ):
        return exc
    if _TIMEOUT_ERROR.fullmatch(message):
        return NativeCliError(f"dsh CLI timed out after {timeout_seconds:.0f}s")
    if _MISSING_KEY_ERROR.fullmatch(message):
        return NativeCliError("DeepSeek API key unavailable")
    # Keep the actionable runtime cause while dropping dyld's full library
    # paths and search list. The daemon's classifier recognizes this fixed
    # marker as a missing native dependency.
    lowered = message.lower()
    if "dyld" in lowered and "library not loaded" in lowered:
        return NativeCliError("dyld: library not loaded")
    return NativeCliError("dsh CLI failed")


class DeepSeekDshAdapter:
    """Run one DeepSeek/DSH headless turn.

    Parameters are env-overridable so a deployment can pin the binary and its
    harness home without changing the Python call site:
    ``NATIVE_CLI_DSH_BIN``, ``NATIVE_CLI_DSH_HOME``, ``NATIVE_CLI_DSH_PROFILE``.
    """

    provider = "deepseek-dsh"

    def __init__(
        self,
        *,
        binary: str | None = None,
        harness_home: str | None = None,
        profile: str = "headless",
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
        account_auth: bool | None = None,
        model: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        self._binary = binary
        self._harness_home = harness_home
        self._profile = profile
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._account_auth = account_auth
        self._model = model
        self._mcp_servers = list(mcp_servers or ())

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_DSH_BIN")
        if explicit:
            return explicit
        found = shutil.which("dsh")
        if not found:
            raise NativeCliUnavailable(
                "dsh CLI not found; install @deepseek-ai/dsh or set NATIVE_CLI_DSH_BIN"
            )
        return found

    @property
    def harness_home(self) -> str:
        isolation = current_worker_isolation()
        explicit = self._harness_home or approved_env_value("NATIVE_CLI_DSH_HOME")
        if isolation is not None:
            if explicit and not Path(explicit).expanduser().resolve().is_relative_to(
                isolation.home.resolve()
            ):
                raise NativeCliUnavailable("DeepSeek home is outside workspace home")
            return str(
                Path(explicit).expanduser() if explicit else private_home() / ".dsh"
            )
        if explicit:
            return explicit
        # The DSH desktop install keeps profiles and account grants in ~/.dsh.
        # The old private cache default has no account credentials; prefer the
        # real home when it looks like a DSH installation.
        home = Path.home() / ".dsh"
        if (home / "profiles" / "headless").is_dir():
            return str(home)
        return str(Path.home() / ".cache" / "conductor" / "dsh")

    @property
    def account_auth(self) -> bool:
        if self._account_auth is not None:
            return self._account_auth
        env = (approved_env_value("NATIVE_CLI_DSH_ACCOUNT") or "").strip().lower()
        if env in {"1", "true", "yes", "on"}:
            return True
        if env in {"0", "false", "no", "off"}:
            return False
        # Authentication is loaded only by the child shell from the operator's
        # env file. Never inspect DSH credential files or the parent process env.
        return False

    def _account_patch_path(self) -> Path | None:
        """Overlay account, MCP and isolation settings onto the private profile."""
        outer_sandboxed = current_worker_isolation() is not None
        if not self.account_auth and not self._mcp_servers and not outer_sandboxed:
            return None
        import hashlib
        import json

        lines: list[str] = []
        if self.account_auth:
            model = self._model or "deepseek-flash"
            lines.extend(
                [
                    "- id: credentials",
                    "  disabled: false",
                    "- id: authorization",
                    "  disabled: false",
                    "- id: deepseek-account",
                    "  disabled: false",
                    "- id: agent-default-model",
                    "  config:",
                    "    provider: deepseek-account",
                    f"    model: {model}",
                ]
            )
        if outer_sandboxed:
            # DSH 0.2.1-rc.2's shipped headless profile uses these entry ids.
            # Its danger-full-access preset disables its nested sandbox and
            # sets approval to "never"; worker_isolation remains the boundary.
            lines.extend(
                [
                    "- id: sandbox-policy",
                    "  config:",
                    "    mode: danger-full-access",
                    "    workspaceRoot: !!js process.cwd()",
                    "- id: approval",
                    "  config:",
                    "    policy: never",
                ]
            )
        mcp_lines: list[str] = []
        for server in self._mcp_servers:
            name = str(server.get("name") or "").strip()
            command = str(server.get("command") or "").strip()
            if not name or not (command or server.get("url")):
                continue
            url = str(server.get("url") or "").strip()
            args = [str(arg) for arg in (server.get("args") or [])]
            if url:
                headers = json.dumps(dict(server.get("headers") or {}))
                mcp_lines.extend(
                    [
                        f"    - id: native-mcp-{name}",
                        "      name: '@deepseek-ai/dsh-mcp-client'",
                        "      config:",
                        "        transport: streamable-http",
                        f"        serverName: {name}",
                        f"        url: {json.dumps(url)}",
                        f"        headers: {headers}",
                    ]
                )
                continue
            env_map = {str(k): str(v) for k, v in (server.get("env") or {}).items()}
            env_lines = [
                f"        env: {json.dumps(env_map)}" if env_map else "        env: {}"
            ]
            mcp_lines.extend(
                [
                    f"    - id: native-mcp-{name}",
                    "      name: '@deepseek-ai/dsh-mcp-client'",
                    "      config:",
                    "        transport: stdio",
                    f"        serverName: {name}",
                    f"        command: {json.dumps(command)}",
                    f"        args: {json.dumps(args)}",
                    *env_lines,
                    "        cwd: ''",
                ]
            )
        if mcp_lines:
            lines.append("- insert:")
            lines.extend(mcp_lines)
        body = "\n".join(lines) + "\n"
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]
        path = Path(self.harness_home) / f"alpha-native-patch-{digest}.yml"
        try:
            if current_worker_isolation() is not None:
                secure_private_write(path, body.encode("utf-8"))
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_file() or path.read_text() != body:
                path.write_text(body)
                path.chmod(0o600)
        except OSError as exc:
            raise NativeCliError(
                f"dsh native patch could not be written at {path}: {exc}"
            ) from exc
        return path

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        argv = [
            "/bin/sh",
            "-c",
            _LOAD_DEEPSEEK_ENV_ACCOUNT if self.account_auth else _LOAD_DEEPSEEK_ENV,
            "conductor-dsh",
            self.binary,
            self._profile,
        ]
        patch = self._account_patch_path()
        if patch is not None:
            argv.extend(["--patch", str(patch)])
        argv.append("--json")
        if session_id:
            argv.extend(["--session-id", session_id])
        # The outer launcher passes this delimiter through after --json; the
        # headless parser consumes it so option-shaped prompts stay literal.
        argv.extend(["--", prompt])
        env = {
            "DSH_HOME": self.harness_home,
            "NATIVE_CLI_DSH_PROFILE": self._profile,
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
        except NativeCliError as exc:
            raise _safe_process_error(exc, self._timeout_seconds) from None
        parsed = parse_ndjson(output.stdout)
        if not parsed.text:
            raise NativeCliError("native CLI produced no final answer")
        return NativeCliResult(
            provider=self.provider,
            text=parsed.text,
            session_id=parsed.session_id or session_id,
            usage=parsed.usage,
            duration_seconds=time.monotonic() - started,
            events=len(parsed.events),
            raw=tuple(parsed.events),
        )


def adapt_result(result: NativeCliResult, **overrides: Any) -> NativeCliResult:
    """Small helper for callers that need to attach their own metadata."""
    return replace(result, **overrides)


# DSH's own wording, read out of the DSH harness packages: it routes on
# stable codes of its own (CONTEXT_WINDOW_EXCEEDED, ACCOUNT_QUOTA,
# ACCOUNT_SIGN_IN_REQUIRED), and those are the codes that reach a diagnostic.
register_failure_rule(
    "deepseek-dsh",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="deepseek_context_window_exceeded",
    patterns=(
        "context_window_exceeded",
        "too large for the model context",
    ),
)
register_failure_rule(
    "deepseek-dsh",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="deepseek_account_quota_exceeded",
    patterns=(
        "account_quota",
        "insufficient_credits",
        "usage_limit_exceeded",
        "credits_exhausted",
        "balance_exhausted",
        "out of budget",
    ),
)
register_failure_rule(
    "deepseek-dsh",
    FailureClass.AUTH_FAILED,
    reason_code="deepseek_api_key_unavailable",
    patterns=("deepseek api key unavailable",),
)
register_failure_rule(
    "deepseek-dsh",
    FailureClass.AUTH_FAILED,
    reason_code="deepseek_account_sign_in_required",
    patterns=(
        "account_sign_in_required",
        "account_token_invalid",
    ),
)
register_failure_rule(
    "deepseek-dsh",
    FailureClass.RATE_LIMITED,
    reason_code="deepseek_rate_limit",
    patterns=("rate_limit",),
)
register_failure_rule(
    "deepseek-dsh",
    FailureClass.NETWORK,
    reason_code="deepseek_transport_failed",
    patterns=("fetch failed",),
)
register_failure_rule(
    "deepseek-dsh",
    FailureClass.CRASH,
    reason_code="deepseek_runtime_dependency_missing",
    patterns=("library not loaded",),
    requires=("dyld",),
)
