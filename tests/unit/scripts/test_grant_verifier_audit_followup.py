"""N44 follow-up: the grant verifier mode reaches the daemons, and a shadow
refusal reaches the server.

S1: ``cond`` passes ``CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE`` to the daemons it
starts (shell first, secrets file as the fallback), and ``cond doctor`` reads
that value, not just its own environment.
S2: a shadow would-refuse decision rides on the daemon's report as
``capability_audit``; the server keeps it on the mandate and the Ticket view
shows it.
S4: the mode goes through the shared rollout-mode normalisation, warning on an
unknown value.

No process is started, no socket opened; files live under ``tmp_path``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.conductor_cli import ops_cli, ops_fleet, ops_runtime, rollout_modes
from src.conductor_cli.daemon_capability import (
    GRANT_VERIFIER_MODE_ENV,
    grant_verifier_mode,
)
from src.conductor_cli.doctor import _grant_verifier_check
from src.conductor_cli.ops_fleet import DaemonSpec

from .test_conductor_local_daemon_capability import (
    RejectingGrantVerifier,
    TrustingGrantVerifier,
    _api,
    _run,
    _store,
    _ticket,
    local_isolation_policy,  # noqa: F401 - autouse fixture: runs reach the CLI
)


@pytest.fixture(autouse=True)
def _clean_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(GRANT_VERIFIER_MODE_ENV, raising=False)


def _secrets(tmp_path: Path, text: str = "") -> Path:
    path = tmp_path / "secrets.env"
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


# --- S1: the mode reaches the daemons ------------------------------------------


def _start_settings(tmp_path: Path, secrets: Path) -> SimpleNamespace:
    workspace_policy = tmp_path / "workspace-policy.json"
    workspace_policy.write_text("{}")
    return SimpleNamespace(
        workspace_policy=workspace_policy,
        isolation_root=tmp_path / "isolation",
        secrets_file=secrets,
        sensitive=[],
        repo=tmp_path / "repo",
        conductor_home=tmp_path / "home",
        worktree_root=tmp_path / "worktrees",
    )


def _capture_start(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, str]]:
    seen: dict[str, dict[str, str]] = {}

    def start(system, settings, component, argv, cwd, env, marker):
        seen["env"] = dict(env)
        return 4242

    monkeypatch.setattr(ops_runtime, "_start_process", start)
    monkeypatch.setattr(ops_runtime, "_token", lambda settings, daemon: "token")
    monkeypatch.setattr(ops_runtime, "daemon_argv", lambda settings, daemon: ["daemon"])
    monkeypatch.setattr(ops_runtime, "_touch_heartbeat", lambda settings, daemon: None)
    monkeypatch.setattr(
        ops_runtime, "_record_registration", lambda settings, daemon, pid: None
    )
    return seen


def test_a_daemon_started_by_cond_gets_the_shell_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(GRANT_VERIFIER_MODE_ENV, "enforce")
    seen = _capture_start(monkeypatch)
    settings = _start_settings(tmp_path, _secrets(tmp_path))

    ops_runtime.start_daemon(object(), settings, DaemonSpec("codex", "codex-1"))

    assert seen["env"][GRANT_VERIFIER_MODE_ENV] == "enforce"
    assert grant_verifier_mode(seen["env"]) == "enforce"


def test_the_provider_allow_list_alone_drops_the_mode(tmp_path, monkeypatch) -> None:
    """Why the pass-through is needed: provider_environment filters it out."""
    monkeypatch.setenv(GRANT_VERIFIER_MODE_ENV, "enforce")
    settings = SimpleNamespace(secrets_file=_secrets(tmp_path), sensitive=[])
    child = ops_fleet.provider_environment(settings, DaemonSpec("codex", "c"))
    assert GRANT_VERIFIER_MODE_ENV not in child


def test_the_secrets_file_is_the_fallback(tmp_path) -> None:
    settings = SimpleNamespace(
        secrets_file=_secrets(tmp_path, f"{GRANT_VERIFIER_MODE_ENV}=enforce\n")
    )
    assert ops_runtime.daemon_rollout_env(settings, {}) == {
        GRANT_VERIFIER_MODE_ENV: "enforce"
    }
    # The shell wins over the file.
    assert ops_runtime.daemon_rollout_env(
        settings, {GRANT_VERIFIER_MODE_ENV: "off"}
    ) == {GRANT_VERIFIER_MODE_ENV: "off"}


def test_a_launchd_daemon_gets_the_mode_from_the_secrets_file(
    tmp_path, monkeypatch
) -> None:
    secrets = _secrets(tmp_path, f"{GRANT_VERIFIER_MODE_ENV}=enforce\n")
    settings = SimpleNamespace(
        secrets_file=secrets,
        sensitive=[],
        repo=tmp_path / "repo",
        conductor_home=tmp_path / "home",
        worktree_root=tmp_path / "worktrees",
    )
    monkeypatch.setattr(
        ops_fleet,
        "load_fleet",
        lambda settings: SimpleNamespace(daemons=[DaemonSpec("codex", "codex-1")]),
    )
    monkeypatch.setattr(ops_runtime, "_token", lambda settings, daemon: "token")
    monkeypatch.setattr(ops_cli, "daemon_argv", lambda settings, daemon: ["daemon"])

    _argv, env, _cwd = ops_cli.foreground_spec(settings, "daemon", "codex", "codex-1")

    assert env[GRANT_VERIFIER_MODE_ENV] == "enforce"


def _policy_without_keys(tmp_path: Path, secrets: Path) -> SimpleNamespace:
    policy = tmp_path / "daemon_policy.yaml"
    policy.write_text("format: 1\n", encoding="utf-8")
    os.chmod(policy, 0o600)
    return SimpleNamespace(
        daemon_policy=policy,
        trusted_keys=tmp_path / "daemon_trusted_keys.json",
        secrets_file=secrets,
    )


def test_doctor_reads_the_daemons_mode_not_only_its_own_env(tmp_path) -> None:
    # The doctor's shell has no mode; the daemons get enforce from secrets.env.
    settings = _policy_without_keys(
        tmp_path, _secrets(tmp_path, f"{GRANT_VERIFIER_MODE_ENV}=enforce\n")
    )
    check = _grant_verifier_check(settings)
    assert check["mode"] == "enforce"
    assert check["ok"] is False


def test_doctor_and_daemon_agree_on_the_shell_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(GRANT_VERIFIER_MODE_ENV, "off")
    settings = _policy_without_keys(
        tmp_path, _secrets(tmp_path, f"{GRANT_VERIFIER_MODE_ENV}=enforce\n")
    )
    check = _grant_verifier_check(settings)
    assert check["mode"] == "off"
    assert check["ok"] is True


# --- S4: one normalisation ------------------------------------------------------


def test_check_modes_reexports_the_shared_normalisation() -> None:
    from src.tools.auto_dispatch import check_modes

    assert check_modes.normalize_mode is rollout_modes.normalize_mode
    assert check_modes.check_mode is rollout_modes.check_mode
    assert (check_modes.OFF, check_modes.SHADOW, check_modes.ENFORCE) == (
        "off",
        "shadow",
        "enforce",
    )
    assert check_modes.MODES == rollout_modes.MODES
    assert check_modes.DEFAULT_MODE == "shadow"


def test_an_unknown_grant_verifier_mode_warns(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert grant_verifier_mode({GRANT_VERIFIER_MODE_ENV: "enforced"}) == "shadow"
    assert GRANT_VERIFIER_MODE_ENV in caplog.text
    assert "'enforced'" in caplog.text


# --- S2: the shadow refusal reaches the server ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("verifier", [RejectingGrantVerifier(), None])
async def test_a_shadow_refusal_rides_on_the_report(
    tmp_path, monkeypatch, verifier
) -> None:
    monkeypatch.setenv(GRANT_VERIFIER_MODE_ENV, "shadow")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    get_adapter = await _run(api, store, verifier=verifier)

    get_adapter.assert_called_once()
    report = api.report.call_args.args[-1]
    assert report["state"] == "ready"
    audit = report["capability_audit"]
    assert audit["mode"] == "shadow"
    assert audit["would_refuse"] is True
    assert "refused the Ticket" in audit["reason"]


@pytest.mark.asyncio
async def test_a_verified_grant_sends_no_audit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(GRANT_VERIFIER_MODE_ENV, "shadow")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    await _run(api, store, verifier=TrustingGrantVerifier())

    assert "capability_audit" not in api.report.call_args.args[-1]


@pytest.mark.asyncio
async def test_off_sends_no_audit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(GRANT_VERIFIER_MODE_ENV, "off")
    store, version = _store(tmp_path)
    api = _api(_ticket(version))

    await _run(api, store, verifier=RejectingGrantVerifier())

    assert "capability_audit" not in api.report.call_args.args[-1]


DAEMON_ID = "55555555-5555-4555-8555-555555555555"
TICKET_ID = "66666666-6666-4666-8666-666666666666"
LEASE_ID = "77777777-7777-4777-8777-777777777777"
AUDIT = {
    "mode": "shadow",
    "would_refuse": True,
    "reason": "Local daemon refused the Ticket: no grant verifier is configured.",
}


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router
    from tests.conftest import create_test_app

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_the_report_endpoint_keeps_the_audit(client) -> None:
    apply = AsyncMock(return_value={"ticket_id": TICKET_ID, "state": "ready"})
    with (
        patch(
            "src.server.app.autonomy._require_daemon",
            new=AsyncMock(return_value={"daemon_id": DAEMON_ID, "user_id": "u-1"}),
        ),
        patch(
            "src.server.database.autonomy.daemons.lease_binding",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.host.apply_daemon_worker_report",
            new=apply,
        ),
    ):
        response = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/report",
            json={
                "lease_id": LEASE_ID,
                "state": "ready",
                "summary": "done",
                "capability_audit": AUDIT,
            },
        )

    assert response.status_code == 200, response.text
    assert apply.await_args.kwargs["capability_audit"] == AUDIT


@pytest.mark.asyncio
async def test_the_audit_is_kept_on_the_mandate() -> None:
    from src.server.database.autonomy import daemons as daemon_db
    from src.server.services.autonomy import host

    ticket = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "status": "running",
        "mandate": {"source_id": "s-1"},
    }
    with (
        patch.object(daemon_db, "daemon_report_context", AsyncMock(return_value=ticket)),
        patch.object(
            daemon_db, "commit_daemon_report", AsyncMock(return_value={})
        ) as commit,
    ):
        await host.apply_daemon_worker_report(
            daemon_id="d-1",
            ticket_id="t-1",
            lease_id="lease-1",
            state="ready",
            summary="Done.",
            capability_audit=AUDIT,
        )
    assert commit.await_args.kwargs["mandate_patch"] == {"capability_audit": AUDIT}


def test_the_ticket_view_shows_the_audit() -> None:
    from datetime import UTC, datetime

    from src.server.models.autonomy import TicketView

    now = datetime.now(UTC)
    row = {
        "ticket_id": "t-1",
        "user_id": "u-1",
        "workspace_id": "w-1",
        "title": "t",
        "scope": "s",
        "status": "completed",
        "mandate": {"capability_audit": AUDIT},
        "origin_thread_key": "k",
        "attempts": 1,
        "summary": "",
        "error": "",
        "created_at": now,
        "updated_at": now,
    }
    assert TicketView.model_validate(row).capability_audit == AUDIT
    assert TicketView.model_validate({**row, "mandate": {}}).capability_audit is None
