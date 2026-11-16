from __future__ import annotations

import json

import pytest

from src.llms.native_cli import registry
from src.server.database import workspace
from src.server.database.autonomy import daemons, sources
from src.server.services.main_agent.production import (
    ConsensusPlanAdapter,
    DaemonTicketParticipantAdapter,
    ExecutionContextError,
    MachineDeliberationAdapter,
    MainAgentExecutionContext,
    SourceUnavailableError,
    build_deliberation_objective,
    finalize_main_agent_run,
    resolve_execution_context,
)


@pytest.mark.asyncio
async def test_execution_context_requires_owned_provider_matched_daemons(monkeypatch):
    monkeypatch.setattr(
        workspace,
        "get_workspace",
        lambda _workspace_id: _resolved({"workspace_id": "ws-1", "user_id": "user-1"}),
    )
    monkeypatch.setattr(
        daemons,
        "get_daemon",
        lambda _user_id, daemon_id: _resolved(
            {"daemon_id": daemon_id, "user_id": "user-1", "provider": "codex"}
        ),
    )
    monkeypatch.setattr(
        registry,
        "list_adapters",
        lambda: [{"provider": provider} for provider in ("codex", "kimi", "grok")],
    )

    resolved = await resolve_execution_context(
        "user-1",
        {
            "workspace_id": "ws-1",
            "agent_daemons": {"codex": "daemon-1"},
            "deliberation": {
                "mode": "mock",
                "max_rounds": 2,
                "participants": [
                    {"agent_key": "planner", "role": "planner"},
                    {"agent_key": "codex", "role": "participant"},
                    {"agent_key": "kimi", "role": "participant"},
                ],
            },
        },
    )

    assert resolved.workspace_id == "ws-1"
    assert resolved.daemon_for("codex") == "daemon-1"


@pytest.mark.asyncio
async def test_execution_context_rejects_provider_mismatch_and_raw_route_fields(monkeypatch):
    monkeypatch.setattr(
        workspace,
        "get_workspace",
        lambda _workspace_id: _resolved({"workspace_id": "ws-1", "user_id": "user-1"}),
    )
    monkeypatch.setattr(
        daemons,
        "get_daemon",
        lambda _user_id, daemon_id: _resolved(
            {"daemon_id": daemon_id, "user_id": "user-1", "provider": "kimi"}
        ),
    )
    monkeypatch.setattr(registry, "list_adapters", lambda: [{"provider": "codex"}])

    with pytest.raises(ExecutionContextError, match="different provider"):
        await resolve_execution_context(
            "user-1",
            {"workspace_id": "ws-1", "agent_daemons": {"codex": "daemon-1"}},
        )
    with pytest.raises(ExecutionContextError, match="provider and worker tokens"):
        await resolve_execution_context("user-1", {"workspace_id": "ws-1", "daemon_id": "daemon-1"})


@pytest.mark.asyncio
async def test_consensus_adapter_preserves_the_agreed_dag_and_assignments():
    consensus = {
        "summary": "agreed plan",
        "steps": [
            {"step_key": "research", "title": "Research", "prompt": "Find facts", "assigned_agent": "codex", "depends_on": []},
            {"step_key": "review", "title": "Review", "prompt": "Check facts", "assigned_agent": "grok", "depends_on": ["research"]},
        ],
    }
    result = await ConsensusPlanAdapter().plan(
        {"context": {"agent_daemons": {"codex": "c", "grok": "g"}}},
        [{"id": "discarded", "description": "must not replace consensus"}],
        {"consensus_plan": consensus, "dissent": [{"agent_id": "kimi", "reason": "risk"}]},
    )

    assert result["tasks"] == [
        {"id": "research", "description": "Find facts", "depends_on": [], "deliberation_agent_id": "codex"},
        {"id": "review", "description": "Check facts", "depends_on": ["research"], "deliberation_agent_id": "grok"},
    ]
    assert result["consensus_plan"] == consensus
    assert result["dissent"] == [{"agent_id": "kimi", "reason": "risk"}]


@pytest.mark.asyncio
async def test_daemon_participant_reads_full_structured_report_and_synthesis_plan(monkeypatch):
    from src.server.database import main_agent_bridge
    from src.server.database.autonomy import tickets

    plan = {"summary": "agreed", "steps": [{"step_key": "one", "title": "One", "prompt": "Do one", "assigned_agent": "codex", "depends_on": []}]}
    raw = json.dumps({"body": "x" * 2500, "plan": plan})
    monkeypatch.setattr(
        main_agent_bridge,
        "create_deliberation_worker_ticket",
        lambda **_kwargs: _resolved({"ticket_id": "child-1"}),
    )
    monkeypatch.setattr(
        tickets,
        "get_ticket",
        lambda _ticket_id: _resolved({"ticket_id": "child-1", "status": "ready", "summary": "truncated"}),
    )
    monkeypatch.setattr(tickets, "latest_report", lambda _ticket_id: _resolved({"summary": raw}))
    adapter = DaemonTicketParticipantAdapter(
        agent_key="codex",
        role="planner",
        request={"user_id": "u1", "ticket_id": "root-1"},
        execution=MainAgentExecutionContext(
            workspace_id="ws-1",
            source_id=None,
            source_kind=None,
            thread_key=None,
            repository="",
            api_base_url="",
            agent_daemons={"codex": "d1"},
            default_provider="",
        ),
    )
    result = await adapter.synthesize({"deliberation_id": "delib-1"}, [])
    assert result == plan


@pytest.mark.asyncio
async def test_github_source_requires_and_preserves_canonical_thread_key(monkeypatch):
    monkeypatch.setattr(
        sources,
        "get_source",
        lambda _user_id, _source_id: _resolved(
            {"source_id": "s1", "user_id": "user-1", "kind": "github", "enabled": True,
             "config": {"repository": "Acme/Repo", "workspace_id": "ws-1"}}
        ),
    )
    monkeypatch.setattr(
        workspace,
        "get_workspace",
        lambda _workspace_id: _resolved({"workspace_id": "ws-1", "user_id": "user-1"}),
    )
    monkeypatch.setattr(registry, "list_adapters", lambda: [{"provider": "codex"}])
    context = {
        "source_id": "s1",
        "thread_key": "github:acme/repo#42",
        "agent_daemons": {},
    }
    result = await resolve_execution_context("user-1", context)
    assert result.thread_key == "github:acme/repo#42"
    with pytest.raises(ExecutionContextError, match="canonical thread key"):
        await resolve_execution_context(
            "user-1", {**context, "thread_key": "acme/repo#42"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, {"enabled": False}])
async def test_missing_or_disabled_source_has_typed_unavailable_error(
    monkeypatch, source
):
    monkeypatch.setattr(
        sources,
        "get_source",
        lambda _user_id, _source_id: _resolved(source),
    )

    with pytest.raises(SourceUnavailableError, match="Source is not found or is disabled"):
        await resolve_execution_context("user-1", {"source_id": "s1"})


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, {"enabled": False}])
async def test_finalizer_types_missing_or_disabled_reply_source(monkeypatch, source):
    monkeypatch.setattr(
        sources,
        "get_source",
        lambda _user_id, _source_id: _resolved(source),
    )
    run = {
        "id": "run-1",
        "user_id": "user-1",
        "request": {
            "context": {"source_id": "s1", "thread_key": "github:acme/repo#42"}
        },
    }

    with pytest.raises(SourceUnavailableError, match="GitHub reply Source"):
        await finalize_main_agent_run(run, "completed", {"content": "done"})


@pytest.mark.asyncio
async def test_finalizer_keeps_invalid_reply_thread_retryable(monkeypatch):
    monkeypatch.setattr(
        sources,
        "get_source",
        lambda _user_id, _source_id: _resolved({"enabled": True}),
    )
    run = {
        "id": "run-1",
        "user_id": "user-1",
        "request": {"context": {"source_id": "s1", "thread_key": ""}},
    }

    with pytest.raises(ExecutionContextError, match="source or thread") as raised:
        await finalize_main_agent_run(run, "completed", {"content": "done"})
    assert type(raised.value) is ExecutionContextError


@pytest.mark.asyncio
async def test_machine_deliberation_persists_division_in_every_participant_context(monkeypatch):
    from src.server.database.autonomy import deliberations as deliberation_db
    from src.server.services.deliberation import machine

    tasks = [
        {
            "id": "spec",
            "description": "Define API behavior",
            "depends_on": [],
            "deliberation_agent_id": "codex",
        },
        {
            "id": "implementation",
            "description": "Implement the agreed API",
            "depends_on": ["spec"],
            "deliberation_agent_id": "kimi",
        },
        {
            "id": "review",
            "description": "Check the implementation against the spec",
            "depends_on": ["spec", "implementation"],
            "deliberation_agent_id": "grok",
        },
    ]
    participants = [
        {"agent_key": "planner", "role": "planner", "adapter": "mock"},
        {"agent_key": "codex", "role": "participant", "adapter": "mock"},
        {"agent_key": "kimi", "role": "participant", "adapter": "mock"},
        {"agent_key": "grok", "role": "participant", "adapter": "mock"},
    ]
    captured = {}

    class Store:
        async def load_for_ticket(self, _ticket_id):
            return None

    store = Store()

    async def create(_store, **kwargs):
        captured.update(kwargs)
        return {"deliberation_id": "deliberation-1", "participants": participants}

    async def start(_store, deliberation_id, adapters, **_kwargs):
        captured["adapter_keys"] = sorted(adapters)
        row = {
            "deliberation_id": deliberation_id,
            "ticket_id": "root-1",
            "objective": captured["objective"],
            "messages": [],
        }
        captured["participant_context"] = machine._ctx(
            row,
            {"agent_key": "codex", "role": "participant"},
            round_index=1,
            kind="proposal",
        )
        return {
            "transcript": [
                {"kind": "proposal", "agent_key": key, "body": f"{key} considered the division"}
                for key in ("codex", "kimi", "grok")
            ],
            "consensus_plan": {"summary": "consensus", "steps": []},
            "dissents": [],
            "decision_reason": "all participants agreed",
            "deliberation_id": deliberation_id,
        }

    monkeypatch.setattr(deliberation_db, "PostgresDeliberationStore", lambda: store)
    monkeypatch.setattr(machine, "create_deliberation", create)
    monkeypatch.setattr(machine, "start_deliberation", start)

    result = await MachineDeliberationAdapter(llm_service=object()).deliberate(
        {
            "user_id": "user-1",
            "ticket_id": "root-1",
            "objective": "Implement the API",
            "context": {"deliberation": {"mode": "mock"}},
        },
        tasks,
    )

    assert captured["objective"] == build_deliberation_objective(
        "Implement the API", tasks
    )
    for subquestion in tasks:
        assert subquestion["id"] in captured["participant_context"]["objective"]
        assert subquestion["description"] in captured["participant_context"]["objective"]
    assert "address the subquestions assigned to your agent key" in captured["objective"]
    assert "critique omissions, conflicts, and dependencies" in captured["objective"]
    assert captured["adapter_keys"] == ["codex", "grok", "kimi", "planner"]
    assert len(result["conclusions"]) == 3


@pytest.mark.asyncio
async def test_fresh_running_deliberation_is_not_replayed(monkeypatch):
    from src.server.database.autonomy import deliberations as deliberation_db
    from src.server.services.deliberation import machine

    class Store:
        async def load_for_ticket(self, _ticket_id):
            return {"deliberation_id": "existing", "status": "running"}

        async def fail_stale_running(self, _deliberation_id):
            return False

    async def unexpected(*_args, **_kwargs):
        raise AssertionError("fresh running deliberation must not be replayed")

    monkeypatch.setattr(deliberation_db, "PostgresDeliberationStore", Store)
    monkeypatch.setattr(machine, "create_deliberation", unexpected)
    monkeypatch.setattr(machine, "start_deliberation", unexpected)

    with pytest.raises(ExecutionContextError, match="still running"):
        await MachineDeliberationAdapter(llm_service=object()).deliberate(
            {
                "user_id": "user-1",
                "ticket_id": "ticket-1",
                "objective": "Plan work",
                "context": {"deliberation": {"mode": "mock"}},
            },
            [],
        )


@pytest.mark.asyncio
async def test_stale_running_deliberation_recovers_into_a_new_run(monkeypatch):
    from src.server.database.autonomy import deliberations as deliberation_db
    from src.server.services.deliberation import machine

    participants = [
        {"agent_key": "planner", "role": "planner", "adapter": "mock"},
        {"agent_key": "codex", "role": "participant", "adapter": "mock"},
        {"agent_key": "kimi", "role": "participant", "adapter": "mock"},
        {"agent_key": "grok", "role": "participant", "adapter": "mock"},
    ]
    calls: dict[str, list] = {"created": [], "started": [], "stale": []}

    class Store:
        async def load_for_ticket(self, _ticket_id):
            return {"deliberation_id": "stale-run", "status": "running"}

        async def fail_stale_running(self, deliberation_id):
            calls["stale"].append(deliberation_id)
            return True

    async def create(_store, **_kwargs):
        calls["created"].append("new-run")
        return {"deliberation_id": "new-run", "participants": participants}

    async def start(_store, deliberation_id, _adapters, **_kwargs):
        calls["started"].append(deliberation_id)
        return {
            "transcript": [
                {"kind": "proposal", "agent_key": "codex", "body": "plan"},
                {"kind": "proposal", "agent_key": "kimi", "body": "plan"},
                {"kind": "proposal", "agent_key": "grok", "body": "plan"},
            ],
            "consensus_plan": {"summary": "consensus", "steps": []},
            "dissents": [],
            "decision_reason": "all participants agreed",
            "deliberation_id": deliberation_id,
        }

    monkeypatch.setattr(deliberation_db, "PostgresDeliberationStore", Store)
    monkeypatch.setattr(machine, "create_deliberation", create)
    monkeypatch.setattr(machine, "start_deliberation", start)

    result = await MachineDeliberationAdapter(llm_service=object()).deliberate(
        {
            "user_id": "user-1",
            "ticket_id": "ticket-1",
            "objective": "Plan work",
            "context": {"deliberation": {"mode": "mock"}},
        },
        [
            {
                "id": "task-1",
                "description": "Plan the task",
                "deliberation_agent_id": "codex",
            }
        ],
    )

    assert calls == {
        "created": ["new-run"],
        "started": ["new-run"],
        "stale": ["stale-run"],
    }
    assert result["deliberation_id"] == "new-run"


async def _resolved(value):
    return value
