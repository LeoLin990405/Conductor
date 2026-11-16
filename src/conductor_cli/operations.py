"""Local deployment and rollback commands for the Conductor checkout."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO
from urllib.error import URLError
from urllib.request import urlopen

from .client import CLIError

BACKUP_DIR = Path.home() / ".conductor" / "backups"


def _active_backup_dir() -> Path:
    override = os.environ.get("CONDUCTOR_BACKUP_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return BACKUP_DIR
HEALTH_URL = "http://127.0.0.1:8000/health"
HEALTH_TIMEOUT_SECONDS = 30
BACKEND_STOP_TIMEOUT_SECONDS = 10
COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
DISPATCH_GATE_TIMEOUT_SECONDS = 5
DISPATCH_ACTIVE_PATH = "/api/v1/dispatch/status/active"


class OperationError(CLIError):
    """A deployment operation could not safely continue."""


def register_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Register local deploy and rollback commands on the shared parser."""
    deploy = subparsers.add_parser(
        "deploy", help="Back up the database and deploy this checkout"
    )
    deploy.add_argument(
        "deploy_command",
        nargs="?",
        choices=("status",),
        help="status: show the last deploy, live commit, health and last backup",
    )
    deploy.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every action without changing files or services",
    )
    deploy.add_argument(
        "--repo",
        help="Conductor checkout (default: CONDUCTOR_REPO or current Git checkout)",
    )
    deploy.set_defaults(handler=handle_deploy)

    rollback = subparsers.add_parser(
        "rollback", help="Restore a database backup and reset this checkout"
    )
    rollback.add_argument(
        "--commit",
        required=True,
        help="Git commit to reset to (7–40 hexadecimal characters)",
    )
    rollback.add_argument(
        "--backup",
        required=True,
        type=Path,
        help="PostgreSQL custom-format backup to restore",
    )
    rollback.add_argument(
        "--dry-run",
        action="store_true",
        help="Print every action without changing files or services",
    )
    rollback.add_argument(
        "--repo",
        help="Conductor checkout (default: CONDUCTOR_REPO or current Git checkout)",
    )
    rollback.set_defaults(handler=handle_rollback)


def _last_deploy_path() -> Path:
    configured = os.environ.get("CONDUCTOR_LAST_DEPLOY")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".config" / "conductor" / "last-deploy.json"


def _record_last_deploy(record: dict[str, object]) -> None:
    path = _last_deploy_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        # A status-file failure must not fail an otherwise healthy deploy.
        return


def _probe_health() -> dict[str, object]:
    try:
        with urlopen(HEALTH_URL, timeout=2) as response:
            body = json.loads(response.read())
            if (
                response.status == 200
                and isinstance(body, dict)
                and body.get("status") == "healthy"
            ):
                return {"status": "healthy"}
            detail = body.get("status") if isinstance(body, dict) else "invalid"
            return {"status": "unhealthy", "detail": str(detail)}
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"status": "unreachable", "detail": type(exc).__name__}


def _newest_backup() -> str | None:
    backup_dir = _active_backup_dir()
    if not backup_dir.is_dir():
        return None
    dumps = [
        path
        for path in backup_dir.glob("*.dump")
        if path.is_file() and not path.is_symlink()
    ]
    if not dumps:
        return None
    dumps.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    return str(dumps[0])


def handle_deploy_status(args: argparse.Namespace) -> dict[str, object]:
    """Report the last deploy record, live commit, health, and last backup."""
    repo_root = _resolve_repo(args.repo)
    path = _last_deploy_path()
    record: dict[str, object] | None = None
    record_error: str | None = None
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                record = loaded
            else:
                record_error = "record is not a JSON object"
        except (OSError, json.JSONDecodeError) as exc:
            record_error = type(exc).__name__
    last_backup = record.get("backup") if isinstance(record, dict) else None
    if not isinstance(last_backup, str) or not Path(last_backup).is_file():
        last_backup = _newest_backup()
    result: dict[str, object] = {
        "last_deploy": record,
        "record_path": str(path),
        "live_commit": _read_git(repo_root, ["rev-parse", "HEAD"]),
        "health": _probe_health(),
        "last_backup": last_backup,
    }
    if record_error:
        result["record_error"] = record_error
    return result


def handle_deploy(args: argparse.Namespace) -> dict[str, object] | int:
    """Run or preview the ordered deployment procedure."""
    if getattr(args, "deploy_command", None) == "status":
        return handle_deploy_status(args)
    dry_run = bool(args.dry_run)
    json_mode = bool(args.json)
    repo_root = _resolve_repo(args.repo)
    backup_path = (
        _active_backup_dir() / f"conductor-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.dump"
    )
    plan = _deploy_plan(backup_path)
    if dry_run:
        blockers = _preflight_deploy(repo_root, dry_run=True)
        if json_mode:
            return {
                "status": "dry_run",
                "actions": plan,
                "preflight": {"ready": not blockers, "blockers": blockers},
            }
        _emit_plan(plan)
        _emit_blockers(blockers)
        return 0

    db_env = _database_environment(repo_root)
    _preflight_deploy(repo_root)
    backup_path = _reserve_backup_file(backup_path)
    _say("Creating a custom-format database backup", json_mode)
    try:
        _command(
            [
                "pg_dump",
                "--format=custom",
                "--file",
                str(backup_path),
                *_database_args(db_env),
            ],
            repo_root=repo_root,
            env=db_env,
        )
        if not backup_path.is_file() or backup_path.stat().st_size == 0:
            raise OperationError("pg_dump did not create a non-empty backup archive.")
        _command(
            ["pg_restore", "--list", str(backup_path)],
            repo_root=repo_root,
            display="Validate the PostgreSQL backup archive",
        )
    except BaseException:
        with contextlib.suppress(OSError):
            backup_path.unlink()
        raise
    _say("Updating the checkout with a fast-forward pull", json_mode)
    _command(["git", "pull", "--ff-only"], repo_root=repo_root)
    _say("Applying database migrations", json_mode)
    _command([".venv/bin/alembic", "upgrade", "head"], repo_root=repo_root, env=db_env)
    _say("Restarting the backend", json_mode)
    _restart_backend(repo_root, db_env)
    _wait_for_health()
    try:
        deployed_commit: str | None = _read_git(repo_root, ["rev-parse", "HEAD"])
    except OperationError:
        deployed_commit = None
    _record_last_deploy(
        {
            "status": "deployed",
            "backup": str(backup_path),
            "commit": deployed_commit,
            "health": "healthy",
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    result = {"status": "deployed", "backup": str(backup_path), "health": "healthy"}
    if json_mode:
        return result
    print(f"Deployment complete. Database backup: {backup_path}")
    return 0


def handle_rollback(args: argparse.Namespace) -> dict[str, object] | int:
    """Run or preview a confirmed database and source rollback."""
    commit = args.commit
    if not COMMIT_RE.fullmatch(commit):
        raise OperationError("Commit must be a 7–40 character hexadecimal SHA.")
    dry_run = bool(args.dry_run)
    json_mode = bool(args.json)
    repo_root = _resolve_repo(args.repo)
    backup_path = Path(args.backup).expanduser().resolve()
    plan = _rollback_plan(commit, backup_path)
    if dry_run:
        blockers = _preflight_rollback(
            commit, backup_path, repo_root=repo_root, dry_run=True
        )
        if json_mode:
            return {
                "status": "dry_run",
                "actions": plan,
                "preflight": {"ready": not blockers, "blockers": blockers},
            }
        _emit_plan(plan)
        _emit_blockers(blockers)
        return 0
    db_env = _database_environment(repo_root)
    _preflight_rollback(commit, backup_path, repo_root=repo_root)
    if not _confirm_rollback(json_mode=json_mode):
        if json_mode:
            return {"status": "cancelled", "exit_code": 1}
        print("Rollback cancelled.")
        return 1

    _say("Stopping the backend before restoring state", json_mode)
    _stop_backend(repo_root)
    _say("Resetting the checkout to the requested commit", json_mode)
    _command(["git", "reset", "--hard", commit], repo_root=repo_root)
    _say("Restoring the PostgreSQL backup", json_mode)
    _command(
        [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--exit-on-error",
            "--single-transaction",
            *_database_args(db_env),
            str(backup_path),
        ],
        repo_root=repo_root,
        env=db_env,
    )
    _say("Starting the backend", json_mode)
    _start_backend(repo_root, db_env)
    _wait_for_health()
    result = {
        "status": "rolled_back",
        "commit": commit,
        "backup": str(backup_path),
        "health": "healthy",
    }
    if json_mode:
        return result
    print(f"Rollback complete at {commit}. Restored backup: {backup_path}")
    return 0


def _resolve_repo(configured: str | None) -> Path:
    candidate = (
        Path(configured or os.environ.get("CONDUCTOR_REPO") or Path.cwd())
        .expanduser()
        .resolve()
    )
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise OperationError(
            f"Could not locate the Conductor checkout: {type(exc).__name__}"
        ) from exc
    if result.returncode:
        raise OperationError(
            f"No Git checkout found at {candidate}; pass --repo or set CONDUCTOR_REPO."
        )
    root = Path(result.stdout.strip()).resolve()
    if not (root / "server.py").is_file() or not (root / "alembic.ini").is_file():
        raise OperationError(
            f"The Git checkout at {root} does not look like a Conductor server checkout."
        )
    return root


def _database_environment(repo_root: Path) -> dict[str, str]:
    """Read configured database settings without mutating the CLI process."""
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise OperationError(
            "python-dotenv is required to read the checkout's .env file."
        ) from exc

    file_values = dotenv_values(repo_root / ".env")
    env = os.environ.copy()
    defaults = {
        "DB_HOST": "PGHOST",
        "DB_PORT": "PGPORT",
        "DB_USER": "PGUSER",
        "DB_NAME": "PGDATABASE",
    }
    values = {"DB_HOST": "localhost", "DB_PORT": "5432", "DB_USER": "postgres", "DB_NAME": "postgres"}
    for source, default in values.items():
        value = os.environ.get(source) or file_values.get(source) or default
        env[source] = value
        env[defaults[source]] = value
    env["DB_PASSWORD"] = os.environ.get("DB_PASSWORD") or file_values.get("DB_PASSWORD") or "postgres"
    env["PGPASSWORD"] = env["DB_PASSWORD"]
    env["DB_SSLMODE"] = os.environ.get("DB_SSLMODE") or file_values.get("DB_SSLMODE") or "prefer"
    env["PGSSLMODE"] = env["DB_SSLMODE"]
    return env


def _database_args(env: dict[str, str]) -> list[str]:
    return [
        "--host",
        env["PGHOST"],
        "--port",
        env["PGPORT"],
        "--username",
        env["PGUSER"],
        "--dbname",
        env["PGDATABASE"],
    ]


def _reserve_backup_file(candidate: Path) -> Path:
    """Create a private, exclusive backup destination before pg_dump writes."""
    backup_dir = _active_backup_dir()
    if backup_dir.is_symlink():
        raise OperationError(f"Refusing to use a symlinked backup directory: {backup_dir}")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not backup_dir.is_dir():
        raise OperationError(f"Backup path is not a directory: {backup_dir}")
    backup_dir.chmod(0o700)
    for suffix in range(1000):
        path = (
            candidate
            if suffix == 0
            else candidate.with_name(f"{candidate.stem}-{suffix}{candidate.suffix}")
        )
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return path
    raise OperationError("Could not reserve a unique database backup path.")


def _preflight_deploy(repo_root: Path, *, dry_run: bool = False) -> list[str]:
    blockers = _checkout_blockers(repo_root, deploy=True)
    for relative in (".venv/bin/python", ".venv/bin/alembic"):
        if not (repo_root / relative).is_file():
            blockers.append(f"Required deploy tool is missing: {relative}")
    for executable in ("git", "pg_dump", "pg_restore", "pkill", "pgrep", "nohup"):
        if shutil.which(executable) is None:
            blockers.append(f"Required executable is missing from PATH: {executable}")
    try:
        _database_environment(repo_root)
    except OperationError as exc:
        blockers.append(str(exc))
    blockers.extend(_dispatch_gate_blockers())
    if blockers and not dry_run:
        raise OperationError("Deploy preflight blocked: " + "; ".join(blockers))
    return blockers


def _dispatch_gate_blockers() -> list[str]:
    """Block a deploy while an auto-dispatch is parked at its approval gate.

    A dispatch's integrate branch is neither merged nor pushed until a human
    answers the gate, so deploying over one would ship work nobody has looked
    at. An unreachable backend blocks too: the check is only useful when it
    can actually see the answer, and a deploy that cannot reach the running
    backend it is about to replace has a bigger problem anyway.
    """
    base = _base_url()
    try:
        with urlopen(
            f"{base}{DISPATCH_ACTIVE_PATH}", timeout=DISPATCH_GATE_TIMEOUT_SECONDS
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, OSError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return [f"Could not read pending auto-dispatch approvals from {base}: {exc}"]
    if not isinstance(payload, dict):
        return [f"Unexpected dispatch status payload from {base}"]
    pending = [
        item
        for item in payload.get("dispatches") or []
        if isinstance(item, dict) and item.get("state") == "APPROVAL"
    ]
    return [
        f"Auto-dispatch {item.get('id')} is waiting for approval; run "
        f"`conductor dispatch approve {item.get('id')}` or "
        f"`conductor dispatch reject {item.get('id')}` first"
        for item in pending
    ]


def _base_url() -> str:
    override = os.environ.get("CONDUCTOR_URL")
    if override:
        return override.rstrip("/")
    return HEALTH_URL.rsplit("/health", 1)[0]


def _preflight_rollback(
    commit: str, backup_path: Path, *, repo_root: Path, dry_run: bool = False
) -> list[str]:
    blockers = _checkout_blockers(repo_root, deploy=False)
    if not backup_path.is_file():
        blockers.append(f"Backup file does not exist: {backup_path}")
    elif dry_run:
        blockers.append("Backup archive contents are not validated during dry-run.")
    else:
        _command(
            ["pg_restore", "--list", str(backup_path)],
            repo_root=repo_root,
            display="Validate the PostgreSQL backup archive",
        )
    try:
        _read_git(repo_root, ["cat-file", "-e", f"{commit}^{{commit}}"])
    except OperationError:
        blockers.append(f"Requested commit is not available in this checkout: {commit}")
    for relative in (".venv/bin/python",):
        if not (repo_root / relative).is_file():
            blockers.append(f"Required rollback tool is missing: {relative}")
    for executable in ("git", "pg_restore", "pkill", "pgrep", "nohup"):
        if shutil.which(executable) is None:
            blockers.append(f"Required executable is missing from PATH: {executable}")
    try:
        _database_environment(repo_root)
    except OperationError as exc:
        blockers.append(str(exc))
    if blockers and not dry_run:
        raise OperationError("Rollback preflight blocked: " + "; ".join(blockers))
    return blockers


def _checkout_blockers(repo_root: Path, *, deploy: bool) -> list[str]:
    blockers = []
    try:
        status = _read_git(repo_root, ["status", "--porcelain"])
        if status:
            blockers.append(
                "The checkout has local changes; commit or save them before this operation."
            )
    except OperationError as exc:
        blockers.append(str(exc))
    if deploy:
        try:
            if not _read_git(repo_root, ["branch", "--show-current"]):
                blockers.append("Deploy requires a named Git branch.")
        except OperationError as exc:
            blockers.append(str(exc))
        try:
            if not _read_git(
                repo_root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
            ):
                blockers.append(
                    "Deploy requires a configured Git upstream for fast-forward pull."
                )
        except OperationError:
            blockers.append(
                "Deploy requires a configured Git upstream for fast-forward pull."
            )
    return blockers


def _read_git(repo_root: Path, args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo_root, check=False, capture_output=True, text=True
        )
    except OSError as exc:
        raise OperationError(
            f"Could not inspect Git checkout: {type(exc).__name__}"
        ) from exc
    if result.returncode:
        raise OperationError(
            f"Git preflight failed for {args[0]} (exit status {result.returncode})."
        )
    return result.stdout.strip()


def _command(
    command: list[str],
    *,
    repo_root: Path,
    env: dict[str, str] | None = None,
    dry_run: bool = False,
    display: str | None = None,
    allow_no_process: bool = False,
) -> None:
    if dry_run:
        _say(f"Would run: {display or ' '.join(command)}", json_mode=False)
        return
    result = subprocess.run(
        command, cwd=repo_root, env=env, check=False, capture_output=True, text=True
    )
    if result.returncode and not (allow_no_process and result.returncode == 1):
        raise OperationError(
            f"Command failed with exit status {result.returncode}: {command[0]}"
        )


def _stop_backend(repo_root: Path) -> None:
    _command(
        ["pkill", "-f", "server.py --port 8000"],
        repo_root=repo_root,
        display="Stop server.py --port 8000",
        allow_no_process=True,
    )
    deadline = time.monotonic() + BACKEND_STOP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = subprocess.run(
            ["pgrep", "-f", "server.py --port 8000"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 1:
            return
        if result.returncode:
            raise OperationError(
                f"Could not confirm backend shutdown (exit status {result.returncode})."
            )
        time.sleep(0.2)
    raise OperationError(
        f"Backend process did not stop within {BACKEND_STOP_TIMEOUT_SECONDS}s."
    )


def _backend_log_path() -> Path:
    home = os.environ.get("CONDUCTOR_HOME", "").strip()
    root = Path(home).expanduser() if home else Path.home() / ".conductor"
    return root / "logs" / "backend.log"


def _start_backend(repo_root: Path, env: dict[str, str]) -> None:
    env = env.copy()
    env["PROJECT_ORCHESTRATOR_BASE_URL"] = "http://127.0.0.1:8000"
    log_path = _backend_log_path()
    log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log_path.open("ab") as log:
        subprocess.Popen(
            [
                "nohup",
                ".venv/bin/python",
                "server.py",
                "--host",
                "127.0.0.1",
                "--port",
                "8000",
                "--log-level",
                "warning",
            ],
            cwd=repo_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def _restart_backend(repo_root: Path, env: dict[str, str]) -> None:
    _stop_backend(repo_root)
    _start_backend(repo_root, env)


def _wait_for_health() -> None:
    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urlopen(HEALTH_URL, timeout=2) as response:
                body = json.loads(response.read())
                if (
                    response.status == 200
                    and isinstance(body, dict)
                    and body.get("status") == "healthy"
                ):
                    return
                last_error = f"HTTP {response.status}, status={body.get('status') if isinstance(body, dict) else 'invalid'}"
        except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last_error = type(exc).__name__
        time.sleep(1)
    raise OperationError(
        f"Backend did not become healthy within {HEALTH_TIMEOUT_SECONDS}s: {last_error}"
    )


def _confirm_rollback(
    *, stdin: TextIO | None = None, stdout: TextIO | None = None, json_mode: bool = False
) -> bool:
    stdin = stdin or sys.stdin
    output = stdout or (sys.stderr if json_mode else sys.stdout)
    output.write(
        "This will reset the checkout and replace the database. Type ROLLBACK to continue: "
    )
    output.flush()
    return stdin.readline().strip() == "ROLLBACK"


def _deploy_plan(backup_path: Path) -> list[dict[str, object]]:
    return [
        {"action": "prepare_backup_directory", "command": f"mkdir -p {_active_backup_dir()}"},
        {
            "action": "database_backup",
            "path": str(backup_path),
            "command": f"pg_dump --format=custom --file {backup_path} <configured DB>",
        },
        {"action": "git_pull", "command": "git pull --ff-only"},
        {"action": "database_migrate", "command": ".venv/bin/alembic upgrade head"},
        {
            "action": "backend_stop",
            "command": "pkill -f 'server.py --port 8000'; wait up to 10s for process exit",
        },
        {
            "action": "backend_start",
            "command": "PROJECT_ORCHESTRATOR_BASE_URL=http://127.0.0.1:8000 nohup .venv/bin/python server.py --host 127.0.0.1 --port 8000 --log-level warning > ~/.conductor/logs/backend.log 2>&1 &",
        },
        {
            "action": "health_check",
            "url": HEALTH_URL,
            "timeout_seconds": HEALTH_TIMEOUT_SECONDS,
        },
    ]


def _rollback_plan(commit: str, backup_path: Path) -> list[dict[str, object]]:
    return [
        {
            "action": "validate_backup",
            "command": f"pg_restore --list {backup_path}",
            "requires_existing_file": True,
        },
        {
            "action": "validate_commit",
            "command": f"git cat-file -e {commit}^{{commit}}",
        },
        {
            "action": "backend_stop",
            "command": "pkill -f 'server.py --port 8000'; wait up to 10s for process exit",
        },
        {"action": "git_reset_hard", "command": f"git reset --hard {commit}"},
        {
            "action": "database_restore",
            "command": f"pg_restore --clean --if-exists --no-owner --exit-on-error --single-transaction <configured DB> {backup_path}",
        },
        {
            "action": "backend_start",
            "command": "PROJECT_ORCHESTRATOR_BASE_URL=http://127.0.0.1:8000 nohup .venv/bin/python server.py --host 127.0.0.1 --port 8000 --log-level warning > ~/.conductor/logs/backend.log 2>&1 &",
        },
        {
            "action": "health_check",
            "url": HEALTH_URL,
            "timeout_seconds": HEALTH_TIMEOUT_SECONDS,
        },
    ]


def _emit_plan(plan: list[dict[str, object]]) -> None:
    print("Dry run. Planned actions:")
    for index, action in enumerate(plan, start=1):
        print(
            f"{index}. {action['action']}: "
            + ", ".join(
                f"{key}={value}" for key, value in action.items() if key != "action"
            )
        )


def _emit_blockers(blockers: list[str]) -> None:
    if blockers:
        print("Execution preflight blockers:")
        for blocker in blockers:
            print(f"- {blocker}")
    else:
        print("Execution preflight: ready")


def _say(message: str, json_mode: bool) -> None:
    if not json_mode:
        print(message)
