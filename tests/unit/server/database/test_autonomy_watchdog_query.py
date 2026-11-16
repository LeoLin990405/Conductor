from unittest.mock import patch

import pytest

from src.server.database.autonomy import tickets


class _Cursor:
    def __init__(self):
        self.sql = ""

    async def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.params = params

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Connection:
    def __init__(self):
        self.cur = _Cursor()

    def cursor(self, row_factory=None):
        return self.cur

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_running_watchdog_age_uses_start_time_not_progress_updated_at():
    conn = _Connection()
    with patch(
        "src.server.database.autonomy.tickets.get_db_connection",
        return_value=conn,
    ):
        await tickets.list_watchdog_candidates(
            queued_seconds=100,
            waiting_seconds=100,
            running_seconds=200,
            limit=5,
        )

    assert "COALESCE(started_at, updated_at)" in conn.cur.sql
    assert "status = 'running'" in conn.cur.sql
    assert "updated_at < NOW()" in conn.cur.sql
    assert "watchdog_reviewed_at" in conn.cur.sql
    assert "orchestration_only" in conn.cur.sql
