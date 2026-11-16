"""OAuth client settings for subscription logins, read from the environment.

The public build ships no vendor OAuth client ids or secrets. Each login reads
its client settings from a ``CONDUCTOR_*_OAUTH_CLIENT_*`` variable. When one is
unset, the login's initiate route answers 503 ``oauth_client_not_configured``
instead of starting a flow that cannot finish, and any later use raises
:class:`OAuthClientNotConfigured` with the variable to set.
"""

from __future__ import annotations

import os

from fastapi import HTTPException


class OAuthClientNotConfigured(RuntimeError):
    """A subscription login's OAuth client setting is not configured."""

    def __init__(self, env: str, label: str) -> None:
        super().__init__(f"{label} sign-in is not configured: set {env}")
        self.env = env
        self.label = label


def require(env: str, label: str) -> str:
    """Return the setting ``env``, or raise :class:`OAuthClientNotConfigured`."""
    value = os.environ.get(env, "").strip()
    if not value:
        raise OAuthClientNotConfigured(env, label)
    return value


# Login -> (label, settings it needs).
ROUTE_SETTINGS: dict[str, tuple[str, tuple[str, ...]]] = {
    "codex": ("ChatGPT (Codex)", ("CONDUCTOR_CODEX_OAUTH_CLIENT_ID",)),
    "grok": ("Grok", ("CONDUCTOR_GROK_OAUTH_CLIENT_ID",)),
    "claude": ("Claude", ("CONDUCTOR_CLAUDE_OAUTH_CLIENT_ID",)),
    "gemini": ("Gemini", ("CONDUCTOR_GEMINI_OAUTH_CLIENT_ID", "CONDUCTOR_GEMINI_OAUTH_CLIENT_SECRET")),
    "kimi": ("Kimi", ("CONDUCTOR_KIMI_OAUTH_CLIENT_ID",)),
}


def missing_settings(login: str) -> list[str]:
    _label, envs = ROUTE_SETTINGS[login]
    return [env for env in envs if not os.environ.get(env, "").strip()]


def require_for_route(login: str) -> None:
    """Raise a clean 503 when ``login`` has no OAuth client configured."""
    missing = missing_settings(login)
    if missing:
        label = ROUTE_SETTINGS[login][0]
        raise HTTPException(
            status_code=503,
            detail={
                "type": "oauth_client_not_configured",
                "message": f"{label} sign-in is not configured on this server. Set {', '.join(missing)}.",
                "missing": missing,
            },
        )
