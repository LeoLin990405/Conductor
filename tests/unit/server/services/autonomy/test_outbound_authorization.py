"""Outbound endpoint authorization: who a reply may be sent to.

The authorization layer is independent of any prompt instruction: it reads
only the Source configuration, the outbox row's identity fields and the
Ticket's origin, so injected Worker or Router text cannot steer a reply to a
destination the Source and the Ticket origin do not name.

These tests pin the rule matrix down: the Source/origin binding, the
GitHub thread binding, the email recipient binding, the fail-closed
missing-origin rule, and the transports that bypass it.
"""

from __future__ import annotations

import pytest

from src.server.services.autonomy.channels import (
    TRANSPORT_EMAIL,
    TRANSPORT_FEISHU,
    TRANSPORT_GITHUB,
    TRANSPORT_LOCAL,
    TRANSPORT_SLACK,
)
from src.server.services.autonomy.replies import (
    OUTBOUND_CROSS_SOURCE,
    OUTBOUND_DESTINATION_NOT_FROM_SOURCE,
    OUTBOUND_MISSING_TICKET_ORIGIN,
    OUTBOUND_OK,
    OUTBOUND_ORIGIN_THREAD_MISSING,
    OUTBOUND_RECIPIENT_MISMATCH,
    OUTBOUND_THREAD_MISMATCH,
    OUTBOUND_UNCHECKED_TRANSPORT,
    authorize_outbound,
)


# --- row / config / origin builders -----------------------------------------


def make_config(**overrides) -> dict:
    """A minimal Source configuration; tests add channel fields as needed."""
    config: dict = {}
    config.update(overrides)
    return config


def make_row(**overrides) -> dict:
    """A minimal outbox row from the ticket's origin Source."""
    row: dict = {"source_id": "source-1", "thread_key": ""}
    row.update(overrides)
    return row


def make_origin(**overrides) -> dict:
    """A ticket origin on source-1 with a GitHub thread by default."""
    origin: dict = {
        "source_id": "source-1",
        "thread_key": "github:acme/widgets#42",
        "reply_source_ids": (),
    }
    origin.update(overrides)
    return origin


# --- github: the happy path ---------------------------------------------------


def test_github_happy_path_allows_origin_thread_in_configured_repository():
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(thread_key="github:acme/widgets#42"),
        make_origin(thread_key="github:acme/widgets#42"),
    )
    assert decision.allowed is True
    assert decision.reason_code == OUTBOUND_OK


# --- github: the Source binding ------------------------------------------------


def test_github_row_from_other_source_is_cross_source():
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(source_id="source-9"),
        make_origin(),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_CROSS_SOURCE


def test_github_row_in_reply_source_ids_is_allowed():
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(source_id="source-2", thread_key="github:acme/widgets#42"),
        make_origin(reply_source_ids=("source-2",)),
    )
    assert decision.allowed is True
    assert decision.reason_code == OUTBOUND_OK


def test_github_source_outside_reply_source_ids_is_cross_source():
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(source_id="source-3"),
        make_origin(reply_source_ids=("source-2",)),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_CROSS_SOURCE


# --- github: the thread binding -------------------------------------------------


def test_github_row_on_other_thread_is_thread_mismatch():
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(thread_key="github:acme/widgets#43"),
        make_origin(thread_key="github:acme/widgets#42"),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_THREAD_MISMATCH


def test_github_row_thread_naming_other_repository_is_not_from_source():
    # The row's thread_key is row data: worker text cannot steer the reply
    # to a repository the Source does not name.
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(thread_key="github:evil/org#42"),
        make_origin(thread_key="github:evil/org#42"),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_DESTINATION_NOT_FROM_SOURCE


def test_github_with_a_malformed_origin_thread_reports_origin_thread_missing():
    # R5: a free-form origin thread (`thread-1`) is not a GitHub thread. The
    # origin fails closed with the stable reason before anything else runs.
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(thread_key="thread-1"),
        make_origin(thread_key="thread-1"),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_ORIGIN_THREAD_MISSING


def test_github_origin_equal_to_a_zero_issue_is_denied():
    # `#0` is not a positive issue number: the origin is malformed.
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(thread_key="github:acme/widgets#0"),
        make_origin(thread_key="github:acme/widgets#0"),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_ORIGIN_THREAD_MISSING


def test_github_without_configured_repository_allows_a_valid_origin_thread():
    # No repository is configured, so the repository check is skipped; the
    # destination is still bound by the origin-thread equality.
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(),
        make_row(thread_key="github:acme/widgets#42"),
        make_origin(thread_key="github:acme/widgets#42"),
    )
    assert decision.allowed is True
    assert decision.reason_code == OUTBOUND_OK


# --- email: the recipient binding ------------------------------------------------


def test_email_matching_recipient_case_insensitive_is_allowed():
    decision = authorize_outbound(
        TRANSPORT_EMAIL,
        make_config(to_address="Replies@Acme.example"),
        make_row(to_address="replies@acme.example"),
        make_origin(),
    )
    assert decision.allowed is True
    assert decision.reason_code == OUTBOUND_OK


def test_email_row_cannot_override_recipient():
    decision = authorize_outbound(
        TRANSPORT_EMAIL,
        make_config(to_address="replies@acme.example"),
        make_row(to_address="attacker@evil.example"),
        make_origin(),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_RECIPIENT_MISMATCH


def test_email_row_without_recipient_is_allowed():
    # The recipient comes from the Source configuration only; an empty row
    # address names no override.
    decision = authorize_outbound(
        TRANSPORT_EMAIL,
        make_config(to_address="replies@acme.example"),
        make_row(to_address=""),
        make_origin(),
    )
    assert decision.allowed is True
    assert decision.reason_code == OUTBOUND_OK


# --- slack and feishu: the Source binding only -----------------------------------


def test_slack_matching_source_is_allowed():
    decision = authorize_outbound(
        TRANSPORT_SLACK,
        make_config(),
        make_row(),
        make_origin(thread_key=""),
    )
    assert decision.allowed is True
    assert decision.reason_code == OUTBOUND_OK


def test_slack_cross_source_is_denied():
    decision = authorize_outbound(
        TRANSPORT_SLACK,
        make_config(),
        make_row(source_id="source-9"),
        make_origin(thread_key=""),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_CROSS_SOURCE


def test_feishu_matching_source_is_allowed():
    decision = authorize_outbound(
        TRANSPORT_FEISHU,
        make_config(),
        make_row(),
        make_origin(thread_key=""),
    )
    assert decision.allowed is True
    assert decision.reason_code == OUTBOUND_OK


def test_feishu_cross_source_is_denied():
    decision = authorize_outbound(
        TRANSPORT_FEISHU,
        make_config(),
        make_row(source_id="source-9"),
        make_origin(thread_key=""),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_CROSS_SOURCE


# --- missing origin: fail closed -------------------------------------------------


@pytest.mark.parametrize(
    "transport",
    [TRANSPORT_GITHUB, TRANSPORT_SLACK, TRANSPORT_EMAIL, TRANSPORT_FEISHU],
)
def test_missing_ticket_origin_denies_external_transports(transport):
    decision = authorize_outbound(
        transport,
        make_config(repository="acme/widgets", to_address="replies@acme.example"),
        make_row(),
        None,
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_MISSING_TICKET_ORIGIN


def test_empty_origin_source_id_denies_external_transport():
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(),
        make_origin(source_id=""),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_MISSING_TICKET_ORIGIN


# --- local and unknown transports ------------------------------------------------


def test_local_transport_is_allowed_without_origin():
    # The local transport has no external destination, so it is allowed even
    # with no ticket origin and an empty outbox row.
    decision = authorize_outbound(TRANSPORT_LOCAL, {}, {}, None)
    assert decision.allowed is True
    assert decision.reason_code == OUTBOUND_OK


def test_unknown_transport_is_allowed_but_unchecked():
    # authorize_outbound does not know this transport, so it reports it as
    # unchecked rather than guessing the rules. The decision still allows:
    # the caller's fallback in reply_effect refuses any transport it cannot
    # deliver, so nothing is sent either way.
    decision = authorize_outbound(
        "carrier-pigeon",
        make_config(),
        make_row(source_id="source-9"),
        make_origin(),
    )
    assert decision.allowed is True
    assert decision.reason_code == OUTBOUND_UNCHECKED_TRANSPORT


# --- a missing GitHub origin thread fails closed (B5) ------------------------


def test_github_with_an_empty_origin_thread_is_denied():
    """Without the origin thread there is nothing to bind the reply to."""
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(thread_key="github:acme/widgets#999", source_id="source-1"),
        make_origin(thread_key=""),
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_ORIGIN_THREAD_MISSING


def test_github_with_a_missing_origin_thread_key_is_denied():
    origin = make_origin()
    origin.pop("thread_key")
    decision = authorize_outbound(
        TRANSPORT_GITHUB,
        make_config(repository="acme/widgets"),
        make_row(thread_key="github:acme/widgets#42"),
        origin,
    )
    assert decision.allowed is False
    assert decision.reason_code == OUTBOUND_ORIGIN_THREAD_MISSING
