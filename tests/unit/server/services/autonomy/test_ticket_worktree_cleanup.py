"""Server-host Ticket worktree cleanup: fenced release, durable retry, stops.

Real tmp git repositories plus an in-memory fake of the contract's
persistence API; no database, no network.

On core/main these fail: there is no ``ticket_worktree`` module (import error)
and no cleanup at all, so a completed or cancelled Ticket's checkout would
never be removed nor retried; the WIP mandate version instead deleted the
checkout a cancelled Ticket's live run was still using, and wrote a failed
cleanup's old binding back over a newer one.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.server.services.autonomy import executor, scheduler, stop as stop_mod
from src.server.services.autonomy import ticket_worktree as tw
from src.server.services.autonomy.ticket_worktree import (
    TicketWorktreeError,
    prepare_server_ticket_worktree,
    release_server_ticket_worktree,
    sweep_ticket_worktree_cleanups,
)

TICKET_ID = "cccccccc-1111-2222-3333-444444444444"
OPEN = ("queued", "running", "waiting", "blocked", "awaiting_clarification")
BINDING_KEYS = (
    "worktree_path",
    "worktree_branch",
    "worktree_repo",
    "worktree_host",
    "worktree_generation",
    "worktree_base_sha",
)


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"})
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for key, value in (
        ("user.name", "Ticket Worktree"),
        ("user.email", "ticket-worktree@example.invalid"),
        ("core.hooksPath", "/dev/null"),
        ("core.fsmonitor", "false"),
    ):
        _git(repo, "config", key, value)
    (repo / "README").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README")
    _git(repo, "commit", "-qm", "base")
    return repo


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("GIT_"):
            monkeypatch.delenv(key, raising=False)
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CONDUCTOR_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    # Runner probes: default to "no live run, no pending stop".
    monkeypatch.setattr(tw, "_thread_has_active_run", AsyncMock(return_value=False))
    monkeypatch.setattr(tw, "_stop_verification", AsyncMock(return_value=None))
    # No process lives in a checkout unless a test says so (the real OS probe
    # is exercised in test_ticket_worktree_hardening.py).
    monkeypatch.setattr(tw, "_checkout_process_ids", AsyncMock(return_value=set()))
    # No reassignment history unless a test records one.
    monkeypatch.setattr(
        "src.server.database.autonomy.lines.list_ticket_events", AsyncMock(return_value=[])
    )


class FakeTicketDb:
    """Contract persistence fake: fenced bind/clear/pending and a real lock."""

    OPEN_STATUSES = OPEN
    MAX_WORKTREE_CLEANUP_ATTEMPTS = 20

    def __init__(self, row: dict) -> None:
        self.rows = {
            row["ticket_id"]: {
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
                "worktree_stale_cleanups": [],
                **row,
            }
        }
        self._lock = asyncio.Lock()
        self.pending_calls: list[dict] = []
        self.clear_calls: list[dict] = []
        self.cancel_calls: list[dict] = []
        self.bind_calls: list[dict] = []

    def row(self) -> dict:
        return self.rows[TICKET_ID]

    async def get_ticket(self, ticket_id):
        row = self.rows.get(ticket_id)
        return dict(row) if row else None

    async def pin_ticket_worktree_base(self, ticket_id, *, base_sha, base_branch):
        row = self.rows[ticket_id]
        if not row["worktree_base_sha"]:
            row.update(worktree_base_sha=base_sha, worktree_base_branch=base_branch)
        return dict(row)

    async def bind_ticket_worktree(
        self, ticket_id, *, worktree_path, branch, base_sha, repo, host,
        expected_revision=None, daemon_id=None, lease_id=None, replace_generation=None,
    ):
        self.bind_calls.append(
            {"path": worktree_path, "host": host, "replace_generation": replace_generation}
        )
        row = self.rows[ticket_id]
        # Round-3 contract: a departed daemon's binding (its daemon is no
        # longer the worker) is replaced only at the generation the caller
        # read; the old binding becomes a stale-cleanup note in the same write.
        departed = (
            replace_generation is not None
            and row["worktree_host"] == "daemon"
            and bool(row["worktree_path"])
            and row["worktree_generation"] == replace_generation
            and row["worktree_daemon_id"] != row.get("worker_daemon_id")
        )
        if departed and not (
            row["status"] not in OPEN
            or row["worktree_base_sha"] != base_sha
            or (host == "server" and row.get("worker_daemon_id") is not None)
            or (expected_revision is not None and row["revision"] != expected_revision)
        ):
            row["worktree_stale_cleanups"] = [
                *row["worktree_stale_cleanups"],
                {
                    "daemon_id": row["worktree_daemon_id"],
                    "worktree_path": row["worktree_path"],
                    "worktree_generation": row["worktree_generation"],
                    "lease_id": row["worktree_lease_id"],
                },
            ]
            row.update(
                worktree_path="",
                worktree_host="",
                worktree_daemon_id=None,
                worktree_lease_id=None,
                worktree_cleanup_pending=False,
                worktree_cleanup_attempts=0,
                worktree_cleanup_next_retry_at=None,
            )
        # Round-2 contract: a pending cleanup refuses only a different path or
        # host; the same path by the same host takes the checkout over.
        takeover = (
            row["worktree_cleanup_pending"]
            and row["worktree_path"] == worktree_path
            and row["worktree_host"] == host
        )
        if (
            row["status"] not in OPEN
            or (row["worktree_cleanup_pending"] and not takeover)
            or row["worktree_base_sha"] != base_sha
            or row["worktree_path"] not in ("", worktree_path)
            or (host == "server" and row.get("worker_daemon_id") is not None)
            or (expected_revision is not None and row["revision"] != expected_revision)
        ):
            return None
        if takeover:
            row.update(
                worktree_cleanup_pending=False,
                worktree_cleanup_attempts=0,
                worktree_cleanup_next_retry_at=None,
            )
        if row["worktree_path"] == worktree_path and row["worktree_host"] == host:
            return dict(row)
        row.update(
            worktree_path=worktree_path,
            worktree_branch=branch,
            worktree_repo=repo,
            worktree_host=host,
            worktree_generation=row["worktree_generation"] + 1,
            revision=row["revision"] + 1,
        )
        return dict(row)

    def _fence(
        self, row, path, generation, *, lease_id=None, host=None, daemon_id=None
    ) -> bool:
        return (
            row["worktree_path"] == path
            and row["worktree_generation"] == generation
            and (lease_id is None or row["worktree_lease_id"] == lease_id)
            and (host is None or row["worktree_host"] == host)
            and (daemon_id is None or row["worktree_daemon_id"] == daemon_id)
        )

    async def clear_ticket_worktree_binding(
        self, ticket_id, *, worktree_path, worktree_generation, lease_id=None,
        host=None, daemon_id=None,
    ):
        self.clear_calls.append(
            {"path": worktree_path, "generation": worktree_generation,
             "lease_id": lease_id, "host": host, "daemon_id": daemon_id}
        )
        row = self.rows[ticket_id]
        if not self._fence(
            row, worktree_path, worktree_generation,
            lease_id=lease_id, host=host, daemon_id=daemon_id,
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
            revision=row["revision"] + 1,
        )
        return True

    async def record_worktree_cleanup_pending(
        self, ticket_id, *, worktree_path, worktree_generation, lease_id=None,
        delay_seconds=0.0, host=None, daemon_id=None,
    ):
        self.pending_calls.append(
            {"path": worktree_path, "generation": worktree_generation,
             "delay": delay_seconds, "host": host, "daemon_id": daemon_id,
             "kind": "record"}
        )
        row = self.rows[ticket_id]
        if not self._fence(
            row, worktree_path, worktree_generation,
            lease_id=lease_id, host=host, daemon_id=daemon_id,
        ):
            return False
        row["worktree_cleanup_pending"] = True
        row["worktree_cleanup_attempts"] += 1
        row["worktree_cleanup_next_retry_at"] = datetime.now(timezone.utc) + timedelta(
            seconds=delay_seconds
        )
        return True

    async def reschedule_worktree_cleanup_pending(
        self, ticket_id, *, worktree_path, worktree_generation, delay_seconds
    ):
        self.pending_calls.append(
            {"path": worktree_path, "generation": worktree_generation,
             "delay": delay_seconds, "host": None, "daemon_id": None,
             "kind": "reschedule"}
        )
        row = self.rows[ticket_id]
        if not row["worktree_cleanup_pending"] or not self._fence(
            row, worktree_path, worktree_generation
        ):
            return False
        row["worktree_cleanup_attempts"] += 1
        row["worktree_cleanup_next_retry_at"] = datetime.now(timezone.utc) + timedelta(
            seconds=delay_seconds
        )
        return True

    async def cancel_worktree_cleanup_pending(
        self, ticket_id, *, worktree_path, worktree_generation, host, daemon_id=None
    ):
        self.cancel_calls.append(
            {"path": worktree_path, "generation": worktree_generation, "host": host}
        )
        row = self.rows[ticket_id]
        orphaned = (
            row.get("worker_daemon_id") is not None
            if host == "server"
            else row.get("worker_daemon_id") != row["worktree_daemon_id"]
        )
        if (
            not row["worktree_cleanup_pending"]
            or not self._fence(
                row, worktree_path, worktree_generation, host=host,
                daemon_id=daemon_id if host == "daemon" else None,
            )
            or row["status"] not in OPEN
            or row.get("closed_at")
            or orphaned
        ):
            return False
        row.update(
            worktree_cleanup_pending=False,
            worktree_cleanup_attempts=0,
            worktree_cleanup_next_retry_at=None,
        )
        return True

    async def claim_worktree_cleanup_pending(self, *, host, limit=20, lease_seconds=60.0):
        now = datetime.now(timezone.utc)
        out = []
        for row in self.rows.values():
            retry = row["worktree_cleanup_next_retry_at"]
            if (
                row["worktree_cleanup_pending"]
                and row["worktree_path"]
                and row["worktree_host"] == host
                and (retry is None or retry <= now)
            ):
                row["worktree_cleanup_next_retry_at"] = now + timedelta(seconds=lease_seconds)
                out.append(dict(row))
        return out[:limit]

    def make_due(self) -> None:
        self.row()["worktree_cleanup_next_retry_at"] = datetime.now(timezone.utc) - timedelta(
            seconds=1
        )

    @asynccontextmanager
    async def ticket_worktree_lock(self, ticket_id, *, timeout_seconds=30.0):
        async with self._lock:
            yield


def _row(repo: Path, **overrides) -> dict:
    row = {
        "ticket_id": TICKET_ID,
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "status": "running",
        "closed_at": None,
        "worker_daemon_id": None,
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "attempts": 1,
        "revision": 1,
        "mandate": {"cwd": str(repo)},
    }
    row.update(overrides)
    return row


@pytest_asyncio.fixture
async def bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A running server Ticket with its checkout bound (generation 1)."""
    repo = _repo(tmp_path / "git")
    fake = FakeTicketDb(_row(repo))
    monkeypatch.setattr(tw, "ticket_db", fake)
    await prepare_server_ticket_worktree(fake.row())
    assert fake.row()["worktree_generation"] == 1
    return fake, repo, Path(fake.row()["worktree_path"])


def _end(fake: FakeTicketDb, status: str = "completed") -> dict:
    fake.row()["status"] = status
    return dict(fake.row())


def _binding(row: dict) -> tuple:
    return tuple(row[key] for key in BINDING_KEYS)


@pytest.mark.asyncio
async def test_release_removes_and_clears_but_keeps_the_base_pin(bound) -> None:
    fake, repo, path = bound
    base = fake.row()["worktree_base_sha"]
    snapshot = _end(fake)

    assert await release_server_ticket_worktree(snapshot, reason="close") == "removed"

    assert not path.exists()
    row = fake.row()
    assert row["worktree_path"] == "" and row["worktree_generation"] == 1
    assert row["worktree_base_sha"] == base
    # The Ticket branch (the delivery) survives the removal of its checkout.
    assert _git(repo, "branch", "--list", "conductor/ticket/*")


@pytest.mark.asyncio
async def test_stale_cleanup_after_newer_bind_leaves_newer_checkout(bound) -> None:
    """Attempt 1's late cleanup must not remove or clear attempt 2's checkout."""
    fake, _repo_path, path = bound
    stale = _end(fake, "cancelled")
    assert await release_server_ticket_worktree(stale, reason="stop") == "removed"
    # The Ticket is resumed and relaunched: generation 2 at the same path.
    fake.row()["status"] = "running"
    await prepare_server_ticket_worktree(fake.row())
    assert fake.row()["worktree_generation"] == 2 and path.exists()
    (path / "work.txt").write_text("attempt 2\n", encoding="utf-8")
    # It ends again, then attempt 1's duplicate cleanup (old snapshot) lands.
    fake.row()["status"] = "cancelled"
    before = _binding(fake.row())

    assert await release_server_ticket_worktree(stale, reason="stop") == "skipped"

    assert (path / "work.txt").read_text(encoding="utf-8") == "attempt 2\n"
    assert _binding(fake.row()) == before


@pytest.mark.asyncio
async def test_stale_cleanup_waiting_on_the_lock_sees_the_newer_bind(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interleaving: a stale release queued behind a running bind."""
    fake, _repo_path, path = bound
    stale = _end(fake, "cancelled")
    assert await release_server_ticket_worktree(stale, reason="stop") == "removed"
    fake.row()["status"] = "running"

    entered = asyncio.Event()
    proceed = asyncio.Event()
    real_ensure = tw.ensure_ticket_worktree

    async def slow_ensure(*args, **kwargs):
        entered.set()
        await proceed.wait()
        return await real_ensure(*args, **kwargs)

    monkeypatch.setattr(tw, "ensure_ticket_worktree", slow_ensure)
    bind_task = asyncio.create_task(prepare_server_ticket_worktree(fake.row()))
    await entered.wait()  # the bind holds the Ticket lock
    # Attempt 1's duplicate cleanup (same path, generation 1) arrives now.
    release_task = asyncio.create_task(
        release_server_ticket_worktree(stale, reason="stop")
    )
    await asyncio.sleep(0.05)
    assert not release_task.done()  # serialised behind the bind
    proceed.set()
    await bind_task
    fake.row()["status"] = "cancelled"  # ended, so only the fence protects it

    assert await release_task == "skipped"
    assert path.exists()
    assert fake.row()["worktree_generation"] == 2
    assert fake.row()["worktree_path"] == str(path)


@pytest.mark.asyncio
async def test_failed_removal_records_pending_without_touching_the_binding(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _repo_path, path = bound
    snapshot = _end(fake)
    before = _binding(fake.row())
    monkeypatch.setattr(
        tw, "remove_ticket_worktree", AsyncMock(side_effect=TicketWorktreeError("busy"))
    )

    assert await release_server_ticket_worktree(snapshot, reason="close") == "pending"

    row = fake.row()
    assert _binding(row) == before
    assert row["worktree_cleanup_pending"] is True
    assert row["worktree_cleanup_attempts"] == 1
    assert row["worktree_cleanup_next_retry_at"] > datetime.now(timezone.utc)
    assert fake.pending_calls[-1]["generation"] == 1
    assert path.exists()


@pytest.mark.asyncio
async def test_later_sweep_retries_and_clears(bound, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _repo_path, path = bound
    snapshot = _end(fake)
    failing = AsyncMock(side_effect=TicketWorktreeError("busy"))
    real_remove = tw.remove_ticket_worktree
    monkeypatch.setattr(tw, "remove_ticket_worktree", failing)
    assert await release_server_ticket_worktree(snapshot, reason="close") == "pending"

    # Not due yet: the sweep leaves it alone.
    assert await sweep_ticket_worktree_cleanups() == 0
    assert failing.await_count == 1

    monkeypatch.setattr(tw, "remove_ticket_worktree", real_remove)
    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 1

    row = fake.row()
    assert not path.exists()
    assert row["worktree_path"] == "" and row["worktree_cleanup_pending"] is False
    assert row["worktree_cleanup_attempts"] == 0
    # Nothing left to retry.
    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 0


@pytest.mark.asyncio
async def test_failed_retry_reschedules_with_backoff(bound, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _repo_path, _path = bound
    _end(fake)
    monkeypatch.setattr(
        tw, "remove_ticket_worktree", AsyncMock(side_effect=TicketWorktreeError("busy"))
    )
    assert await release_server_ticket_worktree(fake.row(), reason="close") == "pending"
    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 0
    assert fake.row()["worktree_cleanup_attempts"] == 2
    assert fake.pending_calls[-1]["delay"] > fake.pending_calls[0]["delay"]


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["stop_pending", "active_run", "live_heartbeat", "live_lease"])
async def test_cancelled_ticket_whose_run_may_live_is_not_removed(
    bound, monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    fake, _repo_path, path = bound
    snapshot = _end(fake, "cancelled")
    if probe == "stop_pending":
        monkeypatch.setattr(tw, "_stop_verification", AsyncMock(return_value="pending"))
    elif probe == "active_run":
        monkeypatch.setattr(tw, "_thread_has_active_run", AsyncMock(return_value=True))
    elif probe == "live_heartbeat":
        fake.row()["mandate"] = {
            **fake.row()["mandate"],
            "cloud_worker": {
                "isolated": True,
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    else:
        fake.row()["worker_lease_id"] = "lease-1"
        fake.row()["worker_lease_expires_at"] = datetime.now(timezone.utc) + timedelta(
            minutes=5
        )
    before = _binding(fake.row())

    assert await release_server_ticket_worktree(snapshot, reason="stop") == "pending"

    assert path.exists()
    assert _binding(fake.row()) == before
    assert fake.row()["worktree_cleanup_pending"] is True


@pytest.mark.asyncio
async def test_sweep_removes_once_the_runner_has_ended(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _repo_path, path = bound
    _end(fake, "cancelled")
    active = AsyncMock(return_value=True)
    monkeypatch.setattr(tw, "_thread_has_active_run", active)
    assert await release_server_ticket_worktree(fake.row(), reason="stop") == "pending"
    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 0 and path.exists()

    active.return_value = False
    fake.make_due()
    assert await sweep_ticket_worktree_cleanups() == 1
    assert not path.exists()


@pytest.mark.asyncio
async def test_open_ticket_keeps_its_checkout_for_resume(bound) -> None:
    fake, _repo_path, path = bound
    fake.row()["status"] = "blocked"
    (path / "dirty.txt").write_text("wip\n", encoding="utf-8")
    assert await release_server_ticket_worktree(fake.row(), reason="report") == "skipped"
    assert (path / "dirty.txt").exists()
    assert fake.row()["worktree_cleanup_pending"] is False


@pytest.mark.asyncio
async def test_daemon_host_binding_is_never_removed_by_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _repo(tmp_path / "git")
    host_dir = tmp_path / "daemon-host-checkout"
    host_dir.mkdir()
    fake = FakeTicketDb(
        _row(
            repo,
            status="completed",
            worker_daemon_id="daemon-1",
            worktree_path=str(host_dir),
            worktree_host="daemon",
            worktree_generation=1,
            worktree_branch="b",
        )
    )
    monkeypatch.setattr(tw, "ticket_db", fake)
    assert await release_server_ticket_worktree(fake.row(), reason="close") == "skipped"
    assert host_dir.exists()
    assert fake.pending_calls == []


@pytest.mark.asyncio
async def test_dirty_checkout_is_archived_before_removal(bound) -> None:
    fake, repo, path = bound
    (path / "README").write_text("dirty\n", encoding="utf-8")
    _end(fake)
    assert await release_server_ticket_worktree(fake.row(), reason="close") == "removed"
    assert not path.exists()
    archives = list((path.parents[2] / "recovery-artifacts").rglob("*"))
    assert archives, "dirty Ticket state must be archived, not discarded"


# ---------------------------------------------------------------- wiring


@pytest.mark.asyncio
async def test_scheduler_pass_runs_the_cleanup_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def mark(name, *_a, **_k):
        calls.append(name)
        return 0

    for name in (
        "enqueue_due_polls",
        "recover_stale_evals",
        "start_due_evals",
        "ensure_launch_effects",
        "poll_running_workers",
        "run_due_schedules",
        "watchdog_sweep",
        "sweep_worktree_cleanups",
    ):
        monkeypatch.setattr(scheduler, name, lambda *a, _n=name, **k: mark(_n, *a, **k))
    monkeypatch.setattr(
        "src.server.database.autonomy.daemons.recover_expired_daemon_leases", AsyncMock()
    )
    monkeypatch.setattr(
        "src.server.database.autonomy.stops.expire_stop_requests", AsyncMock()
    )
    monkeypatch.setattr(
        "src.server.services.refactor.memory_consolidation.run_due_memory_consolidations",
        AsyncMock(),
        raising=False,
    )
    instance = scheduler.AutonomyScheduler.__new__(scheduler.AutonomyScheduler)
    instance._collect_expired_output = AsyncMock()
    instance._enqueue_main_agent_advances = AsyncMock()
    instance.main_agent_orchestrator = None
    instance.memory_store = None

    await instance._run_pass()

    assert "sweep_worktree_cleanups" in calls
    assert calls.index("sweep_worktree_cleanups") > calls.index("poll_running_workers")


@pytest.mark.asyncio
async def test_executor_sweep_absorbs_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tw, "sweep_ticket_worktree_cleanups", AsyncMock(side_effect=RuntimeError("db down"))
    )
    assert await executor.sweep_worktree_cleanups() == 0


@pytest.mark.asyncio
async def test_settled_report_releases_the_server_checkout(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _repo_path, path = bound
    monkeypatch.setattr(executor, "ticket_db", fake)
    _end(fake, "completed")
    await executor._release_settled_worktree(TICKET_ID)
    assert not path.exists()
    assert fake.row()["worktree_path"] == ""


@pytest.mark.asyncio
async def test_backend_stop_releases_only_its_own_snapshot(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop result carrying attempt 1's snapshot cannot remove attempt 2."""
    fake, _repo_path, path = bound
    stale_snapshot = dict(fake.row(), status="cancelled", worktree_generation=1)
    # Attempt 2 has since been bound (cleared and rebound at the same path).
    fake.row().update(worktree_path="", worktree_host="")
    await prepare_server_ticket_worktree(fake.row())
    fake.row()["status"] = "cancelled"
    assert fake.row()["worktree_generation"] == 2

    monkeypatch.setattr(
        stop_mod.stop_db,
        "request_stop",
        AsyncMock(
            return_value={
                "outcome": "ok",
                "ticket": stale_snapshot,
                "stop": {"mode": "backend", "verification": "not_applicable"},
            }
        ),
    )
    cancel = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "src.server.services.projects.dispatch.ProjectDispatcher.cancel_thread", cancel
    )
    await stop_mod.request_ticket_stop(
        user_id="user-1", ticket_id=TICKET_ID, timeout_seconds=1.0
    )
    assert path.exists()
    assert fake.row()["worktree_generation"] == 2 and fake.row()["worktree_path"]

    # The stop of the current attempt does release it.
    current = dict(fake.row())
    stop_mod.stop_db.request_stop.return_value = {
        "outcome": "ok",
        "ticket": current,
        "stop": {"mode": "backend", "verification": "not_applicable"},
    }
    await stop_mod.request_ticket_stop(
        user_id="user-1", ticket_id=TICKET_ID, timeout_seconds=1.0
    )
    assert not path.exists()


@pytest.mark.asyncio
async def test_daemon_stop_ack_never_touches_a_server_checkout(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replaying an old verified ack must not load and delete the current binding."""
    fake, _repo_path, path = bound
    _end(fake, "cancelled")
    monkeypatch.setattr(
        stop_mod.stop_db,
        "acknowledge_stop",
        AsyncMock(return_value={"outcome": "ok", "stop": {"verification": "verified"}}),
    )
    release = AsyncMock()
    monkeypatch.setattr(tw, "release_server_ticket_worktree", release)
    await stop_mod.acknowledge_ticket_stop(
        daemon_id="daemon-1",
        ticket_id=TICKET_ID,
        lease_id="old-lease",
        process_gone=True,
        evidence=None,
    )
    release.assert_not_awaited()
    assert path.exists()


# --- reassignment hands the checkout over ---------------------------------


@pytest.mark.asyncio
async def test_local_to_daemon_reassignment_releases_the_server_checkout(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Ticket stays open, but its server copy is no longer its tree.

    Without the release the daemon's bind is refused for as long as the server
    binding lives (host mismatch), so the reassigned Ticket could never run.
    """
    from src.server.services.autonomy import ownership

    fake, _repo, path = bound
    monkeypatch.setattr(ownership, "ticket_db", fake)
    # The reassignment's audit event (written with it) records the move away
    # from the server; without one the old run is unknown (round 4, S3).
    monkeypatch.setattr(
        "src.server.database.autonomy.lines.list_ticket_events",
        AsyncMock(
            return_value=[
                {"action": "reassigned", "from_owner": "cloud", "detail": {}}
            ]
        ),
    )
    row = fake.row()
    row.update(status="queued", worker_daemon_id="daemon-1")
    assert tw.orphaned_server_checkout(row)

    out = await ownership._hand_over_ticket_worktree(dict(row), from_daemon_id=None)

    assert not path.exists()
    assert out["worktree_path"] == "" and out["worktree_host"] == ""
    # The durable base survives, so the daemon's checkout starts from it.
    assert out["worktree_base_sha"]


@pytest.mark.asyncio
async def test_open_server_ticket_still_keeps_its_checkout(
    bound, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.server.services.autonomy import ownership

    fake, _repo, path = bound
    monkeypatch.setattr(ownership, "ticket_db", fake)
    fake.row()["status"] = "queued"
    out = await ownership._hand_over_ticket_worktree(dict(fake.row()), from_daemon_id=None)
    assert path.exists() and out["worktree_path"] == str(path)
    assert await release_server_ticket_worktree(fake.row(), reason="sweep") == "skipped"


@pytest.mark.asyncio
async def test_orphaned_checkout_waits_for_a_live_isolated_run(bound) -> None:
    fake, _repo, path = bound
    row = fake.row()
    row.update(
        status="queued",
        worker_daemon_id="daemon-1",
        mandate={
            **row["mandate"],
            "cloud_worker": {
                "isolated": True,
                "heartbeat_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    )
    assert await release_server_ticket_worktree(row, reason="reassign") == "pending"
    assert path.exists() and fake.row()["worktree_cleanup_pending"]


@pytest.mark.asyncio
async def test_daemon_to_server_reassignment_drops_the_daemon_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The old host's copy is out of reach; only the binding is dropped."""
    from src.server.services.autonomy import ownership

    fake = FakeTicketDb(
        _row(
            tmp_path,
            status="queued",
            worker_daemon_id=None,
            worktree_path="/daemon/host/tickets/t",
            worktree_generation=3,
            worktree_host="daemon",
            worktree_daemon_id="daemon-1",
            worktree_lease_id="lease-1",
            worktree_base_sha="a" * 40,
        )
    )
    monkeypatch.setattr(ownership, "ticket_db", fake)
    out = await ownership._hand_over_ticket_worktree(
        dict(fake.row()), from_daemon_id="daemon-1", from_lease_id="lease-1"
    )
    assert out["worktree_path"] == "" and out["worktree_generation"] == 3
    assert out["worktree_base_sha"] == "a" * 40
    # Fenced on the old daemon's host, id and released lease.
    assert fake.clear_calls[-1]["host"] == "daemon"
    assert fake.clear_calls[-1]["daemon_id"] == "daemon-1"
    assert fake.clear_calls[-1]["lease_id"] == "lease-1"
