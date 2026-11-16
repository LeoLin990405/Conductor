"""Shared paths, process runner, and redaction for cond operations.

Nothing in this module reads ``~/.zshenv`` or prints secret values.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .log_rotation import log_limits, rotate_log as rotate_log_file, spawn_log_pump
from .worktree import conductor_worktree_root


class OpsError(Exception):
    """A cond operation stopped before it could finish safely."""


def require_loopback_url(url: str) -> None:
    """Refuse any URL that is not http(s) to localhost, 127.0.0.0/8 or ::1.

    cond only talks to the backend it runs on this machine; a URL built from a
    bad setting must not send a request, or a smoke prompt, anywhere else.
    """
    import ipaddress
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError on a malformed port
    except ValueError:
        raise OpsError("refusing a malformed URL") from None
    if parts.scheme not in {"http", "https"}:
        raise OpsError(f"refusing a non-HTTP URL scheme: {parts.scheme or 'none'}")
    if host.lower() == "localhost":
        return
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise OpsError(
            f"refusing to contact non-loopback host {host or '(none)'}; "
            "cond only talks to localhost, 127.0.0.0/8 or ::1"
        )


@dataclass
class Completed:
    code: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class Settings:
    repo: Path
    conductor_home: Path
    backend_host: str
    backend_port: int
    web_host: str
    web_port: int
    web_mode: str
    log_dir: Path
    run_dir: Path
    secrets_file: Path
    daemons_file: Path
    backup_dir: Path
    launchd_dir: Path
    launchd_domain: str
    systemd_dir: Path | None
    newsyslog_dir: Path | None
    worktree_root: Path
    python: Path
    workspace_policy: Path
    isolation_root: Path
    token_dir: Path
    heartbeat_dir: Path
    registry_file: Path
    daemon_policy: Path
    trusted_keys: Path
    mcp_registry: Path
    dispatch_repo: Path | None
    log_max_bytes: int = 10_485_760
    log_backups: int = 5
    backup_keep: int = 10
    heartbeat_ttl: int = 120
    stale_daemon_seconds: int = 86400
    # How long cond down/restart/upgrade waits for dispatch launches
    # (ops_dispatch.py reads their paths from the checkout .env).
    dispatch_wait_seconds: int = 600
    # cond doctor warns about orphaned worker processes older than this.
    orphan_seconds: int = 21600
    sensitive: list[str] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://{self.backend_host}:{self.backend_port}"


def _path_from(env: Mapping[str, str], name: str, default: Path) -> Path:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    return Path(raw).expanduser()


def _reject_zshenv(path: Path) -> None:
    if path.name == ".zshenv" or ".zshenv" in path.parts:
        raise OpsError("refusing to read zshenv")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Resolve cond paths from the environment. Does not read secret files."""
    env = os.environ if env is None else env
    repo_raw = env.get("CONDUCTOR_REPO_ROOT", "").strip()
    if repo_raw:
        repo = Path(repo_raw).expanduser()
    else:
        repo = Path(__file__).resolve().parents[2]
    home = Path(env.get("HOME", str(Path.home()))).expanduser()
    conductor_home = _path_from(env, "CONDUCTOR_HOME", home / ".conductor")
    backend_port = int(env.get("CONDUCTOR_BACKEND_PORT", "8000") or "8000")
    web_port = int(env.get("CONDUCTOR_WEB_PORT", "5170") or "5170")
    web_mode = (env.get("CONDUCTOR_WEB_MODE", "preview") or "preview").strip()
    if web_mode not in {"preview", "static"}:
        raise OpsError("CONDUCTOR_WEB_MODE must be preview or static")
    python_raw = env.get("CONDUCTOR_PYTHON", "").strip()
    venv_python = repo / ".venv" / "bin" / "python"
    if python_raw:
        python = Path(python_raw).expanduser()
    elif venv_python.is_file():
        python = venv_python
    else:
        python = Path(os.environ.get("CONDUCTOR_PYTHON", "") or "python3")
    dispatch_raw = env.get("CONDUCTOR_DISPATCH_REPO", "").strip()
    systemd_raw = env.get("CONDUCTOR_SYSTEMD_DIR", "").strip()
    newsyslog_raw = env.get("CONDUCTOR_NEWSYSLOG_DIR", "").strip()
    log_max_bytes, log_backups = log_limits(env)
    settings = Settings(
        repo=repo,
        conductor_home=conductor_home,
        backend_host="127.0.0.1",
        backend_port=backend_port,
        web_host="127.0.0.1",
        web_port=web_port,
        web_mode=web_mode,
        log_dir=_path_from(env, "CONDUCTOR_LOG_DIR", conductor_home / "logs"),
        run_dir=conductor_home / "run",
        secrets_file=_path_from(env, "CONDUCTOR_SECRETS_FILE", conductor_home / "secrets.env"),
        daemons_file=_path_from(env, "CONDUCTOR_DAEMONS_FILE", conductor_home / "daemons.toml"),
        backup_dir=_path_from(env, "CONDUCTOR_BACKUP_DIR", conductor_home / "backups"),
        launchd_dir=_path_from(
            env, "CONDUCTOR_LAUNCHD_DIR", home / "Library" / "LaunchAgents"
        ),
        launchd_domain=env.get("CONDUCTOR_LAUNCHD_DOMAIN", "").strip()
        or f"gui/{os.getuid()}",
        systemd_dir=Path(systemd_raw).expanduser() if systemd_raw else None,
        newsyslog_dir=Path(newsyslog_raw).expanduser() if newsyslog_raw else None,
        worktree_root=Path(conductor_worktree_root()).expanduser(),
        python=python,
        workspace_policy=_path_from(
            env,
            "CONDUCTOR_WORKSPACE_POLICY",
            home / ".conductor-daemon" / "workspaces-cli.json",
        ),
        isolation_root=_path_from(
            env,
            "CONDUCTOR_ISOLATION_ROOT",
            home / ".conductor-daemon" / "cli-state",
        ),
        token_dir=_path_from(
            env,
            "CONDUCTOR_DAEMON_TOKEN_DIR",
            home / ".config" / "conductor" / "daemons",
        ),
        heartbeat_dir=conductor_home / "run" / "heartbeats",
        registry_file=conductor_home / "run" / "registrations.json",
        daemon_policy=_path_from(
            env,
            "CONDUCTOR_DAEMON_POLICY",
            home / ".config" / "conductor" / "daemon_policy.yaml",
        ),
        trusted_keys=_path_from(
            env,
            "CONDUCTOR_DAEMON_TRUSTED_KEYS",
            home / ".config" / "conductor" / "daemon_trusted_keys.json",
        ),
        mcp_registry=_path_from(
            env,
            "CONDUCTOR_MCP_REGISTRY",
            home / ".conductor" / "mcp" / "servers.toml",
        ),
        dispatch_repo=Path(dispatch_raw).expanduser() if dispatch_raw else None,
        log_max_bytes=log_max_bytes,
        log_backups=log_backups,
        backup_keep=int(env.get("CONDUCTOR_BACKUP_KEEP", "10") or "10"),
        heartbeat_ttl=int(env.get("CONDUCTOR_HEARTBEAT_TTL", "120") or "120"),
        stale_daemon_seconds=int(env.get("CONDUCTOR_STALE_DAEMON_SECONDS", "86400") or "86400"),
        dispatch_wait_seconds=int(
            env.get("CONDUCTOR_DISPATCH_WAIT_SECONDS", "600") or "600"
        ),
        orphan_seconds=int(env.get("CONDUCTOR_ORPHAN_SECONDS", "21600") or "21600"),
    )
    for path in (
        settings.secrets_file,
        settings.daemons_file,
        settings.workspace_policy,
        settings.daemon_policy,
        settings.trusted_keys,
    ):
        _reject_zshenv(path)
    return settings


def redact(text: str, secrets: list[str]) -> str:
    redacted = text
    for secret in secrets:
        if secret and secret in redacted:
            redacted = redacted.replace(secret, "***")
    return redacted


def rotate_log(path: Path, max_bytes: int, backups: int) -> None:
    """Rename an oversized log. ``path.1`` is the newest archive."""
    rotate_log_file(path, max_bytes=max_bytes, backup_count=backups)


class _NotOurChild:
    """This pid was not started by ``RealSystem.popen``."""


NOT_OUR_CHILD = _NotOurChild()


class RealSystem:
    """Process and HTTP calls used by cond. Tests substitute a fake."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def run(
        self,
        argv: list[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Completed:
        try:
            result = subprocess.run(
                argv,
                cwd=None if cwd is None else str(cwd),
                env=None if env is None else dict(env),
                timeout=timeout,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise OpsError(f"could not run {argv[0]}: {type(exc).__name__}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OpsError(f"timed out running {argv[0]}") from exc
        return Completed(result.returncode, result.stdout or "", result.stderr or "")

    def run_terminal(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> Completed:
        try:
            result = subprocess.run(
                argv,
                env=dict(env) if env else None,
                cwd=str(cwd) if cwd else None,
                timeout=timeout,
                check=False,
            )
        except OSError as exc:
            raise OpsError(f"could not run {argv[0]}: {type(exc).__name__}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OpsError(f"timed out running {argv[0]}") from exc
        return Completed(result.returncode, "", "")

    def popen(
        self,
        argv: list[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log_path: Path,
        max_bytes: int,
        backups: int,
    ) -> int:
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        rotate_log(log_path, max_bytes, backups)
        # The child writes the pipe. The pump owns the log file, so crossing
        # the size limit renames that file and opens a new one while this
        # process keeps running. Inheriting the log descriptor would make
        # that rename invisible to the child and the file would grow without
        # a restart.
        try:
            pump, write_fd = spawn_log_pump(
                log_path, max_bytes=max_bytes, backup_count=backups
            )
        except OSError as exc:
            raise OpsError(f"could not start {argv[0]}: {type(exc).__name__}") from exc
        pumps = getattr(self, "_log_pumps", None)
        if pumps is None:
            pumps = []
            self._log_pumps = pumps
        pumps.append(pump)
        # os.pipe() is not inheritable. dup2 in the child usually clears
        # that, but a non-inheritable stdout is dropped when the spawn path
        # closes fds first. The child then writes nowhere, the pump sees
        # end-of-file, and the log never reaches the size limit.
        os.set_inheritable(write_fd, True)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=write_fd,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
                pass_fds=(write_fd,),
            )
        except OSError as exc:
            os.close(write_fd)
            pump.terminate()
            raise OpsError(f"could not start {argv[0]}: {type(exc).__name__}") from exc
        os.close(write_fd)
        children = getattr(self, "_children", None)
        if children is None:
            children = {}
            self._children = children
        children[proc.pid] = proc
        self._reap_log_pumps()
        return proc.pid

    def _reap_log_pumps(self) -> None:
        """Collect pumps whose child has already closed the pipe."""
        pumps = getattr(self, "_log_pumps", None)
        if not pumps:
            return
        still: list[subprocess.Popen] = []
        for pump in pumps:
            if pump.poll() is None:
                still.append(pump)
            else:
                pump.wait(timeout=0)
        self._log_pumps = still

    def child_poll(self, pid: int) -> int | None | _NotOurChild:
        """Reap one of our children. ``None`` means it is still running.

        ``os.kill(pid, 0)`` stays true for an unreaped zombie, so the
        supervisor must not use it to decide whether its own child is alive.
        """
        self._reap_log_pumps()
        children = getattr(self, "_children", {})
        proc = children.get(pid)
        if proc is None:
            return NOT_OUR_CHILD
        return proc.poll()

    def alive(self, pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def start_time(self, pid: int) -> str:
        """Process start time from ps, or empty when the pid is gone."""
        if pid <= 0:
            return ""
        result = self.run(["ps", "-p", str(pid), "-o", "lstart="])
        return " ".join(result.stdout.split()) if result.code == 0 else ""

    def command(self, pid: int) -> str:
        result = self.run(["ps", "-p", str(pid), "-o", "command="])
        return result.stdout.strip()

    def cwd(self, pid: int) -> str:
        result = self.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
        for line in result.stdout.splitlines():
            if line.startswith("n"):
                return line[1:]
        return ""

    def terminate(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)

    def kill(self, pid: int) -> None:
        os.kill(pid, signal.SIGKILL)

    def processes(self) -> str:
        """``ps`` rows: pid, ppid, pgid, elapsed time, command."""
        result = self.run(["ps", "-axww", "-o", "pid=,ppid=,pgid=,etime=,command="])
        return result.stdout if result.code == 0 else ""

    def signal_group(self, pgid: int, sig: int) -> None:
        """Signal one process group. Never this process's own group or init's."""
        if pgid <= 1 or pgid == os.getpgrp():
            raise OpsError(f"refusing to signal process group {pgid}")
        os.killpg(pgid, sig)

    def http_get(self, url: str, timeout: float) -> tuple[int, str]:
        """GET the loopback API. A 4xx/5xx body is an answer, not a failure.

        The body of an error response is kept, because the answer is
        often in it: /health answers 503 when MCP discovery failed and
        that body still names the tree the server runs from.
        """
        require_loopback_url(url)
        if ":8000" in url and os.environ.get("CONDUCTOR_FORBID_PORT_8000") == "1":
            raise OpsError("refusing to contact port 8000")
        try:
            with urlopen(url, timeout=timeout) as response:
                body = response.read(65536).decode("utf-8", errors="replace")
                return int(response.status), body
        except HTTPError as exc:
            body = exc.read(65536).decode("utf-8", errors="replace")
            return int(exc.code), body
        except (URLError, TimeoutError, OSError) as exc:
            return 0, type(exc).__name__

    def http_post(
        self, url: str, payload: Mapping[str, Any], timeout: float
    ) -> tuple[int, str]:
        """POST JSON to the loopback API. A 4xx/5xx body is an answer, not a failure."""
        require_loopback_url(url)
        if ":8000" in url and os.environ.get("CONDUCTOR_FORBID_PORT_8000") == "1":
            raise OpsError("refusing to contact port 8000")
        request = Request(
            url,
            data=json.dumps(dict(payload)).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read(65536).decode("utf-8", errors="replace")
                return int(response.status), body
        except HTTPError as exc:
            body = exc.read(65536).decode("utf-8", errors="replace")
            return int(exc.code), body
        except (URLError, TimeoutError, OSError) as exc:
            return 0, type(exc).__name__

    def port_open(self, host: str, port: int, timeout: float = 0.3) -> bool:
        import socket

        if port == 8000 and os.environ.get("CONDUCTOR_FORBID_PORT_8000") == "1":
            raise OpsError("refusing to contact port 8000")
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def connect_database(self, conninfo: str) -> Any:
        """Open a psycopg connection. ``postgres`` is refused before libpq is called."""
        from .db_guard import guarded_connect

        return guarded_connect(conninfo)


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise OpsError(f"refusing a symlinked directory: {path}")
