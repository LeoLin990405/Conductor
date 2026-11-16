"""Dispatch workspace service: path checks, repo lookup and the pre-start fetch."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import dispatch_workspaces as workspace_db
from src.server.services import dispatch_workspaces as workspaces

pytestmark = pytest.mark.asyncio

WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _isolate_git_env(monkeypatch):
    """The dispatch harness points git at shared object stores; a test repo
    must own its objects, or a bare remote looks empty to ``upload-pack``."""
    monkeypatch.delenv("GIT_OBJECT_DIRECTORY", raising=False)
    monkeypatch.delenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", raising=False)


def _git(*args: str, cwd=None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _repo(tmp_path, *, remote: str | None = None):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-q", "-m", "initial", cwd=repo)
    if remote is not None:
        _git("remote", "add", "origin", remote, cwd=repo)
        if Path(remote).exists():
            _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=repo)
    return repo


def _row(path: str, *, remote: str = "") -> dict:
    return {
        "workspace_id": WORKSPACE_ID,
        "user_id": "u1",
        "name": "demo",
        "path": path,
        "git_remote": remote,
        "sandbox_policy": "workspace-write",
        "default_worker": "codex",
        "created_at": "2026-11-13T00:00:00+00:00",
        "updated_at": "2026-11-13T00:00:00+00:00",
    }


async def test_normalize_path_requires_an_existing_directory(tmp_path):
    with pytest.raises(workspaces.WorkspaceInvalid):
        workspaces.normalize_path(str(tmp_path / "missing"))
    file = tmp_path / "file"
    file.write_text("x", encoding="utf-8")
    with pytest.raises(workspaces.WorkspaceInvalid):
        workspaces.normalize_path(str(file))
    resolved = workspaces.normalize_path(str(tmp_path))
    assert resolved == str(tmp_path.resolve())


async def test_create_discovers_the_origin_remote(tmp_path, monkeypatch):
    repo = _repo(tmp_path, remote="git@example.com:demo.git")
    created = AsyncMock(return_value=_row(str(repo), remote="git@example.com:demo.git"))
    monkeypatch.setattr(workspace_db, "create_workspace", created)

    row = await workspaces.create_workspace(user_id="u1", name="demo", path=str(repo))

    assert row["name"] == "demo"
    fields = created.await_args.kwargs
    assert fields["git_remote"] == "git@example.com:demo.git"
    assert fields["path"] == str(repo.resolve())


async def test_create_maps_a_taken_name(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr(
        workspace_db,
        "create_workspace",
        AsyncMock(side_effect=workspace_db.WorkspaceNameTaken("demo")),
    )
    with pytest.raises(workspaces.WorkspaceNameTaken):
        await workspaces.create_workspace(user_id="u1", name="demo", path=str(repo))


async def test_workspace_repo_falls_back_to_the_checkout_remote(tmp_path, monkeypatch):
    repo = _repo(tmp_path, remote="git@example.com:demo.git")
    monkeypatch.setattr(
        workspace_db, "get_workspace_by_id", AsyncMock(return_value=_row(str(repo)))
    )

    resolved = await workspaces.workspace_repo(WORKSPACE_ID)

    assert resolved["repo_root"] == str(repo.resolve())
    assert resolved["git_remote"] == "git@example.com:demo.git"


async def test_fetch_before_start_runs_git_fetch(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    _git("init", "--bare", "-q", "-b", "main", str(remote))
    repo = _repo(tmp_path, remote=str(remote))
    _git("fetch", "-q", "origin", cwd=repo)
    before = _git("rev-parse", "refs/remotes/origin/main", cwd=repo).stdout.strip()
    # Someone else pushes to the remote; the checkout's origin/main is now stale.
    other = tmp_path / "other"
    _git("clone", "-q", str(remote), str(other))
    _git("config", "user.email", "test@example.com", cwd=other)
    _git("config", "user.name", "Test", cwd=other)
    (other / "NEW.md").write_text("new\n", encoding="utf-8")
    _git("add", "NEW.md", cwd=other)
    _git("commit", "-q", "-m", "upstream", cwd=other)
    _git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=other)
    upstream = _git("rev-parse", "HEAD", cwd=other).stdout.strip()
    monkeypatch.setattr(
        workspace_db,
        "get_workspace_by_id",
        AsyncMock(return_value=_row(str(repo), remote=str(remote))),
    )

    result = await workspaces.fetch_before_start(WORKSPACE_ID, timeout=30)

    assert result["fetched"] is True, result
    assert result["returncode"] == 0
    assert result["remote"] == str(remote)
    after = _git("rev-parse", "refs/remotes/origin/main", cwd=repo).stdout.strip()
    assert before != upstream
    assert after == upstream, "fetch-before-start must move origin/main"


async def test_fetch_before_start_reports_an_exit_failure(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    missing = tmp_path / "missing.git"
    monkeypatch.setattr(
        workspace_db,
        "get_workspace_by_id",
        AsyncMock(return_value=_row(str(repo), remote=str(missing))),
    )

    result = await workspaces.fetch_before_start(WORKSPACE_ID, timeout=30)

    assert result["fetched"] is False
    assert result["returncode"] not in (0, None)
    assert result["stderr"]


async def test_create_refuses_an_option_like_remote(tmp_path, monkeypatch):
    """A remote is never a Git option: ``--upload-pack`` could run a command."""
    repo = _repo(tmp_path)
    created = AsyncMock()
    monkeypatch.setattr(workspace_db, "create_workspace", created)

    with pytest.raises(workspaces.WorkspaceInvalid):
        await workspaces.create_workspace(
            user_id="u1",
            name="demo",
            path=str(repo),
            git_remote="--upload-pack=touch /tmp/pwned",
        )

    created.assert_not_awaited()


async def test_fetch_before_start_refuses_an_option_like_remote(tmp_path, monkeypatch):
    """An older row with a flag-shaped remote must not become executable."""
    repo = _repo(tmp_path)
    marker = tmp_path / "pwned"
    monkeypatch.setattr(
        workspace_db,
        "get_workspace_by_id",
        AsyncMock(
            return_value=_row(
                str(repo), remote=f"--upload-pack=touch {marker}"
            )
        ),
    )

    result = await workspaces.fetch_before_start(WORKSPACE_ID, timeout=30)

    assert result["fetched"] is False
    assert "starts with '-'" in result["stderr"]
    assert not marker.exists()


async def test_fetch_before_start_terminates_option_parsing(tmp_path, monkeypatch):
    """``--`` is between ``fetch`` and the remote, not just before ``--prune``."""
    seen: list[tuple[str, ...]] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*argv, **kwargs):
        del kwargs
        seen.append(tuple(argv))
        return Process()

    repo = _repo(tmp_path)
    remote = "git@example.com:demo.git"
    monkeypatch.setattr(
        workspace_db,
        "get_workspace_by_id",
        AsyncMock(return_value=_row(str(repo), remote=remote)),
    )
    monkeypatch.setattr(workspaces.asyncio, "create_subprocess_exec", fake_exec)

    result = await workspaces.fetch_before_start(WORKSPACE_ID, timeout=30)

    assert result["fetched"] is True
    fetch = seen[-1]
    assert fetch[0] == "git"
    tail = fetch[fetch.index("-C"):]
    # The remote is an operand after ``--``; a remote that is not origin is
    # fetched with an explicit refspec so its tracking refs move.
    assert tail == (
        "-C",
        str(repo.resolve()),
        "fetch",
        "--prune",
        "--no-recurse-submodules",
        "--",
        remote,
        "+refs/heads/*:refs/remotes/dispatch-workspace/*",
    )
    assert "core.hooksPath=/dev/null" in fetch
