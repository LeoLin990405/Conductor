"""Canonical three-step plan shared by the mock deliberation adapters."""

from __future__ import annotations

import copy


CANONICAL_PLAN = {
    "summary": "canonical",
    "steps": [
        {
            "step_key": "spec",
            "title": "Write the spec",
            "prompt": "Write a short spec.",
            "assigned_agent": "codex",
            "depends_on": [],
        },
        {
            "step_key": "impl",
            "title": "Implement",
            "prompt": "Implement the spec.",
            "assigned_agent": "kimi",
            "depends_on": [],
        },
        {
            "step_key": "review",
            "title": "Review",
            "prompt": "Review the implementation.",
            "assigned_agent": "grok",
            "depends_on": ["spec", "impl"],
        },
    ],
}


def copy_plan() -> dict:
    return copy.deepcopy(CANONICAL_PLAN)
