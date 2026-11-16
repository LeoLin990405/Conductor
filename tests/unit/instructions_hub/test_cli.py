"""``conductor instructions`` — the help text and every subcommand."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path

import pytest

from src.conductor_cli.cli import build_parser, main
from src.conductor_cli.output import build_completion

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


def test_the_help_text_advertises_apply_rollback_and_snapshots() -> None:
    stdout = io.StringIO()
    with pytest.raises(SystemExit) as caught:
        with contextlib.redirect_stdout(stdout):
            main(["instructions", "--help"])
    assert caught.value.code == 0
    text = stdout.getvalue()
    for name in ("list", "status", "render", "diff", "apply", "rollback", "snapshots"):
        assert name in text, name
    assert "preview-only" not in text


def test_an_unknown_command_lists_the_real_ones() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with pytest.raises(SystemExit) as caught:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            main(["instructions", "adopt"])
    assert caught.value.code == 2
    text = stdout.getvalue() + stderr.getvalue()
    assert "adopt" in text
    assert "apply" in text and "rollback" in text


def _offered_for_instructions(shell: str, script: str) -> str:
    """The subcommand names the completion script offers for ``instructions``."""
    lines = script.splitlines()
    if shell == "bash":
        index = lines.index("        instructions)")
        return lines[index + 1]
    if shell == "zsh":
        return next(line for line in lines if "instructions subcommand" in line)
    return " ".join(line for line in lines if "subcommand_from instructions" in line)


def test_the_shell_completion_offers_every_command() -> None:
    for shell in ("bash", "zsh", "fish"):
        script = build_completion(shell, build_parser())
        offered = _offered_for_instructions(shell, script)
        for name in ("list", "render", "status", "diff", "apply", "rollback", "snapshots"):
            assert name in offered, (shell, name)


def test_apply_is_a_dry_run_by_default(home: Path) -> None:
    code, payload = run_cli(["instructions", "apply", "--home", str(home)])
    assert code == 0
    assert payload["dry_run"] is True
    assert payload["changes"] > 0
    assert list(home.rglob("*")) == []


def test_apply_then_rollback_round_trip(home: Path) -> None:
    code, payload = run_cli(["instructions", "apply", "--home", str(home), "--apply"])
    assert code == 0
    assert payload["snapshot_id"]
    assert (home / ".zcode" / "AGENTS.md").exists()
    code, listed = run_cli(["instructions", "snapshots", "--home", str(home)])
    assert code == 0
    assert [item["id"] for item in listed["snapshots"]] == [payload["snapshot_id"]]
    code, rolled = run_cli(["instructions", "rollback", "--home", str(home)])
    assert code == 0
    assert rolled["snapshot_id"] == payload["snapshot_id"]
    assert not (home / ".zcode" / "AGENTS.md").exists()


def test_apply_with_a_refusal_fails_and_writes_nothing(home: Path) -> None:
    target = home / ".grok" / "AGENTS.md"
    target.parent.mkdir()
    target.write_text("hand written\n", encoding="utf-8")
    code, payload = run_cli(["instructions", "apply", "--home", str(home)])
    assert code == 0
    assert payload["refused"] == 1
    code, payload = run_cli(["instructions", "apply", "--home", str(home), "--apply"])
    assert code == 1
    assert "unmanaged" in str(payload)
    assert not (home / ".zcode" / "AGENTS.md").exists()
    assert target.read_text(encoding="utf-8") == "hand written\n"


def test_writing_the_operator_home_needs_yes(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    import instructions_hub.apply as hub_apply

    called: list[object] = []
    monkeypatch.setattr(hub_apply, "apply", lambda *a, **k: called.append(a) or {})
    code, payload = run_cli(["instructions", "apply", "--operator-home", "--apply"])
    assert code == 1
    assert "--yes" in str(payload)
    assert called == []


def test_home_and_operator_home_are_exclusive(home: Path) -> None:
    stderr = io.StringIO()
    with pytest.raises(SystemExit) as caught:
        with contextlib.redirect_stderr(stderr):
            main(["instructions", "status", "--home", str(home), "--operator-home"])
    assert caught.value.code == 2
    with pytest.raises(SystemExit) as caught:
        with contextlib.redirect_stderr(stderr):
            main(["instructions", "status"])
    assert caught.value.code == 2


def test_adopt_is_not_a_subcommand() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with pytest.raises(SystemExit) as caught:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            main(["instructions", "adopt"])
    assert caught.value.code == 2


def test_list_names_the_fragments_the_profiles_and_the_targets(home: Path) -> None:
    code, payload = run_cli(["instructions", "list", "--home", str(home)])
    assert code == 0
    assert "worker-safety" in payload["fragments"]
    assert len(payload["targets"]) == 10
    assert payload["profiles"]["worker"]["extra"] == {"dsh": ["course-notes"]}


def test_render_previews_every_file_of_the_target(home: Path) -> None:
    code, payload = run_cli(
        ["instructions", "render", "--home", str(home), "--target", "claude"]
    )
    assert code == 0
    files = payload["files"]
    assert [planned["role"] for planned in files] == ["stub", "rendered"]
    assert files[0]["path"].endswith(".claude/CLAUDE.md")
    assert files[1]["path"].endswith(".conductor/instructions/rendered/operator.md")
    assert files[0]["body"] == "@~/.conductor/instructions/rendered/operator.md\n"
    assert "managed_by=conductor" in files[1]["body"]


def test_status_reports_every_file_of_every_target(home: Path) -> None:
    code, payload = run_cli(
        ["instructions", "status", "--home", str(home), "--profile", "worker"]
    )
    assert code == 0
    rows = {row["target"]: row for row in payload["targets"]}
    assert len(rows) == 10
    dsh = rows["dsh"]
    assert [planned["role"] for planned in dsh["files"]] == ["shared", "cli"]
    assert all(planned["matches"] is False for planned in dsh["files"])
    assert list(home.rglob("*")) == []


def test_diff_reports_every_file_of_the_target(home: Path) -> None:
    code, payload = run_cli(
        [
            "instructions",
            "diff",
            "--home",
            str(home),
            "--target",
            "dsh",
            "--profile",
            "worker",
        ]
    )
    assert code == 0
    assert payload["same"] is False
    assert [planned["role"] for planned in payload["files"]] == ["shared", "cli"]


def test_render_and_diff_describe_the_same_file(home: Path) -> None:
    # The summary of both commands names the file that carries the
    # instructions. For Claude that is the body the stub points at, not the
    # stub, so the two commands do not point at different files.
    code, rendered = run_cli(
        ["instructions", "render", "--home", str(home), "--target", "claude"]
    )
    assert code == 0
    code, differed = run_cli(
        ["instructions", "diff", "--home", str(home), "--target", "claude"]
    )
    assert code == 0
    body = next(
        planned for planned in rendered["files"] if planned["role"] == "rendered"
    )
    assert rendered["path"] == body["path"]
    assert differed["path"] == body["path"]
    assert differed["rendered"] == body["body"]
    assert differed["digest"] == rendered["digest"] == body["digest"]
    # The stub is still reported, and it is still the file the CLI reads.
    stub = next(planned for planned in rendered["files"] if planned["role"] == "stub")
    assert stub["path"].endswith(".claude/CLAUDE.md")
    assert stub["body"] == "@~/.conductor/instructions/rendered/operator.md\n"


def test_status_can_be_filtered_by_target(home: Path) -> None:
    # The plan gives every instructions command a --target. Without it, status
    # reports all ten targets; with it, only the named ones.
    code, payload = run_cli(["instructions", "status", "--home", str(home)])
    assert code == 0
    assert len(payload["targets"]) == 10

    code, payload = run_cli(
        ["instructions", "status", "--home", str(home), "--target", "dsh"]
    )
    assert code == 0
    assert [row["target"] for row in payload["targets"]] == ["dsh"]
    # The filtered row still covers every file the target loads.
    assert [planned["role"] for planned in payload["targets"][0]["files"]] == [
        "shared",
        "cli",
    ]

    code, payload = run_cli(
        [
            "instructions",
            "status",
            "--home",
            str(home),
            "--target",
            "dsh",
            "--target",
            "kimi",
        ]
    )
    assert code == 0
    assert [row["target"] for row in payload["targets"]] == ["dsh", "kimi"]


def test_status_refuses_an_unknown_target(home: Path) -> None:
    code, payload = run_cli(
        ["instructions", "status", "--home", str(home), "--target", "nope"]
    )
    assert code == 1
    assert "nope" in str(payload)


def test_the_preview_reads_the_home_local_library(home: Path) -> None:
    # status and diff read <home>/.conductor/instructions. The preview must
    # read the same library, not the bundled fragments, or the two describe
    # different content for one profile.
    library = home / ".conductor" / "instructions"
    (library / "fragments").mkdir(parents=True)
    (library / "profiles").mkdir(parents=True)
    (library / "fragments" / "home-local.md").write_text(
        "# Home local\n\nOnly this HOME knows this line.\n", encoding="utf-8"
    )
    (library / "profiles" / "op.toml").write_text(
        'fragments = ["home-local"]\n', encoding="utf-8"
    )
    code, rendered = run_cli(
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
    assert "Only this HOME knows this line." in rendered["body"]
    assert "Fleet memory" not in rendered["body"]

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
    row = reported["targets"][0]
    assert row["path"] == rendered["path"]
    assert row["files"][0]["digest"] == rendered["digest"]

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
    assert differed["rendered"] == rendered["body"]
    assert differed["digest"] == rendered["digest"]


def test_the_operator_home_is_refused() -> None:
    from src.instructions_hub.core import operator_home

    code, payload = run_cli(["instructions", "status", "--home", str(operator_home())])
    assert code == 1
    assert "refuses" in str(payload).lower() or "operator" in str(payload).lower()


def test_a_file_over_the_budget_is_refused(home: Path, make_library) -> None:
    library = make_library(
        {"big": "# Big\n\n" + "x" * 10000 + "\n"},
        {"op": {"fragments": ["big"]}},
    )
    code, payload = run_cli(
        [
            "instructions",
            "render",
            "--home",
            str(home),
            "--target",
            "zcode",
            "--library",
            str(library),
            "--profile",
            "op",
        ]
    )
    assert code == 1
    assert "budget" in str(payload).lower()
