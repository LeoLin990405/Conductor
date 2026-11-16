"""G2 counts only lint findings new since the line's base commit (2026-11-13).

R7 line-3 parked as focused_tests_exhausted: its acceptance ran ``ruff check``
on a file that already had 105 findings on main, so no author could pass G2.
A recognised ``ruff check`` that fails is compared with the same command at
the base commit; only new findings fail it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Sequence

import pytest

from src.tools.auto_dispatch.check_modes import SCOPE_CHECK_ENV, WORKTREE_CHECK_ENV
from src.tools.auto_dispatch.gates import (
    FOCUSED_TESTS_FAILED,
    FOCUSED_TESTS_PASSED,
    G2_LINT_BASELINE_ENV,
    CommandResult,
    GateRunner,
    GateSettings,
    g2_failure_note,
    lint_baseline_enabled,
    new_lint_findings,
    parse_ruff_findings,
    ruff_check_index,
    ruff_json_argv,
)
from src.tools.auto_dispatch.types import Dispatch, Line

DEBT = "x = 1  # BAD\n" * 3

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(G2_LINT_BASELINE_ENV, raising=False)
    # These tests pin the lint baseline's own calls; N27's base re-run of a
    # non-ruff failure is tested in test_n27_preexisting_failure.py.
    monkeypatch.setenv("CONDUCTOR_DISPATCH_G2_PREEXISTING_MODE", "off")
    monkeypatch.setenv(WORKTREE_CHECK_ENV, "off")
    monkeypatch.setenv(SCOPE_CHECK_ENV, "off")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class _Repo:
    def __init__(self, tmp_path: Path, base: dict[str, str], head: dict[str, str]) -> None:
        self.root = tmp_path / "repo"
        self.root.mkdir()
        _git(self.root, "init", "-q", "-b", "main")
        _git(self.root, "config", "user.name", "Dispatch Test")
        _git(self.root, "config", "user.email", "dispatch@example.invalid")
        self._write(base)
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "base")
        self.base = _git(self.root, "rev-parse", "HEAD")
        self.branch = "feature/auto-dispatch-d1/line-line-0"
        _git(self.root, "checkout", "-q", "-b", self.branch)
        self._write(head)
        _git(self.root, "add", ".")
        _git(self.root, "commit", "-q", "-m", "the line's work")
        _git(self.root, "checkout", "-q", "main")

    def _write(self, files: dict[str, str]) -> None:
        for name, content in files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


class _FakeRuff:
    """Behaves like ``ruff check <paths>``: one finding per line holding BAD.

    Exit 1 with findings, 0 without, 2 for a missing path (as ruff does).
    ``--output-format json`` prints ruff's JSON, after a stderr-style warning.
    Anything that is not ruff exits ``other_exit``.
    """

    def __init__(self, other_exit: int = 0) -> None:
        self.calls: list[tuple[list[str], Path]] = []
        self._other_exit = other_exit

    async def run(self, argv, *, cwd, timeout, log_path):
        self.calls.append((list(argv), Path(cwd)))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if "check" not in argv:
            log_path.write_text("not ruff\n", encoding="utf-8")
            return CommandResult(exit_code=self._other_exit, duration_ms=1)
        check = argv.index("check")
        paths, skip = [], False
        for token in argv[check + 1 :]:
            if skip:
                skip = False
            elif token == "--output-format":
                skip = True
            elif not token.startswith("-"):
                paths.append(token)
        findings = []
        for rel in paths:
            target = Path(cwd) / rel
            if not target.exists():
                log_path.write_text(f"error: {rel}: No such file\n", encoding="utf-8")
                return CommandResult(exit_code=2, duration_ms=1)
            for number, text in enumerate(target.read_text().splitlines(), 1):
                if "BAD" in text:
                    findings.append(
                        {
                            "filename": str(target.resolve()),
                            "code": "E501",
                            "message": "Line too long",
                            "location": {"row": number, "column": 1},
                        }
                    )
        if "--output-format" in argv:
            log_path.write_text(
                "warning: a stderr line\n" + json.dumps(findings, indent=2) + "\n",
                encoding="utf-8",
            )
        else:
            log_path.write_text(f"Found {len(findings)} errors.\n", encoding="utf-8")
        return CommandResult(exit_code=1 if findings else 0, duration_ms=1)


def _run(
    tmp_path: Path,
    repo: _Repo,
    commands: _FakeRuff,
    acceptance: Sequence[str],
    *,
    base_commit: str | None = None,
):
    settings = GateSettings(
        runs_dir=tmp_path / "state" / "runs",
        repo_root=repo.root,
        worktrees_dir=tmp_path / "state" / "worktrees",
    )
    gate = GateRunner(settings, commands=commands)
    line = Line(
        id="line-0",
        name="lint line",
        task="Implement the line",
        acceptance=tuple(acceptance),
        branch=repo.branch,
        base_commit=repo.base if base_commit is None else base_commit,
    )
    dispatch = Dispatch(id="d1", goal="goal", user_id="u", created_at="now", lines=[line])
    return gate, dispatch, line, settings


def _cell(gate: GateRunner, dispatch: Dispatch, line: Line) -> dict:
    return gate._line(dispatch, line.id)["gates"]["G2"]


async def test_existing_lint_debt_alone_passes_g2(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, {"shell.py": DEBT}, {"shell.py": DEBT + "y = 2\n"})
    commands = _FakeRuff()
    gate, dispatch, line, settings = _run(
        tmp_path, repo, commands, ["cmd: uv run ruff check shell.py"]
    )

    step = await gate.run_g2(dispatch, line)

    assert step.passed, step
    assert step.reason == FOCUSED_TESTS_PASSED
    record = _cell(gate, dispatch, line)["commands"][0]
    assert record["exit_code"] == 1
    baseline = record["lint_baseline"]
    assert baseline["passed"] is True
    assert (baseline["head_findings"], baseline["base_findings"], baseline["new_findings"]) == (
        3,
        3,
        0,
    )
    # Plain run, JSON head run, JSON base run (in a checkout of the base).
    assert len(commands.calls) == 3
    assert "--output-format" in commands.calls[1][0]
    base_cwd = commands.calls[2][1]
    assert base_cwd != commands.calls[0][1]
    assert not base_cwd.exists(), "the base checkout is removed"
    assert (settings.runs_dir / baseline["base_log_ref"]).is_file()


async def test_a_new_finding_still_fails_and_the_note_says_how_many(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, {"shell.py": DEBT}, {"shell.py": DEBT + "z = 3  # BAD\n"})
    commands = _FakeRuff()
    gate, dispatch, line, settings = _run(
        tmp_path, repo, commands, ["cmd: ruff check --output-format concise shell.py"]
    )

    step = await gate.run_g2(dispatch, line)

    assert not step.passed
    assert step.reason == FOCUSED_TESTS_FAILED
    cell = _cell(gate, dispatch, line)
    baseline = cell["commands"][0]["lint_baseline"]
    assert baseline["passed"] is False and baseline["new_findings"] == 1
    note = g2_failure_note(cell, settings.runs_dir)
    assert "1 new (4 now, 3 on the base commit)" in note


async def test_a_path_the_line_added_counts_in_full(tmp_path: Path) -> None:
    repo = _Repo(
        tmp_path,
        {"shell.py": DEBT},
        {"shell.py": DEBT, "new_clean.py": "ok = 1\n", "new_bad.py": "x = 1  # BAD\n"},
    )
    clean = _FakeRuff()
    gate, dispatch, line, _settings = _run(
        tmp_path, repo, clean, ["cmd: ruff check shell.py new_clean.py"]
    )
    assert (await gate.run_g2(dispatch, line)).passed
    # The base run names only the path the base has.
    assert "new_clean.py" not in clean.calls[2][0]
    assert "shell.py" in clean.calls[2][0]

    bad = _FakeRuff()
    gate, dispatch, line, _settings = _run(
        tmp_path, repo, bad, ["cmd: ruff check new_bad.py"]
    )
    step = await gate.run_g2(dispatch, line)
    assert not step.passed
    baseline = _cell(gate, dispatch, line)["commands"][0]["lint_baseline"]
    assert baseline["base_findings"] == 0 and baseline["new_findings"] == 1
    # Every path was new, so the base was not linted at all.
    assert len(bad.calls) == 2


async def test_the_switch_restores_the_plain_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(G2_LINT_BASELINE_ENV, "0")
    repo = _Repo(tmp_path, {"shell.py": DEBT}, {"shell.py": DEBT + "y = 2\n"})
    commands = _FakeRuff()
    gate, dispatch, line, _settings = _run(
        tmp_path, repo, commands, ["cmd: ruff check shell.py"]
    )

    step = await gate.run_g2(dispatch, line)

    assert not step.passed
    assert len(commands.calls) == 1
    assert "lint_baseline" not in _cell(gate, dispatch, line)["commands"][0]


async def test_other_commands_and_unknown_bases_keep_their_failure(tmp_path: Path) -> None:
    repo = _Repo(tmp_path, {"shell.py": DEBT}, {"shell.py": DEBT + "y = 2\n"})
    other = _FakeRuff(other_exit=1)
    gate, dispatch, line, _settings = _run(tmp_path, repo, other, ["cmd: pytest -q"])
    assert not (await gate.run_g2(dispatch, line)).passed
    assert len(other.calls) == 1

    fixing = _FakeRuff()
    gate, dispatch, line, _settings = _run(
        tmp_path, repo, fixing, ["cmd: ruff check --fix shell.py"]
    )
    assert not (await gate.run_g2(dispatch, line)).passed
    assert len(fixing.calls) == 1

    no_base = _FakeRuff()
    gate, dispatch, line, _settings = _run(
        tmp_path, repo, no_base, ["cmd: ruff check shell.py"], base_commit=""
    )
    step = await gate.run_g2(dispatch, line)
    assert not step.passed
    baseline = _cell(gate, dispatch, line)["commands"][0]["lint_baseline"]
    assert baseline == {
        "passed": False,
        "head_findings": None,
        "base_findings": None,
        "new_findings": None,
        "reason": "no_base_commit",
    }


@pytest.mark.parametrize(
    ("command", "index"),
    [
        ("ruff check src", 1),
        ("/opt/bin/ruff check a.py", 1),
        ("uv run ruff check a.py", 3),
        ("uv run --with x --offline ruff check a.py", 6),
        ("uvx --offline ruff@0.15.14 check a.py", 3),
        ("uv tool run ruff check", 4),
        ("python3 -m ruff check a.py", 3),
        ("uv run python -m ruff check a.py", 5),
        ("env FOO=1 ruff check a.py", 3),
        ("ruff format a.py", None),
        ("ruff check --fix a.py", None),
        ("ruff check --output-file=x.json a.py", None),
        ("ruff check --statistics a.py", None),
        ("pytest -q tests/ruff", None),
        ("uv run pytest ruff", None),
    ],
)
def test_ruff_check_is_recognised_conservatively(command: str, index: int | None) -> None:
    assert ruff_check_index(command.split()) == index


def test_the_json_argv_replaces_the_output_format() -> None:
    argv = "ruff check --output-format concise --output-format=full a.py -- b.py".split()
    assert ruff_json_argv(argv, 1) == [
        "ruff", "check", "--output-format", "json", "a.py", "--", "b.py"
    ]


def test_findings_compare_as_a_multiset(tmp_path: Path) -> None:
    text = "warning: noise [x]\n" + json.dumps(
        [
            {"filename": str(tmp_path / "a.py"), "code": "E501", "message": "m"},
            {"filename": "a.py", "code": None, "message": "SyntaxError"},
        ]
    )
    findings = parse_ruff_findings(text, tmp_path)
    assert findings == [("a.py", "E501", "m"), ("a.py", "", "SyntaxError")]
    moved = json.dumps(
        [{"filename": "a.py", "code": "F811", "message": "Redefinition from line 12 (105 > 100)"}]
    )
    assert parse_ruff_findings(moved, tmp_path) == [
        ("a.py", "F811", "Redefinition from line # (# > #)")
    ]
    assert parse_ruff_findings("Found 3 errors.\n", tmp_path) is None
    one = ("a.py", "E501", "m")
    assert new_lint_findings([one, one], [one]) == 1
    assert new_lint_findings([one], [one, one]) == 0


def test_the_fix_note_skips_a_ruff_check_the_baseline_passed(tmp_path: Path) -> None:
    cell = {
        "result": "fail",
        "reason": "g2_worktree_dirty",
        "commands": [
            {"safe_command": "pytest -q", "exit_code": 0, "log_ref": None},
            {
                "safe_command": "ruff check shell.py",
                "exit_code": 1,
                "log_ref": None,
                "lint_baseline": {"passed": True, "new_findings": 0},
            },
        ],
    }
    note = g2_failure_note(cell, tmp_path)
    assert "ruff check" not in note


def test_the_switch_is_on_by_default() -> None:
    assert lint_baseline_enabled({})
    assert lint_baseline_enabled({G2_LINT_BASELINE_ENV: "1"})
    assert not lint_baseline_enabled({G2_LINT_BASELINE_ENV: "0"})
