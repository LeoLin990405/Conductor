"""Background subagent execution middleware.

This module provides async/background execution for subagent tasks,
allowing the main agent to continue working while subagents run.
"""

from ptc_agent.agent.middleware.task_runtime.event_capture import (
    SubagentEventCaptureMiddleware,
    SubagentToolResultCaptureMiddleware,
)
from ptc_agent.agent.middleware.task_runtime.middleware import (
    BackgroundSubagentMiddleware,
    current_background_agent_id,
    current_background_tool_call_id,
)
from ptc_agent.agent.middleware.task_runtime.orchestrator import (
    BackgroundSubagentOrchestrator,
)
from ptc_agent.agent.middleware.task_runtime.registry import (
    BackgroundTask,
    BackgroundTaskRegistry,
)
from ptc_agent.agent.middleware.task_runtime.steering import (
    SubagentSteeringMiddleware,
)
from ptc_agent.agent.middleware.task_runtime.tools import (
    create_task_output_tool,
)

__all__ = [
    "BackgroundSubagentMiddleware",
    "BackgroundSubagentOrchestrator",
    "BackgroundTask",
    "BackgroundTaskRegistry",
    "SubagentSteeringMiddleware",
    "SubagentEventCaptureMiddleware",
    "SubagentToolResultCaptureMiddleware",
    "create_task_output_tool",
    "current_background_agent_id",
    "current_background_tool_call_id",
]
