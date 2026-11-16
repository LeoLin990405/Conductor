"""The signature of a failure, and the rules a human approved.

Section 9.1 of the W8 PRD: a signature is what lets the same failure be
counted twice without keeping the diagnostic, and a learned rule is what a
human approved after seeing the same failure often enough.

The tests below are the acceptance the task file names: the same failure
with other ids and numbers has one signature, two failures have two, the
sentinel is nowhere in the signature, and a shadowed rule changes nothing.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from pathlib import Path

import pytest

from src.llms.native_cli.failure import (
    FailureClass,
    LearnedRule,
    RuleSet,
    Signature,
    _SIGNATURE_VOCAB,
    classify_failure,
    failure_signature,
    redact_credentials,
    register_failure_rule,
)

# A diagnostic the built-in rules know nothing about. Every rule test uses
# it, so that what is being compared is the learned rule and not the
# vocabulary.
UNKNOWN_TEXT = "the widget registry is out of alignment"

# A diagnostic for a learned rule that matches on a pattern. The pattern is
# matched against the reduced text, and the reduced text holds only
# whitelist words now -- so the pattern names a word the reduction keeps.
PATTERN_TEXT = "the scheduler quota drifted"

# The same failure twice, with the parts that differ between two occurrences
# of it replaced: the request id, the timestamp and the token count.
FIRST_QUOTA = (
    "HTTP 429: Too Many Requests "
    "(request_id=req-8842, ts=2026-11-06T12:19:00Z, tokens=131072)"
)
SECOND_QUOTA = (
    "HTTP 429: Too Many Requests "
    "(request_id=req-9931, ts=2026-11-06T13:02:11Z, tokens=65536)"
)

SENTINEL = "sk-" "SENTINELtoken0123456789abcdef"

# A secret that is made of letters only. The number sweep cannot hide a
# leak of it, so a value that reaches the text was really not redacted.
OPAQUE_SECRET = "opaque_sentinel_secret"

# An account id in the same shape.
OPAQUE_ACCOUNT = "customeralpha"


def _write_rules(path: Path, rules: list[dict], mode: int = 0o600) -> Path:
    """Write a rules file with the mode the loader insists on."""
    path.write_text(json.dumps({"rules": rules}), encoding="utf-8")
    os.chmod(path, mode)
    return path


def _rule(
    signature_id: str,
    *,
    provider: str = "kimi",
    failure_class: FailureClass = FailureClass.RATE_LIMITED,
    status: str = "approved",
    pattern: str = "",
    action: str = "retry_backoff",
) -> dict:
    """One rule as it is written in the file."""
    return {
        "provider": provider,
        "signature_id": signature_id,
        "pattern": pattern,
        "class": failure_class.value,
        "action": action,
        "status": status,
        "approved_at": "2026-11-06T12:00:00Z",
        "evidence_count": 3,
    }


def _serialised(signature: Signature) -> str:
    """The signature as it reaches storage: its fields, flattened to text.

    A leak that the dataclass hides but the storage shows would still be a
    leak, so the serialised form is what the secret is looked for in.
    """
    return json.dumps(dataclasses.asdict(signature))


# --------------------------------------------------------------- the signature


def test_the_same_failure_with_other_ids_and_numbers_has_one_signature() -> None:
    """Two occurrences of one failure share a signature."""
    first = failure_signature("kimi", FIRST_QUOTA)
    second = failure_signature("kimi", SECOND_QUOTA)

    assert first.signature_id == second.signature_id
    assert first.normalized_text == second.normalized_text


def test_two_different_classes_have_two_different_signatures() -> None:
    """Two failures that are not the same do not share a signature."""
    quota = failure_signature("kimi", FIRST_QUOTA)
    auth = failure_signature("kimi", "401 Unauthorized: the api key was rejected")
    network = failure_signature("kimi", "connect ECONNREFUSED 10.0.0.1:443")

    assert len({quota.signature_id, auth.signature_id, network.signature_id}) == 3


def test_the_signature_id_is_the_sha256_of_the_reduced_text() -> None:
    """The id is derived from the text that is kept, and nothing else."""
    import hashlib

    signature = failure_signature("kimi", FIRST_QUOTA)

    expected = hashlib.sha256(signature.normalized_text.encode("utf-8")).hexdigest()
    assert signature.signature_id == expected
    assert len(signature.signature_id) == 64


def test_the_signature_text_holds_no_sentinel() -> None:
    """The sentinel never reaches the text that is kept."""
    text = f"the widget registry is out of alignment ({SENTINEL})"

    signature = failure_signature("kimi", text)

    assert SENTINEL not in signature.normalized_text
    assert "SENTINEL" not in signature.normalized_text
    assert signature.normalized_text


def test_the_raw_diagnostic_is_not_kept_anywhere_in_the_signature() -> None:
    """The signature holds the reduced text, not the text it came from."""
    text = f"widget registry drift detected for account acct_99secret ({SENTINEL})"

    signature = failure_signature("kimi", text)

    assert text not in signature.normalized_text
    assert "acct_99secret" not in signature.normalized_text
    assert "99secret" not in signature.normalized_text


def test_a_url_keeps_its_scheme_and_loses_the_rest_of_it() -> None:
    """The path and the query are where a key hides.

    The URL rule keeps the scheme and the host; the whitelist pass then
    keeps the words of that which it knows -- the scheme -- and
    placeholders the rest, so neither the path, nor the query, nor the
    address itself survives in full.
    """
    signature = failure_signature(
        "kimi", "GET https://api.example.com/v1/chat/completions?key=abcd1234 failed"
    )

    assert "https" in signature.normalized_text
    assert "api.example.com" not in signature.normalized_text
    assert "/v1/chat" not in signature.normalized_text
    assert "abcd1234" not in signature.normalized_text


def test_an_email_is_dropped() -> None:
    """An address is identifying, so it does not survive."""
    signature = failure_signature(
        "kimi", "the request from someone@example.com was refused"
    )

    assert "someone@example.com" not in signature.normalized_text
    assert "<email>" in signature.normalized_text


def test_a_token_is_dropped() -> None:
    """A key does not survive, in any of the shapes it takes."""
    for text in (
        f"the call failed with key {SENTINEL}",
        "the call failed with Bearer abcdefghijklmnop",
        "the call failed with api_key=abcdefghijklmnop",
    ):
        signature = failure_signature("kimi", text)
        assert "abcdefghijklmnop" not in signature.normalized_text
        assert "SENTINEL" not in signature.normalized_text


def test_an_account_id_is_dropped() -> None:
    """An account id does not survive."""
    signature = failure_signature("kimi", "account_id=acct_998877 failed")

    assert "998877" not in signature.normalized_text
    assert "<account>" in signature.normalized_text


def test_a_json_credential_is_not_in_the_signature() -> None:
    """The JSON form of a credential does not survive either.

    A rule that wants the separator right after the keyword misses the
    JSON form, because a quote sits between the keyword and the colon.
    The whole value has to go, so no part of it survives in the text or in
    anything that serialises the signature.
    """
    text = (
        '{"error":"request failed","api_key":"opaque_sentinel_secret",'
        '"account_id":"customeralpha"}'
    )

    signature = failure_signature("kimi", text)

    assert OPAQUE_SECRET not in signature.normalized_text
    assert OPAQUE_ACCOUNT not in signature.normalized_text
    assert OPAQUE_SECRET not in _serialised(signature)
    assert OPAQUE_ACCOUNT not in _serialised(signature)


def test_a_plain_credential_and_account_id_are_not_in_the_signature() -> None:
    """The plain form of a credential and of an account id does not survive."""
    for text in (
        f"the call failed with api_key={OPAQUE_SECRET}",
        f"the call failed with account_id={OPAQUE_ACCOUNT}",
    ):
        signature = failure_signature("kimi", text)

        assert OPAQUE_SECRET not in signature.normalized_text
        assert OPAQUE_ACCOUNT not in signature.normalized_text
        assert OPAQUE_SECRET not in _serialised(signature)
        assert OPAQUE_ACCOUNT not in _serialised(signature)


def test_a_bare_token_credential_is_not_in_the_signature() -> None:
    """A bare "token" is a credential when a value follows it."""
    for text in (
        f"the call failed with token={OPAQUE_SECRET}",
        '{"token":"opaquevalue"}',
    ):
        signature = failure_signature("kimi", text)

        assert OPAQUE_SECRET not in signature.normalized_text
        assert "opaquevalue" not in signature.normalized_text
        assert OPAQUE_SECRET not in _serialised(signature)
        assert "opaquevalue" not in _serialised(signature)


def test_the_word_token_in_prose_is_not_a_credential() -> None:
    """A token count is not a secret, so it stays in the text."""
    signature = failure_signature(
        "kimi", "input token count exceeds maximum number of tokens"
    )

    assert "<token>" not in signature.normalized_text
    assert "token" in signature.normalized_text


def test_a_bare_token_number_is_redacted_as_a_credential() -> None:
    """A number after a bare "token" cannot be told apart from a credential.

    The Planner's ruling: the exemption belongs to the keys that name a
    count, and a bare "token" is not one of them -- so "token=131072" goes
    the way every other credential value goes.
    """
    signature = failure_signature("kimi", "the turn used token=131072 of them")

    assert "<token>" in signature.normalized_text
    assert "131072" not in signature.normalized_text


def test_a_class_name_is_not_an_account_id() -> None:
    """A class name that starts with "Account" is not an account id.

    The keyword needs a separator, so a class name is left alone -- and
    the whitelist then takes the whole CamelCase word out, because it
    names no class of its own.
    """
    signature = failure_signature("kimi", "AccountQuotaExceeded")

    assert "<account>" not in signature.normalized_text
    assert signature.normalized_text == "<x>"


def test_a_uuid_is_dropped() -> None:
    """A uuid does not survive."""
    signature = failure_signature(
        "kimi", "request 7f3a9b21-4c5d-4e6f-8a9b-0c1d2e3f4a5b was refused"
    )

    assert "7f3a9b21" not in signature.normalized_text
    assert "<uuid>" in signature.normalized_text


def test_a_timestamp_is_dropped() -> None:
    """A timestamp does not survive."""
    signature = failure_signature(
        "kimi", "failure recorded at 2026-11-06T12:19:00Z by the scheduler"
    )

    assert "2026" not in signature.normalized_text
    assert "<timestamp>" in signature.normalized_text


def test_a_path_is_dropped() -> None:
    """A path does not survive."""
    signature = failure_signature(
        "kimi", "cannot read /Users/someone/private/state.json for the worker"
    )

    assert "/Users/someone" not in signature.normalized_text
    assert "<path>" in signature.normalized_text


def test_an_http_status_code_survives_the_number_sweep() -> None:
    """The status code is the one number that carries the class."""
    too_many = failure_signature("kimi", "HTTP 429 too many requests")
    unavailable = failure_signature("kimi", "HTTP 503 service unavailable")

    assert "429" in too_many.normalized_text
    assert "503" in unavailable.normalized_text
    assert too_many.signature_id != unavailable.signature_id


def test_a_status_code_behind_a_label_survives_the_number_sweep() -> None:
    """A status code named by a label is protected as well."""
    too_many = failure_signature("kimi", "request failed, status code: 429")
    unavailable = failure_signature("kimi", "request failed, status code: 503")

    assert "429" in too_many.normalized_text
    assert "503" in unavailable.normalized_text
    assert too_many.signature_id != unavailable.signature_id


def test_a_bare_status_code_is_removed_by_the_number_sweep() -> None:
    """A number on a line of its own is not a status code.

    Nothing names it, so it is as likely to be a count as a status, and the
    signature keeps only the codes a keyword names.
    """
    too_many = failure_signature("kimi", "429")
    unavailable = failure_signature("kimi", "503")

    assert "429" not in too_many.normalized_text
    assert "503" not in unavailable.normalized_text
    assert "<num>" in too_many.normalized_text


def test_a_number_before_its_phrase_is_removed_by_the_number_sweep() -> None:
    """The number has to follow a keyword; the phrase does not count."""
    too_many = failure_signature("kimi", "429 too many requests")

    assert "429" not in too_many.normalized_text
    assert "<num>" in too_many.normalized_text


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("status 500", "500"),
        ("HTTP/1.1 503", "503"),
        ("error code 404", "404"),
        ("code=429", "429"),
        ("http status 503", "503"),
    ],
)
def test_a_status_code_a_keyword_names_is_kept(text: str, code: str) -> None:
    """A keyword names the number, so the number is a status code."""
    signature = failure_signature("kimi", text)

    assert code in signature.normalized_text


@pytest.mark.parametrize(
    "text",
    [
        "429",
        "429 Too Many Requests",
        "token count 131072",
        "foo500",
        "status 600",
        "099",
        "99",
        "1000",
    ],
)
def test_a_number_no_keyword_names_is_removed(text: str) -> None:
    """Every other number is removed: only a status code survives."""
    signature = failure_signature("kimi", text)

    for number in ("429", "500", "503", "600", "099", "99", "1000", "131072"):
        assert number not in signature.normalized_text


def test_the_reduced_text_is_still_lower_cased_and_bounded() -> None:
    """The shape of a signature does not change with the numbers."""
    signature = failure_signature("kimi", "STATUS 500: Service Unavailable")

    assert signature.normalized_text == signature.normalized_text.lower()
    assert len(signature.normalized_text.encode("utf-8")) <= 512
    assert len(signature.signature_id) == 64
    int(signature.signature_id, 16)


def test_the_earliest_of_two_equally_scored_lines_is_the_error_line() -> None:
    """When two lines say as much as each other, the first one is taken."""
    both = failure_signature("kimi", "the upstream refused\nthe registry refused")
    first_only = failure_signature("kimi", "the upstream refused")
    second_only = failure_signature("kimi", "the registry refused")

    assert both.normalized_text == first_only.normalized_text
    assert both.normalized_text != second_only.normalized_text


def test_a_request_id_in_the_text_does_not_change_the_signature() -> None:
    """The number sweep removes what is only an identifier."""
    first = failure_signature("kimi", "the scheduler refused request 88421 again")
    second = failure_signature("kimi", "the scheduler refused request 11993 again")

    assert first.signature_id == second.signature_id


def test_the_reduced_text_is_bounded() -> None:
    """A signature is a fixed-size thing, however long the diagnostic was."""
    signature = failure_signature("kimi", "widget drift " + ("padding " * 400))

    assert len(signature.normalized_text.encode("utf-8")) <= 512


def test_an_empty_diagnostic_still_has_a_signature() -> None:
    """Nothing to reduce is still a shape, and it is a stable one."""
    signature = failure_signature("kimi", "")

    assert signature.normalized_text == ""
    assert len(signature.signature_id) == 64


def test_the_signature_keeps_the_provider_it_was_asked_for() -> None:
    """The provider is part of the record, beside the id."""
    signature = failure_signature("Kimi", FIRST_QUOTA)

    assert signature.provider == "kimi"


# -------------------------------------------------- the redaction of a credential
#
# A credential must not reach the signature, in any of the shapes it takes:
# the JSON form, the quoted form, the bare form, the Authorization header,
# and the value of a mapping, where the key is the only thing that names it.

# The six counter-examples the review named, one row each: the diagnostic
# and the secret it carries.
CODEX_COUNTER_EXAMPLES = (
    (
        "json_escaped_quote_key",
        json.dumps({"error": "request failed", "api_key": 'prefix"opaque_sentinel_secret'}),
        OPAQUE_SECRET,
    ),
    (
        "json_escaped_quote_account",
        json.dumps({"error": "request failed", "account_id": 'prefix"customeralpha'}),
        OPAQUE_ACCOUNT,
    ),
    (
        "basic_authorization",
        "request failed Authorization: Basic alphabeticopaquecredentials",
        "alphabeticopaquecredentials",
    ),
    (
        "bearer_plus",
        "request failed Authorization: Bearer abcdefghijklmnop+opaque_sentinel_secret",
        OPAQUE_SECRET,
    ),
    (
        "bearer_slash",
        "request failed Authorization: Bearer abcdefghijklmnop/opaque_sentinel_secret",
        OPAQUE_SECRET,
    ),
    (
        "mapping_credential",
        {"api_key": OPAQUE_SECRET, "message": "request rejected"},
        OPAQUE_SECRET,
    ),
)

# The shapes one credential takes. The sentinel is letters only, so no
# number sweep can hide a leak of it.
PURE_LETTERS = "purelettersentinelcredentialvalue"

# Every shape of the same credential, and the secret each one carries.
CREDENTIAL_VECTORS = (
    ("json form", '{"api_key":"purelettersentinelcredentialvalue"}', PURE_LETTERS),
    ("single quoted form", "api_key='purelettersentinelcredentialvalue'", PURE_LETTERS),
    ("double quoted form", 'api_key="purelettersentinelcredentialvalue"', PURE_LETTERS),
    ("bare form", "api_key=purelettersentinelcredentialvalue", PURE_LETTERS),
    (
        "authorization bearer",
        "Authorization: Bearer purelettersentinelcredentialvalue",
        PURE_LETTERS,
    ),
    (
        "authorization basic",
        "Authorization: Basic purelettersentinelcredentialvalue",
        PURE_LETTERS,
    ),
    (
        "authorization token",
        "Authorization: Token purelettersentinelcredentialvalue",
        PURE_LETTERS,
    ),
    (
        "authorization custom scheme",
        "Authorization: CustomScheme purelettersentinelcredentialvalue",
        PURE_LETTERS,
    ),
    (
        "escaped json",
        '{"api_key":"prefix\\"purelettersentinelcredentialvalue"}',
        PURE_LETTERS,
    ),
    ("mapping string", {"api_key": PURE_LETTERS}, PURE_LETTERS),
    ("mapping integer", {"api_key": 12345678}, "12345678"),
    ("mapping list", {"api_key": [PURE_LETTERS]}, PURE_LETTERS),
    ("mapping mapping", {"api_key": {"inner": PURE_LETTERS}}, PURE_LETTERS),
    ("mapping none", {"api_key": None}, "None"),
)


@pytest.mark.parametrize(
    ("case", "diagnostic", "sentinel"),
    CODEX_COUNTER_EXAMPLES,
    ids=[case for case, _, _ in CODEX_COUNTER_EXAMPLES],
)
def test_a_credential_never_reaches_the_signature(
    case: str, diagnostic: object, sentinel: str
) -> None:
    """Every shape a credential takes is gone from the signature.

    The sentinel is looked for in the reduced text and in the serialised
    form, because a leak the dataclass hides but the storage shows would
    still be a leak.
    """
    signature = failure_signature("kimi", diagnostic)  # type: ignore[arg-type]

    assert sentinel not in signature.normalized_text, case
    assert sentinel not in _serialised(signature), case


def test_the_minimal_public_example_leaks_nothing() -> None:
    """The example the review named: a mapping with a credential in it.

    The key is the only thing that names the value, so the value has to go
    before the mapping is flattened into text.
    """
    signature = failure_signature(
        "kimi", {"api_key": "opaque_sentinel_secret", "message": "request rejected"}
    )

    assert OPAQUE_SECRET not in signature.normalized_text
    assert OPAQUE_SECRET not in _serialised(signature)


@pytest.mark.parametrize(
    ("case", "diagnostic", "sentinel"),
    CREDENTIAL_VECTORS,
    ids=[case for case, _, _ in CREDENTIAL_VECTORS],
)
def test_a_credential_vector_never_reaches_the_signature(
    case: str, diagnostic: object, sentinel: str
) -> None:
    """One vector per shape a credential takes, and none of them survives."""
    signature = failure_signature("kimi", diagnostic)  # type: ignore[arg-type]

    assert sentinel not in signature.normalized_text, case
    assert sentinel not in _serialised(signature), case


@pytest.mark.parametrize(
    ("case", "diagnostic", "sentinel"),
    CREDENTIAL_VECTORS,
    ids=[case for case, _, _ in CREDENTIAL_VECTORS],
)
def test_the_redaction_of_a_credential_is_the_same_every_time(
    case: str, diagnostic: object, sentinel: str
) -> None:
    """The same input gives the same output, so a result may be compared."""
    first = redact_credentials(diagnostic)
    second = redact_credentials(diagnostic)

    assert first == second, case
    assert sentinel not in json.dumps(first, default=str), case


@pytest.mark.parametrize(
    ("case", "diagnostic", "sentinel"),
    CREDENTIAL_VECTORS,
    ids=[case for case, _, _ in CREDENTIAL_VECTORS],
)
def test_the_redaction_leaves_one_marker_behind(
    case: str, diagnostic: object, sentinel: str
) -> None:
    """The marker is one string, so a caller may look for it."""
    redacted = redact_credentials(diagnostic)

    assert "[redacted]" in json.dumps(redacted, default=str), case


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Ruling 1: only a three digit code a keyword names survives.
        ("status 500", "status 500"),
        ("HTTP/1.1 503", "http/<num>.<num> 503"),
        ("error code 404", "error code 404"),
        ("code=429", "code=429"),
        ("http status 503", "http status 503"),
        ("429", "<num>"),
        ("429 Too Many Requests", "<num> too many requests"),
        ("token count 131072", "token count <num>"),
        # A word the vocabulary does not name goes, whatever it sat next to.
        ("foo500", "<x><num>"),
        ("status 600", "status <num>"),
        # The vocabulary words stay; the prose between them goes.
        (
            "input token count exceeds maximum number of tokens",
            "input token count exceeds maximum number <x> tokens",
        ),
        # A CamelCase class name is one word the vocabulary does not name.
        ("AccountQuotaExceeded", "<x>"),
        # A bare "token" is a credential key and not a count key, so the
        # number after it goes with it.
        ("token=131072", "<token>"),
        # A count key keeps its number, and the number sweep takes it.
        ("max_tokens=131072", "max_tokens=<num>"),
        ("tokens=131072", "tokens=<num>"),
        # An account id is still an account id.
        ("account_id=acct_998877 failed", "<account> failed"),
    ],
)
def test_the_reduction_is_not_changed_by_the_redaction(text: str, expected: str) -> None:
    """The redaction changes the credentials and nothing else.

    The number rules and the words that only look like credentials are the
    ones that were judged before, and they are judged the same way now --
    and the whitelist that ends the reduction takes every word the
    vocabulary does not name out of what is left of them.
    """
    signature = failure_signature("kimi", text)

    assert signature.normalized_text == expected
    assert len(signature.signature_id) == 64
    assert len(signature.normalized_text.encode("utf-8")) <= 512


def test_a_credential_reaches_neither_the_classification_nor_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The signature is where a credential stops."""
    diagnostic = f"the widget registry is out of alignment ({OPAQUE_SECRET})"

    with caplog.at_level("INFO", logger="src.llms.native_cli.failure"):
        classification = classify_failure("kimi", diagnostic)

    assert OPAQUE_SECRET not in classification.reason_code
    assert OPAQUE_SECRET not in str(classification)
    assert OPAQUE_SECRET not in caplog.text


def test_a_credential_in_a_mapping_reaches_neither_the_log_nor_the_reason_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A mapping is redacted before it is flattened, so nothing leaks."""
    diagnostic = {"api_key": OPAQUE_SECRET, "message": "request rejected"}

    with caplog.at_level("INFO", logger="src.llms.native_cli.failure"):
        classification = classify_failure("kimi", diagnostic)

    assert OPAQUE_SECRET not in classification.reason_code
    assert OPAQUE_SECRET not in caplog.text


# ------------------------------------------------------------- the rule set


def test_a_missing_rules_file_is_not_a_failure(tmp_path: Path) -> None:
    """Most Workers never fail, so most machines have no rules file."""
    rules = RuleSet.load(tmp_path / "does-not-exist.json")

    assert len(rules) == 0


def test_a_rules_file_that_is_not_private_is_refused(tmp_path: Path) -> None:
    """A file another user can write is a file nobody signed off on."""
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(failure_signature("kimi", UNKNOWN_TEXT).signature_id)],
        mode=0o644,
    )

    rules = RuleSet.load(path)

    assert len(rules) == 0


def test_a_private_rules_file_is_read(tmp_path: Path) -> None:
    """The file the approval flow writes is read."""
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(failure_signature("kimi", UNKNOWN_TEXT).signature_id)],
    )

    rules = RuleSet.load(path)

    assert len(rules) == 1
    assert rules.rules[0].failure_class is FailureClass.RATE_LIMITED
    assert rules.rules[0].evidence_count == 3


def test_a_rule_that_cannot_be_used_is_skipped(tmp_path: Path) -> None:
    """One damaged rule costs the rule, not the file."""
    path = _write_rules(
        tmp_path / "rules.json",
        [
            {"provider": "kimi", "signature_id": "not-a-sha", "class": "crash"},
            {"provider": "kimi", "signature_id": "a" * 64, "class": "nonsense"},
            {"provider": "", "signature_id": "b" * 64, "class": "crash"},
            _rule(failure_signature("kimi", UNKNOWN_TEXT).signature_id),
        ],
    )

    rules = RuleSet.load(path)

    assert len(rules) == 1


def test_a_damaged_file_is_not_a_failure(tmp_path: Path) -> None:
    """A file that is not JSON costs the rules and not the classifier."""
    path = tmp_path / "rules.json"
    path.write_text("{not json at all", encoding="utf-8")
    os.chmod(path, 0o600)

    rules = RuleSet.load(path)

    assert len(rules) == 0


# ------------------------------------------------------- the learned rules


def test_an_approved_rule_is_used_when_no_built_in_rule_matched(tmp_path: Path) -> None:
    """An approved rule fills a gap the vocabulary does not cover."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    path = _write_rules(tmp_path / "rules.json", [_rule(signature_id)])
    rules = RuleSet.load(path)

    classification = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert classification.failure_class is FailureClass.RATE_LIMITED
    assert classification.reason_code == "learned_rate_limited"


def test_a_built_in_rule_wins_over_a_learned_rule(tmp_path: Path) -> None:
    """The vocabulary outranks what was learned."""
    signature_id = failure_signature("kimi", FIRST_QUOTA).signature_id
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(signature_id, failure_class=FailureClass.NETWORK)],
    )
    rules = RuleSet.load(path)

    classification = classify_failure("kimi", FIRST_QUOTA, ruleset=rules)

    assert classification.failure_class is FailureClass.RATE_LIMITED
    assert classification.reason_code == "worker_rate_limited"


def test_a_learned_rule_may_not_introduce_an_auth_failure(tmp_path: Path) -> None:
    """That verdict is the one a human has to make."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(signature_id, failure_class=FailureClass.AUTH_FAILED)],
    )
    rules = RuleSet.load(path)

    classification = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert classification.failure_class is not FailureClass.AUTH_FAILED
    assert classification.failure_class is FailureClass.UNKNOWN


def test_a_learned_rule_may_not_overturn_an_auth_failure(tmp_path: Path) -> None:
    """A learned rule cannot talk a Worker out of a credential failure."""
    text = "HTTP 401 Unauthorized: the api key was rejected"
    signature_id = failure_signature("kimi", text).signature_id
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(signature_id, failure_class=FailureClass.RATE_LIMITED)],
    )
    rules = RuleSet.load(path)

    classification = classify_failure("kimi", text, ruleset=rules)

    assert classification.failure_class is FailureClass.AUTH_FAILED


def test_a_shadow_rule_does_not_change_the_action(tmp_path: Path) -> None:
    """A candidate that has not been approved changes nothing."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(signature_id, failure_class=FailureClass.RATE_LIMITED, status="candidate")],
    )
    rules = RuleSet.load(path)

    with_rules = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)
    without_rules = classify_failure("kimi", UNKNOWN_TEXT)

    assert with_rules == without_rules
    assert with_rules.failure_class is FailureClass.UNKNOWN
    assert with_rules.reason_code == "worker_unknown"


def test_a_shadow_rule_records_its_prediction(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A candidate says what it would have said, so it can be judged."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(signature_id, failure_class=FailureClass.RATE_LIMITED, status="candidate")],
    )
    rules = RuleSet.load(path)

    with caplog.at_level("INFO", logger="src.llms.native_cli.failure"):
        classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    shadow = [record for record in caplog.records if "shadow prediction" in record.message]
    assert shadow, "the shadow prediction was not recorded"
    assert "rate_limited" in shadow[0].message
    assert UNKNOWN_TEXT not in shadow[0].message


def test_a_rejected_rule_does_nothing(tmp_path: Path) -> None:
    """A rejected signature is remembered so it is not asked about again."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(signature_id, failure_class=FailureClass.RATE_LIMITED, status="rejected")],
    )
    rules = RuleSet.load(path)

    classification = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert classification.failure_class is FailureClass.UNKNOWN


def test_a_provider_alias_reaches_the_same_rule(tmp_path: Path) -> None:
    """A rule written under one spelling is found under the other."""
    text = "the widget registry is out of alignment"
    signature_id = failure_signature("claude", text).signature_id
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(signature_id, provider="claude")],
    )
    rules = RuleSet.load(path)

    assert rules.approved_rule("claude-code", signature_id) is not None
    assert rules.approved_rule("claude", signature_id) is not None


def test_a_rule_of_another_provider_is_not_applied(tmp_path: Path) -> None:
    """A rule is about one provider's wording."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule(signature_id, provider="codex")],
    )
    rules = RuleSet.load(path)

    classification = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert classification.failure_class is FailureClass.UNKNOWN


def test_a_rule_may_match_on_a_pattern(tmp_path: Path) -> None:
    """A rule written against a shape finds it without knowing the id.

    The pattern is matched against the reduced text, and the reduced text
    holds only whitelist words now -- so a pattern names a word the
    vocabulary keeps, not a word the diagnostic happened to carry.
    """
    path = _write_rules(
        tmp_path / "rules.json",
        [_rule("0" * 64, pattern=r"\bquota\b")],
    )
    rules = RuleSet.load(path)

    classification = classify_failure("kimi", PATTERN_TEXT, ruleset=rules)

    assert classification.failure_class is FailureClass.RATE_LIMITED


def test_the_classification_still_carries_only_three_fields() -> None:
    """The result is still the class, the code and the delay."""
    classification = classify_failure("kimi", FIRST_QUOTA)

    assert set(vars(classification)) == {
        "failure_class",
        "reason_code",
        "retry_after_seconds",
    }


def test_a_learned_rule_is_built_by_hand_the_way_the_file_builds_it() -> None:
    """The dataclass and the file agree about what a rule is."""
    rule = LearnedRule(
        provider="kimi",
        signature_id="a" * 64,
        pattern="",
        failure_class=FailureClass.NETWORK,
        action="retry_backoff",
        status="approved",
        approved_at="2026-11-06T12:00:00Z",
        evidence_count=2,
    )
    rules = RuleSet([rule])

    assert rules.approved_rule("kimi", "a" * 64) is rule


# ------------------------------------------- the provider aliases and the 5xx
#
# The three rules below are about the classifier rather than about the
# signature, but they are the same three defects the review found: the
# redaction, the "glm" alias and the range of the server-error codes.


def test_the_glm_alias_reaches_the_zcode_rules() -> None:
    """The dispatch roster's "glm" is the zcode adapter's spelling.

    The launch script canonicalises "zcode" and "zcode-cli" to "glm", so
    the Worker the roster runs as "glm" is the zcode one, and its own
    wording -- a spent credit -- has to be found under that name. Under
    the wrong alias the same failure was a crash for this Worker and an
    exhausted quota for every other name of it.
    """
    classification = classify_failure("glm", "credit_balance_exhausted", exit_code=1)

    assert classification.failure_class is FailureClass.QUOTA_EXHAUSTED
    assert classification.reason_code == "zcode_credit_exhausted"


def test_zhipu_and_zcode_stay_apart() -> None:
    """The two adapters keep their own rules, under their own names.

    "zhipu" is still reachable under its own name, and the wording of one
    adapter is not the wording of the other.
    """
    zhipu = classify_failure("zhipu", "[1302]")
    zcode = classify_failure("zcode", "[1302]")

    assert zhipu.failure_class is FailureClass.RATE_LIMITED
    assert zhipu.reason_code == "zhipu_account_rate_limited"
    assert zcode.failure_class is FailureClass.UNKNOWN


@pytest.mark.parametrize("code", [500, 503, 522, 529, 599])
def test_a_server_error_in_the_5xx_range_is_overloaded(code: int) -> None:
    """Every 5xx is the server's problem, not the Worker's.

    The range used to stop at 511 and jump to 529, so the codes in
    between fell through to "crash" -- and the recovery policy treats a
    crash differently from a busy server.
    """
    classification = classify_failure("kimi", f"HTTP {code}", exit_code=1)

    assert classification.failure_class is FailureClass.OVERLOADED


def test_a_rate_limit_still_wins_over_a_server_error() -> None:
    """The fixed priority decides when the text says two things at once."""
    classification = classify_failure(
        "kimi", "HTTP 429 Too Many Requests; HTTP 522", exit_code=1
    )

    assert classification.failure_class is FailureClass.RATE_LIMITED


# ----------------------------------------------- the approval of a learned rule
#
# A learned rule acts only when it carries its approval metadata: when it
# was approved, what should be done and how often the failure was seen. A
# rule that is missing any of the three has nothing behind it, so it only
# shadows.


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("approved_at", None),
        ("action", None),
        ("evidence_count", None),
        ("approved_at", ""),
        ("action", "   "),
        ("evidence_count", 0),
    ],
)
def test_an_approved_rule_without_its_approval_only_shadows(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    field: str,
    value: str | int | None,
) -> None:
    """A rule that carries no approval metadata does not act.

    It says what it would have said, and the reason code says why it only
    shadows.
    """
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    rule = _rule(signature_id)
    if value is None:
        rule.pop(field)
    else:
        rule[field] = value
    path = _write_rules(tmp_path / "rules.json", [rule])

    with caplog.at_level("WARNING", logger="src.llms.native_cli.failure"):
        rules = RuleSet.load(path)
        classification = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert classification.failure_class is FailureClass.UNKNOWN
    assert classification.reason_code == "worker_unknown"
    assert rules.approved_rule("kimi", signature_id) is None
    assert rules.shadow_rules("kimi", signature_id)
    assert rules.rules[0].effective_status == "candidate"
    assert field in rules.rules[0].missing_approval
    assert "rule_missing_approval" in caplog.text


def test_an_approved_rule_with_its_approval_acts(tmp_path: Path) -> None:
    """A rule that carries its approval metadata acts as it always did."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    path = _write_rules(tmp_path / "rules.json", [_rule(signature_id)])

    rules = RuleSet.load(path)
    classification = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert classification.failure_class is FailureClass.RATE_LIMITED
    assert classification.reason_code == "learned_rate_limited"
    assert rules.rules[0].effective_status == "approved"
    assert rules.rules[0].missing_approval == ()


def test_a_candidate_rule_still_only_shadows(tmp_path: Path) -> None:
    """A rule that was never approved does not act either."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    rule = _rule(signature_id)
    rule["status"] = "candidate"
    path = _write_rules(tmp_path / "rules.json", [rule])

    rules = RuleSet.load(path)
    classification = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert classification.failure_class is FailureClass.UNKNOWN
    assert rules.approved_rule("kimi", signature_id) is None
    assert rules.shadow_rules("kimi", signature_id)
    assert rules.rules[0].effective_status == "candidate"


def test_the_log_of_a_downgraded_rule_holds_no_diagnostic(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The log says what is missing, not what the Worker said."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    rule = _rule(signature_id)
    rule.pop("approved_at")
    path = _write_rules(tmp_path / "rules.json", [rule])

    with caplog.at_level("INFO", logger="src.llms.native_cli.failure"):
        rules = RuleSet.load(path)
        classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert "rule_missing_approval" in caplog.text
    assert "approved_at" in caplog.text
    assert UNKNOWN_TEXT not in caplog.text


def test_a_rule_built_by_hand_without_its_approval_only_shadows() -> None:
    """A rule that carries no approval metadata does not act.

    The file loader judges a rule and a rule that was built by hand is
    judged by the same rule, so a rule without an approval behind it only
    shadows -- it says what it would have said and changes nothing.
    """
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    rule = LearnedRule(
        provider="kimi",
        signature_id=signature_id,
        pattern="",
        failure_class=FailureClass.RATE_LIMITED,
        action="",
        status="approved",
        approved_at=None,
        evidence_count=0,
    )
    rules = RuleSet([rule])

    classification = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert rule.effective_status == "candidate"
    assert rule.missing_approval == ("approved_at", "action", "evidence_count")
    assert classification.failure_class is FailureClass.UNKNOWN
    assert classification.reason_code == "worker_unknown"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("approved_at", None),
        ("approved_at", ""),
        ("action", ""),
        ("action", "   "),
        ("evidence_count", 0),
    ],
)
def test_a_rule_built_by_hand_is_judged_by_its_own_fields(
    field: str, value: object
) -> None:
    """One field without its approval is enough to keep the rule from acting."""
    fields = {
        "provider": "kimi",
        "signature_id": failure_signature("kimi", UNKNOWN_TEXT).signature_id,
        "pattern": "",
        "failure_class": FailureClass.RATE_LIMITED,
        "action": "retry_backoff",
        "status": "approved",
        "approved_at": "2026-11-06T12:00:00Z",
        "evidence_count": 3,
    }
    fields[field] = value
    rule = LearnedRule(**fields)  # type: ignore[arg-type]

    assert field in rule.missing_approval
    assert rule.effective_status == "candidate"


def test_a_rule_built_by_hand_with_its_approval_still_acts() -> None:
    """A rule that carries its approval metadata acts as it always did."""
    signature_id = failure_signature("kimi", UNKNOWN_TEXT).signature_id
    rule = LearnedRule(
        provider="kimi",
        signature_id=signature_id,
        pattern="",
        failure_class=FailureClass.RATE_LIMITED,
        action="retry_backoff",
        status="approved",
        approved_at="2026-11-06T12:00:00Z",
        evidence_count=3,
    )
    rules = RuleSet([rule])

    classification = classify_failure("kimi", UNKNOWN_TEXT, ruleset=rules)

    assert rule.effective_status == "approved"
    assert rule.missing_approval == ()
    assert classification.failure_class is FailureClass.RATE_LIMITED
    assert classification.reason_code == "learned_rate_limited"


def test_a_rule_that_was_never_approved_is_not_affected_by_the_judgement() -> None:
    """A candidate or a rejected rule was never going to act anyway."""
    for status in ("candidate", "rejected"):
        rule = LearnedRule(
            provider="kimi",
            signature_id=failure_signature("kimi", UNKNOWN_TEXT).signature_id,
            pattern="",
            failure_class=FailureClass.RATE_LIMITED,
            action="",
            status=status,
            approved_at=None,
            evidence_count=0,
        )

        assert rule.missing_approval == ()
        assert rule.effective_status == status


# ------------------------------------------------------- the companion words
#
# A rule may name the words it needs beside its own wording: one phrase
# can mean two classes, and the companion word is what tells them apart.


def test_a_rule_with_a_companion_word_fires_only_with_it() -> None:
    """The phrase and one of the companion words are both needed."""
    register_failure_rule(
        "test-provider-needs-quota",
        FailureClass.QUOTA_EXHAUSTED,
        reason_code="test_needs_quota",
        patterns=("resource_exhausted",),
        requires=("quota", "credit"),
    )

    with_quota = classify_failure(
        "test-provider-needs-quota", "RESOURCE_EXHAUSTED: quota left"
    )
    with_credit = classify_failure(
        "test-provider-needs-quota", "RESOURCE_EXHAUSTED: no credit"
    )
    without = classify_failure("test-provider-needs-quota", "RESOURCE_EXHAUSTED")

    assert with_quota.failure_class is FailureClass.QUOTA_EXHAUSTED
    assert with_quota.reason_code == "test_needs_quota"
    assert with_credit.failure_class is FailureClass.QUOTA_EXHAUSTED
    assert without.failure_class is FailureClass.UNKNOWN


def test_a_rule_with_a_companion_word_ignores_the_status_code() -> None:
    """A rule that needs a companion must not fire on a bare status code."""
    register_failure_rule(
        "test-provider-needs-quota-status",
        FailureClass.QUOTA_EXHAUSTED,
        reason_code="test_needs_quota_status",
        patterns=("resource_exhausted",),
        status_codes=(429,),
        requires=("quota",),
    )

    classification = classify_failure(
        "test-provider-needs-quota-status", {"status_code": 429}
    )

    assert classification.failure_class is FailureClass.RATE_LIMITED
    assert classification.reason_code == "worker_rate_limited"


def test_a_rule_without_a_companion_word_behaves_as_before() -> None:
    """A rule that needs nothing extra fires on its pattern and on its code."""
    register_failure_rule(
        "test-provider-plain",
        FailureClass.OVERLOADED,
        reason_code="test_plain_overloaded",
        patterns=("resource_exhausted",),
        status_codes=(599,),
    )

    on_pattern = classify_failure("test-provider-plain", "RESOURCE_EXHAUSTED")
    on_status = classify_failure("test-provider-plain", {"status_code": 599})

    assert on_pattern.failure_class is FailureClass.OVERLOADED
    assert on_pattern.reason_code == "test_plain_overloaded"
    assert on_status.failure_class is FailureClass.OVERLOADED
    assert on_status.reason_code == "test_plain_overloaded"


# ----------------------------------------------------------------------
# the second fix round: what the independent verifier found
# ----------------------------------------------------------------------


def test_a_byte_diagnostic_is_redacted_too() -> None:
    # A diagnostic may arrive as bytes. The redaction has to see the text
    # inside them: the bytes are not a string, and the text they carry only
    # appears later -- so a byte diagnostic used to be a way around it.
    for payload in (
        b"api_key=opaque_sentinel_secret",
        bytearray(b"api_key=opaque_sentinel_secret"),
    ):
        signature = failure_signature("kimi", payload)

        assert "opaque_sentinel_secret" not in signature.normalized_text
        assert "opaque_sentinel_secret" not in _serialised(signature)


def test_a_number_under_a_credential_key_is_a_value_and_goes() -> None:
    # A number is the value of the key it sits under, and a value goes: a
    # purely numeric API key is still an API key.
    for text in (
        '{"api_key":"1234567890123456"}',
        "api_key=1234567890123456",
        "password='999888777'",
        "secret=1234567890",
    ):
        signature = failure_signature("kimi", text)

        assert "1234567890123456" not in signature.normalized_text
        assert "999888777" not in signature.normalized_text
        assert "1234567890" not in signature.normalized_text


def test_a_count_key_is_still_not_a_credential() -> None:
    # The exemption belongs to the keys that name a count, and to nothing
    # else. A bare "token" is not one of them, so it goes.
    for text in ("the turn used max_tokens=131072 of them", "tokens=42"):
        signature = failure_signature("kimi", text)

        assert "<token>" not in signature.normalized_text

    for text in ("the turn used token=131072 of them", "token=0"):
        signature = failure_signature("kimi", text)

        assert "<token>" in signature.normalized_text


# ----------------------------------------------------------------------
# the third fix round: a credential whose value is a number
# ----------------------------------------------------------------------

# The four shapes in which a credential's value happens to be made of
# digits only, and the digit string each one carries. A number is a secret
# when a key or a scheme names it: the exemption that lets a count through
# belongs to the seven keys that name a count and to nothing else.
#
# The last column is the reduced text each one must give. It says what the
# reduction has to see: a credential, and not a number that was taken for a
# count.
NUMERIC_CREDENTIAL_VECTORS = (
    ("password", "password='9876543210'", "9876543210", "<token>"),
    ("api key", "api_key=9876543211", "9876543211", "<token>"),
    (
        "authorization bearer",
        "Authorization: Bearer 9876543212",
        "9876543212",
        "authorization: bearer [redacted]",
    ),
    ("bearer", "Bearer 9876543213", "9876543213", "bearer [redacted]"),
)

# The one shape that is not a credential: the number of tokens a turn used,
# under a key that names a count.
TOKEN_COUNT_VECTOR = ("token count", "max_tokens=131072", "131072")

# The shape that used to be one and is not any more: a bare "token" cannot
# be told apart from a credential, so it goes with the rest of them.
BARE_TOKEN_COUNT_VECTOR = ("bare token count", "token=131072", "131072")

# The seven key names whose number is a count and not a secret.
COUNT_KEY_NAMES = (
    "tokens",
    "token_count",
    "max_tokens",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "context_window_tokens",
)

# The same shapes as a mapping, where the key is the only thing that names
# the value.
NUMERIC_CREDENTIAL_MAPPINGS = (
    (
        "password",
        {"password": "9876543210", "message": "request rejected"},
        "9876543210",
    ),
    (
        "api key",
        {"api_key": "9876543211", "message": "request rejected"},
        "9876543211",
    ),
    (
        "authorization bearer",
        {"authorization": "Bearer 9876543212", "message": "request rejected"},
        "9876543212",
    ),
    (
        "bearer",
        {"message": "Bearer 9876543213", "request": "rejected"},
        "9876543213",
    ),
    # The count in a mapping: a bare "token" names a credential, so the whole
    # value goes -- the same as it does in a text.
    ("bare token as a value", {"token": 131072, "message": "request rejected"}, "131072"),
    ("bare token in a message", {"message": "token=131072"}, "131072"),
)


@pytest.mark.parametrize(
    ("case", "diagnostic", "sentinel", "reduced"),
    NUMERIC_CREDENTIAL_VECTORS,
    ids=[case for case, _, _, _ in NUMERIC_CREDENTIAL_VECTORS],
)
def test_a_numeric_credential_never_reaches_the_signature(
    case: str, diagnostic: str, sentinel: str, reduced: str
) -> None:
    """A number is a credential when a key or a scheme names it.

    The digits are looked for in the reduced text and in the serialised form,
    because a leak the dataclass hides but the storage shows would still be
    a leak.

    The reduction is checked as well, and so is the redaction: the reduced
    text takes every number away, so a rule that let the digits through
    would still leave a number behind where a credential belongs. The
    redaction is where the digits are still visible.
    """
    signature = failure_signature("kimi", diagnostic)
    redacted = redact_credentials(diagnostic)

    assert sentinel not in signature.normalized_text, case
    assert sentinel not in _serialised(signature), case
    assert sentinel not in redacted, case
    assert "[redacted]" in redacted, case
    assert signature.normalized_text == reduced, case


@pytest.mark.parametrize(
    ("case", "diagnostic", "sentinel"),
    NUMERIC_CREDENTIAL_MAPPINGS,
    ids=[case for case, _, _ in NUMERIC_CREDENTIAL_MAPPINGS],
)
def test_a_numeric_credential_in_a_mapping_never_reaches_the_signature(
    case: str, diagnostic: object, sentinel: str
) -> None:
    """The same shapes as a mapping, and none of them survives.

    A mapping is redacted before it is flattened into text, so a value that
    is made of digits only goes the way every other value goes.
    """
    signature = failure_signature("kimi", diagnostic)  # type: ignore[arg-type]

    assert sentinel not in signature.normalized_text, case
    assert sentinel not in _serialised(signature), case


def test_a_token_count_survives_the_redaction_and_is_not_a_credential() -> None:
    """The count is not a secret, so the redaction leaves it alone.

    The count does not reach the reduced text as it is: the reduction takes
    every number that no status code names away, which is what lets two
    occurrences of one failure -- with two different token counts -- share
    one signature. What the reduction keeps is the key, and the key says
    "count" and not "credential".
    """
    text = TOKEN_COUNT_VECTOR[1]

    assert redact_credentials(text) == text

    signature = failure_signature("kimi", text)

    assert "<token>" not in signature.normalized_text
    assert "max_tokens=" in signature.normalized_text
    assert TOKEN_COUNT_VECTOR[2] not in signature.normalized_text


def test_a_bare_token_count_does_not_survive_the_redaction() -> None:
    """A bare "token" is a credential key, so its number goes with it.

    The Planner's ruling: a bare "token=131072" cannot be told apart from a
    credential, so it is redacted like one. The redaction is where the
    digits are still visible -- the reduction would take them away anyway.
    """
    text = BARE_TOKEN_COUNT_VECTOR[1]

    redacted = redact_credentials(text)

    assert redacted == "token=[redacted]"
    assert BARE_TOKEN_COUNT_VECTOR[2] not in redacted

    signature = failure_signature("kimi", text)

    assert "<token>" in signature.normalized_text
    assert BARE_TOKEN_COUNT_VECTOR[2] not in signature.normalized_text
    assert BARE_TOKEN_COUNT_VECTOR[2] not in _serialised(signature)


@pytest.mark.parametrize("key", COUNT_KEY_NAMES)
def test_a_count_key_keeps_its_number(key: str) -> None:
    """A number under one of the seven count keys stays.

    The exemption is keyed on the name of the key, and the name is judged
    whole: "max_tokens" is a count key and "max_tokens_extra" is not one.
    """
    text = f"{key}=131072"

    assert redact_credentials(text) == text
    assert redact_credentials({key: 131072}) == {key: 131072}

    signature = failure_signature("kimi", text)

    assert "<token>" not in signature.normalized_text
    assert "131072" not in signature.normalized_text


@pytest.mark.parametrize(
    "key",
    (
        "api_key",
        "password",
        "secret",
        "cookie",
        "session_key",
        "access_key",
        "account_id",
        "bearer",
        "token",
    ),
)
def test_a_number_under_any_other_key_goes(key: str) -> None:
    """A number under any other key is a value, and a value goes.

    The exemption belongs to the seven count keys, so the very same number
    is a count under one key and a credential under another.
    """
    text = f"{key}=131072"

    redacted = redact_credentials(text)

    assert redacted == f"{key}=[redacted]"
    assert "131072" not in redacted

    assert redact_credentials({key: 131072}) == {key: "[redacted]"}


def test_a_pair_that_names_no_credential_is_left_as_it_is() -> None:
    """A pair that names no credential is not a match, and it stays.

    The bare rule reads the whole key, so a pair like "status=200" is
    looked at and left alone: the text around it has to come out of the
    redaction byte for byte.
    """
    for text in (
        "status=200",
        "error=failed",
        "request_id=req-8842",
        "the turn used 131072 of them",
    ):
        assert redact_credentials(text) == text


def test_a_value_that_is_already_the_marker_is_left_alone() -> None:
    # The account rule must not take the marker out a second time. The
    # marker's own text runs up to the next space or quote, so redacting it
    # again would swallow whatever came after it -- the closing brace of
    # the JSON here. The text has to stay a faithful rendering of the
    # input, with the values taken out and nothing else touched.
    text = (
        '{"error":"request failed","api_key":"OPAQUE_SENTINEL_SECRET",'
        '"account_id":"customerAlpha"}'
    )

    redacted = redact_credentials(text)
    signature = failure_signature("kimi", text)

    assert redacted == (
        '{"error":"request failed","api_key":[redacted],"account_id":[redacted]}'
    )
    assert signature.normalized_text == '{"error":"request failed",<token>,<account>}'
    assert "opaque_sentinel_secret" not in signature.normalized_text.lower()
    assert "customeralpha" not in signature.normalized_text.lower()


def test_a_key_that_merely_contains_authorization_is_a_credential() -> None:
    # The Authorization header owns its own rule, so the bare rule leaves it
    # alone. "Owns" means the key ENDS with the word: a key that merely
    # contains it -- "authorization_token", "authorization_code" -- names a
    # credential of its own. The two rules have to agree on that, or one of
    # them leaves the secret behind.
    for text in (
        "authorization_token=opaque_sentinel_secret",
        "authorization_code=opaque_sentinel_secret",
        "proxy_authorization=opaque_sentinel_secret",
        "x_authorization=opaque_sentinel_secret",
    ):
        signature = failure_signature("kimi", text)

        assert "opaque_sentinel_secret" not in signature.normalized_text, text

    # And the header itself is still the header's business.
    for text in (
        "Authorization: Bearer opaque_sentinel_secret",
        "X-Authorization: Bearer opaque_sentinel_secret",
        "x_authorization: Bearer opaque_sentinel_secret",
    ):
        signature = failure_signature("kimi", text)

        assert "opaque_sentinel_secret" not in signature.normalized_text, text


# ----------------------------------------------------------------------
# the fourth fix round: one rule for the key of a credential
#
# A key that names a credential names it wherever it appears: quoted or
# not, in front of a string, a bare word or a number. The seven names
# that count tokens keep their number, and a pair that names no
# credential is left exactly as it was.
# ----------------------------------------------------------------------

# A secret made of letters: the number sweep of the reduction cannot
# hide a leak of it, so what reaches the signature is a real leak.
OPAQUE_KEY_SENTINEL = "OPAQUE_SENTINEL_SECRET"

# The forms a key and its value take in a text. Every one of them names a
# credential, and every one of them carries the sentinel. The value is a
# number in most of them: a credential whose value happens to be made of
# digits is still a credential, and it goes like any other one.
CREDENTIAL_KEY_VECTORS = (
    ("quoted key, bare number", '{"api_key": 9876543214}', "9876543214"),
    ("quoted key, quoted number", '{"password": "9876543216"}', "9876543216"),
    ("quoted key, bare word", '{"token": 9876543219}', "9876543219"),
    ("prefixed quoted key", '{"my_api_key": "9876543215"}', "9876543215"),
    ("authorization code", '{"authorization_code": "9876543217"}', "9876543217"),
    ("single quoted key", "{'api_key': '9876543218'}", "9876543218"),
    (
        "escaped quote in the value",
        '{"api_key": "opaque\\"9876543221"}',
        "9876543221",
    ),
    ("bare key, bare number", "api_key=9876543222", "9876543222"),
    ("prefixed bare key", "my_api_key=9876543223", "9876543223"),
    ("value up to a delimiter", "api_key=9876543224; status=200", "9876543224"),
)

# The same rule, with a secret the reduction cannot hide: the digit
# strings above would be taken away by the number sweep even when the
# redaction let them through.
OPAQUE_KEY_VECTORS = (
    (
        "prefixed quoted key",
        '{"my_api_key": "' + OPAQUE_KEY_SENTINEL + '"}',
        OPAQUE_KEY_SENTINEL,
    ),
    (
        "authorization code",
        '{"authorization_code": "' + OPAQUE_KEY_SENTINEL + '"}',
        OPAQUE_KEY_SENTINEL,
    ),
    (
        "private key",
        '{"private_key": "' + OPAQUE_KEY_SENTINEL + '"}',
        OPAQUE_KEY_SENTINEL,
    ),
    (
        "auth code",
        '{"auth_code": "' + OPAQUE_KEY_SENTINEL + '"}',
        OPAQUE_KEY_SENTINEL,
    ),
)


@pytest.mark.parametrize(
    ("case", "text", "sentinel"),
    CREDENTIAL_KEY_VECTORS,
    ids=[case for case, _, _ in CREDENTIAL_KEY_VECTORS],
)
def test_a_credential_under_a_key_is_redacted(case: str, text: str, sentinel: str) -> None:
    """A key that names a credential names it in every form it takes.

    The key may be quoted, the value may be a string, a bare word or a
    number, and the value may run up to a delimiter. What goes is the
    value, and only the value: the key stays, so the text still says
    what the value was.
    """
    redacted = redact_credentials(text)

    assert sentinel not in redacted, case
    assert "[redacted]" in redacted, case


@pytest.mark.parametrize(
    ("case", "text", "sentinel"),
    CREDENTIAL_KEY_VECTORS,
    ids=[case for case, _, _ in CREDENTIAL_KEY_VECTORS],
)
def test_a_redacted_credential_leaves_the_text_around_it(
    case: str, text: str, sentinel: str
) -> None:
    """The value goes, and the text around it comes out as it went in.

    A value that swallows the closing brace of the object it sits in
    leaves a text that is not JSON any more, and a caller that reads the
    diagnostic as JSON reads nothing at all.
    """
    redacted = redact_credentials(text)

    assert sentinel not in redacted, case
    if text.startswith("{"):
        assert redacted.endswith("}"), case
        assert redacted.count("{") == redacted.count("}"), case


@pytest.mark.parametrize(
    ("case", "text", "sentinel"),
    OPAQUE_KEY_VECTORS,
    ids=[case for case, _, _ in OPAQUE_KEY_VECTORS],
)
def test_a_credential_under_a_key_does_not_reach_the_signature(
    case: str, text: str, sentinel: str
) -> None:
    """The reduced text holds no credential, whatever the key is called.

    The sentinel is made of letters on purpose: the reduction takes every
    number away, so a digit string would be hidden by the reduction even
    when the redaction let it through.
    """
    signature = failure_signature("kimi", text)

    assert sentinel not in signature.normalized_text, case
    assert sentinel not in _serialised(signature), case
    assert "<token>" in signature.normalized_text, case


def test_a_credential_under_a_quoted_key_is_a_token_in_the_signature() -> None:
    """The reduction names the credential a token, and not a number.

    The reduction takes every number away, so a digit string does not
    show a leak on its own. What shows it is the word in front of it: a
    credential is a <token>, and a number is not.
    """
    for text in ('{"api_key": 9876543214}', '{"password": 9876543216}'):
        signature = failure_signature("kimi", text)

        assert "<token>" in signature.normalized_text, text


def test_the_count_keys_keep_their_number_and_the_other_keys_do_not() -> None:
    """The exemption belongs to the seven names, and to nothing else.

    The name is judged whole: "max_tokens" is a count and "my_max_tokens"
    is not one of the seven, so the very same number goes under it. A
    bare "token" is not one of the seven either: a number under it cannot
    be told apart from a credential.
    """
    for key in COUNT_KEY_NAMES:
        text = f"{key}=131072"

        assert redact_credentials(text) == text, key
        assert redact_credentials({key: 131072}) == {key: 131072}, key

    # The quoted form is the same key, so it keeps its number too.
    assert redact_credentials('{"max_tokens": 131072}') == '{"max_tokens": 131072}'

    # A name that is not one of the seven is a credential key.
    assert redact_credentials("my_max_tokens=131072") == "my_max_tokens=[redacted]"
    assert redact_credentials('{"api_key": 131072}') == '{"api_key": [redacted]}'
    assert redact_credentials("token=131072") == "token=[redacted]"
    assert redact_credentials({"token": 131072}) == {"token": "[redacted]"}


def test_a_mapping_is_redacted_by_the_name_of_its_key() -> None:
    """A mapping is walked key by key, and the key names the value.

    The key is the only thing that names the value, so the name is
    judged the way a text judges it: a key that names a credential has
    its whole value taken out, whatever the value is.
    """
    assert redact_credentials({"api_key": 9876543214}) == {"api_key": "[redacted]"}
    assert redact_credentials({"password": 9876543216}) == {"password": "[redacted]"}
    assert redact_credentials({"my_api_key": OPAQUE_KEY_SENTINEL}) == {
        "my_api_key": "[redacted]"
    }
    assert redact_credentials({"private_key": OPAQUE_KEY_SENTINEL}) == {
        "private_key": "[redacted]"
    }
    assert redact_credentials({"auth_code": OPAQUE_KEY_SENTINEL}) == {
        "auth_code": "[redacted]"
    }

    # A value that is a mapping of its own is walked as well.
    assert redact_credentials({"outer": {"api_key": 9876543214}}) == {
        "outer": {"api_key": "[redacted]"}
    }
    # And so is a list of them, and a tuple of them.
    assert redact_credentials([{"api_key": 9876543214}]) == [{"api_key": "[redacted]"}]
    assert redact_credentials(({"api_key": 9876543214},)) == ({"api_key": "[redacted]"},)

    # The count keys keep their numbers here too.
    for key in COUNT_KEY_NAMES:
        assert redact_credentials({key: 131072}) == {key: 131072}, key


def test_a_credential_in_a_byte_payload_does_not_come_back_out() -> None:
    """A byte diagnostic is not a way around the redaction.

    The bytes are decoded, redacted and encoded again, so what the
    payload carries is gone by the time it leaves.
    """
    payload = b'{"api_key": 9876543214}'

    redacted = redact_credentials(payload)

    assert isinstance(redacted, bytes)
    assert b"9876543214" not in redacted
    assert b"[redacted]" in redacted


def test_a_pair_that_names_no_credential_stays_and_one_that_names_one_goes() -> None:
    """A pair that names no credential is not a match, and it stays.

    The rule reads the whole name of the key, so a pair like "status=200"
    is looked at and left alone: the text around it has to come out of
    the redaction byte for byte.
    """
    for text in (
        "status=200",
        "error=failed",
        "request_id=req-8842",
        "the turn used 131072 of them",
        '{"status": 200, "attempts": 3}',
    ):
        assert redact_credentials(text) == text

    # The same document with a credential in it: the credential goes, and
    # the rest of it stays as it was.
    assert redact_credentials('{"status": 200, "api_key": 131072}') == (
        '{"status": 200, "api_key": [redacted]}'
    )


def test_the_header_and_the_bearer_keep_their_scheme() -> None:
    """The Authorization header and a bearer token keep what names them.

    The scheme is not the secret: "Bearer" says what the value is, and
    the value is what goes.
    """
    assert redact_credentials("Authorization: Bearer 9876543230") == (
        "Authorization: Bearer [redacted]"
    )
    assert redact_credentials("Bearer 9876543231") == "Bearer [redacted]"
    assert redact_credentials("authorization=bearer 9876543232") == (
        "authorization=bearer [redacted]"
    )
    # The header may arrive as JSON, and then it is taken as it is.
    assert redact_credentials('{"Authorization": 9876543233}') == (
        '{"Authorization": [redacted]}'
    )


# ----------------------------------------------------------------------
# the fifth fix round: the reduction ends in a whitelist
#
# A blacklist can only name what it thought of. The review found two
# diagnostics whose credential walked around the redaction -- an escaped
# quote inside an Authorization value, and a value that starts with the
# redaction marker itself -- so the reduction no longer ends in rules
# that name what must go: it ends in a vocabulary that names what may
# stay. Nothing outside it can reach the text that is stored.
# ----------------------------------------------------------------------

# The sentinel the review's repros carry. It is made of letters, so no
# number sweep could hide a leak of it even if one wanted to.
AUDIT_MARKER = "auditmarker"

# The escaped-quote repro: an Authorization value whose credential
# continues after a quote the header rule stops at.
ESCAPED_QUOTE_AUTHORIZATION = json.dumps(
    {"error": "request failed", "Authorization": f'Basic prefix"{AUDIT_MARKER}'}
)

# The same header in the Digest shape, whose parameters are quoted: the
# header rule reads one value, and the parameters after it were left
# behind.
DIGEST_AUTHORIZATION = (
    "request failed Authorization: Digest username=\"x\", realm=\"y\", "
    f'nonce="{AUDIT_MARKER}"'
)

# A value that starts with the redaction marker. The redaction sees the
# marker and passes the pair through; the suffix used to walk into the
# reduced text behind it.
MARKER_PREFIXED_VALUE = f"api_key=[redacted]{AUDIT_MARKER}"

# Diagnostics that carry secrets in the shapes the review named. Whatever
# a diagnostic carries, the words that reach the reduced text have to be
# the words the vocabulary names, a status code, or nothing at all.
LEAKY_DIAGNOSTICS = (
    ESCAPED_QUOTE_AUTHORIZATION,
    DIGEST_AUTHORIZATION,
    MARKER_PREFIXED_VALUE,
    json.dumps(
        {"error": "request failed", "api_key": f'prefix"{OPAQUE_SECRET}'}
    ),
    "Authorization: Basic alphabeticopaquecredentials",
    f"the call failed with key {SENTINEL} at /Users/someone/private/state.json",
    "account_id=acct_998877 failed after 131072 tokens at 2026-11-06T12:19:00Z",
    FIRST_QUOTA,
    UNKNOWN_TEXT,
)


def test_an_escaped_quote_in_an_authorization_value_leaks_nothing() -> None:
    """The part of the value the header rule stops short of cannot survive.

    The escaped quote used to end the Authorization value early and leave
    the rest of the credential in the reduced text. What stops it now is
    not another rule about quotes: the word the vocabulary does not name
    cannot reach the text at all.
    """
    signature = failure_signature("kimi", ESCAPED_QUOTE_AUTHORIZATION)

    assert AUDIT_MARKER not in signature.normalized_text
    assert AUDIT_MARKER not in _serialised(signature)


def test_a_digest_authorization_with_quoted_parameters_leaks_nothing() -> None:
    """A header with quoted parameters keeps no parameter it did not read.

    The Authorization rule reads one value, so the quoted parameters after
    it were left in the text. None of them survives the whitelist.
    """
    signature = failure_signature("kimi", DIGEST_AUTHORIZATION)

    assert AUDIT_MARKER not in signature.normalized_text
    assert AUDIT_MARKER not in _serialised(signature)


def test_a_value_starting_with_the_marker_leaks_nothing() -> None:
    """A value that starts with the redaction marker is not clean.

    As a text, as bytes and as a mapping value: the suffix after the
    marker is taken wherever the diagnostic arrives from, because the
    whitelist names no word of it.
    """
    for payload in (
        MARKER_PREFIXED_VALUE,
        MARKER_PREFIXED_VALUE.encode("utf-8"),
        {"message": MARKER_PREFIXED_VALUE},
        {"api_key": f"[redacted]{AUDIT_MARKER}"},
    ):
        signature = failure_signature("kimi", payload)

        assert AUDIT_MARKER not in signature.normalized_text, payload
        assert AUDIT_MARKER not in _serialised(signature), payload


def test_a_numeric_heavy_diagnostic_keeps_only_status_codes() -> None:
    """Only a three-digit status code survives as digits.

    Every other number is a count or an identifier, and the sweep takes
    them; what the whitelist is left with is the one code the status pass
    protected.
    """
    text = (
        "request 88421 failed at 2026-11-06T12:19:00Z: status 429, "
        "tokens 131072, port 443, cost 0.25, retries 3"
    )
    signature = failure_signature("kimi", text)

    assert re.findall(r"\d+", signature.normalized_text) == ["429"]


def test_same_class_errors_differing_only_in_ids_and_paths_share_a_signature() -> None:
    """The parts that differ between two occurrences go; the class stays.

    Two calls, one assert: request ids, counts and log paths are taken out
    before the hash, so the same failure shares a signature whatever it
    was called.
    """
    first = failure_signature(
        "kimi",
        "HTTP 429 Too Many Requests (request_id=req-8842, at /Users/one/e5.log)",
    )
    second = failure_signature(
        "kimi",
        "HTTP 429 Too Many Requests (request_id=req-9931, at /var/log/two.log)",
    )

    assert first.signature_id == second.signature_id


def test_the_normalized_text_holds_only_whitelisted_tokens() -> None:
    """Every word in a reduced text is a vocabulary word or a status code.

    This is the invariant the whitelist promises, checked over a battery
    of leaky diagnostics: whatever the redaction in front of it missed,
    the words that reach the text are the words the vocabulary names, a
    three-digit code in the status range, or nothing at all. The ``x`` of
    the ``<x>`` placeholder is the placeholder's own, not a survivor.
    """
    for text in LEAKY_DIAGNOSTICS:
        signature = failure_signature("kimi", text)
        stripped = signature.normalized_text.replace("<x>", "")
        for token in re.findall(r"\w+", stripped):
            assert (
                token in _SIGNATURE_VOCAB
                or (token.isdigit() and len(token) == 3 and 100 <= int(token) <= 599)
            ), (text, token)


# A status code is three ASCII digits. A superscript or a circled number
# looks like digits to ``str.isdigit()`` -- and the ``int()`` that used to
# follow it raises on them -- so the rule is "ASCII digits in the status
# range", and the two ways of reading a number are not used at all.
UNICODE_CODE_DIAGNOSTICS = ("error ²²²", "error ①②③")


@pytest.mark.parametrize("text", UNICODE_CODE_DIAGNOSTICS)
def test_a_unicode_digit_is_not_a_status_code(text: str) -> None:
    """The signature of a diagnostic whose code is not ASCII digits.

    The code must not raise, and it must end up as the placeholder: a
    number that is not an ASCII status code is not a status code, and it is
    not anything else either.
    """
    signature = failure_signature("kimi", text)

    assert text.split()[-1] not in signature.normalized_text
    assert "<x>" in signature.normalized_text


@pytest.mark.parametrize("text", UNICODE_CODE_DIAGNOSTICS)
def test_a_unicode_digit_does_not_break_the_classification(text: str) -> None:
    """The classification of a diagnostic whose code is not ASCII digits.

    The classification reads the same reduced text, so it must not raise
    either, and it must answer with the class it knows: nothing in the text
    names a class.
    """
    result = classify_failure("kimi", text, ruleset=RuleSet(()))

    assert result.failure_class is FailureClass.UNKNOWN
    assert result.reason_code == "worker_unknown"


# One scenario per class, and the tokens that name the class. The
# whitelist may placeholder the prose around them; these have to stay,
# because they are what tells one class from another.
CLASS_TOKEN_SCENARIOS = (
    ("rate limited", "HTTP 429: rate limit exceeded",
     ("http", "429", "rate", "limit", "exceeded")),
    ("quota exhausted", "insufficient_quota: credit balance is too low",
     ("insufficient_quota", "credit", "balance", "too", "low")),
    ("context exhausted", "maximum context length exceeded",
     ("maximum", "context", "length", "exceeded")),
    ("auth failed", "HTTP 401 unauthorized", ("http", "401", "unauthorized")),
    ("overloaded", "HTTP 503 service unavailable",
     ("http", "503", "service", "unavailable")),
    ("network", "connection refused", ("connection", "refused")),
    ("network error name", "connect ECONNREFUSED", ("connect", "econnrefused")),
)


@pytest.mark.parametrize(
    ("case", "text", "tokens"),
    CLASS_TOKEN_SCENARIOS,
    ids=[case for case, _, _ in CLASS_TOKEN_SCENARIOS],
)
def test_class_relevant_tokens_survive_the_whitelist(
    case: str, text: str, tokens: tuple[str, ...]
) -> None:
    """A signature still says what class it is about.

    The placeholders may take the prose; the words that decide the class
    are vocabulary words, and the status code is the one number the
    reduction keeps.
    """
    signature = failure_signature("kimi", text)

    for token in tokens:
        assert token in signature.normalized_text, (case, token)


def test_a_diagnostic_of_no_vocabulary_words_reduces_to_placeholders() -> None:
    """Prose the vocabulary does not name cannot reach the text at all.

    The one word that stays here is "out", which the network rules say,
    too. Every other word of the sentence is a ``<x>``, and no word of the
    sentence itself is left.
    """
    signature = failure_signature("kimi", UNKNOWN_TEXT)

    assert signature.normalized_text == "<x> <x> <x> <x> out <x> <x>"
    assert "widget" not in signature.normalized_text
    assert "registry" not in signature.normalized_text
    assert "alignment" not in signature.normalized_text


# ----------------------------------------------------------------------
# the fifth fix round: the legacy spaced keys
#
# The compound key names used to be written with a space where the
# underscore went. _CRED_KEY reads them again, and a rule of their own
# carries them in a text, where the pair rule's key candidate cannot
# cross a space.
# ----------------------------------------------------------------------

SPACED_KEY_VECTORS = (
    ("api key", "api key", "api_key"),
    ("access key", "access key", "access_key"),
    ("auth code", "auth code", "auth_code"),
    ("session key", "session key", "session_key"),
    ("account id", "account id", "account_id"),
    ("private key", "private key", "private_key"),
    ("client secret", "client secret", "client_secret"),
)


@pytest.mark.parametrize(
    ("case", "spaced", "underscored"),
    SPACED_KEY_VECTORS,
    ids=[case for case, _, _ in SPACED_KEY_VECTORS],
)
def test_a_space_separated_legacy_key_is_a_credential(
    case: str, spaced: str, underscored: str
) -> None:
    """The spaced spelling of a key redacts like the underscored one.

    In a mapping, where the key arrives whole, and in a text, where the
    pair rule cannot carry the space: the value goes either way.
    """
    fake = "fakecredential"

    assert redact_credentials({spaced: fake}) == {spaced: "[redacted]"}, case
    assert redact_credentials(f"{spaced}={fake}") == f"{spaced}=[redacted]", case
    # The underscored spelling never stopped being a credential.
    assert redact_credentials(f"{underscored}={fake}") == (
        f"{underscored}=[redacted]"
    ), case


def test_the_credential_word_stays_in_the_vocabulary() -> None:
    """"credential" is one of the oldest names a secret goes by.

    It is in the credential-key rule on purpose, and it stays there: a key
    called "credential" is redacted in a mapping and in a text.
    """
    assert redact_credentials({"credential": OPAQUE_SECRET}) == {
        "credential": "[redacted]"
    }
    assert redact_credentials(f"credential={OPAQUE_SECRET}") == "credential=[redacted]"
    assert OPAQUE_SECRET not in redact_credentials({"credential": OPAQUE_SECRET})
