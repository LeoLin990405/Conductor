"""Recurring tasks use cron only to create ordinary manual Tickets."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.server.services.autonomy.schedules import ScheduleError, next_run_at


def test_next_run_uses_the_named_timezone():
    # 09:00 Asia/Shanghai on a Monday is 01:00 UTC.
    base = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)
    nxt = next_run_at("0 9 * * 1-5", "Asia/Shanghai", base=base)
    assert nxt == datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)


def test_a_bad_cron_or_zone_is_a_typed_error():
    with pytest.raises(ScheduleError):
        next_run_at("not a cron", "UTC")
    with pytest.raises(ScheduleError):
        next_run_at("0 9 * * *", "Mars/Olympus")


@pytest.mark.asyncio
async def test_a_due_schedule_creates_a_stable_task_and_advances(monkeypatch):
    from datetime import datetime, timezone
    from src.server.database.autonomy import schedules as schedule_db
    from src.server.services.autonomy import schedules as service

    due = datetime(2026, 1, 5, 1, 0, tzinfo=timezone.utc)
    row = {
        "schedule_id": "11111111-1111-4111-8111-111111111111",
        "user_id": "u1",
        "workspace_id": "w1",
        "title": "Daily",
        "prompt": "Summarize",
        "cron_expression": "0 9 * * *",
        "timezone": "Asia/Shanghai",
        "worker_provider": "",
        "worker_model": "",
        "worker_daemon_id": "d1",
        "next_run_at": due,
    }
    created: list[dict] = []
    updated: list[dict] = []

    async def due_rows(*, limit=50):
        return [row]

    async def make_task(**kwargs):
        created.append(kwargs)
        return {"ticket_id": "t1"}

    async def update(user_id, schedule_id, **fields):
        updated.append(fields)
        return {**row, **fields}

    monkeypatch.setattr(schedule_db, "due_schedules", due_rows)
    monkeypatch.setattr(service, "create_manual_task", make_task)
    monkeypatch.setattr(schedule_db, "update_schedule", update)

    assert await service.run_due_schedules() == 1
    assert created[0]["external_key"] == "schedule:11111111-1111-4111-8111-111111111111:2026-01-05T01:00:00+00:00"
    assert created[0]["worker_daemon_id"] == "d1"
    assert updated[0]["next_run_at"] > due
    assert updated[0]["last_run_at"] == due


@pytest.mark.asyncio
async def test_run_now_creates_one_ticket_without_moving_the_cron(monkeypatch):
    from src.server.database.autonomy import schedules as schedule_db
    from src.server.services.autonomy import schedules as service

    row = {
        "schedule_id": "11111111-1111-4111-8111-111111111111",
        "user_id": "u1",
        "workspace_id": "w1",
        "title": "Daily",
        "prompt": "Summarize",
        "worker_provider": "",
        "worker_model": "",
        "worker_daemon_id": "d1",
    }
    created: list[dict] = []
    updated: list[dict] = []

    async def get(user_id, schedule_id):
        return row if schedule_id == row["schedule_id"] else None

    async def make_task(**kwargs):
        created.append(kwargs)
        return {"ticket_id": "t1"}

    async def update(user_id, schedule_id, **fields):
        updated.append(fields)
        return {**row, **fields}

    monkeypatch.setattr(schedule_db, "get_schedule", get)
    monkeypatch.setattr(service, "create_manual_task", make_task)
    monkeypatch.setattr(schedule_db, "update_schedule", update)

    assert await service.run_schedule_now("u1", row["schedule_id"]) == {"ticket_id": "t1"}
    assert created[0]["external_key"].startswith(f"schedule:{row['schedule_id']}:manual:")
    assert created[0]["schedule_id"] == row["schedule_id"]
    assert created[0]["worker_daemon_id"] == "d1"
    assert "last_run_at" in updated[0]
    assert await service.run_schedule_now("u1", "other") is None
