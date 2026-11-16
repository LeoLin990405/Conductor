"""Shapes for one Super-agent assignment call (N73 PRD §4.1, §4.4–§4.6).

The rest of ``superagent_assign`` shares these records. They are frozen so a
choice cannot be edited after feasibility or the mode switch has read it.
An ERROR result is a different type from ``AssignmentPlan``: a failed call
is not a choice, and the caller falls back without asking again.

``role_plan.AssignmentPlan`` is a different record. That one places
developer tasks on a DAG. This one is the model, effort and runtime picked
for each still-open line.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeGuard, TypeVar

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationInfo,
    field_validator,
    model_validator,
)

_E = TypeVar("_E", bound=StrEnum)

# Gate snapshot ``version``. Bump when a stored choice or verdict gains a field.
ASSIGNMENT_VERSION = 1

_LINE_FIELDS = frozenset(
    {"line_id", "model", "model_id", "effort", "runtime", "reason", "confidence"}
)
_LINE_REQUIRED = frozenset(
    {"line_id", "model", "effort", "runtime", "reason", "confidence"}
)
_PLAN_FIELDS = frozenset({"choices"})
_FEASIBILITY_FIELDS = frozenset({"status", "reason_code"})
_ERROR_FIELDS = frozenset({"status", "kind", "reason"})
_OK = "ok"
_INFEASIBLE = "infeasible"
_ERROR_STATUS = "error"


class Effort(StrEnum):
    """Shared scale: ``default``, ``low``, ``medium``, ``high``, or ``max``.

    ``default`` is today's launch behaviour. CLI spellings, including ``n/a``,
    are not members. The effort table is the only place those spellings exist.
    """

    DEFAULT = "default"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    MAX = "max"


class Confidence(StrEnum):
    """Self-reported confidence. Recorded, and never used as a branch."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Mode(StrEnum):
    """``CONDUCTOR_DISPATCH_ASSIGN_MODE``. Unset means shadow."""

    OFF = "off"
    SHADOW = "shadow"
    ENFORCE = "enforce"


DEFAULT_MODE = Mode.SHADOW


class Source(StrEnum):
    """Where the model that actually ran came from.

    ``selector`` is today's selector used on purpose (mode off, or an
    enforce sample miss). ``selector_fallback`` is the selector used because
    the Super-agent choice was infeasible twice, or the call returned ERROR.
    """

    SUPERAGENT = "superagent"
    SUPERAGENT_REASK = "superagent_reask"
    SELECTOR_FALLBACK = "selector_fallback"
    SELECTOR = "selector"


class ErrorKind(StrEnum):
    """Why a call is ERROR. The three cases stay distinct so a report can count them."""

    CALL_ERROR = "call_error"
    TIMEOUT = "timeout"
    BAD_FORMAT = "bad_format"


def _text(value: object, field: str, *, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{field} is required")
    return text


def _token(value: object, field: str) -> str:
    text = _text(value, field, required=True)
    if any(char.isspace() for char in text):
        raise ValueError(f"{field} is a single token")
    return text


def _optional_token(value: object, field: str) -> str:
    text = _text(value, field, required=False)
    if text and any(char.isspace() for char in text):
        raise ValueError(f"{field} is a single token")
    return text


def _enum(value: object, kind: type[_E], field: str) -> _E:
    if isinstance(value, kind):
        return value
    if isinstance(value, str):
        try:
            return kind(value.strip())
        except ValueError:
            pass
    allowed = ", ".join(item.value for item in kind)
    raise ValueError(f"{field} must be one of: {allowed}")


def _object(data: object, what: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"{what} must be an object")
    return data


def _close(data: dict[str, Any], allowed: frozenset[str], what: str) -> None:
    unknown = set(data) - allowed
    if unknown:
        name = sorted(unknown)[0]
        raise ValueError(f"unknown {what} field: {name}")


# Frozen model, not a dataclass: the native structured-output path validates
# only BaseModel schemas. A dataclass comes back as the tool-call dict, with
# no check of the effort scale. This comment stays out of the published schema.
class LineChoice(BaseModel):
    """One line's roster model, shared-scale effort, and launch runtime.

    ``line_id``, ``model``, ``model_id``, and ``runtime`` are single tokens.
    ``model`` is a roster name. ``model_id`` is empty when the provider
    default is enough. ``effort`` is the shared scale, not a CLI flag.
    ``runtime`` is a value the launch script already accepts. ``reason``
    cites the inputs used. ``confidence`` is recorded and is not a branch.
    The mapped CLI effort is not a field here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str
    model: str
    effort: Effort
    runtime: str
    reason: str
    confidence: Confidence
    model_id: str = ""

    @field_validator("line_id", "model", "model_id", "runtime", "reason", mode="before")
    @classmethod
    def _strings(cls, value: object, info: ValidationInfo) -> str:
        field = info.field_name or "field"
        if field == "reason":
            return _text(value, field, required=True)
        if field == "model_id":
            return _optional_token(value, field)
        return _token(value, field)

    @field_validator("effort", "confidence", mode="before")
    @classmethod
    def _strip_scale(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    def to_dict(self) -> dict[str, str]:
        return {
            "line_id": self.line_id,
            "model": self.model,
            "model_id": self.model_id,
            "effort": self.effort.value,
            "runtime": self.runtime,
            "reason": self.reason,
            "confidence": self.confidence.value,
        }

    @classmethod
    def from_dict(cls, data: object) -> LineChoice:
        raw = _object(data, "line choice")
        _close(raw, _LINE_FIELDS, "line choice")
        missing = _LINE_REQUIRED - set(raw)
        if missing:
            raise ValueError(f"missing line choice field: {sorted(missing)[0]}")
        return cls(
            line_id=raw["line_id"],
            model=raw["model"],
            model_id=raw.get("model_id", ""),
            effort=raw["effort"],
            runtime=raw["runtime"],
            reason=raw["reason"],
            confidence=raw["confidence"],
        )


# Same constraint as LineChoice. The call validates AssignmentPlan(**payload)
# and quotes model_json_schema when that parse fails and it asks again.
class AssignmentPlan(BaseModel):
    """One choice for every still-open line.

    One entry per line. A second choice for the same ``line_id`` is rejected,
    so two providers cannot both own a line. An empty plan means nothing was
    left to assign.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    choices: tuple[LineChoice, ...] = ()

    @model_validator(mode="after")
    def _one_choice_per_line(self) -> AssignmentPlan:
        seen: set[str] = set()
        for choice in self.choices:
            if choice.line_id in seen:
                raise ValueError(f"duplicate line_id: {choice.line_id}")
            seen.add(choice.line_id)
        return self

    def by_line(self) -> dict[str, LineChoice]:
        return {choice.line_id: choice for choice in self.choices}

    def to_dict(self) -> dict[str, Any]:
        return {"choices": [choice.to_dict() for choice in self.choices]}

    @classmethod
    def from_dict(cls, data: object) -> AssignmentPlan:
        raw = _object(data, "assignment plan")
        _close(raw, _PLAN_FIELDS, "assignment plan")
        if "choices" not in raw:
            raise ValueError("missing assignment plan field: choices")
        rows = raw["choices"]
        if not isinstance(rows, list):
            raise ValueError("choices must be a list")
        return cls(choices=tuple(LineChoice.from_dict(row) for row in rows))


@dataclass(frozen=True, slots=True)
class FeasibilityResult:
    """Dispatcher verdict for one choice: ``ok`` or ``infeasible(reason_code)``.

    The check lives elsewhere. An ok verdict has no reason. An infeasible
    one carries a single reason token the re-ask and the gate record can quote.
    """

    status: str
    reason_code: str = ""

    def __post_init__(self) -> None:
        if self.status == _OK:
            if self.reason_code:
                raise ValueError("ok result has no reason_code")
            return
        if self.status == _INFEASIBLE:
            object.__setattr__(
                self, "reason_code", _token(self.reason_code, "reason_code")
            )
            return
        raise ValueError("status must be ok or infeasible")

    @property
    def is_ok(self) -> bool:
        return self.status == _OK

    @classmethod
    def ok(cls) -> FeasibilityResult:
        return cls(status=_OK)

    @classmethod
    def infeasible(cls, reason_code: str) -> FeasibilityResult:
        return cls(status=_INFEASIBLE, reason_code=reason_code)

    def to_dict(self) -> dict[str, str]:
        if self.is_ok:
            return {"status": _OK}
        return {"status": _INFEASIBLE, "reason_code": self.reason_code}

    @classmethod
    def from_dict(cls, data: object) -> FeasibilityResult:
        raw = _object(data, "feasibility result")
        _close(raw, _FEASIBILITY_FIELDS, "feasibility result")
        status = raw.get("status")
        if status == _OK:
            if raw.get("reason_code"):
                raise ValueError("ok result has no reason_code")
            return cls.ok()
        if status == _INFEASIBLE:
            if "reason_code" not in raw:
                raise ValueError("missing feasibility result field: reason_code")
            return cls.infeasible(raw["reason_code"])
        raise ValueError("status must be ok or infeasible")


@dataclass(frozen=True, slots=True)
class ErrorResult:
    """The call failed. Not a plan, and not a line choice.

    A call error, a timeout, or a payload that is not a plan is ERROR.
    Callers fall back to today's selector and do not re-ask. None of the
    three kinds is stored as a low-confidence choice.
    """

    kind: ErrorKind
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", _enum(self.kind, ErrorKind, "kind"))
        object.__setattr__(self, "reason", _text(self.reason, "reason", required=True))

    @property
    def status(self) -> str:
        return _ERROR_STATUS

    def to_dict(self) -> dict[str, str]:
        return {"status": _ERROR_STATUS, "kind": self.kind.value, "reason": self.reason}

    @classmethod
    def from_dict(cls, data: object) -> ErrorResult:
        raw = _object(data, "error result")
        if raw.get("status") != _ERROR_STATUS:
            raise ValueError("error result status must be error")
        _close(raw, _ERROR_FIELDS, "error result")
        missing = {"kind", "reason"} - set(raw)
        if missing:
            raise ValueError(f"missing error result field: {sorted(missing)[0]}")
        return cls(kind=raw["kind"], reason=raw["reason"])


AssignmentCall = AssignmentPlan | ErrorResult


def call_error(reason: str) -> ErrorResult:
    """ERROR for a call that raised. Not a choice."""
    return ErrorResult(kind=ErrorKind.CALL_ERROR, reason=reason)


def timeout_error(reason: str) -> ErrorResult:
    """ERROR for a call that ran out of time. Not a choice."""
    return ErrorResult(kind=ErrorKind.TIMEOUT, reason=reason)


def format_error(reason: str) -> ErrorResult:
    """ERROR for a payload that is not a plan. Not a choice."""
    return ErrorResult(kind=ErrorKind.BAD_FORMAT, reason=reason)


def is_error(result: object) -> TypeGuard[ErrorResult]:
    """True only for an ERROR result. A plan never matches."""
    return isinstance(result, ErrorResult)


__all__ = [
    "ASSIGNMENT_VERSION",
    "DEFAULT_MODE",
    "AssignmentCall",
    "AssignmentPlan",
    "Confidence",
    "Effort",
    "ErrorKind",
    "ErrorResult",
    "FeasibilityResult",
    "LineChoice",
    "Mode",
    "Source",
    "call_error",
    "format_error",
    "is_error",
    "timeout_error",
]
