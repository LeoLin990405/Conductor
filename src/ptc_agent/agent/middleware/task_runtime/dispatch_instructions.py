"""Per-dispatch role instructions for subagents.

The compiled subagent graph carries the role's default system prompt. A parent
that dispatches a task with ``instructions`` (the Task tool's optional field)
means that text to shape this run, not every run of that type. Recompiling the
graph per dispatch would be wasteful and would lose its prompt-cache key, so the
text rides a ContextVar and this middleware prepends it at each model call.
The default role stays below it as scaffolding; the dispatch text is stated to
win where the two describe the same thing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse

from ptc_agent.agent.middleware._utils import append_to_system_message
from ptc_agent.agent.middleware.task_runtime.context import (
    current_dispatch_instructions,
)

_DISPATCH_HEADER = (
    "# 本次委派的角色与指令\n\n"
    "以下内容由发起委派的父 Agent 针对本次任务给出，描述你这个子 Agent 本次承担的角色、"
    "工作方法和输出要求；与默认角色说明冲突时以这里为准。\n\n"
)


class DispatchInstructionsMiddleware(AgentMiddleware):
    """Prepend the Task call's per-dispatch instructions to the system prompt."""

    def _system_message(self, request: ModelRequest):
        text = current_dispatch_instructions.get()
        if not text:
            return request.system_message
        return append_to_system_message(request.system_message, _DISPATCH_HEADER + text)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        message = self._system_message(request)
        if message is request.system_message:
            return handler(request)
        return handler(request.override(system_message=message))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        message = self._system_message(request)
        if message is request.system_message:
            return await handler(request)
        return await handler(request.override(system_message=message))
