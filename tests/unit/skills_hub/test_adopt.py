"""skills_hub.adopt — dedupe, conflicts, and a snapshot before any write."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.skills_hub import adopt
from src.skills_hub.adopt import apply_adopt, plan_adopt, require_skill_name
from src.skills_hub.core import SkillsError
from src.skills_hub.snapshots import list_snapshots


def make_skill(root: Path, name: str, body: str = "body") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill


def _files(root: Path) -> list[tuple[str, str, bytes | str]]:
    if not root.exists():
        return []
    found: list[tuple[str, str, bytes | str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            if path.is_symlink():
                found.append((rel, "link", os.readlink(path)))
            elif path.is_file():
                found.append((rel, "file", path.read_bytes()))
    return found


def test_dedupe_identical_trees_share_one_central_copy(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    codex = home / ".codex" / "skills"
    make_skill(claude, "widget", "same")
    make_skill(codex, "widget", "same")
    # Same bytes under another directory name still collapse to one tree.
    alias = codex / "alias"
    alias.mkdir(parents=True)
    (alias / "SKILL.md").write_bytes((claude / "widget" / "SKILL.md").read_bytes())

    apply_adopt(home, repo, [("claude", claude), ("codex", codex)])

    central = sorted(
        p.name for p in repo.iterdir() if p.is_dir() and (p / "SKILL.md").is_file()
    )
    assert central == ["widget"]
    assert (claude / "widget").resolve() == (repo / "widget").resolve()
    assert (codex / "widget").resolve() == (repo / "widget").resolve()
    assert (codex / "alias").resolve() == (repo / "widget").resolve()
    assert (claude / "widget").is_symlink()
    assert (codex / "alias").is_symlink()


def test_dedup_alias_install_and_copy_rollback(home: Path, repo: Path) -> None:
    """``beta`` shares ``alpha``. Install and copy rollback use that directory."""
    import shutil

    from src.skills_hub.manager import install
    from src.skills_hub.snapshots import rollback_snapshot

    claude = home / ".claude" / "skills"
    codex = home / ".codex" / "skills"
    marker = "---\nname: shared\n---\n\nidentical body\n"
    make_skill(claude, "alpha", "same body")
    make_skill(codex, "beta", "same body")
    (claude / "alpha" / "SKILL.md").write_text(marker, encoding="utf-8")
    (codex / "beta" / "SKILL.md").write_text(marker, encoding="utf-8")

    adopted = apply_adopt(
        home, repo, [("claude", claude), ("codex", codex)], link_mode="copy"
    )

    assert (repo / "alpha" / "SKILL.md").is_file()
    assert not (repo / "beta").exists()
    assert "identical body" in (repo / "alpha" / "SKILL.md").read_text(encoding="utf-8")
    shutil.rmtree(codex / "beta")
    installed = install(repo, [("codex", codex)], ["beta"], mode="copy")
    assert ("codex", "beta") in installed.installed
    copied = (codex / "beta" / "SKILL.md").read_text(encoding="utf-8")
    assert copied == marker
    assert "Removed by a Conductor skills rollback" not in copied

    shutil.rmtree(codex / "beta")
    rollback_snapshot(
        repo,
        adopted["snapshot"],
        skills_root=repo,
        allowed_dirs={claude, codex},
    )
    restored = (codex / "beta" / "SKILL.md").read_text(encoding="utf-8")
    assert restored == marker
    assert not (codex / "beta").is_symlink()
    assert "Removed by a Conductor skills rollback" not in restored


def test_dedup_alias_installs_as_symlink(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    codex = home / ".codex" / "skills"
    marker = "---\nname: shared\n---\n\nidentical body\n"
    make_skill(claude, "alpha", "same body")
    make_skill(codex, "beta", "same body")
    (claude / "alpha" / "SKILL.md").write_text(marker, encoding="utf-8")
    (codex / "beta" / "SKILL.md").write_text(marker, encoding="utf-8")
    apply_adopt(home, repo, [("claude", claude), ("codex", codex)])
    (codex / "beta").unlink()

    from src.skills_hub.manager import install

    installed = install(repo, [("codex", codex)], ["beta"], mode="symlink")

    assert ("codex", "beta") in installed.installed
    assert (codex / "beta").is_symlink()
    assert (codex / "beta" / "SKILL.md").read_text(encoding="utf-8") == marker


def test_conflict_keeps_both_bodies(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    codex = home / ".codex" / "skills"
    make_skill(claude, "widget", "from-claude")
    make_skill(codex, "widget", "from-codex")

    plan = plan_adopt(home, repo, [("claude", claude), ("codex", codex)])
    assert plan.conflicts == ("widget",)
    assert {item.central_name for item in plan.items} != {"widget"}

    apply_adopt(home, repo, [("claude", claude), ("codex", codex)])

    bodies = {
        path.name: (path / "SKILL.md").read_text(encoding="utf-8")
        for path in repo.iterdir()
        if path.is_dir() and (path / "SKILL.md").is_file()
    }
    assert any("from-claude" in text for text in bodies.values())
    assert any("from-codex" in text for text in bodies.values())
    assert "widget" in bodies
    suffixed = [name for name in bodies if name.startswith("widget--")]
    assert len(suffixed) == 1
    assert len(suffixed[0].split("--", 1)[1]) >= 8
    assert (claude / "widget").resolve() != (codex / "widget").resolve()
    assert (claude / "widget").is_symlink()
    assert (codex / "widget").is_symlink()


def test_sync_relinks_deduped_variant_and_rollback_keeps_it(
    home: Path, repo: Path
) -> None:
    claude = home / ".claude" / "skills"
    codex = home / ".codex" / "skills"
    make_skill(claude, "widget", "body-A")
    make_skill(codex, "widget", "body-B")
    apply_adopt(home, repo, [("claude", claude), ("codex", codex)])
    variant = next(
        path.name
        for path in repo.iterdir()
        if path.name.startswith("widget--") and (path / "SKILL.md").is_file()
    )
    link = codex / "widget"
    assert variant in os.readlink(link)
    link.unlink()

    from src.skills_hub.manager import sync
    from src.skills_hub.snapshots import rollback_snapshot

    sync(repo, [("claude", claude), ("codex", codex)])
    assert variant in os.readlink(codex / "widget")
    assert "body-B" in (codex / "widget" / "SKILL.md").read_text(encoding="utf-8")
    (codex / "widget").unlink()
    snaps = list_snapshots(repo)
    rollback_snapshot(
        repo,
        snaps[0]["id"],
        skills_root=repo,
        allowed_dirs={claude, codex},
    )
    assert "body-B" in (codex / "widget" / "SKILL.md").read_text(encoding="utf-8")
    assert "body-A" in (repo / "widget" / "SKILL.md").read_text(encoding="utf-8")
    assert "body-B" in (repo / variant / "SKILL.md").read_text(encoding="utf-8")


def test_plan_adopt_writes_nothing(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    skill = make_skill(claude, "widget", "body")
    (skill / "note.txt").write_text("keep", encoding="utf-8")
    before = (_files(repo), _files(claude))

    plan = plan_adopt(home, repo, [("claude", claude)])

    assert plan.items and plan.items[0].name == "widget"
    assert (_files(repo), _files(claude)) == before
    assert not (repo / "snapshots").exists()
    assert not (repo / "manifest.json").exists()


def test_non_skill_directory_is_left_alone(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    make_skill(claude, "widget", "body")
    notes = claude / "notes"
    notes.mkdir(parents=True)
    (notes / "README.md").write_text("not a skill", encoding="utf-8")

    apply_adopt(home, repo, [("claude", claude)])

    assert (notes / "README.md").read_text(encoding="utf-8") == "not a skill"
    assert not (notes).is_symlink()
    assert not (repo / "notes").exists()


def test_symlink_escape_is_refused(home: Path, repo: Path, tmp_path: Path) -> None:
    claude = home / ".claude" / "skills"
    claude.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("secret body\n", encoding="utf-8")
    (claude / "widget").symlink_to(outside)

    with pytest.raises(SkillsError, match="symlink"):
        plan_adopt(home, repo, [("claude", claude)])

    assert not (repo / "widget").exists()
    assert not repo.exists() or "secret body" not in _joined(repo)


def test_intermediate_source_symlink_is_refused(
    home: Path, repo: Path, tmp_path: Path
) -> None:
    claude = home / ".claude" / "skills"
    claude.mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "inner").mkdir(parents=True)
    (outside / "inner" / "SKILL.md").write_text(
        "---\nname: leak\ndescription: leak\n---\n\nOUTSIDE_MARKER\n",
        encoding="utf-8",
    )
    container = claude / "container"
    container.mkdir()
    (container / "bridge").symlink_to(outside)
    (claude / "leak").symlink_to("container/bridge/inner")

    with pytest.raises(SkillsError, match="symlink"):
        plan_adopt(home, repo, [("claude", claude)])

    assert not (repo / "leak").exists()
    assert "OUTSIDE_MARKER" not in _joined(repo)


def test_internal_symlink_escape_is_refused(
    home: Path, repo: Path, tmp_path: Path
) -> None:
    claude = home / ".claude" / "skills"
    skill = make_skill(claude, "widget", "body")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("topsecret", encoding="utf-8")
    (skill / "leak").symlink_to(outside / "secret.txt")

    with pytest.raises(SkillsError, match="symlink"):
        plan_adopt(home, repo, [("claude", claude)])

    assert not (repo / "widget").exists()


def test_parent_traversal_name_is_refused() -> None:
    with pytest.raises(SkillsError, match="refusing"):
        require_skill_name("../secret")


def test_name_containing_dotdot_is_refused(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    weird = claude / "foo..bar"
    weird.mkdir(parents=True)
    (weird / "SKILL.md").write_text("---\nname: x\n---\n\nbody\n", encoding="utf-8")

    with pytest.raises(SkillsError, match="refusing"):
        plan_adopt(home, repo, [("claude", claude)])

    assert not repo.exists() or not (repo / "foo..bar").exists()


def test_interrupted_apply_restores_agent_dir(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claude = home / ".claude" / "skills"
    skill = make_skill(claude, "widget", "original")
    (skill / "note.txt").write_text("keep-me", encoding="utf-8")
    (skill / ".DS_Store").write_bytes(b"ds-store")
    before = _files(skill)
    events: list[str] = []
    real_snapshot = adopt.create_snapshot
    real_copy = adopt.shutil.copytree

    def wrapped_snapshot(*args, **kwargs):
        snap = real_snapshot(*args, **kwargs)
        events.append("snapshot")
        return snap

    def wrapped_copy(*args, **kwargs):
        assert events and events[0] == "snapshot"
        events.append("write")
        return real_copy(*args, **kwargs)

    def fail_symlink(*args, **kwargs):
        events.append("symlink")
        raise IOError("injected failure")

    monkeypatch.setattr(adopt, "create_snapshot", wrapped_snapshot)
    monkeypatch.setattr(adopt.shutil, "copytree", wrapped_copy)
    monkeypatch.setattr(adopt.os, "symlink", fail_symlink)

    with pytest.raises(IOError, match="injected"):
        apply_adopt(home, repo, [("claude", claude)])

    assert events[0] == "snapshot"
    assert "write" in events
    assert _files(skill) == before
    assert not skill.is_symlink()
    assert list_snapshots(repo)
    assert not list(claude.glob(".*.adoptbak-*"))


def _joined(root: Path) -> str:
    parts: list[str] = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)
