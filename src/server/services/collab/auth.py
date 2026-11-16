"""App-token authentication for ``/api/v1/collab`` (N80 3.2, 4.4, 8).

Three rules, checked before anything else touches a service:

* **Loopback only.** The caller's address must be a loopback address and the
  request must not carry a proxy forwarding header, whatever address the
  server happens to bind. A remote caller gets 403.
* **No impersonation.** ``X-Service-Token`` and ``X-User-Id`` are refused on
  every collab tool route (401): the internal service token can act as any
  user, and the tool routes accept only a per-app bearer token. The operator
  routes (``/admin/apps``) are the one exception: there the service token is
  the required operator credential (:func:`require_operator_credential`).
* **Digest lookup.** The bearer token is hashed with SHA-256 and the digest is
  looked up; a missing, malformed, unknown, revoked or disabled token is the
  same 401. The plaintext never leaves this function.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from src.collab.scopes import normalize_scopes
from src.collab.types import (
    AppIdentity,
    InvalidSchemaError,
    ScopeDeniedError,
    UnauthorizedError,
    normalize_agent,
)
from src.server.database.collab import apps as app_db

#: Headers that let a privileged caller pick a user. Never accepted here.
IMPERSONATION_HEADERS = ("x-service-token", "x-user-id")
#: Headers a proxy adds; a loopback socket carrying them is a forwarded call.
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-real-ip")
#: Optional, audit-only agent label header (the L3 client sends it).
AGENT_HEADER = "x-collab-agent"

_REFUSED = "a valid collaboration app token is required"


@dataclass(frozen=True)
class Caller:
    """The authenticated app; ``agent`` is a self-reported label only."""

    app: Mapping[str, Any]
    identity: AppIdentity

    @property
    def app_id(self) -> str:
        return self.identity.app_id

    @property
    def agent(self) -> str:
        return self.identity.agent

    @property
    def actor(self) -> str:
        return self.identity.actor

    @property
    def user_id(self) -> str:
        return str(self.app.get("user_id") or "")

    @property
    def scopes(self) -> frozenset[str]:
        try:
            return normalize_scopes(self.app.get("scopes") or [])
        except ValueError:
            return frozenset()

    @property
    def can_write(self) -> bool:
        return bool(self.app.get("can_write"))

    @property
    def max_leases(self) -> int:
        try:
            return max(1, min(3, int(self.app.get("max_leases") or 1)))
        except (TypeError, ValueError):
            return 1

    @property
    def enabled(self) -> bool:
        return bool(self.app.get("enabled")) and self.app.get("revoked_at") is None

    def with_agent(self, agent: str) -> "Caller":
        return Caller(app=self.app, identity=AppIdentity(self.app_id, agent))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if str(key).lower() == name:
            return value
    return None


def is_loopback_host(host: str | None) -> bool:
    text = str(host or "").strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


def require_loopback(client_host: str | None, headers: Mapping[str, str]) -> None:
    """403 for any caller that is not a direct loopback connection."""
    forwarded = any(_header(headers, name) for name in FORWARDING_HEADERS)
    if forwarded or not is_loopback_host(client_host):
        raise ScopeDeniedError(
            "non_loopback: collaboration routes accept loopback callers only",
            details={"reason": "non_loopback"},
        )


def refuse_impersonation(headers: Mapping[str, str]) -> None:
    """401 when a service token or a user override is presented."""
    if any(_header(headers, name) is not None for name in IMPERSONATION_HEADERS):
        raise UnauthorizedError(
            "service_token_refused: collaboration routes accept only app tokens",
            details={"reason": "service_token_refused"},
        )


#: The header the operator routes take the internal service token in, the
#: same header every other privileged local caller of Conductor sends.
OPERATOR_CREDENTIAL_HEADER = "x-service-token"


def require_operator_credential(
    headers: Mapping[str, str], environ: Mapping[str, str] | None = None
) -> None:
    """401 unless ``X-Service-Token`` equals ``INTERNAL_SERVICE_TOKEN``.

    The ``/admin/apps`` routes register apps, grant scopes, rotate and revoke
    tokens and read the audit trail, so an explicit operator credential is
    required on every one of them: the normal user check returns the local
    user with no credential at all in OSS mode, which would let any local
    process (a registered app included) manage apps. The secret is read at
    call time and compared in constant time; an unset secret refuses every
    admin call rather than opening the routes.
    """
    from src.server.utils.api import service_token_matches

    env = os.environ if environ is None else environ
    secret = str(env.get("INTERNAL_SERVICE_TOKEN") or "").strip()
    if not secret:
        raise UnauthorizedError(
            "operator_credential_unconfigured: the operator routes need "
            "INTERNAL_SERVICE_TOKEN on the server",
            details={"reason": "operator_credential_unconfigured"},
        )
    presented = str(_header(headers, OPERATOR_CREDENTIAL_HEADER) or "").strip()
    if not presented:
        raise UnauthorizedError(
            "missing_operator_credential: send the internal service token as X-Service-Token",
            details={"reason": "missing_operator_credential"},
        )
    if not service_token_matches(presented, secret):
        raise UnauthorizedError(
            "bad_operator_credential: the operator credential was refused",
            details={"reason": "bad_operator_credential"},
        )


def bearer_token(headers: Mapping[str, str]) -> str:
    value = _header(headers, "authorization")
    if not value:
        raise UnauthorizedError(f"missing_token: {_REFUSED}")
    scheme, _, token = str(value).partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        raise UnauthorizedError(f"malformed_token: {_REFUSED}")
    return token


def is_internal_service_token(token: str) -> bool:
    """The internal service token is never a collab credential."""
    from src.server.utils.api import service_token_matches

    service = os.getenv("INTERNAL_SERVICE_TOKEN", "")
    return bool(service) and service_token_matches(token, service)


async def authenticate(headers: Mapping[str, str], *, agent: str = "") -> Caller:
    """Resolve the bearer token to its app, or raise 401."""
    token = bearer_token(headers)
    if is_internal_service_token(token):
        raise UnauthorizedError(f"service_token_refused: {_REFUSED}")
    digest = app_db.hash_token(token)
    del token
    app = await app_db.app_for_token_hash(digest)
    if app is None or app.get("revoked_at") is not None or not app.get("enabled"):
        raise UnauthorizedError(f"unknown_token: {_REFUSED}")
    try:
        label = normalize_agent(agent)
    except ValueError as exc:
        raise InvalidSchemaError("agent must be at most 64 safe label characters") from exc
    return Caller(app=dict(app), identity=AppIdentity(str(app["app_id"]), label))


async def token_belongs_to_app(headers: Mapping[str, str]) -> bool:
    """Whether the presented bearer is a collab app token (admin routes refuse it)."""
    try:
        token = bearer_token(headers)
    except UnauthorizedError:
        return False
    digest = app_db.hash_token(token)
    del token
    return await app_db.app_for_token_hash(digest) is not None


def agent_from(headers: Mapping[str, str], arguments: Mapping[str, Any]) -> str:
    value = arguments.get("agent")
    if isinstance(value, str) and value:
        return value
    return str(_header(headers, AGENT_HEADER) or "")


__all__ = [
    "AGENT_HEADER",
    "Caller",
    "FORWARDING_HEADERS",
    "IMPERSONATION_HEADERS",
    "OPERATOR_CREDENTIAL_HEADER",
    "agent_from",
    "authenticate",
    "bearer_token",
    "is_internal_service_token",
    "is_loopback_host",
    "refuse_impersonation",
    "require_loopback",
    "require_operator_credential",
    "token_belongs_to_app",
]
