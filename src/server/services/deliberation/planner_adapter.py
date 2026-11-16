"""Deterministic planner. Unions plans and prefers the canonical DAG."""

from __future__ import annotations

import copy

from src.server.services.deliberation.canonical import copy_plan
from src.server.services.deliberation.errors import DeliberationError

_CANONICAL_KEYS = {"spec", "impl", "review"}


class MergingPlannerAdapter:
    async def synthesize(self, ctx: dict, proposals: list[dict]) -> dict:
        parsed = [proposal for proposal in proposals if isinstance(proposal, dict)]
        if any(_step_keys(proposal) == _CANONICAL_KEYS for proposal in parsed):
            return copy_plan()

        steps: list[dict] = []
        seen: set[str] = set()
        for proposal in parsed:
            for step in proposal.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                step_key = step.get("step_key")
                if not isinstance(step_key, str) or not step_key or step_key in seen:
                    continue
                seen.add(step_key)
                steps.append(copy.deepcopy(step))
        if not steps:
            steps.append(
                {
                    "step_key": "work",
                    "title": "Work",
                    "prompt": ctx["objective"],
                    "assigned_agent": "codex",
                    "depends_on": [],
                }
            )
        return {"summary": "planner synthesis", "steps": steps}

    async def propose(self, ctx: dict) -> dict:
        del ctx
        raise DeliberationError(
            "bad_role",
            "planner does not take a participant turn",
        )

    async def critique(self, ctx: dict) -> dict:
        del ctx
        raise DeliberationError(
            "bad_role",
            "planner does not take a participant turn",
        )

    async def vote(self, ctx: dict, synthesis: dict) -> dict:
        del ctx, synthesis
        raise DeliberationError(
            "bad_role",
            "planner does not take a participant turn",
        )


def _step_keys(proposal: dict) -> set[str]:
    keys: set[str] = set()
    for step in proposal.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_key = step.get("step_key")
        if isinstance(step_key, str):
            keys.add(step_key)
    return keys
