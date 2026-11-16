"""Clarification and deliberation ports plus shared LLM planning adapters."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field


class PassthroughClarificationGate:
    """Allow a request through; callers can still pause a run with questions."""

    async def check(self, request: Mapping[str, Any]) -> dict[str, Any]:
        del request
        return {"required": False, "questions": [], "adapter": "passthrough_stub"}


class GateAdapter:
    """Adapt the clarification feature's ``gate(ticket)`` callable."""

    def __init__(self, gate: Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]):
        self._gate = gate

    async def check(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return await self._gate(request)


def validate_deliberation_result(
    result: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    conclusions = result.get("conclusions", [])
    if (
        not isinstance(conclusions, list)
        or not conclusions
        or any(
            not isinstance(item, dict)
            or not str(item.get("agent_id") or "").strip()
            or not str(item.get("conclusion") or "").strip()
            for item in conclusions
        )
    ):
        raise ValueError("deliberation needs a conclusion from each assigned agent")
    if not isinstance(result.get("consensus_plan"), dict):
        raise ValueError("deliberation must include a consensus_plan object")
    expected_pairs = {
        (str(task.get("deliberation_agent_id")), str(task.get("id")))
        for task in tasks
        if task.get("deliberation_agent_id")
    }
    concluded_pairs = {
        (
            str(item["agent_id"]),
            str(item.get("task_id") or item.get("subquestion_id") or ""),
        )
        for item in conclusions
        if item.get("task_id") or item.get("subquestion_id")
    }
    missing_pairs = expected_pairs - concluded_pairs
    if missing_pairs:
        raise ValueError(
            "deliberation is missing conclusion(s) from assigned agent/subquestion(s): "
            + ", ".join(
                f"{agent}/{task_id}" for agent, task_id in sorted(missing_pairs)
            )
        )
    return {**dict(result), "conclusions": conclusions}


class DeliberationAdapter:
    """Adapt a per-ticket deliberation round without prescribing its model API."""

    def __init__(
        self,
        round_callable: Callable[
            [Mapping[str, Any], Sequence[Mapping[str, Any]]], Awaitable[dict[str, Any]]
        ],
    ):
        self._round = round_callable

    async def deliberate(
        self, request: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        result = await self._round(request, tasks)
        return validate_deliberation_result(result, tasks)


class StubDeliberationAdapter:
    """Return a clearly marked single conclusion when no deliberation model exists."""

    async def deliberate(
        self, request: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        objective = str(request.get("objective") or request.get("prompt") or "")
        return {
            "conclusions": [
                {
                    "agent_id": str(
                        task.get("deliberation_agent_id")
                        or f"planning_stub_{task.get('id', index)}"
                    ),
                    "task_id": str(task.get("id", index)),
                    "subquestion": str(task.get("description") or ""),
                    "conclusion": objective,
                    "stub": True,
                }
                for index, task in enumerate(tasks)
            ],
            "consensus_plan": {"task_ids": [str(t.get("id", "")) for t in tasks]},
            "adapter": "stub",
        }


class StubDivider:
    """Split a request into one explicit task until a real divider is injected."""

    async def divide(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        objective = str(
            request.get("objective") or request.get("prompt") or "Complete request"
        )
        return [{"id": "task-1", "description": objective, "depends_on": []}]


class StubPlanner:
    """Preserve divider output as a DAG; this is not model-generated planning."""

    async def plan(
        self,
        request: Mapping[str, Any],
        tasks: Sequence[Mapping[str, Any]],
        deliberation: Mapping[str, Any],
    ) -> dict[str, Any]:
        del request
        return {
            "tasks": [dict(task) for task in tasks],
            "consensus_plan": dict(deliberation.get("consensus_plan") or {}),
            "adapter": "stub",
        }


class DefaultResultMerger:
    async def merge(
        self, request: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        del request
        return {"results": [dict(task.get("result") or {}) for task in tasks]}


class DefaultReplySynthesizer:
    async def synthesize(
        self, request: Mapping[str, Any], merged: Mapping[str, Any]
    ) -> dict[str, Any]:
        del request
        return {"content": merged}


class _TaskDraft(BaseModel):
    id: str
    description: str
    depends_on: list[str] = Field(default_factory=list)
    deliberation_agent_id: str | None = None


class _Division(BaseModel):
    tasks: list[_TaskDraft]


class _Plan(BaseModel):
    tasks: list[_TaskDraft]
    rationale: str = ""
    consensus_plan: dict[str, Any] = Field(default_factory=dict)


class _Merge(BaseModel):
    summary: str
    findings: list[str] = Field(default_factory=list)


class _Reply(BaseModel):
    content: str


class LLMMainAgentAdapter:
    """User-aware production planner and synthesis adapter."""

    def __init__(self, llm_service: Any, *, timeout_seconds: float = 45.0):
        self._llm = llm_service
        self._timeout = timeout_seconds

    async def _complete(
        self,
        *,
        user_id: str,
        system_prompt: str,
        payload: Mapping[str, Any],
        schema: type[BaseModel],
    ) -> dict[str, Any]:
        response = await asyncio.wait_for(
            self._llm.complete(
                user_id=user_id,
                system_prompt=system_prompt,
                user_prompt=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                response_schema=schema,
                slot="primary",
            ),
            timeout=self._timeout,
        )
        return (
            response.model_dump(mode="json")
            if isinstance(response, BaseModel)
            else dict(response)
        )

    @staticmethod
    def _public_request(request: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in request.items() if key != "user_id"}

    async def divide(self, request: Mapping[str, Any]) -> list[dict[str, Any]]:
        result = await self._complete(
            user_id=str(request["user_id"]),
            system_prompt=(
                "Break the user's objective into the smallest useful independent work items. "
                "Assign each work item a deliberation_agent_id for an independent conclusion. "
                "Return stable unique task ids, clear descriptions, and dependency ids. "
                "Do not claim that work has been done."
            ),
            payload={"request": self._public_request(request)},
            schema=_Division,
        )
        return result["tasks"]

    async def plan(
        self,
        request: Mapping[str, Any],
        tasks: Sequence[Mapping[str, Any]],
        deliberation: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self._complete(
            user_id=str(request["user_id"]),
            system_prompt=(
                "Review every agent conclusion and reconcile disagreements. Produce one consensus plan "
                "as a DAG. Preserve task ids where possible; dependencies must name returned tasks. "
                "Keep deliberation_agent_id assignments for independent subquestions. "
                "Do not execute tasks or state that they are complete."
            ),
            payload={
                "request": self._public_request(request),
                "division": [dict(task) for task in tasks],
                "per_agent_conclusions": list(deliberation.get("conclusions") or []),
                "deliberation_consensus": deliberation.get("consensus_plan"),
            },
            schema=_Plan,
        )

    async def merge(
        self, request: Mapping[str, Any], tasks: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return await self._complete(
            user_id=str(request["user_id"]),
            system_prompt=(
                "Synthesize only the supplied successful task results. Keep uncertainty and gaps explicit; "
                "do not invent findings."
            ),
            payload={
                "request": self._public_request(request),
                "successful_tasks": [dict(task) for task in tasks],
            },
            schema=_Merge,
        )

    async def synthesize(
        self, request: Mapping[str, Any], merged: Mapping[str, Any]
    ) -> dict[str, Any]:
        return await self._complete(
            user_id=str(request["user_id"]),
            system_prompt="Write a direct user-facing reply from the verified synthesis. Do not add unsupported claims.",
            payload={
                "request": self._public_request(request),
                "synthesis": dict(merged),
            },
            schema=_Reply,
        )
