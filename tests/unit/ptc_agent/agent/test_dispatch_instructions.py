"""Per-dispatch role instructions: Task field, ContextVar, middleware."""

from langchain_core.messages import SystemMessage

from ptc_agent.agent.middleware.task_runtime.context import (
    current_dispatch_instructions,
    dispatch_instructions,
)
from ptc_agent.agent.middleware.task_runtime.dispatch_instructions import (
    DispatchInstructionsMiddleware,
)
from ptc_agent.agent.middleware.task_runtime.subagent import _create_task_tool


class _Request:
    def __init__(self, system_message):
        self.system_message = system_message

    def override(self, *, system_message):
        return _Request(system_message)


def _text(request) -> str:
    blocks = request.system_message.content_blocks
    return "".join(b.get("text", "") for b in blocks if isinstance(b, dict))


def test_the_task_tool_schema_offers_instructions():
    tool = _create_task_tool(subagent_graphs={})
    assert "instructions" in tool.args


def test_dispatch_instructions_scope_is_isolated():
    assert current_dispatch_instructions.get() is None
    with dispatch_instructions("  以审计员身份工作  "):
        assert current_dispatch_instructions.get() == "以审计员身份工作"
        with dispatch_instructions(None):
            assert current_dispatch_instructions.get() is None
        assert current_dispatch_instructions.get() == "以审计员身份工作"
    assert current_dispatch_instructions.get() is None


def test_the_middleware_prepends_the_dispatch_brief():
    seen = {}

    def handler(request):
        seen["request"] = request
        return "ok"

    middleware = DispatchInstructionsMiddleware()
    base = _Request(SystemMessage(content="BASE ROLE"))
    with dispatch_instructions("本次以并购律师视角逐条审阅条款。"):
        middleware.wrap_model_call(base, handler)
    text = _text(seen["request"])
    assert text.startswith("BASE ROLE")
    assert "本次委派的角色与指令" in text
    assert "本次以并购律师视角逐条审阅条款。" in text


def test_the_middleware_is_a_no_op_without_a_brief():
    seen = {}

    def handler(request):
        seen["request"] = request
        return "ok"

    base = _Request(SystemMessage(content="BASE ROLE"))
    DispatchInstructionsMiddleware().wrap_model_call(base, handler)
    assert seen["request"] is base
