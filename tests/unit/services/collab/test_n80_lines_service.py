"""N80 L4, service side: apps claim, heartbeat, release and report dispatch lines.

Pure tests over a synthetic ``Dispatch`` plus a fake ``DispatchService`` for
the async wrappers. No database, no network, no git process (``git_facts``
is a fake), HOME is a temporary directory (the package conftest).
"""

from __future__ import annotations

import asyncio
import copy
from typing import Any

import pytest

from src.server.services.collab import lines as svc
from src.tools.auto_dispatch import app_lines
from src.tools.auto_dispatch.types import Dispatch, Line, LineStatus

FULL = {"CONDUCTOR_COLLAB_MODE": "full"}
NOW = 1_000_000.0
BASE = "a" * 40
HEAD = "c" * 40
GOOD_FACTS = (("docs/a.md",), False, True)


def _line(line_id: str = "L1", **over: Any) -> Line:
    fields: dict[str, Any] = {
        "id": line_id,
        "name": line_id,
        "task": f"do {line_id}",
        "branch": f"feature/auto-dispatch-d1/line-{line_id}",
        "base_commit": BASE,
        "write_scope": ("docs/**",),
        "app_claimable": True,
        "status": LineStatus.PENDING,
    }
    fields.update(over)
    return Line(**fields)


def _dispatch(*lines: Line) -> Dispatch:
    return Dispatch(
        id="d1",
        goal="g",
        user_id="u1",
        created_at="2026-11-14T00:00:00Z",
        lines=list(lines or (_line(),)),
    )


def _claim(dispatch: Dispatch, line_id: str = "L1", **over: Any) -> svc.LineOutcome:
    kwargs: dict[str, Any] = {
        "app_id": "claude-code",
        "agent": "opus",
        "can_write": True,
        "enabled": True,
        "live_leases": 0,
        "max_leases": 1,
        "now": NOW,
        "env": FULL,
    }
    kwargs.update(over)
    return svc.claim_line(dispatch, line_id, **kwargs)


def _claimed(**over: Any) -> Dispatch:
    dispatch = _dispatch(**over) if over else _dispatch()
    assert _claim(dispatch).ok
    return dispatch


def _report(dispatch: Dispatch, *, facts: Any = GOOD_FACTS, **over: Any) -> svc.LineOutcome:
    kwargs: dict[str, Any] = {
        "app_id": "claude-code",
        "status": "done",
        "head": HEAD,
        "summary": "did it",
        "files": ("docs/a.md",),
        "now": NOW + 60,
        "git_facts": (lambda line, item, head: facts),
        "env": FULL,
    }
    kwargs.update(over)
    return svc.report_line(dispatch, "L1", **kwargs)


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def test_claim_writes_active_cell_with_audit():
    dispatch = _dispatch()
    outcome = _claim(dispatch)
    assert outcome.ok and outcome.code is None
    item = app_lines.cell(dispatch, "L1")
    assert item is not None and item["state"] == app_lines.ACTIVE
    assert item["app_id"] == "claude-code"
    assert item["author"] == "app:claude-code"
    assert item["expires_at"] == NOW + app_lines.LEASE_SECONDS
    assert item["max_lease_until"] == NOW + app_lines.MAX_LEASE_SECONDS
    audit = item["assignment_audit"]
    assert len(audit) == 1 and audit[0]["assigned_by"] == "app_claim"
    assert audit[0]["feasibility"]["status"] == "ok"
    assert set(audit[0]["skipped"].values()) == {"interactive_app"}
    assert "reserved_role" in audit[0]["skipped"]
    # Line.assignment_audit keeps its type and value.
    assert dispatch.line("L1").assignment_audit is None
    assert outcome.detail["ref"] == "d:d1:L1"
    assert outcome.detail["rules"]


@pytest.mark.parametrize("mode", [None, "off", "shadow", "docs", "bogus"])
def test_claim_mode_denied_unless_full(mode):
    dispatch = _dispatch(_line(write_scope=("docs/a.md",)))
    env = {} if mode is None else {"CONDUCTOR_COLLAB_MODE": mode}
    outcome = _claim(dispatch, env=env)
    assert outcome.code == "mode_denied"
    assert outcome.envelope_code == "mode_denied"
    assert app_lines.cells(dispatch) == {}


def _assert_refused(dispatch: Dispatch, outcome: svc.LineOutcome, code: str) -> None:
    before = copy.deepcopy(dispatch.gates)
    assert not outcome.ok
    assert outcome.code == code
    assert dispatch.gates == before


def test_claim_refusals_from_line_state():
    dispatch = _dispatch()
    before = copy.deepcopy(dispatch.gates)
    assert _claim(dispatch, "nope").code == "no_such_line"
    assert dispatch.gates == before

    unmarked = _dispatch(_line(app_claimable=False))
    _assert_refused(unmarked, _claim(unmarked), "not_app_claimable")

    running = _dispatch(_line(status=LineStatus.RUNNING))
    _assert_refused(running, _claim(running), "line_not_ready")

    started = _dispatch(_line(current_run="run-1"))
    _assert_refused(started, _claim(started), "line_not_ready")

    deps = _dispatch(_line("L0", app_claimable=False), _line(depends_on=("L0",)))
    _assert_refused(deps, _claim(deps), "dependencies_not_passed")


def test_claim_window_closed():
    dispatch = _dispatch()
    app_lines.mark(dispatch, "L1", app_lines.OPEN, now=NOW - 10_000)
    outcome = _claim(dispatch, env={**FULL, app_lines.WINDOW_ENV: "60"})
    assert outcome.code == "claim_window_closed"
    lapsed = _dispatch()
    app_lines.mark(lapsed, "L1", app_lines.LAPSED, now=NOW)
    assert _claim(lapsed).code == "claim_window_closed"


def test_claim_inside_open_window_succeeds():
    dispatch = _dispatch()
    app_lines.mark(dispatch, "L1", app_lines.OPEN, now=NOW - 5)
    outcome = _claim(dispatch)
    assert outcome.ok
    assert app_lines.cell(dispatch, "L1")["open_since"] == NOW - 5


def test_claim_cas_second_app_gets_already_claimed():
    dispatch = _dispatch()
    assert _claim(dispatch).ok
    before = copy.deepcopy(dispatch.gates)
    second = _claim(dispatch, app_id="codex")
    assert second.code == "already_claimed"
    assert second.envelope_code == "already_claimed"
    assert dispatch.gates == before
    assert app_lines.cell(dispatch, "L1")["app_id"] == "claude-code"


def test_claim_stale_revision():
    dispatch = _dispatch()
    app_lines.mark(dispatch, "L1", app_lines.OPEN, now=NOW, revision=2)
    _assert_refused(dispatch, _claim(dispatch, revision=1), "stale_revision")
    assert _claim(dispatch, revision=2).ok


def test_claim_feasibility_refusals():
    read_only = _dispatch()
    _assert_refused(read_only, _claim(read_only, can_write=False), "cannot_take_line")

    disabled = _dispatch()
    outcome = _claim(disabled, enabled=False)
    _assert_refused(disabled, outcome, "disabled")
    assert outcome.envelope_code == "assignment_refused"

    reviewer = _dispatch()
    reviewer.gates["lines"] = {"L1": {"secondary_reviewer": "app:Claude-Code/sonnet"}}
    _assert_refused(reviewer, _claim(reviewer), "is_line_reviewer")

    fallback = _dispatch()
    fallback.gates["lines"] = {"L1": {"review_fallback": {"reviewer": "app:claude-code"}}}
    _assert_refused(fallback, _claim(fallback), "is_line_reviewer")
    # A bare provider name is a native reviewer (the CLI), not the app.
    native = _dispatch()
    native.gates["lines"] = {"L1": {"review_fallback": {"reviewer": "claude-code"}}}
    assert _claim(native).ok

    reserved = _dispatch()
    _assert_refused(reserved, _claim(reserved, app_id="codex", reserved_role=True), "reserved_role")
    # P5: the reserved-author rule is skipped for apps by default.
    assert _claim(_dispatch(), app_id="codex").ok


@pytest.mark.parametrize(
    ("live", "limit"), [(1, 1), (3, 3), ([object()], 1), (0, 0), (True, 3)]
)
def test_claim_max_leases(live, limit):
    dispatch = _dispatch()
    _assert_refused(dispatch, _claim(dispatch, live_leases=live, max_leases=limit), "max_leases")


def test_claim_order_mode_before_everything():
    dispatch = _dispatch(_line(app_claimable=False))
    assert _claim(dispatch, env={}, can_write=False, max_leases=0).code == "mode_denied"
    assert _claim(dispatch, can_write=False, max_leases=0).code == "not_app_claimable"
    ok_line = _dispatch()
    assert _claim(ok_line, can_write=False, max_leases=0).code == "cannot_take_line"


def test_app_line_lease_count_counts_live_cells():
    first = _claimed()
    other = _dispatch()
    assert _claim(other, app_id="codex").ok
    assert svc.app_line_lease_count([first, other], "Claude-Code") == 1
    assert svc.app_line_lease_count([first, other], "codex") == 1
    app_lines.release(app_lines.cell(first, "L1"), now=NOW)
    assert svc.app_line_lease_count([first], "claude-code") == 0


# ---------------------------------------------------------------------------
# heartbeat / release
# ---------------------------------------------------------------------------


def test_heartbeat_extends_and_caps_at_max_lease_until():
    dispatch = _claimed()
    outcome = svc.heartbeat_line(dispatch, "L1", app_id="claude-code", now=NOW + 60, env=FULL)
    assert outcome.ok
    item = app_lines.cell(dispatch, "L1")
    assert item["expires_at"] == NOW + 60 + app_lines.LEASE_SECONDS
    late = NOW + app_lines.MAX_LEASE_SECONDS - 30
    # Keep the lease alive up to the cap with regular heartbeats.
    item["expires_at"] = late + 10
    assert svc.heartbeat_line(dispatch, "L1", app_id="claude-code", now=late, env=FULL).ok
    assert item["expires_at"] == item["max_lease_until"] == NOW + app_lines.MAX_LEASE_SECONDS
    after = svc.heartbeat_line(
        dispatch, "L1", app_id="claude-code", now=NOW + app_lines.MAX_LEASE_SECONDS, env=FULL
    )
    assert after.code == "lease_expired"


def test_heartbeat_refusals():
    dispatch = _claimed()
    assert svc.heartbeat_line(dispatch, "L1", app_id="codex", now=NOW, env=FULL).code == "lease_lost"
    assert svc.heartbeat_line(dispatch, "L1", app_id="", now=NOW, env=FULL).code == "lease_lost"
    assert svc.heartbeat_line(dispatch, "nope", app_id="claude-code", now=NOW, env=FULL).code == "no_such_line"
    expired = svc.heartbeat_line(
        dispatch, "L1", app_id="claude-code", now=NOW + app_lines.LEASE_SECONDS, env=FULL
    )
    assert expired.code == "lease_expired" and expired.envelope_code == "lease_expired"
    unclaimed = _dispatch()
    assert svc.heartbeat_line(unclaimed, "L1", app_id="claude-code", now=NOW, env=FULL).code == "lease_lost"


def test_release_marks_released():
    dispatch = _claimed()
    assert svc.release_line(dispatch, "L1", app_id="codex", reason="x", now=NOW, env=FULL).code == "lease_lost"
    assert svc.release_line(dispatch, "L1", app_id="claude-code", reason=" ", now=NOW, env=FULL).code == "invalid_schema"
    outcome = svc.release_line(
        dispatch, "L1", app_id="claude-code", reason="quota_exhausted", now=NOW + 5, env=FULL
    )
    assert outcome.ok
    item = app_lines.cell(dispatch, "L1")
    assert item["state"] == app_lines.RELEASED and item["end_reason"] == "quota_exhausted"
    again = svc.release_line(dispatch, "L1", app_id="claude-code", reason="x", now=NOW, env=FULL)
    assert again.code == "lease_lost"


@pytest.mark.parametrize("mode", ["off", "shadow", "docs"])
def test_every_verb_mode_denied_outside_full(mode):
    dispatch = _claimed()
    before = copy.deepcopy(dispatch.gates)
    env = {"CONDUCTOR_COLLAB_MODE": mode}
    assert _claim(_dispatch(), env=env).code == "mode_denied"
    assert svc.heartbeat_line(dispatch, "L1", app_id="claude-code", now=NOW, env=env).code == "mode_denied"
    assert svc.release_line(dispatch, "L1", app_id="claude-code", reason="r", now=NOW, env=env).code == "mode_denied"
    assert _report(dispatch, env=env).code == "mode_denied"
    assert _report(dispatch, env=env, status="partial").code == "mode_denied"
    assert dispatch.gates == before


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def test_report_done_records_reported_cell_with_system_paths():
    dispatch = _claimed()
    outcome = _report(dispatch, files=("ignored/by/system.py",))
    assert outcome.ok
    item = app_lines.cell(dispatch, "L1")
    assert item["state"] == app_lines.REPORTED
    assert item["report"]["status"] == "done"
    assert item["report"]["head"] == HEAD
    assert item["report"]["files"] == ["docs/a.md"]
    assert item["report"]["app_files"] == ["ignored/by/system.py"]
    # The machine adopts a done report into G2 on its next step.
    steps = app_lines.steps(dispatch, now=NOW + 61, env=FULL)
    assert [(step.kind, step.line_id) for step in steps] == [("adopt", "L1")]


@pytest.mark.parametrize("status", ["blocked", "needs_split"])
def test_report_other_statuses_go_to_release(status):
    dispatch = _claimed()
    assert _report(dispatch, status=status).ok
    steps = app_lines.steps(dispatch, now=NOW + 61, env=FULL)
    assert [step.kind for step in steps] == ["release"]


def _assert_report_refused(dispatch: Dispatch, outcome: svc.LineOutcome, code: str) -> None:
    assert outcome.code == code, outcome
    item = app_lines.cell(dispatch, "L1")
    assert item["state"] == app_lines.ACTIVE  # the claim stays active
    assert "report" not in item


@pytest.mark.parametrize("status", ["needs_review", "DONE", "", None, 3])
def test_report_invalid_status(status):
    dispatch = _claimed()
    _assert_report_refused(dispatch, _report(dispatch, status=status), "invalid_schema")


@pytest.mark.parametrize("head", ["", "abc", "C" * 40, "g" * 40])
def test_report_invalid_head(head):
    dispatch = _claimed()
    _assert_report_refused(dispatch, _report(dispatch, head=head), "invalid_schema")


def test_report_out_of_scope():
    dispatch = _claimed()
    outcome = _report(dispatch, facts=(("docs/a.md", "src/evil.py"), False, True))
    _assert_report_refused(dispatch, outcome, "out_of_scope")
    assert outcome.detail["paths"] == ["src/evil.py"]
    unknown = _report(dispatch, facts=(None, False, True))
    _assert_report_refused(dispatch, unknown, "out_of_scope")


def test_report_empty_write_scope_permits_nothing():
    dispatch = _dispatch(_line(write_scope=()))
    assert _claim(dispatch).ok
    _assert_report_refused(dispatch, _report(dispatch), "out_of_scope")


@pytest.mark.parametrize("dirty", [True, None])
def test_report_worktree_dirty(dirty):
    dispatch = _claimed()
    _assert_report_refused(dispatch, _report(dispatch, facts=(("docs/a.md",), dirty, True)), "worktree_dirty")


@pytest.mark.parametrize("on_branch", [False, None])
def test_report_head_not_on_branch(on_branch):
    dispatch = _claimed()
    outcome = _report(dispatch, facts=(("docs/a.md",), False, on_branch))
    _assert_report_refused(dispatch, outcome, "git_not_delivered")


def test_report_done_without_change_is_not_delivered():
    dispatch = _claimed()
    _assert_report_refused(dispatch, _report(dispatch, facts=((), False, True)), "git_not_delivered")
    _assert_report_refused(dispatch, _report(dispatch, head=BASE), "git_not_delivered")
    # blocked without commits is still a valid report.
    assert _report(dispatch, status="blocked", head=BASE, facts=((), False, True)).ok


def test_report_git_facts_failure_fails_closed():
    dispatch = _claimed()

    def broken(line, item, head):
        raise RuntimeError("git exploded")

    outcome = _report(dispatch, git_facts=broken)
    _assert_report_refused(dispatch, outcome, "git_not_delivered")
    bad_shape = _report(dispatch, facts="nope")
    _assert_report_refused(dispatch, bad_shape, "git_not_delivered")


def test_report_partial_is_heartbeat_only():
    dispatch = _claimed()
    calls: list[Any] = []

    def facts(line, item, head):
        calls.append(head)
        return GOOD_FACTS

    outcome = _report(dispatch, status="partial", head="", git_facts=facts, now=NOW + 100)
    assert outcome.ok and outcome.detail["heartbeat_only"] is True
    item = app_lines.cell(dispatch, "L1")
    assert item["state"] == app_lines.ACTIVE and "report" not in item
    assert item["expires_at"] == NOW + 100 + app_lines.LEASE_SECONDS
    assert calls == []


def test_report_holder_checks():
    dispatch = _claimed()
    _assert_report_refused(dispatch, _report(dispatch, app_id="codex"), "lease_lost")
    expired = _report(dispatch, now=NOW + app_lines.LEASE_SECONDS)
    _assert_report_refused(dispatch, expired, "lease_expired")
    assert _report(dispatch).ok
    # A second report on a reported cell: the lease is no longer active.
    assert _report(dispatch).code == "lease_lost"


def test_subprocess_git_facts_without_worktree_is_unknown(tmp_path):
    line = _line()
    assert svc.subprocess_git_facts(line, {}, HEAD) == (None, None, None)
    assert svc.subprocess_git_facts(line, {"worktree": "relative/path"}, HEAD) == (None, None, None)
    bad_branch = _line(branch="-x")
    assert svc.subprocess_git_facts(bad_branch, {"worktree": str(tmp_path)}, HEAD) == (None, None, None)


# ---------------------------------------------------------------------------
# handoff
# ---------------------------------------------------------------------------


def test_line_handoff_has_system_and_bare_conversation_ref():
    dispatch = _claimed()
    pack = svc.line_handoff(dispatch, "L1")
    assert set(pack) == {"system", "conversation_ref"}
    assert pack["conversation_ref"] == {"app": "claude-code", "session_id": None}
    expected = app_lines.line_pack(dispatch, dispatch.line("L1"), app_lines.cell(dispatch, "L1"))
    assert pack["system"] == expected["system"]
    unclaimed = svc.line_handoff(_dispatch(), "L1")
    assert unclaimed["conversation_ref"] == {"app": None, "session_id": None}
    with pytest.raises(KeyError):
        svc.line_handoff(dispatch, "nope")


# ---------------------------------------------------------------------------
# async wrappers
# ---------------------------------------------------------------------------


class FakeDispatchService:
    """``apply_mutation`` / ``get`` with the real lock-copy-save shape."""

    def __init__(self, dispatch: Dispatch) -> None:
        self.rows = {dispatch.id: dispatch}
        self.saves = 0
        self.resumed: list[bool] = []
        self._lock = asyncio.Lock()

    async def get(self, dispatch_id: str) -> Dispatch | None:
        row = self.rows.get(dispatch_id)
        return copy.deepcopy(row) if row is not None else None

    async def apply_mutation(self, dispatch_id, fn, *, resume=False, claim=False):
        async with self._lock:
            current = self.rows[dispatch_id]
            draft = copy.deepcopy(current)
            await asyncio.sleep(0)
            updated = fn(draft) or draft
            self.rows[dispatch_id] = updated
            self.saves += 1
            self.resumed.append(bool(resume() if callable(resume) else resume))
            return updated


@pytest.mark.asyncio
async def test_apply_saves_on_ok_and_never_resumes():
    service = FakeDispatchService(_dispatch())
    outcome = await svc.claim(
        service, "d1", "L1", app_id="claude-code", can_write=True, enabled=True,
        live_leases=0, max_leases=1, now=NOW, env=FULL,
    )
    assert outcome.ok
    assert service.saves == 1 and service.resumed == [False]
    assert app_lines.cell(service.rows["d1"], "L1")["state"] == app_lines.ACTIVE


@pytest.mark.asyncio
async def test_apply_refusal_writes_nothing():
    service = FakeDispatchService(_dispatch())
    before = copy.deepcopy(service.rows["d1"].gates)
    outcome = await svc.claim(
        service, "d1", "L1", app_id="claude-code", can_write=True, enabled=True,
        live_leases=0, max_leases=1, now=NOW, env={},
    )
    assert outcome.code == "mode_denied"
    assert service.saves == 0
    assert service.rows["d1"].gates == before


@pytest.mark.asyncio
async def test_concurrent_claims_one_wins():
    service = FakeDispatchService(_dispatch())

    def go(app: str):
        return svc.claim(
            service, "d1", "L1", app_id=app, can_write=True, enabled=True,
            live_leases=0, max_leases=1, now=NOW, env=FULL,
        )

    first, second = await asyncio.gather(go("claude-code"), go("codex"))
    assert sorted([first.ok, second.ok]) == [False, True]
    loser = first if not first.ok else second
    assert loser.code == "already_claimed"
    assert service.saves == 1


@pytest.mark.asyncio
async def test_async_report_reads_facts_then_writes():
    service = FakeDispatchService(_claimed())
    seen: list[str] = []

    def facts(line, item, head):
        seen.append(item["app_id"])
        return GOOD_FACTS

    outcome = await svc.report(
        service, "d1", "L1", git_facts=facts, app_id="claude-code", status="done",
        head=HEAD, summary="s", files=(), now=NOW + 60, env=FULL,
    )
    assert outcome.ok and seen == ["claude-code"]
    assert app_lines.cell(service.rows["d1"], "L1")["state"] == app_lines.REPORTED
    hb = await svc.heartbeat(service, "d1", "L1", app_id="claude-code", now=NOW, env=FULL)
    assert hb.code == "lease_lost"
    rel = await svc.release(service, "d1", "L1", app_id="claude-code", reason="r", now=NOW, env=FULL)
    assert rel.code == "lease_lost"


def test_refusal_raises_protocol_error():
    from src.collab.types import AlreadyClaimedError, ModeDeniedError

    dispatch = _claimed()
    with pytest.raises(AlreadyClaimedError):
        _claim(dispatch, app_id="codex").raise_for_refusal()
    with pytest.raises(ModeDeniedError):
        _claim(_dispatch(), env={}).raise_for_refusal()
    _claim(_dispatch()).raise_for_refusal()


def test_every_refusal_code_has_an_envelope_code():
    from src.collab.types import ERROR_CODES

    assert set(svc.REFUSAL_CODES.values()) <= set(ERROR_CODES)


@pytest.fixture(autouse=True)
def _line_claims_routed(monkeypatch):
    """Line claims need ``modes.LINE_CLAIMS_ENABLED`` as well as ``full`` (review fix 7)."""
    from src.collab import modes

    monkeypatch.setattr(modes, "LINE_CLAIMS_ENABLED", True)
