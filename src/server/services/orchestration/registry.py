"""Unified worker registry for PTC orchestration.

Subagent tasks already have a durable DB ledger; this registry adds the same
first-class identity and lifecycle for delegated native-CLI workers, so the
frontend can render one worker model and PTC can reason about one plan.

Phase 1 is intentionally in-process: live native delegations are visible while
the backend that runs them is alive. The durable subagent ledger remains the
source of truth for Task workers; a later phase persists native rows too.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable

_MAX_WORKERS_PER_THREAD = 64
_MAX_SNAPSHOT_TTL = 7 * 24 * 3600
_lock = asyncio.Lock()
_workers: dict[str, dict[str, "WorkerRecord"]] = {}
_cancel_handles: dict[str, Callable[[], Any]] = {}


def _key(thread_id: str) -> str:
    return f"orchestration:thread:{thread_id}"


async def _persist(thread_id: str) -> None:
    """Best-effort Redis copy so a page reload/other worker sees the workers."""
    try:
        from src.utils.cache.redis_cache import get_cache_client

        client = await get_cache_client()
        rows = [record.to_dict() for record in _workers.get(thread_id, {}).values()]
        await client.set(_key(thread_id), rows, ttl=_MAX_SNAPSHOT_TTL)
    except Exception:  # noqa: BLE001 - Redis is a view cache, not the executor
        pass


async def _persist_db(record: WorkerRecord) -> None:
    try:
        from . import db

        await db.upsert(record.to_dict())
    except Exception:  # noqa: BLE001
        pass


async def _hydrate(thread_id: str) -> None:
    if _workers.get(thread_id):
        return
    try:
        from . import db

        rows = await db.list_for_thread(thread_id)
    except Exception:  # noqa: BLE001
        rows = []
    if rows:
        bucket: dict[str, WorkerRecord] = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            row = dict(row)
            row.pop("updated_at", None)
            try:
                bucket[row["id"]] = WorkerRecord(**row)
            except TypeError:
                continue
        if bucket:
            _workers[thread_id] = bucket
            return
    try:
        from src.utils.cache.redis_cache import get_cache_client

        client = await get_cache_client()
        rows = await client.get(_key(thread_id))
    except Exception:  # noqa: BLE001
        return
    if not isinstance(rows, list):
        return
    bucket: dict[str, WorkerRecord] = {}
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        try:
            bucket[row["id"]] = WorkerRecord(**row)
        except TypeError:
            continue
    if bucket:
        _workers[thread_id] = bucket


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerKind(str, Enum):
    SUBAGENT = "subagent"
    NATIVE = "native"
    DIRECT = "direct"


class WorkerStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WorkerRecord:
    id: str
    thread_id: str
    kind: str
    provider: str
    label: str
    status: str = WorkerStatus.QUEUED.value
    parent_id: str = ""
    depends_on: list[str] = field(default_factory=list)
    prompt_preview: str = ""
    result_preview: str = ""
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def start_worker(
    *,
    thread_id: str,
    kind: WorkerKind | str,
    provider: str = "",
    label: str = "",
    prompt: str = "",
    parent_id: str = "",
    depends_on: list[str] | None = None,
    meta: dict[str, Any] | None = None,
) -> str:
    worker_id = f"w-{uuid.uuid4().hex[:10]}"
    record = WorkerRecord(
        id=worker_id,
        thread_id=thread_id or "default",
        kind=WorkerKind(kind).value,
        provider=provider,
        label=label or provider or "worker",
        status=WorkerStatus.RUNNING.value,
        parent_id=parent_id,
        depends_on=list(depends_on or []),
        prompt_preview=" ".join(str(prompt or "").split())[:240],
        started_at=_now(),
        meta=dict(meta or {}),
    )
    async with _lock:
        bucket = _workers.setdefault(record.thread_id, {})
        bucket[worker_id] = record
        while len(bucket) > _MAX_WORKERS_PER_THREAD:
            bucket.pop(next(iter(bucket)), None)
    await _persist(record.thread_id)
    await _persist_db(record)
    return worker_id


def register_cancel(worker_id: str, callback: Callable[[], Any]) -> None:
    """Register the task/process cancellation hook owned by a live worker."""
    if worker_id:
        _cancel_handles[worker_id] = callback


def unregister_cancel(worker_id: str) -> None:
    _cancel_handles.pop(worker_id, None)


async def cancel_worker(worker_id: str) -> bool:
    """Invoke a live native worker's cancellation hook, if one is registered."""
    callback = _cancel_handles.pop(worker_id, None)
    if callback is None:
        return False
    try:
        result = callback()
        if asyncio.iscoroutine(result):
            await result
        return True
    except Exception:  # noqa: BLE001 - cancellation is best effort
        return False


async def finish_worker(
    worker_id: str,
    *,
    status: WorkerStatus | str,
    result: str = "",
    error: str = "",
) -> None:
    async with _lock:
        for bucket in _workers.values():
            record = bucket.get(worker_id)
            if record is None:
                continue
            record.status = WorkerStatus(status).value
            record.result_preview = " ".join(str(result or "").split())[:500]
            record.error = str(error or "")[:500]
            record.finished_at = _now()
            await _persist(record.thread_id)
            await _persist_db(record)
            return


async def snapshot(thread_id: str) -> list[dict[str, Any]]:
    async with _lock:
        await _hydrate(thread_id)
        bucket = _workers.get(thread_id) or {}
        return [record.to_dict() for record in bucket.values()]
