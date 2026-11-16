"""HTTP contract for per-Ticket worktrees (N17, line A).

The persistence and service helpers are replaced by stateful fakes: one dict
row per Ticket, and the fakes evaluate the same fences the contract gives the
real SQL (live lease, open state, base pin, path + generation + lease). The
route code under test is real.

Each test fails on main: there the claim response has no ``worktree`` /
``worktree_path`` and never calls ``daemon_claim_worktree_spec`` (no 503),
the ``/worktree`` and ``/worktree/cleanup`` daemon routes do not exist (404),
and close never calls ``release_server_ticket_worktree`` (no pending state).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from src.server.database.autonomy import daemons as daemon_db
from src.server.database.autonomy import stops as stop_db
from src.server.database.autonomy import tickets as tickets_db
from src.server.services.autonomy import ownership
from src.server.services.autonomy import ticket_worktree as worktree_service
from src.server.services.autonomy import worker as worker_service
from src.server.app import autonomy as autonomy_app
from tests.conftest import create_test_app

USER_ID = "test-user-123"
DAEMON_ID = "55555555-5555-4555-8555-555555555555"
OTHER_DAEMON_ID = "55555555-5555-4555-8555-000000000000"
TICKET_ID = "66666666-6666-4666-8666-666666666666"
LEASE_1 = "77777777-7777-4777-8777-000000000001"
LEASE_2 = "77777777-7777-4777-8777-000000000002"
BASE_SHA = "abc1234def5678" + "0" * 26
DAEMON_PATH = "/Users/dev/.conductor/worktrees/ticket-66666666"
SERVER_PATH = "/srv/conductor/ticket-worktrees/ticket-66666666"
BRANCH = f"conductor/ticket/{TICKET_ID}"
OPEN = ("queued", "running", "waiting", "blocked", "awaiting_clarification")

BIND_URL = f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/worktree"
CLEANUP_URL = f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/worktree/cleanup"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _worktree_error(name: str) -> type[Exception]:
    """Line S's TicketWorktreeError subclass, or a stand-in until it lands."""
    existing = getattr(worktree_service, name, None)
    if isinstance(existing, type) and issubclass(
        existing, worktree_service.TicketWorktreeError
    ):
        return existing
    return type(name, (worktree_service.TicketWorktreeError,), {})


# Per-Ticket: this Ticket's base ref / repo cannot be resolved.
TicketWorktreeBaseError = _worktree_error("TicketWorktreeBaseError")
# Server-wide: no repo configured or unreadable, git missing, git timeout.
TicketWorktreeUnavailable = _worktree_error("TicketWorktreeUnavailable")


@pytest.fixture(autouse=True)
def _worktree_error_classes(monkeypatch):
    for cls in (TicketWorktreeBaseError, TicketWorktreeUnavailable):
        monkeypatch.setattr(worktree_service, cls.__name__, cls, raising=False)


def _row(**over: Any) -> dict[str, Any]:
    now = _now()
    row: dict[str, Any] = {
        "ticket_id": TICKET_ID,
        "user_id": USER_ID,
        "workspace_id": "ws-1",
        "title": "Ship worktrees",
        "scope": "one checkout per Ticket",
        "status": "running",
        "mandate": {"cwd": "/legacy/repo", "source_id": ""},
        "policy_version": "policy-1",
        "origin_thread_key": "manual:1",
        "worker_provider": "codex",
        "worker_model": "gpt-5",
        "worker_thread_id": None,
        "worker_daemon_id": DAEMON_ID,
        "worker_lease_id": LEASE_1,
        "worker_lease_expires_at": now + timedelta(minutes=5),
        "attempts": 1,
        "attention": None,
        "summary": "",
        "error": "",
        "revision": 3,
        "switches": [],
        "inputs": [],
        "created_at": now,
        "updated_at": now,
        "started_at": now,
        "ended_at": None,
        "closed_at": None,
        "close_reason": "",
        "worktree_path": "",
        "worktree_generation": 0,
        "worktree_cleanup_pending": False,
        "worktree_cleanup_attempts": 0,
        "worktree_cleanup_next_retry_at": None,
        "worktree_branch": "",
        "worktree_base_sha": BASE_SHA,
        "worktree_base_branch": "origin/main",
        "worktree_repo": "",
        "worktree_host": "",
        "worktree_daemon_id": None,
        "worktree_lease_id": None,
    }
    row.update(over)
    return row


class Store:
    """One Ticket row plus its stop request; fakes evaluate fences on it."""

    def __init__(self, row: dict[str, Any], stop: dict[str, Any] | None = None):
        self.row = row
        self.stop = stop
        self.calls: list[str] = []
        self.bind_kwargs: list[dict[str, Any]] = []
        self.fence_kwargs: list[dict[str, Any]] = []
        # worktree_stale_cleanups: bindings of departed daemons a newer bind
        # replaced, each still owed a cleanup report by that daemon.
        self.stale_notes: list[dict[str, Any]] = []

    # -- persistence fakes (contract names) ---------------------------------
    async def get_ticket(self, ticket_id: str, **_: Any) -> dict[str, Any] | None:
        if ticket_id != TICKET_ID:
            return None
        public = {
            k: v
            for k, v in self.row.items()
            if not k.startswith("worker_lease") and k != "inputs"
        }
        return copy.deepcopy(public)

    async def owned_ticket(self, daemon_id: str, ticket_id: str):
        if ticket_id != TICKET_ID or str(self.row["worker_daemon_id"]) != daemon_id:
            return None
        return copy.deepcopy(self.row)

    async def get_stop(self, ticket_id: str):
        return copy.deepcopy(self.stop)

    async def bind_ticket_worktree(
        self,
        ticket_id: str,
        *,
        worktree_path: str,
        branch: str,
        base_sha: str,
        repo: str,
        host: str,
        expected_revision: int | None = None,
        daemon_id: str | None = None,
        lease_id: str | None = None,
        replace_generation: int | None = None,
    ) -> dict[str, Any] | None:
        self.calls.append("bind")
        self.bind_kwargs.append(
            {
                "worktree_path": worktree_path,
                "branch": branch,
                "base_sha": base_sha,
                "repo": repo,
                "host": host,
                "daemon_id": daemon_id,
                "lease_id": lease_id,
                "replace_generation": replace_generation,
            }
        )
        r = self.row
        # Line P: a departed daemon's binding (bound daemon is no longer the
        # worker daemon) is replaced, pending or not, but only when the
        # generation the caller read is still the row's generation.
        replacing = (
            replace_generation is not None
            and r["worktree_host"] == "daemon"
            and bool(r["worktree_path"])
            and str(r["worktree_daemon_id"]) != str(r["worker_daemon_id"])
            and r["worktree_generation"] == replace_generation
        )
        if replacing:
            r = {
                **r,
                "worktree_path": "",
                "worktree_cleanup_pending": False,
                "worktree_host": "",
                "worktree_daemon_id": None,
                "worktree_lease_id": None,
            }
        # A pending cleanup refuses a bind unless it is the same checkout
        # taken over by the same host (and daemon): line P's takeover.
        takeover = (
            r["worktree_cleanup_pending"]
            and r["worktree_path"] == worktree_path
            and r["worktree_host"] == host
            and (host != "daemon" or str(r["worktree_daemon_id"]) == str(daemon_id))
        )
        if (
            ticket_id != TICKET_ID
            or r["status"] not in OPEN
            or r["closed_at"] is not None
            or (r["worktree_cleanup_pending"] and not takeover)
            or r["worktree_base_sha"] != base_sha
            or r["worktree_path"] not in ("", worktree_path)
        ):
            return None
        if host == "daemon":
            expires = r.get("worker_lease_expires_at")
            if (
                str(r["worker_daemon_id"]) != str(daemon_id)
                or str(r.get("worker_lease_id")) != str(lease_id)
                or expires is None
                or expires <= _now()
            ):
                return None
        elif r["worker_daemon_id"] is not None or (
            expected_revision is not None and r["revision"] != expected_revision
        ):
            return None
        if replacing:
            old = self.row
            self.stale_notes.append(
                {
                    "daemon_id": str(old["worktree_daemon_id"]),
                    "worktree_path": old["worktree_path"],
                    "worktree_generation": old["worktree_generation"],
                    "lease_id": old["worktree_lease_id"],
                }
            )
            r = self.row
        elif (
            not takeover
            and r["worktree_path"] == worktree_path
            and r["worktree_host"] == host
            and r["worktree_branch"] == branch
            and str(r["worktree_lease_id"]) == str(lease_id)
        ):
            return copy.deepcopy(r)
        r.update(
            worktree_cleanup_pending=False,
            worktree_cleanup_attempts=0,
            worktree_cleanup_next_retry_at=None,
            worktree_path=worktree_path,
            worktree_branch=branch,
            worktree_repo=repo,
            worktree_host=host,
            worktree_daemon_id=daemon_id,
            worktree_lease_id=lease_id,
            worktree_generation=r["worktree_generation"] + 1,
            revision=r["revision"] + 1,
        )
        return copy.deepcopy(r)

    def _fence(
        self, ticket_id, worktree_path, worktree_generation, lease_id, host, daemon_id
    ) -> bool:
        r = self.row
        return (
            ticket_id == TICKET_ID
            and r["worktree_path"] == worktree_path
            and r["worktree_generation"] == worktree_generation
            and (lease_id is None or str(r["worktree_lease_id"]) == str(lease_id))
            and (host is None or r["worktree_host"] == host)
            and (daemon_id is None or str(r["worktree_daemon_id"]) == str(daemon_id))
        )

    async def clear_ticket_worktree_binding(
        self,
        ticket_id,
        *,
        worktree_path,
        worktree_generation,
        lease_id=None,
        host=None,
        daemon_id=None,
    ) -> bool:
        self.calls.append("clear")
        self.fence_kwargs.append({"host": host, "daemon_id": daemon_id})
        if not self._fence(
            ticket_id, worktree_path, worktree_generation, lease_id, host, daemon_id
        ):
            return False
        self.row.update(
            worktree_path="",
            worktree_branch="",
            worktree_repo="",
            worktree_host="",
            worktree_daemon_id=None,
            worktree_lease_id=None,
            worktree_cleanup_pending=False,
            worktree_cleanup_attempts=0,
            worktree_cleanup_next_retry_at=None,
            revision=self.row["revision"] + 1,
        )
        return True

    async def record_worktree_cleanup_pending(
        self,
        ticket_id,
        *,
        worktree_path,
        worktree_generation,
        lease_id=None,
        delay_seconds=0.0,
        host=None,
        daemon_id=None,
    ) -> bool:
        self.calls.append("pending")
        self.fence_kwargs.append({"host": host, "daemon_id": daemon_id})
        if not self._fence(
            ticket_id, worktree_path, worktree_generation, lease_id, host, daemon_id
        ):
            return False
        self.row.update(
            worktree_cleanup_pending=True,
            worktree_cleanup_attempts=self.row["worktree_cleanup_attempts"] + 1,
            worktree_cleanup_next_retry_at=_now() + timedelta(seconds=delay_seconds),
        )
        return True

    async def resolve_stale_worktree_cleanup(
        self, ticket_id, *, daemon_id, worktree_path, worktree_generation, outcome
    ) -> bool:
        self.calls.append("resolve_stale")
        for note in self.stale_notes:
            if (
                ticket_id == TICKET_ID
                and note["daemon_id"] == str(daemon_id)
                and note["worktree_path"] == worktree_path
                and note["worktree_generation"] == worktree_generation
            ):
                if outcome == "removed":
                    self.stale_notes.remove(note)
                return True
        return False

    async def update_ticket(self, ticket_id, *, expected_status=None, **fields):
        if expected_status is not None and self.row["status"] != expected_status:
            return None
        self.row.update(fields)
        return await self.get_ticket(ticket_id)


@pytest.fixture
def install(monkeypatch):
    """Install a Store's fakes in place of the persistence helpers."""

    def _install(store: Store) -> Store:
        async def daemon_auth(token):
            return {"daemon_id": DAEMON_ID, "user_id": USER_ID}

        async def feature_on(user_id, name):
            return True

        monkeypatch.setattr("src.server.app.autonomy._require_daemon", daemon_auth)
        monkeypatch.setattr(
            "src.server.app.autonomy.features.user_feature_enabled", feature_on
        )
        for name in (
            "get_ticket",
            "bind_ticket_worktree",
            "clear_ticket_worktree_binding",
            "record_worktree_cleanup_pending",
            "resolve_stale_worktree_cleanup",
            "update_ticket",
        ):
            monkeypatch.setattr(tickets_db, name, getattr(store, name), raising=False)
        monkeypatch.setattr(daemon_db, "owned_ticket", store.owned_ticket)
        monkeypatch.setattr(stop_db, "get_stop", store.get_stop)
        return store

    return _install


@pytest_asyncio.fixture
async def client():
    from src.server.app.autonomy import router

    app = create_test_app(router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# --------------------------------------------------------------------- claim


@contextlib.contextmanager
def _claim_path(monkeypatch, ticket: dict[str, Any], spec):
    """Real claim route; the claim, prompt and grant collaborators are stubs."""

    async def claim(daemon_id, claim_id, policy_version=""):
        return copy.deepcopy(ticket)

    async def empty(*_a, **_k):
        return ""

    async def deliver(ticket, prompt):
        return prompt

    async def no_source(source_id):
        return None

    async def no_grant(**_k):
        return None

    spec_calls: list[dict[str, Any]] = []

    async def claim_spec(row):
        spec_calls.append(dict(row))
        if isinstance(spec, Exception):
            raise spec
        return copy.deepcopy(spec)

    monkeypatch.setattr(worker_service, "claim_daemon_ticket", claim)
    monkeypatch.setattr(worker_service, "worker_memory_context", empty)
    monkeypatch.setattr(worker_service, "worker_issue_context", empty)
    monkeypatch.setattr(worker_service, "worker_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(worker_service, "deliver_staged_handoff", deliver)
    monkeypatch.setattr(
        "src.server.app.autonomy.source_db.get_source_any_user", no_source
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.shared_context.worker_shared_context_text",
        empty,
    )
    monkeypatch.setattr(
        "src.server.services.autonomy.daemon_grants.attach_lease_grant", no_grant
    )
    monkeypatch.setattr(
        worktree_service, "daemon_claim_worktree_spec", claim_spec, raising=False
    )
    yield spec_calls


def _claimed(**over: Any) -> dict[str, Any]:
    row = _row(**over)
    row.update(
        lease_id=LEASE_1,
        lease_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
        lease_duration_seconds=90,
    )
    return row


def _spec(path: str = "") -> dict[str, Any]:
    return {
        "ticket_id": TICKET_ID,
        "branch": BRANCH,
        "base_sha": BASE_SHA,
        "base_branch": "origin/main",
        "worktree_path": path,
        "worktree_generation": 1 if path else 0,
    }


@pytest.mark.asyncio
async def test_claim_keeps_lease_fields_and_returns_worktree_spec(
    client, install, monkeypatch
):
    install(Store(_row()))
    ticket = _claimed(
        worktree_path=DAEMON_PATH,
        worktree_generation=1,
        worktree_host="daemon",
        worktree_daemon_id=DAEMON_ID,
    )
    with _claim_path(monkeypatch, ticket, _spec(DAEMON_PATH)) as spec_calls:
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})

    assert response.status_code == 200, response.text
    body = response.json()["ticket"]
    assert body["lease_id"] == LEASE_1
    assert body["lease_expires_at"] == ticket["lease_expires_at"].isoformat()
    assert body["lease_duration_seconds"] == 90
    # A dirty blocked Ticket resumes its own persisted host checkout.
    assert body["worktree_path"] == DAEMON_PATH
    assert body["worktree"] == _spec(DAEMON_PATH)
    # With a spec the legacy cwd is not sent: the daemon uses its checkout.
    assert body["cwd"] is None
    assert spec_calls and spec_calls[0]["ticket_id"] == TICKET_ID


@pytest.mark.asyncio
async def test_claim_without_spec_keeps_legacy_cwd(client, install, monkeypatch):
    install(Store(_row()))
    with _claim_path(monkeypatch, _claimed(), None):
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})

    assert response.status_code == 200, response.text
    body = response.json()["ticket"]
    assert body["worktree"] is None
    assert body["cwd"] == "/legacy/repo"
    assert body["lease_id"] == LEASE_1


@pytest.mark.asyncio
async def test_claim_never_sends_a_server_or_foreign_checkout_path(
    client, install, monkeypatch
):
    install(Store(_row()))
    server_bound = _claimed(
        worktree_path=SERVER_PATH, worktree_generation=1, worktree_host="server"
    )
    with _claim_path(monkeypatch, server_bound, _spec(SERVER_PATH)):
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})
    body = response.json()["ticket"]
    assert body["worktree_path"] == ""
    assert body["worktree"]["worktree_path"] == ""

    foreign = _claimed(
        worktree_path=DAEMON_PATH,
        worktree_generation=1,
        worktree_host="daemon",
        worktree_daemon_id=OTHER_DAEMON_ID,
    )
    with _claim_path(monkeypatch, foreign, _spec(DAEMON_PATH)):
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})
    assert response.json()["ticket"]["worktree_path"] == ""


@pytest.mark.asyncio
async def test_claim_spec_failure_is_503_without_claim_body(
    client, install, monkeypatch
):
    install(Store(_row()))
    failure = worktree_service.TicketWorktreeError("base ref origin/main not found")
    with _claim_path(monkeypatch, _claimed(), failure):
        monkeypatch.setattr(daemon_db, "release_daemon_claim", _no_release, raising=False)
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})

    assert response.status_code == 503
    assert response.headers.get("Retry-After")
    payload = response.json()
    assert "ticket" not in payload and "prompt" not in payload


async def _no_release(*_a: Any) -> bool:
    return False


# ---------------------------------------------------------------------- bind


def _bind_body(lease_id: str = LEASE_1, path: str = DAEMON_PATH) -> dict[str, Any]:
    return {
        "lease_id": lease_id,
        "worktree_path": path,
        "branch": BRANCH,
        "base_sha": BASE_SHA,
    }


@pytest.mark.asyncio
async def test_bind_live_lease_persists_daemon_binding(client, install):
    store = install(Store(_row()))
    response = await client.post(BIND_URL, json=_bind_body())

    assert response.status_code == 200, response.text
    assert response.json() == {"worktree_path": DAEMON_PATH, "worktree_generation": 1}
    assert store.row["worktree_host"] == "daemon"
    assert store.row["worktree_daemon_id"] == DAEMON_ID
    assert store.row["worktree_lease_id"] == LEASE_1
    assert store.row["worktree_repo"] == ""
    # Replaying the same bind is idempotent: no new generation.
    again = await client.post(BIND_URL, json=_bind_body())
    assert again.json()["worktree_generation"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "row_over,lease",
    [
        ({"worker_lease_expires_at": _now() - timedelta(seconds=1)}, LEASE_1),
        ({}, LEASE_2),
        ({"worker_daemon_id": OTHER_DAEMON_ID}, LEASE_1),
        ({"status": "completed"}, LEASE_1),
        ({"worktree_base_sha": "f" * 40}, LEASE_1),
    ],
    ids=["expired-lease", "foreign-lease", "reassigned", "closed", "base-mismatch"],
)
async def test_bind_fenced_out_is_409_and_writes_nothing(
    client, install, row_over, lease
):
    store = install(Store(_row(**row_over)))
    before = copy.deepcopy(store.row)
    response = await client.post(BIND_URL, json=_bind_body(lease_id=lease))

    assert response.status_code == 409
    assert store.calls == ["bind"]
    assert store.row == before


@pytest.mark.asyncio
async def test_bind_does_not_replace_another_live_checkout(client, install):
    store = install(
        Store(
            _row(
                worktree_path=DAEMON_PATH,
                worktree_generation=2,
                worktree_host="daemon",
                worktree_daemon_id=DAEMON_ID,
                worktree_lease_id=LEASE_1,
            )
        )
    )
    response = await client.post(BIND_URL, json=_bind_body(path=DAEMON_PATH + "-x"))
    assert response.status_code == 409
    assert store.row["worktree_path"] == DAEMON_PATH
    assert store.row["worktree_generation"] == 2


# ------------------------------------------------------------------- cleanup


def _bound(generation: int = 1, lease: str = LEASE_1, **over: Any) -> dict[str, Any]:
    fields = {
        "status": "completed",
        "worker_lease_id": None,
        "worker_lease_expires_at": None,
        "worktree_path": DAEMON_PATH,
        "worktree_generation": generation,
        "worktree_branch": BRANCH,
        "worktree_host": "daemon",
        "worktree_daemon_id": DAEMON_ID,
        "worktree_lease_id": lease,
    }
    fields.update(over)
    return _row(**fields)


def _cleanup_body(
    outcome: str, generation: int = 1, lease: str | None = LEASE_1
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "worktree_path": DAEMON_PATH,
        "worktree_generation": generation,
        "outcome": outcome,
        "detail": "rm failed" if outcome == "failed" else "",
    }
    if lease is not None:
        body["lease_id"] = lease
    return body


@pytest.mark.asyncio
async def test_cleanup_removed_clears_matching_generation(client, install):
    store = install(Store(_bound()))
    response = await client.post(CLEANUP_URL, json=_cleanup_body("removed"))

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "pending": False}
    assert store.row["worktree_path"] == ""
    assert store.row["worktree_host"] == ""
    # The base pin and generation survive cleanup.
    assert store.row["worktree_base_sha"] == BASE_SHA
    assert store.row["worktree_generation"] == 1


@pytest.mark.asyncio
async def test_cleanup_failed_records_durable_pending(client, install):
    store = install(Store(_bound()))
    response = await client.post(CLEANUP_URL, json=_cleanup_body("failed"))

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "pending": True}
    assert store.row["worktree_cleanup_pending"] is True
    assert store.row["worktree_cleanup_attempts"] == 1
    assert store.row["worktree_path"] == DAEMON_PATH


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["removed", "failed"])
async def test_cleanup_for_stale_generation_is_409_and_keeps_newer_binding(
    client, install, outcome
):
    # Attempt 2 (lease 2) is bound and finished; a late report for attempt 1
    # arrives (for example after an old stop acknowledgement is replayed).
    store = install(Store(_bound(generation=2, lease=LEASE_2)))
    before = copy.deepcopy(store.row)
    response = await client.post(
        CLEANUP_URL, json=_cleanup_body(outcome, generation=1, lease=LEASE_1)
    )

    assert response.status_code == 409
    assert store.row == before


@pytest.mark.asyncio
async def test_cleanup_with_old_lease_and_current_generation_is_409(client, install):
    store = install(Store(_bound(generation=2, lease=LEASE_2)))
    before = copy.deepcopy(store.row)
    response = await client.post(
        CLEANUP_URL, json=_cleanup_body("removed", generation=2, lease=LEASE_1)
    )
    assert response.status_code == 409
    assert store.row == before


@pytest.mark.asyncio
async def test_cleanup_refused_while_lease_still_running(client, install):
    store = install(
        Store(
            _bound(
                status="running",
                worker_lease_id=LEASE_1,
                worker_lease_expires_at=_now() + timedelta(minutes=5),
            )
        )
    )
    before = copy.deepcopy(store.row)
    for outcome in ("removed", "failed"):
        response = await client.post(CLEANUP_URL, json=_cleanup_body(outcome))
        assert response.status_code == 409
    assert store.row == before
    assert "clear" not in store.calls and "pending" not in store.calls


@pytest.mark.asyncio
async def test_cleanup_refused_for_cancel_with_unverified_stop(client, install):
    stop = {"lease_id": LEASE_1, "verification": "unverified", "verified": False}
    store = install(
        Store(
            _bound(
                status="cancelled",
                worker_lease_id=LEASE_1,
                worker_lease_expires_at=_now() + timedelta(minutes=5),
            ),
            stop=stop,
        )
    )
    before = copy.deepcopy(store.row)
    response = await client.post(CLEANUP_URL, json=_cleanup_body("removed"))

    assert response.status_code == 409
    assert store.row == before


@pytest.mark.asyncio
async def test_cleanup_after_verified_stop_for_that_lease_is_accepted(client, install):
    stop = {"lease_id": LEASE_1, "verification": "verified", "verified": True}
    store = install(Store(_bound(status="cancelled"), stop=stop))
    response = await client.post(CLEANUP_URL, json=_cleanup_body("removed"))

    assert response.status_code == 200, response.text
    assert store.row["worktree_path"] == ""


@pytest.mark.asyncio
async def test_cleanup_with_verified_stop_of_older_lease_is_refused(client, install):
    stop = {"lease_id": LEASE_1, "verification": "verified", "verified": True}
    store = install(Store(_bound(generation=2, lease=LEASE_2, status="cancelled"), stop=stop))
    before = copy.deepcopy(store.row)
    response = await client.post(
        CLEANUP_URL, json=_cleanup_body("removed", generation=2, lease=LEASE_2)
    )
    assert response.status_code == 409
    assert store.row == before


@pytest.mark.asyncio
async def test_cleanup_of_server_or_foreign_binding_is_refused(client, install):
    for over in ({"worktree_host": "server"}, {"worktree_daemon_id": OTHER_DAEMON_ID}):
        store = install(Store(_bound(**over)))
        before = copy.deepcopy(store.row)
        response = await client.post(CLEANUP_URL, json=_cleanup_body("removed"))
        assert response.status_code == 409
        assert store.row == before


@pytest.mark.asyncio
async def test_replayed_old_stop_ack_never_touches_the_binding(
    client, install, monkeypatch
):
    """A replayed stop acknowledgement for attempt 1 leaves attempt 2 bound."""
    store = install(
        Store(
            _bound(generation=2, lease=LEASE_2, status="running",
                   worker_lease_id=LEASE_2,
                   worker_lease_expires_at=_now() + timedelta(minutes=5))
        )
    )
    before = copy.deepcopy(store.row)

    async def ack(**kwargs):
        assert kwargs["lease_id"] == LEASE_1
        return {"outcome": "ok", "stop": {"lease_id": LEASE_1, "verified": True}}

    async def release(*_a, **_k):
        raise AssertionError("a stop ack must not release a checkout")

    monkeypatch.setattr("src.server.services.autonomy.stop.acknowledge_ticket_stop", ack)
    monkeypatch.setattr(
        worktree_service, "release_server_ticket_worktree", release, raising=False
    )
    response = await client.post(
        f"/api/v1/autonomy/daemon/tickets/{TICKET_ID}/stop/ack",
        json={"lease_id": LEASE_1, "process_gone": True},
    )
    assert response.status_code == 200, response.text
    # The daemon follows up with a cleanup for attempt 1: refused.
    cleanup = await client.post(CLEANUP_URL, json=_cleanup_body("removed"))
    assert cleanup.status_code == 409
    assert store.row == before


# --------------------------------------------------------------------- close


def _server_bound(**over: Any) -> dict[str, Any]:
    fields = {
        "status": "failed",
        "worker_daemon_id": None,
        "worker_lease_id": None,
        "worker_lease_expires_at": None,
        "worktree_path": SERVER_PATH,
        "worktree_generation": 1,
        "worktree_branch": BRANCH,
        "worktree_host": "server",
    }
    fields.update(over)
    return _row(**fields)


@pytest.mark.asyncio
async def test_close_with_failed_removal_closes_and_leaves_pending(
    client, install, monkeypatch
):
    store = install(Store(_server_bound()))
    seen: list[tuple[dict[str, Any], str]] = []

    async def release(ticket, *, reason):
        # The service's failed-removal path: durable pending on the same
        # path and generation the caller saw.
        seen.append((dict(ticket), reason))
        await store.record_worktree_cleanup_pending(
            str(ticket["ticket_id"]),
            worktree_path=ticket["worktree_path"],
            worktree_generation=ticket["worktree_generation"],
            delay_seconds=30.0,
        )
        return "pending"

    monkeypatch.setattr(
        worktree_service, "release_server_ticket_worktree", release, raising=False
    )
    response = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/close", json={})

    assert response.status_code == 200, response.text
    assert store.row["status"] == "cancelled"
    assert store.row["closed_at"] is not None
    assert store.row["worktree_cleanup_pending"] is True
    assert seen and seen[0][1] == "close"
    # The release ran against the committed close, not the pre-close row.
    assert seen[0][0]["closed_at"] is not None


@pytest.mark.asyncio
async def test_close_when_release_errors_still_closes_and_records_pending(
    client, install, monkeypatch
):
    store = install(Store(_server_bound()))

    async def release(ticket, *, reason):
        raise TimeoutError("ticket worktree lock not acquired")

    monkeypatch.setattr(
        worktree_service, "release_server_ticket_worktree", release, raising=False
    )
    response = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/close", json={})

    assert response.status_code == 200, response.text
    assert store.row["closed_at"] is not None
    assert store.row["worktree_cleanup_pending"] is True
    assert store.row["worktree_path"] == SERVER_PATH


@pytest.mark.asyncio
async def test_repeated_close_retries_release(client, install, monkeypatch):
    store = install(Store(_server_bound(status="cancelled", closed_at=_now())))
    reasons: list[str] = []

    async def release(ticket, *, reason):
        reasons.append(reason)
        await store.clear_ticket_worktree_binding(
            str(ticket["ticket_id"]),
            worktree_path=ticket["worktree_path"],
            worktree_generation=ticket["worktree_generation"],
        )
        return "removed"

    monkeypatch.setattr(
        worktree_service, "release_server_ticket_worktree", release, raising=False
    )
    response = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/close", json={})

    assert response.status_code == 200, response.text
    assert reasons == ["close"]
    assert store.row["worktree_path"] == ""



# ------------------------------------------------------- review round 2 (N17)

TICKET_2 = "66666666-6666-4666-8666-000000000002"


class Queue:
    """Two queued daemon Tickets behind the real claim route and report path.

    ``claim`` first requeues every still-running Ticket, the way
    ``recover_expired_daemon_leases`` does once a held lease runs out, then
    takes the oldest queued Ticket. A spec failure that leaves the lease held
    therefore puts the same Ticket back at the head of the queue.
    """

    def __init__(self) -> None:
        self.rows = {
            tid: _row(
                ticket_id=tid,
                status="queued",
                worker_lease_id=None,
                worker_lease_expires_at=None,
            )
            for tid in (TICKET_ID, TICKET_2)
        }
        self.claimed: list[str] = []
        self.leases = 0
        self.commits = 0
        self.released: list[tuple[str, str, str]] = []
        self.revoked: list[tuple[str, str]] = []

    async def claim(self, daemon_id, claim_id, policy_version=""):
        for row in self.rows.values():
            if row["status"] == "running":
                row.update(status="queued", worker_lease_id=None)
        for tid, row in self.rows.items():
            if row["status"] == "queued":
                self.leases += 1
                lease = f"77777777-7777-4777-8777-{self.leases:012d}"
                row.update(status="running", worker_lease_id=lease)
                self.claimed.append(tid)
                out = copy.deepcopy(row)
                out.update(
                    lease_id=lease,
                    lease_expires_at=datetime(2099, 1, 1, tzinfo=timezone.utc),
                    lease_duration_seconds=90,
                )
                return out
        return None

    async def report_context(self, daemon_id, ticket_id, lease_id):
        row = self.rows.get(ticket_id)
        if (
            row is None
            or row["status"] != "running"
            or row["worker_lease_id"] != lease_id
            or str(row["worker_daemon_id"]) != daemon_id
        ):
            return None
        return copy.deepcopy(row)

    async def commit(self, daemon_id, ticket_id, lease_id, *, transition, summary, **_):
        row = self.rows[ticket_id]
        if row["status"] != "running" or row["worker_lease_id"] != lease_id:
            return None
        self.commits += 1
        row.update(
            status=transition["status"],
            summary=summary,
            attention=transition["attention"],
            worker_lease_id=None,
        )
        return {"ticket_id": ticket_id, "status": row["status"]}

    async def release(self, daemon_id, ticket_id, lease_id):
        """Requeue a running Ticket fenced on this daemon's live lease."""
        self.released.append((daemon_id, ticket_id, lease_id))
        row = self.rows.get(ticket_id)
        if (
            row is None
            or row["status"] != "running"
            or row["worker_lease_id"] != lease_id
            or str(row["worker_daemon_id"]) != daemon_id
        ):
            return False
        row.update(status="queued", worker_lease_id=None)
        return True

    async def revoke(self, user_id, lease_id):
        self.revoked.append((user_id, lease_id))
        return "revoked"

    def wire(self, monkeypatch, *, release: bool = True) -> None:
        monkeypatch.setattr(worker_service, "claim_daemon_ticket", self.claim)
        monkeypatch.setattr(daemon_db, "daemon_report_context", self.report_context)
        monkeypatch.setattr(daemon_db, "commit_daemon_report", self.commit)
        monkeypatch.setattr(daemon_db, "revoke_lease", self.revoke)
        if release:
            monkeypatch.setattr(
                daemon_db, "release_daemon_claim", self.release, raising=False
            )
        else:
            monkeypatch.delattr(daemon_db, "release_daemon_claim", raising=False)


# git's text: a server path, a remote URL with an embedded credential.
GIT_TEXT = (
    "fatal: could not read from 'https://bot:s3cret@git.example/repo.git' "
    "at /srv/conductor/repo"
)


@pytest.mark.asyncio
async def test_claim_spec_failure_fails_ticket_and_does_not_starve_queue(
    client, install, monkeypatch
):
    install(Store(_row()))
    queue = Queue()
    failure = TicketWorktreeBaseError(GIT_TEXT)
    with _claim_path(monkeypatch, _claimed(), failure):
        queue.wire(monkeypatch)
        first = await client.post("/api/v1/autonomy/daemon/claim", json={})
        second = await client.post("/api/v1/autonomy/daemon/claim", json={})

    for response in (first, second):
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "1"
        assert "ticket" not in response.json()
    # The second claim reached the next Ticket: the first one was settled.
    assert queue.claimed == [TICKET_ID, TICKET_2]
    failed = queue.rows[TICKET_ID]
    assert failed["status"] == "failed"
    # A fixed message reaches the Source and the daemon, never git's text.
    assert failed["summary"] == autonomy_app._UNRESOLVABLE_BASE_SUMMARY
    for leaked in ("/srv/", "s3cret", "git.example", "fatal"):
        assert leaked not in failed["summary"]
        assert leaked not in str(failed["attention"])
        assert leaked not in first.text
    # No lease stays held on either Ticket.
    assert all(row["worker_lease_id"] is None for row in queue.rows.values())


@pytest.mark.asyncio
async def test_bind_sends_daemon_fences_and_no_base_branch(client, install):
    store = install(Store(_row()))
    response = await client.post(BIND_URL, json=_bind_body())

    assert response.status_code == 200, response.text
    assert store.bind_kwargs == [
        {
            "worktree_path": DAEMON_PATH,
            "branch": BRANCH,
            "base_sha": BASE_SHA,
            "repo": "",
            "host": "daemon",
            "daemon_id": DAEMON_ID,
            "lease_id": LEASE_1,
            "replace_generation": None,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("worktree_path", "   "),
        ("worktree_path", "relative/path"),
        ("branch", "   "),
        ("branch", "main"),
        ("base_sha", "abc1234"),
    ],
)
async def test_bind_with_invalid_field_is_422_and_never_binds(
    client, install, field, value
):
    store = install(Store(_row()))
    body = _bind_body()
    body[field] = value
    response = await client.post(BIND_URL, json=body)

    assert response.status_code == 422, (response.status_code, response.text)
    assert store.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["removed", "failed"])
async def test_cleanup_without_lease_refused_while_daemon_lease_is_live(
    client, install, outcome
):
    # An ending status while this daemon's lease is still live: omitting
    # lease_id must not be weaker than naming that lease.
    live = dict(
        status="failed",
        worker_lease_id=LEASE_1,
        worker_lease_expires_at=_now() + timedelta(minutes=5),
    )
    for lease in (LEASE_1, None):
        store = install(Store(_bound(**live)))
        before = copy.deepcopy(store.row)
        response = await client.post(
            CLEANUP_URL, json=_cleanup_body(outcome, lease=lease)
        )
        assert response.status_code == 409, (lease, response.text)
        assert store.row == before
        assert store.calls == []


@pytest.mark.asyncio
async def test_cleanup_without_lease_after_verified_stop_of_live_lease_is_accepted(
    client, install
):
    stop = {"lease_id": LEASE_1, "verification": "verified", "verified": True}
    store = install(
        Store(
            _bound(
                status="cancelled",
                worker_lease_id=LEASE_1,
                worker_lease_expires_at=_now() + timedelta(minutes=5),
            ),
            stop=stop,
        )
    )
    response = await client.post(CLEANUP_URL, json=_cleanup_body("removed", lease=None))
    assert response.status_code == 200, response.text
    assert store.row["worktree_path"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["removed", "failed"])
async def test_cleanup_writes_are_fenced_on_daemon_host_and_id(
    client, install, outcome
):
    store = install(Store(_bound()))
    response = await client.post(CLEANUP_URL, json=_cleanup_body(outcome))

    assert response.status_code == 200, response.text
    assert store.fence_kwargs == [{"host": "daemon", "daemon_id": DAEMON_ID}]


@pytest.mark.asyncio
async def test_close_fallback_pending_is_fenced_on_server_host(
    client, install, monkeypatch
):
    store = install(Store(_server_bound()))

    async def release(ticket, *, reason):
        raise TimeoutError("ticket worktree lock not acquired")

    monkeypatch.setattr(
        worktree_service, "release_server_ticket_worktree", release, raising=False
    )
    response = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/close", json={})

    assert response.status_code == 200, response.text
    assert store.fence_kwargs == [{"host": "server", "daemon_id": None}]
    assert store.row["worktree_cleanup_pending"] is True


@pytest.mark.asyncio
async def test_retry_of_ticket_with_pending_cleanup_does_not_wedge(
    client, install, monkeypatch
):
    """Retry a failed daemon Ticket whose checkout removal failed.

    The pending cleanup must not refuse the next attempt's bind of the same
    checkout (it is taken over), and the drain's late report for the old
    attempt must neither clear nor re-mark the new binding.
    """
    store = install(Store(_bound(status="failed")))
    failed = await client.post(CLEANUP_URL, json=_cleanup_body("failed"))
    assert failed.json() == {"ok": True, "pending": True}

    async def not_revoked(_row):
        return None

    async def enqueue(**_k):
        return None

    monkeypatch.setattr(ownership, "revoked_mandate_reason", not_revoked)
    monkeypatch.setattr(
        "src.server.database.autonomy.effects.enqueue_effect", enqueue
    )
    retried = await client.post(f"/api/v1/autonomy/tickets/{TICKET_ID}/retry")
    assert retried.status_code == 200, retried.text
    assert store.row["status"] == "queued"

    # The same daemon claims the next attempt and binds the same checkout.
    store.row.update(
        status="running",
        worker_lease_id=LEASE_2,
        worker_lease_expires_at=_now() + timedelta(minutes=5),
    )
    bind = await client.post(BIND_URL, json=_bind_body(lease_id=LEASE_2))
    assert bind.status_code == 200, bind.text
    assert bind.json() == {"worktree_path": DAEMON_PATH, "worktree_generation": 2}
    assert store.row["worktree_cleanup_pending"] is False
    assert store.row["worktree_cleanup_attempts"] == 0

    # The drain retries attempt 1's cleanup: refused, attempt 2 stays bound.
    before = copy.deepcopy(store.row)
    for outcome in ("removed", "failed"):
        late = await client.post(
            CLEANUP_URL, json=_cleanup_body(outcome, generation=1, lease=LEASE_1)
        )
        assert late.status_code == 409
    assert store.row == before


# ------------------------------------------- B6: which spec failure fails a Ticket


@pytest.mark.asyncio
async def test_claim_server_wide_unavailable_releases_lease_and_keeps_queued(
    client, install, monkeypatch
):
    """A server-wide outage is not the Ticket's fault: nothing is failed."""
    install(Store(_row()))
    queue = Queue()
    failure = TicketWorktreeUnavailable(GIT_TEXT)
    with _claim_path(monkeypatch, _claimed(), failure):
        queue.wire(monkeypatch)
        responses = [
            await client.post("/api/v1/autonomy/daemon/claim", json={})
            for _ in range(3)
        ]

    for response in responses:
        assert response.status_code == 503
        assert response.headers.get("Retry-After") == "30"
        assert "ticket" not in response.json()
        assert "s3cret" not in response.text and "/srv/" not in response.text
    assert queue.commits == 0
    assert all(row["status"] == "queued" for row in queue.rows.values())
    assert all(row["worker_lease_id"] is None for row in queue.rows.values())
    assert all(row["summary"] == "" for row in queue.rows.values())
    assert [r[0] for r in queue.released] == [DAEMON_ID] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        worktree_service.TicketWorktreeError("unclassified"),
        RuntimeError("bug in the spec builder"),
    ],
    ids=["plain-worktree-error", "other-exception"],
)
async def test_claim_other_spec_failure_releases_lease_without_failing(
    client, install, monkeypatch, failure
):
    install(Store(_row()))
    queue = Queue()
    with _claim_path(monkeypatch, _claimed(), failure):
        queue.wire(monkeypatch)
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})

    assert response.status_code == 503
    assert response.headers.get("Retry-After")
    assert "ticket" not in response.json()
    assert queue.commits == 0
    head = queue.rows[TICKET_ID]
    assert head["status"] == "queued" and head["worker_lease_id"] is None


@pytest.mark.asyncio
async def test_claim_unavailable_without_release_helper_revokes_the_lease(
    client, install, monkeypatch
):
    """Without a release helper the lease is revoked, never left signed and held."""
    install(Store(_row()))
    queue = Queue()
    with _claim_path(monkeypatch, _claimed(), TicketWorktreeUnavailable("git hung")):
        queue.wire(monkeypatch, release=False)
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})

    assert response.status_code == 503
    lease = queue.rows[TICKET_ID]["worker_lease_id"]
    assert queue.revoked == [(USER_ID, lease)]
    assert queue.commits == 0
    assert queue.rows[TICKET_ID]["status"] == "running"  # recovery requeues it


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_cls", [TicketWorktreeUnavailable, TicketWorktreeBaseError]
)
async def test_claim_failure_after_lease_moved_touches_nothing(
    client, install, monkeypatch, failure_cls
):
    """The lease expired and another daemon reclaimed the Ticket meanwhile."""
    install(Store(_row()))
    queue = Queue()

    async def spec(row):
        moved = queue.rows[row["ticket_id"]]
        moved.update(worker_daemon_id=OTHER_DAEMON_ID, worker_lease_id="other-lease")
        raise failure_cls("boom")

    with _claim_path(monkeypatch, _claimed(), None):
        queue.wire(monkeypatch)
        monkeypatch.setattr(worktree_service, "daemon_claim_worktree_spec", spec)
        response = await client.post("/api/v1/autonomy/daemon/claim", json={})

    assert response.status_code == 503
    row = queue.rows[TICKET_ID]
    assert row["status"] == "running" and row["worker_lease_id"] == "other-lease"
    assert queue.commits == 0


# -------------------------------- handover away from a departed daemon (blocker A)

B_PATH = "/Users/b/.conductor/worktrees/ticket-66666666"


def _after_reassign() -> dict[str, Any]:
    # Bound by DAEMON_ID on LEASE_1, now reassigned to OTHER_DAEMON_ID, queued.
    return _bound(
        status="queued",
        worker_daemon_id=OTHER_DAEMON_ID,
        worker_lease_id=None,
        worker_lease_expires_at=None,
    )


def _as_daemon(monkeypatch, daemon_id: str) -> None:
    async def auth(token):
        return {"daemon_id": daemon_id, "user_id": USER_ID}

    monkeypatch.setattr("src.server.app.autonomy._require_daemon", auth)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_path", [B_PATH, DAEMON_PATH], ids=["own-path", "same-path"])
@pytest.mark.parametrize("from_lease", [LEASE_2, None], ids=["mismatched", "null-lease"])
async def test_new_daemon_binds_after_handover_and_old_cleanup_is_accepted(
    client, install, monkeypatch, from_lease, new_path
):
    """The lease that bound the checkout is gone (NULL, or a later lease)."""
    store = install(Store(_after_reassign()))
    await ownership._hand_over_ticket_worktree(
        store.row, from_daemon_id=DAEMON_ID, from_lease_id=from_lease
    )
    # The binding could not be cleared on that lease: it is still A's.
    assert store.row["worktree_daemon_id"] == DAEMON_ID

    # The new owner B claims (LEASE_2 now B's, live) and binds its own copy.
    store.row.update(
        status="running",
        worker_lease_id=LEASE_2,
        worker_lease_expires_at=_now() + timedelta(minutes=5),
    )
    _as_daemon(monkeypatch, OTHER_DAEMON_ID)
    bind = await client.post(BIND_URL, json=_bind_body(lease_id=LEASE_2, path=new_path))
    assert bind.status_code == 200, bind.text
    assert bind.json() == {"worktree_path": new_path, "worktree_generation": 2}
    assert store.bind_kwargs[-1]["replace_generation"] == 1
    assert store.row["worktree_daemon_id"] == OTHER_DAEMON_ID
    assert store.row["worktree_cleanup_pending"] is False
    assert store.stale_notes == [
        {
            "daemon_id": DAEMON_ID,
            "worktree_path": DAEMON_PATH,
            "worktree_generation": 1,
            "lease_id": LEASE_1,
        }
    ]
    b_binding = copy.deepcopy(store.row)

    # A's drain later reports its own copy: accepted as a stale note, and
    # B's binding is untouched either way.
    _as_daemon(monkeypatch, DAEMON_ID)
    failed = await client.post(CLEANUP_URL, json=_cleanup_body("failed", lease=LEASE_1))
    assert failed.status_code == 200, failed.text
    assert failed.json() == {"ok": True, "pending": True, "stale": True}
    assert len(store.stale_notes) == 1
    removed = await client.post(CLEANUP_URL, json=_cleanup_body("removed", lease=None))
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"ok": True, "pending": False, "stale": True}
    assert store.stale_notes == []
    assert store.row == b_binding
    # Settled: a replay has nothing left to match.
    replay = await client.post(CLEANUP_URL, json=_cleanup_body("removed"))
    assert replay.status_code == 409
    assert store.row == b_binding


@pytest.mark.asyncio
async def test_bind_with_stale_read_of_departed_binding_is_fenced(
    client, install, monkeypatch
):
    """The row moved past the generation the route read: the bind refuses."""
    store = install(
        Store(
            _bound(
                generation=2,
                status="running",
                worker_daemon_id=OTHER_DAEMON_ID,
                worker_lease_id=LEASE_2,
                worker_lease_expires_at=_now() + timedelta(minutes=5),
            )
        )
    )
    real_get = store.get_ticket

    async def stale_get(ticket_id, **kw):
        row = await real_get(ticket_id, **kw)
        row["worktree_generation"] = 1
        return row

    monkeypatch.setattr(tickets_db, "get_ticket", stale_get)
    _as_daemon(monkeypatch, OTHER_DAEMON_ID)
    before = copy.deepcopy(store.row)
    bind = await client.post(BIND_URL, json=_bind_body(lease_id=LEASE_2, path=B_PATH))

    assert bind.status_code == 409
    assert store.bind_kwargs[-1]["replace_generation"] == 1
    assert store.row == before and store.stale_notes == []


@pytest.mark.asyncio
async def test_bind_never_asks_to_replace_a_live_or_own_binding(client, install):
    # Own binding (same daemon) and another daemon that still holds the
    # Ticket: no replace_generation, the ordinary fences answer.
    for over in (
        {"worker_daemon_id": DAEMON_ID},
        {"worker_daemon_id": OTHER_DAEMON_ID, "worktree_daemon_id": OTHER_DAEMON_ID},
    ):
        store = install(
            Store(
                _bound(
                    status="running",
                    worker_lease_id=LEASE_1,
                    worker_lease_expires_at=_now() + timedelta(minutes=5),
                    **over,
                )
            )
        )
        await client.post(BIND_URL, json=_bind_body(path=B_PATH))
        assert store.bind_kwargs[-1]["replace_generation"] is None
        assert store.stale_notes == []


@pytest.mark.asyncio
async def test_stale_cleanup_for_another_daemon_or_generation_is_409(
    client, install, monkeypatch
):
    store = install(Store(_bound(generation=2, worktree_daemon_id=OTHER_DAEMON_ID)))
    store.stale_notes.append(
        {
            "daemon_id": DAEMON_ID,
            "worktree_path": DAEMON_PATH,
            "worktree_generation": 1,
            "lease_id": LEASE_1,
        }
    )
    before = copy.deepcopy(store.row)
    wrong_generation = await client.post(
        CLEANUP_URL, json=_cleanup_body("removed", generation=3)
    )
    _as_daemon(monkeypatch, "55555555-5555-4555-8555-999999999999")
    wrong_daemon = await client.post(CLEANUP_URL, json=_cleanup_body("removed"))

    assert wrong_generation.status_code == 409
    assert wrong_daemon.status_code == 409
    assert len(store.stale_notes) == 1
    assert store.row == before


# ------------------------------- claim replay vs. a slow failing claim (round 4)

CLAIM_URL = "/api/v1/autonomy/daemon/claim"
HEARTBEAT = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


class ClaimLease:
    """One daemon claim, its lease, and the per-Ticket worktree lock.

    ``claim`` replays the claim while its lease is live (the daemon retrying
    the same claim id); ``release`` gives it back, fenced on that lease; the
    lock is an in-process stand-in for ``ticket_worktree_lock`` that records
    when a second request queues behind the holder.
    """

    def __init__(self, cwd: str) -> None:
        self.ticket = _claimed(
            worker_lease_heartbeat_at=HEARTBEAT,
            mandate={"cwd": cwd, "source_id": ""},
        )
        self.live = True
        self.released: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.lock_waits: list[float] = []
        self.queued_behind_holder = asyncio.Event()
        self._lock = asyncio.Lock()

    async def claim(self, daemon_id, claim_id, policy_version=""):
        return copy.deepcopy(self.ticket) if self.live else None

    async def live_lease(self, daemon_id, ticket_id, lease_id):
        return self.live and lease_id == LEASE_1 and daemon_id == DAEMON_ID

    async def release(self, daemon_id, ticket_id, lease_id, **fence):
        self.released.append(((daemon_id, ticket_id, lease_id), fence))
        if not self.live:
            return False
        self.live = False
        return True

    @contextlib.asynccontextmanager
    async def lock(self, ticket_id, *, timeout_seconds=30.0):
        self.lock_waits.append(timeout_seconds)
        if self._lock.locked():
            self.queued_behind_holder.set()
        async with self._lock:
            yield

    def wire(self, monkeypatch) -> None:
        monkeypatch.delenv("CONDUCTOR_DISPATCH_REPO", raising=False)
        monkeypatch.setattr(worker_service, "claim_daemon_ticket", self.claim)
        monkeypatch.setattr(daemon_db, "live_lease_ticket", self.live_lease)
        monkeypatch.setattr(daemon_db, "release_daemon_claim", self.release, raising=False)
        monkeypatch.setattr(tickets_db, "ticket_worktree_lock", self.lock)


def _git_cwd(tmp_path) -> str:
    # A server-visible git mandate cwd: the spec resolves the base with git.
    (tmp_path / ".git").mkdir()
    return str(tmp_path)


@pytest.mark.asyncio
async def test_claim_replay_during_slow_failing_claim_is_never_released_after_delivery(
    client, install, monkeypatch, tmp_path
):
    """The daemon times out at 30 s and retries the same claim id while the
    first request is still resolving the base. The first request then fails;
    its release must come before any response for the claim, so the replay
    answers no Ticket instead of delivering a lease that is then revoked."""
    install(Store(_row()))
    lease = ClaimLease(_git_cwd(tmp_path))
    first_resolving = asyncio.Event()
    git_answers = asyncio.Event()
    spec_calls: list[str] = []

    async def spec(row):
        spec_calls.append(str(row["ticket_id"]))
        if len(spec_calls) == 1:
            first_resolving.set()
            await git_answers.wait()
            raise TicketWorktreeUnavailable("git timed out on the server")
        return _spec()

    with _claim_path(monkeypatch, lease.ticket, None):
        lease.wire(monkeypatch)
        monkeypatch.setattr(worktree_service, "daemon_claim_worktree_spec", spec)
        first = asyncio.create_task(client.post(CLAIM_URL, json={"claim_id": LEASE_1}))
        await first_resolving.wait()
        replay = asyncio.create_task(client.post(CLAIM_URL, json={"claim_id": LEASE_1}))
        queued = asyncio.create_task(lease.queued_behind_holder.wait())
        # Unserialised, the replay is answered while the first request hangs.
        await asyncio.wait({replay, queued}, return_when=asyncio.FIRST_COMPLETED)
        replay_answered_early = replay.done()
        git_answers.set()
        first_response = await first
        replay_response = await replay
        queued.cancel()

    assert not replay_answered_early
    assert first_response.status_code == 503
    assert replay_response.status_code == 200, replay_response.text
    assert replay_response.json() == {"ticket": None}
    # Released once, before any response for the claim, fenced on the
    # heartbeat read with the claim.
    assert lease.released == [
        ((DAEMON_ID, TICKET_ID, LEASE_1), {"heartbeat_at": HEARTBEAT})
    ]
    assert spec_calls == [TICKET_ID]
    assert lease.lock_waits and all(wait < 30 for wait in lease.lock_waits)


@pytest.mark.asyncio
async def test_claim_replay_after_slow_successful_claim_gets_the_same_lease(
    client, install, monkeypatch, tmp_path
):
    install(Store(_row()))
    lease = ClaimLease(_git_cwd(tmp_path))
    first_resolving = asyncio.Event()
    git_answers = asyncio.Event()
    spec_calls: list[str] = []

    async def spec(row):
        spec_calls.append(str(row["worktree_base_sha"]))
        if len(spec_calls) == 1:
            first_resolving.set()
            await git_answers.wait()
        return _spec()

    with _claim_path(monkeypatch, lease.ticket, None):
        lease.wire(monkeypatch)
        monkeypatch.setattr(worktree_service, "daemon_claim_worktree_spec", spec)
        first = asyncio.create_task(client.post(CLAIM_URL, json={"claim_id": LEASE_1}))
        await first_resolving.wait()
        replay = asyncio.create_task(client.post(CLAIM_URL, json={"claim_id": LEASE_1}))
        queued = asyncio.create_task(lease.queued_behind_holder.wait())
        await asyncio.wait({replay, queued}, return_when=asyncio.FIRST_COMPLETED)
        replay_answered_early = replay.done()
        git_answers.set()
        responses = [await first, await replay]
        queued.cancel()

    # The replay waited for the first request instead of building in parallel.
    assert not replay_answered_early
    for response in responses:
        assert response.status_code == 200, response.text
        body = response.json()["ticket"]
        assert body["lease_id"] == LEASE_1 and body["worktree"] == _spec()
    assert lease.released == [] and lease.live
    # The replay builds its spec from the freshly read (pinned) base.
    assert spec_calls == [BASE_SHA, BASE_SHA]


@pytest.mark.asyncio
async def test_claim_busy_past_lock_wait_is_503_and_keeps_the_claim(
    client, install, monkeypatch, tmp_path
):
    install(Store(_row()))
    lease = ClaimLease(_git_cwd(tmp_path))

    @contextlib.asynccontextmanager
    async def busy(ticket_id, *, timeout_seconds=30.0):
        lease.lock_waits.append(timeout_seconds)
        raise TimeoutError("the Ticket worktree is busy; retry later")
        yield  # pragma: no cover

    with _claim_path(monkeypatch, lease.ticket, _spec()) as spec_calls:
        lease.wire(monkeypatch)
        monkeypatch.setattr(tickets_db, "ticket_worktree_lock", busy)
        response = await client.post(CLAIM_URL, json={"claim_id": LEASE_1})

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "1"
    assert "ticket" not in response.json()
    assert spec_calls == [] and lease.released == [] and lease.live
    assert lease.lock_waits == [autonomy_app._CLAIM_WORKTREE_LOCK_SECONDS]
    assert autonomy_app._CLAIM_WORKTREE_LOCK_SECONDS < 30


@pytest.mark.asyncio
async def test_claim_given_back_before_lock_answers_no_ticket(
    client, install, monkeypatch, tmp_path
):
    """The replay read the claim live, but it was released before the lock."""
    install(Store(_row()))
    lease = ClaimLease(_git_cwd(tmp_path))

    async def released_meanwhile(daemon_id, ticket_id, lease_id):
        return False

    with _claim_path(monkeypatch, lease.ticket, _spec()) as spec_calls:
        lease.wire(monkeypatch)
        monkeypatch.setattr(daemon_db, "live_lease_ticket", released_meanwhile)
        response = await client.post(CLAIM_URL, json={"claim_id": LEASE_1})

    assert response.status_code == 200
    assert response.json() == {"ticket": None}
    assert spec_calls == [] and lease.released == []
