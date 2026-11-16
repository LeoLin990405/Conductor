"""Ticket worktree services: durable base, fenced server bind, daemon claim spec.

Every test runs against tmp local git repositories and an in-memory fake of
the ``tickets`` persistence API (contract names, contract fences); nothing
touches a database or the network.

On core/main these fail: ``ticket_worktree.py`` does not exist there (no
per-Ticket checkout at all; native launches run in the shared mandate cwd and
the prompt pins ``conductor/ticket-<id8>``), so every import below errors.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.server.database.autonomy import tickets as real_ticket_db
from src.server.services.autonomy import ticket_worktree as tw
from src.server.services.autonomy import worker
from src.server.services.autonomy.ticket_worktree import (
    TicketWorktreeBaseError,
    TicketWorktreeError,
    TicketWorktreeUnavailable,
    daemon_claim_worktree_spec,
    ensure_ticket_worktree,
    prepare_server_ticket_worktree,
    ticket_branch,
    ticket_worktree_path,
)
from src.server.services.projects.dispatch import DispatchError

TICKET_ID = "aaaaaaaa-1111-2222-3333-444444444444"
THREAD_ID = "thread-worktree-1"
OPEN = ("queued", "running", "waiting", "blocked", "awaiting_clarification")
WORKTREE_FIELDS = {
    "worktree_path": "",
    "worktree_generation": 0,
    "worktree_cleanup_pending": False,
    "worktree_cleanup_attempts": 0,
    "worktree_cleanup_next_retry_at": None,
    "worktree_branch": "",
    "worktree_base_sha": "",
    "worktree_base_branch": "",
    "worktree_repo": "",
    "worktree_host": "",
    "worktree_daemon_id": None,
    "worktree_lease_id": None,
}


# --------------------------------------------------------------------- git


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.name", "Ticket Worktree")
    _git(repo, "config", "user.email", "ticket-worktree@example.invalid")
    _git(repo, "config", "core.hooksPath", "/dev/null")
    _git(repo, "config", "core.fsmonitor", "false")


def _repo_with_origin(root: Path) -> tuple[Path, str]:
    """A clone of a local bare origin; returns (clone, origin/main sha)."""
    root.mkdir(parents=True, exist_ok=True)
    seed = root / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _configure(seed)
    (seed / "README").write_text("base\n", encoding="utf-8")
    _git(seed, "add", "README")
    _git(seed, "commit", "-qm", "base")
    origin = root / "origin.git"
    _git(root, "clone", "--bare", "-q", str(seed), str(origin))
    repo = root / "repo"
    _git(root, "clone", "-q", str(origin), str(repo))
    _configure(repo)
    return repo, _git(repo, "rev-parse", "origin/main")


def _move_origin_main(repo: Path) -> str:
    (repo / "README").write_text(f"moved {datetime.now().isoformat()}\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "moved tip")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _isolated_git_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.delenv("CONDUCTOR_DISPATCH_REPO", raising=False)
    monkeypatch.setattr(tw, "_checkout_process_ids", AsyncMock(return_value=set()))


# ---------------------------------------------------------- persistence fake


class FakeTicketDb:
    """In-memory stand-in for the contract's persistence API.

    Implements the fences the contract specifies so the services are tested
    against the same refusals the SQL makes.
    """

    OPEN_STATUSES = OPEN
    WORKTREE_HOST_SERVER = "server"
    WORKTREE_HOST_DAEMON = "daemon"
    WorkerSlotsFull = real_ticket_db.WorkerSlotsFull

    def __init__(self, *tickets: dict) -> None:
        self.rows = {
            str(t["ticket_id"]): {**WORKTREE_FIELDS, **t} for t in tickets
        }
        self.pins: list[str] = []
        self.binds: list[dict] = []
        self.before_bind = None  # async hook(ticket_id, kwargs) for interleavings
        self._locks: dict[str, asyncio.Lock] = {}
        self.lock_events: list[str] = []

    def row(self, ticket_id: str = TICKET_ID) -> dict:
        return self.rows[ticket_id]

    async def get_ticket(self, ticket_id: str):
        row = self.rows.get(str(ticket_id))
        return dict(row) if row is not None else None

    async def update_ticket(self, ticket_id: str, **kwargs):
        if any(key.startswith("worktree_") for key in kwargs):
            raise ValueError("worktree fields are written only through the helpers")
        row = self.rows.get(str(ticket_id))
        if row is None:
            return None
        for name in ("status", "revision", "thread_id"):
            expected = kwargs.get(f"expected_{name}")
            column = "worker_thread_id" if name == "thread_id" else name
            if expected is not None and row.get(column) != expected:
                return None
        for key, value in kwargs.items():
            if not key.startswith("expected_") and key != "slot_limit":
                row[key] = value
        row["revision"] = int(row.get("revision") or 0) + 1
        return dict(row)

    async def pin_ticket_worktree_base(self, ticket_id, *, base_sha, base_branch):
        self.pins.append(base_sha)
        row = self.rows.get(str(ticket_id))
        if row is None:
            return None
        if not row["worktree_base_sha"]:
            row["worktree_base_sha"] = base_sha
            row["worktree_base_branch"] = base_branch
        return dict(row)

    async def bind_ticket_worktree(
        self,
        ticket_id,
        *,
        worktree_path,
        branch,
        base_sha,
        repo,
        host,
        expected_revision=None,
        daemon_id=None,
        lease_id=None,
    ):
        call = dict(
            worktree_path=worktree_path,
            branch=branch,
            base_sha=base_sha,
            repo=repo,
            host=host,
            expected_revision=expected_revision,
        )
        self.binds.append(call)
        if self.before_bind is not None:
            await self.before_bind(ticket_id, call)
        row = self.rows.get(str(ticket_id))
        if row is None:
            return None
        # Round-2 contract: the same path by the same host (and daemon) takes
        # a pending checkout over; anything else stays refused while pending.
        takeover = (
            row["worktree_cleanup_pending"]
            and row["worktree_path"] == worktree_path
            and row["worktree_host"] == host
            and (host != "daemon" or row["worktree_daemon_id"] == daemon_id)
        )
        if (
            row["status"] not in OPEN
            or row.get("closed_at")
            or (row["worktree_cleanup_pending"] and not takeover)
            or row["worktree_base_sha"] != base_sha
            or row["worktree_path"] not in ("", worktree_path)
        ):
            return None
        if host == "server":
            if row.get("worker_daemon_id") is not None:
                return None
            if expected_revision is not None and row.get("revision") != expected_revision:
                return None
        if takeover:
            row.update(
                worktree_cleanup_pending=False,
                worktree_cleanup_attempts=0,
                worktree_cleanup_next_retry_at=None,
            )
        if (
            row["worktree_path"] == worktree_path
            and row["worktree_host"] == host
            and row["worktree_branch"] == branch
            and row["worktree_lease_id"] == lease_id
        ):
            return dict(row)
        row.update(
            worktree_path=worktree_path,
            worktree_branch=branch,
            worktree_repo=repo,
            worktree_host=host,
            worktree_daemon_id=daemon_id,
            worktree_lease_id=lease_id,
            worktree_generation=row["worktree_generation"] + 1,
            revision=int(row.get("revision") or 0) + 1,
        )
        return dict(row)

    def _fence(self, row, path, generation, lease_id, host=None, daemon_id=None) -> bool:
        return (
            row is not None
            and row["worktree_path"] == path
            and row["worktree_generation"] == generation
            and (lease_id is None or row["worktree_lease_id"] == lease_id)
            and (host is None or row["worktree_host"] == host)
            and (daemon_id is None or row["worktree_daemon_id"] == daemon_id)
        )

    async def clear_ticket_worktree_binding(
        self, ticket_id, *, worktree_path, worktree_generation, lease_id=None,
        host=None, daemon_id=None,
    ):
        row = self.rows.get(str(ticket_id))
        if not self._fence(
            row, worktree_path, worktree_generation, lease_id, host, daemon_id
        ):
            return False
        row.update(
            worktree_path="",
            worktree_branch="",
            worktree_repo="",
            worktree_host="",
            worktree_daemon_id=None,
            worktree_lease_id=None,
            worktree_cleanup_pending=False,
            worktree_cleanup_attempts=0,
            worktree_cleanup_next_retry_at=None,
            revision=int(row.get("revision") or 0) + 1,
        )
        return True

    async def record_worktree_cleanup_pending(
        self, ticket_id, *, worktree_path, worktree_generation, lease_id=None,
        delay_seconds=0.0, host=None, daemon_id=None,
    ):
        row = self.rows.get(str(ticket_id))
        if not self._fence(
            row, worktree_path, worktree_generation, lease_id, host, daemon_id
        ):
            return False
        row["worktree_cleanup_pending"] = True
        row["worktree_cleanup_attempts"] += 1
        row["worktree_cleanup_next_retry_at"] = datetime.now(timezone.utc) + timedelta(
            seconds=delay_seconds
        )
        return True

    async def claim_worktree_cleanup_pending(self, *, host, limit=20, lease_seconds=60.0):
        now = datetime.now(timezone.utc)
        due = []
        for row in self.rows.values():
            retry = row["worktree_cleanup_next_retry_at"]
            if (
                row["worktree_cleanup_pending"]
                and row["worktree_path"]
                and row["worktree_host"] == host
                and (retry is None or retry <= now)
            ):
                row["worktree_cleanup_next_retry_at"] = now + timedelta(seconds=lease_seconds)
                due.append(dict(row))
        return due[:limit]

    def make_due(self, ticket_id: str = TICKET_ID) -> None:
        self.rows[ticket_id]["worktree_cleanup_next_retry_at"] = datetime.now(
            timezone.utc
        ) - timedelta(seconds=1)

    @asynccontextmanager
    async def ticket_worktree_lock(self, ticket_id, *, timeout_seconds=30.0):
        lock = self._locks.setdefault(str(ticket_id), asyncio.Lock())
        await lock.acquire()
        self.lock_events.append(f"acquire:{ticket_id}")
        try:
            yield
        finally:
            self.lock_events.append(f"release:{ticket_id}")
            lock.release()


@pytest.fixture
def install_db(monkeypatch: pytest.MonkeyPatch):
    def _install(*tickets: dict) -> FakeTicketDb:
        fake = FakeTicketDb(*tickets)
        monkeypatch.setattr(tw, "ticket_db", fake)
        monkeypatch.setattr(worker, "ticket_db", fake)
        return fake

    return _install


def _ticket(repo: Path | None, **overrides) -> dict:
    row = {
        "ticket_id": TICKET_ID,
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "title": "t",
        "scope": "s",
        "status": "running",
        "worker_provider": "codex",
        "worker_model": None,
        "worker_thread_id": THREAD_ID,
        "worker_run_id": None,
        "worker_daemon_id": None,
        "attempts": 1,
        "revision": 3,
        "closed_at": None,
        "mandate": {"cwd": str(repo)} if repo is not None else {},
    }
    row.update(overrides)
    return row


# ------------------------------------------------- durable base and reuse


@pytest.mark.asyncio
async def test_worktree_head_is_pinned_base_and_second_call_reuses_path(
    tmp_path: Path, install_db
) -> None:
    repo, base_sha = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo))

    first = await prepare_server_ticket_worktree(_ticket(repo))
    path = Path(first["worktree_path"])
    assert first["worktree_host"] == "server"
    assert first["worktree_base_sha"] == base_sha
    assert first["worktree_branch"] == ticket_branch(TICKET_ID)
    assert first["worktree_generation"] == 1
    assert _git(path, "rev-parse", "HEAD") == base_sha
    # The binding is in the columns, never in the mandate.
    assert "ticket_worktree" not in fake.row()["mandate"]

    moved = _move_origin_main(repo)
    assert moved != base_sha
    second = await prepare_server_ticket_worktree(fake.row())

    assert Path(second["worktree_path"]) == path
    assert second["worktree_generation"] == 1  # idempotent replay, no bump
    assert _git(path, "rev-parse", "HEAD") == base_sha
    assert fake.pins == [base_sha]  # resolved and pinned once
    assert all(call["host"] == "server" for call in fake.binds)


@pytest.mark.asyncio
async def test_existing_pin_wins_over_a_moved_base_ref(tmp_path: Path, install_db) -> None:
    repo, base_sha = _repo_with_origin(tmp_path / "git")
    _move_origin_main(repo)
    fake = install_db(_ticket(repo, worktree_base_sha=base_sha, worktree_base_branch="origin/main"))

    bound = await prepare_server_ticket_worktree(fake.row())

    assert _git(Path(bound["worktree_path"]), "rev-parse", "HEAD") == base_sha
    assert fake.pins == []


@pytest.mark.asyncio
async def test_absent_checkout_is_recreated_from_the_persisted_base(
    tmp_path: Path, install_db
) -> None:
    repo, base_sha = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo))
    bound = await prepare_server_ticket_worktree(fake.row())
    path = Path(bound["worktree_path"])
    _move_origin_main(repo)
    # The checkout vanishes (host wipe); the Ticket branch survives at the base.
    subprocess.run(["rm", "-rf", str(path)], check=True)

    again = await prepare_server_ticket_worktree(fake.row())

    assert Path(again["worktree_path"]) == path
    assert _git(path, "rev-parse", "HEAD") == base_sha
    assert again["worktree_generation"] == 1


@pytest.mark.asyncio
async def test_absent_checkout_rejects_a_branch_whose_tip_differs(
    tmp_path: Path, install_db
) -> None:
    """Old WIP attached the existing branch tip and redefined base_sha from it."""
    repo, base_sha = _repo_with_origin(tmp_path / "git")
    branch = ticket_branch(TICKET_ID)
    # An unrelated root commit under the Ticket branch (not a descendant).
    tree = _git(repo, "write-tree")
    other = _git(repo, "commit-tree", tree, "-m", "unrelated")
    _git(repo, "branch", branch, other)
    install_db(_ticket(repo, worktree_base_sha=base_sha))

    with pytest.raises(TicketWorktreeBaseError, match="no longer descends") as caught:
        await prepare_server_ticket_worktree(_ticket(repo))
    # Round 3 (should-fix 4): an actionable blocker naming the operator fix.
    message = str(caught.value)
    assert f"git branch -D {branch}" in message
    assert "new Ticket" in message
    # Round 4: rebase onto the base, or save the commits before deleting;
    # never move the branch onto the base (that drops its commits).
    assert "rebase" in message and "save its commits" in message
    assert "branch -f" not in message and "reset" not in message

    assert _git(repo, "rev-parse", f"refs/heads/{branch}") == other
    expected = ticket_worktree_path(
        (tmp_path / "worktrees").resolve(), repo.resolve(), TICKET_ID
    )
    assert not expected.exists()


@pytest.mark.asyncio
async def test_ensure_requires_a_durable_base(tmp_path: Path) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    with pytest.raises(TicketWorktreeError, match="durable base_sha"):
        await ensure_ticket_worktree(repo, TICKET_ID, base_sha="")


# ------------------------------------------------------- bind fences


@pytest.mark.asyncio
async def test_reassignment_during_bind_retry_never_persists_server_path(
    tmp_path: Path, install_db
) -> None:
    """A local-to-daemon reassignment bumps revision without attempts."""
    repo, _base = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo))

    async def reassign_first(ticket_id, call):
        if len(fake.binds) == 1:
            row = fake.row()
            row["worker_daemon_id"] = "daemon-remote-1"
            row["revision"] += 1

    fake.before_bind = reassign_first

    with pytest.raises(TicketWorktreeError, match="reassigned to a daemon"):
        await prepare_server_ticket_worktree(_ticket(repo))

    assert len(fake.binds) == 1  # the retry re-checked ownership and stopped
    assert fake.row()["worktree_path"] == ""
    expected = ticket_worktree_path(
        (tmp_path / "worktrees").resolve(), repo.resolve(), TICKET_ID
    )
    assert not expected.exists()  # the unbound checkout this call made is gone


@pytest.mark.asyncio
async def test_reassignment_inside_the_bind_write_is_fenced_by_daemon_id(
    tmp_path: Path, install_db
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo))

    async def reassign_always(ticket_id, call):
        fake.row()["worker_daemon_id"] = "daemon-remote-1"

    fake.before_bind = reassign_always

    with pytest.raises(TicketWorktreeError):
        await prepare_server_ticket_worktree(_ticket(repo))
    assert fake.row()["worktree_path"] == ""


@pytest.mark.asyncio
async def test_bind_retry_after_unrelated_revision_bump_succeeds(
    tmp_path: Path, install_db
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo))

    async def bump_first(ticket_id, call):
        if len(fake.binds) == 1:
            fake.row()["revision"] += 1  # e.g. a heartbeat or summary write

    fake.before_bind = bump_first
    bound = await prepare_server_ticket_worktree(_ticket(repo))
    assert len(fake.binds) == 2
    assert bound["worktree_path"]
    assert fake.binds[1]["expected_revision"] == 4


@pytest.mark.asyncio
async def test_daemon_ticket_never_gets_a_server_path(tmp_path: Path, install_db) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo, worker_daemon_id="daemon-remote-1"))

    out = await prepare_server_ticket_worktree(fake.row())

    assert not out.get("worktree_path")
    assert fake.binds == [] and fake.pins == []
    assert not (tmp_path / "worktrees").exists() or not any(
        (tmp_path / "worktrees").rglob("tickets")
    )


@pytest.mark.asyncio
async def test_read_only_and_non_git_mandates_skip_the_checkout(
    tmp_path: Path, install_db
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo))
    read_only = _ticket(repo)
    read_only["mandate"]["read_only"] = True
    assert not (await prepare_server_ticket_worktree(read_only)).get("worktree_path")
    plain = _ticket(tmp_path)
    assert not (await prepare_server_ticket_worktree(plain)).get("worktree_path")
    assert fake.binds == []


@pytest.mark.asyncio
async def test_closed_ticket_refuses_bind(tmp_path: Path, install_db) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    install_db(_ticket(repo, status="cancelled"))
    with pytest.raises(TicketWorktreeError, match="closed"):
        await prepare_server_ticket_worktree(_ticket(repo))


@pytest.mark.asyncio
async def test_bind_and_release_serialise_on_the_ticket_lock(
    tmp_path: Path, install_db
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo))
    await prepare_server_ticket_worktree(fake.row())
    assert fake.lock_events == [f"acquire:{TICKET_ID}", f"release:{TICKET_ID}"]


# ---------------------------------------------- launch paths (cwd install)


def _metadata_patches(monkeypatch, repo: Path, set_spec) -> None:
    monkeypatch.setattr(
        "src.server.database.conversation.native_runtime.get_thread_metadata",
        AsyncMock(return_value={"native_runtime": {"provider": "codex", "cwd": str(repo)}}),
    )
    monkeypatch.setattr(
        "src.server.database.conversation.native_runtime.set_native_runtime_spec",
        set_spec,
    )


def _dispatcher():
    return SimpleNamespace(
        dispatch_turn=AsyncMock(return_value=SimpleNamespace(run_id="run-1")),
        cancel_thread=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
async def test_native_turn_installs_the_bound_worktree_as_cwd(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_sha = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo))
    set_spec = AsyncMock(return_value={"native_runtime": {"provider": "codex"}})
    _metadata_patches(monkeypatch, repo, set_spec)
    dispatcher = _dispatcher()

    started = await worker._start_worker_turn(
        dispatcher, _ticket(repo), TICKET_ID, THREAD_ID, "prompt", []
    )

    assert started is True
    path = fake.row()["worktree_path"]
    assert path and _git(Path(path), "rev-parse", "HEAD") == base_sha
    assert set_spec.await_args.args[1].cwd == path
    dispatcher.dispatch_turn.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "set_spec",
    [
        AsyncMock(side_effect=RuntimeError("metadata write failed")),
        AsyncMock(return_value=None),
    ],
    ids=["raises", "returns-none"],
)
async def test_cwd_install_failure_fails_the_launch(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch, set_spec
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    install_db(_ticket(repo))
    _metadata_patches(monkeypatch, repo, set_spec)
    dispatcher = _dispatcher()

    with pytest.raises(DispatchError, match="could not bind ticket worktree cwd"):
        await worker._start_worker_turn(
            dispatcher, _ticket(repo), TICKET_ID, THREAD_ID, "prompt", []
        )
    dispatcher.dispatch_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_bind_failure_fails_the_native_turn(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    install_db(_ticket(repo))
    monkeypatch.setattr(
        tw, "ensure_ticket_worktree", AsyncMock(side_effect=TicketWorktreeError("boom"))
    )
    dispatcher = _dispatcher()
    with pytest.raises(DispatchError, match="boom"):
        await worker._start_worker_turn(
            dispatcher, _ticket(repo), TICKET_ID, THREAD_ID, "prompt", []
        )
    dispatcher.dispatch_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_unavailable_worktree_requeues_the_launch_for_retry(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 4: a server-wide outage (Git timed out) never fails the Ticket."""
    repo, _base = _repo_with_origin(tmp_path / "git")
    fake = install_db(_ticket(repo))
    monkeypatch.setattr(
        tw,
        "ensure_ticket_worktree",
        AsyncMock(side_effect=TicketWorktreeUnavailable("git timed out on the server")),
    )
    dispatcher = _dispatcher()
    with pytest.raises(worker._RetryableLaunch):
        await worker._start_worker_turn(
            dispatcher, _ticket(repo), TICKET_ID, THREAD_ID, "prompt", []
        )
    dispatcher.dispatch_turn.assert_not_awaited()
    dispatcher.cancel_thread.assert_awaited_once()
    row = fake.row()
    assert row["status"] == "queued" and row["worker_thread_id"] is None
    assert not row.get("error")


@pytest.mark.asyncio
async def test_unresolvable_ticket_base_still_fails_the_launch(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    install_db(_ticket(repo))
    monkeypatch.setattr(
        tw,
        "ensure_ticket_worktree",
        AsyncMock(side_effect=TicketWorktreeBaseError("could not resolve base ref")),
    )
    dispatcher = _dispatcher()
    with pytest.raises(DispatchError, match="could not resolve base ref"):
        await worker._start_worker_turn(
            dispatcher, _ticket(repo), TICKET_ID, THREAD_ID, "prompt", []
        )
    dispatcher.dispatch_turn.assert_not_awaited()


def _isolated_launch_patches(monkeypatch, fake, published: dict, *, stop_after_prepare=True):
    from src.server.services.autonomy import cloud_runtime

    async def _publish(ticket_arg, *args, **kwargs):
        # Snapshot what the isolated launch published, and the row at that time.
        published["ticket"] = dict(ticket_arg)
        published["row_at_publish"] = dict(fake.row())
        return {
            **ticket_arg,
            "status": "running",
            "worker_thread_id": "thread-new",
            "worker_run_id": kwargs.get("run_id"),
        }

    monkeypatch.setattr(
        cloud_runtime, "cloud_worker_launch_allowed_async", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(worker, "_worker_slot_available", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_claim_effect_lease", AsyncMock(return_value=True))
    monkeypatch.setattr(worker, "_extend_effect_lease", AsyncMock(return_value=None))
    monkeypatch.setattr(worker, "_cloud_worker_prompt", AsyncMock(return_value=("p", "")))
    monkeypatch.setattr(
        worker, "_prompt_with_pending_instructions", AsyncMock(return_value=("p", []))
    )
    monkeypatch.setattr(worker, "_publish_thread_and_queue_late_instructions", _publish)
    dispatcher = SimpleNamespace(
        create_thread=AsyncMock(return_value="thread-new"),
        cancel_thread=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(worker, "ProjectDispatcher", lambda: dispatcher)
    monkeypatch.setattr(
        worker,
        "_mark_isolated_container_started",
        AsyncMock(side_effect=worker._RetryableLaunch("stop-after-prepare")),
    )
    fail = AsyncMock(return_value=None)
    monkeypatch.setattr(worker, "_fail_isolated_launch", fail)
    return dispatcher, fail


@pytest.mark.asyncio
async def test_isolated_launch_does_not_bind_a_server_checkout(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker stages its own workspace copy; a server checkout would only leak.

    Round 1 bound one here (and asserted it); the isolated runtime never runs
    in it, so the bind left a checkout behind for every Docker launch.
    """
    repo, _base_sha = _repo_with_origin(tmp_path / "git")
    fake = install_db(
        _ticket(repo, status="queued", attempts=0, revision=1, worker_thread_id=None)
    )
    published: dict = {}
    dispatcher, _fail = _isolated_launch_patches(monkeypatch, fake, published)
    ensure = AsyncMock(side_effect=AssertionError("isolated launch must not bind"))
    monkeypatch.setattr(tw, "ensure_ticket_worktree", ensure)

    effect = SimpleNamespace(payload={"ticket_id": TICKET_ID}, effect_id="e-1", attempts=1)
    with pytest.raises(worker._RetryableLaunch, match="stop-after-prepare"):
        await worker.launch_worker_effect(effect)

    ensure.assert_not_awaited()
    assert fake.binds == [] and fake.pins == []
    row = published["row_at_publish"]
    assert row["worktree_path"] == "" and row["worktree_host"] == ""
    assert not published["ticket"].get("worktree_path")
    assert not (tmp_path / "worktrees").exists() or not any(
        (tmp_path / "worktrees").rglob("tickets")
    )
    dispatcher.create_thread.assert_awaited_once()


def test_native_prompt_branch_is_the_worktree_branch() -> None:
    prompt = worker.worker_prompt(
        {"ticket_id": TICKET_ID, "title": "Fix", "scope": "Fix"},
        {"config": {}},
        native=True,
    )
    assert f"Default git branch: {ticket_branch(TICKET_ID)}\n" in prompt


# ------------------------------------------------ daemon claim worktree spec


@pytest.mark.asyncio
async def test_daemon_claim_spec_pins_base_before_returning(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base_sha = _repo_with_origin(tmp_path / "git")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REPO", str(repo))
    claimed = _ticket(None, worker_daemon_id="daemon-1", status="running")
    fake = install_db(claimed)

    first = await daemon_claim_worktree_spec(claimed)
    assert fake.row()["worktree_base_sha"] == base_sha  # durable before return
    _move_origin_main(repo)
    # A repeated claim before any bind (stale claim snapshot) keeps the base.
    second = await daemon_claim_worktree_spec(claimed)

    assert first == second == {
        "ticket_id": TICKET_ID,
        "branch": ticket_branch(TICKET_ID),
        "base_sha": base_sha,
        "base_branch": "origin/main",
        "worktree_path": "",
        "worktree_generation": 0,
    }
    assert not (tmp_path / "worktrees").exists()  # nothing created on the server


@pytest.mark.asyncio
async def test_daemon_claim_spec_returns_daemon_host_path_only(install_db) -> None:
    pinned = {"worktree_base_sha": "a" * 40, "worktree_base_branch": "origin/main"}
    daemon_row = _ticket(
        None,
        worker_daemon_id="daemon-1",
        worktree_path="/daemon/host/checkout",
        worktree_host="daemon",
        worktree_daemon_id="daemon-1",
        worktree_generation=2,
        **pinned,
    )
    install_db(daemon_row)
    spec = await daemon_claim_worktree_spec(daemon_row)
    assert spec["worktree_path"] == "/daemon/host/checkout"
    assert spec["worktree_generation"] == 2
    assert spec["base_sha"] == "a" * 40

    server_row = dict(daemon_row, worktree_path="/srv/worktrees/x", worktree_host="server")
    spec = await daemon_claim_worktree_spec(server_row)
    assert spec["worktree_path"] == ""  # never a server absolute path

    # A departed daemon's checkout lives on that daemon's host: never handed
    # to the new owner as if it were its own.
    departed_row = dict(daemon_row, worker_daemon_id="daemon-2")
    spec = await daemon_claim_worktree_spec(departed_row)
    assert spec["worktree_path"] == ""
    assert spec["worktree_generation"] == 2


@pytest.mark.asyncio
async def test_daemon_claim_spec_none_without_git_workspace(tmp_path: Path, install_db) -> None:
    fake = install_db(_ticket(tmp_path, worker_daemon_id="daemon-1"))
    assert await daemon_claim_worktree_spec(fake.row()) is None
    assert fake.pins == []


@pytest.mark.asyncio
async def test_daemon_claim_spec_fails_closed_when_base_unresolvable(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q", "-b", "main")  # no commits: nothing to resolve
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REPO", str(empty))
    fake = install_db(_ticket(None, worker_daemon_id="daemon-1"))
    # Round 3: the server-wide dispatch repository lacking the default base is
    # the server's problem, not the Ticket's.
    with pytest.raises(TicketWorktreeUnavailable, match="dispatch repository"):
        await daemon_claim_worktree_spec(fake.row())
    assert fake.row()["worktree_base_sha"] == ""


# ------------------------------- round 3: typed claim-spec failures (B6)


def test_claim_spec_error_classes_are_ticket_worktree_errors() -> None:
    assert issubclass(TicketWorktreeBaseError, TicketWorktreeError)
    assert issubclass(TicketWorktreeUnavailable, TicketWorktreeError)
    assert not issubclass(TicketWorktreeBaseError, TicketWorktreeUnavailable)
    assert not issubclass(TicketWorktreeUnavailable, TicketWorktreeBaseError)


@pytest.mark.asyncio
async def test_claim_spec_ticket_base_ref_that_does_not_resolve_is_per_ticket(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REPO", str(repo))
    fake = install_db(
        _ticket(None, worker_daemon_id="daemon-1", mandate={"base_branch": "origin/nope"})
    )
    with pytest.raises(TicketWorktreeBaseError) as caught:
        await daemon_claim_worktree_spec(fake.row())
    # Raw Git error text never reaches the exception (it may be relayed).
    assert "fatal" not in str(caught.value).lower()
    assert fake.row()["worktree_base_sha"] == ""


@pytest.mark.asyncio
async def test_claim_spec_option_like_base_ref_is_per_ticket(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REPO", str(repo))
    fake = install_db(
        _ticket(None, worker_daemon_id="daemon-1", mandate={"base_branch": "--all"})
    )
    with pytest.raises(TicketWorktreeBaseError, match="not a valid ref"):
        await daemon_claim_worktree_spec(fake.row())


@pytest.mark.asyncio
async def test_claim_spec_unreadable_mandate_repo_is_per_ticket(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ticket's own (mandate) repository that Git cannot open is its problem."""
    broken = tmp_path / "broken"
    (broken / ".git").mkdir(parents=True)  # looks like a checkout, is not one
    (broken / ".git" / "HEAD").write_text("garbage\n", encoding="utf-8")
    monkeypatch.setattr(tw, "_common_dir", AsyncMock(side_effect=TicketWorktreeError("x")))
    fake = install_db(_ticket(broken, worker_daemon_id="daemon-1"))
    with pytest.raises(TicketWorktreeBaseError):
        await daemon_claim_worktree_spec(fake.row())


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["not-a-repo", "unreadable", "git-missing"])
async def test_claim_spec_server_wide_failures_are_unavailable(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch, cause: str
) -> None:
    repo, _base = _repo_with_origin(tmp_path / "git")
    if cause == "not-a-repo":
        plain = tmp_path / "plain"
        plain.mkdir()
        monkeypatch.setenv("CONDUCTOR_DISPATCH_REPO", str(plain))
    else:
        monkeypatch.setenv("CONDUCTOR_DISPATCH_REPO", str(repo))
    if cause == "unreadable":
        monkeypatch.setattr(
            tw, "_common_dir", AsyncMock(side_effect=TicketWorktreeError("x"))
        )
    if cause == "git-missing":
        from src.server.services.autonomy import git_delivery

        def _missing() -> str:
            raise git_delivery.GitDeliveryError("no git")

        monkeypatch.setattr(git_delivery, "_git_binary", _missing)
    fake = install_db(_ticket(None, worker_daemon_id="daemon-1"))

    with pytest.raises(TicketWorktreeUnavailable):
        await daemon_claim_worktree_spec(fake.row())
    assert fake.row()["worktree_base_sha"] == ""


@pytest.mark.asyncio
async def test_git_runner_times_out_kills_git_and_is_unavailable(
    tmp_path: Path, install_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung Git is killed (with its children) instead of hanging the claim."""
    repo, _base = _repo_with_origin(tmp_path / "git")
    monkeypatch.setenv("CONDUCTOR_DISPATCH_REPO", str(repo))
    pids = tmp_path / "pids"
    hung = tmp_path / "hung-git"
    hung.write_text(
        f"#!/bin/sh\nsleep 30 &\necho $! > {pids}\nwait\n", encoding="utf-8"
    )
    hung.chmod(0o755)
    monkeypatch.setattr(tw, "_git_binary", lambda: str(hung))
    # Long enough for a loaded host to start the shell and its child, so the
    # kill is what ends the child rather than a race with its spawn.
    monkeypatch.setattr(tw, "_GIT_TIMEOUT_SECONDS", 5.0)
    fake = install_db(_ticket(None, worker_daemon_id="daemon-1"))

    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(TicketWorktreeUnavailable, match="timed out"):
        await daemon_claim_worktree_spec(fake.row())
    assert loop.time() - started < 20

    text = pids.read_text().strip() if pids.exists() else ""
    if not text:
        pytest.fail("the hung Git never started its child before the timeout")
    child = int(text)
    for _ in range(50):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("the timed-out Git's child process is still running")
