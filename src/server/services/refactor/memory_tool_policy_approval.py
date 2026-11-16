"""Bind durable tool policy only after an explicit owner revision action."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from src.server.contracts.memory_tool_policy import (
    normalize_memory_tool_policy_metadata,
    normalize_validity_window,
)
from src.server.database.refactor import memory as db_memory
from src.server.database.refactor import memory_tool_policy as db_policy


async def _active_user_entry(
    *, user_id: str, entry_key: str, read_entry: Callable[..., Awaitable[Any]] | None
) -> Mapping[str, Any]:
    reader = read_entry or db_memory.get_active_entry_for_approval
    row = await reader(user_id=user_id, tier="user", entry_key=entry_key)
    if not isinstance(row, Mapping):
        raise ValueError("approved active owner memory revision is unavailable")
    entry_id = row.get("entry_id")
    content_hash = row.get("content_hash")
    if not isinstance(entry_id, str) or not isinstance(content_hash, str):
        raise ValueError("approved memory revision lacks its durable identity")
    return row


async def validate_approved_memory_policy(
    *,
    user_id: str,
    proposal: Mapping[str, Any],
    replaced_entry_ids: tuple[str, ...] = (),
) -> None:
    """Reject an approval whose policy write cannot succeed, before activation.

    The policy write runs after the Store/projection activation; validating its
    inputs here keeps a later policy error from leaving activated memory behind
    (the approval caller also rolls back on a policy-write failure).
    """
    raw_metadata = proposal.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    tool_policy = normalize_memory_tool_policy_metadata(metadata)
    kind = tool_policy.get("kind")
    denials = tool_policy.get("denied_tools", [])
    if kind is None:
        if denials:
            raise ValueError("explicit constraint kind is required to add tool policy")
        if not replaced_entry_ids:
            return
        if await db_policy.has_active_denials(
            user_id=user_id, entry_ids=replaced_entry_ids
        ):
            raise ValueError(
                "explicit memory kind is required to replace approved tool policy"
            )
        return
    normalize_validity_window(
        tool_policy.get("valid_from"), tool_policy.get("valid_until")
    )


async def sync_approved_memory_policy(
    *,
    user_id: str,
    proposal: Mapping[str, Any],
    approved_entry_key: str,
    replaced_entry_ids: tuple[str, ...] = (),
    read_entry: Callable[..., Awaitable[Any]] | None = None,
    persist: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Apply reviewed typed IDs and retire replaced rules after approval.

    The caller must invoke this only inside the authenticated manual approval
    path, after the approved Store revision has been projected. Prose is never
    parsed as a policy. Non-constraint replacements submit an empty set solely
    to retire their predecessor's authority.
    """
    raw_metadata = proposal.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    tool_policy = normalize_memory_tool_policy_metadata(metadata)
    kind = tool_policy.get("kind")
    denials = tool_policy.get("denied_tools", [])
    if kind is None:
        if denials:
            raise ValueError("explicit constraint kind is required to add tool policy")
        if not replaced_entry_ids:
            return
        # Preserve legacy correction/replacement flows when their source has
        # no frozen authority. If it does, require a typed replacement so the
        # owner sees the exact new policy instead of silently lifting a deny.
        if await db_policy.has_active_denials(
            user_id=user_id, entry_ids=replaced_entry_ids
        ):
            raise ValueError(
                "explicit memory kind is required to replace approved tool policy"
            )
        return
    valid_from, valid_until = normalize_validity_window(
        tool_policy.get("valid_from"), tool_policy.get("valid_until")
    )
    row = await _active_user_entry(
        user_id=user_id, entry_key=approved_entry_key, read_entry=read_entry
    )
    writer = persist or db_policy.replace_denials
    await writer(
        user_id=user_id,
        entry_id=str(row["entry_id"]),
        denied_tools=denials if kind == "constraint" else [],
        approval_source="manual",
        approved_content_hash=str(row["content_hash"]),
        valid_from=valid_from,
        valid_until=valid_until,
        replaced_entry_ids=replaced_entry_ids,
    )


async def sync_reverted_memory_policy(
    *,
    user_id: str,
    restored_entry_key: str,
    restored_from_entry_id: str,
    replaced_entry_id: str,
    read_entry: Callable[..., Awaitable[Any]] | None = None,
    copy_policy: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Treat an explicit owner revert as fresh approval of frozen authority."""
    row = await _active_user_entry(
        user_id=user_id, entry_key=restored_entry_key, read_entry=read_entry
    )
    copier = copy_policy or db_policy.copy_reverted_denials
    return bool(
        await copier(
            user_id=user_id,
            source_entry_id=restored_from_entry_id,
            restored_entry_id=str(row["entry_id"]),
            restored_content_hash=str(row["content_hash"]),
            replaced_entry_ids=(replaced_entry_id,),
        )
    )
