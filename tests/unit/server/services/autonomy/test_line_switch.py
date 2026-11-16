from datetime import datetime, timezone

import pytest

from src.server.services.autonomy import line_switch as ls

A = ls.ModelRef("codex", "gpt-x")
B = ls.ModelRef("kimi", "k2")
C = ls.ModelRef("grok", "g4")
POLICY = ls.SwitchPolicy(stall_seconds=600, max_failures=3)


def test_detect_stuck_reasons():
    assert ls.detect_stuck(ls.LineState(A, 10, 0), POLICY) is None
    assert ls.detect_stuck(ls.LineState(A, 600, 0), POLICY) == ls.REASON_STALL
    assert ls.detect_stuck(ls.LineState(A, 0, 3), POLICY) == ls.REASON_FAILURES
    assert ls.detect_stuck(ls.LineState(A, 9999, 9), POLICY, ["CODEX"]) == ls.REASON_BREAKER


def test_pick_skips_current_reviewer_and_open_breakers():
    assert ls.pick_target([A, B, C], A, reviewer=B) == C
    assert ls.pick_target([A, B, C], A, open_providers=["kimi"]) == C
    assert ls.pick_target([A, B], A) == B


def test_pick_reviewer_match_is_case_insensitive():
    with pytest.raises(ls.NoEligibleTarget):
        ls.pick_target([ls.ModelRef("KIMI", "K2")], A, reviewer=B)


def test_pick_raises_when_nothing_eligible():
    with pytest.raises(ls.NoEligibleTarget):
        ls.pick_target([A, B], A, reviewer=B)


def test_handoff_is_bounded_and_keeps_recent_messages():
    msgs = [{"role": "user", "content": f"m{i} " + "x" * 500} for i in range(20)]
    h = ls.build_handoff(msgs, "d" * 9000, [f"finding {i}" for i in range(100)], limit=1000)
    assert "m19" in h.summary and "m0 " not in h.summary
    assert len(h.diff) <= 400 and len(h.summary) <= 400
    assert sum(len(f) for f in h.findings) <= 200
    assert len(ls.render_handoff(h)) < 1400


def test_inject_handoff_puts_context_before_prompt():
    h = ls.Handoff("user: hi", "diff --git", ("fix null",))
    out = ls.inject_handoff("DO THE TASK", h)
    assert out.endswith("DO THE TASK")
    assert out.index("Last diff") < out.index("Open findings") < out.index("DO THE TASK")


def test_plan_switch_records_and_never_picks_reviewer():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    plan = ls.plan_switch(
        ls.LineState(A), [B, C], ls.Handoff("s"), reason=ls.REASON_STALL, reviewer=B, now=now, attempt=2
    )
    assert plan.target == C
    assert plan.record["from"] == A.as_dict() and plan.record["to"] == C.as_dict()
    assert plan.record["reason"] == "stall" and plan.record["attempt"] == 2
    assert plan.record["handoff_chars"] == len(plan.handoff_text)


def test_explicit_target_must_still_pass_rules():
    with pytest.raises(ls.NoEligibleTarget):
        ls.plan_switch(ls.LineState(A), [C], ls.Handoff("s"), reason="manual", reviewer=B, explicit=B)
    with pytest.raises(ls.NoEligibleTarget):
        ls.plan_switch(ls.LineState(A), [], ls.Handoff("s"), reason="manual", explicit=A)
