"""Wire shapes for project-level orchestration.

A project is a durable plan over one workspace. Tasks are dispatched to their
own agent thread so several can run at once; the scheduler owns the transition
from queued to running to terminal.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

TaskStatus = Literal[
    "queued",
    "dispatching",
    "running",
    "completed",
    "failed",
    "cancelled",
]
ProjectStatus = Literal["active", "paused", "completed", "cancelled", "archived"]


class ProjectCreate(BaseModel):
    workspace_id: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1, max_length=120)
    goal: str = Field("", max_length=8000)
    concurrency: int = Field(2, ge=1, le=8)
    auto_start: bool = True


class ProjectUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=120)
    goal: Optional[str] = Field(None, max_length=8000)
    concurrency: Optional[int] = Field(None, ge=1, le=8)


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=120)
    prompt: str = Field(..., min_length=1, max_length=20000)
    depends_on: list[str] = Field(default_factory=list)
    model: Optional[str] = None
    #: Run this task in a vendor-native agent CLI instead of the PTC graph.
    #: The value is a provider id from the native-agents inventory (codex,
    #: deepseek-dsh, kimi, ...); empty runs PTC.
    native_provider: Optional[str] = None


class TaskView(BaseModel):
    task_id: str
    project_id: str
    title: str
    prompt: str
    status: TaskStatus
    depends_on: list[str]
    model: Optional[str]
    native_provider: Optional[str]
    thread_id: Optional[str]
    run_id: Optional[str]
    attempts: int
    error: Optional[str]
    created_at: str
    started_at: Optional[str]
    completed_at: Optional[str]


class ProjectView(BaseModel):
    project_id: str
    workspace_id: str
    name: str
    goal: str
    status: ProjectStatus
    concurrency: int
    created_at: str
    updated_at: str
    tasks_total: int = 0
    tasks_queued: int = 0
    tasks_running: int = 0
    tasks_completed: int = 0
    tasks_failed: int = 0


class ProjectDetail(ProjectView):
    tasks: list[TaskView] = Field(default_factory=list)


class ProjectList(BaseModel):
    projects: list[ProjectView]
    truncated: bool = False
