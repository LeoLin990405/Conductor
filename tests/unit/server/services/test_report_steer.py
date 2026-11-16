"""SteeredReportInbox: which owed reports a live turn takes in, the hold that
makes the taking exclusive, and the confirm that closes them once the message
carrying them is checkpointed."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ptc_agent.agent.middleware.steering import SteeredReport
from src.server.services.runs.reports import steer
from src.server.services.runs.reports.notify_turn import NotOwed

_OUTBOX = "src.server.database.runs.outbox"
_BY_KEY = "src.server.database.runs.lifecycle.find_run_by_request_key"


@pytest.fixture(autouse=True)
def _no_admitted_turn():
    with patch(_BY_KEY, new=AsyncMock(return_value=None)) as by_key:
        yield by_key


@pytest.fixture(autouse=True)
def _no_live_hand_off():
    with patch(
        "src.server.database.runs.outbox.has_live_hand_off",
        new=AsyncMock(return_value=False),
    ) as live:
        yield live


def _job(n: int, hook_type: str = "task_report_back") -> dict:
    return {"hook_outbox_id": f"job-{n}", "hook_type": hook_type, "payload": {}}


def _report(n: int) -> SteeredReport:
    return SteeredReport(
        job_id=f"job-{n}",
        content=f"report {n}",
        shown={"report_source": {"kind": "task", "task_id": str(n)}},
    )


async def _take(jobs, decisions, held, *, closed=(), hold=None, close=None):
    """Run a take with per-job decisions (keyed by call order), the ids the
    hold reports as won, and the ids the unowed close reports as won."""
    hold = hold or AsyncMock(return_value=held)
    close = close or AsyncMock(return_value=list(closed))
    with (
        patch(f"{_OUTBOX}.list_steerable_report_jobs", new=AsyncMock(return_value=jobs)),
        patch(f"{_OUTBOX}.hold_steered_report_jobs", new=hold),
        patch(f"{_OUTBOX}.close_unowed_report_jobs", new=close),
        patch.object(steer, "_task_offer", new=AsyncMock(side_effect=decisions)),
    ):
        reports = await steer.SteeredReportInbox().take(
            "t-1", "run-A", kinds=("report",)
        )
    return reports, hold, close


@pytest.mark.asyncio
async def test_holds_reports_it_hands_over_and_closes_unowed_jobs():
    jobs = [_job(1), _job(2), _job(3), _job(4)]
    decisions = [_report(1), NotOwed("delivered"), None, _report(4)]

    reports, hold, close = await _take(
        jobs, decisions, held=["job-1"], closed=["job-2"]
    )

    # job-3 (left to its executor) is neither held nor closed; job-4 was lost
    # to its executor between the read and the hold.
    hold.assert_awaited_once_with(["job-1", "job-4"], run_id="run-A")
    close.assert_awaited_once_with(["job-2"])
    assert reports == [_report(1)]


@pytest.mark.asyncio
async def test_held_reports_keep_oldest_first_order():
    reports, *_ = await _take(
        [_job(1), _job(2)], [_report(1), _report(2)], held=["job-2", "job-1"]
    )

    assert [r.job_id for r in reports] == ["job-1", "job-2"]


@pytest.mark.asyncio
async def test_jobs_are_decided_concurrently():
    """Each decision reads the ledger and Redis; a turn waits on all of them
    before its model call, so they must not queue behind each other."""
    second_started = asyncio.Event()

    async def first(job):
        await asyncio.wait_for(second_started.wait(), timeout=1)
        return _report(1)

    async def second(job):
        second_started.set()
        return _report(2)

    offers = iter([first, second])

    async def _offer(job):
        return await next(offers)(job)

    with (
        patch(
            f"{_OUTBOX}.list_steerable_report_jobs",
            new=AsyncMock(return_value=[_job(1), _job(2)]),
        ),
        patch(
            f"{_OUTBOX}.hold_steered_report_jobs",
            new=AsyncMock(return_value=["job-1", "job-2"]),
        ),
        patch(f"{_OUTBOX}.close_unowed_report_jobs", new=AsyncMock(return_value=[])),
        patch.object(steer, "_task_offer", new=_offer),
    ):
        reports = await steer.SteeredReportInbox().take("t-1", "run-A", kinds=("report",))

    assert [r.job_id for r in reports] == ["job-1", "job-2"]


@pytest.mark.asyncio
async def test_a_job_whose_read_fails_is_left_to_its_executor():
    reports, hold, close = await _take(
        [_job(1), _job(2)], [RuntimeError("boom"), _report(2)], held=["job-2"]
    )

    hold.assert_awaited_once_with(["job-2"], run_id="run-A")
    close.assert_awaited_once_with([])
    assert reports == [_report(2)]


@pytest.mark.asyncio
async def test_listing_failure_takes_nothing():
    with patch(
        f"{_OUTBOX}.list_steerable_report_jobs",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        assert await steer.SteeredReportInbox().take("t-1", "run-A", kinds=("report",)) == []


@pytest.mark.asyncio
async def test_hold_failure_takes_nothing_but_still_closes_unowed_jobs():
    hold = AsyncMock(side_effect=RuntimeError("db down"))

    reports, _, close = await _take(
        [_job(1), _job(2)], [_report(1), NotOwed("delivered")], held=[], hold=hold,
        closed=["job-2"],
    )

    assert reports == []
    close.assert_awaited_once_with(["job-2"])


@pytest.mark.asyncio
async def test_close_failure_keeps_the_reports_already_held():
    """The hold has committed; dropping its reports here would park them
    until the turn ends, unseen by the turn that holds them."""
    close = AsyncMock(side_effect=RuntimeError("db down"))

    reports, *_ = await _take(
        [_job(1), _job(2)], [_report(1), NotOwed("delivered")], held=["job-1"],
        close=close,
    )

    assert reports == [_report(1)]








@pytest.mark.asyncio
async def test_confirm_closes_the_runs_held_reports_and_never_raises():
    confirm = AsyncMock(return_value=["job-1"])
    with patch(f"{_OUTBOX}.confirm_jobs", new=confirm):
        await steer.SteeredReportInbox().confirm("run-A", ["job-1"])
    confirm.assert_awaited_once_with(["job-1"], owner=("run", "run-A"))

    with patch(
        f"{_OUTBOX}.confirm_jobs",
        new=AsyncMock(side_effect=RuntimeError("db down")),
    ):
        await steer.SteeredReportInbox().confirm("run-A", ["job-1"])


# ---------------------------------------------------------------------------
# _task_offer: the executor's verdict, behind the steer's own pre-checks
# ---------------------------------------------------------------------------

_SR = "src.server.database.runs.subagent_runs.get_task_run"
_TL = "src.server.database.runs.lifecycle.get_run"
_STOPPED_AT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _task_job(**payload) -> dict:
    base = {
        "task_run_id": "tr-1",
        "task_id": "abc123",
        "display_id": "Task-abc123",
        "subagent_type": "research",
        "description": "look things up",
    }
    base.update(payload)
    return {"hook_outbox_id": "job-1", "hook_type": "task_report_back", "payload": base}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run_row",
    [None, {"result_delivered_at": "2026-01-01T00:00:00Z"}],
    ids=["run-row-gone", "result-delivered"],
)
async def test_task_offer_closes_a_report_owed_nothing(run_row):
    with patch(_SR, new=AsyncMock(return_value=run_row)):
        assert isinstance(await steer._task_offer(_task_job()), NotOwed)


@pytest.mark.asyncio
async def test_task_offer_closes_a_report_its_stopped_turn_covered():
    run_row = {
        "result_delivered_at": None,
        "parent_run_id": "run-P",
        "finalized_at": _STOPPED_AT - timedelta(seconds=30),
    }
    parent = {"status": "cancelled", "cancel_requested_at": _STOPPED_AT}
    with (
        patch(_SR, new=AsyncMock(return_value=run_row)),
        patch(_TL, new=AsyncMock(return_value=parent)) as get_run,
    ):
        assert isinstance(await steer._task_offer(_task_job()), NotOwed)
    get_run.assert_awaited_once_with("run-P")


@pytest.mark.asyncio
async def test_task_offer_leaves_an_unlabelled_job_to_the_executor():
    assert await steer._task_offer(_task_job(task_run_id=None)) is None
    assert await steer._task_offer(_task_job(task_id=None)) is None


@pytest.mark.asyncio
async def test_task_offer_leaves_a_report_whose_turn_was_admitted(_no_admitted_turn):
    _no_admitted_turn.return_value = {"conversation_response_id": "run-R"}
    task_run = AsyncMock()
    with patch(_SR, new=task_run):
        assert await steer._task_offer(_task_job()) is None
    task_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_offer_steers_an_owed_report():
    run_row = {"result_delivered_at": None, "parent_run_id": None}
    with patch(_SR, new=AsyncMock(return_value=run_row)):
        offer = await steer._task_offer(_task_job())

    assert offer.job_id == "job-1"
    assert offer.content == (
        "<system>\n"
        "Background subagent Task-abc123 (research) finished while you were "
        "working.\n"
        "Task description: look things up\n"
        "</system>"
    )
    assert offer.shown["report_source"]["kind"] == "task"


# ---------------------------------------------------------------------------
# _analyst_offer
# ---------------------------------------------------------------------------

_CACHE = "src.utils.cache.redis_cache.get_cache_client"


def _analyst_cache(origin: dict, *, member: bool = True) -> MagicMock:
    cache = MagicMock()
    cache.get_strict = AsyncMock(
        side_effect=lambda key: None if key.startswith("handoff_rb_run:") else origin
    )
    cache.client.sismember = AsyncMock(return_value=member)
    return cache


def _analyst_job(**over) -> dict:
    return {
        "hook_outbox_id": "job-1",
        "hook_type": "report_back",
        "payload": {"ptc_thread_id": "ptc-1", "final_status": "completed"},
        **over,
    }


def _origin(**over) -> dict:
    base = {
        "origin": "home",
        "report_back": True,
        "home_thread_id": "t-1",
        "user_id": "u-1",
    }
    base.update(over)
    return base
















