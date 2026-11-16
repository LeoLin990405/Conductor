"""plan_delivery is a pure plan: paths stay in the isolated home, secrets do not."""

from __future__ import annotations

import json
import os
import posixpath
import tomllib
from pathlib import Path

import pytest

from src.skills_hub.core import SkillsError
from src.skills_hub.delivery import (
    KIND_MCP,
    Action,
    DeliveryError,
    apply_delivery,
    plan_delivery,
)
from src.skills_hub.presets import Preset

SECRET = "sk-" "live-do-not-leak-9f3a"


def _inventory(root: Path) -> tuple[tuple[str, int, int], ...]:
    if not root.exists():
        return ()
    rows: list[tuple[str, int, int]] = []
    for path in (root, *root.rglob("*")):
        st = path.lstat()
        rows.append((str(path), st.st_mode, st.st_size))
    return tuple(sorted(rows))


def _assert_inside(dest: str, home: Path | str) -> None:
    home_s = posixpath.normpath(os.fspath(home))
    dest_s = posixpath.normpath(dest)
    assert posixpath.isabs(dest_s)
    assert dest_s == home_s or dest_s.startswith(home_s.rstrip("/") + "/")
    assert not dest_s.startswith("/etc")


def _preset(tmp_path: Path, *, link_mode: str | None = None) -> dict[str, object]:
    repo = tmp_path / "repo"
    for name in ("zeta", "alpha"):
        skill = repo / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    preset: dict[str, object] = {
        "skills": ["zeta", "alpha", "zeta"],
        "mcp": ["zeta", "search"],
        "skill_sources": {
            "zeta": str(repo / "zeta"),
            "alpha": str(repo / "alpha"),
        },
        "mcp_documents": {
            "search": {
                "command": "uvx",
                "args": ["mcp-search"],
                "env": {
                    "API_TOKEN": "${API_TOKEN}",
                    "PASSWORD": "keychain:conductor/search",
                },
            },
            "zeta": {"command": "/opt/secret/bin"},
        },
        "allowed_roots": [str(repo)],
    }
    if link_mode is not None:
        preset["link_mode"] = link_mode
    return preset


def test_happy_path_skills_and_mcp(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    preset = _preset(tmp_path)
    before = _inventory(tmp_path)
    actions = plan_delivery(preset, home)

    assert [action.kind for action in actions] == [
        "link_skill",
        "link_skill",
        "write_mcp",
    ]
    assert actions[0].source == str(tmp_path / "repo" / "zeta")
    assert actions[0].dest == posixpath.join(
        str(home), ".conductor-delivered", "skills", "zeta"
    )
    assert actions[1].dest == posixpath.join(
        str(home), ".conductor-delivered", "skills", "alpha"
    )
    assert actions[0].mode == "symlink"
    assert actions[2].mode == "600"
    assert actions[2].dest == posixpath.join(
        str(home), ".conductor-delivered", "mcp", "servers.toml"
    )
    for action in actions:
        _assert_inside(action.dest, home)
        assert isinstance(action, Action)
    body = actions[2].source
    assert SECRET not in body
    parsed = tomllib.loads(body)
    assert list(parsed["servers"]) == ["zeta", "search"]
    assert parsed["servers"]["search"]["env"]["API_TOKEN"] == "${API_TOKEN}"
    assert parsed["servers"]["search"]["env"]["PASSWORD"] == "keychain:conductor/search"
    assert parsed["servers"]["zeta"]["command"] == "/opt/secret/bin"
    json.dumps([action.to_dict() for action in actions])
    assert _inventory(tmp_path) == before


def test_object_preset_and_copy_mode(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    raw = _preset(tmp_path, link_mode="copy")

    class Bundle:
        skills = raw["skills"]
        mcp = raw["mcp"]
        link_mode = "copy"
        skill_sources = raw["skill_sources"]
        mcp_documents = raw["mcp_documents"]
        allowed_roots = raw["allowed_roots"]

    actions = plan_delivery(Bundle(), home)
    mapped = plan_delivery(raw, home)

    assert actions == mapped
    assert [action.kind for action in actions] == [
        "copy_skill",
        "copy_skill",
        "write_mcp",
    ]
    assert actions[0].mode == "copy"

    named = plan_delivery(Preset(name="core", skills=("alpha",)), home)
    assert len(named) == 1
    assert named[0].kind == "link_skill"
    assert named[0].source == ""
    assert named[0].dest.endswith("/.conductor-delivered/skills/alpha")


def test_agent_home_rel_overrides_skill_dest(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    preset = _preset(tmp_path)
    preset["agent_home_rel"] = ".agents/skills"

    actions = plan_delivery(preset, home)

    assert actions[0].dest == posixpath.join(str(home), ".agents", "skills", "zeta")
    assert actions[-1].dest == posixpath.join(
        str(home), ".conductor-delivered", "mcp", "servers.toml"
    )
    _assert_inside(actions[0].dest, home)


def test_traversal_name_raises_before_any_plan(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    before = _inventory(tmp_path)
    preset = {
        "skills": ["alpha", "../x"],
        "mcp": ["ok"],
        "skill_sources": {"alpha": str(tmp_path / "repo" / "alpha")},
        "mcp_documents": {"ok": {"env": {"API_TOKEN": "${API_TOKEN}"}}},
    }

    with pytest.raises((DeliveryError, SkillsError)) as caught:
        plan_delivery(preset, home)

    assert ".." in str(caught.value)
    assert SECRET not in str(caught.value)
    assert _inventory(tmp_path) == before


def test_secret_env_value_raises(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    before = _inventory(tmp_path)
    preset = {
        "skills": ["alpha"],
        "mcp": ["search"],
        "mcp_documents": {"search": {"env": {"API_TOKEN": SECRET, "OK": "1"}}},
    }

    with pytest.raises(DeliveryError) as caught:
        plan_delivery(preset, home)

    message = str(caught.value)
    assert SECRET not in message
    assert "API_TOKEN" in message
    assert _inventory(tmp_path) == before


def test_env_reference_is_allowed(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    actions = plan_delivery(
        {
            "mcp": ["search"],
            "mcp_documents": {
                "search": {
                    "env": {
                        "API_TOKEN": "${API_TOKEN}",
                        "password": "keychain:conductor/search",
                    }
                }
            },
        },
        home,
    )

    assert len(actions) == 1
    assert actions[0].kind == "write_mcp"
    parsed = tomllib.loads(actions[0].source)
    assert parsed["servers"]["search"]["env"]["API_TOKEN"] == "${API_TOKEN}"
    assert parsed["servers"]["search"]["env"]["password"] == "keychain:conductor/search"


def test_dest_cannot_escape_isolated_home(tmp_path: Path) -> None:
    home = "/tmp/sand"
    existed = Path(home).exists()
    attacks = [
        "../x",
        "../../etc/passwd",
        "/etc/passwd",
        "foo/bar",
        "foo\\bar",
        "bad\nname",
        "..",
        "/etc",
    ]
    try:
        for name in attacks:
            with pytest.raises((DeliveryError, SkillsError)):
                plan_delivery({"skills": [name]}, home)
            with pytest.raises((DeliveryError, SkillsError)):
                plan_delivery({"mcp": [name]}, home)
        with pytest.raises(DeliveryError):
            plan_delivery(
                {"skills": ["alpha"], "agent_home_rel": "../../etc"},
                home,
            )
        with pytest.raises(DeliveryError):
            plan_delivery({"skills": ["alpha"], "agent_home_rel": "/etc"}, home)
        with pytest.raises(DeliveryError):
            plan_delivery(
                {
                    "skills": ["alpha"],
                    "skill_sources": {"alpha": "/tmp/repo/../../etc/passwd"},
                    "allowed_roots": ["/tmp/repo"],
                },
                home,
            )
        actions = plan_delivery({"skills": ["alpha"], "mcp": ["search"]}, home)
        for action in actions:
            _assert_inside(action.dest, home)
            assert action.dest != "/etc"
            assert "/etc/" not in action.dest
        assert actions[0].dest == "/tmp/sand/.conductor-delivered/skills/alpha"
    finally:
        if not existed:
            assert not Path(home).exists()
            assert not Path(home).is_symlink()

    outside = tmp_path / "outside"
    outside.mkdir()
    sandbox = tmp_path / "sand"
    delivered = sandbox / ".conductor-delivered"
    delivered.mkdir(parents=True)
    (delivered / "skills").symlink_to(outside, target_is_directory=True)
    before = _inventory(tmp_path)
    with pytest.raises(DeliveryError):
        plan_delivery({"skills": ["alpha"]}, sandbox)
    assert _inventory(tmp_path) == before


def test_source_symlink_escape_raises(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = repo / "alpha"
    link.symlink_to(outside, target_is_directory=True)
    before = _inventory(tmp_path)

    with pytest.raises(DeliveryError):
        plan_delivery(
            {
                "skills": ["alpha"],
                "skill_sources": {"alpha": str(link)},
                "allowed_roots": [str(repo)],
            },
            home,
        )

    assert _inventory(tmp_path) == before

    real = repo / "real"
    real.mkdir()
    kept = repo / "kept"
    kept.symlink_to(real.name, target_is_directory=True)
    actions = plan_delivery(
        {
            "skills": ["kept"],
            "skill_sources": {"kept": str(kept)},
            "allowed_roots": [str(repo)],
        },
        home,
    )
    assert actions[0].kind == "link_skill"
    assert actions[0].source == str(kept)
    _assert_inside(actions[0].dest, home)


def test_source_symlink_chain_escape_raises(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    farm = tmp_path / "farm"
    farm.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (farm / "l1").symlink_to("l2", target_is_directory=True)
    (farm / "l2").symlink_to("l3", target_is_directory=True)
    (farm / "l3").symlink_to(outside, target_is_directory=True)
    before = _inventory(tmp_path)

    with pytest.raises(DeliveryError, match="symlink"):
        plan_delivery(
            {
                "skills": ["kept"],
                "skill_sources": {"kept": str(farm / "l1")},
                "allowed_roots": [str(farm)],
            },
            home,
        )

    assert _inventory(tmp_path) == before


def test_quoted_server_name_cannot_break_toml(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    name = 'weird"name'
    actions = plan_delivery(
        {
            "mcp": [name],
            "mcp_documents": {name: {"command": 'say "hi"'}},
        },
        home,
    )
    parsed = tomllib.loads(actions[0].source)
    assert parsed["servers"][name]["command"] == 'say "hi"'
    assert actions[0].source.count("\n") >= 1


def test_plan_delivery_creates_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom() -> Path:
        raise AssertionError("Path.home() was called")

    monkeypatch.setattr(Path, "home", boom)
    home = tmp_path / "sand"
    preset = _preset(tmp_path)
    before = _inventory(tmp_path)
    plan_delivery(preset, home)
    plan_delivery({}, home)
    plan_delivery({"skills": [], "mcp": []}, home)
    with pytest.raises(DeliveryError):
        plan_delivery({"skills": ["../x"]}, home)
    assert _inventory(tmp_path) == before
    assert not home.exists()


def test_plan_delivery_is_deterministic(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    preset = _preset(tmp_path)
    first = plan_delivery(preset, home)
    second = plan_delivery(preset, home)

    assert first == second
    assert [action.to_dict() for action in first] == [
        action.to_dict() for action in second
    ]


def test_multicomponent_dest_symlink_cannot_leave_home(tmp_path: Path) -> None:
    """A link target of ``via/nested`` or ``via/../pwned`` must not be written."""
    home = tmp_path / "sand"
    home.mkdir()
    outside = tmp_path / "mid-out"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    pwned = tmp_path / "pwned"
    pwned.mkdir()
    (home / "via").symlink_to(nested, target_is_directory=True)
    (home / ".conductor-delivered").symlink_to("via/nested", target_is_directory=True)

    with pytest.raises(DeliveryError, match="symlink"):
        plan_delivery({"skills": ["alpha"]}, home)
    assert list(nested.iterdir()) == []

    (home / ".conductor-delivered").unlink()
    (home / ".conductor-delivered").symlink_to("via/../pwned", target_is_directory=True)
    with pytest.raises(DeliveryError, match="symlink"):
        plan_delivery({"skills": ["alpha"]}, home)
    assert list(pwned.iterdir()) == []

    # Planted after a clean plan. The write-time recheck refuses it too.
    (home / ".conductor-delivered").unlink()
    actions = plan_delivery({"skills": ["alpha"]}, home)
    (home / ".conductor-delivered").symlink_to("via/nested", target_is_directory=True)
    from src.skills_hub.delivery import apply_delivery

    with pytest.raises(DeliveryError, match="symlink"):
        apply_delivery(actions, home)
    assert list(nested.iterdir()) == []


def test_two_hop_dest_symlink_is_refused_at_plan_and_at_write(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text(SECRET, encoding="utf-8")
    (home / "hop").symlink_to(outside, target_is_directory=True)
    (home / ".conductor-delivered").symlink_to("hop", target_is_directory=True)

    with pytest.raises(DeliveryError, match="symlink"):
        plan_delivery({"skills": ["alpha"]}, home)

    for child in list(home.iterdir()):
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            import shutil

            shutil.rmtree(child)
    actions = plan_delivery({"skills": ["alpha"]}, home)
    assert actions
    delivered = home / ".conductor-delivered"
    delivered.mkdir()
    (delivered / "skills").symlink_to(outside, target_is_directory=True)
    from src.skills_hub.delivery import apply_delivery

    with pytest.raises(DeliveryError, match="symlink"):
        apply_delivery(actions, home)
    assert list(outside.iterdir()) == [outside / "secret"]


def _mcp_action(dest: Path) -> Action:
    return Action(
        kind=KIND_MCP,
        source='{"mcpServers":{"x":{}}}\n',
        dest=str(dest),
        mode="file",
    )


def test_symlinked_home_is_rejected(tmp_path: Path) -> None:
    """A HOME path that is itself a symlink is not resolved into the target."""
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "isolated-home"
    linked.symlink_to(outside, target_is_directory=True)
    dest = linked / ".claude" / "mcp.json"

    with pytest.raises(DeliveryError, match="must not be a symlink"):
        plan_delivery(
            {"mcp": ["search"], "mcp_documents": {"search": {"command": "echo"}}},
            linked,
        )
    with pytest.raises(DeliveryError, match="must not be a symlink"):
        apply_delivery([_mcp_action(dest)], linked)

    assert list(outside.iterdir()) == []
    assert not (outside / ".claude").exists()


def test_symlinked_home_ancestor_is_rejected(tmp_path: Path) -> None:
    """An existing ancestor symlink is refused. The target is not the sandbox."""
    outside = tmp_path / "outside"
    outside.mkdir()
    via = tmp_path / "via"
    via.symlink_to(outside, target_is_directory=True)
    home = via / "worker"
    dest = home / ".claude" / "mcp.json"

    with pytest.raises(DeliveryError, match="ancestor must not be a symlink"):
        plan_delivery(
            {"mcp": ["search"], "mcp_documents": {"search": {"command": "echo"}}},
            home,
        )
    with pytest.raises(DeliveryError, match="ancestor must not be a symlink"):
        apply_delivery([_mcp_action(dest)], home)

    assert not (outside / "worker").exists()
    assert list(outside.iterdir()) == []


def test_real_directory_home_still_receives_mcp(tmp_path: Path) -> None:
    home = tmp_path / "sand"
    actions = plan_delivery(
        {"mcp": ["search"], "mcp_documents": {"search": {"command": "echo"}}},
        home,
    )

    written = apply_delivery(actions, home)

    dest = home / ".conductor-delivered" / "mcp" / "servers.toml"
    assert written == [str(dest)]
    assert dest.is_file() and not dest.is_symlink()
    assert dest.stat().st_mode & 0o777 == 0o600
    assert "echo" in dest.read_text(encoding="utf-8")


def test_planted_dest_tmp_symlink_is_not_followed(tmp_path: Path) -> None:
    """A pre-planted ``<dest>.tmp`` symlink is not written or chmod'd."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-original", encoding="utf-8")
    outside_mode = outside.stat().st_mode & 0o777
    dest = home / ".claude" / "mcp.json"
    planted = dest.with_name(dest.name + ".tmp")
    planted.symlink_to(outside)

    apply_delivery([_mcp_action(dest)], home)

    assert outside.read_text(encoding="utf-8") == "outside-original"
    assert outside.stat().st_mode & 0o777 == outside_mode
    assert planted.is_symlink()
    assert os.readlink(planted) == str(outside)
    assert dest.is_file() and not dest.is_symlink()
    assert dest.stat().st_mode & 0o777 == 0o600
    assert dest.read_text(encoding="utf-8").startswith("{")


def test_empty_preset(tmp_path: Path) -> None:
    home = tmp_path / "sand"

    class Empty:
        skills: tuple[str, ...] = ()

    assert plan_delivery({}, home) == []
    assert plan_delivery({"skills": [], "mcp": []}, home) == []
    assert plan_delivery(Empty(), home) == []
    assert plan_delivery(Preset(name="core", skills=()), home) == []
