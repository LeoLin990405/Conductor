"""Start, stop, and report the backend, web console, and configured daemons."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .daemon_capability import GRANT_VERIFIER_MODE_ENV
from .drain import drain_path, read_drain
from .log_rotation import rotate_log
from .ops_fleet import (
    DaemonSpec,
    FleetFile,
    load_fleet,
    provider_environment,
    read_secrets,
)
from .ops_support import (
    Completed,
    OpsError,
    RealSystem,
    Settings,
    ensure_private_dir,
    load_settings,
    redact,
)

MARKER = {
    "backend": "server.py",
    "web": "vite",
    "web-static": "ops_static",
    "daemon": "conductor_local_daemon.py",
}

# flock is per-process on macOS, so threads also take the matching thread lock.
# The file lock is taken only on the outermost acquire: unlocking one fd
# would drop the process lock while an outer holder still needs it.
# Coordination is the outer lock. The process lock is never taken first.
_COORD_LOCK = threading.RLock()
_COORD_DEPTH = threading.local()
_PROCESS_LOCK = threading.RLock()
_PROCESS_DEPTH = threading.local()


@contextmanager
def _exclusive_lock(
    settings: Settings,
    thread_lock: threading.RLock,
    depth_local: threading.local,
    name: str,
):
    depth = getattr(depth_local, "depth", 0)
    depth_local.depth = depth + 1
    try:
        if depth:
            yield
            return
        ensure_private_dir(settings.run_dir)
        path = settings.run_dir / name
        thread_lock.acquire()
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)
        finally:
            thread_lock.release()
    finally:
        depth_local.depth = depth


@contextmanager
def coordination_lock(settings: Settings):
    """Serialize a whole start, stop, restart, or upgrade.

    Reentrant on the calling thread because upgrade calls restart there.
    A daemon callback may run on another thread and must not take this lock:
    the start that invoked it already holds it.
    """
    with _exclusive_lock(settings, _COORD_LOCK, _COORD_DEPTH, "lifecycle.lock"):
        yield


@contextmanager
def process_lock(settings: Settings):
    """Pid file and spawn only. Never hold this across a port probe."""
    with _exclusive_lock(settings, _PROCESS_LOCK, _PROCESS_DEPTH, "process.lock"):
        yield


@dataclass
class Component:
    kind: str
    provider: str = ""
    ident: str = ""

    @property
    def label(self) -> str:
        if self.kind == "daemon":
            return f"{self.provider}/{self.ident}"
        return self.kind


def pid_path(settings: Settings, component: Component) -> Path:
    if component.kind == "backend":
        return settings.run_dir / "backend.pid"
    if component.kind == "web":
        return settings.run_dir / "web.pid"
    safe = "".join(
        ch if ch.isalnum() or ch in "._-" else "_"
        for ch in f"{component.provider}-{component.ident}"
    )
    return settings.run_dir / f"daemon-{safe}.pid"


def log_path(settings: Settings, component: Component) -> Path:
    if component.kind == "backend":
        return settings.log_dir / "backend.log"
    if component.kind == "web":
        return settings.log_dir / "web.log"
    safe = "".join(
        ch if ch.isalnum() or ch in "._-" else "_"
        for ch in f"{component.provider}-{component.ident}"
    )
    return settings.log_dir / f"daemon-{safe}.log"


def _pid_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").strip().splitlines()
    except OSError:
        return []


def _read_pid(path: Path) -> int | None:
    lines = _pid_lines(path)
    text = lines[0].strip() if lines else ""
    if not text.isdigit():
        return None
    return int(text)


def _read_pid_start(path: Path) -> str:
    """Start time recorded beside the pid. Older pid files have none."""
    lines = _pid_lines(path)
    return lines[1].strip() if len(lines) > 1 else ""


def _start_time(system: RealSystem, pid: int) -> str:
    probe = getattr(system, "start_time", None)
    if not callable(probe):
        return ""
    try:
        return str(probe(pid) or "")
    except (OpsError, OSError):
        return ""


def _write_pid(path: Path, pid: int, started: str = "") -> None:
    ensure_private_dir(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    body = f"{pid}\n{started}\n" if started else f"{pid}\n"
    temporary.write_text(body, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def supervised_component(key: str) -> Component | None:
    """The component behind a key of the pids map that start_all returns."""
    if key in {"backend", "web"}:
        return Component(key)
    if key.startswith("daemon:") and "/" in key:
        provider, ident = key[len("daemon:") :].split("/", 1)
        return Component("daemon", provider, ident)
    return None


def adopted_is_ours(
    system: RealSystem,
    settings: Settings,
    key: str,
    pid: int,
    *,
    check_start: bool,
) -> bool:
    """An adopted pid counts while it is alive and its pid file still names it.

    The pid file's start time must match the process. A pid file without one
    falls back to the command and working-directory check.
    """
    component = supervised_component(key)
    if component is None:
        return bool(system.alive(pid))
    path = pid_path(settings, component)
    if _read_pid(path) != pid or not system.alive(pid):
        return False
    recorded = _read_pid_start(path)
    if not recorded:
        if not check_start:
            return True
        marker = MARKER["daemon"] if component.kind == "daemon" else MARKER[component.kind]
        if component.kind == "web" and settings.web_mode == "static":
            marker = MARKER["web-static"]
        return _owned(system, pid, settings, marker)
    if not check_start:
        return True
    current = _start_time(system, pid)
    # An empty or failed probe is unknown. Do not treat it as a match.
    return bool(current) and current == recorded


def _owned(system: RealSystem, pid: int, settings: Settings, marker: str) -> bool:
    if not system.alive(pid):
        return False
    command = system.command(pid)
    cwd = system.cwd(pid)
    if marker not in command and not (marker == "vite" and "ops_static" in command):
        return False
    try:
        resolved = Path(cwd).resolve()
    except OSError:
        return False
    return resolved in {settings.repo.resolve(), (settings.repo / "web").resolve()}


def _minimal_env(settings: Settings) -> dict[str, str]:
    source = os.environ
    env = {
        name: source[name]
        for name in (
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "LANG",
            "LC_ALL",
            "TMPDIR",
            "TERM",
        )
        if source.get(name)
    }
    env.setdefault("LANG", "en_US.UTF-8")
    env.setdefault("LC_ALL", "en_US.UTF-8")
    env["CONDUCTOR_REPO_ROOT"] = str(settings.repo)
    env["CONDUCTOR_HOME"] = str(settings.conductor_home)
    env["CONDUCTOR_WORKTREE_ROOT"] = str(settings.worktree_root)
    env["PYTHONPATH"] = str(settings.repo)
    return env


#: Rollout settings a daemon reads from its own environment (N58). The
#: provider allow-list drops them, so they are passed on here.
DAEMON_ROLLOUT_ENV = (GRANT_VERIFIER_MODE_ENV,)


def daemon_rollout_env(
    settings: Settings, source: dict[str, str] | None = None
) -> dict[str, str]:
    """The rollout settings a daemon started by ``cond`` gets.

    The shell that runs ``cond`` wins; the secrets file is the fallback, and
    the only source for a launchd daemon, whose environment carries no shell
    variables. ``cond doctor`` reads the daemons' mode from here too.
    """
    env = os.environ if source is None else source
    secrets_file = getattr(settings, "secrets_file", None)
    stored: dict[str, str] = {}
    if secrets_file is not None:
        try:
            stored = read_secrets(secrets_file)
        except (OpsError, OSError, UnicodeError):
            stored = {}
    passed: dict[str, str] = {}
    for name in DAEMON_ROLLOUT_ENV:
        value = str(env.get(name) or "").strip() or str(stored.get(name) or "").strip()
        if value:
            passed[name] = value
    return passed


def backend_argv(settings: Settings) -> list[str]:
    # Host is explicit even if a future server default changes.
    return [
        str(settings.python),
        str(settings.repo / "server.py"),
        "--host",
        "127.0.0.1",
        "--port",
        str(settings.backend_port),
    ]


def web_argv(settings: Settings) -> tuple[list[str], Path, str]:
    if settings.web_mode == "static":
        dist = settings.repo / "web" / "dist"
        return (
            [
                str(settings.python),
                "-m",
                "src.conductor_cli.ops_static",
                "--host",
                "127.0.0.1",
                "--port",
                str(settings.web_port),
                "--directory",
                str(dist),
                "--backend",
                f"http://127.0.0.1:{settings.backend_port}",
            ],
            settings.repo,
            "web-static",
        )
    return (
        [
            "pnpm",
            "exec",
            "vite",
            "preview",
            "--host",
            "127.0.0.1",
            "--port",
            str(settings.web_port),
            "--strictPort",
        ],
        settings.repo / "web",
        "web",
    )


def daemon_argv(settings: Settings, daemon: DaemonSpec) -> list[str]:
    return [
        str(settings.python),
        str(settings.repo / "scripts" / "conductor_local_daemon.py"),
        "--base-url",
        settings.base_url,
        "--provider",
        daemon.provider,
        "--daemon-id",
        daemon.ident,
        "--workspace-policy",
        str(settings.workspace_policy),
        "--isolation-root",
        str(settings.isolation_root),
        "--cwd",
        str(settings.repo),
    ]


def _token(settings: Settings, daemon: DaemonSpec) -> str:
    path = settings.token_dir / f"{daemon.ident}.token"
    if path.is_symlink() or not path.is_file():
        raise OpsError(f"missing daemon token file for {daemon.provider}")
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise OpsError(f"could not read daemon token: {type(exc).__name__}") from exc
    if not token:
        raise OpsError(f"daemon token is empty for {daemon.provider}")
    settings.sensitive.append(token)
    return token


def _keychain_lookup(system: RealSystem, settings: Settings):
    def lookup(service: str, account: str) -> str:
        result = system.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            cwd=settings.repo,
        )
        if result.code != 0:
            raise OpsError(f"keychain lookup failed for {service}")
        return result.stdout.strip()

    return lookup


def _start_process(
    system: RealSystem,
    settings: Settings,
    component: Component,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    marker: str,
) -> int:
    path = pid_path(settings, component)
    existing = _read_pid(path)
    if existing is not None and _owned(system, existing, settings, marker):
        return existing
    # The port probe stays outside the process lock. start_backend can be
    # called on its own; two of those must both reach port_open. start_all
    # holds the coordination lock around this whole function, so two full
    # starts do not. Do not take coordination here.
    if component.kind == "backend" and system.port_open(
        settings.backend_host, settings.backend_port
    ):
        raise OpsError(
            f"port {settings.backend_port} is held by an unexpected process; refusing to start a second backend"
        )
    if component.kind == "web" and system.port_open(
        settings.web_host, settings.web_port
    ):
        raise OpsError(
            f"port {settings.web_port} is held by an unexpected process; refusing to start a second web console"
        )
    with process_lock(settings):
        existing = _read_pid(path)
        if existing is not None and _owned(system, existing, settings, marker):
            return existing
        ensure_private_dir(settings.log_dir)
        # Backend, web, and every daemon open their log through this one
        # call. Rename a file the previous process already filled. The
        # process started below does not inherit the log descriptor:
        # popen hands it a pipe, and the pump rotates when the live file
        # crosses the limit. A test double that only writes the path still
        # has to see the rename above, or a full file would keep growing.
        component_log = log_path(settings, component)
        rotate_log(
            component_log,
            max_bytes=settings.log_max_bytes,
            backup_count=settings.log_backups,
        )
        pid = system.popen(
            argv,
            cwd=cwd,
            env=env,
            log_path=component_log,
            max_bytes=settings.log_max_bytes,
            backups=settings.log_backups,
        )
        _write_pid(path, pid, _start_time(system, pid))
        return pid


def _http_is_up(code: int, body: str) -> bool:
    return code == 200 and (
        '"healthy"' in body or '"ready"' in body or '"status": "ready"' in body
    )


def _wait_http_result(
    system: RealSystem, settings: Settings, url: str, timeout: float
) -> tuple[bool, str]:
    deadline = system.monotonic() + timeout
    last = "no response"
    while system.monotonic() < deadline:
        code, body = system.http_get(url, timeout=1)
        if _http_is_up(code, body):
            return True, body
        # An error response carries its body now, and that body is
        # untrusted and may be long: one readable line is enough.
        last = _plain_reason(body, limit=80) or f"HTTP {code}"
        system.sleep(0.05)
    return False, last


def _wait_http(
    system: RealSystem, settings: Settings, url: str, timeout: float = 30.0
) -> None:
    ok, last = _wait_http_result(system, settings, url, timeout)
    if not ok:
        raise OpsError(f"timed out waiting for {url}: {last}")


def _ready_timeout() -> float:
    raw = os.environ.get("CONDUCTOR_READY_TIMEOUT", "60")
    try:
        value = float(raw)
    except ValueError:
        return 60.0
    return 60.0 if value <= 0 else value


def _names_from_ready_body(body: str) -> list[str]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    checks = payload.get("checks")
    if not isinstance(checks, dict):
        return []
    beat = checks.get("daemon_heartbeats")
    if not isinstance(beat, dict):
        return []
    names: list[str] = []
    for key in ("missing", "stale", "unreadable"):
        values = beat.get(key)
        if isinstance(values, list):
            names.extend(str(item) for item in values if str(item))
    return names


def _daemons_without_heartbeat(
    settings: Settings, fleet: FleetFile, body: str
) -> list[str]:
    named = _names_from_ready_body(body)
    if named:
        return named
    missing: list[str] = []
    for daemon in fleet.daemons:
        path = settings.heartbeat_dir / f"{daemon.ident}.json"
        if path.is_symlink() or not path.is_file():
            missing.append(daemon.ident)
    return missing


def _await_full_ready(
    system: RealSystem, settings: Settings, fleet: FleetFile
) -> None:
    """Wait for every daemon heartbeat. A timeout leaves running daemons up."""
    url = settings.base_url + "/ready"
    ok, body = _wait_http_result(system, settings, url, _ready_timeout())
    if ok:
        return
    names = _daemons_without_heartbeat(settings, fleet, body)
    listed = ", ".join(names) if names else "none reported"
    raise OpsError(
        f"full /ready timed out; daemons without a heartbeat: {listed}; "
        "the backend, web console and started daemons were left running "
        "(cond status shows them, cond down stops them)"
    )


def _process_alive(system: RealSystem, pid: int) -> bool:
    """os.kill sees a real process. A fake process table is the fallback."""
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


def _require_this_backend(system: RealSystem, settings: Settings, pid: int) -> None:
    """Daemons start only behind the backend this call just recorded."""
    recorded = _read_pid(pid_path(settings, Component("backend")))
    if recorded != pid or not _process_alive(system, pid):
        raise OpsError("backend is not ready")
    # Heartbeats do not exist yet. The backend scope is the gate before daemons.
    _wait_http(system, settings, settings.base_url + "/ready?scope=backend")


def start_backend(system: RealSystem, settings: Settings) -> int:
    component = Component("backend")
    pid = _start_process(
        system,
        settings,
        component,
        backend_argv(settings),
        settings.repo,
        _minimal_env(settings),
        MARKER["backend"],
    )
    _wait_http(system, settings, settings.base_url + "/health")
    return pid


def start_web(system: RealSystem, settings: Settings) -> int:
    argv, cwd, marker_key = web_argv(settings)
    if settings.web_mode == "static" and not (settings.repo / "web" / "dist").is_dir():
        raise OpsError(
            "web static mode needs web/dist; run make web (pnpm build in web/), "
            "cond upgrade, or use preview mode"
        )
    return _start_process(
        system,
        settings,
        Component("web"),
        argv,
        cwd,
        web_env(settings),
        MARKER[marker_key],
    )


def web_env(settings: Settings) -> dict[str, str]:
    """The console's environment: cond's minimal one plus its backend.

    ``vite preview`` proxies ``/api/v1`` and ``/ws/v1`` to
    ``VITE_PROXY_BACKEND``. The minimal environment drops it, so without this
    the console would reach whatever answers on the vite default port, not
    the backend this ``cond`` started.
    """
    env = _minimal_env(settings)
    env["CONDUCTOR_BACKEND_PORT"] = str(settings.backend_port)
    env["VITE_PROXY_BACKEND"] = f"http://127.0.0.1:{settings.backend_port}"
    return env


def _touch_heartbeat(settings: Settings, daemon: DaemonSpec) -> None:
    ensure_private_dir(settings.heartbeat_dir)
    from datetime import UTC, datetime

    payload = {
        "id": daemon.ident,
        "provider": daemon.provider,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path = settings.heartbeat_dir / f"{daemon.ident}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def start_daemon(system: RealSystem, settings: Settings, daemon: DaemonSpec) -> int:
    if not settings.workspace_policy.is_file():
        raise OpsError(f"missing daemon workspace policy: {settings.workspace_policy}")
    ensure_private_dir(settings.isolation_root)
    env = provider_environment(
        settings,
        daemon,
        keychain_lookup=_keychain_lookup(system, settings) if daemon.keychain else None,
    )
    env.update(daemon_rollout_env(settings))
    env.update(
        {
            "CONDUCTOR_REPO_ROOT": str(settings.repo),
            "CONDUCTOR_HOME": str(settings.conductor_home),
            "CONDUCTOR_WORKTREE_ROOT": str(settings.worktree_root),
            "CONDUCTOR_DAEMON_TOKEN": _token(settings, daemon),
            "PYTHONPATH": str(settings.repo),
        }
    )
    pid = _start_process(
        system,
        settings,
        Component("daemon", daemon.provider, daemon.ident),
        daemon_argv(settings, daemon),
        settings.repo,
        env,
        MARKER["daemon"],
    )
    _touch_heartbeat(settings, daemon)
    _record_registration(settings, daemon, pid)
    return pid


def _record_registration(settings: Settings, daemon: DaemonSpec, pid: int) -> None:
    ensure_private_dir(settings.registry_file.parent)
    payload = {"daemons": []}
    if settings.registry_file.is_file():
        try:
            loaded = json.loads(settings.registry_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("daemons"), list):
                payload = loaded
        except (OSError, json.JSONDecodeError):
            payload = {"daemons": []}
    rows = [
        row
        for row in payload["daemons"]
        if not (isinstance(row, dict) and row.get("id") == daemon.ident)
    ]
    heartbeat = settings.heartbeat_dir / f"{daemon.ident}.json"
    at = ""
    if heartbeat.is_file():
        try:
            at = str(json.loads(heartbeat.read_text(encoding="utf-8")).get("at") or "")
        except (OSError, json.JSONDecodeError):
            at = ""
    rows.append(
        {"id": daemon.ident, "provider": daemon.provider, "pid": pid, "heartbeat": at}
    )
    settings.registry_file.write_text(
        json.dumps({"daemons": rows}, indent=2), encoding="utf-8"
    )
    os.chmod(settings.registry_file, 0o600)


def _stop_component(
    system: RealSystem, settings: Settings, component: Component, marker: str
) -> None:
    path = pid_path(settings, component)
    pid = _read_pid(path)
    if pid is None:
        return
    if not _owned(system, pid, settings, marker):
        if system.alive(pid):
            raise OpsError(
                f"refusing to stop {component.label}: process is not owned by this checkout"
            )
        path.unlink(missing_ok=True)
        return
    system.terminate(pid)
    deadline = system.monotonic() + 10
    while system.monotonic() < deadline:
        if not system.alive(pid):
            path.unlink(missing_ok=True)
            return
        system.sleep(0.05)
    if _owned(system, pid, settings, marker):
        system.kill(pid)
        system.sleep(0.05)
    if system.alive(pid):
        raise OpsError(f"{component.label} did not exit")
    path.unlink(missing_ok=True)


def _stop_unlocked(system: RealSystem, settings: Settings, fleet: FleetFile) -> None:
    for daemon in reversed(fleet.daemons):
        _stop_component(
            system,
            settings,
            Component("daemon", daemon.provider, daemon.ident),
            MARKER["daemon"],
        )
    web_marker = (
        MARKER["web-static"] if settings.web_mode == "static" else MARKER["web"]
    )
    _stop_component(system, settings, Component("web"), web_marker)
    _stop_component(system, settings, Component("backend"), MARKER["backend"])
    _bootout_installed(system, settings)


def _generation_path(settings: Settings) -> Path:
    return settings.run_dir / "lifecycle.generation"


def _read_generation(settings: Settings) -> int:
    """Current run generation. A missing file is generation 0. No lifecycle lock."""
    try:
        text = _generation_path(settings).read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    return int(text) if text.isdigit() else 0


def _bump_generation(settings: Settings) -> int:
    """Advance the run generation. Caller holds the lifecycle lock."""
    ensure_private_dir(settings.run_dir)
    path = _generation_path(settings)
    nxt = _read_generation(settings) + 1
    temporary = path.with_suffix(".tmp")
    temporary.write_text(f"{nxt}\n", encoding="utf-8")
    os.replace(temporary, path)
    return nxt


def stop_all(system: RealSystem, settings: Settings, fleet: FleetFile) -> None:
    with coordination_lock(settings):
        _bump_generation(settings)
        _stop_unlocked(system, settings, fleet)


def _bootout_installed(system: RealSystem, settings: Settings) -> None:
    marker = settings.conductor_home / "run" / "launchd-labels.json"
    if not marker.is_file():
        return
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    labels = payload.get("labels") if isinstance(payload, dict) else None
    if not isinstance(labels, list):
        return
    for label in labels:
        if not isinstance(label, str) or not label.startswith("com.conductor."):
            continue
        system.run(
            ["launchctl", "bootout", f"{settings.launchd_domain}/{label}"],
            cwd=settings.repo,
        )


def _cli_results(system: RealSystem, settings: Settings) -> list[dict[str, object]]:
    from .ops_cli_update import startup_updates

    return startup_updates(system, settings)


def _cli_status_rows(settings: Settings) -> list[dict[str, object]]:
    """Latest attempt plus persisted health. A skip must not hide broken."""
    from .ops_cli_update import latest_results, persisted_health

    health = persisted_health(settings)
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for row in latest_results(settings):
        item = dict(row)
        name = item.get("name")
        if isinstance(name, str):
            seen.add(name)
            if name in health:
                item["health"] = health[name]
        rows.append(item)
    for name, state in health.items():
        if name in seen:
            continue
        rows.append({"name": name, "result": "unchecked", "health": state})
    return rows


def _daemon_blocked(
    provider: str, results: list[dict[str, object]], settings: Settings
) -> bool:
    from .ops_cli_update import daemon_is_blocked

    return daemon_is_blocked(provider, results, settings)


def _daemon_key(daemon: DaemonSpec) -> str:
    return f"{daemon.provider}/{daemon.ident}"


def _providers_for_cli(name: object) -> set[str]:
    from .ops_cli_update import _PROVIDER_CLI

    if not isinstance(name, str):
        return set()
    return {
        provider for provider, cli_name in _PROVIDER_CLI.items() if cli_name == name
    }


def _start_daemon_if_ready(
    system: RealSystem,
    settings: Settings,
    fleet: FleetFile,
    pids: dict[str, int],
    started: set[str],
    daemon: DaemonSpec,
    results: list[dict[str, object]],
    generation: int,
) -> None:
    # Caller holds the per-start gate. Do not take coordination_lock: this
    # runs from on_finished, which may be another thread, while start_all
    # still holds that lock. A down bumps the generation; discard this start.
    if _read_generation(settings) != generation:
        return
    key = _daemon_key(daemon)
    if key in started or _daemon_blocked(daemon.provider, results, settings):
        return
    pids[f"daemon:{key}"] = start_daemon(system, settings, daemon)
    started.add(key)


def _start_daemons_as_updates_finish(
    system: RealSystem,
    settings: Settings,
    fleet: FleetFile,
    pids: dict[str, int],
    generation: int,
) -> None:
    """Start each daemon when its own CLI update finishes, not after the slowest."""
    from .ops_cli_update import update_clis

    started: set[str] = set()
    gate = threading.Lock()

    def on_finished(row: dict[str, object]) -> None:
        matched = _providers_for_cli(row.get("name"))
        for daemon in fleet.daemons:
            if daemon.provider not in matched:
                continue
            with gate:
                _start_daemon_if_ready(
                    system, settings, fleet, pids, started, daemon, [row], generation
                )

    results = update_clis(system, settings, on_finished=on_finished)
    for daemon in fleet.daemons:
        with gate:
            _start_daemon_if_ready(
                system, settings, fleet, pids, started, daemon, results, generation
            )


def _start_backend_or_clean_up(system: RealSystem, settings: Settings) -> int:
    """Start the backend and pass ``/ready?scope=backend``.

    When that fails, a backend this call started is stopped again, so a failed
    ``cond up`` does not leave a half-started stack behind. A backend that was
    already running before this call is left alone, and the error says so.
    """
    component = Component("backend")
    path = pid_path(settings, component)
    before = _read_pid(path)
    preexisting = before is not None and _owned(
        system, before, settings, MARKER["backend"]
    )
    try:
        backend_pid = start_backend(system, settings)
        _require_this_backend(system, settings, backend_pid)
    except OpsError as exc:
        if preexisting:
            raise OpsError(
                f"{exc}; the backend was already running before this cond up "
                "and was left running (cond down stops it)"
            ) from exc
        started = _read_pid(path)
        if started is None or started == before:
            raise
        try:
            _stop_component(system, settings, component, MARKER["backend"])
        except OpsError as stop_exc:
            raise OpsError(
                f"{exc}; the backend this cond up started (pid {started}) "
                f"could not be stopped ({stop_exc}) and is still running; "
                "run cond down"
            ) from exc
        raise OpsError(f"{exc}; stopped the backend this cond up started") from exc
    return backend_pid


def start_all(
    system: RealSystem, settings: Settings, fleet: FleetFile
) -> dict[str, int]:
    """Start backend, web, and daemons. The coordination lock covers all of it."""
    with coordination_lock(settings):
        ensure_private_dir(settings.run_dir)
        ensure_private_dir(settings.log_dir)
        generation = _read_generation(settings)
        backend_pid = _start_backend_or_clean_up(system, settings)
        pids = {
            "backend": backend_pid,
            "web": start_web(system, settings),
        }
        _start_daemons_as_updates_finish(system, settings, fleet, pids, generation)
        _await_full_ready(system, settings, fleet)
        return pids


def restart_all(
    system: RealSystem, settings: Settings, fleet: FleetFile
) -> dict[str, int]:
    """Rolling restart: backend first, then each daemon as its CLI update finishes."""
    with coordination_lock(settings):
        generation = _bump_generation(settings)
        _stop_component(system, settings, Component("backend"), MARKER["backend"])
        start_backend(system, settings)
        web_marker = (
            MARKER["web-static"] if settings.web_mode == "static" else MARKER["web"]
        )
        _stop_component(system, settings, Component("web"), web_marker)
        start_web(system, settings)
        for daemon in fleet.daemons:
            _stop_component(
                system,
                settings,
                Component("daemon", daemon.provider, daemon.ident),
                MARKER["daemon"],
            )
        _start_daemons_as_updates_finish(system, settings, fleet, {}, generation)
        return status_map(system, settings, fleet)


def restart_daemons(
    system: RealSystem, settings: Settings, fleet: FleetFile
) -> dict[str, object]:
    """Rolling restart of the configured daemons only.

    The backend and web console keep their pids: this reloads a daemon config
    without a full ``cond up``. It also does not update worker CLIs (a restart
    is not an upgrade) and does not wait for dispatch launches.
    """
    with coordination_lock(settings):
        for daemon in fleet.daemons:
            component = Component("daemon", daemon.provider, daemon.ident)
            _stop_component(system, settings, component, MARKER["daemon"])
            start_daemon(system, settings, daemon)
        return status_map(system, settings, fleet)


def _stop_started(
    system: RealSystem, settings: Settings, started: list[Component]
) -> None:
    with coordination_lock(settings):
        for component in reversed(started):
            marker = (
                MARKER["daemon"]
                if component.kind == "daemon"
                else MARKER[component.kind]
            )
            if component.kind == "web" and settings.web_mode == "static":
                marker = MARKER["web-static"]
            try:
                _stop_component(system, settings, component, marker)
            except OpsError:
                pass


def restore_previous_runtime(
    system: RealSystem, settings: Settings, fleet: FleetFile
) -> tuple[int, str]:
    """Stop whatever is running, then start the tree that is checked out now."""
    started: list[Component] = []
    with coordination_lock(settings):
        _stop_unlocked(system, settings, fleet)
        try:
            start_backend(system, settings)
            started.append(Component("backend"))
            start_web(system, settings)
            started.append(Component("web"))
            known = _cli_status_rows(settings)
            for daemon in fleet.daemons:
                if _daemon_blocked(daemon.provider, known, settings):
                    continue
                start_daemon(system, settings, daemon)
                started.append(Component("daemon", daemon.provider, daemon.ident))
            code, body = system.http_get(settings.base_url + "/ready", timeout=5)
            if code != 200 or '"ready"' not in body:
                raise OpsError(
                    "ready check failed after restoring the previous runtime"
                )
            return code, body
        except Exception as exc:
            _stop_started(system, settings, started)
            if isinstance(exc, OpsError):
                raise
            raise OpsError(
                f"previous runtime was not restored: {type(exc).__name__}"
            ) from exc


def status_map(
    system: RealSystem, settings: Settings, fleet: FleetFile
) -> dict[str, object]:
    def one(component: Component, marker: str) -> dict[str, object]:
        pid = _read_pid(pid_path(settings, component))
        running = pid is not None and _owned(system, pid, settings, marker)
        return {"running": running, "pid": pid if running else None}

    web_marker = (
        MARKER["web-static"] if settings.web_mode == "static" else MARKER["web"]
    )
    daemons = []
    for daemon in fleet.daemons:
        item = one(Component("daemon", daemon.provider, daemon.ident), MARKER["daemon"])
        item["provider"] = daemon.provider
        item["id"] = daemon.ident
        daemons.append(item)
    backend = one(Component("backend"), MARKER["backend"])
    healthy = False
    if backend["running"]:
        code, body = system.http_get(settings.base_url + "/health", timeout=2)
        healthy = code == 200 and "healthy" in body
    return {
        "backend": {
            **backend,
            "healthy": healthy,
            "host": "127.0.0.1",
            "port": settings.backend_port,
        },
        "web": {
            **one(Component("web"), web_marker),
            "host": "127.0.0.1",
            "port": settings.web_port,
        },
        "daemons": daemons,
        "logs": str(settings.log_dir),
        "clis": _cli_status_rows(settings),
        "drain": _drain_status(settings),
        "drain_backend": _backend_drain_status(
            system, settings, running=bool(backend["running"])
        ),
    }


def _drain_status(settings: Settings) -> dict[str, object]:
    flag = read_drain(settings.conductor_home)
    if flag is None:
        return {"drained": False}
    return {"drained": True, **flag}


OPS_STATUS_PATH = "/api/v1/dispatch/status/ops"


def _same_path(left: str, right: str) -> bool:
    try:
        return os.path.realpath(left) == os.path.realpath(right)
    except (OSError, ValueError):
        return left == right


def _backend_drain_status(
    system: RealSystem, settings: Settings, *, running: bool = True
) -> dict[str, object]:
    """The drain and busy state as the running backend sees them.

    The backend reads the flag under its own ``CONDUCTOR_HOME``; when that is
    not the CLI's, ``cond drain`` would not hold that backend's launches.
    """
    cli_path = str(drain_path(settings.conductor_home))
    unknown: dict[str, object] = {"reachable": False, "drained": None, "cli_path": cli_path}
    if not running:
        return unknown
    try:
        code, body = system.http_get(settings.base_url + OPS_STATUS_PATH, timeout=2)
    except OpsError:
        return unknown
    if code != 200:
        return unknown
    try:
        payload = json.loads(body)
    except ValueError:
        return unknown
    drain = payload.get("drain") if isinstance(payload, dict) else None
    if not isinstance(drain, dict) or not isinstance(drain.get("drained"), bool):
        return unknown
    backend_path = str(drain.get("path") or "")
    busy = payload.get("busy")
    return {
        "reachable": True,
        "drained": drain["drained"],
        "set_at": str(drain.get("set_at") or ""),
        "reason": str(drain.get("reason") or ""),
        "path": backend_path,
        "cli_path": cli_path,
        "conductor_home": str(payload.get("conductor_home") or ""),
        "home_mismatch": bool(backend_path) and not _same_path(backend_path, cli_path),
        "busy": {
            str(kind): count
            for kind, count in (busy.items() if isinstance(busy, dict) else ())
            if isinstance(count, int) and count > 0
        },
    }


def format_backend_drain(view: dict[str, object]) -> list[str]:
    if not view.get("reachable"):
        return ["drain (backend): unknown"]
    lines = ["drain (backend): " + ("on" if view.get("drained") else "off")]
    busy = view.get("busy")
    if isinstance(busy, dict) and busy:
        lines.append(
            "busy (backend): "
            + ", ".join(f"{kind}={count}" for kind, count in sorted(busy.items()))
        )
    if view.get("home_mismatch"):
        lines.append(
            f"warning: CONDUCTOR_HOME mismatch: cli={view.get('cli_path')} "
            f"backend={view.get('path')}"
        )
    return lines


def format_drain(flag: dict[str, object] | None) -> str:
    """One status line. ``flag`` is the drain flag, or a ``_drain_status`` map."""
    if not flag or flag.get("drained") is False:
        return "drain: off"
    since = str(flag.get("set_at") or "")
    reason = " ".join(str(flag.get("reason") or "").split())[:200]
    text = "drain: on (no new dispatch launches)"
    if since:
        text += f" since={since}"
    if reason:
        text += f" reason={reason}"
    return text


def format_status(report: dict[str, object]) -> str:
    backend = report["backend"]
    web = report["web"]
    assert isinstance(backend, dict) and isinstance(web, dict)
    lines = [
        "backend: {state} host={host} port={port}{pid}".format(
            state="healthy"
            if backend.get("healthy")
            else ("up" if backend.get("running") else "down"),
            host=backend.get("host"),
            port=backend.get("port"),
            pid=f" pid={backend['pid']}" if backend.get("pid") else "",
        ),
        "web: {state} host={host} port={port}{pid}".format(
            state="up" if web.get("running") else "down",
            host=web.get("host"),
            port=web.get("port"),
            pid=f" pid={web['pid']}" if web.get("pid") else "",
        ),
    ]
    daemons = report.get("daemons")
    if isinstance(daemons, list) and daemons:
        for daemon in daemons:
            if not isinstance(daemon, dict):
                continue
            state = "up" if daemon.get("running") else "down"
            pid = f" pid={daemon['pid']}" if daemon.get("pid") else ""
            lines.append(
                f"daemon: {daemon.get('provider')}/{daemon.get('id')} {state}{pid}"
            )
    else:
        lines.append("daemon: none configured")
    drain = report.get("drain")
    if isinstance(drain, dict):
        lines.append(format_drain(drain))
    backend_drain = report.get("drain_backend")
    if isinstance(backend_drain, dict):
        lines.extend(format_backend_drain(backend_drain))
    lines.append(f"logs: {report.get('logs')}")
    clis = report.get("clis")
    if isinstance(clis, list):
        for row in clis:
            if not isinstance(row, dict):
                continue
            name = row.get("name")
            result = row.get("result")
            health = row.get("health")
            # Persisted health is separate from the latest attempt. skip,
            # pin, and dry-run would otherwise hide a broken CLI.
            if health == "broken" and result != "broken":
                lines.append(f"cli: {name} {result} health=broken")
            else:
                lines.append(f"cli: {name} {result}")
    return "\n".join(lines) + "\n"


def status_ok(report: dict[str, object]) -> bool:
    backend = report.get("backend")
    web = report.get("web")
    if not isinstance(backend, dict) or not backend.get("healthy"):
        return False
    if not isinstance(web, dict) or not web.get("running"):
        return False
    daemons = report.get("daemons")
    return not (
        isinstance(daemons, list)
        and any(
            not item.get("running") for item in daemons if isinstance(item, dict)
        )
    )


SMOKE_PROMPT = (
    "This is a read-only smoke check. Do not read or write files, do not run "
    "shell commands or tools, and do not commit. Reply with exactly "
    "SMOKE-READY and nothing else."
)
SMOKE_READY = "SMOKE-READY"
SMOKE_TIMEOUT_DEFAULT = 180.0
# Smoke Tickets are archived when the run ends; the prefix marks any that a
# failed archive leaves behind in a Ticket list.
SMOKE_TITLE_PREFIX = "[smoke]"
# Created under the workspace's policy root and the worktree root (or under
# CONDUCTOR_SMOKE_CWD), never the repo or the workspace root itself, and
# removed when the run ends.
SMOKE_SCRATCH_DIR = ".cond-smoke"


#: An existing directory the smoke scratch cwd is made in, instead of the one
#: derived from this shell's worktree root. The daemons check a Ticket cwd
#: against their own worktree root, which a LaunchAgent may have been given
#: when this shell's environment differs; the operator who knows it names a
#: directory under both roots here.
SMOKE_CWD_ENV = "CONDUCTOR_SMOKE_CWD"


def smoke_cwd_override() -> str | None:
    """``CONDUCTOR_SMOKE_CWD`` when it is set to a non-blank value."""
    raw = os.environ.get(SMOKE_CWD_ENV, "").strip()
    return raw or None


def _configured_smoke_base(raw: str, policy_root: Path) -> Path:
    """The operator's smoke directory, refused unless the daemons could accept it.

    It must be an absolute, existing, real directory (no symlink in its last
    component) inside the Home workspace root. The worktree-root half of the
    daemon's check is left to the daemon: the operator set this because this
    shell's worktree root is not the daemons'.
    """
    import stat as stat_mod

    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise OpsError(f"{SMOKE_CWD_ENV} must be an absolute path")
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError:
        raise OpsError(f"{SMOKE_CWD_ENV} does not exist") from None
    if stat_mod.S_ISLNK(info.st_mode) or not stat_mod.S_ISDIR(info.st_mode):
        raise OpsError(f"{SMOKE_CWD_ENV} must be a directory, not a symlink")
    if not resolved.is_relative_to(policy_root):
        raise OpsError(
            f"{SMOKE_CWD_ENV} is not inside the Home workspace root"
        )
    return resolved


def smoke_timeout_default() -> float:
    """``CONDUCTOR_SMOKE_TIMEOUT`` when it is a positive number, else 180."""
    raw = os.environ.get("CONDUCTOR_SMOKE_TIMEOUT", "")
    try:
        value = float(raw)
    except ValueError:
        return SMOKE_TIMEOUT_DEFAULT
    return value if value > 0 else SMOKE_TIMEOUT_DEFAULT


@dataclass
class SmokeResult:
    """One configured daemon's read-only smoke outcome."""

    daemon: DaemonSpec
    ticket_id: str = ""
    state: str = "failed"
    reason: str = ""

    @property
    def label(self) -> str:
        return _daemon_key(self.daemon)

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def _plain_reason(text: object, *, limit: int = 240) -> str:
    """One readable line. Server and Worker text is untrusted and may be long."""
    value = " ".join(str(text or "").split())
    return value[:limit]


def _api_error(body: str, code: int) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str):
        text = detail
    elif isinstance(detail, list):
        parts: list[str] = []
        for entry in detail:
            if not isinstance(entry, dict):
                continue
            message = entry.get("msg")
            if not message:
                continue
            where = entry.get("loc")
            location = (
                ".".join(str(item) for item in where)
                if isinstance(where, list)
                else ""
            )
            parts.append(f"{location}: {message}" if location else str(message))
        text = "; ".join(parts)
    else:
        text = ""
    return _plain_reason(text) or f"HTTP {code}"


def _api_request(
    system: RealSystem,
    settings: Settings,
    method: str,
    path: str,
    payload: dict[str, object] | None = None,
    *,
    timeout: float = 30.0,
) -> dict[str, object]:
    """Call the loopback API and return the decoded object or an OpsError."""
    url = settings.base_url + path
    if method == "POST":
        post = getattr(system, "http_post", None)
        if not callable(post):
            raise OpsError("the injected system cannot POST to the backend")
        code, body = post(url, payload or {}, timeout)
    else:
        code, body = system.http_get(url, timeout)
    if code == 0:
        raise OpsError(f"could not reach the backend: {_plain_reason(body, limit=80)}")
    if code < 200 or code >= 300:
        raise OpsError(_api_error(body, code))
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise OpsError(f"the backend returned invalid JSON (HTTP {code})") from None
    if not isinstance(parsed, dict):
        raise OpsError(f"the backend returned an unexpected JSON value (HTTP {code})")
    return parsed


def _home_workspace(system: RealSystem, settings: Settings) -> str:
    payload = _api_request(
        system,
        settings,
        "POST",
        "/api/v1/workspaces/home",
        {},
        timeout=15.0,
    )
    workspace_id = str(payload.get("workspace_id") or "")
    if not workspace_id:
        raise OpsError("the backend did not return a Home workspace")
    return workspace_id


def _workspace_policy_root(settings: Settings, workspace_id: str) -> Path:
    """The policy root the local daemons will confine this workspace to.

    Only the ``root`` is read; the daemon still validates the whole policy.
    """
    import stat as stat_mod

    path = settings.workspace_policy
    try:
        info = path.lstat()
    except OSError:
        raise OpsError(f"missing daemon workspace policy: {path}") from None
    if stat_mod.S_ISLNK(info.st_mode) or not stat_mod.S_ISREG(info.st_mode):
        raise OpsError("the daemon workspace policy must be a regular file")
    if info.st_mode & 0o077:
        raise OpsError("the daemon workspace policy must have mode 0600")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OpsError("the daemon workspace policy is unreadable") from None
    row = data.get(workspace_id) if isinstance(data, dict) else None
    root = row.get("root") if isinstance(row, dict) else None
    if not isinstance(root, str) or not root.strip():
        raise OpsError(
            "the daemon workspace policy has no entry for the Home workspace"
        )
    return Path(root).expanduser()


def _make_smoke_scratch(settings: Settings, workspace_id: str) -> Path:
    """A fresh empty cwd the daemons accept, outside the repo and workspace root.

    POST /autonomy/tasks has no read-only flag, so the prompt is the only
    restriction on what the worker does; the cwd keeps an obedient-but-wrong
    worker away from real checkouts. A daemon accepts a cwd only when it is an
    existing directory under both its worktree root and the policy root.
    """
    import tempfile

    try:
        policy_root = _workspace_policy_root(settings, workspace_id).resolve(
            strict=True
        )
    except OSError:
        raise OpsError("the Home workspace root does not exist") from None
    configured = smoke_cwd_override()
    if configured is not None:
        base = _configured_smoke_base(configured, policy_root)
    else:
        try:
            worktree_root = settings.worktree_root.expanduser().resolve(strict=True)
        except OSError:
            raise OpsError(
                "the worktree root or the Home workspace root does not exist"
            ) from None
        if policy_root.is_relative_to(worktree_root):
            base = policy_root
        elif worktree_root.is_relative_to(policy_root):
            base = worktree_root
        else:
            raise OpsError(
                "the worktree root and the Home workspace root do not overlap; "
                f"set {SMOKE_CWD_ENV} to a directory under both"
            )
    parent = base / SMOKE_SCRATCH_DIR
    try:
        parent.mkdir(mode=0o700, exist_ok=True)
        if parent.is_symlink() or not parent.is_dir():
            raise OpsError(f"{parent} must be a directory")
        return Path(tempfile.mkdtemp(prefix="run-", dir=parent))
    except OSError as exc:
        raise OpsError(f"could not create {parent}: {type(exc).__name__}") from None


def _remove_smoke_scratch(scratch: Path | None) -> None:
    if scratch is None:
        return
    import shutil

    shutil.rmtree(scratch, ignore_errors=True)
    try:
        scratch.parent.rmdir()  # only when no other smoke run is using it
    except OSError:
        pass


def _create_smoke_ticket(
    system: RealSystem,
    settings: Settings,
    workspace_id: str,
    daemon: DaemonSpec,
    cwd: Path,
) -> str:
    payload = _api_request(
        system,
        settings,
        "POST",
        "/api/v1/autonomy/tasks",
        {
            "workspace_id": workspace_id,
            "prompt": SMOKE_PROMPT,
            "title": f"{SMOKE_TITLE_PREFIX} {_daemon_key(daemon)}"[:120],
            "worker_provider": daemon.provider,
            "worker_daemon_id": daemon.ident,
            "cwd": str(cwd),
        },
        timeout=30.0,
    )
    ticket_id = str(payload.get("ticket_id") or "")
    if not ticket_id:
        raise OpsError("the backend did not return a Ticket id")
    return ticket_id


def _smoke_progress(
    system: RealSystem, settings: Settings, ticket_id: str
) -> dict[str, object]:
    return _api_request(
        system,
        settings,
        "GET",
        f"/api/v1/autonomy/tickets/{ticket_id}/progress",
        timeout=10.0,
    )


def _stop_smoke_ticket(
    system: RealSystem, settings: Settings, ticket_id: str
) -> None:
    """Best effort: a failed smoke must not leave a Ticket claiming work."""
    try:
        _api_request(
            system,
            settings,
            "POST",
            f"/api/v1/autonomy/tickets/{ticket_id}/stop",
            {},
            timeout=5.0,
        )
    except OpsError:
        pass


def _archive_smoke_ticket(
    system: RealSystem, settings: Settings, ticket_id: str
) -> None:
    """Best effort: keep smoke Tickets out of the normal Ticket lists."""
    try:
        _api_request(
            system,
            settings,
            "POST",
            f"/api/v1/autonomy/tickets/{ticket_id}/archive",
            {},
            timeout=5.0,
        )
    except OpsError:
        pass


def _smoke_outcome(
    daemon: DaemonSpec, ticket_id: str, payload: dict[str, object]
) -> SmokeResult | None:
    """Map one progress payload. ``None`` means the Ticket is still working."""
    status = str(payload.get("status") or "")
    summary = _plain_reason(payload.get("summary"))
    error = _plain_reason(payload.get("error"))
    question = _plain_reason(payload.get("question"))
    if status == "completed":
        # Exact and case-sensitive: "NOT SMOKE-READY" or a chatty reply is not
        # a worker that followed the instruction.
        if str(payload.get("summary") or "").strip() == SMOKE_READY:
            return SmokeResult(daemon, ticket_id, "ready", "")
        reason = summary or f"completed without {SMOKE_READY}"
        return SmokeResult(daemon, ticket_id, "failed", reason)
    if status == "failed":
        return SmokeResult(
            daemon, ticket_id, "failed", error or summary or "the Ticket failed"
        )
    if status in {"waiting", "blocked", "awaiting_clarification"}:
        return SmokeResult(
            daemon,
            ticket_id,
            "failed",
            summary or question or f"the Ticket is {status}",
        )
    if status in {"cancelled", "archived"}:
        return SmokeResult(daemon, ticket_id, "failed", f"the Ticket was {status}")
    return None


def smoke_daemons(
    system: RealSystem,
    settings: Settings,
    fleet: FleetFile,
    *,
    timeout: float | None = None,
) -> list[SmokeResult]:
    """Create one read-only Ticket per configured daemon and collect a verdict.

    Tickets are created together and then polled, so a broken daemon does not
    delay the others. The caller decides the exit code from ``ready``.

    However the run ends (verdicts, timeout, Ctrl-C, an unexpected error),
    every Ticket still working is asked to stop, every created Ticket is
    archived, and the scratch cwd is removed.
    """
    if timeout is None:
        timeout = smoke_timeout_default()
    if not fleet.daemons:
        return []
    workspace_id = _home_workspace(system, settings)
    try:
        scratch = _make_smoke_scratch(settings, workspace_id)
    except OpsError as exc:
        reason = f"no smoke cwd: {exc}"
        return [SmokeResult(daemon, "", "failed", reason) for daemon in fleet.daemons]
    results: list[SmokeResult | None] = [None] * len(fleet.daemons)
    pending: list[tuple[int, DaemonSpec, str]] = []
    created: list[str] = []
    try:
        for index, daemon in enumerate(fleet.daemons):
            try:
                ticket_id = _create_smoke_ticket(
                    system, settings, workspace_id, daemon, scratch
                )
            except OpsError as exc:
                results[index] = SmokeResult(daemon, "", "failed", str(exc))
                continue
            created.append(ticket_id)
            pending.append((index, daemon, ticket_id))
        deadline = system.monotonic() + max(0.0, timeout)
        last_error = ""
        while pending and system.monotonic() < deadline:
            for item in list(pending):
                index, daemon, ticket_id = item
                try:
                    payload = _smoke_progress(system, settings, ticket_id)
                except OpsError as exc:
                    last_error = str(exc)
                    continue
                outcome = _smoke_outcome(daemon, ticket_id, payload)
                if outcome is None:
                    continue
                if not outcome.ready and str(payload.get("status") or "") in {
                    "waiting",
                    "blocked",
                    "awaiting_clarification",
                }:
                    # A waiting smoke would outlive the run and sit in the inbox.
                    _stop_smoke_ticket(system, settings, ticket_id)
                results[index] = outcome
                pending.remove(item)
            if pending:
                system.sleep(max(0.0, min(1.0, deadline - system.monotonic())))
        for index, daemon, ticket_id in pending:
            reason = f"timed out after {timeout:g}s"
            if last_error:
                reason = f"{reason} ({last_error})"
            results[index] = SmokeResult(daemon, ticket_id, "failed", reason)
    finally:
        for _index, _daemon, ticket_id in pending:
            _stop_smoke_ticket(system, settings, ticket_id)
        for ticket_id in created:
            _archive_smoke_ticket(system, settings, ticket_id)
        _remove_smoke_scratch(scratch)
    return [result for result in results if result is not None]


def smoke_payload(
    results: list[SmokeResult], settings: Settings | None = None
) -> dict[str, object]:
    secrets = settings.sensitive if settings is not None else []
    return {
        "ok": all(result.ready for result in results),
        "daemons": [
            {
                "provider": result.daemon.provider,
                "id": result.daemon.ident,
                "state": result.state,
                "reason": redact(_plain_reason(result.reason), secrets),
                "ticket_id": result.ticket_id,
            }
            for result in results
        ],
    }


def format_smoke(results: list[SmokeResult], settings: Settings | None = None) -> str:
    secrets = settings.sensitive if settings is not None else []
    lines: list[str] = []
    for result in results:
        if result.ready:
            lines.append(f"daemon: {result.label} READY")
        else:
            reason = redact(_plain_reason(result.reason), secrets) or "unknown failure"
            lines.append(f"daemon: {result.label} FAILED: {reason}")
    ready = sum(1 for result in results if result.ready)
    lines.append(f"smoke: {ready}/{len(results)} ready")
    return "\n".join(lines) + "\n"


def tail_logs(settings: Settings, component: str | None, lines: int) -> str:
    if lines < 1:
        raise OpsError("log line count must be positive")
    if component:
        files = [
            path for path in settings.log_dir.glob("*.log") if component in path.name
        ]
    else:
        files = sorted(settings.log_dir.glob("*.log"))
    chunks = []
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        chunks.append(f"== {path.name} ==")
        chunks.extend(content[-lines:])
    return redact("\n".join(chunks), settings.sensitive) + (
        "\n" if chunks else "no logs\n"
    )


def prepare(
    system: RealSystem | None = None, env=None
) -> tuple[RealSystem, Settings, FleetFile]:
    system = system or RealSystem()
    settings = load_settings(env)
    fleet = load_fleet(settings)
    return system, settings, fleet


def run_checked(
    system: RealSystem,
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    secrets: list[str] | None = None,
) -> Completed:
    result = system.run(argv, cwd=cwd, env=env)
    if result.code != 0:
        raw = (result.stderr or result.stdout or "").strip().splitlines()
        message = redact(raw[0], secrets or []) if raw else "failed"
        if len(message) > 180:
            message = message[:180]
        raise OpsError(f"{argv[0]} failed ({result.code}): {message}")
    return result
