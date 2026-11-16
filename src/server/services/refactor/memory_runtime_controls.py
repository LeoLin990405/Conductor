"""Per-user opt-out for automatic memory work.

Management and export paths deliberately do not consult this gate. The feature
catalog keeps automatic memory on by default; users can opt out in Settings.
"""

from __future__ import annotations

from src.server.services import features as feature_service

MEMORY_FEATURE_KEY = "memory"


async def automatic_memory_enabled(user_id: str) -> bool:
    """Whether automatic memory recall, capture, and proposal work may run."""
    if not isinstance(user_id, str) or not user_id.strip():
        return False
    try:
        return await feature_service.user_feature_enabled(user_id, MEMORY_FEATURE_KEY)
    except Exception:  # noqa: BLE001 - unknown preference state must not enable recall
        return False
