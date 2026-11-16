"""``conductor merge`` — server-side merge after a Codex MERGE verdict.

The server rehearses the merge in a throwaway git worktree, runs the tests
against that rehearsal, and only then lands the merge. The CLI keeps its
arguments and JSON envelope; every refusal arrives as a structured error with
a remediation the operator can act on.
"""

from __future__ import annotations

import os
from typing import Any

from .client import CLIError
from .lines import require_line, worktree_root

# The server can spend its full test (max 7200s) + Codex (max 3600s) budget on
# one merge request, so the CLI default outlives that sum; ordinary commands
# keep the client's 30s API timeout.
_DEFAULT_MERGE_TIMEOUT = 11_100.0


def merge_timeout() -> float:
    """Seconds the CLI waits for one merge request."""
    configured = os.environ.get("CONDUCTOR_MERGE_TIMEOUT_SECONDS", "").strip()
    if configured:
        try:
            seconds = float(configured)
        except ValueError:
            pass
        else:
            return max(60.0, min(86400.0, seconds))
    return _DEFAULT_MERGE_TIMEOUT


def handle_merge(args: Any, client: Any = None) -> dict[str, Any]:
    root = worktree_root()
    # Local preflight only: the server resolves and validates the paths again,
    # and it is the server that enforces the local-only privilege, the Codex
    # review verdict and the test gate.
    require_line(root, args.line)
    if client is None:
        raise CLIError("merge requires a running Conductor server")
    payload: dict[str, Any] = {
        "line": args.line,
        "into": args.into,
        "root": str(root),
    }
    configured = getattr(args, "test_cmd", None)
    if configured:
        payload["test_cmd"] = configured
    return client.post("/api/v1/merges", payload, timeout=merge_timeout())
