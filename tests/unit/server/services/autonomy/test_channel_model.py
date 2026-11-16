"""The channel model decides the form, the limits and the approval of a reply.

Article §4.2: people communicate differently in group chats, email and GitHub
discussions, so the agent has to adjust both the level of detail and the way
it responds to suit each channel. IM stays light and interrupts people rarely;
asynchronous channels carry structured, information-dense output.

These tests pin that model down: which channel a Source resolves to, what
each channel does to a reply, and when a reply may leave the system at all.
"""

from __future__ import annotations

import pytest

from src.server.services.autonomy.channels import (
    ACTION_DELIVER,
    ACTION_HOLD,
    ACTION_SUPPRESS,
    CHANNEL_EMAIL,
    CHANNEL_FEISHU,
    CHANNEL_GITHUB,
    CHANNEL_MANUAL,
    CHANNEL_SLACK,
    CHANNEL_UNKNOWN,
    FAMILY_ASYNC,
    FAMILY_IM,
    FAMILY_LOCAL,
    KIND_FAILURE,
    KIND_PROGRESS,
    KIND_QUESTION,
    KIND_RESULT,
    cap_reply_words,
    channel_catalog_instruction,
    count_reply_units,
    decide_delivery,
    missing_sections,
    parse_email_sections,
    render_email,
    resolve_channel,
    shape_reply,
    spec_for,
    transport_for,
)


# --- which channel a Source is on ------------------------------------------


@pytest.mark.parametrize(
    ("kind", "channel", "family"),
    [
        ("feishu", CHANNEL_FEISHU, FAMILY_IM),
        ("slack", CHANNEL_SLACK, FAMILY_IM),
        ("email", CHANNEL_EMAIL, FAMILY_ASYNC),
        ("github", CHANNEL_GITHUB, FAMILY_ASYNC),
        ("manual", CHANNEL_MANUAL, FAMILY_LOCAL),
    ],
)
def test_each_source_kind_resolves_to_its_own_channel(kind, channel, family):
    spec = resolve_channel(kind)

    assert spec.channel == channel
    assert spec.family == family


def test_an_unknown_kind_gets_the_conservative_channel_not_a_github_comment():
    """Before the channel model, every non-Feishu kind was posted to GitHub."""
    spec = resolve_channel("something-else")

    assert spec.channel == CHANNEL_UNKNOWN
    assert spec.transport == "none"
    assert decide_delivery(spec, kind=KIND_RESULT).action == ACTION_HOLD


def test_a_source_may_narrow_its_channel_inside_its_own_family():
    assert resolve_channel("feishu", {"channel": "chat"}).channel == "chat"


def test_a_source_may_not_move_its_reply_into_another_family():
    """An email Source stays an email Source, whatever its config claims."""
    spec = resolve_channel("email", {"channel": "feishu"})

    assert spec.channel == CHANNEL_EMAIL
    assert spec.family == FAMILY_ASYNC


def test_the_generic_chat_channel_takes_the_im_transport_of_its_source():
    spec = resolve_channel("feishu", {"channel": "chat"})

    assert transport_for(spec, "feishu") == "feishu"
    assert transport_for(spec, "slack") == "slack"


# --- limits ----------------------------------------------------------------


def test_an_im_reply_is_capped_at_one_hundred_words():
    spec = resolve_channel("feishu")

    assert len(shape_reply(spec, "word " * 130).split()) == 100


def test_a_cjk_character_counts_as_one_unit_against_the_same_cap():
    spec = resolve_channel("feishu")

    assert count_reply_units("问题\n" + "界" * 110) == 112
    assert len(shape_reply(spec, "问题\n" + "界" * 110).replace("\n", "")) == 100


def test_capping_keeps_the_leading_text_and_drops_the_rest():
    assert cap_reply_words("one two three", 2) == "one two"
    assert cap_reply_words("one two three", None) == "one two three"


def test_a_structured_channel_is_not_capped_at_one_hundred_words():
    """Asynchronous channels carry the full background, changes and evidence."""
    body = "Background:\n" + ("detail " * 200)

    assert "detail" in shape_reply(resolve_channel("email"), body)


# --- formatting ------------------------------------------------------------


def test_a_github_reply_gets_a_summary_heading():
    assert shape_reply(resolve_channel("github"), "Fixed the issue.") == (
        "### Summary\nFixed the issue."
    )


def test_an_existing_heading_is_left_alone():
    body = "### Result\nFixed and verified."

    assert shape_reply(resolve_channel("github"), body) == body


def test_an_email_reply_is_a_subject_and_named_sections():
    rendered = shape_reply(resolve_channel("email"), "Completed the review.")

    assert rendered == "Subject: Ticket update\n\nSummary:\nCompleted the review."


def test_an_email_reply_keeps_the_sections_the_router_wrote():
    rendered = render_email(
        "Background:\nThe build was broken.\n\n"
        "Summary:\nFixed it.\n\n"
        "Verification:\n42 tests pass."
    )

    assert rendered.splitlines()[0] == "Subject: Ticket update"
    assert rendered.index("Summary:") < rendered.index("Background:")
    assert rendered.index("Background:") < rendered.index("Verification:")
    assert "42 tests pass." in rendered


def test_an_email_reply_uses_the_subject_the_router_chose():
    rendered = render_email("Subject: Release 2.4 is ready\n\nSummary:\nShip it.")

    assert rendered.splitlines()[0] == "Subject: Release 2.4 is ready"


def test_shaping_a_reply_twice_changes_nothing():
    """The reply is shaped when it is queued and again when it is sent."""
    for kind in ("feishu", "github", "email", "manual"):
        spec = resolve_channel(kind)
        once = shape_reply(spec, "Completed the review.")
        assert shape_reply(spec, once) == once


def test_text_before_the_first_section_is_kept_as_the_summary():
    subject, sections = parse_email_sections("A note.\n\nDetails:\nBody.")

    assert subject == ""
    assert sections[0][0] == "Summary"
    assert "A note." in sections[0][1]


def test_the_email_model_says_which_sections_it_needs():
    spec = resolve_channel("email")

    assert missing_sections(spec, "Summary:\nDone.") == ()
    assert missing_sections(spec, "Background:\nOnly this.") == ("Summary",)


# --- approval rules --------------------------------------------------------


def test_an_im_channel_notifies_only_for_a_decision_a_question_or_a_failure():
    spec = resolve_channel("feishu")

    assert decide_delivery(spec, kind=KIND_RESULT).action == ACTION_DELIVER
    assert decide_delivery(spec, kind=KIND_QUESTION).action == ACTION_DELIVER
    assert decide_delivery(spec, kind=KIND_FAILURE).action == ACTION_DELIVER


def test_an_im_channel_does_not_interrupt_anyone_with_a_progress_update():
    decision = decide_delivery(resolve_channel("feishu"), kind=KIND_PROGRESS)

    assert decision.action == ACTION_SUPPRESS
    assert "decision" in decision.reason


def test_an_email_draft_waits_for_a_person_to_read_it():
    decision = decide_delivery(resolve_channel("email"), kind=KIND_RESULT)

    assert decision.action == ACTION_HOLD
    assert "review" in decision.reason


def test_a_source_may_switch_its_own_approval_rule():
    assert decide_delivery(
        resolve_channel("email"), kind=KIND_RESULT, config={"require_approval": False}
    ).action == ACTION_DELIVER
    assert decide_delivery(
        resolve_channel("feishu"), kind=KIND_RESULT, config={"require_approval": True}
    ).action == ACTION_HOLD


def test_a_github_reply_is_delivered_without_a_person_reading_it_first():
    assert decide_delivery(
        resolve_channel("github"), kind=KIND_RESULT
    ).action == ACTION_DELIVER


def test_the_local_channel_is_recorded_and_never_delivered_outwards():
    spec = resolve_channel("manual")

    assert spec.transport == "local"
    assert decide_delivery(spec, kind=KIND_PROGRESS).action == ACTION_DELIVER


# --- the norms the Router is told about ------------------------------------


def test_every_channel_states_its_own_norm():
    for channel in ("feishu", "slack", "email", "github", "manual"):
        assert spec_for(channel).norm


def test_the_catalog_covers_the_norms_of_every_family():
    catalog = channel_catalog_instruction()

    assert "100 words" in catalog
    assert "### Summary" in catalog
    assert "Subject and Summary" in catalog
