"""Deliberately dishonest adapters. Each one passed the suite before it was tightened.

The contract checks in ``test_contract.py`` are run against these and must
raise. They subclass the in-process reference and change one thing.
"""

from __future__ import annotations

import copy
from typing import Any, ClassVar

from prototypes.superagent.contract import (
    FAKE_TOOL_HANDLERS,
    CreditDecision,
    ScriptedChunk,
    StreamEvent,
    Transcript,
    TranscriptEntry,
    scripted_tool_call_text,
)
from prototypes.superagent.reference.adapter import ReferenceSuperAgent

_DOUBLES = ("model", "store", "credit_gate", "memory", "subscriber")


class _Cheater(ReferenceSuperAgent):
    @classmethod
    def reset(cls) -> None:
        """Clear class-level state; called once per factory."""


def cheat_factory(cls: type[_Cheater]):
    """A ``make_adapter`` stand-in that builds ``cls`` from the doubles."""

    cls.reset()

    def factory(*, gate: Any = None, **deps: Any) -> _Cheater:
        del gate
        return cls(**{name: deps[name] for name in _DOUBLES})

    return factory


class StoreBypassCheater(_Cheater):
    """Commits only the session row. Transcript and events live in process memory."""

    _rows: ClassVar[dict[str, dict[str, list[dict[str, Any]]]]] = {}

    @classmethod
    def reset(cls) -> None:
        cls._rows = {}

    def _commit(self, session, next_seq, status, transcript_rows, event_rows):
        bucket = self._rows.setdefault(
            str(session["session_id"]), {"transcript": [], "events": []}
        )
        bucket["transcript"].extend(copy.deepcopy(transcript_rows))
        bucket["events"].extend(copy.deepcopy(event_rows))
        with self.store.transaction() as txn:
            current = txn.get_session(str(session["session_id"]))
            current["next_seq"] = next_seq
            current["status"] = status
            txn.upsert_session(current)

    def transcript(self, session_id: str) -> Transcript:
        rows = self._rows.get(session_id, {}).get("transcript", [])
        return Transcript(
            session_id=session_id,
            entries=[TranscriptEntry(r["seq"], r["kind"], r["text"]) for r in rows],
        )

    def events(self, session_id: str) -> list[StreamEvent]:
        rows = self._rows.get(session_id, {}).get("events", [])
        return [StreamEvent(r["seq"], r["kind"], r["text"]) for r in rows]


class CacheHistoryCheater(_Cheater):
    """Writes the store honestly but rebuilds history from a process cache."""

    _cache: ClassVar[dict[str, Transcript]] = {}

    @classmethod
    def reset(cls) -> None:
        cls._cache = {}

    def transcript(self, session_id: str) -> Transcript:
        if session_id in self._cache:
            return copy.deepcopy(self._cache[session_id])
        return super().transcript(session_id)

    def _commit(self, session, *args, **kwargs):
        super()._commit(session, *args, **kwargs)
        sid = str(session["session_id"])
        self._cache[sid] = super().transcript(sid)


class StaleStoreCheater(_Cheater):
    """Keeps the first store it saw (a long-lived process) and ignores the new one."""

    _first: ClassVar[Any] = None

    @classmethod
    def reset(cls) -> None:
        cls._first = None

    def __init__(self, **deps: Any) -> None:
        super().__init__(**deps)
        if type(self)._first is None:
            type(self)._first = self.store
        self.store = type(self)._first


class ExtraBuiltinsCheater(_Cheater):
    """Reports the role list from ``tool_names`` but offers the model CLI built-ins too."""

    def offered_tools(self, role: str):
        return [*super().offered_tools(role), {"name": "Bash"}, {"name": "Read"}]


class UnionToolsCheater(_Cheater):
    """Offers every autonomy tool to every role."""

    def offered_tools(self, role: str):
        names = dict.fromkeys(
            [*self.tool_names("router"), *self.tool_names("watchdog")]
        )
        return [{"name": name} for name in names]


class NoToolListCheater(_Cheater):
    """Never tells the model which tools exist (the pre-fix reference)."""

    def offered_tools(self, role: str):
        return None


class NativeBuiltinCheater(_Cheater):
    """Lets a CLI's capitalized built-ins run natively; refuses only lowercase names."""

    def _handle_tool(self, chunk: ScriptedChunk, *, role, messages, emit) -> None:
        name = chunk.tool_name
        if name != name.casefold() and not name.startswith("mcp__"):
            call_text = scripted_tool_call_text(name, chunk.tool_args)
            emit("tool_call", call_text)
            emit("tool_result", f"ran {name} natively")
            messages.append({"role": "tool", "name": name, "content": "ran"})
            return
        super()._handle_tool(chunk, role=role, messages=messages, emit=emit)


class PreflightOnlyCreditCheater(_Cheater):
    """Checks credit once per turn, before the first round, and never again."""

    def __init__(self, **deps: Any) -> None:
        super().__init__(**deps)
        self._real_gate = self.credit_gate
        self.credit_gate = self
        self._checked = False

    def authorize(self, *, user_id: str, session_id: str) -> CreditDecision:
        if self._checked:
            return CreditDecision(allowed=True)
        self._checked = True
        return self._real_gate.authorize(user_id=user_id, session_id=session_id)

    async def turn(self, session_id: str, user_text: str):
        self._checked = False
        return await super().turn(session_id, user_text)


class OutOfRoleDispatchCheater(_Cheater):
    """Runs any autonomy tool the model names, whatever the session's role."""

    def _handle_tool(self, chunk: ScriptedChunk, *, role, messages, emit) -> None:
        name = chunk.tool_name
        handler = FAKE_TOOL_HANDLERS.get(name)
        if handler is None:
            super()._handle_tool(chunk, role=role, messages=messages, emit=emit)
            return
        call_text = scripted_tool_call_text(name, chunk.tool_args)
        result = handler(dict(chunk.tool_args))
        emit("tool_call", call_text)
        emit("tool_result", result)
        messages.append({"role": "assistant", "content": call_text})
        messages.append({"role": "tool", "name": name, "content": result})
