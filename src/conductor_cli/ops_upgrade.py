"""Backup, checkout, migrate, rebuild, and roll back a local Conductor checkout."""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from .ops_fleet import FleetFile
from .ops_runtime import (
    coordination_lock,
    restart_all,
    restore_previous_runtime,
    run_checked,
    stop_all,
)
from .ops_support import OpsError, RealSystem, Settings, ensure_private_dir, redact


def _db_env(settings: Settings) -> dict[str, str]:
    """Read database settings from the environment and the checkout ``.env``.

    Passwords stay in the returned mapping. Callers must not print it.
    """
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise OpsError("python-dotenv is required to read the checkout .env") from exc
    file_values = dotenv_values(settings.repo / ".env")
    if not isinstance(file_values, dict):
        file_values = {}

    def pick(name: str, default: str) -> str:
        env_value = os.environ.get(name)
        file_value = file_values.get(name)
        if isinstance(env_value, str) and env_value:
            return env_value
        if isinstance(file_value, str) and file_value:
            return file_value
        return default

    password = pick("DB_PASSWORD", "postgres")
    settings.sensitive.append(password)
    return {
        "PGHOST": pick("DB_HOST", "localhost"),
        "PGPORT": pick("DB_PORT", "5432"),
        "PGUSER": pick("DB_USER", "postgres"),
        "PGDATABASE": pick("DB_NAME", "postgres"),
        "PGPASSWORD": password,
    }


def _pg_args(db: dict[str, str]) -> list[str]:
    return [
        "--host",
        db["PGHOST"],
        "--port",
        db["PGPORT"],
        "--username",
        db["PGUSER"],
        "--dbname",
        db["PGDATABASE"],
    ]


def reserve_backup(settings: Settings) -> Path:
    ensure_private_dir(settings.backup_dir)
    if settings.backup_dir.is_symlink():
        raise OpsError("refusing a symlinked backup directory")
    os.chmod(settings.backup_dir, 0o700)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    for suffix in range(1000):
        name = (
            f"conductor-{stamp}.dump"
            if suffix == 0
            else f"conductor-{stamp}-{suffix}.dump"
        )
        path = settings.backup_dir / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        return path
    raise OpsError("could not reserve a unique backup path")


def _prune_backups(settings: Settings) -> None:
    dumps = sorted(
        (
            path
            for path in settings.backup_dir.glob("conductor-*.dump")
            if path.is_file() and not path.is_symlink()
        ),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for extra in dumps[settings.backup_keep :]:
        extra.unlink(missing_ok=True)


def backup_database(system: RealSystem, settings: Settings) -> Path:
    db = _db_env(settings)
    path = reserve_backup(settings)
    env = os.environ.copy()
    env.update(db)
    try:
        run_checked(
            system,
            ["pg_dump", "--format=custom", "--file", str(path), *_pg_args(db)],
            cwd=settings.repo,
            env=env,
            secrets=settings.sensitive,
        )
        if not path.is_file() or path.stat().st_size == 0:
            raise OpsError("pg_dump did not create a non-empty backup")
        run_checked(
            system,
            ["pg_restore", "--list", str(path)],
            cwd=settings.repo,
            secrets=settings.sensitive,
        )
    except Exception:
        path.unlink(missing_ok=True)
        raise
    # The previous copies stay until this dump exists and lists cleanly.
    _prune_backups(settings)
    return path


def _git(system: RealSystem, settings: Settings, *args: str) -> str:
    result = run_checked(system, ["git", *args], cwd=settings.repo)
    return result.stdout.strip()


def _tool_env(settings: Settings) -> dict[str, str]:
    tool_env = os.environ.copy()
    tool_env["PATH"] = (
        str(settings.repo / ".venv" / "bin") + os.pathsep + tool_env.get("PATH", "")
    )
    return tool_env


def _uv_sync(system: RealSystem, settings: Settings, tool_env: dict[str, str]) -> None:
    run_checked(
        system,
        ["uv", "sync", "--frozen", "--extra", "cli"],
        cwd=settings.repo,
        env=tool_env,
        secrets=settings.sensitive,
    )


def _web_build(
    system: RealSystem, settings: Settings, tool_env: dict[str, str]
) -> None:
    web = settings.repo / "web"
    run_checked(
        system,
        ["pnpm", "install", "--frozen-lockfile"],
        cwd=web,
        env=tool_env,
        secrets=settings.sensitive,
    )
    run_checked(
        system, ["pnpm", "build"], cwd=web, env=tool_env, secrets=settings.sensitive
    )


def _append_unified_log(settings: Settings, line: str) -> str:
    """One redacted line in the unified log under ``settings.log_dir``.

    Best effort: a full disk or an unreadable directory must not stop a
    rollback. Returns the error name, or an empty string once written.
    """
    try:
        ensure_private_dir(settings.log_dir)
        path = settings.log_dir / "cond.log"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
        os.chmod(path, 0o600)
    except (OSError, OpsError) as exc:
        return type(exc).__name__
    return ""


def _stderr(line: str) -> None:
    try:
        print(line, file=sys.stderr)
    except OSError:
        pass


def _sync_and_build(
    system: RealSystem, settings: Settings, tool_env: dict[str, str]
) -> None:
    """Install and build the checked-out tree before any process is started."""
    _uv_sync(system, settings, tool_env)
    _web_build(system, settings, tool_env)


def _restore_database(system: RealSystem, settings: Settings, backup: Path) -> None:
    db = _db_env(settings)
    env = os.environ.copy()
    env.update(db)
    run_checked(
        system,
        [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--exit-on-error",
            "--single-transaction",
            *_pg_args(db),
            str(backup),
        ],
        cwd=settings.repo,
        env=env,
        secrets=settings.sensitive,
    )


def upgrade(
    system: RealSystem,
    settings: Settings,
    fleet: FleetFile,
    *,
    ref: str,
    restore_db: bool = False,
) -> dict[str, str]:
    """Upgrade this checkout. Code rolls back on failure; the database does not unless asked."""
    if not ref or ref.startswith("-") or "\n" in ref or " " in ref:
        raise OpsError("ref must be a tag, branch, or commit without spaces")
    with coordination_lock(settings):
        return _upgrade_locked(system, settings, fleet, ref=ref, restore_db=restore_db)


def _upgrade_locked(
    system: RealSystem,
    settings: Settings,
    fleet: FleetFile,
    *,
    ref: str,
    restore_db: bool,
) -> dict[str, str]:
    previous = _git(system, settings, "rev-parse", "HEAD")
    backup = backup_database(system, settings)
    checked_out = False
    tool_env = _tool_env(settings)
    try:
        _git(system, settings, "fetch", "--tags", "--force")
        before = run_checked(
            system,
            ["alembic", "current"],
            cwd=settings.repo,
            env=tool_env,
            secrets=settings.sensitive,
        )
        _git(system, settings, "checkout", "--detach", ref)
        checked_out = True
        _uv_sync(system, settings, tool_env)
        run_checked(
            system,
            ["alembic", "upgrade", "head"],
            cwd=settings.repo,
            env=tool_env,
            secrets=settings.sensitive,
        )
        after = run_checked(
            system,
            ["alembic", "current"],
            cwd=settings.repo,
            env=tool_env,
            secrets=settings.sensitive,
        )
        _web_build(system, settings, tool_env)
        restart_all(system, settings, fleet)
        code, body = system.http_get(settings.base_url + "/ready", timeout=5)
        if code != 200 or '"ready"' not in body:
            raise OpsError("ready check failed after upgrade")
    except Exception as exc:
        # Reverse the upgrade and stop at the first step that fails.
        # Order: stop, checkout, sync, rebuild, optional database restore, start, ready.
        message = redact(str(exc), settings.sensitive)
        if not checked_out:
            raise OpsError(message) from exc
        notes: list[str] = []
        log_errors: list[str] = []

        def note(line: str) -> None:
            clean = redact(line, settings.sensitive)
            notes.append(clean)
            _stderr(clean)
            failed = _append_unified_log(settings, clean)
            if failed and not log_errors:
                log_errors.append(failed)
                _stderr(f"[cond] unified log unavailable ({failed}); rollback continues on stderr")

        def run_step(name: str, action) -> bool:  # noqa: ANN001
            note(f"rollback {name} start")
            try:
                detail = action()
            except Exception as step_exc:
                text = redact(str(step_exc), settings.sensitive)
                if name == "runtime" and "ready check" in text:
                    note(f"rollback ready failed {text}")
                note(f"rollback {name} failed {text}")
                return False
            extra = f" {detail}" if detail else ""
            note(f"rollback {name} done{extra}")
            return True

        ok = run_step("stop", lambda: stop_all(system, settings, fleet))
        if ok:

            def _checkout() -> None:
                rolled = system.run(
                    ["git", "checkout", "--detach", previous], cwd=settings.repo
                )
                if rolled.code != 0:
                    raise OpsError("git checkout of the previous ref failed")

            ok = run_step("checkout", _checkout)
        if ok:
            ok = run_step("sync", lambda: _uv_sync(system, settings, tool_env))
        if ok:
            ok = run_step("rebuild", lambda: _web_build(system, settings, tool_env))
        if ok:
            if restore_db:

                def _restore() -> str:
                    if not backup.is_file():
                        raise OpsError("database backup is missing")
                    _restore_database(system, settings, backup)
                    return "ran"

                ok = run_step("restore-db", _restore)
            else:
                note("rollback restore-db skipped")
        if ok:

            def _runtime() -> str:
                code, _body = restore_previous_runtime(system, settings, fleet)
                note(f"rollback ready done code={code}")
                return ""

            run_step("runtime", _runtime)
        summary = "; ".join(notes)
        if log_errors:
            summary += f"; unified log unavailable ({log_errors[0]})"
        raise OpsError(f"{message}; rollback: {summary}") from exc
    return {
        "previous": previous,
        "ref": ref,
        "backup": str(backup),
        "alembic_before": before.stdout.strip(),
        "alembic_after": after.stdout.strip(),
        "ready": "ready",
    }
