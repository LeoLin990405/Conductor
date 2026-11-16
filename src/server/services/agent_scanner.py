"""Read-only discovery of the local agent fleet.

The fleet is a set of git worktrees under one root, each driven by an agent CLI
whose output lands in a per-agent log file. This module observes that shape from
the outside — ``ps``, ``lsof``, ``git`` and the log files — and never writes to
any of it, so a scan cannot disturb a running agent.

Every command runs through an injectable runner with a timeout, which is what
the unit tests substitute; no test spawns a real subprocess or reads the
operator's machine.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import structlog

from src.llms.native_cli.process import kill_child_group, spawn_group
from src.server.models.agent import (
    Agent,
    AgentLogs,
    AgentRole,
    AgentStatus,
    BalanceState,
)
from src.server.utils.sanitize import sanitize_log_text, sanitize_log_tail

logger = structlog.get_logger(__name__)

CommandRunner = Callable[[Sequence[str], float], Awaitable[str]]


class CommandError(RuntimeError):
    """A bounded observation command failed, timed out, or is unavailable."""


# Executables the worktree launcher hands an agent to. A process whose command
# does not start with one of these is not a fleet agent.
_PROVIDER_BY_EXECUTABLE = {
    "codex": "codex",
    "codexrev": "codex",
    "grok": "grok",
    "kimi": "kimi",
    "zcode-cli": "zcode",
    "zcode": "zcode",
    "dsh": "deepseek-dsh",
    "mmx": "minimax",
    "minimax": "minimax",
    "step": "stepfun",
    "stepfun": "stepfun",
    "arkcli": "doubao",
    "doubao": "doubao",
    "mimo": "mimo",
    "claude": "claude",
}

# Failure markers worth surfacing as ERROR. Line-anchored on purpose: agent logs
# are full of prose about errors, and a substring match would call every agent
# broken.
_ERROR_MARKERS = (
    re.compile(r"(?m)^\s*(?:FAILED|ERROR)\b"),
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"(?m)^\s*[A-Za-z_.]*(?:Error|Exception|Failure)\s*:"),
    re.compile(r"(?i)\bpanic:\s"),
    re.compile(r"(?i)\bcommand not found\b"),
    re.compile(r"(?i)\bfatal:\s"),
    re.compile(r"(?i)\bexited with code [1-9]\d*\b"),
)

_PYTEST_COUNTS = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b")

_ROLE_MARKERS: tuple[tuple[AgentRole, tuple[str, ...]], ...] = (
    (
        AgentRole.REVIEW,
        ("code review", "review only", "do not edit", "read-only review", "verdict merge"),
    ),
    (
        AgentRole.PLAN,
        (
            "implementation plan",
            "plan only",
            "no code",
            "write a plan",
            "write a concise",
            "planner",
        ),
    ),
    (
        AgentRole.RESEARCH,
        ("research", "survey", "investigate", "spike", "compare "),
    ),
)

_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class ScanSettings:
    """Resolved, path-bearing view of the fleet configuration."""

    root: Path
    logs_dir: Path
    prompts_dir: Path
    stall_secs: int
    base_ref: str
    log_tail_bytes: int
    max_worktrees: int
    command_timeout: float
    max_processes: int = 64
    max_log_files: int = 512

    @classmethod
    def from_config(cls, config: Any) -> ScanSettings:
        root = Path(str(getattr(config, "root", "~/conductor-wt"))).expanduser()
        logs_dir = str(getattr(config, "logs_dir", "") or "").strip()
        prompts_dir = str(getattr(config, "prompts_dir", "") or "").strip()
        return cls(
            root=root,
            logs_dir=Path(logs_dir).expanduser() if logs_dir else root / "logs",
            prompts_dir=Path(prompts_dir).expanduser() if prompts_dir else root / "prompts",
            stall_secs=max(1, int(getattr(config, "stall_mins", 5))) * 60,
            base_ref=str(getattr(config, "base_ref", "main") or "main"),
            log_tail_bytes=max(256, int(getattr(config, "log_tail_bytes", 4096))),
            max_worktrees=max(1, int(getattr(config, "max_worktrees", 64))),
            command_timeout=float(getattr(config, "command_timeout_seconds", 5.0)),
        )


@dataclass(frozen=True)
class ProcessRow:
    """One ``ps`` row: pid, elapsed time and the full command line."""

    pid: int
    elapsed: str
    command: str


@dataclass(frozen=True)
class ProcessFacts:
    """A candidate agent process with whatever its open files reveal."""

    pid: int
    command: str
    elapsed_secs: int | None = None
    cwd: str | None = None
    open_paths: tuple[str, ...] = field(default_factory=tuple)


async def _default_runner(argv: Sequence[str], timeout: float) -> str:
    """Run one bounded, non-shell command and return its stdout."""
    try:
        process = await spawn_group(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (OSError, ValueError) as exc:
        raise CommandError(f"could not start {argv[0]!r}") from exc
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        kill_child_group(process)
        await process.wait()
        raise CommandError(f"{argv[0]!r} timed out") from exc
    except OSError as exc:  # pragma: no cover - platform dependent
        raise CommandError(f"{argv[0]!r} failed") from exc
    except BaseException:
        # The timeout dies with a cancelled call, and ``lsof`` can block.
        kill_child_group(process)
        raise
    if process.returncode != 0:
        raise CommandError(f"{argv[0]!r} exited {process.returncode}")
    return stdout.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Pure parsers
# ---------------------------------------------------------------------------


def parse_ps_rows(output: str) -> list[ProcessRow]:
    """Parse ``ps -Ao pid=,etime=,command=`` output into rows."""
    rows: list[ProcessRow] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit():
            continue
        rows.append(ProcessRow(pid=int(parts[0]), elapsed=parts[1], command=parts[2]))
    return rows


def parse_elapsed(value: str) -> int | None:
    """Parse a ``ps`` elapsed time (``[DD-]HH:]MM:SS``) into seconds."""
    text = value.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, _, text = text.partition("-")
        if not day_part.isdigit():
            return None
        days = int(day_part)
    fields = text.split(":")
    if not 1 < len(fields) <= 3 or not all(field_.isdigit() for field_ in fields):
        return None
    hours, minutes, seconds = (int(f) for f in (["0", "0"] + fields)[-3:])
    if minutes > 59 or seconds > 59:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_worktree_paths(output: str) -> list[str]:
    """Parse ``git worktree list --porcelain`` into worktree paths."""
    paths: list[str] = []
    for line in output.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
            if path:
                paths.append(path)
    return paths


def parse_branch(output: str) -> str:
    """Parse ``git rev-parse --abbrev-ref HEAD``."""
    branch = output.strip().splitlines()[0].strip() if output.strip() else ""
    return "" if branch == "HEAD" else branch[:200]


def parse_commit_count(output: str) -> int | None:
    """Parse ``git rev-list --count``; None when git could not answer."""
    text = output.strip()
    if not text.isdigit():
        return None
    return min(int(text), 1_000_000)


def provider_for_command(command: str) -> str | None:
    """Map an agent command line to its canonical provider, if it is one."""
    if not command:
        return None
    executable = command.split()[0]
    name = executable.rsplit("/", 1)[-1]
    return _PROVIDER_BY_EXECUTABLE.get(name)


_MODEL_FLAG = re.compile(r"(?:(?<=\s)--model[=\s]+|(?<=\s)-m[=\s]+)([A-Za-z0-9._:/-]{1,80})")


def model_for_command(command: str) -> str:
    """Read an explicit ``--model``/``-m`` flag out of an agent command line."""
    match = _MODEL_FLAG.search(command or "")
    return match.group(1) if match else ""


def detect_status(
    *,
    alive: bool,
    log_present: bool,
    log_age_secs: float | None,
    stall_secs: int,
    error_seen: bool,
    processes_known: bool = True,
) -> AgentStatus:
    """Judge one agent from its process, its log's freshness and its log's text.

    ``processes_known`` is False when the process table could not be read at
    all. A process that is not there is done, but "not there" is only evidence
    once the listing has answered: without it the row says unknown rather than
    reporting work that may still be running as finished.
    """
    if not processes_known:
        return AgentStatus.UNKNOWN
    if not alive:
        return AgentStatus.DONE
    if not log_present:
        return AgentStatus.UNKNOWN
    if error_seen:
        return AgentStatus.ERROR
    if log_age_secs is None or log_age_secs > stall_secs:
        return AgentStatus.STALLED
    return AgentStatus.RUNNING


def log_has_error(text: str) -> bool:
    """True when a log tail carries a failure marker worth escalating."""
    if not text:
        return False
    return any(marker.search(text) for marker in _ERROR_MARKERS)


def parse_test_state(text: str) -> str | None:
    """Summarize the last pytest terminal line in a log, or None if absent."""
    latest: dict[str, int] | None = None
    for line in text.splitlines():
        counts = {kind.rstrip("s"): int(number) for number, kind in _PYTEST_COUNTS.findall(line)}
        counts = {kind: value for kind, value in counts.items() if value}
        if not counts:
            continue
        # Only a terminal summary line reports a whole run; a bare "3 failed"
        # inside a traceback describes one case.
        if "passed" not in counts and "failed" not in counts and "error" not in counts:
            continue
        if " in " not in line and "====" not in line:
            continue
        latest = counts
    if not latest:
        return None
    labels = (
        ("passed", "passed"),
        ("failed", "failed"),
        ("error", "error"),
        ("skipped", "skipped"),
    )
    parts = [f"{latest[kind]} {label}" for kind, label in labels if latest.get(kind)]
    return ", ".join(parts) or None


def infer_role(text: str) -> AgentRole:
    """Guess what an agent was asked to do from the head of its prompt."""
    lowered = (text or "").lower()
    for role, markers in _ROLE_MARKERS:
        if any(marker in lowered for marker in markers):
            return role
    return AgentRole.DEV


def balance_state(provider: str, states: dict[str, str]) -> BalanceState:
    """Project a provider's snapshot state onto the dashboard's three values."""
    if not provider:
        return "unknown"
    state = states.get(provider)
    if state == "available":
        return "ok"
    if state in {"low", "exhausted"}:
        return "low"
    return "unknown"


def read_text_tail(path: Path, max_bytes: int) -> bytes:
    """Read at most the last ``max_bytes`` of a file, tolerating absence."""
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    if size <= 0:
        return b""
    try:
        with path.open("rb") as stream:
            if size > max_bytes:
                stream.seek(size - max_bytes)
            return stream.read(max_bytes)
    except OSError:
        return b""


def read_text_head(path: Path, max_bytes: int) -> bytes:
    """Read at most the first ``max_bytes`` of a file, tolerating absence."""
    try:
        with path.open("rb") as stream:
            return stream.read(max_bytes)
    except OSError:
        return b""


def file_mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def assign_log_files(names: Sequence[str], logs_dir: Path, *, max_files: int = 512) -> dict[str, Path]:
    """Map each agent id to its log file, claiming every file at most once.

    An exact ``<id>.log`` wins over ``<id>-<suffix>.log``, and a suffixed file
    goes to the longest id that claims it, so ``port-codexbar`` never adopts
    ``port-codexbar-snapshot.log``.
    """
    try:
        candidates = sorted(path for path in logs_dir.glob("*.log") if path.is_file())[:max_files]
    except OSError:
        return {}
    available = {path.name: path for path in candidates}
    assigned: dict[str, Path] = {}
    claimed: set[str] = set()
    ordered = sorted(names, key=len, reverse=True)
    for name in ordered:
        exact = f"{name}.log"
        if exact in available:
            assigned[name] = available[exact]
            claimed.add(exact)
    for name in ordered:
        if name in assigned:
            continue
        prefix = f"{name}-"
        matches = [
            path
            for filename, path in available.items()
            if filename.startswith(prefix) and filename not in claimed
        ]
        if not matches:
            continue
        newest = max(matches, key=lambda path: _safe_mtime(path))
        assigned[name] = newest
        claimed.add(newest.name)
    return assigned


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def valid_agent_id(agent_id: str) -> bool:
    """An agent id names a file, so it may only be a bare log basename."""
    return bool(_AGENT_ID_RE.match(agent_id or "")) and ".." not in agent_id


class AgentLogNotFound(LookupError):
    """No readable log exists for the requested agent id."""


# ---------------------------------------------------------------------------
# Observation commands
# ---------------------------------------------------------------------------


def parse_lsof(output: str) -> tuple[str | None, tuple[str, ...]]:
    """Parse ``lsof -p PID -Fn`` into (cwd, other open paths)."""
    cwd: str | None = None
    paths: list[str] = []
    descriptor = ""
    for line in output.splitlines():
        if len(line) < 2:
            continue
        kind, value = line[0], line[1:]
        if kind == "f":
            descriptor = value
        elif kind == "n" and value:
            if descriptor == "cwd":
                cwd = value
            else:
                paths.append(value)
    return cwd, tuple(paths)


async def _git_output(
    path: Path, args: Sequence[str], settings: ScanSettings, run: CommandRunner
) -> str | None:
    try:
        return await run(["git", "-C", str(path), *args], settings.command_timeout)
    except CommandError as exc:
        logger.debug("fleet git command failed", path=str(path), args=list(args), error=str(exc))
        return None


async def _worktree_paths(settings: ScanSettings, run: CommandRunner) -> list[Path]:
    """List the fleet's worktrees through any one of them.

    Worktrees of one repository share a git dir, so the first directory under
    the root that answers lists them all; directories that are not worktrees
    (logs, prompts) simply fail and are skipped.
    """
    try:
        entries = sorted(path for path in settings.root.iterdir() if path.is_dir())
    except OSError as exc:
        logger.warning("fleet root unreadable", root=str(settings.root), error=str(exc))
        return []
    for entry in entries[: settings.max_worktrees]:
        output = await _git_output(entry, ["worktree", "list", "--porcelain"], settings, run)
        if not output:
            continue
        paths = [Path(item) for item in parse_worktree_paths(output)]
        inside = [path for path in paths if _within(path, settings.root)]
        if inside:
            return inside
    return []


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


async def _process_open_paths(
    pid: int, settings: ScanSettings, run: CommandRunner
) -> tuple[str | None, tuple[str, ...]]:
    """Best-effort cwd and open files for one pid; unavailable is not an error."""
    if shutil.which("lsof") is None:
        return None, ()
    try:
        output = await run(["lsof", "-p", str(pid), "-Fn"], settings.command_timeout)
    except CommandError as exc:
        logger.debug("fleet lsof failed", pid=pid, error=str(exc))
        return None, ()
    return parse_lsof(output)


async def _list_processes(
    settings: ScanSettings, run: CommandRunner
) -> list[ProcessFacts] | None:
    """Collect the agent-shaped processes, or None when the table is unreadable.

    A ``ps`` that failed or timed out is not an empty process table, it is no
    answer at all — and an empty list reads as "no agent is running", which
    marks every worktree in the fleet done. Returning None keeps the two apart
    so the scan can report unknown instead.
    """
    try:
        output = await run(["ps", "-Ao", "pid=,etime=,command="], settings.command_timeout)
    except CommandError as exc:
        logger.warning("fleet process listing failed", error=str(exc))
        return None
    # The cap bounds how many *agents* are inspected, so it has to be applied
    # after the provider filter: capping the raw `ps` rows first would drop
    # every agent that shares the machine with more unrelated processes than
    # the limit, and those agents would then read as done.
    agent_rows = [
        row for row in parse_ps_rows(output) if provider_for_command(row.command) is not None
    ]
    facts: list[ProcessFacts] = []
    for row in agent_rows[: settings.max_processes]:
        cwd, open_paths = await _process_open_paths(row.pid, settings, run)
        facts.append(
            ProcessFacts(
                pid=row.pid,
                command=row.command,
                elapsed_secs=parse_elapsed(row.elapsed),
                cwd=cwd,
                open_paths=open_paths,
            )
        )
    return facts


def _associate_processes(
    facts: Sequence[ProcessFacts], worktrees: Sequence[Path], logs: dict[str, Path], settings: ScanSettings
) -> dict[str, int]:
    """Attribute each agent process to one worktree, strongest evidence first."""
    def normalized(path: str | Path) -> str:
        return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))

    paths = {normalized(path): path.name for path in worktrees}

    def owner_for_path(candidate: str) -> str | None:
        matches: list[tuple[int, str]] = []
        for root, name in paths.items():
            try:
                inside = os.path.commonpath((candidate, root)) == root
            except ValueError:
                inside = False
            if inside:
                matches.append((len(root), name))
        return max(matches)[1] if matches else None

    owners = {path.name: name for name, path in logs.items()}
    logs_dir = normalized(settings.logs_dir)
    assigned: dict[str, int] = {}
    for fact in facts:
        owner: str | None = None
        if fact.cwd:
            owner = owner_for_path(normalized(fact.cwd))
        if owner is None:
            for open_path in fact.open_paths:
                if normalized(Path(open_path).parent) != logs_dir:
                    continue
                owner = owners.get(Path(open_path).name)
                if owner is not None:
                    break
        if owner is None:
            try:
                tokens = shlex.split(fact.command)
            except ValueError:
                tokens = fact.command.split()
            option_paths: list[str] = []
            positional_paths: list[str] = []
            for index, token in enumerate(tokens):
                value = ""
                if token in {"-C", "--cd", "--cwd"} and index + 1 < len(tokens):
                    value = tokens[index + 1]
                    option_paths.append(normalized(value))
                elif token.startswith(("--cd=", "--cwd=", "-C")):
                    value = token.split("=", 1)[1] if "=" in token else token[2:]
                    if value:
                        option_paths.append(normalized(value))
                elif token.startswith(("/", "~")):
                    positional_paths.append(normalized(token))
            option_owners = {
                owner for path in option_paths if (owner := owner_for_path(path)) is not None
            }
            if len(option_owners) == 1:
                owner = next(iter(option_owners))
            elif not option_paths:
                command_owners = {paths[path] for path in positional_paths if path in paths}
                if len(command_owners) == 1:
                    owner = next(iter(command_owners))
        if owner is not None and owner not in assigned:
            assigned[owner] = fact.pid
    return assigned


async def _provider_states(now: datetime) -> dict[str, str]:
    """Provider states from the configured local snapshot; advisory only."""
    try:
        from src.server.services.native_runtime.usage_headroom import load_usage_headroom

        return await load_usage_headroom(now=now)
    except Exception as exc:  # an unreadable snapshot must not fail the scan
        logger.warning("fleet provider snapshot unreadable", error=str(exc))
        return {}


@dataclass
class _Observation:
    """What the filesystem says about one worktree, before its process."""

    path: Path
    branch: str
    commits_ahead: int | None
    log_path: Path | None
    log_tail: str
    error_seen: bool
    last_activity: datetime | None
    role: AgentRole
    tests_state: str | None


def _read_prompt_head(name: str, settings: ScanSettings) -> str:
    """Read the head of an agent's prompt file, if one is kept beside its logs.

    The head, not the tail: a prompt opens with the role it was given — "you
    are doing a code review", "write an implementation plan" — and a launcher
    that appends the worktree history or a transcript behind it puts those
    instructions past any bounded read taken from the end.
    """
    for candidate in (settings.prompts_dir / f"{name}.txt", settings.logs_dir / f"{name}.prompt"):
        raw = read_text_head(candidate, 2048)
        if raw:
            return sanitize_log_text(
                bytes(raw).decode("utf-8", errors="replace"), max_length=1000
            )
    return ""


async def _observe(
    path: Path,
    settings: ScanSettings,
    run: CommandRunner,
    logs: dict[str, Path],
) -> _Observation:
    branch = parse_branch(
        await _git_output(path, ["rev-parse", "--abbrev-ref", "HEAD"], settings, run) or ""
    )
    commits = parse_commit_count(
        await _git_output(
            path, ["rev-list", "--count", f"{settings.base_ref}..HEAD"], settings, run
        )
        or ""
    )
    log_path = logs.get(path.name)
    raw_tail = read_text_tail(log_path, settings.log_tail_bytes) if log_path else b""
    raw_text = bytes(raw_tail).decode("utf-8", errors="replace")
    tail = sanitize_log_tail(raw_tail) if log_path else ""
    prompt = _read_prompt_head(path.name, settings)
    return _Observation(
        path=path,
        branch=branch,
        commits_ahead=commits,
        log_path=log_path,
        log_tail=tail,
        error_seen=log_has_error(raw_text),
        last_activity=file_mtime(log_path) if log_path else None,
        role=infer_role(prompt or tail[:1000]),
        tests_state=parse_test_state(raw_text),
    )


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(timezone.utc)


async def scan_agents(
    settings: ScanSettings | None = None,
    *,
    now: datetime | None = None,
    run: CommandRunner | None = None,
) -> list[Agent]:
    """Observe the fleet and return one row per worktree, read-only throughout."""
    if settings is None:
        from src.config.settings import get_agent_fleet_config

        settings = ScanSettings.from_config(get_agent_fleet_config())
    runner = run or _default_runner
    clock = _utc(now)

    worktrees = (await _worktree_paths(settings, runner))[: settings.max_worktrees]
    if not worktrees:
        return []
    logs = assign_log_files(
        [path.name for path in worktrees], settings.logs_dir, max_files=settings.max_log_files
    )

    gate = asyncio.Semaphore(8)

    async def _guarded(path: Path) -> _Observation:
        async with gate:
            return await _observe(path, settings, runner, logs)

    observations = await asyncio.gather(*(_guarded(path) for path in worktrees))
    facts = await _list_processes(settings, runner)
    processes_known = facts is not None
    pids = _associate_processes(facts or (), worktrees, logs, settings)
    by_pid = {fact.pid: fact for fact in facts or ()}
    states = await _provider_states(clock)

    agents: list[Agent] = []
    for observation in observations:
        fact = by_pid.get(pids.get(observation.path.name, -1))
        provider = provider_for_command(fact.command) if fact else None
        age = None
        if observation.last_activity is not None:
            age = max(0.0, (clock - observation.last_activity).total_seconds())
        agents.append(
            Agent(
                id=observation.path.name,
                model=(model_for_command(fact.command) if fact else "") or (provider or ""),
                provider=provider or "",
                role=observation.role,
                pid=fact.pid if fact else None,
                worktree=str(observation.path),
                branch=observation.branch,
                status=detect_status(
                    alive=fact is not None,
                    log_present=observation.log_path is not None,
                    log_age_secs=age,
                    stall_secs=settings.stall_secs,
                    error_seen=observation.error_seen,
                    processes_known=processes_known,
                ),
                elapsed_secs=fact.elapsed_secs if fact else None,
                last_activity_ts=observation.last_activity,
                log_file=observation.log_path.name if observation.log_path else "",
                log_tail=observation.log_tail,
                commits_ahead=observation.commits_ahead,
                tests_state=observation.tests_state,
                provider_balance=balance_state(provider or "", states),
            )
        )
    agents.sort(key=lambda agent: agent.id)
    return agents


async def _resolve_log_path(
    agent_id: str, settings: ScanSettings, run: CommandRunner
) -> Path | None:
    """The log file that owns ``agent_id``, by the fleet's own assignment.

    A scan assigns every file at most once and lets the longest id claim a
    suffixed one, so ``worker-helper.log`` belongs to ``worker-helper`` when
    that is a worktree and ``worker`` has no log of its own. Resolving with the
    requested id alone cannot see the owner: it hands ``worker`` the helper's
    log, which the scan never gave it and which the operator did not ask for.
    The worktrees are the fleet's id set, so the assignment runs over them; the
    requested id joins them, so an id the root no longer holds still resolves
    against the log directory as it always did.
    """
    names = [path.name for path in await _worktree_paths(settings, run)]
    if agent_id not in names:
        names.append(agent_id)
    return assign_log_files(
        names, settings.logs_dir, max_files=settings.max_log_files
    ).get(agent_id)


async def read_agent_logs(
    agent_id: str,
    settings: ScanSettings | None = None,
    *,
    max_lines: int = 200,
    max_bytes: int = 32768,
    run: CommandRunner | None = None,
) -> AgentLogs:
    """Return a bounded, redacted slice of one agent's log."""
    if settings is None:
        from src.config.settings import get_agent_fleet_config

        settings = ScanSettings.from_config(get_agent_fleet_config())
    if not valid_agent_id(agent_id):
        raise AgentLogNotFound("invalid agent id")
    log_path = await _resolve_log_path(agent_id, settings, run or _default_runner)
    if log_path is None:
        raise AgentLogNotFound("no log for that agent")
    raw = read_text_tail(log_path, max_bytes)
    # Redact the tail as one text before it is split: a PEM block spans several
    # lines, so a line-at-a-time pass never sees the BEGIN/END pair that bounds
    # it and hands back the key body. The read is already bounded by max_bytes,
    # so this pass leaves the length alone and the per-line cap below is what
    # shapes the response.
    redacted = sanitize_log_text(
        bytes(raw).decode("utf-8", errors="replace"), max_length=0
    )
    lines = [sanitize_log_text(line, max_length=500) for line in redacted.splitlines()]
    keep = max(0, max_lines)
    return AgentLogs(
        agent_id=agent_id,
        log_file=log_path.name,
        lines=lines[-keep:] if keep else [],
        truncated=len(lines) > keep,
    )
