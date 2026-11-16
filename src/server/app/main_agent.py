"""Authenticated REST surface for durable main-agent runs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.server.services.main_agent.models import (
    ConcurrentUpdate,
    CoordinatorBusy,
    InvalidPlan,
    RunNotFound,
    TaskOwnershipError,
)
from src.server.utils.api import CurrentUserId

router = APIRouter(prefix="/api/v1/main-agent", tags=["Main agent"])


class SubmitRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=20_000)
    context: dict[str, Any] = Field(default_factory=dict)


class ClarificationRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)


class ClaimRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    daemon_id: str | None = Field(default=None, max_length=200)


class HeartbeatRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    claim_token: str = Field(min_length=1, max_length=200)
    current_step: str | None = Field(default=None, max_length=1000)


class CompleteRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    claim_token: str = Field(min_length=1, max_length=200)
    result: dict[str, Any] = Field(default_factory=dict)


class FailRequest(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    claim_token: str = Field(min_length=1, max_length=200)
    error: dict[str, Any]
    retryable: bool = False


def _orchestrator(request: Request):
    orchestrator = getattr(request.app.state, "main_agent_orchestrator", None)
    if orchestrator is None:
        raise HTTPException(status_code=503, detail="Main-agent service is unavailable")
    return orchestrator


def _translate(exc: Exception) -> HTTPException:
    if isinstance(exc, RunNotFound):
        return HTTPException(status_code=404, detail="Run or task not found")
    if isinstance(exc, CoordinatorBusy):
        return HTTPException(
            status_code=409,
            detail={
                "code": "coordinator_busy",
                "message": str(exc),
            },
        )
    if isinstance(exc, (ConcurrentUpdate, TaskOwnershipError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (InvalidPlan, ValueError)):
        return HTTPException(status_code=422, detail=str(exc))
    raise exc


@router.get("/status-board")
async def status_board(user_id: CurrentUserId, orchestrator=Depends(_orchestrator)):
    return await orchestrator.get_status_board(user_id)


@router.post("/runs")
async def submit_run(
    body: SubmitRequest, user_id: CurrentUserId, orchestrator=Depends(_orchestrator)
):
    try:
        return await orchestrator.submit(
            user_id, {"objective": body.goal, "context": body.context}
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str, user_id: CurrentUserId, orchestrator=Depends(_orchestrator)
):
    try:
        return await orchestrator.get(run_id, user_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/runs/{run_id}/clarification")
async def answer_clarification(
    run_id: str,
    body: ClarificationRequest,
    user_id: CurrentUserId,
    orchestrator=Depends(_orchestrator),
):
    try:
        return await orchestrator.answer_clarification(
            run_id, user_id, {"answer": body.answer}
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/runs/{run_id}/advance")
async def advance_run(
    run_id: str, user_id: CurrentUserId, orchestrator=Depends(_orchestrator)
):
    try:
        return await orchestrator.advance(run_id, user_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/runs/{run_id}/tick")
async def tick_run(
    run_id: str, user_id: CurrentUserId, orchestrator=Depends(_orchestrator)
):
    try:
        return await orchestrator.tick(run_id, user_id)
    except Exception as exc:
        raise _translate(exc) from exc


@router.get("/runs/{run_id}/tasks/ready")
async def ready_tasks(
    run_id: str, user_id: CurrentUserId, orchestrator=Depends(_orchestrator)
):
    try:
        return {"tasks": await orchestrator.ready_tasks(run_id, user_id)}
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/runs/{run_id}/tasks/{task_id}/claim")
async def claim_task(
    run_id: str,
    task_id: str,
    body: ClaimRequest,
    user_id: CurrentUserId,
    orchestrator=Depends(_orchestrator),
):
    try:
        return await orchestrator.claim_task(
            run_id, user_id, task_id, body.agent_id, daemon_id=body.daemon_id
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/runs/{run_id}/tasks/{task_id}/heartbeat")
async def heartbeat_task(
    run_id: str,
    task_id: str,
    body: HeartbeatRequest,
    user_id: CurrentUserId,
    orchestrator=Depends(_orchestrator),
):
    try:
        return await orchestrator.heartbeat_task(
            run_id,
            user_id,
            task_id,
            body.agent_id,
            current_step=body.current_step,
            claim_token=body.claim_token,
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/runs/{run_id}/tasks/{task_id}/complete")
async def complete_task(
    run_id: str,
    task_id: str,
    body: CompleteRequest,
    user_id: CurrentUserId,
    orchestrator=Depends(_orchestrator),
):
    try:
        return await orchestrator.complete_task(
            run_id,
            user_id,
            task_id,
            body.agent_id,
            body.result,
            claim_token=body.claim_token,
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/runs/{run_id}/tasks/{task_id}/fail")
async def fail_task(
    run_id: str,
    task_id: str,
    body: FailRequest,
    user_id: CurrentUserId,
    orchestrator=Depends(_orchestrator),
):
    try:
        return await orchestrator.fail_task(
            run_id,
            user_id,
            task_id,
            body.agent_id,
            body.error,
            retryable=body.retryable,
            claim_token=body.claim_token,
        )
    except Exception as exc:
        raise _translate(exc) from exc


@router.post("/runs/{run_id}/tasks/{task_id}/retry")
async def retry_task(
    run_id: str,
    task_id: str,
    user_id: CurrentUserId,
    orchestrator=Depends(_orchestrator),
):
    try:
        return await orchestrator.retry_task(run_id, user_id, task_id)
    except Exception as exc:
        raise _translate(exc) from exc
