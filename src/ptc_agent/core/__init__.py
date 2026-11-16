"""Open PTC Agent - PTC Core Infrastructure.

An open source implementation of Programmatic Tool Calling (PTC) with MCP,
where agents generate executable Python code to interact with MCP servers.

This package provides the core infrastructure:
- PTCSandbox: Daytona sandbox management
- MCPRegistry: MCP server connections and tool discovery
- ToolFunctionGenerator: Convert MCP schemas to Python functions
- Session/SessionManager: Session lifecycle management
- Config: Configuration loading

For agent implementations, see the agent package.
"""

__version__ = "0.1.0"

# Loaded on first access (R9b L11) so importing a submodule such as
# ptc_agent.core.sandbox.runtime does not pull in the session/MCP/config chain.
_LAZY_IMPORTS = {
    "CoreConfig": "ptc_agent.config.core",
    "MCPRegistry": "ptc_agent.core.mcp_registry",
    "MCPToolInfo": "ptc_agent.core.mcp_registry",
    "ChartData": "ptc_agent.core.sandbox",
    "ExecutionResult": "ptc_agent.core.sandbox",
    "PTCSandbox": "ptc_agent.core.sandbox",
    "Session": "ptc_agent.core.session",
    "SessionManager": "ptc_agent.core.session",
    "ToolFunctionGenerator": "ptc_agent.core.tool_generator",
}


def __getattr__(name: str):
    if name in _LAZY_IMPORTS:
        import importlib

        module = importlib.import_module(_LAZY_IMPORTS[name])
        value = getattr(module, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ChartData",
    "CoreConfig",
    "ExecutionResult",
    "MCPRegistry",
    "MCPToolInfo",
    "PTCSandbox",
    "Session",
    "SessionManager",
    "ToolFunctionGenerator",
]
