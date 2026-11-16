"""Launch registry: each dispatch launch's process group, on disk, per run (N35).

A dispatch launch (``scripts/dispatch_launch.sh`` and its Worker) leads its own
process group and outlives the backend that started it. After a restart the
resumed drive reuses the line's reserved run id and relaunches it; when the
earlier launch is still running, two agents then work in one worktree (seen
2026-11-10: three minimax launches ran for 24 minutes beside their relaunches).

This module records, beside the run log, one JSON file per run id:

- at spawn: pid, process group id, the leader's start time (the pid-reuse
  guard), cwd, line and dispatch ids, and the run's start HEAD;
- at exit: the exit code, the branch HEAD the run left, and whether the run
  was killed (143 / 137, or a negative signal code).

Before a relaunch, :func:`settle_earlier_launch` looks at the record for the
same run id:

- the earlier launch ended while no backend watched it (the old backend died
  first): its end HEAD is read and recorded, so the killed run's commits stay
  attributed to it and the N34/N38 chain stays intact;
- it is still running: in ``shadow`` mode this is only logged; in ``enforce``
  mode it is adopted (waited for up to ``CONDUCTOR_LAUNCH_ADOPT_WAIT_SECONDS``,
  default 0) and then stopped (TERM, grace, KILL) before the relaunch;
- it runs but its leader cannot be verified as the recorded process (another
  start time, an unreadable one, another group): it is never signalled, and
  ``enforce`` refuses the relaunch with a :class:`LaunchError`.

Mode: ``CONDUCTOR_LAUNCH_ADOPT_MODE`` = ``off`` | ``shadow`` (default) |
``enforce``. ``off`` still writes records (they are evidence only) but never
reads them before a relaunch.

Records live in ``<runs_dir>/launches/``, private (0700 directory, 0600
files), written atomically, never through a symlink. Every write failure is
logged and swallowed: the registry must never fail a launch that would have
worked without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Literal

from .protocols import LaunchError

logger = logging.getLogger(__name__)

MODE_ENV = "CONDUCTOR_LAUNCH_ADOPT_MODE"
ADOPT_WAIT_ENV = "CONDUCTOR_LAUNCH_ADOPT_WAIT_SECONDS"
MODES = ("off", "shadow", "enforce")
DEFAULT_MODE = "shadow"
REGISTRY_DIR = "launches"
#: TERM -> KILL grace when a relaunch stops an earlier launch.
STOP_GRACE_SECONDS = 10.0
_POLL_SECONDS = 0.25
#: 128 + SIGTERM / SIGKILL from a shell, or the negative code Python reports.
KILLED_EXIT_CODES = frozenset({143, 137, -signal.SIGTERM, -signal.SIGKILL})

Mode = Literal["off", "shadow", "enforce"]
Action = Literal["none", "off", "ended_unwatched", "seen", "adopted", "stopped", "unverified"]
HeadReader = Callable[[], Awaitable[str | None]]

_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}")


def adopt_mode(env: Mapping[str, str] | None = None) -> Mode:
    raw = ((os.environ if env is None else env).get(MODE_ENV) or "").strip().lower()
    return raw if raw in MODES else DEFAULT_MODE  # type: ignore[return-value]


def adopt_wait_seconds(env: Mapping[str, str] | None = None) -> float:
    raw = ((os.environ if env is None else env).get(ADOPT_WAIT_ENV) or "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        return 0.0
    return value if 0.0 <= value < float("inf") else 0.0


def is_killed_exit(code: int | None) -> bool:
    return code is not None and code in KILLED_EXIT_CODES


@dataclass
class LaunchRecord:
    run_id: str
    pid: int
    pgid: int
    leader_start: str = ""
    cwd: str = ""
    line_id: str = ""
    dispatch_id: str = ""
    start_head: str | None = None
    started_at: float = 0.0
    exit_code: int | None = None
    end_head: str | None = None
    ended_at: float | None = None
    killed: bool = False
    #: Who ended it when it was not its own backend: "relaunch", "unwatched".
    ended_by: str = ""

    @property
    def ended(self) -> bool:
        return self.ended_at is not None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> LaunchRecord | None:
        names = {item.name for item in fields(cls)}
        values = {key: value for key, value in data.items() if key in names}
        try:
            record = cls(**values)  # type: ignore[arg-type]
        except TypeError:
            return None
        if not isinstance(record.run_id, str):
            return None
        for number in (record.pid, record.pgid):
            if not isinstance(number, int) or isinstance(number, bool):
                return None
        return record


# --- process probe ------------------------------------------------------------


class ProcessProbe:
    """The OS calls the registry needs; a fake replaces it in tests."""

    def pgid(self, pid: int) -> int | None:
        try:
            return os.getpgid(pid)
        except (ProcessLookupError, PermissionError, OSError):
            return None

    def start_time(self, pid: int) -> str:
        """The process start time as ``ps`` prints it; "" when unreadable."""
        try:
            result = subprocess.run(
                ["ps", "-o", "lstart=", "-p", str(pid)],
                capture_output=True,
                text=True,
                timeout=5,
                env={**os.environ, "LC_ALL": "C"},
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return " ".join(result.stdout.split()) if result.returncode == 0 else ""

    def group_alive(self, pgid: int) -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def signal_group(self, pgid: int, sig: int) -> None:
        os.killpg(pgid, sig)


# --- storage ------------------------------------------------------------------


def registry_dir(runs_dir: Path) -> Path:
    return Path(runs_dir) / REGISTRY_DIR


def _file_name(run_id: str) -> str:
    if _SAFE_ID.fullmatch(run_id) and run_id not in {".", ".."}:
        return f"{run_id}.json"
    return "h-" + hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:32] + ".json"


def record_path(runs_dir: Path, run_id: str) -> Path:
    return registry_dir(runs_dir) / _file_name(run_id)


def _ensure_dir(path: Path) -> bool:
    if path.is_symlink():
        logger.warning("Launch registry %s is a symlink; not written", path)
        return False
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
    return path.is_dir() and not path.is_symlink()


def _write(runs_dir: Path, record: LaunchRecord) -> bool:
    directory = registry_dir(runs_dir)
    try:
        if not _ensure_dir(directory):
            return False
        target = record_path(runs_dir, record.run_id)
        if target.is_symlink():
            logger.warning("Launch record %s is a symlink; not written", target)
            return False
        temporary = directory / f".{target.name}.{os.getpid()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(record.to_json())
        os.replace(temporary, target)
        return True
    except OSError as exc:
        logger.warning(
            "Launch record for run %s not written: %s", record.run_id, type(exc).__name__
        )
        return False


def read_record(runs_dir: Path, run_id: str) -> LaunchRecord | None:
    path = record_path(runs_dir, run_id)
    try:
        if path.is_symlink():
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    record = LaunchRecord.from_mapping(data)
    if record is None or record.run_id != run_id:
        return None
    return record


def list_records(runs_dir: Path) -> list[LaunchRecord]:
    directory = registry_dir(runs_dir)
    if directory.is_symlink() or not directory.is_dir():
        return []
    found: list[LaunchRecord] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            record = LaunchRecord.from_mapping(data)
            if record is not None:
                found.append(record)
    return found


def record_spawn(
    runs_dir: Path,
    run_id: str,
    *,
    pid: int,
    cwd: str | Path = "",
    line_id: str = "",
    dispatch_id: str = "",
    start_head: str | None = None,
    probe: ProcessProbe | None = None,
    now: float | None = None,
) -> LaunchRecord:
    """Record a just-spawned launch. Its group id is read, not assumed."""
    probe = probe or ProcessProbe()
    pgid = probe.pgid(pid)
    record = LaunchRecord(
        run_id=run_id,
        pid=pid,
        pgid=pgid if pgid is not None else pid,
        leader_start=probe.start_time(pid),
        cwd=str(cwd),
        line_id=line_id,
        dispatch_id=dispatch_id,
        start_head=start_head,
        started_at=time.time() if now is None else now,
    )
    _write(runs_dir, record)
    return record


def annotate(runs_dir: Path, run_id: str, **values: object) -> LaunchRecord | None:
    """Add line/dispatch ids or the start HEAD to an existing record."""
    record = read_record(runs_dir, run_id)
    if record is None:
        return None
    for key in ("line_id", "dispatch_id", "start_head"):
        if key in values and values[key] is not None:
            setattr(record, key, values[key])
    _write(runs_dir, record)
    return record


def record_exit(
    runs_dir: Path,
    run_id: str,
    *,
    exit_code: int | None,
    end_head: str | None = None,
    ended_by: str = "",
    killed: bool | None = None,
    now: float | None = None,
) -> LaunchRecord | None:
    """Close a run's record with its exit code and the HEAD it left."""
    record = read_record(runs_dir, run_id)
    if record is None:
        return None
    record.exit_code = exit_code
    if end_head:
        record.end_head = end_head
    record.killed = is_killed_exit(exit_code) if killed is None else killed
    record.ended_by = ended_by or record.ended_by
    record.ended_at = time.time() if now is None else now
    _write(runs_dir, record)
    return record


def interrupted_run_heads(
    runs_dir: Path, run_id: str
) -> tuple[str | None, str | None] | None:
    """``(start_head, end_head)`` of a run that was killed or ended unwatched.

    The machine notes these as the run's chain (not submitted), so a retry
    that finds nothing new is judged against the killed run's HEAD, not
    against a chain that lost its last link. None for any other run.
    """
    record = read_record(runs_dir, run_id)
    if record is None or not record.ended:
        return None
    if not (record.killed or record.ended_by == "unwatched"):
        return None
    return record.start_head, record.end_head


# --- before a relaunch ----------------------------------------------------------


@dataclass(frozen=True)
class SettleResult:
    action: Action
    record: LaunchRecord | None = None
    detail: str = ""


class EarlierLaunchRunning(LaunchError):
    """An earlier launch of this run still runs and cannot be safely stopped."""


def _leader_verified(record: LaunchRecord, probe: ProcessProbe) -> bool | None:
    """True: the recorded launch runs. False: it is gone. None: cannot tell.

    While any member of a process group lives, its id is not handed to a new
    process (POSIX), so a live group whose leader has exited (or whose
    leader pid now reports another group) is still the recorded launch's.
    A leader of the recorded group is checked by its start time: a mismatch
    means the pid was reused by a new session leader after the launch ended.
    """
    pgid = probe.pgid(record.pid)
    if pgid is None or pgid != record.pgid:
        return probe.group_alive(record.pgid)
    start = probe.start_time(record.pid)
    if not record.leader_start or not start:
        return None
    return start == record.leader_start


def _safe_group(pgid: int) -> bool:
    return pgid > 1 and pgid != os.getpgrp()


async def _wait_gone(
    probe: ProcessProbe,
    pgid: int,
    timeout: float,
    sleep: Callable[[float], Awaitable[None]],
    clock: Callable[[], float],
) -> bool:
    deadline = clock() + max(timeout, 0.0)
    while probe.group_alive(pgid):
        if clock() >= deadline:
            return False
        await sleep(_POLL_SECONDS)
    return True


async def _end_head(head_reader: HeadReader | None) -> str | None:
    if head_reader is None:
        return None
    try:
        return await head_reader()
    except Exception:  # noqa: BLE001 - evidence only; never fail the relaunch here
        logger.warning("Could not read the end HEAD of an earlier launch", exc_info=True)
        return None


async def settle_earlier_launch(
    runs_dir: Path,
    run_id: str,
    *,
    mode: Mode | None = None,
    head_reader: HeadReader | None = None,
    probe: ProcessProbe | None = None,
    adopt_wait: float | None = None,
    grace: float = STOP_GRACE_SECONDS,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    clock: Callable[[], float] | None = None,
) -> SettleResult:
    """Adopt or stop an earlier launch of ``run_id`` before it is relaunched.

    Call it after the worktree is ready and the epoch fence passed, right
    before the spawn. See the module docstring for the outcomes. Raises
    :class:`EarlierLaunchRunning` only in ``enforce`` mode, for a live
    earlier launch that cannot be verified, or one that survived KILL.
    """
    mode = adopt_mode() if mode is None else mode
    if mode == "off":
        return SettleResult("off")
    record = read_record(runs_dir, run_id)
    if record is None or record.ended:
        return SettleResult("none", record)
    probe = probe or ProcessProbe()
    sleep = sleep or asyncio.sleep
    clock = clock or time.monotonic
    verified = _leader_verified(record, probe)
    if verified is False:
        # Ended while no backend watched it (the old backend died first).
        closed = record_exit(
            runs_dir,
            run_id,
            exit_code=None,
            end_head=await _end_head(head_reader),
            ended_by="unwatched",
            killed=False,
        )
        logger.info("Earlier launch of run %s ended unwatched; end HEAD recorded", run_id)
        return SettleResult("ended_unwatched", closed or record)
    if verified is None:
        detail = (
            f"an earlier launch of run {run_id} (pgid {record.pgid}) may still run "
            "and its leader cannot be verified; it is not signalled"
        )
        logger.warning(detail)
        if mode == "enforce":
            raise EarlierLaunchRunning(detail)
        return SettleResult("unverified", record, detail)
    if mode == "shadow":
        logger.warning(
            "Earlier launch of run %s still runs (pid %s, pgid %s); shadow mode, "
            "relaunching beside it",
            run_id,
            record.pid,
            record.pgid,
        )
        return SettleResult("seen", record)
    wait = adopt_wait_seconds() if adopt_wait is None else max(float(adopt_wait), 0.0)
    if wait > 0 and await _wait_gone(probe, record.pgid, wait, sleep, clock):
        closed = record_exit(
            runs_dir,
            run_id,
            exit_code=None,
            end_head=await _end_head(head_reader),
            ended_by="adopted",
            killed=False,
        )
        return SettleResult("adopted", closed or record)
    if not _safe_group(record.pgid):
        raise EarlierLaunchRunning(f"refusing to signal process group {record.pgid}")
    logger.warning(
        "Stopping earlier launch of run %s (pid %s, pgid %s) before relaunching",
        run_id,
        record.pid,
        record.pgid,
    )
    with contextlib.suppress(ProcessLookupError):
        probe.signal_group(record.pgid, signal.SIGTERM)
    if not await _wait_gone(probe, record.pgid, grace, sleep, clock):
        # Re-verify before KILL: the leader may have exited and its pid moved on.
        if _leader_verified(record, probe) is True:
            with contextlib.suppress(ProcessLookupError):
                probe.signal_group(record.pgid, signal.SIGKILL)
        if not await _wait_gone(probe, record.pgid, grace, sleep, clock):
            raise EarlierLaunchRunning(
                f"earlier launch of run {run_id} (pgid {record.pgid}) survived KILL"
            )
    closed = record_exit(
        runs_dir,
        run_id,
        exit_code=-signal.SIGTERM,
        end_head=await _end_head(head_reader),
        ended_by="relaunch",
        killed=True,
    )
    return SettleResult("stopped", closed or record)
