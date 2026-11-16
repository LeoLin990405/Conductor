"""The cross-review reply contract (backlog N23).

The review prompt never told the reviewer to answer in JSON (_REVIEW_SYSTEM was
defined but unused), so a reviewer that wrote a careful prose review produced
"Reviewer returned no verdict JSON" and the author got a fix round for nothing.
"""

from __future__ import annotations

import pytest

from src.tools.auto_dispatch.execution import (
    _REVIEW_REPLY_REMINDER,
    _REVIEW_RETRY_NOTE,
    _REVIEW_SYSTEM,
    CrossReviewer,
    _parse_verdict,
)

pytestmark = pytest.mark.asyncio


class Scripted:
    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def review(self, model: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies[min(len(self.prompts) - 1, len(self.replies) - 1)]


def _reviewer(*replies: str) -> tuple[CrossReviewer, Scripted]:
    backend = Scripted(*replies)
    return CrossReviewer(backend, lambda line_id: f"evidence for {line_id}"), backend


async def test_the_prompt_carries_the_reply_contract():
    reviewer, backend = _reviewer('{"passed": true}')
    await reviewer.review_line("line-0", "kimi")
    prompt = backend.prompts[0]
    assert prompt.startswith(_REVIEW_SYSTEM)
    assert "evidence for line-0" in prompt
    assert prompt.rstrip().endswith(_REVIEW_REPLY_REMINDER)


def test_a_verdict_after_prose_with_braces_is_read():
    # The object ends the reply. One followed by more content (other than one
    # closing fence paired with a fence opened just before it) is not the
    # verdict: see test_review_unparseable.py.
    raw = 'The helper uses `{"a": 1}` and `x = {}`.\n{"passed": false, "blockers": ["missing test"], "verdict": "no"}'
    verdict = _parse_verdict(raw)
    assert verdict.passed is False and verdict.blockers == ["missing test"]


async def test_an_unparseable_reply_is_retried_once_with_a_correction():
    reviewer, backend = _reviewer("A long prose review with no object.", '{"passed": true, "verdict": "ok"}')
    verdict = await reviewer.review_line("line-0", "kimi")
    assert verdict.passed is True
    assert len(backend.prompts) == 2 and backend.prompts[1].endswith(_REVIEW_RETRY_NOTE)


async def test_two_unparseable_replies_still_block():
    reviewer, backend = _reviewer("prose", "more prose")
    verdict = await reviewer.review_line("line-0", "kimi")
    assert verdict.passed is False and len(backend.prompts) == 2


async def test_a_real_block_is_not_retried():
    reviewer, backend = _reviewer('{"passed": false, "blockers": ["wrong file"]}')
    verdict = await reviewer.review_line("line-0", "kimi")
    assert verdict.blockers == ["wrong file"] and len(backend.prompts) == 1


def test_a_quoted_passing_fixture_never_overrides_a_shorter_block():
    raw = (
        'The fixture uses `{"passed": true, "blockers": [], "verdict": "fixture says everything ok"}`.\n\n'
        '{"passed": false, "blockers": ["x broken"]}'
    )
    verdict = _parse_verdict(raw)
    assert verdict.passed is False


def test_a_nested_object_inside_the_verdict_is_not_a_candidate():
    raw = '{"passed": false, "blockers": ["see example"], "example": {"passed": true}}'
    verdict = _parse_verdict(raw)
    assert verdict.passed is False and verdict.blockers == ["see example"]


def test_agreeing_candidates_take_the_last():
    raw = '{"passed": false, "blockers": ["a"]} then {"passed": false, "blockers": ["b"]}'
    assert _parse_verdict(raw).blockers == ["b"]


def test_a_non_bool_passed_is_not_a_candidate():
    raw = '{"passed": "false"} {"passed": true, "verdict": "ok"}'
    assert _parse_verdict(raw).passed is True


async def test_a_conflicting_reply_is_retried():
    conflicted = '{"passed": true} and also {"passed": false, "blockers": ["x"]}'
    reviewer, backend = _reviewer(conflicted, '{"passed": false, "blockers": ["x"]}')
    verdict = await reviewer.review_line("line-0", "kimi")
    assert verdict.passed is False and verdict.blockers == ["x"] and len(backend.prompts) == 2
