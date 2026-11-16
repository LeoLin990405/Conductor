"""Errors the MCP hub can show to an operator.

Messages stay free of env values and file bodies. Callers render the
exception text directly.
"""

from __future__ import annotations


class McpHubError(Exception):
    """A hub operation failed; the message is safe to print."""


class PublishConflict(McpHubError):
    """The destination changed after it was read and before publish."""
