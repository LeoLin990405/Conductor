"""Review requests between apps; self-review refused by app id (N80 5.3, 6.5, H4).

The author of a lease's work is the lease holder's ``app:<app_id>``. The agent
label never matters: the same app under another agent name is still the
author, so ``review_request`` naming itself and ``review_submit`` by the
author are both ``self_review`` (also enforced in SQL by
``collab_reviews_not_self``). ``reviewer = native`` (the default) records the
request for the native review path; ``app:<id>`` or ``any`` is visible only to
apps of the same user that hold the ``review`` scope. In v1 an app's verdict
is an additional opinion: it never replaces G2, the main review, G5 or G6.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from src.collab.types import (
    InvalidSchemaError,
    NotFoundError,
    SelfReviewError,
    normalize_app_id,
)
from src.server.database.collab import apps as app_db
from src.server.database.collab import reviews as review_db
from src.server.services.collab import tickets as task_svc
from src.server.services.collab.leases import held_lease, iso
from src.server.services.collab.tools import ToolCall, handler
from src.tools.auto_dispatch.review_strict import strict_mode

#: The pre-P0 closed verdict shape; strict review ``off`` keeps it.
_PRE_P0_VERDICT_KEYS = frozenset({"passed", "blockers", "verdict"})


def _reviewer(value: str, author: str) -> str:
    text = str(value or "native").strip()
    if text in {"native", "any"}:
        return text
    app = text[4:] if text.startswith("app:") else text
    try:
        app = normalize_app_id(app)
    except ValueError as exc:
        raise InvalidSchemaError("reviewer must be app:<app_id>, 'native' or 'any'") from exc
    if app == author:
        raise SelfReviewError("self_review: an app cannot review its own work")
    return f"app:{app}"


def _review_id(value: str) -> str:
    text = task_svc.canonical_uuid(value)
    if text is None:
        raise NotFoundError("not_found: no such review")
    return text


async def _same_user(app_id: str, user_id: str, cache: dict[str, str]) -> bool:
    if app_id not in cache:
        app = await app_db.get_app(app_id)
        cache[app_id] = str((app or {}).get("user_id") or "")
    return cache[app_id] == user_id


def review_item(row: Mapping[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "review_id": str(row["review_id"]),
        "target_ref": str(row.get("target_ref") or ""),
        "author": f"app:{row.get('author_app_id')}",
        "commit": str(row.get("commit_sha") or ""),
        "state": str(row.get("state") or ""),
        "focus": str(row.get("focus") or "")[:1000],
    }
    created = iso(row.get("created_at"))
    if created:
        item["created_at"] = created
    return item


@handler("review_request")
async def review_request(call: ToolCall) -> dict[str, Any]:
    args = call.arguments
    lease_id = str(args["lease_id"])
    call.set_target(lease_id)
    lease = await held_lease(call, lease_id, states=frozenset({"active"}))
    ticket = await task_svc.ticket_for_lease(lease, user_id=call.caller.user_id)
    call.require_task_mode(task_svc.task_facts(ticket))
    author = str(lease["app_id"])
    reviewer = _reviewer(str(args.get("reviewer") or "native"), author)
    async with call.transaction() as conn:
        row = await review_db.create_review(
            requester_app_id=call.caller.app_id,
            author_app_id=author,
            target_ref=str(lease["target_ref"]),
            commit_sha=str(args["commit"]),
            reviewer=reviewer,
            focus=str(args.get("focus") or ""),
            lease_id=str(lease["lease_id"]),
            force=args.get("force") is True,
            conn=conn,
        )
        data: dict[str, Any] = {"review_id": str(row["review_id"]), "state": str(row.get("state") or "pending")}
        if row.get("deduplicated"):
            data["deduplicated"] = True
        expires = iso(row.get("expires_at"))
        if expires:
            data["expires_at"] = expires
        return await call.finish(conn, data)


@handler("review_list")
async def review_list(call: ToolCall) -> dict[str, Any]:
    limit = int(call.arguments.get("limit") or 20)
    for_me = call.arguments.get("for_me", True) is not False
    me = call.caller.app_id
    cache: dict[str, str] = {}
    if for_me:
        if "review" not in call.caller.scopes:
            return {"reviews": []}
        rows = await review_db.list_reviews(app_id=me, limit=50)
        rows = [row for row in rows if str(row.get("author_app_id")) != me]
    else:
        rows = await review_db.list_reviews(limit=50)
        rows = [
            row
            for row in rows
            if me in {str(row.get("author_app_id")), str(row.get("requester_app_id"))}
        ]
    items = []
    for row in rows:
        if not await _same_user(str(row.get("author_app_id")), call.caller.user_id, cache):
            continue
        items.append(review_item(row))
        if len(items) >= limit:
            break
    return {"reviews": items}


@handler("review_submit")
async def review_submit(call: ToolCall) -> dict[str, Any]:
    verdict = call.arguments.get("verdict")
    if (
        strict_mode() == "off"
        and isinstance(verdict, Mapping)
        and not set(verdict) <= _PRE_P0_VERDICT_KEYS
    ):
        # The schema now lists the optional N59 keys; ``off`` refuses them
        # with the error the closed pre-P0 schema gave.
        raise InvalidSchemaError("invalid_schema at verdict: additionalProperties")
    review_id = _review_id(str(call.arguments["review_id"]))
    call.set_target(review_id)
    review = await review_db.get_review(review_id)
    if review is None or not await _same_user(
        str(review.get("author_app_id")), call.caller.user_id, {}
    ):
        raise NotFoundError("not_found: no such review")
    if str(review.get("author_app_id")) == call.caller.app_id:
        raise SelfReviewError("self_review: an app cannot review its own work")
    ticket = await task_svc.load_ticket(str(review["target_ref"]), user_id=call.caller.user_id)
    call.require_task_mode(task_svc.task_facts(ticket))
    async with call.transaction() as conn:
        row = await review_db.submit_review(
            review_id=review_id,
            reviewer_app_id=call.caller.app_id,
            verdict=call.arguments["verdict"],
            conn=conn,
        )
        if row is None:
            raise NotFoundError("not_found: the review is no longer pending")
        data = {
            "review_id": str(row["review_id"]),
            "state": str(row.get("state") or ""),
            "verdict": dict(row.get("verdict") or call.arguments["verdict"]),
        }
        return await call.finish(conn, data)


__all__ = ["review_item", "review_list", "review_request", "review_submit"]
