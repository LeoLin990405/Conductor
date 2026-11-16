"""Tests for the agent_fleet configuration section.

The fleet is machine-shaped, so the section has to carry sane defaults, clamp
what an operator types, and let three environment variables override it for a
single host without a committed config overlay.
"""

from __future__ import annotations

import pytest

from src.config.models import AgentFleetConfig
from src.config.settings import get_agent_fleet_config


class TestAgentFleetConfig:
    def test_defaults(self):
        config = AgentFleetConfig()
        assert config.enabled is True
        assert config.root == "~/.conductor/worktrees"
        assert config.stall_mins == 5
        assert config.base_ref == "main"
        assert config.logs_dir == ""
        assert config.prompts_dir == ""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"stall_mins": 0},
            {"stall_mins": 5000},
            {"max_worktrees": 0},
            {"command_timeout_seconds": 0.1},
            {"root": ""},
        ],
    )
    def test_rejects_out_of_range_values(self, kwargs):
        with pytest.raises(ValueError):
            AgentFleetConfig(**kwargs)

    def test_reads_the_deployment_section(self):
        config = get_agent_fleet_config()
        assert config.root.endswith("conductor-wt")
        assert config.stall_mins >= 1


class TestEnvironmentOverrides:
    def test_stall_minutes_override(self, monkeypatch):
        monkeypatch.setenv("AGENT_STALL_MINS", "15")
        assert get_agent_fleet_config().stall_mins == 15

    def test_stall_minutes_are_clamped_and_invalid_values_ignored(self, monkeypatch):
        baseline = get_agent_fleet_config().stall_mins
        monkeypatch.setenv("AGENT_STALL_MINS", "99999")
        assert get_agent_fleet_config().stall_mins == 1440
        # An unparseable value is ignored, which means the configured default.
        monkeypatch.setenv("AGENT_STALL_MINS", "soon")
        assert get_agent_fleet_config().stall_mins == baseline

    def test_root_and_base_ref_overrides(self, monkeypatch):
        monkeypatch.setenv("AGENT_FLEET_ROOT", "/srv/agents")
        monkeypatch.setenv("AGENT_FLEET_BASE_REF", "origin/main")
        config = get_agent_fleet_config()
        assert config.root == "/srv/agents"
        assert config.base_ref == "origin/main"

    def test_blank_overrides_are_ignored(self, monkeypatch):
        before = get_agent_fleet_config()
        monkeypatch.setenv("AGENT_STALL_MINS", "  ")
        monkeypatch.setenv("AGENT_FLEET_ROOT", "")
        monkeypatch.setenv("AGENT_FLEET_BASE_REF", " ")
        assert get_agent_fleet_config() == before
