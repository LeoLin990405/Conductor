"""Read-only native agent capability inventory.

This endpoint reports what the backend host can actually run. It deliberately
does not report credentials as healthy: an installed CLI may still be signed
out, and the vendor decides that at call time. Thread creation and run routing
remain separate concerns until the native runtime is wired into the chat lane.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.server.database.conversation.native_runtime import list_runtime_decisions
from src.server.services.native_runtime import list_statuses
from src.server.services.native_runtime.login import (
    NativeLoginError,
    auth_status,
    login_manager,
)
from src.server.services.native_runtime.router import RuntimeRequest, route_runtime
from src.server.services.native_runtime.usage_headroom import load_usage_headroom
from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1", tags=["native-agents"])


class NativeRuntimeStatusResponse(BaseModel):
    provider: str
    installed: bool
    binary: str | None
    live_verified: bool
    capabilities: dict[str, bool]
    error: str | None = None


class NativeRuntimeListResponse(BaseModel):
    runtimes: list[NativeRuntimeStatusResponse]


@router.get("/native-agents", response_model=NativeRuntimeListResponse)
async def get_native_agents(x_user_id: CurrentUserId):
    """List vendor-native agent CLIs and the capability contract of each.

    ``installed`` only says the binary was found. ``live_verified`` says this
    provider has a recorded live proof; both an installed binary and
    ``live_verified`` are required before a native thread may be offered in a
    future UI. Authentication is intentionally not probed here.
    """
    return NativeRuntimeListResponse(
        runtimes=[
            NativeRuntimeStatusResponse(**status.as_dict())
            for status in list_statuses()
        ]
    )


class RuntimeRouteRequest(BaseModel):
    task: str = Field(default="", max_length=4000)
    required_capabilities: list[str] = Field(default_factory=list)
    preferred_provider: str | None = None
    allow_ptc: bool = True


class RuntimeRouteResponse(BaseModel):
    runtime: str
    reason: str
    fallbacks: list[str]
    confidence: float
    scores: dict[str, float]


@router.post("/native-agents/route", response_model=RuntimeRouteResponse)
async def route_native_agent(request: RuntimeRouteRequest, x_user_id: CurrentUserId):
    """Recommend a runtime for a new thread without starting one.

    This is the read-only Phase 1 router: capability gates + task affinity +
    installed/live filtering. It never mutates a thread. A native binding is
    immutable once created, so the caller passes the decision to thread
    creation; a live thread cannot switch providers mid-turn.
    """
    headroom = await load_usage_headroom()
    decision = route_runtime(
        list_statuses(),
        RuntimeRequest(
            task=request.task,
            required_capabilities=tuple(request.required_capabilities),
            preferred_provider=request.preferred_provider,
            allow_ptc=request.allow_ptc,
        ),
        headroom=headroom,
    )
    return RuntimeRouteResponse(**decision.as_dict())


class RuntimeDecisionRow(BaseModel):
    thread_id: str
    title: str | None = None
    decision: dict[str, Any]
    native_provider: str | None = None
    created_at: datetime


class RuntimeDecisionListResponse(BaseModel):
    decisions: list[RuntimeDecisionRow]


@router.get(
    "/native-agents/routing/decisions",
    response_model=RuntimeDecisionListResponse,
)
async def routing_decisions(
    x_user_id: CurrentUserId, limit: int = Query(20, ge=1, le=100)
):
    """Recent auto-routing decisions for this user, newest first.

    Read-only telemetry: the immutable binding stays on the thread; this only
    exposes the persisted decision/reason snapshot for evaluation.
    """
    rows = await list_runtime_decisions(x_user_id, limit)
    return RuntimeDecisionListResponse(
        decisions=[RuntimeDecisionRow(**row) for row in rows]
    )

class NativeAuthStatusResponse(BaseModel):
    provider: str
    supported: bool
    installed: bool
    authenticated: bool | None = None


class NativeAuthListResponse(BaseModel):
    providers: list[NativeAuthStatusResponse]


@router.get("/native-agents/auth", response_model=NativeAuthListResponse)
async def native_agent_auth(x_user_id: CurrentUserId):
    """Official CLI auth status for providers with a login flow.

    This asks each vendor's own CLI; Conductor never reads, refreshes or stores
    a vendor token itself.
    """
    providers = await asyncio.gather(
        *(
            asyncio.to_thread(auth_status, provider)
            for provider in login_manager.providers()
        )
    )
    return NativeAuthListResponse(
        providers=[NativeAuthStatusResponse(**row) for row in providers]
    )


class NativeLoginSessionResponse(BaseModel):
    session_id: str
    provider: str
    status: str
    auth_url: str | None = None
    requires_input: bool = False
    instruction: str = ""
    error: str | None = None
    created_at: float | None = None
    updated_at: float | None = None


@router.post(
    "/native-agents/{provider}/login",
    response_model=NativeLoginSessionResponse,
)
async def start_native_agent_login(provider: str, x_user_id: CurrentUserId):
    """Start the vendor CLI's own login flow for ``provider``."""
    try:
        session = await asyncio.to_thread(login_manager.start, provider)
    except NativeLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return NativeLoginSessionResponse(**session)


@router.get(
    "/native-agents/login/{session_id}",
    response_model=NativeLoginSessionResponse,
)
async def get_native_agent_login(session_id: str, x_user_id: CurrentUserId):
    try:
        return NativeLoginSessionResponse(**login_manager.get(session_id))
    except NativeLoginError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class NativeLoginInputRequest(BaseModel):
    code: str = Field(min_length=1, max_length=4096)


@router.post(
    "/native-agents/login/{session_id}/input",
    response_model=NativeLoginSessionResponse,
)
async def submit_native_agent_login_code(
    session_id: str, request: NativeLoginInputRequest, x_user_id: CurrentUserId
):
    """Send a pasted authorization code to the waiting official CLI."""
    try:
        return NativeLoginSessionResponse(
            **login_manager.submit_code(session_id, request.code)
        )
    except NativeLoginError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete(
    "/native-agents/login/{session_id}",
    response_model=NativeLoginSessionResponse,
)
async def cancel_native_agent_login(session_id: str, x_user_id: CurrentUserId):
    try:
        return NativeLoginSessionResponse(**login_manager.cancel(session_id))
    except NativeLoginError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
