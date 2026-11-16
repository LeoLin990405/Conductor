"""Thread detail read used by the Worker dispatcher."""

from fastapi import HTTPException

from src.server.database.conversation import get_thread_by_id
from src.server.models.conversation import WorkspaceThreadListItem
from src.server.utils import api as auth_api
from src.server.utils.api import CurrentUserId

from ._deps import router


@router.get("/{thread_id}")
async def get_thread(thread_id: str, x_user_id: CurrentUserId):
    """Get thread metadata. Used by the frontend to resolve workspaceId."""
    await auth_api.require_thread_owner(thread_id, x_user_id)
    thread = await get_thread_by_id(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    return _thread_list_item(thread)


def _thread_list_item(row: dict) -> WorkspaceThreadListItem:
    """Build the list-item response from a detail/updated thread row."""
    return WorkspaceThreadListItem(
        thread_id=str(row["conversation_thread_id"]),
        workspace_id=str(row["workspace_id"]),
        thread_index=row["thread_index"],
        current_status=row["current_status"],
        msg_type=row.get("msg_type"),
        title=row.get("title"),
        platform=row.get("platform"),
        metadata=row.get("metadata") or {},
        is_shared=bool(row.get("is_shared", False)),
        is_pinned=bool(row.get("is_pinned", False)),
        archived_at=row.get("archived_at"),
        llm_model=row.get("llm_model"),
        subagents_allowed=row["subagents_allowed"],
        swarm_enabled=bool(row.get("swarm_enabled", False)),
        reasoning_effort=row.get("reasoning_effort"),
        fast_mode=row.get("fast_mode"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )




