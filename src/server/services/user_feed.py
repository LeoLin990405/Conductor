"""User-feed hook: a latency hint on ``user:events:{user_id}``.

Finalize enqueues ``user_feed`` so a settled run can reach the owner's
sidebar without waiting on the feed's 30s DB re-read. The event is not
truth — the feed re-reads the database on that cadence — so a republish is
harmless and a transport failure must return rather than raise. A raise
would nack the row toward dead, and ``user_feed`` has no revive.
"""

from __future__ import annotations

import json
import logging

from src.utils.cache.redis_cache import get_cache_client

logger = logging.getLogger(__name__)


def user_events_channel(user_id: str) -> str:
    return f"user:events:{user_id}"


async def execute_user_feed(job: dict) -> None:
    """Publish the job payload. Never raises on a transport failure.

    A non-dict payload or a missing/empty ``user_id`` is not applicable.
    Redis disabled or a missing client is a no-op; a publish error is
    debug-logged and swallowed. Raising would nack this hint toward dead.
    """
    payload = job.get("payload") if isinstance(job, dict) else None
    if not isinstance(payload, dict):
        return
    user_id = payload.get("user_id")
    if not user_id:
        return
    channel = user_events_channel(str(user_id))
    try:
        cache = get_cache_client()
        if not cache.enabled or not cache.client:
            return
        await cache.client.publish(channel, json.dumps(payload))
    except Exception as exc:
        logger.debug("Failed to publish user feed on %s: %s", channel, exc)


def register_outbox_executors() -> None:
    from src.server.services.hook_outbox import register_hook_executor

    register_hook_executor("user_feed", execute_user_feed)
