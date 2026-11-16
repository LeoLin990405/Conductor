"""
Onboarding and personalization completion service.

Onboarding completion is explicit (the setup flow writes the flag). This
module only auto-completes personalization once the user has an API key or
OAuth connection.
"""

import logging

from src.server.database import user as user_db

logger = logging.getLogger(__name__)


async def maybe_complete_personalization(user_id: str) -> bool:
    """
    If personalization requirements are met, set personalization_completed=True.

    Requirements: at least one API key configured (BYOK) or one OAuth connection.

    Returns:
        True if personalization was just completed, False otherwise.
    """
    try:
        user = await user_db.get_user(user_id)
        if not user:
            return False
        if user.get("personalization_completed"):
            return False

        # get_user() already returns has_api_key and has_oauth_token as
        # computed columns — use them instead of issuing separate queries.
        if user.get("has_api_key") or user.get("has_oauth_token"):
            await user_db.update_user(
                user_id=user_id, personalization_completed=True
            )
            logger.info(
                f"[onboarding] Auto-completed personalization for user {user_id}"
            )
            return True
    except Exception as e:
        logger.warning(
            f"[onboarding] Failed to check/complete personalization: {e}"
        )
    return False
