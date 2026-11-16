"""MCP core: server registry, tool-name sanitizing, schema, composite views, diagnostics.

Moved from ``src/ptc_agent/core/mcp_*.py`` in R9b L09. Submodules are imported
directly (``src.server.services.mcp_core.registry`` ...); this package does not
re-export them so importing one does not pull in the MCP client stack.
"""
