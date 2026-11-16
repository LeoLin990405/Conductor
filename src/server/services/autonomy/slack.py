"""The Slack transport: one reply, posted through an incoming webhook.

Slack belongs to the IM family in the channel model: light replies, a 100-word
cap, and no progress updates. The transport is a single webhook POST, so the
credential is the webhook URL itself; it stays in the vault and is never
logged.

The webhook URL decides where the message goes, so the destination is declared
in the configuration (``webhook_host``, never a secret) and checked twice: the
CredentialBroker authorizes ``https://<webhook_host>/`` before the vault is
read, so a refused destination is audited as a structured event that carries no
secret and never reads the vault; and the URL the vault reveals must land on
that same declared host, a check that audits the host only — the URL's path
embeds the secret and never reaches an audit record. The URL must still be
HTTPS only (HTTP only on loopback), with no credentials or query data. The
default declared host is Slack's own webhook host; an Enterprise Grid
deployment on its own domain sets ``webhook_host`` (or lists that host first
in ``allowed_webhook_hosts``) explicitly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

import aiohttp

from src.server.database.user_vault_secrets import reveal_user_secret
from src.server.services.autonomy.credentials import (
    CredentialAllowlist,
    CredentialBroker,
    CredentialDecision,
    CredentialDenied,
    audit_denial,
)
from src.server.services.autonomy.security import redact_external_text

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_HOST = "hooks.slack.com"
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class SlackSourceError(RuntimeError):
    """A Slack Source check or send failed; the message is safe for the UI."""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class SlackConfig:
    """The Source-side configuration of one Slack channel."""

    channel_name: str
    webhook_secret_name: str
    workspace_id: str
    allowed_hosts: tuple[str, ...] = (SLACK_WEBHOOK_HOST,)
    #: The declared destination host for the webhook POST; not a secret. The
    #: broker authorizes ``https://<webhook_host>/`` before the vault is read.
    webhook_host: str = SLACK_WEBHOOK_HOST
    require_approval: bool = False

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "SlackConfig":
        channel_name = str(config.get("channel_name") or "").strip()
        webhook_secret_name = str(config.get("webhook_secret_name") or "").strip()
        workspace_id = str(config.get("workspace_id") or "").strip()
        if not channel_name:
            raise SlackSourceError("channel_name is required")
        if not webhook_secret_name:
            raise SlackSourceError("webhook_secret_name is required")
        if not workspace_id:
            raise SlackSourceError("workspace_id is required")
        entries = [
            str(host).strip().lower()
            for host in (config.get("allowed_webhook_hosts") or [])
            if str(host).strip()
        ]
        hosts = set(entries)
        hosts.add(SLACK_WEBHOOK_HOST)
        webhook_host = str(config.get("webhook_host") or "").strip().lower()
        if not webhook_host:
            webhook_host = entries[0] if entries else SLACK_WEBHOOK_HOST
        hosts.add(webhook_host)
        return cls(
            channel_name=channel_name,
            webhook_secret_name=webhook_secret_name,
            workspace_id=workspace_id,
            allowed_hosts=tuple(sorted(hosts)),
            webhook_host=webhook_host,
            require_approval=bool(config.get("require_approval", False)),
        )


_CANONICAL_WEBHOOK_REFUSAL = (
    "the Slack webhook URL must be HTTPS on the declared host, without "
    "credentials or query data"
)


def _reject_raw_url(raw: str) -> str | None:
    """Reject a vault URL before strip, parse, or send.

    ``empty_url`` is only the empty string. Any whitespace, any character
    below U+0020, or DEL is ``invalid_url_chars``. A space belongs in the
    value only as ``%20``. ``None`` means these two checks passed.
    """
    if raw == "":
        return "empty_url"
    if any(char.isspace() or ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        return "invalid_url_chars"
    return None


class _SlackWebhookBroker(CredentialBroker):
    """The channel broker. ``check`` uses the same raw-URL rejection as send."""

    def check(self, operation: str, destination: str) -> CredentialDecision:
        raw = destination if isinstance(destination, str) else ""
        reason = _reject_raw_url(raw)
        if reason is not None:
            method = str(operation or "").strip().upper()
            return CredentialDecision(False, reason, method, "", "")
        return super().check(operation, raw)


def _validated_webhook_url(
    value: str,
    declared_host: str,
    *,
    credential_name: str,
    user_id: str,
) -> str:
    """Return the webhook URL when it is safe to POST to, else refuse it.

    Every refusal is audited before the raise, and every audit record names
    the host and an empty path only: the webhook path embeds the token, so it
    never reaches an audit record, a log line or an exception message. The
    revealed host must equal the declared one exactly — a loopback declared
    host keeps the local-test scheme exception (http allowed, and only the
    scheme: the host is still compared); for any other declaration the scheme
    is HTTPS and nothing else. IPv6 loopback is outside v1 and stays refused.

    The vault value is used as read. It is not stripped. A refusal audits
    once: the host is the parsed host, or ``""`` when the value never reached
    a parse, and the path is always ``""``.
    """
    raw = value if isinstance(value, str) else ""
    host_text = ""

    def deny(reason: str) -> None:
        audit_denial(
            credential_name=credential_name,
            user_id=user_id,
            method="POST",
            host=host_text,
            path="",
            reason=reason,
        )
        raise SlackSourceError(_CANONICAL_WEBHOOK_REFUSAL) from None

    early = _reject_raw_url(raw)
    if early is not None:
        deny(early)
    if "\\" in raw:
        deny("the webhook URL is not normalized")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        deny("the Slack webhook URL is invalid")
    host_text = (host or "").lower()
    declared = str(declared_host or "").strip().lower()
    declared_is_loopback = declared in _LOOPBACK
    if (
        not raw
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or any(segment in {".", ".."} for segment in parsed.path.split("/"))
        or parsed.scheme
        not in ({"http", "https"} if declared_is_loopback else {"https"})
        or (parsed.scheme == "http" and not declared_is_loopback)
    ):
        deny("the webhook URL is not a valid HTTPS webhook on the declared host")
    if port is not None and not 1 <= port <= 65535:
        deny("the webhook URL has an invalid port")
    if host_text != declared:
        deny("the webhook host does not match the declared destination")
    return raw


def check_slack_source(config: Mapping[str, Any]) -> dict[str, Any]:
    """Check one Slack Source without touching the network."""
    cfg = SlackConfig.from_mapping(config)
    return {
        "agent_login": cfg.channel_name,
        "repository": cfg.channel_name,
        "can_push": True,
        "private": False,
    }


def _slack_broker(user_id: str, cfg: SlackConfig) -> CredentialBroker:
    """The broker for one channel's webhook credential.

    The declared destination is the configured ``webhook_host``, so the
    broker's allowlist names exactly that host, POST, any path, and the
    authorized destination is the pathless ``https://<webhook_host>/``. That
    authorization runs before the vault is read, so a declared-host denial is
    audited and raised with zero vault reads. ``check`` and the post-read
    validator share ``_reject_raw_url``. A refusal of the revealed URL audits
    the parsed host, or an empty host when the value never parsed, and an
    empty path — the path embeds the secret.
    ``reveal_user_secret`` is read from the module namespace at call time, so
    the tests that patch :data:`reveal_user_secret` keep working through the
    broker.
    """
    return _SlackWebhookBroker(
        user_id=user_id,
        credential_name=cfg.webhook_secret_name,
        allowlist=CredentialAllowlist(
            hosts=frozenset({cfg.webhook_host}),
            methods=frozenset({"POST"}),
            path_prefixes=("/",),
        ),
        reveal_secret=reveal_user_secret,
    )


def _client_failure(exc: BaseException) -> SlackSourceError:
    """Map a client failure to a fixed message with no request URL in it.

    ``str(ClientResponseError)`` includes ``request_info.real_url``, which is
    the token-bearing webhook. The message stays one of three fixed strings;
    an HTTP status lives only on ``status``. ``TimeoutError`` is checked
    before the unreachable fallback because ``ServerTimeoutError`` is both a
    ``TimeoutError`` and a ``ClientConnectionError``.
    """
    if isinstance(exc, aiohttp.ClientResponseError):
        status = exc.status
        return SlackSourceError(
            "Slack delivery failed",
            code="slack_http_error",
            status=status if isinstance(status, int) else None,
        )
    if isinstance(exc, TimeoutError):
        return SlackSourceError("Slack delivery timed out", code="slack_timeout")
    return SlackSourceError("Slack is unreachable", code="slack_unreachable")


async def send_slack_message(
    user_id: str,
    config: Mapping[str, Any],
    text: str,
    *,
    session: aiohttp.ClientSession | None = None,
) -> None:
    """Post one reply to a Slack channel through its incoming webhook."""
    cfg = SlackConfig.from_mapping(config)
    broker = _slack_broker(user_id, cfg)
    # The declared destination is known from the configuration, so the broker
    # authorizes it before the vault is read: a declared-host denial costs
    # zero vault reads and is audited by the broker.
    failure: SlackSourceError | None = None
    try:
        async with broker.use(
            operation="POST", destination=f"https://{cfg.webhook_host}/"
        ) as webhook:
            if webhook is None:
                raise SlackSourceError(
                    f"Slack webhook secret {cfg.webhook_secret_name!r} is missing from the vault"
                )
            url = _validated_webhook_url(
                webhook,
                cfg.webhook_host,
                credential_name=cfg.webhook_secret_name,
                user_id=user_id,
            )
            body = (text or "").strip()
            if not body:
                raise SlackSourceError("the Slack message is empty")
            payload = {"text": body}
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            if session is not None:
                await _post(session, url, payload, webhook)
            else:
                async with aiohttp.ClientSession(timeout=timeout) as own:
                    await _post(own, url, payload, webhook)
    except (aiohttp.ClientError, TimeoutError) as exc:
        failure = _client_failure(exc)
    except CredentialDenied as exc:
        raise SlackSourceError(_CANONICAL_WEBHOOK_REFUSAL) from exc
    if failure is not None:
        raise failure
    logger.info("[autonomy] slack reply sent channel=%s", cfg.channel_name)


async def _post(
    session: aiohttp.ClientSession,
    url: str,
    payload: dict[str, Any],
    secret: str,
) -> None:
    async with session.post(
        url,
        json=payload,
        allow_redirects=False,
        headers={"Content-Type": "application/json"},
    ) as resp:
        if resp.status >= 400:
            text = redact_external_text(await resp.text(), secrets=(secret,))[:300]
            raise SlackSourceError(f"Slack API {resp.status}: {text}")
