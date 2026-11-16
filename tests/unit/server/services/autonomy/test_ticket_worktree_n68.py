"""N68: Ticket worktree follow-ups from the R3-2 final review (server side).

Real tmp git repositories; no database, no network.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import tickets as ticket_db
from src.server.services.autonomy import ticket_worktree as tw
from src.server.services.autonomy.ticket_worktree import (
    ensure_ticket_worktree,
    remove_ticket_worktree,
)
from tests.unit.server.services.autonomy.test_ticket_worktree_cleanup import (
    TICKET_ID,
    _git,
    _repo,
)


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(tmp_path / "worktrees"))


def _commit(path: Path, name: str, text: str) -> str:
    (path / name).write_text(text, encoding="utf-8")
    _git(path, "add", name)
    _git(path, "-c", "user.name=w", "-c", "user.email=w@example.invalid", "commit", "-qm", name)
    return _git(path, "rev-parse", "HEAD")


def _recovered(record) -> list[Path]:
    root = record.path.parents[2] / "recovery-artifacts"
    return sorted(root.iterdir()) if root.exists() else []


@pytest.mark.asyncio
async def test_detached_head_checkout_is_kept_not_moved_aside(tmp_path: Path) -> None:
    """Moving it aside, then pruning, dropped the only ref to its commits."""
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "wt"
    record = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    _git(record.path, "switch", "-q", "--detach")
    detached = _commit(record.path, "detached.txt", "only on the detached HEAD\n")

    with pytest.raises(tw.TicketCheckoutDetached) as caught:
        await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    assert caught.value.head_sha == detached
    assert "git switch" in str(caught.value)
    with pytest.raises(tw.TicketCheckoutDetached):
        await remove_ticket_worktree(record)

    assert _git(record.path, "rev-parse", "HEAD") == detached
    assert _recovered(record) == []
    assert f"worktree {record.path.resolve()}" in _git(repo, "worktree", "list", "--porcelain")


@pytest.mark.asyncio
async def test_checkout_switched_to_another_tickets_branch_frees_it(tmp_path: Path) -> None:
    """A Worker that checks out another Ticket's branch must not block that
    Ticket forever: reuse and removal move the checkout aside."""
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "wt"
    other = "conductor/ticket/other"
    _git(repo, "branch", other, base)
    record = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    work = _commit(record.path, "mine.txt", "mine\n")
    _git(record.path, "switch", "-q", other)
    (record.path / "wip.txt").write_text("wip\n", encoding="utf-8")

    again = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    assert _git(again.path, "branch", "--show-current") == record.branch
    assert _git(again.path, "rev-parse", "HEAD") == work
    assert [p.name for p in _recovered(record)][0].endswith(tuple("0123456789Z"))
    assert any((p / "wip.txt").exists() for p in _recovered(record))
    # The other Ticket's branch is free again.
    scratch = tmp_path / "other-checkout"
    _git(repo, "worktree", "add", "-q", str(scratch), other)

    _git(scratch, "switch", "-q", "--detach")
    _git(again.path, "switch", "-q", other)
    moved = await remove_ticket_worktree(again)
    assert moved is not None and moved.exists()
    assert not again.path.exists()
    assert _git(repo, "rev-parse", record.branch) == work


@pytest.mark.asyncio
async def test_half_removed_checkout_is_not_reused(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "wt"
    record = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    work = _commit(record.path, "w.txt", "w\n")
    (record.path / "left.txt").write_text("left\n", encoding="utf-8")
    (record.path / ".git").unlink()  # removal killed right after this

    # Removal retry: the prune dropped the registration; move the rest aside.
    moved = await remove_ticket_worktree(record)
    assert moved is not None and (moved / "left.txt").exists()
    assert not record.path.exists()
    assert _git(repo, "rev-parse", record.branch) == work


@pytest.mark.asyncio
async def test_removal_never_moves_a_path_that_is_not_a_ticket_checkout(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path / "git")
    user_dir = tmp_path / "someone" / "precious"
    user_dir.mkdir(parents=True)
    (user_dir / "keep.txt").write_text("keep\n", encoding="utf-8")
    record = tw.TicketWorktreeRecord(
        path=user_dir, branch="conductor/ticket/x", base_sha="0" * 40, repo=repo
    )
    with pytest.raises(tw.TicketWorktreeError):
        await remove_ticket_worktree(record)
    assert (user_dir / "keep.txt").exists()


@pytest.mark.asyncio
async def test_half_removed_checkout_with_admin_intact_is_recreated(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    root = tmp_path / "wt"
    record = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    work = _commit(record.path, "w.txt", "w\n")
    (record.path / ".git").unlink()
    (record.path / ".git").mkdir()  # not a gitfile: never treated as usable

    again = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    assert (again.path / ".git").is_file()
    assert _git(again.path, "rev-parse", "HEAD") == work


@pytest.mark.asyncio
async def test_worktree_add_gets_its_own_longer_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    seen: dict[str, float | None] = {}
    real_run = tw._run_git

    async def recording(argv, **kwargs):
        seen[" ".join(argv[:2])] = kwargs.get("timeout")
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(tw, "_run_git", recording)
    await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=tmp_path / "wt")
    assert seen["worktree add"] == tw._GIT_ADD_TIMEOUT_SECONDS > tw._GIT_TIMEOUT_SECONDS
    assert seen["worktree prune"] is None


def test_stale_cleanup_notes_are_capped() -> None:
    notes: list = []
    for index in range(ticket_db.MAX_STALE_CLEANUP_NOTES + 5):
        notes = ticket_db.capped_stale_cleanup_notes(notes, {"n": index})
    assert len(notes) == ticket_db.MAX_STALE_CLEANUP_NOTES
    assert notes[-1] == {"n": ticket_db.MAX_STALE_CLEANUP_NOTES + 4}
    assert notes[0] == {"n": 5}
    assert ticket_db.capped_stale_cleanup_notes(None, {"n": 0}) == [{"n": 0}]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["unavailable", "lock_timeout"])
async def test_unbound_launch_requeues_are_capped_and_lock_timeout_is_transient(
    monkeypatch: pytest.MonkeyPatch, error: str
) -> None:
    from src.server.services.autonomy import worker
    from src.server.services.projects.dispatch import DispatchError

    exc = (
        tw.TicketWorktreeUnavailable("git timed out")
        if error == "unavailable"
        else TimeoutError("ticket worktree lock busy")
    )
    monkeypatch.setattr(worker, "bind_ticket_worktree", AsyncMock(side_effect=exc))
    requeue = AsyncMock()
    monkeypatch.setattr(worker, "_requeue_unbound_launch", requeue)
    monkeypatch.setattr(worker, "_UNBOUND_LAUNCH_RETRIES", {})
    ticket = {
        "ticket_id": "T-N68",
        "user_id": "u",
        "worker_provider": "codex",
        "status": "running",
        "mandate": {},
    }
    for _ in range(worker._MAX_UNBOUND_LAUNCH_RETRIES):
        with pytest.raises(worker._RetryableLaunch):
            await worker._bind_launch_checkout(None, ticket, ticket, "T-N68", "thread-1")
    with pytest.raises(DispatchError, match="after"):
        await worker._bind_launch_checkout(None, ticket, ticket, "T-N68", "thread-1")
    assert requeue.await_count == worker._MAX_UNBOUND_LAUNCH_RETRIES
