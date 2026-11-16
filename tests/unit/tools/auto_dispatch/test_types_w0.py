"""W0 interfaces: the DAG fields on a Line, gates and spec_ref on a Dispatch,
and the two wave-2 JSON contract fixtures."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.tools.auto_dispatch.types import Dispatch, DispatchState, Line, LineStatus, Outcome

_FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


@pytest.mark.parametrize("name", ["dispatch_gates_v1.json", "planner_output_v1.json"])
def test_the_contract_fixtures_are_valid_json_objects(name: str) -> None:
    data = json.loads((_FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    text = (_FIXTURES / name).read_text(encoding="utf-8")
    assert "…" not in text and '"..."' not in text, "placeholders must be replaced"


def test_dispatch_gates_fixture_loads_as_a_dispatch_and_its_lines_round_trip() -> None:
    data = json.loads((_FIXTURES / "dispatch_gates_v1.json").read_text(encoding="utf-8"))
    assert data["gates"]["schema_version"] == "dispatch_gates_v1"
    dispatch = Dispatch.from_dict({**data, "user_id": "u1", "created_at": "2026-11-20T10:00:00+00:00"})
    assert dispatch.state is DispatchState.APPROVAL
    assert dispatch.gates == data["gates"]
    for raw, line in zip(data["lines"], dispatch.lines, strict=True):
        assert line.to_dict() == raw


def test_planner_output_fixture_has_the_v1_line_shape() -> None:
    data = json.loads((_FIXTURES / "planner_output_v1.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert set(data["spec"]) == {"title", "body"}
    keys = {
        "name", "task", "depends_on", "write_scope", "declared_paths", "acceptance",
        "risk_bearing",
    }
    names = {line["name"] for line in data["lines"]}
    for line in data["lines"]:
        assert set(line) == keys
        assert isinstance(line["risk_bearing"], bool)
        assert set(line["depends_on"]) <= names


def _full_dispatch() -> Dispatch:
    line = Line(
        id="line-1",
        name="route",
        task="add the route",
        depends_on=("line-0",),
        write_scope=("src/server/app/x.py", "tests/unit/server/app/test_x.py"),
        acceptance=("cmd: pytest -q tests/unit/server/app/test_x.py", "returns 200"),
        risk_bearing=True,
        assignment_audit="role_policy:v1",
    )
    return Dispatch(
        id="d1",
        goal="ship",
        user_id="u1",
        created_at="2026-11-20T10:00:00+00:00",
        lines=[Line(id="line-0", name="model", task="add the model"), line],
        outcomes={"line-0": Outcome("line-0", "kimi", 3, 10, True)},
        gates={"schema_version": "dispatch_gates_v1", "lines": {"line-1": {"phase": "fixing"}}},
        spec_ref="spec/0123456789abcdef",
    )


def test_a_dispatch_with_gates_and_dag_lines_round_trips() -> None:
    dispatch = _full_dispatch()
    data = dispatch.to_dict()
    assert data["gates"]["lines"]["line-1"]["phase"] == "fixing"
    assert data["spec_ref"] == "spec/0123456789abcdef"
    assert data["lines"][1]["depends_on"] == ["line-0"]
    assert data["lines"][1]["risk_bearing"] is True
    json.dumps(data)  # JSONB-ready: tuples are written as lists

    again = Dispatch.from_dict(json.loads(json.dumps(data)))
    assert again == dispatch
    assert again.lines[1].write_scope == ("src/server/app/x.py", "tests/unit/server/app/test_x.py")
    assert again.lines[1].acceptance[0] == "cmd: pytest -q tests/unit/server/app/test_x.py"
    assert again.lines[1].assignment_audit == "role_policy:v1"
    assert again.to_dict() == data


def test_a_legacy_record_without_the_w0_fields_loads_with_defaults() -> None:
    legacy = {
        "id": "d0",
        "goal": "old",
        "user_id": "u1",
        "created_at": "2026-11-01T00:00:00+00:00",
        "state": "REVIEW",
        "lines": [{"id": "line-0", "name": "a", "task": "t", "status": "RUNNING"}],
        "outcomes": {},
    }
    dispatch = Dispatch.from_dict(legacy)
    assert dispatch.gates == {}
    assert dispatch.spec_ref == ""
    line = dispatch.lines[0]
    assert line.status is LineStatus.RUNNING
    assert (line.depends_on, line.write_scope, line.acceptance) == ((), (), ())
    assert line.risk_bearing is False
    assert line.assignment_audit is None


def test_gates_defaults_are_not_shared_between_instances() -> None:
    first = Dispatch(id="a", goal="", user_id="", created_at="")
    second = Dispatch(id="b", goal="", user_id="", created_at="")
    first.gates["x"] = 1
    assert second.gates == {}
    assert Line(id="l", name="l", task="").depends_on == ()
