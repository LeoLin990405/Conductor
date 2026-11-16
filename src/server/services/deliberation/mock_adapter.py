"""Scripted participant adapter. Never spawns a CLI."""

from __future__ import annotations

from src.server.services.deliberation.canonical import copy_plan
from src.server.services.deliberation.errors import DeliberationError


class MockParticipantAdapter:
    def __init__(
        self,
        *,
        agent_key: str,
        role: str,
        script: str = "consensus",
        holdout_key: str = "grok",
    ):
        self.agent_key = agent_key
        self.role = role
        self.script = script
        self.holdout_key = holdout_key

    async def propose(self, ctx: dict) -> dict:
        del ctx
        if self.role == "planner":
            raise DeliberationError("bad_role", "planner is not proposed to")
        plan = copy_plan()
        return {
            "body": f"{self.agent_key} proposes the canonical plan",
            "plan": plan,
            "critique": "",
        }

    async def critique(self, ctx: dict) -> dict:
        del ctx
        plan = copy_plan()
        return {
            "body": f"{self.agent_key} critiques: keep the canonical plan",
            "plan": plan,
            "critique": "no change",
        }

    async def vote(self, ctx: dict, synthesis: dict) -> dict:
        del ctx, synthesis
        if self.role == "planner":
            raise DeliberationError("planner_votes", "planner does not vote")
        if self.script == "holdout" and self.agent_key == self.holdout_key:
            return {"ballot": "dissent", "comment": "holdout"}
        return {"ballot": "agree", "comment": "matches"}

    async def synthesize(self, ctx: dict, proposals: list[dict]) -> dict:
        del ctx, proposals
        if self.role != "planner":
            raise DeliberationError("bad_role", "only the planner synthesizes")
        return copy_plan()
