"""User-scoped Router metrics projection."""

from typing import Any

from psycopg.errors import UndefinedTable

from src.config.settings import get_autonomy_router_snapshot_enabled
from src.server.database.autonomy.router import list_router_metrics


async def router_metrics_for_user(user_id: str, *, limit: int) -> dict[str, Any]:
    """Read bounded decision facts only when the opt-in is enabled."""
    if not get_autonomy_router_snapshot_enabled():
        return {"enabled": False, "available": False, "items": []}
    try:
        items = await list_router_metrics(user_id, limit=limit)
    except UndefinedTable:
        return {"enabled": True, "available": False, "items": []}
    return {"enabled": True, "available": True, "items": items}
