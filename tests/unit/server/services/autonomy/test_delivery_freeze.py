"""A native report's delivery is measured against the branch HEAD it froze.

A Worker reports when it is done; the branch can move before the delivery is
checked. The freeze resolves that HEAD once, records it in the Ticket mandate,
and pins the commit count and the summary to it. The summary belongs to the
freeze: it is captured with it, from the same reading of the workspace, so a
commit or an edit that arrives later cannot change what the delivery says.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import executor, host
from src.server.services.autonomy.delivery_freeze import (
    DELIVERY_FREEZE_KEY,
    DELIVERY_RESULT_KEY,
    DeliveryFreeze,
    DeliveryStateNotDurable,
    bound_freeze,
    capture_delivery_snapshot,
    delivery_state_fields,
    frozen_commit_count,
    frozen_delivery,
    frozen_head,
    frozen_state,
    measure_delivery,
    record_delivery_state,
    resolve_delivery_head,
)
from src.server.services.autonomy.git_delivery import (
    _UNSAFE_STATUS as UNKNOWN_STATUS,
    capture_git_snapshot,
    git_command_runner,
)
from tests.unit.conftest import completion_report_json

#: The question a refuted delivery shows.
GIT_NOT_DELIVERED_QUESTION = (
    "The system could not confirm a delivered commit for this Ticket. "
    "Commit the work, then retry."
)


@pytest.fixture(autouse=True)
def _trusted_temp_repo_runner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONDUCTOR_WORKER_LOCK_ROOT", str(tmp_path / "locks"))

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


def _repo(tmp_path: Path, name: str = "workspace") -> Path:
    path = tmp_path / name
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test Worker")
    _git(path, "config", "user.email", "worker@example.invalid")
    (path / "tracked.txt").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-q", "-m", "baseline")
    return path


def _unborn_repo(tmp_path: Path, name: str = "unborn") -> Path:
    """A repository whose branch exists but holds no commit yet."""
    path = tmp_path / name
    path.mkdir()
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test Worker")
    _git(path, "config", "user.email", "worker@example.invalid")
    return path


def _commit(path: Path, message: str, filename: str = "worker.txt") -> str:
    (path / filename).write_text(f"{message}\n", encoding="utf-8")
    _git(path, "add", filename)
    _git(path, "commit", "-q", "-m", message)
    return _git(path, "rev-parse", "HEAD")


class _FakeTicketDb:
    """One Ticket row in memory, with the fences ``update_ticket`` honours."""

    def __init__(
        self,
        ticket: dict[str, Any] | None = None,
        *,
        lose_writes: int = 0,
    ) -> None:
        self.ticket = ticket
        self.updates: list[dict[str, Any]] = []
        #: How many writes lose the fence before one lands.
        self.lose_writes = lose_writes

    async def get_ticket(self, ticket_id: str) -> dict[str, Any] | None:
        if self.ticket is None or str(self.ticket.get("ticket_id")) != ticket_id:
            return None
        return dict(self.ticket)

    async def update_ticket(self, ticket_id: str, **kwargs: Any) -> dict[str, Any] | None:
        self.updates.append({"ticket_id": ticket_id, **kwargs})
        if self.ticket is None:
            return None
        status = kwargs.get("expected_status")
        if status is not None and status != self.ticket.get("status"):
            return None
        revision = kwargs.get("expected_revision")
        if revision is not None and revision != self.ticket.get("revision"):
            return None
        if self.lose_writes > 0:
            # The row moved on after the read: the fenced write loses, and the
            # row stays moved so the next read sees the new revision.
            self.lose_writes -= 1
            self.ticket["revision"] = int(self.ticket.get("revision") or 0) + 1
            return None
        if "mandate" in kwargs:
            self.ticket["mandate"] = kwargs["mandate"]
            self.ticket["revision"] = int(self.ticket.get("revision") or 0) + 1
        return dict(self.ticket)


def _freeze_record(
    *,
    head: str | None,
    attempts: int = 0,
    known: bool = True,
    branch: str = "main",
    summary: str | None = "",
) -> dict[str, Any]:
    """The mandate record of a freeze, as the module writes it."""
    return DeliveryFreeze(
        head=head,
        branch=branch,
        root_id=None,
        known=known,
        thread_id="thread-1",
        run_id="run-current",
        attempt=attempts,
        summary=summary,
    ).to_record()


def _frozen_result(
    *,
    head: str | None,
    verdict: str = "delivered",
    summary: str = "",
    attempts: int = 0,
    commits: int | None = 1,
) -> dict[str, Any]:
    """The delivery result a frozen HEAD produced."""
    return {
        "version": 2,
        "thread_id": "thread-1",
        "run_id": "run-current",
        "attempt": attempts,
        "summary": summary,
        "commit_delivery": verdict,
        "baseline_clean": True,
        "frozen": True,
        "frozen_head": head,
        "commits": commits,
    }


def _v2_ticket(
    workspace: Path,
    *,
    attempts: int = 0,
    result: dict[str, Any] | None = None,
    freeze: dict[str, Any] | None = None,
    baseline: Any = None,
) -> dict[str, Any]:
    """A running Ticket whose native run wrote a version-2 delivery result."""
    snapshot = (
        baseline if baseline is not None else capture_git_snapshot(str(workspace))
    )
    mandate: dict[str, Any] = {
        "git_delivery_baseline": {
            "version": 2,
            "thread_id": "thread-1",
            "run_id": "run-current",
            "attempt": attempts,
            "cwd_id": hashlib.sha256(
                os.fsencode(os.path.realpath(workspace))
            ).hexdigest(),
            "snapshot": snapshot.to_record(),
            "pending": False,
        }
    }
    if result is not None:
        mandate["git_delivery_result"] = result
    if freeze is not None:
        mandate["git_delivery_freeze"] = freeze
    return {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "codex",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-current",
        "attempts": attempts,
        "revision": 1,
        "mandate": mandate,
    }


def _v1_ticket(
    workspace: Path,
    *,
    attempts: int = 0,
    freeze: dict[str, Any] | None = None,
    baseline: Any = None,
) -> dict[str, Any]:
    """A running Ticket on the version-1 baseline path."""
    snapshot = (
        baseline if baseline is not None else capture_git_snapshot(str(workspace))
    )
    mandate: dict[str, Any] = {
        "git_delivery_baseline": {
            "version": 1,
            "thread_id": "thread-1",
            "attempt": attempts,
            "cwd_id": "test-cwd",
            "snapshot": snapshot.to_record(),
        }
    }
    if freeze is not None:
        mandate["git_delivery_freeze"] = freeze
    return {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_provider": "codex",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-current",
        "attempts": attempts,
        "revision": 1,
        "mandate": mandate,
    }


class _NoCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, *args, **kwargs):
        return None

    async def fetchall(self):
        return [("ticket-1", "user-1", "thread-1")]


class _NoConnection:
    def cursor(self):
        return _NoCursor()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


async def _poll_native_worker(
    *,
    cwd: Path,
    assistant: str | None,
    ticket: dict[str, Any],
    apply_report: AsyncMock | None = None,
) -> dict[str, Any]:
    """Run one executor sweep and return what it settled the Ticket with.

    An empty mapping means the executor settled nothing: the report was not
    recorded, so the Ticket stays running.
    """
    dispatcher = AsyncMock()
    dispatcher.thread_run_status = AsyncMock(return_value="completed")
    dispatcher.thread_last_assistant_text = AsyncMock(return_value=assistant)
    metadata = {"native_runtime": {"provider": "codex", "cwd": str(cwd)}}
    settled: dict[str, Any] = {}

    async def _capture(**kwargs: Any) -> None:
        settled.update(kwargs)

    report = (
        apply_report
        if apply_report is not None
        else AsyncMock(side_effect=_capture)
    )
    with (
        patch("src.server.database.pool.get_db_connection", return_value=_NoConnection()),
        patch.object(executor, "ProjectDispatcher", return_value=dispatcher),
        patch.object(executor.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        patch.object(executor.ticket_db, "latest_report", AsyncMock(return_value=None)),
        patch.object(executor.ticket_db, "update_ticket", AsyncMock(return_value=None)),
        patch(
            "src.server.database.conversation.native_runtime.get_thread_metadata",
            AsyncMock(return_value=metadata),
        ),
        patch.object(executor, "apply_worker_report", report),
    ):
        await executor.poll_running_workers()
    if report.await_count == 0:
        return {}
    # A call that raised settled nothing: the Ticket stays running.
    return dict(settled)


# --- resolving the HEAD -----------------------------------------------------


def test_resolve_delivery_head_reads_the_branch_and_the_commit_it_points_at(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    head = _git(workspace, "rev-parse", "HEAD")

    freeze = resolve_delivery_head(str(workspace))

    assert freeze.resolved is True
    assert freeze.empty is False
    assert freeze.head == head
    assert freeze.branch == "main"
    assert freeze.root_id is not None


def test_resolve_delivery_head_has_no_head_for_a_workspace_that_is_not_a_repo(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()

    freeze = resolve_delivery_head(str(plain))

    assert freeze.resolved is False
    assert freeze.head is None
    assert freeze.known is False


def test_an_unborn_branch_freezes_as_a_known_empty_state(tmp_path: Path) -> None:
    unborn = _unborn_repo(tmp_path)

    freeze = resolve_delivery_head(str(unborn))

    # The branch was read and simply holds no commit yet. That is a resolved
    # freeze of an empty branch, not a HEAD that could not be read: a commit
    # that lands later is not this delivery's.
    assert freeze.known is True
    assert freeze.resolved is True
    assert freeze.empty is True
    assert freeze.head is None


def test_an_empty_freeze_is_read_back_from_the_mandate(tmp_path: Path) -> None:
    unborn = _unborn_repo(tmp_path)
    freeze = resolve_delivery_head(str(unborn))
    mandate = {DELIVERY_FREEZE_KEY: freeze.to_record()}

    restored = frozen_state(mandate)

    assert restored is not None
    assert restored.resolved is True
    assert restored.empty is True
    assert restored.head is None
    # An empty freeze names no commit, so it has no frozen head to point at.
    assert frozen_head(mandate) is None


def test_a_record_of_another_version_is_not_read_as_a_freeze() -> None:
    assert (
        DeliveryFreeze.from_record({"version": 2, "known": True, "head": "a" * 40})
        is None
    )
    assert DeliveryFreeze.from_record("not a record") is None
    assert (
        DeliveryFreeze.from_record(
            {"version": 1, "known": True, "head": "not an object id"}
        )
        is None
    )


def test_a_freeze_of_another_attempt_is_not_reused() -> None:
    head = "a" * 40
    mandate = {
        DELIVERY_FREEZE_KEY: DeliveryFreeze(
            head=head,
            branch="main",
            known=True,
            thread_id="thread-1",
            run_id="run-current",
            attempt=1,
        ).to_record()
    }

    assert (
        bound_freeze(
            mandate,
            thread_id="thread-1",
            run_id="run-current",
            attempt=1,
        )
        is not None
    )
    # A later attempt measures against its own HEAD, never against the one
    # an earlier attempt froze.
    assert (
        bound_freeze(mandate, thread_id="thread-1", run_id="run-current", attempt=2)
        is None
    )
    assert (
        bound_freeze(mandate, thread_id="thread-2", run_id="run-current", attempt=1)
        is None
    )
    assert (
        bound_freeze(mandate, thread_id="thread-1", run_id="run-old", attempt=1)
        is None
    )


# --- measuring against the frozen HEAD ---------------------------------------


def test_the_commit_count_covers_only_the_commits_of_the_frozen_head(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    freeze = resolve_delivery_head(str(workspace))
    # A commit that lands after the report was written.
    _commit(workspace, "later change", "later.txt")

    delivery = frozen_delivery(str(workspace), baseline, freeze)

    assert delivery.verdict == "delivered"
    assert delivery.commits == 1
    assert delivery.freeze is not None
    assert delivery.freeze.head == freeze.head


def test_the_live_branch_carries_more_commits_than_the_frozen_one(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    freeze = resolve_delivery_head(str(workspace))
    later = _commit(workspace, "later change", "later.txt")

    assert frozen_commit_count(str(workspace), baseline.head, freeze.head) == 1
    assert frozen_commit_count(str(workspace), baseline.head, later) == 2


def test_the_summary_is_diffed_against_the_frozen_revision(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    (workspace / "tracked.txt").write_text("b1\nb2\n", encoding="utf-8")
    _git(workspace, "commit", "-q", "-am", "worker change")
    # The Worker left a change uncommitted: the summary describes it.
    (workspace / "tracked.txt").write_text("b1\nb2\nb3\n", encoding="utf-8")
    freeze, snapshot = capture_delivery_snapshot(str(workspace))

    frozen = frozen_delivery(str(workspace), baseline, freeze, current=snapshot)

    assert frozen.summary is not None
    assert " M tracked.txt" in frozen.summary
    # The summary diffs the worktree against the frozen commit, so it holds
    # the change the frozen commit did not.
    assert "1 insertion(+)" in frozen.summary
    # The summary names the revision it was diffed against, so a reader can
    # tell which commit the delivery was measured from.
    assert f"git diff --stat {freeze.head}:" in frozen.summary


def test_later_commits_and_edits_cannot_change_the_frozen_summary(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    freeze, snapshot = capture_delivery_snapshot(str(workspace))
    frozen = frozen_delivery(str(workspace), baseline, freeze, current=snapshot)
    assert frozen.summary == ""

    # A commit and an edit land after the freeze was taken.
    _commit(workspace, "later change", "later.txt")
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    # The frozen delivery keeps the summary it was measured with: the live
    # workspace is not the one the freeze was taken from.
    again = frozen_delivery(
        str(workspace), baseline, freeze, summary=frozen.summary
    )

    assert again.summary == frozen.summary == ""
    assert again.verdict == frozen.verdict == "delivered"
    assert again.commits == frozen.commits == 1


def test_a_measurement_without_the_freeze_workspace_state_reads_no_live_workspace(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    freeze = resolve_delivery_head(str(workspace))
    # The worktree is dirty now, after the freeze was taken.
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    delivery = frozen_delivery(str(workspace), baseline, freeze)

    # The delivery says the workspace state is unknown instead of describing
    # the dirt that arrived after the freeze.
    assert delivery.summary == UNKNOWN_STATUS
    assert delivery.verdict == "delivered"


def test_a_commit_that_lands_after_the_freeze_does_not_change_the_measurement(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    freeze, snapshot = capture_delivery_snapshot(str(workspace))
    before = frozen_delivery(str(workspace), baseline, freeze, current=snapshot)

    _commit(workspace, "later change", "later.txt")
    after = frozen_delivery(str(workspace), baseline, freeze, summary=before.summary)

    # The measurement is the same before and after the later commit: it
    # covers the commits reachable from the frozen HEAD, and no others.
    assert before.verdict == after.verdict == "delivered"
    assert before.commits == after.commits == 1


def test_a_first_commit_after_the_report_is_not_the_delivery(tmp_path: Path) -> None:
    unborn = _unborn_repo(tmp_path)
    baseline = capture_git_snapshot(str(unborn))
    freeze, snapshot = capture_delivery_snapshot(str(unborn))
    # The branch held no commit when the report arrived.
    assert freeze.empty is True

    # A first commit lands after the report.
    (unborn / "worker.txt").write_text("late\n", encoding="utf-8")
    _git(unborn, "add", "worker.txt")
    _git(unborn, "commit", "-q", "-m", "late commit")

    delivery = frozen_delivery(str(unborn), baseline, freeze, current=snapshot)

    # The frozen range ends nowhere: the delivery covered nothing, and the
    # commit that landed later is not this delivery's.
    assert delivery.verdict == "missing"
    assert delivery.commits is None


def test_a_head_that_could_not_be_read_leaves_the_delivery_unknown(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    # The HEAD could not be read: the freeze names no commit, and it is not
    # a branch that held none.
    freeze = DeliveryFreeze(head=None, branch="main", root_id=None, known=False)

    delivery = frozen_delivery(str(workspace), baseline, freeze)

    # There is no frozen commit to measure against, so the delivery is
    # unknown: it is neither confirmed nor refuted.
    assert delivery.verdict == "unknown"
    assert delivery.commits is None
    assert delivery.summary == UNKNOWN_STATUS


@pytest.mark.asyncio
async def test_a_retry_reuses_an_unresolved_freeze_and_stays_unknown(
    tmp_path: Path,
) -> None:
    """A retry of a report whose HEAD was unreadable does not read the live branch."""
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    unresolved = DeliveryFreeze(
        head=None,
        branch="main",
        known=False,
        thread_id="thread-1",
        run_id="run-current",
        attempt=1,
    )
    mandate = {DELIVERY_FREEZE_KEY: unresolved.to_record()}

    reused = bound_freeze(
        mandate, thread_id="thread-1", run_id="run-current", attempt=1
    )
    assert reused is not None
    assert reused.resolved is False

    # A commit lands after the report; the retry must not count it.
    _commit(workspace, "after the report")
    delivery = await measure_delivery(str(workspace), baseline=baseline, freeze=reused)

    assert delivery.verdict == "unknown"


def test_a_later_commit_cannot_confirm_a_delivery_with_an_unresolved_freeze(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    freeze = DeliveryFreeze(head=None, branch="main", root_id=None, known=False)

    # A commit lands after the HEAD could not be read.
    _commit(workspace, "worker change")

    delivery = frozen_delivery(str(workspace), baseline, freeze)

    # The delivery was never frozen, so the later commit cannot confirm it.
    assert delivery.verdict == "unknown"
    assert delivery.commits is None


def test_a_workspace_that_is_not_a_git_repository_has_no_delivery_to_check(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    baseline = capture_git_snapshot(str(plain))

    delivery = frozen_delivery(str(plain), baseline, DeliveryFreeze())

    # There is no Git workspace here, so nothing is confirmed or refuted.
    assert delivery.verdict == "not_checked"
    assert delivery.summary is None


@pytest.mark.asyncio
async def test_a_reused_freeze_without_a_summary_cannot_describe_the_workspace(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    worker_head = _commit(workspace, "worker change")
    # A freeze recorded before the summary belonged to it.
    freeze = DeliveryFreeze(
        head=worker_head, branch="main", root_id=None, known=True
    )
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    delivery = await measure_delivery(str(workspace), baseline=baseline, freeze=freeze)

    # The commit the delivery measured is still the frozen one, but the live
    # workspace is not the one the freeze was taken from, so the delivery says
    # the workspace state is unknown instead of describing the dirt that
    # arrived later.
    assert delivery.verdict == "delivered"
    assert delivery.commits == 1
    assert delivery.summary == UNKNOWN_STATUS


@pytest.mark.asyncio
async def test_the_measurement_is_the_same_when_it_is_taken_again(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    freeze, snapshot = capture_delivery_snapshot(str(workspace))
    _commit(workspace, "later change", "later.txt")

    first = await measure_delivery(str(workspace), baseline=baseline, freeze=freeze)
    second = await measure_delivery(str(workspace), baseline=baseline, freeze=freeze)

    # A retry measures the same commit, never a later one.
    assert first.verdict == second.verdict == "delivered"
    assert first.commits == second.commits == 1


# --- recording the frozen state ---------------------------------------------


@pytest.mark.asyncio
async def test_record_delivery_state_writes_the_freeze_and_the_result_together(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    head = _git(workspace, "rev-parse", "HEAD")
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "revision": 4,
        "mandate": {"keep": "me"},
    }
    db = _FakeTicketDb(ticket)
    freeze = resolve_delivery_head(str(workspace))
    delivery = frozen_delivery(
        str(workspace),
        capture_git_snapshot(str(workspace)),
        freeze,
    )
    fields = delivery_state_fields(
        freeze,
        delivery,
        thread_id="thread-1",
        run_id="run-1",
        attempt=2,
        baseline_clean=True,
    )

    recorded = await record_delivery_state(
        "ticket-1",
        fields,
        thread_id="thread-1",
        run_id="run-1",
        attempt=2,
        ticket_db=db,
    )

    assert recorded is True
    mandate = ticket["mandate"]
    assert mandate["keep"] == "me"
    freeze_record = mandate[DELIVERY_FREEZE_KEY]
    assert freeze_record["version"] == 1
    assert freeze_record["known"] is True
    assert freeze_record["head"] == head
    assert freeze_record["branch"] == "main"
    assert freeze_record["thread_id"] == "thread-1"
    assert freeze_record["run_id"] == "run-1"
    assert freeze_record["attempt"] == 2
    assert frozen_head(mandate) == head
    # The delivery rides along with the freeze it was measured from.
    result = mandate[DELIVERY_RESULT_KEY]
    assert result["version"] == 2
    assert result["frozen"] is True
    assert result["frozen_head"] == head
    assert result["thread_id"] == "thread-1"
    assert result["run_id"] == "run-1"
    assert result["attempt"] == 2
    # The write is fenced by the row it read, so a concurrent write is never
    # overwritten by the freeze and never loses to it.
    assert db.updates[0]["expected_status"] == "running"
    assert db.updates[0]["expected_thread_id"] == "thread-1"
    assert db.updates[0]["expected_run_id"] == "run-1"
    assert db.updates[0]["expected_revision"] == 4


@pytest.mark.asyncio
async def test_record_delivery_state_retries_a_lost_write_with_the_same_head(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    head = _git(workspace, "rev-parse", "HEAD")
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "revision": 5,
        "mandate": {},
    }
    # The row moved on after the freeze was resolved but before it was
    # written: the fenced write loses once and lands on the retry.
    db = _FakeTicketDb(ticket, lose_writes=1)
    freeze = resolve_delivery_head(str(workspace))
    fields = delivery_state_fields(
        freeze,
        frozen_delivery(str(workspace), capture_git_snapshot(str(workspace)), freeze),
        thread_id="thread-1",
        run_id="run-1",
        attempt=0,
    )

    recorded = await record_delivery_state(
        "ticket-1",
        fields,
        thread_id="thread-1",
        run_id="run-1",
        attempt=0,
        ticket_db=db,
    )

    assert recorded is True
    assert len(db.updates) == 2
    # The retry carries the same frozen HEAD, so the delivery is still
    # measured against the commit it was measured from the first time.
    assert ticket["mandate"][DELIVERY_FREEZE_KEY]["head"] == head
    assert ticket["mandate"][DELIVERY_RESULT_KEY]["frozen_head"] == head


@pytest.mark.asyncio
async def test_record_delivery_state_reports_a_write_that_never_lands(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "revision": 5,
        "mandate": {},
    }
    db = _FakeTicketDb(ticket, lose_writes=99)
    freeze = resolve_delivery_head(str(workspace))
    fields = delivery_state_fields(
        freeze,
        frozen_delivery(str(workspace), capture_git_snapshot(str(workspace)), freeze),
        thread_id="thread-1",
        run_id="run-1",
        attempt=0,
    )

    recorded = await record_delivery_state(
        "ticket-1",
        fields,
        thread_id="thread-1",
        run_id="run-1",
        attempt=0,
        ticket_db=db,
    )

    # The state is not durable, so nothing claims it is.
    assert recorded is False
    assert DELIVERY_FREEZE_KEY not in ticket["mandate"]
    assert DELIVERY_RESULT_KEY not in ticket["mandate"]


@pytest.mark.asyncio
async def test_record_delivery_state_reports_a_ticket_that_is_gone(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    db = _FakeTicketDb(None)
    freeze = resolve_delivery_head(str(workspace))

    recorded = await record_delivery_state(
        "ticket-1",
        {DELIVERY_FREEZE_KEY: freeze.to_record()},
        thread_id="thread-1",
        ticket_db=db,
    )

    assert recorded is False


# --- the native report path -------------------------------------------------


def _native_ticket() -> dict[str, Any]:
    """The running Ticket a native Worker reports against."""
    return {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-current",
        "attempts": 1,
        "revision": 3,
        "mandate": {},
    }


async def _run_native_report_path(
    *,
    workspace: Path,
    baseline: Any,
    ticket: dict[str, Any],
    sync: Any = None,
) -> dict[str, Any]:
    """Run the native report path and return the mandate it wrote."""
    from src.server.handlers.chat.native_run import _finish_native_worker_delivery

    async def update_ticket(ticket_id, **kwargs):
        ticket["mandate"] = kwargs["mandate"]
        ticket["revision"] += 1
        return dict(ticket)

    with (
        patch(
            "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
            AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.database.autonomy.tickets.update_ticket",
            update_ticket,
        ),
        patch(
            "src.server.handlers.chat.native_run.sync_workspace_after_turn",
            sync if sync is not None else AsyncMock(return_value=(0, 0)),
        ),
    ):
        await _finish_native_worker_delivery(
            workspace_id="workspace-1",
            native_cwd=str(workspace),
            thread_id="thread-1",
            run_id="run-current",
            attempt=1,
            baseline=baseline,
            baseline_error=False,
            run_completed=True,
        )
    return ticket["mandate"]


@pytest.mark.asyncio
async def test_a_commit_that_lands_during_the_workspace_sync_is_not_the_delivery(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    worker_head = _commit(workspace, "worker change")
    ticket = _native_ticket()

    async def sync(workspace_id, cwd):
        # A commit lands while the workspace manifest is synced.
        _commit(workspace, "sync-time commit", "sync.txt")
        return (0, 0)

    mandate = await _run_native_report_path(
        workspace=workspace,
        baseline=baseline,
        ticket=ticket,
        sync=sync,
    )

    # The freeze was taken before the sync, so the commit that landed during
    # it is after the report and is not this delivery's.
    assert mandate[DELIVERY_FREEZE_KEY]["head"] == worker_head
    assert mandate[DELIVERY_FREEZE_KEY]["commits"] == 1
    assert mandate[DELIVERY_RESULT_KEY]["frozen_head"] == worker_head
    assert mandate[DELIVERY_RESULT_KEY]["commits"] == 1
    assert mandate[DELIVERY_RESULT_KEY]["commit_delivery"] == "delivered"


@pytest.mark.asyncio
async def test_a_snapshot_os_error_still_runs_the_workspace_sync(
    tmp_path: Path,
) -> None:
    """The sync moved after the measurement; a non-Git failure must not skip it."""
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    sync = AsyncMock(return_value=(0, 0))

    with patch(
        "src.server.services.autonomy.delivery_freeze.measure_delivery",
        AsyncMock(side_effect=OSError("unreadable entry")),
    ):
        with pytest.raises(OSError):
            await _run_native_report_path(
                workspace=workspace,
                baseline=baseline,
                ticket=_native_ticket(),
                sync=sync,
            )

    sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_later_commit_and_edit_cannot_change_the_recorded_delivery(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    ticket = _native_ticket()

    mandate = await _run_native_report_path(
        workspace=workspace, baseline=baseline, ticket=ticket
    )
    assert mandate[DELIVERY_RESULT_KEY]["summary"] == ""

    # A commit and an edit land after the report.
    _commit(workspace, "later change", "later.txt")
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    # The recorded delivery is unchanged, and the settlement reads it: the
    # work that arrived after the report cannot block it.
    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Committed result."),
        ticket=_v2_ticket(
            workspace,
            attempts=1,
            baseline=baseline,
            result=mandate[DELIVERY_RESULT_KEY],
            freeze=mandate[DELIVERY_FREEZE_KEY],
        ),
    )

    assert report["state"] == "ready"
    assert mandate[DELIVERY_RESULT_KEY]["summary"] == ""


@pytest.mark.asyncio
async def test_a_head_that_could_not_be_read_leaves_the_recorded_delivery_unknown(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    _commit(workspace, "worker change")
    ticket = _native_ticket()
    unresolved = DeliveryFreeze(head=None, branch="main", root_id=None, known=False)

    with patch(
        "src.server.services.autonomy.delivery_freeze.capture_delivery_snapshot",
        return_value=(unresolved, capture_git_snapshot(str(workspace))),
    ):
        mandate = await _run_native_report_path(
            workspace=workspace, baseline=baseline, ticket=ticket
        )

    # The HEAD could not be read, so the delivery is not frozen and not
    # confirmed.
    assert mandate[DELIVERY_FREEZE_KEY]["known"] is False
    assert mandate[DELIVERY_RESULT_KEY]["commit_delivery"] == "unknown"

    # A commit lands after the report: it cannot confirm a delivery that was
    # never frozen.
    _commit(workspace, "later change", "later.txt")
    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Committed result."),
        ticket=_v2_ticket(
            workspace,
            attempts=1,
            baseline=baseline,
            result=mandate[DELIVERY_RESULT_KEY],
            freeze=mandate[DELIVERY_FREEZE_KEY],
        ),
    )

    assert report["state"] == "blocked"
    assert report["completion"].disagreement_reason == "git_not_delivered"


@pytest.mark.asyncio
async def test_the_native_report_path_freezes_the_head_when_the_report_arrives(
    tmp_path: Path,
) -> None:
    from src.server.handlers.chat.native_run import _finish_native_worker_delivery

    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    head = _commit(workspace, "worker change")
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-current",
        "attempts": 1,
        "revision": 3,
        "mandate": {},
    }

    async def update_ticket(ticket_id, **kwargs):
        ticket["mandate"] = kwargs["mandate"]
        ticket["revision"] += 1
        return dict(ticket)

    with (
        patch(
            "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
            AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.database.autonomy.tickets.update_ticket",
            update_ticket,
        ),
        patch(
            "src.server.handlers.chat.native_run.sync_workspace_after_turn",
            AsyncMock(return_value=(0, 0)),
        ),
    ):
        await _finish_native_worker_delivery(
            workspace_id="workspace-1",
            native_cwd=str(workspace),
            thread_id="thread-1",
            run_id="run-current",
            attempt=1,
            baseline=baseline,
            baseline_error=False,
            run_completed=True,
        )

    mandate = ticket["mandate"]
    freeze = mandate[DELIVERY_FREEZE_KEY]
    assert freeze["head"] == head
    assert freeze["known"] is True
    assert freeze["empty"] is False
    assert freeze["thread_id"] == "thread-1"
    assert freeze["run_id"] == "run-current"
    assert freeze["attempt"] == 1
    # The Worker committed once, and the frozen HEAD is that commit.
    assert freeze["commits"] == 1
    result = mandate[DELIVERY_RESULT_KEY]
    assert result["version"] == 2
    assert result["frozen"] is True
    assert result["frozen_head"] == head
    assert result["commit_delivery"] == "delivered"
    assert result["commits"] == 1


@pytest.mark.asyncio
async def test_a_commit_that_lands_after_the_report_is_not_the_delivery(
    tmp_path: Path,
) -> None:
    from src.server.handlers.chat.native_run import _finish_native_worker_delivery

    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    worker_head = _commit(workspace, "worker change")
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-current",
        "attempts": 1,
        "revision": 3,
        "mandate": {},
    }

    async def update_ticket(ticket_id, **kwargs):
        ticket["mandate"] = kwargs["mandate"]
        ticket["revision"] += 1
        return dict(ticket)

    with (
        patch(
            "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
            AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.database.autonomy.tickets.update_ticket",
            update_ticket,
        ),
        patch(
            "src.server.handlers.chat.native_run.sync_workspace_after_turn",
            AsyncMock(return_value=(0, 0)),
        ),
    ):
        await _finish_native_worker_delivery(
            workspace_id="workspace-1",
            native_cwd=str(workspace),
            thread_id="thread-1",
            run_id="run-current",
            attempt=1,
            baseline=baseline,
            baseline_error=False,
            run_completed=True,
        )

    # A commit lands after the report was recorded.
    later_head = _commit(workspace, "later change", "later.txt")

    mandate = ticket["mandate"]
    assert mandate[DELIVERY_FREEZE_KEY]["head"] == worker_head
    assert mandate[DELIVERY_FREEZE_KEY]["commits"] == 1
    assert mandate[DELIVERY_RESULT_KEY]["frozen_head"] == worker_head
    assert mandate[DELIVERY_RESULT_KEY]["commits"] == 1
    assert later_head != worker_head

    # The executor settles from the recorded result: the later commit is not
    # this delivery's, and it does not change the delivery either.
    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(
            summary="Committed result.", commits=(worker_head,)
        ),
        ticket=_v2_ticket(
            workspace,
            attempts=1,
            baseline=baseline,
            result=mandate[DELIVERY_RESULT_KEY],
            freeze=mandate[DELIVERY_FREEZE_KEY],
        ),
    )

    assert report["state"] == "ready"


@pytest.mark.asyncio
async def test_the_v2_path_settles_against_the_recorded_frozen_result(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    head = _commit(workspace, "worker change")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Committed result."),
        ticket=_v2_ticket(
            workspace,
            result=_frozen_result(head=head),
            freeze=_freeze_record(head=head),
        ),
    )

    # The version-2 path reads the frozen result the native run recorded; it
    # is not bypassed by the version-2 baseline.
    assert report["state"] == "ready"
    assert report["attention_reason"] is None


@pytest.mark.asyncio
async def test_the_v2_path_refuses_a_result_whose_freeze_is_missing(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    head = _commit(workspace, "worker change")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Committed result."),
        ticket=_v2_ticket(
            workspace,
            result=_frozen_result(head=head),
        ),
    )

    # A result that names a frozen commit, with no freeze to match it, says
    # nothing about which commit it measured: the delivery is not confirmed.
    assert report["state"] == "blocked"
    assert report["attention_reason"] == "git_status_unavailable"
    assert report["completion"].disagreement is True
    assert report["completion"].disagreement_reason == "git_not_delivered"


@pytest.mark.asyncio
async def test_the_v2_path_refuses_a_result_whose_freeze_is_another_commit(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    head = _commit(workspace, "worker change")
    other = _git(workspace, "rev-parse", "HEAD~1")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Committed result."),
        ticket=_v2_ticket(
            workspace,
            result=_frozen_result(head=head),
            freeze=_freeze_record(head=other),
        ),
    )

    # The freeze in the mandate is not the one the result was measured from.
    assert report["state"] == "blocked"
    assert report["attention_reason"] == "git_status_unavailable"
    assert report["completion"].disagreement_reason == "git_not_delivered"


@pytest.mark.asyncio
async def test_the_v2_path_keeps_reading_a_result_written_before_the_freeze(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    head = _commit(workspace, "worker change")
    result = _frozen_result(head=head)
    # A result written by the previous code carries no freeze at all.
    result.pop("frozen")
    result.pop("frozen_head")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Committed result."),
        ticket=_v2_ticket(workspace, result=result),
    )

    # An in-flight result keeps the behavior it was written with.
    assert report["state"] == "ready"


@pytest.mark.asyncio
async def test_a_first_commit_after_the_report_is_not_delivered_by_the_executor(
    tmp_path: Path,
) -> None:
    from src.server.handlers.chat.native_run import _finish_native_worker_delivery

    unborn = _unborn_repo(tmp_path)
    baseline = capture_git_snapshot(str(unborn))
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-current",
        "attempts": 1,
        "revision": 3,
        "mandate": {},
    }

    async def update_ticket(ticket_id, **kwargs):
        ticket["mandate"] = kwargs["mandate"]
        ticket["revision"] += 1
        return dict(ticket)

    with (
        patch(
            "src.server.database.autonomy.tickets.get_ticket_by_worker_thread",
            AsyncMock(return_value=ticket),
        ),
        patch(
            "src.server.database.autonomy.tickets.update_ticket",
            update_ticket,
        ),
        patch(
            "src.server.handlers.chat.native_run.sync_workspace_after_turn",
            AsyncMock(return_value=(0, 0)),
        ),
    ):
        await _finish_native_worker_delivery(
            workspace_id="workspace-1",
            native_cwd=str(unborn),
            thread_id="thread-1",
            run_id="run-current",
            attempt=1,
            baseline=baseline,
            baseline_error=False,
            run_completed=True,
        )

    mandate = ticket["mandate"]
    # The empty freeze is kept: it is a resolved freeze of a branch that held
    # no commit, and the record says so.
    assert mandate[DELIVERY_FREEZE_KEY]["known"] is True
    assert mandate[DELIVERY_FREEZE_KEY]["empty"] is True
    assert mandate[DELIVERY_FREEZE_KEY]["head"] is None
    assert mandate[DELIVERY_RESULT_KEY]["frozen"] is True
    assert mandate[DELIVERY_RESULT_KEY]["frozen_head"] is None
    assert mandate[DELIVERY_RESULT_KEY]["commit_delivery"] == "missing"

    # A first commit lands after the report.
    (unborn / "worker.txt").write_text("late\n", encoding="utf-8")
    _git(unborn, "add", "worker.txt")
    _git(unborn, "commit", "-q", "-m", "late commit")
    late_head = _git(unborn, "rev-parse", "HEAD")

    report = await _poll_native_worker(
        cwd=unborn,
        assistant=completion_report_json(
            summary="Committed result.", commits=(late_head,)
        ),
        ticket=_v2_ticket(
            unborn,
            attempts=1,
            baseline=baseline,
            result=mandate[DELIVERY_RESULT_KEY],
            freeze=mandate[DELIVERY_FREEZE_KEY],
        ),
    )

    # The Worker claims a commit, and the frozen empty branch refutes it:
    # the commit is not this delivery's.
    assert report["state"] == "blocked"
    assert report["completion"].disagreement is True
    assert report["completion"].disagreement_reason == "git_not_delivered"
    assert report["question"] == GIT_NOT_DELIVERED_QUESTION


@pytest.mark.asyncio
async def test_the_executor_reuses_the_persisted_freeze_on_a_retry(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    worker_head = _commit(workspace, "worker change")
    # The freeze the report arrival took, before this retry.
    freeze = _freeze_record(head=worker_head)
    # A commit and an edit land between the report and this sweep.
    _commit(workspace, "later change", "later.txt")
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Committed result."),
        ticket=_v1_ticket(workspace, freeze=freeze, baseline=baseline),
    )

    fields = report["mandate_fields"]
    # The sweep measures the frozen commit, not the later one.
    assert fields[DELIVERY_RESULT_KEY]["frozen_head"] == worker_head
    assert fields[DELIVERY_RESULT_KEY]["commits"] == 1
    assert fields[DELIVERY_FREEZE_KEY]["head"] == worker_head
    # The summary is the one the freeze carries, not the live workspace's:
    # the edit that arrived after the report is not this delivery's.
    assert fields[DELIVERY_RESULT_KEY]["summary"] == ""
    assert report["state"] == "ready"


@pytest.mark.asyncio
async def test_the_executor_records_the_freeze_it_took(tmp_path: Path) -> None:
    workspace = _repo(tmp_path)
    baseline = capture_git_snapshot(str(workspace))
    worker_head = _commit(workspace, "worker change")

    report = await _poll_native_worker(
        cwd=workspace,
        assistant=completion_report_json(summary="Committed result."),
        ticket=_v1_ticket(workspace, baseline=baseline),
    )

    fields = report["mandate_fields"]
    # No freeze was recorded before, so this sweep takes one and records it
    # with the delivery it produced.
    assert fields[DELIVERY_FREEZE_KEY]["head"] == worker_head
    assert fields[DELIVERY_FREEZE_KEY]["thread_id"] == "thread-1"
    assert fields[DELIVERY_FREEZE_KEY]["run_id"] == "run-current"
    assert fields[DELIVERY_FREEZE_KEY]["attempt"] == 0
    assert fields[DELIVERY_RESULT_KEY]["frozen"] is True
    assert fields[DELIVERY_RESULT_KEY]["frozen_head"] == worker_head
    assert report["state"] == "ready"


# --- durability --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_ticket_whose_delivery_state_cannot_be_recorded_is_not_settled(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    freeze = resolve_delivery_head(str(workspace))
    fields = delivery_state_fields(
        freeze,
        frozen_delivery(str(workspace), capture_git_snapshot(str(workspace)), freeze),
        thread_id="thread-1",
        run_id="run-current",
        attempt=1,
    )
    ticket = {
        "ticket_id": "ticket-1",
        "status": "running",
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-current",
        "attempts": 1,
        "revision": 3,
        "mandate": {},
    }

    with (
        patch.object(host.ticket_db, "get_ticket", AsyncMock(return_value=ticket)),
        # The write never lands: the row moved on and stayed moved.
        patch.object(host.ticket_db, "update_ticket", AsyncMock(return_value=None)),
        patch.object(
            host.ticket_db,
            "record_worker_report",
            AsyncMock(return_value={"id": "report-1"}),
        ) as record_report,
    ):
        with pytest.raises(DeliveryStateNotDurable):
            await host.apply_worker_report(
                ticket_id="ticket-1",
                state="ready",
                summary="Committed result.",
                expected_thread_id="thread-1",
                expected_run_id="run-current",
                mandate_fields=fields,
            )

    # The report is not recorded, so the Ticket is not settled on a delivery
    # that says nothing about which commit it measured.
    record_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_executor_leaves_the_report_unsettled_when_the_state_is_lost(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path)
    head = _commit(workspace, "worker change")
    apply_report = AsyncMock(side_effect=DeliveryStateNotDurable("ticket-1"))

    with patch.object(executor, "_release_settled_worktree", AsyncMock()) as release:
        report = await _poll_native_worker(
            cwd=workspace,
            assistant=completion_report_json(summary="Committed result."),
            ticket=_v2_ticket(
                workspace,
                result=_frozen_result(head=head),
                freeze=_freeze_record(head=head),
            ),
            apply_report=apply_report,
        )

    # The delivery state did not land, so nothing was settled: the Ticket
    # stays running and the next sweep measures it again.
    assert report == {}
    apply_report.assert_awaited_once()
    release.assert_not_awaited()
