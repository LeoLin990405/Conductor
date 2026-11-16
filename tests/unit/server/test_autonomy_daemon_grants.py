"""A claimed lease carries a server signature, and a revoked one does not run."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.conductor_cli.daemon_grant import SigningKey, TrustedKeys, verify_grant
from src.server.services.autonomy.daemon_grants import (
    attach_lease_grant,
    report_divergence,
)
from tests.conftest import create_test_app

USER_ID = "test-user-123"
DAEMON_ID = "55555555-5555-4555-8555-555555555555"
TICKET_ID = "66666666-6666-4666-8666-666666666666"
LEASE_ID = "77777777-7777-4777-8777-777777777777"
NOW = 1_700_000_000


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _daemon_authed():
    return patch(
        "src.server.app.autonomy._require_daemon",
        new=AsyncMock(return_value={"daemon_id": DAEMON_ID, "user_id": USER_ID}),
    )


def _claimed() -> dict:
    return {
        "ticket_id": TICKET_ID,
        "title": "Check the tree",
        "scope": "run git status",
        "worker_provider": "codex",
        "worker_model": "gpt-5",
        "mandate": {},
        "lease_id": LEASE_ID,
        "lease_expires_at": datetime.now(timezone.utc) + timedelta(seconds=90),
        "lease_duration_seconds": 90,
        "policy_version": "abc123",
    }


def _binding(*, policy_version: str = "abc123", revoked_at=None) -> dict:
    return {
        "daemon_id": DAEMON_ID,
        "policy_version": policy_version,
        "grant": {
            "daemon_id": DAEMON_ID,
            "policy_version": policy_version,
            "nonce": "nonce-value-is-long-enough",
        },
        "revoked_at": revoked_at,
    }


@pytest.mark.asyncio
async def test_a_configured_key_signs_the_lease_once():
    key = SigningKey.generate("server")
    saved: dict[str, dict] = {}

    async def read(daemon_id, lease_id):
        return saved.get("grant")

    async def store(daemon_id, lease_id, grant):
        assert saved.get("grant") is None
        saved["grant"] = grant
        return grant

    with (
        patch("src.server.database.autonomy.daemons.read_lease_grant", new=read),
        patch("src.server.database.autonomy.daemons.store_lease_grant", new=store),
    ):
        first = await attach_lease_grant(
            daemon_id=DAEMON_ID,
            ticket_id=TICKET_ID,
            lease_id=LEASE_ID,
            policy_version="abc123",
            expires_at=NOW + 90,
            signing_key=key,
            now=NOW,
        )
        second = await attach_lease_grant(
            daemon_id=DAEMON_ID,
            ticket_id=TICKET_ID,
            lease_id=LEASE_ID,
            policy_version="abc123",
            expires_at=NOW + 90,
            signing_key=key,
            now=NOW,
        )

    assert first is not None and second is not None
    assert first["nonce"] == second["nonce"]
    assert first["kid"] == "server"
    assert first["daemon_id"] == DAEMON_ID
    assert first["policy_version"] == "abc123"
    verified = verify_grant(first, TrustedKeys.trust(key), now=NOW)
    assert verified.lease_id == LEASE_ID


@pytest.mark.asyncio
async def test_an_unconfigured_server_does_not_sign(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_DAEMON_GRANT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("CONDUCTOR_DAEMON_GRANT_SIGNING_FILE", raising=False)
    read = AsyncMock()

    with patch("src.server.database.autonomy.daemons.read_lease_grant", new=read):
        assert (
            await attach_lease_grant(
                daemon_id=DAEMON_ID,
                ticket_id=TICKET_ID,
                lease_id=LEASE_ID,
                policy_version="abc123",
                expires_at=NOW + 90,
                now=NOW,
            )
            is None
        )

    read.assert_not_awaited()


def test_a_report_must_match_the_signed_daemon_and_policy():
    assert (
        report_divergence(
            _binding(),
            daemon_id=DAEMON_ID,
            policy_version="abc123",
            state="ready",
        )
        == ""
    )
    assert "Policy version" in report_divergence(
        _binding(),
        daemon_id=DAEMON_ID,
        policy_version="changed",
        state="ready",
    )
    assert "Daemon identity" in report_divergence(
        _binding(),
        daemon_id="99999999-9999-4999-8999-999999999999",
        policy_version="abc123",
        state="ready",
    )
    assert report_divergence(None, daemon_id="", policy_version="", state="ready") == ""


def test_a_refusal_may_omit_the_signed_identity():
    binding = _binding()
    for state in ("failed", "dropped"):
        assert (
            report_divergence(
                binding, daemon_id="", policy_version="", state=state
            )
            == ""
        )
    assert "Daemon identity" in report_divergence(
        binding, daemon_id="", policy_version="", state="ready"
    )
    assert "Daemon identity" in report_divergence(
        binding, daemon_id="", policy_version="", state="blocked"
    )
    assert "Daemon identity" in report_divergence(
        binding,
        daemon_id="99999999-9999-4999-8999-999999999999",
        policy_version="",
        state="failed",
    )


def test_a_refusal_survives_a_policy_file_replaced_during_the_run():
    assert (
        report_divergence(
            _binding(),
            daemon_id=DAEMON_ID,
            policy_version="replaced",
            state="failed",
        )
        == ""
    )
    assert "Policy version" in report_divergence(
        _binding(),
        daemon_id=DAEMON_ID,
        policy_version="replaced",
        state="ready",
    )


def test_a_revoked_lease_accepts_only_the_daemon_refusal():
    revoked = _binding(revoked_at=datetime.now(timezone.utc))

    assert "revoked" in report_divergence(
        revoked, daemon_id=DAEMON_ID, policy_version="abc123", state="ready"
    ).lower()
    assert (
        report_divergence(
            revoked, daemon_id=DAEMON_ID, policy_version="abc123", state="failed"
        )
        == ""
    )


@pytest.mark.asyncio
async def test_the_claim_response_carries_the_signed_grant(client, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_DAEMON_GRANT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("CONDUCTOR_DAEMON_GRANT_SIGNING_FILE", raising=False)
    key = SigningKey.generate("server")
    saved: dict[str, dict] = {}

    async def read(daemon_id, lease_id):
        return saved.get("grant")

    async def store(daemon_id, lease_id, grant):
        saved["grant"] = grant
        return grant

    claim = patch(
        "src.server.services.autonomy.worker.claim_daemon_ticket",
        new=AsyncMock(return_value=_claimed()),
    )
    source = patch(
        "src.server.app.autonomy.source_db.get_source_any_user",
        new=AsyncMock(return_value=None),
    )
    context = patch(
        "src.server.services.autonomy.shared_context.worker_shared_context_text",
        new=AsyncMock(return_value=""),
    )
    with (
        _daemon_authed(),
        claim,
        source,
        context,
        patch(
            "src.server.services.autonomy.daemon_grants.configured_signing_key",
            return_value=key,
        ),
        patch("src.server.database.autonomy.daemons.read_lease_grant", new=read),
        patch("src.server.database.autonomy.daemons.store_lease_grant", new=store),
    ):
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})

    assert response.status_code == 200
    body = response.json()
    assert body["ticket"]["daemon_id"] == DAEMON_ID
    verified = verify_grant(
        body["grant"], TrustedKeys.trust(key), now=int(time.time())
    )
    assert verified.policy_version == "abc123"
    assert verified.daemon_id == DAEMON_ID


@pytest.mark.asyncio
async def test_the_lease_status_poll_reports_revocation(client):
    open_lease = {
        "lease_id": LEASE_ID,
        "daemon_id": DAEMON_ID,
        "policy_version": "abc123",
        "revoked": False,
        "revoked_at": None,
    }
    status = AsyncMock(return_value=open_lease)

    with (
        _daemon_authed(),
        patch("src.server.database.autonomy.daemons.lease_status", new=status),
    ):
        response = await client.get(f"/api/v1/autonomy/daemon/lease/{LEASE_ID}/status")

    assert response.status_code == 200
    assert response.json()["revoked"] is False

    status.return_value = {
        **open_lease,
        "revoked": True,
        "revoked_at": datetime(2026, 11, 3, tzinfo=timezone.utc),
    }
    with (
        _daemon_authed(),
        patch("src.server.database.autonomy.daemons.lease_status", new=status),
    ):
        revoked = await client.get(f"/api/v1/autonomy/daemon/lease/{LEASE_ID}/status")

    assert revoked.status_code == 200
    assert revoked.json()["revoked"] is True
    assert revoked.json()["revoked_at"] == "2026-11-03T00:00:00+00:00"


@pytest.mark.asyncio
async def test_the_owner_can_revoke_a_lease(client):
    revoke = AsyncMock(return_value="revoked")

    with (
        patch("src.server.app.autonomy._require_feature", new=AsyncMock()),
        patch("src.server.database.autonomy.daemons.revoke_lease", new=revoke),
    ):
        response = await client.post(
            f"/api/v1/autonomy/daemon/lease/{LEASE_ID}/revoke"
        )

    assert response.status_code == 200
    assert response.json() == {"lease_id": LEASE_ID, "revoked": True}
    revoke.assert_awaited_once_with(USER_ID, LEASE_ID)


@pytest.mark.asyncio
async def test_a_diverging_report_is_refused(client):
    apply = AsyncMock(return_value={"ticket_id": TICKET_ID, "state": "ready"})
    binding = AsyncMock(return_value=_binding())

    with (
        _daemon_authed(),
        patch("src.server.database.autonomy.daemons.lease_binding", new=binding),
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
                "policy_version": "changed",
                "daemon_id": DAEMON_ID,
            },
        )

    assert response.status_code == 409
    assert "Policy version" in response.json()["detail"]
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_matching_report_is_accepted(client):
    apply = AsyncMock(return_value={"ticket_id": TICKET_ID, "state": "ready"})
    report_body = "First worker result line.\nSecond worker result line."

    with (
        _daemon_authed(),
        patch(
            "src.server.database.autonomy.daemons.lease_binding",
            new=AsyncMock(return_value=_binding()),
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
                "body": report_body,
                "policy_version": "abc123",
                "daemon_id": DAEMON_ID,
            },
        )

    assert response.status_code == 200
    apply.assert_awaited_once()
    assert apply.await_args.kwargs["body"] == report_body


@pytest.mark.asyncio
async def test_oversize_utf8_report_body_is_rejected_before_the_handler(client):
    apply = AsyncMock()
    body = "é" * (128 * 1024 + 1)
    assert len(body) < 256 * 1024
    assert len(body.encode("utf-8")) > 256 * 1024

    with (
        _daemon_authed(),
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
                "body": body,
                "policy_version": "abc123",
                "daemon_id": DAEMON_ID,
            },
        )

    assert response.status_code == 422
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refusal_with_no_signed_identity_is_accepted(client):
    apply = AsyncMock(return_value={"ticket_id": TICKET_ID, "state": "failed"})

    with (
        _daemon_authed(),
        patch(
            "src.server.database.autonomy.daemons.lease_binding",
            new=AsyncMock(return_value=_binding()),
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
                "state": "failed",
                "summary": (
                    "Local daemon refused the lease: the signed grant has expired."
                ),
            },
        )

    assert response.status_code == 200
    apply.assert_awaited()


@pytest.mark.asyncio
async def test_an_early_cancel_with_no_signed_identity_is_accepted(client):
    apply = AsyncMock(return_value={"ticket_id": TICKET_ID, "state": "failed"})

    with (
        _daemon_authed(),
        patch(
            "src.server.database.autonomy.daemons.lease_binding",
            new=AsyncMock(return_value=_binding()),
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
                "state": "failed",
                "summary": "Local daemon was cancelled before completing this Ticket.",
            },
        )

    assert response.status_code == 200
    apply.assert_awaited()


@pytest.mark.asyncio
async def test_a_completion_without_the_signed_identity_is_refused(client):
    apply = AsyncMock(return_value={"ticket_id": TICKET_ID, "state": "ready"})

    with (
        _daemon_authed(),
        patch(
            "src.server.database.autonomy.daemons.lease_binding",
            new=AsyncMock(return_value=_binding()),
        ),
        patch(
            "src.server.services.autonomy.host.apply_daemon_worker_report",
            new=apply,
        ),
    ):
        response = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/report",
            json={"lease_id": LEASE_ID, "state": "ready", "summary": "done"},
        )

    assert response.status_code == 409
    assert "Daemon identity" in response.json()["detail"]
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_revocation_detected_at_commit_time_is_a_409(
    client, monkeypatch
):
    """The transaction recheck refusal must be visible, not silently dropped."""
    from src.server.database.autonomy import daemons as daemon_db
    from src.server.services.autonomy import host as host_module

    async def binding(_daemon_id, _lease_id):
        return _binding()

    async def refused(**_kwargs):
        return {"refused": "Lease is revoked"}

    monkeypatch.setattr(daemon_db, "lease_binding", binding)
    monkeypatch.setattr(host_module, "apply_daemon_worker_report", refused)

    with _daemon_authed():
        response = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/report",
            headers={"X-Daemon-Token": "token"},
            json={
                "lease_id": LEASE_ID,
                "state": "ready",
                "summary": "done",
                "daemon_id": DAEMON_ID,
                "policy_version": "abc123",
            },
        )

    assert response.status_code == 409
    assert "revoked" in response.json()["detail"]
