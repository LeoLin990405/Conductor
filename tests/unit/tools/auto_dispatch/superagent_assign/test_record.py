"""PRD 4.5: the assignment entry on gates.lines[line_id], keyed by run_id."""

from __future__ import annotations

import copy
import dataclasses
import datetime as dt
import hashlib
import json
from types import MappingProxyType

import pytest

from src.tools.auto_dispatch.superagent_assign.record import (
    ASSIGNMENT_VERSION,
    CHOICE_FIELDS,
    MAX_ASSIGNMENT_ENTRIES,
    SOURCES,
    append_assignment,
    assignment_entries,
    build_assignment_entry,
    assignment_ref,
    inputs_digest,
)
from src.tools.auto_dispatch.superagent_assign.types import (
    Confidence,
    Effort,
    FeasibilityResult,
    LineChoice,
    Mode,
    Source,
    call_error,
    format_error,
    timeout_error,
)

SENTINEL = "RAW-TASK-do-not-persist-ZZZZ"
ENTRY_KEYS = {
    "version",
    "run_id",
    "mode",
    "source",
    "choice",
    "selector_choice",
    "feasibility",
    "reask",
    "fallback_reason",
    "inputs_digest",
    "latency_ms",
    "llm_tokens",
}


def _append(gates: dict, run_id: str, **overrides):
    fields = {
        "line_id": "L",
        "run_id": run_id,
        "mode": "shadow",
        "source": "selector",
        "choice": {"model": "kimi", "effort": "default"},
        "selector_choice": {"model": "kimi", "effort": "default"},
    }
    fields.update(overrides)
    return append_assignment(gates, **fields)


def _chain(count: int) -> dict:
    gates: dict = {}
    for index in range(1, count + 1):
        gates = _append(gates, f"run-{index}")
    return gates


def test_entry_carries_the_prd_fields_and_selector_choice() -> None:
    entry = build_assignment_entry(
        run_id="run-1",
        mode=" SHADOW ",
        source="Superagent_Reask",
        choice={
            "model": "grok",
            "model_id": "grok-4",
            "effort": "high",
            "cli_effort": "high",
            "runtime": "native_cli",
            "reason": "grok headroom 0.25",
            "confidence": "medium",
        },
        feasibility=[
            {"result": "infeasible", "reason_code": "no_headroom"},
            {"ok": True},
        ],
        reask=True,
        fallback_reason=None,
        inputs={"success_rates": {"grok": 0.5}, "headroom": {"grok": 0.25}},
        latency_ms=18,
        llm_tokens=340,
    )
    assert set(entry) == ENTRY_KEYS
    assert "seq" not in entry
    assert entry["version"] == ASSIGNMENT_VERSION == 1
    assert entry["run_id"] == "run-1"
    assert entry["mode"] == "shadow"
    assert entry["source"] == "superagent_reask"
    assert entry["choice"]["model"] == "grok"
    assert entry["choice"]["confidence"] == "medium"
    assert set(entry["choice"]) == set(CHOICE_FIELDS)
    assert entry["reask"] is True
    assert entry["fallback_reason"] is None
    assert entry["latency_ms"] == 18
    assert entry["llm_tokens"] == 340
    assert entry["feasibility"] == [
        {"attempt": 1, "result": "infeasible", "reason_code": "no_headroom"},
        {"attempt": 2, "result": "ok", "reason_code": None},
    ]


def test_selector_choice_is_present_when_omitted() -> None:
    entry = build_assignment_entry(run_id="run-1", mode="off", source="selector")
    assert set(entry["selector_choice"]) == set(CHOICE_FIELDS)
    assert entry["selector_choice"]["model"] is None
    assert entry["choice"]["model"] is None
    assert entry["reask"] is False
    assert entry["latency_ms"] is None
    assert entry["llm_tokens"] is None


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_every_prd_source_is_stored(source: str) -> None:
    entry = build_assignment_entry(
        run_id="run-1",
        mode="enforce",
        source=source,
        reask=source == "superagent_reask",
        fallback_reason="timeout" if source == "selector_fallback" else None,
    )
    assert entry["source"] == source


def test_unknown_source_is_rejected_and_gates_stay_put() -> None:
    gates = {"lines": {"L": {"phase": "dag_ready"}}}
    snapshot = copy.deepcopy(gates)
    with pytest.raises(ValueError):
        _append(gates, "run-1", source="magic")
    assert gates == snapshot


def test_append_returns_a_new_dict_and_keeps_the_line() -> None:
    choice = {"model": "grok", "reason": "quota"}
    selector = {"model": "kimi"}
    attempts = [{"ok": False, "reason_code": "no_headroom"}]
    payload = {
        "task": SENTINEL,
        "success_rates": {"kimi": 0.5},
        "headroom": {"kimi": 0},
    }
    gates = {
        "epoch": 3,
        "lines": {
            "L": {"phase": "dag_ready", "fix_round": 1},
            "other": {"phase": "passed"},
        },
    }
    snapshot = copy.deepcopy(gates)
    result = append_assignment(
        gates,
        line_id="L",
        run_id="run-9",
        mode="enforce",
        source="superagent",
        choice=choice,
        selector_choice=selector,
        feasibility=attempts,
        reask=False,
        fallback_reason=" ",
        inputs=payload,
        latency_ms=0,
        llm_tokens=2.0,
    )
    choice["model"] = "changed"
    selector["model"] = "changed"
    attempts[0]["reason_code"] = "changed"
    payload["success_rates"]["kimi"] = 0.1
    payload["task"] = "rewritten"
    result["lines"]["L"]["phase"] = "mutated"
    result["epoch"] = 0

    assert result is not gates
    assert isinstance(result, dict)
    assert gates == snapshot
    line = result["lines"]["L"]
    assert line["fix_round"] == 1
    entry = line["assignment"]["run-9"]
    assert entry["seq"] == 1
    assert entry["choice"]["model"] == "grok"
    assert entry["selector_choice"]["model"] == "kimi"
    assert entry["feasibility"][0]["reason_code"] == "no_headroom"
    assert entry["fallback_reason"] is None
    assert entry["latency_ms"] == 0
    assert entry["llm_tokens"] == 2
    assert entry["inputs_digest"]["success_rates"] == {"kimi": 0.5}
    assert entry["inputs_digest"]["headroom"] == {"kimi": 0}
    assert result["lines"]["other"] == {"phase": "passed"}
    assert SENTINEL not in json.dumps(entry)


def test_inputs_digest_hashes_the_task_and_does_not_store_it() -> None:
    first = {"task": "alpha", "goal": SENTINEL, "success_rates": {"kimi": 1}, "b": 1}
    second = {"b": 1, "success_rates": {"kimi": 1}, "task": "beta", "goal": SENTINEL}
    digest = inputs_digest(first)
    encoded = json.dumps(
        first, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert set(digest) == {"hash", "success_rates", "headroom"}
    assert digest["hash"] == hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    assert (
        digest["hash"]
        == inputs_digest(
            {"b": 1, "goal": SENTINEL, "task": "alpha", "success_rates": {"kimi": 1}}
        )["hash"]
    )
    assert digest["hash"] != inputs_digest(second)["hash"]
    assert digest["success_rates"] == {"kimi": 1}
    assert SENTINEL not in json.dumps(digest)
    assert "alpha" not in json.dumps(digest)


def test_a_reason_that_quotes_the_task_does_not_keep_the_task() -> None:
    task = "RAW-TASK-do-not-persist-ZZZZ"
    choice = {"model": "kimi", "reason": "Chose kimi for " + task}
    selector = {"model": "glm", "reason": task}
    entry = build_assignment_entry(
        run_id="r",
        mode="shadow",
        source="selector",
        choice=choice,
        selector_choice=selector,
        fallback_reason="fell back because " + task,
        feasibility=[{"result": "infeasible", "reason_code": "blocked:" + task}],
        inputs={"task": task, "lines": [{"goal": "GOAL-" + task}]},
    )
    blob = json.dumps(entry)
    assert task not in blob
    assert "GOAL-" + task not in blob
    assert choice["reason"] == "Chose kimi for " + task
    assert entry["choice"]["model"] == "kimi"
    assert entry["choice"]["reason"] == "Chose kimi for [redacted]"
    assert entry["selector_choice"]["model"] == "glm"
    assert entry["selector_choice"]["reason"] == "[redacted]"
    assert entry["fallback_reason"] == "fell back because [redacted]"
    assert entry["feasibility"][0]["reason_code"] == "blocked:[redacted]"


def test_a_free_text_choice_field_that_quotes_the_task_does_not_keep_it() -> None:
    task = "RAW-TASK-do-not-persist-ZZZZ"
    choice = {
        "model": "kimi",
        "model_id": task,
        "effort": "high",
        "runtime": "native_cli",
        "confidence": "low",
    }
    selector = {"model": "glm", "model_id": task, "reason": "plain " + task}
    entry = build_assignment_entry(
        run_id="r",
        mode="shadow",
        source="selector_fallback",
        fallback_reason="infeasible_twice",
        choice=choice,
        selector_choice=selector,
        inputs={"task": task},
        feasibility=[{"ok": False, "reason_code": "unknown_model_id"}],
    )
    assert task not in json.dumps(entry)
    assert choice["model_id"] == task
    assert selector["model_id"] == task
    assert entry["choice"]["model"] == "kimi"
    assert entry["choice"]["model_id"] == "[redacted]"
    assert entry["choice"]["effort"] == "high"
    assert entry["choice"]["cli_effort"] == "n/a"
    assert entry["choice"]["runtime"] == "native_cli"
    assert entry["choice"]["reason"] is None
    assert entry["choice"]["confidence"] == "low"
    assert entry["selector_choice"]["model"] == "glm"
    assert entry["selector_choice"]["model_id"] == "[redacted]"
    assert entry["selector_choice"]["reason"] == "plain [redacted]"
    assert entry["feasibility"][0]["result"] == "infeasible"
    assert entry["feasibility"][0]["reason_code"] == "unknown_model_id"


def test_inputs_digest_keeps_numeric_rates_and_quota_only() -> None:
    digest = inputs_digest(
        {
            "lines": [
                {
                    "goal": SENTINEL,
                    "text": SENTINEL,
                    "success_rate": 0.4,
                    "acceptance_count": 2,
                }
            ],
            "providers": {
                "glm": {
                    "task": SENTINEL,
                    "success_rate": 0.25,
                    "headroom": {
                        "remaining": 0.0,
                        "credits": 12.5,
                        "plan_full": True,
                        "resets_at": "2026-11-12T00:00:00Z",
                        "note": SENTINEL,
                    },
                }
            },
        }
    )
    assert inputs_digest({"quota": {"glm": 0}, "success_rate": {"glm": 0.25}})[
        "headroom"
    ] == {"glm": 0}
    assert digest["success_rates"] == {
        "lines": {"0": 0.4},
        "providers": {"glm": 0.25},
    }
    assert digest["headroom"] == {
        "providers": {"glm": {"remaining": 0.0, "credits": 12.5}}
    }
    assert SENTINEL not in json.dumps(digest)
    assert "2026-11-12" not in json.dumps(digest)


def test_same_run_replaces_and_a_new_run_appends() -> None:
    gates = _append({}, "run-1", source="superagent", choice={"model": "grok"})
    replaced = _append(
        gates,
        "run-1",
        source="superagent_reask",
        reask=True,
        choice={"model": "stepfun"},
    )
    appended = _append(
        replaced, "run-2", source="selector_fallback", fallback_reason="no_headroom"
    )
    assignment = appended["lines"]["L"]["assignment"]
    assert set(assignment) == {"run-1", "run-2"}
    assert assignment["run-1"]["seq"] == 1
    assert assignment["run-1"]["choice"]["model"] == "stepfun"
    assert assignment["run-1"]["source"] == "superagent_reask"
    assert assignment["run-2"]["seq"] == 2
    assert assignment["run-2"]["fallback_reason"] == "no_headroom"
    assert [item["run_id"] for item in assignment_entries(assignment)] == [
        "run-1",
        "run-2",
    ]


def test_a_line_keeps_at_most_16_entries_by_seq_not_key_order() -> None:
    assert MAX_ASSIGNMENT_ENTRIES == 16
    gates = _chain(16)
    assignment = gates["lines"]["L"]["assignment"]
    assert len(assignment) == 16
    scrambled = {
        "lines": {
            "L": {
                "phase": "implementing",
                "assignment": {
                    key: assignment[key] for key in sorted(assignment, reverse=True)
                },
            }
        }
    }
    updated = _append(scrambled, "run-17", source="superagent")
    stored = updated["lines"]["L"]["assignment"]
    assert updated["lines"]["L"]["phase"] == "implementing"
    assert len(stored) == 16
    # The first launch is kept for the §6.2 cohort; the oldest of the rest goes.
    assert "run-1" in stored
    assert "run-2" not in stored
    assert "run-3" in stored
    assert stored["run-17"]["seq"] == 17
    assert assignment_entries(stored)[0]["run_id"] == "run-1"
    assert assignment_entries(stored)[1]["run_id"] == "run-3"
    assert assignment_entries(stored)[-1]["run_id"] == "run-17"


def test_replacing_the_oldest_run_does_not_grow_or_evict() -> None:
    gates = _chain(16)
    updated = _append(gates, "run-1", choice={"model": "glm"})
    stored = updated["lines"]["L"]["assignment"]
    assert len(stored) == 16
    assert stored["run-1"]["seq"] == 1
    assert stored["run-1"]["choice"]["model"] == "glm"
    assert "run-16" in stored


def test_the_cap_is_per_line() -> None:
    gates = _chain(16)
    updated = _append(gates, "run-1", line_id="M")
    assert len(updated["lines"]["L"]["assignment"]) == 16
    assert set(updated["lines"]["M"]["assignment"]) == {"run-1"}


def test_a_list_of_entries_is_keyed_by_run_id_on_append() -> None:
    gates = {
        "lines": {
            "L": {
                "assignment": [
                    {"run_id": "run-1", "seq": 1, "mode": "off"},
                    {"run_id": "run-2", "seq": 2, "mode": "off"},
                ]
            }
        }
    }
    updated = _append(gates, "run-3")
    stored = updated["lines"]["L"]["assignment"]
    assert set(stored) == {"run-1", "run-2", "run-3"}
    assert stored["run-1"]["mode"] == "off"
    assert stored["run-3"]["seq"] == 3


def test_callers_pass_the_returned_dict_to_accumulate() -> None:
    original: dict = {"lines": {}}
    first = _append(original, "run-1")
    second = _append(original, "run-2")
    assert set(first["lines"]["L"]["assignment"]) == {"run-1"}
    assert set(second["lines"]["L"]["assignment"]) == {"run-2"}
    assert original == {"lines": {}}


# --- Regressions from the N73a re-review (2026-11-12) -----------------------


def _real_choice(**overrides) -> LineChoice:
    fields = {
        "line_id": "L",
        "model": "grok",
        "model_id": "grok-4",
        "effort": Effort.HIGH,
        "runtime": "native_cli",
        "reason": "grok 9/10 on src/web/**; headroom 0.25",
        "confidence": Confidence.MEDIUM,
    }
    fields.update(overrides)
    return LineChoice(**fields)


def test_real_feasibility_results_are_recorded() -> None:
    """Regression: FeasibilityResult and its to_dict() were both rejected."""
    verdicts = [FeasibilityResult.infeasible("no_headroom"), FeasibilityResult.ok()]
    expected = [
        {"attempt": 1, "result": "infeasible", "reason_code": "no_headroom"},
        {"attempt": 2, "result": "ok", "reason_code": None},
    ]
    for attempts in (verdicts, [item.to_dict() for item in verdicts]):
        entry = build_assignment_entry(
            run_id="r",
            mode="enforce",
            source="superagent_reask",
            reask=True,
            feasibility=attempts,
        )
        assert entry["feasibility"] == expected
    single = build_assignment_entry(
        run_id="r", mode="enforce", source="superagent", feasibility=verdicts[1]
    )
    assert single["feasibility"] == [{**expected[1], "attempt": 1}]


def test_a_line_choice_is_recorded_as_plain_json_with_its_cli_effort() -> None:
    """Regression: enums were stored as enum objects and cli_effort was always None."""
    entry = build_assignment_entry(
        run_id="r",
        mode=Mode.ENFORCE,
        source=Source.SUPERAGENT,
        choice=_real_choice(),
        selector_choice={"model": "kimi", "effort": Effort.DEFAULT},
        feasibility=[FeasibilityResult.ok()],
    )
    assert entry["mode"] == "enforce" and type(entry["mode"]) is str
    assert entry["source"] == "superagent" and type(entry["source"]) is str
    assert entry["choice"] == {
        "model": "grok",
        "model_id": "grok-4",
        "effort": "high",
        "cli_effort": "high",
        "runtime": "native_cli",
        "reason": "grok 9/10 on src/web/**; headroom 0.25",
        "confidence": "medium",
    }
    for value in entry["choice"].values():
        assert value is None or type(value) is str
    assert type(entry["selector_choice"]["effort"]) is str
    assert entry["selector_choice"]["cli_effort"] == "n/a"
    assert json.loads(json.dumps(entry)) == entry


@pytest.mark.parametrize(
    ("model", "effort", "cli_effort"),
    (
        ("grok", "default", "high"),
        ("stepfun", "default", None),
        ("stepfun", "max", "max"),
        ("codex", "max", "ultra"),
        ("kimi", "max", "n/a"),
        ("manus", "high", None),
    ),
)
def test_cli_effort_comes_from_the_effort_table(
    model: str, effort: str, cli_effort: str | None
) -> None:
    entry = build_assignment_entry(
        run_id="r",
        mode="shadow",
        source="selector",
        choice={"model": model, "effort": effort},
    )
    assert entry["choice"]["cli_effort"] == cli_effort


def test_an_explicit_cli_effort_must_match_the_table() -> None:
    entry = build_assignment_entry(
        run_id="r",
        mode="shadow",
        source="selector",
        choice={"model": "grok", "effort": "low", "cli_effort": "low"},
    )
    assert entry["choice"]["cli_effort"] == "low"
    with pytest.raises(ValueError):
        build_assignment_entry(
            run_id="r",
            mode="shadow",
            source="selector",
            choice={"model": "grok", "effort": "low", "cli_effort": "ultra"},
        )


@pytest.mark.parametrize(
    ("model", "cli_effort"), (("kimi", "n/a"), ("grok", "high"), ("stepfun", None))
)
def test_a_model_only_selector_choice_is_recorded_at_default(
    model: str, cli_effort: str | None
) -> None:
    for choice in ({"model": model}, {"model": model, "effort": None}):
        entry = build_assignment_entry(
            run_id="r", mode="shadow", source="selector", selector_choice=choice
        )
        assert entry["selector_choice"]["effort"] == "default"
        assert entry["selector_choice"]["cli_effort"] == cli_effort


@pytest.mark.parametrize("task", ["i", "n", "a", "red", "high", "grok", "kimi"])
def test_a_short_task_does_not_corrupt_closed_vocabulary(task: str) -> None:
    """Regression: substring redaction turned 'kimi' into 'k[redacted]m[redacted]'."""
    entry = build_assignment_entry(
        run_id="r",
        mode="enforce",
        source="superagent",
        choice=_real_choice(model="kimi", model_id="kimi-k2", reason="kimi is idle"),
        selector_choice={"model": "grok", "effort": "high", "runtime": "native_cli"},
        feasibility=[FeasibilityResult.infeasible("no_headroom")],
        inputs={"task": task},
    )
    assert entry["choice"]["model"] == "kimi"
    assert entry["choice"]["model_id"] == "kimi-k2"
    assert entry["choice"]["effort"] == "high"
    assert entry["choice"]["cli_effort"] == "n/a"
    assert entry["choice"]["runtime"] == "native_cli"
    assert entry["choice"]["confidence"] == "medium"
    assert entry["choice"]["reason"] == "kimi is idle"
    assert entry["selector_choice"]["model"] == "grok"
    assert entry["feasibility"][0]["reason_code"] == "no_headroom"
    assert "[redacted]" not in json.dumps(entry)


def test_a_task_that_is_part_of_the_marker_is_still_redacted() -> None:
    """Regression: a task inside the word '[redacted]' was never searched for."""
    task = "redacted task"
    entry = build_assignment_entry(
        run_id="r",
        mode="enforce",
        source="superagent",
        choice={"model": "kimi", "reason": "quoting: redacted task"},
        inputs={"task": task},
    )
    assert entry["choice"]["reason"] == "quoting: [redacted]"


def test_success_rate_by_area_is_kept_in_the_digest() -> None:
    digest = inputs_digest(
        {
            "success_rate": {"grok": 0.9},
            "success_rate_by_area": {"grok": {"src/web": 0.5, "note": SENTINEL}},
        }
    )
    assert digest["success_rates"] == {
        "grok": 0.9,
        "success_rate_by_area": {"grok": {"src/web": 0.5}},
    }
    assert SENTINEL not in json.dumps(digest)


def test_digest_hash_tracks_datetime_and_enum_values() -> None:
    """Regression: every datetime hashed to the string 'datetime'."""
    first = inputs_digest({"resets_at": dt.datetime(2026, 11, 12, 8, 0)})["hash"]
    second = inputs_digest({"resets_at": dt.datetime(2027, 1, 1, 8, 0)})["hash"]
    assert first != second
    assert (
        inputs_digest({"effort": Effort.HIGH})["hash"]
        == inputs_digest({"effort": "high"})["hash"]
    )

    @dataclasses.dataclass
    class Reading:
        remaining: float

    assert (
        inputs_digest({"r": Reading(0.25)})["hash"]
        != inputs_digest({"r": Reading(0.5)})["hash"]
    )


def test_digest_refuses_values_it_cannot_hash_by_value() -> None:
    with pytest.raises(TypeError):
        inputs_digest({"x": object()})
    with pytest.raises(TypeError):
        inputs_digest({1: "a", "1": "b"})


def test_the_reader_accepts_the_list_form_the_writer_accepts() -> None:
    """Regression: assignment_entries raised on the legacy list form."""
    rows = assignment_entries(
        [{"run_id": "b", "seq": 2}, {"run_id": "a", "seq": 1}, "junk"]
    )
    assert [row["run_id"] for row in rows] == ["a", "b"]
    assert assignment_entries(None) == []


def test_legacy_entries_without_seq_do_not_collide() -> None:
    """Regression: a missing seq took its position and could repeat a used one."""
    gates = {"lines": {"L": {"assignment": {"a": {"seq": 2}, "b": {}}}}}
    stored = _append(gates, "c")["lines"]["L"]["assignment"]
    assert stored["a"]["seq"] == 2
    assert stored["b"]["seq"] == 3
    assert stored["c"]["seq"] == 4


def test_duplicate_legacy_run_ids_keep_the_higher_seq() -> None:
    gates = {
        "lines": {
            "L": {
                "assignment": {
                    "y": {"run_id": "a", "seq": 5, "mode": "enforce"},
                    "x": {"run_id": "a", "seq": 1, "mode": "off"},
                }
            }
        }
    }
    stored = _append(gates, "b")["lines"]["L"]["assignment"]
    assert stored["a"]["seq"] == 5
    assert stored["a"]["mode"] == "enforce"
    assert stored["b"]["seq"] == 6


def test_a_malformed_legacy_entry_does_not_block_the_append() -> None:
    gates = {
        "lines": {"L": {"assignment": [{"run_id": 7}, {"run_id": "ok", "seq": 1}]}}
    }
    stored = _append(gates, "next")["lines"]["L"]["assignment"]
    assert set(stored) == {"ok", "next"}


def test_an_error_fallback_records_the_error_kind() -> None:
    for error, kind in (
        (call_error("boom"), "call_error"),
        (timeout_error("slow"), "timeout"),
        (format_error("not a plan"), "bad_format"),
    ):
        entry = build_assignment_entry(
            run_id="r",
            mode="enforce",
            source="selector_fallback",
            fallback_reason=error,
        )
        assert entry["fallback_reason"] == kind


@pytest.mark.parametrize("mode", ["", "enforced", "on", None])
def test_an_unknown_mode_is_rejected_not_recorded_as_shadow(mode) -> None:
    with pytest.raises((ValueError, TypeError)):
        build_assignment_entry(run_id="r", mode=mode, source="selector")


@pytest.mark.parametrize(
    ("mode", "source", "reask"),
    (
        ("off", "superagent", False),
        ("off", "selector_fallback", False),
        ("enforce", "superagent_reask", False),
        ("enforce", "superagent", True),
        ("off", "selector", True),
    ),
)
def test_contradictory_records_are_rejected(
    mode: str, source: str, reask: bool
) -> None:
    with pytest.raises(ValueError):
        build_assignment_entry(run_id="r", mode=mode, source=source, reask=reask)


def test_a_read_only_gates_mapping_is_accepted() -> None:
    gates = MappingProxyType(
        {"lines": MappingProxyType({"L": MappingProxyType({"phase": "x"})})}
    )
    updated = _append(gates, "run-1")
    assert isinstance(updated["lines"]["L"], dict)
    assert updated["lines"]["L"]["phase"] == "x"
    assert "assignment" not in gates["lines"]["L"]


def test_assignment_ref_names_the_line_and_run() -> None:
    assert assignment_ref("L", "run-3") == {"line_id": "L", "run_id": "run-3"}
    with pytest.raises(ValueError):
        assignment_ref("L", " ")


def test_off_records_no_feasibility_and_fallback_names_a_reason() -> None:
    with pytest.raises(ValueError):
        build_assignment_entry(
            run_id="r",
            mode="off",
            source="selector",
            feasibility=[FeasibilityResult.ok()],
        )
    with pytest.raises(ValueError):
        build_assignment_entry(run_id="r", mode="shadow", source="selector_fallback")
    entry = build_assignment_entry(
        run_id="r",
        mode="shadow",
        source="selector_fallback",
        reask=True,
        fallback_reason="infeasible_twice",
        feasibility=[
            FeasibilityResult.infeasible("no_headroom"),
            FeasibilityResult.infeasible("breaker_open"),
        ],
    )
    assert entry["reask"] is True
    assert [item["result"] for item in entry["feasibility"]] == ["infeasible"] * 2


def test_object_readings_keep_their_numbers_in_the_digest() -> None:
    @dataclasses.dataclass
    class Reading:
        remaining: float
        plan_full: bool
        note: str

    digest = inputs_digest(
        {
            "headroom": {"grok": Reading(0.25, False, SENTINEL)},
            "success_rate": {"grok": 0.8},
        }
    )
    assert digest["headroom"] == {"grok": {"remaining": 0.25}}
    assert digest["success_rates"] == {"grok": 0.8}
    assert SENTINEL not in json.dumps(digest)


# --- task text and success readings from context.AssignmentInputs ---------

_LEAK = "RAW-TASK-do-not-persist-ZZZZ"


def _typed_line(task: str = _LEAK):
    from src.tools.auto_dispatch.superagent_assign.context import (
        DifficultyFeatures,
        LineInput,
    )

    return LineInput("L", task, False, (), (), 0, (), DifficultyFeatures(0, (), False, 0, False))


def test_task_text_used_as_a_reading_key_is_not_stored() -> None:
    import json

    from src.tools.auto_dispatch.superagent_assign.record import build_assignment_entry

    entry = build_assignment_entry(
        run_id="r",
        mode="shadow",
        source="superagent",
        choice={"model": "kimi"},
        inputs={
            "task": _LEAK,
            "success_rates": {_LEAK: 0.5, "kimi": 0.9},
            "headroom": {_LEAK: 0.0, "grok": 0.25},
            "per_line": {_LEAK: {"headroom": {"glm": 0.5}}},
        },
    )
    assert _LEAK not in json.dumps(entry)
    assert entry["inputs_digest"]["success_rates"] == {"kimi": 0.9}
    assert entry["inputs_digest"]["headroom"] == {"grok": 0.25}


def test_a_typed_line_task_is_redacted_from_the_reason() -> None:
    import json

    from src.tools.auto_dispatch.superagent_assign.record import build_assignment_entry

    entry = build_assignment_entry(
        run_id="r",
        mode="shadow",
        source="superagent",
        choice={"model": "kimi", "reason": "per " + _LEAK},
        inputs={"lines": (_typed_line(),)},
    )
    assert _LEAK not in json.dumps(entry)
    assert entry["choice"]["reason"] == "per [redacted]"


def test_assignment_inputs_bundle_is_accepted_and_keeps_labelled_rates() -> None:
    import json

    from src.tools.auto_dispatch.superagent_assign.context import (
        AreaRate,
        AssignmentInputs,
        ProviderRate,
    )
    from src.tools.auto_dispatch.superagent_assign.record import (
        build_assignment_entry,
        inputs_digest,
    )

    bundle = AssignmentInputs(
        (_typed_line(),),
        (ProviderRate("kimi", 10, 8),),
        (AreaRate("kimi", "web/", 5, 4),),
    )
    entry = build_assignment_entry(
        run_id="r",
        mode="shadow",
        source="superagent",
        choice={"model": "kimi", "reason": "quoting " + _LEAK},
        inputs=bundle,
    )
    assert _LEAK not in json.dumps(entry)
    rates = entry["inputs_digest"]["success_rates"]
    assert rates == {
        "success_by_provider": [{"model": "kimi", "runs": 10, "passed": 8}],
        "success_rate_by_area": [{"model": "kimi", "area": "web/", "runs": 5, "passed": 4}],
    }
    # Different pass counts are different digests.
    other = AssignmentInputs(
        (_typed_line(),),
        (ProviderRate("kimi", 10, 7),),
        (AreaRate("kimi", "web/", 5, 4),),
    )
    assert inputs_digest(other)["hash"] != entry["inputs_digest"]["hash"]
    # A label that quotes the task is not kept.
    leaked = inputs_digest(
        {"task": _LEAK, "success_rate_by_area": [{"model": "kimi", "area": _LEAK, "runs": 1}]}
    )
    assert _LEAK not in json.dumps(leaked)
