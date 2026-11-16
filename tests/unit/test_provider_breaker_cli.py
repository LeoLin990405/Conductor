"""Local-only CLI coverage for daemon breaker status and startup options."""

from __future__ import annotations

import json
import stat
import uuid
from pathlib import Path
from unittest.mock import Mock

from src.conductor_cli import cli, daemon
from src.conductor_cli.provider_breaker import ProviderCircuitBreaker
from scripts import conductor_local_daemon as worker


DAEMON_ID = "12345678-1234-4234-8234-123456789abc"


def test_daemon_status_is_local_and_reads_only_metadata_snapshot(
    monkeypatch, tmp_path: Path, capsys
):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(daemon, "DAEMON_STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "ensure_private_directory", lambda path, create=True: path.mkdir(parents=True, exist_ok=True) if create else None)
    breaker = ProviderCircuitBreaker()
    worker._persist_breaker_status(str(state_dir / f"{DAEMON_ID}.breaker.json"), "codex", breaker)
    api_client = Mock(side_effect=AssertionError("local status must not create APIClient"))
    monkeypatch.setattr(cli, "APIClient", api_client)

    assert cli.main(["daemons", "status", DAEMON_ID]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["daemon_id"] == DAEMON_ID
    assert output["daemon_status"] == "not_running"
    assert output["provider"] == "codex"
    assert output["state"] == "closed"
    assert output["updated_at"]
    assert "token" not in output
    assert "error" not in output
    api_client.assert_not_called()
    snapshot_path = state_dir / f"{DAEMON_ID}.breaker.json"
    assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600


def test_daemon_status_reports_missing_or_stale_snapshot_without_claiming_freshness(
    monkeypatch, tmp_path: Path, capsys
):
    state_dir = tmp_path / "state"
    monkeypatch.setattr(daemon, "DAEMON_STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "ensure_private_directory", lambda path, create=True: path.mkdir(parents=True, exist_ok=True) if create else None)
    monkeypatch.setattr(
        cli,
        "APIClient",
        Mock(side_effect=AssertionError("local status must not create APIClient")),
    )

    assert cli.main(["daemons", "status", DAEMON_ID]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "daemon_id": DAEMON_ID,
        "daemon_status": "not_running",
        "breaker_status": "unavailable",
    }

    state_dir.mkdir(mode=0o700)
    (state_dir / f"{DAEMON_ID}.breaker.json").write_text(
        json.dumps(
            {
                "provider": "codex",
                "updated_at": "2000-01-01T00:00:00Z",
                "state": "open",
                "retry_in_seconds": 20,
            }
        ),
        encoding="utf-8",
    )
    (state_dir / f"{DAEMON_ID}.breaker.json").chmod(0o600)

    assert cli.main(["daemons", "status", DAEMON_ID]) == 0
    stale = json.loads(capsys.readouterr().out)
    assert stale["daemon_status"] == "not_running"
    assert stale["state"] == "open"
    assert stale["updated_at"] == "2000-01-01T00:00:00Z"


def test_daemon_start_forwards_breaker_config_without_reading_auth_or_running_daemon(
    monkeypatch, tmp_path: Path
):
    token_dir = tmp_path / "credentials"
    state_dir = tmp_path / "state"
    monkeypatch.setattr(daemon, "DAEMON_TOKEN_DIR", token_dir)
    monkeypatch.setattr(daemon, "DAEMON_STATE_DIR", state_dir)
    monkeypatch.setattr(daemon, "ensure_private_directory", lambda path, create=True: path.mkdir(parents=True, exist_ok=True) if create else None)
    monkeypatch.setattr(daemon, "_check_private_file", lambda path: None)
    monkeypatch.setattr(daemon, "read_token", lambda path: "fake-test-token")
    monkeypatch.setattr(daemon, "_process_fingerprint", lambda pid: "fingerprint")
    monkeypatch.setattr(daemon.time, "sleep", lambda seconds: None)
    process = Mock(pid=43210, poll=Mock(return_value=None))
    popen = Mock(return_value=process)
    monkeypatch.setattr(daemon.subprocess, "Popen", popen)
    client = Mock(base_url="http://127.0.0.1:8000")
    client.get.return_value = {
        "daemons": [{"daemon_id": DAEMON_ID, "provider": "codex", "max_concurrency": 2}]
    }

    result = daemon.start_local_daemon(
        client,
        DAEMON_ID,
        str(tmp_path),
        True,
        breaker_failure_threshold=5,
        breaker_recovery_success_threshold=2,
        breaker_reset_timeout=11.0,
        breaker_backoff_max=99.0,
        breaker_jitter=0.4,
    )

    command = popen.call_args.args[0]
    assert command[command.index("--breaker-failure-threshold") + 1] == "5"
    assert command[command.index("--breaker-recovery-success-threshold") + 1] == "2"
    assert command[command.index("--breaker-reset-timeout") + 1] == "11.0"
    assert command[command.index("--breaker-backoff-max") + 1] == "99.0"
    assert command[command.index("--breaker-jitter") + 1] == "0.4"
    assert "--once" in command
    assert popen.call_args.kwargs["env"]["CONDUCTOR_DAEMON_TOKEN"] == "fake-test-token"
    assert result["daemon_id"] == DAEMON_ID
    assert uuid.UUID(result["daemon_id"])
