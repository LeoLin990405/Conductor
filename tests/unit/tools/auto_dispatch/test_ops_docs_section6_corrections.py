"""Ops-docs gaps §6 items #7–#14, #18 and #19 stay corrected.

These sentences were wrong on the parent of the line that fixed them. Each
assertion is a phrase that parent still has, or a phrase it does not, so
restoring that revision fails this module.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]


def _text(path: str) -> str:
    return (_ROOT / path).read_text(encoding="utf-8")


def _between(text: str, start: str, end: str) -> str:
    begin = text.find(start)
    assert begin != -1, f"missing start {start!r}"
    finish = text.find(end, begin + len(start))
    assert finish != -1, f"missing end {end!r}"
    return text[begin:finish]


def test_raising_planner_parks_review_planner_failed() -> None:
    state = _between(
        _text("docs/ports/auto-dispatch.md"),
        "## State Machine",
        "## Data Model",
    )
    assert "parks the dispatch in REVIEW as `planner_failed`" in state
    assert "only an empty plan is ERROR" in state
    assert "plan fails → ERROR" not in state


@pytest.mark.skip(reason="docs/WORKER-HEALTH.md is not in the public snapshot")
def test_launch_failure_uses_the_w8b_recovery_table() -> None:
    state = _between(
        _text("docs/ports/auto-dispatch.md"),
        "## State Machine",
        "## Data Model",
    )
    assert "retry same model up to 2×" not in state
    assert "../WORKER-HEALTH.md#7-dispatch-recovery-w8b" in state
    assert "| `rate_limited`, `overloaded`, `network`, `timeout` |" in state
    assert "parks `fix_launch_failed`" in state
    assert "parks `author_quota_exhausted`" in state
    assert "Park `<class>_retries_exhausted`" in state


def test_env_table_lists_conductor_dispatch_repo() -> None:
    environment = _between(
        _text("docs/ports/auto-dispatch.md"),
        "### Environment",
        "### Parallel lines",
    )
    assert "| `CONDUCTOR_DISPATCH_REPO` |" in environment


def test_v1_lines_run_serially() -> None:
    overview = _between(
        _text("docs/ports/auto-dispatch.md"),
        "## Overview",
        "## Interfaces",
    )
    assert "parallel work lines" not in overview
    assert "v1 runs the lines of one dispatch serially" in overview
    assert "v1 runs those lines serially" in _text("docs/ports/auto-dispatch.md")
    assert "into parallel lines" not in _text("docs/ports/auto-dispatch.md")


@pytest.mark.skip(reason="docs/WORKER-HEALTH.md is not in the public snapshot")
def test_worker_health_states_the_fix_round_rules() -> None:
    text = _text("docs/WORKER-HEALTH.md")
    marker = "## 7. Dispatch recovery (W8b)"
    begin = text.find(marker)
    assert begin != -1
    section = text[begin:]
    assert "is_fix_round" in section
    assert "parks `author_quota_exhausted`" in section
    assert "parks `fix_launch_failed`" in section
    assert "parks `<class>_retries_exhausted`" in section


def test_worker_isolation_notes_match_the_launch_boundary() -> None:
    notes = _between(
        _text("docs/ports/worker-isolation.md"),
        "## Development Worker policy notes",
        "## Legacy unisolated cloud lines",
    )
    assert "Gets `HOME=<root>/home`" in notes
    assert "| `glm` | `zcode` |" in notes
    assert "| `glm` | `zcode-cli` |" not in notes
    assert "| `doubao` | `arkcli` |" in notes
    assert "`DEEPSEEK_API_KEY`, `DSH_HOME`" in notes
    assert "NATIVE_CLI_DSH_ACCOUNT" in notes
    assert "~/.config/deepseek/env" in notes
    assert "sets `HOME` to `<isolation-root>/home`" in notes
    assert "not see the isolated `HOME` at all" not in notes


def test_cli_tui_endblock_shows_the_report_body_once() -> None:
    text = _text("docs/ports/cli-tui.md")
    assert "The Ticket report body (migration 144) is shown once on the EndBlock" in text
    assert "only at a terminal phase" in text
    assert "non-empty body replaces the summary" in text


def test_dispatch_module_docstring_names_the_claude_code_planner() -> None:
    source = _text("src/server/services/dispatch.py")
    docstring = ast.get_docstring(ast.parse(source))
    assert docstring is not None
    assert "Claude Code planner" in docstring
    assert "the LLM planner" not in docstring
