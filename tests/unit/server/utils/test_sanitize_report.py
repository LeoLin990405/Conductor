import json
import itertools
import time

import pytest

from src.server.utils.sanitize import REDACTED, sanitize_output_text, sanitize_report_text
from src.server.database.autonomy.tickets import _report_body


def test_report_prose_preserves_session_false_positive_and_redacts_tokens() -> None:
    assert sanitize_report_text("Fixed the session: handling") == "Fixed the session: handling"
    assert sanitize_report_text("Fixed the session: parsing") == "Fixed the session: parsing"
    assert sanitize_report_text("Fixed the session: processing") == "Fixed the session: processing"
    assert sanitize_report_text("session: abcdef1234567890") == REDACTED
    assert sanitize_output_text("Fixed the session: parsing", limit=0) == "Fixed the [REDACTED]"
    body = "Sentinel token=ghp_" "123456789012345678901234567890\n普通正文"
    assert sanitize_report_text(body) == f"Sentinel {REDACTED}\n普通正文"


def test_clean_report_json_is_returned_byte_for_byte() -> None:
    value = '{ "session_id": "abc123", "token_count": 123, "status": "ready" }'
    assert sanitize_report_text(value) == value


def test_report_json_redacts_sensitive_values_and_preserves_structure() -> None:
    value = json.dumps(
        {
            "session_id": "abc123",
            "token_count": 123,
            "author": "Leo",
            "auth_status": "failed",
            "api_key": "x",
            "clientSecret": "syntheticcredential123456",
            "access_token": "x",
            "bearer": "short",
            "otp": 123456,
            "message": "sk-" "ant-api03-ABCDEFGHIJKLMNOPQRSTUV",
            "cookie": "sid=abcdef1234567890; Path=/; HttpOnly",
        }
    )
    cleaned = sanitize_report_text(value)
    result = json.loads(cleaned)
    assert result["session_id"] == "abc123"
    assert result["token_count"] == 123
    assert result["author"] == "Leo"
    assert result["auth_status"] == "failed"
    for key in ("api_key", "clientSecret", "access_token", "bearer", "otp", "cookie", "message"):
        assert result[key] == REDACTED
    assert "syntheticcredential123456" not in cleaned
    assert "ABCDEFGHIJKLMNOPQRSTUV" not in cleaned
    assert "abcdef1234567890" not in cleaned


def test_nested_json_and_embedded_named_credentials_are_scanned() -> None:
    value = json.dumps(
        {
            "nested": {
                "note": "abc= api_key=syntheticcredential123456",
                "token": "abc= token=syntheticcredential123456",
                "password": {"current": "secret-value"},
            }
        }
    )
    result = json.loads(sanitize_report_text(value))
    nested = result["nested"]
    assert "syntheticcredential123456" not in json.dumps(nested)
    assert nested["password"]["current"] == REDACTED


def test_nested_credential_candidates_after_benign_fields_are_not_skipped() -> None:
    api_key = sanitize_report_text("abc= api_key=syntheticcredential123456")
    token = sanitize_report_text("abc= token=syntheticcredential123456")
    assert "syntheticcredential123456" not in api_key
    assert "syntheticcredential123456" not in token


def test_report_strict_cookie_and_authorization_patterns_keep_header_context() -> None:
    auth = sanitize_report_text("Authorization: Bearer abcdef1234567890")
    cookie = sanitize_report_text(
        "Cookie: sid=abcdef1234567890; other=ghijklmnopqrst; third=stuvwxyz012345"
    )
    set_cookie = sanitize_report_text("Set-Cookie: sid=abcdef1234567890; Path=/; HttpOnly")
    short_cookie = sanitize_report_text("Cookie: a=b; session=longsecret")
    short_basic = sanitize_report_text("Authorization: Basic longbase64")
    for secret in (
        "abcdef1234567890",
        "ghijklmnopqrst",
        "stuvwxyz012345",
    ):
        assert secret not in cookie
    assert "abcdef1234567890" not in auth
    assert "abcdef1234567890" not in set_cookie
    assert "longsecret" not in short_cookie
    assert "longbase64" not in short_basic


def test_report_plaintext_handles_authors_metadata_and_prefixed_credentials() -> None:
    assert sanitize_report_text("author=Alice") == "author=Alice"
    assert sanitize_report_text("auth_status=failed") == "auth_status=failed"
    for label in ("INPUT_API_KEY", "OUTPUT_AUTH_TOKEN", "PROMPT_SECRET", "INPUT_access_token"):
        assert sanitize_report_text(f"{label}=sentinel") == REDACTED


def test_short_author_metadata_is_narrow_and_long_values_remain_strict() -> None:
    assert sanitize_report_text("author=Alice") == "author=Alice"
    secret = "Zq7pLm2Xv9Rt4Wk8"
    assert secret not in sanitize_report_text(f"author={secret}")
    assert json.loads(sanitize_report_text(json.dumps({"author": "Leo"}))) == {
        "author": "Leo"
    }
    assert json.loads(sanitize_report_text(json.dumps({"author": secret}))) == {
        "author": REDACTED
    }


def test_report_only_restores_explicit_session_id_example() -> None:
    assert json.loads(sanitize_report_text('{"session_id":"abc123"}')) == {
        "session_id": "abc123"
    }
    for secret in ("hunter2", "s3cr3t"):
        cleaned = sanitize_report_text(json.dumps({"session_id": secret}))
        assert secret not in cleaned


def test_json_token_count_allowlist_matches_plain_ten_digit_limit() -> None:
    cases = [
        (1234567890, 1234567890),
        (12345678901, REDACTED),
        (-1, REDACTED),
        (123.0, REDACTED),
        (True, True),
    ]
    for value, expected in cases:
        cleaned = sanitize_report_text(json.dumps({"token_count": value}))
        assert json.loads(cleaned)["token_count"] == expected


def test_generated_credential_labels_never_leak_when_strict_output_redacts() -> None:
    import itertools

    prefixes = ["", "AWS", "GitHub", "client", "x"]
    suffixes = ["token", "credentials", "passwords", "private_key", "api_key"]
    values = [
        "Zq7pLm2Xv9Rt4Wk8",
        "Zq7pLm2Xv9Rt4Wk8Yb3N",
        "Zq7pLm2Xv9Rt4Wk8Yb3NZq7pLm2Xv9Rt4Wk8Yb3N",
        "c2VjcmV0cGFzc3dvcmQxMjM0NQ==",
    ]
    labels = set()
    for prefix, separator, suffix in itertools.product(
        prefixes, ["_", "-", "."], suffixes
    ):
        raw = f"{prefix}{separator if prefix else ''}{suffix}"
        labels.update(
            {
                raw,
                raw.upper(),
                raw.lower(),
                "".join(part.title() for part in raw.replace("-", "_").replace(".", "_").split("_")),
                raw.replace("_", "").replace("-", "").replace(".", ""),
            }
        )

    for label, secret in itertools.product(sorted(labels), values):
        bodies = (
            f"{label}={secret}",
            f"{label}: {secret}",
            f"\x1b[1m{label}\x1b[0m: {secret}",
            f"{label[:2]}\x00{label[2:]}: {secret}",
            json.dumps({label: secret}),
            "\ufeff" + json.dumps({label: secret}),
            f"```json\n{json.dumps({label: secret})}\n```",
        )
        for body in bodies:
            strict = sanitize_output_text(body, limit=0)
            if secret not in strict:
                assert secret not in sanitize_report_text(body), (label, body)


def test_provider_credential_inside_restored_auth_family_labels_stays_redacted() -> None:
    provider = "sk-proj-" + "abcd1234" * 4
    suffixes = [
        "_auth", "_authorization", "_bearer", "_cookie", "_set-cookie", ".auth", "-auth"
    ]
    tails = ["", " ", ' \"\"', " ''", " ordinary", " Bearer ", "\n", "\nnext: ordinary"]
    bodies = [
        provider + suffix + ":" + tail
        for suffix, tail in itertools.product(suffixes, tails)
    ]
    bodies.extend(
        [
            '\x1b[1m' + provider + "_auth\x1b[0m: \"\"",
            'payload {"' + provider + '_auth":""} done',
            json.dumps({provider + "_auth": ""}),
        ]
    )
    for body in bodies:
        strict = sanitize_output_text(body, limit=0)
        report = sanitize_report_text(body)
        if provider not in strict:
            assert provider not in report, body


def test_planner_class_templates_have_no_report_or_storage_leaks() -> None:
    secret = "Zq7pLm2Xv9Rt4Wk8"
    class_a_labels = [
        "Authorization", "auth", "X-Auth", "auth_status", "Cookie", "Set-Cookie",
        "bearer", "cookie", "Proxy-Authorization", "x-auth-token", "auth_token",
    ]
    class_a_separators = [":\n", ": \n", "=\n", " = \n", ":\r\n", ":\n\n"]
    class_b_labels = [
        "password", "api_key", "token", "session", "sid", "secret", "Authorization",
        "x-session", "client_secret",
    ]
    templates = [
        f"{label}{separator}{secret}"
        for label, separator in itertools.product(class_a_labels, class_a_separators)
    ]
    templates.extend(
        f"{label}{separator}{scheme}\n{secret}"
        for label, scheme, separator in itertools.product(
            class_b_labels, ["Bearer", "basic", "bearer"], [": ", "="]
        )
    )
    templates.extend(
        ["headers:\n  X-Auth:\n  " + secret, "Cookie:\n" + secret + "\nnext: 1"]
    )
    assert len(templates) == 122
    strict_hidden = strict_kept = 0
    for body in templates:
        for candidate in (body, json.dumps({"note": body})):
            strict = sanitize_output_text(candidate, limit=0)
            if secret not in strict:
                strict_hidden += 1
            else:
                strict_kept += 1
            assert secret not in sanitize_report_text(candidate), candidate
            assert secret not in (_report_body(candidate) or ""), candidate
    assert (strict_hidden, strict_kept) == (237, 7)


def test_fuzz_samples_keep_multiline_credentials_under_strict_redaction() -> None:
    samples = [
        ("Authorization:\nZq7pLm2Xv9Rt4Wk8", "Zq7pLm2Xv9Rt4Wk8"),
        ("token: Bearer\nSECRETx1Q", "SECRETx1Q"),
        ("password=basic\nsk-proj-A1b2C3d4E5f6G7h8I9j0K1", "sk-" "proj-A1b2C3d4E5f6G7h8I9j0K1"),
        ('{"note":"headers:\\nCookie:\\nhunter2pw"}', "hunter2pw"),
    ]
    for body, secret in samples:
        assert secret not in sanitize_report_text(body)
        assert secret not in (_report_body(body) or "")


def test_traced_strict_output_matches_existing_output_scanner() -> None:
    from src.server.utils.sanitize import (
        _strict_scan_with_source_spans,
        _strip_output_controls,
    )

    values = [
        "Authorization:\nZq7pLm2Xv9Rt4Wk8",
        json.dumps({"note": "Authorization: Basic\ns3cr3t"}),
        json.dumps(["-----BEGIN " "PRIVATE KEY-----", "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=", "-----END PRIVATE KEY-----"]),
        "\x1b[1mCookie\x1b[0m: sid=abc123; Path=/",
        '{"api_key":"secret","author":"Leo"}',
        "token_count: 1234567890123",
        "-----BEGIN " "PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n-----END PRIVATE KEY----------BEGIN " "PRIVATE KEY-----\nQUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo=\n-----END PRIVATE KEY-----",
        "-----BEGIN " "PRIVATE KEY-----\n-----BEGIN " "ENCRYPTED PRIVATE KEY-----\nCCCC\n-----END ENCRYPTED PRIVATE KEY-----\n-----END PRIVATE KEY-----",
    ]
    for original in values:
        cleaned_input = _strip_output_controls(original)
        length = len(cleaned_input)
        actual, _, _ = _strict_scan_with_source_spans(
            cleaned_input, list(range(length)), list(range(1, length + 1)), length
        )
        assert actual == sanitize_output_text(original, limit=0)


def test_json_pem_spans_redact_credentials_in_object_keys() -> None:
    body = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVo="
    value = json.dumps(
        {
            "begin": "-----BEGIN " "PRIVATE KEY-----",
            body: 1,
            "end": "-----END PRIVATE KEY-----",
        },
        separators=(",", ":"),
    )
    strict = sanitize_output_text(value, limit=0)
    cleaned = sanitize_report_text(value)
    assert body not in strict
    assert body not in cleaned
    assert "-----BEGIN " "PRIVATE KEY-----" not in cleaned
    assert "-----END PRIVATE KEY-----" not in cleaned


def test_json_context_redaction_preserves_schema_keys_but_redacts_later_payloads() -> None:
    provider = "sk-proj-" + "abcd1234" * 4
    value = json.dumps(
        {
            "cookie": "sid=synthetic-cookie-secret; Path=/",
            "message": provider,
            provider: "later-key-payload",
        },
        separators=(",", ":"),
    )
    cleaned = sanitize_report_text(value)
    result = json.loads(cleaned)
    assert "cookie" in result
    assert "message" not in result  # raw Cookie matching consumes the line through this key
    assert "synthetic-cookie-secret" not in cleaned
    assert provider not in cleaned
    assert "[REDACTED]" in cleaned


def test_cookie_label_inside_json_value_keeps_strict_redaction_provenance() -> None:
    value = '{"basic":"cookie:"}'
    assert "cookie:" not in sanitize_output_text(value, limit=0)
    assert "cookie:" not in sanitize_report_text(value)


def test_report_plaintext_does_not_restore_credential_shaped_or_email_labels() -> None:
    token = "sk-proj-" + "abcd1234" * 4
    assert token not in sanitize_report_text(f"{token}: demo")
    assert "person@example.com" not in sanitize_report_text("person@example.com: demo")


@pytest.mark.parametrize("closer", ["]", "}"])
def test_benign_token_word_does_not_shield_closing_json_punctuation(closer: str) -> None:
    value = f"token=handling{closer}Zq7pLm2Xv9Rt4Wk8"
    assert "Zq7pLm2Xv9Rt4Wk8" not in sanitize_output_text(value, limit=0)
    assert "Zq7pLm2Xv9Rt4Wk8" not in sanitize_report_text(value)


@pytest.mark.parametrize("scheme", ["Bearer", "Basic"])
@pytest.mark.parametrize("separator", ["+", "/", "="])
def test_report_metadata_name_after_token_delimiter_does_not_shield_secret(
    scheme: str, separator: str
) -> None:
    value = f"{scheme} abc{separator}token_count=42"
    strict_value = sanitize_output_text(value, limit=0)

    assert strict_value == REDACTED
    assert sanitize_report_text(value) == strict_value

    encoded = json.dumps({"note": value})
    strict_json = json.loads(sanitize_output_text(encoded, limit=0))
    assert json.loads(sanitize_report_text(encoded)) == strict_json == {
        "note": REDACTED
    }


def test_report_metadata_token_count_integer_remains_allowlisted_with_redacted_note() -> None:
    value = '{"token_count":42,"note":"Bearer abc/token_count=42"}'

    cleaned = json.loads(sanitize_report_text(value))

    assert cleaned == {"token_count": 42, "note": REDACTED}
    benign = '{"token_count":42,"note":"ok"}'
    assert sanitize_report_text(benign) == benign


@pytest.mark.parametrize("length", range(1, 17))
@pytest.mark.parametrize(
    ("form", "header"),
    [
        ("basic", "Authorization: Basic {value}"),
        ("basic_lower", "authorization: basic {value}"),
        ("bearer", "Authorization: Bearer {value}"),
        ("crlf", "Authorization: Bearer {value}"),
        ("lowercase", "authorization: bearer {value}"),
        ("no_scheme_space", "Authorization:Bearer {value}"),
        ("other_scheme", "Authorization: Other {value}"),
        ("tabs", "Authorization:\tBearer\t{value}"),
    ],
)
def test_report_auth_headers_redact_short_values_and_keep_following_header(
    length: int, form: str, header: str
) -> None:
    value = ("1234567890" * 2)[:length]
    cleaned = sanitize_report_text(f"{header.format(value=value)}\r\nX-Trace: safe")

    assert value not in cleaned, f"{form} leaked a {length}-character value"
    assert "X-Trace: safe" in cleaned


@pytest.mark.parametrize(
    "value",
    [
        "INFO Authorization: Bearer A1b2C3",
        "[debug] Authorization: Bearer A1b2C3",
        "Authorization=Bearer A1b2C3",
        "Authorization:\n Bearer A1b2C3",
        "Authorization:\r\n\tBearer A1b2C3",
        'INFO "Authorization": "Bearer A1b2C3"',
        "X-Authorization: Bearer A1b2C3",
        "AUTH_HEADER=Bearer A1b2C3",
        "proxy_authorization=Bearer A1b2C3",
        "auth: Bearer A1b2C3",
        "bearer: Bearer A1b2C3",
        "time=12 Authorization: Bearer A1b2C3",
        "auth_status=ready Authorization: Bearer A1b2C3",
        "abc= api_key=Bearer A1b2C3",
    ],
)
def test_report_auth_fields_with_prefixes_and_folding_redact_full_value(value: str) -> None:
    cleaned = sanitize_report_text(f"{value}\r\nX-Trace: safe")

    assert "A1b2C3" not in cleaned
    assert "X-Trace: safe" in cleaned


@pytest.mark.parametrize("label", ["token", "session"])
@pytest.mark.parametrize("scheme", ["Bearer", "Basic"])
@pytest.mark.parametrize("length", range(1, 9))
def test_report_ambiguous_fields_redact_explicit_auth_schemes(
    label: str, scheme: str, length: int
) -> None:
    credential = "A1b2C3D4"[:length]
    cleaned = sanitize_report_text(
        f"abc= {label}={scheme} {credential}\nX-Trace: safe"
    )

    assert f"{scheme} {credential}" not in cleaned
    assert "X-Trace: safe" in cleaned


def test_report_json_depth_limit_returns_valid_json_redaction() -> None:
    value = "{" + '"nested":{' * 70 + '"value":"safe"' + "}" * 70 + "}"
    assert json.loads(sanitize_report_text(value)) == REDACTED


@pytest.mark.parametrize(
    "label",
    ["TOKEN", "PRIVATE-TOKEN", "X-GITHUB-TOKEN", "id_token", "xsrf-token", "csrf_token", "sessionid", "sessionId", "session_id", "sid", "sessid", "sessId", "sess_id"],
)
def test_report_specific_credential_labels_are_redacted(label: str) -> None:
    secret = "SyntheticSecret123456"
    cleaned = sanitize_report_text(f"{label}: {secret}")
    assert secret not in cleaned
    assert sanitize_report_text("Fixed the session: handling") == "Fixed the session: handling"


def test_report_header_marker_classes_do_not_collide() -> None:
    value = "Authorization: Bearer tiny-secret\n" + "".join(
        f"field{index}: ok\n" for index in range(300)
    )
    cleaned = sanitize_report_text(value)
    assert cleaned.startswith(REDACTED + "\n")
    assert "field240: ok" in cleaned


@pytest.mark.parametrize("label", ["sessionId", "sessId", "session_id", "sess_id"])
def test_report_json_redacts_camel_and_delimited_session_id_keys(label: str) -> None:
    secret = "Zq7pLm2Xv9Rt4Wk8"
    cleaned = sanitize_report_text(json.dumps({label: secret}))
    assert secret not in cleaned


def test_report_duplicate_json_members_are_all_redacted_and_preserved() -> None:
    source = '{"note":"secret=first-secret","note":"secret=second-secret", "nested":{"api_key":"oldsecret","api_key":"newsecret"}}'
    cleaned = sanitize_report_text(source)
    assert "first-secret" not in cleaned and "second-secret" not in cleaned
    assert "oldsecret" not in cleaned and "newsecret" not in cleaned
    assert cleaned.count('"note"') == 1
    assert cleaned.count('"api_key"') == 1
    json.loads(cleaned)


@pytest.mark.parametrize(
    "body",
    [
        "ab: cd\n" * 30000,
        "a: b " * 40000,
        " password: x\n" * 22000,
        "token: Bearer x\n" * 22000,
    ],
)
def test_report_plaintext_sanitization_scales_for_large_headers(body: str) -> None:
    start = time.perf_counter()
    cleaned = sanitize_report_text(body)
    elapsed = time.perf_counter() - start
    assert len(cleaned) > 0
    assert elapsed < 3.0


def test_report_sanitizer_64_128_256k_scaling() -> None:
    durations = []
    for size in (64, 128, 256):
        body = ("a: b\n" * ((size * 1024) // 5 + 1))[: size * 1024]
        start = time.perf_counter()
        sanitize_report_text(body)
        durations.append(time.perf_counter() - start)
    assert durations[-1] < durations[0] * 5 + 0.5


def test_nested_pem_span_mapping_scales_at_64_128_256k() -> None:
    from src.server.utils.sanitize import _strict_scan_with_source_spans, _strip_output_controls

    largest = ""
    for size in (64, 128, 256):
        block = "-----BEGIN " "PRIVATE KEY-----\n" + ("A" * 512) + "\n-----END PRIVATE KEY-----\n"
        body = ("-----BEGIN " "PRIVATE KEY-----\n" + block * ((size * 1024) // len(block) + 1))[: size * 1024]
        largest = body
        clean = _strip_output_controls(body)
        started = time.perf_counter()
        traced, _, _ = _strict_scan_with_source_spans(
            clean, list(range(len(clean))), list(range(1, len(clean) + 1)), len(clean)
        )
        elapsed = time.perf_counter() - started
        assert traced == sanitize_output_text(body, limit=0)
        assert elapsed < 2.0
        started = time.perf_counter()
        sanitize_report_text(body)
        assert time.perf_counter() - started < 2.0

    started = time.perf_counter()
    stored = _report_body(largest)
    assert time.perf_counter() - started < 0.5
    assert stored is not None and len(stored.encode("utf-8")) <= 64 * 1024
