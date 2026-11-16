"""The structured profile: the keys the onboarding form, the Settings page and
the agent's profile file share, the values each may hold, and the one check
every writer holds them to.

They sit in the same JSONB columns as the free-text keys written before them
(``tone``, ``verbosity``, ...), which keep no
vocabulary and stay readable and writable as they were.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# How the user wants the agent to work with them by default: how much
# research a question gets, what an answer usually is, and when the agent
# reaches out.
RESEARCH_DEPTHS = ("light", "adaptive", "thorough")
# A report is not one of these: it is ``output_format: "html"``, which
# Settings toggles and which selects a section of the prompt of its own.
DELIVERABLES = ("text", "visual")
PROACTIVITY_LEVELS = ("proactive", "when_needed", "on_request")
# Absent is markdown: Settings' switch writes null to go back to it.
OUTPUT_FORMATS = ("markdown", "html")


@dataclass(frozen=True, slots=True)
class _Vocabulary:
    # None: the value is free-form, bounded only by its own doc.
    choices: tuple[str, ...] | None
    # What the key means and the values it takes, in markdown: the agent's
    # profile README lists it beside the key, and the API schema describes
    # the field with it.
    doc: str = ""


def _ticks(values: Iterable[str]) -> str:
    return ", ".join(f"`{value}`" for value in values)


STRUCTURED_PREFERENCES: dict[str, dict[str, _Vocabulary]] = {
    "agent_preference": {
        "research_depth": _Vocabulary(
            RESEARCH_DEPTHS,
            doc=(
                "How much research a question gets by default: `light` (a quick answer, "
                "with the full research offered rather than done unasked), `adaptive` "
                "(as much as the question needs), `thorough` (research before every answer)"
            ),
        ),
        "deliverable": _Vocabulary(
            DELIVERABLES,
            doc=(
                "What an answer with substance usually is: `text` (text in the chat) or "
                "`visual` (interactive charts and dashboards in the chat). A report is not "
                'one of these: it is `"output_format": "html"`, which overrides this key'
            ),
        ),
        "proactivity": _Vocabulary(
            PROACTIVITY_LEVELS,
            doc=(
                "When to reach out: `proactive` (keep them posted), `when_needed` (only "
                "when something needs their attention), `on_request` (only when they ask)"
            ),
        ),
        "output_format": _Vocabulary(
            OUTPUT_FORMATS,
            doc=(
                "`markdown` (the default) or `html`, which makes a styled HTML report the "
                "user's usual deliverable"
            ),
        ),
    },
}

def check_profile(
    patches: Mapping[str, Any], stored: Mapping[str, Any] | None
) -> tuple[dict[str, Any], list[tuple[str, str]]]:
    """``patches`` (column to its values) with each structured key in its
    stored spelling, and what is wrong with the rest, as (path, message)
    pairs, every column's together so one refusal can name them all.

    The one check the preferences endpoint and the agent's ``preference.json``
    both run, so the two writers cannot disagree about what a key may hold;
    each raises its own error from the pairs. Free-text keys pass untouched.
    A null passes: the endpoint's merge reads it as a deletion, and the file
    as no value. So does a value equal to the one ``stored`` holds, because
    some keys were free-form before they had a vocabulary, and a save that
    leaves such a value as it is must not be refused for it.
    """
    checked = dict(patches)
    problems: list[tuple[str, str]] = []
    for column in STRUCTURED_PREFERENCES:
        patch = patches.get(column)
        if not patch:
            continue
        checked[column] = _checked_column(
            column, patch, (stored or {}).get(column), problems
        )
    return checked, problems


def _checked_column(
    column: str, values: Mapping[str, Any], stored: Any, problems: list[tuple[str, str]]
) -> dict[str, Any]:
    before = stored if isinstance(stored, Mapping) else {}
    checked = dict(values)
    for key, vocab in STRUCTURED_PREFERENCES[column].items():
        value = values.get(key)
        if value is None or (key in before and value == before[key]):
            continue
        path = f"{column}.{key}"
        checked[key] = _checked_choice(value, vocab.choices or (), path, problems)
    return checked


def _checked_choice(
    value: Any, choices: tuple[str, ...], path: str, problems: list[tuple[str, str]]
) -> Any:
    if not isinstance(value, str):
        problems.append(
            (path, f"expected one of {', '.join(choices)}, got {type(value).__name__}")
        )
        return value
    spelled = value.strip().lower()
    if spelled not in choices:
        problems.append((path, f"is not one of {', '.join(choices)}"))
    return spelled


def prompt_preferences(
    agent_preference: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """``agent_preference`` split the way the prompt reads it: the keys it
    prints as the user wrote them, and the structured keys whose value
    selects guidance instead (``output_format: "html"``, the report, among
    them).

    A structured key holding a value outside its vocabulary, such as free text
    written before the key had one, stays with the printed keys, so the user's
    words still reach the model.
    """
    printed = dict(agent_preference) if isinstance(agent_preference, Mapping) else {}
    steering: dict[str, str] = {}
    for key, vocab in STRUCTURED_PREFERENCES["agent_preference"].items():
        if printed.get(key) in (vocab.choices or ()):
            steering[key] = printed.pop(key)
    return printed, steering
