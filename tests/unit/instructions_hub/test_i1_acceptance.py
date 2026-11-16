"""Instructions hub I1 acceptance: the rules the backlog N12 pins down.

Three things are pinned here:

- a dotted directory override names a directory, so the real ``AGENTS.md``
  under it is the file preview, status and diff report;
- every subcommand resolves the same default library when ``--library`` is
  omitted;
- the N12 acceptance itself: every loaded file is covered, a missing file is
  a mismatch, the budget is exact at 8192/8193 bytes per final file, and (since
  I2) the help text advertises apply and rollback.
"""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from src.conductor_cli.cli import main
from src.instructions_hub.core import HubError
from src.instructions_hub.render import render
from src.instructions_hub.store import diff_one, library_root, status
from src.instructions_hub.targets import SIZE_BUDGET

from .conftest import FRONT_MATTER_BYTES, HEADER_BYTES


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


def _filler(payload_bytes: int) -> str:
    """A fragment whose rendered payload is exactly ``payload_bytes`` long."""
    head = "# Big\n\n"
    body = head + "x" * (payload_bytes - len(head) - 1)
    assert len(body) + 1 == payload_bytes
    return body + "\n"


# --- a dotted directory override names a directory -------------------------


def test_a_dotted_override_names_the_real_agents_file(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # KIMI_CODE_HOME=<home>/kimi.v1 and DSH_HOME=<home>/dsh.v1 are
    # directories, not files. The CLI reads <override>/AGENTS.md from them.
    monkeypatch.setenv("KIMI_CODE_HOME", str(home / "kimi.v1"))
    monkeypatch.setenv("DSH_HOME", str(home / "dsh.v1"))
    kimi = render("operator", "kimi", home=home)
    dsh = render("worker", "dsh", home=home)
    assert kimi.files[1].path == (home / "kimi.v1" / "AGENTS.md").resolve()
    assert dsh.files[1].path == (home / "dsh.v1" / "AGENTS.md").resolve()
    # The shared file stays where it was; the override moves only the CLI file.
    assert kimi.files[0].path == (home / ".agents" / "AGENTS.md").resolve()
    assert dsh.files[0].path == (home / ".agents" / "AGENTS.md").resolve()


def test_the_real_agents_file_under_a_dotted_override_is_found(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The file that really exists under the override is the one status and
    # diff report, and the shared file is reported beside it.
    monkeypatch.setenv("KIMI_CODE_HOME", str(home / "kimi.v1"))
    shared, cli = render("operator", "kimi", home=home).files
    for planned in (shared, cli):
        planned.path.parent.mkdir(parents=True, exist_ok=True)
        planned.path.write_text(planned.body, encoding="utf-8")
    rows = {row["target"]: row for row in status(home, profile="operator")["targets"]}
    kimi = rows["kimi"]
    assert [planned["role"] for planned in kimi["files"]] == ["shared", "cli"]
    assert kimi["files"][1]["path"] == str(cli.path)
    assert kimi["files"][1]["exists"] is True
    assert kimi["files"][1]["matches"] is True
    assert kimi["matches"] is True
    differed = diff_one(home, "operator", "kimi")
    assert differed["files"][1]["path"] == str(cli.path)
    assert differed["files"][1]["matches"] is True


def test_a_dotted_override_reaches_the_cli_commands(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same override is visible through the CLI: preview, status and diff
    # all name <override>/AGENTS.md.
    monkeypatch.setenv("KIMI_CODE_HOME", str(home / "kimi.v1"))
    monkeypatch.setenv("DSH_HOME", str(home / "dsh.v1"))
    expected_kimi = str((home / "kimi.v1" / "AGENTS.md").resolve())
    expected_dsh = str((home / "dsh.v1" / "AGENTS.md").resolve())

    code, preview = run_cli(
        ["instructions", "render", "--home", str(home), "--target", "kimi"]
    )
    assert code == 0
    assert preview["files"][1]["path"] == expected_kimi

    code, reported = run_cli(
        ["instructions", "status", "--home", str(home), "--target", "kimi"]
    )
    assert code == 0
    assert reported["targets"][0]["files"][1]["path"] == expected_kimi

    code, differed = run_cli(
        ["instructions", "diff", "--home", str(home), "--target", "dsh"]
    )
    assert code == 0
    assert differed["files"][1]["path"] == expected_dsh


def test_a_dotted_override_names_one_home_spelling(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shared file and the CLI file of one target name the same HOME, so
    # the two paths can be compared with each other.
    monkeypatch.setenv("KIMI_CODE_HOME", str(home / "kimi.v1"))
    planned = render("operator", "kimi", home=home).files
    assert {item.path.parent.parent for item in planned} == {home.resolve()}


def test_a_symlinked_home_names_one_home_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The HOME may reach the hub through a symlink. The default path and the
    # override path must still name the same directory, or the two files of
    # one target look like they live in different homes.
    real = (tmp_path / "real").resolve()
    (real / "home").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real / "home")
    monkeypatch.setenv("KIMI_CODE_HOME", str(link / "kimi.v1"))
    planned = render("operator", "kimi", home=link).files
    assert {item.path.parent.parent for item in planned} == {(real / "home").resolve()}
    # The override still resolves to a directory that holds the real file.
    assert planned[1].path == (real / "home" / "kimi.v1" / "AGENTS.md").resolve()


# --- one default library for every subcommand ------------------------------


def test_the_default_library_is_the_one_under_the_home(home: Path) -> None:
    assert library_root(home) == home.resolve() / ".conductor" / "instructions"


def test_every_subcommand_resolves_the_same_default_library(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With --library omitted, list, status, render and diff all read
    # <home>/.conductor/instructions. A command that read the bundled
    # fragments instead would describe different content for one HOME.
    library = home / ".conductor" / "instructions"
    (library / "fragments").mkdir(parents=True)
    (library / "profiles").mkdir(parents=True)
    (library / "fragments" / "home-local.md").write_text(
        "# Home local\n\nOnly this HOME knows this line.\n", encoding="utf-8"
    )
    (library / "profiles" / "op.toml").write_text(
        'fragments = ["home-local"]\n', encoding="utf-8"
    )

    code, listed = run_cli(["instructions", "list", "--home", str(home)])
    assert code == 0
    assert "home-local" in listed["fragments"]
    assert "op" in listed["profiles"]

    code, preview = run_cli(
        [
            "instructions",
            "render",
            "--home",
            str(home),
            "--target",
            "zcode",
            "--profile",
            "op",
        ]
    )
    assert code == 0
    assert "Only this HOME knows this line." in preview["body"]

    code, reported = run_cli(
        [
            "instructions",
            "status",
            "--home",
            str(home),
            "--target",
            "zcode",
            "--profile",
            "op",
        ]
    )
    assert code == 0
    assert reported["targets"][0]["files"][0]["digest"] == preview["digest"]

    code, differed = run_cli(
        [
            "instructions",
            "diff",
            "--home",
            str(home),
            "--target",
            "zcode",
            "--profile",
            "op",
        ]
    )
    assert code == 0
    assert differed["digest"] == preview["digest"]
    assert differed["rendered"] == preview["body"]


def test_an_explicit_library_wins_over_the_home_local_one(
    home: Path, make_library
) -> None:
    # --library still names the library, for every command that has one.
    library = make_library(
        {"elsewhere": "# Elsewhere\n\nThis library is not the HOME one.\n"},
        {"op": {"fragments": ["elsewhere"]}},
    )
    code, preview = run_cli(
        [
            "instructions",
            "render",
            "--home",
            str(home),
            "--target",
            "zcode",
            "--profile",
            "op",
            "--library",
            str(library),
        ]
    )
    assert code == 0
    assert "This library is not the HOME one." in preview["body"]


def test_list_needs_a_home_to_resolve_its_library(home: Path) -> None:
    # list resolves <home>/.conductor/instructions like the others, so it
    # needs the HOME that library lives under.
    code, payload = run_cli(["instructions", "list", "--home", str(home)])
    assert code == 0
    assert payload["fragments"]


# --- N12 acceptance --------------------------------------------------------


def test_every_loaded_file_is_covered_by_all_three_commands(
    home: Path,
) -> None:
    # Preview, status and diff cover every file a target loads: the shared
    # AGENTS.md, the CLI's own file, and the body the Claude stub names.
    for profile in ("operator", "worker"):
        rows = {row["target"]: row for row in status(home, profile=profile)["targets"]}
        for target in ("claude", "kimi", "dsh", "zcode"):
            planned = render(profile, target, home=home).files
            roles = [item.role for item in planned]
            paths = [str(item.path) for item in planned]
            assert len(planned) >= 1
            assert [item["role"] for item in rows[target]["files"]] == roles
            assert [item["path"] for item in rows[target]["files"]] == paths
            differed = diff_one(home, profile, target)
            assert [item["role"] for item in differed["files"]] == roles
            assert [item["path"] for item in differed["files"]] == paths


def test_a_missing_file_is_reported_as_a_mismatch(home: Path) -> None:
    # A file that is not on disk is a mismatch, not a silent skip.
    rows = {row["target"]: row for row in status(home)["targets"]}
    for row in rows.values():
        assert row["files"]
        for planned in row["files"]:
            assert planned["exists"] is False
            assert planned["matches"] is False
        assert row["matches"] is False
    differed = diff_one(home, "operator", "dsh")
    assert differed["same"] is False
    assert differed["files"][1]["current"] is None


def test_the_budget_boundary_is_8192_bytes_per_final_file(
    home: Path, make_library
) -> None:
    # 8192 bytes passes, 8193 does not, counted on the file on disk.
    at_budget = make_library(
        {"big": _filler(SIZE_BUDGET - HEADER_BYTES)},
        {"op": {"fragments": ["big"]}},
        name="at-budget",
    )
    over_budget = make_library(
        {"big": _filler(SIZE_BUDGET - HEADER_BYTES + 1)},
        {"op": {"fragments": ["big"]}},
        name="over-budget",
    )
    planned = render("op", "zcode", home=home, library=at_budget).files
    assert planned[0].bytes == SIZE_BUDGET
    with pytest.raises(HubError, match=f"budget {SIZE_BUDGET}"):
        render("op", "zcode", home=home, library=over_budget)


def test_the_budget_counts_the_wrapper_of_the_final_file(
    home: Path, make_library
) -> None:
    # Cursor reads front matter before the body, so that wrapper is part of
    # the file the budget applies to.
    library = make_library(
        {"big": _filler(SIZE_BUDGET - HEADER_BYTES)},
        {"op": {"fragments": ["big"]}},
    )
    assert render("op", "zcode", home=home, library=library).files[0].bytes == SIZE_BUDGET
    with pytest.raises(HubError, match=r"\.cursor/rules/conductor\.mdc"):
        render("op", "cursor-agent", home=home, library=library)
    # The same managed document fits Cursor once the front matter is paid
    # for out of the payload.
    cursor_library = make_library(
        {"big": _filler(SIZE_BUDGET - HEADER_BYTES - FRONT_MATTER_BYTES)},
        {"op": {"fragments": ["big"]}},
        name="cursor",
    )
    assert render("op", "cursor-agent", home=home, library=cursor_library).files[
        0
    ].bytes == SIZE_BUDGET


def test_the_help_text_advertises_apply_and_rollback_since_i2() -> None:
    stdout = io.StringIO()
    with pytest.raises(SystemExit) as caught:
        with contextlib.redirect_stdout(stdout):
            main(["instructions", "--help"])
    assert caught.value.code == 0
    text = stdout.getvalue()
    assert "list" in text and "status" in text and "render" in text and "diff" in text
    # I2 enabled them; the I1 refusal text is gone.
    assert "apply" in text
    assert "rollback" in text
    assert "preview-only" not in text
