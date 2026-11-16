"""N73 L4 (S7): low-risk dispatches auto-approve G6 as auto:n73; everything else stays human."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tools.auto_dispatch.approval import auto_approval, line_approval_facts
from src.tools.auto_dispatch.auto_merge import AUTO_APPROVE_ENV, AUTO_APPROVER
from src.tools.auto_dispatch.machine import DispatchMachine
from src.tools.auto_dispatch.protocols import ApprovalDecision
from src.tools.auto_dispatch.types import Dispatch, Line, LineStatus


def _dispatch(*, files=("docs/x.md",), history=None, g5="pass", risk=False, scope=("docs/x.md",)):
    line = Line(
        id="l1", name="l1", task="t", status=LineStatus.PASSED, write_scope=tuple(scope)
    )
    snap = {
        "risk_bearing": risk,
        "history": history
        if history is not None
        else [{"gate": "primary_review", "result": "merge"}],
        "gates": {
            "G2": {"result": "pass"},
            "G5": {"result": g5},
            "handoff": {"files": list(files) if files is not None else None},
        },
    }
    return Dispatch(
        id="d1", goal="g", user_id="u", created_at="now", lines=[line],
        gates={"schema_version": "dispatch_gates_v1", "lines": {"l1": snap}, "scheduler": {}},
        approval_token="tok",
    )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # The switch defaults OFF (review fix 1); these tests turn it on.
    monkeypatch.setenv(AUTO_APPROVE_ENV, "1")
    monkeypatch.delenv("CONDUCTOR_DISPATCH_HIGH_RISK_GLOBS", raising=False)


def test_low_risk_all_merge_g5_passed_is_approved() -> None:
    decision = auto_approval(_dispatch())
    assert decision.approve and decision.approver == AUTO_APPROVER


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"files": ("src/tools/auto_dispatch/machine.py",)}, "high_risk"),
        ({"scope": ("migrations/**",)}, "high_risk"),
        ({"risk": True, "history": [{"gate": "primary_review", "result": "merge"}]}, "high_risk"),
        ({"g5": "fail"}, None),
        ({"history": [{"gate": "primary_review", "result": "inconclusive"},
                      {"gate": "primary_review", "result": "merge"}]}, "review_inconclusive"),
        ({"history": [{"gate": "primary_review", "result": "block"}]}, "review_not_merge"),
        ({"history": []}, "review_not_merge"),
    ],
)
def test_anything_else_stays_human(kwargs, reason) -> None:
    decision = auto_approval(_dispatch(**kwargs))
    assert not decision.approve
    if reason:
        assert decision.reason == reason


def test_switch_off_keeps_every_dispatch_human(monkeypatch) -> None:
    monkeypatch.setenv(AUTO_APPROVE_ENV, "0")
    assert not auto_approval(_dispatch()).approve
    monkeypatch.delenv(AUTO_APPROVE_ENV)
    assert not auto_approval(_dispatch()).approve  # default OFF


@pytest.mark.parametrize("files", [None, ()])
def test_missing_changed_file_evidence_stays_human(files) -> None:
    decision = auto_approval(_dispatch(files=files))
    assert not decision.approve and decision.reason == "evidence_missing"


def test_unavailable_git_evidence_stays_human() -> None:
    dispatch = _dispatch()
    dispatch.gates["lines"]["l1"]["gates"]["handoff"]["known_limitations"] = (
        "changed-file Git evidence is unavailable"
    )
    assert auto_approval(dispatch).reason == "evidence_missing"


@pytest.mark.parametrize(
    "g2",
    [
        {"result": "pass", "scope_conflict": {"files": ["x"]}},
        {"result": "pass", "dirty_worktree": {"paths": ["x"]}},
        {"result": "pass", "shadow": [{"check": "scope_check", "would_refuse": "g2_scope_conflict"}]},
    ],
)
def test_any_scope_worktree_or_record_only_finding_stays_human(g2) -> None:
    dispatch = _dispatch()
    dispatch.gates["lines"]["l1"]["gates"]["G2"] = g2
    decision = auto_approval(dispatch)
    assert not decision.approve and decision.reason == "gate_finding"


def test_facts_read_the_line_snapshot() -> None:
    (facts,) = line_approval_facts(
        _dispatch(history=[
            {"gate": "primary_review", "result": "merge"},
            {"gate": "secondary_review", "result": "merge"},
        ])
    )
    assert facts.g2_passed and facts.reviews == ("merge", "merge")
    assert facts.diff_files == ("docs/x.md",)


class _Gate:
    def __init__(self):
        self.resolved = []

    async def resolve(self, token, approved, note=""):
        self.resolved.append((token, approved, note))
        return ApprovalDecision.APPROVED


def _machine(dispatch):
    class _Gates:
        def _scheduler(self, d):
            return d.gates.setdefault("scheduler", {})

        def stamp(self):
            return "now"

    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(gates=_Gates(), gate=_Gate(), auto_approve=True)
    machine.saved = 0
    machine.resumed = 0

    async def leave(coro):
        return await coro

    async def save(d):
        machine.saved += 1

    async def resume(d):
        machine.resumed += 1

    machine._leave = leave
    machine._save = save
    machine._resume = resume
    return machine


@pytest.mark.asyncio
async def test_machine_marks_auto_ready_and_never_opens_the_gate() -> None:
    """Review fix 1: no gate resolve, no resume (so no merge/push/delivery)."""
    dispatch = _dispatch()
    machine = _machine(dispatch)
    assert await machine._auto_approve(dispatch) is True
    # Fails on 3025c108e: the gate was resolved APPROVED and _resume ran.
    assert machine._c.gate.resolved == [] and machine.resumed == 0
    assert dispatch.gates["auto_approval"]["state"] == "auto_ready"
    assert dispatch.gates["auto_approval"]["approver"] == AUTO_APPROVER
    assert "need a human approval" in dispatch.message
    audit = dispatch.gates["scheduler"]["audit"]
    assert audit[-1]["kind"] == "auto_approve" and audit[-1]["approve"] is True


@pytest.mark.asyncio
async def test_the_file_gate_refuses_an_automatic_approver(tmp_path) -> None:
    from src.tools.auto_dispatch.approval import FileApprovalGate

    gate = FileApprovalGate(tmp_path)
    token = await gate.request_approval(_dispatch())
    with pytest.raises(ValueError):
        await gate.resolve(token, True, AUTO_APPROVER)
    assert await gate.decision(token) is ApprovalDecision.PENDING
    await gate.resolve(token, True, "leo approved")
    assert await gate.approver(token) == "leo approved"


@pytest.mark.asyncio
async def test_push_refuses_a_gate_approved_by_an_automatic_approver(tmp_path) -> None:
    from src.tools.auto_dispatch.execution import GateNotApprovedError, GitMerger

    class _AutoGate:
        async def decision(self, token):
            return ApprovalDecision.APPROVED

        async def approver(self, token):
            return AUTO_APPROVER

    merger = object.__new__(GitMerger)
    merger._gate = _AutoGate()
    with pytest.raises(GateNotApprovedError, match="automatic approver"):
        await merger.push("integrate/auto-dispatch-d1", "tok")


@pytest.mark.asyncio
async def test_machine_leaves_a_high_risk_dispatch_pending() -> None:
    dispatch = _dispatch(files=("src/server/database/x.py",))
    machine = _machine(dispatch)
    assert await machine._auto_approve(dispatch) is False
    assert machine._c.gate.resolved == [] and machine.resumed == 0
    assert dispatch.gates["scheduler"]["audit"][-1]["reason"] == "high_risk"


@pytest.mark.asyncio
async def test_a_machine_without_the_switch_keeps_the_human_gate() -> None:
    dispatch = _dispatch()
    machine = _machine(dispatch)
    machine._c.auto_approve = False
    assert await machine._auto_approve(dispatch) is False
    assert machine._c.gate.resolved == []
