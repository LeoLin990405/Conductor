"""Source polling effect: fetch one page, intake it, advance the cursor."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from src.server.database.autonomy import sources as source_db
from src.server.database.autonomy.effects import enqueue_effect
from src.server.services.autonomy import intake
from src.server.services.autonomy.github import poll_github_comments
from src.server.services.autonomy.feishu import poll_feishu_messages
from src.server.services.autonomy.models import EFFECT_POLL_SOURCE
from src.server.services.autonomy.security import redact_external_text
from src.server.services.features import user_feature_enabled

logger = logging.getLogger(__name__)

_POLL_KINDS = frozenset({"github", "feishu"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def poll_source_effect(effect) -> None:
    """Run one source poll from a claimed effect row."""
    source_id = str(effect.payload.get("source_id") or "")
    if not source_id:
        raise ValueError("poll effect has no source_id")
    source = await source_db.get_source_any_user(source_id)
    if source is None or not source.get("enabled"):
        return
    kind = str(source.get("kind") or "")
    if kind not in _POLL_KINDS:
        return
    if kind == "github":
        # Repo events (including issue_opened) use the durable wake path before
        # comment intake when RepoBar is on. Failures stay isolated so a list
        # poll outage cannot starve the existing comment cursor.
        await _poll_github_repo_events(source)
    config = dict(source.get("config") or {})
    state = dict(source.get("state") or {})
    poller = poll_github_comments if kind == "github" else poll_feishu_messages
    try:
        drafts, next_state = await poller(
            source_id=source_id,
            user_id=str(source["user_id"]),
            config=config,
            state=state,
        )
    except Exception as exc:
        # Keep the old cursor; the next successful poll re-reads the overlap.
        await source_db.update_source(
            str(source["user_id"]),
            source_id,
            state={
                "last_error": redact_external_text(str(exc))[:1000],
                "last_poll_at": _now().isoformat(),
            },
        )
        raise
    accepted = 0
    for draft in drafts:
        if await intake.receive_trigger(source=source, draft=draft):
            accepted += 1
    next_state["next_poll_at"] = (
        _now() + timedelta(seconds=int(config.get("poll_seconds") or 60))
    ).isoformat()
    await source_db.update_source(
        str(source["user_id"]), source_id, state=next_state
    )
    logger.info(
        "[autonomy] poll source=%s drafts=%s accepted=%s",
        source_id,
        len(drafts),
        accepted,
    )


async def _poll_github_repo_events(source: dict) -> None:
    """Emit RepoBar wakes for one GitHub source, including ``issue_opened``.

    Off when ``autonomy_waker_repobar_enabled`` is off, so a default install
    keeps only comment intake. On uses the same durable list-poll path as the
    waker batch. A list-poll failure is logged and swallowed: the wake cursor
    does not advance inside ``poll_repobar_source``, and comment intake still
    runs on this effect.
    """
    from src.server.services.autonomy.wakers.repobar import poll_repobar_source
    from src.server.services.autonomy.wakers.settings import (
        get_autonomy_waker_repobar_enabled,
    )

    if not get_autonomy_waker_repobar_enabled():
        return
    try:
        events = await poll_repobar_source(source)
    except Exception as exc:  # noqa: BLE001 - must not block comment intake
        logger.warning(
            "[autonomy] poll source=%s repo events failed: %s",
            source.get("source_id"),
            redact_external_text(str(exc))[:500],
        )
        return
    opened = sum(1 for event in events if event.kind == "issue_opened")
    logger.info(
        "[autonomy] poll source=%s repo_events=%s issue_opened=%s",
        source.get("source_id"),
        len(events),
        opened,
    )


async def enqueue_due_polls() -> int:
    """Queue one poll effect for every Source whose next poll is due.

    The idempotency key is derived from the due timestamp, so the pass can run
    every two seconds without stacking polls; a failed effect retries in place
    (the source state does not advance until a poll succeeds).
    """
    queued = 0
    for source in await source_db.list_enabled_sources():
        if source.get("kind") not in _POLL_KINDS:
            continue
        # One source's bad row or failed lookup must not stop the sources
        # behind it, nor the scheduler steps after this one.
        try:
            if await _enqueue_due_poll(source):
                queued += 1
        except Exception as exc:  # noqa: BLE001 - isolate one source from the pass
            logger.error(
                "[autonomy] poll enqueue source=%s failed: %s",
                str(source.get("source_id") or "")[:80],
                redact_external_text(str(exc))[:500],
            )
    return queued


async def _enqueue_due_poll(source: dict) -> bool:
    # The flag is the kill switch for new intake. Existing Tickets still
    # finish because their effects are independent of polling.
    if not await user_feature_enabled(str(source["user_id"]), "super_agent"):
        return False
    state = dict(source.get("state") or {})
    next_poll = str(state.get("next_poll_at") or "")
    if next_poll:
        try:
            due = datetime.fromisoformat(next_poll)
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
        except ValueError:
            due = _now()
        if due > _now():
            return False
    key = f"autonomy:poll:{source['source_id']}:{next_poll or 'initial'}"
    return bool(
        await enqueue_effect(
            user_id=str(source["user_id"]),
            kind=EFFECT_POLL_SOURCE,
            idempotency_key=key,
            payload={"source_id": str(source["source_id"])},
        )
    )


async def poll_source_now(user_id: str, source_id: str) -> bool:
    """Queue an immediate poll for one Source, outside its cron.

    The manual effect gets a unique idempotency key so repeated clicks each
    poll once; the source's own cursor still dedups already-intaken comments.
    """
    source = await source_db.get_source(user_id, source_id)
    if source is None or source.get("kind") not in _POLL_KINDS or not source.get("enabled"):
        return False
    return await enqueue_effect(
        user_id=user_id,
        kind=EFFECT_POLL_SOURCE,
        idempotency_key=f"autonomy:poll:{source_id}:manual:{uuid4()}",
        payload={"source_id": source_id},
    )
