"""``JudgmentPort``: typed fast judgments that never raise (N61 4.1, 4.2).

Status mapping, per question, with the use's ``lo``/``hi``/``margin``:

- choice and score: the top option is PASS when its probability is ``>= hi``
  and it leads the runner-up by ``>= margin``; anything else (including a
  tie at the top) is INCONCLUSIVE. A choice never FAILs.
- bool: ``p >= hi`` PASS (answer True), ``p <= lo`` FAIL (answer False),
  in between INCONCLUSIVE.
- a backend exception, a timeout, or an answer that is not a valid
  distribution over the candidates is ERROR. It is never read as p = 0.

Skips, all INCONCLUSIVE and never sent to the backend: ``mode_off`` (the use
or the global ``CONDUCTOR_JUDGMENT_MODE`` is off) and ``not_sampled`` are not
recorded; ``no_user`` (the request carries no ``user_id``) is recorded. A
judgment without a user is never billed to the platform key.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Mapping
from typing import Any, Protocol

from .record import JudgmentRecord, NullRecorder, Recorder, default_recorder, digest_state
from .types import (
    JudgmentSkipped,
    BoolQuestion,
    JudgmentRequest,
    JudgmentResult,
    JudgmentStatus,
    Question,
    candidates,
    overall_status,
)
from .uses import UseSpec, get_use, in_sample, use_mode

logger = logging.getLogger(__name__)

# A self-reported distribution may be a little off; more than this is garbage.
_SUM_TOLERANCE = 1e-3
_NO_BACKEND = "none"
# Thresholds are decimal values; compare so that a boundary lands on its side
# even after float rounding (0.575 - 0.425 is 0.1499999...).
_EPS = 1e-9


class JudgmentBackend(Protocol):
    name: str

    async def answer(
        self, request: JudgmentRequest
    ) -> Mapping[str, Mapping[str, float]]:
        """Raw probabilities per question id. May raise; may return garbage."""
        ...


class InvalidAnswer(ValueError):
    """The backend's output for one question is not a usable distribution."""


def _probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidAnswer(f"not a number: {type(value).__name__}")
    p = float(value)
    if not math.isfinite(p) or p < 0.0 or p > 1.0:
        raise InvalidAnswer("probability outside [0, 1]")
    return p


def validate_answer(question: Question, raw: Any) -> dict[str, float]:
    """The backend's answer for ``question`` as a clean distribution, or raise.

    Choice/score: keys must be candidates (unknown keys are invalid), at
    least one candidate present, the sum at most 1; absent candidates get 0.
    Bool: exactly ``{"true": p}``.
    """
    if not isinstance(raw, Mapping) or not raw:
        raise InvalidAnswer("missing or empty")
    if isinstance(question, BoolQuestion):
        if list(raw) != ["true"]:
            raise InvalidAnswer("bool answer must be exactly {'true': p}")
        return {"true": _probability(raw["true"])}
    options = candidates(question)
    if not options:
        raise InvalidAnswer("question has no candidates")
    dist: dict[str, float] = {option: 0.0 for option in options}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in dist:
            raise InvalidAnswer("answer outside the candidate set")
        dist[key] = _probability(value)
    if sum(dist.values()) > 1.0 + _SUM_TOLERANCE:
        raise InvalidAnswer("probabilities sum above 1")
    return dist


def map_question(
    question: Question, dist: Mapping[str, float], spec: UseSpec
) -> tuple[JudgmentStatus, str | bool | None]:
    """PRD 4.2: one validated distribution to (status, answer)."""
    if isinstance(question, BoolQuestion):
        p = dist["true"]
        if p >= spec.hi - _EPS:
            return JudgmentStatus.PASS, True
        if p <= spec.lo + _EPS:
            return JudgmentStatus.FAIL, False
        return JudgmentStatus.INCONCLUSIVE, None
    ranked = sorted(dist.items(), key=lambda item: item[1], reverse=True)
    top_option, top = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    if top == runner_up:  # a tie at the top has no answer
        return JudgmentStatus.INCONCLUSIVE, None
    if top >= spec.hi - _EPS and (top - runner_up) >= spec.margin - _EPS:
        return JudgmentStatus.PASS, top_option
    return JudgmentStatus.INCONCLUSIVE, None


class JudgmentPort:
    """Ask typed questions; get a ``JudgmentResult``. Never raises.

    ``recorder=None`` drops records; production callers get a JSONL recorder
    from ``default_port()``. Tests pass a ``MemoryRecorder``.
    """

    def __init__(
        self,
        backend: JudgmentBackend | None,
        *,
        recorder: Recorder | None = None,
        budget_s: float = 20.0,
        retries: int = 1,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.backend = backend
        self.recorder: Recorder = recorder if recorder is not None else NullRecorder()
        self.budget_s = float(budget_s)
        self.retries = max(0, int(retries))
        self._environ = environ

    def mode_for(self, use: str) -> str:
        return use_mode(use, self._environ)

    async def judge(
        self,
        request: JudgmentRequest,
        *,
        action: str = "",
        sample_key: str = "",
        subject: str = "",
    ) -> JudgmentResult:
        """Judge ``request``. ``action`` is what the caller's flow actually did."""
        started = time.monotonic()
        qids = [str(q) for q in _safe_keys(request)]
        mode = "shadow"
        try:
            mode = self.mode_for(request.use)
            if mode == "off":
                return _skipped(qids, "mode_off")
            if not in_sample(request.use, sample_key, self._environ):
                return _skipped(qids, "not_sampled")
            if not _user_id(request):
                result = _skipped(qids, "no_user")
            else:
                result = await self._judge(request, qids, started)
        except Exception as exc:  # the port never raises
            logger.warning("[judgment] judge failed for use=%s", _use(request), exc_info=True)
            result = JudgmentResult.error(
                _backend_name(self.backend),
                f"port_error:{type(exc).__name__}",
                _elapsed_ms(started),
                qids,
            )
        self._record(request, qids, mode, result, action, subject)
        return result

    async def drain(self) -> None:
        """Wait for records still being written (a no-op for sync recorders)."""
        drain = getattr(self.recorder, "drain", None)
        if drain is None:
            return
        try:
            await drain()
        except Exception:
            logger.warning("[judgment] could not drain the recorder", exc_info=True)

    async def _judge(
        self, request: JudgmentRequest, qids: list[str], started: float
    ) -> JudgmentResult:
        try:
            spec = get_use(request.use)
        except KeyError:
            return JudgmentResult.error(
                _backend_name(self.backend), "unknown_use", _elapsed_ms(started), qids
            )
        if not qids:
            return JudgmentResult.error(
                _backend_name(self.backend), "no_questions", _elapsed_ms(started), qids
            )
        backend = self.backend
        if backend is None:
            return JudgmentResult.error(
                _NO_BACKEND, "backend_unavailable", _elapsed_ms(started), qids
            )
        name = _backend_name(backend)
        try:
            raw = await asyncio.wait_for(self._call(backend, request), self.budget_s)
        except JudgmentSkipped as exc:
            skipped = _skipped(qids, exc.reason)
            return JudgmentResult(
                status=skipped.status,
                answers=skipped.answers,
                probabilities=skipped.probabilities,
                question_status=skipped.question_status,
                latency_ms=_elapsed_ms(started),
                backend=name,
                reason=exc.reason,
            )
        except (TimeoutError, asyncio.TimeoutError):
            return JudgmentResult.error(name, "timeout", _elapsed_ms(started), qids)
        except Exception as exc:
            return JudgmentResult.error(
                name, f"backend_error:{type(exc).__name__}", _elapsed_ms(started), qids
            )
        if not isinstance(raw, Mapping):
            return JudgmentResult.error(
                name, "invalid_output:not_a_mapping", _elapsed_ms(started), qids
            )
        answers: dict[str, str | bool | None] = {}
        probabilities: dict[str, dict[str, float]] = {}
        statuses: dict[str, JudgmentStatus] = {}
        reasons: list[str] = []
        for qid in qids:
            question = request.questions[qid]
            try:
                dist = validate_answer(question, raw.get(qid))
            except InvalidAnswer as exc:
                statuses[qid] = JudgmentStatus.ERROR
                answers[qid] = None
                reasons.append(f"invalid_output:{qid}:{exc}")
                continue
            status, answer = map_question(question, dist, spec)
            statuses[qid] = status
            answers[qid] = answer if status in (JudgmentStatus.PASS, JudgmentStatus.FAIL) else None
            probabilities[qid] = dist
        return JudgmentResult(
            status=overall_status(statuses.values()),
            answers=answers,
            probabilities=probabilities,
            question_status=statuses,
            latency_ms=_elapsed_ms(started),
            backend=name,
            reason="; ".join(reasons),
        )

    async def _call(
        self, backend: JudgmentBackend, request: JudgmentRequest
    ) -> Mapping[str, Mapping[str, float]]:
        """One attempt plus ``retries`` more on an exception; the caller bounds the time."""
        last: Exception | None = None
        for _ in range(self.retries + 1):
            try:
                return await backend.answer(request)
            except JudgmentSkipped:
                raise
            except Exception as exc:
                last = exc
        assert last is not None
        raise last

    def _record(
        self,
        request: JudgmentRequest,
        qids: list[str],
        mode: str,
        result: JudgmentResult,
        action: str,
        subject: str,
    ) -> None:
        try:
            state = getattr(request, "state", None)
            record = JudgmentRecord(
                use=_use(request),
                version=str(getattr(request, "version", "")),
                mode=mode,
                question_ids=list(qids),
                status=str(result.status),
                question_status={k: str(v) for k, v in result.question_status.items()},
                answers=dict(result.answers),
                probabilities={k: dict(v) for k, v in result.probabilities.items()},
                latency_ms=result.latency_ms,
                backend=result.backend,
                reason=result.reason,
                action=str(action or ""),
                state_digest=digest_state(state) if isinstance(state, Mapping) else {},
                subject=str(subject or ""),
            )
            self.recorder.write(record)
        except Exception:
            logger.warning("[judgment] could not record use=%s", _use(request), exc_info=True)


def _skipped(qids: list[str], reason: str) -> JudgmentResult:
    return JudgmentResult(
        status=JudgmentStatus.INCONCLUSIVE,
        answers={qid: None for qid in qids},
        probabilities={},
        question_status={qid: JudgmentStatus.INCONCLUSIVE for qid in qids},
        latency_ms=0,
        backend=_NO_BACKEND,
        reason=reason,
    )


def _safe_keys(request: JudgmentRequest) -> list[Any]:
    try:
        return list(request.questions)
    except Exception:
        return []


def _user_id(request: Any) -> str:
    return str(getattr(request, "user_id", None) or "").strip()


def _use(request: Any) -> str:
    return str(getattr(request, "use", ""))


def _backend_name(backend: Any) -> str:
    if backend is None:
        return _NO_BACKEND
    try:
        return str(getattr(backend, "name", "") or type(backend).__name__)
    except Exception:
        return "unknown"


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def default_port(*, recorder: Recorder | None = None) -> JudgmentPort:
    """The server's port: LLM backend over ``setup.llm_service`` when wired.

    The backend bills each call to the request's ``user_id`` (BYOK and
    preferences apply); a request without one is skipped as ``no_user``.
    """
    backend: JudgmentBackend | None = None
    try:
        from src.server.app import setup

        llm_service = getattr(setup, "llm_service", None)
        if llm_service is not None:
            from .backends.llm import LLMJudgmentBackend

            backend = LLMJudgmentBackend(llm_service)
    except Exception:
        logger.warning("[judgment] no LLM backend; judgments will be ERROR", exc_info=True)
        backend = None
    return JudgmentPort(
        backend, recorder=recorder if recorder is not None else default_recorder()
    )
