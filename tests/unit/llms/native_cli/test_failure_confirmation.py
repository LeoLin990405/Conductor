"""N39: a CLI's confirmation refusal is its own class, not a credential failure."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.llms.native_cli.failure import (
    CONFIRMATION_REQUIRED_PROMPT_NOTE,
    CONFIRMATION_REQUIRED_REASON,
    FailureClass,
    classify_failure,
    retry_prompt_note,
)
from src.tools.auto_dispatch.types import Line

STEP_REFUSAL = "Dangerous command requires confirmation (recursive-force-remove)"


@pytest.mark.parametrize("provider", ["stepfun", "step"])
@pytest.mark.parametrize("exit_code", [None, 1, 77, 79])
def test_step_refusal_is_confirmation_required(provider, exit_code):
    result = classify_failure(provider, STEP_REFUSAL, exit_code=exit_code)
    assert result.failure_class is FailureClass.CONFIRMATION_REQUIRED
    assert result.reason_code == CONFIRMATION_REQUIRED_REASON == "worker_confirmation_required"


def test_a_real_auth_failure_in_the_same_log_still_wins():
    """Review repro: model text quoting the rule must not hide a 401."""
    log = (
        "assistant: I grepped failure.py: patterns=('dangerous command requires"
        " confirmation',)\nError: 401 Unauthorized: invalid api key"
    )
    for provider in ("kimi", "stepfun"):
        result = classify_failure(provider, log, exit_code=77)
        assert result.failure_class is FailureClass.AUTH_FAILED, provider


def test_a_real_quota_failure_in_the_same_log_still_wins():
    log = "tool output: No permission client configured for Bash\n...429 insufficient_quota..."
    for provider in ("kimi", "glm"):
        result = classify_failure(provider, log, exit_code=79)
        assert result.failure_class is FailureClass.QUOTA_EXHAUSTED, provider


def test_a_refusal_together_with_auth_reports_auth():
    log = f"{STEP_REFUSAL}\nError: 401 Unauthorized: invalid api key"
    assert classify_failure("stepfun", log, exit_code=77).failure_class is FailureClass.AUTH_FAILED


@pytest.mark.parametrize(
    "text",
    [
        "assistant: the CLI said Dangerous command requires confirmation (x)",
        "patterns=('dangerous command requires confirmation',)",
        "Dangerous command requires confirmation",
        "see: No permission client configured for Bash",
    ],
)
def test_a_quoted_sentence_is_not_a_refusal(text):
    for provider in ("stepfun", "glm"):
        result = classify_failure(provider, text, exit_code=1)
        assert result.failure_class is not FailureClass.CONFIRMATION_REQUIRED


def test_the_refusal_line_inside_a_longer_log_is_found():
    log = f"step: running bash\n  {STEP_REFUSAL}\nexit"
    result = classify_failure("stepfun", log, exit_code=77)
    assert result.failure_class is FailureClass.CONFIRMATION_REQUIRED


def test_a_provider_without_a_rule_does_not_claim_a_refusal():
    result = classify_failure("kimi", STEP_REFUSAL, exit_code=1)
    assert result.failure_class is not FailureClass.CONFIRMATION_REQUIRED


def test_zcode_headless_permission_denial_is_confirmation_required():
    result = classify_failure("glm", "No permission client configured for Bash", exit_code=1)
    assert result.failure_class is FailureClass.CONFIRMATION_REQUIRED
    assert result.reason_code == CONFIRMATION_REQUIRED_REASON


def test_a_plain_auth_failure_is_still_auth():
    result = classify_failure("stepfun", "No API key found", exit_code=77)
    assert result.failure_class is FailureClass.AUTH_FAILED


def test_the_classification_carries_no_diagnostic():
    result = classify_failure("stepfun", STEP_REFUSAL, exit_code=77)
    assert "recursive" not in repr(result)


def test_retry_prompt_note_is_data_for_the_caller():
    assert retry_prompt_note(FailureClass.CONFIRMATION_REQUIRED) == CONFIRMATION_REQUIRED_PROMPT_NOTE
    assert retry_prompt_note("confirmation_required") == CONFIRMATION_REQUIRED_PROMPT_NOTE
    assert "confirmation" in CONFIRMATION_REQUIRED_PROMPT_NOTE
    for other in FailureClass:
        if other not in (
            FailureClass.CONFIRMATION_REQUIRED,
            FailureClass.TURN_BUDGET_EXHAUSTED,
        ):
            assert retry_prompt_note(other) is None


def test_failure_cli_prints_the_class(tmp_path):
    repo = Path(__file__).resolve().parents[4]
    import sys

    completed = subprocess.run(
        [
            sys.executable, "-m", "src.llms.native_cli.failure_cli",
            "--provider", "stepfun", "--text", STEP_REFUSAL,
            "--exit-code", "77", "--no-learned-rules",
        ],
        cwd=repo, capture_output=True, text=True, timeout=120, check=True,
    )
    assert '"failure_class": "confirmation_required"' in completed.stdout
    assert "recursive" not in completed.stdout


@pytest.mark.asyncio
async def test_launcher_exit_77_with_a_refusal_is_not_auth(tmp_path, monkeypatch):
    """The launcher's exit-code fallback must not relabel a named refusal."""
    from src.tools.auto_dispatch.execution import ScriptLauncher, WorkerLaunchFailure

    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo)]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "config", "user.name", "Test"], check=True)
    subprocess.run([*git, "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run([*git, "config", "core.hooksPath", "/dev/null"], check=True)
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        [*git, "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    script = tmp_path / "fake-step"
    script.write_text(f"#!/bin/sh\necho '{STEP_REFUSAL}'\nexit 77\n", encoding="utf-8")
    script.chmod(0o755)
    launcher = ScriptLauncher(
        tmp_path / "runs",
        script=script,
        cwd=repo,
        worktrees_dir=tmp_path / "worktrees",
        dispatch_id="d-confirm",
    )
    line = Line(
        id="line-c",
        name="confirm",
        task="clean up",
        branch="feature/auto-dispatch-d-confirm/line-line-c",
        base_commit=base,
        current_run="d-confirm:line-c:0:one",
    )
    with pytest.raises(WorkerLaunchFailure) as caught:
        await launcher.launch_worker(
            line, "stepfun", [], session_id=None, session_mode=None, handoff_path=None
        )
    assert caught.value.failure_class is FailureClass.CONFIRMATION_REQUIRED
    assert caught.value.reason_code == CONFIRMATION_REQUIRED_REASON
