"""``conductor skills ...`` — CLI wiring over the skills_hub package."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest


from src.conductor_cli.cli import main
from src.skills_hub.core import load_manifest
from src.skills_hub.snapshots import list_snapshots


def make_skill(repo: Path, name: str) -> None:
    skill = repo / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\nbody\n", encoding="utf-8"
    )


def run_cli(argv: list[str], home: Path) -> tuple[int, dict | str]:
    import io
    import contextlib

    stdout = io.StringIO()
    with contextlib.redirect_stdout(stdout):
        code = main(argv)
    text = stdout.getvalue().strip()
    try:
        return code, json.loads(text)
    except json.JSONDecodeError:
        return code, text


def test_cli_runs_locally_without_a_server(home: Path) -> None:
    # No CONDUCTOR_URL and no server: the skills command must not try to
    # reach one.
    code, _ = run_cli(["skills", "list"], home)
    assert code == 0


def test_cli_seed_list_install_uninstall(home: Path) -> None:
    code, seeded = run_cli(["skills", "seed-fleet-rules"], home)
    assert code == 0 and seeded["seeded"] is True

    code, listed = run_cli(["skills", "list"], home)
    assert code == 0
    assert [s["name"] for s in listed["skills"]] == ["fleet-rules"]

    code, installed = run_cli(
        ["skills", "install", "fleet-rules", "--agent", "zcode", "--agent", "shared", "--apply"],
        home,
    )
    assert code == 0
    assert installed["snapshot"], "every install reports its snapshot"
    assert (home / ".zcode" / "skills" / "fleet-rules" / "SKILL.md").is_file()
    assert (home / ".agents" / "skills" / "fleet-rules").is_symlink()

    code, uninstalled = run_cli(
        ["skills", "uninstall", "fleet-rules", "--agent", "zcode", "--apply"], home
    )
    assert code == 0
    assert not (home / ".zcode" / "skills" / "fleet-rules").exists()
    # shared was not in this uninstall's agent list and must survive
    assert (home / ".agents" / "skills" / "fleet-rules").exists()


def test_cli_import_from_local_folder(home: Path, tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    make_skill(bundle, "alpha")

    code, result = run_cli(["skills", "import", str(bundle)], home)

    assert code == 0
    assert result["imported"] == ["alpha"]


def test_cli_preset_lifecycle_and_install(home: Path) -> None:
    make_skill(home / ".conductor" / "skills", "alpha")
    make_skill(home / ".conductor" / "skills", "beta")

    code, _ = run_cli(["skills", "preset", "save", "core", "alpha", "beta"], home)
    assert code == 0

    code, shown = run_cli(["skills", "preset", "show", "core"], home)
    assert code == 0 and shown["skills"] == ["alpha", "beta"]

    code, installed = run_cli(
        ["skills", "install", "preset:core", "--agent", "claude", "--apply"], home
    )
    assert code == 0
    assert {(i["skill"]) for i in installed["installed"]} == {"alpha", "beta"}


def test_cli_project_set_and_show(home: Path) -> None:
    make_skill(home / ".conductor" / "skills", "alpha")

    code, _ = run_cli(
        [
            "skills",
            "project",
            "set",
            "conductor",
            "alpha",
            "--line",
            "r2",
            "--role",
            "dev",
        ],
        home,
    )
    assert code == 0

    code, shown = run_cli(
        ["skills", "project", "show", "conductor", "--line", "r2"], home
    )
    assert code == 0
    assert shown["skills"] == ["alpha"]


def test_cli_sync_and_rollback(home: Path) -> None:
    make_skill(home / ".conductor" / "skills", "alpha")

    run_cli(["skills", "install", "alpha", "--agent", "codex", "--apply"], home)
    # delete the skill from the repo; sync --prune must remove the stale entry
    import shutil

    shutil.rmtree(home / ".conductor" / "skills" / "alpha")
    load_manifest(home / ".conductor" / "skills")  # manifest record survives

    code, synced = run_cli(["skills", "sync", "--agent", "codex", "--prune", "--apply"], home)
    assert code == 0
    assert synced["uninstalled"] == [{"agent": "codex", "skill": "alpha"}]

    snapshot = synced["snapshot"]
    code, rolled = run_cli(["skills", "rollback", snapshot, "--apply"], home)
    assert code == 0
    assert rolled["restored"] == {"codex": ["alpha"]}


def test_cli_sync_reports_stale_without_prune(home: Path) -> None:
    # Deletion is never the default: a plain sync reports the stale entry and
    # leaves the agent directory and the manifest record alone.
    make_skill(home / ".conductor" / "skills", "alpha")
    run_cli(["skills", "install", "alpha", "--agent", "codex", "--apply"], home)
    import shutil

    shutil.rmtree(home / ".conductor" / "skills" / "alpha")

    code, synced = run_cli(["skills", "sync", "--agent", "codex", "--apply"], home)

    assert code == 0
    assert synced["stale"] == [{"agent": "codex", "skill": "alpha"}]
    assert synced["uninstalled"] == []
    assert (home / ".codex" / "skills" / "alpha").is_symlink()  # entry kept
    assert "alpha" in load_manifest(home / ".conductor" / "skills")


def test_cli_rollback_refuses_foreign_directory(home: Path, tmp_path: Path) -> None:
    # A snapshot that names a directory outside the configured agent dirs is
    # refused; the foreign directory is not created or emptied.
    make_skill(home / ".conductor" / "skills", "alpha")
    run_cli(["skills", "install", "alpha", "--agent", "codex", "--apply"], home)
    latest = list_snapshots(home / ".conductor" / "skills")[0]["id"]
    import json

    path = home / ".conductor" / "skills" / "snapshots" / f"{latest}.json"
    body = json.loads(path.read_text())
    body["agents"]["codex"]["dir"] = str(tmp_path / "elsewhere")
    path.write_text(json.dumps(body))

    code, _ = run_cli(["skills", "rollback", latest, "--apply"], home)

    assert code == 1
    assert not (tmp_path / "elsewhere").exists()


def test_cli_rollback_defaults_to_latest(home: Path) -> None:
    make_skill(home / ".conductor" / "skills", "alpha")
    run_cli(["skills", "install", "alpha", "--agent", "codex", "--apply"], home)
    latest = list_snapshots(home / ".conductor" / "skills")[0]["id"]

    code, rolled = run_cli(["skills", "rollback", "--apply"], home)

    assert code == 0
    assert rolled["snapshot"] == latest


def test_cli_rollback_reports_user_added_skill_without_deleting(home: Path) -> None:
    # What the operator sees: the skill they added themselves is named in the
    # rollback output and still on disk afterwards.
    make_skill(home / ".conductor" / "skills", "alpha")
    run_cli(["skills", "install", "alpha", "--agent", "codex", "--apply"], home)
    latest = list_snapshots(home / ".conductor" / "skills")[0]["id"]

    mine = home / ".codex" / "skills" / "mine"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("UNIQUE-USER-CONTENT")

    code, rolled = run_cli(["skills", "rollback", latest, "--apply"], home)

    assert code == 0
    # The snapshot predates the install, so rollback names both the skill the
    # hub installed afterwards and the user's own — and deletes neither.
    assert rolled["kept"] == {"codex": ["alpha", "mine"]}
    assert (mine / "SKILL.md").read_text() == "UNIQUE-USER-CONTENT"
    assert (home / ".codex" / "skills" / "alpha").is_symlink()


def test_cli_list_uses_toml_agent_override(home: Path) -> None:
    cfg = home / ".config" / "conductor"
    cfg.mkdir(parents=True)
    (cfg / "config.json").write_text(
        '{"agents": {"myagent": ".from-json/skills"}}',
        encoding="utf-8",
    )
    overlay = home / ".conductor"
    overlay.mkdir()
    (overlay / "skills-hub.toml").write_text(
        '[agents]\nmyagent = ".from-toml/skills"\n',
        encoding="utf-8",
    )

    code, listed = run_cli(["skills", "list", "--agent", "myagent", "--json"], home)

    assert code == 0
    blob = json.dumps(listed)
    assert ".from-toml" in blob
    assert ".from-json" not in blob
    code, _ = run_cli(["skills", "list", "--agent", "not-a-real-agent", "--json"], home)
    assert code != 0


def test_cli_unknown_agent_fails_cleanly(home: Path) -> None:
    make_skill(home / ".conductor" / "skills", "alpha")
    code, _ = run_cli(["skills", "install", "alpha", "--agent", "nope", "--apply"], home)
    assert code == 1


def test_cli_install_unknown_skill_fails(home: Path) -> None:
    code, _ = run_cli(["skills", "install", "ghost", "--agent", "codex", "--apply"], home)
    assert code == 1


def test_cli_copy_mode(home: Path) -> None:
    make_skill(home / ".conductor" / "skills", "alpha")

    code, _ = run_cli(
        ["skills", "install", "alpha", "--agent", "kimi", "--mode", "copy", "--apply"], home
    )

    assert code == 0
    entry = home / ".kimi-code" / "skills" / "alpha"
    assert entry.is_dir() and not entry.is_symlink()


def _loads(text: str) -> dict | list | str:
    body = text.strip()
    if not body:
        return ""
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


def run_cli_streams(
    argv: list[str], home: Path
) -> tuple[int, dict | list | str, dict | list | str]:
    import contextlib
    import io

    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(argv)
    return code, _loads(stdout.getvalue()), _loads(stderr.getvalue())


def _agent_skill(home: Path, name: str = "alpha") -> Path:
    skill = home / ".codex" / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n\nbody\n", encoding="utf-8"
    )
    return skill


def test_cli_adopt_dry_run_does_not_write(home: Path) -> None:
    skill = _agent_skill(home)
    code, envelope, err = run_cli_streams(["skills", "adopt", "--json"], home)

    assert code == 0 and err == ""
    assert envelope["exit_code"] == 0
    assert envelope["command"] == "skills adopt"
    assert envelope["errors"] is None
    assert envelope["data"]["dry_run"] is True
    assert envelope["data"]["items"][0]["agent"] == "codex"
    assert envelope["data"]["items"][0]["name"] == "alpha"
    assert not (home / ".conductor" / "skills" / "alpha").exists()
    assert skill.is_dir() and not skill.is_symlink()


def test_cli_adopt_apply_links_into_the_repo(home: Path) -> None:
    _agent_skill(home)
    code, envelope, err = run_cli_streams(
        ["skills", "adopt", "--apply", "--json"], home
    )

    assert code == 0 and err == ""
    assert envelope["data"]["dry_run"] is False
    assert envelope["data"]["snapshot"]
    central = home / ".conductor" / "skills" / "alpha" / "SKILL.md"
    link = home / ".codex" / "skills" / "alpha"
    assert central.is_file()
    assert link.is_symlink()


def test_cli_status_reports_agent_skills(home: Path) -> None:
    _agent_skill(home)
    code, envelope, _err = run_cli_streams(["skills", "status", "--json"], home)

    assert code == 0
    rows = envelope["data"]["agents"]["codex"]["skills"]
    assert rows == [
        {"name": "alpha", "how": "manual", "drift": False, "conflict": False}
    ]


def test_cli_check_skips_skills_that_are_not_git_checkouts(home: Path) -> None:
    make_skill(home / ".conductor" / "skills", "alpha")
    code, envelope, err = run_cli_streams(["skills", "check", "--json"], home)

    assert code == 0 and err == ""
    assert envelope["command"] == "skills check"
    assert envelope["data"] == []


def test_cli_update_requires_a_name(home: Path) -> None:
    code, out, envelope = run_cli_streams(["skills", "update", "--json"], home)

    assert code == 2 and out == ""
    assert envelope["exit_code"] == 2
    assert envelope["errors"] == [
        {"code": "usage", "message": "update requires a skill name"}
    ]


def test_cli_update_unknown_skill_is_a_runtime_error(home: Path) -> None:
    code, out, envelope = run_cli_streams(["skills", "update", "ghost", "--json"], home)

    assert code == 1 and out == ""
    assert envelope["exit_code"] == 1
    assert envelope["errors"][0]["code"] == "cli_error"
    assert "ghost" in envelope["errors"][0]["message"]


def test_cli_backup_requires_remote(home: Path) -> None:
    code, out, envelope = run_cli_streams(["skills", "backup", "--json"], home)

    assert code == 2 and out == ""
    assert envelope["exit_code"] == 2
    assert envelope["command"] == "skills backup"
    assert envelope["errors"][0]["message"] == "backup requires --remote"


def test_cli_backup_copies_to_a_local_path(home: Path, tmp_path: Path) -> None:
    make_skill(home / ".conductor" / "skills", "alpha")
    dest = tmp_path / "backup"
    code, envelope, err = run_cli_streams(
        ["skills", "backup", "--remote", str(dest), "--json"], home
    )

    assert code == 0 and err == ""
    assert envelope["data"]["dest"] == str(dest)
    assert (dest / "alpha" / "SKILL.md").is_file()


def test_cli_backup_pushes_a_network_remote_without_echoing_userinfo(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Push uses git argv with userinfo removed. The JSON must not echo it."""
    import skills_hub.updates as updates

    make_skill(home / ".conductor" / "skills", "alpha")
    remote = "https://synthetic-user:synthetic-userinfo@example.com/skills.git"
    real = updates._git
    pushed: list[tuple[str, ...]] = []

    def wrapped(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args and args[0] in {"fetch", "clone"}:
            raise AssertionError("backup must not fetch or clone")
        if args and args[0] == "push":
            pushed.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        return real(cwd, *args)

    monkeypatch.setattr(updates, "_git", wrapped)
    code, envelope, err = run_cli_streams(
        ["skills", "backup", "--remote", remote, "--json"], home
    )

    assert code == 0 and err == ""
    rendered = json.dumps(envelope)
    assert "synthetic-userinfo" not in rendered
    assert envelope["data"]["pushed"] is True
    host = str(envelope["data"]["remote"]).split("://", 1)[-1].split("/", 1)[0]
    assert host == "example.com"
    assert pushed
    push_host = pushed[0][1].split("://", 1)[-1].split("/", 1)[0]
    assert push_host == "example.com"
