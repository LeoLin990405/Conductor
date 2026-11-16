"""Running the lines: launch, watch, cross-review, merge.

Every external effect (a subprocess, a git command) goes through an injected
runner so the state machine can be driven by fakes in tests, and so no test
reaches a real CLI, a real repository or the network.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import tempfile
import time
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Mapping, Protocol

from src.llms.native_cli.base import (
    NativeCliUnavailable,
    NativeCliUsage,
    resumable_session_id,
)
from src.llms.native_cli.failure import FailureClass, classify_failure, redact_credentials
from src.llms.native_cli.grok import GrokCliAdapter
from src.llms.native_cli.kimi import KimiCliAdapter
from src.llms.native_cli.process import ProcessTracker, kill_child_group, spawn_group
from src.llms.native_cli.session_manager import WorkerSessionManager
from src.llms.native_cli.sessions import NativeCliSessionStore
from src.llms.native_cli.worker_report import (
    WORKER_REPORT_PROMPT,
    WORKER_REPORT_TIMEOUT_SECONDS,
    WorkerReport,
)
from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    run_isolated_git_mutation,
    run_isolated_git_readonly,
)

from . import context_budget as _budget
from . import env_preflight as _env_preflight
from .env_noise import filter_porcelain
from .context_budget import ContextEstimate
from .protocols import (
    ApprovalDecision,
    ApprovalGate,
    DispatchError,
    GateNotApprovedError,
    LaunchError,
    MergeConflictError,
    NoNewCommitError,
    UncommittedChangesError,
)
from . import p0_hooks, review_isolation, review_strict
from .review_admission import ReviewAdmission, decide_review_admission
from . import launch_registry
from .review_schema import validate_verdict
from .review_triage import REVIEW_RUBRIC, with_status
from .gates_schema import is_gates_snapshot
from .delivery import build_line_delivery_task
from .self_report import SelfReportReply
from .types import Dispatch, Line, LineStatus, RunStatus, Verdict

logger = logging.getLogger(__name__)


SessionMode = Literal["fresh", "resume"]


@dataclass
class WorkerRunStatus(RunStatus):
    """RunStatus plus recovery metadata for the public Worker poll seam.

    The base type stays unchanged for every existing monitor and caller.
    """

    exit_code: int | None = None
    failure_class: str | None = None
    reason_code: str | None = None
    retry_after_seconds: float | None = None
    #: A finished run's longer log tail: the final report a fix round's ruling
    #: is checked against. ``output`` stays the short tail every caller shows.
    report: str = field(default="", repr=False)
    #: P0: a ``silent_worker.SilentFinding`` when the run produced no model
    #: output (detection only; the machine decides what it costs).
    silent: Any = None


@dataclass(frozen=True)
class LaunchResult:
    """A completed Worker launch, with transient output and parsed usage."""

    run_id: str
    session_id: str | None
    output: str = field(repr=False)
    tokens: int = 0
    usage: NativeCliUsage = field(default_factory=NativeCliUsage, repr=False)
    # Line branch HEAD before and after the run. The machine keeps them per
    # author, so an empty retry can tell whose commits the branch carries.
    start_head: str | None = None
    head: str | None = None
    # N29: the Worker's own handoff request (``context_budget.HANDOFF_*``),
    # its validated report, and the post-run context estimate. Defaulted, so
    # positional constructors keep working.
    handoff_request: str = _budget.HANDOFF_NONE
    worker_report: WorkerReport | None = field(default=None, repr=False)
    self_report_bytes: int = 0
    estimate: ContextEstimate | None = None
    # N29: the ``ok`` request file, left in place until the machine has saved
    # the handoff decision (a crash before that save still finds it).
    handoff_request_file: Path | None = None


@dataclass(frozen=True)
class _BudgetRun:
    """One context-budgeted run's paths and pre-launch state (N29)."""

    paths: _budget.RunPaths
    lineage: _budget.LineageLike
    turns: int
    reported_tokens: int
    started_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerLaunchFailure(LaunchError):
    """A Worker exited with a classified failure; raw output is transient only."""

    def __init__(
        self,
        *,
        run_id: str,
        exit_code: int,
        output: str,
        session_id: str | None,
        retry_after_seconds: int | None,
        failure_class: FailureClass,
        reason_code: str,
        usage: NativeCliUsage | None = None,
    ) -> None:
        super().__init__(f"worker exited with classified failure ({exit_code})")
        self.run_id = run_id
        self.exit_code = exit_code
        self.output = output
        self.session_id = session_id
        self.retry_after_seconds = retry_after_seconds
        self.failure_class = failure_class
        self.reason_code = reason_code
        # The CLI's own token counts for the failed run (N30 cost ledger).
        self.usage = usage

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(run_id={self.run_id!r}, "
            f"exit_code={self.exit_code!r}, failure_class={self.failure_class.value!r})"
        )


_SESSION_CAPABILITIES: dict[str, frozenset[SessionMode]] = {
    # Kimi 2.5 and Grok resume only a session they created themselves: a
    # caller-owned id on a fresh run is answered with "session not found"
    # (N22). Their adapters say so; a fresh run takes no id, and the id the
    # CLI reports in its stream is what a fix round resumes.
    "kimi": KimiCliAdapter.session_modes,  # type: ignore[dict-item]
    "grok": GrokCliAdapter.session_modes,  # type: ignore[dict-item]
    # These CLIs expose resume of an existing id, but no verified explicit
    # id for starting a new session.
    "cursor": frozenset({"resume"}),
    "glm": frozenset({"resume"}),
    "minimax": frozenset({"resume"}),
}


def supports_explicit_session_id(provider: str) -> bool:
    """Whether the dispatch CLI accepts an id when creating a fresh session."""
    return "fresh" in _SESSION_CAPABILITIES.get(provider.lower(), frozenset())


def supports_session_resume(provider: str) -> bool:
    """Whether the dispatch CLI can resume a known session id."""
    return "resume" in _SESSION_CAPABILITIES.get(provider.lower(), frozenset())


# Providers whose CLI resumes only an id it created and reported itself. A
# persisted id for one of them is resumed only when dispatch captured it from
# that CLI's output (``session_origin == "cli"`` on the line).
CLI_OWNED_SESSION_PROVIDERS = frozenset(
    provider
    for provider, adapter in (("kimi", KimiCliAdapter), ("grok", GrokCliAdapter))
    if "fresh" not in adapter.session_modes
)


# The reason codes a CLI gives when the id it was asked to resume is gone.
# The stored id is dropped and the run goes fresh once, on its own budget.
SESSION_LOST_REASON_CODES = frozenset({"kimi_session_not_found", "grok_session_not_saved"})


def requires_cli_owned_session(provider: str) -> bool:
    """Whether only an id this provider's CLI reported may be resumed."""
    return provider.lower() in CLI_OWNED_SESSION_PROVIDERS


def _parse_dispatch(
    provider: str, output: str
) -> tuple[str, str | None, NativeCliUsage] | None:
    """Run one provider parser and return its text, session id, and usage.

    ``None`` means this provider has no parser. Parser failures propagate.
    The text is whatever the parser returns; this does not search the raw
    log for a JSON object.
    """
    if provider == "kimi":
        from src.llms.native_cli.kimi import kimi_session_id, parse_kimi_stream

        text, events = parse_kimi_stream(output)
        # The id Kimi created and named in its stream, never the caller's.
        return text, kimi_session_id(events), NativeCliUsage()
    if provider == "cursor":
        from src.llms.native_cli.cursor import parse_cursor_stream

        text, session_id, usage, _events = parse_cursor_stream(output)
        return text, session_id, usage
    if provider == "glm":
        from src.llms.native_cli.zcode import parse_zcode_result

        text, session_id, usage, _payload = parse_zcode_result(output)
        return text, session_id, usage
    if provider == "grok":
        from src.llms.native_cli.grok import parse_grok_result

        text, session_id, usage, _payload = parse_grok_result(output)
        return text, session_id, usage
    if provider == "minimax":
        from src.llms.native_cli.minimax import parse_minimax_result

        text, session_id, usage, _payload = parse_minimax_result(output)
        return text, session_id, usage
    return None


def _final_text(provider: str, output: str) -> str:
    """The provider parser's text.

    An unknown provider or a parser exception raises so the caller can treat
    the turn as invalid.
    """
    parsed = _parse_dispatch(provider, output)
    if parsed is None:
        raise ValueError(f"no dispatch parser for {provider}")
    text, _session_id, _usage = parsed
    return text


def _session_metadata(
    provider: str,
    output: str,
    requested_id: str | None,
) -> tuple[str | None, NativeCliUsage]:
    """Use the same structured parsers as native adapters when output exposes ids."""
    try:
        parsed = _parse_dispatch(provider, output)
    except Exception:
        # Output may include wrapper progress around the adapter's JSON stream.
        # Parsing metadata must never turn a completed Worker into a failed one.
        return _session_id_from_error_stream(provider, output) or requested_id, NativeCliUsage()
    if parsed is None:
        return requested_id, NativeCliUsage()
    _text, session_id, usage = parsed
    return session_id or requested_id, usage


_EXIT_CLASS = {
    75: FailureClass.RATE_LIMITED,
    76: FailureClass.QUOTA_EXHAUSTED,
    77: FailureClass.AUTH_FAILED,
    78: FailureClass.CONTEXT_EXHAUSTED,
    79: FailureClass.CRASH,
}

# dispatch_launch.sh prints this marker on its own stderr line (captured with
# stdout in the run log) and exits 79 when it could not move the Worker's git
# objects out of the per-launch quarantine. It is a local launcher failure,
# never a provider one: UNKNOWN takes the one fresh retry of the same Worker
# in the same worktree, which retries the migration, and cannot trip the
# rate-limit/quota provider skip whatever the transcript says.
OBJECTS_NOT_MIGRATED_REASON = "objects_not_migrated"
# The launcher did not even try the migration: the Worker's process group
# still had live members (``sweep=failed`` on the marker). Its own reason, so
# the retry and the operator can tell it from a migration that failed.
OBJECTS_NOT_MIGRATED_SWEEP_FAILED_REASON = "objects_not_migrated_sweep_failed"
_OBJECTS_NOT_MIGRATED_MARKER = "dispatch_launch: objects_not_migrated"


# The classes a Worker's own failure keeps behind the marker: they steer the
# provider skip and the backoff, so one unmigrated launch must not hide them.
_KEPT_BEHIND_OBJECTS_NOT_MIGRATED = frozenset(
    {
        FailureClass.QUOTA_EXHAUSTED,
        FailureClass.RATE_LIMITED,
        FailureClass.AUTH_FAILED,
        FailureClass.TIMEOUT,
    }
)


def _objects_not_migrated(output: str) -> tuple[bool, int | None, bool]:
    """Whether the launcher printed the marker, the Worker's own status, and
    whether the launcher's process-group sweep failed.

    The marker reads ``dispatch_launch: objects_not_migrated worker_status=<n>
    [sweep=failed] quarantine=<path>``; an older launcher printed no status.
    The path is last and may hold spaces, so parsing stops there.
    """
    for raw in (output or "").splitlines():
        line = raw.strip()
        if not line.startswith(_OBJECTS_NOT_MIGRATED_MARKER):
            continue
        status: int | None = None
        sweep_failed = False
        for token in line[len(_OBJECTS_NOT_MIGRATED_MARKER) :].split():
            name, _, value = token.partition("=")
            if name == "quarantine":
                break
            if name == "worker_status":
                status = int(value) if value.isdigit() else None
            elif name == "sweep" and value == "failed":
                sweep_failed = True
        return True, status, sweep_failed
    return False, None, False


def _objects_not_migrated_class(
    model: str, output: str, worker_status: int | None
) -> tuple[FailureClass, int | None]:
    """The class for an unmigrated launch: the Worker's own quota, rate
    limit, auth or timeout failure when its status says it failed, unknown
    otherwise. A Worker that exited 0 is never read from its transcript."""
    if not worker_status:
        return FailureClass.UNKNOWN, None
    if worker_status == 124:
        return FailureClass.TIMEOUT, None
    by_status = _EXIT_CLASS.get(worker_status)
    if by_status in _KEPT_BEHIND_OBJECTS_NOT_MIGRATED:
        return by_status, None
    classification = classify_failure(model, output, exit_code=worker_status)
    if classification.failure_class in _KEPT_BEHIND_OBJECTS_NOT_MIGRATED:
        return classification.failure_class, classification.retry_after_seconds
    return FailureClass.UNKNOWN, None


def _session_id_from_error_stream(provider: str, output: str) -> str | None:
    """Recover session ids from adapter event shapes even when its parser raises."""
    import json

    for raw in (output or "").splitlines():
        try:
            event = json.loads(raw.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, Mapping):
            continue
        if provider == "cursor":
            if (event.get("type") == "system" and event.get("subtype") == "init") or event.get(
                "type"
            ) == "result":
                value = event.get("session_id")
                if value:
                    return str(value)
        elif provider in {"glm", "minimax"}:
            value = event.get("sessionId")
            if value:
                return str(value)
    if provider == "grok":
        from src.llms.native_cli.grok import grok_stream_session_id

        # streaming-json is NDJSON; the end event names the session. The
        # last id reported wins.
        return grok_stream_session_id(output)
    if provider == "kimi":
        from src.llms.native_cli.kimi import kimi_session_id

        events: list[Mapping[str, object]] = []
        for raw in (output or "").splitlines():
            try:
                event = json.loads(raw.strip())
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(event, Mapping):
                events.append(event)
        return kimi_session_id(events)
    return None

# argv, cwd, log path -> exit code. The default shells out; tests inject a fake.
RunCommand = Callable[[list[str], Path, Path], Awaitable[int]]
# argv, cwd -> (exit code, stdout, stderr).
GitCommand = Callable[..., Awaitable[tuple[int, str, str]]]

DEFAULT_LAUNCH_SCRIPT = Path.home() / "conductor-wt" / "launch.sh"

# A self-report log is read from the start. A larger file is refused whole.
_SELF_REPORT_MAX_BYTES = 1024 * 1024
# Only these providers are asked. Any other provider returns None. A provider
# whose adapter says it cannot self-report (Kimi, Grok: the report turn would
# run outside the line worktree its session belongs to) is not in the set.
_SELF_REPORT_CAPABLE: dict[str, bool] = {
    "kimi": bool(KimiCliAdapter.supports_self_report),
    "grok": bool(GrokCliAdapter.supports_self_report),
    "minimax": True,
}
SELF_REPORT_PROVIDERS = frozenset(
    provider for provider, capable in _SELF_REPORT_CAPABLE.items() if capable
)


def supports_self_report(provider: str) -> bool:
    """Whether dispatch may start a self-report turn for this provider."""
    return provider.strip().lower() in SELF_REPORT_PROVIDERS

TAIL_CHARS = 500
#: How much of a finished run's log the fix-round ruling check reads. A ruling
#: lists up to 20 must and must-not items, each confirmed on its own line, so
#: the 500-character tail cannot hold the report.
REPORT_TAIL_CHARS = 64_000
# The nudge a worker whose run ended with changes still in its tree gets, once
# (N49). It asks for one commit of what is already changed and nothing else, so
# a second turn cannot start new work the line never asked for.
COMMIT_PROMPT = (
    "Your run ended with uncommitted changes in this worktree. Commit all of "
    "them on the current branch now, with a message that describes the work, "
    "and then stop. Change no file that is not already changed and add nothing "
    "new."
)
# The commit prompt is one bounded turn (N49). Its bound is the worker run
# timeout the dispatch settings already read, so an operator who lengthens a
# run lengthens the nudge with it; a value that is not a positive number keeps
# the default.
COMMIT_PROMPT_TIMEOUT_ENV = "CONDUCTOR_DISPATCH_TIMEOUT_SECONDS"
DEFAULT_COMMIT_PROMPT_TIMEOUT_SECONDS = 600.0
#: The workspace's sandbox policy reaches the Worker CLI as an environment
#: variable. It is an inheritance setting, not an argv switch: each adapter's
#: CLI has its own flag spelling, and the launch script already owns argv.
SANDBOX_POLICY_ENV = "CONDUCTOR_DISPATCH_SANDBOX_POLICY"


def commit_prompt_timeout_from_env(env: Mapping[str, str] | None = None) -> float:
    """The commit prompt's bound in seconds, from ``CONDUCTOR_DISPATCH_TIMEOUT_SECONDS``."""
    source = os.environ if env is None else env
    raw = (source.get(COMMIT_PROMPT_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_COMMIT_PROMPT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not value > 0 or value != value or value == float("inf"):
        logger.warning("ignoring invalid %s value %r", COMMIT_PROMPT_TIMEOUT_ENV, raw)
        return DEFAULT_COMMIT_PROMPT_TIMEOUT_SECONDS
    return value


def _add_usage(first: NativeCliUsage, second: NativeCliUsage) -> NativeCliUsage:
    """Two turns' provider accounting as one run's."""
    return NativeCliUsage(
        input_tokens=first.input_tokens + second.input_tokens,
        output_tokens=first.output_tokens + second.output_tokens,
        cache_read_tokens=first.cache_read_tokens + second.cache_read_tokens,
        cache_write_tokens=first.cache_write_tokens + second.cache_write_tokens,
        total_tokens=first.total_tokens + second.total_tokens,
    )


@dataclass
class _CommitTurn:
    """What the N49 commit prompt left behind, for the run it belongs to.

    The prompt is a second turn of the same worker run: the tokens it spent
    are the run's tokens, and the session it resumed (or the id its CLI
    reported) is the session the line resumes next.
    """

    session_id: str | None = None
    usage: NativeCliUsage = field(default_factory=NativeCliUsage)
# The whole prompt a reviewer is started with, in UTF-8 bytes (not characters: one is up to
# four). StepFun, Claude and Doubao take it as ONE argv string, and Linux refuses to start a
# process with a string of 128 KiB (131,072 bytes) or more, so a prompt over that ends the
# dispatch in ERROR instead of reviewing it. The call also carries the CLI's own flags and
# the environment, and the kernel bounds argv and environment together as well (2 MiB at the
# usual 8 MiB stack limit, but only about 128 KiB under a small one). 96 KiB leaves 32 KiB
# for those. Codex reads the prompt from stdin and is held to the same number.
REVIEW_PROMPT_BUDGET_BYTES = 96 * 1024
# Room kept for the header ``CrossReviewer`` puts in front of the evidence (a sentence, the
# line id and the model's name). ``CrossReviewer`` refuses a prompt that still goes over.
_FRAME_RESERVE_BYTES = 512
# The parts of the evidence that are not the diff, each held to a size. They are what the
# planner or a caller supplies, so without a bound they could leave the diff no room. The
# API takes a goal of at most 8,000 UTF-8 bytes: a goal it accepts is never cut here.
_GOAL_BYTES = 8_000
_TASK_BYTES = 8_000
_NAME_BYTES = 256
_BRANCH_BYTES = 512
_OUTPUT_BYTES = TAIL_CHARS * 4
# What the output slot holds when no worker output was supplied, which is always so live:
# ``Line.log_tail`` is raw and unredacted, so ``DispatchService`` does not pass it. A bare
# "Output tail:" heading there read as "the worker printed nothing" or "nothing failed", and
# neither is known. One line, shorter than ``_OUTPUT_BYTES``, so the slot with output (its
# heading and ``_OUTPUT_BYTES``) stays the largest the evidence can be.
_NO_OUTPUT_NOTICE = (
    "Worker output and test results are NOT part of this evidence; their absence here is "
    "not success and not silence. Do not infer that the worker printed nothing or that "
    "nothing failed."
)
# A diff given less room than this is not a diff the reviewer can judge the branch by.
_MIN_DIFF_BYTES = 1_024


# How long a cancelled launch waits after SIGTERM before SIGKILL. Descendants
# that ignore TERM, including ones that called setsid, still die on KILL.
_LAUNCH_STOP_GRACE_SECONDS = 0.5


def _term_group(pid: int) -> None:
    """SIGTERM one child group, never this process's own."""
    if pid <= 1 or pid == os.getpgrp():
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _kill_pid(pid: int) -> None:
    if pid <= 1:
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


async def _asleep(fut: asyncio.Future[object], timeout: float) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(fut), timeout)
    except (TimeoutError, asyncio.CancelledError):
        pass


async def _wait_out(fut: asyncio.Future[object], timeout: float) -> None:
    """Wait up to ``timeout`` even when this task is already cancelled.

    A cancelled task raises ``CancelledError`` at the next await until its
    cancel count is cleared. The deadline runs on a separate task so the
    grace period is real, then this task is cancelled again by the caller.
    """
    current = asyncio.current_task()
    holder = asyncio.ensure_future(_asleep(fut, timeout))
    while not holder.done():
        try:
            await asyncio.wait({holder})
        except asyncio.CancelledError:
            if current is not None:
                while current.cancelling():
                    current.uncancel()
    if holder.done() and not holder.cancelled():
        holder.exception()


async def _stop_launched(process: asyncio.subprocess.Process) -> None:
    """TERM the launch group, then KILL it and any descendant that left it.

    The ``ps`` snapshot is taken while the leader is alive. A child that
    called ``setsid`` is not in the leader's group, so the group kill misses
    it; its own group is killed from the snapshot.
    """
    tracker = ProcessTracker()
    captured: list[tuple[int, int | None]] = []
    leader = process.pid or 0
    if leader:
        tracker.spawned(leader)
        captured = tracker.capture_descendants(leader)
        _term_group(leader)
    waiter = asyncio.ensure_future(process.wait())
    await _wait_out(waiter, _LAUNCH_STOP_GRACE_SECONDS)
    # KILL even when TERM already reaped the leader. Other members of the
    # group, and the recorded calls a test watches, still need that signal.
    # ``kill_child_group`` ignores a group that is already gone.
    if leader:
        kill_child_group(process)
    for pid, pgid in captured:
        if pgid is not None and pgid != leader and pgid > 1 and pgid != os.getpgrp():
            try:
                os.killpg(pgid, signal.SIGKILL)
            except OSError:
                _kill_pid(pid)
        else:
            _kill_pid(pid)
    if not waiter.done():
        await _wait_out(waiter, _LAUNCH_STOP_GRACE_SECONDS)
        if not waiter.done():
            waiter.cancel()


async def _run_command(
    argv: list[str],
    cwd: Path,
    log_path: Path,
    *,
    registry: dict[str, asyncio.subprocess.Process] | None = None,
    run_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Run a headless CLI line, streaming its output to the run's log file.

    The line is an agent that runs tools for minutes, so it is owned like one:
    in its own session. Cancellation snapshots every descendant, signals TERM
    to the group, waits a bounded interval, then KILL to that group and to any
    descendant that left it. A cancelled launch that left a process running
    would leave a second agent in the same worktree once the line is reassigned.

    When ``registry`` and ``run_id`` are set, the process is recorded before
    the wait so ``ScriptLauncher.cancel`` can stop that same group.

    ``env`` is merged into the child environment. Host ``GIT_*`` variables stay
    unset. A caller-supplied ``CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR`` is kept.
    ``PWD`` remains the worktree.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as stream:
        worker_env = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        if env is not None:
            # Caller overrides win, including CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR.
            worker_env.update(
                {key: value for key, value in env.items() if not key.startswith("GIT_")}
            )
        worker_env["PWD"] = str(Path(cwd).resolve())
        process = await spawn_group(
            *argv,
            cwd=str(cwd),
            env=worker_env,
            stdout=stream,
            stderr=asyncio.subprocess.STDOUT,
        )
        if registry is not None and run_id is not None:
            registry[run_id] = process
        if run_id is not None:
            _record_launch(log_path.parent, run_id, process.pid, cwd)
        try:
            try:
                code = await process.wait()
            except BaseException:
                await _stop_launched(process)
                raise
        finally:
            if registry is not None and run_id is not None:
                registry.pop(run_id, None)
    log_path.with_suffix(".exit").write_text(str(code), encoding="utf-8")
    if run_id is not None:
        _record_launch_exit(log_path.parent, run_id, code)
    return code


def _record_launch(runs_dir: Path, run_id: str, pid: int, cwd: Path) -> None:
    """N35: note the launch's process group so a resumed drive can find it."""
    if launch_registry.adopt_mode() == "off":
        return
    try:
        launch_registry.record_spawn(runs_dir, run_id, pid=pid, cwd=cwd)
    except Exception:  # bookkeeping only; never fails a launch
        logger.debug("launch record for %s not written", run_id, exc_info=True)


def _record_launch_exit(runs_dir: Path, run_id: str, code: int) -> None:
    if launch_registry.adopt_mode() == "off":
        return
    try:
        launch_registry.record_exit(runs_dir, run_id, exit_code=code)
    except Exception:  # bookkeeping only
        logger.debug("launch exit for %s not written", run_id, exc_info=True)


async def _git_command(
    argv: list[str], cwd: Path, *, env: Mapping[str, str] | None = None
) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "git",
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(env) if env is not None else None,
    )
    out, err = await process.communicate()
    return (
        process.returncode or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


async def _isolated_git_readonly_command(
    argv: list[str], cwd: Path
) -> tuple[int, str, str]:
    """Run one public readonly Git form without a host-process fallback."""
    result = await asyncio.to_thread(
        run_isolated_git_readonly, str(cwd), argv, timeout=5
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )


async def _isolated_git_mutation_command(
    repo: Path, cwd: Path, worktrees_root: Path, argv: list[str]
) -> tuple[int, str, str]:
    """Run one allowlisted local Git mutation in its writable OS boundary."""
    result = await asyncio.to_thread(
        run_isolated_git_mutation,
        str(cwd),
        argv,
        repository_root=str(repo),
        worktrees_root=str(worktrees_root),
        timeout=5,
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )


def _is_object_id(value: str) -> bool:
    """A full SHA-1 or SHA-256 object id, lowercase hex."""
    return len(value) in (40, 64) and all(ch in "0123456789abcdef" for ch in value)


def _is_object_id_range(value: str) -> bool:
    """Exactly ``<object-id>..<object-id>``, nothing a caller could steer."""
    parts = value.split("..")
    return len(parts) == 2 and all(_is_object_id(part) for part in parts)


def _task6_readonly_git_argv(argv: list[str]) -> bool:
    """Classify Task 6 reads; the public helper remains the final allowlist."""
    command = tuple(argv)
    exact = {
        ("rev-parse", "HEAD"),
        ("branch", "--show-current"),
        ("worktree", "list", "--porcelain"),
        ("status", "--porcelain", "--untracked-files=all"),
        (
            "status", "--porcelain", "--untracked-files=all",
            "--ignore-submodules=all",
        ),
        ("ls-files", "--others", "--exclude-standard", "-z"),
        ("ls-files", "--stage", "-z"),
        ("ls-tree", "-r", "-l", "-z", "HEAD"),
        ("diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"),
        (
            "diff", "--cached", "--binary", "--no-ext-diff",
            "--no-textconv", "HEAD",
        ),
        ("diff", "--name-only", "--diff-filter=U"),
    }
    if command in exact:
        return True
    if len(command) == 3 and command[:2] == ("rev-list", "--count"):
        return _is_object_id_range(command[2])
    if (
        len(command) == 3
        and command[:2] == ("rev-parse", "--verify")
        and command[2]
        and not command[2].startswith("-")
        and "\x00" not in command[2]
    ):
        return True
    if len(command) == 7 and command[:5] == (
        "diff", "--patch-with-stat", "--no-color", "--no-ext-diff",
        "--no-textconv",
    ) and command[6] == "--":
        return True
    return len(command) == 6 and command[:4] == (
        "diff", "--name-only", "--no-ext-diff", "--no-textconv"
    ) and command[5] == "--"


def _task6_local_mutation_git_argv(argv: list[str]) -> bool:
    """Exact local mutations handled by the separate writable helper."""
    command = tuple(argv)
    if command == ("worktree", "prune"):
        return True
    if (
        len(command) == 6
        and command[:3] == ("worktree", "add", "-b")
        and _safe_git_operand(command[3])
        and Path(command[4]).is_absolute()
        and _safe_git_operand(command[5])
    ):
        return True
    if (
        len(command) == 4
        and command[:2] == ("worktree", "add")
        and Path(command[2]).is_absolute()
        and _safe_git_operand(command[3])
    ):
        return True
    if command[:2] == ("worktree", "remove") and (
        len(command) == 3 and Path(command[2]).is_absolute()
        or len(command) == 4 and command[2] == "--force" and Path(command[3]).is_absolute()
    ):
        return True
    if (
        len(command) == 4
        and command[:2] == ("branch", "-m")
        and _safe_git_operand(command[2])
        and _safe_git_operand(command[3])
    ):
        return True
    if command == ("merge", "--abort"):
        return True
    if (
        len(command) == 4
        and command[:3] == ("merge", "--no-ff", "--no-edit")
        and _safe_git_operand(command[3])
    ):
        return True
    return False


def _task6_approved_push_git_argv(argv: list[str]) -> bool:
    """The one push form an injected runner is handed (see ``GitMerger.push``)."""
    command = tuple(argv)
    return (
        len(command) == 3
        and command[0] == "push"
        and safe_push_remote(command[1])
        and bool(re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?:refs/heads/.+", command[2]))
        and _safe_git_operand(command[2].split(":refs/heads/", 1)[1])
    )


#: A push target: an https or ssh URL without user credentials, query or
#: fragment, or an absolute local path; never something Git could parse as an
#: option or route through a remote helper (``ext::``).
_PUSH_URL = re.compile(
    r"(?:https://[A-Za-z0-9.-]+(?::\d{1,5})?/[A-Za-z0-9._/-]+"
    r"|ssh://[A-Za-z0-9._-]+@[A-Za-z0-9.-]+(?::\d{1,5})?/[A-Za-z0-9._/-]+"
    r"|[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9._/-]+"
    r"|/[A-Za-z0-9._/-]+)"
)
#: The only push URL a GitHub token is ever attached to.
_GITHUB_PUSH_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?(?:\.git)?")


def safe_push_remote(value: str) -> bool:
    """Whether ``value`` may be handed to ``git push`` as the remote (L2).

    ``origin`` may (it is resolved to its configured URL first). A URL may when
    it carries no credentials and no ``..`` segment: a stored remote is data,
    so a leading ``-`` or an ``ext::`` transport must never reach Git as one.
    """
    text = str(value or "")
    if text == "origin":
        return True
    if not text or len(text) > 2048 or text.startswith("-"):
        return False
    if not _PUSH_URL.fullmatch(text):
        return False
    return ".." not in text and "//" not in text.split("://", 1)[-1]


def github_push_url(value: str) -> bool:
    """Whether ``value`` is exactly ``https://github.com/<owner>/<repo>[.git]``."""
    return bool(_GITHUB_PUSH_URL.fullmatch(str(value or ""))) and ".." not in str(value)


#: Settings put on the push's command line: nothing a repository or the
#: environment configured may redirect, proxy or authenticate it.
_PUSH_HARDENING = (
    "core.hooksPath=/dev/null",
    "core.fsmonitor=false",
    "core.askPass=",
    "credential.helper=",
    "http.sslVerify=true",
    "http.proxy=",
    "http.followRedirects=false",
    "protocol.allow=never",
    "protocol.https.allow=always",
    "protocol.ssh.allow=always",
    "protocol.file.allow=always",
    "push.recurseSubmodules=no",
)


async def _isolated_push(
    objects_dir: Path,
    remote_url: str,
    refspec: str,
    *,
    auth_config: Mapping[str, str],
) -> tuple[int, str, str]:
    """``git push <url> <sha>:refs/heads/<branch>`` from an empty repository.

    The shared checkout's ``.git/config`` (proxies, ``insteadOf`` rewrites,
    credential helpers, ``sshCommand``, includes) is never read: Git runs in a
    fresh bare repository whose only link to the checkout is its object store,
    reached through ``GIT_ALTERNATE_OBJECT_DIRECTORIES``. System and global
    config are off, the environment's ``GIT_*`` is dropped, and the credential
    travels as ``GIT_CONFIG_KEY_*`` in the environment, never in the argv.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "/usr/bin/false",
            "SSH_ASKPASS": "/usr/bin/false",
        }
    )
    with tempfile.TemporaryDirectory(prefix="dispatch-push-") as scratch:
        git_dir = Path(scratch) / "push.git"
        code, _out, err = await _git_command(
            ["init", "-q", "--bare", "--template=", str(git_dir)], Path(scratch), env=env
        )
        if code != 0:
            return code, "", err
        push_env = dict(env)
        push_env["GIT_DIR"] = str(git_dir)
        push_env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(objects_dir)
        extra = list(auth_config.items())
        for index, (key, value) in enumerate(extra):
            push_env[f"GIT_CONFIG_KEY_{index}"] = key
            push_env[f"GIT_CONFIG_VALUE_{index}"] = value
        if extra:
            push_env["GIT_CONFIG_COUNT"] = str(len(extra))
        command: list[str] = []
        for item in _PUSH_HARDENING:
            command.extend(["-c", item])
        command.extend(["push", "--no-verify", "--", remote_url, refspec])
        return await _git_command(command, Path(scratch), env=push_env)


def _safe_git_operand(value: str) -> bool:
    return bool(value) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,255}", value)) and all(
        part not in {"", ".", ".."} for part in value.split("/")
    ) and not value.endswith(("/", ".", ".lock")) and "//" not in value


def _ensure_owned_directory(root: Path, relative: Path) -> Path:
    """Create a root-relative directory chain without accepting symlink children."""
    base = Path(root).resolve()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not stat.S_ISDIR(base.lstat().st_mode):
        raise LaunchError("dispatch worktree root is not a directory")
    if relative.is_absolute() or ".." in relative.parts:
        raise LaunchError("unsafe dispatch worktree directory path")
    current = base
    for part in relative.parts:
        if part in ("", "."):
            continue
        child = current / part
        try:
            child.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            mode = child.lstat().st_mode
        except OSError as exc:
            raise LaunchError("could not inspect dispatch worktree directory") from exc
        if not stat.S_ISDIR(mode):
            raise LaunchError("dispatch worktree path contains a non-directory or symlink")
        current = child
    return current


async def _archive_dirty_worktree(
    repo: Path,
    worktree: Path,
    artifact_root: Path,
    git: Callable[[list[str], Path], Awaitable[tuple[int, str, str]]],
    identity: dict[str, str],
) -> Path:
    """Preserve a terminal dirty tree without following worker-created symlinks."""
    root_fd = -1
    try:
        root_fd = os.open(worktree, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        status_code, status, _ = await git(
            ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
            worktree,
        )
        head_code, head, _ = await git(["rev-parse", "HEAD"], worktree)
        diff_code, diff, _ = await git(
            ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"], worktree
        )
        index_code, index_diff, _ = await git(
            ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"],
            worktree,
        )
        untracked_code, untracked, _ = await git(
            ["ls-files", "--others", "--exclude-standard", "-z"], worktree
        )
        if any(
            code != 0
            for code in (status_code, head_code, diff_code, index_code, untracked_code)
        ):
            raise LaunchError("could not snapshot dirty worktree before terminal cleanup")
    except OSError as exc:
        if root_fd >= 0:
            os.close(root_fd)
        raise LaunchError("could not safely open worktree for recovery archive") from exc
    except BaseException:
        if root_fd >= 0:
            os.close(root_fd)
        raise
    root = artifact_root.parent
    artifact_root = _ensure_owned_directory(root, Path(artifact_root.name))
    artifact = artifact_root / f"{time.time_ns()}-{hashlib.sha256(str(worktree).encode()).hexdigest()[:12]}"
    artifact.mkdir(mode=0o700)
    try:
        (artifact / "metadata.json").write_text(
            json.dumps(
                {**identity, "head": head.strip(), "status": status},
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (artifact / "tracked.patch").write_text(diff, encoding="utf-8")
        (artifact / "index.patch").write_text(index_diff, encoding="utf-8")
        with tarfile.open(artifact / "untracked.tar", "w") as archive:
            for raw_path in untracked.split("\0"):
                if not raw_path:
                    continue
                relative = Path(raw_path)
                if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                    raise LaunchError("unsafe path in dirty worktree snapshot")
                parent_fd = os.dup(root_fd)
                try:
                    for parent in relative.parts[:-1]:
                        next_fd = os.open(
                            parent,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=parent_fd,
                        )
                        os.close(parent_fd)
                        parent_fd = next_fd
                    leaf = relative.parts[-1]
                    entry_stat = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                    info = tarfile.TarInfo(str(relative))
                    info.mtime = entry_stat.st_mtime
                    info.mode = stat.S_IMODE(entry_stat.st_mode)
                    if stat.S_ISLNK(entry_stat.st_mode):
                        info.type = tarfile.SYMTYPE
                        info.linkname = os.readlink(leaf, dir_fd=parent_fd)
                        archive.addfile(info)
                    elif stat.S_ISREG(entry_stat.st_mode):
                        file_fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                        try:
                            opened_stat = os.fstat(file_fd)
                            if not stat.S_ISREG(opened_stat.st_mode):
                                raise LaunchError("untracked worktree entry changed type during snapshot")
                            info.size = opened_stat.st_size
                            with os.fdopen(file_fd, "rb", closefd=False) as fileobj:
                                archive.addfile(info, fileobj)
                        finally:
                            os.close(file_fd)
                    else:
                        raise LaunchError("unsupported special file in dirty worktree snapshot")
                finally:
                    os.close(parent_fd)
        return artifact
    except OSError as exc:
        shutil.rmtree(artifact, ignore_errors=True)
        raise LaunchError("could not safely snapshot untracked worktree path") from exc
    except BaseException:
        shutil.rmtree(artifact, ignore_errors=True)
        raise
    finally:
        if root_fd >= 0:
            os.close(root_fd)


async def _registered_worktree(
    runner: GitCommand, repo: Path, path: Path, branch: str
) -> bool:
    code, output, _ = await runner(["worktree", "list", "--porcelain"], repo)
    if code != 0:
        return False
    expected_path = str(path.resolve())
    expected_branch = f"refs/heads/{branch}"
    blocks = output.strip().split("\n\n")
    return any(
        f"worktree {expected_path}" in block.splitlines()
        and f"branch {expected_branch}" in block.splitlines()
        for block in blocks
    )


# The longest argument, in bytes, that Linux starts a process with: it refuses an
# argument of 131,072 bytes or more (MAX_ARG_STRLEN, 32 pages, the terminating NUL
# included). A portable ceiling, not every kernel's: macOS accepts a longer one.
_MAX_ARG_BYTES = 131_071


def _require_startable(argv: list[str], line: Line) -> None:
    """Refuse, before any process exists, an argv no process can be started with.

    A planner's task is one argument of the launch script. The failures this
    catches (a NUL byte, text the argument encoding cannot encode, an argument at
    Linux's per-argument limit, which is a ceiling chosen to be portable: macOS
    accepts a longer one) surface on Linux as an ``OSError`` or ``ValueError`` out
    of the spawn, which the machine does not catch: the error leaves the machine's
    launch loop, with no run for the line. They are decided here, before the spawn,
    because an error from the spawn itself cannot say whether the child ran (Python can create the process and
    then fail to register it), and retrying a line that already ran runs it
    twice. Only the argument's index is named; the text can be the task.
    """
    for index, argument in enumerate(argv):
        try:
            encoded = os.fsencode(argument)
        except UnicodeError:
            problem = "holds text that has no bytes"
        else:
            if b"\x00" in encoded:
                problem = "holds a NUL byte"
            elif len(encoded) > _MAX_ARG_BYTES:
                problem = "is longer than the portable limit for a process argument"
            else:
                continue
        # Neither the model nor the text is named: both are caller-controlled and
        # this message is logged and stored (the argument that failed can be the model).
        raise LaunchError(
            f"The launch of line {line.id} was not started: argument {index} {problem}"
        )


def _worker_exit_error(model: str, line: Line, run_id: str, code: int) -> LaunchError:
    """Describe a non-recovery worker exit while preserving launch retry semantics."""
    error = LaunchError(f"{model} exited {code} for line {line.id}")
    error.worker_started = True
    error.run_id = run_id
    error.exit_code = code
    return error


def provider_launch_env(model: str, *, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Provider keys a dispatch Worker needs and the backend does not have (N26).

    A dispatch Worker is a child of the backend, so it does not get what cond
    hands its daemons. The same rules apply here: only names on the provider's
    allow-list (``ops_fleet.PROVIDER_ENV``), read from ``~/.conductor/secrets.env``
    and the provider's env files (e.g. ``~/.config/deepseek/env``), each a
    private mode-600 regular file. A name already set in the environment wins,
    and no value is ever logged. Any problem yields no extra names.
    """
    try:
        from src.conductor_cli.ops_fleet import (
            PROVIDER_ENV_FILES,
            _mode_is_600,
            allowlist_for,
            parse_env_text,
        )
    except ImportError:
        return {}
    source = os.environ if environ is None else environ
    provider = (model or "").strip().lower()
    allowed = allowlist_for(provider)
    if not allowed:
        return {}
    home = Path(source.get("HOME") or Path.home()).expanduser()
    conductor_home = Path(source.get("CONDUCTOR_HOME") or home / ".conductor").expanduser()
    files = [conductor_home / "secrets.env"]
    for raw in PROVIDER_ENV_FILES.get(provider, ()):
        files.append(home / raw[2:] if raw.startswith("~/") else Path(raw).expanduser())
    extra: dict[str, str] = {}
    for path in files:
        if "zshenv" in path.name or not _mode_is_600(path):
            continue
        try:
            parsed = parse_env_text(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 -- a bad file adds nothing, and says nothing
            continue
        for name, value in parsed.items():
            if name in allowed and value and not source.get(name) and name not in extra:
                extra[name] = value
    return extra


_TRUNCATED_MARKER = "\n[truncated]"
# The most bytes of changed files a dirty-worktree diff is read for.
_DIFF_READ_CAP_BYTES = 256 * 1024


def _head_sizes(listing: str) -> dict[str, int]:
    """Blob sizes at HEAD from ``ls-tree -r -l -z HEAD`` (``mode type oid size\tpath``)."""
    sizes: dict[str, int] = {}
    for entry in listing.split("\0"):
        meta, sep, path = entry.partition("\t")
        if not sep:
            continue
        fields = meta.split()
        if len(fields) == 4 and fields[1] == "blob" and fields[3].isdigit():
            sizes[path] = int(fields[3])
    return sizes


def _diff_read_budget(
    worktree: Path, status: str, head_sizes: dict[str, int]
) -> tuple[bool, str]:
    """Whether ``git diff HEAD`` stays small enough to read at all.

    Git output cannot be capped while it is read, so the diff's size is
    bounded first: for every changed path, its size at HEAD (``ls-tree``)
    plus its size on disk, doubled for the base85 of a binary patch. A file
    truncated to one byte still counts its HEAD size, and a deleted file is
    counted from HEAD. A rename, a quoted path or an unreadable path means
    names only. Symlinks are not followed.
    """
    total = 0
    for raw in status.splitlines():
        if len(raw) < 4:
            continue
        code, path = raw[:2], raw[3:]
        if "R" in code or "C" in code or " -> " in path:
            return False, "the tree has renamed or copied files"
        if path.startswith('"'):
            return False, "a changed path needs quoting"
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            return False, "a changed path is outside the worktree"
        total += head_sizes.get(path, 0)
        try:
            info = os.lstat(worktree / relative)
        except FileNotFoundError:
            if path not in head_sizes:
                return False, "a changed path could not be read"
            info = None
        except OSError:
            return False, "a changed path could not be read"
        if info is not None and not stat.S_ISDIR(info.st_mode):
            total += info.st_size
        if 2 * total > _DIFF_READ_CAP_BYTES:
            return False, f"the diff could exceed {_DIFF_READ_CAP_BYTES} bytes"
    return True, ""


def _drop_binary_hunks(diff: str) -> str:
    """Keep each binary file's header line only, never its patch body."""
    parts = re.split(r"(?m)^(?=diff --git )", diff)
    kept: list[str] = []
    for part in parts:
        if not part:
            continue
        if "\nGIT binary patch\n" in part or re.search(r"(?m)^Binary files .* differ$", part):
            header = part.splitlines()[0]
            kept.append(f"{header}\n(binary file; contents not shown)\n")
        else:
            kept.append(part)
    return "".join(kept)


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Cut ``text`` to at most ``max_bytes`` UTF-8 bytes, marker included."""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    marker = _TRUNCATED_MARKER.encode("utf-8")
    keep = max(0, max_bytes - len(marker))
    head = data[:keep].decode("utf-8", errors="ignore")
    return (head.encode("utf-8") + marker)[: max(0, max_bytes)].decode("utf-8", errors="ignore")


class ScriptLauncher:
    """Launches a line through the shared headless script.

    ``launch.sh model task --skills=... --impact-map=...`` is the seam the
    legacy handoff already had; the run id it returns is the log file the
    Monitor reads, so a run started in one process can be watched in another.
    """

    def __init__(
        self,
        runs_dir: Path,
        *,
        script: Path | None = None,
        cwd: Path | None = None,
        skills_hub: str = "",
        impact_map: str | None = None,
        runner: RunCommand | None = None,
        id_factory: Callable[[Line], str] | None = None,
        worktrees_dir: Path | None = None,
        dispatch_id: str = "",
        git_runner: GitCommand | None = None,
        provider_capacity: Mapping[str, int] | None = None,
        commit_prompt_timeout: float | None = None,
        sandbox_policy: str = "",
    ) -> None:
        self._runs_dir = Path(runs_dir)
        self._script = script or DEFAULT_LAUNCH_SCRIPT
        self._cwd = Path(cwd) if cwd else Path.cwd()
        self._skills_hub = skills_hub
        self._impact_map = impact_map
        self._runner = runner or _run_command
        self._worktrees_dir = Path(worktrees_dir) if worktrees_dir else self._runs_dir.parent / "dispatch-worktrees"
        self._dispatch_id = dispatch_id
        self._git_runner = git_runner or _git_command
        self._id_factory = id_factory or (lambda line: f"{line.id}-{time.monotonic_ns():x}")
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._recovery_sessions: WorkerSessionManager | None = None
        self._provider_capacity = (
            provider_capacity_from_env()
            if provider_capacity is None
            else _normalise_capacity(provider_capacity)
        )
        self._commit_prompt_timeout = (
            commit_prompt_timeout_from_env()
            if commit_prompt_timeout is None or not commit_prompt_timeout > 0
            else float(commit_prompt_timeout)
        )
        self._sandbox_policy = str(sandbox_policy or "").strip()
        _LIVE_LAUNCHERS.add(self)

    def capacity_for(self, provider: str) -> int:
        """Concurrent worker launches this launcher allows for ``provider``."""
        return self._provider_capacity.get(provider.strip().lower(), DEFAULT_PROVIDER_CAPACITY)

    def _launch_env(self, model: str, line_id: str | None = None) -> dict[str, str]:
        """The provider keys plus this workspace's sandbox policy.

        ``provider_launch_env`` only adds credential names, and a name already
        in the backend's environment wins. The policy is the one value this
        workspace contributes to every child process, on the run and on the
        commit nudge, so both turns see the same sandbox. The launch script is
        the enforcement point, not this export: it refuses ``read-only`` (a
        WORK line must write and commit the checkout) and any unknown policy
        before it starts a Worker.
        """
        env = provider_launch_env(model)
        if self._sandbox_policy:
            env[SANDBOX_POLICY_ENV] = self._sandbox_policy
        # N73 L4: an adopted Super-agent choice (model id, effort, runtime).
        # Nothing is registered in off/shadow, so the argv stays main's.
        from .superagent_assign import launch_env as superagent_launch_env

        env.update(superagent_launch_env.for_line(self._dispatch_id, line_id))
        return env

    def log_path(self, run_id: str) -> Path:
        return self._runs_dir / f"{run_id}.log"

    @property
    def runs_dir(self) -> Path:
        """Where this launcher keeps run logs and the N29 recovery files."""
        return self._runs_dir

    def recovery_session_manager(
        self,
        *,
        baseline: str | None = None,
        task_file: str | Path | None = None,
        report: str | Path | None = None,
    ) -> WorkerSessionManager:
        """Return the launcher's private, lazily created Worker session store.

        Recovery state belongs beside this dispatcher's run logs, not in the
        user's interactive native-CLI session store. The first call may attach
        the known baseline and artifact paths; absent metadata remains absent.
        """
        if self._recovery_sessions is None:
            root = self._runs_dir / "recovery-sessions"
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(root, 0o700)
            self._recovery_sessions = WorkerSessionManager(
                path=root / "worker-sessions.json",
                store=NativeCliSessionStore(root / "sessions.json"),
                worktree=self._cwd,
                baseline=baseline,
                task_file=task_file,
                report=report,
                handoff_dir=root / "handoffs",
            )
        return self._recovery_sessions

    def argv(
        self, line: Line, model: str, skills: list[str], *, task: str | None = None
    ) -> list[str]:
        """The launch argv for one run; ``task`` replaces the line's own task."""
        body = line.task if task is None else task
        prompt = (
            build_line_delivery_task(
                dispatch_id=self._dispatch_id,
                line_id=line.id,
                branch=line.branch,
                task=body,
            )
            if line.branch and self._dispatch_id
            else body
        )
        argv = [str(self._script), model, prompt]
        if self._skills_hub:
            argv.append(f"--skills={self._skills_hub}")
        elif skills:
            argv.append(f"--skills={','.join(skills)}")
        if self._impact_map:
            argv.append(f"--impact-map={self._impact_map}")
        return argv

    async def launch(
        self,
        line: Line,
        model: str,
        skills: list[str],
        *,
        before_spawn: Callable[[], Awaitable[None]] | None = None,
    ) -> str:
        if not self._script.exists():
            raise LaunchError(f"launch script not found: {self._script}")
        argv = self.argv(line, model, skills)
        _require_startable(argv, line)
        run_id = line.current_run or self._id_factory(line)
        code, run_cwd, _output, start_commit = await self._execute_worker(
            argv, line, run_id, allow_dirty=False, before_spawn=before_spawn
        )
        if code != 0:
            raise _worker_exit_error(model, line, run_id, code)
        if line.branch:
            try:
                # This surface returns only a run id, so the machine cannot
                # see that a run left the branch where it found it. Keep
                # N34's guard here (``fix_round``): the empty run stops
                # ``no_new_commit`` with its head, and the machine's N38
                # ledger decides whether that head may go on to review.
                await self._verify_submitted_worktree(
                    line,
                    model,
                    skills,
                    run_cwd,
                    run_id=run_id,
                    start_commit=start_commit,
                    fix_round=True,
                    before_spawn=before_spawn,
                )
            except (
                GitDeliveryError,
                TimeoutError,
                subprocess.TimeoutExpired,
                NativeCliUnavailable,
            ) as exc:
                line.status = LineStatus.FAILED
                line.failure_reason = "git_evidence_unavailable"
                line.current_run = None
                raise DispatchError(f"Git evidence for line {line.id} is unavailable") from exc
        return run_id

    async def launch_worker(
        self,
        line: Line,
        model: str,
        skills: list[str],
        *,
        session_id: str | None,
        session_mode: SessionMode | None,
        handoff_path: Path | None,
        allow_dirty_worktree: bool = False,
        fix_round: bool = False,
        before_spawn: Callable[[], Awaitable[None]] | None = None,
        context_key: str | None = None,
    ) -> LaunchResult:
        """Run one recovery-aware Worker and retain its output only in memory.

        The legacy ``launch`` path stays byte-for-byte compatible. This seam
        adds optional script arguments only when the recovery machine supplies
        session or handoff state.

        ``fix_round`` says this run exists to clear blockers a reviewer
        already raised. It changes one thing only: a run that leaves the
        branch where it found it stops ``no_new_commit`` (N34) instead of
        being read as a submission of the work already there (N49).

        ``context_key`` (N29) names the line's recovery session: the run gets
        a context budget file and a handoff request path, and a valid or
        invalid handoff request returns without the submission check (the
        Worker may leave uncommitted work for the next session). Without it
        argv and behaviour are unchanged.
        """
        if not self._script.exists():
            raise LaunchError(f"launch script not found: {self._script}")
        if (session_id is None) != (session_mode is None):
            raise ValueError("session_id and session_mode must be supplied together")
        provider = model.strip().lower()
        if session_id is not None:
            capabilities = _SESSION_CAPABILITIES.get(provider, frozenset())
            if session_mode not in capabilities:
                raise LaunchError(f"{provider} does not support {session_mode} sessions")

        argv = self.argv(line, model, skills)
        if session_id is not None:
            argv.extend((f"--session-id={session_id}", f"--session-mode={session_mode}"))
        if handoff_path is not None:
            argv.append(f"--handoff={handoff_path}")
        budget: _BudgetRun | None = None
        if context_key is not None:
            run_id = line.current_run or self._id_factory(line)
            budget = self._prepare_context_budget(context_key, run_id, provider)
            if budget is not None:
                paths = budget.paths
                argv.extend(
                    (
                        f"{_budget.ARG_CONTEXT_BUDGET}={paths.budget_file}",
                        f"{_budget.ARG_HANDOFF_REQUEST}={paths.request_file}",
                        f"{_budget.ARG_HISTORY_MANIFEST}={paths.manifest}",
                        f"{_budget.ARG_RUN_LOG}={paths.run_log}",
                    )
                )
            _require_startable(argv, line)
        else:
            _require_startable(argv, line)
            run_id = line.current_run or self._id_factory(line)
        code, run_cwd, output, start_commit = await self._execute_worker(
            argv, line, run_id, allow_dirty=allow_dirty_worktree, before_spawn=before_spawn
        )
        request: _budget.HandoffRequest | None = None
        post: ContextEstimate | None = None
        if budget is not None:
            request, post = self._finish_context_budget(
                budget,
                run_id=run_id,
                provider=provider,
                code=code,
                output=output,
                session_id=session_id,
                handoff_path=handoff_path,
            )
        if code in _EXIT_CLASS:
            classification = classify_failure(model, output, exit_code=code)
            failure_session_id, failure_usage = _session_metadata(provider, output, session_id)
            # 79 is the shell's generic worker-failure arm, not a class: keep
            # the richer class the shared classifier can still infer (for
            # example overloaded or network). The dedicated 75-78 exit codes
            # remain a fallback if the outer runner could not capture output.
            failure_class = classification.failure_class
            retry_after_seconds = classification.retry_after_seconds
            not_migrated, worker_status, sweep_failed = (
                _objects_not_migrated(output) if code == 79 else (False, None, False)
            )
            if not_migrated and sweep_failed:
                # Live Worker processes kept the launcher from moving the
                # objects at all: a local fault, never the provider's.
                failure_class = FailureClass.UNKNOWN
                retry_after_seconds = None
                reason_code = OBJECTS_NOT_MIGRATED_SWEEP_FAILED_REASON
            elif not_migrated:
                # The launcher's own quarantine migration failed. The reason
                # says so; the class is the Worker's own quota, rate limit,
                # auth or timeout failure when it had one, so the provider
                # skip and the backoff still happen, and unknown otherwise.
                failure_class, retry_after_seconds = _objects_not_migrated_class(
                    model, output, worker_status
                )
                reason_code = OBJECTS_NOT_MIGRATED_REASON
            else:
                if code != 79 and failure_class is FailureClass.CRASH:
                    failure_class = _EXIT_CLASS[code]
                reason_code = (
                    classification.reason_code
                    if classification.failure_class is failure_class
                    else f"worker_{failure_class.value}"
                )
            failure = WorkerLaunchFailure(
                run_id=run_id,
                exit_code=code,
                output=output,
                session_id=failure_session_id,
                retry_after_seconds=retry_after_seconds,
                failure_class=failure_class,
                reason_code=reason_code,
                usage=failure_usage,
            )
            # A run can commit and then hit a rate limit. Say where it left
            # the branch, so the commits are attributed to this author.
            failure.start_head = start_commit
            failure.head = await self._branch_head(line, run_cwd)
            raise failure
        if code != 0:
            error = _worker_exit_error(model, line, run_id, code)
            # Exit 1 and friends can follow a commit; say where the branch is.
            error.start_head = start_commit
            error.head = await self._branch_head(line, run_cwd)
            raise error
        parsed_session_id, usage = _session_metadata(provider, output, session_id)
        head: str | None = None
        if request is not None and request.status == _budget.HANDOFF_OK:
            # The Worker asked for a handoff: it may leave uncommitted work
            # for the next session, so the submission check does not run.
            # Only a valid request does; an invalid one is a normal exit.
            return LaunchResult(
                run_id=run_id,
                session_id=parsed_session_id,
                output=output,
                tokens=int(usage.input_tokens) + int(usage.output_tokens),
                usage=usage,
                start_head=start_commit,
                head=await self._branch_head(line, run_cwd) if line.branch else None,
                handoff_request=request.status,
                worker_report=request.report,
                self_report_bytes=request.byte_count,
                estimate=post,
                handoff_request_file=budget.paths.request_file if budget is not None else None,
            )
        if request is not None and request.status == _budget.HANDOFF_REFUSED_SYMLINK:
            logger.warning("handoff request of run %s refused: symlink", run_id)
        if request is not None and request.status == _budget.HANDOFF_INVALID:
            logger.warning(
                "handoff request of run %s invalid; the run takes the normal exit path", run_id
            )
        if line.branch:
            turn = _CommitTurn(session_id=parsed_session_id)
            try:
                head = await self._verify_submitted_worktree(
                    line,
                    model,
                    skills,
                    run_cwd,
                    run_id=run_id,
                    start_commit=start_commit,
                    fix_round=fix_round,
                    before_spawn=before_spawn,
                    turn=turn,
                )
            except (UncommittedChangesError, NoNewCommitError) as exc:
                exc.session_id = turn.session_id
                exc.usage = _add_usage(usage, turn.usage)
                exc.start_head = start_commit
                if isinstance(exc, UncommittedChangesError):
                    exc.head = await self._branch_head(line, run_cwd)
                raise
            except (
                GitDeliveryError,
                TimeoutError,
                subprocess.TimeoutExpired,
                NativeCliUnavailable,
            ) as exc:
                line.status = LineStatus.FAILED
                line.failure_reason = "git_evidence_unavailable"
                line.current_run = None
                raise DispatchError(f"Git evidence for line {line.id} is unavailable") from exc
            parsed_session_id = turn.session_id
            usage = _add_usage(usage, turn.usage)
        return LaunchResult(
            run_id=run_id,
            session_id=parsed_session_id,
            output=output,
            tokens=int(usage.input_tokens) + int(usage.output_tokens),
            usage=usage,
            start_head=start_commit,
            head=head,
            handoff_request=(
                request.status
                if request is not None
                and request.status in (_budget.HANDOFF_REFUSED_SYMLINK, _budget.HANDOFF_INVALID)
                else _budget.HANDOFF_NONE
            ),
            estimate=post,
        )

    def _prepare_context_budget(
        self, key: str, run_id: str, provider: str
    ) -> _BudgetRun | None:
        """Write this run's budget file; None (launch without it) on any trouble."""
        try:
            manager = self.recovery_session_manager()
            ensure_lineage = getattr(manager, "ensure_lineage", None)
            if ensure_lineage is None:
                return None
            paths = _budget.prepare_run_paths(self._runs_dir, run_id, key)
            lineage = ensure_lineage(key)
            record = manager.current(key)
            turns = int(record.turns) if record is not None else 0
            reported = (
                int(record.input_tokens) + int(record.output_tokens) if record is not None else 0
            )
            pre = _budget.estimate(
                provider,
                session_turns=turns,
                prior_log_bytes=_budget.lineage_log_bytes(
                    paths.manifest, lineage.current, self._runs_dir
                ),
                reported_tokens=reported,
                env=os.environ,
            )
            _budget.write_budget_file(
                paths.budget_file,
                _budget.render_budget_block(
                    pre, lineage, soft_ratio=_budget.soft_ratio(os.environ)
                ),
            )
        except (OSError, ValueError) as exc:
            logger.warning(
                "context budget for run %s unavailable (%s); launching without it",
                run_id,
                type(exc).__name__,
            )
            return None
        return _BudgetRun(paths, lineage, turns, reported, _utc_now())

    def _finish_context_budget(
        self,
        budget: _BudgetRun,
        *,
        run_id: str,
        provider: str,
        code: int,
        output: str,
        session_id: str | None,
        handoff_path: Path | None,
    ) -> tuple[_budget.HandoffRequest, ContextEstimate | None]:
        """Read the handoff request (exit 0 only), estimate, append the manifest.

        Best effort: nothing here fails the launch, and no log content is kept.
        """
        paths = budget.paths
        none = _budget.HandoffRequest(_budget.HANDOFF_NONE, None, None, 0)
        if code == 0:
            # An ``ok`` request stays on disk until the machine has saved the
            # handoff (it deletes it then); anything else is removed now.
            request = _budget.read_handoff_request(paths.request_file, delete=False)
            if request.status != _budget.HANDOFF_OK:
                _budget.delete_handoff_request(paths.request_file)
        else:
            # A failed run's request is never read: drop a stray one.
            request = none
            try:
                os.unlink(paths.request_file)
            except OSError:
                pass
        post: ContextEstimate | None = None
        try:
            _sid, usage = _session_metadata(provider, output, session_id)
            lineage = budget.lineage
            try:
                run_log_bytes = os.lstat(self.log_path(run_id)).st_size
            except OSError:
                run_log_bytes = 0
            post = _budget.estimate(
                provider,
                session_turns=budget.turns + 1,
                prior_log_bytes=_budget.lineage_log_bytes(
                    paths.manifest, lineage.current, self._runs_dir
                ),
                current_run_log_bytes=run_log_bytes,
                reported_tokens=budget.reported_tokens
                + int(usage.input_tokens)
                + int(usage.output_tokens),
                env=os.environ,
            )
            runs_root = self._runs_dir.resolve()
            note = None
            if handoff_path is not None:
                resolved = Path(handoff_path).resolve()
                if resolved != runs_root and resolved.is_relative_to(runs_root):
                    note = str(handoff_path)
            _budget.append_manifest(
                paths.manifest,
                _budget.ManifestEntry(
                    run_id=run_id,
                    lineage_current=lineage.current,
                    session_generation=int(lineage.generation),
                    provider=provider,
                    log_path=str(self.log_path(run_id)),
                    started_at=budget.started_at,
                    ended_at=_utc_now(),
                    exit_code=code,
                    handoff_path=note,
                    estimate_source=post.source,
                    handoff_request=request.status,
                    lineage_first=lineage.first,
                    lineage_previous=lineage.previous,
                ),
                runs_dir=self._runs_dir,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            logger.warning(
                "context manifest for run %s not appended (%s)", run_id, type(exc).__name__
            )
        return request, post

    async def _execute_worker(
        self,
        argv: list[str],
        line: Line,
        run_id: str,
        *,
        allow_dirty: bool,
        before_spawn: Callable[[], Awaitable[None]] | None = None,
    ) -> tuple[int, Path, str, str | None]:
        """Run both launch surfaces in the same registered line worktree.

        ``before_spawn`` runs after the worktree is ready and immediately before
        the process starts; it raises to stop the launch (the machine's epoch
        fence, so a cancel saved during the worktree preparation wins).
        """
        lease: _env_preflight.WorktreeLease | None = None
        try:
            self._env_preflight(argv, line)
            if line.branch:
                # Leased on the expected path before it is prepared, so a
                # second session reads worktree_in_use, not uncommitted_changes.
                lease = self._lease_worktree(self._expected_line_worktree(line), line, run_id)
            run_cwd = (
                await self._prepare_line_worktree(line, allow_dirty=allow_dirty)
                if line.branch
                else self._cwd
            )
            start_commit: str | None = None
            if line.branch:
                code, output, _ = await self._line_git(
                    self._cwd.resolve(), run_cwd, ["rev-parse", "HEAD"]
                )
                if code != 0 or not output.strip():
                    raise LaunchError(f"could not capture starting commit for line {line.id}")
                start_commit = output.strip()
            log_path = self.log_path(run_id)
            provider = (argv[1] if len(argv) > 1 else "").strip().lower()
            capacity = self.capacity_for(provider)
            gate = _provider_gate(provider)
            if not gate.try_acquire(capacity):
                # Fail fast before queueing: a fence that already refuses
                # should not hold a place in the provider queue.
                if before_spawn is not None:
                    await before_spawn()
                await gate.acquire(capacity)
            try:
                # (Re-)check the epoch fence with the slot held: it may have
                # moved while this launch waited for the provider.
                if before_spawn is not None:
                    await before_spawn()
                if self._runner is _run_command:
                    code = await _run_command(
                        argv,
                        run_cwd,
                        log_path,
                        registry=self._processes,
                        run_id=run_id,
                        env=self._launch_env(argv[1] if len(argv) > 1 else "", line.id),
                    )
                else:
                    code = await self._runner(argv, run_cwd, log_path)
            finally:
                gate.release()
            try:
                output = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                output = ""
            return code, run_cwd, output, start_commit
        except (
            GitDeliveryError,
            TimeoutError,
            subprocess.TimeoutExpired,
            NativeCliUnavailable,
        ) as exc:
            line.status = LineStatus.FAILED
            line.failure_reason = "git_evidence_unavailable"
            line.current_run = None
            raise DispatchError(f"Git evidence for line {line.id} is unavailable") from exc
        finally:
            if lease is not None:
                lease.close()

    def _env_preflight(self, argv: list[str], line: Line) -> None:
        """Live runs only: refuse up front when the Worker PATH lacks git/gh."""
        if self._runner is not _run_command:
            return
        env = self._launch_env(argv[1] if len(argv) > 1 else "", line.id) or {}
        path_env = env.get("PATH") or os.environ.get("PATH", "")
        problems = _env_preflight.preflight(
            path_env, require_gh=_env_preflight.require_gh_from_env()
        )
        if problems:
            # The machine records the park/reassign reason when it handles the
            # LaunchError; the preflight detail stays on the exception.
            raise _env_preflight.EnvPreflightError(problems)

    def _lease_worktree(
        self, worktree: Path, line: Line, run_id: str
    ) -> _env_preflight.WorktreeLease | None:
        """Live runs only: one session per worktree until the Worker exits."""
        if self._runner is not _run_command:
            return None
        # The lease writes under runs_dir, so the root checks
        # ``_prepare_line_worktree`` makes come first: nothing is written
        # into the repository.
        repo = self._cwd.resolve()
        runs_root = self._runs_dir.resolve()
        if runs_root == repo or repo in runs_root.parents:
            raise LaunchError("dispatch runs directory must be outside repo_root")
        root = self._worktrees_dir.resolve()
        if root == repo or repo in root.parents:
            raise LaunchError("dispatch worktree root must be outside repo_root")
        lease = _env_preflight.acquire_worktree_lease(
            worktree, self._runs_dir / "worktree-locks", f"pid {os.getpid()} run {run_id}"
        )
        if isinstance(lease, _env_preflight.EnvProblem):
            raise _env_preflight.EnvPreflightError([lease])
        return lease

    async def pin_base_commit(self) -> str:
        """Capture the repository base before the first line run."""
        repo = self._cwd.resolve()
        code, output, _ = await self._line_git(repo, repo, ["rev-parse", "HEAD"])
        if code != 0 or not output.strip():
            raise LaunchError("could not pin repository base commit")
        return output.strip()

    def _expected_line_worktree(self, line: Line) -> Path:
        """Where ``_prepare_line_worktree`` puts the line (creates nothing)."""
        repo_key = hashlib.sha256(str(self._cwd.resolve()).encode()).hexdigest()[:24]
        dispatch_key = hashlib.sha256(self._dispatch_id.encode()).hexdigest()[:24]
        return self._line_worktree_path(self._worktrees_dir.resolve(), repo_key, dispatch_key, line)

    def line_worktree(self, line: Line) -> Path | None:
        """The line's existing worktree path, or None (read-only, creates nothing)."""
        if not self._dispatch_id or not line.branch:
            return None
        path = self._expected_line_worktree(line)
        return path if path.is_dir() else None

    async def prepare_app_worktree(self, line: Line) -> dict[str, object]:
        """N80 L4: the line's own worktree for the app that claimed it.

        The same dispatch-owned branch and path a Worker would get; no
        Worker starts. A dirty tree is kept (the app may be resuming). The
        worktree gets the refusing ``pre-push`` hook the Ticket side installs
        (``collab.leases.install_push_guard``); ``push_guard`` says whether it
        is in place (it needs ``extensions.worktreeConfig`` in the repo).
        """
        from src.server.services.collab.leases import install_push_guard

        path = await self._prepare_line_worktree(line, allow_dirty=True)
        try:
            guarded = await install_push_guard(path)
        except Exception:  # noqa: BLE001 - recorded as unguarded
            guarded = False
        return {"path": str(path), "push_guard": bool(guarded)}

    async def app_branch_facts(self, line: Line, base: str) -> dict[str, object] | None:
        """N80 L4: the line branch as it is now, read by Conductor (read-only git).

        ``tip`` (the branch head), ``paths`` (changed since ``base``) and
        ``dirty`` (the worktree has uncommitted changes). None when any of
        them cannot be read: the report is then refused.
        """
        worktree = self.line_worktree(line)
        if worktree is None or not re.fullmatch(r"[0-9a-f]{7,64}", base or ""):
            return None
        repo = self._cwd.resolve()
        try:
            if not self._line_ref_is_plain(repo, line.branch):
                return None
            code, tip, _ = await self._line_git(
                repo, worktree, ["rev-parse", "--verify", line.branch]
            )
            tip = tip.strip()
            if code != 0 or not _is_object_id(tip):
                return None
            code, names, _ = await self._line_git(
                repo, worktree,
                ["diff", "--name-only", "--no-ext-diff", "--no-textconv", f"{base}..{tip}", "--"],
            )
            if code != 0:
                return None
            code, dirty, _ = await self._line_git(
                repo, worktree,
                ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
            )
        except (GitDeliveryError, LaunchError, OSError, TimeoutError, subprocess.TimeoutExpired):
            return None
        if code != 0:
            return None
        return {
            "tip": tip,
            "paths": [name for name in names.splitlines() if name.strip()],
            "dirty": bool(filter_porcelain(dirty).strip()),
        }

    async def _prepare_line_worktree(self, line: Line, *, allow_dirty: bool = False) -> Path:
        repo = self._cwd.resolve()
        root = self._worktrees_dir.resolve()
        runs_root = self._runs_dir.resolve()
        if runs_root == repo or repo in runs_root.parents:
            raise LaunchError("dispatch runs directory must be outside repo_root")
        if root == repo or repo in root.parents:
            raise LaunchError("dispatch worktree root must be outside repo_root")
        if not self._dispatch_id:
            raise LaunchError("dispatch identity is required for per-line worktrees")
        initial_branch = f"feature/auto-dispatch-{self._dispatch_id}/line-{line.id}"
        reassigned_branch = re.fullmatch(rf"{re.escape(initial_branch)}-a[1-9][0-9]*", line.branch)
        if line.branch != initial_branch and not reassigned_branch:
            raise LaunchError(f"line {line.id} does not have its dispatch-owned branch")
        repo_key = hashlib.sha256(str(repo).encode()).hexdigest()[:24]
        dispatch_key = hashlib.sha256(self._dispatch_id.encode()).hexdigest()[:24]
        worktree = self._line_worktree_path(root, repo_key, dispatch_key, line)
        _ensure_owned_directory(root, worktree.parent.relative_to(root))
        if worktree.exists():
            self._validate_worktree_gitfile(repo, worktree)
            if not await self._registered_line_worktree(repo, worktree, line.branch):
                raise LaunchError(f"existing worktree for line {line.id} is not registered to its branch")
            code, branch, _ = await self._line_git(repo, worktree, ["branch", "--show-current"])
            if code != 0 or branch.strip() != line.branch:
                raise LaunchError(f"existing worktree for line {line.id} has a different branch")
            code, dirty, _ = await self._line_git(repo, worktree, ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"])
            if code != 0:
                raise LaunchError(f"could not inspect worktree for line {line.id}")
            if filter_porcelain(dirty) and not allow_dirty:
                raise UncommittedChangesError(f"uncommitted_changes for line {line.id}")
            return worktree
        base = line.base_commit or "HEAD"
        await self._line_git(repo, repo, ["worktree", "prune"])
        code, _, err = await self._line_git(
            repo, repo, ["worktree", "add", "-b", line.branch, str(worktree), base]
        )
        if code != 0:
            code, branch_tip, err = await self._line_git(
                repo, repo, ["rev-parse", "--verify", line.branch]
            )
            if code == 0:
                if reassigned_branch and (
                    not line.base_commit or branch_tip.strip() != line.base_commit
                ):
                    raise LaunchError(
                        f"refusing to reuse reassignment branch with commits for line {line.id}"
                    )
                code, _, err = await self._line_git(
                    repo, repo, ["worktree", "add", str(worktree), line.branch]
                )
        if code != 0:
            raise LaunchError(f"could not create worktree for line {line.id}: {err.strip()}")
        return worktree

    @staticmethod
    def _line_worktree_path(root: Path, repo_key: str, dispatch_key: str, line: Line) -> Path:
        line_key = hashlib.sha256(line.id.encode()).hexdigest()[:24]
        branch_key = hashlib.sha256(line.branch.encode()).hexdigest()[:24]
        return root / repo_key / dispatch_key / line_key / branch_key

    async def _owned_line_worktrees(
        self, dispatch: Dispatch, line: Line
    ) -> list[tuple[Path, str]]:
        """Find registered worktrees for this exact dispatch and line identity."""
        repo = self._cwd.resolve()
        root = self._worktrees_dir.resolve()
        repo_key = hashlib.sha256(str(repo).encode()).hexdigest()[:24]
        dispatch_key = hashlib.sha256(dispatch.id.encode()).hexdigest()[:24]
        line_key = hashlib.sha256(line.id.encode()).hexdigest()[:24]
        parent = root / repo_key / dispatch_key / line_key
        if parent.is_symlink():
            raise LaunchError("owned line worktree directory is a symlink")
        if not parent.exists():
            return []
        if not parent.is_dir():
            raise LaunchError("owned line worktree directory is not a directory")

        code, output, _ = await self._line_git(repo, repo, ["worktree", "list", "--porcelain"])
        if code != 0:
            raise LaunchError("could not list registered line worktrees")
        registered: dict[str, str] = {}
        for block in output.strip().split("\n\n"):
            path_line = next((item[9:] for item in block.splitlines() if item.startswith("worktree ")), None)
            branch_line = next((item[7:] for item in block.splitlines() if item.startswith("branch ")), None)
            if path_line and branch_line and branch_line.startswith("refs/heads/"):
                registered[str(Path(path_line).resolve())] = branch_line[len("refs/heads/") :]

        initial = f"{dispatch.branch_prefix}/line-{line.id}"
        allowed_reassignment = re.compile(rf"{re.escape(initial)}-a[1-9][0-9]*\Z")
        owned: list[tuple[Path, str]] = []
        for candidate in sorted(parent.iterdir()):
            if candidate.is_symlink() or not candidate.is_dir():
                raise LaunchError("unknown entry in owned line worktree directory")
            registered_branch = registered.get(str(candidate.resolve()))
            if registered_branch is None:
                raise LaunchError("unregistered entry in owned line worktree directory")
            if registered_branch != initial and not allowed_reassignment.fullmatch(registered_branch):
                raise LaunchError("registered branch does not belong to this dispatch line")
            expected_key = hashlib.sha256(registered_branch.encode()).hexdigest()[:24]
            if candidate.name != expected_key:
                raise LaunchError("registered worktree path does not match its branch identity")
            self._validate_worktree_gitfile(repo, candidate)
            owned.append((candidate, registered_branch))
        return owned

    async def cleanup_worktrees(self, dispatch: Dispatch) -> list[str]:
        """Remove terminal line worktrees, archiving dirty edits and keeping branches."""
        repo = self._cwd.resolve()
        root = self._worktrees_dir.resolve()
        errors: list[str] = []
        for line in dispatch.lines:
            try:
                await self._line_git(repo, repo, ["worktree", "prune"])
                owned_worktrees = await self._owned_line_worktrees(dispatch, line)
                for worktree, branch in owned_worktrees:
                    code, dirty, _ = await self._line_git(
                        repo,
                        worktree,
                        ["status", "--porcelain", "--untracked-files=all"],
                    )
                    if code != 0:
                        raise LaunchError(f"could not inspect terminal line worktree {line.id}")
                    # git refuses to remove a tree with untracked litter, so
                    # --force follows the raw status; only real edits are archived.
                    force = bool(dirty.strip())
                    artifact = None
                    if filter_porcelain(dirty).strip():
                        artifact = await _archive_dirty_worktree(
                            repo,
                            worktree,
                            root / "recovery-artifacts",
                            lambda argv, cwd: self._line_git(repo, cwd, argv),
                            {"dispatch_id": dispatch.id, "line_id": line.id, "branch": branch},
                        )
                    remove_args = ["worktree", "remove"]
                    if force:
                        remove_args.append("--force")
                    remove_args.append(str(worktree))
                    code, _out, err = await self._line_git(repo, repo, remove_args)
                    if code != 0:
                        raise LaunchError(
                            f"could not remove terminal line worktree {line.id}: {err.strip()}"
                        )
                    if artifact is not None:
                        logger.warning("Archived dirty terminal line worktree at %s", artifact)
                    await self._line_git(repo, repo, ["worktree", "prune"])
            except Exception as exc:
                message = f"line {line.id}: terminal worktree cleanup failed ({type(exc).__name__})"
                errors.append(message)
                logger.warning("Could not clean line worktree for %s", line.id)
        if errors and "terminal worktree cleanup failed" not in (dispatch.message or ""):
            dispatch.message = (
                f"{dispatch.message}; terminal worktree cleanup failed"
                if dispatch.message
                else "terminal worktree cleanup failed"
            )
        return errors

    async def _verify_line_worktree(
        self,
        line: Line,
        worktree: Path,
        *,
        run_id: str | None = None,
        start_commit: str | None = None,
        fix_round: bool = False,
    ) -> str:
        """Check the worker's result and return the branch HEAD it left.

        ``fix_round`` marks a run started to clear blockers a reviewer
        already raised. Such a run that leaves the branch exactly where it
        found it has not answered them, so N34's guard stands for it: the run
        stops ``no_new_commit`` instead of resubmitting the head the review
        already refused. A first run, a retry or a reassigned launch keeps
        N49's reading, and an unreviewed head still reaches G2 and review
        through the machine's N38 check.
        """
        repo = self._cwd.resolve()
        self._validate_worktree_gitfile(repo, worktree)
        if not self._line_ref_is_plain(repo, line.branch):
            raise LaunchError(f"line {line.id} branch ref is symbolic or a symlink")
        code, branch, _ = await self._line_git(repo, worktree, ["branch", "--show-current"])
        if code != 0 or branch.strip() != line.branch:
            raise LaunchError(f"worker left line {line.id} on an unexpected branch")
        code, dirty, _ = await self._line_git(repo, worktree, ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"])
        if code != 0:
            raise LaunchError(f"could not verify worktree for line {line.id}")
        if dirty:
            raise UncommittedChangesError(
                f"uncommitted_changes for line {line.id}", run_id=run_id
            )
        code, commit, _ = await self._line_git(repo, worktree, ["rev-parse", "HEAD"])
        if code != 0 or not commit.strip():
            raise LaunchError(f"could not verify commit for line {line.id}")
        if start_commit and commit.strip() == start_commit:
            ahead, base_id = await self._ahead_of_base(repo, worktree, line, commit.strip())
            if ahead and not fix_round:
                # N49: the tree is clean and the branch already carries commits
                # beyond the line's base, so this run submitted work that is
                # already there instead of doing nothing. The head is the
                # submission; only a branch with nothing on it at all still
                # stops the line as ``no_new_commit``.
                return commit.strip()
            error = NoNewCommitError(
                f"no_new_commit for line {line.id}",
                run_id=run_id,
                branch_has_commits=ahead,
            )
            error.head = commit.strip()
            error.base = base_id
            raise error
        return commit.strip()

    async def _verify_submitted_worktree(
        self,
        line: Line,
        model: str,
        skills: list[str],
        worktree: Path,
        *,
        run_id: str | None,
        start_commit: str | None,
        fix_round: bool = False,
        before_spawn: Callable[[], Awaitable[None]] | None = None,
        turn: _CommitTurn | None = None,
    ) -> str:
        """Verify a finished run, giving a dirty tree one prompt to commit (N49).

        R8c's sub-lines reported passing tests and left the work in the working
        tree, which ended them as ``uncommitted_changes`` and parked the line
        for a human. A worker that exits with changes still in the tree now
        gets exactly one extra turn that asks it to commit them, and the same
        verification runs again after it.

        When that turn does not leave a clean tree -- it refused to start, it
        exited non-zero, it ran past its bound, or it simply did not commit --
        the run stops as ``uncommitted_changes`` exactly as before and the tree
        is preserved for recovery. The prompt is never repeated, so a worker
        that will not commit cannot be nudged into an endless loop.

        ``turn`` carries the run's session in and the prompt's session and
        tokens out: the prompt is part of the run, so it resumes the run's
        session and its tokens are counted with the run's.
        """
        try:
            return await self._verify_line_worktree(
                line, worktree, run_id=run_id, start_commit=start_commit, fix_round=fix_round
            )
        except UncommittedChangesError:
            pass
        if not await self._prompt_to_commit(
            line,
            model,
            skills,
            worktree,
            run_id,
            before_spawn=before_spawn,
            turn=turn if turn is not None else _CommitTurn(),
        ):
            raise UncommittedChangesError(
                f"uncommitted_changes for line {line.id}", run_id=run_id
            )
        return await self._verify_line_worktree(
            line, worktree, run_id=run_id, start_commit=start_commit, fix_round=fix_round
        )

    async def _prompt_to_commit(
        self,
        line: Line,
        model: str,
        skills: list[str],
        worktree: Path,
        run_id: str | None,
        *,
        before_spawn: Callable[[], Awaitable[None]] | None = None,
        turn: _CommitTurn,
    ) -> bool:
        """Run one turn that asks the worker to commit its dirty tree (N49).

        True when the tree is clean afterwards. Every failure -- an argv no
        process can start, a non-zero exit, a turn past its bound, a git read
        that cannot run -- is False and leaves the tree exactly as the worker
        left it: a nudge that cannot run must not turn a recoverable dirty
        tree into a failed line.

        The turn is started the way ``_execute_worker`` starts a run: the
        spawn fence runs before the provider slot is waited for and again once
        it is held, so a drain or an epoch change while it waited stops the
        spawn. It resumes the run's own session when the provider can resume
        one (the worker that left the tree is the one asked to commit it),
        and what it spent is written to ``turn``.
        """
        provider = model.strip().lower()
        argv = self.argv(line, model, skills, task=COMMIT_PROMPT)
        session_id = resumable_session_id(turn.session_id)
        if session_id is not None and supports_session_resume(provider):
            argv.extend((f"--session-id={session_id}", "--session-mode=resume"))
        commit_run_id = f"{run_id}-commit" if run_id else "commit"
        try:
            _require_startable(argv, line)
        except Exception as exc:  # noqa: BLE001 -- a nudge that cannot run keeps the tree
            logger.warning(
                "Commit prompt for %s did not run (%s)", line.id, type(exc).__name__
            )
            return False
        log_path = self.log_path(commit_run_id)
        capacity = self.capacity_for(provider)
        gate = _provider_gate(provider)
        # The spawn fence runs outside the guard below: a drive another writer
        # took over must abandon exactly as it does for the run itself, not be
        # answered with a stop the machine would then park the line for.
        if before_spawn is not None:
            await before_spawn()
        if not gate.try_acquire(capacity):
            await gate.acquire(capacity)
        try:
            # (Re-)check the fence with the slot held, as ``_execute_worker``
            # does: a drain set or an epoch moved while this turn waited.
            if before_spawn is not None:
                await before_spawn()
            try:
                if self._runner is _run_command:
                    started = _run_command(
                        argv,
                        worktree,
                        log_path,
                        registry=self._processes,
                        run_id=commit_run_id,
                        env=self._launch_env(model, line.id),
                    )
                else:
                    started = self._runner(argv, worktree, log_path)
                code = await asyncio.wait_for(started, self._commit_prompt_timeout)
            except TimeoutError:
                logger.warning(
                    "Commit prompt for %s ran past %ss; its tree is kept as it was",
                    line.id,
                    self._commit_prompt_timeout,
                )
                code = None
            except Exception as exc:  # noqa: BLE001 -- a nudge that cannot run keeps the tree
                logger.warning(
                    "Commit prompt for %s did not run (%s)", line.id, type(exc).__name__
                )
                return False
        finally:
            gate.release()
        self._account_commit_turn(provider, log_path, turn)
        if code != 0:
            if code is not None:
                logger.info(
                    "Commit prompt for %s exited %s; its tree is kept as it was",
                    line.id,
                    code,
                )
            return False
        try:
            code, dirty, _ = await self._line_git(
                self._cwd.resolve(),
                worktree,
                ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
            )
        except Exception as exc:  # noqa: BLE001 -- a nudge that cannot run keeps the tree
            logger.warning(
                "Commit prompt for %s could not be verified (%s)", line.id, type(exc).__name__
            )
            return False
        if code != 0:
            return False
        return not dirty.strip()

    @staticmethod
    def _account_commit_turn(provider: str, log_path: Path, turn: _CommitTurn) -> None:
        """Add the commit prompt's session and tokens to its run's (N49)."""
        try:
            output = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        session_id, usage = _session_metadata(provider, output, turn.session_id)
        turn.session_id = session_id
        turn.usage = _add_usage(turn.usage, usage)

    async def _branch_head(self, line: Line, worktree: Path) -> str | None:
        """The line branch HEAD after a failed run, or None when unknown."""
        if not line.branch:
            return None
        try:
            if not self._line_ref_is_plain(self._cwd.resolve(), line.branch):
                return None
            code, output, _ = await self._line_git(
                self._cwd.resolve(), worktree, ["rev-parse", "HEAD"]
            )
        except (GitDeliveryError, LaunchError, OSError, TimeoutError, subprocess.TimeoutExpired):
            return None
        head = output.strip()
        return head if code == 0 and _is_object_id(head) else None

    async def _ahead_of_base(
        self, repo: Path, worktree: Path, line: Line, head: str
    ) -> tuple[bool, str | None]:
        """True when HEAD descends from ``base_commit`` with commits beyond it.

        ``base..HEAD`` must count at least one commit, and ``HEAD..base`` none:
        a reset or diverged branch is not "ahead". No base, or one that does
        not resolve to an object id, counts as no commits. The resolved base
        id comes back too, as the anchor of the machine's run chain.
        """
        base = (line.base_commit or "").strip()
        if not base or base.startswith("-") or not _is_object_id(head):
            return False, None
        code, resolved, _ = await self._line_git(repo, worktree, ["rev-parse", "--verify", base])
        base_id = resolved.strip()
        if code != 0 or not _is_object_id(base_id):
            return False, None
        ahead = await self._count_commits(repo, worktree, f"{base_id}..{head}")
        behind = await self._count_commits(repo, worktree, f"{head}..{base_id}")
        return (ahead is not None and ahead > 0 and behind == 0), base_id

    async def _count_commits(self, repo: Path, worktree: Path, span: str) -> int | None:
        code, output, _ = await self._line_git(repo, worktree, ["rev-list", "--count", span])
        text = output.strip()
        if code != 0 or not text.isdigit():
            return None
        return int(text)

    async def _line_git(self, repo: Path, cwd: Path, argv: list[str]) -> tuple[int, str, str]:
        """Use isolated Git for reads/mutations; preserve only injected test runners."""
        if self._git_runner is _git_command:
            if _task6_readonly_git_argv(argv):
                return await _isolated_git_readonly_command(argv, cwd)
            if _task6_local_mutation_git_argv(argv):
                return await _isolated_git_mutation_command(
                    repo, cwd, self._worktrees_dir, argv
                )
            else:
                raise LaunchError("refusing an unclassified Task 6 Git command")
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        git_dir = self._worktree_gitdir(repo, cwd)
        command = [
            "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=/dev/null",
            "--git-dir", str(git_dir),
            "--work-tree", str(cwd),
            *(
                [argv[0], "--ignore-submodules=all", *argv[1:]]
                if argv and argv[0] == "status"
                else argv
            ),
        ]
        filter_keys = self._configured_filter_keys(repo, git_dir, cwd)
        if filter_keys is None:
            raise LaunchError("invalid Git filter name in worker repository config")
        for index, key in enumerate(filter_keys):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = ""
        if filter_keys:
            env["GIT_CONFIG_COUNT"] = str(len(filter_keys))
        if self._git_runner is _git_command:
            return await self._git_runner(command, repo, env=env)
        return await self._git_runner(command, repo)

    async def _registered_line_worktree(self, repo: Path, path: Path, branch: str) -> bool:
        code, output, _ = await self._line_git(repo, repo, ["worktree", "list", "--porcelain"])
        if code != 0:
            return False
        expected_path = str(path.resolve())
        expected_branch = f"refs/heads/{branch}"
        blocks = output.strip().split("\n\n")
        return any(
            f"worktree {expected_path}" in block.splitlines()
            and f"branch {expected_branch}" in block.splitlines()
            for block in blocks
        )

    @staticmethod
    def _repository_gitdirs(repo: Path) -> tuple[Path, Path]:
        entry = repo / ".git"
        if entry.is_symlink():
            raise LaunchError("repository .git entry must not be a symlink")
        if entry.is_dir():
            common = entry.resolve()
            return common, common
        if not entry.is_file():
            raise LaunchError("repository .git entry is missing or invalid")
        try:
            text = entry.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LaunchError("could not inspect repository .git entry") from exc
        if not text.startswith("gitdir: "):
            raise LaunchError("repository .git entry is invalid")
        admin = (repo / text[len("gitdir: ") :]).resolve()
        commondir = admin / "commondir"
        try:
            common_text = commondir.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LaunchError("repository worktree has no common Git directory") from exc
        common = (admin / common_text).resolve()
        if admin.parent != common / "worktrees":
            raise LaunchError("repository .git entry points outside its Git worktree registry")
        return common, admin

    @classmethod
    def _line_ref_is_plain(cls, repo: Path, branch: str) -> bool:
        """False when the line's loose branch ref is a symlink or a symref.

        A Worker can rewrite ``refs/heads/<branch>`` so that HEAD resolves
        through another ref; G2 and merge must never act on such a head.
        Every path component from ``<common>/refs/heads`` down to the ref is
        checked in pure Python (the real Git runner only allows a fixed
        read-only allowlist). A missing loose file is a packed ref: fine.
        """
        common, _ = cls._repository_gitdirs(repo)
        heads = common / "refs" / "heads"
        parts = Path(branch).parts
        if not branch or Path(branch).is_absolute() or ".." in parts:
            return False
        current = heads
        if current.is_symlink():
            return False
        for part in parts:
            current = current / part
            if current.is_symlink():
                return False
        try:
            text = current.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, NotADirectoryError):
            return True
        except OSError:
            return False
        return not text.lstrip().startswith("ref:")

    @classmethod
    def _worktree_gitdir(cls, repo: Path, worktree: Path) -> Path:
        common, repo_admin = cls._repository_gitdirs(repo)
        if worktree.resolve() == repo.resolve():
            return repo_admin
        cls._validate_worktree_gitfile(repo, worktree)
        entry = worktree / ".git"
        text = entry.read_text(encoding="utf-8").strip()
        admin = (worktree / text[len("gitdir: ") :]).resolve()
        if admin.parent != common / "worktrees":
            raise LaunchError("line worktree Git directory is outside its repository")
        return admin

    @classmethod
    def _configured_filter_keys(
        cls, repo: Path, admin: Path, worktree: Path
    ) -> list[str] | None:
        common, _ = cls._repository_gitdirs(repo)
        keys: set[str] = set()
        for config in (common / "config", admin / "config.worktree"):
            if not config.is_file():
                continue
            try:
                result = subprocess.run(
                    [
                        "git",
                        "config",
                        "--includes",
                        "--file",
                        str(config),
                        "--show-origin",
                        "--name-only",
                        "--get-regexp",
                        r"^(filter\..*\.(clean|smudge|process)|include.*)$",
                    ],
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                    env={
                        **{key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_GLOBAL": "/dev/null",
                    },
                )
            except (OSError, subprocess.SubprocessError):
                return None
            if result.returncode not in (0, 1) or result.stderr.strip():
                return None
            for record in result.stdout.splitlines():
                parts = record.rsplit(None, 1)
                if len(parts) != 2:
                    return None
                _origin, key = parts
                if key.startswith("include"):
                    # Includes can point at worker-writable files whose values
                    # race this scan; fail closed instead of trusting them.
                    return None
                if not key.startswith("filter."):
                    return None
                suffix = key.rsplit(".", 1)[-1]
                name = key[len("filter.") : -(len(suffix) + 1)]
                if not name or "=" in name or any(ord(char) < 32 or ord(char) == 127 for char in name):
                    return None
                for filter_field in ("clean", "smudge", "process"):
                    keys.add(f"filter.{name}.{filter_field}")
        return sorted(keys)

    @staticmethod
    def _validate_worktree_gitfile(repo: Path, worktree: Path) -> None:
        common, _ = ScriptLauncher._repository_gitdirs(repo)
        gitfile = worktree / ".git"
        try:
            if gitfile.is_symlink() or not gitfile.is_file():
                raise LaunchError("line worktree .git entry is not a regular file")
            line = gitfile.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise LaunchError("could not inspect line worktree .git entry") from exc
        if not line.startswith("gitdir: "):
            raise LaunchError("line worktree .git entry is invalid")
        target = (worktree / line[len("gitdir: ") :]).resolve()
        expected_root = common / "worktrees"
        reverse = target / "gitdir"
        commondir = target / "commondir"
        try:
            registered_gitfile = Path(reverse.read_text(encoding="utf-8").strip()).resolve()
            if commondir.is_symlink() or not commondir.is_file():
                raise LaunchError("line worktree common directory link is invalid")
            registered_common = (target / commondir.read_text(encoding="utf-8").strip()).resolve()
        except OSError as exc:
            raise LaunchError("line worktree Git registration is missing") from exc
        if (
            target.parent != expected_root
            or not target.is_dir()
            or registered_gitfile != gitfile.resolve()
            or registered_common != common.resolve()
        ):
            raise LaunchError("line worktree .git entry points outside its repository registration")

    async def request_self_report(
        self,
        line: Line,
        model: str,
        *,
        session_id: str,
        timeout: float = WORKER_REPORT_TIMEOUT_SECONDS,
    ) -> SelfReportReply | None:
        """Ask the old session for its self-report in one bounded turn.

        Only a provider in ``SELF_REPORT_PROVIDERS`` is asked. Any other
        provider returns ``None`` before a temporary directory or process is
        created. ``None`` means the caller writes the deterministic note and
        marks the self-report unsupported. The turn resumes ``session_id``
        with the fixed report prompt. Its files live in a private directory
        under this launcher's log directory and are removed when the turn
        ends, including on timeout. Cancellation is not turned into a reply.
        The answered text is only the provider parser's text.
        """
        provider = model.strip().lower()
        if not supports_self_report(provider):
            return None
        # minimax resumes and already has a parser. No resume means this
        # provider cannot be asked.
        if not supports_session_resume(provider):
            return SelfReportReply("unsupported")
        if not self._script.exists():
            return SelfReportReply("failed")
        argv = [
            str(self._script),
            model,
            WORKER_REPORT_PROMPT,
            f"--session-id={session_id}",
            "--session-mode=resume",
        ]
        _require_startable(argv, line)
        run_id = f"{line.current_run or line.id}--self-report"
        self._runs_dir.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix="self-report-", dir=self._runs_dir))
        try:
            os.chmod(tmp, 0o700)
            log_path = tmp / "outer.log"
            env = {**os.environ, "CONDUCTOR_DISPATCH_LAUNCH_LOG_DIR": str(tmp)}
            if self._runner is _run_command:
                code = await asyncio.wait_for(
                    _run_command(
                        argv,
                        self._cwd,
                        log_path,
                        registry=self._processes,
                        run_id=run_id,
                        env=env,
                    ),
                    timeout,
                )
            else:
                code = await asyncio.wait_for(
                    self._runner(argv, self._cwd, log_path), timeout
                )
            if code != 0:
                return SelfReportReply("failed")
            try:
                with log_path.open("rb") as stream:
                    payload = stream.read(_SELF_REPORT_MAX_BYTES + 1)
            except OSError:
                return SelfReportReply("failed")
            if len(payload) > _SELF_REPORT_MAX_BYTES:
                return SelfReportReply("oversize")
            output = payload.decode("utf-8", errors="replace")
            try:
                text = _final_text(provider, output)
            except Exception:
                return SelfReportReply("invalid")
            return SelfReportReply("answered", text)
        except TimeoutError:
            return SelfReportReply("timeout")
        finally:
            # Stopping the child uncancels this task so the grace period can
            # finish. Put the cancellation back afterwards, or the caller
            # sees a normal return.
            cancelled = False
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                cancelled = True
            try:
                if run_id in self._processes:
                    await asyncio.shield(self.cancel(run_id))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if cancelled and current is not None:
                current.cancel()

    async def cancel(self, run_id: str) -> None:
        """Stop one live launch and the group it leads.

        The worker stays in that group. TERM, a bounded wait, then KILL of the
        group and of any descendant that left it. A run that already finished
        is not in the table.
        """
        process = self._processes.get(run_id)
        if process is None:
            return
        await _stop_launched(process)
        self._processes.pop(run_id, None)

    async def uncommitted_diff(self, line: Line, *, max_bytes: int = 16_000) -> str | None:
        """Bounded, read-only view of the uncommitted work in a line's worktree.

        Only the registered worktree that ``_prepare_line_worktree`` would use
        for ``line.branch`` is inspected; nothing is created or pruned. The
        text is the porcelain status, the tracked diff against ``HEAD`` and the
        names (never contents) of untracked files. ``None`` means the tree is
        clean, missing, not registered to the branch, or could not be read.
        """
        if not line.branch or not self._dispatch_id:
            return None
        try:
            repo = self._cwd.resolve()
            root = self._worktrees_dir.resolve()
            repo_key = hashlib.sha256(str(repo).encode()).hexdigest()[:24]
            dispatch_key = hashlib.sha256(self._dispatch_id.encode()).hexdigest()[:24]
            worktree = self._line_worktree_path(root, repo_key, dispatch_key, line)
            current = root
            for part in worktree.relative_to(root).parts:
                current = current / part
                if current.is_symlink():
                    return None
            if not worktree.is_dir():
                return None
            self._validate_worktree_gitfile(repo, worktree)
            if not await self._registered_line_worktree(repo, worktree, line.branch):
                return None
            code, branch, _ = await self._line_git(repo, worktree, ["branch", "--show-current"])
            if code != 0 or branch.strip() != line.branch:
                return None
            code, status, _ = await self._line_git(
                repo,
                worktree,
                ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
            )
            if code != 0:
                return None
            if not status.strip():
                return None
            # Git output cannot be capped, so the diff is read only when the
            # changed files on disk are small enough; otherwise names only.
            code, listing, _ = await self._line_git(
                repo, worktree, ["ls-tree", "-r", "-l", "-z", "HEAD"]
            )
            if code != 0:
                return None
            readable, why = _diff_read_budget(worktree, status, _head_sizes(listing))
            if readable:
                # The isolated read-only Git allowlist admits this exact diff
                # form (no external diff driver, no textconv); a --stat/--patch
                # variant would be refused, and the status lists the paths.
                code, diff, _ = await self._line_git(
                    repo, worktree, ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"]
                )
                if code != 0:
                    return None
                diff = _drop_binary_hunks(diff)
            else:
                diff = f"(diff not read: {why}; the changed paths are listed above)"
            code, others, _ = await self._line_git(
                repo, worktree, ["ls-files", "--others", "--exclude-standard", "-z"]
            )
            if code != 0:
                return None
        except (
            LaunchError,
            GitDeliveryError,
            NativeCliUnavailable,
            OSError,
            ValueError,
            TimeoutError,
            subprocess.SubprocessError,
        ):
            return None
        untracked = [
            "".join(ch if ch.isprintable() else "?" for ch in name)
            for name in others.split("\0")
            if name
        ]
        sections = [
            "## git status --porcelain",
            status.rstrip("\n"),
            "## git diff HEAD",
            diff.rstrip("\n"),
            "## untracked files",
            *(untracked or ["(none)"]),
        ]
        # The text usually goes to another provider: redact the whole of it
        # first, then cut, so a cut never splits a credential past redaction.
        text = str(redact_credentials("\n".join(sections) + "\n"))
        return _truncate_utf8(text, max_bytes)


_LIVE_LAUNCHERS: weakref.WeakSet[ScriptLauncher] = weakref.WeakSet()


# -- per-provider launch capacity ---------------------------------------------

PROVIDER_CAPACITY_ENV = "CONDUCTOR_DISPATCH_PROVIDER_CAPACITY"
DEFAULT_PROVIDER_CAPACITY = 1


def _normalise_capacity(capacity: Mapping[str, int]) -> dict[str, int]:
    result: dict[str, int] = {}
    for name, value in capacity.items():
        provider = str(name).strip().lower()
        if not provider:
            raise ValueError("provider capacity needs a provider name")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"provider capacity for {provider} must be an int >= 1")
        result[provider] = value
    return result


def provider_capacity_from_env(env: Mapping[str, str] | None = None) -> dict[str, int]:
    """Parse ``CONDUCTOR_DISPATCH_PROVIDER_CAPACITY`` (``"grok=1,kimi=2"``).

    Names are stripped and lowercased; values must be ints >= 1. Invalid
    entries are skipped with a warning. Providers not listed fall back to
    ``DEFAULT_PROVIDER_CAPACITY``.
    """
    source = os.environ if env is None else env
    raw = source.get(PROVIDER_CAPACITY_ENV, "") or ""
    result: dict[str, int] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, value = entry.partition("=")
        provider = name.strip().lower()
        try:
            count = int(value.strip()) if sep else 0
        except ValueError:
            count = 0
        if not provider or count < 1:
            logger.warning("ignoring invalid %s entry %r", PROVIDER_CAPACITY_ENV, entry)
            continue
        result[provider] = count
    return result


class _ProviderGate:
    """FIFO counting gate whose limit is supplied by each acquirer.

    One gate per (event loop, provider): asyncio futures belong to a loop, so
    the gate never outlives or crosses the loop it was created on.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.held = 0
        self._waiters: list[tuple[asyncio.Future[None], int]] = []

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    def try_acquire(self, capacity: int) -> bool:
        if self._waiters or self.held >= capacity:
            return False
        self.held += 1
        return True

    async def acquire(self, capacity: int) -> None:
        if self.try_acquire(capacity):
            return
        future: asyncio.Future[None] = self._loop.create_future()
        entry = (future, capacity)
        self._waiters.append(entry)
        try:
            await future
        except BaseException:
            if future.done() and not future.cancelled():
                # Granted, then cancelled before it resumed: give it back.
                self.held -= 1
            elif entry in self._waiters:
                self._waiters.remove(entry)
            self._wake()
            raise

    def release(self) -> None:
        if self.held > 0:
            self.held -= 1
        self._wake()

    def _wake(self) -> None:
        while self._waiters:
            future, capacity = self._waiters[0]
            if future.done():
                self._waiters.pop(0)
                continue
            if self.held >= capacity:
                return
            self._waiters.pop(0)
            self.held += 1
            future.set_result(None)


_PROVIDER_GATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, _ProviderGate]
] = weakref.WeakKeyDictionary()


def _provider_gate(provider: str) -> _ProviderGate:
    loop = asyncio.get_running_loop()
    gates = _PROVIDER_GATES.setdefault(loop, {})
    gate = gates.get(provider)
    if gate is None:
        gate = gates[provider] = _ProviderGate(loop)
    return gate


def _configured_capacity(provider: str) -> int:
    configured = [
        launcher._provider_capacity[provider]
        for launcher in list(_LIVE_LAUNCHERS)
        if provider in launcher._provider_capacity
    ]
    if configured:
        return min(configured)
    return provider_capacity_from_env().get(provider, DEFAULT_PROVIDER_CAPACITY)


def provider_slots_free(provider: str, *, capacity: int | None = None) -> bool:
    """True when a launch of ``provider`` would start without waiting.

    ``capacity`` defaults to the smallest capacity any live launcher has
    configured for the provider, else the environment, else 1.
    """
    name = provider.strip().lower()
    limit = _configured_capacity(name) if capacity is None else capacity
    held = 0
    waiting = 0
    for gates in list(_PROVIDER_GATES.values()):
        gate = gates.get(name)
        if gate is not None:
            held += gate.held
            waiting += gate.waiting
    return waiting == 0 and held < limit


def live_run_ids() -> set[str]:
    """Run ids of every launch in this process whose child has not exited."""
    ids: set[str] = set()
    for launcher in list(_LIVE_LAUNCHERS):
        for run_id, process in list(launcher._processes.items()):
            if process.returncode is None:
                ids.add(run_id)
    return ids


def live_runs_for_line(dispatch_id: str, line_id: str) -> set[str]:
    """Live run ids minted for one dispatch line (``dispatch:line:attempt:hex``)."""
    prefix = f"{dispatch_id}:{line_id}:"
    return {run_id for run_id in live_run_ids() if run_id.startswith(prefix)}


async def cancel_live_run(run_id: str) -> bool:
    """Stop ``run_id`` through its launcher's TERM/KILL group path.

    True when a launcher in this process held the run and it was stopped;
    False when no launcher holds it.
    """
    for launcher in list(_LIVE_LAUNCHERS):
        if run_id in launcher._processes:
            await launcher.cancel(run_id)
            return True
    return False


class LogMonitor:
    """Watches a run's log file for growth, completion and silence.

    Stall is no new bytes for ``stall_seconds``; timeout is more than
    ``timeout_seconds`` since the run was first seen. Both cost the line its
    attempt, so the caller reassigns it rather than waiting longer.
    """

    def __init__(
        self,
        runs_dir: Path,
        *,
        stall_seconds: float = 60.0,
        timeout_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
        canceller: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._runs_dir = Path(runs_dir)
        self._stall_seconds = stall_seconds
        self._timeout_seconds = timeout_seconds
        self._clock = clock
        self._canceller = canceller
        self._first_seen: dict[str, float] = {}
        self._last_growth: dict[str, float] = {}
        self._last_size: dict[str, int] = {}
        # Runs whose log is known to carry model output (P0 silent worker).
        self._spoke: set[str] = set()

    def _silent(
        self, run_id: str, log_path: Path, size: int, *, complete: bool,
        exit_code: int | None, elapsed: float,
    ) -> Any:
        if run_id in self._spoke:
            return None
        finding, spoke = p0_hooks.monitor_silent(
            log_path, size, complete=complete, exit_code=exit_code, elapsed=elapsed
        )
        if spoke:
            self._spoke.add(run_id)
        return finding

    async def poll(self, run_id: str, model: str | None = None) -> RunStatus:
        now = self._clock()
        log_path = self._runs_dir / f"{run_id}.log"
        size = log_path.stat().st_size if log_path.exists() else 0
        first = self._first_seen.setdefault(run_id, now)
        if run_id not in self._last_growth or size != self._last_size.get(run_id):
            self._last_growth[run_id] = now
            self._last_size[run_id] = size
        exit_path = self._runs_dir / f"{run_id}.exit"
        if exit_path.exists():
            try:
                exit_code = int(exit_path.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                exit_code = None
            if exit_code in (None, 0):
                return WorkerRunStatus(
                    output=_tail(log_path),
                    complete=True,
                    tokens=size // 4,
                    exit_code=exit_code,
                    report=_tail(log_path, REPORT_TAIL_CHARS),
                    silent=self._silent(
                        run_id, log_path, size, complete=True,
                        exit_code=exit_code, elapsed=now - first,
                    ),
                )
            try:
                full_output = log_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                full_output = ""
            classification = classify_failure(model or "", full_output, exit_code=exit_code)
            return WorkerRunStatus(
                output=_tail(log_path),
                complete=True,
                tokens=size // 4,
                exit_code=exit_code,
                failure_class=classification.failure_class.value,
                reason_code=classification.reason_code,
                retry_after_seconds=classification.retry_after_seconds,
            )
        timed_out = now - first > self._timeout_seconds
        stalled = not timed_out and now - self._last_growth[run_id] > self._stall_seconds
        silent = self._silent(
            run_id, log_path, size, complete=False, exit_code=None, elapsed=now - first
        )
        if silent is not None:
            return WorkerRunStatus(
                output=_tail(log_path), timed_out=timed_out, stalled=stalled,
                tokens=size // 4, silent=silent,
            )
        if timed_out:
            return RunStatus(output=_tail(log_path), timed_out=True, tokens=size // 4)
        if stalled:
            return RunStatus(output=_tail(log_path), stalled=True, tokens=size // 4)
        return RunStatus(output=_tail(log_path), tokens=size // 4)

    async def cancel(self, run_id: str) -> None:
        self._spoke.discard(run_id)
        self._first_seen.pop(run_id, None)
        self._last_growth.pop(run_id, None)
        self._last_size.pop(run_id, None)
        if self._canceller is not None:
            await self._canceller(run_id)


class ReviewBackend(Protocol):
    """Asks one model to review one line's evidence.

    A structured-output adapter may return the already-decoded object; the
    text path returns the reply as written. ``CrossReviewer`` validates a
    mapping first and falls back to parsing a string.
    """

    async def review(self, model: str, prompt: str) -> str | Mapping[str, object]: ...


#: Reasons that mean a line is parked waiting for a review to happen.
REVIEW_WAIT_REASONS = frozenset(
    {
        "review_unavailable",
        "review_unparseable",
        "reviewers_quota_exhausted",
        "no_eligible_reviewer",
        "no_secondary_reviewer",
    }
)


def _review_stage(snapshot: object) -> str:
    if not isinstance(snapshot, Mapping):
        return ""
    machine = snapshot.get("machine")
    if isinstance(machine, Mapping):
        stage = machine.get("stage")
        if isinstance(stage, str):
            return stage.strip().lower()
    phase = snapshot.get("phase")
    return phase.strip().lower() if isinstance(phase, str) else ""


def review_backlog_count(dispatch: Dispatch, gates: object | None = None) -> int:
    """Lines already waiting for review plus lines already in review.

    This is the feasibility count N76 admit-on-launch uses. A line that is
    running implementation work is not waiting for review yet; a line parked
    on one of the review reasons is, even though it is BLOCKED.
    """
    waiting = 0
    for line in dispatch.lines:
        if line.status is LineStatus.REVIEWING:
            waiting += 1
            continue
        if line.status is LineStatus.BLOCKED:
            if line.failure_reason in REVIEW_WAIT_REASONS:
                waiting += 1
            continue
        if line.status is not LineStatus.RUNNING:
            continue
        snapshot = (
            gates.line_snapshot(dispatch, line)
            if gates is not None and hasattr(gates, "line_snapshot")
            else None
        )
        if _review_stage(snapshot) in {"primary_review", "secondary_review"}:
            waiting += 1
        elif isinstance(snapshot, Mapping) and isinstance(
            snapshot.get("review_retry"), Mapping
        ):
            waiting += 1
    return waiting


def decide_launch_admission(
    dispatch: Dispatch,
    *,
    primary_concurrency: int,
    gates: object | None = None,
    env: Mapping[str, str] | None = None,
) -> ReviewAdmission:
    """N76's launch admission: start a new line, or queue it behind review.

    ``primary_concurrency`` is how many primary reviews can run at once; the
    backlog cap is twice that unless ``CONDUCTOR_DISPATCH_REVIEW_BACKLOG_MAX``
    tightens it.
    """
    source = os.environ if env is None else env
    return decide_review_admission(
        review_backlog_count(dispatch, gates),
        max(1, int(primary_concurrency)),
        source,
    )


_REVIEW_SYSTEM = (
    "You review one work line of a parallel dispatch. Reply with JSON only: "
    '{"passed": true|false, "blockers": ["what must change"], "verdict": "one line"}. '
    "Block on correctness, failing tests or scope the line did not cover; do not "
    "block on style. Start a blocker with 'scope:' only when the line changed "
    "something outside its task, and with 'security:' only for a security "
    "problem. Review only this evidence and the repository: do not search or "
    "read Fleet Memory, memfw, notes or agent memory files, and do not write "
    "memory records, whatever global or AGENTS.md guidance says." + " " + REVIEW_RUBRIC
)


class CrossReviewer:
    """Reviews each line with a model that did not write it.

    The reviewer model is chosen by the selector with the author excluded, so
    the only thing this class owns is the evidence and the verdict shape. The
    evidence may be computed (``branch_evidence`` reads git), so it can be a
    coroutine function as well as a plain one.
    """

    def __init__(
        self,
        backend: ReviewBackend,
        evidence: Callable[[str], str | Awaitable[str]],
        *,
        max_prompt_bytes: int = REVIEW_PROMPT_BUDGET_BYTES,
    ) -> None:
        self._backend = backend
        self._evidence = evidence
        self._max_prompt_bytes = max_prompt_bytes
        # P0 strict review: the attempts the machine copies onto the snapshot.
        self._strict_attempts: dict[str, list[dict[str, object]]] = {}

    def pop_strict_attempts(self, line_id: str) -> list[dict[str, object]]:
        """The strict-review attempts noted for ``line_id`` since the last pop."""
        return self._strict_attempts.pop(line_id, [])

    def _note_strict(self, line_id: str, mode: str, **attempt: object) -> None:
        if mode == "off":
            return
        if line_id not in self._strict_attempts and len(self._strict_attempts) >= 256:
            self._strict_attempts.pop(next(iter(self._strict_attempts)))
        self._strict_attempts.setdefault(line_id, []).append(attempt)
        del self._strict_attempts[line_id][:-8]

    async def review_line(self, line_id: str, model: str, *, notes: str = "") -> Verdict:
        material = self._evidence(line_id)
        if inspect.isawaitable(material):
            material = await material
        # The reply contract has to be in the prompt itself: the review runs as
        # one native-CLI turn with no separate system prompt, and a reviewer
        # left to its own format writes prose that carries no verdict.
        prompt = (
            f"{_REVIEW_SYSTEM}\n\n"
            f"Review line {line_id}, produced by a model other than {model}.\n\n"
            f"{material}\n\n{_REVIEW_REPLY_REMINDER}"
        )
        if notes:
            # Machine notes for the reviewer (unconfirmed fix rulings, R5 N6).
            # They are advisory: past the budget they are dropped, never fatal.
            noted = (
                f"{_REVIEW_SYSTEM}\n\n"
                f"Review line {line_id}, produced by a model other than {model}.\n\n"
                f"{material}\n\n{notes}\n\n{_REVIEW_REPLY_REMINDER}"
            )
            if _size(noted) <= self._max_prompt_bytes:
                prompt = noted
            else:
                logger.info("Review notes for %s dropped: over the prompt budget", line_id)
        # The one place the complete prompt exists, so the one place that can say it will not
        # start: ``branch_evidence`` keeps its own evidence inside the budget, and this holds
        # any other evidence, and the header, to it. A reviewer that cannot be started would
        # fail the same way, later and less clearly.
        size = _size(prompt)
        if size > self._max_prompt_bytes:
            raise DispatchError(
                f"The review prompt for line {line_id} is {size} bytes, over the "
                f"{self._max_prompt_bytes}-byte budget"
            )
        raw = await self._backend.review(model, prompt)
        strict = review_strict.strict_mode()
        kind = review_strict.classify_reply(raw) if strict != "off" else ""
        verdict = _structured_verdict(raw)
        if verdict is None:
            verdict = _parse_verdict(str(raw))
        if verdict.passed or not verdict_unparseable(verdict):
            self._note_strict(
                line_id, strict, reviewer=model, kind=kind, retried=False, outcome="valid"
            )
            return verdict
        # One retry with a correction: an unusable reply is the reviewer's
        # format, not the author's work, so it must not become a fix round.
        # Enforce sends the schema-led note; off and shadow send the old one.
        note = (
            "\n\n" + review_strict.strict_retry_note(kind)
            if strict == "enforce"
            else _REVIEW_RETRY_NOTE
        )
        retry = prompt + note
        if _size(retry) > self._max_prompt_bytes:
            self._note_strict(
                line_id, strict, reviewer=model, kind=kind, retried=False,
                outcome="unparseable",
            )
            return verdict
        raw = await self._backend.review(model, retry)
        verdict = _structured_verdict(raw)
        if verdict is None:
            verdict = _parse_verdict(str(raw))
        if strict != "off":
            again = review_strict.classify_reply(raw)
            self._note_strict(
                line_id, strict, reviewer=model, kind=again if again != "valid" else kind,
                retried=True, outcome="valid" if again == "valid" else "unparseable",
            )
        return verdict


_REVIEW_REPLY_REMINDER = (
    "Reply with only the JSON object described at the top: "
    '{"passed": true|false, "blockers": [...], "verdict": "..."}. '
    "Put every finding in blockers. The object must be the last thing in the "
    "reply: write nothing after it but the closing fence of a code block that "
    "holds only the object."
)
_REVIEW_RETRY_NOTE = (
    "\n\nYour previous reply had no usable verdict object. Reply again with "
    "only the JSON object with a boolean passed field."
)
# A reply whose verdict object is followed by more content. The reviewer has
# not stopped writing, so nothing after that object is the verdict.
_VERDICT_NOT_LAST = "Reviewer verdict object is not the last content of the reply"
_UNPARSEABLE_PREFIXES = (
    "Reviewer returned no verdict JSON",
    "Reviewer returned invalid JSON",
    "Reviewer verdict must be an object",
    "Reviewer verdict has no boolean 'passed' field",
    _VERDICT_NOT_LAST,
)


def _review_contract_bytes() -> int:
    """Bytes CrossReviewer adds around the evidence, with room for ids and model."""
    wrapper = (
        f"{_REVIEW_SYSTEM}\n\nReview line , produced by a model other than .\n\n"
        f"\n\n{_REVIEW_REPLY_REMINDER}{_REVIEW_RETRY_NOTE}"
    )
    return len(wrapper.encode("utf-8")) + 256


def verdict_unparseable(verdict: Verdict) -> bool:
    """True when a reply carried no usable verdict object.

    ``CrossReviewer`` uses this to decide on its one same-model correction,
    and the machine uses it to keep a reviewer's format failure from
    becoming the author's fix round.
    """
    return bool(verdict.blockers) and all(
        str(item).startswith(_UNPARSEABLE_PREFIXES) for item in verdict.blockers
    )


def _verdict_object(text: str) -> tuple[object, str | None]:
    """The verdict object at the end of a reply, or (None, error message).

    Review prose can quote code with braces, so every open brace outside an
    already decoded object is tried with raw_decode. Only top-level objects
    with a boolean ``passed`` count: a quoted fixture nested in prose or inside
    the verdict is never chosen over it. When top-level candidates disagree on
    ``passed`` the reply is unusable (an author could plant a passing object in
    the diff for the reviewer to quote), so it is reported as invalid and the
    review is retried. The verdict object must also be the last non-whitespace
    content of the reply: a reviewer that keeps writing after the object has
    not finished reviewing, and prose after a valid object is never a verdict.
    The one exception is a single closing code fence that pairs with the fence
    opened right before the object (```json ... ```).
    """
    import json

    decoder = json.JSONDecoder()
    candidates: list[tuple[dict, int, int]] = []
    first_error: str | None = None
    index = text.find("{")
    while index != -1:
        try:
            value, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError as exc:
            first_error = first_error or exc.msg
            index = text.find("{", index + 1)
            continue
        if isinstance(value, dict) and isinstance(value.get("passed"), bool):
            candidates.append((value, index, end))
        index = text.find("{", end)
    if not candidates:
        return None, first_error
    if len({item["passed"] for item, _start, _end in candidates}) > 1:
        return None, "conflicting verdict objects"
    payload, start, end = candidates[-1]
    tail = text[end:].strip()
    if tail and not (tail == "```" and _opens_fence(text[:start])):
        return None, _VERDICT_NOT_LAST
    return payload, None


# A code fence line: three backticks at the start of a line, then an optional
# language tag (```json) and nothing else.
_FENCE_LINE = re.compile(r"^[ \t]*```[\w+-]*[ \t]*$", re.MULTILINE)


def _opens_fence(before: str) -> bool:
    """True when ``before`` ends with a code fence that opens a block.

    A closing fence after the verdict object counts as part of the object only
    when it pairs with a fence opened right before it: the last non-blank line
    before the object is a fence line, and it is an odd one (fences before it
    open and close in pairs). Any other trailing fence is more content.
    """
    fences = list(_FENCE_LINE.finditer(before))
    if not fences or len(fences) % 2 == 0:
        return False
    return not before[fences[-1].end():].strip()


def _structured_payload(raw: str | Mapping[str, object]) -> Mapping[str, object] | None:
    """The reply as one JSON object, or None when it is not structured output.

    A structured-output CLI writes the object alone, possibly inside one code
    fence. Anything else is a text reply and goes to ``_parse_verdict``.
    """
    if isinstance(raw, Mapping):
        return raw
    text = str(raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3 and lines[0].strip().lower() in ("```", "```json"):
            text = "\n".join(lines[1:])
            if text.rstrip().endswith("```"):
                text = text.rstrip()[:-3]
            text = text.strip()
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _structured_verdict(raw: str | Mapping[str, object]) -> Verdict | None:
    """A schema-valid verdict object from a structured reply, or None.

    The object is validated against ``verdict.schema.json`` before it is
    trusted. A reply that merely resembles it is left for the text fallback,
    which owns the stricter "object is the last content" rule.
    """
    payload = _structured_payload(raw)
    if payload is None or validate_verdict(dict(payload)) is None:
        return None
    passed = payload.get("passed")
    blockers = payload.get("blockers")
    verdict = payload.get("verdict")
    if not isinstance(passed, bool) or not isinstance(blockers, list):
        return None
    return with_status(
        Verdict(
            passed=passed,
            blockers=[str(item) for item in blockers],
            verdict=str(verdict or ("pass" if passed else "blocked")),
        ),
        payload,
    )


def _parse_verdict(raw: str) -> Verdict:
    text = (raw or "").strip()
    if "{" not in text:
        return Verdict.blocked(["Reviewer returned no verdict JSON"])
    payload, error = _verdict_object(text)
    if payload is None:
        if error is None:
            return Verdict.blocked(["Reviewer returned no verdict JSON"])
        if error == _VERDICT_NOT_LAST:
            return Verdict.blocked([error])
        return Verdict.blocked([f"Reviewer returned invalid JSON: {error}"])
    if not isinstance(payload, dict):
        return Verdict.blocked(["Reviewer verdict must be an object"])
    passed = payload.get("passed")
    if not isinstance(passed, bool):
        return Verdict.blocked(["Reviewer verdict has no boolean 'passed' field"])
    blockers = [str(item) for item in payload.get("blockers") or []]
    verdict = str(payload.get("verdict") or ("pass" if passed else "blocked"))
    return with_status(Verdict(passed=passed, blockers=blockers, verdict=verdict), payload)


class GitMerger:
    """Merges line branches into the dispatch's integrate branch.

    Bound to one dispatch: it owns the integrate branch name and the mapping
    from line id to branch, so ``merge_line`` needs nothing but the id.
    """

    def __init__(
        self,
        repo_root: Path,
        dispatch: Dispatch,
        *,
        runner: GitCommand | None = None,
        gate: ApprovalGate | None = None,
        worktrees_dir: Path | None = None,
    ) -> None:
        self._repo_root = Path(repo_root)
        self._dispatch = dispatch
        self._runner = runner or _git_command
        self._gate = gate
        self._worktrees_dir = Path(worktrees_dir) if worktrees_dir else self._repo_root.parent / "dispatch-worktrees"
        root = self._repo_root.resolve()
        worktree_root = self._worktrees_dir.resolve()
        if worktree_root == root or root in worktree_root.parents:
            raise ValueError("dispatch worktree root must be outside repo_root")
        repo_key = hashlib.sha256(str(root).encode()).hexdigest()[:24]
        dispatch_key = hashlib.sha256(dispatch.id.encode()).hexdigest()[:24]
        self._integration_worktree = worktree_root / repo_key / dispatch_key / "integration"

    async def _git(self, argv: list[str], cwd: Path) -> tuple[int, str, str]:
        """Run local Git in isolation; ``push`` has its own isolated path."""
        if self._runner is not _git_command:
            return await self._runner(argv, cwd)
        if _task6_readonly_git_argv(argv):
            return await _isolated_git_readonly_command(argv, cwd)
        repo = self._repo_root.resolve()
        if _task6_local_mutation_git_argv(argv):
            return await _isolated_git_mutation_command(
                repo, cwd, self._worktrees_dir, argv
            )
        # A push never comes through here: ``push`` runs Git from an empty,
        # isolated repository so the shared checkout's config cannot reach it.
        raise MergeConflictError("refusing an unclassified integration Git command")

    async def _registered_integration(self, expected_branch: str | None = None) -> bool:
        repo = self._repo_root.resolve()
        try:
            ScriptLauncher._validate_worktree_gitfile(repo, self._integration_worktree)
        except LaunchError:
            return False
        code, output, _ = await self._git(["worktree", "list", "--porcelain"], repo)
        if code != 0:
            return False
        expected_path = str(self._integration_worktree.resolve())
        for block in output.strip().split("\n\n"):
            lines = block.splitlines()
            if f"worktree {expected_path}" not in lines:
                continue
            return expected_branch is None or f"branch refs/heads/{expected_branch}" in lines
        return False

    async def merge_line(self, line_id: str) -> str:
        line = self._dispatch.line(line_id)
        if line is None:
            raise MergeConflictError(f"Unknown line {line_id}")
        branch = line.branch
        if not branch:
            raise MergeConflictError(f"Line {line_id} has no branch to merge")
        await self._ensure_integrate_branch()
        code, _, err = await self._git(
            ["merge", "--no-ff", "--no-edit", branch], self._integration_worktree
        )
        if code != 0:
            paths = await self._conflicting_paths()
            await self._git(["merge", "--abort"], self._integration_worktree)
            raise MergeConflictError(
                f"Merging {branch} into {self._dispatch.integrate_branch} failed: {err.strip()}",
                paths,
            )
        return self._dispatch.integrate_branch

    async def cleanup_worktree(self) -> str | None:
        """Remove the integration worktree, preserving dirty edits and its branch."""
        if not self._integration_worktree.exists():
            return None
        try:
            if not await self._registered_integration():
                return "integration: terminal worktree is missing or unregistered"
            code, dirty, _ = await self._git(
                ["status", "--porcelain", "--untracked-files=all"], self._integration_worktree
            )
            if code != 0:
                return "integration: could not inspect terminal worktree"
            artifact = None
            force = bool(dirty.strip())
            if force:
                artifact = await _archive_dirty_worktree(
                    self._repo_root.resolve(),
                    self._integration_worktree,
                    self._worktrees_dir / "recovery-artifacts",
                    self._git,
                    {
                        "dispatch_id": self._dispatch.id,
                        "line_id": "integration",
                        "branch": self._dispatch.integrate_branch,
                    },
                )
            remove_args = ["worktree", "remove"]
            if force:
                remove_args.append("--force")
            remove_args.append(str(self._integration_worktree))
            code, _out, _err = await self._git(
                remove_args, self._repo_root
            )
            if code != 0:
                return "integration: git worktree remove failed"
            await self._git(["worktree", "prune"], self._repo_root)
            if artifact is not None:
                logger.warning("Archived dirty integration worktree at %s", artifact)
            return None
        except Exception as exc:
            logger.warning("Could not clean dispatch integration worktree")
            return f"integration: terminal cleanup failed ({type(exc).__name__})"

    async def push(
        self,
        branch: str,
        approval_token: str,
        *,
        remote: str = "origin",
        auth_config: Mapping[str, str] | None = None,
        secrets: tuple[str, ...] = (),
    ) -> str:
        """Push, but only against a token a human has already approved.

        The dispatch's own gate is the authority here: a token the gate still
        holds as pending or rejected is refused even if the caller has it.

        No branch is checked out anywhere for this (L2): the branch's commit is
        pushed by object id from an empty, isolated repository
        (``_isolated_push``), so neither the shared checkout nor a config a
        Worker wrote into it takes part. ``remote`` is ``origin`` (resolved to
        the URL in the repository's own config file, without ``insteadOf``
        rewriting) or a credential-free URL. ``auth_config`` is accepted only
        for an ``https://github.com/<owner>/<repo>`` destination. ``secrets``
        are redacted from Git's stderr before it reaches an exception.
        """
        if self._gate is None:
            raise GateNotApprovedError("No approval gate is configured for this dispatch")
        decision = await self._gate.decision(approval_token)
        if decision is not ApprovalDecision.APPROVED:
            raise GateNotApprovedError(
                f"Approval gate is {decision.value}; merge and push stay blocked"
            )
        approver = getattr(self._gate, "approver", None)
        if callable(approver):
            note = str(await approver(approval_token) or "").strip().lower()
            if note.startswith("auto:"):
                # Review fix 1: an automatic approver never pushes.
                raise GateNotApprovedError(
                    "Approved by an automatic approver; push needs a human approval"
                )
        if not safe_push_remote(remote) or not _safe_git_operand(branch):
            raise MergeConflictError("refusing to push to an unsafe remote")
        url = await self._origin_url() if remote == "origin" else remote
        if not url or not safe_push_remote(url) or url == "origin":
            raise MergeConflictError(f"Pushing {branch} failed: no usable push remote")
        auth = dict(auth_config or {})
        if auth and not github_push_url(url):
            raise MergeConflictError(
                "refusing to send a credential to a remote other than github.com over https"
            )
        repo = self._repo_root.resolve()
        code, sha = await self._branch_commit(branch)
        sha = sha.strip()
        if code != 0 or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", sha):
            raise MergeConflictError(f"Pushing {branch} failed: the branch does not exist")
        refspec = f"{sha}:refs/heads/{branch}"
        if self._runner is not _git_command:
            argv = ["push", url, refspec]
            assert _task6_approved_push_git_argv(argv)
            code, _, err = await self._runner(argv, repo)
        else:
            common, _admin = ScriptLauncher._repository_gitdirs(repo)
            code, _, err = await _isolated_push(
                common / "objects", url, refspec, auth_config=auth
            )
        if code != 0:
            text = err.strip()
            for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
                text = text.replace(secret, "[REDACTED]")
            text = redact_credentials(text)[:500]
            raise MergeConflictError(f"Pushing {branch} failed: {text}")
        return branch

    async def _branch_commit(self, branch: str) -> tuple[int, str]:
        """The object id ``refs/heads/<branch>`` names, read without a work tree.

        ``rev-parse`` of a full ref name runs no hook, filter, helper or
        fsmonitor, and system and global config are off, so it is safe even
        in a checkout whose own config a Worker rewrote.
        """
        argv = ["rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"]
        if self._runner is not _git_command:
            code, out, _ = await self._runner(argv, self._repo_root)
            return code, out
        try:
            common, _admin = ScriptLauncher._repository_gitdirs(self._repo_root.resolve())
        except LaunchError:
            return 1, ""
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        code, out, _ = await _git_command(
            ["-c", "core.fsmonitor=false", "--git-dir", str(common), *argv], common, env=env
        )
        return code, out

    async def _origin_url(self) -> str:
        """``remote.origin.url`` read from the repository's own config file only.

        ``--file`` reads that one file: no includes, no ``insteadOf`` or
        ``pushInsteadOf`` rewriting, no system or global config.
        """
        if self._runner is not _git_command:
            code, out, _ = await self._runner(["config", "--get", "remote.origin.url"], self._repo_root)
            return out.strip() if code == 0 else ""
        try:
            common, _admin = ScriptLauncher._repository_gitdirs(self._repo_root.resolve())
        except LaunchError:
            return ""
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
        code, out, _ = await _git_command(
            ["config", "--file", str(common / "config"), "--get", "remote.origin.url"],
            common,
            env=env,
        )
        return out.strip() if code == 0 else ""

    async def set_aside(self) -> str:
        """Move a rejected integrate branch out of the way instead of deleting it."""
        rejected = f"{self._dispatch.integrate_branch}-rejected"
        if not self._integration_worktree.exists() or not await self._registered_integration(
            self._dispatch.integrate_branch
        ):
            raise MergeConflictError("dispatch integration worktree is missing or unregistered")
        code, _, err = await self._git(
            ["branch", "-m", self._dispatch.integrate_branch, rejected], self._integration_worktree
        )
        if code != 0:
            logger.warning("Could not move %s aside: %s", self._dispatch.integrate_branch, err.strip())
            raise MergeConflictError(f"Could not move {self._dispatch.integrate_branch} aside")
        code, _, err = await self._git(
            ["worktree", "remove", str(self._integration_worktree)], self._repo_root
        )
        if code != 0:
            raise MergeConflictError(f"Could not release rejected integration worktree: {err.strip()}")
        return rejected

    async def _ensure_integrate_branch(self) -> None:
        target = self._dispatch.integrate_branch
        if self._integration_worktree.exists():
            if not await self._registered_integration():
                raise MergeConflictError("dispatch integration worktree is not registered to its branch")
            code, branch, _ = await self._git(["branch", "--show-current"], self._integration_worktree)
            if code == 0 and branch.strip() == target:
                return
            if code == 0 and branch.strip() == f"{target}-rejected":
                code, dirty, _ = await self._git(
                    ["status", "--porcelain", "--untracked-files=all"], self._integration_worktree
                )
                if code == 0 and not dirty.strip():
                    await self._git(["worktree", "remove", str(self._integration_worktree)], self._repo_root)
                else:
                    raise MergeConflictError("rejected integration worktree is dirty")
            else:
                raise MergeConflictError("dispatch integration worktree has an unexpected branch")
        _ensure_owned_directory(
            self._worktrees_dir.resolve(),
            self._integration_worktree.parent.relative_to(self._worktrees_dir.resolve()),
        )
        await self._git(["worktree", "prune"], self._repo_root)
        base_commit = ""
        if isinstance(self._dispatch.gates, dict):
            raw_base = self._dispatch.gates.get("base_commit")
            if isinstance(raw_base, str):
                base_commit = raw_base
        code, _, err = await self._git(
            ["worktree", "add", "-b", target, str(self._integration_worktree), base_commit or "HEAD"],
            self._repo_root,
        )
        if code != 0:
            code, _, err = await self._git(
                ["worktree", "add", str(self._integration_worktree), target], self._repo_root
            )
        if code != 0:
            raise MergeConflictError(f"Could not create integration worktree for {target}: {err.strip()}")

    async def _conflicting_paths(self) -> list[str]:
        code, out, _ = await self._git(
            ["diff", "--name-only", "--diff-filter=U"], self._integration_worktree
        )
        if code != 0:
            return []
        return [line for line in out.splitlines() if line.strip()]


def _tail(path: Path, limit: int = TAIL_CHARS) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _size(text: str) -> int:
    """The bytes ``text`` takes in a prompt. A lone surrogate counts as the "?" it is sent as."""
    return len(text.encode("utf-8", "replace"))


def _fit(text: str, limit: int, what: str, *, keep_end: bool = False) -> str:
    """``text`` in at most ``limit`` UTF-8 bytes, cut on a character boundary and saying so.

    A character count cannot bound what an argv string weighs, and a lone surrogate (JSON
    from the planner can carry one) cannot be encoded to start a process at all, so the
    text is measured and returned as the bytes that will be sent. ``keep_end`` keeps the
    last bytes, for a tail.
    """
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return raw.decode("utf-8")
    note = f"[{what} truncated: {len(raw)} bytes in all]"
    room = max(limit - len(note.encode("utf-8")) - 1, 0)
    if keep_end:
        return f"{note}\n{raw[len(raw) - room :].decode('utf-8', 'ignore')}"
    return f"{raw[:room].decode('utf-8', 'ignore')}\n{note}"


def _gated_evidence(dispatch: Dispatch, line: Line) -> str:
    """Text acceptance and G2 exit codes, only when a gate snapshot is present.

    An empty ``gates`` (dev, or live with the switch off) adds nothing, so the
    prompt those tests pin does not change. Command output stays in the log.
    """
    gates = dispatch.gates
    if not is_gates_snapshot(gates):
        return ""
    parts: list[str] = []
    prose = [
        item
        for item in line.acceptance
        if isinstance(item, str) and item and not item.startswith("cmd:")
    ]
    if prose:
        body = "\n".join(f"- {item}" for item in prose)
        parts.append(f"Acceptance:\n{_fit(body, _TASK_BYTES, 'acceptance')}")
    lines = gates.get("lines")
    snap = lines.get(line.id) if isinstance(lines, dict) else None
    gate_map = snap.get("gates") if isinstance(snap, dict) else None
    g2 = gate_map.get("G2") if isinstance(gate_map, dict) else None
    if isinstance(g2, dict) and g2.get("result") not in (None, "pending"):
        rows = [f"G2 {g2.get('result')} {g2.get('reason')}"]
        commands = g2.get("commands")
        if isinstance(commands, list):
            for command in commands:
                if isinstance(command, dict):
                    rows.append(
                        f"command {command.get('index')} exit {command.get('exit_code')}"
                    )
        parts.append(
            "G2 results (host-run on the line's commit; treat as the test evidence):\n"
            + "\n".join(rows)
        )
    handoff = gate_map.get("handoff") if isinstance(gate_map, dict) else None
    if isinstance(handoff, dict):
        files = handoff.get("files") or []
        shown = ", ".join(str(item) for item in files) if isinstance(files, list) else str(files)
        block = (
            "Handoff:\n"
            f"branch: {handoff.get('branch') or ''}\n"
            f"commit: {handoff.get('commit') or ''}\n"
            f"files: {shown}\n"
            f"tests: {handoff.get('test_command') or ''}\n"
            f"results: {handoff.get('results') or ''}"
        )
        # R6 L5: the count is the blocking reviewer's verdict size, a
        # conclusion; the secondary reviewer sees facts only.
        if not review_isolation.evidence_hides_blocker_count(snap):
            block += f"\nblocker_count: {handoff.get('blocker_count') or 0}"
        parts.append(block)
    if not parts:
        return ""
    return "\n".join(parts) + "\n"


def _output_slot(outputs: Mapping[str, str], line_id: str) -> str:
    """The output tail under its heading, or the line saying there is none to show.

    The live service passes no outputs, because ``Line.log_tail`` is the worker's raw output
    and nothing redacts it before a second vendor's CLI would see it. The reviewer is told
    that, rather than shown a heading with nothing under it. A blank tail is the same as
    none: it is just as empty a heading.
    """
    tail = outputs.get(line_id, "")
    if not tail.strip():
        return _NO_OUTPUT_NOTICE
    return f"Output tail:\n{_fit(tail, _OUTPUT_BYTES, 'output', keep_end=True)}"


def line_evidence(dispatch: Dispatch, outputs: Mapping[str, str]) -> Callable[[str], str]:
    """The review material for one line: its goal, task and branch, and its output tail.

    Every part is held to its own size (``_GOAL_BYTES`` and the like), which is what lets
    ``branch_evidence`` know how much room the diff has left.
    """

    def evidence(line_id: str) -> str:
        line = dispatch.line(line_id)
        if line is None:
            return f"No line {_fit(line_id, _NAME_BYTES, 'line id')} in dispatch {dispatch.id}"
        return (
            f"Goal: {_fit(dispatch.goal, _GOAL_BYTES, 'goal')}\n"
            f"Line {_fit(line.id, _NAME_BYTES, 'line id')} "
            f"({_fit(line.name, _NAME_BYTES, 'line name')}) "
            f"on branch {_fit(line.branch, _BRANCH_BYTES, 'branch')}\n"
            f"Task: {_fit(line.task, _TASK_BYTES, 'task')}\n"
            f"{_gated_evidence(dispatch, line)}"
            f"{_output_slot(outputs, line_id)}"
        )

    return evidence


def branch_evidence(
    dispatch: Dispatch,
    repo_root: Path,
    *,
    outputs: Mapping[str, str] | None = None,
    runner: GitCommand | None = None,
    budget_bytes: int | None = None,
) -> Callable[[str], Awaitable[str]]:
    """``line_evidence`` plus what the line's branch adds to the repository's HEAD.

    A reviewer in a tool-less or text-only mode cannot open the repository, and the task
    and branch name say what the author was asked to do, not what it did; without the diff
    a pass says nothing about the code. A diff that cannot be read fails the review rather
    than leaving the reviewer to guess, and the machine ends the dispatch in ERROR for it.

    The three-dot form is the merge's own view: the branch's changes since it left HEAD,
    without whatever HEAD gained after. The repository's own diff programs (``diff.external``,
    ``textconv``) are switched off, since a reviewer's evidence must not run its code.

    The diff is what is elastic. Everything around it is measured in bytes and the diff gets
    what is left of ``budget_bytes``, cut on a character boundary with the cut stated, so the
    whole prompt fits one argv string whatever the goal, task and diff are made of. A budget
    with no room for a diff fails the review instead of showing the reviewer none.
    """
    if budget_bytes is None:
        # Leave room for the reply contract CrossReviewer wraps around the
        # evidence, and for the retry note, so the whole prompt stays in budget.
        budget_bytes = REVIEW_PROMPT_BUDGET_BYTES - _review_contract_bytes()
    base = line_evidence(dispatch, outputs or {})
    from src.server.services.autonomy.git_delivery import _GIT_OUTPUT_LIMIT_BYTES

    # Read up to Git's small cap so a diff under it is cut with its exact
    # size stated; past the cap Git is stopped and the size is a lower bound.
    read_limit = max(budget_bytes, _GIT_OUTPUT_LIMIT_BYTES)

    async def evidence(line_id: str) -> str:
        text = base(line_id)
        line = dispatch.line(line_id)
        if line is None:
            return text
        if not line.branch:
            raise DispatchError(f"Line {line_id} has no branch to review")
        base_commit = line.base_commit or "HEAD"
        argv = [
            "diff",
            "--patch-with-stat",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            f"{base_commit}...{line.branch}",
            "--",
        ]
        cut = False
        try:
            if runner is None:
                code, diff, cut = await _review_diff_prefix(argv, Path(repo_root), read_limit)
            else:
                code, diff, _ = await runner(argv, Path(repo_root))
        except (GitDeliveryError, OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
            raise DispatchError(
                f"Could not read the diff for line {line_id}"
            ) from exc
        if code != 0:
            raise DispatchError(f"Could not diff {line.branch} against HEAD for line {line_id}")
        # N73 L4 (PRD 5.1): assignment record, record-only findings, scope.
        # Empty when there is nothing to say, so main's prompt is unchanged.
        gates_now = dispatch.gates if isinstance(dispatch.gates, dict) else {}
        cells_now = gates_now.get("lines") if isinstance(gates_now.get("lines"), dict) else {}
        from .superagent_assign.evidence import review_section as review_evidence_section

        section = review_evidence_section(cells_now.get(line_id), line, diff)
        if section:
            text = f"{text}\n{section}"
        head = f"{text}\nWhat {_fit(line.branch, _BRANCH_BYTES, 'branch')} adds to HEAD:\n"
        room = budget_bytes - _FRAME_RESERVE_BYTES - _size(head)
        if room < _MIN_DIFF_BYTES:
            raise DispatchError(
                f"Line {line_id} leaves no room for its diff in a {budget_bytes}-byte review prompt"
            )
        if cut:
            # Git was stopped at the read cap, so the full size is unknown.
            note = f"[diff truncated: more than {read_limit} bytes in all]"
            keep = max(room - len(note.encode("utf-8")) - 1, 0)
            raw = diff.encode("utf-8", "replace")[:keep]
            return head + f"{raw.decode('utf-8', 'ignore')}\n{note}"
        return head + (_fit(diff, room, "diff") or "(nothing)")

    return evidence


async def _review_diff_prefix(
    argv: list[str], cwd: Path, limit: int
) -> tuple[int, str, bool]:
    """At most ``limit`` bytes of a review diff, and whether Git was cut off.

    A diff past the cap is evidence to trim, not a failed read (N56 follow-up):
    Git is stopped at ``limit`` so nothing unbounded is held, and the caller
    marks the cut. A truncated read has no exit status; it counts as success.
    """
    from src.server.services.autonomy.git_delivery import (
        run_isolated_git_readonly_prefix,
    )

    result = await asyncio.to_thread(
        run_isolated_git_readonly_prefix, str(cwd), argv, limit=limit, timeout=5
    )
    code = 0 if result.truncated else (result.returncode or 0)
    return code, result.stdout.decode("utf-8", "replace"), result.truncated
