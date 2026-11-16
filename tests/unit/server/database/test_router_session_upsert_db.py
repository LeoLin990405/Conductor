"""The router-session upsert must run on migration 122's primary key."""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from src.server.database.autonomy import router as router_db

_SESSION_TABLE = """
CREATE TABLE autonomy_router_sessions (
    user_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    thread_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _local_port() -> int:
    """Pick a port without opening a socket. Unit tests block AF_INET."""
    return 25000 + (os.getpid() % 10000)


def _migration_122_statements() -> list[str]:
    import importlib

    migration = importlib.import_module(
        "migrations.versions.122_router_shard_peer_channel"
    )
    statements: list[str] = []

    class _Op:
        @staticmethod
        def execute(sql: str) -> None:
            statements.append(sql)

    original = migration.op
    try:
        migration.op = _Op()
        migration.upgrade()
    finally:
        migration.op = original
    return statements


@pytest.fixture
def router_session_dsn(tmp_path: Path):
    data = tmp_path / "data"
    sock = Path("/tmp") / f"pgrouter{uuid.uuid4().hex[:8]}"
    sock.mkdir()
    port = _local_port()
    init = subprocess.run(
        [
            "initdb",
            "-D",
            str(data),
            "-U",
            "reviewer",
            "-A",
            "trust",
            "--no-sync",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if init.returncode != 0:
        shutil.rmtree(sock, ignore_errors=True)
        raise RuntimeError(init.stderr or init.stdout)
    start = subprocess.run(
        [
            "pg_ctl",
            "-D",
            str(data),
            "-o",
            f"-p {port} -k {sock} -c listen_addresses=",
            "-l",
            str(tmp_path / "postgres.log"),
            "-w",
            "start",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if start.returncode != 0:
        log = (tmp_path / "postgres.log").read_text(encoding="utf-8", errors="replace")
        shutil.rmtree(sock, ignore_errors=True)
        raise RuntimeError(f"{start.stderr}\n{log}")
    dsn = f"host={sock} port={port} user=reviewer dbname=postgres"
    try:
        yield dsn
    finally:
        subprocess.run(
            ["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(sock, ignore_errors=True)


@pytest.mark.asyncio
async def test_save_router_session_upserts_on_the_shard_primary_key(
    router_session_dsn, monkeypatch
):
    conn = await psycopg.AsyncConnection.connect(router_session_dsn, autocommit=True)
    try:
        await conn.execute(_SESSION_TABLE)
        async with conn.transaction():
            for statement in _migration_122_statements():
                await conn.execute(statement)
        await _reject_user_id_only_conflict(conn)

        @asynccontextmanager
        async def connection(_conn=None):
            yield conn

        monkeypatch.setattr(router_db, "get_db_connection", connection)
        shard_0 = str(uuid.uuid4())
        replaced = str(uuid.uuid4())
        shard_1 = str(uuid.uuid4())
        first = await router_db.save_router_session("u1", "ws-a", shard_0, shard=0)
        second = await router_db.save_router_session("u1", "ws-b", replaced, shard=0)
        other = await router_db.save_router_session("u1", "ws-c", shard_1, shard=1)

        assert first["thread_id"] == shard_0
        assert second["thread_id"] == replaced
        assert second["workspace_id"] == "ws-b"
        assert second["shard"] == 0
        assert other["shard"] == 1

        loaded = await router_db.get_router_session("u1", shard=0)
        assert loaded is not None
        assert loaded["thread_id"] == replaced
        assert loaded["workspace_id"] == "ws-b"
        untouched = await router_db.get_router_session("u1", shard=1)
        assert untouched is not None
        assert untouched["thread_id"] == shard_1

        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT shard FROM autonomy_router_sessions WHERE user_id = %s "
                "ORDER BY shard",
                ("u1",),
            )
            rows = await cur.fetchall()
        assert [row["shard"] for row in rows] == [0, 1]
    finally:
        await conn.close()


async def _reject_user_id_only_conflict(conn) -> None:
    """The pre-122 upsert target is not a unique key after the shard migration."""
    with pytest.raises(psycopg.errors.InvalidColumnReference, match="ON CONFLICT"):
        await conn.execute(
            """
            INSERT INTO autonomy_router_sessions
                (user_id, workspace_id, thread_id, shard)
            VALUES ('legacy', 'ws', %s, 0)
            ON CONFLICT (user_id) DO UPDATE
            SET workspace_id = EXCLUDED.workspace_id
            """,
            (uuid.uuid4(),),
        )
