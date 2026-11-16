"""
Utility endpoints for TTS, podcast, RAG, config, health checks, and WebSocket.

This module handles miscellaneous endpoints:
- Text-to-speech conversion
- Podcast generation
- RAG configuration and resources
- Server configuration
- WebSocket chat
- Health checks
- Custom metrics
"""

import logging
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)
INTERNAL_SERVER_ERROR_DETAIL = "Internal Server Error"

# Create router (health checks are unversioned at /health)
health_router = APIRouter(tags=["Health"])

# The checkout this server runs from: src/server/app/utilities.py -> <root>.
_SERVER_ROOT = Path(__file__).resolve().parents[3]

# The tree SHA reported when the server runs from an installation that has
# no checkout to name.
_UNKNOWN_SHA = "unknown"


def _read_server_sha(root: Path) -> str:
    """The tree SHA of the checkout at ``root``; ``unknown`` when it cannot say."""
    try:
        from src.conductor_cli.build_info import build_info

        return build_info(root).sha
    except Exception:
        # A health check that cannot read a version still has to answer.
        logger.warning("could not read the build identity of the server", exc_info=True)
        return _UNKNOWN_SHA


# Frozen when this module is imported, which is when the server process
# starts: the tree this server was started from. Read per request, it would
# answer with the checkout's *current* HEAD after the operator pulled new
# code, and report a match for a server that still runs the old tree --
# hiding exactly the mismatch ``cond doctor`` exists to show.
_SERVER_SHA = _read_server_sha(_SERVER_ROOT)


@health_router.get("/health")
async def health_check():
    """Health check endpoint with checkpointer pool stats.

    A built-in remote MCP server that failed boot discovery makes this a 503,
    not only a body field: readiness probes read the status code, and this
    process serves without that server's tools until its background reconnect
    gets through. Each worker discovers and reconnects on its own, so the
    answer is this worker's.
    """
    from src.server.services.mcp_core.registry import get_global_registry
    from src.server.app.setup import checkpointer
    from src.server.utils.checkpointer import get_checkpointer_health

    result = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat(),
        "version": "0.1.0",
        "service": "ptc-agent",
        # The tree this server answers from, so `cond doctor` can tell a
        # running server from the checkout the operator is editing.
        "sha": _SERVER_SHA,
    }

    # Include checkpointer pool health if configured
    try:
        checkpointer_health = await get_checkpointer_health(checkpointer)
        result["checkpointer"] = checkpointer_health
        if checkpointer_health.get("status") == "unhealthy":
            result["status"] = "degraded"
    except Exception as e:
        result["checkpointer"] = {"status": "error", "error": str(e)}
        result["status"] = "degraded"

    # Process-local memory federation failure counters (reason code -> count).
    # Never raise out of /health: a counter read must not flip readiness.
    try:
        from src.server.services.refactor.memory_federation.health import (
            memory_failure_status,
        )

        result["memory_federation"] = memory_failure_status()
    except Exception:
        result["memory_federation"] = {"failure_counts": {}}

    registry = get_global_registry()
    if registry is not None and registry.failed_builtin_remote:
        result["mcp"] = {
            "status": "degraded",
            "failed_servers": sorted(registry.failed_builtin_remote),
        }
        result["status"] = "unhealthy"
        return JSONResponse(result, status_code=503)

    return result