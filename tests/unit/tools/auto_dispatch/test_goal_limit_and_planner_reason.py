"""The goal ceiling and the planner's failure reason: one contract each.

The goal limit is one constant. The API and the planner hold a goal to the
same UTF-8 byte ceiling, so a goal the API accepts is a goal the planner can
plan, and a goal the API refuses is refused before a model is called.

A planner failure is recorded as ``planner_failed:<reason>``, where the reason
is one of a fixed vocabulary. The model's own words never reach the message,
the row or a log line, and a failure that names no reason is recorded as
``planner_error`` rather than guessed at.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from pydantic import ValidationError

from src.server.app.dispatch import DispatchStartRequest
from src.server.services.autonomy.roles import AgentState, RolePolicy
from src.tools.auto_dispatch import (
    AlwaysPassReviewer,
    Collaborators,
    Dispatch,
    DispatchMachine,
    DispatchState,
    FileApprovalGate,
    InMemoryDispatchStore,
    InMemoryOutcomeStore,
    NoopMerger,
    RecordingLauncher,
    RoundRobinSelector,
    ScriptedMonitor,
)
from src.tools.auto_dispatch.claude_planner import ClaudePlannerError
from src.tools.auto_dispatch.gates import PLANNER_FAILED
from src.tools.auto_dispatch.machine import cancel_planner_failed
from src.tools.auto_dispatch.planner_reason import (
    GOAL_TOO_LARGE,
    INVALID_JSON,
    NO_JSON,
    PLANNER_ERROR,
    PLANNER_TIMEOUT,
    UNASSIGNED,
    is_planner_failed,
    parked_planner_reason,
    planner_failure_reason,
)
from src.tools.auto_dispatch.planning import MAX_GOAL_BYTES, LLMPlanner
from src.tools.auto_dispatch.protocols import DispatchError

SECRET = "the model said: sk-" "live-SECRET-TOKEN"


def _planner(reply: str, **kwargs: object):
    """An LLMPlanner over a fixed reply, with the calls it made."""
    calls: list[tuple[str, str]] = []

    async def complete(prompt: str, system: str) -> str:
        calls.append((prompt, system))
        return reply

    return LLMPlanner(complete, **kwargs), calls


def _line(name: str) -> dict[str, object]:
    return {
        "name": name,
        "task": "do the work",
        "depends_on": [],
        "write_scope": [f"src/{name}.py"],
        "declared_paths": [f"src/{name}.py"],
        "acceptance": [f"cmd: pytest -q tests/unit/{name}.py"],
        "risk_bearing": False,
    }


def _reply(*lines: dict[str, object]) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "spec": {"title": "Do the work", "body": "## Goal\nShip it."},
            "lines": list(lines),
        }
    )


def _collaborators(tmp_path, planner) -> Collaborators:
    return Collaborators(
        planner=planner,
        selector=RoundRobinSelector(),
        launcher=RecordingLauncher(),
        monitor=ScriptedMonitor(),
        reviewer=AlwaysPassReviewer(),
        merger=NoopMerger(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        outcomes=InMemoryOutcomeStore(),
        store=InMemoryDispatchStore(),
    )


async def _asleep(_seconds: float) -> None:
    return None


async def _parked(tmp_path, planner, goal: str = "ship it") -> Dispatch:
    """Run one dispatch to its parked state, and return it."""
    collaborators = _collaborators(tmp_path, planner)
    machine = DispatchMachine(collaborators, sleep=_asleep)
    dispatch = await collaborators.store.create(
        Dispatch(id="d1", goal=goal, user_id="u", created_at="now")
    )
    return await machine.run(dispatch)


# -- the goal ceiling --------------------------------------------------------


def test_the_request_model_holds_a_goal_to_the_planner_ceiling() -> None:
    """The API and the planner share one limit, so one accepts what the other does."""
    assert DispatchStartRequest(goal="x" * MAX_GOAL_BYTES).goal == "x" * MAX_GOAL_BYTES
    with pytest.raises(ValidationError):
        DispatchStartRequest(goal="x" * (MAX_GOAL_BYTES + 1))


def test_the_goal_limit_counts_bytes_not_characters() -> None:
    """Three bytes per character: under the character count, over the byte count."""
    goal = "€" * (MAX_GOAL_BYTES // 3 + 1)
    assert len(goal) < MAX_GOAL_BYTES
    with pytest.raises(ValidationError):
        DispatchStartRequest(goal=goal)


@pytest.mark.asyncio
async def test_the_planner_refuses_the_goal_the_api_refuses() -> None:
    """The same goal, refused by both, and no model is called for it."""
    goal = "x" * (MAX_GOAL_BYTES + 1)
    with pytest.raises(ValidationError):
        DispatchStartRequest(goal=goal)

    planner, calls = _planner("{}", policy=RolePolicy.default())
    with pytest.raises(DispatchError) as caught:
        await planner.plan(goal)

    assert planner_failure_reason(caught.value) == GOAL_TOO_LARGE
    assert calls == []


@pytest.mark.asyncio
async def test_a_goal_at_the_ceiling_reaches_the_planner() -> None:
    """The goal the API accepts is the goal the planner plans."""
    planner, calls = _planner(_reply(_line("a")), policy=RolePolicy.default())
    lines = await planner.plan("x" * MAX_GOAL_BYTES)
    assert len(lines) == 1
    assert len(calls) == 1


# -- the planner reason ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reply_without_a_plan_parks_no_json(tmp_path) -> None:
    planner, _ = _planner("I am afraid I cannot plan that.")
    parked = await _parked(tmp_path, planner)
    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{NO_JSON}"
    assert parked.lines == []


@pytest.mark.asyncio
async def test_a_reply_that_does_not_parse_parks_invalid_json(tmp_path) -> None:
    planner, _ = _planner('{"schema_version": 1, "lines": [')
    parked = await _parked(tmp_path, planner)
    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{INVALID_JSON}"


@pytest.mark.asyncio
async def test_a_plan_the_policy_cannot_place_parks_unassigned(tmp_path) -> None:
    policy = RolePolicy(developer_pool=("kimi",))
    # A dry pool: the size check needs a place to compare with, so the
    # assignment is what fails.
    planner, _ = _planner(
        _reply(*[_line(f"n{index}") for index in range(7)]),
        policy=policy,
        agents=(AgentState(provider="kimi", capacity=0),),
    )
    parked = await _parked(tmp_path, planner)
    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{UNASSIGNED}"


@pytest.mark.asyncio
async def test_a_plan_larger_than_the_pool_parks_exceeds_developer_pool(tmp_path) -> None:
    """N28: more lines than developer places is its own fixed reason."""
    policy = RolePolicy(developer_pool=("kimi",))
    planner, calls = _planner(
        _reply(*[_line(f"n{index}") for index in range(7)]), policy=policy
    )
    parked = await _parked(tmp_path, planner)
    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:exceeds_developer_pool"
    # The model was told the limit and asked again exactly once.
    assert len(calls) == 2
    assert "at most 1 line(s)" in calls[1][0]
    assert "Developers available: 1." in calls[0][0]


@pytest.mark.asyncio
async def test_a_timed_out_planner_parks_planner_timeout(tmp_path) -> None:
    class TimingOutPlanner:
        async def plan(self, goal: str):
            raise ClaudePlannerError("claude_planner_timeout", reason=PLANNER_TIMEOUT)

    parked = await _parked(tmp_path, TimingOutPlanner())
    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{PLANNER_TIMEOUT}"


@pytest.mark.asyncio
async def test_a_planner_run_that_times_out_parks_planner_timeout(tmp_path) -> None:
    """The LLM planner propagates the timeout its model call raised.

    That exception carries no reason of its own, so the machine has to read
    the timeout off it: a run that ran out of time is not a plain failure.
    """
    async def complete(prompt: str, system: str) -> str:
        raise TimeoutError("the model call ran out of time")

    planner = LLMPlanner(complete, policy=RolePolicy.default())
    parked = await _parked(tmp_path, planner)
    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{PLANNER_TIMEOUT}"


@pytest.mark.asyncio
async def test_an_asyncio_timeout_is_the_same_case(tmp_path) -> None:
    """``asyncio.TimeoutError`` is this same class, so both spellings agree."""
    async def complete(prompt: str, system: str) -> str:
        raise asyncio.TimeoutError("the model call ran out of time")

    planner = LLMPlanner(complete, policy=RolePolicy.default())
    parked = await _parked(tmp_path, planner)
    assert parked.message == f"{PLANNER_FAILED}:{PLANNER_TIMEOUT}"


@pytest.mark.asyncio
async def test_an_unknown_failure_parks_planner_error_and_keeps_the_text_out(
    tmp_path, caplog
) -> None:
    caplog.set_level(logging.DEBUG)

    class RaisingPlanner:
        async def plan(self, goal: str):
            raise RuntimeError(SECRET)

    parked = await _parked(tmp_path, RaisingPlanner())

    assert parked.state is DispatchState.REVIEW
    assert parked.message == f"{PLANNER_FAILED}:{PLANNER_ERROR}"
    assert SECRET not in repr(parked.message)
    assert SECRET not in repr(parked.gates)
    assert SECRET not in caplog.text


@pytest.mark.asyncio
async def test_a_parked_dispatch_can_be_cancelled() -> None:
    """Nothing ran, so there is no branch to move aside: the run just ends."""
    dispatch = Dispatch(
        id="d1",
        goal="ship it",
        user_id="u",
        created_at="now",
        state=DispatchState.REVIEW,
        message=f"{PLANNER_FAILED}:{INVALID_JSON}",
    )

    cancel_planner_failed(dispatch, note="the goal was too big")

    assert dispatch.state is DispatchState.ABORT
    assert dispatch.message == (
        f"{PLANNER_FAILED}:{INVALID_JSON}:cancelled: the goal was too big"
    )


@pytest.mark.asyncio
async def test_a_dispatch_that_did_not_park_on_a_planner_failure_is_not_cancelled() -> None:
    dispatch = Dispatch(
        id="d1",
        goal="ship it",
        user_id="u",
        created_at="now",
        state=DispatchState.REVIEW,
        message="a blocked review",
    )

    with pytest.raises(DispatchError):
        cancel_planner_failed(dispatch)


@pytest.mark.asyncio
async def test_a_row_parked_before_the_reason_codes_can_still_be_cancelled() -> None:
    """A row parked by the older code carries the bare ``planner_failed``.

    It is still a planner failure, so it still has to be reachable: the row
    that predates the reason codes must not be stranded by the reject path.
    """
    dispatch = Dispatch(
        id="d1",
        goal="ship it",
        user_id="u",
        created_at="now",
        state=DispatchState.REVIEW,
        message=PLANNER_FAILED,
    )

    assert is_planner_failed(dispatch)
    assert parked_planner_reason(dispatch) == PLANNER_ERROR

    cancel_planner_failed(dispatch, note="the plan never arrived")

    assert dispatch.state is DispatchState.ABORT
    assert dispatch.message == (
        f"{PLANNER_FAILED}:{PLANNER_ERROR}:cancelled: the plan never arrived"
    )
