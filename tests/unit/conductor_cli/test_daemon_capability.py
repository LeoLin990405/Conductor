"""A per-Ticket operation capability, and the binding the daemon enforces."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.conductor_cli.daemon_capability import (
    GrantError,
    authorize,
    parse_capability,
    refuse_result,
)
from src.conductor_cli.daemon_policy import parse_policy

DAEMON_ID = "11111111-1111-4111-8111-111111111111"
OTHER_DAEMON = "22222222-2222-4222-8222-222222222222"
WHEN = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)

POLICY = {
    "format": 1,
    "read_only_operations": ["repo.read", "test.run"],
    "operations": [
        {"operation": "repo.read", "patterns": [r"\bgit\s+status\b"]},
        {"operation": "test.run", "patterns": [r"\bpytest\b"]},
        {"operation": "file.write", "patterns": [r"\bwrite the file\b"]},
        {"operation": "repo.push", "patterns": [r"\bgit\s+push\b"]},
    ],
    "providers": {
        "codex": {
            "allow": ["repo.read", "test.run", "file.write"],
            "deny": ["repo.push"],
        }
    },
}


def _body(**overrides) -> dict:
    body = {
        "ticket_id": "t-cap",
        "daemon_id": DAEMON_ID,
        "policy_version": "abc123",
        "expires_at": "2099-01-01T00:00:00Z",
        "operations": ["repo.read"],
    }
    body.update(overrides)
    return body


def _decision(**overrides):
    capability = parse_capability(_body(**overrides.pop("capability", {})))
    fields = {
        "ticket_id": "t-cap",
        "daemon_id": DAEMON_ID,
        "policy_version": "abc123",
        "allowed_operations": frozenset({"repo.read", "test.run"}),
        "requested": {"repo.read"},
        "now": WHEN,
    }
    fields.update(overrides)
    return authorize(capability, **fields)


def test_a_grant_body_parses_into_the_bound_capability():
    capability = parse_capability(_body(signature="phase-2-owns-this"))

    assert capability.ticket_id == "t-cap"
    assert capability.daemon_id == DAEMON_ID
    assert capability.policy_version == "abc123"
    assert capability.operations == frozenset({"repo.read"})
    assert capability.expires_at == datetime(2099, 1, 1, tzinfo=timezone.utc)


def test_a_capability_authorizes_the_operation_it_names():
    decision = _decision()

    assert decision.allowed is True
    assert decision.reason == ""


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ticket_id": ""}, "ticket id"),
        ({"daemon_id": "  "}, "daemon id"),
        ({"policy_version": "abc 123"}, "policy version"),
        ({"expires_at": "2099-01-01T00:00:00"}, "no timezone"),
        ({"expires_at": "yesterday"}, "not a timestamp"),
        ({"expires_at": "2099-01-01T00:00:00.5Z"}, "whole seconds"),
        ({"operations": []}, "no operations"),
        ({"operations": ["repo read"]}, "empty operation"),
        ({"operations": [""]}, "empty operation"),
    ],
)
def test_a_malformed_capability_is_refused(overrides, message):
    with pytest.raises(GrantError, match=message):
        parse_capability(_body(**overrides))


def test_a_missing_body_is_refused():
    with pytest.raises(GrantError, match="not a mapping"):
        parse_capability(["repo.read"])  # type: ignore[arg-type]


def test_an_expired_capability_is_refused_at_its_expiry():
    decision = _decision(
        capability={"expires_at": "2026-11-03T12:00:00Z"},
    )

    assert decision.allowed is False
    assert decision.reason == "the operation capability expired"


def test_a_capability_for_another_ticket_is_refused():
    decision = _decision(ticket_id="t-other")

    assert decision.allowed is False
    assert "different ticket" in decision.reason


def test_a_capability_for_another_daemon_is_refused():
    decision = _decision(daemon_id=OTHER_DAEMON)

    assert decision.allowed is False
    assert "different daemon" in decision.reason


def test_the_same_daemon_uuid_matches_in_either_case():
    # A UUID with alphabetic hex characters, so case folding is exercised.
    hex_daemon = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    decision = _decision(
        daemon_id=hex_daemon.upper(),
        capability={"daemon_id": hex_daemon},
    )

    assert decision.allowed is True


def test_a_daemon_with_no_id_cannot_bind_a_capability():
    decision = _decision(daemon_id="")

    assert decision.allowed is False
    assert "no id" in decision.reason


def test_a_policy_version_mismatch_is_refused():
    decision = _decision(policy_version="ffff")

    assert decision.allowed is False
    assert "abc123" in decision.reason
    assert "ffff" in decision.reason


def test_a_capability_wider_than_this_run_is_refused():
    decision = _decision(capability={"operations": ["repo.read", "repo.push"]})

    assert decision.allowed is False
    assert "repo.push" in decision.reason
    assert "does not allow" in decision.reason


def test_a_requested_operation_outside_the_capability_is_refused():
    decision = _decision(requested={"test.run"})

    assert decision.allowed is False
    assert "test.run" in decision.reason
    assert "outside the operation capability" in decision.reason


def test_a_speculative_run_cannot_be_granted_a_mutating_operation():
    policy = parse_policy(POLICY)
    allowed = policy.allowed_operations("codex", speculative=True)

    assert allowed == frozenset({"repo.read", "test.run"})
    decision = _decision(
        capability={"operations": ["repo.read", "file.write"]},
        allowed_operations=allowed,
    )

    assert decision.allowed is False
    assert "file.write" in decision.reason


def test_a_result_outside_the_capability_is_not_reportable():
    capability = parse_capability(_body())

    reason = refuse_result(
        capability,
        {"test.run"},
        policy_version="abc123",
        now=WHEN,
    )

    assert reason == "operation test.run is outside the operation capability"
    assert "pytest" not in reason


def test_a_result_inside_the_capability_may_be_reported():
    capability = parse_capability(_body())

    assert (
        refuse_result(
            capability,
            {"repo.read"},
            policy_version="abc123",
            now=WHEN,
        )
        == ""
    )


def test_a_result_is_refused_when_the_capability_has_expired():
    capability = parse_capability(_body(expires_at="2026-11-03T12:00:00Z"))

    reason = refuse_result(
        capability, set(), policy_version="abc123", now=WHEN
    )

    assert reason == "the operation capability expired"


def test_a_result_is_refused_when_the_policy_version_changed():
    capability = parse_capability(_body())

    reason = refuse_result(
        capability, {"repo.read"}, policy_version="ffff", now=WHEN
    )

    assert "policy changed" in reason
