"""Durable storage for the main-agent run and task ledger.

Production uses Postgres rows as the source of truth. SQLite is an explicit
offline/test backend; it is never selected implicitly by the server.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sqlite3
import threading
from copy import deepcopy
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import anyio
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.server.database.pool import get_db_connection, open_session_connection


def _coordinator_lock_key(run_id: str) -> int:
    """Use a stable 64-bit key so every worker addresses the same run lock."""
    digest = hashlib.blake2b(f"main-agent:{run_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


_sqlite_init_guard = threading.Lock()
_sqlite_init_locks: dict[str, threading.Lock] = {}
_sqlite_initialized_paths: set[str] = set()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _decode_record(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    return deepcopy(dict(value or {}))


def _task(run: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    return next(
        (task for task in run.get("tasks", []) if str(task.get("id")) == task_id),
        None,
    )


def _task_result(run: dict[str, Any] | None, task_id: str) -> dict[str, Any] | None:
    task = _task(run or {}, task_id)
    return deepcopy(task) if task is not None else None


class PostgresMainAgentRepository:
    """Postgres implementation with row-lock plus revision-CAS mutations."""

    @asynccontextmanager
    async def run_guard(self, run_id: str):
        """Serialize coordinator side effects across workers without pool waiters."""
        conn = await open_session_connection()
        acquired = False
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_try_advisory_lock(%s)", (_coordinator_lock_key(run_id),)
                )
                row = await cur.fetchone()
                acquired = bool(row[0] if row else False)
            yield acquired
        finally:
            try:
                if acquired:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT pg_advisory_unlock(%s)",
                            (_coordinator_lock_key(run_id),),
                        )
            finally:
                with anyio.CancelScope(shield=True):
                    await conn.close()

    async def _read(self, run_id: str, user_id: str | None = None):
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                sql = "SELECT user_id, revision, payload FROM main_agent_runs WHERE run_id = %s"
                params: list[Any] = [run_id]
                if user_id is not None:
                    sql += " AND user_id = %s"
                    params.append(user_id)
                await cur.execute(sql, params)
                row = await cur.fetchone()
        if not row:
            return None
        record = _decode_record(row["payload"])
        record["user_id"] = str(row["user_id"])
        record["revision"] = int(row["revision"])
        return record

    async def create_run(self, user_id: str, request: dict[str, Any]) -> dict[str, Any]:
        run = deepcopy(request)
        run_id = str(run.get("id") or "")
        if not run_id:
            raise ValueError("run record must include id")
        run.update(user_id=user_id, revision=0)
        run.setdefault("alerts", [])
        for task in run.get("tasks", []):
            task.setdefault("revision", 0)
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "INSERT INTO main_agent_runs (run_id, user_id, revision, payload) "
                    "VALUES (%s, %s, 0, %s) RETURNING run_id",
                    (run_id, user_id, Json(run)),
                )
                await cur.fetchone()
        return run

    async def get_run(
        self, run_id: str, user_id: str | None = None
    ) -> dict[str, Any] | None:
        return await self._read(run_id, user_id)

    async def transition_run(
        self, run_id: str, expected_revision: int, patch: dict[str, Any]
    ):
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT user_id, revision, payload FROM main_agent_runs "
                        "WHERE run_id = %s FOR UPDATE",
                        (run_id,),
                    )
                    row = await cur.fetchone()
                    if not row or int(row["revision"]) != expected_revision:
                        return None
                    run = _decode_record(row["payload"])
                    run.update(deepcopy(patch))
                    run["revision"] = expected_revision + 1
                    run["updated_at"] = _iso(_now())
                    await cur.execute(
                        "UPDATE main_agent_runs SET revision = %s, payload = %s, updated_at = NOW() "
                        "WHERE run_id = %s AND user_id = %s AND revision = %s",
                        (
                            run["revision"],
                            Json(run),
                            run_id,
                            row["user_id"],
                            expected_revision,
                        ),
                    )
                    return run if cur.rowcount == 1 else None

    async def list_runs(
        self,
        user_id: str | None,
        statuses: Iterable[str] | None = None,
        limit: int = 500,
        *,
        oldest_first: bool = False,
    ):
        states = tuple(statuses or ())
        where = ""
        params: list[Any] = []
        if user_id is not None:
            where = "WHERE user_id = %s"
            params.append(user_id)
        if states:
            where += (" AND " if where else "WHERE ") + "payload->>'status' = ANY(%s)"
            params.append(list(states))
        params.append(max(1, min(int(limit), 5000)))
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"SELECT user_id, revision, payload FROM main_agent_runs {where} "
                    f"ORDER BY updated_at {'ASC' if oldest_first else 'DESC'}, run_id LIMIT %s",
                    params,
                )
                rows = await cur.fetchall()
        result = []
        for row in rows:
            record = _decode_record(row["payload"])
            record["user_id"] = str(row["user_id"])
            record["revision"] = int(row["revision"])
            result.append(record)
        return result

    async def _mutate_task(
        self,
        run_id: str,
        task_id: str,
        agent_id: str,
        expected_revision: int,
        mutate,
        user_id: str | None = None,
    ):
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    sql = "SELECT user_id, revision, payload FROM main_agent_runs WHERE run_id = %s"
                    params: list[Any] = [run_id]
                    if user_id is not None:
                        sql += " AND user_id = %s"
                        params.append(user_id)
                    await cur.execute(sql + " FOR UPDATE", params)
                    row = await cur.fetchone()
                    if not row:
                        return None
                    run = _decode_record(row["payload"])
                    task = _task(run, task_id)
                    if (
                        task is None
                        or int(task.get("revision") or 0) != expected_revision
                    ):
                        return None
                    if not mutate(run, task, agent_id):
                        return None
                    run["revision"] = int(row["revision"]) + 1
                    run["updated_at"] = _iso(_now())
                    await cur.execute(
                        "UPDATE main_agent_runs SET revision = %s, payload = %s, updated_at = NOW() "
                        "WHERE run_id = %s AND user_id = %s AND revision = %s",
                        (
                            run["revision"],
                            Json(run),
                            run_id,
                            row["user_id"],
                            row["revision"],
                        ),
                    )
                    return run if cur.rowcount == 1 else None

    async def claim_task(
        self,
        run_id,
        task_id,
        agent_id,
        expected_revision,
        lease_seconds,
        *,
        user_id: str,
        daemon_id=None,
    ):
        def mutate(run, task, owner):
            if run.get("status") not in {"queued", "running"} or run.get("phase") in {
                "failed",
                "completed",
                "cancelled",
            }:
                return False
            if task.get("status") != "queued":
                return False
            task_map = {str(t.get("id")): t for t in run.get("tasks", [])}
            if any(
                task_map.get(str(dep), {}).get("status") != "succeeded"
                for dep in task.get("depends_on", [])
            ):
                return False
            now = _now()
            claim_token = secrets.token_urlsafe(24)
            task.update(
                status="running",
                assigned_agent_id=owner,
                daemon_id=daemon_id,
                claim_token=claim_token,
                lease_until=_iso(now + timedelta(seconds=max(1, lease_seconds))),
                heartbeat_at=_iso(now),
                progress_at=_iso(now),
                attempt=int(task.get("attempt") or 0) + 1,
                revision=int(task.get("revision") or 0) + 1,
            )
            run["status"] = "running"
            return True

        run = await self._mutate_task(
            run_id, task_id, agent_id, expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def heartbeat_task(
        self,
        run_id,
        task_id,
        agent_id,
        expected_revision,
        current_step=None,
        lease_seconds=300,
        *,
        user_id: str,
        claim_token: str,
    ):
        def mutate(run, task, owner):
            if (
                task.get("status") != "running"
                or task.get("assigned_agent_id") != owner
                or not claim_token
                or task.get("claim_token") != claim_token
            ):
                return False
            now = _now()
            lease_until = _datetime(task.get("lease_until"))
            if lease_until is None or lease_until <= now:
                return False
            task["heartbeat_at"] = _iso(now)
            if current_step is not None and current_step != task.get("current_step"):
                task["current_step"] = current_step
                task["progress_at"] = _iso(now)
            task["lease_until"] = _iso(now + timedelta(seconds=max(1, lease_seconds)))
            task["revision"] = int(task.get("revision") or 0) + 1
            return True

        run = await self._mutate_task(
            run_id, task_id, agent_id, expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def complete_task(
        self,
        run_id,
        task_id,
        agent_id,
        expected_revision,
        result,
        *,
        user_id: str,
        claim_token: str,
    ):
        def mutate(run, task, owner):
            if (
                task.get("status") != "running"
                or task.get("assigned_agent_id") != owner
                or not claim_token
                or task.get("claim_token") != claim_token
            ):
                return False
            lease_until = _datetime(task.get("lease_until"))
            if lease_until is None or lease_until <= _now():
                return False
            task.update(
                status="succeeded",
                result=deepcopy(result),
                lease_until=None,
                claim_token=None,
                completed_at=_iso(_now()),
                revision=int(task.get("revision") or 0) + 1,
            )
            return True

        run = await self._mutate_task(
            run_id, task_id, agent_id, expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def fail_task(
        self,
        run_id,
        task_id,
        agent_id,
        expected_revision,
        error,
        retryable,
        *,
        user_id: str,
        claim_token: str,
    ):
        def mutate(run, task, owner):
            if (
                task.get("status") != "running"
                or task.get("assigned_agent_id") != owner
                or not claim_token
                or task.get("claim_token") != claim_token
            ):
                return False
            lease_until = _datetime(task.get("lease_until"))
            if lease_until is None or lease_until <= _now():
                return False
            history = task.setdefault("attempt_history", [])
            history.append(
                {
                    "attempt": int(task.get("attempt") or 0),
                    "error": deepcopy(error),
                    "retryable": bool(retryable),
                    "failed_at": _iso(_now()),
                }
            )
            task.update(
                status="failed",
                error=deepcopy(error),
                retryable=bool(retryable),
                lease_until=None,
                assigned_agent_id=None,
                claim_token=None,
                revision=int(task.get("revision") or 0) + 1,
            )
            return True

        run = await self._mutate_task(
            run_id, task_id, agent_id, expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def retry_task(self, run_id, task_id, expected_revision, *, user_id: str):
        def mutate(run, task, _owner):
            if task.get("status") == "failed" and task.get("retryable"):
                pass
            elif task.get("status") == "running":
                lease_until = _datetime(task.get("lease_until"))
                if lease_until is None or lease_until > _now():
                    return False
                task.setdefault("attempt_history", []).append(
                    {
                        "attempt": int(task.get("attempt") or 0),
                        "error": {"code": "lease_expired"},
                        "retryable": True,
                        "failed_at": _iso(_now()),
                    }
                )
            else:
                return False
            task.update(
                status="queued",
                error=None,
                retryable=False,
                assigned_agent_id=None,
                daemon_id=None,
                claim_token=None,
                lease_until=None,
                revision=int(task.get("revision") or 0) + 1,
            )
            run["status"] = "queued"
            run["phase"] = "execute"
            return True

        run = await self._mutate_task(
            run_id, task_id, "", expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def record_alert_once(self, run_id, task_id, alert_key, record):
        async with get_db_connection() as conn:
            async with conn.transaction():
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT user_id, revision, payload FROM main_agent_runs WHERE run_id = %s FOR UPDATE",
                        (run_id,),
                    )
                    row = await cur.fetchone()
                    if not row:
                        return False
                    try:
                        await cur.execute(
                            "INSERT INTO main_agent_alerts (run_id, task_id, alert_key, user_id, payload) "
                            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING alert_key",
                            (run_id, task_id, alert_key, row["user_id"], Json(record)),
                        )
                        inserted = await cur.fetchone()
                        if not inserted:
                            return False
                        run = _decode_record(row["payload"])
                        run.setdefault("alerts", []).append(deepcopy(record))
                        revision = int(row["revision"]) + 1
                        run["revision"] = revision
                        run["updated_at"] = _iso(_now())
                        await cur.execute(
                            "UPDATE main_agent_runs SET revision = %s, payload = %s, updated_at = NOW() WHERE run_id = %s AND revision = %s",
                            (revision, Json(run), run_id, row["revision"]),
                        )
                        return cur.rowcount == 1
                    except Exception:
                        raise

    async def list_alerts(self, user_id: str, limit: int = 200):
        async with get_db_connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT payload FROM main_agent_alerts WHERE user_id = %s ORDER BY created_at DESC LIMIT %s",
                    (user_id, max(1, min(int(limit), 1000))),
                )
                return [_decode_record(row["payload"]) for row in await cur.fetchall()]


class SQLiteMainAgentRepository:
    """Explicit local backend for offline demos and race tests."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError(
                "SQLite repository needs a file path for cross-connection durability"
            )
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @asynccontextmanager
    async def run_guard(self, _run_id: str):
        """SQLite is an explicit single-process demo/test backend."""
        yield True

    def _initialize(self):
        key = str(Path(self.path).resolve())
        with _sqlite_init_guard:
            lock = _sqlite_init_locks.setdefault(key, threading.Lock())
        with lock:
            if key in _sqlite_initialized_paths:
                return
            self._initialize_locked()
            _sqlite_initialized_paths.add(key)

    def _initialize_locked(self):
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS main_agent_runs (
                    run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS main_agent_runs_user_updated
                    ON main_agent_runs(user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS main_agent_alerts (
                    run_id TEXT NOT NULL, task_id TEXT NOT NULL, alert_key TEXT NOT NULL,
                    user_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(run_id, task_id, alert_key)
                );
                CREATE INDEX IF NOT EXISTS main_agent_alerts_user_created
                    ON main_agent_alerts(user_id, created_at DESC);
            """)
        finally:
            conn.close()

    async def _init(self):
        # DDL is idempotent, and doing it on each operation avoids retaining
        # an event-loop-bound initialization task across multiple async loops.
        await asyncio.to_thread(self._initialize)

    @staticmethod
    def _run(row):
        if row is None:
            return None
        run = _decode_record(row["payload"])
        run["user_id"] = row["user_id"]
        run["revision"] = int(row["revision"])
        return run

    async def create_run(self, user_id, request):
        await self._init()
        run = deepcopy(request)
        run_id = str(run.get("id") or "")
        if not run_id:
            raise ValueError("run record must include id")
        run.update(user_id=user_id, revision=0)
        run.setdefault("alerts", [])
        for task in run.get("tasks", []):
            task.setdefault("revision", 0)

        def op():
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO main_agent_runs VALUES (?, ?, 0, ?, ?)",
                    (run_id, user_id, json.dumps(run), _iso(_now())),
                )
            finally:
                conn.close()

        await asyncio.to_thread(op)
        return run

    async def get_run(self, run_id, user_id=None):
        await self._init()

        def op():
            conn = self._connect()
            try:
                if user_id is None:
                    row = conn.execute(
                        "SELECT user_id, revision, payload FROM main_agent_runs WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT user_id, revision, payload FROM main_agent_runs WHERE run_id=? AND user_id=?",
                        (run_id, user_id),
                    ).fetchone()
                return self._run(row)
            finally:
                conn.close()

        return await asyncio.to_thread(op)

    async def _mutate(self, run_id, expected_revision, mutate):
        await self._init()

        def op():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT user_id, revision, payload FROM main_agent_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None or int(row["revision"]) != expected_revision:
                    conn.rollback()
                    return None
                run = self._run(row)
                if not mutate(run):
                    conn.rollback()
                    return None
                run["revision"] = expected_revision + 1
                run["updated_at"] = _iso(_now())
                changed = conn.execute(
                    "UPDATE main_agent_runs SET revision=?, payload=?, updated_at=? WHERE run_id=? AND user_id=? AND revision=?",
                    (
                        run["revision"],
                        json.dumps(run),
                        run["updated_at"],
                        run_id,
                        row["user_id"],
                        expected_revision,
                    ),
                ).rowcount
                conn.commit()
                return run if changed == 1 else None
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

        return await asyncio.to_thread(op)

    async def transition_run(self, run_id, expected_revision, patch):
        return await self._mutate(
            run_id, expected_revision, lambda run: run.update(deepcopy(patch)) is None
        )

    async def list_runs(self, user_id, statuses=None, limit=500, *, oldest_first=False):
        await self._init()
        states = tuple(statuses or ())

        def op():
            conn = self._connect()
            try:
                sql = "SELECT user_id, revision, payload FROM main_agent_runs"
                params: list[Any] = []
                conditions = []
                if user_id is not None:
                    conditions.append("user_id=?")
                    params.append(user_id)
                if states:
                    conditions.append(
                        "json_extract(payload, '$.status') IN ("
                        + ",".join("?" for _ in states)
                        + ")"
                    )
                    params.extend(states)
                if conditions:
                    sql += " WHERE " + " AND ".join(conditions)
                sql += f" ORDER BY updated_at {'ASC' if oldest_first else 'DESC'}, run_id LIMIT ?"
                params.append(max(1, min(int(limit), 5000)))
                rows = conn.execute(sql, params).fetchall()
                result = [self._run(row) for row in rows]
                return result
            finally:
                conn.close()

        return await asyncio.to_thread(op)

    async def _mutate_task(
        self, run_id, task_id, agent_id, expected_revision, mutate, user_id=None
    ):
        await self._init()

        def op():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                if user_id is None:
                    row = conn.execute(
                        "SELECT user_id, revision, payload FROM main_agent_runs WHERE run_id=?",
                        (run_id,),
                    ).fetchone()
                else:
                    row = conn.execute(
                        "SELECT user_id, revision, payload FROM main_agent_runs WHERE run_id=? AND user_id=?",
                        (run_id, user_id),
                    ).fetchone()
                if row is None:
                    conn.rollback()
                    return None
                run = self._run(row)
                task = _task(run, task_id)
                if (
                    task is None
                    or int(task.get("revision") or 0) != expected_revision
                    or not mutate(run, task, agent_id)
                ):
                    conn.rollback()
                    return None
                run["revision"] = int(row["revision"]) + 1
                run["updated_at"] = _iso(_now())
                changed = conn.execute(
                    "UPDATE main_agent_runs SET revision=?, payload=?, updated_at=? WHERE run_id=? AND user_id=? AND revision=?",
                    (
                        run["revision"],
                        json.dumps(run),
                        run["updated_at"],
                        run_id,
                        row["user_id"],
                        row["revision"],
                    ),
                ).rowcount
                conn.commit()
                return run if changed == 1 else None
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

        return await asyncio.to_thread(op)

    async def claim_task(
        self,
        run_id,
        task_id,
        agent_id,
        expected_revision,
        lease_seconds,
        *,
        user_id: str,
        daemon_id=None,
    ):
        def mutate(run, task, owner):
            if run.get("status") not in {"queued", "running"} or run.get("phase") in {
                "failed",
                "completed",
                "cancelled",
            }:
                return False
            if task.get("status") != "queued":
                return False
            task_map = {str(t.get("id")): t for t in run.get("tasks", [])}
            if any(
                task_map.get(str(dep), {}).get("status") != "succeeded"
                for dep in task.get("depends_on", [])
            ):
                return False
            now = _now()
            claim_token = secrets.token_urlsafe(24)
            task.update(
                status="running",
                assigned_agent_id=owner,
                daemon_id=daemon_id,
                claim_token=claim_token,
                lease_until=_iso(now + timedelta(seconds=max(1, lease_seconds))),
                heartbeat_at=_iso(now),
                progress_at=_iso(now),
                attempt=int(task.get("attempt") or 0) + 1,
                revision=int(task.get("revision") or 0) + 1,
            )
            run["status"] = "running"
            return True

        run = await self._mutate_task(
            run_id, task_id, agent_id, expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def heartbeat_task(
        self,
        run_id,
        task_id,
        agent_id,
        expected_revision,
        current_step=None,
        lease_seconds=300,
        *,
        user_id: str,
        claim_token: str,
    ):
        def mutate(run, task, owner):
            if (
                task.get("status") != "running"
                or task.get("assigned_agent_id") != owner
                or not claim_token
                or task.get("claim_token") != claim_token
            ):
                return False
            now = _now()
            lease_until = _datetime(task.get("lease_until"))
            if lease_until is None or lease_until <= now:
                return False
            task["heartbeat_at"] = _iso(now)
            if current_step is not None and current_step != task.get("current_step"):
                task["current_step"] = current_step
                task["progress_at"] = _iso(now)
            task["lease_until"] = _iso(now + timedelta(seconds=max(1, lease_seconds)))
            task["revision"] = int(task.get("revision") or 0) + 1
            return True

        run = await self._mutate_task(
            run_id, task_id, agent_id, expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def complete_task(
        self,
        run_id,
        task_id,
        agent_id,
        expected_revision,
        result,
        *,
        user_id: str,
        claim_token: str,
    ):
        def mutate(run, task, owner):
            if (
                task.get("status") != "running"
                or task.get("assigned_agent_id") != owner
                or not claim_token
                or task.get("claim_token") != claim_token
            ):
                return False
            lease_until = _datetime(task.get("lease_until"))
            if lease_until is None or lease_until <= _now():
                return False
            task.update(
                status="succeeded",
                result=deepcopy(result),
                lease_until=None,
                claim_token=None,
                completed_at=_iso(_now()),
                revision=int(task.get("revision") or 0) + 1,
            )
            return True

        run = await self._mutate_task(
            run_id, task_id, agent_id, expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def fail_task(
        self,
        run_id,
        task_id,
        agent_id,
        expected_revision,
        error,
        retryable,
        *,
        user_id: str,
        claim_token: str,
    ):
        def mutate(run, task, owner):
            if (
                task.get("status") != "running"
                or task.get("assigned_agent_id") != owner
                or not claim_token
                or task.get("claim_token") != claim_token
            ):
                return False
            lease_until = _datetime(task.get("lease_until"))
            if lease_until is None or lease_until <= _now():
                return False
            history = task.setdefault("attempt_history", [])
            history.append(
                {
                    "attempt": int(task.get("attempt") or 0),
                    "error": deepcopy(error),
                    "retryable": bool(retryable),
                    "failed_at": _iso(_now()),
                }
            )
            task.update(
                status="failed",
                error=deepcopy(error),
                retryable=bool(retryable),
                lease_until=None,
                assigned_agent_id=None,
                claim_token=None,
                revision=int(task.get("revision") or 0) + 1,
            )
            return True

        run = await self._mutate_task(
            run_id, task_id, agent_id, expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def retry_task(self, run_id, task_id, expected_revision, *, user_id: str):
        def mutate(run, task, _owner):
            if task.get("status") == "failed" and task.get("retryable"):
                pass
            elif task.get("status") == "running":
                lease_until = _datetime(task.get("lease_until"))
                if lease_until is None or lease_until > _now():
                    return False
                task.setdefault("attempt_history", []).append(
                    {
                        "attempt": int(task.get("attempt") or 0),
                        "error": {"code": "lease_expired"},
                        "retryable": True,
                        "failed_at": _iso(_now()),
                    }
                )
            else:
                return False
            task.update(
                status="queued",
                error=None,
                retryable=False,
                assigned_agent_id=None,
                daemon_id=None,
                claim_token=None,
                lease_until=None,
                revision=int(task.get("revision") or 0) + 1,
            )
            run["status"] = "queued"
            run["phase"] = "execute"
            return True

        run = await self._mutate_task(
            run_id, task_id, "", expected_revision, mutate, user_id
        )
        return _task_result(run, task_id)

    async def record_alert_once(self, run_id, task_id, alert_key, record):
        await self._init()

        def op():
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT user_id, revision, payload FROM main_agent_runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO main_agent_alerts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        run_id,
                        task_id,
                        alert_key,
                        row["user_id"],
                        json.dumps(record),
                        _iso(_now()),
                    ),
                ).rowcount
                if not inserted:
                    conn.rollback()
                    return False
                run = self._run(row)
                run.setdefault("alerts", []).append(deepcopy(record))
                run["revision"] = int(row["revision"]) + 1
                run["updated_at"] = _iso(_now())
                conn.execute(
                    "UPDATE main_agent_runs SET revision=?, payload=?, updated_at=? WHERE run_id=? AND revision=?",
                    (
                        run["revision"],
                        json.dumps(run),
                        run["updated_at"],
                        run_id,
                        row["revision"],
                    ),
                )
                conn.commit()
                return True
            except BaseException:
                conn.rollback()
                raise
            finally:
                conn.close()

        return await asyncio.to_thread(op)

    async def list_alerts(self, user_id, limit=200):
        await self._init()

        def op():
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT payload FROM main_agent_alerts WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                    (user_id, max(1, min(int(limit), 1000))),
                ).fetchall()
                return [_decode_record(row["payload"]) for row in rows]
            finally:
                conn.close()

        return await asyncio.to_thread(op)
