"""Frame- and document-scoped references for semantic snapshots."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any


class StaleReferenceError(ValueError):
    """A reference is absent from the latest snapshot or its node was removed."""


@dataclass(slots=True)
class _Entry:
    frame_id: str
    document_generation: int
    node_id: str
    handle: Any
    resources: tuple[Any, ...] = ()


async def _dispose(handles: list[Any]) -> None:
    seen: set[int] = set()
    for handle in handles:
        if id(handle) in seen:
            continue
        seen.add(id(handle))
        try:
            await handle.dispose()
        except Exception:
            pass


class ReferenceRegistry:
    """Keep handles only for the latest snapshot while preserving stable keys."""

    def __init__(self) -> None:
        self._frame_ids: dict[int, tuple[Any, str]] = {}
        self._document_ids: dict[str, tuple[str, int]] = {}
        self._entries: dict[str, _Entry] = {}

    def frame_id(self, frame: Any) -> str:
        key = id(frame)
        current = self._frame_ids.get(key)
        if current is None or current[0] is not frame:
            current = (frame, secrets.token_urlsafe(9))
            self._frame_ids[key] = current
        return current[1]

    def retain_frames(self, frames: list[Any]) -> None:
        live_ids = {id(frame) for frame in frames}
        for key in list(self._frame_ids):
            if key not in live_ids:
                _frame, frame_id = self._frame_ids.pop(key)
                self._document_ids.pop(frame_id, None)

    def document_generation(self, frame_id: str, document_token: str) -> int:
        previous = self._document_ids.get(frame_id)
        if previous is None:
            generation = 1
        elif previous[0] == document_token:
            generation = previous[1]
        else:
            generation = previous[1] + 1
        self._document_ids[frame_id] = (document_token, generation)
        return generation

    async def replace_snapshot(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        old_handles = [handle for entry in self._entries.values() for handle in (entry.handle, *entry.resources)]
        entries: dict[str, _Entry] = {}
        rendered: list[dict[str, Any]] = []
        for row in rows:
            frame_id = str(row["frame_id"])
            generation = int(row["document_generation"])
            node_id = str(row["node_id"])
            ref = f"d{generation}.{frame_id}.{node_id}"
            entries[ref] = _Entry(frame_id, generation, node_id, row["handle"], tuple(row.get("resources", ())))
            rendered.append({**row["summary"], "frame": frame_id, "ref": ref})
        self._entries = entries
        retained = {id(handle) for entry in entries.values() for handle in (entry.handle, *entry.resources)}
        await _dispose([handle for handle in old_handles if id(handle) not in retained])
        return rendered

    async def resolve(self, ref: str) -> Any:
        entry = self._entries.get(ref)
        if entry is None:
            raise StaleReferenceError("Reference is stale; take a fresh snapshot")
        try:
            connected = await entry.handle.evaluate("element => element.isConnected")
        except Exception:
            connected = False
        if not connected:
            self._entries.pop(ref, None)
            raise StaleReferenceError("Reference is stale; take a fresh snapshot")
        return entry.handle

    def entry_for(self, ref: str) -> tuple[str, int] | None:
        entry = self._entries.get(ref)
        return (entry.frame_id, entry.document_generation) if entry is not None else None

    async def clear(self) -> None:
        old_handles = [handle for entry in self._entries.values() for handle in (entry.handle, *entry.resources)]
        self._entries.clear()
        self._frame_ids.clear()
        self._document_ids.clear()
        await _dispose(old_handles)
