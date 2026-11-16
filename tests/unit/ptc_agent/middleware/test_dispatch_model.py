"""Per-dispatch Task model override: context, middleware substitution, refusals."""

from unittest.mock import AsyncMock

import pytest

from ptc_agent.agent.middleware.task_runtime.dispatch_model import (
    DispatchModelMiddleware,
    current_dispatch_model,
    dispatch_model,
)
from ptc_agent.agent.middleware.task_runtime.subagent import (
    resolve_dispatch_model,
)


class _Request:
    def __init__(self, model):
        self.model = model

    def override(self, **changes):
        return _Request(changes.get("model", self.model))


def _middleware_call(request, handler):
    return DispatchModelMiddleware().wrap_model_call(request, handler)


def test_no_override_keeps_the_graph_model():
    request = _Request("compiled-model")
    seen = {}
    _middleware_call(request, lambda req: seen.setdefault("model", req.model))
    assert seen["model"] == "compiled-model"


def test_an_active_dispatch_substitutes_its_client():
    request = _Request("compiled-model")
    seen = {}
    with dispatch_model("resolved-client"):
        _middleware_call(request, lambda req: seen.setdefault("model", req.model))
    assert seen["model"] == "resolved-client"
    # The scope ends with the dispatch; a sibling/parent keeps the old model.
    _middleware_call(request, lambda req: seen.setdefault("after", req.model))
    assert seen["after"] == "compiled-model"
    assert current_dispatch_model.get() is None


def test_nested_dispatch_scopes_restore_the_outer_client():
    seen = []
    with dispatch_model("outer"):
        with dispatch_model("inner"):
            _middleware_call(_Request("compiled"), lambda req: seen.append(req.model))
        _middleware_call(_Request("compiled"), lambda req: seen.append(req.model))
    assert seen == ["inner", "outer"]


@pytest.mark.asyncio
async def test_an_unknown_model_is_refused_without_a_resolver_call():
    resolver = AsyncMock()
    client, error = await resolve_dispatch_model("not-a-model", resolver)
    assert client is None and "unknown model" in error
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_resolver_refuses_a_known_model():
    from src.llms.llm import LLM

    known = LLM.get_model_config().get_model_config("deepseek-flash")
    assert known is not None
    client, error = await resolve_dispatch_model("deepseek-flash", None)
    assert client is None and "not available in this build" in error


@pytest.mark.asyncio
async def test_a_credential_refusal_is_reported_verbatim():
    resolver = AsyncMock(side_effect=RuntimeError("connect an account first"))
    client, error = await resolve_dispatch_model("deepseek-flash", resolver)
    assert client is None
    assert "connect an account first" in error
    resolver.assert_awaited_once_with("deepseek-flash")


@pytest.mark.asyncio
async def test_a_resolved_client_is_returned():
    resolver = AsyncMock(return_value="client")
    client, error = await resolve_dispatch_model("deepseek-flash", resolver)
    assert client == "client" and error is None


def test_the_task_tool_exposes_the_model_argument():
    from ptc_agent.agent.middleware.task_runtime.subagent import _create_task_tool

    tool = _create_task_tool(subagent_graphs={}, model_resolver=None)
    assert "model" in tool.args
    assert tool.args["model"]["default"] is None
