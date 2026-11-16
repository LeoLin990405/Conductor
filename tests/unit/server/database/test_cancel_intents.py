"""Cancel polling reads only pending intents and has a bounded UUID parameter list."""

from uuid import uuid4

import pytest

from src.server.database.runs import lifecycle, subagent_runs


@pytest.mark.asyncio
@pytest.mark.parametrize("reader,run_column,thread_column", [
    (lifecycle.get_run_cancel_intents, "conversation_response_id", "conversation_thread_id"),
    (subagent_runs.get_task_run_cancel_intents, "task_run_id", "thread_id"),
])
async def test_poll_query_is_scoped_to_live_stopped_ids(
    reader, run_column, thread_column, mock_db_connection, mock_cursor,
):
    run_id, thread_id = str(uuid4()), str(uuid4())
    mock_cursor.fetchall.return_value = [{
        run_column: run_id, thread_column: thread_id, "task_id": "task-1",
    }]
    result = await reader([run_id, "invalid", f"urn:uuid:{run_id}"])
    assert run_id in result
    sql, parameters = mock_cursor.execute.await_args.args
    assert "status = 'in_progress'" in sql
    assert "cancel_requested_at IS NOT NULL" in sql
    assert parameters == ([run_id],)


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", [
    lifecycle.get_run_cancel_intents, subagent_runs.get_task_run_cancel_intents,
])
async def test_empty_or_invalid_batch_never_queries(reader, mock_db_connection, mock_cursor):
    assert await reader([]) == {}
    assert await reader(["malformed"]) == {}
    mock_cursor.execute.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", [
    lifecycle.get_run_cancel_intents, subagent_runs.get_task_run_cancel_intents,
])
async def test_over_limit_batch_fails_before_database_acquisition(reader, mock_db_connection, mock_cursor):
    with pytest.raises(ValueError, match="batch exceeds"):
        await reader([str(uuid4()) for _ in range(257)])
    mock_cursor.execute.assert_not_awaited()
