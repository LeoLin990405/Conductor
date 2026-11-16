"""skills_hub.importer — local folder and zip imports, budgets and safety."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from src.skills_hub import importer
from src.skills_hub.core import SkillsError, discover_skills, load_manifest


def make_skill(root: Path, name: str, body: str = "hello") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill


def test_import_from_local_bundle(repo: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    make_skill(bundle, "alpha")
    make_skill(bundle, "beta")

    result = importer.import_source(repo, str(bundle))

    assert result.imported == ("alpha", "beta")
    assert sorted(e.name for e in discover_skills(repo)) == ["alpha", "beta"]
    manifest = load_manifest(repo)
    assert manifest["alpha"]["source"] == str(bundle)


def test_import_single_skill_directory(repo: Path, tmp_path: Path) -> None:
    skill = make_skill(tmp_path / "solo", "solo-skill")

    result = importer.import_source(repo, str(skill))

    assert result.imported == ("solo-skill",)
    assert (repo / "solo-skill" / "SKILL.md").is_file()


def test_import_wrapped_zip_layout(repo: Path, tmp_path: Path) -> None:
    stage = tmp_path / "stage" / "wrapper"
    make_skill(stage, "zipped")
    zip_path = tmp_path / "bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path in sorted((tmp_path / "stage").rglob("*")):
            archive.write(path, path.relative_to(tmp_path / "stage"))

    result = importer.import_source(repo, str(zip_path))

    assert result.imported == ("zipped",)


def test_import_skips_existing_names_without_force(repo: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    make_skill(bundle, "alpha", body="first")
    assert importer.import_source(repo, str(bundle)).imported == ("alpha",)

    make_skill(tmp_path / "bundle2", "alpha", body="second")
    result = importer.import_source(repo, str(tmp_path / "bundle2"))

    assert result.skipped == ("alpha",)
    assert "first" in (repo / "alpha" / "SKILL.md").read_text()


def test_import_force_replaces_existing(repo: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    make_skill(bundle, "alpha", body="first")
    importer.import_source(repo, str(bundle))

    make_skill(tmp_path / "bundle2", "alpha", body="second")
    result = importer.import_source(repo, str(tmp_path / "bundle2"), force=True)

    assert result.imported == ("alpha",)
    assert "second" in (repo / "alpha" / "SKILL.md").read_text()


def test_import_rejects_invalid_skill_names(repo: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    (bundle / "Not_A_Skill").mkdir(parents=True)
    (bundle / "Not_A_Skill" / "SKILL.md").write_text("x")
    make_skill(bundle, "fine")

    result = importer.import_source(repo, str(bundle))

    assert result.imported == ("fine",)
    assert result.skipped == ("Not_A_Skill",)


def test_import_rejects_zip_entry_traversal(repo: Path, tmp_path: Path) -> None:
    zip_path = tmp_path / "evil.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("../escape.txt", "nope")

    with pytest.raises(SkillsError, match="escapes"):
        importer.import_source(repo, str(zip_path))


def test_import_rejects_oversized_skill(repo: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    skill = make_skill(bundle, "big")
    big = skill / "big.bin"
    big.write_bytes(b"x" * (importer.MAX_SKILL_BYTES + 1))

    with pytest.raises(SkillsError, match="budget"):
        importer.import_source(repo, str(bundle))


def test_import_missing_local_source_raises(repo: Path, tmp_path: Path) -> None:
    with pytest.raises(SkillsError, match="not a directory"):
        importer.import_source(repo, str(tmp_path / "nowhere"))


def test_git_urls_are_detected() -> None:
    assert importer.is_git_url("https://github.com/org/repo")
    assert importer.is_git_url("git@github.com:org/repo.git")
    assert importer.is_git_url("ssh://git@host/repo")
    assert not importer.is_git_url("/local/path")
    assert not importer.is_git_url("relative/path")
    assert not importer.is_git_url("")


def test_remove_skill_keeps_manifest_agents(repo: Path, tmp_path: Path) -> None:
    # The record survives removal so a later sync can clean up agent entries
    # that still point at the deleted skill.
    bundle = tmp_path / "bundle"
    make_skill(bundle, "alpha")
    importer.import_source(repo, str(bundle))

    importer.remove_skill(repo, "alpha")

    assert not (repo / "alpha").exists()
    assert "alpha" in load_manifest(repo)


# ---------------------------------------------------------------------------
# Regression: BLOCK review findings (secrets / force ordering / git URL)
# ---------------------------------------------------------------------------


def test_import_preserves_an_external_symlink_without_its_target(repo: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret-token"
    secret.write_text("SUPERSECRET", encoding="utf-8")
    bundle = tmp_path / "bundle"
    skill = make_skill(bundle, "linky")
    (skill / "leak.txt").symlink_to(secret)

    result = importer.import_source(repo, str(bundle))

    assert result.imported == ("linky",)
    installed = repo / "linky" / "leak.txt"
    assert installed.is_symlink()
    # The link is copied as a link, never followed: the target's bytes do not
    # enter the repo, and nothing beyond the link itself is materialized.
    assert not (repo / "linky" / "secret-token").exists()
    assert "SUPERSECRET" not in "".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in repo.rglob("*")
        if p.is_file() and not p.is_symlink()
    )


def test_import_force_keeps_existing_when_budget_fails(repo: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    make_skill(bundle, "alpha", body="first")
    assert importer.import_source(repo, str(bundle)).imported == ("alpha",)

    bad = tmp_path / "bad"
    skill = make_skill(bad, "alpha", body="second")
    (skill / "huge.bin").write_bytes(b"x" * (importer.MAX_SKILL_BYTES + 1))

    with pytest.raises(SkillsError, match="budget"):
        importer.import_source(repo, str(bad), force=True)

    # The pre-existing skill must survive a failed --force import.
    assert "first" in (repo / "alpha" / "SKILL.md").read_text(encoding="utf-8")


def test_manifest_source_is_redacted(repo: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    make_skill(bundle, "alpha")
    # Simulate a credential-bearing source string without cloning.
    result = importer.import_source(repo, str(bundle))
    assert result.imported == ("alpha",)

    from src.skills_hub.core import redact_source

    raw = "https://user:hunter2@example.com/org/repo.git"
    assert redact_source(raw) == "https://example.com/org/repo.git"
    assert "hunter2" not in redact_source(raw)


def test_file_uri_is_not_a_git_url() -> None:
    from src.skills_hub.core import is_git_url

    assert not is_git_url("file:///Users/you/.ssh")
    assert not is_git_url("file:/tmp/repo")
    assert is_git_url("https://github.com/org/repo")
    assert is_git_url("git@github.com:org/repo.git")


def test_import_force_keeps_existing_skill_when_budget_exceeded(
    repo: Path, tmp_path: Path
) -> None:
    bundle = tmp_path / "bundle"
    make_skill(bundle, "alpha", body="first")
    importer.import_source(repo, str(bundle))

    replacement = make_skill(tmp_path / "bundle2", "alpha", body="second")
    (replacement / "big.bin").write_bytes(b"x" * (importer.MAX_SKILL_BYTES + 1))
    with pytest.raises(SkillsError, match="budget"):
        importer.import_source(repo, str(tmp_path / "bundle2"), force=True)

    # The previous skill is untouched and no staging copy is left behind.
    assert "first" in (repo / "alpha" / "SKILL.md").read_text()
    leftovers = [p.name for p in repo.iterdir() if p.name.startswith(".stage-")]
    assert leftovers == []


def test_import_preserves_symlinks_and_stays_in_budget(
    repo: Path, tmp_path: Path
) -> None:
    # A link pointing at a huge tree outside the skill must not be followed:
    # neither the budget walk nor the copy may pull the external tree in.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "huge.bin").write_bytes(b"x" * (importer.MAX_SKILL_BYTES + 1))
    bundle = tmp_path / "bundle"
    skill = make_skill(bundle, "linked")
    (skill / "external").symlink_to(outside, target_is_directory=True)

    result = importer.import_source(repo, str(bundle))

    assert result.imported == ("linked",)
    assert (repo / "linked" / "external").is_symlink()
    assert not (repo / "linked" / "huge.bin").exists()


def test_import_budget_walk_ignores_symlink_loops(repo: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    skill = make_skill(bundle, "loopy")
    (skill / "a").symlink_to(skill / "b")
    (skill / "b").symlink_to(skill / "a")

    result = importer.import_source(repo, str(bundle))

    assert result.imported == ("loopy",)
    assert (repo / "loopy" / "a").is_symlink()


def test_import_rejects_reserved_hub_names(repo: Path, tmp_path: Path) -> None:
    # "snapshots"/"presets"/"projects" are the hub's own subdirectories; a
    # skill occupying one would sit inside those stores (and could plant fake
    # snapshot JSON), so imports must skip them.
    bundle = tmp_path / "bundle"
    make_skill(bundle, "snapshots")
    make_skill(bundle, "presets")
    make_skill(bundle, "projects")
    make_skill(bundle, "fine")

    result = importer.import_source(repo, str(bundle))

    assert result.imported == ("fine",)
    assert sorted(result.skipped) == ["presets", "projects", "snapshots"]
    assert not (repo / "snapshots").exists()
    assert not (repo / "presets").exists()
    assert not (repo / "projects").exists()


# --- credentials in error text --------------------------------------------


class _FailedClone:
    """The shape of a ``subprocess.run`` result for a failed clone."""

    returncode = 128
    stdout = ""
    stderr = (
        "remote: Invalid username or password.\n"
        "fatal: Authentication failed for "
        "'https://deploy:hunter2@git.example.com/org/repo.git/'\n"
    )


def test_clone_failure_never_quotes_the_url_credentials(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git echoes the remote it was given back on an auth failure, so the
    # password rides in stderr; it must not reach the error message (which the
    # CLI prints and the operator may paste into a ticket).
    monkeypatch.setattr(importer.subprocess, "run", lambda *a, **k: _FailedClone())

    with pytest.raises(SkillsError) as excinfo:
        importer.import_source(
            repo, "https://deploy:hunter2@git.example.com/org/repo.git"
        )

    message = str(excinfo.value)
    assert "hunter2" not in message
    assert "deploy:" not in message
    # The remote is still named: a refusal you cannot diagnose is not safe.
    assert "git.example.com/org/repo.git" in message


def test_clone_failure_scrubs_a_token_in_the_userinfo(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _TokenInUserinfo:
        returncode = 128
        stdout = ""
        stderr = (
            "fatal: Authentication failed for "
            "'https://ghp_" "AbCdEf0123456789xyz@github.com/org/repo.git/'\n"
        )

    monkeypatch.setattr(importer.subprocess, "run", lambda *a, **k: _TokenInUserinfo())

    with pytest.raises(SkillsError) as excinfo:
        importer.import_source(
            repo, "https://ghp_" "AbCdEf0123456789xyz@github.com/org/repo.git"
        )

    assert "ghp_" "AbCdEf0123456789xyz" not in str(excinfo.value)


def test_redact_masks_secret_shapes_and_keeps_the_diagnosis() -> None:
    assert importer.redact("https://host/repo.git?access_token=SUPERSECRET&ref=main") == (
        "https://host/repo.git?access_token=[REDACTED]&ref=main"
    )
    assert importer.redact("Authorization: Bearer abcdefghijklmnop") == (
        "Authorization: Bearer [REDACTED]"
    )
    assert importer.redact("sk-" "ant-api03-1234567890abcdef") == "[REDACTED]"
    # A remote with no credentials in it is quoted verbatim.
    plain = "fatal: repository https://github.com/org/repo.git not found"
    assert importer.redact(plain) == plain


def test_missing_zip_source_error_is_scrubbed(repo: Path, tmp_path: Path) -> None:
    # The zip branch quotes the source string too, so it carries the same
    # scrub. Called directly: a ``.zip`` URL is routed to the clone by
    # ``is_git_url``, which would need a git stub to reach here.
    with pytest.raises(SkillsError) as excinfo:
        importer._unzip("https://host/bundle.zip?token=SUPERSECRET", tmp_path)

    assert "SUPERSECRET" not in str(excinfo.value)
