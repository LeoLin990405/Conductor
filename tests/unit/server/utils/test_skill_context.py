"""Tests for the server's skill request-parsing helpers.

Body loading + inline injection moved into ``ptc_agent`` (SkillsMiddleware); this
module now only resolves *which* skills a request activated:
``parse_skill_contexts`` (from ``additional_context``) and ``detect_slash_commands``
(leading ``/command`` fallback).
"""

from unittest.mock import patch

from src.server.models.additional_context import SkillContext
from src.server.utils.skill_context import (
    detect_slash_commands,
    parse_skill_contexts,
)

MOD = "src.server.utils.skill_context"


# ---------------------------------------------------------------------------
# parse_skill_contexts
# ---------------------------------------------------------------------------


def test_parse_none_and_empty_returns_empty():
    assert parse_skill_contexts(None) == []
    assert parse_skill_contexts([]) == []


def test_parse_dict_skill_item():
    result = parse_skill_contexts(
        [{"type": "skills", "name": "chart-annotation", "instruction": "rainfall:1d"}]
    )
    assert len(result) == 1
    assert result[0].name == "chart-annotation"
    assert result[0].instruction == "rainfall:1d"


def test_parse_passes_through_skillcontext_instances():
    ctx = SkillContext(type="skills", name="research", instruction="news")
    assert parse_skill_contexts([ctx]) == [ctx]


def test_parse_filters_non_skill_items():
    result = parse_skill_contexts(
        [
            {"type": "directive", "content": "be terse"},
            {"type": "skills", "name": "research"},
        ]
    )
    assert [s.name for s in result] == ["research"]


_PROPOSE_INSTRUCTION = (
    "Onboarding kickoff. Action: propose. Calendar: none. Calendars used: "
    "Outlook, 飞书日历. Screenshots: 2. Daily brief: attached. The work "
    "profile is already saved; do not ask for it again."
)
_BRIEF = (
    "<daily-brief>\n"
    "# Office reopens after renovation\n"
    "Type: daily · Completed: 2026-10-19T13:30:00Z · Model: example\n\n"
    "The east wing opened a week early.\n"
    "</daily-brief>"
)
_PNG = "data:image/png;base64,iVBORw0KGgo="

# What the welcome flow sends when the user wrote no task: the onboarding
# skill alone, the day's brief as a directive, and the screenshots as images.
_KICKOFF = [
    {"type": "skills", "name": "onboarding", "instruction": _PROPOSE_INSTRUCTION},
    {"type": "directive", "content": _BRIEF},
    {"type": "image", "data": _PNG, "description": "tasks.png"},
    {"type": "image", "data": _PNG, "description": "calendar.png"},
]


def test_kickoff_parses_to_one_skill_beside_its_brief_and_images():
    """Each parser takes only its own entries from the kickoff: one skill with
    its instruction whole, the brief as a directive, the images as images."""
    from src.server.models.chat import ChatRequest
    from src.server.utils.directive_context import parse_directive_contexts
    from src.server.utils.multimodal_context import parse_multimodal_contexts

    request = ChatRequest(
        messages=[{"role": "user", "content": "Bring in my tasks and suggest where to start."}],
        additional_context=_KICKOFF,
    )

    assert [(s.name, s.instruction) for s in parse_skill_contexts(request.additional_context)] == [
        ("onboarding", _PROPOSE_INSTRUCTION),
    ]
    assert [d.content for d in parse_directive_contexts(request.additional_context)] == [_BRIEF]
    images = parse_multimodal_contexts(request.additional_context)
    assert [(i.type, i.description) for i in images] == [
        ("image", "tasks.png"),
        ("image", "calendar.png"),
    ]


def test_kickoff_reaches_the_agent_config_whole():
    """``prepare_skill_contexts`` is what rides ``configurable``: the one
    entry arrives with its instruction, and the message is left alone since
    no slash fallback runs."""
    from src.server.handlers.chat.request_prep import prepare_skill_contexts
    from src.server.models.chat import ChatRequest

    messages = [{"role": "user", "content": "Suggest where to start."}]
    request = ChatRequest(messages=messages, additional_context=_KICKOFF)

    contexts = prepare_skill_contexts(messages, request)

    assert contexts == [{"name": "onboarding", "instruction": _PROPOSE_INSTRUCTION}]
    assert messages[0]["content"] == "Suggest where to start."


# ---------------------------------------------------------------------------
# detect_slash_commands
# ---------------------------------------------------------------------------


def test_detect_non_slash_text_is_unchanged():
    text, detected = detect_slash_commands("hello world")
    assert text == "hello world"
    assert detected == []


def test_detect_matched_command_strips_prefix():
    with patch(f"{MOD}.get_command_to_skill_map", return_value={"research": "research"}):
        text, detected = detect_slash_commands("/research competitor analysis")
    assert text == "competitor analysis"
    assert [s.name for s in detected] == ["research"]


def test_detect_command_only_keeps_original_text():
    """A bare ``/command`` with no body keeps the original text so the agent at
    least knows what was asked."""
    with patch(f"{MOD}.get_command_to_skill_map", return_value={"research": "research"}):
        text, detected = detect_slash_commands("/research")
    assert text == "/research"
    assert [s.name for s in detected] == ["research"]


def test_detect_unregistered_command_is_unchanged():
    with patch(f"{MOD}.get_command_to_skill_map", return_value={"research": "research"}):
        text, detected = detect_slash_commands("/unknown do thing")
    assert text == "/unknown do thing"
    assert detected == []


def test_detect_extra_command_triggers():
    with patch(f"{MOD}.get_command_to_skill_map", return_value={"research": "research"}):
        text, detected = detect_slash_commands(
            "/pb do thing", extra_commands={"pb": "probe"}
        )
    assert text == "do thing"
    assert [s.name for s in detected] == ["probe"]


def test_detect_extra_command_replaces_builtin_trigger():
    """A rename in ``extra_commands`` REPLACES the builtin trigger for the
    same skill: the old command must stop answering."""
    builtin = {"chart-annotation": "chart-annotation", "research": "research"}
    extra = {"mw": "chart-annotation"}
    with patch(f"{MOD}.get_command_to_skill_map", return_value=builtin):
        old_text, old_detected = detect_slash_commands(
            "/chart-annotation rainfall", extra_commands=extra
        )
        new_text, new_detected = detect_slash_commands(
            "/mw rainfall", extra_commands=extra
        )
        other_text, other_detected = detect_slash_commands(
            "/research news", extra_commands=extra
        )
    assert old_detected == [] and old_text == "/chart-annotation rainfall"
    assert [s.name for s in new_detected] == ["chart-annotation"] and new_text == "rainfall"
    assert [s.name for s in other_detected] == ["research"]


def test_detect_gates_on_the_builds_own_skill_set():
    """A command the build will refuse must not match at all.

    Detection strips the prefix and records a skill context; if the effective
    registry then rejects the skill, nothing loads and the model silently
    receives the shortened message. The gate covers both an alias and a
    default trigger, since a disabled skill has both.
    """
    builtin = {"chart-annotation": "chart-annotation", "research": "research"}
    extra = {"mw": "chart-annotation"}
    allowed = {"research"}  # chart-annotation disabled for this user
    with patch(f"{MOD}.get_command_to_skill_map", return_value=builtin):
        alias_text, alias_detected = detect_slash_commands(
            "/mw rainfall", extra_commands=extra, allowed_skills=allowed
        )
        plain_text, plain_detected = detect_slash_commands(
            "/chart-annotation rainfall", allowed_skills=allowed
        )
        live_text, live_detected = detect_slash_commands(
            "/research news", extra_commands=extra, allowed_skills=allowed
        )
    assert alias_detected == [] and alias_text == "/mw rainfall"
    assert plain_detected == [] and plain_text == "/chart-annotation rainfall"
    assert [s.name for s in live_detected] == ["research"] and live_text == "news"


def test_gating_a_renamed_skill_does_not_revive_its_builtin_trigger():
    """The filter runs after the merge. Applied before it, dropping the alias
    would leave the builtin trigger the rename was supposed to replace."""
    builtin = {"chart-annotation": "chart-annotation"}
    with patch(f"{MOD}.get_command_to_skill_map", return_value=builtin):
        text, detected = detect_slash_commands(
            "/chart-annotation rainfall",
            extra_commands={"mw": "chart-annotation"},
            allowed_skills=set(),
        )
    assert detected == [] and text == "/chart-annotation rainfall"


def test_no_allowed_set_means_no_gating():
    """The CLI and tests have no user, so None keeps today's behavior."""
    with patch(f"{MOD}.get_command_to_skill_map", return_value={"research": "research"}):
        text, detected = detect_slash_commands("/research news", allowed_skills=None)
    assert [s.name for s in detected] == ["research"] and text == "news"
