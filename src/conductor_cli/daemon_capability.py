"""Per-Ticket operation capability for a BYOM local daemon.

A policy says what a provider may do on this device. A capability says what
this one Ticket may do: a subset of those operations, bound to the ticket id,
the daemon id, the policy version and an expiry. The cloud can narrow that
set. It cannot widen it, and it cannot move the grant onto another ticket,
another daemon, another policy or a later time.

This module does not sign a grant and does not check a signature. A
``GrantVerifier`` authenticates the grant and returns the capability it
carries. Phase 2 supplies the verifier that checks a signature. Until one is
installed, a policy-backed daemon has nothing it can trust, and it refuses
the Ticket.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from .rollout_modes import MODES, check_mode


class GrantError(ValueError):
    """The grant is not a capability this daemon can read.

    Raised for a malformed grant. A verifier also raises it when the grant
    does not authenticate. The message is safe to show: it must not echo the
    grant itself.
    """


class GrantVerifier(Protocol):
    """Authenticates one grant and returns the capability it authorizes.

    The daemon calls this and then enforces the binding itself. An
    implementation may check a signature. This phase does not provide one,
    and a verifier that skips authentication must not be installed in the
    daemon process.
    """

    def verify(self, grant: Mapping[str, Any]) -> OperationCapability:
        """Return the capability ``grant`` authorizes, or raise GrantError."""


@dataclass(frozen=True)
class OperationCapability:
    """The operations one Ticket may run, and the binding that keeps it there."""

    ticket_id: str
    daemon_id: str
    policy_version: str
    expires_at: datetime
    operations: frozenset[str]


#: N44 rollout mode (N58): off | shadow | enforce, default shadow. ``enforce``
#: refuses a policy-backed Ticket whose signed grant is missing, rejected or
#: unverifiable; ``shadow`` logs that refusal and runs the Ticket under the
#: local policy allowlist alone; ``off`` skips the signature check. The local
#: policy allowlist, its version binding and the operation check stay
#: enforced in every mode.
GRANT_VERIFIER_MODE_ENV = "CONDUCTOR_DAEMON_GRANT_VERIFIER_MODE"
GRANT_VERIFIER_MODES = MODES


def grant_verifier_mode(environ: Mapping[str, str] | None = None) -> str:
    """The N44 mode; ``shadow`` (with a warning) when unset or not a mode."""
    return check_mode(GRANT_VERIFIER_MODE_ENV, environ)


@dataclass(frozen=True)
class CapabilityDecision:
    """Whether a verified capability authorizes this Ticket to run."""

    allowed: bool
    reason: str = ""


def parse_capability(body: Mapping[str, Any]) -> OperationCapability:
    """Read a capability body.

    This does not authenticate the body. A ``GrantVerifier`` proves the grant
    is authentic and then parses it; the daemon never calls this on a grant
    the verifier has not accepted.
    """
    if not isinstance(body, Mapping):
        raise GrantError("the operation capability is not a mapping")
    return OperationCapability(
        ticket_id=_text(body.get("ticket_id"), "ticket id"),
        daemon_id=_text(body.get("daemon_id"), "daemon id"),
        policy_version=_text(body.get("policy_version"), "policy version"),
        expires_at=_expiry(body.get("expires_at")),
        operations=frozenset(_operation_names(body.get("operations"))),
    )


def authorize(
    capability: OperationCapability,
    *,
    ticket_id: str,
    daemon_id: str,
    policy_version: str,
    allowed_operations: frozenset[str],
    requested: Iterable[str],
    now: datetime | None = None,
) -> CapabilityDecision:
    """Whether this capability lets this Ticket run the operations it names.

    Fail closed: a missing id, a different binding, an expiry that has
    arrived, an operation the device does not allow, or a requested operation
    the capability does not name, all refuse the Ticket.
    """
    if not _same_id(str(ticket_id or "").strip(), capability.ticket_id):
        return CapabilityDecision(
            False, "the operation capability is for a different ticket"
        )
    bound_daemon = str(daemon_id or "").strip()
    if not bound_daemon:
        return CapabilityDecision(
            False, "this daemon has no id to bind an operation capability to"
        )
    if not _same_id(bound_daemon, capability.daemon_id):
        return CapabilityDecision(
            False, "the operation capability is for a different daemon"
        )
    current_version = str(policy_version or "")
    if current_version != capability.policy_version:
        return CapabilityDecision(
            False,
            "the operation capability was granted under policy "
            f"{capability.policy_version or '(none)'}, not {current_version or '(none)'}",
        )
    current = _aware_now(now)
    if capability.expires_at <= current:
        return CapabilityDecision(False, "the operation capability expired")
    outside_policy = sorted(set(capability.operations) - set(allowed_operations))
    if outside_policy:
        return CapabilityDecision(
            False,
            "the operation capability names "
            f"{', '.join(outside_policy)}, which this run does not allow",
        )
    outside_request = sorted(set(requested) - set(capability.operations))
    if outside_request:
        return CapabilityDecision(
            False,
            f"operation {', '.join(outside_request)} is outside the operation capability",
        )
    return CapabilityDecision(True, "")


def refuse_result(
    capability: OperationCapability,
    detected: Iterable[str],
    *,
    policy_version: str,
    now: datetime | None = None,
) -> str:
    """Why a CLI result must not be reported, or an empty string when it may.

    The result is classified with the same operation patterns as the request.
    An operation it implies that the capability does not name is not reported,
    and neither is a result whose capability has expired or whose policy
    version is no longer the file on disk. The returned reason never includes
    the result text.
    """
    if str(policy_version or "") != capability.policy_version:
        return (
            "the local policy changed after the operation capability was granted"
        )
    if capability.expires_at <= _aware_now(now):
        return "the operation capability expired"
    outside = sorted(set(detected) - set(capability.operations))
    if outside:
        return (
            f"operation {', '.join(outside)} is outside the operation capability"
        )
    return ""


def _same_id(left: str, right: str) -> bool:
    """True when both ids are the same token, or the same UUID in any case."""
    if left == right:
        return True
    try:
        return uuid.UUID(left) == uuid.UUID(right)
    except (ValueError, AttributeError):
        return False


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GrantError(f"the capability {label} is missing")
    if any(character.isspace() for character in value):
        raise GrantError(f"the capability {label} is missing")
    return value


def _operation_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise GrantError("the capability names no operations")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise GrantError("the capability names an operation that is not text")
        name = item.strip()
        if not name or any(character.isspace() for character in name):
            raise GrantError("the capability names an empty operation")
        names.append(name)
    return tuple(names)


def _expiry(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise GrantError("the capability expiry is missing")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise GrantError("the capability expiry is not a timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GrantError("the capability expiry has no timezone")
    if parsed.microsecond:
        raise GrantError("the capability expiry must be whole seconds")
    return parsed.astimezone(timezone.utc)


def _aware_now(now: datetime | None) -> datetime:
    current = now if now is not None else datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise GrantError("the capability clock has no timezone")
    return current.astimezone(timezone.utc)
