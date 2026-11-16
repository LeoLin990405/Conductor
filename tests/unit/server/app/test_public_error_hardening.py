"""Public failure text: a fixed HTTP 500 body and a hardened daemon scrubber.

The HTTP 500 body never carries exception text. The daemon's failure report
does carry a scrubbed sentence, so every shape the older blocklist let
through has a case here.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.server.public_errors import PUBLIC_MESSAGE_LIMIT, scrub_public_message

# (raw text, fragments that must not survive)
_LEAK_CASES = {
    "github_fine_grained_pat": (
        "push failed github_pat_" "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGHIJ",
        ("github_pat_" "11ABCDEFG", "abcdefghijklmnopqrstuvwxyz"),
    ),
    "github_classic_pat": (
        "push failed ghp_" "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
        ("ghp_AbCdEf", "Yz0123456789"),
    ),
    "gitlab_pat": ("gitlab said glpat-" "AbCdEf1234567890xyzQ", ("glpat-", "1234567890xyzQ")),
    "huggingface": ("hub hf_" "AbCdEfGhIjKlMnOpQrStUvWxYz012345", ("hf_AbCd", "Yz012345")),
    "npm": ("npm npm_" "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789", ("npm_AbCd", "Yz0123456789")),
    "azure_sig": (
        "GET https://x.blob.core.windows.net/a?sv=2024&sig=AbCdEf1234567890%2Bxyz failed",
        ("AbCdEf1234567890",),
    ),
    "aws_signature": (
        "GET https://s3.amazonaws.com/b/k?X-Amz-Credential=AKIAEXAMPLE"
        "&X-Amz-Signature=abcdef0123456789abcdef0123 failed",
        ("abcdef0123456789abcdef", "AKIAEXAMPLE"),
    ),
    "query_token": ("GET https://h/p?token=TOKSECRET123456 failed", ("TOKSECRET123456",)),
    "query_access_token": (
        "GET https://h/p?x=1&access_token=ACCSECRET123456 failed",
        ("ACCSECRET123456",),
    ),
    "query_key": ("GET https://h/p?key=KEYSECRET123456 failed", ("KEYSECRET123456",)),
    "query_unnamed_high_entropy": (
        "GET https://h/p?q=1&zz=Q9vL2mX7pR4tK8wB3nF6 failed",
        ("Q9vL2mX7pR4tK8wB3nF6",),
    ),
    "dsn_password_with_slash": (
        "could not connect postgresql://u:pa/ss@host:5432/db",
        ("pa/ss", "u:pa"),
    ),
    "dsn_password_with_at": (
        "could not connect postgresql://u:p@ss/w0rd@host:5432/db",
        ("p@ss", "w0rd"),
    ),
    "pass_key": ("login pass=PASSSECRET1 rejected", ("PASSSECRET1",)),
    "pwd_key": ("login pwd=PWDSECRET1 rejected", ("PWDSECRET1",)),
    "passwd_colon": ("passwd: PASSWDSECRET1 rejected", ("PASSWDSECRET1",)),
    "secret_key": ("config secret=SECSECRET1 rejected", ("SECSECRET1",)),
    "api_key_key": ("config api_key=APISECRET1 rejected", ("APISECRET1",)),
    "apikey_quoted_json": ('{"apikey": "APIKEYSECRET1"}', ("APIKEYSECRET1",)),
    "access_key_key": ("aws access_key='ACCESSKEY1' rejected", ("ACCESSKEY1",)),
    "sqlalchemy_parameters": (
        "(psycopg.errors.UniqueViolation) duplicate key "
        "[SQL: INSERT INTO users (email, token) VALUES (%(email)s, %(token)s)] "
        "[parameters: {'email': 'a@b.example', 'token': 'PARAMSECRET1'}] "
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)",
        ("PARAMSECRET1", "a@b.example"),
    ),
    "sqlalchemy_parameters_tuple": (
        "boom [SQL: INSERT INTO t VALUES (%s, %s)] [parameters: ('PARAMSECRET2', 2)]",
        ("PARAMSECRET2",),
    ),
    "home_users": ("open /Users/you/secret/file.txt failed", ("/Users/you", "leo/")),
    "home_linux": ("open /home/alice/x failed", ("/home/alice", "alice")),
    "db_server_and_user": (
        'connection to server at "10.1.2.3", port 5432 failed: FATAL:  '
        'password authentication failed for user "dbadmin"',
        ("10.1.2.3", "dbadmin"),
    ),
    "bare_ipv4": ("connect to 192.0.2.15:6379 refused", ("192.0.2.15",)),
}


@pytest.mark.parametrize(
    ("raw", "forbidden"),
    list(_LEAK_CASES.values()),
    ids=list(_LEAK_CASES),
)
def test_scrub_public_message_removes_each_leak_shape(raw, forbidden):
    cleaned = scrub_public_message(raw)
    for fragment in forbidden:
        assert fragment not in cleaned, cleaned
    assert len(cleaned) <= PUBLIC_MESSAGE_LIMIT


def test_scrub_public_message_replaces_the_real_home_dir():
    home = str(Path.home())
    cleaned = scrub_public_message(f"cannot read {home}/projects/x.txt")
    assert home not in cleaned
    assert "~/projects/x.txt" in cleaned


def test_scrub_public_message_keeps_useful_words():
    cleaned = scrub_public_message(
        'connection to server at "10.1.2.3", port 5432 failed: timeout expired'
    )
    assert "connection to server at" in cleaned
    assert "port 5432" in cleaned
    assert "timeout expired" in cleaned
    assert "<ip>" in cleaned or "<host>" in cleaned


def test_scrub_public_message_does_not_stack_placeholders():
    cleaned = scrub_public_message("secret=SECSECRET1 api_key=APISECRET1 x")
    assert cleaned == "secret=[redacted] api_key=[redacted] x"


def test_scrub_public_message_still_caps_after_scrubbing():
    cleaned = scrub_public_message("token=" + "a" * 50 + " " + "m" * 2000)
    assert len(cleaned) == PUBLIC_MESSAGE_LIMIT
    assert "a" * 50 not in cleaned


# --- HTTP 500 through the handler the app really registers ----------------

_EXCEPTION_TEXT = (
    "kaboom github_pat_" "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz "
    "postgresql://u:pa/ss@10.0.0.5:5432/db /Users/you/private"
)


def _app_with_the_real_handler() -> FastAPI:
    from src.server.app import setup

    app = FastAPI()
    for exc_class, handler in setup.app.exception_handlers.items():
        app.add_exception_handler(exc_class, handler)
    app.add_middleware(setup.RequestIDMiddleware)

    @app.get("/boom")
    def boom():
        raise RuntimeError(_EXCEPTION_TEXT)

    @app.get("/plain")
    def plain():
        raise ValueError("plain words that are not secret")

    return app


def test_real_500_handler_returns_no_exception_text(caplog):
    with caplog.at_level(logging.ERROR):
        with TestClient(_app_with_the_real_handler(), raise_server_exceptions=False) as client:
            response = client.get("/boom")
            plain = client.get("/plain")
    assert response.status_code == 500
    body = response.json()
    assert body == {
        "detail": "Internal server error",
        "correlation_id": body["correlation_id"],
    }
    assert response.headers["x-correlation-id"] == body["correlation_id"]
    for fragment in ("kaboom", "github_pat", "pa/ss", "10.0.0.5", "/Users/you", "RuntimeError"):
        assert fragment not in response.text
    # Even text with no secret shape stays out of the body.
    assert plain.status_code == 500
    assert plain.json()["detail"] == "Internal server error"
    assert "plain words" not in plain.text
    assert "ValueError" not in plain.text
    # The detail is in the log under the same id.
    logged = [
        record
        for record in caplog.records
        if f"correlation_id={body['correlation_id']}" in record.getMessage()
    ]
    assert logged and logged[0].exc_info is not None
    assert "kaboom" in str(logged[0].exc_info[1])


# --- Daemon failure summary -------------------------------------------------


def test_daemon_failure_detail_has_type_scrubbed_text_and_correlation_id(caplog):
    from scripts import conductor_local_daemon as daemon

    exc = RuntimeError(
        "git push failed glpat-" "AbCdEf1234567890xyzQ at /home/alice/repo "
        "via https://h/p?sig=AbCdEf1234567890xyzw [parameters: ('PARAMSECRET3',)]"
    )
    with caplog.at_level(logging.ERROR, logger="scripts.conductor_local_daemon"):
        detail = daemon._failure_detail(exc, where="test failure")
    assert detail.startswith("RuntimeError: git push failed ")
    assert "(correlation_id=" in detail
    for fragment in ("glpat-", "/home/alice", "AbCdEf1234567890xyzw", "PARAMSECRET3"):
        assert fragment not in detail
    correlation_id = detail.rsplit("(correlation_id=", 1)[1].rstrip(")")
    assert any(
        f"correlation_id={correlation_id}" in record.getMessage() for record in caplog.records
    )


def test_daemon_failure_detail_names_the_type_for_an_empty_message():
    from scripts import conductor_local_daemon as daemon

    detail = daemon._failure_detail(TimeoutError(), where="test failure")
    assert detail.startswith("TimeoutError: no message (correlation_id=")
