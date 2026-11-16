"""The assignment report recommends. It does not change the launch mode."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import Any

from scripts.assign_report import (
    SCHEMA,
    build_report,
    load_snapshot,
    main,
    median,
    percentile_nearest,
)

# The stage-C consumer contract from modes.rollback_due. It reads a cohort
# rate, median, value, or mean through Decimal(str(float)). It does not read
# hits or n. A pass rate more than 10 points lower, or a wall-clock cost per
# line more than 50% higher, fires. Exactly 10 points and exactly 50% stay.
# This copy does not read another commit: collection has to work on a shallow
# checkout.
_PASS_LIMIT = Decimal("10")
_COST_LIMIT = Decimal("0.5")
_SUPER_KEYS = ("superagent", "treatment", "candidate")
_SELECTOR_KEYS = ("selector", "control", "baseline")
_VALUE_KEYS = ("median", "value", "rate", "mean")
_METRIC_ALIASES = {
    "pass_rate": "pass_rate",
    "通过率": "pass_rate",
    "cost_per_line": "cost_per_line",
    "cost": "cost_per_line",
    "成本": "cost_per_line",
}


def _cohort_number(value: object, names: tuple[str, ...]) -> float | None:
    if isinstance(value, str) and value.strip().casefold() == "insufficient":
        return None
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if isfinite(number) else None
    if not isinstance(value, Mapping):
        return None
    status = value.get("status")
    if isinstance(status, str) and status.strip().casefold() == "insufficient":
        return None
    folded = {str(key).casefold(): inner for key, inner in value.items()}
    for name in names:
        if name.casefold() in folded:
            return _cohort_number(folded[name.casefold()], names)
    for key in _VALUE_KEYS:
        if key in value:
            return _cohort_number(value[key], names)
    return None


def _rate_fraction(value: float) -> Decimal | None:
    if not isfinite(value):
        return None
    number = Decimal(str(value))
    if number > 1:
        number = number / Decimal(100)
    if number < 0 or number > 1:
        return None
    return number


def rollback_due(metrics: Mapping[str, Any]) -> bool:
    """Whether ``modes.rollback_due`` would return to shadow.

    The consumer compares ``Decimal(str(rate))``. Exact hit counts would hide
    a rate that only looks like a 10-point gap after the float round-trip.
    """
    if not isinstance(metrics, Mapping):
        return False
    saw_pass = False
    saw_cost = False
    fired = False
    for key, value in metrics.items():
        metric = _METRIC_ALIASES.get(str(key))
        if metric == "pass_rate" and not saw_pass:
            saw_pass = True
            pair = (
                _cohort_number(value, _SUPER_KEYS),
                _cohort_number(value, _SELECTOR_KEYS),
            )
            if pair[0] is None or pair[1] is None:
                continue
            super_rate = _rate_fraction(pair[0])
            selector_rate = _rate_fraction(pair[1])
            if super_rate is None or selector_rate is None:
                continue
            fired = fired or (selector_rate - super_rate) * 100 > _PASS_LIMIT
        elif metric == "cost_per_line" and not saw_cost:
            saw_cost = True
            super_cost = _cohort_number(value, _SUPER_KEYS)
            selector_cost = _cohort_number(value, _SELECTOR_KEYS)
            if (
                super_cost is None
                or selector_cost is None
                or not isfinite(super_cost)
                or not isfinite(selector_cost)
                or selector_cost <= 0
                or super_cost < 0
            ):
                continue
            rise = (Decimal(str(super_cost)) - Decimal(str(selector_cost))) / Decimal(
                str(selector_cost)
            )
            fired = fired or rise > _COST_LIMIT
    return fired


def _row(index: int, group: str, **overrides: object) -> dict[str, object]:
    source = "superagent" if group == "superagent" else "selector"
    base: dict[str, object] = {
        "line_id": f"{group}-{index}",
        "provider": "kimi",
        "area": "src/tools/auto_dispatch",
        "source": source,
        "adopted": group,
        "passed": True,
        "review_blocks": 1,
        "wall_clock_secs": 100.0,
        "tokens": 1000,
        "token_basis": "provider",
        "duration_secs": 80.0,
        "first_feasible": True,
        "asked": True,
        "latency_ms": 100,
        "mode": "enforce",
        "fallback": False,
        "skipped": False,
        "day": "2026-11-12",
    }
    base.update(overrides)
    return base


def _cohort(count: int, group: str, **overrides: object) -> list[dict[str, object]]:
    return [_row(index, group, **overrides) for index in range(count)]


def _report(rows: list[dict[str, object]], stage: str = "C") -> dict:
    return build_report(load_snapshot({"lines": rows}), stage=stage)


def _ready() -> list[dict[str, object]]:
    """Twelve adopted lines. Q8 does not call this sample ready."""
    return _cohort(12, "superagent") + _cohort(12, "selector")


def _promotable() -> list[dict[str, object]]:
    return _cohort(40, "superagent") + _cohort(40, "selector")


def test_rank_helpers_use_nearest_rank_and_an_even_median():
    assert median([1, 2, 3, 4]) == 2.5
    assert percentile_nearest([float(value) for value in range(1, 21)], 95) == 19


def test_stage_b_promotes_a_balanced_sample_and_stage_c_does_not_roll_back():
    short = _report(_ready(), "B")
    assert short["schema"] == SCHEMA
    assert short["promotion"]["ready"] is False
    assert "insufficient:adopted_lines" in short["promotion"]["reasons"]
    assert short["promotion"]["applies"] is True
    assert short["pass_rate"]["superagent"]["n"] == 12
    report = _report(_promotable(), "B")
    assert report["promotion"]["ready"] is True
    assert report["promotion"]["reasons"] == []
    assert report["promotion"]["applies"] is True
    assert report["rollback"]["applies"] is False
    assert report["rollback"]["to_shadow"] is False
    assert report["pass_rate"]["superagent"]["rate"] == 1.0
    assert report["pass_rate"]["superagent"]["n"] == 40
    stage_c = _report(_ready(), "C")
    assert stage_c["rollback"]["status"] == "ok"
    assert stage_c["rollback"]["applies"] is True
    assert stage_c["promotion"]["applies"] is False
    stage_a = _report(_ready(), "A")
    assert stage_a["promotion"]["applies"] is False
    assert stage_a["rollback"]["applies"] is False


def test_pass_gap_and_cost_boundaries():
    dropped = _cohort(12, "superagent")
    dropped[0]["passed"] = False
    dropped[1]["passed"] = False
    wide = _report(dropped + _cohort(12, "selector"), "C")
    assert wide["pass_rate"]["superagent"]["rate"] == 10 / 12
    assert "pass_rate_gap" in wide["promotion"]["reasons"]
    assert wide["rollback"]["to_shadow"] is True
    assert wide["rollback"]["reasons"] == ["pass_rate"]
    assert wide["rollback"]["status"] == "rollback"
    assert "Planner" in wide["rollback"]["notice"]
    assert "does not change the mode" in wide["rollback"]["notice"]
    assert rollback_due(wide["spec"]["modes.rollback_due"]) is True

    costly = _cohort(12, "superagent", wall_clock_secs=160.0)
    costly[0]["passed"] = False
    cost = _report(costly + _cohort(12, "selector", wall_clock_secs=100.0), "C")
    assert cost["rollback"]["reasons"] == ["cost_per_line"]
    assert "pass_rate" not in cost["rollback"]["reasons"]

    exact = _report(
        _cohort(12, "superagent", wall_clock_secs=150.0, tokens=1500)
        + _cohort(12, "selector", wall_clock_secs=100.0, tokens=1000),
        "C",
    )
    assert exact["rollback"]["to_shadow"] is False
    assert exact["rollback"]["status"] == "ok"

    over = _report(
        _cohort(12, "superagent", wall_clock_secs=100.0, tokens=1501)
        + _cohort(12, "selector", wall_clock_secs=100.0, tokens=1000),
        "C",
    )
    # Token medians are not a stage-C arm. Equal wall-clock cost stays.
    assert over["rollback"]["to_shadow"] is False
    assert over["rollback"]["reasons"] == []

    at_ten = _cohort(20, "superagent")
    at_ten[0]["passed"] = False
    at_ten[1]["passed"] = False
    hold = _report(at_ten + _cohort(10, "selector"), "C")
    assert hold["pass_rate"]["superagent"]["rate"] == 0.9
    assert "pass_rate" not in hold["rollback"]["reasons"]
    assert hold["rollback"]["to_shadow"] is False
    assert rollback_due(wide["spec"]["modes.rollback_due"]) is wide["rollback"]["to_shadow"]
    assert rollback_due(exact["spec"]["modes.rollback_due"]) is False
    assert rollback_due(over["spec"]["modes.rollback_due"]) is False


def test_small_cells_do_not_roll_back_or_decide():
    small = _report(_cohort(9, "superagent") + _cohort(9, "selector"), "C")
    assert small["rollback"]["status"] == "insufficient"
    assert small["rollback"]["to_shadow"] is False
    assert small["promotion"]["ready"] is False
    assert any(reason.startswith("insufficient:") for reason in small["promotion"]["reasons"])


def test_missing_usage_is_not_zero_and_credits_omit_unknowns():
    bare = load_snapshot(
        {
            "lines": [
                _row(0, "superagent", tokens=50, token_basis=None),
                _row(1, "superagent", tokens=None, token_basis="provider"),
            ]
        }
    )
    assert bare[0].tokens is None
    assert bare[1].tokens is None
    report = _report(
        _cohort(12, "superagent", tokens=50, token_basis=None) + _cohort(12, "selector"),
        "B",
    )
    assert report["cost"]["tokens"]["superagent"]["median"] is None
    assert report["cost"]["tokens"]["superagent"]["n"] == 0
    assert report["cost"]["usage_missing"] == 12
    assert 0 not in report["cost"]["token_basis"]

    credits = _report(
        [
            _row(0, "superagent", line_id="a", paid_credits=1.5, codexbar_delta=None),
            _row(1, "superagent", line_id="b", paid_credits=2.0),
            _row(2, "selector", line_id="c", paid_credits=None, codexbar_delta=1.25),
            _row(3, "selector", line_id="d", paid_credits=None, codexbar_delta=0.75),
        ]
    )
    assert credits["paid_credits"]["known"] is True
    assert credits["paid_credits"]["by_day"] == [{"day": "2026-11-12", "amount": 3.5}]
    assert credits["paid_credits"]["lines_with_spend"] == 2
    assert credits["paid_credits"]["lines_missing"] == 2
    assert credits["codexbar"] == {"known": True, "delta": 2.0, "n": 2}

    unknown = _report([_row(0, "superagent", paid_credits=None, codexbar_delta=None)])
    assert unknown["codexbar"] == {"known": False, "delta": None, "n": 0}
    assert unknown["paid_credits"]["known"] is False
    assert unknown["paid_credits"]["lines_missing"] == 1


def test_groups_exclude_skips_fallbacks_and_self_inflicted_cost():
    skipped = _row(0, "superagent", line_id="skip", skipped=True, passed=False)
    fallback = _row(
        0,
        "selector",
        line_id="fb",
        source="selector_fallback",
        adopted="selector",
        fallback=True,
        assignment_error=True,
        passed=False,
        first_feasible=False,
        inconclusive=3,
        review_errors=2,
        review_blocks=9,
    )
    rows = _cohort(40, "superagent") + _cohort(40, "selector") + [skipped, fallback]
    report = _report(rows, "B")
    assert report["counts"]["skipped"] == 1
    assert report["counts"]["fallbacks"] == 1
    assert report["counts"]["assignment_errors"] == 1
    assert report["counts"]["superagent"] == 40
    assert report["counts"]["selector"] == 40
    assert report["counts"]["inconclusive"] == 3
    assert report["counts"]["review_errors"] == 2
    assert report["pass_rate"]["superagent"]["rate"] == 1.0
    assert report["review_rounds"]["superagent"]["mean"] == 1.0
    assert report["promotion"]["ready"] is True
    reassigned = _cohort(12, "superagent")
    reassigned[0]["human_reassign"] = True
    reassigned[1]["human_unblock"] = True
    blocked = _report(reassigned + _cohort(12, "selector"))
    assert blocked["pass_rate"]["superagent"]["hits"] == 10
    assert blocked["pass_rate"]["superagent"]["n"] == 12

    poisoned = _cohort(11, "superagent", wall_clock_secs=100.0, duration_secs=80.0)
    poisoned.append(
        _row(
            99,
            "superagent",
            line_id="self",
            self_inflicted=True,
            wall_clock_secs=9999.0,
            duration_secs=9999.0,
            passed=True,
        )
    )
    cost = _report(poisoned + _cohort(12, "selector"), "C")
    assert cost["pass_rate"]["superagent"]["hits"] == 12
    assert cost["cost"]["wall_clock"]["superagent"]["median"] == 100.0
    assert cost["cost"]["wall_clock"]["superagent"]["n"] == 11
    assert cost["duration"]["superagent"]["median"] == 80.0

    shadow = load_snapshot(
        {"lines": [_row(0, "selector", source="superagent", adopted="selector")]}
    )
    assert shadow[0].group() == "selector"
    assert load_snapshot({"lines": [fallback]})[0].group() is None


def test_promotion_gates_use_strict_limits():
    def reasons(rows: list[dict[str, object]]) -> list[str]:
        return _report(rows, "B")["promotion"]["reasons"]

    eight = [_row(index, "superagent" if index < 5 else "selector", first_feasible=index < 8) for index in range(10)]
    nine = [_row(index, "superagent" if index < 5 else "selector", first_feasible=index < 9) for index in range(10)]
    assert "feasibility" in reasons(eight)
    assert "feasibility" not in reasons(nine)

    louder = _cohort(10, "superagent", review_blocks=1)
    # six 1s and four 2s average 1.4; seven 1s and three 2s average 1.3
    for index, blocks in enumerate([1, 1, 1, 1, 1, 1, 2, 2, 2, 2]):
        louder[index]["review_blocks"] = blocks
    assert "review_rounds" in reasons(louder + _cohort(10, "selector", review_blocks=1))
    exact_review = _cohort(10, "superagent", review_blocks=1)
    for index, blocks in enumerate([1, 1, 1, 1, 1, 1, 1, 2, 2, 2]):
        exact_review[index]["review_blocks"] = blocks
    assert "review_rounds" not in reasons(exact_review + _cohort(10, "selector", review_blocks=1))

    calm = _cohort(10, "superagent", latency_ms=100) + _cohort(10, "selector", latency_ms=100)
    assert _report(calm, "B")["call_overhead"]["p95_ms"] == 100
    assert "latency_p95" not in reasons(calm)
    # n=10 makes nearest-rank P95 the largest sample.
    late = _cohort(10, "superagent")
    late[-1]["latency_ms"] = 30001
    assert "latency_p95" in reasons(late)
    edge = _cohort(10, "superagent")
    edge[-1]["latency_ms"] = 30000
    assert "latency_p95" not in reasons(edge)

    one_of_ten = _cohort(9, "superagent") + [
        _row(0, "selector", line_id="fb", source="selector_fallback", fallback=True, passed=False)
    ]
    assert "fallback" in reasons(one_of_ten)
    one_of_twenty = _cohort(19, "superagent") + [
        _row(0, "selector", line_id="fb", source="selector_fallback", fallback=True, passed=False)
    ]
    assert "fallback" not in reasons(one_of_twenty)

    # 18/20 is a pass rate of 0.9 with enough clean passes for the cost cell.
    waived = _cohort(12, "superagent", wall_clock_secs=1000.0, tokens=5000)
    cheaper = _cohort(20, "selector", wall_clock_secs=100.0, tokens=100)
    cheaper[0]["passed"] = False
    cheaper[1]["passed"] = False
    assert "cost_wall_clock" not in reasons(waived + cheaper)
    assert "cost_tokens" not in reasons(waived + cheaper)

    held = _cohort(12, "superagent", wall_clock_secs=200.0)
    selector = _cohort(11, "selector", wall_clock_secs=100.0)
    selector[0]["passed"] = False  # 10/11, gain under 0.10
    assert "cost_wall_clock" in reasons(held + selector)

    duration_ok = _cohort(12, "superagent", duration_secs=115.0) + _cohort(12, "selector", duration_secs=100.0)
    assert "duration" not in reasons(duration_ok)
    duration_over = _cohort(12, "superagent", duration_secs=116.0) + _cohort(12, "selector")
    assert "duration" in reasons(duration_over)

    five = _cohort(20, "superagent")
    five[0]["passed"] = False
    assert "pass_rate_gap" not in reasons(five + _cohort(10, "selector"))


def test_token_bases_are_not_compared_with_each_other():
    mixed = _report(
        _cohort(10, "superagent", tokens=200, token_basis="estimate")
        + _cohort(10, "selector", tokens=100, token_basis="cli")
    )
    assert mixed["rollback"]["to_shadow"] is False
    assert "cost_tokens" not in mixed["rollback"]["reasons"]
    assert set(mixed["cost"]["tokens"]["by_basis"]) == {"estimate", "cli"}
    assert mixed["cost"]["tokens"]["by_basis"]["estimate"]["superagent"]["median"] == 200
    assert mixed["cost"]["tokens"]["by_basis"]["cli"]["selector"]["median"] == 100
    assert mixed["cost"]["tokens"]["superagent"]["n"] == 0


def test_area_buckets_match_the_assignment_context():
    rows = [
        {
            "provider": "kimi",
            "source": "superagent",
            "adopted": "superagent",
            "passed": True,
            "write_scope": [
                "src/tools/auto_dispatch/machine.py" if index < 6 else "src/llms/native_cli/grok.py"
            ],
        }
        for index in range(12)
    ]
    cells = {item["area"]: item for item in build_report(load_snapshot({"lines": rows}))["by_provider_area"]}
    assert set(cells) == {"src/tools/auto_dispatch", "src/llms/native_cli"}
    assert cells["src/tools/auto_dispatch"]["superagent"]["n"] == 6
    assert cells["src/tools/auto_dispatch"]["superagent"]["status"] == "insufficient"
    assert cells["src/llms/native_cli"]["superagent"]["n"] == 6
    assert cells["src/llms/native_cli"]["superagent"]["status"] == "insufficient"


def test_call_overhead_keeps_assignment_token_usage():
    snapshot = {
        "gates": {
            "lines": {
                "L": {"assignment": {"r": {"mode": "shadow", "llm_tokens": {"total_tokens": 123}}}}
            }
        }
    }
    overhead = build_report(load_snapshot(snapshot))["call_overhead"]
    assert overhead["tokens"]["total"] == 123
    assert overhead["tokens"]["n"] == 1
    assert overhead["tokens"]["median"] == 123


def test_provider_area_cells_below_ten_stay_insufficient():
    rows = _cohort(3, "superagent", provider="glm", area="web")
    rows += _cohort(3, "selector", provider="glm", area="web")
    cell = _report(rows)["by_provider_area"][0]
    assert cell["provider"] == "glm"
    assert cell["area"] == "web/"
    assert cell["superagent"]["status"] == "insufficient"
    assert cell["superagent"]["rate"] == 1.0
    assert cell["superagent"]["n"] == 3


def test_gate_snapshot_and_flat_lines():
    payload = {
        "day": "2026-11-12",
        "gates": {
            "lines": {
                "L1": {
                    "write_scope": ["src/tools/auto_dispatch/modes.py"],
                    "assignment": {
                        "run-1": {
                            "seq": 1,
                            "source": "superagent",
                            "adopted": "superagent",
                            "mode": "enforce",
                            "choice": {"model": "kimi", "effort": "low"},
                            "selector_choice": {"model": "grok"},
                            "feasibility": [{"attempt": 1, "ok": True, "reason_code": None}],
                            "latency_ms": 100,
                        },
                        "run-reassign": {
                            "seq": 2,
                            "source": "selector",
                            "adopted": "selector",
                            "choice": {"model": "kimi"},
                            "selector_choice": {"model": "grok"},
                            "latency_ms": 1,
                        },
                    },
                },
                "L2": {
                    "assignment": {
                        "run-1": {
                            "seq": 1,
                            "source": "selector_fallback",
                            "adopted": "selector",
                            "fallback_reason": "timeout",
                            "choice": {"model": "kimi"},
                            "selector_choice": {"model": "grok"},
                            "feasibility": [{"ok": False}],
                            "latency_ms": 30,
                        }
                    }
                },
            }
        },
        "outcomes": {
            "L1": {
                "passed": True,
                "tokens": 1000,
                "token_basis": "provider",
                "duration_secs": 80,
                "wall_clock_secs": 100,
                "review_blocks": 1,
                "human_reassign": True,
            }
        },
    }
    lines = {item.line_id: item for item in load_snapshot(payload)}
    assert lines["L1"].provider == "kimi"
    assert lines["L1"].area == "src/tools/auto_dispatch"
    assert lines["L1"].adopted == "superagent"
    assert lines["L1"].first_feasible is True
    assert lines["L1"].passed is True
    assert lines["L1"].tokens == 1000
    assert lines["L1"].latency_ms == 100
    assert lines["L1"].asked is True
    assert lines["L1"].group() == "superagent"
    assert lines["L1"].human_reassign is True
    assert lines["L1"].clean_pass() is False
    assert lines["L2"].fallback is True
    assert lines["L2"].assignment_error is True
    assert lines["L2"].provider == "grok"
    assert lines["L2"].group() is None

    flat = {
        "lines": [_row(0, "superagent")],
        "gates": payload["gates"],
    }
    assert len(load_snapshot(flat)) == 1
    wrapped = load_snapshot(
        {"dispatches": [{"lines": [_row(0, "superagent")]}, {"lines": [_row(0, "selector")]}]}
    )
    assert len(wrapped) == 2
    off = load_snapshot({"lines": [{"line_id": "off", "mode": "off", "source": "selector", "adopted": "selector"}]})
    assert off[0].asked is False

    joined = load_snapshot(
        {
            "id": "D",
            "lines": [{"id": "L", "assigned_model": "kimi", "status": "passed"}],
            "outcomes": {"L": {"model": "kimi", "passed": True}},
        }
    )[0]
    assert (joined.line_id, joined.provider, joined.passed) == ("L", "kimi", True)

    def _launch(seq: int, source: str) -> dict[str, object]:
        return {
            "seq": seq,
            "source": source,
            "adopted": source,
            "choice": {"model": "kimi"},
            "selector_choice": {"model": "grok"},
        }

    first = load_snapshot(
        {
            "gates": {
                "lines": {
                    "L": {"assignment": {"r1": _launch(1, "superagent"), "r2": _launch(2, "selector")}}
                }
            },
            "outcomes": {"L": {"passed": True, "human_reassign": True}},
        }
    )[0]
    assert first.group() == "superagent"

    intervened = load_snapshot(
        {
            "lines": [{"id": "L"}],
            "outcomes": {"L": {"passed": True}},
            "gates": {
                "integration": {"phase": "failed"},
                "scheduler": {
                    "audit": [
                        {"kind": "human_reassign", "line_id": "L", "result": "applied"},
                        {"kind": "human_unblock", "line_id": "other", "result": "applied"},
                        {"kind": "human_reassign", "line_id": "L", "result": "refused"},
                    ]
                },
            },
        }
    )[0]
    assert intervened.human_reassign is True
    assert intervened.human_unblock is False
    assert intervened.passed is True
    assert intervened.clean_pass() is False

    failed_g5 = load_snapshot(
        {
            "lines": [{"id": "L"}],
            "outcomes": {"L": {"passed": True}},
            "gates": {"integration": {"phase": "failed"}},
        }
    )[0]
    assert failed_g5.human_reassign is False
    assert failed_g5.clean_pass() is False
    finished = load_snapshot(
        {
            "lines": [{"id": "L", "source": "superagent", "adopted": "superagent"}],
            "outcomes": {"L": {"passed": True}},
            "gates": {"integration": {"phase": "passed"}},
        }
    )[0]
    assert finished.clean_pass() is True


def test_main_exits_zero_even_when_rollback_fires(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_DISPATCH_ASSIGN_MODE", "enforce")
    missing = tmp_path / "missing.json"
    assert main([str(missing)]) == 2
    assert "snapshot not found" in capsys.readouterr().err

    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert main([str(bad)]) == 2
    assert "snapshot unreadable" in capsys.readouterr().err

    dropped = _cohort(12, "superagent")
    dropped[0]["passed"] = False
    dropped[1]["passed"] = False
    path = tmp_path / "snap.json"
    path.write_text(
        json.dumps({"lines": dropped + _cohort(12, "selector")}),
        encoding="utf-8",
    )
    assert main([str(path)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "C"
    assert report["rollback"]["to_shadow"] is True
    assert os.environ["CONDUCTOR_DISPATCH_ASSIGN_MODE"] == "enforce"

    assert main([str(path), "--stage", "B"]) == 0
    staged = json.loads(capsys.readouterr().out)
    assert staged["stage"] == "B"
    assert staged["promotion"]["applies"] is True
    assert rollback_due(staged["spec"]["modes.rollback_due"]) is staged["rollback"]["to_shadow"]


def test_cost_ledger_does_not_treat_missing_usage_as_zero():
    payload = {
        "created_at": "2026-11-12T01:00:00Z",
        "gates": {
            "lines": {
                "known": {
                    "assignment": {
                        "run-1": {
                            "seq": 1,
                            "source": "superagent",
                            "adopted": "superagent",
                            "mode": "enforce",
                            "choice": {"model": "kimi"},
                            "feasibility": [{"ok": True}],
                            "latency_ms": 20,
                        }
                    }
                },
                "gap": {
                    "assignment": {
                        "run-1": {
                            "seq": 1,
                            "source": "selector",
                            "adopted": "selector",
                            "mode": "enforce",
                            "selector_choice": {"model": "glm"},
                            "feasibility": [{"ok": True}],
                        }
                    }
                },
            },
            "cost": {
                "v": 1,
                "lines": {
                    "known": [
                        {
                            "run_id": "a",
                            "role": "author",
                            "wall_secs": 40,
                            "token_basis": "cli",
                            "tokens_in": 10,
                            "tokens_out": 5,
                            "tokens_est": 999,
                            "paid_credits": 1.25,
                            "quota": {"delta_pct": 2.0},
                        },
                        {
                            "run_id": "b",
                            "role": "author",
                            "wall_secs": 60,
                            "token_basis": "cli",
                            "tokens_in": 1,
                            "tokens_out": 1,
                            "quota": {"delta_pct": None},
                        },
                        {
                            "run_id": "review",
                            "role": "reviewer",
                            "wall_secs": 500,
                            "token_basis": "cli",
                            "tokens_in": 100,
                            "tokens_out": 100,
                            "paid_credits": 9,
                        },
                    ],
                    "gap": [
                        {
                            "run_id": "c",
                            "role": "author",
                            "wall_secs": 10,
                            "token_basis": "cli",
                            "tokens_in": None,
                            "tokens_out": None,
                            "tokens_est": 4000,
                        },
                        {
                            "run_id": "d",
                            "role": "author",
                            "wall_secs": None,
                            "token_basis": "estimate",
                            "tokens_est": 10,
                        },
                    ],
                },
            },
        },
        "outcomes": {
            "known": {"passed": True, "tokens": 0, "duration_secs": 80, "review_blocks": 0},
            "gap": {"passed": True, "tokens": 0, "duration_secs": 10},
        },
    }
    lines = {item.line_id: item for item in load_snapshot(payload)}
    assert lines["known"].wall_clock_secs == 100
    assert lines["known"].tokens == 17
    assert lines["known"].token_basis == "cli"
    assert lines["known"].paid_credits == 1.25
    assert lines["known"].codexbar_delta == 2.0
    assert lines["known"].day == "2026-11-12"
    assert lines["gap"].tokens is None
    assert lines["gap"].wall_clock_secs is None
    assert lines["gap"].token_basis is None
    report = build_report(list(lines.values()))
    assert report["cost"]["tokens"]["by_basis"]["cli"]["superagent"]["median"] == 17
    assert report["cost"]["usage_missing"] >= 1
    assert 0 not in (lines["gap"].tokens, lines["gap"].wall_clock_secs)
    mapping = report["spec"]["modes.rollback_due"]
    assert list(mapping) == [
        "pass_rate",
        "review_rounds",
        "cost_per_line",
        "time_per_line",
        "feasibility_rate",
        "fallback_rate",
        "call_overhead",
    ]
    assert "cost" not in mapping
    assert set(mapping["cost_per_line"]) == {"superagent", "selector"}
    assert mapping["cost_per_line"]["superagent"]["median"] == report["cost"]["wall_clock"]["superagent"]["median"]
    assert mapping["cost_per_line"]["selector"]["median"] == report["cost"]["wall_clock"]["selector"]["median"]
    assert "by_basis" not in mapping["cost_per_line"]["superagent"]


def test_shadow_assignment_credits_the_selector_that_ran():
    entry = {
        "mode": "shadow",
        "source": "superagent",
        "choice": {"model": "grok"},
        "selector_choice": {"model": "stepfun"},
    }
    line = load_snapshot({"gates": {"lines": {"L": {"assignment": {"r": entry}}}}})[0]
    assert (line.group(), line.provider) == ("selector", "stepfun")

    chosen = dict(entry, adopted="superagent")
    kept = load_snapshot({"gates": {"lines": {"L": {"assignment": {"r": chosen}}}}})[0]
    assert (kept.group(), kept.provider) == ("superagent", "grok")


def test_sampled_out_enforce_credits_the_selector_model():
    """An enforce line the sample skipped ran the selector, not ``choice``.

    The writer leaves ``adopted`` blank and stores ``source=selector``.
    ``choice`` is still the proposal. The control cohort is ``selector_choice``.
    """
    entry = {
        "mode": "enforce",
        "source": "selector",
        "choice": {"model": "grok"},
        "selector_choice": {"model": "kimi"},
    }
    line = load_snapshot({"gates": {"lines": {"L": {"assignment": {"r": entry}}}}})[0]
    assert (line.group(), line.provider) == ("selector", "kimi")

    flat = load_snapshot({"lines": [dict(entry, line_id="F")]})[0]
    assert (flat.group(), flat.provider) == ("selector", "kimi")


def test_wall_cost_rise_matches_the_rollback_consumer():
    rows = [
        {
            "source": group,
            "passed": True,
            "wall_clock_secs": 160 if group == "superagent" else 100,
        }
        for group in ("superagent", "selector")
        for _ in range(10)
    ]
    report = build_report(load_snapshot({"lines": rows}))
    assert report["rollback"]["to_shadow"] is True
    assert report["rollback"]["reasons"] == ["cost_per_line"]
    assert rollback_due(report["spec"]["modes.rollback_due"]) is report["rollback"]["to_shadow"]


def test_human_unblock_still_counts_toward_author_cost():
    rows = [
        {
            "source": "superagent",
            "passed": True,
            "g5_passed": True,
            "wall_clock_secs": 100 if index < 10 else 300,
            "human_unblock": index == 19,
        }
        for index in range(20)
    ] + [
        {"source": "selector", "passed": True, "g5_passed": True, "wall_clock_secs": 100}
        for _ in range(20)
    ]
    report = build_report(load_snapshot({"lines": rows}))
    wall = report["cost"]["wall_clock"]["superagent"]
    assert (wall["median"], wall["n"]) == (200, 20)
    assert report["pass_rate"]["superagent"]["hits"] == 19
    assert report["rollback"]["to_shadow"] is True
    assert rollback_due(report["spec"]["modes.rollback_due"]) is True


def test_review_blocks_come_from_gate_history():
    history = [
        {"gate": "G3", "round": index, "result": result}
        for index, result in enumerate(("BLOCK", "BLOCK", "MERGE"))
    ]
    payload = {
        "gates": {
            "integration": {"phase": "passed"},
            "lines": {
                "L": {
                    "assignment": {"r": {"mode": "enforce", "source": "superagent"}},
                    "history": history,
                }
            },
        },
        "outcomes": {"L": {"passed": True}},
    }
    assert load_snapshot(payload)[0].review_blocks == 2

    mixed = load_snapshot(
        {
            "gates": {
                "integration": {"phase": "passed"},
                "lines": {
                    "L": {
                        "assignment": {"r": {"mode": "enforce", "source": "superagent"}},
                        "history": [
                            {"result": "block"},
                            {"result": "ERROR"},
                            {"result": "INCONCLUSIVE"},
                            {"result": "approve"},
                        ],
                    }
                },
            },
            "outcomes": {"L": {"passed": True}},
        }
    )[0]
    assert (mixed.review_blocks, mixed.inconclusive, mixed.review_errors) == (1, 1, 1)

    explicit = load_snapshot(
        {
            "lines": [{"id": "L", "source": "superagent", "passed": True, "review_blocks": 5}],
            "gates": {"lines": {"L": {"history": [{"result": "block"}]}}},
        }
    )[0]
    assert explicit.review_blocks == 5

    untouched = load_snapshot(
        {"gates": {"lines": {"L": {"assignment": {"r": {"mode": "enforce", "source": "superagent"}}}}}}
    )[0]
    assert untouched.review_blocks is None

    joined = load_snapshot(
        {
            "lines": [{"id": "L", "source": "superagent", "adopted": "superagent"}],
            "outcomes": {"L": {"passed": True}},
            "gates": {
                "integration": {"phase": "passed"},
                "lines": {
                    "L": {
                        "history": [
                            {"result": "block"},
                            {"result": "block"},
                            {"result": "merge"},
                        ]
                    }
                },
            },
        }
    )[0]
    assert joined.review_blocks == 2
    assert joined.group() == "superagent"


def test_later_selection_calls_stay_in_call_overhead():
    assignment = {
        str(seq): {
            "seq": seq,
            "mode": "enforce",
            "source": "superagent",
            "latency_ms": latency,
            "llm_tokens": {"total_tokens": tokens},
        }
        for seq, latency, tokens in ((1, 100, 10), (2, 40000, 20))
    }
    payload = {
        "gates": {
            "lines": {
                str(index): {
                    "assignment": {
                        key: dict(entry, latency_ms=entry["latency_ms"] + index)
                        for key, entry in assignment.items()
                    }
                }
                for index in range(10)
            }
        }
    }
    lines = load_snapshot(payload)
    assert lines[0].group() == "superagent"
    assert lines[0].latency_ms == 100
    overhead = build_report(lines)["call_overhead"]
    assert (overhead["n"], overhead["p95_ms"], overhead["tokens"]["total"]) == (20, 40008, 300)


def test_assignment_record_schema_keeps_feasibility_and_tokens():
    entry = {
        "mode": "enforce",
        "source": "superagent",
        "feasibility": [{"result": "ok"}],
        "llm_tokens": 123,
    }
    report = build_report(load_snapshot({"gates": {"lines": {"L": {"assignment": {"r": entry}}}}}))
    assert report["feasibility_rate"]["n"] == 1
    assert report["feasibility_rate"]["hits"] == 1
    assert report["call_overhead"]["tokens"]["total"] == 123
    assert report["call_overhead"]["tokens"]["n"] == 1

    missed = dict(entry, feasibility=[{"result": "infeasible"}], llm_tokens=4)
    missed_report = build_report(
        load_snapshot({"gates": {"lines": {"L": {"assignment": {"r": missed}}}}})
    )
    assert missed_report["feasibility_rate"]["n"] == 1
    assert missed_report["feasibility_rate"]["hits"] == 0
    assert missed_report["call_overhead"]["tokens"]["total"] == 4


def test_promotion_requires_a_shared_token_basis_unless_pass_rate_waives_cost():
    def row(group: str, basis: str, **extra: object) -> dict[str, object]:
        base: dict[str, object] = {
            "source": group,
            "adopted": group,
            "passed": True,
            "review_blocks": 0,
            "wall_clock_secs": 100,
            "duration_secs": 100,
            "tokens": 100,
            "token_basis": basis,
            "asked": True,
            "first_feasible": True,
            "latency_ms": 1,
        }
        base.update(extra)
        return base

    mixed = [
        row(group, basis)
        for group, basis in (("superagent", "estimate"), ("selector", "cli"))
        for _ in range(40)
    ]
    blocked = build_report(load_snapshot({"lines": mixed}), stage="B")["promotion"]
    assert blocked["ready"] is False
    assert "insufficient:cost_tokens" in blocked["reasons"]

    waived_rows = [row("superagent", "estimate") for _ in range(40)]
    waived_rows += [
        row("selector", "cli", passed=index >= 4) for index in range(40)
    ]
    waived = build_report(load_snapshot({"lines": waived_rows}), stage="B")["promotion"]
    assert "insufficient:cost_tokens" not in waived["reasons"]
    assert "cost_tokens" not in waived["reasons"]
    assert waived["ready"] is True


def test_later_fallback_is_counted_without_leaving_the_cohort():
    assignment = {
        "first": {
            "seq": 1,
            "mode": "enforce",
            "source": "superagent",
            "latency_ms": 1,
        },
        "retry": {
            "seq": 2,
            "mode": "enforce",
            "source": "selector_fallback",
            "fallback_reason": "timeout",
            "latency_ms": 30000,
        },
    }
    payload = {
        "gates": {
            "lines": {
                str(index): {
                    "assignment": {
                        key: dict(entry, latency_ms=entry["latency_ms"] + index)
                        for key, entry in assignment.items()
                    }
                }
                for index in range(10)
            }
        }
    }
    lines = load_snapshot(payload)
    assert lines[0].group() == "superagent"
    report = build_report(lines)
    assert report["counts"]["fallbacks"] == 10
    assert report["counts"]["assignment_errors"] == 10
    assert report["call_overhead"]["n"] == 20


def test_passed_line_duration_survives_a_failed_integration_gate():
    payload = {
        "lines": [{"source": "superagent", "passed": True, "duration_to_passed_secs": 120}] * 10,
        "gates": {"integration": {"phase": "failed"}},
    }
    cell = build_report(load_snapshot(payload))["duration"]["superagent"]
    assert cell["n"] == 10
    assert cell["median"] == 120


def test_self_environment_attempts_do_not_become_zero_cost():
    """Auth and unparseable attempts stay out of the line cost.

    A missing reading on that attempt must not wipe the usage the author did
    record, and a zero on that attempt must not look like a cheap line. A
    rate limit is the provider's own cost and stays in the sum.
    """

    def author(run_id: str, **extra: object) -> dict[str, object]:
        row: dict[str, object] = {
            "run_id": run_id,
            "role": "author",
            "wall_secs": 40,
            "token_basis": "cli",
            "tokens_in": 10,
            "tokens_out": 5,
            "result": "ok",
        }
        row.update(extra)
        return row

    lines: dict[str, object] = {}
    outcomes: dict[str, object] = {}
    cost: dict[str, object] = {}
    for index in range(12):
        line_id = f"L{index}"
        lines[line_id] = {
            "assignment": {
                "run-1": {
                    "seq": 1,
                    "mode": "enforce",
                    "source": "superagent",
                    "choice": {"model": "kimi"},
                    "feasibility": [{"result": "ok"}],
                    "latency_ms": 10,
                    "llm_tokens": 3,
                }
            },
            "history": [{"result": "merge"}],
        }
        # The outcome type writes 0 when nobody recorded tokens.
        outcomes[line_id] = {
            "passed": True,
            "tokens": 0,
            "model": "kimi",
        }
        cost[line_id] = [
            author(
                "auth",
                wall_secs=500,
                tokens_in=None,
                tokens_out=None,
                failure_class="auth_failed",
                reason_code="worker_auth_failed",
                result="failed",
            ),
            author(
                "limit",
                wall_secs=10,
                tokens_in=1,
                tokens_out=1,
                failure_class="rate_limited",
                reason_code="worker_rate_limited",
            ),
            author("work"),
        ]
    cost["L0"] = [
        author(
            "only",
            wall_secs=0,
            tokens_in=0,
            tokens_out=0,
            failure_class="auth_failed",
            reason_code="missing_credential",
            result="failed",
        )
    ]
    cost["L1"].append(
        author(
            "unparsed",
            wall_secs=80,
            tokens_in=None,
            tokens_out=None,
            reason_code="review_unparseable",
            result="failed",
        )
    )
    payload = {
        "id": "D",
        "created_at": "2026-11-12T01:00:00Z",
        "lines": [
            {
                "id": line_id,
                "status": "PASSED",
                "assigned_model": "kimi",
                "write_scope": ["src/tools/auto_dispatch/machine.py"],
                "duration_to_passed_secs": 80,
            }
            for line_id in lines
        ],
        "outcomes": outcomes,
        "gates": {
            "lines": lines,
            "integration": {"phase": "passed"},
            "cost": {"v": 1, "lines": cost},
        },
    }
    loaded = {item.line_id: item for item in load_snapshot(payload)}
    assert loaded["L0"].tokens is None
    assert loaded["L0"].wall_clock_secs is None
    assert loaded["L0"].token_basis is None
    assert loaded["L1"].wall_clock_secs == 50
    assert loaded["L1"].tokens == 17
    assert loaded["L2"].wall_clock_secs == 50
    assert loaded["L2"].tokens == 17
    report = build_report(list(loaded.values()), stage="C")
    wall = report["cost"]["wall_clock"]["superagent"]
    tokens = report["cost"]["tokens"]["superagent"]
    assert (wall["median"], wall["n"]) == (50, 11)
    assert (tokens["median"], tokens["n"]) == (17, 11)
    assert report["cost"]["usage_missing"] == 1
    assert report["pass_rate"]["superagent"]["n"] == 12
    assert report["duration"]["superagent"]["median"] == 80
    mapping = report["spec"]["modes.rollback_due"]
    assert list(mapping) == [
        "pass_rate",
        "review_rounds",
        "cost_per_line",
        "time_per_line",
        "feasibility_rate",
        "fallback_rate",
        "call_overhead",
    ]
    assert mapping["cost_per_line"]["superagent"]["median"] == 50
    assert rollback_due(mapping) is report["rollback"]["to_shadow"]


def test_relaunched_outcome_duration_is_not_the_line_span():
    """A native relaunch replaces ``_Run.started_at`` before the outcome.

    Launch at 100, retry at 180, pass at 200 stores 20 seconds. That is not
    the span to PASSED, and the snapshot cannot rebuild 100, so the median
    stays unknown. A line that recorded the complete span still reports it.
    """
    from types import SimpleNamespace

    from src.tools.auto_dispatch.machine import DispatchMachine, _Run
    from src.tools.auto_dispatch.types import LineStatus

    machine = DispatchMachine.__new__(DispatchMachine)
    machine._runs = {"L": _Run(started_at=100)}
    machine._runs["L"] = _Run(started_at=180)
    machine._clock = lambda: 200
    dispatch = SimpleNamespace(outcomes={})
    line = SimpleNamespace(
        id="L",
        assigned_model="grok",
        status=LineStatus.PASSED,
        failure_reason=None,
    )
    machine._record_outcome(dispatch, line, reviewer_verdict="MERGE")
    assert dispatch.outcomes["L"].duration_secs == 20
    payload = {
        "lines": [{"id": "L", "source": "superagent"}],
        "outcomes": {"L": dispatch.outcomes["L"].to_dict()},
    }
    report = build_report(load_snapshot(payload))
    assert report["duration"]["superagent"]["median"] in (None, 100)
    assert report["duration"]["superagent"]["median"] is None

    recorded = {
        "lines": [
            {
                "id": "L",
                "source": "superagent",
                "passed": True,
                "duration_to_passed_secs": 100,
            }
        ],
        "outcomes": {"L": dispatch.outcomes["L"].to_dict()},
    }
    kept = build_report(load_snapshot(recorded))
    assert kept["duration"]["superagent"]["median"] == 100

    gates_only = {
        "gates": {
            "lines": {
                "L": {
                    "assignment": {
                        "r": {
                            "mode": "enforce",
                            "source": "superagent",
                            "adopted": "superagent",
                        }
                    }
                }
            }
        },
        "outcomes": {"L": dispatch.outcomes["L"].to_dict()},
    }
    via_gates = build_report(load_snapshot(gates_only))
    assert via_gates["duration"]["superagent"]["median"] is None


def test_pass_rate_rollback_keeps_a_gap_just_past_ten_points():
    """299/1499 versus 149/1498 is 10.0000356 points, not exactly 10.

    Rounding each rate to six decimals makes the gap exactly 10, which must
    not fire. The decision and the exported SPEC rate both keep the gap.
    """
    rows = [{"source": "selector", "passed": index < 299} for index in range(1499)]
    rows += [{"source": "superagent", "passed": index < 149} for index in range(1498)]
    report = build_report(load_snapshot({"lines": rows}))
    assert report["rollback"]["to_shadow"] is True
    assert report["rollback"]["reasons"] == ["pass_rate"]
    assert rollback_due(report["spec"]["modes.rollback_due"]) is True


def test_pass_rate_rollback_exactly_ten_points_stays():
    """22/30 versus 25/30 is exactly 10 points.

    The report compares hit counts. The exported rate is the reading
    ``modes.rollback_due`` turns back into a decimal, and that reading stays
    on the line too. The raw binary fractions compare as slightly over 10.
    """
    rows = [
        {"source": group, "passed": index < hits}
        for group, hits in (("superagent", 22), ("selector", 25))
        for index in range(30)
    ]
    report = build_report(load_snapshot({"lines": rows}))
    assert report["rollback"]["to_shadow"] is False
    assert report["rollback"]["reasons"] == []
    assert rollback_due(report["spec"]["modes.rollback_due"]) is False


def test_cost_rollback_and_spec_keep_unrounded_medians():
    rows = [
        {"source": group, "passed": True, "wall_clock_secs": value}
        for group, value in (("superagent", 1.5000001), ("selector", 1.0))
        for _ in range(10)
    ]
    report = build_report(load_snapshot({"lines": rows}))
    assert report["cost"]["wall_clock"]["superagent"]["median"] == 1.5000001
    assert report["rollback"]["to_shadow"] is True
    assert report["rollback"]["reasons"] == ["cost_per_line"]
    mapping = report["spec"]["modes.rollback_due"]
    assert mapping["cost_per_line"]["superagent"]["median"] == 1.5000001
    assert rollback_due(mapping) is True


def test_batch_assignment_entries_count_once_per_dispatch():
    def batch(count: int, latency: int, tokens: int) -> dict:
        return {
            "gates": {
                "lines": {
                    str(index): {
                        "assignment": {
                            str(index): {
                                "mode": "shadow",
                                "source": "superagent",
                                "run_id": str(index),
                                "choice": {"model": "kimi", "reason": str(index)},
                                "inputs_digest": {"hash": "batch"},
                                "latency_ms": latency,
                                "llm_tokens": tokens,
                            }
                        }
                    }
                    for index in range(count)
                }
            }
        }

    report = build_report(load_snapshot([batch(20, 1000, 100), batch(1, 40000, 20)]))
    overhead = report["call_overhead"]
    assert (overhead["n"], overhead["p50_ms"], overhead["p95_ms"]) == (2, 20500, 40000)
    assert overhead["status"] == "insufficient"
    assert overhead["tokens"] == {"total": 120, "median": 60, "n": 2}


def test_call_eligibility_is_determined_per_assignment_entry():
    snapshot = {
        "gates": {
            "lines": {
                "L": {
                    "assignment": {
                        "a": {"seq": 1, "mode": "off", "source": "selector"},
                        "b": {
                            "seq": 2,
                            "mode": "shadow",
                            "source": "superagent",
                            "latency_ms": 2500,
                            "llm_tokens": 71,
                        },
                    }
                }
            }
        }
    }
    lines = load_snapshot(snapshot)
    assert lines[0].asked is False
    overhead = build_report(lines)["call_overhead"]
    assert (overhead["n"], overhead["p50_ms"], overhead["p95_ms"]) == (1, 2500, 2500)
    assert overhead["tokens"] == {"total": 71, "median": 71, "n": 1}


def test_review_errors_read_persisted_retry_counts():
    snapshot = {
        "gates": {
            "lines": {
                "L": {
                    "assignment": {"r": {"mode": "enforce", "source": "superagent"}},
                    "review_attempts": 3,
                    "review_retry": {"attempt": 3, "reviewer": "codex"},
                    "history": [],
                }
            }
        }
    }
    report = build_report(load_snapshot(snapshot))
    assert report["counts"]["review_errors"] == 3


def test_distinct_input_digests_stay_distinct_calls():
    """Matching latency and tokens are not a call id.

    The shared batch (digest ``batch``) counts once. Each relaunch has its
    own digest, so the two 1000 ms calls stay separate: 3 calls and 50 tokens.
    """
    snapshot = {
        "gates": {
            "lines": {
                line: {
                    "assignment": {
                        str(seq): {
                            "seq": seq,
                            "run_id": f"{line}-{seq}",
                            "mode": "enforce",
                            "source": "superagent",
                            "inputs_digest": {"hash": "batch" if seq == 1 else line},
                            "latency_ms": 100 if seq == 1 else 1000,
                            "llm_tokens": 10 if seq == 1 else 20,
                        }
                        for seq in (1, 2)
                    }
                }
                for line in ("A", "B")
            }
        }
    }
    overhead = build_report(load_snapshot(snapshot))["call_overhead"]
    assert overhead["n"] == 3
    assert overhead["tokens"]["total"] == 50


def test_shared_call_stays_on_every_provider_member():
    """Global dedup must not drop a batch call from later providers."""

    def cells(names: tuple[str, ...]) -> tuple[dict[str, int], int]:
        payload = {
            "gates": {
                "lines": {
                    provider: {
                        "assignment": {
                            "r": {
                                "mode": "enforce",
                                "source": "superagent",
                                "choice": {"model": provider},
                                "inputs_digest": {"hash": "batch"},
                                "latency_ms": 100,
                                "llm_tokens": 10,
                            }
                        }
                    }
                    for provider in names
                }
            }
        }
        report = build_report(load_snapshot(payload))
        found = {
            cell["provider"]: cell["superagent"]["call_overhead"]["n"]
            for cell in report["by_provider_area"]
        }
        return found, report["call_overhead"]["n"]

    left, left_n = cells(("kimi", "grok"))
    right, right_n = cells(("grok", "kimi"))
    assert left == right == {"grok": 1, "kimi": 1}
    assert left_n == right_n == 1


def test_later_decision_sets_rate_denominators():
    """An opening off launch does not own the feasibility or fallback rate."""
    snapshot = {
        "gates": {
            "lines": {
                "L": {
                    "assignment": {
                        "a": {"seq": 1, "mode": "off", "source": "selector"},
                        "b": {
                            "seq": 2,
                            "mode": "shadow",
                            "source": "selector_fallback",
                            "fallback_reason": "timeout",
                            "latency_ms": 30000,
                            "feasibility": [{"result": "infeasible"}],
                        },
                    }
                }
            }
        }
    }
    lines = load_snapshot(snapshot)
    assert lines[0].asked is False
    report = build_report(lines)
    assert report["counts"]["fallbacks"] == 1
    assert report["call_overhead"]["n"] == 1
    assert report["feasibility_rate"]["n"] == 1
    assert report["feasibility_rate"]["hits"] == 0
    assert report["fallback_rate"]["n"] == 1
    assert report["fallback_rate"]["hits"] == 1


def test_review_rounds_include_passes_with_human_intervention():
    assisted = [
        {
            "source": "superagent",
            "passed": True,
            "g5_passed": True,
            "human_unblock": True,
            "review_blocks": 2,
        }
    ] * 10
    unassisted = [
        {
            "source": "superagent",
            "passed": True,
            "g5_passed": True,
            "review_blocks": 0,
        }
    ] * 10
    report = build_report(load_snapshot({"lines": assisted + unassisted}))
    assert report["pass_rate"]["superagent"]["hits"] == 10
    assert report["review_rounds"]["superagent"] == {"mean": 1, "n": 20, "status": "ok"}
    bucket = report["by_provider_area"][0]
    assert bucket["superagent"]["review_rounds"] == {"mean": 1, "n": 20, "status": "ok"}


def test_same_digest_launches_with_different_seq_stay_distinct():
    """Two launches that share an input hash are still two calls."""
    assignment = {
        str(index): {
            "seq": index,
            "run_id": str(index),
            "mode": "enforce",
            "source": "superagent",
            "inputs_digest": "same",
            "latency_ms": latency,
            "llm_tokens": tokens,
        }
        for index, latency, tokens in ((1, 100, 10), (2, 40000, 20))
    }
    overhead = build_report(
        load_snapshot({"gates": {"lines": {"L": {"assignment": assignment}}}})
    )["call_overhead"]
    assert overhead["n"] == 2
    assert overhead["p95_ms"] == 40000
    assert overhead["tokens"]["total"] == 30


def test_same_digest_and_seq_keeps_a_different_run_on_the_same_line():
    """A second run id on one line is not a copy of the first launch."""
    assignment = {
        str(index): {
            "seq": 1,
            "run_id": str(index),
            "mode": "enforce",
            "source": "superagent",
            "inputs_digest": "same",
            "latency_ms": latency,
            "llm_tokens": tokens,
        }
        for index, latency, tokens in ((1, 100, 10), (2, 40000, 20))
    }
    overhead = build_report(
        load_snapshot({"gates": {"lines": {"L": {"assignment": assignment}}}})
    )["call_overhead"]
    assert overhead["n"] == 2
    assert overhead["p95_ms"] == 40000
    assert overhead["tokens"]["total"] == 30

    repeated = {
        key: {
            "seq": 1,
            "run_id": "run",
            "mode": "enforce",
            "source": "superagent",
            "inputs_digest": "same",
            "latency_ms": 100,
            "llm_tokens": 10,
        }
        for key in ("a", "b")
    }
    copied = build_report(
        load_snapshot({"gates": {"lines": {"L": {"assignment": repeated}}}})
    )["call_overhead"]
    assert copied["n"] == 1
    assert copied["tokens"]["total"] == 10


def test_running_lines_are_not_conclusive_pass_rate_samples():
    """A cohort that is still running is not a 0% pass rate."""
    dispatches = [
        {
            "lines": [
                {"id": str(index), "source": group, "status": status} for index in range(10)
            ],
            "gates": {"integration": {"phase": phase}},
        }
        for group, status, phase in (
            ("superagent", "RUNNING", "pending"),
            ("selector", "PASSED", "passed"),
        )
    ]
    report = build_report(load_snapshot(dispatches))
    superagent = report["pass_rate"]["superagent"]
    selector = report["pass_rate"]["selector"]
    assert superagent["status"] == "insufficient"
    assert (superagent["hits"], superagent["n"]) == (0, 0)
    assert (selector["hits"], selector["n"], selector["status"]) == (10, 10, "ok")
    assert report["counts"]["superagent"] == 10
    assert report["counts"]["selector"] == 10
    assert report["rollback"]["to_shadow"] is False
    assert rollback_due(report["spec"]["modes.rollback_due"]) is False
    bucket = report["by_provider_area"][0]["superagent"]
    assert (bucket["n"], bucket["status"]) == (0, "insufficient")
    for status in ("PENDING", "ASSIGNED", "REVIEWING"):
        open_rows = [{"source": "superagent", "status": status} for _ in range(10)]
        open_rows += [{"source": "selector", "passed": True} for _ in range(10)]
        held = build_report(load_snapshot({"lines": open_rows}))
        assert held["pass_rate"]["superagent"]["n"] == 0
        assert held["rollback"]["to_shadow"] is False

    finished = [
        {"source": group, "status": status}
        for group, status in (("superagent", "FAILED"), ("selector", "PASSED"))
        for _ in range(10)
    ]
    failed = build_report(load_snapshot({"lines": finished}))
    assert (failed["pass_rate"]["superagent"]["hits"], failed["pass_rate"]["superagent"]["n"]) == (
        0,
        10,
    )
    assert failed["rollback"]["to_shadow"] is True


def test_unfinished_g5_does_not_fail_passed_lines():
    """A PASSED line whose G5 is still in progress is not a pass-rate miss."""

    def dispatches(phase: str) -> list[dict[str, object]]:
        return [
            {
                "lines": [
                    {"id": str(index), "source": group, "status": "PASSED"} for index in range(10)
                ],
                "gates": {"integration": {"phase": phase if group == "superagent" else "passed"}},
            }
            for group in ("superagent", "selector")
        ]

    loaded = load_snapshot(dispatches("running"))
    running = [line for line in loaded if line.group() == "superagent"]
    assert running
    assert all(line.outcome_resolved is False and line.g5_passed is None for line in running)
    report = build_report(loaded)
    superagent = report["pass_rate"]["superagent"]
    selector = report["pass_rate"]["selector"]
    assert (superagent["hits"], superagent["n"], superagent["status"]) == (0, 0, "insufficient")
    assert (selector["hits"], selector["n"], selector["status"]) == (10, 10, "ok")
    assert report["counts"]["superagent"] == 10
    assert report["rollback"]["to_shadow"] is False
    assert rollback_due(report["spec"]["modes.rollback_due"]) is False
    bucket = report["by_provider_area"][0]["superagent"]
    assert (bucket["n"], bucket["status"]) == (0, "insufficient")
    for phase in ("pending", "merging"):
        held = build_report(load_snapshot(dispatches(phase)))
        assert held["pass_rate"]["superagent"]["n"] == 0
        assert held["rollback"]["to_shadow"] is False

    missed = build_report(load_snapshot(dispatches("failed")))
    assert (missed["pass_rate"]["superagent"]["hits"], missed["pass_rate"]["superagent"]["n"]) == (
        0,
        10,
    )
    assert missed["rollback"]["to_shadow"] is True


def test_shared_call_ignores_per_line_seq():
    """One invocation stored at different seqs on each line is one call."""

    def entry(seq: int) -> dict[str, object]:
        return {
            "seq": seq,
            "mode": "enforce",
            "source": "superagent",
            "inputs_digest": {"hash": "one-batch"},
            "latency_ms": 1000,
            "llm_tokens": 100,
        }

    payload = {
        "gates": {
            "lines": {
                "A": {
                    "assignment": {
                        "old": {"seq": 1, "mode": "off", "source": "selector"},
                        "new": entry(2),
                    }
                },
                "B": {"assignment": {"new": entry(1)}},
            }
        }
    }
    overhead = build_report(load_snapshot(payload))["call_overhead"]
    assert overhead["n"] == 1
    assert overhead["tokens"]["total"] == 100

    relaunch = {
        "gates": {
            "lines": {
                "L": {"assignment": {"a": entry(1), "b": entry(2)}},
            }
        }
    }
    second = build_report(load_snapshot(relaunch))["call_overhead"]
    assert second["n"] == 2
    assert second["tokens"]["total"] == 200


# --- integration with the superagent_assign package -------------------------


def test_unfinished_g5_lines_stay_out_of_cost_samples():
    """Reviewer repro: a running G5 cohort must not trigger the cost rollback."""
    payload = [
        {
            "lines": [
                {"id": str(i), "source": group, "status": "PASSED", "wall_clock_secs": wall}
                for i in range(10)
            ],
            "gates": {"integration": {"phase": phase}},
        }
        for group, wall, phase in (("superagent", 160, "running"), ("selector", 100, "passed"))
    ]
    report = build_report(load_snapshot(payload))
    assert report["cost"]["wall_clock"]["superagent"]["n"] == 0
    assert report["cost"]["wall_clock"]["selector"]["n"] == 10
    assert not report["rollback"]["to_shadow"]
    from src.tools.auto_dispatch.superagent_assign import modes

    assert not modes.rollback_due(report["spec"]["modes.rollback_due"])
    # Once that G5 passes, the same spend is a merged line's cost and fires.
    payload[0]["gates"]["integration"]["phase"] = "passed"
    finished = build_report(load_snapshot(payload))
    assert finished["cost"]["wall_clock"]["superagent"]["n"] == 10
    assert finished["rollback"]["reasons"] == ["cost_per_line"]
    assert modes.rollback_due(finished["spec"]["modes.rollback_due"])


def test_repeated_shared_batches_count_once_per_invocation():
    """Reviewer repro: two shared calls on lines A and B are two samples."""
    payload = {
        "gates": {
            "lines": {
                line: {
                    "assignment": {
                        str(seq): {
                            "seq": seq,
                            "run_id": f"{line}{seq}",
                            "mode": "enforce",
                            "source": "superagent",
                            "inputs_digest": "same",
                            "latency_ms": 100 if seq == 1 else 40000,
                            "llm_tokens": 10 if seq == 1 else 20,
                        }
                        for seq in (1, 2)
                    }
                }
                for line in ("A", "B")
            }
        }
    }
    overhead = build_report(load_snapshot(payload))["call_overhead"]
    assert overhead["n"] == 2
    assert overhead["tokens"]["total"] == 30
    assert overhead["tokens"]["n"] == 2
    assert overhead["p50_ms"] == 20050
    # A repeated copy of one run is the same occurrence, not a third call.
    entries = payload["gates"]["lines"]["A"]["assignment"]
    entries["dup"] = dict(entries["2"])
    again = build_report(load_snapshot(payload))["call_overhead"]
    assert again["n"] == 2
    assert again["tokens"]["total"] == 30


def test_script_vocabularies_match_the_package_types():
    import scripts.assign_report as report_module
    from src.tools.auto_dispatch.superagent_assign.types import ErrorKind, Source

    assert report_module._ERROR_REASONS == frozenset(kind.value for kind in ErrorKind)
    assert report_module._SUPERAGENT_SOURCES == frozenset(
        {Source.SUPERAGENT.value, Source.SUPERAGENT_REASK.value}
    )


def test_area_bucket_matches_the_context_bucket():
    from scripts.assign_report import area_bucket
    from src.tools.auto_dispatch.superagent_assign import context

    import scripts.assign_report as report_module

    assert report_module._TOP_LEVEL_AREAS == context._TOP_LEVEL_AREAS
    assert report_module._INVISIBLE_CATEGORIES == context._INVISIBLE_CATEGORIES
    assert report_module._INVISIBLE_CHARS == context._INVISIBLE_CHARS
    for path in (
        "web/src/x.ts",
        "web/**",
        "src/tools/auto_dispatch/machine.py",
        "src/tools/auto_dispatch/superagent_assign/**",
        "src/llms/native_cli/grok.py",
        "src/x.py",
        "src/tools/**",
        "migrations/versions/1.py",
        "docs/**",
        "tests/unit/x.py",
        "libs/ptc-cli/ptc_cli/a.py",
        ".github/workflows/ci.yml",
        "./scripts/assign_report.py",
        "foo/bar/baz/qux/q.py",
        "src/a:b/c/d.py",
        "\uff57\uff45\uff42/app.ts",
        "sr\u200bc/tools/auto_dispatch/x.py",
    ):
        assert area_bucket(path) == context.area_bucket(path), path
    assert area_bucket("") == "unspecified"
    assert area_bucket("../etc/passwd") == "unspecified"
    assert area_bucket("README.md") == "unspecified"


def _record_gates() -> dict[str, Any]:
    """Gates written by the real record writer, one line per outcome kind."""
    from src.tools.auto_dispatch.superagent_assign import record, types

    gates: dict[str, Any] = {}
    errors = {
        "E1": types.call_error("boom"),
        "E2": types.timeout_error("slow"),
        "E3": types.format_error("not a plan"),
    }
    for line_id, error in errors.items():
        gates = record.append_assignment(
            gates,
            line_id=line_id,
            run_id=f"{line_id}-r1",
            mode="enforce",
            source="selector_fallback",
            selector_choice={"model": "grok"},
            fallback_reason=error,
            latency_ms=50,
        )
    gates = record.append_assignment(
        gates,
        line_id="F1",
        run_id="F1-r1",
        mode="enforce",
        source="selector_fallback",
        choice={"model": "kimi", "effort": "default", "runtime": "default"},
        selector_choice={"model": "grok"},
        feasibility=[
            types.FeasibilityResult.infeasible("no_headroom"),
            types.FeasibilityResult.infeasible("no_headroom"),
        ],
        reask=True,
        fallback_reason=record.FALLBACK_INFEASIBLE_TWICE,
        latency_ms=60,
    )
    gates = record.append_assignment(
        gates,
        line_id="S1",
        run_id="S1-r1",
        mode="enforce",
        source="superagent",
        choice={"model": "stepfun", "effort": "high", "runtime": "native_cli"},
        selector_choice={"model": "grok"},
        feasibility=[types.FeasibilityResult.ok()],
        latency_ms=70,
        llm_tokens=12,
    )
    return gates


def test_records_from_the_writer_are_read_back():
    lines = {line.line_id: line for line in load_snapshot({"gates": _record_gates()})}
    for line_id in ("E1", "E2", "E3"):
        assert lines[line_id].assignment_error is True, line_id
        assert lines[line_id].fallback is True
        assert lines[line_id].group() is None
    assert lines["F1"].assignment_error is False
    assert lines["F1"].fallback is True
    assert lines["F1"].first_feasible is False
    assert lines["S1"].group() == "superagent"
    assert lines["S1"].provider == "stepfun"
    assert lines["S1"].first_feasible is True
    report = build_report(list(lines.values()))
    assert report["counts"]["assignment_errors"] == 3
    assert report["counts"]["fallbacks"] == 4
    assert report["call_overhead"]["tokens"]["total"] == 12
