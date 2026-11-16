"""Fork dispatch: a child that inherits the parent conversation on purpose."""

from langchain_core.messages import AIMessage, HumanMessage

from ptc_agent.agent.middleware.task_runtime.subagent import (
    build_subagent_state,
)


def _parent() -> dict:
    return {
        "messages": [
            HumanMessage(content="u1", id="u1"),
            AIMessage(content="a1", id="a1"),
        ],
        "todos": [{"content": "parent scratch"}],
        "structured_response": {"x": 1},
        "files": {"report.md": "data"},
    }


def test_fresh_dispatch_keeps_only_the_prompt_and_portable_keys():
    state = build_subagent_state(_parent(), "do the work")

    assert state["messages"] == [HumanMessage(content="do the work")]
    assert state["files"] == {"report.md": "data"}
    # Parent turn scratch never crosses into the child.
    assert "todos" not in state
    assert "structured_response" not in state


def test_fork_dispatch_inherits_the_parent_history_then_the_prompt():
    state = build_subagent_state(_parent(), "continue from here", fork=True)

    assert state["messages"][:-1] == _parent()["messages"]
    assert state["messages"][-1] == HumanMessage(content="continue from here")
    assert "todos" not in state
    assert "structured_response" not in state


def test_fork_without_a_parent_history_is_still_a_fresh_prompt():
    state = build_subagent_state({"todos": []}, "start", fork=True)
    assert state["messages"] == [HumanMessage(content="start")]
