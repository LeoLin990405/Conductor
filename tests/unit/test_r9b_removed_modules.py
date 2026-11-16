"""R9b guard: modules retired by R9b must stay gone."""

from __future__ import annotations

import importlib.util

import pytest

# L12: target modules that no entry reached (ptc-audit "reached by no entry").
REMOVED_L12 = [
    "src.ptc_agent.agent.tools.utils",
    "src.ptc_agent.core.security",
]

REMOVED = REMOVED_L12


@pytest.mark.parametrize("dotted", REMOVED)
def test_retired_module_is_gone(dotted: str) -> None:
    assert importlib.util.find_spec(dotted) is None
