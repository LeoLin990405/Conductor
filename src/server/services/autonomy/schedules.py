"""Recurring tasks as a Super-agent Source.

A schedule does not run an agent itself: when due it creates a manual Ticket,
which enters the same Router/Worker/report path as any other task. The next
cron instant is computed after the Ticket is created, so a restart replays the
same due key and the Ticket's origin trigger deduplicates it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from src.server.database.autonomy import schedules as schedule_db
from src.server.services.autonomy.manual import create_manual_task

logger = logging.getLogger(__name__)


class ScheduleError(ValueError):
    """Invalid cron or timezone from an API caller."""


def _zone(name: str) -> ZoneInfo:
    # The route returns this text. A path-shaped name is a ValueError that
    # quotes it ("got: /etc/passwd"), not a not-found, so both are caught and
    # neither is chained: the caller's text is not in the sentence or its cause.
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        raise ScheduleError("unknown timezone") from None


def next_run_at(cron_expression: str, timezone_name: str, *, base: datetime | None = None) -> datetime:
    if not croniter.is_valid(cron_expression):
        raise ScheduleError("invalid cron expression")
    zone = _zone(timezone_name)
    anchor = (base or datetime.now(timezone.utc)).astimezone(zone)
    return croniter(cron_expression, anchor).get_next(datetime).astimezone(timezone.utc)


async def created_next_run(
    cron_expression: str, timezone_name: str, *, base: datetime | None = None
) -> datetime:
    return next_run_at(cron_expression, timezone_name, base=base)


async def run_schedule_now(user_id: str, schedule_id: str) -> dict | None:
    """Create one Ticket for a schedule outside its cron, on demand.

    Unlike a due run this is an explicit user action, so the external key is
    unique per click; the manual Source/Trigger still deduplicates its own
    effects. Returns the Ticket, or None when the schedule is not the caller's.
    """
    schedule = await schedule_db.get_schedule(user_id, schedule_id)
    if schedule is None:
        return None
    ticket = await create_manual_task(
        user_id=user_id,
        workspace_id=str(schedule["workspace_id"]),
        prompt=str(schedule["prompt"]),
        title=str(schedule.get("title") or ""),
        worker_provider=str(schedule.get("worker_provider") or ""),
        worker_model=str(schedule.get("worker_model") or ""),
        worker_daemon_id=str(schedule.get("worker_daemon_id") or "") or None,
        external_key=f"schedule:{schedule_id}:manual:{uuid4()}",
        schedule_id=str(schedule_id),
    )
    await schedule_db.update_schedule(
        user_id,
        schedule_id,
        last_run_at=datetime.now(timezone.utc),
        last_error="",
    )
    return ticket


async def run_due_schedules(*, hold_for_confirmation: bool = False) -> int:
    created = 0
    for schedule in await schedule_db.due_schedules():
        due = schedule["next_run_at"]
        key = f"schedule:{schedule['schedule_id']}:{due.isoformat()}"
        try:
            await create_manual_task(
                user_id=str(schedule["user_id"]),
                workspace_id=str(schedule["workspace_id"]),
                prompt=str(schedule["prompt"]),
                title=str(schedule.get("title") or ""),
                worker_provider=str(schedule.get("worker_provider") or ""),
                worker_model=str(schedule.get("worker_model") or ""),
                worker_daemon_id=str(schedule.get("worker_daemon_id") or "") or None,
                external_key=key,
                schedule_id=str(schedule["schedule_id"]),
                hold_for_confirmation=hold_for_confirmation,
            )
        except Exception as exc:  # noqa: BLE001 - one bad schedule must not stop the pass
            logger.warning("[autonomy] scheduled ticket failed id=%s: %s", schedule["schedule_id"], exc)
            await schedule_db.update_schedule(
                str(schedule["user_id"]),
                str(schedule["schedule_id"]),
                last_error=str(exc)[:1000],
            )
            continue
        try:
            following = next_run_at(
                str(schedule["cron_expression"]),
                str(schedule.get("timezone") or "UTC"),
                base=due,
            )
        except ScheduleError as exc:
            logger.warning("[autonomy] schedule %s cannot advance: %s", schedule["schedule_id"], exc)
            await schedule_db.update_schedule(
                str(schedule["user_id"]),
                str(schedule["schedule_id"]),
                enabled=False,
                last_error=str(exc)[:1000],
            )
            continue
        await schedule_db.update_schedule(
            str(schedule["user_id"]),
            str(schedule["schedule_id"]),
            next_run_at=following,
            last_run_at=due,
            last_error="",
        )
        created += 1
    return created
