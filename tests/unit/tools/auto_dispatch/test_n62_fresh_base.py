"""N62: a new dispatch is not cut from a stale dispatch checkout.

F2 was dispatched after F1 merged while dispatch-repo still pointed at the
commit before it, and its worker re-created F1's file. The origin here is a
local bare repository: no network.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.tools.auto_dispatch.fresh_base import (
    BASE_REF_ENV,
    FRESH_BASE_MODE_ENV,
    StaleBaseCheckout,
    check_base,
)
from src.tools.auto_dispatch.gates import GateRunner, GateSettings
from src.tools.auto_dispatch.types import Dispatch, Line


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FRESH_BASE_MODE_ENV, raising=False)
    monkeypatch.delenv(BASE_REF_ENV, raising=False)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _identity(repo: Path) -> None:
    _git(repo, "config", "user.name", "Dispatch Test")
    _git(repo, "config", "user.email", "dispatch@example.invalid")


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


class _World:
    """A bare origin, a developer clone that merges, and the deployed checkout."""

    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True
        )
        self.dev = tmp_path / "dev"
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.dev)], check=True)
        _identity(self.dev)
        _git(self.dev, "checkout", "-q", "-b", "main")
        self.first = _commit(self.dev, "s01.txt")
        _git(self.dev, "push", "-q", "origin", "main")
        self.deployed = tmp_path / "dispatch-repo"
        subprocess.run(
            ["git", "clone", "-q", str(self.origin), str(self.deployed)], check=True
        )
        _identity(self.deployed)

    def merge_upstream(self, name: str = "f1.txt") -> str:
        sha = _commit(self.dev, name)
        _git(self.dev, "push", "-q", "origin", "main")
        return sha


def _prepared(tmp_path: Path, repo: Path) -> Dispatch:
    gate = GateRunner(
        GateSettings(
            runs_dir=tmp_path / "runs",
            repo_root=repo,
            worktrees_dir=tmp_path / "worktrees",
        )
    )
    line = Line(id="line-0", name="l", task="t", acceptance=("cmd: true",))
    dispatch = Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=[line])
    gate.prepare(dispatch)
    return dispatch


def test_enforce_bases_a_behind_checkout_on_origin_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _World(tmp_path)
    merged = world.merge_upstream()
    monkeypatch.setenv(FRESH_BASE_MODE_ENV, "enforce")

    dispatch = _prepared(tmp_path, world.deployed)

    assert dispatch.gates["base_commit"] == merged
    assert dispatch.lines[0].base_commit == merged
    check = dispatch.gates["base_check"]
    assert (check["status"], check["behind"], check["ahead"]) == ("behind", 1, 0)
    assert check["head"] == world.first and check["fetched"] is True
    # The checkout itself is not moved; only the dispatch base is.
    assert _git(world.deployed, "rev-parse", "HEAD") == world.first


def test_shadow_records_the_staleness_and_keeps_head(tmp_path: Path) -> None:
    world = _World(tmp_path)
    merged = world.merge_upstream()

    dispatch = _prepared(tmp_path, world.deployed)

    assert dispatch.gates["base_commit"] == world.first
    check = dispatch.gates["base_check"]
    assert check["mode"] == "shadow"
    assert check["status"] == "behind" and check["would"] == "base_on_ref"
    assert check["ref_commit"] == merged


@pytest.mark.asyncio
async def test_prepare_async_uses_the_same_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _World(tmp_path)
    merged = world.merge_upstream()
    monkeypatch.setenv(FRESH_BASE_MODE_ENV, "enforce")
    gate = GateRunner(
        GateSettings(runs_dir=tmp_path / "runs", repo_root=world.deployed,
                     worktrees_dir=tmp_path / "wt")
    )
    line = Line(id="line-0", name="l", task="t", acceptance=("cmd: true",))
    dispatch = Dispatch(id="d1", goal="g", user_id="u", created_at="now", lines=[line])

    await gate.prepare_async(dispatch)

    assert dispatch.lines[0].base_commit == merged


def test_enforce_refuses_a_diverged_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _World(tmp_path)
    world.merge_upstream()
    _commit(world.deployed, "local-only.txt")
    monkeypatch.setenv(FRESH_BASE_MODE_ENV, "enforce")

    with pytest.raises(StaleBaseCheckout, match="^base_checkout_diverged: ") as raised:
        _prepared(tmp_path, world.deployed)
    assert "Update the dispatch checkout to origin/main" in str(raised.value)


def test_enforce_refuses_when_the_fetch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _World(tmp_path)
    _git(world.deployed, "remote", "set-url", "origin", str(tmp_path / "gone.git"))

    head, record = check_base(world.deployed, mode="shadow")
    assert head == world.first and record["status"] == "fetch_failed"
    assert record["would"] == "refuse"

    monkeypatch.setenv(FRESH_BASE_MODE_ENV, "enforce")
    with pytest.raises(StaleBaseCheckout, match="^base_checkout_fetch_failed: "):
        check_base(world.deployed)


def test_ahead_or_current_keeps_head(tmp_path: Path) -> None:
    world = _World(tmp_path)
    head, record = check_base(world.deployed, mode="enforce")
    assert head == world.first and record["status"] == "current"
    local = _commit(world.deployed, "ahead.txt")
    head, record = check_base(world.deployed, mode="enforce")
    assert head == local and record["status"] == "ahead"


def test_no_remote_and_off_keep_head(tmp_path: Path) -> None:
    repo = tmp_path / "local"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _identity(repo)
    sha = _commit(repo, "a.txt")
    head, record = check_base(repo, mode="enforce")
    assert head == sha and record["status"] == "no_remote"
    assert check_base(repo, mode="off") == (sha, None)


def test_a_configured_ref_and_an_existing_base_are_respected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = _World(tmp_path)
    _git(world.dev, "checkout", "-q", "-b", "release")
    release = _commit(world.dev, "release.txt")
    _git(world.dev, "push", "-q", "origin", "release")
    monkeypatch.setenv(FRESH_BASE_MODE_ENV, "enforce")
    monkeypatch.setenv(BASE_REF_ENV, "origin/release")

    dispatch = _prepared(tmp_path, world.deployed)
    assert dispatch.gates["base_commit"] == release

    # A dispatch that already has its base (a replan) keeps it: no new check.
    world.merge_upstream("later.txt")
    gate = GateRunner(
        GateSettings(runs_dir=tmp_path / "runs", repo_root=world.deployed,
                     worktrees_dir=tmp_path / "wt")
    )
    gate.prepare(dispatch)
    assert dispatch.gates["base_commit"] == release
