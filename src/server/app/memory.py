"""Authenticated memory lifecycle endpoints."""

from __future__ import annotations

from typing import Literal, Mapping
from uuid import UUID, uuid4, uuid5

from fastapi import APIRouter, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field

from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1/memory", tags=["Memory"])

ForgetReason = Literal["user_request", "correction", "duplicate", "other"]


class ForgetRequest(BaseModel):
    reason: ForgetReason = "user_request"


class ProposalDecisionRequest(BaseModel):
    approve: bool


class MemoryRevertRequest(BaseModel):
    reason: str


ProposalType = Literal["user", "feedback", "project", "reference"]

#: The fixed thread every owner-typed proposal is filed under. Proposals are
#: keyed by ``(thread, task run)``; a proposal typed at the CLI has neither, so
#: it gets this per-owner thread and a fresh run id per request.
_MANUAL_PROPOSAL_NAMESPACE = UUID("6f1c2b0e-3d4a-5e6f-8a9b-0c1d2e3f4a5b")


class ProposalCreateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    type: ProposalType = "user"


def _long_term_store():
    # Resolve lazily so importing this router does not initialize app setup.
    from src.server.app import setup

    if setup.store is None:
        raise HTTPException(
            status_code=503,
            detail="Long-term store is not configured on this server",
        )
    return setup.store


async def _with_stored_notices(
    payload: dict, *, user_id: str, entry_id: str
) -> dict:
    """Answer a forget with the provider notices storage kept, not the live ones.

    The forget result already carries the read-back the service made after it
    wrote, and that is what goes on the wire. A result without one -- a caller
    whose service could not reach the database -- is read back here instead, so
    the response still names ``provider``, ``status`` and ``memfw_reason`` from
    the rows rather than from the federation's answer.
    """
    from src.server.services.refactor.memory_forget import forget_provider_notices

    if "provider_notices" in payload:
        return payload
    try:
        notices = await forget_provider_notices(user_id=user_id, entry_id=entry_id)
    except Exception:
        # No database handle is reachable in this code path, so the immediate
        # result the service returned stands as the answer.
        return payload
    payload["provider_notices"] = [
        dict(notice) for notice in notices or () if isinstance(notice, Mapping)
    ]
    return payload


@router.get("/export")
async def export_memory_route(
    user_id: CurrentUserId,
    limit: int = Query(default=100, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=64),
    include_secret: bool = False,
):
    """Export canonical owner memory as one bounded JSONL page."""
    from src.server.services.refactor.memory_export import (
        MemoryExportError,
        MemoryExportInputError,
        export_memory_page,
    )

    try:
        return await export_memory_page(
            store=_long_term_store(),
            user_id=user_id,
            limit=limit,
            cursor=cursor,
            include_secret=include_secret,
        )
    except MemoryExportInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MemoryExportError as exc:
        raise HTTPException(status_code=503, detail="Memory export is unavailable") from exc


@router.get("/concepts")
async def list_memory_concepts_route(
    user_id: CurrentUserId,
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """The owner's memory concepts, one row per class and concept id.

    This is what the browse-by-concept page lists: every ``(class, about)``
    pair the owner has memory for, with the number of memories under it. A
    memory that names neither is reported as an unnamed pair rather than
    dropped, so the page never claims there is nothing left to browse.

    ``offset`` continues the listing past its first page, so an owner with
    more concepts than one page holds can still reach the rest.
    """
    from src.server.services.refactor.memory import list_memory_concepts
    from src.server.services.refactor_flags import refactor_flag_enabled

    if not refactor_flag_enabled("memory_index"):
        return {"enabled": False, "concepts": []}
    rows = await list_memory_concepts(user_id=user_id, limit=limit, offset=offset)
    return {
        "enabled": True,
        "concepts": [
            {
                "class": row.get("class"),
                "about": row.get("about"),
                "entries": int(row.get("entries") or 0),
            }
            for row in rows
        ],
    }


@router.get("/entries")
async def list_memory_entries_route(
    user_id: CurrentUserId,
    memory_class: str | None = Query(default=None, alias="class", max_length=256),
    about: str | None = Query(default=None, max_length=256),
    class_is_null: bool = Query(default=False),
    about_is_null: bool = Query(default=False),
    tier: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """The owner's memory rows, narrowed to one class and/or one concept.

    ``class`` names what kind of claim a memory is and ``about`` names the
    concept it is about; both are optional, and one without the other reads
    every memory of that kind or about that concept.

    Each filter has three states, and the third one is why there are two
    parameters per field. Leaving a field out narrows nothing. Naming it
    narrows to the memories that name it. And ``class_is_null`` /
    ``about_is_null`` narrow to the memories that name *nothing* -- the
    unnamed groups the browse page offers next to the named ones. Asking for
    the unnamed group is not the same as asking for everything, so the two
    are told apart on the wire instead of being guessed from the value.

    ``offset`` continues the listing past its first page, so a memory beyond
    the first hundred is still reachable.
    """
    from src.server.contracts.refactor import ContractError
    from src.server.contracts.memory_concept import UNSET
    from src.server.services.refactor.memory import list_memory
    from src.server.services.refactor_flags import refactor_flag_enabled

    if memory_class is not None and class_is_null:
        raise HTTPException(status_code=422, detail="Invalid memory filter")
    if about is not None and about_is_null:
        raise HTTPException(status_code=422, detail="Invalid memory filter")
    class_filter = UNSET
    if class_is_null:
        class_filter = None
    elif memory_class is not None:
        class_filter = memory_class
    about_filter = UNSET
    if about_is_null:
        about_filter = None
    elif about is not None:
        about_filter = about
    if not refactor_flag_enabled("memory_index"):
        return {"enabled": False, "entries": []}
    try:
        rows = await list_memory(
            user_id=user_id,
            tier=tier,
            limit=limit,
            offset=offset,
            memory_class=class_filter,
            about=about_filter,
        )
    except ContractError as exc:
        raise HTTPException(status_code=422, detail="Invalid memory filter") from exc
    return {"enabled": True, "entries": rows}


@router.delete("/entries/{entry_id}")
async def forget_memory_entry_route(
    entry_id: UUID,
    user_id: CurrentUserId,
    body: ForgetRequest | None = None,
):
    """Forget one owned memory entry and report complete or partial cleanup."""
    from src.server.services.refactor.memory_forget import (
        ForgetReason as ServiceForgetReason,
        forget_memory_entry,
    )

    request = body or ForgetRequest()
    try:
        result = await forget_memory_entry(
            user_id=user_id,
            entry_id=str(entry_id),
            store=_long_term_store(),
            reason=ServiceForgetReason(request.reason),
        )
    except ValueError as exc:
        # The request model validates the public reason vocabulary. Keep service
        # validation failures free of internal details on the wire.
        raise HTTPException(status_code=422, detail="Invalid forget request") from exc

    payload = jsonable_encoder(result)
    status = getattr(result, "status", None)
    status = getattr(status, "value", status)
    if status is None and isinstance(payload, dict):
        status = payload.get("status")
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return await _with_stored_notices(
        payload, user_id=user_id, entry_id=str(entry_id)
    )


@router.get("/entries/{entry_id}/source")
async def memory_entry_source_route(entry_id: UUID, user_id: CurrentUserId):
    """Show an exact source only when stored message and offset evidence resolves."""
    from src.server.services.refactor.memory_sources import lookup_memory_source

    result = await lookup_memory_source(user_id=user_id, entry_id=str(entry_id))
    if result.get("status") == "not_found":
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return jsonable_encoder(result)


@router.post("/entries/{entry_id}/revert")
async def revert_memory_entry_route(
    entry_id: UUID,
    request: MemoryRevertRequest,
    user_id: CurrentUserId,
):
    """Restore the immediate predecessor as a new owner-approved revision."""
    from src.server.services.refactor.memory_sources import (
        MemoryRevisionError,
        revert_memory_revision,
    )

    try:
        result = await revert_memory_revision(
            user_id=user_id,
            entry_id=str(entry_id),
            store=_long_term_store(),
            reason=request.reason,
        )
    except MemoryRevisionError as exc:
        if exc.code == "entry_not_found":
            raise HTTPException(status_code=404, detail="Memory entry not found") from exc
        raise HTTPException(status_code=409, detail=exc.code) from exc
    return jsonable_encoder(result)


@router.get("/search")
async def search_memory_route(
    user_id: CurrentUserId,
    q: str = Query(min_length=1, max_length=512),
    limit: int = Query(default=10, ge=1, le=100),
):
    """Ranked recall over the owner's memory (``conductor memory recall``).

    Flag-shaped like the browse endpoints: with the projection switched off the
    answer is an empty result list, not an error.
    """
    from src.server.contracts.refactor import ContractError
    from src.server.services.refactor.memory import search_memory
    from src.server.services.refactor_flags import refactor_flag_enabled

    if not refactor_flag_enabled("memory_index"):
        return {"enabled": False, "query": q, "results": []}
    try:
        rows = await search_memory(user_id=user_id, query=q, limit=limit)
    except ContractError as exc:
        raise HTTPException(status_code=422, detail="Invalid memory search") from exc
    return jsonable_encoder({"enabled": True, "query": q, "results": rows})


@router.post("/proposals", status_code=201)
async def create_memory_proposal_route(
    request: ProposalCreateRequest,
    user_id: CurrentUserId,
):
    """Stage an owner-typed memory for review (``conductor memory propose``).

    This only queues a proposal; nothing reaches long-term memory until the
    owner approves it through the decision route.
    """
    from src.server.database.refactor import proposals as db_proposals
    from src.server.services.refactor.proposals import proposal_identity
    from src.server.services.refactor_flags import refactor_flag_enabled
    from src.server.utils.pg_sanitize import refactor_owner_uuid

    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="Proposal text is empty")
    if not refactor_flag_enabled("memory_index"):
        raise HTTPException(status_code=404, detail="Memory proposals are unavailable")
    owner = refactor_owner_uuid(user_id)
    if owner is None:
        raise HTTPException(status_code=422, detail="Invalid proposal owner")
    origin = "user"
    text_hash, proposal_key = proposal_identity(request.type, text, origin)
    thread_id = str(uuid5(_MANUAL_PROPOSAL_NAMESPACE, f"conductor-cli:{owner}"))
    task_run_id = str(uuid4())
    inserted = await db_proposals.insert_proposals(
        user_id=user_id,
        thread_id=thread_id,
        task_run_id=task_run_id,
        proposals=[
            {
                "type": request.type,
                "text": text,
                "text_hash": text_hash,
                "proposal_key": proposal_key,
                "origin": origin,
            }
        ],
    )
    if not inserted:
        raise HTTPException(status_code=409, detail="Memory proposal was not recorded")
    rows = await db_proposals.list_proposals(user_id=user_id, status="proposed", limit=50)
    proposal_id = next(
        (row.get("proposal_id") for row in rows if row.get("task_run_id") == task_run_id),
        None,
    )
    return {
        "proposal_id": proposal_id,
        "type": request.type,
        "text": text,
        "status": "proposed",
    }


@router.get("/proposals")
async def list_memory_proposals_route(user_id: CurrentUserId, limit: int = 50):
    """List pending proposals for the authenticated owner."""
    from src.server.contracts.refactor import ContractError
    from src.server.services.refactor.proposals import list_pending_proposals

    try:
        rows = await list_pending_proposals(user_id=user_id, limit=limit)
    except ContractError as exc:
        raise HTTPException(status_code=422, detail="Invalid proposal page size") from exc
    if rows is None:
        raise HTTPException(status_code=404, detail="Memory proposals are unavailable")
    return {"proposals": rows}


@router.post("/proposals/{proposal_id}/decision")
async def decide_memory_proposal_route(
    proposal_id: UUID,
    request: ProposalDecisionRequest,
    user_id: CurrentUserId,
):
    """Record an explicit owner decision; approval authority is fixed here."""
    from src.server.contracts.refactor import ContractError
    from src.server.services.refactor.proposals import decide_proposal

    try:
        result = await decide_proposal(
            proposal_id=str(proposal_id),
            user_id=user_id,
            approve=request.approve,
            approval_source="manual",
            store=_long_term_store() if request.approve else None,
        )
    except ContractError as exc:
        message = str(exc)
        if "not found" in message:
            raise HTTPException(status_code=404, detail="Memory proposal not found") from exc
        raise HTTPException(status_code=409, detail="Memory proposal is no longer pending") from exc
    if result is None:
        raise HTTPException(status_code=404, detail="Memory proposal not found")
    return jsonable_encoder(result)
