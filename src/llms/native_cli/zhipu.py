"""One-shot adapter for Zhipu's clean-room CLI and legacy ``zcode`` fallback."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import asynccontextmanager
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
from src.llms.native_cli.failure import FailureClass, register_failure_rule
from src.llms.native_cli.process import run_process
from src.llms.native_cli.zcode_transcript import (
    ZCodeMaxTurnsError,
    exhausted_turn_budget,
    final_result,
    iter_json_objects,
    turn_failure,
)

_DEFAULT_MODEL = "GLM-5.3"
_DEFAULT_MAX_TURNS = 200
_PROVIDER_ID = "bigmodel-individual-coding-plan"
_PERMISSION_MODES = {"build", "edit", "plan", "yolo", "guarded"}
_LOCK_PATH = Path("/tmp/conductor-native-cli-zhipu.lock")
_LOCK_POLL_SECONDS = 0.05
# Captured at import time on purpose: the provider lock must always yield to
# the event loop, even when a test patches ``asyncio.sleep`` to observe the
# retry backoff. Patching it would turn the lock poll into a busy loop that
# starves the loop and hangs the whole process.
_lock_sleep = asyncio.sleep
_MAX_ATTEMPTS = 3
_RETRY_BASE_SECONDS = 1.0
_RETRY_MAX_SECONDS = 2.0
_TIMEOUT_RE = re.compile(r"native CLI timed out after \d+s\Z")
_RETRYABLE_STATUS_RE = re.compile(
    r"(?:\b(?:http|status(?:\s+code)?|error\s+code)\s*[:=]?\s*(429|502)\b"
    r"|\b(?:api\s+)?error\s*[:=]\s*(429|502)\b"
    r"|\b(429|502)\s+(?:too many requests|rate limit|bad gateway))",
    re.IGNORECASE,
)


def _error_detail(payload: Mapping[str, Any]) -> str:
    raw = payload.get("error")
    if isinstance(raw, Mapping):
        return " ".join(str(raw.get(key) or "") for key in ("code", "type", "message"))
    return str(raw or "")


def _retryable_status(detail: str) -> int | None:
    match = _RETRYABLE_STATUS_RE.search(detail)
    if not match:
        return None
    return int(next(group for group in match.groups() if group is not None))


def _has_retryable_status(payload: Mapping[str, Any]) -> int | None:
    raw_error = payload.get("error")
    candidates = [
        payload.get("status_code"),
        payload.get("statusCode"),
        payload.get("status"),
    ]
    if isinstance(raw_error, Mapping):
        candidates.extend(
            raw_error.get(key)
            for key in ("status", "status_code", "statusCode", "code")
        )
    for value in candidates:
        if value is not None and str(value).strip() in {"429", "502"}:
            return int(str(value).strip())
    return None


def _has_failed_status(payload: Mapping[str, Any]) -> bool:
    candidates = [
        payload.get("status_code"),
        payload.get("statusCode"),
        payload.get("status"),
    ]
    return any(
        str(value).strip().isdigit() and 400 <= int(str(value).strip()) <= 599
        for value in candidates
        if value is not None
    )


def _has_error_marker(payload: Mapping[str, Any]) -> bool:
    values = (payload.get("status"), payload.get("type"))
    return any(
        isinstance(value, str)
        and value.strip().lower() in {"error", "failed", "failure"}
        for value in values
    )


def _resolve_max_turns(value: int | None) -> int:
    """The turn budget, falling back to the default for unusable input."""
    raw = os.environ.get("NATIVE_CLI_ZHIPU_MAX_TURNS", "").strip()
    if value is not None:
        raw = str(value)
    if not raw:
        return _DEFAULT_MAX_TURNS
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TURNS
    return parsed if parsed > 0 else _DEFAULT_MAX_TURNS


def parse_zhipu_text(
    stdout: str, *, require_json: bool = False
) -> tuple[str, NativeCliUsage]:
    """Accept normal text and a small, known set of JSON response envelopes."""
    text = (stdout or "").strip()
    if not text:
        raise NativeCliError("zcode returned no output")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        if require_json:
            raise NativeCliError("zcode-cli returned non-JSON output") from None
        return text, NativeCliUsage()
    if not isinstance(payload, Mapping):
        if require_json:
            raise NativeCliError("zcode-cli returned an unexpected JSON result")
        return text, NativeCliUsage()

    retryable_status = _has_retryable_status(payload)
    if retryable_status:
        raise NativeCliError(f"HTTP {retryable_status}")
    if require_json and payload.get("isError") is True:
        raise NativeCliError("Zhipu CLI reported a request error")
    if payload.get("error"):
        detail = _error_detail(payload)
        raise_if_auth_failure("Zhipu", detail)
        retryable_status = _retryable_status(detail)
        if retryable_status:
            raise NativeCliError(f"HTTP {retryable_status}")
        raise NativeCliError("Zhipu reported a request error")
    if _has_failed_status(payload):
        raise NativeCliError("Zhipu reported a request error")
    if _has_error_marker(payload):
        raise NativeCliError("Zhipu reported a request error")

    keys = (
        ("response",)
        if require_json
        else (
            "response",
            "result",
            "output",
            "text",
            "content",
        )
    )
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            raw_usage = payload.get("usage")
            if raw_usage is not None and not isinstance(raw_usage, Mapping):
                raise NativeCliError("Zhipu CLI returned invalid usage data")
            try:
                usage = NativeCliUsage.from_mapping(raw_usage)
            except (TypeError, ValueError, OverflowError):
                raise NativeCliError("Zhipu CLI returned invalid usage data") from None
            return value.strip(), usage
    if require_json:
        raise NativeCliError("zcode-cli returned an unexpected JSON result")
    # The legacy CLI may return a valid JSON answer that is not an envelope.
    return text, NativeCliUsage()


def _raise_classified(detail: str) -> None:
    """Raise the auth or retryable error a diagnostic implies, else return."""
    raise_if_auth_failure("Zhipu", detail)
    status = _retryable_status(detail)
    if status:
        raise NativeCliError(f"HTTP {status}")


def parse_zhipu_transcript(stdout: str) -> tuple[str, NativeCliUsage]:
    """Read the final result out of a zcode-cli event transcript.

    The CLI stops at its turn budget and then reports an empty failed result,
    so the transcript's turn resultType decides whether the run failed or
    simply ran out of turns.
    """
    events = list(iter_json_objects(stdout or ""))
    if not events:
        raise NativeCliError("zcode-cli returned non-JSON output")
    payload = final_result(events)
    if exhausted_turn_budget(events, payload):
        raise ZCodeMaxTurnsError(
            "zcode-cli reached its turn budget (max_turns); retry with more turns"
        )
    failure = turn_failure(events)
    if failure:
        # A failed turn names its cause; classify it like any other error
        # detail so a credential failure or a retryable HTTP status survives.
        _raise_classified(failure)
    if not payload:
        raise NativeCliError("zcode-cli returned an unexpected JSON result")
    return parse_zhipu_text(json.dumps(payload), require_json=True)


def _linked_worktree(path: str | Path) -> Path:
    """Require a real linked worktree before granting a write-capable CLI mode."""
    from src.llms.native_cli.isolation import current_worker_isolation

    if current_worker_isolation() is not None:
        raise NativeCliUnavailable(
            "Zhipu Git worktree probing is unsupported in isolated workers"
        )
    try:
        candidate = Path(path).expanduser().resolve(strict=True)
    except OSError:
        raise NativeCliError(
            "Zhipu write mode requires an existing linked Git worktree"
        ) from None
    if not candidate.is_dir():
        raise NativeCliError(
            "Zhipu write mode requires an existing linked Git worktree"
        )
    try:
        git_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GIT_")
        }
        git_env["GIT_OPTIONAL_LOCKS"] = "0"
        result = subprocess.run(
            [
                "git",
                "-C",
                str(candidate),
                "rev-parse",
                "--show-toplevel",
                "--git-dir",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
            env=git_env,
        )
        top, git_dir, common_dir = result.stdout.splitlines()
    except (OSError, subprocess.SubprocessError, ValueError):
        raise NativeCliError(
            "Zhipu write mode requires an existing linked Git worktree"
        ) from None
    top_path = Path(top).resolve()
    git_path = (
        (candidate / git_dir).resolve()
        if not Path(git_dir).is_absolute()
        else Path(git_dir).resolve()
    )
    common_path = (
        (candidate / common_dir).resolve()
        if not Path(common_dir).is_absolute()
        else Path(common_dir).resolve()
    )
    marker = top_path / ".git"
    try:
        registered_marker = Path((git_path / "gitdir").read_text().strip()).resolve()
    except OSError:
        raise NativeCliError(
            "Zhipu write mode requires an existing linked Git worktree"
        ) from None
    if (
        top_path != candidate
        or not marker.is_file()
        or registered_marker != marker.resolve()
        or git_path == common_path
        or not git_path.is_relative_to(common_path / "worktrees")
        or not git_path.exists()
        or not common_path.exists()
    ):
        raise NativeCliError(
            "Zhipu write mode requires an existing linked Git worktree"
        )
    return candidate


def _is_clean_room_cli(binary: str) -> bool:
    path = Path(binary)
    if path.name == "zcode-cli":
        return True
    try:
        return path.resolve().name == "zcode-cli"
    except OSError:
        return False


@asynccontextmanager
async def _provider_lock():
    """Serialize aliases and workers with a nonblocking OS file lock."""
    descriptor = os.open(_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await _lock_sleep(_LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


class ZhipuCliAdapter:
    """Run one Zhipu turn with serialized calls and an explicit permission mode."""

    provider = "zhipu"
    model = _DEFAULT_MODEL

    def __init__(
        self,
        *,
        binary: str | None = None,
        timeout_seconds: float = 300.0,
        cwd: str | None = None,
        model: str | None = None,
        max_turns: int | None = None,
        permission_mode: str = "plan",
    ) -> None:
        self._binary = binary
        self._timeout_seconds = timeout_seconds
        self._model = model or os.environ.get("NATIVE_CLI_ZHIPU_MODEL", _DEFAULT_MODEL)
        self._max_turns = _resolve_max_turns(max_turns)
        if permission_mode not in _PERMISSION_MODES:
            raise ValueError("unsupported Zhipu permission mode")
        self.permission_mode = permission_mode
        self.model = self._model
        self.max_turns = self._max_turns
        self._cwd = cwd

    @property
    def binary(self) -> str:
        configured = self._binary or os.environ.get("NATIVE_CLI_ZHIPU_BIN")
        if configured:
            return configured
        default = Path.home() / ".local" / "bin" / "zcode-cli"
        if default.is_file():
            return str(default)
        found = shutil.which("zcode-cli")
        if found:
            return found
        # Keep the prior adapter usable on machines that have not installed the
        # clean-room CLI yet.
        legacy_default = Path.home() / ".local" / "bin" / "zcode"
        if legacy_default.is_file():
            return str(legacy_default)
        found = shutil.which("zcode")
        if found:
            return found
        raise NativeCliUnavailable(
            "zcode CLI not found; install zcode-cli or keep ~/.local/bin/zcode available"
        )

    def build_invocation(
        self,
        prompt: str,
        *,
        session_id: str | None = None,
        cwd: str | None = None,
    ) -> CliInvocation:
        if session_id:
            raise NativeCliError("Zhipu CLI does not support session resume")
        binary = self.binary
        if _is_clean_room_cli(binary):
            write_capable = self.permission_mode != "plan"
            invocation_cwd = "/tmp"
            argv = [
                binary,
                "-p",
                prompt,
                "--output-format",
                "stream-json",
                "--model",
                self._model,
                # The CLI stops after 40 turns by default and then reports an
                # empty failed result, so the budget has to be explicit.
                "--max-turns",
                str(self._max_turns),
                "--provider",
                _PROVIDER_ID,
                "--permission-mode",
                self.permission_mode,
            ]
            if write_capable:
                requested_cwd = cwd or self._cwd
                if not requested_cwd:
                    raise NativeCliError(
                        "Zhipu write mode requires an explicit linked Git worktree cwd"
                    )
                invocation_cwd = str(_linked_worktree(requested_cwd))
                argv.extend(("--cwd", invocation_cwd))
            return CliInvocation(
                argv=tuple(argv),
                cwd=invocation_cwd,
                timeout_seconds=self._timeout_seconds,
            )
        if self.permission_mode != "plan":
            raise NativeCliError(
                "write-capable permission modes require the zcode-cli executable"
            )
        # The older ZCode CLI has a separate command grammar. Keep its
        # existing plan-mode call as a compatibility fallback.
        argv = (binary, "-p", prompt, "--mode", "plan")
        return CliInvocation(
            argv=argv,
            cwd="/tmp",
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
            async with asyncio.timeout(self._timeout_seconds):
                async with _provider_lock():
                    for attempt in range(_MAX_ATTEMPTS):
                        try:
                            if self.permission_mode != "plan":
                                invocation = self.build_invocation(
                                    prompt, session_id=session_id, cwd=cwd
                                )
                            output = await run_process(
                                invocation, check=False, keep_tail=True
                            )
                            clean_room = _is_clean_room_cli(invocation.argv[0])
                            try:
                                if clean_room:
                                    answer, usage = parse_zhipu_transcript(
                                        output.stdout
                                    )
                                else:
                                    answer, usage = parse_zhipu_text(output.stdout)
                            except ZCodeMaxTurnsError:
                                # The CLI exits non-zero when it runs out of
                                # turns, so the transcript has to be read
                                # first: a spent budget stays a spent budget.
                                raise
                            except NativeCliError:
                                # The transcript did not explain the failure.
                                # The process diagnostic may still classify
                                # it: a credential failure explains itself
                                # there, and it is never a spent turn budget.
                                _raise_classified(output.stderr.strip())
                                raise
                            if output.returncode:
                                # The transcript decided the outcome above, so
                                # a non-zero exit is still a plain failure.
                                detail = (
                                    output.stderr.strip()
                                    or output.stdout.strip()
                                    or "no output"
                                )
                                raise_if_auth_failure("Zhipu", detail)
                                raise NativeCliError(
                                    f"Zhipu CLI exited {output.returncode}: {detail[:2000]}"
                                )
                            return NativeCliResult(
                                provider=self.provider,
                                text=answer,
                                usage=usage,
                                duration_seconds=time.monotonic() - started,
                            )
                        except NativeCliUnavailable as exc:
                            if (
                                "not logged in" in str(exc).lower()
                                or "credentials" in str(exc).lower()
                            ):
                                raise NativeCliUnavailable(
                                    "Zhipu CLI is not logged in or has no usable credentials"
                                ) from None
                            raise NativeCliUnavailable("Zhipu CLI is unavailable") from None
                        except ZCodeMaxTurnsError:
                            # A spent turn budget is not a provider failure and
                            # must stay distinguishable from one.
                            raise
                        except NativeCliError as exc:
                            message = str(exc)
                            if _TIMEOUT_RE.fullmatch(message):
                                raise NativeCliError(
                                    f"Zhipu CLI timed out after {self._timeout_seconds:.2f}s"
                                ) from None
                            retryable = _retryable_status(message)
                            if retryable and attempt + 1 < _MAX_ATTEMPTS:
                                delay = min(
                                    _RETRY_BASE_SECONDS * (2**attempt),
                                    _RETRY_MAX_SECONDS,
                                )
                                await asyncio.sleep(delay)
                                continue
                            if retryable:
                                raise NativeCliError(
                                    "Zhipu request remained unavailable after bounded retries"
                                ) from None
                            raise NativeCliError(
                                "Zhipu CLI request failed; details omitted"
                            ) from None
        except TimeoutError:
            raise NativeCliError(
                f"Zhipu CLI timed out after {self._timeout_seconds:.2f}s"
            ) from None
        raise NativeCliError("Zhipu CLI request failed")


# Zhipu's own wording: the Z.ai / BigModel endpoint answers a throttled
# account with its own business code and its own sentence, and the CLI that
# speaks to it says so when no API key is configured. The 502 shape is the
# one this adapter already treats as retryable.
register_failure_rule(
    "zhipu",
    FailureClass.RATE_LIMITED,
    reason_code="zhipu_account_rate_limited",
    patterns=(
        "您的账户已达到速率限制",
        "[1302]",
    ),
)
register_failure_rule(
    "zhipu",
    FailureClass.AUTH_FAILED,
    reason_code="zhipu_login_required",
    patterns=("未登录：缺少 API Key",),
)
register_failure_rule(
    "zhipu",
    FailureClass.OVERLOADED,
    reason_code="zhipu_bad_gateway",
    status_codes=(502,),
)
