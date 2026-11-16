"""Process-global agent configuration, readable without importing the app.

``src.server.app.setup`` loads the agent configuration at startup and records
it here with :func:`set_agent_config` (it keeps ``setup.agent_config`` too, for
readers inside the app). Services read it with :func:`get_agent_config`, so
importing them does not load the FastAPI app module (R9b L11).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config.runtime.agent import AgentConfig

__all__ = ["get_agent_config", "set_agent_config"]

_agent_config: AgentConfig | None = None


def get_agent_config() -> AgentConfig | None:
    """The loaded agent configuration, or ``None`` before startup loads it."""
    return _agent_config


def set_agent_config(config: AgentConfig | None) -> None:
    """Record the process's agent configuration (``None`` clears it)."""
    global _agent_config
    _agent_config = config
