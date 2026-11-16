"""The three runtime-context middlewares, built together.

They tell the model what world it is running in, and each has a position in
the stack it has to take relative to the others (see ``ContextMiddleware``).
Built here as one unit, a change to that wiring cannot land in one of them and
miss the rest.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from langgraph.config import get_config

from ptc_agent.agent.middleware.runtime_context import (
    BaselineContextMiddleware,
    BaselineSources,
    BlockReader,
    TailEnvelopeMiddleware,
    TurnContext,
    TurnContextMiddleware,
)
from ptc_agent.agent.middleware.runtime_context.epoch import Workspace
from ptc_agent.agent.middleware.runtime_context.profile import (
    ProfileSwitches,
)
from ptc_agent.agent.roles import AgentRole


@dataclass(frozen=True, slots=True)
class ContextMiddleware:
    """The three middlewares, each named for the position it has to take.

    ``turn`` and ``baseline`` both write at the turn boundary, right after the
    turn's user message, and the order they appear in the stack is the order
    their rows land in: the turn row first (when the turn opened,
    the gap since the last turn, the run context), then the baseline's
    rows for whatever moved underneath it. ``baseline`` must also follow the
    caching middlewares so its block lands after their system breakpoint and
    carries its own, and ``tail`` is innermost overall so what it carries is the
    true tail of the request and the breakpoint it pins stays on the last block.
    """

    turn: TurnContextMiddleware
    baseline: BaselineContextMiddleware
    tail: TailEnvelopeMiddleware


def build_context_middleware(
    *,
    now: datetime,
    guidance: str | None,
    model_name: str | None,
    turn_context: TurnContext | None = None,
    user_profile: dict | None = None,
    session: Any | None = None,
    workspace: Workspace | None = None,
    sources: BaselineSources | None = None,
    blocks: dict[str, BlockReader] | None = None,
    files_mounted: bool | None = None,
    role: AgentRole = "generalist",
    switches: ProfileSwitches = ProfileSwitches(),
    force_memory_rebuild: bool = False,
) -> ContextMiddleware:
    """Wire the turn row, the per-thread baseline and the tail envelope.

    Everything from ``session`` down is the baseline's own material: the
    workspace and its sandbox to read, the store-backed tiers, and in
    ``blocks`` the harness-authored text the build states. ``role`` reaches
    the blocks whose wording differs by role; a build that names none is the
    analyst.
    """
    turn = turn_context or TurnContext()
    return ContextMiddleware(
        turn=TurnContextMiddleware(
            now=now,
            timezone=turn.timezone,
            last_turn_at=turn.last_turn_at,
            platform=turn.platform,
            origin=turn.origin,
            origin_agent=turn.origin_agent,
            surface_rules=turn.surface_rules,
            inherits_rules=turn.inherits_rules,
            disk_free_mb=turn.disk_free_mb,
            disk_known=turn.disk_known,
        ),
        baseline=BaselineContextMiddleware(
            session=session,
            workspace=workspace,
            sources=sources,
            blocks=blocks,
            user_profile=user_profile,
            timezone=turn.timezone,
            guidance=guidance,
            model_name=model_name,
            files_mounted=files_mounted,
            role=role,
            switches=switches,
            force_memory_rebuild=force_memory_rebuild,
        ),
        tail=TailEnvelopeMiddleware(
            now=now,
            guidance=guidance,
            model_name=model_name,
        ),
    )


@dataclass(frozen=True, slots=True)
class SubagentContext:
    """What a subagent's baseline is built from, settled once per parent turn.

    The compiler builds one baseline per definition from it, since the MCP
    roster its connectors allow and the memory switch differ by definition.
    The stack those baselines join is shared, so what differs by run, the
    task's own scratchpad, is read from the run's config at its turn boundary.
    """

    timezone: str | None = None
    #: Memory tiers only: a subagent gets no memo pointer.
    sources: BaselineSources | None = None
    skills: BlockReader | None = None
    #: The thread's scratchpad folder; each task writes a folder inside it.
    scratchpad: str | None = None

    @classmethod
    def build(
        cls,
        *,
        turn_context: TurnContext | None,
        user_profile: dict | None,
        sources: BaselineSources | None,
        skills: BlockReader | None,
        scratchpad: str | None,
    ) -> SubagentContext:
        return cls(
            timezone=(turn_context or TurnContext()).timezone,
            sources=(
                BaselineSources(
                    store=sources.store,
                    store_cache=sources.store_cache,
                    memory=sources.memory,
                    index_key=sources.index_key,
                )
                if sources is not None and sources.memory
                else None
            ),
            skills=skills,
            scratchpad=scratchpad,
        )

    def baseline(
        self,
        *,
        user_profile: dict | None,
        memory: bool,
        mcp_servers: str | None,
        files: bool,
        writes_files: bool,
        guidance: str | None,
    ) -> tuple[dict[str, bool], BaselineContextMiddleware]:
        """One definition's baseline, and the prompt sections that point at it.

        Decided together, so a section never names a block the baseline lacks.
        ``memory`` is the definition's switch, ``files`` whether it can open
        what the memory index points to, and ``writes_files`` whether it
        writes files at all.
        """
        sources = self.sources if memory else None
        blocks: dict[str, BlockReader] = {}
        if mcp_servers is not None:
            blocks["mcp_servers"] = lambda _state: mcp_servers
        if self.skills is not None:
            blocks["skills"] = self.skills
        if writes_files and self.scratchpad is not None:
            folder = self.scratchpad
            blocks["scratchpad"] = lambda _state: _task_folder(folder)
        sections = {
            "memory_system": sources is not None,
            "thread_scratchpad": "scratchpad" in blocks,
        }
        return sections, BaselineContextMiddleware(
            sources=sources,
            blocks=blocks,
            user_profile=user_profile,
            # The counts stay with the main agent; empty rather than None, so
            # the profile reads as answered.
            sandbox_enabled=files,
            timezone=self.timezone,
            guidance=guidance,
            subagent=True,
        )


def _task_folder(thread_folder: str) -> str:
    """The running task's scratchpad, inside its thread's.

    A subagent's checkpoint namespace opens with ``task:<id>``. Outside a run
    there is no task to name the folder after, which states no folder rather
    than leaving the block unanswered.
    """
    try:
        namespace = (get_config().get("configurable") or {}).get("checkpoint_ns") or ""
    except RuntimeError:  # outside a runnable context
        return ""
    head = str(namespace).split("|", 1)[0]
    if not head.startswith("task:") or len(head) <= len("task:"):
        return ""
    return posixpath.join(thread_folder, head[len("task:") :]) + "/"
