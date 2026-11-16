"""The Slack transport posts through an incoming webhook.

The webhook URL is the credential and it decides where a reply goes, so the
destination is declared in the configuration (``webhook_host``) and checked
twice: the broker authorizes the declared host before the vault is read, and
the revealed URL must land on that same host — audited with an empty path, so
the secret-bearing URL path never reaches a log line. The URL itself is
validated before use: HTTPS, an allowed host, no credentials and no query
data. These tests pin that down, and they check that the URL stays in the
vault. When the POST itself fails, the error names the failure and leaves the
webhook URL out of the message, the cause, and the traceback.
"""

from __future__ import annotations

import logging
import traceback
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from yarl import URL

from src.server.services.autonomy.credentials import (
    CredentialAllowlist,
    CredentialBroker,
    CredentialDenied,
)
from src.server.services.autonomy.effects import AutonomyEffectRunner
from src.server.services.autonomy.slack import (
    SlackConfig,
    SlackSourceError,
    _slack_broker,
    check_slack_source,
    send_slack_message,
)

WEBHOOK = "https://hooks.slack.com/services/T000/B000/abcdef"
FAKE_SECRET = "not-a-real-webhook"


def _config(**overrides) -> dict:
    config = {
        "channel_name": "#ops",
        "webhook_secret_name": "SLACK_WEBHOOK",
        "workspace_id": "w-1",
    }
    config.update(overrides)
    return config


class _Response:
    status = 200

    async def text(self) -> str:
        return "ok"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        response = _Response()
        response.status = self.status
        return response


# --- the configuration ------------------------------------------------------


def test_a_valid_slack_source_config_is_accepted():
    cfg = SlackConfig.from_mapping(_config())

    assert cfg.channel_name == "#ops"
    assert "hooks.slack.com" in cfg.allowed_hosts


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"channel_name": ""}, "channel_name"),
        ({"webhook_secret_name": ""}, "webhook_secret_name"),
        ({"workspace_id": ""}, "workspace_id"),
    ],
)
def test_a_broken_slack_source_config_is_rejected(overrides, message):
    with pytest.raises(SlackSourceError, match=message):
        SlackConfig.from_mapping(_config(**overrides))


def test_the_source_check_names_the_channel():
    checked = check_slack_source(_config())

    assert checked["agent_login"] == "#ops"
    assert checked["can_push"] is True


# --- the declared webhook host ----------------------------------------------


def test_the_declared_webhook_host_defaults_to_slacks_host():
    cfg = SlackConfig.from_mapping(_config())

    assert cfg.webhook_host == "hooks.slack.com"


def test_an_explicit_webhook_host_key_wins_over_the_allowlist():
    cfg = SlackConfig.from_mapping(
        _config(
            webhook_host=" Hooks.Example.Com ",
            allowed_webhook_hosts=["hooks.slack-gov.com"],
        )
    )

    assert cfg.webhook_host == "hooks.example.com"


def test_the_declared_webhook_host_falls_back_to_the_first_allowed_host():
    cfg = SlackConfig.from_mapping(
        _config(allowed_webhook_hosts=["hooks.slack-gov.com", "hooks.example.com"])
    )

    assert cfg.webhook_host == "hooks.slack-gov.com"


# --- the webhook URL -------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "webhook",
    [
        "http://hooks.slack.com/services/T000/B000/abcdef",
        "https://evil.example.com/hook",
        "https://hooks.slack.com:pass@hooks.slack.com/services/x",
        "https://hooks.slack.com/services/x?token=abc",
        "https://hooks.slack.com/services/x#frag",
        "not a url",
    ],
)
async def test_a_webhook_url_that_could_reach_somewhere_else_is_refused(webhook):
    session = _Session()
    with patch(
        "src.server.services.autonomy.slack.reveal_user_secret",
        AsyncMock(return_value=webhook),
    ):
        with pytest.raises(SlackSourceError):
            await send_slack_message("u-1", _config(), "Done.", session=session)

    assert session.calls == []


@pytest.mark.asyncio
async def test_the_webhook_url_is_revealed_from_the_vault_and_the_message_is_posted():
    session = _Session()
    with (
        patch(
            "src.server.services.autonomy.slack.reveal_user_secret",
            AsyncMock(return_value=WEBHOOK),
        ) as reveal,
        patch("src.server.services.autonomy.slack.audit_denial") as audit,
    ):
        await send_slack_message("u-1", _config(), "Done.", session=session)

    reveal.assert_awaited_once_with("u-1", "SLACK_WEBHOOK")
    audit.assert_not_called()
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == WEBHOOK
    assert session.calls[0]["json"] == {"text": "Done."}
    assert session.calls[0]["allow_redirects"] is False


_SENTINEL = "RAW-URL-SENTINEL"
_RAW_URL_CASES = (
    "\thttps://hooks.slack.com/services/" + _SENTINEL,
    "https://hooks.slack.com/services/" + _SENTINEL + "\n",
    "https://hooks.slack.com/services/" + _SENTINEL + " ",
    "https://hooks.slack.com/services/SEC\n" + _SENTINEL,
    "https://hooks.slack.com/services/SEC\t" + _SENTINEL,
    "https://hooks.slack.com/services/SE\x7f" + _SENTINEL,
    "https://hooks.slack.com/services/SEC\r" + _SENTINEL,
    "https://hooks.slack.com/services/SEC " + _SENTINEL,
    "",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", _RAW_URL_CASES)
async def test_a_raw_vault_url_with_blank_or_control_characters_is_refused(raw, caplog):
    session = _Session()
    reveal = AsyncMock(return_value=raw)
    audits: list[dict] = []

    def record(**fields):
        audits.append(fields)

    with (
        patch("src.server.services.autonomy.slack.reveal_user_secret", reveal),
        patch("src.server.services.autonomy.slack.audit_denial", side_effect=record),
        caplog.at_level(logging.DEBUG),
    ):
        with pytest.raises(SlackSourceError) as excinfo:
            await send_slack_message("u-1", _config(), "Done.", session=session)

    reveal.assert_awaited_once_with("u-1", "SLACK_WEBHOOK")
    assert len(audits) == 1
    assert audits[0]["path"] == ""
    if raw == "":
        assert audits[0]["host"] == ""
        assert audits[0]["reason"] == "empty_url"
    else:
        assert audits[0]["host"] == ""
        assert audits[0]["reason"] == "invalid_url_chars"
    assert session.calls == []
    broker = _slack_broker("u-1", SlackConfig.from_mapping(_config()))
    assert broker.check("POST", raw).allowed is False
    captured = "\n".join(record.getMessage() for record in caplog.records)
    blob = captured + "\n" + str(excinfo.value)
    if raw:
        assert raw not in blob
    assert _SENTINEL not in blob


@pytest.mark.asyncio
async def test_an_explicit_declared_webhook_host_sends_with_one_vault_read():
    session = _Session()
    url = "https://hooks.example.com/services/x"
    config = _config(webhook_host="hooks.example.com")
    with patch(
        "src.server.services.autonomy.slack.reveal_user_secret",
        AsyncMock(return_value=url),
    ) as reveal:
        await send_slack_message("u-1", config, "Done.", session=session)

    reveal.assert_awaited_once_with("u-1", "SLACK_WEBHOOK")
    assert session.calls[0]["url"] == url


@pytest.mark.asyncio
async def test_an_enterprise_host_has_to_be_allowed_explicitly():
    session = _Session()
    config = _config(allowed_webhook_hosts=["hooks.slack-gov.com"])
    with patch(
        "src.server.services.autonomy.slack.reveal_user_secret",
        AsyncMock(return_value="https://hooks.slack-gov.com/services/x"),
    ):
        await send_slack_message("u-1", config, "Done.", session=session)

    assert session.calls[0]["url"] == "https://hooks.slack-gov.com/services/x"


@pytest.mark.asyncio
async def test_a_missing_vault_credential_stops_the_send_before_any_connection():
    session = _Session()
    with patch(
        "src.server.services.autonomy.slack.reveal_user_secret",
        AsyncMock(return_value=None),
    ):
        with pytest.raises(SlackSourceError, match="missing from the vault"):
            await send_slack_message("u-1", _config(), "Done.", session=session)

    assert session.calls == []


# --- the declared destination and the vault ---------------------------------


@pytest.mark.asyncio
async def test_a_denied_declared_destination_never_reaches_the_vault(caplog):
    # The declared destination is authorized before the vault is read, so a
    # refused declared host costs zero vault reads and is audited. An
    # unbracketed IPv6 literal is a declared host the broker refuses as a URL
    # authority — the one way a declared destination can be denied, since the
    # allowlist is built from the declared host itself.
    session = _Session()
    reveal = AsyncMock(return_value=WEBHOOK)
    config = _config(webhook_host="::1")
    with patch(
        "src.server.services.autonomy.slack.reveal_user_secret", reveal
    ):
        with caplog.at_level(
            logging.WARNING, logger="src.server.services.autonomy.credentials"
        ):
            with pytest.raises(
                SlackSourceError, match="must be HTTPS on the declared host"
            ):
                await send_slack_message("u-1", config, "Done.", session=session)

    reveal.assert_not_awaited()
    assert session.calls == []
    assert any(
        record.name == "src.server.services.autonomy.credentials"
        and "credential_denied" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_a_vault_url_off_the_declared_host_is_refused_without_leaking_its_path(caplog):
    # Default configuration: the allowlist is not widened. The revealed host
    # is not the declared host, so the send is refused after exactly one vault
    # read and exactly one denial. The audit names the parsed host and an
    # empty path: the URL path embeds the secret and must never reach an audit
    # record, a log line, or an exception message.
    session = _Session()
    secret = "https://evil.example.com/services/SECRET-TOKEN-SENTINEL/x"
    config = _config()
    reveal = AsyncMock(return_value=secret)
    with patch(
        "src.server.services.autonomy.slack.reveal_user_secret", reveal
    ):
        with caplog.at_level(
            logging.WARNING, logger="src.server.services.autonomy.credentials"
        ):
            with pytest.raises(
                SlackSourceError, match="must be HTTPS on the declared host"
            ) as excinfo:
                await send_slack_message("u-1", config, "Done.", session=session)

    reveal.assert_awaited_once_with("u-1", "SLACK_WEBHOOK")
    assert session.calls == []
    denials = [
        record
        for record in caplog.records
        if record.name == "src.server.services.autonomy.credentials"
        and "credential_denied" in record.getMessage()
    ]
    assert len(denials) == 1
    for record in denials:
        message = record.getMessage()
        assert "host=evil.example.com" in message
        # An empty audited path: the path is where the secret lives.
        assert "path= reason=" in message
    captured = "\n".join(record.getMessage() for record in caplog.records)
    assert "SECRET-TOKEN-SENTINEL" not in captured
    assert "SECRET-TOKEN-SENTINEL" not in str(excinfo.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("vault_url", ["http://127.0.0.1:9000/hook", "http://[::1]:9000/hook"])
async def test_a_loopback_vault_url_never_bypasses_the_declared_host(vault_url, caplog):
    """R1: only a DECLARED loopback host keeps the http scheme exception.

    With the default declaration (hooks.slack.com), a vault URL on IPv4 or
    IPv6 loopback is refused, audited once, and never posted.
    """
    session = _Session()
    with patch(
        "src.server.services.autonomy.slack.reveal_user_secret",
        AsyncMock(return_value=vault_url),
    ):
        with caplog.at_level(
            logging.WARNING, logger="src.server.services.autonomy.credentials"
        ):
            with pytest.raises(SlackSourceError):
                await send_slack_message("u-1", _config(), "Done.", session=session)

    assert session.calls == []
    denials = [
        record.getMessage()
        for record in caplog.records
        if record.name == "src.server.services.autonomy.credentials"
        and "credential_denied" in record.getMessage()
    ]
    assert len(denials) == 1, denials
    assert "path= reason=" in denials[0]


def test_a_declared_loopback_host_keeps_the_scheme_exception_only():
    """A declared loopback host may use http — and the host is still compared."""
    session = _Session()
    config = _config(webhook_host="127.0.0.1")
    with patch(
        "src.server.services.autonomy.slack.reveal_user_secret",
        AsyncMock(return_value="http://127.0.0.1:9000/hook"),
    ):
        # The shape check admits the scheme (declared loopback); the equality
        # holds; the post goes through.
        import asyncio

        asyncio.run(
            send_slack_message("u-1", config, "Done.", session=session)
        )

    assert session.calls[0]["url"] == "http://127.0.0.1:9000/hook"


# --- the credential broker --------------------------------------------------


def _broker(reveal: AsyncMock | None = None) -> CredentialBroker:
    """Build the broker exactly the way ``_slack_broker`` builds it."""
    return CredentialBroker(
        user_id="u-1",
        credential_name="SLACK_WEBHOOK",
        allowlist=CredentialAllowlist(
            hosts=frozenset({"hooks.slack.com"}),
            methods=frozenset({"POST"}),
            path_prefixes=("/",),
        ),
        reveal_secret=reveal or AsyncMock(return_value=WEBHOOK),
    )


@pytest.mark.asyncio
async def test_the_broker_denies_a_destination_outside_the_allowlist_before_the_vault(caplog):
    reveal = AsyncMock(return_value=WEBHOOK)
    broker = _broker(reveal)

    with caplog.at_level(
        logging.WARNING, logger="src.server.services.autonomy.credentials"
    ):
        with pytest.raises(CredentialDenied):
            broker.require("POST", "https://evil.example.com/hook")

    reveal.assert_not_awaited()
    denials = [
        record
        for record in caplog.records
        if record.name == "src.server.services.autonomy.credentials"
        and "credential_denied" in record.getMessage()
    ]
    assert denials
    # The audit record carries metadata only, never the destination URL that
    # doubles as the secret.
    for record in denials:
        assert WEBHOOK not in record.getMessage()
        assert "https://evil.example.com/hook" not in record.getMessage()


# --- client failures while posting ------------------------------------------


SENTINEL = "TEST-TRANSPORT-SECRET-SENTINEL"
SECRET_URL = f"https://hooks.slack.com/services/{SENTINEL}"


class _RaisingSession:
    """``_Session`` posts. This one raises from ``post`` before a response."""

    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls: list[str] = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        raise self._error


def _response_error() -> aiohttp.ClientResponseError:
    request_info = SimpleNamespace(real_url=URL(SECRET_URL))
    return aiohttp.ClientResponseError(
        request_info=request_info,
        history=(),
        status=502,
        message="malformed upstream response",
    )


class _FakeEffectStore:
    """In-memory effect store, same shape as the effects-runner tests."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.acked: list[str] = []
        self.nacked: list[tuple[str, str, int]] = []

    def add(self, effect_id: str, kind: str, attempts: int = 1) -> None:
        self.rows.append(
            {
                "effect_id": effect_id,
                "user_id": "u1",
                "kind": kind,
                "idempotency_key": f"key:{effect_id}",
                "payload": {},
                "attempts": attempts,
            }
        )

    async def park_exhausted_effects(self, *, max_attempts: int) -> list[dict]:
        return []

    async def claim_due_effects(
        self, *, limit: int, lease_seconds: float, max_attempts: int
    ) -> list[dict]:
        claimed, self.rows = self.rows[:limit], self.rows[limit:]
        return claimed

    async def ack_effect(self, *, effect_id: str, attempts: int) -> bool:
        self.acked.append(effect_id)
        return True

    async def nack_effect(
        self,
        *,
        effect_id: str,
        attempts: int,
        error: str,
        retry_delay_seconds: float,
        max_attempts: int,
    ) -> bool:
        self.nacked.append((effect_id, error, attempts))
        return True


def _assert_text_hides_the_webhook(text: str) -> None:
    assert SENTINEL not in text
    assert SECRET_URL not in text


def _assert_error_hides_the_webhook(exc: BaseException) -> None:
    _assert_text_hides_the_webhook(str(exc))
    _assert_text_hides_the_webhook(repr(exc))
    assert exc.__cause__ is None
    if exc.__context__ is not None:
        _assert_text_hides_the_webhook(str(exc.__context__))
        _assert_text_hides_the_webhook(repr(exc.__context__))
    formatted = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _assert_text_hides_the_webhook(formatted)


def _patched_webhook():
    return patch(
        "src.server.services.autonomy.slack.reveal_user_secret",
        AsyncMock(return_value=SECRET_URL),
    )


@pytest.mark.asyncio
async def test_a_client_response_error_does_not_leak_the_webhook_url():
    session = _RaisingSession(_response_error())
    with _patched_webhook():
        with pytest.raises(SlackSourceError) as excinfo:
            await send_slack_message("u-1", _config(), "Done.", session=session)

    exc = excinfo.value
    assert session.calls == [SECRET_URL]
    assert type(exc) is SlackSourceError
    assert str(exc) == "Slack delivery failed"
    assert exc.code == "slack_http_error"
    assert exc.status == 502
    _assert_error_hides_the_webhook(exc)


@pytest.mark.asyncio
async def test_a_client_response_error_is_nacked_without_the_webhook_url(caplog):
    store = _FakeEffectStore()
    store.add("e1", "slack_reply")
    session = _RaisingSession(_response_error())

    async def handler(effect):
        await send_slack_message("u-1", _config(), "Done.", session=session)

    runner = AutonomyEffectRunner(store=store)
    runner.register("slack_reply", handler)
    with (
        _patched_webhook(),
        caplog.at_level(logging.WARNING, logger="src.server.services.autonomy.effects"),
    ):
        assert await runner.run_once() == 1
        await runner.wait_idle()

    assert session.calls == [SECRET_URL]
    assert store.acked == []
    assert len(store.nacked) == 1
    _assert_text_hides_the_webhook(store.nacked[0][1])
    warnings = [
        "\n".join(
            part
            for part in (record.getMessage(), record.exc_text or "")
            if part
        )
        for record in caplog.records
        if record.name == "src.server.services.autonomy.effects"
        and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    _assert_text_hides_the_webhook(warnings[0])


@pytest.mark.asyncio
async def test_a_connection_error_is_a_safe_unreachable_error():
    session = _RaisingSession(aiohttp.ClientConnectionError(SENTINEL))
    with _patched_webhook():
        with pytest.raises(SlackSourceError) as excinfo:
            await send_slack_message("u-1", _config(), "Done.", session=session)

    exc = excinfo.value
    assert session.calls == [SECRET_URL]
    assert type(exc) is SlackSourceError
    assert str(exc) == "Slack is unreachable"
    assert exc.code == "slack_unreachable"
    _assert_error_hides_the_webhook(exc)


@pytest.mark.asyncio
async def test_a_timeout_is_a_safe_timeout_error():
    # aiohttp 3.14 accepts a message. ServerTimeoutError is a
    # ClientConnectionError and a TimeoutError, so this has to surface as
    # slack_timeout rather than slack_unreachable.
    session = _RaisingSession(aiohttp.ServerTimeoutError(SENTINEL))
    with _patched_webhook():
        with pytest.raises(SlackSourceError) as excinfo:
            await send_slack_message("u-1", _config(), "Done.", session=session)

    exc = excinfo.value
    assert session.calls == [SECRET_URL]
    assert type(exc) is SlackSourceError
    assert str(exc) == "Slack delivery timed out"
    assert exc.code == "slack_timeout"
    assert exc.status is None
    _assert_error_hides_the_webhook(exc)
