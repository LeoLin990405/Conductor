"""``conductor review`` — server-backed cross-review queue; no self-review.

The queue lives on the Conductor server, not in a per-user directory, so
``review status`` sees the same records from any client and a second client
cannot skip the self-review rule. The CLI still resolves both lines locally
first: a typo fails immediately instead of after a round trip.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .client import CLIError
from .lines import require_line, worktree_root

# Server-issued ids are UUIDs. Refusing anything else before it reaches a URL
# keeps a hostile id from being normalised into a different path.
_REVIEW_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _require_client(client: Any) -> Any:
    if client is None:
        raise CLIError("review requires a running Conductor server")
    return client


def handle_run(args: Any, client: Any = None) -> dict[str, Any]:
    root = worktree_root()
    target = require_line(root, args.line)
    reviewer = require_line(root, args.by)
    if args.line == args.by or _same_line(target, reviewer):
        raise CLIError(
            f"refusing self-review: {args.line!r} cannot review itself"
        )
    api = _require_client(client)
    provider = str(getattr(args, "provider", "codex") or "codex").strip().lower()
    return api.post(
        "/api/v1/reviews",
        {
            "line": args.line,
            "reviewer": args.by,
            "provider": provider,
            "root": str(root),
        },
    )


def _same_line(left: Path, right: Path) -> bool:
    """True when two line paths are the same directory on disk.

    Case-insensitive filesystems (APFS default) make ``LineA`` and ``linea``
    the same folder while ``resolve()`` strings still differ.
    """
    try:
        return left.samefile(right)
    except OSError:
        return left.resolve() == right.resolve()


def handle_status(args: Any, client: Any = None) -> dict[str, Any]:
    api = _require_client(client)
    params: dict[str, Any] = {"limit": 100}
    if args.line:
        params["line"] = args.line
    return api.get("/api/v1/reviews", params=params)


def handle_verdict(args: Any, client: Any = None) -> dict[str, Any]:
    """Record the reviewer's verdict; the server binds it to the line head."""
    review_id = str(getattr(args, "review_id", "") or "")
    if not _REVIEW_ID.fullmatch(review_id):
        raise CLIError(f"{review_id!r} is not a review id")
    api = _require_client(client)
    payload: dict[str, Any] = {
        "verdict": str(args.verdict).lower(),
        "note": str(getattr(args, "note", "") or "")[:2000],
        "root": str(worktree_root()),
    }
    return api.post(f"/api/v1/reviews/{quote(review_id, safe='')}/verdict", payload)
