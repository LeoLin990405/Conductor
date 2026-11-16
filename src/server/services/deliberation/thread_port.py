"""Thread seam. Shared-thread context stays behind this port."""

from __future__ import annotations

import copy
from typing import Protocol


class DeliberationThreadPort(Protocol):
    async def open(
        self,
        *,
        deliberation_id: str,
        ticket_id: str,
        participant_keys: list[str],
    ) -> str: ...

    async def append(
        self,
        thread_key: str,
        *,
        author: str,
        kind: str,
        body: str,
        round_index: int,
        event_key: str | None = None,
    ) -> None: ...

    async def read(self, thread_key: str) -> list[dict]: ...


class MemoryThreadPort:
    """In-memory thread. ``open`` returns ``delib:{deliberation_id}``."""

    def __init__(self) -> None:
        self._threads: dict[str, list[dict]] = {}

    async def open(
        self,
        *,
        deliberation_id: str,
        ticket_id: str,
        participant_keys: list[str],
    ) -> str:
        del ticket_id, participant_keys
        key = f"delib:{deliberation_id}"
        self._threads.setdefault(key, [])
        return key

    async def append(
        self,
        thread_key: str,
        *,
        author: str,
        kind: str,
        body: str,
        round_index: int,
        event_key: str | None = None,
    ) -> None:
        self._threads.setdefault(thread_key, []).append(
            {
                "author": author,
                "kind": kind,
                "body": body,
                "round_index": round_index,
                "event_key": event_key,
            }
        )

    async def read(self, thread_key: str) -> list[dict]:
        return copy.deepcopy(self._threads.get(thread_key, []))


class DatabaseTicketThreadPort:
    """Persist deliberation turns on the owning autonomy Ticket's shared thread."""

    async def open(
        self,
        *,
        deliberation_id: str,
        ticket_id: str,
        participant_keys: list[str],
    ) -> str:
        del deliberation_id, participant_keys
        from src.server.database.autonomy import tickets

        ticket = await tickets.get_ticket(ticket_id)
        if ticket is None:
            raise ValueError("Deliberation root Ticket was not found")
        thread_key = str(ticket.get("origin_thread_key") or f"main-agent:{ticket_id}")
        await tickets.add_subscription(ticket_id, thread_key, kind="own")
        return thread_key

    async def append(
        self,
        thread_key: str,
        *,
        author: str,
        kind: str,
        body: str,
        round_index: int,
        event_key: str | None = None,
    ) -> None:
        from src.server.database.main_agent_bridge import append_shared_thread_message

        await append_shared_thread_message(
            thread_key=thread_key,
            author=author,
            kind=kind,
            body=body,
            round_index=round_index,
            event_key=event_key,
        )

    async def read(self, thread_key: str) -> list[dict]:
        from src.server.database.main_agent_bridge import read_shared_thread_messages

        return await read_shared_thread_messages(thread_key)
