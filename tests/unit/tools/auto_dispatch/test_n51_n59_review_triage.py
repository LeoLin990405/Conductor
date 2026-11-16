"""N51 (blockers need a repro) and N59 (INCONCLUSIVE verdict), module level."""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch import review_triage
from src.tools.auto_dispatch.auto_merge import (
    REASON_INCONCLUSIVE,
    LineApprovalFacts,
    auto_approve_decision,
    review_history_from_snapshot,
)
from src.tools.auto_dispatch.review_schema import validate_verdict
from src.tools.auto_dispatch.review_triage import (
    EVIDENCE_REQUEST,
    INCONCLUSIVE_MODE_ENV,
    REPRO_MODE_ENV,
    REVIEW_RUBRIC,
    REVIEW_STATUS_HISTORY,
    REVIEW_TRIAGE,
    ReviewVerdict,
    blocker_missing,
    record_triage,
    triage_verdict,
    with_status,
)
from src.tools.auto_dispatch.types import Verdict

GOOD = (
    "src/app.py:42 - scenario: an empty list -> IndexError - "
    "repro: `pytest tests/test_app.py::test_empty -q`"
)
VAGUE = "the error handling looks fragile"


def _env(repro: str = "", inconclusive: str = "") -> dict[str, str]:
    env: dict[str, str] = {}
    if repro:
        env[REPRO_MODE_ENV] = repro
    if inconclusive:
        env[INCONCLUSIVE_MODE_ENV] = inconclusive
    return env


# --- N51: what a reproducible blocker is -------------------------------------


def test_a_blocker_with_file_line_scenario_and_repro_is_complete() -> None:
    assert blocker_missing(GOOD) == []


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        (VAGUE, ["file_line", "scenario", "repro"]),
        ("src/app.py:42 is wrong", ["scenario", "repro"]),
        ("src/app.py:42 - scenario: empty input -> crash", ["repro"]),
        ("scenario: x -> y - repro: `pytest -q`", ["file_line"]),
        ("src/app.py:42 fails when the list is empty; run `python -c 'import app'`", []),
    ],
)
def test_what_a_blocker_lacks_is_named(text: str, missing: list[str]) -> None:
    assert blocker_missing(text) == missing


def test_the_rubric_asks_for_the_n51_shape_and_the_operator_exit_rule() -> None:
    assert "file:line" in REVIEW_RUBRIC
    assert "repro" in REVIEW_RUBRIC
    assert "executable repro" in REVIEW_RUBRIC
    assert "operator exit" in REVIEW_RUBRIC and "CLI test" in REVIEW_RUBRIC
    assert '"inconclusive"' in REVIEW_RUBRIC and "needed_evidence" in REVIEW_RUBRIC


def test_shadow_is_the_default_and_keeps_the_block() -> None:
    verdict = Verdict.blocked([VAGUE, GOOD])
    triage = triage_verdict(verdict, environ={})
    assert triage.verdict is verdict
    assert triage.action == ""
    assert triage.record["repro_mode"] == "shadow"
    assert triage.record["would"] == "downgrade"
    assert triage.record["downgraded_count"] == 1
    assert triage.record["downgraded"][0]["blocker"] == VAGUE


def test_enforce_downgrades_only_the_blockers_without_a_repro() -> None:
    triage = triage_verdict(Verdict.blocked([VAGUE, GOOD]), environ=_env("enforce"))
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == [GOOD]
    assert triage.verdict.should_fix == [VAGUE]


def test_enforce_passes_a_review_left_with_no_reproducible_blocker() -> None:
    triage = triage_verdict(Verdict.blocked([VAGUE]), environ=_env("enforce"))
    assert triage.verdict.passed is True
    assert triage.verdict.blockers == []
    assert triage.verdict.should_fix == [VAGUE]
    assert triage.record["passed_after_downgrade"] is True


def test_security_and_ruling_blockers_are_never_downgraded() -> None:
    triage = triage_verdict(
        Verdict.blocked(["security: token is logged"]), environ=_env("enforce")
    )
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == ["security: token is logged"]


def test_off_records_nothing_and_changes_nothing() -> None:
    verdict = Verdict.blocked([VAGUE])
    triage = triage_verdict(verdict, environ=_env("off", "off"))
    assert triage.verdict is verdict and triage.record == {}


def test_a_pass_is_never_touched() -> None:
    verdict = Verdict.ok()
    triage = triage_verdict(verdict, environ=_env("enforce", "enforce"))
    assert triage.verdict is verdict and triage.action == ""


# --- N59: INCONCLUSIVE --------------------------------------------------------


def _inconclusive(*needed: str) -> ReviewVerdict:
    return ReviewVerdict(
        passed=False,
        blockers=[],
        verdict="cannot tell",
        status="inconclusive",
        needed_evidence=list(needed),
    )


def test_with_status_reads_the_n59_keys_and_drops_unknown_statuses() -> None:
    base = Verdict.blocked([GOOD])
    enriched = with_status(base, {"status": "INCONCLUSIVE", "needed_evidence": ["G2 log"]})
    assert isinstance(enriched, ReviewVerdict)
    assert enriched.status == "inconclusive"
    assert enriched.needed_evidence == ["G2 log"]
    assert enriched.passed is False and enriched.blockers == [GOOD]
    assert with_status(base, {"status": "maybe"}) is base
    assert with_status(base, None) is base


def test_shadow_inconclusive_still_blocks_and_records_the_request() -> None:
    triage = triage_verdict(_inconclusive("pytest output for test_x"), environ={})
    assert triage.action == ""
    assert triage.verdict.passed is False
    assert triage.record["status"] == "inconclusive"
    assert triage.record["would"] == EVIDENCE_REQUEST
    assert triage.record["needed_evidence"] == ["pytest output for test_x"]


def test_enforce_inconclusive_is_an_evidence_request_when_the_exit_exists() -> None:
    triage = triage_verdict(
        _inconclusive("G2 log"), environ=_env(inconclusive="enforce"), exit_ready=True
    )
    assert triage.action == EVIDENCE_REQUEST
    assert triage.record["action"] == EVIDENCE_REQUEST


def test_enforce_without_an_operator_exit_acts_as_shadow() -> None:
    triage = triage_verdict(
        _inconclusive("G2 log"), environ=_env(inconclusive="enforce"), exit_ready=False
    )
    assert triage.action == ""
    assert triage.record["enforce_blocked"] == "no_operator_exit"


def test_shadow_inconclusive_never_becomes_an_empty_fix_round() -> None:
    """Repro enforce downgrades the vague blocker; the evidence request blocks."""
    verdict = ReviewVerdict(
        passed=False, blockers=[VAGUE], status="inconclusive", needed_evidence=["log"]
    )
    triage = triage_verdict(verdict, environ=_env("enforce"))
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == ["evidence: log"]


def test_record_triage_keeps_a_status_history() -> None:
    snap: dict[str, object] = {}
    record_triage(snap, triage_verdict(_inconclusive("log"), environ={}), reviewer="codex", head="abc")
    assert snap[REVIEW_TRIAGE]["reviewer"] == "codex"
    assert snap[REVIEW_TRIAGE]["head"] == "abc"
    assert snap[REVIEW_STATUS_HISTORY] == ["inconclusive"]


def test_auto_merge_sees_an_earlier_inconclusive_in_the_snapshot() -> None:
    snap: dict[str, object] = {
        "history": [{"gate": "primary_review", "result": "merge"}],
        REVIEW_STATUS_HISTORY: ["inconclusive", "pass"],
    }
    history = review_history_from_snapshot(snap)
    assert "inconclusive" in history
    decision = auto_approve_decision(
        [
            LineApprovalFacts(
                line_id="a",
                g2_passed=True,
                reviews=["merge"],
                review_history=history,
                diff_files=["docs/x.md"],
                write_scope=["docs/x.md"],
            )
        ],
        g5_passed=True,
        # N73: S7 auto-approve is off by default; turn it on to reach the triage check.
        environ={"CONDUCTOR_DISPATCH_AUTO_APPROVE": "1"},
        globs=(),
    )
    assert decision.reason == REASON_INCONCLUSIVE


# --- the schema ----------------------------------------------------------------


def test_schema_accepts_n59_keys_and_the_legacy_shape() -> None:
    legacy = {"passed": False, "blockers": [GOOD], "verdict": "no"}
    assert validate_verdict(legacy) is legacy
    full = dict(legacy, status="inconclusive", needed_evidence=["log"])
    assert validate_verdict(full) is full


@pytest.mark.parametrize(
    "extra",
    [
        {"status": "maybe"},
        {"status": 1},
        {"needed_evidence": "log"},
        {"needed_evidence": [1]},
    ],
)
def test_schema_rejects_bad_n59_values(extra: dict[str, object]) -> None:
    value = {"passed": False, "blockers": [], "verdict": "no", **extra}
    assert validate_verdict(value) is None


def test_inconclusive_exit_follows_the_operator_park_list(monkeypatch) -> None:
    from src.tools.auto_dispatch import operator

    monkeypatch.setattr(operator, "_RESUMABLE_REVIEW_PARKS", frozenset({"x"}))
    assert review_triage.inconclusive_exit_ready() is False
    monkeypatch.setattr(
        operator, "_RESUMABLE_REVIEW_PARKS", frozenset({review_triage.REVIEW_INCONCLUSIVE})
    )
    assert review_triage.inconclusive_exit_ready() is True


# --- hooks in files this line does not own (skipped until integrated) ----------

def _source(module) -> str:
    from pathlib import Path

    return Path(module.__file__).read_text(encoding="utf-8")


def _execution_hooked() -> bool:
    from src.tools.auto_dispatch import execution

    return "review_triage" in _source(execution)


def _cli_hooked() -> bool:
    from src.conductor_cli import dispatch as cli_dispatch

    return "review_evidence_status" in _source(cli_dispatch)


@pytest.mark.skipif(not _execution_hooked(), reason="execution.py hook not integrated yet")
def test_the_review_prompt_and_both_parsers_carry_n59() -> None:
    from src.tools.auto_dispatch.execution import (
        _REVIEW_SYSTEM,
        _parse_verdict,
        _structured_verdict,
    )

    assert REVIEW_RUBRIC in _REVIEW_SYSTEM
    reply = {
        "passed": False,
        "blockers": [],
        "verdict": "need evidence",
        "status": "inconclusive",
        "needed_evidence": ["G2 log"],
    }
    structured = _structured_verdict(reply)
    assert isinstance(structured, ReviewVerdict) and structured.status == "inconclusive"
    import json

    parsed = _parse_verdict("Notes.\n" + json.dumps(reply))
    assert isinstance(parsed, ReviewVerdict) and parsed.needed_evidence == ["G2 log"]


@pytest.mark.skipif(not _cli_hooked(), reason="conductor_cli/dispatch.py hook not integrated yet")
def test_dispatch_status_shows_inconclusive_and_its_exit() -> None:
    from src.conductor_cli import dispatch as cli_dispatch

    record = {
        REVIEW_TRIAGE: {
            "status": "inconclusive",
            "needed_evidence": ["G2 log"],
            "action": EVIDENCE_REQUEST,
        }
    }
    assert any("INCONCLUSIVE" in item for item in cli_dispatch._gate_lines(record))
    hints = cli_dispatch._line_hints(
        "d1", "a", {"status": "BLOCKED", "failure_reason": review_triage.REVIEW_INCONCLUSIVE}
    )
    assert any("conductor dispatch unblock d1 a" in item for item in hints)


def test_a_pass_that_reports_inconclusive_or_fail_never_merges() -> None:
    # Integration review: a reply with passed true but status inconclusive (or
    # fail) must not merge on the boolean alone, in any mode.
    from src.tools.auto_dispatch.types import Verdict

    for status in ("inconclusive", "fail"):
        verdict = with_status(
            Verdict(passed=True, blockers=[], verdict="ok"),
            {"status": status, "needed_evidence": ["the pytest output"]},
        )
        assert verdict.passed is False
        assert verdict.blockers
    kept = with_status(Verdict(passed=True, blockers=[], verdict="ok"), {"status": "pass"})
    assert kept.passed is True


def test_the_rubric_does_not_tell_reviewers_to_skip_blocking() -> None:
    # Shadow is the default: the prompt must not itself downgrade blockers.
    assert "should-fix" not in REVIEW_RUBRIC
    assert "passed false" in REVIEW_RUBRIC


# --- Review fix (owner decision, 2026-11-14): tags anywhere are exempt; a repro is real ---


@pytest.mark.parametrize(
    "blocker",
    [
        "the token handling is wrong (security)",
        "SECURITY - token logged",
        "scope: edits src/billing.py outside write scope",
        "src/auth.py:12 - security: token logged",
        "a.py:3 - scope: edits outside write scope",
        "edits src/billing.py [scope]",
        "edits src/billing.py (Scope)",
        "the Security review found a token in the log",
        "SCOPE - edits outside the task",
    ],
)
def test_enforce_never_downgrades_a_security_or_scope_tagged_blocker(blocker: str) -> None:
    triage = triage_verdict(Verdict.blocked([blocker]), environ=_env("enforce"))
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == [blocker]
    assert "downgraded" not in triage.record


@pytest.mark.parametrize(
    "blocker",
    [
        "the telescope lens handling looks fragile",
        "the fixture is module-scoped and looks fragile",
        "a scoped variable looks fragile",
    ],
)
def test_enforce_still_downgrades_an_unrelated_word(blocker: str) -> None:
    triage = triage_verdict(Verdict.blocked([blocker]), environ=_env("enforce"))
    assert triage.verdict.passed is True
    assert triage.verdict.should_fix == [blocker]


_NO_REPRO = "src/app.py:42 - scenario: an empty list -> IndexError - repro:"


@pytest.mark.parametrize(
    "tail", ["", " ", " n/a", " N/A", " none", " tbd", " TBD.", " -", " --", " todo", " na"]
)
def test_a_repro_marker_without_a_repro_does_not_count(tail: str) -> None:
    blocker = _NO_REPRO + tail
    assert blocker_missing(blocker) == ["repro"]
    triage = triage_verdict(Verdict.blocked([blocker]), environ=_env("enforce"))
    assert triage.verdict.passed is True
    assert triage.verdict.blockers == []
    assert triage.record["downgraded"][0]["missing"] == ["repro"]


@pytest.mark.parametrize(
    "tail",
    [
        " `pytest tests/test_app.py -q`",
        " `app.parse([])`",
        " pytest tests/test_app.py::test_empty -q",
        " uv run python -c 'import app; app.parse([])'",
        " $ make test",
        " tests/test_app.py::test_empty",
        " 1. call parse([]) 2. read result.first",
        " call parse([]); then read result.first",
    ],
)
def test_a_command_or_concrete_steps_is_a_repro(tail: str) -> None:
    blocker = _NO_REPRO + tail
    assert blocker_missing(blocker) == []
    triage = triage_verdict(Verdict.blocked([blocker]), environ=_env("enforce"))
    assert triage.verdict.passed is False
    assert triage.verdict.blockers == [blocker]


def test_a_vague_repro_phrase_is_not_a_repro() -> None:
    assert blocker_missing(_NO_REPRO + " run the tests") == ["repro"]
