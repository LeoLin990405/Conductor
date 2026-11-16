"""N80 L4: the Planner may mark a line ``app_claimable`` (collab ``full`` only)."""

from __future__ import annotations

import json
from copy import deepcopy

import pytest

from src.tools.auto_dispatch import planner_prompt
from src.tools.auto_dispatch.planning import (
    FileSpecArchive,
    LLMPlanner,
    PlanRejection,
    PlanResult,
    _record,
    archived_lines,
    validate_plan,
)
from src.tools.auto_dispatch.types import Line

MODE_ENV = "CONDUCTOR_COLLAB_MODE"


def _line(name: str, **over: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "name": name,
        "task": f"do {name}",
        "depends_on": [],
        "write_scope": [f"docs/{name}.md"],
        "declared_paths": [f"docs/{name}.md"],
        "acceptance": [f"cmd: pytest -q tests/unit/test_{name}.py"],
        "risk_bearing": False,
    }
    entry.update(over)
    return entry


def _payload(*lines: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "spec": {"title": "Docs", "body": "## Goal\nWrite docs."},
        "lines": list(lines),
    }


@pytest.mark.parametrize("mode", [None, "off", "shadow", "docs"])
def test_outside_full_the_field_is_unknown(monkeypatch, mode) -> None:
    if mode is None:
        monkeypatch.delenv(MODE_ENV, raising=False)
    else:
        monkeypatch.setenv(MODE_ENV, mode)
    outcome = validate_plan(_payload(_line("a", app_claimable=True)))
    assert isinstance(outcome, PlanRejection)
    assert outcome.code == "unknown_field:app_claimable"


def test_full_accepts_the_field_and_marks_the_line(monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "full")
    outcome = validate_plan(
        _payload(_line("a", app_claimable=True), _line("b", app_claimable=False), _line("c"))
    )
    assert isinstance(outcome, PlanResult), outcome
    assert [line.app_claimable for line in outcome.lines] == [True, False, False]


def test_explicit_keyword_overrides_the_env(monkeypatch) -> None:
    monkeypatch.delenv(MODE_ENV, raising=False)
    outcome = validate_plan(_payload(_line("a", app_claimable=True)), app_claims=True)
    assert isinstance(outcome, PlanResult)
    assert outcome.lines[0].app_claimable is True


@pytest.mark.parametrize("value", ["yes", 1, None, ["true"]])
def test_full_refuses_a_non_bool(monkeypatch, value) -> None:
    monkeypatch.setenv(MODE_ENV, "full")
    outcome = validate_plan(_payload(_line("a", app_claimable=value)))
    assert isinstance(outcome, PlanRejection)
    assert outcome.code == "invalid_field:app_claimable:a"


def _result(app_claims: bool, **over: object) -> PlanResult:
    outcome = validate_plan(_payload(_line("a", **over), _line("b")), app_claims=app_claims)
    assert isinstance(outcome, PlanResult), outcome
    return outcome


def test_archive_round_trip_keeps_a_marked_line() -> None:
    payload = _record(_result(True, app_claimable=True)).plan_payload()
    assert payload["lines"][0]["app_claimable"] is True
    assert "app_claimable" not in payload["lines"][1]
    lines = archived_lines(json.loads(json.dumps(payload)))
    assert lines is not None
    assert [line.app_claimable for line in lines] == [True, False]


def test_a_plan_without_the_mark_archives_as_before() -> None:
    marked_off = _record(_result(True, app_claimable=False)).plan_payload()
    plain = _record(_result(False)).plan_payload()
    assert marked_off == plain
    for entry in plain["lines"]:
        assert "app_claimable" not in entry


@pytest.mark.asyncio
async def test_file_archive_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "full")
    archive = FileSpecArchive(tmp_path)
    ref = await archive.save(_record(_result(True, app_claimable=True)))
    lines = await archive.load(ref)
    assert lines is not None
    assert [line.app_claimable for line in lines] == [True, False]


def test_line_to_dict_omits_the_key_when_false() -> None:
    plain = Line(id="l", name="l", task="t")
    assert "app_claimable" not in plain.to_dict()
    marked = Line(id="l", name="l", task="t", app_claimable=True)
    assert marked.to_dict()["app_claimable"] is True
    assert Line.from_dict(marked.to_dict()).app_claimable is True


# --------------------------------------------------------------------------
# Prompt and schema
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "shadow", "docs", "bogus"])
def test_prompt_is_byte_identical_outside_full(monkeypatch, mode) -> None:
    monkeypatch.delenv(MODE_ENV, raising=False)
    baseline = planner_prompt.build_planner_prompt("goal", {"kimi": 2})
    schema = deepcopy(planner_prompt.plan_json_schema())
    monkeypatch.setenv(MODE_ENV, mode)
    assert planner_prompt.build_planner_prompt("goal", {"kimi": 2}) == baseline
    assert "app_claimable" not in baseline
    assert planner_prompt.plan_json_schema() == schema


def test_full_prompt_and_schema_mention_the_field(monkeypatch) -> None:
    monkeypatch.delenv(MODE_ENV, raising=False)
    baseline = planner_prompt.build_planner_prompt("goal", {"kimi": 2})
    system = planner_prompt.planner_system_prompt()
    monkeypatch.setenv(MODE_ENV, "full")
    prompt = planner_prompt.build_planner_prompt("goal", {"kimi": 2})
    assert planner_prompt.APP_CLAIMABLE_RULE in prompt
    assert prompt.replace(planner_prompt.APP_CLAIMABLE_RULE + "\n", "") == baseline
    assert planner_prompt.planner_system_prompt() == system
    props = planner_prompt.plan_json_schema()["properties"]["lines"]["items"]["properties"]
    assert props["app_claimable"] == {"type": "boolean"}
    # The shared module-level schema is not mutated.
    base_props = planner_prompt.PLAN_JSON_SCHEMA["properties"]["lines"]["items"]["properties"]
    assert "app_claimable" not in base_props


@pytest.mark.asyncio
async def test_planner_in_full_mode_plans_a_marked_line(monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "full")
    seen: list[str] = []

    async def complete(prompt: str, system: str) -> str:
        seen.append(prompt)
        return json.dumps(_payload(_line("a", app_claimable=True)))

    lines = await LLMPlanner(complete).plan("write docs")
    assert lines[0].app_claimable is True
    assert planner_prompt.APP_CLAIMABLE_RULE in seen[0]


# --------------------------------------------------------------------------
# Service: merge_drive and service-owned gates
# --------------------------------------------------------------------------


def _dispatch(*lines: Line):
    from src.tools.auto_dispatch.types import Dispatch

    return Dispatch(
        id="d1", goal="g", user_id="u", created_at="now", lines=list(lines)
    )


def test_merge_drive_copies_a_changed_app_claimable() -> None:
    from src.server.services.dispatch import merge_drive

    before = _dispatch(Line(id="a", name="a", task="t"))
    after = deepcopy(before)
    after.line("a").app_claimable = True
    saved = merge_drive(deepcopy(before), before, after)
    assert saved.line("a").app_claimable is True


def test_keep_service_gates_keeps_app_line_claims() -> None:
    from src.server.services.dispatch import _keep_service_gates
    from src.tools.auto_dispatch import app_lines

    current = _dispatch(Line(id="a", name="a", task="t"))
    current.gates = {app_lines.GATE_KEY: {"a": {"state": "open"}}}
    updated = _dispatch(Line(id="a", name="a", task="t"))
    updated.gates = {"other": 1}
    _keep_service_gates(current, updated)
    assert updated.gates[app_lines.GATE_KEY] == {"a": {"state": "open"}}


@pytest.fixture(autouse=True)
def _line_claims_routed(monkeypatch):
    """Line claims need ``modes.LINE_CLAIMS_ENABLED`` as well as ``full`` (review fix 7)."""
    from src.collab import modes

    monkeypatch.setattr(modes, "LINE_CLAIMS_ENABLED", True)
