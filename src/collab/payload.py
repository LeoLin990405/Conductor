"""Reject credential-shaped text before a collaboration payload is stored.

The rule from N80 is intentionally blunt: a payload that looks like it carries
a credential is refused, not silently redacted and stored. Redaction still
happens on the way out (the HTTP layer owns that), but the durable row must not
contain a secret in the first place. Findings never repeat the matched text.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.collab.types import SecretInPayloadError

#: Named, high-confidence credential shapes. Keep each pattern anchored enough
#: that ordinary prose and commit hashes do not match.
SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "openai_like": re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}"),
    "stripe_live": re.compile(r"(?<![A-Za-z0-9_])sk_live_[A-Za-z0-9]{16,}"),
    "stripe_test": re.compile(r"(?<![A-Za-z0-9_])sk_test_[A-Za-z0-9]{16,}"),
    "prefixed_token": re.compile(
        r"(?i)(?<![A-Za-z0-9_])(?:tok|token|pat|secret)_[A-Za-z0-9_-]{16,}"
    ),
    "github_token": re.compile(
        r"(?<![A-Za-z0-9_])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"
    ),
    "github_fine_grained": re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}"),
    "aws_access_key": re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])"),
    "google_api_key": re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{30,}"),
    "slack_token": re.compile(r"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{10,}"),
    "jwt": re.compile(
        r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\."
        r"[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    ),
    "private_key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "bearer_header": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    "assigned_secret": re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|"
        r"password|passwd|secret)\b\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{8,})"
    ),
}

#: A long, mixed-case, digit-bearing run is a token even without a known
#: vendor prefix. Lower-case hex (a commit SHA) and a hyphenated UUID do not
#: match the case/digit requirements, so ordinary delivery evidence survives.
_GENERIC_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{32,})(?![A-Za-z0-9_-])")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
#: A field *named* like a credential is refused even if its value has no
#: vendor prefix. ``token_sha256`` is deliberately not a match: it names a
#: digest, not a bearer credential.
_SENSITIVE_KEY = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?key|secret|client[_-]?secret|password|"
    r"passwd|token|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"bearer[_-]?token|private[_-]?key)"
)


@dataclass(frozen=True)
class SecretFinding:
    """One suspected credential. It names where, never what."""

    path: str
    kind: str

    def __str__(self) -> str:
        return f"{self.path}: {self.kind}"


def _is_generic_token(candidate: str) -> bool:
    if len(candidate) < 32:
        return False
    # Task references and delivery evidence contain UUIDs.
    if _UUID.fullmatch(candidate):
        return False
    # A path or dotted module is not a token. Base64url tokens have neither.
    if "." in candidate or "/" in candidate:
        return False
    has_upper = any(char.isupper() for char in candidate)
    has_lower = any(char.islower() for char in candidate)
    has_digit = any(char.isdigit() for char in candidate)
    if has_upper and has_lower and has_digit:
        return True
    # A lower-case-only run with a non-hex character and a digit is still
    # token-shaped, unless it carries a path/slug separator. Underscores and
    # hyphens turn such a run into ordinary file and module names -- for
    # example ``tests/unit/migrations/test_118_deliberation_groups_continue.py``
    # splits into lower-case segments that must not be refused as credentials.
    # Named token prefixes (``tok_``, ``ghp_`` and friends) are matched above,
    # so this loosening does not hide a labelled credential. Pure hex is left
    # alone because commit SHAs and digests are ordinary delivery evidence.
    if (
        has_lower
        and has_digit
        and "_" not in candidate
        and "-" not in candidate
        and any(char not in "0123456789abcdef" for char in candidate)
    ):
        return True
    return False


def _add(findings: list[SecretFinding], path: str, kind: str) -> None:
    finding = SecretFinding(path=path or "$", kind=kind)
    if finding not in findings:
        findings.append(finding)


def _scan_text(
    text: str,
    path: str,
    *,
    findings: list[SecretFinding],
    extra_secrets: tuple[str, ...],
) -> None:
    if not text:
        return
    for secret in extra_secrets:
        if secret and secret in text:
            _add(findings, path, "known_secret")
            break
    for kind, pattern in SECRET_PATTERNS.items():
        if pattern.search(text):
            _add(findings, path, kind)
    for match in _GENERIC_TOKEN.finditer(text):
        if _is_generic_token(match.group(1)):
            _add(findings, path, "token_like")
            break


def _walk(
    value: Any,
    path: str,
    *,
    findings: list[SecretFinding],
    extra_secrets: tuple[str, ...],
) -> None:
    if isinstance(value, str):
        _scan_text(value, path, findings=findings, extra_secrets=extra_secrets)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            child = f"{path}.{key}" if path else str(key)
            if (
                isinstance(item, str)
                and item
                and _SENSITIVE_KEY.fullmatch(str(key))
            ):
                _add(findings, child, "secret_field")
            # Even a metadata field can carry a pasted token. Scan every
            # string; only the reported path changes.
            _walk(item, child, findings=findings, extra_secrets=extra_secrets)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            _walk(
                item,
                f"{path}[{index}]",
                findings=findings,
                extra_secrets=extra_secrets,
            )


def find_secrets(
    payload: Any, *, extra_secrets: Iterable[str] = ()
) -> list[SecretFinding]:
    """Return every suspected credential path in ``payload``.

    ``extra_secrets`` are exact values from the user's vault (or another
    trusted source); they are checked before the shape patterns so a secret
    with an unusual format is still caught.
    """
    secrets = tuple(
        str(secret)
        for secret in extra_secrets
        if isinstance(secret, str) and len(secret) >= 4
    )
    findings: list[SecretFinding] = []
    _walk(payload, "", findings=findings, extra_secrets=secrets)
    return findings


def contains_secret(payload: Any, *, extra_secrets: Iterable[str] = ()) -> bool:
    return bool(find_secrets(payload, extra_secrets=extra_secrets))


def looks_secret(text: str, *, extra_secrets: Iterable[str] = ()) -> bool:
    return contains_secret(text, extra_secrets=extra_secrets)


def secret_kinds(
    payload: Any, *, extra_secrets: Iterable[str] = ()
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                finding.kind
                for finding in find_secrets(payload, extra_secrets=extra_secrets)
            }
        )
    )


def assert_no_secrets(payload: Any, *, extra_secrets: Iterable[str] = ()) -> None:
    """Raise ``secret_in_payload`` without echoing the secret.

    The refusal names only the paths and kinds, which is enough for an app to
    find the bad field and is safe to log and audit.
    """
    findings = find_secrets(payload, extra_secrets=extra_secrets)
    if not findings:
        return
    locations = ", ".join(f"{finding.path} ({finding.kind})" for finding in findings)
    raise SecretInPayloadError(
        f"payload contains credential-like text at {locations}",
        details={"fields": [finding.path for finding in findings]},
    )


#: Names L2/L3 may use interchangeably.
scan_payload = find_secrets
assert_no_secret = assert_no_secrets
ensure_no_secrets = assert_no_secrets


__all__ = [
    "SECRET_PATTERNS",
    "SecretFinding",
    "assert_no_secret",
    "assert_no_secrets",
    "contains_secret",
    "ensure_no_secrets",
    "find_secrets",
    "looks_secret",
    "scan_payload",
    "secret_kinds",
]
