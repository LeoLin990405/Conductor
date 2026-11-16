"""PTC Graph Factory — builds per-conversation agents with dependency-injected session management."""

import asyncio
import logging
from typing import Any, Protocol, runtime_checkable

from ptc_agent.agent.agent import AgentRole, PTCAgent
from ptc_agent.agent.middleware.runtime_context import TurnContext
from ptc_agent.agent.middleware.runtime_context.epoch import Workspace
from ptc_agent.agent.middleware.compaction.window import WindowCoverage
from ptc_agent.agent.middleware.subagent_switch import SubagentSwitchReader
from ptc_agent.config import AgentConfig
from ptc_agent.core.project_context import ProjectContext
from ptc_agent.core.session import Session
from ptc_agent.memory_tool_policy import MemoryToolPolicySnapshot

logger = logging.getLogger(__name__)


@runtime_checkable
class SessionProvider(Protocol):
    """Dependency-injection boundary for session management (server, CLI, tests)."""

    async def get_or_create_session(
        self, conversation_id: str, sandbox_id: str | None = None
    ) -> Session:
        ...


async def _read_harness_blocks(
    user_id: str | None,
    home_id: str | None,
    thread_id: str | None,
    turn_context: TurnContext | None,
    role: AgentRole,
) -> dict[str, str | None]:
    """Legacy activity block retired with the Chief-of-Staff surface."""
    return {}


async def _read_workspace_naming(
    workspace_id: str, role: AgentRole = "generalist"
) -> Workspace | None:
    """The workspace's name, description and computer for the `<workspace>` block.

    Read once here rather than inside the model call, so the values are bound
    when the turn's agent is built and the model never sees the name change
    under it mid-answer. The baseline freezes them per epoch; a rename
    reaches the model as a `workspace_changed` row on the next turn. A read
    that fails answers None, not an empty name: the baseline must not file a
    row saying the workspace lost its name. The Chief of Staff's Home is named
    for its folder, not for the row it is stored in.
    """
    if not workspace_id:
        return None
    try:
        from src.server.database.workspace import get_workspace_naming

        row = await get_workspace_naming(workspace_id) or {}
    except Exception as e:
        logger.warning(f"Failed to read the name of workspace {workspace_id}: {e}")
        return None
    name = row.get("computer_name")
    computers = row.get("computers") or 0
    computer = (name.strip() or "Untitled") if name is not None and computers >= 2 else ""
    return Workspace(
        name=(row.get("name") or "").strip(),
        description=(row.get("description") or "").strip(),
        computer=computer,
    )


async def build_ptc_graph(
    conversation_id: str,
    config: AgentConfig,
    session_provider: SessionProvider,
    sandbox_id: str | None = None,
    operation_callback: Any | None = None,
    checkpointer: Any | None = None,
    background_registry: Any | None = None,
    store: Any | None = None,
    on_signed_url: Any | None = None,
    user_id: str | None = None,
    memory_context_block: str | None = None,
    permission_mode: str = "auto",
    memory_proposals_enabled: bool = False,
    memory_enabled: bool = True,
    memory_tool_policy: MemoryToolPolicySnapshot | None = None,
) -> Any:
    """Build a BackgroundSubagentOrchestrator for ``conversation_id``, acquiring a session via ``session_provider``."""
    logger.debug(f"Building PTC graph for conversation: {conversation_id}")

    # Get session from provider
    session = await session_provider.get_or_create_session(
        conversation_id=conversation_id,
        sandbox_id=sandbox_id,
    )

    if not session.sandbox or not session.mcp_registry:
        raise RuntimeError(
            f"Failed to initialize session for conversation {conversation_id}"
        )

    ptc_agent, workspace = await asyncio.gather(
        asyncio.to_thread(PTCAgent, config),
        _read_workspace_naming(conversation_id),
    )

    inner_agent = ptc_agent.create_agent(
        sandbox=session.sandbox,
        mcp_registry=session.mcp_registry,
        operation_callback=operation_callback,
        checkpointer=checkpointer,
        background_registry=background_registry,
        # session gives workspace-tier memory a real namespace.
        session=session,
        workspace=workspace,
        store=store,
        on_signed_url=on_signed_url,
        user_id=user_id,
        tool_summary=getattr(session, "mcp_tool_summary", None),
        memory_context_block=memory_context_block,
        permission_mode=permission_mode,
        memory_proposals_enabled=memory_proposals_enabled,
        memory_enabled=memory_enabled,
        memory_tool_policy=memory_tool_policy,
    )

    logger.debug(
        f"Created PTC agent for {conversation_id} "
        f"(checkpointer={'enabled' if checkpointer else 'disabled'})"
    )

    return inner_agent


async def build_ptc_graph_with_session(
    session: Session,
    config: AgentConfig,
    operation_callback: Any | None = None,
    checkpointer: Any | None = None,
    background_registry: Any | None = None,
    user_id: str | None = None,
    user_profile: dict[str, Any] | None = None,
    thread_id: str | None = None,
    store: Any | None = None,
    on_signed_url: Any | None = None,
    namespace_owner: Any | None = None,
    disable_subagents: bool = False,
    direct_mcp: Any | None = None,
    turn_context: TurnContext | None = None,
    project: ProjectContext | None = None,
    tool_view: Any | None = None,
    role: AgentRole = "generalist",
    subagent_switch: SubagentSwitchReader | None = None,
    window_coverage: WindowCoverage | None = None,
    memory_context_block: str | None = None,
    permission_mode: str = "auto",
    memory_proposals_enabled: bool = False,
    memory_enabled: bool = True,
    memory_tool_policy: MemoryToolPolicySnapshot | None = None,
    extra_tools: list[Any] | None = None,
) -> Any:
    """Build a BackgroundSubagentOrchestrator from a pre-acquired session (WorkspaceManager path).

    ``turn_context`` is what this turn knows about itself, for the turn anchor
    row. It is optional because this builder also serves context-free callers
    (thread maintenance) that have no turn. ``user_profile`` is the caller's
    read of the profile, the one its ``turn_context`` zone came from, so the
    identity block and the stamp never answer from two different reads.

    ``project`` is the workspace folder the turn runs in. The build happens
    before the run's task binds it, so it travels as an argument.

    ``tool_view`` is the project's frozen registry and summary. The session's
    own fields belong to whichever project on the machine resolved last.

    ``subagent_switch`` reads the thread's subagent switch on every call; a
    build without one leaves subagents as built. ``window_coverage`` lets the
    main agent trim its checkpoint to its window (see ``compaction.window``);
    a build without one never trims.
    """
    mcp_registry = (
        tool_view.mcp_registry if tool_view is not None else session.mcp_registry
    )
    tool_summary = (
        tool_view.mcp_tool_summary
        if tool_view is not None
        else getattr(session, "mcp_tool_summary", None)
    )
    # From the project, never from the session: the session is cached per
    # computer and several workspaces share it, so its own label names
    # whichever workspace happened to acquire it first.
    workspace_id = project.workspace_id if project else ""
    logger.debug(f"Building PTC graph with session for workspace: {workspace_id}")

    if not session.sandbox or not mcp_registry:
        raise RuntimeError(
            f"Session for workspace {workspace_id} is not properly initialized"
        )

    (
        ptc_agent,
        workspace,
        harness_blocks,
    ) = await asyncio.gather(
        asyncio.to_thread(PTCAgent, config),
        _read_workspace_naming(workspace_id, role),
        _read_harness_blocks(user_id, workspace_id, thread_id, turn_context, role),
    )

    if workspace_id and user_id:
        from src.server.database.user_vault_secrets import get_user_secrets_decrypted

        # Leak detection redacts the owner's whole vault, read fresh: every
        # workspace can read every secret, and the sandbox's cached copy is
        # process-local, so a rotation handled by another worker leaves it stale.
        vault_secrets = await get_user_secrets_decrypted(user_id)
    else:
        vault_secrets = dict(getattr(session.sandbox, "vault_secrets", None) or {})

    inner_agent = ptc_agent.create_agent(
        sandbox=session.sandbox,
        mcp_registry=mcp_registry,
        disable_subagents=disable_subagents,
        operation_callback=operation_callback,
        checkpointer=checkpointer,
        background_registry=background_registry,
        namespace_owner=namespace_owner,
        user_profile=user_profile,
        session=session,
        thread_id=thread_id,
        workspace=workspace,
        on_agent_md_write=session.note_agent_md_write,
        store=store,
        on_signed_url=on_signed_url,
        vault_secrets=vault_secrets,
        user_id=user_id,
        # Session-cached tool summary (precomputed once per session) so the per
        # turn create_agent never recomputes it — keeps the prompt-cache prefix
        # byte-stable. None → create_agent computes from the registry.
        tool_summary=tool_summary,
        direct_mcp=direct_mcp,
        turn_context=turn_context,
        project=project,
        role=role,
        harness_blocks=harness_blocks,
        memory_context_block=memory_context_block,
        permission_mode=permission_mode,
        memory_proposals_enabled=memory_proposals_enabled,
        memory_enabled=memory_enabled,
        memory_tool_policy=memory_tool_policy,
        subagent_switch=subagent_switch,
        window_coverage=window_coverage,
        extra_tools=extra_tools,
    )

    logger.debug(
        f"Created PTC agent for workspace {workspace_id} "
        f"(checkpointer={'enabled' if checkpointer else 'disabled'})"
    )

    return inner_agent
