"""G2 and G5 for one dispatch, and the ``dispatch_gates_v1`` snapshot.

The runner records focused tests and the integration check. The machine
applies the stage transitions and copies the result back onto this
snapshot. The snapshot stores exit codes, durations, log paths, and a
redacted command display with a hash of that redacted display. It does not
store raw command text, command output, prompts, diffs or blocker text.

Commands are argument lists. Nothing in this module starts a shell.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Protocol, Sequence

from src.llms.native_cli.failure import redact_credentials
from src.server.utils.sanitize import sanitize_output_text
from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    run_isolated_git_mutation,
    run_isolated_git_readonly,
)

from .soft_gates import record_only
from . import worktree_trash
from .env_noise import filter_porcelain, is_noise_path
from .check_modes import (
    ENFORCE,
    OFF,
    REQUIRE_TESTS_CHECK,
    REQUIRE_TESTS_ENV,
    SCOPE_CHECK,
    SCOPE_CHECK_ENV,
    SHADOW,
    SHADOW_REFUSAL,
    WORKTREE_CHECK,
    WORKTREE_CHECK_ENV,
    check_mode,
)
from .fresh_base import BASE_CHECK_KEY, check_base
from .gates_schema import (
    CURRENT_REVISION,
    SCHEMA_REVISION_KEY,
    migrate_dispatch_gates,
    schema_revision,
)
from .preexisting import (
    PREEXISTING_FAILURE,
    check_preexisting,
    preexisting_note,
    rerun_timeout,
)
from .protocols import DispatchError
from .types import Dispatch, Line

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "dispatch_gates_v1"
NOT_A_DAG = "The plan's line dependencies are not a DAG"
MAX_EVENTS = 50


def _safe_command_record(argv: Sequence[str]) -> tuple[str, str]:
    """Return a redacted display and hash of that safe display only."""
    safe_argv: list[str] = []
    redact_next = False
    for item in argv:
        if redact_next:
            safe_argv.append("[REDACTED]")
            redact_next = False
            continue
        lowered = item.casefold().lstrip("-").replace("_", "-")
        flag_name = lowered.split("=", 1)[0]
        if item.startswith("--") and flag_name in {
            "token", "api-key", "apikey", "access-token", "auth-token",
            "password", "passwd", "secret", "client-secret", "credential",
        }:
            if "=" in item:
                safe_argv.append(item.split("=", 1)[0] + "=[REDACTED]")
            else:
                safe_argv.append(item)
                redact_next = True
            continue
        safe_argv.append(item)
    display = sanitize_output_text(shlex.join(safe_argv), limit=1000)
    digest = hashlib.sha256(display.encode("utf-8")).hexdigest()
    return display, digest

PLANNER_OUTPUT_VALID = "planner_output_valid"
FOCUSED_TESTS_PASSED = "focused_tests_passed"
FOCUSED_TESTS_FAILED = "focused_tests_failed"
G2_NO_COMMANDS = "g2_no_commands"
G2_NO_COMMANDS_RISK_BEARING = "g2_no_commands_risk_bearing"
G2_TIMEOUT = "g2_timeout"
G2_REVISION_MOVED = "g2_revision_moved"
# The focused test run left changes in the worktree outside a temporary
# directory: the tests wrote into the checkout instead of into tmp (N47).
G2_WORKTREE_DIRTY = "g2_worktree_dirty"
# The line's diff names files outside its write scope: a scope conflict the
# gate found itself, not one a reviewer wrote in prose (N52).
G2_SCOPE_CONFLICT = "g2_scope_conflict"
G5_PASSED = "g5_passed"
G5_NOT_CONFIGURED = "g5_not_configured"
G5_FULL_SUITE_FAILED = "g5_full_suite_failed"
G5_TIMEOUT = "g5_timeout"
G5_WORKTREE_DIRTY = "g5_worktree_dirty"
ALEMBIC_SINGLE_HEAD = "alembic_single_head"
ALEMBIC_MULTIPLE_HEADS = "alembic_multiple_heads"
ALEMBIC_NO_HEAD = "alembic_no_head"
ALEMBIC_UNPARSEABLE = "alembic_unparseable"
# The checkout has no ``migrations/versions`` directory: a target repository
# that does not use Alembic. The single-head check does not apply there.
ALEMBIC_NOT_APPLICABLE = "alembic_not_applicable"
# TERM, then KILL, each followed by a wait no longer than this.
_STOP_GRACE_SECONDS = 0.5
AWAITING_HUMAN = "awaiting_human"
HUMAN_APPROVED = "human_approved"
HUMAN_REJECTED = "human_rejected"
MERGE_CONFLICT = "merge_conflict"

# Stable strings a later round will record. Listed so the port doc and the
# runner share one spelling; W3a does not emit the review or replan ones.
REVIEW_MERGED = "review_merged"
REVIEW_BLOCKED = "review_blocked"
REVIEWER_CHANGED = "reviewer_changed"
TWO_FAILED_ROUNDS = "two_failed_rounds"
REPLANNED = "replanned"
REPLAN_EXHAUSTED = "replan_exhausted"
HANDOFF_INCOMPLETE = "handoff_incomplete"
FIX_LAUNCH_FAILED = "fix_launch_failed"
STALE_EVENT = "stale_event"
STALE_RESULT = "stale_result"
STALE_DRIVE = "stale_drive"
STALE_FIX_INTENT = "stale_fix_intent"
RESUME_SPEC_MISSING = "resume_spec_missing"
# The planner call raised. The dispatch parks in REVIEW with no lines.
PLANNER_FAILED = "planner_failed"
REVIEW_UNAVAILABLE = "review_unavailable"
REVIEW_ATTEMPT_LIMIT = 3
# The reviewer's reply carried no usable verdict object, not even after the
# same-model correction and one other eligible reviewer.
REVIEW_UNPARSEABLE = "review_unparseable"
SECONDARY_REQUIRED = "secondary_required"
REVIEWER_CONFLICT_SCOPE = "reviewer_conflict_scope"
REVIEWER_CONFLICT_SECURITY = "reviewer_conflict_security"
FOCUSED_TESTS_EXHAUSTED = "focused_tests_exhausted"
NO_SECONDARY_REVIEWER = "no_secondary_reviewer"

G5_COMMAND_SOURCE = "CONDUCTOR_DISPATCH_G5_COMMAND"
CHECK_FULL_SUITE = "full_suite"
CHECK_ALEMBIC = "alembic_single_head"

DEFAULT_RISK_GLOBS: tuple[str, ...] = (
    "migrations/**",
    "src/server/database/**",
    "**/auth*",
    "**/credential*",
    "**/secret*",
    "src/server/app/**",
)

_EVENT_KEYS = (
    "task_id",
    "from_stage",
    "to_stage",
    "gate",
    "result",
    "reason",
    "provider",
    "timestamp",
    "handoff_ref",
)

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(moment: datetime) -> str:
    """UTC ``YYYY-MM-DDTHH:MM:SS+00:00``. Naive values are read as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(frozen=True)
class CommandResult:
    """What a gate command left behind. Output stays in the log file."""

    exit_code: int
    duration_ms: int
    timed_out: bool = False


@dataclass(frozen=True)
class GateStep:
    """One G2 or G5 outcome. ``check`` is the G5 message token, or empty."""

    passed: bool
    reason: str
    result: str
    check: str = ""


class CommandRunner(Protocol):
    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        log_path: Path,
    ) -> CommandResult: ...


class WorktreeManager(Protocol):
    async def add_detached(self, repo: Path, path: Path, start_point: str) -> None: ...

    async def remove(self, repo: Path, path: Path) -> None: ...


@dataclass
class GateSettings:
    runs_dir: Path
    repo_root: Path
    g2_command: str | None = None
    g2_timeout_seconds: float = 900.0
    g5_command: str | None = None
    g5_timeout_seconds: float = 3600.0
    risk_globs: tuple[str, ...] = DEFAULT_RISK_GLOBS
    max_g2_fixes: int = 2
    max_replans: int = 1
    watchdog_seconds: float = 900.0
    heartbeat_threshold_seconds: float = 30.0
    heartbeat_interval_seconds: float = 30.0
    worktrees_dir: Path | None = None


class AlembicUnparseable(Exception):
    """A migration file could not be read or parsed. The head set is unknown."""


class WorktreeDirtyError(RuntimeError):
    """An owned leftover worktree was dirty and could not be rebuilt."""


def _group_alive(pid: int | None) -> bool:
    """True when ``pid``'s process group still has a member.

    The leader's pid is the group id (``start_new_session``). A group id stays
    reserved while any member is alive, including after the leader has been
    reaped. An empty group raises ``ProcessLookupError``.
    """
    if not pid or pid <= 1 or pid == os.getpgrp():
        return False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return True
    return True


def _signal_group(pid: int, sig: int) -> None:
    """Signal one child group, never this process's own."""
    if pid <= 1 or pid == os.getpgrp():
        return
    try:
        os.killpg(pid, sig)
    except OSError:
        try:
            os.kill(pid, sig)
        except OSError:
            pass


async def _read_all(stream: asyncio.StreamReader | None) -> bytes:
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


async def _reap(
    process: asyncio.subprocess.Process, reader: asyncio.Task[bytes]
) -> bytes:
    """Wait out a killed command. Both waits are capped. Cancel is not success."""
    try:
        await asyncio.wait_for(process.wait(), _STOP_GRACE_SECONDS)
    except TimeoutError:
        pass
    if reader.done() and not reader.cancelled() and reader.exception() is None:
        return reader.result()
    try:
        return await asyncio.wait_for(asyncio.shield(reader), _STOP_GRACE_SECONDS)
    except TimeoutError:
        reader.cancel()
        return b""
    except asyncio.CancelledError:
        reader.cancel()
        raise


async def _asleep_bound(fut: asyncio.Future[object], timeout: float) -> None:
    try:
        await asyncio.wait_for(asyncio.shield(fut), timeout)
    except (TimeoutError, asyncio.CancelledError):
        pass


async def _bounded(fut: asyncio.Future[object], timeout: float) -> bool:
    """Wait up to ``timeout``. A cancelled caller still waits out the deadline.

    Returns whether this task was cancelled during the wait. The cancel count
    is cleared so the deadline can elapse; the caller re-raises.
    """
    current = asyncio.current_task()
    saw_cancel = False
    holder = asyncio.ensure_future(_asleep_bound(fut, timeout))
    while not holder.done():
        try:
            await asyncio.wait({holder})
        except asyncio.CancelledError:
            saw_cancel = True
            if current is not None:
                while current.cancelling():
                    current.uncancel()
    if holder.done() and not holder.cancelled():
        holder.exception()
    return saw_cancel


async def _until(fut: asyncio.Future[object]) -> bool:
    """Wait until ``fut`` finishes, absorbing cancellation. Returns whether it did."""
    current = asyncio.current_task()
    saw_cancel = False
    while not fut.done():
        try:
            await asyncio.wait({fut})
        except asyncio.CancelledError:
            saw_cancel = True
            if current is not None:
                while current.cancelling():
                    current.uncancel()
    if fut.done() and not fut.cancelled():
        fut.exception()
    return saw_cancel


async def _stop_command(
    process: asyncio.subprocess.Process, reader: asyncio.Task[bytes]
) -> bytes:
    """TERM the group, wait, KILL it, then reap.

    A cancellation during the grace wait cannot skip the KILL or the reap.
    The ``CancelledError`` is raised again after both, so the command does
    not look like it finished.
    """
    if process.pid:
        _signal_group(process.pid, signal.SIGTERM)
    waiter = asyncio.ensure_future(process.wait())
    cancelled = await _bounded(waiter, _STOP_GRACE_SECONDS)
    # KILL the group even if TERM already reaped the leader. A child that
    # ignored TERM can still hold the stdout pipe.
    if process.pid:
        _signal_group(process.pid, signal.SIGKILL)
    reaper = asyncio.ensure_future(_reap(process, reader))
    cancelled = await _until(reaper) or cancelled
    if cancelled:
        raise asyncio.CancelledError
    if reaper.cancelled() or reaper.exception() is not None:
        return b""
    return reaper.result()


def _reader_result(reader: asyncio.Task[bytes]) -> bytes:
    if reader.cancelled() or reader.exception() is not None:
        return b""
    return reader.result()


async def _finish_group(
    process: asyncio.subprocess.Process, reader: asyncio.Task[bytes]
) -> bytes:
    """The leader has exited. A live group member means the command has not.

    The stdout pipe gets one bounded wait to close. If it stays open, or the
    process group still has a member, the group is stopped: TERM, a bounded
    wait, KILL, reap. A cancel during the pipe wait is not caught here. The
    caller stops the group and raises ``CancelledError`` again.
    """
    if reader.done():
        output = _reader_result(reader)
    else:
        try:
            output = await asyncio.wait_for(asyncio.shield(reader), _STOP_GRACE_SECONDS)
        except TimeoutError:
            output = b""
    if _group_alive(process.pid) or not reader.done():
        stopped = await _stop_command(process, reader)
        return stopped or output
    return output


class SubprocessCommandRunner:
    """Runs an argv with no shell. Stdout and stderr go to ``log_path`` only.

    The command leads its own session. A timeout, a cancelled task, and a
    leader that exited while a descendant still holds the group or the stdout
    pipe all stop that group: TERM, a bounded wait, then KILL, then a bounded
    reap. The command is not finished while a member of the group is alive.
    """

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout: float,
        log_path: Path,
    ) -> CommandResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        reader = asyncio.create_task(_read_all(process.stdout))
        timed_out = False
        stdout = b""
        try:
            try:
                await asyncio.wait_for(process.wait(), timeout)
            except TimeoutError:
                timed_out = True
                stdout = await _stop_command(process, reader)
            else:
                stdout = await _finish_group(process, reader)
        except asyncio.CancelledError:
            await _stop_command(process, reader)
            raise
        finally:
            if not reader.done():
                reader.cancel()
        log_path.write_bytes(stdout or b"")
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if timed_out:
            return CommandResult(exit_code=124, duration_ms=duration_ms, timed_out=True)
        return CommandResult(exit_code=process.returncode or 0, duration_ms=duration_ms)


def _occupied(path: Path) -> bool:
    """True when ``path`` exists and is not an empty directory."""
    if not path.exists():
        return False
    if not path.is_dir():
        return True
    return any(path.iterdir())


def _porcelain_worktrees(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    path = ""
    head = ""
    for line in text.splitlines():
        if line.startswith("worktree "):
            if path:
                found.append((path, head))
            path = line[len("worktree ") :]
            head = ""
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :].strip()
        elif line == "" and path:
            found.append((path, head))
            path = ""
            head = ""
    if path:
        found.append((path, head))
    return found


def git_head(checkout: Path) -> str | None:
    """HEAD of a checkout, or None when ``checkout`` is not a git worktree."""
    if not checkout.is_dir():
        return None
    try:
        done = run_isolated_git_readonly(
            str(checkout), ["rev-parse", "--verify", "HEAD"], timeout=5
        )
    except (GitDeliveryError, OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    sha = done.stdout.decode("utf-8", "replace").strip()
    return sha or None


def git_revision(repo: Path, branch: str) -> str | None:
    """The commit a line branch points at, or None when git cannot say."""
    if not branch:
        return None
    try:
        done = run_isolated_git_readonly(
            str(repo), ["rev-parse", "--verify", branch], timeout=5
        )
    except (GitDeliveryError, OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    sha = done.stdout.decode("utf-8", "replace").strip()
    return sha or None


def git_changed_paths(
    repo: Path, revision: str, base: str = "HEAD"
) -> tuple[str, ...] | None:
    """Files changed since the pinned base, or None when safe inspection failed."""
    if not revision:
        return None
    try:
        done = run_isolated_git_readonly(
            str(repo),
            [
                "diff", "--name-only", "--no-ext-diff", "--no-textconv",
                f"{base}...{revision}", "--",
            ],
            timeout=5,
        )
    except (GitDeliveryError, OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return tuple(
        line.strip()
        for line in done.stdout.decode("utf-8", "replace").splitlines()
        if line.strip()
    )


#: Path segments that name a temporary or tool-generated directory. A
#: focused test run may write under one of these and nowhere else: the
#: tests write to tmp, not into the checkout (N47). The generated trees are
#: here because a test run creates them on its own, and a repository that
#: does not ignore them would otherwise fail every line.
_TEMPORARY_SEGMENTS = frozenset(
    {
        "tmp",
        ".tmp",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        ".venv",
        "venv",
        "node_modules",
        "htmlcov",
        ".coverage",
    }
)

#: How many out-of-scope file names one finding names.
_SCOPE_FILE_LIMIT = 8


def _porcelain_v1_paths(raw: bytes) -> tuple[str, ...]:
    """The paths in one ``git status --porcelain=v1 -z`` stream.

    The ``-z`` form separates the records with NUL, so a file name with a
    space, a quote or a non-ASCII character needs no unescaping.
    """
    fields = raw.decode("utf-8", "replace").split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2] != " ":
            continue
        status = field[:2]
        original = ""
        if "R" in status or "C" in status:
            # A rename or a copy names the original path in the next field.
            original = fields[index] if index < len(fields) else ""
            index += 1
        path = field[3:]
        if status == "??" and is_noise_path(path):
            # Untracked OS litter (.DS_Store) is not a Worker's change.
            continue
        if path:
            paths.append(path)
        if "R" in status and original:
            # A rename also removed the original path, so it changed too.
            paths.append(original)
    return tuple(paths)


def git_status_paths(checkout: Path) -> tuple[str, ...] | None:
    """The paths a checkout has changed, or None when Git cannot say.

    Untracked files count: a test that wrote a stub module into the source
    tree and left it there shows up as one (N47).
    """
    try:
        done = run_isolated_git_readonly(
            str(checkout),
            [
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=all",
            ],
            timeout=5,
        )
    except (GitDeliveryError, OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return _porcelain_v1_paths(done.stdout)


def path_is_temporary(path: str) -> bool:
    """True when a repo-relative path sits under a temporary directory.

    A temporary directory is a ``tmp`` one, or one of the tool-generated
    caches a test run leaves behind on its own.
    """
    normalized = path.replace("\\", "/").strip("/")
    if not normalized:
        return False
    return any(segment in _TEMPORARY_SEGMENTS for segment in normalized.split("/"))


def scope_violations(
    paths: Sequence[str], write_scope: Sequence[str]
) -> tuple[str, ...]:
    """The changed paths that fall outside the line's write scope.

    A line with no write scope is not judged: there is nothing to compare
    the diff against, so the gate says nothing rather than guessing.
    """
    patterns = [pattern for pattern in write_scope if pattern]
    if not patterns:
        return ()
    return tuple(
        path
        for path in paths
        if not any(path_matches(pattern, path) for pattern in patterns)
    )


def _is_git_checkout(path: Path) -> bool:
    """True when the directory carries Git's own bookkeeping.

    A linked worktree holds a ``.git`` file that points at the repository's
    worktree directory.
    """
    return (path / ".git").exists()


#: How many paths one structural record keeps, and how long its finding may
#: be: a run that writes thousands of files must not grow the dispatch row
#: by hundreds of kilobytes per round.
_RECORD_PATH_LIMIT = 50
_FINDING_LIMIT = 2000


def _shown(paths: Sequence[str]) -> str:
    shown = ", ".join(paths[:_SCOPE_FILE_LIMIT])
    if len(paths) > _SCOPE_FILE_LIMIT:
        shown += f", +{len(paths) - _SCOPE_FILE_LIMIT} more"
    return shown


def _unknown_scope(write_scope: Sequence[str]) -> dict[str, object]:
    """The record for a scope check that could not compare the diff."""
    return {
        "conflict": "scope",
        "files": [],
        "count": 0,
        "unknown": True,
        "write_scope": [pattern for pattern in write_scope if pattern][:_RECORD_PATH_LIMIT],
        "finding": (
            "scope: the line's diff could not be read, so it cannot be shown "
            "to fit its write scope"
        ),
    }


def _unknown_worktree() -> dict[str, object]:
    """The record for a worktree whose status could not be read."""
    return {
        "paths": [],
        "unknown": True,
        "finding": (
            "The focused test run left the worktree in an unknown state: Git "
            "could not read its status, so it cannot be shown to be clean."
        ),
    }


def scope_conflict_finding(
    files: Sequence[str], write_scope: Sequence[str]
) -> str:
    """The tagged finding a scope conflict carries.

    The ``scope:`` prefix is the tag a reviewer's findings use, so a
    conflict the gate found reads the same way as one a reviewer wrote
    (N52).
    """
    shown = ", ".join(files[:_SCOPE_FILE_LIMIT])
    if len(files) > _SCOPE_FILE_LIMIT:
        shown += f", +{len(files) - _SCOPE_FILE_LIMIT} more"
    scope = ", ".join(pattern for pattern in write_scope if pattern) or "(none)"
    return (
        f"scope: the line changed {len(files)} file(s) outside its write scope: "
        f"{shown} (write scope: {scope})"
    )


#: The structural G2 records, the check each belongs to, the reason G2 fails
#: with in ``enforce`` mode, and the variable that sets its mode (N58).
_STRUCTURAL_CHECKS = (
    ("dirty_worktree", WORKTREE_CHECK, G2_WORKTREE_DIRTY, WORKTREE_CHECK_ENV),
    ("scope_conflict", SCOPE_CHECK, G2_SCOPE_CONFLICT, SCOPE_CHECK_ENV),
)


def shadow_record(
    check: str, would_refuse: str, finding: str, record: dict[str, object] | None = None
) -> dict[str, object]:
    """What a ``shadow`` check would have refused, as G2 stores it."""
    entry: dict[str, object] = {
        "check": check,
        "mode": SHADOW,
        "would_refuse": would_refuse,
        "finding": _clip_text(finding),
    }
    if record is not None:
        entry["record"] = _capped_record(record)
    return entry


def _clip_text(text: str) -> str:
    return text if len(text) <= _FINDING_LIMIT else text[: _FINDING_LIMIT - 1] + "…"


def _capped_record(record: dict[str, object]) -> dict[str, object]:
    """A copy with every list and the finding cut to the record limits."""
    capped: dict[str, object] = {}
    for key, value in record.items():
        if isinstance(value, list):
            capped[key] = value[:_RECORD_PATH_LIMIT]
            if len(value) > _RECORD_PATH_LIMIT and "count" not in record:
                capped["count"] = len(value)
        elif isinstance(value, str):
            capped[key] = _clip_text(value)
        else:
            capped[key] = value
    return capped


#: N73 S4-S6: how many shadow findings the review evidence carries per line.
SHADOW_FINDINGS_MAX = 10


def shadow_findings(snapshot: object) -> list[dict[str, object]]:
    """N46/N47/N52 records-only findings on a line, for the review evidence.

    Read from the line's G2 cell (``gates.G2.shadow``): the check, what it
    would have refused, the clipped finding and the file names it named.
    Nothing else of the snapshot leaks into the review (R6 L5).
    """
    if not isinstance(snapshot, dict):
        return []
    cells = snapshot.get("gates")
    g2 = cells.get("G2") if isinstance(cells, dict) else None
    entries = g2.get("shadow") if isinstance(g2, dict) else None
    out: list[dict[str, object]] = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        record = entry.get("record") if isinstance(entry.get("record"), dict) else {}
        files = [
            str(item)
            for key in ("files", "paths", "outside_scope", "dirty")
            for item in (record.get(key) or [])
            if isinstance(record.get(key), list)
        ][:_RECORD_PATH_LIMIT]
        out.append(
            {
                "check": str(entry.get("check") or ""),
                "would_refuse": str(entry.get("would_refuse") or ""),
                "finding": _clip_text(str(entry.get("finding") or "")),
                "files": files,
            }
        )
        if len(out) >= SHADOW_FINDINGS_MAX:
            break
    return out


def apply_check_modes(
    findings: dict[str, object], modes: dict[str, str]
) -> dict[str, object]:
    """Keep enforced records; turn shadow ones into ``shadow`` entries.

    ``modes`` maps a record name (``dirty_worktree``, ``scope_conflict``) to its
    mode. An ``enforce`` record stays where G2 fails on it; a ``shadow`` one
    moves to the ``shadow`` list, which G2 records and does not fail on; an
    ``off`` one is dropped.
    """
    kept: dict[str, object] = {}
    shadow = list(findings.get("shadow") or [])
    for name, check, reason, _env in _STRUCTURAL_CHECKS:
        record = findings.get(name)
        if not isinstance(record, dict):
            continue
        mode = modes.get(name, ENFORCE)
        if mode == ENFORCE:
            kept[name] = record
        elif mode == SHADOW:
            shadow.append(
                shadow_record(check, reason, str(record.get("finding") or ""), record)
            )
    if shadow:
        kept["shadow"] = shadow
    return kept


def scheduler_view(scheduler: dict[str, object], *, now: datetime | None = None) -> dict[str, object]:
    """A copy whose ``healthy`` flag reflects the heartbeat age."""
    viewed = dict(scheduler)
    moment = now or datetime.now(timezone.utc)
    threshold = float(viewed.get("threshold_seconds") or 30)
    beat = viewed.get("heartbeat_at")
    healthy = False
    if isinstance(beat, str) and beat:
        try:
            recorded = datetime.fromisoformat(beat)
        except ValueError:
            recorded = None
        if recorded is not None:
            if recorded.tzinfo is None:
                recorded = recorded.replace(tzinfo=timezone.utc)
            age = (moment - recorded.astimezone(timezone.utc)).total_seconds()
            healthy = age <= threshold
    viewed["healthy"] = healthy
    return viewed


async def _isolated_git_exec(
    argv: list[str], cwd: Path
) -> tuple[int, str, str]:
    """Async adapter for the shared readonly Git boundary."""
    result = await asyncio.to_thread(
        run_isolated_git_readonly, str(cwd), argv, timeout=5
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )


async def _isolated_git_mutation_exec(
    argv: list[str], cwd: Path, worktrees_root: Path, *, timeout: float = 5
) -> tuple[int, str, str]:
    """Async adapter for the separate writable, no-network Git boundary."""
    result = await asyncio.to_thread(
        run_isolated_git_mutation,
        str(cwd),
        argv,
        repository_root=str(cwd),
        worktrees_root=str(worktrees_root),
        timeout=timeout,
    )
    return (
        result.returncode,
        result.stdout.decode("utf-8", "replace"),
        result.stderr.decode("utf-8", "replace"),
    )


class GitWorktreeManager:
    """``git worktree add --detach`` and ``git worktree remove --force``."""

    def __init__(
        self,
        runner: Callable[[list[str], Path], Awaitable[tuple[int, str, str]]]
        | None = None,
        trash_root: Path | None = None,
    ) -> None:
        self._runner = runner or _git_exec
        # Where remove() renames a worktree before deleting it in the
        # background. The GateRunner points it at its worktrees root.
        self.trash_root = trash_root

    async def add_detached(self, repo: Path, path: Path, start_point: str) -> None:
        """Add a detached worktree, reusing or clearing only this repo's leftover.

        A previous run of this dispatch may have left ``path`` registered.
        If that registration belongs to ``repo`` and already sits on
        ``start_point``, it is reused. If it belongs to ``repo`` at another
        commit, it is removed and added again. A path that is not this repo's
        worktree is left untouched.
        """
        desired = await self._revision(repo, start_point)
        owned = await self._owned_head(repo, path)
        dirty = False
        if owned is not None:
            if desired and owned == desired and path.is_dir() and await self._clean(path):
                return
            dirty = bool(desired and owned == desired and path.is_dir())
            # A dirty tree at the wanted commit is not renamed into the trash:
            # ``git worktree remove --force`` still discards its edits, but
            # when Git refuses (busy, locked) the content stays (G5 dirty).
            await self.remove(repo, path, trash=not dirty)
            if dirty and _occupied(path):
                logger.warning("dirty worktree could not be removed")
                raise WorktreeDirtyError(G5_WORKTREE_DIRTY)
        elif _occupied(path):
            logger.warning(
                "refusing to replace a path that is not this repo's worktree"
            )
            raise RuntimeError("worktree path is not ours")
        path.parent.mkdir(parents=True, exist_ok=True)
        argv = ["worktree", "add", "--detach", str(path), start_point]
        if self._runner is _git_exec:
            code, _out, _err = await _isolated_git_mutation_exec(
                argv, repo, path.parent
            )
        else:
            code, _out, _err = await self._runner(argv, repo)
        if code != 0:
            logger.warning("worktree add failed")
            if dirty:
                raise WorktreeDirtyError(G5_WORKTREE_DIRTY)
            raise RuntimeError("worktree add failed")

    async def _clean(self, path: Path) -> bool:
        """True when the worktree has no tracked, untracked, or unmerged change."""
        argv = ["status", "--porcelain", "--untracked-files=all"]
        if self._runner is _git_exec:
            code, out, _err = await _isolated_git_exec(argv, path)
        else:
            code, out, _err = await self._runner(argv, path)
        if code != 0:
            return False
        return filter_porcelain(out).strip() == ""

    async def _revision(self, repo: Path, start_point: str) -> str | None:
        argv = ["rev-parse", "--verify", start_point]
        try:
            if self._runner is _git_exec:
                code, out, _err = await _isolated_git_exec(argv, repo)
            else:
                code, out, _err = await self._runner(argv, repo)
        except GitDeliveryError:
            return None
        if code != 0:
            return None
        text = out.strip()
        return text or None

    async def _owned_head(self, repo: Path, path: Path) -> str | None:
        try:
            if self._runner is _git_exec:
                code, out, _err = await _isolated_git_exec(
                    ["worktree", "list", "--porcelain"], repo
                )
            else:
                code, out, _err = await self._runner(
                    ["worktree", "list", "--porcelain"], repo
                )
        except GitDeliveryError:
            return None
        if code != 0:
            return None
        try:
            target = path.resolve()
        except OSError:
            return None
        for listed, head in _porcelain_worktrees(out):
            try:
                if Path(listed).resolve() == target:
                    return head
            except OSError:
                continue
        return None

    async def remove(self, repo: Path, path: Path, *, trash: bool = True) -> None:
        """Free ``path`` now; delete its files later. Never raises.

        The worktree is renamed into the trash (atomic, so a large
        ``node_modules`` cannot outlast the Git timeout), the dangling
        registration is pruned and the files are deleted in the background.
        When the rename is impossible, or ``trash`` is false (a dirty tree
        that only Git may discard), it falls back to ``git worktree remove
        --force`` with a longer bounded timeout.
        """
        try:
            moved = None
            if trash and not await self._locked(repo, path):
                trash_root = worktree_trash.trash_dir_for(path, self.trash_root)
                moved = worktree_trash.move_to_trash(path, trash_root)
            if moved is not None:
                await self._git_mutation(["worktree", "prune"], repo, path.parent)
                worktree_trash.delete_later(moved)
                return
            code = await self._git_mutation(
                ["worktree", "remove", "--force", str(path)],
                repo,
                path.parent,
                timeout=_WORKTREE_REMOVE_TIMEOUT,
            )
            if code != 0:
                logger.warning("worktree remove failed")
        except Exception:  # noqa: BLE001 - a failed delete is never a test failure
            logger.warning("worktree remove failed")

    async def _locked(self, repo: Path, path: Path) -> bool:
        """True when Git lists ``path`` as locked, or the list is unreadable."""
        argv = ["worktree", "list", "--porcelain"]
        try:
            if self._runner is _git_exec:
                code, out, _err = await _isolated_git_exec(argv, repo)
            else:
                code, out, _err = await self._runner(argv, repo)
        except Exception:  # noqa: BLE001 - unknown means do not trash
            return True
        if code != 0:
            return True
        return worktree_trash.is_locked(out, path)

    async def _git_mutation(
        self, argv: list[str], repo: Path, root: Path, *, timeout: float = 5
    ) -> int:
        if self._runner is _git_exec:
            code, _out, _err = await _isolated_git_mutation_exec(
                argv, repo, root, timeout=timeout
            )
        else:
            code, _out, _err = await self._runner(argv, repo)
        if code != 0 and argv[:2] == ["worktree", "prune"]:
            logger.warning("worktree prune failed")
        return code


#: Bounded timeout for the ``git worktree remove --force`` fallback.
_WORKTREE_REMOVE_TIMEOUT = 120.0


async def _git_exec(argv: list[str], cwd: Path) -> tuple[int, str, str]:
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    process = await asyncio.create_subprocess_exec(
        "git",
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await process.communicate()
    return (
        process.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace"),
    )


def _glob_regex(pattern: str) -> re.Pattern[str]:
    pattern = pattern.replace("\\", "/").lstrip("/")
    parts = ["^"]
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("/**", index) and index + 3 == len(pattern):
            parts.append("(?:/.*)?")
            index += 3
        elif pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    parts.append("$")
    return re.compile("".join(parts))


def path_matches(pattern: str, path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return _glob_regex(pattern).search(normalized) is not None


def line_is_risk_bearing(line: Line, globs: Sequence[str]) -> bool:
    """The planner flag, or any write path that hits a risk glob."""
    if line.risk_bearing:
        return True
    for raw in line.write_scope:
        for pattern in globs:
            if pattern and path_matches(pattern, raw):
                return True
    return False


def acceptance_command_body(item: object) -> str | None:
    """The command text of a ``cmd:`` acceptance item, or ``None`` if it is not one.

    The one reading of the prefix: G2 runs what this returns, and the planner
    validator judges the same text.
    """
    if not isinstance(item, str) or not item.startswith("cmd:"):
        return None
    return item[4:].strip()


def acceptance_commands(line: Line, fallback: str | None) -> list[list[str]] | None:
    """Argv lists from ``cmd:`` items, else the fallback. ``None`` if one cannot be split.

    Items that do not start with ``cmd:`` are text for a reviewer, not commands.
    """
    commands: list[list[str]] = []
    saw_cmd = False
    for item in line.acceptance:
        body = acceptance_command_body(item)
        if body is None:
            continue
        saw_cmd = True
        if not body:
            continue
        try:
            argv = shlex.split(body)
        except ValueError:
            logger.warning("G2 acceptance command could not be split")
            return None
        if argv:
            commands.append(argv)
    if commands:
        return commands
    if saw_cmd:
        return None
    if fallback and fallback.strip():
        try:
            argv = shlex.split(fallback.strip())
        except ValueError:
            logger.warning("G2 fallback command could not be split")
            return None
        return [argv] if argv else None
    return []


# ---------------------------------------------------------------------------
# G2 lint baseline (2026-11-13, R7 line-3)
#
# An acceptance command that lints files with lint debt already on the base
# commit could never pass: ``ruff check shell.py`` failed on 105 findings
# nobody on the line had written. A recognised ``ruff check`` that fails with
# findings (exit 1) is therefore run again with ``--output-format json`` in the
# gate worktree and in a detached worktree at the line's base commit, and the
# command passes when every finding at the head was already there at the base,
# compared as a multiset of (file, code, message). Anything the comparison
# cannot establish -- no base commit, a base checkout that fails, an exit other
# than 0/1, output that is not ruff's JSON, a timeout -- keeps the original
# failure. Other commands are untouched. ``CONDUCTOR_DISPATCH_G2_LINT_BASELINE=0``
# restores the plain exit-code verdict.
# ---------------------------------------------------------------------------

G2_LINT_BASELINE_ENV = "CONDUCTOR_DISPATCH_G2_LINT_BASELINE"

# ``ruff check`` options that change what the command does, not only what it
# reports: a fix writes files, a watch never ends, a diff or a statistics
# table is not the findings list, silent prints nothing, an output file takes
# the findings away, and stdin has no file to compare. Such a command keeps
# its plain verdict.
_RUFF_UNSUPPORTED = frozenset(
    {
        "--fix",
        "--fix-only",
        "--unsafe-fixes",
        "--watch",
        "-w",
        "--diff",
        "--statistics",
        "--show-files",
        "--show-settings",
        "--add-noqa",
        "--silent",
        "-s",
        "--exit-zero",
        "-e",
        "--output-file",
        "-o",
        "--stdin-filename",
        "-",
    }
)
# ``ruff check`` options that take the next argument as their value.
_RUFF_VALUE_OPTIONS = frozenset(
    {
        "--config",
        "--select",
        "--extend-select",
        "--ignore",
        "--extend-ignore",
        "--per-file-ignores",
        "--extend-per-file-ignores",
        "--exclude",
        "--extend-exclude",
        "--fixable",
        "--unfixable",
        "--extend-fixable",
        "--target-version",
        "--line-length",
        "--dummy-variable-rgx",
        "--cache-dir",
        "--output-format",
        "--extension",
        "--python",
    }
)
# ``uv run`` / ``uvx`` options that take the next argument as their value.
_UV_VALUE_OPTIONS = frozenset(
    {
        "--with",
        "--with-editable",
        "--with-requirements",
        "--python",
        "-p",
        "--project",
        "--directory",
        "--package",
        "--extra",
        "--group",
        "--env-file",
        "--index",
        "--default-index",
        "--index-url",
        "--extra-index-url",
        "--from",
        "--cache-dir",
        "--config-file",
    }
)
_DIGITS = re.compile(r"\d+")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_RUFF_TOOL = re.compile(r"^ruff(?:[@=<>~!].*)?$")


def lint_baseline_enabled(environ: dict[str, str] | None = None) -> bool:
    """On unless ``CONDUCTOR_DISPATCH_G2_LINT_BASELINE`` is ``0`` (or off/false/no)."""
    value = (environ if environ is not None else os.environ).get(G2_LINT_BASELINE_ENV, "")
    return value.strip().lower() not in {"0", "off", "false", "no"}


def _skip_uv_options(argv: Sequence[str], index: int) -> int:
    while index < len(argv) and argv[index].startswith("-"):
        token = argv[index]
        if token == "--":
            return index + 1
        index += 2 if token in _UV_VALUE_OPTIONS else 1
    return index


def ruff_check_index(argv: Sequence[str]) -> int | None:
    """Where ``check`` sits in a recognised ``ruff check`` argv, else ``None``.

    Recognised: ``ruff``, a path ending in ``/ruff``, ``python -m ruff``,
    ``uv run [opts] ruff``, ``uvx [opts] ruff[@version]`` and ``uv tool run``,
    each after optional ``VAR=value`` words or ``env``. ``check`` must follow
    the program directly, and the command must not use an option that
    changes what ``ruff check`` does (:data:`_RUFF_UNSUPPORTED`).
    """
    index = 0
    while index < len(argv) and (argv[index] == "env" or _ENV_ASSIGNMENT.match(argv[index])):
        index += 1
    if index >= len(argv):
        return None
    program = os.path.basename(argv[index])
    if program == "uv" and index + 1 < len(argv) and argv[index + 1] == "run":
        index = _skip_uv_options(argv, index + 2)
        if index < len(argv) and os.path.basename(argv[index]) == "ruff":
            index += 1
        elif (
            index + 2 < len(argv)
            and re.match(r"^python[0-9.]*$", os.path.basename(argv[index]))
            and argv[index + 1] == "-m"
            and argv[index + 2] == "ruff"
        ):
            index += 3
        else:
            return None
    elif program == "uvx" or (
        program == "uv" and argv[index + 1 : index + 3] == ["tool", "run"]
    ):
        index = _skip_uv_options(argv, index + (1 if program == "uvx" else 3))
        if index < len(argv) and _RUFF_TOOL.match(argv[index]):
            index += 1
        else:
            return None
    elif program == "ruff":
        index += 1
    elif (
        re.match(r"^python[0-9.]*$", program)
        and argv[index + 1 : index + 3] == ["-m", "ruff"]
    ):
        index += 3
    else:
        return None
    if index >= len(argv) or argv[index] != "check":
        return None
    for token in argv[index + 1 :]:
        if token.split("=", 1)[0] in _RUFF_UNSUPPORTED:
            return None
    return index


def _ruff_paths(argv: Sequence[str], check: int) -> list[int]:
    """The indexes of the positional (path) words after ``check``."""
    found: list[int] = []
    index = check + 1
    only_paths = False
    while index < len(argv):
        token = argv[index]
        if only_paths or not token.startswith("-"):
            found.append(index)
        elif token == "--":
            only_paths = True
        elif token in _RUFF_VALUE_OPTIONS:
            index += 1
        index += 1
    return found


def ruff_json_argv(argv: Sequence[str], check: int) -> list[str]:
    """The same ``ruff check`` with its findings printed as JSON."""
    out = list(argv[: check + 1]) + ["--output-format", "json"]
    index = check + 1
    while index < len(argv):
        token = argv[index]
        if token == "--":
            out.extend(argv[index:])
            break
        if token == "--output-format":
            index += 2
            continue
        if token.startswith("--output-format="):
            index += 1
            continue
        out.append(token)
        index += 1
    return out


def _base_ruff_argv(
    json_argv: Sequence[str], check: int, head: Path, base: Path
) -> list[str] | None:
    """The JSON argv for the base checkout, or ``None`` when nothing is left.

    A path the line added does not exist at the base, and ruff reports a
    missing path as an error, so such a path is dropped: everything found in
    it at the head is new. Only a word that names a path in the head
    worktree and none in the base one is dropped, so an option's value is
    never mistaken for a path. A command whose every path was dropped has no
    base findings (``None``), never a bare ``ruff check`` of the whole tree.
    """
    paths = _ruff_paths(json_argv, check)
    dropped = {
        index
        for index in paths
        if (head / json_argv[index]).exists() and not (base / json_argv[index]).exists()
    }
    if paths and dropped and len(dropped) == len(paths):
        return None
    return [token for index, token in enumerate(json_argv) if index not in dropped]


def parse_ruff_findings(text: str, cwd: Path) -> list[tuple[str, str, str]] | None:
    """Ruff's JSON findings as (path relative to ``cwd``, code, message).

    The log holds stdout and stderr together, so a warning may precede the
    JSON array: the first line that starts a JSON array of objects is read.
    ``None`` when there is none.
    """
    decoder = json.JSONDecoder()
    root = cwd.resolve()
    offset = 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith("["):
            start = offset + (len(line) - len(line.lstrip()))
            try:
                payload, _end = decoder.raw_decode(text, start)
            except ValueError:
                payload = None
            if isinstance(payload, list) and all(isinstance(item, dict) for item in payload):
                findings: list[tuple[str, str, str]] = []
                for item in payload:
                    raw = str(item.get("filename") or "")
                    path = Path(raw)
                    try:
                        rel = (path if path.is_absolute() else cwd / path).resolve()
                        name = rel.relative_to(root).as_posix()
                    except (OSError, ValueError):
                        name = raw
                    # Digits go: a message may name a line or a length
                    # ("from line 12", "(105 > 100)"), and an edit above old
                    # debt must not make that debt look new.
                    message = _DIGITS.sub("#", str(item.get("message") or ""))
                    findings.append((name, str(item.get("code") or ""), message))
                return findings
        offset += len(line)
    return None


def new_lint_findings(
    head: Sequence[tuple[str, str, str]], base: Sequence[tuple[str, str, str]]
) -> int:
    """How many head findings the base did not already have (multiset)."""
    remaining = Counter(base)
    new = 0
    for finding in head:
        if remaining[finding] > 0:
            remaining[finding] -= 1
        else:
            new += 1
    return new


# N27: what a G2 fix round tells the Worker about the failed focused tests.
# The tail is cut from the end of the log, where pytest and most runners print
# the failures and the summary. The budget is fixed so the note always fits in
# the fix task next to the handoff (``MAX_FIX_TASK_BYTES``, the planner's and
# the machine's task ceiling).
G2_FIX_OUTPUT_BUDGET_BYTES = 4_000
MAX_FIX_TASK_BYTES = 8_000
_G2_TRUNCATED = "\ntruncated\n"


def _tail_bytes(text: str, budget: int) -> str:
    """The last ``budget`` UTF-8 bytes of ``text``, cut on a character edge."""
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    return raw[len(raw) - budget:].decode("utf-8", "ignore")


def _window_size(budget: int) -> int:
    """How many raw bytes of a log's end are read for a ``budget``-byte tail."""
    return 2 * budget


def read_log_tail(path: Path, size: int) -> tuple[bytes, bool]:
    """The last ``size`` bytes of ``path``, and whether the file was longer.

    Only the tail is read: a focused-test log can be large, and the note
    needs its end only.
    """
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        length = stream.tell()
        start = max(length - size, 0)
        stream.seek(start)
        return stream.read(size), start > 0


def redacted_output_tail(
    raw: bytes, budget: int = G2_FIX_OUTPUT_BUDGET_BYTES, *, cut: bool | None = None
) -> str:
    """A bounded, redacted tail of a command log, made of whole lines.

    ``raw`` is the log, or the end of it that :func:`read_log_tail` read; then
    ``cut`` says the file went on before it. Redaction needs the whole of a
    secret's line -- its key or its ``Bearer`` prefix is what marks the value
    -- so no line is redacted from a fragment:

    * when the window is cut, its first line is a fragment and is dropped;
    * the rest of the window (twice the budget) is redacted as one text, so a
      key on one line still covers a value on the next;
    * only then are the lines kept that start in the last ``budget`` raw
      bytes, so the line before any kept line was part of the redacted text;
    * after redaction the text is held to ``budget`` again by dropping whole
      lines from the front (a marker can be longer than what it replaced).

    Credentials go through the classifier's redaction and then the output
    sanitiser, which also strips terminal controls. A last line longer than
    the budget yields nothing: there is no whole line to show.
    """
    if budget <= 0:
        return ""
    size = _window_size(budget)
    if cut is None:
        cut = len(raw) > size
    window = raw[-size:] if len(raw) > size else raw
    keep_from = max(len(window) - budget, 0)
    body_start = 0
    if cut:
        newline = window.find(b"\n")
        if newline < 0:
            return ""
        body_start = newline + 1
    body = window[body_start:].rstrip(b"\n")
    if not body:
        return ""
    # How many whole lines start in the last ``budget`` raw bytes.
    starts = [body_start]
    starts.extend(
        body_start + index + 1 for index, byte in enumerate(body) if byte == 0x0A
    )
    # When the window is cut, the first body line is never kept: its
    # predecessor is the dropped fragment, which was not redacted with it, so
    # a key at the end of that fragment would leave its value bare. This holds
    # even when the fragment alone is longer than the budget.
    counted = starts[1:] if cut else starts
    keep = sum(1 for at in counted if at >= keep_from)
    if keep == 0:
        return ""
    # The whole body is redacted at once, so a key on one line still covers
    # its value on the next. Only then are the kept lines picked.
    text = str(redact_credentials(body.decode("utf-8", "replace")))
    text = sanitize_output_text(text, limit=0).strip("\n")
    redacted = text.split("\n")
    if cut:
        # The first line is never shown (see above), also when redaction
        # joined or split lines and the count no longer maps onto them.
        redacted = redacted[1:]
    lines = redacted[-keep:] if redacted else []
    while len(lines) > 1 and len("\n".join(lines).encode("utf-8")) > budget:
        lines.pop(0)
    kept = "\n".join(lines)
    if len(kept.encode("utf-8")) > budget:
        # One redacted line longer than the budget: its end, already redacted.
        kept = _tail_bytes(kept, budget)
    return kept


def g2_failure_note(
    cell: object,
    runs_dir: Path,
    *,
    budget: int = G2_FIX_OUTPUT_BUDGET_BYTES,
) -> str:
    """The fix-round note for a failed G2 cell, or ``""`` when it did not fail.

    Pure apart from reading the failed command's own log under ``runs_dir``:
    the command display is the redacted one the snapshot already stores, the
    exit code is the recorded one, and the output is a redacted tail of at
    most ``budget`` bytes. A log reference that leaves ``runs_dir`` is not
    read.
    """
    if not isinstance(cell, dict) or cell.get("result") != "fail":
        return ""
    reason = str(cell.get("reason") or FOCUSED_TESTS_FAILED)
    commands = [item for item in cell.get("commands") or [] if isinstance(item, dict)]
    # A ruff check the lint baseline passed did not fail the gate, so it is
    # neither the failed command nor the fallback one.
    counted = [
        item
        for item in commands
        if not (
            isinstance(item.get("lint_baseline"), dict)
            and item["lint_baseline"].get("passed") is True
        )
    ]
    failed = next(
        (item for item in reversed(counted) if item.get("exit_code") not in (0, None)),
        # Every command passed: the last one, whose baseline line says so.
        counted[-1] if counted else (commands[-1] if commands else None),
    )
    if reason in (G2_WORKTREE_DIRTY, G2_SCOPE_CONFLICT):
        # The tests themselves passed; the gate failed on what the worktree
        # showed. Saying "focused tests failed" here would be wrong.
        lines = [f"G2 failed: {reason}"]
    else:
        lines = [f"Focused tests (G2) failed: {reason}"]
    # The structural findings the gate read from the worktree. The scope
    # conflict carries the ``scope:`` tag, so it reads like a reviewer's
    # finding and can be adjudicated the same way (N52).
    for name in ("dirty_worktree", "scope_conflict"):
        record = cell.get(name)
        if not isinstance(record, dict):
            continue
        finding = str(record.get("finding") or "").strip()
        if finding:
            lines.append(finding)
    if failed is None:
        lines.append("No focused-test command ran, so there is no output.")
        return "\n".join(lines) + "\n"
    lines.append(f"command: {failed.get('safe_command') or 'unknown'}")
    lines.append(f"exit code: {failed.get('exit_code')}")
    baseline = failed.get("lint_baseline")
    if isinstance(baseline, dict) and isinstance(baseline.get("new_findings"), int):
        lines.append(
            "lint baseline: only findings new since the base commit count;"
            f" {baseline['new_findings']} new ({baseline.get('head_findings')} now,"
            f" {baseline.get('base_findings')} on the base commit)"
        )
    preexisting = preexisting_note(failed.get("preexisting"))
    if preexisting:
        lines.append(preexisting)
    output = ""
    log_ref = failed.get("log_ref")
    if isinstance(log_ref, str) and log_ref:
        root = Path(runs_dir).resolve()
        path = (root / log_ref).resolve()
        if path.is_relative_to(root):
            try:
                raw, cut = read_log_tail(path, _window_size(budget))
                output = redacted_output_tail(raw, budget, cut=cut)
            except OSError:
                output = ""
    if output:
        lines.append(
            f"output (last {budget} bytes at most, credentials redacted):"
        )
        lines.append(output)
    else:
        lines.append("output: unavailable")
    if reason == G2_SCOPE_CONFLICT:
        lines.append(
            "Undo the changes outside the line's write scope before you commit."
        )
    elif reason == G2_WORKTREE_DIRTY:
        lines.append(
            "Leave no changes outside a temporary directory before you commit."
        )
    else:
        lines.append("Fix these failures; re-run the command above before you commit.")
    return "\n".join(lines) + "\n"


def with_g2_failure(task: str, note: str, *, limit: int = MAX_FIX_TASK_BYTES) -> str:
    """The fix task with the G2 note appended, kept inside ``limit`` bytes.

    The note is what the Worker needs to act on, so it is kept whole and the
    task text before it is cut (with a ``truncated`` marker) when the two do
    not fit together.
    """
    if not note:
        return task
    joined = f"{task.rstrip()}\n\n{note}"
    if len(joined.encode("utf-8")) <= limit:
        return joined
    room = limit - len(note.encode("utf-8")) - len(_G2_TRUNCATED.encode("utf-8")) - 1
    if room <= 0:
        return _tail_bytes(note, limit)
    head = task.encode("utf-8")[:room].decode("utf-8", "ignore")
    return f"{head}{_G2_TRUNCATED}\n{note}"


def topological_lines(lines: Sequence[Line]) -> list[Line]:
    """Planner order, with ``depends_on`` edges first. Raises if that is not a DAG."""
    by_id = {line.id: line for line in lines}
    index = {line.id: position for position, line in enumerate(lines)}
    children: dict[str, list[str]] = {line.id: [] for line in lines}
    indegree = {line.id: 0 for line in lines}
    for line in lines:
        seen: set[str] = set()
        for dep in line.depends_on:
            if dep not in by_id or dep in seen:
                if dep not in by_id:
                    raise DispatchError(NOT_A_DAG)
                continue
            seen.add(dep)
            children[dep].append(line.id)
            indegree[line.id] += 1
    ready = sorted(
        (line_id for line_id, degree in indegree.items() if degree == 0), key=index.get
    )
    ordered: list[Line] = []
    while ready:
        line_id = ready.pop(0)
        ordered.append(by_id[line_id])
        unlocked: list[str] = []
        for child in children[line_id]:
            indegree[child] -= 1
            if indegree[child] == 0:
                unlocked.append(child)
        if unlocked:
            ready.extend(unlocked)
            ready.sort(key=index.get)
    if len(ordered) != len(lines):
        raise DispatchError(NOT_A_DAG)
    return ordered


def alembic_versions_dir(worktree: Path) -> tuple[Path, bool]:
    """The versions directory G5 checks, and whether ``alembic.ini`` exists.

    With an ``alembic.ini`` the directory is ``<script_location>/versions``
    (``%(here)s`` is the worktree). A malformed file raises
    ``AlembicUnparseable``.
    """
    import configparser

    ini = worktree / "alembic.ini"
    if not ini.is_file():
        return worktree / "migrations" / "versions", False
    parser = configparser.ConfigParser(defaults={"here": str(worktree)})
    try:
        parser.read(ini, encoding="utf-8")
        location = parser.get("alembic", "script_location", fallback="migrations")
    except (configparser.Error, OSError, UnicodeDecodeError):
        raise AlembicUnparseable from None
    script = Path(location.strip() or "migrations")
    return (script if script.is_absolute() else worktree / script) / "versions", True


def parse_alembic_heads(versions_dir: Path) -> list[str]:
    """Revision ids that no other file names as ``down_revision``.

    Reads the source as text and walks assignments. It does not import the
    modules and it does not open a database. The result is sorted.
    """
    import ast

    revisions: set[str] = set()
    parents: set[str] = set()
    if not versions_dir.is_dir():
        return []
    for path in sorted(versions_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            raise AlembicUnparseable from None
        try:
            tree = ast.parse(source)
        except SyntaxError:
            raise AlembicUnparseable from None
        found: dict[str, ast.AST] = {}
        for node in tree.body:
            targets: list[ast.AST] = []
            value: ast.AST | None = None
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
                value = node.value
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
                value = node.value
            if value is None:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                }:
                    found[target.id] = value
        revision = _string_literal(found.get("revision"))
        if revision is None:
            continue
        revisions.add(revision)
        parents.update(_down_revisions(found.get("down_revision")))
    heads = revisions - parents
    return sorted(heads)


def _string_literal(node: object) -> str | None:
    import ast

    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value
    return None


def _down_revisions(node: object) -> list[str]:
    import ast

    if node is None or (isinstance(node, ast.Constant) and node.value is None):
        return []
    single = _string_literal(node)
    if single is not None:
        return [single]
    if isinstance(node, (ast.Tuple, ast.List)):
        found: list[str] = []
        for item in node.elts:
            text = _string_literal(item)
            if text:
                found.append(text)
        return found
    return []


def _blank_check(result: str = "pending") -> dict[str, object]:
    return {"result": result, "reason": None, "at": None}


def _machine_shell(line_id: str, risk: bool) -> dict[str, object]:
    """The fixture's machine keys. ``handoff`` stays null in this round."""
    return {
        "task_id": line_id,
        "stage": "dag_ready",
        "risk_bearing": risk,
        "author_provider": None,
        "developer_provider": None,
        "primary_reviewer": None,
        "secondary_reviewer": None,
        "primary_verdict": None,
        "primary_passed": None,
        "secondary_passed": None,
        "secondary_skipped": None,
        "fix_rounds": 0,
        "blocking_reviewer": None,
        "blocking_rounds": 0,
        "return_stage": None,
        "handoff": None,
        "handoff_ref": None,
        "blocked_from": None,
        "replan_options": [],
        "contributors": [],
        "conflict_reviewer": None,
    }


def record_stale_result(dispatch: Dispatch) -> None:
    """Append ``{kind: stale_result}`` on the scheduler audit only."""
    _append_audit(dispatch, STALE_RESULT)


def record_stale_drive(dispatch: Dispatch) -> None:
    """Append ``{kind: stale_drive}`` on the scheduler audit only."""
    _append_audit(dispatch, STALE_DRIVE)


def record_stale_fix_intent(dispatch: Dispatch) -> None:
    """Append ``{kind: stale_fix_intent}`` on the scheduler audit only."""
    _append_audit(dispatch, STALE_FIX_INTENT)


def drive_epoch(dispatch: Dispatch) -> int:
    """The row's epoch. A missing value is 0."""
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    raw = gates.get("epoch", 0)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw


def bump_epoch(dispatch: Dispatch) -> int:
    """Advance the epoch inside the caller's mutation. Returns the new value."""
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    dispatch.gates = gates
    value = drive_epoch(dispatch) + 1
    gates["epoch"] = value
    return value


def _append_audit(dispatch: Dispatch, kind: str) -> None:
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    dispatch.gates = gates
    scheduler = gates.get("scheduler")
    if not isinstance(scheduler, dict):
        scheduler = {}
        gates["scheduler"] = scheduler
    audit = scheduler.get("audit")
    if not isinstance(audit, list):
        audit = []
        scheduler["audit"] = audit
    audit.append({"kind": kind})


#: How many shadow refusals the scheduler audit keeps; older ones drop off.
_SHADOW_AUDIT_LIMIT = 200


def _append_shadow_audit(
    dispatch: Dispatch, line: Line, entry: dict[str, object], attempt: int
) -> None:
    """Record a refusal a shadow-mode check did not make (N58)."""
    gates = dispatch.gates if isinstance(dispatch.gates, dict) else {}
    dispatch.gates = gates
    scheduler = gates.get("scheduler")
    if not isinstance(scheduler, dict):
        scheduler = {}
        gates["scheduler"] = scheduler
    audit = scheduler.get("audit")
    if not isinstance(audit, list):
        audit = []
        scheduler["audit"] = audit
    audit.append(
        {
            "kind": SHADOW_REFUSAL,
            "check": entry.get("check"),
            "line_id": line.id,
            "attempt": attempt,
            "would_refuse": entry.get("would_refuse"),
        }
    )
    shadows = [
        index
        for index, item in enumerate(audit)
        if isinstance(item, dict) and item.get("kind") == SHADOW_REFUSAL
    ]
    for index in reversed(shadows[:-_SHADOW_AUDIT_LIMIT]):
        del audit[index]


def _line_shell(line: Line, risk: bool) -> dict[str, object]:
    return {
        "line_id": line.id,
        "phase": "dag_ready",
        "risk_bearing": risk,
        "author": None,
        "primary_reviewer": None,
        "secondary_reviewer": None,
        "fix_round": 0,
        "blocking_rounds": 0,
        "replans": 0,
        "replanned_from": None,
        "reason": None,
        "updated_at": None,
        "gates": {
            "G0": _blank_check(),
            "D1": _blank_check(),
            "G2": {"result": "pending", "reason": None, "at": None, "commands": []},
            "primary_review": None,
            "secondary_review": None,
            "G5": _blank_check(),
            "G6": _blank_check(),
        },
        "history": [],
        "handoff_ref": None,
        "events": [],
        "machine": _machine_shell(line.id, risk),
    }


class GateRunner:
    """Runs G2 and G5 and writes the snapshot the API returns."""

    def __init__(
        self,
        settings: GateSettings,
        *,
        commands: CommandRunner | None = None,
        worktrees: WorktreeManager | None = None,
        clock: Clock | None = None,
        revisions: Callable[[Line], str | None] | None = None,
        busy: Callable[[str, str], contextlib.AbstractContextManager[object]]
        | None = None,
    ) -> None:
        self._settings = settings
        # N64: ``cond restart --drain`` waits for G2 and G5 runs through
        # busy markers. The live service passes ``activity.busy``; anything
        # else (tests, dev) writes none.
        self._busy = busy or (lambda kind, detail: contextlib.nullcontext())
        self._worktrees_dir = (
            Path(settings.worktrees_dir)
            if settings.worktrees_dir
            else Path(settings.runs_dir).parent / "dispatch-worktrees"
        )
        repo_root = Path(settings.repo_root).resolve()
        runs_dir = Path(settings.runs_dir).resolve()
        worktrees_root = self._worktrees_dir.resolve()
        if worktrees_root == repo_root or repo_root in worktrees_root.parents:
            raise ValueError("dispatch worktree root must be outside repo_root")
        if (
            worktrees_root == runs_dir
            or worktrees_root in runs_dir.parents
            or runs_dir in worktrees_root.parents
        ):
            raise ValueError("dispatch worktrees and run artifacts must be disjoint")
        self._commands = commands or SubprocessCommandRunner()
        self._worktrees = worktrees or GitWorktreeManager()
        trash_root = self._worktrees_dir / worktree_trash.TRASH_DIRNAME
        if isinstance(self._worktrees, GitWorktreeManager) and self._worktrees.trash_root is None:
            self._worktrees.trash_root = trash_root
        # Once per process, off the event loop (a GateRunner per dispatch).
        worktree_trash.sweep_trash_once(trash_root)
        self._clock = clock or _utc_now
        self._globs = tuple(settings.risk_globs) or DEFAULT_RISK_GLOBS
        self._revisions = revisions or (
            lambda line: git_revision(self._settings.repo_root, line.branch)
        )
        # The structural records the gate read from the worktree, waiting for
        # the settle that writes them onto the G2 cell. The key carries the
        # dispatch, the line and the attempt, so two dispatches that share a
        # line id cannot pick up each other's record.
        self._pending_findings: dict[
            tuple[str, str, int], dict[str, object]
        ] = {}

    def stamp(self) -> str:
        """UTC timestamp the stage machine puts on each event."""
        return self._now()

    def line_snapshot(self, dispatch: Dispatch, line: Line) -> dict[str, object]:
        return self._ensure_line(dispatch, line)

    def beat(self, dispatch: Dispatch) -> None:
        """Record that the scheduler is alive. The API recomputes ``healthy``.

        A beat updates ``heartbeat_at`` and ``last_seen`` only. It does not
        move ``deadline_at``.
        """
        sched = self._scheduler(dispatch)
        now = self._now()
        sched["heartbeat_at"] = now
        sched["last_seen"] = now
        sched["seq"] = int(sched.get("seq") or 0) + 1
        sched["threshold_seconds"] = self._settings.heartbeat_threshold_seconds

    def heartbeat_due(self, dispatch: Dispatch) -> bool:
        """True when the saved heartbeat is older than the interval, or missing."""
        interval = self._settings.heartbeat_interval_seconds
        sched = dispatch.gates.get("scheduler") if isinstance(dispatch.gates, dict) else None
        raw = sched.get("heartbeat_at") if isinstance(sched, dict) else None
        if not isinstance(raw, str) or not raw:
            return True
        try:
            seen = datetime.fromisoformat(raw)
        except ValueError:
            return True
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        elapsed = self._clock() - seen.astimezone(timezone.utc)
        if interval <= 0:
            return True
        return elapsed.total_seconds() >= interval

    def arm_watch(self, dispatch: Dispatch, seconds: float) -> bool:
        """Arm the stage deadline once. A later call does not move it.

        ``seconds`` is the poll interval and is not the deadline. The deadline
        is ``watchdog_seconds`` from the clock at the first arm. A non-positive
        watchdog records nothing: ``watch_expired`` fires immediately. Returns
        True when a new deadline was written and the caller should persist it.
        """
        del seconds
        if self._settings.watchdog_seconds <= 0:
            return False
        sched = self._scheduler(dispatch)
        current = sched.get("deadline_at")
        if isinstance(current, str) and current:
            return False
        deadline = self._clock() + timedelta(seconds=self._settings.watchdog_seconds)
        sched["deadline_at"] = isoformat_utc(deadline)
        sched["deadline_reason"] = "watchdog"
        return True

    def clear_watch(self, dispatch: Dispatch) -> None:
        """Drop the deadline so the next wait can arm once."""
        sched = dispatch.gates.get("scheduler") if isinstance(dispatch.gates, dict) else None
        if not isinstance(sched, dict):
            return
        sched["deadline_at"] = None
        sched["deadline_reason"] = None

    def watch_expired(self, dispatch: Dispatch) -> bool:
        """True when the stage deadline is behind the clock.

        A non-positive watchdog expires without a deadline. Otherwise the check
        is ``now > deadline`` and does not depend on the last heartbeat.
        """
        if self._settings.watchdog_seconds <= 0:
            return True
        sched = dispatch.gates.get("scheduler") if isinstance(dispatch.gates, dict) else None
        if not isinstance(sched, dict):
            return False
        raw = sched.get("deadline_at")
        if not isinstance(raw, str) or not raw:
            return False
        try:
            deadline = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return self._clock() > deadline.astimezone(timezone.utc)

    def push_wakeup(
        self,
        dispatch: Dispatch,
        event_id: str,
        kind: str,
        line_id: str,
        *,
        run_id: str | None = None,
        attempt: int | None = None,
    ) -> bool:
        """Queue one durable event. The same id a second time is a no-op.

        ``run_id`` and ``attempt`` identify the worker the event belongs to.
        A later consume ignores the event when that triple does not match.
        """
        sched = self._scheduler(dispatch)
        handled = sched.setdefault("handled", [])
        pending = sched.setdefault("pending", [])
        assert isinstance(handled, list) and isinstance(pending, list)
        if event_id in handled or any(
            isinstance(item, dict) and item.get("id") == event_id for item in pending
        ):
            return False
        pending.append(
            {
                "id": event_id,
                "kind": kind,
                "dispatch_id": dispatch.id,
                "line_id": line_id,
                "run_id": run_id,
                "attempt": attempt,
                "at": self._now(),
            }
        )
        return True

    def event_matches(self, line: Line, item: object) -> bool:
        """True when the event is the line's current ``(line_id, run_id, attempt)``."""
        if not isinstance(item, dict):
            return False
        if str(item.get("line_id") or "") != line.id:
            return False
        run_id = item.get("run_id")
        if line.current_run:
            if not isinstance(run_id, str) or run_id != line.current_run:
                return False
        elif isinstance(run_id, str) and run_id:
            return False
        if item.get("attempt") is None:
            return False
        try:
            attempt = int(item["attempt"])
        except (TypeError, ValueError):
            return False
        return attempt == line.attempts

    def note_stale_event(self, dispatch: Dispatch) -> None:
        """Record the reason code only. The event payload stays out of the audit."""
        sched = self._scheduler(dispatch)
        audit = sched.setdefault("audit", [])
        if not isinstance(audit, list):
            audit = []
            sched["audit"] = audit
        audit.append({"kind": STALE_EVENT})

    def note_stale_result(self, dispatch: Dispatch) -> None:
        """Record a discarded result. The payload stays out of the audit."""
        record_stale_result(dispatch)

    def peek_wakeups(self, dispatch: Dispatch) -> list[dict[str, object]]:
        """Copy pending events. The caller persists its own fact before acking."""
        sched = self._scheduler(dispatch)
        pending = sched.get("pending")
        if not isinstance(pending, list):
            return []
        return [dict(item) for item in pending if isinstance(item, dict)]

    def mark_run_settled(self, dispatch: Dispatch, line: Line) -> None:
        """The worker finished. Saved before the wakeup is marked handled.

        The fact is ``{line_id, run_id, attempt}`` on the scheduler. A line
        snapshot key would not match the shared fixture. The previous fact for
        this line, including a legacy line-id string, is replaced.
        """
        sched = self._scheduler(dispatch)
        settled = sched.get("settled_lines")
        if not isinstance(settled, list):
            settled = []
        kept: list[object] = []
        for item in settled:
            if item == line.id:
                continue
            if isinstance(item, dict) and item.get("line_id") == line.id:
                continue
            kept.append(item)
        kept.append(
            {
                "dispatch_id": dispatch.id,
                "line_id": line.id,
                "run_id": line.current_run,
                "attempt": line.attempts,
            }
        )
        sched["settled_lines"] = kept

    def clear_run_settled(self, dispatch: Dispatch, line: Line) -> None:
        """A new launch has to be polled. The previous completion does not apply."""
        gates = dispatch.gates if isinstance(dispatch.gates, dict) else None
        sched = gates.get("scheduler") if isinstance(gates, dict) else None
        settled = sched.get("settled_lines") if isinstance(sched, dict) else None
        if not isinstance(settled, list):
            return
        sched["settled_lines"] = [
            item
            for item in settled
            if item != line.id and not (isinstance(item, dict) and item.get("line_id") == line.id)
        ]

    def run_is_settled(self, dispatch: Dispatch, line: Line) -> bool:
        """True for this run's triple, or for a legacy line-id string."""
        gates = dispatch.gates if isinstance(dispatch.gates, dict) else None
        sched = gates.get("scheduler") if isinstance(gates, dict) else None
        settled = sched.get("settled_lines") if isinstance(sched, dict) else None
        if not isinstance(settled, list):
            return False
        for item in settled:
            if item == line.id:
                return True
            if not isinstance(item, dict) or item.get("line_id") != line.id:
                continue
            if item.get("run_id") == line.current_run and item.get("attempt") == line.attempts:
                return True
        return False

    def ack_wakeup(self, dispatch: Dispatch, event_id: str) -> None:
        """Mark one id handled. The completion fact is already on the scheduler."""
        if not event_id:
            return
        sched = self._scheduler(dispatch)
        pending = sched.get("pending")
        if isinstance(pending, list):
            sched["pending"] = [
                item
                for item in pending
                if not (isinstance(item, dict) and item.get("id") == event_id)
            ]
        handled = sched.setdefault("handled", [])
        if isinstance(handled, list) and event_id not in handled:
            handled.append(event_id)
            if len(handled) > 200:
                del handled[:-200]

    def pull_wakeups(self, dispatch: Dispatch) -> list[dict[str, object]]:
        """Take pending events once. A second pull of the same id returns nothing."""
        sched = self._scheduler(dispatch)
        pending = sched.get("pending")
        sched["pending"] = []
        handled = sched.setdefault("handled", [])
        assert isinstance(handled, list)
        fresh: list[dict[str, object]] = []
        if not isinstance(pending, list):
            return fresh
        for item in pending:
            if not isinstance(item, dict):
                continue
            event_id = str(item.get("id") or "")
            if not event_id or event_id in handled:
                continue
            handled.append(event_id)
            fresh.append(dict(item))
        if len(handled) > 200:
            del handled[:-200]
        return fresh

    def handoff_facts(self, dispatch: Dispatch, line: Line, *, limitations: str) -> dict[str, object]:
        """Branch, commit, files, tests and results. No model text."""
        current_commit = git_revision(self._settings.repo_root, line.branch or "") or ""
        commit, base = self._handoff_revision_inputs(dispatch, line, current_commit)
        files = (
            git_changed_paths(self._settings.repo_root, commit, base)
            if commit != "unknown"
            else None
        )
        return self._handoff_facts_from_git(
            dispatch, line, limitations, commit, files
        )

    async def handoff_facts_async(
        self, dispatch: Dispatch, line: Line, *, limitations: str
    ) -> dict[str, object]:
        """Read Git off-thread, then build the handoff on the actor thread."""
        # Preserve explicit test/integration adapters installed on this runner.
        if "handoff_facts" in self.__dict__:
            return self.handoff_facts(
                dispatch, line, limitations=limitations
            )
        current_commit = await asyncio.to_thread(
            git_revision, self._settings.repo_root, line.branch or ""
        ) or ""
        commit, base = self._handoff_revision_inputs(dispatch, line, current_commit)
        files = (
            await asyncio.to_thread(
                git_changed_paths, self._settings.repo_root, commit, base
            )
            if commit != "unknown"
            else None
        )
        return self._handoff_facts_from_git(
            dispatch, line, limitations, commit, files
        )

    def _handoff_revision_inputs(
        self, dispatch: Dispatch, line: Line, current_commit: str
    ) -> tuple[str, str]:
        base = line.base_commit or "HEAD"
        snap = self._line(dispatch, line.id)
        pinned_commit = ""
        has_g2_record = False
        if isinstance(snap, dict):
            gates = snap.get("gates")
            g2 = gates.get("G2") if isinstance(gates, dict) else None
            if isinstance(g2, dict):
                has_g2_record = True
                revision = g2.get("revision")
                if isinstance(revision, str):
                    pinned_commit = revision
        return pinned_commit or ("unknown" if has_g2_record else current_commit), base

    def _handoff_facts_from_git(
        self,
        dispatch: Dispatch,
        line: Line,
        limitations: str,
        commit: str,
        files: tuple[str, ...] | None,
    ) -> dict[str, object]:
        branch = line.branch or ""
        snap = self._line(dispatch, line.id)
        results = "none"
        test_command = ""
        if isinstance(snap, dict):
            gates = snap.get("gates")
            g2 = gates.get("G2") if isinstance(gates, dict) else None
            if isinstance(g2, dict):
                recorded = g2.get("commands")
                parts: list[str] = []
                command_displays: list[str] = []
                if isinstance(recorded, list):
                    for command in recorded:
                        if isinstance(command, dict):
                            parts.append(
                                f"{command.get('index')}:{command.get('exit_code')}"
                            )
                            display = command.get("safe_command")
                            digest = command.get("safe_command_sha256")
                            if isinstance(display, str) and isinstance(digest, str):
                                command_displays.append(f"{display} [safe-display-sha256:{digest}]")
                if parts:
                    results = ", ".join(parts)
                elif g2.get("reason"):
                    results = str(g2.get("reason"))
                test_command = " && ".join(command_displays)
                if not test_command:
                    if isinstance(recorded, list) and recorded:
                        test_command = "Recorded G2 command text unavailable (legacy record)"
                    elif g2.get("result") == "skipped":
                        test_command = (
                            "No focused test command was run "
                            f"(G2 skipped: {g2.get('reason') or 'unspecified'})"
                        )
                    else:
                        test_command = "Recorded G2 command text unavailable"
        known_limitations = limitations or "none"
        if files is None:
            known_limitations = (
                f"{known_limitations}; changed-file Git evidence is unavailable"
                if known_limitations != "none"
                else "changed-file Git evidence is unavailable"
            )
        return {
            "branch": branch,
            "commit": commit,
            "files": files,
            "test_command": test_command,
            "results": results,
            "known_limitations": known_limitations,
        }

    def record_handoff_view(self, dispatch: Dispatch, line: Line, facts: dict[str, object]) -> None:
        """Redacted handoff fields for evidence. The machine handoff stays null."""
        snap = self._ensure_line(dispatch, line)
        gates = snap.get("gates")
        if not isinstance(gates, dict):
            return
        files = facts.get("files") or ()
        gates["handoff"] = {
            "branch": facts.get("branch") or "",
            "commit": facts.get("commit") or "",
            "files": list(files) if isinstance(files, tuple) else list(files or []),
            "test_command": facts.get("test_command") or "",
            "results": facts.get("results") or "",
            "blocker_count": facts.get("blocker_count") or 0,
        }

    def _scheduler(self, dispatch: Dispatch) -> dict[str, object]:
        root = self._ensure_root(dispatch)
        current = root.get("scheduler")
        if not isinstance(current, dict):
            current = {
                "heartbeat_at": None,
                "seq": 0,
                "threshold_seconds": self._settings.heartbeat_threshold_seconds,
                "deadline_at": None,
                "deadline_reason": None,
                "pending": [],
                "handled": [],
            }
            root["scheduler"] = current
        return current

    def prepare(self, dispatch: Dispatch) -> None:
        """Reject a plan that is not a DAG, then record G0 and D1."""
        topological_lines(dispatch.lines)
        root = self._ensure_root(dispatch)
        base_commit = root.get("base_commit")
        if not isinstance(base_commit, str) or not base_commit:
            # N62: fetched and compared with the base ref, not the bare HEAD.
            base_commit, check = check_base(self._settings.repo_root)
            if check is not None:
                root[BASE_CHECK_KEY] = check
            if base_commit:
                root["base_commit"] = base_commit
        for line in dispatch.lines:
            if not line.base_commit:
                line.base_commit = base_commit
            snap = self._ensure_line(dispatch, line)
            gates = snap["gates"]
            assert isinstance(gates, dict)
            moment = self._now()
            for name in ("G0", "D1"):
                current = gates.get(name)
                if isinstance(current, dict) and current.get("result") == "pass":
                    continue
                gates[name] = {
                    "result": "pass",
                    "reason": PLANNER_OUTPUT_VALID,
                    "at": moment,
                }
                self._event(
                    snap,
                    gate=name,
                    result="pass",
                    reason=PLANNER_OUTPUT_VALID,
                    to_stage="dag_ready",
                )
            snap["phase"] = "dag_ready"
            snap["reason"] = PLANNER_OUTPUT_VALID
            self._touch(dispatch, snap, "dag_ready")

    async def prepare_async(self, dispatch: Dispatch) -> None:
        """Prepare gates without blocking the async dispatch actor on Git."""
        topological_lines(dispatch.lines)
        root = self._ensure_root(dispatch)
        base_commit = root.get("base_commit")
        if not isinstance(base_commit, str) or not base_commit:
            # N62: fetched and compared with the base ref, not the bare HEAD.
            base_commit, check = await asyncio.to_thread(
                check_base, self._settings.repo_root
            )
            if check is not None:
                root[BASE_CHECK_KEY] = check
        self._prepare_with_base(dispatch, base_commit)

    def _prepare_with_base(self, dispatch: Dispatch, base_commit: str) -> None:
        root = self._ensure_root(dispatch)
        if base_commit and not root.get("base_commit"):
            root["base_commit"] = base_commit
        for line in dispatch.lines:
            if not line.base_commit:
                line.base_commit = base_commit
            snap = self._ensure_line(dispatch, line)
            gates = snap["gates"]
            assert isinstance(gates, dict)
            moment = self._now()
            for name in ("G0", "D1"):
                current = gates.get(name)
                if isinstance(current, dict) and current.get("result") == "pass":
                    continue
                gates[name] = {
                    "result": "pass",
                    "reason": PLANNER_OUTPUT_VALID,
                    "at": moment,
                }
                self._event(
                    snap,
                    gate=name,
                    result="pass",
                    reason=PLANNER_OUTPUT_VALID,
                    to_stage="dag_ready",
                )
            snap["phase"] = "dag_ready"
            snap["reason"] = PLANNER_OUTPUT_VALID
            self._touch(dispatch, snap, "dag_ready")

    def allows_review(self, dispatch: Dispatch, line_id: str) -> bool:
        """G2 passed for this attempt and revision, or there was nothing to run."""
        return self._g2_reusable(dispatch, line_id, ("pass", "skipped"))

    def settled(self, dispatch: Dispatch, line_id: str) -> bool:
        """A G2 result still describes the attempt and revision under test."""
        return self._g2_reusable(dispatch, line_id, ("pass", "skipped", "fail"))

    async def settled_async(self, dispatch: Dispatch, line_id: str) -> bool:
        return await self._g2_reusable_async(
            dispatch, line_id, ("pass", "skipped", "fail")
        )

    async def allows_review_async(self, dispatch: Dispatch, line_id: str) -> bool:
        return await self._g2_reusable_async(dispatch, line_id, ("pass", "skipped"))

    def invalidate_g2(self, dispatch: Dispatch, line: Line, *, force: bool = False) -> bool:
        """Drop a G2 result that does not belong to the launch about to start.

        A failure is always dropped: the line is being launched again. A pass
        or a skip is dropped when the attempt or the commit changed. The same
        attempt at the same commit keeps its result, unless ``force`` is set
        because a fix round changed the work and G2 has to run again.
        """
        snap = self._line(dispatch, line.id)
        if snap is None:
            return False
        gates = snap.get("gates")
        if not isinstance(gates, dict):
            return False
        current = gates.get("G2")
        if not isinstance(current, dict):
            return False
        result = current.get("result")
        if result not in ("pass", "skipped", "fail"):
            return False
        if not force and result != "fail" and self._g2_bound(dispatch, line):
            return False
        moment = self._now()
        launched = current.get("fix_launches") if isinstance(current, dict) else 0
        gates["G2"] = {
            "result": "pending",
            "reason": None,
            "at": moment,
            "commands": [],
            "attempt": line.attempts,
            "revision": self._revision(line),
            "fix_launches": launched or 0,
        }
        snap["phase"] = "dag_ready"
        snap["reason"] = PLANNER_OUTPUT_VALID
        self._touch(dispatch, snap, "dag_ready")
        return True

    async def invalidate_g2_async(
        self, dispatch: Dispatch, line: Line, *, force: bool = False
    ) -> bool:
        snap = self._line(dispatch, line.id)
        if snap is None:
            return False
        gates = snap.get("gates")
        if not isinstance(gates, dict):
            return False
        current = gates.get("G2")
        if not isinstance(current, dict):
            return False
        result = current.get("result")
        if result not in ("pass", "skipped", "fail"):
            return False
        if not force and result != "fail" and await self._g2_bound_async(dispatch, line):
            return False
        revision = await asyncio.to_thread(self._revision, line)
        moment = self._now()
        launched = current.get("fix_launches") or 0
        gates["G2"] = {
            "result": "pending",
            "reason": None,
            "at": moment,
            "commands": [],
            "attempt": line.attempts,
            "revision": revision,
            "fix_launches": launched,
        }
        snap["phase"] = "dag_ready"
        snap["reason"] = PLANNER_OUTPUT_VALID
        self._touch(dispatch, snap, "dag_ready")
        return True

    def mark_g2_running(self, dispatch: Dispatch, line: Line) -> None:
        """Persist that G2 is in progress before the command starts.

        A crash after this save leaves a result a new machine will run again.
        A result that still matches this attempt and commit is left as it is.
        """
        if self._g2_reusable(dispatch, line.id, ("pass", "skipped", "fail")):
            return
        snap = self._ensure_line(dispatch, line)
        gates = snap["gates"]
        assert isinstance(gates, dict)
        moment = self._now()
        previous = gates.get("G2")
        launched = previous.get("fix_launches") if isinstance(previous, dict) else 0
        gates["G2"] = {
            "result": "running",
            "reason": None,
            "at": moment,
            "commands": [],
            "attempt": line.attempts,
            "revision": self._revision(line),
            "fix_launches": launched or 0,
        }
        snap["phase"] = "focused_tests"
        self._touch(dispatch, snap, "focused_tests")

    async def mark_g2_running_async(self, dispatch: Dispatch, line: Line) -> None:
        if await self._g2_reusable_async(
            dispatch, line.id, ("pass", "skipped", "fail")
        ):
            return
        revision = await asyncio.to_thread(self._revision, line)
        snap = self._ensure_line(dispatch, line)
        gates = snap["gates"]
        assert isinstance(gates, dict)
        previous = gates.get("G2")
        launched = previous.get("fix_launches") if isinstance(previous, dict) else 0
        gates["G2"] = {
            "result": "running",
            "reason": None,
            "at": self._now(),
            "commands": [],
            "attempt": line.attempts,
            "revision": revision,
            "fix_launches": launched or 0,
        }
        snap["phase"] = "focused_tests"
        self._touch(dispatch, snap, "focused_tests")

    def integration_phase(self, dispatch: Dispatch) -> str:
        integration = self._integration(dispatch)
        phase = integration.get("phase") if isinstance(integration, dict) else None
        return str(phase or "pending")

    def merged_ids(self, dispatch: Dispatch) -> list[str]:
        integration = self._integration(dispatch)
        merged = (
            integration.get("merged_lines") if isinstance(integration, dict) else None
        )
        if not isinstance(merged, list):
            return []
        return [str(item) for item in merged]

    def mark_merging(self, dispatch: Dispatch) -> None:
        integration = self._integration(dispatch)
        integration["phase"] = "merging"
        integration["branch"] = dispatch.integrate_branch
        integration["at"] = self._now()
        self._root(dispatch)["updated_at"] = integration["at"]

    def note_merged(self, dispatch: Dispatch, line_id: str) -> None:
        integration = self._integration(dispatch)
        merged = integration.setdefault("merged_lines", [])
        if isinstance(merged, list) and line_id not in merged:
            merged.append(line_id)

    def mark_merge_conflict(self, dispatch: Dispatch) -> None:
        integration = self._integration(dispatch)
        moment = self._now()
        integration["phase"] = "failed"
        integration["reason"] = MERGE_CONFLICT
        integration["at"] = moment
        self._root(dispatch)["updated_at"] = moment

    def mark_running(self, dispatch: Dispatch) -> None:
        integration = self._integration(dispatch)
        moment = self._now()
        integration["phase"] = "running"
        integration["branch"] = dispatch.integrate_branch
        integration["at"] = moment
        self._root(dispatch)["updated_at"] = moment
        for line in dispatch.lines:
            if line.status.value == "FAILED":
                continue
            snap = self._ensure_line(dispatch, line)
            snap["phase"] = "integration_checks"
            self._touch(dispatch, snap, "integration_checks")

    def mark_awaiting_human(self, dispatch: Dispatch) -> None:
        moment = self._now()
        for line in dispatch.lines:
            if line.status.value == "FAILED":
                continue
            snap = self._ensure_line(dispatch, line)
            gates = snap["gates"]
            assert isinstance(gates, dict)
            gates["G6"] = {"result": "pending", "reason": AWAITING_HUMAN, "at": moment}
            snap["phase"] = "awaiting_human"
            snap["reason"] = G5_PASSED
            self._event(
                snap,
                gate="G6",
                result="pending",
                reason=AWAITING_HUMAN,
                to_stage="awaiting_human",
            )
            self._touch(dispatch, snap, "awaiting_human")
        approval = self._approval(dispatch)
        approval["result"] = "pending"
        approval["token_present"] = bool(dispatch.approval_token)
        approval["at"] = moment
        self._root(dispatch)["updated_at"] = moment

    def mark_approved(self, dispatch: Dispatch) -> None:
        moment = self._now()
        for line in dispatch.lines:
            snap = self._line(dispatch, line.id)
            if snap is None:
                continue
            gates = snap["gates"]
            assert isinstance(gates, dict)
            gates["G6"] = {"result": "pass", "reason": HUMAN_APPROVED, "at": moment}
            snap["phase"] = "merged"
            snap["reason"] = HUMAN_APPROVED
            self._touch(dispatch, snap, "merged")
        approval = self._approval(dispatch)
        approval["result"] = "pass"
        approval["token_present"] = bool(dispatch.approval_token)
        approval["at"] = moment
        self._root(dispatch)["updated_at"] = moment

    def mark_rejected(self, dispatch: Dispatch) -> None:
        moment = self._now()
        for line in dispatch.lines:
            snap = self._line(dispatch, line.id)
            if snap is None:
                continue
            gates = snap["gates"]
            assert isinstance(gates, dict)
            gates["G6"] = {"result": "fail", "reason": HUMAN_REJECTED, "at": moment}
            snap["phase"] = "blocked"
            snap["reason"] = HUMAN_REJECTED
            self._touch(dispatch, snap, "blocked")
        approval = self._approval(dispatch)
        approval["result"] = "fail"
        approval["token_present"] = bool(dispatch.approval_token)
        approval["at"] = moment
        self._root(dispatch)["updated_at"] = moment

    async def _g2_settle(
        self,
        finish: Callable[..., Awaitable[GateStep]] | None,
        dispatch: Dispatch,
        line: Line,
        *,
        result: str,
        reason: str,
        recorded: list[dict[str, object]],
        attempt: int,
        revision: str | None,
    ) -> GateStep:
        """Workers pass ``finish`` so the drive task applies the snapshot.

        The structural records the gate read from the worktree travel on the
        runner itself, so the settle that writes the snapshot needs no extra
        argument and the caller's signature stays as it was.
        """
        if finish is None:
            return self._finish_g2(
                dispatch,
                line,
                result=result,
                reason=reason,
                recorded=recorded,
                attempt=attempt,
                revision=revision,
            )
        return await finish(result, reason, recorded, attempt, revision)

    async def _g5_settle(
        self,
        finish: Callable[..., Awaitable[GateStep]] | None,
        dispatch: Dispatch,
        *,
        full: dict[str, object],
        alembic: dict[str, object],
        reason: str,
        check: str,
    ) -> GateStep:
        if finish is None:
            return self._finish_g5(
                dispatch,
                full=full,
                alembic=alembic,
                reason=reason,
                check=check,
            )
        return await finish(full, alembic, reason, check)

    async def run_g2(
        self,
        dispatch: Dispatch,
        line: Line,
        *,
        persist: Callable[[], Awaitable[None]] | None = None,
        note_target: Callable[[int, str | None], Awaitable[None]] | None = None,
        finish: Callable[..., Awaitable[GateStep]] | None = None,
    ) -> GateStep:
        """G2 for one line, under a busy marker a drained restart waits for."""
        with self._busy("g2", f"{dispatch.id}:{line.id}"):
            return await self._run_g2(
                dispatch, line, persist=persist, note_target=note_target, finish=finish
            )

    async def _run_g2(
        self,
        dispatch: Dispatch,
        line: Line,
        *,
        persist: Callable[[], Awaitable[None]] | None = None,
        note_target: Callable[[int, str | None], Awaitable[None]] | None = None,
        finish: Callable[..., Awaitable[GateStep]] | None = None,
    ) -> GateStep:
        """Focused tests for one line. A failure is terminal in this round.

        The attempt and the worktree HEAD are fixed before the commands run.
        The result records those values. It does not read ``line`` again at
        the end. If the branch moves while the commands run, the result is
        discarded and G2 runs once more on a new worktree. A second move
        fails closed and does not allow review.

        Two structural checks run after the commands, while the worktree
        still exists. The test run must not have left changes outside a
        temporary directory (N47), and the line's diff must fit its write
        scope (N52). A line whose tests passed but which broke either rule
        does not reach review.
        """
        self._ensure_root(dispatch)
        pinned_attempt = line.attempts
        # A record left by an earlier round of this line would be read as
        # this round's finding, so it goes before anything else happens.
        self._pending_findings.pop((dispatch.id, line.id, pinned_attempt), None)
        pinned_revision = await asyncio.to_thread(self._revision, line)
        risk = line_is_risk_bearing(line, self._globs)
        commands = acceptance_commands(line, self._settings.g2_command)
        if commands is None:
            return await self._g2_settle(
                finish,
                dispatch,
                line,
                result="fail",
                reason=FOCUSED_TESTS_FAILED,
                recorded=[],
                attempt=pinned_attempt,
                revision=pinned_revision,
            )
        if not commands:
            if risk:
                return await self._g2_settle(
                finish,
                    dispatch,
                    line,
                    result="fail",
                    reason=G2_NO_COMMANDS_RISK_BEARING,
                    recorded=[],
                    attempt=pinned_attempt,
                    revision=pinned_revision,
                )
            return await self._g2_settle(
                finish,
                dispatch,
                line,
                result="skipped",
                reason=G2_NO_COMMANDS,
                recorded=[],
                attempt=pinned_attempt,
                revision=pinned_revision,
            )
        # Logs stay at gates/<id>/<line>/. The worktree is a sibling so
        # ``git worktree remove`` cannot delete the log the snapshot names.
        worktree = self._gate_worktree_path(dispatch, "line", line.id)
        recorded: list[dict[str, object]] = []
        failure: str | None = None
        tested = pinned_revision
        for round_index in range(2):
            try:
                await self._worktrees.add_detached(
                    self._settings.repo_root, worktree, line.branch or "HEAD"
                )
            except Exception as exc:
                logger.warning(
                    "G2 worktree add failed on %s: %s", line.id, type(exc).__name__
                )
                return await self._g2_settle(
                finish,
                    dispatch,
                    line,
                    result="fail",
                    reason=FOCUSED_TESTS_FAILED,
                    recorded=[],
                    attempt=pinned_attempt,
                    revision=tested,
                )
            tested = await asyncio.to_thread(git_head, worktree) or pinned_revision
            # Whether the worktree is a Git checkout is read before the
            # commands run, so a test cannot make the gate skip its own
            # check by removing Git's bookkeeping.
            is_checkout = _is_git_checkout(worktree)
            if note_target is None:
                self._note_g2_target(dispatch, line, pinned_attempt, tested)
            else:
                await note_target(pinned_attempt, tested)
            recorded = []
            failure = None
            findings: dict[str, object] = {}
            try:
                # Persist sits with the commands: cancel or a failed save still
                # runs the finally that removes the worktree, and no command
                # starts unless the save returned.
                if persist is not None:
                    await persist()
                for index, argv in enumerate(commands):
                    safe_command, safe_command_sha256 = _safe_command_record(argv)
                    log_ref = f"gates/{dispatch.id}/{line.id}/g2-0-{index}.log"
                    log_path = self._settings.runs_dir / log_ref
                    try:
                        result = await self._commands.run(
                            argv,
                            cwd=worktree,
                            timeout=self._settings.g2_timeout_seconds,
                            log_path=log_path,
                        )
                    except Exception as exc:
                        logger.warning(
                            "G2 command could not start: %s", type(exc).__name__
                        )
                        recorded.append(
                            {
                                "index": index,
                                "safe_command": safe_command,
                                "safe_command_sha256": safe_command_sha256,
                                "exit_code": 127,
                                "duration_ms": 0,
                                "log_ref": log_ref,
                            }
                        )
                        failure = FOCUSED_TESTS_FAILED
                        break
                    recorded.append(
                        {
                            "index": index,
                            "safe_command": safe_command,
                            "safe_command_sha256": safe_command_sha256,
                            "exit_code": result.exit_code,
                            "duration_ms": result.duration_ms,
                            "log_ref": log_ref,
                        }
                    )
                    if result.timed_out:
                        failure = G2_TIMEOUT
                        break
                    if result.exit_code != 0:
                        # A ruff check that only found the base commit's
                        # own lint debt passes; anything else still fails.
                        baseline = await self._g2_lint_baseline(
                            dispatch, line, argv, result, worktree, index
                        )
                        if baseline is not None:
                            recorded[-1]["lint_baseline"] = baseline
                        if baseline is None or baseline.get("passed") is not True:
                            failure = FOCUSED_TESTS_FAILED
                            # N27: does the base commit fail the same way?
                            if baseline is None:
                                failure = await self._g2_preexisting(
                                    dispatch, line, argv, result, log_path,
                                    index, recorded[-1], worktree,
                                )
                            break
                # The two structural checks read the worktree after the
                # commands ran, while it still exists. They run even when a
                # command failed: a test that polluted the checkout is worth
                # reporting next to the failure that stopped it. A check that
                # cannot answer fails the round closed; it never lets the
                # round through unchecked.
                # N58: each check has its own mode, read once per round.
                modes = {
                    # N73 S5/S6: record-only; enforce acts as shadow.
                    name: record_only(check_mode(env), check=name)
                    for name, _c, _r, env in _STRUCTURAL_CHECKS
                }
                try:
                    findings = await asyncio.to_thread(
                        self._structural_findings,
                        line,
                        worktree,
                        tested,
                        is_checkout,
                        worktree_check=modes["dirty_worktree"] != OFF,
                        scope_check=modes["scope_conflict"] != OFF,
                    )
                except Exception as exc:
                    logger.warning(
                        "G2 structural checks failed: %s", type(exc).__name__
                    )
                    # Each check that is on fails closed on its own, so an
                    # enforced scope check does not depend on the worktree
                    # check's mode.
                    findings = {}
                    if modes["dirty_worktree"] != OFF:
                        findings["dirty_worktree"] = _unknown_worktree()
                    if modes["scope_conflict"] != OFF and line.write_scope:
                        findings["scope_conflict"] = _unknown_scope(line.write_scope)
                findings = apply_check_modes(findings, modes)
                # The settle below reads the records from here. The write
                # happens before the settle is awaited, so the record is
                # there when the snapshot is written.
                self._pending_findings[(dispatch.id, line.id, pinned_attempt)] = (
                    findings
                )
            finally:
                try:
                    await self._worktrees.remove(self._settings.repo_root, worktree)
                except Exception as exc:
                    logger.warning(
                        "G2 worktree remove failed: %s", type(exc).__name__
                    )
            current = await asyncio.to_thread(self._revision, line)
            drifted = bool(tested and current and tested != current)
            if drifted and round_index == 0:
                continue
            if drifted:
                return await self._g2_settle(
                finish,
                    dispatch,
                    line,
                    result="fail",
                    reason=G2_REVISION_MOVED,
                    recorded=recorded,
                    attempt=pinned_attempt,
                    revision=tested,
                )
            if failure:
                return await self._g2_settle(
                finish,
                    dispatch,
                    line,
                    result="fail",
                    reason=failure,
                    recorded=recorded,
                    attempt=pinned_attempt,
                    revision=tested,
                )
            # The tests passed, but the worktree they ran in was not clean
            # outside tmp (N47), or the diff does not fit the line's write
            # scope (N52). Both are the gate's own verdict, not a reviewer's
            # prose.
            if findings.get("dirty_worktree") is not None:
                return await self._g2_settle(
                finish,
                    dispatch,
                    line,
                    result="fail",
                    reason=G2_WORKTREE_DIRTY,
                    recorded=recorded,
                    attempt=pinned_attempt,
                    revision=tested,
                )
            if findings.get("scope_conflict") is not None:
                return await self._g2_settle(
                finish,
                    dispatch,
                    line,
                    result="fail",
                    reason=G2_SCOPE_CONFLICT,
                    recorded=recorded,
                    attempt=pinned_attempt,
                    revision=tested,
                )
            # N61 U2: shadow fast judgment, in the background; never changes this outcome.
            with contextlib.suppress(Exception):
                from . import completion_judgment

                completion_judgment.schedule(
                    self._settings, dispatch, line, pinned_attempt, tested, recorded
                )
            return await self._g2_settle(
                finish,
                dispatch,
                line,
                result="pass",
                reason=FOCUSED_TESTS_PASSED,
                recorded=recorded,
                attempt=pinned_attempt,
                revision=tested,
            )
        return await self._g2_settle(
                finish,
            dispatch,
            line,
            result="fail",
            reason=G2_REVISION_MOVED,
            recorded=recorded,
            attempt=pinned_attempt,
            revision=tested,
        )

    async def run_g5(
        self,
        dispatch: Dispatch,
        *,
        finish: Callable[..., Awaitable[GateStep]] | None = None,
    ) -> GateStep:
        """G5 under a busy marker a drained restart waits for."""
        with self._busy("g5", dispatch.id):
            return await self._run_g5(dispatch, finish=finish)

    async def _run_g5(
        self,
        dispatch: Dispatch,
        *,
        finish: Callable[..., Awaitable[GateStep]] | None = None,
    ) -> GateStep:
        """Full-suite command, then one Alembic head, in a detached worktree."""
        self._ensure_root(dispatch)
        worktree = self._gate_worktree_path(dispatch, "integration")
        try:
            await self._worktrees.add_detached(
                self._settings.repo_root,
                worktree,
                dispatch.integrate_branch or "HEAD",
            )
        except WorktreeDirtyError:
            logger.warning("G5 worktree stayed dirty")
            return await self._g5_settle(
                finish,
                dispatch,
                full=self._full_suite(
                    "fail", exit_code=None, duration_ms=0, log_ref=None
                ),
                alembic={"result": "pending", "heads": [], "reason": None},
                reason=G5_WORKTREE_DIRTY,
                check=CHECK_FULL_SUITE,
            )
        except Exception as exc:
            logger.warning("G5 worktree add failed: %s", type(exc).__name__)
            return await self._g5_settle(
                finish,
                dispatch,
                full=self._full_suite(
                    "fail", exit_code=None, duration_ms=0, log_ref=None
                ),
                alembic={"result": "pending", "heads": [], "reason": None},
                reason=G5_FULL_SUITE_FAILED,
                check=CHECK_FULL_SUITE,
            )
        try:
            return await self._g5_inside(dispatch, worktree, finish)
        finally:
            try:
                await self._worktrees.remove(self._settings.repo_root, worktree)
            except Exception as exc:
                logger.warning("G5 worktree remove failed: %s", type(exc).__name__)

    async def _g5_inside(
        self,
        dispatch: Dispatch,
        worktree: Path,
        finish: Callable[..., Awaitable[GateStep]] | None = None,
    ) -> GateStep:
        log_ref = f"gates/{dispatch.id}/g5-full.log"
        log_path = self._settings.runs_dir / log_ref
        command = (self._settings.g5_command or "").strip()
        full = self._full_suite("fail", exit_code=None, duration_ms=0, log_ref=None)
        full_reason: str | None = G5_NOT_CONFIGURED
        if command:
            try:
                argv = shlex.split(command)
            except ValueError:
                logger.warning("G5 command could not be split")
                argv = []
            if argv:
                try:
                    result = await self._commands.run(
                        argv,
                        cwd=worktree,
                        timeout=self._settings.g5_timeout_seconds,
                        log_path=log_path,
                    )
                except Exception as exc:
                    logger.warning("G5 command could not start: %s", type(exc).__name__)
                    full = self._full_suite(
                        "fail", exit_code=127, duration_ms=0, log_ref=log_ref
                    )
                    full_reason = G5_FULL_SUITE_FAILED
                else:
                    full = self._full_suite(
                        "fail" if result.timed_out or result.exit_code != 0 else "pass",
                        exit_code=result.exit_code,
                        duration_ms=result.duration_ms,
                        log_ref=log_ref,
                    )
                    if result.timed_out:
                        full_reason = G5_TIMEOUT
                    elif result.exit_code != 0:
                        full_reason = G5_FULL_SUITE_FAILED
                    else:
                        full_reason = None
        configured = True
        try:
            versions_dir, configured = alembic_versions_dir(worktree)
            heads: list[str] | None = parse_alembic_heads(versions_dir)
        except AlembicUnparseable:
            heads = None
        if heads == [] and not configured and not versions_dir.is_dir():
            # No alembic.ini and no Alembic tree (any repository other than
            # Conductor's own): the check passes as not applicable. With an
            # alembic.ini, a missing or headless versions dir fails below.
            alembic: dict[str, object] = {
                "result": "pass",
                "heads": [],
                "reason": ALEMBIC_NOT_APPLICABLE,
            }
            alembic_reason: str | None = None
        elif heads is None:
            alembic = {
                "result": "fail",
                "heads": [],
                "reason": ALEMBIC_UNPARSEABLE,
            }
            alembic_reason = ALEMBIC_UNPARSEABLE
        elif len(heads) == 1:
            alembic = {"result": "pass", "heads": heads, "reason": ALEMBIC_SINGLE_HEAD}
            alembic_reason = None
        elif len(heads) == 0:
            alembic = {"result": "fail", "heads": [], "reason": ALEMBIC_NO_HEAD}
            alembic_reason = ALEMBIC_NO_HEAD
        else:
            alembic = {
                "result": "fail",
                "heads": heads,
                "reason": ALEMBIC_MULTIPLE_HEADS,
            }
            alembic_reason = ALEMBIC_MULTIPLE_HEADS
        if full_reason:
            return await self._g5_settle(
                finish,
                dispatch,
                full=full,
                alembic=alembic,
                reason=full_reason,
                check=CHECK_FULL_SUITE,
            )
        if alembic_reason:
            return await self._g5_settle(
                finish,
                dispatch,
                full=full,
                alembic=alembic,
                reason=alembic_reason,
                check=CHECK_ALEMBIC,
            )
        return await self._g5_settle(
                finish,
            dispatch,
            full=full,
            alembic=alembic,
            reason=G5_PASSED,
            check="",
        )

    def _note_g2_target(
        self,
        dispatch: Dispatch,
        line: Line,
        attempt: int,
        revision: str | None,
    ) -> None:
        """Record the attempt and the worktree HEAD before the commands run."""
        snap = self._ensure_line(dispatch, line)
        gates = snap["gates"]
        assert isinstance(gates, dict)
        current = gates.get("G2")
        if not isinstance(current, dict):
            current = {
                "result": "running",
                "reason": None,
                "at": self._now(),
                "commands": [],
            }
            gates["G2"] = current
        current["result"] = "running"
        current["attempt"] = attempt
        current["revision"] = revision
        current.setdefault("fix_launches", 0)
        snap["phase"] = "focused_tests"
        self._touch(dispatch, snap, "focused_tests")

    def _structural_findings(
        self,
        line: Line,
        worktree: Path,
        revision: str | None,
        is_checkout: bool,
        *,
        worktree_check: bool = True,
        scope_check: bool = True,
    ) -> dict[str, object]:
        """What the worktree says about the run that just finished.

        Two independent records, each absent when it has nothing to say:

        ``dirty_worktree``
            The paths the focused test run left behind outside a temporary
            directory. A test that writes into the checkout instead of into
            tmp leaves them there (N47).

        ``scope_conflict``
            The changed file names that fall outside the line's write scope,
            with the tagged finding a reviewer-style conflict carries (N52).

        The status is read before the worktree is removed. When Git cannot
        read it the worktree cannot be shown to be clean, so the record says
        so and the gate fails closed. A directory that is not a Git checkout
        at all has no Git state to inspect, so it has nothing to report.
        """
        findings: dict[str, object] = {}
        if is_checkout and worktree_check:
            try:
                status = git_status_paths(worktree)
            except Exception as exc:
                logger.warning("G2 worktree check failed: %s", type(exc).__name__)
                status = None
            if status is None:
                # Git cannot read the status, so the worktree cannot be
                # shown to be clean.
                findings["dirty_worktree"] = _unknown_worktree()
            else:
                outside = tuple(
                    path for path in status if not path_is_temporary(path)
                )
                if outside:
                    findings["dirty_worktree"] = {
                        "paths": list(outside[:_RECORD_PATH_LIMIT]),
                        "count": len(outside),
                        "unknown": False,
                        "finding": (
                            "The focused test run left changes outside a "
                            f"temporary directory: {_shown(outside)}. Tests "
                            "write to tmp only; they must not create or edit "
                            "files in the checkout."
                        ),
                    }
        conflict = None
        if scope_check:
            try:
                conflict = self._scope_conflict(line, revision)
            except Exception as exc:
                logger.warning("G2 scope check failed: %s", type(exc).__name__)
                conflict = _unknown_scope(line.write_scope)
        if conflict is not None:
            findings["scope_conflict"] = conflict
        return findings

    def _scope_conflict(
        self, line: Line, revision: str | None
    ) -> dict[str, object] | None:
        """The structured scope conflict for the tested revision, or None.

        The line's diff is compared with its write scope by file name, so
        the verdict does not depend on a reviewer's prose. The diff is the
        one the handoff already reports: the line's base commit against the
        revision G2 tested, or the repository's HEAD when the line has no
        pinned base. A line with no write scope, no revision, or a diff Git
        cannot produce is not judged: there is nothing to compare against.
        """
        if not revision or not line.write_scope:
            return None
        base = line.base_commit or "HEAD"
        changed = git_changed_paths(self._settings.repo_root, revision, base)
        if changed is None:
            # Git could not produce the diff, so the line cannot be shown to
            # fit its scope: fail closed like the worktree check does.
            return _unknown_scope(line.write_scope)
        outside = scope_violations(changed, line.write_scope)
        if not outside:
            return None
        return {
            "conflict": "scope",
            "files": list(outside[:_RECORD_PATH_LIMIT]),
            "count": len(outside),
            "write_scope": list(line.write_scope),
            "finding": scope_conflict_finding(outside, line.write_scope),
        }

    @staticmethod
    def _require_tests_shadow(line: Line) -> dict[str, object] | None:
        """N46 in ``shadow`` mode: the plan rejection the validator let through.

        In ``enforce`` mode the plan was refused before the line existed, and
        in ``off`` mode the rule does not run, so only ``shadow`` records one.
        """
        if record_only(check_mode(REQUIRE_TESTS_ENV), check="require_tests") != SHADOW:
            return None
        # planning imports this module, so the import waits until G2 runs.
        from .planning import require_tests_refusal

        try:
            refusal = require_tests_refusal(line)
        except Exception as exc:  # a shadow check never breaks the gate
            logger.warning("G2 require-tests shadow check failed: %s", type(exc).__name__)
            return None
        if refusal is None:
            return None
        return shadow_record(
            REQUIRE_TESTS_CHECK,
            refusal.code,
            "plan: the line's acceptance runs no test command covering "
            f"its test files ({refusal.code})",
        )

    def _finish_g2(
        self,
        dispatch: Dispatch,
        line: Line,
        *,
        result: str,
        reason: str,
        recorded: list[dict[str, object]],
        attempt: int,
        revision: str | None,
    ) -> GateStep:
        snap = self._ensure_line(dispatch, line)
        moment = self._now()
        gates = snap["gates"]
        assert isinstance(gates, dict)
        previous = gates.get("G2")
        launched = previous.get("fix_launches") if isinstance(previous, dict) else 0
        # The structural records the gate read from the worktree, left there
        # by the round that just ran. They are taken out here, so the cell
        # is written once and the store does not keep them.
        findings = self._pending_findings.pop(
            (dispatch.id, line.id, attempt), None
        )
        cell: dict[str, object] = {
            "result": result,
            "reason": reason,
            "at": moment,
            "commands": recorded,
            "attempt": attempt,
            "revision": revision,
            "fix_launches": launched or 0,
        }
        # The structural records the gate read from the worktree. They name
        # files and carry the tagged finding; they store no diff and no
        # command output.
        for name in ("dirty_worktree", "scope_conflict"):
            record = (findings or {}).get(name)
            if isinstance(record, dict):
                cell[name] = record
        # N58: what the shadow-mode checks would have refused. They change
        # nothing about the result; they are written on the cell and on the
        # scheduler audit, so ``dispatch status`` shows them.
        shadow = [
            entry
            for entry in (findings or {}).get("shadow") or []
            if isinstance(entry, dict)
        ]
        planned = self._require_tests_shadow(line)
        if planned is not None:
            shadow.append(planned)
        if shadow:
            cell["shadow"] = shadow
            for entry in shadow:
                _append_shadow_audit(dispatch, line, entry, attempt)
        gates["G2"] = cell
        phase = "blocked" if result == "fail" else "focused_tests"
        previous = str(snap.get("phase") or "")
        snap["phase"] = phase
        snap["reason"] = reason
        self._event(
            snap,
            gate="G2",
            result=result,
            reason=reason,
            from_stage=previous,
            to_stage=phase,
        )
        self._touch(dispatch, snap, phase)
        passed = result in ("pass", "skipped")
        return GateStep(passed=passed, reason=reason, result=result)

    def _finish_g5(
        self,
        dispatch: Dispatch,
        *,
        full: dict[str, object],
        alembic: dict[str, object],
        reason: str,
        check: str,
    ) -> GateStep:
        moment = self._now()
        passed = reason == G5_PASSED
        integration = self._integration(dispatch)
        integration["phase"] = "passed" if passed else "failed"
        integration["branch"] = dispatch.integrate_branch
        integration["reason"] = reason
        integration["at"] = moment
        integration["checks"] = {"full_suite": full, "alembic_single_head": alembic}
        result = "pass" if passed else "fail"
        for line in dispatch.lines:
            if line.status.value == "FAILED":
                continue
            snap = self._ensure_line(dispatch, line)
            gates = snap["gates"]
            assert isinstance(gates, dict)
            gates["G5"] = {"result": result, "reason": reason, "at": moment}
            phase = "integration_checks" if passed else "blocked"
            previous = str(snap.get("phase") or "")
            snap["phase"] = phase
            snap["reason"] = reason
            self._event(
                snap,
                gate="G5",
                result=result,
                reason=reason,
                from_stage=previous,
                to_stage=phase,
            )
            self._touch(dispatch, snap, phase)
        self._root(dispatch)["updated_at"] = moment
        return GateStep(passed=passed, reason=reason, result=result, check=check)

    def _full_suite(
        self,
        result: str,
        *,
        exit_code: int | None,
        duration_ms: int,
        log_ref: str | None,
    ) -> dict[str, object]:
        return {
            "result": result,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "command_source": G5_COMMAND_SOURCE,
            "log_ref": log_ref,
        }

    async def _g2_lint_baseline(
        self,
        dispatch: Dispatch,
        line: Line,
        argv: list[str],
        result: CommandResult,
        worktree: Path,
        index: int,
    ) -> dict[str, object] | None:
        """Compare a failed ``ruff check`` with the line's base commit.

        ``None`` when the command is not a recognised ``ruff check`` that
        found something (exit 1), or the baseline is switched off: the plain
        verdict stands. Otherwise a record whose ``passed`` is true only when
        every finding at the head was already there at the base. A record
        that cannot establish that has ``passed`` false and a ``reason``.
        """
        if result.timed_out or result.exit_code != 1 or not lint_baseline_enabled():
            return None
        check = ruff_check_index(argv)
        if check is None:
            return None
        record: dict[str, object] = {
            "passed": False,
            "head_findings": None,
            "base_findings": None,
            "new_findings": None,
        }
        base_commit = (line.base_commit or "").strip()
        if not base_commit:
            record["reason"] = "no_base_commit"
            return record
        json_argv = ruff_json_argv(argv, check)
        prefix = f"gates/{dispatch.id}/{line.id}/g2-0-{index}-lint"
        record["head_log_ref"] = f"{prefix}-head.log"
        head = await self._ruff_findings(
            json_argv, worktree, self._settings.runs_dir / f"{prefix}-head.log"
        )
        if head is None:
            record["reason"] = "head_findings_unreadable"
            return record
        base_worktree = self._gate_worktree_path(dispatch, "line-base", line.id)
        base: list[tuple[str, str, str]] | None = None
        try:
            await self._worktrees.add_detached(
                self._settings.repo_root, base_worktree, base_commit
            )
            base_argv = _base_ruff_argv(json_argv, check, worktree, base_worktree)
            if base_argv is None:
                base = []
            else:
                record["base_log_ref"] = f"{prefix}-base.log"
                base = await self._ruff_findings(
                    base_argv,
                    base_worktree,
                    self._settings.runs_dir / f"{prefix}-base.log",
                )
        except Exception as exc:
            logger.warning("G2 lint baseline failed: %s", type(exc).__name__)
            base = None
        finally:
            try:
                await self._worktrees.remove(self._settings.repo_root, base_worktree)
            except Exception as exc:
                logger.warning(
                    "G2 lint baseline worktree remove failed: %s", type(exc).__name__
                )
        if base is None:
            record["reason"] = "base_findings_unavailable"
            return record
        new = new_lint_findings(head, base)
        record.update(
            head_findings=len(head),
            base_findings=len(base),
            new_findings=new,
            # A head run that found nothing contradicts the failed run it
            # repeats: the verdict cannot be trusted, so it stays a failure.
            passed=bool(head) and new == 0,
        )
        if not head:
            record["reason"] = "head_findings_unreadable"
        return record

    async def _g2_preexisting(
        self,
        dispatch: Dispatch,
        line: Line,
        argv: list[str],
        result: CommandResult,
        log_path: Path,
        index: int,
        record: dict[str, object],
        worktree: Path | None = None,
    ) -> str:
        """N27: the G2 reason for a failed command, after the base re-run."""
        log_ref = f"gates/{dispatch.id}/{line.id}/g2-0-{index}-base.log"
        found = await check_preexisting(
            argv=argv,
            head_exit=result.exit_code,
            head_log=log_path,
            base_commit=line.base_commit,
            repo_root=self._settings.repo_root,
            base_worktree=self._gate_worktree_path(dispatch, "line-base", line.id),
            base_log=self._settings.runs_dir / log_ref,
            base_log_ref=log_ref,
            timeout=rerun_timeout(self._settings.g2_timeout_seconds),
            commands=self._commands,
            worktrees=self._worktrees,
            line_worktree=worktree,
        )
        if found is None:
            return FOCUSED_TESTS_FAILED
        record["preexisting"] = found
        return PREEXISTING_FAILURE if found.get("enforced") else FOCUSED_TESTS_FAILED

    async def _ruff_findings(
        self, argv: list[str], cwd: Path, log_path: Path
    ) -> list[tuple[str, str, str]] | None:
        """Run a JSON ``ruff check`` and read its findings, or ``None``."""
        try:
            result = await self._commands.run(
                argv,
                cwd=cwd,
                timeout=self._settings.g2_timeout_seconds,
                log_path=log_path,
            )
        except Exception as exc:
            logger.warning("G2 lint baseline command failed: %s", type(exc).__name__)
            return None
        if result.timed_out or result.exit_code not in (0, 1):
            return None
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        findings = parse_ruff_findings(text, cwd)
        if findings is None or bool(findings) != (result.exit_code == 1):
            return None
        return findings

    def _gate_worktree_path(
        self, dispatch: Dispatch, purpose: str, line_id: str = ""
    ) -> Path:
        repo_key = hashlib.sha256(str(Path(self._settings.repo_root).resolve()).encode()).hexdigest()[:24]
        dispatch_key = hashlib.sha256(dispatch.id.encode()).hexdigest()[:24]
        line_key = hashlib.sha256(line_id.encode()).hexdigest()[:24] if line_id else ""
        parts = [self._worktrees_dir, repo_key, "gates", dispatch_key, purpose]
        if line_key:
            parts.append(line_key)
        return Path(*parts)

    def _ensure_root(self, dispatch: Dispatch) -> dict[str, object]:
        root = dispatch.gates
        if root.get("schema_version") == SCHEMA_VERSION:
            # N45: a row older code wrote is upgraded before it is read.
            if schema_revision(root) < CURRENT_REVISION:
                migrate_dispatch_gates(dispatch, now=self._now)
        else:
            dispatch.gates = {
                "schema_version": SCHEMA_VERSION,
                SCHEMA_REVISION_KEY: CURRENT_REVISION,
                "updated_at": None,
                "lines": {},
                "integration": {
                    "phase": "pending",
                    "branch": dispatch.integrate_branch,
                    "merged_lines": [],
                    "reason": None,
                    "at": None,
                    "checks": {},
                },
                "approval": {
                    "gate": "G6",
                    "result": "pending",
                    "token_present": False,
                    "at": None,
                },
            }
        return dispatch.gates

    def _root(self, dispatch: Dispatch) -> dict[str, object]:
        return self._ensure_root(dispatch)

    def _integration(self, dispatch: Dispatch) -> dict[str, object]:
        root = self._ensure_root(dispatch)
        integration = root.get("integration")
        if not isinstance(integration, dict):
            integration = {}
            root["integration"] = integration
        return integration

    def _approval(self, dispatch: Dispatch) -> dict[str, object]:
        root = self._ensure_root(dispatch)
        approval = root.get("approval")
        if not isinstance(approval, dict):
            approval = {
                "gate": "G6",
                "result": "pending",
                "token_present": False,
                "at": None,
            }
            root["approval"] = approval
        return approval

    def _ensure_line(self, dispatch: Dispatch, line: Line) -> dict[str, object]:
        root = self._ensure_root(dispatch)
        lines = root.setdefault("lines", {})
        assert isinstance(lines, dict)
        current = lines.get(line.id)
        risk = line_is_risk_bearing(line, self._globs)
        if not isinstance(current, dict):
            current = _line_shell(line, risk)
            lines[line.id] = current
        current["risk_bearing"] = risk
        current["author"] = line.assigned_model
        machine = current.get("machine")
        if isinstance(machine, dict):
            # The machine owns the stage. Mirroring ``phase`` back into it
            # would drop a transition that has not been displayed yet.
            machine["handoff"] = None
            machine["risk_bearing"] = risk
        return current

    def _line(self, dispatch: Dispatch, line_id: str) -> dict[str, object] | None:
        lines = (
            dispatch.gates.get("lines") if isinstance(dispatch.gates, dict) else None
        )
        if not isinstance(lines, dict):
            return None
        snap = lines.get(line_id)
        return snap if isinstance(snap, dict) else None

    def g2_fix_task(self, dispatch: Dispatch, line: Line, task: str) -> str:
        """``task`` plus the failed G2 command, exit code and output tail (N27).

        The machine calls this on the text ``_fix_task`` built for a G2 fix
        round. When the stored G2 result is not a failure the task comes back
        unchanged.
        """
        snap = self._line(dispatch, line.id)
        gates = snap.get("gates") if isinstance(snap, dict) else None
        cell = gates.get("G2") if isinstance(gates, dict) else None
        note = g2_failure_note(cell, self._settings.runs_dir)
        return with_g2_failure(task, note)

    def _g2_result(self, dispatch: Dispatch, line_id: str) -> str | None:
        snap = self._line(dispatch, line_id)
        if snap is None:
            return None
        gates = snap.get("gates")
        if not isinstance(gates, dict):
            return None
        g2 = gates.get("G2")
        if not isinstance(g2, dict):
            return None
        result = g2.get("result")
        return str(result) if isinstance(result, str) else None

    def _revision(self, line: Line) -> str | None:
        """Commit the line branch names, or None when git cannot say."""
        try:
            return self._revisions(line)
        except Exception as exc:
            logger.warning("G2 revision lookup failed: %s", type(exc).__name__)
            return None

    def _g2_bound(self, dispatch: Dispatch, line: Line) -> bool:
        """True when the stored G2 result is this attempt at this commit.

        A missing revision on both sides (tests with no git repo) matches on
        the attempt alone. A stored revision that can no longer be read does
        not match: the result is not reused.
        """
        snap = self._line(dispatch, line.id)
        if snap is None:
            return False
        gates = snap.get("gates")
        if not isinstance(gates, dict):
            return False
        g2 = gates.get("G2")
        if not isinstance(g2, dict):
            return False
        stored_attempt = g2.get("attempt")
        if stored_attempt is None:
            stored_attempt = 0
        try:
            stored_attempt_int = int(stored_attempt)
        except (TypeError, ValueError):
            return False
        if stored_attempt_int != line.attempts:
            return False
        stored = g2.get("revision")
        if stored is not None and not isinstance(stored, str):
            stored = None
        current = self._revision(line)
        if not stored and not current:
            return True
        if not stored or not current:
            return False
        return stored == current

    def _g2_reusable(
        self, dispatch: Dispatch, line_id: str, results: tuple[str, ...]
    ) -> bool:
        line = next((item for item in dispatch.lines if item.id == line_id), None)
        if line is None:
            return False
        result = self._g2_result(dispatch, line_id)
        return result in results and self._g2_bound(dispatch, line)

    async def _g2_reusable_async(
        self, dispatch: Dispatch, line_id: str, results: tuple[str, ...]
    ) -> bool:
        line = next((item for item in dispatch.lines if item.id == line_id), None)
        if line is None:
            return False
        result = self._g2_result(dispatch, line_id)
        return result in results and await self._g2_bound_async(dispatch, line)

    async def _g2_bound_async(self, dispatch: Dispatch, line: Line) -> bool:
        snap = self._line(dispatch, line.id)
        gates = snap.get("gates") if isinstance(snap, dict) else None
        g2 = gates.get("G2") if isinstance(gates, dict) else None
        if not isinstance(g2, dict):
            return False
        stored_attempt = g2.get("attempt")
        try:
            stored_attempt_int = int(stored_attempt if stored_attempt is not None else 0)
        except (TypeError, ValueError):
            return False
        if stored_attempt_int != line.attempts:
            return False
        stored = g2.get("revision")
        if stored is not None and not isinstance(stored, str):
            stored = None
        current = await asyncio.to_thread(self._revision, line)
        if not stored and not current:
            return True
        if not stored or not current:
            return False
        return stored == current

    def _event(
        self,
        snap: dict[str, object],
        *,
        gate: str,
        result: str,
        reason: str,
        to_stage: str,
        from_stage: str | None = None,
    ) -> None:
        events = snap.setdefault("events", [])
        if not isinstance(events, list):
            events = []
            snap["events"] = events
        event = {
            "task_id": snap.get("line_id") or "",
            "from_stage": from_stage
            if from_stage is not None
            else str(snap.get("phase") or ""),
            "to_stage": to_stage,
            "gate": gate,
            "result": result,
            "reason": reason,
            "provider": "",
            "timestamp": self._now(),
            "handoff_ref": "",
        }
        events.append({key: event[key] for key in _EVENT_KEYS})
        if len(events) > MAX_EVENTS:
            del events[:-MAX_EVENTS]

    def _touch(self, dispatch: Dispatch, snap: dict[str, object], stage: str) -> None:
        """Stamp the snapshot. ``stage`` is the displayed phase, not the machine stage."""
        _ = stage
        moment = self._now()
        snap["updated_at"] = moment
        machine = snap.get("machine")
        if isinstance(machine, dict):
            machine["handoff"] = None
        root = self._root(dispatch)
        root["updated_at"] = moment

    def _now(self) -> str:
        return isoformat_utc(self._clock())


__all__ = [
    "PREEXISTING_FAILURE",
    "ALEMBIC_MULTIPLE_HEADS",
    "ALEMBIC_NO_HEAD",
    "ALEMBIC_NOT_APPLICABLE",
    "ALEMBIC_SINGLE_HEAD",
    "CHECK_ALEMBIC",
    "CHECK_FULL_SUITE",
    "DEFAULT_RISK_GLOBS",
    "FOCUSED_TESTS_FAILED",
    "FOCUSED_TESTS_PASSED",
    "G2_NO_COMMANDS",
    "G2_NO_COMMANDS_RISK_BEARING",
    "G2_REVISION_MOVED",
    "G2_TIMEOUT",
    "G5_NOT_CONFIGURED",
    "G5_PASSED",
    "G5_WORKTREE_DIRTY",
    "GateRunner",
    "GateSettings",
    "GateStep",
    "NO_SECONDARY_REVIEWER",
    "NOT_A_DAG",
    "SCHEMA_VERSION",
    "acceptance_commands",
    "line_is_risk_bearing",
    "parse_alembic_heads",
    "topological_lines",
]
