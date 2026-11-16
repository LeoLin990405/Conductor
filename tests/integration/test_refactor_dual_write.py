from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _seed(pool) -> tuple[str, str, str]:
    user_id = str(uuid.uuid4())
    workspace_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    response_id = str(uuid.uuid4())
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO users (user_id, email) VALUES (%s, %s)",
            (user_id, "refactor-dual-write@example.com"),
        )
        await conn.execute(
            "INSERT INTO workspaces (workspace_id, user_id, name, name_key, status)"
            " VALUES (%s, %s, 'Research', 'research', 'running')",
            (workspace_id, user_id),
        )
        await conn.execute(
            "INSERT INTO conversation_threads"
            " (conversation_thread_id, workspace_id, current_status, thread_index)"
            " VALUES (%s, %s, 'completed', 1)",
            (thread_id, workspace_id),
        )
        # The lifecycle guard requires a run to be born in_progress; the
        # projection does not require a terminal status on the row.
        await conn.execute(
            "INSERT INTO conversation_responses"
            " (conversation_response_id, conversation_thread_id, turn_index, status,"
            "  created_at) VALUES (%s, %s, 1, 'in_progress', NOW())",
            (response_id, thread_id),
        )
    return user_id, thread_id, response_id


class TestTurnFinalizeProjection:
    async def test_writes_contract_and_event_rows(self, refactor_db):
        from src.server.services.refactor.dual_write import record_turn_finalized

        user_id, thread_id, response_id = await _seed(refactor_db)
        result = await record_turn_finalized(
            user_id=user_id,
            thread_id=thread_id,
            response_id=response_id,
            status="completed",
            execution_time=1.234,
            metadata={"workspace_id": "w", "model": "kimi-k3", "is_byok": True},
        )
        assert result is not None
        assert result["event"] and result["contract"]

        async with refactor_db.connection() as conn:
            event = await (
                await conn.execute(
                    "SELECT type, payload FROM agent_events WHERE response_id = %s",
                    (response_id,),
                )
            ).fetchone()
            contract = await (
                await conn.execute(
                    "SELECT permission_mode, model_id FROM agent_run_contracts"
                    " WHERE conversation_response_id = %s",
                    (response_id,),
                )
            ).fetchone()
        assert event["type"] == "run.contract"
        assert event["payload"]["status"] == "completed"
        assert event["payload"]["model"] == "kimi-k3"
        assert contract["permission_mode"] == "auto"
        assert contract["model_id"] == "kimi-k3"

    async def test_second_finalize_updates_one_contract_row(self, refactor_db):
        from src.server.services.refactor.dual_write import record_turn_finalized

        user_id, thread_id, response_id = await _seed(refactor_db)
        await record_turn_finalized(
            user_id=user_id,
            thread_id=thread_id,
            response_id=response_id,
            status="completed",
        )
        await record_turn_finalized(
            user_id=user_id,
            thread_id=thread_id,
            response_id=response_id,
            status="completed",
            metadata={"model": "kimi-k3"},
        )
        async with refactor_db.connection() as conn:
            count = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM agent_run_contracts"
                    " WHERE conversation_response_id = %s",
                    (response_id,),
                )
            ).fetchone()
            model = await (
                await conn.execute(
                    "SELECT model_id FROM agent_run_contracts"
                    " WHERE conversation_response_id = %s",
                    (response_id,),
                )
            ).fetchone()
        assert count["n"] == 1
        # The retry's model fills the field the first write did not have.
        assert model["model_id"] == "kimi-k3"

    async def test_flag_off_writes_nothing(self, refactor_db, monkeypatch):
        from src.server.services.refactor.dual_write import record_turn_finalized

        monkeypatch.delenv("REFACTOR_EVENTS_ENABLED", raising=False)
        user_id, thread_id, response_id = await _seed(refactor_db)
        result = await record_turn_finalized(
            user_id=user_id,
            thread_id=thread_id,
            response_id=response_id,
            status="completed",
        )
        assert result is None
        async with refactor_db.connection() as conn:
            events = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM agent_events WHERE response_id = %s",
                    (response_id,),
                )
            ).fetchone()
            contracts = await (
                await conn.execute(
                    "SELECT count(*) AS n FROM agent_run_contracts"
                    " WHERE conversation_response_id = %s",
                    (response_id,),
                )
            ).fetchone()
        assert events["n"] == 0
        assert contracts["n"] == 0


class TestMemoryInjectionAudit:
    async def test_injection_audit_survives_finalize(self, refactor_db):
        from src.server.services.refactor.dual_write import (
            record_memory_injection,
            record_turn_finalized,
        )
        from src.server.services.refactor.memory_context import (
            ContextEntry,
            MemoryContext,
        )

        user_id, thread_id, response_id = await _seed(refactor_db)
        context = MemoryContext(
            query="q",
            budget_tokens=800,
            entries=(
                ContextEntry(
                    tier="user",
                    entry_key="prefs/language",
                    index_text="prefers Chinese",
                    content_hash="h1",
                    tokens=12,
                    rank=0,
                ),
            ),
            dropped=2,
            per_tier={"user": 12},
        )
        audit = await record_memory_injection(
            user_id=user_id,
            thread_id=thread_id,
            response_id=response_id,
            context=context,
        )
        assert audit and audit["contract"]

        # Finalize writes its own contract fields with empty audit defaults; the
        # turn-start injection audit must survive that update.
        await record_turn_finalized(
            user_id=user_id,
            thread_id=thread_id,
            response_id=response_id,
            status="completed",
            metadata={"model": "kimi-k3"},
        )
        async with refactor_db.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT injected_memory, context_budget, model_id"
                    " FROM agent_run_contracts WHERE conversation_response_id = %s",
                    (response_id,),
                )
            ).fetchone()
        assert row["injected_memory"] == [
            {
                "tier": "user",
                "key": "prefs/language",
                "source": "conductor_pg",
                "concept_id": None,
                "concepts": [],
                "provenance": {},
            }
        ]
        assert row["context_budget"]["memory_tokens"] == 12
        assert row["context_budget"]["memory_budget"] == 800
        assert row["model_id"] == "kimi-k3"
