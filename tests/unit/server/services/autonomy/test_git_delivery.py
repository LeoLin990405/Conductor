"""Read-only git delivery checks for native Ticket completion."""

from __future__ import annotations

import os
import asyncio
import threading
import shutil
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    AsyncWorkspaceDeliveryLock,
    WorkspaceDeliveryBusy,
    WorkspaceDeliveryLock,
    _GIT_COMMAND_RUNNER,
    _git_binary,
    _git_runtime_read_paths,
    apply_git_delivery_guard,
    bound_git_delivery_body,
    capture_git_snapshot,
    clear_ticket_git_baseline,
    git_workspace_summary,
    safe_git_workspace_summary,
    ticket_git_baseline,
    uncommitted_git_summary,
    git_command_runner,
    run_isolated_git_mutation,
    run_isolated_git_readonly,
)


@pytest.fixture(autouse=True)
def _trusted_temp_repo_runner():
    """Keep filesystem fixtures local; production never uses this host runner."""
    def runner(argv, *, cwd, timeout, env):
        return subprocess.run(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_output=True,
            check=False,
        )

    with git_command_runner(runner):
        yield


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
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
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def test_non_git_workspace_is_unaffected(tmp_path: Path) -> None:
    workspace = tmp_path / "plain-workspace"
    workspace.mkdir()

    assert uncommitted_git_summary(str(workspace)) is None


def test_public_git_probe_rejects_mutation_and_redirection(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "readonly-helper")
    for args in (
        ["reset", "--hard"],
        ["worktree", "add", str(tmp_path / "other")],
        ["diff", "--output", str(tmp_path / "outside")],
        ["diff", "--out=" + str(tmp_path / "outside")],
        ["diff", "-o" + str(tmp_path / "outside")],
        ["diff", "-cfoo=bar"],
        ["rev-parse", "--git-dir"],
        ["show", "HEAD"],
        ["branch", "-D", "main"],
        ["worktree", "add", str(tmp_path / "worktree")],
        ["worktree", "prune"],
    ):
        with pytest.raises(GitDeliveryError):
            run_isolated_git_readonly(str(workspace), args)

    result = run_isolated_git_readonly(
        str(workspace), ["rev-parse", "--show-toplevel"]
    )
    assert result.returncode == 0
    assert Path(result.stdout.decode().strip()) == workspace
    worktrees = run_isolated_git_readonly(
        str(workspace), ["worktree", "list", "--porcelain"]
    )
    assert worktrees.returncode == 0
    assert f"worktree {workspace}" in worktrees.stdout.decode()


def test_public_git_probe_accepts_task6_readonly_forms(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "task6-readonly-forms")
    commands = (
        ["status", "--porcelain", "--untracked-files=all", "--ignore-submodules=all"],
        ["rev-parse", "HEAD"],
        ["rev-parse", "--verify", "HEAD"],
        ["branch", "--show-current"],
        ["worktree", "list", "--porcelain"],
        ["diff", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"],
        ["diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", "HEAD"],
        ["diff", "--name-only", "--diff-filter=U"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    )
    for command in commands:
        assert run_isolated_git_readonly(str(workspace), command).returncode == 0


def test_public_git_probe_accepts_only_guarded_task6_revision_diffs(tmp_path: Path) -> None:
    from src.server.services.autonomy.git_delivery import _public_git_command_is_safe

    workspace = _repo(tmp_path / "task6-revision-diffs")
    base = _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "checkout", "-qb", "feature/review")
    (workspace / "changed.txt").write_text("change\n", encoding="utf-8")
    _git(workspace, "add", "changed.txt")
    _git(workspace, "commit", "-q", "-m", "change")
    revision = _git(workspace, "rev-parse", "HEAD")
    review = [
        "diff", "--patch-with-stat", "--no-color", "--no-ext-diff",
        "--no-textconv", f"{base}...{revision}", "--",
    ]
    changed = [
        "diff", "--name-only", "--no-ext-diff", "--no-textconv",
        f"{base}...{revision}", "--",
    ]
    assert run_isolated_git_readonly(str(workspace), review).returncode == 0
    result = run_isolated_git_readonly(str(workspace), changed)
    assert result.returncode == 0
    assert result.stdout.decode().splitlines() == ["changed.txt"]

    unicode_branch = "feature/ü-safe"
    _git(workspace, "checkout", "-qb", unicode_branch)
    (workspace / "unicode.txt").write_text("unicode branch\n", encoding="utf-8")
    _git(workspace, "add", "unicode.txt")
    _git(workspace, "commit", "-q", "-m", "unicode ref")
    unicode_review = [
        "diff", "--patch-with-stat", "--no-color", "--no-ext-diff",
        "--no-textconv", f"{base}...{unicode_branch}", "--",
    ]
    assert run_isolated_git_readonly(str(workspace), unicode_review).returncode == 0

    rejected = (
        ["diff", "--patch-with-stat", "--no-color", "--no-textconv", f"{base}...{revision}", "--"],
        ["diff", "--name-only", f"{base}...{revision}"],
        ["diff", "--name-only", "--no-ext-diff", "--no-textconv", f"{base}...{revision}", "--", "changed.txt"],
        ["diff", "--name-only", "--no-ext-diff", "--no-textconv", f"-{base}...{revision}", "--"],
        ["diff", "--patch-with-stat", "--no-color", "--no-ext-diff", "--no-textconv", f"{base}...{revision}...HEAD", "--"],
        *[
            [
                "diff", "--patch-with-stat", "--no-color", "--no-ext-diff",
                "--no-textconv", f"{base}...{bad_ref}", "--",
            ]
            for bad_ref in (
                "feature/a b", "feature/zero\u200bwidth", "feature/right\u202eleft",
                "feature/\uff0doption", "feature/a..b", "feature/@{bad}",
                "feature/a\nb", "-option",
            )
        ],
    )
    for command in rejected:
        assert not _public_git_command_is_safe(tuple(command))
        with pytest.raises(GitDeliveryError):
            run_isolated_git_readonly(str(workspace), command)


def test_public_git_probe_accepts_exact_committed_paths_form(tmp_path: Path) -> None:
    """The collab report's rename-free, NUL-separated committed-path listing."""
    from src.server.services.autonomy.git_delivery import _public_git_command_is_safe

    workspace = _repo(tmp_path / "committed-paths")
    (workspace / "outside.txt").write_text("x\n", encoding="utf-8")
    _git(workspace, "add", "outside.txt")
    _git(workspace, "commit", "-q", "-m", "outside")
    base = _git(workspace, "rev-parse", "HEAD")
    _git(workspace, "checkout", "-qb", "feature/move")
    (workspace / "inside").mkdir()
    _git(workspace, "mv", "outside.txt", "inside/outside.txt")
    _git(workspace, "commit", "-q", "-m", "move")
    revision = _git(workspace, "rev-parse", "HEAD")
    flags = ["--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv"]
    command = ["diff", *flags, f"{base}...{revision}", "--"]
    result = run_isolated_git_readonly(str(workspace), command)
    assert result.returncode == 0
    assert sorted(p for p in result.stdout.decode().split("\0") if p) == [
        "inside/outside.txt", "outside.txt",
    ]
    rejected = (
        ["diff", *flags, f"{base}...{revision}"],
        ["diff", *flags, f"{base}...{revision}", "--", "outside.txt"],
        ["diff", *flags[:2], "--find-renames", *flags[3:], f"{base}...{revision}", "--"],
        ["diff", *flags[::-1], f"{base}...{revision}", "--"],
        ["diff", *flags, f"-{base}...{revision}", "--"],
        ["diff", *flags, f"{base}..{revision}", "--"],
    )
    for bad in rejected:
        assert not _public_git_command_is_safe(tuple(bad))
        with pytest.raises(GitDeliveryError):
            run_isolated_git_readonly(str(workspace), bad)


def test_task6_merge_driver_runs_inside_writable_no_network_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "mutation-boundary-repo")
    worktrees = tmp_path / "dispatch-worktrees"
    worktrees.mkdir()
    integration = worktrees / "integration"
    marker = tmp_path / "outside-marker"
    monkeypatch.setenv(
        "CONDUCTOR_WORKER_ISOLATION_ROOT", str(tmp_path / "isolation-state")
    )
    main = _git(repo, "branch", "--show-current")

    _git(repo, "config", "merge.task6poc.name", "task6 marker driver")
    _git(
        repo,
        "config",
        "merge.task6poc.driver",
        f"/usr/bin/touch {marker}; /bin/cp %B %A",
    )
    _git(repo, "checkout", "-qb", "attacker")
    (repo / "tracked.txt").write_text("attacker\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "attacker change")
    _git(repo, "checkout", "-q", main)
    (repo / "tracked.txt").write_text("main\n", encoding="utf-8")
    _git(repo, "commit", "-qam", "main change")
    _git(repo, "worktree", "add", "-qb", "integrate", str(integration), main)
    (repo / ".git" / "info" / "attributes").write_text(
        "tracked.txt merge=task6poc\n", encoding="utf-8"
    )

    result = run_isolated_git_mutation(
        str(integration),
        ["merge", "--no-ff", "--no-edit", "attacker"],
        repository_root=str(repo),
        worktrees_root=str(worktrees),
        timeout=10,
    )

    # The repository's merge driver is neutralised, so Git never starts it:
    # the path becomes a conflict instead of being merged by its program.
    assert result.returncode != 0
    stderr = result.stderr.decode("utf-8", "replace")
    assert "cannot run" in stderr
    assert not marker.exists()
    with pytest.raises(GitDeliveryError):
        run_isolated_git_mutation(
            str(integration),
            ["push", "--set-upstream", "origin", "attacker"],
            repository_root=str(repo),
            worktrees_root=str(worktrees),
        )


def test_task6_mutations_accept_trusted_linked_repository_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _repo(tmp_path / "source-repo")
    main = _git(source, "branch", "--show-current")
    trusted_root = tmp_path / "trusted-linked-root"
    _git(source, "worktree", "add", "-b", "trusted-root", str(trusted_root), main)
    worktrees = tmp_path / "dispatch-worktrees"
    worktrees.mkdir()
    integration = worktrees / "integration"
    _git(source, "worktree", "add", "-b", "integration", str(integration), main)
    monkeypatch.setenv(
        "CONDUCTOR_WORKER_ISOLATION_ROOT", str(tmp_path / "isolation-state")
    )

    detached = worktrees / "detached"
    added = run_isolated_git_mutation(
        str(trusted_root),
        ["worktree", "add", "--detach", str(detached), main],
        repository_root=str(trusted_root),
        worktrees_root=str(worktrees),
    )
    assert added.returncode == 0, added.stderr.decode("utf-8", "replace")
    removed = run_isolated_git_mutation(
        str(trusted_root),
        ["worktree", "remove", "--force", str(detached)],
        repository_root=str(trusted_root),
        worktrees_root=str(worktrees),
    )
    assert removed.returncode == 0, removed.stderr.decode("utf-8", "replace")
    pruned = run_isolated_git_mutation(
        str(trusted_root),
        ["worktree", "prune"],
        repository_root=str(trusted_root),
        worktrees_root=str(worktrees),
    )
    assert pruned.returncode == 0, pruned.stderr.decode("utf-8", "replace")

    renamed = run_isolated_git_mutation(
        str(trusted_root),
        ["branch", "-m", "trusted-root", "renamed-root"],
        repository_root=str(trusted_root),
        worktrees_root=str(worktrees),
    )
    assert renamed.returncode == 0, renamed.stderr.decode("utf-8", "replace")
    with pytest.raises(GitDeliveryError, match="outside the dispatch tree"):
        run_isolated_git_mutation(
            str(trusted_root),
            ["merge", "--no-ff", "--no-edit", main],
            repository_root=str(trusted_root),
            worktrees_root=str(worktrees),
        )

    _git(source, "checkout", "-qb", "feature-conflict")
    (source / "tracked.txt").write_text("feature\n", encoding="utf-8")
    _git(source, "commit", "-qam", "feature change")
    _git(source, "checkout", "-q", main)
    (source / "tracked.txt").write_text("main change\n", encoding="utf-8")
    _git(source, "commit", "-qam", "main conflict")
    merged_main = run_isolated_git_mutation(
        str(integration),
        ["merge", "--no-ff", "--no-edit", main],
        repository_root=str(trusted_root),
        worktrees_root=str(worktrees),
    )
    assert merged_main.returncode == 0, merged_main.stderr.decode("utf-8", "replace")
    conflict = run_isolated_git_mutation(
        str(integration),
        ["merge", "--no-ff", "--no-edit", "feature-conflict"],
        repository_root=str(trusted_root),
        worktrees_root=str(worktrees),
    )
    assert conflict.returncode != 0
    aborted = run_isolated_git_mutation(
        str(integration),
        ["merge", "--abort"],
        repository_root=str(trusted_root),
        worktrees_root=str(worktrees),
    )
    assert aborted.returncode == 0, aborted.stderr.decode("utf-8", "replace")

    _git(source, "checkout", "-qb", "feature-safe", main)
    (source / "safe.txt").write_text("safe merge\n", encoding="utf-8")
    _git(source, "add", "safe.txt")
    _git(source, "commit", "-qm", "safe change")
    merged = run_isolated_git_mutation(
        str(integration),
        ["merge", "--no-ff", "--no-edit", "feature-safe"],
        repository_root=str(trusted_root),
        worktrees_root=str(worktrees),
    )
    assert merged.returncode == 0, merged.stderr.decode("utf-8", "replace")


def test_task6_mutation_rejects_foreign_linked_repository_identity(
    tmp_path: Path,
) -> None:
    trusted = _repo(tmp_path / "trusted")
    trusted_branch = _git(trusted, "branch", "--show-current")
    trusted_root = tmp_path / "trusted-linked-root"
    _git(trusted, "worktree", "add", "-b", "trusted", str(trusted_root), trusted_branch)
    worktrees = tmp_path / "dispatch-worktrees"
    worktrees.mkdir()
    foreign = _repo(tmp_path / "foreign")
    foreign_branch = _git(foreign, "branch", "--show-current")
    foreign_root = worktrees / "foreign"
    _git(foreign, "worktree", "add", "-b", "foreign", str(foreign_root), foreign_branch)

    with pytest.raises(GitDeliveryError, match="repository identity"):
        run_isolated_git_mutation(
            str(foreign_root),
            ["branch", "-m", "foreign", "renamed-foreign"],
            repository_root=str(trusted_root),
            worktrees_root=str(worktrees),
        )


def test_snapshot_uses_one_whole_index_probe_for_all_dirty_paths(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "single-index")
    for index in range(5):
        (workspace / f"untracked-{index}.txt").write_text("worker\n", encoding="utf-8")
    commands = []

    def recording_runner(argv, *, cwd, timeout, env):
        commands.append(tuple(argv))
        return subprocess.run(
            argv, cwd=cwd, timeout=timeout, env=env, capture_output=True, check=False
        )

    with git_command_runner(recording_runner):
        snapshot = capture_git_snapshot(str(workspace))

    assert len(snapshot.entries) == 5
    index_calls = [command for command in commands if "--stage" in command]
    assert len(index_calls) == 1
    assert index_calls[0][-3:] == ("ls-files", "--stage", "-z")


def test_diff_stats_use_top_literal_pathspec_from_a_subdirectory(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "top-literal")
    nested = workspace / "nested"
    nested.mkdir()
    special = nested / "file[one].txt"
    neighbor = nested / "file1.txt"
    special.write_text("base special\n", encoding="utf-8")
    neighbor.write_text("base neighbor\n", encoding="utf-8")
    _git(workspace, "add", "nested")
    _git(workspace, "commit", "-q", "-m", "nested")
    neighbor.write_text("preexisting dirt\n", encoding="utf-8")
    baseline = capture_git_snapshot(str(nested))
    special.write_text("new worker edit\n", encoding="utf-8")

    summary = git_workspace_summary(str(nested), baseline)

    assert summary is not None
    assert "nested/file[one].txt" in summary
    assert "nested/file1.txt" not in summary


def test_subdirectory_staged_only_change_uses_repo_root_index(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "subdir-staged-index")
    nested = workspace / "nested"
    nested.mkdir()
    tracked = nested / "staged.txt"
    tracked.write_text("initial\n", encoding="utf-8")
    _git(workspace, "add", "nested/staged.txt")
    _git(workspace, "commit", "-q", "-m", "add staged fixture")
    baseline = capture_git_snapshot(str(nested))
    tracked.write_text("staged worker change\n", encoding="utf-8")
    _git(workspace, "add", "nested/staged.txt")

    summary = git_workspace_summary(str(nested), baseline)

    assert summary is not None
    assert "M  nested/staged.txt" in summary
    assert "nested/staged.txt |" in summary


def test_transient_ticket_baseline_error_is_not_cached(tmp_path: Path, monkeypatch) -> None:
    workspace = _repo(tmp_path / "transient-baseline")
    from src.server.services.autonomy import git_delivery

    original = git_delivery.capture_git_snapshot
    calls = 0

    def flaky_capture(cwd):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GitDeliveryError("temporary boundary issue")
        return original(cwd)

    monkeypatch.setattr(git_delivery, "capture_git_snapshot", flaky_capture)
    first, first_error = ticket_git_baseline("transient-ticket", str(workspace))
    second, second_error = ticket_git_baseline("transient-ticket", str(workspace))

    assert first is None and first_error
    assert second is not None and not second_error
    assert calls == 2
    clear_ticket_git_baseline("transient-ticket")


def test_git_delivery_body_cap_counts_utf8_bytes_and_keeps_warning_first() -> None:
    result = apply_git_delivery_guard(
        {"state": "ready", "body": "界" * (256 * 1024)},
        "Uncommitted git changes detected.\ntracked.txt",
    )

    body = result["body"]
    assert body.startswith("Uncommitted git changes detected.")
    assert body.endswith("[… report truncated …]")
    assert len(body.encode("utf-8")) <= 256 * 1024


def test_git_summary_caps_many_untracked_paths(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "many-untracked")
    baseline = capture_git_snapshot(str(workspace))
    for index in range(125):
        (workspace / f"new-{index:03}.txt").write_text("x\n", encoding="utf-8")

    summary = git_workspace_summary(str(workspace), baseline)

    assert summary is not None
    assert "… and 25 more changed paths" in summary
    assert summary.count("?? new-") == 100
    assert len(bound_git_delivery_body(summary).encode("utf-8")) <= 256 * 1024


def test_clean_git_workspace_has_no_uncommitted_summary(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "clean")

    assert uncommitted_git_summary(str(workspace)) == ""


def test_untracked_file_is_reported(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "untracked")
    (workspace / "new.txt").write_text("new\n", encoding="utf-8")

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "Uncommitted git changes detected." in summary
    assert "\n?? new.txt\n" in summary


def test_unstaged_file_is_reported_with_diff_stats(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "unstaged")
    (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "\n M tracked.txt\n" in summary
    assert "tracked.txt |" in summary


def test_staged_file_is_reported_even_without_cached_diff_stats(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "staged")
    (workspace / "tracked.txt").write_text("staged change\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "\nM  tracked.txt\n" in summary
    assert "Uncommitted git changes detected." in summary
    assert "tracked.txt |" in summary


def test_committed_change_is_clean(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "committed")
    (workspace / "tracked.txt").write_text("committed change\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    _git(workspace, "commit", "-q", "-m", "worker change")

    assert uncommitted_git_summary(str(workspace)) == ""


def test_preexisting_dirty_file_is_ignored_but_same_file_edits_are_detected(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path / "baseline-dirty")
    tracked = workspace / "tracked.txt"
    tracked.write_text("preexisting dirty\n", encoding="utf-8")
    baseline = capture_git_snapshot(str(workspace))
    baseline = type(baseline).from_record(baseline.to_record())
    assert baseline is not None

    assert git_workspace_summary(str(workspace), baseline) == ""

    prior_mtime = tracked.stat().st_mtime_ns
    tracked.write_text("new value same size\n", encoding="utf-8")
    import os

    os.utime(tracked, ns=(prior_mtime, prior_mtime))
    summary = git_workspace_summary(str(workspace), baseline)
    assert summary is not None
    assert " M tracked.txt" in summary


def test_snapshot_round_trip_rejects_partial_records(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "snapshot-record")
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    snapshot = capture_git_snapshot(str(workspace))
    record = snapshot.to_record()

    assert type(snapshot).from_record(record) == snapshot
    record["entries"][next(iter(record["entries"]))] = [" M", "not-a-digest", "also-bad"]
    assert type(snapshot).from_record(record) is None


def test_preexisting_staged_file_detects_new_index_identity(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "baseline-staged")
    tracked = workspace / "tracked.txt"
    tracked.write_text("first staged\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")
    baseline = capture_git_snapshot(str(workspace))
    tracked.write_text("other staged\n", encoding="utf-8")
    _git(workspace, "add", "tracked.txt")

    summary = git_workspace_summary(str(workspace), baseline)
    assert summary is not None
    assert "M  tracked.txt" in summary


def test_ticket_retry_reuses_original_baseline_for_its_uncommitted_work(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path / "same-ticket-retry")
    ticket_id = f"ticket-{workspace.name}"
    baseline, error = ticket_git_baseline(ticket_id, str(workspace))
    assert not error and baseline is not None
    (workspace / "unfinished.txt").write_text("first attempt output\n", encoding="utf-8")

    retry_baseline, retry_error = ticket_git_baseline(ticket_id, str(workspace))
    assert not retry_error
    assert retry_baseline == baseline
    summary = safe_git_workspace_summary(str(workspace), retry_baseline)
    assert summary is not None
    assert "unfinished.txt" in summary
    clear_ticket_git_baseline(ticket_id)


def test_new_untracked_file_is_reported_while_old_dirty_file_is_omitted(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path / "baseline-mixed")
    (workspace / "tracked.txt").write_text("operator dirt\n", encoding="utf-8")
    baseline = capture_git_snapshot(str(workspace))
    (workspace / "new[one].txt").write_text("worker dirt\n", encoding="utf-8")

    summary = git_workspace_summary(str(workspace), baseline)
    assert summary is not None
    assert "new[one].txt" in summary
    assert "tracked.txt" not in summary


def test_non_git_baseline_detects_a_new_repo_created_by_the_worker(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "initially-plain"
    workspace.mkdir()
    baseline = capture_git_snapshot(str(workspace))
    _git(workspace, "init", "-q")
    (workspace / "new.txt").write_text("worker output\n", encoding="utf-8")

    summary = git_workspace_summary(str(workspace), baseline)
    assert summary is not None
    assert "new.txt" in summary


def test_gitdir_redirect_outside_workspace_is_rejected(tmp_path: Path) -> None:
    external = _repo(tmp_path / "external")
    workspace = tmp_path / "redirected"
    workspace.mkdir()
    (workspace / ".git").write_text(f"gitdir: {external / '.git'}\n", encoding="utf-8")

    with pytest.raises(GitDeliveryError):
        capture_git_snapshot(str(workspace))


def test_invalid_utf8_filter_config_fails_closed(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "invalid-filter-name")
    config = workspace / ".git" / "config"
    config.write_bytes(
        config.read_bytes()
        + b"\n[filter \"raw" + bytes([0xFF])
        + b"\"]\n\tclean = touch /tmp/unsafe-filter-marker\n"
    )
    (workspace / ".gitattributes").write_bytes(b"tracked.txt filter=raw" + bytes([0xFF]) + b"\n")

    with pytest.raises(GitDeliveryError):
        capture_git_snapshot(str(workspace))


def test_host_probe_disables_worker_controlled_git_programs_and_git_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _repo(tmp_path / "hostile")
    _git(workspace, "config", "extensions.worktreeConfig", "true")
    linked = tmp_path / "linked-worktree"
    _git(workspace, "worktree", "add", "-q", "-b", "delivery-probe", str(linked))
    markers = [tmp_path / f"executed-{index}" for index in range(2)]
    script = tmp_path / "git-program.sh"
    script.write_text(
        "#!/bin/sh\n"
        f"printf x >> {shlex.quote(str(markers[0]))}\n"
        "cat >/dev/null\n"
        "cat\n",
        encoding="utf-8",
    )
    script.chmod(0o700)
    command = f"sh {shlex.quote(str(script))}"
    # The include-only form of this driver is refused outright; see
    # test_an_included_filter_driver_never_runs_on_any_read_only_path.
    _git(workspace, "config", "filter.included.clean", command)
    _git(workspace, "config", "filter.included.smudge", command)
    _git(workspace, "config", "filter.included.process", command)
    _git(workspace, "config", "filter.included.required", "true")
    _git(workspace, "config", "core.fsmonitor", command)
    _git(workspace, "config", "diff.hostile.textconv", command)
    _git(workspace, "config", "diff.external", command)
    _git(workspace, "config", "core.hooksPath", str(tmp_path / "hooks"))
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {shlex.quote(str(markers[1]))}\n", encoding="utf-8")
    hook.chmod(0o700)
    (workspace / ".gitattributes").write_text(
        "tracked.txt filter=included diff=hostile\n", encoding="utf-8"
    )
    tracked = workspace / "tracked.txt"
    tracked.write_text("worker changed\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-git-dir"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-work-tree"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", command)
    real_run = subprocess.run
    observed: list[tuple[list[str], dict[str, str]]] = []

    def record_git_commands(args, *call_args, **kwargs):
        if args and Path(args[0]).name == "git":
            observed.append((list(args), dict(kwargs.get("env") or {})))
        return real_run(args, *call_args, **kwargs)

    monkeypatch.setattr(
        "src.server.services.autonomy.git_delivery.subprocess.run",
        record_git_commands,
    )

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "tracked.txt" in summary
    assert all(not marker.exists() for marker in markers)
    assert observed
    assert all("-c" in args and "core.hooksPath=/dev/null" in args for args, _env in observed)
    assert any(
        any(
            env.get(f"GIT_CONFIG_KEY_{index}") == "filter.included.clean"
            and env.get(f"GIT_CONFIG_VALUE_{index}") == ""
            for index in range(int(env.get("GIT_CONFIG_COUNT", "0")))
        )
        for _args, env in observed
    )
    assert all("--no-ext-diff" in args and "--no-textconv" in args for args, _env in observed if "diff" in args)
    assert all(
        not ({"GIT_DIR", "GIT_WORK_TREE"} & env.keys())
        for _args, env in observed
    )
    assert all(env.get("GIT_CONFIG_NOSYSTEM") == "1" for _args, env in observed)
    assert all(env.get("GIT_CONFIG_GLOBAL") == "/dev/null" for _args, env in observed)
    assert all(env.get("GIT_NO_LAZY_FETCH") == "1" for _args, env in observed)
    assert all(env.get("GIT_TERMINAL_PROMPT") == "0" for _args, env in observed)

    # Linked worktrees can add filter definitions in config.worktree. The
    # effective-config query must discover and neutralize those too.
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}

    def trusted_git(path: Path, *args: str) -> str:
        result = real_run(
            ["git", "-C", str(path), *args],
            check=True,
            capture_output=True,
            text=True,
            env=clean_env,
        )
        return result.stdout.strip()

    trusted_git(linked, "config", "--worktree", "filter.worktree.clean", command)
    trusted_git(linked, "config", "--worktree", "filter.worktree.smudge", command)
    trusted_git(linked, "config", "--worktree", "filter.worktree.process", command)
    (linked / ".gitattributes").write_text(
        "tracked.txt filter=worktree\n", encoding="utf-8"
    )
    (linked / "tracked.txt").write_text("linked worker change\n", encoding="utf-8")
    assert all(not marker.exists() for marker in markers), "test fixture setup executed Git hooks or filters"
    linked_summary = uncommitted_git_summary(str(linked))

    assert linked_summary is not None
    assert "tracked.txt" in linked_summary
    assert any(
        any(
            env.get(f"GIT_CONFIG_KEY_{index}") == "filter.worktree.clean"
            and env.get(f"GIT_CONFIG_VALUE_{index}") == ""
            for index in range(int(env.get("GIT_CONFIG_COUNT", "0")))
        )
        for _args, env in observed
    )
    assert all(not marker.exists() for marker in markers), {
        "markers": [marker.name for marker in markers if marker.exists()],
        "commands": [args for args, _env in observed[-12:]],
    }


@pytest.mark.parametrize("setting", ["clean", "smudge", "process"])
def test_equals_filter_name_fails_closed_without_running_program(
    tmp_path: Path, setting: str
) -> None:
    workspace = _repo(tmp_path / f"equals-filter-{setting}")
    marker = tmp_path / "equals-filter-executed"
    command = f"touch {shlex.quote(str(marker))}"
    config = workspace / ".git" / "config"
    with config.open("a", encoding="utf-8") as handle:
        handle.write(f'\n[filter "a=b"]\n\t{setting} = {command}\n')
        if setting == "process":
            handle.write("\trequired = true\n")
    (workspace / ".gitattributes").write_text(
        "tracked.txt filter=a=b\n", encoding="utf-8"
    )

    with pytest.raises(GitDeliveryError):
        capture_git_snapshot(str(workspace))

    assert not marker.exists()


def test_same_repository_subdirectories_share_worker_lock(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "locked")
    nested = workspace / "nested"
    nested.mkdir()
    lock_root = tmp_path / "locks"
    with WorkspaceDeliveryLock(str(workspace), timeout=0.1, lock_root=str(lock_root)):
        with pytest.raises(WorkspaceDeliveryBusy, match="Worker lock is busy"):
            WorkspaceDeliveryLock(
                str(nested), timeout=0.05, lock_root=str(lock_root)
            ).acquire()


@pytest.mark.asyncio
async def test_eight_waiting_tickets_survive_a_two_second_lock_contention(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path / "eight-lockers")
    lock_root = tmp_path / "locks"
    owner = WorkspaceDeliveryLock(str(workspace), lock_root=str(lock_root))
    assert owner.timeout == 30.0
    owner.acquire()
    acquired: list[int] = []

    async def waiter(index: int) -> None:
        async with AsyncWorkspaceDeliveryLock(
            str(workspace), lock_root=str(lock_root)
        ):
            acquired.append(index)

    waiters = [asyncio.create_task(waiter(index)) for index in range(8)]
    await asyncio.sleep(2.1)
    owner.release()
    await asyncio.wait_for(asyncio.gather(*waiters), timeout=5.0)
    assert sorted(acquired) == list(range(8))


@pytest.mark.asyncio
async def test_cancelled_async_workspace_lock_waiter_releases_after_acquire(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path / "cancelled-locker")
    lock_root = tmp_path / "locks"
    owner = WorkspaceDeliveryLock(str(workspace), lock_root=str(lock_root))
    owner.acquire()
    waiter = asyncio.create_task(
        AsyncWorkspaceDeliveryLock(
            str(workspace), lock_root=str(lock_root)
        ).__aenter__()
    )
    await asyncio.sleep(0.05)
    waiter.cancel()
    owner.release()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, timeout=2.0)
    with AsyncWorkspaceDeliveryLock(
        str(workspace), timeout=0.1, lock_root=str(lock_root)
    ):
        pass


@pytest.mark.asyncio
async def test_repeated_cancel_during_async_lock_attempt_does_not_leak_flock(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path / "repeat-cancel-locker")
    lock_root = tmp_path / "locks"
    owner = WorkspaceDeliveryLock(str(workspace), lock_root=str(lock_root))
    owner.acquire()
    waiter_lock = AsyncWorkspaceDeliveryLock(str(workspace), lock_root=str(lock_root))
    started = threading.Event()
    finish_attempt = threading.Event()
    original_try = waiter_lock._try_acquire

    def delayed_try() -> bool:
        started.set()
        finish_attempt.wait()
        return original_try()

    waiter_lock._try_acquire = delayed_try

    async def wait_for_lock() -> None:
        async with waiter_lock:
            pass

    waiter = asyncio.create_task(wait_for_lock())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=2.0)
        waiter.cancel()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=2.0)
        owner.release()
        finish_attempt.set()
        await asyncio.sleep(0.05)
        async with AsyncWorkspaceDeliveryLock(
            str(workspace), timeout=0.1, lock_root=str(lock_root)
        ):
            pass
    finally:
        finish_attempt.set()
        owner.release()


def test_workspace_lock_timeout_is_finite_and_capped(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "bounded-lock-timeout")
    assert WorkspaceDeliveryLock(str(workspace), timeout=60).timeout == 30.0
    with pytest.raises(ValueError, match="finite"):
        WorkspaceDeliveryLock(str(workspace), timeout=float("inf"))


def test_git_and_runtime_tools_ignore_workspace_controlled_path_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _repo(tmp_path / "path-override")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    git_marker = tmp_path / "fake-git-executed"
    otool_marker = tmp_path / "fake-otool-executed"
    fake_git = fake_bin / "git"
    fake_otool = fake_bin / "otool"
    fake_git.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(git_marker))}\nexit 99\n",
        encoding="utf-8",
    )
    fake_otool.write_text(
        f"#!/bin/sh\ntouch {shlex.quote(str(otool_marker))}\nexit 0\n",
        encoding="utf-8",
    )
    fake_git.chmod(0o700)
    fake_otool.chmod(0o700)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.defpath}")

    selected_git = Path(_git_binary())
    runtime_paths = _git_runtime_read_paths()
    snapshot = capture_git_snapshot(str(workspace))

    assert selected_git != fake_git
    assert fake_bin not in selected_git.parents
    assert all(fake_bin not in Path(path).parents for path in runtime_paths)
    assert snapshot.root_id
    assert not git_marker.exists()
    assert not otool_marker.exists()


@pytest.mark.skipif(
    not (shutil.which("sandbox-exec") or shutil.which("bwrap")),
    reason="native OS isolation tool is unavailable",
)
def test_git_probe_process_starts_inside_readonly_no_network_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _repo(tmp_path / "boundary")
    marker = tmp_path / "outside-write-marker"
    _git(workspace, "config", "core.fsmonitor", f"touch {shlex.quote(str(marker))}")
    (workspace / "tracked.txt").write_text("boundary check\n", encoding="utf-8")
    monkeypatch.setenv(
        "CONDUCTOR_WORKER_ISOLATION_ROOT", str(tmp_path / "git-probe-state")
    )
    monkeypatch.setenv("WORKER_GIT_SECRET_SENTINEL", "must-not-enter-probe")
    token = _GIT_COMMAND_RUNNER.set(None)
    real_popen = subprocess.Popen
    launched: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def record_boundary(argv, *args, **kwargs):
        launched.append((tuple(argv), dict(kwargs.get("env") or {})))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(
        "src.server.services.autonomy.git_delivery.subprocess.Popen", record_boundary
    )
    try:
        summary = uncommitted_git_summary(str(workspace))
    finally:
        _GIT_COMMAND_RUNNER.reset(token)

    assert summary and "tracked.txt" in summary
    assert not marker.exists()
    assert launched
    sandbox_tools = {shutil.which("sandbox-exec"), shutil.which("bwrap")}
    git_processes = [
        (argv, env)
        for argv, env in launched
        if argv[0] in sandbox_tools
    ]
    assert git_processes
    for argv, env in git_processes:
        if Path(argv[0]).name == "sandbox-exec":
            assert "(allow network-outbound)" not in argv[2]
        else:
            assert "--unshare-net" in argv
        assert "WORKER_GIT_SECRET_SENTINEL" not in env
    assert all(
        env.get("GIT_CONFIG_NOSYSTEM") == "1"
        and env.get("GIT_CONFIG_GLOBAL") == "/dev/null"
        for _argv, env in git_processes
    )


@pytest.mark.skipif(
    not (shutil.which("sandbox-exec") or shutil.which("bwrap")),
    reason="native OS isolation tool is unavailable",
)
def test_linked_worktree_probe_reads_only_registered_git_metadata_under_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _repo(tmp_path / "linked-parent")
    _git(workspace, "config", "extensions.worktreeConfig", "true")
    linked = tmp_path / "linked-child"
    _git(workspace, "worktree", "add", "-q", "-b", "probe-linked", str(linked))
    monkeypatch.setenv(
        "CONDUCTOR_WORKER_ISOLATION_ROOT", str(tmp_path / "linked-probe-state")
    )
    token = _GIT_COMMAND_RUNNER.set(None)
    try:
        baseline = capture_git_snapshot(str(linked))
        (linked / "tracked.txt").write_text("linked change\n", encoding="utf-8")
        summary = git_workspace_summary(str(linked), baseline)
    finally:
        _GIT_COMMAND_RUNNER.reset(token)

    assert summary is not None
    assert "tracked.txt" in summary


def test_unborn_repository_keeps_dirty_status_when_stats_fail(tmp_path: Path) -> None:
    workspace = tmp_path / "unborn"
    workspace.mkdir()
    _git(workspace, "init", "-q")
    (workspace / "new.txt").write_text("new\n", encoding="utf-8")

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "Uncommitted git changes detected." in summary
    assert "\n?? new.txt\n" in summary
    assert "diff stats unavailable" in summary


def test_status_filename_and_diagnostic_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _repo(tmp_path / "redacted")
    sentinel = "sentinelcredentialvalue123456"
    monkeypatch.setenv("TEST_API_KEY", sentinel)
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.current_worker_isolation",
        lambda: SimpleNamespace(env_names=frozenset({"TEST_API_KEY"})),
    )
    (workspace / f"api_key={sentinel}.txt").write_text(
        "secret path\n", encoding="utf-8"
    )

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "Uncommitted git changes detected." in summary
    assert sentinel not in summary
    assert "[REDACTED]" in summary


@pytest.mark.parametrize("failure", ["nonzero", "timeout"])
def test_status_failure_returns_safe_nonempty_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    workspace = _repo(tmp_path / f"status-{failure}")
    real_run = subprocess.run

    def failing_run(command, *args, **kwargs):
        if "status" in command:
            if failure == "timeout":
                raise subprocess.TimeoutExpired(command, timeout=10)
            return subprocess.CompletedProcess(
                command, 2, stdout="", stderr="secret detail"
            )
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(
        "src.server.services.autonomy.git_delivery.subprocess.run", failing_run
    )

    summary = uncommitted_git_summary(str(workspace))

    assert summary.startswith("Git workspace status ")
    assert "safely." in summary
    assert summary
    assert "secret detail" not in summary


def test_dirty_status_survives_diff_stats_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _repo(tmp_path / "stats-failure")
    (workspace / "tracked.txt").write_text("changed\n", encoding="utf-8")
    real_run = subprocess.run

    def failing_stats(command, *args, **kwargs):
        if "diff" in command and "--stat" in command:
            raise subprocess.TimeoutExpired(command, timeout=10)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(
        "src.server.services.autonomy.git_delivery.subprocess.run", failing_stats
    )

    summary = uncommitted_git_summary(str(workspace))

    assert summary is not None
    assert "Uncommitted git changes detected." in summary
    assert "tracked.txt" in summary
    assert "diff stats unavailable" in summary
