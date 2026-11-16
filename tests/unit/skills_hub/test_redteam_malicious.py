"""Red-team probes for skill adopt and delivery.

A probe passes only when the attack is blocked. Do not loosen an assertion
to match a vulnerable result.
"""

from __future__ import annotations

import os
import shutil
import signal
import threading
from pathlib import Path

import pytest

from src.skills_hub.adopt import apply_adopt, plan_adopt
from src.skills_hub.core import SkillsError
from src.skills_hub.delivery import DeliveryError, plan_delivery

_MARKER = "REDTEAM-MARKER-OUTSIDE-7f3a9c"
_INJECT_EXEC = "REDTEAM-SKILLMD-EXEC"


class _NameTrap:
    """Directory entry whose name cannot exist as a single POSIX component."""

    def __init__(self, name: str) -> None:
        self.name = name

    def is_symlink(self) -> bool:
        return False

    def is_dir(self) -> bool:
        return True

    def __fspath__(self) -> str:
        raise AssertionError(f"used hostile skill name {self.name!r} as a path")

    def __truediv__(self, other: object) -> Path:
        raise AssertionError(f"joined hostile skill name {self.name!r}")


def _wipe(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _files_with_marker(root: Path, marker: str) -> list[str]:
    """Regular files under ``root`` whose own bytes contain ``marker``.

    Symlinks are not followed and are not treated as copies of the target.
    """
    if not root.exists():
        return []
    needle = marker.encode("utf-8")
    hits: list[str] = []
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            if needle in path.read_bytes():
                hits.append(str(path))
    return hits


def _signature(root: Path) -> tuple:
    """Stable picture of one agent skill directory, without following links."""
    if not root.exists():
        return (("missing", str(root)),)
    rows: list[tuple] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        base = Path(dirpath)
        for name in dirnames:
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                rows.append(("symlink", rel, os.readlink(path)))
            else:
                rows.append(("dir", rel))
        for name in sorted(filenames):
            path = base / name
            rel = path.relative_to(root).as_posix()
            if path.is_symlink():
                rows.append(("symlink", rel, os.readlink(path)))
            else:
                rows.append(("file", rel, path.read_bytes()))
    return tuple(rows)


def _finish_within(seconds: float, fn):
    def handler(signum, frame):
        raise TimeoutError(f"did not finish within {seconds} seconds")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _refuse(fn) -> None:
    with pytest.raises(SkillsError):
        fn()


def test_plan_and_apply_refuse_hostile_skill_names(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = home / ".claude" / "skills"
    agent.mkdir(parents=True)
    body = f"---\nname: x\ndescription: x\n---\n\n{_MARKER}\n"

    for name in ("..\\windows", "bad\nname"):
        skill = agent / name
        _wipe(skill)
        skill.mkdir()
        (skill / "SKILL.md").write_text(body, encoding="utf-8")
        agents = [("claude", agent)]
        _refuse(lambda: plan_adopt(home, repo, agents))
        _refuse(lambda: apply_adopt(home, repo, agents))
        assert _files_with_marker(repo, _MARKER) == []
        _wipe(skill)

    real_iterdir = Path.iterdir
    for name in ("../../.ssh", "foo/bar"):

        def fake(self: Path, _name: str = name) -> object:
            if Path(os.path.abspath(self)) == Path(os.path.abspath(agent)):
                return iter([_NameTrap(_name)])
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", fake)
        agents = [("claude", agent)]
        _refuse(lambda: plan_adopt(home, repo, agents))
        _refuse(lambda: apply_adopt(home, repo, agents))
        assert not (home / ".ssh").exists()
        assert not (agent / "foo").exists()
        assert _files_with_marker(repo, _MARKER) == []


def test_adopt_symlink_outside_home_is_not_copied(
    home: Path, repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text(_MARKER, encoding="utf-8")
    (outside / "SKILL.md").write_text(
        f"---\nname: leak\ndescription: leak\n---\n\n{_MARKER}\n",
        encoding="utf-8",
    )
    before = secret.read_bytes()
    agent = home / ".claude" / "skills"
    agent.mkdir(parents=True)
    (agent / "leak").symlink_to(outside, target_is_directory=True)
    agents = [("claude", agent)]

    with pytest.raises(SkillsError):
        plan_adopt(home, repo, agents)
    with pytest.raises(SkillsError):
        apply_adopt(home, repo, agents)

    assert _files_with_marker(repo, _MARKER) == []
    assert secret.read_bytes() == before


def test_symlink_loops_finish_within_five_seconds(home: Path, tmp_path: Path) -> None:
    repo = home / ".conductor" / "skills"
    repo.mkdir(parents=True)
    (repo / "a").symlink_to("b", target_is_directory=True)
    (repo / "b").symlink_to("a", target_is_directory=True)
    agent = home / ".claude" / "skills"
    agent.mkdir(parents=True)
    (agent / "c").symlink_to("d", target_is_directory=True)
    (agent / "d").symlink_to("c", target_is_directory=True)
    skill = agent / "okname"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: okname\ndescription: ok\n---\n\nbody\n",
        encoding="utf-8",
    )
    (skill / "p").symlink_to("q", target_is_directory=True)
    (skill / "q").symlink_to("p", target_is_directory=True)
    agents = [("claude", agent)]

    for fn in (
        lambda: plan_adopt(home, repo, agents),
        lambda: apply_adopt(home, repo, agents),
    ):
        try:
            _finish_within(5, fn)
        except SkillsError:
            pass

    left = tmp_path / "repo-loop"
    right = tmp_path / "agent-loop"
    left.symlink_to(right, target_is_directory=True)
    right.symlink_to(left, target_is_directory=True)
    try:
        _finish_within(5, lambda: plan_adopt(home, left, [("claude", right)]))
    except (SkillsError, OSError):
        pass
    try:
        _finish_within(5, lambda: apply_adopt(home, left, [("claude", right)]))
    except (SkillsError, OSError):
        pass


def test_skill_md_injection_is_stored_not_executed(
    home: Path, repo: Path, tmp_path: Path
) -> None:
    executed = tmp_path / _INJECT_EXEC
    payload = (
        "---\n"
        "name: alpha\n"
        'description: "toml [mcp_servers.evil]\\nuserID = \\"pwned\\""\n'
        "---\n\n"
        "[mcp_servers.evil]\n"
        'userID = "pwned"\n'
        '{"extra": true, "path": "../outside.json"}\n'
        f"import os; os.makedirs({str(executed)!r})\n"
    )
    agent = home / ".claude" / "skills"
    agent.mkdir(parents=True)
    skill = agent / "alpha"
    skill.mkdir()
    (skill / "SKILL.md").write_text(payload, encoding="utf-8")
    before = _all_files(home)

    apply_adopt(home, repo, [("claude", agent)])

    stored = repo / "alpha" / "SKILL.md"
    assert stored.is_file()
    assert stored.read_text(encoding="utf-8") == payload
    assert not executed.exists()
    allowed = {repo / "alpha", repo / "manifest.json", repo / "snapshots"}
    for path in _all_files(home) - before:
        assert _under_allowed(Path(path), allowed), path


def test_concurrent_apply_adopt_leaves_a_whole_skill(home: Path, repo: Path) -> None:
    agent = home / ".claude" / "skills"
    agent.mkdir(parents=True)
    skill = agent / "alpha"
    skill.mkdir()
    body = "---\nname: alpha\ndescription: alpha\n---\n\nconcurrent-body\n"
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    (skill / "extra.txt").write_text("extra-bytes", encoding="utf-8")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []
    uncaught: list[BaseException] = []

    def hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is not None:
            uncaught.append(args.exc_value)

    previous = threading.excepthook
    threading.excepthook = hook

    def worker() -> None:
        try:
            barrier.wait(10)
            apply_adopt(home, repo, [("claude", agent)])
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
    finally:
        threading.excepthook = previous

    assert not uncaught
    assert not any(thread.is_alive() for thread in threads)
    assert _skills_are_complete(repo)
    adopted = agent / "alpha" / "SKILL.md"
    assert adopted.is_file()
    text = adopted.read_text(encoding="utf-8")
    assert "concurrent-body" in text
    assert text.endswith("\n")


def test_interrupted_apply_does_not_leave_a_partial_agent_tree(
    home: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.skills_hub.adopt as adopt_mod

    agent = home / ".claude" / "skills"
    agent.mkdir(parents=True)
    repo.mkdir(parents=True, exist_ok=True)
    skill = agent / "alpha"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: alpha\n---\n\nkeep-me\n",
        encoding="utf-8",
    )
    (skill / "extra.txt").write_text("keep-extra", encoding="utf-8")
    before = _signature(agent)
    state = {"armed": False, "hits": 0}
    real_snapshot = adopt_mod.create_snapshot
    real_copytree = adopt_mod.shutil.copytree

    def snapshot(*args, **kwargs):
        result = real_snapshot(*args, **kwargs)
        state["armed"] = True
        return result

    def copytree(*args, **kwargs):
        if state["armed"]:
            state["hits"] += 1
            raise OSError("redteam: interrupted write after snapshot")
        return real_copytree(*args, **kwargs)

    monkeypatch.setattr(adopt_mod, "create_snapshot", snapshot)
    monkeypatch.setattr(adopt_mod.shutil, "copytree", copytree)

    with pytest.raises(OSError, match="interrupted write"):
        apply_adopt(home, repo, [("claude", agent)])

    assert state["hits"] >= 1
    after = _signature(agent)
    assert after == before, f"agent tree changed:\n before={before!r}\n after={after!r}"


def test_plan_delivery_rejects_traversal_and_keeps_dest_in_sandbox(
    tmp_path: Path,
) -> None:
    sand = tmp_path / "sand"
    sand.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text(_MARKER, encoding="utf-8")
    actions = None
    try:
        actions = plan_delivery({"skills": ["../../outside"]}, sand)
    except (DeliveryError, SkillsError):
        actions = None
    if actions:
        for action in actions:
            dest = os.path.normpath(action.dest)
            home_s = os.path.normpath(str(sand))
            assert dest == home_s or dest.startswith(home_s + os.sep), dest
    assert actions is None

    actions = None
    try:
        actions = plan_delivery(
            {
                "skills": ["../../outside"],
                "skill_sources": {"../../outside": str(outside)},
                "allowed_roots": [str(tmp_path)],
            },
            sand,
        )
    except (DeliveryError, SkillsError):
        actions = None
    assert actions is None
    assert _files_with_marker(sand, _MARKER) == []


def test_plan_delivery_symlink_farm_dest_stays_inside_sandbox(tmp_path: Path) -> None:
    sand = tmp_path / "sand"
    sand.mkdir()
    farm = tmp_path / "farm"
    farm.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text(_MARKER, encoding="utf-8")
    (farm / "l1").symlink_to("l2", target_is_directory=True)
    (farm / "l2").symlink_to("l3", target_is_directory=True)
    (farm / "l3").symlink_to(outside, target_is_directory=True)
    for index in range(6):
        (farm / f"extra-{index}").symlink_to(
            f"l{(index % 3) + 1}", target_is_directory=True
        )

    try:
        actions = plan_delivery(
            {
                "skills": ["kept"],
                "skill_sources": {"kept": str(farm / "l1")},
                "allowed_roots": [str(farm)],
            },
            sand,
        )
    except (DeliveryError, SkillsError):
        return

    home_s = os.path.normpath(str(sand))
    outside_s = os.path.normpath(str(outside))
    assert actions
    for action in actions:
        dest = os.path.normpath(action.dest)
        assert dest == home_s or dest.startswith(home_s + os.sep), action
        resolved = os.path.normpath(str(Path(dest).resolve()))
        assert resolved == home_s or resolved.startswith(home_s + os.sep), resolved
        assert not (resolved == outside_s or resolved.startswith(outside_s + os.sep))


def _all_files(root: Path) -> set[str]:
    found: set[str] = set()
    if not root.exists():
        return found
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            found.add(str(Path(dirpath) / name))
    return found


def _under_allowed(path: Path, allowed: set[Path]) -> bool:
    for root in allowed:
        if path == root:
            return True
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _skills_are_complete(repo: Path) -> bool:
    if not repo.exists():
        return True
    for child in repo.iterdir():
        if child.name.startswith(".") or child.name in {"snapshots", "presets", "projects"}:
            continue
        if child.name == "manifest.json":
            continue
        if not (child.is_symlink() or child.is_dir()):
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.is_file() or skill_md.stat().st_size == 0:
            return False
    return True
