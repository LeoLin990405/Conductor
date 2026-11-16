"""In-history system-prompt updates for DeepSeek-V4.1-Flash."""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from ptc_agent.agent.middleware.deepseek_in_history import (
    DeepSeekInHistorySystemPromptMiddleware,
)


class _Model:
    def __init__(self, model="deepseek-flash", route="deepseek"):
        self.model = model
        self.metadata = {"provider_route": route}


class _Runtime:
    def __init__(self, thread_id="thread-1"):
        self.config = {"configurable": {"thread_id": thread_id}}


class _Request:
    def __init__(self, messages, system, *, model=None, thread_id="thread-1"):
        self.messages = messages
        self.system_message = SystemMessage(content=system) if isinstance(system, str) else system
        self.model = model or _Model()
        self.runtime = _Runtime(thread_id)

    def override(self, *, system_message, messages):
        req = _Request(messages, system_message, model=self.model)
        return req


def test_the_first_system_prompt_is_the_epoch():
    middleware = DeepSeekInHistorySystemPromptMiddleware()
    req = _Request([HumanMessage("hi")], "SYSTEM ONE")
    assert middleware.wrap_model_call(req, lambda r: r) is req


def test_a_changed_system_prompt_is_appended_in_place():
    middleware = DeepSeekInHistorySystemPromptMiddleware()
    first = _Request([HumanMessage("hi")], "SYSTEM ONE")
    middleware.wrap_model_call(first, lambda r: r)

    second = _Request(
        [HumanMessage("hi"), AIMessage("hello"), HumanMessage("new turn")],
        "SYSTEM TWO",
    )
    seen = {}

    def handler(request):
        seen["request"] = request

    middleware.wrap_model_call(second, handler)
    req = seen["request"]
    assert "".join(
        b.get("text", "") for b in req.system_message.content_blocks if isinstance(b, dict)
    ) == "SYSTEM ONE"
    assert isinstance(req.messages[-1], SystemMessage)
    assert "SYSTEM TWO" in "".join(
        b.get("text", "") for b in req.messages[-1].content_blocks if isinstance(b, dict)
    )


def test_an_unchanged_prompt_is_a_no_op():
    middleware = DeepSeekInHistorySystemPromptMiddleware()
    middleware.wrap_model_call(_Request([HumanMessage("hi")], "SYSTEM ONE"), lambda r: r)
    same = _Request([HumanMessage("hi"), HumanMessage("again")], "SYSTEM ONE")
    assert middleware.wrap_model_call(same, lambda r: r) is same


def test_other_models_or_routes_are_untouched():
    middleware = DeepSeekInHistorySystemPromptMiddleware()
    middleware.wrap_model_call(_Request([HumanMessage("hi")], "A"), lambda r: r)
    request = _Request(
        [HumanMessage("hi"), HumanMessage("again")],
        "B",
        model=_Model(model="deepseek-v4-pro"),
    )
    assert middleware.wrap_model_call(request, lambda r: r) is request
