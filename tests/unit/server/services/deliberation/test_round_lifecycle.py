"""Provider-group round semantics and linked continuation controls."""

from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timedelta, timezone

import pytest

from src.server.services.deliberation.canonical import copy_plan
from src.server.services.deliberation.errors import DeliberationError
from src.server.services.deliberation import machine
from src.server.services.deliberation.machine import (
    cancel_deliberation,
    continue_deliberation,
    create_deliberation,
    get_transcript,
    start_deliberation,
)
from src.server.services.deliberation.planner_adapter import MergingPlannerAdapter
from src.server.services.deliberation.sqlite_store import SqliteDeliberationStore
from src.server.services.deliberation.store import MemoryDeliberationStore
from src.server.services.deliberation.thread_port import MemoryThreadPort


def _participants() -> list[dict[str, str]]:
    return [
        {"agent_key": "alpha", "role": "participant", "adapter": "mock"},
        {"agent_key": "beta", "role": "participant", "adapter": "mock"},
        {"agent_key": "gamma", "role": "participant", "adapter": "mock"},
        {"agent_key": "planner", "role": "planner", "adapter": "mock"},
    ]


async def _created(
    store: MemoryDeliberationStore,
    *,
    objective: str = "Plan work",
    max_rounds: int = 1,
):
    thread_port = MemoryThreadPort()
    row = await create_deliberation(
        store,
        ticket_id="ticket-1",
        user_id="user-1",
        objective=objective,
        participants=_participants(),
        max_rounds=max_rounds,
        thread_port=thread_port,
    )
    return row, thread_port


class TrackingParticipant:
    def __init__(self, agent_key: str, tracker: dict, *, fail: bool = False):
        self.agent_key = agent_key
        self.tracker = tracker
        self.fail = fail

    async def propose(self, ctx: dict) -> dict:
        async with self.tracker["lock"]:
            self.tracker["active"] += 1
            self.tracker["maximum"] = max(
                self.tracker["maximum"], self.tracker["active"]
            )
            self.tracker["snapshots"].append((self.agent_key, list(ctx["transcript"])))
        if self.fail:
            raise RuntimeError("provider failed")
        await asyncio.sleep(0)
        ctx["transcript"].append({"body": "adapter mutation"})
        async with self.tracker["lock"]:
            self.tracker["active"] -= 1
        return {"body": f"proposal from {self.agent_key}", "plan": copy_plan()}

    async def critique(self, ctx: dict) -> dict:
        del ctx
        return {"body": "critique", "plan": copy_plan()}

    async def vote(self, ctx: dict, synthesis: dict) -> dict:
        del ctx, synthesis
        async with self.tracker["lock"]:
            self.tracker["vote_active"] += 1
            self.tracker["vote_maximum"] = max(
                self.tracker["vote_maximum"], self.tracker["vote_active"]
            )
            if self.agent_key != "gamma":
                self.tracker["first_group_active"] += 1
        await asyncio.sleep(0)
        async with self.tracker["lock"]:
            self.tracker["vote_active"] -= 1
            if self.agent_key != "gamma":
                self.tracker["first_group_active"] -= 1
                if self.tracker["first_group_active"] == 0:
                    self.tracker["first_group_done"].set()
            else:
                assert self.tracker["first_group_done"].is_set()
        return {"ballot": "agree", "comment": "settled"}


class BlockingParticipant(TrackingParticipant):
    def __init__(self, agent_key: str, tracker: dict):
        super().__init__(agent_key, tracker)

    async def propose(self, ctx: dict) -> dict:
        del ctx
        self.tracker["started"].set()
        try:
            await self.tracker["release"].wait()
        except asyncio.CancelledError:
            self.tracker["cancelled_keys"].add(self.agent_key)
            self.tracker["cancelled"].set()
            raise
        return {"body": f"proposal from {self.agent_key}", "plan": copy_plan()}


class MutatingRoundParticipant(TrackingParticipant):
    def __init__(self, agent_key: str, tracker: dict):
        super().__init__(agent_key, tracker)

    async def critique(self, ctx: dict) -> dict:
        proposals = [
            item for item in ctx["transcript"] if item.get("kind") == "proposal"
        ]
        if self.agent_key == "alpha":
            proposals[0]["plan"]["steps"][0]["title"] = "tampered"
            self.tracker["mutated"].set()
        elif self.agent_key == "beta":
            await self.tracker["mutated"].wait()
            self.tracker["beta_title"] = proposals[0]["plan"]["steps"][0]["title"]
        return {"body": f"review by {self.agent_key}", "plan": copy_plan()}

    async def vote(self, ctx: dict, synthesis: dict) -> dict:
        del ctx, synthesis
        if self.agent_key == "alpha" and self.tracker["round"] == 0:
            self.tracker["round"] = 1
            return {"ballot": "dissent", "comment": "one more round"}
        return {"ballot": "agree", "comment": "settled"}


class RecordingExecutor:
    def __init__(self, *, fail: bool = False):
        self.calls: list[list[dict]] = []
        self.fail = fail

    async def dispatch(self, steps: list[dict]) -> None:
        self.calls.append(steps)
        if self.fail:
            raise RuntimeError("executor dispatch failed")


class VoteContextParticipant(TrackingParticipant):
    def __init__(self, agent_key: str, tracker: dict):
        super().__init__(agent_key, tracker)

    async def vote(self, ctx: dict, synthesis: dict) -> dict:
        round_index = ctx["round_index"]
        self.tracker["votes"].setdefault(round_index, {})[self.agent_key] = {
            "transcript": copy.deepcopy(ctx["transcript"]),
            "synthesis": copy.deepcopy(synthesis),
        }
        if self.agent_key == "alpha" and round_index == 1:
            synthesis["steps"][0]["title"] = "mutated synthesis"
            return {"ballot": "dissent", "comment": "one more round"}
        if self.agent_key == "alpha" and round_index == 2:
            proposal = next(
                message
                for message in ctx["transcript"]
                if message["kind"] == "proposal"
            )
            proposal["plan"]["steps"][0]["title"] = "mutated transcript"
        return {"ballot": "agree", "comment": "settled"}


def _adapters(tracker: dict, *, failing: str | None = None) -> dict:
    result = {
        key: TrackingParticipant(key, tracker, fail=key == failing)
        for key in ("alpha", "beta", "gamma")
    }
    result["planner"] = MergingPlannerAdapter()
    return result


def _tracker() -> dict:
    return {
        "active": 0,
        "maximum": 0,
        "snapshots": [],
        "vote_active": 0,
        "vote_maximum": 0,
        "first_group_active": 0,
        "first_group_done": asyncio.Event(),
        "lock": asyncio.Lock(),
    }


@pytest.mark.asyncio
async def test_group_rounds_bound_fanout_freeze_context_and_persist_snapshot():
    store = MemoryDeliberationStore()
    created, thread_port = await _created(store)
    tracker = _tracker()
    groups = [
        {"group_key": "first", "agent_keys": ["alpha", "beta"]},
        {"group_key": "second", "agent_keys": ["gamma"]},
    ]

    result = await start_deliberation(
        store,
        created["deliberation_id"],
        _adapters(tracker),
        provider_groups=groups,
        thread_port=thread_port,
    )

    assert result["status"] == "consensus"
    assert tracker["maximum"] == 2
    assert tracker["vote_maximum"] == 2
    assert dict(tracker["snapshots"]) == {
        "alpha": [],
        "beta": [],
        "gamma": [],
    }
    assert result["rounds"][0]["provider_groups"] == groups
    assert result["summary"] == result["consensus_plan"]["summary"]
    assert len(result["votes"]) == 3
    assert result["steps"]


@pytest.mark.asyncio
async def test_votes_keep_frozen_pre_round_transcript_and_receive_synthesis_separately():
    store = MemoryDeliberationStore()
    created, thread_port = await _created(store, max_rounds=2)
    tracker = {**_tracker(), "votes": {}}
    adapters = {
        key: VoteContextParticipant(key, tracker) for key in ("alpha", "beta", "gamma")
    }
    adapters["planner"] = MergingPlannerAdapter()

    result = await start_deliberation(
        store,
        created["deliberation_id"],
        adapters,
        thread_port=thread_port,
    )

    first_round = tracker["votes"][1]
    second_round = tracker["votes"][2]
    assert all(
        vote["transcript"] == first_round["alpha"]["transcript"]
        for vote in first_round.values()
    )
    assert all(
        not any(
            message["kind"] in {"proposal", "synthesis"}
            for message in vote["transcript"]
        )
        for vote in first_round.values()
    )
    assert all("steps" in vote["synthesis"] for vote in first_round.values())
    assert all(
        not any(
            message["kind"] == "synthesis" and message["round_index"] == 1
            for message in vote["transcript"]
        )
        for vote in first_round.values()
    )
    assert all(
        any(
            message["kind"] == "proposal" and message["round_index"] == 1
            for message in vote["transcript"]
        )
        for vote in second_round.values()
    )
    assert all(
        vote["transcript"] == second_round["alpha"]["transcript"]
        for vote in second_round.values()
    )
    assert all(
        not any(message["round_index"] == 2 for message in vote["transcript"])
        for vote in second_round.values()
    )
    assert all(
        not any(
            message["kind"] == "synthesis" and message["round_index"] == 2
            for message in vote["transcript"]
        )
        for vote in second_round.values()
    )
    assert all(
        next(
            message
            for message in vote["transcript"]
            if message["kind"] == "proposal" and message["round_index"] == 1
        )["plan"]["steps"][0]["title"]
        == "Write the spec"
        for vote in second_round.values()
    )
    assert all(
        vote["synthesis"]["steps"][0]["title"] == "Write the spec"
        for votes in tracker["votes"].values()
        for vote in votes.values()
    )
    assert result["status"] == "consensus"


@pytest.mark.asyncio
async def test_thread_event_identity_preserves_distinct_same_kind_messages():
    port = MemoryThreadPort()
    common = {
        "thread_port": port,
        "thread_key": "thread-1",
        "author": "planner",
        "kind": "synthesis",
        "round_index": 1,
    }

    await machine._append_thread(**common, body="first", deliberation_id="delib-1")
    await machine._append_thread(**common, body="first", deliberation_id="delib-1")
    await machine._append_thread(**common, body="second", deliberation_id="delib-1")
    await machine._append_thread(**common, body="first", deliberation_id="delib-2")

    events = await port.read("thread-1")
    assert events[0]["event_key"] == events[1]["event_key"]
    assert events[0]["event_key"] != events[2]["event_key"]
    assert events[0]["event_key"] != events[3]["event_key"]


@pytest.mark.parametrize("missing_method", ["finalize", "claim_start"])
@pytest.mark.asyncio
async def test_start_requires_atomic_store_methods_before_mutating_draft(missing_method):
    store_type = type(
        "StoreWithoutRequiredMethod",
        (MemoryDeliberationStore,),
        {missing_method: None},
    )
    store = store_type()
    created, thread_port = await _created(store)

    required_method = "finalize" if missing_method == "finalize" else "claim_start"
    with pytest.raises(DeliberationError, match=f"atomic {required_method}"):
        await start_deliberation(
            store,
            created["deliberation_id"],
            _adapters(_tracker()),
            thread_port=thread_port,
        )

    row = await store.load(created["deliberation_id"])
    assert row["status"] == "draft"
    assert row["rounds"] == []


@pytest.mark.parametrize("backend", ["memory", "sqlite"])
@pytest.mark.asyncio
async def test_store_claims_once_and_fails_only_stale_running_rows(backend):
    store = (
        MemoryDeliberationStore()
        if backend == "memory"
        else SqliteDeliberationStore(":memory:")
    )
    created, _thread_port = await _created(store)
    deliberation_id = created["deliberation_id"]

    try:
        assert await store.claim_start(deliberation_id)
        assert not await store.claim_start(deliberation_id)
        assert not await store.fail_stale_running(deliberation_id)
        await store.update(
            deliberation_id,
            updated_at=(datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat(),
        )
        assert await store.fail_stale_running(deliberation_id, stale_seconds=30)
        assert not await store.fail_stale_running(deliberation_id, stale_seconds=30)
        row = await store.load(deliberation_id)
        assert row["status"] == "failed"
        assert row["decision_reason"] == (
            "worker lease expired; retry creates a new deliberation"
        )
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


@pytest.mark.asyncio
async def test_provider_failure_prevents_synthesis_votes_and_task_claims():
    store = MemoryDeliberationStore()
    created, thread_port = await _created(store)
    tracker = _tracker()
    executor = RecordingExecutor()
    adapters = _adapters(tracker, failing="beta")
    planner = adapters["planner"]
    planner_calls = 0
    original_synthesize = planner.synthesize

    async def counted_synthesize(ctx: dict, proposals: list[dict]) -> dict:
        nonlocal planner_calls
        planner_calls += 1
        return await original_synthesize(ctx, proposals)

    planner.synthesize = counted_synthesize
    groups = [
        {"group_key": "all", "agent_keys": ["alpha", "beta", "gamma"]}
    ]

    with pytest.raises(RuntimeError, match="provider failed"):
        await start_deliberation(
            store,
            created["deliberation_id"],
            adapters,
            provider_groups=groups,
            thread_port=thread_port,
            executor=executor,
        )

    result = await get_transcript(store, created["deliberation_id"])
    assert result["status"] == "failed"
    assert planner_calls == 0
    assert result["votes"] == []
    assert result["steps"] == []
    assert executor.calls == []


@pytest.mark.asyncio
async def test_executor_error_does_not_rewrite_already_settled_decision():
    store = MemoryDeliberationStore()
    created, thread_port = await _created(store)
    executor = RecordingExecutor(fail=True)

    with pytest.raises(RuntimeError, match="executor dispatch failed"):
        await start_deliberation(
            store,
            created["deliberation_id"],
            _adapters(_tracker()),
            thread_port=thread_port,
            executor=executor,
        )

    result = await get_transcript(store, created["deliberation_id"])
    assert result["status"] == "consensus"
    assert result["consensus_plan"]
    assert result["steps"]
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_finalize_cancel_race_does_not_dispatch_consensus_steps():
    class CancelAtFinalizeStore(MemoryDeliberationStore):
        async def finalize(self, deliberation_id: str, **kwargs) -> bool:
            await self.request_cancel(deliberation_id)
            return await super().finalize(deliberation_id, **kwargs)

    store = CancelAtFinalizeStore()
    created, thread_port = await _created(store)
    executor = RecordingExecutor()

    with pytest.raises(DeliberationError) as caught:
        await start_deliberation(
            store,
            created["deliberation_id"],
            _adapters(_tracker()),
            thread_port=thread_port,
            executor=executor,
        )

    result = await get_transcript(store, created["deliberation_id"])
    assert caught.value.code == "cancelled"
    assert result["status"] == "cancelled"
    assert result["steps"] == []
    assert executor.calls == []


@pytest.mark.asyncio
async def test_nested_plan_mutation_in_one_adapter_does_not_reach_its_peers():
    store = MemoryDeliberationStore()
    created, thread_port = await _created(store, max_rounds=2)
    tracker = {
        **_tracker(),
        "mutated": asyncio.Event(),
        "beta_title": None,
        "round": 0,
    }
    adapters = {
        key: MutatingRoundParticipant(key, tracker)
        for key in ("alpha", "beta", "gamma")
    }
    adapters["planner"] = MergingPlannerAdapter()

    result = await start_deliberation(
        store,
        created["deliberation_id"],
        adapters,
        thread_port=thread_port,
    )

    assert result["status"] == "consensus"
    assert tracker["beta_title"] == "Write the spec"
    stored_proposal = next(
        message for message in result["messages"] if message["kind"] == "proposal"
    )
    assert stored_proposal["plan"]["steps"][0]["title"] == "Write the spec"


@pytest.mark.asyncio
async def test_cancel_during_provider_call_prevents_finalize_and_dispatch():
    store = MemoryDeliberationStore()
    created, thread_port = await _created(store)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    tracker = {
        "started": started,
        "release": release,
        "cancelled": cancelled,
        "cancelled_keys": set(),
    }
    adapters = {
        "alpha": BlockingParticipant("alpha", tracker),
        "beta": BlockingParticipant("beta", tracker),
        "gamma": BlockingParticipant("gamma", tracker),
        "planner": MergingPlannerAdapter(),
    }
    executor = RecordingExecutor()
    running = asyncio.create_task(
        start_deliberation(
            store,
            created["deliberation_id"],
            adapters,
            thread_port=thread_port,
            executor=executor,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    assert await cancel_deliberation(store, created["deliberation_id"])
    await asyncio.wait_for(cancelled.wait(), timeout=1)

    with pytest.raises(DeliberationError) as caught:
        await running
    assert caught.value.code == "cancelled"
    result = await get_transcript(store, created["deliberation_id"])
    assert result["status"] == "cancelled"
    assert result["consensus_plan"] is None
    assert result["steps"] == []
    assert executor.calls == []
    assert tracker["cancelled_keys"] == {"alpha", "beta", "gamma"}


@pytest.mark.asyncio
async def test_external_task_cancellation_is_recorded_durably():
    store = MemoryDeliberationStore()
    created, thread_port = await _created(store)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    tracker = {
        "started": started,
        "release": release,
        "cancelled": cancelled,
        "cancelled_keys": set(),
    }
    adapters = {
        key: BlockingParticipant(key, tracker) for key in ("alpha", "beta", "gamma")
    }
    adapters["planner"] = MergingPlannerAdapter()
    running = asyncio.create_task(
        start_deliberation(
            store,
            created["deliberation_id"],
            adapters,
            thread_port=thread_port,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    running.cancel()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await running

    result = await get_transcript(store, created["deliberation_id"])
    assert result["status"] == "cancelled"
    assert result["consensus_plan"] is None
    assert result["steps"] == []
    assert tracker["cancelled_keys"] == {"alpha", "beta", "gamma"}


@pytest.mark.asyncio
async def test_provider_timeout_marks_failure_and_cleans_up_sibling_calls(monkeypatch):
    monkeypatch.setattr(machine, "_PROVIDER_TIMEOUT_SECONDS", 0.01)
    store = MemoryDeliberationStore()
    created, thread_port = await _created(store)
    started = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()
    tracker = {
        "started": started,
        "release": release,
        "cancelled": cancelled,
        "cancelled_keys": set(),
    }
    adapters = {
        key: BlockingParticipant(key, tracker) for key in ("alpha", "beta", "gamma")
    }
    adapters["planner"] = MergingPlannerAdapter()
    executor = RecordingExecutor()

    with pytest.raises(TimeoutError):
        await start_deliberation(
            store,
            created["deliberation_id"],
            adapters,
            thread_port=thread_port,
            executor=executor,
        )

    result = await get_transcript(store, created["deliberation_id"])
    assert result["status"] == "failed"
    assert result["steps"] == []
    assert tracker["cancelled_keys"] == {"alpha", "beta", "gamma"}
    assert executor.calls == []


@pytest.mark.asyncio
async def test_continue_creates_child_with_parent_history_and_keeps_parent_immutable():
    store = MemoryDeliberationStore()
    parent, thread_port = await _created(store, objective="Parent question")
    tracker = _tracker()
    completed = await start_deliberation(
        store,
        parent["deliberation_id"],
        _adapters(tracker),
        thread_port=thread_port,
    )
    parent_messages = completed["messages"]

    child = await continue_deliberation(
        store,
        parent["deliberation_id"],
        objective="Follow-up question",
        additional_context="Focus on deployment risks.",
        max_rounds=2,
        thread_port=thread_port,
    )
    child_tracker = _tracker()
    child_result = await start_deliberation(
        store,
        child["deliberation_id"],
        _adapters(child_tracker),
        thread_port=thread_port,
    )

    saved_parent = await get_transcript(store, parent["deliberation_id"])
    assert child_result["parent_deliberation_id"] == parent["deliberation_id"]
    assert child_result["objective"].startswith(
        "Follow-up question\n\nContext from completed deliberation:"
    )
    assert "Parent question" in child_result["objective"]
    assert "canonical" in child_result["objective"]
    assert "Additional context: Focus on deployment risks." in child_result["objective"]
    assert [
        (row["agent_key"], row["role"])
        for row in child_result["participants"]
    ] == [
        (row["agent_key"], row["role"])
        for row in completed["participants"]
    ]
    assert child_tracker["snapshots"]
    assert saved_parent["messages"] == parent_messages
