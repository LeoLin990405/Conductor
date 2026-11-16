"""G2/G5 helpers that do not need the state machine: Alembic, globs, evidence."""

from __future__ import annotations

import ast
import json
import subprocess
from pathlib import Path

import pytest

from src.server.services.autonomy.git_delivery import GitDeliveryError
from src.tools.auto_dispatch.approval import FileApprovalGate
from src.tools.auto_dispatch.execution import line_evidence
from src.tools.auto_dispatch.gates import (
    SCHEMA_VERSION,
    CommandResult,
    GateRunner,
    GateSettings,
    GitWorktreeManager,
    acceptance_commands,
    git_changed_paths,
    line_is_risk_bearing,
    AlembicUnparseable,
    parse_alembic_heads,
    path_matches,
    topological_lines,
)
import src.tools.auto_dispatch.gates as gates_module
from src.tools.auto_dispatch.protocols import DispatchError
from src.tools.auto_dispatch.types import Dispatch, Line, LineStatus


class _PassingGateCommands:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path]] = []

    async def run(self, argv, *, cwd, timeout, log_path):
        self.calls.append((list(argv), cwd))
        return CommandResult(exit_code=0, duration_ms=1)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(directory: Path, name: str, revision: str, down: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        f'revision = "{revision}"\ndown_revision = {down}\n',
        encoding="utf-8",
    )


def test_changed_paths_reports_unknown_when_isolated_git_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(*_args, **_kwargs):
        raise GitDeliveryError("isolated Git refusal")

    monkeypatch.setattr(gates_module, "run_isolated_git_readonly", refused)

    assert git_changed_paths(tmp_path, "a" * 40, "b" * 40) is None


def test_one_alembic_head_is_the_revision_nobody_points_at(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    _write(versions, "140_base.py", "140", "None")
    _write(versions, "141_dispatch.py", "141", '"140"')
    assert parse_alembic_heads(versions) == ["141"]


def test_a_tuple_down_revision_is_a_merge_parent(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    _write(versions, "a.py", "a", "None")
    _write(versions, "b.py", "b", "None")
    _write(versions, "c.py", "c", '("a", "b")')
    assert parse_alembic_heads(versions) == ["c"]


def test_two_unrelated_revisions_are_two_heads(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    _write(versions, "a.py", "a", "None")
    _write(versions, "b.py", "b", "None")
    assert parse_alembic_heads(versions) == ["a", "b"]


def test_a_missing_versions_directory_has_no_head(tmp_path: Path) -> None:
    assert parse_alembic_heads(tmp_path / "missing") == []


def test_an_undecodable_migration_fails_closed(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "141.py").write_text(
        'revision = "141"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (versions / "bad.py").write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(AlembicUnparseable):
        parse_alembic_heads(versions)


def test_a_syntax_error_does_not_hide_a_second_head(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "one.py").write_text(
        'revision = "one"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (versions / "bad.py").write_text('revision = "hidden-head"\n(', encoding="utf-8")
    with pytest.raises(AlembicUnparseable):
        parse_alembic_heads(versions)


def test_an_unreadable_migration_fails_closed(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "one.py").write_text(
        'revision = "one"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (versions / "bad.py").mkdir()
    with pytest.raises(AlembicUnparseable):
        parse_alembic_heads(versions)


def test_annotated_assignments_count(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "141.py").write_text(
        'revision: str = "141"\ndown_revision: str | None = "140"\n',
        encoding="utf-8",
    )
    (versions / "140.py").write_text(
        'revision: str = "140"\ndown_revision: str | None = None\n',
        encoding="utf-8",
    )
    assert parse_alembic_heads(versions) == ["141"]


def test_the_parser_does_not_import_the_module(tmp_path: Path) -> None:
    versions = tmp_path / "versions"
    versions.mkdir()
    (versions / "bad.py").write_text(
        'import alembic\nrevision = "9"\ndown_revision = None\n',
        encoding="utf-8",
    )
    assert parse_alembic_heads(versions) == ["9"]
    tree = ast.parse((versions / "bad.py").read_text(encoding="utf-8"))
    assert any(isinstance(node, ast.Import) for node in tree.body)


def test_risk_globs_match_the_documented_prefixes() -> None:
    assert path_matches("migrations/**", "migrations/versions/141_dispatch_gates.py")
    assert path_matches("src/server/database/**", "src/server/database/dispatch.py")
    assert path_matches("**/auth*", "src/server/auth_tokens.py")
    assert path_matches("**/credential*", "pkg/credential_store.py")
    assert path_matches("**/secret*", "secrets.py")
    assert path_matches("src/server/app/**", "src/server/app/health.py")
    assert not path_matches("src/server/app/**", "src/tools/auto_dispatch/gates.py")
    line = Line(
        id="line-0",
        name="api",
        task="add it",
        write_scope=("src/server/app/health.py",),
    )
    assert line_is_risk_bearing(line, ("src/server/app/**",))
    plain = Line(id="line-1", name="docs", task="write", write_scope=("docs/readme.md",))
    assert not line_is_risk_bearing(plain, ("src/server/app/**",))
    flagged = Line(id="line-2", name="docs", task="write", risk_bearing=True)
    assert line_is_risk_bearing(flagged, ())


def test_cmd_acceptance_is_split_without_a_shell() -> None:
    line = Line(
        id="line-0",
        name="api",
        task="add it",
        acceptance=("cmd: pytest -q tests/unit/server/app/test_health.py", "returns 200"),
    )
    assert acceptance_commands(line, None) == [
        ["pytest", "-q", "tests/unit/server/app/test_health.py"]
    ]
    prose = Line(id="line-1", name="docs", task="write", acceptance=("returns 200",))
    assert acceptance_commands(prose, None) == []
    assert acceptance_commands(prose, "pytest -q") == [["pytest", "-q"]]


def test_gate_worktrees_must_be_disjoint_from_run_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    runs_dir = tmp_path / "state" / "runs"
    for worktrees_dir in (runs_dir / "worktrees", runs_dir.parent):
        with pytest.raises(ValueError, match="must be disjoint"):
            GateRunner(
                GateSettings(
                    runs_dir=runs_dir,
                    repo_root=repo,
                    worktrees_dir=worktrees_dir,
                )
            )


@pytest.mark.asyncio
async def test_handoff_uses_the_revision_and_commands_g2_actually_tested(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Dispatch Test")
    _git(repo, "config", "user.email", "dispatch@example.invalid")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    branch = "feature/auto-dispatch-d1/line-line-0"
    _git(repo, "checkout", "-q", "-b", branch)
    (repo / "tested.txt").write_text("tested by G2\n", encoding="utf-8")
    _git(repo, "add", "tested.txt")
    _git(repo, "commit", "-q", "-m", "G2 tested change")
    tested_revision = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")

    settings = GateSettings(
        runs_dir=tmp_path / "state" / "runs",
        repo_root=repo,
        g2_command="pytest -q --tested-revision",
        worktrees_dir=tmp_path / "state" / "worktrees",
    )
    commands = _PassingGateCommands()
    gate = GateRunner(
        settings,
        commands=commands,
        worktrees=GitWorktreeManager(),
    )
    line = Line(
        id="line-0",
        name="test line",
        task="Implement the line",
        acceptance=("focused tests pass",),
        branch=branch,
        base_commit=base,
    )
    dispatch = Dispatch(id="d1", goal="goal", user_id="u", created_at="now", lines=[line])

    outcome = await gate.run_g2(dispatch, line)
    assert outcome.passed
    assert commands.calls[0][0] == ["pytest", "-q", "--tested-revision"]
    g2 = gate._line(dispatch, line.id)["gates"]["G2"]
    assert g2["revision"] == tested_revision
    assert g2["commands"][0]["safe_command"] == "pytest -q --tested-revision"
    assert len(g2["commands"][0]["safe_command_sha256"]) == 64
    assert "argv" not in g2["commands"][0]

    _git(repo, "checkout", "-q", branch)
    (repo / "later.txt").write_text("not tested by G2\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-q", "-m", "later line change")
    _git(repo, "checkout", "-q", "main")
    (repo / "root-later.txt").write_text("new repo HEAD\n", encoding="utf-8")
    _git(repo, "add", "root-later.txt")
    _git(repo, "commit", "-q", "-m", "advance repo HEAD")
    settings.g2_command = "pytest -q --current-setting"

    facts = gate.handoff_facts(dispatch, line, limitations="none")

    assert facts["branch"] == branch
    assert facts["commit"] == tested_revision
    assert facts["files"] == ("tested.txt",)
    assert facts["test_command"] == (
        "pytest -q --tested-revision [safe-display-sha256:"
        f"{g2['commands'][0]['safe_command_sha256']}]"
    )
    assert facts["results"] == "0:0"


def test_skipped_g2_has_an_explicit_handoff_test_command_fact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    gate = GateRunner(
        GateSettings(runs_dir=tmp_path / "runs", repo_root=repo)
    )
    line = Line(id="line-0", name="docs", task="write docs", branch="feature/docs")
    dispatch = Dispatch(id="d1", goal="goal", user_id="u", created_at="now", lines=[line])
    snapshot = gate.line_snapshot(dispatch, line)
    snapshot["gates"]["G2"] = {
        "result": "skipped",
        "reason": "g2_no_commands",
        "commands": [],
    }

    facts = gate.handoff_facts(dispatch, line, limitations="none")

    assert facts["test_command"] == (
        "No focused test command was run (G2 skipped: g2_no_commands)"
    )
    assert facts["results"] == "g2_no_commands"


def test_legacy_g2_command_record_does_not_claim_tests_were_skipped(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    gate = GateRunner(GateSettings(runs_dir=tmp_path / "runs", repo_root=repo))
    line = Line(id="line-0", name="docs", task="write docs", branch="feature/docs")
    dispatch = Dispatch(id="d1", goal="goal", user_id="u", created_at="now", lines=[line])
    snapshot = gate.line_snapshot(dispatch, line)
    snapshot["gates"]["G2"] = {
        "result": "passed",
        "revision": "a" * 40,
        "commands": [{"index": 0, "exit_code": 0, "log_ref": "g2.log"}],
    }

    facts = gate.handoff_facts(dispatch, line, limitations="none")

    assert facts["test_command"] == "Recorded G2 command text unavailable (legacy record)"
    assert facts["commit"] == "a" * 40


def test_g2_snapshot_and_handoff_redact_sensitive_flags_and_assignments(tmp_path: Path) -> None:
    from tools.auto_dispatch.gates import _safe_command_record

    secrets = ("TOKEN_SENTINEL_8f09", "API_KEY_SENTINEL_0e12")
    display, digest = _safe_command_record(
        [
            "pytest",
            "--token",
            secrets[0],
            "--api-key=" + secrets[1],
            "TOKEN=" + secrets[0],
            "API_KEY=" + secrets[1],
        ]
    )
    assert all(secret not in display for secret in secrets)
    assert "[REDACTED]" in display
    assert len(digest) == 64

    repo = tmp_path / "repo"
    repo.mkdir()
    gate = GateRunner(GateSettings(runs_dir=tmp_path / "runs", repo_root=repo))
    line = Line(id="line-0", name="docs", task="write docs", branch="feature/docs")
    dispatch = Dispatch(id="d1", goal="goal", user_id="u", created_at="now", lines=[line])
    snapshot = gate.line_snapshot(dispatch, line)
    snapshot["gates"]["G2"] = {
        "result": "passed",
        "revision": "a" * 40,
        "commands": [{
            "index": 0,
            "safe_command": display,
            "safe_command_sha256": digest,
            "exit_code": 0,
        }],
    }
    facts = gate.handoff_facts(dispatch, line, limitations="none")
    serialized = repr((snapshot, facts))
    assert all(secret not in serialized for secret in secrets)
    assert display in facts["test_command"]
    assert digest in facts["test_command"]


def test_a_cycle_and_a_missing_line_are_not_a_dag() -> None:
    cycle = [
        Line(id="a", name="a", task="a", depends_on=("b",)),
        Line(id="b", name="b", task="b", depends_on=("a",)),
    ]
    try:
        topological_lines(cycle)
    except DispatchError as exc:
        assert str(exc) == "The plan's line dependencies are not a DAG"
    else:
        raise AssertionError("cycle was accepted")
    missing = [Line(id="a", name="a", task="a", depends_on=("nope",))]
    try:
        topological_lines(missing)
    except DispatchError as exc:
        assert str(exc) == "The plan's line dependencies are not a DAG"
    else:
        raise AssertionError("missing dependency was accepted")


def test_topo_order_keeps_planner_order_inside_a_level() -> None:
    lines = [
        Line(id="c", name="c", task="c", depends_on=("a",)),
        Line(id="b", name="b", task="b", depends_on=("a",)),
        Line(id="a", name="a", task="a"),
    ]
    assert [line.id for line in topological_lines(lines)] == ["a", "c", "b"]


def test_gated_evidence_adds_prose_and_exit_codes_only(tmp_path: Path) -> None:
    dispatch = Dispatch(id="d1", goal="SECRET_GOAL", user_id="u", created_at="now")
    line = Line(
        id="line-0",
        name="api",
        task="SECRET_TASK",
        acceptance=("cmd: pytest -q", "returns 200 with SECRET_PROSE"),
        status=LineStatus.PASSED,
    )
    dispatch.lines = [line]
    plain = line_evidence(dispatch, {})("line-0")
    assert "Acceptance:" not in plain
    assert "SECRET_PROSE" not in plain
    dispatch.gates = {
        "schema_version": SCHEMA_VERSION,
        "lines": {
            "line-0": {
                "gates": {
                    "G2": {
                        "result": "pass",
                        "reason": "focused_tests_passed",
                        "commands": [
                            {
                                "index": 0,
                                "exit_code": 0,
                                "duration_ms": 5,
                                "log_ref": "gates/d1/line-0/g2-0-0.log",
                            }
                        ],
                    }
                }
            }
        },
    }
    gated = line_evidence(dispatch, {"line-0": "SECRET_OUTPUT"})("line-0")
    assert "returns 200 with SECRET_PROSE" in gated
    assert "command 0 exit 0" in gated
    assert "SECRET_OUTPUT" in gated  # the pre-existing output slot, when a test fills it
    assert "pytest -q" not in gated


@pytest.mark.asyncio
async def test_a_gated_summary_names_g2_and_g5_and_the_new_decision(tmp_path: Path) -> None:
    dispatch = Dispatch(id="d1", goal="ship it", user_id="u", created_at="now")
    dispatch.integrate_branch = "integrate/auto-dispatch-d1"
    dispatch.lines = [Line(id="line-0", name="api", task="write it", assigned_model="kimi")]
    dispatch.gates = {
        "schema_version": SCHEMA_VERSION,
        "lines": {
            "line-0": {
                "gates": {
                    "G2": {"result": "pass", "reason": "focused_tests_passed"},
                    "G5": {"result": "pass", "reason": "g5_passed"},
                    "primary_review": None,
                }
            }
        },
    }
    gate = FileApprovalGate(tmp_path)
    await gate.request_approval(dispatch)
    summary = (tmp_path / "d1" / "summary.md").read_text(encoding="utf-8")
    assert "| Line | G2 | Primary | Secondary | G5 |" in summary
    assert "pass (focused_tests_passed)" in summary
    assert "pass (g5_passed)" in summary
    assert "primary_review" not in summary
    assert "already merged into the integrate branch and G5 passed" in summary
    bare = Dispatch(id="d2", goal="ship it", user_id="u", created_at="now")
    bare.integrate_branch = "integrate/auto-dispatch-d2"
    bare.lines = [Line(id="line-0", name="api", task="write it")]
    await gate.request_approval(bare)
    ungated = (tmp_path / "d2" / "summary.md").read_text(encoding="utf-8")
    assert "approve — merge every line into the integrate branch" in ungated
    assert "## Gates" not in ungated


def test_the_runner_does_not_import_stage_gate_or_alembic() -> None:
    source = Path("src/tools/auto_dispatch/gates.py").read_text(encoding="utf-8")
    imports = [
        line.strip()
        for line in source.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    ]
    assert all("stage_gate" not in line and "alembic" not in line for line in imports)
    assert "shell=True" not in source


def test_fixture_shape_keys_are_the_ones_the_runner_emits() -> None:
    """The fixture is the contract. This only pins the key set the runner fills."""
    fixture = json.loads(
        Path("tests/fixtures/dispatch_gates_v1.json").read_text(encoding="utf-8")
    )
    sample = fixture["gates"]["lines"]["line-0"]
    assert "machine" in sample and "handoff" in sample["machine"]
    assert set(sample["events"][0]) == {
        "task_id",
        "from_stage",
        "to_stage",
        "gate",
        "result",
        "reason",
        "provider",
        "timestamp",
        "handoff_ref",
    }
