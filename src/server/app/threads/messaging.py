"""Thread messaging: send/stream SSE, reconnect/replay, control, retry, mux."""

import json
from typing import Literal, Optional
from uuid import uuid4

import asyncio

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

# require_thread_owner is called through the module (auth_api.…) so a single
# definition-site patch governs every route — a consumer-site patch that stops
# intercepting after a move would silently bypass auth in tests.
from src.server.utils import api as auth_api
from src.server.utils.api import (
    CurrentUserId,
    database_unavailable,
    service_token_matches,
)
from src.server.models.chat import ChatRequest
from src.server.database.conversation import (
    get_thread_auth_meta,
    get_thread_owner_id,
    get_replay_thread_data,
)
from src.server.contracts.turn_kinds import ANNOUNCED_KEYS
from src.server.database.pool import DATABASE_UNAVAILABLE
from src.server.dependencies.usage_limits import (
    ChatRateLimited,
    quota_service_unavailable,
)

from src.observability import (
    observe_background_chat_turn,
    observe_replay_stream,
)

# Import setup module to access initialized globals
from src.server.app import setup

from ._deps import _get_service_token, _track_task, logger, router


#: The only thread surfaces this route still serves. ``superagent_router`` is
#: the Router; ``superagent_worker`` and ``superagent_watchdog`` are Worker-class
#: lanes (implementation and review) that run the PTC graph on the platform's
#: behalf. Every other surface — a person's chat, a legacy automation, a
#: channel thread — has been retired.
_LIVE_THREAD_PLATFORMS = frozenset({
    "superagent_router",
    "superagent_worker",
    "superagent_watchdog",
})

_CHAT_RETIRED_MESSAGE = (
    "Direct chat is retired. This thread no longer runs a chat turn."
)
_CHAT_RETIRED_HINT = (
    "Hand the work to the Super-agent instead: create a Ticket, or send the "
    "request through the Router/Worker dispatch path so it can be routed, "
    "implemented and reviewed. See docs/operations.md#retired-direct-chat."
)


def _authoritative_platform(
    thread_meta: dict | None, requested: str | None
) -> str:
    """The surface a turn on this thread runs as.

    An existing thread's stored surface decides, and a missing or NULL one
    means a retired chat thread (``""``). Only a thread with no row yet takes
    the request's surface, since that request is what creates it.
    """
    if thread_meta is not None:
        return str(thread_meta.get("platform") or "")
    return str(requested or "")


def _chat_retired() -> HTTPException:
    """410 for a turn that would fall through to the retired PTC chat graph.

    The code is the stable contract; the hint is the migration path. Returned
    before any durable work (a run row, a fork truncation, a credit hold) so a
    retired call cannot leave state behind.
    """
    return HTTPException(
        status_code=410,
        detail={
            "code": "chat_retired",
            "message": _CHAT_RETIRED_MESSAGE,
            "hint": _CHAT_RETIRED_HINT,
        },
    )


async def _assert_stream_transport_ready() -> None:
    """503 before any durable row when the Redis event transport is down (I6).

    Every chat consumer — first connect included — tails the Redis stream
    (``stream_from_log``), so a deployment without Redis event storage can
    never deliver a live turn: refuse admission outright instead of
    committing a 200 to a stream nothing will ever write. When Redis IS
    configured, a PING failure here means the run's very first buffered
    event would finalize it failed(transport_lost); refusing admission is
    strictly cheaper.
    """
    from src.server.services.runs.executor import LocalRunExecutor
    from src.utils.cache.redis_cache import get_cache_client

    manager = LocalRunExecutor.get_instance()
    if not (manager.enable_storage and manager.event_storage_backend == "redis"):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "transport_unavailable",
                "message": (
                    "This deployment is configured without the Redis "
                    "event-stream transport; live chat streaming is "
                    "unavailable."
                ),
            },
        )
    try:
        cache = get_cache_client()
        if cache.enabled and cache.client and await cache.client.ping():
            return
    except Exception:
        pass
    raise HTTPException(
        status_code=503,
        detail={
            "code": "transport_unavailable",
            "message": (
                "The event-stream transport is temporarily unreachable; "
                "retry shortly."
            ),
        },
        headers={"Retry-After": "3"},
    )


async def _consume_background_gen(
    gen,
    label: str,
    thread_id: str,
    run_id: str,
) -> bool:
    """Drain an async generator in the background, cleaning up Redis on failure."""
    _ok = True
    _error_text: str | None = None
    try:
        async for _ in gen:
            pass
    except Exception as exc:
        _ok = False
        _error_text = f"{type(exc).__name__}: {exc}"
        logger.error(
            f"[{label}] Background workflow failed: thread_id={thread_id} run_id={run_id}",
            exc_info=True,
        )
    finally:
        # Ownership check: if BTM still drives this exact run's workflow
        # task, this generator was only a dead tail (e.g. the stream
        # consumer failed after handoff). The executor owns the ledger row
        # and all terminal transport — finalizing here would terminalize a
        # row whose graph is still checkpointing, and BTM would later lose
        # its own CAS.
        _btm_live = False
        try:
            from src.server.services.runs.executor import (
                LocalRunExecutor,
            )

            _btm_live = await LocalRunExecutor.get_instance().is_run_live(
                thread_id, run_id
            )
        except Exception:
            pass
        if not _ok and _btm_live:
            logger.warning(
                f"[{label}] stream consumer died but the run's executor is "
                f"still live: thread_id={thread_id} run_id={run_id}; leaving "
                f"the run to its owner"
            )

        # When the generator raised before reaching start_run, the
        # frontend already received {status: dispatched, run_id} and
        # navigated to workflow:stream:{tid}:{rid} — but no events will
        # ever land. The coordinator is the last-resort owner (I6): it
        # settles a still-in_progress row and writes the terminal frames a
        # reconnected client needs; it never raises.
        if not _ok and not _btm_live:
            # This generator was already primed past START (2.4c) — the run
            # row exists, so the coordinator's reconcile (here, or the real
            # owner's finalize) enqueues the durable watch_clear on the
            # report-back ordering chain, which owns any pair teardown. No direct
            # report-back clear here: it would run OFF-CHAIN against a live
            # admission (round-19 P1).
            from src.server.services.runs.coordinator import RunCoordinator

            await RunCoordinator.get_instance().reconcile_orphaned_dispatch(
                thread_id, run_id, error_text=_error_text, label=label
            )

    return _ok


# =============================================================================
# THREAD MESSAGES (SSE streams)
# =============================================================================




@router.post("/{thread_id}/messages")
async def send_thread_message(
    thread_id: str, request: ChatRequest, auth: ChatRateLimited,
    raw_request: Request,
):
    """
    Send a message to an existing thread. Returns an SSE stream.
    """
    return await _handle_send_message(
        _client_body(request, raw_request), auth, thread_id, raw_request
    )


def _is_service_request(raw_request: Request | None) -> bool:
    """Whether the server sent ``raw_request`` itself: it carries the service
    token, or oss mode has no token configured to check, which trusts the
    self-dispatch."""
    from src.config.settings import HOST_MODE

    token = _get_service_token()
    sent = raw_request.headers.get("X-Service-Token", "") if raw_request else ""
    return service_token_matches(sent, token) or (HOST_MODE == "oss" and not token)


# Fields only the server's own requests set. A user's copy would post a system
# message, label their message as a report-back, forge a report-back watch's
# cleanup, or settle a parked steer as its own turn; ``surface_rules`` is a channel gateway's, whose text
# rides the operator role on providers with a native channel.
_SERVER_ONLY_FIELDS = (
    "query_type",
    *ANNOUNCED_KEYS,
    "report_back_ptc_thread_id",
    "origin_dispatch_gen",
    "disable_subagents",
    "surface_rules",
    "steer_job_id",
)


def _client_body(request: ChatRequest, raw_request: Request) -> ChatRequest:
    """``request`` with the server-only fields cleared, unless the server sent it.

    Cleared as the body enters, before the route settles anything: a
    regenerate of a turn that never started takes the type the turn's row
    stored, a report-back's ``system``, which a strip after it would clear.
    """
    if _is_service_request(raw_request):
        return request
    cleared = {name: None for name in _SERVER_ONLY_FIELDS if getattr(request, name)}
    return request.model_copy(update=cleared) if cleared else request


async def _reject_duplicate_request(
    thread_id: str,
    request_key: str,
    user_id: str,
    *,
    except_job_id: str | None = None,
) -> None:
    """409 if this request_key already reached a run, as its own turn or as
    a steer into another: a retransmit.

    Route-level twin of the in-generator ``dedup_retransmit_or_raise``:
    classifying here answers with a clean HTTP 409 before a thread is
    minted, a fork truncates the rows holding the key, or a steering path
    consumes the duplicate. Discloses the existing run's identity only to
    the owning user. ``except_job_id`` is the parked steer a server-posted
    turn carries, which is that turn's own message, not a copy of it.
    """
    from src.server.services.runs.admission import run_named_by_key

    keyed = await run_named_by_key(
        thread_id, request_key, except_job_id=except_job_id
    )
    if keyed is None:
        return
    existing = keyed.run
    existing_thread = str(existing["conversation_thread_id"])
    owner_id = await get_thread_owner_id(existing_thread)
    detail: dict = {
        "code": "duplicate_request",
        "message": (
            "This request was already accepted; reconnect to the existing "
            "run instead of resending."
        ),
    }
    # Fail closed: an unresolvable owner (thread deleted mid-probe) gets the
    # bare conflict, never another user's run identity.
    if owner_id is not None and owner_id == user_id:
        detail.update(
            thread_id=existing_thread,
            run_id=str(existing["conversation_response_id"]),
            run_status=existing["status"],
        )
        # A steer's run is another message's turn, not one to retry or adopt.
        if keyed.steered:
            detail["steered"] = True
    raise HTTPException(status_code=409, detail=detail)


async def _settle_against_branch(
    thread_id: str, request: ChatRequest, user_id: str
) -> ChatRequest:
    """A fork or HITL answer checked against the thread's branch.

    A retransmit whose first copy started after the duplicate probe finds
    that copy's run there and reads it as a change its view never saw. Its
    key names the run, so it gets the duplicate answer, not a refusal.
    """
    from src.server.handlers.checkpoint_handler import (
        settle_fork_request,
        settle_resume_request,
    )

    try:
        if request.fork_from_turn is not None and request.checkpoint_id:
            return await settle_fork_request(thread_id, request)
        if request.hitl_response:
            return await settle_resume_request(thread_id, request)
        return request
    except HTTPException as refused:
        if refused.status_code == 409 and request.request_key:
            await _reject_duplicate_request(
                thread_id,
                request.request_key,
                user_id,
                except_job_id=request.steer_job_id,
            )
        raise


async def _handle_send_message(
    request: ChatRequest, auth: ChatRateLimited, thread_id: str,
    raw_request: Request | None = None,
    *,
    retry_of_run_id: str | None = None,
):
    """Shared logic for both POST /threads/messages and POST /threads/{id}/messages.

    Those routes pass a body already cleared of the server-only fields
    (:func:`_client_body`); /retry builds its request from the stored rows.

    ``retry_of_run_id`` is retry provenance and route-internal: only the
    /retry route passes it. Whatever the public body carried is overwritten
    — a forged value could chain a new attempt onto an arbitrary failed run.
    """
    from src.server.handlers.chat import astream_ptc_workflow
    from src.server.services.turn_runtime import TurnRoute, requested_workspace, turn_route

    # Canonical run_id generation site. Each POST gets a fresh UUID that
    # flows through every downstream key: BTM ``(tid, rid)``, persistence
    # service, ``workflow:stream:{tid}:{rid}``, LangGraph ``config["run_id"]``
    # → ``CheckpointMetadata.run_id``, and the SSE ``metadata`` event the
    # frontend sees as the first event of the stream. 1:1 with
    # ``conversation_response_id``.
    run_id = str(uuid4())

    user_id = auth.user_id
    is_byok = auth.is_byok
    workspace_id = request.workspace_id

    from src.server.services.runs.admission import RunScope

    # HTTP-window owner of the burst lease: every pre-generator failure and
    # dispatch-priming exit that doesn't hand the run to an executor releases
    # through this scope (idempotent). The generator carries its own scope
    # for the window it owns — same layering as before, one vocabulary.
    scope = RunScope(user_id=user_id, burst_slot_id=auth.burst_slot_id)

    try:
        # Retry provenance: force the route-supplied value over anything in
        # the public body (see docstring).
        if request.retry_of_run_id != retry_of_run_id:
            request = request.model_copy(update={"retry_of_run_id": retry_of_run_id})

        # Burst slot: server-stamped from the admission dependency; never
        # trust a client-sent value (see ChatRequest.burst_slot_id).
        if request.burst_slot_id != auth.burst_slot_id:
            request = request.model_copy(
                update={"burst_slot_id": auth.burst_slot_id}
            )

        # Idempotency: a request_key that already produced a run is a
        # retransmit — classify it before any durable work happens for
        # this copy (thread creation, fork truncation, steering).
        if request.request_key:
            await _reject_duplicate_request(
                thread_id,
                request.request_key,
                user_id,
                except_job_id=request.steer_job_id,
            )

        # 403 guard: require BYOK, OAuth, or platform access (tier >= 0).
        # All flags are pre-checked by enforce_chat_limit — no DB calls here.
        # A tier nobody could read is an outage, not a missing provider: telling
        # a paying user to set one up sends them to the setup wizard for it.
        from src.config.settings import HOST_MODE
        needs_plan = HOST_MODE == "platform" and not auth.is_byok and not auth.has_oauth
        if needs_plan and auth.access_tier is None:
            raise quota_service_unavailable()
        if needs_plan and auth.access_tier < 0:
            raise HTTPException(
                status_code=403,
                detail={
                    "message": "No provider configured. Set up an API key or connect via OAuth.",
                    "type": "no_provider",
                    "link": {"url": "/setup/method", "label": "Set up provider"},
                },
            )

        # Reject an unauthenticated background dispatch up front, before the
        # owner lookup, workspace/LLM resolution, and credit check below — a
        # request that will 403 anyway shouldn't do that work first. In oss mode
        # with no INTERNAL_SERVICE_TOKEN configured there is nothing to
        # authenticate against, so the self-dispatch is trusted; a configured
        # token is enforced in every mode. Past this check a dispatch is
        # always the server's own, so ``is_dispatch`` alone says so below.
        _req_token = (raw_request.headers.get("X-Service-Token", "") if raw_request else "")
        _svc_token = _get_service_token()
        is_internal = _is_service_request(raw_request)
        is_dispatch = bool(
            raw_request and raw_request.headers.get("X-Dispatch") == "background"
        )
        if is_dispatch and not is_internal:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Background dispatch requires internal service auth. "
                    "Configure INTERNAL_SERVICE_TOKEN and send it as "
                    "X-Service-Token."
                ),
            )

        # IDOR guard: an existing thread must belong to the caller. A brand-new
        # thread_id has no owner yet -> creation proceeds. The internal report-back
        # dispatch sets X-User-Id to the owner, so it passes. The same row also
        # gives the workspace when the request names none, and the thread's model.
        thread_meta = await get_thread_auth_meta(thread_id) if thread_id else None
        owner_id = thread_meta["user_id"] if thread_meta else None
        if owner_id is not None and owner_id != user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        # RA-3: this door serves the Super-agent only. A thread is live when its
        # surface is the Router or a Worker-class lane AND the turn is a
        # background dispatch; anything else would fall through to the retired
        # PTC chat graph, so it is refused before any durable work.
        #
        # The surface is authoritative from the stored row whenever the thread
        # exists: a client cannot relabel an ordinary thread by sending
        # ``platform``, and a row with no stored surface (NULL, or a row read
        # without the column) is a retired chat thread, never "whatever the
        # request says". Only a brand-new thread (no row yet) is classified by
        # the request, because that request is what will create the row.
        thread_platform = _authoritative_platform(thread_meta, request.platform)
        if not is_dispatch or thread_platform not in _LIVE_THREAD_PLATFORMS:
            raise _chat_retired()
        # Every later reader (role routing, Worker/Watchdog host and tools,
        # the runtime lane, the credit surface, the workflow itself) reads the
        # surface off the request. Pin it to the authoritative value so an
        # admitted turn that omitted ``platform`` (a report-back, a steer
        # replay) or named a different one still runs as the thread it is.
        if request.platform != thread_platform:
            if request.platform is not None:
                logger.warning(
                    f"[CONDUCTOR_CHAT] Request platform {request.platform!r} "
                    f"ignored for thread {thread_id}; stored surface is "
                    f"{thread_platform!r}"
                )
            request = request.model_copy(update={"platform": thread_platform})

        # A fork deletes rows from the turn it replaces, and that turn is the
        # one its checkpoint forks, not the number the client counted. Every
        # later reader (the truncation, the regenerate's turn, the prior-turn
        # read) takes it from the request, so it is settled here, once, with
        # the branch tip it was read off for START to hold it to.
        request = await _settle_against_branch(thread_id, request, user_id)

        # Resolve workspace_id from thread if not provided
        if not workspace_id and thread_meta:
            workspace_id = str(thread_meta["workspace_id"])
            logger.debug(
                f"[CHAT] Resolved workspace_id={workspace_id} from thread_id={thread_id}"
            )

        # Validate that agent_config is initialized
        if not hasattr(setup, "agent_config") or setup.agent_config is None:
            raise HTTPException(
                status_code=503,
                detail="PTC Agent not initialized. Check server startup logs.",
            )

        # A turn naming Home's id, or any turn on a thread in Home, runs in
        # Home. The workspace guard comes first: it pairs with the thread
        # guard above. Home is bound once the admission gates below pass.
        workspace = await requested_workspace(user_id, workspace_id)
        route = turn_route(user_id, workspace)
        if is_dispatch and request.platform == "superagent_router":
            # Internal only: is_dispatch passed service-token auth above. A
            # normal client can never select the restricted Router role.
            route = TurnRoute(route.workspace_id, "supervisor")
        elif is_dispatch and request.platform == "superagent_worker":
            # Workers get the work prompt and report tool, not the Router's
            # supervisor role.
            route = TurnRoute(route.workspace_id, "worker")
        workspace_id = route.workspace_id

        # Extract user input
        user_input = ""
        if request.messages:
            last_msg = request.messages[-1]
            if isinstance(last_msg.content, str):
                user_input = last_msg.content
            elif isinstance(last_msg.content, list):
                for item in last_msg.content:
                    if hasattr(item, "text") and item.text:
                        user_input = item.text
                        break

        logger.info(
            f"[CONDUCTOR_CHAT] New request: "
            f"workspace_id={workspace_id} thread_id={thread_id} user_id={user_id} "
            f"role={route.role}"
        )

        # Resolve LLM config eagerly — credit check must happen before SSE stream starts
        from src.server.services.llm import thread_model
        from src.server.services.llm.config import resolve_llm_config
        from src.server.dependencies.usage_limits import enforce_credit_limit
        from src.config.runtime.agent import CredentialSource

        # A turn naming no model runs the thread's own, the last one a client
        # named for it. Read here rather than in the resolver so automation
        # turns, which never pass through this route, keep their own model.
        requested_model = request.llm_model or None
        held_model = thread_meta.get("llm_model") if thread_meta else None
        named_model = (
            thread_model.NamedModel(requested_model, seen=held_model)
            if requested_model
            else None
        )

        # Likewise a turn naming no effort or fast mode runs the thread's own,
        # picked in its composer, and with none of those the user's Settings.
        # A client sends what its composer shows, so the row decides only for
        # a turn no composer started: a report-back, a resume after a reload.
        reasoning_effort = getattr(request, "reasoning_effort", None) or (
            thread_meta.get("reasoning_effort") if thread_meta else None
        )
        fast_mode = getattr(request, "fast_mode", None)
        if fast_mode is None and thread_meta:
            fast_mode = thread_meta.get("fast_mode")

        async def resolve(model: str | None):
            return await resolve_llm_config(
                setup.agent_config,
                user_id,
                model,
                is_byok,
                reasoning_effort=reasoning_effort,
                fast_mode=fast_mode,
                thread_id=thread_id,
                enabled_subagents=request.subagents_enabled,
                workspace_id=workspace_id,
                role=route.role,
            )

        # A report-back or a channel message has nobody at a client to
        # reconnect an account, and a refusal would drop it. A person's send
        # names no model while its client has not read the thread yet, and
        # gets the refusal rather than an answer from a model it never chose.
        from_service = service_token_matches(_req_token, _svc_token) or is_dispatch
        config = await thread_model.resolve_turn_config(
            user_id,
            thread_id,
            named=requested_model,
            held=held_model,
            fall_back=from_service,
            resolve=resolve,
        )

        # is_byok is True only when the stamped credential_source confirms the user
        # supplied their own key (OAUTH or BYOK), not merely that a client object exists.
        is_byok = config.credential_source in (CredentialSource.OAUTH, CredentialSource.BYOK)

        # Credit check: always ask, whoever's key this is. Which pools apply to
        # an own-key turn is the quota service's call, not ours, and so is what
        # the surface the turn came from means for it.
        await enforce_credit_limit(user_id, byok=is_byok, surface=request.platform)

        # I6: Redis down at START = 503 before any durable row. Ordered after
        # the auth/authz/credit gates (their statuses are more meaningful and
        # leak nothing about infra), before anything durable happens. Without
        # the transport the run's first buffered event would kill it as
        # failed(transport_lost) anyway — refuse cheaply instead.
        await _assert_stream_transport_ready()
        await route.bind(user_id, workspace)


    except BaseException:
        await scope.release_slot()
        raise

    # Resolve model name for observability labels (bounded by models.json keys).
    _model = getattr(getattr(config, "llm", None), "name", None) or ""

    extra_configurable = None
    extra_tools = None
    if is_dispatch and request.platform == "superagent_watchdog":
        from src.server.services.autonomy.host import AutonomyToolHost
        from src.tools.autonomy import AUTONOMY_WATCHDOG_TOOLS

        extra_configurable = {
            **(extra_configurable or {}),
            "autonomy_host": AutonomyToolHost(
                user_id=user_id, thread_id=thread_id
            ),
        }
        extra_tools = AUTONOMY_WATCHDOG_TOOLS
    elif route.role == "supervisor":
        from src.server.services.autonomy.host import AutonomyToolHost
        from src.tools.autonomy import AUTONOMY_ROUTER_TOOLS

        extra_configurable = {
            **(extra_configurable or {}),
            "autonomy_host": AutonomyToolHost(
                user_id=user_id, thread_id=thread_id
            ),
        }
        extra_tools = AUTONOMY_ROUTER_TOOLS
    elif is_dispatch and request.platform == "superagent_worker" and thread_id:
        from src.server.database.autonomy.tickets import get_ticket_by_worker_thread
        from src.server.services.autonomy.host import AutonomyWorkerHost
        from src.tools.autonomy import AUTONOMY_WORKER_TOOLS

        ticket = await get_ticket_by_worker_thread(thread_id)
        if ticket is not None:
            extra_configurable = {
                **(extra_configurable or {}),
                "autonomy_host": AutonomyWorkerHost(
                    ticket_id=str(ticket["ticket_id"]),
                    thread_id=thread_id,
                    user_id=str(ticket["user_id"]),
                ),
            }
            extra_tools = AUTONOMY_WORKER_TOOLS
    # R8c: the per-role runtime switch, read once per admission. Workers and
    # ordinary turns never get a lane; the PTC kwargs above stay unchanged.
    from src.server.services.superagent_runtime.selection import superagent_lane_for

    superagent_lane = superagent_lane_for(
        platform=request.platform,
        route_role=route.role,
        is_dispatch=is_dispatch,
        host=(extra_configurable or {}).get("autonomy_host"),
        tools=extra_tools,
    )
    gen = astream_ptc_workflow(
        request=request,
        thread_id=thread_id,
        run_id=run_id,
        user_input=user_input,
        user_id=user_id,
        route=route,
        is_byok=is_byok,
        config=config,
        dispatched=is_dispatch,
        named_model=named_model,
        extra_configurable=extra_configurable,
        extra_tools=extra_tools,
        superagent_lane=superagent_lane,
        # START-row stamp for audit and rollback checks (R8c 4.6).
        run_metadata=(
            {"superagent_runtime": "p"} if superagent_lane is not None else None
        ),
    )

    return await _prime_dispatch(
        gen,
        scope,
        request,
        thread_id=thread_id,
        run_id=run_id,
        user_id=user_id,
        workspace_id=workspace_id,
        model=_model,
    )


async def _prime_dispatch(
    gen,
    scope,
    request: ChatRequest,
    *,
    thread_id: str,
    run_id: str,
    user_id: str,
    workspace_id: str,
    model: str,
) -> JSONResponse:
    """Drive a background dispatch's turn to its START, answer with the run
    and the workspace the turn resolved to, then drain ``gen`` in the
    background.

    Every exit that does not hand ``gen`` to that drain releases ``scope``'s
    slot.
    """
    from src.server.database.runs.lifecycle import DuplicateRequestError
    from src.server.handlers.chat.request_prep import (
        DISPATCH_STARTED_MARKER,
        DISPATCH_STEERED_MARKER,
    )
    async def steered(target_run_id: str) -> JSONResponse:
        # The hand-off went into a running turn as a steer: this request's
        # run never starts, so its pre-START stamp and slot go.
        await scope.release_slot()
        logger.info(
            f"[CONDUCTOR_DISPATCH] Hand-off steered into running run {target_run_id} "
            f"on thread {thread_id}"
        )
        return JSONResponse({
            "status": "steered",
            "thread_id": thread_id,
            "run_id": target_run_id,
            "workspace_id": workspace_id,
        })

    # Durable receipt (v4 2.4c): drive the generator through its own
    # admission (per-thread lock, dedup, wait_or_steer, START txn) to the
    # post-START marker; the dispatched 200 is returned only once the
    # in_progress row is committed.
    try:
        first = await anext(gen)
    except DuplicateRequestError as dup:
        existing = str(dup.existing_run["conversation_response_id"])
        if dup.steered:
            return await steered(existing)
        await scope.release_slot()
        logger.info(
            f"[CONDUCTOR_DISPATCH] Retransmit adopted existing run {existing} "
            f"for thread {thread_id}"
        )
        return JSONResponse({
            "status": "dispatched",
            "thread_id": thread_id,
            "run_id": existing,
            "workspace_id": workspace_id,
        })
    except HTTPException:
        await scope.release_slot()
        raise
    except Exception as e:
        await scope.release_slot()
        raise HTTPException(
            status_code=503,
            detail=f"Dispatch could not start durably: {e}",
        )
    if isinstance(first, str) and first.startswith(DISPATCH_STEERED_MARKER):
        await gen.aclose()
        return await steered(first[len(DISPATCH_STEERED_MARKER):])
    if first != DISPATCH_STARTED_MARKER:
        await gen.aclose()
        await scope.release_slot()
        raise HTTPException(
            status_code=500,
            detail="Dispatch priming yielded an unexpected event.",
        )

    _track_task(asyncio.create_task(
        observe_background_chat_turn(
            _consume_background_gen(gen, "CONDUCTOR_DISPATCH", thread_id, run_id),
            model=model,
            user_id=user_id,
            workspace_id=workspace_id,
            thread_id=thread_id,
        ),
        name=f"ptc-dispatch-{thread_id}-{run_id[:8]}",
    ))
    logger.info(
        "[CONDUCTOR_DISPATCH] Started background workflow: "
        f"thread_id={thread_id} run_id={run_id} workspace_id={workspace_id}"
    )
    return JSONResponse({
        "status": "dispatched",
        "thread_id": thread_id,
        "run_id": run_id,
        "workspace_id": workspace_id,
    })






@router.get("/{thread_id}/messages/replay")
async def replay_thread_messages(
    thread_id: str,
    x_user_id: CurrentUserId,
    source: Literal["auto", "checkpoint"] = Query(
        "auto",
        description=(
            "Replay source: 'checkpoint' projects the transcript from LangGraph "
            "checkpoints and answers 409 when they cannot cover it; 'auto' "
            "replays a turn it cannot project as its user message and closing "
            "rows, and every turn that way when the thread's coverage is "
            "incomplete."
        ),
    ),
    limit: int | None = Query(
        None,
        ge=1,
        description=(
            "Paged replay: only the most recent N turns (before "
            "``before_turn`` when given), plus a leading ``history_page`` "
            "event naming the oldest turn sent and whether older ones remain."
        ),
    ),
    before_turn: int | None = Query(
        None,
        ge=0,
        description=(
            "Paged replay: only turns older than this turn_index, the "
            "previous page's ``history_page.first_turn_index``."
        ),
    ),
):
    """Replay a thread as SSE.

    Stream includes:
    - history_page: first, only when ``limit`` or ``before_turn`` is given
    - user_message: emitted once per turn_index (query content)
    - message_chunk/tool_* events: projected from checkpoints; a turn they
      cannot cover sends only its closing rows (``source=auto``)
    - snapshot: cursors for the runs replay could not project (newest page only)
    - replay_done: terminal sentinel; on the newest page it also names the
      thread's newest run (``latest_run_id``), read before the transcript
    """
    try:
        rows = await get_replay_thread_data(thread_id)

        # Preserve existing 404/403 semantics from require_thread_owner
        if rows is None:
            raise HTTPException(status_code=404, detail="Thread not found")
        if rows.owner_id != x_user_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        from src.server.services.history.replay import (
            CheckpointReplayUnavailable,
            finish_lines,
            read_replay_page,
        )

        try:
            page, resolved_source = await read_replay_page(
                rows, source=source, before_turn=before_turn, limit=limit
            )
        except CheckpointReplayUnavailable as e:
            raise HTTPException(
                status_code=409,
                detail=f"Checkpoint replay unavailable: {e}",
            )

        # After source selection and outside the stored lines: task artifacts
        # replay their spawn-time payload, so the card's status is stamped
        # here from current liveness (see history/task_status.py).
        replay_lines = page.lines
        await finish_lines(thread_id, replay_lines)
        paged = limit is not None or before_turn is not None

        async def event_generator():
            seq = 0
            if paged:
                seq += 1
                history_page = {
                    "first_turn_index": page.first_turn_index,
                    "has_more": page.has_more,
                }
                yield (
                    f"id: {seq}\n"
                    f"event: history_page\n"
                    f"data: {json.dumps(history_page, default=str)}\n\n"
                )
            for line in replay_lines:
                seq += 1
                yield f"id: {seq}\nevent: {line.event}\ndata: {line.data_json}\n\n"

            # Cursors for the runs replay could not project — an in-flight run
            # belongs to its stream, so the snapshot hands the client where to
            # resume instead of its content. Additive: a snapshot outage (None)
            # just omits the frame, and v1 clients ignore the unknown event.
            # An older page holds no in-flight run.
            if before_turn is None:
                from src.server.services.history.snapshot import (
                    build_thread_snapshot,
                )

                snapshot = await build_thread_snapshot(thread_id)
                if snapshot is not None:
                    seq += 1
                    yield (
                        f"id: {seq}\n"
                        f"event: snapshot\n"
                        f"data: {json.dumps(snapshot, ensure_ascii=False, default=str)}\n\n"
                    )

            # The newest page names the thread's newest run as of this read,
            # which a fork from this view sends back as expected_latest_run_id.
            done: dict[str, object] = {"thread_id": thread_id}
            if before_turn is None:
                latest_run_id = rows.thread.get("latest_run_id")
                done["latest_run_id"] = (
                    str(latest_run_id) if latest_run_id is not None else None
                )
            seq += 1
            yield f"id: {seq}\nevent: replay_done\ndata: {json.dumps(done, default=str)}\n\n"

        return StreamingResponse(
            observe_replay_stream(event_generator(), source="private"),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Replay-Source": resolved_source,
            },
        )

    except DATABASE_UNAVAILABLE as e:
        # Replay ends the read on a database fault rather than show the
        # thread as stubs.
        raise database_unavailable(f"replaying thread {thread_id}", logger, e)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error replaying thread {thread_id}: {e}")
        raise HTTPException(
            status_code=500, detail=f"Failed to replay thread: {str(e)}"
        )


# =============================================================================
# THREAD CONTROL (was "workflow")
# =============================================================================




# Upper bound on ids per liveness request — one MGET stays cheap. Ids past the
# cap are dropped for that request and stay unresolved on the client (there is
# no per-card fallback); >100 concurrently-unresolved cards would need the
# frontend to chunk requests.
_MAX_LIVENESS_IDS = 100




@router.post("/{thread_id}/cancel", status_code=200)
async def cancel_thread(
    thread_id: str,
    x_user_id: CurrentUserId,
    run_id: Optional[str] = Query(None),
):
    """Cancel a running workflow for this thread.

    ``run_id`` targets a specific run so a retried stop can't cancel a newer
    turn started after the stopped one ended (defaults to latest active run).
    """
    await auth_api.require_thread_owner(thread_id, x_user_id)
    from src.server.services.cancel_dispatch import cancel_workflow

    return await cancel_workflow(thread_id, run_id)












