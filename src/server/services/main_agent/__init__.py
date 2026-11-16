"""Public main-agent service API."""

from __future__ import annotations

from typing import Any

from .adapters import (
    DeliberationAdapter,
    GateAdapter,
    LLMMainAgentAdapter,
    PassthroughClarificationGate,
    StubDeliberationAdapter,
    StubDivider,
    StubPlanner,
)
from .interfaces import MainAgentRepository
from .models import MainAgentThresholds
from .orchestrator import MainAgentOrchestrator, run_needs_reconciliation, validate_dag
from .production import (
    ConsensusPlanAdapter,
    ExecutionContextError,
    MainAgentClarificationGate,
    MachineDeliberationAdapter,
    ConfiguredDivider,
    resolve_execution_context,
)


def create_main_agent_orchestrator(
    *,
    llm_service: Any,
    repository: MainAgentRepository,
    clarification_gate: Any | None = None,
    deliberation: Any | None = None,
    thresholds: MainAgentThresholds | None = None,
) -> MainAgentOrchestrator:
    """Build production adapters over the durable Ticket and deliberation stores."""
    llm = LLMMainAgentAdapter(llm_service)
    from src.server.database.main_agent_bridge import create_run_with_root_ticket

    orchestrator = MainAgentOrchestrator(
        repository=repository,
        divider=ConfiguredDivider(llm),
        deliberation=deliberation or MachineDeliberationAdapter(llm_service),
        planner=ConsensusPlanAdapter(),
        merger=llm,
        reply_synthesizer=llm,
        clarification_gate=clarification_gate or MainAgentClarificationGate(),
        run_creator=create_run_with_root_ticket,
        execution_context_resolver=resolve_execution_context,
        thresholds=thresholds,
    )
    from src.server.services.main_agent.bridge_sync import reconcile_child_tickets
    from src.server.services.main_agent.production import finalize_main_agent_run

    orchestrator.child_ticket_reconciler = lambda run, user_id: reconcile_child_tickets(
        orchestrator, run, user_id
    )
    orchestrator.run_finalizer = finalize_main_agent_run
    return orchestrator


__all__ = [
    "DeliberationAdapter",
    "GateAdapter",
    "LLMMainAgentAdapter",
    "MainAgentOrchestrator",
    "MainAgentRepository",
    "MainAgentThresholds",
    "PassthroughClarificationGate",
    "StubDeliberationAdapter",
    "StubDivider",
    "StubPlanner",
    "create_main_agent_orchestrator",
    "ConsensusPlanAdapter",
    "ExecutionContextError",
    "MainAgentClarificationGate",
    "MachineDeliberationAdapter",
    "run_needs_reconciliation",
    "validate_dag",
]
