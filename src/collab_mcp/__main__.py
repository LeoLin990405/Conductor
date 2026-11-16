"""``conductor-collab-mcp --app <app_id> [--http --port <p>]``.

The token is read from ``--token-ref`` (default
``keychain:conductor-collab/<app_id>``) and is never printed. The HTTP
transport always binds 127.0.0.1: there is deliberately no host option.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conductor-collab-mcp",
        description="Serve the Conductor collab protocol to one registered app over MCP.",
        allow_abbrev=False,
    )
    parser.add_argument("--app", required=True, help="registered app id, e.g. claude-code")
    parser.add_argument(
        "--token-ref",
        default=None,
        help="keychain:<service>/<account> holding the app token "
        "(default keychain:conductor-collab/<app>)",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="Conductor base URL; loopback only (default CONDUCTOR_URL or http://127.0.0.1:8000)",
    )
    parser.add_argument("--agent", default=None, help="agent/model name for the audit trail only")
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve streamable HTTP on 127.0.0.1 instead of stdio",
    )
    parser.add_argument("--port", type=int, default=8765, help="HTTP port on 127.0.0.1 (default 8765)")
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="serve only the read tools plus superagent_context; every other tool is refused locally",
    )
    return parser


def _ensure_checkout_on_path() -> None:
    # The console script resolves ``collab_mcp`` through the editable ``src``
    # path; the protocol schemas live in ``src.collab``, so the checkout root
    # has to be importable as well.
    root = str(Path(__file__).resolve().parents[2])
    if root not in sys.path:
        sys.path.insert(0, root)


def main(argv: Sequence[str] | None = None, *, keychain=None, transport=None) -> int:
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    _ensure_checkout_on_path()
    import os

    import anyio

    from src.collab_mcp.client import CollabClient, CollabClientError, Keychain, KeychainRef
    from src.collab_mcp.server import CollabMCP, build_server, run_http, run_stdio

    try:
        ref = KeychainRef.parse(args.token_ref) if args.token_ref else KeychainRef.for_app(args.app)
        token = (keychain or Keychain()).read(ref)
        client = CollabClient(
            token,
            base_url=args.url or os.environ.get("CONDUCTOR_URL"),
            agent=args.agent,
            transport=transport,
        )
    except CollabClientError as exc:
        print(f"conductor-collab-mcp: {exc}", file=sys.stderr)
        return 2
    if args.read_only:
        from src.collab_mcp.readonly import ReadOnlyCollabMCP, build_read_only_server

        core = ReadOnlyCollabMCP(client)
        server = build_read_only_server(core)
    else:
        core = CollabMCP(client)
        server = build_server(core)
    try:
        if args.http:
            anyio.run(lambda: run_http(server, core, token, port=args.port))
        else:
            anyio.run(lambda: run_stdio(server, core))
    except KeyboardInterrupt:
        return 130
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
