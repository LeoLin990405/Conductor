"""The per-user hidden Watchdog Session lifecycle."""

from __future__ import annotations

import logging
from typing import Any

from src.server.database.autonomy import watchdog_sessions as session_db
from src.server.database.home_workspace import get_home_workspace_id
from src.server.services.projects.dispatch import ProjectDispatcher

logger = logging.getLogger(__name__)

WATCHDOG_PLATFORM = "superagent_watchdog"


async def ensure_watchdog_session(
    user_id: str, *, dispatcher: ProjectDispatcher | None = None
) -> dict[str, Any]:
    """Return the user's Watchdog thread, creating it once."""
    row = await session_db.get_watchdog_session(user_id)
    if row is not None:
        return row
    if dispatcher is None:
        dispatcher = ProjectDispatcher()
    workspace_id = get_home_workspace_id(user_id)
    thread_id = await dispatcher.create_thread(
        user_id=user_id,
        workspace_id=workspace_id,
        title="Super-agent Watchdog",
        first_query="Super-agent Watchdog",
        platform=WATCHDOG_PLATFORM,
    )
    saved = await session_db.save_watchdog_session(
        user_id, workspace_id=workspace_id, thread_id=thread_id
    )
    logger.info("[autonomy] created watchdog session user=%s thread=%s", user_id, thread_id)
    return saved
