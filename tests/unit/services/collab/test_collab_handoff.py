"""Handoff between holders: request, list, accept, expiry (N80 L2, 6.1-6.3).

Two apps of the same user, ``codex`` and ``claude-code`` (scopes read, claim,
report), and a Ticket whose ``write_scope`` is ``docs/**``. ``codex`` claims
it and hands it off. Everything runs on the in-memory ``FakeStore``; only fake
tokens are used.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.collab.schemas import validate_tool_output
from src.server.services.collab import handoff as handoff_svc
from src.server.services.collab import sweep
from tests.unit.services.collab.collab_fakes import OTHER_USER

SCOPES = ["read", "claim", "report"]
COMMIT = "a" * 40


def _report(status: str = "partial", commits: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "summary": "Updated the protocol docs section.",
        "artifacts": {"commits": list(commits if commits is not None else [COMMIT]), "files": ["docs/protocol.md"], "pr": None},
        "verification": [{"ran": "docs build", "result": "passed"}],
        "next": "Finish the remaining section.",
        "handbacks": [],
    }


SELF_REPORT = {
    "done": ["wrote the intro"],
    "in_progress": ["section two"],
    "next": ["section three"],
    "pitfalls": ["the build is slow"],
    "open_questions": ["which version to cite"],
}


def _tokens(store, **overrides: Any) -> dict[str, str]:
    codex = store.add_app("codex", scopes=SCOPES, **overrides.get("codex", {}))
    claude = store.add_app("claude-code", scopes=SCOPES, **overrides.get("claude-code", {}))
    return {"codex": codex, "claude-code": claude}


async def _claim(call, store, token: str, **ticket_kw: Any) -> tuple[dict[str, Any], str]:
    ticket = store.add_ticket(**ticket_kw)
    ref = f"t:{ticket['ticket_id']}"
    status, env = await call("task_claim", token, ref=ref, revision=ticket["revision"])
    assert status == 200, env
    return ticket, env["data"]["lease_id"]


async def _request(call, token: str, lease_id: str, **kw: Any):
    kw.setdefault("reason", "quota_exhausted")
    return await call("handoff_request", token, lease_id=lease_id, **kw)


def _audit_codes(store, tool: str) -> list[str]:
    return [row["code"] for row in store.audit if row["tool"] == tool]


# ---------------------------------------------------------------------------
# 1. request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("to", ["claude-code", "app:claude-code"])
async def test_request_marks_lease_pending_and_old_holder_cannot_report(store, call, to):
    tokens = _tokens(store)
    _, lease_id = await _claim(call, store, tokens["codex"])
    before = datetime.now(timezone.utc)
    status, env = await _request(call, tokens["codex"], lease_id, to=to, note="over to you")
    assert status == 200, env
    data = env["data"]
    assert set(data) == {"handoff_id", "expires_at"}
    expires = datetime.fromisoformat(data["expires_at"])
    assert timedelta(minutes=29) <= expires - before <= timedelta(minutes=31)
    assert store.handoffs[data["handoff_id"]]["to_target"] == "app:claude-code"

    lease = store.leases[lease_id]
    assert lease["state"] == "handoff_pending"
    assert lease["app_id"] == "codex"

    status, env = await call("task_report", tokens["codex"], lease_id=lease_id, report=_report())
    assert status == 409
    assert env["code"] == "lease_lost"
    # Still codex's until somebody accepts.
    assert store.leases[lease_id]["app_id"] == "codex"
    assert store.leases[lease_id]["state"] == "handoff_pending"


# ---------------------------------------------------------------------------
# 2. list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_shows_handoff_to_target_only(store, call):
    tokens = _tokens(store)
    other = store.add_app("other-app", scopes=SCOPES, user_id=OTHER_USER)
    _, lease_id = await _claim(call, store, tokens["codex"])
    status, env = await _request(call, tokens["codex"], lease_id, to="claude-code", reason="context_full", note="please finish")
    assert status == 200
    handoff_id = env["data"]["handoff_id"]

    status, env = await call("handoff_list", tokens["claude-code"])
    assert status == 200, env
    validate_tool_output("handoff_list", env["data"])
    items = env["data"]["handoffs"]
    assert len(items) == 1
    item = items[0]
    assert item["handoff_id"] == handoff_id
    assert item["from"] == "app:codex"
    assert item["reason"] == "context_full"
    assert item["note"] == "please finish"
    assert item["state"] == "pending"

    status, env = await call("handoff_list", tokens["codex"])
    assert status == 200
    assert env["data"]["handoffs"] == []

    status, env = await call("handoff_list", other)
    assert status == 200
    assert env["data"]["handoffs"] == []


@pytest.mark.asyncio
async def test_list_any_target_hidden_from_other_user(store, call):
    tokens = _tokens(store)
    other = store.add_app("other-app", scopes=SCOPES, user_id=OTHER_USER)
    _, lease_id = await _claim(call, store, tokens["codex"])
    status, env = await _request(call, tokens["codex"], lease_id, to="any")
    assert status == 200
    handoff_id = env["data"]["handoff_id"]

    _, env = await call("handoff_list", tokens["claude-code"])
    assert [h["handoff_id"] for h in env["data"]["handoffs"]] == [handoff_id]
    _, env = await call("handoff_list", tokens["codex"])
    assert env["data"]["handoffs"] == []
    _, env = await call("handoff_list", other)
    assert env["data"]["handoffs"] == []

    # Another user's app cannot accept it either.
    status, env = await call("handoff_accept", other, handoff_id=handoff_id)
    assert status == 404
    assert env["code"] == "not_found"
    assert store.leases[lease_id]["app_id"] == "codex"


# ---------------------------------------------------------------------------
# 3. accept
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accept_moves_lease_and_old_holder_loses_it(store, call):
    tokens = _tokens(store)
    ticket, lease_id = await _claim(call, store, tokens["codex"])
    _, env = await _request(call, tokens["codex"], lease_id, to="claude-code", self_report=SELF_REPORT)
    handoff_id = env["data"]["handoff_id"]

    status, env = await call("handoff_accept", tokens["claude-code"], handoff_id=handoff_id)
    assert status == 200, env
    data = env["data"]
    validate_tool_output("handoff_accept", data)
    assert data["lease_id"] == lease_id
    # The new holder heartbeats the moved lease like a claimed one.
    assert data["heartbeat_every_seconds"] == 60

    lease = store.leases[lease_id]
    assert lease["app_id"] == "claude-code"
    assert lease["state"] == "active"
    assert store.handoffs[handoff_id]["state"] == "accepted"

    handed = [
        e for e in store.events_for(ticket["ticket_id"])
        if e["detail"].get("collab_event") == "handed_over"
    ]
    assert len(handed) == 1
    event = handed[0]
    assert event["action"] == "reassigned"
    assert event["from_owner"] == "app:codex"
    assert event["to_owner"] == "app:claude-code"
    assert event["detail"]["handoff_id"] == handoff_id
    assert event["detail"]["lease_id"] == lease_id

    status, env = await call("task_report", tokens["codex"], lease_id=lease_id, report=_report())
    assert status == 409
    assert env["code"] == "lease_lost"
    status, env = await call("task_heartbeat", tokens["codex"], lease_id=lease_id)
    assert status == 409
    assert env["code"] == "lease_lost"

    status, env = await call("task_report", tokens["claude-code"], lease_id=lease_id, report=_report("done"))
    assert status == 200, env
    assert env["data"]["ticket_state"] == "ready"
    assert store.leases[lease_id]["state"] == "completed"


# ---------------------------------------------------------------------------
# 4. pack contents
# ---------------------------------------------------------------------------


async def _scenario(call, store, tokens, *, conversation_ref: dict[str, str] | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """claim -> partial report -> handoff -> accept; returns (envelope, ticket)."""
    ticket, lease_id = await _claim(call, store, tokens["codex"])
    status, env = await call("task_report", tokens["codex"], lease_id=lease_id, report=_report("partial"))
    assert status == 200, env
    assert store.leases[lease_id]["state"] == "active"
    kw: dict[str, Any] = {"to": "claude-code", "self_report": SELF_REPORT, "reason": "end_of_session"}
    if conversation_ref is not None:
        kw["conversation_ref"] = conversation_ref
    status, env = await _request(call, tokens["codex"], lease_id, **kw)
    assert status == 200, env
    status, env = await call("handoff_accept", tokens["claude-code"], handoff_id=env["data"]["handoff_id"])
    assert status == 200, env
    # Free claude-code's lease slot for the next scenario.
    status, done = await call("task_report", tokens["claude-code"], lease_id=lease_id, report=_report("done"))
    assert status == 200, done
    return env, ticket


@pytest.mark.asyncio
async def test_pack_contents_are_system_facts_and_deterministic(store, call):
    tokens = _tokens(store)
    convo = {"app": "codex", "session_id": "sess-123"}
    env1, ticket1 = await _scenario(call, store, tokens, conversation_ref=convo)
    env2, ticket2 = await _scenario(call, store, tokens, conversation_ref=convo)

    pack = env1["data"]["pack"]
    system = pack["system"]
    assert set(system) >= {"task", "commits", "gate_results", "open_findings", "from", "reason"}
    assert system["task"]["ref"] == f"t:{ticket1['ticket_id']}"
    assert system["task"]["kind"] == "ticket"
    assert system["task"]["write_scope"] == ["docs/**"]
    assert system["from"] == "app:codex"
    assert system["reason"] == "end_of_session"
    assert system["gate_results"] == []
    assert system["open_findings"] == []
    assert system["last_report"]["status"] == "partial"
    assert system["last_report"]["artifacts"]["commits"] == [COMMIT]
    assert system["commits"] == [COMMIT]

    assert pack["worker_reported"] == SELF_REPORT
    assert pack["conversation_ref"] == convo
    assert set(pack) == {"system", "worktree", "worker_reported", "conversation_ref"}

    fields = env1["untrusted_fields"]
    assert "data.pack.worker_reported" in fields
    assert "data.pack.system.last_report" in fields
    # The handing-off app chose conversation_ref and the task's title.
    assert "data.pack.conversation_ref" in fields
    assert "data.pack.system.task.title" in fields

    def strip(section: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(section)
        out["task"].pop("ref")
        return out

    assert ticket1["ticket_id"] != ticket2["ticket_id"]
    assert strip(env1["data"]["pack"]["system"]) == strip(env2["data"]["pack"]["system"])


def test_clean_self_report_limits_items_and_chars():
    raw = {"done": [f"{i}-" + "x" * 500 for i in range(25)], "bogus": ["dropped"], "next": []}
    out = handoff_svc.clean_self_report(raw)
    assert set(out) == {"done"}
    assert len(out["done"]) == 10
    assert all(len(item) <= 200 for item in out["done"])
    assert out["done"][0].startswith("0-")


@pytest.mark.asyncio
async def test_worker_reported_is_sanitized(store, call, monkeypatch):
    calls: list[str] = []

    def fake_sanitize(text: str) -> str:
        calls.append(text)
        return text.replace("SECRETISH", "[REDACTED]")

    monkeypatch.setattr(handoff_svc, "_sanitize", fake_sanitize)
    tokens = _tokens(store)
    _, lease_id = await _claim(call, store, tokens["codex"])
    _, env = await _request(call, tokens["codex"], lease_id, to="claude-code", self_report={"done": ["has SECRETISH inside"]})
    _, env = await call("handoff_accept", tokens["claude-code"], handoff_id=env["data"]["handoff_id"])
    assert env["data"]["pack"]["worker_reported"] == {"done": ["has [REDACTED] inside"]}
    assert "has SECRETISH inside" in calls


# ---------------------------------------------------------------------------
# 5. "not readable" predecessor
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pack_without_conversation_ref_has_no_conversation(store, call):
    tokens = _tokens(store)
    env, _ = await _scenario(call, store, tokens, conversation_ref=None)
    pack = env["data"]["pack"]
    assert "conversation_ref" not in pack
    assert set(pack) <= {"system", "worktree", "worker_reported"}
    assert not any("conversation" in key or "transcript" in key for key in pack["system"])


# ---------------------------------------------------------------------------
# 6. to = router
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handoff_to_router_releases_and_requeues(store, call):
    tokens = _tokens(store)
    ticket, lease_id = await _claim(call, store, tokens["codex"])
    status, env = await _request(call, tokens["codex"], lease_id, to="router")
    assert status == 200, env
    handoff_id = env["data"]["handoff_id"]
    assert store.leases[lease_id]["state"] == "released"
    assert store.tickets[ticket["ticket_id"]]["status"] == "queued"
    handed = [
        e for e in store.events_for(ticket["ticket_id"])
        if e["detail"].get("collab_event") == "handed_over"
    ]
    assert len(handed) == 1
    assert handed[0]["to_owner"] == "router"
    assert handed[0]["from_owner"] == "app:codex"
    assert handed[0]["detail"]["handoff_id"] == handoff_id
    # The Router took it: the handoff row is closed at once, not left pending.
    assert store.handoffs[handoff_id]["state"] == "cancelled"

    # Nobody can pick up the released lease through the handoff.
    status, env = await call("handoff_accept", tokens["claude-code"], handoff_id=handoff_id)
    assert status in {404, 409}
    assert store.leases[lease_id]["app_id"] == "codex"


# ---------------------------------------------------------------------------
# 7. nobody accepts in time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_expired_handoff_goes_back_to_router(store, call):
    tokens = _tokens(store)
    ticket, lease_id = await _claim(call, store, tokens["codex"])
    _, env = await _request(call, tokens["codex"], lease_id, to="claude-code")
    handoff_id = env["data"]["handoff_id"]
    store.handoffs[handoff_id]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    result = await sweep.sweep_once(environ={"CONDUCTOR_COLLAB_MODE": "full"})
    assert result["handoffs"] == 1
    assert store.tickets[ticket["ticket_id"]]["status"] == "queued"
    assert store.leases[lease_id]["state"] == "released"
    assert store.handoffs[handoff_id]["state"] == "expired"
    released = [
        e for e in store.events_for(ticket["ticket_id"])
        if e["detail"].get("collab_event") == "released"
    ]
    assert released and released[-1]["to_owner"] == "router"
    assert released[-1]["detail"]["reason"] == "handoff_expired"

    status, env = await call("handoff_accept", tokens["claude-code"], handoff_id=handoff_id)
    assert status == 404
    assert env["code"] == "not_found"
    assert _audit_codes(store, "handoff_accept")[-1] == "not_found"


# ---------------------------------------------------------------------------
# 8. refusals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("to", ["codex", "app:codex"])
async def test_refuse_handoff_to_self(store, call, to):
    tokens = _tokens(store)
    _, lease_id = await _claim(call, store, tokens["codex"])
    status, env = await _request(call, tokens["codex"], lease_id, to=to)
    assert status == 400
    assert env["code"] == "invalid_schema"
    assert store.leases[lease_id]["state"] == "active"
    assert store.handoffs == {}
    assert _audit_codes(store, "handoff_request") == ["invalid_schema"]


@pytest.mark.asyncio
async def test_refuse_self_report_over_4kib(store, call):
    tokens = _tokens(store)
    _, lease_id = await _claim(call, store, tokens["codex"])
    big = {name: ["y" * 200] * 10 for name in handoff_svc.SELF_REPORT_FIELDS}
    status, env = await _request(call, tokens["codex"], lease_id, to="claude-code", self_report=big)
    assert status == 413
    assert env["code"] == "too_large"
    assert store.leases[lease_id]["state"] == "active"
    assert store.handoffs == {}
    assert _audit_codes(store, "handoff_request") == ["too_large"]


@pytest.mark.asyncio
async def test_refuse_accepting_own_handoff(store, call):
    tokens = _tokens(store)
    _, lease_id = await _claim(call, store, tokens["codex"])
    _, env = await _request(call, tokens["codex"], lease_id, to="any")
    handoff_id = env["data"]["handoff_id"]
    status, env = await call("handoff_accept", tokens["codex"], handoff_id=handoff_id)
    assert status == 409
    assert env["code"] == "assignment_refused"
    assert store.handoffs[handoff_id]["state"] == "pending"
    assert _audit_codes(store, "handoff_accept") == ["assignment_refused"]


@pytest.mark.asyncio
async def test_refuse_accept_at_max_leases(store, call):
    tokens = _tokens(store)
    _, own_lease = await _claim(call, store, tokens["claude-code"])
    _, lease_id = await _claim(call, store, tokens["codex"])
    _, env = await _request(call, tokens["codex"], lease_id, to="claude-code")
    handoff_id = env["data"]["handoff_id"]
    status, env = await call("handoff_accept", tokens["claude-code"], handoff_id=handoff_id)
    assert status == 409
    assert env["code"] == "assignment_refused"
    assert "max_leases" in env.get("message", "")
    assert store.leases[lease_id]["app_id"] == "codex"
    assert store.leases[lease_id]["state"] == "handoff_pending"
    assert store.handoffs[handoff_id]["state"] == "pending"
    assert _audit_codes(store, "handoff_accept") == ["assignment_refused"]


@pytest.mark.asyncio
async def test_refuse_accept_by_read_only_app_for_write_scope(store, call):
    tokens = _tokens(store, **{"claude-code": {"can_write": False}})
    _, lease_id = await _claim(call, store, tokens["codex"])
    _, env = await _request(call, tokens["codex"], lease_id, to="claude-code")
    handoff_id = env["data"]["handoff_id"]
    status, env = await call("handoff_accept", tokens["claude-code"], handoff_id=handoff_id)
    assert status == 409
    assert env["code"] == "assignment_refused"
    assert store.leases[lease_id]["app_id"] == "codex"
    assert store.handoffs[handoff_id]["state"] == "pending"
    assert _audit_codes(store, "handoff_accept") == ["assignment_refused"]


@pytest.mark.asyncio
async def test_refuse_credential_in_note(store, call):
    tokens = _tokens(store)
    _, lease_id = await _claim(call, store, tokens["codex"])
    fake_token = "ghp_" + "A1b2C3d4E5" * 3 + "F6g7H8"
    assert len(fake_token) == 40
    status, env = await _request(call, tokens["codex"], lease_id, to="claude-code", note=f"use {fake_token}")
    assert status == 400
    assert env["code"] == "secret_in_payload"
    assert fake_token not in str(env)
    assert store.leases[lease_id]["state"] == "active"
    assert store.handoffs == {}
    assert _audit_codes(store, "handoff_request") == ["secret_in_payload"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["nope", "00000000-0000-0000-0000-000000000000"])
async def test_refuse_bad_handoff_id(store, call, bad):
    tokens = _tokens(store)
    status, env = await call("handoff_accept", tokens["claude-code"], handoff_id=bad)
    assert status == 404
    assert env["code"] == "not_found"
    assert _audit_codes(store, "handoff_accept") == ["not_found"]
