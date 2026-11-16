"""N6: a fix round is an itemised Input and its report is checked item by item."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.server.services.autonomy.stage_gate import Handoff
from src.tools.auto_dispatch.machine import (
    RULING_ENFORCE_ENV,
    RULING_UNCONFIRMED,
    DispatchMachine,
    _fix_task,
)
from src.tools.auto_dispatch.rulings import (
    DEFAULT_MUST_NOT,
    FIX_RULING,
    RULING_CHECK,
    RulingCheck,
    build_ruling,
    check_confirmation,
    deliver_ruling,
    render_ruling,
    ruling_from_dict,
    ruling_from_snapshot,
)
from src.tools.auto_dispatch.types import Dispatch, Line, LineStatus



def _handoff() -> Handoff:
    return Handoff(
        branch="feature/line-0",
        commit="abc123",
        files=("src/app.py",),
        test_command="pytest -q tests/unit/test_app.py",
        results="1 failed",
        known_limitations="none",
    )


def test_each_blocker_becomes_one_must_item_with_a_stable_id() -> None:
    ruling = build_ruling("line-0", ["missing unit test", "unused import"], fix_round=2)

    assert ruling.line_id == "line-0"
    assert ruling.fix_round == 2
    assert [item.id for item in ruling.must] == ["M1", "M2"]
    assert ruling.must_texts == ("missing unit test", "unused import")
    assert [item.kind for item in ruling.must] == ["must", "must"]
    assert {item.kind for item in ruling.must_not} == {"must_not"}
    for item in DEFAULT_MUST_NOT:
        assert item in ruling.must_not_texts
    assert all(item.id.startswith("N") for item in ruling.must_not)


def test_the_rendered_input_has_both_lists_examples_and_a_report_format() -> None:
    ruling = build_ruling(
        "line-0",
        ["missing unit test"],
        fix_round=2,
        examples=["assert response.status_code == 200"],
    )

    text = ruling.render()

    assert "Must:" in text
    assert "Must not:" in text
    assert "[M1] missing unit test" in text
    assert "[N1] Do not patch only the reported edge case" in text
    assert "Examples:" in text
    assert "RULING M1: confirmed" in text
    assert "RULING N1: confirmed" in text


def test_the_fix_round_is_delivered_as_an_input_not_only_text() -> None:
    delivered = deliver_ruling("line-0", ["missing test"], fix_round=1)
    again = deliver_ruling("line-0", ["missing test"], fix_round=1)

    assert delivered.kind == "fix_round"
    assert delivered.role == "user"
    assert delivered.line_id == "line-0"
    assert delivered.fix_round == 1
    assert delivered.text == again.text  # stable text, no random ids
    assert [item.id for item in delivered.must] == ["M1"]
    payload = delivered.to_dict()
    assert payload["kind"] == "fix_round"
    assert payload["must"][0]["id"] == "M1"


def test_a_report_that_confirms_every_item_is_complete() -> None:
    ruling = build_ruling("line-0", ["missing test", "unused import"])
    report = "\n".join(
        f"RULING {item.id}: confirmed - evidence for {item.id}"
        for item in ruling.items
    )

    check = check_confirmation(report, ruling)

    assert isinstance(check, RulingCheck)
    assert check.complete
    assert set(check.confirmed) == {item.id for item in ruling.items}
    assert check.missing == ()


def test_a_report_that_skips_one_item_names_the_missing_item() -> None:
    ruling = build_ruling("line-0", ["missing test"])
    report = "RULING M1: confirmed - added the test"

    check = check_confirmation(report, ruling)

    assert not check.complete
    assert check.missing == ("N1", "N2", "N3")


def test_a_negative_line_does_not_count_as_a_confirmation() -> None:
    ruling = build_ruling("line-0", ["missing test"])
    report = "M1: not done\nN1: confirmed - avoided it\nN2: confirmed\nN3: confirmed"

    check = check_confirmation(report, ruling)

    assert check.missing == ("M1",)


def test_a_structured_report_is_read_without_parsing_prose() -> None:
    ruling = build_ruling("line-0", ["missing test"])
    report = {"confirmed": [item.id for item in ruling.items]}

    assert check_confirmation(report, ruling).complete

    partial = {"must": {"M1": True}, "must_not": {"N1": "confirmed"}}
    check = check_confirmation(partial, ruling)
    assert check.missing == ("N2", "N3")


def test_naming_an_item_by_its_full_text_is_not_a_confirmation() -> None:
    ruling = build_ruling("line-0", ["missing regression test"])

    check = check_confirmation("missing regression test", ruling)

    assert "M1" not in check.confirmed
    assert "M1" in check.missing
    assert not check.complete


def test_repeating_every_item_text_is_not_a_confirmation() -> None:
    ruling = build_ruling("line-0", ["missing regression test"])
    report = "\n".join(
        [*ruling.must_texts, *ruling.must_not_texts]
    )

    check = check_confirmation(report, ruling)

    assert not check.complete
    assert set(check.missing) == {item.id for item in ruling.items}


def test_an_explicit_false_confirmation_is_missing() -> None:
    ruling = build_ruling("line-0", ["missing test"])
    report = {
        "items": [
            {"id": item.id, "confirmed": item.kind == "must_not"}
            for item in ruling.items
        ]
    }

    check = check_confirmation(report, ruling)

    assert not check.complete
    assert check.missing == ("M1",)


def test_a_named_item_without_a_status_is_not_confirmed() -> None:
    ruling = build_ruling("line-0", ["missing test"])

    check = check_confirmation({"id": "M1"}, ruling)

    assert "M1" in check.missing


def test_a_nested_false_confirmation_is_missing() -> None:
    ruling = build_ruling("line-0", ["missing test"])

    check = check_confirmation(
        {"must": {"M1": {"confirmed": False}}, "must_not": {"N1": {"confirmed": True}}},
        ruling,
    )

    assert check.missing == ("M1", "N2", "N3")


def test_a_persisted_incomplete_check_stays_incomplete_without_missing_ids() -> None:
    check = RulingCheck.from_dict({"complete": False})

    assert check is not None
    assert not check.complete


def test_an_item_line_must_affirm_the_item() -> None:
    ruling = build_ruling("line-0", ["missing test"])
    report = "\n".join(f"[{item.id}] {item.text}" for item in ruling.items)

    check = check_confirmation(report, ruling)

    assert not check.complete
    assert set(check.missing) == {item.id for item in ruling.items}


def test_the_ruling_round_trips_through_the_snapshot() -> None:
    ruling = build_ruling("line-0", ["missing test"], fix_round=2)
    snapshot = {FIX_RULING: ruling.to_dict()}

    restored = ruling_from_snapshot(snapshot)

    assert restored == ruling
    assert ruling_from_dict(snapshot[FIX_RULING]) == ruling
    assert render_ruling(snapshot[FIX_RULING]) == ruling.render()


def test_the_fix_task_keeps_the_blocker_brief_and_adds_the_structured_ruling() -> None:
    ruling = build_ruling("line-0", ["missing test"], fix_round=1)

    task = _fix_task("do a", _handoff(), ["missing test"], ruling=ruling)

    assert "Blockers:\n- missing test\n" in task
    assert "Ruling for the fix round on line-0 (round 1)." in task
    assert "[M1] missing test" in task
    assert "[N1]" in task


def test_the_machine_records_the_confirmation_on_the_line_snapshot() -> None:
    ruling = build_ruling("line-0", ["missing test"], fix_round=1)
    snapshot = {FIX_RULING: ruling.to_dict()}

    class _Gates:
        def line_snapshot(self, dispatch: Dispatch, line: Line) -> dict[str, object]:
            return snapshot

    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(gates=_Gates())
    dispatch = Dispatch(
        id="d1", goal="goal", user_id="u1", created_at="now", lines=[]
    )
    line = Line(id="line-0", name="line-0", task="do a")
    report = "\n".join(
        f"RULING {item.id}: confirmed - evidence" for item in ruling.items
    )

    machine._record_ruling_check(dispatch, line, report)

    assert snapshot[RULING_CHECK]["complete"] is True
    assert set(snapshot[RULING_CHECK]["confirmed"]) == {
        item.id for item in ruling.items
    }


def test_a_missing_confirmation_is_stored_as_an_incomplete_check() -> None:
    ruling = build_ruling("line-0", ["missing test"], fix_round=1)
    snapshot = {FIX_RULING: ruling.to_dict()}

    class _Gates:
        def line_snapshot(self, dispatch: Dispatch, line: Line) -> dict[str, object]:
            return snapshot

    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(gates=_Gates())
    dispatch = Dispatch(
        id="d1", goal="goal", user_id="u1", created_at="now", lines=[]
    )
    line = Line(id="line-0", name="line-0", task="do a")

    machine._record_ruling_check(dispatch, line, "M1: confirmed - test added")

    assert snapshot[RULING_CHECK]["complete"] is False
    assert "N1" in snapshot[RULING_CHECK]["missing"]


@pytest.mark.parametrize("status", ["confirmed", "true", "yes", "done", "verified"])
def test_common_structured_statuses_confirm(status: str) -> None:
    ruling = build_ruling("line-0", ["missing test"])
    report = {"items": {item.id: status for item in ruling.items}}

    assert check_confirmation(report, ruling).complete


def test_rendering_a_ruling_includes_the_escalation_instruction() -> None:
    ruling = build_ruling(
        "line-0",
        ["missing test"],
        escalation={"instruction": "Prefer a new approach.", "summary": "Two rounds failed."},
    )

    assert any("Prefer a new approach." == item.text for item in ruling.must)
    assert any("Do not continue the same approach" in item.text for item in ruling.must_not)


def _review_gate_machine(snapshot, reviewed, blocked):
    scheduler: dict[str, object] = {}

    class _Gates:
        async def allows_review_async(self, dispatch: Dispatch, line_id: str) -> bool:
            return True

        def line_snapshot(self, dispatch: Dispatch, line: Line) -> dict[str, object]:
            return snapshot

        def _scheduler(self, dispatch: Dispatch) -> dict[str, object]:
            return scheduler

        def stamp(self) -> str:
            return "now"

    machine = object.__new__(DispatchMachine)
    machine._c = SimpleNamespace(gates=_Gates())
    machine._review_waits = lambda dispatch: set()

    async def absorb_midpass(dispatch: Dispatch) -> int:
        # N77 absorbs queued operator requests mid-pass; none are queued here.
        return 0

    machine._absorb_midpass = absorb_midpass

    async def review_once(dispatch: Dispatch, line: Line, *, secondary: bool) -> None:
        reviewed.append((line.id, secondary))

    async def apply_review_result(event, dispatch, line, **kwargs) -> None:
        blocked.append((line.id, tuple(event.blockers)))

    machine._review_once = review_once
    machine._apply_review_result = apply_review_result
    machine.scheduler = scheduler
    return machine


@pytest.mark.asyncio
async def test_an_enforced_unconfirmed_ruling_never_reaches_review(monkeypatch) -> None:
    monkeypatch.setenv(RULING_ENFORCE_ENV, "1")
    ruling = build_ruling("line-0", ["missing test"], fix_round=1)
    snapshot = {
        FIX_RULING: ruling.to_dict(),
        RULING_CHECK: {"confirmed": [], "missing": ["M1"], "complete": False},
        "machine": {"task_id": "line-0", "stage": "primary_review", "blocking_reviewer": "codex"},
    }
    reviewed: list[tuple[str, bool]] = []
    blocked: list[tuple[str, tuple[str, ...]]] = []
    machine = _review_gate_machine(snapshot, reviewed, blocked)
    line = Line(id="line-0", name="line-0", task="do a", status=LineStatus.RUNNING)
    dispatch = Dispatch(
        id="d1", goal="goal", user_id="u1", created_at="now", lines=[line]
    )

    await machine._review_gated(dispatch)

    assert reviewed == []
    assert blocked and blocked[0][0] == "line-0"
    assert any("RULING M1" in item and "not confirmed" in item for item in blocked[0][1])


@pytest.mark.asyncio
async def test_an_unconfirmed_ruling_is_recorded_and_reviewed_by_default(monkeypatch) -> None:
    monkeypatch.delenv(RULING_ENFORCE_ENV, raising=False)
    ruling = build_ruling("line-0", ["missing test"], fix_round=1)
    snapshot = {
        FIX_RULING: ruling.to_dict(),
        RULING_CHECK: {"confirmed": [], "missing": ["M1"], "complete": False},
        "machine": {"task_id": "line-0", "stage": "primary_review", "blocking_reviewer": "codex"},
    }
    reviewed: list[tuple[str, bool]] = []
    blocked: list[tuple[str, tuple[str, ...]]] = []
    machine = _review_gate_machine(snapshot, reviewed, blocked)
    line = Line(id="line-0", name="line-0", task="do a", status=LineStatus.RUNNING)
    dispatch = Dispatch(
        id="d1", goal="goal", user_id="u1", created_at="now", lines=[line]
    )

    await machine._review_gated(dispatch)
    await machine._review_gated(dispatch)

    # No synthetic BLOCK and no extra fix round: the real reviewer runs.
    assert blocked == []
    assert reviewed == [("line-0", False), ("line-0", False)]
    assert snapshot[RULING_UNCONFIRMED][0].startswith("RULING M1: not confirmed")
    audit = machine.scheduler["audit"]
    assert [entry["kind"] for entry in audit] == [RULING_UNCONFIRMED]
    assert audit[0]["items"][0] == "M1"
    notes = machine._review_notes(line, dispatch)
    assert "Unconfirmed rulings" in notes and "- M1: missing test" in notes
    assert "not confirmed" not in notes


@pytest.mark.asyncio
async def test_a_confirmed_ruling_reaches_review() -> None:
    ruling = build_ruling("line-0", ["missing test"], fix_round=1)
    snapshot = {
        FIX_RULING: ruling.to_dict(),
        RULING_CHECK: {
            "confirmed": [item.id for item in ruling.items],
            "missing": [],
            "complete": True,
        },
        "machine": {"task_id": "line-0", "stage": "primary_review", "blocking_reviewer": "codex"},
    }
    reviewed: list[tuple[str, bool]] = []
    blocked: list[tuple[str, tuple[str, ...]]] = []
    machine = _review_gate_machine(snapshot, reviewed, blocked)
    line = Line(id="line-0", name="line-0", task="do a", status=LineStatus.RUNNING)
    dispatch = Dispatch(
        id="d1", goal="goal", user_id="u1", created_at="now", lines=[line]
    )

    await machine._review_gated(dispatch)

    assert reviewed == [("line-0", False)]
    assert blocked == []
