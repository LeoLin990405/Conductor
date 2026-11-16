"""Planner, assigner and reviewer as explicit service roles (R6 L3, N2 item 5).

The Super-agent's chain for a dispatch is plan -> assign -> review. Before R6
the three steps were fixed code paths with nothing tying them together; here
each one is a *service call* the Router makes, recorded on the dispatch as
``gates["service_chain"]``: role, action, model, session, step, start and end
time, outcome. The chain is bounded (``CHAIN_MAX`` newest entries) and holds
no task text, prompts or verdict prose -- only who did what, when, and how it
ended (a reviewer entry records that a verdict came back, never which) -- so
it can be shown to anyone who can read the dispatch. It is recorded only with
a stored role registry or ``CONDUCTOR_DISPATCH_SERVICE_CHAIN=1``.

The recorders instrument the existing collaborators in place, so their types
do not change: a planner's ``plan``, a selector's ``select`` (the assign
step, WORK and REVIEW picks) and a reviewer's ``review_line`` return exactly
what they returned before, and an exception still propagates after being
recorded.
"""

from __future__ import annotations

import functools
import inspect
import time
from dataclasses import dataclass
from typing import Any, Callable

CHAIN_KEY = "service_chain"
CHAIN_MAX = 200
ROLE_PLANNER = "planner"
ROLE_ROUTER = "router"
ROLE_REVIEWER = "reviewer"
ACTION_PLAN = "plan"
ACTION_ASSIGN = "assign"
ACTION_REVIEW = "review"
GATES_SCHEMA = "dispatch_gates_v1"


CHAIN_ENV = "CONDUCTOR_DISPATCH_SERVICE_CHAIN"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def chain_enabled() -> bool:
    """Recorded only with a stored role registry or ``SERVICE_CHAIN=1``.

    With neither, the dispatch row keeps main's shape (review fix 3).
    """
    import os

    from src.server.services.autonomy.role_types import stored_registry

    if str(os.environ.get(CHAIN_ENV, "") or "").strip().lower() in _TRUTHY:
        return True
    return stored_registry() is not None


@dataclass
class ServiceCall:
    role: str
    action: str
    model: str
    session: str
    step: int
    started_at: float
    ended_at: float | None = None
    outcome: str = "running"
    subject: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "action": self.action,
            "model": self.model,
            "session": self.session,
            "step": self.step,
            "subject": self.subject,
            "started_at": round(self.started_at, 3),
            "ended_at": None if self.ended_at is None else round(self.ended_at, 3),
            "outcome": self.outcome,
        }


class ServiceChain:
    """The bounded, auditable list of service calls for one dispatch."""

    def __init__(self, dispatch: Any, *, clock: Callable[[], float] = time.time) -> None:
        self._dispatch = dispatch
        self._clock = clock

    @property
    def session(self) -> str:
        return f"dispatch:{getattr(self._dispatch, 'id', '')}"

    def _entries(self) -> list[dict[str, Any]] | None:
        if not chain_enabled():
            return None
        gates = getattr(self._dispatch, "gates", None)
        if not isinstance(gates, dict) or gates.get("schema_version") != GATES_SCHEMA:
            return None
        entries = gates.get(CHAIN_KEY)
        if not isinstance(entries, list):
            entries = []
            gates[CHAIN_KEY] = entries
        return entries

    def entries(self) -> list[dict[str, Any]]:
        return list(self._entries() or [])

    def start(self, role: str, action: str, model: str, subject: str = "") -> ServiceCall:
        entries = self._entries()
        step = (int(entries[-1].get("step") or 0) + 1) if entries else 1
        return ServiceCall(
            role=role,
            action=action,
            model=str(model or ""),
            session=self.session,
            step=step,
            started_at=self._clock(),
            subject=subject,
        )

    def finish(self, call: ServiceCall, outcome: str) -> None:
        call.ended_at = self._clock()
        call.outcome = outcome
        entries = self._entries()
        if entries is None:
            return
        entries.append(call.to_dict())
        del entries[:-CHAIN_MAX]


def _outcome_of(result: Any) -> str:
    if result is None:
        return "none"
    passed = getattr(result, "passed", None)
    if isinstance(passed, bool):
        return "merge" if passed else "block"
    if isinstance(result, list):
        return f"lines:{len(result)}"
    return "ok"


def instrument_planner(planner: Any, chain: ServiceChain, *, model: str = "") -> Any:
    """Record each ``plan`` as the planner service call; returns ``planner``.

    The instance keeps its type (callers check ``isinstance``); only the
    bound ``plan`` is replaced, and it returns and raises exactly as before.
    """
    original = planner.plan

    @functools.wraps(original)
    async def plan(goal: str, *args: Any, **kwargs: Any) -> Any:
        call = chain.start(ROLE_PLANNER, ACTION_PLAN, model)
        try:
            result = await original(goal, *args, **kwargs)
        except BaseException as exc:
            chain.finish(call, f"error:{type(exc).__name__}")
            raise
        chain.finish(call, _outcome_of(result))
        return result

    planner.plan = plan
    return planner


def instrument_reviewer(reviewer: Any, chain: ServiceChain) -> Any:
    """Record each ``review_line`` as the reviewer service call; returns ``reviewer``."""
    original = reviewer.review_line

    @functools.wraps(original)
    async def review_line(line_id: str, model: str, *args: Any, **kwargs: Any) -> Any:
        call = chain.start(ROLE_REVIEWER, ACTION_REVIEW, model, subject=line_id)
        try:
            result = await original(line_id, model, *args, **kwargs)
        except BaseException as exc:
            chain.finish(call, f"error:{type(exc).__name__}")
            raise
        # R6 L5 review fix 4: never the verdict -- the chain is readable while
        # a secondary review runs, and a conclusion must not reach it.
        chain.finish(call, "returned")
        return result

    reviewer.review_line = review_line
    return reviewer


def instrument_selector(selector: Any, chain: ServiceChain) -> Any:
    """Record each ``select`` as the Router's assign step; returns ``selector``.

    The bound method is replaced on the instance (``functools.wraps`` keeps
    the signature the machine inspects), so every attribute the machine reads
    or sets on the selector still lives on the selector itself.
    """
    original = selector.select

    @functools.wraps(original)
    async def select(line: Any, used_models: Any, *args: Any, **kwargs: Any) -> Any:
        role = kwargs.get("role")
        role_name = str(getattr(role, "value", role) or "work")
        call = chain.start(ROLE_ROUTER, ACTION_ASSIGN, "", subject=f"{getattr(line, 'id', '')}:{role_name}")
        try:
            result = original(line, used_models, *args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except BaseException as exc:
            chain.finish(call, f"error:{type(exc).__name__}")
            raise
        call.model = str(result or "")
        chain.finish(call, "chosen" if result else "none")
        return result

    selector.select = select
    return selector
