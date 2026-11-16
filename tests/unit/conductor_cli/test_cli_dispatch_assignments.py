"""N73 L5: ``conductor dispatch assignments ID`` and the status rows."""

from __future__ import annotations

from src.conductor_cli.dispatch import VERBS, render_assignments, render_status


def test_assignments_is_a_verb_and_renders_each_attempt() -> None:
    assert "assignments" in VERBS
    rows = render_assignments(
        {
            "dispatch_id": "d1",
            "lines": {
                "l1": [
                    {"seq": 1, "mode": "enforce", "source": "superagent_reask",
                     "choice": {"model": "glm", "effort": "default", "reason": "re-asked"},
                     "selector_choice": {"model": "kimi"},
                     "feasibility": [{"status": "infeasible", "reason_code": "reserved_role"},
                                     {"status": "ok"}]}
                ]
            },
        }
    )
    assert rows[0] == "d1 assignments"
    assert any("superagent_reask pick glm/default selector kimi" in row for row in rows)
    assert any("feasibility reserved_role,ok" in row for row in rows)
    assert any("reason: re-asked" in row for row in rows)
    assert render_assignments({"dispatch_id": "d2", "lines": {}})[1] == "  (none recorded)"


def test_status_shows_the_assignment_rows() -> None:
    rows = render_status(
        {
            "id": "d1",
            "state": "RUNNING",
            "progress_pct": 10,
            "integrate_branch": "integrate/d1",
            "lines": [],
            "gates": {},
            "assignment": {
                "l1": {"mode": "shadow", "source": "superagent", "model": "stepfun",
                       "effort": "high", "runtime": "default", "selector_choice": "kimi"}
            },
        },
        now=0.0,
    )
    assert "assignment (Super-agent vs selector)" in rows
    assert any("pick stepfun/high/default  selector kimi" in row for row in rows)
