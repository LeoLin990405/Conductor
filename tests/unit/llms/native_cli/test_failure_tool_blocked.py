"""Step's safety gate ending a run is worker_tool_blocked, not auth (2026-11-13).

R11 line-3 on stepfun exited 77 (``worker_auth_failed``): the real cause was
Step's own gate refusing a heredoc it could not run unattended ("no
interactive approval is available"). The login was fine. Such a block is its
own class, ``tool_blocked``, and the recovery hands the line to another
Worker. N39's "Dangerous command requires confirmation" line keeps its own
class, ``confirmation_required``.

Also pins grok's exit 77 "max turns reached" (#143) with the exit code the
launcher actually reports.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from src.conductor_cli.workers import FAILURE_CLASSES
from src.llms.native_cli.failure import (
    TOOL_BLOCKED_REASON,
    FailureClass,
    classify_failure,
    failure_rules,
    register_failure_rule,
    retry_prompt_note,
)
from src.tools.auto_dispatch import worker_recovery
from src.tools.auto_dispatch.liveness.consistency import REASON_STAGES
from src.tools.auto_dispatch.liveness.policy import DEFAULT_POLICY, HUMAN
from src.tools.auto_dispatch.worker_recovery import Action, decide

NOW = datetime(2026, 11, 14, tzinfo=UTC)

# The four sentences Step's formatUnattendedBlockReason ends a run with,
# other than the hazardous (N39) one. Synthetic reasons in front.
_ANALYSIS = (
    "Shell command could not be fully analyzed (heredoc); explicit approval is"
    " required. No interactive approval is available. Use a supported literal"
    " command or review it in an interactive session."
)
_REFUSED = (
    "Bash requires approval (no interactive approval is available, and this"
    " run's policy denies unattended approvals). Permit them with"
    " --non-interactive-approval allow or --approval-mode auto."
)
_UNCONFIGURED = (
    "Write requires approval (no interactive approval is available and no"
    " permission preset is configured). Permit unattended writes with"
    " --non-interactive-approval allow or --approval-mode auto."
)
_BARE = "Edit requires approval (no interactive approval is available)."
_HAZARDOUS = (
    "Dangerous command requires confirmation (destructive-git): git reset --hard"
    " (no interactive approval is available). Dangerous commands always require"
    " interactive confirmation; no flag or preset overrides that. Run it in an"
    " interactive session."
)
# The gate's "continue" mode: a tool result the run went on from.
_CONTINUE = (
    "Bash requires approval No interactive approval is available in this run, so"
    " the call was not executed. Do not retry it verbatim; use a safer"
    " equivalent or continue the task without it."
)


def _event(**fields: object) -> str:
    return json.dumps(fields, separators=(",", ":"))


@pytest.mark.parametrize("line", [_ANALYSIS, _REFUSED, _UNCONFIGURED, _BARE])
@pytest.mark.parametrize("provider", ["stepfun", "step"])
def test_the_gate_block_is_tool_blocked(provider: str, line: str) -> None:
    result = classify_failure(provider, line, exit_code=77)
    assert result.failure_class is FailureClass.TOOL_BLOCKED
    assert result.reason_code == TOOL_BLOCKED_REASON == "worker_tool_blocked"
    assert retry_prompt_note(result.failure_class) is None


def test_a_gate_block_ending_the_run_outranks_auth_words_in_the_model_text() -> None:
    """The R11 shape: the heredoc's text talks about logins and 401s; the
    gate's block is the run's last line, so the provider answered."""
    text = "\n".join(
        [
            _event(type="text", text="Runbook: on 401 Unauthorized, not logged in, re-login."),
            _event(type="tool_call", input="python3 - <<'EOF'\ninvalid api key\nEOF"),
            _ANALYSIS,
        ]
    )
    result = classify_failure("stepfun", text, exit_code=77)
    assert result.failure_class is FailureClass.TOOL_BLOCKED


def test_a_gate_sentence_far_from_the_end_does_not_outrank_a_real_auth_failure() -> None:
    text = "\n".join(
        [_REFUSED] + [_event(type="text", text=f"step {n}") for n in range(10)]
        + ["401 Unauthorized: invalid api key"]
    )
    assert classify_failure("stepfun", text, exit_code=1).failure_class is (
        FailureClass.AUTH_FAILED
    )
    # Without the credential failure, the earlier block still names the class.
    text = "\n".join([_REFUSED] + [_event(type="text", text=f"step {n}") for n in range(10)])
    assert classify_failure("stepfun", text, exit_code=1).failure_class is (
        FailureClass.TOOL_BLOCKED
    )


def test_the_hazardous_line_stays_confirmation_required() -> None:
    result = classify_failure("stepfun", _HAZARDOUS, exit_code=1)
    assert result.failure_class is FailureClass.CONFIRMATION_REQUIRED


def test_the_continue_mode_tool_result_is_not_a_block() -> None:
    result = classify_failure("stepfun", _CONTINUE, exit_code=1)
    assert result.failure_class is FailureClass.CRASH


def test_other_providers_do_not_read_steps_sentences() -> None:
    assert classify_failure("kimi", _ANALYSIS, exit_code=1).failure_class is (
        FailureClass.CRASH
    )


def test_a_terminal_rule_may_be_a_tool_block_but_never_auth() -> None:
    with pytest.raises(ValueError):
        register_failure_rule(
            "test-terminal-auth", FailureClass.AUTH_FAILED, line_patterns=("^x$",), terminal=True
        )
    with pytest.raises(ValueError):
        register_failure_rule(
            "test-terminal-tb", FailureClass.TOOL_BLOCKED, patterns=("x",), terminal=True
        )
    register_failure_rule(
        "test-terminal-tb", FailureClass.TOOL_BLOCKED, line_patterns=("^blocked x$",), terminal=True
    )
    # Registering the same rule again adds nothing.
    before = failure_rules("test-terminal-tb")
    register_failure_rule(
        "test-terminal-tb", FailureClass.TOOL_BLOCKED, line_patterns=("^blocked x$",), terminal=True
    )
    assert failure_rules("test-terminal-tb") == before
    assert classify_failure("test-terminal-tb", "401 unauthorized\nblocked x").failure_class is (
        FailureClass.TOOL_BLOCKED
    )


# ---------------------------------------------------------------- recovery


def test_a_tool_block_reassigns_to_another_worker() -> None:
    for used in (0, 1, 5):
        action = decide(FailureClass.TOOL_BLOCKED, None, {"tool_blocked": used}, False, NOW)
        assert action == Action("reassign_fallback", reason="worker_tool_blocked")
    # Not a same-worker retry, not auth, and it spends no other budget.
    action = decide("tool_blocked", None, {"crash": 1, "unknown": 1}, False, NOW)
    assert action.kind == "reassign_fallback"
    assert action.prompt_note is None


def test_a_tool_block_in_a_fix_round_parks_with_its_own_reason() -> None:
    assert decide("tool_blocked", None, {}, True, NOW) == Action(
        "park", reason="worker_tool_blocked"
    )


def test_the_class_is_in_every_table_that_enumerates_classes() -> None:
    assert "tool_blocked" in worker_recovery._FAILURE_CLASSES
    assert "tool_blocked" in FAILURE_CLASSES
    assert {c.value for c in FailureClass} == set(worker_recovery._FAILURE_CLASSES)
    assert {c.value for c in FailureClass} == set(FAILURE_CLASSES)
    assert "worker_tool_blocked" in REASON_STAGES
    # The worker recovery owns the class; the liveness sweep adds no step.
    assert DEFAULT_POLICY.group_for("worker_tool_blocked") == HUMAN


# ------------------------------------------------- grok exit 77 (#143 check)


def test_grok_exit_77_max_turns_with_auth_words_is_a_turn_budget() -> None:
    text = "\n".join(
        [
            _event(type="text", text="Checking: 401 Unauthorized, not logged in."),
            _event(type="tool_call_update", output="invalid api key (HTTP 403)"),
            "Error: max turns reached",
        ]
    )
    result = classify_failure("grok", text, exit_code=77)
    assert result.failure_class is FailureClass.TURN_BUDGET_EXHAUSTED
    assert result.failure_class is not FailureClass.AUTH_FAILED
