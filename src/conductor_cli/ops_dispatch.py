"""Dispatch launches and orphaned Worker processes, seen from ``cond``.

``cond down`` and ``cond restart`` stop the backend. A dispatch Worker the
backend started (``scripts/dispatch_launch.sh`` and its process group) does
not die with the backend: it is reparented to init and keeps working in its
line worktree. This module finds those launches before the backend stops,
waits for them, or stops their process groups when asked to.

A launch belongs to this deployment only when its command is the configured
launcher script (``CONDUCTOR_DISPATCH_LAUNCH_SCRIPT``, or
``scripts/dispatch_launch.sh`` under ``CONDUCTOR_DISPATCH_REPO`` or this
checkout, as the checkout ``.env`` sets them for the backend), it leads its
own process group, which is how the dispatcher spawns it, and its parent is
this deployment's backend, or init with a cwd under this deployment's
worktree roots. Nothing else is ever signalled.

The process table is injectable: a system with ``processes()`` returns the
``ps -o pid=,ppid=,pgid=,etime=,command=`` text. Without it ``ps`` runs
through ``system.run``.
"""

from __future__ import annotations

import math
import os
import signal
from dataclasses import dataclass
from pathlib import Path

from .ops_support import OpsError, RealSystem, Settings

LAUNCH_SCRIPT_NAME = "dispatch_launch.sh"
_SHELLS = {"bash", "sh", "zsh", "dash"}
# How long ``--force`` waits after SIGTERM before SIGKILL.
FORCE_GRACE_SECONDS = 10.0
_POLL_SECONDS = 2.0
# How often the wait prints which launches it is still waiting for.
_REPORT_EVERY_SECONDS = 30.0


@dataclass(frozen=True)
class ProcRow:
    pid: int
    ppid: int
    pgid: int
    age_seconds: int
    command: str


@dataclass(frozen=True)
class Launch:
    """A running dispatch launch: the group leader and its group members."""

    pid: int
    pgid: int
    age_seconds: int
    command: str
    members: tuple[int, ...]


def parse_etime(text: str) -> int:
    """``ps`` elapsed time ``[[dd-]hh:]mm:ss`` in seconds; -1 when unreadable."""
    text = text.strip()
    days = 0
    if "-" in text:
        head, text = text.split("-", 1)
        if not head.isdigit():
            return -1
        days = int(head)
    parts = text.split(":")
    if not parts or len(parts) > 3 or not all(part.isdigit() for part in parts):
        return -1
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return days * 86400 + seconds


def parse_ps(text: str) -> list[ProcRow]:
    rows: list[ProcRow] = []
    for line in text.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 5:
            continue
        pid, ppid, pgid, etime, command = fields
        if not (pid.isdigit() and ppid.isdigit() and pgid.isdigit()):
            continue
        rows.append(
            ProcRow(int(pid), int(ppid), int(pgid), parse_etime(etime), command.strip())
        )
    return rows


def process_rows(system: RealSystem) -> list[ProcRow]:
    lister = getattr(system, "processes", None)
    if callable(lister):
        text = lister()
    else:
        result = system.run(["ps", "-axww", "-o", "pid=,ppid=,pgid=,etime=,command="])
        text = result.stdout if result.code == 0 else ""
    return parse_ps(text or "")


_ENV_NAMES = (
    "CONDUCTOR_DISPATCH_LAUNCH_SCRIPT",
    "CONDUCTOR_DISPATCH_REPO",
    "CONDUCTOR_DISPATCH_RUNS_DIR",
    "CONDUCTOR_DISPATCH_ADAPTERS",
)


@dataclass(frozen=True)
class DispatchConfig:
    """Dispatch paths as the backend that ``cond up`` started sees them.

    That backend gets cond's minimal environment plus the checkout ``.env``
    (``load_dotenv``), so the ``.env`` is the source of truth. A different
    value in the operator's shell is reported and ignored.
    """

    launch_script: Path | None
    repo: Path
    runs_dir: Path
    live: bool
    warnings: tuple[str, ...]


def _dotenv(settings: Settings) -> dict[str, str]:
    """The checkout ``.env``, read for the dispatch names only.

    ``load_dotenv`` follows a symlinked ``.env``, so this does too.
    """
    path = settings.repo / ".env"
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    try:
        values = dotenv_values(path)
    except (OSError, ValueError, UnicodeError):
        return {}
    return {
        name: (values.get(name) or "").strip()
        for name in _ENV_NAMES
        if values.get(name) is not None
    }


def _backend_path(settings: Settings, raw: str) -> Path:
    # The backend runs with cwd = the checkout, so a relative path is relative to it.
    path = Path(raw).expanduser()
    return path if path.is_absolute() else settings.repo / path


def dispatch_config(
    settings: Settings, shell: dict[str, str] | None = None
) -> DispatchConfig:
    shell = dict(os.environ) if shell is None else shell
    dotenv = _dotenv(settings)
    warnings: list[str] = []
    for name in _ENV_NAMES:
        mine = (shell.get(name) or "").strip()
        backend = dotenv.get(name, "")
        if mine and mine != backend:
            seen = f"{backend!r} from the checkout .env" if backend else "its default"
            warnings.append(
                f"{name} in this shell ({mine!r}) is not what the backend uses; "
                f"using {seen}"
            )
    launch_raw = dotenv.get("CONDUCTOR_DISPATCH_LAUNCH_SCRIPT", "")
    repo_raw = dotenv.get("CONDUCTOR_DISPATCH_REPO", "")
    runs_raw = dotenv.get("CONDUCTOR_DISPATCH_RUNS_DIR", "")
    home = Path(shell.get("HOME") or Path.home()).expanduser()
    live = dotenv.get("CONDUCTOR_DISPATCH_ADAPTERS", "dev") == "live"
    if live and not launch_raw and not repo_raw:
        # The backend's own default (~/conductor-wt/launch.sh) is not matched on
        # purpose: it is not tied to this checkout.
        warnings.append(
            "live dispatch is on but neither CONDUCTOR_DISPATCH_LAUNCH_SCRIPT nor "
            "CONDUCTOR_DISPATCH_REPO is set in the checkout .env; dispatch launches "
            "cannot be detected"
        )
    return DispatchConfig(
        launch_script=_backend_path(settings, launch_raw) if launch_raw else None,
        repo=_backend_path(settings, repo_raw) if repo_raw else settings.repo,
        runs_dir=_backend_path(settings, runs_raw)
        if runs_raw
        else home / ".conductor" / "dispatch" / "runs",
        live=live,
        warnings=tuple(warnings),
    )


def launch_scripts(
    settings: Settings, config: DispatchConfig | None = None
) -> list[str]:
    """Launcher paths that count as this deployment's, plain and resolved."""
    config = dispatch_config(settings) if config is None else config
    candidates: list[Path] = []
    if config.launch_script is not None:
        candidates.append(config.launch_script)
    for repo in dict.fromkeys([settings.repo, config.repo]):
        candidates.append(repo / "scripts" / LAUNCH_SCRIPT_NAME)
    names: list[str] = []
    for path in candidates:
        for form in (str(path), _resolved(path)):
            if form and form not in names:
                names.append(form)
    return names


def _resolved(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return ""


def _runs_script(command: str, scripts: list[str]) -> bool:
    """The command is ``SCRIPT ...`` or ``<shell> SCRIPT ...``. Paths may hold spaces."""
    for script in scripts:
        if command == script or command.startswith(script + " "):
            return True
        head, _, rest = command.partition(" ")
        if Path(head).name in _SHELLS and (
            rest == script or rest.startswith(script + " ")
        ):
            return True
    return False


def _backend_pid(system: RealSystem, settings: Settings) -> int | None:
    """This deployment's backend pid, only when the process is still ours."""
    from .ops_runtime import MARKER, Component, _owned, _read_pid, pid_path

    pid = _read_pid(pid_path(settings, Component("backend")))
    if pid is None or not _owned(system, pid, settings, MARKER["backend"]):
        return None
    return pid


@dataclass(frozen=True)
class LaunchScan:
    ours: list[Launch]
    # Launcher processes that are not tied to this deployment. Never signalled.
    foreign: list[Launch]


def scan_launches(
    system: RealSystem,
    settings: Settings,
    rows: list[ProcRow] | None = None,
    config: DispatchConfig | None = None,
) -> LaunchScan:
    """Running dispatch launches, split into this deployment's and the rest.

    A launch is a group leader (pid == pgid; the dispatcher starts each one
    in a session of its own) whose command is a launcher script of this
    deployment. It is ours only when its parent is this deployment's backend,
    or its parent is init and its cwd is under this deployment's worktree
    roots (a launch the previous backend left behind). A launch in cond's
    own group or init's is never returned.
    """
    config = dispatch_config(settings) if config is None else config
    rows = process_rows(system) if rows is None else rows
    scripts = launch_scripts(settings, config)
    roots = worktree_roots(settings, config)
    backend = _backend_pid(system, settings)
    own_group = os.getpgrp()
    ours: list[Launch] = []
    foreign: list[Launch] = []
    for row in rows:
        if row.pid != row.pgid or row.pgid <= 1 or row.pgid == own_group:
            continue
        if not _runs_script(row.command, scripts):
            continue
        members = tuple(sorted(other.pid for other in rows if other.pgid == row.pgid))
        launch = Launch(row.pid, row.pgid, row.age_seconds, row.command, members)
        if backend is not None and row.ppid == backend:
            ours.append(launch)
        elif row.ppid == 1 and _under(system.cwd(row.pid), roots):
            ours.append(launch)
        else:
            foreign.append(launch)
    return LaunchScan(ours, foreign)


def dispatch_launches(
    system: RealSystem,
    settings: Settings,
    rows: list[ProcRow] | None = None,
    config: DispatchConfig | None = None,
) -> list[Launch]:
    """This deployment's running dispatch launches (see ``scan_launches``)."""
    return scan_launches(system, settings, rows, config).ours


def _describe(launch: Launch) -> str:
    words = launch.command.split()
    # The task argument is a prompt; print the script and model only.
    shown = " ".join(words[:3]) if words and Path(words[0]).name in _SHELLS else " ".join(words[:2])
    return (
        f"pid={launch.pid} pgid={launch.pgid} age={format_age(launch.age_seconds)} "
        f"processes={len(launch.members)} {shown}"
    )


def format_age(seconds: int) -> str:
    if seconds < 0:
        return "unknown"
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours >= 24:
        return f"{hours // 24}d{hours % 24}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def _seconds(value: float) -> str:
    return f"{value:g}s"


def wait_for_launches(
    system: RealSystem,
    settings: Settings,
    timeout: float,
    *,
    out,
    config: DispatchConfig | None = None,
) -> list[Launch]:
    """Wait until no launch runs or ``timeout`` passes. Return what still runs."""
    launches = dispatch_launches(system, settings, config=config)
    if not launches or timeout <= 0:
        return launches
    print(
        f"[cond] waiting up to {_seconds(timeout)} for {len(launches)} dispatch launch(es):",
        file=out,
    )
    for launch in launches:
        print(f"[cond]   {_describe(launch)}", file=out)
    deadline = system.monotonic() + timeout
    next_report = system.monotonic() + _REPORT_EVERY_SECONDS
    while launches and system.monotonic() < deadline:
        system.sleep(min(_POLL_SECONDS, max(deadline - system.monotonic(), 0.0)))
        launches = dispatch_launches(system, settings, config=config)
        if launches and system.monotonic() >= next_report:
            next_report = system.monotonic() + _REPORT_EVERY_SECONDS
            pids = ", ".join(str(launch.pid) for launch in launches)
            print(f"[cond] still waiting for dispatch launch(es): {pids}", file=out)
    return launches


def _group_alive(system: RealSystem, pgid: int) -> bool:
    return any(row.pgid == pgid for row in process_rows(system))


def _signal(system: RealSystem, pgid: int, sig: int) -> None:
    if pgid <= 1 or pgid == os.getpgrp():
        raise OpsError(f"refusing to signal process group {pgid}")
    sender = getattr(system, "signal_group", None)
    if not callable(sender):
        raise OpsError("this system cannot signal a process group")
    try:
        sender(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        raise OpsError(f"not allowed to signal process group {pgid}") from exc


def stop_launches(
    system: RealSystem,
    settings: Settings,
    launches: list[Launch],
    *,
    grace: float = FORCE_GRACE_SECONDS,
    out,
    config: DispatchConfig | None = None,
) -> None:
    """TERM each launch group, wait ``grace``, then KILL what is left.

    Before each signal the leader is checked again: it must still be one of
    this deployment's launches, so a pid that exited and was reused by
    another program is left alone. A group whose leader has gone is not
    killed; its remaining members are reported instead.
    """
    current = {
        launch.pgid for launch in dispatch_launches(system, settings, config=config)
    }
    targets = [launch for launch in launches if launch.pgid in current]
    for launch in targets:
        print(f"[cond] stopping dispatch launch {_describe(launch)} (TERM)", file=out)
        _signal(system, launch.pgid, signal.SIGTERM)
    deadline = system.monotonic() + grace
    remaining = list(targets)
    while remaining and system.monotonic() < deadline:
        system.sleep(0.25)
        remaining = [launch for launch in remaining if _group_alive(system, launch.pgid)]
    if not remaining:
        return
    still = {
        launch.pgid for launch in dispatch_launches(system, settings, config=config)
    }
    for launch in remaining:
        if launch.pgid in still:
            print(f"[cond] dispatch launch {launch.pid} ignored TERM (KILL)", file=out)
            _signal(system, launch.pgid, signal.SIGKILL)
        elif _group_alive(system, launch.pgid):
            print(
                f"[cond] warning: dispatch launch {launch.pid} exited but processes "
                f"of group {launch.pgid} remain; not killed",
                file=out,
            )


def settle_launches(
    system: RealSystem,
    settings: Settings,
    *,
    action: str,
    wait_seconds: float,
    force: bool,
    no_wait: bool,
    out,
) -> None:
    """Run before the backend stops. Raise to keep the backend running.

    Default: wait up to ``wait_seconds`` for every launch of this deployment
    to finish, and refuse to stop the backend if one is still running then.
    ``force`` stops their launch groups first. ``no_wait`` only warns.
    Launches that are not tied to this deployment are listed, never waited
    for and never signalled.
    """
    if not math.isfinite(wait_seconds) or wait_seconds < 0:
        raise OpsError("--wait-timeout must be a finite number of seconds >= 0")
    config = dispatch_config(settings)
    for warning in config.warnings:
        print(f"[cond] warning: {warning}", file=out)
    scan = scan_launches(system, settings, config=config)
    for launch in scan.foreign:
        print(
            f"[cond] not ours (left alone): {_describe(launch)}",
            file=out,
        )
    launches = scan.ours
    if no_wait:
        if launches:
            print(
                f"[cond] warning: {action} without waiting; "
                f"{len(launches)} dispatch launch(es) keep running as orphans:",
                file=out,
            )
            for launch in launches:
                print(f"[cond]   {_describe(launch)}", file=out)
        return
    if force:
        if launches:
            stop_launches(system, settings, launches, out=out, config=config)
        return
    launches = wait_for_launches(system, settings, wait_seconds, out=out, config=config)
    if launches:
        pids = ", ".join(str(launch.pid) for launch in launches)
        raise OpsError(
            f"dispatch launch(es) still running after {_seconds(wait_seconds)}: {pids}; "
            f"the backend was not stopped. Use --force to stop them or --no-wait to "
            f"{action} anyway"
        )


# ---------------------------------------------------------------------------
# cond doctor: orphan_processes
# ---------------------------------------------------------------------------


def _worker_kind(command: str) -> str:
    words = command.split()
    head = [Path(word).name for word in words[:2]]
    rest = words[1:]
    if "claude" in head and ("-p" in rest or "--print" in rest):
        return "claude -p"
    if "codex" in head and "exec" in rest:
        return "codex exec"
    return ""


def _under(path: str, roots: list[Path]) -> bool:
    if not path:
        return False
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    for root in roots:
        try:
            base = root.resolve()
        except OSError:
            continue
        if resolved == base or base in resolved.parents:
            return True
    return False


def worktree_roots(
    settings: Settings, config: DispatchConfig | None = None
) -> list[Path]:
    config = dispatch_config(settings) if config is None else config
    return [settings.worktree_root, config.runs_dir.parent / "dispatch-worktrees"]


def orphan_processes(
    system: RealSystem, settings: Settings, *, threshold: int | None = None
) -> list[dict[str, object]]:
    """Processes reparented to init that a Worker left behind. Read-only.

    A dispatch launch reparented to init is reported at any age: its backend
    is gone, so nothing watches it, and a relaunch of the same line may run
    beside it (N35). ``threshold`` applies only to a stray ``claude -p`` /
    ``codex exec`` under the worktree roots, which may be a short manual run.
    """
    threshold = settings.orphan_seconds if threshold is None else threshold
    config = dispatch_config(settings)
    scripts = launch_scripts(settings, config)
    roots = worktree_roots(settings, config)
    found: list[dict[str, object]] = []
    for row in process_rows(system):
        if row.ppid != 1:
            continue
        if _runs_script(row.command, scripts):
            kind = "dispatch launch"
            cwd = system.cwd(row.pid)
        else:
            if row.age_seconds < threshold:
                continue
            kind = _worker_kind(row.command)
            if not kind:
                continue
            cwd = system.cwd(row.pid)
            if not _under(cwd, roots):
                continue
        found.append(
            {
                "pid": row.pid,
                "kind": kind,
                "age_seconds": row.age_seconds,
                "age": format_age(row.age_seconds),
                "cwd": cwd,
            }
        )
    return found


def orphan_check(system: RealSystem, settings: Settings) -> dict[str, object]:
    """A warning, never a failure. Nothing is signalled."""
    rows = orphan_processes(system, settings)
    check: dict[str, object] = {
        "name": "orphan_processes",
        "ok": True,
        "warn": bool(rows),
        "threshold_seconds": settings.orphan_seconds,
        "processes": rows,
    }
    if rows:
        check["detail"] = (
            f"{len(rows)} orphaned worker process(es) (dispatch launches at any "
            f"age; claude -p / codex exec older than "
            f"{format_age(settings.orphan_seconds)}); cond does not stop them"
        )
    else:
        check["detail"] = "none"
    warnings = dispatch_config(settings).warnings
    if warnings:
        check["warn"] = True
        check["config_warnings"] = list(warnings)
        if not rows:
            check["detail"] = warnings[0]
    return check
