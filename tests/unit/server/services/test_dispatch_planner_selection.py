"""CONDUCTOR_DISPATCH_PLANNER picks the live planner backend; dev is unchanged."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.server.services.dispatch import (
    PLANNER_CLAUDE_CODE,
    PLANNER_LLM,
    DispatchService,
    DispatchSettings,
)
from src.tools.auto_dispatch import Dispatch, FileApprovalGate, InMemoryDispatchStore
from src.tools.auto_dispatch.claude_planner import ClaudeCodeComplete
from src.tools.auto_dispatch.dev import FixedPlanner
from src.tools.auto_dispatch.planning import LLMPlanner

pytestmark = pytest.mark.asyncio


class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def complete(self, **kwargs: str) -> str:
        self.calls.append(kwargs)
        return "llm-reply"


def _dispatch() -> Dispatch:
    # Built directly rather than through start(), which would also drive it.
    return Dispatch(id="d1", goal="one", user_id="u1", created_at="now")


def _service(tmp_path, settings: DispatchSettings, llm=None) -> DispatchService:
    return DispatchService(
        settings,
        store=InMemoryDispatchStore(),
        gate=FileApprovalGate(tmp_path / "proposed"),
        llm_service=llm,
    )


async def test_settings_default_planner_is_claude_code() -> None:
    settings = DispatchSettings.from_env({"CONDUCTOR_DISPATCH_ADAPTERS": "live"})
    assert settings.planner == PLANNER_CLAUDE_CODE == "claude-code"
    assert settings.planner_bin == "claude"
    assert settings.planner_model == "claude-opus-5-5"
    assert settings.planner_timeout_seconds == 600.0
    assert DispatchSettings.from_env({}).planner == PLANNER_CLAUDE_CODE


async def test_settings_read_the_planner_overrides() -> None:
    settings = DispatchSettings.from_env(
        {
            "CONDUCTOR_DISPATCH_ADAPTERS": "live",
            "CONDUCTOR_DISPATCH_PLANNER": " LLM ",
            "CONDUCTOR_DISPATCH_PLANNER_BIN": "/opt/bin/claude",
            "CONDUCTOR_DISPATCH_PLANNER_MODEL": "claude-other",
            "CONDUCTOR_DISPATCH_PLANNER_TIMEOUT": "42",
        }
    )
    assert settings.planner == PLANNER_LLM
    assert settings.planner_bin == "/opt/bin/claude"
    assert settings.planner_model == "claude-other"
    assert settings.planner_timeout_seconds == 42.0


async def test_blank_planner_values_fall_back_to_the_defaults() -> None:
    settings = DispatchSettings.from_env(
        {
            "CONDUCTOR_DISPATCH_PLANNER": "  ",
            "CONDUCTOR_DISPATCH_PLANNER_BIN": "",
            "CONDUCTOR_DISPATCH_PLANNER_MODEL": " ",
        }
    )
    assert settings.planner == PLANNER_CLAUDE_CODE
    assert settings.planner_bin == "claude"
    assert settings.planner_model == "claude-opus-5-5"


async def test_an_unknown_planner_is_refused() -> None:
    with pytest.raises(ValueError, match="CONDUCTOR_DISPATCH_PLANNER"):
        DispatchSettings.from_env({"CONDUCTOR_DISPATCH_PLANNER": "gpt"})


async def test_live_defaults_to_the_claude_code_planner_in_the_repo(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = DispatchSettings.from_env(
        {
            "CONDUCTOR_DISPATCH_ADAPTERS": "live",
            "CONDUCTOR_DISPATCH_REPO": str(repo),
            "CONDUCTOR_DISPATCH_RUNS_DIR": str(tmp_path / "runs"),
            "CONDUCTOR_DISPATCH_PLANNER_BIN": "/fake/claude",
            "CONDUCTOR_DISPATCH_PLANNER_MODEL": "claude-m",
            "CONDUCTOR_DISPATCH_PLANNER_TIMEOUT": "7",
        }
    )
    llm = FakeLLM()
    service = _service(tmp_path, settings, llm=llm)
    dispatch = _dispatch()

    planner = service._machine(dispatch)._c.planner

    assert isinstance(planner, LLMPlanner)
    complete = planner._complete
    assert isinstance(complete, ClaudeCodeComplete)
    assert complete.cwd == Path(repo)
    assert complete.binary == "/fake/claude"
    assert complete.model == "claude-m"
    assert complete.timeout_seconds == 7.0
    assert llm.calls == []


async def test_llm_planner_keeps_the_llm_service_path(tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    settings = DispatchSettings(
        adapters="live",
        runs_dir=tmp_path.parent / f"{tmp_path.name}-runs",
        repo_root=repo,
        planner=PLANNER_LLM,
    )
    llm = FakeLLM()
    service = _service(tmp_path, settings, llm=llm)
    dispatch = _dispatch()

    planner = service._machine(dispatch)._c.planner

    assert isinstance(planner, LLMPlanner)
    assert not isinstance(planner._complete, ClaudeCodeComplete)
    assert await planner._complete("P", "S") == "llm-reply"
    assert llm.calls == [{"user_id": "u1", "user_prompt": "P", "system_prompt": "S"}]


@pytest.mark.parametrize("choice", [PLANNER_CLAUDE_CODE, PLANNER_LLM])
async def test_dev_adapters_keep_the_fixed_planner(tmp_path, choice) -> None:
    settings = DispatchSettings(adapters="dev", runs_dir=tmp_path / "runs", planner=choice)
    service = _service(tmp_path, settings)
    dispatch = _dispatch()

    assert isinstance(service._machine(dispatch)._c.planner, FixedPlanner)
