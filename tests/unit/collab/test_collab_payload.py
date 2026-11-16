"""Credential-shaped collaboration payloads are refused, never stored."""

from __future__ import annotations

import pytest

from src.collab import payload
from src.collab.types import SecretInPayloadError


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("sk-" + "a" * 30, "openai_like"),
        ("ghp_" + "A" * 30, "github_token"),
        ("github_pat_" + "A" * 30, "github_fine_grained"),
        ("AKIA" + "A" * 16, "aws_access_key"),
        ("AIza" + "a" * 35, "google_api_key"),
        ("xoxb-" + "A" * 12, "slack_token"),
        ("eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10, "jwt"),
        ("-----BEGIN " "RSA PRIVATE KEY-----", "private_key"),
        ("Bearer " + "a" * 20, "bearer_header"),
        ("api_key = 'abcd1234efgh'", "assigned_secret"),
    ],
)
def test_known_credential_shapes_are_found(text: str, kind: str) -> None:
    findings = payload.find_secrets(text)
    assert findings
    assert findings[0].kind == kind
    assert text not in str(findings[0])


def test_finding_reports_the_field_path_and_never_the_value():
    secret = "ghp_" + "Q" * 30
    findings = payload.find_secrets({"report": {"summary": secret}})
    assert [finding.path for finding in findings] == ["report.summary"]
    with pytest.raises(SecretInPayloadError) as caught:
        payload.assert_no_secrets({"body": secret})
    assert secret not in str(caught.value)
    assert caught.value.code == "secret_in_payload"
    assert caught.value.details["fields"] == ["body"]


def test_list_items_are_reported_with_their_index():
    findings = payload.find_secrets({"messages": [{"text": "ok"}, {"text": "sk-" + "z" * 30}]})
    assert [finding.path for finding in findings] == ["messages[1].text"]


def test_extra_secrets_are_checked_before_shape_patterns():
    marker = "ordinary vault marker"
    assert payload.contains_secret(
        {"body": f"see {marker}"}, extra_secrets=[marker]
    )
    assert payload.find_secrets(
        {"body": f"see {marker}"}, extra_secrets=[marker]
    )[0].kind == "known_secret"
    # Too short to be an exact secret check; it must not turn every payload red.
    assert not payload.contains_secret("abcd", extra_secrets=["abc"])


def test_a_credential_named_field_is_refused_even_without_a_prefix():
    findings = payload.find_secrets({"password": "hunter2", "api_key": "none"})
    assert [(finding.path, finding.kind) for finding in findings] == [
        ("password", "secret_field"),
        ("api_key", "secret_field"),
    ]
    # A digest field names its storage, not a bearer credential.
    assert not payload.contains_secret({"token_sha256": "a" * 64})


def test_commit_hashes_uuids_and_prose_are_not_secrets():
    assert not payload.contains_secret("a" * 40)
    assert not payload.contains_secret("a" * 64)
    assert not payload.contains_secret("550e8400-e29b-41d4-a716-446655440000")
    assert not payload.contains_secret(
        "Use the parser in src/server and run pytest before committing."
    )
    assert not payload.contains_secret({"files": ["src/server/app/collab.py"]})


@pytest.mark.parametrize(
    "path",
    [
        "tests/unit/migrations/test_118_deliberation_groups_continue.py",
        "tests/unit/collab/test_collab_envelope.py",
        "tests/unit/database/collab/test_collab_db.py",
        "src/server/database/collab/leases.py",
        "docs/plans/next/n80-collab-protocol.md",
        "feature/auto-dispatch-fb82909c-9dc0-4bc9-92e7-97c04960141d/line-line-0",
    ],
)
def test_ordinary_write_scope_paths_are_not_credentials(path: str) -> None:
    assert payload.find_secrets({"write_scope": [path]}) == []


def test_a_generic_mixed_case_token_is_still_refused():
    token = "Abcdefghijklmnopqrstuvwxyz0123456789"
    findings = payload.find_secrets({"text": token})
    assert findings and findings[0].kind == "token_like"


def test_helpers_agree_and_assert_is_silent_when_clean():
    clean = {"body": "plain task text"}
    assert not payload.contains_secret(clean)
    assert payload.find_secrets(clean) == []
    assert payload.secret_kinds(clean) == ()
    assert not payload.looks_secret(clean)
    payload.assert_no_secrets(clean)
    assert payload.assert_no_secret is payload.assert_no_secrets
    assert payload.ensure_no_secrets is payload.assert_no_secrets
    assert payload.scan_payload is payload.find_secrets
