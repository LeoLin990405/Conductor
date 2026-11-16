"""Resolve owned autonomy resources for Main Agent execution.

Routing comes from durable, user-owned Source and daemon rows. The request may
select those rows, but it cannot supply a provider or a worker token.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import BaseModel, Field

from src.server.services.deliberation.thread_port import DatabaseTicketThreadPort


class ExecutionContextError(ValueError):
    """The requested workspace, source, or daemon is not usable by this user."""


class SourceUnavailableError(ExecutionContextError):
    """A durable run refers to a Source that was deleted or disabled."""


@dataclass(frozen=True, slots=True)
class MainAgentExecutionContext:
    workspace_id: str
    source_id: str | None
    source_kind: str | None
    thread_key: str | None
    repository: str
    api_base_url: str
    agent_daemons: dict[str, str]
    default_provider: str

    def daemon_for(self, agent_key: str) -> str:
        daemon_id = self.agent_daemons.get(agent_key)
        if daemon_id:
            return daemon_id
        if agent_key == "default" and self.agent_daemons.get("default"):
            return self.agent_daemons["default"]
        raise ExecutionContextError(
            f"No owned {agent_key} daemon is configured for this task; "
            "add it to context.agent_daemons or assign a compatible agent."
        )


class _Step(BaseModel):
    step_key: str
    title: str
    prompt: str
    assigned_agent: str
    depends_on: list[str] = Field(default_factory=list)


class _Plan(BaseModel):
    summary: str
    steps: list[_Step]


class _Turn(BaseModel):
    body: str
    critique: str = ""
    plan: _Plan


class _Vote(BaseModel):
    ballot: str
    comment: str = ""


class LLMDeliberationParticipantAdapter:
    """One user-configured LLM participant for the persisted deliberation machine."""

    def __init__(self, llm_service: Any, *, user_id: str, agent_key: str):
        self._llm = llm_service
        self._user_id = user_id
        self._agent_key = agent_key

    async def _complete(self, *, ctx: Mapping[str, Any], instruction: str, schema: type[BaseModel]) -> dict[str, Any]:
        import asyncio
        import json

        response = await asyncio.wait_for(
            self._llm.complete(
                user_id=self._user_id,
                system_prompt=(
                    f"You are independent deliberation participant {self._agent_key}. "
                    f"{instruction} Return a concrete plan, preserve dependencies, and do not claim execution."
                ),
                user_prompt=json.dumps(dict(ctx), ensure_ascii=False, sort_keys=True),
                response_schema=schema,
                slot="primary",
            ),
            timeout=45,
        )
        return response.model_dump(mode="json") if isinstance(response, BaseModel) else dict(response)

    async def propose(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await self._complete(ctx=ctx, instruction="Propose an initial DAG plan.", schema=_Turn)

    async def critique(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await self._complete(ctx=ctx, instruction="Critique the current proposal and return a revised DAG plan.", schema=_Turn)

    async def vote(self, ctx: dict[str, Any], synthesis: dict[str, Any]) -> dict[str, Any]:
        payload = {**ctx, "candidate_consensus": synthesis}
        result = await self._complete(
            ctx=payload,
            instruction="Vote agree or dissent on the candidate consensus. Explain dissent clearly.",
            schema=_Vote,
        )
        if result.get("ballot") not in {"agree", "dissent"}:
            raise ExecutionContextError("LLM deliberation returned an invalid ballot")
        return result

    async def synthesize(self, ctx: dict[str, Any], proposals: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._complete(
            ctx={**ctx, "proposals": proposals},
            instruction="Reconcile the proposals into one consensus DAG while preserving useful dissent risks.",
            schema=_Plan,
        )


class ConsensusPlanAdapter:
    """Convert the deliberation consensus DAG directly into executable tasks."""

    async def plan(
        self,
        request: Mapping[str, Any],
        tasks: list[dict[str, Any]],
        deliberation: Mapping[str, Any],
    ) -> dict[str, Any]:
        del tasks
        consensus = deliberation.get("consensus_plan")
        steps = consensus.get("steps") if isinstance(consensus, Mapping) else None
        if not isinstance(steps, list) or not steps:
            raise ExecutionContextError("Deliberation returned no consensus DAG")
        allowed_agents = set(dict((request.get("context") or {}).get("agent_daemons") or {}))
        dag_tasks: list[dict[str, Any]] = []
        for step in steps:
            if not isinstance(step, Mapping):
                raise ExecutionContextError("Consensus DAG contains an invalid step")
            assigned_agent = str(step.get("assigned_agent") or "").strip()
            if assigned_agent not in allowed_agents:
                raise ExecutionContextError(
                    f"Consensus step {step.get('step_key')} assigns {assigned_agent or 'no provider'}; "
                    "configure that provider in context.agent_daemons."
                )
            dag_tasks.append(
                {
                    "id": str(step.get("step_key") or ""),
                    "description": str(step.get("prompt") or step.get("title") or "").strip(),
                    "depends_on": list(step.get("depends_on") or []),
                    "deliberation_agent_id": assigned_agent,
                }
            )
        return {
            "tasks": dag_tasks,
            "consensus_plan": dict(consensus),
            "dissent": list(deliberation.get("dissent") or []),
            "decision_reason": str(deliberation.get("decision_reason") or ""),
            "adapter": "consensus_conversion",
        }


class ConfiguredDivider:
    """Keep the LLM's subquestions while binding each to a configured agent key."""

    def __init__(self, delegate: Any):
        self._delegate = delegate

    async def divide(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        tasks = await self._delegate.divide(request)
        context = dict(request.get("context") or {})
        definitions = dict(context.get("deliberation") or {}).get("participants") or [
            {"agent_key": key, "role": role}
            for key, role in (("planner", "planner"), ("codex", "participant"), ("kimi", "participant"), ("grok", "participant"))
        ]
        participant_keys = [str(item["agent_key"]) for item in definitions if item.get("role") == "participant"]
        if not participant_keys:
            raise ExecutionContextError("Configure at least two deliberation participants")
        normalized = []
        for index, raw in enumerate(tasks):
            task = dict(raw)
            assigned = str(task.get("deliberation_agent_id") or "")
            task["deliberation_agent_id"] = assigned if assigned in participant_keys else participant_keys[index % len(participant_keys)]
            normalized.append(task)
        return normalized


def build_deliberation_objective(
    objective: str, tasks: list[Mapping[str, Any]]
) -> str:
    """Carry the accepted division into every durable deliberation turn."""
    import json

    subquestions = [
        {
            "id": str(task.get("id") or ""),
            "description": str(task.get("description") or ""),
            "depends_on": [str(item) for item in task.get("depends_on", [])],
            "deliberation_agent_id": str(task.get("deliberation_agent_id") or ""),
        }
        for task in tasks
    ]
    encoded = json.dumps(subquestions, ensure_ascii=False, separators=(",", ":"))
    return (
        f"User objective: {objective.strip()}\n\n"
        f"Assigned subquestions (JSON): {encoded}\n\n"
        "Deliberation instructions: address the subquestions assigned to your agent key; "
        "review the other assignments and critique omissions, conflicts, and dependencies. "
        "The planner must produce a consensus plan that covers these subquestions, respects "
        "their dependencies and agent assignments, and does not claim that work was executed."
    )


class MainAgentClarificationGate:
    """Run the real Ticket gate and confirm its durable clarification row."""

    async def check(self, request: Mapping[str, Any]) -> dict[str, Any]:
        from src.server.database.autonomy import clarifications as clarification_db
        from src.server.database.autonomy import tickets as ticket_db
        from src.server.services.autonomy.clarify import gate

        ticket_id = str(request.get("ticket_id") or "")
        ticket = await ticket_db.get_ticket(ticket_id)
        if ticket is None:
            raise ExecutionContextError("Main Agent root Ticket was not found")
        result = await gate(ticket=ticket, enabled=True)
        row = await clarification_db.get_active_clarification(ticket_id)
        if result == "awaiting" or row is not None:
            return {
                "required": True,
                "questions": list((row or {}).get("open_questions") or []),
                "clarification_id": (row or {}).get("clarification_id"),
                "expanded_request": (row or {}).get("expanded_request"),
                "assumptions": list((row or {}).get("assumptions") or []),
                "status": "awaiting_clarification",
                "adapter": "autonomy_ticket_gate",
            }
        return {"required": False, "questions": [], "status": result, "adapter": "autonomy_ticket_gate"}

    async def confirm(self, request: Mapping[str, Any], text: str) -> dict[str, Any]:
        from src.server.services.autonomy.clarify import confirm_clarification

        ticket_id = str(request.get("ticket_id") or "")
        ticket = await confirm_clarification(
            ticket_id=ticket_id, text=text, enqueue_launch=False
        )
        return ticket


async def finalize_main_agent_run(run: Mapping[str, Any], status: str, result: Mapping[str, Any]) -> None:
    from src.server.database.autonomy import effects as effects_db
    from src.server.database.autonomy import outbox as outbox_db
    from src.server.database.autonomy import sources as source_db
    from src.server.database.main_agent_bridge import finalize_root_ticket
    from src.server.services.autonomy.models import EFFECT_REPLY

    request = dict(run.get("request") or {})
    user_id = str(run.get("user_id") or request.get("user_id") or "")
    ticket_id = str(request.get("ticket_id") or "")
    if status == "completed":
        reply = dict(result)
        body = str(reply.get("content") or reply.get("summary") or "").strip()
        if not body:
            raise ExecutionContextError("Main Agent produced an empty final reply")
        context = dict(request.get("context") or {})
        source_id = str(context.get("source_id") or "")
        thread_key = str(context.get("thread_key") or "")
        if source_id:
            source = await source_db.get_source(user_id, source_id)
            if source is None or not source.get("enabled"):
                raise SourceUnavailableError(
                    "GitHub reply Source is not found or is disabled"
                )
            if not thread_key:
                raise ExecutionContextError("GitHub reply source or thread is no longer valid")
            idempotency_key = f"main-agent:reply:{run.get('id')}"
            outbox = await outbox_db.enqueue_outbox(
                user_id=user_id,
                ticket_id=ticket_id,
                source_id=source_id,
                thread_key=thread_key,
                body=body[:20000],
                idempotency_key=idempotency_key,
            ) or await outbox_db.get_by_idempotency_key(idempotency_key)
            if outbox is None:
                raise ExecutionContextError("Could not persist final reply to the source outbox")
            await effects_db.enqueue_effect(
                user_id=user_id,
                kind=EFFECT_REPLY,
                idempotency_key=f"main-agent:reply-effect:{run.get('id')}",
                payload={"outbox_id": str(outbox["outbox_id"])},
            )
        summary = body
        ticket_status = "completed"
    else:
        summary = str(result.get("error") or "Main Agent stopped before completing the request")
        ticket_status = "failed"
    await finalize_root_ticket(
        ticket_id=ticket_id,
        user_id=user_id,
        status=ticket_status,
        summary=summary,
    )


DurableTicketThreadPort = DatabaseTicketThreadPort


class DaemonTicketParticipantAdapter:
    """Ask a registered daemon through an ordinary lease and report Ticket."""

    def __init__(self, *, agent_key: str, role: str, request: Mapping[str, Any], execution: MainAgentExecutionContext):
        self.agent_key = agent_key
        self.role = role
        self.request = dict(request)
        self.execution = execution

    async def _ask(self, ctx: dict[str, Any], instruction: str, *, synthesis: dict[str, Any] | None = None) -> dict[str, Any]:
        import asyncio
        import json
        from src.server.database.main_agent_bridge import create_deliberation_worker_ticket
        from src.server.database.autonomy.tickets import get_ticket, latest_report

        ticket_id = str(self.request.get("ticket_id") or "")
        deliberation_id = str(ctx.get("deliberation_id") or "")
        daemon_id = self.execution.daemon_for(self.agent_key)
        payload = {"instruction": instruction, "role": self.role, "context": ctx, "candidate_consensus": synthesis}
        prompt = (
            "Return only a JSON object with fields body, critique, plan, and ballot. "
            "For plan use {summary,steps:[{step_key,title,prompt,assigned_agent,depends_on}]}. "
            "Do not execute work or claim results.\n" + json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )
        worker_ticket = await create_deliberation_worker_ticket(
            user_id=str(self.request.get("user_id") or ""),
            workspace_id=self.execution.workspace_id,
            root_ticket_id=ticket_id,
            deliberation_id=deliberation_id,
            agent_key=self.agent_key,
            daemon_id=daemon_id,
            prompt=prompt,
        )
        deadline = asyncio.get_running_loop().time() + 180
        while asyncio.get_running_loop().time() < deadline:
            current = await get_ticket(str(worker_ticket["ticket_id"]))
            if current is None:
                raise ExecutionContextError("Deliberation daemon Ticket disappeared")
            state = str(current.get("status") or "")
            if state in {"ready", "completed"}:
                report = await latest_report(str(worker_ticket["ticket_id"]))
                raw = str((report or {}).get("summary") or current.get("summary") or "")
                if raw.startswith("```"):
                    raw = raw.strip("`\n ")
                    if raw.lower().startswith("json"):
                        raw = raw[4:].lstrip()
                try:
                    result = json.loads(raw)
                except ValueError as exc:
                    raise ExecutionContextError("Daemon deliberation report must contain valid JSON in Ticket summary") from exc
                if not isinstance(result, dict):
                    raise ExecutionContextError("Daemon deliberation report must be a JSON object")
                return result
            if state in {"failed", "cancelled", "blocked"}:
                raise ExecutionContextError(f"Daemon deliberation Ticket ended as {state}: {current.get('error') or current.get('summary') or ''}")
            await asyncio.sleep(1)
        raise ExecutionContextError(
            f"Timed out waiting for {self.agent_key} deliberation daemon; Ticket {worker_ticket['ticket_id']} remains available for recovery."
        )

    async def propose(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await self._ask(ctx, "Propose an initial DAG plan.")

    async def critique(self, ctx: dict[str, Any]) -> dict[str, Any]:
        return await self._ask(ctx, "Critique the current proposal and return a revised DAG plan.")

    async def vote(self, ctx: dict[str, Any], synthesis: dict[str, Any]) -> dict[str, Any]:
        return await self._ask(ctx, "Vote agree or dissent on the candidate consensus and explain dissent in comment.", synthesis=synthesis)

    async def synthesize(self, ctx: dict[str, Any], proposals: list[dict[str, Any]]) -> dict[str, Any]:
        result = await self._ask({**ctx, "proposals": proposals}, "Reconcile proposals into the consensus DAG; put the DAG under plan.")
        plan = result.get("plan")
        if not isinstance(plan, dict):
            raise ExecutionContextError("Daemon synthesis report must include a plan object")
        return plan


class MachineDeliberationAdapter:
    """Run deliberation through the durable machine and shared Ticket thread."""

    def __init__(self, llm_service: Any):
        self._llm_service = llm_service

    async def deliberate(self, request: Mapping[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
        from src.server.database.autonomy.deliberations import PostgresDeliberationStore
        from src.server.services.deliberation.machine import create_deliberation, start_deliberation
        from src.server.services.deliberation.mock_adapter import MockParticipantAdapter
        from src.server.services.deliberation.errors import DeliberationError

        context = dict(request.get("context") or {})
        options = dict(context.get("deliberation") or {})
        mode = str(options.get("mode") or "llm")
        user_id = str(request.get("user_id") or "")
        default = [
            {"agent_key": "planner", "role": "planner"},
            {"agent_key": "codex", "role": "participant"},
            {"agent_key": "kimi", "role": "participant"},
            {"agent_key": "grok", "role": "participant"},
        ]
        definitions = options.get("participants") or default
        if mode == "mock":
            holdout_key = str(options.get("holdout_key") or "grok")
            participant_rows = [{**item, "adapter": "mock"} for item in definitions]
        elif mode == "daemon":
            holdout_key = ""
            execution = await resolve_execution_context(user_id, context)
            if not options.get("participants"):
                configured = sorted(key for key in execution.agent_daemons if key != "default")
                if len(configured) < 3:
                    raise ExecutionContextError(
                        "Daemon deliberation requires three provider assignments in context.agent_daemons "
                        "or explicit participants with matching provider keys."
                    )
                definitions = [
                    {"agent_key": configured[0], "role": "planner"},
                    {"agent_key": configured[1], "role": "participant"},
                    {"agent_key": configured[2], "role": "participant"},
                ]
            participant_rows = [
                {**item, "adapter": "daemon", "daemon_id": execution.daemon_for(str(item["agent_key"]))}
                for item in definitions
            ]
        else:
            holdout_key = ""
            participant_rows = [{**item, "adapter": "llm"} for item in definitions]
        ticket_id = str(request.get("ticket_id") or "")
        objective = build_deliberation_objective(
            str(request.get("objective") or request.get("prompt") or ""), tasks
        )
        store = PostgresDeliberationStore()
        thread_port = DurableTicketThreadPort()
        existing = await store.load_for_ticket(ticket_id)
        if existing is not None and existing.get("status") == "running":
            stale = await store.fail_stale_running(str(existing["deliberation_id"]))
            if not stale:
                raise ExecutionContextError(
                    f"Deliberation {existing.get('deliberation_id')} is still running; "
                    "retry after its 30-minute recovery window if its worker stopped."
                )
            existing = None
        if existing is not None and existing.get("status") == "failed":
            existing = None
        if existing is not None:
            created = existing
        else:
            try:
                created = await create_deliberation(
                    store,
                    ticket_id=ticket_id,
                    user_id=user_id,
                    objective=objective,
                    participants=participant_rows,
                    max_rounds=int(options.get("max_rounds", 3)),
                    thread_port=thread_port,
                )
            except DeliberationError as exc:
                if getattr(exc, "code", "") != "already_exists":
                    raise
                created = await store.load_for_ticket(ticket_id)
                if created is None:
                    raise
        adapters: dict[str, Any] = {}
        persisted_participants = list(created.get("participants") or participant_rows)
        script = str(options.get("script") or "consensus")
        if script not in {"consensus", "holdout"}:
            raise ExecutionContextError("context.deliberation.script must be consensus or holdout")
        holdout_key = str(options.get("holdout_key") or "grok") if script == "holdout" else ""
        for item in persisted_participants:
            key = str(item["agent_key"])
            adapters[key] = (
                MockParticipantAdapter(
                    agent_key=key,
                    role=str(item["role"]),
                    script=script,
                    holdout_key=holdout_key,
                )
                if mode == "mock"
                else DaemonTicketParticipantAdapter(agent_key=key, role=str(item["role"]), request=request, execution=execution)
                if mode == "daemon"
                else LLMDeliberationParticipantAdapter(self._llm_service, user_id=user_id, agent_key=key)
            )
        result = await start_deliberation(
            store,
            str(created["deliberation_id"]),
            adapters,
            thread_port=thread_port,
            provider_groups=options.get("provider_groups"),
        )
        transcript = list(result.get("transcript") or [])
        conclusions = []
        latest: dict[str, str] = {}
        for msg in transcript:
            if msg.get("kind") in {"proposal", "revision", "critique"} and msg.get("body"):
                latest[str(msg.get("agent_key") or "")] = str(msg["body"])
        for task in tasks:
            agent = str(task.get("deliberation_agent_id") or "")
            if not agent:
                raise DeliberationError("bad_state", f"subquestion {task.get('id')} has no deliberation agent")
            conclusion = latest.get(agent)
            if not conclusion:
                raise DeliberationError("bad_state", f"no deliberation conclusion from {agent}")
            conclusions.append({"agent_id": agent, "task_id": str(task["id"]), "subquestion": str(task.get("description") or ""), "conclusion": conclusion})
        return {
            "conclusions": conclusions,
            "consensus_plan": dict(result.get("consensus_plan") or {}),
            "dissent": list(result.get("dissents") or []),
            "decision_reason": result.get("decision_reason", ""),
            "deliberation_id": result.get("deliberation_id"),
            "adapter": f"postgres_{mode}_deliberation",
        }


async def resolve_execution_context(
    user_id: str,
    context: Mapping[str, Any],
    *,
    default_workspace_id: str | None = None,
) -> MainAgentExecutionContext:
    """Validate IDs and derive routing from owned Source/daemon records.

    Daemon heartbeat freshness and capacity remain the ordinary daemon claim
    endpoint's job; successful validation here does not claim a daemon is live.
    """
    from src.server.database.autonomy import daemons as daemon_db
    from src.server.database.autonomy import sources as source_db
    from src.server.database.workspace import get_workspace, get_workspaces_for_user
    from src.llms.native_cli.registry import list_adapters

    if not isinstance(context, Mapping):
        raise ExecutionContextError("context must be an object")
    forbidden = {"daemon_id", "worker_daemon_id", "worker_provider", "token"}
    if forbidden.intersection(context):
        raise ExecutionContextError(
            "Use context.agent_daemons with owned daemon IDs; provider and worker tokens are server-controlled."
        )

    deliberation = context.get("deliberation")
    if deliberation is not None:
        if not isinstance(deliberation, Mapping):
            raise ExecutionContextError("context.deliberation must be an object")
        mode = str(deliberation.get("mode") or "")
        if mode not in {"mock", "llm", "daemon"}:
            raise ExecutionContextError("context.deliberation.mode must be mock, llm, or daemon")
        max_rounds = deliberation.get("max_rounds", 3)
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or not 1 <= max_rounds <= 10:
            raise ExecutionContextError("context.deliberation.max_rounds must be from 1 to 10")
        participants = deliberation.get("participants")
        if mode == "mock" and not isinstance(participants, list):
            raise ExecutionContextError("mock deliberation requires explicit participants")
        if participants is not None:
            if not isinstance(participants, list):
                raise ExecutionContextError("context.deliberation.participants must be a list")
            keys: set[str] = set()
            planners = voters = 0
            for item in participants:
                if not isinstance(item, Mapping):
                    raise ExecutionContextError("each deliberation participant must be an object")
                key = str(item.get("agent_key") or "").strip().lower()
                role = str(item.get("role") or "")
                if not key or key in keys or role not in {"planner", "participant"}:
                    raise ExecutionContextError("deliberation participants need unique agent_key and valid role")
                keys.add(key)
                planners += role == "planner"
                voters += role == "participant"
            if planners != 1 or voters < 2:
                raise ExecutionContextError("deliberation needs one planner and at least two participants")

    source_id = str(context.get("source_id") or "").strip() or None
    requested_thread_key = str(context.get("thread_key") or "").strip() or None
    source: dict[str, Any] | None = None
    if source_id:
        source = await source_db.get_source(user_id, source_id)
        if source is None or not source.get("enabled"):
            raise SourceUnavailableError("Source is not found or is disabled")
        if source.get("kind") != "github":
            raise ExecutionContextError("Main Agent execution requires a GitHub Source")
        repository = str((source.get("config") or {}).get("repository") or "")
        canonical_thread_prefix = f"github:{repository.lower()}#"
        if not requested_thread_key or not requested_thread_key.startswith(canonical_thread_prefix) or not requested_thread_key[len(canonical_thread_prefix):].isdigit():
            raise ExecutionContextError(
                "A selected GitHub Source needs its canonical thread key github:owner/repo#issue-number."
            )
    elif requested_thread_key:
        raise ExecutionContextError("context.thread_key requires an owned context.source_id")

    supplied_workspace_id = str(context.get("workspace_id") or "").strip()
    source_workspace = str((source or {}).get("config", {}).get("workspace_id") or "")
    if supplied_workspace_id and source_workspace and supplied_workspace_id != source_workspace:
        raise ExecutionContextError("workspace_id must match the selected Source")
    workspace_id = supplied_workspace_id or source_workspace or default_workspace_id or ""
    if not workspace_id:
        workspaces, _total = await get_workspaces_for_user(user_id)
        owned = [row for row in workspaces if row.get("status") != "deleted"]
        if len(owned) != 1:
            raise ExecutionContextError(
                "Select an owned workspace; automatic discovery requires exactly one workspace."
            )
        workspace_id = str(owned[0]["workspace_id"])
    workspace = await get_workspace(workspace_id)
    if workspace is None or str(workspace.get("user_id") or "") != user_id:
        raise ExecutionContextError("Workspace is not found or is not owned by this user")

    supported = {str(row.get("provider") or "") for row in list_adapters()}
    requested = context.get("agent_daemons") or {}
    if not isinstance(requested, Mapping):
        raise ExecutionContextError("context.agent_daemons must be an object")
    agent_daemons: dict[str, str] = {}
    for raw_agent, raw_id in requested.items():
        agent = str(raw_agent).strip().lower()
        daemon_id = str(raw_id).strip()
        if not agent or agent not in supported or not daemon_id:
            raise ExecutionContextError("Each agent daemon must name a supported provider and daemon ID")
        daemon = await daemon_db.get_daemon(user_id, daemon_id)
        if daemon is None or str(daemon.get("provider") or "").lower() != agent:
            raise ExecutionContextError(
                f"Daemon for {agent} is not owned by this user or has a different provider"
            )
        agent_daemons[agent] = daemon_id

    config = dict((source or {}).get("config") or {})
    source_daemon_id = str(config.get("worker_daemon_id") or "").strip()
    source_provider = str(config.get("worker_provider") or "").strip().lower()
    if source_daemon_id:
        daemon = await daemon_db.get_daemon(user_id, source_daemon_id)
        if daemon is None:
            raise ExecutionContextError("Source worker daemon is no longer owned by this user")
        actual_provider = str(daemon.get("provider") or "").lower()
        if source_provider and source_provider != actual_provider:
            raise ExecutionContextError("Source worker provider no longer matches its daemon")
        if actual_provider not in supported:
            raise ExecutionContextError("Source worker daemon provider is unsupported")
        if actual_provider in agent_daemons and agent_daemons[actual_provider] != source_daemon_id:
            raise ExecutionContextError("Source and context select different daemons for the same provider")
        agent_daemons.setdefault(actual_provider, source_daemon_id)
        agent_daemons.setdefault("default", source_daemon_id)

    return MainAgentExecutionContext(
        workspace_id=workspace_id,
        source_id=source_id,
        source_kind=str(source.get("kind")) if source else None,
        thread_key=requested_thread_key,
        repository=str(config.get("repository") or ""),
        api_base_url=str(config.get("api_base_url") or ""),
        agent_daemons=agent_daemons,
        default_provider=source_provider,
    )
