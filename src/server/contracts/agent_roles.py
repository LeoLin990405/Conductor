"""The roles one agent runs in.

Its own module, importing nothing from the package, so the subagent definitions
and the runtime-context baseline can say what differs by role without importing
the agent that builds them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

# The generalist works inside one workspace. The supervisor is the Super-agent's
# restricted Router: it routes Tickets and never implements them; the worker
# implements one Ticket.
AgentRole = Literal["generalist", "supervisor", "worker"]

# Subagents run only for the generalist. The supervisor and worker are the
# Super-agent's restricted roles; the subagent API and definition compiler read
# ALL_ROLES as the set a subagent may be available to.
ALL_ROLES: frozenset[AgentRole] = frozenset({"generalist"})

#: What the generalist was called before the rename, for one version. The name
#: is persisted (``user_subagents.roles``, ``plugin_subagents.roles``) and
#: crosses the API (``report_source.kind``), so a row or a payload still
#: written by an older server has to read as the same role. Reads only: every
#: write spells the current name, and the alias is dropped with that version.
LEGACY_ROLES: Mapping[str, str] = {"analyst": "generalist"}


def normalize_role(value: str) -> str:
    """``value`` with the one-version alias applied; anything else unchanged.

    The single place the legacy spelling is known, so the two readers of
    persisted or external values (the subagent role filter and the
    ``report_source`` discriminator) cannot drift on it. An unknown name passes
    through, leaving each reader's own validation to refuse it.
    """
    return LEGACY_ROLES.get(value, value)
