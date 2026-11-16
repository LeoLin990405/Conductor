from unittest.mock import patch

import pytest

from src.server.database.autonomy import effects, outbox


class _Cursor:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount
        self.sql = ""
        self.params = ()

    async def execute(self, sql, params):
        self.sql = " ".join(sql.split())
        self.params = params

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


class _Connection:
    def __init__(self, rowcount: int):
        self.cur = _Cursor(rowcount)

    def cursor(self):
        return self.cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
async def test_dead_letter_is_fenced_by_claim_status_and_attempt_number(
    rowcount, expected
):
    conn = _Connection(rowcount)
    with patch.object(effects, "get_db_connection", return_value=conn):
        result = await effects.dead_letter_effect(
            effect_id="effect-1", attempts=3, error="source deleted"
        )

    assert result is expected
    assert "status = 'claimed' AND attempts = %s" in conn.cur.sql
    assert conn.cur.params == ("source deleted", "effect-1", 3)


@pytest.mark.asyncio
@pytest.mark.parametrize(("rowcount", "expected"), [(1, True), (0, False)])
async def test_dropped_reply_outbox_is_pending_status_fenced(rowcount, expected):
    conn = _Connection(rowcount)
    with patch.object(outbox, "get_db_connection", return_value=conn):
        result = await outbox.mark_dropped("outbox-1", "source deleted")

    assert result is expected
    assert "SET status = 'dropped'" in conn.cur.sql
    assert "WHERE outbox_id = %s AND status = 'pending'" in conn.cur.sql
    assert conn.cur.params == ("source deleted", "outbox-1")
