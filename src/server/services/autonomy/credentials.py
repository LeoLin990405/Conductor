"""Per-credential destination allowlists for credential brokers.

A broker that only checks the operation still trusts whatever endpoint it was
handed. This module makes the destination part of the credential's own policy:
each credential names the hosts, HTTP methods and path prefixes it may be used
for. Everything else is denied before the vault is read, and a denial is logged
as a structured audit event that never contains the secret value.

The allowlist is deny-by-default: an empty allowlist permits nothing. Path
prefixes match at a path-segment boundary, so ``/repos/acme/widgets`` does not
permit ``/repos/acme/widgets-other``. Custom enterprise hosts are explicit
entries, and HTTP is accepted only on loopback for local tests.

The audit record intentionally carries only non-secret request metadata:
credential name, user id, method, host, path and the deny reason. Parameters
and bodies are never part of it, and the broker authorizes before it asks the
vault for the secret.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Mapping
from urllib.parse import urlsplit

from src.server.database.user_vault_secrets import reveal_user_secret
from src.server.services.autonomy.security import redact_external_text

logger = logging.getLogger(__name__)

#: Bumped when the allowlist semantics change; included in every denial record.
#: v2 adds the ``schemes`` dimension so non-HTTP credentials (SMTP) can name
#: the scheme their destination uses instead of being forced into HTTPS. v3
#: adds the ``ports`` dimension: a policy that names ports binds the
#: destination to exactly those ports, not just to the host.
CREDENTIAL_POLICY_VERSION = "credential-allowlist-v3"

#: The only hosts for which plain HTTP is accepted (local tests).
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

_METHOD_RE = re.compile(r"[A-Z][A-Z0-9_-]{0,15}")
_SCHEME_RE = re.compile(r"[a-z][a-z0-9+.-]{1,15}")
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")
_AUDIT_TEXT_LIMIT = 200


class CredentialAllowlistError(ValueError):
    """A credential allowlist is malformed; the message is safe for an operator."""


class CredentialDenied(RuntimeError):
    """A credential use is outside its allowlist; the message is safe for logs."""


@dataclass(frozen=True, slots=True)
class CredentialDecision:
    """The result of checking one request against one credential allowlist."""

    allowed: bool
    reason: str = ""
    method: str = ""
    host: str = ""
    path: str = ""


def _normalize_host(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        pass
    if not text or len(text) > 253:
        raise CredentialAllowlistError("a host must be a non-empty host name")
    labels = text.split(".")
    if not all(_HOST_LABEL_RE.fullmatch(label) for label in labels):
        raise CredentialAllowlistError(
            "a host must be a host name or IP address without a scheme, port or wildcard"
        )
    return text


def _normalize_method(value: str) -> str:
    text = str(value or "").strip().upper()
    if not _METHOD_RE.fullmatch(text):
        raise CredentialAllowlistError(
            "a method must be an HTTP method token such as GET or POST"
        )
    return text


def _normalize_scheme(value: str) -> str:
    text = str(value or "").strip().lower()
    if not _SCHEME_RE.fullmatch(text):
        raise CredentialAllowlistError(
            "a scheme must be a lowercase URL scheme such as https or smtp"
        )
    return text


def _normalize_port(value: Any) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise CredentialAllowlistError("a port must be a number") from exc
    if not 1 <= port <= 65535:
        raise CredentialAllowlistError("a port must be between 1 and 65535")
    return port


def _as_port_sequence(raw: Mapping[str, Any]) -> list[int]:
    items: list[int] = []
    for item in _as_sequence(raw, "ports"):
        items.append(_normalize_port(item))
    return items


def _normalize_path_prefix(value: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("/") or text.startswith("//"):
        raise CredentialAllowlistError("a path prefix must start with a single '/'")
    if any(char in text for char in "\\%?#") or any(char.isspace() for char in text):
        raise CredentialAllowlistError(
            "a path prefix must not carry an encoded path, query data or whitespace"
        )
    if any(ord(char) < 0x20 for char in text):
        raise CredentialAllowlistError("a path prefix must not contain control characters")
    parsed = urlsplit(text)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        raise CredentialAllowlistError("a path prefix must be a path only")
    if any(segment in {".", ".."} for segment in parsed.path.split("/")):
        raise CredentialAllowlistError("a path prefix must not contain dot segments")
    return parsed.path.rstrip("/") or "/"


def _as_sequence(raw: Mapping[str, Any], name: str) -> Iterable[Any]:
    value = raw.get(name, ())
    if value is None:
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(
        value, (list, tuple, set, frozenset)
    ):
        raise CredentialAllowlistError(f"{name} must be a list")
    return value


def _as_string_sequence(raw: Mapping[str, Any], name: str) -> tuple[str, ...]:
    """Require list entries to be strings before any frozenset construction.

    Nested objects such as ``{}`` or ``[]`` are not hashable; constructing a
    frozenset from them raises ``TypeError`` and escapes as a 500. Reject them
    here so Source validation returns a client error instead.
    """
    items: list[str] = []
    for item in _as_sequence(raw, name):
        if not isinstance(item, str):
            raise CredentialAllowlistError(f"{name} entries must be strings")
        items.append(item)
    return tuple(items)


@dataclass(frozen=True, slots=True)
class CredentialAllowlist:
    """The destinations one credential may be used for.

    ``hosts`` match the URL host name (case-insensitively), ``methods`` match
    the HTTP method (or the operation token for non-HTTP credentials, such as
    ``SMTP_SEND``), ``path_prefixes`` match the URL path at a segment
    boundary, ``schemes`` names the URL schemes the credential may use
    (HTTPS by default; a non-HTTP credential lists its own scheme, such as
    ``smtp``), and ``ports`` binds the destination to exactly the named
    ports. A missing or empty set of hosts, methods or schemes denies every
    request; an empty ``ports`` leaves the port unrestricted.
    """

    hosts: frozenset[str] = frozenset()
    methods: frozenset[str] = frozenset()
    path_prefixes: tuple[str, ...] = ()
    schemes: frozenset[str] = frozenset({"https"})
    ports: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "hosts", frozenset(_normalize_host(host) for host in self.hosts)
        )
        object.__setattr__(
            self, "methods", frozenset(_normalize_method(method) for method in self.methods)
        )
        object.__setattr__(
            self,
            "path_prefixes",
            tuple(sorted({_normalize_path_prefix(prefix) for prefix in self.path_prefixes})),
        )
        object.__setattr__(
            self,
            "schemes",
            frozenset(_normalize_scheme(scheme) for scheme in self.schemes),
        )
        object.__setattr__(
            self, "ports", frozenset(_normalize_port(port) for port in self.ports)
        )

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, Any] | None,
        *,
        defaults: "CredentialAllowlist | None" = None,
    ) -> "CredentialAllowlist":
        """Build an allowlist from its config mapping.

        With ``defaults``, an omitted dimension keeps the default while an
        explicit empty list denies that dimension. Without defaults every
        dimension is empty, so the allowlist denies every request.
        """
        if raw is None:
            return defaults if defaults is not None else cls()
        if not isinstance(raw, Mapping):
            raise CredentialAllowlistError("credential_allowlist must be a mapping")
        unknown = set(raw) - {"hosts", "methods", "path_prefixes", "schemes", "ports"}
        if unknown:
            raise CredentialAllowlistError(
                "credential_allowlist takes only hosts, methods, path_prefixes, "
                "schemes and ports"
            )
        base = defaults or cls()
        return cls(
            hosts=(
                frozenset(_as_string_sequence(raw, "hosts"))
                if "hosts" in raw
                else base.hosts
            ),
            methods=(
                frozenset(_as_string_sequence(raw, "methods"))
                if "methods" in raw
                else base.methods
            ),
            path_prefixes=(
                _as_string_sequence(raw, "path_prefixes")
                if "path_prefixes" in raw
                else base.path_prefixes
            ),
            schemes=(
                frozenset(_as_string_sequence(raw, "schemes"))
                if "schemes" in raw
                else base.schemes
            ),
            ports=(
                frozenset(_as_port_sequence(raw))
                if "ports" in raw
                else base.ports
            ),
        )

    def check(self, method: str, url: str) -> CredentialDecision:
        """Check one absolute URL without network, vault or secret access."""
        method_text = str(method or "").strip().upper()
        text_url = str(url or "")
        try:
            parsed = urlsplit(text_url)
            port = parsed.port  # Access validates the port syntax and range.
            host = _normalize_host(parsed.hostname) if parsed.hostname else ""
        except (ValueError, CredentialAllowlistError):
            return CredentialDecision(False, "destination URL is invalid", method_text)
        path = redact_external_text(parsed.path or "/")[:_AUDIT_TEXT_LIMIT]

        def deny(reason: str) -> CredentialDecision:
            return CredentialDecision(False, reason, method_text, host, path)

        if port is not None and not 1 <= port <= 65535:
            return deny("destination port is invalid")
        if self.ports and port not in self.ports:
            return deny("destination port is not in the credential allowlist")

        if any(ord(char) < 0x20 for char in text_url) or any(
            char.isspace() for char in text_url
        ):
            return deny("destination URL contains whitespace or control characters")
        if "\\" in text_url:
            return deny("destination URL is not normalized")
        if not parsed.scheme or not parsed.netloc:
            return deny("destination URL must be absolute")
        if parsed.username is not None or parsed.password is not None:
            return deny("destination URL must not embed credentials")
        if parsed.query or parsed.fragment or "?" in text_url or "#" in text_url:
            return deny("destination URL must not carry query data")
        if not host:
            return deny("destination host is missing")
        scheme = parsed.scheme.lower()
        # The loopback HTTP exception is a web-policy convenience: it applies
        # only when the policy itself is an HTTPS one. A policy that names a
        # non-web scheme (smtp) matches its scheme exactly — an SMTP relay on
        # loopback does not become an HTTP destination.
        if scheme not in self.schemes and not (
            "https" in self.schemes
            and scheme == "http"
            and host in LOOPBACK_HOSTS
        ):
            return deny("destination scheme is not in the credential allowlist")
        if host not in self.hosts:
            return deny("host is not in the credential allowlist")
        if method_text not in self.methods:
            return deny("method is not in the credential allowlist")
        if "%" in parsed.path:
            return deny("path is not normalized")
        if any(segment in {".", ".."} for segment in parsed.path.split("/")):
            return deny("path is not normalized")
        if not any(_path_matches(parsed.path or "/", prefix) for prefix in self.path_prefixes):
            return deny("path is outside the credential allowlist")
        return CredentialDecision(True, "", method_text, host, path)

    def authorize(
        self,
        method: str,
        url: str,
        *,
        credential_name: str = "",
        user_id: str = "",
    ) -> None:
        """Raise :class:`CredentialDenied` and audit the record when not allowed."""
        decision = self.check(method, url)
        if decision.allowed:
            return
        audit_denial(
            credential_name=credential_name,
            user_id=user_id,
            method=decision.method,
            host=decision.host,
            path=decision.path,
            reason=decision.reason,
        )
        raise CredentialDenied(decision.reason)


def _path_matches(path: str, prefix: str) -> bool:
    if prefix == "/":
        return True
    return path == prefix or path.startswith(prefix + "/")


def _audit_value(value: object, *, upper: bool = False, limit: int = _AUDIT_TEXT_LIMIT) -> str:
    text = str(value or "")
    if upper:
        text = text.upper()
    # Keep one denial on one log line even when a caller-supplied path or name
    # carries control characters.
    text = "".join(
        " " if ord(char) < 0x20 or ord(char) == 0x7F else char for char in text
    )
    return redact_external_text(text)[:limit]


def audit_denial(
    *,
    credential_name: str,
    user_id: str,
    method: str,
    host: str = "",
    path: str = "",
    reason: str,
) -> None:
    """Record one denied credential use.

    Callers authorize before reading the secret, so this function has no secret
    to leak. It still redacts the supplied metadata in case a caller put
    credential-shaped text in a path or name.
    """
    logger.warning(
        "[autonomy] credential denied event=credential_denied policy=%s "
        "credential=%s user=%s method=%s host=%s path=%s reason=%s",
        CREDENTIAL_POLICY_VERSION,
        _audit_value(credential_name),
        _audit_value(user_id),
        _audit_value(method, upper=True),
        _audit_value(host),
        _audit_value(path),
        _audit_value(reason),
    )


@dataclass(frozen=True, slots=True)
class CredentialBroker:
    """The single choke point between a credential's policy and the vault.

    Every credential use in ``services/autonomy`` goes through a broker: it
    authorizes the destination against the credential's allowlist *before*
    the vault is read, a denial is audited without the secret, and the secret
    exists only inside the caller's scope — never in a log line, an exception
    or a return value. The allowlist is deny-by-default: a broker without an
    explicit allowlist authorizes nothing.

    ``reveal_secret`` is a seam for host wiring (and the tests that patch the
    per-module name); production callers never pass it, so the read goes to
    the real vault.
    """

    user_id: str
    credential_name: str
    allowlist: CredentialAllowlist | None = None
    reveal_secret: Callable[[str, str], Awaitable[str]] | None = None

    @property
    def policy(self) -> CredentialAllowlist:
        return self.allowlist or CredentialAllowlist()

    def check(self, operation: str, destination: str) -> CredentialDecision:
        """Judge one destination without touching the vault or the network."""
        return self.policy.check(operation, destination)

    async def reveal(self) -> str:
        """The one vault read for this credential.

        Only call this after :meth:`require` allowed the destination — the
        broker methods that compose the two (:meth:`use`) keep that order, so
        a denied use never reaches the vault at all.
        """
        reveal = self.reveal_secret or reveal_user_secret
        return await reveal(self.user_id, self.credential_name)

    def require(self, operation: str, destination: str) -> None:
        """Authorize one destination; audit and raise :class:`CredentialDenied` when refused.

        Safe to call before or after :meth:`reveal`: the audit record carries
        only the request metadata, never the secret or the request body.
        """
        decision = self.check(operation, destination)
        if decision.allowed:
            return
        audit_denial(
            credential_name=self.credential_name,
            user_id=self.user_id,
            method=decision.method,
            host=decision.host,
            path=decision.path,
            reason=decision.reason,
        )
        raise CredentialDenied(decision.reason)

    @asynccontextmanager
    async def use(self, *, operation: str, destination: str) -> AsyncIterator[str]:
        """Authorize the destination, then reveal the secret into the scope.

        A denied use audits and raises before the vault is read, so the vault
        read count for a denial is exactly zero; an allowed use reads the
        vault exactly once.
        """
        self.require(operation, destination)
        yield await self.reveal()
