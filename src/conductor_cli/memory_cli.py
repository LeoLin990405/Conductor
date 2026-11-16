"""``conductor memory`` — recall / propose / approve / reject over the WP12 API.

Every path here must be a route of ``src/server/app/memory.py``;
``tests/unit/conductor_cli/test_memory_cli_routes.py`` checks them against the
real app's route table so the CLI cannot drift onto routes that do not exist.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .client import APIError, CLIError, ServerError

_ABSENT = (404, 405, 501)

SEARCH_PATH = "/api/v1/memory/search"
PROPOSALS_PATH = "/api/v1/memory/proposals"
DECISION_PATH = PROPOSALS_PATH + "/{proposal_id}/decision"


def _unwrap(client: Any, path: str, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None, what: str) -> Any:
    try:
        if body is None:
            return client.get(path, params=params)
        return client.post(path, body)
    except (APIError, ServerError) as exc:
        status = getattr(exc, "status_code", None)
        if status in _ABSENT:
            raise CLIError(
                f"memory {what} is not available on this server "
                f"(HTTP {status}: the memory line's endpoint is absent)"
            ) from exc
        raise


def handle_recall(args: Any, client: Any) -> Any:
    return _unwrap(
        client,
        SEARCH_PATH,
        params={"q": args.query, "limit": args.limit},
        what="recall",
    )


def handle_propose(args: Any, client: Any) -> Any:
    return _unwrap(
        client,
        PROPOSALS_PATH,
        body={"text": args.entry, "type": getattr(args, "type", None) or "user"},
        what="propose",
    )


def _require_proposal_id(proposal_id: str) -> str:
    # ``..`` alone has no slash, so a slash-only check lets httpx normalize
    # ``/proposals/../decision`` to a different endpoint with the bearer token.
    if (
        not proposal_id
        or proposal_id in {".", ".."}
        or ".." in proposal_id
        or any(char in proposal_id for char in "/\\")
    ):
        raise CLIError(f"Invalid proposal id: {proposal_id!r}")
    return proposal_id


def _decide(args: Any, client: Any, approve: bool) -> Any:
    proposal_id = _require_proposal_id(args.proposal_id)
    # quote() is belt-and-braces: even a validated id never reaches the URL raw.
    encoded = quote(proposal_id, safe="")
    return _unwrap(
        client,
        DECISION_PATH.format(proposal_id=encoded),
        body={"approve": approve},
        what="decision",
    )


def handle_approve(args: Any, client: Any) -> Any:
    return _decide(args, client, True)


def handle_reject(args: Any, client: Any) -> Any:
    return _decide(args, client, False)