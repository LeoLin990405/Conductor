"""The three shapes redact_credentials used to leave in a diagnostic.

A review probe found each one still readable after redaction: a PEM
private-key block, a URL that carries ``user:password@``, and a credential
whose key and value a unified diff split across two ``+`` lines. The marker
is ``[redacted]``. The key, the scheme and the host stay, so the text still
says what was removed. The same input redacts once and then stays put.
"""

from __future__ import annotations

import time

import pytest

from src.llms.native_cli.failure import redact_credentials

_PEM_BODY = "MIIEogIBAAKCAQEAnotarealsecretvalue1234567890"
_URL_USER = "dbuser9k"
_URL_PASSWORD = "s3cret-pass"
_DIFF_SECRET = "super-secret-value"


def _pem(label: str, body: str = _PEM_BODY) -> str:
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


@pytest.mark.parametrize(
    "label",
    (
        "PRIVATE KEY",
        "RSA PRIVATE KEY",
        "EC PRIVATE KEY",
        "OPENSSH PRIVATE KEY",
        "ENCRYPTED PRIVATE KEY",
    ),
)
def test_a_pem_private_key_block_is_redacted(label: str) -> None:
    """The label and the body are one secret.

    Leaving either one leaves the key. The words around the block are the
    diagnostic, and they stay.
    """
    text = f"before\n{_pem(label)}after\n"

    redacted = redact_credentials(text)

    assert redacted == "before\n[redacted]\nafter\n"
    assert _PEM_BODY not in redacted
    assert "PRIVATE KEY" not in redacted


def test_a_pem_block_on_one_line_and_a_crlf_block_are_redacted() -> None:
    """A block is a block however its lines are separated."""
    inline = (
        "-----BEGIN " "PRIVATE KEY----- "
        f"{_PEM_BODY} "
        "-----END PRIVATE KEY-----"
    )
    crlf = (
        "-----BEGIN " "RSA PRIVATE KEY-----\r\n"
        f"{_PEM_BODY}\r\n"
        "-----END RSA PRIVATE KEY-----\r\n"
    )

    assert redact_credentials(inline) == "[redacted]"
    assert redact_credentials(crlf) == "[redacted]\r\n"
    assert _PEM_BODY not in redact_credentials(inline)
    assert _PEM_BODY not in redact_credentials(crlf)


def test_a_diff_prefixed_pem_block_is_redacted() -> None:
    """An added diff puts ``+`` on every line of the block.

    The prefixes sit between the labels. The body is still the key.
    """
    text = (
        "+-----BEGIN " "OPENSSH PRIVATE KEY-----\n"
        f"+{_PEM_BODY}\n"
        "+-----END OPENSSH PRIVATE KEY-----\n"
    )

    redacted = redact_credentials(text)

    assert redacted == "+[redacted]\n"
    assert _PEM_BODY not in redacted
    assert "PRIVATE KEY" not in redacted


def test_an_unterminated_pem_block_redacts_the_rest_of_the_text() -> None:
    """A BEGIN with no END means the body runs off the end of the text."""
    text = f"see -----BEGIN " f"PRIVATE KEY-----\n{_PEM_BODY} and more"

    redacted = redact_credentials(text)

    assert redacted == "see [redacted]"
    assert _PEM_BODY not in redacted


def test_two_pem_blocks_leave_the_text_between_them() -> None:
    text = f"{_pem('PRIVATE KEY')}kept\n{_pem('EC PRIVATE KEY')}"

    assert redact_credentials(text) == "[redacted]\nkept\n[redacted]\n"


@pytest.mark.parametrize(
    "block",
    (
        "-----BEGIN CERTIFICATE-----\nMIIBnotaprivatekey\n-----END CERTIFICATE-----\n",
        "-----BEGIN PUBLIC KEY-----\nMIIBpublickeymaterial\n-----END PUBLIC KEY-----\n",
    ),
)
def test_a_certificate_and_a_public_key_stay(block: str) -> None:
    """Those labels are not a private key, so the text is left as it was."""
    assert redact_credentials(f"before\n{block}after") == f"before\n{block}after"


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            f"postgres://{_URL_USER}:{_URL_PASSWORD}@db.internal:5432/app",
            "postgres://[redacted]@db.internal:5432/app",
        ),
        (
            f"postgres://{_URL_USER}:password@localhost/app",
            "postgres://[redacted]@localhost/app",
        ),
        (
            f"postgresql+psycopg://{_URL_USER}:{_URL_PASSWORD}@db.internal/app",
            "postgresql+psycopg://[redacted]@db.internal/app",
        ),
        (
            f"mongodb+srv://{_URL_USER}:{_URL_PASSWORD}@cluster.example/db",
            "mongodb+srv://[redacted]@cluster.example/db",
        ),
        (
            f"https://{_URL_USER}:{_URL_PASSWORD}@example.com/v1?x=1",
            "https://[redacted]@example.com/v1?x=1",
        ),
        (
            f"redis://:{_URL_PASSWORD}@localhost:6379/0",
            "redis://[redacted]@localhost:6379/0",
        ),
        (
            f"postgres://{_URL_USER}:p%40ss@db.internal/app",
            "postgres://[redacted]@db.internal/app",
        ),
        (
            f"failed (postgres://{_URL_USER}:{_URL_PASSWORD}@db.internal/app).",
            "failed (postgres://[redacted]@db.internal/app).",
        ),
        (
            f"https://{_URL_USER}:{_URL_PASSWORD}@example.com,next",
            "https://[redacted]@example.com,next",
        ),
        (
            f"postgres://{_URL_USER}:{_URL_PASSWORD}@[::1]/db",
            "postgres://[redacted]@[::1]/db",
        ),
        # The user name is itself a credential word. The host still has to
        # survive: the password rule must not read the rest of the URL as
        # the value of that word.
        (
            f"postgres://password:{_URL_PASSWORD}@db.internal/app",
            "postgres://[redacted]@db.internal/app",
        ),
    ),
)
def test_a_url_userinfo_is_redacted_and_the_host_stays(text: str, expected: str) -> None:
    """``user:password@`` goes; the scheme and the host stay.

    The user name goes with the password: in a URL the user name can be
    the token. A second pass finds nothing left to take.
    """
    redacted = redact_credentials(text)

    assert redacted == expected
    if _URL_USER in text:
        assert _URL_USER not in redacted
    assert _URL_PASSWORD not in redacted
    assert redact_credentials(redacted) == redacted


@pytest.mark.parametrize(
    "text",
    (
        f"postgres://{_URL_USER}@db.internal/app",
        "https://example.com/health",
        "https://example.com:8080/not@a-url",
        "http://127.0.0.1:8080/v1",
        f"https://{_URL_USER}@github.com/org/repo.git",
        "reach ada@example.com for the host",
    ),
)
def test_a_url_without_a_password_stays(text: str) -> None:
    """A user name with no password, and an ordinary URL, are not userinfo."""
    assert redact_credentials(text) == text


@pytest.mark.parametrize(
    ("text", "secret"),
    (
        ("https://alice:pa,ss@example.com/path", "pa,ss"),
        ("https://alice:pa(ss)@example.com/path", "pa(ss)"),
        ("https://alice:o'clock@example.com/path", "o'clock"),
        ("https://alice:123,secret@example.com/path", "123,secret"),
    ),
)
def test_userinfo_punctuation_in_a_password_is_redacted(text: str, secret: str) -> None:
    """A comma, parenthesis or apostrophe can sit inside the password.

    Ending the authority on one of them leaves the password in place.
    The host after the ``@`` still stops at the next one, so a later
    email is not swallowed with the host.
    """
    redacted = redact_credentials(text)

    assert redacted == "https://[redacted]@example.com/path"
    assert secret not in redacted
    assert "alice" not in redacted
    assert redact_credentials(redacted) == redacted


def test_a_comma_after_the_host_is_not_userinfo() -> None:
    """The comma that follows the host is the end of the authority.

    The email glued on after that comma stays. A port with no password
    ``@`` stays too. A slash still ends the authority before a later ``@``.
    """
    glued = "https://alice:pa,ss@example.com,email@b.com"
    port = "https://example.com:8080,next"
    slashed = "https://example.com:8080/not@a-url"

    assert redact_credentials(glued) == "https://[redacted]@example.com,email@b.com"
    assert "pa,ss" not in redact_credentials(glued)
    assert redact_credentials(port) == port
    assert redact_credentials(slashed) == slashed


@pytest.mark.parametrize(
    ("text", "secret"),
    (
        ("https://db,user:secret@host/path", "secret"),
        ("https://db(user):secret@host/path", "secret"),
        ("https://john.doe:123,secret@host/path", "123,secret"),
    ),
)
def test_punctuation_in_the_user_name_does_not_hide_the_password(
    text: str, secret: str
) -> None:
    """The user name is read through to the password's ``@``.

    A comma in the name, or a dot and a numeric password prefix, is not
    a host and a port. Stopping there leaves the password in place.
    """
    redacted = redact_credentials(text)

    assert redacted == "https://[redacted]@host/path"
    assert secret not in redacted
    assert "db,user" not in redacted
    assert "john.doe" not in redacted
    assert redact_credentials(redacted) == redacted


def test_a_password_longer_than_the_authority_cap_is_redacted() -> None:
    """A long password is followed through its ``@``.

    Stopping early and leaving the tail would return the secret unchanged.
    A long user name is the same: the colon can sit past where a cap used
    to give up, and the password after it still goes.
    """
    secret = "s" * 2050
    text = f"https://alice:{secret}@example.com/path"
    user = "a" * 2050
    named = f"https://{user}:s3cr3t@host/path"

    redacted = redact_credentials(text)
    named_redacted = redact_credentials(named)

    assert redacted == "https://[redacted]@example.com/path"
    assert secret not in redacted
    assert redact_credentials(redacted) == redacted
    assert named_redacted == "https://[redacted]@host/path"
    assert "s3cr3t" not in named_redacted
    assert user not in named_redacted
    assert redact_credentials(named_redacted) == named_redacted


def test_a_password_that_holds_an_at_sign_is_redacted_whole() -> None:
    """The userinfo ends at the last ``@`` of the authority.

    A password may contain ``@``. Stopping at the first one leaves the
    suffix of the password in front of the host.
    """
    text = "https://alice:pa@ss@example.org/path"

    redacted = redact_credentials(text)

    assert redacted == "https://[redacted]@example.org/path"
    assert "@ss@" not in redacted
    assert "pa@ss" not in redacted
    assert "alice" not in redacted
    assert redact_credentials(redacted) == redacted


def test_a_credential_free_url_stops_before_json_punctuation() -> None:
    """A URL ends at the quote, not at an ``@`` later in the same run.

    Minified JSON keeps the URL and an email in one stretch with no
    whitespace. The email's ``@`` is not userinfo, and the host stays.
    """
    text = '{"url":"https://example.com","email":"a@b.com"}'

    redacted = redact_credentials(text)

    assert redacted == text
    assert redact_credentials(redacted) == redacted


def test_a_url_in_minified_json_keeps_the_host() -> None:
    """The same punctuation still ends a real userinfo at the host."""
    text = '{"url":"https://alice:secret@example.com/x","email":"a@b.com"}'

    redacted = redact_credentials(text)

    assert redacted == (
        '{"url":"https://[redacted]@example.com/x","email":"a@b.com"}'
    )
    assert "alice" not in redacted
    assert "secret" not in redacted
    assert redact_credentials(redacted) == redacted


def test_a_password_url_glued_to_a_plain_url_is_still_redacted() -> None:
    """The slash of a later scheme is what ends the earlier authority.

    Moving past that whole authority would move past the later scheme,
    and its password would stay in front of the host.
    """
    text = "https://example.comhttps://alice:pa@ss@host/path"

    redacted = redact_credentials(text)

    assert redacted == "https://example.comhttps://[redacted]@host/path"
    assert "pa@ss" not in redacted
    assert "alice" not in redacted
    assert redact_credentials(redacted) == redacted


def test_a_long_scheme_run_is_scanned_once() -> None:
    """A whitespace-free run of schemes is not walked once per scheme.

    ``x://`` repeated is the run a minified or hostile diagnostic sends.
    Each rejected scheme used to leave the scan where it was, so the next
    one walked the rest of the run again. Unfixed, this call took long
    enough to matter; a second is the bound that still fails then.
    """
    text = "x://" * 50_000

    started = time.perf_counter()
    redacted = redact_credentials(text)
    elapsed = time.perf_counter() - started

    assert redacted == text
    assert elapsed < 1.0


def test_a_url_inside_a_mapping_string_is_redacted() -> None:
    """A string under a key that names no credential is still walked."""
    dsn = f"postgres://{_URL_USER}:{_URL_PASSWORD}@db.internal/app"

    assert redact_credentials({"dsn": dsn}) == {
        "dsn": "postgres://[redacted]@db.internal/app"
    }


def test_a_url_in_bytes_is_redacted() -> None:
    """Bytes are decoded, redacted and encoded, so they are not a way around."""
    raw = f"postgres://{_URL_USER}:{_URL_PASSWORD}@db/app".encode()

    redacted = redact_credentials(raw)
    as_bytes = redact_credentials(bytearray(raw))

    assert redacted == b"postgres://[redacted]@db/app"
    assert isinstance(as_bytes, bytearray)
    assert bytes(as_bytes) == redacted
    assert _URL_PASSWORD.encode() not in redacted


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            f"+api_key:\n+  \"{_DIFF_SECRET}\"\n",
            "+api_key:\n+  [redacted]\n",
        ),
        (
            f"+password=\n+{_DIFF_SECRET}\n",
            "+password=\n+[redacted]\n",
        ),
        (
            f'+\"api_key\":\n+\"{_DIFF_SECRET}\"\n',
            '+\"api_key\":\n+[redacted]\n',
        ),
        (
            f"+token:\n+  {_DIFF_SECRET}\n",
            "+token:\n+  [redacted]\n",
        ),
        (
            f"error api_key:\n+  \"{_DIFF_SECRET}\"\n",
            "error api_key:\n+  [redacted]\n",
        ),
        (
            f"+private key:\n+  \"{_DIFF_SECRET}\"\n",
            "+private key:\n+  [redacted]\n",
        ),
        (
            f"+api key=\n+{_DIFF_SECRET}\n",
            "+api key=\n+[redacted]\n",
        ),
        (
            f"+Authorization:\n+  \"{_DIFF_SECRET}\"\n",
            "+Authorization:\n+  [redacted]\n",
        ),
        (
            f"+Authorization:\n+  Bearer {_DIFF_SECRET}\n",
            "+Authorization:\n+  Bearer [redacted]\n",
        ),
        (
            f"+uid:\n+  {_DIFF_SECRET}\n",
            "+uid:\n+  [redacted]\n",
        ),
        (
            f'+\"api_key\"\n+: \"{_DIFF_SECRET}\"\n',
            '+\"api_key\"\n+: [redacted]\n',
        ),
        (
            f"+api_key\n+: {_DIFF_SECRET}\n",
            "+api_key\n+: [redacted]\n",
        ),
        (
            f'+\"private key\"\n+: \"{_DIFF_SECRET}\"\n',
            '+\"private key\"\n+: [redacted]\n',
        ),
        (
            "+PASSWORD=\n+p,a;ss&word\n",
            "+PASSWORD=\n+[redacted]\n",
        ),
        (
            "+password=\n+p,a;ss&word\n",
            "+password=\n+[redacted]\n",
        ),
        (
            "+api key:\n+  two words secret\n",
            "+api key:\n+  [redacted]\n",
        ),
        (
            "+private key\n+: two words secret\n",
            "+private key\n+: [redacted]\n",
        ),
    ),
)
def test_a_value_on_the_next_added_diff_line_is_redacted(
    text: str, expected: str
) -> None:
    """The ``+`` of the next added line is the marker, not the value.

    Reading it as the value stops there and leaves the secret on the rest
    of the line. The key and the marker stay, so the diff still shows
    which line changed.
    """
    redacted = redact_credentials(text)

    assert redacted == expected
    assert _DIFF_SECRET not in redacted
    assert "ss&word" not in redacted
    assert ",a;ss&word" not in redacted
    assert "words secret" not in redacted
    assert redact_credentials(redacted) == redacted


def test_a_diff_hunk_keeps_its_context_and_loses_the_secret() -> None:
    text = (
        "diff --git a/settings.env b/settings.env\n"
        "--- a/settings.env\n"
        "+++ b/settings.env\n"
        "@@ -1,2 +1,4 @@\n"
        " host=db.internal\n"
        "+api_key:\n"
        f"+  \"{_DIFF_SECRET}\"\n"
        f"+DATABASE_URL=postgres://{_URL_USER}:{_URL_PASSWORD}@db.internal:5432/app\n"
    )

    redacted = redact_credentials(text)

    assert "host=db.internal" in redacted
    assert "@@ -1,2 +1,4 @@" in redacted
    assert "+api_key:\n+  [redacted]\n" in redacted
    assert (
        "+DATABASE_URL=postgres://[redacted]@db.internal:5432/app\n" in redacted
    )
    assert _DIFF_SECRET not in redacted
    assert _URL_PASSWORD not in redacted
    assert _URL_USER not in redacted


def test_a_split_line_keeps_the_tail_after_a_provider_prefix() -> None:
    """A provider prefix is not the whole secret on the next added line.

    The shape rule takes ``sk-""" """abcdefgh`` out on its own and leaves
    ``/tail-secret``. The line is still the value, so the tail goes with
    the prefix. A spaced key is the same line.
    """
    text = "+password=\n+sk-" "abcdefgh/tail-secret\n"
    spaced = "+api key:\n+sk-" "abcdefgh/tail-secret\n"

    redacted = redact_credentials(text)
    spaced_redacted = redact_credentials(spaced)

    assert redacted == "+password=\n+[redacted]\n"
    assert "tail-secret" not in redacted
    assert "abcdefgh" not in redacted
    assert redact_credentials(redacted) == redacted
    assert spaced_redacted == "+api key:\n+[redacted]\n"
    assert "tail-secret" not in spaced_redacted
    assert redact_credentials(spaced_redacted) == spaced_redacted


def test_a_count_split_across_diff_lines_stays() -> None:
    """A number under a count key is a count on the next line too."""
    text = "+max_tokens:\n+  131072\n"

    assert redact_credentials(text) == text


def test_a_pair_that_names_no_credential_stays_across_diff_lines() -> None:
    text = "+status:\n+  200\n"

    assert redact_credentials(text) == text


def test_a_same_line_value_that_starts_with_plus_is_still_the_value() -> None:
    """The diff rule is only for a ``+`` that begins the next line.

    A ``+`` on the same line is the first character of the value, and the
    whole value goes, the way it did before the diff rule existed.
    """
    assert redact_credentials("api_key=+plus-secret") == "api_key=[redacted]"
    assert redact_credentials("api key=+plus-secret") == "api key=[redacted]"
    assert (
        redact_credentials("api key: two words secret")
        == "api key: [redacted] words secret"
    )
    assert (
        redact_credentials("-api_key: super-secret-value")
        == "-api_key: [redacted]"
    )
    assert "plus-secret" not in redact_credentials("api_key=+plus-secret")


def test_the_three_gaps_redact_together_and_a_second_pass_changes_nothing() -> None:
    text = (
        f"{_pem('RSA PRIVATE KEY')}"
        f"dsn=postgres://{_URL_USER}:{_URL_PASSWORD}@db.internal:5432/app\n"
        "+api_key:\n"
        f"+  \"{_DIFF_SECRET}\"\n"
    )

    once = redact_credentials(text)
    twice = redact_credentials(once)

    assert once == twice
    assert once == (
        "[redacted]\n"
        "dsn=postgres://[redacted]@db.internal:5432/app\n"
        "+api_key:\n"
        "+  [redacted]\n"
    )
    for secret in (_PEM_BODY, _URL_USER, _URL_PASSWORD, _DIFF_SECRET, "PRIVATE KEY"):
        assert secret not in once
