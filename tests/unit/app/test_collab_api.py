"""HTTP tests for ``/api/v1/collab`` (N80 L2: the collab routes).

The router is mounted on a bare FastAPI app and driven with Starlette's
in-process TestClient: no socket, no database, no vault, no real token. Every
persistence call goes to the in-memory ``FakeStore`` from the collab service
tests, and HOME is a temporary directory with a real-HOME tripwire.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.server.app.collab as collab_routes
from src.collab import ratelimit
from src.collab.schemas import validate_tool_output
from src.collab.scopes import TOOL_SCOPES
from src.server.services.collab import leases as lease_svc
from src.server.services.collab import report as report_svc
from src.server.services.collab import tools
from tests.unit._isolated_home import assert_untouched, isolate
from tests.unit.services.collab.collab_fakes import USER, FakeStore, install

REPO = Path(__file__).resolve().parents[3]
PREFIX = "/api/v1/collab"
BASE_SHA = "b" * 40
OPERATOR_TOKEN = "fake-operator-credential"
ENVELOPE_KEYS = {"ok", "code", "data", "untrusted_fields", "redacted"}
ALL_SCOPES = ("read", "propose", "claim", "report", "ask", "review")

#: The N80 4.1 contract, copied as data: tool -> (method, path).
TOOL_ROUTES: dict[str, tuple[str, str]] = {
    "whoami": ("GET", "/whoami"),
    "task_list": ("GET", "/tasks"),
    "task_get": ("GET", "/tasks/{ref}"),
    "task_create": ("POST", "/tasks"),
    "task_claim": ("POST", "/tasks/{ref}/claim"),
    "task_heartbeat": ("POST", "/leases/{lease_id}/heartbeat"),
    "task_release": ("POST", "/leases/{lease_id}/release"),
    "task_report": ("POST", "/leases/{lease_id}/report"),
    "handoff_request": ("POST", "/leases/{lease_id}/handoff"),
    "handoff_list": ("GET", "/handoffs"),
    "handoff_accept": ("POST", "/handoffs/{handoff_id}/accept"),
    "agent_ask": ("POST", "/messages"),
    "agent_inbox": ("GET", "/messages/inbox"),
    "review_request": ("POST", "/reviews"),
    "review_list": ("GET", "/reviews"),
    "review_submit": ("POST", "/reviews/{review_id}/verdict"),
}
ADMIN_ROUTES: dict[str, tuple[str, str]] = {
    "apps_register": ("POST", "/admin/apps"),
    "apps_list": ("GET", "/admin/apps"),
    "apps_scopes": ("POST", "/admin/apps/{app_id}/scopes"),
    "apps_rotate": ("POST", "/admin/apps/{app_id}/rotate"),
    "apps_revoke": ("POST", "/admin/apps/{app_id}/revoke"),
    "apps_audit": ("GET", "/admin/apps/{app_id}/audit"),
}

_PATH_VALUES = {
    "{ref}": "t:00000000-0000-4000-8000-000000000000",
    "{lease_id}": "00000000-0000-4000-8000-000000000001",
    "{handoff_id}": "00000000-0000-4000-8000-000000000002",
    "{review_id}": "00000000-0000-4000-8000-000000000003",
    "{app_id}": "app-a",
}


def _concrete(path: str) -> str:
    for placeholder, value in _PATH_VALUES.items():
        path = path.replace(placeholder, value)
    return PREFIX + path


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _operator(token: str = OPERATOR_TOKEN, user: str = USER) -> dict[str, str]:
    """The operator credential, sent the way every service caller sends it."""
    return {"X-Service-Token": token, "X-User-Id": user}


def _report(status: str = "done") -> dict[str, Any]:
    return {
        "status": status,
        "summary": "Updated the protocol docs.",
        "artifacts": {"commits": ["c" * 40], "files": ["docs/protocol.md"], "pr": None},
        "verification": [{"ran": "docs build", "result": "passed"}],
        "next": "",
        "handbacks": [],
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_collab_api_home(tmp_path, monkeypatch):
    home, original, before = isolate(tmp_path, monkeypatch)
    yield home
    assert_untouched(original, before)


@pytest.fixture(autouse=True)
def fresh_default_limiter():
    ratelimit.reset_default_limiter()
    yield
    ratelimit.reset_default_limiter()


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    tools.load_handlers()
    fake = install(monkeypatch, FakeStore())
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "full")
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    # A fake (the fake vault never encrypts); register/rotate refuse without one.
    monkeypatch.setenv("BYOK_ENCRYPTION_KEY", "fake-test-encryption-key")

    async def worktree(ticket: dict[str, Any]) -> dict[str, Any]:
        return {
            "path": f"/tmp/collab-wt/{ticket['ticket_id']}",
            "branch": f"conductor/ticket/{ticket['ticket_id']}",
            "base_sha": BASE_SHA,
            "push_guard": True,
        }

    monkeypatch.setattr(lease_svc, "prepare_claim_worktree", worktree)

    async def facts(lease: dict[str, Any]) -> report_svc.GitFacts:
        return fake.git_facts

    fake.git_facts = report_svc.GitFacts(
        commit_delivery="delivered",
        dirty_summary=None,
        changed_paths=("docs/protocol.md",),
        branch=None,
        has_upstream=False,
    )
    monkeypatch.setattr(report_svc, "collect_git_facts", facts)
    return fake


@pytest.fixture
def operator(monkeypatch):
    """The operator credential: the (fake) internal service token.

    ``INTERNAL_SERVICE_TOKEN`` is ``OPERATOR_TOKEN`` for the server, and the
    real user resolver runs behind a recorder: the list holds the
    ``X-Service-Token`` of every call that got as far as resolving a user.
    """
    import src.server.utils.api as api_utils

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", OPERATOR_TOKEN)
    monkeypatch.setattr(api_utils, "_SERVICE_TOKEN", OPERATOR_TOKEN)
    real = api_utils.get_current_user_id
    seen: list[str | None] = []

    async def recording_user(request, credentials=None):
        seen.append(request.headers.get("x-service-token"))
        return await real(request, credentials)

    monkeypatch.setattr(api_utils, "get_current_user_id", recording_user)
    return seen


@pytest.fixture
def app() -> FastAPI:
    built = FastAPI()
    built.include_router(collab_routes.router)
    return built


@pytest.fixture
def client(app, store) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


@pytest.fixture
def remote_client(app, store) -> TestClient:
    return TestClient(app, client=("10.1.2.3", 50000))


def _assert_envelope(body: dict[str, Any]) -> None:
    assert ENVELOPE_KEYS <= set(body)
    assert set(body) <= ENVELOPE_KEYS | {"message", "retry_after"}


# ---------------------------------------------------------------------------
# 1. Contract table
# ---------------------------------------------------------------------------


def test_contract_tools_match_scope_table():
    assert set(TOOL_ROUTES) == set(TOOL_SCOPES)
    assert len(TOOL_ROUTES) == 16


@pytest.mark.parametrize(
    "name,method,path",
    [(name, method, path) for name, (method, path) in {**TOOL_ROUTES, **ADMIN_ROUTES}.items()]
    + [("health", "GET", "/health")],
)
def test_contract_route_exists(name, method, path):
    routes = {
        (route.path, frozenset(getattr(route, "methods", ()) or ()))
        for route in collab_routes.router.routes
    }
    full = PREFIX + path
    assert any(p == full and method in methods for p, methods in routes), (name, method, full)


def test_contract_no_extra_routes():
    expected = {(m, PREFIX + p) for m, p in {**TOOL_ROUTES, **ADMIN_ROUTES}.values()}
    expected.add(("GET", PREFIX + "/health"))
    actual = set()
    for route in collab_routes.router.routes:
        for method in getattr(route, "methods", ()) or ():
            if method != "HEAD":
                actual.add((method, route.path))
    assert actual == expected


# ---------------------------------------------------------------------------
# 2. Mode off
# ---------------------------------------------------------------------------


def test_mode_off_refuses_every_tool_route_and_audits(client, store, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_COLLAB_MODE", raising=False)
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    for tool, (method, path) in TOOL_ROUTES.items():
        before = len(store.audit)
        if method == "GET":
            response = client.get(_concrete(path), headers=_bearer(token))
        else:
            response = client.post(_concrete(path), headers=_bearer(token), json={})
        assert response.status_code == 403, tool
        body = response.json()
        _assert_envelope(body)
        assert body["ok"] is False and body["code"] == "mode_denied", tool
        assert len(store.audit) == before + 1, tool
        row = store.audit[-1]
        assert row["tool"] == tool and row["code"] == "mode_denied"
        assert row["http_status"] == 403


def test_mode_off_refuses_every_admin_route(client, store, monkeypatch, operator):
    monkeypatch.delenv("CONDUCTOR_COLLAB_MODE", raising=False)
    store.add_app("app-a")
    for name, (method, path) in ADMIN_ROUTES.items():
        before = len(store.audit)
        headers = _operator()
        if method == "GET":
            response = client.get(_concrete(path), headers=headers)
        else:
            response = client.post(_concrete(path), headers=headers, json={"app_id": "app-z"})
        assert response.status_code == 403, name
        body = response.json()
        _assert_envelope(body)
        assert body["code"] == "mode_denied", name
        assert len(store.audit) == before + 1, name
        assert store.audit[-1]["tool"] == name
    assert "app-z" not in store.apps
    assert operator == []  # the operator credential is never even resolved


def test_mode_off_health_is_open(client, store, monkeypatch):
    monkeypatch.delenv("CONDUCTOR_COLLAB_MODE", raising=False)
    response = client.get(PREFIX + "/health")
    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert body["ok"] is True
    assert body["data"]["mode"] == "off"
    assert body["data"]["tools"] == []


def test_health_full_mode_lists_tools(client):
    body = client.get(PREFIX + "/health").json()
    assert body["data"]["mode"] == "full"
    assert set(body["data"]["tools"]) == set(TOOL_SCOPES)


def test_health_refuses_non_loopback(remote_client):
    response = remote_client.get(PREFIX + "/health")
    assert response.status_code == 403
    assert response.json()["code"] == "scope_denied"


# ---------------------------------------------------------------------------
# 3. Auth over HTTP
# ---------------------------------------------------------------------------


def _whoami(client: TestClient, headers: dict[str, str] | None = None):
    return client.get(PREFIX + "/whoami", headers=headers or {})


def test_auth_missing_token_is_401(client, store):
    response = _whoami(client)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert store.audit[-1]["code"] == "unauthorized"
    assert store.audit[-1]["tool"] == "whoami"


def test_auth_wrong_token_is_401(client, store):
    store.add_app("app-a")
    response = _whoami(client, _bearer("not-a-registered-fake-token"))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert len(store.audit) == 1 and store.audit[0]["code"] == "unauthorized"


def test_auth_malformed_scheme_is_401(client, store):
    token = store.add_app("app-a")
    response = _whoami(client, {"Authorization": f"Basic {token}"})
    assert response.status_code == 401
    assert store.audit[-1]["code"] == "unauthorized"


def test_auth_revoked_token_is_401(client, store):
    token = store.add_app("app-a", revoked=True)
    response = _whoami(client, _bearer(token))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert store.audit[-1]["code"] == "unauthorized"


def test_auth_service_token_headers_are_401(client, store):
    token = store.add_app("app-a")
    for extra in (
        {"X-Service-Token": "fake-service-token-123"},
        {"X-User-Id": USER},
        {"X-Service-Token": "fake-service-token-123", "X-User-Id": USER},
    ):
        before = len(store.audit)
        response = _whoami(client, {**_bearer(token), **extra})
        assert response.status_code == 401, extra
        assert response.json()["code"] == "unauthorized"
        assert len(store.audit) == before + 1


def test_auth_internal_service_token_as_bearer_is_401(client, store, monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "fake-internal-service-token")
    response = _whoami(client, _bearer("fake-internal-service-token"))
    assert response.status_code == 401
    assert store.audit[-1]["code"] == "unauthorized"


def test_auth_non_loopback_is_403(remote_client, store):
    token = store.add_app("app-a")
    response = _whoami(remote_client, _bearer(token))
    assert response.status_code == 403
    assert response.json()["code"] == "scope_denied"
    assert store.audit[-1]["code"] == "scope_denied"
    assert store.audit[-1]["http_status"] == 403


def test_auth_forwarded_loopback_is_403(client, store):
    token = store.add_app("app-a")
    response = _whoami(client, {**_bearer(token), "X-Forwarded-For": "10.9.9.9"})
    assert response.status_code == 403
    assert store.audit[-1]["code"] == "scope_denied"


def test_auth_valid_token_whoami(client, store):
    token = store.add_app("app-a", scopes=("read", "claim"))
    response = _whoami(client, {**_bearer(token), "X-Collab-Agent": "codex"})
    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert body["data"]["app_id"] == "app-a"
    assert body["data"]["scopes"] == ["claim", "read"]
    assert body["data"]["agent"] == "codex"
    assert store.audit[-1]["code"] == "ok" and store.audit[-1]["app_id"] == "app-a"


def test_scope_denied_over_http(client, store):
    token = store.add_app("app-a", scopes=("read",))
    ticket = store.add_ticket()
    response = client.post(
        f"{PREFIX}/tasks/t:{ticket['ticket_id']}/claim",
        headers=_bearer(token),
        json={"revision": ticket["revision"]},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "scope_denied"
    assert store.leases == {}


# ---------------------------------------------------------------------------
# 4. Query typing, bodies and refs
# ---------------------------------------------------------------------------


def test_task_list_query_typing(client, store):
    token = store.add_app("app-a")
    store.add_ticket(status="queued")
    store.add_ticket(status="running")
    store.add_ticket(status="ready")
    response = client.get(
        PREFIX + "/tasks?claimable=false&limit=5&status=queued&status=running",
        headers=_bearer(token),
    )
    assert response.status_code == 200, response.json()
    assert response.json()["ok"] is True


def test_query_coercion_unit():
    properties = {"type": "boolean"}
    assert collab_routes._coerce("true", properties) is True
    assert collab_routes._coerce("false", properties) is False
    assert collab_routes._coerce("5", {"type": "integer"}) == 5
    assert collab_routes._coerce("abc", {"type": "integer"}) == "abc"


def test_task_list_bad_limit_is_400(client, store):
    token = store.add_app("app-a")
    response = client.get(PREFIX + "/tasks?limit=abc", headers=_bearer(token))
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_schema"
    assert store.audit[-1]["code"] == "invalid_schema"


def test_task_list_bad_boolean_is_400(client, store):
    token = store.add_app("app-a")
    response = client.get(PREFIX + "/tasks?claimable=yes", headers=_bearer(token))
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_schema"


def test_task_list_unknown_query_key_is_400(client, store):
    token = store.add_app("app-a")
    response = client.get(PREFIX + "/tasks?surprise=1", headers=_bearer(token))
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_schema"


@pytest.mark.parametrize("payload", [b"[1, 2]", b"\"text\"", b"{not json", b"\xff\xfe"])
def test_post_non_object_body_is_400(client, store, payload):
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    ticket = store.add_ticket()
    response = client.post(
        f"{PREFIX}/tasks/t:{ticket['ticket_id']}/claim",
        headers={**_bearer(token), "Content-Type": "application/json"},
        content=payload,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_schema"
    assert store.audit[-1]["code"] == "invalid_schema"
    assert store.leases == {}


def test_path_param_overrides_body(client, store):
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    ticket = store.add_ticket()
    other = store.add_ticket()
    response = client.post(
        f"{PREFIX}/tasks/t:{ticket['ticket_id']}/claim",
        headers=_bearer(token),
        json={"revision": ticket["revision"], "ref": f"t:{other['ticket_id']}"},
    )
    assert response.status_code == 200, response.json()
    assert response.json()["data"]["ref"] == f"t:{ticket['ticket_id']}"


@pytest.mark.parametrize("sep", [":", "%3A", "%3a"])
def test_task_get_ref_with_colon(client, store, sep):
    token = store.add_app("app-a")
    ticket = store.add_ticket()
    response = client.get(f"{PREFIX}/tasks/t{sep}{ticket['ticket_id']}", headers=_bearer(token))
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert f"t:{ticket['ticket_id']}" in response.text
    assert data


def test_task_get_bad_ref_is_400(client, store):
    token = store.add_app("app-a")
    response = client.get(f"{PREFIX}/tasks/not-a-ref", headers=_bearer(token))
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_schema"


# ---------------------------------------------------------------------------
# 5. End to end and rate limiting
# ---------------------------------------------------------------------------


def test_claim_heartbeat_report_done_over_http(client, store):
    token = store.add_app("app-a", scopes=("read", "claim", "report"), can_write=True)
    ticket = store.add_ticket()
    ref = f"t:{ticket['ticket_id']}"
    headers = _bearer(token)

    got = client.get(f"{PREFIX}/tasks/{ref}", headers=headers)
    assert got.status_code == 200, got.json()

    claim = client.post(
        f"{PREFIX}/tasks/{ref}/claim", headers=headers, json={"revision": ticket["revision"]}
    )
    assert claim.status_code == 200, claim.json()
    data = claim.json()["data"]
    lease_id = data["lease_id"]
    assert data["ref"] == ref
    assert data["worktree"]["base_sha"] == BASE_SHA
    assert store.tickets[ticket["ticket_id"]]["status"] == "running"

    beat = client.post(f"{PREFIX}/leases/{lease_id}/heartbeat", headers=headers, json={"progress": "half"})
    assert beat.status_code == 200, beat.json()

    done = client.post(f"{PREFIX}/leases/{lease_id}/report", headers=headers, json={"report": _report()})
    assert done.status_code == 200, done.json()
    assert done.json()["data"]["ticket_state"] == "ready"
    assert done.json()["data"]["reconciled"]["vetoed"] is False
    assert store.leases[lease_id]["state"] == "completed"
    assert store.tickets[ticket["ticket_id"]]["status"] == "ready"
    assert len(store.reports) == 1

    # A finished lease cannot be heartbeated again.
    again = client.post(f"{PREFIX}/leases/{lease_id}/heartbeat", headers=headers, json={})
    assert again.status_code == 409
    assert again.json()["code"] in {"lease_lost", "lease_expired"}

    ok_tools = [row["tool"] for row in store.audit if row["code"] == "ok"]
    assert ok_tools == ["task_get", "task_claim", "task_heartbeat", "task_report"]


def _handed_over_lease(client: TestClient, store: FakeStore) -> tuple[str, str, str]:
    """codex claims a task and hands it to claude-code; returns (token, handoff_id, lease_id)."""
    codex = store.add_app("codex", scopes=("read", "claim", "report"), can_write=True)
    claude = store.add_app("claude-code", scopes=("read", "claim", "report"), can_write=True)
    ticket = store.add_ticket()
    claim = client.post(
        f"{PREFIX}/tasks/t:{ticket['ticket_id']}/claim",
        headers=_bearer(codex),
        json={"revision": ticket["revision"]},
    )
    assert claim.status_code == 200, claim.json()
    lease_id = claim.json()["data"]["lease_id"]
    handoff = client.post(
        f"{PREFIX}/leases/{lease_id}/handoff",
        headers=_bearer(codex),
        json={"to": "claude-code", "reason": "quota_exhausted"},
    )
    assert handoff.status_code == 200, handoff.json()
    return claude, handoff.json()["data"]["handoff_id"], lease_id


def test_handoff_accept_over_http_names_the_heartbeat_interval(client, store):
    claude, handoff_id, lease_id = _handed_over_lease(client, store)
    accepted = client.post(f"{PREFIX}/handoffs/{handoff_id}/accept", headers=_bearer(claude), json={})
    assert accepted.status_code == 200, accepted.json()
    data = accepted.json()["data"]
    assert data["lease_id"] == lease_id
    assert data["heartbeat_every_seconds"] == lease_svc.HEARTBEAT_EVERY_SECONDS
    validate_tool_output("handoff_accept", data)


def test_l3_heartbeat_tracks_an_accepted_lease_against_the_asgi_app(app, client, store, monkeypatch):
    """The L3 client + AutoHeartbeat, in process, against the real routes."""
    from src.collab_mcp.client import CollabClient
    from src.collab_mcp.heartbeat import AutoHeartbeat
    from src.server.services.collab import handoff as handoff_svc

    # A server interval unlike the client default proves where it came from.
    monkeypatch.setattr(handoff_svc, "HEARTBEAT_EVERY_SECONDS", 20)
    claude, handoff_id, lease_id = _handed_over_lease(client, store)
    asgi = TestClient(app, client=("127.0.0.1", 50001))

    class InProcess(httpx.BaseTransport):
        """Hands each request to the ASGI app; no socket is ever opened."""

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            answer = asgi.request(
                request.method, request.url.path, params=request.url.params,
                content=request.content, headers=request.headers,
            )
            return httpx.Response(answer.status_code, headers=answer.headers, content=answer.content)

    now = [1000.0]
    with CollabClient(claude, base_url="http://127.0.0.1:8000", transport=InProcess()) as l3:
        beat = AutoHeartbeat(
            lambda lease: l3.call("task_heartbeat", {"lease_id": lease}).envelope,
            clock=lambda: now[0],
        )
        beat.note_activity()
        arguments = {"handoff_id": handoff_id}
        response = l3.call("handoff_accept", arguments)
        assert response.ok, response.envelope
        beat.observe("handoff_accept", arguments, response.envelope)
        assert beat.leases == (lease_id,)

        now[0] += 19
        assert beat.tick() == []
        now[0] += 2
        assert beat.tick() == [lease_id]
    beats = [row for row in store.audit if row["tool"] == "task_heartbeat"]
    assert [row["code"] for row in beats] == ["ok"]
    assert store.leases[lease_id]["app_id"] == "claude-code"


def test_second_claim_is_conflict(client, store):
    first = store.add_app("app-a", scopes=ALL_SCOPES)
    second = store.add_app("app-b", scopes=ALL_SCOPES)
    ticket = store.add_ticket()
    ref = f"t:{ticket['ticket_id']}"
    assert client.post(f"{PREFIX}/tasks/{ref}/claim", headers=_bearer(first), json={"revision": 3}).status_code == 200
    response = client.post(f"{PREFIX}/tasks/{ref}/claim", headers=_bearer(second), json={"revision": 3})
    assert response.status_code == 409
    assert len(store.leases) == 1


def test_rate_limit_429_carries_retry_after(client, store):
    token = store.add_app("app-a")
    headers = _bearer(token)
    for _ in range(120):
        assert _whoami(client, headers).status_code == 200
    response = _whoami(client, headers)
    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "rate_limited"
    retry = response.headers.get("Retry-After")
    assert retry is not None and retry.isdigit() and int(retry) >= 1
    assert store.audit[-1]["code"] == "rate_limited"
    assert store.audit[-1]["rate_limited"] is True


def test_respond_only_sets_retry_after_on_429():
    plain = collab_routes._respond(409, {"ok": False, "code": "x", "retry_after": 3})
    assert "retry-after" not in plain.headers
    limited = collab_routes._respond(429, {"ok": False, "code": "rate_limited", "retry_after": 0.2})
    assert limited.headers["retry-after"] == "1"


# ---------------------------------------------------------------------------
# 6. Admin routes
# ---------------------------------------------------------------------------


def _register(client: TestClient, app_id: str = "app-new", **extra: Any):
    return client.post(
        PREFIX + "/admin/apps",
        headers=_operator(),
        json={"app_id": app_id, **extra},
    )


def test_admin_register_returns_token_once(client, store, operator):
    response = _register(client, scopes=["read", "claim"], can_write=True, name="Codex")
    assert response.status_code == 200, response.json()
    body = response.json()
    _assert_envelope(body)
    token = body["data"]["token"]
    assert isinstance(token, str) and len(token) >= 32
    row = store.apps["app-new"]
    assert row["token_sha256"] == hashlib.sha256(token.encode()).hexdigest()
    assert token not in repr(row)
    assert body["data"]["token_prefix"] == row["token_sha256"][:8]
    assert body["data"]["scopes"] == ["claim", "read"]
    assert store.vault[(USER, "app-new")] == token
    assert operator == [OPERATOR_TOKEN]
    assert store.audit[-1]["tool"] == "apps_register" and store.audit[-1]["code"] == "ok"
    for audit_row in store.audit:
        assert token not in repr(audit_row)

    # The new token works on a tool route.
    who = _whoami(client, _bearer(token))
    assert who.status_code == 200 and who.json()["data"]["app_id"] == "app-new"
    assert token not in who.text

    # Registering the same id again is refused and no second token is issued.
    dup = _register(client)
    assert dup.status_code == 409
    assert "token" not in dup.json()["data"]


def test_admin_register_defaults_to_read(client, store, operator):
    response = _register(client, "app-plain")
    assert response.status_code == 200
    assert response.json()["data"]["scopes"] == ["read"]


def test_admin_register_bad_body(client, store, operator):
    assert _register(client, scopes=["root"]).status_code == 400
    assert _register(client, max_leases=9).status_code == 400
    assert _register(client, surprise=True).status_code == 400
    bad = client.post(
        PREFIX + "/admin/apps",
        headers={**_operator(), "Content-Type": "application/json"},
        content=b"[1]",
    )
    assert bad.status_code == 400
    assert store.apps == {}


def test_admin_list_has_prefix_never_token(client, store, operator):
    token = _register(client, "app-a").json()["data"]["token"]
    store.add_app("app-other-user", user_id="user-2")
    response = client.get(PREFIX + "/admin/apps", headers=_operator())
    assert response.status_code == 200
    apps = response.json()["data"]["apps"]
    assert [item["app_id"] for item in apps] == ["app-a"]
    item = apps[0]
    assert "token" not in item and "token_sha256" not in item
    assert len(item["token_prefix"]) == 8
    assert all(ch in "0123456789abcdef" for ch in item["token_prefix"])
    assert token not in response.text
    assert store.apps["app-a"]["token_sha256"] not in response.text


def test_admin_scopes_add_remove(client, store, operator):
    _register(client, "app-a")
    url = PREFIX + "/admin/apps/app-a/scopes"
    added = client.post(url, headers=_operator(), json={"add": ["claim", "report"]})
    assert added.status_code == 200, added.json()
    assert added.json()["data"]["scopes"] == ["claim", "read", "report"]
    removed = client.post(url, headers=_operator(), json={"remove": ["report"]})
    assert removed.json()["data"]["scopes"] == ["claim", "read"]
    assert store.apps["app-a"]["scopes"] == ["claim", "read"]
    bad = client.post(url, headers=_operator(), json={"add": ["root"]})
    assert bad.status_code == 400
    extra = client.post(url, headers=_operator(), json={"set": ["read"]})
    assert extra.status_code == 400


def test_admin_other_users_app_is_not_found(client, store, operator):
    store.add_app("app-theirs", user_id="user-2")
    for action in ("rotate", "revoke"):
        response = client.post(f"{PREFIX}/admin/apps/app-theirs/{action}", headers=_operator())
        assert response.status_code == 404, action
    audit = client.get(f"{PREFIX}/admin/apps/app-theirs/audit", headers=_operator())
    assert audit.status_code == 404
    assert store.apps["app-theirs"]["revoked_at"] is None


def test_admin_rotate_revoked_meanwhile_leaves_vault_on_old_token(
    client, store, operator, monkeypatch
):
    """The digest is written first: an app revoked between the ownership read
    and the rotation must not leave the vault holding a token nothing accepts."""
    from src.server.database.collab import apps as app_db

    old = _register(client, "app-a").json()["data"]["token"]

    async def revoked_meanwhile(app_id, token_sha256, *, conn=None):
        return None

    monkeypatch.setattr(app_db, "rotate_token", revoked_meanwhile)
    response = client.post(PREFIX + "/admin/apps/app-a/rotate", headers=_operator())
    assert response.status_code == 404
    assert store.vault[(USER, "app-a")] == old


def test_admin_rotate_vault_failure_rolls_the_digest_back(client, store, operator, monkeypatch):
    from src.server.services.collab import apps as app_svc

    old = _register(client, "app-a").json()["data"]["token"]
    digest = store.apps["app-a"]["token_sha256"]
    order: list[str] = []
    real_rotate = store.rotate_token

    async def rotate(app_id, token_sha256, *, conn=None):
        order.append("db")
        return await real_rotate(app_id, token_sha256, conn=conn)

    async def failing_vault(user_id, app_id, token, *, conn=None):
        order.append("vault")
        assert conn is not None, "the vault write must join the rotation's transaction"
        raise RuntimeError("vault down")

    from src.server.database.collab import apps as app_db

    monkeypatch.setattr(app_db, "rotate_token", rotate)
    monkeypatch.setattr(app_svc, "store_vault_token", failing_vault)
    response = client.post(PREFIX + "/admin/apps/app-a/rotate", headers=_operator())
    assert response.status_code == 500
    assert order == ["db", "vault"]
    assert store.apps["app-a"]["token_sha256"] == digest
    assert store.vault[(USER, "app-a")] == old
    assert _whoami(client, _bearer(old)).status_code == 200


def test_admin_rotate_invalidates_old_token(client, store, operator):
    old = _register(client, "app-a").json()["data"]["token"]
    assert _whoami(client, _bearer(old)).status_code == 200
    response = client.post(PREFIX + "/admin/apps/app-a/rotate", headers=_operator())
    assert response.status_code == 200, response.json()
    new = response.json()["data"]["token"]
    assert new and new != old
    assert store.apps["app-a"]["token_sha256"] == hashlib.sha256(new.encode()).hexdigest()
    assert store.vault[(USER, "app-a")] == new
    stale = _whoami(client, _bearer(old))
    assert stale.status_code == 401
    assert stale.json()["code"] == "unauthorized"
    assert _whoami(client, _bearer(new)).status_code == 200


def test_admin_revoke_releases_leases(client, store, operator):
    token = _register(client, "app-a", scopes=["read", "claim"], can_write=True).json()["data"]["token"]
    ticket = store.add_ticket()
    ref = f"t:{ticket['ticket_id']}"
    claim = client.post(f"{PREFIX}/tasks/{ref}/claim", headers=_bearer(token), json={"revision": ticket["revision"]})
    assert claim.status_code == 200, claim.json()
    lease_id = claim.json()["data"]["lease_id"]

    response = client.post(PREFIX + "/admin/apps/app-a/revoke", headers=_operator())
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["revoked"] is True
    assert data["released_leases"] == 1
    assert "token" not in data
    assert store.leases[lease_id]["state"] == "released"
    assert store.tickets[ticket["ticket_id"]]["status"] == "queued"
    assert _whoami(client, _bearer(token)).status_code == 401
    rotate = client.post(PREFIX + "/admin/apps/app-a/rotate", headers=_operator())
    assert rotate.status_code == 404


def test_admin_audit_lists_rows_without_token(client, store, operator):
    token = _register(client, "app-a").json()["data"]["token"]
    _whoami(client, _bearer(token))
    client.get(PREFIX + "/tasks?limit=abc", headers=_bearer(token))
    response = client.get(PREFIX + "/admin/apps/app-a/audit?limit=50", headers=_operator())
    assert response.status_code == 200, response.json()
    rows = response.json()["data"]["audit"]
    tools_seen = {row["tool"] for row in rows}
    assert {"whoami", "task_list", "apps_register"} <= tools_seen
    codes = {row["tool"]: row["code"] for row in rows}
    assert codes["task_list"] == "invalid_schema"
    assert token not in response.text
    bad = client.get(PREFIX + "/admin/apps/app-a/audit?limit=x", headers=_operator())
    assert bad.status_code == 400


def test_admin_refuses_app_token_as_operator(client, store, operator):
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    response = client.get(PREFIX + "/admin/apps", headers=_bearer(token))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert operator == []
    assert store.audit[-1]["tool"] == "apps_list"
    assert store.audit[-1]["code"] == "unauthorized"
    reg = client.post(PREFIX + "/admin/apps", headers=_bearer(token), json={"app_id": "app-evil"})
    assert reg.status_code == 401
    assert "app-evil" not in store.apps


def test_admin_refuses_app_token_next_to_the_service_token(client, store, operator):
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    response = client.post(
        PREFIX + "/admin/apps", headers={**_operator(), **_bearer(token)}, json={"app_id": "app-evil"}
    )
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert "app-evil" not in store.apps
    assert store.audit[-1]["tool"] == "apps_register"
    assert store.audit[-1]["code"] == "unauthorized"
    assert operator == []


def _assert_refused_and_audited(response, store, tool: str = "apps_list") -> None:
    assert response.status_code == 401
    body = response.json()
    _assert_envelope(body)
    assert body["code"] == "unauthorized"
    assert OPERATOR_TOKEN not in response.text
    assert store.audit[-1]["tool"] == tool
    assert store.audit[-1]["code"] == "unauthorized"
    assert store.audit[-1]["http_status"] == 401


def test_admin_refuses_missing_or_bad_operator_credential(client, store, operator):
    attempts = (
        {},
        {"X-User-Id": USER},
        _bearer(OPERATOR_TOKEN),
        _operator("fake-wrong-operator"),
        {"X-Service-Token": "", "X-User-Id": USER},
    )
    for headers in attempts:
        before = len(store.audit)
        response = client.get(PREFIX + "/admin/apps", headers=headers)
        _assert_refused_and_audited(response, store)
        assert len(store.audit) == before + 1, headers
    reg = client.post(PREFIX + "/admin/apps", json={"app_id": "app-evil"})
    _assert_refused_and_audited(reg, store, "apps_register")
    assert "app-evil" not in store.apps
    assert operator == []


def test_admin_with_no_credential_is_refused_by_the_real_resolver(client, store, monkeypatch):
    """OSS mode: the normal user check alone answers the local user. Not here."""
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", OPERATOR_TOKEN)
    store.add_app("app-a", scopes=ALL_SCOPES)
    for name, (method, path) in ADMIN_ROUTES.items():
        if method == "GET":
            response = client.get(_concrete(path))
        else:
            response = client.post(_concrete(path), json={"app_id": "app-z", "add": ["review"]})
        _assert_refused_and_audited(response, store, name)
    assert "app-z" not in store.apps


def test_admin_refuses_everything_when_the_server_has_no_service_token(client, store, operator, monkeypatch):
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    response = client.get(PREFIX + "/admin/apps", headers=_operator())
    _assert_refused_and_audited(response, store)
    assert "INTERNAL_SERVICE_TOKEN" in response.json()["message"]
    assert operator == []


def test_admin_correct_operator_credential_is_200(client, store, operator):
    store.add_app("app-a", scopes=ALL_SCOPES)
    response = client.get(PREFIX + "/admin/apps", headers=_operator())
    assert response.status_code == 200, response.json()
    assert [row["app_id"] for row in response.json()["data"]["apps"]] == ["app-a"]
    assert OPERATOR_TOKEN not in response.text
    assert operator == [OPERATOR_TOKEN]


def test_tool_routes_still_refuse_the_operator_credential(client, store, operator):
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    response = client.get(PREFIX + "/whoami", headers={**_bearer(token), **_operator()})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_admin_non_loopback_is_403(remote_client, store, operator):
    response = remote_client.get(PREFIX + "/admin/apps", headers=_operator())
    assert response.status_code == 403
    assert operator == []


# ---------------------------------------------------------------------------
# 7. Tokens never in tool responses
# ---------------------------------------------------------------------------


def test_tokens_never_in_tool_responses(client, store, operator):
    tokens = [
        store.add_app("app-a", scopes=ALL_SCOPES),
        store.add_app("app-b", scopes=ALL_SCOPES),
    ]
    registered = _register(client, "app-c", scopes=list(ALL_SCOPES), can_write=True)
    tokens.append(registered.json()["data"]["token"])
    rotated = client.post(PREFIX + "/admin/apps/app-c/rotate", headers=_operator())
    tokens.append(rotated.json()["data"]["token"])
    a, b, c = tokens[0], tokens[1], tokens[3]
    ticket = store.add_ticket()
    ref = f"t:{ticket['ticket_id']}"

    texts: list[str] = []
    statuses: list[tuple[str, int, Any]] = []

    def get(path: str, token: str):
        response = client.get(PREFIX + path, headers=_bearer(token))
        texts.append(response.text)
        statuses.append((path, response.status_code, response.json().get("code")))
        return response

    def post(path: str, token: str, body: dict[str, Any]):
        response = client.post(PREFIX + path, headers=_bearer(token), json=body)
        texts.append(response.text)
        statuses.append((path, response.status_code, response.json().get("code")))
        return response

    get("/whoami", a)
    get("/tasks", a)
    get(f"/tasks/{ref}", a)
    claim = post(f"/tasks/{ref}/claim", a, {"revision": ticket["revision"]})
    lease_id = claim.json()["data"]["lease_id"]
    post(f"/leases/{lease_id}/heartbeat", a, {})
    post("/messages", a, {"to": "app:app-b", "text": "please look at the docs"})
    get("/messages/inbox", b)
    get("/handoffs", b)
    get("/reviews", c)
    post(f"/leases/{lease_id}/report", a, {"report": _report()})
    post("/tasks", c, {"title": "A proposal", "body": "Describe it.", "repo": "/repo"})
    spare = store.add_ticket()
    spare_claim = post(f"/tasks/t:{spare['ticket_id']}/claim", b, {"revision": spare["revision"]})
    spare_lease = spare_claim.json()["data"]["lease_id"]
    post(f"/leases/{spare_lease}/release", b, {"reason": "cannot_do", "note": "not mine"})
    get("/whoami", "fake-unknown-token")
    for path in ("/admin/apps", "/admin/apps/app-a/audit"):
        texts.append(client.get(PREFIX + path, headers=_operator()).text)

    assert len(texts) >= 14
    failed = [item for item in statuses[:-1] if item[1] != 200]
    assert failed == [], failed
    assert statuses[-1][1] == 401
    for text in texts:
        for token in tokens:
            assert token not in text
        for row in store.apps.values():
            assert row["token_sha256"] not in text
    for row in store.audit:
        for token in tokens:
            assert token not in repr(row)


# ---------------------------------------------------------------------------
# 8. setup.py wiring
# ---------------------------------------------------------------------------


SETUP = REPO / "src" / "server" / "app" / "setup.py"


def test_setup_mounts_collab_router_once():
    text = SETUP.read_text(encoding="utf-8")
    mounts = [
        line
        for line in text.splitlines()
        if "src.server.app.collab" in line and not line.lstrip().startswith("#")
    ]
    assert len(mounts) == 1, mounts
    assert "include_router" in mounts[0]


def test_setup_registers_one_collab_sweeper():
    tree = ast.parse(SETUP.read_text(encoding="utf-8"))
    value = None
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value_node = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value_node = node.targets[0].id, node.value
        if target == "_REDIS_BACKGROUND_SINGLETONS":
            value = ast.literal_eval(value_node)
    assert value is not None
    rows = [row for row in value if row[0] == "CollabLeaseSweeper"]
    assert rows == [("CollabLeaseSweeper", "src.server.services.collab.sweep")]
    assert len({row[0] for row in value}) == len(value)


def test_setup_diff_is_two_added_lines():
    """N80 wires into the app factory with exactly two lines: the lease sweeper
    and the /api/v1/collab router. Checked on the file itself rather than as a
    diff against the pre-N80 commit, because later rounds edit setup.py too."""
    text = (REPO / "src" / "server" / "app" / "setup.py").read_text(encoding="utf-8")
    collab_lines = [line.strip() for line in text.splitlines() if "collab" in line.lower()]
    assert collab_lines == [
        '("CollabLeaseSweeper", "src.server.services.collab.sweep"),  # N80 collab_lease_sweep',
        'app.include_router(importlib.import_module("src.server.app.collab").router)  # /api/v1/collab/* - N80',
    ]


# ---------------------------------------------------------------------------
# Request bodies: capped, and read only after loopback and mode (security L1)
# ---------------------------------------------------------------------------


def _oversized_task() -> dict[str, Any]:
    return {"title": "big task", "body": "x" * (collab_routes.MAX_BODY_BYTES + 10)}


def test_body_over_declared_limit_is_too_large(client, store):
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    response = client.post(PREFIX + "/tasks", headers=_bearer(token), json=_oversized_task())
    assert response.status_code == 413
    assert response.json()["code"] == "too_large"
    assert store.audit[-1]["code"] == "too_large"
    assert store.tickets == {} or all(t["title"] != "big task" for t in store.tickets.values())


def test_streamed_body_without_length_is_cut_off(client, store):
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    sent: list[int] = []

    def chunks():
        for _ in range(64):
            sent.append(1)
            yield b"x" * 16384

    response = client.post(
        PREFIX + "/tasks",
        headers={**_bearer(token), "Content-Type": "application/json"},
        content=chunks(),
    )
    assert response.status_code == 413
    assert response.json()["code"] == "too_large"


def test_non_numeric_content_length_is_invalid_schema():
    import asyncio

    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-length", b"12abc")],
    }

    async def receive():  # pragma: no cover - never reached
        raise AssertionError("the body must not be read")

    request = Request(scope, receive)
    from src.collab.types import InvalidSchemaError

    with pytest.raises(InvalidSchemaError):
        asyncio.run(collab_routes._read_capped(request))


@pytest.fixture
def body_reads(monkeypatch) -> list[str]:
    reads: list[str] = []
    real = collab_routes._body

    async def recording(request):
        reads.append(request.url.path)
        return await real(request)

    monkeypatch.setattr(collab_routes, "_body", recording)
    return reads


def test_non_loopback_post_is_refused_before_the_body_is_read(remote_client, store, body_reads):
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    response = remote_client.post(PREFIX + "/tasks", headers=_bearer(token), json=_oversized_task())
    assert response.status_code == 403
    assert response.json()["code"] == "scope_denied"
    assert body_reads == []


def test_mode_off_post_is_refused_before_the_body_is_read(client, store, body_reads, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "off")
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    response = client.post(PREFIX + "/tasks", headers=_bearer(token), json={"title": "abc"})
    assert response.json()["code"] == "mode_denied"
    assert body_reads == []


def test_admin_body_is_read_only_after_the_operator_authenticates(
    client, store, operator, body_reads
):
    refused = client.post(PREFIX + "/admin/apps", json={"app_id": "app-x"})
    assert refused.status_code == 401
    assert body_reads == []
    ok = _register(client, "app-x")
    assert ok.status_code == 200, ok.json()
    assert body_reads == [PREFIX + "/admin/apps"]


def test_admin_body_over_limit_is_too_large(client, store, operator):
    response = client.post(
        PREFIX + "/admin/apps",
        headers=_operator(),
        json={"app_id": "app-x", "name": "x" * (collab_routes.MAX_BODY_BYTES + 1)},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "too_large"
    assert "app-x" not in store.apps


def test_path_parameters_win_over_body_fields(client, store):
    token = store.add_app("app-a", scopes=ALL_SCOPES)
    response = client.post(
        PREFIX + "/leases/lease-from-path/heartbeat",
        headers=_bearer(token),
        json={"lease_id": "lease-from-body"},
    )
    assert response.json()["code"] == "not_found"
    assert store.audit[-1]["target"] == "lease-from-path"


# ---------------------------------------------------------------------------
# Refusal audit rows for unauthenticated callers are metered per peer
# ---------------------------------------------------------------------------


def test_unauthenticated_refusals_cannot_flood_the_audit_table(client, store):
    limit = ratelimit.REFUSAL_AUDIT_POLICY.limit
    for _ in range(limit + 40):
        assert _whoami(client, _bearer("not-a-real-token")).status_code == 401
    unauthorized = [row for row in store.audit if row["code"] == "unauthorized"]
    assert len(unauthorized) == limit
    # An authenticated caller's refusals are still recorded.
    token = store.add_app("app-a", scopes=("read",))
    response = client.post(PREFIX + "/tasks", headers=_bearer(token), json={"title": "abc"})
    assert response.json()["code"] == "scope_denied"
    assert store.audit[-1]["code"] == "scope_denied"
    assert store.audit[-1]["app_id"] == "app-a"


def test_unauthenticated_admin_refusals_are_metered(client, store, operator):
    limit = ratelimit.REFUSAL_AUDIT_POLICY.limit
    for _ in range(limit + 5):
        assert client.get(PREFIX + "/admin/apps").status_code == 401
    assert len([row for row in store.audit if row["tool"] == "apps_list"]) == limit


def test_refusal_audit_bucket_is_per_peer_and_bounded():
    now = [0.0]
    limiter = ratelimit.RateLimiter(clock=lambda: now[0])
    limit = ratelimit.REFUSAL_AUDIT_POLICY.limit
    assert all(limiter.allow_refusal_audit("127.0.0.1") for _ in range(limit))
    assert not limiter.allow_refusal_audit("127.0.0.1")
    assert limiter.allow_refusal_audit("::1")  # another peer has its own bucket
    now[0] += ratelimit.REFUSAL_AUDIT_POLICY.window_seconds
    assert limiter.allow_refusal_audit("127.0.0.1")  # refilled
    # Past MAX_REFUSAL_PEERS, new peers share one overflow bucket.
    for index in range(ratelimit.MAX_REFUSAL_PEERS - 2):  # two peers above
        limiter.allow_refusal_audit(f"10.0.{index // 256}.{index % 256}")
    shared = [limiter.allow_refusal_audit(f"peer-{index}") for index in range(limit + 1)]
    assert shared.count(True) == limit
    # App buckets are untouched by refusal metering.
    assert limiter.allow("app-a", "whoami")


# ---------------------------------------------------------------------------
# BYOK_ENCRYPTION_KEY unset: register/rotate name the setting (503), not 500
# ---------------------------------------------------------------------------


def _assert_key_missing(response) -> None:
    assert response.status_code == 503, response.json()
    body = response.json()
    _assert_envelope(body)
    assert body["ok"] is False
    assert body["code"] == "byok_key_missing"
    assert "BYOK_ENCRYPTION_KEY" in body["message"]
    assert body["data"] == {"missing_setting": "BYOK_ENCRYPTION_KEY"}
    assert "token" not in body["data"]


def test_admin_register_without_byok_key_is_503_naming_the_setting(
    client, store, operator, monkeypatch
):
    monkeypatch.delenv("BYOK_ENCRYPTION_KEY", raising=False)
    _assert_key_missing(_register(client, scopes=["read"]))
    assert store.apps == {}
    assert store.vault == {}
    assert operator == [OPERATOR_TOKEN]  # refused only after the operator check
    assert store.audit[-1]["tool"] == "apps_register"


def test_admin_rotate_without_byok_key_is_503_and_keeps_the_token(
    client, store, operator, monkeypatch
):
    old = _register(client, "app-a").json()["data"]["token"]
    digest = store.apps["app-a"]["token_sha256"]
    monkeypatch.delenv("BYOK_ENCRYPTION_KEY", raising=False)
    _assert_key_missing(client.post(PREFIX + "/admin/apps/app-a/rotate", headers=_operator()))
    assert store.apps["app-a"]["token_sha256"] == digest
    assert store.vault[(USER, "app-a")] == old


def test_admin_without_byok_key_still_authenticates_first(client, remote_client, store, operator, monkeypatch):
    """The configuration gap is reported only to an authenticated operator."""
    monkeypatch.delenv("BYOK_ENCRYPTION_KEY", raising=False)
    anonymous = client.post(PREFIX + "/admin/apps", json={"app_id": "app-new"})
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] != "byok_key_missing"
    wrong = client.post(
        PREFIX + "/admin/apps", headers=_operator("wrong-credential"), json={"app_id": "app-new"}
    )
    assert wrong.status_code == 401
    remote = remote_client.post(PREFIX + "/admin/apps", headers=_operator(), json={"app_id": "app-new"})
    assert remote.status_code == 403
    monkeypatch.delenv("CONDUCTOR_COLLAB_MODE", raising=False)
    off = _register(client)
    assert off.status_code == 403 and off.json()["code"] == "mode_denied"
    assert store.apps == {}


def test_admin_list_and_revoke_do_not_need_byok_key(client, store, operator, monkeypatch):
    _register(client, "app-a")
    monkeypatch.delenv("BYOK_ENCRYPTION_KEY", raising=False)
    assert client.get(PREFIX + "/admin/apps", headers=_operator()).status_code == 200
    assert client.post(PREFIX + "/admin/apps/app-a/revoke", headers=_operator()).status_code == 200
