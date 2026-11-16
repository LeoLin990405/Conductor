"""The guard's URL userinfo rule: ``scheme://user:password@host``.

The shared sanitizer only reacts to such a URL when the host happens to look
like an e-mail domain, and then only after the password is out, so the guard
carries its own rule. These tests pin the contract: a password (or a token used
as a user name) is never stored, not even its first characters when a message
ends inside the URL, and ordinary URLs keep flowing.

Fixtures are obviously fake credentials.
"""

from __future__ import annotations

import functools
import itertools
import random
import re
import string
import time
import unicodedata
from urllib.parse import urlsplit

import pytest

from src.server.utils import output_guard as guard
from src.server.utils.output_guard import GuardState, scan

HEX40 = "0123456789abcdef0123456789abcdef01234567"
TOKEN32 = "AB12" * 8


def _chain(events: list[tuple[str, str, bool]]):
    """Scan ``(text, channel, join)`` events; returns per-event results and the
    text as the model wrote it (the synthetic newline of a join event is not
    part of it) over the events that were stored."""
    state = GuardState()
    results = []
    written = ""
    for text, channel, join in events:
        result = scan(state, text + ("\n" if join else ""), channel, join=join)
        state = result.state
        results.append(result)
        if result.ok:
            written += result.text[:-1] if join else result.text
    return results, state, written


def _messages(*messages: str):
    """Model messages the way the daemon sends them: one that ends mid-word is
    marked ``join`` and gets a synthetic newline."""
    state = GuardState()
    results = []
    for message in messages:
        join = not message[-1].isspace()
        result = scan(state, message + ("\n" if join else ""), join=join)
        state = result.state
        results.append(result)
    return results, state


# Refused by the URL rule itself (the sanitizer and the earlier rules miss them
# or refuse them only once the password is out).
URL_CREDENTIALS = [
    "postgres://admin:hunter2pw@db/app",
    "postgres://admin:hunter2pw@db.example.com/app",
    "postgres://admin:hunter2pw@localhost:5432/app",
    "postgres://admin:hunter2pw@10.0.0.5:5432/app",
    "https://user:pw@host/path",
    "https://:pw@host/path",  # empty user name
    "redis://:s3cr3t@cache:6379/0",
    "amqp://guest:guest@rabbit:5672//",
    "mysql://root:p%40ss@[::1]:3306/db",
    "https://user:p@ss@host/x",  # an unescaped @ in the password: the last one splits
    "https://user:pw@",  # nothing after the @
    "https://user::@host",
    "jdbc:postgresql://u:pw@db/x",
    "https://user:pw@host?x=1",
    "https://user:pw@host#frag",
    "https:\\\\user:pw@host/p",  # backward slashes are read as forward ones
    "https:///user:pw@host/p",
    # A tab or line break is dropped from a URL by the parsers that read one, so
    # it may sit in the anchor as well.
    "postgres:\n//admin:hunter2pw@db/app",
    "postgres:/\t/admin:hunter2pw@db/app",
    "postgres:\t//admin:hunter2pw@db/app",
    "postgres:/\n/admin:hunter2pw@db/app",
    "postgres:\r\n//admin:hunter2pw@db/app",
    "http://a://user:pw@host",  # a second URL inside the first one's authority
    "database-connection-string-for-the-main-database-postgres://u:pw@db",
    "x://a:b@c",
    "https://\uff55\uff53\uff45\uff52\uff1apw\uff20host/p",  # full-width look-alikes
    f"https://{HEX40}@host/o/r",  # a 40-hex token used as the user name
    f"https://{TOKEN32}@host/o/r",
    # ``curl -u TOKEN:`` style: the token is the user name whatever follows it
    f"https://{TOKEN32}:@api.internal/v1",
    f"https://{HEX40}:@host/o",
    "https://sk_" "test_4eC39HqLyjWDarjtT1zdp7dcXyz:@host/o",
    f"https://{TOKEN32}:***@host/o",
    f"https://{TOKEN32}:[REDACTED]@host/o",
    f"https://{TOKEN32}@x@host/o",  # a token before the first @ of two
    # A tab, line break or Unicode space is no boundary of a user name.
    f"https://{TOKEN32}\n@host/o/r",
    f"https://{TOKEN32}\t@host/o/r",
    f"https://{TOKEN32}\u00a0@host/o/r",
    f"https://{TOKEN32[:10]}\n{TOKEN32[10:]}@host/o/r",
    f"https://\n{TOKEN32}@host/o/r",
    f"https://{TOKEN32[:7]}\u3000{TOKEN32[7:]}:@host/o/r",
    # A backslash is a userinfo character for every scheme but the web ones.
    "postgres://admin:pa\\ss99@db/app",
    "mysql://admin:pa\\ss99@db:3306/app",
    "redis://:pa\\ss99@cache:6379/0",
    "amqp://admin:pa\\ss99@mq",
    "postgres://us\\er:hunter2pw@db/app",  # and in the user name
    "file://C://user:pw@host",  # the colon of a drive letter opens the next URL
    # SQLAlchemy and most DSN parsers take everything up to the @ as the
    # password, including a raw / ? # (what base64 passwords carry).
    "postgresql://app:Zk3/9vQ+x==@db:5432/app",
    "postgresql://app:Zk3?9vQ#x@db:5432/app",
    "postgresql://app:Zk39vQ#x@db:5432/app",
    "postgresql://app:/Zk39vQx@db:5432/app",
    "postgresql://app:#Zk39vQx@db:5432/app",
    "postgresql://app:Zk3/9vQ@x@db:5432/app",
    "postgresql://app:Zk\n3/9vQ+x==@db:5432/app",  # a line break, then a raw slash
    # A raw delimiter, then a tab, line break or Unicode space: the password still
    # runs to the @ (only a plain blank ends the authority).
    "postgresql://app:Zk3/9vQ+x\n==@db:5432/app",
    "postgresql://app:Zk3?9vQ+x\t==@db:5432/app",
    "postgresql://app:Zk3#9vQ+x\u00a0==@db:5432/app",
    "postgresql://app:Zk3/9vQ+x\u2028==@db:5432/app",
    "postgresql://app:Zk3/9vQ+x\u3000==@db:5432/app",
    # The parsers that drop a tab or line break from a URL read one URL.
    "postgres://admin:hunter2pw\n@db.example.com/app",
    "postgres://admin:hunter\n2pw@db/app",
    "postgres://admin:hunter2pw\t@db/app",
    "postgres://admin:\nhunter2pw@db/app",
    # Unicode spaces are userinfo characters, not separators, to a URL parser.
    "postgres://admin:hunter2pw\u00a0@db/app",
    "postgres://admin:hunter2pw\u3000@db/app",
    "postgres://admin:hunter2pw\u2003@db/app",
    "postgres://admin:hunter\u202f2pw@db/app",
    "postgres://admin:hunter2pw\u2028@db/app",
    # NFKC turns these full-width delimiters into real ones; parsers keep them.
    "postgres://admin:hunter\uff0f2pw@db/app",
    "postgres://admin:hunter\uff1f2pw@db/app",
    "postgres://admin:hunter\uff032pw@db/app",
    "postgres://admin:hunter\uff3c2pw@db/app",
]
# Refused too, but by the rules that already existed.
EARLIER_RULES = [
    "https://ghp_" + "x" * 20 + "@host/o/r",
    "https://sk-" + "x" * 20 + "@host",
    "https://x-access-token:abc@github.com/o/r",
]


@pytest.mark.parametrize("url", URL_CREDENTIALS)
def test_url_userinfo_credentials_are_refused(url):
    result = scan(GuardState(), f"see {url} tail\n")
    assert not result.ok
    assert result.reason == guard.REASON_URL_CREDENTIAL
    assert result.state.tainted
    assert result.text == ""
    # The refusal leaves no trace of the secret in the persisted state.
    assert "hunter2" not in repr(result.state) and "pw@" not in repr(result.state)


@pytest.mark.parametrize("url", EARLIER_RULES)
def test_urls_an_earlier_rule_already_covers_stay_refused(url):
    assert not scan(GuardState(), f"see {url} tail\n").ok


# Clean for the URL rule. `git@github.com` and `user@example.com` are NOT here:
# the sanitizer's e-mail rule refuses them (`redaction_candidate`) with or
# without this rule, so they are not asserted clean through `scan()`.
CLEAN_URLS = [
    "https://example.com:8080/path",
    "http://localhost:3000",
    "http://localhost:3000/",
    "http://10.0.0.5:8080",
    "http://[::1]:3000",
    "https://[::1]:8080/x",
    "ssh://git@host/org/repo.git",
    "user@host",
    "http://example.com/a@b",
    "https://registry.npmjs.org/@types/node",
    "https://medium.com/@someone",
    "http://example.com/?mail=a@b.c",
    "http://example.com#a:b@c",
    "https://user:@host",  # an empty password
    "https://user@host",
    "https://deploy-bot@host/path",
    "https://ec2-user@10.0.0.5/x",
    "https://john.doe@host/x",
    "https://john%40example.com@smtp/x",
    "https://service-account-readonly@host/x",  # 23 letters: below the token length
    "https://a1b2c3d4e5f6@host/x",
    "file:///home/user/@scope",
    "https://example.com:8080/path.",
    "https://123456789012345678901234567890@host/x",  # a numeric id, not a token
    "http://localhost:3000/@vite/client",  # a path that starts with @
    "http://localhost:5173/@fs/home/me/app/main.ts",
    "http://[::1]:3000/@vite/client",
    "https://example.com:8443/a/b@c",
    "http://host:abc/x y@z",  # a plain blank ends the run that may hold the @
    "C:/Users/me:x@y",  # one slash is no anchor
    "file:///C:/Users/me@host/x",
    "key:/a:b@c",  # one slash is no anchor either
    "key:\n/a:b@c",
    "note:\n// a comment with user@host",  # a blank right after the slashes
    # A mask carries nothing: what pip, npm and SQLAlchemy print in its place.
    "https://user:***@host/x",
    "https://user:****@pypi.org/simple",
    "https://user:[REDACTED]@db/x",
]


@pytest.mark.parametrize("url", CLEAN_URLS)
def test_urls_without_a_credential_stay_clean(url):
    assert guard._url_credential(f"see {url} tail\n") is False
    assert guard._url_credential(f"see {url}\n") is False


@pytest.mark.parametrize(
    "url",
    [url for url in CLEAN_URLS if "git@" not in url and "@example" not in url],
)
def test_clean_urls_flow_through_the_whole_guard(url):
    result = scan(GuardState(), f"see {url} tail\n")
    assert result.ok, result.reason


def _chains(prefix: str, url: str):
    """Every way to cut ``url`` into 2 and 3 messages, and into 2 with a
    reasoning event between them (hidden in the view without reasoning)."""
    cuts = range(1, len(url))
    for i in cuts:
        yield [
            (prefix + url[:i], "text", True),
            (url[i:] + " done \n", "text", False),
        ]
        yield [
            (prefix + url[:i], "text", True),
            ("thinking about it ", "reasoning", False),
            (url[i:] + " done \n", "text", False),
        ]
    for i, j in itertools.combinations(cuts, 2):
        yield [
            (prefix + url[:i], "text", True),
            (url[i:j], "text", True),
            (url[j:] + " done \n", "text", False),
        ]


# (url, what precedes the secret, the secret). A password may leak a few leading
# digits and a bare token its first 23 characters before it is recognizable;
# everything else is withheld at its first character.
SPLIT_CASES = [
    ("postgres://admin:hunter2pw@db/app", ":", "hunter2pw"),
    ("postgres://admin:hunter2pw@localhost:5432/app", ":", "hunter2pw"),
    ("redis://:s3cr3t@cache:6379/0", ":", "s3cr3t"),
    ("https://user:pw@host/path", ":", "pw"),
    ("mysql://root:p%40ss@[::1]:3306/db", ":", "p%40ss"),
    ("https://user:p@ss@host/x", ":", "p@ss"),
    (f"https://{HEX40}@host/o/r", "://", HEX40),
    (f"https://{TOKEN32}@host/o/r", "://", TOKEN32),
    ("postgresql://app:Zk3/9vQ+x==@db:5432/app", ":", "Zk3/9vQ+x=="),
    ("postgres://admin:pa\\ss99@db/app", ":", "pa\\ss99"),
    (f"https://{TOKEN32}:@host/o/r", "://", TOKEN32),
    (f"https://{HEX40}:***@host/o/r", "://", HEX40),
]


@pytest.mark.parametrize(
    "url,anchor,secret", SPLIT_CASES, ids=[case[0][:30] for case in SPLIT_CASES]
)
def test_a_url_credential_split_at_every_offset_is_never_stored(url, anchor, secret):
    leaked_at = min(len(secret), 24 if anchor == "://" else 6)
    for events in _chains("see ", url):
        results, _, written = _chain(events)
        assert not all(r.ok for r in results), events
        assert anchor + secret[:leaked_at] not in written, events


def test_a_password_is_withheld_at_its_first_character_not_when_the_url_completes():
    results, _ = _messages("see postgres://admin:h", "unter2pw@db/app done \n")
    assert results[0].reason == guard.REASON_URL_CREDENTIAL
    assert not results[0].ok  # nothing of the password was stored


def test_a_token_user_name_is_withheld_before_the_colon_that_ends_the_message():
    """``https://TOKEN:`` is open-ended: the ``@`` can come in the next message."""
    results, state = _messages(f"see https://{TOKEN32}:", "@api.internal/v1 done \n")
    assert not results[0].ok
    assert results[0].reason == guard.REASON_URL_CREDENTIAL
    assert state.tainted


def test_a_user_name_alone_is_clean_until_a_password_follows():
    results, state = _messages("see https://user:", "pw@host done \n")
    assert [r.ok for r in results] == [True, False]
    assert results[1].reason == guard.REASON_URL_CREDENTIAL
    assert state.tainted


def test_open_authority_longer_than_a_run_taints():
    """``:`` and ``=`` are gap characters, so an authority built from them is not
    one run; the cap keeps ``scheme://`` inside the scan tail until the ``@``."""
    results, state = _messages("see https://k=", *["v=k=" * 100] * 4, "@host done \n")
    refused = next(i for i, r in enumerate(results) if not r.ok)
    assert results[refused].reason == guard.REASON_URL_CREDENTIAL
    assert refused < len(results) - 1  # before the @ arrives
    assert state.tainted


@pytest.mark.parametrize(
    "message,continuation",
    [
        ("Open http://localhost:3000", "/health ok \n"),
        ("Open http://localhost:3000.", " Next sentence \n"),
        ("(http://localhost:3000)", " ok \n"),
        ("Open `http://localhost:3000`", " ok \n"),
        ("Open http://[::1]:3000", "/ ok \n"),
        ("Listening on http://0.0.0.0:8080,", " ok \n"),
        ("Cache at redis://cache:6379", "/0 ok \n"),
        ("Open https://example.com", "/docs ok \n"),
        ("Clone ssh://git@host", "/org/repo.git ok \n"),
    ],
)
def test_host_port_at_the_end_of_a_joined_message_is_clean(message, continuation):
    results, state = _messages(message, continuation)
    assert [r.ok for r in results] == [True, True]
    assert not state.tainted


@pytest.mark.parametrize(
    "message",
    [
        "Open http://localhost:65535",
        "Open http://localhost:80.",
        "Open http://localhost:3000),",
        "Open **http://localhost:3000**",
        "Open http://localhost:3000!",
        "Open http://[::1]:8080.",
    ],
)
def test_a_port_with_closing_prose_at_the_end_of_a_message_is_clean(message):
    results, _ = _messages(message, "/x ok \n")
    assert results[0].ok, message


@pytest.mark.parametrize(
    "message",
    [
        "see postgres://admin:123456",  # six digits are not a port
        "see postgres://admin:12345$",  # a symbol a password uses, not prose
        "see postgres://admin:12345\u5bc6",  # nor is another script
        "see postgres://admin:7\u5bc6\u7801\u5f88\u957f",
        "see postgres://admin:2024-.-.-.-.-.-.",
        "see postgres://admin:3000.....",  # more than three closing characters
        "see redis://:1234567890",
        "see postgres://admin:hunter2pw",
        "see postgres://admin:5432a",
    ],
)
def test_a_password_that_is_not_a_port_is_withheld_at_the_end_of_a_message(message):
    results, state = _messages(message, "@db/app done \n")
    assert not results[0].ok, message
    assert results[0].reason == guard.REASON_URL_CREDENTIAL
    assert state.tainted


@pytest.mark.parametrize(
    "message",
    [
        "see http://[admin:hunter2pw",  # not an IPv6 literal: a user name
        "see http://[::1]:hunter2pw",
        "see postgres://[ab]:s3cretpw",
        "see postgres://[x]:hunter2",
        "see http://[fe80::1]:pw",
    ],
)
def test_a_bracket_does_not_exempt_userinfo_at_the_end_of_a_message(message):
    results, _ = _messages(message, "@db/app done \n")
    assert not results[0].ok, message
    assert results[0].reason == guard.REASON_URL_CREDENTIAL


@pytest.mark.parametrize(
    "message,continuation",
    [
        ("Open http://[", "::1]:3000 ok \n"),
        ("Open http://[fe80::1", "%25eth0]:3000 ok \n"),
        ("Open http://[2001:db8::7", "]:8080/x ok \n"),
        ("Open http://[::1]", ":3000 ok \n"),
    ],
)
def test_an_ipv6_host_still_being_written_is_clean(message, continuation):
    results, state = _messages(message, continuation)
    assert [r.ok for r in results] == [True, True]
    assert not state.tainted


@pytest.mark.parametrize(
    "message",
    [
        # Documented false positives: a message that ends on what could still
        # become a user name. Only the live text of that run ends.
        "Deployed to http://order-service-api-v2-replica",
        "Open http://my-app-preview-pr-1234-abcdef-staging",
        "Visit http://localhost:$PORT",
        "Open http://localhost:${PORT}",
        "Open http://localhost:123456",
    ],
)
def test_known_false_positives_end_the_live_text_only(message):
    results, state = _messages(message, " ok \n")
    assert results[0].reason == guard.REASON_URL_CREDENTIAL
    assert state.tainted


def test_a_continuation_that_turns_host_port_into_userinfo_is_refused():
    results, _ = _messages("see http://localhost:3000", "@evil.example ok \n")
    assert results[1].reason == guard.REASON_URL_CREDENTIAL


def test_the_url_rule_judges_the_text_as_the_model_wrote_it():
    """Displayed, the two messages are separated by a newline; written, the
    scheme and the user information are one URL."""
    results, _ = _messages("see https:", "//user:pw@host done \n")
    assert not results[1].ok


_USERINFO = string.ascii_letters + string.digits + "-._~%!$&'()*+,;="
_SCHEMES = [
    "http",
    "https",
    "postgres",
    "postgresql+psycopg",
    "redis",
    "amqp",
    "mysql",
    "ssh",
    "git+https",
    "s3",
    "x",
]
_HOSTS = [
    "localhost",
    "db",
    "cache",
    "host",
    "10.0.0.5",
    "[::1]",
    "example.com",
    "a.b.internal",
    "svc-1",
]


def _random_url(rng: random.Random) -> str:
    def pick(chars: str, low: int, high: int) -> str:
        return "".join(rng.choice(chars) for _ in range(rng.randint(low, high)))

    userinfo = ""
    shape = rng.random()
    if shape < 0.3:
        userinfo = pick(_USERINFO, 1, 12) + "@"
    elif shape < 0.6:
        userinfo = pick(_USERINFO, 0, 12) + ":" + pick(_USERINFO + ":@", 0, 12) + "@"
    host = rng.choice(_HOSTS) + (
        ":" + str(rng.randint(1, 65535)) if rng.random() < 0.5 else ""
    )
    tail = rng.choice(
        ["", "/", "/a/b", "/p?q=1", "/p#f", "?x=1", "#a:b@c", "/@vite/client", "/a/b@c"]
    )
    return f"{rng.choice(_SCHEMES)}://{userinfo}{host}{tail}"


def test_url_credential_random_urls_agree_with_urlsplit():
    rng = random.Random(20261105)
    with_password = without = 0
    for _ in range(3000):
        url = _random_url(rng)
        password = urlsplit(url).password
        text = f"see {url} tail\n"
        if password and set(password) != {"*"}:
            with_password += 1
            assert not scan(GuardState(), text).ok, url
        else:
            without += 1
            assert guard._url_credential(text) is False, url
    assert with_password > 500 and without > 500  # both branches were exercised


# What SQLAlchemy's ``make_url`` reads before the host (engine/url.py): the user
# name stops at ``:`` or ``/``, the password runs to the first ``@``.
_SQLA_USERINFO = re.compile(r"[\w+]+://(?P<user>[^:/]*)(?::(?P<password>[^@]*))?@")
_PASSWORD_CHARS = [
    "/", "?", "#", "\\", "\t", "\n", "\u00a0", "\u3000", "\u2028", "\uff0f", "\uff1f",
    "\uff03", "\uff3c", "%", "!", "$", "*", "+", "=", "&", "(", ")", ",", ";", "'", "~",
    ":", "[", "]", "<", ">", "|", "^", "`", '"', "{", "}", "-", "_", ".",
]  # fmt: skip


def test_no_password_a_url_parser_reads_is_stored():
    """URLs whose password SQLAlchemy's regex or urllib reads, with one character
    from the set that parsers disagree on at every offset of the password."""
    rng = random.Random(20261106)
    alnum = string.ascii_letters + string.digits
    read = 0
    for _ in range(4000):
        scheme = rng.choice(
            ["postgres", "https", "redis", "mysql", "x", "amqp", "http"]
        )
        user = "".join(rng.choice(alnum) for _ in range(rng.randint(0, 8)))
        # Starts with a letter: a password of up to five digits is a port (documented).
        body = rng.choice(string.ascii_letters) + "".join(
            rng.choice(alnum) for _ in range(rng.randint(5, 11))
        )
        at = rng.randint(0, len(body))
        password = body[:at] + rng.choice(_PASSWORD_CHARS) + body[at:]
        host = rng.choice(
            ["db", "localhost", "10.0.0.5", "cache.svc", "db:5432", "[::1]:6379"]
        )
        url = f"{scheme}://{user}:{password}@{host}/app"
        match = _SQLA_USERINFO.match(url)
        seen = (match.group("password") if match else None) or urlsplit(url).password
        if not seen or set(seen) == {"*"}:
            continue
        read += 1
        result = scan(GuardState(), f"see {url} tail\n")
        assert not result.ok, repr(url)
    assert read > 3000  # the generator is not just producing unparsable URLs


def test_a_line_break_in_the_userinfo_does_not_hide_the_password_of_the_next_event():
    results, state = _messages("see postgres://admin:\n", "hunter2pw@db/app done \n")
    assert [r.ok for r in results] == [True, False]
    assert results[1].reason == guard.REASON_URL_CREDENTIAL
    assert state.tainted


@pytest.mark.parametrize("end", ["\t", "\u00a0", "\u3000", "\u2028"])
def test_an_event_that_ends_on_a_blank_other_than_a_line_break_is_still_open(end):
    """Only a line break is how an event ends; a password followed by any other
    blank is withheld, since the next event can bring the @ right behind it."""
    results, _ = _messages("see postgres://admin:hunter2pw" + end, "@db/app done \n")
    assert not results[0].ok
    assert results[0].reason == guard.REASON_URL_CREDENTIAL


def test_an_event_that_ends_on_a_line_break_after_a_port_is_clean():
    results, _ = _messages("Open http://localhost:3000\n", "ok \n")
    assert [r.ok for r in results] == [True, True]


@pytest.mark.parametrize(
    "message",
    [
        "Dev server at http://localhost:<port>\n",
        "Dev server at http://localhost:$PORT\n",
        "Dev server at http://localhost:PORT\n",
        "Open http://localhost:3000\t",
        "Open http://localhost:3000\u00a0",
    ],
)
def test_a_placeholder_or_port_at_the_end_of_a_line_is_clean(message):
    results, _ = _messages(message, "ok \n")
    assert [r.ok for r in results] == [True, True]


def test_the_sanitizer_is_shown_blanks_where_the_text_has_unicode_spaces(monkeypatch):
    seen: list[str] = []

    def spy(text, **_):
        seen.append(text)
        return text

    monkeypatch.setattr(guard, "sanitize_log_text", spy)
    assert scan(GuardState(), "a\u00a0b\u3000c\u2003d\n").ok
    assert seen
    assert all(not set(text) & {"\u00a0", "\u3000", "\u2003"} for text in seen)


def test_unicode_spaces_are_kept_by_the_fold_and_other_look_alikes_still_fold():
    assert guard._fold("a\u00a0b\u3000c\u2003d") == "a\u00a0b\u3000c\u2003d"
    assert guard._fold("\uff50\uff41\u00a0\uff53") == "pa\u00a0s"


@pytest.mark.parametrize(
    "text",
    [
        "password : hunter2",
        "Authorization: Bearer abcdef0123456789abcdef",
        "Cookie: session=abc",
        "token = ghp_" + "a" * 20,
        "Basic YWRtaW46cGFzcw==",
        "contact me at someone@example.com",
        "-----BEGIN " "PRIVATE KEY-----",
        "the build passed in 3.2s with no warnings",
    ],
)
@pytest.mark.parametrize("space", ["\u00a0", "\u2003", "\u202f", "\u3000"])
def test_a_unicode_space_reads_as_a_blank_for_every_other_rule(text, space):
    plain = scan(GuardState(), text + "\n")
    unusual = scan(GuardState(), text.replace(" ", space) + "\n")
    assert (unusual.ok, unusual.reason) == (plain.ok, plain.reason)


def test_a_chain_of_anchors_inside_one_authority_is_linear():
    started = time.perf_counter()
    guard._url_credential(":\\\\a" * 30_000 + " ")
    guard._url_credential(("://a:b/" + "x" * 20) * 5_000 + " ")
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    "text",
    [
        ("://" + "a:" * 250 + " ") * 8,
        (":" + "/\\" * 250 + "x ") * 8,
        "://" * 20_000,
        ":" + "/" * 60_000,
        "://" + "@" * 60_000,
    ],
    ids=["colon-labels", "backslashes", "repeated-anchor", "many-slashes", "many-ats"],
)
def test_the_url_rule_is_linear_on_adversarial_text(text):
    started = time.perf_counter()
    guard._url_credential(text)
    assert time.perf_counter() - started < 1.0


# A gap longer than the window keeps is normalized, and a plain blank is the one
# character that ends an authority: the blank kept in a run's place has to be of
# the kind the run held, or the password after it is no longer part of the URL.
_NON_ENDING_BLANKS = ["\t", "\u00a0", "\u3000", "\u2003", "\u202f", "\u2028", "\t\u00a0"]


@pytest.mark.parametrize("blank", _NON_ENDING_BLANKS)
@pytest.mark.parametrize("length", [1, 28, 29, 100, 400])
@pytest.mark.parametrize("side", ["after_colon", "before_colon"])
def test_a_long_run_of_blanks_that_keeps_the_authority_open_does_not_hide_the_password(
    blank, length, side
):
    gap = (blank * length)[:length]
    if side == "after_colon":
        first, second = "see postgres://admin:" + gap, "hunter2pw@db/app done \n"
    else:
        first, second = "see postgres://admin" + gap, ":hunter2pw@db/app done \n"
    results, state = _messages(first, second)
    assert [r.ok for r in results] == [True, False]
    assert results[1].reason == guard.REASON_URL_CREDENTIAL
    assert state.tainted


@pytest.mark.parametrize("blank", ["\t", "\u00a0", "\u3000"])
def test_a_gap_beyond_the_window_never_stores_the_password(blank):
    results, _ = _messages(
        "see postgres://admin:" + blank * 3000, "hunter2pw@db/app done \n"
    )
    assert not all(r.ok for r in results)
    assert not any("hunter2pw" in r.text for r in results)


def test_a_plain_blank_run_still_ends_the_authority_as_it_does_in_one_event():
    text = "see postgres://admin:" + " " * 100 + "hunter2pw@db/app done \n"
    assert scan(GuardState(), text).ok
    results, _ = _messages("see postgres://admin:" + " " * 100, "hunter2pw@db/app done \n")
    assert [r.ok for r in results] == [True, True]


def test_a_split_url_is_refused_whenever_the_same_text_in_one_event_is():
    """The window is a compressed copy of the history; it must not accept what
    the history itself would have been refused for."""
    rng = random.Random(20261107)
    blanks = ["\t", "\u00a0", "\u3000", "\u2003", "\n", "\t\u00a0", "\u2028"]

    def gap() -> str:
        if rng.random() < 0.4:
            return ""
        run = "".join(rng.choice(blanks) for _ in range(rng.randint(1, 90)))
        if rng.random() < 0.1:  # a plain blank ends the authority, in either form
            at = rng.randint(0, len(run))
            run = run[:at] + " " + run[at:]
        return run

    refused = 0
    for _ in range(600):
        text = (
            f"see {rng.choice(['postgres', 'https', 'redis'])}://{gap()}"
            f"{rng.choice(['admin', 'u', ''])}{gap()}:{gap()}hunter2pw{gap()}@db/app done \n"
        )
        if scan(GuardState(), text).ok:
            continue
        refused += 1
        cuts = [i for i in range(1, len(text)) if text[i - 1].isspace()]
        cut = rng.choice(cuts)
        results, _ = _messages(text[:cut], text[cut:])
        assert not all(r.ok for r in results), repr(text[:cut]) + " | " + repr(text[cut:])
    assert refused > 300


@functools.cache
def _characters_that_fold_into_a_blank():
    """Every character NFKC spells as, or with, a blank, other than a blank."""
    return [
        chr(code)
        for code in range(0x110000)
        if not chr(code).isspace()
        and not 0xD800 <= code <= 0xDFFF
        and " " in unicodedata.normalize("NFKC", chr(code))
        and not guard._FOLD_OUT_RE.match(chr(code))
    ]


def test_the_fold_keeps_every_character_that_nfkc_would_turn_into_a_blank():
    """A plain blank ends an authority; a URL parser reads none of these as one.
    The list is fixed (server and daemon must decide alike), so it is checked
    against the tables of the interpreter that runs the test."""
    kept = {chr(c) for low, high in guard._KEPT_RANGES for c in range(low, high + 1)}
    missing = [f"U+{ord(c):04X}" for c in _characters_that_fold_into_a_blank() if c not in kept]
    assert not missing
    assert all(guard._fold(c) == c for c in kept)
    assert guard._fold("\uff50\uff41\u00b4\uff53") == "pa\u00b4s"


@pytest.mark.parametrize("char", sorted(_characters_that_fold_into_a_blank()), ids=ascii)
def test_a_spacing_accent_in_a_password_does_not_end_the_authority(char):
    url = f"postgres://admin:hunter{char}2pw@db/app"
    assert urlsplit(url).password == f"hunter{char}2pw"
    result = scan(GuardState(), f"see {url} tail\n")
    assert not result.ok and result.reason == guard.REASON_URL_CREDENTIAL
    split, _ = _messages("see postgres://admin:hunter", f"{char}2pw@db/app done \n")
    assert not all(r.ok for r in split)


def test_the_sanitizer_still_reads_a_spacing_accent_as_the_blank_it_folds_to():
    assert guard._BLANKED_RE.sub(lambda m: guard._BLANKED_FORM[m.group()], "a\u00b4b\u3000c") == (
        "a \u0301b c"
    )


def test_a_token_user_name_with_a_line_break_inside_is_withheld_where_a_message_ends():
    results, state = _messages(
        f"see https://{TOKEN32[:10]}\n{TOKEN32[10:]}", "@host/o/r done \n"
    )
    assert not results[0].ok and results[0].reason == guard.REASON_URL_CREDENTIAL
    assert state.tainted


def test_a_placeholder_port_followed_by_a_path_at_the_end_of_a_line_is_clean():
    """The run that may still hold the @ ends at a line break that ends the text,
    as a displayed event always does."""
    results, _ = _messages("Open http://localhost:$PORT/docs\n", "ok \n")
    assert [r.ok for r in results] == [True, True]


@pytest.mark.parametrize(
    "first,second",
    [
        ("see smtp://deploy@mail:hunter2pw", "@smtp.internal/x done \n"),
        ("see postgres://a@b:hunter2pw", "@db/app done \n"),
        ("see https://user@corp:Zk39vQx", "@host/ done \n"),
        ("see postgres://a@b:hunter", "2pw@db/app done \n"),
        ("see postgres://a@b:h", "unter2pw@db/app done \n"),
        ("see postgres://a@b@c:hunter2pw", "@db/app done \n"),
        (f"see https://u@v:{TOKEN32}", "@host done \n"),
    ],
)
def test_an_at_in_the_user_name_does_not_open_the_password_to_a_message_boundary(
    first, second
):
    """With an ``@`` in the user name the userinfo is still open: the real one can
    come in the next message, so the password is withheld at its first character."""
    results, state = _messages(first, second)
    assert not results[0].ok
    assert results[0].reason == guard.REASON_URL_CREDENTIAL
    assert state.tainted


@pytest.mark.parametrize(
    "message",
    [
        "Clone ssh://git@host",
        "Clone ssh://git@host:22",
        "Open https://user@intranet:8080",
        "Open https://user@host:",
        "Open http://user@[::1",
        "Open http://user@[::1]:3000",
    ],
)
def test_a_host_and_port_after_an_at_at_the_end_of_a_message_is_clean(message):
    results, _ = _messages(message, "/x ok \n")
    assert results[0].ok, message
