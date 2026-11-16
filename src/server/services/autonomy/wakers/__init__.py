"""CodexBar and RepoBar wakers.

Each source is switched on its own. A failure in one is recorded and does not
stop the other. Wake effects are enqueued under the event's idempotency key,
so a repeated delivery does not park or open a Ticket twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from src.server.services.autonomy.effects import PermanentEffectError
from src.server.services.autonomy.models import EffectRecord
from src.server.services.autonomy.wakers.base import (
    DispatchReport,
    WakeEvent,
    Waker,
    WakerFailure,
    dispatch_wakers,
)
from src.server.services.autonomy.wakers.codexbar import (
    CodexBarWaker,
    quota_transitions,
)
from src.server.services.autonomy.wakers.repobar import (
    RepoBarWaker,
    RepoBinding,
    ensure_fix_ci_ticket,
    fix_ci_draft,
    fix_ci_enabled,
    fix_ci_origin_lock,
    matching_binding,
    poll_configured_repositories,
    repo_action,
    workspace_owner as default_workspace_owner,
)
from src.server.services.autonomy.wakers.scheduling import (
    DEFAULT_MAX_QUOTA_RETRIES,
    DbQuotaStore,
    QuotaAction,
    plan_quota,
    quota_continuation_key,
)

logger = logging.getLogger(__name__)

EnqueueWake = Callable[..., Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class WakerPassResult:
    """Scheduler-pass outcome. Failures are isolated per waker."""

    enqueued: tuple[str, ...] = ()
    failures: tuple[WakerFailure, ...] = ()


def default_wakers(*, codexbar_enabled: bool, repobar_enabled: bool) -> list[Waker]:
    return [
        CodexBarWaker(enabled=codexbar_enabled),
        RepoBarWaker(enabled=repobar_enabled),
    ]


async def enqueue_wake_events(
    events: list[WakeEvent],
    *,
    user_id: str,
    enqueue: EnqueueWake,
) -> list[str]:
    """Enqueue each wake once. A repeated key in this batch or in the sink is dropped."""
    accepted: list[str] = []
    seen: set[str] = set()
    for event in events:
        key = event.idempotency_key.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        payload = event.as_payload()
        inserted = await enqueue(
            user_id=user_id or event.user_id,
            idempotency_key=key,
            payload=payload,
        )
        if inserted:
            accepted.append(key)
    return accepted


async def apply_waker_effect(
    effect: EffectRecord,
    *,
    store: Any | None = None,
    codexbar_enabled: bool = True,
    repobar_enabled: bool = True,
    fix_ci_repos: set[str] | frozenset[str] | None = None,
    bindings: list[RepoBinding] | None = None,
    workspace_owner: Callable[[str, str, str], Awaitable[bool]] | None = None,
    max_retries: int = DEFAULT_MAX_QUOTA_RETRIES,
    find_ticket: Callable[[str], Awaitable[str | None]] | None = None,
    create_ticket: Callable[[dict[str, Any]], Awaitable[str]] | None = None,
) -> list[QuotaAction]:
    """Run one wake effect. The caller already deduped it by idempotency key."""
    event = WakeEvent.from_payload(effect.payload)
    if effect.idempotency_key:
        event = replace(event, idempotency_key=str(effect.idempotency_key))
    if not event.user_id:
        event = replace(event, user_id=str(effect.user_id or ""))
    if effect.effect_id:
        event = replace(event, effect_id=str(effect.effect_id))
    if event.source == "codexbar":
        if not codexbar_enabled:
            return []
        return await _apply_quota(
            event, store=store or DbQuotaStore(), max_retries=max_retries
        )
    if event.source == "repobar":
        if not repobar_enabled:
            return []
        await _apply_repo(
            event,
            fix_ci_repos=fix_ci_repos,
            bindings=bindings,
            workspace_owner=workspace_owner,
            find_ticket=find_ticket,
            create_ticket=create_ticket,
        )
        return []
    raise PermanentEffectError(f"unknown waker source {event.source!r}")


async def run_waker_pass(
    *,
    load_states: Callable[[], Awaitable[dict[str, str]]] | None = None,
    load_previous: Callable[[], Awaitable[dict[str, str]]] | None = None,
    load_exhausted: Callable[[], Awaitable[set[str]]] | None = None,
    poll_repobar: Callable[[], Awaitable[list[WakeEvent]]] | None = None,
    enqueue: EnqueueWake | None = None,
    codexbar_enabled: bool | None = None,
    repobar_enabled: bool | None = None,
) -> WakerPassResult:
    """Poll switched-on wakers. One source failing does not skip the other.

    RepoBar polls configured repositories unless the caller injects
    ``poll_repobar``. CodexBar reads the usage snapshot only while its switch
    is on. A failure is logged as redacted text, without the traceback.
    """
    from src.server.services.autonomy.wakers.settings import (
        get_autonomy_waker_codexbar_enabled,
        get_autonomy_waker_repobar_enabled,
    )

    codexbar_on = (
        get_autonomy_waker_codexbar_enabled()
        if codexbar_enabled is None
        else codexbar_enabled
    )
    repobar_on = (
        get_autonomy_waker_repobar_enabled()
        if repobar_enabled is None
        else repobar_enabled
    )
    if not codexbar_on and not repobar_on:
        return WakerPassResult()
    sink = enqueue or _enqueue_effect
    enqueued: list[str] = []
    failures: list[WakerFailure] = []
    if codexbar_on:
        try:
            enqueued.extend(
                await _poll_codexbar(
                    load_states=load_states,
                    load_previous=load_previous,
                    load_exhausted=load_exhausted,
                    enqueue=sink,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the other waker still runs
            failures.append(WakerFailure("codexbar", _safe_error(exc)))
            logger.error(
                "[autonomy] %s waker failed: %s", "codexbar", _safe_error(exc)
            )
    if repobar_on:
        try:
            if poll_repobar is not None:
                found = await poll_repobar()
                enqueued.extend(
                    await enqueue_wake_events(found, user_id="", enqueue=sink)
                )
            else:
                # The poller commits each wake before it advances that
                # source's cursor. Enqueueing the returned events again
                # would run after the cursor and reopen the loss window.
                found = await poll_configured_repositories()
                seen: set[str] = set()
                for event in found:
                    key = event.idempotency_key.strip()
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    enqueued.append(key)
        except Exception as exc:  # noqa: BLE001 - isolation from the CodexBar pass
            failures.append(WakerFailure("repobar", _safe_error(exc)))
            logger.error(
                "[autonomy] %s waker failed: %s", "repobar", _safe_error(exc)
            )
    return WakerPassResult(enqueued=tuple(enqueued), failures=tuple(failures))


async def _apply_quota(
    event: WakeEvent, *, store: Any, max_retries: int
) -> list[QuotaAction]:
    """Park/unpark under the provider lock, retrying a fenced-out write once.

    A concurrent Ticket revision bump can make the first save a no-op while
    this wake effect would still be acknowledged. Reloading that Ticket and
    replanning keeps a still-applicable requeue from being dropped. The
    provider lock serializes applies so a newer reset is visible before an
    older exhaust plans a park.
    """
    from src.server.services.autonomy.wakers.quota_store import quota_provider_lock
    from src.server.services.autonomy.wakers.base import canonical_provider_id

    async with quota_provider_lock(canonical_provider_id(event.provider) or event.provider):
        return await _apply_quota_locked(event, store=store, max_retries=max_retries)


async def _apply_quota_locked(
    event: WakeEvent, *, store: Any, max_retries: int
) -> list[QuotaAction]:
    items = await store.load(event)
    stale = bool(getattr(store, "observation_stale", False))
    # Capture before any targeted retry load; those must not drop the page.
    page_after = str(getattr(store, "continuation_after", "") or "").strip()
    actions = plan_quota(
        items, event, max_retries=max_retries, observation_stale=stale
    )
    applied: list[QuotaAction] = []
    for action in actions:
        if not action.writes:
            applied.append(action)
            continue
        if await store.save(action):
            applied.append(action)
            continue
        # The row moved. Reload just this Ticket and retry once so a reset
        # that lost a revision race still requeues when it still applies.
        retry_event = replace(
            event,
            ticket_id=action.target_id,
            line_id="",
            quota_after_ticket_id="",
        )
        refreshed = await store.load(retry_event)
        if page_after and hasattr(store, "continuation_after"):
            store.continuation_after = page_after
        retry_stale = bool(getattr(store, "observation_stale", False))
        retried = plan_quota(
            refreshed,
            event,
            max_retries=max_retries,
            observation_stale=retry_stale,
        )
        again = next(
            (
                item
                for item in retried
                if item.target_id == action.target_id and item.writes
            ),
            None,
        )
        if again is None:
            applied.append(
                next(
                    (
                        item
                        for item in retried
                        if item.target_id == action.target_id
                    ),
                    action,
                )
            )
            continue
        if await store.save(again):
            applied.append(again)
            continue
        # Second failure: replan once more. A permanent fence mismatch
        # (newer observation, human question) stops writing; a still-
        # applicable change must not be acknowledged as done.
        if page_after and hasattr(store, "continuation_after"):
            store.continuation_after = page_after
        final_items = await store.load(retry_event)
        if page_after and hasattr(store, "continuation_after"):
            store.continuation_after = page_after
        final_stale = bool(getattr(store, "observation_stale", False))
        final_plan = plan_quota(
            final_items,
            event,
            max_retries=max_retries,
            observation_stale=final_stale,
        )
        still = next(
            (
                item
                for item in final_plan
                if item.target_id == action.target_id and item.writes
            ),
            None,
        )
        if still is not None:
            raise RuntimeError(
                "quota schedule write lost a race after retry; retry the wake"
            )
        applied.append(
            next(
                (
                    item
                    for item in final_plan
                    if item.target_id == action.target_id
                ),
                action,
            )
        )
    # A full page leaves a cursor on the store. Enqueueing the next page is
    # part of finishing this effect: if it fails, the effect is nacked and
    # the same continuation key is enqueued again.
    after = str(getattr(store, "continuation_after", "") or page_after or "").strip()
    # A newer observation for this provider already outranks this wake, and
    # its own effect walks every page. Continuing would only enqueue one
    # no-op effect per page of Tickets.
    if after and not stale:
        await _enqueue_quota_continuation(event, after)
    return applied


async def _enqueue_quota_continuation(event: WakeEvent, after_ticket_id: str) -> None:
    from src.server.services.autonomy.wakers.runtime import enqueue_waker_effect

    key = quota_continuation_key(event.idempotency_key, after_ticket_id)
    payload = event.as_payload()
    payload["idempotency_key"] = key
    payload["quota_after_ticket_id"] = after_ticket_id
    # Keep the first page's observation rank. A fresh effect row would win on
    # created_at against a same-timestamp reset that already beat the origin.
    origin = event.ranking_effect_id()
    if origin:
        payload["quota_rank_effect_id"] = origin
    await enqueue_waker_effect(
        user_id=event.user_id, idempotency_key=key, payload=payload
    )


async def _apply_repo(
    event: WakeEvent,
    *,
    fix_ci_repos: set[str] | frozenset[str] | None,
    bindings: list[RepoBinding] | None,
    workspace_owner: Callable[[str, str, str], Awaitable[bool]] | None,
    find_ticket: Callable[[str], Awaitable[str | None]] | None,
    create_ticket: Callable[[dict[str, Any]], Awaitable[str]] | None,
) -> str:
    """Record the wake. Open a fix-CI Ticket only for a trusted, opted-in repo."""
    binding = matching_binding(event, bindings)
    if binding is None:
        return "record"
    enabled = fix_ci_enabled(event.repository, fix_ci_repos) or binding.fix_ci
    action = repo_action(event.kind, fix_ci=enabled)
    if action != "create_ticket":
        return "record"
    checker = workspace_owner or default_workspace_owner
    allowed = await checker(binding.user_id, binding.workspace_id, binding.repository)
    if not allowed:
        logger.info(
            "[autonomy] fix-CI skipped for %s; workspace policy refused",
            binding.repository,
        )
        return "record"
    draft = fix_ci_draft(
        replace(
            event,
            user_id=binding.user_id,
            workspace_id=binding.workspace_id,
            repository=binding.repository,
        )
    )
    finder = find_ticket or _find_fix_ci_ticket
    creator = create_ticket or _create_fix_ci_ticket
    # The DB insert path also serializes across processes. Injected writers
    # (tests, callers with their own store) get the in-process lock only.
    await ensure_fix_ci_ticket(
        draft,
        find=finder,
        create=creator,
        lock=fix_ci_origin_lock if create_ticket is None else None,
    )
    return "create_ticket"


async def _poll_codexbar(
    *,
    load_states: Callable[[], Awaitable[dict[str, str]]] | None,
    load_previous: Callable[[], Awaitable[dict[str, str]]] | None,
    load_exhausted: Callable[[], Awaitable[set[str]]] | None,
    enqueue: EnqueueWake,
) -> list[str]:
    states = await (load_states or _load_headroom_states)()
    if not states:
        return []
    previous = await (load_previous or _load_previous_kinds)()
    exhausted = await (load_exhausted or _load_exhausted)()
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    events = quota_transitions(
        previous, states, exhausted=set(exhausted), observed_at=observed_at
    )
    return await enqueue_wake_events(events, user_id="", enqueue=enqueue)


async def _enqueue_effect(
    *, user_id: str, idempotency_key: str, payload: dict[str, Any]
) -> bool:
    from src.server.services.autonomy.wakers.runtime import enqueue_waker_effect

    return await enqueue_waker_effect(
        user_id=user_id, idempotency_key=idempotency_key, payload=payload
    )


async def _load_headroom_states() -> dict[str, str]:
    from src.server.services.native_runtime.usage_headroom import load_usage_headroom

    states = await load_usage_headroom()
    return {str(key): str(value) for key, value in states.items()}


async def _load_previous_kinds() -> dict[str, str]:
    from src.server.services.autonomy.wakers.quota_history import latest_codexbar_quota_kinds

    return await latest_codexbar_quota_kinds()


async def _load_exhausted() -> set[str]:
    from src.server.services.autonomy.wakers.quota_history import known_exhausted_providers

    return await known_exhausted_providers()


async def _find_fix_ci_ticket(origin_trigger_id: str) -> str | None:
    from src.server.database.autonomy.tickets import get_ticket_by_origin_trigger

    row = await get_ticket_by_origin_trigger(origin_trigger_id)
    if not row:
        return None
    return str(row.get("ticket_id") or "") or None


async def _create_fix_ci_ticket(draft: dict[str, Any]) -> str:
    from src.server.database.autonomy.tickets import create_ticket

    row = await create_ticket(
        user_id=str(draft["user_id"]),
        workspace_id=str(draft["workspace_id"]),
        title=str(draft["title"]),
        scope=str(draft["scope"]),
        mandate=dict(draft.get("mandate") or {}),
        policy_version=str(draft.get("policy_version") or ""),
        origin_trigger_id=str(draft.get("origin_trigger_id") or ""),
        origin_thread_key=str(draft.get("origin_thread_key") or ""),
    )
    return str(row.get("ticket_id") or "")


def _safe_error(exc: BaseException) -> str:
    from src.server.services.autonomy.security import redact_external_text

    return redact_external_text(str(exc))[:500]


__all__ = [
    "DispatchReport",
    "WakeEvent",
    "Waker",
    "WakerPassResult",
    "apply_waker_effect",
    "default_wakers",
    "dispatch_wakers",
    "enqueue_wake_events",
    "run_waker_pass",
]
