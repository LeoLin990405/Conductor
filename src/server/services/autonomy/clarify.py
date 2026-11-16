"""Prompt clarification gate: an unclear Ticket asks before it runs.

A Ticket created from a vague prompt would burn a Worker turn guessing. The
gate scores the prompt at creation time; below the threshold the Ticket parks
in ``awaiting_clarification`` with a stored draft (expanded request,
assumptions, open questions) and asks on the Source channel when there is
one. The scheduler claims ``queued`` rows only, so the parked status holds
execution without any scheduler change; a confirmation moves the Ticket back
to ``queued`` and enqueues the launch effect. Disabled by default, so the
default behavior is unchanged.
"""

from __future__ import annotations

import logging
import os
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Protocol

from pydantic import BaseModel, Field

from src.server.database.autonomy import clarifications as clarification_db
from src.server.database.autonomy import outbox as outbox_db
from src.server.database.autonomy import sources as source_db
from src.server.database.autonomy import tickets as ticket_db
from src.server.database.pool import open_session_connection
from src.server.database.autonomy.effects import enqueue_effect
from src.server.services.autonomy.models import EFFECT_REPLY

logger = logging.getLogger(__name__)

_VAGUE_TOKENS = (
    "something",
    "anything",
    "whatever",
    "stuff",
    "etc",
    "asap",
    "maybe",
    "somehow",
    "help",
)
_GOAL_CUES = (
    "do",
    "fix",
    "add",
    "implement",
    "refactor",
    "write",
    "create",
    "check",
    "investigate",
    "update",
    "build",
    "please",
    "can you",
    "how",
    "what",
    "why",
    "?",
)

_CLARITY_SYSTEM_PROMPT = (
    "You score how clear an autonomous coding request is. Reply with a score "
    "from 0 to 1 (1 = unambiguous, ready to execute as written), an expanded "
    "restatement of the request, the assumptions you rely on, and up to three "
    "open questions a human must answer before work can safely start."
)


class ClarityAssessment(BaseModel):
    score: float
    expanded_request: str = ""
    assumptions: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


class Clarifier(Protocol):
    async def assess(self, *, user_id: str, prompt: str) -> ClarityAssessment: ...


def _has_goal_cue(text: str) -> bool:
    lowered = text.lower()
    return any(cue in lowered for cue in _GOAL_CUES)


def _has_specificity(text: str) -> bool:
    if any(marker in text for marker in ("`", "/", '"', "'")):
        return True
    if any(ch.isdigit() for ch in text):
        return True
    return any(len(word) > 3 and word[0].isupper() for word in text.split())


def heuristic_clarity_score(prompt: str) -> float:
    """Cheap deterministic floor; never blocks a clear prompt on an LLM."""
    text = (prompt or "").strip()
    score = 1.0
    if len(text) < 20:
        score -= 0.35
    elif len(text) < 60:
        score -= 0.15
    lowered = text.lower()
    vague_hits = sum(1 for token in _VAGUE_TOKENS if token in lowered)
    score -= min(0.3, 0.1 * vague_hits)
    if not _has_goal_cue(text):
        score -= 0.25
    if not _has_specificity(text):
        score -= 0.2
    return round(max(0.0, min(1.0, score)), 3)


def clarify_enabled() -> bool:
    return os.getenv("AUTONOMY_CLARIFY_ENABLED", "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def clarity_threshold() -> float:
    try:
        value = float(os.getenv("AUTONOMY_CLARIFY_THRESHOLD", "0.6"))
    except ValueError:
        value = 0.6
    return max(0.0, min(1.0, value))


async def default_clarifier() -> Clarifier | None:
    """LLM-backed scorer; None where no server-side LLM is wired."""
    from src.server.app import setup

    llm_service = getattr(setup, "llm_service", None)
    if llm_service is None:
        return None

    class _LLMClarifier:
        async def assess(self, *, user_id: str, prompt: str) -> ClarityAssessment:
            try:
                result = await llm_service.complete(
                    user_id=user_id,
                    system_prompt=_CLARITY_SYSTEM_PROMPT,
                    user_prompt=prompt,
                    response_schema=ClarityAssessment,
                    slot="background",
                )
                score = max(0.0, min(1.0, float(getattr(result, "score", 0.0))))
                return ClarityAssessment(
                    score=score,
                    expanded_request=str(
                        getattr(result, "expanded_request", "") or ""
                    ),
                    assumptions=[
                        str(item)
                        for item in (getattr(result, "assumptions", None) or [])
                    ],
                    open_questions=[
                        str(item)
                        for item in (getattr(result, "open_questions", None) or [])
                    ],
                )
            except Exception:
                logger.warning(
                    "[autonomy] clarify assess failed for user=%s; "
                    "falling back to the heuristic score",
                    user_id,
                    exc_info=True,
                )
                return ClarityAssessment(score=heuristic_clarity_score(prompt))

    return _LLMClarifier()


def build_fallback_assessment(prompt: str) -> ClarityAssessment:
    """Heuristic score plus a template draft derived from what the prompt lacks."""
    text = (prompt or "").strip()
    open_questions: list[str] = []
    if len(text) < 20:
        open_questions.append("What is the exact goal or desired outcome?")
    if not _has_goal_cue(text):
        open_questions.append("What action should be taken?")
    if not _has_specificity(text):
        open_questions.append("Which files, paths, or components does this refer to?")
    return ClarityAssessment(
        score=heuristic_clarity_score(prompt),
        expanded_request=(
            f"Goal: {text}\n\n"
            "Assumptions and open questions are listed below; "
            "please confirm or correct."
        ),
        assumptions=["The request targets the workspace this Ticket was filed in."],
        open_questions=open_questions,
    )


async def gate(
    *,
    ticket: dict,
    clarifier: Clarifier | None = None,
    enabled: bool | None = None,
) -> str:
    """Decide whether a fresh Ticket starts now or waits for clarification.

    Returns ``"passed"`` when the Ticket may launch (or the gate is off, or
    the Ticket already moved) and ``"awaiting"`` when it parked in
    ``awaiting_clarification``. Callers enqueue their launch effect only on
    ``"passed"``.
    """
    gate_on = clarify_enabled() if enabled is None else enabled
    if not gate_on:
        return "passed"
    ticket_id = str(ticket["ticket_id"])
    # Keep two API workers from running different assessments and racing to
    # release or draft the same Ticket. This lock is durable across processes.
    async with _ticket_gate_lock(ticket_id):
        return await _gate_locked(ticket, clarifier=clarifier)


@asynccontextmanager
async def _ticket_gate_lock(ticket_id: str):
    conn = await open_session_connection()
    deadline = time.monotonic() + 30
    try:
        acquired = False
        while not acquired:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 731))",
                    (ticket_id,),
                )
                acquired = bool((await cur.fetchone() or (False,))[0])
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("clarification gate is busy; retry the request")
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_unlock(hashtextextended(%s, 731))",
                    (ticket_id,),
                )
    finally:
        await conn.close()


async def _gate_locked(
    ticket: dict, *, clarifier: Clarifier | None = None
) -> str:
    current = await ticket_db.get_ticket(str(ticket["ticket_id"]))
    if current is None:
        return "passed"
    ticket = current
    status = ticket.get("status")
    if status not in (None, "queued", "awaiting_clarification"):
        return "passed"
    if (ticket.get("mandate") or {}).get("clarified"):
        return "passed"
    ticket_id = str(ticket["ticket_id"])
    if await clarification_db.get_active_clarification(ticket_id) is not None:
        return "awaiting"
    if status != "awaiting_clarification":
        # Park before any async scoring so the launch sweeper and daemons
        # cannot observe a queued Ticket while the gate is still deciding.
        held = await ticket_db.update_ticket(
            ticket_id,
            expected_status="queued",
            status="awaiting_clarification",
            attention={"question": "Checking whether this request needs clarification."},
        )
        if held is None:
            current = await ticket_db.get_ticket(ticket_id)
            if not current or current.get("status") != "awaiting_clarification":
                return "passed"
    prompt = str(ticket.get("scope") or ticket.get("title") or "")
    heuristic = heuristic_clarity_score(prompt)
    threshold = clarity_threshold()
    if heuristic >= threshold:
        released = await ticket_db.update_ticket(
            ticket_id,
            expected_status="awaiting_clarification",
            status="queued",
            attention=None,
        )
        if released is None:
            latest = await ticket_db.get_ticket(ticket_id)
            return (
                "awaiting"
                if latest and latest.get("status") == "awaiting_clarification"
                else "passed"
            )
        return "passed"
    if clarifier is None:
        clarifier = await default_clarifier()
    if clarifier is not None:
        try:
            result = await clarifier.assess(
                user_id=str(ticket.get("user_id") or ""), prompt=prompt
            )
            # The model may see structure the heuristic misses, but it must
            # not score a prompt lower than the cheap floor that already ran.
            score = max(heuristic, min(1.0, float(result.score)))
            fallback = build_fallback_assessment(prompt)
            assessment = ClarityAssessment(
                score=score,
                expanded_request=result.expanded_request or fallback.expanded_request,
                assumptions=list(result.assumptions) or fallback.assumptions,
                open_questions=list(result.open_questions) or fallback.open_questions,
            )
        except Exception:
            logger.warning(
                "[autonomy] clarifier raised for ticket=%s; heuristic only",
                ticket_id,
                exc_info=True,
            )
            assessment = build_fallback_assessment(prompt)
    else:
        assessment = build_fallback_assessment(prompt)
    current = await ticket_db.get_ticket(ticket_id)
    if (
        current is None
        or current.get("status") != "awaiting_clarification"
        or (current.get("mandate") or {}).get("clarified")
    ):
        return "passed"
    if assessment.score >= threshold:
        released = await ticket_db.update_ticket(
            ticket_id,
            expected_status="awaiting_clarification",
            status="queued",
            attention=None,
        )
        if released is None:
            latest = await ticket_db.get_ticket(ticket_id)
            return (
                "awaiting"
                if latest and latest.get("status") == "awaiting_clarification"
                else "passed"
            )
        return "passed"
    draft = await clarification_db.create_clarification(
        ticket_id=ticket_id,
        user_id=str(ticket.get("user_id") or ""),
        original_prompt=prompt,
        clarity_score=assessment.score,
        expanded_request=assessment.expanded_request,
        assumptions=assessment.assumptions,
        open_questions=assessment.open_questions,
    )
    if draft is None:
        return "passed"
    moved = await ticket_db.update_ticket(
        ticket_id, expected_status="awaiting_clarification",
        attention={
            "question": (
                "Please confirm the clarified request before this Ticket starts."
            )
        },
    )
    if moved is None:
        return "passed"
    if draft is not None:
        assessment = ClarityAssessment(
            score=float(draft.get("clarity_score", assessment.score)),
            expanded_request=str(
                draft.get("expanded_request") or assessment.expanded_request
            ),
            assumptions=list(draft.get("assumptions") or assessment.assumptions),
            open_questions=list(
                draft.get("open_questions") or assessment.open_questions
            ),
        )
    await _send_clarification_request(ticket, assessment)
    return "awaiting"


async def _send_clarification_request(
    ticket: dict, assessment: ClarityAssessment
) -> None:
    """Ask "is this what you mean?" on the Source channel, when there is one.

    Manual Tickets have no external channel; the UI reads the stored draft
    and the attention notice instead. A duplicate outbox idempotency key means
    the question already went out — no second message, no second effect.
    """
    ticket_id = str(ticket["ticket_id"])
    source_id = str((ticket.get("mandate") or {}).get("source_id") or "")
    source = await source_db.get_source_any_user(source_id) if source_id else None
    if source is None or source.get("kind") == "manual":
        return
    thread_key = str(ticket.get("origin_thread_key") or "")
    if not thread_key:
        return
    lines = [
        "This request needs clarification before its Ticket can start. "
        "Please confirm or correct:",
        "",
        assessment.expanded_request or f"Goal: {str(ticket.get('scope') or '')}",
    ]
    if assessment.assumptions:
        lines += ["", "Assumptions:"]
        lines += [f"- {item}" for item in assessment.assumptions]
    if assessment.open_questions:
        lines += ["", "Open questions:"]
        lines += [
            f"{index}. {question}"
            for index, question in enumerate(assessment.open_questions, 1)
        ]
    outbox = await outbox_db.enqueue_outbox(
        user_id=str(ticket.get("user_id") or ""),
        ticket_id=ticket_id,
        source_id=source_id or str(source["source_id"]),
        thread_key=thread_key,
        body="\n".join(lines)[:20000],
        idempotency_key=f"autonomy:clarify-request:{ticket_id}",
    )
    if outbox is None:
        return
    await enqueue_effect(
        user_id=str(ticket.get("user_id") or ""),
        kind=EFFECT_REPLY,
        idempotency_key=f"autonomy:reply-effect:{outbox['outbox_id']}",
        payload={"outbox_id": outbox["outbox_id"]},
    )


async def confirm_clarification(
    *, ticket_id: str, text: str, enqueue_launch: bool = True
) -> dict:
    """The confirmed/edited expanded request becomes the Ticket's goal."""
    text = (text or "").strip()
    if not 1 <= len(text) <= 20000:
        raise ValueError("confirmation text must be between 1 and 20000 chars")
    result = await clarification_db.confirm_ticket_clarification(
        ticket_id=ticket_id, text=text, enqueue_launch=enqueue_launch
    )
    if result is None:
        if await ticket_db.get_ticket(ticket_id) is None:
            raise ValueError("ticket not found")
        raise ValueError("ticket changed or no active clarification; retry")
    return result["ticket"]


async def answer_via_channel(*, ticket: dict, text: str) -> dict:
    """Channel-reply entry point: a follow-up on the Ticket's own Thread."""
    return await confirm_clarification(
        ticket_id=str(ticket["ticket_id"]), text=text
    )
