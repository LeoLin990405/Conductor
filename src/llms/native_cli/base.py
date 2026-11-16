"""Shared contract for native agent CLI adapters."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence


# What an earlier Kimi adapter stored in place of a session id. It meant
# "continue the last session of this working directory"; no CLI knows a
# session by that name. A turn that finds it starts a fresh session.
LEGACY_SESSION_SENTINELS = frozenset({"continue"})

# What a session id may look like before it becomes ``--session <id>`` or
# ``--resume <id>``: it starts with a letter or digit (never "-", so it cannot
# read as a flag) and holds no whitespace or shell metacharacter. Kimi's
# ``session_<uuid>`` and Grok's hex or UUID ids fit. The launch script repeats
# this check (dispatch_launch.sh).
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def resumable_session_id(session_id: str | None) -> str | None:
    """The id a turn may resume, or None when there is nothing to resume.

    An empty id, a legacy sentinel and anything that does not match
    ``SESSION_ID_PATTERN`` are not session ids: passing one to a CLI would
    fail the turn, or be read as a flag.
    """
    if not isinstance(session_id, str):
        return None
    value = session_id.strip()
    if not value or value.lower() in LEGACY_SESSION_SENTINELS:
        return None
    if SESSION_ID_PATTERN.fullmatch(value) is None:
        return None
    return value


class NativeCliError(RuntimeError):
    """The CLI was found but the invocation or its event stream failed."""


class NativeCliNoFinalAnswer(NativeCliError):
    """The turn finished without error but produced no final assistant message.

    A Worker turn like this has no completion report: it is settled as
    waiting for a person, not as a CLI failure.
    """


class NativeCliUnavailable(NativeCliError):
    """The CLI is not installed (or not configured) on this host."""


class ReadOnlyUnavailable(NativeCliError):
    """The CLI has no read-only mode this layer can select, so it may not run read-only work."""


_AUTH_FAILURE_MARKERS = (
    "not logged in",
    "not signed in",
    "please login",
    "please log in",
    "login required",
    "sign in required",
    "unauthorized",
    "unauthenticated",
    "invalid api key",
    "api key is required",
    "api key required",
    "missing api key",
    "no api key found",
    "credential required",
    "missing credentials",
    "authentication_error",
    "invalid_api_key",
    "invalidapikey",
)


def raise_if_auth_failure(provider: str, diagnostic: str) -> None:
    """Replace known sign-in failures with a safe message, never echoing diagnostics."""
    lowered = diagnostic.lower()
    is_401_status = lowered.strip() == "401" or bool(
        re.search(r"\b(?:http(?: status)?|status code)\s*[:=]?\s*401\b", lowered)
    )
    is_403_status = bool(
        re.search(r"\b(?:http(?: status)?|status code)\s*[:=]?\s*403\b", lowered)
    )
    markers = _AUTH_FAILURE_MARKERS
    if is_403_status:
        markers = tuple(
            marker
            for marker in _AUTH_FAILURE_MARKERS
            if marker not in {"unauthorized", "unauthenticated"}
        )
    if "no api key found" in lowered:
        raise NativeCliUnavailable("No API key found") from None
    if is_401_status or any(marker in lowered for marker in markers):
        raise NativeCliUnavailable(
            f"{provider} is not logged in or has no usable credentials"
        ) from None


@dataclass(frozen=True)
class NativeCliUsage:
    """The provider's own accounting, as the CLI reported it.

    Cache fields are kept separate from input/output: the whole point of this
    layer is measuring cache reuse, not hiding it inside a total.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "NativeCliUsage":
        data = raw or {}
        return cls(
            input_tokens=int(data.get("inputTokens") or data.get("input_tokens") or 0),
            output_tokens=int(data.get("outputTokens") or data.get("output_tokens") or 0),
            cache_read_tokens=int(
                data.get("cacheReadTokens")
                or data.get("cached_input_tokens")
                or data.get("cachedInputTokens")
                or data.get("cache_read_input_tokens")
                or data.get("cached_tokens")
                or data.get("cache_read_tokens")
                or data.get("cache_read")
                or 0
            ),
            cache_write_tokens=int(
                data.get("cacheWriteTokens")
                or data.get("cache_write_input_tokens")
                or data.get("cacheWriteInputTokens")
                or data.get("cache_creation_input_tokens")
                or 0
            ),
            total_tokens=int(
                data.get("totalTokens")
                or data.get("total_tokens")
                or 0
            ),
        )


@dataclass(frozen=True)
class NativeCliResult:
    """One completed native-agent turn."""

    provider: str
    text: str
    session_id: str | None = None
    usage: NativeCliUsage = field(default_factory=NativeCliUsage)
    duration_seconds: float = 0.0
    events: int = 0
    raw: Sequence[Mapping[str, Any]] = ()

    @property
    def cache_read_ratio(self) -> float:
        """Share of input tokens served from the provider cache."""
        input_side = self.usage.input_tokens + self.usage.cache_read_tokens
        return self.usage.cache_read_tokens / input_side if input_side else 0.0


@dataclass(frozen=True)
class CliInvocation:
    """A fully-built process invocation; adapters own the argument grammar."""

    argv: tuple[str, ...]
    stdin: str = ""
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    timeout_seconds: float = 300.0
