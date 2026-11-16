"""Z.ai / ZCode CLI adapter.

Two headless grammars are supported:

* the official ZCode app CLI (``zcode``, the default): ``--prompt``,
  ``--mode``, ``--surface terminal``, ``--output-format stream-json``;
* the clean-room ``zcode-cli``, only when an explicit binary override selects
  it: ``--prompt``, ``--mode``, ``--model``, ``--max-turns``,
  ``--output-format stream-json``.

Both CLIs stop a long run at a turn budget and then report an empty, failed
result; ``zcode-cli`` does that after 40 turns by default. The adapter
therefore always passes an explicit mode, model and turn budget, reads the
final result out of the event stream instead of trusting the exit code, and
reports an exhausted turn budget as its own error rather than a generic
failure.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
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
    classify_failure,
    register_failure_rule,
)
from src.llms.native_cli.isolation import (
    approved_env_value,
    current_worker_isolation,
    private_home,
    secure_private_write,
)
from src.llms.native_cli.process import run_process
from src.llms.native_cli.zcode_transcript import (
    ZCodeMaxTurnsError,
    exhausted_turn_budget,
    final_result,
    iter_json_objects,
    turn_failure,
)

_DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/anthropic"
_DEFAULT_MODEL = "GLM-5.3"
_DEFAULT_MAX_TURNS = 200
_PROVIDER_ID = "alpha-zai"

_LOCK_PATH = Path("/tmp/conductor-native-cli-zcode.lock")
_LOCK_POLL_SECONDS = 0.05
# Captured at import time on purpose: the provider lock must always yield to
# the event loop, even when a test patches ``asyncio.sleep`` to observe the
# retry backoff. Patching it would turn the lock poll into a busy loop that
# starves the loop and hangs the whole process.
_lock_sleep = asyncio.sleep
_MAX_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 1.0
_RETRY_MAX_SECONDS = 2.0
_RETRYABLE_STATUS_RE = re.compile(
    r"(?:\b(?:http|status(?:\s+code)?|error\s+code)\s*[:=]?\s*(429|502)\b"
    r"|\b(?:api\s+)?error\s*[:=]\s*(429|502)\b"
    r"|\b(429|502)\s+(?:too many requests|rate limit|bad gateway))",
    re.IGNORECASE,
)


def _retryable_status(detail: str) -> int | None:
    """The retryable HTTP status in a diagnostic, if it is one."""
    match = _RETRYABLE_STATUS_RE.search(detail or "")
    if not match:
        return None
    return int(next(group for group in match.groups() if group is not None))


def _payload_retryable_status(payload: Mapping[str, Any]) -> int | None:
    """The retryable HTTP status a structured result reports, if any.

    Only structured status fields count. Answer text that happens to mention
    a status code is a successful answer, not a rate limit.
    """
    candidates = [
        payload.get("status_code"),
        payload.get("statusCode"),
        payload.get("status"),
    ]
    raw_error = payload.get("error")
    if isinstance(raw_error, Mapping):
        candidates.extend(
            raw_error.get(key) for key in ("status", "status_code", "statusCode", "code")
        )
    for value in candidates:
        if value is not None and str(value).strip() in {"429", "502"}:
            return int(str(value).strip())
    return None


def _resolve_max_turns(value: int | None) -> int:
    """The turn budget, falling back to the default for unusable input."""
    raw = os.environ.get("NATIVE_CLI_ZCODE_MAX_TURNS", "").strip()
    if value is not None:
        raw = str(value)
    if not raw:
        return _DEFAULT_MAX_TURNS
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TURNS
    return parsed if parsed > 0 else _DEFAULT_MAX_TURNS


def parse_zcode_result(
    stdout: str,
) -> tuple[str, str | None, NativeCliUsage, Mapping[str, Any]]:
    """Read the final result out of a ZCode transcript.

    The transcript mixes session events with the terminal result object, and
    the CLI exits non-zero both for a failed turn and for an exhausted turn
    budget. The transcript decides the outcome: a ``turn.completed`` event
    whose ``resultType`` is ``error_max_turns`` is a budget problem and is
    reported as such, not as a generic failure.
    """
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("zcode returned no output")
    events = list(iter_json_objects(text))
    if not events:
        raise NativeCliError("zcode returned non-JSON output")

    payload = final_result(events)
    retryable = _payload_retryable_status(payload)
    if retryable:
        raise NativeCliError(f"HTTP {retryable}")

    if exhausted_turn_budget(events, payload):
        raise ZCodeMaxTurnsError(
            "zcode reached its turn budget (max_turns); retry with more turns"
        )

    error = payload.get("error")
    if error:
        message = error.get("message") if isinstance(error, Mapping) else error
        diagnostic = (
            " ".join(str(error.get(key) or "") for key in ("code", "type", "message"))
            if isinstance(error, Mapping)
            else str(error)
        )
        raise_if_auth_failure("zcode", diagnostic)
        raise NativeCliError(str(message or "zcode reported an error")[:1000])

    failure = turn_failure(events)
    if failure:
        raise_if_auth_failure("zcode", failure)
        raise NativeCliError(failure[:1000] or "zcode reported an error")

    if payload.get("isError") is True:
        raise NativeCliError("zcode reported an error")

    answer = str(payload.get("response") or "").strip()
    if not answer:
        # The result also carries intermediate responses in turnResponses.
        turns = payload.get("turnResponses")
        if isinstance(turns, list) and turns:
            answer = str(turns[-1] or "").strip()
    if not answer:
        raise NativeCliError("zcode produced no final answer")

    session_id = str(payload.get("sessionId") or "") or None
    return (
        answer,
        session_id,
        NativeCliUsage.from_mapping(payload.get("usage")),
        payload,
    )


def _is_clean_room_cli(binary: str) -> bool:
    """Whether the binary speaks the clean-room ``zcode-cli`` grammar.

    Two steps, the same rule the launch script uses: the invocation name
    (before any link is resolved) wins, then the resolved target's name wins.
    A bare command name is looked up on PATH first, like the shell's
    ``command -v``: ``Path.resolve()`` would resolve it against the current
    directory and miss a PATH override such as a symlinked ``glm-custom``.
    """
    path = Path(binary)
    if path.name == "zcode-cli":
        return True
    if not path.is_absolute() and path.parent == Path("."):
        found = shutil.which(binary)
        if not found:
            return False
        path = Path(found)
        if path.name == "zcode-cli":
            return True
    try:
        return path.resolve().name == "zcode-cli"
    except OSError:
        return False


@asynccontextmanager
async def _provider_lock(*, timeout_seconds: float, started_at: float | None = None):
    """Serialize ZCode calls with a nonblocking OS file lock.

    The wait is bounded by the call's own budget: a queued call that cannot
    get the lock in time fails with a timeout instead of waiting behind
    another turn indefinitely.
    """
    deadline = (time.monotonic() if started_at is None else started_at) + timeout_seconds
    descriptor = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise NativeCliError(
                        f"zcode CLI timed out after {timeout_seconds:.2f}s "
                        "waiting for the provider lock"
                    ) from None
                await _lock_sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _api_key() -> str | None:
    for name in ("NATIVE_CLI_ZCODE_API_KEY", "ZAI_CN_CODING_API_KEY", "ZAI_API_KEY"):
        value = approved_env_value(name)
        if value and value.strip():
            return value.strip()
    return None


def _stderr_diagnostic(stderr: str) -> str:
    """Keep actionable diagnostics while ignoring bundled source and stack."""
    selected: list[str] = []
    skip_truncated_tail_line = False
    for raw_line in (stderr or "").splitlines():
        raw = raw_line.strip()
        if skip_truncated_tail_line:
            skip_truncated_tail_line = False
            continue
        if raw == "[stderr middle omitted; tail-mid-line=1]":
            skip_truncated_tail_line = True
            continue
        if raw == "[stderr middle omitted; tail-mid-line=0]":
            skip_truncated_tail_line = False
            continue
        line = raw
        if not line:
            continue
        if (
            len(line) > 1000
            or line.startswith(("at ", "at async ", "node:internal/"))
            or re.match(r"^/.+\.(?:cjs|js|mjs):\d+(?::\d+)?$", line)
            or re.match(r"^!?(?:function\b|\(\s*function\b|var\s+[$\w]+\s*=)", line)
        ):
            continue
        selected.append(line[:500])
        if len(selected) >= 8:
            break
    return "\n".join(selected)[:3000]


@dataclass(frozen=True)
class ZCodeTurnScope:
    """The resources one ZCode turn owns, and the single budget it may spend.

    The provider lock, the transient MCP config and the turn's deadline all
    have the same lifetime, so they are owned together: the lock keeps the
    shared config private to this turn, and the deadline covers the wait for
    the lock *and* the process run. A transport opens a scope and closes it.
    Nothing else touches the lock, and no transport may hand out a second
    budget, so a queued turn can neither run with another turn's model,
    endpoint or MCP servers nor take twice the timeout it was given.
    """

    invocation: CliInvocation
    deadline: float
    timeout_seconds: float

    def remaining(self) -> float:
        """The seconds this turn may still spend, or a timeout error.

        The wait for the provider lock is part of the turn, so the budget
        that is left when the lock is taken is what the process run gets.
        """
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise NativeCliError(
                f"zcode CLI timed out after {self.timeout_seconds:.2f}s"
            )
        return left


def _personal_provider_path() -> Path:
    isolation = current_worker_isolation()
    configured = approved_env_value("NATIVE_CLI_ZCODE_PROVIDER_DIR")
    root = (
        Path(configured)
        if configured
        else private_home() / ".cache" / "conductor" / "native-cli" / "zcode"
    )
    if isolation is not None and not root.resolve().is_relative_to(
        isolation.home.resolve()
    ):
        raise NativeCliUnavailable(
            "ZCode provider config path is outside workspace home"
        )
    return root / "provider_config.json"


def write_personal_provider_config(
    *,
    api_key: str,
    base_url: str,
    model: str,
    path: Path | None = None,
) -> Path:
    """Materialize a private provider file for the ZCode CLI.

    ZCode's bundled desktop providers require a desktop account snapshot the
    standalone CLI cannot load. A personal API-key provider does materialize:
    this is the supported standalone lane from the ZCode CLI docs. Keep the
    file private because ZCode does not support ``${ENV}`` in ``apiKey``.
    """

    target = path or _personal_provider_path()
    payload = {
        "schemaVersion": 1,
        "config": {
            "providerConfigRules": {
                "providerRules": [
                    {
                        "providerId": _PROVIDER_ID,
                        "providerName": "Conductor Z.ai",
                        "config": {
                            "group": "standard-personal",
                            "access": {"type": "api-key", "apiKey": api_key},
                            "api": {
                                "type": "anthropic-messages",
                                "baseUrl": base_url,
                            },
                            "personalModelIds": [model],
                        },
                    }
                ]
            },
            "modelConfigRules": {
                "providerModelRules": [],
                "manualProviderModelRules": [],
            },
            "defaultModelSelection": {
                "providerId": _PROVIDER_ID,
                "modelId": model,
            },
        },
    }
    contents = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    if current_worker_isolation() is not None:
        secure_private_write(target, contents)
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    target.write_bytes(contents)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


class ZCodeCliAdapter:
    """Run one ZCode headless prompt and reuse its session id."""

    provider = "zcode"

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        read_only: bool = True,
        mcp_servers: list[dict[str, Any]] | None = None,
        mode: str | None = None,
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._cwd = cwd
        self._api_key = api_key
        configured_base_url = (
            base_url or os.environ.get("NATIVE_CLI_ZCODE_BASE_URL") or ""
        ).strip()
        self._base_url = configured_base_url or _DEFAULT_BASE_URL
        # Only an explicitly configured endpoint is handed to the CLI: the
        # clean-room CLI is logged in to an endpoint of its own, and an
        # unset override must not silently replace it.
        self._base_url_explicit = bool(configured_base_url)
        self._model = model or os.environ.get(
            "NATIVE_CLI_ZCODE_MODEL", _DEFAULT_MODEL
        )
        self._max_turns = _resolve_max_turns(max_turns)
        self._read_only = read_only
        self._mode = mode
        self._mcp_servers = list(mcp_servers or ())
        self._mcp_backup: list[tuple[Path, bytes | None]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    @property
    def max_turns(self) -> int:
        return self._max_turns

    @property
    def binary(self) -> str:
        explicit = self._binary or os.environ.get("NATIVE_CLI_ZCODE_BIN")
        if explicit:
            return explicit
        # The official ZCode app CLI is the default. The clean-room
        # ``zcode-cli`` speaks a different grammar and is selected only by an
        # explicit override, so it never wins this implicit lookup.
        found = shutil.which("zcode")
        if found:
            return found
        # The desktop app ships the official CLI as a Node bundle under
        # glm/. It has a node shebang, not a PATH command, so discover the
        # app resource before declaring the provider unavailable.
        bundled = Path("/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs")
        if bundled.is_file():
            return str(bundled)
        raise NativeCliUnavailable(
            "zcode CLI not found; install ZCode or set NATIVE_CLI_ZCODE_BIN"
        )

    @property
    def clean_room(self) -> bool:
        """Whether the resolved binary is the clean-room ``zcode-cli``.

        That CLI has no MCP integration and no desktop permission broker, so
        callers gate those capabilities on this. An unresolvable binary is
        not clean-room: there is nothing to gate yet.
        """
        try:
            return _is_clean_room_cli(self.binary)
        except NativeCliUnavailable:
            return False

    def _write_mcp_file(self, path: Path) -> None:
        import json

        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.read_bytes() if path.is_file() else None
        self._mcp_backup.append((path, backup))
        try:
            current = json.loads(path.read_text()) if path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        mcp = current.get("mcp") if isinstance(current.get("mcp"), dict) else {}
        servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else {}
        for server in self._mcp_servers:
            name = str(server.get("name") or "").strip()
            if not name:
                continue
            url = str(server.get("url") or "").strip()
            if url:
                entry: dict[str, Any] = {"type": "http", "url": url}
                headers = server.get("headers") or {}
                if headers:
                    entry["headers"] = {str(k): str(v) for k, v in headers.items()}
            else:
                command = str(server.get("command") or "").strip()
                if not command:
                    continue
                if not os.path.isabs(command):
                    resolved = shutil.which(command)
                    if not resolved:
                        continue
                    command = resolved
                entry = {
                    "type": "stdio",
                    "command": command,
                    "args": [str(arg) for arg in (server.get("args") or [])],
                }
                env = server.get("env") or {}
                if env:
                    entry["env"] = {str(k): str(v) for k, v in env.items()}
            servers[name] = entry
        mcp["servers"] = servers
        current["mcp"] = mcp
        path.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def _materialize_mcp(self, cwd: str | None) -> None:
        """Write transient user/workspace ZCode MCP config; restore afterwards."""
        if not cwd or not self._mcp_servers or self.clean_room:
            return
        if current_worker_isolation() is not None:
            raise NativeCliUnavailable(
                "workspace MCP config provisioning is unsupported for isolated ZCode workers"
            )
        # Headless ZCode reliably reads the user config; the workspace scope is
        # also written for clients that honor it.
        self._write_mcp_file(Path(cwd) / ".zcode" / "config.json")
        self._write_mcp_file(private_home() / ".zcode" / "cli" / "config.json")

    def _restore_mcp(self) -> None:
        backups, self._mcp_backup = self._mcp_backup, []
        for path, backup in backups:
            try:
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(backup)
            except OSError:
                pass

    def _resolve_mode(self, cwd: str | None) -> str:
        """Name the permission mode explicitly on every call.

        A review run stays read-only. A write-capable run only gets an
        unrestricted mode when the caller designated a dedicated working
        directory for it; otherwise it fails closed instead of running a
        free-for-all mode somewhere unintended.
        """
        if self._read_only:
            return "plan"
        if self._mode:
            return self._mode
        if not (cwd or self._cwd):
            raise NativeCliError(
                "zcode write mode requires an explicit dedicated working directory"
            )
        return "yolo"

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
        stream: bool = False,
    ) -> CliInvocation:
        binary = self.binary
        clean_room = _is_clean_room_cli(binary)
        argv = [
            binary,
            "--prompt",
            prompt,
            # The stream carries the terminal result object and the turn
            # resultType, so one format serves both the final answer and the
            # turn budget diagnosis.
            "--output-format",
            "stream-json",
            "--mode",
            self._resolve_mode(cwd),
        ]
        if clean_room:
            # The clean-room CLI stops after 40 turns by default and then
            # reports an empty failed result, so both the model and the turn
            # budget have to be explicit.
            argv.extend(["--model", self._model, "--max-turns", str(self._max_turns)])
        else:
            argv.extend(["--surface", "terminal"])
        argv.append("--no-color")
        if session_id:
            argv.extend(["--resume", session_id])
        # The app ships its built-in provider config beside the CLI resource,
        # while the CLI looks for it relative to a packaged layout. Point it at
        # the real file when the binary is the ZCode app's own resource.
        env: dict[str, str] = {}
        binary_path = Path(binary).resolve()
        bundled = binary_path.parent.parent / "config" / "provider" / "zcode-builtin.json"
        if bundled.is_file():
            env["ZCODE_BUILTIN_PROVIDER_CONFIG_FILE"] = str(bundled)
        if clean_room and self._base_url_explicit:
            # The clean-room CLI resolves its endpoint from the environment
            # (below its own saved config and credentials), so an override
            # that is not passed on would be silently ignored.
            env["ZCODE_BASE_URL"] = self._base_url
        api_key = (self._api_key or _api_key() or "").strip()
        if api_key:
            if clean_room:
                # The clean-room CLI reads the key from the environment; it
                # must never appear in the argument list.
                env["ZCODE_API_KEY"] = api_key
            else:
                personal = write_personal_provider_config(
                    api_key=api_key,
                    base_url=self._base_url,
                    model=self._model,
                )
                env["ZCODE_PERSONAL_PROVIDER_CONFIG_FILE"] = str(personal)
        return CliInvocation(
            argv=tuple(argv),
            cwd=cwd if cwd is not None else self._cwd,
            timeout_seconds=self._timeout_seconds,
            env=env or None,
        )

    @asynccontextmanager
    async def turn_scope(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
        stream: bool = False,
        started_at: float | None = None,
    ):
        """Own one ZCode turn's shared resources for its whole life.

        The provider config and the transient MCP config are shared files, so
        both are written, and the MCP one restored, only while holding the
        lock. Every transport (run, stream, approval) goes through here: a
        queued call can neither run with another call's model, endpoint or
        servers nor capture (and later restore) its temporary config.

        The scope is the only owner of the lock, and it carries the turn's
        single deadline. Waiting for the lock spends that budget and so does
        the process run, so a queued turn cannot exceed the timeout it was
        given. Closing the scope releases the lock and restores the shared
        config, whether the turn succeeded, failed or was abandoned.
        """
        started = time.monotonic() if started_at is None else started_at
        deadline = started + self._timeout_seconds
        async with _provider_lock(
            timeout_seconds=self._timeout_seconds, started_at=started
        ):
            try:
                self._materialize_mcp(cwd if cwd is not None else self._cwd)
                yield ZCodeTurnScope(
                    invocation=self.build_invocation(
                        prompt, session_id=session_id, cwd=cwd, stream=stream
                    ),
                    deadline=deadline,
                    timeout_seconds=self._timeout_seconds,
                )
            finally:
                self._restore_mcp()

    async def run(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> NativeCliResult:
        started = time.monotonic()
        async with self.turn_scope(
            prompt, session_id=session_id, cwd=cwd, started_at=started
        ) as scope:
            for attempt in range(_MAX_ATTEMPTS):
                remaining = scope.remaining()
                output = await run_process(
                    # The transcript carries its result at the end of the
                    # stream, so the tail is the part that must survive.
                    replace(scope.invocation, timeout_seconds=remaining),
                    check=False,
                    keep_tail=True,
                    stderr_keep_ends=True,
                )
                failure: NativeCliError | None = None
                text = ""
                new_session_id: str | None = None
                usage = NativeCliUsage()
                payload: Mapping[str, Any] = {}
                try:
                    text, new_session_id, usage, payload = parse_zcode_result(
                        output.stdout
                    )
                except NativeCliError as exc:
                    failure = exc
                if failure is None and output.returncode:
                    failure = NativeCliError(f"zcode CLI exited {output.returncode}")
                if failure is not None:
                    stderr = _stderr_diagnostic(output.stderr)
                    if (
                        classify_failure("zcode", stderr).reason_code
                        == "zcode_plan_quota_exhausted"
                    ):
                        raise NativeCliError("zcode plan quota exhausted") from None
                    if "select a model before sending a message" in stderr.lower():
                        raise NativeCliError("zcode has no selected model") from None
                    if stderr and not isinstance(failure, ZCodeMaxTurnsError):
                        # The transcript does not always explain a
                        # credential failure; short process diagnostics do.
                        # Bundled JavaScript source lines can contain words
                        # such as "unauthorized" unrelated to this turn.
                        raise_if_auth_failure("zcode", stderr)
                    status = _retryable_status(str(failure))
                    if status is None and output.returncode and not isinstance(
                        failure, ZCodeMaxTurnsError
                    ):
                        # A failed process explains itself on stderr.
                        detail = stderr or output.stdout.strip()
                        status = _retryable_status(detail)
                    if status and attempt + 1 < _MAX_ATTEMPTS:
                        delay = min(
                            _RETRY_BASE_SECONDS * (2**attempt),
                            _RETRY_MAX_SECONDS,
                        )
                        # The backoff is part of the call's budget: a
                        # retry must not push the call past the timeout
                        # the caller asked for.
                        await asyncio.sleep(min(delay, scope.remaining()))
                        continue
                    if status:
                        raise NativeCliError(
                            "zcode request remained unavailable after bounded retries"
                        ) from None
                    raise failure
                return NativeCliResult(
                    provider=self.provider,
                    text=text,
                    session_id=new_session_id or session_id,
                    usage=usage,
                    duration_seconds=time.monotonic() - started,
                    events=int(payload.get("eventCount") or 0),
                    raw=(payload,),
                )
        raise NativeCliError("zcode CLI request failed")


# ZCode's own wording, read out of the ZCode app bundle: it carries the
# provider's business codes for a spent credit, for a context that did not
# fit and for a throttled model, and those codes are what reaches a
# diagnostic.
register_failure_rule(
    "zcode",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="zcode_plan_quota_exhausted",
    patterns=("您已达到每周/每月使用上限",),
    requires=("[1310]",),
)
register_failure_rule(
    "zcode",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="zcode_plan_quota_exhausted",
    patterns=("zcode plan quota exhausted",),
)
register_failure_rule(
    "zcode",
    FailureClass.QUOTA_EXHAUSTED,
    reason_code="zcode_credit_exhausted",
    patterns=(
        "credit_balance_exhausted",
        "organization_spend_limit_exceeded",
        "project_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "exceeded_current_quota_error",
    ),
)
register_failure_rule(
    "zcode",
    FailureClass.CONTEXT_EXHAUSTED,
    reason_code="zcode_context_exceeded",
    patterns=(
        "context_exceeded",
        "model_context_exceeded",
        "prompt_too_long",
        "maximum number of tokens allowed",
        "range of input length should be",
        "total message token length",
    ),
)
register_failure_rule(
    "zcode",
    FailureClass.RATE_LIMITED,
    reason_code="zcode_rate_limit_reached",
    patterns=(
        "rate_limit_reached_error",
        "rate_limit_error",
    ),
)
register_failure_rule(
    "zcode",
    FailureClass.OVERLOADED,
    reason_code="zcode_engine_overloaded",
    patterns=(
        "engine_overloaded_error",
        "overloaded_error",
    ),
)
register_failure_rule(
    "zcode",
    FailureClass.NETWORK,
    reason_code="zcode_network_error",
    patterns=(
        "enotfound",
        "ehostunreach",
        "und_err_socket",
    ),
)
register_failure_rule(
    "zcode",
    FailureClass.UNKNOWN,
    reason_code="zcode_model_selection_missing",
    patterns=("zcode has no selected model",),
)
register_failure_rule(
    "zcode",
    FailureClass.AUTH_FAILED,
    reason_code="zcode_credentials_unavailable",
    patterns=("zcode is not logged in or has no usable credentials",),
)
# ZCode's headless permission broker (N39). With no permission client it
# denies a tool call that needs approval with "No permission client configured
# for <tool>" -- the shipped broker's own wording, quoted in
# src/server/services/native_runtime/zcode_approval.py (_DENY_CLASS). Only
# that line shape matches, for the same reason as Step's rule.
register_failure_rule(
    "zcode",
    FailureClass.CONFIRMATION_REQUIRED,
    reason_code=CONFIRMATION_REQUIRED_REASON,
    line_patterns=(r"^[ \t]*no permission client configured for [a-z0-9_.:-]+[ \t]*$",),
)
