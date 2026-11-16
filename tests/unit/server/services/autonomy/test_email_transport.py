"""The email transport: a structured message, sent over SMTP.

Before this port the email channel had no transport at all — a reply for an
email Source was posted to GitHub as a comment. These tests pin the transport
down: the message it builds, the MIME it renders, and the fact that the
credential stays in the vault and never travels with the message.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.server.services.autonomy.credentials import (
    CredentialAllowlistError,
    CredentialDenied,
)
from src.server.services.autonomy.email import (
    EmailConfig,
    EmailSourceError,
    build_mail_message,
    check_email_source,
    message_id_for_key,
    render_mime,
    send_mail_message,
)

FAKE_SECRET = "not-a-real-password"


def _config(**overrides) -> dict:
    config = {
        "from_address": "conductor@example.com",
        "to_address": "ops@example.com",
        "secret_name": "EMAIL_SMTP_PASSWORD",
        "workspace_id": "w-1",
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
    }
    config.update(overrides)
    return config


# --- the configuration -----------------------------------------------------


def test_a_valid_email_source_config_is_accepted():
    cfg = EmailConfig.from_mapping(_config())

    assert cfg.from_address == "conductor@example.com"
    assert cfg.to_address == "ops@example.com"
    assert cfg.smtp_port == 587
    assert cfg.smtp_username == "conductor@example.com"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"from_address": "not-an-address"}, "from_address"),
        ({"to_address": "nope"}, "to_address"),
        ({"secret_name": ""}, "secret_name"),
        ({"smtp_host": ""}, "smtp_host"),
        ({"smtp_port": 0}, "smtp_port"),
    ],
)
def test_a_broken_email_source_config_is_rejected_with_a_readable_message(
    overrides, message
):
    with pytest.raises(EmailSourceError, match=message):
        EmailConfig.from_mapping(_config(**overrides))


def test_the_source_check_names_the_mailbox_without_dialling_the_relay():
    checked = check_email_source(_config())

    assert checked["agent_login"] == "conductor@example.com"
    assert checked["repository"] == "ops@example.com"
    assert checked["can_push"] is True


def test_the_default_credential_allowlist_names_exactly_the_configured_relay():
    cfg = EmailConfig.from_mapping(_config())

    assert cfg.credential_allowlist is not None
    assert cfg.credential_allowlist.hosts == frozenset({"smtp.example.com"})
    assert cfg.credential_allowlist.methods == frozenset({"SMTP_SEND"})
    assert cfg.credential_allowlist.path_prefixes == ("/",)
    assert cfg.credential_allowlist.schemes == frozenset({"smtp"})


def test_an_explicit_credential_allowlist_overrides_the_dimensions_it_names():
    cfg = EmailConfig.from_mapping(
        _config(credential_allowlist={"hosts": ["smtp.example.com", "relay.example.com"]})
    )

    assert cfg.credential_allowlist.hosts == frozenset(
        {"smtp.example.com", "relay.example.com"}
    )
    # The dimensions the mapping omits keep their defaults.
    assert cfg.credential_allowlist.methods == frozenset({"SMTP_SEND"})
    assert cfg.credential_allowlist.schemes == frozenset({"smtp"})


def test_a_malformed_credential_allowlist_is_rejected_at_config_time():
    with pytest.raises(CredentialAllowlistError):
        EmailConfig.from_mapping(_config(credential_allowlist={"hosts": 42}))


# --- the structured message ------------------------------------------------


def test_the_message_keeps_the_sections_of_the_reply():
    mail = build_mail_message(
        to="ops@example.com",
        text="Subject: Conductor update\n\nSummary:\nFixed it.\n\nVerification:\n42 tests pass.",
    )

    assert mail.subject == "Conductor update"
    assert "Summary:\nFixed it." in mail.body
    assert "Verification:\n42 tests pass." in mail.body


def test_a_reply_without_a_subject_keeps_the_default_one():
    mail = build_mail_message(to="ops@example.com", text="Just done.")

    assert mail.subject == "Ticket update"


def test_the_rendered_mime_carries_the_recipient_the_subject_and_the_body():
    mail = build_mail_message(
        to="ops@example.com", text="Subject: Release ready\n\nSummary:\nShip it."
    )

    mime = render_mime(mail, from_address="conductor@example.com")

    assert mime["To"] == "ops@example.com"
    assert mime["From"] == "conductor@example.com"
    assert mime["Subject"] == "Release ready"
    assert "Ship it." in mime.get_content()


# --- the send --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_send_resolves_the_credential_from_the_vault_and_sends_the_message():
    sent: list[tuple] = []

    async def sender(mail, settings):
        sent.append((mail, settings))

    with patch(
        "src.server.services.autonomy.email.reveal_user_secret",
        AsyncMock(return_value=FAKE_SECRET),
    ) as reveal:
        await send_mail_message(
            "u-1", _config(), build_mail_message(to="ops@example.com", text="Done."), sender=sender
        )

    reveal.assert_awaited_once_with("u-1", "EMAIL_SMTP_PASSWORD")
    mail, settings = sent[0]
    assert mail.to == "ops@example.com"
    assert settings.host == "smtp.example.com"
    assert settings.port == 587
    # The credential is used for the connection and never travels with the mail.
    assert settings.password == FAKE_SECRET
    assert FAKE_SECRET not in mail.body
    assert FAKE_SECRET not in mail.subject


@pytest.mark.asyncio
async def test_a_missing_vault_credential_stops_the_send_before_any_connection():
    sender = AsyncMock()

    with patch(
        "src.server.services.autonomy.email.reveal_user_secret",
        AsyncMock(return_value=None),
    ):
        with pytest.raises(EmailSourceError, match="missing from the vault"):
            await send_mail_message(
                "u-1",
                _config(),
                build_mail_message(to="ops@example.com", text="Done."),
                sender=sender,
            )

    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_credential_allowlist_that_excludes_the_relay_denies_before_the_vault():
    sender = AsyncMock()

    with patch(
        "src.server.services.autonomy.email.reveal_user_secret",
        AsyncMock(return_value=FAKE_SECRET),
    ) as reveal:
        with pytest.raises(CredentialDenied, match="allowlist"):
            await send_mail_message(
                "u-1",
                _config(credential_allowlist={"hosts": ["other.example.com"]}),
                build_mail_message(to="ops@example.com", text="Done."),
                sender=sender,
            )

    # The authorization runs before the vault: zero secret reads on a denial.
    reveal.assert_not_awaited()
    sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_default_config_authorizes_the_configured_relay_and_reads_the_vault_once():
    sent: list[tuple] = []

    async def sender(mail, settings):
        sent.append((mail, settings))

    with patch(
        "src.server.services.autonomy.email.reveal_user_secret",
        AsyncMock(return_value=FAKE_SECRET),
    ) as reveal:
        await send_mail_message(
            "u-1", _config(), build_mail_message(to="ops@example.com", text="Done."), sender=sender
        )

    reveal.assert_awaited_once_with("u-1", "EMAIL_SMTP_PASSWORD")
    assert sent[0][1].password == FAKE_SECRET


# --- the deterministic Message-ID --------------------------------------------


def test_the_message_id_is_deterministic_per_idempotency_key():
    one = message_id_for_key("autonomy:reply:t-1:result:4:done")
    same = message_id_for_key("autonomy:reply:t-1:result:4:done")
    other = message_id_for_key("autonomy:reply:t-1:result:5:done!")

    assert one == same
    assert one != other


def test_the_message_id_uses_the_senders_domain_and_stays_rfc2822_shaped():
    message_id = message_id_for_key(
        "autonomy:reply:t-1:result:4:done", from_address="conductor@example.com"
    )

    assert message_id.startswith("<conductor-")
    assert message_id.endswith("@example.com>")
    # No reply text and no characters that would break a MIME header.
    assert "done" not in message_id
    assert "\n" not in message_id and "\r" not in message_id


def test_the_message_id_falls_back_to_a_local_domain_without_a_sender():
    message_id = message_id_for_key("autonomy:reply:t-1:result:4:done")

    assert message_id.endswith("@conductor.local>")


def test_the_rendered_mime_carries_the_deterministic_message_id():
    mail = build_mail_message(
        to="ops@example.com",
        text="Subject: Release ready\n\nSummary:\nShip it.",
        headers={"Message-ID": message_id_for_key("key-1", from_address="conductor@example.com")},
    )

    mime = render_mime(mail, from_address="conductor@example.com")

    assert mime["Message-ID"] == message_id_for_key(
        "key-1", from_address="conductor@example.com"
    )


# --- the SMTP capability binds the port and honours IPv6 relays (B3, B4) -----


def test_the_default_smtp_capability_binds_the_configured_port():
    policy = EmailConfig.from_mapping(_config()).credential_allowlist
    assert policy.ports == frozenset({587})
    assert policy.check("SMTP_SEND", "smtp://smtp.example.com:587").allowed
    assert policy.check("SMTP_SEND", "smtp://smtp.example.com:2525").allowed is False
    assert policy.check("SMTP_SEND", "smtp://smtp.example.com:465").allowed is False


def test_an_explicit_policy_keeps_its_own_port_binding():
    cfg = EmailConfig.from_mapping(_config(credential_allowlist={"ports": [465]}))
    assert cfg.credential_allowlist.ports == frozenset({465})
    assert (
        cfg.credential_allowlist.check("SMTP_SEND", "smtp://smtp.example.com:587").allowed
        is False
    )


def test_a_malformed_port_is_rejected_at_config_time():
    with pytest.raises(CredentialAllowlistError):
        EmailConfig.from_mapping(_config(credential_allowlist={"ports": ["none"]}))


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["::1", "2001:db8::1", "smtp.example.com"])
async def test_an_ipv6_relay_reaches_the_sender_like_a_dns_host(host):
    """An IPv6 literal is bracketed in the destination, so the relay sends."""
    sent = []

    async def sender(mail, settings):
        sent.append(settings)

    with patch(
        "src.server.services.autonomy.email.reveal_user_secret",
        AsyncMock(return_value=FAKE_SECRET),
    ) as reveal:
        await send_mail_message(
            "u-1",
            _config(smtp_host=host),
            build_mail_message(to="ops@example.com", text="Done."),
            sender=sender,
        )

    reveal.assert_awaited_once_with("u-1", "EMAIL_SMTP_PASSWORD")
    assert len(sent) == 1
    assert sent[0].host == host
