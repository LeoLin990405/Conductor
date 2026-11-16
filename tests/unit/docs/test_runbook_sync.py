"""docs/RUNBOOK.md stays aligned with the CLI and the dispatch park reasons.

The runbook is the operator's first stop, so a command it names must exist and
a park reason the drive can write must have a row. Everything compared here is
read from the tree at test time:

- ``conductor`` commands come from the argparse parser that
  ``src/conductor_cli/cli.py`` builds in-process, and ``conductor dispatch``
  verbs from ``dispatch.VERBS``. The verbs are matched by hand, and an unknown
  first word starts a real dispatch, so a misspelt verb in the runbook is the
  most dangerous kind of drift.
- ``cond`` verbs come from the ``case`` table in ``scripts/cond``, and their
  second word from ``ops_cli.build_parser()``.
- Park reasons come from ``liveness/consistency.py`` (every reason a parked or
  failed line may carry), ``planner_reason.PLANNER_FAILURE_REASONS``, and the
  recovery group of each reason from ``liveness/policy.py``.

Nothing is run: the parsers are built, never invoked, and no network or
database is touched.
"""

from __future__ import annotations

import argparse
import re
from functools import lru_cache
from pathlib import Path

import pytest

from src.conductor_cli.cli import build_parser as build_conductor_parser
from src.conductor_cli.dispatch import VERBS as DISPATCH_VERBS
from src.conductor_cli.ops_cli import build_parser as build_ops_parser
from src.tools.auto_dispatch import planner_reason
from src.tools.auto_dispatch.liveness import consistency, policy

_ROOT = Path(__file__).resolve().parents[3]
_RUNBOOK = _ROOT / "docs" / "RUNBOOK.md"
_README = _ROOT / "README.md"
_COND = _ROOT / "scripts" / "cond"

# `scripts/cond` maps these to the ops CLI verb it forwards.
_COND_ALIASES = {"start": "up", "stop": "down"}

_FENCE = re.compile(r"^```[^\n]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_SPAN = re.compile(r"`([^`\n]+)`")
# A command word, not a path segment (`~/.conductor`, `src/conductor_cli`,
# `com.conductor.*`) and not part of a longer name (`conductor_restore_drill`).
_INVOCATION = re.compile(r"(?<![\w./~<-])(conductor|cond)(?![\w./-])")
_WORD = re.compile(r"^[a-z][a-z0-9-]*$")
_CASE_LABEL = re.compile(r"^\s*([A-Za-z|-]+)\)\s*$", re.MULTILINE)
_REASON_ROW = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|(.*)$", re.MULTILINE)
_README_LINK = re.compile(r"\]\(docs/RUNBOOK\.md(?:#[^)]*)?\)")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_texts(markdown: str) -> list[str]:
    """Fenced block lines (shell comments dropped) and inline code spans."""
    texts: list[str] = []
    for block in _FENCE.finditer(markdown):
        for line in block.group(1).splitlines():
            code = line.split("#", 1)[0].strip()
            if code:
                texts.append(code)
    prose = _FENCE.sub("", markdown)
    texts.extend(match.group(1) for match in _SPAN.finditer(prose))
    return texts


def _invocations(markdown: str) -> list[tuple[str, str, str]]:
    """``(program, word, second)`` for each command named in code.

    ``second`` is empty when the next token is a flag, a placeholder or absent.
    A first token that is not a command word (a path after ``--dbname
    conductor``, a quoted goal) is not an invocation.
    """
    found: list[tuple[str, str, str]] = []
    for code in _code_texts(markdown):
        for match in _INVOCATION.finditer(code):
            rest = re.split(r"&&|;|\|\||(?<!\\)\|", code[match.end() :], maxsplit=1)[0]
            tokens = rest.split()
            if not tokens or not _WORD.match(tokens[0]):
                continue
            second = tokens[1] if len(tokens) > 1 and _WORD.match(tokens[1]) else ""
            item = (match.group(1), tokens[0], second)
            if item not in found:
                found.append(item)
    return found


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _second_words(parser: argparse.ArgumentParser) -> set[str] | None:
    """Words allowed after a command, or None when it takes free text."""
    nested = _subcommands(parser)
    if nested:
        return set(nested)
    for action in parser._actions:
        if not action.option_strings and action.choices:
            return {str(choice) for choice in action.choices}
    return None


@lru_cache(maxsize=1)
def _conductor_commands() -> dict[str, argparse.ArgumentParser]:
    commands = _subcommands(build_conductor_parser())
    assert commands, "conductor build_parser() has no subcommands"
    return commands


@lru_cache(maxsize=1)
def _ops_commands() -> dict[str, argparse.ArgumentParser]:
    commands = _subcommands(build_ops_parser())
    assert commands, "ops_cli build_parser() has no subcommands"
    return commands


@lru_cache(maxsize=1)
def _cond_verbs() -> frozenset[str]:
    """The words ``scripts/cond`` dispatches in its ``case "$cmd"`` table."""
    labels: set[str] = set()
    for match in _CASE_LABEL.finditer(_text(_COND)):
        labels.update(part for part in match.group(1).split("|") if _WORD.match(part))
    assert {"up", "down", "status"} <= labels, f"scripts/cond case table not found: {sorted(labels)}"
    return frozenset(labels)


def _unknown_commands(markdown: str) -> list[str]:
    """Every ``conductor``/``cond`` invocation in code the CLI does not define."""
    bad: list[str] = []
    for program, word, second in _invocations(markdown):
        if program == "conductor":
            commands = _conductor_commands()
            if word not in commands:
                bad.append(f"conductor {word}")
                continue
            if word == "dispatch":
                allowed: set[str] | None = set(DISPATCH_VERBS)
            else:
                allowed = _second_words(commands[word])
        else:
            if word not in _cond_verbs():
                bad.append(f"cond {word}")
                continue
            ops = _ops_commands().get(_COND_ALIASES.get(word, word))
            allowed = _second_words(ops) if ops is not None else None
        if second and allowed is not None and second not in allowed:
            bad.append(f"{program} {word} {second}")
    return bad


def _park_section(markdown: str) -> str:
    match = re.search(r"^## Park reasons\s*$", markdown, re.MULTILINE)
    assert match, "RUNBOOK.md has no '## Park reasons' section"
    rest = markdown[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def _reason_rows(markdown: str) -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for match in _REASON_ROW.finditer(_park_section(markdown)):
        cells = [cell.strip() for cell in match.group(2).strip().strip("|").split("|")]
        rows[match.group(1)] = cells
    return rows


def _defined_reasons() -> frozenset[str]:
    reasons = frozenset(consistency.KNOWN_REASONS) | frozenset(planner_reason.PLANNER_FAILURE_REASONS)
    assert reasons, "the dispatch code defines no park reasons"
    return reasons


def _missing_reasons(markdown: str) -> list[str]:
    rows = _reason_rows(markdown)
    return sorted(reason for reason in _defined_reasons() if reason not in rows)


def _wrong_groups(markdown: str) -> list[str]:
    """Rows whose Group cell is not the recovery group the policy assigns."""
    wrong: list[str] = []
    for reason, cells in _reason_rows(markdown).items():
        if reason not in consistency.BLOCK_REASONS:
            continue
        expected = policy.DEFAULT_POLICY.group_for(reason)
        stated = cells[0] if cells else ""
        if stated != expected:
            wrong.append(f"{reason}: runbook {stated!r}, policy {expected!r}")
    return wrong


def test_runbook_names_only_real_commands() -> None:
    markdown = _text(_RUNBOOK)
    assert _invocations(markdown), "RUNBOOK.md names no commands in code"
    bad = _unknown_commands(markdown)
    assert not bad, "RUNBOOK.md names commands the CLI does not define: " + "; ".join(bad)


def test_runbook_covers_every_dispatch_verb() -> None:
    named = {second for program, word, second in _invocations(_text(_RUNBOOK)) if (program, word) == ("conductor", "dispatch")}
    missing = [verb for verb in DISPATCH_VERBS if verb not in named]
    assert not missing, f"RUNBOOK.md does not show conductor dispatch {missing}"


def test_runbook_lists_every_park_reason() -> None:
    missing = _missing_reasons(_text(_RUNBOOK))
    assert not missing, f"RUNBOOK.md has no row for park reasons {missing}"


def test_runbook_lists_no_unknown_park_reason() -> None:
    stale = sorted(set(_reason_rows(_text(_RUNBOOK))) - _defined_reasons())
    assert not stale, f"RUNBOOK.md documents reasons the code does not define: {stale}"


def test_runbook_group_matches_recovery_policy() -> None:
    wrong = _wrong_groups(_text(_RUNBOOK))
    assert not wrong, "; ".join(wrong)


def test_readme_links_runbook() -> None:
    assert _README_LINK.search(_text(_README)), "README.md does not link docs/RUNBOOK.md"


@pytest.mark.parametrize(
    ("snippet", "expected"),
    (
        ("`conductor frobnicate`", "conductor frobnicate"),
        ("`conductor dispatch frobnicate <id>`", "conductor dispatch frobnicate"),
        ("`conductor logs frobnicate`", "conductor logs frobnicate"),
        ("`conductor deploy frobnicate`", "conductor deploy frobnicate"),
        ("```bash\ncond frobnicate\n```\n", "cond frobnicate"),
        ("`cond launchd frobnicate`", "cond launchd frobnicate"),
        ("`cond restart nowhere`", "cond restart nowhere"),
    ),
)
def test_a_bogus_command_is_reported(tmp_path: Path, snippet: str, expected: str) -> None:
    """The scan is not vacuous: a made-up command in a copy of the runbook fails."""
    copy = tmp_path / "RUNBOOK.md"
    copy.write_text(_text(_RUNBOOK) + "\n" + snippet + "\n", encoding="utf-8")
    assert expected in _unknown_commands(_text(copy))


@pytest.mark.parametrize(
    "snippet",
    (
        "`conductor dispatch recover <id> <line-id> --split`",
        "`conductor dispatch start \"<goal>\"`",
        "`conductor deploy status`",
        "`cond restart daemons`",
        "`cond stop --force`",
        "`cond logs backend`",
        "`~/.conductor/logs/backend.log`",
        "`src/conductor_cli/cli.py`",
        "`pg_restore --dbname conductor ~/.conductor/backups/x.dump`",
        "```bash\ncond up   # cond frobnicate in a comment is prose\n```\n",
    ),
)
def test_real_commands_and_paths_are_not_reported(snippet: str) -> None:
    assert _unknown_commands(snippet) == []


def test_a_missing_park_reason_is_reported() -> None:
    text = "\n".join(
        line for line in _text(_RUNBOOK).splitlines() if not line.startswith("| `watchdog_timeout`")
    )
    assert "watchdog_timeout" in _missing_reasons(text)


def test_a_wrong_group_is_reported() -> None:
    text = _text(_RUNBOOK).replace("| `no_new_commit` | R-EXIT |", "| `no_new_commit` | R-REVIEW |")
    assert any(item.startswith("no_new_commit:") for item in _wrong_groups(text))


def test_a_missing_readme_link_is_reported() -> None:
    assert not _README_LINK.search("See docs/RUNBOOK.md for operations.")
