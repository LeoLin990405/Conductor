"""The composer's slash triggers are declared in the frontend and mirrored in
Python. Nothing at runtime reads both, so only this test stops them drifting
when a seventh composer command lands.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.server.services.user_skills.validate import (
    COMPOSER_COMMANDS,
    reserved_skill_names,
)

HELPERS = (
    Path(__file__).resolve().parents[5]
    / "web/src/components/ui/chat-input.helpers.tsx"
)


def _declared_triggers() -> set[str]:
    """Composer-owned names and aliases inside the built-in command array.

    Navigation entries are client-only route changes; they are not skill-name
    triggers and are intentionally outside the Python reservation set. The
    array may carry a TypeScript type annotation between its name and ``=``.
    """
    src = HELPERS.read_text()
    m = re.search(
        r"BUILTIN_SLASH_COMMANDS(?:\s*:[^=]+)?\s*=\s*\[(.*?)\n\];",
        src,
        re.DOTALL,
    )
    assert m, "BUILTIN_SLASH_COMMANDS array literal not found"
    body = m.group(1)
    triggers: set[str] = set()
    for entry in re.findall(r"\{([^{}]*)\}", body):
        kind = re.search(r"type:\s*'([^']+)'", entry)
        if kind and kind.group(1) not in {"action", "subagent"}:
            continue
        name = re.search(r"name:\s*'([^']+)'", entry)
        if name:
            triggers.add(name.group(1))
        aliases = re.search(r"aliases:\s*\[([^\]]*)\]", entry)
        if aliases:
            triggers.update(re.findall(r"'([^']+)'", aliases.group(1)))
    return triggers


@pytest.mark.skipif(not HELPERS.is_file(), reason="frontend tree not present")
def test_python_mirror_matches_the_frontend_declaration():
    assert _declared_triggers() == set(COMPOSER_COMMANDS)


def test_composer_triggers_are_reserved_against_user_skills():
    assert COMPOSER_COMMANDS <= reserved_skill_names()


def test_every_shipped_skill_name_is_reserved():
    """A name a bundle ships must be one a user upload cannot take.

    The sync is last-source-wins, so an upload that takes a shipped name is
    the copy the agent loads afterwards. This is a directory listing on both
    sides on purpose: a registry-based check passes for the skills that
    registered and says nothing about the ones that did not, which is the
    half this guard exists for.
    """
    from src.config.runtime.plugins import bundled_skill_dirs

    shipped = {
        p.name
        for d in bundled_skill_dirs()
        for p in d.iterdir()
        if p.is_dir() and (p / "SKILL.md").is_file()
    }
    assert shipped, "no shipped skills found; the lookup moved"
    assert shipped <= reserved_skill_names()


def test_a_configured_drop_in_root_is_the_one_that_gets_reserved(
    tmp_path, monkeypatch
):
    """Reservation reads the operator's directory, not the field default.

    Delivery honours ``skills.user_skills_dir``, so reservation has to as
    well: a name reserved against the wrong root is a name a user can take,
    and the sync then overwrites the operator's copy with theirs.
    """
    from src.server.services import agent_config_state

    (tmp_path / "operators-own-skill").mkdir()
    monkeypatch.setattr(
        agent_config_state,
        "_agent_config",
        SimpleNamespace(skills=SimpleNamespace(user_skills_dir=str(tmp_path))),
        raising=False,
    )
    # No cache to clear either side: the directory listing is read on every
    # call, because the operator's root is writable while the server runs.
    assert "operators-own-skill" in reserved_skill_names()


def test_the_preview_shows_the_skill_the_operator_actually_delivers(
    tmp_path, monkeypatch
):
    """Management preview and delivery resolve a shipped skill the same way.

    ``user_skills_dir`` is last-wins, so an operator can replace a shipped
    skill by dropping one of the same name into it. A preview that reads the
    field default instead shows the shipped instructions for a skill the agent
    runs the operator's version of, which is the one mismatch this page must
    not have.
    """
    from ptc_agent.agent.middleware.skills.content import load_skill_content
    from src.server.services.skills_core.registry import get_skill_registry
    from src.server.services import agent_config_state
    from src.server.services.user_skills.validate import configured_skill_dirs

    registry = get_skill_registry()
    shipped = next(n for n in registry if registry[n].source_dir is None)
    (tmp_path / shipped).mkdir()
    (tmp_path / shipped / "SKILL.md").write_text("# OPERATOR OVERRIDE")

    monkeypatch.setattr(
        agent_config_state,
        "_agent_config",
        SimpleNamespace(skills=SimpleNamespace(user_skills_dir=str(tmp_path))),
        raising=False,
    )
    body = load_skill_content(
        shipped, [str(d) for d in configured_skill_dirs()], registry=registry
    )
    assert body is not None and "OPERATOR OVERRIDE" in body
