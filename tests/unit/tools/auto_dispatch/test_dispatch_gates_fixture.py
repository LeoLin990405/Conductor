"""The dispatch_gates_v1 fixture's Stage-Gate parts are real Stage-Gate output.

``machine`` must be the ``StageGateState.to_dict()`` (handoff nulled) and
``events`` the ``GateEvent.to_dict()`` records that the pure module produces for
the legal sequence in ``tests/fixtures/gen_dispatch_gates_v1.py``. The outer,
dispatch-level fields keep the API's MERGE/BLOCK words and the dispatch's own
fix-round count; see ``tests/fixtures/README-dispatch-gates.md``.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from src.server.services.autonomy import stage_gate as sg
from src.tools.auto_dispatch.types import Dispatch

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
_FIXTURE = _FIXTURES / "dispatch_gates_v1.json"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_dispatch_gates_v1", _FIXTURES / "gen_dispatch_gates_v1.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gen() -> ModuleType:
    return _generator()


@pytest.fixture(scope="module")
def data() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def line(data: dict[str, Any]) -> dict[str, Any]:
    return data["gates"]["lines"]["line-0"]


def test_machine_is_the_stage_gate_snapshot_field_for_field(
    gen: ModuleType, line: dict[str, Any]
) -> None:
    expected = gen.build_line_snapshot()["machine"]
    machine = line["machine"]
    assert list(machine) == list(expected), "machine keys follow StageGateState.to_dict()"
    mismatched = {
        key: (machine[key], expected[key]) for key in expected if machine[key] != expected[key]
    }
    assert mismatched == {}


def test_events_are_the_stage_gate_audit_records(gen: ModuleType, line: dict[str, Any]) -> None:
    assert line["events"] == gen.build_line_snapshot()["events"]
    for event in line["events"]:
        assert tuple(event) == sg.EVENT_FIELDS


def test_machine_invariants_for_a_risk_bearing_line_at_g6(line: dict[str, Any]) -> None:
    machine = line["machine"]
    assert machine["risk_bearing"] is True and line["risk_bearing"] is True
    # a risk-bearing primary pass is APPROVE inside Stage-Gate; MERGE is refused
    assert machine["primary_verdict"] == sg.Verdict.APPROVE.value
    assert machine["primary_passed"] and machine["secondary_passed"]
    assert machine["secondary_skipped"] is False
    # the passing reviews cleared Stage-Gate's own counters ...
    assert machine["fix_rounds"] == 0 and machine["blocking_rounds"] == 0
    assert machine["blocking_reviewer"] == ""
    # ... but the first BLOCK's return point survives
    assert machine["return_stage"] == sg.Stage.PRIMARY_REVIEW.value
    assert machine["stage"] == sg.Stage.AWAITING_HUMAN.value == line["phase"]
    assert machine["handoff"] is None
    assert machine["handoff_ref"] == line["handoff_ref"]
    assert machine["primary_reviewer"] == line["primary_reviewer"]
    assert machine["secondary_reviewer"] == line["secondary_reviewer"]
    assert machine["author_provider"] == line["author"]


def test_outer_fields_use_dispatch_level_words_and_counts(line: dict[str, Any]) -> None:
    gates = line["gates"]
    for name in ("primary_review", "secondary_review"):
        assert gates[name]["verdict"] in {"merge", "block"}
    # outer fix_round counts the fix rounds the dispatch launched
    resubmits = [e for e in line["events"] if e["reason"] == "fix_resubmitted"]
    assert line["fix_round"] == len(resubmits) == 1
    blocks = [h for h in line["history"] if h["result"] == "fail"]
    assert len(blocks) == 1 and blocks[0]["gate"] == "primary_review"


def test_the_snapshot_restores_and_g6_is_the_only_way_forward(line: dict[str, Any]) -> None:
    state = sg.StageGateState.from_dict(line["machine"])
    approved = sg.advance(
        state,
        sg.Event(
            kind=sg.EventKind.GATE_RESULT.value,
            gate="G6",
            result=sg.GateResult(sg.PASS),
            timestamp="2026-11-20T11:00:00+00:00",
        ),
    )
    assert approved.accepted, approved.reason
    assert approved.state.stage is sg.Stage.MERGED


def test_top_level_spec_ref_is_exposed(data: dict[str, Any]) -> None:
    assert isinstance(data["spec_ref"], str) and data["spec_ref"]
    dispatch = Dispatch.from_dict({**data, "user_id": "u1", "created_at": "2026-11-20T10:00:00+00:00"})
    assert dispatch.spec_ref == data["spec_ref"]


def test_the_committed_fixture_is_what_the_generator_writes(gen: ModuleType) -> None:
    assert gen.main(["--check"]) == 0
