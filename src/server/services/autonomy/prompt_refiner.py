"""Prompt refiner: rewrite a request, then wait for the user to confirm it.

A request the user only half-specified would burn a Worker turn guessing. The
refiner rewrites it once — through a one-shot model call that sees only the
request text, with no tools, no repository path and no write access — and
parks the Ticket with that rewrite stored. Nothing runs until a human
confirms the rewrite (or rejects it and keeps the original): the rewrite is a
proposal, never an instruction a Worker sees on its own.

``AUTONOMY_PROMPT_REFINE`` selects the mode, default ``off``:

- ``off`` — the default; no request is rewritten and nothing changes.
- ``vague`` — only requests the shared clarity heuristic scores below the
  clarification threshold are rewritten.
- ``always`` — every request on an interactive entry is rewritten.

Unattended entries (channel sources in ``intake``, due schedules in the
scheduler pass) never auto-refine: nobody is there to confirm a rewrite, so
``hold_gate`` parks the request with its original text instead and an
operator confirms or rejects it later. A ``/raw`` prefix or an explicit
``no_refine`` flag skips the refiner for one request.

The gate reuses the clarification gate's parked state (``awaiting_clarification``
plus one stored draft), so the scheduler's queued-only claim already holds
execution and the confirm/reject endpoints below are the only ways out. A
failed or missing refiner degrades to the original request marked
``refine_degraded`` — it never blocks the Ticket.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Protocol

from pydantic import BaseModel, Field

from src.server.database.autonomy import clarifications as clarification_db
from src.server.database.autonomy import tickets as ticket_db
from src.server.services.autonomy.clarify import (
    _ticket_gate_lock,
    clarity_threshold,
    confirm_clarification,
    heuristic_clarity_score,
)

logger = logging.getLogger(__name__)

REFINE_OFF = "off"
REFINE_VAGUE = "vague"
REFINE_ALWAYS = "always"
REFINE_MODES = (REFINE_OFF, REFINE_VAGUE, REFINE_ALWAYS)

# The refiner is one short model call. A deployment that hangs must degrade to
# the original request, never park the Ticket forever.
REFINE_TIMEOUT_SECONDS = 60.0

_RAW_PREFIX = "/raw"

_REFINE_SYSTEM_PROMPT = (
    "You rewrite a user's request to an autonomous coding agent so it can be "
    "executed without guessing. Preserve the user's intent exactly; never "
    "widen the scope, never invent requirements, never write code, never "
    "propose an implementation, and never run anything. Reply with the "
    "rewritten request, the assumptions you relied on, up to three open "
    "questions a human must answer before work can safely start, and the "
    "acceptance criteria the finished work should meet."
)


class RefinedPrompt(BaseModel):
    """The refiner's proposal for one request, shown for confirmation."""

    refined_prompt: str = ""
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)


class PromptRefiner(Protocol):
    async def refine(self, *, user_id: str, prompt: str) -> RefinedPrompt: ...


def refine_mode() -> str:
    """``AUTONOMY_PROMPT_REFINE``: off (default), vague, or always.

    An unset or unknown value is ``off``: a typo must not silently start
    rewriting every request.
    """
    value = (os.getenv("AUTONOMY_PROMPT_REFINE") or "").strip().lower()
    if value in REFINE_MODES:
        return value
    if value:
        logger.warning(
            "[autonomy] AUTONOMY_PROMPT_REFINE=%r is not one of %s; using off",
            value,
            "/".join(REFINE_MODES),
        )
    return REFINE_OFF


def strip_raw_prefix(prompt: str) -> tuple[str, bool]:
    """Split a leading ``/raw`` control token off a request.

    Returns ``(request, raw)``. ``raw`` is True only for a real prefix —
    ``/rawfix`` is an ordinary request — and an empty remainder stays empty so
    the caller can refuse it. The control prefix is removed in every mode;
    other requests stay unchanged when the refiner is off.
    """
    text = prompt or ""
    if not text.startswith(_RAW_PREFIX):
        return text, False
    rest = text[len(_RAW_PREFIX) :]
    if rest and not rest[:1].isspace():
        return text, False
    return rest.strip(), True


def build_fallback_refinement(prompt: str) -> RefinedPrompt:
    """The original request, unchanged: the refiner never blocks a Ticket."""
    text = (prompt or "").strip()
    return RefinedPrompt(refined_prompt=text)


async def default_refiner() -> PromptRefiner | None:
    """LLM-backed refiner; None where no server-side LLM is wired.

    The call is a single ``LLMService.complete``: no tools are bound, the
    prompt carries only the user's own request text (no repository path, no
    ticket context, no credentials), and a completion cannot write anything.
    """
    from src.server.app import setup

    llm_service = getattr(setup, "llm_service", None)
    if llm_service is None:
        return None

    class _LLMRefiner:
        async def refine(self, *, user_id: str, prompt: str) -> RefinedPrompt:
            result = await llm_service.complete(
                user_id=user_id,
                system_prompt=_REFINE_SYSTEM_PROMPT,
                user_prompt=prompt,
                response_schema=RefinedPrompt,
                slot="background",
            )
            return _coerce(result, prompt)

    return _LLMRefiner()


def _coerce(result: Any, prompt: str) -> RefinedPrompt:
    """Normalise a model result; an empty rewrite keeps the original request."""
    text = (prompt or "").strip()

    def _items(value: Any, limit: int) -> list[str]:
        return [str(item).strip() for item in (value or []) if str(item).strip()][:limit]

    refined = str(getattr(result, "refined_prompt", "") or "").strip()
    return RefinedPrompt(
        refined_prompt=refined or text,
        assumptions=_items(getattr(result, "assumptions", None), 8),
        open_questions=_items(getattr(result, "open_questions", None), 3),
        acceptance_criteria=_items(getattr(result, "acceptance_criteria", None), 8),
    )


def _settled(latest: dict[str, Any] | None) -> str:
    """Outcome once the Ticket moved underneath a gate.

    Still parked is ``"awaiting"``. A confirm or reject that released it
    (``mandate.clarified``) already enqueued its own launch, so the gate
    reports ``"handled"`` and the caller must not launch a second time. Only
    a Ticket that is queued and never confirmed is ``"passed"``.
    """
    if latest is None:
        return "handled"
    if latest.get("status") == "awaiting_clarification":
        return "awaiting"
    if (latest.get("mandate") or {}).get("clarified"):
        return "handled"
    return "passed"


def _request_text(ticket: dict[str, Any]) -> str:
    return str(ticket.get("scope") or ticket.get("title") or "").strip()


def _selected(mode: str | None) -> str:
    return refine_mode() if mode is None else mode


async def _run_refiner(
    refiner: PromptRefiner | None, *, user_id: str, prompt: str
) -> tuple[RefinedPrompt, bool]:
    """Refine once; a timeout, error or missing refiner degrades to the original."""
    if refiner is None:
        refiner = await default_refiner()
    if refiner is None:
        logger.info(
            "[autonomy] no refiner is wired; keeping the original request user=%s",
            user_id,
        )
        return build_fallback_refinement(prompt), True
    try:
        refined = await asyncio.wait_for(
            refiner.refine(user_id=user_id, prompt=prompt),
            timeout=REFINE_TIMEOUT_SECONDS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "[autonomy] prompt refine timed out after %.0fs; keeping the original",
            REFINE_TIMEOUT_SECONDS,
        )
        return build_fallback_refinement(prompt), True
    except Exception:
        logger.warning(
            "[autonomy] prompt refine failed; keeping the original request",
            exc_info=True,
        )
        return build_fallback_refinement(prompt), True
    if not refined.refined_prompt.strip():
        return build_fallback_refinement(prompt), True
    return refined, False


async def refine_gate(
    *, ticket: dict[str, Any], mode: str | None = None, refiner: PromptRefiner | None = None
) -> str:
    """Rewrite the request and park the Ticket until the user confirms.

    Returns ``"passed"`` when the Ticket may launch (mode off, the request is
    not selected, or the Ticket is not parkable), ``"awaiting"`` when it
    parked with a stored rewrite, and ``"handled"`` when a confirm or reject
    already released it with its own launch. Never enqueues a launch effect:
    the caller launches only on ``"passed"``.
    """
    selected = _selected(mode)
    if selected == REFINE_OFF:
        return "passed"
    ticket_id = str(ticket["ticket_id"])
    # The clarification gate parks the same Ticket under this lock; sharing it
    # keeps the two gates from interleaving on one row.
    async with _ticket_gate_lock(ticket_id):
        return await _refine_locked(ticket_id, mode=selected, refiner=refiner)


async def _refine_locked(
    ticket_id: str, *, mode: str, refiner: PromptRefiner | None
) -> str:
    current = await ticket_db.get_ticket(ticket_id)
    if current is None:
        return "passed"
    status = current.get("status")
    if status not in ("queued", "awaiting_clarification"):
        return "passed"
    if (current.get("mandate") or {}).get("clarified"):
        return "passed"
    if await clarification_db.get_active_clarification(ticket_id) is not None:
        return "awaiting"
    if status != "queued":
        # Another gate parked this Ticket and is still deciding; it must not
        # launch underneath that decision.
        return "awaiting"
    prompt = _request_text(current)
    if not prompt:
        return "passed"
    if mode == REFINE_VAGUE and heuristic_clarity_score(prompt) >= clarity_threshold():
        return "passed"
    # Park before the model call so the launch sweeper cannot observe a queued
    # Ticket while the refiner is still deciding (the clarify gate's rule).
    held = await ticket_db.update_ticket(
        ticket_id,
        expected_status="queued",
        status="awaiting_clarification",
        attention={"question": "Refining this request before it starts."},
    )
    if held is None:
        return _settled(await ticket_db.get_ticket(ticket_id))
    user_id = str(current.get("user_id") or "")
    refined, degraded = await _run_refiner(refiner, user_id=user_id, prompt=prompt)
    latest = await ticket_db.get_ticket(ticket_id)
    if latest is None or latest.get("status") != "awaiting_clarification":
        # The user answered while the refiner ran; the confirm already
        # enqueued the launch.
        return _settled(latest)
    draft = await clarification_db.create_clarification(
        ticket_id=ticket_id,
        user_id=user_id,
        original_prompt=prompt,
        clarity_score=heuristic_clarity_score(prompt),
        expanded_request=refined.refined_prompt,
        assumptions=refined.assumptions,
        open_questions=refined.open_questions,
    )
    if draft is None:
        return _settled(await ticket_db.get_ticket(ticket_id))
    mandate = dict(latest.get("mandate") or {})
    mandate["refine"] = {
        "mode": mode,
        "degraded": degraded,
        "acceptance_criteria": refined.acceptance_criteria,
    }
    moved = await ticket_db.update_ticket(
        ticket_id,
        expected_status="awaiting_clarification",
        attention={
            "question": "Please confirm the refined request before this Ticket starts."
        },
        mandate=mandate,
    )
    if moved is None:
        return _settled(await ticket_db.get_ticket(ticket_id))
    return "awaiting"


async def hold_gate(*, ticket: dict[str, Any], mode: str | None = None) -> str:
    """Hold an unattended request for confirmation instead of refining it.

    Channel sources and cron runs have nobody to confirm a rewrite, so the
    refiner is never called for them: the request parks with its original
    text and an operator confirms or rejects it later. Returns ``"passed"``
    when the Ticket may launch unchanged (mode off, request not selected, or
    not parkable), ``"awaiting"`` when it parked, and ``"handled"`` when a
    confirm or reject already released it with its own launch.
    """
    selected = _selected(mode)
    if selected == REFINE_OFF:
        return "passed"
    ticket_id = str(ticket["ticket_id"])
    async with _ticket_gate_lock(ticket_id):
        current = await ticket_db.get_ticket(ticket_id)
        if current is None:
            return "passed"
        status = current.get("status")
        if status not in ("queued", "awaiting_clarification"):
            return "passed"
        if (current.get("mandate") or {}).get("clarified"):
            return "passed"
        if await clarification_db.get_active_clarification(ticket_id) is not None:
            return "awaiting"
        if status != "queued":
            return "awaiting"
        prompt = _request_text(current)
        if not prompt:
            return "passed"
        if (
            selected == REFINE_VAGUE
            and heuristic_clarity_score(prompt) >= clarity_threshold()
        ):
            return "passed"
        mandate = dict(current.get("mandate") or {})
        mandate["refine"] = {"mode": selected, "degraded": False, "held": True}
        held = await ticket_db.update_ticket(
            ticket_id,
            expected_status="queued",
            status="awaiting_clarification",
            attention={
                "question": "Please confirm this request before its Ticket starts."
            },
            mandate=mandate,
        )
        if held is None:
            return _settled(await ticket_db.get_ticket(ticket_id))
        draft = await clarification_db.create_clarification(
            ticket_id=ticket_id,
            user_id=str(current.get("user_id") or ""),
            original_prompt=prompt,
            clarity_score=heuristic_clarity_score(prompt),
            expanded_request=prompt,
            assumptions=[],
            open_questions=[],
        )
        if draft is None:
            return _settled(await ticket_db.get_ticket(ticket_id))
        return "awaiting"


async def confirm_refinement(*, ticket_id: str, text: str | None = None) -> dict:
    """Adopt the rewrite — or the user's edit of it — as the Ticket's goal."""
    if text is None:
        draft = await clarification_db.get_active_clarification(ticket_id)
        if draft is None:
            raise ValueError("no active refinement to confirm")
        text = str(draft.get("expanded_request") or "")
    return await confirm_clarification(ticket_id=ticket_id, text=text)


async def reject_refinement(*, ticket_id: str) -> dict:
    """Decline the rewrite; the Ticket runs as the user first wrote it."""
    result = await clarification_db.reject_ticket_clarification(
        ticket_id=ticket_id, enqueue_launch=True
    )
    if result is None:
        if await ticket_db.get_ticket(ticket_id) is None:
            raise ValueError("ticket not found")
        raise ValueError("ticket changed or no active refinement; retry")
    return result["ticket"]
