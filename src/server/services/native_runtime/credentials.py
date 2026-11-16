"""Native runtime credential bridge.

The native CLI owns its model call, so Conductor only hands it a credential when
the CLI has no account of its own. Values are injected through the child
environment, never argv or logs.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def minimax_api_key(user_id: str) -> str | None:
    """The user's MiniMax coding-plan key, if configured in Conductor."""
    try:
        from src.server.database.api_keys import get_key_for_provider

        for provider in ("minimax-coding", "minimax"):
            key = await get_key_for_provider(user_id, provider)
            if key:
                return key
    except Exception:
        logger.warning("[native-runtime] MiniMax credential read failed", exc_info=True)
    return None


async def mimo_api_key(user_id: str) -> tuple[str | None, str | None]:
    """The user's MiMo token-plan credential and base URL, if configured."""
    try:
        from src.server.database.api_keys import (
            get_base_url_for_provider,
            get_key_for_provider,
        )

        for provider in ("mimo-oauth", "mimo"):
            key = await get_key_for_provider(user_id, provider)
            if key:
                base = await get_base_url_for_provider(user_id, provider)
                return key, base
    except Exception:
        logger.warning("[native-runtime] MiMo credential read failed", exc_info=True)
    return None, None
