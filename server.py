"""
Server script
"""

import argparse
import ipaddress
import logging
import os

import uvicorn

logger = logging.getLogger(__name__)

DEFAULT_BIND_HOST = "127.0.0.1"
BIND_HOST_ENV = "CONDUCTOR_BIND_HOST"


def default_bind_host() -> str:
    """The deployment's bind host, falling back to loopback.

    A blank value is treated as unset: an empty environment variable cannot
    name an address, and silently falling back beats failing to start.
    """
    return os.getenv(BIND_HOST_ENV, "").strip() or DEFAULT_BIND_HOST


def is_loopback_host(host: str) -> bool:
    """Whether ``host`` names only this machine.

    ``localhost`` and the loopback addresses (including the rest of
    127.0.0.0/8 and ``::1``) count. A wildcard like ``0.0.0.0`` does not: it
    listens on every interface.
    """
    candidate = host.strip().strip("[]")
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the server")
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (default: True except on Windows)",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=default_bind_host(),
        help=(
            "Host to bind the server to "
            f"(default: {BIND_HOST_ENV}, or {DEFAULT_BIND_HOST})"
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind the server to (default: 8000)",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="info",
        choices=["debug", "info", "warning", "error", "critical"],
        help="Log level (default: info)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of uvicorn worker processes (default: 1)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if not is_loopback_host(args.host):
        logger.warning(
            "Binding to non-loopback host %s; the API is reachable from other "
            "machines. Pass --host %s or set %s=%s to keep it local.",
            args.host,
            DEFAULT_BIND_HOST,
            BIND_HOST_ENV,
            DEFAULT_BIND_HOST,
        )

    # Configure SSE event logger independently
    # This allows viewing ONLY SSE events by setting SSE_EVENT_LOG_LEVEL=info
    # and server --log-level=error
    sse_event_log_level = os.getenv("SSE_EVENT_LOG_LEVEL", "info").upper()
    sse_logger = logging.getLogger("sse_events")
    sse_logger.setLevel(getattr(logging, sse_event_log_level))
    # Add dedicated handler so SSE logs output independently of root logger level
    sse_handler = logging.StreamHandler()
    sse_handler.setLevel(getattr(logging, sse_event_log_level))
    sse_handler.setFormatter(logging.Formatter("%(message)s"))
    sse_logger.addHandler(sse_handler)
    # Prevent duplicate logs by not propagating to root logger
    sse_logger.propagate = False

    # Determine reload setting
    reload = False
    if args.reload:
        reload = True

    # Uvicorn never exposes the worker count to the app, so hand it over via
    # env: the lifespan refuses --workers>1 when the WriterGuard fence cannot
    # activate (non-Postgres checkpointer or split app/checkpoint databases).
    # That hard gate is the whole multi-worker admission story — the old
    # single-worker warning's mechanisms (in-process report-back caps/drain,
    # in-memory compaction guard, BTM liveness healing) are all distributed
    # or deleted as of v4 Phase 2 (accepted residual: same-pair dispatch
    # lifecycle overlap across processes, gen-CAS-bounded last-writer-wins).
    os.environ["CONDUCTOR_WORKERS"] = str(args.workers)

    try:
        logger.info(f"Starting server on {args.host}:{args.port}")
        uvicorn.run(
            "src.server.app:app",
            host=args.host,
            port=args.port,
            reload=reload,
            workers=args.workers,
            log_level=args.log_level,
            timeout_keep_alive=300,  # 5 minutes - for long-running workflows
            timeout_graceful_shutdown=60,  # 60 seconds for graceful shutdown
        )
    except Exception as e:
        logger.error(f"Failed to start server: {str(e)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
