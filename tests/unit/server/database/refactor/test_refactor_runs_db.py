"""Run-contract SQL shape: idempotent upsert keyed by the response id."""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.server.database.refactor import runs

pytestmark = pytest.mark.asyncio


async def test_upsert_is_idempotent_on_response_id(mock_db_connection, mock_cursor):
    mock_cursor.fetchone.return_value = {"conversation_response_id": "r"}
    row = await runs.upsert_contract(
        conversation_response_id=str(uuid4()),
        permission_mode="auto",
        model_id="kimi-k3",
        context_budget={"max_tokens": 8000},
        injected_memory=["memory/user/prefs/language"],
        verifier_verdict={"verdict": "satisfied"},
    )
    assert row == {"conversation_response_id": "r"}
    sql, params = mock_cursor.execute.await_args.args
    assert "INSERT INTO agent_run_contracts" in sql
    assert "ON CONFLICT (conversation_response_id) DO UPDATE" in sql
    # A retry with no model must not erase the one the first attempt stored.
    assert "COALESCE(EXCLUDED.model_id, agent_run_contracts.model_id)" in sql
    # Finalize passes neither audit field; empty defaults must preserve the
    # injection audit written at turn start.
    assert "WHEN EXCLUDED.context_budget = '{}'::jsonb" in sql
    assert "WHEN EXCLUDED.injected_memory = '[]'::jsonb" in sql
    assert params[1] == "auto"
    assert params[2] == "kimi-k3"


async def test_omitted_audit_fields_bind_null(mock_db_connection, mock_cursor):
    mock_cursor.fetchone.return_value = {"conversation_response_id": "r"}
    await runs.upsert_contract(conversation_response_id=str(uuid4()))
    _sql, params = mock_cursor.execute.await_args.args
    assert params[3] is None  # context_budget: omitted -> preserve
    assert params[4] is None  # injected_memory: omitted -> preserve


async def test_invalid_response_id_is_rejected(mock_db_connection):
    with pytest.raises(ValueError, match="conversation_response_id"):
        await runs.upsert_contract(conversation_response_id="not-a-uuid")


async def test_record_subagent_contract_updates_only_the_ledger_row(
    mock_db_connection, mock_cursor
):
    task_run_id = str(uuid4())
    mock_cursor.fetchone.return_value = {
        "task_run_id": task_run_id,
        "contract_version": 1,
        "status": "completed",
        "result": {"summary": "x"},
    }
    row = await runs.record_subagent_contract(
        task_run_id=task_run_id,
        result={"contract_version": 1, "task_id": "t", "status": "completed", "summary": "x"},
        tool_allowlist=["Read"],
        context_budget={"tokens": 10},
    )
    assert row["task_run_id"] == task_run_id
    sql, params = mock_cursor.execute.await_args.args
    assert "UPDATE subagent_runs" in sql
    assert "SET contract_version = %s" in sql
    assert "result = COALESCE(%s::jsonb, result)" in sql
    assert "tool_allowlist = COALESCE(%s::text[], tool_allowlist)" in sql
    assert params[0] == 1
    assert params[3] == ["Read"]
    assert params[-1] == task_run_id


async def test_record_subagent_contract_missing_run_returns_none(
    mock_db_connection, mock_cursor
):
    mock_cursor.fetchone.return_value = None
    assert (
        await runs.record_subagent_contract(
            task_run_id=str(uuid4()), result={"summary": "x"}
        )
        is None
    )
