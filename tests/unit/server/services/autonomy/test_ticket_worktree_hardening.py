"""Ticket worktree hardening (N17 review round 2), line S.

Real tmp git repositories plus the in-memory persistence fake from
``test_ticket_worktree_cleanup``; no database, no network.

Each test pins one round-2 finding that the round-1 services got wrong:
removal deleted the Ticket branch (the delivery), server Git ran inside the
checkout and followed its worker-writable ``.git`` file (so a planted
fsmonitor, hook or filter ran as the server), a reopened Ticket wedged on its
own pending cleanup, removal did not wait for the Worker's processes, an
orphaned checkout ignored the old server run, a daemon hand-over cleared
bindings unfenced, and the retry sweep ignored the attempt cap.
"""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.server.services.autonomy import ownership
from src.server.services.autonomy import ticket_worktree as tw
from src.server.services.autonomy.ticket_worktree import (
    TicketWorktreeError,
    ensure_ticket_worktree,
    prepare_server_ticket_worktree,
    release_server_ticket_worktree,
    remove_ticket_worktree,
    sweep_ticket_worktree_cleanups,
)
from tests.unit.server.services.autonomy.test_ticket_worktree_cleanup import (
    TICKET_ID,
    FakeTicketDb,
    _git,
    _repo,
    _row,
)


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setattr(tw, "_thread_has_active_run", AsyncMock(return_value=False))
    monkeypatch.setattr(tw, "_stop_verification", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "src.server.database.autonomy.lines.list_ticket_events", AsyncMock(return_value=[])
    )


def _reassigned_from_server(
    monkeypatch: pytest.MonkeyPatch, thread_id: str | None = None
) -> None:
    """The audit event every reassignment away from the server writes."""
    detail = {tw.REASSIGN_FROM_THREAD_KEY: thread_id} if thread_id else {}
    monkeypatch.setattr(
        "src.server.database.autonomy.lines.list_ticket_events",
        AsyncMock(
            return_value=[
                {"action": "reassigned", "from_owner": "cloud", "detail": detail}
            ]
        ),
    )


def _no_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tw, "_checkout_process_ids", AsyncMock(return_value=set()), raising=False
    )


@pytest_asyncio.fixture
async def bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path / "git")
    fake = FakeTicketDb(_row(repo))
    monkeypatch.setattr(tw, "ticket_db", fake)
    await prepare_server_ticket_worktree(fake.row())
    assert fake.row()["worktree_generation"] == 1
    return fake, repo, Path(fake.row()["worktree_path"])


def _commit(path: Path, name: str, text: str) -> str:
    (path / name).write_text(text, encoding="utf-8")
    _git(path, "add", name)
    _git(path, "commit", "-qm", f"worker {name}")
    return _git(path, "rev-parse", "HEAD")


# ------------------------------------------------- 1. the branch survives


@pytest.mark.asyncio
async def test_release_keeps_the_ticket_branch_and_its_commits(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """probe_branch_loss: a completed Ticket's commits must survive close."""
    _no_processes(monkeypatch)
    fake, repo, path = bound
    base = fake.row()["worktree_base_sha"]
    branch = fake.row()["worktree_branch"]
    tip = _commit(path, "work.txt", "delivered\n")
    assert tip != base
    fake.row()["status"] = "completed"

    assert await release_server_ticket_worktree(dict(fake.row()), reason="close") == "removed"

    assert not path.exists()
    assert _git(repo, "rev-parse", "--verify", f"refs/heads/{branch}") == tip
    assert _git(repo, "rev-list", "--count", f"{base}..{branch}") == "1"


@pytest.mark.asyncio
async def test_remove_keeps_the_branch_even_when_dirty(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    record = await ensure_ticket_worktree(
        repo, TICKET_ID, base_sha=base, root=tmp_path / "wt"
    )
    tip = _commit(record.path, "a.txt", "a\n")
    (record.path / "untracked.txt").write_text("wip\n", encoding="utf-8")

    artifact = await remove_ticket_worktree(record)

    assert artifact is not None and artifact.exists()
    assert not record.path.exists()
    assert _git(repo, "rev-parse", "--verify", f"refs/heads/{record.branch}") == tip


@pytest.mark.asyncio
async def test_reopened_ticket_reattaches_its_kept_branch(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keeping the branch must not wedge the next attempt's create."""
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    tip = _commit(path, "work.txt", "attempt 1\n")
    fake.row()["status"] = "cancelled"
    assert await release_server_ticket_worktree(dict(fake.row()), reason="stop") == "removed"

    fake.row()["status"] = "running"
    again = await prepare_server_ticket_worktree(fake.row())

    assert Path(again["worktree_path"]) == path
    assert _git(path, "rev-parse", "HEAD") == tip
    assert again["worktree_base_sha"] != tip  # the pin is never redefined


# ------------------------------------------- 2. hardened checkout git calls


def _plant_evil_gitdir(worktree: Path, branch: str, marker: Path) -> None:
    """What a sandboxed Worker can do inside its writable checkout."""
    evil = worktree / ".evil"
    _git(worktree, "init", "-q", "--bare", str(evil))
    (evil / "HEAD").write_text(f"ref: refs/heads/{branch}\n", encoding="utf-8")
    hook = worktree / ".fsm.sh"
    hook.write_text(f"#!/bin/sh\necho fsmonitor >> {marker}\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    include = worktree / ".inc"
    include.write_text(f"[core]\n\tfsmonitor = {hook}\n", encoding="utf-8")
    hooks = evil / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("post-checkout", "reference-transaction", "pre-auto-gc"):
        (hooks / name).write_text(
            f"#!/bin/sh\necho hook >> {marker}\n", encoding="utf-8"
        )
        (hooks / name).chmod(0o755)
    with open(evil / "config", "a", encoding="utf-8") as handle:
        handle.write(
            f"[core]\n\tbare = false\n\tfsmonitor = {hook}\n"
            f"\thooksPath = {hooks}\n[include]\n\tpath = {include}\n"
        )
    (worktree / ".git").write_text(f"gitdir: {evil}\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_worker_planted_git_file_never_runs_on_the_server(tmp_path: Path) -> None:
    """probe_fsmonitor: the server must not follow the checkout's .git file."""
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    record = await ensure_ticket_worktree(
        repo, TICKET_ID, base_sha=base, root=tmp_path / "wt"
    )
    marker = tmp_path / "PWNED"
    _plant_evil_gitdir(record.path, record.branch, marker)
    (record.path / "README").write_text("dirty\n", encoding="utf-8")

    # Reuse (registration + branch check) and removal (status, archive, remove).
    await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=tmp_path / "wt")
    await remove_ticket_worktree(record)

    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert not record.path.exists()  # restored .git pointer, so remove works
    assert _git(repo, "rev-parse", "--verify", f"refs/heads/{record.branch}")


@pytest.mark.asyncio
async def test_worker_planted_worktree_config_never_runs(tmp_path: Path) -> None:
    """config.worktree (+ include) with fsmonitor, hooksPath and a filter driver."""
    repo = _repo(tmp_path / "git")
    _git(repo, "config", "extensions.worktreeConfig", "true")
    base = _git(repo, "rev-parse", "HEAD")
    record = await ensure_ticket_worktree(
        repo, TICKET_ID, base_sha=base, root=tmp_path / "wt"
    )
    admin = Path(_git(record.path, "rev-parse", "--absolute-git-dir"))
    marker = tmp_path / "PWNED"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\necho ran >> {marker}\ncat\n", encoding="utf-8")
    script.chmod(0o755)
    include = tmp_path / "inc"
    include.write_text(
        f'[filter "evil"]\n\tclean = {script}\n\tsmudge = {script}\n', encoding="utf-8"
    )
    (admin / "config.worktree").write_text(
        f"[core]\n\tfsmonitor = {script}\n\thooksPath = {tmp_path}\n"
        f"[include]\n\tpath = {include}\n",
        encoding="utf-8",
    )
    (record.path / ".gitattributes").write_text("README filter=evil\n", encoding="utf-8")
    (record.path / "README").write_text("changed\n", encoding="utf-8")

    await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=tmp_path / "wt")
    await remove_ticket_worktree(record)

    assert not marker.exists(), marker.read_text(encoding="utf-8")
    assert not record.path.exists()


@pytest.mark.asyncio
async def test_repository_hooks_do_not_run_on_create(tmp_path: Path) -> None:
    repo = _repo(tmp_path / "git")
    _git(repo, "config", "--unset", "core.hooksPath")
    _git(repo, "config", "--unset", "core.fsmonitor")
    marker = tmp_path / "HOOKED"
    hook = repo / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho hook >> {marker}\n", encoding="utf-8")
    hook.chmod(0o755)
    base = _git(repo, "rev-parse", "HEAD")

    record = await ensure_ticket_worktree(
        repo, TICKET_ID, base_sha=base, root=tmp_path / "wt"
    )

    assert record.path.exists()
    assert not marker.exists()


@pytest.mark.asyncio
async def test_checkout_git_uses_an_explicit_server_resolved_git_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    record = await ensure_ticket_worktree(
        repo, TICKET_ID, base_sha=base, root=tmp_path / "wt"
    )
    calls: list[tuple[list[str], dict]] = []
    real_exec = tw.asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        calls.append((list(argv), kwargs))
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(tw.asyncio, "create_subprocess_exec", spy)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.fsmonitor'='/bin/false'")
    await remove_ticket_worktree(record)

    git_calls = [argv for argv, _ in calls if Path(argv[0]).name == "git"]
    assert git_calls
    for argv, kwargs in calls:
        if Path(argv[0]).name != "git":
            continue
        assert "core.fsmonitor=false" in argv and "core.hooksPath=/dev/null" in argv
        assert any(arg.startswith("--git-dir=") for arg in argv), argv
        env = kwargs["env"]
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert "GIT_DIR" not in env and "GIT_CONFIG_PARAMETERS" not in env
    common = (repo / ".git").resolve()
    checkout_dirs = {
        arg.split("=", 1)[1]
        for argv in git_calls
        for arg in argv
        if arg.startswith("--git-dir=")
    }
    assert checkout_dirs <= {str(common), str(common / "worktrees" / record.path.name)}


# ------------------------------------------- 3. pending wedge on reopen


@pytest.mark.asyncio
async def test_stop_then_resume_takes_the_pending_checkout_over(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """test_probe_wedge: resume before the retry must not wedge the Ticket."""
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    fake.row()["status"] = "cancelled"
    monkeypatch.setattr(tw, "_thread_has_active_run", AsyncMock(return_value=True))
    assert await release_server_ticket_worktree(dict(fake.row()), reason="stop") == "pending"
    assert fake.row()["worktree_cleanup_pending"] is True

    monkeypatch.setattr(tw, "_thread_has_active_run", AsyncMock(return_value=False))
    fake.row()["status"] = "queued"
    fake.row()["revision"] += 1
    resumed = await prepare_server_ticket_worktree(fake.row())

    assert Path(resumed["worktree_path"]) == path and path.exists()
    assert fake.row()["worktree_cleanup_pending"] is False
    assert fake.row()["worktree_cleanup_attempts"] == 0
    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 0
    assert path.exists()


@pytest.mark.asyncio
async def test_sweep_cancels_pending_for_a_reopened_server_ticket(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    fake.row()["status"] = "cancelled"
    monkeypatch.setattr(tw, "_thread_has_active_run", AsyncMock(return_value=True))
    assert await release_server_ticket_worktree(dict(fake.row()), reason="stop") == "pending"
    fake.row()["status"] = "blocked"  # reopened, still server-owned

    for _ in range(2):
        fake.make_due()
        assert await sweep_ticket_worktree_cleanups() == 0

    assert fake.cancel_calls and fake.cancel_calls[0]["host"] == "server"
    assert fake.row()["worktree_cleanup_pending"] is False
    assert path.exists() and fake.row()["worktree_path"] == str(path)
    # Nothing is owed any more: the next sweep does not even claim it.
    fake.make_due()
    await sweep_ticket_worktree_cleanups()
    assert len(fake.cancel_calls) == 1


@pytest.mark.asyncio
async def test_pending_cleanup_of_another_path_still_refuses_bind(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_processes(monkeypatch)
    fake, _repo_path, _path = bound
    fake.row().update(
        worktree_path="/srv/other/checkout", worktree_cleanup_pending=True
    )
    with pytest.raises(TicketWorktreeError, match="waiting for cleanup"):
        await prepare_server_ticket_worktree(fake.row())


# ------------------------------------------- 4. wait for the Worker process


@pytest.mark.asyncio
async def test_removal_waits_until_the_worker_process_is_gone(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Real OS probe: a process with its cwd in the checkout keeps it."""
    fake, _repo_path, path = bound
    monkeypatch.setattr(tw, "_PROCESS_GONE_GRACE_SECONDS", 0.3)
    worker_process = subprocess.Popen(["sleep", "60"], cwd=path)
    try:
        fake.row()["status"] = "completed"
        outcome = await release_server_ticket_worktree(dict(fake.row()), reason="close")
        assert outcome == "pending"
        assert path.exists() and fake.row()["worktree_path"] == str(path)
        assert fake.row()["worktree_cleanup_pending"] is True
    finally:
        worker_process.kill()
        worker_process.wait()

    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 1
    assert not path.exists()


@pytest.mark.asyncio
async def test_unknown_process_state_is_not_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tw, "_checkout_process_ids", AsyncMock(return_value=None))
    assert await tw._process_gone_within(tmp_path, 0.0) is False


# ------------------------------------------- 5. orphan release and active run


@pytest.mark.asyncio
async def test_orphan_release_waits_for_the_old_server_run(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reassignment clears worker_thread_id; the old run may still be live."""
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    fake.row().update(status="queued", worker_daemon_id="daemon-1", worker_thread_id=None)
    _reassigned_from_server(monkeypatch, "thread-old")
    active = AsyncMock(side_effect=lambda thread_id: thread_id == "thread-old")
    monkeypatch.setattr(tw, "_thread_has_active_run", active)
    monkeypatch.setattr(ownership, "ticket_db", fake)

    out = await ownership._hand_over_ticket_worktree(
        dict(fake.row()), from_daemon_id=None, from_thread_id="thread-old"
    )

    assert path.exists() and out["worktree_path"] == str(path)
    assert fake.row()["worktree_cleanup_pending"] is True
    active.assert_any_await("thread-old")

    active.side_effect = lambda thread_id: False
    assert (
        await release_server_ticket_worktree(
            dict(fake.row()), reason="reassign", runner_thread_id="thread-old"
        )
        == "removed"
    )
    assert not path.exists()


@pytest.mark.asyncio
async def test_orphan_release_ignores_the_new_daemon_run_thread(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon's own run never writes the server copy; it must not wedge it."""
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    fake.row().update(
        status="running", worker_daemon_id="daemon-1", worker_thread_id="thread-daemon"
    )
    _reassigned_from_server(monkeypatch)
    monkeypatch.setattr(
        tw,
        "_thread_has_active_run",
        AsyncMock(side_effect=lambda thread_id: thread_id == "thread-daemon"),
    )

    assert await release_server_ticket_worktree(dict(fake.row()), reason="sweep") == "removed"
    assert not path.exists()


# ------------------------------------------- 7. daemon hand-over fences


def _daemon_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **extra) -> FakeTicketDb:
    fake = FakeTicketDb(
        _row(
            tmp_path,
            status="queued",
            worker_daemon_id=None,
            worktree_path="/daemon/host/tickets/t",
            worktree_generation=3,
            worktree_host="daemon",
            worktree_daemon_id="daemon-1",
            worktree_lease_id="lease-2",
            worktree_base_sha="a" * 40,
            **extra,
        )
    )
    monkeypatch.setattr(ownership, "ticket_db", fake)
    return fake


@pytest.mark.asyncio
@pytest.mark.parametrize("lease", [None, "lease-1"], ids=["unknown", "older-lease"])
async def test_daemon_hand_over_without_a_matching_lease_records_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lease
) -> None:
    """Never clear a daemon binding the released lease did not bind."""
    fake = _daemon_bound(tmp_path, monkeypatch)

    out = await ownership._hand_over_ticket_worktree(
        dict(fake.row()), from_daemon_id="daemon-1", from_lease_id=lease
    )

    assert out["worktree_path"] == "/daemon/host/tickets/t"
    assert out["worktree_lease_id"] == "lease-2"
    assert fake.row()["worktree_cleanup_pending"] is True
    recorded = fake.pending_calls[-1]
    assert recorded["host"] == "daemon" and recorded["daemon_id"] == "daemon-1"
    assert recorded["generation"] == 3


@pytest.mark.asyncio
async def test_daemon_hand_over_never_clears_another_daemons_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _daemon_bound(tmp_path, monkeypatch)
    row = dict(fake.row())
    # The caller's snapshot says daemon-1; the stored binding moved to daemon-2.
    fake.row()["worktree_daemon_id"] = "daemon-2"

    await ownership._hand_over_ticket_worktree(
        row, from_daemon_id="daemon-1", from_lease_id="lease-2"
    )

    assert fake.row()["worktree_path"] == "/daemon/host/tickets/t"
    assert fake.row()["worktree_daemon_id"] == "daemon-2"
    assert fake.row()["worktree_cleanup_pending"] is False


# ------------------------------------------- 8. fences, reschedule, cap


@pytest.mark.asyncio
async def test_server_release_writes_are_host_fenced(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_processes(monkeypatch)
    fake, _repo_path, _path = bound
    fake.row()["status"] = "completed"
    monkeypatch.setattr(
        tw, "remove_ticket_worktree", AsyncMock(side_effect=TicketWorktreeError("busy"))
    )
    assert await release_server_ticket_worktree(dict(fake.row()), reason="close") == "pending"
    assert fake.pending_calls[-1]["host"] == "server"

    monkeypatch.setattr(tw, "remove_ticket_worktree", AsyncMock(return_value=None))
    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 1
    assert fake.clear_calls[-1]["host"] == "server"


@pytest.mark.asyncio
async def test_failed_retry_of_a_pending_row_reschedules(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    fake.row()["status"] = "completed"
    monkeypatch.setattr(
        tw, "remove_ticket_worktree", AsyncMock(side_effect=TicketWorktreeError("busy"))
    )
    assert await release_server_ticket_worktree(dict(fake.row()), reason="close") == "pending"
    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 0

    assert [call["kind"] for call in fake.pending_calls] == ["record", "reschedule"]
    assert fake.row()["worktree_cleanup_attempts"] == 2
    assert fake.row()["worktree_cleanup_next_retry_at"] > datetime.now(timezone.utc)
    assert path.exists()


@pytest.mark.asyncio
async def test_sweep_reschedules_when_the_retry_raises(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_processes(monkeypatch)
    fake, _repo_path, _path = bound
    fake.row().update(
        status="completed", worktree_cleanup_pending=True, worktree_cleanup_attempts=1
    )
    fake.make_due()
    monkeypatch.setattr(
        tw, "release_server_ticket_worktree", AsyncMock(side_effect=RuntimeError("db"))
    )

    assert await sweep_ticket_worktree_cleanups() == 0

    assert fake.pending_calls[-1]["kind"] == "reschedule"
    assert fake.row()["worktree_cleanup_attempts"] == 2


@pytest.mark.asyncio
async def test_sweep_respects_the_attempt_cap(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    fake.row().update(
        status="completed",
        worktree_cleanup_pending=True,
        worktree_cleanup_attempts=FakeTicketDb.MAX_WORKTREE_CLEANUP_ATTEMPTS,
    )
    fake.make_due()
    remove = AsyncMock(return_value=None)
    monkeypatch.setattr(tw, "remove_ticket_worktree", remove)

    assert await sweep_ticket_worktree_cleanups() == 0

    remove.assert_not_awaited()
    assert path.exists() and fake.row()["worktree_cleanup_pending"] is True


# ===================================================== N17 review round 3


# --------------------- Blocker A: handover away from a departed daemon


def _server_ticket_on_departed_daemon_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[FakeTicketDb, Path]:
    """Daemon-1 bound the Ticket (lease-1, generation 3); it is now the server's."""
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    fake = FakeTicketDb(
        _row(
            repo,
            status="queued",
            worker_daemon_id=None,
            worker_thread_id=None,
            worktree_path="/daemon/host/tickets/t",
            worktree_generation=3,
            worktree_host="daemon",
            worktree_daemon_id="daemon-1",
            worktree_lease_id="lease-1",
            worktree_branch=tw.ticket_branch(TICKET_ID),
            worktree_base_sha=base,
        )
    )
    monkeypatch.setattr(tw, "ticket_db", fake)
    monkeypatch.setattr(ownership, "ticket_db", fake)
    return fake, repo


@pytest.mark.asyncio
@pytest.mark.parametrize("from_lease", [None, "lease-2"], ids=["null-lease", "mismatched-lease"])
async def test_server_binds_after_handover_from_a_departed_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, from_lease
) -> None:
    """r2routes/test_probe_reassign_handover: the new owner must not wedge.

    The released lease is unknown (expired, requeued) or is a later attempt's
    lease that never re-bound, so the hand-over cannot clear the binding and
    records it pending for daemon-1. Before round 3 the server's bind was
    refused for as long as that binding lived ("already has a daemon-host
    checkout"), so the Ticket could never run on its new owner.
    """
    _no_processes(monkeypatch)
    fake, repo = _server_ticket_on_departed_daemon_binding(tmp_path, monkeypatch)
    await ownership._hand_over_ticket_worktree(
        dict(fake.row()), from_daemon_id="daemon-1", from_lease_id=from_lease
    )
    assert fake.row()["worktree_path"] == "/daemon/host/tickets/t"
    assert fake.row()["worktree_cleanup_pending"] is True

    bound = await prepare_server_ticket_worktree(dict(fake.row()))

    path = Path(bound["worktree_path"])
    assert path.is_dir() and bound["worktree_host"] == "server"
    assert bound["worktree_generation"] == 4
    assert bound["worktree_cleanup_pending"] is False
    assert fake.bind_calls[-1]["replace_generation"] == 3
    # The departed daemon's binding is kept as a note for its late report.
    assert fake.row()["worktree_stale_cleanups"] == [
        {
            "daemon_id": "daemon-1",
            "worktree_path": "/daemon/host/tickets/t",
            "worktree_generation": 3,
            "lease_id": "lease-1",
        }
    ]
    assert _git(path, "rev-parse", "HEAD") == fake.row()["worktree_base_sha"]


@pytest.mark.asyncio
async def test_server_bind_onto_a_departed_binding_is_fenced_on_its_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale read of the departed binding fences out; the retry re-reads."""
    _no_processes(monkeypatch)
    fake, _repo_path = _server_ticket_on_departed_daemon_binding(tmp_path, monkeypatch)
    real_bind = fake.bind_ticket_worktree
    moved = {"done": False}

    async def bind_after_a_concurrent_move(ticket_id, **kwargs):
        if not moved["done"]:
            # Between the read and the bind the departed binding advanced.
            moved["done"] = True
            fake.row()["worktree_generation"] = 5
            fake.row()["revision"] += 1
        return await real_bind(ticket_id, **kwargs)

    monkeypatch.setattr(fake, "bind_ticket_worktree", bind_after_a_concurrent_move)

    bound = await prepare_server_ticket_worktree(dict(fake.row()))

    assert [call["replace_generation"] for call in fake.bind_calls] == [3, 5]
    assert bound["worktree_host"] == "server" and bound["worktree_generation"] == 6
    assert fake.row()["worktree_stale_cleanups"][0]["worktree_generation"] == 5


@pytest.mark.asyncio
async def test_live_daemon_binding_is_still_never_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a departed daemon's binding is replaceable; the owner's is not."""
    _no_processes(monkeypatch)
    fake, _repo_path = _server_ticket_on_departed_daemon_binding(tmp_path, monkeypatch)
    fake.row()["worktree_daemon_id"] = None  # not departed: no daemon to depart
    with pytest.raises(TicketWorktreeError, match="daemon-host checkout"):
        await prepare_server_ticket_worktree(dict(fake.row()))
    assert fake.bind_calls == []
    assert tw.departed_daemon_binding(
        {"worktree_host": "daemon", "worktree_path": "/p",
         "worktree_daemon_id": "d1", "worker_daemon_id": "d2"}
    )
    assert not tw.departed_daemon_binding(
        {"worktree_host": "daemon", "worktree_path": "/p",
         "worktree_daemon_id": "d1", "worker_daemon_id": "d1"}
    )


# ------------------ should-fix 2: the orphan sweep checks the old run


@pytest.mark.asyncio
async def test_orphan_sweep_retry_waits_for_the_old_server_run(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r2svc/test_probe_s3: the sweep has no caller thread, the history does.

    The reassignment records the old server Worker thread in its audit event;
    a later sweep retry of the orphaned checkout checks that thread instead of
    removing the checkout under the still-running old run.
    """
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    from src.server.database.autonomy import lines as line_db

    events: list[dict] = []

    async def reassign(ticket_id, **kwargs):
        events.append({"action": "reassigned", **kwargs["event"]})
        fake.row().update(
            status="queued",
            worker_daemon_id=kwargs["worker_daemon_id"],
            worker_thread_id=None,
            revision=fake.row()["revision"] + 1,
        )
        return dict(fake.row())

    async def list_events(ticket_id, *, limit=100, offset=0):
        return [dict(event) for event in events]

    monkeypatch.setattr(ownership, "ticket_db", fake)
    monkeypatch.setattr(ownership.line_db, "reassign_ticket", reassign)
    monkeypatch.setattr(line_db, "list_ticket_events", list_events)
    monkeypatch.setattr(ownership, "_owned_ticket", AsyncMock(return_value=dict(fake.row())))
    monkeypatch.setattr(
        ownership.daemon_db, "get_daemon", AsyncMock(return_value={"provider": "codex"})
    )
    monkeypatch.setattr(ownership, "_cancel_cloud_worker_before_takeover", AsyncMock())
    active = AsyncMock(side_effect=lambda thread_id: thread_id == "thread-1")
    monkeypatch.setattr(tw, "_thread_has_active_run", active)

    await ownership.reassign_ticket(
        user_id="user-1", ticket_id=TICKET_ID, daemon_id="daemon-1"
    )
    assert events[0]["detail"][tw.REASSIGN_FROM_THREAD_KEY] == "thread-1"
    assert path.exists() and fake.row()["worktree_cleanup_pending"] is True

    for _ in range(2):  # every retry while the old run lives keeps it
        fake.make_due()
        assert await sweep_ticket_worktree_cleanups() == 0
        assert path.exists()
    active.assert_any_await("thread-1")

    active.side_effect = lambda thread_id: False
    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 1
    assert not path.exists()


@pytest.mark.asyncio
async def test_orphan_release_with_unreadable_history_is_not_removed(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    fake.row().update(status="queued", worker_daemon_id="daemon-1", worker_thread_id=None)
    monkeypatch.setattr(
        "src.server.database.autonomy.lines.list_ticket_events",
        AsyncMock(side_effect=RuntimeError("db down")),
    )
    assert await release_server_ticket_worktree(dict(fake.row()), reason="retry") == "pending"
    assert path.exists()


# ------------------------ should-fix 6: no lsof and no /proc is unknown


@pytest.mark.asyncio
async def test_host_without_proc_or_lsof_reports_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tw, "_PROC_ROOT", tmp_path / "no-proc")
    monkeypatch.setattr(tw, "_LSOF_CANDIDATES", (str(tmp_path / "no-lsof"),))
    assert await tw._checkout_process_ids(tmp_path) is None
    assert await tw._process_gone_within(tmp_path, 0.0) is False


# ------------------- should-fix 7: worktree add runs no smudge filter


@pytest.mark.asyncio
async def test_worktree_add_runs_no_repository_smudge_filter(tmp_path: Path) -> None:
    """r2svc/probe_b2_add: a Worker-committed .gitattributes on the kept branch
    names a filter the server repository's own config defines; recreating the
    checkout must not run it (nor an LFS smudge)."""
    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    markers = tmp_path / "markers"
    markers.mkdir()
    smudge = tmp_path / "smudge"
    smudge.write_text(f"#!/bin/sh\necho s >> {markers}/smudge\ncat\n", encoding="utf-8")
    smudge.chmod(0o755)
    _git(repo, "config", "filter.userf.smudge", str(smudge))
    _git(repo, "config", "filter.userf.required", "true")
    root = tmp_path / "worktrees"

    record = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    (record.path / ".gitattributes").write_text("* filter=userf\n", encoding="utf-8")
    (record.path / "file.txt").write_text("payload\n", encoding="utf-8")
    _git(record.path, "-c", "filter.userf.clean=cat", "add", ".")
    _git(record.path, "-c", "filter.userf.clean=cat", "commit", "-qm", "attrs")
    await remove_ticket_worktree(record)

    again = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)

    assert (again.path / "file.txt").read_text(encoding="utf-8") == "payload\n"
    assert sorted(p.name for p in markers.iterdir()) == []
    assert tw._git_env()["GIT_LFS_SKIP_SMUDGE"] == "1"


# =============================================================== round 4
#
# N17 review round 4, line S. The probes these replace live in the review
# scratchpad (rv11/r3svc): probe_halfstate.py, test_probe_s3_r3.py.


def _big_repo(root: Path, files: int = 40) -> tuple[Path, str]:
    repo = _repo(root)
    tree = repo / "tree"
    tree.mkdir()
    for index in range(files):
        (tree / f"f{index}.txt").write_text(f"{index}\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "tree")
    return repo, _git(repo, "rev-parse", "HEAD")


def _half_build(repo: Path, worktree: Path) -> None:
    """What a ``worktree add`` killed mid-checkout leaves behind.

    Deterministic stand-in for the timeout (no sub-second race): the
    registration stays locked "initializing", the index was never written and
    part of the tree is missing, so ``status`` reports the repo as deleted.
    """
    admin = (repo / ".git" / "worktrees" / worktree.name).resolve()
    assert admin.is_dir()
    (admin / "locked").write_text("initializing\n", encoding="utf-8")
    (admin / "index").unlink(missing_ok=True)
    for path in sorted((worktree / "tree").iterdir())[::2]:
        path.unlink()


def _listed(repo: Path, worktree: Path) -> bool:
    return f"worktree {worktree.resolve()}" in _git(
        repo, "worktree", "list", "--porcelain"
    ).splitlines()


def test_server_git_timeout_is_well_under_the_daemon_client_timeout() -> None:
    assert tw._GIT_TIMEOUT_SECONDS == 20.0
    assert tw._GIT_TIMEOUT_SECONDS < 30.0


@pytest.mark.asyncio
async def test_checkout_git_is_built_by_the_shared_hardened_builder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.services.autonomy.git_delivery import git_pinned_config_args

    repo = _repo(tmp_path / "git")
    base = _git(repo, "rev-parse", "HEAD")
    calls: list[tuple[list[str], dict]] = []
    real_exec = tw.asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        calls.append((list(argv), kwargs))
        return await real_exec(*argv, **kwargs)

    monkeypatch.setattr(tw.asyncio, "create_subprocess_exec", spy)
    record = await ensure_ticket_worktree(
        repo, TICKET_ID, base_sha=base, root=tmp_path / "wt"
    )
    await remove_ticket_worktree(record)

    pins = list(git_pinned_config_args())
    git_calls = [(a, k) for a, k in calls if Path(a[0]).name == "git"]
    assert git_calls
    for argv, kwargs in git_calls:
        assert argv[1 : 1 + len(pins)] == pins, argv
        assert kwargs["env"]["GIT_LFS_SKIP_SMUDGE"] == "1"
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert all(
        any(arg.startswith("--git-dir=") for arg in argv)
        for argv, _ in git_calls
        if "worktree" in argv or "status" in argv
    )


@pytest.mark.asyncio
async def test_timed_out_add_leaves_no_half_built_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """probe_halfstate (add): a timeout mid-add was reused as a 'deleted' repo."""
    repo, base = _big_repo(tmp_path / "git")
    root = tmp_path / "wt"
    real_run = tw._run_git
    worktree = tw.ticket_worktree_path(tw._worktree_root(root), repo.resolve(), TICKET_ID)

    async def timing_out_add(argv, **kwargs):
        result = await real_run(argv, **kwargs)
        if argv[:2] == ["worktree", "add"]:
            _half_build(repo, worktree)
            raise tw.TicketWorktreeUnavailable("git timed out on the server after 20 s")
        return result

    monkeypatch.setattr(tw, "_run_git", timing_out_add)
    with pytest.raises(tw.TicketWorktreeUnavailable):
        await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)

    assert not worktree.exists()
    assert not _listed(repo, worktree)
    # The branch this add created (still at the pin) went with it.
    assert _git(repo, "branch", "--list", tw.ticket_branch(TICKET_ID)) == ""

    monkeypatch.setattr(tw, "_run_git", real_run)
    record = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    assert _git(record.path, "status", "--porcelain") == ""
    assert len(list((record.path / "tree").iterdir())) == 40


@pytest.mark.asyncio
async def test_half_built_checkout_is_never_reused_and_keeps_branch_commits(
    tmp_path: Path,
) -> None:
    """probe_halfstate (reuse): the cleanup itself did not run (crash)."""
    repo, base = _big_repo(tmp_path / "git")
    root = tmp_path / "wt"
    record = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    work = _commit(record.path, "work.txt", "work\n")
    _half_build(repo, record.path)
    assert _git(record.path, "status", "--porcelain")  # looks all-deleted

    again = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)

    assert again.path == record.path
    assert _git(again.path, "rev-parse", "HEAD") == work
    assert _git(again.path, "status", "--porcelain") == ""
    assert len(list((again.path / "tree").iterdir())) == 40
    assert "locked" not in _git(repo, "worktree", "list", "--porcelain")


@pytest.mark.asyncio
async def test_checkout_failing_the_integrity_check_is_moved_aside_and_recreated(
    tmp_path: Path,
) -> None:
    repo, base = _big_repo(tmp_path / "git")
    root = tmp_path / "wt"
    record = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    work = _commit(record.path, "work.txt", "work\n")
    (record.path / "unsaved.txt").write_text("uncommitted\n", encoding="utf-8")
    admin = (repo / ".git" / "worktrees" / record.path.name).resolve()
    (admin / "HEAD").write_text("garbage\n", encoding="utf-8")  # HEAD unresolvable

    again = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)

    assert _git(again.path, "rev-parse", "HEAD") == work
    assert _git(again.path, "status", "--porcelain") == ""
    saved = list((record.path.parents[2] / "recovery-artifacts").glob("*/unsaved.txt"))
    assert [p.read_text(encoding="utf-8") for p in saved] == ["uncommitted\n"]


@pytest.mark.asyncio
async def test_unanswered_integrity_check_discards_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _big_repo(tmp_path / "git")
    root = tmp_path / "wt"
    record = await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    (record.path / "unsaved.txt").write_text("uncommitted\n", encoding="utf-8")
    real_run = tw._run_git

    async def status_times_out(argv, **kwargs):
        if argv[:1] == ["status"]:
            raise tw.TicketWorktreeUnavailable("git timed out on the server after 20 s")
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(tw, "_run_git", status_times_out)
    with pytest.raises(tw.TicketWorktreeUnavailable):
        await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    assert (record.path / "unsaved.txt").exists() and _listed(repo, record.path)


@pytest.mark.asyncio
async def test_failed_add_is_undone_and_carries_no_git_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base = _big_repo(tmp_path / "git")
    root = tmp_path / "wt"
    real_run = tw._run_git

    async def failing_add(argv, **kwargs):
        if argv[:2] == ["worktree", "add"]:
            await real_run(argv, **kwargs)
            return 128, "", "fatal: secret-stderr /private/path"
        return await real_run(argv, **kwargs)

    monkeypatch.setattr(tw, "_run_git", failing_add)
    with pytest.raises(TicketWorktreeError) as caught:
        await ensure_ticket_worktree(repo, TICKET_ID, base_sha=base, root=root)
    assert "secret-stderr" not in str(caught.value)
    worktree = tw.ticket_worktree_path(tw._worktree_root(root), repo.resolve(), TICKET_ID)
    assert not worktree.exists() and not _listed(repo, worktree)


# ---------------------- S3: the reassignment history is read, or unknown


def _reassign_event(thread: str | None = None, action: str = "reassigned") -> dict:
    detail = {"from_status": "running"}
    if thread:
        detail[tw.REASSIGN_FROM_THREAD_KEY] = thread
    return {
        "action": action,
        "from_owner": "cloud" if action == "reassigned" else "",
        "detail": detail,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,events,active,expected",
    [
        ("present_live", [_reassign_event("T1")], {"T1"}, "pending"),
        ("present_ended", [_reassign_event("T1")], set(), "removed"),
        ("missing", [], set(), "pending"),
        ("only_other_events", [_reassign_event(action="stopped")] * 3, set(), "pending"),
        (
            "second_page_live",
            [_reassign_event(action="stopped")] * 150 + [_reassign_event("T1")],
            {"T1"},
            "pending",
        ),
        (
            "second_page_ended",
            [_reassign_event(action="stopped")] * 150 + [_reassign_event("T1")],
            set(),
            "removed",
        ),
        (
            "out_of_window",
            [_reassign_event(action="stopped")] * 1000 + [_reassign_event("T1")],
            set(),
            "pending",
        ),
        (
            "multi_old_thread_live",
            [_reassign_event("T2"), _reassign_event(), _reassign_event("T1")],
            {"T1"},
            "pending",
        ),
    ],
)
async def test_orphan_retry_reads_the_reassignment_history_or_keeps_it(
    bound, monkeypatch: pytest.MonkeyPatch, name, events, active, expected
) -> None:
    """test_probe_s3_r3: a missing or out-of-window event is unknown, not ended."""
    _no_processes(monkeypatch)
    fake, _repo_path, path = bound
    fake.row().update(status="queued", worker_daemon_id="daemon-1", worker_thread_id=None)

    async def list_events(ticket_id, *, limit=100, offset=0):
        return [dict(event) for event in events[offset : offset + limit]]

    monkeypatch.setattr("src.server.database.autonomy.lines.list_ticket_events", list_events)
    monkeypatch.setattr(
        tw, "_thread_has_active_run", AsyncMock(side_effect=lambda tid: tid in active)
    )

    outcome = await release_server_ticket_worktree(dict(fake.row()), reason="retry")

    assert outcome == expected, name
    assert path.exists() is (expected == "pending")
