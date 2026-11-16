"""The lease carries the local policy version the daemon reported."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.database.autonomy import daemons as daemon_db
from tests.conftest import create_test_app

USER_ID = "test-user-123"
DAEMON_ID = "55555555-5555-4555-8555-555555555555"
TICKET_ID = "66666666-6666-4666-8666-666666666666"


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


def _claim_patched(claim):
    """The claim service plus the reads the endpoint makes around it."""
    return (
        patch(
            "src.server.services.autonomy.worker.claim_daemon_ticket",
            new=claim,
        ),
        patch(
            "src.server.app.autonomy.source_db.get_source_any_user",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "src.server.services.autonomy.shared_context.worker_shared_context_text",
            new=AsyncMock(return_value=""),
        ),
    )


def _claimed(policy_version: str = "abc123") -> dict:
    return {
        "ticket_id": TICKET_ID,
        "title": "Check the tree",
        "scope": "run git status",
        "worker_provider": "codex",
        "worker_model": "gpt-5",
        "mandate": {"cwd": "/tmp/work"},
        "lease_id": "77777777-7777-4777-8777-777777777777",
        "lease_expires_at": datetime.now(timezone.utc) + timedelta(seconds=60),
        "lease_duration_seconds": 60,
        "policy_version": policy_version,
    }


@pytest.mark.asyncio
async def test_the_claim_endpoint_returns_the_lease_policy_version(client):
    claim, source, context = _claim_patched(
        AsyncMock(return_value=_claimed())
    )

    with _daemon_authed(), claim, source, context:
        response = await client.post(
            "/api/v1/autonomy/daemon/claim",
            json={"claim_id": "88888888-8888-4888-8888-888888888888"},
        )

    assert response.status_code == 200
    assert response.json()["ticket"]["policy_version"] == "abc123"


@pytest.mark.asyncio
async def test_the_claim_endpoint_hands_the_policy_version_to_the_service(client):
    claim_service = AsyncMock(return_value=_claimed())
    claim, source, context = _claim_patched(claim_service)

    with _daemon_authed(), claim, source, context:
        response = await client.post(
            "/api/v1/autonomy/daemon/claim",
            json={
                "claim_id": "88888888-8888-4888-8888-888888888888",
                "policy_version": "abc123",
            },
        )

    assert response.status_code == 200
    assert claim_service.call_args.kwargs["policy_version"] == "abc123"


@pytest.mark.asyncio
async def test_the_claim_endpoint_carries_the_ticket_scope(client):
    """The device classifies the Ticket's own request, not the launch prompt."""
    claim, source, context = _claim_patched(
        AsyncMock(return_value=_claimed())
    )

    with _daemon_authed(), claim, source, context:
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})

    assert response.status_code == 200
    assert response.json()["ticket"]["scope"] == "run git status"


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        if "FROM autonomy_daemons" in normalized:
            self.row = {
                "daemon_id": "d1",
                "user_id": "u1",
                "provider": "codex",
                "max_concurrency": 1,
            }
        elif "COUNT(*) AS running_count" in normalized:
            self.row = {"running_count": 0}
        elif normalized.startswith("WITH next AS ("):
            self.row = {
                "ticket_id": "t1",
                "worker_lease_expires_at": datetime.now(timezone.utc),
                "worker_provider": "codex",
            }
        elif "worker_lease_claim_id = %s" in normalized:
            # The replay probe: this claim has no earlier response.
            self.row = None
        elif "FROM autonomy_daemon_claims WHERE claim_id=%s" in normalized:
            # This claim id was never issued before.
            self.row = None
        elif normalized.startswith("INSERT INTO autonomy_daemon_claims"):
            self.conn.claim_insert = (normalized, params)
            self.row = None
        elif "INSERT INTO autonomy_ticket_events" in normalized:
            # ob-tickets adds a claim audit event; it has no result row.
            self.row = None
        else:
            raise AssertionError(f"Unexpected claim SQL: {normalized}")

    async def fetchone(self):
        return self.row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Conn:
    def __init__(self):
        self.claim_insert = None

    def transaction(self):
        return _Tx()

    def cursor(self, row_factory=None):
        return _Cursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_the_claim_records_the_policy_version_on_the_lease():
    conn = _Conn()

    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        claimed = await daemon_db.claim_ticket(
            "d1",
            "99999999-9999-4999-8999-999999999999",
            policy_version="abc123",
        )

    assert claimed["policy_version"] == "abc123"
    assert conn.claim_insert is not None
    sql, params = conn.claim_insert
    assert "policy_version" in sql
    assert params[-1] == "abc123"


@pytest.mark.asyncio
async def test_a_claim_without_a_policy_version_records_nothing():
    conn = _Conn()

    with patch(
        "src.server.database.autonomy.daemons.get_db_connection",
        return_value=conn,
    ):
        claimed = await daemon_db.claim_ticket(
            "d1", "99999999-9999-4999-8999-999999999999"
        )

    assert claimed["policy_version"] == ""
    assert conn.claim_insert[1][-1] == ""
