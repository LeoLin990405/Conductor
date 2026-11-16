"""The prompt preview offers the roles the agent runs in, and each of them renders.

``--role chief_of_staff`` outlived the Chief of Staff: the loader reads ``role`` only to
choose the supervisor's or the worker's prompt, so the "Chief of Staff" preview was the
generalist's prompt with Home's paths and an empty subagent roster, and the supervisor and
worker prompts could not be previewed at all.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import get_args

import pytest

from ptc_agent.agent.prompts.loader import reset_loader
from src.server.contracts.agent_roles import AgentRole

ROOT = Path(__file__).resolve().parents[3]
ROLES = get_args(AgentRole)


@pytest.fixture(scope="module")
def render_prompt():
    # The script puts the repository and src/ first on sys.path when it loads;
    # that is its own concern, not something later tests should inherit.
    saved = list(sys.path)
    try:
        spec = importlib.util.spec_from_file_location(
            "render_prompt_under_test", ROOT / "scripts" / "utils" / "render_prompt.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


@pytest.fixture(autouse=True)
def _fresh_loader():
    # render() replaces the process-wide prompt loader with one of its own.
    yield
    reset_loader()


def test_the_preview_offers_exactly_the_roles_the_agent_runs_in(render_prompt):
    parser = render_prompt.build_parser()
    (role_action,) = [a for a in parser._actions if "--role" in a.option_strings]

    assert set(role_action.choices) == set(ROLES)


def test_a_role_the_agent_no_longer_has_is_refused(render_prompt):
    with pytest.raises(SystemExit):
        render_prompt.build_parser().parse_args(["--role", "chief_of_staff"])


def test_every_role_renders_a_prompt_of_its_own(render_prompt):
    prompts = {
        role: render_prompt.render(render_prompt.build_parser().parse_args(["--role", role]))
        for role in ROLES
    }

    assert all(text.strip() for text in prompts.values())
    assert len(set(prompts.values())) == len(ROLES)
