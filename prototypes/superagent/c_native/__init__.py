"""Native-CLI SuperAgent prototype: Claude Code, an MCP server, a model shim.

The Router and the Watchdog run inside Claude Code. The CLI's own tools are
switched off, so the only tools the model can reach are the autonomy tools the
MCP server publishes. The CLI's model traffic goes to a shim that forwards
every request to the harness's scripted model over the contract bridge.
"""

from __future__ import annotations
