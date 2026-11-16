"""The sandbox path constants the TUI's slash commands and completer read.

A copy of the values in ``src/utils/paths.py`` (R9b L07 moved that module out
of ``ptc_agent``, into a package no wheel ships). The TUI runs from an
installed wheel, so it cannot import it; ``tests/unit/conductor_cli/
test_wheel_only_imports.py`` pins these values to the server's. This module
goes with ``ptc_cli/{commands,input}`` in R9b L27.
"""

from __future__ import annotations

DEFAULT_SANDBOX_ROOT: str = "/home/workspace"

AGENT_SYSTEM_DIRS: frozenset[str] = frozenset({".agents", ".self-improve", ".system", "mcp_servers"})
LEGACY_ROOT_DIRS: tuple[str, ...] = ("code", "tools")
HIDDEN_DIR_NAMES: frozenset[str] = frozenset({"_internal"})

ALWAYS_HIDDEN_PATH_SEGMENTS: tuple[str, ...] = ("/__pycache__/",)
ALWAYS_HIDDEN_BASENAMES: tuple[str, ...] = (
    "__init__.py",
    ".bash_logout",
    ".bashrc",
    ".profile",
)
ALWAYS_HIDDEN_SUFFIXES: tuple[str, ...] = (".pyc",)
