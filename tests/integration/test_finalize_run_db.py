"""finalize_run against real Postgres.

The finalize writes the run's replay facts with its status and rewrites the
main lane's provenance from the lane's own record, so only a real database
can show that a subagent lane's records survive and that a lost CAS writes
nothing.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

# Session loop: the pool lives there, and the lock test needs a second connection.
pytestmark = [pytest.mark.integration, pytest.mark.asyncio(loop_scope="session")]

# What a finalize hands back, won or lost.
ROW_KEYS = {
    "conversation_response_id",
    "conversation_thread_id",
    "turn_index",
    "run_seq",
    "status",
    "interrupt_reason",
    "metadata",
    "execution_time",
}

CHUNK = {"event": "message_chunk", "data": {"agent": "ptc", "content": "m"}}


def _provenance(url: str) -> dict:
    return {
        "event": "provenance",
        "source_type": "web",
        "identifier": url,
        "agent": "ptc",
    }


def _finalize(run_id: str, thread_id: str, status: str, **fields):
    from src.server.database.runs.lifecycle import RunOutcome, finalize_run

    return finalize_run(
        run_id=run_id, thread_id=thread_id, outcome=RunOutcome(status=status, **fields)
    )


async def _persisted_times(db_conn, run_id: str) -> tuple[float, float]:
    cur = await db_conn.execute(
        """
        SELECT execution_time,
               EXTRACT(EPOCH FROM (clock_timestamp() - created_at)) AS db_elapsed
        FROM conversation_responses
        WHERE conversation_response_id = %s
        """,
        (run_id,),
    )
    row = await cur.fetchone()
    execution_time = row["execution_time"]
    return (
        None if execution_time is None else float(execution_time),
        float(row["db_elapsed"]),
    )


def _assert_db_timed(execution_time: float, db_elapsed: float) -> None:
    assert execution_time >= 0, f"negative execution_time {execution_time}"
    assert abs(execution_time - db_elapsed) <= 0.5, (
        f"execution_time {execution_time} vs database elapsed {db_elapsed}"
    )


def _record(*events: dict, reasoning_ms=None):
    """A producer's record of ``events``' provenance."""
    from src.server.database.runs.lifecycle import ProducerRecord

    return ProducerRecord(
        provenance=[e for e in events if e["event"] == "provenance"],
        reasoning_ms=reasoning_ms,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def coord_thread(seed_workspace, patched_get_db_connection):
    """A thread with no run yet, for tests that START their own run."""
    from src.server.database.conversation import create_thread

    thread_id = str(uuid.uuid4())
    await create_thread(
        conversation_thread_id=thread_id,
        workspace_id=str(seed_workspace["workspace_id"]),
        current_status="completed",
        msg_type="ptc",
    )
    return thread_id


@pytest_asyncio.fixture(loop_scope="session")
async def open_run(seed_workspace, patched_get_db_connection):
    from src.server.database.conversation import create_thread
    from src.server.database.runs.lifecycle import start_run

    thread_id = str(uuid.uuid4())
    await create_thread(
        conversation_thread_id=thread_id,
        workspace_id=str(seed_workspace["workspace_id"]),
        current_status="in_progress",
        msg_type="ptc",
    )
    run_id = str(uuid.uuid4())
    await start_run(
        run_id=run_id,
        thread_id=thread_id,
        request_key=str(uuid.uuid4()),
        metadata={"msg_type": "ptc", "user_id": seed_workspace["user_id"]},
    )
    return thread_id, run_id


async def _provenance_ids(conn, run_id: str) -> list[str]:
    cur = await conn.execute(
        "SELECT identifier FROM provenance_records "
        "WHERE conversation_response_id = %s ORDER BY identifier",
        (run_id,),
    )
    return [r["identifier"] for r in await cur.fetchall()]


async def test_finalize_settles_the_row(open_run, db_conn):
    thread_id, run_id = open_run
    events = [CHUNK, _provenance("https://final.example")]

    result = await _finalize(run_id, thread_id, "completed", record=_record(*events))

    assert result.applied
    assert set(result.run) == ROW_KEYS
    assert result.run["status"] == "completed"
    assert await _provenance_ids(db_conn, run_id) == ["https://final.example"]
    # The outbox jobs are built from the returned row alone.
    cur = await db_conn.execute(
        "SELECT payload FROM hook_outbox WHERE run_id = %s AND hook_type = 'user_feed'",
        (run_id,),
    )
    assert (await cur.fetchone())["payload"]["run_seq"] == result.run["run_seq"]


async def test_lost_cas_writes_nothing_and_returns_the_survivor(open_run, db_conn):
    thread_id, run_id = open_run
    won = _record(_provenance("https://won.example"))
    first = await _finalize(run_id, thread_id, "completed", record=won)
    assert first.applied

    lost = _record(_provenance("https://lost.example"))
    second = await _finalize(run_id, thread_id, "error", errors=["late"], record=lost)

    assert not second.applied
    assert set(second.run) == ROW_KEYS
    assert second.run["status"] == "completed"
    assert await _provenance_ids(db_conn, run_id) == ["https://won.example"]


async def _replay_facts(conn, run_id: str):
    cur = await conn.execute(
        "SELECT replay_facts FROM conversation_responses "
        "WHERE conversation_response_id = %s",
        (run_id,),
    )
    return (await cur.fetchone())["replay_facts"]


async def test_replay_facts_land_with_the_status_and_survive_a_lost_cas(
    open_run, db_conn
):
    thread_id, run_id = open_run
    durations = {"msg-1": [1840, 920]}
    first = await _finalize(
        run_id,
        thread_id,
        "completed",
        record=_record(reasoning_ms=durations),
    )
    assert first.applied
    assert await _replay_facts(db_conn, run_id) == {"reasoning_ms": durations}

    second = await _finalize(
        run_id,
        thread_id,
        "error",
        errors=["late"],
        record=_record(reasoning_ms={"msg-2": [5]}),
    )
    assert not second.applied
    assert await _replay_facts(db_conn, run_id) == {"reasoning_ms": durations}


async def test_a_stopped_runs_late_captured_images_land_in_its_facts(
    open_run, db_conn
):
    """Every ending keeps the images its saved text still named, not only a
    completed one."""
    thread_id, run_id = open_run
    images = {"charts/a.png": "https://cdn.example/a.png"}
    assert (await _finalize(run_id, thread_id, "cancelled", images=images)).applied
    assert await _replay_facts(db_conn, run_id) == {"images": images}


@pytest.mark.parametrize("recovered", [False, True], ids=["fail-open", "recovery"])
async def test_finalize_without_facts_leaves_the_column_null(
    open_run, db_conn, recovered
):
    """Recovery and fail-open settles hold no producer that timed the
    reasoning, so their rows keep no durations."""
    thread_id, run_id = open_run
    record = _record(CHUNK) if recovered else None
    assert (await _finalize(run_id, thread_id, "cancelled", record=record)).applied
    assert await _replay_facts(db_conn, run_id) is None


async def test_main_lane_provenance_leaves_subagent_lanes(open_run, db_conn):
    """A subagent's archive writes its lane before the parent settles; the
    finalize rewrites only the main lane."""
    from src.server.database.conversation.responses import replace_lane_provenance

    thread_id, run_id = open_run
    task_record = {
        "event": "provenance",
        "data": {
            "agent": "task:abc",
            "source_type": "web",
            "identifier": "https://task.example",
        },
    }
    assert await replace_lane_provenance(run_id, [task_record], {"task:abc": "abc"})
    assert await _provenance_ids(db_conn, run_id) == ["https://task.example"]

    main_record = {
        "event": "provenance",
        "data": {
            "agent": "main",
            "source_type": "web",
            "identifier": "https://main.example",
        },
    }
    result = await _finalize(
        run_id,
        thread_id,
        "completed",
        record=_record(main_record),
    )
    assert result.applied
    assert await _provenance_ids(db_conn, run_id) == [
        "https://main.example",
        "https://task.example",
    ]


# ---------------------------------------------------------------------------
# execution_time: the coordinator times it, the terminal CAS stores it
# ---------------------------------------------------------------------------


def _coordinator():
    from src.server.services.runs.coordinator import RunCoordinator

    return RunCoordinator()


def _clock_skew(request, monkeypatch):
    """Wall clock shifted by ``request.param`` seconds; monotonic advanced 10 s.

    The artifact chain computes a wall-clock duration (time.time() - start_time);
    the coordinator must ignore it and persist its own monotonic measurement.
    """
    from datetime import datetime as real_datetime  # noqa: F401 - keep parity
    from types import SimpleNamespace as NS

    from src.server.services.runs import coordinator
    from src.server.utils import persistence_utils

    skew = request.param
    real_time = persistence_utils.time.time
    monkeypatch.setattr(
        persistence_utils, "time", NS(time=lambda: real_time() + skew)
    )
    monkeypatch.setattr(coordinator, "time", NS(monotonic=lambda: 10.0))
    # Old fixture parity: the modules still import datetime; leave it real.
    return skew


@pytest.fixture(
    params=(0.0, 2.0, -2.0),
    ids=("app-clock", "app-clock+2s", "app-clock-2s"),
)
def python_clock_skew(request, monkeypatch):
    return _clock_skew(request, monkeypatch)


async def _same_process_handle(thread_id: str):
    """A handle for a run STARTed by this process (monotonic start available)."""
    from src.server.database.runs.lifecycle import start_run
    from src.server.services.runs.coordinator import RunHandle

    run_id = str(uuid.uuid4())
    row = await start_run(
        run_id=run_id,
        thread_id=thread_id,
        request_key=str(uuid.uuid4()),
        metadata={"msg_type": "ptc"},
    )
    return RunHandle(
        run_id=run_id,
        thread_id=thread_id,
        turn_index=row["turn_index"],
        attempt_no=row["attempt_no"],
        started_at=row["created_at"],
        started_monotonic=0.0,  # the fixture's monotonic() reports 10.0
    )


def _isolate_tail(coordinator, monkeypatch) -> None:
    """Cut the post-CAS transport tail: this file tests the timing, not exports."""
    from unittest.mock import AsyncMock

    monkeypatch.setattr(coordinator, "_newest_checkpoint", AsyncMock(return_value=None))
    monkeypatch.setattr(coordinator, "post_finalize_tail", lambda *a, **k: None)


async def _artifact_outcome(thread_id, run_id, *, status="completed", skew=0.0):
    """Build a RunOutcome through the real artifact assembly, without a handler."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    from src.server.services.runs import finalization as fin

    with patch.object(fin, "_delivered_steering_inputs", AsyncMock(return_value=[])), \
         patch.object(fin, "_late_images", AsyncMock(return_value={})), \
         patch.object(fin, "_producer_record", AsyncMock(return_value=None)), \
         patch.object(
             fin, "get_token_usage_from_callback", lambda *a, **k: (None, [])
         ), \
         patch.object(fin, "get_tool_usage_from_handler", lambda *a, **k: None):
        return await fin.assemble_finalize_artifacts(
            (thread_id, run_id),
            metadata={"start_time": 1.0},
            status=status,
            phase="final",
            interrupt_reason=None,
            pause_message=None,
            error=None,
            handler=SimpleNamespace(),
            cancelled_by_user=False,
            workspace_id=None,
            user_id=None,
        )


async def test_the_coordinator_times_the_run_and_ignores_the_artifact_value(
    coord_thread, db_conn, monkeypatch, python_clock_skew
):
    """The artifact chain's wall-clock duration (shifted with the app clock) is
    ignored: the coordinator persists its 10 s monotonic measurement, and the
    value it returns is the stored one."""
    thread_id = coord_thread
    handle = await _same_process_handle(thread_id)
    coordinator = _coordinator()
    _isolate_tail(coordinator, monkeypatch)
    outcome = await _artifact_outcome(thread_id, handle.run_id)
    upstream = outcome.execution_time
    assert upstream is not None  # the artifact value exists, and is ignored
    outcome.execution_time = -99.0  # the previous cycle's scenario: must be ignored

    result = await coordinator.finalize_run(handle, outcome)
    assert result.applied
    assert abs(result.run["execution_time"] - 10.0) < 1e-9

    execution_time, _ = await _persisted_times(db_conn, handle.run_id)
    assert abs(execution_time - 10.0) < 1e-9  # not -99, not the wall value
    row = await _row_metadata(db_conn, handle.run_id)
    assert "execution_time_unknown" not in row["metadata"]


async def test_fail_open_is_timed_by_the_coordinator(
    coord_thread, db_conn, monkeypatch, python_clock_skew
):
    thread_id = coord_thread
    handle = await _same_process_handle(thread_id)
    coordinator = _coordinator()
    _isolate_tail(coordinator, monkeypatch)

    result = await coordinator.fail_open_run(handle, "client disconnected during setup")
    assert result is not None and result.applied
    assert abs(result.run["execution_time"] - 10.0) < 1e-9
    execution_time, _ = await _persisted_times(db_conn, handle.run_id)
    assert abs(execution_time - 10.0) < 1e-9


async def test_a_cancelled_run_is_timed_by_the_coordinator(
    coord_thread, db_conn, monkeypatch, python_clock_skew
):
    from src.server.database.runs.lifecycle import RunOutcome, request_run_cancel

    thread_id = coord_thread
    handle = await _same_process_handle(thread_id)
    coordinator = _coordinator()
    _isolate_tail(coordinator, monkeypatch)
    await request_run_cancel(handle.run_id)

    result = await coordinator.finalize_run(
        handle, RunOutcome(status="cancelled", execution_time=-99.0)
    )
    assert result.applied
    assert result.run["status"] == "cancelled"
    execution_time, _ = await _persisted_times(db_conn, handle.run_id)
    assert abs(execution_time - 10.0) < 1e-9


async def test_a_restored_handle_uses_the_database_clock(
    coord_thread, db_conn, monkeypatch, python_clock_skew
):
    from src.server.database.runs.lifecycle import RunOutcome, start_run
    from src.server.services.runs.admission import _committed_start

    thread_id = coord_thread
    run_id = str(uuid.uuid4())
    await start_run(
        run_id=run_id,
        thread_id=thread_id,
        request_key=str(uuid.uuid4()),
        metadata={"msg_type": "ptc"},
    )
    restored = await _committed_start(
        run_id,
        thread_id=thread_id,
        workspace_id=None,
        user_id=None,
        is_byok=False,
    )
    assert restored is not None and restored.started_monotonic is None
    coordinator = _coordinator()
    _isolate_tail(coordinator, monkeypatch)

    result = await coordinator.finalize_run(
        restored, RunOutcome(status="cancelled", execution_time=-99.0)
    )
    assert result.applied
    execution_time, db_elapsed = await _persisted_times(db_conn, run_id)
    assert execution_time is not None and execution_time >= 0
    assert abs(execution_time - db_elapsed) <= 0.5


async def test_a_clock_regression_is_reported_not_written(
    coord_thread, db_conn, monkeypatch, python_clock_skew
):
    """A recovered run whose database difference is negative: execution_time is
    NULL and metadata records the reason; the artifact value is not used."""
    from src.server.database.runs.lifecycle import RunOutcome, start_run
    from src.server.services.runs.admission import _committed_start

    thread_id = coord_thread
    run_id = str(uuid.uuid4())
    await start_run(
        run_id=run_id,
        thread_id=thread_id,
        request_key=str(uuid.uuid4()),
        metadata={"msg_type": "ptc"},
    )
    # Push the row's created_at into the future: the database difference at CAS
    # becomes negative, as after a wall-clock step backwards.
    await db_conn.execute(
        "UPDATE conversation_responses SET created_at = "
        "statement_timestamp() + INTERVAL '5 seconds' "
        "WHERE conversation_response_id = %s",
        (run_id,),
    )
    restored = await _committed_start(
        run_id,
        thread_id=thread_id,
        workspace_id=None,
        user_id=None,
        is_byok=False,
    )
    assert restored is not None and restored.started_monotonic is None
    coordinator = _coordinator()
    _isolate_tail(coordinator, monkeypatch)

    result = await coordinator.finalize_run(
        restored, RunOutcome(status="error", execution_time=-99.0)
    )
    assert result.applied
    execution_time, _ = await _persisted_times(db_conn, run_id)
    assert execution_time is None  # not -99, not a clamp
    row = await _row_metadata(db_conn, run_id)
    assert row["metadata"]["execution_time_unknown"] == "clock_regressed"


async def _row_metadata(db_conn, run_id: str) -> dict:
    cur = await db_conn.execute(
        "SELECT metadata FROM conversation_responses WHERE conversation_response_id = %s",
        (run_id,),
    )
    return await cur.fetchone()
