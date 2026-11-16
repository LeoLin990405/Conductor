"""Review fix: a conflict/security tag after a ``path:LINE -`` prefix still routes.

The N51 rubric asks for ``path:LINE - scenario ...``, which moved a reviewer's
``security:`` / ``scope:`` tag off the start of the blocker. Both tag regexes
(``machine._CONFLICT_TAG`` and ``review_triage._SECURITY_TAG``) accept the tag
at the start, right after a leading ``path:LINE -`` / ``path:LINE:`` prefix,
or anywhere before the first " - ".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.tools.auto_dispatch import LineStatus, Verdict
from src.tools.auto_dispatch.machine import ADJUDICATION, _conflict_of, conflict_tag
from src.tools.auto_dispatch import review_triage
from src.tools.auto_dispatch.review_triage import REVIEW_RUBRIC


def is_security_tagged(blocker: str) -> bool:
    return bool(review_triage._SECURITY_TAG.match(blocker))


@pytest.mark.parametrize(
    ("blocker", "tag"),
    [
        # Start of the blocker: the pre-existing shape.
        ("security: token logged", "security"),
        ("scope: touches billing", "scope"),
        ("[Security] token in a log", "security"),
        ("  [scope] edits app.py", "scope"),
        # After a leading path:LINE prefix (the N51 rubric shape).
        ("src/auth.py:12 - security: token logged", "security"),
        ("a.py:3 - scope: edits outside write scope", "scope"),
        ("src/auth.py:12: security: token logged", "security"),
        ("src/auth.py:12:4 - [security] token logged", "security"),
        # Anywhere before the first " - ".
        ("High severity security: token logged - repro: `pytest -q`", "security"),
        ("Blocker [scope] src/x.py edits billing - scenario: y", "scope"),
        # The rubric's tag-first shape.
        ("security: src/auth.py:12 - scenario: login -> token in log", "security"),
    ],
)
def test_a_tag_routes_at_the_start_after_a_path_or_before_the_first_dash(
    blocker: str, tag: str
) -> None:
    assert conflict_tag(blocker) == tag
    assert is_security_tagged(blocker) is (tag == "security")


@pytest.mark.parametrize(
    "blocker",
    [
        "the fixture is module-scoped",
        "no security review was asked for",
        "src/a.py:3 - the telescope: lens is off",
        "src/a.py:3 - module-scope: leak",
        # After the first " - " that is not the path prefix: prose, not a tag.
        "src/a.py:3 - the bug - security: mentioned later",
        "Bug in parser - scope: mentioned after the dash",
        "",
    ],
)
def test_an_untagged_blocker_is_not_a_conflict(blocker: str) -> None:
    assert conflict_tag(blocker) == ""
    assert is_security_tagged(blocker) is False


def test_conflict_of_routes_a_path_prefixed_security_or_scope_blocker() -> None:
    assert _conflict_of(Verdict.blocked(["src/auth.py:12 - security: token logged"])) == (
        "security"
    )
    assert _conflict_of(Verdict.blocked(["a.py:3 - scope: edits outside write scope"])) == (
        "scope"
    )
    mixed = Verdict.blocked(
        ["a.py:3 - scope: edits outside write scope", "src/auth.py:12 - security: token"]
    )
    assert _conflict_of(mixed) == "security"


def test_the_rubric_puts_the_tag_before_the_path() -> None:
    assert "'security: path/to/file.py:LINE - " in REVIEW_RUBRIC
    assert "'scope: path/to/file.py:LINE - " in REVIEW_RUBRIC


# -- the drive: a secondary review's path-prefixed tag parks for adjudication --


async def _parked(tmp_path: Path, blocker: str):
    from tests.unit.server.services.test_dispatch_unwedge import (
        ScriptedReviewer,
        _assigned,
        _drive,
        _setup,
    )

    reviewer = ScriptedReviewer({"a": [Verdict.ok("primary ok"), Verdict.blocked([blocker])]})
    store, _launcher, _gates, service, row = await _setup(
        tmp_path,
        [_assigned("a", "deepseek", risk_bearing=True)],
        reviewer=reviewer,
    )
    await _drive(service, row.id)
    return await store.get(row.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("blocker", "reason", "conflict"),
    [
        ("src/auth.py:12 - security: token logged", "reviewer_conflict_security", "security"),
        ("a.py:3 - scope: edits outside write scope", "reviewer_conflict_scope", "scope"),
    ],
)
async def test_the_drive_routes_a_path_prefixed_tag_as_a_conflict(
    tmp_path: Path, blocker: str, reason: str, conflict: str
) -> None:
    parked = await _parked(tmp_path, blocker)
    line = parked.line("a")
    assert line.status is LineStatus.BLOCKED
    assert line.failure_reason == reason
    snap = parked.gates["lines"]["a"]
    assert snap["machine"]["stage"] == "awaiting_adjudication"
    assert snap[ADJUDICATION]["conflict"] == conflict
    assert snap[ADJUDICATION]["findings"] == [blocker]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker",
    [
        "src/app.py:3 - the token budget is exceeded",  # "token" is common here
        "keep the secret sauce in README",
        "src/login.py:12 - SQL injection in the form",
    ],
)
async def test_the_drive_keeps_an_untagged_keyword_blocker_in_a_fix_round(
    tmp_path: Path, blocker: str
) -> None:
    """R2 review item 1 (high): a keyword alone must not park the line.

    The keyword exemption (N51) keeps such a blocker from being downgraded,
    but N41 routing is tag-only, so the line goes to the author as a normal
    fix round -- in every mode, with no new switch to flip.
    """
    parked = await _parked(tmp_path, blocker)
    snap = parked.gates["lines"]["a"]
    events = snap["events"]
    # The secondary review blocks, and the line goes back to the author.
    assert any(
        event.get("from_stage") == "secondary_review"
        and event.get("to_stage") == "fixing"
        and event.get("reason") == "review_blocked"
        for event in events
    )
    # Nothing ever parked it for a human.
    assert not any(
        event.get("to_stage") == "awaiting_adjudication" for event in events
    )
    assert ADJUDICATION not in snap
