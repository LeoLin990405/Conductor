"""N51/N59/N60 in the gated drive: shadow keeps today's outcome, enforce acts.

A fake reviewer returns verdicts as a live ``CrossReviewer`` would after the
``execution.py`` hook (``review_triage.with_status``): an INCONCLUSIVE one is
a ``ReviewVerdict`` with ``status`` and ``needed_evidence``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.conductor_cli.review_evidence_status import (
    review_evidence_hints,
    review_evidence_lines,
)
from src.server.services.autonomy.stage_gate import Stage
from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch import operator as operator_module
from src.tools.auto_dispatch import review_triage
from src.tools.auto_dispatch.completion_evidence import (
    COMPLETION_EVIDENCE,
    COMPLETION_MODE_ENV,
)
from src.tools.auto_dispatch.dev import RoundRobinSelector
from src.tools.auto_dispatch.machine import REVIEW_PARKED, REVIEWED_HEADS, _load_state
from src.tools.auto_dispatch.review_triage import (
    INCONCLUSIVE_MODE_ENV,
    REPRO_MODE_ENV,
    REVIEW_INCONCLUSIVE,
    REVIEW_STATUS_HISTORY,
    REVIEW_TRIAGE,
    ReviewVerdict,
)
from src.tools.auto_dispatch.types import Verdict

from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle as _machine_bundle
from tests.unit.tools.auto_dispatch.test_machine_gates import _line as _machine_line
from tests.unit.tools.auto_dispatch.test_machine_gates import _repo_with_lines
from tests.unit.tools.auto_dispatch.test_w3b_fix1 import _settings

pytestmark = pytest.mark.asyncio

VAGUE = "the error handling looks fragile"
GOOD = (
    "a.txt:1 - scenario: an empty file -> crash - repro: `pytest tests/test_a.py -q`"
)


def _inconclusive() -> ReviewVerdict:
    return ReviewVerdict(
        passed=False,
        blockers=["cannot tell without the G2 log"],
        verdict="need evidence",
        status="inconclusive",
        needed_evidence=["G2 log for the head"],
    )


class VerdictReviewer:
    def __init__(self, *verdicts: Verdict) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        return self.verdicts[min(len(self.calls) - 1, len(self.verdicts) - 1)]


async def _drive(tmp_path: Path, *verdicts: Verdict):
    repo = _repo_with_lines(tmp_path / "repo", "a")
    reviewer = VerdictReviewer(*verdicts)
    collaborators, machine, _commands, _trees = _machine_bundle(
        tmp_path,
        [_machine_line("a", assigned_model="kimi")],
        reviewer=reviewer,
        settings=_settings(tmp_path, repo),
        selector=RoundRobinSelector(("kimi", "codex")),
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    result = await machine.run(dispatch)
    return collaborators, machine, reviewer, result


def _launches(collaborators) -> list[str]:
    return [model for _line, model, _spec in collaborators.launcher.launches]


# --- N59 ------------------------------------------------------------------------


async def test_shadow_inconclusive_acts_as_block_and_records_it(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(INCONCLUSIVE_MODE_ENV, raising=False)
    collab, _m, _r, result = await _drive(tmp_path, _inconclusive(), Verdict.ok())
    # Today's outcome: the BLOCK sent the author a fix round, then it passed.
    assert _launches(collab) == ["kimi", "kimi"]
    assert result.line("a").status is LineStatus.PASSED
    snap = result.gates["lines"]["a"]
    assert snap[REVIEW_STATUS_HISTORY] == ["inconclusive"]
    record = snap[REVIEW_TRIAGE]
    assert record["would"] == review_triage.EVIDENCE_REQUEST
    assert record["needed_evidence"] == ["G2 log for the head"]
    assert record["reviewer"] == "codex"


async def test_enforce_without_an_operator_exit_still_blocks(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(INCONCLUSIVE_MODE_ENV, "enforce")
    monkeypatch.setattr(review_triage, "inconclusive_exit_ready", lambda: False)
    collab, _m, _r, result = await _drive(tmp_path, _inconclusive(), Verdict.ok())
    assert _launches(collab) == ["kimi", "kimi"]
    assert result.gates["lines"]["a"][REVIEW_TRIAGE]["enforce_blocked"] == "no_operator_exit"


async def test_enforce_parks_for_evidence_without_a_fix_round(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(INCONCLUSIVE_MODE_ENV, "enforce")
    monkeypatch.setattr(review_triage, "inconclusive_exit_ready", lambda: True)
    collab, _m, _r, result = await _drive(tmp_path, _inconclusive())
    line = result.line("a")
    assert result.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_INCONCLUSIVE
    assert line.blockers == ["evidence: G2 log for the head"]
    # No fix round: the author ran once, and Stage-Gate counted no BLOCK.
    assert _launches(collab) == ["kimi"]
    snap = result.gates["lines"]["a"]
    state = _load_state(snap)
    assert state.stage is Stage.BLOCKED
    assert state.blocked_from == Stage.PRIMARY_REVIEW.value
    assert state.blocking_rounds == 0
    assert snap["machine"]["fix_rounds"] == 0
    assert snap[REVIEW_PARKED]["needed_evidence"] == ["G2 log for the head"]
    assert snap[REVIEW_PARKED]["reviewer"] == "codex"
    # ``dispatch status`` shows INCONCLUSIVE on its own line, with its own exit.
    lines = review_evidence_lines(snap)
    assert any("INCONCLUSIVE (evidence requested)" in item for item in lines)
    hints = review_evidence_hints("d1", "a", {"status": "BLOCKED", "failure_reason": line.failure_reason})
    assert hints and "conductor dispatch unblock d1 a" in hints[0]
    # ERROR (review_unavailable) is not this branch.
    assert review_evidence_hints("d1", "a", {"status": "BLOCKED", "failure_reason": "review_unavailable"}) == []


@pytest.mark.skipif(
    REVIEW_INCONCLUSIVE not in getattr(operator_module, "_RESUMABLE_REVIEW_PARKS", ()),
    reason="operator.py hook for review_inconclusive not integrated yet",
)
async def test_unblock_reviews_the_same_run_again(tmp_path, monkeypatch) -> None:
    """N51 rule: the new drive-stopping state has an operator exit."""
    monkeypatch.setenv(INCONCLUSIVE_MODE_ENV, "enforce")
    collab, machine, reviewer, result = await _drive(tmp_path, _inconclusive())
    stored = await collab.store.get("d1")
    line = stored.line("a")
    assert operator_module.unblock_refusal(stored, line) is None
    assert operator_module.apply_unblock(stored, line, note="G2 log attached") is None
    assert line.status is LineStatus.RUNNING
    await collab.store.save(stored)
    reviewer.verdicts = [Verdict.ok()]
    reviewer.calls.clear()
    done = await machine.run(stored)
    assert done.line("a").status is LineStatus.PASSED
    assert _launches(collab) == ["kimi"]


# --- N51 ------------------------------------------------------------------------


async def test_shadow_keeps_a_blocker_without_repro_blocking(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(REPRO_MODE_ENV, raising=False)
    collab, _m, _r, result = await _drive(tmp_path, Verdict.blocked([VAGUE]), Verdict.ok())
    assert _launches(collab) == ["kimi", "kimi"]
    record = result.gates["lines"]["a"][REVIEW_TRIAGE]
    assert record["would"] == "downgrade" and record["downgraded_count"] == 1


async def test_shadow_repro_log_carries_the_line_id_not_the_file_line(
    tmp_path, monkeypatch, caplog
) -> None:
    """The drive passes the line id into ``triage_verdict``, not a text match.

    The ``file:line`` is a locator, not an identity: a sampled judgement has to
    come back to the review of line ``a`` and de-duplicate by it.
    """
    monkeypatch.delenv(REPRO_MODE_ENV, raising=False)
    blocker = "a.txt:1 - the error handling looks fragile"
    with caplog.at_level(logging.WARNING):
        await _drive(tmp_path, Verdict.blocked([blocker]), Verdict.ok())
    [line] = [
        record.message
        for record in caplog.records
        if "shadow_decision mode=review_repro" in record.message
    ]
    assert " subject=a " in line
    assert "location=a.txt:1" in line


async def test_enforce_downgrades_a_blocker_without_repro(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(REPRO_MODE_ENV, "enforce")
    collab, _m, _r, result = await _drive(tmp_path, Verdict.blocked([VAGUE]), Verdict.ok())
    assert _launches(collab) == ["kimi"]
    assert result.line("a").status is LineStatus.PASSED
    assert result.gates["lines"]["a"][REVIEW_TRIAGE]["action"] == "downgrade"


async def test_enforce_keeps_a_reproducible_blocker(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(REPRO_MODE_ENV, "enforce")
    collab, _m, _r, result = await _drive(
        tmp_path, Verdict.blocked([VAGUE, GOOD]), Verdict.ok()
    )
    assert _launches(collab) == ["kimi", "kimi"]
    assert result.line("a").status is LineStatus.PASSED


# --- N60 ------------------------------------------------------------------------


async def test_shadow_records_completion_evidence_and_changes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(COMPLETION_MODE_ENV, raising=False)
    collab, _m, _r, result = await _drive(tmp_path, Verdict.ok())
    assert result.line("a").status is LineStatus.PASSED
    assert _launches(collab) == ["kimi"]
    snap = result.gates["lines"]["a"]
    # Verified: only the ledger holds it; the snapshot keeps its old shape.
    assert COMPLETION_EVIDENCE not in snap
    ledger = result.gates[REVIEWED_HEADS]["a"][result.line("a").branch]
    (head,) = ledger["heads"]
    assert ledger["evidence"][head]["verified"] is True
    assert ledger["evidence"][head]["failed"] == []


async def test_shadow_flags_a_diff_outside_write_scope_without_refusing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv(COMPLETION_MODE_ENV, raising=False)
    repo = _repo_with_lines(tmp_path / "repo", "a")
    collaborators, machine, _c, _t = _machine_bundle(
        tmp_path,
        [_machine_line("a", assigned_model="kimi", write_scope=("src/**",))],
        reviewer=VerdictReviewer(Verdict.ok()),
        settings=_settings(tmp_path, repo),
        selector=RoundRobinSelector(("kimi", "codex")),
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    result = await machine.run(dispatch)
    assert result.line("a").status is LineStatus.PASSED
    evidence = result.gates["lines"]["a"][COMPLETION_EVIDENCE]
    assert evidence["verified"] is False
    assert evidence["failed"] == ["diff_outside_write_scope"]
    assert evidence["outside_scope"] == ["a.txt"]
    assert evidence["g2"]["revision"] == evidence["head"]
    assert any("NOT verified" in item for item in review_evidence_lines(result.gates["lines"]["a"]))


async def test_off_records_no_completion_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(COMPLETION_MODE_ENV, "off")
    _collab, _m, _r, result = await _drive(tmp_path, Verdict.ok())
    assert result.line("a").status is LineStatus.PASSED
    assert COMPLETION_EVIDENCE not in result.gates["lines"]["a"]
