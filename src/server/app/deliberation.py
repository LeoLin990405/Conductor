"""Super-agent deliberation API.

Mounted under ``/api/v1/autonomy``. Opt-in: create does not start a
deliberation, and start does not claim a ticket or touch the scheduler.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException

from src.server.database.autonomy import tickets as tickets_db
from src.server.models.deliberation import (
    DeliberationContinue,
    DeliberationCreate,
    DeliberationStart,
)
from src.server.services import features
from src.server.services.deliberation.cli_adapter import LocalCliParticipantAdapter
from src.server.services.deliberation.errors import DeliberationError
from src.server.services.deliberation.factory import get_store
from src.server.services.deliberation.machine import (
    cancel_deliberation,
    continue_deliberation,
    create_deliberation,
    get_transcript,
    start_deliberation,
)
from src.server.services.deliberation.mock_adapter import MockParticipantAdapter
from src.server.services.deliberation.planner_adapter import MergingPlannerAdapter
from src.server.services.deliberation.thread_port import DatabaseTicketThreadPort
from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1/autonomy", tags=["Super-agent"])


def _thread_port() -> DatabaseTicketThreadPort:
    return DatabaseTicketThreadPort()


def _jsonable(value: Any) -> Any:
    """Recursively encode datetimes and UUIDs for both stores."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        encoded: dict[Any, Any] = {}
        for key, item in value.items():
            encoded[str(key) if isinstance(key, UUID) else key] = _jsonable(item)
        return encoded
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_jsonable(item) for item in value)
    return value


async def _require_feature(user_id: str) -> None:
    if not await features.user_feature_enabled(user_id, "super_agent"):
        # Hidden surface, not a permission error: the feature catalog is the
        # only thing that should tell a client this exists.
        raise HTTPException(status_code=404, detail="Feature not enabled")


def _client_error(exc: DeliberationError) -> HTTPException:
    if exc.code == "not_found":
        return HTTPException(status_code=404, detail="Deliberation not found")
    return HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)})


def _participant_dicts(body: DeliberationCreate) -> list[dict[str, Any]]:
    return [
        {
            "agent_key": item.agent_key,
            "role": item.role,
            "adapter": item.adapter,
            "daemon_id": item.daemon_id,
            "display_name": item.display_name,
        }
        for item in body.participants
    ]


def _adapters(participants: list[Any], body: DeliberationStart) -> dict[str, Any]:
    adapters: dict[str, Any] = {}
    for participant in participants:
        agent_key = participant["agent_key"]
        role = participant.get("role")
        # Planner is local and deterministic; do not spawn a CLI for it.
        if role == "planner":
            adapters[agent_key] = MergingPlannerAdapter()
        elif role == "participant" and participant.get("adapter") == "cli":
            adapters[agent_key] = LocalCliParticipantAdapter(
                agent_key=agent_key,
                daemon_id=participant.get("daemon_id"),
            )
        elif role == "participant" and participant.get("adapter") == "mock":
            adapters[agent_key] = MockParticipantAdapter(
                agent_key=agent_key,
                role="participant",
                script=body.script,
                holdout_key=body.holdout_key,
            )
    return adapters


async def _owned_aggregate(store: Any, deliberation_id: str, user_id: str) -> dict[str, Any]:
    try:
        aggregate = await get_transcript(store, deliberation_id)
    except DeliberationError as exc:
        raise _client_error(exc) from exc
    # Memory store keeps strings; Postgres may return a UUID.
    if str(aggregate["user_id"]) != user_id:
        raise HTTPException(status_code=404, detail="Deliberation not found")
    return aggregate


@router.post("/tickets/{ticket_id}/deliberations", status_code=201)
async def create_ticket_deliberation(
    ticket_id: str,
    body: DeliberationCreate,
    x_user_id: CurrentUserId,
) -> dict[str, Any]:
    await _require_feature(x_user_id)
    ticket = await tickets_db.get_ticket(ticket_id)
    if ticket is None or str(ticket["user_id"]) != x_user_id:
        raise HTTPException(status_code=404, detail="Ticket not found")
    store = get_store()
    try:
        created = await create_deliberation(
            store,
            ticket_id=ticket_id,
            user_id=x_user_id,
            objective=body.objective,
            participants=_participant_dicts(body),
            max_rounds=body.max_rounds,
            thread_port=_thread_port(),
        )
    except DeliberationError as exc:
        raise HTTPException(status_code=400, detail={"code": exc.code, "message": str(exc)}) from exc
    return _jsonable(created)


@router.post("/deliberations/{deliberation_id}/start")
async def start_ticket_deliberation(
    deliberation_id: str,
    body: DeliberationStart,
    x_user_id: CurrentUserId,
) -> dict[str, Any]:
    await _require_feature(x_user_id)
    store = get_store()
    aggregate = await _owned_aggregate(store, deliberation_id, x_user_id)
    adapters = _adapters(list(aggregate["participants"]), body)
    try:
        provider_groups = (
            [group.model_dump() for group in body.provider_groups]
            if body.provider_groups is not None
            else None
        )
        result = await start_deliberation(
            store,
            deliberation_id,
            adapters,
            thread_port=_thread_port(),
            provider_groups=provider_groups,
        )
    except DeliberationError as exc:
        raise _client_error(exc) from exc
    return _jsonable(result)


@router.get("/deliberations/{deliberation_id}")
async def get_ticket_deliberation(
    deliberation_id: str,
    x_user_id: CurrentUserId,
) -> dict[str, Any]:
    await _require_feature(x_user_id)
    store = get_store()
    aggregate = await _owned_aggregate(store, deliberation_id, x_user_id)
    return _jsonable(aggregate)


@router.get("/deliberations/{deliberation_id}/summary")
async def get_ticket_deliberation_summary(
    deliberation_id: str,
    x_user_id: CurrentUserId,
) -> dict[str, Any]:
    await _require_feature(x_user_id)
    aggregate = await _owned_aggregate(get_store(), deliberation_id, x_user_id)
    return _jsonable(
        {
            "deliberation_id": aggregate["deliberation_id"],
            "status": aggregate["status"],
            "round_index": aggregate["round_index"],
            "summary": aggregate.get("summary", ""),
            "consensus_plan": aggregate.get("consensus_plan"),
            "decision_reason": aggregate.get("decision_reason", ""),
            "dissents": aggregate.get("dissents", []),
        }
    )


@router.post("/deliberations/{deliberation_id}/continue", status_code=201)
async def continue_ticket_deliberation(
    deliberation_id: str,
    body: DeliberationContinue,
    x_user_id: CurrentUserId,
) -> dict[str, Any]:
    await _require_feature(x_user_id)
    store = get_store()
    await _owned_aggregate(store, deliberation_id, x_user_id)
    try:
        result = await continue_deliberation(
            store,
            deliberation_id,
            objective=body.objective,
            additional_context=body.additional_context,
            max_rounds=body.max_rounds,
            thread_port=_thread_port(),
        )
    except DeliberationError as exc:
        raise _client_error(exc) from exc
    return _jsonable(result)


@router.post("/deliberations/{deliberation_id}/cancel")
async def cancel_ticket_deliberation(
    deliberation_id: str,
    x_user_id: CurrentUserId,
) -> dict[str, Any]:
    await _require_feature(x_user_id)
    store = get_store()
    await _owned_aggregate(store, deliberation_id, x_user_id)
    try:
        result = await cancel_deliberation(store, deliberation_id)
    except DeliberationError as exc:
        raise _client_error(exc) from exc
    return _jsonable(result)
