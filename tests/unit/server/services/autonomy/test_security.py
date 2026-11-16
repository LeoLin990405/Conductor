from src.server.services.autonomy.security import (
    redact_external_data,
    redact_external_text,
)


def test_redacts_known_github_secret_and_common_token_shapes():
    token = "ghp_" + "a" * 36
    text = f"echo {token}; Authorization: Bearer abcdefghijklmnop"
    result = redact_external_text(text, secrets=(token,))
    assert token not in result
    assert "abcdefghijklmnop" not in result
    assert result.count("[REDACTED]") == 2


def test_redacts_named_secrets_without_changing_unrelated_text():
    text = "please review this diff; api_key=abcdef123456"
    assert redact_external_text(text) == "please review this diff; api_key=[REDACTED]"


def test_short_known_secret_is_redacted_as_a_literal():
    assert redact_external_text("key=abc", secrets=("abc",)) == "key=[REDACTED]"


def test_recursive_redaction_scrubs_dictionary_keys_and_values():
    token = "private-token-value"
    result = redact_external_data(
        {f"field-{token}": f"echo {token}"}, secrets=(token,)
    )
    assert result == {"field-[REDACTED]": "echo [REDACTED]"}


def test_redaction_runs_before_output_truncation():
    token = "ghp_" + "c" * 36
    result = redact_external_text("x" * 990 + token)[:1000]
    assert token not in result
