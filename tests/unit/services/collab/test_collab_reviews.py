"""Review request / list / submit between apps (N80 L2, 5.3, 6.5, H4).

Codex (the author) claims a docs ticket and asks for reviews; claude-code
reviews; cursor has no ``review`` scope. Self review is refused by app id,
whatever agent label the call carries. Everything runs against the in-memory
``FakeStore``; no database, socket or real token is used.
"""

from __future__ import annotations

import copy
import uuid
from typing import Any

import pytest
import pytest_asyncio

from src.collab.schemas import validate_tool_output

COMMIT = "c" * 40
VERDICT = {"passed": True, "blockers": [], "verdict": "Looks right to me."}


@pytest.fixture
def apps(store) -> dict[str, str]:
    return {
        "codex": store.add_app("codex", scopes=["read", "claim", "report", "review"]),
        "claude-code": store.add_app("claude-code", scopes=["read", "review"]),
        "cursor": store.add_app("cursor", scopes=["read"]),
    }


async def _claim(call, store, token: str, *, write_scope=("docs/**",)) -> dict[str, Any]:
    ticket = store.add_ticket(write_scope=write_scope)
    status, env = await call(
        "task_claim", token, ref=f"t:{ticket['ticket_id']}", revision=ticket["revision"]
    )
    assert status == 200, env
    return env["data"]


@pytest_asyncio.fixture
async def lease(call, store, apps) -> dict[str, Any]:
    return await _claim(call, store, apps["codex"])


async def _request(call, token: str, lease_id: str, **extra: Any):
    return await call("review_request", token, lease_id=lease_id, commit=COMMIT, **extra)


async def _review_id(call, apps, lease, reviewer: str = "app:claude-code") -> str:
    status, env = await _request(call, apps["codex"], lease["lease_id"], reviewer=reviewer)
    assert status == 200, env
    return env["data"]["review_id"]


def _last_audit(store) -> dict[str, Any]:
    return store.audit[-1]


# ---------------------------------------------------------------------------
# 1. review_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_request_to_app_reviewer_ok(call, store, apps, lease):
    status, env = await _request(call, apps["codex"], lease["lease_id"], reviewer="app:claude-code")
    assert status == 200, env
    data = env["data"]
    validate_tool_output("review_request", data)
    assert data["state"] == "pending"
    row = store.reviews[data["review_id"]]
    assert row["reviewer_target"] == "app:claude-code"
    assert row["author_app_id"] == "codex"
    assert row["requester_app_id"] == "codex"
    assert row["commit_sha"] == COMMIT
    assert row["target_ref"] == lease["ref"]
    assert row["lease_id"] == lease["lease_id"]
    ok = _last_audit(store)
    assert ok["code"] == "ok" and ok["tool"] == "review_request" and ok["in_transaction"]


@pytest.mark.asyncio
async def test_review_request_default_reviewer_is_native(call, store, apps, lease):
    status, env = await _request(call, apps["codex"], lease["lease_id"])
    assert status == 200, env
    validate_tool_output("review_request", env["data"])
    assert store.reviews[env["data"]["review_id"]]["reviewer_target"] == "native"


@pytest.mark.asyncio
async def test_review_request_any_reviewer(call, store, apps, lease):
    status, env = await _request(call, apps["codex"], lease["lease_id"], reviewer="any")
    assert status == 200, env
    validate_tool_output("review_request", env["data"])
    assert store.reviews[env["data"]["review_id"]]["reviewer_target"] == "any"


# ---------------------------------------------------------------------------
# 2. self review refused by app id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("reviewer", ["app:codex", "codex"])
async def test_review_request_naming_self_is_self_review(call, store, apps, lease, reviewer):
    before = copy.deepcopy(store.reviews)
    status, env = await _request(call, apps["codex"], lease["lease_id"], reviewer=reviewer)
    assert status == 403
    assert env["code"] == "self_review"
    assert store.reviews == before
    audit = _last_audit(store)
    assert audit["code"] == "self_review"
    assert audit["http_status"] == 403
    assert audit["tool"] == "review_request"


@pytest.mark.asyncio
async def test_review_submit_by_author_under_other_agent_is_self_review(call, store, apps, lease):
    review_id = await _review_id(call, apps, lease, reviewer="any")
    before = copy.deepcopy(store.reviews)
    status, env = await call(
        "review_submit",
        apps["codex"],
        headers={"X-Collab-Agent": "other-model"},
        review_id=review_id,
        verdict=VERDICT,
    )
    assert status == 403
    assert env["code"] == "self_review"
    assert store.reviews == before
    assert store.reviews[review_id]["state"] == "pending"
    audit = _last_audit(store)
    assert audit["code"] == "self_review"
    assert audit["http_status"] == 403
    assert audit["app_id"] == "codex"
    assert audit["agent"] == "other-model"


# ---------------------------------------------------------------------------
# 3. review_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_list_for_reviewer_shows_app_and_any_not_native(call, store, apps, lease):
    mine = await _review_id(call, apps, lease, reviewer="app:claude-code")
    anyone = await _review_id(call, apps, lease, reviewer="any")
    native = await _review_id(call, apps, lease, reviewer="native")
    # A review claude-code itself authored must never be in its own queue.
    own_id = str(uuid.uuid4())
    store.reviews[own_id] = {
        **copy.deepcopy(store.reviews[anyone]),
        "review_id": own_id,
        "author_app_id": "claude-code",
        "requester_app_id": "claude-code",
        "reviewer_target": "any",
    }
    status, env = await call("review_list", apps["claude-code"], limit=50)
    assert status == 200, env
    validate_tool_output("review_list", env["data"])
    ids = {item["review_id"] for item in env["data"]["reviews"]}
    assert ids == {mine, anyone}
    assert native not in ids and own_id not in ids
    for item in env["data"]["reviews"]:
        assert item["author"] == "app:codex"
        assert item["commit"] == COMMIT
    assert "data.reviews[].focus" in env["untrusted_fields"]


@pytest.mark.asyncio
async def test_review_list_without_review_scope_is_empty(call, store, apps, lease):
    await _review_id(call, apps, lease, reviewer="any")
    status, env = await call("review_list", apps["cursor"])
    assert status == 200, env
    assert env["data"] == {"reviews": []}
    validate_tool_output("review_list", env["data"])


@pytest.mark.asyncio
async def test_review_list_not_for_me_lists_authored(call, store, apps, lease):
    ids = {
        await _review_id(call, apps, lease, reviewer="app:claude-code"),
        await _review_id(call, apps, lease, reviewer="native"),
    }
    status, env = await call("review_list", apps["codex"], for_me=False)
    assert status == 200, env
    validate_tool_output("review_list", env["data"])
    assert {item["review_id"] for item in env["data"]["reviews"]} == ids
    # claude-code neither authored nor requested any of them.
    status, env = await call("review_list", apps["claude-code"], for_me=False)
    assert status == 200 and env["data"]["reviews"] == []


@pytest.mark.asyncio
async def test_review_list_other_user_sees_nothing(call, store, apps, lease):
    await _review_id(call, apps, lease, reviewer="any")
    other = store.add_app("stranger", scopes=["read", "review"], user_id="user-2")
    status, env = await call("review_list", other)
    assert status == 200, env
    assert env["data"]["reviews"] == []
    status, env = await call("review_list", other, for_me=False)
    assert status == 200 and env["data"]["reviews"] == []


# ---------------------------------------------------------------------------
# 4. review_submit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_submit_by_reviewer_ok_then_not_found(call, store, apps, lease):
    review_id = await _review_id(call, apps, lease)
    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=VERDICT)
    assert status == 200, env
    data = env["data"]
    validate_tool_output("review_submit", data)
    assert data["review_id"] == review_id
    assert data["state"] == "completed"
    assert data["verdict"] == VERDICT
    assert "data.verdict" in env["untrusted_fields"]
    assert store.reviews[review_id]["state"] == "completed"
    assert store.reviews[review_id]["reviewer_app_id"] == "claude-code"
    audit = _last_audit(store)
    assert audit["code"] == "ok" and audit["tool"] == "review_submit"
    assert audit["in_transaction"] is True
    assert audit["target"] == review_id

    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=VERDICT)
    assert status == 404
    assert env["code"] == "not_found"


@pytest.mark.asyncio
async def test_review_submit_by_unassigned_third_app_is_scope_denied(call, store, apps, lease):
    review_id = await _review_id(call, apps, lease, reviewer="app:claude-code")
    third = store.add_app("gemini", scopes=["read", "review"])
    before = copy.deepcopy(store.reviews)
    status, env = await call("review_submit", third, review_id=review_id, verdict=VERDICT)
    assert status == 403
    assert env["code"] == "scope_denied"
    assert store.reviews == before
    assert _last_audit(store)["code"] == "scope_denied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict",
    [
        {"passed": True, "verdict": "fine"},
        {**VERDICT, "extra": "x"},
    ],
    ids=["missing_blockers", "extra_key"],
)
async def test_review_submit_bad_verdict_shape_invalid_schema(call, store, apps, lease, verdict):
    review_id = await _review_id(call, apps, lease)
    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=verdict)
    assert status == 400
    assert env["code"] == "invalid_schema"
    assert store.reviews[review_id]["state"] == "pending"


@pytest.mark.asyncio
async def test_review_submit_secret_in_verdict_refused(call, store, apps, lease):
    review_id = await _review_id(call, apps, lease)
    leaked = {**VERDICT, "verdict": "token ghp_" + "A1b2C3d4E5" * 3 + "F6g7H8"}
    assert len(leaked["verdict"].split("ghp_")[1]) == 36
    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=leaked)
    assert status == 400
    assert env["code"] == "secret_in_payload"
    assert store.reviews[review_id]["state"] == "pending"
    assert store.reviews[review_id]["verdict"] is None


# ---------------------------------------------------------------------------
# 5. review_request needs the caller's active lease
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_request_on_other_apps_lease_is_lease_lost(call, store, apps, lease):
    other = store.add_app("kimi", scopes=["read", "claim", "report"])
    other_lease = await _claim(call, store, other)
    status, env = await _request(call, apps["codex"], other_lease["lease_id"], reviewer="any")
    assert status == 409
    assert env["code"] == "lease_lost"
    assert store.reviews == {}


@pytest.mark.asyncio
async def test_review_request_unknown_lease_not_found(call, store, apps, lease):
    status, env = await _request(call, apps["codex"], str(uuid.uuid4()))
    assert status == 404
    assert env["code"] == "not_found"
    assert store.reviews == {}


@pytest.mark.asyncio
async def test_review_request_bad_commit_invalid_schema(call, store, apps, lease):
    status, env = await call("review_request", apps["codex"], lease_id=lease["lease_id"], commit="XYZ")
    assert status == 400
    assert env["code"] == "invalid_schema"
    assert store.reviews == {}


# ---------------------------------------------------------------------------
# 6. rollout modes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_docs_mode_review_submit_on_non_docs_task_mode_denied(call, store, apps, monkeypatch):
    lease = await _claim(call, store, apps["codex"], write_scope=("src/a.py",))
    review_id = await _review_id(call, apps, lease)
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "docs")
    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=VERDICT)
    assert status == 403
    assert env["code"] == "mode_denied"
    assert store.reviews[review_id]["state"] == "pending"
    assert _last_audit(store)["code"] == "mode_denied"


@pytest.mark.asyncio
async def test_docs_mode_review_submit_on_docs_task_ok(call, store, apps, lease, monkeypatch):
    review_id = await _review_id(call, apps, lease)
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "docs")
    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=VERDICT)
    assert status == 200, env
    assert env["data"]["state"] == "completed"


@pytest.mark.asyncio
async def test_shadow_mode_review_submit_mode_denied(call, store, apps, lease, monkeypatch):
    review_id = await _review_id(call, apps, lease)
    monkeypatch.setenv("CONDUCTOR_COLLAB_MODE", "shadow")
    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=VERDICT)
    assert status == 403
    assert env["code"] == "mode_denied"
    assert store.reviews[review_id]["state"] == "pending"


# ---------------------------------------------------------------------------
# review de-duplication and N59 verdict keys
# ---------------------------------------------------------------------------

_DEDUP_ENV = "CONDUCTOR_DISPATCH_DEDUP_MODE"


@pytest.mark.asyncio
async def test_enforce_mode_repeat_request_returns_the_existing_review(call, store, apps, lease, monkeypatch):
    monkeypatch.setenv(_DEDUP_ENV, "enforce")
    first = await _review_id(call, apps, lease)
    status, env = await _request(call, apps["codex"], lease["lease_id"], reviewer="app:claude-code")
    assert status == 200, env
    validate_tool_output("review_request", env["data"])
    assert env["data"]["review_id"] == first
    assert env["data"]["deduplicated"] is True
    assert len(store.reviews) == 1


@pytest.mark.asyncio
async def test_enforce_mode_force_creates_a_new_review(call, store, apps, lease, monkeypatch):
    monkeypatch.setenv(_DEDUP_ENV, "enforce")
    first = await _review_id(call, apps, lease)
    status, env = await _request(
        call, apps["codex"], lease["lease_id"], reviewer="app:claude-code", force=True
    )
    assert status == 200, env
    assert env["data"]["review_id"] != first
    assert "deduplicated" not in env["data"]
    assert store.review_force_calls == [False, True]


@pytest.mark.asyncio
async def test_shadow_mode_repeat_request_still_creates_a_review(call, store, apps, lease, monkeypatch):
    monkeypatch.delenv(_DEDUP_ENV, raising=False)
    first = await _review_id(call, apps, lease)
    second = await _review_id(call, apps, lease)
    assert first != second and len(store.reviews) == 2


@pytest.mark.asyncio
async def test_review_request_force_must_be_boolean(call, store, apps, lease):
    status, env = await _request(call, apps["codex"], lease["lease_id"], force="yes")
    assert status == 400 and env["code"] == "invalid_schema"


@pytest.mark.asyncio
async def test_review_submit_accepts_n59_status_and_needed_evidence(call, store, apps, lease):
    review_id = await _review_id(call, apps, lease)
    verdict = {**VERDICT, "status": "pass", "needed_evidence": ["ci log"]}
    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=verdict)
    assert status == 200, env
    validate_tool_output("review_submit", env["data"])
    assert env["data"]["verdict"]["status"] == "pass"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"status": "maybe"}, {"needed_evidence": "ci log"}],
    ids=["bad_status", "evidence_not_list"],
)
async def test_review_submit_rejects_bad_n59_keys(call, store, apps, lease, extra):
    review_id = await _review_id(call, apps, lease)
    status, env = await call(
        "review_submit", apps["claude-code"], review_id=review_id, verdict={**VERDICT, **extra}
    )
    assert status == 400 and env["code"] == "invalid_schema"


# ---------------------------------------------------------------------------
# P0 fix: strict review off keeps the closed pre-P0 verdict shape
# ---------------------------------------------------------------------------

_STRICT_ENV = "CONDUCTOR_DISPATCH_REVIEW_STRICT_MODE"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"status": "pass"}, {"needed_evidence": ["ci log"]}])
async def test_off_mode_review_submit_refuses_n59_keys(call, store, apps, lease, monkeypatch, extra):
    monkeypatch.setenv(_STRICT_ENV, "off")
    review_id = await _review_id(call, apps, lease)
    status, env = await call(
        "review_submit", apps["claude-code"], review_id=review_id, verdict={**VERDICT, **extra}
    )
    assert status == 400
    assert env["code"] == "invalid_schema"
    assert store.reviews[review_id]["state"] == "pending"


@pytest.mark.asyncio
async def test_off_mode_review_submit_plain_verdict_still_ok(call, store, apps, lease, monkeypatch):
    monkeypatch.setenv(_STRICT_ENV, "off")
    review_id = await _review_id(call, apps, lease)
    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=VERDICT)
    assert status == 200, env
    assert store.reviews[review_id]["state"] == "completed"


@pytest.mark.asyncio
async def test_shadow_mode_review_submit_accepts_n59_keys(call, store, apps, lease, monkeypatch):
    monkeypatch.setenv(_STRICT_ENV, "shadow")
    review_id = await _review_id(call, apps, lease)
    verdict = {**VERDICT, "status": "pass", "needed_evidence": ["ci log"]}
    status, env = await call("review_submit", apps["claude-code"], review_id=review_id, verdict=verdict)
    assert status == 200, env
    assert env["data"]["verdict"]["needed_evidence"] == ["ci log"]
