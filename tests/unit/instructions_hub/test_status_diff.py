"""Instructions hub: status and diff cover every file a target loads."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.instructions_hub.core import operator_home
from src.instructions_hub.render import render
from src.instructions_hub.store import diff_one, status
from src.instructions_hub.targets import TARGETS


def _rows(result: dict) -> dict[str, dict]:
    return {row["target"]: row for row in result["targets"]}


def test_status_covers_every_file_of_every_target(home: Path) -> None:
    result = status(home, profile="worker")
    rows = _rows(result)
    assert set(rows) == {target.name for target in TARGETS}
    dsh = rows["dsh"]
    assert [planned["role"] for planned in dsh["files"]] == ["shared", "cli"]
    assert dsh["files"][0]["path"].endswith(".agents/AGENTS.md")
    assert dsh["files"][1]["path"].endswith(".dsh/AGENTS.md")
    kimi = rows["kimi"]
    assert [planned["role"] for planned in kimi["files"]] == ["shared", "cli"]
    assert kimi["files"][0]["path"].endswith(".agents/AGENTS.md")
    assert kimi["files"][1]["path"].endswith(".kimi-code/AGENTS.md")
    claude = rows["claude"]
    assert [planned["role"] for planned in claude["files"]] == ["stub", "rendered"]
    assert claude["files"][0]["path"].endswith(".claude/CLAUDE.md")
    assert claude["files"][1]["path"].endswith(
        ".conductor/instructions/rendered/worker.md"
    )
    for row in rows.values():
        assert row["files"]
        for planned in row["files"]:
            assert planned["matches"] is False
            assert planned["exists"] is False


def test_a_missing_file_is_a_mismatch(home: Path) -> None:
    shared, cli = render("worker", "dsh", home=home).files
    shared.path.parent.mkdir(parents=True, exist_ok=True)
    shared.path.write_text(shared.body, encoding="utf-8")
    rows = _rows(status(home, profile="worker"))
    dsh = rows["dsh"]
    assert dsh["files"][0]["exists"] is True
    assert dsh["files"][0]["matches"] is True
    assert dsh["files"][1]["exists"] is False
    assert dsh["files"][1]["matches"] is False
    assert dsh["matches"] is False


def test_operator_dsh_reports_a_missing_cli_file(home: Path) -> None:
    # Empty extras must not hide .dsh/AGENTS.md from status/diff.
    shared, cli = render("operator", "dsh", home=home).files
    shared.path.parent.mkdir(parents=True, exist_ok=True)
    shared.path.write_text(shared.body, encoding="utf-8")
    rows = _rows(status(home, profile="operator"))
    dsh = rows["dsh"]
    assert [planned["role"] for planned in dsh["files"]] == ["shared", "cli"]
    assert dsh["files"][0]["matches"] is True
    assert dsh["files"][1]["exists"] is False
    assert dsh["files"][1]["matches"] is False
    assert dsh["matches"] is False
    result = diff_one(home, "operator", "dsh")
    assert result["same"] is False
    assert result["files"][1]["current"] is None
    assert result["files"][1]["rendered"] == cli.body


def test_kimi_missing_cli_file_is_a_mismatch(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kimi_home = home / "kimi-home"
    monkeypatch.setenv("KIMI_CODE_HOME", str(kimi_home))
    shared, cli = render("operator", "kimi", home=home).files
    shared.path.parent.mkdir(parents=True, exist_ok=True)
    shared.path.write_text(shared.body, encoding="utf-8")
    rows = _rows(status(home, profile="operator"))
    kimi = rows["kimi"]
    assert [planned["role"] for planned in kimi["files"]] == ["shared", "cli"]
    assert kimi["files"][0]["path"].endswith(".agents/AGENTS.md")
    assert kimi["files"][1]["path"] == str(cli.path)
    assert kimi["files"][0]["matches"] is True
    assert kimi["files"][1]["exists"] is False
    assert kimi["files"][1]["matches"] is False
    assert kimi["matches"] is False


def test_a_target_matches_only_when_every_file_matches(home: Path) -> None:
    shared, cli = render("worker", "dsh", home=home).files
    for planned in (shared, cli):
        planned.path.parent.mkdir(parents=True, exist_ok=True)
        planned.path.write_text(planned.body, encoding="utf-8")
    rows = _rows(status(home, profile="worker"))
    dsh = rows["dsh"]
    assert all(planned["matches"] for planned in dsh["files"])
    assert dsh["matches"] is True
    assert dsh["files"][0]["managed"] is True


def test_diff_covers_every_file(home: Path) -> None:
    shared, cli = render("worker", "dsh", home=home).files
    shared.path.parent.mkdir(parents=True, exist_ok=True)
    shared.path.write_text(shared.body, encoding="utf-8")
    result = diff_one(home, "worker", "dsh")
    assert result["same"] is False
    files = {planned["role"]: planned for planned in result["files"]}
    assert files["shared"]["matches"] is True
    assert files["cli"]["matches"] is False
    assert files["cli"]["current"] is None
    assert files["cli"]["rendered"] == cli.body


def test_diff_reports_the_body_a_claude_stub_names(home: Path) -> None:
    result = diff_one(home, "operator", "claude")
    files = {planned["role"]: planned for planned in result["files"]}
    assert files["stub"]["path"].endswith(".claude/CLAUDE.md")
    assert files["rendered"]["path"].endswith(
        ".conductor/instructions/rendered/operator.md"
    )
    assert "managed_by=conductor" in files["rendered"]["rendered"]


def test_status_and_diff_do_not_write(home: Path) -> None:
    status(home)
    status(home, profile="worker")
    diff_one(home, "operator", "zcode")
    assert list(home.rglob("*")) == []


def test_one_broken_target_does_not_hide_the_others(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROK_HOME", str(operator_home() / ".grok"))
    rows = _rows(status(home))
    assert "inside the isolated HOME" in rows["grok"]["error"]
    assert rows["grok"]["matches"] is False
    assert rows["zcode"]["files"]
    assert rows["zcode"]["matches"] is False


def test_the_three_commands_list_the_same_files_for_every_target(
    home: Path,
) -> None:
    # Preview, status and diff all plan the same files for a target. A target
    # that loads two files must not lose one of them in one of the three.
    for profile in ("operator", "worker"):
        rows = _rows(status(home, profile=profile))
        for target in TARGETS:
            planned = render(profile, target.name, home=home).files
            roles = [item.role for item in planned]
            paths = [str(item.path) for item in planned]
            row = rows[target.name]
            assert [item["role"] for item in row["files"]] == roles, target.name
            assert [item["path"] for item in row["files"]] == paths, target.name
            differed = diff_one(home, profile, target.name)
            assert [item["role"] for item in differed["files"]] == roles, target.name
            assert [item["path"] for item in differed["files"]] == paths, target.name


def test_the_summary_names_the_file_that_carries_the_instructions(
    home: Path,
) -> None:
    rows = _rows(status(home, profile="worker"))
    for target in TARGETS:
        content = render("worker", target.name, home=home).content
        assert rows[target.name]["path"] == str(content.path), target.name
        differed = diff_one(home, "worker", target.name)
        assert differed["path"] == str(content.path), target.name
        assert differed["rendered"] == content.body, target.name


def test_a_dotted_override_is_reported_by_status_and_diff(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The override names a directory, so the CLI file is
    # <override>/AGENTS.md. Preview, status and diff must all name it.
    monkeypatch.setenv("DSH_HOME", str(home / "dsh.v1"))
    planned = render("worker", "dsh", home=home).files
    paths = [str(item.path) for item in planned]
    assert paths[1].endswith("dsh.v1/AGENTS.md")
    rows = _rows(status(home, profile="worker"))
    dsh = rows["dsh"]
    assert [item["path"] for item in dsh["files"]] == paths
    assert dsh["files"][1]["exists"] is False
    assert dsh["files"][1]["matches"] is False
    differed = diff_one(home, "worker", "dsh")
    assert [item["path"] for item in differed["files"]] == paths


def test_an_unknown_profile_is_refused(home: Path) -> None:
    from src.instructions_hub.core import HubError

    with pytest.raises(HubError, match="unknown profile"):
        status(home, profile="nope")
