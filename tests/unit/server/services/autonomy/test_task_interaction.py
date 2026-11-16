"""Task activity and close are about the task, not a worker session."""

from datetime import datetime, timedelta, timezone

from src.server.services.autonomy.task_interaction import (
    close_update,
    project_task_activity,
    undelivered_instructions,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)


def _ticket(**overrides):
    ticket = {
        "ticket_id": "t1",
        "title": "Fix the save button",
        "scope": "Change the label",
        "status": "running",
        "created_at": NOW - timedelta(minutes=5),
        "worker_thread_id": "thread-1",
        "worker_run_id": "run-1",
        "closed_at": None,
        "close_reason": "",
        "ended_at": None,
    }
    ticket.update(overrides)
    return ticket


def test_activity_orders_create_instruction_report_and_close_without_session_ids():
    events = project_task_activity(
        _ticket(closed_at=NOW, close_reason="Signed off"),
        [
            {
                "input_id": "i1",
                "kind": "trigger",
                "text": "from GitHub",
                "created_at": NOW - timedelta(minutes=4),
            },
            {
                "input_id": "i2",
                "kind": "instruction",
                "text": "Use the dev branch",
                "created_at": NOW - timedelta(minutes=2),
            },
        ],
        [
            {
                "report_id": "r1",
                "state": "progress",
                "summary": "Label updated",
                "question": "",
                "created_at": NOW - timedelta(minutes=1),
            }
        ],
    )
    assert [event["kind"] for event in events] == [
        "created",
        "source",
        "instruction",
        "report",
        "closed",
    ]
    assert events[0]["summary"] == "Fix the save button"
    assert events[0]["detail"] == "Change the label"
    assert events[2]["summary"] == "Use the dev branch"
    assert events[3]["state"] == "progress"
    assert events[4]["summary"] == "Signed off"
    rendered = repr(events)
    assert "thread-1" not in rendered
    assert "run-1" not in rendered


def test_undelivered_instructions_skip_delivered_rows_and_source_triggers():
    rows = undelivered_instructions(
        [
            {"input_id": "a", "kind": "instruction", "text": "keep", "delivered_at": None},
            {"input_id": "b", "kind": "answer", "text": "also", "delivered_at": None},
            {"input_id": "c", "kind": "instruction", "text": "gone", "delivered_at": NOW},
            {"input_id": "d", "kind": "trigger", "text": "source", "delivered_at": None},
            {"input_id": "e", "kind": "instruction", "text": "  ", "delivered_at": None},
        ]
    )
    assert [row["input_id"] for row in rows] == ["a", "b"]


def test_close_cancels_open_work_and_keeps_a_completed_outcome():
    running = close_update(_ticket(), reason="  Stop here  ", now=NOW)
    assert running is not None
    assert running["status"] == "cancelled"
    assert running["attention"] is None
    assert running["ended_at"] == NOW
    assert running["closed_at"] == NOW
    assert running["close_reason"] == "Stop here"

    completed = close_update(
        _ticket(status="completed", ended_at=NOW - timedelta(minutes=1)),
        reason="x" * 600,
        now=NOW,
    )
    assert completed is not None
    assert "status" not in completed
    assert "ended_at" not in completed
    assert completed["close_reason"] == "x" * 500

    archived = close_update(_ticket(status="archived", ended_at=NOW), reason="keep", now=NOW)
    assert archived is not None
    assert "status" not in archived
    assert archived["closed_at"] == NOW


def test_close_is_a_no_op_when_the_task_is_already_closed():
    assert close_update(_ticket(closed_at=NOW), reason="again", now=NOW) is None
