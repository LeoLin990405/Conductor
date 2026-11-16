"""Typed questions and the four judgment statuses (N61 section 4.1).

A fast judgment answers narrow, typed questions with probabilities a program
can branch on. It never answers open questions; those stay with the
Super-agent (System Two).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum


class JudgmentSkipped(Exception):
    """A backend declines to judge (for example ``no_user_credentials``).

    The port records it as a skip with ``reason``: no retry, not an ERROR.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class JudgmentStatus(StrEnum):
    """PASS adopt, FAIL reject, INCONCLUSIVE escalate, ERROR use the fallback.

    ERROR is a call failure. It is never read as a probability of zero.
    """

    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    ERROR = "error"


@dataclass(frozen=True)
class ChoiceQuestion:
    """Pick one of ``options``."""

    prompt: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class ScoreQuestion:
    """Pick one of ``levels``, ordered low to high. Maps like a choice."""

    prompt: str
    levels: tuple[str, ...]


@dataclass(frozen=True)
class BoolQuestion:
    """Is the proposition true? Answered as ``{"true": p}``."""

    prompt: str


Question = ChoiceQuestion | ScoreQuestion | BoolQuestion


def candidates(question: Question) -> tuple[str, ...]:
    """The answer space of ``question``; ``("true", "false")`` for a bool."""
    if isinstance(question, ChoiceQuestion):
        return tuple(question.options)
    if isinstance(question, ScoreQuestion):
        return tuple(question.levels)
    return ("true", "false")


@dataclass(frozen=True)
class JudgmentRequest:
    """One judgment: a registered use, its version, the material and the questions.

    ``state`` is the material (already capped by the caller). It is sent to
    the backend only; records keep its hash and length. ``user_id`` is whose
    credentials and model preferences the backend uses; without one the port
    skips the judgment (``no_user``) rather than bill the platform key.
    """

    use: str
    version: str
    state: Mapping[str, str]
    questions: Mapping[str, Question]
    user_id: str | None = None


@dataclass(frozen=True)
class JudgmentResult:
    """What the port decided.

    ``answers[qid]`` is the chosen option/level, or a bool for a
    ``BoolQuestion``; ``None`` when that question is not PASS/FAIL.
    ``probabilities[qid]`` is option->p (``{"true": p}`` for a bool); it is
    left out for a question that ended in ERROR.
    """

    status: JudgmentStatus
    answers: dict[str, str | bool | None]
    probabilities: dict[str, dict[str, float]]
    question_status: dict[str, JudgmentStatus]
    latency_ms: int
    backend: str
    reason: str = ""

    @classmethod
    def error(
        cls,
        backend: str,
        reason: str,
        latency_ms: int = 0,
        questions: Iterable[str] = (),
    ) -> JudgmentResult:
        """An ERROR result: every question ERROR, no answers, no probabilities."""
        qids = list(questions)
        return cls(
            status=JudgmentStatus.ERROR,
            answers={qid: None for qid in qids},
            probabilities={},
            question_status={qid: JudgmentStatus.ERROR for qid in qids},
            latency_ms=int(latency_ms),
            backend=backend,
            reason=reason,
        )


def overall_status(statuses: Iterable[JudgmentStatus]) -> JudgmentStatus:
    """ERROR if any ERROR, else FAIL if any FAIL, else INCONCLUSIVE if any, else PASS."""
    seen = set(statuses)
    for status in (
        JudgmentStatus.ERROR,
        JudgmentStatus.FAIL,
        JudgmentStatus.INCONCLUSIVE,
    ):
        if status in seen:
            return status
    return JudgmentStatus.PASS
