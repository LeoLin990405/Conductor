"""In-memory stand-in for ``src.server.database.autonomy.daemon_output``.

It reproduces the properties the service relies on, not the SQL: one lock per
ticket (``SELECT ... FOR UPDATE``), a decision made on a *copy* of the locked
row, and an all-or-nothing apply (a failure injected anywhere rolls back the
session update and the event inserts together). A primary-key check on
``(ticket_id, epoch, seq)`` mimics the events table.
"""

from __future__ import annotations

import asyncio
import copy
from collections import defaultdict
from typing import Any


class FakeStoreError(RuntimeError):
    """Raised for an injected database failure."""


class FakeOutputDb:
    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.events: dict[tuple[str, int], dict[int, dict[str, Any]]] = defaultdict(
            dict
        )
        self.live: set[tuple[str, str, str]] = set()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.fail_before_commit: Exception | None = None
        self.fail_reads: Exception | None = None
        self.commits = 0
        self.deciders = 0
        # The database-wide epoch sequence: never reset, not even when a
        # session row is deleted and recreated.
        self.epoch_seq = 0

    def allow(self, daemon_id: str, ticket_id: str, lease_id: str) -> None:
        self.live.add((daemon_id, ticket_id, lease_id))

    def revoke(self, daemon_id: str, ticket_id: str, lease_id: str) -> None:
        self.live.discard((daemon_id, ticket_id, lease_id))

    @staticmethod
    def _fresh(ticket_id: str) -> dict[str, Any]:
        return {
            "ticket_id": ticket_id,
            "epoch": 1,
            "lease_id": None,
            "run_id": "",
            "next_seq": 0,
            "last_batch": 0,
            "tainted": False,
            "taint_reason": "",
            "guard_tail": "",
            "guard_state": {},
        }

    def delete_session(self, ticket_id: str) -> None:
        """Drop the session and (by cascade) its events, as a row delete would."""
        self.sessions.pop(ticket_id, None)
        for key in [k for k in self.events if k[0] == ticket_id]:
            del self.events[key]

    async def commit_batch(self, *, daemon_id, ticket_id, lease_id, allocate, decide):
        async with self._locks[ticket_id]:
            # Yield while holding the row lock so an unserialized second writer
            # would interleave here.
            await asyncio.sleep(0)
            if (daemon_id, ticket_id, lease_id) not in self.live:
                return False, None
            self.sessions.setdefault(ticket_id, self._fresh(ticket_id))
            row = copy.deepcopy(self.sessions[ticket_id])
            self.deciders += 1
            new_epoch = None
            if allocate:
                self.epoch_seq += 1
                new_epoch = self.epoch_seq
            plan = decide(row, new_epoch)
            if not plan.write:
                return True, plan
            new_events = {
                key: dict(value) for key, value in self.events.items()
            }
            session = plan.session
            if plan.purge_older_epochs:
                for key in [k for k in new_events if k[0] == ticket_id and k[1] < session["epoch"]]:
                    del new_events[key]
            bucket = new_events.setdefault((ticket_id, session["epoch"]), {})
            for seq, data in plan.rows:
                if seq in bucket:
                    raise FakeStoreError("duplicate key (ticket_id, epoch, seq)")
                bucket[seq] = copy.deepcopy(data)
            if plan.trim_through > 0:
                for seq in [s for s in bucket if s <= plan.trim_through]:
                    del bucket[seq]
            await asyncio.sleep(0)
            if self.fail_before_commit is not None:
                raise self.fail_before_commit  # rollback: nothing below ran
            self.sessions[ticket_id] = {
                **row,
                **{key: copy.deepcopy(value) for key, value in session.items()},
            }
            self.events = defaultdict(dict, new_events)
            self.commits += 1
            return True, plan

    async def read_session(self, ticket_id):
        if self.fail_reads is not None:
            raise self.fail_reads
        row = self.sessions.get(ticket_id)
        return copy.deepcopy(row) if row else None

    async def read_events(self, ticket_id, epoch, after_seq, limit):
        if self.fail_reads is not None:
            raise self.fail_reads
        bucket = self.events.get((ticket_id, epoch), {})
        return [
            (seq, copy.deepcopy(bucket[seq]))
            for seq in sorted(bucket)
            if seq > after_seq
        ][:limit]

    # -- helpers for assertions ------------------------------------------

    def stored_text(self, ticket_id: str, epoch: int | None = None) -> str:
        current = epoch if epoch is not None else self.sessions[ticket_id]["epoch"]
        bucket = self.events.get((ticket_id, current), {})
        return "".join(
            str(bucket[seq].get("text") or "")
            for seq in sorted(bucket)
            if bucket[seq].get("kind") in {"text", "reasoning"}
        )

    def kinds(self, ticket_id: str, epoch: int | None = None) -> list[str]:
        current = epoch if epoch is not None else self.sessions[ticket_id]["epoch"]
        bucket = self.events.get((ticket_id, current), {})
        return [str(bucket[seq].get("kind")) for seq in sorted(bucket)]
