"""The file a CLI reads is the file the hub reports.

An override variable stands in for the directory a CLI reads from, and it
replaces that whole directory prefix: the file name is the only part of
the path that survives. A variable that the PRD calls "config only" is
not an override at all. Cursor reads ``.cursor/rules/*.mdc`` and the PRD
calls ``CURSOR_CONFIG_DIR`` config only, so it moves the config and not
the rules. Treating it as an override made the hub report
``.cursor/conductor.mdc`` — a file Cursor never loads.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from src.conductor_cli.cli import main
from src.instructions_hub.render import render
from src.instructions_hub.store import diff_one, status
from src.instructions_hub.targets import (
    CLI_EXTRA_FILES,
    TARGETS,
    Target,
    get_target,
    target_path,
)


def run_cli(argv: list[str]) -> tuple[int, dict | str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    text = stdout.getvalue().strip()
    err = stderr.getvalue().strip()
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text or err


def _rule_path(home: Path) -> Path:
    """The rule file Cursor loads, spelled the way the hub spells it."""
    return (home / ".cursor" / "rules" / "conductor.mdc").resolve()


# --- a config only variable does not move the rule ---------------------------


def test_cursor_keeps_the_rule_path_when_the_config_dir_is_set(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # CURSOR_CONFIG_DIR names the config directory, not the rules directory.
    # The rule stays in .cursor/rules, so preview, status and diff all report
    # the file Cursor loads.
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(home / ".cursor"))
    expected = _rule_path(home)
    assert target_path(get_target("cursor-agent"), home) == expected
    assert render("operator", "cursor-agent", home=home).content.path == expected
    rows = {row["target"]: row for row in status(home)["targets"]}
    assert rows["cursor-agent"]["path"] == str(expected)
    assert diff_one(home, "operator", "cursor-agent")["path"] == str(expected)


def test_the_config_dir_does_not_move_the_rule_to_another_directory(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A config directory somewhere else is still only the config directory.
    # The rules stay where Cursor looks for them.
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(home / "cursor-config"))
    expected = _rule_path(home)
    assert target_path(get_target("cursor-agent"), home) == expected
    rows = {row["target"]: row for row in status(home)["targets"]}
    assert rows["cursor-agent"]["path"] == str(expected)


def test_the_rule_file_cursor_loads_is_the_one_status_and_diff_read(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reported file is not only spelled right, it is the file the hub
    # reads: the one under .cursor/rules is found, and the one the old
    # override produced is not reported at all.
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(home / ".cursor"))
    planned = render("operator", "cursor-agent", home=home).content
    planned.path.parent.mkdir(parents=True, exist_ok=True)
    planned.path.write_text(planned.body, encoding="utf-8")

    rows = {row["target"]: row for row in status(home)["targets"]}
    cursor = rows["cursor-agent"]
    assert cursor["path"] == str(planned.path)
    assert cursor["matches"] is True
    assert cursor["files"][0]["exists"] is True
    assert cursor["files"][0]["matches"] is True

    differed = diff_one(home, "operator", "cursor-agent")
    assert differed["same"] is True
    assert differed["current"] == planned.body
    assert str(home / ".cursor" / "conductor.mdc") not in str(differed)


def test_the_three_commands_report_the_same_rule_path(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same override is visible through the CLI: preview, status and diff
    # all name the rule under .cursor/rules.
    monkeypatch.setenv("CURSOR_CONFIG_DIR", str(home / ".cursor"))
    expected = str(_rule_path(home))

    code, preview = run_cli(
        ["instructions", "render", "--home", str(home), "--target", "cursor-agent"]
    )
    assert code == 0
    assert preview["path"] == expected
    assert preview["files"][0]["path"] == expected

    code, reported = run_cli(
        ["instructions", "status", "--home", str(home), "--target", "cursor-agent"]
    )
    assert code == 0
    assert reported["targets"][0]["path"] == expected
    assert reported["targets"][0]["files"][0]["path"] == expected

    code, differed = run_cli(
        ["instructions", "diff", "--home", str(home), "--target", "cursor-agent"]
    )
    assert code == 0
    assert differed["path"] == expected
    assert differed["files"][0]["path"] == expected


def test_the_other_targets_still_move_with_their_override(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only a config only variable is exempt. A real override still stands in
    # for the directory the file is read from.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude-config"))
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(home / "opencode-config"))
    assert target_path(get_target("claude"), home) == (
        home / "claude-config" / "CLAUDE.md"
    ).resolve()
    assert target_path(get_target("opencode"), home) == (
        home / "opencode-config" / "AGENTS.md"
    ).resolve()


# --- the shape of an override ------------------------------------------------


def test_every_override_keeps_the_file_name_of_its_default() -> None:
    # The override replaces the directory, so it may not rename the file.
    for target in TARGETS:
        if target.env:
            assert target.env_file == target.relative[-1], target.name
    for name, extra in CLI_EXTRA_FILES.items():
        if extra.env:
            assert extra.env_file == extra.relative[-1], name


def test_an_override_may_not_rename_the_file() -> None:
    # A target that points its override at another file would report a file
    # the CLI never reads, so the table refuses to be built that way.
    with pytest.raises(ValueError, match="renames the file"):
        Target("bad", "managed", (".x", "AGENTS.md"), env="X_DIR", env_file="OTHER.md")
