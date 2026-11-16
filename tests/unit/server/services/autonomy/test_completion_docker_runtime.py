"""Completion-report handling for the Docker-backed native Worker.

No test here runs the Docker CLI: ``_patch_launch`` replaces every engine
command with a scripted runner.
"""

import json
from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy import docker_worker_runtime as runtime
from src.server.services.autonomy.completion import (
    COMPLETION_REPORT_MAX_BYTES,
    COMPLETION_REPORT_MISSING,
)
from tests.unit.server.services.autonomy.test_docker_worker_runtime import (
    _patch_launch,
)


@pytest.fixture(autouse=True)
def _no_leftover_sweep(monkeypatch):
    monkeypatch.setattr(runtime, "_sweep_leftovers", AsyncMock())


def _report(
    *,
    status="done",
    summary="Implemented the change.",
    next_step="Send the Ticket to review.",
):
    return json.dumps(
        {
            "status": status,
            "summary": summary,
            "artifacts": {"commits": [], "files": ["src/example.py"], "pr": None},
            "verification": [{"ran": "pytest tests/unit", "result": "passed"}],
            "next": next_step,
            "handbacks": [],
        }
    )


def _message(text, item_id="item_0"):
    return json.dumps(
        {
            "type": "item.completed",
            "item": {"id": item_id, "type": "agent_message", "text": text},
        }
    )


def _codex_stdout(*messages):
    return "\n".join(
        [
            '{"type":"thread.started","thread_id":"t-1"}',
            *(_message(text, f"item_{index}") for index, text in enumerate(messages)),
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":5}}',
        ]
    )


_LAUNCH_TICKET = {"ticket_id": "ticket-1", "user_id": "u1", "workspace_id": "ws-1"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no structured report",
        "done\nAUTONOMY_STATE: ready",
        '{"status":"done","summary":"missing the other fields"}',
    ],
)
def test_missing_or_invalid_completion_report_waits(text):
    parsed = runtime.parse_worker_report(text)

    assert parsed["state"] == "waiting"
    assert parsed["completion"] is None
    assert parsed["reconciled"] is None
    assert parsed["attention_reason"] == COMPLETION_REPORT_MISSING
    assert parsed["body"] == text
    assert "No valid completion report" in parsed["summary"]


def test_valid_completion_report_is_returned_with_no_disagreement():
    parsed = runtime.parse_worker_report(_report())

    assert parsed["state"] == "ready"
    assert parsed["completion"].status == "done"
    assert parsed["completion"].artifacts.files == ("src/example.py",)
    assert parsed["reconciled"].state == "ready"
    assert parsed["reconciled"].disagreement is False
    assert parsed["summary"] == "Implemented the change."
    assert parsed["question"] == ""
    assert json.loads(parsed["body"])["reconciled_state"] == "ready"


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("done", "ready"),
        ("partial", "waiting"),
        ("blocked", "blocked"),
        ("needs_review", "waiting"),
        ("needs_split", "waiting"),
    ],
)
def test_completion_status_controls_worker_state(status, state):
    parsed = runtime.parse_worker_report(
        _report(status=status, next_step="Which branch should I use?")
    )

    assert parsed["state"] == state
    assert parsed["reconciled"].disagreement is False
    assert parsed["question"] == (
        "Which branch should I use?" if status == "blocked" else ""
    )


def test_completion_report_with_an_extra_key_is_not_repaired():
    payload = json.loads(_report())
    payload["unexpected"] = "not in the schema"
    text = json.dumps(payload)

    parsed = runtime.parse_worker_report(text)

    assert parsed["state"] == "waiting"
    assert parsed["completion"] is None
    assert parsed["body"] == text


def test_oversized_completion_report_is_not_repaired():
    text = _report(summary="x" * COMPLETION_REPORT_MAX_BYTES)

    parsed = runtime.parse_worker_report(text)

    assert parsed["state"] == "waiting"
    assert parsed["completion"] is None
    assert parsed["body"] == text


def test_codex_stdout_yields_only_the_final_agent_message():
    stdout = _codex_stdout("Working on it: reading the tests.", _report())

    text, final = runtime._parse_provider_stdout("codex", stdout)
    assert final == _report()
    assert text.startswith("Working on it: reading the tests.")


@pytest.mark.asyncio
async def test_launch_settles_the_reported_claim_without_a_git_check(
    monkeypatch, tmp_path
):
    _patch_launch(monkeypatch, tmp_path, _codex_stdout(_report()))

    result = await runtime.launch(_LAUNCH_TICKET, "do the work")

    assert result["state"] == "ready"
    assert result["completion"].status == "done"
    assert result["reconciled"].disagreement is False
    assert result["git_check"] == "unsupported_runtime"
    assert result["text"] == _report()


@pytest.mark.asyncio
async def test_launch_reads_the_report_after_a_progress_message(
    monkeypatch, tmp_path
):
    """Regression: a progress message must not be joined to the final report."""
    _patch_launch(
        monkeypatch,
        tmp_path,
        _codex_stdout("Working on it: reading the tests.", _report()),
    )

    result = await runtime.launch(_LAUNCH_TICKET, "do the work")

    assert result["state"] == "ready"
    assert result["completion"] is not None
    assert result["completion"].status == "done"
    assert result["attention_reason"] is None
    assert result["text"].startswith("Working on it: reading the tests.")


@pytest.mark.asyncio
async def test_launch_does_not_default_marker_only_output_to_ready(
    monkeypatch, tmp_path
):
    _patch_launch(
        monkeypatch,
        tmp_path,
        _codex_stdout("Implemented the change.\nAUTONOMY_STATE: ready"),
    )

    result = await runtime.launch(_LAUNCH_TICKET, "do the work")

    assert result["state"] == "waiting"
    assert result["completion"] is None
    assert result["attention_reason"] == COMPLETION_REPORT_MISSING


@pytest.mark.asyncio
async def test_launch_with_empty_final_output_waits_instead_of_failing(
    monkeypatch, tmp_path
):
    _patch_launch(monkeypatch, tmp_path, _codex_stdout(""))

    result = await runtime.launch(_LAUNCH_TICKET, "do the work")

    assert result["state"] == "waiting"
    assert result["completion"] is None
    assert result["attention_reason"] == COMPLETION_REPORT_MISSING
    assert result["text"] == ""


def test_missing_report_keeps_the_whole_output_for_review():
    parsed = runtime.parse_worker_report(
        "Working on it.", raw_text="Working on it.\nStill going."
    )

    assert parsed["state"] == "waiting"
    assert parsed["body"] == "Working on it.\nStill going."


@pytest.mark.parametrize(("status", "state"), [("done", "dropped"), ("needs_review", "waiting")])
def test_speculative_reports_use_the_speculative_table(status, state):
    parsed = runtime.parse_worker_report(_report(status=status), speculative=True)

    assert parsed["state"] == state


@pytest.mark.asyncio
async def test_speculative_launch_with_nothing_to_do_is_dropped(monkeypatch, tmp_path):
    _patch_launch(monkeypatch, tmp_path, _codex_stdout(_report(status="done")))

    result = await runtime.launch(
        {**_LAUNCH_TICKET, "mandate": {"speculative": True}}, "look only"
    )

    assert result["state"] == "dropped"


def test_a_failed_codex_turn_without_a_message_is_a_worker_failure():
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"t-1"}',
            '{"type":"turn.failed","error":{"message":"usage limit"}}',
        ]
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="no final message"):
        runtime._parse_provider_stdout("codex", stdout)
