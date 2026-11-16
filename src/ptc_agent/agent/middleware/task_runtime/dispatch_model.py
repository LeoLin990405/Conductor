"""Per-dispatch model override for subagents.

The Task tool lets the parent run one child on a different manifest model than
the type's pinned/default one. Recompiling the child graph per dispatch would
spend a compile (and lose its prompt-cache key), so the resolved client rides a
ContextVar and this middleware substitutes it at each model call of that run.
The client is resolved by the caller before the graph starts, so no DB/OAuth
work happens inside the model call itself.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse

# The client a parent chose for the dispatch currently running, or None when the
# child runs on the model its graph was compiled with.
current_dispatch_model: contextvars.ContextVar[Any | None] = contextvars.ContextVar(
    "current_dispatch_model", default=None
)


@contextlib.contextmanager
def dispatch_model(client: Any | None):
    """Scope one dispatch's model to the subagent run it starts.

    The Task tool opens this around the child invocation; a sibling dispatch and
    the parent turn keep whatever they had.
    """
    token = current_dispatch_model.set(client)
    try:
        yield
    finally:
        current_dispatch_model.reset(token)


class DispatchModelMiddleware(AgentMiddleware):
    """Replace the child graph's model with the dispatch's resolved client."""

    def _apply(self, request: ModelRequest) -> ModelRequest | None:
        client = current_dispatch_model.get()
        if client is None or request.model is client:
            return None
        return request.override(model=client)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        return handler(self._apply(request) or request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        return await handler(self._apply(request) or request)
