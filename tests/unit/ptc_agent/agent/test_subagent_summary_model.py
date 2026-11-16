"""The subagent roster names each preset's pinned model."""

from ptc_agent.agent.prompts.formatter import format_subagent_summary


class _Client:
    def __init__(self, model: str):
        self.model = model


def test_a_string_model_is_shown():
    out = format_subagent_summary([
        {"name": "research", "description": "Deep research", "model": "glm-5.3", "tools": []},
    ])
    assert "- **research**: Deep research" in out
    assert "  Model: glm-5.3" in out


def test_a_resolved_client_model_is_shown():
    out = format_subagent_summary([
        {"name": "coder", "description": "Writes code", "model": _Client("deepseek-flash")},
    ])
    assert "  Model: deepseek-flash" in out


def test_inheriting_the_thread_model_adds_no_line():
    out = format_subagent_summary([
        {"name": "general-purpose", "description": "General agent", "tools": []},
    ])
    assert "Model:" not in out
