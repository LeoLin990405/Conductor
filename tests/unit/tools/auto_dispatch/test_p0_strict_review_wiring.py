"""P0 strict review, wired: the one retry, the attempt record, INCONCLUSIVE.

``off`` is main (old retry note, no record); ``shadow`` (the default) sends
the old note and parks ``review_unparseable`` as main does, but records the
attempts and that enforce would have parked ``review_inconclusive``;
``enforce`` sends the schema-led note and parks ``review_inconclusive``
(never PASS, FAIL or an author fix round) when the fallback reviewer fails too.
"""

from __future__ import annotations

import asyncio

import pytest

from src.tools.auto_dispatch import DispatchState, LineStatus
from src.tools.auto_dispatch.execution import _REVIEW_RETRY_NOTE, CrossReviewer
from src.tools.auto_dispatch.gates import REVIEW_UNPARSEABLE
from src.tools.auto_dispatch.operator import apply_unblock
from src.tools.auto_dispatch.outcomes import InMemoryMemoryPort, MemoryP0OutcomeStore
from src.tools.auto_dispatch.review_strict import (
    EMPTY,
    INCONCLUSIVE_PREFIX,
    MODE_ENV,
    PROSE,
    RECORD_KEY,
    inconclusive_verdict,
    strict_retry_note,
)
from src.tools.auto_dispatch.review_triage import REVIEW_INCONCLUSIVE
from src.tools.auto_dispatch.types import Outcome

from .test_machine import _collaborators, _dispatch, _machine
from .test_review_unparseable import (
    PASS,
    PROSE_AFTER,
    ReplyReviewer,
    Scripted,
    _gated_run,
)
from .test_machine_gates import _line as _machine_line


@pytest.fixture(autouse=True)
def _default_mode(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)


def _reviewer(*replies: str) -> tuple[CrossReviewer, Scripted]:
    backend = Scripted(*replies)
    return CrossReviewer(backend, lambda line_id: f"evidence for {line_id}"), backend


# ------------------------------------------------------------- CrossReviewer


@pytest.mark.asyncio
async def test_off_sends_the_old_note_and_notes_nothing(monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "off")
    reviewer, backend = _reviewer(PROSE_AFTER, PROSE_AFTER)
    await reviewer.review_line("line-0", "codex")
    assert backend.prompts[1] == backend.prompts[0] + _REVIEW_RETRY_NOTE
    assert reviewer.pop_strict_attempts("line-0") == []


@pytest.mark.asyncio
async def test_shadow_sends_the_old_note_and_notes_the_attempt() -> None:
    reviewer, backend = _reviewer(PROSE_AFTER, PROSE_AFTER)
    verdict = await reviewer.review_line("line-0", "codex")
    assert backend.prompts[1] == backend.prompts[0] + _REVIEW_RETRY_NOTE
    assert verdict.passed is False
    assert reviewer.pop_strict_attempts("line-0") == [
        {"reviewer": "codex", "kind": PROSE, "retried": True, "outcome": "unparseable"}
    ]
    assert reviewer.pop_strict_attempts("line-0") == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("first", "kind"), [(PROSE_AFTER, PROSE), ("", EMPTY)])
async def test_enforce_sends_the_strict_schema_note(monkeypatch, first, kind) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    reviewer, backend = _reviewer(first, PASS)
    verdict = await reviewer.review_line("line-0", "codex")
    assert backend.prompts[1] == backend.prompts[0] + "\n\n" + strict_retry_note(kind)
    assert verdict.passed is True
    assert reviewer.pop_strict_attempts("line-0") == [
        {"reviewer": "codex", "kind": kind, "retried": True, "outcome": "valid"}
    ]


@pytest.mark.asyncio
async def test_a_valid_first_reply_is_not_retried() -> None:
    reviewer, backend = _reviewer(PASS)
    await reviewer.review_line("line-0", "codex")
    assert len(backend.prompts) == 1
    assert reviewer.pop_strict_attempts("line-0")[0]["outcome"] == "valid"


# ------------------------------------------------------------------ machine


async def _twice_unparseable(tmp_path):
    reviewer = ReplyReviewer(PROSE_AFTER, PROSE_AFTER)
    collaborators, parked = await _gated_run(
        tmp_path, _machine_line("a", assigned_model="kimi"), reviewer
    )
    return collaborators, parked, reviewer


@pytest.mark.asyncio
async def test_enforce_parks_review_inconclusive_never_pass_fail_or_fix(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    collaborators, parked, reviewer = await _twice_unparseable(tmp_path)
    line = parked.line("a")
    snap = parked.gates["lines"]["a"]

    assert parked.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_INCONCLUSIVE
    assert snap["reason"] == REVIEW_INCONCLUSIVE
    assert len(line.blockers) == 1 and line.blockers[0].startswith(INCONCLUSIVE_PREFIX)
    assert "codex" in line.blockers[0] and "claude" in line.blockers[0]
    assert snap["review_parked"]["failed"] == ["codex", "claude"]
    assert snap["machine"]["stage"] == "blocked"
    assert snap["machine"]["fix_rounds"] == 0 and "pending_fix" not in snap
    assert [m for _l, m, _s in collaborators.launcher.launches] == ["kimi"]
    assert [model for _l, model in reviewer.calls] == ["codex", "claude"]
    assert snap[RECORD_KEY]["attempts"][-1]["outcome"] == "inconclusive"


@pytest.mark.asyncio
async def test_enforce_inconclusive_park_is_resumed_by_unblock(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    _collaborators_, parked, _reviewer = await _twice_unparseable(tmp_path)
    line = parked.line("a")

    assert apply_unblock(parked, line) is None
    assert line.status is LineStatus.RUNNING
    assert line.failure_reason is None
    assert parked.gates["lines"]["a"]["machine"]["stage"] == "primary_review"


@pytest.mark.asyncio
async def test_shadow_keeps_review_unparseable_and_records_would_inconclusive(
    tmp_path,
) -> None:
    _collaborators_, parked, _reviewer = await _twice_unparseable(tmp_path)
    line = parked.line("a")
    snap = parked.gates["lines"]["a"]

    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_UNPARSEABLE
    assert line.blockers == ["Reviewer verdict object is not the last content of the reply"]
    attempt = snap[RECORD_KEY]["attempts"][-1]
    assert attempt["outcome"] == "would_inconclusive"
    assert attempt["mode"] == "shadow"
    assert attempt["reviewer"] == "codex,claude"


@pytest.mark.asyncio
async def test_off_keeps_review_unparseable_and_records_nothing(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(MODE_ENV, "off")
    _collaborators_, parked, _reviewer = await _twice_unparseable(tmp_path)
    assert parked.line("a").failure_reason == REVIEW_UNPARSEABLE
    assert RECORD_KEY not in parked.gates["lines"]["a"]


@pytest.mark.asyncio
async def test_the_ungated_path_parks_inconclusive_in_enforce(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(MODE_ENV, "enforce")
    reviewer = ReplyReviewer(PROSE_AFTER, PROSE_AFTER)
    collaborators = _collaborators(tmp_path, reviewer=reviewer)
    machine, _recorder = _machine(collaborators)
    parked = await machine.run(await collaborators.store.create(_dispatch()))

    assert parked.state is DispatchState.REVIEW
    assert {line.failure_reason for line in parked.lines} == {REVIEW_INCONCLUSIVE}
    assert all(line.blockers[0].startswith(INCONCLUSIVE_PREFIX) for line in parked.lines)
    assert len(collaborators.launcher.launches) == 3


class _InconclusiveThenPass:
    """A reviewer whose first verdict is already a strict INCONCLUSIVE."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def review_line(self, line_id: str, model: str):
        from src.tools.auto_dispatch.execution import _parse_verdict

        self.calls.append(model)
        if len(self.calls) == 1:
            return inconclusive_verdict(PROSE, [model])
        return _parse_verdict(PASS)


@pytest.mark.asyncio
async def test_a_strict_inconclusive_verdict_skips_triage_and_goes_to_another_reviewer(
    tmp_path,
) -> None:
    reviewer = _InconclusiveThenPass()
    _collaborators_, parked = await _gated_run(
        tmp_path, _machine_line("a", assigned_model="kimi"), reviewer
    )
    line = parked.line("a")
    assert reviewer.calls == ["codex", "claude"]
    assert line.status is LineStatus.PASSED
    # The INCONCLUSIVE verdict never reached the author as a fix round.
    assert parked.gates["lines"]["a"]["machine"]["fix_rounds"] == 0


@pytest.mark.asyncio
async def test_crossreviewer_attempts_land_on_the_line_snapshot(tmp_path) -> None:
    backend = Scripted(PROSE_AFTER, PASS)
    reviewer = CrossReviewer(backend, lambda line_id: f"evidence for {line_id}")
    _collaborators_, parked = await _gated_run(
        tmp_path, _machine_line("a", assigned_model="kimi"), reviewer
    )
    assert parked.line("a").status is LineStatus.PASSED
    attempts = parked.gates["lines"]["a"][RECORD_KEY]["attempts"]
    assert attempts[0]["kind"] == PROSE and attempts[0]["outcome"] == "valid"
    assert attempts[0]["retried"] is True and attempts[0]["mode"] == "shadow"


# ----------------------------------------------------------------- reporting


def _outcome(passed: bool, reason: str | None) -> Outcome:
    return Outcome(
        line_id="L",
        model="glm",
        duration_secs=1,
        tokens=1,
        passed=passed,
        failure_reason=reason,
        reviewer_verdict="",
        category="code",
    )


def test_outcomes_count_inconclusive_apart_from_runs_and_failures(monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REVIEW_STRICT_MODE", "enforce")
    port = InMemoryMemoryPort()
    store = MemoryP0OutcomeStore(port)
    asyncio.run(
        store.record(
            "d1",
            {
                "a": _outcome(True, None),
                "b": _outcome(False, "review_inconclusive"),
                "c": _outcome(False, "review_unparseable"),
                "d": _outcome(False, "review_blocked"),
            },
        )
    )
    assert asyncio.run(store.success_rate("glm")) == 0.5
    assert asyncio.run(store.category_counts("glm")) == (1, 2)
    table = asyncio.run(store.category_table("code"))
    assert table["all"]["glm"] == (1, 2)
    assert table["category"]["glm"] == (1, 2)
    rollup = asyncio.run(port.get("auto_dispatch_outcomes", "_models"))
    assert rollup["glm"]["runs"] == 2 and rollup["glm"]["passed"] == 1
    assert rollup["glm"]["inconclusive"] == 2
