"""N73 L4 (PRD 5.1): the reviewer sees the assignment record, gate findings and scope."""

from __future__ import annotations

from src.tools.auto_dispatch.superagent_assign.evidence import diff_files, review_section
from src.tools.auto_dispatch.superagent_assign.record import append_assignment
from src.tools.auto_dispatch.types import Line

DIFF = (
    "diff --git a/docs/a.md b/docs/a.md\n+x\n"
    "diff --git a/src/tools/auto_dispatch/machine.py b/src/tools/auto_dispatch/machine.py\n+y\n"
)


def _line(scope=("docs/**",)):
    return Line(id="l1", name="l1", task="t", write_scope=tuple(scope))


def test_nothing_to_say_keeps_the_prompt_unchanged() -> None:
    assert review_section({}, _line(), "diff --git a/docs/a.md b/docs/a.md\n") == ""


def test_scope_high_risk_findings_and_assignment_are_listed() -> None:
    snap = append_assignment(
        {"lines": {"l1": {}}},
        line_id="l1",
        run_id="l1:assign:0",
        mode="enforce",
        source="superagent",
        choice={"line_id": "l1", "model": "stepfun", "effort": "high", "runtime": "default",
                "reason": "9/10 on docs", "confidence": "high", "model_id": ""},
        selector_choice={"model": "kimi"},
        feasibility=[{"status": "ok"}],
    )["lines"]["l1"]
    snap["primary_verdict"] = "SENTINEL-VERDICT"
    snap["gates"] = {"G2": {"shadow": [{"check": "scope_check", "would_refuse": "g2_scope_conflict",
                                        "finding": "scope: src/x.py", "record": {"files": ["src/x.py"]}}]}}
    text = review_section(snap, _line(), DIFF)
    assert "model=stepfun effort=high" in text and "selector_would_pick=kimi" in text
    assert "gate finding scope_check" in text
    assert "outside write_scope: src/tools/auto_dispatch/machine.py" in text
    assert "high-risk paths touched: yes" in text
    assert "BLOCK with file:line" in text
    # R6 L5: never another reviewer's conclusion.
    assert "SENTINEL-VERDICT" not in text


def test_diff_files_parse() -> None:
    assert diff_files(DIFF) == ["docs/a.md", "src/tools/auto_dispatch/machine.py"]
