"""An in-memory stand-in for the collab persistence layer (tests only).

It implements the L1 helpers the L2 services call (``src.server.database.collab``
plus the few Ticket helpers) with the same contracts: the claim is a revision
compare-and-swap with one live lease per target, reports go through
``record_lease_report``, every recovery path requeues the Ticket, and
``collab_transaction`` is a real transaction: state is snapshotted on entry
and restored when the block raises, so "an audit failure rolls the write
back" is observable. Transactions are serialized, like a single database
connection under row locks.

No socket, no database, no real token, no vault and no keychain is touched.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from src.collab.scopes import normalize_scopes
from src.collab.types import (
    MESSAGE_CHANNEL_CAPACITY,
    AlreadyClaimedError,
    ChannelFullError,
    ScopeDeniedError,
    SelfReviewError,
    normalize_app_id,
)
from src.server.database.collab import apps as real_app_db
from src.tools.auto_dispatch.dedup import dedup_mode

USER = "user-1"
OTHER_USER = "user-2"


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


class FakeCursor:
    def __init__(self, store: "FakeStore") -> None:
        self.store = store

    async def execute(self, sql: str, params: Any = None) -> None:
        self.store.sql.append((" ".join(str(sql).split()), params))
        if "INSERT INTO autonomy_ticket_reports" in str(sql):
            ticket_id, state, summary, question, body, git_check = params
            self.store.reports.append(
                {
                    "report_id": new_uuid(),
                    "ticket_id": ticket_id,
                    "state": state,
                    "summary": summary,
                    "question": question,
                    "body": body,
                    "git_check": git_check,
                    "created_at": now(),
                }
            )

        if "DELETE FROM autonomy_tickets" in str(sql):
            (ticket_id,) = params
            row = self.store.tickets.get(str(ticket_id))
            if row and row["status"] == "awaiting_clarification" and row["revision"] == 0:
                del self.store.tickets[str(ticket_id)]

    async def fetchone(self) -> Any:
        return None

    async def __aenter__(self) -> "FakeCursor":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class FakeConn:
    def __init__(self, store: "FakeStore") -> None:
        self.store = store

    def cursor(self, *args: Any, **kwargs: Any) -> FakeCursor:
        return FakeCursor(self.store)


_STATE_FIELDS = (
    "apps",
    "tickets",
    "leases",
    "handoffs",
    "messages",
    "reviews",
    "events",
    "reports",
    "audit",
    "vault",
)


@dataclass
class FakeStore:
    apps: dict[str, dict[str, Any]] = field(default_factory=dict)
    tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    leases: dict[str, dict[str, Any]] = field(default_factory=dict)
    handoffs: dict[str, dict[str, Any]] = field(default_factory=dict)
    messages: list[dict[str, Any]] = field(default_factory=list)
    reviews: dict[str, dict[str, Any]] = field(default_factory=dict)
    review_force_calls: list[bool] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)
    vault: dict[tuple[str, str], str] = field(default_factory=dict)
    sql: list[tuple[str, Any]] = field(default_factory=list)
    fail_audit: bool = False
    fail_audit_for: set[str] = field(default_factory=set)
    _tx_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # Seeding helpers
    # ------------------------------------------------------------------

    def add_app(
        self,
        app_id: str,
        *,
        token: str | None = None,
        scopes: Any = ("read",),
        user_id: str = USER,
        can_write: bool = True,
        max_leases: int = 1,
        enabled: bool = True,
        revoked: bool = False,
    ) -> str:
        """Register an app; returns its generated (fake) plaintext token."""
        token = token or real_app_db.generate_token()
        self.apps[app_id] = {
            "app_id": app_id,
            "user_id": user_id,
            "name": "",
            "token_sha256": real_app_db.hash_token(token),
            "scopes": sorted(normalize_scopes(scopes)),
            "can_write": can_write,
            "max_leases": max_leases,
            "enabled": enabled and not revoked,
            "revoked_at": now() if revoked else None,
            "last_used_at": None,
            "created_at": now(),
            "updated_at": now(),
        }
        return token

    def add_ticket(
        self,
        *,
        ticket_id: str | None = None,
        user_id: str = USER,
        status: str = "queued",
        title: str = "Fix the docs",
        body: str = "Update the protocol docs.",
        app_claimable: bool = True,
        write_scope: Any = ("docs/**",),
        acceptance: Any = ("docs build passes",),
        risk_bearing: bool = False,
        cwd: str = "/repo",
        revision: int = 3,
        worker_run_id: str | None = None,
        worker_lease_id: str | None = None,
        worker_daemon_id: str | None = None,
    ) -> dict[str, Any]:
        ticket_id = ticket_id or new_uuid()
        mandate: dict[str, Any] = {
            "collab": {
                "app_claimable": app_claimable,
                "write_scope": list(write_scope),
                "acceptance": list(acceptance),
                "risk_bearing": risk_bearing,
            }
        }
        if cwd:
            mandate["cwd"] = cwd
        row = {
            "ticket_id": ticket_id,
            "user_id": user_id,
            "workspace_id": "ws-1",
            "title": title,
            "scope": body,
            "status": status,
            "mandate": mandate,
            "revision": revision,
            "worker_run_id": worker_run_id,
            "worker_lease_id": worker_lease_id,
            "worker_daemon_id": worker_daemon_id,
            "worktree_base_sha": "",
            "created_at": now(),
            "updated_at": now(),
        }
        self.tickets[ticket_id] = row
        return row

    def events_for(self, ticket_id: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["ticket_id"] == ticket_id]

    def audit_codes(self) -> list[str]:
        return [row["code"] for row in self.audit]

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def _snapshot(self) -> dict[str, Any]:
        return {name: copy.deepcopy(getattr(self, name)) for name in _STATE_FIELDS}

    def _restore(self, snapshot: dict[str, Any]) -> None:
        for name, value in snapshot.items():
            setattr(self, name, value)

    @asynccontextmanager
    async def transaction(self, conn: Any = None):
        if self._tx_lock is None:
            self._tx_lock = asyncio.Lock()
        async with self._tx_lock:
            snapshot = self._snapshot()
            try:
                yield FakeConn(self)
            except BaseException:
                self._restore(snapshot)
                raise

    # ------------------------------------------------------------------
    # apps
    # ------------------------------------------------------------------

    async def app_for_token_hash(self, digest: str) -> dict[str, Any] | None:
        real_app_db.validate_token_sha256(digest)
        for row in self.apps.values():
            if row["token_sha256"] == digest and row["revoked_at"] is None and row["enabled"]:
                return dict(row)
        return None

    async def get_app(self, app_id: str) -> dict[str, Any] | None:
        row = self.apps.get(normalize_app_id(app_id))
        return dict(row) if row else None

    async def touch_app(self, app_id: str, *, conn: Any = None) -> None:
        if app_id in self.apps:
            self.apps[app_id]["last_used_at"] = now()

    async def create_app(self, *, user_id, app_id, token_sha256, scopes=None, can_write=False, max_leases=1, name="", conn=None):
        digest = real_app_db.validate_token_sha256(token_sha256)
        if app_id in self.apps:
            raise RuntimeError("duplicate app")
        self.apps[app_id] = {
            "app_id": app_id,
            "user_id": user_id,
            "name": name,
            "token_sha256": digest,
            "scopes": sorted(normalize_scopes(scopes)),
            "can_write": bool(can_write),
            "max_leases": int(max_leases),
            "enabled": True,
            "revoked_at": None,
            "last_used_at": None,
            "created_at": now(),
            "updated_at": now(),
        }
        return dict(self.apps[app_id])

    async def update_scopes(self, app_id, scopes, *, conn=None):
        row = self.apps.get(app_id)
        if row is None or row["revoked_at"] is not None:
            return None
        row["scopes"] = sorted(normalize_scopes(scopes))
        return dict(row)

    async def rotate_token(self, app_id, token_sha256, *, conn=None):
        row = self.apps.get(app_id)
        if row is None or row["revoked_at"] is not None:
            return None
        row["token_sha256"] = real_app_db.validate_token_sha256(token_sha256)
        return dict(row)

    async def set_enabled(self, app_id, enabled, *, conn=None):
        row = self.apps.get(app_id)
        if row is None:
            return None
        row["enabled"] = bool(enabled)
        return dict(row)

    async def revoke_app(self, app_id, *, conn=None):
        row = self.apps.get(app_id)
        if row is None:
            return None
        row["revoked_at"] = row["revoked_at"] or now()
        row["enabled"] = False
        return dict(row)

    async def list_apps(self, user_id=None):
        return [dict(row) for row in self.apps.values() if user_id is None or row["user_id"] == user_id]

    # ------------------------------------------------------------------
    # audit
    # ------------------------------------------------------------------

    async def record_audit(self, *, tool, app_id=None, agent="", target="", params=None, params_sha256=None, code="ok", http_status=None, rate_limited=False, redacted=0, duration_ms=0, conn=None):
        if self.fail_audit or code in self.fail_audit_for:
            raise RuntimeError("audit insert failed")
        row = {
            "audit_id": new_uuid(),
            "app_id": app_id,
            "agent": agent,
            "tool": tool,
            "target": target,
            "params_sha256": params_sha256,
            "code": code,
            "http_status": http_status,
            "rate_limited": rate_limited,
            "redacted": redacted,
            "duration_ms": duration_ms,
            "created_at": now(),
            "in_transaction": conn is not None,
        }
        self.audit.append(row)
        return row

    async def list_audit(self, *, app_id=None, code=None, limit=200, conn=None):
        rows = [r for r in self.audit if (app_id is None or r["app_id"] == app_id) and (code is None or r["code"] == code)]
        return list(reversed(rows))[:limit]

    # ------------------------------------------------------------------
    # tickets
    # ------------------------------------------------------------------

    async def get_ticket(self, ticket_id, *, conn=None, for_update=False):
        row = self.tickets.get(str(ticket_id))
        return copy.deepcopy(row) if row else None

    async def list_tickets(self, user_id, *, statuses=None, limit=100, offset=0, **_kw):
        rows = [
            copy.deepcopy(row)
            for row in self.tickets.values()
            if row["user_id"] == user_id and (not statuses or row["status"] in statuses)
        ]
        return rows[offset : offset + limit]

    async def create_ticket(self, *, user_id, workspace_id, title, scope, mandate, policy_version, origin_trigger_id, origin_thread_key, router_thread_id=None, worker_provider="", worker_model="", worker_daemon_id=None, initial_status="queued", context_scope_id=None):
        assert initial_status in {"queued", "awaiting_clarification"}
        ticket_id = new_uuid()
        row = {
            "ticket_id": ticket_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "title": title,
            "scope": scope,
            "status": initial_status,
            "mandate": copy.deepcopy(mandate),
            "revision": 0,
            "policy_version": policy_version,
            "origin_trigger_id": origin_trigger_id,
            "worker_run_id": None,
            "worker_lease_id": None,
            "worker_daemon_id": worker_daemon_id,
            "worktree_base_sha": "",
            "created_at": now(),
            "updated_at": now(),
        }
        self.tickets[ticket_id] = row
        return copy.deepcopy(row)

    async def latest_report(self, ticket_id, *, conn=None):
        rows = [r for r in self.reports if r["ticket_id"] == ticket_id]
        return dict(rows[-1]) if rows else None

    async def pin_ticket_worktree_base(self, ticket_id, *, base_sha, base_branch):
        row = self.tickets.get(ticket_id)
        if row is None:
            return None
        row["worktree_base_sha"] = row.get("worktree_base_sha") or base_sha
        return dict(row)

    async def insert_ticket_event(self, cur, *, ticket_id, user_id, actor, action, from_owner="", to_owner="", detail=None):
        assert action in {"claimed", "stopped", "resumed", "reassigned", "archived", "stop_acknowledged", "mandate_revoked"}
        self.events.append(
            {
                "ticket_id": str(ticket_id),
                "user_id": user_id,
                "actor": actor,
                "action": action,
                "from_owner": from_owner,
                "to_owner": to_owner,
                "detail": dict(detail or {}),
            }
        )

    def _requeue(self, lease: dict[str, Any]) -> int:
        ticket = self.tickets.get(str(lease.get("ticket_id") or ""))
        if (
            ticket is None
            or lease.get("target_kind") != "ticket"
            or ticket["status"] not in {"running", "waiting", "blocked"}
            or ticket["revision"] != lease["revision"]
            or ticket.get("worker_lease_id")
            or ticket.get("worker_run_id")
        ):
            return 0
        ticket["status"] = "queued"
        ticket["revision"] += 1
        return 1

    # ------------------------------------------------------------------
    # leases
    # ------------------------------------------------------------------

    def _live(self, lease: dict[str, Any]) -> bool:
        return lease["state"] in {"active", "handoff_pending"}

    def _unexpired(self, lease: dict[str, Any]) -> bool:
        return lease["expires_at"] > now() and lease["max_lease_until"] > now()

    async def get_lease(self, lease_id, *, conn=None):
        row = self.leases.get(str(lease_id))
        return dict(row) if row else None

    async def lease_for_target(self, target_ref, *, conn=None):
        for row in self.leases.values():
            if row["target_ref"] == target_ref and self._live(row):
                return dict(row)
        return None

    async def list_active_leases(self, app_id=None, *, conn=None):
        return [dict(r) for r in self.leases.values() if self._live(r) and (app_id is None or r["app_id"] == app_id)]

    async def claim_ticket_lease(self, *, ticket_id, app_id, agent="", revision, lease_seconds=900, max_lease_seconds=14400, worktree_path="", worktree_branch="", worktree_base_sha="", conn=None):
        ticket = self.tickets.get(ticket_id)
        if (
            ticket is None
            or ticket["revision"] != int(revision)
            or ticket["status"] != "queued"
            or ticket.get("worker_lease_id")
            or ticket.get("worker_run_id")
        ):
            return None
        ticket["status"] = "running"
        ticket["revision"] += 1
        if await self.lease_for_target(f"t:{ticket_id}") is not None:
            raise AlreadyClaimedError("the task already has a live collaboration lease")
        lease_id = new_uuid()
        stamp = now()
        row = {
            "lease_id": lease_id,
            "app_id": app_id,
            "agent": agent,
            "target_ref": f"t:{ticket_id}",
            "target_kind": "ticket",
            "ticket_id": ticket_id,
            "line_id": "",
            "revision": ticket["revision"],
            "state": "active",
            "worktree_path": worktree_path,
            "worktree_branch": worktree_branch,
            "worktree_base_sha": worktree_base_sha,
            "heartbeat_at": stamp,
            "expires_at": stamp + timedelta(seconds=lease_seconds),
            "max_lease_until": stamp + timedelta(seconds=max_lease_seconds),
            "created_at": stamp,
            "ended_at": None,
            "end_reason": "",
        }
        self.leases[lease_id] = row
        return dict(row)

    async def heartbeat_lease(self, lease_id, *, app_id, extend_seconds=900, conn=None):
        row = self.leases.get(lease_id)
        if row is None or row["app_id"] != app_id or not self._live(row) or not self._unexpired(row):
            return None
        row["heartbeat_at"] = now()
        row["expires_at"] = min(row["max_lease_until"], now() + timedelta(seconds=extend_seconds))
        return dict(row)

    async def record_lease_report(self, lease_id, *, app_id, ticket_status, lease_seconds=900, conn=None):
        assert ticket_status in {"ready", "waiting", "blocked"}
        row = self.leases.get(lease_id)
        if row is None or row["app_id"] != app_id or row["state"] != "active" or row["target_kind"] != "ticket" or not self._unexpired(row):
            return None
        ticket = self.tickets.get(row["ticket_id"])
        if ticket is None or ticket["status"] not in {"running", "waiting", "blocked"} or ticket["revision"] < row["revision"] or ticket.get("worker_lease_id") or ticket.get("worker_run_id"):
            return None
        ticket["status"] = ticket_status
        ticket["revision"] += 1
        row["revision"] = ticket["revision"]
        row["heartbeat_at"] = now()
        if ticket_status == "ready":
            row["state"] = "completed"
            row["ended_at"] = now()
            row["end_reason"] = "report_done"
        else:
            row["expires_at"] = now() + timedelta(seconds=lease_seconds)
        return {**dict(row), "ticket_status": ticket_status}

    async def release_lease(self, lease_id, *, app_id, reason="", state="released", conn=None):
        row = self.leases.get(lease_id)
        if row is None or row["app_id"] != app_id or not self._live(row):
            return None
        row["state"] = state
        row["ended_at"] = now()
        row["end_reason"] = reason
        return {**dict(row), "requeued": self._requeue(row)}

    async def expire_lease(self, lease_id, *, reason="lease_expired", conn=None):
        row = self.leases.get(lease_id)
        if row is None or not self._live(row) or self._unexpired(row):
            return None
        row["state"] = "expired"
        row["end_reason"] = reason
        return {**dict(row), "requeued": self._requeue(row)}

    async def set_handoff_pending(self, lease_id, *, app_id, expires_at, conn=None):
        row = self.leases.get(lease_id)
        if row is None or row["app_id"] != app_id or row["state"] != "active" or not self._unexpired(row):
            return None
        row["state"] = "handoff_pending"
        row["expires_at"] = min(row["max_lease_until"], expires_at)
        return dict(row)

    async def release_app_leases(self, app_id, *, reason="revoked", conn=None):
        out = []
        for row in self.leases.values():
            if row["app_id"] == app_id and self._live(row):
                row["state"] = "released"
                row["end_reason"] = reason
                out.append({**dict(row), "requeued": self._requeue(row)})
        return out

    async def sweep_expired_leases(self, *, limit=100, conn=None):
        out = []
        for row in list(self.leases.values()):
            if self._live(row) and not self._unexpired(row):
                row["state"] = "expired"
                row["end_reason"] = "lease_expired"
                requeued = self._requeue(row)
                out.append({k: row[k] for k in ("lease_id", "app_id", "ticket_id", "target_ref", "target_kind", "revision")} | {"requeued": requeued})
        return out[:limit]

    # ------------------------------------------------------------------
    # handoffs
    # ------------------------------------------------------------------

    async def create_handoff(self, *, lease_id, from_app_id, reason, expires_at=None, to_target="router", note="", self_report=None, conversation_ref=None, conn=None):
        lease = self.leases.get(lease_id)
        if lease is None or lease["app_id"] != from_app_id or lease["state"] != "handoff_pending":
            return None
        if any(h["lease_id"] == lease_id and h["state"] == "pending" for h in self.handoffs.values()):
            return None
        handoff_id = new_uuid()
        row = {
            "handoff_id": handoff_id,
            "lease_id": lease_id,
            "from_app_id": from_app_id,
            "to_target": to_target,
            "reason": reason,
            "note": note,
            "self_report": dict(self_report or {}),
            "conversation_ref": dict(conversation_ref) if conversation_ref else None,
            "state": "pending",
            "expires_at": expires_at or now() + timedelta(minutes=30),
            "accepted_by": None,
            "accepted_at": None,
            "created_at": now(),
        }
        self.handoffs[handoff_id] = row
        return copy.deepcopy(row)

    async def get_handoff(self, handoff_id, *, conn=None):
        row = self.handoffs.get(handoff_id)
        return copy.deepcopy(row) if row else None

    async def list_handoffs(self, *, to_target=None, app_id=None, state="pending", limit=20, conn=None):
        out = []
        for row in self.handoffs.values():
            if row["state"] != state:
                continue
            if app_id is not None and row["to_target"] not in {"any", app_id, f"app:{app_id}"}:
                continue
            out.append(copy.deepcopy(row))
        return out[:limit]

    async def accept_handoff(self, *, handoff_id, app_id, agent="", lease_seconds=900, conn=None):
        row = self.handoffs.get(handoff_id)
        if row is None or row["state"] != "pending" or row["expires_at"] <= now() or row["to_target"] not in {"any", app_id, f"app:{app_id}"}:
            return None
        lease = self.leases.get(row["lease_id"])
        if lease is None or lease["state"] != "handoff_pending" or lease["app_id"] != row["from_app_id"] or not self._unexpired(lease):
            return None
        row["state"] = "accepted"
        row["accepted_by"] = app_id
        lease["app_id"] = app_id
        lease["agent"] = agent
        lease["state"] = "active"
        lease["expires_at"] = min(lease["max_lease_until"], now() + timedelta(seconds=lease_seconds))
        return {"handoff": copy.deepcopy(row), "lease": dict(lease)}

    async def cancel_handoff(self, handoff_id, *, app_id=None, conn=None):
        row = self.handoffs.get(handoff_id)
        if row is None or row["state"] != "pending" or (app_id is not None and row["from_app_id"] != app_id):
            return None
        row["state"] = "cancelled"
        lease = self.leases.get(row["lease_id"])
        if lease and lease["state"] == "handoff_pending" and lease["app_id"] == row["from_app_id"] and self._unexpired(lease):
            lease["state"] = "active"
        return copy.deepcopy(row)

    async def expire_handoffs(self, *, limit=100, conn=None):
        out = []
        for row in self.handoffs.values():
            if row["state"] == "pending" and row["expires_at"] <= now():
                row["state"] = "expired"
                lease = self.leases.get(row["lease_id"])
                released = requeued = 0
                if lease and lease["state"] == "handoff_pending" and lease["app_id"] == row["from_app_id"]:
                    lease["state"] = "released"
                    lease["end_reason"] = "handoff_expired"
                    released = 1
                    requeued = self._requeue(lease)
                out.append({"handoff_id": row["handoff_id"], "lease_id": row["lease_id"], "released": released, "requeued": requeued})
        return out[:limit]

    # ------------------------------------------------------------------
    # messages
    # ------------------------------------------------------------------

    async def send_message(self, *, from_app_id, to_target, text, from_agent="", about_ref="", reply_to="", channel_capacity=MESSAGE_CHANNEL_CAPACITY, conn=None):
        if not isinstance(text, str) or not 1 <= len(text) <= 2000:
            raise ValueError("message text must be between 1 and 2000 characters")
        unread = [m for m in self.messages if m["to_target"] == to_target and m["read_at"] is None]
        if len(unread) >= channel_capacity:
            raise ChannelFullError("the recipient's collaboration inbox is full")
        row = {
            "message_id": new_uuid(),
            "from_app_id": from_app_id,
            "from_agent": from_agent,
            "to_target": to_target,
            "text": text,
            "about_ref": about_ref,
            "reply_to": reply_to,
            "read_at": None,
            "created_at": now(),
            "untrusted": True,
        }
        self.messages.append(row)
        return dict(row)

    async def inbox(self, *, to_target, limit=20, peek=False, conn=None):
        rows = [m for m in self.messages if m["to_target"] == to_target and m["read_at"] is None][:limit]
        if not peek:
            for row in rows:
                row["read_at"] = now()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # reviews
    # ------------------------------------------------------------------

    async def create_review(self, *, requester_app_id, author_app_id, target_ref, commit_sha, reviewer="native", focus="", lease_id=None, expires_at=None, force=False, conn=None):
        target = reviewer
        reviewer_app = None
        if reviewer.startswith("app:"):
            reviewer_app = reviewer[4:]
        if reviewer_app == author_app_id:
            raise SelfReviewError("an app cannot review its own work")
        self.review_force_calls.append(bool(force))
        if dedup_mode() == "enforce" and not force:
            for row in reversed(list(self.reviews.values())):
                if (
                    row["requester_app_id"] == requester_app_id
                    and row["target_ref"] == target_ref
                    and row["commit_sha"] == commit_sha
                    and row["reviewer_target"] == target
                    and row["state"] in {"pending", "completed"}
                ):
                    return {**copy.deepcopy(row), "deduplicated": True}
        review_id = new_uuid()
        row = {
            "review_id": review_id,
            "requester_app_id": requester_app_id,
            "author_app_id": author_app_id,
            "reviewer_target": target,
            "reviewer_app_id": reviewer_app,
            "target_ref": target_ref,
            "lease_id": lease_id,
            "commit_sha": commit_sha,
            "focus": focus,
            "state": "pending",
            "expires_at": expires_at or now() + timedelta(hours=24),
            "verdict": None,
            "created_at": now(),
            "completed_at": None,
        }
        self.reviews[review_id] = row
        return dict(row)

    async def get_review(self, review_id):
        row = self.reviews.get(review_id)
        return copy.deepcopy(row) if row else None

    async def list_reviews(self, *, app_id=None, reviewer_target=None, state="pending", limit=20):
        out = []
        for row in self.reviews.values():
            if row["state"] != state:
                continue
            if app_id is not None and not (row["reviewer_target"] in {"any", f"app:{app_id}"} or row["reviewer_app_id"] == app_id):
                continue
            out.append(copy.deepcopy(row))
        return out[:limit]

    async def submit_review(self, *, review_id, reviewer_app_id, verdict, conn=None):
        row = self.reviews.get(review_id)
        if row is None or row["state"] != "pending":
            return None
        if row["author_app_id"] == reviewer_app_id:
            raise SelfReviewError("an app cannot review its own work")
        if row["reviewer_target"] not in {"any", f"app:{reviewer_app_id}"}:
            raise ScopeDeniedError("the review is assigned to a different reviewer")
        row["state"] = "completed"
        row["verdict"] = dict(verdict)
        row["reviewer_app_id"] = reviewer_app_id
        return copy.deepcopy(row)

    async def expire_reviews(self, *, limit=100, conn=None):
        out = []
        for row in self.reviews.values():
            if row["state"] == "pending" and row["expires_at"] <= now():
                row["state"] = "expired"
                out.append({"review_id": row["review_id"]})
        return out[:limit]


class FakeRedactor:
    """Replaces known secrets the way ``SecretRedactor.redact`` does."""

    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = dict(secrets or {})

    def redact(self, text: str, vault_secrets: dict[str, str] | None = None) -> str:
        for name, value in {**self.secrets, **(vault_secrets or {})}.items():
            if value and len(value) >= 8 and value in text:
                text = text.replace(value, f"[REDACTED:{name}]")
        return text


def install(monkeypatch, store: FakeStore) -> FakeStore:
    """Point every persistence call the collab services make at ``store``."""
    from src.server.database.autonomy import lines as line_db
    from src.server.database.autonomy import tickets as ticket_db
    from src.server.database.collab import apps as app_db
    from src.server.database.collab import audit as audit_db
    from src.server.database.collab import handoffs as handoff_db
    from src.server.database.collab import leases as lease_db
    from src.server.database.collab import messages as message_db
    from src.server.database.collab import reviews as review_db
    from src.server.services.collab import apps as app_svc
    from src.server.services.collab import sweep as sweep_svc
    from src.server.services.collab import tools as tools_svc

    for name in ("app_for_token_hash", "get_app", "touch_app", "create_app", "update_scopes", "rotate_token", "set_enabled", "revoke_app", "list_apps"):
        monkeypatch.setattr(app_db, name, getattr(store, name))
    monkeypatch.setattr(audit_db, "record_audit", store.record_audit)
    monkeypatch.setattr(audit_db, "list_audit", store.list_audit)
    for name in ("get_lease", "lease_for_target", "list_active_leases", "claim_ticket_lease", "heartbeat_lease", "record_lease_report", "release_lease", "expire_lease", "set_handoff_pending", "release_app_leases", "sweep_expired_leases"):
        monkeypatch.setattr(lease_db, name, getattr(store, name))
    for name in ("create_handoff", "get_handoff", "list_handoffs", "accept_handoff", "cancel_handoff", "expire_handoffs"):
        monkeypatch.setattr(handoff_db, name, getattr(store, name))
    monkeypatch.setattr(message_db, "send_message", store.send_message)
    monkeypatch.setattr(message_db, "inbox", store.inbox)
    for name in ("create_review", "get_review", "list_reviews", "submit_review", "expire_reviews"):
        monkeypatch.setattr(review_db, name, getattr(store, name))
    for name in ("get_ticket", "list_tickets", "create_ticket", "latest_report", "pin_ticket_worktree_base"):
        monkeypatch.setattr(ticket_db, name, getattr(store, name))
    monkeypatch.setattr(line_db, "insert_ticket_event", store.insert_ticket_event)
    for module in (tools_svc, app_svc, sweep_svc):
        monkeypatch.setattr(module, "collab_transaction", store.transaction)

    async def no_vault(user_id: str) -> dict[str, str]:
        return {}

    monkeypatch.setattr(tools_svc, "vault_secrets", no_vault)
    monkeypatch.setattr(tools_svc, "redactor", lambda: FakeRedactor())

    async def store_vault(user_id, app_id, token, *, conn=None):
        store.vault[(user_id, app_id)] = token

    monkeypatch.setattr(app_svc, "store_vault_token", store_vault)
    return store


__all__ = ["FakeRedactor", "FakeStore", "OTHER_USER", "USER", "install", "new_uuid", "now"]
