"""Strict reviewer output: classify, retry once from the schema, then INCONCLUSIVE.

A reviewer that replies with prose or nothing has not reviewed the author's
work, so neither reply may become PASS or FAIL. These tests pin the helper
the drive wires around ``CrossReviewer.review_line``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from src.tools.auto_dispatch import review_strict
from src.tools.auto_dispatch.auto_merge import (
    REASON_INCONCLUSIVE,
    LineApprovalFacts,
    auto_approve_decision,
    review_history_from_snapshot,
)
from src.tools.auto_dispatch.cost import INFRA_REASON_CODES, normalize_reason_code
from src.tools.auto_dispatch.execution import CrossReviewer, verdict_unparseable
from src.tools.auto_dispatch.outcomes import InMemoryMemoryPort, MemoryP0OutcomeStore
from src.tools.auto_dispatch.review_schema import load_verdict_schema, validate_verdict
from src.tools.auto_dispatch.review_strict import (
    EMPTY,
    INCONCLUSIVE_PREFIX,
    MODE_ENV,
    PROSE,
    RECORD_KEY,
    VALID,
    classify_reply,
    inconclusive_verdict,
    is_strict_inconclusive,
    record_attempt,
    strict_example,
    strict_mode,
    strict_retry_note,
)
from src.tools.auto_dispatch.review_triage import (
    EVIDENCE_REQUEST,
    INCONCLUSIVE_MODE_ENV,
    REPRO_MODE_ENV,
    ReviewVerdict,
    record_triage,
    triage_verdict,
)
from src.tools.auto_dispatch.types import Outcome, Verdict

FULL = {
    "passed": False,
    "blockers": ["a.py:1 - scenario: x -> y - repro: `pytest a`"],
    "verdict": "blocked",
    "status": "fail",
    "needed_evidence": [],
}


# --- mode -------------------------------------------------------------------


def test_mode_defaults_to_shadow() -> None:
    assert strict_mode({}) == "shadow"
    assert strict_mode({MODE_ENV: ""}) == "shadow"
    assert strict_mode({MODE_ENV: " ENFORCE "}) == "shadow"
    assert strict_mode({MODE_ENV: "enforce"}) == "enforce"
    assert strict_mode({MODE_ENV: "off"}) == "off"
    assert MODE_ENV == "CONDUCTOR_DISPATCH_REVIEW_STRICT_MODE"


def test_mode_reads_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    assert strict_mode() == "enforce"
    monkeypatch.delenv(MODE_ENV)
    assert strict_mode() == "shadow"


# --- classify ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [None, "", "   \n\t ", {}, "```\n```", "```json\n\n```", "```json```", "  ```\n  ```  "],
)
def test_empty_replies(raw: object) -> None:
    assert classify_reply(raw) == EMPTY


@pytest.mark.parametrize(
    "raw",
    [
        "I looked at the diff and it seems fine overall.",
        '{"passed": false, "blockers": ["no tests"]}\n\nI also read the diff again.',
        '{"verdict": "ok"}',
        "{not json",
        {"verdict": "missing passed"},
        "```json\n{\"verdict\": \"x\"}\n```",
    ],
)
def test_prose_replies(raw: object) -> None:
    assert classify_reply(raw) == PROSE


@pytest.mark.parametrize(
    "raw",
    [
        json.dumps(FULL),
        FULL,
        "```json\n" + json.dumps(FULL) + "\n```",
        '{"passed": true, "verdict": "ok"}',
        'Looks good.\n{"passed": false, "blockers": ["no tests"], "verdict": "blocked"}',
    ],
)
def test_valid_replies(raw: object) -> None:
    assert classify_reply(raw) == VALID


# --- retry note -------------------------------------------------------------


@pytest.mark.parametrize("kind", [EMPTY, PROSE])
def test_retry_note_is_built_from_schema(kind: str) -> None:
    note = strict_retry_note(kind)
    schema = load_verdict_schema()
    for name in schema["required"]:
        assert f'"{name}"' in note
    assert '"passed" (boolean)' in note
    assert '"blockers" (array of strings)' in note
    assert '"verdict" (string)' in note
    assert '"needed_evidence" (array of strings)' in note
    for status in schema["properties"]["status"]["enum"]:
        assert f'"{status}"' in note
    assert "no prose before or after" in note
    assert "one JSON object only" in note


def test_retry_note_says_what_was_wrong() -> None:
    assert "was empty" in strict_retry_note(EMPTY)
    assert "was empty" not in strict_retry_note(PROSE)
    assert "prose" in strict_retry_note(PROSE).split("Reply again")[0]


def test_retry_note_example_validates() -> None:
    example = strict_example()
    assert validate_verdict(dict(example)) is not None
    note = strict_retry_note(PROSE)
    shown = json.loads(note[note.index("{"):])
    assert shown == example
    assert validate_verdict(shown) is not None
    assert shown["passed"] is False and shown["status"] == "inconclusive"
    assert classify_reply(json.dumps(shown)) == VALID


def test_retry_note_carries_no_material() -> None:
    note = strict_retry_note(PROSE)
    assert note == strict_retry_note(PROSE)
    assert "diff --git" not in note and "Review line" not in note
    assert len(note.encode("utf-8")) < 1500


# --- inconclusive verdict ---------------------------------------------------


def test_inconclusive_verdict_shape() -> None:
    verdict = inconclusive_verdict(PROSE, ["codex", "glm", "codex"])
    assert isinstance(verdict, ReviewVerdict)
    assert verdict.passed is False
    assert verdict.verdict == "inconclusive"
    assert verdict.status == "inconclusive"
    assert len(verdict.blockers) == 1
    assert verdict.blockers[0].startswith(INCONCLUSIVE_PREFIX)
    assert "prose" in verdict.blockers[0]
    assert verdict.blockers[0].count("codex") == 1
    assert verdict.needed_evidence
    assert is_strict_inconclusive(verdict)
    # Stable: the same inputs give the same line.
    assert inconclusive_verdict(PROSE, ["codex", "glm"]).blockers == verdict.blockers
    assert "empty" in inconclusive_verdict(EMPTY, []).blockers[0]


def test_is_strict_inconclusive_rejects_others() -> None:
    assert not is_strict_inconclusive(None)
    assert not is_strict_inconclusive(Verdict.ok())
    assert not is_strict_inconclusive(Verdict.blocked(["a.py:1 bug"]))
    assert not is_strict_inconclusive(
        ReviewVerdict(passed=False, blockers=["need logs"], status="inconclusive")
    )
    assert not is_strict_inconclusive(
        Verdict.blocked([f"{INCONCLUSIVE_PREFIX} (prose)"])
    )


# --- consumers treat it as neither pass nor fail ----------------------------


def test_not_unparseable_and_not_pass() -> None:
    verdict = inconclusive_verdict(PROSE, ["codex"])
    assert not verdict.passed
    # The machine's unparseable branch must be chosen by is_strict_inconclusive,
    # not verdict_unparseable (the blocker prefix is not an unparseable one).
    assert not verdict_unparseable(verdict)


def test_triage_shadow_records_inconclusive() -> None:
    verdict = inconclusive_verdict(PROSE, ["codex"])
    triage = triage_verdict(verdict, environ={}, exit_ready=True)
    assert triage.record["status"] == "inconclusive"
    assert triage.record["status_reported"] is True
    assert triage.record["would"] == EVIDENCE_REQUEST
    assert triage.action == ""
    # Shadow keeps the verdict exactly as given (no downgrade, no pass).
    assert triage.verdict is verdict


def test_triage_enforce_requests_evidence() -> None:
    verdict = inconclusive_verdict(EMPTY, ["codex"])
    triage = triage_verdict(
        verdict,
        environ={INCONCLUSIVE_MODE_ENV: "enforce", REPRO_MODE_ENV: "enforce"},
        exit_ready=True,
    )
    assert triage.action == EVIDENCE_REQUEST
    assert triage.verdict.passed is False
    assert triage.record["needed_evidence"] == verdict.needed_evidence


def test_triage_repro_enforce_never_passes_it() -> None:
    verdict = inconclusive_verdict(PROSE, ["codex"])
    triage = triage_verdict(
        verdict, environ={REPRO_MODE_ENV: "enforce", INCONCLUSIVE_MODE_ENV: "off"}
    )
    assert triage.verdict.passed is False
    assert not triage.record.get("passed_after_downgrade")


def test_auto_merge_sees_inconclusive() -> None:
    snapshot: dict = {}
    verdict = inconclusive_verdict(PROSE, ["codex"])
    record_triage(snapshot, triage_verdict(verdict, environ={}), reviewer="codex")
    history = review_history_from_snapshot(snapshot)
    assert "inconclusive" in history
    decision = auto_approve_decision(
        [LineApprovalFacts("L1", g2_passed=True, reviews=("merge",), review_history=history)],
        g5_passed=True,
        environ={"CONDUCTOR_DISPATCH_AUTO_APPROVE": "1"},
        globs=(),
    )
    assert decision.approve is False
    assert decision.reason == REASON_INCONCLUSIVE


def test_cost_treats_unparseable_park_as_infrastructure() -> None:
    assert normalize_reason_code("review_unparseable") in INFRA_REASON_CODES


def test_cost_treats_inconclusive_park_as_infrastructure(monkeypatch) -> None:
    from src.tools.auto_dispatch.cost import infra_reason_codes

    monkeypatch.setenv(MODE_ENV, "enforce")
    assert normalize_reason_code("review_inconclusive") in infra_reason_codes()


@pytest.mark.parametrize("reason", ["review_unparseable", "review_inconclusive"])
def test_outcomes_do_not_count_inconclusive_as_fail(reason: str, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    store = MemoryP0OutcomeStore(InMemoryMemoryPort())
    outcome = Outcome(
        line_id="L1",
        model="glm",
        duration_secs=1,
        tokens=1,
        passed=False,
        failure_reason=reason,
        reviewer_verdict="inconclusive",
    )
    asyncio.run(store.record("d1", {"L1": outcome}))
    assert asyncio.run(store.success_rate("glm")) is None


# --- record -----------------------------------------------------------------


def test_record_attempt_is_json_safe_and_bounded() -> None:
    snapshot: dict = {}
    now = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
    for index in range(40):
        record_attempt(
            snapshot,
            reviewer=f"r{index}" + "x" * 500,
            kind=PROSE,
            retried=bool(index % 2),
            outcome="would_inconclusive",
            mode="shadow",
            now=now if index % 2 else 1700000000 + index,
        )
    cell = snapshot[RECORD_KEY]
    json.dumps(cell)
    assert len(cell["attempts"]) == 32
    assert cell["total"] == 40
    assert cell["mode"] == "shadow"
    assert cell["attempts"][-1]["reviewer"].startswith("r39")
    assert cell["attempts"][0]["reviewer"].startswith("r8")
    assert all(len(item["reviewer"]) <= 200 for item in cell["attempts"])
    assert cell["attempts"][-1]["at"] == now.isoformat()
    assert cell["attempts"][-1]["retried"] is True


def test_record_attempt_ignores_non_dict() -> None:
    record_attempt(None, reviewer="a", kind=EMPTY, retried=False, outcome="x", mode="off", now=0)  # type: ignore[arg-type]


# --- audit: the dispatch reviewer prompt names the schema keys --------------


class _Scripted:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def review(self, model: str, prompt: str) -> str:
        del model
        self.prompts.append(prompt)
        return self.reply


def test_cross_reviewer_first_prompt_names_schema_keys() -> None:
    backend = _Scripted(json.dumps(FULL))
    reviewer = CrossReviewer(backend, lambda _line: "diff --git a/x b/x")
    asyncio.run(reviewer.review_line("L1", "glm"))
    first = backend.prompts[0]
    for name in load_verdict_schema()["required"]:
        assert f'"{name}"' in first
    for status in ("pass", "fail", "inconclusive"):
        assert f'"{status}"' in first


def test_cross_reviewer_retry_on_prose_classifies() -> None:
    # Today's retry happens once; the second reply is what the helper classifies.
    backend = _Scripted("I think it is fine.")
    reviewer = CrossReviewer(backend, lambda _line: "diff")
    verdict = asyncio.run(reviewer.review_line("L1", "glm"))
    assert len(backend.prompts) == 2
    assert verdict_unparseable(verdict)
    assert classify_reply(backend.reply) == PROSE
    assert is_strict_inconclusive(inconclusive_verdict(PROSE, ["glm"]))
    assert review_strict.RECORD_KEY == "review_strict"
