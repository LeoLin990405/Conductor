"""A reserve that never settles still spends its slot."""

from __future__ import annotations

import json

from src.tools.auto_dispatch.harness_eval.budget import Ledger

from harness_eval_fakes import budget_spec


def test_unsettled_reserve_counts_and_a_refusal_is_not_written(tmp_path) -> None:
    path = tmp_path / "ledger.jsonl"
    budget = budget_spec(max_runs=2, max_models=1)
    first = Ledger(path, budget, clock=lambda: 10.0)
    assert first.reserve("run", "aaaaaaa1", "stepfun").ok
    assert not any(item["event"] == "settle" for item in first.events)

    resumed = Ledger(path, budget, clock=lambda: 11.0)
    assert resumed.state().runs_reserved == 1
    assert resumed.reserve("run", "aaaaaaa2", "stepfun").ok
    refused = resumed.reserve("run", "aaaaaaa3", "stepfun")
    assert refused.ok is False
    assert refused.reason == "max_runs"
    text = path.read_text(encoding="utf-8")
    assert "aaaaaaa3" not in text
    assert text.count('"event": "reserve"') == 2


def test_reviewer_model_does_not_spend_the_worker_pool(tmp_path) -> None:
    ledger = Ledger(tmp_path / "ledger.jsonl", budget_spec(max_models=1, max_reviews=1))
    assert ledger.reserve("run", "aaaaaaa1", "stepfun").ok
    assert ledger.reserve("review", "aaaaaaa1", "codex").ok
    assert ledger.state().models == frozenset({"stepfun"})
    refused = ledger.reserve("run", "aaaaaaa2", "glm")
    assert refused.reason == "max_models"
    reviews = ledger.reserve("review", "aaaaaaa2", "codex")
    assert reviews.reason == "max_reviews"


def test_wall_clock_and_reload_keep_the_spent_slot(tmp_path) -> None:
    clock = {"now": 0.0}
    path = tmp_path / "ledger.jsonl"
    budget = budget_spec(max_wall_minutes=1)
    ledger = Ledger(path, budget, clock=lambda: clock["now"])
    assert ledger.reserve("run", "aaaaaaa1", "stepfun").ok
    clock["now"] = 60.0
    refused = ledger.reserve("run", "aaaaaaa2", "stepfun")
    assert refused.reason == "max_wall_minutes"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [row["run8"] for row in rows] == ["aaaaaaa1"]


def test_two_ledgers_on_one_file_cannot_double_the_budget(tmp_path) -> None:
    # A second process opened the same ledger before the first reserved.
    budget = budget_spec(max_runs=1, max_models=1, max_reviews=1)
    path = tmp_path / "ledger.jsonl"
    first = Ledger(path, budget)
    second = Ledger(path, budget)
    assert first.reserve("run", "aaaaaaaa", "stepfun").ok is True
    refused = second.reserve("run", "bbbbbbbb", "stepfun")
    assert refused.ok is False
    assert refused.reason == "max_runs"
    assert first.reserve("review", "aaaaaaaa", "codex").ok is True
    assert second.reserve("review", "aaaaaaaa", "codex").ok is False
