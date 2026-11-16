"""Load owner-approved tool denials into a pure per-turn execution snapshot."""

from __future__ import annotations

from typing import Any

from src.server.contracts.memory_tool_policy import (
    MemoryToolPolicySnapshot,
    snapshot_from_authority_rows,
)
from src.server.database.refactor import memory_tool_policy as db_policy

__all__ = ["MemoryToolPolicySnapshot", "load_memory_tool_policy"]


async def load_memory_tool_policy(
    *,
    user_id: str,
    read_denials: Any = None,
) -> MemoryToolPolicySnapshot:
    """Read all active authority rows, without consulting recall or token budgets.

    Authority failures return a deny-all snapshot. The caller must attach that
    snapshot before graph construction and must not start a native runtime.
    """
    if not isinstance(user_id, str) or not user_id.strip():
        return MemoryToolPolicySnapshot(deny_all=True, digest="missing-owner")
    reader = read_denials or db_policy.list_active_denials
    try:
        rows = await reader(user_id=user_id)
    except Exception:
        return MemoryToolPolicySnapshot(deny_all=True, digest="load-error")
    return snapshot_from_authority_rows(rows)
