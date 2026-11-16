"""Thread pre-creation: POST /api/v1/threads — create + auto-title.

Decouples thread creation from the message stream. The client gets the real
thread id immediately in a plain 201, then the generated title arrives on the
user lifecycle feed when the Background model's call lands. The first message
then targets POST /threads/{id}/messages, where ensure_thread_exists finds the
row and no-ops — identical to any follow-up turn. The legacy new-thread door
(POST /threads/messages) stays for channels and older clients; rows from either
door are interchangeable.

The legacy ``Accept: text/event-stream`` branch is gone; creation is always a
plain 201 and the title arrives on the lifecycle feed.
"""

import asyncio
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from src.config.settings import HOST_MODE
from src.server.database.conversation import create_thread
from src.server.services.turn_runtime import requested_workspace, turn_route
from src.server.models.conversation import (
    ThreadCreateRequest,
    ThreadCreateResponse,
)
from src.server.services.native_runtime import (
    metadata_for,
    provider_ids,
    status_for,
)
from src.server.services.native_runtime.cwd import resolve_native_cwd
from src.server.services.thread_title import schedule_title_generation
from src.server.utils.api import CurrentUserId

from ._deps import logger, router

@router.post("", status_code=201, response_model=ThreadCreateResponse)
async def create_thread_endpoint(
    request: ThreadCreateRequest, x_user_id: CurrentUserId
):
    """Create a thread and return it as a plain 201.

    Title generation runs detached — a client that never reads the response
    still gets the title persisted, and the durable truth is always readable
    via GET /threads/{id}.
    """
    workspace = await requested_workspace(x_user_id, request.workspace_id)

    # Same credit gate the follow-up /messages call enforces — a quota-blocked
    # client must not mint durable rows + platform title calls through this
    # door. byok and the surface are reported the same way the message path
    # reports them, so both doors get the same verdict for the same user.
    if HOST_MODE != "oss":
        from src.server.database.api_keys import is_byok_active
        from src.server.database.oauth_tokens import has_any_oauth_token
        from src.server.dependencies.usage_limits import enforce_credit_limit

        is_byok, has_oauth = await asyncio.gather(
            is_byok_active(x_user_id), has_any_oauth_token(x_user_id)
        )
        await enforce_credit_limit(
            x_user_id, byok=is_byok or has_oauth, surface=request.platform
        )

    # Decided where the message path decides it, so the thread is born in the
    # workspace its runs will actually use.
    route = turn_route(x_user_id, workspace)
    await route.bind(x_user_id, workspace)
    workspace_id = route.workspace_id

    # A title the caller named is the thread's own: nothing generates over it,
    # since the first message may be no words of the user's at all. Else the
    # same stamp ensure_thread_exists would apply, which is also the CAS
    # expectation the title generator swaps against.
    named_title = request.title
    stamped_title = named_title if named_title is not None else request.first_query[:255]

    requested_native = (request.native_provider or "").strip() or None
    native_provider = None
    runtime_decision = None
    if requested_native == "auto":
        from src.server.services.native_runtime.router import aresolve_requested_provider

        native_provider, decision = await aresolve_requested_provider(
            "auto",
            task=request.first_query or "",
            required_capabilities=request.routing_capabilities,
        )
        if decision is not None:
            runtime_decision = decision.as_dict()
    else:
        native_provider = requested_native
    native_metadata = (
        {"runtime_decision": runtime_decision} if runtime_decision is not None else None
    )
    if native_provider is not None:
        from src.server.services.features import user_feature_enabled

        if not await user_feature_enabled(x_user_id, "native_runtime"):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Native agent runtime is opt-in; enable native_runtime in "
                    "Settings → Experiments first"
                ),
            )
        if native_provider not in provider_ids():
            raise HTTPException(
                status_code=422,
                detail=(
                    "Unknown native provider; available: "
                    f"{', '.join(provider_ids())}"
                ),
            )
        native_status = status_for(native_provider)
        if not native_status.installed:
            raise HTTPException(
                status_code=422,
                detail=(
                    "That native provider is not installed on "
                    f"this host: {native_status.error or 'binary not found'}"
                ),
            )
        try:
            native_cwd = resolve_native_cwd(request.native_cwd)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        native_metadata = metadata_for(native_provider, cwd=native_cwd)
        if runtime_decision is not None:
            native_metadata["runtime_decision"] = runtime_decision

    thread_id = str(uuid4())
    try:
        row = await create_thread(
            conversation_thread_id=thread_id,
            workspace_id=workspace_id,
            current_status="completed",
            thread_index=None,
            title=stamped_title,
            platform=request.platform,
            subagents_allowed=request.subagents_allowed,
            swarm_enabled=request.swarm_enabled,
            reasoning_effort=request.reasoning_effort,
            fast_mode=request.fast_mode,
            metadata=native_metadata,
        )
    except Exception as e:
        logger.exception(f"Error pre-creating thread: {e}")
        raise HTTPException(status_code=500, detail="Failed to create thread")

    if named_title is None:
        schedule_title_generation(
            thread_id=thread_id,
            user_id=x_user_id,
            first_query=request.first_query,
            expected_title=stamped_title,
            timezone=request.timezone,
        )

    payload = ThreadCreateResponse(
        thread_id=thread_id,
        workspace_id=str(workspace_id),
        thread_index=row["thread_index"],
        title=stamped_title,
        msg_type=row["msg_type"],
        native_provider=native_provider,
        runtime_decision=runtime_decision,
        subagents_allowed=row["subagents_allowed"],
        swarm_enabled=bool(row.get("swarm_enabled", False)),
        reasoning_effort=row.get("reasoning_effort"),
        fast_mode=row.get("fast_mode"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )

    return JSONResponse(status_code=201, content=payload.model_dump(mode="json"))
