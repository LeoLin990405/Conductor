"""Per-credential destination allowlists: deny by default, no secret access."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock

import pytest

from src.server.services.autonomy.credentials import (
    CREDENTIAL_POLICY_VERSION,
    CredentialAllowlist,
    CredentialAllowlistError,
    CredentialBroker,
    CredentialDenied,
    audit_denial,
)


def _allowlist(**overrides):
    values = {
        "hosts": frozenset({"api.github.com"}),
        "methods": frozenset({"GET"}),
        "path_prefixes": ("/repos/acme/widgets",),
    }
    values.update(overrides)
    return CredentialAllowlist(**values)


def test_empty_allowlist_denies_every_dimension():
    empty = CredentialAllowlist()
    decision = empty.check("GET", "https://api.github.com/repos/acme/widgets")
    assert not decision.allowed
    assert "host" in decision.reason

    hosts_only = CredentialAllowlist(hosts={"api.github.com"})
    assert "method" in hosts_only.check(
        "GET", "https://api.github.com/repos/acme/widgets"
    ).reason

    no_paths = CredentialAllowlist(
        hosts={"api.github.com"}, methods={"GET"}
    )
    assert "path" in no_paths.check(
        "GET", "https://api.github.com/repos/acme/widgets"
    ).reason


def test_configured_host_method_and_path_prefix_are_allowed():
    allowlist = _allowlist()
    for path in (
        "/repos/acme/widgets",
        "/repos/acme/widgets/",
        "/repos/acme/widgets/issues/comments",
        "/repos/acme/widgets/pulls/4",
    ):
        assert allowlist.check("GET", f"https://api.github.com{path}").allowed


def test_host_method_and_path_prefix_are_each_enforced():
    allowlist = _allowlist()
    assert not allowlist.check(
        "GET", "https://evil.example/repos/acme/widgets"
    ).allowed
    assert not allowlist.check(
        "POST", "https://api.github.com/repos/acme/widgets"
    ).allowed
    assert not allowlist.check(
        "GET", "https://api.github.com/repos/other/project"
    ).allowed
    # The prefix must end at a path segment, not split a segment.
    assert not allowlist.check(
        "GET", "https://api.github.com/repos/acme/widgets-other"
    ).allowed


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@api.github.com/repos/acme/widgets",
        "https://api.github.com/repos/acme/widgets?token=x",
        "https://api.github.com/repos/acme/widgets#fragment",
        "https://api.github.com/repos/acme/widgets?",
        "https://api.github.com/repos/acme/widgets#",
        "http://api.github.com/repos/acme/widgets",
        "https://api.github.com/repos/acme/widgets/../other",
        "https://api.github.com/repos/acme/widgets/%2e%2e/other",
        'https://api.github.com/repos/acme/widgets\\other',
        "https://api.github.com/repos/acme/widgets\nother",
        "https://api.github.com:0/repos/acme/widgets",
        "/repos/acme/widgets",
        "https:///repos/acme/widgets",
    ],
)
def test_url_tricks_are_denied(url):
    decision = _allowlist().check("GET", url)
    assert not decision.allowed
    assert decision.reason


def test_loopback_http_is_allowed_but_public_http_is_not():
    allowlist = CredentialAllowlist(
        hosts={"127.0.0.1", "localhost", "::1"},
        methods={"GET"},
        path_prefixes=("/repos/acme/widgets",),
    )
    assert allowlist.check(
        "GET", "http://127.0.0.1:8080/repos/acme/widgets"
    ).allowed
    assert allowlist.check(
        "GET", "http://localhost/repos/acme/widgets"
    ).allowed
    assert allowlist.check(
        "GET", "http://[::1]/repos/acme/widgets"
    ).allowed
    assert not allowlist.check(
        "GET", "http://api.github.com/repos/acme/widgets"
    ).allowed


def test_host_match_is_case_insensitive_and_ignores_the_port():
    allowlist = _allowlist(hosts={"API.GitHub.com"})
    assert allowlist.check(
        "GET", "https://api.github.com:8443/repos/acme/widgets"
    ).allowed


def test_allowlist_from_mapping_normalizes_and_rejects_unknown_config():
    allowlist = CredentialAllowlist.from_mapping(
        {
            "hosts": [" API.GitHub.com ", "[::1]"],
            "methods": ["get", "post"],
            "path_prefixes": ["/repos/acme/widgets/", "/user"],
        }
    )
    assert allowlist.hosts == frozenset({"api.github.com", "::1"})
    assert allowlist.methods == frozenset({"GET", "POST"})
    assert allowlist.path_prefixes == ("/repos/acme/widgets", "/user")
    with pytest.raises(CredentialAllowlistError):
        CredentialAllowlist.from_mapping({"hostz": []})
    with pytest.raises(CredentialAllowlistError):
        CredentialAllowlist.from_mapping({"hosts": "api.github.com"})
    with pytest.raises(CredentialAllowlistError):
        CredentialAllowlist.from_mapping({"methods": ["GET POST"]})
    with pytest.raises(CredentialAllowlistError):
        CredentialAllowlist.from_mapping({"path_prefixes": ["repos"]})


@pytest.mark.parametrize(
    "raw",
    [
        {"hosts": [{}]},
        {"methods": [[]]},
        {"path_prefixes": [{"prefix": "/repos"}]},
        {"hosts": [None]},
        {"methods": [1]},
    ],
)
def test_allowlist_from_mapping_rejects_non_string_entries(raw):
    with pytest.raises(CredentialAllowlistError, match="entries must be strings"):
        CredentialAllowlist.from_mapping(raw)


def test_authorize_raises_and_audits_a_denial(caplog):
    with caplog.at_level(logging.WARNING):
        with pytest.raises(CredentialDenied, match="host is not in the credential allowlist"):
            _allowlist().authorize(
                "GET",
                "https://evil.example/repos/acme/widgets",
                credential_name="GITHUB_TOKEN",
                user_id="u1",
            )
    assert "event=credential_denied" in caplog.text
    assert CREDENTIAL_POLICY_VERSION in caplog.text
    assert "host=evil.example" in caplog.text


def test_audit_denial_redacts_credential_shaped_path_text(caplog):
    secret = "ghp_" + "a" * 36
    with caplog.at_level(logging.WARNING):
        audit_denial(
            credential_name="GITHUB_TOKEN",
            user_id="u1",
            method="post",
            host="evil.example",
            path=f"/collect/{secret}",
            reason="host is not in the credential allowlist",
        )
    assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text
    assert "event=credential_denied" in caplog.text
    assert "method=POST" in caplog.text
    assert "path=/collect/[REDACTED]" in caplog.text


def test_audit_denial_keeps_a_caller_supplied_path_on_one_line(caplog):
    with caplog.at_level(logging.WARNING):
        audit_denial(
            credential_name="GITHUB_TOKEN",
            user_id="u1",
            method="GET",
            host="evil.example",
            path="/collect\nhost=trusted.example",
            reason="host is not in the credential allowlist",
        )
    assert "\nhost=trusted.example" not in caplog.text
    assert "path=/collect host=trusted.example" in caplog.text


# --- the ports dimension (policy v3) ----------------------------------------


def test_a_policy_with_ports_binds_the_destination_port():
    policy = CredentialAllowlist(
        hosts={"smtp.example.com"},
        methods={"SMTP_SEND"},
        path_prefixes=("/"),
        schemes={"smtp"},
        ports={587},
    )
    assert policy.check("SMTP_SEND", "smtp://smtp.example.com:587").allowed
    assert policy.check("SMTP_SEND", "smtp://smtp.example.com:2525").allowed is False
    assert policy.check("SMTP_SEND", "smtp://smtp.example.com:465").allowed is False


def test_an_empty_ports_dimension_leaves_the_port_free():
    policy = CredentialAllowlist(hosts={"h.example"}, methods={"GET"}, path_prefixes=("/"))
    assert policy.check("GET", "https://h.example/x").allowed


def test_from_mapping_reads_and_validates_ports():
    policy = CredentialAllowlist.from_mapping({"ports": [587, 465]})
    assert policy.ports == frozenset({587, 465})
    with pytest.raises(CredentialAllowlistError):
        CredentialAllowlist.from_mapping({"ports": ["none"]})
    with pytest.raises(CredentialAllowlistError):
        CredentialAllowlist.from_mapping({"ports": [70000]})


def test_the_loopback_http_exception_is_web_policy_only():
    """R4: an SMTP capability is scheme-exact, loopback or not.

    An SMTP policy for 127.0.0.1:587 must not accept an http destination just
    because the host is loopback — that exception belongs to HTTPS (web)
    policies alone — and the refusal happens before the vault is read.
    """
    policy = CredentialAllowlist(
        hosts={"127.0.0.1"},
        methods={"SMTP_SEND"},
        path_prefixes=("/"),
        schemes={"smtp"},
        ports={587},
    )
    assert policy.check("SMTP_SEND", "smtp://127.0.0.1:587").allowed
    assert policy.check("SMTP_SEND", "http://127.0.0.1:587").allowed is False


@pytest.mark.asyncio
async def test_a_broker_use_on_an_http_destination_with_an_smtp_policy_never_reads_the_vault():
    broker = CredentialBroker(
        user_id="u-1",
        credential_name="SMTP_PASSWORD",
        allowlist=CredentialAllowlist(
            hosts={"127.0.0.1"},
            methods={"SMTP_SEND"},
            path_prefixes=("/"),
            schemes={"smtp"},
            ports={587},
        ),
        reveal_secret=AsyncMock(return_value="secret"),
    )
    with pytest.raises(CredentialDenied):
        async with broker.use(
            operation="SMTP_SEND", destination="http://127.0.0.1:587"
        ):
            pass  # pragma: no cover - the body never runs on a denial

    broker.reveal_secret.assert_not_awaited()
