"""skills_hub.presets — named bundles and per-project/per-line role sets."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.skills_hub import presets
from src.skills_hub.core import SkillsError, write_manifest


def make_skill(repo: Path, name: str, roles: list[str] | None = None) -> None:
    skill = repo / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\nbody\n", encoding="utf-8"
    )
    if roles:
        manifest = presets.load_manifest(repo)
        record = manifest.setdefault(name, {})
        record["roles"] = roles
        write_manifest(repo, manifest)


@pytest.fixture
def populated(repo: Path) -> Path:
    make_skill(repo, "alpha")
    make_skill(repo, "beta")
    make_skill(repo, "review-only", roles=["review"])
    return repo


def test_save_and_list_presets(populated: Path) -> None:
    presets.save_preset(populated, "core", ["alpha", "beta"], description="core set")

    listed = presets.list_presets(populated)

    assert [p.name for p in listed] == ["core"]
    assert listed[0].skills == ("alpha", "beta")
    assert listed[0].description == "core set"


def test_save_preset_rejects_unknown_skills(populated: Path) -> None:
    with pytest.raises(SkillsError, match="unknown skills"):
        presets.save_preset(populated, "bad", ["nope"])


def test_save_preset_rejects_empty(populated: Path) -> None:
    with pytest.raises(SkillsError, match="at least one skill"):
        presets.save_preset(populated, "empty", [])


def test_get_and_delete_preset(populated: Path) -> None:
    presets.save_preset(populated, "core", ["alpha"])

    assert presets.get_preset(populated, "core") is not None
    assert presets.get_preset(populated, "missing") is None

    assert presets.delete_preset(populated, "core") is True
    assert presets.delete_preset(populated, "core") is False


def test_project_sets_per_line_and_role(populated: Path) -> None:
    presets.set_project_skills(populated, "conductor", ["alpha"], line="default")
    presets.set_project_skills(
        populated, "conductor", ["alpha", "review-only"], line="r2", role="review"
    )

    default = presets.get_project_skills(populated, "conductor")
    r2 = presets.get_project_skills(populated, "conductor", line="r2")

    assert default is not None and default.skills == ("alpha",)
    assert r2 is not None and r2.role == "review"
    assert "review-only" in r2.skills


def test_effective_skills_filter_by_role(populated: Path) -> None:
    presets.set_project_skills(
        populated, "conductor", ["alpha", "review-only"], role="review"
    )

    review = presets.effective_skills(populated, "conductor")
    dev = presets.effective_skills(populated, "conductor", role="dev")

    # review-only carries roles=["review"]; unrestricted alpha passes both
    assert "review-only" in review
    assert "review-only" not in dev
    assert "alpha" in dev


def test_effective_skills_falls_back_to_default_line(populated: Path) -> None:
    presets.set_project_skills(populated, "conductor", ["alpha"])

    skills = presets.effective_skills(populated, "conductor", line="other-line")

    assert skills == ["alpha"]


def test_line_names_are_validated(populated: Path) -> None:
    with pytest.raises(SkillsError, match="invalid line name"):
        presets.set_project_skills(populated, "conductor", ["alpha"], line="Bad Line")


def test_list_projects(populated: Path) -> None:
    presets.set_project_skills(populated, "conductor", ["alpha"], line="default")
    presets.set_project_skills(populated, "conductor", ["beta"], line="r2")

    projects = presets.list_projects(populated)

    assert projects == [
        {"project": "conductor", "lines": ["default", "r2"]}
    ]
