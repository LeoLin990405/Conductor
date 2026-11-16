"""One transaction boundary for a collaboration write and its audit row.

The protocol requires the state change, the ownership event and the audit row
to be one commit: a write that cannot be audited must fail, not land. These
helpers accept the connection this context yields (``conn=...``) instead of
opening their own, so the caller owns the boundary.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from src.server.database.pool import get_db_connection


@asynccontextmanager
async def collab_transaction(conn: Any = None) -> AsyncIterator[Any]:
    """Yield a connection inside one transaction, or join ``conn``'s.

    Use it around a claim/release/handoff and the ``record_audit`` call that
    describes it::

        async with collab_transaction() as db:
            async with db.cursor() as cur:
                lease = await claim_ticket_lease(..., conn=db)
                await insert_ticket_event(cur, ...)
                await record_audit(..., conn=db)

    An exception (for example a failed audit insert) exits the transaction with
    that exception, so the claim and every other statement roll back together.
    Passing an existing ``conn`` nests a savepoint, which still fails the whole
    block when the exception propagates.
    """
    async with get_db_connection(conn) as db:
        async with db.transaction():
            yield db


__all__ = ["collab_transaction"]
