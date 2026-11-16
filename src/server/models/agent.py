"""Wire shapes for the local agent fleet dashboard.

Everything here is derived from a read-only scan of the operator's own machine:
processes, worktrees and log files the agent CLIs already wrote. No field
carries a credential, a balance amount or an account identity — provider
headroom is a three-valued state, and log text arrives pre-redacted.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class AgentStatus(str, Enum):
    """Liveness of one agent, judged from its process and its log."""

    RUNNING = "running"
    STALLED = "stalled"
    ERROR = "error"
    DONE = "done"
    # The process is alive but nothing can be said about progress: no log file,
    # an unreadable one, a worktree git can no longer resolve, or a process
    # table the scan could not read at all.
    UNKNOWN = "unknown"


class AgentRole(str, Enum):
    """What the agent was asked to do, inferred from its prompt."""

    PLAN = "plan"
    DEV = "dev"
    REVIEW = "review"
    RESEARCH = "research"


BalanceState = Literal["ok", "low", "unknown"]


class Agent(BaseModel):
    """One agent in the fleet, as the dashboard renders it."""

    id: str = Field(description="Agent id: the worktree directory name")
    model: str = ""
    provider: str = ""
    role: AgentRole = AgentRole.DEV
    pid: Optional[int] = None
    worktree: str = ""
    branch: str = ""
    status: AgentStatus = AgentStatus.UNKNOWN
    elapsed_secs: Optional[int] = None
    last_activity_ts: Optional[datetime] = None
    log_file: str = Field(default="", description="Basename of the agent's log")
    log_tail: str = ""
    commits_ahead: Optional[int] = None
    tests_state: Optional[str] = None
    provider_balance: BalanceState = "unknown"


class AgentLogs(BaseModel):
    """A bounded, redacted slice of one agent's log."""

    agent_id: str
    log_file: str = ""
    lines: list[str] = Field(default_factory=list)
    truncated: bool = False


class AgentFleet(BaseModel):
    """The whole fleet plus the scan context the UI needs to read it."""

    agents: list[Agent] = Field(default_factory=list)
    scan_ts: datetime
    stall_threshold_mins: int
    # The scan is advisory: a process can start or a log can move between the
    # observation and the response, so a client treats a row as a sample.
    error: Optional[str] = None
