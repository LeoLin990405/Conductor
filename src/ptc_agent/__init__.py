"""PTC Agent - Programmatic Tool Calling for AI agents with MCP.

This package provides:
- Core infrastructure (sandbox, MCP, sessions)
- Agent implementations (PTCAgent, tools, middleware)
- Configuration system
- Utility functions

Quick start:
    from ptc_agent import AgentConfig, PTCAgent
    from ptc_agent.core import SessionManager

    config = AgentConfig.create(llm=your_llm)
    session = SessionManager.get_session("my_session", config.to_core_config())
    await session.initialize()

    agent = PTCAgent(config)
    executor = agent.create_agent(session.sandbox, session.mcp_registry)
"""

__version__ = "0.1.0"

# Every public name is loaded on first access via __getattr__ (R9b L11): importing
# the package loads nothing else, not even the config package, so
# `from ptc_agent.config.file_utils import X` and tools that only touch one
# submodule do not pull in the config/agent/middleware/src.* chain.

_LAZY_IMPORTS = {
    # ptc_agent.config
    "AgentConfig": "ptc_agent.config",
    "CoreConfig": "ptc_agent.config",
    "LLMConfig": "ptc_agent.config",
    "LLMDefinition": "ptc_agent.config",
    "load_core_from_files": "ptc_agent.config",
    "load_from_files": "ptc_agent.config",
    # ptc_agent.agent
    "DaytonaBackend": "ptc_agent.agent.backends",
    "SandboxBackend": "ptc_agent.agent.backends",
    "PTCAgent": "ptc_agent.agent.agent",
    # ptc_agent.core
    "MCPRegistry": "ptc_agent.core",
    "MCPToolInfo": "ptc_agent.core",
    "PTCSandbox": "ptc_agent.core",
    "Session": "ptc_agent.core",
    "SessionManager": "ptc_agent.core",
    # ptc_agent.agent.tools.todo
    "TodoWrite": "ptc_agent.agent.tools.todo",
    "TodoItem": "ptc_agent.agent.tools.todo",
    "TodoStatus": "ptc_agent.agent.tools.todo",
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        import importlib
        module = importlib.import_module(_LAZY_IMPORTS[name])
        value = getattr(module, name)
        # Cache on the module to avoid repeated lookups
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Config (lazy)
    "AgentConfig",
    "CoreConfig",
    "LLMConfig",
    "LLMDefinition",
    "load_core_from_files",
    "load_from_files",
    # Agent (lazy)
    "DaytonaBackend",
    "SandboxBackend",
    "PTCAgent",
    # Core (lazy)
    "MCPRegistry",
    "MCPToolInfo",
    "PTCSandbox",
    "Session",
    "SessionManager",
    # Todo tracking (lazy)
    "TodoWrite",
    "TodoItem",
    "TodoStatus",
    # Version
    "__version__",
]
