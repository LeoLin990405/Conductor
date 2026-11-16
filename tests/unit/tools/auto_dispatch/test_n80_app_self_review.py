"""N80 §5.3 H4 / acceptance 8: an app cannot review its own line under another agent name.

An app-claimed dispatch line records author / developer / contributor as
``app:<app_id>``. A reviewer ``app:<app_id>/<agent>`` or ``app:<app_id>:<agent>``
is the same identity and is refused with ``self_review_forbidden``.

Decision: ``app:kimi`` is a different identity from the native CLI worker
``kimi`` (an app is not the CLI Worker), so neither bars the other.
"""

from __future__ import annotations

import pytest

from src.server.services.autonomy.stage_gate import (
    PASS,
    Event,
    GateResult,
    Stage,
    StageGateState,
    advance,
    reviewer_identity,
)

APP = "app:claude-code"


def _state(stage: Stage, *, developer: str = APP, primary: str = "") -> StageGateState:
    return StageGateState(
        task_id="n80-line-1",
        stage=stage,
        risk_bearing=True,
        author_provider=developer,
        developer_provider=developer,
        contributors=(developer,),
        primary_reviewer=primary,
        primary_passed=bool(primary),
    )


def _review(kind: str, provider: str) -> Event:
    return Event(
        kind=kind,
        provider=provider,
        result=GateResult(outcome=PASS, reason="criteria met"),
        verdict="approve",
        timestamp="2026-11-14T00:00:07Z",
    )


SAME_APP = (
    "app:claude-code",
    "app:Claude-Code/opus",
    "app:claude-code:sonnet",
    "  APP:CLAUDE-CODE/haiku  ",
)


@pytest.mark.parametrize("reviewer", SAME_APP)
def test_same_app_other_agent_cannot_primary_review(reviewer: str) -> None:
    transition = advance(_state(Stage.PRIMARY_REVIEW), _review("primary_review", reviewer))
    assert not transition.accepted
    assert transition.reason == "self_review_forbidden"


@pytest.mark.parametrize("reviewer", SAME_APP)
def test_same_app_other_agent_cannot_secondary_review(reviewer: str) -> None:
    state = _state(Stage.SECONDARY_REVIEW, primary="glm")
    transition = advance(state, _review("secondary_review", reviewer))
    assert not transition.accepted
    assert transition.reason == "self_review_forbidden"


def test_suffixed_developer_bars_the_bare_app() -> None:
    state = _state(Stage.PRIMARY_REVIEW, developer="app:claude-code/opus")
    transition = advance(state, _review("primary_review", "app:claude-code"))
    assert transition.reason == "self_review_forbidden"


@pytest.mark.parametrize("reviewer", ("glm", "app:cursor", "app:cursor/claude-code", "app:claude-codex"))
def test_other_app_or_native_reviewer_is_accepted(reviewer: str) -> None:
    transition = advance(_state(Stage.PRIMARY_REVIEW), _review("primary_review", reviewer))
    assert transition.accepted, transition.reason
    assert transition.state.stage is Stage.SECONDARY_REVIEW
    assert transition.state.primary_reviewer == reviewer

    secondary = advance(
        _state(Stage.SECONDARY_REVIEW, primary="kimi"), _review("secondary_review", reviewer)
    )
    assert secondary.accepted, secondary.reason


@pytest.mark.parametrize(
    ("name", "identity"),
    (
        ("app:claude-code", "app:claude-code"),
        ("app:Claude-Code/opus", "app:claude-code"),
        ("app:claude-code:sonnet", "app:claude-code"),
        ("  APP:Cursor  ", "app:cursor"),
        ("App:cursor / gpt", "app:cursor"),
        ("kimi", "kimi"),
        ("  Kimi ", "Kimi"),
        ("rev-codex/high", "rev-codex/high"),
        ("apps:x", "apps:x"),
        ("app:", "app:"),
        ("", ""),
    ),
)
def test_reviewer_identity(name: str, identity: str) -> None:
    assert reviewer_identity(name) == identity


def test_native_author_still_bars_itself_but_not_the_app_of_the_same_name() -> None:
    state = _state(Stage.PRIMARY_REVIEW, developer="kimi")
    barred = advance(state, _review("primary_review", "kimi"))
    assert barred.reason == "self_review_forbidden"

    app = advance(state, _review("primary_review", "app:kimi"))
    assert app.accepted, app.reason

    reverse = advance(_state(Stage.PRIMARY_REVIEW, developer="app:kimi"), _review("primary_review", "kimi"))
    assert reverse.accepted, reverse.reason


def test_app_line_state_serializes_without_new_fields() -> None:
    state = _state(Stage.PRIMARY_REVIEW)
    assert StageGateState.from_dict(state.to_dict()) == state
    assert advance(state, _review("primary_review", "app:claude-code/opus")).state == state
