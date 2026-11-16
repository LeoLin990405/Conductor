"""Pins for ops-docs-gaps section 6 items #1, #2, #3, #4, #6, #16 and #17.

These sentences used to name a schema head or a rollback range, tell ``live``
to need an LLM service, and install or launch from ``~/conductor-wt/int-r2``.
The operational sentences now point at ``MIGRATIONS.md``, and a daemon checkout
is exported before the shell expands ``--cwd``. Historical measurements of a
named commit stay (the v1 gate's single head ``143``, the evidence rows that
printed ``141`` or ``140``).

``docs/README.en.md`` and ``docs/README.zh-CN.md`` are product blurbs. They
never stated a head, a rollback range, an int-r2 install, or a live-dispatch
prerequisite, so this file does not pin them.

The checks take the document text, so a checkout of the parent revision fails
them. ``tests/unit/test_docs_name_real_tests.py`` only checks that a named
test file exists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_FENCE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
_REPO_ASSIGNMENT = re.compile(r"(^|\s)CONDUCTOR_REPO_ROOT=")
_EXPORT_CHECKOUT = re.compile(r"^export CONDUCTOR_REPO_ROOT=/path/to/conductor$")
_DAEMON_CWD = '--cwd "$CONDUCTOR_REPO_ROOT"'

# Phrases that stated the current head or the rollback range as a number.
_STALE_HEAD_RANGE = (
    "from 141 down to",
    "from 140 down to",
    "the head is 141",
    "head is 140",
    "prints `141 (head)`",
)

_POST_V1 = (
    "## Post-v1 changes (#22–#27)",
    "**#22.**",
    "planner_failed",
    "**#23.**",
    "144_ticket_report_body",
    "**#24.**",
    "CONDUCTOR_DISPATCH_PLANNER=claude-code",
    "server's working directory",
    "**#25.**",
    "EndBlock",
    "**#26.**",
    "DEEPSEEK_API_KEY",
    "NATIVE_CLI_DSH_ACCOUNT",
    "**#27.**",
    "fix_launch_failed",
    "author_quota_exhausted",
    "_retries_exhausted",
)


def _text(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _logical_commands(script: str) -> list[str]:
    """Shell commands after joining backslash continuations. Comments drop out."""
    commands: list[str] = []
    pending: list[str] = []
    for raw in script.splitlines():
        line = raw.strip()
        if not pending and (not line or line.startswith("#")):
            continue
        continued = line.endswith("\\")
        piece = line[:-1].rstrip() if continued else line
        if piece.startswith("#") and not pending:
            continue
        if piece:
            pending.append(piece)
        if not continued:
            command = " ".join(pending)
            pending = []
            if command and not command.startswith("#"):
                commands.append(command)
    if pending:
        commands.append(" ".join(pending))
    return commands


def _daemon_launch(text: str) -> str:
    bodies = [
        match.group(1)
        for match in _FENCE.finditer(text)
        if "scripts/conductor_local_daemon.py" in match.group(1)
    ]
    assert len(bodies) == 1, "expected one daemon-launch bash fence"
    return bodies[0]


def assert_daemon_checkout_is_exported_before_cwd(text: str) -> None:
    """``--cwd "$CONDUCTOR_REPO_ROOT"`` expands before a prefix assignment.

    The export has to be its own earlier statement, and it has to overwrite
    any value the shell already has. A same-command ``CONDUCTOR_REPO_ROOT=``
    changes only the child environment.
    """
    commands = _logical_commands(_daemon_launch(text))
    launches = [command for command in commands if "scripts/conductor_local_daemon.py" in command]
    assert len(launches) == 1, launches
    launch = launches[0]
    earlier = commands[: commands.index(launch)]
    assert any(_EXPORT_CHECKOUT.match(command) for command in earlier), earlier
    assert _DAEMON_CWD in launch, launch
    assert not _REPO_ASSIGNMENT.search(launch), launch
    assert "int-r2" not in launch, launch


def assert_release_readiness(text: str) -> None:
    missing = [phrase for phrase in _POST_V1 if phrase not in text]
    assert not missing, missing
    assert "The current schema head, and the range of a hand-run `alembic downgrade`, are in [MIGRATIONS.md](../MIGRATIONS.md)." in text
    assert "confirm `alembic heads` matches the single head in" in text
    assert "for the range that file states" in text
    stale = [phrase for phrase in _STALE_HEAD_RANGE if phrase in text]
    assert not stale, stale
    # The v1 measurement and the named-commit evidence stay.
    assert "single head `143`" in text
    assert "printed `141 (head)`" in text
    assert "prints `140 (head)`" in text


def assert_operations(text: str) -> None:
    assert "`claude` CLI" in text
    assert "CONDUCTOR_DISPATCH_PLANNER=claude-code" in text
    assert "CONDUCTOR_DISPATCH_REPO" in text
    assert "server's working directory" in text
    assert "CONDUCTOR_DISPATCH_LAUNCH_SCRIPT" in text
    assert "the current head is in [`../MIGRATIONS.md`](../MIGRATIONS.md)" in text
    assert "the range in" in text and "[`../MIGRATIONS.md`](../MIGRATIONS.md)" in text
    assert "cond foreground daemon --provider <provider> --id <id>" in text
    assert "A daemon other than Codex" in text
    absent = [
        "an LLM service and git",
        "the head is 141",
        "from 141 down to",
        "CONDUCTOR_DAEMON_PROVIDER",
        "a local Codex daemon",
    ]
    stale = [phrase for phrase in absent if phrase in text]
    assert not stale, stale


def assert_cli_superagent(text: str) -> None:
    assert "export CONDUCTOR_REPO_ROOT=/path/to/conductor" in text
    assert "ln -sfn /path/to/conductor/scripts/cond" in text
    assert "cond foreground daemon --provider cursor --id cursor-id" in text
    assert "current head: ../MIGRATIONS.md" in text
    assert "The current head is in [`../MIGRATIONS.md`](../MIGRATIONS.md)." in text
    absent = [
        "~/conductor-wt/int-r2",
        "int-r2 backend",
        "head is 140",
        "CONDUCTOR_DAEMON_PROVIDER",
    ]
    stale = [phrase for phrase in absent if phrase in text]
    assert not stale, stale
    assert_daemon_checkout_is_exported_before_cwd(text)
    # The 2026-11-05 re-run's printed head stays a measurement.
    assert "printed a single Alembic head of `140`" in text


def assert_cond(text: str) -> None:
    assert "ln -sfn /path/to/conductor/scripts/cond ~/.local/bin/cond" in text
    assert "A daemon other than Codex" in text
    assert "cond foreground daemon --provider cursor --id cursor-id" in text
    assert "CONDUCTOR_REPO_ROOT" in text
    assert "~/conductor-wt/int-r2" not in text
    assert "int-r2 backend" not in text


def assert_launchd(text: str) -> None:
    assert "com.conductor.supervisor.plist" in text
    assert "CONDUCTOR_REPO_ROOT" in text
    assert "there is no" in text and "~/conductor-wt/int-r2" in text
    assert "cond foreground daemon --provider cursor --id cursor-id" in text
    assert "runs the int-r2 backend" not in text
    assert "cond __daemon-foreground" not in text


@pytest.mark.skip(reason="docs/RELEASE-READINESS.md is not in the public snapshot")
def test_release_readiness_points_at_migrations_and_records_post_v1():
    assert_release_readiness(_text("docs/RELEASE-READINESS.md"))


def test_operations_live_needs_claude_and_names_no_head():
    assert_operations(_text("docs/operations.md"))


def test_cli_exports_the_daemon_checkout_before_cwd_expands():
    assert_cli_superagent(_text("docs/CLI-SUPERAGENT.md"))


def test_cond_describes_a_daemon_other_than_codex():
    assert_cond(_text("docs/COND.md"))


def test_launchd_readme_uses_the_supervisor_and_this_checkout():
    assert_launchd(_text("scripts/launchd/README.md"))


def test_a_prefix_assignment_does_not_count_as_exporting_the_checkout():
    broken = """```bash
CONDUCTOR_REPO_ROOT=/path/to/conductor \\
.venv/bin/python scripts/conductor_local_daemon.py \\
  --cwd "$CONDUCTOR_REPO_ROOT"
```
"""
    try:
        assert_daemon_checkout_is_exported_before_cwd(broken)
    except AssertionError:
        return
    raise AssertionError("inline assignment was accepted")


def test_an_export_that_keeps_the_existing_value_is_rejected():
    kept = """```bash
export CONDUCTOR_REPO_ROOT="${CONDUCTOR_REPO_ROOT:-/path/to/conductor}"
.venv/bin/python scripts/conductor_local_daemon.py --cwd "$CONDUCTOR_REPO_ROOT"
```
"""
    try:
        assert_daemon_checkout_is_exported_before_cwd(kept)
    except AssertionError:
        return
    raise AssertionError("a default that keeps the existing checkout was accepted")
