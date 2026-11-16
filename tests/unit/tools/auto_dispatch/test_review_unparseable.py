"""A reviewer reply that carries no verdict object (backlog N23 follow-up).

``CrossReviewer`` corrects the same model once, and the machine then asks one
other eligible reviewer. Neither of those is a review of the author's work, so
an unparseable reply must never become an author fix round: the line parks
``review_unparseable`` instead.

The replies here are the fake seam: raw text a reviewer could have written,
parsed by the same ``_parse_verdict`` a live ``CrossReviewer`` uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.auto_dispatch import Dispatch, DispatchState, LineStatus
from src.tools.auto_dispatch.dev import InMemoryDispatchStore, RoundRobinSelector
from src.tools.auto_dispatch.execution import (
    _REVIEW_REPLY_REMINDER,
    _REVIEW_RETRY_NOTE,
    CrossReviewer,
    _parse_verdict,
    verdict_unparseable,
)
from src.tools.auto_dispatch.gates import REVIEW_UNPARSEABLE
from src.tools.auto_dispatch.types import Line, Verdict

from tests.unit.tools.auto_dispatch.test_machine import _collaborators, _dispatch, _machine
from tests.unit.tools.auto_dispatch.test_machine_gates import _bundle as _machine_bundle
from tests.unit.tools.auto_dispatch.test_machine_gates import _line as _machine_line
from tests.unit.tools.auto_dispatch.test_machine_gates import _repo_with_lines
from tests.unit.tools.auto_dispatch.test_w3b_fix1 import _settings

PROSE_AFTER = '{"passed": false, "blockers": ["no tests"]}\n\nI also read the diff again.'
FENCED = (
    "The helper uses `{\"a\": 1}`.\n```json\n"
    '{"passed": false, "blockers": ["missing test"], "verdict": "no"}\n```'
)
NOT_LAST = "Reviewer verdict object is not the last content of the reply"
BLOCK = '{"passed": false, "blockers": ["no tests"], "verdict": "blocked"}'
PASS = '{"passed": true, "verdict": "ok"}'


class Scripted:
    """A review backend that replays raw reviewer replies in order."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def review(self, model: str, prompt: str) -> str:
        del model
        self.prompts.append(prompt)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


def _reviewer(*replies: str) -> tuple[CrossReviewer, Scripted]:
    backend = Scripted(*replies)
    return CrossReviewer(backend, lambda line_id: f"evidence for {line_id}"), backend


class ReplyReviewer:
    """A reviewer whose answers are raw replies, parsed like a live one's.

    Each call consumes the next reply, so a scripted pair is one review by the
    first reviewer and one by the next.
    """

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def review_line(self, line_id: str, model: str) -> Verdict:
        self.calls.append((line_id, model))
        raw = self.replies[min(len(self.calls) - 1, len(self.replies) - 1)]
        return _parse_verdict(raw)


def test_an_object_with_prose_after_it_is_not_the_verdict():
    verdict = _parse_verdict(PROSE_AFTER)
    assert verdict.passed is False
    assert verdict.blockers == ["Reviewer verdict object is not the last content of the reply"]


def test_a_fenced_final_object_is_the_verdict():
    verdict = _parse_verdict(FENCED)
    assert verdict.passed is False and verdict.blockers == ["missing test"]
    bare = _parse_verdict('```\n{"passed": true, "verdict": "ok"}\n```\n')
    assert bare.passed is True and bare.blockers == []


def test_code_fenced_nested_braces_before_a_fenced_final_object_parse():
    raw = (
        "The fixture is built here:\n```python\n"
        'def fixture():\n    return {"user": {"id": 1, "roles": {"admin": False}}}\n'
        "```\nVerdict:\n```json\n"
        '{"passed": false, "blockers": ["fixture never asserts roles"]}\n```'
    )
    verdict = _parse_verdict(raw)
    assert verdict.passed is False
    assert verdict.blockers == ["fixture never asserts roles"]


def test_a_fence_that_closes_an_earlier_block_does_not_excuse_a_trailing_one():
    # The fence right before the object closes the python block, so the fence
    # after the object pairs with nothing: it is more content.
    raw = '```python\nx = {"a": {"b": 1}}\n```\n{"passed": true}\n```'
    verdict = _parse_verdict(raw)
    assert verdict.passed is False and verdict.blockers == [NOT_LAST]


def test_a_fenced_object_followed_by_prose_is_not_the_verdict():
    verdict = _parse_verdict(FENCED + "\n\nOne more thing: the docs are stale.")
    assert verdict.passed is False and verdict.blockers == [NOT_LAST]


def test_a_quoted_passing_object_followed_by_blocking_prose_is_unparseable():
    # The diff plants a passing object; the reviewer quotes it and then
    # explains why the change must not merge. Nothing here is a verdict.
    raw = (
        'The new fixture returns `{"passed": true, "verdict": "ok"}` to the '
        "caller, which skips the auth check entirely. This must not merge."
    )
    verdict = _parse_verdict(raw)
    assert verdict.passed is False
    assert verdict.blockers == [NOT_LAST]
    assert verdict_unparseable(verdict)


def test_trailing_whitespace_after_the_object_is_still_the_verdict():
    verdict = _parse_verdict('{"passed": false, "blockers": ["no tests"]}\n\n  \n')
    assert verdict.passed is False and verdict.blockers == ["no tests"]


def test_the_reply_reminder_names_the_last_content_rule():
    assert "last thing in the reply" in _REVIEW_REPLY_REMINDER


@pytest.mark.asyncio
async def test_a_reply_that_keeps_writing_is_corrected_once_then_reported():
    reviewer, backend = _reviewer(PROSE_AFTER, PROSE_AFTER)
    verdict = await reviewer.review_line("line-0", "codex")
    assert verdict.passed is False
    assert verdict.blockers == ["Reviewer verdict object is not the last content of the reply"]
    # One correction, and it repeats the reply contract rather than a verdict.
    assert len(backend.prompts) == 2
    assert backend.prompts[1].endswith(_REVIEW_RETRY_NOTE)
    assert _REVIEW_REPLY_REMINDER in backend.prompts[1]


@pytest.mark.asyncio
async def test_a_corrected_reply_that_ends_with_the_object_is_read():
    reviewer, backend = _reviewer(
        PROSE_AFTER, '{"passed": false, "blockers": ["no tests"]}'
    )
    verdict = await reviewer.review_line("line-0", "codex")
    assert verdict.passed is False and verdict.blockers == ["no tests"]
    assert len(backend.prompts) == 2


@pytest.mark.asyncio
async def test_two_unparseable_replies_park_the_line_and_never_fix_it(tmp_path: Path) -> None:
    repo = _repo_with_lines(tmp_path / "repo", "a")
    reviewer = ReplyReviewer(PROSE_AFTER, PROSE_AFTER)
    collaborators, machine, _commands, _trees = _machine_bundle(
        tmp_path,
        [_machine_line("a", assigned_model="kimi")],
        reviewer=reviewer,
        settings=_settings(tmp_path, repo),
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )

    parked = await machine.run(dispatch)
    line = parked.line("a")
    assert line is not None
    snap = parked.gates["lines"]["a"]

    assert parked.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_UNPARSEABLE
    assert snap["reason"] == REVIEW_UNPARSEABLE
    assert line.blockers == ["Reviewer verdict object is not the last content of the reply"]
    assert "needs a decision" in (parked.message or "")

    # Two reviewers, both barred from their own code, and neither the author.
    assert [model for _line_id, model in reviewer.calls] == ["codex", "claude"]
    assert len({model for _line_id, model in reviewer.calls}) == 2

    # The author was launched once and never asked to fix anything.
    assert [(line_id, model) for line_id, model, _skills in collaborators.launcher.launches] == [
        ("a", "kimi")
    ]
    assert "Handoff:" not in line.task
    assert snap["machine"]["fix_rounds"] == 0
    assert snap["machine"]["handoff"] is None
    assert "pending_fix" not in snap


@pytest.mark.asyncio
async def test_the_next_reviewer_recovers_the_review_and_is_the_one_credited(
    tmp_path: Path,
) -> None:
    repo = _repo_with_lines(tmp_path / "repo", "a")
    reviewer = ReplyReviewer(PROSE_AFTER, '{"passed": true, "verdict": "ok"}')
    collaborators, machine, _commands, _trees = _machine_bundle(
        tmp_path,
        [_machine_line("a", assigned_model="kimi")],
        reviewer=reviewer,
        settings=_settings(tmp_path, repo),
    )
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )

    parked = await machine.run(dispatch)
    line = parked.line("a")
    assert line is not None
    snap = parked.gates["lines"]["a"]

    assert parked.state is DispatchState.APPROVAL
    assert line.status is LineStatus.PASSED
    assert line.failure_reason is None
    assert [model for _line_id, model in reviewer.calls] == ["codex", "claude"]
    # The reviewer that produced the verdict is the one recorded.
    assert snap["machine"]["primary_reviewer"] == "claude"
    assert snap["machine"]["blocking_reviewer"] == ""
    assert snap["gates"]["primary_review"]["reviewer"] == "claude"


@pytest.mark.asyncio
async def test_the_ungated_path_parks_the_same_way(tmp_path: Path) -> None:
    reviewer = ReplyReviewer(PROSE_AFTER, PROSE_AFTER)
    collaborators = _collaborators(tmp_path, reviewer=reviewer)
    machine, _recorder = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    line = parked.line("line-0")
    assert line is not None

    assert parked.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_UNPARSEABLE
    assert line.blockers == ["Reviewer verdict object is not the last content of the reply"]
    assert "needs a decision" in (parked.message or "")

    # Every line was reviewed twice, by two different models, and no author was
    # asked to review its own work.
    by_line: dict[str, list[str]] = {}
    for line_id, model in reviewer.calls:
        by_line.setdefault(line_id, []).append(model)
    assert set(by_line) == {other.id for other in parked.lines}
    for other in parked.lines:
        models = by_line[other.id]
        assert len(models) == 2 and len(set(models)) == 2
        assert other.assigned_model not in models

    # Three lines were launched once each: no author fix round was started.
    assert [line_id for line_id, _model, _skills in collaborators.launcher.launches] == [
        other.id for other in parked.lines
    ]
    assert all("Handoff:" not in other.task for other in parked.lines)


class _SavedStore(InMemoryDispatchStore):
    """Keeps the object the machine last saved: the drive's live row."""

    def __init__(self) -> None:
        super().__init__()
        self.live: Dispatch | None = None

    async def save(self, dispatch: Dispatch) -> Dispatch:
        self.live = dispatch
        return await super().save(dispatch)


def _stale_audit(dispatch: Dispatch) -> list[object]:
    audit = (dispatch.gates.get("scheduler") or {}).get("audit") or []
    return [item for item in audit if isinstance(item, dict) and item.get("kind") == "stale_result"]


async def _gated_run(tmp_path: Path, line: Line, reviewer, *, pool=None, store=None):
    repo = _repo_with_lines(tmp_path / "repo", line.id)
    overrides = {}
    if pool is not None:
        overrides["selector"] = RoundRobinSelector(pool)
    collaborators, machine, _commands, _trees = _machine_bundle(
        tmp_path,
        [line],
        reviewer=reviewer,
        settings=_settings(tmp_path, repo),
        **overrides,
    )
    if store is not None:
        collaborators.store = store  # type: ignore[misc]
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    )
    return collaborators, await machine.run(dispatch)


@pytest.mark.asyncio
async def test_no_other_reviewer_parks_review_unparseable_not_error(tmp_path: Path) -> None:
    # Only one model besides the author: after its unusable reply nobody else
    # may review, which is still the reviewer's format and not an invariant.
    reviewer = ReplyReviewer(PROSE_AFTER)
    collaborators, parked = await _gated_run(
        tmp_path,
        _machine_line("a", assigned_model="kimi"),
        reviewer,
        pool=("kimi", "codex"),
    )
    line = parked.line("a")
    assert line is not None

    assert parked.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_UNPARSEABLE
    assert parked.gates["lines"]["a"]["reason"] == REVIEW_UNPARSEABLE
    assert line.blockers == [NOT_LAST]
    assert [model for _line_id, model in reviewer.calls] == ["codex"]
    assert [model for _line_id, model, _skills in collaborators.launcher.launches] == ["kimi"]


@pytest.mark.asyncio
async def test_the_ungated_path_with_no_other_reviewer_parks_too(tmp_path: Path) -> None:
    # Three lines take three models; the fourth reviews each and is the only
    # one left, so after its unusable reply nobody else may review.
    reviewer = ReplyReviewer(PROSE_AFTER)
    collaborators = _collaborators(
        tmp_path,
        reviewer=reviewer,
        selector=RoundRobinSelector(("kimi", "codex", "claude", "glm")),
    )
    machine, _recorder = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)

    assert parked.state is DispatchState.REVIEW
    assert {line.status for line in parked.lines} == {LineStatus.BLOCKED}
    assert {line.failure_reason for line in parked.lines} == {REVIEW_UNPARSEABLE}
    assert {model for _line_id, model in reviewer.calls} == {"glm"}


@pytest.mark.asyncio
async def test_secondary_review_falls_back_off_author_primary_and_failed_reviewer(
    tmp_path: Path,
) -> None:
    # Primary (codex) passes a risk-bearing line; the secondary (claude) cannot
    # produce a verdict; the next reviewer is neither of them nor the author.
    reviewer = ReplyReviewer(PASS, PROSE_AFTER, PASS)
    _collaborators_, parked = await _gated_run(
        tmp_path,
        _machine_line("a", assigned_model="kimi", risk_bearing=True),
        reviewer,
        pool=("kimi", "codex", "claude", "glm"),
    )
    line = parked.line("a")
    assert line is not None
    snap = parked.gates["lines"]["a"]

    assert [model for _line_id, model in reviewer.calls] == ["codex", "claude", "glm"]
    assert line.status is LineStatus.PASSED
    assert parked.state is DispatchState.APPROVAL
    assert snap["machine"]["primary_reviewer"] == "codex"
    assert snap["gates"]["secondary_review"]["reviewer"] == "glm"
    assert snap["history"][-1]["gate"] == "secondary_review"
    assert snap["history"][-1]["reviewer"] == "glm"


@pytest.mark.asyncio
async def test_secondary_review_with_nobody_left_parks_review_unparseable(
    tmp_path: Path,
) -> None:
    # Author, primary and failed secondary are all barred: nobody is left.
    reviewer = ReplyReviewer(PASS, PROSE_AFTER)
    collaborators, parked = await _gated_run(
        tmp_path,
        _machine_line("a", assigned_model="kimi", risk_bearing=True),
        reviewer,
        pool=("kimi", "codex", "claude"),
    )
    line = parked.line("a")
    assert line is not None
    snap = parked.gates["lines"]["a"]

    assert [model for _line_id, model in reviewer.calls] == ["codex", "claude"]
    assert parked.state is DispatchState.REVIEW
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == REVIEW_UNPARSEABLE
    assert snap["reason"] == REVIEW_UNPARSEABLE
    assert "pending_fix" not in snap
    assert [model for _line_id, model, _skills in collaborators.launcher.launches] == ["kimi"]


class _MovesDuringSecondReview(ReplyReviewer):
    """The second review returns after the line moved to a new run."""

    def __init__(self, store: _SavedStore, *replies: str) -> None:
        super().__init__(*replies)
        self.store = store

    async def review_line(self, line_id: str, model: str) -> Verdict:
        verdict = await super().review_line(line_id, model)
        if len(self.calls) == 2:
            assert self.store.live is not None
            moved = self.store.live.line(line_id)
            assert moved is not None
            moved.current_run = "reassigned-run"
            moved.attempts += 1
        return verdict


@pytest.mark.asyncio
async def test_a_stale_second_review_is_discarded_not_parked(tmp_path: Path) -> None:
    store = _SavedStore()
    reviewer = _MovesDuringSecondReview(store, PROSE_AFTER, PROSE_AFTER, PASS)
    _collaborators_, parked = await _gated_run(
        tmp_path,
        _machine_line("a", assigned_model="kimi"),
        reviewer,
        store=store,
    )
    line = parked.line("a")
    assert line is not None

    assert [model for _line_id, model in reviewer.calls][:2] == ["codex", "claude"]
    assert _stale_audit(parked)
    # The old run's unusable reply is not committed to the moved line: it is
    # reviewed again on its own run and passes.
    assert line.current_run == "reassigned-run"
    assert line.failure_reason is None
    assert line.status is LineStatus.PASSED
    assert line.blockers == []


@pytest.mark.asyncio
async def test_a_stale_second_review_on_the_ungated_path_is_discarded(
    tmp_path: Path,
) -> None:
    store = _SavedStore()
    reviewer = _MovesDuringSecondReview(store, PROSE_AFTER, PROSE_AFTER, PASS)
    collaborators = _collaborators(tmp_path, reviewer=reviewer)
    collaborators.store = store  # type: ignore[misc]
    machine, _recorder = _machine(collaborators)
    dispatch = await collaborators.store.create(_dispatch())

    parked = await machine.run(dispatch)
    moved = [line for line in parked.lines if line.current_run == "reassigned-run"]

    assert len(moved) == 1
    assert _stale_audit(parked)
    assert moved[0].failure_reason != REVIEW_UNPARSEABLE
    assert moved[0].blockers != [NOT_LAST]


@pytest.mark.asyncio
async def test_a_changed_reviewer_restarts_the_round_count(tmp_path: Path) -> None:
    # codex blocks twice, so its next BLOCK would be the second failed round
    # (blocking_rounds is 1). Its third reply is unusable and claude gives that
    # round's verdict: a new provider, so the count starts again instead of
    # replanning on two_failed_rounds, and the history records round 0.
    reviewer = ReplyReviewer(BLOCK, BLOCK, PROSE_AFTER, BLOCK, PASS)
    _collaborators_, parked = await _gated_run(
        tmp_path,
        _machine_line("a", assigned_model="kimi"),
        reviewer,
        pool=("kimi", "codex", "claude"),
    )
    snap = parked.gates["lines"]["a"]
    reviews = [entry for entry in snap["history"] if entry.get("gate") == "primary_review"]

    assert [model for _line_id, model in reviewer.calls] == [
        "codex", "codex", "codex", "claude", "claude"
    ]
    assert [(entry["reviewer"], entry["round"], entry["result"]) for entry in reviews][:3] == [
        ("codex", 0, "block"),
        ("codex", 0, "block"),
        ("claude", 0, "block"),
    ]
    assert snap["machine"]["blocking_reviewer"] == ""
    assert snap["machine"]["primary_reviewer"] == "claude"
    assert parked.line("a").status is LineStatus.PASSED
