"""``cond`` command line. Process starts go through the injected system."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

from .deploy_launchd import disable_units, enable_units, install_units, uninstall_units
from .drain import clear_drain, drain_path, read_drain, set_drain
from .drain_owner import clear_stale_fence, set_owned_drain
from .ops_fleet import migrate_v1, render_fleet_toml
from .ops_runtime import (
    backend_argv,
    coordination_lock,
    daemon_argv,
    format_drain,
    format_smoke,
    format_status,
    prepare,
    restart_all,
    restart_daemons,
    smoke_daemons,
    smoke_payload,
    start_all,
    status_map,
    status_ok,
    stop_all,
    tail_logs,
    web_argv,
)
from .ops_support import OpsError, RealSystem, Settings, redact
from .ops_upgrade import backup_database, upgrade


def _print_error(settings: Settings | None, exc: OpsError) -> None:
    secrets = settings.sensitive if settings is not None else []
    print(f"[cond] error: {redact(str(exc), secrets)}", file=sys.stderr)


def _fleet_or_raise(system, settings):
    from .ops_fleet import load_fleet

    return load_fleet(settings)


def _own_child(system: RealSystem, pid: int) -> bool:
    from .ops_support import NOT_OUR_CHILD

    poll = getattr(system, "child_poll", None)
    return poll is not None and poll(pid) is not NOT_OUR_CHILD


def _running(
    system: RealSystem,
    settings: Settings,
    key: str,
    pid: int,
    *,
    own: bool,
    check_start: bool,
) -> bool:
    """Our child is polled; ``os.kill(pid, 0)`` stays true for an unreaped zombie.

    A pid this process adopted is not a child, so it cannot be polled. It is
    running while it is alive and its pid file still names it with the same
    start time.
    """
    from .ops_runtime import adopted_is_ours

    if own:
        return system.child_poll(pid) is None
    return adopted_is_ours(system, settings, key, pid, check_start=check_start)


def _reap_children(system: RealSystem, pids: dict[str, int]) -> None:
    import subprocess

    children = getattr(system, "_children", {})
    for pid in pids.values():
        proc = children.get(pid)
        if proc is None or proc.poll() is None:
            continue
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            continue


def _supervise(system: RealSystem, settings: Settings, pids: dict[str, int]) -> int:
    """Stay in this process. start_all has already released the lifecycle lock.

    Exit when the backend exits, when every process has exited, or when a
    newer lifecycle generation starts. The wait at the end is bounded so a
    live sibling cannot hold the supervisor open.
    """
    from .ops_runtime import _read_generation

    generation = _read_generation(settings)
    print(json.dumps({"supervisor": True, "pids": pids}))
    if os.environ.get("CONDUCTOR_OPS_NO_EXEC") == "1":
        return 0
    own = {key: _own_child(system, pid) for key, pid in pids.items()}
    tick = 0
    while _read_generation(settings) == generation:
        # ps runs about once a second per adopted pid, not on every poll.
        check_start = tick % 4 == 0
        running = {
            key: _running(
                system, settings, key, pid, own=own[key], check_start=check_start
            )
            for key, pid in pids.items()
        }
        if "backend" in running and not running["backend"]:
            break
        if running and not any(running.values()):
            break
        tick += 1
        time.sleep(0.25)
    _reap_children(system, pids)
    return 0


def cmd_up(system: RealSystem, settings: Settings, *, foreground: bool = False) -> int:
    fleet = _fleet_or_raise(system, settings)
    pids = start_all(system, settings, fleet)
    if foreground:
        return _supervise(system, settings, pids)
    report = status_map(system, settings, fleet)
    print(format_status(report), end="")
    return 0 if status_ok(report) else 1


def _settle_dispatch(
    system: RealSystem, settings: Settings, action: str, options: dict[str, object] | None
) -> None:
    """Dispatch Workers outlive the backend. Settle them before it stops."""
    from .ops_dispatch import settle_launches

    options = options or {}
    wait = options.get("wait_timeout")
    settle_launches(
        system,
        settings,
        action=action,
        wait_seconds=float(settings.dispatch_wait_seconds if wait is None else wait),
        force=bool(options.get("force")),
        no_wait=bool(options.get("no_wait")),
        out=sys.stderr,
    )


def cmd_down(
    system: RealSystem, settings: Settings, options: dict[str, object] | None = None
) -> int:
    fleet = _fleet_or_raise(system, settings)
    with _stop_fence(system, settings, "stop") as fresh:
        _fenced_settle(system, settings, "stop", options, fresh=fresh)
        stop_all(system, settings, fleet)
    print("stopped")
    return 0


#: The drain reason ``cond down`` / ``restart`` / ``upgrade`` write while they
#: stop the backend (N35). Only a flag with this reason is cleared again.
STOP_FENCE_REASON = "cond {action}: stop requested"


@contextlib.contextmanager
def _stop_fence(system: RealSystem, settings: Settings, action: str):
    """Hold new dispatch launches from the moment a backend stop is requested.

    Without it a launch could start between the launch check and the backend
    stop and be orphaned by it (N35: three such launches ran for 24 minutes
    next to the new backend's relaunches). The block also runs under the
    lifecycle lock, so no other ``cond`` start/stop interleaves with it.

    Yields True when this command set the drain flag just now (the caller
    then scans once more after ``DRAIN_GRACE_SECONDS``). An operator's drain
    that was already set is left alone and yields False. The flag is
    cleared on every exit path, also SIGTERM / SIGHUP, but only while it
    still carries this command's reason: a ``cond drain`` set meanwhile stays.
    A held launch in the old backend never spawned; the resumed drive starts
    it again.
    """
    home = settings.conductor_home
    reason = STOP_FENCE_REASON.format(action=action)
    with coordination_lock(settings), _signals_as_errors() as quiet:
        fresh = False
        _clear_stale_fence(home)
        if read_drain(home) is None:
            set_owned_drain(home, reason)
            fresh = True
            print(
                f"[cond] drain: on while the backend {action}s; "
                "no new dispatch launch starts",
                file=sys.stderr,
            )
        try:
            yield fresh
        finally:
            quiet()
            if fresh:
                flag = read_drain(home)
                if flag is not None and flag.get("reason") == reason:
                    with contextlib.suppress(OSError):
                        clear_drain(home)
                    print("[cond] drain: off", file=sys.stderr)


def _clear_stale_fence(home: Path) -> None:
    """Remove a fence a killed cond left behind (owner pid gone), and say so.

    Only a cond-owned flag (owner pid + cond's reason) is removed; an
    operator's ``cond drain`` is never touched.
    """
    removed = clear_stale_fence(home)
    if removed is not None:
        print(
            f"[cond] drain: cleared a stale fence ({removed.get('reason')}, "
            f"pid {removed.get('owner_pid')} is gone)",
            file=sys.stderr,
        )


def _fenced_settle(
    system: RealSystem,
    settings: Settings,
    action: str,
    options: dict[str, object] | None,
    *,
    fresh: bool,
) -> None:
    """Settle launches; after a fresh fence, scan again after the grace period.

    A launch that passed its drain check just before the fence was set can
    take a moment to show up as a process. The second scan shares the first
    one's ``--wait-timeout`` budget; ``--force`` stops what it finds again.
    ``--no-wait`` keeps its single warning scan.
    """
    options = dict(options or {})
    wait = options.get("wait_timeout")
    budget = float(settings.dispatch_wait_seconds if wait is None else wait)
    start = system.monotonic()
    _settle_dispatch(system, settings, action, options)
    if not fresh or options.get("no_wait"):
        return
    from .ops_dispatch import _backend_pid

    if _backend_pid(system, settings) is None:
        # No backend of ours runs: nothing can start a launch now.
        return
    system.sleep(DRAIN_GRACE_SECONDS)
    left = max(start + budget - system.monotonic(), 0.0)
    _settle_dispatch(system, settings, action, {**options, "wait_timeout": left})


def cmd_status(system: RealSystem, settings: Settings, *, json_mode: bool) -> int:
    fleet = _fleet_or_raise(system, settings)
    report = status_map(system, settings, fleet)
    if json_mode:
        print(json.dumps(report, indent=2))
    else:
        print(format_status(report), end="")
    return 0 if status_ok(report) else 1


def cmd_restart_daemons(
    system: RealSystem, settings: Settings, fleet=None
) -> int:
    """Reload the daemons only: no backend/web stop and no launch wait."""
    fleet = fleet if fleet is not None else _fleet_or_raise(system, settings)
    report = restart_daemons(system, settings, fleet)
    print(format_status(report), end="")
    return 0 if status_ok(report) else 1


def cmd_restart(
    system: RealSystem,
    settings: Settings,
    options: dict[str, object] | None = None,
    *,
    target: str = "all",
    drain: bool = False,
) -> int:
    if target not in {"all", "daemons"}:
        raise OpsError("restart target must be 'all' or 'daemons'")
    if drain and target == "daemons":
        raise OpsError("--drain applies only to a full restart")
    if drain and (options or {}).get("no_wait"):
        raise OpsError("--drain waits for running launches; it cannot use --no-wait")
    fleet = _fleet_or_raise(system, settings)
    if drain:
        return _restart_drained(system, settings, fleet, options)
    if target == "daemons":
        options = options or {}
        if (
            options.get("force")
            or options.get("no_wait")
            or options.get("wait_timeout") is not None
        ):
            raise OpsError(
                "--force, --no-wait, and --wait-timeout apply only to a full restart"
            )
        return cmd_restart_daemons(system, settings, fleet)
    with _stop_fence(system, settings, "restart") as fresh:
        _fenced_settle(system, settings, "restart", options, fresh=fresh)
        restart_all(system, settings, fleet)
    report = status_map(system, settings, fleet)
    print(format_status(report), end="")
    return 0 if status_ok(report) else 1


#: After the drain is set: a launch that passed its drain check just before
#: has this long to show up as a process before the second scan.
DRAIN_GRACE_SECONDS = 2.0
_BUSY_POLL_SECONDS = 1.0


class _Interrupted(OpsError):
    """SIGTERM / SIGHUP during ``restart --drain``: unwind and undrain."""


@contextlib.contextmanager
def _signals_as_errors():
    """Map SIGTERM and SIGHUP to ``_Interrupted`` for the block.

    A dropped SSH session (SIGHUP) or a ``kill`` (SIGTERM) would otherwise
    end the process without running ``finally`` and leave the drain on. Only
    the first signal raises, and none raises once the caller has called the
    yielded ``quiet()`` (its cleanup has begun), so the cleanup finishes. A
    signal swallowed that way is raised after the cleanup. The previous
    handlers come back on every exit path.
    """
    names = [name for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name)]
    state = {"raised": False, "quiet": False, "pending": 0}

    def handler(signum, _frame):
        if state["raised"] or state["quiet"]:
            state["pending"] = state["pending"] or signum
            return
        state["raised"] = True
        raise _Interrupted(
            f"interrupted by {signal.Signals(signum).name}; nothing more is restarted"
        )

    def quiet() -> None:
        state["quiet"] = True

    previous: dict[int, object] = {}
    try:
        for name in names:
            signum = getattr(signal, name)
            previous[signum] = signal.signal(signum, handler)
    except ValueError:
        # Not the main thread: signals cannot be handled here.
        pass
    try:
        yield quiet
    finally:
        state["quiet"] = True
        for signum, old in previous.items():
            signal.signal(signum, old)
    if state["pending"] and not state["raised"]:
        name = signal.Signals(state["pending"]).name
        raise _Interrupted(f"interrupted by {name} while finishing")


def _wait_for_busy(system: RealSystem, settings: Settings, deadline: float) -> None:
    """Wait until no busy marker (G2, G5, review, merge, launch call) is live."""
    from .activity import busy_entries, count_entries

    entries = busy_entries(settings.conductor_home)
    if entries:
        print(
            f"[cond] waiting for dispatch work: {_format_busy(count_entries(entries))}",
            file=sys.stderr,
        )
    while entries and system.monotonic() < deadline:
        system.sleep(min(_BUSY_POLL_SECONDS, max(deadline - system.monotonic(), 0.0)))
        entries = busy_entries(settings.conductor_home)
    if entries:
        # Name the files: a leaked marker is then one ``rm`` away, and the
        # operator can tell it from real work by its pid.
        paths = ", ".join(str(entry.get("path") or "?") for entry in entries)
        raise OpsError(
            f"dispatch work still busy: {_format_busy(count_entries(entries))}; "
            f"markers: {paths}; "
            "the backend was not stopped (--force skips this wait)"
        )


def _require_backend_drained(system: RealSystem, settings: Settings) -> None:
    """Refuse when the running backend answers but does not see the drain.

    It reads the flag under its own ``CONDUCTOR_HOME``; when that is not the
    CLI's, the drain set here holds none of its launches, and a restart would
    cut them off. A backend that does not answer keeps the old behaviour.
    """
    from .ops_runtime import _backend_drain_status

    view = _backend_drain_status(system, settings, running=True)
    if not view.get("reachable") or view.get("drained") is not False:
        return
    why = (
        "CONDUCTOR_HOME differs, so its launches are not held"
        if view.get("home_mismatch")
        else "its launches are not held"
    )
    raise OpsError(
        "the running backend does not see the drain "
        f"(cli={view.get('cli_path')} backend={view.get('path') or 'unknown'}); "
        f"{why}; nothing was restarted"
    )


def _format_busy(counts: dict[str, int]) -> str:
    return ", ".join(f"{kind}={count}" for kind, count in sorted(counts.items()))


def _restart_drained(
    system: RealSystem, settings: Settings, fleet, options: dict[str, object] | None
) -> int:
    """Drain, wait for running launches and busy work, restart, undrain (N64).

    After the drain is set the launch scan runs twice, before and after a
    short grace period, so a launch that passed its drain check just before
    cannot start after the wait and be orphaned by the restart. Then it waits
    for the busy markers (G2, G5, review, merge, launch calls) in the same
    ``--wait-timeout`` budget.

    The drain is cleared whatever happens -- also when the wait times out, in
    which case nothing was restarted and the command exits 1, and on SIGTERM
    or SIGHUP -- so a failed deploy never leaves dispatch quietly holding
    every launch. A drain that was already set before this command
    (``cond drain``) is the operator's and is left on.
    """
    home = settings.conductor_home
    options = dict(options or {})
    wait = options.get("wait_timeout")
    budget = float(settings.dispatch_wait_seconds if wait is None else wait)
    with _signals_as_errors() as quiet:
        _clear_stale_fence(home)
        already = read_drain(home)
        if already is None:
            set_owned_drain(home, "cond restart --drain")
            print("[cond] drain: on; no new dispatch launch starts", file=sys.stderr)
        else:
            print(
                "[cond] drain: already on; it stays on after the restart",
                file=sys.stderr,
            )
        try:
            _require_backend_drained(system, settings)
            deadline = system.monotonic() + budget
            _settle_dispatch(system, settings, "restart", options)
            system.sleep(DRAIN_GRACE_SECONDS)
            left = max(deadline - system.monotonic(), 0.0)
            _settle_dispatch(system, settings, "restart", {**options, "wait_timeout": left})
            if options.get("force"):
                print(
                    "[cond] --force: not waiting for busy markers (G2, G5, review, "
                    "merge, launch); running work may be cut off",
                    file=sys.stderr,
                )
            else:
                _wait_for_busy(system, settings, deadline)
            restart_all(system, settings, fleet)
        finally:
            quiet()
            if already is None:
                clear_drain(home)
                print("[cond] drain: off", file=sys.stderr)
    report = status_map(system, settings, fleet)
    print(format_status(report), end="")
    return 0 if status_ok(report) else 1


def cmd_drain(settings: Settings, reason: str) -> int:
    """Hold new dispatch launches and fix rounds; running work continues."""
    flag = read_drain(settings.conductor_home)
    if flag is None:
        flag = set_drain(settings.conductor_home, reason)
    print(format_drain(flag))
    return 0


def cmd_undrain(settings: Settings) -> int:
    try:
        removed = clear_drain(settings.conductor_home)
    except OSError as exc:
        path = drain_path(settings.conductor_home)
        raise OpsError(
            f"cannot clear the drain flag {path}: {exc.strerror or type(exc).__name__}"
            "; it is not a file, remove it by hand"
        ) from None
    print("drain: off" if removed else "drain: off (was not set)")
    return 0


def cmd_smoke(
    system: RealSystem,
    settings: Settings,
    *,
    timeout: float | None = None,
    json_mode: bool = False,
) -> int:
    """One read-only Ticket per configured daemon; READY or a failure reason."""
    fleet = _fleet_or_raise(system, settings)
    try:
        results = smoke_daemons(system, settings, fleet, timeout=timeout)
    except KeyboardInterrupt:
        # smoke_daemons has already stopped and archived its Tickets.
        print("smoke: interrupted; smoke Tickets were stopped", file=sys.stderr)
        return 130
    if json_mode:
        print(json.dumps(smoke_payload(results, settings), indent=2))
    elif not results:
        print("daemon: none configured")
    else:
        print(format_smoke(results, settings), end="")
    return 0 if all(result.ready for result in results) else 1


def cmd_logs(settings: Settings, component: str | None, lines: int) -> int:
    print(tail_logs(settings, component, lines), end="")
    return 0


def cmd_upgrade(
    system: RealSystem,
    settings: Settings,
    ref: str,
    restore_db: bool,
    options: dict[str, object] | None = None,
) -> int:
    fleet = _fleet_or_raise(system, settings)
    # Before the backup and checkout: a refusal leaves code and database as
    # they are, and a rollback's stop never runs with launches still going.
    with _stop_fence(system, settings, "upgrade") as fresh:
        _fenced_settle(system, settings, "upgrade", options, fresh=fresh)
        result = upgrade(system, settings, fleet, ref=ref, restore_db=restore_db)
    print(
        f"upgraded ref={result['ref']} previous={result['previous']} backup={result['backup']}"
    )
    if result.get("alembic_before") or result.get("alembic_after"):
        print(f"alembic before: {result['alembic_before'] or 'unknown'}")
        print(f"alembic after: {result['alembic_after'] or 'unknown'}")
    return 0


def cmd_backup(system: RealSystem, settings: Settings) -> int:
    path = backup_database(system, settings)
    print(f"backup: {path}")
    return 0


def foreground_spec(
    settings: Settings, role: str, provider: str, ident: str
) -> tuple[list[str], dict[str, str], str]:
    from .ops_fleet import DaemonSpec, load_fleet, provider_environment
    from .ops_runtime import _minimal_env, _token, daemon_rollout_env

    if role == "backend":
        return backend_argv(settings), _minimal_env(settings), str(settings.repo)
    if role == "web":
        argv, cwd, _marker = web_argv(settings)
        return argv, _minimal_env(settings), str(cwd)
    if role != "daemon":
        raise OpsError("foreground role must be backend, web, or daemon")
    fleet = load_fleet(settings)
    chosen = None
    for daemon in fleet.daemons:
        if daemon.provider == provider and (not ident or daemon.ident == ident):
            chosen = daemon
            break
    if chosen is None and provider and ident:
        chosen = DaemonSpec(provider, ident)
    if chosen is None:
        raise OpsError(
            "foreground daemon needs --provider and --id, or a daemons.toml entry"
        )
    from .ops_runtime import _keychain_lookup

    env = provider_environment(
        settings,
        chosen,
        keychain_lookup=_keychain_lookup(RealSystem(), settings)
        if chosen.keychain
        else None,
    )
    env.update(daemon_rollout_env(settings))
    env["CONDUCTOR_DAEMON_TOKEN"] = _token(settings, chosen)
    env["CONDUCTOR_REPO_ROOT"] = str(settings.repo)
    env["CONDUCTOR_HOME"] = str(settings.conductor_home)
    env["CONDUCTOR_WORKTREE_ROOT"] = str(settings.worktree_root)
    return daemon_argv(settings, chosen), env, str(settings.repo)


def _pid_is_live(system: RealSystem, pid: int) -> bool:
    """os.kill sees this process. A fake process table is only the fallback."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        try:
            return bool(system.alive(pid))
        except OSError:
            return False
    return True


def _require_running_backend(settings: Settings) -> None:
    """The launchd daemon may exec only behind a live backend from this checkout.

    An empty command plus a live pid is the same-process login probe: ps was
    not stubbed, and /ready must not be required or that probe never execs.
    A popped process with a command line has to be our backend and ready.
    """
    from .ops_runtime import MARKER, Component, _read_pid, pid_path

    pid = _read_pid(pid_path(settings, Component("backend")))
    system = RealSystem()
    if pid is None or not _pid_is_live(system, pid):
        raise OpsError("backend is not running")
    command = system.command(pid)
    if not command.strip():
        return
    if MARKER["backend"] not in command:
        raise OpsError("backend is not owned by this checkout")
    try:
        cwd = Path(system.cwd(pid)).resolve()
    except OSError as exc:
        raise OpsError("backend is not owned by this checkout") from exc
    if cwd != settings.repo.resolve():
        raise OpsError("backend is not owned by this checkout")
    code, body = system.http_get(settings.base_url + "/ready", timeout=5)
    if code != 200 or '"ready"' not in body:
        raise OpsError("backend is not ready")


def _record_foreground(
    settings: Settings, role: str, provider: str, ident: str
) -> None:
    """The launchd process is this process. Record it before exec replaces us."""
    from .ops_fleet import DaemonSpec, load_fleet
    from .ops_runtime import (
        Component,
        _record_registration,
        _start_time,
        _touch_heartbeat,
        _write_pid,
        pid_path,
    )

    me = os.getpid()
    # exec keeps the pid and the start time, so a supervisor can verify both.
    started = _start_time(RealSystem(), me)
    if role == "backend":
        _write_pid(pid_path(settings, Component("backend")), me, started)
        return
    if role == "web":
        _write_pid(pid_path(settings, Component("web")), me, started)
        return
    fleet = load_fleet(settings)
    chosen = next(
        (
            daemon
            for daemon in fleet.daemons
            if daemon.provider == provider and (not ident or daemon.ident == ident)
        ),
        None,
    )
    if chosen is None and provider and ident:
        chosen = DaemonSpec(provider, ident)
    if chosen is None:
        raise OpsError(
            "foreground daemon needs --provider and --id, or a daemons.toml entry"
        )
    from .ops_cli_update import daemon_is_blocked, latest_results

    if daemon_is_blocked(chosen.provider, latest_results(settings), settings):
        raise OpsError(f"{chosen.provider} is broken; refusing to start its daemon")
    backend_pid = pid_path(settings, Component("backend"))
    if not backend_pid.is_file():
        raise OpsError("backend is not running")
    _write_pid(
        pid_path(settings, Component("daemon", chosen.provider, chosen.ident)),
        me,
        started,
    )
    _touch_heartbeat(settings, chosen)
    _record_registration(settings, chosen, me)


def cmd_foreground(settings: Settings, role: str, provider: str, ident: str) -> int:
    # Backend starts first. Only the daemon waits for the shared CLI update,
    # and only after a live backend is already recorded.
    if role == "daemon":
        _require_running_backend(settings)
        from .ops_cli_update import startup_updates

        startup_updates(RealSystem(), settings)
    argv, env, cwd = foreground_spec(settings, role, provider, ident)
    _record_foreground(settings, role, provider, ident)
    if os.environ.get("CONDUCTOR_OPS_NO_EXEC") == "1":
        print(json.dumps({"argv": argv, "cwd": cwd, "pid": os.getpid()}))
        return 0
    # execvpe does not return on success. A stub or a failed exec must not
    # leave this process in the service directory.
    previous = os.getcwd()
    os.chdir(cwd)
    try:
        os.execvpe(argv[0], argv, env)
    finally:
        if os.getcwd() != previous:
            os.chdir(previous)
    return 1


def _wait_seconds(text: str) -> float:
    """A finite, non-negative number of seconds. ``inf`` and ``nan`` are refused."""
    try:
        value = float(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(
            f"must be a finite number of seconds >= 0, not {text!r}"
        )
    return value


def _add_dispatch_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--force",
        action="store_true",
        help="stop running dispatch launches (TERM, 10 s grace, KILL) first",
    )
    group.add_argument(
        "--no-wait",
        action="store_true",
        help="do not wait for dispatch launches; they keep running as orphans",
    )
    parser.add_argument(
        "--wait-timeout",
        type=_wait_seconds,
        default=None,
        metavar="SECONDS",
        help="how long to wait for dispatch launches to finish "
        "(default CONDUCTOR_DISPATCH_WAIT_SECONDS or 600; 0 does not wait)",
    )


def _dispatch_options(args: argparse.Namespace) -> dict[str, object]:
    return {
        "force": bool(getattr(args, "force", False)),
        "no_wait": bool(getattr(args, "no_wait", False)),
        "wait_timeout": getattr(args, "wait_timeout", None),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cond", description="Start, stop, and upgrade Conductor"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser(
        "up", help="start the backend, web console, and configured daemons"
    )
    up.add_argument(
        "--foreground",
        action="store_true",
        help="supervise this run in-process instead of returning",
    )
    down = sub.add_parser(
        "down", help="stop the backend, web console, and configured daemons"
    )
    _add_dispatch_options(down)
    start = sub.add_parser("start", help="alias of up")
    start.add_argument(
        "--foreground",
        action="store_true",
        help="supervise this run in-process instead of returning",
    )
    stop = sub.add_parser("stop", help="alias of down")
    _add_dispatch_options(stop)

    status = sub.add_parser("status", help="show backend, web, and daemon state")
    status.add_argument("--json", action="store_true")

    restart = sub.add_parser(
        "restart", help="rolling restart of the backend, web console, and daemons"
    )
    restart.add_argument(
        "target",
        nargs="?",
        choices=("all", "daemons"),
        default="all",
        help="'daemons' restarts only the configured daemons, one at a time",
    )
    _add_dispatch_options(restart)
    restart.add_argument(
        "--drain",
        action="store_true",
        help="hold new dispatch launches, wait for running ones, restart, undrain",
    )

    drain = sub.add_parser(
        "drain", help="hold new dispatch launches and fix rounds (deploy prep)"
    )
    drain.add_argument("--reason", default="", help="recorded in the drain flag")
    sub.add_parser("undrain", help="clear the drain flag; held launches continue")

    smoke = sub.add_parser(
        "smoke", help="send one read-only Ticket to each configured daemon"
    )
    smoke.add_argument(
        "--timeout",
        type=_wait_seconds,
        default=None,
        metavar="SECONDS",
        help="how long to wait for all smoke Tickets "
        "(default CONDUCTOR_SMOKE_TIMEOUT or 180)",
    )
    smoke.add_argument("--json", action="store_true")

    logs = sub.add_parser("logs", help="show logs from the conductor log directory")
    logs.add_argument("-n", "--lines", type=int, default=80)
    logs.add_argument("component", nargs="?", help="backend, web, or a daemon provider")

    doctor = sub.add_parser(
        "doctor",
        help="check port 5170, daemons, sandbox policy files, orphaned workers, and schema drift",
    )
    doctor.add_argument("--json", action="store_true")

    upgrade_parser = sub.add_parser(
        "upgrade", help="back up, check out a ref, migrate, rebuild, and restart"
    )
    upgrade_parser.add_argument(
        "--ref", default="HEAD", help="tag, branch, or commit (detached HEAD is ok)"
    )
    upgrade_parser.add_argument(
        "--restore-db",
        action="store_true",
        help="on failure, restore the database backup taken at the start",
    )
    _add_dispatch_options(upgrade_parser)

    clean = sub.add_parser(
        "clean", help="list stale worktrees, registrations, and dead pid files"
    )
    clean.add_argument("--apply", action="store_true")
    clean.add_argument("--json", action="store_true")
    clean.add_argument("--root")
    clean.add_argument("--base", default="main")

    launchd = sub.add_parser("launchd", help="render com.conductor.* LaunchAgents")
    launchd_sub = launchd.add_subparsers(dest="launchd_command", required=True)
    launchd_sub.add_parser("install")
    launchd_sub.add_parser("enable")
    launchd_sub.add_parser("disable")
    launchd_sub.add_parser("uninstall")
    render_codexbar = launchd_sub.add_parser(
        "render-codexbar",
        help="generate the CodexBar snapshot plist; does not load launchd",
    )
    render_codexbar.add_argument(
        "--output",
        help="write the plist here; default is stdout. The Planner loads it.",
    )

    daemons = sub.add_parser("daemons", help="fleet configuration helpers")
    daemons_sub = daemons.add_subparsers(dest="daemons_command", required=True)
    migrate = daemons_sub.add_parser(
        "migrate", help="import v1-daemon.sh and v1-daemons.txt (dry run unless --apply)"
    )
    migrate.add_argument("--script", required=True)
    migrate.add_argument("--list", required=True)
    migrate.add_argument("--output")
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="write daemons.toml; without this flag only print the result",
    )

    sub.add_parser(
        "backup", help="write a custom-format database backup and validate it"
    )

    foreground = sub.add_parser(
        "foreground", help="run one component in the foreground for launchd"
    )
    foreground.add_argument("role", choices=("backend", "web", "daemon"))
    foreground.add_argument("--provider", default="")
    foreground.add_argument("--id", default="")

    cli = sub.add_parser("cli", help="show or update worker CLIs")
    cli_sub = cli.add_subparsers(dest="cli_command", required=True)
    cli_status = cli_sub.add_parser(
        "status", help="show install, version, and last result"
    )
    cli_status.add_argument("--json", action="store_true")
    cli_update = cli_sub.add_parser(
        "update", help="update worker CLIs from the built-in table"
    )
    cli_update.add_argument("--only", default="")
    cli_update.add_argument("--dry-run", action="store_true")
    cli_update.add_argument("--force", action="store_true")
    cli_update.add_argument("--json", action="store_true")

    setup = sub.add_parser(
        "setup", help="detect installed CLIs and optionally write agents.toml"
    )
    setup.add_argument("--json", action="store_true")
    setup.add_argument(
        "--write",
        action="store_true",
        help="enable every installed CLI as official (role selection is deferred)",
    )
    setup.add_argument(
        "--force",
        action="store_true",
        help="replace an existing agents.toml",
    )

    agents_cmd = sub.add_parser("agents", help="configure worker CLIs in agents.toml")
    agents_sub = agents_cmd.add_subparsers(dest="agents_command", required=True)
    agents_list = agents_sub.add_parser("list", help="show agents.toml plus detection")
    agents_list.add_argument("--json", action="store_true")
    agents_set = agents_sub.add_parser("set", help="enable, disable, or set a backend note")
    agents_set.add_argument("cli")
    agents_set.add_argument("--enable", action="store_true")
    agents_set.add_argument("--disable", action="store_true")
    agents_set.add_argument("--mode", choices=("official", "custom"))
    agents_set.add_argument("--backend-note")
    agents_set.add_argument("--backend-vendor")
    agents_set.add_argument("--passthrough-env", action="append")
    agents_login = agents_sub.add_parser(
        "login", help="hand the terminal to the CLI's own official login command"
    )
    agents_login.add_argument("cli")
    agents_login.add_argument("--for-worker", action="store_true")
    agents_login.add_argument("--worker-home")
    agents_login.add_argument("--json", action="store_true")
    return parser


#: ``cond <verb>`` words that belong to the operator CLI and are handed to
#: ``conductor <verb>`` untouched (they need the server API, not the stack).
#: They are deliberately not subparsers here: ``scripts/cond`` forwards every
#: ``build_parser()`` verb to this module, and its case table is the place
#: that routes these words straight to ``conductor``.
FORWARDED_VERBS = frozenset({"apps"})


def forward_to_conductor(argv: list[str]) -> int:
    from .cli import main as conductor_main

    return conductor_main(argv)


def main(argv: list[str] | None = None, system: RealSystem | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] in FORWARDED_VERBS:
        return forward_to_conductor(raw)
    parser = build_parser()
    args = parser.parse_args(argv)
    system = system or RealSystem()
    settings: Settings | None = None
    try:
        system, settings, _fleet = prepare(system)
        command = args.command
        # N35: a cond SIGKILLed mid-stop leaves its fence set; any later cond
        # run (status, doctor, drain, ...) lifts it once the owner is gone.
        _clear_stale_fence(settings.conductor_home)
        if command in {"up", "start"}:
            return cmd_up(system, settings, foreground=bool(args.foreground))
        if command in {"down", "stop"}:
            return cmd_down(system, settings, _dispatch_options(args))
        if command == "status":
            return cmd_status(system, settings, json_mode=bool(args.json))
        if command == "restart":
            return cmd_restart(
                system,
                settings,
                _dispatch_options(args),
                target=args.target,
                drain=bool(args.drain),
            )
        if command == "drain":
            return cmd_drain(settings, str(args.reason or ""))
        if command == "undrain":
            return cmd_undrain(settings)
        if command == "smoke":
            return cmd_smoke(
                system,
                settings,
                timeout=args.timeout,
                json_mode=bool(args.json),
            )
        if command == "logs":
            return cmd_logs(settings, args.component, args.lines)
        if command == "doctor":
            from .doctor import run_doctor

            code, text = run_doctor(system, json_mode=bool(args.json))
            print(text, end="")
            return code
        if command == "upgrade":
            return cmd_upgrade(
                system,
                settings,
                args.ref,
                bool(args.restore_db),
                _dispatch_options(args),
            )
        if command == "clean":
            from .clean import apply_clean, clean_report, format_clean

            root = Path(args.root).expanduser() if args.root else None
            report = clean_report(system, settings, root=root, base_ref=args.base)
            if args.apply:
                report["applied"] = apply_clean(
                    system, settings, report, base_ref=args.base
                )
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(format_clean(report), end="")
            return 0
        if command == "launchd":
            from .ops_fleet import load_fleet

            if args.launchd_command == "render-codexbar":
                from .deploy_launchd import render_codexbar_plist

                text = render_codexbar_plist(settings)
                output = getattr(args, "output", None)
                if output:
                    path = Path(output).expanduser()
                    path.write_text(text, encoding="utf-8")
                    os.chmod(path, 0o600)
                    print(str(path))
                else:
                    print(text, end="" if text.endswith("\n") else "\n")
                return 0
            fleet = load_fleet(settings)
            if args.launchd_command == "install":
                written = install_units(system, settings, fleet, enabled=False)
            elif args.launchd_command == "enable":
                written = enable_units(system, settings, fleet)
            elif args.launchd_command == "disable":
                disable_units(system, settings, fleet)
                print("disabled launchd jobs")
                return 0
            elif args.launchd_command == "uninstall":
                uninstall_units(system, settings, fleet)
                print("uninstalled launchd jobs")
                return 0
            else:
                raise OpsError(
                    "usage: cond launchd install|enable|disable|uninstall|render-codexbar"
                )
            print("\n".join(str(path) for path in written))
            return 0
        if command == "daemons" and args.daemons_command == "migrate":
            output = (
                Path(args.output).expanduser() if args.output else settings.daemons_file
            )
            if args.apply:
                fleet = migrate_v1(Path(args.script), Path(args.list), output)
                print(f"migrated {len(fleet.daemons)} daemons to {output}")
            else:
                fleet = migrate_v1(Path(args.script), Path(args.list))
                print(render_fleet_toml(fleet), end="")
            return 0
        if command == "backup":
            return cmd_backup(system, settings)
        if command == "foreground":
            return cmd_foreground(settings, args.role, args.provider, args.id)
        if command == "cli":
            from .ops_cli_update import cli_status, update_clis

            if args.cli_command == "status":
                rows = cli_status(system, settings)
                if args.json:
                    print(json.dumps(rows, indent=2))
                else:
                    for row in rows:
                        print(
                            f"{row['name']}: install={row['install']} path={row['path']} "
                            f"current={row['current'] or 'unknown'} latest={row['latest']} "
                            f"result={row['result']}"
                        )
                return 0
            if args.cli_command == "update":
                rows = update_clis(
                    system,
                    settings,
                    only=args.only or None,
                    dry_run=bool(args.dry_run),
                    force=bool(args.force),
                )
                if args.json:
                    print(json.dumps(rows, indent=2))
                else:
                    for row in rows:
                        print(f"{row['name']}: {row['result']}")
                if any(row.get("result") in {"failed", "broken"} for row in rows):
                    return 1
                return 0
            raise OpsError("usage: cond cli status|update")
        if command == "setup":
            from .onboarding import agents_path, detect, setup_from_detect

            if args.write and agents_path(settings).exists() and not args.force:
                print("agents.toml already exists; pass --force", file=sys.stderr)
                return 2
            payload = (
                setup_from_detect(system, settings, force=bool(args.force))
                if args.write
                else {"detected": detect(system, settings)}
            )
            print(json.dumps(payload, indent=2 if args.json else None))
            return 0
        if command == "agents":
            from .onboarding import detect, load_agents, login, upsert

            if args.agents_command == "list":
                payload = {
                    "agents": {
                        name: {
                            "enabled": row.enabled,
                            "mode": row.mode,
                            "backend_note": row.backend_note,
                            "backend_vendor": row.backend_vendor,
                            "passthrough_env": row.passthrough_env,
                        }
                        for name, row in load_agents(settings).items()
                    },
                    "detected": detect(system, settings),
                }
                print(json.dumps(payload, indent=2 if args.json else None))
                return 0
            if args.agents_command == "set":
                if args.enable and args.disable:
                    raise OpsError("use --enable or --disable, not both")
                enabled = True if args.enable else False if args.disable else None
                upsert(
                    settings,
                    args.cli,
                    enabled=enabled,
                    mode=args.mode,
                    backend_note=args.backend_note,
                    backend_vendor=args.backend_vendor,
                    passthrough_env=args.passthrough_env,
                )
                return 0
            if args.agents_command == "login":
                worker_home = Path(args.worker_home) if args.worker_home else None
                payload = login(
                    system,
                    settings,
                    args.cli,
                    for_worker=bool(args.for_worker),
                    worker_home=worker_home,
                )
                print(json.dumps(payload, indent=2 if args.json else None))
                return 0 if payload.get("ok") else 1
            raise OpsError("usage: cond agents list|set|login")
        raise OpsError(f"unknown command {command}")
    except OpsError as exc:
        _print_error(settings, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
