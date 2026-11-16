"""Unhandled errors the client can see, and the id that finds the traceback.

A response from here carries a fixed sentence (``Internal server error``)
and a correlation id, in the body and in ``x-correlation-id``. No text of
the exception reaches the client: a blocklist scrubber cannot know every
secret shape, so the 500 body does not depend on one. The exception's
type, message, and traceback stay in the log next to the same id. Starlette
uses this handler for anything that is not a more specific exception
(HTTPException included), and only when the app is not in debug mode.
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse

from src.server.public_errors import (
    PUBLIC_MESSAGE_LIMIT,
    log_failure,
    new_correlation_id,
    public_exception_message,
    scrub_public_message,
    usable_correlation_id,
)

__all__ = [
    "PUBLIC_500_DETAIL",
    "PUBLIC_MESSAGE_LIMIT",
    "log_failure",
    "new_correlation_id",
    "public_exception_message",
    "scrub_public_message",
    "unhandled_exception_handler",
    "usable_correlation_id",
]

logger = logging.getLogger(__name__)

PUBLIC_500_DETAIL = "Internal server error"


def _correlation_id_for(request: Request) -> str:
    # The request-id middleware stamps this before the route runs. Reusing
    # it ties the 500 to every other line from the same request.
    trace_id = usable_correlation_id(getattr(request.state, "trace_id", None))
    if trace_id is not None:
        return trace_id
    return new_correlation_id()


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    correlation_id = _correlation_id_for(request)
    log_failure(correlation_id, exc, log=logger)
    return JSONResponse(
        status_code=500,
        content={
            "detail": PUBLIC_500_DETAIL,
            "correlation_id": correlation_id,
        },
        headers={
            "x-correlation-id": correlation_id,
            "x-trace-id": correlation_id,
        },
    )
