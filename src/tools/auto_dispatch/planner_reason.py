"""The fixed vocabulary a planner failure is recorded with.

A dispatch whose planner fails is parked as ``planner_failed:<reason>``. The
reason is one of a fixed set, so an operator can grep for it and a dashboard
can switch on it. The model's own words never reach the message, the row or a
log line: a failure that names no reason is recorded as ``planner_error``, and
the exception's text is never carried into the reason.
"""

from __future__ import annotations

from .gates import PLANNER_FAILED
from .protocols import DispatchError
from .types import Dispatch, DispatchState

#: The reply was not usable as a plan: it did not parse, or it did not hold
#: the schema.
INVALID_JSON = "invalid_json"
#: The reply held no plan at all: no JSON object, or an empty result.
NO_JSON = "no_json"
#: The plan was fine, but the role policy could not place every line.
UNASSIGNED = "unassigned"
#: The plan had more lines than the developer pool can staff (N28).
EXCEEDS_DEVELOPER_POOL = "exceeds_developer_pool"
#: An acceptance command ran pytest on a whole test directory (N27).
ACCEPTANCE_TEST_DIRECTORY = "acceptance_test_directory"
#: The goal was over the byte ceiling and was refused before the model ran.
GOAL_TOO_LARGE = "goal_too_large"
#: The planner run itself timed out.
PLANNER_TIMEOUT = "planner_timeout"
#: Anything else: the planner failed without saying more.
PLANNER_ERROR = "planner_error"

#: Every reason a parked dispatch may carry.
PLANNER_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        INVALID_JSON,
        NO_JSON,
        UNASSIGNED,
        EXCEEDS_DEVELOPER_POOL,
        ACCEPTANCE_TEST_DIRECTORY,
        GOAL_TOO_LARGE,
        PLANNER_TIMEOUT,
        PLANNER_ERROR,
    }
)

#: The prefix every parked planner failure carries.
PLANNER_FAILED_PREFIX = f"{PLANNER_FAILED}:"


class PlannerFailure(DispatchError):
    """A planner failure that names its reason, never the model's text.

    ``reason`` is one of :data:`PLANNER_FAILURE_REASONS`; the message is the
    detail a log may show. Neither carries what the model said.
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason if reason in PLANNER_FAILURE_REASONS else PLANNER_ERROR


def planner_failure_reason(exc: BaseException) -> str:
    """The fixed reason code for a planner failure.

    A failure that names no reason is ``planner_error``: the reason is never
    guessed at, and the exception's own text is never carried into it.

    A timeout is the one exception to that. The planner run itself ran out of
    time, and the caller may not know it: the LLM planner propagates the
    timeout its model call raised, and that exception carries no reason of its
    own. ``asyncio.TimeoutError`` is this same class, so both spellings are
    one case.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason in PLANNER_FAILURE_REASONS:
        return reason
    if isinstance(exc, TimeoutError):
        return PLANNER_TIMEOUT
    return PLANNER_ERROR


def planner_failure_message(reason: str) -> str:
    """The dispatch message for a planner failure: the code, and nothing else."""
    code = reason if reason in PLANNER_FAILURE_REASONS else PLANNER_ERROR
    return f"{PLANNER_FAILED}:{code}"


def parked_planner_reason(dispatch: Dispatch) -> str:
    """The reason a parked dispatch carries, or ``""`` when it carries none.

    A row parked before the reason codes existed carries the bare
    ``planner_failed``. It did not say why, so it reads as ``planner_error``:
    the row is still a planner failure, and it still has to be reachable.
    """
    message = (dispatch.message or "").strip()
    if message == PLANNER_FAILED:
        return PLANNER_ERROR
    if not message.startswith(PLANNER_FAILED_PREFIX):
        return ""
    code = message[len(PLANNER_FAILED_PREFIX) :].split(":", 1)[0].strip()
    return code if code in PLANNER_FAILURE_REASONS else ""


def is_planner_failed(dispatch: Dispatch) -> bool:
    """Whether the dispatch is parked on a planner failure, with nothing run.

    A planner failure parks in REVIEW with no line, so there is no branch to
    move aside and no worker to stop: the dispatch can be abandoned as it is.
    """
    return (
        dispatch.state is DispatchState.REVIEW
        and not dispatch.lines
        and parked_planner_reason(dispatch) != ""
    )
