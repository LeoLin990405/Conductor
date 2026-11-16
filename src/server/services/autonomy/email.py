"""The email transport: one structured reply, sent over SMTP.

Article §4.2 asks the asynchronous channels to deliver structured,
information-dense output and to draft structured emails and documents that a
person can review and send. Before this module the email channel had no
transport at all: a reply for an email Source fell through to the GitHub
comment path, so an email was posted as a GitHub comment.

The transport is deliberately small:

* :func:`build_mail_message` turns one shaped reply into a message with a
  subject and named sections — the structure the article asks for.
* :func:`send_mail_message` hands that message to SMTP. The SMTP credential
  stays in the vault and is never logged.

The send is *at least once*: the outbox row is the durable record, and a
crash between "SMTP accepted the message" and "the row was marked sent"
leaves a message that may be sent again. Every send carries a deterministic
``Message-ID`` derived from the outbox idempotency key, so the retried send
renders the identical message: duplicate-aware receivers can drop it, and a
future Sent-folder reconcile can find it. Unlike GitHub or Feishu, an SMTP
send has no mailbox scan here, so the transport still cannot prove the
message is already there before a retry.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import smtplib
from dataclasses import dataclass, replace
from email.message import EmailMessage
from typing import Any, Awaitable, Callable, Mapping

from src.server.database.user_vault_secrets import reveal_user_secret
from src.server.services.autonomy.channels import parse_email_sections
from src.server.services.autonomy.credentials import (
    CredentialAllowlist,
    CredentialBroker,
)

logger = logging.getLogger(__name__)

_ADDRESS_RE = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+$")

DEFAULT_SUBJECT = "Ticket update"


class EmailSourceError(RuntimeError):
    """An email Source check or send failed; the message is safe for the UI."""


def _smtp_allowlist(cfg: "EmailConfig") -> CredentialAllowlist:
    """The default credential policy for one email Source's SMTP credential.

    By default the credential is good for exactly the relay configured on the
    Source — its host, its port, the ``SMTP_SEND`` operation, the root path
    and the ``smtp`` scheme — so an explicit ``credential_allowlist`` can
    only narrow what the Source already names.
    """
    return CredentialAllowlist(
        hosts=frozenset({cfg.smtp_host}),
        methods=frozenset({"SMTP_SEND"}),
        path_prefixes=("/",),
        schemes=frozenset({"smtp"}),
        ports=frozenset({cfg.smtp_port}),
    )


@dataclass(frozen=True, slots=True)
class EmailConfig:
    """The Source-side configuration of one email channel."""

    from_address: str
    to_address: str
    secret_name: str
    workspace_id: str
    smtp_host: str
    smtp_port: int = 587
    smtp_username: str = ""
    use_tls: bool = True
    timeout: float = 30.0
    require_approval: bool = True
    #: Where the SMTP credential may be used; ``None`` is never stored —
    #: :meth:`from_mapping` resolves it to the Source-configured relay.
    credential_allowlist: CredentialAllowlist | None = None

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "EmailConfig":
        from_address = str(config.get("from_address") or "").strip()
        to_address = str(config.get("to_address") or "").strip()
        secret_name = str(config.get("secret_name") or "").strip()
        workspace_id = str(config.get("workspace_id") or "").strip()
        smtp_host = str(config.get("smtp_host") or "").strip()
        smtp_username = str(config.get("smtp_username") or "").strip()
        if not _ADDRESS_RE.match(from_address):
            raise EmailSourceError("from_address must be a valid email address")
        if not _ADDRESS_RE.match(to_address):
            raise EmailSourceError("to_address must be a valid email address")
        if not secret_name:
            raise EmailSourceError("secret_name is required")
        if not workspace_id:
            raise EmailSourceError("workspace_id is required")
        if not smtp_host:
            raise EmailSourceError("smtp_host is required")
        try:
            raw_port = config.get("smtp_port")
            smtp_port = 587 if raw_port in (None, "") else int(raw_port)
        except (TypeError, ValueError) as exc:
            raise EmailSourceError("smtp_port must be a number") from exc
        if not 1 <= smtp_port <= 65535:
            raise EmailSourceError("smtp_port must be between 1 and 65535")
        try:
            raw_timeout = config.get("timeout")
            timeout = 30.0 if raw_timeout in (None, "") else float(raw_timeout)
        except (TypeError, ValueError) as exc:
            raise EmailSourceError("timeout must be a number") from exc
        if not 1 <= timeout <= 300:
            raise EmailSourceError("timeout must be between 1 and 300 seconds")
        cfg = cls(
            from_address=from_address,
            to_address=to_address,
            secret_name=secret_name,
            workspace_id=workspace_id,
            smtp_host=smtp_host,
            smtp_port=smtp_port,
            smtp_username=smtp_username or from_address,
            use_tls=bool(config.get("use_tls", True)),
            timeout=timeout,
            require_approval=bool(config.get("require_approval", True)),
        )
        # An omitted mapping keeps the default (exactly the configured relay);
        # an explicit mapping overrides, so a hosts list without the configured
        # host:port denies the send before the vault is ever read.
        return replace(
            cfg,
            credential_allowlist=CredentialAllowlist.from_mapping(
                config.get("credential_allowlist"), defaults=_smtp_allowlist(cfg)
            ),
        )


@dataclass(frozen=True, slots=True)
class MailMessage:
    """One structured email: a subject and the named sections of the body."""

    to: str
    subject: str
    body: str
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """The resolved SMTP connection, including the vault-held credential."""

    host: str
    port: int
    username: str
    password: str
    from_address: str
    to_address: str
    use_tls: bool
    timeout: float


#: A seam for tests: it receives the message and the resolved settings.
SendFunc = Callable[[MailMessage, SmtpSettings], Awaitable[None]]


def build_mail_message(
    *,
    to: str,
    text: str,
    subject: str = "",
    headers: Mapping[str, str] | None = None,
) -> MailMessage:
    """Turn one shaped reply into a structured email.

    The reply text is expected to be the channel-shaped body (see
    :func:`src.server.services.autonomy.channels.shape_reply`): a
    ``Subject:`` line and one named section per block. A reply that carries
    no subject of its own keeps the given default.
    """
    body = (text or "").strip()
    parsed_subject, sections = parse_email_sections(body)
    rendered = []
    for name, section_body in sections:
        rendered.append(f"{name}:\n{section_body}".rstrip())
    return MailMessage(
        to=str(to or "").strip(),
        subject=(parsed_subject or subject or DEFAULT_SUBJECT).strip(),
        body="\n\n".join(rendered).strip(),
        headers=tuple(sorted((headers or {}).items())),
    )


def message_id_for_key(idempotency_key: str, *, from_address: str = "") -> str:
    """The deterministic RFC 2822 ``Message-ID`` for one logical reply.

    Derived from the outbox idempotency key, so a retried send after a lost
    local acknowledgement renders the identical message and a
    duplicate-aware receiver (or a Sent-folder reconcile) can recognise it.
    """
    digest = hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:32]
    domain = str(from_address or "").strip().rsplit("@", 1)[-1] or "conductor.local"
    return f"<conductor-{digest}@{domain}>"


def render_mime(mail: MailMessage, *, from_address: str) -> EmailMessage:
    """Render the message as a MIME mail, ready for ``send_message``."""
    mime = EmailMessage()
    mime["From"] = from_address
    mime["To"] = mail.to
    mime["Subject"] = mail.subject
    for name, value in mail.headers:
        mime[name] = value
    mime.set_content(mail.body)
    return mime


def check_email_source(config: Mapping[str, Any]) -> dict[str, Any]:
    """Check one email Source without touching the network.

    The relay is not dialled here: a Source must stay editable after a bad
    check, and the credential is verified when the first message is sent.
    """
    cfg = EmailConfig.from_mapping(config)
    return {
        "agent_login": cfg.from_address,
        "repository": cfg.to_address,
        "can_push": True,
        "private": False,
    }


def _email_broker(user_id: str, cfg: EmailConfig) -> CredentialBroker:
    """The broker for one email Source's SMTP credential.

    ``reveal_user_secret`` is read from the module namespace at call time —
    the injection seam the tests patch as ``email.reveal_user_secret``.
    """
    return CredentialBroker(
        user_id=user_id,
        credential_name=cfg.secret_name,
        allowlist=cfg.credential_allowlist,
        reveal_secret=reveal_user_secret,
    )


def _destination_host(host: str) -> str:
    """The host as it appears in a destination URL authority.

    An IPv6 literal must be bracketed (``smtp://[::1]:587``); a bare ``::1``
    parses as an invalid authority and the policy would refuse a relay the
    Source actually configured.
    """
    text = str(host or "").strip()
    return f"[{text}]" if ":" in text else text


async def _smtp_settings(user_id: str, cfg: EmailConfig) -> SmtpSettings:
    """Resolve the SMTP credential through the broker.

    The broker authorizes the destination — by default exactly the
    Source-configured host:port — *before* the vault is read, so a denied use
    never reaches the vault at all.
    """
    broker = _email_broker(user_id, cfg)
    async with broker.use(
        # An IPv6 literal is an authority only in brackets; a bare ::1 is an
        # invalid destination URL and would be refused by the policy check.
        operation="SMTP_SEND",
        destination=f"smtp://{_destination_host(cfg.smtp_host)}:{cfg.smtp_port}"
    ) as password:
        if not password:
            raise EmailSourceError(
                f"email secret {cfg.secret_name!r} is missing from the vault"
            )
        return SmtpSettings(
            host=cfg.smtp_host,
            port=cfg.smtp_port,
            username=cfg.smtp_username,
            password=password,
            from_address=cfg.from_address,
            to_address=cfg.to_address,
            use_tls=cfg.use_tls,
            timeout=cfg.timeout,
        )


def _smtp_send(settings: SmtpSettings, mime: EmailMessage) -> None:
    """Blocking SMTP delivery; runs in a worker thread."""
    with smtplib.SMTP(
        settings.host, settings.port, timeout=settings.timeout
    ) as client:
        if settings.use_tls:
            client.starttls()
        if settings.username:
            client.login(settings.username, settings.password)
        client.send_message(mime)


async def send_mail_message(
    user_id: str,
    config: Mapping[str, Any],
    mail: MailMessage,
    *,
    sender: SendFunc | None = None,
) -> None:
    """Send one structured email over SMTP.

    The credential is authorized against its allowlist, then read from the
    vault at send time and never logged.
    """
    cfg = EmailConfig.from_mapping(config)
    settings = await _smtp_settings(user_id, cfg)
    if sender is not None:
        await sender(mail, settings)
        return
    mime = render_mime(mail, from_address=settings.from_address)
    try:
        await asyncio.to_thread(_smtp_send, settings, mime)
    except smtplib.SMTPException as exc:
        raise EmailSourceError(f"email delivery failed: {exc}") from exc
    except OSError as exc:
        raise EmailSourceError(f"email delivery failed: {exc}") from exc
    logger.info(
        "[autonomy] email sent to=%s subject=%r", settings.to_address, mail.subject
    )
