"""One audit row per collaboration call, refusals included (N80 3.3).

The row stores the tool, the target, a SHA-256 digest of the parameters (never
the parameters), the result code, the HTTP status, whether the call was rate
limited, how many output fragments were redacted and how long it took. A write
records its row on the same connection as the state change, so a write that
cannot be audited rolls back (the ``review_merge._audit_merge`` rule). A
refusal records its row on its own connection after the refused work has
rolled back.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from src.collab.types import ERROR_CODES, payload_digest
from src.server.database.collab import audit as audit_db

logger = logging.getLogger(__name__)

#: Request arguments that are credential material in any front end. They are
#: dropped before the digest is computed, so even the digest cannot be used to
#: confirm a guessed token.
_NEVER_DIGESTED = frozenset({"token", "authorization"})


def params_digest(arguments: Any) -> str:
    """The SHA-256 of the canonical JSON of ``arguments``; never the text."""
    if isinstance(arguments, dict):
        arguments = {
            key: value
            for key, value in arguments.items()
            if str(key).lower() not in _NEVER_DIGESTED
        }
    return payload_digest(arguments if arguments is not None else {})


@dataclass
class AuditRecord:
    """What one call will write; filled in as the call progresses."""

    tool: str
    target: str = ""
    params_sha256: str = ""
    app_id: str | None = None
    agent: str = ""
    rate_limited: bool = False
    redacted: int = 0
    started: float = field(default_factory=time.monotonic)

    def duration_ms(self) -> int:
        return max(0, int((time.monotonic() - self.started) * 1000))


async def write_audit(
    record: AuditRecord,
    *,
    code: str,
    http_status: int | None = None,
    conn: Any = None,
) -> dict[str, Any]:
    """Insert the row; raises when it cannot be written (callers decide)."""
    status = http_status if http_status is not None else ERROR_CODES.get(code, 500)
    return await audit_db.record_audit(
        tool=record.tool,
        app_id=record.app_id,
        agent=record.agent,
        target=record.target[:200],
        params_sha256=record.params_sha256 or params_digest({}),
        code=code,
        http_status=int(status),
        rate_limited=record.rate_limited,
        redacted=record.redacted,
        duration_ms=record.duration_ms(),
        conn=conn,
    )


async def write_refusal_audit(
    record: AuditRecord,
    *,
    code: str,
    http_status: int | None = None,
    authenticated: bool = True,
    peer: str | None = None,
    limiter: Any = None,
) -> bool:
    """Record a refusal; a failure is logged (by code only) and reported False.

    The refusal itself still reaches the caller: there is no state change to
    roll back, and answering ``internal_error`` instead would hide why the
    call was refused. A caller that never authenticated is metered per
    ``peer`` (``RateLimiter.allow_refusal_audit``): past its bucket the row
    is skipped, so unauthenticated traffic cannot flood the audit table.
    """
    if not authenticated:
        from src.collab.ratelimit import get_default_limiter

        if not (limiter or get_default_limiter()).allow_refusal_audit(peer):
            logger.debug(
                "[collab] refusal audit row skipped (peer over its bucket) tool=%s code=%s",
                record.tool,
                code,
            )
            return False
    try:
        await write_audit(record, code=code, http_status=http_status)
    except Exception:  # noqa: BLE001 - never mask the refusal itself
        logger.warning(
            "[collab] could not write the refusal audit row tool=%s code=%s",
            record.tool,
            code,
        )
        return False
    return True


__all__ = [
    "AuditRecord",
    "params_digest",
    "write_audit",
    "write_refusal_audit",
]
