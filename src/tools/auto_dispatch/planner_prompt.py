"""The planner's prompt templates: the schema the model is held to.

Decomposition is the one stage of an auto-dispatch run that asks a model for a
decision, so its prompt is the contract the strict validator in ``planning.py``
checks the reply against. :data:`PLANNER_SYSTEM_PROMPT` states the JSON schema
(``planner_prompt_v2``) and the hard rules; :func:`build_planner_prompt` adds
the goal and the developer roster for one run.

Neither template reads the clock, the filesystem or the network, and neither
carries a credential or anything from an earlier exchange, so the same goal and
roster always produce the same two strings.
"""

from __future__ import annotations

import copy

from collections.abc import Iterable, Iterator, Mapping

PLANNER_PROMPT_VERSION: str = "planner_prompt_v2"

# The plan cap both prompts state; planning.py enforces it as LLMPlanner's
# max_lines, and a longer plan is a rejection rather than a truncation.
MAX_PLAN_LINES = 8

# A roster as the caller spells it: a name/capacity mapping, a mapping holding
# a nested developer pool, or an iterable of (name, capacity) pairs.
Roster = Mapping[str, object] | Iterable[object]

# A planner that names paths it never looked at plans work the developers cannot
# find, and a risk-bearing line with no command blocks at G2 as
# g2_no_commands_risk_bearing, so the user turn asks for both up front.
REPO_GROUNDING_RULE = (
    "Inspect the repository before planning. Only name files and directories"
    " that exist in it, or that the line itself creates. Every risk_bearing line"
    " (migrations, schemas, auth) needs at least one acceptance entry written as"
    ' "cmd: <argv>" that verifies it, such as a focused pytest run.'
)

# N27: an acceptance command checks the line's own work. A whole test
# directory or the whole suite also checks other lines' work and whatever the
# base commit already breaks, so the validator refuses it
# (acceptance_test_directory).
ACCEPTANCE_SCOPE_RULE = (
    "Acceptance commands cover only the line's own write_scope: name the test"
    " files the line adds or changes (cmd: pytest -q tests/unit/test_x.py),"
    " never a whole test directory such as tests/unit and never a bare pytest"
    " run of the whole suite."
)

# The reply schema, spelled out field by field. A field the model is not told
# about is a field it invents, and an invented field is an unknown_field
# rejection, so the schema below is exhaustive.
PLANNER_SYSTEM_PROMPT = """\
You are the planner of an auto-dispatch run: you turn one software goal into a
SPEC and a dependency-ordered set of parallel work lines for a developer pool.

Reply with one JSON object and nothing else. No prose before or after it, no
markdown fence, no comments inside it. The object has exactly these fields, and
no unknown field anywhere:

{
  "schema_version": 1,
  "spec": {
    "title": string, at most 256 bytes,
    "body": markdown, at most 16000 bytes, with the sections "## Goal",
            "## Non-goals", "## Constraints" and "## Acceptance"
  },
  "lines": [
    {
      "name": snake_case, unique in the plan, at most 256 bytes,
      "task": one imperative sentence naming the files to touch, at most
              8000 bytes,
      "depends_on": names of other lines of this same plan; [] for a root line,
      "write_scope": repository-relative paths or globs, 1 to 32 entries, each
                     at most 512 bytes,
      "declared_paths": repository-relative paths or globs owned by this line
                        while it is active, 1 to 32 entries, each at most 512
                        bytes; normally the same paths as "write_scope",
      "acceptance": an executable check written as "cmd: <argv>", or a
                    human-readable assertion, 1 to 16 entries,
      "risk_bearing": true or false,
      "estimated_size": optional "small", "medium" or "large"; omit it for an
                        ordinary medium task. A "large" line is routed to the
                        developer with the most remaining quota.
    }
  ]
}

Hard rules. A plan that breaks one is rejected as a whole:

- "schema_version" is the integer 1 and every required field above is present.
  "estimated_size" is the one optional field.
- No empty or whitespace-only value anywhere: no empty string, list or object.
  "depends_on" is the one exception -- an empty list is a legal root line that
  waits on nothing, and every plan needs at least one.
- Every "name" is snake_case and unique across the plan.
- "depends_on" names only other lines of this same plan, never the line it
  belongs to, and never a name the plan does not define.
- The "write_scope" and "declared_paths" of any two lines must not overlap,
  not even of two lines where one depends on the other, because every assigned
  line is started at once. Two entries overlap when they name the same path,
  when one is a directory prefix of the other, or when a glob prefix of one is
  a prefix of the other.
- Every "write_scope" and "declared_paths" entry is repository-relative: no
  absolute path, no ".." segment, no leading "~" and no drive letter such as
  "C:". "declared_paths" is required and is the ownership claim checked against
  active lines of every dispatch, not only this plan.
- At most 8 lines, and never more lines than the developer places the user turn
  states: every line holds one developer place for the whole run, even a line
  that waits on a dependency. Merge small items into fewer lines.
- An acceptance command names the line's own test files. A pytest run of a
  whole test directory, or of the whole suite, is rejected.

Output the JSON object only."""


# The same shape as PLANNER_SYSTEM_PROMPT, as a JSON Schema for CLIs that can
# enforce structured output (Claude Code --json-schema). It guarantees valid
# JSON of the right shape; the byte limits and cross-field rules stay with
# validate_plan, which checks every reply either way.
PLAN_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "spec": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
            "required": ["title", "body"],
            "additionalProperties": False,
        },
        "lines": {
            "type": "array",
            "minItems": 1,
            "maxItems": MAX_PLAN_LINES,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "task": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "write_scope": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "declared_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "acceptance": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "risk_bearing": {"type": "boolean"},
                    "estimated_size": {"type": "string", "enum": ["small", "medium", "large"]},
                },
                "required": [
                    "name", "task", "depends_on", "write_scope", "declared_paths",
                    "acceptance", "risk_bearing",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["schema_version", "spec", "lines"],
    "additionalProperties": False,
}


# N76 §4.4: under CONDUCTOR_DISPATCH_LIVENESS_MODE=enforce the planner may mark
# lines that are integrated together or not at all (``atomic_with``); R-SPLIT
# never removes one member alone. ``off`` and ``shadow`` keep main's prompt and
# schema above byte for byte, so the planner never emits the field there.
_ATOMIC_PROMPT_EDITS = (
    (
        """developer with the most remaining quota.
""",
        """developer with the most remaining quota,
      "atomic_with": optional names of other lines of this same plan that must
                     be integrated with this one or not at all; omit it for an
                     independent line
""",
    ),
    (
        """  "estimated_size" is the one optional field.
""",
        """  "estimated_size" and "atomic_with" are the only optional fields.
""",
    ),
    (
        """  belongs to, and never a name the plan does not define.
""",
        """  belongs to, and never a name the plan does not define.
- "atomic_with" names only other lines of this same plan. An atomic group is
  integrated as a whole: if one member cannot pass, none of the group is
  merged, and a split never removes one member alone.
""",
    ),
)


def _with_atomic(prompt: str) -> str:
    for plain, atomic in _ATOMIC_PROMPT_EDITS:
        if prompt.count(plain) != 1:  # pragma: no cover - guarded by a test
            raise RuntimeError("planner prompt drifted from the atomic_with edits")
        prompt = prompt.replace(plain, atomic)
    return prompt


PLANNER_SYSTEM_PROMPT_ATOMIC = _with_atomic(PLANNER_SYSTEM_PROMPT)
PLAN_JSON_SCHEMA_ATOMIC: dict = copy.deepcopy(PLAN_JSON_SCHEMA)
PLAN_JSON_SCHEMA_ATOMIC["properties"]["lines"]["items"]["properties"]["atomic_with"] = {
    "type": "array",
    "items": {"type": "string"},
}


def atomic_plans_enabled() -> bool:
    """Whether plans may carry ``atomic_with``: only while the sweep enforces."""
    from .liveness import modes as liveness_modes

    return liveness_modes.liveness_mode() == liveness_modes.ENFORCE


# N80 L4: under CONDUCTOR_COLLAB_MODE=full the planner may mark a line an
# interactive app may claim. Every other mode keeps the prompt and the schema
# above byte for byte.
APP_CLAIMABLE_RULE = (
    "- You may set \"app_claimable\": true on a line suited to an interactive"
    " app: a small, self-contained docs- or tests-sized line. Omit the field"
    " on every other line."
)


def app_claims_enabled() -> bool:
    """Whether plans may carry ``app_claimable``: only under collab ``full``."""
    from .app_lines import lines_enabled

    return lines_enabled()


def planner_system_prompt(*, atomic: bool | None = None) -> str:
    """Main's system prompt, plus ``atomic_with`` under enforce."""
    use = atomic_plans_enabled() if atomic is None else atomic
    return PLANNER_SYSTEM_PROMPT_ATOMIC if use else PLANNER_SYSTEM_PROMPT


def plan_json_schema(*, atomic: bool | None = None) -> dict:
    """Main's structured-output schema, plus ``atomic_with`` under enforce."""
    use = atomic_plans_enabled() if atomic is None else atomic
    schema = PLAN_JSON_SCHEMA_ATOMIC if use else PLAN_JSON_SCHEMA
    if not app_claims_enabled():
        return schema
    schema = copy.deepcopy(schema)
    schema["properties"]["lines"]["items"]["properties"]["app_claimable"] = {
        "type": "boolean"
    }
    return schema


def build_planner_prompt(
    goal: str,
    roster: Roster,
    *,
    developers: int | None = None,
    app_claims: bool | None = None,
) -> str:
    """The user turn for one run: the goal verbatim plus the developer roster.

    The goal is embedded exactly as given. The roster contributes developer
    names and their capacities only, so the planner can keep the DAG inside
    what the pool can run at once; nothing else about the caller, and nothing
    from an earlier exchange, reaches the model.

    ``developers`` is the number of developer places the pool can staff right
    now, as the plan validator counts them (N28); without it the roster's
    capacities are summed. Either way the prompt states the number and makes
    it the plan's line limit.

    ``app_claims`` (N80 L4) adds the ``app_claimable`` rule; ``None`` reads
    ``CONDUCTOR_COLLAB_MODE`` (only ``full``). Otherwise the text is unchanged.
    """
    if app_claims is None:
        app_claims = app_claims_enabled()
    pairs = _roster_pairs(roster)
    capacity = (
        max(developers, 0)
        if developers is not None
        else sum(count for _, count in pairs)
    )
    # A roster naming no developer, or only exhausted ones, still needs a bound
    # to plan against, and one line at a time is the only safe one.
    parallel = min(capacity if capacity > 0 else 1, MAX_PLAN_LINES)
    return "\n".join(
        (
            "Decompose this goal and nothing else:",
            "",
            str(goal or ""),
            "",
            _roster_block(pairs),
            f"Developers available: {capacity}.",
            "",
            "Plan rules:",
            f"- At most {parallel} line(s): every line takes one developer place"
            " for the whole run, even one that waits on a dependency, so merge"
            " small items into fewer lines. A plan with more lines is rejected"
            " as exceeds_developer_pool.",
            "- No two lines may write overlapping paths, not even when one of"
            " them depends on the other. Every line must declare its owned paths"
            " in declared_paths; those claims are checked across dispatches.",
            f"- {REPO_GROUNDING_RULE}",
            f"- {ACCEPTANCE_SCOPE_RULE}",
            *((APP_CLAIMABLE_RULE,) if app_claims else ()),
            "",
            "Reply with the JSON object only, following the schema and the hard"
            " rules of the system prompt.",
        )
    )


def _roster_block(developers: tuple[tuple[str, int], ...]) -> str:
    """The developer list the planner has to fit its DAG inside."""
    lines = ["Developers, and how many lines each can run at once:"]
    if developers:
        lines.extend(f"- {name}: {count}" for name, count in developers)
    else:
        lines.append("- none were named")
    return "\n".join(lines)


def _roster_pairs(roster: Roster) -> tuple[tuple[str, int], ...]:
    """Developer names and capacities, in the order the caller gave them.

    Anything that is not a name with a capacity is dropped rather than guessed
    at: the roster comes from a policy the caller owns, and a planner prompt is
    not where a typo in it should surface.
    """
    pairs: list[tuple[str, int]] = []
    seen: set[str] = set()
    for name, count in _iter_roster(roster):
        if name in seen:
            continue
        seen.add(name)
        pairs.append((name, count))
    return tuple(pairs)


def _iter_roster(roster: object) -> Iterator[tuple[str, int]]:
    """Yield (name, capacity) pairs from a mapping, a nested pool or a list.

    A mapping value that is itself a pool is a nested roster under a container
    key, so the key is not a developer and is not reported as one.
    """
    if roster is None:
        return
    if isinstance(roster, Mapping):
        for key, value in roster.items():
            if not isinstance(key, str) or not key.strip():
                continue
            count = _as_capacity(value)
            if count is not None:
                yield key.strip(), count
            elif isinstance(value, (Mapping, list, tuple)):
                yield from _iter_roster(value)
        return
    if isinstance(roster, (str, bytes)):
        return
    if isinstance(roster, Iterable):
        for item in roster:
            if isinstance(item, str):
                if item.strip():
                    yield item.strip(), 1
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                name, value = item
                count = _as_capacity(value)
                if isinstance(name, str) and name.strip() and count is not None:
                    yield name.strip(), count


def _as_capacity(value: object) -> int | None:
    """A capacity as a non-negative int, or None when the value is not one."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str):
        try:
            return max(int(value.strip()), 0)
        except ValueError:
            return None
    return None
