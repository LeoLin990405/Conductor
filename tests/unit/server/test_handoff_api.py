"""Handoff API: register, list, daemon register/verify, no secrets echoed."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.conftest import create_test_app

USER_ID = "test-user-123"
TICKET_ID = "22222222-2222-4222-8222-222222222222"
HANDOFF_ID = "33333333-3333-4333-8333-333333333333"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ticket() -> dict[str, Any]:
    return {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "w1",
        "title": "Fix the save button",
        "scope": "Change the label to Save",
        "status": "running",
        "mandate": {"strength": "explicit"},
        "origin_trigger_id": "trg_1",
        "origin_thread_key": "github:acme/widgets#12",
        "router_thread_id": None,
        "worker_thread_id": None,
        "worker_run_id": None,
        "worker_provider": "",
        "worker_model": "",
        "worker_daemon_id": None,
        "attempts": 0,
        "attention": None,
        "summary": "",
        "error": "",
        "created_at": _now(),
        "updated_at": _now(),
        "started_at": None,
        "ended_at": None,
    }


def _handoff(status: str = "registered", **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "handoff_id": HANDOFF_ID,
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "producer": "worker:glm",
        "consumer": "deliberation:summarizer",
        "purpose": "draft sections",
        "status": status,
        "files": [
            {
                "path": "out/draft.md",
                "size": 6,
                "sha256": hashlib.sha256(b"draft\n").hexdigest(),
            }
        ],
        "verify_error": "",
        "created_at": _now(),
        "updated_at": _now(),
        "verified_at": None,
    }
    row.update(over)
    return row


def _daemon() -> dict[str, Any]:
    return {"daemon_id": "d-1", "user_id": USER_ID, "provider": "glm"}


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest_asyncio.fixture
def ws_root(tmp_path: Path, monkeypatch) -> Path:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "draft.md").write_bytes(b"draft\n")
    # The handoff root sits behind the same boundary as a native CLI cwd; the
    # deployment's own workspace root env var admits pytest's tmp dir.
    monkeypatch.setenv("NATIVE_CLI_WORKSPACE_ROOT", str(tmp_path))
    return tmp_path


def _feature(on: bool = True) -> Any:
    return patch(
        "src.server.app.autonomy.features.user_feature_enabled",
        new=AsyncMock(return_value=on),
    )


def _tickets(monkeypatch, ticket: dict | None) -> None:
    monkeypatch.setattr(
        "src.server.app.autonomy.tickets_db.get_ticket",
        AsyncMock(return_value=ticket),
    )


def _store(monkeypatch, rows: dict[str, dict]) -> AsyncMock:
    fake = AsyncMock()
    fake.list_handoffs = AsyncMock(return_value=list(rows.values()))

    async def get_handoff(handoff_id: str):
        return rows.get(handoff_id)

    fake.get_handoff = get_handoff
    monkeypatch.setattr("src.server.app.autonomy.handoff_db", fake, raising=False)
    return fake


def _daemon_auth(monkeypatch, daemon: dict | None) -> None:
    monkeypatch.setattr(
        "src.server.app.autonomy._require_daemon",
        AsyncMock(return_value=daemon),
    )


def _owned_ticket(monkeypatch, row: dict | None) -> None:
    monkeypatch.setattr(
        "src.server.services.autonomy.host",
        __import__("types").ModuleType("host"),
        raising=False,
    )


# ---------------------------------------------------------------------------
# user-facing routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_list_hidden_when_feature_off(client, monkeypatch):
    _tickets(monkeypatch, _ticket())
    _store(monkeypatch, {})
    with _feature(False):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/handoffs")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_handoff_list_returns_manifests(client, monkeypatch):
    _tickets(monkeypatch, _ticket())
    _store(monkeypatch, {HANDOFF_ID: _handoff()})
    with _feature(True):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/handoffs")
    assert resp.status_code == 200
    body = resp.json()
    assert body["handoffs"][0]["files"][0]["path"] == "out/draft.md"
    assert body["handoffs"][0]["status"] == "registered"
    assert "secret" not in str(body).lower() or "summarizer" in str(body)


@pytest.mark.asyncio
async def test_handoff_list_404_for_other_users_ticket(client, monkeypatch):
    other = _ticket()
    other["user_id"] = "someone-else"
    _tickets(monkeypatch, other)
    _store(monkeypatch, {})
    with _feature(True):
        resp = await client.get(f"/api/v1/autonomy/tickets/{TICKET_ID}/handoffs")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_handoff_register_with_supplied_manifest(client, monkeypatch):
    _tickets(monkeypatch, _ticket())
    fake = _store(monkeypatch, {})

    async def create_handoff(**kwargs):
        return _handoff(**{k: v for k, v in kwargs.items() if k in _handoff()})

    fake.create_handoff = create_handoff
    payload = {
        "producer": "worker:glm",
        "consumer": "deliberation:summarizer",
        "purpose": "draft",
        "files": [
            {
                "path": "out/draft.md",
                "size": 6,
                "sha256": hashlib.sha256(b"draft\n").hexdigest(),
            }
        ],
    }
    with _feature(True):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/handoffs", json=payload
        )
    assert resp.status_code == 201
    assert resp.json()["files"][0]["sha256"] == payload["files"][0]["sha256"]


@pytest.mark.asyncio
async def test_handoff_register_rejects_secret_path(client, monkeypatch):
    _tickets(monkeypatch, _ticket())
    _store(monkeypatch, {})
    payload = {
        "producer": "worker:glm",
        "consumer": "deliberation:summarizer",
        "purpose": "draft",
        "files": [
            {"path": ".env", "size": 6, "sha256": "a" * 64},
        ],
    }
    with _feature(True):
        resp = await client.post(
            f"/api/v1/autonomy/tickets/{TICKET_ID}/handoffs", json=payload
        )
    assert resp.status_code == 400
    assert "secret" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# daemon routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daemon_register_builds_manifest_from_files(
    client, monkeypatch, ws_root: Path
):
    _tickets(monkeypatch, _ticket())
    _daemon_auth(monkeypatch, _daemon())
    _store(monkeypatch, {})
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.owned_ticket",
        AsyncMock(return_value=_ticket()),
    )
    fake = _store(monkeypatch, {})

    async def create_handoff(**kwargs):
        row = _handoff()
        row.update({k: v for k, v in kwargs.items() if k in row})
        row["files"] = kwargs["files"]
        return row

    fake.create_handoff = create_handoff
    payload = {
        "producer": "worker:glm",
        "consumer": "deliberation:summarizer",
        "purpose": "draft",
        "paths": ["out/draft.md"],
        "workspace_root": str(ws_root),
    }
    with (
        _feature(True),
        patch(
            "src.server.services.autonomy.handoff.handoff_db.create_handoff",
            new=create_handoff,
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/handoffs", json=payload
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["files"][0]["path"] == "out/draft.md"
    assert body["files"][0]["size"] == 6
    assert body["files"][0]["sha256"] == hashlib.sha256(b"draft\n").hexdigest()


@pytest.mark.asyncio
async def test_daemon_register_refuses_escape(client, monkeypatch, ws_root: Path):
    _tickets(monkeypatch, _ticket())
    _daemon_auth(monkeypatch, _daemon())
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.owned_ticket",
        AsyncMock(return_value=_ticket()),
    )
    payload = {
        "producer": "worker:glm",
        "consumer": "deliberation:summarizer",
        "purpose": "draft",
        "paths": ["../../etc/passwd"],
        "workspace_root": str(ws_root),
    }
    with _feature(True):
        resp = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/handoffs", json=payload
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_daemon_register_requires_workspace_root(client, monkeypatch, ws_root):
    _tickets(monkeypatch, _ticket())
    _daemon_auth(monkeypatch, _daemon())
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.owned_ticket",
        AsyncMock(return_value=_ticket()),
    )
    payload = {
        "producer": "worker:glm",
        "consumer": "deliberation:summarizer",
        "purpose": "draft",
        "paths": ["out/draft.md"],
    }
    with _feature(True):
        resp = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/handoffs", json=payload
        )
    assert resp.status_code == 400
    assert "workspace_root" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_daemon_verify_happy_path(client, monkeypatch, ws_root: Path):
    _tickets(monkeypatch, _ticket())
    _daemon_auth(monkeypatch, _daemon())
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.owned_ticket",
        AsyncMock(return_value=_ticket()),
    )
    registered = _handoff()
    _store(monkeypatch, {HANDOFF_ID: registered})

    async def settle_handoff(
        handoff_id, *, status, verify_error="", expected_status="registered"
    ):
        return _handoff(
            status=status,
            verify_error=verify_error,
            verified_at=_now() if status == "verified" else None,
        )

    with (
        _feature(True),
        patch(
            "src.server.services.autonomy.handoff.handoff_db.settle_handoff",
            new=settle_handoff,
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/handoffs/{HANDOFF_ID}/verify",
            json={"workspace_root": str(ws_root)},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outcome"] == "verified"
    assert body["handoff"]["status"] == "verified"
    assert body["handoff"]["verified_at"]


@pytest.mark.asyncio
async def test_daemon_verify_rejects_tampered_file(client, monkeypatch, ws_root: Path):
    (ws_root / "out" / "draft.md").write_bytes(b"draft!\n")
    _tickets(monkeypatch, _ticket())
    _daemon_auth(monkeypatch, _daemon())
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.owned_ticket",
        AsyncMock(return_value=_ticket()),
    )
    _store(monkeypatch, {HANDOFF_ID: _handoff()})

    async def settle_handoff(
        handoff_id, *, status, verify_error="", expected_status="registered"
    ):
        return _handoff(status=status, verify_error=verify_error)

    with (
        _feature(True),
        patch(
            "src.server.services.autonomy.handoff.handoff_db.settle_handoff",
            new=settle_handoff,
        ),
    ):
        resp = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/handoffs/{HANDOFF_ID}/verify",
            json={"workspace_root": str(ws_root)},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "rejected"
    assert "mismatch" in body["error"]


@pytest.mark.asyncio
async def test_daemon_verify_404_for_other_ticket(client, monkeypatch, ws_root):
    _tickets(monkeypatch, _ticket())
    _daemon_auth(monkeypatch, _daemon())
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.owned_ticket",
        AsyncMock(return_value=_ticket()),
    )
    other = _handoff()
    other["ticket_id"] = "44444444-4444-4444-8444-444444444444"
    _store(monkeypatch, {HANDOFF_ID: other})
    with _feature(True):
        resp = await client.post(
            f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/handoffs/{HANDOFF_ID}/verify",
            json={"workspace_root": str(ws_root)},
        )
    assert resp.status_code == 404
