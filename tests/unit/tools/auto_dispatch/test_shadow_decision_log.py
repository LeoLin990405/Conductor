"""Shadow modes write one structured log line per decision a rollout review counts.

Four checks ship behind ``off|shadow|enforce`` and change dispatch/review
results only when promoted. The log is the stream a rollout review counts
(how many judgements, how many would change a result, which ones look wrong),
so each shadow branch must write exactly one ``shadow_decision`` line and the
enforce branch must write none. These tests pin the shared format and each of
the three auto-dispatch modes; the Ticket Worker sweep has its own test in
``tests/unit/server/services/autonomy/test_ticket_silent_worker.py``.
"""

from __future__ import annotations

import logging

import pytest

from src.tools.auto_dispatch import dedup, review_strict, review_triage
from src.tools.auto_dispatch.types import Verdict
from src.utils.shadow_log import MAX_VALUE_CHARS, shadow_decision

GOOD = (
    "src/app.py:42 - scenario: an empty list -> IndexError - "
    "repro: `pytest tests/test_app.py::test_empty -q`"
)
VAGUE = "the error handling looks fragile"


def _shadow_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.message for record in caplog.records if "shadow_decision" in record.message]


# --- the one format -----------------------------------------------------------


def test_one_line_carries_the_shared_fields_and_is_one_token_each(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        line = shadow_decision(
            logging.getLogger("tests.shadow"),
            "review_repro",
            would_change=True,
            subject="t-1",
            current="merge",
            enforced="fix",
            reason="no_marker",
            extra_field="a b=c",
        )
    assert line == (
        "shadow_decision mode=review_repro would_change=true subject=t-1 "
        "current=merge enforced=fix reason=no_marker extra_field=a_b:c"
    )
    assert caplog.records[-1].message == line
    assert caplog.records[-1].levelno == logging.WARNING


def test_missing_and_oversized_values_stay_one_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        line = shadow_decision(
            logging.getLogger("tests.shadow"),
            "mode with space",
            would_change=False,
            subject="",
            current=None,
            enforced="x" * (MAX_VALUE_CHARS + 40),
            reason="",
            long="y" * (MAX_VALUE_CHARS + 40),
        )
    assert "mode=mode_with_space" in line
    assert "would_change=false" in line
    assert "subject=- current=-" in line
    assert "reason=" not in line
    assert "x" * (MAX_VALUE_CHARS + 1) not in line
    assert "y" * (MAX_VALUE_CHARS + 1) not in line
    assert "\n" not in line


# --- dedup: it would reuse an in-flight or done entry -------------------------

KEY = "dispatch|issue:59|l1|abcdef1"


def _ledger(state: str = "in_flight") -> dict[str, dict[str, object]]:
    return {
        KEY: {
            "key": KEY,
            "ref": "issue:59",
            "state": state,
            "attempts": 2,
            "duplicates": 1,
        }
    }


def test_dedup_shadow_logs_the_reuse_it_did_not_do(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        decision = dedup.decide(_ledger(), KEY, environ={})
    # The decision is unchanged: a shadow duplicate still runs.
    assert decision.action == "new" and decision.would_reuse is True
    [line] = _shadow_lines(caplog)
    assert line.startswith("shadow_decision mode=dedup would_change=true")
    assert f"subject={KEY}" in line
    assert "current=new enforced=reuse" in line
    assert "reason=duplicate_in_flight" in line
    assert "existing_ref=issue:59" in line
    assert "attempts=2" in line


@pytest.mark.parametrize(
    ("env", "ledger"),
    [
        ({dedup.MODE_ENV: "enforce"}, _ledger()),
        ({}, {}),
    ],
)
def test_dedup_logs_no_shadow_line_when_there_is_nothing_to_count(
    caplog: pytest.LogCaptureFixture,
    env: dict[str, str],
    ledger: dict[str, dict[str, object]],
) -> None:
    with caplog.at_level(logging.WARNING):
        dedup.decide(ledger, KEY, environ=env)
    assert _shadow_lines(caplog) == []


@pytest.mark.parametrize(
    ("ledger", "force", "reason"),
    [
        # An entry exists but the operator forced past it.
        (_ledger(), True, "operator_forced"),
        # An entry exists but its state is never reused.
        (_ledger("failed"), False, "not_reusable_failed"),
    ],
)
def test_dedup_shadow_logs_the_denominator_it_would_not_change(
    caplog: pytest.LogCaptureFixture,
    ledger: dict[str, dict[str, object]],
    force: bool,
    reason: str,
) -> None:
    """Every ledger hit is logged, reusable or not: T4 divides by the hits.

    The old code logged only ``would_change=true``, so a ``failed`` entry or an
    operator override was invisible and the denominator undercounted.
    """
    with caplog.at_level(logging.WARNING):
        decision = dedup.decide(ledger, KEY, force=force, environ={})
    assert decision.would_reuse is False
    [line] = _shadow_lines(caplog)
    assert line.startswith("shadow_decision mode=dedup would_change=false")
    assert "current=new enforced=new" in line
    assert f"reason={reason}" in line
    assert f"subject={KEY}" in line


# --- review_repro: a blocker without a repro would stop blocking -------------


def test_review_repro_shadow_logs_the_merge_it_did_not_take(
    caplog: pytest.LogCaptureFixture,
) -> None:
    verdict = Verdict.blocked([VAGUE])
    with caplog.at_level(logging.WARNING):
        triage = review_triage.triage_verdict(verdict, environ={})
    # Shadow keeps the original verdict and the block.
    assert triage.verdict is verdict and triage.verdict.passed is False
    [line] = _shadow_lines(caplog)
    assert line.startswith("shadow_decision mode=review_repro would_change=true")
    assert "current=block enforced=merge" in line
    assert "reason=no_reproducible_blockers" in line
    assert "missing=file_line,scenario,repro" in line
    assert "blockers_down=1 blockers_left=0" in line
    assert "blocker=the_error_handling_looks_fragile" in line


def test_review_repro_a_partial_downgrade_does_not_change_the_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        triage = review_triage.triage_verdict(Verdict.blocked([VAGUE, GOOD]), environ={})
    assert triage.verdict.passed is False
    [line] = _shadow_lines(caplog)
    assert "would_change=false" in line
    assert "current=block enforced=block" in line
    assert "reason=some_blockers_not_reproducible" in line
    assert "blockers_down=1 blockers_left=1" in line


def test_review_repro_logs_a_verdict_whose_blockers_all_pass_n51(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A blocked verdict where nothing would be downgraded is the denominator.

    The old code returned before logging it, so T4 could count changed reviews
    but not how many reviews the check looked at.
    """
    with caplog.at_level(logging.WARNING):
        triage = review_triage.triage_verdict(Verdict.blocked([GOOD]), environ={})
    assert triage.verdict.passed is False
    [line] = _shadow_lines(caplog)
    assert line.startswith("shadow_decision mode=review_repro would_change=false")
    assert "reason=all_blockers_reproducible" in line
    assert "blockers_down=0 blockers_left=1" in line


def test_review_repro_names_the_line_as_subject_and_the_file_line_as_location(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The subject must be the line id, not a ``file:line`` grabbed from text.

    A ``file:line`` cannot be joined back to a review and cannot de-duplicate
    a line, so it is an ``extra`` field, never ``subject``.
    """
    blocker = "src/app.py:42 - the error handling looks fragile"
    with caplog.at_level(logging.WARNING):
        review_triage.triage_verdict(
            Verdict.blocked([blocker]), subject="line-9", environ={}
        )
    [line] = _shadow_lines(caplog)
    assert "subject=line-9" in line
    assert "location=src/app.py:42" in line


def test_review_repro_enforce_writes_no_shadow_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        triage = review_triage.triage_verdict(
            Verdict.blocked([VAGUE]), environ={review_triage.REPRO_MODE_ENV: "enforce"}
        )
    # Enforce is unchanged by the log: the vague blocker no longer blocks.
    assert triage.verdict.passed is True
    assert _shadow_lines(caplog) == []


# --- review_strict: enforce would park review_inconclusive instead -------------


def test_review_strict_shadow_logs_the_park_enforce_would_pick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    snapshot: dict[str, object] = {"line_id": "line-7"}
    with caplog.at_level(logging.WARNING):
        review_strict.record_attempt(
            snapshot,
            reviewer="codex,glm",
            kind=review_strict.PROSE,
            retried=True,
            outcome=review_strict.WOULD_INCONCLUSIVE,
            mode="shadow",
            now=0,
        )
    assert snapshot[review_strict.RECORD_KEY]["total"] == 1  # type: ignore[index]
    [line] = _shadow_lines(caplog)
    assert line.startswith("shadow_decision mode=review_strict would_change=true")
    assert "subject=line-7" in line
    assert "current=park_review_unparseable enforced=park_review_inconclusive" in line
    assert "reviewer=codex,glm" in line
    assert "kind=prose" in line
    assert "retried=true" in line


@pytest.mark.parametrize(
    ("outcome", "would_change", "current", "reason"),
    [
        (review_strict.VALID, "false", "accept_reply", "usable_reviewer_reply"),
        ("unparseable", "false", "retry_reviewer", "reviewer_reply_unusable"),
        (
            review_strict.WOULD_INCONCLUSIVE,
            "true",
            "park_review_unparseable",
            "reviewer_reply_unusable_after_retry",
        ),
    ],
)
def test_review_strict_shadow_logs_every_attempt_as_the_denominator(
    caplog: pytest.LogCaptureFixture,
    outcome: str,
    would_change: str,
    current: str,
    reason: str,
) -> None:
    """Every recorded attempt is one judgement, not only the would-park one."""
    with caplog.at_level(logging.WARNING):
        review_strict.record_attempt(
            {"line_id": "line-7"},
            reviewer="codex",
            kind=review_strict.PROSE,
            retried=False,
            outcome=outcome,
            mode="shadow",
            now=0,
        )
    [line] = _shadow_lines(caplog)
    assert f"would_change={would_change}" in line
    assert f"current={current}" in line
    assert f"reason={reason}" in line
    assert "subject=line-7" in line


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("enforce", "inconclusive"),
        ("off", "would_inconclusive"),
    ],
)
def test_review_strict_logs_no_shadow_line_outside_shadow(
    caplog: pytest.LogCaptureFixture, mode: str, outcome: str
) -> None:
    with caplog.at_level(logging.WARNING):
        review_strict.record_attempt(
            {"line_id": "line-7"},
            reviewer="codex",
            kind=review_strict.PROSE,
            retried=True,
            outcome=outcome,
            mode=mode,
            now=0,
        )
    assert _shadow_lines(caplog) == []
