"""Migration 146 preserves a nullable, constrained Docker git_check field."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from psycopg.errors import CheckViolation

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.skipif(
    not any(os.getenv(name) for name in ("TEST_DB_HOST", "TEST_DB_NAME", "TEST_DB_USER")),
    reason="TEST_DB_* is not configured",
)
async def test_ticket_report_git_check_round_trips_through_145_146_145_146(
    test_db_pool, test_db_uri, seed_user, seed_workspace
):
    ticket_id = uuid4()
    thread_id = uuid4()
    run_id = uuid4()
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option(
        "sqlalchemy.url",
        test_db_uri.replace("postgresql://", "postgresql+psycopg://", 1),
    )
    config.config_file_name = None

    try:
        await asyncio.to_thread(command.downgrade, config, "145")
        async with test_db_pool.connection() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO autonomy_tickets (ticket_id, user_id, workspace_id, status) "
                    "VALUES (%s, %s, %s, 'completed')",
                    (
                        ticket_id,
                        str(seed_user["user_id"]),
                        str(seed_workspace["workspace_id"]),
                    ),
                )
                await conn.execute(
                    "INSERT INTO autonomy_ticket_reports (ticket_id, state, summary) "
                    "VALUES (%s, 'ready', 'legacy report')",
                    (ticket_id,),
                )

        await asyncio.to_thread(command.upgrade, config, "146")
        async with test_db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT git_check FROM autonomy_ticket_reports "
                    "WHERE ticket_id = %s",
                    (ticket_id,),
                )
                assert await cur.fetchall() == [{"git_check": None}]
                workspace_id = str(seed_workspace["workspace_id"])
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO conversation_threads "
                    "(conversation_thread_id, workspace_id, msg_type, current_status, thread_index) "
                    "VALUES (%s, %s, 'ptc', 'completed', %s)",
                    (thread_id, workspace_id, thread_id.int % 2_000_000_000),
                )
                await conn.execute(
                    "UPDATE autonomy_tickets SET status = 'running', "
                    "worker_thread_id = %s, worker_run_id = %s WHERE ticket_id = %s",
                    (thread_id, run_id, ticket_id),
                )

        from src.server.database.autonomy.tickets import (
            latest_report,
            list_reports,
            record_worker_report,
        )
        from src.server.models.autonomy import TicketReportView

        report = await record_worker_report(
            ticket_id=str(ticket_id),
            expected_thread_id=str(thread_id),
            expected_run_id=str(run_id),
            state="ready",
            summary="Docker report",
            body="Docker report body",
            git_check="unsupported_runtime",
            ticket_fields={
                "status": "completed",
                "summary": "Docker report",
                "ended_at": datetime.now(timezone.utc),
            },
        )
        assert report is not None
        assert report["git_check"] == "unsupported_runtime"
        latest = await latest_report(str(ticket_id))
        assert latest is not None
        assert latest["git_check"] == "unsupported_runtime"
        listed = await list_reports(str(ticket_id))
        persisted = next(item for item in listed if item["summary"] == "Docker report")
        assert TicketReportView.model_validate(persisted).git_check == "unsupported_runtime"

        async with test_db_pool.connection() as conn:
            with pytest.raises(CheckViolation):
                async with conn.transaction():
                    await conn.execute(
                        "INSERT INTO autonomy_ticket_reports "
                        "(ticket_id, state, summary, git_check) "
                        "VALUES (%s, 'ready', 'invalid', 'passed')",
                        (ticket_id,),
                    )

        await asyncio.to_thread(command.downgrade, config, "145")
        async with test_db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'autonomy_ticket_reports' AND column_name = 'git_check'"
                )
                assert await cur.fetchone() is None
                await cur.execute(
                    "SELECT count(*) AS n FROM autonomy_ticket_reports WHERE ticket_id = %s",
                    (ticket_id,),
                )
                assert (await cur.fetchone())["n"] == 2

        await asyncio.to_thread(command.upgrade, config, "146")
        async with test_db_pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT git_check FROM autonomy_ticket_reports "
                    "WHERE ticket_id = %s ORDER BY summary",
                    (ticket_id,),
                )
                assert await cur.fetchall() == [{"git_check": None}, {"git_check": None}]
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
        async with test_db_pool.connection() as conn:
            await conn.execute(
                "DELETE FROM autonomy_tickets WHERE ticket_id = %s", (ticket_id,)
            )
            await conn.execute(
                "DELETE FROM conversation_threads WHERE conversation_thread_id = %s",
                (thread_id,),
            )
