"""N63: dispatch status shows a review that fell back, and how to resume a quota park."""

from __future__ import annotations

from src.conductor_cli.dispatch import render_status


def _summary(**gates):
    return {
        "id": "d1",
        "goal": "g",
        "state": "REVIEW",
        "lines": [{"id": "a", "status": "BLOCKED", "failure_reason": "reviewers_quota_exhausted"}],
        "gates": gates,
        "outcomes": {},
    }


def test_a_reviewer_fallback_is_shown() -> None:
    summary = _summary(
        scheduler={
            "audit": [
                {
                    "kind": "reviewer_fallback",
                    "line_id": "a",
                    "stage": "primary_review",
                    "from": "codex",
                    "reviewer": "stepfun",
                }
            ]
        }
    )
    text = "\n".join(render_status(summary, now=0.0))
    assert "a: primary_review fell back codex → stepfun" in text


def test_a_reviewers_quota_park_hints_unblock() -> None:
    text = "\n".join(render_status(_summary(), now=0.0))
    assert "conductor dispatch unblock d1 a" in text
