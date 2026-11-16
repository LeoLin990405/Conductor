"""Deliberation state machine.

Status moves draft, running, then consensus, decided, or failed.
Round 1 collects proposals. Later rounds critique and revise.
Only non-planner participants vote.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import uuid
from datetime import datetime, timezone

from src.utils.provider_roles import provider_role
from src.server.services.deliberation.dag import handoff_ready_steps, plan_to_dag
from src.server.services.deliberation.errors import DeliberationError
from src.server.services.deliberation.thread_port import MemoryThreadPort

_ROLES = {"participant", "planner"}
_ADAPTERS = {"mock", "cli", "llm", "daemon"}
_BALLOTS = {"agree", "dissent"}
_SETTLED = {"consensus", "decided"}
_PROVIDER_TIMEOUT_SECONDS = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_text(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeliberationError("invalid", message)
    return value


def _normalize_participants(participants: object) -> tuple[list[dict], str]:
    if not isinstance(participants, list):
        raise DeliberationError("invalid_participants", "participants must be a list")
    normalized: list[dict] = []
    seen: set[str] = set()
    planners = 0
    voters = 0
    for item in participants:
        if not isinstance(item, dict):
            raise DeliberationError(
                "invalid_participants",
                "participant must be an object",
            )
        agent_key = item.get("agent_key")
        if not isinstance(agent_key, str) or not agent_key.strip():
            raise DeliberationError("invalid_participants", "agent_key is required")
        if agent_key in seen:
            raise DeliberationError("invalid_participants", "agent_key must be unique")
        seen.add(agent_key)
        role = item.get("role")
        if role not in _ROLES:
            raise DeliberationError(
                "invalid_participants",
                "role must be participant or planner",
            )
        adapter = item.get("adapter")
        if adapter not in _ADAPTERS:
            raise DeliberationError(
                "invalid_participants",
                "adapter must be mock or cli",
            )
        if adapter == "cli" and provider_role(agent_key) == "research":
            raise DeliberationError(
                "invalid_participants",
                "research-only providers cannot join deliberation",
            )
        display_name = item.get("display_name") or agent_key
        if not isinstance(display_name, str) or not display_name.strip():
            raise DeliberationError(
                "invalid_participants",
                "display_name must be a string",
            )
        daemon_id = item.get("daemon_id") or None
        if daemon_id is not None and not isinstance(daemon_id, str):
            raise DeliberationError(
                "invalid_participants",
                "daemon_id must be a string",
            )
        if role == "planner":
            planners += 1
        else:
            voters += 1
        normalized.append(
            {
                "agent_key": agent_key,
                "role": role,
                "adapter": adapter,
                "display_name": display_name,
                "daemon_id": daemon_id,
            }
        )
    if planners != 1 or voters < 2:
        raise DeliberationError(
            "invalid_participants",
            "need one planner and at least two participants",
        )
    planner_key = next(
        row["agent_key"] for row in normalized if row["role"] == "planner"
    )
    return normalized, planner_key


def _normalize_provider_groups(groups: object, participants: list[dict]) -> list[dict]:
    agent_keys = {
        row["agent_key"] for row in participants if row["role"] == "participant"
    }
    if groups is None:
        return [{"group_key": "all", "agent_keys": sorted(agent_keys)}]
    if not isinstance(groups, list) or not groups:
        raise DeliberationError(
            "invalid_provider_groups", "provider_groups must be a non-empty list"
        )
    normalized: list[dict] = []
    seen_groups: set[str] = set()
    seen_agents: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise DeliberationError(
                "invalid_provider_groups", "each provider group must be an object"
            )
        group_key = group.get("group_key")
        keys = group.get("agent_keys")
        if (
            not isinstance(group_key, str)
            or not group_key.strip()
            or group_key in seen_groups
        ):
            raise DeliberationError(
                "invalid_provider_groups", "group_key must be unique and non-empty"
            )
        if (
            not isinstance(keys, list)
            or not keys
            or any(not isinstance(key, str) for key in keys)
        ):
            raise DeliberationError(
                "invalid_provider_groups", "agent_keys must be a non-empty list"
            )
        if len(keys) != len(set(keys)):
            raise DeliberationError(
                "invalid_provider_groups", "agent_keys must be unique within a group"
            )
        unknown = set(keys) - agent_keys
        duplicate = set(keys) & seen_agents
        if unknown or duplicate:
            raise DeliberationError(
                "invalid_provider_groups",
                "provider groups must partition participant agent_keys",
            )
        seen_groups.add(group_key)
        seen_agents.update(keys)
        normalized.append({"group_key": group_key, "agent_keys": list(keys)})
    if seen_agents != agent_keys:
        raise DeliberationError(
            "invalid_provider_groups",
            "provider groups must include every participant agent_key",
        )
    return normalized


def _transcript(messages: list[dict]) -> list[dict]:
    return copy.deepcopy(
        [
            {
                "round_index": message.get("round_index"),
                "agent_key": message.get("agent_key"),
                "kind": message.get("kind"),
                "body": message.get("body"),
                "plan": message.get("plan"),
            }
            for message in messages
        ]
    )


def _ctx(
    row: dict,
    participant: dict,
    *,
    round_index: int,
    kind: str,
) -> dict:
    return {
        "deliberation_id": row["deliberation_id"],
        "ticket_id": row["ticket_id"],
        "objective": row["objective"],
        "round_index": round_index,
        "round_kind": kind,
        "agent_key": participant["agent_key"],
        "role": participant["role"],
        "transcript": _transcript(row.get("messages") or []),
    }


def _plan_of(turn: object) -> dict:
    if not isinstance(turn, dict):
        raise DeliberationError("invalid_turn", "turn must be an object")
    plan = turn.get("plan")
    steps = plan.get("steps") if isinstance(plan, dict) else None
    if not isinstance(steps, list) or not steps:
        raise DeliberationError("invalid_turn", "plan steps are required")
    return plan


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


async def _snapshot(store, deliberation_id: str) -> dict:
    row = await store.load(deliberation_id)
    if row is None:
        raise DeliberationError(
            "not_found",
            f"deliberation {deliberation_id} not found",
        )
    return row


async def _ensure_running(store, deliberation_id: str) -> None:
    get_status = getattr(store, "get_status", None)
    status = (
        await get_status(deliberation_id)
        if get_status is not None
        else (await store.load(deliberation_id) or {}).get("status")
    )
    if status != "running":
        raise DeliberationError(
            "cancelled", "deliberation stopped before the next stage"
        )


async def _await_group(store, deliberation_id: str, calls: list) -> list:
    """Poll durable cancellation while bounded provider calls are in flight."""
    tasks = [asyncio.create_task(call) for call in calls]
    pending = set(tasks)
    try:
        while pending:
            _, pending = await asyncio.wait(
                pending,
                timeout=0.2,
                return_when=asyncio.FIRST_COMPLETED,
            )
            get_status = getattr(store, "get_status", None)
            status = (
                await get_status(deliberation_id)
                if get_status is not None
                else (await store.load(deliberation_id) or {}).get("status")
            )
            if status != "running":
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise DeliberationError(
                    "cancelled", "deliberation was cancelled during provider execution"
                )
            failures = [
                task
                for task in tasks
                if task.done() and not task.cancelled() and task.exception() is not None
            ]
            if failures:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise failures[0].exception()
        return [task.result() for task in tasks]
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _save_message(
    store,
    *,
    deliberation_id: str,
    round_id: str,
    round_index: int,
    participant: dict,
    kind: str,
    body: str,
    plan: dict | None,
) -> None:
    await store.save_message(
        {
            "message_id": str(uuid.uuid4()),
            "deliberation_id": deliberation_id,
            "round_id": round_id,
            "round_index": round_index,
            "participant_id": participant["participant_id"],
            "agent_key": participant["agent_key"],
            "kind": kind,
            "body": body,
            "plan": plan,
            "created_at": _now(),
        }
    )


async def _append_thread(
    thread_port,
    thread_key: str,
    *,
    author: str,
    kind: str,
    body: str,
    round_index: int,
    deliberation_id: str,
) -> None:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    await thread_port.append(
        thread_key,
        author=author,
        kind=kind,
        body=body,
        round_index=round_index,
        event_key=f"{deliberation_id}:{round_index}:{author}:{kind}:{digest}",
    )


async def create_deliberation(
    store,
    *,
    ticket_id: str,
    user_id: str,
    objective: str,
    participants: list[dict],
    max_rounds: int = 3,
    thread_port=None,
    parent_deliberation_id: str | None = None,
) -> dict:
    ticket_id = _require_text(ticket_id, "ticket_id is required")
    user_id = _require_text(user_id, "user_id is required")
    objective_text = _require_text(objective, "objective is required").strip()
    if (
        isinstance(max_rounds, bool)
        or not isinstance(max_rounds, int)
        or not 1 <= max_rounds <= 10
    ):
        raise DeliberationError(
            "invalid_rounds",
            "max_rounds must be an integer from 1 to 10",
        )
    normalized, planner_agent_key = _normalize_participants(participants)
    deliberation_id = str(uuid.uuid4())
    port = MemoryThreadPort() if thread_port is None else thread_port
    thread_key = await port.open(
        deliberation_id=deliberation_id,
        ticket_id=ticket_id,
        participant_keys=sorted(row["agent_key"] for row in normalized),
    )
    now = _now()
    await store.create(
        {
            "deliberation_id": deliberation_id,
            "ticket_id": ticket_id,
            "user_id": user_id,
            "objective": objective_text,
            "status": "draft",
            "round_index": 0,
            "max_rounds": max_rounds,
            "planner_agent_key": planner_agent_key,
            "thread_key": thread_key,
            "parent_deliberation_id": parent_deliberation_id,
            "consensus_plan": None,
            "decision_reason": "",
            "created_at": now,
            "updated_at": now,
        }
    )
    for row in normalized:
        await store.save_participant(
            {
                "participant_id": str(uuid.uuid4()),
                "deliberation_id": deliberation_id,
                "agent_key": row["agent_key"],
                "role": row["role"],
                "adapter": row["adapter"],
                "display_name": row["display_name"],
                "daemon_id": row["daemon_id"],
            }
        )
    return await get_transcript(store, deliberation_id)


async def start_deliberation(
    store,
    deliberation_id: str,
    adapters: dict,
    *,
    thread_port=None,
    executor=None,
    provider_groups: list[dict] | None = None,
) -> dict:
    row = await _snapshot(store, deliberation_id)
    status = row.get("status")
    if status in _SETTLED:
        result = await get_transcript(store, deliberation_id)
        result["handoff"] = None
        return result
    if status != "draft":
        raise DeliberationError(
            "bad_state",
            f"cannot start deliberation from status {status}",
        )
    finalize = getattr(store, "finalize", None)
    if not callable(finalize):
        raise DeliberationError(
            "bad_state",
            "deliberation store must provide atomic finalize() before work can start",
        )
    claim_start = getattr(store, "claim_start", None)
    if not callable(claim_start):
        raise DeliberationError(
            "bad_state",
            "deliberation store must provide atomic claim_start() before work can start",
        )
    participants = list(row.get("participants") or [])
    if not isinstance(adapters, dict):
        raise DeliberationError("missing_adapter", "adapters are required")
    missing = [
        str(participant.get("agent_key"))
        for participant in participants
        if participant.get("agent_key") not in adapters
    ]
    if missing:
        raise DeliberationError(
            "missing_adapter",
            "missing adapter for " + ", ".join(missing),
        )
    planners = [
        participant
        for participant in participants
        if participant.get("role") == "planner"
    ]
    if len(planners) != 1:
        raise DeliberationError("bad_state", "deliberation planner is missing")
    planner = planners[0]
    voters = sorted(
        (
            participant
            for participant in participants
            if participant.get("role") == "participant"
        ),
        key=lambda participant: participant["agent_key"],
    )
    groups = _normalize_provider_groups(provider_groups, participants)
    port = MemoryThreadPort() if thread_port is None else thread_port
    thread_key = row["thread_key"]
    max_rounds = int(row["max_rounds"])
    claimed = await claim_start(deliberation_id)
    if not claimed:
        raise DeliberationError(
            "bad_state",
            "deliberation was already started by another worker or needs recovery",
        )

    try:
        finished_round, synthesis, final_status, decision_reason = await _run_rounds(
            store,
            deliberation_id=deliberation_id,
            adapters=adapters,
            thread_port=port,
            thread_key=thread_key,
            voters=voters,
            planner=planner,
            max_rounds=max_rounds,
            provider_groups=groups,
        )
        daemon_by_agent = {
            participant["agent_key"]: participant.get("daemon_id")
            for participant in participants
        }
        steps = plan_to_dag(
            synthesis,
            deliberation_id=deliberation_id,
            daemon_by_agent=daemon_by_agent,
        )
        handoff = handoff_ready_steps(steps)
        settled = await finalize(
            deliberation_id,
            status=final_status,
            round_index=finished_round,
            consensus_plan=synthesis,
            decision_reason=decision_reason,
            steps=steps,
        )
        if not settled:
            raise DeliberationError(
                "cancelled", "deliberation was cancelled before finalization"
            )
        if executor is not None and handoff["dispatched"]:
            await executor.dispatch(handoff["dispatched"])
    except Exception as exc:
        if isinstance(exc, DeliberationError) and exc.code == "cancelled":
            raise
        fail_running = getattr(store, "fail_running", None)
        if fail_running is not None:
            await fail_running(deliberation_id, str(exc))
        else:
            current = await _snapshot(store, deliberation_id)
            if current.get("status") == "running":
                await store.update(
                    deliberation_id,
                    status="failed",
                    decision_reason=str(exc)[:500],
                    updated_at=_now(),
                )
        raise
    except asyncio.CancelledError:
        request_cancel = getattr(store, "request_cancel", None)
        if request_cancel is not None:
            await request_cancel(deliberation_id)
        else:
            current = await _snapshot(store, deliberation_id)
            if current.get("status") == "running":
                await store.update(
                    deliberation_id, status="cancelled", updated_at=_now()
                )
        raise

    result = await get_transcript(store, deliberation_id)
    result["handoff"] = handoff
    return result


async def get_transcript(store, deliberation_id: str) -> dict:
    row = await _snapshot(store, deliberation_id)
    row["transcript"] = _transcript(row.get("messages") or [])
    synthesis = next(
        (
            message
            for message in reversed(row.get("messages") or [])
            if message.get("kind") == "synthesis"
        ),
        None,
    )
    row["summary"] = (
        (synthesis or {}).get("body")
        or (row.get("consensus_plan") or {}).get("summary")
        or ""
    )
    return row


async def cancel_deliberation(store, deliberation_id: str) -> dict:
    row = await _snapshot(store, deliberation_id)
    if row.get("status") not in {"draft", "running"}:
        raise DeliberationError(
            "bad_state", "only draft or running deliberations can be cancelled"
        )
    request_cancel = getattr(store, "request_cancel", None)
    if request_cancel is None:
        await store.update(deliberation_id, status="cancelled", updated_at=_now())
        cancelled = True
    else:
        cancelled = await request_cancel(deliberation_id)
    if not cancelled:
        raise DeliberationError(
            "bad_state", "deliberation already settled or cancelled"
        )
    return await get_transcript(store, deliberation_id)


async def continue_deliberation(
    store,
    deliberation_id: str,
    *,
    objective: str,
    additional_context: str | None = None,
    max_rounds: int | None = None,
    thread_port=None,
) -> dict:
    parent = await _snapshot(store, deliberation_id)
    if parent.get("status") not in {"consensus", "decided"}:
        raise DeliberationError("bad_state", "can only continue a settled deliberation")
    follow_up = _require_text(objective, "objective is required").strip()
    history = [
        f"Previous objective: {parent.get('objective', '')}",
        f"Previous summary: {parent.get('summary') or (parent.get('consensus_plan') or {}).get('summary', '')}",
    ]
    history.extend(
        f"{message.get('agent_key')} ({message.get('kind')}): {str(message.get('body') or '')[:500]}"
        for message in (parent.get("messages") or [])
        if message.get("kind") in {"proposal", "revision", "dissent"}
    )
    if additional_context and additional_context.strip():
        history.append(f"Additional context: {additional_context.strip()}")
    inherited_objective = (
        f"{follow_up}\n\nContext from completed deliberation:\n" + "\n".join(history)
    )
    participants = [
        {
            key: row.get(key)
            for key in ("agent_key", "role", "adapter", "daemon_id", "display_name")
        }
        for row in parent.get("participants") or []
    ]
    child = await create_deliberation(
        store,
        ticket_id=str(parent["ticket_id"]),
        user_id=str(parent["user_id"]),
        objective=inherited_objective,
        participants=participants,
        max_rounds=int(parent["max_rounds"]) if max_rounds is None else max_rounds,
        thread_port=thread_port,
        parent_deliberation_id=deliberation_id,
    )
    if thread_port is not None:
        await thread_port.append(
            child["thread_key"],
            author=parent["planner_agent_key"],
            kind="continuation",
            body=f"Continuation {child['deliberation_id']}: {follow_up}",
            round_index=0,
            event_key=f"{child['deliberation_id']}:continuation",
        )
    return child


async def _run_rounds(
    store,
    *,
    deliberation_id: str,
    adapters: dict,
    thread_port,
    thread_key: str,
    voters: list[dict],
    planner: dict,
    max_rounds: int,
    provider_groups: list[dict],
) -> tuple[int, dict, str, str]:
    planner_adapter = adapters[planner["agent_key"]]
    for round_index in range(1, max_rounds + 1):
        kind = "propose" if round_index == 1 else "critique"
        round_id = str(uuid.uuid4())
        await store.save_round(
            {
                "round_id": round_id,
                "deliberation_id": deliberation_id,
                "round_index": round_index,
                "kind": kind,
                "provider_groups": provider_groups,
                "status": "open",
                "created_at": _now(),
                "closed_at": None,
            }
        )
        await store.update(deliberation_id, round_index=round_index, updated_at=_now())
        await _ensure_running(store, deliberation_id)
        round_snapshot = await _snapshot(store, deliberation_id)
        participant_turns: dict[str, dict] = {}
        for group in provider_groups:
            await _ensure_running(store, deliberation_id)
            members = [
                participant
                for participant in voters
                if participant["agent_key"] in group["agent_keys"]
            ]
            calls = []
            for participant in members:
                ctx = _ctx(
                    round_snapshot, participant, round_index=round_index, kind=kind
                )
                method = "propose" if kind == "propose" else "critique"
                calls.append(
                    asyncio.wait_for(
                        getattr(adapters[participant["agent_key"]], method)(ctx),
                        timeout=_PROVIDER_TIMEOUT_SECONDS,
                    )
                )
            turns = await _await_group(store, deliberation_id, calls)
            touch_running = getattr(store, "touch_running", None)
            if touch_running is not None and not await touch_running(deliberation_id):
                raise DeliberationError(
                    "cancelled", "deliberation was cancelled after provider execution"
                )
            participant_turns.update(
                (participant["agent_key"], turn)
                for participant, turn in zip(members, turns, strict=True)
            )

        if kind == "propose":
            for participant in voters:
                turn = participant_turns[participant["agent_key"]]
                plan = _plan_of(turn)
                body = _as_text(turn.get("body"))
                await _save_message(
                    store,
                    deliberation_id=deliberation_id,
                    round_id=round_id,
                    round_index=round_index,
                    participant=participant,
                    kind="proposal",
                    body=body,
                    plan=plan,
                )
                await _append_thread(
                    thread_port,
                    thread_key,
                    deliberation_id=deliberation_id,
                    author=participant["agent_key"],
                    kind="proposal",
                    body=body,
                    round_index=round_index,
                )
            plan_kind = "proposal"
        else:
            for participant in voters:
                turn = participant_turns[participant["agent_key"]]
                plan = _plan_of(turn)
                critique_body = _as_text(turn.get("critique") or turn.get("body"))
                revision_body = _as_text(turn.get("body"))
                await _save_message(
                    store,
                    deliberation_id=deliberation_id,
                    round_id=round_id,
                    round_index=round_index,
                    participant=participant,
                    kind="critique",
                    body=critique_body,
                    plan=None,
                )
                await _append_thread(
                    thread_port,
                    thread_key,
                    deliberation_id=deliberation_id,
                    author=participant["agent_key"],
                    kind="critique",
                    body=critique_body,
                    round_index=round_index,
                )
                await _save_message(
                    store,
                    deliberation_id=deliberation_id,
                    round_id=round_id,
                    round_index=round_index,
                    participant=participant,
                    kind="revision",
                    body=revision_body,
                    plan=plan,
                )
                await _append_thread(
                    thread_port,
                    thread_key,
                    deliberation_id=deliberation_id,
                    author=participant["agent_key"],
                    kind="revision",
                    body=revision_body,
                    round_index=round_index,
                )
            plan_kind = "revision"

        current = await _snapshot(store, deliberation_id)
        proposals = [
            message["plan"]
            for message in current.get("messages") or []
            if message.get("round_id") == round_id
            and message.get("kind") == plan_kind
            and isinstance(message.get("plan"), dict)
        ]
        await _ensure_running(store, deliberation_id)
        planner_ctx = _ctx(
            current,
            planner,
            round_index=round_index,
            kind=kind,
        )
        synthesis = await _await_group(
            store,
            deliberation_id,
            [
                asyncio.wait_for(
                    planner_adapter.synthesize(
                        copy.deepcopy(planner_ctx), copy.deepcopy(proposals)
                    ),
                    timeout=_PROVIDER_TIMEOUT_SECONDS,
                )
            ],
        )
        synthesis = synthesis[0]
        synthesis_steps = (
            synthesis.get("steps") if isinstance(synthesis, dict) else None
        )
        if not isinstance(synthesis_steps, list) or not synthesis_steps:
            raise DeliberationError("invalid_turn", "synthesis has no steps")
        summary = _as_text(synthesis.get("summary") or "synthesis")
        await _save_message(
            store,
            deliberation_id=deliberation_id,
            round_id=round_id,
            round_index=round_index,
            participant=planner,
            kind="synthesis",
            body=summary,
            plan=synthesis,
        )
        await _append_thread(
            thread_port,
            thread_key,
            deliberation_id=deliberation_id,
            author=planner["agent_key"],
            kind="synthesis",
            body=summary,
            round_index=round_index,
        )

        votes_by_key: dict[str, dict] = {}
        for group in provider_groups:
            await _ensure_running(store, deliberation_id)
            members = [
                participant
                for participant in voters
                if participant["agent_key"] in group["agent_keys"]
            ]
            vote_calls = [
                asyncio.wait_for(
                    adapters[participant["agent_key"]].vote(
                        _ctx(
                            round_snapshot,
                            participant,
                            round_index=round_index,
                            kind=kind,
                        ),
                        copy.deepcopy(synthesis),
                    ),
                    timeout=_PROVIDER_TIMEOUT_SECONDS,
                )
                for participant in members
            ]
            votes = await _await_group(store, deliberation_id, vote_calls)
            touch_running = getattr(store, "touch_running", None)
            if touch_running is not None and not await touch_running(deliberation_id):
                raise DeliberationError(
                    "cancelled", "deliberation was cancelled after voting"
                )
            votes_by_key.update(
                (participant["agent_key"], vote)
                for participant, vote in zip(members, votes, strict=True)
            )
        cast: list[dict] = []
        for participant in voters:
            vote = votes_by_key[participant["agent_key"]]
            if not isinstance(vote, dict) or vote.get("ballot") not in _BALLOTS:
                raise DeliberationError(
                    "invalid_turn",
                    "ballot must be agree or dissent",
                )
            ballot = vote["ballot"]
            comment = _as_text(vote.get("comment"))
            await store.save_vote(
                {
                    "vote_id": str(uuid.uuid4()),
                    "deliberation_id": deliberation_id,
                    "round_id": round_id,
                    "round_index": round_index,
                    "participant_id": participant["participant_id"],
                    "agent_key": participant["agent_key"],
                    "ballot": ballot,
                    "comment": comment,
                }
            )
            cast.append(
                {
                    "participant": participant,
                    "ballot": ballot,
                    "comment": comment,
                }
            )
        await store.mark_round_closed(round_id)
        if all(item["ballot"] == "agree" for item in cast):
            return round_index, synthesis, "consensus", "all participants agreed"
        if round_index == max_rounds:
            for item in cast:
                if item["ballot"] != "dissent":
                    continue
                participant = item["participant"]
                reason = item["comment"] or "dissent"
                await store.save_dissent(
                    {
                        "dissent_id": str(uuid.uuid4()),
                        "deliberation_id": deliberation_id,
                        "participant_id": participant["participant_id"],
                        "round_id": round_id,
                        "round_index": round_index,
                        "agent_key": participant["agent_key"],
                        "reason": reason,
                    }
                )
                await _save_message(
                    store,
                    deliberation_id=deliberation_id,
                    round_id=round_id,
                    round_index=round_index,
                    participant=participant,
                    kind="dissent",
                    body=reason,
                    plan=None,
                )
                await _append_thread(
                    thread_port,
                    thread_key,
                    deliberation_id=deliberation_id,
                    author=participant["agent_key"],
                    kind="dissent",
                    body=reason,
                    round_index=round_index,
                )
            return (
                round_index,
                synthesis,
                "decided",
                "planner decided at max_rounds",
            )
    raise DeliberationError("bad_state", "deliberation produced no decision")
