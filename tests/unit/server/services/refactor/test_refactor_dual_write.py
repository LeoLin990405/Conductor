"""First call-site wiring: turn-finalize projection behind the events flag.

The contract under test is the safety one: flag off is a strict no-op (so the
runtime is unchanged by default), a missing owner identity skips quietly, and a
store failure in either write is logged and dropped — the terminal CAS already
committed, so the turn must never fail on a projection error.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.server.database.refactor import runs as db_runs
from src.server.services.refactor import dual_write
from src.server.services.refactor import events as svc_events

pytestmark = pytest.mark.asyncio

RUN_ID = "9b2c1d3e-5f60-4a7b-8c9d-0e1f2a3b4c5d"
THREAD_ID = "4f6a0e4c-3f9c-4d43-8a0b-9e0f1a2b3c4d"
USER_ID = "00000000-0000-0000-0000-00000000aa01"


@pytest.fixture
def events_flag(monkeypatch):
    monkeypatch.setenv("REFACTOR_EVENTS_ENABLED", "1")


class TestFlagOff:
    async def test_no_writes_when_off(self, monkeypatch):
        record_event = AsyncMock()
        upsert = AsyncMock()
        monkeypatch.setattr(svc_events, "record_event", record_event)
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        result = await dual_write.record_turn_finalized(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            status="completed",
            execution_time=1.5,
            metadata={"workspace_id": "w"},
        )
        assert result is None
        record_event.assert_not_awaited()
        upsert.assert_not_awaited()


class TestIdentity:
    async def test_missing_owner_identity_skips_quietly(self, events_flag, monkeypatch):
        record_event = AsyncMock()
        upsert = AsyncMock()
        monkeypatch.setattr(svc_events, "record_event", record_event)
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        for user_id, thread_id, response_id in (
            (None, THREAD_ID, RUN_ID),
            (USER_ID, None, RUN_ID),
            (USER_ID, THREAD_ID, None),
        ):
            assert (
                await dual_write.record_turn_finalized(
                    user_id=user_id,
                    thread_id=thread_id,
                    response_id=response_id,
                    status="completed",
                )
                is None
            )
        record_event.assert_not_awaited()
        upsert.assert_not_awaited()


class TestProjection:
    async def test_writes_event_and_contract(self, events_flag, monkeypatch):
        event = {"event_id": 1}
        contract = {"conversation_response_id": RUN_ID}
        record_event = AsyncMock(return_value=event)
        upsert = AsyncMock(return_value=contract)
        monkeypatch.setattr(svc_events, "record_event", record_event)
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)

        result = await dual_write.record_turn_finalized(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            status="completed",
            execution_time=2.3456,
            metadata={"workspace_id": "w", "is_byok": True, "model": "kimi-k3"},
        )

        assert result == {"event": event, "contract": contract}
        event_kwargs = record_event.await_args.kwargs
        assert event_kwargs["event_type"] == "run.contract"
        assert event_kwargs["payload"] == {
            "status": "completed",
            "execution_time_s": 2.346,
            "workspace_id": "w",
            "is_byok": True,
            "model": "kimi-k3",
            "has_errors": False,
        }
        assert event_kwargs["thread_id"] == THREAD_ID
        assert event_kwargs["response_id"] == RUN_ID
        contract_kwargs = upsert.await_args.kwargs
        assert contract_kwargs["conversation_response_id"] == RUN_ID
        assert contract_kwargs["permission_mode"] == "auto"
        assert contract_kwargs["model_id"] == "kimi-k3"

    async def test_event_failure_does_not_block_contract(self, events_flag, monkeypatch):
        record_event = AsyncMock(side_effect=RuntimeError("event store down"))
        upsert = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(svc_events, "record_event", record_event)
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        result = await dual_write.record_turn_finalized(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            status="error",
        )
        assert result == {"event": None, "contract": {"ok": True}}
        upsert.assert_awaited_once()

    async def test_contract_failure_does_not_raise(self, events_flag, monkeypatch):
        monkeypatch.setattr(
            svc_events, "record_event", AsyncMock(return_value={"event_id": 2})
        )
        monkeypatch.setattr(
            db_runs, "upsert_contract", AsyncMock(side_effect=RuntimeError("db down"))
        )
        result = await dual_write.record_turn_finalized(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            status="cancelled",
        )
        assert result == {"event": {"event_id": 2}, "contract": None}

    async def test_no_model_omits_model_id(self, events_flag, monkeypatch):
        upsert = AsyncMock()
        monkeypatch.setattr(svc_events, "record_event", AsyncMock(return_value={}))
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        await dual_write.record_turn_finalized(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            status="interrupted",
        )
        assert upsert.await_args.kwargs["model_id"] is None


class TestPermissionDecision:
    async def test_flag_off_is_noop(self, monkeypatch):
        record_event = AsyncMock()
        monkeypatch.setattr(svc_events, "record_event", record_event)
        assert (
            await dual_write.record_permission_decision(
                user_id=USER_ID,
                thread_id=THREAD_ID,
                response_id=RUN_ID,
                hitl_response={"i1": {"decisions": [{"type": "approve"}]}},
            )
            is None
        )
        record_event.assert_not_awaited()

    async def test_missing_identity_is_noop(self, events_flag, monkeypatch):
        record_event = AsyncMock()
        monkeypatch.setattr(svc_events, "record_event", record_event)
        assert (
            await dual_write.record_permission_decision(
                user_id=None,
                thread_id=THREAD_ID,
                response_id=RUN_ID,
                hitl_response={"i1": {"decisions": [{"type": "approve"}]}},
            )
            is None
        )
        record_event.assert_not_awaited()

    async def test_summarizes_types_without_content(self, events_flag, monkeypatch):
        record_event = AsyncMock(return_value={"event_id": 3})
        monkeypatch.setattr(svc_events, "record_event", record_event)
        await dual_write.record_permission_decision(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            hitl_response={
                "i1": {
                    "decisions": [
                        {"type": "approve"},
                        {"type": "reject", "message": "do not do that"},
                    ],
                },
                "i2": {"decisions": [{"type": "reject"}]},
            },
        )
        kwargs = record_event.await_args.kwargs
        assert kwargs["event_type"] == "permission.decision"
        assert kwargs["payload"] == {
            "interrupts": 2,
            "approve": 1,
            "reject": 2,
            "feedback": True,
        }
        assert "do not do that" not in str(kwargs["payload"])

    async def test_pydantic_style_objects_are_supported(self, events_flag, monkeypatch):
        record_event = AsyncMock(return_value={"event_id": 4})
        monkeypatch.setattr(svc_events, "record_event", record_event)

        class Decision:
            def __init__(self, type_, message=None):
                self._data = {"type": type_, "message": message}

            def model_dump(self):
                return self._data

        class Response:
            def model_dump(self):
                return {"decisions": [Decision("approve").model_dump()]}

        await dual_write.record_permission_decision(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            hitl_response={"i1": Response()},
        )
        assert record_event.await_args.kwargs["payload"]["approve"] == 1

    async def test_store_failure_is_dropped(self, events_flag, monkeypatch):
        monkeypatch.setattr(
            svc_events, "record_event", AsyncMock(side_effect=RuntimeError("down"))
        )
        assert (
            await dual_write.record_permission_decision(
                user_id=USER_ID,
                thread_id=THREAD_ID,
                response_id=RUN_ID,
                hitl_response={"i1": {"decisions": [{"type": "approve"}]}},
            )
            is None
        )


class TestMemoryInjection:
    @pytest.fixture(autouse=True)
    def automatic_memory_on(self, monkeypatch):
        from src.server.services.refactor import memory_runtime_controls

        monkeypatch.setattr(
            memory_runtime_controls,
            "automatic_memory_enabled",
            AsyncMock(return_value=True),
        )

    @pytest.fixture
    def memory_flag(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        yield

    def _context(self):
        from src.server.services.refactor.memory_context import (
            ContextEntry,
            MemoryContext,
        )

        return MemoryContext(
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
                    concept_id="person:leo",
                ),
                ContextEntry(
                    tier="workspace",
                    entry_key="fact/symbols",
                    index_text="tracks atlas-repo",
                    content_hash="h2",
                    tokens=9,
                    rank=0,
                ),
            ),
            dropped=3,
            per_tier={"user": 12, "workspace": 9},
        )

    async def test_flag_off_is_noop(self, monkeypatch):
        upsert = AsyncMock()
        record_event = AsyncMock()
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        monkeypatch.setattr(svc_events, "record_event", record_event)
        assert (
            await dual_write.record_memory_injection(
                user_id=USER_ID,
                thread_id=THREAD_ID,
                response_id=RUN_ID,
                context=self._context(),
            )
            is None
        )
        upsert.assert_not_awaited()
        record_event.assert_not_awaited()

    async def test_user_memory_off_skips_injection_audit(self, memory_flag, monkeypatch):
        from src.server.services.refactor import memory_runtime_controls

        monkeypatch.setattr(
            memory_runtime_controls,
            "automatic_memory_enabled",
            AsyncMock(return_value=False),
        )
        upsert = AsyncMock()
        record_event = AsyncMock()
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        monkeypatch.setattr(svc_events, "record_event", record_event)

        assert (
            await dual_write.record_memory_injection(
                user_id=USER_ID,
                thread_id=THREAD_ID,
                response_id=RUN_ID,
                context=self._context(),
            )
            is None
        )
        upsert.assert_not_awaited()
        record_event.assert_not_awaited()

    async def test_missing_identity_is_noop(self, memory_flag, monkeypatch):
        upsert = AsyncMock()
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        assert (
            await dual_write.record_memory_injection(
                user_id=None,
                thread_id=THREAD_ID,
                response_id=RUN_ID,
                context=self._context(),
            )
            is None
        )
        upsert.assert_not_awaited()

    async def test_writes_contract_audit_and_event(self, memory_flag, monkeypatch):
        upsert = AsyncMock(return_value={"conversation_response_id": RUN_ID})
        record_event = AsyncMock(return_value={"event_id": 9})
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        monkeypatch.setattr(svc_events, "record_event", record_event)
        result = await dual_write.record_memory_injection(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            context=self._context(),
        )
        assert result == {
            "contract": {"conversation_response_id": RUN_ID},
            "event": {"event_id": 9},
        }
        contract_kwargs = upsert.await_args.kwargs
        assert contract_kwargs["injected_memory"] == [
            {
                "tier": "user",
                "key": "prefs/language",
                "source": "conductor_pg",
                "concept_id": "person:leo",
                "concepts": ["person:leo"],
                "provenance": {},
            },
            {
                "tier": "workspace",
                "key": "fact/symbols",
                "source": "conductor_pg",
                "concept_id": None,
                "concepts": [],
                "provenance": {},
            },
        ]
        assert contract_kwargs["context_budget"] == {
            "memory_tokens": 21,
            "memory_budget": 800,
        }
        event_kwargs = record_event.await_args.kwargs
        assert event_kwargs["event_type"] == "memory.injected"
        assert event_kwargs["payload"] == {
            "entries": 2,
            "tokens": 21,
            "budget": 800,
            "dropped": 3,
            "per_tier": {"user": 12, "workspace": 9},
            "sources": {"conductor_pg": 2},
        }
        # Identity/provenance only, never index lines or bodies.
        assert "prefers Chinese" not in str(event_kwargs["payload"])

    async def test_external_entry_keeps_source_and_provenance(
        self, memory_flag, monkeypatch
    ):
        from src.server.services.refactor.memory_context import (
            ContextEntry,
            MemoryContext,
        )

        upsert = AsyncMock(return_value={"conversation_response_id": RUN_ID})
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        monkeypatch.setattr(svc_events, "record_event", AsyncMock(return_value={}))
        context = MemoryContext(
            query="q",
            budget_tokens=100,
            entries=(
                ContextEntry(
                    tier="user",
                    entry_key="shared-key",
                    index_text="Conductor body",
                    content_hash="h1",
                    tokens=4,
                    rank=0,
                ),
                ContextEntry(
                    tier="user",
                    entry_key="shared-key",
                    index_text="External body",
                    content_hash="h2",
                    tokens=4,
                    rank=1,
                    source="memfw",
                    provenance={"document_hash": "abc", "layer": "current"},
                ),
            ),
        )
        await dual_write.record_memory_injection(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            context=context,
        )
        records = upsert.await_args.kwargs["injected_memory"]
        assert records[0]["source"] == "conductor_pg"
        assert records[1] == {
            "tier": "user",
            "key": "shared-key",
            "source": "memfw",
            "concept_id": None,
            "concepts": [],
            "provenance": {"document_hash": "abc", "layer": "current"},
        }

    async def test_store_failures_are_isolated(self, memory_flag, monkeypatch):
        monkeypatch.setattr(
            db_runs, "upsert_contract", AsyncMock(side_effect=RuntimeError("db"))
        )
        record_event = AsyncMock(return_value={"event_id": 10})
        monkeypatch.setattr(svc_events, "record_event", record_event)
        result = await dual_write.record_memory_injection(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            context=self._context(),
        )
        assert result == {"contract": None, "event": {"event_id": 10}}


class TestSubagentContract:
    @pytest.fixture
    def contract_flag(self, monkeypatch):
        monkeypatch.setenv("REFACTOR_SUBAGENT_CONTRACT_ENABLED", "1")
        yield

    def _patch_owner(self, monkeypatch, user_id=USER_ID):
        from src.server.database import conversation as conv_db

        monkeypatch.setattr(
            conv_db,
            "get_thread_auth_meta",
            AsyncMock(return_value={"user_id": user_id}),
        )

    async def test_flag_off_is_noop(self, monkeypatch):
        record = AsyncMock()
        monkeypatch.setattr(db_runs, "record_subagent_contract", record)
        assert (
            await dual_write.record_subagent_result(
                thread_id=THREAD_ID,
                task_run_id=RUN_ID,
                task_id="task-1",
                status="completed",
            )
            is None
        )
        record.assert_not_awaited()

    async def test_completed_run_projects_resolved_summary(
        self, contract_flag, monkeypatch
    ):
        self._patch_owner(monkeypatch)
        contract = AsyncMock(return_value={"task_run_id": RUN_ID})
        event = AsyncMock(return_value={"event_id": 11})
        monkeypatch.setattr(db_runs, "record_subagent_contract", contract)
        monkeypatch.setattr(svc_events, "record_event", event)
        monkeypatch.setattr(
            dual_write,
            "_resolve_completed_result",
            AsyncMock(
                return_value=dual_write.StructuredSubagentResult(
                    summary="final answer",
                    findings=("one", "two"),
                    artifact_ids=("work/report.md",),
                    open_questions=("still open?",),
                )
            ),
        )
        written = await dual_write.record_subagent_result(
            thread_id=THREAD_ID,
            task_run_id=RUN_ID,
            task_id="task-1",
            status="completed",
        )
        assert written == {"contract": {"task_run_id": RUN_ID}, "event": {"event_id": 11}}
        result = contract.await_args.kwargs["result"]
        assert result["status"] == "completed"
        assert result["task_id"] == "task-1"
        assert result["summary"] == "final answer"
        assert result["findings"] == ["one", "two"]
        assert result["artifact_ids"] == ["work/report.md"]
        assert result["open_questions"] == ["still open?"]
        assert result["contract_version"] == 1
        event_kwargs = event.await_args.kwargs
        assert event_kwargs["event_type"] == "subagent.result"
        assert event_kwargs["task_run_id"] == RUN_ID
        assert event_kwargs["payload"]["summary_chars"] == len("final answer")
        assert event_kwargs["payload"]["findings"] == 2
        assert event_kwargs["payload"]["artifact_ids"] == 1
        assert "final answer" not in str(event_kwargs["payload"])

    async def test_proposals_are_projected_and_counted(
        self, contract_flag, monkeypatch
    ):
        self._patch_owner(monkeypatch)
        contract = AsyncMock(return_value={"task_run_id": RUN_ID})
        event = AsyncMock(return_value={"event_id": 13})
        monkeypatch.setattr(db_runs, "record_subagent_contract", contract)
        monkeypatch.setattr(svc_events, "record_event", event)
        monkeypatch.setattr(
            dual_write,
            "_resolve_completed_result",
            AsyncMock(
                return_value=dual_write.StructuredSubagentResult(
                    summary="done",
                    proposed_memories=(
                        {
                            "type": "feedback", "text": "cite sources", "origin": "web",
                            "confidence": 0.4,
                            "evidence_refs": [
                                {"kind": "web_source", "id": "src-1", "verified": True}
                            ],
                        },
                        {"type": "user", "text": "prefers Chinese"},
                    ),
                )
            ),
        )
        await dual_write.record_subagent_result(
            thread_id=THREAD_ID,
            task_run_id=RUN_ID,
            task_id="task-1",
            status="completed",
        )
        result = contract.await_args.kwargs["result"]
        assert result["proposed_memories"] == [
            {
                "type": "feedback", "text": "cite sources",
                "change_kind": "long_term", "replaces_entry_key": None, "reason": None,
                "origin": "web", "confidence": 0.4,
                "evidence_refs": [{"kind": "web_source", "id": "src-1", "verified": False}], "evidence_count": 0,
                "counts_as_evidence": False,
                "metadata": {"sensitivity": "private"},
            },
            {
                "type": "user", "text": "prefers Chinese",
                "change_kind": "long_term", "replaces_entry_key": None, "reason": None,
                "origin": "assistant_inference", "confidence": 0.5,
                "evidence_refs": [], "evidence_count": 0,
                "counts_as_evidence": False,
                "metadata": {"sensitivity": "private"},
            },
        ]
        assert event.await_args.kwargs["payload"]["proposed_memories"] == 2
        assert "cite sources" not in str(event.await_args.kwargs["payload"])

    async def test_proposals_are_queued_for_review(
        self, contract_flag, monkeypatch
    ):
        self._patch_owner(monkeypatch)
        monkeypatch.setattr(
            db_runs, "record_subagent_contract", AsyncMock(return_value={"task_run_id": RUN_ID})
        )
        monkeypatch.setattr(svc_events, "record_event", AsyncMock(return_value=None))
        monkeypatch.setattr(
            dual_write,
            "_resolve_completed_result",
            AsyncMock(
                return_value=dual_write.StructuredSubagentResult(
                    summary="done",
                    proposed_memories=({"type": "feedback", "text": "cite sources"},),
                )
            ),
        )
        from src.server.services.refactor import proposals as proposals_svc

        queue = AsyncMock(return_value=1)
        monkeypatch.setattr(proposals_svc, "project_proposals", queue)
        written = await dual_write.record_subagent_result(
            thread_id=THREAD_ID,
            task_run_id=RUN_ID,
            task_id="task-1",
            status="completed",
        )
        assert written["proposals"] == 1
        queue.assert_awaited_once_with(
            user_id=USER_ID,
            settings_user_id=USER_ID,
            thread_id=THREAD_ID,
            task_run_id=RUN_ID,
            proposals=({"type": "feedback", "text": "cite sources"},),
        )

    async def test_oss_non_uuid_owner_is_mapped(
        self, contract_flag, monkeypatch
    ):
        from src.server.utils.pg_sanitize import refactor_owner_uuid

        self._patch_owner(monkeypatch, user_id="local-dev-user")
        contract = AsyncMock(return_value={"task_run_id": RUN_ID})
        event = AsyncMock(return_value=None)
        monkeypatch.setattr(db_runs, "record_subagent_contract", contract)
        monkeypatch.setattr(svc_events, "record_event", event)
        monkeypatch.setattr(
            dual_write,
            "_resolve_completed_result",
            AsyncMock(
                return_value=dual_write.StructuredSubagentResult(summary="done")
            ),
        )
        await dual_write.record_subagent_result(
            thread_id=THREAD_ID,
            task_run_id=RUN_ID,
            task_id="task-1",
            status="completed",
        )
        contract.assert_awaited_once()
        assert event.await_args.kwargs["user_id"] == refactor_owner_uuid("local-dev-user")

    @pytest.mark.parametrize("memory_enabled", [False, True])
    async def test_oss_capture_uses_original_settings_owner_and_mapped_projection_owner(
        self, contract_flag, monkeypatch, memory_enabled
    ):
        from src.server.database import conversation as conv_db
        from src.server.database.refactor import proposals as db_proposals
        from src.server.services import features
        from src.server.utils.pg_sanitize import refactor_owner_uuid

        monkeypatch.setenv("REFACTOR_MEMORY_INDEX_ENABLED", "1")
        monkeypatch.setattr(
            conv_db,
            "get_thread_auth_meta",
            AsyncMock(return_value={"user_id": "local-dev-user"}),
        )
        monkeypatch.setattr(
            db_runs, "record_subagent_contract", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(svc_events, "record_event", AsyncMock(return_value=None))
        monkeypatch.setattr(
            dual_write,
            "_resolve_completed_result",
            AsyncMock(
                return_value=dual_write.StructuredSubagentResult(
                    summary="done",
                    proposed_memories=({"type": "feedback", "text": "cite sources"},),
                )
            ),
        )

        settings_reads = []

        async def fake_settings_read(user_id):
            settings_reads.append(user_id)
            # This is the Settings override stored for the original OSS user.
            # A lookup under the deterministic projection UUID would miss it.
            return {
                "other_preference": {
                    "feature_overrides": {"memory": memory_enabled}
                }
            }

        monkeypatch.setattr(features, "get_user_preferences", fake_settings_read)
        insert = AsyncMock(return_value=1)
        monkeypatch.setattr(db_proposals, "insert_proposals", insert)

        await dual_write.record_subagent_result(
            thread_id=THREAD_ID,
            task_run_id=RUN_ID,
            task_id="task-1",
            status="completed",
        )

        assert settings_reads == ["local-dev-user"]
        if memory_enabled:
            insert.assert_awaited_once()
            assert insert.await_args.kwargs["user_id"] == refactor_owner_uuid(
                "local-dev-user"
            )
        else:
            insert.assert_not_awaited()

    async def test_completed_run_without_result_is_not_fabricated(
        self, contract_flag, monkeypatch
    ):
        self._patch_owner(monkeypatch)
        contract = AsyncMock()
        monkeypatch.setattr(db_runs, "record_subagent_contract", contract)
        monkeypatch.setattr(
            dual_write, "_resolve_completed_result", AsyncMock(return_value=None)
        )
        assert (
            await dual_write.record_subagent_result(
                thread_id=THREAD_ID,
                task_run_id=RUN_ID,
                task_id="task-1",
                status="completed",
            )
            is None
        )
        contract.assert_not_awaited()

    async def test_failed_run_summarizes_status_without_resolving(
        self, contract_flag, monkeypatch
    ):
        self._patch_owner(monkeypatch)
        contract = AsyncMock(return_value={"task_run_id": RUN_ID})
        monkeypatch.setattr(db_runs, "record_subagent_contract", contract)
        monkeypatch.setattr(svc_events, "record_event", AsyncMock(return_value=None))
        resolve = AsyncMock()
        monkeypatch.setattr(dual_write, "_resolve_completed_result", resolve)
        await dual_write.record_subagent_result(
            thread_id=THREAD_ID,
            task_run_id=RUN_ID,
            task_id="task-1",
            status="error",
            failure={"error": "boom"},
        )
        resolve.assert_not_awaited()
        assert contract.await_args.kwargs["result"]["summary"] == "run ended error: boom"

    async def test_unknown_owner_writes_nothing(self, contract_flag, monkeypatch):
        from src.server.database import conversation as conv_db

        monkeypatch.setattr(
            conv_db, "get_thread_auth_meta", AsyncMock(return_value=None)
        )
        contract = AsyncMock()
        monkeypatch.setattr(db_runs, "record_subagent_contract", contract)
        assert (
            await dual_write.record_subagent_result(
                thread_id=THREAD_ID,
                task_run_id=RUN_ID,
                task_id="task-1",
                status="completed",
            )
            is None
        )
        contract.assert_not_awaited()

    async def test_contract_write_failure_still_records_the_event(
        self, contract_flag, monkeypatch
    ):
        self._patch_owner(monkeypatch)
        monkeypatch.setattr(
            db_runs,
            "record_subagent_contract",
            AsyncMock(side_effect=RuntimeError("db")),
        )
        event = AsyncMock(return_value={"event_id": 12})
        monkeypatch.setattr(svc_events, "record_event", event)
        monkeypatch.setattr(
            dual_write,
            "_resolve_completed_result",
            AsyncMock(
                return_value=dual_write.StructuredSubagentResult(summary="done")
            ),
        )
        written = await dual_write.record_subagent_result(
            thread_id=THREAD_ID,
            task_run_id=RUN_ID,
            task_id="task-1",
            status="completed",
        )
        assert written == {"contract": None, "event": {"event_id": 12}}
        event.assert_awaited_once()


    async def test_contract_records_the_thread_mode_when_set(
        self, events_flag, monkeypatch
    ):
        from src.server.services.refactor import permissions as svc_permissions

        monkeypatch.setattr(
            svc_permissions, "effective_permission_mode", AsyncMock(return_value="manual")
        )
        upsert = AsyncMock(return_value={"conversation_response_id": RUN_ID})
        monkeypatch.setattr(db_runs, "upsert_contract", upsert)
        monkeypatch.setattr(svc_events, "record_event", AsyncMock(return_value=None))
        await dual_write.record_turn_finalized(
            user_id=USER_ID,
            thread_id=THREAD_ID,
            response_id=RUN_ID,
            status="completed",
        )
        assert upsert.await_args.kwargs["permission_mode"] == "manual"
