"""Linked Ticket worktree Git metadata mounts for the Docker Worker runtime.

These tests only inspect the argv ``build_container_argv`` produces. Nothing
here starts a Docker engine or a container.
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from src.server.services.autonomy import docker_worker_runtime as runtime


def _settings(**overrides):
    settings = runtime.DockerWorkerSettings.from_env(
        {"AUTONOMY_CLOUD_WORKER_DOCKER_BIN": "/bin/sh"}
    )
    return replace(settings, **overrides)


def _argv(workspace: str, **overrides):
    kwargs = {
        "name": "worker-1",
        "workspace": workspace,
        "child_argv": ("codex", "exec", "-"),
        "env_vars": {},
    }
    kwargs.update(overrides)
    return runtime.build_container_argv(_settings(), **kwargs)


def _bind_mounts(argv: tuple[str, ...]) -> list[str]:
    return [argv[index + 1] for index, item in enumerate(argv) if item == "--mount"]


def _make_linked_worktree(
    tmp_path: Path, *, relative_paths: bool = False, branch: str = "conductor/ticket/demo"
) -> tuple[Path, Path, Path]:
    """Create a real linked worktree; return (worktree, gitdir, common_dir)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        env=env,
    )
    (repo / "README").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README"],
        check=True,
        env=env,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
        check=True,
        env=env,
    )
    worktree = tmp_path / "ticket-wt"
    add_argv = ["git", "-C", str(repo), "worktree", "add", "-q"]
    if relative_paths:
        add_argv.append("--relative-paths")
    add_argv.extend(["-b", branch, str(worktree)])
    subprocess.run(add_argv, check=True, env=env)
    pointer = (worktree / ".git").read_text(encoding="utf-8").strip()
    assert pointer.startswith("gitdir: ")
    raw_gitdir = Path(pointer[len("gitdir: ") :])
    gitdir = (
        raw_gitdir.resolve()
        if raw_gitdir.is_absolute()
        else (worktree / raw_gitdir).resolve()
    )
    common_record = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    common_dir = (gitdir / common_record).resolve()
    return worktree, gitdir, common_dir


def test_linked_worktree_git_mounts_returns_common_and_gitdir(tmp_path: Path):
    worktree, gitdir, common_dir = _make_linked_worktree(tmp_path)
    gitdir_text = os.path.normpath(
        (worktree / ".git").read_text(encoding="utf-8").strip()[len("gitdir: ") :]
    )

    mounts = runtime.linked_worktree_git_mounts(worktree)
    common_from_gitdir = os.path.normpath(
        str(
            Path(gitdir_text)
            / (gitdir / "commondir").read_text(encoding="utf-8").strip()
        )
    )

    assert mounts == (
        (str(common_dir), common_from_gitdir),
        (str(gitdir), gitdir_text),
    )
    # Relative ``commondir`` must still land on the common dir from the gitdir
    # target spelling Git opens inside the container.
    assert Path(common_from_gitdir).resolve() == common_dir


def test_container_argv_bind_mounts_linked_worktree_git_metadata(tmp_path: Path):
    worktree, gitdir, common_dir = _make_linked_worktree(tmp_path)
    expected = runtime.linked_worktree_git_mounts(worktree)

    argv = _argv(str(worktree))
    mounts = _bind_mounts(argv)

    assert mounts[0] == f"type=bind,source={worktree},target=/workspace"
    assert mounts[1:] == [
        f"type=bind,source={source},target={target}"
        for source, target in expected
    ]
    assert (str(common_dir), expected[0][1]) == expected[0]
    assert expected[1] == (
        str(gitdir),
        os.path.normpath(
            (worktree / ".git").read_text(encoding="utf-8").strip()[len("gitdir: ") :]
        ),
    )
    # Workspace + common + gitdir — never a docker.sock or host home mount.
    assert len(mounts) == 3
    assert "/var/run/docker.sock" not in " ".join(argv)


def test_container_argv_marks_git_metadata_readonly_for_speculative_runs(
    tmp_path: Path,
):
    worktree, _gitdir, _common_dir = _make_linked_worktree(tmp_path)
    expected = runtime.linked_worktree_git_mounts(worktree)

    mounts = _bind_mounts(_argv(str(worktree), read_only_workspace=True))

    assert mounts == [
        f"type=bind,source={worktree},target=/workspace,readonly",
        *[
            f"type=bind,source={source},target={target},readonly"
            for source, target in expected
        ],
    ]


def test_plain_workspace_gets_no_git_metadata_mounts(tmp_path: Path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "README").write_text("no git\n", encoding="utf-8")

    mounts = _bind_mounts(_argv(str(stage)))

    assert mounts == [f"type=bind,source={stage},target=/workspace"]
    assert runtime.linked_worktree_git_mounts(stage) == ()


def test_embedded_git_directory_needs_no_extra_mounts(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        env=env,
    )

    assert (repo / ".git").is_dir()
    assert runtime.linked_worktree_git_mounts(repo) == ()
    assert _bind_mounts(_argv(str(repo))) == [
        f"type=bind,source={repo},target=/workspace"
    ]


def test_broken_linked_worktree_pointer_fails_closed(tmp_path: Path):
    worktree = tmp_path / "broken"
    worktree.mkdir()
    (worktree / ".git").write_text(
        f"gitdir: {tmp_path / 'missing-gitdir'}\n", encoding="utf-8"
    )

    with pytest.raises(runtime.DockerWorkerRuntimeError, match="could not be mounted"):
        runtime.linked_worktree_git_mounts(worktree)
    with pytest.raises(runtime.DockerWorkerRuntimeError, match="could not be mounted"):
        _argv(str(worktree))


def test_fifo_git_metadata_fails_closed_without_blocking(tmp_path: Path):
    """A FIFO planted as ``commondir`` must not hang the launch path."""
    worktree, gitdir, _common = _make_linked_worktree(tmp_path)
    planted = gitdir / "commondir"
    planted.unlink()
    os.mkfifo(planted)

    started = time.monotonic()
    with pytest.raises(runtime.DockerWorkerRuntimeError, match="could not be mounted"):
        runtime.linked_worktree_git_mounts(worktree)
    assert time.monotonic() - started < 1.0

    started = time.monotonic()
    with pytest.raises(runtime.DockerWorkerRuntimeError, match="could not be mounted"):
        _argv(str(worktree))
    assert time.monotonic() - started < 1.0


def test_relative_gitdir_pointer_mounts_at_container_paths(tmp_path: Path):
    worktree, gitdir, common_dir = _make_linked_worktree(
        tmp_path, relative_paths=True, branch="conductor/ticket/relative"
    )
    pointer_text = (worktree / ".git").read_text(encoding="utf-8").strip()
    raw_gitdir = Path(pointer_text[len("gitdir: ") :])
    assert not raw_gitdir.is_absolute()
    gitdir_target = os.path.normpath(
        str(Path(runtime._WORKSPACE_MOUNT).joinpath(*raw_gitdir.parts))
    )
    common_record = (gitdir / "commondir").read_text(encoding="utf-8").strip()
    common_target = os.path.normpath(str(Path(gitdir_target) / common_record))

    expected = runtime.linked_worktree_git_mounts(worktree)
    mounts = _bind_mounts(_argv(str(worktree)))

    assert expected == (
        (str(common_dir), common_target),
        (str(gitdir), gitdir_target),
    )
    assert mounts == [
        f"type=bind,source={worktree},target=/workspace",
        f"type=bind,source={common_dir},target={common_target}",
        f"type=bind,source={gitdir},target={gitdir_target}",
    ]
    # In-container Git resolves the relative pointer against /workspace.
    assert gitdir_target.startswith("/")
    assert not gitdir_target.startswith(str(worktree))
    assert Path(gitdir_target).is_absolute()
