"""L2 review fixes: an isolated push, the token only for github https, and
credential-free workspace remotes.

Every push goes to a bare repository in ``tmp_path``; nothing leaves the host.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import dispatch_workspaces as workspace_db
from src.server.services import dispatch_workspaces as workspaces
from src.server.services.dispatch_delivery import DispatchDelivery, default_remote
from src.tools.auto_dispatch.approval import FileApprovalGate
from src.tools.auto_dispatch.execution import GitMerger
from src.tools.auto_dispatch.protocols import MergeConflictError
from src.tools.auto_dispatch.types import Dispatch, DispatchState, Line, LineStatus

pytestmark = pytest.mark.asyncio

BRANCH = "integrate/auto-dispatch-d-iso"
WORKSPACE_ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture(autouse=True)
def _own_objects(monkeypatch):
    monkeypatch.delenv("GIT_OBJECT_DIRECTORY", raising=False)
    monkeypatch.delenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", raising=False)


def _git(cwd: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


@pytest.fixture
def hostile(tmp_path: Path) -> dict[str, Path]:
    """A checkout whose own config tries to redirect and authenticate a push."""
    repo, remote, evil = tmp_path / "repo", tmp_path / "remote.git", tmp_path / "evil.git"
    marker = tmp_path / "helper-ran"
    repo.mkdir()
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(evil))
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "d@example.com")
    _git(repo, "config", "user.name", "D")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "a")
    _git(repo, "branch", BRANCH)
    _git(repo, "remote", "add", "origin", str(remote))
    # What a Worker could write into the shared .git/config.
    _git(repo, "config", f"url.{evil}.pushInsteadOf", str(remote))
    _git(repo, "config", "credential.helper", f"!touch {marker}")
    _git(repo, "config", "http.proxy", "http://127.0.0.1:9")
    _git(repo, "config", "core.sshCommand", f"touch {marker}; false")
    included = tmp_path / "included.cfg"
    included.write_text(f"[url \"{evil}\"]\n\tinsteadOf = {remote}\n", encoding="utf-8")
    _git(repo, "config", "include.path", str(included))
    return {"repo": repo, "remote": remote, "evil": evil, "marker": marker}


async def _approved(gate: FileApprovalGate) -> Dispatch:
    dispatch = Dispatch(
        id="d-iso",
        goal="g",
        user_id="u",
        created_at="now",
        integrate_branch=BRANCH,
        lines=[Line(id="l", name="l", task="t", status=LineStatus.PASSED)],
    )
    token = await gate.request_approval(dispatch)
    await gate.resolve(token, True)
    dispatch.approval_token = token
    dispatch.state = DispatchState.COMPLETE
    return dispatch


def _branches(bare: Path) -> str:
    return _git(bare, "branch", "--list", BRANCH)


async def test_a_hostile_repo_config_does_not_redirect_the_push(tmp_path, hostile) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(gate)
    merger = GitMerger(hostile["repo"], dispatch, gate=gate, worktrees_dir=tmp_path / "wt")

    await merger.push(BRANCH, dispatch.approval_token)

    assert _branches(hostile["remote"]), "the push reached the registered remote"
    assert not _branches(hostile["evil"]), "insteadOf/include from the checkout was honoured"
    assert not hostile["marker"].exists(), "a repo credential helper or sshCommand ran"
    assert _git(hostile["repo"], "branch", "--show-current") == "main"


async def test_an_explicit_url_push_ignores_the_checkout_config_too(tmp_path, hostile) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(gate)
    merger = GitMerger(hostile["repo"], dispatch, gate=gate, worktrees_dir=tmp_path / "wt")

    await merger.push(BRANCH, dispatch.approval_token, remote=str(hostile["remote"]))

    assert _branches(hostile["remote"]) and not _branches(hostile["evil"])


async def test_a_credential_is_refused_for_anything_but_github_https(tmp_path, hostile) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(gate)
    merger = GitMerger(hostile["repo"], dispatch, gate=gate, worktrees_dir=tmp_path / "wt")
    header = {"http.https://github.com/.extraheader": "AUTHORIZATION: basic eA=="}

    with pytest.raises(MergeConflictError, match="credential"):
        await merger.push(
            BRANCH, dispatch.approval_token, remote=str(hostile["remote"]), auth_config=header
        )
    assert not _branches(hostile["remote"])


async def test_an_ssh_github_remote_gets_no_token(tmp_path) -> None:
    gate = FileApprovalGate(tmp_path / "proposed")
    dispatch = await _approved(gate)
    seen: list[dict] = []

    async def pusher(_dispatch, **kwargs):
        seen.append(kwargs)

    async def remote_for(_dispatch, _root):
        remote = "git@github.com:acme/widgets.git"
        return remote, remote

    class PRs:
        async def open(self, **_kwargs):
            return {"number": 1, "url": "https://github.com/acme/widgets/pull/1"}

    async def reveal(_user, _name):
        return "ghp_" "L2SSHNOTOKEN0123456789"

    await DispatchDelivery(
        gate=gate,
        repo_root_for=lambda _d: tmp_path,
        worktrees_dir_for=lambda _d: tmp_path / "wt",
        reveal=reveal,
        pull_requests=PRs(),
        pusher=pusher,
        remote_for=remote_for,
        base_branch="main",
    ).deliver(dispatch)

    (call,) = seen
    assert call["remote"] == "git@github.com:acme/widgets.git"
    assert call["auth"] == {}


async def test_delivery_pushes_to_the_remote_snapshotted_at_start(tmp_path) -> None:
    dispatch = Dispatch(id="d", goal="g", user_id="u", created_at="now")
    dispatch.workspace_id = WORKSPACE_ID
    dispatch.gates = {"workspace_remote": "https://github.com/acme/snap.git"}
    remote, _origin = await default_remote(dispatch, tmp_path)
    assert remote == "https://github.com/acme/snap.git"


async def test_start_snapshots_the_workspace_remote_without_its_credential(
    tmp_path, monkeypatch
) -> None:
    from src.server.services.dispatch import DispatchService, DispatchSettings
    from src.tools.auto_dispatch import InMemoryDispatchStore

    monkeypatch.setattr("src.server.services.dispatch._schedule", lambda key, factory: None)
    store = InMemoryDispatchStore()
    service = DispatchService(
        DispatchSettings(proposed_dir=tmp_path / "p", runs_dir=tmp_path / "runs"),
        store=store,
        gate=FileApprovalGate(tmp_path / "p"),
    )
    row = await service.start(
        "g",
        "u",
        workspace={
            "workspace_id": WORKSPACE_ID,
            "repo_root": str(tmp_path),
            "git_remote": "https://bob:ghp_" "SNAPSECRET@github.com/acme/snap.git",
        },
    )
    stored = await store.get(row.id)
    assert stored.gates["workspace_remote"] == "https://github.com/acme/snap.git"
    assert "ghp_" "SNAPSECRET" not in str(stored.to_dict())


async def test_the_restart_checklist_is_bounded(tmp_path, monkeypatch) -> None:
    import src.server.services.dispatch as module
    from src.server.services.dispatch import DispatchService, DispatchSettings
    from src.tools.auto_dispatch import InMemoryDispatchStore

    monkeypatch.setattr(module, "_RESTART_CHECKED_LIMIT", 4, raising=False)
    service = DispatchService(
        DispatchSettings(proposed_dir=tmp_path / "p", runs_dir=tmp_path / "runs"),
        store=InMemoryDispatchStore(),
        gate=FileApprovalGate(tmp_path / "p"),
    )

    async def no_drive(dispatch_id, owner=None):
        return None

    service._drive = no_drive  # type: ignore[method-assign]
    for index in range(20):
        await service._recover_drive(f"d{index}")
    assert len(service._restart_checked) <= 4


# -- workspace remotes ----------------------------------------------------------


SECRET_REMOTE = "https://bob:ghp_" "WSSECRET0123456789@github.com/acme/widgets.git"


def _row(path: str, remote: str) -> dict:
    return {
        "workspace_id": WORKSPACE_ID,
        "user_id": "u1",
        "name": "demo",
        "path": path,
        "git_remote": remote,
        "sandbox_policy": "",
        "default_worker": "",
    }


async def test_a_remote_credential_is_stripped_on_write_and_on_read(tmp_path, monkeypatch) -> None:
    created = AsyncMock(side_effect=lambda **fields: {**_row(str(tmp_path), ""), **fields})
    monkeypatch.setattr(workspace_db, "create_workspace", created)
    monkeypatch.setattr(
        workspace_db, "list_workspaces", AsyncMock(return_value=[_row(str(tmp_path), SECRET_REMOTE)])
    )

    row = await workspaces.create_workspace(
        user_id="u1", name="demo", path=str(tmp_path), git_remote=SECRET_REMOTE
    )
    listed = await workspaces.list_workspaces("u1")

    assert created.await_args.kwargs["git_remote"] == "https://github.com/acme/widgets.git"
    assert row["git_remote"] == "https://github.com/acme/widgets.git"
    assert listed[0]["git_remote"] == "https://github.com/acme/widgets.git"
    assert "WSSECRET" not in str(row) + str(listed)


async def test_fetch_output_is_redacted(tmp_path, monkeypatch) -> None:
    class Process:
        returncode = 1

        async def communicate(self):
            return b"", f"fatal: unable to access '{SECRET_REMOTE}': 403".encode()

    async def fake_exec(*argv, **kwargs):
        return Process()

    _git(tmp_path, "init", "-q", "-b", "main")
    monkeypatch.setattr(
        workspace_db,
        "get_workspace_by_id",
        AsyncMock(return_value=_row(str(tmp_path), "https://github.com/acme/widgets.git")),
    )
    monkeypatch.setattr(workspaces.asyncio, "create_subprocess_exec", fake_exec)

    result = await workspaces.fetch_before_start(WORKSPACE_ID, timeout=5)

    assert result["fetched"] is False
    assert "WSSECRET" not in result["stderr"]
    assert "403" in result["stderr"]
