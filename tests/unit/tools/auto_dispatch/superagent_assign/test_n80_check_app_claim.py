"""N80 §5.2 step 4: the pure claim check for an interactive app.

Planner ruling P5: the reserved-author rule is off for apps by default.
Q3 (whether it should apply) is open for the owner.
"""

from __future__ import annotations

import dataclasses

import pytest

from src.tools.auto_dispatch.superagent_assign.feasibility import (
    APP_SKIPPED_CHECKS,
    CANNOT_TAKE_LINE,
    CLI_NOT_FOUND,
    DISABLED,
    IS_LINE_REVIEWER,
    MODEL_NOT_LAUNCHABLE,
    NO_HEADROOM,
    RESERVED_ROLE,
    UNSUPPORTED_RUNTIME,
    AppClaimant,
    AppClaimCheck,
    Choice,
    Snapshot,
    check_app_claim,
    check_feasibility,
)
from src.tools.auto_dispatch.superagent_assign.types import FeasibilityResult

SCOPE = ("src/foo.py",)


def _code(check: AppClaimCheck) -> str:
    return check.result.reason_code


def test_healthy_app_is_ok() -> None:
    check = check_app_claim(AppClaimant("claude-code", can_write=True), write_scope=SCOPE)
    assert check.result == FeasibilityResult.ok()
    assert check.result.is_ok


def test_read_only_app_with_scope_cannot_take_line() -> None:
    check = check_app_claim(AppClaimant("cursor-app", can_write=False), write_scope=SCOPE)
    assert _code(check) == CANNOT_TAKE_LINE


@pytest.mark.parametrize("scope", [(), ("",), ("  ", "")])
def test_read_only_app_with_empty_scope_passes(scope: tuple[str, ...]) -> None:
    check = check_app_claim(AppClaimant("cursor-app", can_write=False), write_scope=scope)
    assert check.result.is_ok


def test_disabled_app_is_refused() -> None:
    app = AppClaimant("claude-code", can_write=True, enabled=False)
    assert _code(check_app_claim(app, write_scope=SCOPE)) == DISABLED


def test_line_reviewer_is_refused() -> None:
    app = AppClaimant("claude-code", can_write=True)
    check = check_app_claim(app, write_scope=SCOPE, line_reviewers=["app:claude-code"])
    assert _code(check) == IS_LINE_REVIEWER


def test_reserved_role_applied_only_when_asked() -> None:
    app = AppClaimant("app:Codex/gpt", can_write=True)
    default = check_app_claim(app, write_scope=SCOPE)
    assert default.result.is_ok
    assert RESERVED_ROLE in default.skipped
    applied = check_app_claim(app, write_scope=SCOPE, reserved_role=True)
    assert _code(applied) == RESERVED_ROLE
    assert RESERVED_ROLE not in applied.skipped
    assert applied.skipped == APP_SKIPPED_CHECKS


def test_reserved_role_applied_lets_other_apps_through() -> None:
    app = AppClaimant("cursor-app", can_write=True)
    assert check_app_claim(app, write_scope=SCOPE, reserved_role=True).result.is_ok


def test_precedence_cannot_take_line_before_disabled() -> None:
    app = AppClaimant("claude", can_write=False, enabled=False)
    check = check_app_claim(
        app, write_scope=SCOPE, line_reviewers=["claude"], reserved_role=True
    )
    assert _code(check) == CANNOT_TAKE_LINE


def test_precedence_disabled_before_reviewer() -> None:
    app = AppClaimant("claude", can_write=True, enabled=False)
    check = check_app_claim(
        app, write_scope=SCOPE, line_reviewers=["claude"], reserved_role=True
    )
    assert _code(check) == DISABLED


def test_precedence_reviewer_before_reserved_role() -> None:
    app = AppClaimant("claude", can_write=True)
    check = check_app_claim(
        app, write_scope=SCOPE, line_reviewers=["app:claude"], reserved_role=True
    )
    assert _code(check) == IS_LINE_REVIEWER


@pytest.mark.parametrize(
    ("app_id", "reviewer"),
    [
        ("claude-code", "app:claude-code"),
        ("claude-code", "APP:Claude-Code"),
        ("claude-code", "APP: Claude-Code"),
        ("claude-code", "app:claude-code:sonnet"),
        ("claude-code", "app:Claude-Code/opus"),
        ("claude-code", "  app:Claude-Code worker"),
        ("app:Claude-Code/opus", "app:claude-code"),
        ("app:Claude-Code/opus", "app:claude-code/sonnet"),
    ],
)
def test_reviewer_identity_is_canonicalised(app_id: str, reviewer: str) -> None:
    app = AppClaimant(app_id, can_write=True)
    check = check_app_claim(app, write_scope=SCOPE, line_reviewers=[reviewer])
    assert _code(check) == IS_LINE_REVIEWER


@pytest.mark.parametrize(
    # A name without ``app:`` is a native provider (the CLI worker), never the app.
    "reviewer", ["app:claude", "claude-code-2", "codex", "", "app:", "claude-code", "Claude-Code worker"]
)
def test_other_reviewers_do_not_match(reviewer: str) -> None:
    app = AppClaimant("claude-code", can_write=True)
    check = check_app_claim(app, write_scope=SCOPE, line_reviewers=[reviewer])
    assert check.result.is_ok


def test_line_reviewers_accepts_any_iterable() -> None:
    app = AppClaimant("claude-code", can_write=True)
    gen = (name for name in ["kimi", "app:claude-code"])
    assert _code(check_app_claim(app, write_scope=SCOPE, line_reviewers=gen)) == (
        IS_LINE_REVIEWER
    )


def test_skipped_default_and_constant() -> None:
    assert APP_SKIPPED_CHECKS == (
        CLI_NOT_FOUND,
        MODEL_NOT_LAUNCHABLE,
        UNSUPPORTED_RUNTIME,
        NO_HEADROOM,
    )
    check = check_app_claim(AppClaimant("x", can_write=True), write_scope=SCOPE)
    assert check.skipped == (*APP_SKIPPED_CHECKS, RESERVED_ROLE)


def test_audit_shape_ok() -> None:
    check = check_app_claim(AppClaimant("x", can_write=True), write_scope=SCOPE)
    assert check.audit() == {
        "feasibility": {"status": "ok"},
        "skipped": {
            "cli_not_found": "interactive_app",
            "model_not_launchable": "interactive_app",
            "unsupported_runtime": "interactive_app",
            "no_headroom": "interactive_app",
            "reserved_role": "interactive_app",
        },
    }


def test_audit_shape_refused() -> None:
    app = AppClaimant("codex", can_write=True)
    check = check_app_claim(app, write_scope=SCOPE, reserved_role=True)
    assert check.audit() == {
        "feasibility": {"status": "infeasible", "reason_code": "reserved_role"},
        "skipped": {code: "interactive_app" for code in APP_SKIPPED_CHECKS},
    }


def test_records_are_frozen() -> None:
    app = AppClaimant("x", can_write=True)
    with pytest.raises(dataclasses.FrozenInstanceError):
        app.app_id = "y"  # type: ignore[misc]
    check = check_app_claim(app, write_scope=SCOPE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        check.skipped = ()  # type: ignore[misc]


WORK = ("kimi", "glm", "grok")


def _snapshot(**overrides: object) -> Snapshot:
    base: dict[str, object] = {
        "eligible_models": WORK,
        "can_take_line": WORK,
        "cli_found": WORK,
        "headroom": {name: 1.0 for name in WORK},
        "now": 1_000.0,
    }
    base.update(overrides)
    return Snapshot(**base)  # type: ignore[arg-type]


def test_check_feasibility_unchanged() -> None:
    assert check_feasibility(Choice(model="kimi"), _snapshot()).is_ok
    assert check_feasibility(Choice(model="claude"), _snapshot()) == (
        FeasibilityResult.infeasible(RESERVED_ROLE)
    )
    assert check_feasibility(
        Choice(model="kimi"), _snapshot(line_reviewers=("kimi",))
    ) == FeasibilityResult.infeasible(IS_LINE_REVIEWER)
    assert check_feasibility(
        Choice(model="kimi"), _snapshot(cli_found=("glm",))
    ) == FeasibilityResult.infeasible(CLI_NOT_FOUND)


def test_feasibility_and_stage_gate_share_one_identity() -> None:
    """Review fix: one normaliser, so both checks agree on every spelling."""
    from src.server.services.autonomy.stage_gate import reviewer_identity
    from src.tools.auto_dispatch.superagent_assign.feasibility import app_identity

    for name in ("app:codex agent", "APP: codex", "app:Codex/gpt", "app:codex:mini"):
        assert reviewer_identity(name) == "app:codex" == app_identity(name)
    # A bare provider is a Worker for the self-review bar, and only a
    # registered app id (AppClaimant.app_id) is read as an app.
    assert reviewer_identity("codex") == "codex"
    check = check_app_claim(AppClaimant("codex", can_write=True), write_scope=SCOPE, line_reviewers=["codex"])
    assert check.result.is_ok
