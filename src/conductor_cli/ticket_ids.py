"""Resolve a Ticket id the user typed: a full UUID or a unique id prefix.

The CLI and the REPL print short ids (the first 8 characters). The server's
Ticket routes are typed UUID, so a short id is resolved first: through
``GET /api/v1/autonomy/tickets/resolve`` when the server has it, else by
scanning the caller's Ticket listing (an older server answers the resolve path
with 404/405/422 because it reads ``resolve`` as a Ticket id).
"""

from __future__ import annotations

import re
from typing import Any, Protocol
from uuid import UUID

from .client import APIError, CLIError

RESOLVE_PATH = "/api/v1/autonomy/tickets/resolve"
LIST_PATH = "/api/v1/autonomy/tickets"
_PREFIX = re.compile(r"[0-9a-f-]{1,36}")
# Old-server fallback: at most this many listing pages of 200 Tickets.
_FALLBACK_PAGE = 200
_FALLBACK_PAGES = 10
_ROUTE_MISSING = (404, 405, 422)
_SHOWN_CANDIDATES = 5


class _Client(Protocol):
    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any: ...


def _full_uuid(value: str) -> str | None:
    if len(value) not in (32, 36):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _candidate_line(match: dict[str, Any]) -> str:
    ticket_id = str(match.get("ticket_id") or "")
    status = str(match.get("status") or "?")
    title = " ".join(str(match.get("title") or "").split())
    if len(title) > 40:
        title = title[:37] + "..."
    return f"{ticket_id[:8]} [{status}] {title}".rstrip()


def _server_matches(client: _Client, prefix: str) -> tuple[list[dict[str, Any]], bool] | None:
    try:
        body = client.get(RESOLVE_PATH, params={"prefix": prefix})
    except APIError as exc:
        if getattr(exc, "status_code", None) in _ROUTE_MISSING:
            return None
        raise
    if not isinstance(body, dict) or not isinstance(body.get("matches"), list):
        return None
    matches = [m for m in body["matches"] if isinstance(m, dict) and m.get("ticket_id")]
    return matches, bool(body.get("more"))


def _listing_matches(client: _Client, prefix: str) -> tuple[list[dict[str, Any]], bool]:
    matches: list[dict[str, Any]] = []
    for page in range(_FALLBACK_PAGES):
        body = client.get(
            LIST_PATH, params={"limit": _FALLBACK_PAGE, "offset": page * _FALLBACK_PAGE}
        )
        rows = body.get("tickets") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            break
        matches.extend(
            row
            for row in rows
            if isinstance(row, dict) and str(row.get("ticket_id") or "").lower().startswith(prefix)
        )
        if len(rows) < _FALLBACK_PAGE:
            break
    return matches, False


def resolve_ticket_ref(client: _Client, ref: str) -> str:
    """The full Ticket id for ``ref`` (a UUID or a unique prefix).

    No match raises a 404 ``APIError``; several matches raise a 409 one that
    lists the candidates (``detail["candidates"]``).
    """
    text = (ref or "").strip()
    if not text:
        raise CLIError("A Ticket id (or a unique id prefix) is required")
    full = _full_uuid(text)
    if full is not None:
        return full
    prefix = text.lower()
    if not _PREFIX.fullmatch(prefix):
        raise APIError(f"No Ticket matches {text!r}: an id is hex digits and dashes", 404)
    found = _server_matches(client, prefix)
    matches, more = found if found is not None else _listing_matches(client, prefix)
    if not matches:
        raise APIError(f"No Ticket id starts with {prefix!r}", 404)
    if len(matches) == 1 and not more:
        return str(matches[0]["ticket_id"])
    shown = [_candidate_line(m) for m in matches[:_SHOWN_CANDIDATES]]
    extra = len(matches) - len(shown)
    tail = f"; and {extra} more" if extra > 0 else ""
    tail += "; and others" if more else ""
    raise APIError(
        f"{prefix!r} is ambiguous; it matches: {'; '.join(shown)}{tail}. "
        "Type more of the id.",
        409,
        detail={"candidates": [str(m["ticket_id"]) for m in matches]},
    )
