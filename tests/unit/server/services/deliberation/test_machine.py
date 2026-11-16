"""Deliberation state machine: consensus, max rounds, and the thread port."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from src.server.services.deliberation.canonical import CANONICAL_PLAN
from src.server.services.deliberation.dag import RecordingExecutor
from src.server.services.deliberation.errors import DeliberationError
from src.server.services.deliberation.machine import (
    _normalize_participants,
    create_deliberation,
    get_transcript,
    start_deliberation,
)
from src.server.services.deliberation.mock_adapter import MockParticipantAdapter
from src.server.services.deliberation.sqlite_store import SqliteDeliberationStore
from src.server.services.deliberation.store import MemoryDeliberationStore
from src.server.services.deliberation.thread_port import MemoryThreadPort

_ROOT = Path(__file__).resolve().parents[5]


def _participants(script: str = "consensus") -> list[dict[str, str]]:
    return [
        {"agent_key": "codex", "role": "participant", "adapter": "mock"},
        {"agent_key": "grok", "role": "participant", "adapter": "mock"},
        {"agent_key": "kimi", "role": "participant", "adapter": "mock"},
        {"agent_key": "planner", "role": "planner", "adapter": "mock"},
    ]


def test_research_only_manus_is_rejected_before_deliberation_adapters_exist():
    participants = [
        {"agent_key": "manus", "role": "participant", "adapter": "cli"},
        {"agent_key": "grok", "role": "participant", "adapter": "cli"},
        {"agent_key": "planner", "role": "planner", "adapter": "mock"},
    ]
    with pytest.raises(DeliberationError, match="research-only providers"):
        _normalize_participants(participants)


def _adapters(
    script: str = "consensus", holdout_key: str = "grok"
) -> dict[str, MockParticipantAdapter]:
    return {
        key: MockParticipantAdapter(
            agent_key=key,
            role="planner" if key == "planner" else "participant",
            script=script,
            holdout_key=holdout_key,
        )
        for key in ("codex", "grok", "kimi", "planner")
    }


def _open_store(backend: str) -> MemoryDeliberationStore | SqliteDeliberationStore:
    # ":memory:" is a distinct database per connection, so build it in the test.
    if backend == "memory":
        return MemoryDeliberationStore()
    return SqliteDeliberationStore(":memory:")


async def _close_store(store: object) -> None:
    closer = getattr(store, "aclose", None) or getattr(store, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


def _plan_step_keys(plan: dict | list) -> list[str]:
    steps = plan["steps"] if isinstance(plan, dict) else plan
    keys: list[str] = []
    for step in steps:
        keys.append(step if isinstance(step, str) else step["step_key"])
    return keys


def _kinds(messages: list[dict]) -> list[str]:
    return [message["kind"] for message in messages]


async def _create(store, participants: list[dict[str, str]] | None = None):
    thread_port = MemoryThreadPort()
    created = await create_deliberation(
        store,
        ticket_id="t1",
        user_id="u1",
        objective="Add add(a,b)",
        participants=_participants() if participants is None else participants,
        max_rounds=3,
        thread_port=thread_port,
    )
    return created, thread_port


async def _start(
    store,
    created: dict,
    script: str,
    executor: RecordingExecutor,
    thread_port: MemoryThreadPort,
):
    # The store keeps only the thread key. Appends land on the port from create.
    return await start_deliberation(
        store,
        created["deliberation_id"],
        _adapters(script, holdout_key="grok"),
        thread_port=thread_port,
        executor=executor,
    )


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.asyncio
async def test_consensus_stops_on_round_one(backend: str) -> None:
    store = _open_store(backend)
    try:
        created, thread_port = await _create(store)
        executor = RecordingExecutor()
        result = await _start(store, created, "consensus", executor, thread_port)

        assert result["status"] == "consensus"
        assert result["round_index"] == 1
        assert result["decision_reason"] == "all participants agreed"
        assert result["dissents"] == []

        messages = result["messages"]
        assert not any(
            message["kind"] in {"critique", "revision", "dissent"}
            for message in messages
        )
        proposals = [message for message in messages if message["kind"] == "proposal"]
        assert [message["agent_key"] for message in proposals] == [
            "codex",
            "grok",
            "kimi",
        ]
        assert _kinds(messages).count("synthesis") == 1

        assert len(result["votes"]) == 3
        assert {vote["ballot"] for vote in result["votes"]} == {"agree"}
        assert {vote["agent_key"] for vote in result["votes"]} == {
            "codex",
            "grok",
            "kimi",
        }

        assert _plan_step_keys(result["consensus_plan"]) == ["spec", "impl", "review"]
        assert _plan_step_keys(CANONICAL_PLAN) == ["spec", "impl", "review"]
        statuses = {step["step_key"]: step["status"] for step in result["steps"]}
        assert statuses == {"spec": "running", "impl": "running", "review": "pending"}

        assert result["handoff"]["parallel"] is True
        assert {step["step_key"] for step in result["handoff"]["dispatched"]} == {
            "spec",
            "impl",
        }
        assert {step["step_key"] for step in result["handoff"]["waiting"]} == {"review"}
        assert len(executor.waves) == 1
        assert len(executor.waves[0]) == 2
        assert {step["step_key"] for step in executor.waves[0]} == {"spec", "impl"}

        posted = thread_port.read(result["thread_key"])
        if inspect.isawaitable(posted):
            posted = await posted
        assert posted

        transcript = await get_transcript(store, created["deliberation_id"])
        assert transcript["status"] == result["status"]
        kinds = _kinds(transcript["transcript"])
        assert "proposal" in kinds
        assert "synthesis" in kinds
    finally:
        await _close_store(store)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.asyncio
async def test_max_rounds_records_dissent(backend: str) -> None:
    store = _open_store(backend)
    try:
        created, thread_port = await _create(store)
        executor = RecordingExecutor()
        result = await _start(store, created, "holdout", executor, thread_port)

        assert result["status"] == "decided"
        assert result["round_index"] == 3
        assert result["decision_reason"] == "planner decided at max_rounds"
        assert len(result["dissents"]) == 1
        assert result["dissents"][0]["agent_key"] == "grok"
        assert result["dissents"][0]["reason"] == "holdout"
        assert any(
            message["kind"] == "dissent" and message["agent_key"] == "grok"
            for message in result["messages"]
        )

        rounds = {rnd["round_index"]: rnd for rnd in result["rounds"]}
        assert set(rounds) == {1, 2, 3}
        assert rounds[1]["kind"] == "propose"
        assert rounds[2]["kind"] == "critique"
        assert rounds[3]["kind"] == "critique"
        assert {rnd["status"] for rnd in result["rounds"]} == {"closed"}

        votes = result["votes"]
        assert len(votes) == 9
        for round_index in (1, 2, 3):
            ballots = {
                vote["agent_key"]: vote["ballot"]
                for vote in votes
                if vote["round_index"] == round_index
            }
            assert ballots == {"codex": "agree", "grok": "dissent", "kimi": "agree"}

        assert _plan_step_keys(result["consensus_plan"]) == ["spec", "impl", "review"]
        statuses = {step["step_key"]: step["status"] for step in result["steps"]}
        assert statuses["spec"] == "running"
        assert statuses["impl"] == "running"
        assert statuses["review"] == "pending"
        assert result["handoff"]["parallel"] is True
        assert {step["step_key"] for step in result["handoff"]["dispatched"]} == {
            "spec",
            "impl",
        }
    finally:
        await _close_store(store)


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.asyncio
async def test_create_rejects_fewer_than_two_participants(backend: str) -> None:
    store = _open_store(backend)
    participants = [
        {"agent_key": "codex", "role": "participant", "adapter": "mock"},
        {"agent_key": "planner", "role": "planner", "adapter": "mock"},
    ]
    try:
        with pytest.raises(DeliberationError) as caught:
            await _create(store, participants)
        assert caught.value.code == "invalid_participants"
    finally:
        await _close_store(store)


def test_thread_port_does_not_touch_conversation_tables() -> None:
    text = (_ROOT / "src/server/services/deliberation/thread_port.py").read_text(
        encoding="utf-8"
    )
    assert "conversation_threads" not in text
    assert "autonomy_threads" not in text
