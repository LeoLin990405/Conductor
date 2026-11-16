"""Migration 144 through a real Alembic 143 → 144 → 143 round trip."""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _uri(dbname: str) -> str:
    from tests.integration.conftest import _build_db_uri

    return urlunparse(urlparse(_build_db_uri())._replace(path=f"/{dbname}"))


async def _alembic(uri: str, action: str, revision: str) -> None:
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.set_main_option(
        "sqlalchemy.url", uri.replace("postgresql://", "postgresql+psycopg://", 1)
    )
    cfg.config_file_name = None
    runner = command.upgrade if action == "upgrade" else command.downgrade
    await asyncio.to_thread(runner, cfg, revision)


async def _fetch(uri: str, query: str, params: tuple = ()) -> list[tuple]:
    async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
        cursor = await conn.execute(query, params)
        return await cursor.fetchall()


@pytest.mark.skipif(
    not any(os.getenv(name) for name in ("TEST_DB_HOST", "TEST_DB_NAME", "TEST_DB_USER")),
    reason="TEST_DB_* is not configured",
)
async def test_migration_144_round_trips_real_alembic_schema_and_data():
    """Upgrade a scratch database to 143, add 144, roll back, and reapply."""
    admin_uri = _uri("template1")
    database = f"conductor_migration_144_{uuid.uuid4().hex[:10]}"
    async with await psycopg.AsyncConnection.connect(admin_uri, autocommit=True) as admin:
        await admin.execute(f'CREATE DATABASE "{database}"')
    uri = _uri(database)
    try:
        await _alembic(uri, "upgrade", "143")
        assert await _fetch(uri, "SELECT version_num FROM alembic_version") == [("143",)]
        assert await _fetch(
            uri,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'autonomy_ticket_reports' "
            "AND column_name = 'body'",
        ) == []

        async with await psycopg.AsyncConnection.connect(uri, autocommit=True) as conn:
            ticket = await conn.execute(
                "INSERT INTO autonomy_tickets (user_id, workspace_id) "
                "VALUES ('task5-fix2-migration', 'workspace') RETURNING ticket_id"
            )
            ticket_id = (await ticket.fetchone())[0]
            await conn.execute(
                "INSERT INTO autonomy_ticket_reports (ticket_id, state, summary) "
                "VALUES (%s, 'ready', 'existing report')",
                (ticket_id,),
            )

        await _alembic(uri, "upgrade", "144")
        assert await _fetch(uri, "SELECT version_num FROM alembic_version") == [("144",)]
        assert await _fetch(
            uri,
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'autonomy_ticket_reports' "
            "AND column_name = 'body'",
        ) == [("YES",)]
        assert await _fetch(uri, "SELECT body FROM autonomy_ticket_reports") == [(None,)]

        await _fetch(
            uri,
            "INSERT INTO autonomy_ticket_reports (ticket_id, state, summary, body) "
            "VALUES (%s, 'ready', 'full report', %s) RETURNING summary",
            (ticket_id, "first line\nsecond line"),
        )
        assert await _fetch(
            uri, "SELECT body FROM autonomy_ticket_reports ORDER BY summary"
        ) == [(None,), ("first line\nsecond line",)]

        await _alembic(uri, "downgrade", "143")
        assert await _fetch(uri, "SELECT version_num FROM alembic_version") == [("143",)]
        assert await _fetch(
            uri,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'autonomy_ticket_reports' "
            "AND column_name = 'body'",
        ) == []
        assert await _fetch(uri, "SELECT summary FROM autonomy_ticket_reports ORDER BY summary") == [
            ("existing report",),
            ("full report",),
        ]

        await _alembic(uri, "upgrade", "144")
        assert await _fetch(uri, "SELECT version_num FROM alembic_version") == [("144",)]
        assert await _fetch(uri, "SELECT body FROM autonomy_ticket_reports ORDER BY summary") == [
            (None,), (None,)
        ]
    finally:
        async with await psycopg.AsyncConnection.connect(admin_uri, autocommit=True) as admin:
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
