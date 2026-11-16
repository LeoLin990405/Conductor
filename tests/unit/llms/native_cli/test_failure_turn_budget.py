"""A CLI that stops at its own --max-turns spent a turn budget (2026-11-13).

Grok exited with ``Error: max turns reached`` (``stopReason``
``max_turn_requests``) after a long run whose log also held credential words
from the model's text and tool output, and the run was classified
``worker_auth_failed``: parked for a re-login nobody needed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from src.llms.native_cli.failure import (
    TURN_BUDGET_EXHAUSTED_PROMPT_NOTE,
    FailureClass,
    classify_failure,
    register_failure_rule,
    retry_prompt_note,
)
from src.tools.auto_dispatch.liveness.policy import DEFAULT_REASON_GROUPS, R_SPLIT


def _event(**fields: object) -> str:
    return json.dumps(fields, separators=(",", ":"))


# A grok streaming-json run: model text and tool output carry auth words and
# a 401, JSON-encoded; the CLI's own lines end the run.
_GROK_RUN = "\n".join(
    [
        _event(type="text", text="Reading the auth code: 401 Unauthorized, not logged in."),
        _event(
            type="tool_call_update",
            output='raise NativeCliUnavailable("invalid api key") # HTTP 403',
        ),
        _event(type="end", stopReason="max_turn_requests", sessionId="s-1"),
        "Error: max turns reached",
    ]
)


def test_grok_max_turns_is_a_turn_budget_not_auth() -> None:
    result = classify_failure("grok", _GROK_RUN, exit_code=1)
    assert result.failure_class is FailureClass.TURN_BUDGET_EXHAUSTED
    assert result.reason_code == "grok_max_turns_reached"


def test_grok_end_event_alone_is_a_turn_budget() -> None:
    text = "\n".join(_GROK_RUN.splitlines()[:3])
    result = classify_failure("grok", text, exit_code=1)
    assert result.failure_class is FailureClass.TURN_BUDGET_EXHAUSTED
    assert result.reason_code == "worker_turn_budget_exhausted"


def test_zcode_and_claude_result_types_are_a_turn_budget() -> None:
    zcode = json.dumps(
        {"type": "turn.completed", "payload": {"resultType": "error_max_turns"}}
    )
    claude = _event(type="result", subtype="error_max_turns", is_error=True)
    for provider, text in (("zcode", zcode), ("glm", zcode), ("claude", claude)):
        result = classify_failure(provider, f"401 unauthorized\n{text}", exit_code=1)
        assert result.failure_class is FailureClass.TURN_BUDGET_EXHAUSTED, provider


def test_a_stop_marker_quoted_inside_a_string_is_not_the_cli_stopping() -> None:
    """Tool output that prints the marker reaches the log escaped; a real
    credential failure in the same log still wins."""
    text = "\n".join(
        [
            _event(type="tool_call_update", output='{"stopReason":"max_turns"}'),
            _event(type="tool_call_update", output='"resultType": "error_max_turns"'),
            "Unauthorized (401) from messages API error",
        ]
    )
    result = classify_failure("grok", text, exit_code=1)
    assert result.failure_class is FailureClass.AUTH_FAILED


def test_a_nested_stop_earlier_in_the_log_is_not_this_runs_stop() -> None:
    """A sub-agent's own ``stopReason`` inside tool output is a nested object,
    not escaped; only the log's last lines are the CLI's end of run."""
    nested = _event(
        type="tool_call_update",
        rawOutput={"stopReason": "max_turn_requests", "resultType": "error_max_turns"},
    )
    text = "\n".join(
        [nested]
        + [_event(type="text", text=f"step {n}") for n in range(10)]
        + ["401 Unauthorized: invalid api key"]
    )
    assert classify_failure("grok", text, exit_code=1).failure_class is FailureClass.AUTH_FAILED


def test_the_loose_phrase_is_a_turn_budget_but_never_outranks_auth() -> None:
    assert (
        classify_failure("kimi", "max turns reached", exit_code=1).failure_class
        is FailureClass.TURN_BUDGET_EXHAUSTED
    )
    # The phrase alone could be the model's text: a credential failure wins.
    assert (
        classify_failure("kimi", "401 Unauthorized\nmax turns", exit_code=1).failure_class
        is FailureClass.AUTH_FAILED
    )
    # And a full context is still a full context, even next to the phrase.
    assert (
        classify_failure(
            "kimi", "model said: max turns\ncontext_length_exceeded", exit_code=1
        ).failure_class
        is FailureClass.CONTEXT_EXHAUSTED
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"failure_class": FailureClass.AUTH_FAILED, "line_patterns": ("^x$",)},
        {"failure_class": FailureClass.TURN_BUDGET_EXHAUSTED, "patterns": ("x",)},
        {"failure_class": FailureClass.TURN_BUDGET_EXHAUSTED},
    ],
)
def test_only_a_line_shaped_turn_budget_rule_may_be_terminal(kwargs) -> None:
    failure_class = kwargs.pop("failure_class")
    with pytest.raises(ValueError):
        register_failure_rule("test-terminal", failure_class, terminal=True, **kwargs)


def test_the_continuation_carries_a_note_and_a_split_follows() -> None:
    assert retry_prompt_note("turn_budget_exhausted") == TURN_BUDGET_EXHAUSTED_PROMPT_NOTE
    # The note's echo in the next run's log must not decide that run's class.
    assert (
        classify_failure("kimi", TURN_BUDGET_EXHAUSTED_PROMPT_NOTE).failure_class
        is FailureClass.UNKNOWN
    )
    assert DEFAULT_REASON_GROUPS["turn_budget_exhausted_retries_exhausted"] == R_SPLIT


def test_the_launcher_classifier_prints_the_class(tmp_path: Path) -> None:
    """dispatch_launch.sh reads this class; any class it has no exit code
    for exits 79, where the dispatcher keeps the classifier's class."""
    log = tmp_path / "grok.log"
    log.write_text(_GROK_RUN + "\n", encoding="utf-8")
    repo = Path(__file__).resolve().parents[4]
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "src.llms.native_cli.failure_cli",
            "--provider",
            "grok",
            "--diagnostic-file",
            str(log),
            "--exit-code",
            "1",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert json.loads(completed.stdout)["failure_class"] == "turn_budget_exhausted"
