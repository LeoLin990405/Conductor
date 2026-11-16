"""The wake for a Worker that stopped moving.

A Worker that has made no commit and filed no completion report is not doing
work the Ticket can see. The sweep decides when that silence is long enough
to be worth a wake, and the wake itself is a notice the Worker reads while
it runs.

The wake has to reach the Worker: a notice that only lands in the database
is a wake that never happened. The route depends on who owns the Worker's
runtime, and each of those routes is checked here against the real delivery
path, not against a fake of it.
"""

from __future__ import annotations

import re

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy import watchdog, worker, worker_idle
from src.server.services.autonomy.git_delivery import capture_git_snapshot
from src.server.services.autonomy.worker_idle import (
    IDLE_WAKE_ENV,
    IDLE_WAKE_MANDATE_KEY,
    IdleWakeSweep,
    decide_idle_wake,
    idle_state_fragment,
    idle_wake_minutes,
    read_idle_state,
    sweep_worker_idle_wakes,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.name", "test")
    _git(path, "config", "user.email", "test@example.invalid")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-qm", "base")
    return path


def _commit(path: Path, name: str = "b.txt") -> None:
    (path / name).write_text("b\n", encoding="utf-8")
    _git(path, "add", name)
    _git(path, "commit", "-qm", "work")


def _ticket(workspace: Path | None = None, **overrides) -> dict:
    ticket = {
        "ticket_id": "ticket-1",
        "user_id": "user-1",
        "workspace_id": "ws-1",
        "status": "running",
        "revision": 3,
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "attempts": 0,
        "stall_seconds": 3600,
        "mandate": {},
    }
    if workspace is not None:
        ticket["worktree_host"] = "server"
        ticket["worktree_path"] = str(workspace)
    ticket.update(overrides)
    return ticket


class _Dispatcher:
    """The dispatcher the delivery path talks to."""

    def __init__(self, status: str = "steering_accepted", run_id: str = "run-2"):
        self.dispatched: list[dict] = []
        self.status = status
        self.run_id = run_id

    async def dispatch_turn(self, **kwargs):
        self.dispatched.append(kwargs)
        return SimpleNamespace(
            thread_id=str(kwargs.get("thread_id") or ""),
            run_id=self.run_id,
            status=self.status,
        )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("0", None),
        ("-5", None),
        ("abc", None),
        ("30", 30),
        (" 45 ", 45),
    ],
)
def test_the_switch_is_off_by_default(value, expected) -> None:
    environ = {} if value is None else {IDLE_WAKE_ENV: value}

    assert idle_wake_minutes(environ) == expected


def test_the_switch_reads_the_environment_by_default(monkeypatch) -> None:
    monkeypatch.delenv(IDLE_WAKE_ENV, raising=False)
    assert idle_wake_minutes() is None

    monkeypatch.setenv(IDLE_WAKE_ENV, "12")
    assert idle_wake_minutes() == 12


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"threshold_minutes": None}, "disabled"),
        ({"threshold_minutes": 0}, "disabled"),
        ({"has_report": True}, "reported"),
        ({"idle_seconds": 60}, "not_yet_idle"),
        ({"last_wake_at": NOW - timedelta(minutes=5)}, "recently_woken"),
    ],
)
def test_a_worker_that_is_not_silent_is_left_alone(overrides, reason) -> None:
    arguments = {
        "threshold_minutes": 30,
        "idle_seconds": 3600,
        "has_commit": False,
        "has_report": False,
        "now": NOW,
    }
    arguments.update(overrides)

    decision = decide_idle_wake(**arguments)

    assert decision.wake is False
    assert decision.reason == reason


def test_a_silent_worker_is_woken() -> None:
    decision = decide_idle_wake(
        threshold_minutes=30,
        idle_seconds=3600,
        has_commit=False,
        has_report=False,
        last_wake_at=NOW - timedelta(minutes=45),
        now=NOW,
    )

    assert decision.wake is True
    assert decision.reason == "idle"
    assert decision.idle_seconds == 3600


def test_an_unreadable_commit_state_does_not_keep_a_worker_asleep() -> None:
    """A daemon owns its checkout on another host: the state is unknowable.

    The report is the other half of the signal, and the notice asks a stuck
    Worker to say what blocks it, so an unknown commit state is not a reason
    to stay silent.
    """
    decision = decide_idle_wake(
        threshold_minutes=30,
        idle_seconds=3600,
        has_commit=None,
        has_report=False,
        now=NOW,
    )

    assert decision.wake is True
    assert decision.reason == "idle"


def test_a_recent_commit_is_not_a_wake_but_a_silent_one_is() -> None:
    """The window runs from the last commit, not from the start of the run.

    A Worker that committed and then went still is silent again once the
    window passes: the commit is what says when it last moved.
    """
    recent = decide_idle_wake(
        threshold_minutes=30,
        idle_seconds=60,
        has_commit=True,
        has_report=False,
        now=NOW,
    )
    stale = decide_idle_wake(
        threshold_minutes=30,
        idle_seconds=3600,
        has_commit=True,
        has_report=False,
        now=NOW,
    )

    assert recent.wake is False
    assert recent.reason == "committed"
    assert stale.wake is True
    assert stale.reason == "idle"


def test_the_state_round_trips() -> None:
    fragment = idle_state_fragment(last_wake_at=NOW, wakes=2)

    state = read_idle_state({IDLE_WAKE_MANDATE_KEY: fragment[IDLE_WAKE_MANDATE_KEY]})
    assert state.wakes == 2
    assert state.last_wake_at == NOW
    assert state.pending_input_id == ""
    assert read_idle_state({}) == worker_idle.IdleWakeState()
    assert read_idle_state(None) == worker_idle.IdleWakeState()
    assert read_idle_state({IDLE_WAKE_MANDATE_KEY: {"version": 9}}) == (
        worker_idle.IdleWakeState()
    )


def test_the_state_carries_the_notice_it_owes() -> None:
    fragment = idle_state_fragment(
        last_wake_at=NOW, wakes=1, pending_input_id="input-1"
    )

    state = read_idle_state({IDLE_WAKE_MANDATE_KEY: fragment[IDLE_WAKE_MANDATE_KEY]})

    assert state.pending_input_id == "input-1"


def _commit_check_ticket(workspace: Path, baseline, **overrides) -> dict:
    ticket = _ticket(workspace)
    ticket["mandate"] = {
        "git_delivery_baseline": {
            "version": 2,
            "thread_id": "thread-1",
            "attempt": 0,
            "snapshot": baseline.to_record(),
        }
    }
    ticket.update(overrides)
    return ticket


def test_the_commit_check_reads_the_checkout(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")
    baseline = capture_git_snapshot(str(workspace))
    ticket = _commit_check_ticket(workspace, baseline)

    assert worker_idle._worker_has_commit(ticket) is False

    _commit(workspace)

    assert worker_idle._worker_has_commit(ticket) is True


def test_the_commit_check_is_unknown_without_a_baseline(tmp_path: Path) -> None:
    workspace = _repo(tmp_path / "repo")

    assert worker_idle._worker_has_commit(_ticket(workspace)) is None
    assert worker_idle._worker_has_commit(_ticket()) is None


def test_an_earlier_attempts_commit_does_not_silence_the_current_run(
    tmp_path: Path,
) -> None:
    """The silence is measured for the run that is running now.

    An earlier attempt committed. The Worker was retried, and the retry has
    committed nothing: the earlier commit says nothing about this run, so the
    Worker may still be stuck.
    """
    workspace = _repo(tmp_path / "repo")
    first = capture_git_snapshot(str(workspace))
    _commit(workspace)
    # The retry starts here: its own baseline is the commit the earlier
    # attempt made.
    retry = capture_git_snapshot(str(workspace))
    ticket = _commit_check_ticket(
        workspace,
        retry,
        worker_run_id="run-2",
        attempts=1,
        mandate={
            "git_delivery_baseline": {
                "version": 2,
                "thread_id": "thread-1",
                "attempt": 1,
                "snapshot": retry.to_record(),
            },
            # The Ticket's delivery origin counts the whole Ticket: the
            # earlier attempt's commit is part of it.
            "git_delivery_origin": {
                "version": 1,
                "known": True,
                "head": first.head,
                "root_id": first.root_id,
            },
        },
    )

    assert worker_idle._worker_has_commit(ticket) is False


def test_the_baseline_of_another_attempt_is_not_the_current_run(
    tmp_path: Path,
) -> None:
    workspace = _repo(tmp_path / "repo")
    baseline = capture_git_snapshot(str(workspace))
    ticket = _commit_check_ticket(
        workspace,
        baseline,
        attempts=2,
        mandate={
            "git_delivery_baseline": {
                "version": 2,
                "thread_id": "thread-1",
                "attempt": 1,
                "snapshot": baseline.to_record(),
            }
        },
    )

    assert worker_idle._worker_has_commit(ticket) is None


@pytest.mark.asyncio
async def test_the_silence_runs_from_the_last_commit(tmp_path: Path) -> None:
    """A Worker that committed and then went still is silent again.

    The commit is what says when the branch last moved, so the window opens
    at the commit and not at the start of the run.
    """
    workspace = _repo(tmp_path / "repo")
    baseline = capture_git_snapshot(str(workspace))
    ticket = _commit_check_ticket(workspace, baseline)
    committed_at = datetime.now(timezone.utc) - timedelta(minutes=45)
    with (
        patch.object(worker_idle, "_worker_has_commit", lambda _ticket: True),
        patch.object(worker_idle, "_worker_last_commit_at", lambda _ticket: committed_at),
    ):
        silence, committed = await worker_idle._worker_silence(
            ticket, now=datetime.now(timezone.utc)
        )

    assert committed is True
    # The run has been quiet for an hour, but the commit is only 45 minutes
    # old: the silence is measured from the commit.
    assert 44 * 60 < silence <= 46 * 60


_UNSET = object()


async def _sweep(
    rows: list[dict],
    *,
    has_commit: bool | None = False,
    report: dict | None = None,
    minutes: int | None = 30,
    dispatcher: _Dispatcher | None = None,
    attempt_started_at: datetime | None | object = _UNSET,
    delivery_error: Exception | None = None,
    add_input_result: dict | None = None,
    committed_at: datetime | None = None,
    dispatch_status: str = "steering_accepted",
    pages: dict[int, list[dict]] | None = None,
    page_size: int | None = None,
    marked: list[str] | None = None,
) -> tuple[IdleWakeSweep, list[dict], list[dict], list[dict]]:
    """Run the idle sweep over ``rows`` and report what it did.

    The fakes move the Ticket's revision the way the database does, and they
    refuse a mandate write whose ``expected_revision`` is not the revision
    they hold now. An input row and a delivered stamp both bump it, so a
    fence taken before either write refuses the wake it belongs to.

    ``attempt_started_at`` defaults to two hours before ``NOW``. Passing
    None leaves the attempt's start unknown, which is not the Ticket clock.
    """
    records: list[dict] = []
    inputs: list[dict] = []
    gate = dispatcher or _Dispatcher(status=dispatch_status)
    revision = {"value": 3}
    if attempt_started_at is _UNSET:
        attempt_started_at = NOW - timedelta(hours=2)

    async def update_mandate_record(
        ticket_id,
        key,
        *,
        set_fields=None,
        remove_fields=(),
        expected_status=None,
        expected_thread_id=None,
        expected_revision=None,
    ):
        records.append(
            {
                "ticket_id": ticket_id,
                "key": key,
                "set_fields": dict(set_fields or {}),
                "remove_fields": list(remove_fields),
                "expected_status": expected_status,
                "expected_thread_id": expected_thread_id,
                "expected_revision": expected_revision,
            }
        )
        if (
            expected_revision is not None
            and expected_revision != revision["value"]
        ):
            return None
        revision["value"] += 1
        return {
            "ticket_id": ticket_id,
            "revision": revision["value"],
            "mandate": {key: dict(set_fields or {})},
        }

    async def add_input(**kwargs):
        inputs.append(kwargs)
        # The real write moves the Ticket's revision.
        revision["value"] += 1
        if add_input_result is None:
            return {"input_id": "input-1", "ticket_revision": revision["value"]}
        reported = dict(add_input_result)
        # A caller that names an input and no revision is reporting the
        # revision this insert just moved to, unless it sets one itself.
        if reported.get("input_id") and "ticket_revision" not in reported:
            reported["ticket_revision"] = revision["value"]
        return reported

    async def mark_input_delivered(_input_id):
        if marked is not None:
            marked.append(_input_id)
        # The real write moves the Ticket's revision too.
        revision["value"] += 1
        return revision["value"]

    async def dispatch_turn(**kwargs):
        if delivery_error is not None:
            raise delivery_error
        return await gate.dispatch_turn(**kwargs)

    async def list_candidates(*, idle_seconds, limit, offset):
        if pages is not None:
            return list(pages.get(offset, []))
        return list(rows)

    list_mock = AsyncMock(side_effect=list_candidates)

    with (
        patch.object(
            worker_idle.ticket_db,
            "list_worker_idle_candidates",
            list_mock,
        ),
        patch.object(
            worker_idle.ticket_db, "latest_report", AsyncMock(return_value=report)
        ),
        patch.object(
            worker_idle.ticket_db,
            "update_mandate_record",
            AsyncMock(side_effect=update_mandate_record),
        ),
        patch.object(
            worker_idle.ticket_db, "add_input", AsyncMock(side_effect=add_input)
        ),
        patch.object(
            worker_idle.ticket_db,
            "mark_input_delivered",
            AsyncMock(side_effect=mark_input_delivered),
        ),
        patch.object(worker, "ProjectDispatcher", return_value=SimpleNamespace(
            dispatch_turn=dispatch_turn
        )),
        patch.object(worker_idle, "_worker_has_commit", lambda _ticket: has_commit),
        patch.object(
            worker_idle,
            "_worker_last_commit_at",
            lambda _ticket: committed_at,
        ),
        patch(
            "src.server.database.runs.lifecycle.get_run_started_at",
            AsyncMock(return_value=attempt_started_at),
        ),
        patch.object(
            worker_idle,
            "_CANDIDATE_PAGE",
            page_size if page_size is not None else worker_idle._CANDIDATE_PAGE,
        ),
    ):
        result = await sweep_worker_idle_wakes(minutes=minutes, now=NOW)
    return result, records, inputs, gate.dispatched


@pytest.mark.asyncio
async def test_the_sweep_does_nothing_when_the_switch_is_off() -> None:
    with patch.object(
        worker_idle.ticket_db,
        "list_worker_idle_candidates",
        AsyncMock(return_value=[_ticket()]),
    ) as candidates:
        result = await sweep_worker_idle_wakes(minutes=None, now=NOW)

    assert result.woken == 0
    candidates.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_sweep_wakes_a_silent_worker() -> None:
    result, records, _inputs, _dispatched = await _sweep([_ticket()])

    assert result.woken == 1
    assert result.tickets == frozenset({"ticket-1"})
    # The wake is recorded on the mandate, so it needs no migration.
    assert records[0]["key"] == IDLE_WAKE_MANDATE_KEY
    assert records[0]["set_fields"]["wakes"] == 1
    assert records[0]["set_fields"]["last_wake_at"]
    assert records[0]["expected_status"] == "running"


@pytest.mark.asyncio
async def test_the_wake_is_recorded_after_the_notice_moved_the_revision() -> None:
    """The notice row moves the Ticket's revision before the marker is written.

    A marker fenced on the revision the sweep read would refuse the very
    wake it belongs to, and the notice would pile up undelivered.
    """
    result, records, inputs, dispatched = await _sweep([_ticket()])

    assert result.woken == 1
    assert len(inputs) == 1
    assert len(dispatched) == 1
    # The marker is written after the notice, and it does not depend on the
    # revision: it is fenced on the Ticket's status and Worker thread.
    assert records[0]["expected_status"] == "running"
    assert records[0]["expected_thread_id"] == "thread-1"
    # add_input moved 3 -> 4. The marker fences on 4, not on the 3 the
    # sweep read. The delivered stamp then moves 5 -> 6, and the settlement
    # fences on 6. A fence on the pre-image refuses both writes.
    assert records[0]["expected_revision"] == 4
    assert records[0]["set_fields"]["pending_input_id"] == "input-1"
    assert records[1]["remove_fields"] == ["pending_input_id"]
    assert records[1]["expected_revision"] == 6


@pytest.mark.asyncio
async def test_the_wake_reaches_the_worker() -> None:
    """The notice is carried into the turn the Worker is running.

    This is the delivery itself, through the real path: the dispatcher the
    delivery talks to is the only fake here.
    """
    dispatcher = _Dispatcher()

    result, _updates, _inputs, dispatched = await _sweep(
        [_ticket()], dispatcher=dispatcher
    )

    assert result.woken == 1
    assert len(dispatched) == 1
    sent = dispatched[0]
    assert sent["thread_id"] == "thread-1"
    assert sent["platform"] == worker.WORKER_PLATFORM
    # A wake joins the turn that is running; it never starts a turn.
    assert sent["steer_only"] is True
    assert "no commit" in sent["prompt"]
    assert sent["request_key"].startswith("autonomy:idle-wake:ticket-1:")


@pytest.mark.asyncio
async def test_a_daemon_worker_pulls_the_wake_instead() -> None:
    """A daemon-bound Ticket is pull-only: the notice is stored, never steered.

    A daemon has no inbound poll during a claim, so a steer into the thread
    a legacy row still carries would be stamped delivered while the daemon
    never reads it. The notice stays stored for the next claim, and that
    store is not a wake.
    """
    dispatcher = _Dispatcher(status="not_running")
    ticket = _ticket(worker_daemon_id="daemon-1")

    result, _updates, inputs, dispatched = await _sweep(
        [ticket], dispatcher=dispatcher, has_commit=None
    )

    assert result.woken == 0
    assert dispatched == []
    # The daemon reads the Ticket's instructions with its next claim.
    assert inputs[0]["kind"] == "instruction"
    assert "no commit" in inputs[0]["text"]


@pytest.mark.asyncio
async def test_a_daemon_with_a_live_server_thread_is_still_not_steered() -> None:
    """Even when the thread would accept a steer, a daemon only pulls."""
    dispatcher = _Dispatcher()
    ticket = _ticket(worker_daemon_id="daemon-1")

    result, _updates, inputs, dispatched = await _sweep(
        [ticket], dispatcher=dispatcher, has_commit=None
    )

    assert result.woken == 0
    assert dispatched == []
    assert inputs[0]["kind"] == "instruction"


@pytest.mark.asyncio
async def test_an_isolated_cloud_worker_keeps_the_notice() -> None:
    """The container owns that turn; a steer that finds nothing does not start one.

    ``not_running`` means no server turn accepted the notice. The notice is
    kept for the container's next claim, and it is not a wake: counting it
    as one would tell the watchdog that a Worker was reached when it was not.
    """
    dispatcher = _Dispatcher(status="not_running")
    ticket = _ticket(
        mandate={"cloud_worker": {"isolated": True, "heartbeat_at": "2026-01-01"}}
    )

    result, _updates, inputs, dispatched = await _sweep(
        [ticket], dispatcher=dispatcher
    )

    assert result.woken == 0
    assert result.tickets == frozenset()
    assert len(dispatched) == 1
    assert dispatched[0]["steer_only"] is True
    assert inputs[0]["kind"] == "instruction"


@pytest.mark.asyncio
async def test_an_isolated_cloud_worker_with_a_live_turn_is_woken() -> None:
    """A live server thread is steered. The wake does not start a second run."""
    dispatcher = _Dispatcher()
    ticket = _ticket(
        mandate={"cloud_worker": {"isolated": True, "heartbeat_at": "2026-01-01"}}
    )

    result, _updates, _inputs, dispatched = await _sweep(
        [ticket], dispatcher=dispatcher
    )

    assert result.woken == 1
    assert len(dispatched) == 1
    assert dispatched[0]["steer_only"] is True


@pytest.mark.asyncio
async def test_a_worker_that_committed_is_left_alone() -> None:
    """A Worker that committed a moment ago is not a silent one."""
    dispatcher = _Dispatcher()

    result, records, _inputs, dispatched = await _sweep(
        [_ticket()],
        has_commit=True,
        dispatcher=dispatcher,
        committed_at=NOW - timedelta(minutes=1),
    )

    assert result.woken == 0
    assert records == []
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_worker_that_committed_and_went_still_is_woken() -> None:
    """The window runs from the last commit, not from the start of the run.

    A Worker that committed an hour ago and has been quiet since then is
    silent again: the commit says when it last moved.
    """
    dispatcher = _Dispatcher()

    result, _updates, _inputs, dispatched = await _sweep(
        [_ticket()],
        has_commit=True,
        dispatcher=dispatcher,
        committed_at=NOW - timedelta(hours=1),
    )

    assert result.woken == 1
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_commit_from_an_earlier_attempt_does_not_wake_a_fresh_one() -> None:
    """The window runs from the later of the attempt start and the last commit."""
    dispatcher = _Dispatcher()

    result, _updates, _inputs, dispatched = await _sweep(
        [_ticket()],
        has_commit=True,
        dispatcher=dispatcher,
        committed_at=NOW - timedelta(hours=3),
        attempt_started_at=NOW - timedelta(minutes=5),
    )

    assert result.woken == 0
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_worker_that_reported_is_left_alone() -> None:
    """A completion report of this attempt settles it: no wake."""
    report = {"created_at": NOW, "state": "ready", "report_id": "report-1"}
    dispatcher = _Dispatcher()

    result, _updates, _inputs, dispatched = await _sweep(
        [_ticket()],
        report=report,
        dispatcher=dispatcher,
        attempt_started_at=NOW - timedelta(minutes=5),
    )

    assert result.woken == 0
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_progress_report_does_not_silence_the_wake() -> None:
    """A heartbeat is not a completion.

    ``autonomy_worker_progress`` writes a ``progress`` row into the same
    reports table. It leaves the Ticket running, so it is the report of the
    run that is silent now, and it must not silence the wake.
    """
    report = {"created_at": NOW, "state": "progress", "report_id": "report-1"}
    dispatcher = _Dispatcher()

    result, _updates, _inputs, dispatched = await _sweep(
        [_ticket()], report=report, dispatcher=dispatcher
    )

    assert result.woken == 1
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_completion_report_of_an_earlier_attempt_does_not_silence_it() -> None:
    """The attempt is identified by the run that owns the Ticket now.

    The report predates this run, so it belongs to an earlier attempt: it
    says nothing about the run that is silent now.
    """
    report = {"created_at": NOW - timedelta(hours=2), "state": "ready"}
    dispatcher = _Dispatcher()

    result, _updates, _inputs, dispatched = await _sweep(
        [_ticket()],
        report=report,
        dispatcher=dispatcher,
        attempt_started_at=NOW - timedelta(hours=1),
    )

    assert result.woken == 1
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_the_attempt_does_not_depend_on_the_ticket_row() -> None:
    """A later write to the Ticket must not unfile its report.

    ``updated_at`` moves on every write to the Ticket, so a report that
    belongs to this attempt stops looking like it does after a heartbeat.
    The run ledger row never moves.
    """
    report = {"created_at": NOW, "state": "ready", "report_id": "report-1"}
    ticket = _ticket(updated_at=NOW + timedelta(hours=1))
    dispatcher = _Dispatcher()

    result, _updates, _inputs, dispatched = await _sweep(
        [ticket],
        report=report,
        dispatcher=dispatcher,
        attempt_started_at=NOW - timedelta(minutes=5),
    )

    assert result.woken == 0
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_worker_woken_recently_is_left_alone() -> None:
    ticket = _ticket()
    ticket["mandate"] = idle_state_fragment(
        last_wake_at=NOW - timedelta(minutes=5), wakes=1
    )
    dispatcher = _Dispatcher()

    result, _updates, _inputs, dispatched = await _sweep(
        [ticket], dispatcher=dispatcher
    )

    assert result.woken == 0
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_failed_delivery_is_owed_and_retried() -> None:
    """A notice that could not be delivered is not a wake that happened.

    The marker carries the notice, so the next sweep delivers it instead of
    letting the window pass in silence.
    """
    dispatcher = _Dispatcher()
    ticket = _ticket()

    result, records, inputs, dispatched = await _sweep(
        [ticket],
        dispatcher=dispatcher,
        delivery_error=RuntimeError("the door is closed"),
    )

    assert result.woken == 0
    assert dispatched == []
    # The obligation is on the Ticket, with the notice it owes.
    assert records[0]["set_fields"]["pending_input_id"] == "input-1"
    assert len(inputs) == 1

    # The next sweep finds the obligation and delivers the same notice.
    ticket["mandate"] = {IDLE_WAKE_MANDATE_KEY: records[0]["set_fields"]}
    dispatcher = _Dispatcher()
    result, records, inputs, dispatched = await _sweep(
        [ticket], dispatcher=dispatcher, has_commit=False
    )

    assert result.woken == 1
    assert len(dispatched) == 1
    assert "no commit" in dispatched[0]["prompt"]
    # The notice is stored once: the retry carries the one that exists.
    assert len(inputs) == 0


@pytest.mark.asyncio
async def test_a_notice_that_cannot_be_stored_leaves_the_window() -> None:
    """Without a notice there is nothing to owe, so the window is not spent."""
    dispatcher = _Dispatcher()

    result, records, inputs, dispatched = await _sweep(
        [_ticket()],
        dispatcher=dispatcher,
        add_input_result={},
    )

    assert result.woken == 0
    # The notice was attempted, but nothing was stored and nothing was sent.
    assert len(inputs) == 1
    assert records == []
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_wake_that_cannot_be_recorded_leaves_the_worker_alone() -> None:
    dispatcher = _Dispatcher()
    with (
        patch.object(
            worker_idle.ticket_db,
            "list_worker_idle_candidates",
            AsyncMock(return_value=[_ticket()]),
        ),
        patch.object(
            worker_idle.ticket_db, "latest_report", AsyncMock(return_value=None)
        ),
        patch.object(
            worker_idle.ticket_db,
            "update_mandate_record",
            AsyncMock(return_value=None),
        ),
        patch.object(
            worker_idle.ticket_db,
            "add_input",
            AsyncMock(return_value={"input_id": "input-1", "ticket_revision": 4}),
        ),
        patch.object(
            worker, "ProjectDispatcher", return_value=dispatcher
        ),
        patch.object(worker_idle, "_worker_has_commit", lambda _ticket: False),
        patch(
            "src.server.database.runs.lifecycle.get_run_started_at",
            AsyncMock(return_value=NOW - timedelta(hours=2)),
        ),
    ):
        result = await sweep_worker_idle_wakes(minutes=30, now=NOW)

    assert result.woken == 0
    assert dispatcher.dispatched == []


@pytest.mark.asyncio
async def test_a_notice_that_is_only_stored_is_not_a_wake() -> None:
    """A notice the Worker did not receive is not a wake.

    The dispatch found no running turn, so the notice waits for the Worker's
    next turn. Counting it as a wake would tell the watchdog that the Worker
    was reached when it was not.
    """
    dispatcher = _Dispatcher(status="not_running")

    result, _records, inputs, dispatched = await _sweep(
        [_ticket()], dispatcher=dispatcher
    )

    assert result.woken == 0
    assert result.tickets == frozenset()
    # The notice was stored and the dispatch was tried.
    assert len(inputs) == 1
    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_a_plan_run_is_left_alone() -> None:
    dispatcher = _Dispatcher()

    result, _updates, _inputs, dispatched = await _sweep(
        [_ticket(mandate={"speculative": True})], dispatcher=dispatcher
    )

    assert result.woken == 0
    assert dispatched == []


@pytest.mark.asyncio
async def test_the_watchdog_sweep_wakes_the_idle_workers() -> None:
    with (
        patch.object(
            watchdog, "sweep_worker_idle_wakes", AsyncMock(return_value=IdleWakeSweep())
        ) as sweep,
        patch.object(
            watchdog.ticket_db, "list_watchdog_candidates", AsyncMock(return_value=[])
        ),
    ):
        flagged = await watchdog.watchdog_sweep()

    sweep.assert_awaited_once()
    assert flagged == 0


@pytest.mark.asyncio
async def test_the_watchdog_does_not_flag_a_worker_it_just_woke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake does not replace stall handling.

    The Worker is steered, and the same pass still flags a Ticket that has
    been running past the stall threshold. Skipping the flag because the
    wake landed would hide that Ticket.
    """
    monkeypatch.setenv(IDLE_WAKE_ENV, "180")
    ticket = _ticket(stall_seconds=181 * 60)
    dispatcher = _Dispatcher()
    updates: list[dict] = []

    async def update_ticket(_ticket_id, **kwargs):
        updates.append(kwargs)
        return {"ticket_id": "ticket-1", "revision": 4, "mandate": kwargs.get("mandate")}

    with (
        patch.object(
            worker_idle.ticket_db,
            "list_worker_idle_candidates",
            AsyncMock(return_value=[ticket]),
        ),
        patch.object(
            watchdog.ticket_db,
            "list_watchdog_candidates",
            AsyncMock(return_value=[ticket]),
        ),
        patch.object(
            worker_idle.ticket_db, "latest_report", AsyncMock(return_value=None)
        ),
        patch.object(
            watchdog.ticket_db, "update_ticket", AsyncMock(side_effect=update_ticket)
        ),
        patch.object(
            worker_idle.ticket_db,
            "update_mandate_record",
            AsyncMock(return_value={"ticket_id": "ticket-1", "revision": 5}),
        ),
        patch.object(
            worker_idle.ticket_db,
            "add_input",
            AsyncMock(return_value={"input_id": "input-1", "ticket_revision": 4}),
        ),
        patch.object(
            worker_idle.ticket_db,
            "mark_input_delivered",
            AsyncMock(return_value=6),
        ),
        patch.object(worker, "ProjectDispatcher", return_value=dispatcher),
        patch.object(watchdog, "_run_is_live", AsyncMock(return_value=True)),
        patch.object(watchdog, "enqueue_effect", AsyncMock(return_value=True)),
        patch.object(worker_idle, "_worker_has_commit", lambda _ticket: False),
        patch(
            "src.server.database.runs.lifecycle.get_run_started_at",
            AsyncMock(return_value=NOW - timedelta(hours=2)),
        ),
    ):
        flagged = await watchdog.watchdog_sweep()

    # The Worker was woken...
    assert len(dispatcher.dispatched) == 1
    assert dispatcher.dispatched[0]["steer_only"] is True
    # ...and the stall flag still happens. The wake is not a reason to skip it.
    assert flagged == 1
    attention = [update for update in updates if "attention" in update]
    assert len(attention) == 1
    assert "without completing" in str(attention[0]["attention"]["question"])


@pytest.mark.asyncio
async def test_the_watchdog_fails_a_woken_worker_whose_run_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wake does not keep a finished run alive.

    The run ended without a report, so the Ticket is failed whether or not it
    was just woken. The wake is the Worker's chance to answer, not a reason to
    leave a dead run sitting in ``running`` for a person to tidy up.
    """
    monkeypatch.setenv(IDLE_WAKE_ENV, "180")
    ticket = _ticket(stall_seconds=181 * 60)
    dispatcher = _Dispatcher()
    updates: list[dict] = []

    async def update_ticket(_ticket_id, **kwargs):
        updates.append(kwargs)
        return {"ticket_id": "ticket-1", "revision": 4}

    with (
        patch.object(
            worker_idle.ticket_db,
            "list_worker_idle_candidates",
            AsyncMock(return_value=[ticket]),
        ),
        patch.object(
            watchdog.ticket_db,
            "list_watchdog_candidates",
            AsyncMock(return_value=[ticket]),
        ),
        patch.object(
            worker_idle.ticket_db, "latest_report", AsyncMock(return_value=None)
        ),
        patch.object(
            worker_idle.ticket_db, "update_ticket", AsyncMock(side_effect=update_ticket)
        ),
        patch.object(
            worker_idle.ticket_db,
            "update_mandate_record",
            AsyncMock(return_value={"ticket_id": "ticket-1", "revision": 4}),
        ),
        patch.object(
            worker_idle.ticket_db,
            "add_input",
            AsyncMock(return_value={"input_id": "input-1", "ticket_revision": 4}),
        ),
        patch.object(
            worker_idle.ticket_db,
            "mark_input_delivered",
            AsyncMock(return_value=6),
        ),
        patch.object(worker, "ProjectDispatcher", return_value=dispatcher),
        patch.object(watchdog, "_run_is_live", AsyncMock(return_value=False)),
        patch.object(watchdog, "enqueue_effect", AsyncMock(return_value=True)),
        patch.object(worker_idle, "_worker_has_commit", lambda _ticket: False),
        patch(
            "src.server.database.runs.lifecycle.get_run_started_at",
            AsyncMock(return_value=NOW - timedelta(hours=2)),
        ),
    ):
        flagged = await watchdog.watchdog_sweep()

    # The Worker was woken...
    assert len(dispatcher.dispatched) == 1
    # ...and the dead run took the Ticket with it anyway.
    assert flagged == 1
    failed = [update for update in updates if update.get("status") == "failed"]
    assert len(failed) == 1
    assert "ended without a report" in str(failed[0].get("error"))


@pytest.mark.asyncio
async def test_the_watchdog_still_flags_a_stale_worker_it_did_not_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wake is a courtesy, not a shield.

    A Ticket that was not woken -- because it committed, or because the switch
    is off -- is still stale, and the stall handling is not the wake's to
    change.
    """
    monkeypatch.setenv(IDLE_WAKE_ENV, "180")
    ticket = _ticket(stall_seconds=181 * 60)
    updates: list[dict] = []

    async def update_ticket(_ticket_id, **kwargs):
        updates.append(kwargs)
        return {"ticket_id": "ticket-1", "revision": 4}

    with (
        patch.object(
            worker_idle.ticket_db,
            "list_worker_idle_candidates",
            AsyncMock(return_value=[]),
        ),
        patch.object(
            watchdog.ticket_db,
            "list_watchdog_candidates",
            AsyncMock(return_value=[ticket]),
        ),
        patch.object(
            worker_idle.ticket_db, "update_ticket", AsyncMock(side_effect=update_ticket)
        ),
        patch.object(
            watchdog.ticket_db, "update_ticket", AsyncMock(side_effect=update_ticket)
        ),
        patch.object(watchdog, "_run_is_live", AsyncMock(return_value=True)),
        patch.object(watchdog, "enqueue_effect", AsyncMock(return_value=True)),
    ):
        flagged = await watchdog.watchdog_sweep()

    assert flagged == 1
    attention = [update for update in updates if "attention" in update]
    assert len(attention) == 1
    assert "without completing" in str(attention[0]["attention"]["question"])


class _QueryCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: tuple = ()

    async def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.params = params

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _QueryConnection:
    def __init__(self) -> None:
        self.cur = _QueryCursor()

    def cursor(self, row_factory=None):
        return self.cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_the_candidates_are_running_tickets_silent_since_their_start() -> None:
    conn = _QueryConnection()
    with patch(
        "src.server.database.autonomy.tickets.get_db_connection",
        return_value=conn,
    ):
        await worker_idle.ticket_db.list_worker_idle_candidates(
            idle_seconds=1800, limit=5
        )

    assert "status = 'running'" in conn.cur.sql
    assert "attention IS NULL" in conn.cur.sql
    assert "conversation_responses" in conn.cur.sql
    assert "attempt.created_at" in conn.cur.sql
    # The silence clock is the attempt's own start, not the Ticket's
    # started_at (which survives a resume); the column list still selects it.
    assert not re.search(
        r"COALESCE\(\s*(autonomy_tickets\.)?started_at", conn.cur.sql
    )
    assert "ORDER BY autonomy_tickets.ticket_id ASC" in conn.cur.sql
    assert "OFFSET" in conn.cur.sql
    assert "orchestration_only" in conn.cur.sql
    assert conn.cur.params == (1800, 5, 0)


@pytest.mark.asyncio
async def test_a_stale_revision_from_before_the_notice_refuses_the_wake() -> None:
    """A fence on the revision the sweep read does not record the wake.

    add_input has already moved the revision. Reporting that older revision
    makes the compare-and-swap miss, so the sweep does not dispatch and does
    not treat the missed write as a wake.
    """
    dispatcher = _Dispatcher()

    result, records, inputs, dispatched = await _sweep(
        [_ticket()],
        dispatcher=dispatcher,
        add_input_result={"input_id": "input-1", "ticket_revision": 3},
    )

    assert result.woken == 0
    assert len(inputs) == 1
    assert records[0]["expected_revision"] == 3
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_refused_wake_record_does_not_leave_its_notice_behind() -> None:
    """The notice a refused record stored is stamped, not left for a later turn.

    Without the stamp every sweep the fence refuses stores another copy, and
    the Worker's next turn reads all of them.
    """
    marked: list[str] = []

    result, _records, inputs, dispatched = await _sweep(
        [_ticket()],
        dispatcher=_Dispatcher(),
        add_input_result={"input_id": "input-1", "ticket_revision": 3},
        marked=marked,
    )

    assert result.woken == 0
    assert len(inputs) == 1
    assert dispatched == []
    assert marked == ["input-1"]


@pytest.mark.asyncio
async def test_a_notice_without_its_revision_is_not_left_behind() -> None:
    marked: list[str] = []

    result, _records, _inputs, dispatched = await _sweep(
        [_ticket()],
        dispatcher=_Dispatcher(),
        add_input_result={"input_id": "input-1", "ticket_revision": None},
        marked=marked,
    )

    assert result.woken == 0
    assert dispatched == []
    assert marked == ["input-1"]


@pytest.mark.asyncio
async def test_a_resumed_attempt_is_not_woken_from_the_ticket_clock() -> None:
    """Resume keeps the Ticket's started_at. The window is this attempt.

    stall_seconds is the old Ticket clock (an hour). The run that owns the
    Ticket now began a minute ago, so the attempt is not idle.
    """
    dispatcher = _Dispatcher()

    result, records, inputs, dispatched = await _sweep(
        [_ticket(stall_seconds=3600)],
        dispatcher=dispatcher,
        attempt_started_at=NOW - timedelta(minutes=1),
    )

    assert result.woken == 0
    assert records == []
    assert inputs == []
    assert dispatched == []


@pytest.mark.asyncio
async def test_an_unknown_attempt_start_is_not_the_ticket_clock() -> None:
    """When the run's start cannot be read, the Ticket clock is not a substitute."""
    dispatcher = _Dispatcher()

    result, records, inputs, dispatched = await _sweep(
        [_ticket(stall_seconds=99999)],
        dispatcher=dispatcher,
        attempt_started_at=None,
    )

    assert result.woken == 0
    assert records == []
    assert inputs == []
    assert dispatched == []


@pytest.mark.asyncio
async def test_a_pending_notice_is_cancelled_when_the_worker_has_moved() -> None:
    """A failed delivery does not skip the next activity check.

    The notice is still owed, and the Worker has since committed. The retry
    must not dispatch. The input is stamped so a later claim does not deliver
    a stale "you have been silent" notice, and the stamp's revision is the
    fence for clearing the obligation.
    """
    ticket = _ticket()
    ticket["mandate"] = idle_state_fragment(
        last_wake_at=NOW - timedelta(minutes=5),
        wakes=1,
        pending_input_id="input-owed",
    )
    dispatcher = _Dispatcher()

    result, records, inputs, dispatched = await _sweep(
        [ticket],
        dispatcher=dispatcher,
        has_commit=True,
        committed_at=NOW - timedelta(minutes=1),
    )

    assert result.woken == 0
    assert dispatched == []
    assert inputs == []
    assert records[0]["remove_fields"] == ["pending_input_id"]
    # The ticket row was revision 3. The delivered stamp moves it to 4.
    assert records[0]["expected_revision"] == 4


@pytest.mark.asyncio
async def test_a_later_page_reaches_an_idle_worker_the_first_page_hid() -> None:
    """The first page of active Workers must not starve a later idle one."""
    recent = idle_state_fragment(
        last_wake_at=NOW - timedelta(minutes=1), wakes=1
    )
    active = [
        _ticket(ticket_id="active-1", mandate=recent),
        _ticket(ticket_id="active-2", mandate=recent),
    ]
    idle = _ticket(ticket_id="idle-9", revision=3)
    dispatcher = _Dispatcher()

    result, _records, _inputs, dispatched = await _sweep(
        [],
        dispatcher=dispatcher,
        pages={0: active, 2: [idle]},
        page_size=2,
        has_commit=False,
    )

    assert result.woken == 1
    assert result.tickets == frozenset({"idle-9"})
    assert len(dispatched) == 1
    assert dispatched[0]["thread_id"] == "thread-1"


@pytest.mark.asyncio
async def test_an_empty_dispatch_status_is_not_a_wake() -> None:
    """An empty receipt did not confirm that a running turn has the notice."""
    dispatcher = _Dispatcher(status="")

    result, _records, inputs, dispatched = await _sweep(
        [_ticket()], dispatcher=dispatcher
    )

    assert result.woken == 0
    assert len(dispatched) == 1
    assert len(inputs) == 1


@pytest.mark.asyncio
async def test_a_stored_notice_does_not_skip_the_stall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """not_running is not a wake, and it does not skip the stall or the liveness check."""
    monkeypatch.setenv(IDLE_WAKE_ENV, "180")
    ticket = _ticket(stall_seconds=181 * 60)
    dispatcher = _Dispatcher(status="not_running")
    updates: list[dict] = []

    async def update_ticket(_ticket_id, **kwargs):
        updates.append(kwargs)
        return {"ticket_id": "ticket-1", "revision": 4}

    with (
        patch.object(
            worker_idle.ticket_db,
            "list_worker_idle_candidates",
            AsyncMock(return_value=[ticket]),
        ),
        patch.object(
            watchdog.ticket_db,
            "list_watchdog_candidates",
            AsyncMock(return_value=[ticket]),
        ),
        patch.object(
            worker_idle.ticket_db, "latest_report", AsyncMock(return_value=None)
        ),
        patch.object(
            watchdog.ticket_db, "update_ticket", AsyncMock(side_effect=update_ticket)
        ),
        patch.object(
            worker_idle.ticket_db,
            "update_mandate_record",
            AsyncMock(return_value={"ticket_id": "ticket-1", "revision": 5}),
        ),
        patch.object(
            worker_idle.ticket_db,
            "add_input",
            AsyncMock(return_value={"input_id": "input-1", "ticket_revision": 4}),
        ),
        patch.object(
            worker_idle.ticket_db,
            "mark_input_delivered",
            AsyncMock(return_value=6),
        ),
        patch.object(worker, "ProjectDispatcher", return_value=dispatcher),
        patch.object(watchdog, "_run_is_live", AsyncMock(return_value=True)),
        patch.object(watchdog, "enqueue_effect", AsyncMock(return_value=True)),
        patch.object(worker_idle, "_worker_has_commit", lambda _ticket: False),
        patch(
            "src.server.database.runs.lifecycle.get_run_started_at",
            AsyncMock(return_value=NOW - timedelta(hours=2)),
        ),
    ):
        flagged = await watchdog.watchdog_sweep()

    assert len(dispatcher.dispatched) == 1
    assert flagged == 1
    attention = [update for update in updates if "attention" in update]
    assert len(attention) == 1
    assert "without completing" in str(attention[0]["attention"]["question"])
    failed = [update for update in updates if update.get("status") == "failed"]
    assert failed == []
