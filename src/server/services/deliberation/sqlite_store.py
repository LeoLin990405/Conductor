"""Offline JSON deliberation store. Same methods as MemoryDeliberationStore."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from src.server.services.deliberation.errors import DeliberationError

_LIST_FIELDS = (
    "participants",
    "rounds",
    "messages",
    "votes",
    "dissents",
    "steps",
)
_UPDATABLE = frozenset(
    {
        "status",
        "round_index",
        "consensus_plan",
        "decision_reason",
        "thread_key",
        "planner_agent_key",
        "updated_at",
        "max_rounds",
        "objective",
        "parent_deliberation_id",
    }
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=_json_default, ensure_ascii=False)


class SqliteDeliberationStore:
    """JSON-blob store with the same methods as MemoryDeliberationStore. Offline from Postgres. For tests and scripts/deliberation_demo.py."""

    def __init__(self, path: str = ":memory:") -> None:
        # One connection for the instance lifetime. :memory: is per connection.
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS deliberation_state (
                deliberation_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def _read(self, deliberation_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM deliberation_state WHERE deliberation_id = ?",
            (deliberation_id,),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    def _write(self, deliberation_id: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            """
            UPDATE deliberation_state
            SET payload = ?
            WHERE deliberation_id = ?
            """,
            (_dump(payload), deliberation_id),
        )
        self._conn.commit()

    def _require(self, deliberation_id: str) -> dict[str, Any]:
        row = self._read(deliberation_id)
        if row is None:
            raise DeliberationError(
                "not_found",
                f"deliberation {deliberation_id} not found",
            )
        return row

    async def create(self, row: dict) -> dict:
        if not isinstance(row, dict):
            raise DeliberationError("invalid", "deliberation row is required")
        deliberation_id = row.get("deliberation_id")
        if not isinstance(deliberation_id, str) or not deliberation_id:
            raise DeliberationError("invalid", "deliberation_id is required")
        stored = copy.deepcopy(row)
        for name in _LIST_FIELDS:
            if not isinstance(stored.get(name), list):
                stored[name] = []
        try:
            self._conn.execute(
                "INSERT INTO deliberation_state (deliberation_id, payload) VALUES (?, ?)",
                (deliberation_id, _dump(stored)),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            self._conn.rollback()
            raise DeliberationError(
                "already_exists",
                f"deliberation {deliberation_id} already exists",
            ) from exc
        return copy.deepcopy(stored)

    async def save_participant(self, row: dict) -> dict:
        return await self._append("participants", row)

    async def save_round(self, row: dict) -> dict:
        return await self._append("rounds", row)

    async def mark_round_closed(self, round_id: str) -> None:
        rows = self._conn.execute(
            "SELECT deliberation_id, payload FROM deliberation_state"
        ).fetchall()
        for deliberation_id, raw in rows:
            parent = json.loads(raw)
            for rnd in parent.get("rounds") or []:
                if rnd.get("round_id") != round_id:
                    continue
                rnd["status"] = "closed"
                if not rnd.get("closed_at"):
                    rnd["closed_at"] = _now()
                self._write(deliberation_id, parent)
                return
        raise DeliberationError("not_found", f"round {round_id} not found")

    async def request_cancel(self, deliberation_id: str) -> bool:
        parent = self._read(deliberation_id)
        if parent is None or parent.get("status") not in {"draft", "running"}:
            return False
        parent["status"] = "cancelled"
        parent["updated_at"] = _now()
        self._write(deliberation_id, parent)
        return True

    async def claim_start(self, deliberation_id: str) -> bool:
        parent = self._read(deliberation_id)
        if parent is None or parent.get("status") != "draft":
            return False
        parent["status"] = "running"
        parent["round_index"] = 0
        parent["updated_at"] = _now()
        self._write(deliberation_id, parent)
        return True

    async def fail_stale_running(
        self, deliberation_id: str, stale_seconds: int = 1800
    ) -> bool:
        parent = self._read(deliberation_id)
        if parent is None or parent.get("status") != "running":
            return False
        updated_at = parent.get("updated_at") or parent.get("created_at")
        if not isinstance(updated_at, str) or not updated_at:
            return False
        try:
            timestamp = datetime.fromisoformat(updated_at)
        except ValueError:
            return False
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - timestamp).total_seconds() <= stale_seconds:
            return False
        parent["status"] = "failed"
        parent["decision_reason"] = (
            "worker lease expired; retry creates a new deliberation"
        )
        parent["updated_at"] = _now()
        self._write(deliberation_id, parent)
        return True

    async def finalize(
        self,
        deliberation_id: str,
        *,
        status: str,
        round_index: int,
        consensus_plan: dict,
        decision_reason: str,
        steps: list[dict],
    ) -> bool:
        parent = self._read(deliberation_id)
        if parent is None or parent.get("status") != "running":
            return False
        parent["status"] = status
        parent["round_index"] = round_index
        parent["consensus_plan"] = copy.deepcopy(consensus_plan)
        parent["decision_reason"] = decision_reason
        parent["steps"] = copy.deepcopy(steps)
        parent["updated_at"] = _now()
        self._write(deliberation_id, parent)
        return True

    async def fail_running(self, deliberation_id: str, reason: str) -> bool:
        parent = self._read(deliberation_id)
        if parent is None or parent.get("status") != "running":
            return False
        parent["status"] = "failed"
        parent["decision_reason"] = reason[:500]
        parent["updated_at"] = _now()
        self._write(deliberation_id, parent)
        return True

    async def get_status(self, deliberation_id: str) -> str | None:
        parent = self._read(deliberation_id)
        return None if parent is None else str(parent.get("status") or "")

    async def touch_running(self, deliberation_id: str) -> bool:
        parent = self._read(deliberation_id)
        if parent is None or parent.get("status") != "running":
            return False
        parent["updated_at"] = _now()
        self._write(deliberation_id, parent)
        return True

    async def save_message(self, row: dict) -> dict:
        return await self._append("messages", row)

    async def save_vote(self, row: dict) -> dict:
        return await self._append("votes", row)

    async def save_dissent(self, row: dict) -> dict:
        return await self._append("dissents", row)

    async def save_steps(self, deliberation_id: str, steps: list[dict]) -> None:
        parent = self._require(deliberation_id)
        parent["steps"] = copy.deepcopy(steps)
        self._write(deliberation_id, parent)

    async def update(self, deliberation_id: str, **fields: Any) -> dict:
        parent = self._require(deliberation_id)
        for key, value in fields.items():
            if key in _UPDATABLE:
                parent[key] = copy.deepcopy(value)
        self._write(deliberation_id, parent)
        return copy.deepcopy(parent)

    async def load(self, deliberation_id: str) -> dict | None:
        row = self._read(deliberation_id)
        if row is None:
            return None
        return copy.deepcopy(row)

    async def _append(self, field: str, row: dict) -> dict:
        if not isinstance(row, dict):
            raise DeliberationError("not_found", "deliberation not found")
        deliberation_id = row.get("deliberation_id")
        if not isinstance(deliberation_id, str):
            raise DeliberationError("not_found", "deliberation not found")
        parent = self._require(deliberation_id)
        stored = copy.deepcopy(row)
        parent[field].append(stored)
        self._write(deliberation_id, parent)
        return copy.deepcopy(stored)
