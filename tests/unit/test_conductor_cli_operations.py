from __future__ import annotations

import argparse
import io
import os
import stat
from pathlib import Path
from unittest.mock import Mock
from urllib.error import URLError

import pytest

from src.conductor_cli import operations

def test_deploy_dry_run_previews_every_action_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(operations, "BACKUP_DIR", backup_dir)
    monkeypatch.setattr(operations, "_resolve_repo", lambda configured: tmp_path)
    monkeypatch.setattr(
        operations, "_preflight_deploy", lambda repo_root, *, dry_run=False: []
    )
    run = Mock(side_effect=AssertionError("subprocess.run must not be called"))
    popen = Mock(side_effect=AssertionError("subprocess.Popen must not be called"))
    monkeypatch.setattr(operations.subprocess, "run", run)
    monkeypatch.setattr(operations.subprocess, "Popen", popen)
    monkeypatch.setattr(
        operations,
        "_database_environment",
        lambda repo_root: {
            "PGHOST": "localhost",
            "PGPORT": "5432",
            "PGUSER": "postgres",
            "PGDATABASE": "postgres",
        },
    )

    assert (
        operations.handle_deploy(
            argparse.Namespace(dry_run=True, json=False, repo=None)
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "database_backup" in output
    assert "mkdir -p" in output
    assert "git pull --ff-only" in output
    assert ".venv/bin/alembic upgrade head" in output
    assert "pkill" in output
    assert "nohup" in output
    assert "/health" in output
    assert not backup_dir.exists()
    run.assert_not_called()
    popen.assert_not_called()


def test_rollback_requires_exact_confirmation() -> None:
    assert operations._confirm_rollback(
        stdin=io.StringIO("ROLLBACK\n"), stdout=io.StringIO()
    )
    assert not operations._confirm_rollback(
        stdin=io.StringIO("rollback\n"), stdout=io.StringIO()
    )
    assert not operations._confirm_rollback(
        stdin=io.StringIO("yes\n"), stdout=io.StringIO()
    )


def test_rollback_rejects_invalid_sha_before_any_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    command = Mock(side_effect=AssertionError("no command should run"))
    monkeypatch.setattr(operations, "_command", command)

    with pytest.raises(operations.OperationError, match="hexadecimal SHA"):
        operations.handle_rollback(
            argparse.Namespace(
                commit="main; rm -rf /",
                backup=tmp_path / "backup.dump",
                dry_run=True,
                json=False,
                repo=None,
            )
        )

    command.assert_not_called()


def test_rollback_dry_run_previews_restore_and_restart_without_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(operations, "_resolve_repo", lambda configured: tmp_path)
    monkeypatch.setattr(
        operations,
        "_preflight_rollback",
        lambda commit, backup_path, *, repo_root, dry_run=False: [],
    )
    monkeypatch.setattr(
        operations,
        "_database_environment",
        lambda repo_root: {
            "PGHOST": "localhost",
            "PGPORT": "5432",
            "PGUSER": "postgres",
            "PGDATABASE": "postgres",
        },
    )
    monkeypatch.setattr(
        operations,
        "_restart_backend",
        lambda repo_root, env: pytest.fail("backend must not restart in dry-run"),
    )
    monkeypatch.setattr(
        operations,
        "_wait_for_health",
        lambda: pytest.fail("health must not be polled in dry-run"),
    )

    assert (
        operations.handle_rollback(
            argparse.Namespace(
                commit="a1b2c3d",
                backup=tmp_path / "prior.dump",
                dry_run=True,
                json=False,
                repo=None,
            )
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "requires_existing_file=True" in output
    assert "git_reset_hard" in output
    assert "database_restore" in output
    assert "health" in output


def test_deploy_mocked_execution_orders_backup_update_migration_restart_and_health(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(operations, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(operations, "_resolve_repo", lambda configured: tmp_path)
    monkeypatch.setenv("CONDUCTOR_LAST_DEPLOY", str(tmp_path / "last-deploy.json"))
    monkeypatch.setattr(
        operations, "_preflight_deploy", lambda repo_root, *, dry_run=False: []
    )
    db_env = {
        "DB_HOST": "db.local",
        "PGHOST": "db.local",
        "PGPORT": "5432",
        "PGUSER": "operator",
        "PGDATABASE": "conductor",
    }
    monkeypatch.setattr(operations, "_database_environment", lambda repo_root: db_env)
    events: list[str] = []

    def command(argv: list[str], **kwargs: object) -> None:
        events.append(argv[0] + " " + argv[1])
        if argv[0] == "pg_dump":
            Path(argv[argv.index("--file") + 1]).write_bytes(b"mock archive")

    monkeypatch.setattr(operations, "_command", command)
    monkeypatch.setattr(
        operations,
        "_restart_backend",
        lambda repo_root, env: events.append(
            "restart" if env is db_env else "restart-wrong-env"
        ),
    )
    monkeypatch.setattr(operations, "_wait_for_health", lambda: events.append("health"))

    result = operations.handle_deploy(
        argparse.Namespace(dry_run=False, json=True, repo=None)
    )

    assert result["status"] == "deployed"
    assert events == [
        "pg_dump --format=custom",
        "pg_restore --list",
        "git pull",
        ".venv/bin/alembic upgrade",
        "restart",
        "health",
    ]
    assert Path(result["backup"]).parent == tmp_path / "backups"


def test_deploy_failure_before_restart_does_not_start_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(operations, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(operations, "_resolve_repo", lambda configured: tmp_path)
    monkeypatch.setattr(
        operations, "_preflight_deploy", lambda repo_root, *, dry_run=False: []
    )
    monkeypatch.setattr(
        operations,
        "_database_environment",
        lambda repo_root: {
            "PGHOST": "db",
            "PGPORT": "5432",
            "PGUSER": "op",
            "PGDATABASE": "db",
        },
    )
    events: list[str] = []

    def command(argv: list[str], **kwargs: object) -> None:
        events.append(argv[0])
        if argv[0] == "pg_dump":
            Path(argv[argv.index("--file") + 1]).write_bytes(b"mock archive")
        if argv[0] == ".venv/bin/alembic":
            raise operations.OperationError("mock migration failure")

    monkeypatch.setattr(operations, "_command", command)
    monkeypatch.setattr(
        operations,
        "_restart_backend",
        lambda repo_root, env: pytest.fail("must not restart after migration failure"),
    )

    with pytest.raises(operations.OperationError, match="mock migration failure"):
        operations.handle_deploy(
            argparse.Namespace(dry_run=False, json=False, repo=None)
        )

    assert events == ["pg_dump", "pg_restore", "git", ".venv/bin/alembic"]


def test_rollback_mocked_execution_stops_before_reset_and_restore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    backup = tmp_path / "prior.dump"
    backup.touch()
    monkeypatch.setattr(operations, "_resolve_repo", lambda configured: tmp_path)
    monkeypatch.setattr(
        operations,
        "_preflight_rollback",
        lambda commit, backup_path, *, repo_root, dry_run=False: [],
    )
    db_env = {
        "DB_HOST": "db.local",
        "PGHOST": "db.local",
        "PGPORT": "5432",
        "PGUSER": "operator",
        "PGDATABASE": "conductor",
    }
    monkeypatch.setattr(operations, "_database_environment", lambda repo_root: db_env)
    monkeypatch.setattr(operations, "_confirm_rollback", lambda **kwargs: True)
    events: list[str] = []
    monkeypatch.setattr(
        operations, "_stop_backend", lambda repo_root: events.append("stop")
    )
    monkeypatch.setattr(
        operations, "_command", lambda command, **kwargs: events.append(command[0])
    )
    monkeypatch.setattr(
        operations,
        "_start_backend",
        lambda repo_root, env: events.append(
            "start" if env is db_env else "start-wrong-env"
        ),
    )
    monkeypatch.setattr(operations, "_wait_for_health", lambda: events.append("health"))

    result = operations.handle_rollback(
        argparse.Namespace(
            commit="a1b2c3d", backup=backup, dry_run=False, json=True, repo=None
        )
    )

    assert result["status"] == "rolled_back"
    assert events == ["stop", "git", "pg_restore", "start", "health"]


def test_database_environment_reads_dotenv_without_mutating_process_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "DB_HOST=db.internal\nDB_PORT=5544\nDB_USER=operator\nDB_PASSWORD=hidden\nDB_NAME=conductor\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DB_HOST", "")
    for key in ("DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME"):
        monkeypatch.delenv(key, raising=False)
    before = dict(os.environ)

    env = operations._database_environment(tmp_path)

    assert env["PGHOST"] == "db.internal"
    assert env["PGPORT"] == "5544"
    assert env["PGUSER"] == "operator"
    assert env["PGDATABASE"] == "conductor"
    assert env["PGPASSWORD"] == "hidden"
    assert dict(os.environ) == before


def test_database_environment_uses_backend_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_PORT", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_NAME", raising=False)
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    monkeypatch.delenv("DB_SSLMODE", raising=False)

    env = operations._database_environment(tmp_path)

    assert (env["PGHOST"], env["PGPORT"], env["PGUSER"], env["PGDATABASE"]) == (
        "localhost", "5432", "postgres", "postgres"
    )
    assert env["PGPASSWORD"] == "postgres"
    assert env["PGSSLMODE"] == "prefer"


def test_backup_reservation_uses_private_directory_and_exclusive_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(operations, "BACKUP_DIR", backup_dir)
    candidate = backup_dir / "conductor.dump"

    first = operations._reserve_backup_file(candidate)
    second = operations._reserve_backup_file(candidate)

    assert first == candidate
    assert second.name == "conductor-1.dump"
    assert stat.S_IMODE(backup_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(first.stat().st_mode) == 0o600


def test_backup_reservation_rejects_symlinked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "backups"
    link.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(operations, "BACKUP_DIR", link)

    with pytest.raises(operations.OperationError, match="symlinked backup directory"):
        operations._reserve_backup_file(link / "conductor.dump")


class _Response:
    def __init__(self, payload) -> None:
        self._payload = payload

    def read(self) -> bytes:
        import json

        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(payload=None, error=None):
    def open_url(url, timeout=None):
        if error is not None:
            raise error
        assert url.endswith(operations.DISPATCH_ACTIVE_PATH)
        return _Response(payload)

    return open_url


def test_a_pending_dispatch_blocks_the_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        operations,
        "urlopen",
        _urlopen({"dispatches": [{"id": "d1", "state": "APPROVAL"}]}),
    )

    blockers = operations._dispatch_gate_blockers()

    assert blockers == [
        "Auto-dispatch d1 is waiting for approval; "
        "run `conductor dispatch approve d1` or `conductor dispatch reject d1` first"
    ]


def test_a_dispatch_past_the_gate_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        operations,
        "urlopen",
        _urlopen(
            {
                "dispatches": [
                    {"id": "d1", "state": "COMPLETE"},
                    {"id": "d2", "state": "ABORT"},
                ]
            }
        ),
    )

    assert operations._dispatch_gate_blockers() == []


def test_an_unreachable_backend_blocks_the_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations, "urlopen", _urlopen(error=URLError("refused")))

    blockers = operations._dispatch_gate_blockers()

    assert len(blockers) == 1
    assert "Could not read pending auto-dispatch approvals" in blockers[0]


def test_an_unexpected_payload_blocks_the_deploy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(operations, "urlopen", _urlopen(["not", "a", "dict"]))

    blockers = operations._dispatch_gate_blockers()

    assert blockers == ["Unexpected dispatch status payload from http://127.0.0.1:8000"]


def test_the_gate_check_honours_the_url_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[str] = []

    def open_url(url, timeout=None):
        seen.append(url)
        return _Response({"dispatches": []})

    monkeypatch.setattr(operations, "urlopen", open_url)
    monkeypatch.setenv("CONDUCTOR_URL", "http://conductor.internal:9000/")

    assert operations._dispatch_gate_blockers() == []
    assert seen == ["http://conductor.internal:9000/api/v1/dispatch/status/active"]


def test_the_deploy_preflight_reports_the_pending_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operations, "_checkout_blockers", lambda repo_root, **_: [])
    monkeypatch.setattr(operations.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        operations,
        "_database_environment",
        lambda repo_root: {"PGHOST": "db", "PGUSER": "op", "PGDATABASE": "db"},
    )
    monkeypatch.setattr(
        operations,
        "urlopen",
        _urlopen({"dispatches": [{"id": "d1", "state": "APPROVAL"}]}),
    )
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    for tool in ("python", "alembic"):
        (tmp_path / ".venv" / "bin" / tool).touch()

    blockers = operations._preflight_deploy(tmp_path, dry_run=True)

    assert any("Auto-dispatch d1 is waiting for approval" in item for item in blockers)
    with pytest.raises(operations.OperationError, match="waiting for approval"):
        operations._preflight_deploy(tmp_path, dry_run=False)
