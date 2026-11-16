"""R4-2 hardening and N56 bounded reads, together.

- Every Git process the bounded (streaming) runner starts, for a snapshot, a
  listing and a trimmed review-diff prefix, carries exactly the invocation
  ``hardened_git_invocation`` built: ``is_hardened_git_invocation`` accepts it.
- R4-2's own listing forms (``ls-files -v``, the untracked / ignored
  expansions) get the listing cap, like every ``ls-files`` form.
- The streamed index parse still feeds R4-2's hidden-``.gitattributes`` check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.server.services.autonomy import git_delivery
from src.server.services.autonomy.git_delivery import (
    GitDeliveryError,
    capture_git_snapshot,
    is_hardened_git_invocation,
    run_isolated_git_readonly,
    run_isolated_git_readonly_prefix,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Merge Test")
    _git(path, "config", "user.email", "merge@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, str]]]:
    """Run Git on the host (no sandbox wrapper) and record every Popen."""
    monkeypatch.setattr(git_delivery, "_git_runtime_read_paths", lambda: ())
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.current_worker_isolation", lambda: object()
    )
    monkeypatch.setattr(
        "src.llms.native_cli.isolation.prepare_process",
        lambda argv, cwd, env, **_kwargs: (tuple(argv), dict(env), cwd),
    )
    seen: list[tuple[list[str], dict[str, str]]] = []
    real_popen = subprocess.Popen

    def popen(args, *call_args, **kwargs):
        if args and Path(args[0]).name == "git":
            seen.append((list(args), dict(kwargs.get("env") or {})))
        return real_popen(args, *call_args, **kwargs)

    monkeypatch.setattr(git_delivery.subprocess, "Popen", popen)
    token = git_delivery._GIT_COMMAND_RUNNER.set(None)
    yield seen
    git_delivery._GIT_COMMAND_RUNNER.reset(token)


def _assert_all_hardened(seen) -> None:
    assert seen, "no Git process was started"
    for argv, env in seen:
        assert is_hardened_git_invocation(argv, env), argv


def test_a_snapshot_streams_only_hardened_git(repo: Path, spawned) -> None:
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    spawned.clear()

    snapshot = capture_git_snapshot(str(repo))

    assert len(snapshot.entries) == 2
    _assert_all_hardened(spawned)
    assert any(argv[-3:] == ["ls-files", "--stage", "-z"] for argv, _ in spawned)


def test_a_listing_and_a_trimmed_prefix_run_hardened_git(repo: Path, spawned) -> None:
    _git(repo, "checkout", "-q", "-b", "line")
    for index in range(40):
        (repo / f"f{index:02}.txt").write_text("x" * 200 + "\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "more")
    spawned.clear()  # the setup's own git calls are not under test

    listing = run_isolated_git_readonly(str(repo), ["ls-tree", "-r", "-l", "-z", "HEAD"])
    prefix = run_isolated_git_readonly_prefix(
        str(repo),
        [
            "diff", "--patch-with-stat", "--no-color", "--no-ext-diff",
            "--no-textconv", "main...line", "--",
        ],
        limit=512,
    )

    assert listing.returncode == 0
    assert prefix.truncated and len(prefix.stdout) <= 512
    _assert_all_hardened(spawned)


@pytest.mark.parametrize(
    "args",
    [
        ["ls-files", "-v", "-z"],
        ["ls-files", "-z", "--others", "--exclude-per-directory=.gitignore", "--"],
        [
            "ls-files", "-z", "--others", "--ignored",
            "--exclude-per-directory=.gitignore", "--directory",
        ],
    ],
)
def test_r4_2_listing_forms_get_the_listing_cap(args: list[str]) -> None:
    assert (
        git_delivery._effective_output_limit(args, git_delivery._GIT_OUTPUT_LIMIT_BYTES)
        == git_delivery._GIT_LISTING_OUTPUT_LIMIT_BYTES
    )


def test_content_reads_keep_the_small_cap() -> None:
    assert (
        git_delivery._effective_output_limit(
            ["diff", "--binary", "HEAD"], git_delivery._GIT_OUTPUT_LIMIT_BYTES
        )
        == git_delivery._GIT_OUTPUT_LIMIT_BYTES
    )


def test_the_streamed_index_still_refuses_a_hidden_attributes_file(
    repo: Path, spawned
) -> None:
    """An ignored, untracked .gitattributes beside tracked files is refused."""
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "code.txt").write_text("code\n", encoding="utf-8")
    # A committed ignore rule: status never lists the attributes file.
    (repo / ".gitignore").write_text(".gitattributes\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "pkg")
    (sub / ".gitattributes").write_text("*.txt -text\n", encoding="utf-8")

    with pytest.raises(GitDeliveryError):
        capture_git_snapshot(str(repo))


def test_a_tracked_attributes_file_outside_the_changed_rows_is_fine(
    repo: Path, spawned
) -> None:
    """The streamed parse keeps every tracked .gitattributes, not only changed ones."""
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "code.txt").write_text("code\n", encoding="utf-8")
    (sub / ".gitattributes").write_text("*.txt text\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "pkg")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")

    snapshot = capture_git_snapshot(str(repo))
    assert len(snapshot.entries) == 1
