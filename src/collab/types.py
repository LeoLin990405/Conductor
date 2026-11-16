"""Wire types and the closed error vocabulary of the collab protocol.

The protocol package deliberately has no database or transport dependency: the
HTTP routes, the CLI and the MCP server must validate and render the same
shapes. These are small value objects, patterns and refusal classes, not ORM
models. The code, not the HTTP status, is the contract a caller sees.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

APP_ID_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"
AGENT_PATTERN = r"^[A-Za-z0-9_.:/-]{0,64}$"
TICKET_REF_PATTERN = r"t:[0-9a-f-]{36}"
LINE_REF_PATTERN = r"d:[A-Za-z0-9_-]{1,64}:[A-Za-z0-9_-]{1,64}"
TASK_REF_PATTERN = rf"(?:{TICKET_REF_PATTERN}|{LINE_REF_PATTERN})"

_APP_ID_RE = re.compile(APP_ID_PATTERN)
_AGENT_RE = re.compile(AGENT_PATTERN)
_TASK_REF_RE = re.compile(rf"^(?:{TASK_REF_PATTERN})$")
_TICKET_REF_RE = re.compile(rf"^(?:{TICKET_REF_PATTERN})$")

#: Envelope and audit codes from N80 section 4.2, plus ``not_deliverable``
#: (section 6.4) and the internal fallback for an unclassified exception.
ERROR_CODES: dict[str, int] = {
    "ok": 200,
    "unauthorized": 401,
    "scope_denied": 403,
    "mode_denied": 403,
    "rate_limited": 429,
    "not_found": 404,
    "not_claimable": 409,
    "already_claimed": 409,
    "assignment_refused": 409,
    "lease_lost": 409,
    "lease_expired": 409,
    "stale_revision": 409,
    "self_review": 403,
    "invalid_schema": 400,
    "secret_in_payload": 400,
    "channel_full": 409,
    "too_large": 413,
    "not_deliverable": 409,
    "internal_error": 500,
}
ERROR_HTTP_STATUS = ERROR_CODES

#: Reasons the completion reconciler can return in ``reconciled.reason``.
#: They are not envelope codes: a report can be accepted with a veto reason
#: that only changes the Ticket state.
RECONCILIATION_REASONS = (
    "git_not_delivered",
    "tests_failed",
    "out_of_scope",
    "push_suspected",
)

#: Lease and handoff states are stored as text, but callers compare these.
ACTIVE_LEASE_STATES = frozenset({"active", "handoff_pending"})
LEASE_STATES = frozenset(
    {"active", "handoff_pending", "released", "expired", "completed"}
)
HANDOFF_STATES = frozenset({"pending", "accepted", "expired", "cancelled"})
REVIEW_STATES = frozenset({"pending", "completed", "expired", "cancelled"})
MESSAGE_MAX_CHARS = 2000
MESSAGE_CHANNEL_CAPACITY = 32
LEASE_SECONDS = 900
MAX_LEASE_SECONDS = 4 * 60 * 60


class CollabError(ValueError):
    """A protocol refusal carrying the code and HTTP status to report."""

    code = "invalid_schema"
    status_code = 400

    def __init__(
        self,
        message: str = "",
        *,
        code: str | None = None,
        status_code: int | None = None,
        retry_after: float | int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        resolved_code = str(code or self.code)
        if resolved_code == "ok" or resolved_code not in ERROR_CODES:
            resolved_code = "internal_error"
        super().__init__(message)
        self.code = resolved_code
        self.status_code = (
            int(status_code)
            if status_code is not None
            else ERROR_CODES.get(resolved_code, self.status_code)
        )
        self.retry_after = retry_after
        self.details = dict(details or {})

    def to_payload(self) -> dict[str, Any]:
        """The error fields safe to put on the wire (never a secret)."""
        payload: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        if self.details:
            payload["details"] = dict(self.details)
        return payload


class UnauthorizedError(CollabError):
    code = "unauthorized"
    status_code = 401


class ScopeDeniedError(CollabError):
    code = "scope_denied"
    status_code = 403


class ModeDeniedError(CollabError):
    code = "mode_denied"
    status_code = 403


class RateLimitedError(CollabError):
    code = "rate_limited"
    status_code = 429


class NotFoundError(CollabError):
    code = "not_found"
    status_code = 404


class NotClaimableError(CollabError):
    code = "not_claimable"
    status_code = 409


class AlreadyClaimedError(CollabError):
    code = "already_claimed"
    status_code = 409


class AssignmentRefusedError(CollabError):
    code = "assignment_refused"
    status_code = 409


class LeaseLostError(CollabError):
    code = "lease_lost"
    status_code = 409


class LeaseExpiredError(CollabError):
    code = "lease_expired"
    status_code = 409


class StaleRevisionError(CollabError):
    code = "stale_revision"
    status_code = 409


class SelfReviewError(CollabError):
    code = "self_review"
    status_code = 403


class InvalidSchemaError(CollabError):
    code = "invalid_schema"
    status_code = 400


class SecretInPayloadError(CollabError):
    code = "secret_in_payload"
    status_code = 400


class ChannelFullError(CollabError):
    code = "channel_full"
    status_code = 409


class TooLargeError(CollabError):
    code = "too_large"
    status_code = 413


class NotDeliverableError(CollabError):
    code = "not_deliverable"
    status_code = 409


class InternalError(CollabError):
    code = "internal_error"
    status_code = 500


#: Spellings different front ends use; keep one class identity.
CollabException = CollabError
RateLimitError = RateLimitedError
RateLimitExceeded = RateLimitedError
SchemaValidationError = InvalidSchemaError
CollabSchemaError = InvalidSchemaError
SecretsError = SecretInPayloadError


def error_for_code(
    code: str, message: str = "", *, retry_after: float | int | None = None
) -> CollabError:
    """Build the refusal class for ``code`` without a giant dispatch table."""
    classes: dict[str, type[CollabError]] = {
        "unauthorized": UnauthorizedError,
        "scope_denied": ScopeDeniedError,
        "mode_denied": ModeDeniedError,
        "rate_limited": RateLimitedError,
        "not_found": NotFoundError,
        "not_claimable": NotClaimableError,
        "already_claimed": AlreadyClaimedError,
        "assignment_refused": AssignmentRefusedError,
        "lease_lost": LeaseLostError,
        "lease_expired": LeaseExpiredError,
        "stale_revision": StaleRevisionError,
        "self_review": SelfReviewError,
        "invalid_schema": InvalidSchemaError,
        "secret_in_payload": SecretInPayloadError,
        "channel_full": ChannelFullError,
        "too_large": TooLargeError,
        "not_deliverable": NotDeliverableError,
        "internal_error": InternalError,
    }
    if code == "ok" or code not in classes:
        code = "internal_error"
    return classes[code](message, code=code, retry_after=retry_after)


def is_collab_error(value: object) -> bool:
    return isinstance(value, CollabError)


def normalize_app_id(value: str) -> str:
    """Validate the short, lower-case app id a registration chose."""
    text = str(value or "").strip().lower()
    if not _APP_ID_RE.fullmatch(text):
        raise ValueError(
            "app_id must start with a letter and contain only lower-case "
            "letters, digits and hyphens (max 64)"
        )
    return text


def normalize_agent(value: str | None) -> str:
    """An agent label is audit metadata, never an authorization decision."""
    text = str(value or "").strip()
    if len(text) > 64 or not _AGENT_RE.fullmatch(text):
        raise ValueError("agent must be at most 64 safe label characters")
    return text


def ticket_ref(ticket_id: str) -> str:
    text = str(ticket_id or "").strip().lower()
    if not _TICKET_REF_RE.fullmatch(f"t:{text}"):
        raise ValueError("ticket id is not a UUID-shaped task reference")
    return f"t:{text}"


def line_ref(dispatch_id: str, line_id: str) -> str:
    value = f"d:{dispatch_id}:{line_id}"
    if not _TASK_REF_RE.fullmatch(value):
        raise ValueError("dispatch and line ids are not a valid line reference")
    return value


def normalize_task_ref(value: str) -> str:
    """Validate and canonicalize a task reference."""
    return TaskRef.parse(value).value


@dataclass(frozen=True)
class TaskRef:
    """A parsed ``t:<ticket>`` or ``d:<dispatch>:<line>`` reference."""

    value: str
    kind: str
    ticket_id: str = ""
    dispatch_id: str = ""
    line_id: str = ""

    @classmethod
    def parse(cls, value: str) -> "TaskRef":
        text = str(value or "").strip()
        if not _TASK_REF_RE.fullmatch(text):
            raise ValueError("task reference must be t:<uuid> or d:<dispatch>:<line>")
        if text.startswith("t:"):
            return cls(value=text, kind="ticket", ticket_id=text[2:])
        _, dispatch_id, line_id = text.split(":", 2)
        return cls(value=text, kind="line", dispatch_id=dispatch_id, line_id=line_id)

    @property
    def is_ticket(self) -> bool:
        return self.kind == "ticket"

    @property
    def is_line(self) -> bool:
        return self.kind == "line"


@dataclass(frozen=True)
class AppIdentity:
    """The authenticated app and the self-reported agent label.

    ``app_id`` is the authorization identity; ``agent`` is only displayed and
    audited. Two agents claiming the same app are the same author for review.
    """

    app_id: str
    agent: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "app_id", normalize_app_id(self.app_id))
        object.__setattr__(self, "agent", normalize_agent(self.agent))

    @property
    def actor(self) -> str:
        """The ``autonomy_ticket_events.actor`` spelling."""
        return f"app:{self.app_id}" + (f"/{self.agent}" if self.agent else "")

    @property
    def review_key(self) -> str:
        """The author identity compared by the self-review check."""
        return self.app_id


def canonical_json(value: Any) -> str:
    """One stable byte string for hashing; never store the original payload."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def payload_digest(value: Any) -> str:
    return sha256_text(canonical_json(value))


__all__ = [
    "ACTIVE_LEASE_STATES",
    "AGENT_PATTERN",
    "APP_ID_PATTERN",
    "AppIdentity",
    "AlreadyClaimedError",
    "AssignmentRefusedError",
    "ChannelFullError",
    "CollabError",
    "CollabException",
    "CollabSchemaError",
    "ERROR_CODES",
    "ERROR_HTTP_STATUS",
    "HANDOFF_STATES",
    "InternalError",
    "InvalidSchemaError",
    "LEASE_SECONDS",
    "LEASE_STATES",
    "LINE_REF_PATTERN",
    "LeaseExpiredError",
    "LeaseLostError",
    "MAX_LEASE_SECONDS",
    "MESSAGE_CHANNEL_CAPACITY",
    "MESSAGE_MAX_CHARS",
    "ModeDeniedError",
    "NotClaimableError",
    "NotDeliverableError",
    "NotFoundError",
    "RECONCILIATION_REASONS",
    "REVIEW_STATES",
    "RateLimitError",
    "RateLimitExceeded",
    "RateLimitedError",
    "SchemaValidationError",
    "ScopeDeniedError",
    "SecretInPayloadError",
    "SecretsError",
    "SelfReviewError",
    "StaleRevisionError",
    "TASK_REF_PATTERN",
    "TICKET_REF_PATTERN",
    "TaskRef",
    "TooLargeError",
    "UnauthorizedError",
    "canonical_json",
    "error_for_code",
    "is_collab_error",
    "line_ref",
    "normalize_agent",
    "normalize_app_id",
    "normalize_task_ref",
    "payload_digest",
    "sha256_text",
    "ticket_ref",
]
