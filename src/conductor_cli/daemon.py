"""Local process control for the existing Conductor BYOM worker."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .client import (
    CONFIG_DIR,
    CLIError,
    _check_private_file,
    ensure_private_directory,
    read_token,
)

DAEMON_TOKEN_DIR = CONFIG_DIR / "daemons"
DAEMON_STATE_DIR = Path.home() / ".local" / "state" / "conductor" / "daemons"


def _daemon_path(daemon_id: str, suffix: str) -> Path:
    try:
        daemon_id = str(uuid.UUID(daemon_id))
    except (ValueError, AttributeError) as exc:
        raise CLIError("Invalid daemon id") from exc
    return DAEMON_TOKEN_DIR / f"{daemon_id}.{suffix}"


def save_daemon_token(daemon_id: str, token: str) -> None:
    path = _daemon_path(daemon_id, "token")
    _check_private_file(path)
    ensure_private_directory(path.parent)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError as exc:
        raise CLIError(f"A private token is already stored for daemon {daemon_id}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")


def preflight_daemon_token(daemon_id: str) -> None:
    path = _daemon_path(daemon_id, "token")
    ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        _check_private_file(path)
        raise CLIError(f"A token is already stored for daemon {daemon_id}")


def preflight_daemon_token_storage() -> None:
    ensure_private_directory(DAEMON_TOKEN_DIR)


def _read_json(path: Path) -> dict[str, Any] | None:
    ensure_private_directory(path.parent, create=False)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CLIError(f"Cannot safely open local daemon state file: {path}") from exc
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        os.close(descriptor)
        raise CLIError(f"Local daemon state file is not private: {path}")
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except json.JSONDecodeError as exc:
        raise CLIError(f"Invalid local daemon state file: {path}") from exc


def _script_path() -> Path:
    configured = os.getenv("CONDUCTOR_DAEMON_SCRIPT")
    repository = os.getenv("CONDUCTOR_REPO")
    if configured:
        path = Path(configured).expanduser()
    elif repository:
        path = Path(repository).expanduser() / "scripts" / "conductor_local_daemon.py"
    else:
        path = Path(__file__).resolve().parents[2] / "scripts" / "conductor_local_daemon.py"
    if not path.is_file():
        raise CLIError("Local daemon script is missing; set CONDUCTOR_DAEMON_SCRIPT to scripts/conductor_local_daemon.py")
    return path


def start_local_daemon(
    client: Any,
    daemon_id: str,
    cwd: str,
    once: bool = False,
    *,
    worktree_root: str = "",
    breaker_failure_threshold: int = 3,
    breaker_recovery_success_threshold: int = 1,
    breaker_reset_timeout: float = 30.0,
    breaker_backoff_max: float = 300.0,
    breaker_jitter: float = 0.2,
    policy: str = "",
    trusted_keys: str = "",
) -> dict[str, Any]:
    """Start a registered daemon using its saved token and local worker script."""
    if not worktree_root:
        from .worktree import conductor_worktree_root

        worktree_root = conductor_worktree_root()
    try:
        resolved_worktree_root = Path(worktree_root).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise CLIError("Worktree root must be a valid path") from exc
    token_path = _daemon_path(daemon_id, "token")
    daemon_id = token_path.stem.removesuffix(".token")
    token = read_token(token_path)
    if token is None:
        raise CLIError(f"No local credential saved for daemon {daemon_id}; register it with this CLI first")

    daemons = client.get("/api/v1/autonomy/daemons").get("daemons", [])
    daemon = next((item for item in daemons if str(item.get("daemon_id")) == daemon_id), None)
    if daemon is None:
        raise CLIError(f"Daemon {daemon_id} is not registered on this Conductor server")

    state_path = DAEMON_STATE_DIR / f"{_daemon_path(daemon_id, 'state').stem}.json"
    breaker_status_path = DAEMON_STATE_DIR / f"{daemon_id}.breaker.json"
    prior = _read_json(state_path) if state_path.exists() else None
    if prior and _pid_is_our_daemon(
        int(prior.get("pid", 0)), str(prior.get("script", "")), str(prior.get("fingerprint", ""))
    ):
        raise CLIError(f"Daemon {daemon_id} is already running (pid {prior['pid']})")

    ensure_private_directory(DAEMON_STATE_DIR)
    log_path = DAEMON_STATE_DIR / f"{daemon_id}.log"
    script = _script_path()
    command = [
        sys.executable,
        str(script),
        "--base-url",
        client.base_url,
        "--daemon-id",
        daemon_id,
        "--provider",
        str(daemon["provider"]),
        "--cwd",
        str(Path(cwd).expanduser().resolve()),
        "--worktree-root",
        str(resolved_worktree_root),
        "--max-concurrency",
        str(daemon.get("max_concurrency", 1)),
        "--status-file",
        str(breaker_status_path),
        "--breaker-failure-threshold",
        str(breaker_failure_threshold),
        "--breaker-recovery-success-threshold",
        str(breaker_recovery_success_threshold),
        "--breaker-reset-timeout",
        str(breaker_reset_timeout),
        "--breaker-backoff-max",
        str(breaker_backoff_max),
        "--breaker-jitter",
        str(breaker_jitter),
    ]
    if once:
        command.append("--once")
    policy_path = str(policy or "").strip()
    if policy_path:
        command.extend(["--policy", policy_path])
    trusted_keys_path = str(trusted_keys or "").strip()
    if trusted_keys_path:
        command.extend(["--trusted-keys", trusted_keys_path])
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    log_stat = os.fstat(log_fd)
    if log_stat.st_uid != os.getuid() or log_stat.st_mode & 0o077:
        os.close(log_fd)
        raise CLIError(f"Local daemon log file is not private: {log_path}")
    with os.fdopen(log_fd, "ab") as log_stream:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_stream,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            env={**os.environ, "CONDUCTOR_DAEMON_TOKEN": token},
        )
    time.sleep(0.2)
    if process.poll() is not None:
        raise CLIError(f"Daemon exited during startup; inspect {log_path}")
    fingerprint = _process_fingerprint(process.pid)
    state = {
        "daemon_id": daemon_id,
        "pid": process.pid,
        "script": str(script),
        "cwd": str(Path(cwd).expanduser().resolve()),
        "worktree_root": str(resolved_worktree_root),
        "log": str(log_path),
        "started_at": datetime.now(UTC).isoformat(),
        "fingerprint": fingerprint,
    }
    _write_state(state_path, state)
    return {key: value for key, value in state.items() if key != "script"}


def local_daemon_status(daemon_id: str) -> dict[str, Any]:
    """Read the local provider breaker snapshot without contacting the server."""
    state = _daemon_path(daemon_id, "state")
    normalized_id = state.stem.removesuffix(".state")
    state_path = DAEMON_STATE_DIR / f"{normalized_id}.json"
    process_state = _read_json(state_path)
    running = bool(
        process_state
        and _pid_is_our_daemon(
            int(process_state.get("pid", 0)),
            str(process_state.get("script", "")),
            str(process_state.get("fingerprint", "")),
        )
    )
    path = DAEMON_STATE_DIR / f"{normalized_id}.breaker.json"
    snapshot = _read_json(path)
    if snapshot is None:
        return {
            "daemon_id": normalized_id,
            "daemon_status": "running" if running else "not_running",
            "breaker_status": "unavailable",
        }
    return {
        "daemon_id": normalized_id,
        "daemon_status": "running" if running else "not_running",
        **snapshot,
    }


def _process_fingerprint(pid: int) -> str:
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _write_state(path: Path, state: dict[str, Any]) -> None:
    ensure_private_directory(path.parent)
    _check_private_file(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(state, indent=2) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _pid_is_our_daemon(pid: int, script: str, fingerprint: str = "") -> bool:
    if pid <= 1 or not script:
        return False
    try:
        os.kill(pid, 0)
        if os.getpgid(pid) != pid:
            return False
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        script in command
        and "conductor_local_daemon.py" in command
        and (not fingerprint or _process_fingerprint(pid) == fingerprint)
    )


def stop_local_daemon(daemon_id: str, timeout: float = 10.0) -> dict[str, Any]:
    """Stop only a PID whose command still names the managed daemon script."""
    daemon_path = _daemon_path(daemon_id, "state")
    daemon_id = daemon_path.stem.removesuffix(".state")
    state_path = DAEMON_STATE_DIR / f"{daemon_path.stem}.json"
    state = _read_json(state_path)
    if state is None:
        raise CLIError(f"No local process state found for daemon {daemon_id}")
    pid = int(state.get("pid", 0))
    if not _pid_is_our_daemon(pid, str(state.get("script", "")), str(state.get("fingerprint", ""))):
        state_path.unlink(missing_ok=True)
        return {"daemon_id": daemon_id, "status": "not_running"}
    os.killpg(pid, signal.SIGINT)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_our_daemon(pid, str(state["script"]), str(state.get("fingerprint", ""))):
            break
        time.sleep(0.1)
    forced = False
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        pass
    else:
        forced = True
        os.killpg(pid, signal.SIGTERM)
        time.sleep(0.5)
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            pass
        else:
            os.killpg(pid, signal.SIGKILL)
    state_path.unlink(missing_ok=True)
    return {"daemon_id": daemon_id, "status": "stopped", "pid": pid, "forced": forced}
