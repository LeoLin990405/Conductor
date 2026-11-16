"""``task_report``: the completion contract reconciled with system Git facts (N80 L2).

Every persistence call goes to the in-memory ``FakeStore`` and the Git facts
come from ``store.git_facts`` (see ``conftest.py``); only the last section runs
real Git, against a throwaway repository built inside ``tmp_path``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from datetime import timedelta
from typing import Any

import pytest

from src.collab.schemas import validate_tool_output
from src.server.services.autonomy.completion import completion_report_from_text
from src.server.services.collab import report as report_svc
from tests.unit.services.collab.collab_fakes import now

SCOPES = ["read", "claim", "report"]
LEASE_BRANCH_PREFIX = "conductor/ticket/"


def valid_report(**overrides: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "done",
        "summary": "s",
        "artifacts": {"commits": ["abcdef1"], "files": ["docs/protocol.md"], "pr": ""},
        "verification": [{"ran": "pytest", "result": "passed"}],
        "next": "",
        "handbacks": [],
    }
    report.update(overrides)
    return report


async def claimed(store, call, *, app_id: str = "app-a") -> tuple[str, str, str]:
    """Register an app, add a docs ticket and claim it: ``(token, ticket_id, lease_id)``."""
    token = store.add_app(app_id, scopes=SCOPES)
    ticket = store.add_ticket(write_scope=("docs/**",))
    tid = ticket["ticket_id"]
    status, envelope = await call("task_claim", token, ref=f"t:{tid}", revision=3)
    assert status == 200, envelope
    return token, tid, envelope["data"]["lease_id"]


def report_events(store, tid: str) -> list[dict[str, Any]]:
    return [e for e in store.events_for(tid) if e["detail"].get("collab_event") == "reported"]


def body_of(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["body"])


# ---------------------------------------------------------------------------
# 1. done + delivered + in scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_delivered_in_scope_completes_lease_and_readies_ticket(store, call):
    token, tid, lease_id = await claimed(store, call)
    audit_before = len(store.audit)

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert status == 200, envelope
    data = envelope["data"]
    assert data == {"ticket_state": "ready", "reconciled": {"vetoed": False, "reason": ""}}
    validate_tool_output("task_report", data)
    assert store.leases[lease_id]["state"] == "completed"
    assert store.tickets[tid]["status"] == "ready"

    assert len(store.reports) == 1
    row = store.reports[0]
    assert row["ticket_id"] == tid
    assert row["state"] == "ready"
    assert row["git_check"] is None  # migration 146 allows only NULL here
    body = body_of(row)
    assert body["source"] == "app"
    assert body["report"]["status"] == "done"
    assert body["reconciled"] == {"vetoed": False, "reason": "", "out_of_scope_paths": []}

    events = report_events(store, tid)
    assert len(events) == 1
    detail = events[0]["detail"]
    assert detail["status"] == "done"
    assert detail["ticket_state"] == "ready"
    assert detail["lease_id"] == lease_id
    assert "reason" not in detail
    assert events[0]["to_owner"] == ""

    new_audit = store.audit[audit_before:]
    assert len(new_audit) == 1
    assert new_audit[0]["tool"] == "task_report"
    assert new_audit[0]["code"] == "ok"
    assert new_audit[0]["in_transaction"] is True


# ---------------------------------------------------------------------------
# 2. invalid_schema, never repaired
# ---------------------------------------------------------------------------


def _missing_field() -> dict[str, Any]:
    report = valid_report()
    del report["next"]
    return report


def _extra_field() -> dict[str, Any]:
    return valid_report(extra="x")


def _extra_nested_field() -> dict[str, Any]:
    report = valid_report()
    report["artifacts"]["branch"] = "x"
    return report


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_report",
    [
        pytest.param(_missing_field(), id="missing-field"),
        pytest.param(_extra_field(), id="extra-field"),
        pytest.param(_extra_nested_field(), id="extra-nested-field"),
        pytest.param(valid_report(status="finished"), id="unknown-status"),
        pytest.param(valid_report(summary=7), id="wrong-type-summary"),
        pytest.param(valid_report(handbacks="none"), id="wrong-type-handbacks"),
        pytest.param(valid_report(verification=[{"ran": "pytest"}]), id="verification-missing-result"),
        pytest.param("done", id="report-not-object"),
    ],
)
async def test_invalid_report_is_refused_without_repair(store, call, bad_report):
    token, tid, lease_id = await claimed(store, call)
    ticket_before = copy.deepcopy(store.tickets[tid])
    lease_before = copy.deepcopy(store.leases[lease_id])
    events_before = len(store.events)

    status, envelope = await call("task_report", token, lease_id=lease_id, report=bad_report)

    assert status == 400, envelope
    assert envelope["ok"] is False
    assert envelope["code"] == "invalid_schema"
    assert store.tickets[tid] == ticket_before
    assert store.leases[lease_id] == lease_before
    assert store.reports == []
    assert len(store.events) == events_before
    assert store.audit[-1]["code"] == "invalid_schema"


@pytest.mark.asyncio
async def test_handler_rejects_report_the_contract_refuses(store, call):
    """An over-long item passes the JSON schema but not the N1 contract."""
    token, tid, lease_id = await claimed(store, call)
    huge = valid_report(summary="x" * 200_000)
    assert completion_report_from_text(json.dumps(huge)) is None

    status, envelope = await call("task_report", token, lease_id=lease_id, report=huge)

    assert status == 400, envelope
    assert envelope["code"] == "invalid_schema"
    assert store.tickets[tid]["status"] == "running"
    assert store.leases[lease_id]["state"] == "active"
    assert store.reports == []


# ---------------------------------------------------------------------------
# 3. git_not_delivered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_done_without_new_commit_is_vetoed(store, call):
    store.git_facts = report_svc.GitFacts(
        commit_delivery="missing", changed_paths=(), has_upstream=False
    )
    token, tid, lease_id = await claimed(store, call)

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert status == 200, envelope
    assert envelope["data"] == {
        "ticket_state": "blocked",
        "reconciled": {"vetoed": True, "reason": "git_not_delivered"},
    }
    validate_tool_output("task_report", envelope["data"])
    assert store.tickets[tid]["status"] == "blocked"
    assert store.leases[lease_id]["state"] == "active"
    row = store.reports[-1]
    assert row["state"] == "blocked"
    assert row["git_check"] is None
    assert body_of(row)["reconciled"]["reason"] == "git_not_delivered"
    assert row["question"]
    assert report_events(store, tid)[-1]["detail"]["reason"] == "git_not_delivered"


@pytest.mark.asyncio
async def test_done_with_dirty_workspace_is_vetoed(store, call):
    store.git_facts = report_svc.GitFacts(
        commit_delivery="delivered",
        dirty_summary="The git workspace has uncommitted changes.",
        changed_paths=("docs/protocol.md",),
        has_upstream=False,
    )
    token, tid, lease_id = await claimed(store, call)

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert status == 200, envelope
    assert envelope["data"]["ticket_state"] == "blocked"
    assert envelope["data"]["reconciled"] == {"vetoed": True, "reason": "git_not_delivered"}
    assert store.tickets[tid]["status"] == "blocked"


# ---------------------------------------------------------------------------
# 4. out_of_scope and push_suspected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("claimed_status", ["done", "partial"])
async def test_changed_path_outside_write_scope_is_out_of_scope(store, call, claimed_status):
    store.git_facts = report_svc.GitFacts(
        commit_delivery="delivered",
        changed_paths=("docs/a.md", "src/server/x.py"),
        has_upstream=False,
    )
    token, tid, lease_id = await claimed(store, call)

    status, envelope = await call(
        "task_report", token, lease_id=lease_id, report=valid_report(status=claimed_status)
    )

    assert status == 200, envelope
    assert envelope["data"] == {
        "ticket_state": "blocked",
        "reconciled": {"vetoed": True, "reason": "out_of_scope"},
    }
    assert store.tickets[tid]["status"] == "blocked"
    assert store.leases[lease_id]["state"] == "active"
    events = report_events(store, tid)
    assert events[-1]["detail"]["reason"] == "out_of_scope"
    row = store.reports[-1]
    assert row["git_check"] is None
    assert row["question"] == "System check vetoed the app report: out_of_scope."
    body = body_of(row)
    assert body["reconciled"]["out_of_scope_paths"] == ["src/server/x.py"]
    assert body["reconciled"]["vetoed"] is True


@pytest.mark.asyncio
async def test_head_on_another_branch_is_out_of_scope(store, call):
    store.git_facts = report_svc.GitFacts(
        commit_delivery="delivered",
        changed_paths=("docs/protocol.md",),
        branch="main",
        has_upstream=False,
    )
    token, tid, lease_id = await claimed(store, call)

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert status == 200, envelope
    assert envelope["data"]["ticket_state"] == "blocked"
    assert envelope["data"]["reconciled"]["reason"] == "out_of_scope"
    assert body_of(store.reports[-1])["reconciled"]["out_of_scope_paths"] == []


@pytest.mark.asyncio
async def test_head_on_lease_branch_is_in_scope(store, call):
    token = store.add_app("app-a", scopes=SCOPES)
    ticket = store.add_ticket()
    tid = ticket["ticket_id"]
    store.git_facts = report_svc.GitFacts(
        commit_delivery="delivered",
        changed_paths=("docs/protocol.md",),
        branch=f"{LEASE_BRANCH_PREFIX}{tid}",
        has_upstream=False,
    )
    _, claim = await call("task_claim", token, ref=f"t:{tid}", revision=3)
    lease_id = claim["data"]["lease_id"]

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert status == 200, envelope
    assert envelope["data"]["ticket_state"] == "ready"


@pytest.mark.asyncio
async def test_upstream_on_lease_branch_is_push_suspected(store, call):
    store.git_facts = report_svc.GitFacts(
        commit_delivery="delivered",
        changed_paths=("docs/protocol.md",),
        has_upstream=True,
    )
    token, tid, lease_id = await claimed(store, call)

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert status == 200, envelope
    assert envelope["data"] == {
        "ticket_state": "blocked",
        "reconciled": {"vetoed": True, "reason": "push_suspected"},
    }
    assert store.tickets[tid]["status"] == "blocked"
    assert store.reports[-1]["question"] == "System check vetoed the app report: push_suspected."


# ---------------------------------------------------------------------------
# 5. non-done statuses keep the lease
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_waits_and_renews_the_lease(store, call):
    token, tid, lease_id = await claimed(store, call)
    store.leases[lease_id]["expires_at"] = now() + timedelta(seconds=60)

    status, envelope = await call(
        "task_report", token, lease_id=lease_id, report=valid_report(status="partial")
    )

    assert status == 200, envelope
    assert envelope["data"] == {
        "ticket_state": "waiting",
        "reconciled": {"vetoed": False, "reason": ""},
    }
    lease = store.leases[lease_id]
    assert lease["state"] == "active"
    assert lease["expires_at"] > now() + timedelta(seconds=600)
    assert store.tickets[tid]["status"] == "waiting"
    assert report_events(store, tid)[-1]["to_owner"] == "app:app-a"


@pytest.mark.asyncio
async def test_blocked_report_blocks_and_keeps_lease(store, call):
    token, tid, lease_id = await claimed(store, call)

    status, envelope = await call(
        "task_report",
        token,
        lease_id=lease_id,
        report=valid_report(status="blocked", next="Which API version?"),
    )

    assert status == 200, envelope
    assert envelope["data"]["ticket_state"] == "blocked"
    assert envelope["data"]["reconciled"]["vetoed"] is False
    assert store.leases[lease_id]["state"] == "active"
    assert store.tickets[tid]["status"] == "blocked"
    assert store.reports[-1]["question"] == "Which API version?"


@pytest.mark.asyncio
async def test_needs_review_waits_and_opens_native_review_then_done_still_works(store, call):
    token, tid, lease_id = await claimed(store, call)

    status, envelope = await call(
        "task_report", token, lease_id=lease_id, report=valid_report(status="needs_review")
    )

    assert status == 200, envelope
    assert envelope["data"]["ticket_state"] == "waiting"
    assert store.leases[lease_id]["state"] == "active"
    assert store.tickets[tid]["status"] == "waiting"
    reviews = list(store.reviews.values())
    assert len(reviews) == 1
    review = reviews[0]
    assert review["state"] == "pending"
    assert review["reviewer_target"] == "native"
    assert review["commit_sha"] == "abcdef1"
    assert review["lease_id"] == lease_id
    assert review["target_ref"] == f"t:{tid}"
    assert review["author_app_id"] == "app-a"

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert status == 200, envelope
    assert envelope["data"]["ticket_state"] == "ready"
    assert store.leases[lease_id]["state"] == "completed"
    assert store.tickets[tid]["status"] == "ready"
    assert len(store.reports) == 2
    assert len(report_events(store, tid)) == 2


@pytest.mark.asyncio
async def test_needs_review_without_hex_commit_opens_no_review(store, call):
    token, _tid, lease_id = await claimed(store, call)
    report = valid_report(status="needs_review")
    report["artifacts"]["commits"] = ["not-a-sha"]

    status, envelope = await call("task_report", token, lease_id=lease_id, report=report)

    assert status == 200, envelope
    assert store.reviews == {}


@pytest.mark.asyncio
async def test_report_after_completion_is_lease_lost(store, call):
    token, _tid, lease_id = await claimed(store, call)
    await call("task_report", token, lease_id=lease_id, report=valid_report())

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert status != 200
    assert envelope["code"] == "lease_lost"
    assert len(store.reports) == 1


# ---------------------------------------------------------------------------
# 6. after the lease is lost
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_report_on_another_apps_lease_is_lease_lost(store, call):
    _token, tid, lease_id = await claimed(store, call)
    other = store.add_app("app-b", scopes=SCOPES)

    status, envelope = await call("task_report", other, lease_id=lease_id, report=valid_report())

    assert envelope["code"] == "lease_lost", envelope
    assert status != 200
    assert store.tickets[tid]["status"] == "running"
    assert store.leases[lease_id]["state"] == "active"
    assert store.reports == []


@pytest.mark.asyncio
async def test_report_on_expired_lease_is_lease_expired(store, call):
    token, tid, lease_id = await claimed(store, call)
    store.leases[lease_id]["expires_at"] = now() - timedelta(seconds=1)

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert envelope["code"] == "lease_expired", envelope
    assert status != 200
    assert store.tickets[tid]["status"] == "running"
    assert store.reports == []


@pytest.mark.asyncio
async def test_report_on_handoff_pending_lease_is_lease_lost(store, call):
    token, tid, lease_id = await claimed(store, call)
    status, envelope = await call(
        "handoff_request", token, lease_id=lease_id, reason="context_full", to="any"
    )
    assert status == 200, envelope
    assert store.leases[lease_id]["state"] == "handoff_pending"

    status, envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert envelope["code"] == "lease_lost", envelope
    assert status != 200
    assert store.leases[lease_id]["state"] == "handoff_pending"
    assert store.tickets[tid]["status"] == "running"
    assert store.reports == []


@pytest.mark.asyncio
async def test_report_on_unknown_lease_is_not_found(store, call):
    token, _tid, _lease_id = await claimed(store, call)

    status, envelope = await call(
        "task_report",
        token,
        lease_id="00000000-0000-0000-0000-000000000000",
        report=valid_report(),
    )

    assert envelope["code"] == "not_found", envelope
    assert status == 404


@pytest.mark.asyncio
async def test_audit_failure_rolls_report_back(store, call):
    token, tid, lease_id = await claimed(store, call)
    store.fail_audit_for = {"ok"}

    status, _envelope = await call("task_report", token, lease_id=lease_id, report=valid_report())

    assert status != 200
    assert store.reports == []
    assert store.tickets[tid]["status"] == "running"
    assert store.leases[lease_id]["state"] == "active"
    assert report_events(store, tid) == []


# ---------------------------------------------------------------------------
# 7. scope_violations and reconcile, directly
# ---------------------------------------------------------------------------


def test_scope_violations_empty_scope_permits_nothing():
    assert report_svc.scope_violations(["docs/a.md"], []) == ("docs/a.md",)
    assert report_svc.scope_violations(["docs/a.md"], [""]) == ("docs/a.md",)
    assert report_svc.scope_violations([], []) == ()


@pytest.mark.parametrize(
    ("path", "scope", "allowed"),
    [
        ("docs/a.md", ["docs/**"], True),
        ("docs/deep/nested/b.md", ["docs/**"], True),
        ("src/server/x.py", ["docs/**"], False),
        ("docsx/a.md", ["docs/**"], False),
        ("docs/a.md", ["docs/*.md"], True),
        ("docs/sub/a.md", ["docs/*.md"], False),
        ("pkg/sub/test_x.py", ["**/test_*.py"], True),
        ("README.md", ["README.md"], True),
        ("README.md", ["docs/**", "README.md"], True),
        ("./docs/a.md", ["docs/**"], True),
    ],
)
def test_scope_violations_globs(path, scope, allowed):
    assert report_svc.scope_violations([path], scope) == (() if allowed else (path,))


def _parsed(**overrides: Any):
    report = completion_report_from_text(json.dumps(valid_report(**overrides)))
    assert report is not None
    return report


DELIVERED = report_svc.GitFacts(
    commit_delivery="delivered", changed_paths=("docs/a.md",), branch="lb", has_upstream=False
)


@pytest.mark.parametrize(
    ("status", "facts", "expected"),
    [
        ("done", DELIVERED, ("ready", "")),
        ("partial", DELIVERED, ("waiting", "")),
        ("blocked", DELIVERED, ("blocked", "")),
        ("needs_review", DELIVERED, ("waiting", "")),
        ("needs_split", DELIVERED, ("waiting", "")),
        ("done", report_svc.GitFacts(commit_delivery="missing"), ("blocked", "git_not_delivered")),
        ("done", report_svc.GitFacts(commit_delivery="unknown"), ("blocked", "git_not_delivered")),
        (
            "done",
            report_svc.GitFacts(commit_delivery="delivered", dirty_summary="dirty"),
            ("blocked", "git_not_delivered"),
        ),
        # Unreadable changed paths cannot be shown to fit the write scope.
        ("done", report_svc.GitFacts(commit_delivery="not_checked"), ("blocked", "out_of_scope")),
        (
            "done",
            report_svc.GitFacts(commit_delivery="delivered", changed_paths=("src/x.py",)),
            ("blocked", "out_of_scope"),
        ),
        (
            "blocked",
            report_svc.GitFacts(commit_delivery="delivered", changed_paths=("src/x.py",)),
            ("blocked", "out_of_scope"),
        ),
        (
            "done",
            report_svc.GitFacts(commit_delivery="delivered", branch="main"),
            ("blocked", "out_of_scope"),
        ),
        (
            "done",
            report_svc.GitFacts(commit_delivery="delivered", has_upstream=True),
            ("blocked", "push_suspected"),
        ),
        # Scope wins over a suspected push, and over a missing commit.
        (
            "done",
            report_svc.GitFacts(
                commit_delivery="missing", changed_paths=("src/x.py",), has_upstream=True
            ),
            ("blocked", "out_of_scope"),
        ),
        # A non-done claim that lists commits Git refutes is flagged, not moved.
        ("partial", report_svc.GitFacts(commit_delivery="missing"), ("waiting", "git_not_delivered")),
    ],
)
def test_reconcile(status, facts, expected):
    outcome = report_svc.reconcile(
        _parsed(status=status), facts, write_scope=["docs/**"], lease_branch="lb"
    )
    assert (outcome.ticket_state, outcome.reason) == expected


def test_reconcile_reports_violations_and_ignores_branch_without_lease_branch():
    facts = report_svc.GitFacts(
        commit_delivery="delivered", changed_paths=("docs/a.md", "src/x.py", "b.txt"), branch="other"
    )
    outcome = report_svc.reconcile(_parsed(), facts, write_scope=["docs/**"], lease_branch="")
    assert outcome == report_svc.Reconciliation("blocked", "out_of_scope", ("src/x.py", "b.txt"))

    in_scope = report_svc.GitFacts(
        commit_delivery="delivered", changed_paths=("docs/a.md",), branch="other"
    )
    outcome = report_svc.reconcile(_parsed(), in_scope, write_scope=["docs/**"], lease_branch="")
    assert outcome == report_svc.Reconciliation("ready", "")


def test_reconcile_empty_scope_vetoes_any_change():
    facts = report_svc.GitFacts(commit_delivery="delivered", changed_paths=("docs/a.md",))
    outcome = report_svc.reconcile(_parsed(), facts, write_scope=[], lease_branch="")
    assert (outcome.ticket_state, outcome.reason) == ("blocked", "out_of_scope")


@pytest.mark.asyncio
async def test_collect_git_facts_without_worktree_is_not_checked():
    facts = await report_svc.collect_git_facts({"worktree_path": "", "worktree_base_sha": ""})
    assert facts == report_svc.GitFacts(commit_delivery="not_checked")


def test_git_facts_sync_rejects_short_base_without_running_git(tmp_path):
    facts = report_svc._git_facts_sync(str(tmp_path / "missing"), "abc")
    assert facts.commit_delivery == "unknown"
    assert facts.dirty_summary


# ---------------------------------------------------------------------------
# 8. _git_facts_sync against a throwaway repository in tmp_path
# ---------------------------------------------------------------------------


def _git(repo, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(repo.parent),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "core.fsmonitor=false", "-c", "commit.gpgsign=false", *args],
        check=True,
        capture_output=True,
        env=env,
        timeout=30,
    )
    return result.stdout.decode().strip()


@pytest.fixture
def temp_repo(tmp_path):
    """A repo in tmp_path: base commit on main, one docs commit on the lease branch."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "conductor/ticket/t1")
    (repo / "docs").mkdir()
    (repo / "docs" / "a.md").write_text("doc\n", encoding="utf-8")
    _git(repo, "add", "docs/a.md")
    _git(repo, "commit", "-q", "-m", "docs")
    head = _git(repo, "rev-parse", "HEAD")

    from src.server.services.autonomy import git_delivery as gd

    try:
        probe = gd.run_isolated_git_readonly(str(repo), ["rev-parse", "HEAD"], timeout=10)
    except Exception as exc:  # noqa: BLE001 - any isolation refusal means skip
        pytest.skip(f"isolated read-only git is unavailable here: {type(exc).__name__}: {exc}")
    if probe.returncode != 0 or probe.stdout.decode().strip() != head:
        pytest.skip("isolated read-only git did not read the temporary repository")
    return repo, base, head


def test_git_facts_sync_delivered_with_changed_paths(temp_repo):
    repo, base, _head = temp_repo
    facts = report_svc._git_facts_sync(str(repo), base)
    assert facts == report_svc.GitFacts(
        commit_delivery="delivered",
        dirty_summary=None,
        changed_paths=("docs/a.md",),
        branch="conductor/ticket/t1",
        has_upstream=False,
    )


def test_git_facts_sync_missing_when_head_is_base(temp_repo):
    repo, _base, head = temp_repo
    facts = report_svc._git_facts_sync(str(repo), head)
    assert facts.commit_delivery == "missing"
    assert facts.changed_paths == ()
    assert facts.dirty_summary is None


def test_git_facts_sync_dirty_adds_untracked_path(temp_repo):
    repo, base, _head = temp_repo
    (repo / "src").mkdir()
    (repo / "src" / "x.py").write_text("x = 1\n", encoding="utf-8")
    facts = report_svc._git_facts_sync(str(repo), base)
    assert facts.commit_delivery == "delivered"
    assert facts.dirty_summary == "The git workspace has uncommitted changes."
    assert facts.changed_paths == ("docs/a.md", "src/x.py")
    outcome = report_svc.reconcile(
        _parsed(), facts, write_scope=["docs/**"], lease_branch="conductor/ticket/t1"
    )
    assert (outcome.ticket_state, outcome.reason, outcome.violations) == (
        "blocked",
        "out_of_scope",
        ("src/x.py",),
    )


def test_git_facts_sync_dirty_staged_rename_lists_new_path(temp_repo):
    """A staged rename out of scope must surface the new path for the scope check."""
    repo, base, _head = temp_repo
    _git(repo, "mv", "docs/a.md", "README2.md")
    facts = report_svc._git_facts_sync(str(repo), base)
    assert facts.dirty_summary == "The git workspace has uncommitted changes."
    assert facts.changed_paths is not None
    assert "README2.md" in facts.changed_paths
    assert report_svc.scope_violations(facts.changed_paths, ["docs/**"]) == ("README2.md",)


def test_git_facts_sync_dirty_path_with_space_is_unquoted(temp_repo):
    repo, base, _head = temp_repo
    (repo / "docs" / "sp ace.md").write_text("x\n", encoding="utf-8")
    facts = report_svc._git_facts_sync(str(repo), base)
    assert facts.changed_paths == ("docs/a.md", "docs/sp ace.md")


# ---------------------------------------------------------------------------
# 9. committed renames, deletions, copies and empty commits (security review)
# ---------------------------------------------------------------------------

LEASE = "conductor/ticket/t1"


def _reconciled(repo, base: str) -> report_svc.Reconciliation:
    facts = report_svc._git_facts_sync(str(repo), base)
    return report_svc.reconcile(_parsed(), facts, write_scope=["docs/**"], lease_branch=LEASE)


def test_committed_rename_from_outside_scope_is_out_of_scope(temp_repo):
    """``git mv README.md docs/README.md`` removes a path outside the scope."""
    repo, base, _head = temp_repo
    _git(repo, "mv", "README.md", "docs/README.md")
    _git(repo, "commit", "-q", "-m", "move")
    facts = report_svc._git_facts_sync(str(repo), base)
    assert facts.dirty_summary is None
    assert facts.changed_paths is not None
    assert set(facts.changed_paths) == {"README.md", "docs/README.md", "docs/a.md"}
    outcome = _reconciled(repo, base)
    assert (outcome.ticket_state, outcome.reason, outcome.violations) == (
        "blocked", "out_of_scope", ("README.md",)
    )


def test_committed_rename_is_out_of_scope_with_copy_detection_configured(temp_repo):
    """The repository's own ``diff.renames=copies`` cannot turn detection back on."""
    repo, base, _head = temp_repo
    _git(repo, "config", "diff.renames", "copies")
    _git(repo, "mv", "README.md", "docs/README.md")
    _git(repo, "commit", "-q", "-m", "move")
    outcome = _reconciled(repo, base)
    assert (outcome.ticket_state, outcome.reason, outcome.violations) == (
        "blocked", "out_of_scope", ("README.md",)
    )


def test_committed_copy_and_edit_lists_both_paths(temp_repo):
    repo, base, _head = temp_repo
    _git(repo, "config", "diff.renames", "copies")
    (repo / "docs" / "copy.md").write_text("base\n", encoding="utf-8")
    (repo / "README.md").write_text("base\nedited\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "copy")
    facts = report_svc._git_facts_sync(str(repo), base)
    assert facts.changed_paths is not None
    assert {"README.md", "docs/copy.md"} <= set(facts.changed_paths)
    assert _reconciled(repo, base).violations == ("README.md",)


def test_committed_delete_outside_scope_is_out_of_scope(temp_repo):
    repo, base, _head = temp_repo
    _git(repo, "rm", "-q", "README.md")
    _git(repo, "commit", "-q", "-m", "delete")
    outcome = _reconciled(repo, base)
    assert (outcome.ticket_state, outcome.reason, outcome.violations) == (
        "blocked", "out_of_scope", ("README.md",)
    )


def test_empty_commit_is_not_delivered(temp_repo):
    """``git commit --allow-empty`` adds a commit and changes nothing."""
    repo, base, _head = temp_repo
    _git(repo, "checkout", "-q", "-b", "conductor/ticket/t2", base)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "nothing")
    facts = report_svc._git_facts_sync(str(repo), base)
    assert (facts.commit_delivery, facts.changed_paths) == ("delivered", ())
    outcome = report_svc.reconcile(
        _parsed(), facts, write_scope=["docs/**"], lease_branch="conductor/ticket/t2"
    )
    assert (outcome.ticket_state, outcome.reason) == ("blocked", "git_not_delivered")


@pytest.mark.parametrize("status", ["done", "partial"])
def test_reconcile_delivered_without_changed_paths_is_not_delivered(status):
    facts = report_svc.GitFacts(commit_delivery="delivered", changed_paths=(), branch="lb")
    outcome = report_svc.reconcile(
        _parsed(status=status), facts, write_scope=["docs/**"], lease_branch="lb"
    )
    assert outcome.reason == "git_not_delivered"
    assert outcome.ticket_state != "ready"


def test_committed_paths_command_is_the_allowlisted_form():
    from src.server.services.autonomy import git_delivery as gd

    command = report_svc.committed_paths_command("a" * 40, "b" * 40)
    assert "--no-renames" in command
    assert gd._public_git_command_is_safe(command)
    assert gd._is_listing_command(command)
