"""RV12: Git state that hides work, and drivers a mutation could run.

Each case builds a fake repository in ``tmp_path`` and runs the real Git
checks through a plain host runner. Before this change every hiding case
below read as delivered-clean (or, for the mutation, ran the repository's
merge driver); now each reads as uncommitted or unknown, never clean.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import pytest

from src.server.services.autonomy.git_delivery import (
    GitDrivers,
    _filter_names_with,
    _run_git,
    capture_git_snapshot,
    git_command_runner,
    git_probe_env,
    hardened_git_invocation,
    is_hardened_git_invocation,
    is_neutral_driver_override,
    run_isolated_git_mutation,
    safe_git_workspace_summary,
)

_UNKNOWN = "Git workspace status is unknown"


@pytest.fixture(autouse=True)
def _host_runner():
    def runner(argv, *, cwd, timeout, env):
        return subprocess.run(
            argv, cwd=cwd, timeout=timeout, env=env, capture_output=True, check=False
        )

    with git_command_runner(runner):
        yield


def _git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "Test Worker")
    _git(path, "config", "user.email", "worker@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    (path / "other.txt").write_text("other\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def _verdict(workspace: Path, attack) -> str:
    baseline = capture_git_snapshot(str(workspace))
    attack(workspace)
    summary = safe_git_workspace_summary(str(workspace), baseline)
    assert summary is not None
    return summary


def _write(path: Path, text: str, mode: str = "w") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, newline="", encoding="utf-8") as handle:
        handle.write(text)


# --- should-fix 1: a mutation never runs a repository-configured driver ----


@pytest.fixture
def passthrough_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run the mutation unsandboxed so only the Git hardening stands in the way."""
    monkeypatch.setenv("CONDUCTOR_WORKER_ISOLATION_ROOT", str(tmp_path / "iso"))

    @contextlib.contextmanager
    def isolation(**_kwargs):
        yield object()

    monkeypatch.setattr(
        "src.llms.native_cli.isolation.current_worker_isolation", lambda: None
    )
    monkeypatch.setattr("src.llms.native_cli.isolation.worker_isolation", isolation)
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.prepare_process",
        lambda argv, cwd, env, **_kw: (tuple(argv), dict(env), cwd),
    )


def test_mutation_merge_never_runs_the_repository_merge_driver(
    tmp_path: Path, passthrough_isolation
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@example.invalid")
    (repo / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    main = _git(repo, "branch", "--show-current")
    marker = tmp_path / "MERGE_DRIVER_RAN"
    _git(repo, "config", "merge.evil.driver", f"sh -c 'echo ran > {marker}; cp %B %A'")
    (repo / ".gitattributes").write_text("*.txt merge=evil\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "attrs")
    _git(repo, "checkout", "-q", "-b", "worker")
    (repo / "f.txt").write_text("X\nb\nc\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "worker")
    _git(repo, "checkout", "-q", main)
    (repo / "f.txt").write_text("Y\nb\nc\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "main")
    worktrees = tmp_path / "wts"
    worktrees.mkdir()
    line = worktrees / "line"
    _git(repo, "worktree", "add", "-q", "-b", "line", str(line), main)

    result = run_isolated_git_mutation(
        str(line),
        ["merge", "--no-ff", "--no-edit", "worker"],
        repository_root=str(repo),
        worktrees_root=str(worktrees),
        timeout=10,
    )

    assert not marker.exists()
    # The neutralised driver is an empty command: Git cannot run it and
    # leaves a conflict rather than the driver's resolution.
    assert result.returncode != 0
    assert b"cannot run" in result.stderr


def test_driver_discovery_names_merge_and_diff_drivers(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "drivers")
    _git(workspace, "config", "merge.evil.driver", "touch /tmp/never")
    _git(workspace, "config", "diff.Fancy.Name.textconv", "touch /tmp/never")
    _git(workspace, "config", "diff.cmd.command", "touch /tmp/never")
    _git(workspace, "config", "filter.f.clean", "touch /tmp/never")
    _git(workspace, "config", "merge.renormalize", "true")

    def run(args, timeout):
        return _run_git(str(workspace), list(args), timeout=timeout)

    drivers = _filter_names_with(run, str(workspace))

    assert drivers == GitDrivers(
        filters=("f",), merges=("evil",), diffs=("Fancy.Name", "cmd")
    )


def test_hardened_invocation_empties_every_driver_command() -> None:
    _argv, env = hardened_git_invocation(
        "git",
        ("status",),
        drivers=GitDrivers(filters=("f",), merges=("m",), diffs=("d",)),
    )
    overrides = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"]
        for index in range(int(env["GIT_CONFIG_COUNT"]))
    }

    assert overrides == {
        "filter.f.clean": "",
        "filter.f.smudge": "",
        "filter.f.process": "",
        "filter.f.required": "false",
        "merge.m.driver": "",
        "diff.d.textconv": "",
        "diff.d.command": "",
    }
    assert is_hardened_git_invocation(_argv, env)


def test_hardened_invocation_check_refuses_other_config_slots() -> None:
    """A forwarded env cannot smuggle a setting through the config slots."""
    argv, env = hardened_git_invocation("git", ("--version",))
    for key, value in (
        ("core.fsmonitor", "touch /tmp/x"),
        ("merge.evil.driver", "touch /tmp/x"),
        ("diff.evil.textconv", "cat"),
        ("filter.evil.required", "true"),
    ):
        smuggled = {
            **env,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": key,
            "GIT_CONFIG_VALUE_0": value,
        }
        assert not is_neutral_driver_override(key, value)
        assert not is_hardened_git_invocation(argv, smuggled)
    assert not is_hardened_git_invocation(
        argv, {**env, "GIT_CONFIG_PARAMETERS": "'core.fsmonitor'='x'"}
    )


# --- should-fix 2: dirty work must never read as delivered-clean ----------


def test_assume_unchanged_entry_is_unknown(tmp_path: Path) -> None:
    def attack(p: Path) -> None:
        _write(p / "tracked.txt", "secretly changed\n")
        _git(p, "update-index", "--assume-unchanged", "tracked.txt")

    assert _verdict(_repo(tmp_path / "w"), attack).startswith(_UNKNOWN)


def test_skip_worktree_entry_is_unknown(tmp_path: Path) -> None:
    def attack(p: Path) -> None:
        _write(p / "tracked.txt", "secretly changed\n")
        _git(p, "update-index", "--skip-worktree", "tracked.txt")

    assert _verdict(_repo(tmp_path / "w"), attack).startswith(_UNKNOWN)


def test_core_ignore_stat_marked_index_is_unknown(tmp_path: Path) -> None:
    def attack(p: Path) -> None:
        _git(p, "config", "core.ignoreStat", "true")
        _git(p, "update-index", "--really-refresh", check=False)
        _write(p / "tracked.txt", "changed2\n")

    assert _verdict(_repo(tmp_path / "w"), attack).startswith(_UNKNOWN)


def test_file_hidden_by_info_exclude_is_reported(tmp_path: Path) -> None:
    def attack(p: Path) -> None:
        _write(p / "newfile.py", "print('x')\n")
        _write(p / ".git" / "info" / "exclude", "newfile.py\n", "a")

    summary = _verdict(_repo(tmp_path / "w"), attack)
    assert summary.startswith("Uncommitted")
    assert "newfile.py" in summary


def test_self_ignoring_directory_is_reported(tmp_path: Path) -> None:
    def attack(p: Path) -> None:
        _write(p / "sub" / "new.py", "x\n")
        _write(p / "sub" / ".gitignore", "*\n")

    summary = _verdict(_repo(tmp_path / "w"), attack)
    assert summary.startswith("Uncommitted")
    assert "sub/new.py" in summary


def test_committed_ignore_rules_still_hide_build_output(tmp_path: Path) -> None:
    """Only non-committed rules are revealed; committed policy still applies."""
    workspace = _repo(tmp_path / "w")
    _write(workspace / ".gitignore", "build/\n")
    _git(workspace, "add", ".gitignore")
    _git(workspace, "commit", "-q", "-m", "ignore build")

    def attack(p: Path) -> None:
        _write(p / "build" / "out.o", "binary\n")
        _write(p / "build" / ".gitignore", "*\n")

    assert _verdict(workspace, attack) == ""


def test_replace_ref_over_head_does_not_hide_staged_work(tmp_path: Path) -> None:
    def attack(p: Path) -> None:
        _write(p / "tracked.txt", "uncommitted staged\n")
        _git(p, "add", "tracked.txt")
        tree = _git(p, "write-tree")
        head = _git(p, "rev-parse", "HEAD")
        fake = _git(p, "commit-tree", tree, "-m", "baseline")
        _git(p, "replace", head, fake)

    summary = _verdict(_repo(tmp_path / "w"), attack)
    assert summary.startswith("Uncommitted")
    assert "tracked.txt" in summary
    assert git_probe_env()["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_sparse_checkout_is_unknown(tmp_path: Path) -> None:
    def attack(p: Path) -> None:
        _git(p, "config", "core.sparseCheckout", "true")
        _write(p / ".git" / "info" / "sparse-checkout", "/other.txt\n")
        _write(p / "other.txt", "changed\n")

    assert _verdict(_repo(tmp_path / "w"), attack).startswith(_UNKNOWN)


def test_core_worktree_elsewhere_is_unknown(tmp_path: Path) -> None:
    """Already unknown before RV12 (root mismatch); kept as a guard."""

    def attack(p: Path) -> None:
        _git(p, "config", "core.worktree", str(p.parent))
        _write(p / "tracked.txt", "changed\n")

    assert _verdict(_repo(tmp_path / "w"), attack).startswith(_UNKNOWN)


def _ident_repo(path: Path) -> Path:
    workspace = _repo(path)
    _write(workspace / "id.txt", "t $Id$\n")
    _git(workspace, "add", "id.txt")
    _git(workspace, "commit", "-q", "-m", "ident")
    return workspace


def _readd_with_hidden_payload(p: Path) -> None:
    # With ``ident`` in force the payload normalises back to ``$Id$`` when
    # added, so the index matches HEAD while the file holds new content.
    _write(p / "id.txt", "t $Id: secret payload$\n")
    _git(p, "add", "id.txt")


def test_info_attributes_is_unknown(tmp_path: Path) -> None:
    def attack(p: Path) -> None:
        _write(p / ".git" / "info" / "attributes", "* ident\n")
        _readd_with_hidden_payload(p)

    assert _verdict(_ident_repo(tmp_path / "w"), attack).startswith(_UNKNOWN)


def test_ignored_gitattributes_beside_tracked_files_is_unknown(
    tmp_path: Path,
) -> None:
    workspace = _ident_repo(tmp_path / "w")
    _write(workspace / ".gitignore", ".gitattributes\n")
    _git(workspace, "add", ".gitignore")
    _git(workspace, "commit", "-q", "-m", "ignore attributes")

    def attack(p: Path) -> None:
        _write(p / ".gitattributes", "* ident\n")
        _readd_with_hidden_payload(p)

    assert _verdict(workspace, attack).startswith(_UNKNOWN)


def test_preexisting_untracked_gitattributes_is_unknown(tmp_path: Path) -> None:
    workspace = _ident_repo(tmp_path / "w")
    _write(workspace / ".gitattributes", "* ident\n")

    assert _verdict(workspace, _readd_with_hidden_payload).startswith(_UNKNOWN)


def test_plain_changes_are_still_reported_and_clean_stays_clean(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path / "w")
    assert _verdict(workspace, lambda _p: None) == ""

    def attack(p: Path) -> None:
        _write(p / "tracked.txt", "changed\n")

    summary = _verdict(_repo(tmp_path / "w2"), attack)
    assert summary.startswith("Uncommitted")
    assert "tracked.txt" in summary
