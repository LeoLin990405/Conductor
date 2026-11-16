"""skills_hub.status — link, copy, manual, missing, and drift."""

from __future__ import annotations

from pathlib import Path

from src.skills_hub.adopt import apply_adopt
from src.skills_hub.core import load_manifest
from src.skills_hub.status import status_report


def make_skill(root: Path, name: str, body: str = "body") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill


def _rows(report: dict, agent: str) -> dict[str, dict]:
    return {row["name"]: row for row in report["agents"][agent]["skills"]}


def test_apply_then_status_has_no_drift(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    codex = home / ".codex" / "skills"
    make_skill(claude, "widget", "same")
    make_skill(codex, "widget", "same")
    notes = claude / "notes"
    notes.mkdir(parents=True)
    (notes / "README.md").write_text("ignore", encoding="utf-8")

    apply_adopt(home, repo, [("claude", claude), ("codex", codex)])
    report = status_report(home, repo, [("claude", claude), ("codex", codex)])

    for agent in ("claude", "codex"):
        row = _rows(report, agent)["widget"]
        assert row["how"] == "link"
        assert row["drift"] is False
        assert row["conflict"] is False
    assert "notes" not in _rows(report, "claude")


def test_copy_mode_status_has_no_drift(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    make_skill(claude, "widget", "body")

    apply_adopt(home, repo, [("claude", claude)], link_mode="copy")
    report = status_report(home, repo, [("claude", claude)])

    row = _rows(report, "claude")["widget"]
    assert row == {"name": "widget", "how": "copy", "drift": False, "conflict": False}
    assert not (claude / "widget").is_symlink()


def test_manual_missing_and_broken_link(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    make_skill(claude, "widget", "body")
    apply_adopt(home, repo, [("claude", claude)])

    manual = claude / "extra"
    manual.mkdir()
    (manual / "SKILL.md").write_text(
        "---\nname: extra\ndescription: extra\n---\n\nhand made\n",
        encoding="utf-8",
    )
    (claude / "widget").unlink()
    (claude / "widget").symlink_to(repo / "does-not-exist")

    manifest = load_manifest(repo)
    manifest["gone"] = {"agents": ["claude"], "content_hash": "abc", "conflict": False}
    from src.skills_hub.core import write_manifest

    write_manifest(repo, manifest)

    report = status_report(home, repo, [("claude", claude)])
    rows = _rows(report, "claude")

    assert rows["extra"]["how"] == "manual"
    assert rows["extra"]["drift"] is False
    assert rows["widget"]["how"] == "link"
    assert rows["widget"]["drift"] is True
    assert rows["gone"]["how"] == "missing"
    assert rows["gone"]["drift"] is False


def test_status_does_not_follow_a_symlink_escape(
    home: Path, repo: Path, tmp_path: Path
) -> None:
    claude = home / ".claude" / "skills"
    claude.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("secret body\n", encoding="utf-8")
    (claude / "widget").symlink_to(outside)

    report = status_report(home, repo, [("claude", claude)])

    assert _rows(report, "claude") == {}


def test_conflict_is_marked_and_neither_body_drifts(home: Path, repo: Path) -> None:
    claude = home / ".claude" / "skills"
    codex = home / ".codex" / "skills"
    make_skill(claude, "widget", "from-claude")
    make_skill(codex, "widget", "from-codex")

    apply_adopt(home, repo, [("claude", claude), ("codex", codex)])
    report = status_report(home, repo, [("claude", claude), ("codex", codex)])

    for agent in ("claude", "codex"):
        row = _rows(report, agent)["widget"]
        assert row["how"] == "link"
        assert row["drift"] is False
        assert row["conflict"] is True
