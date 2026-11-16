"""SQLite main-agent repository races use two independent connections."""

from __future__ import annotations

import asyncio

import pytest

from src.server.database.main_agent import SQLiteMainAgentRepository


def _run(run_id: str, *, user_id: str = "u1", tasks=None) -> dict:
    return {
        "id": run_id,
        "user_id": user_id,
        "status": "queued",
        "phase": "execute",
        "revision": 0,
        "request": {"objective": "test"},
        "tasks": tasks or [{"id": "a", "status": "queued", "depends_on": []}],
        "alerts": [],
    }


@pytest.mark.asyncio
async def test_two_connections_claim_independent_tasks_for_same_daemon(tmp_path):
    path = tmp_path / "main-agent.sqlite"
    first = SQLiteMainAgentRepository(path)
    second = SQLiteMainAgentRepository(path)
    await first.create_run(
        "u1",
        _run(
            "run-1",
            tasks=[
                {"id": "a", "status": "queued", "depends_on": []},
                {"id": "b", "status": "queued", "depends_on": []},
                {"id": "c", "status": "queued", "depends_on": ["a"]},
            ],
        ),
    )

    blocked = await second.claim_task(
        "run-1", "c", "agent-1", 0, 60, user_id="u1", daemon_id="daemon-1"
    )
    assert blocked is None

    task_a, task_b = await asyncio.gather(
        first.claim_task(
            "run-1", "a", "agent-1", 0, 60, user_id="u1", daemon_id="daemon-1"
        ),
        second.claim_task(
            "run-1", "b", "agent-1", 0, 60, user_id="u1", daemon_id="daemon-1"
        ),
    )
    assert task_a and task_b
    assert task_a["daemon_id"] == task_b["daemon_id"] == "daemon-1"
    assert task_a["claim_token"] != task_b["claim_token"]
    assert await first.get_run("run-1", "u2") is None
    assert await second.claim_task("run-1", "c", "agent-2", 0, 60, user_id="u2") is None

    ready = await first.claim_task(
        "run-1", "c", "agent-2", 0, 60, user_id="u1", daemon_id="daemon-2"
    )
    assert ready is None
    after_a = await first.complete_task(
        "run-1",
        "a",
        "agent-1",
        task_a["revision"],
        {"ok": True},
        user_id="u1",
        claim_token=task_a["claim_token"],
    )
    assert after_a and after_a["status"] == "succeeded"
    task_c = await second.claim_task(
        "run-1", "c", "agent-2", 0, 60, user_id="u1", daemon_id="daemon-2"
    )
    assert task_c and task_c["status"] == "running"


@pytest.mark.asyncio
async def test_competing_claims_for_same_task_have_one_winner(tmp_path):
    path = tmp_path / "same-task.sqlite"
    first = SQLiteMainAgentRepository(path)
    second = SQLiteMainAgentRepository(path)
    await first.create_run("u1", _run("run-race"))
    claims = await asyncio.gather(
        first.claim_task(
            "run-race", "a", "daemon-agent-a", 0, 60, user_id="u1", daemon_id="d1"
        ),
        second.claim_task(
            "run-race", "a", "daemon-agent-b", 0, 60, user_id="u1", daemon_id="d2"
        ),
    )
    assert sum(claim is not None for claim in claims) == 1


@pytest.mark.asyncio
async def test_claim_token_fences_old_attempt_and_retry_is_explicit(tmp_path):
    first = SQLiteMainAgentRepository(tmp_path / "fencing.sqlite")
    second = SQLiteMainAgentRepository(tmp_path / "fencing.sqlite")
    await first.create_run("u1", _run("run-fence"))
    first_claim = await first.claim_task(
        "run-fence", "a", "same-agent", 0, 60, user_id="u1", daemon_id="d1"
    )
    failed = await second.fail_task(
        "run-fence",
        "a",
        "same-agent",
        first_claim["revision"],
        {"message": "transient"},
        True,
        user_id="u1",
        claim_token=first_claim["claim_token"],
    )
    assert failed["status"] == "failed"
    assert failed["attempt_history"][0]["retryable"] is True
    assert (await first.get_run("run-fence", "u1"))["status"] == "running"

    queued = await first.retry_task("run-fence", "a", failed["revision"], user_id="u1")
    assert queued["status"] == "queued"
    second_claim = await second.claim_task(
        "run-fence",
        "a",
        "same-agent",
        queued["revision"],
        60,
        user_id="u1",
        daemon_id="d1",
    )
    stale = await first.complete_task(
        "run-fence",
        "a",
        "same-agent",
        second_claim["revision"],
        {"old": True},
        user_id="u1",
        claim_token=first_claim["claim_token"],
    )
    assert stale is None
    completed = await first.complete_task(
        "run-fence",
        "a",
        "same-agent",
        second_claim["revision"],
        {"ok": True},
        user_id="u1",
        claim_token=second_claim["claim_token"],
    )
    assert completed["status"] == "succeeded"
    assert (await first.get_run("run-fence", "u1"))["status"] == "running"


@pytest.mark.asyncio
async def test_claim_and_callbacks_are_user_scoped(tmp_path):
    repo = SQLiteMainAgentRepository(tmp_path / "ownership.sqlite")
    await repo.create_run("owner", _run("owned"))
    assert (
        await repo.claim_task("owned", "a", "agent", 0, 60, user_id="intruder") is None
    )
    claim = await repo.claim_task("owned", "a", "agent", 0, 60, user_id="owner")
    assert claim is not None
    assert (
        await repo.heartbeat_task(
            "owned",
            "a",
            "agent",
            claim["revision"],
            current_step="step 1",
            user_id="intruder",
            claim_token=claim["claim_token"],
        )
        is None
    )
