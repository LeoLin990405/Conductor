"""Two-process, real-Postgres/Redis probe for lost and delayed cancel nudges.

Uses an isolated schema and real LocalRunExecutor/TurnCancelListener. Checkpoint
flush, subagent teardown and billing/finalization are replaced with a small
ledger update: this verifies cancellation transport, not HTTP/LLM end to end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import sys
import time
from uuid import uuid4

os.environ["LANGSMITH_TRACING"] = "false"
os.environ["LANGCHAIN_TRACING_V2"] = "false"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import psycopg
from psycopg import sql
import redis.asyncio as redis


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


async def _wait_stage(path: Path, stage: str, timeout: float = 20) -> dict:
    async with asyncio.timeout(timeout):
        while True:
            if path.exists():
                value = json.loads(path.read_text(encoding="utf-8"))
                if value.get("stage") == stage:
                    return value
                if value.get("stage") == "failed":
                    raise RuntimeError("worker probe failed")
            await asyncio.sleep(0.02)


async def _worker(args) -> None:
    from src.server.database import pool
    from src.server.services import workspace_status_pubsub
    from src.server.services.runs.task_registry_store import BackgroundRegistryStore
    from src.server.services.runs.cancel import TurnCancelListener
    from src.server.services.runs.executor import LocalRunExecution, LocalRunExecutor, LocalRunStatus

    dsn = os.environ[args.db_env]
    bus = redis.from_url(os.environ[args.redis_env])
    path = Path(args.output)
    command = path.with_suffix(".command.json")
    initial_read = asyncio.Event()

    @asynccontextmanager
    async def connection(conn=None):
        if conn is not None:
            yield conn
            return
        async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as current:
            await current.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(args.schema)))
            yield current
        initial_read.set()

    async def shared_bus():
        return bus

    async def noop(*_args):
        pass

    manager = LocalRunExecutor()
    manager.enable_storage = False
    LocalRunExecutor._instance = manager
    BackgroundRegistryStore._instance = BackgroundRegistryStore()
    pool.get_db_connection = connection
    workspace_status_pubsub.get_shared_pubsub_client = shared_bus
    manager._flush_checkpoint = noop
    manager._teardown_subagents_on_stop = noop

    async def finalize(thread_id, run_id, *, kind):
        info = manager.executions[(thread_id, run_id)]
        info.status = LocalRunStatus.CANCELLED
        async with connection() as conn:
            await conn.execute(
                "UPDATE conversation_responses SET status = 'cancelled' WHERE conversation_response_id = %s",
                (run_id,),
            )

    manager._finalize_run = finalize
    listener = TurnCancelListener()

    async def start(run_id):
        started, unwound = asyncio.Event(), asyncio.Event()

        async def tool_wait():
            started.set()
            try:
                await asyncio.Event().wait()
                yield "must never run past stop"
            finally:
                unwound.set()

        info = LocalRunExecution(args.thread, run_id, LocalRunStatus.RUNNING, datetime.now())
        manager.executions[(args.thread, run_id)] = info
        info.task = asyncio.create_task(manager._run_workflow(
            args.thread, run_id, tool_wait(), info.cancel_event,
        ))
        await started.wait()
        return info, unwound

    info, unwound = await start(args.target)
    successor = None
    listener.start()
    try:
        # Let startup/reconnect reconciliation finish before the other process
        # requests a stop: this must exercise the periodic lost-message path.
        async with asyncio.timeout(5):
            while not any(count for _channel, count in await bus.pubsub_numsub("turn:cancel")):
                await asyncio.sleep(0.02)
            await initial_read.wait()
            await asyncio.sleep(0.2)
        _write(path, {"stage": "ready"})
        start_time = time.monotonic()
        try:
            await asyncio.wait_for(info.task, 18)
        except asyncio.CancelledError:
            pass
        elapsed = time.monotonic() - start_time
        assert unwound.is_set() and info.cancel_event.is_set() and info.user_stop
        successor, _ = await start(args.successor)
        _write(path, {"stage": "successor_ready", "owner_wait_seconds": elapsed})
        await _wait_stage(command, "stale_sent")
        await asyncio.sleep(0.35)
        assert not successor.cancel_event.is_set() and not successor.task.done()
        _write(path, {
            "stage": "passed", "lost_nudge_stopped": True,
            "owner_wait_seconds": elapsed, "delayed_old_nudge_ignored": True,
            "scope": "two processes, real PostgreSQL/Redis, real executor and cancel listener",
            "excludes": "HTTP, model calls, checkpoint flush, billing and full finalization",
        })
    finally:
        await listener.stop()
        for active in (info, successor):
            if active is not None and active.task is not None and not active.task.done():
                await manager.signal_cancel(active.thread_id, active.run_id)
                await asyncio.gather(active.task, return_exceptions=True)
        await bus.aclose()


async def _main(args) -> dict:
    schema = "cancel_probe_" + uuid4().hex
    thread, target, successor = (str(uuid4()) for _ in range(3))
    path = Path(args.output).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    command = path.with_suffix(".command.json")
    path.unlink(missing_ok=True)
    command.unlink(missing_ok=True)
    bus = redis.from_url(os.environ[args.redis_env])
    process = None
    result = None
    async with await psycopg.AsyncConnection.connect(os.environ[args.db_env], autocommit=True) as conn:
        await bus.ping()
        await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            await conn.execute(sql.SQL(
                "CREATE TABLE {}.conversation_responses (conversation_response_id uuid PRIMARY KEY, "
                "conversation_thread_id uuid NOT NULL, status text NOT NULL, cancel_requested_at timestamptz)"
            ).format(sql.Identifier(schema)))
            await conn.execute(sql.SQL(
                "INSERT INTO {}.conversation_responses VALUES (%s, %s, 'in_progress', NULL), "
                "(%s, %s, 'in_progress', NULL)"
            ).format(sql.Identifier(schema)), (target, thread, successor, thread))
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), "--worker",
                "--db-env", args.db_env, "--redis-env", args.redis_env,
                "--schema", schema, "--thread", thread,
                "--target", target, "--successor", successor, "--output", str(path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            await _wait_stage(path, "ready")
            requested = time.monotonic()
            await conn.execute(sql.SQL(
                "UPDATE {}.conversation_responses SET cancel_requested_at = NOW() "
                "WHERE conversation_response_id = %s"
            ).format(sql.Identifier(schema)), (target,))
            # Fault injection: deliberately omit the cancel publish entirely.
            await _wait_stage(path, "successor_ready")
            stop_latency = time.monotonic() - requested
            await bus.publish("turn:cancel", json.dumps({"thread_id": thread, "run_id": target}))
            _write(command, {"stage": "stale_sent"})
            result = await _wait_stage(path, "passed")
            await asyncio.wait_for(process.wait(), 5)
            assert process.returncode == 0
            result["stop_latency_seconds"] = stop_latency
            return result
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                await process.wait()
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
            if result is not None:
                result["schema_removed"] = True
                _write(path, result)
            command.unlink(missing_ok=True)
            await bus.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-env", default="CONDUCTOR_PROBE_DB_URI")
    parser.add_argument("--redis-env", default="CONDUCTOR_PROBE_REDIS_URL")
    parser.add_argument("--output", required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    for field in ("schema", "thread", "target", "successor"):
        parser.add_argument(f"--{field}", help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.worker:
        try:
            asyncio.run(_worker(arguments))
        except BaseException as exc:
            _write(Path(arguments.output), {"stage": "failed", "error_type": type(exc).__name__})
            raise
    else:
        print(json.dumps(asyncio.run(_main(arguments)), indent=2))
