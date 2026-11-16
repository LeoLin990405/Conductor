"""Park on quota exhaustion and requeue on reset, with a recorded retry budget.

The plan is pure: it never touches the database. A Ticket parked for quota
keeps the count in ``mandate.quota_waker`` so a later reset still knows how
many requeues are left after attention is cleared. Lines have no parked
status of their own; a line action records that the line's open Tickets were
the thing that moved.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from src.server.services.autonomy.wakers.base import (
    WakeEvent,
    canonical_provider_id,
    provider_spellings,
)

logger = logging.getLogger(__name__)

QUOTA_LEDGER_KEY = "quota_waker"
DEFAULT_MAX_QUOTA_RETRIES = 3
_MAX_LOG = 8
_MAX_ROWS = 1000

_QUOTA_REASONS = frozenset({"quota_exhausted", "quota_retries_exhausted"})


@dataclass(frozen=True, slots=True)
class WorkItem:
    """One Ticket the quota plan can park or requeue."""

    target_id: str
    provider: str
    status: str
    line_id: str = ""
    retries: int = 0
    parked_reason: str = ""
    last_event_key: str = ""
    has_human_question: bool = False
    user_id: str = ""
    log: tuple[dict[str, Any], ...] = ()
    last_observed_at: str = ""
    revision: int = 0


@dataclass(frozen=True, slots=True)
class QuotaAction:
    """One planned change. ``writes`` is false when the row should stay as it is."""

    target_kind: str
    target_id: str
    action: str
    provider: str
    from_status: str
    to_status: str
    retries: int
    writes: bool
    replace_attention: bool
    ledger: dict[str, Any] = field(default_factory=dict)
    attention: dict[str, Any] | None = None
    record: dict[str, Any] = field(default_factory=dict)
    expected_revision: int = 0
    launch_key: str = ""
    launch_user_id: str = ""


def work_item_from_ticket(row: Mapping[str, Any]) -> WorkItem:
    """Read the quota ledger off a ticket row without requiring every column."""
    mandate = row.get("mandate")
    ledger = _ledger(mandate if isinstance(mandate, Mapping) else {})
    attention = row.get("attention")
    reason = str(ledger.get("reason") or "")
    question = ""
    attention_reason = ""
    if isinstance(attention, Mapping):
        question = str(attention.get("question") or "").strip()
        attention_reason = str(attention.get("reason") or "")
        if not reason:
            reason = attention_reason
    # The ledger can still say quota_exhausted after an operator replaced the
    # park notice with their own question. That question is the thing to keep.
    human = bool(question) and attention_reason not in _QUOTA_REASONS
    retries = _retries(ledger.get("retries"))
    log = _log(ledger.get("log"))
    line = row.get("line_id")
    revision = row.get("revision")
    return WorkItem(
        target_id=str(row.get("ticket_id") or ""),
        provider=canonical_provider_id(row.get("worker_provider")),
        status=str(row.get("status") or ""),
        line_id=str(line or ""),
        retries=retries,
        parked_reason=reason if reason in _QUOTA_REASONS else "",
        last_event_key=str(ledger.get("last_event_key") or ""),
        has_human_question=human,
        user_id=str(row.get("user_id") or ""),
        log=tuple(log),
        last_observed_at=str(ledger.get("last_observed_at") or ""),
        revision=revision if isinstance(revision, int) and not isinstance(revision, bool) else 0,
    )


def plan_quota(
    items: list[WorkItem],
    event: WakeEvent,
    *,
    max_retries: int = DEFAULT_MAX_QUOTA_RETRIES,
    observation_stale: bool = False,
) -> list[QuotaAction]:
    """Park on ``quota_exhausted`` and requeue on ``quota_reset``.

    Any other kind, including ``quota_low``, changes nothing: the effect row
    is already the record of that wake. A provider-wide exhaust parks queued
    Tickets only, so a Ticket waiting on a person is left alone. Naming a
    line or a Ticket also parks ``waiting`` work that is not a human question.
    ``observation_stale`` covers a later-ranked observation that shares this
    wake's ``observed_at`` but wins on ``created_at`` / ``effect_id``.
    """
    bound = _bound(max_retries)
    provider = canonical_provider_id(event.provider)
    if event.kind not in {"quota_exhausted", "quota_reset"} or not provider:
        return _empty_target(event, provider)
    selected = [item for item in items if _matches(item, event, provider)]
    if not selected:
        return _empty_target(event, provider)
    targeted = bool(event.ticket_id or event.line_id)
    actions = [
        _plan_item(
            item,
            event,
            provider=provider,
            targeted=targeted,
            max_retries=bound,
            observation_stale=observation_stale,
        )
        for item in selected
    ]
    return actions + _line_rollups(actions, selected, event, provider)


def item_after(item: WorkItem, action: QuotaAction) -> WorkItem:
    """The item a second pass would read after ``action`` was saved."""
    if action.target_kind != "ticket" or not action.writes:
        return item
    ledger = action.ledger
    return WorkItem(
        target_id=item.target_id,
        provider=item.provider,
        status=action.to_status or item.status,
        line_id=item.line_id,
        retries=_retries(ledger.get("retries")),
        parked_reason=str(ledger.get("reason") or ""),
        last_event_key=str(ledger.get("last_event_key") or ""),
        has_human_question=(
            False if action.replace_attention else item.has_human_question
        ),
        user_id=item.user_id,
        log=tuple(_log(ledger.get("log"))),
        last_observed_at=str(ledger.get("last_observed_at") or item.last_observed_at),
        revision=item.revision + (1 if action.writes else 0),
    )


class DbQuotaStore:
    """Ticket reads and ledger writes for the production wake handler."""

    def __init__(self) -> None:
        # Set by ``load`` when another page remains. The handler enqueues a
        # continuation effect instead of walking the rest inside this lease.
        self.continuation_after = ""
        # Set when a later-ranked observation already beat this wake.
        self.observation_stale = False
        self._effect_id = ""

    async def load(self, event: WakeEvent) -> list[WorkItem]:
        from src.server.services.autonomy.wakers.quota_history import (
            latest_quota_observed_at,
            quota_observation_outranks_effect,
        )
        from src.server.services.autonomy.wakers.quota_store import list_quota_work_items

        # Targeted retries must not clear a page cursor the outer apply still
        # owes. Only a provider-wide load refreshes continuation_after.
        targeted = bool(str(event.ticket_id or "").strip())
        if not targeted:
            self.continuation_after = ""
        self.observation_stale = False
        # Continuations fence against the origin wake, not their own row.
        self._effect_id = event.ranking_effect_id()
        names = provider_spellings(event.provider)
        if not names:
            return []
        # A named id that is not a uuid must not fall through into a
        # provider-wide update. The filter would otherwise disappear.
        ticket_id = _uuid_or_none(event.ticket_id) if event.ticket_id else None
        if event.ticket_id and ticket_id is None:
            return []
        line_id = (
            None
            if event.ticket_id
            else (_uuid_or_none(event.line_id) if event.line_id else None)
        )
        if event.line_id and not event.ticket_id and line_id is None:
            return []
        after = _uuid_or_none(event.quota_after_ticket_id)
        if event.quota_after_ticket_id and after is None:
            return []
        # One extra row tells us the page is full without a second query.
        # Ordering by ticket id keeps the cursor stable when a write touches
        # updated_at; an updated_at cursor would skip the rows just parked.
        page = await list_quota_work_items(
            provider_names=names,
            user_id=event.user_id,
            line_id=line_id,
            ticket_id=ticket_id,
            limit=_MAX_ROWS + 1,
            after_ticket_id=after,
        )
        if not targeted and len(page) > _MAX_ROWS:
            page = page[:_MAX_ROWS]
            self.continuation_after = str(page[-1].get("ticket_id") or "")
        elif targeted and len(page) > _MAX_ROWS:
            page = page[:_MAX_ROWS]
        horizon = await latest_quota_observed_at(canonical_provider_id(event.provider))
        if self._effect_id:
            self.observation_stale = await quota_observation_outranks_effect(
                event.provider, self._effect_id
            )
        return [with_quota_horizon(work_item_from_ticket(row), horizon) for row in page]

    async def save(self, action: QuotaAction) -> bool:
        if action.target_kind != "ticket" or not action.writes:
            return False
        from src.server.services.autonomy.wakers.quota_store import apply_quota_schedule

        # The plan already rejected an observation that was newer at load
        # time. Pass it again so the row fence can see one that landed
        # after the plan: a reset that skips a queued Ticket does not bump
        # revision, and the next available snapshot will not emit a reset.
        return await apply_quota_schedule(
            action.target_id,
            expected_status=action.from_status,
            expected_revision=action.expected_revision,
            status=action.to_status or None,
            attention=action.attention,
            replace_attention=action.replace_attention,
            ledger=action.ledger,
            launch_key=action.launch_key,
            launch_user_id=action.launch_user_id,
            observed_at=str(action.ledger.get("last_observed_at") or ""),
            provider=action.provider,
            effect_id=self._effect_id,
        )


def _plan_item(
    item: WorkItem,
    event: WakeEvent,
    *,
    provider: str,
    targeted: bool,
    max_retries: int,
    observation_stale: bool = False,
) -> QuotaAction:
    # A wake that is older than the last applied observation must not
    # reverse it. The provider horizon is copied onto the item before
    # planning, so a delayed exhaust that lost the race to a reset is
    # rejected even when this Ticket was not the row the reset wrote.
    # ``observation_stale`` covers equal timestamps where created_at /
    # effect_id already chose a later wake as the provider history winner.
    if observation_stale or _is_older(event.observed_at, item.last_observed_at):
        return _action(
            item,
            provider,
            "skipped",
            writes=False,
            to_status=item.status,
            ledger=_view(item),
            record={"reason": "stale_quota_event"},
        )
    if item.last_event_key and item.last_event_key == event.idempotency_key:
        return _action(
            item,
            provider,
            "already",
            writes=False,
            to_status=item.status,
            ledger=_view(item),
            record={"reason": "duplicate_event"},
        )
    if event.kind == "quota_exhausted":
        return _plan_exhaust(
            item, event, provider=provider, targeted=targeted, max_retries=max_retries
        )
    return _plan_reset(item, event, provider=provider, max_retries=max_retries)


def _plan_exhaust(
    item: WorkItem, event: WakeEvent, *, provider: str, targeted: bool, max_retries: int
) -> QuotaAction:
    if item.status == "blocked" and item.parked_reason in _QUOTA_REASONS:
        return _action(
            item,
            provider,
            "already",
            writes=False,
            to_status="blocked",
            ledger=_view(item),
            record={"reason": "already_parked"},
        )
    allowed = {"queued", "waiting"} if targeted else {"queued"}
    if item.status == "running":
        return _action(
            item,
            provider,
            "skipped",
            writes=False,
            to_status="running",
            ledger=_view(item),
            record={"reason": "live_lease"},
        )
    if item.status not in allowed:
        return _action(
            item,
            provider,
            "skipped",
            writes=False,
            to_status=item.status,
            ledger=_view(item),
            record={"reason": "not_parkable"},
        )
    if item.has_human_question:
        return _action(
            item,
            provider,
            "skipped",
            writes=False,
            to_status=item.status,
            ledger=_view(item),
            record={"reason": "needs_attention"},
        )
    ledger = _next_ledger(
        item,
        event,
        provider=provider,
        max_retries=max_retries,
        reason="quota_exhausted",
        state="parked",
        action="park",
        retries=item.retries,
    )
    attention = {
        "question": f"Parked because the {provider} quota is exhausted. It will be requeued on quota reset.",
        "reason": "quota_exhausted",
        "provider": provider,
        "retries": item.retries,
        "max_retries": max_retries,
    }
    return _action(
        item,
        provider,
        "park",
        writes=True,
        to_status="blocked",
        ledger=ledger,
        attention=attention,
        replace_attention=True,
        record={
            "reason": "quota_exhausted",
            "retries": item.retries,
            "max_retries": max_retries,
        },
    )


def _plan_reset(
    item: WorkItem, event: WakeEvent, *, provider: str, max_retries: int
) -> QuotaAction:
    if item.status != "blocked":
        return _action(
            item,
            provider,
            "skipped",
            writes=False,
            to_status=item.status,
            ledger=_view(item),
            record={"reason": "not_parked"},
        )
    if item.has_human_question:
        return _action(
            item,
            provider,
            "skipped",
            writes=False,
            to_status="blocked",
            ledger=_view(item),
            record={"reason": "needs_attention"},
        )
    if item.parked_reason == "quota_retries_exhausted":
        return _action(
            item,
            provider,
            "budget_spent",
            writes=False,
            to_status="blocked",
            ledger=_view(item),
            record={"reason": "quota_retries_exhausted", "retries": item.retries},
        )
    if item.parked_reason != "quota_exhausted":
        return _action(
            item,
            provider,
            "skipped",
            writes=False,
            to_status="blocked",
            ledger=_view(item),
            record={"reason": "not_quota_park"},
        )
    if item.retries >= max_retries:
        ledger = _next_ledger(
            item,
            event,
            provider=provider,
            max_retries=max_retries,
            reason="quota_retries_exhausted",
            state="retries_exhausted",
            action="budget_spent",
            retries=item.retries,
        )
        attention = {
            "question": (
                f"Parked because the {provider} quota is exhausted and the "
                f"requeue limit ({max_retries}) is spent."
            ),
            "reason": "quota_retries_exhausted",
            "provider": provider,
            "retries": item.retries,
            "max_retries": max_retries,
        }
        return _action(
            item,
            provider,
            "budget_spent",
            writes=True,
            to_status="blocked",
            ledger=ledger,
            attention=attention,
            replace_attention=True,
            record={
                "reason": "quota_retries_exhausted",
                "retries": item.retries,
                "max_retries": max_retries,
            },
        )
    retries = item.retries + 1
    ledger = _next_ledger(
        item,
        event,
        provider=provider,
        max_retries=max_retries,
        reason="",
        state="requeued",
        action="requeue",
        retries=retries,
    )
    return _action(
        item,
        provider,
        "requeue",
        writes=True,
        to_status="queued",
        ledger=ledger,
        attention=None,
        replace_attention=True,
        record={
            "reason": "quota_reset",
            "retries": retries,
            "max_retries": max_retries,
        },
        # attempts stays put, and the launch key is derived from attempts.
        # A launch that was acknowledged while this Ticket was parked has
        # consumed that key, so the reset carries a new one.
        launch_key=quota_launch_key(item.target_id, event.idempotency_key),
        launch_user_id=item.user_id,
    )


def _line_rollups(
    actions: list[QuotaAction], items: list[WorkItem], event: WakeEvent, provider: str
) -> list[QuotaAction]:
    by_id = {item.target_id: item for item in items}
    grouped: dict[str, list[QuotaAction]] = {}
    for action in actions:
        item = by_id.get(action.target_id)
        if item is None or not item.line_id:
            continue
        if action.action not in {"park", "requeue"}:
            continue
        grouped.setdefault(item.line_id, []).append(action)
    rollups: list[QuotaAction] = []
    for line_id, group in grouped.items():
        kind = "park" if any(action.action == "park" for action in group) else "requeue"
        rollups.append(
            QuotaAction(
                target_kind="line",
                target_id=line_id,
                action=kind,
                provider=provider,
                from_status="active",
                to_status="active",
                retries=max(action.retries for action in group),
                writes=False,
                replace_attention=False,
                record={
                    "reason": event.kind,
                    "tickets": [action.target_id for action in group],
                    "idempotency_key": event.idempotency_key,
                },
            )
        )
    return rollups


def _empty_target(event: WakeEvent, provider: str) -> list[QuotaAction]:
    if event.kind not in {"quota_exhausted", "quota_reset"}:
        return []
    if event.ticket_id:
        return [
            QuotaAction(
                target_kind="ticket",
                target_id=event.ticket_id,
                action="skipped",
                provider=provider,
                from_status="",
                to_status="",
                retries=0,
                writes=False,
                replace_attention=False,
                record={"reason": "not_found"},
            )
        ]
    if event.line_id:
        return [
            QuotaAction(
                target_kind="line",
                target_id=event.line_id,
                action="skipped",
                provider=provider,
                from_status="active",
                to_status="active",
                retries=0,
                writes=False,
                replace_attention=False,
                record={"reason": "no_open_work"},
            )
        ]
    return []


def _matches(item: WorkItem, event: WakeEvent, provider: str) -> bool:
    if item.provider != provider:
        return False
    if event.ticket_id:
        return item.target_id == event.ticket_id
    if event.line_id:
        return item.line_id == event.line_id
    return True


def _next_ledger(
    item: WorkItem,
    event: WakeEvent,
    *,
    provider: str,
    max_retries: int,
    reason: str,
    state: str,
    action: str,
    retries: int,
) -> dict[str, Any]:
    entry = {
        "action": action,
        "provider": provider,
        "idempotency_key": event.idempotency_key,
        "retries": retries,
        "at": event.observed_at,
    }
    log = list(item.log)
    if not (log and log[-1].get("action") == "already" and action == "already"):
        log.append(entry)
    return {
        "provider": provider,
        "reason": reason,
        "state": state,
        "retries": retries,
        "max_retries": max_retries,
        "last_event_key": event.idempotency_key,
        "last_observed_at": event.observed_at,
        "line_id": item.line_id,
        "log": log[-_MAX_LOG:],
    }


def _view(item: WorkItem) -> dict[str, Any]:
    return {
        "provider": item.provider,
        "reason": item.parked_reason,
        "retries": item.retries,
        "last_event_key": item.last_event_key,
        "last_observed_at": item.last_observed_at,
        "line_id": item.line_id,
        "log": list(item.log),
    }


def _action(
    item: WorkItem,
    provider: str,
    action: str,
    *,
    writes: bool,
    to_status: str,
    ledger: dict[str, Any],
    record: dict[str, Any],
    attention: dict[str, Any] | None = None,
    replace_attention: bool = False,
    launch_key: str = "",
    launch_user_id: str = "",
) -> QuotaAction:
    return QuotaAction(
        target_kind="ticket",
        target_id=item.target_id,
        action=action,
        provider=provider,
        from_status=item.status,
        to_status=to_status,
        retries=_retries(ledger.get("retries")),
        writes=writes,
        replace_attention=replace_attention,
        ledger=ledger,
        attention=attention,
        record=record,
        expected_revision=item.revision,
        launch_key=launch_key,
        launch_user_id=launch_user_id,
    )


def _ledger(mandate: Mapping[str, Any]) -> dict[str, Any]:
    raw = mandate.get(QUOTA_LEDGER_KEY)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _retries(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _log(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    entries = [dict(item) for item in value if isinstance(item, dict)]
    return entries[-_MAX_LOG:]


def _bound(max_retries: int) -> int:
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        return DEFAULT_MAX_QUOTA_RETRIES
    return min(20, max(0, max_retries))


def quota_launch_key(ticket_id: str, event_key: str) -> str:
    """Launch identity for one quota requeue.

    The ordinary launch key is ``attempt:{attempts + 1}``. A reset does not
    increment attempts, so it needs its own key or the acknowledged launch
    from the park would never run again.
    """
    raw = f"autonomy:launch:{ticket_id}:quota:{event_key}"
    if len(raw) <= 240 and "\n" not in raw and "\r" not in raw:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"autonomy:launch:{ticket_id}:quota:{digest}"


def quota_continuation_key(event_key: str, after_ticket_id: str) -> str:
    """Effect key for the next page of one quota wake."""
    raw = f"{event_key}:after:{after_ticket_id}"
    if len(raw) <= 240 and "\n" not in raw and "\r" not in raw:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"waker:quota:after:{digest}"


def with_quota_horizon(item: WorkItem, observed_at: str) -> WorkItem:
    """Raise the item's horizon to the provider's newest stored observation."""
    if observed_at and _is_older(item.last_observed_at, observed_at):
        return replace(item, last_observed_at=observed_at)
    return item


def _as_time(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_older(incoming: str, latest: str) -> bool:
    """True when ``incoming`` is strictly earlier than a known observation."""
    if not latest:
        return False
    left = _as_time(incoming)
    right = _as_time(latest)
    if left is None or right is None:
        return str(incoming or "") < str(latest)
    return left < right


def _uuid_or_none(value: str) -> str | None:
    if not value:
        return None
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def ledger_json(ledger: dict[str, Any]) -> str:
    """JSON text for a jsonb write. The ledger is planner output, not a raw event."""
    return json.dumps(ledger, default=str, separators=(",", ":"))
