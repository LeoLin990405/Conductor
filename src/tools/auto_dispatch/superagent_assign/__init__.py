"""Super-agent assignment types.

Other lines own the sibling modules (effort, feasibility, context, backend,
record, modes). Importing this package loads none of them, so the types can
be used before those files exist.
"""

from __future__ import annotations

from src.tools.auto_dispatch.superagent_assign.types import (
    ASSIGNMENT_VERSION,
    DEFAULT_MODE,
    AssignmentCall,
    AssignmentPlan,
    Confidence,
    Effort,
    ErrorKind,
    ErrorResult,
    FeasibilityResult,
    LineChoice,
    Mode,
    Source,
    call_error,
    format_error,
    is_error,
    timeout_error,
)

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
